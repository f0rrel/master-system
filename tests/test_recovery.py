"""Tests for safe resume: errors, crashes, the project lock, and no replay.

The crash tests kill a real child process with ``os._exit`` while its worker
is running, so nothing in that process gets to clean up. Recovery then happens
in this process, which shares nothing with the dead one but the files.
"""

import json
import subprocess
import sys
import time
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
from core.paths import RuntimePaths
from core.recovery import STOP_INTERRUPTED, recover_project
from core.session_runner import STOP_ERROR, SessionRunner
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
        if mode == "subprocess":
            # A real worker process: it writes, then waits until released.
            sentinel = ROOT / "release-worker"
            workspace.run(["sh", "-c", "echo partial > partial.txt; "
                           f"while [ ! -f {{sentinel}} ]; do sleep 0.1; done"])
        if mode == "forever":
            workspace.run(["sh", "-c", "echo started > started.txt; "
                           "while true; do sleep 1; done"])
        return ExecutionResult(status="success", reason="ok")


class Verifier:
    def verify(self, task, context, evidence=None, workspace=None):
        return VerificationResult(verdict="pass", summary="checked")


SessionRunner(
    Master(ROOT / "projects"), Scripted(), Backend(), Verifier(),
    store=FileSessionStore(ROOT / "sessions"),
    history=SQLiteHistoryStore(ROOT / "history.sqlite"),
    attempt_timeout_s=3 if mode == "forever" else 1800,
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

    report = recover_project(r.history, "alpha", store=r.store)

    recovered = r.store.load("s1")
    assert report.stopped_sessions == ["s1"]
    assert recovered.status is SessionStatus.STOPPED
    assert recovered.last_stop_reason == STOP_INTERRUPTED
    assert recovered.steps_completed == 2


def test_a_running_session_with_no_history_recovers_without_inventing_events(tmp_path):
    write_project(tmp_path)
    store = FileSessionStore(tmp_path / "sessions")
    store.create(WorkSession.create("s1", "alpha", "finish t1"))
    store.save(store.load("s1").evolve(status=SessionStatus.RUNNING))
    r = runner(tmp_path, [])

    recover_project(r.history, "alpha", store=r.store)

    recovered = r.store.load("s1")
    assert recovered.status is SessionStatus.STOPPED
    assert recovered.last_stop_reason == STOP_INTERRUPTED
    assert recovered.steps_completed == 0
    assert r.history.events() == ()


def test_recovery_is_project_wide(tmp_path):
    """Resuming one session closes what any dead process left in the project."""
    write_project(tmp_path)
    run_child(tmp_path, "crash", [start(), run_task()])  # session s1 dies mid-attempt
    history = SQLiteHistoryStore(tmp_path / "history.sqlite")
    # A session-less run that also died.
    history.append(type=EventType.RUN_STARTED, run_id="orphan-run", project_id="alpha")
    store = FileSessionStore(tmp_path / "sessions")
    store.create(WorkSession.create("s2", "alpha", "something else"))

    r = runner(tmp_path, [wait()])
    r.resume("s2")

    interrupted = {e.run_id for e in r.history.events(types=[EventType.RUN_INTERRUPTED])}
    assert "orphan-run" in interrupted and len(interrupted) == 2
    assert len(r.history.events(types=[EventType.ATTEMPT_INTERRUPTED])) == 1
    assert r.store.load("s1").last_stop_reason == STOP_INTERRUPTED


def test_a_loop_taking_the_lock_itself_recovers_too(tmp_path):
    write_project(tmp_path)
    run_child(tmp_path, "crash", [start(), run_task()])
    history = SQLiteHistoryStore(tmp_path / "history.sqlite")
    from core.autonomous_loop import AutonomousLoop

    AutonomousLoop(Master(tmp_path / "projects"), Scripted([wait()]), Backend(),
                   Verifier(), history=history).run("alpha")

    assert len(history.events(types=[EventType.ATTEMPT_INTERRUPTED])) == 1


# --- A1: kill -9 of the loop while a subprocess worker runs -------------------------


def wait_for(predicate, timeout=20):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def lock_is_free(project):
    try:
        ProjectLock(project).acquire().release()
        return True
    except ProjectBusyError:
        return False


def start_child(root, mode, replies):
    return subprocess.Popen(
        [sys.executable, "-c", CHILD.format(repo=str(REPO), root=str(root)), mode,
         json.dumps(replies)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def worktree_file(name):
    root = RuntimePaths.default().worktrees_root
    found = list(root.glob(f"alpha/*/{name}"))
    return found[0] if found else None


def test_kill9_of_the_loop_keeps_the_lock_until_the_worker_exits(tmp_path):
    project = write_project(tmp_path)
    child = start_child(tmp_path, "subprocess", [start(), run_task()])
    try:
        assert wait_for(lambda: worktree_file("partial.txt") is not None), child.stderr
        worktree = worktree_file("partial.txt").parent

        child.kill()  # SIGKILL: no cleanup in the loop's process
        child.wait()

        # The worker inherited the lock, so the project is still busy...
        backend = Backend()
        r = runner(tmp_path, [wait()], backend=backend)
        with pytest.raises(ProjectBusyError):
            r.resume("s1")
        assert r.history.events(types=[EventType.ATTEMPT_INTERRUPTED]) == ()
    finally:
        (tmp_path / "release-worker").write_text("go")

    # ...until the worker exits.
    assert wait_for(lambda: lock_is_free(project))
    r.resume("s1")

    interrupted = r.history.events(types=[EventType.ATTEMPT_INTERRUPTED])
    assert len(interrupted) == 1
    facts = interrupted[0].payload
    assert facts["worktree"] == str(worktree)
    assert facts["worktree_present"] is True
    assert "?? partial.txt" in facts["git_status"]
    assert facts["untracked"] == 1
    assert "diffstat" in facts
    # The worktree is kept, and the worker was not run again.
    assert (worktree / "partial.txt").exists()
    assert backend.calls == []
    assert r.store.load("s1").status is SessionStatus.STOPPED


def test_an_orphaned_worker_is_still_stopped_at_its_deadline(tmp_path):
    project = write_project(tmp_path)
    child = start_child(tmp_path, "forever", [start(), run_task()])
    assert wait_for(lambda: worktree_file("started.txt") is not None)
    child.kill()
    child.wait()
    assert not lock_is_free(project)

    # Nothing is left to enforce the deadline except the worker's own wrapper.
    assert wait_for(lambda: lock_is_free(project), timeout=15)


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
