from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


VALID_VERDICTS = {
    "pass",
    "fail",
    "needs_human",
    "unable_to_verify",
}


@dataclass(frozen=True)
class VerificationResult:
    """Result of assessing completed/attempted work.

    The result is an assessment/observation only. It does not mutate
    authoritative project state. Verdicts are constrained to known values.
    """

    verdict: str
    summary: str = ""
    findings: Sequence[Mapping[str, object]] = field(default_factory=tuple)
    evidence: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.verdict, str) or self.verdict not in VALID_VERDICTS:
            raise ValueError(
                f"invalid verification verdict {self.verdict!r}; "
                f"expected one of {sorted(VALID_VERDICTS)}"
            )

        # Normalize to immutable structures
        findings = tuple(dict(f) for f in self.findings if isinstance(f, Mapping))
        object.__setattr__(self, "findings", findings)
        object.__setattr__(self, "evidence", dict(self.evidence))


@runtime_checkable
class VerificationBackend(Protocol):
    """Backend-agnostic verification contract.

    A verifier assesses work (deterministic or model-assisted) and returns
    a structured assessment. It must not mutate ProjectState and must not
    automatically mark tasks completed/failed.
    """

    def verify(
        self,
        task: Mapping[str, object],
        context: Mapping[str, object],
        evidence: Mapping[str, object] | None = None,
        *,
        workspace,
    ) -> VerificationResult:
        """Verify the specified task/work attempt.

        Args:
            task: Authoritative task representation (read-only).
            context: Minimal read-only project/context information.
            evidence: Facts the orchestrator observed about the attempt
                (base/result SHA, files changed, diff stats, outcome). Never
                the worker's own artifacts.
            workspace: The orchestrator's :class:`core.workspace.AttemptWorkspace`
                for the attempt, with ``result_sha`` set. Processes start only
                through ``workspace.run``.

        Returns:
            VerificationResult assessment.
        """
        ...
