#!/usr/bin/env python3
"""
MCP Claude History - Search Claude Code conversation history
(D-Heap optimized version: orjson + heap insert on match)

Usage: python server_dheap.py (stdio mode)
"""

import orjson
import math
import heapq
from pathlib import Path
from typing import List, Dict, Tuple
from itertools import combinations

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


@mcp.tool()
def search_history(query: str, limit: int = 3) -> List[Dict]:
    """
    Search Claude Code conversation history using D-Heap algorithm.
    (Optimized: insert into heap on match, no separate sort step)

    Args:
        query: Search query (Chinese/English mixed)
        limit: Max results to return (default 3)

    Returns:
        List of matching conversations with score and content
    """
    tokens = tokenize(query)
    pairs = create_pairs(tokens)
    max_pairs = len(pairs)

    if max_pairs == 0:
        return []

    # D-Heap: max-heap using negative values
    # Key: (-hits, -mtime, counter, data) for sorting by hits desc, then mtime desc
    heap = []
    counter = 0

    for session_file in PROJECTS_DIR.glob('*/*.jsonl'):
        project = session_file.parent.name
        mtime = session_file.stat().st_mtime  # file modification time

        try:
            with open(session_file, 'rb') as f:
                for line_num, line in enumerate(f, 1):
                    try:
                        entry = orjson.loads(line)
                        msg_type = entry.get('type')
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
                            if hits > 0:
                                counter += 1
                                # Insert into heap: (-hits, -mtime, counter) for max-heap behavior
                                heapq.heappush(heap, (
                                    -hits,
                                    -mtime,
                                    counter,
                                    {
                                        'project': project,
                                        'type': msg_type,
                                        'content': content,
                                        'session': session_file.stem[:8],
                                        'file': session_file.name,
                                        'line': line_num,
                                        'hits': hits,
                                        'normalized': hits / max_pairs
                                    }
                                ))
                    except:
                        continue
        except:
            continue

    # Extract top N from heap
    results = []
    rank = 0
    while heap and rank < limit:
        rank += 1
        neg_hits, neg_mtime, _, item = heapq.heappop(heap)

        # 找 hit 位置，取前後各 100 字
        content = item['content']
        content_lower = content.lower()

        # 找第一個匹配的 token 位置
        best_pos = 0
        for t1, t2 in pairs:
            pos1 = content_lower.find(t1)
            pos2 = content_lower.find(t2)
            if pos1 >= 0 and pos2 >= 0:
                best_pos = min(pos1, pos2)
                break

        # 取前後各 100 字
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
            'snippet': snippet
        })

    return results


@mcp.tool()
def search_stats() -> Dict:
    """Get statistics about conversation history"""
    messages = []
    projects = set()

    for session_file in PROJECTS_DIR.glob('*/*.jsonl'):
        project = session_file.parent.name
        projects.add(project)
        try:
            with open(session_file, 'rb') as f:
                for line_num, line in enumerate(f, 1):
                    try:
                        entry = orjson.loads(line)
                        msg_type = entry.get('type')

                        if msg_type == 'user' and not entry.get('isMeta'):
                            content = entry.get('message', {}).get('content', '')
                            if content and isinstance(content, str) and len(content) > 20:
                                messages.append(1)

                        elif msg_type == 'assistant':
                            content_items = entry.get('message', {}).get('content', [])
                            text = ""
                            for item in content_items:
                                if isinstance(item, dict) and item.get('type') == 'text':
                                    text += item.get('text', '')
                            if text and len(text) > 50:
                                messages.append(1)
                    except:
                        continue
        except:
            continue

    return {
        'total_messages': len(messages),
        'projects': len(projects),
        'project_list': sorted(projects)
    }


@mcp.tool()
def get_context(file: str, line: int, context_lines: int = 5) -> Dict:
    """
    Get conversation context around a specific line.

    Args:
        file: Filename (e.g., "860e858e-2203-461e-a2e1-4fccb0611830.jsonl")
        line: Line number (1-indexed)
        context_lines: Number of lines before/after to include (default 5)

    Returns:
        Context with surrounding messages
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
                                        content += f"\n[Tool: {item.get('name')}]"
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
