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
| Completion gate: latest attempt finished, verdict `pass`, no newer attempt; otherwise a human decides | A QA pass is not completion | Approved; widened to cancellation (see "Orchestrator scope") |
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

## Worker limits

| Decision | Rationale | Status |
| --- | --- | --- |
| A provider limit (rate limit, quota, model gone or no longer free, missing credential, provider error before any work) ends the attempt as `limited`; it is not verified and never counts against a task's budgets | Infrastructure must not exhaust tasks or mislead the Master | Approved |
| The reset time is read from the provider's answer and recorded, or recorded as unknown | Waiting is only sensible with a known end | Approved |
| A known reset within `max_auto_wait_minutes` (default 120) is waited for automatically (reset + 2 minutes), then the same worker continues; an unknown reset is waited for 60 minutes once | Short limits resolve themselves without cost or owner effort | Approved |
| Longer limits, gone or no-longer-free models and missing credentials pause only that project and ask the owner: wait, next free worker, or the paid worker | Switching models or spending money is the owner's decision | Approved |
| The owner's free or paid choice lasts until the limited worker's reset (or 24 hours), then the first worker is used again | The preferred worker comes back without another decision | Implementation choice |
| Classification is by patterns in the worker's output; a generic provider error counts as a limit only before the first worker step | OpenCode reports some provider failures (e.g. an unknown model) only as a generic error | Implementation choice |
| Second free worker: OpenCode `space-bunny-free` (zero data retention per OpenCode Zen; verified on a real edit). `longcat-2.5-preview-free` also qualifies | Zero retention was requested; both passed a test edit | Approved |
| Paid worker: DeepSeek `deepseek/deepseek-flash` through OpenCode in its own worker home with a spend-limited key; the Master's key is not reused | Workers hold no control-plane secrets | Approved |
| A weekly check lists the providers' models and notifies when a free profile's model is gone, inactive or no longer free | Free models change without notice | Approved |

## Telegram

| Decision | Rationale | Status |
| --- | --- | --- |
| Telegram is the main phone interface; ntfy stays as a notification fallback | Two-way control (buttons, chat, files) from the phone | Approved |
| Long polling from a thread in the service, stdlib HTTP, no webhook and no open port | No inbound exposure, no new dependency | Approved |
| One owner, paired with a single-use 15-minute code; other users get no answer | No information leaks to strangers | Approved |
| Only fixed operations; buttons carry random ids of actions the bot stored itself, single use, 48-hour expiry | Nothing typed or forged can name a command | Implementation choice |
| Approve, release and worker-limit choices need a confirmation button; there are no payment actions at all | Consequential actions need a deliberate second tap; money stays manual | Approved |
| Bot actions reuse the `ms` commands with `--actor telegram`; others are recorded directly; system-wide actions use project `_service` | Same behaviour and audit trail as the terminal | Implementation choice |
| `/spend` shows DeepSeek's balance from its read-only `GET /user/balance` | Information only | Approved |

## Stalled workers

| Decision | Rationale | Status |
| --- | --- | --- |
| An attempt that changes no file is `stalled` (infrastructure, not a failure) when the model was cut off or ran at least `stall_minutes` (5); its excerpt (reason, reasoning tokens, last tool calls) is recorded and shown in `ms report` | Found on a real task: three attempts spent Big Pickle's 32,000-token output limit on reasoning and wrote nothing, and were counted as failures | Approved |
| Three stalls since the last human action make the task wait for the owner | Stalls must not loop forever at no visible cost | Implementation choice |
| Workers are told to write files early and in pieces | The cut-off happened while composing six SVG drawings in one go | Implementation choice |
| Every task type keeps the `bash` tool | OpenCode's free tier refuses requests without it (HTTP 403); found by a live test creating a file under each type | Implementation choice |
| `ms reopen` puts a blocked task back to planned with a recorded human action, spec unchanged | A retry after an infrastructure fix needs no spec change | Approved |

## Task size and splits

| Decision | Rationale | Status |
| --- | --- | --- |
| The Master's evidence covers only attempts since the task's last human action | A reopen must be a fresh start; ml-15 was blocked again from its pre-reopen failures | Approved |
| Planner rule: one new thing per task, about 150 lines, bigger work as an ordered sequence with depends_on; every task has estimate_lines | Free models have a per-reply output limit; ml-15 (six drawings) never fit | Approved |
| `check` flags oversized tasks (estimate, item count, files) with a suggested split; approve refuses them unless "approve anyway" (recorded) | Catch oversized tasks before they run, keep the owner's override | Approved; heuristics are implementation choices |
| After one cut-off the next attempt writes incrementally; after two, the planner drafts a split for the owner instead of blocking | Recover without burning attempts; splitting changes the plan, so the owner approves it | Approved |
| A split is a draft with `replaces`; approval cancels the task (`replaced_by`), inserts the replacements in its place and rewires dependents to all of them | The plan stays a single ordered list; nothing that depended on the task can start early | Implementation choice |
| Escalate (set size to hard, the top tier) is offered only when worker tiers are configured | Escalation without tiers would do nothing | Approved |

## Hardening: independent verification

| Decision | Rationale | Status |
| --- | --- | --- |
| Verify every attempt in a fresh worktree of its committed result (`<attempt>-verify`), never in the worker's | Uncommitted or ignored files (gap V1) must not influence the verdict; acceptance builds what it needs from committed files (planner `setup` runs first) | Approved |
| Record every ref and the common git dir's `config`, `hooks/` and `info/` before the worker; restore them before the snapshot; anything beyond the attempt's branch, stash, remote-tracking refs and git identity fails the attempt as `repository_tampered` through the existing fail path | Worktrees share authority with the clone (gap V2); restoring before the snapshot keeps planted filters and hooks from running; reusing the fail path keeps failure counting, evidence and reports unchanged. Detection and restoration, not a sandbox | Approved |
| A tampering attempt that changed no file is a failure, not a stall; the tips of restored refs are kept in `restored_refs` | A stall is not counted, and tampering must be; a hand commit made to develop during a run must stay recoverable | Implementation choice |
| Every control-plane git command runs with `core.hooksPath=/dev/null` and `core.fsmonitor=false` through one helper, guarded by a boundary test | A worker can write the shared git dir; the control plane must not run its code (gap G1) | Approved |
| Ref writers outside a run (release check, `ms publish`, the release fetch, the service's watcher and post-run publisher) take the project lock and skip while it is busy | During a worker run every ref change is attributed to the worker and undone | Implementation choice |
| Publish guard: `fast_forward` records the tip it set in `refs/ms-system/heads/<branch>`; the publisher and release preparation refuse a different `develop`; the owner accepts a deliberate change with `ms publish --accept-tip` (a recorded human action) | Nothing should reach GitHub that no gate produced. A ref rather than a history event: image and changelog commits record no event and a new event type needs a schema migration; `refs/ms-*` changes during a run are already tampering | Approved (guard); implementation choice (ref) |
| The recorded tip advances only when the branch moved from it; before the first system write the current tip is trusted | Building on a foreign tip must not adopt it; existing installations need no manual step | Implementation choice |

## Orchestrator scope

| Decision | Rationale | Status |
| --- | --- | --- |
| **Reversed:** the completion gate applied only to `completed`; every other status change, including `cancelled`, was left to the approval policy, where `update_task` is routine, so the orchestrating model could cancel a task on its own | — | Superseded by the next row |
| A proposal to set a task `cancelled` (`update_task` or `create_task`) is held for the owner with gate reason `cancellation_requires_human`, through the completion gate's stop path; `blocked`, `in_progress` and `planned` stay routine | A model that cannot fake a completion could otherwise drop a task it cannot finish | Approved |
| `ms cancel <project> <task> --reason` cancels an open task as a recorded human action and names the tasks that depend on it (unchanged); `ms reopen` keeps a task | The owner needs a recorded way to accept a held cancellation; a recorded human action also clears the wait | Approved |
| A held cancellation makes its task wait for the owner in the service (other tasks keep running); held completions are unchanged | Running the task again would stop on the same proposal and stall the whole project | Approved |
| Split approval still cancels the replaced task | It is the owner's action, not the model's | Approved |

## The first ten minutes

| Decision | Rationale | Status |
| --- | --- | --- |
| Without a projects root, `ms status` and `ms doctor` print a one-line hint, and the service idles (logging the hint once) until the root exists | A traceback is no help to a new user, and an exiting service is restarted by systemd every minute with "service stopped" notifications | Approved |
| `ms install` creates the projects root (mode 700) | One step fewer, and the right permissions by default | Approved |
| Node is optional for workers: looked up in `[worker] node_bin`, nvm, then the worker `PATH`; without it runs and `ms chat` warn once (and `ms doctor` says so) instead of refusing | Python projects need no Node; a hard requirement blocked them at the first `ms chat` | Approved |
| `[worker] path_dirs` adds existing directories (checked at load, `~` expanded) to the worker and verification `PATH`; the secret-name check for environment extras is unchanged | Project toolchains such as uv live outside the system `PATH`; the environment stays an allowlist | Approved |
| A Node binary's version is read by running `node --version` from `core/host.py`; the lookup in `core/worker_env.py` starts no process | Keeps the process-starting modules as they are (boundary test) | Implementation choice |
| A quickstart project in `examples/quickstart/` with zero dependencies, whose `init.sh` writes only the target repository and prints the remaining steps | A new user can try the whole loop with nothing to install beyond Node; never touching `~/.config` keeps the script safe to run | Approved |

## Project hygiene

| Decision | Rationale | Status |
| --- | --- | --- |
| CI runs the offline suite on Python 3.12 and 3.13 with Node 22, linked into `~/.nvm/versions/node/` | Without Node two tests skip (the quickstart's base check, the worker Node test); nvm's layout is where workers look first after `node_bin` | Implementation choice |
