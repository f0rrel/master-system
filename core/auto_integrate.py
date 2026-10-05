"""Routine integration of verified attempts into a project's development branch.

Owner decision H-D1 (2026-10-06): for a project whose ``project.yaml`` sets
``auto_integrate: true``, integrating a verified attempt into its
``base_branch`` (``develop``) is a ROUTINE system action, done right after the
attempt passes verification and inside the same project lock. It uses the same
rules as a human integration (``core.attempts.integrate_held``): the attempt
must have finished, passed and match the current spec; if the base moved, the
result is replayed onto it and re-verified, and the base moves to exactly that
re-verified commit. ``main`` is never touched here: releases are human-only.

H-D2: such a project's tasks complete only once integrated (the completion
gate checks it). H-D3: on a conflict or a failed re-verification the attempt is
not integrated; the run's next attempt starts from the new base.
"""

from __future__ import annotations

import uuid
from typing import Mapping

from core.attempts import IntegrationRefused, integrate_held
from core.history import EventType

__all__ = ["AutoIntegrator"]


class AutoIntegrator:
    def __init__(self, master, history, paths, verifier=None, worker_env=None,
                 verification_timeout_s=None):
        self._master = master
        self._history = history
        self._paths = paths
        self._verifier = verifier
        self._worker_env = worker_env
        self._timeout = verification_timeout_s

    def applies(self, project_id: str) -> bool:
        try:
            project = self._master.project_state(project_id).project()
        except Exception:
            return False
        return project.get("auto_integrate") is True

    def __call__(self, project_id: str, attempt_id: str, lock_fd) -> Mapping:
        """Integrate now; a refusal is returned (and seen by Master), never raised."""
        kwargs = {}
        if self._timeout is not None:
            kwargs["verification_timeout_s"] = self._timeout
        try:
            result = integrate_held(self._master, self._history, project_id, attempt_id,
                                    lock_fd=lock_fd, paths=self._paths, rebase=True,
                                    verifier=self._verifier, worker_env=self._worker_env,
                                    actor="system", **kwargs)
        except IntegrationRefused as refusal:
            outcome = {"integrated": False, "reason": refusal.reason,
                       "detail": str(refusal)[:500]}
            started = self._history.events(project_id=project_id, attempt_id=attempt_id,
                                           types=[EventType.ATTEMPT_STARTED])
            self._history.append(
                type=EventType.INTEGRATION_REFUSED, run_id=started[0].run_id if started
                else uuid.uuid4().hex, project_id=project_id,
                session_id=started[0].session_id if started else None,
                task_id=started[0].task_id if started else None, attempt_id=attempt_id,
                payload={"reason": refusal.reason, "detail": str(refusal)[:500],
                         "actor": "system"})
            return outcome
        return {"integrated": True, "base_branch": result.base_branch,
                "result_sha": result.result_sha, "method": result.method}
