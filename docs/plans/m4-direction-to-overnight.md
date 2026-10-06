# Milestone 4 plan: From direction to overnight work

Status: implemented.

Goal: the owner states a direction and a prioritised backlog once; the service works
through approved backlog items overnight, with typed tasks, shared lessons, a visual
reviewer and generated images, and reports once in the morning. Everything is
project-agnostic: project content lives in the project's repository and `project.yaml`.

## 1. Project direction (`core/direction.py`)

- `project.yaml` → `direction: {path: docs/DIRECTION.md, max_chars: 6000}` (both optional;
  these are the defaults). The file is read from the base branch with `git show`; a
  missing file means "no direction".
- The planner, the Master, the worker and the visual reviewer get the text, truncated to
  `max_chars` with a visible marker. Planner and Master rules: judge every proposal
  against the direction; when a request conflicts, say so and ask instead of drafting or
  acting.
- The direction file is added to every planned task's protected paths: workers cannot
  change it.

## 2. Backlog (`core/backlog.py`, `ms backlog`)

- Epics are milestones with two new optional fields: `priority` (integer, lower first)
  and `summary`. A new milestone status `proposed` marks an unapproved backlog epic (no
  tasks). Approving a planner draft for it turns it into `planned` with its tasks; task
  order is file order.
- `ms backlog <project>` lists epics by priority with state and progress;
  `ms backlog <project> add "<title>" [--summary …] [--priority N]` and
  `ms backlog <project> priority <epic> <N>` are human edits.
- The planner may draft backlog epics (`draft.backlog`), written as `proposed` on approve.
- Ordering: `work_for` and the Master's `ready_tasks` are sorted by (epic priority, epic
  order, task order); the service objective says to take ready tasks in that order.
  Ordering is guidance for the Master, not a policy refusal (a refusal loop would cost
  more than a mis-ordered task).
- The service already works until no task is runnable or the daily cap is reached; at
  that point it sends the morning summary (part 7).

## 3. Task types (`core/task_types.py`)

- Types: `developer`, `visual`, `logic`, `docs`. Each has a generic skill doc
  (`skills/<type>.md` in this repository), default allowed paths and allowed tools.
  `project.yaml` → `task_types.<type>: {allowed_paths, tools, skill}` narrows paths, tools,
  and adds a project skill doc (a path in the project's repository).
- The planner must set `type` on every drafted task. On approve, the type's allowed
  paths are frozen into `acceptance.allowed_paths` (part of the spec hash).
- Enforcement by the orchestrator: the verifier fails an attempt that changes a path
  outside `allowed_paths` (finding `outside_allowed_paths`); worker tools are restricted
  per attempt through OpenCode's inline config (`OPENCODE_CONFIG_CONTENT`: denied tools
  are disabled and denied in `permission`).
- Tasks without a type behave as before (developer, no path limit).

## 4. Shared project memory (`core/lessons.py`, `ms lessons`)

- The worker prompt invites up to 3 `LESSON:` lines at the end of its reply. After a
  **verified pass**, the orchestrator extracts them (bounded length, deduplicated) into
  the pending list of `<projects root>/<project>/lessons.yaml`, tagged with the task type.
- `ms lessons <project>` shows pending lessons and approves or rejects them in a batch
  (`--approve 1,3`, `--approve all`, `--reject …`).
- Only approved lessons for the task's type (or `all`) are added to worker prompts,
  newest first, within a budget. Unapproved lessons are never used anywhere.

## 5. Visual reviewer (`core/visual_review.py`)

- Runs only for `visual` tasks whose acceptance passed, on the attempt's own committed
  result (not on rebase re-verification). It can turn `pass` into `fail`, never the
  reverse; an unavailable reviewer records a note and does not block.
- Screens: `project.yaml` → `visual_review: {viewport: [390, 844], screens: [{name,
  url}], capture: "<command>", max_screens: 4}`. With `capture`, the project's own
  script takes the screenshots (`{out_dir}`, `{screens}`, `{width}`, `{height}`); without
  it, `npx playwright screenshot` captures each `url` (relative to the site directory,
  over `file://`). Capture runs in the attempt worktree with the worker environment (no
  secrets). A task may name `screens`; otherwise all configured screens are used.
- The vision model gets the screenshots, the task (title, description, manual check) and
  the direction, and answers JSON: `{verdict: ok|block, readability, change_visible,
  fits_style, notes}`.
- Model: `[reviewer]` in `config.toml`, default DeepSeek `deepseek-flash` (vision input,
  existing key, ≤1024 tokens per image). Estimated cost per review with 3 screenshots
  and a 6000-character direction: about 6–8k input tokens and 300 output tokens, i.e.
  about $0.002–0.003 at peak prices, half off-peak.
- Screenshots are stored as task artifacts in
  `~/.local/share/master-system/artifacts/<project>/<task>/<attempt>/`; the review (verdict,
  notes, usage, cost, files) is recorded in the verification event and counts toward the
  daily cap.

## 6. Images (`core/images.py`, `ms pick`)

- Workers never hold keys, so images are generated by the orchestrator, not by a worker
  tool. A visual task may declare `assets: [{name, prompt, path, candidates}]` (part of
  the spec). Before such a task can run, the service generates the candidates with the
  project's fixed style prompt (`project.yaml` → `images: {style, width, height}`).
- Providers: Pollinations (`POLLINATIONS_API_KEY`), then Cloudflare Workers AI
  (`CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_API_TOKEN`) as fallback, both from `master.env`.
  No scraping.
- `candidates: 1` → the image is committed to the base branch at `path` by the system.
  `candidates: 3` → the candidates and an HTML contact sheet go to the state directory,
  the task waits, and `ms status` lists it under "Needs you";
  `ms pick <project> <task> <asset> <n>` commits the chosen image (recorded as a human
  action) and the task becomes runnable.

## 7. Morning summary (`core/summary.py`)

- When the service runs out of runnable work or reaches the daily cap after having
  worked since the last summary, it writes one summary (text and HTML with screenshot
  thumbnails) to `~/.local/share/master-system/reports/` and sends one notification.
- Content: tasks done (with manual checks), blocked, screenshots per task, cost by kind
  (Master, planner, workers, reviewer), and what needs the owner (exhausted or blocked
  tasks, image picks, pending lessons, release ready).
- `ms report --summary` prints the latest summary and its HTML path.
- Per-run and per-item notifications ("batch done", "needs you") are off by default
  (`[daemon] batch_notifications`): everything goes into the one summary, and `ms status`
  shows the same "needs you" list at any time. (Changed during the build: keeping
  "needs you" immediate would have meant notifications during the night.)

## 8. Planner: whole epics from the backlog

- The planner sees the backlog (numbered by priority). "Plan epic 1" drafts all tasks of
  that epic in one draft whose `epic.id` is the existing proposed epic; every task has a
  type, tests, test commands, a manual check and, for visual tasks, optional screens and
  assets. Up to 12 tasks per epic.

## Order of work

Each part is one or more commits with tests; the full suite stays green. Then: the
documentation, the managed project's direction file and configuration (in its own
repository and private config), its backlog, and an end-to-end planner draft for its
first epic, stopped before approval.

## Owner setup needed (not blocking the build)

- An image provider key: a free Pollinations key (enter.pollinations.ai) or a Cloudflare
  account ID and API token, in `~/.config/master-system/master.env`.
