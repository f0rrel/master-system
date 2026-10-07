"""The owner's worker switch: ms worker, the pin, the priority, the fallback."""

import io
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

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

from test_worker_limits import (NOW, LimitedWorker, Verifier, clock, limit,  # noqa: E402,F401
                                limits)


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


# --- 3. an override whose original worker is still limited stays, with one notice ---

from core.notify import Notifier  # noqa: E402
from test_worker_limits import daemon  # noqa: E402,F401


def override_for_a_gone_model(limits):
    limits.record_limit("app", "big-pickle", limit(None, "model_unavailable"))
    return limits.choose("app", "free", "space-bunny")


def test_an_override_stays_after_its_time_while_the_original_has_no_end_in_sight(limits, clock):
    state = override_for_a_gone_model(limits)
    clock.now = NOW + timedelta(hours=23)
    assert limits.override_info("app") == {"profile": "space-bunny", "for": "big-pickle",
                                           "until": state["override_until"], "kept": False,
                                           "source": "owner"}
    clock.now = NOW + timedelta(hours=25)
    assert limits.override("app") == "space-bunny"
    assert limits.override_info("app")["kept"] is True


def test_an_override_for_a_known_reset_ends_at_the_reset(limits, clock):
    limits.record_limit("app", "big-pickle", limit(300))
    limits.choose("app", "free", "space-bunny")
    clock.now = NOW + timedelta(minutes=301)
    assert limits.override("app") is None


def test_an_override_from_before_this_change_still_ends(limits, clock):
    state = override_for_a_gone_model(limits)
    for key in ("override_kind", "override_reset"):
        state.pop(key, None)
    limits._save("app", state)
    clock.now = NOW + timedelta(hours=25)
    assert limits.override("app") is None


def test_the_original_working_again_ends_the_override(limits, clock):
    override_for_a_gone_model(limits)
    clock.now = NOW + timedelta(hours=25)
    limits.record_success("app", "big-pickle")
    assert limits.override("app") is None


def test_a_kept_override_is_described_and_the_service_says_so_once(tmp_path, limits, clock):
    from core.worker_limits import describe

    notifier = Notifier(topic=None)
    d, runs = daemon(tmp_path, limits, notifier)
    override_for_a_gone_model(limits)
    d.cycle()
    assert notifier.sent == []  # the override still has time
    clock.now = NOW + timedelta(hours=25)
    d.cycle()
    d.cycle()
    [sent] = notifier.sent
    assert sent["title"] == "app: staying on space-bunny"
    assert "big-pickle is still limited (model_unavailable)" in sent["message"]
    assert "ms worker app auto" in sent["message"]
    line = describe("app", limits.get("app"), now=clock.now)
    assert "space-bunny instead of big-pickle" in line and "still limited" in line


def test_auto_ends_a_kept_override(switch):
    clock = SimpleNamespace(now=datetime.now(timezone.utc) - timedelta(days=2))
    limits = LimitState(RuntimePaths.default().state_dir, now=lambda: clock.now)
    limits.record_limit(PROJECT, "free-a", {"kind": "model_unavailable", "reset_at": "unknown"})
    limits.choose(PROJECT, "free", "free-b")
    assert switch["limits"].override(PROJECT) == "free-b"  # kept: its day is over
    assert "Free B (temporary" in run_worker(switch)[1]
    code, out = run_worker(switch, "auto")
    assert code == 0 and "Free A (order)" in out
    assert switch["limits"].override(PROJECT) is None


def test_pinning_the_limited_worker_after_a_passing_probe_ends_the_override(switch):
    clock = SimpleNamespace(now=datetime.now(timezone.utc) - timedelta(days=2))
    limits = LimitState(RuntimePaths.default().state_dir, now=lambda: clock.now)
    limits.record_limit(PROJECT, "free-a", {"kind": "model_unavailable", "reset_at": "unknown"})
    limits.choose(PROJECT, "free", "free-b")
    code, out = run_worker(switch, "free-a")
    assert code == 0, out
    assert switch["limits"].override(PROJECT) is None
    assert "Active: Free A (pinned)" in run_worker(switch)[1]


# --- 4. [worker] auto_paid_fallback ---

import yaml  # noqa: E402

from core.master import Master  # noqa: E402
from core.run_config import ConfigError, load_config  # noqa: E402


def fallback_config(tmp_path, on=True, workers=("free-a", "free-b", "paid-c")):
    path = tmp_path / "fallback.toml"
    path.write_text(CONFIG.replace(
        'workers = ["free-a", "free-b", "paid-c"]',
        f"workers = {json.dumps(list(workers))}\nauto_paid_fallback = {str(on).lower()}"))
    return load_config(path)


def test_the_fallback_is_off_by_default_and_must_be_a_boolean(tmp_path):
    assert load_config(tmp_path / "none.toml").worker.auto_paid_fallback is False
    assert fallback_config(tmp_path, on=True).worker.auto_paid_fallback is True
    bad = tmp_path / "bad.toml"
    bad.write_text('[worker]\nauto_paid_fallback = "yes"\n')
    with pytest.raises(ConfigError, match="auto_paid_fallback must be true or false"):
        load_config(bad)


def fallback_daemon(tmp_path, limits, config, notifier):
    root = tmp_path / "projects"
    (root / "app").mkdir(parents=True)
    (root / "app" / "project.yaml").write_text(yaml.safe_dump(
        {"id": "app", "name": "App", "status": "active", "auto_integrate": True}))
    (root / "app" / "milestones.yaml").write_text(
        "milestones:\n  - {id: m, name: M, status: planned}\n")
    (root / "app" / "tasks.yaml").write_text(yaml.safe_dump({"tasks": [
        {"id": "t1", "milestone": "m", "title": "T", "status": "planned",
         "acceptance": {"commands": ["true"]}}]}))
    runs = []
    d = Daemon(master=Master(root), history=InMemoryHistoryStore(), config=config,
               state_dir=tmp_path / "state", notifier=notifier,
               run=lambda r: runs.append(r) or 0, now=lambda: NOW, limits=limits)
    return d, runs


ORDER = ["free-a", "free-b", "paid-c"]


def test_without_the_switch_a_long_limit_still_asks(tmp_path, limits):
    notifier = Notifier(topic=None)
    config = fallback_config(tmp_path, on=False, workers=ORDER)
    d, runs = fallback_daemon(tmp_path, limits, config, notifier)
    limits.record_limit("app", "free-a", limit(500))
    d.cycle()
    assert runs == [] and limits.override("app") is None
    assert notifier.sent[0]["title"] == "app: needs you"


def test_a_long_limit_of_a_free_worker_switches_to_the_paid_one_and_says_so(tmp_path, limits):
    notifier = Notifier(topic=None)
    d, runs = fallback_daemon(tmp_path, limits, fallback_config(tmp_path, workers=ORDER),
                              notifier)
    limits.record_limit("app", "free-a", limit(500))
    d.cycle()
    d.cycle()
    assert limits.override_info("app")["profile"] == "paid-c"
    assert limits.paused("app") is None and len(runs) == 1  # it ran at once, did not ask
    [sent] = notifier.sent
    assert sent["title"] == "app: switched to Paid C"
    assert "Free A is rate-limited" in sent["message"] and "daily" in sent["message"]
    assert "ms worker app free-a" in sent["message"]


def test_a_short_limit_is_still_waited_for(tmp_path, limits):
    notifier = Notifier(topic=None)
    d, runs = fallback_daemon(tmp_path, limits, fallback_config(tmp_path, workers=ORDER),
                              notifier)
    limits.record_limit("app", "free-a", limit(50))
    d.cycle()
    assert runs == [] and limits.override("app") is None and notifier.sent == []


def test_a_limited_paid_worker_still_asks(tmp_path, limits):
    notifier = Notifier(topic=None)
    d, runs = fallback_daemon(tmp_path, limits, fallback_config(tmp_path, workers=ORDER),
                              notifier)
    limits.record_limit("app", "paid-c", limit(500))
    d.cycle()
    assert runs == [] and notifier.sent[0]["title"] == "app: needs you"


def test_without_a_paid_profile_it_asks(tmp_path, limits):
    notifier = Notifier(topic=None)
    path = tmp_path / "free-only.toml"
    path.write_text('[worker]\nauto_paid_fallback = true\nworkers = ["free-a", "free-b"]\n'
                    '[worker.profiles.free-a]\nmodel = "acme/free-1"\n'
                    '[worker.profiles.free-b]\nmodel = "acme/free-2"\n')
    d, runs = fallback_daemon(tmp_path, limits, load_config(path), notifier)
    limits.record_limit("app", "free-a", limit(500))
    d.cycle()
    assert runs == [] and notifier.sent[0]["title"] == "app: needs you"


def test_the_daily_cap_still_stops_everything(tmp_path, limits):
    notifier = Notifier(topic=None)
    d, runs = fallback_daemon(tmp_path, limits, fallback_config(tmp_path, workers=ORDER),
                              notifier)
    d.spent_today = lambda: 99
    limits.record_limit("app", "free-a", limit(500))
    d.cycle()
    assert runs == [] and limits.override("app") is None


def test_an_automatic_switch_is_recorded_as_a_fallback(tmp_path, limits):
    orch, history = three_workers(tmp_path, limits, LimitedWorker(limited=False))
    limits.record_limit("p", "big-pickle", limit(500))
    limits.choose("p", "paid", "deepseek-flash", source="auto")
    orch.orchestrate("p", "t1", run_id="r")
    assert started(history) == ("deepseek-flash", "fallback")
