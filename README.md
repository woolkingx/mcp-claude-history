# D-Heap Search: Lightweight Conversation History Search

## TL;DR

150 lines Python, zero dependencies, search 20k messages in <2s.

## Installation

```bash
# Clone
git clone https://github.com/woolkingx/mcp-claude-history.git

# Add to Claude Code
claude mcp add claude-history python3 /path/to/mcp-claude-history/server.py

# Or manually edit ~/.claude.json
```

```json
{
  "mcpServers": {
    "claude-history": {
      "type": "stdio",
      "command": "python3",
      "args": ["/path/to/mcp-claude-history/server.py"]
    }
  }
}
```

## Usage

```
mcp__claude-history__search_history("your query", 3)
mcp__claude-history__search_stats()
```

## Core Algorithm

```python
# 1. Tokenize: Chinese → chars, English → words
tokens = tokenize(query)  # ["softmax", "transformer", "核", "心"]

# 2. Create pairs (implicit Q·K)
pairs = combinations(tokens, 2)  # 28 pairs

# 3. Count co-occurrence (implicit attention)
hits = sum(1 for t1, t2 in pairs if t1 in doc and t2 in doc)

# 4. D-Heap rank decay (discrete Softmax)
score = (hits / max_pairs) * (1 / d ** floor(log_d(rank)))
# d=4: rank 1-3 → weight 1.0, rank 4-15 → 0.25, rank 16-63 → 0.0625
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
Query: "softmax transformer d-heap 核心概念"
Tokens: 8 → Pairs: 28

| Method  | Time  | Matches | Top Hits | Precision |
|---------|-------|---------|----------|-----------|
| Keyword | 940ms | 12184   | 8        | Low       |
| D-Heap  | 391ms | 3758    | 28       | High      |
```

- 2.4x faster
- 70% less noise
- Same top results

## Full Implementation

```python
import json, math
from pathlib import Path
from itertools import combinations

def tokenize(text):
    tokens, en = [], ""
    for c in text:
        if '\u4e00' <= c <= '\u9fff':
            if en.strip(): tokens.append(en.strip().lower()); en = ""
            tokens.append(c)
        elif c.isalpha(): en += c
        else:
            if en.strip(): tokens.append(en.strip().lower()); en = ""
    if en.strip(): tokens.append(en.strip().lower())
    seen = set()
    return [t for t in tokens if t and not (t in seen or seen.add(t))][:10]

def search_dheap(query, messages, d=4, top_n=5):
    tokens = tokenize(query)
    pairs = list(combinations(tokens, 2))
    if not pairs: return []

    scored = []
    for msg in messages:
        text = msg['content'].lower()
        hits = sum(1 for t1, t2 in pairs if t1 in text and t2 in text)
        if hits > 0:
            scored.append({**msg, 'hits': hits})

    scored.sort(key=lambda x: x['hits'], reverse=True)

    results = []
    for rank, item in enumerate(scored[:top_n], 1):
        layer = int(math.log(max(rank,1)) / math.log(d))
        weight = 1.0 / (d ** layer)
        score = (item['hits'] / len(pairs)) * weight
        results.append({**item, 'rank': rank, 'score': score})

    return results
```

## Use Cases

1. **Conversation history search** - Find past discussions
2. **Code archaeology** - Why was this changed? (Git shows what, this shows why)
3. **Agent/skill matching** - Match by content, not description

## License

Public domain. Copy, modify, do whatever you want.
