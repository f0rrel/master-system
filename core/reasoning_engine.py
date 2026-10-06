"""The controlled reasoning loop: request, model, proposal, approval, execution.

    ReasoningEngine
          |
          +-- builds prompt + JSON schema from SPECS, calls a ReasoningProvider
          |
          v
    ReasoningInterface.propose()   <- every operation passes the allowlist here
          |
          v
    human approval
          |
          v
    ReasoningInterface.execute()   <- Master, and only Master, mutates anything

This module is the whole of the model-backed system: it turns a human sentence
and some project state into a proposal, and it does not execute anything on its
own. Two properties matter more than any feature here.

Reasoning never mutates
-----------------------
:meth:`ReasoningEngine.reason` performs no writes at all. It reads through
Master's inspection methods and asks a provider for text. The first moment any
state can change is :meth:`Proposal.execute`, which requires a proposal that a
human has approved and which still routes every operation through
ReasoningInterface, so Master remains the only writer.

A proposal is data, and invalid data stays invalid
--------------------------------------------------
The model's reply is parsed as JSON and nothing else. There is no ``eval``, no
``exec``, no import, and no attempt to interpret a reply as anything other than
an object with an ``operations`` list. Each entry is handed to
``ReasoningInterface.propose``, which applies the same allowlist and structural
checks as any other caller. An entry that fails is kept in the proposal as a
visible, non-executable error rather than being repaired, guessed at, or
dropped quietly.

The prompt and the JSON schema are both generated from ``core.reasoning.SPECS``
rather than written by hand. That is what keeps the model's instructions honest:
the catalogue of operations it is shown is the same table that will judge its
answer, so adding an operation to the system cannot leave the prompt stale, and
an operation the model was never told about cannot reach Master.

Project context
---------------
The model is told what it needs to reason and nothing more. Context comes from
Master's inspection methods and is reduced to plain project facts: identity,
status, progress, milestones, tasks. With an evidence source, it also carries
execution evidence read from history -- attempt counts and each in-progress
task's latest outcome and verification -- and the session's recent decisions.
Worker output and worker identity are never part of it.

One field is deliberately dropped. ``Master.status()`` includes an absolute
filesystem ``path``, which is reasonable for a human reading a terminal and
pointless to a model choosing what work to propose. Removing it here rather than
in ``Master.status()`` keeps that decision visible at the boundary where it
belongs, and means the model is never shown a filesystem path at all. The field
is reported as a limitation of ``Master.status()`` rather than silently relied
on.
"""

import json
import sys
from dataclasses import dataclass, replace
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.master import Master
from core.provider import ProviderError
from core.reasoning import (
    SPECS,
    ApprovalState,
    BatchResult,
    InvalidOperationError,
    Operation,
    ReasoningInterface,
    Decision,
    MasterDecision,
)

__all__ = [
    "Proposal",
    "ProposalEntry",
    "ReasoningEngine",
    "ReasoningError",
    "build_operation_schema",
]


class ReasoningError(RuntimeError):
    """Raised when a request produced no usable proposal.

    This is the failed-reasoning-request case: unparseable output, an envelope
    of the wrong shape, or a provider that could not be reached. It is distinct
    from an invalid individual operation, which is recorded in the proposal and
    displayed rather than raised, because one bad entry should not hide the rest
    of a human's answer from review.

    ``usage`` carries the provider's token usage when a call was made, so an
    unusable reply is still counted.
    """

    usage = None


# --- prompt and schema, both derived from the allowlist ------------------


def build_operation_schema():
    """Return a JSON schema describing what a valid proposal looks like.

    Generated from ``core.reasoning.SPECS``, so the shape offered to the model
    cannot drift from the shape that will be validated. Update operations take
    arbitrary field names, so their schema allows extra scalar properties;
    ``ReasoningInterface.propose`` is what decides whether a proposed field is
    meaningful.
    """
    operations = {}
    for name, spec in SPECS.items():
        properties = {
            argument: {"type": "string"}
            for argument in spec.required
        }
        properties.update(
            {
                argument: {"type": ["string", "null"]}
                for argument in spec.optional
            }
        )

        operation_schema = {
            "type": "object",
            "properties": dict(sorted(properties.items())),
            "required": list(spec.required),
        }

        if spec.variadic:
            operation_schema["additionalProperties"] = {
                "type": ["string", "number", "boolean", "null"]
            }

        operations[name] = operation_schema

    op_item = {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": sorted(SPECS),
            },
            **{
                argument: {"type": "string"}
                for spec in SPECS.values()
                for argument in spec.required
                if argument != "operation"
            },
        },
        "required": ["operation"],
    }
    return {
        "type": "object",
        "properties": {
            "decision": {
                "type": "string",
                "enum": ["act", "wait", "blocked", "needs_information", "request_approval"],
            },
            "reason": {
                "type": "string",
                "description": "Brief explanation of the decision.",
            },
            "operation": {
                "oneOf": [op_item, {"type": "null"}],
            },
        },
        "required": ["decision", "reason", "operation"],
    }


def describe_operations():
    """Return a human-readable catalogue of the operations the model may use."""
    lines = []
    for name, spec in SPECS.items():
        if spec.variadic:
            arguments = ", ".join(
                list(spec.required)
                + ["<field>=<new value> (one or more)"]
            )
        else:
            arguments = ", ".join(list(spec.required) + list(spec.optional))
        lines.append(f"- {name}({arguments})")
    return "\n".join(lines)


PROMPT_TEMPLATE = """\
You are Master, the strategic orchestrator for a project management system.

Your role:
- Understand the current situation from authoritative project state.
- Respect authoritative facts and constraints; do not invent facts.
- Identify feasible actions and consider consequences and alternatives.
- Select the smallest useful next step toward the user's goal.
- Do not assume the target/requested milestone is necessarily the next place to work.
- You may decide that no action should be taken.

Reply with a single JSON object and nothing else. Do not write code, do not
write explanations outside the JSON, and do not invent operations that are not
listed below.

The JSON object must have these keys:
  "decision"  - one of: act, wait, blocked, needs_information, request_approval
  "reason"    - a short string explaining your choice
  "operation" - an operation object or null

Valid decisions:
- act: requires exactly one valid operation (you will perform this action next)
- wait: requires operation = null (no action now; wait for conditions)
- blocked: requires operation = null (cannot proceed due to blocking condition)
- needs_information: requires operation = null (need more info before deciding)
- request_approval: requires exactly one valid operation (action that needs approval)

When decision is "act" or "request_approval", you must include a valid operation
from the allowed list. For other decisions, "operation" must be null.

OPERATION SHAPE
An operation is a single FLAT JSON object. Its operation name goes in the
"operation" key, and its arguments are siblings of that key at the same level.
Do not nest arguments under "name", "params", "args" or "operation_type", and
do not write the operation name outside the "operation" key. This is valid:

{{
  "decision": "act",
  "reason": "t1 has no unmet dependencies, so start it",
  "operation": {{
    "operation": "update_task",
    "project_id": "p",
    "task_id": "t1",
    "status": "in_progress"
  }}
}}

And this is valid when you are choosing not to act:

{{
  "decision": "wait",
  "reason": "nothing is ready yet",
  "operation": null
}}

OPERATIONS YOU MAY USE
{catalogue}

PROJECT STATE
{context}

ADDITIONAL RULES
- Deterministic code is authoritative for state and operation validity.
- Use only milestone ids and task ids that already appear in PROJECT STATE,
  except when you are creating a brand new id.
- Propose the smallest change that answers the request.
- If no action is appropriate, choose wait/blocked/needs_information with operation null.
- Anything not in the list above will be rejected.
- "ready_tasks" are in backlog order (epic priority first). Work on them in
  that order unless the evidence gives a reason not to. Never change a milestone
  whose status is "proposed": it is an unapproved backlog epic. A task whose
  readiness is "waiting_for_owner" waits for a human (for example to pick an
  image): do not start or run it.
- If PROJECT STATE has "direction", it is the owner's standing intent for the
  project. Judge every proposal against it: never create or change a task in a
  way that contradicts it; if the request conflicts with it, choose
  needs_information and say why.

WHAT THIS SYSTEM CAN DO
You do not write code and you do not edit files yourself. A separate worker
process performs implementation. What you do is choose the next action and
express it as an operation, and the system carries it out:
- You can start a task by proposing update_task with status "in_progress".
  That only records that the task is being worked on; it does not run anything.
- To have the worker execute a task, propose run_task for a task that is
  already in_progress. run_task changes no state, and it is refused for any
  task that is not in_progress. Starting work therefore takes two decisions:
  update_task to in_progress, then run_task.
- Each attempt runs in a fresh copy of the project's repository. After it, a
  verifier runs the task's acceptance checks, if a human defined them
  ("has_acceptance"). PROJECT STATE then shows, under "execution_evidence",
  each task's attempts in this session, its attempts in total, and its latest
  attempt:
  - "outcome", observed by the system: finished, timed_out, error,
    interrupted or unfinished;
  - "worker_reported_status": what the worker claimed. It is a claim, not a fact;
  - "changes": files changed, insertions, deletions;
  - "spec_current": whether the attempt was made against the task's current
    title and acceptance;
  - "verification": verdict, summary and findings.
  "recent_decisions" lists your last decisions.
- An "interrupted" attempt means the worker started but the system stopped
  before its outcome was recorded; what it did is uncertain. Nothing is retried
  for you: decide whether to run_task again, block the task, or ask for help.
- Each task may be run at most "attempt_limit" times per session. Beyond that,
  run_task is refused and a human has to look.
- You can then record the outcome with an operation. Marking a task
  "completed" applies on its own only when its latest attempt finished, was made
  against the current spec, and was verified as "pass"; otherwise it waits for
  a human. Changing a task's title makes earlier evidence stale.
- An attempt's work reaches the project's real branch only when a human
  integrates it. You may ask for that with integrate_attempt (naming the
  attempt); it always waits for a human, and it never changes task status. You can mark it "blocked" whenever
  the evidence supports that.
So a task's text describing implementation or testing does not mean you must do
that yourself, and does not mean you cannot advance it. Judge progress by the
state and the execution evidence, and choose the next operation from those.

REQUEST
{request}
"""


# --- proposal -----------------------------------------------------------


@dataclass(frozen=True)
class ProposalEntry:
    """One operation the model asked for, and whether it survived validation."""

    raw: object
    operation: Operation = None
    error: InvalidOperationError = None

    @property
    def valid(self):
        return self.operation is not None

    @property
    def name(self):
        if isinstance(self.raw, dict):
            candidate = self.raw.get("operation")
            if isinstance(candidate, str):
                return candidate
        if self.operation is not None:
            return self.operation.operation
        return None

    def describe(self):
        """A short label for display, e.g. 'create_task'."""
        return self.name or "<unrecognised>"


@dataclass(frozen=True)
class Proposal:
    """A model's answer, awaiting a human decision.

    Holding this object changes nothing. It becomes executable only after
    :meth:`approve`, which is the programmatic stand-in for a person saying yes.
    """

    request: str
    reasoning: str = ""
    entries: tuple = ()
    state: ApprovalState = ApprovalState.PROPOSED
    _decision: "MasterDecision | None" = None
    #: Token usage of the provider call that produced this proposal, and the
    #: provider's own name: provenance for accounting, never interpreted.
    usage: "dict | None" = None
    reasoner: "str | None" = None

    @property
    def valid_entries(self):
        return tuple(entry for entry in self.entries if entry.valid)

    @property
    def invalid_entries(self):
        return tuple(entry for entry in self.entries if not entry.valid)

    @property
    def is_empty(self):
        return not self.valid_entries

    def approve(self):
        """Return an approved copy whose operations are approved with it.

        Approving the proposal also approves each operation inside it, because
        the human's yes covers the operations they were shown. Operations that
        failed validation have no ``Operation`` to approve and stay inert.
        """
        entries = tuple(
            replace(entry, operation=entry.operation.approve())
            if entry.valid
            else entry
            for entry in self.entries
        )
        return replace(self, entries=entries, state=ApprovalState.APPROVED)

    def reject(self):
        """Return a rejected copy. Nothing in it can ever execute."""
        return replace(self, state=ApprovalState.REJECTED)

    def execute(self, interface):
        """Execute this proposal's operations through the interface.

        Behaviour by state, which is the whole point of the state:

        * proposed  - a ROUTINE operation still runs, because policy approves
          it; anything policy gates is reported REJECTED, so a caller that
          wanted human approval on a gated operation gets a visible refusal
        * rejected  - nothing runs and no results are produced
        * approved  - the valid operations run, in order, each through
          ReasoningInterface, which re-checks and delegates to Master

        Not atomic: a later failure does not undo an earlier success.
        """
        if self.state is ApprovalState.REJECTED:
            return BatchResult(results=())

        operations = [
            entry.operation for entry in self.entries if entry.valid
        ]
        return interface.execute_batch(operations)


# --- engine -------------------------------------------------------------


class ReasoningEngine:
    """Turns a human request plus project state into an approvable proposal."""

    def __init__(self, provider, master=None, interface=None, evidence_source=None,
                 direction_source=None, waiting_source=None):
        if master is None:
            master = Master()
        if interface is None:
            interface = ReasoningInterface(master)
        self._provider = provider
        self._master = master
        self._interface = interface
        #: Supplies execution evidence read from history, e.g.
        #: :class:`core.evidence.HistoryEvidence`. Optional: without one the
        #: model sees project state only.
        self._evidence_source = evidence_source
        #: project_id -> the project's direction text or None (core.direction).
        #: Optional: without one the model sees no direction.
        self._direction_source = direction_source
        #: project_id -> {task id: why it waits for the owner} (e.g. an image pick).
        self._waiting_source = waiting_source

    @property
    def provider(self):
        return self._provider

    def context_for(self, project_id):
        """Return the project facts a model is allowed to see.

        Read-only, via Master's inspection methods, reduced to plain data. The
        absolute filesystem path that ``Master.status()`` includes is omitted
        on purpose; see the module docstring.
        """
        status = self._master.status(project_id)

        from core.work_manager import calculate_readiness

        project_state = self._master.project_state(project_id)
        from core.backlog import epic_rank, ordered_tasks

        waiting = (self._waiting_source(project_id) or {}) if self._waiting_source else {}
        # Backlog order: epic priority, then file order. ready_tasks keeps it.
        milestones = sorted(status.get("milestones", []),
                            key=lambda m: epic_rank(status.get("milestones", []))[m.get("id")])
        tasks_with_readiness = []
        for task in ordered_tasks(status.get("milestones", []), status.get("tasks", [])):
            readiness, blocked_by, reason = calculate_readiness(project_state, task)
            if readiness == "ready" and task.get("id") in waiting:
                readiness, reason = "waiting_for_owner", waiting[task.get("id")]
            tasks_with_readiness.append(
                {
                    "id": task.get("id"),
                    "milestone": task.get("milestone"),
                    "title": task.get("title"),
                    "status": task.get("status"),
                    "assigned_to": task.get("assigned_to"),
                    "readiness": readiness,
                    "blocked_by": blocked_by,
                    "readiness_reason": reason,
                    # Whether a human has defined "done" for this task. The
                    # acceptance commands themselves are not shown.
                    "has_acceptance": bool(task.get("acceptance")),
                    "has_description": bool(task.get("description")),
                }
            )

        ready_tasks = [t for t in tasks_with_readiness if t["readiness"] == "ready"]
        blocked_tasks = [t for t in tasks_with_readiness if t["readiness"] == "blocked"]
        in_progress_tasks = [t for t in tasks_with_readiness if t["status"] == "in_progress"]
        completed_tasks = [t for t in tasks_with_readiness if t["status"] == "completed"]

        context = {
            "project_id": status.get("project_id"),
            "name": status.get("name"),
            "status": status.get("status"),
            "progress": status.get("progress"),
            "milestones": [
                {
                    "id": milestone.get("id"),
                    "name": milestone.get("name"),
                    "status": milestone.get("status"),
                }
                for milestone in milestones
            ],
            "tasks": tasks_with_readiness,
            "ready_tasks": ready_tasks,
            "blocked_tasks": blocked_tasks,
            "in_progress_tasks": in_progress_tasks,
            "completed_tasks": completed_tasks,
        }
        if self._direction_source is not None:
            direction = self._direction_source(project_id)
            if direction:
                context["direction"] = direction
        if self._evidence_source is not None:
            context.update(
                self._evidence_source.for_project(
                    project_id,
                    [t["id"] for t in in_progress_tasks],
                    {t.get("id"): t for t in status.get("tasks", [])},
                )
            )
        return context

    def build_prompt(self, request, project_id):
        prompt = PROMPT_TEMPLATE.format(
            catalogue=describe_operations(),
            context=json.dumps(self.context_for(project_id), indent=2),
            request=request,
        )
        return prompt

    def reason(self, request, project_id):
        """Ask the model for a proposal. Never mutates project state.

        A provider failure or unusable reply raises :class:`ReasoningError`, so
        a failed reasoning request is always distinguishable from a rejected
        operation and never turns into an executable one.
        """
        if not isinstance(request, str) or not request.strip():
            raise ReasoningError("request must be a non-empty string")

        prompt = self.build_prompt(request, project_id)
        try:
            reply = self._provider.complete(prompt, schema=build_operation_schema())
        except ProviderError as error:
            failure = ReasoningError(str(error))
            failure.usage = _provider_usage(self._provider)
            raise failure from error
        usage = _provider_usage(self._provider)
        try:
            proposal = self.parse(reply, request)
        except ReasoningError as error:
            error.usage = usage
            raise
        return replace(proposal, usage=usage, reasoner=_provider_name(self._provider))

    def parse(self, reply, request=""):
        """Turn raw model text into a Proposal/decision structure.

        Supports both the new decision contract and legacy format for
        compatibility during transition.
        """
        document = _loads_strictly(reply)

        if not isinstance(document, dict):
            raise ReasoningError(
                f"model reply must be a JSON object, got {type(document).__name__}"
            )

        # New contract
        if "decision" in document:
            decision_str = document.get("decision")
            if not isinstance(decision_str, str):
                raise ReasoningError("'decision' must be a string")
            try:
                decision = Decision(decision_str)
            except ValueError:
                raise ReasoningError(f"invalid decision: {decision_str}")

            reason = document.get("reason", document.get("reasoning", ""))
            if not isinstance(reason, str):
                raise ReasoningError("'reason' must be a string")

            op_raw = document.get("operation")
            operation = None
            if op_raw is not None:
                if not isinstance(op_raw, dict):
                    raise ReasoningError("'operation' must be an object or null")
                entry = self._entry_for(op_raw)
                if not entry.valid:
                    # Re-raise validation error clearly
                    if entry.error:
                        raise ReasoningError(str(entry.error))
                operation = entry.operation

            try:
                master_decision = MasterDecision(
                    decision=decision, reason=reason, operation=operation
                )
            except (ValueError, InvalidOperationError) as e:
                raise ReasoningError(str(e))

            # Return as Proposal for backward compatibility with CLI
            entries = ()
            if master_decision.operation is not None:
                entries = (ProposalEntry(raw=op_raw, operation=master_decision.operation),)
            return Proposal(
                request=request,
                reasoning=master_decision.reason,
                entries=entries,
                _decision=master_decision,
            )

        # Legacy format (operations list)
        reasoning = document.get("reasoning", "")
        if not isinstance(reasoning, str):
            raise ReasoningError("'reasoning' must be a string")

        operations = document.get("operations")
        if not isinstance(operations, list):
            raise ReasoningError(
                "'operations' must be a list of operation objects"
            )

        entries = []
        for raw in operations:
            if not isinstance(raw, dict):
                raise ReasoningError(
                    "each operation must be a JSON object, got "
                    f"{type(raw).__name__}"
                )
            entries.append(self._entry_for(raw))

        return Proposal(
            request=request,
            reasoning=reasoning,
            entries=tuple(entries),
        )

    def _entry_for(self, raw):
        """Validate one model-proposed operation, keeping failures visible."""
        try:
            operation = self._interface.propose(raw)
        except InvalidOperationError as error:
            return ProposalEntry(raw=raw, error=error)
        return ProposalEntry(raw=raw, operation=operation)


def _provider_usage(provider):
    """The provider's last token usage, if it reports any (accounting only)."""
    try:
        return provider.last_usage
    except AttributeError:
        return None


def _provider_name(provider):
    try:
        return str(provider.name)
    except AttributeError:
        return None


def _loads_strictly(reply):
    """Parse model text as JSON, or raise ReasoningError.

    Deliberately narrow. A reply wrapped in a markdown fence is unwrapped; a
    reply with prose around the JSON is not, because guessing where the JSON
    starts and stops is how unparseable model output turns into a half-understood
    instruction. There is no ``eval`` and no fallback parser.
    """
    if not isinstance(reply, str):
        raise ReasoningError(
            f"model reply must be text, got {type(reply).__name__}"
        )

    text = reply.strip()

    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        # Drop the opening fence, its optional language tag, and the closing one.
        lines = lines[1:-1]
        if lines and lines[0].strip().lower() in ("json", "jsonc", ""):
            lines = lines[1:]
        text = "\n".join(lines).strip()

    try:
        return json.loads(text)
    except ValueError as error:
        raise ReasoningError(
            f"model reply was not valid JSON: {error}"
        ) from error