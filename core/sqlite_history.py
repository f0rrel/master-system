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
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from core.history import EventType, HistoryEvent, InvalidEventError, prepare_event
from core.paths import RuntimePaths

__all__ = [
    "BUSY_TIMEOUT_MS",
    "SCHEMA_VERSION",
    "HistoryMigrationError",
    "SQLiteHistoryStore",
]

SCHEMA_VERSION = 2
#: How long a writer waits for another connection's lock before failing.
BUSY_TIMEOUT_MS = 5000

_TYPES = ", ".join(f"'{t.value}'" for t in EventType)


def _events_table(name: str) -> str:
    return f"""CREATE TABLE {name} (
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
)"""


#: Everything on the events table except the table itself. Run one statement at
#: a time, never through executescript, which would commit an open transaction.
_EVENTS_EXTRAS = (
    "CREATE INDEX ix_events_task ON events(project_id, task_id, seq)",
    "CREATE INDEX ix_events_session ON events(session_id, seq)",
    "CREATE INDEX ix_events_attempt ON events(attempt_id)",
    "CREATE TRIGGER events_no_update BEFORE UPDATE ON events "
    "BEGIN SELECT RAISE(ABORT, 'history is append-only'); END",
    "CREATE TRIGGER events_no_delete BEFORE DELETE ON events "
    "BEGIN SELECT RAISE(ABORT, 'history is append-only'); END",
)


class HistoryMigrationError(RuntimeError):
    """Raised when an older history database cannot be safely migrated."""


def _backup_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


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
    """HistoryStore backed by one SQLite file, created on first use.

    A version 1 database is copied to ``<name>.bak-v1-<timestamp>`` and then
    migrated in one transaction. If the copy fails, nothing is migrated.
    """

    def __init__(self, path=None):
        self.path = Path(path) if path is not None else RuntimePaths.default().history_path
        #: Where a v1 database was copied before it was migrated, if it was.
        self.migration_backup: Optional[Path] = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            str(self.path), timeout=BUSY_TIMEOUT_MS / 1000
        )
        self._connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")

        version = self._stored_version()
        if version is None:
            self._transaction(
                [_events_table("events"), *_EVENTS_EXTRAS,
                 "CREATE TABLE schema_meta (version INTEGER NOT NULL)",
                 ("INSERT INTO schema_meta (version) VALUES (?)", (SCHEMA_VERSION,))]
            )
        elif version == 1:
            self._migrate_from_v1()
        elif version != SCHEMA_VERSION:
            raise RuntimeError(
                f"history schema version {version} is not supported "
                f"(expected {SCHEMA_VERSION})"
            )

    # --- schema ------------------------------------------------------------

    def _stored_version(self) -> Optional[int]:
        exists = self._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_meta'"
        ).fetchone()
        if exists is None:
            return None
        row = self._connection.execute("SELECT version FROM schema_meta").fetchone()
        return None if row is None else row[0]

    def _transaction(self, statements) -> None:
        """Run DDL and DML statements atomically."""
        connection = self._connection
        previous = connection.isolation_level
        connection.isolation_level = None
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for statement in statements:
                    if isinstance(statement, tuple):
                        connection.execute(*statement)
                    else:
                        connection.execute(statement)
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")
        finally:
            connection.isolation_level = previous

    def backup_path_for(self, stamp: str) -> Path:
        return self.path.with_name(f"{self.path.name}.bak-v1-{stamp}")

    def _backup(self) -> Path:
        """Copy the database aside before changing its schema, and check the copy."""
        target = self.backup_path_for(_backup_stamp())
        try:
            if target.exists():
                raise FileExistsError(f"{target} already exists")
            copy = sqlite3.connect(str(target))
            try:
                self._connection.backup(copy)
                ok = copy.execute("PRAGMA integrity_check").fetchone()[0]
                copied = copy.execute("SELECT count(*) FROM events").fetchone()[0]
            finally:
                copy.close()
            original = self._connection.execute("SELECT count(*) FROM events").fetchone()[0]
            if ok != "ok" or copied != original:
                raise sqlite3.DatabaseError(
                    f"backup check failed (integrity={ok!r}, rows {copied}/{original})"
                )
        except (OSError, sqlite3.Error) as error:
            raise HistoryMigrationError(
                f"refusing to migrate {self.path} from schema v1: "
                f"the backup to {target} failed: {error}"
            ) from error
        return target

    def _migrate_from_v1(self) -> None:
        """Widen the event-type CHECK by rebuilding the table, rows and seq kept."""
        self.migration_backup = self._backup()
        self._transaction(
            [
                _events_table("events_v2"),
                "INSERT INTO events_v2 SELECT * FROM events",
                "DROP TABLE events",
                "ALTER TABLE events_v2 RENAME TO events",
                *_EVENTS_EXTRAS,
                ("UPDATE schema_meta SET version = ?", (SCHEMA_VERSION,)),
            ]
        )

    def busy_timeout_ms(self) -> int:
        return self._connection.execute("PRAGMA busy_timeout").fetchone()[0]

    def close(self) -> None:
        self._connection.close()

    def append(self, **request) -> HistoryEvent:
        row = prepare_event(**request)
        try:
            cursor = self._insert(row)
        except sqlite3.IntegrityError as error:
            raise InvalidEventError(f"event could not be recorded: {error}") from error
        return HistoryEvent(seq=cursor.lastrowid, **row)

    def _insert(self, row):
        with self._connection:
            return self._connection.execute(
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
