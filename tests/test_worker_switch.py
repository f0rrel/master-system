"""The owner's worker switch: ms worker, the pin, the priority, the fallback."""

import io

import pytest

from core.daemon import Daemon
from core.history import EventType, InMemoryHistoryStore
from core.ms import main as ms_main
from core.paths import RuntimePaths
from core.sqlite_history import SQLiteHistoryStore
from core.worker_limits import LimitState
from test_worker_probe import CONFIG, FakeOpenCode, events, reply

PROJECT = "sample-project"


@pytest.fixture
def switch(tmp_path, monkeypatch):
    """Three worker profiles (two free, one paid) and a fake OpenCode for the probe."""
    import core.worker_process

    config = tmp_path / "config.toml"
    config.write_text(CONFIG)
    fake = FakeOpenCode()
    monkeypatch.setattr(core.worker_process, "run_process", fake)
    state_dir = RuntimePaths.default().state_dir
    return {"config": str(config), "fake": fake, "limits": LimitState(state_dir),
            "history": SQLiteHistoryStore(RuntimePaths.default().history_path)}


def run_worker(switch, *argv, actor=None):
    out = io.StringIO()
    prefix = ["--config", switch["config"]] + (["--actor", actor] if actor else [])
    code = ms_main([*prefix, "worker", PROJECT, *argv], out=out)
    return code, out.getvalue()


def human_actions(switch, action):
    return [e.payload for e in switch["history"].events(
        project_id=PROJECT, types=[EventType.HUMAN_ACTION]) if e.payload.get("action") == action]


# --- 1. ms worker <project> [profile | auto] ---


def test_ms_worker_shows_the_order_the_active_worker_and_why(switch):
    code, out = run_worker(switch)
    assert code == 0, out
    assert "Free A, Free B, Paid C" in out
    assert "Active: Free A (order)" in out
    assert switch["fake"].calls == []  # showing costs nothing


def test_pinning_probes_the_profile_then_pins_it_and_records_a_human_action(switch):
    code, out = run_worker(switch, "free-b", actor="telegram")
    assert code == 0, out
    assert "Free B answered a test request" in out and "Free B is pinned" in out
    [call] = switch["fake"].calls
    assert call["argv"][call["argv"].index("--model") + 1] == "acme/free-2"
    assert switch["limits"].pin(PROJECT) == "free-b"
    [probe] = human_actions(switch, "worker_probe")
    assert probe["ok"] is True and probe["choice"] == "pin" and probe["profile"] == "free-b"
    [pinned] = human_actions(switch, "worker_pin")
    assert pinned["profile"] == "free-b" and pinned["actor"] == "telegram"
    code, out = run_worker(switch)
    assert "Active: Free B (pinned)" in out


def test_an_unknown_profile_is_refused_without_a_probe(switch):
    code, out = run_worker(switch, "nope")
    assert code == 1
    assert "unknown worker profile 'nope'" in out and "free-a, free-b, paid-c" in out
    assert switch["fake"].calls == [] and switch["limits"].pin(PROJECT) is None
    assert human_actions(switch, "worker_pin") == []


def test_a_failed_probe_does_not_pin_and_says_why(switch):
    switch["fake"].stdout = events({"type": "error", "error": {"data": {
        "message": "Rate limit exceeded", "statusCode": 429}}})
    switch["fake"].returncode = 1
    code, out = run_worker(switch, "free-b")
    assert code == 1
    assert "Free B failed a test request (rate_limited)" in out and "Not pinned" in out
    assert switch["limits"].pin(PROJECT) is None
    assert human_actions(switch, "worker_pin") == []
    [probe] = human_actions(switch, "worker_probe")
    assert probe["ok"] is False and probe["kind"] == "rate_limited"


def test_a_wrong_answer_does_not_pin_either(switch):
    switch["fake"].stdout = reply("no idea")
    code, out = run_worker(switch, "paid-c")
    assert code == 1 and "did not give the expected answer" in out
    assert switch["limits"].pin(PROJECT) is None


def test_auto_removes_the_pin_without_a_probe(switch):
    run_worker(switch, "free-b")
    switch["fake"].calls.clear()
    code, out = run_worker(switch, "auto")
    assert code == 0, out
    assert "no longer pinned" in out and "Free A" in out
    assert switch["limits"].pin(PROJECT) is None and switch["fake"].calls == []
    [unpinned] = human_actions(switch, "worker_unpin")
    assert unpinned["previous"] == "free-b"
    assert "Active: Free A (order)" in run_worker(switch)[1]


def test_the_pin_survives_limit_state_changes(switch):
    run_worker(switch, "free-b")
    switch["limits"].record_limit(PROJECT, "free-b", {"kind": "rate_limited",
                                                      "reset_at": "unknown"})
    switch["limits"].record_success(PROJECT, "free-b")
    assert switch["limits"].pin(PROJECT) == "free-b"


def test_a_pin_counts_as_a_human_action_for_the_stall(switch):
    daemon = Daemon(master=None, history=switch["history"], config=None,
                    state_dir=RuntimePaths.default().state_dir, notifier=None, run=None)
    before = daemon._human_mark(PROJECT)
    run_worker(switch, "free-b")
    after_pin = daemon._human_mark(PROJECT)
    assert after_pin > before
    run_worker(switch, "auto")
    assert daemon._human_mark(PROJECT) > after_pin


# --- 2. the priority: pin, temporary override, configured order ---

from datetime import timedelta  # noqa: E402

from test_worker_limits import NOW, LimitedWorker, Verifier, clock, limit, limits  # noqa: E402,F401


def three_workers(tmp_path, limits, worker):
    from test_task_types import typed_project

    from core.master import Master
    from core.task_orchestrator import TaskOrchestrator
    from core.worker_tiers import TierSet

    typed_project(tmp_path)
    names = ("big-pickle", "space-bunny", "deepseek-flash")
    tiers = TierSet((), {n: worker for n in names}, {n: {} for n in names},
                    {n: f"acme/{n}" for n in names}, workers=names,
                    paid={"deepseek-flash": True}, labels={})
    history = InMemoryHistoryStore()
    return TaskOrchestrator(Master(tmp_path / "projects"), worker, Verifier(), history=history,
                            tiers=tiers, limits=limits), history


def started(history):
    [event] = history.events(types=[EventType.ATTEMPT_STARTED])
    return event.payload["worker_profile"], event.payload["worker_choice"]


def test_the_pin_comes_before_the_configured_order(tmp_path, limits):
    orch, history = three_workers(tmp_path, limits, LimitedWorker(limited=False))
    limits.set_pin("p", "space-bunny")
    orch.orchestrate("p", "t1", run_id="r")
    assert started(history) == ("space-bunny", "pinned")


def test_the_pin_comes_before_a_temporary_override_for_another_worker(tmp_path, limits):
    orch, history = three_workers(tmp_path, limits, LimitedWorker(limited=False))
    limits.record_limit("p", "big-pickle", limit(300))
    limits.choose("p", "paid", "deepseek-flash")  # big-pickle's limit: use the paid worker
    limits.set_pin("p", "space-bunny")
    orch.orchestrate("p", "t1", run_id="r")
    assert started(history) == ("space-bunny", "pinned")


def test_the_override_comes_before_the_order_and_ends_with_the_limit(tmp_path, limits, clock):
    orch, history = three_workers(tmp_path, limits, LimitedWorker(limited=False))
    limits.record_limit("p", "big-pickle", limit(300))
    limits.choose("p", "free", "space-bunny")
    orch.orchestrate("p", "t1", run_id="r")
    assert started(history) == ("space-bunny", "owner")


def test_a_limited_pin_uses_the_limit_flow(tmp_path, limits):
    orch, history = three_workers(tmp_path, limits, LimitedWorker())
    limits.set_pin("p", "space-bunny")
    result = orch.orchestrate("p", "t1", run_id="r")
    assert result["outcome"] == "limited" and limits.paused("p") == "auto_wait"
    assert limits.get("p")["profile"] == "space-bunny"
    assert limits.pin("p") == "space-bunny"  # the limit does not unpin


def test_the_owner_may_bypass_a_limited_pin_until_the_reset(tmp_path, limits, clock):
    orch, history = three_workers(tmp_path, limits, LimitedWorker(limited=False))
    limits.set_pin("p", "space-bunny")
    limits.record_limit("p", "space-bunny", limit(300))
    limits.choose("p", "paid", "deepseek-flash")
    orch.orchestrate("p", "t1", run_id="r")
    assert started(history) == ("deepseek-flash", "owner")
    clock.now = NOW + timedelta(minutes=301)
    assert limits.override("p") is None  # the pin applies again


def test_ms_worker_shows_a_temporary_override_and_a_pin_that_outranks_it(switch):
    switch["limits"].record_limit(PROJECT, "free-a", {"kind": "rate_limited",
                                                      "reset_at": "unknown"})
    switch["limits"].record_limit(PROJECT, "free-a", {"kind": "model_unavailable",
                                                      "reset_at": "unknown"})
    switch["limits"].choose(PROJECT, "free", "free-b")
    until = switch["limits"].override_info(PROJECT)["until"]
    from core.worker_limits import _local_time

    code, out = run_worker(switch)
    assert f"Active: Free B (temporary until {_local_time(until)})" in out
    run_worker(switch, "paid-c")
    assert "Active: Paid C (pinned)" in run_worker(switch)[1]
