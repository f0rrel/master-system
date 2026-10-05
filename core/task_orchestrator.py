from __future__ import annotations

from collections.abc import Mapping
from typing import Optional

from core.execution import ExecutionBackend, ExecutionResult
from core.execution_runner import TaskExecutionRunner
from core.history import EventType, HistoryStore, jsonable
from core.master import Master
from core.verification import VerificationBackend, VerificationResult


def describe_worker(backend) -> str:
    """Opaque provenance for a backend. Recorded, never interpreted."""
    kind = type(backend)
    return f"{kind.__module__}.{kind.__qualname__}"


class TaskOrchestrator:
    """Thin orchestration seam coordinating exec → verify → feedback.

    The orchestrator does not decide WHAT to do. It coordinates execution
    and verification for an explicitly specified task, preserves both results
    as structured read-only data, and returns them in a form suitable for
    feeding into the next reasoning cycle. It never mutates ProjectState.

    With a history store, one call is one execution attempt, recorded as
    ``attempt_started`` (committed before the backend runs), then exactly one
    ``attempt_finished`` -- including when the backend raises -- then
    ``verification``. The backend is invoked at most once per call: any
    transport retry happens inside the backend, within this one attempt, and
    can never hide an execution that actually started.
    """

    def __init__(
        self,
        master: Master,
        execution_backend: ExecutionBackend,
        verification_backend: VerificationBackend,
        reasoning_engine=None,
        history: Optional[HistoryStore] = None,
    ):
        self._master = master
        self._execution_backend = execution_backend
        self._verification_backend = verification_backend
        self._runner = TaskExecutionRunner(master, execution_backend)
        self._reasoning_engine = reasoning_engine
        self._history = history

    def prepare(self, project_id: str, task_id: str):
        """Check that task_id may be executed now. Raises ExecutionError if not."""
        return self._runner.prepare(project_id, task_id)

    def orchestrate(
        self,
        project_id: str,
        task_id: str,
        *,
        run_id: Optional[str] = None,
        session_id: Optional[str] = None,
        step: Optional[int] = None,
    ) -> Mapping[str, object]:
        """Run execution then verification for task_id in project_id.

        Returns:
            A structured dict containing authoritative references, execution
            and verification results (as dicts), suitable for reasoning input.
        """
        if not isinstance(project_id, str) or not project_id.strip():
            raise ValueError("project_id must be a non-empty string")
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("task_id must be a non-empty string")
        if self._history is not None and not run_id:
            raise ValueError("run_id is required when recording history")

        ps = self._master.project_state(project_id)
        task = None
        for t in ps.tasks():
            if t.get("id") == task_id:
                task = t
                break
        if task is None:
            raise ValueError(f"unknown task {task_id!r} in project {project_id!r}")

        prepared_task, context = self._runner.prepare(project_id, task_id)
        worker = describe_worker(self._execution_backend)
        ids = {
            "run_id": run_id,
            "session_id": session_id,
            "project_id": project_id,
            "task_id": task_id,
        }

        attempt_id = None
        if self._history is not None:
            started = self._history.append(
                type=EventType.ATTEMPT_STARTED,
                payload={"step": step, "worker": worker},
                **ids,
            )
            attempt_id = started.attempt_id

        try:
            exec_res = self._runner.invoke(prepared_task, context)
        except BaseException as error:
            if self._history is not None:
                self._history.append(
                    type=EventType.ATTEMPT_FINISHED,
                    attempt_id=attempt_id,
                    payload={
                        "status": "error",
                        "error_type": type(error).__name__,
                        "message": str(error),
                        "worker": worker,
                    },
                    **ids,
                )
            raise

        if self._history is not None:
            self._history.append(
                type=EventType.ATTEMPT_FINISHED,
                attempt_id=attempt_id,
                payload={
                    "status": exec_res.status,
                    "reason": exec_res.reason,
                    "artifacts": jsonable(exec_res.artifacts),
                    "state_updates": jsonable(exec_res.state_updates),
                    "worker": worker,
                },
                **ids,
            )

        evidence = dict(exec_res.artifacts) if isinstance(exec_res.artifacts, Mapping) else {}
        try:
            ver_res = self._verification_backend.verify(
                task, {"project_id": project_id, "task_id": task_id}, evidence
            )
            if not isinstance(ver_res, VerificationResult):
                raise TypeError(
                    f"verifier returned unexpected result type {type(ver_res).__name__}"
                )
        except BaseException as error:
            if self._history is not None:
                self._history.append(
                    type=EventType.VERIFICATION,
                    attempt_id=attempt_id,
                    payload={
                        "verdict": None,
                        "error_type": type(error).__name__,
                        "message": str(error),
                    },
                    **ids,
                )
            raise

        if self._history is not None:
            self._history.append(
                type=EventType.VERIFICATION,
                attempt_id=attempt_id,
                payload={
                    "verdict": ver_res.verdict,
                    "summary": ver_res.summary,
                    "findings": jsonable(list(ver_res.findings)),
                    "evidence": jsonable(ver_res.evidence),
                },
                **ids,
            )

        result = {
            "project_id": project_id,
            "task_id": task_id,
            "attempt_id": attempt_id,
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
