"""Tests for evidence read from history: completion gate, attempt limit, context.

The gate and the limit are deterministic rules over recorded events, so most
cases are set up by writing those events directly. A few run the real loop to
show the rules are what the loop actually applies.
"""

import json

import pytest
import yaml

from conftest import attach_repository
from core.autonomous_loop import (
    STOP_APPROVAL,
    STOP_ATTEMPT_LIMIT,
    STOP_MASTER,
    STOP_NO_WORK,
    AutonomousLoop,
)
from core.evidence import RECENT_DECISIONS, HistoryEvidence, spec_hash
from core.execution import ExecutionResult
from core.history import EventType, InMemoryHistoryStore
from core.master import Master
from core.provider import ReasoningProvider
from core.reasoning import Operation, requires_approval
from core.verification import VerificationResult

MILESTONE = {"id": "m1", "name": "M1", "status": "in_progress"}


def write_project(root, tasks):
    project = root / "alpha-project"
    project.mkdir(parents=True)
    (project / "project.yaml").write_text(
        yaml.safe_dump({"id": "alpha", "name": "alpha", "status": "active"})
    )
    (project / "milestones.yaml").write_text(yaml.safe_dump({"milestones": [MILESTONE]}))
    (project / "tasks.yaml").write_text(yaml.safe_dump({"tasks": tasks}))
    attach_repository(project)


def task(task_id="t1", status="in_progress"):
    return {"id": task_id, "milestone": "m1", "title": f"Task {task_id}",
            "status": status, "assigned_to": "master"}


class ScriptedProvider(ReasoningProvider):
    name = "scripted"

    def __init__(self, replies):
        self._replies = list(replies)
        self.prompts = []

    def complete(self, prompt, schema=None):
        self.prompts.append(prompt)
        if not self._replies:
            raise AssertionError("provider called more often than scripted")
        return self._replies.pop(0)


class Backend:
    def __init__(self, result=None):
        self._result = result or ExecutionResult(status="success", reason="ok")
        self.calls = []

    def execute(self, task, context, workspace=None):
        self.calls.append(task["id"])
        return self._result


class Verifier:
    def __init__(self, verdict="pass", summary="ok"):
        self._result = VerificationResult(verdict=verdict, summary=summary)

    def verify(self, task, context, evidence=None, workspace=None):
        return self._result


def act(operation):
    return json.dumps({"decision": "act", "reason": "onwards", "operation": operation})


def complete(task_id="t1"):
    return act({"operation": "update_task", "project_id": "alpha",
                "task_id": task_id, "status": "completed"})


def run_task(task_id="t1"):
    return act({"operation": "run_task", "project_id": "alpha", "task_id": task_id})


def wait():
    return json.dumps({"decision": "wait", "reason": "enough", "operation": None})


def loop_for(tmp_path, replies, history=None, verdict="pass", backend=None,
             tasks=None, **kwargs):
    write_project(tmp_path, tasks or [task()])
    master = Master(tmp_path)
    provider = ScriptedProvider(replies)
    backend = backend or Backend()
    loop = AutonomousLoop(master, provider, backend, Verifier(verdict),
                          history=history or InMemoryHistoryStore(), **kwargs)
    return loop, master, provider, backend


def status_of(master, task_id="t1"):
    return next(t for t in master.status("alpha")["tasks"] if t["id"] == task_id)["status"]


# --- recording attempts by hand -------------------------------------------


#: The task the hand-written attempts were made against.
TASK = task()


def started(history, task_id="t1", session_id="s1", run_id="r1", spec=TASK):
    payload = {} if spec is None else {"spec_hash": spec_hash(spec)}
    return history.append(type=EventType.ATTEMPT_STARTED, run_id=run_id,
                          session_id=session_id, project_id="alpha",
                          task_id=task_id, payload=payload).attempt_id


def finished(history, attempt_id, outcome="finished", task_id="t1", session_id="s1",
             reported="success"):
    history.append(type=EventType.ATTEMPT_FINISHED, run_id="r1", session_id=session_id,
                   project_id="alpha", task_id=task_id, attempt_id=attempt_id,
                   payload={"outcome": outcome, "worker_reported_status": reported,
                            "diffstat": {"files": 1, "insertions": 2, "deletions": 0}})


def verified(history, attempt_id, verdict="pass", task_id="t1", session_id="s1"):
    history.append(type=EventType.VERIFICATION, run_id="r1", session_id=session_id,
                   project_id="alpha", task_id=task_id, attempt_id=attempt_id,
                   payload={"verdict": verdict, "summary": "s", "findings": []})


def interrupted(history, attempt_id, task_id="t1", session_id="s1"):
    history.append(type=EventType.ATTEMPT_INTERRUPTED, run_id="r2",
                   session_id=session_id, project_id="alpha", task_id=task_id,
                   attempt_id=attempt_id)


def completion(task_id="t1", operation="update_task"):
    payload = {"operation": operation, "project_id": "alpha", "task_id": task_id,
               "status": "completed"}
    if operation == "create_task":
        payload.update(milestone="m1", title="T")
    return Operation.propose(payload)


# --- completion gate ------------------------------------------------------


def test_completing_with_no_attempt_is_gated():
    assert HistoryEvidence(InMemoryHistoryStore()).completion_gate(completion(), TASK) == "no_attempt"


def test_completing_after_a_passing_attempt_is_allowed():
    history = InMemoryHistoryStore()
    a = started(history)
    finished(history, a)
    verified(history, a, "pass")

    assert HistoryEvidence(history).completion_gate(completion(), TASK) is None


@pytest.mark.parametrize(
    "verdict, reason",
    [("fail", "verification_fail"), ("needs_human", "verification_needs_human"),
     ("unable_to_verify", "verification_unable_to_verify"),
     (None, "verification_errored")],
)
def test_completing_after_anything_but_pass_is_gated(verdict, reason):
    history = InMemoryHistoryStore()
    a = started(history)
    finished(history, a)
    verified(history, a, verdict)

    assert HistoryEvidence(history).completion_gate(completion(), TASK) == reason


def test_the_latest_attempt_decides_not_an_earlier_pass():
    history = InMemoryHistoryStore()
    first = started(history)
    finished(history, first)
    verified(history, first, "pass")
    second = started(history)
    finished(history, second, reported="failed")
    verified(history, second, "fail")

    assert HistoryEvidence(history).completion_gate(completion(), TASK) == "verification_fail"


def test_an_interrupted_latest_attempt_is_gated():
    history = InMemoryHistoryStore()
    a = started(history)
    interrupted(history, a)

    assert HistoryEvidence(history).completion_gate(completion(), TASK) == "latest_attempt_interrupted"


def test_an_unfinished_latest_attempt_is_gated():
    history = InMemoryHistoryStore()
    started(history)

    assert HistoryEvidence(history).completion_gate(completion(), TASK) == "latest_attempt_unfinished"


def test_an_errored_latest_attempt_is_gated():
    history = InMemoryHistoryStore()
    a = started(history)
    finished(history, a, "error", reported=None)

    assert HistoryEvidence(history).completion_gate(completion(), TASK) == "latest_attempt_errored"


def test_a_finished_but_unverified_attempt_is_gated():
    history = InMemoryHistoryStore()
    a = started(history)
    finished(history, a)

    assert HistoryEvidence(history).completion_gate(completion(), TASK) == "not_verified"


def test_a_pass_from_an_earlier_session_still_counts():
    history = InMemoryHistoryStore()
    a = started(history, session_id="old")
    finished(history, a, session_id="old")
    verified(history, a, "pass", session_id="old")

    evidence = HistoryEvidence(history, session_id="new")
    assert evidence.completion_gate(completion(), TASK) is None


def test_creating_a_task_as_already_completed_is_gated():
    gate = HistoryEvidence(InMemoryHistoryStore()).completion_gate(
        completion("t9", operation="create_task"), None
    )

    assert gate == "no_attempt"


@pytest.mark.parametrize("status", ["blocked", "cancelled", "in_progress", "planned"])
def test_other_status_changes_are_not_gated(status):
    operation = Operation.propose({"operation": "update_task", "project_id": "alpha",
                                   "task_id": "t1", "status": status})

    assert HistoryEvidence(InMemoryHistoryStore()).completion_gate(operation) is None


def test_the_approval_policy_itself_stays_a_pure_function_of_the_operation():
    assert requires_approval(completion()) is False


# --- the gate through the real loop --------------------------------------


def test_the_loop_holds_a_completion_after_failed_verification(tmp_path):
    loop, master, _, _ = loop_for(tmp_path, [run_task(), complete()], verdict="fail")

    result = loop.run("alpha")

    assert result.stop_reason == STOP_APPROVAL
    assert result.approval_reason == "verification_fail"
    assert status_of(master) == "in_progress"
    decision = loop.history.events(types=[EventType.DECISION])[-1].payload
    assert decision["gate_reason"] == "verification_fail"
    assert loop.history.events(types=[EventType.OPERATION_RESULT]) == ()


def test_the_loop_applies_a_completion_after_a_pass(tmp_path):
    loop, master, _, _ = loop_for(tmp_path, [run_task(), complete()])

    result = loop.run("alpha")

    assert result.stop_reason == STOP_NO_WORK
    assert status_of(master) == "completed"


def test_the_loop_holds_a_completion_with_no_attempt(tmp_path):
    loop, master, _, _ = loop_for(tmp_path, [complete()])

    result = loop.run("alpha")

    assert result.stop_reason == STOP_APPROVAL
    assert result.approval_reason == "no_attempt"
    assert status_of(master) == "in_progress"


# --- attempt limit --------------------------------------------------------


def test_the_attempt_after_the_limit_is_refused(tmp_path):
    loop, _, _, backend = loop_for(tmp_path, [run_task()] * 4, session_id="s1")

    result = loop.run("alpha")

    assert result.stop_reason == STOP_ATTEMPT_LIMIT
    assert backend.calls == ["t1"] * 3
    refusal = loop.history.events(types=[EventType.OPERATION_RESULT])[-1].payload
    assert refusal["status"] == "refused" and refusal["reason"] == "attempt_limit"


def test_errored_and_interrupted_attempts_count(tmp_path):
    history = InMemoryHistoryStore()
    a = started(history)
    finished(history, a, "error", reported=None)
    b = started(history)
    interrupted(history, b)
    started(history)
    loop, _, _, backend = loop_for(tmp_path, [run_task()], history=history,
                                   session_id="s1")

    result = loop.run("alpha")

    assert result.stop_reason == STOP_ATTEMPT_LIMIT
    assert backend.calls == []


def test_a_new_session_gets_a_fresh_budget_but_sees_the_old_attempts(tmp_path):
    history = InMemoryHistoryStore()
    for _ in range(3):
        a = started(history, session_id="old")
        finished(history, a, reported="failed", session_id="old")
    loop, _, provider, backend = loop_for(tmp_path, [run_task(), wait()],
                                          history=history, session_id="new")

    result = loop.run("alpha")

    assert result.stop_reason == STOP_MASTER
    assert backend.calls == ["t1"]
    context = json.loads(provider.prompts[1].split("PROJECT STATE\n", 1)[1]
                         .split("\n\nADDITIONAL RULES", 1)[0])
    counts = context["execution_evidence"]["tasks"]["t1"]
    assert counts["attempts_this_session"] == 1
    assert counts["attempts_total"] == 4


def test_without_a_session_the_budget_is_per_run(tmp_path):
    history = InMemoryHistoryStore()
    for _ in range(3):
        started(history, session_id=None, run_id="earlier-run")
    loop, _, _, backend = loop_for(tmp_path, [run_task(), wait()], history=history)

    loop.run("alpha")

    assert backend.calls == ["t1"]


# --- the context Master sees ---------------------------------------------


def context_of(prompt):
    body = prompt.split("PROJECT STATE\n", 1)[1].split("\n\nADDITIONAL RULES", 1)[0]
    return json.loads(body)


def test_master_sees_the_latest_attempt_and_counts(tmp_path):
    loop, _, provider, _ = loop_for(tmp_path, [run_task(), wait()], verdict="fail",
                                    session_id="s1")

    loop.run("alpha")

    evidence = context_of(provider.prompts[1])["execution_evidence"]
    assert evidence["attempt_limit"] == 3
    latest = evidence["tasks"]["t1"]["latest_attempt"]
    assert evidence["tasks"]["t1"]["attempts_this_session"] == 1
    assert evidence["tasks"]["t1"]["attempts_total"] == 1
    assert latest["outcome"] == "finished"
    assert latest["worker_reported_status"] == "success"
    assert latest["spec_current"] is True
    assert set(latest["changes"]) == {"files", "insertions", "deletions"}
    assert latest["verification"] == {"verdict": "fail", "summary": "ok", "findings": []}


def test_master_sees_an_interrupted_attempt(tmp_path):
    history = InMemoryHistoryStore()
    a = started(history)
    interrupted(history, a)
    loop, _, provider, backend = loop_for(tmp_path, [wait()], history=history,
                                          session_id="s1")

    loop.run("alpha")

    latest = context_of(provider.prompts[0])["execution_evidence"]["tasks"]["t1"]
    assert latest["latest_attempt"]["outcome"] == "interrupted"
    assert latest["latest_attempt"]["verification"] is None
    assert backend.calls == []


def test_worker_output_and_identity_never_reach_master(tmp_path):
    backend = Backend(ExecutionResult(
        status="success",
        reason="secret-runtime-name finished",
        artifacts={"runtime": "secret-runtime-name", "model": "secret-model"},
    ))
    loop, _, provider, _ = loop_for(tmp_path, [run_task(), wait()], backend=backend)

    loop.run("alpha")

    assert "secret-runtime-name" not in provider.prompts[1]
    assert "secret-model" not in provider.prompts[1]
    assert "Backend" not in provider.prompts[1]
    # ...while history keeps it as opaque provenance.
    recorded = loop.history.events(types=[EventType.ATTEMPT_FINISHED])[0].payload
    assert recorded["artifacts"]["model"] == "secret-model"
    assert recorded["reason"] == "secret-runtime-name finished"


def test_recent_decisions_are_bounded(tmp_path):
    loop, _, provider, _ = loop_for(
        tmp_path, [act({"operation": "inspect_project", "project_id": "alpha"})] * 7
        + [wait()], session_id="s1", max_steps=8,
    )

    loop.run("alpha")

    recent = context_of(provider.prompts[-1])["recent_decisions"]
    assert len(recent) == RECENT_DECISIONS
    assert recent[-1]["operation"] == "inspect_project"


# --- evidence is bound to the spec it verified (A3) --------------------------


def passed(history, spec=TASK):
    a = started(history, spec=spec)
    finished(history, a)
    verified(history, a, "pass")
    return a


def test_retitling_a_task_after_a_pass_is_spec_changed():
    history = InMemoryHistoryStore()
    passed(history)

    retitled = dict(TASK, title="Something else entirely")

    assert HistoryEvidence(history).completion_gate(completion(), retitled) == "spec_changed"


def test_changing_acceptance_after_a_pass_is_spec_changed():
    history = InMemoryHistoryStore()
    passed(history, spec=dict(TASK, acceptance={"commands": ["pytest -q"]}))

    loosened = dict(TASK, acceptance={"commands": ["true"]})

    assert HistoryEvidence(history).completion_gate(completion(), loosened) == "spec_changed"


def test_retitling_and_completing_in_one_operation_is_spec_changed():
    history = InMemoryHistoryStore()
    passed(history)
    operation = Operation.propose({"operation": "update_task", "project_id": "alpha",
                                   "task_id": "t1", "title": "Renamed",
                                   "status": "completed"})

    assert HistoryEvidence(history).completion_gate(operation, TASK) == "spec_changed"


def test_an_attempt_recorded_without_a_spec_hash_is_spec_changed():
    history = InMemoryHistoryStore()
    passed(history, spec=None)

    assert HistoryEvidence(history).completion_gate(completion(), TASK) == "spec_changed"


def test_a_timed_out_latest_attempt_is_gated():
    history = InMemoryHistoryStore()
    a = started(history)
    finished(history, a, "timed_out")

    assert HistoryEvidence(history).completion_gate(completion(), TASK) == "latest_attempt_timed_out"


def test_the_spec_hash_covers_title_and_acceptance_only():
    base = {"id": "t1", "title": "T", "status": "planned", "milestone": "m1"}

    assert spec_hash(base) == spec_hash(dict(base, status="in_progress", milestone="m2"))
    assert spec_hash(base) != spec_hash(dict(base, title="U"))
    assert spec_hash(base) != spec_hash(dict(base, acceptance={"commands": ["x"]}))


def test_master_sees_when_the_latest_attempt_is_stale(tmp_path):
    loop, master, provider, _ = loop_for(
        tmp_path, [run_task(), act({"operation": "update_task", "project_id": "alpha",
                                    "task_id": "t1", "title": "Retitled"}), wait()],
        session_id="s1",
    )

    loop.run("alpha")

    latest = context_of(provider.prompts[2])["execution_evidence"]["tasks"]["t1"]
    assert latest["latest_attempt"]["spec_current"] is False


def test_the_loop_refuses_to_complete_after_a_retitle(tmp_path):
    loop, master, _, _ = loop_for(
        tmp_path, [run_task(), act({"operation": "update_task", "project_id": "alpha",
                                    "task_id": "t1", "title": "Retitled"}), complete()],
    )

    result = loop.run("alpha")

    assert result.stop_reason == STOP_APPROVAL
    assert result.approval_reason == "spec_changed"
    assert status_of(master) == "in_progress"


# --- legacy history -----------------------------------------------------------


def test_attempts_recorded_before_milestone_1_are_read():
    history = InMemoryHistoryStore()
    a = started(history, spec=None)
    history.append(type=EventType.ATTEMPT_FINISHED, run_id="r1", session_id="s1",
                   project_id="alpha", task_id="t1", attempt_id=a,
                   payload={"status": "failed"})
    b = started(history, spec=None)
    history.append(type=EventType.ATTEMPT_FINISHED, run_id="r1", session_id="s1",
                   project_id="alpha", task_id="t1", attempt_id=b,
                   payload={"status": "error"})

    from core.evidence import attempts_for_task
    first, second = attempts_for_task(history, "alpha", "t1")

    assert (first.outcome, first.worker_reported_status) == ("finished", "failed")
    assert (second.outcome, second.worker_reported_status) == ("error", None)


def test_master_is_told_whether_a_task_has_acceptance(tmp_path):
    write_project(tmp_path, [dict(task(), acceptance={"commands": ["pytest"]}),
                             task("t2")])
    from core.reasoning_engine import ReasoningEngine

    context = ReasoningEngine(None, Master(tmp_path)).context_for("alpha")

    assert {t["id"]: t["has_acceptance"] for t in context["tasks"]} == {"t1": True,
                                                                        "t2": False}
    assert "pytest" not in json.dumps(context)
