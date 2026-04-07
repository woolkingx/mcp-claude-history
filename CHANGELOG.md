# Changelog

## v4.0.0 (2026-04-07)

### Architecture (Breaking)
- Removed FastMCP — uses low-level MCP SDK (`Server`, `@server.call_tool()`, `types.TextContent`)
- SQLite FTS5 replaces D-Heap full JSONL scan — prebuilt index at `~/.claude/history-index.db`
- Porter stemming via FTS5 tokenizer (`porter unicode61 remove_diacritics 2`) — `running` matches `run`
- Async server with `run_in_executor` for non-blocking tool calls
- Schema migration system: `DB_SCHEMA_VERSION` in `meta` table, bump to force rebuild

### Indexing
- Startup full index + per-search quick index (only files modified since `last_index_ts`)
- Stale cleanup: deleted JSONL files automatically removed from index on startup
- `last_index_ts` persisted in `meta` table across restarts
- tool_use and tool_result content now indexed (197k messages vs 75k text-only)
- New indexed fields: `content_type`, `tool_name`, `ts`, `branch`

### Search
- 9 search parameters: `query`, `limit`, `since`, `project`, `msg_type`, `content_type`, `tool_name`, `branch`, `cwd`
- Combined scoring: 70% BM25 (FTS5 rank) + 30% jieba meet_count x recency boost
- Recency half-life: 7 days (exponential decay)
- Session-level aggregation: sessions ranked by sum of message scores
- Active filters displayed in output header
- Output shows `msg_type`, `content_type`, `tool_name` per message hit

### Performance
- Full index (first run): ~35s for 1135 sessions
- Incremental (no changes): 0.03s
- Search: ~0.2s

## v3.3.0 (2026-04-05)

### Algorithm
- Two-layer scoring: single-token TF x IDF (x0.3) + pair co-occurrence bonus (x1.0)
- Single-word queries now return results

### Bug Fix
- Queries with 1 token no longer return empty results

## v3.2.0 (2026-03-30)

### Algorithm
- jieba tokenization for Chinese (word-level)
- TF-log x IDF scoring replaces binary presence counting

### Dependencies
- Added: `jieba`

## v3.1.0 (2026-03-09)

### Algorithm
- Scoring unit changed from line to session
- Cross-message relevance captured

## v3.0.0 (2026-03-09)

### Algorithm (Breaking)
- True d=4 D-ary heap replaces `heapq`
- Fixed-size heap: only top-N kept during scan
- `dheap_weight` rank decay

## v2.0.0 (2025-01-21)

### Performance
- `orjson` replaces `json` (2.6x faster)
- Heap insert on match instead of load-all-then-sort

## v1.1.0 (2024-12-23)

- Add `get_context` tool

## v1.0.0 (2024-12-07)

- Initial release
