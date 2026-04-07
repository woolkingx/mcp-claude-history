#!/usr/bin/env python3
"""
MCP Claude History v4.0.0 — Search Claude Code conversation history.

SQLite FTS5 + porter stemming + jieba. Low-level MCP SDK.

Module layout:
  1. DB           — schema, connection, migration
  2. Tokenizer    — jieba init, tokenize, FTS5 query builder
  3. Extractor    — JSONL entry → (text, content_type, tool_name)
  4. Indexer      — full scan, quick scan, stale cleanup
  5. Scorer       — BM25 + jieba re-score + recency boost
  6. Formatter    — snippets, markdown output
  7. Tools        — search_history, search_stats, get_context
  8. Server       — MCP handlers, async startup, main
"""

import asyncio
import logging
import math
import re
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import mcp.server.stdio
import mcp.types as types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.models import InitializationOptions

import orjson

logger = logging.getLogger("mcp-claude-history")

PROJECTS_DIR = Path.home() / ".claude/projects"
DB_PATH = Path.home() / ".claude/history-index.db"
RECENCY_HALF_LIFE = 7
DB_SCHEMA_VERSION = 2


# =========================================================================
# 1. DB
# =========================================================================


def db_connect() -> sqlite3.Connection:
    """Open DB, apply migration if needed, return connection."""
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"
    )
    _migrate(conn)
    _ensure_schema(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Drop tables if schema version is outdated."""
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()
    version = int(row[0]) if row else 0
    if version < DB_SCHEMA_VERSION:
        conn.executescript("DROP TABLE IF EXISTS messages; DROP TABLE IF EXISTS sessions;")
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(DB_SCHEMA_VERSION),),
        )
        conn.commit()


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create tables if they don't exist."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            file_path TEXT PRIMARY KEY,
            project   TEXT,
            mtime     REAL,
            cwd       TEXT,
            branch    TEXT
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS messages USING fts5(
            file_path    UNINDEXED,
            line_num     UNINDEXED,
            msg_type     UNINDEXED,
            content_type UNINDEXED,
            tool_name    UNINDEXED,
            ts           UNINDEXED,
            text,
            tokenize='porter unicode61 remove_diacritics 2'
        );
    """)


def db_get_meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def db_set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value)
    )


# =========================================================================
# 2. Tokenizer
# =========================================================================

_jieba = None  # lazy init


def _get_jieba():
    """Lazy-load jieba to avoid blocking import."""
    global _jieba
    if _jieba is None:
        import jieba as _j
        _j.setLogLevel(logging.WARNING)
        _j.initialize()
        _jieba = _j
    return _jieba


def tokenize_query(text: str) -> List[str]:
    """Query → unique lowered jieba tokens, max 15."""
    j = _get_jieba()
    tokens = [w.strip().lower() for w in j.cut(text) if w.strip()]
    seen = set()
    return [t for t in tokens if t and not (t in seen or seen.add(t))][:15]


def tokenize_message(text: str) -> set:
    """Message text → set of lowered jieba tokens."""
    j = _get_jieba()
    return {w.strip().lower() for w in j.cut(text) if w.strip()}


def build_fts_query(tokens: List[str]) -> str:
    """Tokens → FTS5 MATCH expression (OR for recall)."""
    parts = []
    for t in tokens:
        if re.search(r'[^a-zA-Z0-9\u4e00-\u9fff]', t):
            parts.append(f'"{t}"')
        else:
            parts.append(t)
    return " OR ".join(parts)


# =========================================================================
# 3. Extractor
# =========================================================================


def extract_content(entry: dict) -> Tuple[str, str, str]:
    """JSONL entry → (text, content_type, tool_name).

    content_type: 'text' | 'tool_use' | 'tool_result' | 'mixed'
    tool_name:    first tool_use name, or ''
    """
    raw = entry.get("message", {}).get("content", "")

    if isinstance(raw, str):
        return (raw, "text", "") if raw else ("", "", "")

    if not isinstance(raw, list):
        return ("", "", "")

    parts, ctypes, tool = [], set(), ""
    for item in raw:
        if not isinstance(item, dict):
            continue
        ct = item.get("type", "")
        ctypes.add(ct)
        if ct == "text":
            parts.append(item.get("text", ""))
        elif ct == "tool_use":
            if not tool:
                tool = item.get("name", "")
            inp = orjson.dumps(item.get("input", {})).decode()[:300]
            parts.append(f"[Tool: {item.get('name', '')} {inp}]")
        elif ct == "tool_result":
            rc = item.get("content", "")
            if isinstance(rc, str):
                parts.append(rc[:500])
            elif isinstance(rc, list):
                for sub in rc:
                    if isinstance(sub, dict) and sub.get("type") == "text":
                        parts.append(sub.get("text", "")[:500])

    content = " ".join(parts)
    ctype = "mixed" if len(ctypes) > 1 else (ctypes.pop() if ctypes else "")
    return (content, ctype, tool)


# =========================================================================
# 4. Indexer
# =========================================================================


def index_full(conn: sqlite3.Connection) -> int:
    """Full scan: index all new/changed files + remove stale entries."""
    existing = _load_existing(conn)
    _cleanup_stale(conn, existing)
    count = _scan_and_index(conn, existing, since_mtime=0)
    _save_index_ts(conn)
    return count


def index_quick(conn: sqlite3.Connection) -> int:
    """Quick scan: only files modified since last index timestamp."""
    existing = _load_existing(conn)
    last_ts = float(db_get_meta(conn, "last_index_ts") or "0")
    count = _scan_and_index(conn, existing, since_mtime=last_ts)
    _save_index_ts(conn)
    return count


def _load_existing(conn: sqlite3.Connection) -> Dict[str, float]:
    """Load {file_path: mtime} from sessions table."""
    return {r[0]: r[1] for r in conn.execute("SELECT file_path, mtime FROM sessions")}


def _cleanup_stale(conn: sqlite3.Connection, existing: Dict[str, float]) -> None:
    """Remove index entries for deleted files."""
    stale = [fp for fp in existing if not Path(fp).exists()]
    for fp in stale:
        conn.execute("DELETE FROM sessions WHERE file_path = ?", (fp,))
        conn.execute("DELETE FROM messages WHERE file_path = ?", (fp,))
    if stale:
        logger.info(f"cleaned {len(stale)} stale sessions")


def _scan_and_index(
    conn: sqlite3.Connection,
    existing: Dict[str, float],
    since_mtime: float,
) -> int:
    """Walk JSONL files, index those newer than since_mtime."""
    indexed = 0
    for session_file in PROJECTS_DIR.glob("*/*.jsonl"):
        fpath = str(session_file)
        try:
            mtime = session_file.stat().st_mtime
        except OSError:
            continue
        if mtime < since_mtime:
            continue
        if fpath in existing and existing[fpath] == mtime:
            continue

        rows, cwd, branch = _parse_session(session_file)
        if not rows:
            continue

        # replace old data
        if fpath in existing:
            conn.execute("DELETE FROM sessions WHERE file_path = ?", (fpath,))
            conn.execute("DELETE FROM messages WHERE file_path = ?", (fpath,))

        conn.execute(
            "INSERT OR REPLACE INTO sessions (file_path, project, mtime, cwd, branch) "
            "VALUES (?, ?, ?, ?, ?)",
            (fpath, session_file.parent.name, mtime, cwd, branch),
        )
        conn.executemany(
            "INSERT INTO messages "
            "(file_path, line_num, msg_type, content_type, tool_name, ts, text) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        indexed += 1

    if indexed > 0:
        conn.execute("INSERT INTO messages(messages) VALUES('optimize')")
    conn.commit()
    return indexed


def _parse_session(path: Path) -> Tuple[list, str, str]:
    """Parse one JSONL file → (rows, cwd, branch)."""
    rows, cwd, branch = [], "", ""
    fpath = str(path)
    try:
        with open(path, "rb") as f:
            for line_num, raw in enumerate(f, 1):
                try:
                    entry = orjson.loads(raw)
                except orjson.JSONDecodeError:
                    continue
                if not cwd:
                    cwd = entry.get("cwd", "")
                if not branch:
                    branch = entry.get("gitBranch", "")
                if entry.get("type") not in ("user", "assistant"):
                    continue
                content, content_type, tool_name = extract_content(entry)
                if content:
                    rows.append((
                        fpath, str(line_num), entry["type"],
                        content_type, tool_name,
                        entry.get("timestamp", ""), content,
                    ))
    except Exception:
        pass
    return rows, cwd, branch


def _save_index_ts(conn: sqlite3.Connection) -> None:
    db_set_meta(conn, "last_index_ts", str(time.time()))


# =========================================================================
# 5. Scorer
# =========================================================================


def score_candidates(
    rows: list,
    token_set: set,
    conn: sqlite3.Connection,
) -> List[dict]:
    """FTS5 rows → scored + filtered results."""
    now = time.time()
    scored = []
    session_cache: Dict[str, Tuple] = {}

    for file_path, line_num, msg_type, ct, tool, ts, text, fts_rank in rows:
        project, mtime = _session_meta(conn, file_path, session_cache)
        if not project:
            continue

        mc = len(token_set & tokenize_message(text))
        if mc == 0:
            continue

        bm25 = -fts_rank if fts_rank else 0
        recency = _recency_boost(now, mtime)
        score = 0.7 * bm25 + 0.3 * mc * (1 + recency)

        scored.append({
            "file": Path(file_path).name,
            "project": project,
            "line": int(line_num),
            "hits": mc,
            "score": score,
            "msg_type": msg_type,
            "content_type": ct,
            "tool_name": tool,
            "text": text,
        })

    scored.sort(key=lambda x: -x["score"])
    return scored


def _session_meta(
    conn: sqlite3.Connection,
    file_path: str,
    cache: Dict[str, Tuple],
) -> Tuple[Optional[str], float]:
    """Get (project, mtime) for a file_path, with caching."""
    if file_path not in cache:
        row = conn.execute(
            "SELECT project, mtime FROM sessions WHERE file_path = ?", (file_path,)
        ).fetchone()
        cache[file_path] = row if row else (None, 0)
    return cache[file_path]


def _recency_boost(now: float, mtime: float) -> float:
    """Exponential decay with half-life."""
    age_days = max((now - mtime) / 86400, 0.01)
    return math.exp(-0.693 * age_days / RECENCY_HALF_LIFE)


# =========================================================================
# 6. Formatter
# =========================================================================


def make_snippets(text: str, token_set: set, context: int = 20) -> List[str]:
    """Extract ±context chars around each matched token, with **bold**."""
    j = _get_jieba()
    tokens = list(j.tokenize(text))
    seen, snippets = set(), []
    for word, start, end in tokens:
        w = word.strip().lower()
        if w in token_set and w not in seen:
            seen.add(w)
            s, e = max(0, start - context), min(len(text), end + context)
            before = text[s:start].replace("\n", " ")
            after = text[end:e].replace("\n", " ")
            frag = f"{before}**{text[start:end]}**{after}"
            if s > 0:
                frag = "..." + frag
            if e < len(text):
                frag += "..."
            snippets.append(frag)
    return snippets or [text[:100]]


def format_search_results(
    scored: List[dict],
    token_set: set,
    limit: int,
    filters: List[str],
) -> str:
    """Scored messages → grouped markdown output."""
    groups, order = _group_by_session(scored)

    # sort sessions by aggregated score
    order.sort(key=lambda k: -groups[k]["session_score"])
    sessions = [groups[k] for k in order[:limit]]

    out = f"{len(scored)} matched, {len(sessions)} sessions\n"
    if filters:
        out += f"filters: {', '.join(filters)}\n"

    for i, s in enumerate(sessions, 1):
        out += f"\n## Session {i} — {s['file']}\n"
        out += f"project: {s['project']} | score: {s['session_score']:.1f} | hits: {s['total_hits']}\n\n"

        shown = s["messages"][:5]
        rest = len(s["messages"]) - len(shown)
        for m in shown:
            out += _format_hit(m, token_set)
        if rest > 0:
            out += f"\n+{rest} more\n"

    return out


def _group_by_session(scored: List[dict]) -> Tuple[dict, list]:
    """Group scored messages by (project, file)."""
    groups: Dict[tuple, dict] = {}
    order: list = []
    for item in scored:
        key = (item["project"], item["file"])
        if key not in groups:
            groups[key] = {
                "project": item["project"],
                "file": item["file"],
                "messages": [],
                "session_score": 0.0,
                "total_hits": 0,
            }
            order.append(key)
        groups[key]["messages"].append(item)
        groups[key]["session_score"] += item["score"]
        groups[key]["total_hits"] += item["hits"]
    return groups, order


def _format_hit(m: dict, token_set: set) -> str:
    """Format one search hit as markdown."""
    meta = f"L{m['line']} | {m['msg_type']}"
    if m["content_type"] and m["content_type"] != "text":
        meta += f" [{m['content_type']}]"
    if m["tool_name"]:
        meta += f" tool:{m['tool_name']}"
    meta += f" | hits:{m['hits']} score:{m['score']:.3f}\n"

    snippets = make_snippets(m["text"], token_set)
    for snip in snippets:
        meta += f"  {snip}\n"
    return meta


def format_stats(conn: sqlite3.Connection) -> str:
    """Build corpus statistics markdown from index."""
    sess_count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    msg_count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    type_dist = conn.execute(
        "SELECT msg_type, COUNT(*) FROM messages GROUP BY msg_type ORDER BY COUNT(*) DESC"
    ).fetchall()

    ct_dist = conn.execute(
        "SELECT content_type, COUNT(*) FROM messages "
        "GROUP BY content_type ORDER BY COUNT(*) DESC"
    ).fetchall()

    top_tools = conn.execute(
        "SELECT tool_name, COUNT(*) FROM messages WHERE tool_name != '' "
        "GROUP BY tool_name ORDER BY COUNT(*) DESC LIMIT 20"
    ).fetchall()

    projects = conn.execute(
        "SELECT project, cwd, branch FROM sessions ORDER BY project"
    ).fetchall()

    out = "## Corpus Statistics\n\n"
    out += f"- Sessions: {sess_count}\n"
    out += f"- Indexed messages: {msg_count}\n\n"

    out += "### Message types\n"
    for t, c in type_dist:
        out += f"- {t}: {c:,}\n"

    out += "\n### Content types\n"
    for t, c in ct_dist:
        out += f"- {t or '(empty)'}: {c:,}\n"

    out += "\n### Top tools\n"
    for t, c in top_tools:
        out += f"- {t}: {c:,}\n"

    seen = set()
    out += f"\n### Projects ({len(set(p[0] for p in projects))})\n"
    out += "| Project | CWD | Branch |\n|---------|-----|--------|\n"
    for proj, cwd_val, br in projects:
        if proj not in seen:
            seen.add(proj)
            out += f"| {proj} | {cwd_val} | {br} |\n"

    return out


def format_context(target_file: Path, file: str, line: int, context_lines: int) -> str:
    """Read JSONL and format context around target line."""
    start = max(1, line - context_lines)
    end = line + context_lines
    out = f"## Context: {file} L{start}-{end}\n\n"

    try:
        with open(target_file, "rb") as f:
            for n, raw in enumerate(f, 1):
                if n > end:
                    break
                if n < start:
                    continue
                try:
                    entry = orjson.loads(raw)
                    msg_type = entry.get("type")
                    content, _, tool_nm = extract_content(entry)
                    if not content:
                        content = str(entry.get("message", ""))

                    marker = ">>> " if n == line else "    "
                    label = f"[{msg_type}]" if msg_type else "[?]"
                    if tool_nm:
                        label += f" tool:{tool_nm}"
                    truncated = (content[:1000] or "").replace("\n", "\n" + marker)
                    out += f"L{n} {marker}{label} {truncated}\n\n"
                except Exception:
                    continue
    except Exception as e:
        return f"Failed to read: {e}"

    return out


# =========================================================================
# 7. Tools
# =========================================================================


def parse_since(since: Optional[str]) -> Optional[float]:
    """'7d' / '24h' / '30m' → unix timestamp cutoff."""
    if not since:
        return None
    since = since.strip().lower()
    units = {"d": 86400, "h": 3600, "m": 60}
    if since[-1] in units:
        try:
            delta = float(since[:-1]) * units[since[-1]]
            return (datetime.now(timezone.utc) - timedelta(seconds=delta)).timestamp()
        except ValueError:
            pass
    return None


def tool_search_history(
    query: str,
    limit: int = 10,
    since: Optional[str] = None,
    project: Optional[str] = None,
    msg_type: Optional[str] = None,
    content_type: Optional[str] = None,
    tool_name: Optional[str] = None,
    branch: Optional[str] = None,
    cwd: Optional[str] = None,
) -> str:
    """Full pipeline: tokenize → index → query → score → format."""
    tokens = tokenize_query(query)
    if not tokens:
        return "No valid tokens in query."

    conn = db_connect()
    index_quick(conn)

    # FTS5 query
    rows, err = _fts_query(conn, tokens, limit * 30,
                           msg_type, content_type, tool_name,
                           project, branch, cwd, parse_since(since))
    if err:
        conn.close()
        return err

    # score + format
    token_set = set(tokens)
    scored = score_candidates(rows, token_set, conn)
    conn.close()

    if not scored:
        return "No results."

    filters = _active_filters(msg_type=msg_type, content_type=content_type,
                              tool_name=tool_name, branch=branch, cwd=cwd,
                              since=since, project=project)
    return format_search_results(scored, token_set, limit, filters)


def _fts_query(
    conn: sqlite3.Connection,
    tokens: List[str],
    candidate_limit: int,
    msg_type: Optional[str],
    content_type: Optional[str],
    tool_name: Optional[str],
    project: Optional[str],
    branch: Optional[str],
    cwd: Optional[str],
    since_ts: Optional[float],
) -> Tuple[list, Optional[str]]:
    """Build and execute FTS5 query. Returns (rows, error_or_none)."""
    where, params = ["messages MATCH ?"], [build_fts_query(tokens)]

    # message-level filters
    if msg_type:
        where.append("msg_type = ?")
        params.append(msg_type)
    if content_type:
        where.append("content_type = ?")
        params.append(content_type)
    if tool_name:
        where.append("tool_name = ?")
        params.append(tool_name)

    # session-level filters
    sf = []
    if project:
        sf.append("LOWER(project) LIKE ?")
        params.append(f"%{project.lower()}%")
    if branch:
        sf.append("LOWER(branch) LIKE ?")
        params.append(f"%{branch.lower()}%")
    if cwd:
        sf.append("LOWER(cwd) LIKE ?")
        params.append(f"%{cwd.lower()}%")
    if since_ts:
        sf.append("mtime >= ?")
        params.append(since_ts)
    if sf:
        where.append(
            f"file_path IN (SELECT file_path FROM sessions WHERE {' AND '.join(sf)})"
        )

    params.append(candidate_limit)
    sql = (
        "SELECT file_path, line_num, msg_type, content_type, tool_name, ts, text, rank "
        f"FROM messages WHERE {' AND '.join(where)} ORDER BY rank LIMIT ?"
    )

    try:
        return conn.execute(sql, params).fetchall(), None
    except sqlite3.OperationalError as e:
        return [], f"Search error: {e}"


def _active_filters(**kw) -> List[str]:
    """Collect non-None filter labels."""
    names = {
        "msg_type": "msg_type", "content_type": "content_type",
        "tool_name": "tool", "branch": "branch", "cwd": "cwd",
        "since": "since", "project": "project",
    }
    return [f"{names[k]}={v}" for k, v in kw.items() if v]


def tool_search_stats() -> str:
    """Corpus statistics from index."""
    conn = db_connect()
    index_quick(conn)
    result = format_stats(conn)
    conn.close()
    return result


def tool_get_context(
    file: str,
    line: int,
    context_lines: int = 5,
    project: Optional[str] = None,
) -> str:
    """Read conversation context around a search hit."""
    target = _resolve_file(file, project)
    if isinstance(target, str):
        return target  # error message
    return format_context(target, file, line, context_lines)


def _resolve_file(file: str, project: Optional[str]) -> Any:
    """Find session file. Returns Path on success, error string on failure."""
    if project:
        target = PROJECTS_DIR / project / file
        return target if target.exists() else f"File not found: {project}/{file}"
    for session_file in PROJECTS_DIR.glob("*/*.jsonl"):
        if session_file.name == file:
            return session_file
    return f"File not found: {file}"


# =========================================================================
# 8. Server
# =========================================================================

server = Server("claude-history")

_index_ready: Optional[asyncio.Event] = None


def _text(s: str) -> List[types.TextContent]:
    return [types.TextContent(type="text", text=s)]


@server.list_tools()
async def handle_list_tools() -> List[types.Tool]:
    return [
        types.Tool(
            name="search_history",
            description=(
                "Search Claude Code conversation history with field-level filtering. "
                "SQLite FTS5 + porter stemming + jieba, BM25 ranking with recency boost."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Full-text search (Chinese/English/mixed). "
                        "FTS5 syntax: OR, AND, \"phrase\", prefix*",
                    },
                    "limit": {"type": "integer", "default": 10},
                    "since": {"type": ["string", "null"], "default": None,
                              "description": "Time window: '7d','24h','30m'"},
                    "project": {"type": ["string", "null"], "default": None},
                    "msg_type": {"type": ["string", "null"], "default": None,
                                 "description": "'user' or 'assistant'"},
                    "content_type": {"type": ["string", "null"], "default": None,
                                     "description": "'text','tool_use','tool_result','mixed'"},
                    "tool_name": {"type": ["string", "null"], "default": None,
                                  "description": "e.g. 'Edit','Bash','Read'"},
                    "branch": {"type": ["string", "null"], "default": None},
                    "cwd": {"type": ["string", "null"], "default": None,
                            "description": "Working directory substring match"},
                },
                "required": ["query"],
            },
        ),
        types.Tool(
            name="search_stats",
            description="Corpus statistics: message counts, tool distribution, projects.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="get_context",
            description="Conversation context around a search hit.",
            inputSchema={
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                    "context_lines": {"type": "integer", "default": 5},
                    "project": {"type": ["string", "null"], "default": None},
                },
                "required": ["file", "line"],
            },
        ),
    ]


_SEARCH_PARAMS = [
    ("query", ""), ("limit", 10), ("since", None), ("project", None),
    ("msg_type", None), ("content_type", None), ("tool_name", None),
    ("branch", None), ("cwd", None),
]


@server.call_tool()
async def handle_call_tool(name: str, arguments: Dict[str, Any]) -> List[types.TextContent]:
    if _index_ready:
        await _index_ready.wait()

    loop = asyncio.get_event_loop()

    if name == "search_history":
        args = {k: arguments.get(k, d) for k, d in _SEARCH_PARAMS}
        return _text(await loop.run_in_executor(None, lambda: tool_search_history(**args)))

    if name == "search_stats":
        return _text(await loop.run_in_executor(None, tool_search_stats))

    if name == "get_context":
        return _text(await loop.run_in_executor(None, lambda: tool_get_context(
            file=arguments.get("file", ""),
            line=arguments.get("line", 1),
            context_lines=arguments.get("context_lines", 5),
            project=arguments.get("project"),
        )))

    return _text(f"Unknown tool: {name}")


def _sync_startup_index() -> int:
    """Sync: connect + full index + close. Runs in single thread."""
    t0 = time.time()
    conn = db_connect()
    count = index_full(conn)
    conn.close()
    logger.info(f"startup: indexed {count} sessions in {time.time()-t0:.1f}s")
    return count


async def _startup_index():
    """Full index in background thread."""
    global _index_ready
    _index_ready = asyncio.Event()
    await asyncio.get_event_loop().run_in_executor(None, _sync_startup_index)
    _index_ready.set()


async def run():
    asyncio.create_task(_startup_index())
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="claude-history",
                server_version="4.0.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


def main():
    asyncio.run(run())


if __name__ == "__main__":
    main()
