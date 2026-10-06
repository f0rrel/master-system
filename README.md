# Master System

![Status: personal project, actively developed](https://img.shields.io/badge/status-personal%20project%2C%20actively%20developed-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)
![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-informational)

A model-independent control plane for AI-assisted software development. You describe a
change in plain language; a planner model turns it into small tasks with executable tests;
coding agents implement each task in an isolated git worktree; the system verifies every
result itself, integrates passing work into a development branch, publishes a preview, and
leaves releases to a human.

## Why

Coding agents are capable but not trustworthy on their own: they report success they did
not achieve, edit files they should not touch, and spend money without bound. Master
System treats every model as an untrusted proposer and keeps authority in deterministic
code:

- **Done means verified.** A task completes only when acceptance tests, fixed before the
  work started, pass on the committed result and that result is integrated.
- **Models propose, policy decides.** The orchestrating model requests one operation at a
  time; a policy layer and a completion gate decide whether it runs.
- **Releases stay human.** The system may change the development branch; only a human
  merge changes the release branch.
- **Everything is auditable.** Every decision, attempt, verification, integration and
  release is recorded in an append-only history, and reports are built from it alone.

## Features

- **Planner chat** (`ms chat`): turns a request into an epic of small tasks, each with
  test files, acceptance commands and manual-check steps. Drafts are validated in a fresh
  worktree (tests must be valid and must fail on the current code) before they can be
  approved.
- **Background service**: a systemd user service that runs approved tasks within daily,
  per-run and per-task cost caps.
- **Isolated attempts**: one git worktree per attempt, process-group deadlines, and an
  allowlisted environment with no secrets for workers.
- **Independent verification**: the orchestrator runs each task's acceptance commands and
  checks protected paths; worker claims are recorded but never trusted.
- **Automatic integration**: verified work is fast-forwarded, or rebased and re-verified,
  onto `develop`; a conflict sends the task back for another attempt.
- **Preview publishing**: `develop`, and optionally a static preview site, are pushed
  through a GitHub App with short-lived tokens and no administrative rights.
- **Human-approved releases**: `ms release` writes release notes and a changelog entry and
  opens a pull request; merging it is the approval. The system then tags the version,
  creates the GitHub Release and republishes the site.
- **Worker tiers** (optional): tasks start on the cheapest worker model and escalate after
  repeated failures.
- **Operations**: [ntfy](https://ntfy.sh) notifications, plain-language status, run
  reports with token and cost accounting, and a secret-free diagnostic dump (`ms doctor`).
- **Any stack**: test file types, syntax checks and test commands come from each
  project's configuration.
- **Pluggable models**: the orchestrator, planner and workers sit behind adapters
  (DeepSeek, Ollama and the OpenCode CLI are included).

## Architecture overview

```text
 human ──ms chat──▶ Planner model ──approve──▶ tasks + tests committed to develop
                                                   │
 background service (systemd user unit), every N minutes, within cost caps
                                                   ▼
   run (child process) ─▶ autonomous loop ─▶ Master model proposes ONE operation
                                │                    │
                                │          policy + completion gate decide
                                ▼
   task orchestrator ─▶ git worktree per attempt ─▶ coding worker (allowlisted env)
        │                                        ─▶ acceptance verifier
        └─ pass ─▶ auto-integrator: rebase + re-verify ─▶ develop (fast-forward)
   after the run: publisher ─▶ push develop + preview site (GitHub App) ─▶ notification
 human ──ms release──▶ PR develop → main ──human merges──▶ tag, GitHub Release, live site
```

Project state lives in YAML files with a single writer; history lives in SQLite. The full
design, module map and trust boundaries are in **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

## Requirements

| Requirement | Notes |
| --- | --- |
| Linux with systemd user services | The background service is a systemd user unit. |
| Python 3.12+ and [uv](https://docs.astral.sh/uv/) | Dependencies are pinned in `uv.lock`. |
| git, coreutils `timeout`, `openssl` | Worktrees, deadlines, GitHub App JWT signing. |
| A model API key for the Master and planner | DeepSeek by default; Ollama and OpenCode adapters are included. |
| [OpenCode CLI](https://opencode.ai) | The default coding worker. |
| The managed project's own toolchain | Whatever its tests need, e.g. Node.js and Playwright for a web project. |
| A GitHub repository per managed project | For publishing and releases; see [docs/GITHUB-SETUP.md](docs/GITHUB-SETUP.md). |
| [ntfy](https://ntfy.sh) app (optional) | Push notifications. |

## Installation and setup

Paths used throughout the documentation:

| Path | Meaning |
| --- | --- |
| `$MS_HOME` | Where this repository is cloned, e.g. `~/src/master-system` |
| `~/.config/master-system/` | Configuration and secrets, outside the repository |
| `~/.config/master-system/projects/` | Project definitions (private; `[run] projects_root` overrides) |
| `~/.local/share/master-system/` | Runtime state: history, sessions, logs, planner chats |

```bash
git clone https://github.com/f0rrel/master-system.git "$MS_HOME"
cd "$MS_HOME"
uv sync                                   # creates .venv
uv run python -m core.ms install          # installs the `ms` wrapper to ~/.local/bin/ms

mkdir -p ~/.config/master-system && chmod 700 ~/.config/master-system
printf 'DEEPSEEK_API_KEY=%s\n' '<your key>' > ~/.config/master-system/master.env
chmod 600 ~/.config/master-system/master.env

ms notify setup                           # optional: phone notifications
ms service install                        # installs and starts the background service
ms doctor                                 # checks the installation
```

A missing `config.toml` means "use the defaults" (see [Configuration](#configuration)).
Workers use their own home directory as their only credential store; sign the worker in
to its model provider once inside that home, e.g.
`HOME=~/.local/share/master-system-worker opencode auth login`.

The service unit records the absolute path of `$MS_HOME`. After moving the repository,
run `uv sync`, `ms install` and `ms service install` again from the new location.

## Adding a project

Project definitions are private configuration and never live in this repository. A
complete, fictional example is in [`examples/projects/example-app/`](examples/projects/example-app/).

1. **Create a dedicated clone** for the system to work in, separate from any checkout you
   edit by hand, with a `develop` branch:

   ```bash
   git clone https://github.com/<you>/example-app.git ~/managed/example-app
   cd ~/managed/example-app && git switch -c develop && git push -u origin develop
   ```

2. **Describe the project** in `~/.config/master-system/projects/example-app/`:

   ```bash
   cp -r "$MS_HOME/examples/projects/example-app" ~/.config/master-system/projects/
   ```

   Then edit `project.yaml`, and empty `tasks.yaml` (`tasks: []`) and `milestones.yaml`
   (`milestones: []`):

   ```yaml
   id: example-app
   name: Example App
   status: active
   repository: ~/managed/example-app
   base_branch: develop
   auto_integrate: true              # the service may run it and integrate into develop
   github:
     repo: <you>/example-app
     release_branch: main
     site_dir: public                # optional: publish this directory as the preview site
     # site_url: https://app.example.com/   # optional: default https://<you>.github.io/example-app/
   planner:
     test_dir: tests/tasks           # where drafted tests are committed
     test_suffixes: [".test.js"]     # allowed test file endings (any if unset)
     syntax_check: "node --check {path}"           # optional, per drafted test file
     test_command_examples: ["node --test {path}"] # shown to the planner
     # test_guidance: "Use the helpers in tests/helpers."
     setup:                          # run before checks in a fresh worktree
       - npm ci --no-audit --no-fund
     base_checks:                    # must pass on the current code
       - npm test
     protected_paths:                # workers may not change these
       - tests/*
       - package.json
       - package-lock.json
   ```

   For a Python project the planner keys would be, for example,
   `test_suffixes: [".py"]`, `syntax_check: "python -m py_compile {path}"` and
   `test_command_examples: ["python -m pytest -q {path}"]`. Other optional keys:
   `broken_test_markers` (output that means a drafted test is itself broken) and
   `ignore_paths` (directories hidden from the planner's file list).

3. **Connect GitHub**: create the GitHub App, protect `main` and, if the project has a
   `site_dir`, enable Pages, as described in [docs/GITHUB-SETUP.md](docs/GITHUB-SETUP.md).
   Then run `ms github check`.

4. **Plan the first work**: `ms chat example-app`.

A project without `auto_integrate: true` is never run by the service. A project without
`github.site_dir` gets no preview site; only `develop` is pushed.

## Daily workflow

| Step | Action | Command |
| --- | --- | --- |
| 1. Plan | Describe the change, answer the planner's questions, run `check`, then `approve` | `ms chat <project>` |
| 2. Wait | The service runs approved tasks; notifications report batches and blockers | — |
| 3. Review | Open the preview and follow each task's manual-check steps | the preview URL |
| 4. Release | Open the release pull request, review it, merge it on GitHub | `ms release <project>` |
| 5. Monitor | What needs attention, what finished, what is next, today's spend | `ms status` |

The loop is the same for any software project. For a web app or game, the review step is
the deployed preview; for a CLI tool or library, it is the diff on `develop` and the run
report (`ms report`).

## Command reference

| Command | Description |
| --- | --- |
| `ms status [--details]` | Headline per project, items that need you, work finished since you last looked, upcoming work, links and today's spend. Also completes releases merged on GitHub. `--details` lists every task, session and recent event. |
| `ms chat <project> [--new] [--file <path>]` | Talk to the planner. Continues the last open chat unless `--new`; `--file` sends a file as the first message. In-chat commands: `show`, `check`, `approve`, `discard`, `release`, `help`, `quit`. For multi-line input, paste it or put it between two lines containing only `"""`. |
| `ms release <project>` | Commits a changelog entry to `develop` and opens a pull request `develop` → release branch with generated notes. Merging it approves the release. |
| `ms report [<session>]` | A run's report: steps, attempts, verdicts, tokens and cost. Defaults to the latest run. |
| `ms publish <project>` | Push `develop` and the preview site now (the service also does this after every run). |
| `ms pause` / `ms resume` | Start no new work (a running task finishes) / allow new work. |
| `ms stop` | Stop the current run now, keeping its work, and pause. |
| `ms doctor` | A diagnostic block to paste into an issue or an AI chat. Keys, tokens and the notification topic are redacted. |
| `ms notify setup` / `test` / `send "<text>" [--link <url>]` | Create the notification topic, send a test, or send a one-line message. |
| `ms github setup --app-id <id>` / `ms github check` | Record the GitHub App id; check the App, rulesets and Pages. |
| `ms service install` / `uninstall` | Install and start, or remove, the systemd user service. |
| `ms install` | Install the `ms` wrapper to `~/.local/bin/ms`. |
| `ms daemon [--once]` | The service loop itself (run by systemd). |

Every command accepts `--config <path>` before the subcommand. Lower-level tools (start,
resume, integrate, task edits) are available through `python -m core.run_cli --help`.

### Notifications

| Title | Meaning | Action |
| --- | --- | --- |
| `<project>: batch done` | Tasks finished and are in the preview | Review the preview |
| `<project>: run finished` | A run ended without finishing a task | None, unless it repeats |
| `<project>: needs you` | A run made no progress; the project is stalled until a human acts | `ms status`, then revise the task in `ms chat` |
| `<project>: <task-id> needs you` | A task used up its attempt budget | Revise or split the task in `ms chat` |
| `Daily budget reached` | The daily cap was hit; work resumes the next day | None, or raise the cap |
| `<project>: release` | A release was published, or a release pull request was closed | None |
| `Master System service stopped` | The service exited; systemd restarts it | If it repeats: `ms doctor` |

## Configuration

Settings live in `~/.config/master-system/config.toml`. Every key is optional.

| Section | Key | Default | Meaning |
| --- | --- | --- | --- |
| `[master]` | `provider` | `deepseek` | Orchestrating model adapter: `deepseek`, `ollama` or `opencode` |
| | `model` | `deepseek-v4-flash` | Model name |
| | `base_url` | adapter default | API endpoint |
| | `timeout_s` | `180` | Per-call timeout (seconds) |
| `[planner]` | `provider`, `model`, `base_url` | as `[master]` | Planner model |
| | `timeout_s` | `300` | Per-call timeout (seconds) |
| | `chat_usd` | `0.30` | Cost cap per planner chat |
| `[worker]` | `opencode_bin` | `~/.opencode/bin/opencode` | Worker binary |
| | `model` | the worker home's default | Passed to the worker as `--model` |
| | `home` | `~/.local/share/master-system-worker` | The worker's `HOME` and only credential store |
| | `extra_args` | `[]` | Extra worker arguments |
| | `node_min_major` | `22` | Minimum Node.js major version put on the worker `PATH` |
| | `playwright_browsers_path` | `~/.cache/ms-playwright` | Shared Playwright browsers |
| | `ladder` | `[]` | Worker tiers, cheapest first; empty disables tiers |
| `[worker.profiles.<name>]` | `model`, `home` | — | One worker tier |
| `[run]` | `max_steps` | `20` | Master decisions per run |
| | `max_retries` | `1` | Retries after an unusable model reply |
| | `max_attempts_per_task` | `3` | Attempts per task within one run |
| | `attempt_timeout_s` | `1800` | Worker deadline per attempt |
| | `verification_timeout_s` | `1800` | Acceptance deadline per attempt |
| | `projects_root` | `~/.config/master-system/projects` | Location of project definitions |
| `[budget]` | `daily_usd` | `0.50` | Daily cap: Master, planner and paid workers |
| | `run_usd` | `0.20` | Cap per service run |
| `[daemon]` | `interval_s` | `300` | Service polling interval (seconds) |
| | `ntfy_server` | `https://ntfy.sh` | Notification server |
| `[github]` | `app_id` | — | GitHub App id |
| | `key_path` | `~/.config/master-system/github-app.pem` | GitHub App private key |
| `[prices]` | `"<model>" = { input, output, cached_input }` | DeepSeek V4 prices | USD per million tokens, used for caps and reports |

**Caps.** The service stops working on a task after 3 failed attempts since the last
human change to it (2 per tier when tiers are enabled), starts no run once the daily cap
is reached, and bounds each run by `min(run_usd, remaining daily budget)`. After changing
the configuration, restart the service once `ms status` reports Idle:
`systemctl --user restart master-system.service`.

**Secrets** never go in `config.toml`:

| File | Content |
| --- | --- |
| `~/.config/master-system/master.env` (mode 600) | `DEEPSEEK_API_KEY=…` for the Master and planner |
| `~/.config/master-system/github-app.pem` (mode 600) | The GitHub App private key |
| `~/.config/master-system/ntfy-topic` (mode 600) | The private notification topic |
| The worker home | The worker's own, ideally spend-limited, model credential |

Per-project options (`repository`, `base_branch`, `auto_integrate`, `github`, `planner`)
are shown in [Adding a project](#adding-a-project).

## Safety model and trust boundaries

| Boundary | Enforcement |
| --- | --- |
| Models only propose; one component writes project state | `core/master.py` is the single writer; boundary tests |
| Worker output is untrusted | Worker state updates are never applied; raw output is stored as provenance and never shown to the orchestrating model |
| The definition of done is independent of the worker | Acceptance commands and protected paths are set by a human or the approved planner flow and run by the orchestrator's verifier |
| Evidence is bound to the spec | Every attempt carries a `spec_hash`; changing the spec invalidates a pass |
| Isolation | One git worktree per attempt; runtime state outside every workspace |
| No secrets for workers | An allowlisted environment: toolchain `PATH`, a dedicated worker home, no keys |
| Bounded execution | `timeout --kill-after` in a separate process group; one run per project (`flock`) |
| Intent before side effects | Decisions and attempt starts are recorded before they act; interrupted attempts are never replayed |
| The release branch is human-only | The GitHub App has no Administration permission; a ruleset on `main` requires a pull request and allows no bypass |
| Least-privilege publishing | One-hour installation tokens, passed only to the `git push` that needs them |
| Spend | Daily, per-run, per-chat and per-task caps |

**Known limitation:** workers run as the same OS user as the system. Worktrees isolate
files, not authority; an OS-level sandbox is on the roadmap. See
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#safety-rules-and-trust-boundaries).

## Troubleshooting

Start with `ms status`, which states in plain words what needs attention. Symptoms and
fixes are listed in **[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)**. For anything
else, run `ms doctor` and include its output when asking for help.

## Roadmap

| Milestone | Scope | State |
| --- | --- | --- |
| 1. Contain and verify | Worktree per attempt, locking, deadlines, acceptance verifier, spec-bound evidence | Done |
| 2. Run it for real | Run CLI, real worker, reports from history, cost accounting | Done |
| 3. Hands-off | Background service, auto-integration, preview publishing, planner, releases, worker tiers | Done |
| Next | Policy by state transition, out-of-run approvals, unified attempt budgets | Planned |
| 4. Durable runtime | Evaluate DBOS in place of homemade durable execution | Planned |
| 5. Focused context | Per-task digests, project notes, versioned prompts | Partly done |
| Later | OS-level worker sandbox; self-hosting | Planned |

Changes are listed in [CHANGELOG.md](CHANGELOG.md), design decisions in
[docs/DECISIONS.md](docs/DECISIONS.md).

## Development

```bash
uv sync --extra dev
uv run python -m pytest -q        # full offline suite
```

The module map, contribution rules and documentation policy are in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#developing).

## License

[MIT](LICENSE)

---

## Author's setup (personal, not part of the system)

This section describes the author's own installation. It is not needed to use the system.

**Managed projects**

| Project | Preview (`develop`) | Live (`main`) |
| --- | --- | --- |
| [Match Legends](https://github.com/f0rrel/Match_Legends_mobile_game) (mobile web game) | <https://f0rrel.github.io/Match_Legends_mobile_game/develop/> | <https://f0rrel.github.io/Match_Legends_mobile_game/> |

Releases: <https://github.com/f0rrel/Match_Legends_mobile_game/releases>

**Machine notes**

- `$MS_HOME` is `~/AI/master-system` (branch `main`); the service runs from this checkout.
- Project definitions: `~/.config/master-system/projects/` (`match-legends`, and
  `ai-system`, the system's own backlog, which has no `auto_integrate` and is never run).
- Managed clone: `~/AI/managed/match-legends`. Its push URL is disabled; pushes go through
  the GitHub App.
- Daily cap: `$0.60`. Worker: OpenCode's free default model only (tiers off).
- Node.js 22 for workers is installed through nvm; the system Node.js is left untouched.
