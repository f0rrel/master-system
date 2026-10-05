"""Tests for the session/loop integration and for process-independent resume.

The integration tests use the real Master, the real AutonomousLoop and the real
store. Only the three collaborators that would otherwise need a model or a
worker are faked, so what is under test is the wiring, not the fakes.

The resume tests genuinely start a second Python process. Re-loading a session
in the same interpreter would not prove the thing that matters, which is that
nothing needed survives in memory.
"""

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

from core.autonomous_loop import (
    STOP_APPROVAL,
    STOP_MASTER,
    STOP_NO_WORK,
    STOP_STEP_LIMIT,
    STOP_UNUSABLE_REPLY,
)
from core.execution import ExecutionResult
from core.master import Master
from core.provider import ReasoningProvider
from core.session_runner import SessionRunner
from core.session_store import FileSessionStore
from core.verification import VerificationResult
from core.work_session import SessionStatus

MILESTONE = {"id": "m1", "name": "M1", "status": "in_progress"}


# --- fixtures -------------------------------------------------------------


@pytest.fixture
def projects(tmp_path):
    return _write_project(tmp_path)


def _write_project(root, project_id="alpha"):
    project = root / f"{project_id}-project"
    project.mkdir(parents=True)
    (project / "project.yaml").write_text(
        yaml.safe_dump({"id": project_id, "name": project_id, "status": "active"})
    )
    (project / "milestones.yaml").write_text(
        yaml.safe_dump({"milestones": [MILESTONE]})
    )
    (project / "tasks.yaml").write_text(
        yaml.safe_dump(
            {
                "tasks": [
                    {
                        "id": "t1",
                        "milestone": "m1",
                        "title": "Task t1",
                        "status": "planned",
                        "assigned_to": "master",
                    }
                ]
            }
        )
    )
    return project.parent


class ScriptedProvider(ReasoningProvider):
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
        self.calls.append(dict(task)["id"])
        return self._result


class FakeVerificationBackend:
    def __init__(self, result=None):
        self._result = result or VerificationResult(
            verdict="pass", summary="looks good"
        )
        self.calls = []

    def verify(self, task, context, evidence=None):
        self.calls.append(dict(task)["id"])
        return self._result


def act(operation, reason="onwards"):
    return json.dumps({"decision": "act", "reason": reason, "operation": operation})


def start_task(task_id="t1", status="in_progress"):
    return act(
        {
            "operation": "update_task",
            "project_id": "alpha",
            "task_id": task_id,
            "status": status,
        }
    )


def no_op(decision="wait"):
    return json.dumps({"decision": decision, "reason": "nothing to do"})


def make_runner(projects, replies, tmp_path, **kwargs):
    return SessionRunner(
        Master(projects),
        ScriptedProvider(replies),
        FakeExecutionBackend(),
        FakeVerificationBackend(),
        store=FileSessionStore(tmp_path / "sessions"),
        **kwargs,
    )


# --- starting a session ---------------------------------------------------


def test_start_runs_the_loop_and_persists_a_session(projects, tmp_path):
    runner = make_runner(
        projects, [start_task(), start_task(status="completed")], tmp_path
    )

    session = runner.start("alpha", "finish t1", "run-1")

    assert session.session_id == "run-1"
    assert session.objective == "finish t1"
    assert runner.store.load("run-1") == session


def test_start_records_the_stop_reason(projects, tmp_path):
    runner = make_runner(projects, [start_task(status="completed")], tmp_path)

    session = runner.start("alpha", "finish t1", "run-1")

    assert session.last_stop_reason == STOP_NO_WORK
    assert session.status is SessionStatus.STOPPED


def test_start_records_the_latest_progress(projects, tmp_path):
    runner = make_runner(
        projects, [start_task(), start_task(status="completed")], tmp_path
    )

    session = runner.start("alpha", "finish t1", "run-1")

    assert session.last_progress == {
        "task_id": "t1",
        "execution_status": "success",
        "verification_verdict": "pass",
        "summary": "looks good",
    }
    assert session.last_task_id == "t1"


def test_start_counts_the_steps_it_took(projects, tmp_path):
    runner = make_runner(
        projects, [start_task(), start_task(status="completed")], tmp_path
    )

    session = runner.start("alpha", "finish t1", "run-1")

    assert session.steps_completed == 2


def test_the_session_never_records_task_or_project_state(projects, tmp_path):
    runner = make_runner(
        projects, [start_task(), start_task(status="completed")], tmp_path
    )

    session = runner.start("alpha", "finish t1", "run-1")
    stored = yaml.safe_load((tmp_path / "sessions" / "run-1.yaml").read_text())

    assert "title" not in json.dumps(stored)
    assert set(stored) == {
        "session_id", "project_id", "objective", "status", "created_at",
        "started_at", "updated_at", "steps_completed", "last_task_id",
        "last_progress", "pending_approval", "last_stop_reason",
    }


def test_a_session_holds_no_task_list_of_its_own(projects, tmp_path):
    runner = make_runner(
        projects, [start_task(), start_task(status="completed")], tmp_path
    )

    runner.start("alpha", "finish t1", "run-1")

    stored = yaml.safe_load((tmp_path / "sessions" / "run-1.yaml").read_text())

    assert not isinstance(stored.get("tasks"), list)
    assert "Task t1" not in json.dumps(stored)


def test_master_still_owns_the_task_status(projects, tmp_path):
    runner = make_runner(
        projects, [start_task(), start_task(status="completed")], tmp_path
    )

    runner.start("alpha", "finish t1", "run-1")

    master = Master(projects)
    assert master.status("alpha")["tasks"][0]["status"] == "completed"


# --- stop reasons map onto session states --------------------------------


def test_a_run_that_ran_out_of_steps_is_stopped(projects, tmp_path):
    runner = make_runner(
        projects, [start_task()] * 3, tmp_path, max_steps=2
    )

    session = runner.start("alpha", "keep going", "run-1")

    assert session.last_stop_reason == STOP_STEP_LIMIT
    assert session.status is SessionStatus.STOPPED


def test_an_unusable_reply_is_recorded(projects, tmp_path):
    bad = json.dumps(
        {
            "decision": "act",
            "reason": "x",
            "operation": {"operation_type": "update_task"},
        }
    )
    runner = make_runner(projects, [bad, bad], tmp_path)

    session = runner.start("alpha", "try", "run-1")

    assert session.last_stop_reason == STOP_UNUSABLE_REPLY
    assert session.status is SessionStatus.STOPPED


def test_a_gated_decision_is_recorded_as_needing_a_human():
    """Exercised directly, because no production operation is gated today.

    Every entry in SPECS is ROUTINE, so AutonomousLoop cannot currently reach
    STOP_APPROVAL at all. Testing the recording path through the real loop
    would mean gating an operation globally to force it, which would test the
    test rather than the code.
    """
    from core.autonomous_loop import LoopResult
    from core.reasoning import ApprovalState, Decision, MasterDecision, Operation
    from core.work_session import WorkSession

    gated = MasterDecision(
        decision=Decision.ACT,
        operation=Operation("unlisted_capability", {}, ApprovalState.PROPOSED),
    )
    assert gated.pending_approval is True, "an unclassified operation is gated"

    running = WorkSession.create("run-1", "alpha", "needs a human").evolve(
        status=SessionStatus.RUNNING
    )
    recorded = SessionRunner._apply_result(
        running, LoopResult(stop_reason=STOP_APPROVAL, decision=gated)
    )

    assert recorded.status is SessionStatus.NEEDS_HUMAN
    assert recorded.last_stop_reason == STOP_APPROVAL
    assert recorded.pending_approval == {
        "operation": "unlisted_capability",
        "approval_state": "proposed",
        "decision": "act",
    }


def test_no_operation_is_gated_so_the_approval_stop_is_unreachable_today():
    """A fact about the current policy, asserted so a change is noticed."""
    from core.reasoning import SPECS, ImpactLevel

    gated = [n for n, s in SPECS.items() if s.impact is not ImpactLevel.ROUTINE]

    assert gated == [], f"operations became gated: {gated}"


def test_a_stale_pending_request_is_cleared_by_a_later_run(projects, tmp_path):
    """A resumed session must not keep reporting a human request already handled."""
    from core.autonomous_loop import LoopResult
    from core.reasoning import ApprovalState, Decision, MasterDecision, Operation
    from core.work_session import WorkSession

    gated = MasterDecision(
        decision=Decision.ACT,
        operation=Operation("unlisted_capability", {}, ApprovalState.PROPOSED),
    )
    assert gated.pending_approval is True, "an unclassified operation is gated"

    running = WorkSession.create("run-1", "alpha", "ask").evolve(
        status=SessionStatus.RUNNING
    )
    waiting = SessionRunner._apply_result(
        running, LoopResult(stop_reason=STOP_APPROVAL, decision=gated)
    )
    assert waiting.pending_approval["operation"] == "unlisted_capability"
    assert waiting.status is SessionStatus.NEEDS_HUMAN

    moved_on = SessionRunner._apply_result(
        waiting, LoopResult(stop_reason=STOP_NO_WORK, steps=1)
    )

    assert moved_on.pending_approval is None
    assert moved_on.status is SessionStatus.STOPPED


# --- provider neutrality -------------------------------------------------


BACKEND_REASON = "Ollama model completed the task (qwen3:8b via ollama)"


def test_a_backend_reason_naming_its_runtime_never_reaches_the_session(
    projects, tmp_path
):
    runner = make_runner(
        projects, [start_task(), start_task(status="completed")], tmp_path
    )
    runner._execution_backend = FakeExecutionBackend(
        ExecutionResult(status="success", reason=BACKEND_REASON)
    )

    session = runner.start("alpha", "finish t1", "run-1")

    stored = (tmp_path / "sessions" / "run-1.yaml").read_text().lower()
    for word in ("ollama", "qwen3", "qwen", "opencode", "big pickle"):
        assert word not in stored


def test_the_session_module_names_no_provider():
    for module in ("work_session", "session_store", "session_runner"):
        text = (Path("core") / f"{module}.py").read_text().lower()
        for word in ("deepseek", "opencode", "qwen3:8b", "big pickle"):
            assert word not in text, f"{module} mentions {word}"


def test_orchestration_does_not_import_concrete_adapters():
    """The dependency is injected, so no module may reach a concrete adapter."""
    import ast

    adapters = {"ollama_backend", "opencode_backend", "ollama_provider",
                "opencode_provider", "deepseek_provider"}
    modules = ["autonomous_loop", "session_runner", "work_session", "session_store",
               "execution_runner", "task_orchestrator", "master", "reasoning_engine",
               "reasoning", "execution", "verification"]
    offenders = []
    for name in modules:
        tree = ast.parse((Path("core") / f"{name}.py").read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for imported in names:
                if imported.rsplit(".", 1)[-1] in adapters:
                    offenders.append(f"{name} imports {imported}")
    assert not offenders, offenders


# --- resume --------------------------------------------------------------


def test_resuming_a_session_runs_the_loop_again(projects, tmp_path):
    # First run starts the task and then declines to continue, so there is
    # genuinely something left for the second run to do.
    runner = make_runner(projects, [start_task(), no_op("wait")], tmp_path)
    runner.start("alpha", "finish t1", "run-1")

    runner2 = make_runner(projects, [start_task(status="completed")], tmp_path)
    session = runner2.resume("run-1")

    assert session.steps_completed == 3, "the resumed run added its own step"
    assert session.last_stop_reason == STOP_NO_WORK
    assert session.started_at is not None


def test_resuming_accumulates_steps_across_runs(projects, tmp_path):
    first = make_runner(projects, [start_task(), no_op("wait")], tmp_path)
    first.start("alpha", "finish t1", "run-1")
    assert first.store.load("run-1").steps_completed == 2

    for _ in range(2):
        again = make_runner(projects, [no_op("wait")], tmp_path, max_steps=1)
        again.resume("run-1")

    assert again.store.load("run-1").steps_completed == 4


def test_resuming_an_unknown_session_raises(projects, tmp_path):
    runner = make_runner(projects, [], tmp_path)

    from core.session_store import UnknownSessionError

    with pytest.raises(UnknownSessionError):
        runner.resume("never-existed")


def test_a_completed_session_cannot_be_resumed(projects, tmp_path):
    runner = make_runner(projects, [], tmp_path)
    session = runner.store.create(
        __import__("core.work_session", fromlist=["WorkSession"])
        .WorkSession.create("run-1", "alpha", "done")
        .evolve(status=SessionStatus.RUNNING)
        .evolve(status=SessionStatus.COMPLETED)
    )

    with pytest.raises(ValueError, match="cannot be resumed"):
        runner.resume(session.session_id)


def test_the_objective_is_reused_as_the_standing_request(projects, tmp_path):
    runner = make_runner(projects, [start_task(), no_op("wait")], tmp_path)

    runner.start("alpha", "make the tests pass", "run-1")

    assert "make the tests pass" in runner._provider.prompts[0]


def test_an_explicit_request_overrides_the_objective(projects, tmp_path):
    runner = make_runner(
        projects, [start_task(), no_op("wait")], tmp_path,
        request="use a different wording",
    )

    runner.start("alpha", "objective text", "run-1")

    assert "use a different wording" in runner._provider.prompts[0]


# --- process-independent resume ------------------------------------------


RESUME_SCRIPT = '''
import json, sys
sys.path.insert(0, {repo!r})
from pathlib import Path
import yaml

from core.execution import ExecutionResult
from core.master import Master
from core.provider import ReasoningProvider
from core.session_runner import SessionRunner
from core.session_store import FileSessionStore
from core.verification import VerificationResult

ROOT = Path({root!r})
PROJECTS = ROOT / "projects"
SESSIONS = ROOT / "sessions"

MILESTONE = {{"id": "m1", "name": "M1", "status": "in_progress"}}


def task(task_id, status):
    return {{
        "id": task_id, "milestone": "m1", "title": "Task " + task_id,
        "status": status, "assigned_to": "master",
    }}


def write_project():
    project = PROJECTS / "alpha-project"
    project.mkdir(parents=True, exist_ok=True)
    (project / "project.yaml").write_text(
        yaml.safe_dump({{"id": "alpha", "name": "alpha", "status": "active"}})
    )
    (project / "milestones.yaml").write_text(
        yaml.safe_dump({{"milestones": [MILESTONE]}})
    )
    (project / "tasks.yaml").write_text(
        yaml.safe_dump({{"tasks": [task("t1", "planned"), task("t2", "planned")]}})
    )


class Once(ReasoningProvider):
    name = "once"

    def __init__(self, replies):
        self._replies = list(replies)

    def complete(self, prompt, schema=None):
        assert self._replies, "provider called more often than scripted"
        return self._replies.pop(0)


class Backend:
    def __init__(self):
        self.calls = []

    def execute(self, task, context):
        self.calls.append(task["id"])
        return ExecutionResult(status="success", reason="ok")


class Verifier:
    def verify(self, task, context, evidence=None):
        return VerificationResult(verdict="pass", summary="looks good")


def act(task_id, status):
    return json.dumps({{
        "decision": "act",
        "reason": "onwards",
        "operation": {{
            "operation": "update_task",
            "project_id": "alpha",
            "task_id": task_id,
            "status": status,
        }},
    }})


def runner(replies, backend):
    return SessionRunner(
        Master(PROJECTS), Once(replies), Backend() if backend is None else backend,
        Verifier(), store=FileSessionStore(SESSIONS), max_steps=4,
    )


phase = sys.argv[1]

if phase == "start":
    write_project()
    # Finish t1, then decline to continue: the session ends with real work
    # still outstanding, which is what makes the next process meaningful.
    session = runner(
        [act("t1", "in_progress"), act("t1", "completed"),
         json.dumps({{"decision": "wait", "reason": "stopping here"}})],
        None,
    ).start("alpha", "finish t1", "durable-1")
    print(json.dumps({{
        "session_id": session.session_id,
        "status": session.status.value,
        "steps": session.steps_completed,
        "stop_reason": session.last_stop_reason,
        "task_id": session.last_task_id,
        "progress": session.last_progress,
        "worker_calls": 1,
    }}))
elif phase == "inspect":
    # A brand-new interpreter: load only, and report what it can tell without
    # having run anything itself.
    store = FileSessionStore(SESSIONS)
    session = store.load("durable-1")
    master = Master(PROJECTS)
    print(json.dumps({{
        "session_id": session.session_id,
        "objective": session.objective,
        "status": session.status.value,
        "steps": session.steps_completed,
        "stop_reason": session.last_stop_reason,
        "task_id": session.last_task_id,
        "progress": session.last_progress,
        "master_task_status": master.status("alpha")["tasks"][0]["status"],
        "outstanding": [t["id"] for t in master.status("alpha")["tasks"]
                        if t["status"] == "planned"],
    }}))
elif phase == "resume":
    backend = Backend()
    session = runner(
        [act("t2", "in_progress"), act("t2", "completed")], backend
    ).resume("durable-1")
    print(json.dumps({{
        "status": session.status.value,
        "steps": session.steps_completed,
        "task_id": session.last_task_id,
        "worker_calls": len(backend.calls),
    }}))
'''


def _run_phase(root, phase):
    script = Path(root) / "phase.py"
    if not script.exists():
        script.write_text(
            textwrap.dedent(RESUME_SCRIPT).format(
                repo=str(Path(__file__).resolve().parent.parent), root=str(root)
            )
        )
    done = subprocess.run(
        [sys.executable, str(script), phase],
        capture_output=True, text=True, cwd=str(root),
    )
    assert done.returncode == 0, f"phase {phase} failed:\n{done.stderr}"
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_a_session_survives_the_process_that_made_it(tmp_path):
    started = _run_phase(tmp_path, "start")
    assert started["steps"] == 3
    assert started["stop_reason"] == STOP_MASTER

    # A different interpreter, with no shared memory of the first one.
    inspected = _run_phase(tmp_path, "inspect")

    assert inspected["session_id"] == "durable-1"
    assert inspected["objective"] == "finish t1"
    assert inspected["steps"] == 3
    assert inspected["stop_reason"] == STOP_MASTER
    assert inspected["task_id"] == "t1"
    assert inspected["progress"]["verification_verdict"] == "pass"
    # Authoritative state comes from Master, not from the session.
    assert inspected["master_task_status"] == "completed"
    assert inspected["outstanding"] == ["t2"]


def test_work_continues_in_a_fresh_process_from_persisted_state(tmp_path):
    _run_phase(tmp_path, "start")

    resumed = _run_phase(tmp_path, "resume")

    assert resumed["steps"] == 5, "the resumed run added its own two steps"
    assert resumed["worker_calls"] == 1, "a worker really ran in the new process"
    assert resumed["task_id"] == "t2", "the resumed run picked up the outstanding task"
    assert resumed["status"] == "stopped"


def test_the_success_criterion_end_to_end(tmp_path):
    """Start, persist, exit, fresh runtime, load, continue -- no shared memory."""
    _run_phase(tmp_path, "start")
    _run_phase(tmp_path, "inspect")
    final = _run_phase(tmp_path, "resume")

    assert final["steps"] == 5
    assert final["worker_calls"] == 1
