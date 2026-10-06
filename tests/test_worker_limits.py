"""Worker limits: recognised, waited for when short, put to the owner otherwise."""

import io
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import yaml

from core.daemon import Daemon, failed_attempts_since_human
from core.history import EventType, InMemoryHistoryStore
from core.master import Master
from core.ms import main as ms_main
from core.notify import Notifier
from core.paths import RuntimePaths
from core.run_config import load_config
from core.summary import build_summary, render_text
from core.worker_limits import (LimitState, choice_profile, classify_failure, describe,
                                parse_reset)

NOW = datetime(2026, 10, 6, 2, 10, tzinfo=timezone.utc)


def error_event(message, name="APIError", **data):
    return json.dumps({"type": "error", "error": {"name": name,
                                                   "data": {"message": message, **data}}})


# --- recognising limits ---


@pytest.mark.parametrize("stdout, stderr, kind", [
    (error_event("Rate limit exceeded", statusCode=429), "", "rate_limited"),
    ("", "Error: 429 Too Many Requests", "rate_limited"),
    (error_event("You have exceeded your free usage quota"), "", "rate_limited"),
    (error_event("Model big-pickle not found"), "", "model_unavailable"),
    (error_event("Payment Required", statusCode=402), "", "model_unavailable"),
    ("", "Error: Provider not found: deepseek", "not_configured"),
    (error_event("Unexpected server error", name="UnknownError"), "", "provider_error"),
])
def test_limits_are_recognised(stdout, stderr, kind):
    assert classify_failure(stdout, stderr, 1, 0, NOW)["kind"] == kind


def test_real_failures_are_not_limits():
    assert classify_failure("", "", 0, 3) is None
    assert classify_failure("", "AssertionError: expected 3", 1, 4) is None
    # A provider error after the worker did work is not assumed to be a limit.
    assert classify_failure(error_event("Unexpected", name="UnknownError"), "", 1, 2) is None


@pytest.mark.parametrize("text, minutes", [
    ('"retry-after": "120"', 2),
    ("Retry-After: 3600", 60),
    ("Rate limit exceeded. Try again in 45 minutes.", 45),
    ("please try again in 1h30m", 90),
    ("try again in 2 hours and 5 minutes", 125),
    ("quota resets at 2026-10-06T05:10:00Z", 180),
    ("resets at 03:40 UTC", 90),
])
def test_reset_times_are_read(text, minutes):
    assert parse_reset(text, NOW) == NOW + timedelta(minutes=minutes)
    info = classify_failure(error_event(f"Rate limit exceeded; {text}"), "", 1, 0, NOW)
    assert info["reset_at"] == (NOW + timedelta(minutes=minutes)).isoformat()


def test_an_unknown_reset_is_recorded_as_unknown():
    assert parse_reset("rate limited", NOW) is None
    assert classify_failure(error_event("rate limited"), "", 1, 0, NOW)["reset_at"] == "unknown"


# --- what happens next ---


def limit(minutes=None, kind="rate_limited"):
    reset = "unknown" if minutes is None else (NOW + timedelta(minutes=minutes)).isoformat()
    return {"kind": kind, "detail": "x", "reset_at": reset}


@pytest.fixture
def clock():
    return SimpleNamespace(now=NOW)


@pytest.fixture
def limits(tmp_path, clock):
    return LimitState(tmp_path, now=lambda: clock.now)


def test_a_short_known_reset_is_waited_for_automatically(limits, clock):
    state = limits.record_limit("app", "big-pickle", limit(50))
    assert state["phase"] == "auto_wait"
    assert state["until"] == (NOW + timedelta(minutes=52)).isoformat()
    assert limits.paused("app") == "auto_wait"
    clock.now = NOW + timedelta(minutes=53)
    assert limits.paused("app") is None  # then the same worker runs again


def test_the_auto_wait_threshold_is_configurable(limits):
    assert limits.record_limit("app", "big-pickle", limit(150))["phase"] == "needs_choice"
    assert limits.record_limit("b", "big-pickle", limit(150), max_auto_wait_min=180)[
        "phase"] == "auto_wait"


def test_an_unknown_reset_is_waited_for_once_then_the_owner_is_asked(limits, clock):
    first = limits.record_limit("app", "big-pickle", limit())
    assert first["phase"] == "auto_wait"
    assert first["until"] == (NOW + timedelta(minutes=60)).isoformat()
    clock.now = NOW + timedelta(minutes=61)
    second = limits.record_limit("app", "big-pickle", limit())
    assert second["phase"] == "needs_choice" and second["since"] == NOW.isoformat()
    assert len(second["events"]) == 2 and limits.paused("app") == "needs_choice"


@pytest.mark.parametrize("kind", ["model_unavailable", "not_configured"])
def test_a_gone_model_or_missing_credential_asks_at_once(limits, kind):
    assert limits.record_limit("app", "big-pickle", limit(5, kind))["phase"] == "needs_choice"


def test_the_owners_choices(limits, clock):
    limits.record_limit("app", "big-pickle", limit(300))
    with pytest.raises(ValueError):
        limits.choose("app", "free", None)
    state = limits.choose("app", "free", "space-bunny")
    assert limits.paused("app") is None and limits.override("app") == "space-bunny"
    assert state["override_until"] == (NOW + timedelta(minutes=300)).isoformat()
    clock.now = NOW + timedelta(minutes=301)
    assert limits.override("app") is None  # back to the first worker after its reset


def test_choosing_to_wait_keeps_waiting_without_asking_again(limits, clock):
    limits.record_limit("app", "big-pickle", limit(300))
    state = limits.choose("app", "wait")
    assert state["until"] == (NOW + timedelta(minutes=302)).isoformat()
    clock.now = NOW + timedelta(minutes=303)
    again = limits.record_limit("app", "big-pickle", limit())
    assert again["phase"] == "chosen_wait"


def test_a_working_worker_clears_its_limit(limits):
    limits.record_limit("app", "big-pickle", limit(50))
    limits.record_success("app", "space-bunny")
    assert limits.get("app")["phase"] == "auto_wait"
    limits.record_success("app", "big-pickle")
    assert limits.paused("app") is None and limits.get("app")["history"]
    with pytest.raises(ValueError, match="no worker limit"):
        limits.choose("app", "wait")


def test_choice_profiles_and_descriptions(limits):
    profiles = {"big-pickle": {}, "space-bunny": {}, "deepseek-flash": {"paid": True}}
    order = ["big-pickle", "space-bunny", "deepseek-flash"]
    assert choice_profile("free", order, profiles, "big-pickle") == "space-bunny"
    assert choice_profile("free", order, profiles, "space-bunny") == "big-pickle"
    assert choice_profile("paid", order, profiles, "big-pickle") == "deepseek-flash"
    assert choice_profile("free", ["big-pickle"], {"big-pickle": {}}, "big-pickle") is None
    state = limits.record_limit("app", "big-pickle", limit(300))
    line = describe("app", state, {"big-pickle": "Big Pickle"})
    assert line.startswith("Big Pickle is rate-limited until ~")
    assert line.endswith("Choose: ms limit app wait | free | paid")
    assert describe("app", {}) is None


# --- the orchestrator ---


class LimitedWorker:
    def __init__(self, limited=True):
        self.limited = limited

    def execute(self, task, context, workspace=None):
        from core.execution import ExecutionResult

        artifacts = {"summary": "", "model": "opencode/big-pickle"}
        if self.limited:
            artifacts["limit"] = limit(50)
        return ExecutionResult(status="failed" if self.limited else "success",
                               artifacts=artifacts)


class Verifier:
    calls = 0

    def verify(self, task, context, evidence=None, workspace=None):
        from core.verification import VerificationResult

        Verifier.calls += 1
        return VerificationResult(verdict="fail", summary="")


def orchestrator(tmp_path, limits, worker):
    from test_task_types import typed_project

    from core.task_orchestrator import TaskOrchestrator
    from core.worker_tiers import TierSet

    typed_project(tmp_path)
    tiers = TierSet((), {"big-pickle": worker, "space-bunny": worker},
                    {"big-pickle": {}, "space-bunny": {}},
                    {"big-pickle": "opencode/big-pickle", "space-bunny": "opencode/space"},
                    workers=("big-pickle", "space-bunny"), paid={}, labels={})
    history = InMemoryHistoryStore()
    return TaskOrchestrator(Master(tmp_path / "projects"), worker, Verifier(), history=history,
                            tiers=tiers, limits=limits), history


def test_a_limited_attempt_is_not_verified_and_not_a_failure(tmp_path, limits):
    Verifier.calls = 0
    orch, history = orchestrator(tmp_path, limits, LimitedWorker())
    result = orch.orchestrate("p", "t1", run_id="r")
    assert result["outcome"] == "limited" and result["verification"] is None
    assert Verifier.calls == 0
    [started] = history.events(types=[EventType.ATTEMPT_STARTED])
    assert started.payload["worker_profile"] == "big-pickle"
    assert started.payload["worker_choice"] == "order"
    [finished] = history.events(types=[EventType.ATTEMPT_FINISHED])
    assert finished.payload["limit"]["kind"] == "rate_limited"
    assert finished.payload["limit_decision"]["phase"] == "auto_wait"
    assert limits.paused("p") == "auto_wait"
    assert failed_attempts_since_human(history, "p", "t1") == 0
    from core.evidence import HistoryEvidence

    assert HistoryEvidence(history).attempts_in_scope("p", "t1") == 0


def test_the_owners_choice_picks_the_worker(tmp_path, limits):
    orch, history = orchestrator(tmp_path, limits, LimitedWorker(limited=False))
    limits.record_limit("p", "big-pickle", limit(300))
    limits.choose("p", "free", "space-bunny")
    orch.orchestrate("p", "t1", run_id="r")
    [started] = history.events(types=[EventType.ATTEMPT_STARTED])
    assert started.payload["worker_profile"] == "space-bunny"
    assert started.payload["worker_choice"] == "owner"


# --- the service, ms status, ms limit and the summary ---


def daemon(tmp_path, limits, notifier):
    root = tmp_path / "projects"
    (root / "app").mkdir(parents=True)
    (root / "app" / "project.yaml").write_text(yaml.safe_dump(
        {"id": "app", "name": "App", "status": "active", "auto_integrate": True}))
    (root / "app" / "milestones.yaml").write_text("milestones:\n  - {id: m, name: M, status: planned}\n")
    (root / "app" / "tasks.yaml").write_text(yaml.safe_dump({"tasks": [
        {"id": "t1", "milestone": "m", "title": "T", "status": "planned",
         "acceptance": {"commands": ["true"]}}]}))
    runs = []
    d = Daemon(master=Master(root), history=InMemoryHistoryStore(),
               config=load_config(tmp_path / "none.toml"), state_dir=tmp_path / "state",
               notifier=notifier, run=lambda r: runs.append(r) or 0, now=lambda: NOW,
               limits=limits)
    return d, runs


def test_the_service_waits_quietly_and_asks_once(tmp_path, limits, clock):
    notifier = Notifier(topic=None)
    d, runs = daemon(tmp_path, limits, notifier)
    limits.record_limit("app", "big-pickle", limit(50))
    log = d.cycle()
    assert runs == [] and notifier.sent == []
    assert any("waits until" in line and "automatic" in line for line in log)
    limits.record_limit("app", "big-pickle", limit(500))
    d.cycle()
    d.cycle()
    [sent] = notifier.sent
    assert sent["title"] == "app: needs you" and "ms limit app wait | free | paid" in \
        sent["message"]
    assert runs == []  # still paused until the owner chooses


def test_ms_limit_and_ms_status():
    state_dir = RuntimePaths.default().state_dir
    LimitState(state_dir).record_limit("sample-project", "default", limit(None,
                                                                         "model_unavailable"))
    out = io.StringIO()
    assert ms_main(["limit", "sample-project"], out=out) == 0
    assert "Choose: ms limit sample-project" in out.getvalue()
    out = io.StringIO()
    assert ms_main(["limit", "sample-project", "paid"], out=out) == 1
    assert "no paid worker profile" in out.getvalue()
    out = io.StringIO()
    assert ms_main(["limit", "sample-project", "wait"], out=out) == 0
    assert "waits until" in out.getvalue()


def test_the_summary_mentions_limits_and_workers(tmp_path):
    from test_summary import project

    master = project(tmp_path)
    history = InMemoryHistoryStore()
    for n, (profile, lim) in enumerate([("big-pickle", limit(50)), ("deepseek-flash", None),
                                        ("deepseek-flash", None), ("deepseek-flash", None)]):
        history.append(type=EventType.ATTEMPT_STARTED, run_id="r", project_id="app",
                       task_id="t-1", event_id=f"{n:032x}", payload={"worker_profile": profile})
        payload = {"outcome": "limited" if lim else "finished",
                   "artifacts": {"model": "deepseek/deepseek-flash"},
                   "worker_reported_usage": {"input_tokens": 100000, "output_tokens": 50000}}
        if lim:
            payload.update(limit=lim, limit_decision={"phase": "auto_wait",
                                                      "until": "2026-10-06T03:02:00+00:00"})
        history.append(type=EventType.ATTEMPT_FINISHED, run_id="r", project_id="app",
                       task_id="t-1", attempt_id=f"{n:032x}", payload=payload)
    summary = build_summary(master, history, "2000-01-01T00:00:00+00:00",
                            {"deepseek-flash": {"input": 0.30, "output": 1.20}},
                            labels={"big-pickle": "Big Pickle",
                                    "deepseek-flash": "DeepSeek Flash"})
    text = render_text(summary)
    assert "Big Pickle was rate-limited at" in text and "waited automatically until" in text
    assert "3 attempt(s) ran on DeepSeek Flash for $0.27" in text


def test_a_run_stops_while_its_project_waits(tmp_path):
    from core.run_cli import limit_check

    ctx = SimpleNamespace(paths=RuntimePaths(state_dir=tmp_path, worktrees_root=tmp_path / "w"))
    assert limit_check(ctx, "app", lambda: None)() is None
    assert limit_check(ctx, "app", lambda: "budget")() == "budget"
    LimitState(tmp_path).record_limit("app", "big-pickle", limit(None, "model_unavailable"))
    reason = limit_check(ctx, "app")()
    assert "big-pickle is limited (model_unavailable)" in reason and "owner's choice" in reason
    assert limit_check(ctx, None)() is None


def test_the_report_shows_the_worker_and_the_limit():
    from core.report import build_report, render_report

    history = InMemoryHistoryStore()
    history.append(type=EventType.ATTEMPT_STARTED, run_id="r", session_id="s", project_id="app",
                   task_id="t-1", event_id="a" * 32, payload={"worker_profile": "big-pickle"})
    history.append(type=EventType.ATTEMPT_FINISHED, run_id="r", session_id="s",
                   project_id="app", task_id="t-1", attempt_id="a" * 32,
                   payload={"outcome": "limited", "limit": limit(50)})
    text = render_report(build_report(history, "s"))
    assert "limited" in text and "worker big-pickle" in text
    assert "worker limit: rate_limited" in text and "not counted as a failure" in text
