# Changelog

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
