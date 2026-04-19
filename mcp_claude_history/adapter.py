from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import sqlite3
import time
from pathlib import Path
from typing import Any

import mcp.server.stdio
import mcp.types as types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.models import InitializationOptions

from mcp_claude_history.extractor import extract_normalized_entry
from mcp_claude_history.indexer import update_index
from mcp_claude_history.paths import AppPaths
from mcp_claude_history.parser import parse_json_line
from mcp_claude_history.query import DEFAULT_FIELDS, search_messages
from mcp_claude_history.render import format_search_result
from mcp_claude_history.storage import open_database

VERSION = "0.5.3"

logger = logging.getLogger("mcp-claude-history")
_LOGGING_READY = False


# ── Pure helpers ─────────────────────────────────────────────────

def _runtime_paths() -> AppPaths:
    return AppPaths(root=Path.home() / ".claude")


def _configure_logging(paths: AppPaths) -> None:
    global _LOGGING_READY

    if _LOGGING_READY:
        return

    paths.log_dir.mkdir(parents=True, exist_ok=True)
    log_path = paths.log_dir / "claude_history.log"
    handler = logging.handlers.TimedRotatingFileHandler(
        str(log_path),
        when="midnight",
        backupCount=30,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter(
            '{"time":"%(asctime)s","level":"%(levelname)s","msg":"%(message)s","file":"%(filename)s:%(lineno)d"}'
        )
    )
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    _LOGGING_READY = True


def _tc(text: str) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=text)]


# ── DAG nodes: (conn, paths, ...) → str ─────────────────────────

def do_search(
    conn: sqlite3.Connection,
    paths: AppPaths,
    *,
    query: str,
    limit: int = 10,
    fields: str | None = None,
    since: str | None = None,
    project: str | None = None,
    msg_type: str | None = None,
    tool_name: str | None = None,
    model: str | None = None,
    cwd: str | None = None,
) -> str:
    update_index(conn, paths)
    result = search_messages(
        conn,
        query=query,
        limit=limit,
        fields=fields,
        since=since,
        project=project,
        msg_type=msg_type,
        tool_name=tool_name,
        model=model,
        cwd=cwd,
    )
    return format_search_result(result)


def do_update(conn: sqlite3.Connection, paths: AppPaths) -> str:
    t0 = time.time()
    summary = update_index(conn, paths)
    dt = time.time() - t0
    return (
        f"index updated in {dt:.2f}s | "
        f"new:{summary.new} modified:{summary.modified} stale:{summary.stale} "
        f"unchanged:{summary.unchanged} indexed:{summary.indexed} appended:{summary.appended}"
    )


def do_rebuild(paths: AppPaths) -> str:
    """Rebuild requires its own connection lifecycle (deletes DB file)."""
    db_path = paths.db_path
    if db_path.exists():
        db_path.unlink()
    conn = open_database(db_path)
    t0 = time.time()
    try:
        summary = update_index(conn, paths)
    finally:
        conn.close()
    return f"rebuilt: {summary.indexed} sessions in {time.time() - t0:.1f}s"


def do_stats(conn: sqlite3.Connection, paths: AppPaths) -> str:
    update_index(conn, paths)

    out = "## Corpus Statistics\n\n"
    out += f"- Sessions: {conn.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]}\n"
    out += f"- Indexed messages: {conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0]}\n\n"

    out += "### Message types\n"
    for message_type, count in conn.execute(
        "SELECT msg_type, COUNT(*) FROM messages GROUP BY msg_type ORDER BY COUNT(*) DESC"
    ):
        out += f"- {message_type}: {count:,}\n"

    out += "\n### Content types\n"
    for content_type, count in conn.execute(
        "SELECT content_type, COUNT(*) FROM messages GROUP BY content_type ORDER BY COUNT(*) DESC"
    ):
        out += f"- {content_type or '(empty)'}: {count:,}\n"

    out += "\n### Top tools\n"
    for tool_name, count in conn.execute(
        """
        SELECT tool_names, COUNT(*)
        FROM messages
        WHERE tool_names != ''
        GROUP BY tool_names
        ORDER BY COUNT(*) DESC
        LIMIT 20
        """
    ):
        out += f"- {tool_name}: {count:,}\n"

    out += f"\n### Projects ({conn.execute('SELECT COUNT(DISTINCT project) FROM sessions').fetchone()[0]})\n"
    for (project,) in conn.execute("SELECT DISTINCT project FROM sessions ORDER BY project"):
        out += f"- {project}\n"
    return out


def do_context(
    paths: AppPaths,
    *,
    file: str,
    line: int,
    context_lines: int = 5,
    project: str | None = None,
) -> str:
    if project:
        target = paths.projects_dir / project / file
        if not target.exists():
            return f"File not found: {project}/{file}"
    else:
        target = None
        for session_file in paths.projects_dir.glob("*/*.jsonl"):
            if session_file.name == file:
                target = session_file
                break
        if not target:
            return f"File not found: {file}"

    start = max(1, line - context_lines)
    end = line + context_lines
    out = f"## Context: {file} L{start}-{end}\n\n"

    try:
        with target.open("rb") as handle:
            for line_num, raw in enumerate(handle, 1):
                if line_num > end:
                    break
                if line_num < start:
                    continue

                parsed = parse_json_line(raw)
                if parsed is None:
                    continue

                entry = extract_normalized_entry(
                    parsed,
                    file_path=str(target),
                    line_num=line_num,
                    project=target.parent.name,
                )
                content = ""
                tool = ""
                msg_type = parsed.get("type", "?")
                if entry is not None:
                    content = entry.searchable.user_text or entry.searchable.assist_text
                    tool = entry.searchable.tool_names
                    msg_type = entry.msg_type
                else:
                    message = parsed.get("message", {})
                    if isinstance(message, dict):
                        content = str(message.get("content", ""))

                marker = ">>> " if line_num == line else "    "
                label = f"[{msg_type}]"
                if tool:
                    label += f" tool:{tool}"
                snippet = content[:1000].replace("\n", "\n" + marker)
                out += f"L{line_num} {marker}{label} {snippet}\n\n"
    except Exception as exc:  # pragma: no cover - defensive I/O path
        logger.exception("get_context error: %s", target)
        return f"Failed to read: {exc}"

    return out


# ── Dispatch node: (conn, paths, name, args) → TextContent[] ───

def _dispatch(
    conn: sqlite3.Connection,
    paths: AppPaths,
    name: str,
    args: dict[str, Any],
) -> list[types.TextContent]:
    logger.info("call_tool: %s args=%s", name, args)
    try:
        if name == "search_history":
            text = do_search(
                conn,
                paths,
                query=str(args.get("query", "")),
                limit=int(args.get("limit", 10)),
                fields=args.get("fields"),
                since=args.get("since"),
                project=args.get("project"),
                msg_type=args.get("msg_type"),
                tool_name=args.get("tool_name"),
                model=args.get("model"),
                cwd=args.get("cwd"),
            )
        elif name == "update_index":
            text = do_update(conn, paths)
        elif name == "rebuild_index":
            text = do_rebuild(paths)
        elif name == "search_stats":
            text = do_stats(conn, paths)
        elif name == "get_context":
            text = do_context(
                paths,
                file=str(args.get("file", "")),
                line=int(args.get("line", 1)),
                context_lines=int(args.get("context_lines", 5)),
                project=args.get("project"),
            )
        else:
            logger.error("unknown tool: %s", name)
            text = f"Unknown tool: {name}"
        logger.info("call_tool done: %s", name)
        return _tc(text)
    except Exception:
        logger.exception("call_tool error: %s", name)
        raise


# ── MCP server factory ──────────────────────────────────────────

def create_server(conn: sqlite3.Connection, paths: AppPaths) -> Server:
    server = Server("claude-history")

    def _property(typ: str, desc: str, **kwargs: Any) -> dict[str, Any]:
        schema_type: str | list[str]
        if typ == "string_required":
            schema_type = "string"
        else:
            schema_type = [typ, "null"]
        schema = {"type": schema_type, "description": desc}
        schema.update(kwargs)
        return schema

    @server.list_tools()
    async def handle_list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name="search_history",
                description=(
                    "Search Claude Code conversation history with field-level control. "
                    f"fields: {', '.join(sorted(DEFAULT_FIELDS))}, tool_names, tool_input, tool_result, all. "
                    f"Default: {', '.join(sorted(DEFAULT_FIELDS))}."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": _property("string_required", "Full-text query (Chinese/English/mixed)"),
                        "limit": _property("integer", "Max sessions (default 10)", default=10),
                        "fields": _property("string", "Comma-separated fields to search", default="user_text,assist_text"),
                        "since": _property("string", "Time window: '7d','24h','30m'"),
                        "project": _property("string", "Filter by project name"),
                        "msg_type": _property("string", "Filter: 'user' or 'assistant'"),
                        "tool_name": _property("string", "Filter by tool name"),
                        "model": _property("string", "Filter by model name"),
                        "cwd": _property("string", "Filter by working directory (substring)"),
                    },
                    "required": ["query"],
                },
            ),
            types.Tool(
                name="update_index",
                description="Incremental index update: scan for new/modified/stale sessions, append new lines.",
                inputSchema={"type": "object", "properties": {}},
            ),
            types.Tool(
                name="rebuild_index",
                description="Drop and rebuild the entire FTS5 index from scratch.",
                inputSchema={"type": "object", "properties": {}},
            ),
            types.Tool(
                name="search_stats",
                description="Corpus statistics: session/message counts, tool usage, project list.",
                inputSchema={"type": "object", "properties": {}},
            ),
            types.Tool(
                name="get_context",
                description="Get conversation context around a line. Use file/line/project from search_history.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "file": _property("string_required", "Session filename"),
                        "line": {"type": "integer", "description": "Line number (1-indexed)"},
                        "context_lines": {
                            "type": "integer",
                            "default": 5,
                            "description": "Messages before/after",
                        },
                        "project": _property("string", "Project dir name (skips scan)"),
                    },
                    "required": ["file", "line"],
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, args: dict[str, Any]) -> list[types.TextContent]:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: _dispatch(conn, paths, name, args))

    return server


# ── Boundary: MCP server (owns connection lifecycle) ────────────

async def run_mcp() -> None:
    paths = _runtime_paths()
    _configure_logging(paths)
    logger.info("mcp-claude-history v%s starting", VERSION)

    conn = open_database(paths.db_path)
    try:
        update_index(conn, paths)
        logger.info("index ready, serving")

        server = create_server(conn, paths)
        async with mcp.server.stdio.stdio_server() as (reader, writer):
            logger.info("stdio transport connected")
            await server.run(
                reader,
                writer,
                InitializationOptions(
                    server_name="claude-history",
                    server_version=VERSION,
                    capabilities=server.get_capabilities(
                        notification_options=NotificationOptions(),
                        experimental_capabilities={},
                    ),
                ),
            )
    finally:
        conn.close()
    logger.info("server shutdown")


# ── Boundary: CLI (owns connection lifecycle) ───────────────────

def cli() -> None:
    root = argparse.ArgumentParser(
        prog="mcp-claude-history",
        description=f"mcp-claude-history v{VERSION}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    root.add_argument("-d", "--debug", action="store_true", help="verbose logging")
    sub = root.add_subparsers(dest="cmd")

    # search <query> [-f fields] [-n limit] [-s since] [-p project] [-t type] [-T tool] [-m model] [-c cwd]
    p_search = sub.add_parser("search", aliases=["s"], help="full-text search")
    p_search.add_argument("query", help="search query")
    p_search.add_argument("-f", "--fields", default="user_text,assist_text",
                          help="user_text,assist_text,tool_names,tool_input,tool_result,all (default: user_text,assist_text)")
    p_search.add_argument("-n", "--limit", type=int, default=10, help="max sessions (default: 10)")
    p_search.add_argument("-s", "--since", help="time window: 7d, 24h, 30m")
    p_search.add_argument("-p", "--project", help="filter by project")
    p_search.add_argument("-t", "--msg-type", help="user or assistant")
    p_search.add_argument("-T", "--tool-name", help="filter by tool name")
    p_search.add_argument("-m", "--model", help="filter by model")
    p_search.add_argument("-c", "--cwd", help="filter by working directory")

    # context <file> <line> [-n context_lines] [-p project]
    p_ctx = sub.add_parser("context", aliases=["ctx"], help="show conversation context around a line")
    p_ctx.add_argument("file", help="session filename")
    p_ctx.add_argument("line", type=int, help="line number (1-indexed)")
    p_ctx.add_argument("-n", "--lines", type=int, default=5, help="context lines before/after (default: 5)")
    p_ctx.add_argument("-p", "--project", help="project dir name (skips scan)")

    # stats / update / rebuild — no extra args
    sub.add_parser("stats", help="corpus statistics")
    sub.add_parser("update", aliases=["up"], help="incremental index update")
    sub.add_parser("rebuild", help="drop and rebuild entire index")

    args = root.parse_args()

    paths = _runtime_paths()
    _configure_logging(paths)
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
        logger.setLevel(logging.DEBUG)

    if args.cmd in ("rebuild",):
        print(do_rebuild(paths))
        return

    if args.cmd in ("context", "ctx"):
        print(do_context(paths, file=args.file, line=args.line, context_lines=args.lines, project=args.project))
        return

    if args.cmd is None:
        root.print_help()
        return

    conn = open_database(paths.db_path)
    try:
        if args.cmd in ("update", "up"):
            print(do_update(conn, paths))
        elif args.cmd == "stats":
            print(do_stats(conn, paths))
        elif args.cmd in ("search", "s"):
            t0 = time.time()
            print(
                do_search(
                    conn,
                    paths,
                    query=args.query,
                    limit=args.limit,
                    fields=args.fields,
                    since=args.since,
                    project=args.project,
                    msg_type=args.msg_type,
                    tool_name=args.tool_name,
                    model=args.model,
                    cwd=args.cwd,
                )
            )
            print(f"--- {time.time() - t0:.2f}s | fields: {args.fields} ---")
    finally:
        conn.close()


__all__ = ["cli", "create_server", "run_mcp"]
