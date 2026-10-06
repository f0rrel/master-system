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
| `Projects root not found` | No project definitions at the configured root | Create `~/.config/master-system/projects/<id>/`, or set `[run] projects_root` |
| No preview link in `ms status` | The project has no `github.site_dir` | Add `site_dir` if the project has a static site to preview |
| A task "waits for images" and nothing generates them | No image provider key | Add `POLLINATIONS_API_KEY` (free key from enter.pollinations.ai) or `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN` to `master.env`, then restart the service |
| "pick an image for …" in `ms status` | A visual task asked for several candidates | Open the contact sheet link, then `ms pick <project> <task> <asset> <n>` |
| A visual task fails with "the visual review blocked it" | The reviewer judged the screenshots unreadable, the change invisible, or off-style | Read the notes in `ms report`; the next attempt sees them. If the review is wrong, adjust the task, the direction, or set `[reviewer] provider = "none"` |
| Visual review "unavailable" | Screenshot capture failed, or the model answer was unusable | Check `visual_review.screens` / `capture` in `project.yaml`; capture needs Playwright in the project (`npm ci`) |
| An attempt fails with "outside the task type's allowed paths" | The worker changed files its type may not touch | Change the task's type in a new plan, or widen `task_types.<type>.allowed_paths` |
| The planner says an epic id "is already used" | The epic already has tasks, or is not a proposed backlog epic | Check `ms backlog <project>`; plan only `proposed` epics |
| No notification after runs | Per-run notifications are off by default | Read the morning summary (`ms report --summary`), or set `[daemon] batch_notifications = true` |
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
| `check` shows `[!!] too big` | A task asks for too much at once (estimate, items, files) | Ask the planner to split it as suggested, or `approve anyway` |
| "N task(s) are too big … approve anyway" on approve | The size check flagged tasks | Split them in the chat, or type `approve anyway` |
| A task "waits for your decision on a split" | The worker was cut off twice; the planner drafted a split | Telegram buttons, or `ms split <project> <task> approve \| reject` (see the draft with `ms chat`, `show`) |
| A reopened task was blocked again at once | Fixed: the Master counted attempts from before the reopen | Update; `ms reopen` again |
| A task is blocked although the worker never changed anything | Attempts before stall detection counted as failures | `ms reopen <project> <task> --reason "…"` |
| Worker error 403 "free tier can only be used from within OpenCode" | A profile's tool configuration disabled `bash` | Keep `bash` in `task_types.<type>.tools` (it is added automatically) |
| `ms lessons` shows nothing | No verified attempt has proposed lessons yet | Nothing to do |
| Acceptance passes in the worker's worktree but fails in verification (missing `node_modules`, build output, generated files) | Verification runs in a fresh worktree of the committed result; nothing the worker left uncommitted or ignored is there | Acceptance must build what it needs from committed files: put installs and builds in `planner.setup` (approved plans run them before the checks), or in the task's `acceptance.commands` |
| `check` fails during a `setup` command | Dependency installation or network failure | Run the setup command manually in `<clone>`; check connectivity |
| The preview did not update after "batch done" | GitHub Pages builds take 1–2 minutes, or publishing failed | Wait and reload; `ms publish <project>` shows any error; `ms github check` |
| "Publishing is not set up yet (GitHub App)" | App id or private key missing | `ms github check`; [GITHUB-SETUP.md](GITHUB-SETUP.md) |
| `ms github check`: the App cannot sign in | Wrong key file or App ID, or the App was uninstalled | Repeat steps 3–5 of [GITHUB-SETUP.md](GITHUB-SETUP.md) |
| `ms status` still shows a release waiting for a merge | GitHub has not been checked since the merge | Run `ms status` again (it checks at most once a minute) |
| Release: "main's files differ from the reviewed develop; not tagged" | The merge on GitHub is not exactly the reviewed `develop` (e.g. edited during the merge) | Inspect the merge on GitHub; correct `main` with a new release pull request |
| `integration_refused` / "conflicts with newer work" in a report | Two tasks changed the same code; the later one is retried from the new `develop` automatically | Nothing, unless it repeats (it then surfaces as "needs you") |
| A run stopped with "completion refused twice" | The Master kept proposing completion for a task whose pass could not be integrated | The service retries next cycle; if the project stalls, revise the task in `ms chat` |
| `error: … project is busy` | A run or another command holds the project lock | Wait until `ms status` reports Idle. The lock is released when its holding process exits (`ps aux \| grep run_cli`). |
| History schema error on start | The database is newer or older than the code | Update the checkout; migration backups are `~/.local/share/master-system/history.sqlite.bak-*` |
| Worker fails immediately ("OpenCode binary not found" / "no Node >= N") | Worker toolchain missing | Check `[worker] opencode_bin`; `ls ~/.nvm/versions/node/` must contain a version ≥ `node_min_major` |
| No phone notifications | Topic not subscribed, or ntfy unreachable | `ms notify test`; the topic is in `~/.config/master-system/ntfy-topic` (keep it private) |
| Disk filling up | Attempt worktrees are kept for inspection | Remove integrated ones under `~/.local/share/master-system-worktrees/` with `git -C <clone> worktree remove <path>` |

## Resolved issues

| Problem | Resolution |
| --- | --- |
| The Master proposed completion repeatedly for a task whose pass conflicted with newer work | The Master is told the attempt is not integrated; a second refusal stops the run; runs without progress stall the project |
| The service could pick up the system's own backlog project | Only projects with `auto_integrate: true` are run |
| The GitHub App could not enable Pages (HTTP 403) | Pages is enabled once by a repository administrator ([GITHUB-SETUP.md](GITHUB-SETUP.md), step 7) |
| A merged release waited because nothing checked GitHub while the service was idle | `ms status` completes merged releases |
| A multi-line paste in `ms chat` became many messages and consumed the chat cap | Pastes arrive as one message; `"""` blocks; `--file` |
| `ms` run inside another checkout of the repository used that checkout's code | The wrapper runs `python -P` with an explicit `PYTHONPATH` |
| The planner replied "I'll read the files…" and ended its turn, so every nudge cost a paid turn (P1) | Reads happen within the turn: files named in prose are read, an empty promise is re-asked once, and the last call must answer |
