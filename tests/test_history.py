"""Tests for the append-only history contract and both of its stores."""

import ast
import sqlite3
from pathlib import Path

import pytest

from core.history import (
    MAX_PAYLOAD_BYTES,
    EventType,
    HistoryStore,
    InMemoryHistoryStore,
    InvalidEventError,
)
from core import sqlite_history
from core.sqlite_history import SCHEMA_VERSION, HistoryMigrationError, SQLiteHistoryStore

CORE = Path(__file__).resolve().parent.parent / "core"


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    if request.param == "memory":
        return InMemoryHistoryStore()
    return SQLiteHistoryStore(tmp_path / "history.sqlite")


def append(store, type, **kwargs):
    kwargs.setdefault("run_id", "r1")
    kwargs.setdefault("project_id", "alpha")
    return store.append(type=type, **kwargs)


# --- contract -------------------------------------------------------------


def test_both_stores_satisfy_the_protocol(store):
    assert isinstance(store, HistoryStore)


def test_sequence_numbers_increase_in_recording_order(store):
    first = append(store, EventType.RUN_STARTED)
    second = append(store, EventType.DECISION, payload={"decision": "wait"})

    assert second.seq > first.seq
    assert [e.type for e in store.events()] == [EventType.RUN_STARTED, EventType.DECISION]


def test_payload_round_trips_and_cannot_be_mutated_through_the_event(store):
    event = append(store, EventType.DECISION, payload={"reason": "x", "n": [1, 2]})

    copy = event.payload
    copy["reason"] = "changed"

    assert store.events()[0].payload == {"reason": "x", "n": [1, 2]}
    assert event.payload["reason"] == "x"


def test_an_unknown_event_type_is_refused(store):
    with pytest.raises(InvalidEventError):
        append(store, "made_up")
    assert store.events() == ()


def test_a_payload_must_be_a_json_mapping(store):
    with pytest.raises(InvalidEventError):
        append(store, EventType.DECISION, payload=["not", "a", "mapping"])
    with pytest.raises(InvalidEventError):
        append(store, EventType.DECISION, payload={"x": object()})


def test_run_and_project_ids_are_required(store):
    with pytest.raises(InvalidEventError):
        append(store, EventType.RUN_STARTED, run_id="")
    with pytest.raises(InvalidEventError):
        append(store, EventType.RUN_STARTED, project_id=None)


def test_an_attempt_names_itself(store):
    started = append(store, EventType.ATTEMPT_STARTED, task_id="t1")

    assert started.attempt_id == started.event_id


def test_attempt_started_cannot_borrow_another_attempt_id(store):
    with pytest.raises(InvalidEventError):
        append(store, EventType.ATTEMPT_STARTED, task_id="t1", attempt_id="other")


def test_attempt_started_requires_a_task(store):
    with pytest.raises(InvalidEventError):
        append(store, EventType.ATTEMPT_STARTED)


@pytest.mark.parametrize(
    "event_type",
    [EventType.ATTEMPT_FINISHED, EventType.VERIFICATION, EventType.ATTEMPT_INTERRUPTED,
     EventType.ATTEMPT_PROCESS, EventType.INTEGRATION],
)
def test_attempt_outcomes_must_name_their_attempt_and_task(store, event_type):
    with pytest.raises(InvalidEventError):
        append(store, event_type, task_id="t1")
    with pytest.raises(InvalidEventError):
        append(store, event_type, attempt_id="a1")


def test_events_can_be_filtered(store):
    a = append(store, EventType.ATTEMPT_STARTED, task_id="t1", session_id="s1")
    append(store, EventType.ATTEMPT_FINISHED, task_id="t1", attempt_id=a.attempt_id,
           session_id="s1")
    append(store, EventType.ATTEMPT_STARTED, task_id="t2", session_id="s2", run_id="r2")
    append(store, EventType.RUN_STARTED, project_id="beta")

    assert len(store.events(project_id="alpha")) == 3
    assert len(store.events(session_id="s1")) == 2
    assert len(store.events(task_id="t2")) == 1
    assert len(store.events(run_id="r2")) == 1
    assert len(store.events(attempt_id=a.attempt_id)) == 2
    assert len(store.events(types=[EventType.ATTEMPT_STARTED])) == 2
    assert store.events(types=[]) == ()


def test_last_keeps_the_newest_events_in_recording_order(store):
    for n in range(6):
        append(store, EventType.DECISION, payload={"n": n})

    assert [e.payload["n"] for e in store.events(last=3)] == [3, 4, 5]
    assert store.events(last=0) == ()


def test_an_oversized_string_is_replaced_by_a_fingerprint(store):
    huge = "x" * (MAX_PAYLOAD_BYTES + 10)
    event = append(store, EventType.ATTEMPT_FINISHED, task_id="t1", attempt_id="a",
                   payload={"status": "success", "stdout": huge})

    payload = event.payload
    assert payload["status"] == "success"
    assert payload["stdout"]["truncated"] is True
    assert payload["stdout"]["original_bytes"] == len(huge)
    assert len(payload["stdout"]["sha256"]) == 64
    assert len(event.payload_json.encode()) <= MAX_PAYLOAD_BYTES


def test_a_payload_too_big_even_after_cutting_strings_is_fingerprinted(store):
    many = {f"k{n}": "y" * 1000 for n in range(400)}
    event = append(store, EventType.DECISION, payload=many)

    assert event.payload["truncated"] is True
    assert len(event.payload["keys"]) == 400


# --- SQLite specifics -----------------------------------------------------


def test_sqlite_history_survives_reopening(tmp_path):
    path = tmp_path / "h.sqlite"
    first = SQLiteHistoryStore(path)
    append(first, EventType.RUN_STARTED, payload={"request": "go"})
    first.close()

    again = SQLiteHistoryStore(path)
    assert [e.payload for e in again.events()] == [{"request": "go"}]


def test_sqlite_refuses_update_and_delete(tmp_path):
    path = tmp_path / "h.sqlite"
    store = SQLiteHistoryStore(path)
    append(store, EventType.RUN_STARTED)
    store.close()

    raw = sqlite3.connect(str(path))
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        raw.execute("UPDATE events SET type = 'decision'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        raw.execute("DELETE FROM events")
    raw.close()


def test_sqlite_creates_its_directory(tmp_path):
    SQLiteHistoryStore(tmp_path / "deep" / "er" / "h.sqlite")

    assert (tmp_path / "deep" / "er" / "h.sqlite").is_file()


def test_the_store_has_no_update_or_delete_method(store):
    assert not any(hasattr(store, name) for name in ("update", "delete", "remove"))


def test_only_the_sqlite_store_imports_sqlite3():
    importers = []
    for path in sorted(CORE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            if any(name.split(".")[0] == "sqlite3" for name in names):
                importers.append(path.name)

    assert sorted(set(importers)) == ["sqlite_history.py"]


# --- caller-chosen attempt ids --------------------------------------------


def test_attempt_started_may_carry_the_orchestrators_attempt_id(store):
    chosen = "0123456789abcdef0123456789abcdef"

    started = append(store, EventType.ATTEMPT_STARTED, task_id="t1", event_id=chosen)

    assert started.event_id == started.attempt_id == chosen


def test_only_attempt_started_may_choose_its_id(store):
    with pytest.raises(InvalidEventError):
        append(store, EventType.DECISION, event_id="0123456789abcdef0123456789abcdef")


@pytest.mark.parametrize("bad", ["short", "0123456789ABCDEF0123456789ABCDEF", 7, "../x"])
def test_a_malformed_attempt_id_is_refused(store, bad):
    with pytest.raises(InvalidEventError):
        append(store, EventType.ATTEMPT_STARTED, task_id="t1", event_id=bad)


def test_a_duplicate_attempt_id_is_refused(store):
    chosen = "0123456789abcdef0123456789abcdef"
    append(store, EventType.ATTEMPT_STARTED, task_id="t1", event_id=chosen)

    with pytest.raises(InvalidEventError):
        append(store, EventType.ATTEMPT_STARTED, task_id="t1", event_id=chosen)
    assert len(store.events()) == 1


# --- schema v2 migration ---------------------------------------------------

V1_SCHEMA = """
CREATE TABLE events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL, session_id TEXT, project_id TEXT NOT NULL, task_id TEXT,
    attempt_id TEXT,
    type TEXT NOT NULL CHECK (type IN ('run_started', 'decision', 'operation_result',
        'attempt_started', 'attempt_finished', 'verification', 'attempt_interrupted',
        'run_stopped', 'run_error', 'run_interrupted')),
    payload TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX ix_events_task ON events(project_id, task_id, seq);
CREATE INDEX ix_events_session ON events(session_id, seq);
CREATE INDEX ix_events_attempt ON events(attempt_id);
CREATE TRIGGER events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'history is append-only'); END;
CREATE TRIGGER events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'history is append-only'); END;
CREATE TABLE schema_meta (version INTEGER NOT NULL);
INSERT INTO schema_meta (version) VALUES (1);
"""


def make_v1(path, rows=3):
    raw = sqlite3.connect(str(path))
    raw.executescript(V1_SCHEMA)
    for n in range(rows):
        raw.execute(
            "INSERT INTO events (event_id, run_id, project_id, type, payload, created_at)"
            " VALUES (?, 'r1', 'alpha', 'decision', ?, '2026-10-01T00:00:00+00:00')",
            (f"{n:032x}", f'{{"n": {n}}}'),
        )
    raw.commit()
    raw.close()


def version_of(path):
    raw = sqlite3.connect(str(path))
    try:
        return raw.execute("SELECT version FROM schema_meta").fetchone()[0]
    finally:
        raw.close()


def test_a_v1_database_is_backed_up_then_migrated(tmp_path):
    path = tmp_path / "history.sqlite"
    make_v1(path)

    store = SQLiteHistoryStore(path)

    assert version_of(path) == SCHEMA_VERSION == 3
    assert [e.payload["n"] for e in store.events()] == [0, 1, 2]
    assert [e.seq for e in store.events()] == [1, 2, 3]
    # The new types fit, and numbering continues after the old rows.
    new = store.append(type=EventType.ATTEMPT_PROCESS, run_id="r2", project_id="alpha",
                       task_id="t1", attempt_id="a" * 32, payload={"pid": 1})
    assert new.seq == 4
    # The copy is the untouched v1 database.
    backup = store.migration_backup
    assert backup.name.startswith("history.sqlite.bak-v1-")
    assert backup.parent == path.parent
    assert version_of(backup) == 1
    store.close()


def test_the_migrated_table_is_still_append_only(tmp_path):
    path = tmp_path / "history.sqlite"
    make_v1(path)
    SQLiteHistoryStore(path).close()

    raw = sqlite3.connect(str(path))
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        raw.execute("UPDATE events SET type = 'decision'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        raw.execute("DELETE FROM events")
    with pytest.raises(sqlite3.IntegrityError):
        raw.execute("INSERT INTO events (event_id, run_id, project_id, type, payload,"
                    " created_at) VALUES ('x', 'r', 'p', 'made_up', '{}', 't')")
    raw.close()


def test_migration_is_refused_when_the_backup_cannot_be_made(tmp_path, monkeypatch):
    path = tmp_path / "history.sqlite"
    make_v1(path)
    monkeypatch.setattr(sqlite_history, "_backup_stamp", lambda: "FIXED")
    # Something already occupies the backup's name, so the copy cannot be made.
    (tmp_path / "history.sqlite.bak-v1-FIXED").mkdir()

    with pytest.raises(HistoryMigrationError, match="backup"):
        SQLiteHistoryStore(path)

    assert version_of(path) == 1
    raw = sqlite3.connect(str(path))
    assert raw.execute("SELECT count(*) FROM events").fetchone()[0] == 3
    raw.close()


def test_a_newer_schema_is_refused(tmp_path):
    path = tmp_path / "history.sqlite"
    SQLiteHistoryStore(path).close()
    raw = sqlite3.connect(str(path))
    raw.execute("UPDATE schema_meta SET version = 99")
    raw.commit()
    raw.close()

    with pytest.raises(RuntimeError, match="99"):
        SQLiteHistoryStore(path)


def test_a_new_database_is_created_at_the_current_version_without_a_backup(tmp_path):
    store = SQLiteHistoryStore(tmp_path / "h.sqlite")

    assert version_of(tmp_path / "h.sqlite") == SCHEMA_VERSION
    assert store.migration_backup is None
    assert list(tmp_path.glob("*.bak-*")) == []


def test_a_v2_database_is_backed_up_then_migrated_to_v3(tmp_path):
    path = tmp_path / "history.sqlite"
    make_v1(path)
    raw = sqlite3.connect(str(path))
    raw.execute("UPDATE schema_meta SET version = 2")
    raw.commit()
    raw.close()

    store = SQLiteHistoryStore(path)

    assert version_of(path) == 3
    assert store.migration_backup.name.startswith("history.sqlite.bak-v2-")
    event = store.append(type=EventType.HUMAN_ACTION, run_id="r", project_id="alpha",
                         task_id="t1", payload={"action": "set_description"})
    assert event.seq == 4
