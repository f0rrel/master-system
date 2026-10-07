# Troubleshooting

Start with `ms status`. If the symptom is not listed below, run `ms doctor` and include
its output (it contains no keys, tokens or notification topic) when asking for help.
Paths: `$MS_HOME` is the repository; `<clone>` is a managed project's dedicated clone
(`repository` in its `project.yaml`).

## Common problems

| Symptom | Meaning | What to check or run |
| --- | --- | --- |
| `ms: command not found` | The wrapper is not on `PATH` | `cd "$MS_HOME" && uv run python -m core.ms install`; make sure `~/.local/bin` is on `PATH` |
| `ms status` says **Paused** | `ms pause` or `ms stop` was used | `ms resume` |
| Tasks are listed as upcoming but nothing runs | The service is stopped or paused, the daily cap is reached, or the project is stalled | `systemctl --user status master-system.service`; `ms status` names the reason; `journalctl --user -u master-system.service -n 30` |
| Notification `<project>: needs you` | The last run made no progress; the service waits for a human | `ms report`; revise the task in `ms chat` (any recorded change resumes work) |
| Notification `<project>: <task-id> needs you` | The task failed its attempt budget: too hard or underspecified for the worker | `ms report` shows the failing test output; describe the task more precisely, or split it, in `ms chat` |
| Notification `Daily budget reached` | `[budget] daily_usd` was hit | Wait for the next day, or raise the cap and restart the service when Idle |
| Planner: "this chat reached its cap" | `[planner] chat_usd` was hit | `ms chat <project> --new`, or raise the cap |
| A multi-line paste arrived as several messages | The terminal does not use bracketed paste | Put the text between two `"""` lines, or use `ms chat <project> --file <path>` |
| `check` fails: "passes already on the current code" | The drafted test does not test anything new | Ask the planner for a test that fails until the feature exists |
| `check` fails: "the test itself is broken", or a syntax check fails | Syntax error or missing helper in the drafted test | Paste the failure to the planner and ask it to fix the test |
| The planner drafts tests in the wrong language or location | The project's planner conventions are not configured | Set `planner.test_suffixes`, `test_command_examples` and, optionally, `syntax_check` and `test_guidance` in `project.yaml` |
| "No projects yet: … does not exist" (`ms status`, `ms doctor`, the service log) | The projects root is missing; the service idles until it exists | `ms install` creates it; then add a project (`~/.config/master-system/projects/<id>/`), or set `[run] projects_root` |
| `Projects root not found` (a traceback, older versions) | The same, before this hint existed; the service exited and systemd restarted it every minute | Update, or create the directory |
| No preview link in `ms status` | The project has no `github.site_dir` | Add `site_dir` if the project has a static site to preview |
| A task "waits for images" and nothing generates them | No image provider key | Add `POLLINATIONS_API_KEY` (free key from enter.pollinations.ai) or `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN` to `master.env`, then restart the service |
| "pick an image for …" in `ms status` | A visual task asked for several candidates | Open the contact sheet link, then `ms pick <project> <task> <asset> <n>` |
| A visual task fails with "the visual review blocked it" | The reviewer judged the screenshots unreadable, the change invisible, or off-style | Read the notes in `ms report`; the next attempt sees them. If the review is wrong, adjust the task, the direction, or set `[reviewer] provider = "none"` |
| Visual review "unavailable" | Screenshot capture failed, or the model answer was unusable | Check `visual_review.screens` / `capture` in `project.yaml`; capture needs Playwright in the project (`npm ci`) |
| An attempt fails with "outside the task type's allowed paths" | The worker changed files its type may not touch | Change the task's type in a new plan, or widen `task_types.<type>.allowed_paths` |
| The planner says an epic id "is already used" | The epic already has tasks, or is not a proposed backlog epic | Check `ms backlog <project>`; plan only `proposed` epics |
| No notification after runs | Per-run notifications are off by default | Read the morning summary (`ms report --summary`), or set `[daemon] batch_notifications = true` |
| `ms limit … free\|paid`: "<worker> failed a test request (…). Not switched; the project stays paused" | Before switching, the target profile gets one small request in an empty temporary directory, with its model, home and environment. It failed: `rate_limited` (its quota is gone too), `model_unavailable` (the model is not offered, or OpenCode answered with a different model), `not_configured` (no login or key in that profile's home), `provider_error`, a timeout (90 s) or a reply without the expected answer | Do what the message suggests next (the other choice, or `wait`). For `not_configured`, log in within that profile's home (for example `HOME=<home> opencode auth login`). Check the model with `HOME=<home> opencode models <provider>`. The probe's output is in `~/.local/share/master-system/logs/probes/`, and history records it (`human_action` `worker_probe`) |
| `ms worker <project> <profile>`: "<worker> failed a test request (…). Not pinned; the worker stays …" | The same test request as for `ms limit free\|paid` failed (see the row above); nothing was pinned | Fix the profile (login, model, quota) or pin another; `ms worker <project>` shows what is active |
| `ms worker <project> <profile>`: "unknown worker profile" | The name is not a `[worker.profiles.<name>]` section | `ms worker <project>` lists the worker order; the profile names are in `config.toml` |
| The project is paused by a limit of a worker you no longer use ("Choose: ms limit …" for a profile that is not the pinned one) | Before this fix, pinning another profile left the old limit question in place | `ms worker <project> <profile>` for the worker you want: after its test request passes, the old limit is ended (kept in the history). If an older version is installed, update it |
| The project is paused by a limit but `ms worker <project>` shows another worker as active | Not expected: a limit pauses a project only while the limited profile is the active worker | Run `ms doctor` and open an issue with its output |
| The project keeps using a worker you did not expect | A pin (`Worker: … (pinned)` in `ms status`), or a temporary worker you or `auto_paid_fallback` chose for a limit | `ms worker <project>` shows which and why; `ms worker <project> auto` removes the pin and a kept temporary worker |
| Notification "<project>: staying on <worker>" | A temporary worker's time is over but the original is still limited (model gone, no credential, or reset unknown), so the service did not switch back | When the original works again: `ms worker <project> <original>` (tests it, then pins it) or `ms worker <project> auto` |
| Notification "<project>: switched to <paid worker>" | `[worker] auto_paid_fallback = true` and a free worker's limit was longer than `max_auto_wait_minutes` | Its cost counts toward the daily cap; turn the setting off to be asked instead |
| `ms status`: "<worker> is rate-limited until ~18:00. Choose: ms limit …" | The worker's provider limit lasts longer than `max_auto_wait_minutes`, or it stayed limited after an automatic wait | `ms limit <project> wait` (keep waiting), `free` (next free worker) or `paid` (the paid worker; counts toward the daily cap) |
| `ms status`: "<worker> is not available (model gone or no longer free)" | The model was removed or now costs money | Choose `free` or `paid`, then update `[worker] workers` |
| `ms status`: "<worker> is not set up (no credential)" | A profile (usually the paid one) has no login in its worker home | `HOME=<profile home> opencode auth login`, or choose another worker |
| "the project waits until 14:02 (automatic)" | A short limit; the same worker continues after the reset | Nothing |
| Attempts show outcome `limited` in `ms report` | The provider refused; the attempt was not verified and does not count | Nothing |
| Notification "Worker models need you" | The weekly check found a free model gone or no longer free | Edit `[worker] workers` and the profiles in `config.toml` |
| The bot does not answer | Not paired, the service is not running, or the token is missing | `ms telegram status`; `journalctl --user -u master-system.service -n 20` should show `telegram on`; see [TELEGRAM-SETUP.md](TELEGRAM-SETUP.md) |
| `/pair` is ignored | The code expired (15 minutes) or was already used | `ms telegram pair` again and send the new code |
| Log: `telegram: getUpdates: HTTP 409 Conflict` | Another program polls the same bot (two services, or a webhook) | Stop the other poller; `curl …/deleteWebhook` if a webhook was set elsewhere |
| A button says "This button has expired." | Buttons work once and expire after 48 hours | Run the command again (`/lessons`, `/pick`, `/limit`) |
| Telegram notifications stopped, ntfy still works | Telegram send failed or the bot was unpaired; ntfy is the fallback | `ms telegram test` |
| An attempt is `stalled` in `ms report` | The worker changed nothing: cut off while thinking (`finish_reason: length`) or busy for minutes without writing | Usually nothing; it is not a failure and is retried. After 3 stalls the task waits: split it or describe a smaller first step, then `ms reopen <project> <task>` |
| `show` says "(incomplete) task …: must_not needs 1 to 3 short conditions" | Every task needs 1–3 "must not" conditions; drafts saved before the upgrade have none | Tell the planner "add must-not conditions to every task" (or name them yourself), then `check` again |
| `check` shows `[!!] too big` | A task asks for too much at once (estimate, items, files) | Ask the planner to split it as suggested, or `approve anyway` |
| "N task(s) are too big … approve anyway" on approve | The size check flagged tasks | Split them in the chat, or type `approve anyway` |
| A task "waits for your decision on a split" | The worker was cut off twice; the planner drafted a split | Telegram buttons, or `ms split <project> <task> approve \| reject` (see the draft with `ms chat`, `show`) |
| A reopened task was blocked again at once | Fixed: the Master counted attempts from before the reopen | Update; `ms reopen` again |
| A task is blocked although the worker never changed anything | Attempts before stall detection counted as failures | `ms reopen <project> <task> --reason "…"` |
| Worker error 403 "free tier can only be used from within OpenCode" | A profile's tool configuration disabled `bash` | Keep `bash` in `task_types.<type>.tools` (it is added automatically) |
| `ms lessons` shows nothing | No verified attempt has proposed lessons yet | Nothing to do |
| "N approved lessons don't fit in the 2,000-character budget and aren't used" (`ms status`, `ms lessons`) | Lessons of a type (plus `all`) are used newest first up to a fixed 2,000 characters; the rest are left out of every worker prompt | `ms lessons <project>` shows which ones ("does not fit"). Reject some (`--reject L-3,L-5`, approved ids are accepted), preferring ones marked "never in a passing attempt", or shorten their `text` in `lessons.yaml` while no run is in progress |
| A lesson is marked "never in a passing attempt" | It was in the prompt of 5 or more finished attempts and none passed. A hint, not proof: hard tasks fail with good lessons too | Read it; if it is wrong or stale, `ms lessons <project> --reject <id>`. Nothing is removed automatically |
| Acceptance passes in the worker's worktree but fails in verification (missing `node_modules`, build output, generated files) | Verification runs in a fresh worktree of the committed result; nothing the worker left uncommitted or ignored is there | Acceptance must build what it needs from committed files: put installs and builds in `planner.setup` (approved plans run them before the checks), or in the task's `acceptance.commands` |
| An attempt fails with `repository_tampered` | The worker changed refs or the clone's shared git directory (`config`, `hooks/`, `info/`) outside its own branch; everything was put back before the snapshot and acceptance did not run | `ms report` lists what changed. If you committed to `develop` in the dedicated clone during the run, that commit was undone too: its sha is in the attempt's `restored_refs` (history); recover it with `git -C <clone> branch recovered <sha>`. Do not work in the dedicated clone while a run is in progress |
| "develop was not published: it is at …, which the system did not set" (`<project>: needs you`) | `develop` was changed outside Master System (by hand, or by a process the tamper check could not see); the publisher and release preparation refuse to push it | Inspect `git -C <clone> log refs/ms-system/heads/develop..develop`. If the change is yours and intended: `ms publish <project> --accept-tip`. If not: `git -C <clone> update-ref refs/heads/develop refs/ms-system/heads/develop` |
| `ms publish`: "A run is in progress" | Publishing changes refs; while a run holds the project lock it waits | Nothing: the service publishes after the run |
| `ms status`: "<task>: the orchestrating model wants to cancel it: …" (a run stopped with `cancellation_requires_human`) | The model proposed cancelling the task; cancelling is never automatic. The service skips this task until you decide; other tasks keep running | Keep it: `ms reopen <project> <task> --reason "…"` (fresh attempt budget; consider clarifying it in `ms chat` first). Accept: `ms cancel <project> <task> --reason "…"`; it lists tasks that depend on it, which cannot start until you change or cancel them |
| `ms cancel`: "… is already completed" (or cancelled) | Only open tasks can be cancelled | Nothing to do |
| `check` fails during a `setup` command | Dependency installation or network failure | Run the setup command manually in `<clone>`; check connectivity |
| The preview did not update after "batch done" | GitHub Pages builds take 1–2 minutes, or publishing failed | Wait and reload; `ms publish <project>` shows any error; `ms github check` |
| "Publishing is not set up yet (GitHub App)" | App id or private key missing | `ms github check`; [GITHUB-SETUP.md](GITHUB-SETUP.md) |
| `ms github check`: the App cannot sign in | Wrong key file or App ID, or the App was uninstalled | Repeat steps 3–5 of [GITHUB-SETUP.md](GITHUB-SETUP.md) |
| GitHub: "Review required" / merging is blocked on the release pull request | The `main` ruleset requires one approval ([GITHUB-SETUP.md](GITHUB-SETUP.md), step 6) | Approve it (**Files changed** → **Review changes** → **Approve**), then merge |
| "could not check the release on GitHub" for a private repository | Release fetches are anonymous: control-plane git ignores your global git config, including credential helpers | Use a public repository for releases, or finish the release by hand (tag the merge); pushes are unaffected (they use the App token) |
| `ms status` still shows a release waiting for a merge | GitHub has not been checked since the merge | Run `ms status` again (it checks at most once a minute) |
| Release: "main's files differ from the reviewed develop; not tagged" | The merge on GitHub is not exactly the reviewed `develop` (e.g. edited during the merge) | Inspect the merge on GitHub; correct `main` with a new release pull request |
| `integration_refused` / "conflicts with newer work" in a report | Two tasks changed the same code; the later one is retried from the new `develop` automatically | Nothing, unless it repeats (it then surfaces as "needs you") |
| A run stopped with "completion refused twice" | The Master kept proposing completion for a task whose pass could not be integrated | The service retries next cycle; if the project stalls, revise the task in `ms chat` |
| `error: … project is busy` | A run or another command holds the project lock | Wait until `ms status` reports Idle. The lock is released when its holding process exits (`ps aux \| grep run_cli`). |
| History schema error on start | The database is newer or older than the code | Update the checkout; migration backups are `~/.local/share/master-system/history.sqlite.bak-*` |
| Worker fails immediately ("OpenCode binary not found") | Worker toolchain missing | Check `[worker] opencode_bin` |
| "warning: no Node >= N for workers; Node-based acceptance commands will fail" (run log, `ms doctor`) | No Node ≥ `node_min_major` in `[worker] node_bin`, nvm or the worker `PATH`. Python projects are unaffected | For a Node project: install Node (nvm, or a system package), or set `[worker] node_bin` to its `bin` directory |
| An acceptance command says `uv: not found` (or another tool in `~/.local/bin`) | The worker `PATH` is an allowlist | Add the directory to `[worker] path_dirs`, then restart the service |
| No phone notifications | Topic not subscribed, or ntfy unreachable | `ms notify test`; the topic is in `~/.config/master-system/ntfy-topic` (keep it private) |
| Disk filling up | Attempt worktrees are kept for inspection | Remove integrated ones under `~/.local/share/master-system-worktrees/` with `git -C <clone> worktree remove <path>` |

## Resolved issues

| Problem | Resolution |
| --- | --- |
| `ms doctor` showed "(no report: 'Namespace' object has no attribute 'summary')" | Fixed: it shows the last run report, or "No sessions yet." |
| The Master proposed completion repeatedly for a task whose pass conflicted with newer work | The Master is told the attempt is not integrated; a second refusal stops the run; runs without progress stall the project |
| The service could pick up the system's own backlog project | Only projects with `auto_integrate: true` are run |
| The GitHub App could not enable Pages (HTTP 403) | Pages is enabled once by a repository administrator ([GITHUB-SETUP.md](GITHUB-SETUP.md), step 7) |
| A merged release waited because nothing checked GitHub while the service was idle | `ms status` completes merged releases |
| A multi-line paste in `ms chat` became many messages and consumed the chat cap | Pastes arrive as one message; `"""` blocks; `--file` |
| `ms` run inside another checkout of the repository used that checkout's code | The wrapper runs `python -P` with an explicit `PYTHONPATH` |
| The planner replied "I'll read the files…" and ended its turn, so every nudge cost a paid turn (P1) | Reads happen within the turn: files named in prose are read, an empty promise is re-asked once, and the last call must answer |
