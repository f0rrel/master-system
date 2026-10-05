"""The smallest autonomous orchestration loop over the existing components.

This module wires four pieces that already exist and do not know about each
other::

    ReasoningEngine      what to do next     (provider-independent)
    ReasoningInterface   how a decision lands (approval policy)
    TaskOrchestrator     doing + assessing    (execution + verification)
    Master                what is true         (authoritative state)

It adds no framework. There is no state machine, no event log, no scheduler and
no new abstraction: a ``while`` loop, one decision per turn, and a stop
condition. Everything that could decide something has already been built; this
only sequences them.

The division of authority is the point of the file
---------------------------------------------------
Master is the single writer of project state. A decision reaches it only
through :class:`ReasoningInterface`, which applies the approval policy and
delegates to Master. Execution and verification produce
:class:`ExecutionResult` and :class:`VerificationResult`, which are attached to
the *next* reasoning request and are never written anywhere. So the model can
be wrong, the worker can fail, and the verifier can lie, and none of them can
change a task's status without Master deciding to.

Model independence
------------------
Nothing here names a provider, a model or a worker. The constructor takes a
:class:`core.provider.ReasoningProvider`, an :class:`core.execution.ExecutionBackend`
and a :class:`core.verification.VerificationBackend`. DeepSeek, Ollama, OpenCode
and Big Pickle are all just arguments; swapping them cannot change this code.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Optional

from core.execution import ExecutionBackend
from core.master import Master
from core.provider import ReasoningProvider
from core.reasoning import Decision, MasterDecision, ReasoningInterface
from core.reasoning_engine import ReasoningEngine, ReasoningError
from core.task_orchestrator import TaskOrchestrator
from core.verification import VerificationBackend
from core.work_manager import calculate_readiness

__all__ = [
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_REQUEST",
    "STOP_APPROVAL",
    "STOP_MASTER",
    "STOP_NO_WORK",
    "STOP_OPERATION_FAILED",
    "STOP_STEP_LIMIT",
    "STOP_UNUSABLE_REPLY",
    "AutonomousLoop",
    "LoopResult",
    "run_autonomous",
]

#: The standing request a human would otherwise have to retype every turn. It
#: asks for progress and names no provider, model or tool.
DEFAULT_REQUEST = "Choose the next safe action that moves this project forward."

# Why the loop stopped. Deliberately a handful of plain strings rather than a
# registry of stop kinds: a caller reads one, a test asserts one, and nothing
# outside this module needs to interpret them.
STOP_NO_WORK = "no_actionable_work"
STOP_MASTER = "master_stop"
STOP_APPROVAL = "approval_required"
STOP_STEP_LIMIT = "step_limit"
STOP_UNUSABLE_REPLY = "unusable_reasoning_reply"
STOP_OPERATION_FAILED = "operation_failed"

#: How many times one turn may be re-asked after the model replies with
#: something unusable. One retry: a single malformed reply is usually a
#: formatting slip, and asking again with the same context costs one call. A
#: second identical failure means the model cannot answer this question right
#: now, so continuing would only spend tokens to reach the same place.
DEFAULT_MAX_RETRIES = 1


@dataclass(frozen=True)
class LoopResult:
    """What the loop did and why it stopped."""

    stop_reason: str
    steps: int = 0
    decision: Optional[MasterDecision] = None
    last_output: Optional[Mapping] = None
    #: Why the loop could not act this turn: a provider/parse error message, or
    #: the failing OperationResult when Master proposed something Master then
    #: refused. None when the loop stopped for an ordinary reason.
    detail: Optional[str] = None

    def __str__(self) -> str:
        where = self.stop_reason
        if self.decision is not None:
            where = f"{where} ({self.decision.decision.value})"
        if self.detail:
            where = f"{where}: {self.detail}"
        return where


class AutonomousLoop:
    """Drive one project until there is nothing safe left to do.

    One turn is: inspect state, ask Master, act on the answer, and if a task
    is now in flight run it through the worker and the verifier so the results
    come back as evidence for the next decision.
    """

    def __init__(
        self,
        master: Master,
        provider: ReasoningProvider,
        execution_backend: ExecutionBackend,
        verification_backend: VerificationBackend,
        request: str = DEFAULT_REQUEST,
        max_steps: int = 20,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ):
        if not isinstance(master, Master):
            raise TypeError(f"expected a Master, got {type(master).__name__}")
        if not isinstance(request, str) or not request.strip():
            raise ValueError("request must be a non-empty string")
        if max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        if max_retries < 0:
            raise ValueError("max_retries must not be negative")

        self._master = master
        self._request = request
        self._max_steps = max_steps
        self._max_retries = max_retries

        # One interface shared by the engine and the loop, so a decision and a
        # direct execution both land on the same approval gate and the same
        # Master instance.
        self._interface = ReasoningInterface(master)
        self._engine = ReasoningEngine(provider, master, self._interface)
        self._orchestrator = TaskOrchestrator(
            master,
            execution_backend,
            verification_backend,
            reasoning_engine=self._engine,
        )

    @property
    def engine(self) -> ReasoningEngine:
        return self._engine

    # --- read-only project inspection ---------------------------------

    def _task_records(self, project_id: str):
        return list(self._master.project_state(project_id).tasks())

    def _actionable_task(self, project_id: str):
        """Return a task the system could work on right now, else None.

        Either a task that is already in flight, or one whose dependencies are
        satisfied so Master could start it. Blocked, completed and cancelled
        tasks are not actionable, which is what makes the loop terminate.
        """
        for task in self._task_records(project_id):
            if task.get("status") == "in_progress":
                return task
            readiness, _blocked_by, _reason = calculate_readiness(
                self._master.project_state(project_id), task
            )
            if readiness == "ready":
                return task
        return None

    def _in_progress_task(self, project_id: str):
        for task in self._task_records(project_id):
            if task.get("status") == "in_progress":
                return task
        return None

    # --- asking Master ---------------------------------------------------

    def _decide(self, project_id: str):
        """Ask Master once, re-asking up to the retry bound on an unusable reply.

        Returns ``(proposal, decision, detail)``. On success ``proposal`` is the
        parsed Proposal and ``detail`` is None. On give-up ``proposal`` is None
        and ``detail`` explains why.

        Only :class:`ReasoningError` is retried, and only because it means the
        reply could not be turned into a decision at all -- a malformed
        operation, an unparseable body, a provider that could not be reached.
        It is never interpreted as a decision and never repaired: a second
        unusable answer stops the loop rather than being guessed at, because
        inventing an operation the model did not propose is exactly the
        authority this system refuses to take for itself.
        """
        detail = None

        for attempt in range(self._max_retries + 1):
            try:
                proposal = self._engine.reason(self._request, project_id)
            except ReasoningError as error:
                detail = str(error)
                continue
            return proposal, proposal._decision, None

        return None, None, (
            f"no usable reply after {attempt + 1} attempt(s): {detail}"
        )

    @staticmethod
    def _describe_failures(batch) -> str:
        """Name the operations Master refused, and why, in one line."""
        parts = []
        for outcome in batch.failed:
            reason = outcome.reason.value if outcome.reason is not None else None
            parts.append(
                f"{outcome.operation} -> {outcome.status.value}"
                + (f" ({reason})" if reason else "")
                + (f": {outcome.message}" if outcome.message else "")
            )
        return "; ".join(parts)

    # --- the loop -------------------------------------------------------

    def run(self, project_id: str) -> LoopResult:
        """Run until Master stops or nothing is actionable.

        Returns the stop reason and the final decision when there is one.
        Stopping is a result, not an exception: an ordinary WAIT, an
        unusable model reply and a refused operation all come back as a
        :class:`LoopResult` with authoritative state exactly as it was.

        An unusable reply is retried a bounded number of times before giving
        up, because a malformed reply is usually one formatting slip rather
        than a decision. It is never repaired or guessed at: if the model
        cannot produce a well-formed answer within the bound, the loop stops.
        """
        if not isinstance(project_id, str) or not project_id.strip():
            raise ValueError("project_id must be a non-empty string")

        steps = 0
        last_output = None

        while steps < self._max_steps:
            if self._actionable_task(project_id) is None:
                return LoopResult(
                    stop_reason=STOP_NO_WORK, steps=steps, last_output=last_output
                )

            steps += 1

            proposal, decision, unusable = self._decide(project_id)
            if proposal is None:
                return LoopResult(
                    stop_reason=STOP_UNUSABLE_REPLY,
                    steps=steps,
                    last_output=last_output,
                    detail=unusable,
                )

            # Anything but an explicit ACT stops the loop. This covers WAIT,
            # BLOCKED, NEEDS_INFORMATION, REQUEST_APPROVAL, and a reply that
            # carried no decision at all: the safe reading of an answer we
            # cannot act on is that we should not act on it.
            if decision is None or decision.decision is not Decision.ACT:
                return LoopResult(
                    stop_reason=STOP_MASTER,
                    steps=steps,
                    decision=decision,
                    last_output=last_output,
                )

            # An ACT on something policy gates does not become an instruction
            # just because the model said ACT. Policy decides, not the model.
            if decision.pending_approval:
                return LoopResult(
                    stop_reason=STOP_APPROVAL,
                    steps=steps,
                    decision=decision,
                    last_output=last_output,
                )

            # Master said do this. If Master then refuses it, the loop must not
            # carry on as though the world had moved: the task did not start,
            # so there is nothing to execute and nothing to verify. Anything
            # that did apply before the failure stays applied; nothing is
            # rolled back, because BatchResult is not a transaction.
            batch = proposal.execute(self._interface)
            if not batch.all_successful:
                return LoopResult(
                    stop_reason=STOP_OPERATION_FAILED,
                    steps=steps,
                    decision=decision,
                    last_output=last_output,
                    detail=self._describe_failures(batch),
                )

            # Execution and verification happen only when Master has actually
            # put a task in flight. Their results are attached to the next
            # reasoning request by the orchestrator, never applied to state.
            inflight = self._in_progress_task(project_id)
            if inflight is not None:
                last_output = self._orchestrator.orchestrate(
                    project_id, inflight["id"]
                )

        return LoopResult(
            stop_reason=STOP_STEP_LIMIT, steps=steps, last_output=last_output
        )


def run_autonomous(
    project_id: str,
    *,
    master: Master,
    provider: ReasoningProvider,
    execution_backend: ExecutionBackend,
    verification_backend: VerificationBackend,
    request: str = DEFAULT_REQUEST,
    max_steps: int = 20,
) -> LoopResult:
    """Entry point. Every collaborator is injected, nothing is hard-coded.

    This is the seam a real runtime fills in: pass a reasoning provider, an
    execution backend and a verifier, and this function knows nothing about
    which ones they are.
    """
    return AutonomousLoop(
        master,
        provider,
        execution_backend,
        verification_backend,
        request=request,
        max_steps=max_steps,
    ).run(project_id)
