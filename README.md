# mcp-claude-history

Your Claude Code conversations contain months of problem-solving, design decisions, and debugging sessions. This MCP server makes all of it searchable.

## What you can do

**Find how you solved it before.** Search across all your past conversations — "how did I fix that auth bug?" or "what was the SQLite migration approach?" — and get the exact message with context.

**Search by what happened, not just what was said.** Filter by tool usage (`tool_name=Edit`), message role (`msg_type=user`), content type (`content_type=tool_use`), git branch, or working directory. Find every time Claude edited a specific file, or every Bash command run in a project.

**Navigate your work history.** Every conversation is indexed — user messages, assistant responses, tool calls and their results. Search "dheap" and find not just where you discussed it, but the actual Edit/Bash/Read calls that implemented it.

**Cross-project search.** One query searches across all projects, or narrow down with `project=`, `cwd=`, or `branch=` filters.

**Chinese + English.** Full CJK support via jieba tokenization. Search in Chinese, English, or mixed — "transformer 注意力" just works.

## Install

```bash
pip install orjson mcp jieba
claude mcp add claude-history python3 /path/to/server.py
```

## Tools

### search_history

The main search tool. 9 parameters:

| Parameter | Example | What it does |
|-----------|---------|-------------|
| `query` | `"auth token refresh"` | Full-text search (required) |
| `msg_type` | `"user"` | Only your messages, or only Claude's |
| `content_type` | `"tool_use"` | Only tool calls, or only text, or only tool results |
| `tool_name` | `"Edit"` | Find every Edit/Bash/Read/Write call |
| `project` | `"firebox"` | Narrow to one project |
| `branch` | `"dev"` | Filter by git branch |
| `cwd` | `"/home/user/myapp"` | Filter by working directory |
| `since` | `"7d"` | Last 7 days / 24 hours / 30 minutes |
| `limit` | `5` | Max sessions to return |

### get_context

Jump into a conversation. Pass `file` and `line` from search results to read surrounding messages — see the full discussion around a hit.

### search_stats

Corpus overview: total messages, token usage, tool distribution, project list.

## How it works

Conversations are indexed into SQLite FTS5 on startup. Each search:

1. FTS5 MATCH with BM25 ranking + porter stemming (`running` finds `run`)
2. jieba re-scoring for precise CJK token matching
3. Recency boost — recent conversations rank higher (7-day half-life)
4. Results grouped by session, sorted by aggregated score

Index updates are incremental — only new or modified files are re-indexed.

## Architecture

```
~/.claude/projects/*/*.jsonl
        |
        v
  [Indexer] ── startup: full scan + stale cleanup
        |      per-search: quick scan (mtime > last_index_ts)
        v
  ~/.claude/history-index.db (SQLite WAL)
        |
        |   sessions: file_path, project, mtime, cwd, branch
        |   messages: FTS5 (file_path, line_num, msg_type, content_type, tool_name, ts, text)
        |   meta:     schema_version, last_index_ts
        |
        v
  [Search Pipeline]
        |
        |   1. FTS5 MATCH (BM25 + porter stemming) ── candidate retrieval
        |   2. jieba re-score ── precise CJK token matching
        |   3. recency boost ── exp(-0.693 * age / 7 days)
        |   4. score = 0.7 * BM25 + 0.3 * meet_count * (1 + recency)
        |   5. session aggregation ── sum of message scores per session
        |
        v
  List[TextContent] ── markdown output via low-level MCP SDK
```

- Single file, no FastMCP. Python 3.10+, SQLite FTS5, jieba, orjson.
- Schema migration: bump `DB_SCHEMA_VERSION` to force rebuild.
- Indexed content: user text, assistant text, tool_use (name + input), tool_result (text).

## Performance

| | |
|---|---|
| First index | ~35s (1000+ sessions) |
| Search | ~0.2s |
| Incremental update | 0.03s |

## License

Public domain.
