# Milestone 2 plan: Run it for real

| | |
| --- | --- |
| Status | **Approved by the owner (2026-10-05)** with D1–D4 decided (§0) and one addition: the five tasks must be independent (§2.3). |
| Branch | `m2-run-for-real` (from `main` = `e68cea8`, tag `m1-contain-verify`) |
| Closes | H5. Partly M4 (human edits made through `run_cli` are recorded) and L7 (if D1 = A). |
| Baseline | 1140 passed, 1 skipped, 5 integration tests deselected |

README §12 objective: a `run_cli` (start/resume/status), a verifier, one real worker,
and a throwaway repo with 5 tasks, run overnight. Acceptance:

- 5 tasks completed and integrated, with every human touch recorded in history;
- every completion traces to a decision, an attempt and a result SHA;
- a per-run report of steps, attempts, wall-clock time and tokens, generated from history alone.

Decided by the owner: the worker is the **OpenCode CLI** (ACP stays an option). The
target is **Match Legends**, managed in a **dedicated clone**. Workers get **no
secrets** in their environment.

---

## 0. Owner decisions (2026-10-05)

- **D1 = A.** `integrate --rebase` is human-only. It cherry-picks onto the current base,
  re-runs acceptance on the new commit, and refuses on conflict. The re-run is recorded
  as a **new `verification` event bound to the rebased SHA**, and integration
  fast-forwards **exactly to that verified SHA**, nothing else.
- **D2:** the Master default is **DeepSeek V4 Flash**, configurable.
- **D3:** the worker gets a dedicated, spend-limited key in its own worker home. The
  environment allowlist (§1.3) is approved; no other secrets reach workers.
- **D4:** one real probe was approved. Findings below.
- Also approved: the human-only `description` (in `spec_hash`), the disabled push URL,
  `human_action`, and token accounting in the providers (worker usage as a claim).
- **Addition:** the five overnight tasks must be independent: no `depends_on`, and none
  may need another task's code, because attempts start from the base branch. New gap
  N2 in README §11; its fix (dependencies count only when integrated) is Milestone 3.

### D4 probe findings (one real call, 2026-10-05)

The call was `opencode run --format json --dir <tmp git repo> "Create a file named
hello.txt ... containing exactly the text: hi"`, using the owner's existing OpenCode
setup (empty `opencode.json`, so the default model) and **without `--auto`**.

- **Edits inside `--dir` need no `--auto`.** `hello.txt` was written; exit code 0,
  about 2 s, nothing on stderr.
  - `--auto` stays **off**.
  - Whether a shell command (`bash` tool) or a write outside `--dir` would prompt and
    stall in run mode was **not** probed: one call only. The worker home's
    `opencode.json` will set permissions explicitly. This is checked in the
    supervised smoke run (step 4) before any overnight run.
- **The event stream** is one JSON object per line on stdout, with
  `{type, timestamp (ms), sessionID, part}`. Types seen:
  - `step_start`;
  - `tool_use` (`part.tool`, `part.state.{status,input,output}`);
  - `text` (`part.text`: the final summary);
  - `step_finish` (`part.reason`: `tool-calls` or `stop`; `part.tokens`:
    `{total, input, output, reasoning, cache: {read, write}}`; `part.cost` in USD).
- **Usage:**
  - **Tokens:** `worker_reported_usage` is the sum over all `step_finish` events of
    `input`, `output`, `reasoning`, `cache.read` and `cache.write`. `input` excludes
    cached tokens (the probe: 6,053 + 273 input, 1,792 + 7,680 cache reads,
    100 output).
  - **Cost:** the probe reported `cost: 0` (the default model is free). `reported_cost_usd`
    is the sum of `part.cost`; the price table is the fallback when it is 0 and the
    model is known to be paid.
- **No model name appears in the events.** The configured worker model (`--model`, from
  config) is recorded as opaque provenance for pricing.
- **OpenCode keeps its own state** (`opencode.db`, a `snapshot/` git store,
  `tool-output/`) under `$XDG_DATA_HOME/opencode`. With `HOME`/`XDG_*` pointing into the
  worker home (§1.3), that state lands in the worker home, not in the owner's.

## 0b. Decisions as originally proposed

| # | Question | Options | Recommendation |
| --- | --- | --- | --- |
| **D1** | **Integration of several tasks from one run.** Milestone 1 integrates by fast-forward only, and every attempt starts from `base_branch`. In an overnight run nothing is integrated until the morning, so all 5 attempts start from the same base. Once the first is integrated, the others no longer fast-forward and are refused (`base_moved`). Without a change, at most one task per run can be integrated. | **A.** `integrate --rebase`: cherry-pick the attempt's commits onto the current base in a fresh worktree, **re-run the task's acceptance on the new commit**, and fast-forward only if it passes (refuse on conflict). Human-only and recorded, as today. **B.** Supervised run: you integrate each task as soon as it completes, and the run waits for you. Not overnight. **C.** Make the 5 tasks touch disjoint files and accept the conflicts we still get. | **A**, plus tasks chosen to touch mostly separate files (C as mitigation). A keeps evidence bound to exactly what is merged: the rebased commit is re-verified, never trusted. |
| **D2** | Default Master provider | DeepSeek V4 Flash; DeepSeek V4 Pro; Ollama (local, free); OpenCode server | **DeepSeek V4 Flash** (cheap, follows strict JSON well enough), configurable per run. Ollama `qwen2.5-coder:7b` is unlikely to hold the two-step `run_task` protocol reliably. |
| **D3** | Worker model inside OpenCode | Whatever the worker's OpenCode config uses (for example Big Pickle on OpenCode Zen, or a DeepSeek key) | A **dedicated, spend-limited key** in the worker's own OpenCode home (§2.3). It is the one credential the worker must be able to read, so it should be cheap to revoke. |
| **D4** | Spike before coding | One real `opencode run --format json` call on a toy prompt (a few cents at most), to confirm the JSON event schema (token/cost fields) and whether `--auto` is required for edits in run mode | Approve the spike; its findings go into this plan before commit 4. |

---

## 1. Design

### 1.1 `core/run_cli.py`: the composition root

```text
python -m core.run_cli start  <project> --objective TEXT [--session ID] [--until-stopped]
python -m core.run_cli resume <session> [--until-stopped]
python -m core.run_cli status [<project>]      # sessions, open attempts, pending approvals, lock holder, last events
python -m core.run_cli report <session>        # §1.6, from history only
python -m core.run_cli task describe <project> <task> --text TEXT       # human edit, recorded
python -m core.run_cli task set-acceptance <project> <task> ...          # human edit, recorded
python -m core.run_cli integrate <project> <attempt> [--rebase]         # wraps core.attempts
```

- **Wiring.** It builds `Master(root)`, the configured `ReasoningProvider`,
  `OpenCodeCliBackend`, the existing `AcceptanceVerifier`, `SQLiteHistoryStore` and
  `FileSessionStore` in the state dir, and runs through **`SessionRunner`**. Nothing
  bypasses the lock, recovery, the completion gate or the attempt limit.
- **`--until-stopped`** keeps calling `resume` while the stop reason is `step_limit`,
  bounded by `--max-runs` (default 10) and `--max-hours` (default 8). It stops on
  anything else: `approval_required`, `attempt_limit`, `no_actionable_work`, an error,
  or Ctrl-C (recorded as `run_error`).
- **Configuration.** `~/.config/master-system/config.toml` is read with the standard
  library's `tomllib` (no new dependency); CLI flags override it. It holds the
  provider and model, the price table, the worker settings (OpenCode binary, model,
  worker home, flags), timeouts, and `max_steps`. No secrets: the Master's API key
  stays in `run_cli`'s own environment.
- **No new frameworks.** argparse, tomllib and the existing modules only.

### 1.2 Worker: the OpenCode CLI backend

Adapter-only changes (justified: the worker needs a model choice, structured output
for accounting, and non-interactive edits):

- constructor options `model`, `extra_args` (for example `["--auto"]`, pending D4), and `format_json=True`;
- the prompt carries the task's **description** and its acceptance commands and
  protected paths, stated as constraints ("do not modify these paths; these commands
  must pass");
- with `--format json`, the backend parses the event stream into the summary and a
  **`worker_reported_usage`** (tokens/cost, if the events carry them; D4 confirms).
  It is recorded in `attempt_finished` as a **claim**, used for accounting only and
  never for authority.

**Gap found:** tasks have only a `title`. The worker needs more than a title to do real
work. Proposal: an optional `description` on tasks with the same status as
`acceptance` (human-only, absent from `SPECS` and `MUTABLE_TASK_FIELDS`), included in
`spec_hash` (which becomes title + description + acceptance). Attempts recorded under
the old hash become `spec_changed`; there are no real runs yet, so nothing is lost.

### 1.3 Workers get no secrets

- Today `AttemptWorkspace.run(env=None)` **inherits the full environment** of `run_cli`,
  including `DEEPSEEK_API_KEY`. **This must change before any real run.**
- The orchestrator builds the environment of every worker and verification process
  from an **allowlist**:
  - `PATH`, `LANG`, `LC_ALL`, `TERM`, `TZ`;
  - `HOME` = the **worker home** (`$XDG_DATA_HOME/master-system-worker/`);
  - `XDG_CONFIG_HOME`, `XDG_DATA_HOME` and `XDG_CACHE_HOME` inside it;
  - `PLAYWRIGHT_BROWSERS_PATH` (a shared, read-mostly browser cache);
  - `npm_config_cache`;
  - `CI=1`.

  Nothing else. In particular no `*_KEY`, `*_TOKEN`, `SSH_AUTH_SOCK` or `GIT_ASKPASS`.
- The worker home holds only the worker's OpenCode config and credential (D3). You log
  it in once with `HOME=<worker home> opencode auth login`.
- **Limit (N1):** this removes secrets from the *environment*. A worker still runs as
  your user and could read files in your real home. Containers remain the real fix.

### 1.4 Target project: a dedicated Match Legends clone

- **Clone:** `~/AI/managed/match-legends`, separate from your working copy, from the
  control-plane repo, from the projects root and from the state dir (the isolation
  rule checks this).
- **Push disabled in the clone:**
  `git remote set-url --push origin DISABLED-by-master-system`, so neither a worker nor
  an integration can push to GitHub. You push reviewed results yourself, with an
  explicit URL.
- **Project definition:** `projects/match-legends/` in this repository, with
  `repository: ~/AI/managed/match-legends`, `base_branch: main`, one milestone and the 5
  tasks (§2). Task status changes made by runs show up as a git diff of this directory,
  which you review and commit; that is an audit trail of its own.
- **Workspaces:** each attempt is a worktree of the dedicated clone under
  `master-system-worktrees/match-legends/<attempt>`.

### 1.5 Recording human touches and completion traces

For "every human touch recorded":

- New event type **`human_action`** (`actor: "human-cli"`, `action`, `task_id`,
  `spec_hash_before`, `spec_hash_after`). `run_cli task …` writes it after the
  Master write. A schema v3 migration, with backup as in v2.
- Integration is already recorded (`integration`).
- Direct YAML edits or `core.master` CLI edits are **not** recorded (M4). The report
  flags any spec change that no `human_action` explains, as "unrecorded human edit".
  For M2, human edits go through `run_cli`.

For "every completion traces…":

- The `decision` event of an autonomous completion gains **`gate_attempt_id`**, the
  attempt whose evidence authorised it.
- The report then shows the chain:
  `decision(seq)` → `attempt_id` (`base_sha`, `spec_hash`) → `result_sha` → `verification` → `integration`.

### 1.6 The per-run report: from history alone

`run_cli report <session>` reads **only** `history.sqlite`. A test proves it by
deleting the projects root before generating it. It shows:

- **Steps:** decisions per run, stop reasons, and runs interrupted or errored.
- **Attempts:** per task, by outcome (finished / timed_out / error / interrupted) and
  verdict; attempt wall-clock time (from the `created_at` of `attempt_started` and
  `attempt_finished`).
- **Wall-clock:** from the first `run_started` to the last run end; active time vs. idle.
- **Tokens and cost:**
  - Master: from `decision.usage`, which the providers must start reporting (below);
  - worker: from `worker_reported_usage`, labelled as claimed;
  - cost = tokens × the config price table, with the model taken from the recorded
    provenance.
- **Human touches:** `human_action`, `integration`, plus unexplained spec changes.
- **Completion traces:** §1.5.

**Missing for token accounting, to add:**

- **`ReasoningProvider.last_usage`**, optional and `None` by default. Neutral keys:
  `input_tokens`, `output_tokens`, `cached_input_tokens`, `reported_cost_usd`.
  - **DeepSeek:** the response's `usage` (`prompt_tokens`, `completion_tokens`,
    `prompt_cache_hit_tokens`).
  - **Ollama:** `prompt_eval_count` and `eval_count`.
  - **OpenCode server:** the message's `tokens` and `cost` fields, if present.
- **The engine** copies it into the `decision` event, with the provider and model as
  opaque provenance (`reasoner`), which is needed for pricing and never branched on.
  Retried unusable replies still cost tokens: the engine sums their usage into
  `run_stopped.detail_usage` (or the next decision), so failed calls are counted too.
- **The worker:** `worker_reported_usage` from OpenCode's JSON events (D4).

### 1.7 Cost per run (estimate)

Assumptions: 5 tasks, about 6 Master decisions per task (start, run, sometimes re-run,
complete, plus waits): about 30 decisions per run.

**Master**, at about 6k input and 300 output tokens per decision:

| Model | Input | Output | Cost per run |
| --- | --- | --- | --- |
| V4 Flash (peak) | 180k tokens at $0.44/M | 9k tokens at $1.32/M | **≈ $0.09** (lower off-peak or with cache hits) |
| V4 Pro (peak) | at $1.32/M | at $3.96/M | **≈ $0.27** |

**Worker:** this dominates. A coding-agent attempt that reads the 1,500-line game file
uses roughly 100–300k input tokens. 5 tasks × about 1.5 attempts × 200k ≈ 1.5M input
plus about 50k output:

| Worker model | Cost per run |
| --- | --- |
| V4 Flash | **≈ $0.70** |
| V4 Pro | **≈ $2.20** |
| A free model | $0 |

**Total: about $0.10–$2.50 per overnight run**, depending mostly on the worker model.
The report replaces these estimates with measured numbers after the first run.

Prices: DeepSeek V4 Flash $0.22/$0.66 per M off-peak and $0.44/$1.32 peak; V4 Pro
$0.66/$1.98 off-peak and $1.32/$3.96 peak ([cloudzero](https://www.cloudzero.com/blog/deepseek-pricing/),
[benchlm](https://benchlm.ai/deepseek/api-pricing)).

---

## 2. Match Legends

### 2.1 What it is today (read from `f0rrel/Match_Legends_mobile_game`, `main` = `5875c5a`)

- **Code:** a Capacitor 6 app. The whole game is one classic `<script>` in
  `www/index.html` (1,479 lines). Root `index.html` is a byte-identical copy.
- **Tooling:** no tests and no `test` script. Node 18.19 is installed (`node --test` is available).
- **Pure logic is already isolated in the file, but not loadable:**
  - hex geometry: `buildHexCells`, lines, `hexNeighbors`, `isHexAdjacent`, `swapHex`;
  - `findMatches`, `hasPossibleMove`, `createBoard` / `createPlayableBoard`, `computeCollapse`;
  - power targeting in the power-button handler (`clearRow`, `clearColumn`, `clearArea`
    with chain lightning, `clearType`, `convertTiles`);
  - scoring inside `resolveCascade`.

  Randomness comes from `Math.random` throughout.

### 2.2 Prep work (with you, directly in that repo, on branch `system-prep`, outside the system)

1. **Delete the duplicate root `index.html`** (keep `www/index.html`), and update the README's "cp index.html" step.
2. **Extract the pure logic** into `www/js/match-logic.js`:
   - a classic script that sets `window.MatchLogic`, and also `module.exports` when
     loaded by Node; no bundler, no ES-module conversion, and `file://` still works;
   - contents: constants, hex geometry, lines, `findMatches`, `hasPossibleMove`,
     board creation and `computeCollapse` (taking an `rng` parameter that defaults
     to `Math.random`), and the power *targeting* functions (which cells each power
     clears, given a board and an rng);
   - `index.html` loads it with `<script src="js/match-logic.js">` and calls it;
     rendering and UI stay where they are.
3. **Unit tests**, `tests/unit/*.test.js`, with `node:test` and no new dependency:
   - the board has 61 cells and the 3 axis line groups;
   - adjacency;
   - `findMatches` on hand-built boards, including runs of 4 and 5 (`runLen`);
   - a fresh board has no matches;
   - `hasPossibleMove` true and false cases;
   - `computeCollapse` keeps survivors in order and fills from `rMin`;
   - each power's targeted cells on a fixed board.
4. **One headless smoke test** with Playwright (`@playwright/test` as the only new dev
   dependency, Chromium installed once with `npx playwright install chromium`) in
   `tests/smoke/game.spec.js`:
   - open `www/index.html` over `file://`;
   - assert no console errors or page errors;
   - start level 1;
   - find a legal swap through `window.MatchLogic` on the live board (a small read-only
     test hook, `window.__ML_TEST__ = { board: () => ... }`);
   - drive it with pointer events;
   - assert that the moves counter dropped by one.
5. **One `npm test`** (`node --test tests/unit && playwright test`), plus `test:unit`
   and `test:smoke`. Add `playwright.config.js`, and ignore `test-results/` and
   `playwright-report/` in `.gitignore`.
6. **The five tasks' acceptance tests** (§2.3), written now in `tests/tasks/` and
   **expected to fail** until each task is done. `npm test` excludes them. They
   *are* the definition of done, written by a human, which is why `tests/*` is protected.

After you merge `system-prep` into Match Legends `main`: create the dedicated clone (§1.4).

### 2.3 Five tasks for the overnight run

Each task's `acceptance` runs exactly what it needs, never `npm test`, because the
other tasks' tests fail until their own tasks are done:

```yaml
commands:
  - npm ci --prefer-offline --no-audit --no-fund
  - node --test tests/unit
  - node --test tests/tasks/<task>.test.js       # or: npx playwright test tests/tasks/<task>.spec.js
  - npx playwright test tests/smoke
protected_paths: ["tests/*", "package.json", "package-lock.json", "playwright.config.js"]
```

`package.json` is protected because it defines the scripts and dependencies; none of
these tasks needs a new dependency.

**Independence rule (owner addition).** No `depends_on`, and no task needs another's
code. Re-checked against the original proposal:

| Task | Independent? | Why |
| --- | --- | --- |
| ml-1 seeded-rng | yes | Prep already threads an `rng` parameter (default `Math.random`) through the board functions; ml-1 only adds `createRng(seed)` and uses it per level. |
| ml-2 shuffle-in-place | **was not** (`depends_on ml-1`) → **fixed** | Its test now supplies its own deterministic rng function, so it needs only the `rng` parameter that prep provides, not ml-1's `createRng`. `depends_on` removed. |
| ml-3 scoring-rules | yes | Moves existing scoring code; no other task's code. |
| ml-4 hint | yes | Uses only `hasPossibleMove`-style logic already in the base. |
| ml-5 persist-progress | yes | Only the `Storage` object. |

Tasks 1–4 all add to `match-logic.js`, so a rebase can still conflict on adjacent
hunks. To limit that, prep creates one marked, empty section per task in the file, and
each task's description names its section.

| Task | What | Acceptance test (written in prep) | Files mostly touched |
| --- | --- | --- | --- |
| **ml-1 seeded-rng** | `MatchLogic.createRng(seed)` (mulberry32) and an `rng` passed through every random choice, so a board can be reproduced from a seed | The same seed gives identical playable boards; different seeds differ; a seeded board has no matches and has a move | `match-logic.js` |
| **ml-2 shuffle-in-place** | Replace "No moves left — board reshuffled" (which today deals a *new* board) with `shuffleBoard(board, rng)`: permute the existing tiles into a match-free, playable arrangement | The multiset of tile types is preserved, there are no matches, `hasPossibleMove` is true, and it is deterministic **for an rng the test supplies itself** (so it does not need ml-1's `createRng`) | `match-logic.js` + 3 lines in `index.html` |
| **ml-3 scoring-rules** | Move scoring out of `resolveCascade` into `scoreMatch({size, maxRun, combo, buffMult})`, with the same rules (4-run ×1.5, 5-run ×2, combo multiplier, buff rounding) | A table of cases, including buff rounding | `match-logic.js` + `resolveCascade` |
| **ml-4 hint** | `findHint(board)` returns an adjacent pair whose swap matches, or `null`; a 💡 button highlights it | Unit test of `findHint` on hand-built boards; Playwright: the button exists and highlights exactly 2 tiles | `match-logic.js`, `index.html` (new button) |
| **ml-5 persist-progress** | `Storage` falls back to `localStorage` when `window.storage` is absent, as the README suggests | Playwright: select a free avatar, reload, and the selection persists | `index.html` (`Storage` object only) |


---

## 3. How the M2 acceptance criteria will be checked

| Criterion | Check |
| --- | --- |
| 5 tasks completed and integrated, every human touch recorded | `run_cli report` shows 5 `integration` events (one per task) and every `human_action`, and reports **no** unexplained spec change. `run_cli status match-legends` shows the 5 tasks `completed`. The dedicated clone's `main` contains the 5 integrated SHAs. |
| Every completion traces to a decision, an attempt and a result SHA | The report's completion traces: for each task, `decision` (with `gate_attempt_id`) → attempt (`base_sha`, `spec_hash` equal to the task's spec at completion) → `result_sha` → verification `pass` → integration (`result_sha`, or the rebased SHA with its re-verification). An offline test asserts the chain on a fake run. |
| Per-run report from history alone | The report runs with the projects root removed (test), and prints steps, attempts, wall-clock, tokens and cost. Token totals are checked against provider usage in the providers' unit tests. |

---

## 4. File-by-file changes (M2 code, after prep)

| File | Change |
| --- | --- |
| `core/run_cli.py` (new) | Commands §1.1; `--until-stopped` |
| `core/run_config.py` (new) | `tomllib` config, defaults, price table, validation |
| `core/report.py` (new) | Report from a `HistoryStore` only |
| `core/provider.py` | `last_usage` (default `None`) |
| `core/deepseek_provider.py`, `core/ollama_provider.py`, `core/opencode_provider.py` | Fill `last_usage` from the API responses (adapter changes; justified: token accounting is an M2 criterion) |
| `core/reasoning_engine.py` | Record `usage` and opaque `reasoner` provenance in decisions; `has_description`; prompt text for descriptions |
| `core/autonomous_loop.py` | `gate_attempt_id` in completion decisions |
| `core/opencode_backend.py` | `model`, `extra_args`, JSON event parsing, `worker_reported_usage`, prompt with description and acceptance (adapter) |
| `core/execution.py` | `ExecutionResult.usage` (optional, neutral keys) |
| `core/task_orchestrator.py`, `core/workspace.py` | Worker environment allowlist (§1.3); `worker_reported_usage` in `attempt_finished` |
| `core/history.py`, `core/sqlite_history.py` | `human_action` event; schema v3 migration with backup |
| `core/project_state.py`, `core/work_manager.py`, `core/master.py` | `description` (human-only, validated, `set_task_description`). Justified: the worker needs a spec beyond the title, and humans set it through Master. No new imports in `master.py`. |
| `core/evidence.py` | `spec_hash` covers `description`; expose the gate's attempt id |
| `core/attempts.py`, `core/workspace.py` | `integrate --rebase` with re-verification (if D1 = A) |
| `projects/match-legends/` (new) | Project, milestone and the 5 tasks with acceptance (after prep) |
| `README.md` | §2, §8, §10, §11, §12, §13 |

## 5. Commit sequence (each one green and pushed)

1. Spike notes (D4) into this plan; no code.
2. `run_config` + defaults.
3. Worker environment allowlist (no secrets) + tests.
4. Task `description` (human-only; in `spec_hash`) + the OpenCode backend's model/flags/JSON parsing/usage/prompt.
5. Provider `last_usage` + engine recording (`usage`, `reasoner`) + the retried-reply total.
6. `gate_attempt_id` in completion decisions.
7. `human_action` event, schema v3 migration with backup, `run_cli task` commands.
8. `run_cli` start / resume / status / `--until-stopped`.
9. `report` (history only) + completion traces.
10. `integrate --rebase` with re-verification (D1 = A).
11. `projects/match-legends/` definition (after prep is merged in Match Legends).
12. A dry run on a tiny local toy repo with a scripted fake worker (offline test), then the **first real run** together, then the overnight run.
13. README updates; tick the criteria as they pass.

## 6. Tests mapped to the criteria

- **Environment:** the worker and verifier processes see none of `DEEPSEEK_API_KEY`,
  `*_TOKEN` or `SSH_AUTH_SOCK`, even when `run_cli`'s environment has them, and `HOME`
  is the worker home.
- **Report:** generated with the projects root deleted; steps, attempts, wall-clock,
  tokens and cost match a scripted run; failed-reply tokens are counted.
- **Traces:** each autonomous completion's `gate_attempt_id` points to a `pass` with
  the right `spec_hash`.
- **Human touches:** `run_cli task describe` / `set-acceptance` write `human_action`;
  a direct YAML edit appears as "unrecorded human edit" in the report.
- **Rebase:**
  - a moved base with a clean cherry-pick is re-verified and fast-forwarded;
  - a conflict is refused;
  - a re-verification `fail` is refused;
  - each case records exactly what happened.
- **CLI:**
  - start/resume/status end to end with a fake worker;
  - `--until-stopped` stops on each non-`step_limit` reason, on `--max-runs` and on `--max-hours`;
  - Ctrl-C leaves the session `stopped (error)`.
- **Providers:** `last_usage` is parsed from recorded API responses (DeepSeek, Ollama, OpenCode server).
- **Worker:** the OpenCode JSON event parsing is tested against a recorded event
  stream from the D4 spike.

## 7. What I expect to break in the first real run

1. **OpenCode in run mode refuses to edit files** without permission (`--auto`
   needed, D4), or asks a question and stalls until the deadline.
2. **Master protocol slips.** Master skips `update_task → in_progress` before
   `run_task`, puts two tasks in progress, or retries a failing task until the attempt
   limit. Expect `operation_failed` and `attempt_limit` stops in the first runs; the
   prompt will need tuning.
3. **`npm ci` in every fresh worktree** is slow and needs the network. With the
   scrubbed `HOME`, the npm cache and Playwright's browser path must be set
   explicitly, or verification fails as `fail` for the wrong reason.
4. **Flaky smoke test:** WebAudio, `navigator.vibrate`, animation timing and pointer
   events in headless Chromium. The first runs will show whether the scripted move
   needs waits.
5. **Token-heavy attempts:** the worker re-reading `index.html`. Expect timeouts at
   30 minutes on the first tries, and costs at the high end of §1.7.
6. **Integration conflicts** between tasks 1–4 (all touching `match-logic.js`) even
   with `--rebase`, so the affected task is re-run from the new base.
7. **The worker editing `tests/tasks/*`** to "make it pass": correctly caught as
   `protected_path` fail, but it burns attempts.
8. **Disk use:** a worktree plus `node_modules` per attempt (Capacitor packages and
   Playwright), never cleaned up (L6).
9. **Human edits made outside `run_cli`** appearing as unexplained spec changes.

## 8. Out of scope

Containers or sandboxing (N1), the approval round-trip (Milestone 3), policy by
transition, the attempt-budget change, DBOS, multiple projects, a dashboard,
self-hosting, ACP, automatic integration.
