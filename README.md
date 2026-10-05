# Master System: a model-independent AI Work System

> **Read this first.** This is the onboarding document for anyone, human or AI agent,
> who picks up this project after a break, a lost conversation, or an agent switch.
> It covers the goal, the architecture, the current state, the plan, and the rules for
> working here. If you change something this file describes, update the file in the same commit.

| | |
| --- | --- |
| Last updated | 2026-10-05 |
| Written against | branch `m1-contain-verify` (from `claude-week` `dab0928`) |
| Owner | f0rrel (GitHub) |
| Local path (owner) | `~/AI/master-system-big-pickle` |
| Remote | `https://github.com/f0rrel/master-system` (private) |

---

## Contents

1. [What this is](#1-what-this-is)
2. [Status snapshot](#2-status-snapshot)
3. [Principles: the invariants](#3-principles-the-invariants)
4. [Constraints](#4-constraints)
5. [Tech stack and environment](#5-tech-stack-and-environment)
6. [Repository map](#6-repository-map)
7. [Architecture](#7-architecture)
8. [Data and runtime files](#8-data-and-runtime-files)
9. [Trust boundaries](#9-trust-boundaries)
10. [How to run things](#10-how-to-run-things)
11. [Known gaps](#11-known-gaps)
12. [Roadmap](#12-roadmap)
13. [Decision log](#13-decision-log)
14. [Ecosystem stance](#14-ecosystem-stance)
15. [Rules for working in this repo](#15-rules-for-working-in-this-repo)
16. [Resuming after a break](#16-resuming-after-a-break)
17. [Open questions for the owner](#17-open-questions-for-the-owner)
18. [Glossary](#18-glossary)
19. [References](#19-references)

---

## 1. What this is

A **persistent AI control plane** that manages real software projects over long
periods with minimal human intervention.

Four roles divide the work:

- A reasoning model, the **Master**, decides what should happen next.
- Interchangeable **workers** (coding agents) do the work.
- **Verification** checks the result.
- Everything is recorded as **evidence**, and a **human** approves the consequential steps.

**Target experience:**

> Give the system an objective, let it work for hours or overnight, watch the progress,
> and step in only when it is blocked or a consequential decision needs a human.

The human should increasingly become a **supervisor and approval layer**, not the
person copying prompts between AI agents.

### Long-term capabilities

The eventual system should be able to:

- understand a high-level objective and break it into tasks;
- research unfamiliar technologies and approaches;
- plan and prioritise work;
- execute coding and development tasks (through existing coding agents);
- run tests and QA, operate tools and infrastructure, and inspect results;
- recover from failures and continue after interruptions or crashes;
- keep persistent project state, history and memory;
- make decisions over hours, days and weeks;
- ask a human for approval only when it is genuinely necessary;
- learn from **validated** experience;
- eventually improve parts of its own system;
- support different models, coding agents, tools and execution backends without being
  tied to any one of them.

**Self-modification is a long-term goal, not current work.** The architecture should
leave room for it without implementing it early.

### Evolution path

| Stage | Meaning |
| --- | --- |
| V1 | Single machine, one trustworthy autonomous loop per project |
| V2 | Richer autonomy: round-trip approvals, deadlines, retries, budgets, overnight runs |
| V3 | Several agents and projects, with concurrent attempts |
| V4 | A self-improving system: the control-plane repo becomes one more project, under stricter policy |

---

## 2. Status snapshot

*Update this section at the end of every milestone.*

| Item | State (2026-10-05) |
| --- | --- |
| Current milestone | **Milestone 1, "Contain and verify"** (approved 2026-10-05), on branch `m1-contain-verify`. It closes the earlier "Trustworthy loop" milestone, which is implemented on `claude-week` but not closed (see [§11](#11-known-gaps)). Status: **plan written, waiting for owner approval** (`docs/plans/m1-contain-verify.md`). |
| Tests | 983 passed, 1 skipped, 5 integration tests deselected (`python -m pytest -q -m "not integration"`) |
| Can the autonomous loop be run from a CLI? | **No.** It runs only inside tests. No composition root exists yet (gap H5). |
| Production verifier in `core/`? | **No.** The only real verifier is a test helper (`SubprocessPytestVerifier` in `tests/test_ollama_backend.py`). |
| Workers (execution backends) | `OpenCodeCliBackend` (runs `opencode run`) and `OllamaExecutionBackend` (a homemade tool loop, to be frozen) |
| Reasoning providers (for Master) | Ollama, OpenCode server, DeepSeek API |
| Branches | `main` = `49c0a4e` (baseline). `claude-week` holds the "Trustworthy loop" work and this README. `m1-contain-verify` branches from `claude-week`. Nothing is merged into `main`. |
| Managed project | `projects/ai-system` is the system's own project. Its tasks are stale: `foundation-001` and `-002` are `in_progress`, although that work was done by hand. **Self-hosting is stopped until Milestone 3** (approved 2026-10-05): no worker runs against this repository. |

### Next actions

1. Owner reviews and approves the Milestone 1 plan (`docs/plans/m1-contain-verify.md`).
2. Implement Milestone 1 in small, green, pushed commits; tick the [§12](#12-roadmap)
   acceptance boxes as each criterion passes.

---

## 3. Principles: the invariants

These rules *are* the architecture. Changing one is a decision: record it in
[§13](#13-decision-log), together with the owner's approval.

1. **Project state describes facts; operations request side effects.** Setting a task
   to `in_progress` never runs a worker. Only an explicit `run_task` decision does.
2. **Master is the only writer of project state** (`core/master.py`, through
   `WorkManager`). Everything else, whether models, workers or verifiers, only proposes.
3. **Models propose, policy disposes.** A model's `act` is a request. The approval policy
   decides whether it runs, and it never takes into account which model asked.
4. **Worker output is untrusted.** Worker `state_updates` are never applied. Raw worker
   output is stored as opaque provenance and never shown to Master.
5. **Record provenance; never branch on it.** No code path may depend on which model,
   provider or worker ran.
6. **Record intent before every side effect.** A `decision` event is written before a state
   change, and an `attempt_started` event is committed before a worker runs.
7. **Never automatically replay an uncertain side effect.** An attempt with no recorded
   outcome is `interrupted`; Master or a human decides what happens next.
8. **Infrastructure retries are not semantic task attempts.**
9. **Don't fake atomicity.** YAML project state and the SQLite history are separate stores.
   YAML is authoritative, and a crash between the two leaves an audit gap, not corruption.
10. **A QA pass is not completion. A QA fail is not a retry. An execution failure is not a state transition.**
    Completion needs explicit authorisation and evidence.
11. **Model independence.** No provider, model or worker name appears above the adapters;
    tests enforce this.
12. **Don't build our own coding agent.** Use mature coding agents as interchangeable
    workers.
13. **A human can always approve.** The control plane is never mutated autonomously
    without oversight.
14. **Fail closed.** Unknown operations are gated. An unparseable model reply stops the loop
    and is never repaired or guessed at.
15. **Build the product-specific control plane; borrow mature infrastructure** rather than
    reinventing durable execution.

---

## 4. Constraints

- A single developer, on a local Linux machine with modest hardware.
- No distributed or cloud infrastructure required (for now).
- Both cloud and local models may be used. **Cost matters.**
- Execution agents will change over time.
- **Reliability matters more than maximum autonomy.**
- Human approval must always remain possible.
- The design must stay understandable by one person; no premature enterprise infrastructure.
- No coupling of the core to any model, provider or agent.

---

## 5. Tech stack and environment

### Code

| Area | Choice |
| --- | --- |
| Language | Python ≥ 3.12 (the owner runs 3.12.3; 3.13 also passes) |
| Runtime dependencies | **PyYAML only** (`pyproject.toml`) |
| Dev dependencies | pytest ≥ 8 |
| Package manager | uv (`[tool.uv] package = false`, so this is not an installable package) |
| Authoritative state | YAML files, written atomically (temp file, fsync, rename) |
| History / evidence | SQLite through the stdlib `sqlite3`: WAL, `synchronous=FULL`, append-only enforced by triggers |
| Locking | `fcntl.flock` (Linux/POSIX only) |
| HTTP | stdlib `urllib` (no `requests` in core) |

### Owner's machine

Pop!_OS (Linux), Python 3.12.3, Git 2.43, Docker 29.1.3, uv 0.12.19,
OpenCode 1.18.34, Ollama 0.20.4, Claude Code 2.1.289.

### Models and runtimes

| Role | Runtime | Default in code | Notes |
| --- | --- | --- | --- |
| Master reasoning | Ollama | `qwen2.5-coder:7b` at `http://localhost:11434` | `core/ollama_provider.py` |
| Master reasoning | OpenCode server | provider `opencode`, model `big-pickle`, agent `plan`, at `http://127.0.0.1:4096` | Start with `opencode serve --port 4096` |
| Master reasoning | DeepSeek API | `deepseek-v4-pro` at `https://api.deepseek.com` | Needs the `DEEPSEEK_API_KEY` env var |
| Worker | OpenCode CLI | `opencode run --dir <attempt worktree>` | `core/opencode_backend.py`. Runs through `workspace.run`: inherits the lock, bounded by the attempt deadline. |
| Worker | Ollama tool loop | `qwen3:8b`, 12 turns | `core/ollama_backend.py`. A homemade agent, to be frozen as a test fixture. |

Other local models the owner has used: `qwen2.5:7b`, `qwen3:8b`, `gemma4:e2b`, `gemma4:e4b`, `manfred:latest`.

**Claude Code** is used *temporarily* as the implementation and research engineer.
It must **never** become an architectural dependency.

---

## 6. Repository map

```text
core/
  master.py              Master: the deterministic control API + CLI. The only writer of state.
                         Its import graph is kept tiny on purpose (enforced by tests).
  project_manager.py     Read-only discovery and overview of projects under projects/.
  project_state.py       Validated reads, atomic YAML writes, status vocabularies.
  work_manager.py        Domain rules for creating and updating milestones and tasks;
                         calculate_readiness().

  reasoning.py           The operation contract: SPECS allowlist, OperationKind (STATE/DISPATCH),
                         ImpactLevel, approval policy, Decision / MasterDecision, ReasoningInterface.
  reasoning_engine.py    Builds the Master prompt and JSON schema from SPECS, calls a provider,
                         parses the reply strictly into a decision. Never mutates anything.
  provider.py            The ReasoningProvider protocol: complete(prompt, schema) -> text.
  ollama_provider.py     Reasoning provider: Ollama.
  opencode_provider.py   Reasoning provider: OpenCode server (HTTP).
  deepseek_provider.py   Reasoning provider: DeepSeek API.
  reason_cli.py          CLI: one human-approved proposal (reason -> show -> y/N -> execute).

  execution.py           ExecutionBackend protocol + ExecutionResult (status/reason/artifacts/state_updates).
  execution_runner.py    TaskExecutionRunner: prepare() (checks, no side effects) + invoke() (the side effect).
  opencode_backend.py    Worker: the OpenCode CLI.
  ollama_backend.py      Worker: Ollama tool loop (list/read/write files).
  verification.py        VerificationBackend protocol + VerificationResult (pass/fail/needs_human/unable_to_verify).
  task_orchestrator.py   One attempt: attempt_started -> worker -> attempt_finished -> verification.

  autonomous_loop.py     The loop: inspect -> ask Master -> act (one operation) -> repeat until a stop reason.
  evidence.py            Reads history into neutral evidence: latest attempt, attempt counts,
                         completion gate, context for Master.
  history.py             Event types, HistoryEvent, the HistoryStore protocol, InMemoryHistoryStore,
                         payload bounding.
  sqlite_history.py      SQLiteHistoryStore. The ONLY module that imports sqlite3 (enforced by tests).
  run_lock.py            ProjectLock: non-blocking flock on <project>/.run.lock.

  work_session.py        WorkSession (a durable record of one objective across runs), status lifecycle.
  session_store.py       FileSessionStore: one YAML file per session under sessions/.
  session_runner.py      SessionRunner: lock -> recover dead runs -> run the loop -> save the session.

projects/ai-system/      The managed project (the system's own roadmap): project.yaml,
                         milestones.yaml, tasks.yaml.
tests/                   ~12k lines of pytest. Integration tests are opt-in (marker `integration`).

agents/                  EXPERIMENTS, not part of the control plane:
  bob/                   An early QA-agent persona + identity.md (used by bob.py).
  sergei/, sergei-opencode-test/   A Russian-tutor chat app (an OpenCode worker test project).
bob.py                   An early experiment: a hand-rolled tool loop against Ollama.
docker/bob/Dockerfile    An experiment image with openhands-sdk 1.49.6 (a candidate worker runtime).
```

---

## 7. Architecture

### 7.1 Layers

```text
          ReasoningProvider (Ollama | OpenCode | DeepSeek)   <- returns text only
                     |
          ReasoningEngine        prompt + schema from SPECS; strict JSON parse -> MasterDecision
                     |
          AutonomousLoop         one decision per turn; records history; stop reasons
             |              \
   ReasoningInterface        TaskOrchestrator  (DISPATCH: run_task)
   (approval policy;            |-- TaskExecutionRunner -> ExecutionBackend (worker)
    refuses DISPATCH)           |-- VerificationBackend (verifier)
             |                  `-- HistoryStore (attempt events)
          Master                (the only writer of project state)
             |
   ProjectManager / WorkManager
             |
          ProjectState  ->  projects/<id>/*.yaml

   SessionRunner wraps AutonomousLoop with ProjectLock + recovery + WorkSession persistence.
   HistoryEvidence (evidence.py) reads HistoryStore for the completion gate, attempt limits and Master's context.
```

### 7.2 One turn of the loop

```text
1. Is any task actionable (in_progress, or ready to start)?   no -> stop: no_actionable_work
2. Ask Master (ReasoningEngine.reason), retrying once on an unusable reply -> stop: unusable_reasoning_reply
3. Completion gate: if the operation would set status=completed, check the evidence
4. Append a `decision` event (the recorded intent)
5. decision != act                      -> stop: master_stop
   operation gated by policy            -> stop: approval_required (reason: policy)
   completion gate not satisfied        -> stop: approval_required (reason: e.g. verification_fail)
6a. DISPATCH (run_task): check project, executable (in_progress), attempt limit
    -> TaskOrchestrator: attempt_started -> worker -> attempt_finished -> verification
6b. STATE: ReasoningInterface -> Master writes YAML -> operation_result event(s)
    any failure -> stop: operation_failed
7. Repeat until max_steps (default 20) -> stop: step_limit
A run always ends with exactly one run_stopped or run_error event.
```

### 7.3 Decision contract (what Master returns)

```json
{
  "decision": "act",
  "reason": "t1 has no unmet dependencies, so start it",
  "operation": {"operation": "update_task", "project_id": "p", "task_id": "t1", "status": "in_progress"}
}
```

| Decision | Operation | Meaning |
| --- | --- | --- |
| `act` | exactly one | Do this now (still subject to policy and the completion gate) |
| `wait` | `null` | Nothing to do right now |
| `blocked` | `null` | Cannot proceed |
| `needs_information` | `null` | More information is needed first |
| `request_approval` | exactly one | Ask a human to approve this operation |

Starting work takes **two decisions**: `update_task(status=in_progress)`, then `run_task`.

### 7.4 Operation catalogue (`core/reasoning.py: SPECS`)

| Operation | Kind | Arguments | Impact today |
| --- | --- | --- | --- |
| `inspect_project` | STATE (read) | project_id | ROUTINE |
| `create_milestone` | STATE | project_id, milestone_id, name, [status] | ROUTINE |
| `update_milestone` | STATE | project_id, milestone_id, any of name/status | ROUTINE |
| `create_task` | STATE | project_id, task_id, milestone, title, [status, assigned_to] | ROUTINE |
| `update_task` | STATE | project_id, task_id, any of milestone/title/status/assigned_to | ROUTINE |
| `run_task` | DISPATCH | project_id, task_id | ROUTINE, but refused unless the task is `in_progress` and under the attempt limit |

- An operation that is not in SPECS is gated.
- A new SPECS entry defaults to `CRITICAL`, which means gated.
- `ReasoningInterface.execute` always refuses DISPATCH operations (`dispatch_only`); only the loop dispatches.
- **Known gap (H2):** policy is keyed on the operation *name*. Spec edits, cancels,
  reopening and milestone completion are all autonomous today.

### 7.5 Vocabularies

| Thing | Values |
| --- | --- |
| Task status | `planned`, `in_progress`, `blocked`, `completed`, `cancelled` (no transition rules yet) |
| Milestone status | `planned`, `in_progress`, `completed` |
| Readiness (`calculate_readiness`) | `ready` (planned, deps completed), `blocked`, `waiting` (already started or finished) |
| ExecutionResult.status (worker claim) | `success`, `failed`, `partial`, `blocked`, `cancelled`, `needs_human` |
| Attempt outcome in evidence | the above, or `error` (worker raised), `interrupted` (found by recovery), `unfinished` |
| Verification verdict | `pass`, `fail`, `needs_human`, `unable_to_verify` |
| Loop stop reasons | `no_actionable_work`, `master_stop`, `approval_required`, `step_limit`, `unusable_reasoning_reply`, `operation_failed`, `attempt_limit` |
| Runner stop reasons | `error`, `interrupted` |
| Session status | `created`, `running`, `needs_human`, `stopped`, `completed`, `failed`. `approval_required` and `attempt_limit` map to `needs_human`; everything else maps to `stopped`. `completed` and `failed` are never set automatically. |

### 7.6 Completion gate and attempt limit (current semantics)

- **Completion gate** (`HistoryEvidence.completion_gate`): an autonomous
  `status=completed` applies only if the task's latest attempt, anywhere in history,
  finished and was verified `pass`. Otherwise the loop stops with `approval_required`
  and a reason such as `no_attempt`, `latest_attempt_interrupted` or `verification_fail`.
- **Attempt limit:** 3 attempts per (session, task) by default (`DEFAULT_MAX_ATTEMPTS`).
  - Errored and interrupted attempts count.
  - A 4th attempt is refused with `attempt_limit`, which means a human is needed.
- **Known gaps:** the gate is not bound to the task spec or code revision (H1, verified),
  and a new session resets the budget (H4).

### 7.7 History events (`core/history.py`)

| Event | When | Notes |
| --- | --- | --- |
| `run_started` | start of `AutonomousLoop.run` | request, max_steps |
| `decision` | every turn, before acting | decision, reason, operation, pending_approval, gate_reason |
| `operation_result` | after a STATE operation, or a refused `run_task` | status, reason, message, value |
| `attempt_started` | before the worker runs (committed first) | its event_id becomes the attempt_id |
| `attempt_finished` | after the worker returns or raises | status (or `error`), reason, artifacts, worker identity (opaque) |
| `verification` | after the verifier | verdict, summary, findings, evidence |
| `attempt_interrupted` | recovery found no outcome | no replay |
| `run_stopped` / `run_error` / `run_interrupted` | end of run / exception / recovery | exactly one per run |

Payloads are JSON and capped at 256 KiB. Long strings are cut down to a sha256
fingerprint plus their head. The schema is `SCHEMA_VERSION = 1`, and updates and
deletes are aborted by triggers.

### 7.8 Sessions, lock and recovery

- `SessionRunner.start(project_id, objective, session_id)` creates the session and runs once.
  `resume(session_id)` runs one more bounded pass in any process.
- Every run holds `ProjectLock` (`<project>/.run.lock`). The `core.master` CLI and
  `reason_cli` take the same lock for writes and refuse while it is held.
- If a session says `running` but the lock can be acquired, its process is dead.
  Recovery then:
  1. appends `attempt_interrupted` for each open attempt;
  2. appends `run_interrupted` for each open run;
  3. marks the session `stopped (interrupted)`.

  Nothing is replayed.
- **Known gaps:** the lock does not cover the worker's child process; an orphaned worker
  keeps writing (C2, verified). Recovery only runs when *the same* session is resumed
  (M2). Approvals cannot be granted programmatically (H3).

---

## 8. Data and runtime files

| Path | Contents | In git? | Written by |
| --- | --- | --- | --- |
| `projects/<id>/project.yaml` | id, name, description, status | yes | humans |
| `projects/<id>/milestones.yaml` | `milestones: [{id, name, status}]` | yes | Master |
| `projects/<id>/tasks.yaml` | `tasks: [{id, milestone, title, status, assigned_to?, depends_on?}]` | yes | Master |
| `projects/<id>/.run.lock` | the holder description (informational only) | no (gitignored) | ProjectLock |
| `$XDG_DATA_HOME/master-system/sessions/<session_id>.yaml` | WorkSession records | no (outside the repo) | SessionRunner |
| `$XDG_DATA_HOME/master-system/history.sqlite` | the event history | no (outside the repo) | HistoryStore |

`$XDG_DATA_HOME` defaults to `~/.local/share`. Both paths are configurable
(`core/paths.py: RuntimePaths`). Before Milestone 1 they defaulted to `sessions/` and
`var/history.sqlite` inside the repository; those are not moved automatically. If they
exist on your machine, move them once:

```bash
mkdir -p ~/.local/share/master-system
mv sessions ~/.local/share/master-system/sessions
mv var/history.sqlite* ~/.local/share/master-system/
```

There is no `acceptance` / definition-of-done field on tasks yet; the title is the only
spec (gap C1).

---

## 9. Trust boundaries

Only facts written by the orchestrator, a deterministic verifier or a human may
authorise anything. A model may block an action but never authorise it. Text written
by a worker never reaches a prompt unless it is quoted as data.

| Information | Class | Reaches Master? | Can authorise? |
| --- | --- | --- | --- |
| Task status, dependencies, milestones | Authoritative | yes | yes |
| Task spec / acceptance criteria, once frozen *(planned)* | Human-approved fact | yes | yes: it defines "done" |
| Approval records *(planned)* | Human-approved fact | yes | yes, bound to the operation hash and state revision |
| Attempt facts observed by the orchestrator: times, exit code, base/result SHA, files changed *(partly planned)* | Trusted evidence | yes | yes |
| Deterministic verdict from protected checks in an orchestrator-owned workspace | Trusted evidence | yes | yes (the completion gate) |
| Verifier text: test names, failure excerpts | Trusted source, tainted content | bounded and quoted | no |
| Verdict from a model reviewer *(future)* | Untrusted evidence | labelled | can block, never pass |
| Worker's reported status, summary, `state_updates` | Untrusted evidence | status only, labelled as a claim | no |
| Raw transcript, stdout, tool calls, worker and model identity | Opaque provenance | no | no |
| Master decisions, reasons, proposed tasks and plans | Model suggestion | its own recent decisions | only through policy or approval |
| Lessons / memory *(future)* | Suggestion until a human confirms it | after confirmation | no |

Today the code deviates from this table in two places. The verifier's input comes
from the worker (C1), and the worker's claimed status is shown as a neutral outcome (M3).

---

## 10. How to run things

### Setup and tests

```bash
uv sync --extra dev
uv run python -m pytest -q -m "not integration"      # ~11 s, no network
```

Without uv: `pip install pyyaml pytest`, then `python -m pytest -q -m "not integration"`.

Live tests are opt-in, need real services, and use quota:

```bash
opencode serve --port 4096
OPENCODE_LIVE=1 python -m pytest tests/test_opencode_live.py -v
DEEPSEEK_LIVE_TEST=1 DEEPSEEK_API_KEY=... python -m pytest tests/test_deepseek_provider_live_optin.py -v
```

### Inspect and edit project state (human CLI)

```bash
python -m core.master projects
python -m core.master overview
python -m core.master status ai-system
python -m core.master create-task ai-system foundation-005 --milestone foundation --title "..."
python -m core.master update-task ai-system foundation-003 --status in_progress
```

Mutating commands take the project lock and refuse while an autonomous run holds it.

### Ask Master for one proposal (human approves)

```bash
python -m core.reason_cli "What should happen next?" --project ai-system --provider ollama
python -m core.reason_cli "..." --provider deepseek --dry-run      # show only, never execute
```

The approval prompt defaults to **no**. A `run_task` in a proposal is never executed here.

### Run the autonomous loop

There is **no CLI yet** (gap H5). For reference wiring, see:

- `tests/test_ollama_backend.py::test_the_full_loop_runs_through_ollama_without_touching_the_loop`
  (loop, Ollama worker and a real pytest verifier);
- `tests/test_session_runner.py` and `tests/test_recovery.py` (sessions, lock, recovery).

---

## 11. Known gaps

These come from the architecture review of 2026-10-05, which covered commit `18f8239`.
IDs match the review document. **Verified** means the behaviour was reproduced with
a script against the code.

| ID | Severity | Gap | Planned fix |
| --- | --- | --- | --- |
| C1 | CRITICAL | The verifier is not independent of the worker. It finds the workspace via worker `artifacts["workdir"]` and runs tests the worker can edit. Tasks have no acceptance criteria. | The orchestrator owns the workspace path. Add a frozen `acceptance` field (commands and protected paths). Fail any diff that touches protected paths. |
| C2 | CRITICAL | Worker side effects are not contained. **Verified:** after `SIGKILL` of the loop, the lock frees at once while a `subprocess.run` worker keeps writing. State, sessions and history live inside the managed repo. | A git worktree per attempt (base/result SHA); a gated INTEGRATE operation; subprocess workers inherit the lock fd (`pass_fds`, **verified** to keep the lock held); move runtime state outside workspaces. |
| H1 | HIGH | Completion evidence is not bound to what it verified. **Verified:** reopen and re-title a task, and an old `pass` still authorises completion. | Record `spec_hash` and `base_sha` in `attempt_started` and `result_sha` in `attempt_finished`; the gate requires a matching spec_hash. |
| H2 | HIGH | Policy is keyed on operation names. The model can autonomously rewrite titles, cancel, reopen, or complete milestones, and any status can go to any status. | A task transition table; `requires_approval(operation, state)` by transition and field. |
| H3 | HIGH | Approvals can be requested but never granted (`pending_approval` lacks the arguments; there is no approve command or event). | `approval_requested`/`granted`/`denied` events bound to the operation hash and state revision; CLI approve/reject; resume applies a granted operation without a model call. |
| H4 | HIGH | The attempt budget resets every session, and infrastructure failures spend it. | Count per task since the last human action; classify failures; add time and cost budgets. |
| H5 | HIGH | Nothing runs the loop outside tests. | `core/run_cli.py` (start/resume/status/approve), a `CommandVerifier`, and an end-to-end run on a toy repo. |
| H6 | HIGH | The project is at the reinvention line for durable execution. | Keep the existing code; spike DBOS for timeouts, cancellation, waits and scheduling after C2. |
| M1 | MEDIUM | No attempt deadline or cancellation (the OpenCode timeout defaults to `None`). | A required deadline; kill the process group; record a `timed_out` outcome; a `cancel` command. |
| M2 | MEDIUM | Recovery is session-scoped and observes nothing about the interrupted attempt. | Project-wide recovery on every lock acquisition; record the attempt's diff. |
| M3 | MEDIUM | Master sees too little of what changed, and the worker's claim is presented as a neutral fact. | Add files changed, diff stats, exit code and failing test names; label the worker's status as a claim. |
| M4 | MEDIUM | YAML state has no revision, and human CLI edits leave no history. | A `revision` counter; record every Master write, with `actor`. |
| M5 | MEDIUM | WorkSession duplicates history (counters stored twice). | Keep a session identity row in the history DB; derive the rest from events. |
| M6 | MEDIUM | The Ollama backend is a homemade coding agent. | Freeze it as a fixture; build the next worker via ACP or the OpenHands SDK. |
| L1 | LOW | Events lack `actor` and `caused_by`. | Add both columns now. |
| L2 | LOW | No stuck or oscillation detection. | Repetition and ping-pong rules (OpenHands-style). |
| L3 | LOW | A model call is spent on every forced move. | Deterministic rules for single-legal-move steps, recorded as actor=system. |
| L4 | LOW | Legacy parser path, `proposal._decision`, experiments in the repo root, a stale docstring in `work_session.py`. | Delete or move; keep docstrings short; write ADRs. |
| L5 | LOW | SQLite has no `busy_timeout`, and the DB path is inside the repo. | Set busy_timeout; move the path (with C2). |

---

## 12. Roadmap

### Owner-approved

- The five-milestone roadmap below was **approved by the owner on 2026-10-05**.
- **Current milestone: 1, "Contain and verify".** Finishing it also closes the earlier
  "Trustworthy loop: explicit execution, evidence history, safe resume" milestone.
- The loop must be trustworthy before any memory architecture or self-improvement work.
- Memory comes after the loop is trustworthy. Self-improvement comes last.

### Approved roadmap (2026-10-05)

| # | Milestone | Exit gate |
| --- | --- | --- |
| 1 | Contain and verify (finishes the current milestone) | `kill -9` is safe; a forged pass fails |
| 2 | Run it for real | 5 tasks merged on a toy repo, each traced |
| 3 | Approvals that round-trip | An approval from another process applies exactly that operation |
| 4 | Durable runtime: DBOS spike, then adopt or walk away | Homemade code shrinks, or walk away and build only timeouts |
| 5 | Plans and focused context | A plan from a one-paragraph objective |

**1. Contain and verify** (current)

- Objective:
  - a worktree per attempt;
  - the lock held by the worker process;
  - frozen acceptance criteria, checked by a verifier the orchestrator owns;
  - evidence bound to the spec hash and SHAs;
  - per-attempt deadlines;
  - runtime state moved outside every workspace.
- Not yet: containers, parallel attempts, auto-merge, DBOS.
- Acceptance:
  - [x] `kill -9` during a worker: recovery is refused until the worker exits, the worktree is kept, and its diff stats are recorded.
    (`tests/test_recovery.py::test_kill9_of_the_loop_keeps_the_lock_until_the_worker_exits`)
  - [x] A worker that edits a protected test gets `fail`.
    (`tests/test_acceptance_verifier.py`)
  - [x] Re-titling a task or changing its acceptance invalidates an earlier pass.
    (`tests/test_evidence.py`: `spec_changed`)
    ("Reopening invalidates a pass" moved to Milestone 3, decision P7.)
  - [x] An attempt past its deadline is killed and recorded as `timed_out`.
    (`tests/test_attempt_workspace.py`, `tests/test_worker_process.py`)
  - [x] No worker can reach project state, sessions or history.
    (`tests/test_attempt_workspace.py`, `tests/test_workspace.py`, `tests/test_paths.py`)

**2. Run it for real**

- Objective: `run_cli` (start/resume/status), a `CommandVerifier`, one real worker
  (the OpenCode CLI or an ACP client), and a throwaway repo with 5 tasks, run overnight.
- Not yet: a dashboard, multiple projects, self-hosting.
- Acceptance:
  - [ ] 5 tasks completed and integrated, with every human touch recorded in history.
  - [ ] Every completion traces to a decision, an attempt and a result SHA.
  - [ ] A per-run report of steps, attempts, wall-clock time and tokens, generated from history alone.

**3. Approvals that round-trip**

- Objective:
  - approval events bound to the operation hash and state revision;
  - CLI approve/reject;
  - policy by transition (H2);
  - an attempt budget counted since the last human action (H4);
  - human edits recorded (M4);
  - a stuck detector (L2).
- Not yet: a web UI, roles, notifications beyond one push or email hook.
- Acceptance:
  - [ ] An operation approved from another process resumes and applies exactly that operation, with no model call.
  - [ ] A stale approval is refused.
  - [ ] Spec edits, cancel, reopen and milestone completion wait for a human.
  - [ ] Reopening a task invalidates an earlier pass (moved here from Milestone 1, P7).
  - [ ] A new session doesn't reset the budget.

**4. Durable runtime (DBOS spike)**

- Objective: `run_session` as a DBOS workflow and `run_attempt` as a step, with timeouts,
  cancellation, infrastructure retries with backoff, scheduled runs, and approval waits
  via `recv`.
- Not yet: Postgres, multiple machines, Temporal.
- Acceptance:
  - [ ] The crash scenario tests pass unchanged.
  - [ ] Cancel works mid-attempt.
  - [ ] An approval wait survives a restart.
  - [ ] Our history is still the only evidence Master sees.
  - [ ] More homemade code is deleted than adapter code is added. Otherwise, walk away.

**5. Plans and focused context**

- Objective:
  - objective → proposed task graph (with acceptance criteria and dependencies) → human-approved plan;
  - Master's context gains per-task digests and human-confirmed project notes;
  - prompt and policy versions recorded on every decision.
- Not yet: automatic memory promotion, a vector DB, self-modification.
- Acceptance:
  - [ ] A plan from a one-paragraph objective that the owner can edit, approve and run.
  - [ ] The prompt stays under budget (for example 8k tokens) on a 50-task project.

### Later: memory and self-improvement (intent, not yet designed in detail)

**Memory hierarchy:**

```text
Current WorkSession -> Episodic History -> Semantic / Long-term Memory -> Procedural Memory
```

- History is not memory. Memory is derived from validated evidence.
- Worker hallucinations must never become permanent memory.
- Promotion into long-term memory needs validation or confirmation; automatic promotion is
  considered dangerous.
- Retrieval should supply narrowly relevant context, not dump the whole history. Token
  efficiency comes from selecting context, not from making Master less capable.
- SQLite is the likely V1 substrate for memory too. **Do not implement a MemoryStore yet.**

**Self-improvement:**

- The control-plane repo becomes one more project with stricter policy: every
  integration into it is human-gated, and its acceptance is the test suite plus a replay
  suite built from recorded history.
- Interfaces to keep stable so components can be replaced:
  - `ExecutionBackend`
  - `VerificationBackend`
  - `ReasoningProvider.complete`
  - the `HistoryStore` event schema
  - the operation catalogue and policy
  - the acceptance spec format
- Plan to add a `workspace` parameter to the first two before more backends exist.

---

## 13. Decision log

Add a row whenever an architectural decision is made or reversed.

| Decision | Why | Status |
| --- | --- | --- |
| Separate project state (facts) from operations (side effects); `in_progress` no longer implies execution | Auditability, explicit side effects, safe recovery, no accidental duplicate execution | Approved, implemented |
| Two operation kinds: STATE and DISPATCH. `run_task(project_id, task_id)` takes no worker, model, workspace or prompt | Execution details are orchestration concerns; Master stays model-independent | Approved, implemented |
| Master returns one decision with at most one operation | Smallest useful step; clear audit | Approved, implemented |
| Worker `state_updates` are untrusted proposals and never applied | Single writer; worker output is not authoritative | Approved, implemented |
| Worker output is opaque provenance, kept out of Master's context ("record provenance, don't branch on it") | Model independence; prompt-injection resistance | Approved, implemented (the review suggests adding orchestrator-observed facts, M3) |
| YAML stays authoritative; SQLite holds append-only history; no faked atomicity | Simplicity; human-editable state; honest failure semantics | Approved, implemented |
| SQLite with WAL + `synchronous=FULL`; only `sqlite_history.py` imports sqlite3 | Durability before side effects; replaceable store | Approved, implemented |
| No automatic replay of uncertain attempts | The first worker may have changed the world | Approved, implemented (the review: isolation is the real fix, C2) |
| One autonomous run per project via a non-blocking `flock`; human mutations use the same lock; PIDs are informational only | Simple, kernel-managed, no leases needed on one machine | Approved, implemented (needs worker fd inheritance, C2) |
| Completion gate: latest attempt finished + `pass` + no newer attempt; otherwise a human decides | QA pass ≠ completion | Approved, implemented (needs spec binding, H1) |
| Attempt limit of 3 per (session, task) | Prevents hammering a broken task | Approved, implemented. **Approved to change in Milestone 3 (H4)** |
| WorkSession is operational continuity, not memory | Keep concerns separate | Approved, implemented (the review proposes shrinking it, M5) |
| Claude Code is a temporary implementation engineer, never a dependency | Model and agent independence | Approved |
| Prefer mature coding agents as workers over building one | Focus on the control plane | Approved (the Ollama tool loop contradicts this, M6) |
| Don't adopt Temporal (cluster) or LangGraph (graph DSL, LangChain coupling) for now | Infrastructure weight; unwanted coupling | Approved |
| DBOS is the escape hatch if homemade durability grows too complex | In-process library on SQLite | Approved as an option. The review recommends a spike in milestone 4. |
| Worktree per attempt + gated INTEGRATE | Makes retries safe and evidence attributable | **Approved (2026-10-05)**; Milestone 1 |
| Frozen acceptance criteria per task (shell commands + protected paths), verified by the orchestrator | An independent definition of done | **Approved (2026-10-05)**; Milestone 1 |
| Approval policy by transition and field | Risk lives in the arguments | **Approved (2026-10-05)**; Milestone 3 |
| Stop self-hosting (`projects/ai-system`) until approvals work (Milestone 3); dogfood on a toy repo | Avoid self-modification by the back door | **Approved (2026-10-05)** |
| Five-milestone roadmap (§12) | The review's sequencing: contain, run, approve, durable runtime, plans | **Approved (2026-10-05)** |
| DBOS spike in Milestone 4 | Test the escape hatch before building more homemade durability | **Approved (2026-10-05)** |
| `uv.lock` is committed | Reproducible dev environments | **Approved (2026-10-05)** |
| M1 plan P1: state dir `$XDG_DATA_HOME/master-system/` (history, sessions); worktrees in the sibling `master-system-worktrees/` | Worktrees inside the state dir would be refused by the isolation rule | **Approved (2026-10-05)** |
| M1 plan P2: workers run under coreutils `timeout --signal=TERM --kill-after=30s`, so the deadline holds even if the loop dies | An orphaned worker would otherwise hold the inherited lock forever | **Approved (2026-10-05)** |
| M1 plan P3: the orchestrator generates the attempt id, so `attempt_started` (with base SHA and worktree) is written before `git worktree add` | Intent before side effect | **Approved (2026-10-05)** |
| M1 plan P4: integration is fast-forward only, idempotent by SHA; a moved base is refused | Merges with conflicts need judgement | **Approved (2026-10-05)** |
| M1 plan P5: recovery runs on lock acquisition by history users (SessionRunner, the loop, integrate), not by the `core.master` / `reason_cli` state CLIs | `master.py` may not import history; human edits don't depend on closed records | **Approved (2026-10-05)** |
| M1 plan P6: the completion gate compares spec_hash with the spec *after* the operation | Retitle-and-complete in one operation must not pass | **Approved (2026-10-05)** |
| M1 plan P7: "reopen invalidates a pass" moved to Milestone 3 | spec_hash cannot detect a reopen; needs transition policy | **Approved (2026-10-05)** |
| M1 plan P8: verification has its own deadline (30 min total, 10 min per command) | Acceptance commands need a bound too | **Approved (2026-10-05)** |
| Cancel deferred | Killing a recorded PID risks killing an unrelated process after PID reuse | **Approved (2026-10-05)** |
| History DB is copied to `history.sqlite.bak-v1-<timestamp>` before the v1→v2 migration; a failed copy refuses the migration | A schema rebuild must be reversible | **Approved (2026-10-05)** |

---

## 14. Ecosystem stance

| System | What we borrow | Decision |
| --- | --- | --- |
| DBOS (Python, MIT, a library, SQLite by default) | Durable workflows and steps, retries and backoff, queues, cancel/resume/fork, `send`/`recv`, durable sleep, scheduling | The most likely adoption: spike in milestone 4. Its steps re-run when interrupted, so isolation (C2) comes first. |
| Temporal | Activities, retry policies, timeouts, heartbeats, deterministic workflows | Borrow the ideas; don't run a cluster |
| LangGraph | Checkpoints, interrupts, explicit side-effect boundaries | Borrow the ideas; no graph DSL, no LangChain coupling |
| OpenHands (SDK) | Event-oriented state, workspaces, confirmation policies, risk analyser, stuck detector | A candidate worker runtime (`docker/bob` already pins `openhands-sdk`); copy the stuck-detector rules |
| OpenAI Agents SDK | `needs_approval` as a function of the arguments; serialisable `RunState` for paused runs | Borrow the approval pattern; no coupling to the OpenAI ecosystem |
| PydanticAI | Typed structured outputs; durable-execution integrations | Reference; maybe for providers if Master gets tools (V2) |
| Agent Client Protocol (ACP) | One protocol for OpenCode, Claude Agent, Codex CLI, Gemini CLI, Qwen Code, OpenHands, Goose | The preferred way to build the next worker adapter |
| Letta / Mem0 | Retrieval, persistent agent context | Later, as retrieval only, never as the source of truth; no self-written memory |

---

## 15. Rules for working in this repo

These apply to AI agents (Claude Code, OpenCode, Codex, …) and humans alike.

### Before you start

- Read this README fully. Then read `git log --oneline -20` and the docstring of every module you will touch.
- Run the offline test suite. It must be green before you change anything.
- Confirm the milestone and scope with the owner. **Don't start a new milestone or make an
  architectural change without the owner's approval.**
- Work on a branch, created from the latest meaningful checkpoint. Don't push experiments
  to `main`. (`claude-week` is the current development branch.)

### While working

- **Boundaries enforced by tests** (don't break them; don't weaken the tests):
  - `core/master.py` must not import YAML, ProjectState, execution, network or sqlite modules;
  - only `core/sqlite_history.py` imports `sqlite3`;
  - no orchestration module imports a concrete adapter;
  - session code names no provider.
- Don't change `core/master.py`, `core/work_manager.py`, `core/project_state.py` or the
  provider and backend modules gratuitously, only when a requirement genuinely needs it.
- **Never** let model or worker output mutate project state except through Master and policy.
- **Never** add provider, model or worker names above the adapter modules.
- **Never** auto-replay an attempt whose outcome is unknown.
- **Never** commit runtime state (`sessions/`, `var/`, `.run.lock`, `.venv/`) or secrets
  (`.env`, API keys). `uv.lock` **is** committed (approved 2026-10-05).
- **Never** mark `projects/ai-system` tasks completed on the owner's behalf.
- Add a dependency only with a decision-log entry explaining why.
- Every behaviour change comes with tests. Prefer scenario tests (crash, forgery,
  approval round-trip) over tests that pin internals.
- Keep docstrings short and factual. Put design rationale in the decision log, or in ADRs
  under `docs/adr/` once that folder exists.

### When you finish a piece of work

- Run the full offline suite.
- Update this README: the [§2 Status snapshot](#2-status-snapshot), [§11 Known gaps](#11-known-gaps)
  (remove what you fixed) and [§13 Decision log](#13-decision-log).
- Make small commits with descriptive messages, then push the branch.
- Tell the owner what changed, what is still open, and anything you decided without them.

---

## 16. Resuming after a break

1. `git fetch --all && git branch -a && git log --oneline -20`. Find the latest branch and checkpoint.
2. Read [§2 Status snapshot](#2-status-snapshot) and [§12 Roadmap](#12-roadmap). What is the current milestone, and what is next?
3. `uv sync --extra dev && uv run python -m pytest -q -m "not integration"`.
4. `python -m core.master status ai-system` (and `overview`) to see the managed project's state.
5. If autonomous runs have happened on this machine, look at `sessions/*.yaml` and
   `var/history.sqlite` (for example `sqlite3 var/history.sqlite "select seq,type,task_id,created_at from events order by seq desc limit 30"`).
   A session left in `running` is recovered automatically by the next `resume`.
6. Check [§17 Open questions](#17-open-questions-for-the-owner). If any are still open, ask the owner before building on them.
7. Continue with the first unchecked acceptance criterion of the current milestone.

---

## 17. Open questions for the owner

- [x] Accept, change or reject the proposed roadmap in [§12](#12-roadmap)? **Accepted (2026-10-05).**
- [x] Is the `ai-system` project's worker meant to run inside this same repo, or in a separate checkout?
  **Resolved: stop self-hosting until Milestone 3.** No worker runs against this repository until then.
- [x] What format should acceptance criteria take? **Shell commands plus protected paths (2026-10-05).**
- [x] Attempt budget: per task since the last human action, or keep per session?
  **Per task since the last human action (2026-10-05)**, implemented in Milestone 3.
- [ ] Which worker comes next: the OpenCode CLI as it is, an ACP client, or the OpenHands SDK?
- [ ] Should `claude-week` be merged into `main` now, or after milestone 1?
- [x] Is a DBOS spike in milestone 4 acceptable? **Yes (2026-10-05).**

---

## 18. Glossary

| Term | Meaning |
| --- | --- |
| **Master** | The strategic decision maker. In code, both the deterministic control API (`core/master.py`, the only writer of state) and the reasoning role played by a model through `ReasoningEngine`. |
| **Decision** | Master's answer for one turn: `act`, `wait`, `blocked`, `needs_information` or `request_approval`. |
| **Operation** | A named, allowlisted request (`SPECS`). Its kind is STATE (changes project state) or DISPATCH (causes an external side effect). |
| **Policy / approval** | `requires_approval()`: whether an operation may run without a human. Today it is keyed on the operation name and impact level. |
| **Completion gate** | Evidence check before an autonomous `completed` (see §7.6). |
| **Attempt** | One execution of a task by a worker, from `attempt_started` to its outcome and verification. |
| **Worker / ExecutionBackend** | Whatever performs the work (OpenCode, Ollama tool loop, future agents). Untrusted. |
| **Verifier / VerificationBackend** | Assesses an attempt and returns a verdict. It must not mutate state. |
| **Run** | One `AutonomousLoop.run` call (`run_id`). |
| **Session** | A WorkSession: one objective pursued across one or more runs (`session_id`). |
| **History** | The append-only event log: evidence, not memory. |
| **Evidence** | Neutral facts read from history for decisions (attempt outcomes, verdicts, counts). |
| **Readiness** | Whether a task can start: `ready` / `blocked` / `waiting` (`calculate_readiness`). |
| **Recovery** | Closing runs and attempts left open by a dead process. It never replays. |
| **INTEGRATE** *(proposed)* | A gated operation that merges a verified attempt's result SHA into the real branch. |

---

## 19. References

- **Architecture review (2026-10-05)**, private to the owner:
  https://claude.ai/code/artifact/b6c807ab-eb85-4ed3-9aef-854147be49ff
  The findings are summarised in [§11](#11-known-gaps) so this file stays self-contained.
- DBOS Transact Python: https://github.com/dbos-inc/dbos-transact-py ·
  steps: https://docs.dbos.dev/python/tutorials/step-tutorial ·
  workflow management: https://docs.dbos.dev/python/tutorials/workflow-management
- Temporal activities: https://docs.temporal.io/activities
- LangGraph interrupts: https://docs.langchain.com/oss/python/langgraph/interrupts
- OpenAI Agents SDK human-in-the-loop: https://github.com/openai/openai-agents-python/blob/main/docs/human_in_the_loop.md
- OpenHands SDK security: https://docs.openhands.dev/sdk/guides/security ·
  stuck detector: https://docs.openhands.dev/sdk/guides/agent-stuck-detector
- PydanticAI durable execution: https://pydantic.dev/docs/ai/capabilities/durable_execution/overview/
- Agent Client Protocol agents: https://agentclientprotocol.com/get-started/agents
