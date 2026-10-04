# Sergei

An interactive Russian-language conversation partner, running locally on
[Ollama](https://ollama.com).

Sergei talks with a Polish-speaking learner: mostly in Russian, switching to
Polish when an explanation is genuinely clearer, and watching for
Polish↔Russian interference. It is deliberately *not* gamified — no XP,
streaks, badges, or other artificial motivation.

The persona is defined by `SYSTEM_PROMPT` in `src/main.py` and is sent as the
first message of every conversation.

## Requirements

- Python >= 3.12
- A running Ollama server with the model pulled

```bash
ollama serve                 # if it is not already running
ollama pull qwen3:8b         # the default model
```

## Setup

```bash
uv sync
```

This creates/updates `.venv` from `uv.lock`. Only `requests` is required.

## Running

```bash
uv run python src/main.py
```

Or, using the virtualenv directly:

```bash
.venv/bin/python src/main.py
```

Then just type messages and press Enter. Sergei replies in the terminal.

```
You: Привет! Как твои дела?
Sergei: Привет! Спасибо, всё нормально. А как у тебя?

You: /quit
До свидания!
```

Quit and restart, and Sergei picks up where you left off:

```
Resumed 2 earlier message(s) from conversation.json. Use /reset to start over.

You: Как меня зовут?
Sergei: Меня зовут Bartosz, и вы живёте в Кракове.
```

### Commands

| Command    | Effect                                                  |
| ---------- | ------------------------------------------------------- |
| `/help`    | Show the command list                                   |
| `/reset`   | Clear the conversation **and** the saved history        |
| `/quit`    | Exit (`/exit` and `/q` also work)                       |
| `Ctrl-D`   | Exit on end of input                                    |
| `Ctrl-C`   | Exit                                                   |

Blank input is ignored.

### Configuration

All are optional; the defaults match the values in `src/main.py`.

| Variable         | Default                           | Purpose                          |
| ---------------- | --------------------------------- | -------------------------------- |
| `OLLAMA_URL`     | `http://localhost:11434/api/chat` | Ollama chat endpoint             |
| `OLLAMA_MODEL`   | `qwen3:8b`                        | Model tag to use                 |
| `SERGEI_DATA_DIR`| `data/`                           | Where `conversation.json` is kept |

```bash
OLLAMA_MODEL=qwen2.5:7b uv run python src/main.py
```

Point `SERGEI_DATA_DIR` somewhere else to keep separate conversations, or to
keep scratch runs out of the project:

```bash
SERGEI_DATA_DIR=/tmp/sergei-scratch uv run python src/main.py
```

## Conversation history

Sergei remembers the conversation across restarts. The full message list is
sent to Ollama on every turn, because `/api/chat` is stateless — the saved file
is simply the memory that makes that possible next time.

- Stored as JSON in `data/conversation.json`, in plain readable UTF-8.
- Written after every successful exchange, so a crash or a failed turn costs
  you at most the current turn.
- Writes go to a temporary file that is then moved into place, so an
  interrupted write cannot corrupt the previous history.
- **The system persona is never written to disk.** It is rebuilt from
  `SYSTEM_PROMPT` in `src/main.py` on every start, so the persona cannot drift
  or be damaged by a bad file, and editing the prompt takes effect immediately
  for existing conversations.
- `/reset` deletes the file as well as clearing the in-memory conversation.
- No file is written if you quit without exchanging any messages.

### If the saved history is unusable

A missing file simply means a fresh conversation. Anything else is reported on
stderr, the conversation starts clean, and the file is rewritten on your next
successful turn — a damaged file never stops Sergei from starting.

| Situation                              | What you see                                                     |
| -------------------------------------- | ---------------------------------------------------------------- |
| No file yet                            | nothing; a fresh conversation                                     |
| Empty or whitespace-only file          | `conversation.json is empty, starting a fresh conversation`       |
| Not valid JSON                         | `conversation.json is not valid JSON (...), starting fresh`       |
| Not a list of messages                 | `... does not hold a list of messages, starting fresh`           |
| Individual malformed messages          | `...: ignored N invalid message(s)` — valid ones are kept        |
| File unreadable                        | `could not read conversation.json: ...`                           |
| Cannot write the file                  | `could not save conversation to conversation.json: ...`           |

A message is valid only if it is an object with a `role` of `user` or
`assistant` and non-empty string `content`. Anything else is dropped, so a
hand-edited or partially corrupted file costs you only the bad entries.

## Error handling

Failures are reported on stderr and the loop keeps running, so a transient
error does not cost you the conversation. If a turn fails, that user message is
discarded rather than left in the history for the model to trip over — and it is
never written to disk.

| Situation                        | What you see                                              |
| -------------------------------- | --------------------------------------------------------- |
| Ollama not running               | `Cannot reach Ollama at ... Start it with 'ollama serve'.`|
| Model not pulled                 | `Model '...' is not available. Pull it with 'ollama pull ...'` |
| Reply exceeds the 120s timeout   | `Ollama did not reply within 120s. ...`                    |
| Other HTTP error                 | `Ollama returned HTTP <status>.`                            |
| Non-JSON or unexpected body      | `Unexpected response from Ollama: ...` plus the raw response |

## Tests

```bash
uv run python -m unittest discover -s tests -v
```

The suite uses only the standard library and needs no running Ollama server.

- `tests/test_chat.py` — request shape, every Ollama error branch, the loop
  behaviour (multi-turn history, `/reset`, `/quit`, EOF, `Ctrl-C`, ignored
  blank input, recovery after a failed turn) and the persistence layer in
  isolation, including every malformed-file case. Persistence is redirected at
  a temporary directory per test, so the suite never touches `data/`.
- `tests/test_restart.py` — runs `src/main.py` as a real subprocess against a
  stub Ollama server, several times in a row, to prove history genuinely
  survives a process restart and that a damaged file never blocks startup.