from __future__ import annotations

import time
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from core.execution import ExecutionBackend, ExecutionResult
from core.execution_runner import ExecutionError, TaskExecutionRunner
from core.history import EventType, HistoryStore, jsonable, new_event_id
from core.master import Master
from core.paths import RuntimePaths
from core.verification import VerificationBackend, VerificationResult
from core.workspace import (
    AttemptWorkspace,
    GitWorktrees,
    IsolationError,
    RepositoryError,
    WorkspaceError,
    control_plane_root,
)

#: Default bound on one worker run, and on one verification.
DEFAULT_ATTEMPT_TIMEOUT_S = 30 * 60
DEFAULT_VERIFICATION_TIMEOUT_S = 30 * 60


class WorkspaceRefusal(ExecutionError):
    """The project cannot run an attempt: no repository, or an unsafe one."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def describe_worker(backend) -> str:
    """Opaque provenance for a backend. Recorded, never interpreted."""
    kind = type(backend)
    return f"{kind.__module__}.{kind.__qualname__}"


def _deadline(seconds: float):
    """A monotonic deadline and its wall-clock rendering for the record."""
    at = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return time.monotonic() + seconds, at.isoformat(timespec="seconds")


def default_worktrees(master: Master, paths: Optional[RuntimePaths] = None) -> GitWorktrees:
    paths = paths if paths is not None else RuntimePaths.default()
    return GitWorktrees(
        paths.worktrees_root,
        protected=(master.root, paths.state_dir, control_plane_root()),
    )


class TaskOrchestrator:
    """Runs one attempt of a task in a workspace the orchestrator owns.

    The orchestrator does not decide WHAT to do. Given a task, it creates a
    git worktree for the attempt at the base branch's current commit, runs the
    worker there, commits whatever the worker left, and has the verifier check
    that commit. It never mutates ProjectState, and it never takes a location
    or any other fact from the worker's output.

    With a history store, one call is one attempt, recorded as
    ``attempt_started`` (committed before the worktree exists), an
    ``attempt_process`` per process started, exactly one ``attempt_finished``
    (including when the worker raises or times out), then ``verification``.
    The worker is invoked at most once per call: any transport retry happens
    inside the backend, within this one attempt.
    """

    def __init__(
        self,
        master: Master,
        execution_backend: ExecutionBackend,
        verification_backend: VerificationBackend,
        history: Optional[HistoryStore] = None,
        worktrees: Optional[GitWorktrees] = None,
        attempt_timeout_s: float = DEFAULT_ATTEMPT_TIMEOUT_S,
        verification_timeout_s: float = DEFAULT_VERIFICATION_TIMEOUT_S,
    ):
        if attempt_timeout_s <= 0 or verification_timeout_s <= 0:
            raise ValueError("timeouts must be positive")
        self._master = master
        self._execution_backend = execution_backend
        self._verification_backend = verification_backend
        self._runner = TaskExecutionRunner(master, execution_backend)
        self._history = history
        self._worktrees = worktrees if worktrees is not None else default_worktrees(master)
        self._attempt_timeout_s = attempt_timeout_s
        self._verification_timeout_s = verification_timeout_s

    @property
    def worktrees(self) -> GitWorktrees:
        return self._worktrees

    def prepare(self, project_id: str, task_id: str):
        """Check that task_id may be executed now, with its project's repository.

        Raises :class:`ExecutionError` for a task that is not executable, or
        :class:`WorkspaceRefusal` (a subclass) when the project has no usable,
        isolated repository. Changes nothing.
        """
        task, context = self._runner.prepare(project_id, task_id)
        project = self._master.project_state(project_id).project()
        repository = project.get("repository")
        base_branch = project.get("base_branch")
        if not repository:
            raise WorkspaceRefusal(
                "no_repository", f"project {project_id!r} has no repository configured"
            )
        try:
            repo, base_sha = self._worktrees.check_repository(
                Path(repository).expanduser(), base_branch
            )
        except IsolationError as error:
            raise WorkspaceRefusal("workspace_refused", str(error)) from error
        except WorkspaceError as error:
            raise WorkspaceRefusal("repository_unusable", str(error)) from error
        return {
            "task": task,
            "context": context,
            "repository": repo,
            "base_branch": base_branch,
            "base_sha": base_sha,
        }

    def orchestrate(
        self,
        project_id: str,
        task_id: str,
        *,
        run_id: Optional[str] = None,
        session_id: Optional[str] = None,
        step: Optional[int] = None,
        lock_fd: Optional[int] = None,
    ) -> Mapping[str, object]:
        """Run one attempt of task_id: worktree, worker, commit, verification.

        Returns a structured summary of the attempt. The next reasoning cycle
        reads the outcome from history, not from this return value.
        """
        if not isinstance(project_id, str) or not project_id.strip():
            raise ValueError("project_id must be a non-empty string")
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("task_id must be a non-empty string")
        if self._history is not None and not run_id:
            raise ValueError("run_id is required when recording history")

        prepared = self.prepare(project_id, task_id)
        task, context = prepared["task"], prepared["context"]
        base_sha = prepared["base_sha"]

        attempt_id = new_event_id()
        try:
            worktree = self._worktrees.path_for(project_id, attempt_id)
        except IsolationError as error:
            raise WorkspaceRefusal("workspace_refused", str(error)) from error
        branch = GitWorktrees.branch_for(attempt_id)
        worker = describe_worker(self._execution_backend)
        deadline, deadline_at = _deadline(self._attempt_timeout_s)
        ids = {
            "run_id": run_id,
            "session_id": session_id,
            "project_id": project_id,
            "task_id": task_id,
        }

        def record(event_type, **payload):
            if self._history is not None:
                self._history.append(
                    type=event_type, attempt_id=attempt_id, payload=payload, **ids
                )

        def recorder(phase, phase_deadline_at):
            def on_spawn(pid, pgid):
                record(EventType.ATTEMPT_PROCESS, phase=phase, pid=pid, pgid=pgid,
                       deadline_at=phase_deadline_at)
            return on_spawn

        # Intent first: everything about the workspace is known and recorded
        # before the worktree is created.
        if self._history is not None:
            self._history.append(
                type=EventType.ATTEMPT_STARTED,
                event_id=attempt_id,
                payload={
                    "step": step,
                    "worker": worker,
                    "repository": str(prepared["repository"]),
                    "base_branch": prepared["base_branch"],
                    "base_sha": base_sha,
                    "worktree": str(worktree),
                    "branch": branch,
                    "timeout_s": self._attempt_timeout_s,
                    "deadline_at": deadline_at,
                },
                **ids,
            )

        try:
            self._worktrees.create(prepared["repository"], worktree, attempt_id, base_sha)
        except BaseException as error:
            record(
                EventType.ATTEMPT_FINISHED,
                outcome="error",
                worker_reported_status=None,
                error_type=type(error).__name__,
                message=str(error),
                worker=worker,
            )
            raise

        workspace = AttemptWorkspace(
            path=worktree,
            base_sha=base_sha,
            deadline=deadline,
            deadline_at=deadline_at,
            lock_fd=lock_fd,
            on_spawn=recorder("worker", deadline_at),
        )

        exec_res = None
        raised = None
        try:
            exec_res = self._runner.invoke(task, context, workspace)
        except BaseException as error:
            raised = error

        timed_out = workspace.expired() or any(p.timed_out for p in workspace.processes)
        if raised is not None:
            outcome = "error"
        elif timed_out:
            outcome = "timed_out"
        else:
            outcome = "finished"

        facts = {}
        try:
            facts = self._worktrees.snapshot(worktree, base_sha, attempt_id)
        except WorkspaceError as error:
            facts = {"result_sha": None, "snapshot_error": str(error)}

        finished = {
            "outcome": outcome,
            "worker_reported_status": exec_res.status if exec_res is not None else None,
            "worker": worker,
            "processes": [
                {"pid": p.pid, "pgid": p.pgid, "returncode": p.returncode,
                 "timed_out": p.timed_out}
                for p in workspace.processes
            ],
            **facts,
        }
        if exec_res is not None:
            finished.update(
                reason=exec_res.reason,
                artifacts=jsonable(exec_res.artifacts),
                state_updates=jsonable(exec_res.state_updates),
            )
        if raised is not None:
            finished.update(error_type=type(raised).__name__, message=str(raised))
        record(EventType.ATTEMPT_FINISHED, **finished)
        if raised is not None:
            raise raised

        result = {
            "project_id": project_id,
            "task_id": task_id,
            "attempt_id": attempt_id,
            "outcome": outcome,
            "base_sha": base_sha,
            "result_sha": facts.get("result_sha"),
            "execution": {
                "status": exec_res.status,
                "reason": exec_res.reason,
                "artifacts": dict(exec_res.artifacts),
                "state_updates": dict(exec_res.state_updates),
            },
            "verification": None,
        }

        # Only a finished attempt with a committed result is verified. A timed
        # out attempt is not: the completion gate refuses it either way.
        if outcome != "finished" or not facts.get("result_sha"):
            return result

        verify_deadline, verify_deadline_at = _deadline(self._verification_timeout_s)
        verification_workspace = AttemptWorkspace(
            path=worktree,
            base_sha=base_sha,
            result_sha=facts["result_sha"],
            deadline=verify_deadline,
            deadline_at=verify_deadline_at,
            lock_fd=lock_fd,
            on_spawn=recorder("verification", verify_deadline_at),
        )
        # Orchestrator facts only. Worker artifacts never reach the verifier.
        observed = {
            "base_sha": base_sha,
            "result_sha": facts["result_sha"],
            "files_changed": facts.get("files_changed", []),
            "files_changed_count": facts.get("files_changed_count", 0),
            "diffstat": facts.get("diffstat", {}),
            "outcome": outcome,
        }
        try:
            ver_res = self._verification_backend.verify(
                task,
                {"project_id": project_id, "task_id": task_id},
                observed,
                workspace=verification_workspace,
            )
            if not isinstance(ver_res, VerificationResult):
                raise TypeError(
                    f"verifier returned unexpected result type {type(ver_res).__name__}"
                )
        except BaseException as error:
            record(
                EventType.VERIFICATION,
                verdict=None,
                error_type=type(error).__name__,
                message=str(error),
                deadline_at=verify_deadline_at,
            )
            raise

        record(
            EventType.VERIFICATION,
            verdict=ver_res.verdict,
            summary=ver_res.summary,
            findings=jsonable(list(ver_res.findings)),
            evidence=jsonable(ver_res.evidence),
            deadline_at=verify_deadline_at,
        )

        result["verification"] = {
            "verdict": ver_res.verdict,
            "summary": ver_res.summary,
            "findings": [dict(f) for f in ver_res.findings],
            "evidence": dict(ver_res.evidence),
        }
        return result
