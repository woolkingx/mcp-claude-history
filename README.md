# mcp-claude-history

Lightweight MCP server for searching Claude Code conversation history.

## TL;DR

Single-file Python server. Session-block scoring with true d=4 D-ary heap — finds the most relevant conversation, not just a line. Search 20k+ messages with `orjson` I/O.

## Installation

```bash
# Clone
git clone https://github.com/woolkingx/mcp-claude-history.git

# Install dependencies
pip install orjson mcp
# or:
pip install -e .

# Add to Claude Code
claude mcp add claude-history python3 /path/to/mcp-claude-history/server.py
```

## Tools

| Tool | Description | Parameters |
|------|-------------|------------|
| `search_history` | Search by token-pair co-occurrence, ranked by D-Heap | `query: str`, `limit: int = 3`, `since: str?`, `project: str?` |
| `search_stats` | Corpus statistics (message counts, token usage, projects) | — |
| `get_context` | Read surrounding messages around a search hit | `file: str`, `line: int`, `context_lines: int = 5` |

### search_history

```
query   — Chinese (char-level) or English (word-level) or mixed
limit   — max results (default 3)
since   — time window: "7d", "24h", "30m"
project — filter by project name or cwd path substring
```

Returns per result:
```
file    — session filename (pass to get_context)
line    — highest-hit line in session (get_context entry point)
hits    — total pair co-occurrence across entire session
score   — session hits / max_possible_pairs (>1.0 = repeated co-occurrence)
snippet — ±100 chars around the best-hit line
```

### get_context

```
file          — filename from search_history result
line          — line number from search_history result
context_lines — messages before/after to include (default 5, up to 11 total)
```

## Algorithm

```python
# 1. Tokenize: CJK → chars, English → words (max 10, deduped)
tokens = tokenize(query)  # ["softmax", "transformer", "注", "意", "力"]

# 2. Generate token pairs (implicit Q·K)
pairs = combinations(tokens, 2)  # C(5,2) = 10 pairs

# 3. Scan each session: accumulate hits across ALL messages
for session_file in sessions:
    session_hits = 0
    best_line, best_hits = 1, 0

    for line_num, raw in enumerate(file):
        # bytes pre-filter: skip summary/tool/system lines without JSON parse
        if b'"user"' not in raw and b'"assistant"' not in raw:
            continue
        hits = count_pair_hits(content, pairs)
        session_hits += hits
        if hits > best_hits:          # track densest line as entry point
            best_line, best_hits = line_num, hits

# 4. D-ary heap (d=4), bounded to `limit` entries
#    weighted_score = session_hits * dheap_weight(heap_size)
#    → admission threshold rises as heap fills
if len(heap) < limit:
    _dh_push(heap, (weighted_score, mtime, counter, session_item))
elif weighted_score > heap[0][0]:
    _dh_replace_min(heap, (weighted_score, mtime, counter, session_item))

# 5. Sort top-N by weighted_score desc, mtime desc
heap.sort(key=lambda e: (-e[0], -e[1]))
```

**dheap_weight(k)**: rank decay — `1 / (4 ** layer)` where layer = `floor(log_4(k))`.
Heap size 0–3 → weight 1.0. Size 4+ → weight 0.25. Later sessions need 4× hits to displace.

**Why session-block**: query tokens naturally split across user/assistant turns. Line-level scoring misses cross-message relevance. Session scoring captures the full conversation signal.

**Why d=4**: push-heavy workload (one push per session, few pops). `log_4(n)` sift-up layers vs `log_2(n)` in binary heap.

## Why It Works

| Concept | Transformer | D-Heap |
|---------|-------------|--------|
| Similarity | Q·K^T (explicit) | Co-occurrence (implicit) |
| Weights | softmax(scores) | dheap_weight (rank decay) |
| Parameters | Q, K, V matrices | Zero |
| Complexity | O(n²) | O(n · limit) |

Pair co-occurrence = implicit self-attention. Document content is the key matrix.

## Use Cases

1. **Recall past solutions** — find how you solved a similar problem last month
2. **Context navigation** — jump to a specific conversation point, read surrounding messages
3. **Code archaeology** — git shows what changed; this shows why
4. **Cross-project search** — search across all projects, or filter by `project=`

## Changelog

See [CHANGELOG.md](CHANGELOG.md).

## License

Public domain. Copy, modify, do whatever you want.
