"""Tests for explicit execution: the worker runs only on a ``run_task`` decision.

Project state describes facts; operations explicitly request side effects. These
tests pin both halves: no state, however it looks, makes the worker run, and a
``run_task`` decision runs it exactly once, recorded as intent then outcome.
"""

import json

import pytest
import yaml

from conftest import attach_repository
from core.autonomous_loop import (
    STOP_MASTER,
    STOP_NO_WORK,
    STOP_OPERATION_FAILED,
    STOP_UNUSABLE_REPLY,
    AutonomousLoop,
)
from core.execution import ExecutionResult
from core.history import EventType, InMemoryHistoryStore
from core.master import Master
from core.provider import ReasoningProvider
from core.reasoning import (
    Operation,
    ReasoningInterface,
    RequestError,
    ResultStatus,
)
from core.reasoning_engine import build_operation_schema, describe_operations
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
    return project


def task(task_id="t1", status="planned"):
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
    def __init__(self, result=None, error=None):
        self._result = result or ExecutionResult(status="success", reason="ok")
        self._error = error
        self.calls = []

    def execute(self, task, context, workspace=None):
        self.calls.append(task["id"])
        if self._error is not None:
            raise self._error
        return self._result


class Verifier:
    def __init__(self, result=None, error=None):
        self._result = result or VerificationResult(verdict="pass", summary="ok")
        self._error = error
        self.calls = []

    def verify(self, task, context, evidence=None, workspace=None):
        self.calls.append(task["id"])
        if self._error is not None:
            raise self._error
        return self._result


def act(operation):
    return json.dumps({"decision": "act", "reason": "onwards", "operation": operation})


def update(task_id="t1", status="in_progress"):
    return act({"operation": "update_task", "project_id": "alpha",
                "task_id": task_id, "status": status})


def run_task(task_id="t1", project_id="alpha", **extra):
    return act({"operation": "run_task", "project_id": project_id,
                "task_id": task_id, **extra})


def wait():
    return json.dumps({"decision": "wait", "reason": "enough", "operation": None})


def build(tmp_path, tasks, replies, backend=None, verifier=None, **kwargs):
    write_project(tmp_path, tasks)
    master = Master(tmp_path)
    backend = backend or Backend()
    verifier = verifier or Verifier()
    history = InMemoryHistoryStore()
    loop = AutonomousLoop(master, ScriptedProvider(replies), backend, verifier,
                          history=history, **kwargs)
    return loop, master, backend, verifier, history


def tasks_of(master):
    return {t["id"]: t["status"] for t in master.status("alpha")["tasks"]}


def types(history):
    return [e.type for e in history.events()]


# --- state never implies execution ---------------------------------------


def test_starting_a_task_does_not_run_the_worker(tmp_path):
    loop, master, backend, verifier, _ = build(tmp_path, [task()], [update(), wait()])

    result = loop.run("alpha")

    assert result.stop_reason == STOP_MASTER
    assert tasks_of(master) == {"t1": "in_progress"}
    assert backend.calls == [] and verifier.calls == []


@pytest.mark.parametrize(
    "operation",
    [
        {"operation": "inspect_project", "project_id": "alpha"},
        {"operation": "create_task", "project_id": "alpha", "task_id": "t9",
         "milestone": "m1", "title": "Another"},
    ],
)
def test_an_unrelated_act_never_reruns_an_in_progress_task(tmp_path, operation):
    loop, _, backend, verifier, _ = build(
        tmp_path, [task(status="in_progress")], [act(operation), wait()]
    )

    result = loop.run("alpha")

    assert result.steps == 2

    assert backend.calls == [] and verifier.calls == []


# --- run_task runs exactly once ------------------------------------------


def test_run_task_executes_and_verifies_exactly_once(tmp_path):
    loop, master, backend, verifier, _ = build(
        tmp_path, [task(status="in_progress")], [run_task(), wait()]
    )

    result = loop.run("alpha")

    assert backend.calls == ["t1"]
    assert verifier.calls == ["t1"]
    assert result.last_output["verification"]["verdict"] == "pass"


def test_run_task_changes_no_task_state(tmp_path):
    loop, master, _, _, _ = build(
        tmp_path, [task(status="in_progress")], [run_task(), wait()]
    )
    before = master.status("alpha")["tasks"]

    loop.run("alpha")

    assert master.status("alpha")["tasks"] == before


def test_run_task_runs_the_task_it_names_not_the_first_in_progress_one(tmp_path):
    loop, _, backend, _, _ = build(
        tmp_path,
        [task("t1", "in_progress"), task("t2", "in_progress")],
        [run_task("t2"), wait()],
    )

    loop.run("alpha")

    assert backend.calls == ["t2"]


# --- run_task refusals ----------------------------------------------------


@pytest.mark.parametrize("status", ["planned", "completed", "blocked", "cancelled"])
def test_run_task_is_refused_unless_the_task_is_in_progress(tmp_path, status):
    tasks = [task("t1", status), task("t2", "planned")]
    loop, master, backend, verifier, history = build(tmp_path, tasks, [run_task()])

    result = loop.run("alpha")

    assert result.stop_reason == STOP_OPERATION_FAILED
    assert "not_executable" in result.detail
    assert backend.calls == [] and verifier.calls == []
    assert tasks_of(master)["t1"] == status
    refused = history.events(types=[EventType.OPERATION_RESULT])[-1].payload
    assert refused["status"] == "refused" and refused["operation"] == "run_task"
    assert history.events(types=[EventType.ATTEMPT_STARTED]) == ()


def test_run_task_on_an_unknown_task_is_refused(tmp_path):
    loop, _, backend, _, _ = build(
        tmp_path, [task(status="in_progress")], [run_task("nope")]
    )

    result = loop.run("alpha")

    assert result.stop_reason == STOP_OPERATION_FAILED
    assert backend.calls == []


def test_run_task_on_another_project_is_refused(tmp_path):
    loop, _, backend, _, _ = build(
        tmp_path, [task(status="in_progress")], [run_task(project_id="beta")]
    )

    result = loop.run("alpha")

    assert result.stop_reason == STOP_OPERATION_FAILED
    assert "wrong_project" in result.detail
    assert backend.calls == []


@pytest.mark.parametrize("extra", [{"worker": "x"}, {"model": "m"}, {"prompt": "p"},
                                   {"workspace": "/tmp"}])
def test_run_task_accepts_no_execution_details(tmp_path, extra):
    loop, _, backend, _, _ = build(
        tmp_path, [task(status="in_progress")], [run_task(**extra)] * 2
    )

    result = loop.run("alpha")

    assert result.stop_reason == STOP_UNUSABLE_REPLY
    assert "does not accept" in result.detail
    assert backend.calls == []


def test_master_never_executes_a_dispatch_operation(tmp_path):
    write_project(tmp_path, [task(status="in_progress")])
    master = Master(tmp_path)
    before = master.status("alpha")["tasks"]
    operation = Operation.propose(
        {"operation": "run_task", "project_id": "alpha", "task_id": "t1"}
    ).approve()

    outcome = ReasoningInterface(master).execute(operation)

    assert outcome.status is ResultStatus.REJECTED
    assert outcome.reason is RequestError.DISPATCH_ONLY
    assert master.status("alpha")["tasks"] == before


def test_run_task_is_offered_to_the_model():
    assert "run_task" in build_operation_schema()["properties"]["operation"]["oneOf"][0][
        "properties"]["operation"]["enum"]
    assert "run_task(project_id, task_id)" in describe_operations()


# --- recorded intent and outcome -----------------------------------------


def test_an_attempt_is_recorded_as_intent_then_outcome(tmp_path):
    loop, _, _, _, history = build(
        tmp_path, [task(status="in_progress")], [run_task(), wait()]
    )

    result = loop.run("alpha")

    assert types(history) == [
        EventType.RUN_STARTED,
        EventType.DECISION,
        EventType.ATTEMPT_STARTED,
        EventType.ATTEMPT_FINISHED,
        EventType.VERIFICATION,
        EventType.DECISION,
        EventType.RUN_STOPPED,
    ]
    started, finished, verified = history.events(
        types=[EventType.ATTEMPT_STARTED, EventType.ATTEMPT_FINISHED,
               EventType.VERIFICATION]
    )
    assert started.attempt_id == finished.attempt_id == verified.attempt_id
    assert result.last_output["attempt_id"] == started.attempt_id
    assert finished.payload["outcome"] == "finished"
    assert finished.payload["worker_reported_status"] == "success"
    assert finished.payload["worker"].endswith("Backend")
    assert verified.payload["verdict"] == "pass"
    assert {e.run_id for e in history.events()} == {result.run_id}


def test_a_state_operation_is_recorded_as_decision_then_result(tmp_path):
    loop, _, _, _, history = build(tmp_path, [task()], [update(), wait()])

    loop.run("alpha")

    decision, result = history.events(
        types=[EventType.DECISION, EventType.OPERATION_RESULT]
    )[:2]
    assert decision.seq < result.seq
    assert decision.payload["operation"]["name"] == "update_task"
    assert result.payload["status"] == "success"
    assert result.task_id == "t1"


def test_a_run_ends_with_exactly_one_run_stopped(tmp_path):
    loop, _, _, _, history = build(tmp_path, [task(status="completed")], [])

    result = loop.run("alpha")

    assert result.stop_reason == STOP_NO_WORK
    assert types(history) == [EventType.RUN_STARTED, EventType.RUN_STOPPED]
    assert history.events()[-1].payload["stop_reason"] == STOP_NO_WORK


def test_a_worker_that_raises_still_finishes_its_attempt(tmp_path):
    backend = Backend(error=RuntimeError("transport died mid-task"))
    loop, _, _, verifier, history = build(
        tmp_path, [task(status="in_progress")], [run_task()], backend=backend
    )

    with pytest.raises(RuntimeError, match="transport died"):
        loop.run("alpha")

    finished = history.events(types=[EventType.ATTEMPT_FINISHED])
    assert len(finished) == 1
    assert finished[0].payload["outcome"] == "error"
    assert finished[0].payload["error_type"] == "RuntimeError"
    assert verifier.calls == []
    assert types(history)[-1] is EventType.RUN_ERROR
    assert EventType.RUN_STOPPED not in types(history)


def test_a_verifier_that_raises_is_recorded(tmp_path):
    verifier = Verifier(error=RuntimeError("verifier broke"))
    loop, _, _, _, history = build(
        tmp_path, [task(status="in_progress")], [run_task()], verifier=verifier
    )

    with pytest.raises(RuntimeError, match="verifier broke"):
        loop.run("alpha")

    verification = history.events(types=[EventType.VERIFICATION])
    assert verification[0].payload["verdict"] is None
    assert verification[0].payload["error_type"] == "RuntimeError"


def test_a_transport_retry_inside_the_worker_is_one_attempt(tmp_path):
    class RetryingBackend:
        """Retries its own flaky transport; all of it is a single attempt."""

        def __init__(self):
            self.transport_calls = 0

        def execute(self, task, context, workspace=None):
            for _ in range(3):
                self.transport_calls += 1
                if self.transport_calls == 3:
                    return ExecutionResult(status="success", reason="third time")
            raise AssertionError("unreachable")

    backend = RetryingBackend()
    loop, _, _, _, history = build(
        tmp_path, [task(status="in_progress")], [run_task(), wait()], backend=backend
    )

    loop.run("alpha")

    assert backend.transport_calls == 3
    assert len(history.events(types=[EventType.ATTEMPT_STARTED])) == 1
    assert len(history.events(types=[EventType.ATTEMPT_FINISHED])) == 1


# --- token usage is recorded with each decision -----------------------------------


class Metered(ScriptedProvider):
    """A provider that reports usage for every call, usable or not."""

    def complete(self, prompt, schema=None):
        self.last_usage = {"input_tokens": 1000, "output_tokens": 50}
        return super().complete(prompt, schema)


def test_decisions_record_usage_and_the_reasoner(tmp_path):
    write_project(tmp_path, [task(status="in_progress")])
    history = InMemoryHistoryStore()
    loop = AutonomousLoop(Master(tmp_path), Metered(["not json at all", wait()]), Backend(),
                          Verifier(), history=history)

    loop.run("alpha")

    decision = history.events(types=[EventType.DECISION])[0].payload
    assert decision["usage"] == {"input_tokens": 1000, "output_tokens": 50}
    assert decision["reasoner"] == "scripted"
    # The unusable first reply was paid for too.
    assert decision["failed_call_usage"]["input_tokens"] == 1000


def test_usage_of_replies_the_loop_gave_up_on_is_recorded_at_the_end(tmp_path):
    write_project(tmp_path, [task(status="in_progress")])
    history = InMemoryHistoryStore()
    loop = AutonomousLoop(Master(tmp_path), Metered(["nope", "still nope"]), Backend(),
                          Verifier(), history=history)

    result = loop.run("alpha")

    assert result.stop_reason == STOP_UNUSABLE_REPLY
    stopped = history.events(types=[EventType.RUN_STOPPED])[0].payload
    assert stopped["failed_call_usage"]["input_tokens"] == 2000
    assert stopped["failed_call_usage"]["output_tokens"] == 100
