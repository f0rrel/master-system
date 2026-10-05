# Troubleshooting

Start with `ms status`. If the answer isn't below, run `ms doctor` and paste its output,
plus the README, into any AI chat.

## Known bugs (to fix next)

| Problem | What it means | What to do now |
| --- | --- | --- |
| **The planner says "I'll read the files…" (or "let me look at…") and then stops**, so you have to nudge it ("go on"), and every nudge costs a turn | **Bug P1.** The model answered in words instead of listing the files in its `read_files` field, so the chat had nothing to read and ended the turn. Seen 2026-10-06. | Reply `go on` once. If it repeats, name the files yourself ("read www/js/game.js and tests/helpers/game.js"). Planned fix: when a reply promises to read but lists no files, or has no draft and no questions, the chat re-asks the model automatically (once, at no cost to you beyond that call) and reminds it of the JSON format. |

## Common problems

| Symptom | What it means | What to check or run |
| --- | --- | --- |
| `ms: command not found` | The wrapper isn't on your PATH | `cd ~/AI/master-system-big-pickle && uv run python -m core.ms install` |
| `ms status` says **Paused** | `ms pause` or `ms stop` was used | `ms resume` |
| Nothing happens for a long time, but tasks are "Coming up" | The service is stopped, paused, over today's cap, or the project is stalled | `systemctl --user status master-system.service`; `ms status` (it says which); `journalctl --user -u master-system.service -n 30` |
| Notification: **needs you** (no progress) | The last run did nothing useful, so the service waits | `ms report`; change the task in `ms chat` (any recorded change restarts work) |
| Notification: **ml-N needs you** (failed several attempts) | The task is too hard or badly specified for the worker | `ms report` shows the failing test output; describe the task more precisely in `ms chat`; or split it |
| **Daily budget reached** | `[budget] daily_usd` was hit | Wait for tomorrow, or raise it in `~/.config/master-system/config.toml` and restart the service when Idle |
| Planner: "this chat reached its cap" | `[planner] chat_usd` was hit | `ms chat match-legends --new`, or raise the cap |
| A multi-line paste became several messages | The terminal didn't use bracketed paste | Put the text between two `"""` lines, or use `ms chat match-legends --file request.txt` |
| `check` fails: "passes already on the current code" | The drafted test doesn't test anything new | Tell the planner the test must fail until the feature exists |
| `check` fails: "the test itself is broken" | A syntax error or a missing helper in the drafted test | Paste the failure to the planner and ask it to fix the test |
| `check` fails at `setup: npm ci` | Dependencies or the network | Run `npm ci` in `~/AI/managed/match-legends`; check your internet connection |
| Preview didn't update after "batch done" | The GitHub Pages build takes 1–2 minutes, or publishing failed | Wait, then reload; `ms publish match-legends` shows any error; `ms github check` |
| "Publishing is not set up yet (GitHub App)" | The App id or key is missing | `ms github check`; [github-setup.md](github-setup.md) |
| `ms github check`: the app cannot sign in | The key file or App ID is wrong, or the App was uninstalled | Redo steps 3–5 of [github-setup.md](github-setup.md) |
| `ms status` still says a release waits for your merge | You merged it, but GitHub wasn't checked yet | Run `ms status` again (it checks at most once a minute) |
| Release: "main's files differ from the reviewed develop; not tagged" | The merge on GitHub isn't exactly the reviewed `develop` (e.g. edited during the merge) | Look at the merge on GitHub; fix `main` by a new release PR |
| `integration_refused` / "conflicts with newer work" in a report | Two tasks changed the same code; the second is retried from the new `develop` automatically | Nothing, unless it repeats (then it shows up as "needs you") |
| A run was stopped as "completion refused twice" | Master kept trying to complete a task whose pass couldn't be integrated | The service retries on the next cycle; if it stalls, change the task in `ms chat` |
| `error: … project is busy` (lock) | A run or another command holds the project | Wait for `ms status` to say Idle; if no process runs (`ps aux | grep run_cli`) the lock frees itself when the process is gone |
| History schema error on start | The database is newer or older than the code | Make sure the checkout is on `main` and up to date; backups are `~/.local/share/master-system/history.sqlite.bak-*` |
| Worker fails immediately ("OpenCode binary not found" / "no Node >= 22") | The tools the workers need are missing | `ls ~/.opencode/bin/opencode`; `ls ~/.nvm/versions/node/` (needs v22+) |
| Phone gets no notifications | The topic isn't subscribed, or ntfy is unreachable | `ms notify test`; the topic is in `~/.config/master-system/ntfy-topic` (keep it private) |
| Disk filling up | Old attempt worktrees are kept for inspection | `~/.local/share/master-system-worktrees/`; remove integrated ones (`git -C ~/AI/managed/match-legends worktree remove …`) |

## Fixed problems (history)

| When | Problem | Fix |
| --- | --- | --- |
| 2026-10-05 | Master proposed "complete" 12 times for a task whose pass conflicted with newer work | Master is told the attempt isn't integrated; a second refusal stops the run; runs without progress stall the project |
| 2026-10-05 | The service could have worked on the system's own project | It only runs projects with `auto_integrate: true` |
| 2026-10-06 | The GitHub App couldn't switch Pages on (403) | Pages was switched on once by the owner (github-setup.md step 7) |
| 2026-10-06 | A merged release waited because nothing checked GitHub while the service was idle | `ms status` finishes merged releases |
| 2026-10-06 | A multi-line paste in `ms chat` became many messages and used up the chat cap | Pastes arrive as one message; `"""` blocks; `--file` |
