#!/usr/bin/env python3
"""
MCP Claude History v0.5.2 — Field-driven conversation search.

Architecture:
  JSONL -> extract_fields -> SQLite FTS5 (per-field columns)
  search -> FTS5 MATCH (column-filtered) -> str.find re-score -> time_decay -> top-K
  Index: status check (mtime + line_count) -> append-only for modified files

Entry points:
  MCP server : python server.py          (stdio, auto update_index on startup)
  CLI        : python server.py "query"  (search / --stats / --rebuild / --context)
"""

import asyncio
import logging
import logging.handlers
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import mcp.server.stdio
import mcp.types as types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.models import InitializationOptions
import orjson

logger = logging.getLogger("mcp-claude-history")
LOG_DIR = Path.home() / ".claude" / "log"
LOG_DIR.mkdir(parents=True, exist_ok=True)

_file_handler = logging.handlers.TimedRotatingFileHandler(
    str(LOG_DIR / "claude_history.log"),
    when="midnight", backupCount=30, encoding="utf-8",
)
_file_handler.setFormatter(logging.Formatter(
    '{"time":"%(asctime)s","level":"%(levelname)s","msg":"%(message)s","file":"%(filename)s:%(lineno)d"}'
))
logger.addHandler(_file_handler)
logger.setLevel(logging.DEBUG)

# redirect stderr to log file
sys.stderr = open(str(LOG_DIR / "claude_history.log"), "a", encoding="utf-8")

# ══════════════════════════════════════════════════════════════════════════════
# Config
# ══════════════════════════════════════════════════════════════════════════════

PROJECTS_DIR = Path.home() / ".claude/projects"
DB_PATH = Path.home() / ".claude/history-field-index.db"
DB_SCHEMA_VERSION = 2
VERSION = "0.5.2"

ALL_FIELDS = frozenset({"user_text", "assist_text", "tool_names", "tool_input", "tool_result"})
DEFAULT_FIELDS = frozenset({"user_text", "assist_text"})
_SYSREM_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)

# ══════════════════════════════════════════════════════════════════════════════
# Field extraction — JSONL entry -> typed field dict
# ══════════════════════════════════════════════════════════════════════════════


def strip_system_reminder(text: str) -> str:
    return _SYSREM_RE.sub("", text).strip()


def extract_user_fields(message: dict, entry: dict) -> Dict:
    """Extract fields from a user entry."""
    raw = message.get("content", "")
    fields = {"user_text": "", "tool_result": ""}
    content_types = set()

    if isinstance(raw, str):
        fields["user_text"] = strip_system_reminder(raw)
        content_types.add("text")
    elif isinstance(raw, list):
        texts, results = [], []
        for block in raw:
            if not isinstance(block, dict):
                continue
            bt = block.get("type", "")
            content_types.add(bt)
            if bt == "text":
                texts.append(block.get("text", ""))
            elif bt == "tool_result":
                rc = block.get("content", "")
                if isinstance(rc, str):
                    results.append(rc[:500])
                elif isinstance(rc, list):
                    for sub in rc:
                        if isinstance(sub, dict) and sub.get("type") == "text":
                            results.append(sub.get("text", "")[:500])
        fields["user_text"] = strip_system_reminder(" ".join(texts))
        fields["tool_result"] = " ".join(results)

    return {**fields, "content_types": content_types}


def extract_assistant_fields(message: dict) -> Optional[Dict]:
    """Extract fields from an assistant entry."""
    raw = message.get("content", [])
    if not isinstance(raw, list):
        return None

    texts, names, inputs = [], [], []
    content_types = set()

    for block in raw:
        if not isinstance(block, dict):
            continue
        bt = block.get("type", "")
        content_types.add(bt)
        if bt == "text":
            texts.append(block.get("text", ""))
        elif bt == "tool_use":
            name = block.get("name", "")
            if name:
                names.append(name)
            inp = block.get("input", {})
            if inp:
                inputs.append(f"{name}: {orjson.dumps(inp).decode()[:500]}")

    return {
        "assist_text": strip_system_reminder(" ".join(texts)),
        "tool_names": ", ".join(names),
        "tool_input": " | ".join(inputs),
        "content_types": content_types,
    }


def extract_fields(entry: dict) -> Optional[Dict]:
    """Extract all searchable fields from a JSONL entry. Returns None if not indexable."""
    msg_type = entry.get("type")
    if msg_type not in ("user", "assistant"):
        return None

    message = entry.get("message")
    if not isinstance(message, dict):
        return None

    base = {
        "user_text": "", "assist_text": "", "tool_names": "",
        "tool_input": "", "tool_result": "",
        "msg_type": msg_type,
        "content_type": "",
        "cwd": entry.get("cwd", ""),
        "model": message.get("model", ""),
        "timestamp": entry.get("timestamp", ""),
    }

    if msg_type == "user":
        extracted = extract_user_fields(message, entry)
        base["user_text"] = extracted["user_text"]
        base["tool_result"] = extracted["tool_result"]
        ctypes = extracted["content_types"]
    else:
        extracted = extract_assistant_fields(message)
        if not extracted:
            return None
        base["assist_text"] = extracted["assist_text"]
        base["tool_names"] = extracted["tool_names"]
        base["tool_input"] = extracted["tool_input"]
        ctypes = extracted["content_types"]

    base["content_type"] = "mixed" if len(ctypes) > 1 else (ctypes.pop() if ctypes else "")

    if not any(base[f] for f in ALL_FIELDS):
        return None
    return base


# ══════════════════════════════════════════════════════════════════════════════
# Index — SQLite FTS5, status check, append-only update
# ══════════════════════════════════════════════════════════════════════════════


def db_open() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()

    if (int(row[0]) if row else 0) < DB_SCHEMA_VERSION:
        conn.executescript("DROP TABLE IF EXISTS messages; DROP TABLE IF EXISTS sessions;")
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(DB_SCHEMA_VERSION),),
        )
        conn.commit()

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            file_path TEXT PRIMARY KEY, project TEXT, mtime REAL,
            line_count INTEGER DEFAULT 0
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS messages USING fts5(
            file_path UNINDEXED, line_num UNINDEXED,
            msg_type UNINDEXED, content_type UNINDEXED,
            cwd UNINDEXED, model UNINDEXED, ts UNINDEXED,
            user_text, assist_text, tool_names, tool_input, tool_result,
            tokenize='unicode61 remove_diacritics 2'
        );
    """)
    return conn


def index_check_status(conn: sqlite3.Connection) -> Dict:
    """Compare filesystem vs DB. Returns {new, modified, stale, unchanged}."""
    existing = {}
    for row in conn.execute("SELECT file_path, mtime, line_count FROM sessions"):
        existing[row[0]] = (row[1], row[2])

    disk = {}
    for f in PROJECTS_DIR.glob("*/*.jsonl"):
        try:
            disk[str(f)] = f.stat().st_mtime
        except OSError:
            continue

    new, modified, unchanged = [], [], []
    for fp, mt in disk.items():
        if fp not in existing:
            new.append((fp, mt))
        elif existing[fp][0] != mt:
            modified.append((fp, mt, existing[fp][1]))
        else:
            unchanged.append(fp)

    stale = [fp for fp in existing if fp not in disk]
    return {"new": new, "modified": modified, "stale": stale, "unchanged": unchanged}


def index_file(conn: sqlite3.Connection, fpath: str, mtime: float,
               skip_lines: int = 0) -> int:
    """Index one JSONL file. skip_lines > 0 = append mode. Returns total line count."""
    project_dir = Path(fpath).parent.name
    rows = []
    total = 0
    try:
        with open(fpath, "rb") as f:
            for ln, raw in enumerate(f, 1):
                total = ln
                if ln <= skip_lines:
                    continue
                try:
                    entry = orjson.loads(raw)
                except orjson.JSONDecodeError:
                    continue
                fld = extract_fields(entry)
                if not fld:
                    continue
                rows.append((
                    fpath, str(ln), fld["msg_type"], fld["content_type"],
                    fld["cwd"], fld["model"], fld["timestamp"],
                    fld["user_text"], fld["assist_text"],
                    fld["tool_names"], fld["tool_input"], fld["tool_result"],
                ))
    except Exception:
        logger.exception("index_file error: %s", fpath)
        return 0

    if rows:
        conn.executemany(
            "INSERT INTO messages (file_path, line_num, msg_type, content_type, "
            "cwd, model, ts, user_text, assist_text, tool_names, tool_input, tool_result) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows,
        )
    conn.execute(
        "INSERT OR REPLACE INTO sessions (file_path, project, mtime, line_count) "
        "VALUES (?, ?, ?, ?)", (fpath, project_dir, mtime, total),
    )
    return total


def index_update(conn: sqlite3.Connection) -> Dict:
    """Incremental: check status, process only changed files."""
    status = index_check_status(conn)

    for fp in status["stale"]:
        conn.execute("DELETE FROM sessions WHERE file_path = ?", (fp,))
        conn.execute("DELETE FROM messages WHERE file_path = ?", (fp,))

    indexed = sum(1 for fp, mt in status["new"] if index_file(conn, fp, mt) > 0)
    appended = sum(1 for fp, mt, lc in status["modified"] if index_file(conn, fp, mt, lc) > 0)

    if indexed:
        conn.execute("INSERT INTO messages(messages) VALUES('optimize')")
    conn.commit()

    result = {
        "new": len(status["new"]), "modified": len(status["modified"]),
        "stale": len(status["stale"]), "unchanged": len(status["unchanged"]),
        "indexed": indexed, "appended": appended,
    }
    logger.info("index_update: %s", result)
    return result


def index_rebuild() -> str:
    """Drop and rebuild entire index."""
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = db_open()
    t0 = time.time()
    st = index_update(conn)
    conn.close()
    return f"rebuilt: {st['indexed']} sessions in {time.time()-t0:.1f}s"


# ══════════════════════════════════════════════════════════════════════════════
# Search — tokenize, FTS5 query, re-score, format
# ══════════════════════════════════════════════════════════════════════════════


def tokenize(text: str) -> List[str]:
    tokens = text.lower().split()
    seen = set()
    return [t for t in tokens if t and not (t in seen or seen.add(t))][:15]


def parse_fields(fields_str: Optional[str]) -> Set[str]:
    if not fields_str or fields_str.strip().lower() == "all":
        return set(ALL_FIELDS)
    valid = set(f.strip() for f in fields_str.split(",") if f.strip()) & ALL_FIELDS
    return valid if valid else set(DEFAULT_FIELDS)


def build_fts_query(tokens: List[str], search_fields: Set[str]) -> str:
    parts = []
    for t in tokens:
        parts.append(f'"{t}"' if re.search(r'[^a-zA-Z0-9\u4e00-\u9fff]', t) else t)
    terms = " OR ".join(parts)
    if search_fields == set(ALL_FIELDS):
        return terms
    return f"{{{' '.join(sorted(search_fields))}}} : {terms}"


def snippet(text: str, token_set: Set[str], ctx: int = 30) -> str:
    tl = text.lower()
    parts = []
    for tok in token_set:
        idx = tl.find(tok)
        if idx == -1:
            continue
        s, e = max(0, idx - ctx), min(len(text), idx + len(tok) + ctx)
        before = text[s:idx].replace("\n", " ")
        matched = text[idx:idx + len(tok)]
        after = text[idx + len(tok):e].replace("\n", " ")
        frag = f"{before}**{matched}**{after}"
        if s > 0:
            frag = "..." + frag
        if e < len(text):
            frag += "..."
        parts.append(frag)
    return " | ".join(parts) if parts else text[:80]


def parse_since(since: Optional[str]) -> Optional[float]:
    if not since:
        return None
    s = since.strip().lower()
    units = {"d": 86400, "h": 3600, "m": 60}
    if s[-1] in units:
        try:
            return (datetime.now(timezone.utc) - timedelta(seconds=float(s[:-1]) * units[s[-1]])).timestamp()
        except ValueError:
            pass
    return None


def rescore_row(row, token_set: Set[str], search_fields: Set[str],
                now: float, session_cache: Dict, conn) -> Optional[Dict]:
    """Re-score a single FTS5 result row. Returns result dict or None."""
    (file_path, line_num, msg_type, content_type, tn, mdl,
     user_text, assist_text, tool_names_val, tool_input, tool_result, fts_rank) = row

    if file_path not in session_cache:
        meta = conn.execute(
            "SELECT project, mtime FROM sessions WHERE file_path = ?", (file_path,)
        ).fetchone()
        session_cache[file_path] = meta if meta else (None, None)

    proj, mtime = session_cache[file_path]
    if not proj:
        return None

    field_map = {
        "user_text": user_text or "", "assist_text": assist_text or "",
        "tool_names": tool_names_val or "", "tool_input": tool_input or "",
        "tool_result": tool_result or "",
    }

    total_mc = 0
    pair_bonus = 0.0
    matched = []
    n_tokens = len(token_set)

    for fname in search_fields:
        text = field_map[fname]
        if not text:
            continue
        tl = text.lower()
        mc = sum(1 for t in token_set if t in tl)
        if mc > 0:
            total_mc += mc
            matched.append({"field": fname, "hits": mc, "snippet": snippet(text, token_set)})
            # pair bonus: reward co-occurrence of multiple tokens in same field
            if n_tokens > 1 and mc > 1:
                pair_bonus += mc * (mc - 1) / 2  # C(mc,2)

    if total_mc == 0:
        return None

    age_days = max((now - mtime) / 86400, 0.01)
    recency = 1.0 / (1.0 + age_days)
    # hits + pair bonus, recency as multiplier bonus
    score = (total_mc + pair_bonus) * (1.0 + recency)
    return {
        "file": Path(file_path).name, "project": proj, "line": int(line_num),
        "hits": total_mc, "score": score,
        "msg_type": msg_type, "content_type": content_type,
        "tool_name": tn or "", "matched_fields": matched,
    }


def group_by_session(results: List[Dict], limit: int) -> List[Dict]:
    """Group results by (project, file), sort by session_score."""
    groups: Dict[tuple, dict] = {}
    order = []
    for item in results:
        key = (item["project"], item["file"])
        if key not in groups:
            groups[key] = {"project": item["project"], "file": item["file"],
                           "messages": [], "session_score": 0.0, "total_hits": 0}
            order.append(key)
        groups[key]["messages"].append(item)
        groups[key]["session_score"] += item["score"]
        groups[key]["total_hits"] += item["hits"]
    # sort: session_score desc, total_hits desc, message count desc
    order.sort(key=lambda k: (
        -groups[k]["session_score"],
        -groups[k]["total_hits"],
        -len(groups[k]["messages"]),
    ))
    # sort messages within each session: hits desc, score desc
    for k in order:
        groups[k]["messages"].sort(key=lambda m: (-m["hits"], -m["score"]))
    return [groups[k] for k in order[:limit]]


def format_sessions(sessions: List[Dict], total: int,
                    search_fields: Set[str], filters: Dict) -> str:
    """Format grouped sessions into markdown output."""
    out = f"{total} matched, {len(sessions)} sessions"
    sf = ",".join(sorted(search_fields))
    if sf != ",".join(sorted(DEFAULT_FIELDS)):
        out += f" | fields: {sf}"
    tags = [f"{k}={v}" for k, v in filters.items() if v]
    if tags:
        out += f" | {', '.join(tags)}"
    out += "\n"

    for i, s in enumerate(sessions, 1):
        out += f"\n## Session {i} — {s['file']}\n"
        out += f"project: {s['project']} | score: {s['session_score']:.1f} | hits: {s['total_hits']}\n\n"
        for m in s["messages"][:5]:
            for mf in m["matched_fields"]:
                meta = f"L{m['line']} | {mf['field']} | {m['msg_type']}"
                if m["tool_name"]:
                    meta += f" tool:{m['tool_name']}"
                meta += f" | hits:{mf['hits']} score:{m['score']:.3f}"
                out += f"{meta}\n  {mf['snippet']}\n"
        rest = len(s["messages"]) - 5
        if rest > 0:
            out += f"\n+{rest} more\n"
    return out


# ══════════════════════════════════════════════════════════════════════════════
# MCP tool implementations
# ══════════════════════════════════════════════════════════════════════════════


def _tc(text: str) -> List[types.TextContent]:
    return [types.TextContent(type="text", text=text)]


def do_search(query: str, limit: int = 10, fields: Optional[str] = None,
              since: Optional[str] = None, project: Optional[str] = None,
              msg_type: Optional[str] = None, tool_name: Optional[str] = None,
              model: Optional[str] = None, cwd: Optional[str] = None) -> List[types.TextContent]:
    tokens = tokenize(query)
    if not tokens:
        return _tc("No valid tokens in query.")

    search_fields = parse_fields(fields)
    token_set = set(tokens)
    now = time.time()

    conn = db_open()
    index_update(conn)

    # build SQL
    where, params = ["messages MATCH ?"], [build_fts_query(tokens, search_fields)]
    if msg_type:
        where.append("msg_type = ?"); params.append(msg_type)
    if tool_name:
        where.append("tool_names LIKE ?"); params.append(f"%{tool_name}%")
    if model:
        where.append("model LIKE ?"); params.append(f"%{model}%")
    if cwd:
        where.append("cwd LIKE ?"); params.append(f"%{cwd}%")

    sess_where = []
    if project:
        sess_where.append("LOWER(project) LIKE ?"); params.append(f"%{project.lower()}%")
    since_ts = parse_since(since)
    if since_ts:
        sess_where.append("mtime >= ?"); params.append(since_ts)
    if sess_where:
        where.append(f"file_path IN (SELECT file_path FROM sessions WHERE {' AND '.join(sess_where)})")

    params.append(limit * 50)
    sql = (
        "SELECT file_path, line_num, msg_type, content_type, tool_names, model, "
        "user_text, assist_text, tool_names, tool_input, tool_result, rank "
        f"FROM messages WHERE {' AND '.join(where)} ORDER BY rank LIMIT ?"
    )

    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as e:
        logger.error("search error: %s sql=%s", e, sql)
        conn.close()
        return _tc(f"Search error: {e}")

    if not rows:
        conn.close()
        return _tc("No results.")

    cache: Dict[str, tuple] = {}
    results = [r for r in (rescore_row(row, token_set, search_fields, now, cache, conn) for row in rows) if r]
    conn.close()

    results.sort(key=lambda x: -x["score"])
    sessions = group_by_session(results, limit)
    active_filters = {"msg_type": msg_type, "tool": tool_name, "model": model,
                      "cwd": cwd, "since": since, "project": project}
    return _tc(format_sessions(sessions, len(results), search_fields, active_filters))


def do_update_index() -> List[types.TextContent]:
    conn = db_open()
    t0 = time.time()
    st = index_update(conn)
    conn.close()
    dt = time.time() - t0
    return _tc(
        f"index updated in {dt:.2f}s | "
        f"new:{st['new']} modified:{st['modified']} stale:{st['stale']} "
        f"unchanged:{st['unchanged']} indexed:{st['indexed']} appended:{st['appended']}"
    )


def do_rebuild_index() -> List[types.TextContent]:
    return _tc(index_rebuild())


def do_stats() -> List[types.TextContent]:
    conn = db_open()
    index_update(conn)

    out = "## Corpus Statistics\n\n"
    out += f"- Sessions: {conn.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]}\n"
    out += f"- Indexed messages: {conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0]}\n\n"

    out += "### Message types\n"
    for t, c in conn.execute("SELECT msg_type, COUNT(*) FROM messages GROUP BY msg_type ORDER BY COUNT(*) DESC"):
        out += f"- {t}: {c:,}\n"

    out += "\n### Content types\n"
    for t, c in conn.execute("SELECT content_type, COUNT(*) FROM messages GROUP BY content_type ORDER BY COUNT(*) DESC"):
        out += f"- {t or '(empty)'}: {c:,}\n"

    out += "\n### Top tools\n"
    for t, c in conn.execute("SELECT tool_names, COUNT(*) FROM messages WHERE tool_names != '' GROUP BY tool_names ORDER BY COUNT(*) DESC LIMIT 20"):
        out += f"- {t}: {c:,}\n"

    out += f"\n### Projects ({conn.execute('SELECT COUNT(DISTINCT project) FROM sessions').fetchone()[0]})\n"
    for (p,) in conn.execute("SELECT DISTINCT project FROM sessions ORDER BY project"):
        out += f"- {p}\n"

    conn.close()
    return _tc(out)


def do_context(file: str, line: int, context_lines: int = 5,
               project: Optional[str] = None) -> List[types.TextContent]:
    if project:
        target = PROJECTS_DIR / project / file
        if not target.exists():
            return _tc(f"File not found: {project}/{file}")
    else:
        target = None
        for sf in PROJECTS_DIR.glob("*/*.jsonl"):
            if sf.name == file:
                target = sf; break
        if not target:
            return _tc(f"File not found: {file}")

    start, end = max(1, line - context_lines), line + context_lines
    out = f"## Context: {file} L{start}-{end}\n\n"

    try:
        with open(target, "rb") as f:
            for ln, raw in enumerate(f, 1):
                if ln > end:
                    break
                if ln < start:
                    continue
                try:
                    entry = orjson.loads(raw)
                    fld = extract_fields(entry)
                    content = (fld.get("user_text") or fld.get("assist_text") or "") if fld else str(entry.get("message", ""))
                    tool = fld.get("tool_names", "") if fld else ""
                    marker = ">>> " if ln == line else "    "
                    label = f"[{entry.get('type', '?')}]"
                    if tool:
                        label += f" tool:{tool}"
                    out += f"L{ln} {marker}{label} {(content[:1000]).replace(chr(10), chr(10) + marker)}\n\n"
                except Exception:
                    continue
    except Exception as e:
        logger.exception("get_context error: %s", target)
        return _tc(f"Failed to read: {e}")

    return _tc(out)


# ══════════════════════════════════════════════════════════════════════════════
# MCP server wiring
# ══════════════════════════════════════════════════════════════════════════════

mcp_server = Server("claude-history")


@mcp_server.list_tools()
async def handle_list_tools() -> List[types.Tool]:
    def prop(typ, desc, **kw):
        p = {"type": [typ, "null"] if typ != "string_required" else "string", "description": desc}
        p.update(kw)
        return p

    return [
        types.Tool(
            name="search_history",
            description=(
                "Search Claude Code conversation history with field-level control. "
                "fields: user_text, assist_text, tool_names, tool_input, tool_result, all. "
                "Default: user_text,assist_text."
            ),
            inputSchema={"type": "object", "properties": {
                "query": prop("string_required", "Full-text query (Chinese/English/mixed)"),
                "limit": prop("integer", "Max sessions (default 10)", default=10),
                "fields": prop("string", "Comma-separated fields to search", default="user_text,assist_text"),
                "since": prop("string", "Time window: '7d','24h','30m'"),
                "project": prop("string", "Filter by project name"),
                "msg_type": prop("string", "Filter: 'user' or 'assistant'"),
                "tool_name": prop("string", "Filter by tool name"),
                "model": prop("string", "Filter by model name"),
                "cwd": prop("string", "Filter by working directory (substring)"),
            }, "required": ["query"]},
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
            inputSchema={"type": "object", "properties": {
                "file": prop("string_required", "Session filename"),
                "line": {"type": "integer", "description": "Line number (1-indexed)"},
                "context_lines": {"type": "integer", "default": 5, "description": "Messages before/after"},
                "project": prop("string", "Project dir name (skips scan)"),
            }, "required": ["file", "line"]},
        ),
    ]


_SEARCH_DEFAULTS = [
    ("query", ""), ("limit", 10), ("fields", None), ("since", None),
    ("project", None), ("msg_type", None), ("tool_name", None),
    ("model", None), ("cwd", None),
]


@mcp_server.call_tool()
async def call_tool(name: str, args: Dict[str, Any]) -> List[types.TextContent]:
    logger.info("call_tool: %s args=%s", name, args)
    loop = asyncio.get_event_loop()
    dispatch = {
        "search_history": lambda: do_search(**{k: args.get(k, d) for k, d in _SEARCH_DEFAULTS}),
        "update_index": do_update_index,
        "rebuild_index": do_rebuild_index,
        "search_stats": do_stats,
        "get_context": lambda: do_context(
            file=args.get("file", ""), line=args.get("line", 1),
            context_lines=args.get("context_lines", 5), project=args.get("project"),
        ),
    }
    fn = dispatch.get(name)
    if not fn:
        logger.error("unknown tool: %s", name)
        return _tc(f"Unknown tool: {name}")
    try:
        result = await loop.run_in_executor(None, fn)
        logger.info("call_tool done: %s", name)
        return result
    except Exception:
        logger.exception("call_tool error: %s", name)
        raise


# ══════════════════════════════════════════════════════════════════════════════
# Entry points — MCP server / CLI
# ══════════════════════════════════════════════════════════════════════════════


async def run_mcp():
    logger.info("mcp-claude-history v%s starting", VERSION)
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, lambda: (db_open(), index_update(db_open()))[0].close() or None)
    logger.info("index ready, serving")

    async with mcp.server.stdio.stdio_server() as (rd, wr):
        logger.info("stdio transport connected")
        await mcp_server.run(rd, wr, InitializationOptions(
            server_name="claude-history", server_version=VERSION,
            capabilities=mcp_server.get_capabilities(
                notification_options=NotificationOptions(), experimental_capabilities={},
            ),
        ))
    logger.info("server shutdown")


def cli():
    import argparse
    p = argparse.ArgumentParser(
        description=f"mcp-claude-history v{VERSION}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Fields: user_text, assist_text, tool_names, tool_input, tool_result, all",
    )
    p.add_argument("query", nargs="?", help="Search query")
    p.add_argument("--fields", default="user_text,assist_text")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--since"); p.add_argument("--project")
    p.add_argument("--msg-type"); p.add_argument("--tool-name")
    p.add_argument("--model"); p.add_argument("--cwd")
    p.add_argument("--rebuild", action="store_true", help="Rebuild index")
    p.add_argument("--update", action="store_true", help="Update index")
    p.add_argument("--stats", action="store_true", help="Show stats")
    p.add_argument("--context", nargs=2, metavar=("FILE", "LINE"))
    p.add_argument("--debug", action="store_true")
    a = p.parse_args()

    if a.debug:
        logging.basicConfig(level=logging.DEBUG)

    if a.rebuild:
        print(index_rebuild()); return
    if a.update:
        print(do_update_index()[0].text); return
    if a.stats:
        print(do_stats()[0].text); return
    if a.context:
        print(do_context(file=a.context[0], line=int(a.context[1]), project=a.project)[0].text); return
    if not a.query:
        p.print_help(); return

    t0 = time.time()
    result = do_search(
        query=a.query, limit=a.limit, fields=a.fields, since=a.since,
        project=a.project, msg_type=a.msg_type, tool_name=a.tool_name,
        model=a.model, cwd=a.cwd,
    )
    print(result[0].text)
    print(f"--- {time.time()-t0:.2f}s | fields: {a.fields} ---")


if __name__ == "__main__":
    if len(sys.argv) > 1 and not sys.argv[1].startswith("{"):
        cli()
    else:
        asyncio.run(run_mcp())
