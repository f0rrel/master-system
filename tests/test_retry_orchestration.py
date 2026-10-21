"""Retries end to end: what the next attempt is told, what history keeps, when the loop stops.

Uses the real orchestrator and loop with fake workers and verifiers; no model is called.
"""

import sys

sys.path.insert(0, ".")

from core.autonomous_loop import STOP_ATTEMPT_LIMIT
from core.execution import ExecutionBackend, ExecutionResult
from core.history import EventType, InMemoryHistoryStore
from core.master import Master
from core.task_orchestrator import TaskOrchestrator
from core.verification import VerificationBackend, VerificationResult
from test_evidence import Backend, Verifier, loop_for, run_task, status_of
from test_task_orchestrator import make_project

SECRET = "sk-" + "a1b2c3d4e5f6a7b8c9d0e1f2"
PROTECTED = {"kind": "protected_path", "path": "tests/test_levels.py"}


class RecordingWorker(ExecutionBackend):
    def __init__(self, artifacts=None, reason="ok"):
        self.briefings = []
        self._artifacts = artifacts or {}
        self._reason = reason

    def execute(self, task, context, workspace=None):
        self.briefings.append(dict(context.get("briefing") or {}))
        return ExecutionResult(status="success", reason=self._reason,
                               artifacts=dict(self._artifacts))


class FailingVerifier(VerificationBackend):
    def __init__(self, findings=(PROTECTED,), summary="1 protected path(s) changed"):
        self._findings, self._summary = list(findings), summary

    def verify(self, task, context, evidence=None, workspace=None):
        return VerificationResult(verdict="fail", summary=self._summary,
                                  findings=self._findings, evidence=evidence or {})


def orchestrator(tmp_path, worker, verifier, history):
    master = Master(make_project(tmp_path, [{"id": "t1", "status": "in_progress"}]))
    return master, TaskOrchestrator(master, worker, verifier, history=history)


def test_the_second_attempt_is_told_what_the_first_one_got_wrong(tmp_path):
    history, worker = InMemoryHistoryStore(), RecordingWorker()
    _, orch = orchestrator(tmp_path, worker, FailingVerifier(), history)

    orch.orchestrate("p", "t1", run_id="r1")
    orch.orchestrate("p", "t1", run_id="r1")

    first, second = worker.briefings
    assert "previous_failure" not in first
    assert "tests/test_levels.py" in second["previous_failure"]
    assert "exactly the same way" not in second["previous_failure"]


def test_a_repeat_is_called_out_and_recorded_with_its_fingerprint(tmp_path):
    history, worker = InMemoryHistoryStore(), RecordingWorker()
    _, orch = orchestrator(tmp_path, worker, FailingVerifier(), history)

    for _ in range(3):
        orch.orchestrate("p", "t1", run_id="r1")

    assert "exactly the same way" in worker.briefings[2]["previous_failure"]
    started = history.events(types=[EventType.ATTEMPT_STARTED])
    assert "previous_failure_fingerprint" not in started[0].payload
    assert started[1].payload["previous_failures"] == 1
    assert started[2].payload["identical_failure_streak"] == 2
    assert started[1].payload["previous_failure_fingerprint"] == \
        started[2].payload["previous_failure_fingerprint"]


def test_a_passing_attempt_leaves_nothing_to_tell_the_next_one(tmp_path):
    history, worker = InMemoryHistoryStore(), RecordingWorker()
    _, orch = orchestrator(tmp_path, worker, Verifier("pass"), history)

    orch.orchestrate("p", "t1", run_id="r1")
    orch.orchestrate("p", "t1", run_id="r1")

    assert all("previous_failure" not in b for b in worker.briefings)


def test_secrets_in_worker_and_verifier_output_are_redacted_in_history(tmp_path):
    history = InMemoryHistoryStore()
    worker = RecordingWorker(
        artifacts={"stderr_tail": f"curl failed\nDEEPSEEK_API_KEY={SECRET}",
                   "summary": f"I used {SECRET} to log in"},
        reason=f"the worker printed {SECRET}")
    verifier = FailingVerifier(findings=[{
        "kind": "command_failed", "command": "npm test", "exit_code": 1, "timed_out": False,
        "output_excerpt": f"Error: bad credentials {SECRET}\nAuthorization: Bearer zzz"}],
        summary=f"1 of 1 command(s) failed {SECRET}")
    _, orch = orchestrator(tmp_path, worker, verifier, history)

    result = orch.orchestrate("p", "t1", run_id="r1")
    orch.orchestrate("p", "t1", run_id="r1")

    everything = repr([(e.type, e.payload) for e in history.events()])
    assert SECRET not in everything
    assert "<redacted>" in everything
    assert SECRET not in repr(result)
    assert "bad credentials" in everything  # the diagnosis stays readable
    assert SECRET not in worker.briefings[1]["previous_failure"]
    assert "bad credentials" in worker.briefings[1]["previous_failure"]


def test_a_failed_verification_leaves_the_task_state_alone(tmp_path):
    history, worker = InMemoryHistoryStore(), RecordingWorker()
    master, orch = orchestrator(tmp_path, worker, FailingVerifier(), history)
    before = master.status("p")

    for _ in range(2):
        result = orch.orchestrate("p", "t1", run_id="r1")
        assert result["verification"]["verdict"] == "fail"

    status = master.status("p")
    assert status == before  # the orchestrator never changes task state
    task = next(t for t in status["tasks"] if t["id"] == "t1")
    assert task["status"] == "in_progress"


def test_the_loop_refuses_a_third_identical_attempt_and_changes_no_state(tmp_path):
    loop, master, _, backend = loop_for(
        tmp_path, [run_task()] * 5, verdict="fail",
        max_attempts_per_task=5, max_identical_failures=2)

    result = loop.run("alpha")

    assert result.stop_reason == STOP_ATTEMPT_LIMIT
    assert backend.calls == ["t1", "t1"]
    refusal = loop.history.events(types=[EventType.OPERATION_RESULT])[-1].payload
    assert refusal["status"] == "refused" and refusal["reason"] == "repeated_failure"
    assert "same way" in refusal["message"]
    assert status_of(master) == "in_progress"


def test_without_the_limit_the_loop_behaves_as_before(tmp_path):
    loop, _, _, backend = loop_for(tmp_path, [run_task()] * 5, verdict="fail",
                                   max_attempts_per_task=3)

    result = loop.run("alpha")

    assert result.stop_reason == STOP_ATTEMPT_LIMIT
    assert backend.calls == ["t1"] * 3


def test_a_human_edit_between_attempts_lets_the_loop_run_again(tmp_path):
    history = InMemoryHistoryStore()
    loop, _, _, backend = loop_for(tmp_path, [run_task()] * 6, verdict="fail",
                                   history=history, max_attempts_per_task=6,
                                   max_identical_failures=2)
    loop.run("alpha")
    assert backend.calls == ["t1", "t1"]

    history.append(type=EventType.HUMAN_ACTION, run_id="r9", project_id="alpha",
                   task_id="t1", payload={"action": "reopen"})
    loop2, _, _, backend2 = loop_for(tmp_path / "again", [run_task()] * 6, verdict="fail",
                                     history=history, max_attempts_per_task=6,
                                     max_identical_failures=2)
    loop2.run("alpha")
    assert backend2.calls == ["t1", "t1"]


class LimitedWorker(ExecutionBackend):
    """A worker whose provider reported a limit, with a secret echoed in the error text."""

    def execute(self, task, context, workspace=None):
        limit = {"kind": "provider_error", "reset_at": "unknown",
                 "detail": f"401 invalid credentials for DEEPSEEK_API_KEY={SECRET}; "
                           f"retry later (key {SECRET})"}
        return ExecutionResult(status="failed", reason="the worker's provider is limited",
                               artifacts={"limit": limit})


def test_a_secret_in_worker_limit_details_cannot_reach_stored_history(tmp_path):
    history = InMemoryHistoryStore()
    _, orch = orchestrator(tmp_path, LimitedWorker(), FailingVerifier(), history)

    result = orch.orchestrate("p", "t1", run_id="r1")

    assert result["outcome"] == "limited"
    stored = history.events(types=[EventType.ATTEMPT_FINISHED])[0].payload
    assert stored["outcome"] == "limited"
    assert stored["limit"]["kind"] == "provider_error"  # the diagnosis stays
    assert "invalid credentials" in stored["limit"]["detail"]
    assert "<redacted>" in stored["limit"]["detail"]
    everything = repr([(e.type, e.payload) for e in history.events()]) + repr(result)
    assert SECRET not in everything
