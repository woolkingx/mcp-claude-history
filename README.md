# D-Heap Search: Lightweight Conversation History Search

## TL;DR

250 lines Python, search 20k messages in ~3.5s with orjson + heap optimization.

## Installation

```bash
# Clone
git clone https://github.com/woolkingx/mcp-claude-history.git

# Install dependency
pip install orjson

# Add to Claude Code
claude mcp add claude-history python3 /path/to/mcp-claude-history/server.py
```

## Usage

```python
# Search conversation history
mcp__claude-history__search_history("your query", 3)

# Returns: file:line:hits | snippet (hit point ±100 chars)

# Get statistics
mcp__claude-history__search_stats()

# Get context around a specific line
mcp__claude-history__get_context(
    file="860e858e-2203-461e-a2e1-4fccb0611830.jsonl",
    line=42,
    context_lines=5
)
```

### Available Tools

| Tool | Description | Parameters |
|------|-------------|------------|
| `search_history` | D-Heap search with co-occurrence ranking | `query: str`, `limit: int = 3` |
| `search_stats` | Get corpus statistics | None |
| `get_context` | Retrieve conversation context around specific line | `file: str`, `line: int`, `context_lines: int = 5` |

## Core Algorithm

```python
# 1. Tokenize: Chinese → chars, English → words
tokens = tokenize(query)  # ["softmax", "transformer", "核", "心"]

# 2. Create pairs (implicit Q·K)
pairs = combinations(tokens, 2)  # 28 pairs

# 3. Count co-occurrence + insert into heap on match
hits = sum(1 for t1, t2 in pairs if t1 in doc and t2 in doc)
if hits > 0:
    heapq.heappush(heap, (-hits, -mtime, counter, item))

# 4. Sort: hits desc, then mtime desc (newer first)
```

## Why It Works

| Concept | Transformer | D-Heap |
|---------|-------------|--------|
| Similarity | Q·K^T (explicit) | Co-occurrence (implicit) |
| Weights | softmax(scores) | 1/d^layer |
| Parameters | Q,K,V matrices | **Zero** |
| Complexity | O(n²) | O(n) |

**Key insight**: Pair co-occurrence = implicit Self-Attention. Document content itself is the Key matrix.

## Benchmark

```
Query: "D-Heap 搜索 優化"

| Version | Time    | Speedup | Result Quality |
|---------|---------|---------|----------------|
| v1 json | 9200ms  | 1.0x    | Old conversations |
| v2 orjson+heap | 3500ms | 2.6x | Latest conversations |
```

- 2.6x faster (orjson)
- Better results (mtime sorting, newer first)
- Precise snippets (hit point ±100 chars)

## Changes in v2

- **orjson**: Replace json with orjson for 2.6x speedup
- **Heap insert**: Insert into heap on match, no separate sort step
- **mtime sorting**: Same hits → newer conversation first
- **Snippet**: Return hit point ±100 chars instead of content[:500]

## Use Cases

1. **Conversation history search** - Find past discussions with D-Heap ranking
2. **Context navigation** - Jump to specific conversation points and explore surrounding context
3. **Code archaeology** - Why was this changed? (Git shows what, this shows why)
4. **Agent/skill matching** - Match by content, not description

## License

Public domain. Copy, modify, do whatever you want.
