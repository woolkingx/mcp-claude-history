# Changelog

## 0.5.2 (2026-04-12)

- Add file logging: `~/.claude/log/claude_history.log` with daily rotation (30-day retention)
- Redirect stderr to log file
- Log all key paths: startup, index_update, call_tool (entry/exit/error), search errors, shutdown
- Fix: logger was declared but never used — now active on all MCP server operations

## 0.5.1 (2026-04-10)

- fn-style rewrite: extract_fields split, index/search ops as pure functions
- MCP tools: +update_index, +rebuild_index
- CLI: --update, --rebuild, --stats, --context
- Multi-dim sort: hits dominant, recency as bonus
- Pair bonus: multi-token co-occurrence scoring C(mc,2)
- 923 → 761 lines

## 0.5.0 (2026-04-10)

- Field-driven FTS5 search: 5 searchable fields per message
- `fields` param: user_text, assist_text, tool_names, tool_input, tool_result
- Default searches user_text + assist_text (reduces noise vs all-field search)
- Append-only incremental index with status table (mtime + line_count)
- Removed jieba dependency — FTS5 unicode61 tokenizer
- Filters: msg_type, tool_name, model, cwd, project, since
- Search 0.03s vs 16s (full scan) vs 188s (v0.3 jieba per-line)

## 0.4.0 (2026-04-08)

- SQLite FTS5 + porter stemming + jieba re-score
- Field-level filtering: msg_type, content_type, tool_name, branch, cwd
- BM25 ranking with recency boost
- Low-level MCP SDK (replaced FastMCP)

## 0.3.0 (2026-04-05)

- D-ary heap (d=4) top-K scoring
- Two-layer: single-token TF×IDF + pair co-occurrence bonus
- jieba tokenization for Chinese
- Bytes pre-filter before JSON parse
- FastMCP framework
