from __future__ import annotations

import time
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from core.evidence import spec_hash
from core.execution import ExecutionBackend, ExecutionResult
from core.execution_runner import ExecutionError, TaskExecutionRunner
from core.history import EventType, HistoryStore, jsonable, new_event_id
from core.master import Master
from core.paths import RuntimePaths
from core.verification import VerificationBackend, VerificationResult
from core.worker_env import default_worker_home, find_node_bin, worker_environment
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


def commit_subject(task) -> str:
    """``<task id>: <task title>``, one line, for the attempt's commit."""
    title = " ".join(str(task.get("title") or "").split())
    return f"{task.get('id')}: {title}"[:120]


def default_worker_env() -> dict:
    node_bin = find_node_bin()
    return worker_environment(default_worker_home(),
                              path_dirs=[node_bin] if node_bin else [])


def process_records(workspace) -> list:
    """What the orchestrator observed about each process: ids, exit, logs."""
    return [
        {
            "pid": p.pid,
            "pgid": p.pgid,
            "returncode": p.returncode,
            "timed_out": p.timed_out,
            "stdout_log": p.stdout_log.to_dict() if p.stdout_log else None,
            "stderr_log": p.stderr_log.to_dict() if p.stderr_log else None,
        }
        for p in workspace.processes
    ]


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
        log_root: Optional[Path] = None,
        worker_env: Optional[Mapping[str, str]] = None,
        auto_integrate=None,
        tiers=None,
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
        #: Every worker and verification process gets exactly this environment
        #: (an allowlist; no secrets). Default: core.worker_env with the
        #: default worker home and the newest Node >= 22 found, if any.
        self._worker_env = dict(worker_env) if worker_env is not None else default_worker_env()
        #: Integrates a passing attempt into the base branch right after its
        #: verification, for projects that opt in (core.auto_integrate).
        self._auto_integrate = auto_integrate
        #: Worker tiers (core.worker_tiers.TierSet): the orchestrator, never
        #: Master, picks each attempt's worker profile from history.
        self._tiers = tiers
        #: Process logs live in the state dir, never in the worktree.
        self._log_root = (
            Path(log_root) if log_root is not None
            else RuntimePaths.default().state_dir / "logs"
        )

    @property
    def worktrees(self) -> GitWorktrees:
        return self._worktrees

    def _propose_lessons(self, project_id, task, attempt_id, reply) -> list:
        """A verified attempt's LESSON lines go to the owner's pending list, nowhere else."""
        from core.lessons import LessonStore, extract_lessons

        texts = extract_lessons(reply if isinstance(reply, str) else None)
        if not texts:
            return []
        store = LessonStore(self._master.project_state(project_id).project_path)
        added = store.propose(task.get("type") or "developer", texts, task.get("id"),
                              attempt_id)
        return [r["id"] for r in added]

    def _briefing(self, project_id, task):
        """What the worker is told besides the spec, and the type's tool limits.

        Returns ``(briefing, facts for attempt_started, OpenCode config or None)``.
        Everything comes from the project's files and the owner's approvals.
        """
        from core.direction import read_direction
        from core.lessons import LessonStore
        from core.task_types import opencode_config, skill_text, type_settings

        project = self._master.project_state(project_id).project()
        direction = read_direction(project)
        briefing = {"direction": direction.text if direction else None}
        task_type = task.get("type")
        if not task_type:
            return briefing, {}, None
        settings = type_settings(project, task_type)
        lessons = LessonStore(self._master.project_state(project_id).project_path)
        briefing.update(type=task_type, skill=skill_text(project, settings),
                        lessons=lessons.for_prompt(task_type))
        facts = {"task_type": task_type, "allowed_tools": settings["tools"],
                 "lessons_used": len(briefing["lessons"])}
        return briefing, facts, opencode_config(settings["tools"])

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
        briefing, type_facts, tools_config = self._briefing(project_id, task)
        context = {**context, "briefing": briefing}
        base_sha = prepared["base_sha"]

        attempt_id = new_event_id()
        try:
            worktree = self._worktrees.path_for(project_id, attempt_id)
        except IsolationError as error:
            raise WorkspaceRefusal("workspace_refused", str(error)) from error
        branch = GitWorktrees.branch_for(attempt_id)
        runner, worker_env, tier_facts = self._runner, self._worker_env, {}
        if self._tiers is not None and self._tiers.ladder:
            from core.worker_tiers import choose_tier
            from core.history import InMemoryHistoryStore

            choice = choose_tier(self._history or InMemoryHistoryStore(), project_id, task,
                                 self._tiers.ladder)
            backend = self._tiers.backends[choice.profile]
            runner = TaskExecutionRunner(self._master, backend)
            worker_env = dict(self._tiers.envs[choice.profile])
            tier_facts = {"worker_profile": choice.profile, "worker_tier": choice.tier,
                          "worker_model": self._tiers.models.get(choice.profile)}
            if choice.escalated_from is not None:
                tier_facts["escalated_from"] = choice.escalated_from
            worker = describe_worker(backend)
        else:
            worker = describe_worker(self._execution_backend)
        if tools_config is not None:
            # The task type's allowed tools, enforced by the worker's own config.
            worker_env = {**worker_env, "OPENCODE_CONFIG_CONTENT": tools_config}
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
                    "spec_hash": spec_hash(task),
                    "task_title": task.get("title"),
                    # Human-written; copied so the report can show it from
                    # history alone. Not part of the spec hash.
                    "manual_check": task.get("manual_check"),
                    "timeout_s": self._attempt_timeout_s,
                    "deadline_at": deadline_at,
                    **tier_facts,
                    **type_facts,
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

        log_dir = self._log_root / project_id / attempt_id
        workspace = AttemptWorkspace(
            path=worktree,
            base_sha=base_sha,
            deadline=deadline,
            deadline_at=deadline_at,
            log_dir=log_dir,
            phase="worker",
            env=worker_env,
            lock_fd=lock_fd,
            on_spawn=recorder("worker", deadline_at),
        )

        exec_res = None
        raised = None
        try:
            exec_res = runner.invoke(task, context, workspace)
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
            facts = self._worktrees.snapshot(
                worktree, base_sha, attempt_id, subject=commit_subject(task))
        except WorkspaceError as error:
            facts = {"result_sha": None, "snapshot_error": str(error)}

        finished = {
            "outcome": outcome,
            "worker_reported_status": exec_res.status if exec_res is not None else None,
            "worker": worker,
            "processes": process_records(workspace),
            **facts,
        }
        if exec_res is not None:
            finished.update(
                reason=exec_res.reason,
                artifacts=jsonable(exec_res.artifacts),
                state_updates=jsonable(exec_res.state_updates),
                worker_reported_usage=jsonable(exec_res.usage),
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
            log_dir=log_dir,
            phase="verification",
            env=self._worker_env,
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
                processes=process_records(verification_workspace),
            )
            raise

        record(
            EventType.VERIFICATION,
            verdict=ver_res.verdict,
            summary=ver_res.summary,
            findings=jsonable(list(ver_res.findings)),
            evidence=jsonable(ver_res.evidence),
            deadline_at=verify_deadline_at,
            processes=process_records(verification_workspace),
        )

        result["verification"] = {
            "verdict": ver_res.verdict,
            "summary": ver_res.summary,
            "findings": [dict(f) for f in ver_res.findings],
            "evidence": dict(ver_res.evidence),
        }
        if ver_res.verdict == "pass":
            result["lessons_proposed"] = self._propose_lessons(
                project_id, task, attempt_id, exec_res.artifacts.get("summary"))
        if (ver_res.verdict == "pass" and self._auto_integrate is not None
                and self._auto_integrate.applies(project_id)):
            result["integration"] = self._auto_integrate(project_id, attempt_id, lock_fd)
        return result
