from __future__ import annotations

from collections.abc import Mapping

from core.execution import ExecutionBackend, ExecutionResult
from core.execution_runner import TaskExecutionRunner
from core.master import Master
from core.verification import VerificationBackend, VerificationResult


class TaskOrchestrator:
    """Thin orchestration seam coordinating exec → verify → feedback.

    The orchestrator does not decide WHAT to do. It coordinates execution
    and verification for an explicitly specified task, preserves both results
    as structured read-only data, and returns them in a form suitable for
    feeding into the next reasoning cycle. It never mutates ProjectState.
    """

    def __init__(
        self,
        master: Master,
        execution_backend: ExecutionBackend,
        verification_backend: VerificationBackend,
        reasoning_engine=None,
    ):
        self._master = master
        self._execution_backend = execution_backend
        self._verification_backend = verification_backend
        self._runner = TaskExecutionRunner(master, execution_backend)
        self._reasoning_engine = reasoning_engine

    def orchestrate(self, project_id: str, task_id: str) -> Mapping[str, object]:
        """Run execution then verification for task_id in project_id.

        Returns:
            A structured dict containing authoritative references, execution
            and verification results (as dicts), suitable for reasoning input.
        """
        if not isinstance(project_id, str) or not project_id.strip():
            raise ValueError("project_id must be a non-empty string")
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("task_id must be a non-empty string")

        ps = self._master.project_state(project_id)
        task = None
        for t in ps.tasks():
            if t.get("id") == task_id:
                task = t
                break
        if task is None:
            raise ValueError(f"unknown task {task_id!r} in project {project_id!r}")

        exec_res = self._runner.execute(project_id, task_id)

        evidence = dict(exec_res.artifacts) if isinstance(exec_res.artifacts, Mapping) else {}
        ver_res = self._verification_backend.verify(task, {"project_id": project_id, "task_id": task_id}, evidence)

        result = {
            "project_id": project_id,
            "task_id": task_id,
            "execution": {
                "status": exec_res.status,
                "reason": exec_res.reason,
                "artifacts": dict(exec_res.artifacts),
                "state_updates": dict(exec_res.state_updates),
            },
            "verification": {
                "verdict": ver_res.verdict,
                "summary": ver_res.summary,
                "findings": [dict(f) for f in ver_res.findings],
                "evidence": dict(ver_res.evidence),
            },
        }
        if self._reasoning_engine is not None and hasattr(self._reasoning_engine, "attach_last_result"):
            self._reasoning_engine.attach_last_result(result)
        return result
