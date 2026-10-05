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
from core.sqlite_history import SQLiteHistoryStore

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
    [EventType.ATTEMPT_FINISHED, EventType.VERIFICATION, EventType.ATTEMPT_INTERRUPTED],
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
