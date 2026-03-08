#!/usr/bin/env python3
"""
MCP Claude History - Search Claude Code conversation history
(D-Heap: d=4 bounded heap, dheap_weight rank decay, orjson)

Usage: python server.py (stdio mode)
"""

import orjson
import math
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from itertools import combinations


# --- D-ary heap (d=4, min-heap, bounded size) ---

_D = 4  # branching factor


def _dh_push(heap: list, item: tuple) -> None:
    """Push item onto d-ary min-heap."""
    heap.append(item)
    _dh_sift_up(heap, len(heap) - 1)


def _dh_replace_min(heap: list, item: tuple) -> None:
    """Replace root (min) with item, restore heap property. Heap must be non-empty."""
    heap[0] = item
    _dh_sift_down(heap, 0)


def _dh_sift_up(heap: list, i: int) -> None:
    item = heap[i]
    while i > 0:
        parent = (i - 1) // _D
        if item < heap[parent]:
            heap[i] = heap[parent]
            i = parent
        else:
            break
    heap[i] = item


def _dh_sift_down(heap: list, i: int) -> None:
    n = len(heap)
    item = heap[i]
    while True:
        first_child = _D * i + 1
        if first_child >= n:
            break
        # find min child among up to _D children
        min_child = first_child
        for c in range(first_child + 1, min(first_child + _D, n)):
            if heap[c] < heap[min_child]:
                min_child = c
        if heap[min_child] < item:
            heap[i] = heap[min_child]
            i = min_child
        else:
            break
    heap[i] = item

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("claude-history")

PROJECTS_DIR = Path.home() / '.claude/projects'


def tokenize(text: str) -> List[str]:
    """Tokenize: Chinese → chars, English → words"""
    tokens = []
    current_en = ""

    for char in text:
        if '\u4e00' <= char <= '\u9fff':
            if current_en.strip():
                tokens.append(current_en.strip().lower())
                current_en = ""
            tokens.append(char)
        elif char.isalpha():
            current_en += char
        else:
            if current_en.strip():
                tokens.append(current_en.strip().lower())
                current_en = ""

    if current_en.strip():
        tokens.append(current_en.strip().lower())

    seen = set()
    return [t for t in tokens if t and not (t in seen or seen.add(t))][:10]


def create_pairs(tokens: List[str]) -> List[Tuple[str, str]]:
    return list(combinations(tokens, 2))


def count_pair_hits(text: str, pairs: List[Tuple[str, str]]) -> int:
    if isinstance(text, list):
        text = ' '.join(str(t) for t in text)
    text_lower = str(text).lower()
    return sum(1 for t1, t2 in pairs if t1 in text_lower and t2 in text_lower)


def dheap_weight(rank: int, d: int = 4) -> float:
    if rank <= 0:
        return 1.0
    layer = int(math.log(max(rank, 1)) / math.log(d))
    return 1.0 / (d ** layer)


def parse_since(since: Optional[str]) -> Optional[float]:
    """Parse since string like '7d', '24h', '30m' into a Unix timestamp cutoff."""
    if not since:
        return None
    since = since.strip().lower()
    units = {'d': 86400, 'h': 3600, 'm': 60}
    if since[-1] in units:
        try:
            delta = float(since[:-1]) * units[since[-1]]
            return (datetime.now(timezone.utc) - timedelta(seconds=delta)).timestamp()
        except ValueError:
            pass
    return None


@mcp.tool()
def search_history(
    query: str,
    limit: int = 3,
    since: Optional[str] = None,
    project: Optional[str] = None,
) -> List[Dict]:
    """
    Search Claude Code conversation history using a d=4 D-Heap algorithm.

    Scoring unit is the session (entire JSONL file): pair hits are accumulated
    across all user/assistant messages. The line returned points to the
    highest-hit message within the session — use it as the get_context entry point.

    Maintains a fixed-size heap of `limit` entries during scan; each candidate's
    weighted_score = session_hits * dheap_weight(heap_size) so the admission threshold
    rises as the heap fills — only strictly better sessions displace the current worst.

    Args:
        query: Search query. Supports Chinese (char-level) and English (word-level),
               or mixed. E.g. "transformer attention", "dheap 搜尋", "auth token jwt"
        limit: Max results to return (default 3)
        since: Restrict to files modified within this window.
               Format: "<number><unit>" where unit is d/h/m.
               Examples: "7d" (last 7 days), "24h" (last 24 hours), "30m" (last 30 min)
        project: Filter by project. Matches against the working directory path (cwd)
                 or the hashed project directory name. E.g. "myapp", "claude-history"

    Returns:
        List of dicts, each with:
          file    — session filename (pass to get_context)
          line    — highest-hit line in session (pass to get_context)
          hits    — total pair co-occurrence count across entire session
          score   — session hits / max_possible_pairs (>1.0 = repeated co-occurrence)
          snippet — ±100 chars around the best-hit line
    """
    tokens = tokenize(query)
    pairs = create_pairs(tokens)
    max_pairs = len(pairs)

    if max_pairs == 0:
        return []

    since_ts = parse_since(since)
    project_lower = project.lower() if project else None

    # Fixed-size min-heap of `limit` entries.
    # Key: (weighted_score, mtime, counter, data)
    # weighted_score = hits * dheap_weight(heap_size) — admission threshold rises as heap fills.
    # Min-heap: smallest weighted_score is evicted when a better entry arrives.
    heap = []
    counter = 0

    for session_file in PROJECTS_DIR.glob('*/*.jsonl'):
        project_dir = session_file.parent.name
        mtime = session_file.stat().st_mtime

        if since_ts and mtime < since_ts:
            continue

        if project_lower and project_lower not in project_dir.lower():
            continue

        try:
            cwd = None
            session_hits = 0
            best_line = 1
            best_hits = 0
            best_content = ''

            with open(session_file, 'rb') as f:
                for line_num, raw in enumerate(f, 1):
                    try:
                        # bytes pre-filter: skip non-user/assistant lines without parsing
                        if cwd is not None and b'"user"' not in raw and b'"assistant"' not in raw:
                            continue

                        entry = orjson.loads(raw)
                        msg_type = entry.get('type')

                        if cwd is None and entry.get('cwd'):
                            cwd = entry['cwd']

                        content = None

                        if msg_type == 'user' and not entry.get('isMeta'):
                            content = entry.get('message', {}).get('content', '')
                            if not (content and isinstance(content, str) and len(content) > 20):
                                content = None

                        elif msg_type == 'assistant':
                            content_items = entry.get('message', {}).get('content', [])
                            text = ""
                            for item in content_items:
                                if isinstance(item, dict) and item.get('type') == 'text':
                                    text += item.get('text', '')
                            if text and len(text) > 50:
                                content = text

                        if content:
                            hits = count_pair_hits(content, pairs)
                            session_hits += hits
                            # track the line with most hits as get_context entry point
                            if hits > best_hits:
                                best_hits = hits
                                best_line = line_num
                                best_content = content

                    except orjson.JSONDecodeError:
                        continue
                    except Exception:
                        continue

            # score the session as a whole
            if session_hits > 0:
                weight = dheap_weight(len(heap))
                weighted = session_hits * weight
                counter += 1
                entry_data = (
                    weighted,
                    mtime,
                    counter,
                    {
                        'project': project_dir,
                        'cwd': cwd or '',
                        'file': session_file.name,
                        'line': best_line,
                        'hits': session_hits,
                        'score': round(session_hits / max_pairs, 3),
                        'content': best_content,
                    }
                )
                if len(heap) < limit:
                    _dh_push(heap, entry_data)
                elif weighted > heap[0][0]:
                    _dh_replace_min(heap, entry_data)
        except Exception:
            continue

    # heap contains at most `limit` entries; sort descending by weighted_score then mtime
    heap.sort(key=lambda e: (-e[0], -e[1]))

    # Extract results from sorted heap
    results = []
    for weighted_score, mtime_val, _, item in heap:
        content = item['content']
        content_lower = content.lower()

        best_pos = 0
        for t1, t2 in pairs:
            pos1 = content_lower.find(t1)
            pos2 = content_lower.find(t2)
            if pos1 >= 0 and pos2 >= 0:
                best_pos = min(pos1, pos2)
                break

        start = max(0, best_pos - 100)
        end = min(len(content), best_pos + 100)
        snippet = content[start:end]
        if start > 0:
            snippet = '...' + snippet
        if end < len(content):
            snippet = snippet + '...'

        results.append({
            'file': item['file'],
            'line': item['line'],
            'hits': item['hits'],
            'score': item['score'],
            'snippet': snippet,
        })

    return results


@mcp.tool()
def search_stats() -> Dict:
    """
    Get corpus-wide statistics about Claude Code conversation history.

    Scans all sessions and returns message counts, token usage totals, and a
    list of known projects with their working directory paths.

    Returns:
        total_messages       — user + assistant message count
        user_messages        — user turn count
        assistant_messages   — assistant turn count
        total_input_tokens   — cumulative input tokens across all sessions
        total_output_tokens  — cumulative output tokens across all sessions
        projects             — number of distinct project directories
        project_list         — list of {dir, cwd} for each project
    """
    total_user = 0
    total_assistant = 0
    total_input_tokens = 0
    total_output_tokens = 0
    projects = set()
    cwd_map = {}  # project_dir -> cwd

    for session_file in PROJECTS_DIR.glob('*/*.jsonl'):
        project_dir = session_file.parent.name
        projects.add(project_dir)
        try:
            with open(session_file, 'rb') as f:
                for line in f:
                    try:
                        entry = orjson.loads(line)
                        msg_type = entry.get('type')

                        if entry.get('cwd') and project_dir not in cwd_map:
                            cwd_map[project_dir] = entry['cwd']

                        if msg_type == 'user' and not entry.get('isMeta'):
                            content = entry.get('message', {}).get('content', '')
                            if content and isinstance(content, str) and len(content) > 20:
                                total_user += 1

                        elif msg_type == 'assistant':
                            content_items = entry.get('message', {}).get('content', [])
                            text = ''.join(
                                item.get('text', '')
                                for item in content_items
                                if isinstance(item, dict) and item.get('type') == 'text'
                            )
                            if text and len(text) > 50:
                                total_assistant += 1
                            usage = entry.get('message', {}).get('usage', {})
                            total_input_tokens += usage.get('input_tokens', 0)
                            total_output_tokens += usage.get('output_tokens', 0)
                    except:
                        continue
        except:
            continue

    project_info = [
        {'dir': d, 'cwd': cwd_map.get(d, '')}
        for d in sorted(projects)
    ]

    return {
        'total_messages': total_user + total_assistant,
        'user_messages': total_user,
        'assistant_messages': total_assistant,
        'total_input_tokens': total_input_tokens,
        'total_output_tokens': total_output_tokens,
        'projects': len(projects),
        'project_list': project_info,
    }


@mcp.tool()
def get_context(file: str, line: int, context_lines: int = 5) -> Dict:
    """
    Get conversation context around a specific line in a session file.

    Use the `file` and `line` values returned by search_history to retrieve
    the surrounding messages for a search hit.

    Args:
        file: Session filename from search_history result,
              e.g. "860e858e-2203-461e-a2e1-4fccb0611830.jsonl"
        line: Line number (1-indexed) from search_history result
        context_lines: Number of messages before and after the target line
                       to include (default 5, i.e. up to 11 messages total)

    Returns:
        file           — session filename
        target_line    — the requested line number
        context_range  — actual line range read, e.g. "37-47"
        messages       — list of {line, type, content, is_target}
        total_messages — number of messages returned
    """
    target_file = None
    for session_file in PROJECTS_DIR.glob('*/*.jsonl'):
        if session_file.name == file:
            target_file = session_file
            break

    if not target_file:
        return {
            'error': f'File not found: {file}',
            'searched_in': str(PROJECTS_DIR)
        }

    start_line = max(1, line - context_lines)
    end_line = line + context_lines

    context_messages = []
    try:
        with open(target_file, 'rb') as f:
            for line_num, json_line in enumerate(f, 1):
                if start_line <= line_num <= end_line:
                    try:
                        entry = orjson.loads(json_line)
                        msg_type = entry.get('type')

                        if msg_type == 'user':
                            content = entry.get('message', {}).get('content', '')
                        elif msg_type == 'assistant':
                            content_items = entry.get('message', {}).get('content', [])
                            content = ""
                            for item in content_items:
                                if isinstance(item, dict):
                                    if item.get('type') == 'text':
                                        content += item.get('text', '')
                                    elif item.get('type') == 'tool_use':
                                        tool_input = item.get('input', {})
                                        input_str = orjson.dumps(tool_input).decode()[:200]
                                        content += f"\n[Tool: {item.get('name')} {input_str}]"
                        else:
                            content = str(entry.get('message', ''))

                        context_messages.append({
                            'line': line_num,
                            'type': msg_type,
                            'content': content[:1000] if content else '',
                            'is_target': line_num == line
                        })
                    except:
                        continue

                if line_num > end_line:
                    break
    except Exception as e:
        return {
            'error': f'Failed to read file: {str(e)}'
        }

    return {
        'file': file,
        'target_line': line,
        'context_range': f'{start_line}-{end_line}',
        'messages': context_messages,
        'total_messages': len(context_messages)
    }


if __name__ == '__main__':
    mcp.run()
