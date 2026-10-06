# Reference

Full setup and reference material for Master System: requirements, installation,
project definitions, commands, notifications, configuration, the safety model table and
the roadmap. The [README](../README.md) explains what the system is and how to try it;
[ARCHITECTURE.md](ARCHITECTURE.md) explains how it works.

## Contents

- [Requirements](#requirements)
- [Installation and setup](#installation-and-setup)
- [Adding a project](#adding-a-project)
- [Daily workflow](#daily-workflow)
- [Phone workflow (Telegram)](#phone-workflow-telegram)
- [Command reference](#command-reference)
- [Notifications](#notifications)
- [Configuration](#configuration)
- [Safety model and trust boundaries](#safety-model-and-trust-boundaries)
- [Roadmap](#roadmap)

## Requirements

| Requirement | Notes |
| --- | --- |
| Linux with systemd user services | The background service is a systemd user unit; `ms daemon --once` runs one cycle without it. |
| Python 3.12+ and [uv](https://docs.astral.sh/uv/) | Dependencies are pinned in `uv.lock`. |
| git, coreutils `timeout`, `openssl` | Worktrees, deadlines, GitHub App JWT signing. |
| Node.js ≥ 22, for Node projects | Looked up in `[worker] node_bin`, then [nvm](https://github.com/nvm-sh/nvm)'s install directory, then the worker `PATH` (`path_dirs`, `/usr/local/bin`, `/usr/bin`, `/bin`). Without one, runs and `ms chat` still work: they warn once ("no Node >= 22 for workers; Node-based acceptance commands will fail"), and so does `ms doctor` (`[worker] node_min_major`). |
| [OpenCode CLI](https://opencode.ai) at `~/.opencode/bin/opencode` | The coding worker (`[worker] opencode_bin`). It must be signed in inside the worker home (see below). |
| A DeepSeek API key | The default orchestrating model, planner and visual reviewer use DeepSeek; the key goes in `~/.config/master-system/master.env`. Ollama and OpenCode adapters exist for the orchestrating model and the planner. |
| The managed project's own toolchain, on the worker `PATH` | Worker and verification processes get only the Node `bin` directory found, `[worker] path_dirs` and `/usr/local/bin:/usr/bin:/bin`, and `HOME` is the worker home. Tools in `~/.local/bin` (uv, pipx) are visible only if you add that directory to `path_dirs`, and `python` fails where only `python3` exists. |
| A GitHub repository per managed project (optional) | For publishing and releases; see [GITHUB-SETUP.md](GITHUB-SETUP.md). Without the GitHub App, publishing is skipped with a message. |
| Telegram or the [ntfy](https://ntfy.sh) app (optional) | The phone interface and notifications; see [TELEGRAM-SETUP.md](TELEGRAM-SETUP.md). |

## Installation and setup

Paths used throughout the documentation:

| Path | Meaning |
| --- | --- |
| `$MS_HOME` | Where this repository is cloned, e.g. `~/src/master-system` |
| `~/.config/master-system/` | Configuration and secrets, outside the repository |
| `~/.config/master-system/projects/` | Project definitions (private; `[run] projects_root` overrides). Created by `ms install`. While it is missing, `ms status` and `ms doctor` say so and the service idles instead of exiting |
| `~/.local/share/master-system/` | Runtime state: history, sessions, logs, planner chats |

```bash
export MS_HOME=~/src/master-system
git clone https://github.com/f0rrel/master-system.git "$MS_HOME"
cd "$MS_HOME"
uv sync                                   # creates .venv
uv run python -m core.ms install          # installs ~/.local/bin/ms; creates the projects root (mode 700)

chmod 700 ~/.config/master-system
printf 'DEEPSEEK_API_KEY=%s\n' '<your key>' > ~/.config/master-system/master.env
chmod 600 ~/.config/master-system/master.env

# Sign the worker in to its model provider, inside its own home:
HOME=~/.local/share/master-system-worker ~/.opencode/bin/opencode auth login
```

Then [add a project](#adding-a-project). Install the background service **last**, once a
project definition exists: without a projects root the service exits with "Projects
root not found", and systemd restarts it every 60 seconds (with notifications set up,
each exit also sends a "service stopped" notification).

```bash
ms notify setup                           # optional: ntfy notifications
ms service install                        # installs and starts the background service
ms doctor                                 # checks the installation
```

A missing `config.toml` means "use the defaults" (see [Configuration](#configuration)).
The worker home is the worker's only credential store.

The service unit records the absolute path of `$MS_HOME`. After moving the repository,
run `uv sync`, `ms install` and `ms service install` again from the new location.

## Adding a project

Project definitions are private configuration and never live in this repository. A
complete, fictional example is in [`examples/projects/example-app/`](../examples/projects/example-app/).

1. **Create a dedicated clone** for the system to work in, separate from any checkout you
   edit by hand, with a `develop` branch:

   ```bash
   git clone https://github.com/<you>/example-app.git ~/managed/example-app
   cd ~/managed/example-app && git switch -c develop && git push -u origin develop
   ```

2. **Write the direction** (optional, recommended): `docs/DIRECTION.md` in the project's
   repository on `develop`: vision, audience, the feel every change is judged against,
   and what must never change.

3. **Describe the project** in `~/.config/master-system/projects/example-app/`:

   ```bash
   mkdir -p ~/.config/master-system/projects/example-app
   cp "$MS_HOME"/examples/projects/example-app/*.yaml ~/.config/master-system/projects/example-app/
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

   Optional sections (all project-specific content stays here or in the project's
   repository):

   ```yaml
   direction: {path: docs/DIRECTION.md, max_chars: 6000}
   task_types:                       # narrow what each type may change and use
     visual:
       allowed_paths: ["public/css/*", "public/assets/*", "public/index.html"]
       tools: [read, edit, write, grep, glob, list, bash]
       skill: docs/skills/visual.md  # project skill doc, added to the generic one
     docs:
       allowed_paths: ["*.md", "docs/*"]
   visual_review:
     viewport: [390, 844]
     screens:
       - {name: home, url: index.html}
     capture: "node tests/screens/capture.js {out_dir} {screens} {width} {height}"
   images:
     style: "flat vector illustration, soft shadows, pastel background"
     width: 768
     height: 768
   ```

   For a Python project the planner keys would be, for example,
   `test_suffixes: [".py"]`, `syntax_check: "python3 -m py_compile {path}"` and
   `test_command_examples: ["python3 -m pytest -q {path}"]`; the test runner must be
   installed where the worker `PATH` can see it (see [Requirements](#requirements)).
   Other optional keys: `broken_test_markers` (output that means a drafted test is
   itself broken), `ignore_paths` (directories hidden from the planner's file list) and
   `max_task_lines` (default 150).

4. **Connect GitHub**: create the GitHub App, protect `main` and, if the project has a
   `site_dir`, enable Pages, as described in [GITHUB-SETUP.md](GITHUB-SETUP.md).
   Then run `ms github check`.

5. **Fill the backlog and plan**: `ms backlog example-app add "First epic"`, then
   `ms chat example-app` and "plan epic 1 from the backlog".

A project without `auto_integrate: true` is never run by the service. A project without
`github.site_dir` gets no preview site; only `develop` is pushed.

## Daily workflow

| Step | Action | Command |
| --- | --- | --- |
| 1. Plan | Add epics to the backlog; ask the planner to plan one; answer its questions, run `check`, then `approve` | `ms backlog <project>`, `ms chat <project>` |
| 2. Overnight | The service works through approved tasks in backlog order until the backlog or the daily cap runs out, then sends one summary | — |
| 3. Morning | Read the summary (done, blocked, screenshots, cost, what needs you) | `ms report --summary` |
| 4. Decide | Pick generated images, approve lessons, revise blocked tasks, decide splits | `ms pick …`, `ms lessons <project>`, `ms chat <project>`, `ms split …` |
| 5. Review | Open the preview and follow each task's manual-check steps | the preview URL |
| 6. Release | Open the release pull request, review it, merge it on GitHub | `ms release <project>` |

`ms status` shows the same "needs you" list at any time.

The loop is the same for any software project. For a web app or game, the review step is
the deployed preview; for a CLI tool or library, it is the diff on `develop` and the run
report (`ms report`).

## Phone workflow (Telegram)

After [setting up the bot](TELEGRAM-SETUP.md) (`ms telegram pair`), the whole loop works
from a phone:

| When | In Telegram |
| --- | --- |
| Evening | Send the request as a message or a `.md` file; answer the planner; `/show`, `/check`, `/approve` (confirm) |
| Night | Nothing. Only a worker limit that needs a decision arrives, with **Wait / Free / Paid** buttons |
| Morning | The summary arrives with the preview link and the reviewer's screenshots; `/pick` images, `/lessons`, `/splits` |
| Any time | `/status`, `/spend`, `/backlog`, `/pause`, `/resume` |
| Release | `/release` (confirm) sends the pull request link; merge it on GitHub |

The bot answers only the paired owner and only offers fixed actions: nothing typed is
ever run as a command.

## Command reference

| Command | Description |
| --- | --- |
| `ms status [--details]` | Headline per project, items that need you, work finished since you last looked, upcoming work, links and today's spend. Also completes releases merged on GitHub. `--details` lists every task, session and recent event. |
| `ms chat <project> [--new] [--file <path>]` | Talk to the planner. Continues the last open chat unless `--new`; `--file` sends a file as the first message. In-chat commands: `show`, `check`, `approve`, `approve anyway` (also queues tasks the size check flagged), `discard`, `release`, `help`, `quit`. For multi-line input, paste it or put it between two lines containing only `"""`. |
| `ms release <project>` | Commits a changelog entry to `develop` and opens a pull request `develop` → release branch with generated notes. Merging it approves the release. |
| `ms report [<session>]` | A run's report: steps, attempts, verdicts, tokens and cost. Defaults to the latest run. |
| `ms report --summary` | The latest morning summary, with a link to its HTML page (screenshot thumbnails). |
| `ms backlog <project> [add "<title>" [--summary …] [--priority N] [--id …] \| priority <epic> <N>]` | List the backlog in priority order; add a proposed epic; change an epic's priority (lower runs first). |
| `ms lessons <project> [--approve all\|IDS] [--reject IDS\|rest]` | Review lessons workers proposed; only approved lessons are used. |
| `ms pick <project> <task> <asset> <n>` | Choose one of a task's generated image candidates; it is committed and the task can run. |
| `ms split <project> <task> draft ["guidance"] \| recheck \| approve [anyway] \| reject \| escalate` | Ask the planner to split a too-big task (again: it continues the pending split), re-run its checks without the model, or decide it (also offered in Telegram). Approving replaces the task in place; its dependents wait for all new tasks. |
| `ms reopen <project> <task> [--reason …]` | Put a blocked or open task back in the queue (spec unchanged, recorded); its attempt budget starts over. Also keeps a task whose cancellation the orchestrating model proposed. |
| `ms cancel <project> <task> --reason …` | Cancel an open task (recorded as a human action). Lists tasks that depend on it; they are not changed and cannot start while it is cancelled. The orchestrating model can only propose a cancellation; this is how you accept one. |
| `ms telegram pair\|status\|test\|unpair` | Pair the Telegram bot with your account (one-time code), check it, send a test message, or forget the owner. |
| `ms limit <project> [wait\|free\|paid]` | Show or answer a worker limit: wait for the reset, switch to the next free worker profile, or use the paid one (counts toward the daily cap). |
| `ms publish <project> [--accept-tip]` | Push `develop` and the preview site now (the service also does this after every run). `develop` is pushed only at a tip the system set; `--accept-tip` accepts a change you made outside Master System (recorded as a human action). |
| `ms pause` / `ms resume` | Start no new work (a running task finishes) / allow new work. |
| `ms stop` | Stop the current run now, keeping its work, and pause. |
| `ms doctor` | A diagnostic block to paste into an issue or an AI chat. Keys, tokens and the notification topic are redacted. |
| `ms notify setup` / `test` / `send "<text>" [--link <url>]` | Create the ntfy topic, send a test, or send a one-line message. |
| `ms github setup --app-id <id>` / `ms github check` | Record the GitHub App id; check the App, rulesets and Pages. |
| `ms service install` / `uninstall` | Install and start, or remove, the systemd user service. |
| `ms install` | Install the `ms` wrapper to `~/.local/bin/ms`. |
| `ms daemon [--once]` | The service loop itself (run by systemd). `--once` runs one cycle in the foreground and exits; only projects with `auto_integrate: true` are run. |

Every command accepts `--config <path>` before the subcommand. Lower-level tools are
available through `python -m core.run_cli --help`: `start`, `resume`, `integrate`,
`report`, `status` and `task`.

## Notifications

Notifications go to Telegram when a bot is set up and paired, otherwise (or if a
Telegram send fails) to ntfy. By default the service sends one **Morning summary** when
its work runs out (the backlog is done or waits for you, or the daily cap is reached).
With `[daemon] batch_notifications = true` it also notifies after every run and item:

| Title | Meaning | Action |
| --- | --- | --- |
| `Morning summary` | Done, blocked, screenshots, cost and what needs you since the service started working | `ms report --summary` |
| `<project>: batch done` (batch mode) | Tasks finished and are in the preview | Review the preview |
| `<project>: run finished` (batch mode) | A run ended without finishing a task | None, unless it repeats |
| `<project>: needs you` (batch mode) | A run made no progress, or images wait for your pick | `ms status` |
| `<project>: <task-id> needs you` (batch mode) | A task used up its attempt budget | Revise or split the task in `ms chat` |
| `Daily budget reached` | The daily cap was hit without work to summarise; work resumes the next day | None, or raise the cap |
| `<project>: needs you` (always) | A worker is limited for longer than `max_auto_wait_minutes`, gone, or not set up, or the planner drafted a split of a task the worker could not finish; the project or task waits | `ms limit …` or `ms split …` |
| `<project>: needs you` (always) | `develop` is at a tip the system did not set, so it is not published | Inspect it; `ms publish <project> --accept-tip` if the change is yours |
| `Worker models need you` (always) | The weekly check found a free model that is gone or no longer free | Change `[worker] workers` or the profiles |
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
| `[planner]` | `provider`, `model`, `base_url` | `deepseek`, `deepseek-v4-flash`, adapter default | Planner model (same adapters as `[master]`) |
| | `timeout_s` | `300` | Per-call timeout (seconds) |
| | `chat_usd` | `0.30` | Cost cap per planner chat |
| | `max_output_tokens` | `16000` | Output tokens per planner reply (a whole epic with tests) |
| project `planner.max_task_lines` | | `150` | Lines one task may add or change before `check` flags it |
| `[worker]` | `opencode_bin` | `~/.opencode/bin/opencode` | Worker binary |
| | `model` | the worker home's default | Passed to the worker as `--model` |
| | `home` | `~/.local/share/master-system-worker` | The worker's `HOME` and only credential store |
| | `extra_args` | `[]` | Extra worker arguments |
| | `node_min_major` | `22` | Minimum Node.js major version put on the worker `PATH` |
| | `node_bin` | unset | A directory holding `node`, tried before nvm and the system `PATH`; must exist |
| | `path_dirs` | `[]` | Extra directories on the worker and verification `PATH`, before the system ones (for example `~/.local/bin` for uv); each must exist (`~` is expanded) |
| | `playwright_browsers_path` | `~/.cache/ms-playwright` | Shared Playwright browsers |
| | `ladder` | `[]` | Worker tiers, cheapest first; empty disables tiers |
| | `workers` | `[]` | Worker profiles in order of preference; the first is used (per project: `workers` in `project.yaml`) |
| | `max_auto_wait_minutes` | `120` | A worker limit whose reset is this close is waited for automatically |
| | `unknown_limit_wait_minutes` | `60` | A limit without a known reset is waited for this long, once |
| | `model_check_days` | `7` | How often free worker models are checked |
| | `stall_minutes` | `5` | A worker that changes no file for this long (or is cut off) has stalled: not a failure; 3 stalls in a row wait for you |
| `[worker.profiles.<name>]` | `model`, `home`, `paid`, `label` | — | One worker profile: model, worker home, whether it costs money, a readable name |
| `[run]` | `max_steps` | `20` | Orchestrating-model decisions per run |
| | `max_retries` | `1` | Retries after an unusable model reply |
| | `max_attempts_per_task` | `3` | Attempts per task within one session |
| | `attempt_timeout_s` | `1800` | Worker deadline per attempt |
| | `verification_timeout_s` | `1800` | Acceptance deadline per attempt |
| | `projects_root` | `~/.config/master-system/projects` | Location of project definitions |
| `[budget]` | `daily_usd` | `0.50` | Daily cap: orchestrating model, planner, reviewer and paid workers |
| | `run_usd` | `0.20` | Cap per service run |
| `[daemon]` | `interval_s` | `300` | Service polling interval (seconds) |
| | `ntfy_server` | `https://ntfy.sh` | Notification server |
| | `batch_notifications` | `false` | Notify after every run and item; off: one summary when the work runs out |
| `[reviewer]` | `provider` | `deepseek` | Visual reviewer: `deepseek`, or `none` to switch it off |
| | `model` | `deepseek-flash` | A vision-capable model |
| | `base_url`, `timeout_s` | adapter default, `120` | |
| | `detail` | `high` | Image detail: `high`, or `low` (512 px, cheaper) |
| `[images]` | `providers` | `["pollinations", "cloudflare"]` | Image providers, tried in order |
| | `pollinations_model` | `flux` | Pollinations model |
| | `cloudflare_model` | `@cf/black-forest-labs/flux-1-schnell` | Workers AI model |
| | `timeout_s` | `120` | Per-image timeout |
| `[github]` | `app_id` | — | GitHub App id |
| | `key_path` | `~/.config/master-system/github-app.pem` | GitHub App private key |
| `[prices]` | `"<model>" = { input, output, cached_input }` | DeepSeek prices (peak) | USD per million tokens, used for caps and reports |

**Worker limits.** A worker attempt that hits a provider limit (rate limit, quota, model
gone or no longer free, missing credential) ends as `limited`: not verified, not a
failure, no cost. The reset time is read from the provider's answer when it gives one.
A known reset within `max_auto_wait_minutes` pauses the project until then (plus two
minutes) and the same worker continues; an unknown reset is waited for once
(`unknown_limit_wait_minutes`). Anything longer, and any model that is gone or not set
up, pauses the project and asks: `ms limit <project> wait | free | paid`. Waits and
pending choices appear in `ms status` and the morning summary; other projects keep
working. Example worker order:

```toml
[worker]
workers = ["big-pickle", "space-bunny", "deepseek-flash"]

[worker.profiles.big-pickle]
model = "opencode/big-pickle"
label = "Big Pickle"

[worker.profiles.space-bunny]      # free, zero data retention
model = "opencode/space-bunny-free"
label = "Space Bunny"

[worker.profiles.deepseek-flash]   # paid: needs its own spend-limited key
model = "deepseek/deepseek-flash"
home = "~/.local/share/master-system-worker-paid"
paid = true
label = "DeepSeek Flash"
```

A paid profile needs a credential in its own worker home:
`HOME=~/.local/share/master-system-worker-paid opencode auth login`.

**Visual review cost.** With `deepseek-flash`, a review of three phone screenshots plus
the task and a 6000-character direction is about 6–8k input and 300 output tokens:
about $0.002–0.003 at peak prices, half that off-peak. Reviews count toward the daily cap.

**Caps and attempt limits.** Dollar caps are daily, per run and per planner chat. They
are checked between steps, so a call already in flight can overshoot a cap. Per task
there is no dollar cap but attempt limits: the service stops working on a task after 3
failed attempts since the last human change to it (2 × the number of tiers when tiers
are enabled, at least 3), a session runs a task at most `max_attempts_per_task` times,
and 3 stalls in a row make a task wait for you. The service starts no run once the
daily cap is reached and bounds each run by `min(run_usd, remaining daily budget)`.
After changing the configuration, restart the service once `ms status` reports Idle:
`systemctl --user restart master-system.service`.

**Secrets** never go in `config.toml`:

| File | Content |
| --- | --- |
| `~/.config/master-system/master.env` (mode 600) | `DEEPSEEK_API_KEY=…` for the orchestrating model, planner and reviewer; `TELEGRAM_BOT_TOKEN=…` for the bot; `POLLINATIONS_API_KEY=…` and/or `CLOUDFLARE_ACCOUNT_ID=…`, `CLOUDFLARE_API_TOKEN=…` for images |
| `~/.config/master-system/github-app.pem` (mode 600) | The GitHub App private key |
| `~/.config/master-system/ntfy-topic` (mode 600) | The private notification topic |
| The worker home | The worker's own, ideally spend-limited, model credential |

These files are kept out of the worker's environment, but not out of its reach: workers
run as the same OS user and can read anything that user can (see
[Safety model](#safety-model-and-trust-boundaries)).

Per-project options (`repository`, `base_branch`, `auto_integrate`, `github`, `planner`,
`direction`, `task_types`, `visual_review`, `images`, `workers`) are shown in
[Adding a project](#adding-a-project).

## Safety model and trust boundaries

| Boundary | Enforcement |
| --- | --- |
| Models only propose; one component writes project state | `core/master.py` is the single writer; boundary tests |
| The orchestrating model cannot complete or cancel on its own | Completing needs a verified (and, with `auto_integrate`, integrated) pass; cancelling always waits for the owner (`ms cancel` / `ms reopen`) |
| Worker output is untrusted | Worker state updates are never applied; raw output is stored as provenance and never shown to the orchestrating model |
| The definition of done is independent of the worker | Acceptance commands and protected paths are set by a human or the approved planner flow and run by the orchestrator's verifier |
| Evidence is bound to the spec | Every attempt carries a `spec_hash` (title, acceptance, description, assets); changing the spec invalidates a pass |
| A separate working tree per attempt (not a sandbox) | One git worktree per attempt; runtime state outside every workspace. The worktree shares refs and the `.git` directory with the dedicated clone, so it isolates files, not authority (gap N1) |
| Verification sees only the committed result | Acceptance runs in a fresh worktree of the result commit, so uncommitted or ignored files cannot change the verdict |
| The shared repository is checked around the worker (detection and restoration, not a sandbox) | Refs and the clone's git `config`, `hooks/` and `info/` are recorded before the worker and restored before its result is committed; changes beyond its own branch, the stash, remote-tracking refs and the git identity fail the attempt (`repository_tampered`) |
| Control-plane git runs no repository code | Hooks and fsmonitor are off for every git command the control plane runs |
| Only system-set tips are published | `develop` is pushed only at a tip the system set (`refs/ms-system/heads/develop`); `ms publish --accept-tip` accepts a deliberate change |
| No secrets in the worker's environment | An allowlisted environment: toolchain `PATH`, a dedicated worker home, no keys. Workers still run as the same OS user and can read that user's files, including `master.env` and `github-app.pem` |
| Bounded execution | `timeout --kill-after` in a separate process group; one run per project (`flock`) |
| Intent before side effects | Decisions and attempt starts are recorded before they act; interrupted attempts are never replayed |
| The release branch is human-only | The system's code never merges to `main`, and the ruleset from [GITHUB-SETUP.md](GITHUB-SETUP.md) blocks direct pushes (pull request required, no bypass); the GitHub App has no Administration permission |
| One phone owner, fixed actions | The Telegram bot answers only the user paired with a one-time code, offers fixed operations behind single-use buttons, asks to confirm approvals, releases and limit choices, and has no payment actions |
| Least-privilege publishing | One-hour installation tokens, passed only to the `git push` that needs them |
| Work stays within its type | A task's type freezes `allowed_paths` into its acceptance, and the verifier fails changes elsewhere. Tools are limited through a per-attempt OpenCode config, but `bash` is always enabled, so tool limits are not a security boundary; path limits are |
| The direction is not the worker's to change | The direction document is a protected path of every planned task |
| Reviewers can only say no | The visual reviewer can turn a pass into a fail, never the reverse; an unavailable reviewer changes nothing |
| Nothing unapproved is remembered | Lessons from workers are used only after the owner approves them |
| Image keys stay with the orchestrator | Images are generated by the service, not by workers; a multi-candidate image is committed only after the owner's pick |
| Limits are not failures | A `limited` attempt is never verified or counted; switching to another worker, and any paid worker, needs the owner's choice |
| Spend | Daily, per-run and per-chat dollar caps, checked between steps; per-task attempt limits; reviews count toward the daily cap |
| History | SQLite, append-only through its API and the source of every report; not tamper-evident, and direct YAML edits are not recorded (gap M4) |

**Known limitation:** workers run as the same OS user as the system. Worktrees isolate
files, not authority; an OS-level sandbox is on the roadmap. See
[ARCHITECTURE.md](ARCHITECTURE.md#safety-rules-and-trust-boundaries) and
[Known gaps](ARCHITECTURE.md#known-gaps).

## Roadmap

| Milestone | Scope | State |
| --- | --- | --- |
| 1. Contain and verify | Worktree per attempt, locking, deadlines, acceptance verifier, spec-bound evidence | Done |
| 2. Run it for real | Run CLI, real worker, reports from history, cost accounting | Done |
| 3. Hands-off | Background service, auto-integration, preview publishing, planner, releases, worker tiers | Done |
| 4. From direction to overnight work | Project direction, backlog, task types, shared lessons, visual reviewer, generated images, morning summary, whole-epic planning | Done |
| Next | Policy by state transition, out-of-run approvals, unified attempt budgets | Planned |
| 5. Durable runtime | Evaluate DBOS in place of homemade durable execution | Planned |
| 6. Focused context | Per-task digests, versioned prompts | Partly done (direction, lessons) |
| Later | OS-level worker sandbox; self-hosting | Planned |

Changes are listed in [CHANGELOG.md](../CHANGELOG.md), design decisions in
[DECISIONS.md](DECISIONS.md).
