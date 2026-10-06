# Changelog

Notable changes to Master System, newest first. Versions correspond to git tags.
Rationale for each change is in [docs/DECISIONS.md](docs/DECISIONS.md).

## Unreleased

### Lessons: no silent drops, and evidence per lesson
- `ms lessons` lists approved lessons with whether each fits in the worker prompt for its
  type, how many attempts used it and how many of those passed, and marks "never in a
  passing attempt" after 5 attempts without a pass (a suggestion; nothing changes
  automatically). It ends with "N approved lessons don't fit in the 2,000-character budget
  and aren't used; reject or shorten some" when lessons are dropped; the budget is fixed.
- `ms status` shows that line under "Needs you" only while lessons are dropped.
- `attempt_started` records `lessons_used_ids` next to the `lessons_used` count. Older
  attempts have no ids and are not counted.
- `ms lessons --reject` accepts approved lesson ids, to retire a lesson.

### Upgrading an existing installation
- No migration: the history schema stays v4, and events from before the upgrade read as
  before (no held cancellations, no tampering findings).
- The publish guard trusts the current `develop` the first time it checks (the first
  publish or integration after the upgrade) and records it in
  `refs/ms-system/heads/develop`. To record it yourself right after restarting, run
  `ms publish <project> --accept-tip` (recorded as a human action).
- Edit the `main` ruleset to require one approval (docs/GITHUB-SETUP.md, step 6).
- Control-plane git no longer uses your `~/.gitconfig`: release fetches are anonymous.

### Project hygiene
- Continuous integration: `.github/workflows/tests.yml` runs the offline suite on
  Python 3.12 and 3.13 with Node 22 (exposed in nvm's layout, as workers find it);
  README badge.
- `pyproject.toml`: the package is named `master-system`, described with the README's
  tagline (`uv.lock` updated; no dependency changed).
- `docs/GITHUB-SETUP.md` recommends 1 required approval on `main` (was 0): the App opens
  the release pull request and cannot approve it, but with 0 approvals its token could
  merge it through the API. Existing installations: edit the `main` ruleset.
- The image providers' default models come from `core/run_config.py` (Pollinations
  `flux`; the adapter defaulted to `zimage`). The service always passed the configured
  model, so its behaviour is unchanged.
- Issue templates for bypasses and setup problems, asking for the fields in the README's
  "useful feedback" list, and links to the README sections and troubleshooting.

### The first ten minutes
- Without a projects root, `ms status` and `ms doctor` print a one-line hint instead of a
  traceback, and the service idles and logs the hint instead of exiting (systemd no
  longer restarts it every minute). `ms install` creates
  `~/.config/master-system/projects` (mode 700).
- Node is optional for workers: it is looked up in a new `[worker] node_bin`, then nvm,
  then the worker `PATH`. Without one, runs and `ms chat` warn once ("no Node >= N for
  workers; Node-based acceptance commands will fail") instead of refusing to start, and
  `ms doctor` says so.
- New `[worker] path_dirs`: extra directories (for example where uv is installed) on the
  worker and verification `PATH`.
- `examples/quickstart/`: a tiny zero-dependency Node project (`node --test`), its
  project definition, and `init.sh <target-dir>`, which creates the dedicated repository
  on `develop` and prints the next commands. The README's "Try it" uses it.
- Fixed: `ms doctor` printed "(no report: 'Namespace' object has no attribute 'summary')"
  instead of the last run report (or "No sessions yet.").

### Orchestrator scope
- The orchestrating model can no longer cancel a task: a proposal to set `cancelled` is
  held for the owner (`cancellation_requires_human`), like a completion without evidence.
  `ms status` lists it with the model's reason; the service skips that task (others keep
  running) until the owner decides. Other status changes are unchanged.
- New `ms cancel <project> <task> --reason "…"`: cancel an open task as a recorded human
  action; it names the tasks that depend on it.

### Hardening: independent verification
- Verification runs in a fresh worktree of the attempt's committed result
  (`<attempt>-verify`, named in the `verification` event), so files a worker leaves
  uncommitted or ignored (`.gitignore`, `info/exclude`) cannot change the verdict
  (gap V1 closed). Acceptance must build what it needs from committed files.
- The repository is checked around every worker run: refs, the worktree's `HEAD`, and
  the common git dir's `config`, `hooks/` and `info/` are restored before the snapshot.
  Changes beyond the attempt's branch, the stash, remote-tracking refs and the git
  identity fail the attempt with a `repository_tampered` finding, shown in `ms report`
  (gap V2 narrowed). Detection and restoration, not a sandbox.
- Control-plane git runs without repository hooks or fsmonitor, with a minimal
  environment (no `master.env` keys) and without the global or system git config, so a
  filter planted in `~/.gitconfig` no longer runs during the snapshot (gap G1 closed).
  It no longer uses your git credential helper: release fetches are anonymous.
- Publish guard: develop is pushed (by the publisher and by release preparation) only
  at a tip the system set; otherwise "needs you". `ms publish <project> --accept-tip`
  accepts a deliberate change.
- The release check in `ms status`, `ms publish` and the release fetch take the project
  lock and wait while a run is in progress.

### Documentation
- README rewritten for visitors, engineers and external testers: what it is, what makes
  it different, how a task flows, a worked example, the trust model, a local trial, a
  "Try to break it" guide, real use, status and code entry points. Reference material
  (requirements, setup, project definitions, commands, notifications, configuration,
  the safety table, the roadmap) moved to `docs/REFERENCE.md`, with fresh-install fixes
  and corrected statements. Gaps V1 and V2 added to `docs/ARCHITECTURE.md`.

### Right-sized tasks
- Fixed: a reopened task could be blocked again at once, because the Master still saw
  its attempts from before the reopen. The Master now sees only attempts since the
  task's last human action.
- Planner sizing rules (one new thing, about 150 lines, ordered splits, `estimate_lines`);
  `check` flags oversized tasks with a suggested split; `approve anyway` overrides.
- After a cut-off the next attempt writes incrementally; after a second, the planner
  drafts a split, sent with Approve / Reject (/ Escalate) buttons. `ms split` and
  `/splits` decide it. Approving replaces the task in place and rewires its dependents.

### Stalled workers
- Fixed: a task creating a large new file (six SVG drawings) failed three times without
  changing anything: the free model spent its whole 32,000-token output budget on
  thinking and was cut off before writing. Workers are now told to write early and in
  pieces, and such attempts are `stalled` (not failures) with an excerpt in `ms report`;
  three stalls in a row wait for the owner.
- Fixed: the `docs` task type disabled `bash`, which OpenCode's free tier refuses
  (HTTP 403); every type now keeps `bash`, and paths still limit what a type may change.
- `ms reopen <project> <task>` puts a blocked task back in the queue, recorded.

### Telegram bot
- A Telegram bot is the phone interface: status, summary with screenshots, report,
  backlog, spend (with the DeepSeek balance), the planner chat (messages and .md/.txt
  files, /show, /check, /approve, /discard), release, pause/resume/stop, image picks,
  lessons and worker-limit choices with buttons, and a redacted doctor.
- Pairing with a one-time code (`ms telegram pair`); everyone else is ignored. Approve,
  release and limit choices ask for confirmation. Actions are recorded with
  `actor: telegram`. No payment actions.
- Notifications go to Telegram (summary photos, limit buttons), with ntfy as fallback.
- Setup guide: `docs/TELEGRAM-SETUP.md`.

### Worker limits
- Provider limits (HTTP 429, quota, model gone or no longer free, missing credential)
  end an attempt as `limited`: not verified, never counted against budgets, no cost.
- The reset time is read from the provider. Known resets within
  `[worker] max_auto_wait_minutes` (120) are waited for automatically; unknown resets
  get one 60-minute wait. Longer limits and gone models pause the project and ask the
  owner: `ms limit <project> wait | free | paid`, with an immediate notification.
- Worker profiles gain `paid` and `label`; `[worker] workers` (or `workers` per project)
  orders them. Every attempt records its profile; `ms report`, `ms status` and the
  morning summary show limits, waits, pending choices and attempts per worker with cost.
- A weekly check confirms the free worker models are still offered and free.

## m4-direction-to-overnight — 2026-10-06

### Milestone 4: From direction to overnight work
- **Direction:** a project's `docs/DIRECTION.md` (configurable path and size budget) is
  read by the planner, the Master, workers and the visual reviewer, who judge proposals
  against it; planned tasks protect it.
- **Backlog:** epics with `priority` and `summary`; `proposed` epics are unapproved.
  `ms backlog` lists, adds and reorders; the planner adds epics and plans a whole epic
  ("plan epic 1 from the backlog", up to 12 tasks). Ready tasks are taken in backlog order.
- **Task types:** `developer`, `visual`, `logic`, `docs`, each with a skill doc
  (`skills/`), allowed paths (frozen into `acceptance.allowed_paths`, enforced by the
  verifier) and allowed tools (enforced through a per-attempt OpenCode config).
- **Shared project memory:** `LESSON:` lines from verified attempts go to a pending list;
  `ms lessons` approves them in a batch; only approved lessons reach later workers.
- **Visual reviewer:** phone-sized screenshots of visual tasks, judged by DeepSeek
  `deepseek-flash` against the task and the direction; it can block, never pass.
  Screenshots are kept as task artifacts; review cost counts toward the daily cap.
- **Images:** visual tasks can declare assets generated in the project's fixed style
  (Pollinations, then Cloudflare Workers AI); several candidates wait for `ms pick`.
- **Morning summary:** one notification and one report page (`ms report --summary`) with
  tasks done, blocked, screenshots, cost and what needs the owner, when the work runs out
  or the daily cap is reached. Per-run notifications are off by default.
- `ms status` lists image picks and pending lessons under "needs you".
- New configuration: `[reviewer]`, `[images]`, `[daemon] batch_notifications`,
  `[planner] max_output_tokens`; project keys `direction`, `task_types`, `visual_review`,
  `images`; task fields `type`, `assets`, `screens`.

### Changed
- Project definitions moved out of the repository to
  `~/.config/master-system/projects/` (or `[run] projects_root`). The repository's
  `projects/` and the tests tied to live project data are removed; tests use a fictional
  fixture project, and `examples/projects/example-app/` documents a definition.
- The preview site is optional: without `github.site_dir` only `develop` is pushed.
  `github.site_url` overrides the default github.io address. `ms status` labels the
  released site "Live:".
- Planner test conventions are configured per project (`test_suffixes`, `syntax_check`,
  `test_command_examples`, `test_guidance`, `broken_test_markers`, `ignore_paths`)
  instead of being JavaScript-only.
- Code messages point to `docs/GITHUB-SETUP.md` and `docs/ARCHITECTURE.md`.

### Fixed
- P1: the planner could answer "I'll read the files…" and end its turn without reading
  them. Reads now complete within the same turn (files named in prose are read; an empty
  promise is re-asked once; the last call must answer).

### Documentation
- README restructured as project-agnostic documentation: features, architecture
  overview, requirements, setup, adding a project, workflow, command and configuration
  reference, safety model, roadmap. Personal setup confined to a final section.
- New `docs/ARCHITECTURE.md` (design, module map, trust boundaries, development rules).
- `docs/github-setup.md` renamed to `docs/GITHUB-SETUP.md` and made generic.
- `docs/TROUBLESHOOTING.md` and `docs/DECISIONS.md` reworded without project-specific
  references.
- Milestone working plans moved to `archive/plans/`.
- MIT license added.

## m3-hands-off — 2026-10-06

### Added
- Background service (systemd user unit) that runs approved tasks on its own, with daily,
  per-run and per-task caps, stall detection, ntfy notifications and
  `ms pause` / `ms resume` / `ms stop`.
- Automatic integration of verified work into `develop`, with rebase and re-verification;
  a conflicting task is retried. The release branch changes only through a release a
  human merges.
- Publishing through a GitHub App that can never change `main`: pushes `develop` and a
  Pages site (released version at `/`, preview at `/develop/`).
- `ms status` (plain-language headline), `ms report`, `ms publish`,
  `ms github setup/check`, `ms notify setup/test/send`.
- Planner chat (`ms chat`): drafts epics and tasks with tests, verifies that tests fail on
  the current code, and queues nothing until approved.
- Releases: notes from history and a `develop` → `main` pull request; after the merge, a
  tag, a GitHub Release and the live site. `ms release` also commits a `CHANGELOG.md`
  entry to the managed project.
- `ms status` completes releases merged on GitHub, checking at most once a minute.
- `ms doctor`: a paste-ready diagnostic block (versions, checkout, service, logs, last
  report, redacted configuration, GitHub check, disk space) that never prints secrets.
- Worker tiers with escalation (disabled until a paid worker credential is configured).
- Planner chat usability: multi-line pastes arrive as one message (bracketed paste or
  `"""` blocks), `--file` sends a file as the first message, replies wrap to the terminal,
  questions are numbered, drafts are formatted per task, and the input line supports
  readline editing and history.

### Changed
- History schema v4.
- Planner chat cap defaults to $0.30.

### Fixed
- `ms` run inside another checkout of the repository used that checkout's code and project
  files; the wrapper now runs `python -P`.

### Removed
- Early experiments (`bob.py`, `agents/`, `docker/bob`) moved to `archive/`.

## m2-run-for-real — 2026-10-05

### Added
- `run_cli`: start, resume, status, report, integrate (`--rebase`), task describe /
  set-acceptance / manual-check, `--until-stopped`, `--max-cost-usd`.
- OpenCode worker in a dedicated home with an allowlisted environment and no secrets;
  the acceptance verifier; usage and cost accounting; reports built from history alone.
- First end-to-end runs against a real managed project, supervised and then unattended.

## m1-contain-verify — 2026-10-05

### Added
- One git worktree per attempt, a project lock, recovery, process deadlines and logs.
- Completion gate (latest attempt passed on the current spec) and an attempt limit.
- SQLite history with migrations and backups.
