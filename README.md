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
- **Dual interface**: MCP server + CLI through a thin compatibility wrapper

## Package Layout

Runtime orchestration now lives in `mcp_claude_history/adapter.py`. The core search stack is split into focused modules:

```text
mcp_claude_history/
  adapter.py   # CLI parsing, MCP wiring, logging/bootstrap
  parser.py    # JSONL line parsing
  extractor.py # normalize Claude history entries
  storage.py   # SQLite schema and connection setup
  indexer.py   # incremental indexing
  query.py     # candidate retrieval and scoring
  ranker.py    # scoring helpers
  render.py    # search-result formatting
  paths.py     # filesystem locations
  schema.py    # shared records
server.py      # compatibility shim that dispatches to adapter.cli/run_mcp
```

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

The compatibility wrapper still handles the CLI, and the `mcp-claude-history` console script remains wired to `server:cli`.

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

If you are importing the runtime from Python code, use `mcp_claude_history.adapter`:

```python
from mcp_claude_history.adapter import cli, run_mcp
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
JSONL files → parse_json_line → extract_normalized_entry → SQLite FTS5
Search → candidate_retrieve → analyze_candidate / score_from_analysis → format_search_result
Index → sessions snapshot + append-only update_index
```
