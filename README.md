# Master System

A personal AI work system. You describe what you want in plain language. The system plans
it into small tasks with tests, has coding agents do them one at a time, checks every result
itself, and puts finished work into a preview you can play. You approve releases.

It is model-independent: the planner, the "Master" that drives the work, and the coding
workers are interchangeable models behind adapters. Today the Master and the planner use
DeepSeek V4 Flash, and the workers use the OpenCode CLI.

- **Part 1, [Using it](#part-1-using-it):** for the owner, in plain language.
- **Part 2, [How it works](#part-2-how-it-works):** for AI helpers and developers.

---

## Status

| Item | State (2026-10-06) |
| --- | --- |
| Milestones done | 1 "Contain and verify" (tag `m1-contain-verify`), 2 "Run it for real" (tag `m2-run-for-real`), 3 "Hands-off" (tag `m3-hands-off`) |
| Branch | `main`; the background service runs from this checkout |
| Tests | 1323 passed, 6 skipped (`uv run python -m pytest -q`, about 65 s) |
| Managed project | Match Legends (released v0.1); tasks ml-1 to ml-8 done, plus tonight's epic |
| Next | Fix the planner "says it will read files, then stops" bug ([Troubleshooting](docs/TROUBLESHOOTING.md)); policy by transition (H2); see [Roadmap](#roadmap) |

---

# Part 1: Using it

## What it does for you

1. You talk to the **planner** (`ms chat`). It turns your idea into an *epic*, a group of
   small *tasks*, each with tests and "how to check by hand" steps. It asks you questions
   when something is your decision.
2. You type **approve**. From then on the **background service** works on its own. For each
   task, a coding agent makes the change in an isolated copy of the code. The system then
   runs the tests itself and puts passing work into **develop**.
3. After each batch, the **preview link** shows the new version, and your phone gets a
   notification.
4. When you like the preview, you **release**: `ms release` opens a pull request on GitHub,
   and you merge it. The system tags the version, writes the release notes and changelog,
   and updates the **live game**.

## Daily workflow

| Step | You do | Command |
| --- | --- | --- |
| 1. Plan | Describe what you want; answer questions; type `check`, then `approve` | `ms chat match-legends` |
| 2. Wait | Nothing. Notifications tell you when a batch is done or something needs you | — |
| 3. Play-test | Open the preview link on your PC or phone; follow "how to check by hand" | the preview link |
| 4. Release | Open the release PR, then merge it on GitHub | `ms release match-legends` |
| 5. Look around | Any time | `ms status` |

## Commands

| Command | What it does |
| --- | --- |
| `ms status` | A headline ("Idle. 8 of 8 tasks done. Nothing needs you."), then *Needs you*, *Done since you last looked*, *Coming up*, links and today's spend. It also finishes releases you merged. |
| `ms status --details` | The technical view: every task, session and recent event. |
| `ms chat match-legends` | Talk to the planner (continues the last open chat). `--new` starts a new chat; `--file request.txt` sends a file as your first message. Inside the chat: `show`, `check`, `approve`, `discard`, `release`, `help`, `quit`. To send several lines as one message, paste them, or put them between two lines that contain only `"""`. |
| `ms release match-legends` | Writes release notes and a changelog entry, and opens a pull request `develop` → `main`. Merging it is your release approval. |
| `ms report` | The latest run's report: steps, attempts, verdicts, tokens and cost. `ms report <session>` shows a specific run. |
| `ms pause` / `ms resume` | Start nothing new (a running task finishes), or allow new work again. |
| `ms stop` | Stop the current run now (its work is kept), and pause. |
| `ms publish match-legends` | Push `develop` and the preview site now. The service does this after every run anyway. |
| `ms doctor` | Prints a diagnostic block to paste into any AI chat. It contains no keys, tokens or topics. |
| `ms notify test` / `ms notify send "text" [--link URL]` | Test notifications, or send your own one-line message to your phone. |
| `ms github check` | Checks the GitHub App, the branch protection and Pages, in plain words. |
| `ms service install` / `uninstall` | Install (or remove) the background service. It starts when you log in. |
| `ms install` | Put `ms` on your PATH (`~/.local/bin/ms`). |

## Notifications (ntfy app on your phone)

| Title | Meaning | What to do |
| --- | --- | --- |
| *match-legends: batch done* | One or more tasks are done and in the preview | Play-test the preview link |
| *match-legends: run finished* | A run ended without finishing a task (it may say "Blocked" or show an error) | Usually nothing; if it repeats, `ms status` |
| *match-legends: needs you* | A run made no progress, so the service waits for you | `ms status`, then change the task in `ms chat` |
| *match-legends: ml-N needs you* | A task failed several attempts | Change its description in `ms chat`; it is then retried |
| *Daily budget reached* | Today's spending cap was hit; work resumes tomorrow | Nothing, or raise the cap (below) |
| *match-legends: release* | A release was made ("Released v0.N"), or a release PR was closed | Nothing |
| *Master System service stopped* | The service crashed. systemd restarts it within a minute | If it repeats: `ms doctor` |

Batch messages end with "Release ready…" when the preview has work that isn't released yet.

## Links

| What | Where |
| --- | --- |
| Preview (`develop`) | <https://f0rrel.github.io/Match_Legends_mobile_game/develop/> |
| Live game (`main`) | <https://f0rrel.github.io/Match_Legends_mobile_game/> |
| Releases | <https://github.com/f0rrel/Match_Legends_mobile_game/releases> |
| This system's code | <https://github.com/f0rrel/master-system> |

## Costs and caps

All caps are in `~/.config/master-system/config.toml`. Prices are per million tokens in
`[prices]`.

| Cap | Setting | Now |
| --- | --- | --- |
| Per day (Master + planner + paid workers) | `[budget] daily_usd` | $0.60 |
| Per run | `[budget] run_usd` | $0.20 |
| Per planner chat | `[planner] chat_usd` | $0.30 |
| Per task | 3 failed attempts since your last change to it, then it waits for you | fixed (2 per worker tier, if tiers are on) |

Typical costs: a Master decision is about $0.001, and a planner turn about $0.005. The
default worker (OpenCode's free model) costs nothing. After changing caps, restart the
service when `ms status` says Idle: `systemctl --user restart master-system.service`.

## When something goes wrong

1. Run `ms status`. It says in plain words what needs you.
2. Look up the symptom in **[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)**.
3. Still stuck? Run `ms doctor`, then paste its output **and this README** into any AI chat.
   That is enough for a helper with no history to understand the system.

---

# Part 2: How it works

## Architecture

```text
 owner ──ms chat──▶ Planner (model) ──approve──▶ tasks.yaml + tests on develop
                                                   │
 systemd user service: ms daemon (core/daemon.py) — every 5 min, within caps
                                                   ▼
   run_cli (child process) ─▶ AutonomousLoop ─▶ Master (model) proposes ONE operation
                                   │                 │
                                   │        policy + completion gate decide
                                   ▼
   TaskOrchestrator ─▶ git worktree per attempt ─▶ worker (OpenCode CLI, allowlisted env)
        │                                         ─▶ AcceptanceVerifier (owner's tests)
        └─ pass ─▶ AutoIntegrator: rebase + re-verify ─▶ develop (fast-forward)
   after run: Publisher ─▶ push develop + gh-pages (GitHub App) ─▶ ntfy notification
 owner ──ms release──▶ PR develop→main ──owner merges──▶ tag, GitHub Release, live site
```

- **Master** (`core/master.py`) is the only writer of project state (YAML files).
- **The reasoning Master** is a model that proposes one operation per step:
  - `update_task` / `create_task` (state);
  - `run_task` (dispatch);
  - `integrate_attempt` (human only).
- **Policy and the completion gate** decide whether a proposal runs. A task completes only
  if its latest attempt finished, was verified `pass` against the current spec and, in
  hands-off projects, is integrated into `develop`.
- **History** (`core/sqlite_history.py`, SQLite, append-only, schema v4) records every
  decision, attempt, verification, integration, human action, publish and release. Reports
  are built from history alone.
- **Workers** never touch project state. Their claims (status, summary, usage) are recorded
  as claims; what counts is the verifier's verdict on the committed result.

### Key modules (`core/`)

| Area | Modules |
| --- | --- |
| State | `master.py`, `work_manager.py`, `project_state.py`, `project_manager.py`, `human_edits.py` |
| Loop | `autonomous_loop.py`, `reasoning.py` (operations, policy), `reasoning_engine.py`, `evidence.py` (gate, attempt facts), `session_runner.py`, `work_session.py`, `session_store.py`, `recovery.py`, `run_lock.py` |
| Execution | `task_orchestrator.py`, `workspace.py` (worktrees, git), `worker_process.py`, `worker_env.py`, `opencode_backend.py`, `ollama_backend.py` (frozen), `worker_tiers.py`, `acceptance_verifier.py` |
| Integration and release | `attempts.py` (integrate), `auto_integrate.py`, `publish.py`, `github.py` (App, push), `release.py` |
| Models | `provider.py`, `deepseek_provider.py`, `ollama_provider.py`, `opencode_provider.py`, `usage.py` |
| Owner tools | `ms.py` (the `ms` command), `daemon.py`, `host.py` (service processes), `notify.py`, `planner.py`, `planner_checks.py`, `report.py`, `run_cli.py` (lower-level CLI), `run_config.py` |

## Files and folders

| Where | What |
| --- | --- |
| `~/AI/master-system-big-pickle` | This repo. The service runs from it (branch `main`). |
| `projects/<id>/` | Project state: `project.yaml`, `milestones.yaml`, `tasks.yaml`. Runs change `tasks.yaml`. |
| `docs/` | `TROUBLESHOOTING.md`, `DECISIONS.md` (decision log), `github-setup.md`, `plans/` (milestone plans) |
| `archive/` | Early experiments (`bob.py`, `agents/`, `docker/bob`). Unused. |
| `~/.config/master-system/` | `config.toml` (settings, no secrets); `master.env` (the DeepSeek key, mode 600); `github-app.pem` (the App's private key, 600); `ntfy-topic` (600); older `overnight.toml` and `supervised-*.toml` |
| `~/.local/share/master-system/` | `history.sqlite` (+ `.bak-*` backups); `sessions/`; `logs/` (worker and verification logs, `runs/` per service run); `planner/` (chats); `daemon-state.json`; `paused` (flag); `run.pid`; `last-looked`; `release-check`; `git-askpass.sh` (no secret in it) |
| `~/.local/share/master-system-worktrees/<project>/` | One git worktree per attempt, rebase or planner check (kept for inspection) |
| `~/.local/share/master-system-worker/` | The worker's home (OpenCode login). Workers get no other secrets. |
| `~/AI/managed/match-legends` | The dedicated clone the system works in. Its push URL is disabled; pushes go through the App. `refs/ms-release/main` = GitHub's `main` at the last check. |
| `~/.config/systemd/user/master-system.service` | The service unit. Logs: `journalctl --user -u master-system.service` |

## The service (`core/daemon.py`)

Every `[daemon] interval_s` (300 s), unless paused or over today's cap, it:

1. finishes merged releases;
2. for each project with `auto_integrate: true`, if it is idle and has work, starts one run
   (`core.run_cli start … --until-stopped`) in a child process, with a run cap of
   `min(run_usd, what is left today)`;
3. after the run, publishes and sends a notification.

"Work" is a task that:
- is `planned` with its dependencies done, or `in_progress`;
- has acceptance commands;
- has fewer than 3 failed attempts since the last human action.

A run that makes no progress marks the project **stalled** until a human acts. The
system's own project (`projects/ai-system`) has no `auto_integrate`, so it is never run.

## Safety rules and trust boundaries

| Rule | How it's enforced |
| --- | --- |
| Master is the only writer of project state; models only propose | `core/master.py`; boundary tests |
| Worker output is untrusted | `state_updates` are never applied; raw output is stored as provenance and never shown to Master |
| One worktree per attempt; runtime state outside every workspace | `core/workspace.py` isolation checks (`check_isolated`) |
| An independent definition of done | The task's `acceptance` (commands and protected paths), written by a human or the approved planner flow, run by the orchestrator's verifier |
| Evidence is bound to the spec | `spec_hash` on every attempt; a changed spec invalidates a pass |
| Intent before side effects; no automatic replay | `decision` / `attempt_started` are written first; unfinished attempts become `interrupted` |
| One run per project | Non-blocking `flock`, inherited by workers, never unlocked early |
| Deadlines | Workers run under `timeout --kill-after`, in their own process group |
| No secrets for workers | An allowlisted environment (`core/worker_env.py`): Node 22 on `PATH`, the worker home, no keys |
| `main` changes only by the owner | The App has no Administration permission; a ruleset on `main` requires a PR, with no bypass |
| Only `core/worker_process.py`, `workspace.py`, `attempts.py` and `host.py` start processes | `tests/test_backend_boundaries.py` |
| No provider or model names above the adapters | Boundary tests |
| Caps | Daily, run, chat and task caps (above) |

Known limit (N1): workers run as the owner's user. Worktrees isolate files, not authority.

## GitHub App and branches

| Branch | Who changes it |
| --- | --- |
| `main` | Only you, by merging a release PR (ruleset: PR required, no force push, no deletion, no bypass) |
| `develop` | The system: verified attempts, planner tests, changelog entries (ruleset: no force push, no deletion) |
| `gh-pages` | The system: `/` = `main`'s `www/`, `/develop/` = `develop`'s `www/` |
| `attempt/*`, `planner/*` | Local only, in the dedicated clone |

The App (id in `config.toml` `[github] app_id`) is installed only on Match Legends. Its
permissions are Contents write, Pull requests write and Pages read. It has no
Administration permission, so it can't create a Pages site or change rules; you did those
once. For every use the system signs a JWT with `openssl`, exchanges it for a one-hour
installation token, and passes the token only in the environment of the one `git push` that
needs it (`GIT_ASKPASS`). Setup guide: [docs/github-setup.md](docs/github-setup.md).

**Integration into `develop`** happens right after an attempt passes, under the run's lock:
- **`develop` hasn't moved:** a fast-forward.
- **`develop` moved:** a cherry-pick onto it, a re-run of the acceptance, then a
  fast-forward to exactly the re-verified commit.
- **A conflict:** recorded as `integration_refused`, and the task runs again from the new
  `develop`.

**A release** happens when you merge the PR (a merge commit). The system then:
1. checks that the merge commit's files equal the reviewed `develop`'s;
2. tags `v0.N`;
3. creates the GitHub Release;
4. republishes the site.

## Configuration (`~/.config/master-system/config.toml`)

| Section | Keys |
| --- | --- |
| `[master]` | `provider` (deepseek, ollama, opencode), `model`, `base_url`, `timeout_s` |
| `[worker]` | `opencode_bin`, `model` (empty = the OpenCode default), `home`, `extra_args`, `node_min_major`, `playwright_browsers_path`, `ladder` + `[worker.profiles.<name>]` (`model`, `home`) for tiers |
| `[run]` | `max_steps`, `max_retries`, `max_attempts_per_task`, `attempt_timeout_s`, `verification_timeout_s`, `projects_root` |
| `[budget]` | `daily_usd`, `run_usd` |
| `[daemon]` | `interval_s`, `ntfy_server` |
| `[planner]` | `provider`, `model`, `base_url`, `timeout_s`, `chat_usd` |
| `[github]` | `app_id`, `key_path` |
| `[prices]` | `"<model>" = { input, output, cached_input }` (USD per million tokens) |

Project options live in `project.yaml`:
- `repository` and `base_branch`;
- `auto_integrate: true` (hands-off);
- `github: {repo, release_branch, site_dir}`;
- `planner: {test_dir, setup, base_checks, protected_paths}`.

## Developing

```bash
uv sync --extra dev
uv run python -m pytest -q            # the whole suite, offline
python -m core.run_cli --help         # lower-level: start/resume/status/report/integrate/task …
```

Rules for changing the system:
- Small, green commits, pushed after each one.
- Never weaken a boundary test.
- Never commit runtime state, except the tasks' status in `projects/*/tasks.yaml`.
- Record every decision in `docs/DECISIONS.md`.
- Don't restart the service while `ms status` says it is working.

## Decision log (summary)

The full log is in **[docs/DECISIONS.md](docs/DECISIONS.md)**. The decisions that shape
everything:

| Decision | Why |
| --- | --- |
| Project state (facts) is separate from operations (side effects); Master is the single writer | Auditability, safe recovery |
| Models propose; policy and evidence decide; no code path branches on which model ran | Model independence; prompt-injection resistance |
| A worktree per attempt; acceptance written by a human or the approved planner; an orchestrator-owned verifier | An independent definition of done |
| SQLite append-only history; YAML stays authoritative; no faked atomicity | Simple, honest failure semantics |
| H-D1: integration into `develop` is routine for hands-off projects; `main` only through an owner-merged release | Hands-off without giving up control of releases |
| H-D2/H-D3: done means integrated; a conflict reruns the task | A dependency counts only when its code is in `develop` |
| A GitHub App limited to one repo, with short-lived tokens and no admin rights | Least privilege |
| Use mature coding agents as workers (OpenCode); don't build one | Focus on the control plane |

## Known issues and gaps

| ID | Gap | Plan |
| --- | --- | --- |
| P1 | **Planner bug:** it sometimes says "I'll read the files…" and ends its turn without doing anything | Fix next ([Troubleshooting](docs/TROUBLESHOOTING.md)) |
| N1 | Workers run as the owner's user: isolation, not a sandbox | Containers or an OS sandbox (later) |
| H2 | Policy is keyed on operation names, not on transitions (Master may retitle, cancel or reopen on its own) | A transition table (next) |
| H3 | Approvals for gated Master operations can't be granted from outside a run | Approval events plus `ms approve` |
| H4 | Partly done: the service stops a task after 3 failures since the last human action; inside a run, the attempt limit is still per session | Unify |
| M4 | Edits made directly in YAML aren't recorded (`ms chat` and `run_cli task …` edits are) | Record every write, with an actor |
| H6 | Homemade durable execution | A DBOS spike (Milestone 4) |
| L6 | Old attempt worktrees and branches pile up | Periodic cleanup |
| T1 | Worker tiers are built but off: the paid tiers need a spend-limited key in their own worker home | When the owner wants a paid worker |

## Roadmap

| # | Milestone | State |
| --- | --- | --- |
| 1 | Contain and verify | Done (`m1-contain-verify`) |
| 2 | Run it for real | Done (`m2-run-for-real`) |
| 3 | Hands-off: service, auto-integration, preview, planner, releases, tiers | Done (`m3-hands-off`) |
| next | Planner bug (P1), policy by transition (H2), approvals (H3), budget unification (H4) | Planned |
| 4 | Durable runtime: a DBOS spike, then adopt it or walk away | Planned |
| 5 | Plans and focused context: per-task digests, project notes, prompt versions on decisions | Partly done by the planner |
| later | Memory and self-improvement; self-hosting (the system working on itself) | Stopped until approvals and transition policy exist |

## Documentation rule

Every milestone, release or fix batch updates, in the same change:

1. **Status** (top of this README): milestone, tests, branch, next.
2. **[CHANGELOG.md](CHANGELOG.md)**: what changed, in plain words. For a managed project,
   `ms release` writes the project's own `CHANGELOG.md` from the task history.
3. **[docs/DECISIONS.md](docs/DECISIONS.md)**: every decision, who took it, and why.
4. **[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)**: anything that broke and how it was
   fixed, plus known issues.
