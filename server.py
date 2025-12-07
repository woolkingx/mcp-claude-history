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
                for line in f:
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
                                    'session': session_file.stem[:8]
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
                                    'session': session_file.stem[:8]
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
            'content': item['content'][:500]  # Truncate for response
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


if __name__ == '__main__':
    mcp.run()
