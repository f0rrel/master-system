# Milestone 3 plan: Review through GitHub

| | |
| --- | --- |
| Status | **Waiting for owner approval.** No Milestone 3 code before approval. |
| Branch | `m3-github-review` (from `m2-run-for-real` once Milestone 2 is closed and merged) |
| Closes | H2 (policy by transition), H3 (approvals round-trip, via PR review), H4 (attempt budget), N2 (dependencies count only when integrated), P7 (reopen invalidates a pass), L2 (stuck detector) |
| Target | Match Legends (`f0rrel/Match_Legends_mobile_game`) |

**Goal.** The owner works only in GitHub:
- approve an Issue, and it becomes a task;
- review a Pull Request, and that is the decision to integrate.

The system does everything in between and records it in history.

---

## 0. Decisions needed from the owner

| # | Question | Options | Recommendation |
| --- | --- | --- | --- |
| **G1** | **Which GitHub identity does the system act as?** If the system uses *your* token, the PRs are authored by you, and GitHub does not let an author approve their own PR. "Your approval is the integration approval" then cannot work with branch protection. | **A. A GitHub App**, installed on Match Legends only. Its installation tokens are limited to that one repo and expire after an hour; PRs are authored by the app; you approve them. It needs a private key, and JWTs signed with the `openssl` CLI (no new Python dependency). **B. A machine user**, `f0rrel-bot`, collaborator on Match Legends only, with a classic PAT (`repo` scope). Simpler, but the token reaches every repo that user can see, which is only Match Legends as long as nobody adds it elsewhere. | **A.** It is the only option where the token itself is limited to one repo and is short-lived. |
| **G2** | **How integration lands.** D1 says the base moves *exactly* to the verified SHA, but GitHub's merge API creates a merge commit, and branch protection forbids pushing `main` directly. | **A. Merge commit.** Require "branch up to date before merging", so the merge commit's tree is byte-identical to the verified commit's tree and the verified commit is its second parent. The system checks the tree equality before and after. **B. Keep fast-forward.** Allow only the app to bypass protection and update the ref. | **A**, recording `merge_sha`, `verified_sha` and the tree hash. D1 becomes "lands exactly the verified *tree*, with the verified commit as a parent", which needs your approval because it changes an approved decision. |
| **G3** | How the system notices GitHub changes | Polling (`run_cli github sync`, also run at the start of every run); webhooks (need a public endpoint) | **Polling.** No server, and it is enough for one person. |
| **G4** | The harder task for the paid-worker test (§5) | The ml-7 debug panel if the free model fails it tonight; or a new Issue, for example "Battle Arena: three AI difficulty levels" | Decide after tonight's run. |

---

## 1. Issues become tasks

- **Issue template** (`.github/ISSUE_TEMPLATE/task.md` in Match Legends) with headed sections:
  - `Description`;
  - `Acceptance commands` (a list);
  - `Protected paths` (a list);
  - `How to check by hand`.
- **Approval** means a label **`master:approved` added by an allowed login** (config: `[github] approvers = ["f0rrel"]`). The system reads the issue's label events and accepts the label only from an approver.
- **`run_cli github sync`:**
  - Each approved issue becomes or updates a task, through `human_edits`, so every change is a `human_action` with `actor = "github:<login>"` and the issue number.
  - Task id: `gh-<issue number>`.
  - Fields: description, acceptance and `manual_check` from the template sections; the title from the issue title.
  - The **issue body is human input, so it is untrusted text** for prompts. It is quoted as data, never used as instructions to the control plane, and size-capped (the 4,000-character field limit).
- **Editing an approved issue changes the spec** (`spec_hash`). sync then **clears the approval:**
  - the system removes the label and comments "edited after approval, please re-approve";
  - the task is `blocked` until it is approved again.

  Nobody can change a task after you approved it without you seeing it.
- **Closing an issue** makes the task `cancelled`. This is human-initiated, so policy allows it.
- **Tasks created by Master** (`create_task`) are not runnable until a human approves them: policy holds them for approval (§3). In practice, tasks come from Issues.

## 2. Each verified attempt becomes a Pull Request

After an attempt is verified `pass`, the system:
1. Pushes the attempt branch `attempt/<attempt_id>` to GitHub.
   - It uses the app token, through a `GIT_ASKPASS` helper that reads it from the environment.
   - The token never appears on a command line, in history or in logs, and never reaches a worker (the `worker_env` allowlist already blocks it).
   - The dedicated clone's push URL stays disabled, so the system pushes with an explicit URL.
2. Opens a PR into `main`. The title is `<task id>: <task title>`.
3. Writes the PR body from history only:
   - `Closes #<issue>`;
   - **What changed**: files and diff stats, plus the worker's summary *labelled as the worker's claim*;
   - **How to check by hand**: `manual_check`;
   - **Test results**: every acceptance command with its exit code, the verdict, and the protected-path findings;
   - **Cost**: Master and worker tokens and USD for this task;
   - attempt id, base and result SHAs, spec hash, and the log hashes.
4. Records a `pull_request_opened` event (PR number, head SHA).

**One open PR per task.** A later passing attempt replaces it: the old PR is closed with a comment, and a new one opened.

## 3. Your PR approval is the integration approval (H3)

- `github sync` reads PR reviews. Integration happens only if **all** of these hold:
  - an `APPROVED` review by an approver;
  - the PR's head equals the verified SHA;
  - the task's `spec_hash` still matches;
  - no newer attempt exists;
  - the base is an ancestor of the head.
- **If the base moved:** the system does `--rebase`, re-verifies, and force-pushes the attempt branch. The new head then needs a **new approval** (PR review "dismiss stale approvals").
- **Merge:** per G2. The system records an `integration` event with `actor = "github:<approver>"`, the PR number and review id, plus `verified_sha`, `merge_sha` and `tree`.
- **A merge you do yourself in the GitHub UI** is detected and recorded the same way. It is still checked against the verified tree; a mismatch is flagged in the report.
- **"Changes requested"** sets the task to `blocked` and records the review comment. The comment is human text, and Master sees it **quoted as data**, as a reason for a new attempt. Your review comment can therefore steer the next attempt without going through the spec.
- **Approvals for gated operations** (policy, §4) use the same channel:
  - the system opens an Issue "Approval needed: <operation>" whose body includes the operation's hash;
  - your `master:approved` label grants it, closing it denies it;
  - the system records `approval_requested`, `approval_granted` and `approval_denied`, bound to the operation hash and the task's state revision;
  - `resume` applies a granted operation **without a model call**;
  - a stale approval (the state changed since) is refused.

## 4. Policy by transition (H2), attempt budget (H4), and the rest

**Policy by transition.** `requires_approval(operation, current_state)` decides from the transition and the fields, not only the operation name.

| Change | Autonomous? |
| --- | --- |
| task `planned → in_progress`, `in_progress → blocked` | yes |
| task `in_progress → completed` | yes, if the completion gate passes (unchanged) |
| reopen (`completed → *`), cancel (`* → cancelled`), `blocked → planned/in_progress` | human |
| title edits; `create_task` | human |
| milestone `→ completed` | human |
| `run_task` | yes |
| `integrate_attempt` | human (unchanged) |

**Reopen invalidates a pass (P7).** A reopen is a human approval event, and the completion gate requires the latest attempt to start *after* the last reopen.

**Attempt budget (H4).**
- The budget is counted **per task since the last human action on it**: an approval, a spec edit, a review, or a reopen. A new session no longer resets it.
- **Failures are classified.** Infrastructure failures do not count against the budget but are capped separately (3 in a row means the run stops). Infrastructure failures are:
  - `git worktree add` failed;
  - the worker binary is missing;
  - the worker process exited before writing anything, with transport errors in its log.
- **Per-task limits** on worker wall-clock time and cost (claimed tokens priced from the config). Hitting either holds the task for a human.

**Dependencies (N2).** A dependency counts as satisfied only when it is **integrated**, not merely completed.

**Stuck detector (L2).** The run stops with `stuck` if any of these happen:
- the same decision (operation and arguments) three times in a row with no state change;
- a ping-pong between two states;
- three verdicts in a row with the same findings.

Master is told why.

## 5. Paid worker test (one harder task)

1. Create a **spend-limited DeepSeek key** for the worker only (a separate key with a small balance cap), and log it in only in the worker home:
   `HOME=~/.local/share/master-system-worker opencode auth login`
2. Set `[worker] model = "deepseek/deepseek-v4-flash"` for one run, with the task chosen in G4.
3. Run the same task with the free default model and the paid model, in separate sessions.
4. Compare from the reports alone: attempts until a pass, wall-clock time, worker tokens and cost (reported vs priced), and review outcome.
5. Record the result and the choice of default worker in the decision log.

Exit: one run whose report shows the paid worker's cost, measured against the `[prices]` table.

---

## 6. File-by-file changes

| File | Change |
| --- | --- |
| `core/github.py` (new) | GitHub REST over `urllib` (issues, labels, label events, PRs, reviews, merges, refs); app JWTs via the `openssl` CLI (G1 = A); `GIT_ASKPASS` helper. No new dependency. |
| `core/github_sync.py` (new) | issue → task sync; PR open/replace; review → integrate / block; approval issues |
| `core/attempts.py` | integration by merge commit with tree equality (G2 = A); records the PR, review and actors |
| `core/reasoning.py` | `requires_approval(operation, state)`; the transition table |
| `core/evidence.py` | budget since the last human action; failure classification; reopen-aware gate; dependencies satisfied only when integrated |
| `core/work_manager.py` | `calculate_readiness` takes an "integrated" predicate (justified: N2 is a readiness rule) |
| `core/autonomous_loop.py` | stuck detector; applying granted approvals without a model call; infrastructure-failure cap |
| `core/history.py`, `core/sqlite_history.py` | events `pull_request_opened`, `review_observed`, `approval_requested/granted/denied`, `issue_synced`; schema v4 with backup |
| `core/run_cli.py`, `core/run_config.py` | `github sync`, `github status`; `[github]` config (repo, app id, key path, approvers); the token is never in config |
| `core/report.py` | PR, review and approver per completion; budget and infrastructure failures |
| Match Legends | issue template; branch protection (below), set up by the owner |

**Branch protection on Match Legends `main`** (set up by you, documented step by step):
- require a pull request with 1 approval;
- dismiss stale approvals;
- require the branch to be up to date;
- block force pushes and deletions;
- do not allow bypassing;
- optionally, a GitHub Actions job that re-runs `npm test`, as an independent check.

## 7. Acceptance (README §12 Milestone 3, revised)

- [ ] An Issue approved by the owner becomes a task. Editing it after approval clears the approval.
- [ ] A verified attempt opens a PR whose body shows what changed, how to check by hand, test results and cost.
- [ ] Integration happens only after the owner's PR approval. The integrated tree equals the verified tree, and history records who approved it.
- [ ] An operation approved in GitHub resumes and applies exactly that operation, with no model call. A stale approval is refused.
- [ ] Reopen, cancel, title edits and milestone completion wait for a human. A reopen invalidates an earlier pass.
- [ ] A new session doesn't reset the attempt budget; infrastructure failures don't spend it.
- [ ] A dependency that is completed but not integrated doesn't make a task ready.
- [ ] One paid-worker run on a harder task, with its cost in the report.
- [ ] No token appears in history, logs, config, or a worker's environment (tested).

## 8. Tests

**Offline, the default suite:**
- A fake GitHub API (a local `http.server` in a thread) drives the scenarios:
  - issue approved → task;
  - edit after approval → approval cleared;
  - pass → PR body contents;
  - approval → merge with tree equality;
  - "changes requested" → task blocked, with the comment shown quoted to Master;
  - a stale head → refused;
  - a manual UI merge → observed and recorded;
  - an approval issue → operation applied with no model call;
  - a stale approval → refused.
- Transition-table tests for every status pair.
- Budget across sessions; failure classification.
- Stuck-detector patterns.
- Token leakage: scan history, logs and worker environments.

**Integration (opt-in, marker `integration`):**
- one real round trip on a throwaway GitHub repo;
- then one real task on Match Legends with you approving the PR.

## 9. Commit sequence (each one green and pushed)

1. Policy by transition (H2) + reopen-aware gate (P7)
2. Attempt budget since the last human action + failure classification (H4)
3. Dependencies satisfied only when integrated (N2)
4. Stuck detector (L2)
5. `core/github.py` + the fake-API test harness
6. Issue → task sync
7. Attempt → PR
8. Review → integration (G2) and "changes requested"
9. Approval issues for gated operations (H3)
10. Report additions
11. README; branch-protection guide for you; issue template PR to Match Legends
12. Integration test on a throwaway repo, then a supervised real round trip
13. The paid-worker test (§5)

## 10. Out of scope

Webhooks or any server; several repositories; GitHub Actions as the verifier of record (it can be an extra check); auto-merge without your review; containers (N1); DBOS (Milestone 4).

## 11. Cost and time

- **GitHub API calls** are free (rate limits are far above the need).
- **Master cost:** M2 measured about $0.0012 per decision, so a five-task night costs about $0.03–0.05.
- **The paid worker test:** a few cents to about $1, depending on the task.
- **Claude Code time:** about 12 commits, similar in size to Milestone 2. Commits 5–9 carry the most risk (GitHub semantics), and the fake-API harness keeps them cheap to test.
