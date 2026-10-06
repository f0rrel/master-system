# Architecture

How Master System works, for developers and AI assistants working on the code. For
installation and usage, see the [README](../README.md).

## Components

```text
 human ──ms chat──▶ Planner ──approve──▶ tasks.yaml + tests on develop
                                           │
 systemd user service: ms daemon (core/daemon.py), every interval_s, within caps
                                           ▼
   run_cli (child process) ─▶ AutonomousLoop ─▶ Master model proposes ONE operation
                                   │                 │
                                   │        policy + completion gate decide
                                   ▼
   TaskOrchestrator ─▶ git worktree per attempt ─▶ worker (OpenCode CLI, allowlisted env)
        │                                         ─▶ AcceptanceVerifier (frozen acceptance)
        └─ pass ─▶ AutoIntegrator: rebase + re-verify ─▶ develop (fast-forward)
   after the run: Publisher ─▶ push develop + gh-pages (GitHub App) ─▶ ntfy notification
 human ──ms release──▶ PR develop→main ──human merges──▶ tag, GitHub Release, live site
```

- **Master** (`core/master.py`) is the only writer of project state (the YAML files).
- **The reasoning Master** is a model that proposes one operation per step:
  - `update_task` / `create_task` (state);
  - `run_task` (dispatch; carries no worker, model, workspace or prompt);
  - `integrate_attempt` (human only).
- **Policy and the completion gate** decide whether a proposal runs. A task completes only
  if its latest attempt finished, was verified `pass` against the current spec and, in
  projects with `auto_integrate: true`, is integrated into the base branch.
- **History** (`core/sqlite_history.py`, SQLite, append-only, schema v4) records every
  decision, attempt, verification, integration, human action, publish and release.
  Reports are built from history alone.
- **Workers** never touch project state. Their claims (status, summary, usage) are recorded
  as claims; what counts is the verifier's verdict on the committed result.

## Module map (`core/`)

| Area | Modules |
| --- | --- |
| State | `master.py`, `work_manager.py`, `project_state.py`, `project_manager.py`, `human_edits.py` |
| Loop | `autonomous_loop.py`, `reasoning.py` (operations, policy), `reasoning_engine.py`, `evidence.py` (completion gate, attempt facts), `session_runner.py`, `work_session.py`, `session_store.py`, `recovery.py`, `run_lock.py` |
| Execution | `task_orchestrator.py`, `workspace.py` (worktrees, git), `worker_process.py`, `worker_env.py`, `opencode_backend.py`, `ollama_backend.py` (frozen), `worker_tiers.py`, `acceptance_verifier.py`, `verification.py`, `execution.py`, `execution_runner.py` |
| Integration and release | `attempts.py` (integrate), `auto_integrate.py`, `publish.py`, `github.py` (App auth, push), `release.py` |
| Models | `provider.py` (interface), `deepseek_provider.py`, `ollama_provider.py`, `opencode_provider.py`, `usage.py` |
| Operator tools | `ms.py` (the `ms` command), `daemon.py`, `host.py` (service processes), `notify.py`, `planner.py`, `planner_checks.py`, `report.py`, `run_cli.py` (lower-level CLI), `run_config.py`, `paths.py` |
| History | `history.py` (event types, interface), `sqlite_history.py` (the only module that imports `sqlite3`) |

## Files and directories

| Location | Content |
| --- | --- |
| `$MS_HOME` | This repository. The service runs from it. |
| `~/.config/master-system/projects/<id>/` | Project definitions and state: `project.yaml`, `milestones.yaml`, `tasks.yaml`. Runs update `tasks.yaml`. Private; never in this repository. `[run] projects_root` (or `--root` for the lower-level CLIs) points elsewhere. |
| `$MS_HOME/examples/projects/` | A fictional example project definition |
| `$MS_HOME/docs/` | This document, troubleshooting, the decision log, the GitHub setup guide |
| `$MS_HOME/archive/` | Early experiments and historical milestone plans; unused by the code |
| `~/.config/master-system/` | `config.toml` (no secrets); `master.env` (model API key, mode 600); `github-app.pem` (600); `ntfy-topic` (600) |
| `~/.local/share/master-system/` | `history.sqlite` (and `.bak-*` migration backups); `sessions/`; `logs/` (worker and verification logs; `runs/` per service run); `planner/` (chats); `daemon-state.json`; `paused` (flag); `run.pid`; `last-looked`; `release-check`; `git-askpass.sh` (contains no secret) |
| `~/.local/share/master-system-worktrees/<project>/` | One git worktree per attempt, rebase or planner check, kept for inspection |
| `~/.local/share/master-system-worker/` | The worker's home and its only credential store |
| A dedicated clone per managed project | The repository the system works in (`repository` in `project.yaml`). Its push URL should be disabled; pushes go through the GitHub App. `refs/ms-release/<branch>` records the remote release branch at the last check. |
| `~/.config/systemd/user/master-system.service` | The service unit. Logs: `journalctl --user -u master-system.service` |

All XDG locations honour `$XDG_CONFIG_HOME` and `$XDG_DATA_HOME`.

## The service (`core/daemon.py`)

Every `[daemon] interval_s` seconds, unless paused or over the daily cap, the service:

1. completes releases that were merged on GitHub;
2. for each project with `auto_integrate: true` that is idle and has work, starts one run
   (`core.run_cli start … --until-stopped`) in a child process, capped at
   `min(run_usd, remaining daily budget)`;
3. after the run, publishes and sends a notification.

A task counts as work when it:

- is `planned` with all dependencies done, or is `in_progress`;
- has acceptance commands;
- has fewer than 3 failed attempts since the last human action (2 × ladder length when
  worker tiers are enabled).

A run that makes no progress marks the project **stalled** until a human acts on it.

## Planner (`core/planner.py`, `core/planner_checks.py`)

`ms chat <project>` opens a chat with the planner model. The planner may read files from
the project's base branch (read-only, through git) and returns structured JSON: questions,
files to read, or a draft epic with tasks. Each task has a title, size, description,
manual-check steps, test files and acceptance commands.

One owner message is one *turn* of up to four model calls. Files the model lists in
`read_files` are read and the model is asked again within the turn. If it names files in
prose instead, those are read too; if it only promises to act ("let me check…"), it is
re-asked once with a corrective note; the last call tells it to answer without further
reads. The owner sees only the final answer.

The project's test conventions come from `planner` in `project.yaml`: `test_dir`,
`test_suffixes`, `syntax_check`, `test_command_examples`, `test_guidance`,
`broken_test_markers`, `ignore_paths`, `setup`, `base_checks`, `protected_paths`. Nothing
in the planner assumes a language.

`check` validates a draft in a fresh worktree:

1. the project's `setup` commands run;
2. each drafted test passes `syntax_check` (if configured);
3. each task's test commands **fail** on the current code for the right reason (output
   containing a `broken_test_markers` entry means the test itself is broken);
4. `base_checks` still pass.

`approve` repeats the checks under the project lock, commits the tests to the base branch,
and writes the epic and tasks atomically through `Master.add_planned_work` (human-only),
recording a `human_action` per task with the draft's hash. Acceptance edits therefore stay
human-approved.

## Integration, publishing and releases

**Integration into `develop`** happens right after an attempt passes, under the run's lock:

- **`develop` has not moved:** fast-forward.
- **`develop` moved:** cherry-pick onto it, re-run acceptance, fast-forward to exactly the
  re-verified commit.
- **Conflict or failed re-verification:** recorded as `integration_refused`; the task runs
  again from the new `develop`.

**Publishing** pushes `develop`. If the project sets `github.site_dir`, it also rebuilds a
`gh-pages` branch with git plumbing (no checkout, no build step): the release branch's
`site_dir` at `/`, `develop`'s at `/develop/`. The site URL defaults to
`https://<owner>.github.io/<repo>/` and can be overridden with `github.site_url`.

| Branch | Changed by |
| --- | --- |
| `main` (release branch) | Only a human, by merging a release pull request (ruleset: PR required, no force push, no deletion, no bypass) |
| `develop` | The system: verified attempts, planner tests, changelog entries (ruleset: no force push, no deletion) |
| `gh-pages` | The system: the published preview and live site (only with `site_dir`) |
| `attempt/*`, `planner/*` | Local only, in the dedicated clone |

**A release** is a merge commit created by a human merging the release pull request. The
system then:

1. checks that the merge commit's tree equals the reviewed `develop` tree (a mismatch is
   reported and not tagged);
2. tags `v0.N`;
3. creates the GitHub Release;
4. republishes the site.

**GitHub App authentication:** for each use the system signs a JWT with `openssl`,
exchanges it for a one-hour installation token, and passes the token only in the
environment of the `git push` that needs it (`GIT_ASKPASS`). The App has Contents and Pull
requests write, Pages read, and no Administration. Setup: [GITHUB-SETUP.md](GITHUB-SETUP.md).

## Worker tiers (`core/worker_tiers.py`)

`[worker.profiles.*]` and `[worker] ladder` define tiers, cheapest first. A task starts at
the tier matching its `size` and moves up one tier after 2 failed semantic attempts
(finished or timed out without a pass; errors and interruptions do not count). It never
moves down. The orchestrator records the profile, tier and model on `attempt_started`; the
Master's context never names them. Reports show attempts, passes and cost per tier.

## Safety rules and trust boundaries

| Rule | Enforcement |
| --- | --- |
| Master is the only writer of project state; models only propose | `core/master.py`; boundary tests |
| Worker output is untrusted | `state_updates` are never applied; raw output is stored as provenance and never shown to Master |
| One worktree per attempt; runtime state outside every workspace | `core/workspace.py` isolation checks (`check_isolated`) |
| An independent definition of done | The task's `acceptance` (commands and protected paths), written by a human or the approved planner flow, run by the orchestrator's verifier |
| Evidence is bound to the spec | `spec_hash` on every attempt; a changed spec invalidates a pass |
| Intent before side effects; no automatic replay | `decision` / `attempt_started` are written first; unfinished attempts become `interrupted` |
| One run per project | Non-blocking `flock`, inherited by workers, never unlocked early |
| Deadlines | Workers run under `timeout --kill-after` in their own process group; leftovers in the group are killed |
| No secrets for workers | Allowlisted environment (`core/worker_env.py`): toolchain on `PATH`, the worker home, no keys; secret-looking extras are refused |
| The release branch changes only by a human merge | The App has no Administration permission; a ruleset on `main` requires a PR with no bypass |
| Only `worker_process.py`, `workspace.py`, `attempts.py` and `host.py` start processes | `tests/test_backend_boundaries.py` |
| No provider or model names above the adapters | Boundary tests |
| Spend | Daily, per-run, per-chat and per-task caps |

## Known gaps

| ID | Gap | Plan |
| --- | --- | --- |
| N1 | Workers run as the system's OS user: isolation, not a sandbox | Containers or an OS sandbox |
| H2 | Policy is keyed on operation names, not state transitions | A transition table |
| H3 | Approvals for gated operations cannot be granted outside a run | Approval events and `ms approve` |
| H4 | The service's per-task failure budget and the in-run attempt limit are separate | Unify |
| M4 | Edits made directly in YAML are not recorded (`ms chat` and `run_cli task …` edits are) | Record every write with an actor |
| H6 | Homemade durable execution | DBOS evaluation (milestone 4) |
| L6 | Old attempt worktrees and branches accumulate | Periodic cleanup |

## Developing

```bash
uv sync --extra dev
uv run python -m pytest -q            # the whole suite, offline
python -m core.run_cli --help         # start / resume / status / report / integrate / task …
```

Rules:

- Small commits, each with a green test suite.
- Never weaken a boundary test.
- Never commit runtime state or project definitions; tests use the fictional fixtures in
  `tests/fixtures/projects/`, copied into an isolated `XDG_CONFIG_HOME` for every test.
- Do not restart the service while `ms status` reports work in progress; develop in a
  separate worktree.
- Code is project-agnostic: project-specific values belong in `project.yaml` or the
  managed repository, never in `core/`.

### Documentation policy

Every milestone, release or fix batch updates, in the same change:

1. [CHANGELOG.md](../CHANGELOG.md): what changed.
2. [DECISIONS.md](DECISIONS.md): each decision and its rationale.
3. [TROUBLESHOOTING.md](TROUBLESHOOTING.md): new failure modes and known issues.
4. This document and the README, when behaviour, commands or configuration change.

Documentation stays project-agnostic. Information about a specific managed project lives
in that project's `project.yaml` and repository; the author's own setup is confined to the
last section of the README.
