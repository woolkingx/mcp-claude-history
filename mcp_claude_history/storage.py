from __future__ import annotations

import sqlite3
from pathlib import Path


SCHEMA_VERSION = 2
_SCHEMA_VERSION_KEY = "schema_version"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    file_path TEXT PRIMARY KEY,
    project TEXT,
    mtime REAL,
    line_count INTEGER DEFAULT 0
);

CREATE VIRTUAL TABLE IF NOT EXISTS messages USING fts5(
    file_path UNINDEXED,
    line_num UNINDEXED,
    msg_type UNINDEXED,
    content_type UNINDEXED,
    cwd UNINDEXED,
    model UNINDEXED,
    ts UNINDEXED,
    user_text,
    assist_text,
    tool_names,
    tool_input,
    tool_result,
    tokenize='unicode61 remove_diacritics 2'
);
"""


def _schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT value FROM meta WHERE key = ?",
        (_SCHEMA_VERSION_KEY,),
    ).fetchone()
    if row is None:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return 0


def _set_schema_version(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
        (_SCHEMA_VERSION_KEY, str(SCHEMA_VERSION)),
    )


def _drop_payload_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DROP TABLE IF EXISTS messages;
        DROP TABLE IF EXISTS sessions;
        """
    )


def open_database(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(_SCHEMA_SQL)

    current_version = _schema_version(conn)
    if current_version > SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema version {current_version} is newer than supported {SCHEMA_VERSION}"
        )

    if current_version < SCHEMA_VERSION:
        _drop_payload_tables(conn)
        conn.executescript(_SCHEMA_SQL)
        _set_schema_version(conn)
        conn.commit()

    return conn
