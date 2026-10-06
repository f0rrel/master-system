"""The orchestrator owns each attempt's workspace, its deadline and its facts.

Scenario tests with real git worktrees and real processes.
"""

import json
import os
import sys
import time
from pathlib import Path

import pytest
import yaml

from conftest import attach_repository, git, make_repo
from core.autonomous_loop import STOP_OPERATION_FAILED, AutonomousLoop
from core.execution import ExecutionResult
from core.history import EventType, InMemoryHistoryStore
from core.master import Master
from core.provider import ReasoningProvider
from core.verification import VerificationResult
from core.workspace import GitWorktrees, WorkspaceError

MILESTONE = {"id": "m1", "name": "M1", "status": "in_progress"}


def write_project(root, repository=True, status="in_progress"):
    project = root / "alpha-project"
    project.mkdir(parents=True)
    (project / "project.yaml").write_text(
        yaml.safe_dump({"id": "alpha", "name": "alpha", "status": "active"})
    )
    (project / "milestones.yaml").write_text(yaml.safe_dump({"milestones": [MILESTONE]}))
    (project / "tasks.yaml").write_text(yaml.safe_dump({"tasks": [
        {"id": "t1", "milestone": "m1", "title": "T1", "status": status}
    ]}))
    repo = attach_repository(project) if repository else None
    return project, repo


class Scripted(ReasoningProvider):
    name = "scripted"

    def __init__(self, replies):
        self._replies = list(replies)

    def complete(self, prompt, schema=None):
        return self._replies.pop(0)


RUN = json.dumps({"decision": "act", "reason": "go", "operation":
                  {"operation": "run_task", "project_id": "alpha", "task_id": "t1"}})
WAIT = json.dumps({"decision": "wait", "reason": "look", "operation": None})


class Recorder:
    """A verifier that records what it was given."""

    def __init__(self, verdict="pass"):
        self.verdict = verdict
        self.calls = []

    def verify(self, task, context, evidence=None, *, workspace):
        self.calls.append({"evidence": dict(evidence or {}), "path": workspace.path,
                           "base": workspace.base_sha, "result": workspace.result_sha})
        return VerificationResult(verdict=self.verdict, summary="checked")


def run_once(tmp_path, backend, verifier=None, **kwargs):
    project, repo = write_project(tmp_path)
    history = InMemoryHistoryStore()
    verifier = verifier or Recorder()
    loop = AutonomousLoop(Master(tmp_path), Scripted([RUN, WAIT]), backend, verifier,
                          history=history, **kwargs)
    result = loop.run("alpha")
    return result, history, repo, verifier, project


def payload(history, event_type):
    events = history.events(types=[event_type])
    return events[-1].payload if events else None


class Writes:
    """A worker that edits files in the workspace it was given."""

    def __init__(self, files=None, artifacts=None):
        if files is None:
            files = {"app.py": "def greet(name):\n    return 'Hello ' + name\n"}
        self.files = files
        self.artifacts = artifacts or {}
        self.seen = []

    def execute(self, task, context, *, workspace):
        self.seen.append(workspace.path)
        for name, content in self.files.items():
            (workspace.path / name).write_text(content)
        return ExecutionResult(status="success", reason="done", artifacts=self.artifacts)


# --- the workspace ------------------------------------------------------------


def test_an_attempt_runs_in_its_own_worktree_and_its_result_is_committed(tmp_path):
    worker = Writes()
    result, history, repo, verifier, _ = run_once(tmp_path, worker)

    started = payload(history, EventType.ATTEMPT_STARTED)
    finished = payload(history, EventType.ATTEMPT_FINISHED)
    worktree = Path(started["worktree"])
    assert worker.seen == [worktree]
    assert started["base_sha"] == git(repo, "rev-parse", "main")
    assert started["branch"] == f"attempt/{history.events()[2].attempt_id}"
    assert started["repository"] == str(repo.resolve())
    assert finished["outcome"] == "finished"
    assert finished["worker_reported_status"] == "success"
    assert finished["result_sha"] == git(worktree, "rev-parse", "HEAD") != started["base_sha"]
    assert finished["files_changed"] == ["app.py"]
    assert finished["diffstat"] == {"files": 1, "insertions": 1, "deletions": 1}
    # The worktree is kept, and the owner's checkout and branch are untouched.
    assert worktree.is_dir()
    assert git(repo, "rev-parse", "main") == started["base_sha"]
    assert "Hello" not in (repo / "app.py").read_text()


def test_a_worker_that_changes_nothing_has_the_base_as_its_result(tmp_path):
    result, history, *_ = run_once(tmp_path, Writes(files={}))

    started = payload(history, EventType.ATTEMPT_STARTED)
    finished = payload(history, EventType.ATTEMPT_FINISHED)
    assert finished["result_sha"] == started["base_sha"]
    assert finished["files_changed"] == []


def test_the_attempt_is_recorded_before_the_worktree_exists(tmp_path, monkeypatch):
    seen = {}

    def failing_create(self, repository, path, attempt_id, base_sha):
        seen["recorded_first"] = [e.type for e in history.events()]
        seen["exists"] = Path(path).exists()
        raise WorkspaceError("disk full")

    project, _ = write_project(tmp_path)
    history = InMemoryHistoryStore()
    monkeypatch.setattr(GitWorktrees, "create", failing_create)
    loop = AutonomousLoop(Master(tmp_path), Scripted([RUN]), Writes(), Recorder(),
                          history=history)

    with pytest.raises(WorkspaceError):
        loop.run("alpha")

    assert seen["recorded_first"][-1] is EventType.ATTEMPT_STARTED
    assert seen["exists"] is False
    finished = payload(history, EventType.ATTEMPT_FINISHED)
    assert finished["outcome"] == "error" and finished["error_type"] == "WorkspaceError"


def test_after_a_raise_the_partial_work_is_recorded(tmp_path):
    class Breaks:
        def execute(self, task, context, *, workspace):
            (workspace.path / "half.txt").write_text("half done\n")
            raise RuntimeError("worker died")

    project, _ = write_project(tmp_path)
    history = InMemoryHistoryStore()
    loop = AutonomousLoop(Master(tmp_path), Scripted([RUN]), Breaks(), Recorder(),
                          history=history)
    with pytest.raises(RuntimeError):
        loop.run("alpha")

    finished = payload(history, EventType.ATTEMPT_FINISHED)
    assert finished["outcome"] == "error"
    assert finished["files_changed"] == ["half.txt"]
    assert finished["result_sha"]
    assert payload(history, EventType.VERIFICATION) is None


# --- A6: the verifier never takes a location from the worker -------------------


def test_the_verifier_works_in_a_fresh_worktree_of_the_result_not_the_workers_claim(tmp_path):
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()
    worker = Writes(artifacts={"workdir": str(elsewhere), "summary": "trust me"})
    verifier = Recorder()

    result, history, repo, verifier, _ = run_once(tmp_path, worker, verifier)

    started = payload(history, EventType.ATTEMPT_STARTED)
    finished = payload(history, EventType.ATTEMPT_FINISHED)
    call = verifier.calls[0]
    verification = payload(history, EventType.VERIFICATION)
    assert call["path"] == Path(verification["worktree"]) != Path(started["worktree"])
    assert git(call["path"], "rev-parse", "HEAD") == finished["result_sha"]
    assert call["base"] == started["base_sha"] and call["result"] == finished["result_sha"]
    assert set(call["evidence"]) == {"base_sha", "result_sha", "files_changed",
                                     "files_changed_count", "diffstat", "outcome"}
    assert str(elsewhere) not in json.dumps(call["evidence"])
    # The worker's claim is kept as provenance only.
    assert finished["artifacts"]["workdir"] == str(elsewhere)


# --- A4: deadlines ------------------------------------------------------------------


class Sleeps:
    """A subprocess worker that never finishes on its own, with a grandchild."""

    def execute(self, task, context, *, workspace):
        outcome = workspace.run(
            ["sh", "-c", "echo partial > partial.txt; sleep 60 & echo $! > gc.pid; sleep 60"]
        )
        return ExecutionResult(status="failed" if outcome.timed_out else "success")


def test_a_subprocess_worker_past_its_deadline_is_killed_and_timed_out(tmp_path, monkeypatch):
    monkeypatch.setattr("core.worker_process.DEFAULT_GRACE_S", 1)
    monkeypatch.setattr("core.workspace.DEFAULT_GRACE_S", 1)
    started_at = time.monotonic()

    result, history, repo, verifier, _ = run_once(tmp_path, Sleeps(), attempt_timeout_s=1)

    assert time.monotonic() - started_at < 20
    finished = payload(history, EventType.ATTEMPT_FINISHED)
    assert finished["outcome"] == "timed_out"
    assert finished["processes"][0]["timed_out"] is True
    assert finished["files_changed"] == ["gc.pid", "partial.txt"]
    process = payload(history, EventType.ATTEMPT_PROCESS)
    assert process["phase"] == "worker" and process["pid"] == process["pgid"]
    worktree = Path(payload(history, EventType.ATTEMPT_STARTED)["worktree"])
    grandchild = int((worktree / "gc.pid").read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("the worker's grandchild survived the deadline")
    # A timed-out attempt is not verified.
    assert verifier.calls == []
    assert payload(history, EventType.VERIFICATION) is None


def test_an_in_process_worker_returning_after_its_deadline_is_timed_out(tmp_path):
    class Slow:
        def execute(self, task, context, *, workspace):
            time.sleep(1.2)
            return ExecutionResult(status="success")

    result, history, *_ = run_once(tmp_path, Slow(), attempt_timeout_s=1)

    assert payload(history, EventType.ATTEMPT_FINISHED)["outcome"] == "timed_out"
    assert payload(history, EventType.ATTEMPT_FINISHED)["worker_reported_status"] == "success"


def test_the_deadline_is_recorded_when_the_attempt_starts(tmp_path):
    result, history, *_ = run_once(tmp_path, Writes(), attempt_timeout_s=123)

    started = payload(history, EventType.ATTEMPT_STARTED)
    assert started["timeout_s"] == 123 and started["deadline_at"]


# --- the lock covers worker processes ------------------------------------------------


def test_worker_processes_hold_the_project_lock(tmp_path):
    class ListsFds:
        def execute(self, task, context, *, workspace):
            out = workspace.run(["sh", "-c", "ls -l /proc/self/fd/"])
            return ExecutionResult(status="success", artifacts={"fds": out.stdout})

    result, history, *_ = run_once(tmp_path, ListsFds())

    assert ".run.lock" in payload(history, EventType.ATTEMPT_FINISHED)["artifacts"]["fds"]


# --- refusals: no attempt is started ---------------------------------------------------


def refusal(history):
    return payload(history, EventType.OPERATION_RESULT)


def test_a_project_without_a_repository_cannot_run_attempts(tmp_path):
    write_project(tmp_path, repository=False)
    history = InMemoryHistoryStore()
    loop = AutonomousLoop(Master(tmp_path), Scripted([RUN]), Writes(), Recorder(),
                          history=history)

    result = loop.run("alpha")

    assert result.stop_reason == STOP_OPERATION_FAILED
    assert refusal(history)["reason"] == "no_repository"
    assert history.events(types=[EventType.ATTEMPT_STARTED]) == ()


def set_repository(project, repository, base_branch="main"):
    data = yaml.safe_load((project / "project.yaml").read_text())
    data.update(repository=str(repository), base_branch=base_branch)
    (project / "project.yaml").write_text(yaml.safe_dump(data))


@pytest.mark.parametrize("where", ["inside_projects_root", "containing_projects_root",
                                   "inside_state_dir", "control_plane"])
def test_an_overlapping_repository_is_refused(tmp_path, where, runtime_paths):
    project, _ = write_project(tmp_path / "root")
    target = {
        "inside_projects_root": lambda: make_repo(tmp_path / "root" / "inner"),
        "containing_projects_root": lambda: make_repo(tmp_path / "outer", files={"x": "1"}),
        "inside_state_dir": lambda: make_repo(runtime_paths.state_dir / "repo"),
        "control_plane": lambda: Path(__file__).resolve().parent.parent,
    }[where]()
    if where == "containing_projects_root":
        # Put the projects root inside this repository.
        (tmp_path / "root").rename(tmp_path / "outer" / "root")
        project = tmp_path / "outer" / "root" / "alpha-project"
        root = tmp_path / "outer" / "root"
    else:
        root = tmp_path / "root"
    set_repository(project, target)
    history = InMemoryHistoryStore()
    loop = AutonomousLoop(Master(root), Scripted([RUN]), Writes(), Recorder(),
                          history=history, paths=runtime_paths)

    result = loop.run("alpha")

    assert result.stop_reason == STOP_OPERATION_FAILED
    assert refusal(history)["reason"] == "workspace_refused"
    assert history.events(types=[EventType.ATTEMPT_STARTED]) == ()


def test_a_missing_base_branch_is_refused(tmp_path):
    project, repo = write_project(tmp_path)
    set_repository(project, repo, base_branch="nope")
    history = InMemoryHistoryStore()
    loop = AutonomousLoop(Master(tmp_path), Scripted([RUN]), Writes(), Recorder(),
                          history=history)

    loop.run("alpha")

    assert refusal(history)["reason"] == "repository_unusable"


# --- process logs ---------------------------------------------------------------


def test_process_logs_are_recorded_with_their_hashes_outside_the_worktree(
        tmp_path, tmp_path_factory):
    import hashlib
    from core.paths import RuntimePaths

    runtime_paths = RuntimePaths(state_dir=tmp_path_factory.mktemp("state"),
                                 worktrees_root=tmp_path_factory.mktemp("wt") / "root")

    class Talks:
        def execute(self, task, context, *, workspace):
            workspace.run(["sh", "-c", "echo hello from the worker"])
            return ExecutionResult(status="success")

    result, history, *_ = run_once(tmp_path, Talks(), paths=runtime_paths)

    started = payload(history, EventType.ATTEMPT_STARTED)
    process = payload(history, EventType.ATTEMPT_FINISHED)["processes"][0]
    log = Path(process["stdout_log"]["path"])
    assert log.read_text() == "hello from the worker\n"
    assert process["stdout_log"]["sha256"] == hashlib.sha256(log.read_bytes()).hexdigest()
    assert runtime_paths.state_dir in log.parents
    assert Path(started["worktree"]) not in log.parents



def test_the_workers_usage_claim_is_recorded(tmp_path):
    class Counts:
        def execute(self, task, context, *, workspace):
            return ExecutionResult(status="success",
                                   usage={"input_tokens": 1200, "output_tokens": 30})

    result, history, *_ = run_once(tmp_path, Counts())

    finished = payload(history, EventType.ATTEMPT_FINISHED)
    assert finished["worker_reported_usage"] == {"input_tokens": 1200, "output_tokens": 30}


def test_the_attempt_commit_is_named_after_the_task(tmp_path):
    result, history, repo, *_ = run_once(tmp_path, Writes())

    finished = payload(history, EventType.ATTEMPT_FINISHED)
    attempt_id = payload(history, EventType.ATTEMPT_STARTED) and \
        history.events(types=[EventType.ATTEMPT_STARTED])[0].attempt_id
    worktree = Path(payload(history, EventType.ATTEMPT_STARTED)["worktree"])
    message = git(worktree, "log", "-1", "--format=%B", finished["result_sha"])
    assert message.splitlines()[0] == "t1: T1"
    assert f"Attempt {attempt_id}." in message
