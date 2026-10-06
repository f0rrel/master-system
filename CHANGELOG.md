# Changelog

Notable changes to Master System, newest first. Versions correspond to git tags.
Rationale for each change is in [docs/DECISIONS.md](docs/DECISIONS.md).

## Unreleased

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
