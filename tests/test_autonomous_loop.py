"""Tests for the autonomous loop: sequencing only, no new authority.

Every collaborator is a fake. Nothing here needs a network, a paid model or a
real worker, because the loop's whole job is to sequence things that already
have their own tests.
"""

import json

import pytest
import yaml

from core.autonomous_loop import (
    STOP_APPROVAL,
    STOP_MASTER,
    STOP_NO_WORK,
    STOP_OPERATION_FAILED,
    STOP_STEP_LIMIT,
    STOP_UNUSABLE_REPLY,
    AutonomousLoop,
    run_autonomous,
)
from core.execution import ExecutionResult
from core.master import Master
from core.provider import ReasoningProvider
from core.reasoning import Decision, SPECS
from core import reasoning
from core.verification import VerificationResult

MILESTONE = {"id": "m1", "name": "M1", "status": "in_progress"}


def write_project(root, project_id="alpha", tasks=None):
    project = root / f"{project_id}-project"
    project.mkdir(parents=True)
    (project / "project.yaml").write_text(
        yaml.safe_dump({"id": project_id, "name": project_id, "status": "active"})
    )
    (project / "milestones.yaml").write_text(
        yaml.safe_dump({"milestones": [MILESTONE]})
    )
    (project / "tasks.yaml").write_text(yaml.safe_dump({"tasks": tasks or []}))
    return project


def task(task_id="t1", status="planned", **extra):
    record = {
        "id": task_id,
        "milestone": "m1",
        "title": f"Task {task_id}",
        "status": status,
        "assigned_to": "master",
    }
    record.update(extra)
    return record


# --- deterministic fakes --------------------------------------------------


class ScriptedProvider(ReasoningProvider):
    """Returns queued replies and refuses to be called more often than scripted.

    Running off the end is an AssertionError rather than a default WAIT, so a
    loop that spins forever fails the test instead of quietly "passing".
    """

    name = "scripted"

    def __init__(self, replies):
        self._replies = list(replies)
        self.prompts = []

    def complete(self, prompt, schema=None):
        self.prompts.append(prompt)
        if not self._replies:
            raise AssertionError(
                f"provider called {len(self.prompts)}x but only "
                f"{len(self._replies)} reply(ies) were scripted"
            )
        return self._replies.pop(0)


class FakeExecutionBackend:
    def __init__(self, result=None):
        self._result = result or ExecutionResult(status="success", reason="ok")
        self.calls = []

    def execute(self, task, context):
        self.calls.append((dict(task), dict(context)))
        return self._result


class FakeVerificationBackend:
    def __init__(self, result=None):
        self._result = result or VerificationResult(
            verdict="pass", summary="looks good"
        )
        self.calls = []

    def verify(self, task, context, evidence=None):
        self.calls.append((dict(task), dict(context), dict(evidence or {})))
        return self._result


# --- decision replies -----------------------------------------------------


def act(operation, reason="onwards"):
    return json.dumps(
        {"decision": "act", "reason": reason, "operation": operation}
    )


def no_op(decision, reason="because"):
    return json.dumps({"decision": decision, "reason": reason})


def start():
    return act(
        {
            "operation": "update_task",
            "project_id": "alpha",
            "task_id": "t1",
            "status": "in_progress",
        }
    )


def dispatch(task_id="t1", project_id="alpha"):
    return act(
        {"operation": "run_task", "project_id": project_id, "task_id": task_id}
    )


def finish():
    return act(
        {
            "operation": "update_task",
            "project_id": "alpha",
            "task_id": "t1",
            "status": "completed",
        }
    )


@pytest.fixture
def master(tmp_path):
    write_project(tmp_path, tasks=[task()])
    return Master(tmp_path)


def build(master, replies, exec_result=None, verify_result=None, **kwargs):
    provider = ScriptedProvider(replies)
    backend = FakeExecutionBackend(exec_result)
    verifier = FakeVerificationBackend(verify_result)
    loop = AutonomousLoop(master, provider, backend, verifier, **kwargs)
    return loop, provider, backend, verifier


def status_of(master, task_id="t1"):
    return next(
        t for t in master.status("alpha")["tasks"] if t["id"] == task_id
    )["status"]


# --- 1. the happy path ----------------------------------------------------


def test_a_ready_task_runs_the_whole_path_to_completed(master):
    loop, provider, backend, verifier = build(
        master, [start(), dispatch(), finish()]
    )

    result = loop.run("alpha")

    assert result.stop_reason == STOP_NO_WORK
    assert status_of(master) == "completed"
    assert len(backend.calls) == 1
    assert len(verifier.calls) == 1
    assert len(provider.prompts) == 3
    assert result.steps == 3


def test_execution_and_verification_results_are_fed_back_to_master(master):
    loop, provider, backend, verifier = build(
        master, [start(), dispatch(), finish()]
    )

    loop.run("alpha")

    # The decision after run_task was made with its outcome in view.
    assert len(provider.prompts) == 3
    assert "last_result" in provider.prompts[2]
    assert '"status": "success"' in provider.prompts[2]
    assert '"verdict": "pass"' in provider.prompts[2]


# --- 2. worker failure ----------------------------------------------------


def test_a_worker_failure_reaches_master_and_does_not_complete(master):
    loop, provider, backend, verifier = build(
        master,
        [start(), dispatch(), no_op("wait")],
        exec_result=ExecutionResult(status="failed", reason="boom"),
        verify_result=VerificationResult(verdict="fail", summary="no output"),
    )

    result = loop.run("alpha")

    assert result.stop_reason == STOP_MASTER
    assert result.decision.decision is Decision.WAIT
    assert status_of(master) == "in_progress"
    assert '"status": "failed"' in provider.prompts[2]
    assert '"verdict": "fail"' in provider.prompts[2]


# --- 3. QA failure --------------------------------------------------------


def test_a_failed_qa_reaches_master_and_does_not_complete(master):
    loop, provider, backend, verifier = build(
        master,
        [start(), dispatch(), no_op("wait")],
        exec_result=ExecutionResult(status="success", reason="ok"),
        verify_result=VerificationResult(verdict="fail", summary="wrong output"),
    )

    result = loop.run("alpha")

    assert result.stop_reason == STOP_MASTER
    assert status_of(master) == "in_progress"
    assert '"verdict": "fail"' in provider.prompts[2]


# --- 4. Master stops ------------------------------------------------------


@pytest.mark.parametrize("decision", ["wait", "blocked", "needs_information"])
def test_a_non_act_decision_stops_without_running_anything(master, decision):
    loop, provider, backend, verifier = build(master, [no_op(decision)])

    result = loop.run("alpha")

    assert result.stop_reason == STOP_MASTER
    assert result.decision.decision is Decision(decision)
    assert backend.calls == []
    assert verifier.calls == []
    assert status_of(master) == "planned"


def test_a_stop_wins_over_in_flight_work(tmp_path):
    """Master says WAIT while a task is already in progress: the worker stays
    idle. Stopping is a decision, not something a queued task overrides."""
    write_project(tmp_path, tasks=[task(status="in_progress")])
    master = Master(tmp_path)

    loop, provider, backend, verifier = build(master, [no_op("wait")])

    result = loop.run("alpha")

    assert result.stop_reason == STOP_MASTER
    assert backend.calls == []
    assert verifier.calls == []
    assert status_of(master) == "in_progress"


# --- 5. approval ----------------------------------------------------------


def test_a_request_approval_stops_without_applying_the_action(master):
    loop, provider, backend, verifier = build(
        master,
        [
            json.dumps(
                {
                    "decision": "request_approval",
                    "reason": "gate it",
                    "operation": {
                        "operation": "update_task",
                        "project_id": "alpha",
                        "task_id": "t1",
                        "status": "in_progress",
                    },
                }
            )
        ],
    )

    result = loop.run("alpha")

    assert result.stop_reason == STOP_MASTER
    assert result.decision.decision is Decision.REQUEST_APPROVAL
    assert backend.calls == []
    assert status_of(master) == "planned"


def test_an_act_on_a_gated_operation_is_still_approval_required(
    master, monkeypatch
):
    """The model's ACT cannot talk past the approval policy."""
    from types import MappingProxyType

    from core.reasoning import ImpactLevel, OperationSpec

    monkeypatch.setattr(
        reasoning,
        "SPECS",
        MappingProxyType(
            {
                **SPECS,
                "gated_update_task": OperationSpec(
                    "update_task",
                    ("project_id", "task_id"),
                    variadic=True,
                    impact=ImpactLevel.CRITICAL,
                ),
            }
        ),
    )

    loop, provider, backend, verifier = build(
        master,
        [
            act(
                {
                    "operation": "gated_update_task",
                    "project_id": "alpha",
                    "task_id": "t1",
                    "status": "in_progress",
                }
            )
        ],
    )

    result = loop.run("alpha")

    assert result.stop_reason == STOP_APPROVAL
    assert result.decision.decision is Decision.ACT
    assert result.decision.pending_approval is True
    assert backend.calls == []
    assert status_of(master) == "planned"


# --- 6. nothing to do -----------------------------------------------------


def test_a_finished_project_exits_without_asking_the_model(tmp_path):
    write_project(tmp_path, tasks=[task(status="completed")])
    master = Master(tmp_path)

    loop, provider, backend, verifier = build(master, [])

    result = loop.run("alpha")

    assert result.stop_reason == STOP_NO_WORK
    assert result.steps == 0
    assert provider.prompts == []
    assert backend.calls == []


def test_a_blocked_project_exits_without_running_anything(tmp_path):
    # t1 waits on t2, which was cancelled: nothing is ready, nothing runs.
    write_project(
        tmp_path,
        tasks=[
            task("t2", status="cancelled"),
            task("t1", status="planned", depends_on=["t2"]),
        ],
    )
    master = Master(tmp_path)

    loop, provider, backend, verifier = build(master, [])

    result = loop.run("alpha")

    assert result.stop_reason == STOP_NO_WORK
    assert provider.prompts == []
    assert backend.calls == []


# --- 7. results are never authoritative -----------------------------------


def test_results_never_mutate_state_on_their_own(master):
    # The worker claims completion and the verifier agrees. Neither is Master.
    loop, provider, backend, verifier = build(
        master,
        [start(), no_op("wait")],
        exec_result=ExecutionResult(
            status="success",
            reason="looks done",
            state_updates={"status": "completed"},
        ),
        verify_result=VerificationResult(verdict="pass", summary="verified"),
    )

    result = loop.run("alpha")

    assert status_of(master) == "in_progress"
    assert result.stop_reason == STOP_MASTER
    # The claim was carried as evidence, not applied.
    assert '"completed"' in provider.prompts[1]


def test_completion_requires_a_second_explicit_master_decision(master):
    loop, provider, backend, verifier = build(
        master,
        [start(), no_op("wait")],
        exec_result=ExecutionResult(status="success", state_updates={"status": "completed"}),
        verify_result=VerificationResult(verdict="pass", summary="verified"),
    )

    loop.run("alpha")
    assert status_of(master) == "in_progress"

    # A later, explicit Master decision is what finishes the task.
    loop2, _, _, _ = build(master, [finish()])
    assert loop2.run("alpha").stop_reason == STOP_NO_WORK
    assert status_of(master) == "completed"


# --- the entry point takes everything from the caller ---------------------


def test_the_entry_point_accepts_any_injected_collaborators(master):
    result = run_autonomous(
        "alpha",
        master=master,
        provider=ScriptedProvider([no_op("blocked")]),
        execution_backend=FakeExecutionBackend(),
        verification_backend=FakeVerificationBackend(),
    )

    assert result.stop_reason == STOP_MASTER


def test_the_loop_refuses_a_bad_request(master):
    with pytest.raises(ValueError):
        AutonomousLoop(
            master,
            ScriptedProvider([]),
            FakeExecutionBackend(),
            FakeVerificationBackend(),
            request="   ",
        )


def test_the_loop_stops_at_its_step_limit_instead_of_spinning(master):
    # A worker that always fails and a Master that always restarts: bounded.
    loop, provider, backend, verifier = build(
        master,
        [start(), start(), start()],
        exec_result=ExecutionResult(status="failed", reason="boom"),
        verify_result=VerificationResult(verdict="fail", summary="nope"),
        max_steps=3,
    )

    result = loop.run("alpha")

    assert result.stop_reason == STOP_STEP_LIMIT
    assert result.steps == 3


# --- unusable reasoning replies -----------------------------------------
#
# These are the shapes a real model produced during the end-to-end experiment:
# the operation name in the wrong key, so the parser could not build a
# decision. The engine stays strict; the loop has to survive it.


def malformed(key):
    """A reply whose operation name sits under the wrong key."""
    return json.dumps(
        {
            "decision": "act",
            "reason": "start it",
            "operation": {
                key: "update_task",
                "project_id": "alpha",
                "task_id": "t1",
                "status": "in_progress",
            },
        }
    )


def test_a_malformed_reply_is_retried_and_the_loop_continues(master):
    loop, provider, backend, verifier = build(
        master, [malformed("operation_type"), start(), finish()]
    )

    result = loop.run("alpha")

    assert result.stop_reason == STOP_NO_WORK
    assert len(provider.prompts) == 3
    assert status_of(master) == "completed"


def test_two_malformed_replies_stop_the_loop_cleanly(master):
    loop, provider, backend, verifier = build(
        master, [malformed("operation_type"), malformed("name")]
    )

    result = loop.run("alpha")

    assert result.stop_reason == STOP_UNUSABLE_REPLY
    assert result.decision is None
    assert len(provider.prompts) == 2
    assert "missing 'operation'" in result.detail


def test_a_malformed_reply_mutates_nothing(master):
    # Nothing is invented or repaired, so the task never moves.
    loop, provider, backend, verifier = build(
        master, [malformed("operation_type"), malformed("name")]
    )

    loop.run("alpha")

    assert status_of(master) == "planned"
    assert backend.calls == []
    assert verifier.calls == []


def test_the_retry_bound_is_configurable(master):
    loop, provider, backend, verifier = build(
        master, [malformed("name")], max_retries=0
    )

    result = loop.run("alpha")

    assert result.stop_reason == STOP_UNUSABLE_REPLY
    assert len(provider.prompts) == 1
    assert "1 attempt(s)" in result.detail


def test_a_provider_failure_is_bounded_the_same_way(master):
    class FailingProvider(ReasoningProvider):
        name = "always-down"

        def complete(self, prompt, schema=None):
            from core.provider import ProviderError

            raise ProviderError("down")

    loop = AutonomousLoop(
        master,
        FailingProvider(),
        FakeExecutionBackend(),
        FakeVerificationBackend(),
    )

    result = loop.run("alpha")

    assert result.stop_reason == STOP_UNUSABLE_REPLY
    assert "down" in result.detail
    assert status_of(master) == "planned"


def test_the_engine_still_rejects_a_malformed_reply(master):
    """The loop absorbs it; the engine does not soften it."""
    from core.reasoning_engine import ReasoningError

    loop, provider, backend, verifier = build(
        master, [malformed("operation_type"), malformed("name")]
    )

    with pytest.raises(ReasoningError):
        loop.engine.parse(malformed("operation_type"), "x")


# --- an operation Master then refuses -----------------------------------


def test_a_refused_operation_stops_the_loop(master):
    # Structurally valid, so the engine accepts it, but Master refuses it:
    # there is no such task.
    loop, provider, backend, verifier = build(
        master,
        [
            act(
                {
                    "operation": "update_task",
                    "project_id": "alpha",
                    "task_id": "no-such-task",
                    "status": "in_progress",
                }
            )
        ],
    )

    result = loop.run("alpha")

    assert result.stop_reason == STOP_OPERATION_FAILED
    assert "update_task" in result.detail
    assert result.decision.decision is Decision.ACT
    # The loop did not go on to run the worker as if the task had started.
    assert backend.calls == []
    assert verifier.calls == []
    assert status_of(master) == "planned"


def test_an_unknown_operation_is_bounded_and_applies_nothing(master):
    loop, provider, backend, verifier = build(
        master,
        [
            act({"operation": "delete_everything", "project_id": "alpha"}),
            act({"operation": "delete_everything", "project_id": "alpha"}),
        ],
    )

    result = loop.run("alpha")

    # Unknown operations fail validation in the engine, so the loop treats the
    # reply as unusable rather than acting on it.
    assert result.stop_reason == STOP_UNUSABLE_REPLY
    assert len(provider.prompts) == 2
    assert backend.calls == []
    assert status_of(master) == "planned"


# --- the prompt tells Master the real shape ------------------------------


def test_the_prompt_shows_the_canonical_operation_shape(tmp_path):
    from core.master import Master
    from core.reasoning_engine import ReasoningEngine

    write_project(tmp_path, tasks=[task()])
    engine = ReasoningEngine(ScriptedProvider([]), Master(tmp_path))

    prompt = engine.build_prompt("do the thing", "alpha")

    # Flat, with the operation name under "operation" and arguments as siblings.
    assert '"operation": {' in prompt
    assert '"operation": "update_task"' in prompt
    assert '"operation": null' in prompt
    # And the exact wrong shapes seen in the experiment are called out.
    assert '"operation_type"' in prompt
    assert '"params"' in prompt


def test_the_prompt_example_is_accepted_by_the_real_parser(tmp_path):
    """Strongest form: the example printed in the prompt actually parses."""
    from core.master import Master
    from core.reasoning_engine import ReasoningEngine

    write_project(tmp_path, tasks=[task()])
    engine = ReasoningEngine(ScriptedProvider([]), Master(tmp_path))

    prompt = engine.build_prompt("do the thing", "alpha")

    start = prompt.index("This is valid:") + len("This is valid:")
    cursor = prompt.index("{", start)
    depth = 0
    for index in range(cursor, len(prompt)):
        if prompt[index] == "{":
            depth += 1
        elif prompt[index] == "}":
            depth -= 1
            if depth == 0:
                example = prompt[cursor: index + 1]
                break
    else:  # pragma: no cover - would mean the prompt lost its example
        raise AssertionError("no complete JSON example in the prompt")

    proposal = engine.parse(example, "x")

    assert proposal._decision.decision is Decision.ACT
    assert proposal._decision.operation.operation == "update_task"
    assert proposal._decision.operation.arguments == {
        "project_id": "p",
        "task_id": "t1",
        "status": "in_progress",
    }


def test_the_prompt_states_what_the_system_can_do(tmp_path):
    from core.master import Master
    from core.reasoning_engine import ReasoningEngine

    write_project(tmp_path, tasks=[task()])
    engine = ReasoningEngine(ScriptedProvider([]), Master(tmp_path))

    prompt = engine.build_prompt("do the thing", "alpha")

    lowered = prompt.lower()
    assert "what this system can do" in lowered
    assert "worker" in lowered
    # Provider-neutral: no model, service or product is named as the mechanism.
    for name in ("deepseek", "opencode", "qwen", "big pickle", "big-pickle", "ollama"):
        assert name not in lowered
