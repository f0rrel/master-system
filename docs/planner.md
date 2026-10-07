# Planner

The planner turns the owner's plain-language request into an epic of small, testable tasks
whose acceptance tests fail on today's code. It is the only path that creates planned work
without a human hand-editing YAML. This document focuses on the planner's behaviour and,
in particular, on task replacement (`replaces`). For the surrounding system see
[ARCHITECTURE.md](ARCHITECTURE.md); for commands see [REFERENCE.md](REFERENCE.md).

Code: `core/planner.py` (chat, drafts, validation, `task_records`),
`core/planner_checks.py` (`make_checker`, `make_approver`), `core/work_manager.py`
(`add_planned_work`, `split_task`), `core/splits.py` (automatic split drafts).

## Flow

```text
ms chat turn ─▶ draft ─▶ draft_problems ─▶ check (fresh worktree, tests must fail)
                                                        │
                                          approve ──▶ re-check (project lock)
                                                        ├─ add backlog epics
                                                        ├─ commit acceptance tests
                                                        │    (skipped if already on the base branch)
                                                        └─ replaces? split_task : add_planned_work
```

1. **Draft.** One owner message is one *turn* of up to four model calls: the planner may
   list files to read (read-only, via git) and is asked again before the owner sees
   anything. It returns a complete draft or null (`PlannerChat.turn`,
   `core/planner.py:682-740`).
2. **Validation.** `PlannerChat.problems` (`core/planner.py:770-788`) plus
   `draft_problems` (`core/planner.py:178-277`) check ids, epic/backlog rules, task types,
   sizes, `must_not`, test paths/suffixes/commands, and that dependencies exist.
3. **Check.** `check` validates in a **fresh worktree of the base branch**
   (`make_checker`, `core/planner_checks.py:51-124`): run the project's `setup`, run each
   test through `syntax_check`, require each task's test commands to **fail** on the
   current code, and keep the project's `base_checks` green. A draft with only backlog
   epics needs no check.
4. **Approve.** `PlannerChat.approve` (`core/planner.py:805-830`) requires a passing
   `check` of the *current* draft (the stored `draft_hash` must match), then calls the
   approver, which re-runs the checks under the project lock (`make_approver`,
   `core/planner_checks.py:127-188`).

## Approval writes

Under `ProjectLock`, `make_approver`:

1. re-runs the checker and refuses if it now fails;
2. adds any `draft.backlog` epics as unplanned backlog (`master.add_backlog_epic`);
3. commits the draft's acceptance-test files to the base branch (see below);
4. writes the tasks through `Master.split_task` when `draft.replaces` is set, otherwise
   through `Master.add_planned_work` (`core/planner.py:288-317` builds the records);
5. records one `human_action` per task (`actor: owner via planner`) with the draft hash.

`task_records` turns a draft task into a project task: `status: planned`, the type's
frozen `allowed_paths`, and an acceptance whose `commands` are `setup + the task's
test_commands + base_checks`. `must_not` is appended to the description as a `Must not:`
section (`with_must_not`), so no new spec field is introduced.

## Acceptance tests

The acceptance test files are part of the draft. During `approve` they are staged with
`git add` and committed to the base branch with a fixed identity, message
`<epic id>: acceptance tests for <epic title>` and a body naming the chat and draft.

If the same file with identical content is already on the base branch — a replacement
whose acceptance tests an earlier epic already committed — there is nothing to commit.
Git reports "nothing to commit" and exits `1` **on stdout**. The approver therefore checks
`git status --porcelain -- <test_paths>` first: when nothing is staged it skips the commit
and uses the current base tip as the tests commit (see fix 2 below).

## `replaces`

`replaces` names an existing open task (`planned`, `in_progress` or `blocked`) that the
draft re-plans. The draft's `epic.id` must be that task's own epic
(`PlannerChat.problems`, `core/planner.py:773-784`). Two shapes are supported:

- **New-id replacement (a split).** Every replacement record has a new id. On approval
  `split_task` sets the original task `status: cancelled` with `replaced_by` naming the
  new ids, inserts the new tasks in its place, and rewires tasks that depended on it to
  depend on all of the new ones (`core/work_manager.py:421-428`).
- **Same-id replacement (a re-plan).** A record reuses the replaced task's own id. On
  approval `split_task` updates that task **in place** (same id and position, new spec,
  `status: planned`) with no `replaced_by`; any records with other ids are inserted after
  it (`core/work_manager.py:414-420`). Dependents that point at the id stay valid, so no
  dependency rewiring happens.

`draft_problems` exempts the replaced id from the "id is already used" rule
(`core/planner.py:215`), so both shapes pass validation. The planner's system prompt asks
for new ids on a split (`core/planner.py:453-455`); the same-id form is what a re-plan
produces.

## Automatic split drafts

After a second output cut-off since the last human action, `SplitStep` (`core/splits.py`)
has the planner draft a split (`replaces: <task>` in the task's own epic), runs its checks,
and offers Approve / Reject (Escalate with worker tiers). Approving goes through the same
`PlannerChat.approve` path, so the replacement rules above apply.

## Recent planner fixes

These three fixes were made together in October 2026. The commit after the third is tagged
`checkpoint-2026-10-07-after-planner-fixes`.

### 1. Allow planner task replacements (`56538f9`)

- **Before:** `draft_problems` rejected any task id already present, so a replacement that
  reused the replaced id failed validation with "the id is already used". A re-plan of an
  existing task was impossible.
- **Why that was wrong:** a replacement should be able to keep the task's identity; the
  owner reported a draft with `replaces: ml-43` and task id `ml-43` being refused.
- **Change:** `core/planner.py:215` exempts the replaced id:
  `(tid in existing_task_ids and tid != draft.get("replaces")) or ids.count(tid) > 1`.
- **Why it works:** the replaced task's id is allowed once, while other collisions and
  duplicate ids within a draft are still rejected.
- **Test:** `test_replacing_an_existing_task_id_is_allowed` (`tests/test_planner.py:137`).

### 2. Planner approve tolerates already-committed acceptance tests (`b8ade04`)

- **Before:** `approve` always ran `git commit` for the test paths. When the acceptance
  test was already on the base branch with identical content (a replacement whose test an
  earlier epic had committed), the commit was empty: git exits `1` and prints
  `nothing to commit, working tree clean` on **stdout**, not stderr. `core.host.git`
  reported only stderr, so the owner saw `error: git commit failed (1):` with no detail.
- **Why that was wrong:** a valid replacement failed at approval, and the empty message
  made the failure undiagnosable.
- **Change:** `core/planner_checks.py:156-167` checks
  `git status --porcelain -- <test_paths>`; if nothing is staged it skips the commit and
  uses `result["base_sha"]` as the tests commit. `core/host.py:68-70` falls back to stdout
  when stderr is empty.
- **Why it works:** "the tests are already on the branch" is recorded as the base tip
  without creating an empty commit; new tests still commit and fast-forward as before.
- **Test:** none directly — the existing approve test uses fresh tests, so only the
  commit branch is covered.

### 3. Replace a planner task in place when the replacement keeps its id (`3c2a314`)

- **Before:** `WorkManager.split_task` built `existing` from all tasks and raised
  `DuplicateRecordError: Task already exists: <id>` for a record whose id was already
  present. A same-id replacement therefore passed validation (`56538f9`) but failed at
  approval with `Task already exists: ml-43`.
- **Why that was wrong:** validation and execution disagreed; the replaced task could not
  be re-planned.
- **Change:** `core/work_manager.py:407` excludes the replaced id from the duplicate
  check, and a new branch (`:414-420`) updates the task in place when a record reuses its
  id. The original cancel-and-replace path, including dependency rewiring, runs only for
  new-id splits (`:421-428`).
- **Why it works:** same id means the same identity, so an in-place update is the correct
  replacement and keeps dependents valid; new-id splits are unchanged.
- **Test:** `test_split_task_replaces_a_task_that_keeps_its_id`
  (`tests/test_task_sizing.py:82`); `test_split_task_replaces_in_place_and_rewires_dependents`
  pins the new-id path.
