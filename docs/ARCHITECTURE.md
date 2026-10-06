# Architecture

How Master System works, for developers and AI assistants working on the code. For
installation and usage, see the [README](../README.md).

## Components

```text
 human ──ms chat──▶ Planner (direction, backlog) ──approve──▶ typed tasks + tests on develop
                                           │
 systemd user service: ms daemon (core/daemon.py), every interval_s, within caps
   AssetStep: images for visual tasks (commit, or wait for ms pick)
                                           ▼
   run_cli (child process) ─▶ AutonomousLoop ─▶ Master model proposes ONE operation
                                   │                 │ (backlog order, direction)
                                   │        policy + completion gate decide
                                   ▼
   TaskOrchestrator ─▶ git worktree per attempt ─▶ worker (OpenCode CLI, allowlisted env,
        │                                          type's tools, briefing)
        │                                         ─▶ AcceptanceVerifier (frozen acceptance,
        │                                            allowed paths) ─▶ VisualReviewVerifier
        ├─ pass ─▶ lessons ─▶ pending list (owner approves)
        └─ pass ─▶ AutoIntegrator: rebase + re-verify ─▶ develop (fast-forward)
   after the run: Publisher ─▶ push develop + gh-pages (GitHub App)
   work runs out: morning summary (core/summary.py) ─▶ one ntfy notification
 human ──ms release──▶ PR develop→main ──human merges──▶ tag, GitHub Release, live site
```

- **Master** (`core/master.py`) is the only writer of project state (the YAML files).
- **The reasoning Master** is a model that proposes one operation per step:
  - `update_task` / `create_task` (state);
  - `run_task` (dispatch; carries no worker, model, workspace or prompt);
  - `integrate_attempt` (human only).
- **Policy and the completion gate** decide whether a proposal runs. A task completes only
  if its latest attempt finished, was verified `pass` against the current spec and, in
  projects with `auto_integrate: true`, is integrated into the base branch. A proposal to
  cancel a task is always held for the owner (`cancellation_requires_human`): the service
  skips that task until `ms reopen` (keep it) or `ms cancel` (accept), and `ms status`
  lists it with the model's reason.
- **History** (`core/sqlite_history.py`, SQLite, append-only, schema v4) records every
  decision, attempt, verification, integration, human action, publish and release.
  Reports are built from history alone.
- **Workers** never touch project state. Their claims (status, summary, usage) are recorded
  as claims; what counts is the verifier's verdict on the committed result.

## Module map (`core/`)

| Area | Modules |
| --- | --- |
| State | `master.py`, `work_manager.py`, `project_state.py`, `project_manager.py`, `human_edits.py`, `backlog.py` (epic order), `lessons.py` (shared project memory) |
| Project guidance | `direction.py` (the direction document), `task_types.py` (types, skills, paths, tools); generic skill docs in `$MS_HOME/skills/` |
| Loop | `autonomous_loop.py`, `reasoning.py` (operations, policy), `reasoning_engine.py`, `evidence.py` (completion gate, attempt facts), `session_runner.py`, `work_session.py`, `session_store.py`, `recovery.py`, `run_lock.py` |
| Execution | `task_orchestrator.py`, `workspace.py` (worktrees, git), `worker_process.py`, `worker_env.py`, `opencode_backend.py`, `ollama_backend.py` (frozen), `worker_tiers.py`, `worker_limits.py`, `worker_models.py`, `acceptance_verifier.py`, `visual_review.py`, `images.py`, `verification.py`, `execution.py`, `execution_runner.py` |
| Integration and release | `attempts.py` (integrate), `auto_integrate.py`, `publish.py`, `github.py` (App auth, push), `release.py` |
| Models | `provider.py` (interface), `deepseek_provider.py` (also `DeepSeekVision` for the reviewer), `ollama_provider.py`, `opencode_provider.py`, `usage.py` |
| Operator tools | `ms.py` (the `ms` command), `daemon.py`, `summary.py` (morning summary), `host.py` (service processes), `notify.py`, `planner.py`, `planner_checks.py`, `report.py`, `run_cli.py` (lower-level CLI), `run_config.py`, `paths.py` |
| History | `history.py` (event types, interface), `sqlite_history.py` (the only module that imports `sqlite3`) |

## Files and directories

| Location | Content |
| --- | --- |
| `$MS_HOME` | This repository. The service runs from it. |
| `~/.config/master-system/projects/<id>/` | Project definitions and state: `project.yaml`, `milestones.yaml`, `tasks.yaml`, `lessons.yaml`. Runs update `tasks.yaml`. Private; never in this repository. `[run] projects_root` (or `--root` for the lower-level CLIs) points elsewhere. |
| `$MS_HOME/examples/projects/` | A fictional example project definition |
| `$MS_HOME/docs/` | This document, troubleshooting, the decision log, the GitHub setup guide |
| `$MS_HOME/archive/` | Early experiments and historical milestone plans; unused by the code |
| `~/.config/master-system/` | `config.toml` (no secrets); `master.env` (model API key, mode 600); `github-app.pem` (600); `ntfy-topic` (600) |
| `~/.local/share/master-system/` | `history.sqlite` (and `.bak-*` migration backups); `sessions/`; `logs/` (worker and verification logs; `runs/` per service run); `planner/` (chats); `limits/<project>.json` (worker limits); `worker-models.json` (weekly check); `artifacts/<project>/<task>/<result>/` (review screenshots); `assets/<project>/<task>/<asset>/` (image candidates, contact sheet); `reports/` (morning summaries, `latest.html`); `daemon-state.json`; `paused` (flag); `run.pid`; `last-looked`; `release-check`; `git-askpass.sh` (contains no secret) |
| `~/.local/share/master-system-worktrees/<project>/` | One git worktree per attempt, rebase or planner check, kept for inspection |
| `~/.local/share/master-system-worker/` | The worker's home and its only credential store |
| A dedicated clone per managed project | The repository the system works in (`repository` in `project.yaml`). Its push URL should be disabled; pushes go through the GitHub App. `refs/ms-release/<branch>` records the remote release branch at the last check. |
| `~/.config/systemd/user/master-system.service` | The service unit. Logs: `journalctl --user -u master-system.service` |

All XDG locations honour `$XDG_CONFIG_HOME` and `$XDG_DATA_HOME`.

## The service (`core/daemon.py`)

Every `[daemon] interval_s` seconds, unless paused or over the daily cap, the service:

1. completes releases that were merged on GitHub;
2. for each idle project with `auto_integrate: true`, prepares images for visual tasks
   (`core.images.AssetStep`): generates missing ones, commits single images, and leaves
   multi-candidate images waiting for the owner's pick;
3. if the project has work, starts one run (`core.run_cli start … --until-stopped`) in a
   child process, capped at `min(run_usd, remaining daily budget)`;
4. after the run, publishes; with `batch_notifications`, it also notifies;
5. when it has worked and then finds no work (or reaches the daily cap), it writes the
   morning summary (`core.summary`) and sends one notification.

A task counts as work when it:

- is `planned` with all dependencies done, or is `in_progress`;
- has acceptance commands;
- has fewer than 3 failed attempts since the last human action (2 × ladder length when
  worker tiers are enabled);
- does not wait for images.

Work is taken in **backlog order**: epic `priority` (lower first; none = last), then the
epic's position in `milestones.yaml`, then the task's position in `tasks.yaml`
(`core.backlog`). The Master sees `ready_tasks` in that order and is told to follow it;
the order is guidance, not a policy refusal. Epics with status `proposed` are unapproved
backlog entries: they have no tasks, and only `ms backlog` or an approved planner draft
changes them.

A run that makes no progress marks the project **stalled** until a human acts on it.

## Planner (`core/planner.py`, `core/planner_checks.py`)

`ms chat <project>` opens a chat with the planner model. The planner may read files from
the project's base branch (read-only, through git) and returns structured JSON: questions,
files to read, or a draft. A draft is an epic with up to 12 tasks, and/or `backlog`
epics to add unplanned. Each task has a title, type, size, description, manual-check
steps, test files and test commands; visual tasks may add `assets` and review `screens`.
The planner's context holds the direction, the backlog (numbered by priority), the file
list, the README and the existing tasks. "Plan epic N" drafts all tasks of backlog epic
N, whose id the draft reuses; approving it turns the epic from `proposed` into `planned`
and keeps its priority.

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

A draft with only backlog epics needs no check. `approve` repeats the checks under the
project lock, adds any backlog epics, commits the tests to the base branch,
and writes the epic and tasks atomically through `Master.add_planned_work` (human-only),
recording a `human_action` per task with the draft's hash. Acceptance edits therefore stay
human-approved.

## Task size, cut-offs and splits (`core/task_size.py`, `core/splits.py`)

**Size.** The planner's rules: one new thing per task (one module piece, one screen, one
asset group), about `max_task_lines` (150) at most, bigger work as an ordered sequence
with `depends_on`, each task's tests covering only its part, and an `estimate_lines`
per task. `check` adds `size` findings (estimate missing or too large, more than three
items created at once, more than three files) with a suggested split; `approve`
refuses oversized drafts unless `approve anyway` (recorded as `size_override`).

**Cut-offs.** A stalled attempt with `finish_reason: length` means the model spent its
output on thinking. The next attempt's briefing carries a write-incrementally
instruction (`recovery_note`). After a second cut-off since the last human action,
`SplitStep` (a service step) has the planner draft a split: a draft with
`replaces: <task>` in the task's own epic. It runs the draft's checks and notifies the
owner with Approve / Reject (Escalate when a worker ladder exists). The task is
`waiting` meanwhile (the service skips it, the Master sees `waiting_for_owner`).
Approving calls `Master.split_task`: the task becomes `cancelled` with `replaced_by`,
the new tasks take its place, and its dependents depend on all of them. Reject blocks
the task; Escalate sets its size to `hard` (the top tier).

**Fresh starts.** The Master's evidence for a task covers only attempts after its last
human action (reopen, edit, pick, split decision); earlier ones are counted as
`attempts_before_last_human_change`. Without this, a reopened task was blocked again
from its old failures.

## Direction, task types and lessons

**Direction** (`core/direction.py`): `docs/DIRECTION.md` (configurable) is read from the
base branch with a size budget (`max_chars`, default 6000) and given to the planner, the
Master (`context["direction"]`, injected by the loop), the worker briefing and the
visual reviewer. Planner and Master rules: judge every proposal against it; ask when a
request conflicts. It is a protected path of every planned task.

**Task types** (`core/task_types.py`): `developer`, `visual`, `logic`, `docs`. For each,
`project.yaml` → `task_types` may set `allowed_paths`, `tools` and a project `skill`
doc. On approval the type's paths are frozen into `acceptance.allowed_paths` (part of
the spec hash); the acceptance verifier fails any change outside them
(`outside_allowed_paths`). Each attempt of a typed task gets
`OPENCODE_CONFIG_CONTENT` disabling, and denying in `permission`, every tool not
allowed. `attempt_started` records `task_type`, `allowed_tools` and `lessons_used`.

**Briefing** (`TaskOrchestrator._briefing`): the worker prompt carries the type, the
generic plus project skill doc, the direction and the approved lessons for the type,
and invites up to three `LESSON:` lines.

**Lessons** (`core/lessons.py`): after a verified pass, `LESSON:` lines from the worker's
reply are added to `lessons.yaml` → `pending` (bounded, deduplicated). `ms lessons`
approves or rejects them; only `approved` lessons of the task's type (or `all`) are
used, newest first, within 2000 characters.

## Visual review and images

**Visual reviewer** (`core/visual_review.py`): wraps the acceptance verifier. For a
`visual` task whose acceptance passed (not on rebase re-verification), it captures the
configured screens in the attempt worktree with the worker environment (the project's
`capture` command, or `npx playwright screenshot` over `file://`), stores them under
`artifacts/`, and asks the vision model (`[reviewer]`, default DeepSeek `deepseek-flash`)
for `{verdict: ok|block, readability, change_visible, fits_style, notes}`. A block turns
`pass` into `fail` with a `visual_review` finding, so the next attempt sees the notes; a
failed capture or an unusable answer is recorded and changes nothing. The review's
usage and cost are part of the verification evidence and count toward the daily cap.

**Images** (`core/images.py`): a visual task may declare `assets` (name, prompt, path,
candidates; part of the spec hash, and protected paths of the task). The service
generates missing images with `images.style` in front of each prompt, through
Pollinations with Cloudflare Workers AI as fallback (keys from `master.env`; workers
never see them). One candidate is committed to the base branch as a system commit;
several are stored with a contact sheet and the task waits: the daemon skips it, the
Master sees `readiness: waiting_for_owner`, and `TaskOrchestrator.prepare` refuses it.
`ms pick` commits the chosen candidate under the project lock and records a human action.

## Worker profiles and limits (`core/worker_limits.py`, `core/worker_models.py`)

**Profiles.** `[worker.profiles.<name>]` define workers (`model`, `home`, `paid`,
`label`). `[worker] workers` (or `workers` in `project.yaml`) orders them; the
orchestrator uses the first, unless the owner chose another after a limit
(`LimitState.override`), or a tier ladder is configured. Every attempt records
`worker_profile`, `worker_model`, `worker_paid` and `worker_choice` (`order` or `owner`).

**Limits.** The OpenCode backend classifies a failed run (`classify_failure`):
`rate_limited`, `model_unavailable` (including HTTP 402), `not_configured`, or
`provider_error` (an error event before the first worker step). It reads the reset time
(`parse_reset`: `Retry-After`, `x-ratelimit-reset`, "try again in …", "resets at …").
The attempt ends `limited`: no verification; `failed_attempts_since_human` and the
session attempt limit skip it. `LimitState.record_limit` decides:

| Situation | Phase | Effect |
| --- | --- | --- |
| Known reset within `max_auto_wait_minutes` | `auto_wait` | Paused until the reset + 2 min; then the same worker |
| Unknown reset, first time | `auto_wait` | Paused `unknown_limit_wait_minutes` |
| Longer, still limited, model gone, not configured | `needs_choice` | Paused; one immediate notification |
| Owner chose `wait` | `chosen_wait` | Paused until the reset (or hourly); not asked again |
| Owner chose `free` / `paid` | — | `override` profile until the limited worker's reset (or 24 h) |

A run stops as soon as its project is paused (`run_cli.limit_check`, part of the stop
check); the service skips paused projects and holds the morning summary while an
automatic wait is pending. `record_success` clears the limit when the worker works again.

**Stalled workers.** An attempt whose worker ends without changing any file is
`stalled` (not verified, not a failure) when the model was cut off (`finish_reason:
length`, typically its whole output budget spent on thinking) or it ran for at least
`[worker] stall_minutes`. The record keeps an excerpt: the reason, minutes, reasoning
tokens and the last tool calls (`ms report` shows it). Three stalls since the last
human action make the task wait for the owner. Workers are told to write files early
and in pieces, so a long plan is not lost to the output limit.

**Tools every type keeps.** OpenCode's free tier answers HTTP 403 ("can only be used
from within OpenCode") when the `bash` tool is disabled, so every type keeps `bash`;
path limits (`allowed_paths`, enforced by the verifier) keep each type to its files.

**Weekly model check.** `FreeModelCheck` runs `opencode models <provider> --verbose
--refresh` in each free profile's home every `model_check_days` and notifies the owner
when a model is no longer offered, no longer active, or no longer free.

## Telegram (`core/telegram.py`, `core/telegram_bot.py`)

`ms daemon` starts a thread that long-polls `getUpdates` (30 s) when
`TELEGRAM_BOT_TOKEN` is set; there is no webhook and no listening socket. The client is
stdlib `urllib`; every error has the token replaced by `<token>`.

- **State** (`<state dir>/telegram.json`, 600): the paired user and chat, the pairing
  code's hash and expiry (15 minutes, single use), the update offset, the active
  project, and pending button actions (random ids, 48 hours, single use).
- **Security:** updates from anyone but the paired user are dropped without an answer;
  the only unpaired input handled is `/pair <code>`. Callback data is `a:<id>`, mapping
  to an action the bot stored itself, so a forged button cannot name an operation.
- **Operations** (`BotOps`): fixed methods. Most call the `ms` commands in-process with
  `--actor telegram`, so behaviour and history records match the terminal; the others
  record a `human_action` with `actor: telegram` themselves (system-wide actions under
  project `_service`). Planner messages and `.md`/`.txt` files go to `PlannerChat.turn`.
  Approve, release and limit choices go through a confirmation button.
- **Replies:** HTML, split at 4096 characters on paragraph boundaries; long reports and
  drafts are sent as documents.
- **Notifications:** `build_notifier` returns a `FallbackNotifier` (Telegram first, ntfy
  second). Notifications may carry button actions (worker limits) and photos (the morning
  summary's screenshots); ntfy ignores both.

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
| `main` (release branch) | Only a human, by approving and merging a release pull request (ruleset: PR with one approval required, no force push, no deletion, no bypass) |
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
| Verification sees only the committed result | The verifier runs in a fresh worktree of `result_sha` (`<attempt>-verify`, named in the `verification` event), never in the worker's; uncommitted and ignored files are not there |
| The shared repository is checked around every worker run | `workspace.record_repository` / `restore_repository`: every ref, the worktree's `HEAD`, and the common git dir's `config`, `hooks/` and `info/` are recorded before the worker starts and put back before the snapshot. Changes beyond the attempt's own branch, `refs/stash`, `refs/remotes/*` and `user.name`/`user.email` fail the attempt (`repository_tampered`, without running acceptance). This is detection and restoration, not a sandbox: it does not stop a worker acting outside git (N1) |
| Control-plane git runs no repository code | Every git command the control plane starts goes through `workspace.control_git_argv` (`-c core.hooksPath=/dev/null -c core.fsmonitor=false`) and `control_git_env` (a minimal environment without keys, `GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_NOSYSTEM=1`); a boundary test keeps call sites on the helper. Ref writers outside a run (release check, `ms publish`, release fetch) take the project lock |
| Only system-set tips are published | `workspace.fast_forward` records each new tip in `refs/ms-system/heads/<branch>` when the branch moved from the recorded tip; the publisher and release preparation refuse to push a `develop` that differs (the owner accepts with `ms publish <project> --accept-tip`) |
| Evidence is bound to the spec | `spec_hash` on every attempt; a changed spec invalidates a pass |
| Intent before side effects; no automatic replay | `decision` / `attempt_started` are written first; unfinished attempts become `interrupted` |
| One run per project | Non-blocking `flock`, inherited by workers, never unlocked early |
| Deadlines | Workers run under `timeout --kill-after` in their own process group; leftovers in the group are killed |
| No secrets for workers | Allowlisted environment (`core/worker_env.py`): toolchain on `PATH`, the worker home, no keys; secret-looking extras are refused |
| The release branch changes only by a human merge | The App has no Administration permission; a ruleset on `main` requires a PR with one approval and no bypass (the App opens the PR, so it cannot approve it) |
| A task stays within its type | `acceptance.allowed_paths` (verifier) and a per-attempt OpenCode tool config |
| Reviewers and memory cannot widen authority | The visual reviewer can only block; lessons are used only after approval; image keys never reach workers |
| Provider limits are not failures, and money is not spent without the owner | `limited` attempts are excluded from every budget; switching workers or using a paid one is the owner's choice |
| Only `worker_process.py`, `workspace.py`, `attempts.py` and `host.py` start processes | `tests/test_backend_boundaries.py` |
| No provider or model names above the adapters | Boundary tests |
| Spend | Daily, per-run, per-chat and per-task caps |

## Known gaps

| ID | Gap | Plan |
| --- | --- | --- |
| N1 | Workers run as the system's OS user: isolation, not a sandbox. A worker can read the owner's files (`master.env`, the GitHub App key), write outside its worktree (for example `~/.gitconfig`, the state directory, the dedicated clone's working files) and leave processes behind (`setsid`). Everything below that says "detected" assumes the worker stays inside git | Containers or an OS sandbox |
| H2 | Policy is keyed on operation names, not state transitions. Partly addressed: completing needs evidence and cancelling is held for the owner; other transitions (`blocked`, `in_progress`, `planned`) and retitling are still routine for the orchestrating model | A transition table |
| H3 | Approvals for gated operations cannot be granted outside a run | Approval events and `ms approve` |
| H4 | The service's per-task failure budget and the in-run attempt limit are separate | Unify |
| M4 | Edits made directly in YAML are not recorded (`ms chat` and `run_cli task …` edits are) | Record every write with an actor |
| H6 | Homemade durable execution | DBOS evaluation (milestone 5) |
| L6 | Old attempt worktrees and branches accumulate | Periodic cleanup |
| B1 | Backlog order is guidance for the Master, not enforced by policy | Enforce with transition policy (H2) if it is ignored |
| T2 | Type isolation is only as fine as the project's file layout (a single-file app cannot separate visual from logic work by path) | Split such files; tool limits still apply |
| S1 | Screenshots and the summary page are local files, not viewable from a phone | Optionally publish them with the preview |
| V1 | Closed. Verification ran in the worker's worktree, where an uncommitted file ignored through `.gitignore` or `info/exclude` could change how acceptance behaved. It now runs in a fresh worktree of the result commit | — |
| V2 | Narrowed. Attempt worktrees still share refs, objects and the common git directory with the dedicated clone. Changes to refs, `config`, `hooks/` and `info/` are undone after the worker returns and fail the attempt, but they are not prevented while it runs, other files in the common git dir (`objects/`, other worktrees' metadata) are not checked, and a process that outlives the attempt (N1) can change them later. The publisher no longer pushes a `develop` the system did not set | Separate repositories per attempt, with the OS sandbox |
| G1 | Closed. Control-plane git ran the repository's hooks and fsmonitor, the filters and other commands defined in the repository's or the owner's global git config (a worker can write both), with the control plane's environment, which holds the keys from `master.env` during a run. Now every control-plane git command runs without hooks or fsmonitor, with a minimal environment (`PATH`, `HOME`, locale, `GIT_TERMINAL_PROMPT=0`, plus only what that call needs) and without global or system config; the repository config is restored before the snapshot. What remains is N1: between runs, a process a worker left behind could change the repository config | — |

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
4. This document, the README and [REFERENCE.md](REFERENCE.md), when behaviour, commands or
   configuration change.

Documentation stays project-agnostic. Information about a specific managed project lives
in that project's `project.yaml` and repository; the README mentions the author's real
project only in its "Used in practice" section, and the author's machine setup is not
documented here. Reference material (setup, commands, configuration) lives in
[REFERENCE.md](REFERENCE.md).
