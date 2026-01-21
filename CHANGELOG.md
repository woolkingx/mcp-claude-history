# Changelog

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
