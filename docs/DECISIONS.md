# Decision log

Architectural decisions and their rationale, oldest first, grouped by milestone. Add an
entry whenever a decision is made or reversed (see the documentation policy in
[ARCHITECTURE.md](ARCHITECTURE.md#documentation-policy)).

**Status values:** *Approved* — reviewed and accepted by the maintainer. *Implementation
choice* — made during implementation and recorded here for review.

## Foundations

| Decision | Rationale | Status |
| --- | --- | --- |
| Separate project state (facts) from operations (side effects); `in_progress` does not imply execution | Auditability, explicit side effects, safe recovery, no accidental duplicate execution | Approved |
| Two operation kinds, STATE and DISPATCH; `run_task(project_id, task_id)` carries no worker, model, workspace or prompt | Execution details are orchestration concerns; the Master stays model-independent | Approved |
| The Master returns one decision with at most one operation | Smallest useful step; clear audit trail | Approved |
| Worker `state_updates` are untrusted proposals and never applied | Single writer; worker output is not authoritative | Approved |
| Worker output is opaque provenance, kept out of the Master's context ("record provenance, don't branch on it") | Model independence; prompt-injection resistance | Approved |
| YAML stays authoritative; SQLite holds append-only history; no faked atomicity | Simplicity; human-editable state; honest failure semantics | Approved |
| SQLite with WAL and `synchronous=FULL`; only `sqlite_history.py` imports `sqlite3` | Durability before side effects; replaceable store | Approved |
| No automatic replay of uncertain attempts | The first worker may already have changed the world | Approved |
| One autonomous run per project via a non-blocking `flock`; human mutations take the same lock; PIDs are informational only | Simple and kernel-managed; no leases needed on one machine | Approved |
| Completion gate: latest attempt finished, verdict `pass`, no newer attempt; otherwise a human decides | A QA pass is not completion | Approved |
| Attempt limit of 3 per (session, task) | Prevents hammering a broken task | Approved; to be unified with the service budget (gap H4) |
| A work session is operational continuity, not memory | Separation of concerns | Approved |
| AI coding assistants used to build the system are never runtime dependencies | Model and agent independence | Approved |
| Use mature coding agents as workers rather than building one | Focus on the control plane | Approved |
| Do not adopt Temporal (cluster) or LangGraph (graph DSL, LangChain coupling) for now | Infrastructure weight; unwanted coupling | Approved |
| DBOS is the fallback if homemade durability grows too complex; evaluate it in milestone 4 | In-process library on SQLite | Approved |
| Approval policy by transition and field | Risk lives in the arguments, not the operation name | Approved; not yet implemented (gap H2) |
| No self-hosting (the system working on itself) until approvals exist; validate on a separate repository first | Avoid self-modification through the back door | Approved |
| Five-milestone roadmap: contain, run, hands-off, durable runtime, focused context | Sequencing from an external design review | Approved |
| `uv.lock` is committed | Reproducible development environments | Approved |

## Milestone 1: Contain and verify

| Decision | Rationale | Status |
| --- | --- | --- |
| A git worktree per attempt, and gated integration | Safe retries; attributable evidence | Approved |
| Frozen acceptance criteria per task (shell commands and protected paths), verified by the orchestrator | An independent definition of done | Approved |
| State directory `$XDG_DATA_HOME/master-system/`; worktrees in the sibling `master-system-worktrees/` | Worktrees inside the state directory would violate the isolation rule | Approved |
| Workers run under `timeout --signal=TERM --kill-after=30s` | The deadline holds even if the loop dies; an orphaned worker would otherwise hold the inherited lock | Approved |
| The orchestrator generates the attempt id, so `attempt_started` (base SHA, worktree) is written before `git worktree add` | Intent before side effect | Approved |
| Integration is fast-forward only and idempotent by SHA; a moved base is refused | Conflicting merges need judgement (later relaxed for `develop`, see H-D1) | Approved |
| Recovery runs on lock acquisition by history users (session runner, loop, integrate), not by the state CLIs | `master.py` must not import history | Approved |
| The completion gate compares `spec_hash` with the spec *after* the operation | Retitle-and-complete in one operation must not pass | Approved |
| "Reopen invalidates a pass" deferred to transition policy | `spec_hash` cannot detect a reopen | Approved |
| Verification has its own deadline (30 min total, 10 min per command) | Acceptance commands need a bound too | Approved |
| Cancellation deferred | Killing a recorded PID risks hitting an unrelated process after PID reuse | Approved |
| The history database is backed up before each schema migration; a failed backup refuses the migration | Schema rebuilds must be reversible | Approved |
| Every autonomous run holds the project lock; a loop without one takes it itself and recovers first | Every worker inherits a lock regardless of who started the loop | Implementation choice |
| Snapshot commits set identity through `GIT_AUTHOR_*` / `GIT_COMMITTER_*` environment, not `-c user.*` | Inherited environment would otherwise override the orchestrator's identity | Implementation choice |
| A process killed by signal or exiting 124/137 counts as `timed_out` only once the deadline has passed | `timeout` signals its own group; a worker may legitimately exit 124 | Implementation choice |
| When a worker exits, anything left in its process group is killed | Leftover children would keep writing and keep the lock | Implementation choice |
| An attempt whose snapshot commit fails is not verified | There is no committed result to verify | Implementation choice |
| Backends do not put a `workdir` in their artifacts | Nothing may take a location from worker output | Implementation choice |
| Integration also refuses if the project's repository or base branch changed since the attempt, or if the base checkout has uncommitted tracked changes | Never integrate into something other than what was verified; never overwrite manual edits | Implementation choice |
| Recovery records an observation error instead of failing | One broken worktree must not block recovery of the rest | Implementation choice |
| `create_task(status=completed)` is gated like `update_task` | Otherwise creation would bypass the completion gate | Implementation choice |
| Worker output goes to log files under the state directory (path and SHA-256 in history); the helper waits on the process, not its pipes | A finished worker with a background child was misreported as timed out | Approved |
| `ProjectLock.release` closes its descriptor and never calls `LOCK_UN` | Unlocking would release the lock for a worker that escaped its group | Approved |

## Milestone 2: Run it for real

| Decision | Rationale | Status |
| --- | --- | --- |
| The worker is the OpenCode CLI, run in a dedicated home; the managed project is worked on in a dedicated clone | Mature agent; separation from manual checkouts | Approved |
| `integrate --rebase` (human-only at the time) cherry-picks onto the current base, re-runs acceptance, records a new verification bound to the rebased SHA and fast-forwards exactly to it | Only verified commits are integrated | Approved |
| Default Master model: DeepSeek V4 Flash, configurable | Cost and quality balance | Approved |
| Workers and verification get an allowlisted environment (`core/worker_env.py`): newest suitable Node.js from nvm's directory plus system paths, a dedicated `HOME` and `XDG_*`, locale and terminal variables, `CI=1`, no secrets (secret-looking extras are refused) | Workers must not see the Master's keys | Approved |
| The human-only task `description` is part of `spec_hash` only when present | Adding the field must not invalidate existing evidence | Implementation choice |
| `run_cli` refuses to start without the model API key, the worker binary or the required Node.js | Fail before any history is written, with a clear message | Implementation choice |
| Usage of unusable model replies is recorded as `failed_call_usage` | Every paid call is counted | Approved |
| A report counts a spec change as explained by a `human_action` or by the Master's own recorded state operation | Only edits made outside the tools are "unexplained" | Implementation choice |
| A failed re-verification during `integrate --rebase` is recorded on the attempt | The record must show what was checked, including failures | Implementation choice |
| Tasks have a human-only `manual_check`, excluded from `spec_hash` and copied into `attempt_started` | Reports show it from history alone; rewording it must not invalidate evidence | Approved; exclusion is an implementation choice |
| Attempt commits are named `<task id>: <task title>` at snapshot time | Integration must land exactly the verified SHA | Approved |
| `integrate` accepts a unique attempt-id prefix of 8+ characters | Reports print 12 characters | Approved |

## Milestone 3: Hands-off

| Decision | Rationale | Status |
| --- | --- | --- |
| The human's role is reduced to planning, reviewing the preview and approving releases; the service starts on login | Hands-off operation | Approved |
| A background service runs approved, ready tasks with acceptance, within daily and per-run caps, with no retry after 3 failures since the last human action, and sends notifications | Unattended operation that cannot spend indefinitely on a hopeless task | Approved |
| `core/host.py` joins the modules allowed to start processes (the service's own runs and unit; never workers) | The boundary test lists who may start processes | Implementation choice |
| **H-D1:** for projects with `auto_integrate: true`, integrating a verified attempt into `develop` is a routine system action, done right after the pass under the run's lock, with the same rules as a human integration. The release branch changes only through a human-merged release pull request | Hands-off without giving up control of releases | Approved |
| **H-D2:** in such projects a task completes only when its latest attempt is integrated (`not_integrated` gate reason), so a dependency is satisfied only when its code is in `develop` | "Done" means "in the preview" | Approved |
| **H-D3:** a rebase conflict or failed re-verification is recorded as `integration_refused` and presented to the Master as "run the task again"; the next attempt starts from the new `develop` within the attempt limit | Ordinary conflicts need no human | Approved |
| **H-D4:** after each run the service pushes `develop` and a `gh-pages` site (release branch at `/`, `develop` at `/develop/`) with a GitHub App installation token passed only via `GIT_ASKPASS`. The App has no Administration permission; a ruleset on `main` allows no bypass | Stable preview URL; the App can never change `main` | Approved |
| **H-D6:** `ms chat`: a planner model drafts an epic of tasks with tests, asks about unclear points and reads the base branch read-only. A draft can be approved only after deterministic checks in a fresh worktree; `approve` re-checks under the lock, commits the tests, writes the epic and tasks atomically and records a `human_action` per task. Tasks gain a human-only `size` | Acceptance edits remain human-approved | Approved; checks and flow are implementation choices |
| Releases: notes from history and a pull request `develop` → release branch. After a human merge the system verifies the merge tree equals the reviewed `develop` tree, tags `v0.N`, creates the GitHub Release and republishes; a mismatch is reported and not tagged | The release branch changes only by a human merge | Approved; tree check is an implementation choice |
| Worker tiers: profiles and a ladder, cheapest first; start at the task size's tier, move up after 2 failed semantic attempts, never down; the orchestrator records the tier; the Master never sees it; the service's per-task budget becomes 2 × ladder length (minimum 3). Default ladder is empty | Spend more only where cheap models fail | Approved |
| `ms status` completes merged releases, checking GitHub at most once a minute | A release must not wait for the service to be busy | Approved |
| `ms doctor` prints a diagnostic block with secrets redacted | Shareable diagnostics without leaking credentials | Approved |
| `ms release` commits a `CHANGELOG.md` entry to the project's `develop` under the project lock before opening the pull request | Releases document themselves | Approved |
| Planner chat cap defaults to $0.30 | $0.10 was too low for a realistic epic | Approved |
| History schema v4 adds `integration_refused`, `published`, `planner_turn` and `release` in one migration with backup | One migration for the whole milestone | Implementation choice |
| Early experiments and milestone plans moved to `archive/`; documentation split into a usage README and developer docs | Documentation readable without project history | Approved |
| Documentation is project-agnostic; per-project information lives in `project.yaml` and the managed repository | The system is reusable across projects | Approved |

## Project-agnostic system

| Decision | Rationale | Status |
| --- | --- | --- |
| Project definitions live outside the repository, by default in `$XDG_CONFIG_HOME/master-system/projects` (`[run] projects_root` overrides); tests use fictional fixtures in an isolated `XDG_CONFIG_HOME` | The public repository holds no managed project's data; tests no longer depend on live task state | Approved |
| The preview site is optional (`github.site_dir`, no default) and its URL configurable (`github.site_url`) | Not every project has a static site | Approved |
| The planner's test conventions (`test_suffixes`, `syntax_check`, `test_command_examples`, `test_guidance`, `broken_test_markers`, `ignore_paths`) come from `project.yaml`; without them any file under `test_dir` is accepted and no syntax check runs | Any language, no JavaScript assumptions in the code | Approved; key names are implementation choices |
| P1: one planner turn is up to four model calls. Files named in prose are read; an empty promise to act gets one corrective re-ask; the last call must answer without reads | Tool use completes within the turn, so the owner never pays to nudge the model | Approved; heuristics are implementation choices |

## Milestone 4: From direction to overnight work

| Decision | Rationale | Status |
| --- | --- | --- |
| Each project may keep `docs/DIRECTION.md` in its own repository; planner, Master, workers and the visual reviewer read it from the base branch within a size budget, and planned tasks protect it | Proposals are judged against the owner's standing intent; project content stays in the project | Approved |
| Backlog epics are milestones with `priority` and `summary`; status `proposed` marks unapproved epics, changeable only through `ms backlog` or an approved planner draft | No new state store; the Master cannot approve or fill epics itself | Approved; field names are implementation choices |
| Backlog order is given to the Master as ordered `ready_tasks` and an objective, not enforced by policy | A refusal loop would cost more than an out-of-order task (gap B1) | Implementation choice |
| Task types `developer`, `visual`, `logic`, `docs` replace personas; each has a skill doc, allowed paths and allowed tools; paths are frozen into `acceptance.allowed_paths` and enforced by the verifier; tools by a per-attempt OpenCode inline config | Enforcement by the orchestrator, not by the model's goodwill | Approved; enforcement mechanism is an implementation choice |
| Lessons come only from verified attempts, wait in a pending list, and are used only after the owner approves them, for matching types, within a budget | Shared memory without unreviewed prompt injection | Approved |
| The visual reviewer wraps the acceptance verifier, runs only for passed visual tasks on their own result, and can only block; an unavailable reviewer changes nothing | A model may veto, never vouch | Approved |
| Reviewer model: DeepSeek `deepseek-flash` (vision input, ≤1024 tokens per image, the existing key); about $0.002–0.003 per review at peak prices | Cheapest suitable model without a new account | Approved |
| Images are generated by the service (orchestrator side), not by a worker tool; assets are declared in the task spec | Workers hold no keys; generated assets are reviewable before work starts | Implementation choice |
| Image providers: Pollinations (now requires a free API key), then Cloudflare Workers AI; no scraping | Free providers with official APIs | Approved |
| A single image candidate is committed to the base branch as a system commit; several candidates wait for `ms pick`, which commits the chosen one as a human action | The owner chooses identity-defining art; routine art does not wait | Implementation choice |
| A task waiting for images is skipped by the service, shown to the Master as `waiting_for_owner`, and refused by the orchestrator | Three layers, so a waiting task cannot run by mistake | Implementation choice |
| One morning summary (text and HTML) when the work runs out or the daily cap is reached; per-run notifications off by default (`[daemon] batch_notifications`) | One notification per night instead of many | Approved; trigger rules are implementation choices |
| Planner replies may use up to 16000 output tokens (`[planner] max_output_tokens`) and drafts up to 12 tasks | A whole epic with its tests does not fit in 4000 tokens | Implementation choice |
