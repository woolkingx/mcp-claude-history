#!/usr/bin/env python3
"""
MCP Claude History - Search Claude Code conversation history

Usage: python server.py (stdio mode)
"""

import json
import math
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


def load_all_messages() -> List[Dict]:
    """Load all messages from all sessions"""
    messages = []

    for session_file in PROJECTS_DIR.glob('*/*.jsonl'):
        project = session_file.parent.name
        try:
            with open(session_file, 'r', encoding='utf-8') as f:
                for line_num, line in enumerate(f, 1):
                    try:
                        entry = json.loads(line)
                        msg_type = entry.get('type')

                        if msg_type == 'user' and not entry.get('isMeta'):
                            content = entry.get('message', {}).get('content', '')
                            if content and isinstance(content, str) and len(content) > 20:
                                messages.append({
                                    'project': project,
                                    'type': 'user',
                                    'content': content,
                                    'session': session_file.stem[:8],
                                    'file': session_file.name,
                                    'line': line_num
                                })

                        elif msg_type == 'assistant':
                            content_items = entry.get('message', {}).get('content', [])
                            text = ""
                            for item in content_items:
                                if isinstance(item, dict) and item.get('type') == 'text':
                                    text += item.get('text', '')
                            if text and len(text) > 50:
                                messages.append({
                                    'project': project,
                                    'type': 'assistant',
                                    'content': text,
                                    'session': session_file.stem[:8],
                                    'file': session_file.name,
                                    'line': line_num
                                })
                    except:
                        continue
        except:
            continue

    return messages


@mcp.tool()
def search_history(query: str, limit: int = 3) -> List[Dict]:
    """
    Search Claude Code conversation history using D-Heap algorithm.

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

    messages = load_all_messages()

    scored = []
    for msg in messages:
        hits = count_pair_hits(msg['content'], pairs)
        if hits > 0:
            scored.append({**msg, 'hits': hits, 'normalized': hits / max_pairs})

    scored.sort(key=lambda x: x['hits'], reverse=True)

    results = []
    for rank, item in enumerate(scored[:limit], 1):
        weight = dheap_weight(rank, 4)
        results.append({
            'rank': rank,
            'score': round(item['normalized'] * weight, 4),
            'hits': item['hits'],
            'project': item['project'],
            'type': item['type'],
            'content': item['content'][:500],  # Truncate for response
            'file': item['file'],
            'line': item['line']
        })

    return results


@mcp.tool()
def search_stats() -> Dict:
    """Get statistics about conversation history"""
    messages = load_all_messages()
    projects = set(m['project'] for m in messages)
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
    # Find the file in projects directory
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

    # Read context lines
    start_line = max(1, line - context_lines)
    end_line = line + context_lines

    context_messages = []
    try:
        with open(target_file, 'r', encoding='utf-8') as f:
            for line_num, json_line in enumerate(f, 1):
                if start_line <= line_num <= end_line:
                    try:
                        entry = json.loads(json_line)
                        msg_type = entry.get('type')

                        # Extract content based on message type
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
                            'content': content[:1000] if content else '',  # Limit length
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
