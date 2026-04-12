# mcp-claude-history

MCP server for searching Claude Code conversation history with field-level control.

## Features

- **Field-driven search**: choose which parts of messages to search
  - `user_text` — user messages
  - `assist_text` — assistant replies
  - `tool_names` — tool calls (Edit, Bash, Read...)
  - `tool_input` — tool arguments (file paths, queries)
  - `tool_result` — tool outputs
- **FTS5 index**: SQLite full-text search, incremental append-only updates
- **Filters**: msg_type, tool_name, model, cwd, project, since
- **Pair bonus**: multi-token co-occurrence scoring
- **Dual interface**: MCP server + CLI in single file

## Install

```bash
pip install -e .
```

Requires: Python 3.10+, orjson, mcp

## Usage

### MCP Server (stdio)

```bash
python server.py
```

Add to Claude Code settings:

```json
{
  "mcpServers": {
    "claude-history": {
      "command": "python",
      "args": ["/path/to/server.py"]
    }
  }
}
```

### CLI

```bash
# Search (default: user_text + assist_text)
python server.py "transformer attention"

# Search specific fields
python server.py "auth" --fields user_text
python server.py "server.py" --fields tool_input
python server.py "Edit" --fields tool_names

# All fields (like old behavior)
python server.py "query" --fields all

# Filters
python server.py "schema" --msg-type user --since 7d --project obus

# Index management
python server.py --update    # incremental update
python server.py --rebuild   # full rebuild

# Stats & context
python server.py --stats
python server.py --context SESSION_FILE LINE_NUM
```

## MCP Tools

| Tool | Description |
|------|-------------|
| `search_history` | Full-text search with field + filter control |
| `update_index` | Incremental index update (new/modified/stale) |
| `rebuild_index` | Drop and rebuild entire index |
| `search_stats` | Corpus statistics |
| `get_context` | Read conversation context around a line |

## Performance

| Operation | Time |
|-----------|------|
| Index check (1200 sessions) | 0.03s |
| Search (FTS5 + re-score) | 0.03-0.14s |
| Append index (modified session) | 0.03s |
| Full rebuild (1200 sessions) | ~45s |

## Logging

Server writes structured JSON logs to `~/.claude/log/claude_history.log`:
- Daily rotation at midnight, 30-day retention
- Logs: startup, index updates, tool calls (entry/exit/error), shutdown
- stderr redirected to same log file

## Architecture

```
JSONL files → extract_fields → SQLite FTS5 (per-field columns)
Search → FTS5 MATCH (column filter) → str.find re-score → pair bonus → time_decay → top-K
Index → status table (mtime + line_count) → append-only for modified files
```
