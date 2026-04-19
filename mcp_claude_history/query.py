from __future__ import annotations

import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from mcp_claude_history.ranker import analyze_candidate, score_from_analysis
from mcp_claude_history.schema import SearchResult

ALL_FIELDS = frozenset({"user_text", "assist_text", "tool_names", "tool_input", "tool_result"})
DEFAULT_FIELDS = frozenset({"user_text", "assist_text"})


def tokenize_query(text: str) -> list[str]:
    tokens = text.lower().split()
    seen = set()
    return [t for t in tokens if t and not (t in seen or seen.add(t))][:15]


def parse_fields(fields_str: str | None) -> set[str]:
    if not fields_str or fields_str.strip().lower() == "all":
        return set(ALL_FIELDS)
    valid = {f.strip() for f in fields_str.split(",") if f.strip()} & ALL_FIELDS
    return valid if valid else set(DEFAULT_FIELDS)


def build_fts_query(tokens: list[str], search_fields: set[str]) -> str:
    parts = [f'"{t}"' if re.search(r"[^a-zA-Z0-9\u4e00-\u9fff]", t) else t for t in tokens]
    terms = " OR ".join(parts)
    return f"{{{' '.join(sorted(search_fields))}}} : {terms}"


def parse_since(since: str | None) -> float | None:
    if not since:
        return None
    s = since.strip().lower()
    units = {"d": 86400, "h": 3600, "m": 60}
    if s and s[-1] in units:
        try:
            seconds = float(s[:-1]) * units[s[-1]]
        except ValueError:
            return None
        return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).timestamp()
    return None


def candidate_retrieve(
    conn: sqlite3.Connection,
    query: str,
    *,
    filters: dict[str, str | None] | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:

    tokens = tokenize_query(query)
    if not tokens:
        return []

    filters = filters or {}
    search_fields = parse_fields(filters.get("fields"))
    where: list[str] = ["messages MATCH ?"]
    params: list[Any] = [build_fts_query(tokens, search_fields)]

    if filters.get("msg_type"):
        where.append("msg_type = ?")
        params.append(filters["msg_type"])
    if filters.get("tool"):
        where.append("tool_names LIKE ?")
        params.append(f"%{filters['tool']}%")
    if filters.get("model"):
        where.append("model LIKE ?")
        params.append(f"%{filters['model']}%")
    if filters.get("cwd"):
        where.append("cwd LIKE ?")
        params.append(f"%{filters['cwd']}%")

    sess_where: list[str] = []
    if filters.get("project"):
        sess_where.append("LOWER(project) LIKE ?")
        params.append(f"%{filters['project'].lower()}%")

    since_ts = parse_since(filters.get("since"))
    if since_ts:
        sess_where.append("mtime >= ?")
        params.append(since_ts)

    if sess_where:
        where.append(
            f"messages.file_path IN (SELECT file_path FROM sessions WHERE {' AND '.join(sess_where)})"
        )

    params.append(limit * 50)
    sql = (
        "SELECT messages.file_path, line_num, msg_type, content_type, cwd, model, ts, "
        "user_text, assist_text, tool_names, tool_input, tool_result, "
        "bm25(messages) AS fts_rank, sessions.project AS project, sessions.mtime AS mtime "
        "FROM messages JOIN sessions ON sessions.file_path = messages.file_path "
        f"WHERE {' AND '.join(where)} ORDER BY fts_rank LIMIT ?"
    )

    rows = conn.execute(sql, params).fetchall()
    columns = [
        "file_path", "line_num", "msg_type", "content_type", "cwd", "model", "ts",
        "user_text", "assist_text", "tool_names", "tool_input", "tool_result",
        "fts_rank", "project", "mtime",
    ]
    return [dict(zip(columns, row, strict=True)) for row in rows]


def group_by_session(results: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []

    for item in results:
        key = (str(item["project"]), Path(str(item["file_path"])).name)
        if key not in groups:
            groups[key] = {
                "project": key[0],
                "file": key[1],
                "messages": [],
                "session_score": 0.0,
                "total_hits": 0,
            }
            order.append(key)
        groups[key]["messages"].append(item)
        groups[key]["session_score"] += float(item["score"])
        groups[key]["total_hits"] += int(item["hits"])

    order.sort(
        key=lambda key: (
            -groups[key]["session_score"],
            -groups[key]["total_hits"],
            -len(groups[key]["messages"]),
        )
    )

    for key in order:
        groups[key]["messages"].sort(key=lambda message: (-message["hits"], -message["score"]))

    return [groups[key] for key in order[:limit]]


def search_messages(
    conn: sqlite3.Connection,
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
) -> SearchResult:
    tokens = tokenize_query(query)
    search_fields = parse_fields(fields)
    raw_filters = {
        "fields": fields,
        "since": since,
        "project": project,
        "msg_type": msg_type,
        "tool": tool_name,
        "model": model,
        "cwd": cwd,
    }

    rows = candidate_retrieve(conn, query, filters=raw_filters, limit=limit)
    now = time.time()
    scored: list[dict[str, Any]] = []
    for row in rows:
        analysis = analyze_candidate(row, tokens, search_fields)
        score = score_from_analysis(row, analysis, now=now)
        if score <= 0:
            continue
        total_mc, _pair_bonus, matched_fields = analysis
        row = dict(row)
        row["score"] = score
        row["hits"] = total_mc
        row["matched_fields"] = matched_fields
        scored.append(row)

    scored.sort(key=lambda item: -item["score"])
    sessions = group_by_session(scored, limit)
    active_filters: dict[str, str] = {}
    if msg_type:
        active_filters["msg_type"] = msg_type
    if tool_name:
        active_filters["tool"] = tool_name
    if model:
        active_filters["model"] = model
    if cwd:
        active_filters["cwd"] = cwd
    if project:
        active_filters["project"] = project
    if parse_since(since) is not None:
        active_filters["since"] = since or ""

    return SearchResult(
        query=query,
        search_fields=tuple(sorted(search_fields)),
        filters=active_filters,
        total_matches=len(scored),
        sessions=sessions,
    )
