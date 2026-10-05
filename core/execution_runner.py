from __future__ import annotations

from collections.abc import Mapping

from core.execution import ExecutionBackend, ExecutionResult
from core.master import Master
from core.work_manager import calculate_readiness


class ExecutionError(Exception):
    """Raised when execution cannot be invoked on an invalid candidate."""


class TaskExecutionRunner:
    """Backend-agnostic invocation seam for executing a specific task.

    The runner does not decide WHAT should be executed; the caller explicitly
    specifies project_id and task_id. It obtains authoritative task/context
    from Master/ProjectState, invokes the provided backend, and returns the
    backend's ExecutionResult unchanged. It never mutates ProjectState and
    never applies state_updates.
    """

    def __init__(self, master: Master, backend: ExecutionBackend):
        self._master = master
        self._backend = backend

    def execute(self, project_id: str, task_id: str, *, workspace) -> ExecutionResult:
        """Execute the specified task via the configured backend.

        Args:
            project_id: Authoritative project identifier.
            task_id: Authoritative task identifier.

        Returns:
            ExecutionResult from the backend (state_updates remain untrusted).

        Raises:
            ExecutionError: If the task cannot be executed as a valid candidate.
        """
        task, context = self.prepare(project_id, task_id)
        return self.invoke(task, context, workspace)

    def prepare(self, project_id: str, task_id: str):
        """Check the task is executable and build its context. No side effects.

        Split from :meth:`invoke` so a caller can durably record that an
        attempt is starting after every check has passed and before the
        backend can touch anything.

        Returns:
            ``(task, context)`` ready for :meth:`invoke`.

        Raises:
            ExecutionError: If the task cannot be executed as a valid candidate.
        """
        if not isinstance(project_id, str) or not project_id.strip():
            raise ExecutionError("project_id must be a non-empty string")
        if not isinstance(task_id, str) or not task_id.strip():
            raise ExecutionError("task_id must be a non-empty string")

        ps = self._master.project_state(project_id)

        task = None
        for t in ps.tasks():
            if t.get("id") == task_id:
                task = t
                break

        if task is None:
            raise ExecutionError(f"unknown task {task_id!r} in project {project_id!r}")

        readiness, blocked_by, readiness_reason = calculate_readiness(ps, task)
        status = task.get("status")

        if status in ("completed", "cancelled"):
            raise ExecutionError(
                f"task {task_id!r} is not executable (status={status})"
            )
        if readiness == "blocked":
            raise ExecutionError(
                f"task {task_id!r} is not executable (readiness=blocked, blocked_by={blocked_by}, reason={readiness_reason})"
            )
        if status != "in_progress":
            raise ExecutionError(
                f"task {task_id!r} is not executable (status={status}; must be in_progress)"
            )

        context: Mapping[str, object] = {
            "project_id": project_id,
            "task_id": task_id,
            "status": status,
            "readiness": readiness,
            "blocked_by": blocked_by,
            "readiness_reason": readiness_reason,
        }
        return task, context

    def invoke(self, task, context, workspace) -> ExecutionResult:
        """Run the backend once on a prepared task. This is the side effect."""
        result = self._backend.execute(task, context, workspace=workspace)
        if not isinstance(result, ExecutionResult):
            raise ExecutionError(
                f"backend returned unexpected result type {type(result).__name__}"
            )

        return result
