# Master System: a model-independent AI Work System

> **Read this first.** This is the onboarding document for anyone, human or AI agent,
> who picks up this project after a break, a lost conversation, or an agent switch.
> It covers the goal, the architecture, the current state, the plan, and the rules for
> working here. If you change something this file describes, update the file in the same commit.

| | |
| --- | --- |
| Last updated | 2026-10-05 |
| Written against | branch `m1-contain-verify`, end of Milestone 1 |
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
| Current milestone | **Milestone 2, "Run it for real"**, on branch `m2-run-for-real`: implemented and tested offline (an end-to-end dry run through `run_cli` on a toy repo). **Next: the supervised one-task run on Match Legends with the owner, then the overnight run.** The §12 criteria are ticked only by real runs. Milestone 1 is done and merged to `main` (tag `m1-contain-verify`). |
| Tests | 1219 passed, 1 skipped, 5 integration tests deselected (`uv run python -m pytest -q -m "not integration"`, about 45 s; scenario tests start real processes and git worktrees) |
| Can the autonomous loop be run from a CLI? | **Yes:** `python -m core.run_cli start/resume/status/report/integrate` (see [§10](#10-how-to-run-things)). Not yet run against a real model and worker. |
| Production verifier in `core/`? | **Yes:** `AcceptanceVerifier` (`core/acceptance_verifier.py`) checks a task's human-written `acceptance` in the attempt's worktree. |
| Workers (execution backends) | `OpenCodeCliBackend` (runs `opencode run` through the attempt workspace) and `OllamaExecutionBackend` (a homemade tool loop, to be frozen). Both work only in the worktree the orchestrator gives them. |
| Reasoning providers (for Master) | Ollama, OpenCode server, DeepSeek API |
| Branches | `main` = Milestone 1 (fast-forwarded to `m1-contain-verify`, tag `m1-contain-verify`), which includes the `claude-week` work. Milestone 2 is planned on `m2-run-for-real`. |
| Managed project | `projects/ai-system` is the system's own project. Its tasks are stale: `foundation-001` and `-002` are `in_progress`, although that work was done by hand. **Self-hosting is stopped until Milestone 3** (approved 2026-10-05): no worker runs against this repository. |

### Next actions

1. Supervised one-task run on Match Legends with the owner: dedicated clone, worker home
   and credential, `run_cli start`, `integrate`, `report`. Fix what breaks.
2. The owner starts the overnight run (5 tasks) and reads the report in the morning.
3. Tick the §12 Milestone 2 criteria that the real runs satisfy.

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

  execution.py           ExecutionBackend protocol (execute(task, context, *, workspace)) + ExecutionResult.
  execution_runner.py    TaskExecutionRunner: prepare() (checks, no side effects) + invoke() (the side effect).
  opencode_backend.py    Worker: the OpenCode CLI, launched through workspace.run.
  ollama_backend.py      Worker: Ollama tool loop (list/read/write files), deadline-checked per turn.
  verification.py        VerificationBackend protocol + VerificationResult (pass/fail/needs_human/unable_to_verify).
  acceptance_verifier.py AcceptanceVerifier: protected paths + acceptance commands, in the attempt worktree.
  task_orchestrator.py   One attempt: attempt_started -> worktree -> worker -> commit -> attempt_finished
                         -> verification. Owns the workspace, deadline and recorded facts.
  workspace.py           GitWorktrees (one worktree per attempt, snapshot commits, observation,
                         fast-forward integration), AttemptWorkspace, the isolation rule.
  worker_process.py      run_process: the one way to start an attempt process (timeout wrapper,
                         new process group, inherited lock fd).
  paths.py               RuntimePaths: state dir and worktrees root, outside the repo (XDG).
  recovery.py            recover_project: close out a project's dead runs/attempts/sessions. No replay.
  attempts.py            Human CLI: `integrate` a verified attempt (fast-forward; --rebase replays
                         onto a moved base and re-verifies).
  run_cli.py             The composition root for real runs: start / resume / status / report /
                         integrate / task describe|set-acceptance (human edits, recorded).
  run_config.py          ~/.config/master-system/config.toml (tomllib): Master provider, worker,
                         run limits, price table. No secrets.
  report.py              The per-session report, from history alone.
  human_edits.py         Human spec edits through run_cli, recorded as human_action events.
  worker_env.py          The allowlisted environment of worker/verification processes (Node 22
                         PATH from nvm, a dedicated worker home, no secrets).
  usage.py               One neutral token/cost usage shape (accounting only).

  autonomous_loop.py     The loop: inspect -> ask Master -> act (one operation) -> repeat until a stop reason.
  evidence.py            Reads history into neutral evidence: latest attempt, attempt counts,
                         completion gate, context for Master.
  history.py             Event types, HistoryEvent, the HistoryStore protocol, InMemoryHistoryStore,
                         payload bounding.
  sqlite_history.py      SQLiteHistoryStore. The ONLY module that imports sqlite3 (enforced by tests).
  run_lock.py            ProjectLock: non-blocking flock on <project>/.run.lock; fileno() for workers.

  work_session.py        WorkSession (a durable record of one objective across runs), status lifecycle.
  session_store.py       FileSessionStore: one YAML file per session in the state dir.
  session_runner.py      SessionRunner: lock -> recover the project -> run the loop -> save the session.

projects/ai-system/      The managed project (the system's own roadmap): project.yaml,
                         milestones.yaml, tasks.yaml.
tests/                   pytest. conftest.py isolates every test from the owner's XDG dir and git
                         config. Integration tests are opt-in (marker `integration`).
docs/plans/              Milestone plans (m1-contain-verify.md).

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
   (approval policy;            |-- GitWorktrees: worktree per attempt, snapshot commit
    refuses DISPATCH            |-- TaskExecutionRunner -> ExecutionBackend (worker, in AttemptWorkspace)
    and INTEGRATE)              |-- VerificationBackend (e.g. AcceptanceVerifier, same worktree)
             |                  `-- HistoryStore (attempt events)
          Master                (the only writer of project state)
             |
   ProjectManager / WorkManager
             |
          ProjectState  ->  projects/<id>/*.yaml

   SessionRunner wraps AutonomousLoop with ProjectLock + project-wide recovery + WorkSession persistence.
   Worker processes start only through AttemptWorkspace.run -> worker_process.run_process.
   Humans integrate verified attempts with core.attempts (never the loop).
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
6a. DISPATCH (run_task): check project, executable (in_progress), repository (no_repository,
    workspace_refused, repository_unusable), attempt limit
    -> TaskOrchestrator: attempt_started -> git worktree add -> worker (deadline, lock fd)
       -> snapshot commit -> attempt_finished -> verification (finished attempts only)
6c. INTEGRATE (integrate_attempt): always stops for approval; only a human integrates
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
| `run_task` | DISPATCH | project_id, task_id | ROUTINE, but refused unless the task is `in_progress`, under the attempt limit, and its project has a usable, isolated repository |
| `integrate_attempt` | INTEGRATE | project_id, task_id, attempt_id | CRITICAL: always gated; `ReasoningInterface` refuses it (`human_only`) even when approved |

- An operation that is not in SPECS is gated.
- A new SPECS entry defaults to `CRITICAL`, which means gated.
- `ReasoningInterface.execute` always refuses DISPATCH operations (`dispatch_only`); only the loop dispatches.
- Task `acceptance` is not in the vocabulary: only humans set it (`core.master set-acceptance` or YAML).
- **Known gap (H2):** policy is keyed on the operation *name*. Spec edits, cancels,
  reopening and milestone completion are all autonomous today.

### 7.5 Vocabularies

| Thing | Values |
| --- | --- |
| Task status | `planned`, `in_progress`, `blocked`, `completed`, `cancelled` (no transition rules yet) |
| Milestone status | `planned`, `in_progress`, `completed` |
| Readiness (`calculate_readiness`) | `ready` (planned, deps completed), `blocked`, `waiting` (already started or finished) |
| ExecutionResult.status (worker claim) | `success`, `failed`, `partial`, `blocked`, `cancelled`, `needs_human`; shown to Master only as `worker_reported_status` |
| Attempt outcome (orchestrator) | `finished`, `timed_out`, `error` (worker or workspace raised), `interrupted` (found by recovery), `unfinished` |
| `run_task` refusals | `not_executable`, `wrong_project`, `no_repository`, `workspace_refused`, `repository_unusable`, `attempt_limit` |
| Completion gate reasons | `no_attempt`, `latest_attempt_interrupted` / `_unfinished` / `_errored` / `_timed_out`, `spec_changed`, `not_verified`, `verification_errored`, `verification_<verdict>` |
| Verification verdict | `pass`, `fail`, `needs_human`, `unable_to_verify` |
| Loop stop reasons | `no_actionable_work`, `master_stop`, `approval_required`, `step_limit`, `unusable_reasoning_reply`, `operation_failed`, `attempt_limit` |
| Runner stop reasons | `error`, `interrupted` |
| Session status | `created`, `running`, `needs_human`, `stopped`, `completed`, `failed`. `approval_required` and `attempt_limit` map to `needs_human`; everything else maps to `stopped`. `completed` and `failed` are never set automatically. |

### 7.6 Completion gate and attempt limit (current semantics)

- **Completion gate** (`HistoryEvidence.completion_gate`): an autonomous
  `status=completed` applies only if the task's latest attempt, anywhere in history,
  - finished (not timed out, errored or interrupted),
  - was run against the task's spec as it will be **after** the operation
    (`spec_hash` = sha256 of `{title, acceptance}`), and
  - was verified `pass`.

  Otherwise the loop stops with `approval_required` and the gate reason (§7.5).
- **Attempt limit:** 3 attempts per (session, task) by default (`DEFAULT_MAX_ATTEMPTS`).
  - Errored, timed-out and interrupted attempts count.
  - A 4th attempt is refused with `attempt_limit`, which means a human is needed.
- **Known gaps:** a new session resets the budget (H4, Milestone 3); reopening a task
  does not invalidate an earlier pass (moved to Milestone 3, P7).

### 7.7 History events (`core/history.py`)

| Event | When | Notes |
| --- | --- | --- |
| `run_started` | start of `AutonomousLoop.run` | request, max_steps |
| `decision` | every turn, before acting | decision, reason, operation, pending_approval, gate_reason |
| `operation_result` | after a STATE operation, or a refused `run_task` | status, reason, message, value |
| `attempt_started` | before the worktree exists (committed first) | the orchestrator's attempt_id; repository, base_branch, base_sha, worktree, branch, spec_hash, timeout_s, deadline_at |
| `attempt_process` | right after a worker or verification process starts | phase, pid, pgid, deadline_at |
| `attempt_finished` | after the worker returns, raises or times out, and the worktree is committed | outcome, worker_reported_status, processes, result_sha, files_changed, diffstat; reason/artifacts/worker as opaque provenance |
| `verification` | after the verifier (finished attempts only) | verdict, summary, findings, evidence (orchestrator facts), deadline_at |
| `attempt_interrupted` | recovery found no outcome | worktree_present, git_status, diffstat, untracked; no replay |
| `integration` | a human integrated an attempt | base_branch, previous_sha, result_sha, method, actor |
| `run_stopped` / `run_error` / `run_interrupted` | end of run / exception / recovery | exactly one per run |

Payloads are JSON and capped at 256 KiB. Long strings are cut down to a sha256
fingerprint plus their head. The schema is `SCHEMA_VERSION = 2`; a v1 database is
copied to `history.sqlite.bak-v1-<timestamp>` and then migrated in one transaction.
Updates and deletes are aborted by triggers. SQLite waits up to 5 s on a busy database.

### 7.8 Sessions, lock, workspaces and recovery

- `SessionRunner.start(project_id, objective, session_id)` creates the session and runs once.
  `resume(session_id)` runs one more bounded pass in any process.
- Every autonomous run holds `ProjectLock` (`<project>/.run.lock`); a loop without a
  lock takes it itself. Every worker and verification process inherits the lock's fd,
  so **a worker that outlives its loop keeps the project locked** until it exits.
  The `core.master` CLI, `reason_cli` and `core.attempts integrate` take the same lock
  for writes and refuse while it is held.
- **Workspaces.** Each attempt runs in `git worktree add <worktrees_root>/<project>/<attempt_id>
  -b attempt/<attempt_id> <base_sha>` in the project's `repository`. The orchestrator
  commits the result itself (fixed identity, no hooks, unsigned). Worktrees and branches
  are kept. A repository, worktree or worktrees root may not be, contain or sit inside
  the projects root, the state dir or the control plane's own source directory.
- **Deadlines.** Worker processes run under `timeout --signal=TERM --kill-after=30s`
  in their own process group, so the deadline holds even if the loop dies. The
  in-process Ollama worker checks the deadline between turns. Past the deadline the
  attempt is `timed_out` and is not verified.
- **Recovery** (`core/recovery.py`) runs whenever a history user acquires the lock
  (SessionRunner, a self-locking loop, integrate). For the whole project it:
  1. appends `attempt_interrupted` for each open attempt, with its worktree's git status
     and diff stats;
  2. appends `run_interrupted` for each open run;
  3. marks every `running` session of the project `stopped (interrupted)`.

  Nothing is replayed.
- **Known gaps:** approvals cannot be granted programmatically (H3); there is no cancel
  command (deferred to Milestone 4).

---

## 8. Data and runtime files

| Path | Contents | In git? | Written by |
| --- | --- | --- | --- |
| `projects/<id>/project.yaml` | id, name, description, status, `repository` (absolute path of the git repo the project manages), `base_branch` | yes | humans |
| `projects/<id>/milestones.yaml` | `milestones: [{id, name, status}]` | yes | Master |
| `projects/<id>/tasks.yaml` | `tasks: [{id, milestone, title, status, assigned_to?, depends_on?, acceptance?}]`; `acceptance: {commands: [...], protected_paths: [globs]}` is set only by humans | yes | Master (acceptance: humans via `set-acceptance`) |
| `projects/<id>/.run.lock` | the holder description (informational only) | no (gitignored) | ProjectLock |
| `$XDG_DATA_HOME/master-system/sessions/<session_id>.yaml` | WorkSession records | no (outside the repo) | SessionRunner |
| `$XDG_DATA_HOME/master-system/history.sqlite` | the event history | no (outside the repo) | HistoryStore |
| `$XDG_DATA_HOME/master-system-worktrees/<project>/<attempt_id>/` | one git worktree per attempt (branch `attempt/<attempt_id>`), kept | no (outside the repo) | TaskOrchestrator |

`$XDG_DATA_HOME` defaults to `~/.local/share`. Both paths are configurable
(`core/paths.py: RuntimePaths`). Before Milestone 1 they defaulted to `sessions/` and
`var/history.sqlite` inside the repository; those are not moved automatically. If they
exist on your machine, move them once:

```bash
mkdir -p ~/.local/share/master-system
mv sessions ~/.local/share/master-system/sessions
mv var/history.sqlite* ~/.local/share/master-system/
```

A project without `repository` cannot run attempts (`run_task` is refused with
`no_repository`). `projects/ai-system` has none on purpose: self-hosting is stopped
until Milestone 3.

---

## 9. Trust boundaries

Only facts written by the orchestrator, a deterministic verifier or a human may
authorise anything. A model may block an action but never authorise it. Text written
by a worker never reaches a prompt unless it is quoted as data.

| Information | Class | Reaches Master? | Can authorise? |
| --- | --- | --- | --- |
| Task status, dependencies, milestones | Authoritative | yes | yes |
| Task spec / acceptance criteria (`acceptance`, human-set; evidence bound by `spec_hash`) | Human-approved fact | `has_acceptance` only | yes: it defines "done" |
| Approval records *(planned)* | Human-approved fact | yes | yes, bound to the operation hash and state revision |
| Attempt facts observed by the orchestrator: outcome, deadline, pids, exit codes, base/result SHA, files changed, diff stats | Trusted evidence | outcome and changes (exit codes: history only) | yes |
| Deterministic verdict from protected checks in an orchestrator-owned workspace | Trusted evidence | yes | yes (the completion gate) |
| Verifier text: test names, failure excerpts | Trusted source, tainted content | bounded and quoted | no |
| Verdict from a model reviewer *(future)* | Untrusted evidence | labelled | can block, never pass |
| Worker's reported status, summary, `state_updates` | Untrusted evidence | status only, labelled as a claim | no |
| Raw transcript, stdout, tool calls, worker and model identity | Opaque provenance | no | no |
| Master decisions, reasons, proposed tasks and plans | Model suggestion | its own recent decisions | only through policy or approval |
| Lessons / memory *(future)* | Suggestion until a human confirms it | after confirmation | no |

Since Milestone 1 the code follows this table: the verifier works only from the
orchestrator's workspace and facts, and the worker's status reaches Master only as
`worker_reported_status`.

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
python -m core.master set-acceptance <project> <task> --command "python -m pytest -q" --protect "tests/*"
python -m core.master set-acceptance <project> <task> --clear
```

Mutating commands take the project lock and refuse while an autonomous run holds it.

### Integrate a verified attempt (human only)

```bash
python -m core.attempts integrate <project> <attempt_id>     # fast-forward only; idempotent
```

It requires a finished attempt verified `pass` against the task's current spec, refuses
if the base branch moved on or its checkout is dirty, and records an `integration`
event. Attempt ids are in history (`attempt_started` events).

### Ask Master for one proposal (human approves)

```bash
python -m core.reason_cli "What should happen next?" --project ai-system --provider ollama
python -m core.reason_cli "..." --provider deepseek --dry-run      # show only, never execute
```

The approval prompt defaults to **no**. A `run_task` in a proposal is never executed here.

### The owner's command: `ms` (Milestone 3, hands-off)

```bash
ms status             # projects, tasks by state, what waits for you, today's spend, links
ms report [session]   # a session's report (default: the latest)
ms install            # put `ms` on PATH (~/.local/bin/ms)
ms service install    # the background service: starts at login, processes approved tasks
ms pause | ms resume  # start nothing new / allow new work (a running task finishes)
ms stop               # stop the current run now (recorded as interrupted), and pause
ms notify setup       # phone notifications via ntfy: prints the private topic to subscribe to
ms notify test
ms daemon [--once]    # the service loop itself (systemd runs this)
```

The service (`core/daemon.py`, systemd user unit `master-system.service`) checks every
`[daemon] interval_s` seconds: if not paused and today's priced spend (Master + worker, from
history) is under `[budget] daily_usd`, it starts one run per idle project that has work, with
a run cap of `min([budget] run_usd, what is left today)`, through `core.run_cli` in a child
process (`core/host.py`), so every run_cli safety rule applies. Work = a task that is
`in_progress`, or `planned` with its dependencies completed, has acceptance commands, and has
fewer than 3 failed attempts since the last human action on it (otherwise the owner is asked,
once). After each run the owner gets a short phone notification: done, blocked, errors, spend
today, and the preview link. The Master's key is read from `~/.config/master-system/master.env`
into the run's environment only.

Branches (Match Legends, `project.yaml`): `main` = the released game, changed only by a
release PR the owner merges; `develop` = where the system integrates verified work
(`auto_integrate: true`); `gh-pages` = the published site. Preview:
<https://f0rrel.github.io/Match_Legends_mobile_game/develop/>. GitHub App setup:
`docs/github-setup.md`, then `ms github setup --app-id N` and `ms github check`.

### Run the autonomous loop (`core/run_cli.py`)

```bash
export DEEPSEEK_API_KEY=...                      # the Master's key: run_cli's environment only
python -m core.run_cli start <project> --objective "..." [--session ID] [--until-stopped]
python -m core.run_cli resume <session> [--until-stopped] [--max-runs 10] [--max-hours 8]
python -m core.run_cli status [<project>]        # tasks, sessions, open attempts, lock holder, events
python -m core.run_cli report <session> [--json] # from history alone
python -m core.run_cli integrate <project> <attempt_id> [--rebase]     # human only
python -m core.run_cli task describe <project> <task> --text "..."      # human edit, recorded
python -m core.run_cli task set-acceptance <project> <task> --command "..." --protect "tests/*"
python -m core.run_cli task manual-check <project> <task> --text "..."  # how to check by hand
```

Configuration: `~/.config/master-system/config.toml` (see `core/run_config.py`; defaults:
DeepSeek V4 Flash for the Master, OpenCode at `~/.opencode/bin/opencode` for the worker,
worker home `~/.local/share/master-system-worker`). Workers get only the allowlisted
environment of `core/worker_env.py`: Node >= 22 from nvm on `PATH`, the worker home as
`HOME`, no secrets. The worker's own OpenCode credential lives in the worker home
(`HOME=~/.local/share/master-system-worker opencode auth login`).

Reference wiring in tests:

- `tests/test_ollama_backend.py::test_the_full_loop_runs_through_ollama_without_touching_the_loop`
  (loop, Ollama worker and a real pytest verifier);
- `tests/test_session_runner.py` and `tests/test_recovery.py` (sessions, lock, recovery);
- `tests/test_acceptance_verifier.py` and `tests/test_attempt_workspace.py` (worktrees,
  acceptance, deadlines);
- `tests/test_m2_dry_run.py` (one task end to end through `run_cli` on a toy repo).

---

## 11. Known gaps

These come from the architecture review of 2026-10-05, which covered commit `18f8239`,
updated at the end of Milestone 1. IDs match the review document. **Verified** means
the behaviour was reproduced with a script against the code. Closed in Milestone 1:
C1, C2, H1, M2, L5.

| ID | Severity | Gap | Planned fix |
| --- | --- | --- | --- |
| N1 | HIGH | **Isolation, not sandboxing.** Workers run as the owner's user. A worktree isolates files, not authority: a worker can write outside its worktree (its log file descriptors also reveal the state dir's location) or change refs in the managed repository (including `base_branch`) through git. A process started with `setsid` escapes its process group, so neither the group kill nor the `timeout` deadline reaches it. It does keep the inherited lock descriptor, and `ProjectLock.release` never unlocks, so recovery, the next run and integration refuse while it lives instead of racing it; a stuck escapee blocks the project until a human kills it. | Containers or an OS sandbox for workers (a later milestone). Integration already refuses a moved base. |
| H2 | HIGH | Policy is keyed on operation names. The model can autonomously rewrite titles, cancel, reopen, or complete milestones, and any status can go to any status. A retitle does invalidate earlier evidence (`spec_changed`), but a reopen does not (P7). | A task transition table; `requires_approval(operation, state)` by transition and field. Milestone 3. |
| H3 | HIGH | Approvals can be requested but never granted (`pending_approval` lacks the arguments; there is no approve command or event). | `approval_requested`/`granted`/`denied` events bound to the operation hash and state revision; CLI approve/reject; resume applies a granted operation without a model call. Milestone 3. |
| H4 | HIGH | The attempt budget resets every session, and infrastructure failures (including a failed `git worktree add`) spend it. | Count per task since the last human action; classify failures; add time and cost budgets. Milestone 3. |
| H5 | HIGH | **Mostly closed.** `core/run_cli.py` runs the loop for real (start/resume/status/report/integrate), tested end to end offline. It has not yet run against a real model and worker. | The supervised run and the overnight run on Match Legends (Milestone 2). Approve is Milestone 3. |
| H6 | HIGH | The project is at the reinvention line for durable execution. | Keep the existing code; spike DBOS for timeouts, cancellation, waits and scheduling. Milestone 4. |
| M1 | MEDIUM | **Partly closed.** Deadlines, group kill and `timed_out` are done. There is no `cancel` command (deferred: killing a recorded PID risks PID reuse). | Cancel with DBOS (Milestone 4) or a PID-reuse-safe design. |
| M3 | MEDIUM | **Mostly closed.** Master sees the orchestrator's outcome, files changed, diff stats, `spec_current` and the verifier's findings; the worker's status is labelled a claim. Process exit codes are recorded in history but not shown to Master. | Add exit codes / failing test names to the context if Milestone 2 shows they are needed. |
| M4 | MEDIUM | **Partly closed.** Human spec edits made through `run_cli task ...` are recorded (`human_action`), and the report flags spec changes no recorded event explains. Edits made with `core.master` or in YAML are still not recorded, and YAML has no revision. | A `revision` counter; record every Master write, with `actor`. Milestone 3. |
| M5 | MEDIUM | WorkSession duplicates history (counters stored twice). | Keep a session identity row in the history DB; derive the rest from events. |
| M6 | MEDIUM | The Ollama backend is a homemade coding agent. | Freeze it as a fixture; build the next worker via ACP or the OpenHands SDK. |
| L1 | LOW | Events lack `actor` and `caused_by` (only `integration` records an actor). | Add both columns. |
| L2 | LOW | No stuck or oscillation detection. | Repetition and ping-pong rules (OpenHands-style). Milestone 3. |
| L3 | LOW | A model call is spent on every forced move. | Deterministic rules for single-legal-move steps, recorded as actor=system. |
| L4 | LOW | Legacy parser path, `proposal._decision`, experiments in the repo root. | Delete or move; keep docstrings short; write ADRs. |
| L6 | LOW | Attempt worktrees and `attempt/*` branches are never cleaned up. Acceptance commands leave their by-products (for example `__pycache__`) in the worktree. | A cleanup command for integrated or abandoned attempts. |
| L7 | LOW | **Closed by `integrate --rebase`** (M2, D1): the attempt is replayed onto the moved base, re-verified there, and integrated exactly at the verified SHA. A conflict still means re-running the task. | — |
| N2 | MEDIUM | A task whose dependency is completed but not integrated can start against a base that lacks it: attempts start from `base_branch`, and readiness counts a dependency as satisfied once it is `completed`. | **Milestone 3:** dependencies count as satisfied only when integrated. Until then, tasks run in one unattended session must be independent. |

---

## 12. Roadmap

### Owner-approved

- The five-milestone roadmap below was **approved by the owner on 2026-10-05**.
- **Milestone 1, "Contain and verify", is implemented** (branch `m1-contain-verify`,
  all acceptance criteria pass). It also closes the earlier "Trustworthy loop: explicit
  execution, evidence history, safe resume" milestone.
- **Next: Milestone 2, "Run it for real"**, once the owner starts it.
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

**1. Contain and verify** (implemented on `m1-contain-verify`; plan: `docs/plans/m1-contain-verify.md`)

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

- Objective: `run_cli` (start/resume/status), a verifier (the existing `AcceptanceVerifier`),
  one real worker (the OpenCode CLI; owner decision), and a real repo (Match Legends, in a
  dedicated clone) with 5 independent tasks, run overnight. Plan: `docs/plans/m2-run-for-real.md`.
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
| History DB is copied to `history.sqlite.bak-v1-<timestamp>` before the v1→v2 migration; a failed copy refuses the migration | A schema rebuild must be reversible | **Approved (2026-10-05)**, implemented |
| M1 plan (worktree per attempt, orchestrator-owned workspace, lock fd inherited by workers, deadlines, acceptance + AcceptanceVerifier, spec binding, project-wide recovery, gated INTEGRATE, runtime state outside the repo) | Contain and verify | **Approved (2026-10-05)**, implemented on `m1-contain-verify` |
| *Implementation:* every autonomous run holds the project lock; a loop without one takes it itself (and recovers first) | So every worker inherits a lock, whoever started the loop | Taken without the owner (in the plan, §2.11); conservative |
| *Implementation:* snapshot commits set their identity through `GIT_AUTHOR_*` / `GIT_COMMITTER_*` env, not `-c user.*` | Inherited `GIT_AUTHOR_*` env would otherwise override the orchestrator's identity (found by a test) | Taken without the owner; conservative |
| *Implementation:* a process killed by signal or exiting 124/137 counts as `timed_out` only once the deadline has passed | `timeout` signals its own group, so it can die by signal; a worker may legitimately exit 124 | Taken without the owner |
| *Implementation:* when a worker process exits, anything left in its process group is killed | Leftover children would keep writing and keep the lock | Taken without the owner; conservative |
| *Implementation:* an attempt whose snapshot commit fails is not verified | There is no committed result to verify | Taken without the owner; conservative |
| *Implementation:* backends no longer put a `workdir` in their artifacts | Nothing may take a location from worker output | Taken without the owner |
| *Implementation:* integrate also refuses if `project.yaml`'s repository/base branch changed since the attempt, or if the base's checkout has uncommitted tracked changes | Never integrate into something other than what was verified; never clobber the owner's edits | Taken without the owner; conservative |
| *Implementation:* recovery records an observation error instead of failing | A broken worktree must not block recovery of the rest | Taken without the owner |
| *Implementation:* `create_task(status=completed)` is gated like `update_task` | Otherwise creating a task as completed bypasses the gate | Taken without the owner (claude-week); conservative |
| *Process:* plan commit 7 was split into 7a (loop holds the lock) and 7b (orchestrator owns the workspace) | It was bigger than planned | Per the owner's instruction to split large commits |
| Worker output goes to log files under the state dir (path + sha256 in history); the helper waits on the process, not its pipes | A finished worker with a background child was reported `timed_out` (independent review) | **Approved (2026-10-05)**, implemented |
| `ProjectLock.release` closes its descriptor and never calls `LOCK_UN` | Unlocking would release the lock for a worker that escaped its group (independent review) | **Approved (2026-10-05)**, implemented |
| Worker and verification processes get an allowlisted environment (`core/worker_env.py`): `PATH` = the newest Node >= 22 from nvm's install directory (found by directory name, no shell startup files) + `/usr/local/bin:/usr/bin:/bin`; `HOME` and `XDG_*` = a dedicated worker home; `LANG`/`LC_ALL`/`TERM`/`TZ`; `CI=1`; no secrets (secret-looking extras are refused) | Workers must not see the Master's keys; the managed project needs Node 22 (owner upgraded the user's Node to 22 via nvm, 2026-10-05; system Node 18 untouched) | **Approved (2026-10-05)**, implemented (M2) |
| M2: the human-only task `description` is part of `spec_hash` only when present | Adding the field must not invalidate evidence for tasks without one | Taken without the owner (plan said "included"); conservative |
| M2: `run_cli` refuses to start without `DEEPSEEK_API_KEY` (for the DeepSeek provider), an OpenCode binary, or a Node >= 22 | Fail before any history is written, with a clear message | Taken without the owner |
| M2: the usage of unusable model replies is recorded as `failed_call_usage` (on the next decision, or on `run_stopped`) | Every paid call is counted | Approved in the plan; implemented |
| M2: the report counts a spec change as explained by a `human_action` or by Master's own recorded `update_task`/`create_task` result | A model retitle is recorded; only edits outside run_cli are "unexplained" | Taken without the owner |
| M2: a failed re-verification during `integrate --rebase` is recorded on the attempt (and so becomes its latest verdict) | The record must show what was checked, including failures | Taken without the owner |
| Match Legends requires Node 22; Playwright unpinned (`^1.63.0`); tests use globs for Node 22's runner | The owner upgraded the user's Node to 22 via nvm (2026-10-05) | **Approved (2026-10-05)**, done in Match Legends `main` (`f89b357`) |
| Tasks gain a human-only `manual_check` (how to check the result by hand); it is **not** part of `spec_hash`, is copied into `attempt_started`, and the report prints it under each completion | The report must show it from history alone; rewording a manual check should not invalidate verified evidence | Requested by the owner; exclusion from `spec_hash` taken without the owner |
| Attempt commits are named `<task id>: <task title>` when the attempt is snapshotted, not at integration | Integration must land exactly the verified SHA (D1); renaming at integration would change it | Requested by the owner; placement taken without the owner |
| `integrate` accepts a unique attempt-id prefix of 8+ characters | The report prints 12 | Requested by the owner |
| M3 hands-off plan approved (`docs/plans/m3-hands-off.md`), H-D1..H-D6 as proposed, start on login | The owner's role: talk to a planner, play-test develop, approve releases | **Approved (2026-10-06)** |
| A background service starts runs on its own: approved, ready tasks with acceptance; daily and per-run caps; no retry after 3 failures since the last human action; ntfy notifications | Hands-off; it cannot spend on a hopeless task every day | **Approved (2026-10-06)**; details taken without the owner |
| `core/host.py` joins the process-starting modules (the service's own runs and systemd unit; never workers) | The boundary test lists who may start processes | Taken without the owner |
| **H-D1:** for a project with `auto_integrate: true`, integrating a verified attempt into its `base_branch` (`develop`) is a ROUTINE system action, done by the orchestrator right after the attempt passes, under the run's lock, with the same rules as a human integration (finished, pass, current spec; rebase + re-verification if the base moved; the base moves to exactly the (re-)verified commit). `integrate` into the release branch (`main`) stays human-only: it happens only by the owner merging a release PR on GitHub | Hands-off: the owner play-tests develop, approves releases | **Approved (2026-10-06)**, changes P4/D1 for develop only |
| **H-D2:** in such a project a task completes only when its latest attempt is integrated (completion-gate reason `not_integrated`), so a dependency is satisfied only when its code is in develop (closes N2 there) | "Done" means "in the preview" | **Approved (2026-10-06)** |
| **H-D3:** a rebase conflict or failed re-verification is recorded as `integration_refused` (history schema v4) and shown to Master as "run the task again"; the next attempt starts from the new develop, within the attempt limit | No human needed for an ordinary conflict | **Approved (2026-10-06)** |
| **H-D4/G1:** after each run the service pushes develop and a `gh-pages` site (release branch's `www/` at `/`, develop's at `/develop/`) with a GitHub App installation token (JWT signed by `openssl`; token only in the git process's environment via GIT_ASKPASS). The app has Contents/Pull requests/Pages write and no Administration; a ruleset on main allows no bypass (docs/github-setup.md) | Stable preview URL; the app can never change main | **Approved (2026-10-06)** |
| History schema v4 adds `integration_refused`, `published`, `planner_turn`, `release` (migration with backup, as before) | One migration for all of Milestone 3 | Taken without the owner |
| Merged Milestone 1 to main (2026-10-05) | Milestone 1 complete and reviewed; `main` fast-forwarded to `m1-contain-verify`, tagged `m1-contain-verify` | **Approved (2026-10-05)** |

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
  - session code names no provider;
  - no backend or verifier imports `subprocess`; only `worker_process.py`, `workspace.py`
    and `attempts.py` start processes;
  - `acceptance` is not in the operation vocabulary, and `integrate_attempt` is always gated.
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
5. If autonomous runs have happened on this machine, look at
   `~/.local/share/master-system/sessions/*.yaml` and `~/.local/share/master-system/history.sqlite`
   (for example `sqlite3 ~/.local/share/master-system/history.sqlite "select seq,type,task_id,created_at from events order by seq desc limit 30"`),
   and the attempt worktrees under `~/.local/share/master-system-worktrees/`.
   Anything a dead process left open is recovered automatically the next time a run takes the lock.
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
- [x] Should `claude-week` be merged into `main` now, or after milestone 1?
  **After Milestone 1: `main` was fast-forwarded to `m1-contain-verify` (tag `m1-contain-verify`) on 2026-10-05.**
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
| **INTEGRATE** | `integrate_attempt`: always gated, so Master can only request it; a human runs `python -m core.attempts integrate`, which fast-forwards the base branch to a verified attempt's result SHA. |
| **Worktree / workspace** | The git worktree the orchestrator creates for one attempt; workers and verifiers get it as an `AttemptWorkspace`. |
| **spec_hash** | sha256 of a task's `{title, acceptance}`; binds an attempt's evidence to the spec it was run against. |

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
