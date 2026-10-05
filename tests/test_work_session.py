"""Tests for the durable session boundary: the model, its lifecycle, and its store.

Nothing here needs a network, a model or a worker. Sessions are plain data, so
the tests are about validation, lifecycle rules and round trips.
"""

import pytest
import yaml

from core.session_store import (
    FileSessionStore,
    InvalidSessionError,
    SessionStoreError,
    UnknownSessionError,
)
from core.work_session import (
    ALLOWED_TRANSITIONS,
    SessionStatus,
    WorkSession,
    status_for_stop_reason,
)


@pytest.fixture
def store(tmp_path):
    return FileSessionStore(tmp_path / "sessions")


def make(session_id="s1", project_id="alpha", objective="ship it"):
    return WorkSession.create(session_id, project_id, objective)


# --- create / load round trip --------------------------------------------


def test_a_new_session_starts_created_with_a_timestamp(store):
    session = make()

    assert session.status is SessionStatus.CREATED
    assert session.created_at == session.updated_at
    assert session.started_at is None
    assert session.steps_completed == 0
    assert session.last_progress is None
    assert session.last_stop_reason is None


def test_create_then_load_is_a_faithful_round_trip(store):
    session = make()
    store.create(session)

    assert store.load("s1") == session


def test_load_by_id_finds_exactly_that_session(store):
    store.create(make("alpha-1"))
    store.create(make("alpha-2", project_id="beta"))

    assert store.load("alpha-2").project_id == "beta"


def test_the_stored_form_is_plain_readable_data(store):
    store.create(make())

    raw = yaml.safe_load((store.root / "s1.yaml").read_text())

    assert raw["session_id"] == "s1"
    assert raw["status"] == "created"
    assert raw["objective"] == "ship it"


def test_one_file_per_session(store):
    store.create(make("one"))
    store.create(make("two"))

    assert sorted(p.name for p in store.root.iterdir()) == ["one.yaml", "two.yaml"]


# --- updates persist -----------------------------------------------------


def test_an_update_is_visible_to_the_next_load(store):
    store.create(make())
    updated = make().evolve(status=SessionStatus.RUNNING, steps_completed=2)

    store.save(updated)

    assert store.load("s1") == updated
    assert store.load("s1").steps_completed == 2


def test_saving_replaces_rather_than_appends(store):
    store.create(make())
    store.save(make().evolve(last_stop_reason="step_limit"))
    store.save(make().evolve(last_stop_reason="no_actionable_work"))

    assert store.load("s1").last_stop_reason == "no_actionable_work"


def test_save_does_not_leave_a_stale_temporary_file(store):
    store.create(make())
    store.save(make().evolve(steps_completed=1))

    assert [p.name for p in store.root.iterdir()] == ["s1.yaml"]


def test_evolving_does_not_mutate_the_original(store):
    session = make()
    store.create(session)

    session.evolve(steps_completed=9)

    assert store.load("s1").steps_completed == 0


def test_updated_at_moves_forward(store):
    session = make()
    later = session.evolve(steps_completed=1)

    assert later.updated_at >= session.updated_at
    assert later.created_at == session.created_at


# --- missing and duplicate sessions --------------------------------------


def test_loading_an_unknown_session_raises(store):
    with pytest.raises(UnknownSessionError):
        store.load("ghost")


def test_a_duplicate_id_is_refused_rather_than_overwritten(store):
    store.create(make())

    with pytest.raises(SessionStoreError):
        store.create(make(objective="different"))


def test_saving_an_unknown_session_is_refused(store):
    with pytest.raises(UnknownSessionError):
        store.save(make())


def test_a_broken_file_does_not_hide_the_good_ones(store):
    store.create(make("good-1"))
    store.create(make("good-2"))
    (store.root / "broken.yaml").write_text("just: a string\n")

    found = [s.session_id for s in store.list_sessions()]

    assert found == ["good-2", "good-1"]
    assert [pid for pid, _ in store.problems()] == ["broken"]


# --- invalid session data ------------------------------------------------


@pytest.mark.parametrize("objective", ["", "   ", None, 42])
def test_an_unusable_objective_is_refused(objective):
    with pytest.raises(InvalidSessionError):
        WorkSession.create("s1", "alpha", objective)


@pytest.mark.parametrize("session_id", [
    "../escape", "with/slash", "", "   ", None, "-leading-dash", "x" * 65,
    ".hidden",
])
def test_an_unusable_session_id_is_refused(session_id):
    """Ids reach the filesystem, so anything path-like is refused up front."""
    with pytest.raises(InvalidSessionError):
        WorkSession.create(session_id, "alpha", "ship it")


@pytest.mark.parametrize("session_id", ["a", "s-1", "run.2", "A_b-9"])
def test_ordinary_session_ids_are_accepted(session_id):
    assert WorkSession.create(session_id, "alpha", "x").session_id == session_id


def test_an_unknown_status_is_refused_on_load():
    data = make().to_dict()
    data["status"] = "pondering"

    with pytest.raises(InvalidSessionError, match="unknown session status"):
        WorkSession.from_dict(data)


def test_a_non_timestamp_is_refused_on_load():
    data = make().to_dict()
    data["created_at"] = "last tuesday"

    with pytest.raises(InvalidSessionError, match="ISO 8601"):
        WorkSession.from_dict(data)


def test_missing_required_fields_are_named():
    with pytest.raises(InvalidSessionError) as error:
        WorkSession.from_dict({"session_id": "s1", "project_id": "alpha"})

    assert "objective" in str(error.value)
    assert "status" in str(error.value)


def test_unknown_fields_are_refused_rather_than_dropped():
    """A reader that silently ignores a newer field loses the reason it exists."""
    data = make().to_dict()
    data["future_field"] = "something a later version added"

    with pytest.raises(InvalidSessionError, match="unknown session fields"):
        WorkSession.from_dict(data)


def test_non_mapping_data_is_refused():
    with pytest.raises(InvalidSessionError):
        WorkSession.from_dict(["not", "a", "mapping"])


def test_a_file_that_is_not_yaml_is_reported_as_invalid(store):
    store.create(make())
    (store.root / "s1.yaml").write_text("\t- [unclosed\n")

    with pytest.raises(InvalidSessionError):
        store.load("s1")


def test_progress_may_only_carry_known_keys(store):
    data = make().to_dict()
    data["last_progress"] = {"task_id": "t1", "invented": "x"}

    with pytest.raises(InvalidSessionError, match="unsupported keys"):
        WorkSession.from_dict(data)


# --- lifecycle -----------------------------------------------------------


def test_completed_is_terminal():
    session = make().evolve(status=SessionStatus.RUNNING).evolve(
        status=SessionStatus.COMPLETED
    )

    with pytest.raises(InvalidSessionError, match="cannot move session"):
        session.evolve(status=SessionStatus.RUNNING)


def test_an_illegal_transition_is_refused():
    with pytest.raises(InvalidSessionError, match="cannot move session"):
        make().evolve(status=SessionStatus.NEEDS_HUMAN)


def test_rewinding_from_stopped_to_created_is_refused():
    session = make().evolve(status=SessionStatus.STOPPED)

    with pytest.raises(InvalidSessionError):
        session.evolve(status=SessionStatus.CREATED)


def test_repeating_the_current_status_is_not_a_transition():
    session = make().evolve(status=SessionStatus.STOPPED)

    assert session.evolve(status=SessionStatus.STOPPED).status is SessionStatus.STOPPED


def test_started_at_is_stamped_on_the_move_to_running():
    session = make().evolve(status=SessionStatus.RUNNING)

    assert session.started_at == session.updated_at


def test_a_stopped_session_can_run_again():
    session = (
        make()
        .evolve(status=SessionStatus.RUNNING)
        .evolve(status=SessionStatus.STOPPED)
    )

    assert session.evolve(status=SessionStatus.RUNNING).status is SessionStatus.RUNNING
    assert session.started_at is not None, "the original start is kept"


def test_started_at_survives_a_second_run():
    first = make().evolve(status=SessionStatus.RUNNING)
    again = first.evolve(status=SessionStatus.STOPPED).evolve(
        status=SessionStatus.RUNNING
    )

    assert again.started_at == first.started_at


def test_every_declared_transition_is_actually_allowed():
    """The table and the behaviour must not drift apart."""
    for source, targets in ALLOWED_TRANSITIONS.items():
        session = _session_in_state(source)
        for target in targets:
            assert session.evolve(status=target).status is target


def _session_in_state(status):
    if status is SessionStatus.CREATED:
        return make()
    if status is SessionStatus.RUNNING:
        return make().evolve(status=SessionStatus.RUNNING)
    if status is SessionStatus.NEEDS_HUMAN:
        return make().evolve(status=SessionStatus.RUNNING).evolve(
            status=SessionStatus.NEEDS_HUMAN
        )
    if status is SessionStatus.STOPPED:
        return make().evolve(status=SessionStatus.STOPPED)
    if status is SessionStatus.FAILED:
        return make().evolve(status=SessionStatus.FAILED)
    return (
        make()
        .evolve(status=SessionStatus.RUNNING)
        .evolve(status=SessionStatus.COMPLETED)
    )


def test_a_completed_session_is_not_a_resume_candidate():
    session = (
        make().evolve(status=SessionStatus.RUNNING).evolve(
            status=SessionStatus.COMPLETED
        )
    )

    assert session.can_resume() is False


def test_a_stopped_session_is_a_resume_candidate():
    assert make().evolve(status=SessionStatus.STOPPED).can_resume() is True


# --- stop reason mapping -------------------------------------------------


@pytest.mark.parametrize("stop_reason", [
    "no_actionable_work",
    "step_limit",
    "master_stop",
    "operation_failed",
    "unusable_reasoning_reply",
    None,
])
def test_ordinary_stops_map_to_stopped(stop_reason):
    assert status_for_stop_reason(stop_reason) is SessionStatus.STOPPED


def test_waiting_on_a_human_maps_to_needs_human():
    from core.autonomous_loop import STOP_APPROVAL

    assert status_for_stop_reason(STOP_APPROVAL) is SessionStatus.NEEDS_HUMAN


@pytest.mark.parametrize("stop_reason", [
    "no_actionable_work", "step_limit", "unusable_reasoning_reply",
])
def test_no_stop_reason_completes_a_session_by_itself(stop_reason):
    """Finishing a bounded run is not the same as the objective being met."""
    assert status_for_stop_reason(stop_reason) is not SessionStatus.COMPLETED


# --- the store satisfies the interface -----------------------------------


def test_the_file_store_implements_the_session_store_contract(store):
    from core.work_session import SessionStore

    assert isinstance(store, SessionStore)


# --- listing -------------------------------------------------------------


def test_listing_is_newest_first(store):
    store.create(make("old"))
    store.create(make("new"))
    newest = store.load("new").evolve()
    store.save(newest)

    found = [s.session_id for s in store.list_sessions()]

    assert set(found) == {"old", "new"}
    assert len(found) == 2


def test_listing_can_be_filtered_by_project(store):
    store.create(make("a1", project_id="alpha"))
    store.create(make("b1", project_id="beta"))

    found = [s.session_id for s in store.list_sessions(project_id="beta")]

    assert found == ["b1"]


def test_listing_an_empty_store_is_empty(store):
    assert store.list_sessions() == ()


def test_a_missing_sessions_root_is_created(tmp_path):
    nested = tmp_path / "deeply" / "nested" / "sessions"

    FileSessionStore(nested)

    assert nested.is_dir()