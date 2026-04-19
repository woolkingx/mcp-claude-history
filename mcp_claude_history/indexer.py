from __future__ import annotations

import sqlite3
from pathlib import Path

from mcp_claude_history.extractor import extract_normalized_entry
from mcp_claude_history.parser import parse_json_line
from mcp_claude_history.paths import AppPaths
from mcp_claude_history.schema import IndexSummary


def _session_snapshot(conn: sqlite3.Connection) -> dict[str, tuple[float, int]]:
    snapshot: dict[str, tuple[float, int]] = {}
    for file_path, mtime, line_count in conn.execute(
        "SELECT file_path, mtime, line_count FROM sessions"
    ):
        snapshot[str(file_path)] = (float(mtime), int(line_count or 0))
    return snapshot


def _disk_snapshot(paths: AppPaths) -> dict[str, tuple[float, Path]]:
    snapshot: dict[str, tuple[float, Path]] = {}
    for file_path in paths.projects_dir.glob("*/*.jsonl"):
        try:
            stat_result = file_path.stat()
        except OSError:
            continue
        snapshot[str(file_path)] = (stat_result.st_mtime, file_path)
    return snapshot


def _delete_session(conn: sqlite3.Connection, file_path: str) -> None:
    conn.execute("DELETE FROM sessions WHERE file_path = ?", (file_path,))
    conn.execute("DELETE FROM messages WHERE file_path = ?", (file_path,))


def _index_file(
    conn: sqlite3.Connection,
    file_path: Path,
    *,
    project: str,
    skip_lines: int = 0,
) -> int:
    rows: list[tuple[object, ...]] = []
    total_lines = 0

    with file_path.open("rb") as handle:
        for line_num, raw in enumerate(handle, 1):
            total_lines = line_num
            if line_num <= skip_lines:
                continue

            parsed = parse_json_line(raw)
            if parsed is None:
                continue

            entry = extract_normalized_entry(
                parsed,
                file_path=str(file_path),
                line_num=line_num,
                project=project,
            )
            if entry is None:
                continue

            rows.append(
                (
                    entry.file_path,
                    entry.line_num,
                    entry.msg_type,
                    entry.content_type,
                    entry.cwd,
                    entry.model,
                    entry.timestamp,
                    entry.searchable.user_text,
                    entry.searchable.assist_text,
                    entry.searchable.tool_names,
                    entry.searchable.tool_input,
                    entry.searchable.tool_result,
                )
            )

    if rows:
        conn.executemany(
            """
            INSERT INTO messages (
                file_path, line_num, msg_type, content_type, cwd, model, ts,
                user_text, assist_text, tool_names, tool_input, tool_result
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )

    conn.execute(
        """
        INSERT OR REPLACE INTO sessions (file_path, project, mtime, line_count)
        VALUES (?, ?, ?, ?)
        """,
        (str(file_path), project, file_path.stat().st_mtime, total_lines),
    )
    return len(rows)


def update_index(conn: sqlite3.Connection, paths: AppPaths) -> IndexSummary:
    existing = _session_snapshot(conn)
    disk = _disk_snapshot(paths)

    stale_paths = [file_path for file_path in existing if file_path not in disk]
    new_files: list[Path] = []
    modified_files: list[tuple[Path, int]] = []
    unchanged = 0

    for file_path, (mtime, path) in disk.items():
        current = existing.get(file_path)
        if current is None:
            new_files.append(path)
            continue
        if current[0] != mtime:
            modified_files.append((path, current[1]))
            continue
        unchanged += 1

    for file_path in stale_paths:
        _delete_session(conn, file_path)

    indexed = 0
    appended = 0

    for path in new_files:
        project = path.parent.name
        inserted_rows = _index_file(conn, path, project=project)
        if inserted_rows > 0:
            indexed += 1

    for path, line_count in modified_files:
        project = path.parent.name
        inserted_rows = _index_file(
            conn,
            path,
            project=project,
            skip_lines=line_count,
        )
        if inserted_rows > 0:
            appended += 1

    conn.commit()

    return IndexSummary(
        new=len(new_files),
        modified=len(modified_files),
        stale=len(stale_paths),
        unchanged=unchanged,
        indexed=indexed,
        appended=appended,
    )
