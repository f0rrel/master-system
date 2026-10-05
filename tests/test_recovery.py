"""Tests for safe resume: errors, crashes, the project lock, and no replay.

The crash tests kill a real child process with ``os._exit`` while its worker
is running, so nothing in that process gets to clean up. Recovery then happens
in this process, which shares nothing with the dead one but the files.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from conftest import attach_repository
from core.autonomous_loop import STOP_MASTER
from core.execution import ExecutionResult
from core.history import EventType
from core.master import Master
from core.provider import ReasoningProvider
from core.run_lock import ProjectBusyError, ProjectLock
from core.session_runner import STOP_ERROR, STOP_INTERRUPTED, SessionRunner
from core.session_store import FileSessionStore
from core.sqlite_history import SQLiteHistoryStore
from core.verification import VerificationResult
from core.work_session import SessionStatus, WorkSession

REPO = Path(__file__).resolve().parent.parent


# --- shared pieces --------------------------------------------------------


def write_project(root):
    project = root / "projects" / "alpha-project"
    project.mkdir(parents=True)
    (project / "project.yaml").write_text(
        yaml.safe_dump({"id": "alpha", "name": "alpha", "status": "active"})
    )
    (project / "milestones.yaml").write_text(yaml.safe_dump(
        {"milestones": [{"id": "m1", "name": "M1", "status": "in_progress"}]}
    ))
    (project / "tasks.yaml").write_text(yaml.safe_dump({"tasks": [
        {"id": "t1", "milestone": "m1", "title": "T1", "status": "planned",
         "assigned_to": "master"}
    ]}))
    attach_repository(project)
    return project


def act(operation):
    return json.dumps({"decision": "act", "reason": "onwards", "operation": operation})


def start():
    return act({"operation": "update_task", "project_id": "alpha", "task_id": "t1",
                "status": "in_progress"})


def run_task():
    return act({"operation": "run_task", "project_id": "alpha", "task_id": "t1"})


def wait():
    return json.dumps({"decision": "wait", "reason": "look first", "operation": None})


class Scripted(ReasoningProvider):
    name = "scripted"

    def __init__(self, replies):
        self._replies = list(replies)
        self.prompts = []

    def complete(self, prompt, schema=None):
        self.prompts.append(prompt)
        assert self._replies, "provider called more often than scripted"
        return self._replies.pop(0)


class Backend:
    def __init__(self, error=None):
        self._error = error
        self.calls = []

    def execute(self, task, context, workspace=None):
        self.calls.append(task["id"])
        if self._error is not None:
            raise self._error
        return ExecutionResult(status="success", reason="ok")


class Verifier:
    def verify(self, task, context, evidence=None, workspace=None):
        return VerificationResult(verdict="pass", summary="checked")


def runner(root, replies, backend=None, **kwargs):
    return SessionRunner(
        Master(root / "projects"),
        Scripted(replies),
        backend or Backend(),
        Verifier(),
        store=FileSessionStore(root / "sessions"),
        history=SQLiteHistoryStore(root / "history.sqlite"),
        **kwargs,
    )


def types(history, **filters):
    return [e.type for e in history.events(**filters)]


def context_of(prompt):
    body = prompt.split("PROJECT STATE\n", 1)[1].split("\n\nADDITIONAL RULES", 1)[0]
    return json.loads(body)


# --- an error is recorded, never left as RUNNING --------------------------


@pytest.mark.parametrize("error", [RuntimeError("worker died"), KeyboardInterrupt()])
def test_a_failing_run_stops_the_session_with_error(tmp_path, error):
    write_project(tmp_path)
    r = runner(tmp_path, [start(), run_task()], backend=Backend(error=error))

    with pytest.raises(type(error)):
        r.start("alpha", "finish t1", "s1")

    session = r.store.load("s1")
    assert session.status is SessionStatus.STOPPED
    assert session.last_stop_reason == STOP_ERROR
    assert session.steps_completed == 2
    assert types(r.history)[-3:] == [
        EventType.ATTEMPT_STARTED, EventType.ATTEMPT_FINISHED, EventType.RUN_ERROR,
    ]


def test_the_lock_is_released_after_an_error(tmp_path):
    project = write_project(tmp_path)
    r = runner(tmp_path, [start(), run_task()], backend=Backend(error=RuntimeError("x")))

    with pytest.raises(RuntimeError):
        r.start("alpha", "finish t1", "s1")

    with ProjectLock(project):
        pass


# --- a hard crash in another process --------------------------------------


CHILD = '''
import json, os, sys
sys.path.insert(0, {repo!r})
from pathlib import Path
from core.execution import ExecutionResult
from core.master import Master
from core.provider import ReasoningProvider
from core.session_runner import SessionRunner
from core.session_store import FileSessionStore
from core.sqlite_history import SQLiteHistoryStore
from core.verification import VerificationResult

ROOT = Path({root!r})
mode, replies = sys.argv[1], json.loads(sys.argv[2])


class Scripted(ReasoningProvider):
    name = "scripted"
    def complete(self, prompt, schema=None):
        return replies.pop(0)


class Backend:
    def execute(self, task, context, workspace=None):
        if mode == "crash":
            (ROOT / "worker-touched-workspace").write_text("half done")
            os._exit(9)
        return ExecutionResult(status="success", reason="ok")


class Verifier:
    def verify(self, task, context, evidence=None, workspace=None):
        return VerificationResult(verdict="pass", summary="checked")


SessionRunner(
    Master(ROOT / "projects"), Scripted(), Backend(), Verifier(),
    store=FileSessionStore(ROOT / "sessions"),
    history=SQLiteHistoryStore(ROOT / "history.sqlite"),
).start("alpha", "finish t1", "s1")
'''


def run_child(root, mode, replies):
    return subprocess.run(
        [sys.executable, "-c", CHILD.format(repo=str(REPO), root=str(root)), mode,
         json.dumps(replies)],
        capture_output=True, text=True,
    )


def test_a_crashed_attempt_is_recovered_as_interrupted_and_not_replayed(tmp_path):
    write_project(tmp_path)
    crashed = run_child(tmp_path, "crash", [start(), run_task()])
    assert crashed.returncode == 9, crashed.stderr
    assert (tmp_path / "worker-touched-workspace").exists()
    assert FileSessionStore(tmp_path / "sessions").load("s1").status is SessionStatus.RUNNING

    backend = Backend()
    r = runner(tmp_path, [wait()], backend=backend)
    session = r.resume("s1")

    # Recovery closed out what the dead process left open...
    history = r.history
    interrupted = history.events(types=[EventType.ATTEMPT_INTERRUPTED])
    assert len(interrupted) == 1
    started = history.events(types=[EventType.ATTEMPT_STARTED])
    assert interrupted[0].attempt_id == started[0].attempt_id
    assert len(history.events(types=[EventType.RUN_INTERRUPTED])) == 1
    # ...did not run the worker again on its own...
    assert backend.calls == []
    # ...and handed the uncertainty to Master as evidence.
    evidence = context_of(r._provider.prompts[0])["execution_evidence"]["tasks"]["t1"]
    assert evidence["latest_attempt"]["outcome"] == "interrupted"
    assert evidence["latest_attempt"]["verification"] is None
    assert evidence["attempts_this_session"] == 1
    # The crashed run's two decisions plus the resumed run's one.
    assert session.steps_completed == 3
    assert session.last_stop_reason == STOP_MASTER
    assert Master(tmp_path / "projects").status("alpha")["tasks"][0]["status"] == "in_progress"


def test_rerunning_after_a_crash_is_an_explicit_counted_decision(tmp_path):
    write_project(tmp_path)
    run_child(tmp_path, "crash", [start(), run_task()])

    backend = Backend()
    r = runner(tmp_path, [run_task(), wait()], backend=backend)
    r.resume("s1")

    assert backend.calls == ["t1"]
    evidence = context_of(r._provider.prompts[1])["execution_evidence"]["tasks"]["t1"]
    assert evidence["attempts_this_session"] == 2
    assert evidence["latest_attempt"]["outcome"] == "finished"
    assert evidence["latest_attempt"]["worker_reported_status"] == "success"


def test_recovery_is_recorded_once(tmp_path):
    write_project(tmp_path)
    run_child(tmp_path, "crash", [start(), run_task()])

    runner(tmp_path, [wait()]).resume("s1")
    r = runner(tmp_path, [wait()])
    r.resume("s1")

    assert len(r.history.events(types=[EventType.RUN_INTERRUPTED])) == 1
    assert len(r.history.events(types=[EventType.ATTEMPT_INTERRUPTED])) == 1


def test_recovery_closes_the_dead_run_and_counts_its_steps(tmp_path):
    write_project(tmp_path)
    run_child(tmp_path, "crash", [start(), run_task()])
    r = runner(tmp_path, [])

    recovered = r._recover(r.store.load("s1"))

    assert recovered.status is SessionStatus.STOPPED
    assert recovered.last_stop_reason == STOP_INTERRUPTED
    assert recovered.steps_completed == 2
    assert r.store.load("s1") == recovered


def test_a_running_session_with_no_history_recovers_without_inventing_events(tmp_path):
    write_project(tmp_path)
    store = FileSessionStore(tmp_path / "sessions")
    store.create(WorkSession.create("s1", "alpha", "finish t1"))
    store.save(store.load("s1").evolve(status=SessionStatus.RUNNING))
    r = runner(tmp_path, [])

    recovered = r._recover(r.store.load("s1"))

    assert recovered.status is SessionStatus.STOPPED
    assert recovered.last_stop_reason == STOP_INTERRUPTED
    assert recovered.steps_completed == 0
    assert r.history.events() == ()


# --- the lock decides whether a session is alive -------------------------


HOLDER = '''
import sys
sys.path.insert(0, {repo!r})
from core.run_lock import ProjectLock
lock = ProjectLock({path!r}, holder="session=s1").acquire()
print("locked", flush=True)
sys.stdin.readline()
'''


def test_resuming_while_another_process_holds_the_project_changes_nothing(tmp_path):
    project = write_project(tmp_path)
    store = FileSessionStore(tmp_path / "sessions")
    store.create(WorkSession.create("s1", "alpha", "finish t1"))
    store.save(store.load("s1").evolve(status=SessionStatus.RUNNING))
    session_file = tmp_path / "sessions" / "s1.yaml"
    before = session_file.read_bytes()

    child = subprocess.Popen(
        [sys.executable, "-c", HOLDER.format(repo=str(REPO), path=str(project))],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        assert child.stdout.readline().strip() == "locked"
        backend = Backend()
        r = runner(tmp_path, [], backend=backend)
        with pytest.raises(ProjectBusyError):
            r.resume("s1")
    finally:
        child.kill()
        child.wait()

    assert session_file.read_bytes() == before
    assert r.history.events() == ()
    assert backend.calls == []


def test_starting_while_the_project_is_held_creates_nothing(tmp_path):
    project = write_project(tmp_path)
    r = runner(tmp_path, [])

    with ProjectLock(project):
        with pytest.raises(ProjectBusyError):
            r.start("alpha", "finish t1", "s1")

    assert r.store.list_sessions() == ()


# --- evidence survives the process that produced it -----------------------


def test_a_resumed_process_sees_the_previous_verification(tmp_path):
    write_project(tmp_path)
    done = run_child(tmp_path, "ok", [start(), run_task(), wait()])
    assert done.returncode == 0, done.stderr

    r = runner(tmp_path, [wait()])
    r.resume("s1")

    evidence = context_of(r._provider.prompts[0])["execution_evidence"]["tasks"]["t1"]
    assert evidence["latest_attempt"]["outcome"] == "finished"
    assert evidence["latest_attempt"]["spec_current"] is True
    assert evidence["latest_attempt"]["verification"] == {
        "verdict": "pass", "summary": "checked", "findings": []}
    recent = context_of(r._provider.prompts[0])["recent_decisions"]
    assert [d["operation"] for d in recent] == ["update_task", "run_task", None]


def test_the_attempt_budget_persists_across_resumes(tmp_path):
    write_project(tmp_path)
    run_child(tmp_path, "ok", [start(), run_task(), run_task(), wait()])

    backend = Backend()
    r = runner(tmp_path, [run_task(), run_task()], backend=backend,
               max_attempts_per_task=3)
    session = r.resume("s1")

    assert backend.calls == ["t1"]
    assert session.status is SessionStatus.NEEDS_HUMAN
    assert session.last_stop_reason == "attempt_limit"


def test_a_completion_after_resume_uses_the_earlier_pass(tmp_path):
    write_project(tmp_path)
    run_child(tmp_path, "ok", [start(), run_task(), wait()])

    r = runner(tmp_path, [act({"operation": "update_task", "project_id": "alpha",
                               "task_id": "t1", "status": "completed"})])
    r.resume("s1")

    assert Master(tmp_path / "projects").status("alpha")["tasks"][0]["status"] == "completed"
