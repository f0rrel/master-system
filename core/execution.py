from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


VALID_STATUSES = {
    "success",
    "failed",
    "partial",
    "blocked",
    "cancelled",
    "needs_human",
}


@dataclass(frozen=True)
class ExecutionResult:
    """Result of executing a selected work item via a backend.

    The result is treated as observations/proposals only. It does not
    mutate authoritative project state. Status values are constrained
    to known terminal/feedback states.
    """

    status: str
    reason: str = ""
    artifacts: Mapping[str, object] = field(default_factory=dict)
    state_updates: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.status, str) or self.status not in VALID_STATUSES:
            raise ValueError(
                f"invalid execution status {self.status!r}; "
                f"expected one of {sorted(VALID_STATUSES)}"
            )
        # Normalize to immutable mappings
        object.__setattr__(self, "artifacts", dict(self.artifacts))
        object.__setattr__(self, "state_updates", dict(self.state_updates))


@runtime_checkable
class ExecutionBackend(Protocol):
    """Backend-agnostic execution contract.

    A backend decides HOW to execute a task; it does not decide what
    constitutes authoritative project state. It must not mutate
    ProjectState directly.
    """

    def execute(self, task: Mapping[str, object], context: Mapping[str, object],
                *, workspace) -> ExecutionResult:
        """Execute the specified task using this backend.

        Args:
            task: Authoritative task representation (read-only view).
            context: Minimal read-only execution context.
            workspace: The :class:`core.workspace.AttemptWorkspace` the
                orchestrator created for this attempt. The backend works only
                there, starts processes only through ``workspace.run``, and
                stops by ``workspace.deadline``. It never chooses a location.

        Returns:
            ExecutionResult containing outcome and proposed state updates.
        """
        ...
