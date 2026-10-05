# Milestone 3 plan: Hands-off

| | |
| --- | --- |
| Status | **Waiting for owner approval.** No Milestone 3 code before approval. |
| Replaces | `m3-github-review.md` (2026-10-05). Its decisions still hold where they apply: G1 GitHub App, G2 merge commits for `main`, G3 polling. GitHub Issues as task input and a Projects board are deferred. |
| Branch | `m3-hands-off`, worked on in a separate worktree (`~/AI/ms-m3`) while runs use the main checkout |
| Target | Match Legends (public repository, so GitHub Pages is free) |

**Goal for this week.** The owner's whole job becomes:
1. **talk to a planner** about what they want;
2. **play-test `develop` from a link**, on a PC or a phone;
3. **approve releases**.

No terminal chores and no commits by the owner. The six parts below are in priority
order, and each one is usable on its own.

**Safety rules that stay:**
- one worktree per attempt;
- independent verification;
- acceptance and specs edited only by humans, or through the planner flow the owner approves;
- policy by transition (H2) for everything Master does on its own;
- budgets (H4 plus daily caps);
- no secrets in worker environments;
- `main` changes only through a release the owner approves.

---

## 0. Decisions needed from the owner

| # | Question | Recommendation |
| --- | --- | --- |
| **H-D1** | **Auto-integration into `develop`.** This changes approved decisions: integration was human-only (P4/D1, M1), and becomes a ROUTINE system action for `develop` only. | Approve. `main` stays human-only; integration into `develop` still lands exactly the verified commit (fast-forward to the re-verified SHA), so D1 holds for `develop`. |
| **H-D2** | **When a task counts as done.** With auto-integration, a task can wait until it is *integrated*, not just verified. | `completed` = **integrated into `develop`**. This also closes N2: a dependency is satisfied only when its code is in `develop`. |
| **H-D3** | **Rebase conflict.** | The system retries the task once from the new `develop`. That retry counts toward the attempt budget but not toward tier escalation; a second conflict waits for the owner. |
| **H-D4** | **Preview hosting.** One Pages site, two paths: `/` = the released game (`main`), `/develop/` = the preview. Published by pushing a `gh-pages` branch. | Approve. Stable URLs: `https://f0rrel.github.io/Match_Legends_mobile_game/` and `.../develop/`. |
| **H-D5** | **Start "with my PC".** A systemd user service starts when you log in. Starting *before* login needs `loginctl enable-linger`, which may ask for your password once. | Start at login (no admin step). Lingering is optional, a one-line command if you want it. |
| **H-D6** | **Planner tests.** The planner (a model) writes the acceptance tests, which define "done". | The tests are shown to you in the chat and must pass the automatic "fails on base for the right reason" check. Nothing is queued without your "approve", which is recorded as `human_action` with `actor = owner via planner`. The tests are then committed to `develop` and protected as usual. |

---

## 1. `ms`: one command on your PATH — about 0.5 day

- **Installed by the system:** `~/.local/bin/ms`, a two-line wrapper that runs `uv run python -m core.ms` in the Master System repo. There is no shell configuration to edit (`~/.local/bin` is already on PATH).
- **`ms status`** prints plain, readable text for every project:
  - running, idle or paused, and what is running right now;
  - tasks by state (waiting / running / blocked / done in `develop` / released);
  - **"Waiting for you"**: blocked tasks with their reason, planner drafts to approve, and "release ready (N tasks in develop since v0.3)";
  - **today's spend**: Master + planner + worker (priced from `[prices]`), against the daily cap;
  - the `develop` preview URL and the live URL.
- **`ms report [session]`**: the existing report. With no argument, it shows the latest session.
- **`ms stop`**: stop the current run cleanly (it is recorded as stopped by the owner).
- **Later parts add:** `ms pause` / `ms resume` (part 3), `ms chat` (part 4), `ms release` (part 5).

**Built on:** the existing `run_cli` and report code. `run_cli` stays as the lower-level tool.

**Tests:** `ms status` from a fake history and a fake project tree (golden text); `ms stop`
against a running fake session.

## 2. Branch model, auto-integration and the preview link — about 2 days

**Branches in Match Legends:**
- **`main`**: the released game. Protected: only a merged release PR changes it.
- **`develop`**: created once from the current `main` plus `tasks-batch-2`. The system owns
  it. The dedicated clone's `base_branch` becomes `develop`.
- **`attempt/<id>`**: one per attempt. These are pushed only when needed for debugging;
  local by default.
- **`gh-pages`**: the published site.

**Auto-integration (H-D1, H-D2, H-D3).** When an attempt is verified `pass` and its `spec_hash`
matches the current spec, the system integrates it into `develop`:
- **Fast-forward** if `develop` hasn't moved.
- **Otherwise rebase**: cherry-pick onto `develop`, re-run the acceptance on the new commit
  (a new `verification` event bound to that SHA), and fast-forward `develop` to exactly that SHA.
- **On conflict or failed re-verification:** the task is retried from the new `develop`
  (once; then it waits for you).
- **Then** push `develop`, mark the task `completed`, and record an `integration` event with
  `actor = "system"`, `target = "develop"`.
- **Policy:** `integrate_to_develop` is a ROUTINE system step, not a Master operation. Master
  still cannot request anything that touches `main`; `integrate_attempt` into `main` stays
  CRITICAL.

**Preview publishing.** After each integration, the system builds the site:
- `www/` of `main` goes at `/`, and `www/` of `develop` at `/develop/`. It is plain HTML/JS,
  so there is no build step.
- It is pushed to `gh-pages` with the app token.
- A `pages_published` event records the URL and the SHAs.
- Pages updates within about a minute. The URL never changes.

**GitHub App (G1).** Installed only on Match Legends, with the minimum permissions:
- Contents read/write: push `develop`, `gh-pages`, and tags at release;
- Pull requests read/write: the release PR;
- Metadata read.

It is **never able to change `main`**, enforced by a branch ruleset:
- `main`: require a pull request; no bypass for the app; block force pushes and deletion;
- `develop`: block force pushes; only the app may push.

Tokens:
- The app's private key lives in `~/.config/master-system/github-app.pem` (mode 600).
- Tokens are created per use, through a JWT signed with the `openssl` CLI (no new dependency),
  and last 1 hour.
- They never reach history, logs, config files or workers. A test scans for leaks.

**Step-by-step guide for you (written for someone new to GitHub),** delivered as
`docs/github-setup.md` and walked through together:
1. Create the app: Settings → Developer settings → GitHub Apps → New. The plan lists every
   field to fill in, and the permissions above.
2. Generate the private key; save it to the path above with one command.
3. Install the app on **only** Match Legends.
4. Create the two rulesets (Settings → Rules), with screenshots described field by field.
5. Turn on Pages: Settings → Pages → source `gh-pages`, root.
6. Run `ms github check`, which verifies all of the above and reports what is missing in
   plain words.

**Tests:** fake-git scenarios for fast-forward, rebase with re-verification, conflict → retry,
and failed re-verification → retry; Pages layout from two branches; a fake GitHub API for
token minting, push and rulesets; a leak scan.

## 3. Background service and notifications — about 1 day

**`ms daemon`**, run by a systemd **user** service, `~/.config/systemd/user/master-system.service`,
installed by `ms service install` (H-D5). Every few minutes, unless paused, it:
1. syncs from GitHub (G3);
2. finds projects with approved, ready tasks;
3. starts or resumes a session (one project at a time, one run at a time; the existing lock
   still applies);
4. integrates and publishes (part 2);
5. sleeps.

It needs no session commands.

**Caps:**
- **daily cost cap** (`[budget] daily_usd`): Master + planner + priced worker usage today,
  from history; when reached, the daemon pauses until tomorrow and tells you;
- **per-run cap** (the existing `--max-cost-usd`);
- **per-task caps** (H4, part 6);
- **a run-time cap.**

**`ms pause` / `ms resume`** write a pause flag the daemon checks before starting anything. A
running attempt finishes; nothing new starts.

**Phone notifications via ntfy:**
- An HTTP POST to `https://ntfy.sh/<topic>` with `urllib`. The topic is a long random string
  generated at install; you subscribe once in the ntfy phone app (from a QR code shown by
  `ms notify setup`).
- **Events:** batch done, task blocked, needs you, release ready, daily cap reached, the
  service stopped unexpectedly.
- Each message has a 2–4 line summary and the preview link.
- Messages carry no secrets and no code. A topic URL is effectively a password; self-hosting
  ntfy is an option later.

**Tests:** the daemon loop with fake time and a fake run (pause, caps, one-run-at-a-time);
notification payloads; generating the service file.

## 4. Planner chat: `ms chat <project>` — about 2 days

**A conversation in the terminal** (a web chat is later work; this is the one place you type):
- **You describe what you want.** The planner drafts an **epic** split into **tasks**, each
  with:
  - a description;
  - a size (small / medium / hard);
  - acceptance commands, including tests it writes;
  - protected paths;
  - a `manual_check`.
- **It asks questions** whenever something is unclear or is your decision (for example,
  where avatar art comes from). It does not guess.
- **Commands in the chat:** `show`, `change <task>: ...`, `drop <task>`, `approve`,
  `discard`, `release`.

**Automatic checks before you can approve:**
- Each drafted test runs in a fresh worktree of `develop`.
- It **must fail**, and fail for the right reason: an assertion or a missing feature, not a
  syntax error or a broken import in the test itself.
- `npm test` must stay green with the new tests present.
- Results are shown inline. A draft that fails these checks cannot be approved.

**Approve** (`human_action`, `actor = "owner via planner"`, with a hash of the approved draft):
1. the tests are committed to `develop`;
2. the tasks are written through Master (acceptance and `manual_check` are human-only fields,
   written here because you approved them);
3. the daemon picks them up.

**Nothing is queued without "approve".**

**Model:** configurable, `[planner] provider/model`, default DeepSeek V4 Flash.
- Every turn's usage is recorded (`planner_turn` events).
- The chat shows a running cost ("this chat: $0.004").
- The chat has its own cap.

**Context:** the project's README, task list and recent reports, plus the code layout. The
planner reads files through a read-only file tool limited to the dedicated clone, so it can
reference real functions. It never writes code.

**Tests:** a scripted planner model; question/answer turns; approve → tests committed and
tasks queued; the must-fail check with good, already-passing and syntax-error tests; nothing
queued without approval; cost display.

## 5. Release: `ms release <project>` (and "release" in the chat) — about 1 day

1. **Release notes from history:** every task integrated into `develop` since the last
   release tag, with its title, what changed (files and stats), how to check by hand, and its
   verification. Plus the cost of the cycle.
2. **Opens a PR `develop` → `main`** with the notes as its body. You get a notification with
   the link.
3. **You review and merge on GitHub.** That is the release approval (G2: a merge commit).
4. **On the next sync after the merge**, the system:
   - verifies that `main`'s tree equals the released `develop` commit's tree;
   - tags `v0.<n>`;
   - creates a GitHub Release with the notes;
   - republishes Pages, so `/` is now the new `main`;
   - records `release_published`;
   - notifies you "released v0.<n>".

**Tests:** notes from a fake history; the PR body; post-merge steps against a fake GitHub API;
the tree-equality check refuses a mismatched merge.

## 6. Worker tiers with escalation — about 1 day

Unchanged from the approved §5b of the previous plan:
- profiles tier0 (OpenCode free), tier1 (DeepSeek Flash via OpenCode), tier2 (DeepSeek Pro via
  OpenCode), with the paid tiers in their own worker home holding only a spend-limited key;
- a human-only `size` sets the starting tier, and the planner proposes it in part 4;
- after 2 failed semantic attempts on a tier, the task goes up one tier, and never down;
- the orchestrator chooses the tier, Master never names a model, and every attempt records
  profile and model;
- the report and `ms status` show cost and success rate per tier;
- the Ollama/Qwen backend is off the default ladder.

This includes the paid-worker test (G4, decided after tonight's run) and H4's attempt budget
since the last human action.

---

## 7. What fits in this week

| Part | Effort | This week? |
| --- | --- | --- |
| 1. `ms` command | 0.5 day | yes |
| 2. Branch model, auto-integration, Pages, App guide | 2 days | yes, including the guided App setup with you |
| 3. Service, caps, ntfy, pause | 1 day | yes |
| 4. Planner chat | 2 days | likely. It's the riskiest part (prompting and test-checking); a first version without file reading is a fallback. |
| 5. Release | 1 day | probably not this week. It's only needed when you want the first release; the notes can be generated sooner. |
| 6. Worker tiers | 1 day | partly. Profiles and escalation if time allows; otherwise early next week. |

That's about 7.5 days of work against roughly 5–6 working days. Parts 1–3 make the system
run itself; part 4 removes the last terminal chore (writing tasks). The order of the parts
is the order of value, so whatever is unfinished at the end of the week is the
least-needed part.

Also folded in where the parts touch them: H2 (policy by transition) for everything Master
does on its own, part of H4 (per-task budgets), and N2 (closed by H-D2).

## 8. Commit sequence (each one green and pushed)

1. `core/ms.py` + `ms status/report/stop` + install of the wrapper
2. Branch model: `develop` base, auto-integration with re-verification and conflict retry; README §13 (H-D1, H-D2)
3. Pages publishing (`gh-pages`, two paths)
4. GitHub App token minting via `openssl`, push via `GIT_ASKPASS`, leak scan; `docs/github-setup.md`; `ms github check`
5. Guided setup with you; first real auto-integration and preview link
6. `ms daemon`, the systemd user service, daily and run caps, `ms pause/resume`
7. ntfy notifications + `ms notify setup`
8. Planner: chat loop, drafts, questions, the scripted-model tests
9. Planner: the must-fail check, approve → tests committed and tasks queued
10. Release notes + `ms release` PR
11. Post-merge release steps (tag, GitHub Release, Pages)
12. Worker profiles, `size`, escalation, per-tier report
13. Policy by transition (H2) and per-task budgets (H4)

## 9. Out of scope (deferred)

GitHub Issues as task input and a Projects board (maybe later, as a one-way mirror); APK
builds; a web UI for the chat; multiple machines; containers (N1); DBOS (Milestone 4).
