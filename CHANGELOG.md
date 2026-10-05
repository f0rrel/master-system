# Changelog

Notable changes to the Master System, newest first. Each milestone and each fix batch adds
an entry here (see README, "Documentation rule"). Decisions and their reasons are in the
README decision log (§13).

## Unreleased (branch `m3-hands-off`)

### 2026-10-06: quick fixes
- `ms status` finishes releases the owner merged on GitHub (tag, GitHub Release, live site),
  checking GitHub at most once a minute, so nothing waits for the service to be busy.
- `ms doctor`: a paste-ready diagnostic block (versions, checkout, service, logs, last
  report, redacted config, GitHub check, disk space). It never prints keys, tokens or the
  ntfy topic.
- `ms release` commits a `CHANGELOG.md` entry to the managed project's `develop`,
  generated from the task history.
- The planner chat cap is now $0.30 by default.
- The README has a documentation rule.

### 2026-10-06: Milestone 3, "Hands-off"
- A background service (systemd user unit) processes approved tasks on its own, with daily,
  per-run and per-task caps, ntfy phone notifications, `ms pause` / `ms resume` / `ms stop`,
  and stall detection.
- The system integrates verified work into `develop` automatically, with rebase and
  re-verification, and retries a task on conflict. `main` changes only through a release
  the owner merges.
- A GitHub App (only on Match Legends, never able to change `main`) pushes `develop` and a
  Pages preview (`/` is the released game, `/develop/` is the preview).
- `ms status` (a plain-words headline), `ms report`, `ms publish`, `ms github setup/check`
  and `ms notify setup/test/send`.
- The planner chat (`ms chat`) drafts epics and tasks with tests, checks that the tests fail
  on the current code, and queues nothing until the owner approves.
- Releases: notes from history, a `develop` -> `main` PR; after the merge, a tag, a GitHub
  Release and the live site.
- Worker tiers with escalation (off until a paid worker key exists).
- History schema v4.

## m2-run-for-real (2026-10-05): Milestone 2, "Run it for real"
- `run_cli`: start, resume, status, report, integrate (`--rebase`), task describe /
  set-acceptance / manual-check, `--until-stopped`, `--max-cost-usd`.
- The OpenCode worker in a dedicated home with an allowlisted environment (Node 22, no
  secrets); the AcceptanceVerifier; usage and cost accounting; reports from history alone.
- Match Legends tasks ml-1 to ml-8 run for real (supervised, then overnight).

## m1-contain-verify (2026-10-05): Milestone 1, "Contain and verify"
- One git worktree per attempt, a project lock, recovery, process deadlines and logs.
- The completion gate (the latest attempt passed on the current spec) and an attempt limit.
- SQLite history with migrations and backups.
