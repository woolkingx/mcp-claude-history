# Changelog

## v3.3.0 (2026-04-05)

### Algorithm
- Two-layer scoring replaces pair-only scoring: single-token TF×IDF (×0.3) + pair co-occurrence bonus (×1.0)
- Single-word queries now return results (previously returned empty due to 0 pairs)
- `score_content()` replaces `count_pair_hits()` — unified scoring function for both layers
- Snippet extraction falls back to single-token position when no pair match found

### Bug Fix
- **Critical**: queries with 1 token (e.g. "hooks", "搜尋") no longer return empty results
- Guard changed from `max_pairs == 0` to `not tokens` — only empty tokenization aborts

## v3.2.0 (2026-03-30)

### Algorithm
- jieba tokenization for Chinese (word-level instead of char-level)
- TF-log × IDF scoring replaces binary presence counting
- Single-pass scan: collect contents + doc_count simultaneously, then re-score with precise IDF

### Dependencies
- Added: `jieba`

## v3.1.0 (2026-03-09)

### Algorithm
- Scoring unit changed from **line** to **session**: hits accumulated across all messages in a session
- `line` in result now points to the highest-hit message within the session — natural `get_context` entry point
- Cross-message relevance captured: user asks X, assistant answers X → both contribute to session score
- `score` field now reflects session-level density (session_hits / max_pairs); values > 1.0 indicate repeated co-occurrence across the session
- bytes pre-filter (`b'"user"' not in raw`) applied before `orjson.loads` — skips summary/tool/system lines without JSON parse

### Workflow
- Natural search flow enforced: **session → line → content** (was: line → session)
- `search_history` returns best session, `get_context` navigates into it

## v3.0.0 (2026-03-09)

### Algorithm (Breaking)
- Replaced `heapq` (binary heap) with true d=4 D-ary heap (`_dh_push`, `_dh_replace_min`, `_dh_sift_up`, `_dh_sift_down`) — no external dependency
- Fixed-size heap of `limit` entries: only top-N candidates kept during scan, not all hits
- `dheap_weight(heap_size)` rank decay now active: admission threshold rises as heap fills, so later entries must have higher pair hits to displace current worst
- Removed post-scan project filter + heapify; project filter applied at file-loop level

### Features
- Search result now includes `score` field (normalized: hits / max_possible_pairs)
- Improved exception handling: `orjson.JSONDecodeError` caught explicitly, bare `except:` removed

### Performance
- Push-heavy workload benefits from d=4: `log_4(n)` sift-up layers vs `log_2(n)` in binary heap
- Heap size bounded to `limit` throughout scan — memory O(limit) instead of O(hits)

### Docs
- All three tool docstrings updated with parameter formats, examples, and return field descriptions

## v2.0.0 (2025-01-21)

### Performance
- Replace `json` with `orjson` (2.6x faster)
- Insert into heap on match instead of load-all-then-sort

### Features
- Sort by hits desc, then mtime desc (newer conversations first)
- Return snippet (hit point ±100 chars) instead of content[:500]
- Simplified result format: `file:line:hits | snippet`

### Dependencies
- Added: `orjson`

## v1.1.0 (2024-12-23)

- Add `get_context` tool for context navigation

## v1.0.0 (2024-12-07)

- Initial release
- D-Heap search with co-occurrence ranking
- `search_history` and `search_stats` tools
