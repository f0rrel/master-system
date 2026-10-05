"""The smallest autonomous orchestration loop over the existing components.

This module wires four pieces that already exist and do not know about each
other::

    ReasoningEngine      what to do next     (provider-independent)
    ReasoningInterface   how a decision lands (approval policy)
    TaskOrchestrator     doing + assessing    (execution + verification)
    Master                what is true         (authoritative state)

It adds no framework. There is no state machine and no scheduler: a ``while``
loop, one decision per turn, and a stop condition. Everything that could decide
something has already been built; this only sequences them, and records what
happened in an append-only :class:`core.history.HistoryStore`.

The division of authority is the point of the file
---------------------------------------------------
Master is the single writer of project state. A decision reaches it only
through :class:`ReasoningInterface`, which applies the approval policy and
delegates to Master. Execution and verification produce
:class:`ExecutionResult` and :class:`VerificationResult`, which are recorded in
history as evidence and shown to the *next* reasoning request, and are never
applied to project state. So the model can be wrong, the worker can fail, and
the verifier can lie, and none of them can change a task's status without
Master deciding to.

Project state describes facts; operations explicitly request side effects. A
task being in_progress never causes the worker to run. Only an explicit
``run_task`` decision does, so a worker side effect is always traceable to the
decision that asked for it and is never replayed because state still looks
unfinished.

Model independence
------------------
Nothing here names a provider, a model or a worker. The constructor takes a
:class:`core.provider.ReasoningProvider`, an :class:`core.execution.ExecutionBackend`
and a :class:`core.verification.VerificationBackend`. DeepSeek, Ollama, OpenCode
and Big Pickle are all just arguments; swapping them cannot change this code.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Optional

from core.execution import ExecutionBackend
from core.evidence import DEFAULT_MAX_ATTEMPTS, HistoryEvidence
from core.execution_runner import ExecutionError
from core.history import EventType, HistoryStore, InMemoryHistoryStore, jsonable
from core.usage import add_usage
from core.master import EXPECTED_ERRORS, Master
from core.provider import ReasoningProvider
from core.reasoning import (
    SPECS,
    Decision,
    MasterDecision,
    OperationKind,
    ReasoningInterface,
)
from core.reasoning_engine import ReasoningEngine, ReasoningError
from core.recovery import recover_project
from core.run_lock import ProjectLock
from core.paths import RuntimePaths
from core.task_orchestrator import (
    DEFAULT_ATTEMPT_TIMEOUT_S,
    DEFAULT_VERIFICATION_TIMEOUT_S,
    TaskOrchestrator,
    WorkspaceRefusal,
    default_worktrees,
)
from core.workspace import GitWorktrees
from core.verification import VerificationBackend
from core.work_manager import calculate_readiness

__all__ = [
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_REQUEST",
    "STOP_APPROVAL",
    "STOP_ATTEMPT_LIMIT",
    "STOP_BUDGET",
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
STOP_ATTEMPT_LIMIT = "attempt_limit"
STOP_BUDGET = "budget_exhausted"

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
    #: Identifies this run's events in history.
    run_id: Optional[str] = None
    #: Why the decision is waiting for a human when ``stop_reason`` is
    #: ``approval_required``: ``policy`` for a gated operation, otherwise the
    #: completion gate's reason, e.g. ``verification_fail``.
    approval_reason: Optional[str] = None

    def __str__(self) -> str:
        where = self.stop_reason
        if self.decision is not None:
            where = f"{where} ({self.decision.decision.value})"
        if self.detail:
            where = f"{where}: {self.detail}"
        return where


class AutonomousLoop:
    """Drive one project until there is nothing safe left to do.

    One turn is: inspect state, ask Master, act on the answer. Execution is
    never implied by state: the worker runs only when Master explicitly
    decides ``run_task`` for a task that is already in_progress, and the
    execution and verification results come back as evidence for the next
    decision.

    Every run is recorded in history: ``run_started``, then per turn a
    ``decision`` (the intent) followed by an ``operation_result`` or the
    attempt events, and finally exactly one ``run_stopped`` or ``run_error``.
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
        history: Optional[HistoryStore] = None,
        session_id: Optional[str] = None,
        max_attempts_per_task: int = DEFAULT_MAX_ATTEMPTS,
        lock: Optional[ProjectLock] = None,
        paths: Optional[RuntimePaths] = None,
        worktrees: Optional[GitWorktrees] = None,
        attempt_timeout_s: float = DEFAULT_ATTEMPT_TIMEOUT_S,
        verification_timeout_s: float = DEFAULT_VERIFICATION_TIMEOUT_S,
        worker_env: Optional[Mapping] = None,
        stop_check=None,
        auto_integrate=None,
        tiers=None,
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
        self._history = history if history is not None else InMemoryHistoryStore()
        self._session_id = session_id
        #: Called before every Master call; a non-empty string stops the run
        #: with STOP_BUDGET and that detail (for example a spending cap).
        self._stop_check = stop_check
        #: The project lock, when the caller already holds it. Without one,
        #: run() takes the lock itself for the duration of the run.
        self._lock = lock
        self._evidence = HistoryEvidence(
            self._history, session_id=session_id, max_attempts=max_attempts_per_task,
            integration_required=auto_integrate.applies if auto_integrate else None,
        )

        # One interface shared by the engine and the loop, so every decision
        # lands on the same approval gate and the same Master instance.
        self._interface = ReasoningInterface(master)
        self._engine = ReasoningEngine(
            provider, master, self._interface, evidence_source=self._evidence
        )
        self._orchestrator = TaskOrchestrator(
            master,
            execution_backend,
            verification_backend,
            history=self._history,
            worktrees=(
                worktrees if worktrees is not None else default_worktrees(master, paths)
            ),
            attempt_timeout_s=attempt_timeout_s,
            verification_timeout_s=verification_timeout_s,
            log_root=(paths if paths is not None else RuntimePaths.default()).state_dir / "logs",
            worker_env=worker_env,
            auto_integrate=auto_integrate,
            tiers=tiers,
        )

    @property
    def engine(self) -> ReasoningEngine:
        return self._engine

    @property
    def history(self) -> HistoryStore:
        return self._history

    @property
    def run_id(self) -> Optional[str]:
        """The id of the current or most recent run, once one has started."""
        return self._evidence.run_id

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
        # Unusable replies were still paid for: their usage is recorded with
        # the next decision, or with run_stopped if the loop gives up.
        self._failed_usage = []

        for attempt in range(self._max_retries + 1):
            try:
                proposal = self._engine.reason(self._request, project_id)
            except ReasoningError as error:
                detail = str(error)
                if getattr(error, "usage", None):
                    self._failed_usage.append(error.usage)
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

    # --- history ----------------------------------------------------------

    def _record(self, event_type, run_id, project_id, task_id=None, **payload):
        return self._history.append(
            type=event_type,
            run_id=run_id,
            session_id=self._session_id,
            project_id=project_id,
            task_id=task_id,
            payload=payload,
        )

    def _current_task(self, operation):
        """The task an operation names, as it is now; None if there is none."""
        project_id = operation.arguments.get("project_id")
        task_id = self._task_of(operation)
        if not isinstance(project_id, str) or task_id is None:
            return None
        try:
            return self._master.project_state(project_id).get_task(task_id)
        except EXPECTED_ERRORS:
            return None

    @staticmethod
    def _task_of(operation) -> Optional[str]:
        if operation is None:
            return None
        task_id = operation.arguments.get("task_id")
        return task_id if isinstance(task_id, str) and task_id.strip() else None

    def _failed_call_usage(self):
        failed = getattr(self, "_failed_usage", None)
        return add_usage(failed) if failed else None

    def _record_decision(self, run_id, project_id, step, decision, gate_reason=None,
                         proposal=None, gate_attempt_id=None):
        operation = decision.operation if decision is not None else None
        self._record(
            EventType.DECISION,
            run_id,
            project_id,
            task_id=self._task_of(operation),
            step=step,
            decision=decision.decision.value if decision is not None else None,
            reason=decision.reason if decision is not None else None,
            operation=(
                {"name": operation.operation, "arguments": dict(operation.arguments)}
                if operation is not None
                else None
            ),
            pending_approval=bool(decision is not None and decision.pending_approval),
            gate_reason=gate_reason,
            gate_attempt_id=gate_attempt_id,
            usage=getattr(proposal, "usage", None),
            reasoner=getattr(proposal, "reasoner", None),
            failed_call_usage=self._failed_call_usage(),
        )
        self._failed_usage = []

    def _record_result(self, run_id, project_id, step, task_id, outcome):
        self._record(
            EventType.OPERATION_RESULT,
            run_id,
            project_id,
            task_id=task_id,
            step=step,
            operation=outcome.operation,
            status=outcome.status.value,
            reason=outcome.reason.value if outcome.reason is not None else None,
            message=outcome.message,
            value=jsonable(outcome.value),
        )

    def _record_refusal(self, run_id, project_id, step, operation, reason, message):
        self._record(
            EventType.OPERATION_RESULT,
            run_id,
            project_id,
            task_id=self._task_of(operation),
            step=step,
            operation=operation.operation,
            status="refused",
            reason=reason,
            message=message,
            value=None,
        )

    # --- the loop -------------------------------------------------------

    def run(self, project_id: str) -> LoopResult:
        """Run until Master stops or nothing is actionable.

        Returns the stop reason and the final decision when there is one.
        Stopping is a result, not an exception: an ordinary WAIT, an
        unusable model reply and a refused operation all come back as a
        :class:`LoopResult` with authoritative state exactly as it was.

        An exception -- a worker or verifier that raised, a broken project
        file -- is recorded as ``run_error`` and propagates. The loop never
        swallows it, and never retries the step that raised.
        """
        if not isinstance(project_id, str) or not project_id.strip():
            raise ValueError("project_id must be a non-empty string")

        if self._lock is not None:
            return self._run_locked(project_id)

        # Every autonomous run holds the project lock; take it if the caller
        # did not. ProjectBusyError propagates before anything is recorded.
        project_path = self._master.project_state(project_id).project_path
        with ProjectLock(project_path, holder="autonomous loop") as lock:
            self._lock = lock
            # Holding the lock proves nothing else runs here: close out what
            # dead processes left open before starting.
            recover_project(self._history, project_id,
                            worktrees=self._orchestrator.worktrees)
            try:
                return self._run_locked(project_id)
            finally:
                self._lock = None

    @property
    def lock(self) -> Optional[ProjectLock]:
        """The lock held for the current run, if any."""
        return self._lock

    def _run_locked(self, project_id: str) -> LoopResult:
        run_id = uuid.uuid4().hex
        self._evidence.run_id = run_id
        self._record(
            EventType.RUN_STARTED,
            run_id,
            project_id,
            request=self._request,
            max_steps=self._max_steps,
        )
        self._steps = 0
        try:
            result = self._run(project_id, run_id)
        except BaseException as error:
            self._record(
                EventType.RUN_ERROR,
                run_id,
                project_id,
                error_type=type(error).__name__,
                message=str(error),
                step=self._steps,
            )
            raise

        self._record(
            EventType.RUN_STOPPED,
            run_id,
            project_id,
            stop_reason=result.stop_reason,
            steps=result.steps,
            detail=result.detail,
            failed_call_usage=self._failed_call_usage(),
        )
        self._failed_usage = []
        return result

    def _run(self, project_id: str, run_id: str) -> LoopResult:
        steps = 0
        last_output = None

        def stop(reason, decision=None, detail=None, approval_reason=None):
            return LoopResult(
                stop_reason=reason,
                steps=steps,
                decision=decision,
                last_output=last_output,
                detail=detail,
                run_id=run_id,
                approval_reason=approval_reason,
            )

        while steps < self._max_steps:
            if self._actionable_task(project_id) is None:
                return stop(STOP_NO_WORK)

            if self._stop_check is not None:
                exhausted = self._stop_check()
                if exhausted:
                    return stop(STOP_BUDGET, detail=exhausted)

            steps += 1
            self._steps = steps

            proposal, decision, unusable = self._decide(project_id)
            if proposal is None:
                return stop(STOP_UNUSABLE_REPLY, detail=unusable)

            # A completed status applies on its own only with evidence that the
            # latest attempt passed verification. Worked out before the
            # decision is recorded so the record says why it is waiting.
            gate_reason = gate_attempt_id = None
            if (
                decision is not None
                and decision.decision is Decision.ACT
                and decision.operation is not None
                and not decision.pending_approval
            ):
                gate_reason, gate_attempt_id = self._evidence.completion_check(
                    decision.operation, self._current_task(decision.operation)
                )

            # The decision is the recorded intent: it is written before any
            # state changes or any worker runs.
            self._record_decision(run_id, project_id, steps, decision, gate_reason, proposal,
                                  gate_attempt_id)

            # Anything but an explicit ACT stops the loop. This covers WAIT,
            # BLOCKED, NEEDS_INFORMATION, REQUEST_APPROVAL, and a reply that
            # carried no decision at all: the safe reading of an answer we
            # cannot act on is that we should not act on it.
            if decision is None or decision.decision is not Decision.ACT:
                return stop(STOP_MASTER, decision)

            # An ACT on something policy gates does not become an instruction
            # just because the model said ACT. Policy decides, not the model.
            if decision.pending_approval:
                return stop(STOP_APPROVAL, decision, approval_reason="policy")

            # H-D3: in a project the system integrates itself, a passing
            # attempt that could not be integrated (a conflict with newer
            # work) is not a question for a human: the task runs again from
            # the new base. Refused, and Master decides again.
            if gate_reason == "not_integrated":
                self._record_refusal(
                    run_id, project_id, steps, decision.operation, gate_reason,
                    "the latest attempt passed but is not in the development branch (it "
                    "conflicted with newer work); run the task again: the next attempt "
                    "starts from the current base")
                continue

            # Completing without passing evidence is not refused, it is held
            # for a human: they may know something the evidence does not.
            if gate_reason is not None:
                return stop(
                    STOP_APPROVAL,
                    decision,
                    detail=f"completion requires a human: {gate_reason}",
                    approval_reason=gate_reason,
                )

            operation = decision.operation
            spec = SPECS.get(operation.operation)

            # Policy gates INTEGRATE above; this keeps that true even if an
            # impact level is ever mis-set.
            if spec is not None and spec.kind is OperationKind.INTEGRATE:
                return stop(STOP_APPROVAL, decision, approval_reason="policy")

            # run_task: the only way the worker runs. It changes no project
            # state and is refused unless the task is already in_progress in
            # the project this loop is running.
            if spec is not None and spec.kind is OperationKind.DISPATCH:
                task_id = operation.arguments.get("task_id")
                refusal = None
                if operation.arguments.get("project_id") != project_id:
                    refusal = (
                        "wrong_project",
                        f"run_task may only target project {project_id!r}",
                    )
                else:
                    try:
                        self._orchestrator.prepare(project_id, task_id)
                    except WorkspaceRefusal as error:
                        refusal = (error.reason, str(error))
                    except ExecutionError as error:
                        refusal = ("not_executable", str(error))
                if refusal is None and self._evidence.attempt_limit_reached(
                    project_id, task_id
                ):
                    refusal = (
                        "attempt_limit",
                        f"task {task_id!r} has had {self._evidence.max_attempts} "
                        "attempt(s) in this session",
                    )
                if refusal is not None:
                    self._record_refusal(run_id, project_id, steps, operation, *refusal)
                    return stop(
                        STOP_ATTEMPT_LIMIT
                        if refusal[0] == "attempt_limit"
                        else STOP_OPERATION_FAILED,
                        decision,
                        f"{operation.operation} -> refused ({refusal[0]}): {refusal[1]}",
                    )

                last_output = self._orchestrator.orchestrate(
                    project_id,
                    task_id,
                    run_id=run_id,
                    session_id=self._session_id,
                    step=steps,
                    lock_fd=self._lock.fileno(),
                )
                continue

            # A state operation. Master is the only writer. History and the
            # YAML project files are separate stores: the decision above is
            # the intent, the mutation happens here, and operation_result
            # follows. A crash between the two leaves ProjectState
            # authoritative and an audit gap in history, nothing more.
            batch = proposal.execute(self._interface)
            for outcome in batch.results:
                self._record_result(
                    run_id, project_id, steps, self._task_of(operation), outcome
                )
            if not batch.all_successful:
                return stop(
                    STOP_OPERATION_FAILED, decision, self._describe_failures(batch)
                )

        return stop(STOP_STEP_LIMIT)


def run_autonomous(
    project_id: str,
    *,
    master: Master,
    provider: ReasoningProvider,
    execution_backend: ExecutionBackend,
    verification_backend: VerificationBackend,
    request: str = DEFAULT_REQUEST,
    max_steps: int = 20,
    history: Optional[HistoryStore] = None,
    max_attempts_per_task: int = DEFAULT_MAX_ATTEMPTS,
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
        history=history,
        max_attempts_per_task=max_attempts_per_task,
    ).run(project_id)
