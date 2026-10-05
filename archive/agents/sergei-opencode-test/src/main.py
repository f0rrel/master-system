import json
import os
import sys
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(
    os.environ.get("SERGEI_DATA_DIR") or PROJECT_ROOT / "data"
).expanduser()
HISTORY_PATH = DATA_DIR / "conversation.json"

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/chat")
MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:8b")
TIMEOUT = 120

SYSTEM_PROMPT = """
You are Sergei (Сергей), a personal Russian language companion.

Your purpose is to help the learner actually use and retain Russian through
natural interaction.

The learner speaks Polish.

Rules:
- Prefer communicating in Russian.
- Use Polish when an explanation is genuinely useful.
- Prefer meaningful sentences and phrases over isolated vocabulary.
- Correct important or recurring mistakes, but do not correct every tiny error.
- When explaining Russian grammar, use Polish when it makes the explanation clearer.
- Pay particular attention to interference between Polish and Russian.
- Do not behave like a gamified language-learning application.
- Do not use XP, streaks, badges, or artificial motivation.
- Have natural conversations about topics the learner finds interesting.
"""

WELCOME = (
    f"Sergei is online. You are talking to {MODEL} through Ollama.\n"
    "Type a message in Russian or Polish. Commands: /help, /reset, /quit"
)

HELP = """Commands:
  /reset   forget the conversation so far, keep the same persona
  /quit    exit (Ctrl-D or Ctrl-C also works)"""

QUIT_COMMANDS = {"/quit", "/exit", "/q"}
RESET_COMMANDS = {"/reset"}
HELP_COMMANDS = {"/help", "/?"}

PERSISTED_ROLES = ("user", "assistant")


class OllamaError(RuntimeError):
    """Raised when a chat turn cannot be completed."""


def _unexpected(detail, body):
    raw = repr(body)
    if len(raw) > 200:
        raw = raw[:200] + "..."
    return OllamaError(f"Unexpected response from Ollama: {detail}\nRaw response: {raw}")


def chat(messages):
    """Send the full conversation to Ollama and return the assistant reply."""
    try:
        response = requests.post(
            OLLAMA_URL,
            json={
                "model": MODEL,
                "messages": list(messages),
                "stream": False,
            },
            timeout=TIMEOUT,
        )
        response.raise_for_status()
    except requests.exceptions.ConnectionError as exc:
        raise OllamaError(
            f"Cannot reach Ollama at {OLLAMA_URL}. Start it with 'ollama serve'."
        ) from exc
    except requests.exceptions.Timeout as exc:
        raise OllamaError(
            f"Ollama did not reply within {TIMEOUT}s. The model may be loading or "
            "the machine may be short on memory."
        ) from exc
    except requests.exceptions.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "unknown"
        if status == 404:
            raise OllamaError(
                f"Model '{MODEL}' is not available. Pull it with 'ollama pull {MODEL}'."
            ) from exc
        raise OllamaError(f"Ollama returned HTTP {status}.") from exc
    except requests.exceptions.RequestException as exc:
        raise OllamaError(f"Request to Ollama failed: {exc}") from exc

    try:
        data = response.json()
    except ValueError as exc:
        raise _unexpected("body was not valid JSON", response.text[:200]) from exc

    if not isinstance(data, dict):
        raise _unexpected(f"expected a JSON object, got {type(data).__name__}", data)

    message = data.get("message")
    if not isinstance(message, dict):
        raise _unexpected("no 'message' object in response", data)

    content = message.get("content")
    if not isinstance(content, str):
        raise _unexpected("'message.content' was missing or not a string", data)
    if not content.strip():
        raise _unexpected("'message.content' was empty", data)

    return content


def _valid_turn(item):
    return (
        isinstance(item, dict)
        and item.get("role") in PERSISTED_ROLES
        and isinstance(item.get("content"), str)
        and item["content"].strip() != ""
    )


def load_history():
    """Read persisted turns.

    Returns (turns, warning). A missing file is not a warning: it simply means
    there is no history yet. Anything unreadable or malformed yields a fresh
    conversation plus a human-readable explanation instead of an exception.
    """
    try:
        raw = HISTORY_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [], None
    except OSError as exc:
        return [], f"could not read {HISTORY_PATH.name}: {exc}"

    if not raw.strip():
        return [], f"{HISTORY_PATH.name} is empty, starting a fresh conversation"

    try:
        payload = json.loads(raw)
    except ValueError as exc:
        return [], f"{HISTORY_PATH.name} is not valid JSON ({exc}), starting fresh"

    if not isinstance(payload, list):
        return [], (
            f"{HISTORY_PATH.name} does not hold a list of messages, starting fresh"
        )

    turns = []
    skipped = 0
    for item in payload:
        if _valid_turn(item):
            turns.append({"role": item["role"], "content": item["content"]})
        else:
            skipped += 1

    if skipped:
        return turns, f"{HISTORY_PATH.name}: ignored {skipped} invalid message(s)"
    return turns, None


def save_history(turns):
    """Persist turns, without the system message.

    Written to a temporary file and moved into place so an interrupted write
    cannot leave a half-written file behind. Returns None on success, or a
    warning string on failure.
    """
    tmp_path = HISTORY_PATH.with_name(HISTORY_PATH.name + ".tmp")
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(
            json.dumps(turns, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(tmp_path, HISTORY_PATH)
    except OSError as exc:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        return f"could not save conversation to {HISTORY_PATH.name}: {exc}"
    return None


def clear_history():
    """Delete the persisted conversation. Returns None or a warning string."""
    try:
        HISTORY_PATH.unlink()
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"could not delete {HISTORY_PATH.name}: {exc}"
    return None


def run_conversation():
    turns, warning = load_history()
    if warning:
        print(f"[warning] {warning}", file=sys.stderr)

    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + turns
    print(WELCOME)
    if turns:
        print(
            f"Resumed {len(turns)} earlier message(s) from {HISTORY_PATH.name}. "
            "Use /reset to start over."
        )

    while True:
        try:
            user_input = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not user_input:
            continue

        command = user_input.lower()
        if command in QUIT_COMMANDS:
            break
        if command in HELP_COMMANDS:
            print(HELP)
            continue
        if command in RESET_COMMANDS:
            messages[:] = messages[:1]
            print("Sergei: Контекст очищен. С чего начнём?")
            problem = clear_history()
            if problem:
                print(f"[warning] {problem}", file=sys.stderr)
            continue

        messages.append({"role": "user", "content": user_input})
        try:
            reply = chat(messages)
        except OllamaError as exc:
            messages.pop()
            print(f"[error] {exc}", file=sys.stderr)
            continue

        messages.append({"role": "assistant", "content": reply})
        problem = save_history(messages[1:])
        if problem:
            print(f"[warning] {problem}", file=sys.stderr)
        print(f"Sergei: {reply}")

    print("До свидания!")
    return 0


def main():
    try:
        return run_conversation()
    except KeyboardInterrupt:
        print()
        return 130


if __name__ == "__main__":
    sys.exit(main())