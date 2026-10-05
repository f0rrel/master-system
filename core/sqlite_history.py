"""SQLite persistence for :mod:`core.history`.

The only module that imports ``sqlite3``. One commit per append, in WAL mode
with ``synchronous=FULL``, so an event is on disk before the caller goes on to
the side effect it announces. Append-only is enforced by the database itself
through triggers, not just by the absence of an update method here.

SQLite and the YAML project files are separate stores. Nothing here pretends
they commit together.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from core.history import EventType, HistoryEvent, prepare_event
from core.paths import RuntimePaths

__all__ = ["BUSY_TIMEOUT_MS", "SCHEMA_VERSION", "SQLiteHistoryStore"]

SCHEMA_VERSION = 1
#: How long a writer waits for another connection's lock before failing.
BUSY_TIMEOUT_MS = 5000

_TYPES = ", ".join(f"'{t.value}'" for t in EventType)

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS events (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    TEXT NOT NULL UNIQUE,
    run_id      TEXT NOT NULL,
    session_id  TEXT,
    project_id  TEXT NOT NULL,
    task_id     TEXT,
    attempt_id  TEXT,
    type        TEXT NOT NULL CHECK (type IN ({_TYPES})),
    payload     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_task ON events(project_id, task_id, seq);
CREATE INDEX IF NOT EXISTS ix_events_session ON events(session_id, seq);
CREATE INDEX IF NOT EXISTS ix_events_attempt ON events(attempt_id);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'history is append-only'); END;
CREATE TABLE IF NOT EXISTS schema_meta (version INTEGER NOT NULL);
"""

_COLUMNS = (
    "seq, event_id, run_id, session_id, project_id, task_id, attempt_id, "
    "type, payload, created_at"
)


def _event(row) -> HistoryEvent:
    return HistoryEvent(
        seq=row[0],
        event_id=row[1],
        run_id=row[2],
        session_id=row[3],
        project_id=row[4],
        task_id=row[5],
        attempt_id=row[6],
        type=EventType(row[7]),
        payload_json=row[8],
        created_at=row[9],
    )


class SQLiteHistoryStore:
    """HistoryStore backed by one SQLite file, created on first use."""

    def __init__(self, path=None):
        self.path = Path(path) if path is not None else RuntimePaths.default().history_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            str(self.path), timeout=BUSY_TIMEOUT_MS / 1000
        )
        self._connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        with self._connection:
            self._connection.executescript(_SCHEMA)
            stored = self._connection.execute(
                "SELECT version FROM schema_meta"
            ).fetchone()
            if stored is None:
                self._connection.execute(
                    "INSERT INTO schema_meta (version) VALUES (?)", (SCHEMA_VERSION,)
                )
            elif stored[0] != SCHEMA_VERSION:
                raise RuntimeError(
                    f"history schema version {stored[0]} is not supported "
                    f"(expected {SCHEMA_VERSION})"
                )

    def busy_timeout_ms(self) -> int:
        return self._connection.execute("PRAGMA busy_timeout").fetchone()[0]

    def close(self) -> None:
        self._connection.close()

    def append(self, **request) -> HistoryEvent:
        row = prepare_event(**request)
        with self._connection:
            cursor = self._connection.execute(
                "INSERT INTO events (event_id, run_id, session_id, project_id, "
                "task_id, attempt_id, type, payload, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row["event_id"],
                    row["run_id"],
                    row["session_id"],
                    row["project_id"],
                    row["task_id"],
                    row["attempt_id"],
                    row["type"].value,
                    row["payload_json"],
                    row["created_at"],
                ),
            )
        return HistoryEvent(seq=cursor.lastrowid, **row)

    def events(
        self,
        *,
        project_id: Optional[str] = None,
        session_id: Optional[str] = None,
        run_id: Optional[str] = None,
        task_id: Optional[str] = None,
        attempt_id: Optional[str] = None,
        types=None,
        last: Optional[int] = None,
    ) -> tuple:
        clauses, values = [], []
        for column, value in (
            ("project_id", project_id),
            ("session_id", session_id),
            ("run_id", run_id),
            ("task_id", task_id),
            ("attempt_id", attempt_id),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                values.append(value)
        if types is not None:
            names = [EventType(t).value for t in types]
            if not names:
                return ()
            clauses.append(f"type IN ({', '.join('?' for _ in names)})")
            values.extend(names)

        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        if last is not None:
            if last <= 0:
                return ()
            query = (
                f"SELECT {_COLUMNS} FROM (SELECT {_COLUMNS} FROM events{where} "
                f"ORDER BY seq DESC LIMIT ?) ORDER BY seq ASC"
            )
            values.append(last)
        else:
            query = f"SELECT {_COLUMNS} FROM events{where} ORDER BY seq ASC"

        return tuple(_event(row) for row in self._connection.execute(query, values))
