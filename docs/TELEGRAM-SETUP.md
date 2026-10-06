# Telegram setup

The Telegram bot is the phone interface to Master System: status, the planner chat,
approvals, image picks, lesson reviews, worker-limit choices and all notifications.
It runs inside the background service and long-polls Telegram: no webhook, no open
port. ntfy remains an optional fallback for notifications.

## 1. Create the bot

1. In Telegram, open **@BotFather** and send `/newbot`.
2. Choose a display name and a username ending in `bot`.
3. BotFather replies with a token like `123456789:AA…`. Treat it as a password.
4. Optional: `/setprivacy` → your bot → **Enable**, and `/setjoingroups` → **Disable**,
   so the bot cannot be added to groups.

## 2. Give the token to the service

```bash
printf 'TELEGRAM_BOT_TOKEN=%s\n' '<the token>' >> ~/.config/master-system/master.env
chmod 600 ~/.config/master-system/master.env
systemctl --user restart master-system.service     # when `ms status` says Idle
```

`journalctl --user -u master-system.service -n 3` should show `telegram on`.
The token is never printed or logged; errors show `<token>` instead.

## 3. Pair your account

```bash
ms telegram pair
```

It prints a one-time code, valid for 15 minutes. In Telegram, open your bot and send
`/pair <code>`. The bot answers "Paired" and shows its button menu. From then on it
answers only your Telegram user; messages from anyone else get no answer at all.

`ms telegram status` shows the pairing, `ms telegram test` sends a test message,
`ms telegram unpair` forgets the owner, and pairing again replaces it.

## 4. Use it

| Command or input | What happens |
| --- | --- |
| `/status`, `/summary`, `/report`, `/spend`, `/doctor` | Read-only views. `/summary` adds the reviewer's screenshots as photos; `/spend` shows today's spend, the caps and, if available, the DeepSeek balance. |
| `/project <name>` | Sets the active project for the commands below (not needed with one project). |
| `/backlog [project]` | The backlog in priority order. |
| Any plain message, or a `.md` / `.txt` file | Goes to the planner chat of the active project; the reply comes back formatted. |
| `/show`, `/check`, `/approve`, `/discard` | The planner's chat commands. `/approve` asks for a confirmation button. |
| `/release` | Opens the release pull request (after a confirmation) and sends its link. Merging stays on GitHub. |
| `/pause`, `/resume`, `/stop` | Control the service. |
| `/pick` | Each image candidate as a photo with a **Pick n** button. |
| `/lessons` | Each pending lesson with **Approve** / **Reject** buttons. |
| `/limit` | The current worker limit with **Wait** / **Free** / **Paid** buttons (each asks to confirm). |
| `/menu` | The command list and the button keyboard. |

There are no payment or billing actions; top up provider balances on their websites.
Every action that changes something is recorded in the history as a `human_action`
with `actor: telegram`.
