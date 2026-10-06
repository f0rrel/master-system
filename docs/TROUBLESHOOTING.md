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
| `ms lessons` shows nothing | No verified attempt has proposed lessons yet | Nothing to do |
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
