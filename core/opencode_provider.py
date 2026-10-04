"""OpenCode adapter: a local agent runtime, reached over its HTTP API.

This is a second concrete implementation of :class:`core.provider.ReasoningProvider`,
sitting beside the Ollama adapter and using the same contract. Nothing else in the
system learns that it exists: the engine still receives a string, still parses it,
and still routes every proposed operation through ``ReasoningInterface``.

    python -m core.reason_cli "Add a task" --provider opencode --model big-pickle

The model is addressed through a server that is already running locally::

    opencode serve --port 4096

Provider and model are two separate settings
--------------------------------------------
OpenCode names a model by a pair, ``providerID`` plus ``modelID``, and so does this
adapter: ``provider_id="opencode"`` with ``model_id="big-pickle"`` is the machine
this was written on, nothing more. No constant in this module, and no branch
anywhere above it, treats Big Pickle as special. Pointing the same adapter at
``opencode/space-bunny-free`` or at any provider OpenCode is configured with is a
constructor argument, and the CLI exposes both halves separately.

Asynchronous submission, then polling
--------------------------------------
The obvious call, ``POST /session/{id}/message``, streams the reply and has no
deadline of its own: it returns when the model does. One reasoning turn through a
hosted model can take minutes, and a provider that cannot bound its own wait cannot
report a timeout, only hang.

So this adapter uses ``POST /session/{id}/prompt_async``, which accepts the prompt
and answers ``204 No Content`` immediately, and then polls
``GET /session/{id}/message`` until an assistant message carries a completion
timestamp. The deadline belongs to this adapter, not to a socket, so an over-long
turn becomes a :class:`ProviderError` naming the model and the budget. On timeout
the session is aborted, so a model that is still generating stops instead of
quietly spending the rest of its budget on an answer nobody will read.

One session per request
-----------------------
Each ``complete`` call creates its own OpenCode session. A reply therefore cannot
inherit another request's history, and nothing has to be reset between calls. The
sessions are left in place afterwards rather than deleted: they are the transcript
of what was sent, readable with ``opencode sessions``.

Tools are not part of the authority model
-----------------------------------------
The adapter sends one text part and reads back text parts. It never sends a tool
configuration, never asks OpenCode to run anything, and discards every non-text
part of the reply: a tool call, were one to appear, is not text, is never returned
to the engine, and therefore can never be parsed as an operation. Master state is
reachable only through Master's own methods, and only after a human approves.

Two honest notes about what remains outside that boundary. OpenCode's agent still
has its own tool policy, and this adapter does not try to widen it: it leaves that
policy alone and defaults to OpenCode's ``plan`` agent, which opencode describes as
disallowing all edit tools, because this is a model that plans. Overriding the
session's tool set or permission ruleset was deliberately not done — OpenCode
Zen's free-tier models answer such requests with ``403 "OpenCode's free tier can
only be used from within OpenCode"`` (verified against opencode 1.18.34) — and it
would not have been a boundary worth having, since the operations Master accepts
still come only from text that a human approved.

The schema hint is not sent
---------------------------
``complete`` receives the same advisory JSON schema the Ollama adapter forwards,
and ignores it. OpenCode's published API accepts an ``OutputFormat`` of
``{"type": "json_schema", "schema": ...}``; opencode 1.18.34 answers ``400 Bad
Request`` to that payload, both as an object and as a JSON-encoded string. Ignoring
the hint is explicitly allowed by the provider contract, and correctness never
depended on it: the engine parses and validates the reply either way.

Standard library only, no credentials
-------------------------------------
For the same reason as the Ollama adapter: the declared dependencies are PyYAML and
pytest, so this adapter uses ``urllib.request`` and adds nothing to install or
audit. It reads no token, no environment variable and no credential file, and sends
no ``Authorization`` header. That matches the default ``opencode serve``, which is
unsecured and loopback-only; a server started with ``OPENCODE_SERVER_PASSWORD``
would reject this adapter, and authentication belongs in configuration outside this
repository rather than in a provider that has no use for it.

Every failure is a ProviderError
--------------------------------
Unreachable server, non-2xx status, non-JSON body, a message list of the wrong
shape, an assistant message that carries an error instead of an answer, a session
that never completes, and a completed session with no text all raise
:class:`ProviderError`. That is the existing mechanism for "the backend could not
be reached or gave no usable text", and it is a failed reasoning request rather
than a rejected operation: nothing was proposed, so there is nothing to approve
and nothing could have changed.
"""

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.provider import ProviderError, ReasoningProvider

__all__ = [
    "DEFAULT_AGENT",
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL_ID",
    "DEFAULT_POLL_INTERVAL",
    "DEFAULT_PROVIDER_ID",
    "DEFAULT_REQUEST_TIMEOUT",
    "DEFAULT_TOTAL_TIMEOUT",
    "SESSION_TITLE",
    "OpenCodeProvider",
]

DEFAULT_BASE_URL = "http://127.0.0.1:4096"
DEFAULT_PROVIDER_ID = "opencode"
DEFAULT_MODEL_ID = "big-pickle"

#: OpenCode's read-only-by-default primary agent: "Plan mode. Disallows all edit
#: tools." The reasoning model here only ever plans.
DEFAULT_AGENT = "plan"

#: Seconds allowed for one HTTP exchange with the server. Small, because every
#: call in this adapter is a single request/response.
DEFAULT_REQUEST_TIMEOUT = 30

#: Seconds allowed for the whole of one ``complete`` call, submission to answer.
#: This is the budget a slow or wedged model runs into.
DEFAULT_TOTAL_TIMEOUT = 300

#: Seconds between polls of the session's messages.
DEFAULT_POLL_INTERVAL = 1.0

#: Label for the session this adapter creates, so its transcripts are
#: recognisable in ``opencode sessions``.
SESSION_TITLE = "reasoning request"


class OpenCodeProvider(ReasoningProvider):
    """Ask a model served by a local OpenCode server to complete a prompt."""

    def __init__(
        self,
        base_url=DEFAULT_BASE_URL,
        provider_id=DEFAULT_PROVIDER_ID,
        model_id=DEFAULT_MODEL_ID,
        timeout=DEFAULT_REQUEST_TIMEOUT,
        total_timeout=DEFAULT_TOTAL_TIMEOUT,
        poll_interval=DEFAULT_POLL_INTERVAL,
        agent=DEFAULT_AGENT,
    ):
        if not _is_text(provider_id) or not _is_text(model_id):
            raise ValueError("provider_id and model_id must be non-empty strings")
        if poll_interval < 0:
            raise ValueError("poll_interval cannot be negative")

        self.base_url = base_url.rstrip("/")
        self.provider_id = provider_id
        self.model_id = model_id
        self.timeout = timeout
        self.total_timeout = total_timeout
        self.poll_interval = poll_interval
        self.agent = agent

    @property
    def name(self):
        return f"opencode:{self.provider_id}/{self.model_id}"

    def complete(self, prompt, schema=None):
        """Send the prompt to a fresh session and return the assistant's text.

        ``schema`` is accepted and ignored; see the module docstring for why, and
        ``core.provider.ReasoningProvider`` for why ignoring it is allowed.
        """
        deadline = time.monotonic() + self.total_timeout

        session_id = self._create_session()
        self._submit(session_id, prompt)
        messages = self._await_completion(session_id, deadline)

        return self._reply_text(messages)

    def list_models(self):
        """Return the models this server reports, as ``providerID/modelID``."""
        payload = self._get("/provider")
        providers = payload.get("all") if isinstance(payload, dict) else None
        if not isinstance(providers, list):
            raise ProviderError("OpenCode returned no provider list")

        names = []
        for provider in providers:
            if not isinstance(provider, dict):
                continue
            provider_id = provider.get("id")
            models = provider.get("models")
            if isinstance(models, dict):
                model_ids = list(models)
            elif isinstance(models, list):
                model_ids = [
                    model.get("id") for model in models if isinstance(model, dict)
                ]
            else:
                continue
            for model_id in model_ids:
                if _is_text(provider_id) and _is_text(model_id):
                    names.append(f"{provider_id}/{model_id}")

        return sorted(names)

    def is_available(self):
        """Whether the server answers and serves the configured model.

        Returns False instead of raising, so a missing server can become a
        readable instruction rather than a traceback.
        """
        try:
            return f"{self.provider_id}/{self.model_id}" in self.list_models()
        except ProviderError:
            return False

    # --- session lifecycle -------------------------------------------------

    def _create_session(self):
        """Open a session pinned to the configured model and agent."""
        body = {
            "title": SESSION_TITLE,
            "model": {"id": self.model_id, "providerID": self.provider_id},
        }
        if self.agent:
            body["agent"] = self.agent

        payload = self._post("/session", body)
        session_id = payload.get("id") if isinstance(payload, dict) else None
        if not _is_text(session_id):
            raise ProviderError("OpenCode returned no session id")

        return session_id

    def _submit(self, session_id, prompt):
        """Hand the prompt over and return immediately, without waiting for it."""
        body = {
            "model": {"providerID": self.provider_id, "modelID": self.model_id},
            "parts": [{"type": "text", "text": prompt}],
        }
        if self.agent:
            body["agent"] = self.agent

        # 204 No Content: acceptance, not an answer.
        self._post(f"/session/{session_id}/prompt_async", body, expect_json=False)

    def _await_completion(self, session_id, deadline):
        """Poll until an assistant message is finished, or the budget runs out."""
        while True:
            messages = self._messages(session_id)
            info = _last_completed_assistant(messages)
            if info is not None:
                error = info.get("error")
                if error:
                    raise ProviderError(
                        f"OpenCode could not complete {self.name}: "
                        f"{_describe_error(error)}"
                    )
                return messages

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._abort(session_id)
                raise ProviderError(
                    f"OpenCode did not finish {self.name} within "
                    f"{self.total_timeout}s"
                )

            time.sleep(min(self.poll_interval, remaining))

    def _abort(self, session_id):
        """Stop a session that outstayed its welcome.

        Best effort by design: this runs while already failing, and a server that
        cannot be told to stop must not replace the real error with a new one.
        """
        try:
            self._post(f"/session/{session_id}/abort", None, expect_json=False)
        except ProviderError:
            pass

    # --- reading the session ----------------------------------------------

    def _messages(self, session_id):
        payload = self._get(f"/session/{session_id}/message")
        if not isinstance(payload, list):
            raise ProviderError("OpenCode returned no message list")

        for message in payload:
            if not isinstance(message, dict) or not isinstance(
                message.get("info"), dict
            ):
                raise ProviderError("OpenCode returned a malformed message")

        return payload

    def _reply_text(self, messages):
        """Return the assistant's text and nothing else.

        Every part that is not text is ignored, so a tool call cannot reach the
        engine even if OpenCode decides to make one.
        """
        texts = []
        for message in messages:
            info = message["info"]
            if info.get("role") != "assistant" or not _is_completed(info):
                continue
            parts = message.get("parts")
            if not isinstance(parts, list):
                continue
            for part in parts:
                if not isinstance(part, dict) or part.get("type") != "text":
                    continue
                text = part.get("text")
                if _is_text(text) and text.strip():
                    texts.append(text)

        if not texts:
            raise ProviderError(
                f"OpenCode returned no assistant text for {self.name}"
            )

        return "\n".join(texts)

    # --- HTTP --------------------------------------------------------------

    def _post(self, path, body, expect_json=True):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return self._send(request, expect_json)

    def _get(self, path):
        return self._send(urllib.request.Request(f"{self.base_url}{path}"), True)

    def _send(self, request, expect_json):
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            raise ProviderError(
                f"OpenCode returned HTTP {error.code} for {request.full_url}"
            ) from error
        except urllib.error.URLError as error:
            raise ProviderError(
                f"Cannot reach OpenCode at {request.full_url}: {error.reason}. "
                "Start it with 'opencode serve' or pass --base-url."
            ) from error
        except OSError as error:
            raise ProviderError(
                f"Cannot reach OpenCode at {request.full_url}: {error}"
            ) from error

        if not raw:
            if expect_json:
                raise ProviderError(
                    f"OpenCode returned an empty reply for {request.full_url}"
                )
            return None

        try:
            return json.loads(raw)
        except ValueError as error:
            raise ProviderError(
                f"OpenCode returned a non-JSON reply: {error}"
            ) from error


# --- reading OpenCode's replies ------------------------------------------


def _is_text(value):
    return isinstance(value, str) and bool(value.strip())


def _is_completed(info):
    """Whether a message carries the completion timestamp OpenCode sets at the end."""
    time_info = info.get("time")
    return isinstance(time_info, dict) and bool(time_info.get("completed"))


def _last_completed_assistant(messages):
    """The newest finished assistant message, or None while the model is working."""
    for message in reversed(messages):
        info = message["info"]
        if info.get("role") == "assistant" and _is_completed(info):
            return info
    return None


def _describe_error(error):
    """Turn an OpenCode error object into one readable sentence."""
    if not isinstance(error, dict):
        return str(error)

    name = error.get("name") or "unknown error"
    data = error.get("data")
    if not isinstance(data, dict):
        return name

    parts = [name]
    status = data.get("statusCode")
    if isinstance(status, int):
        parts.append(f"HTTP {status}")
    message = data.get("message")
    if _is_text(message):
        parts.append(message.strip())

    return " ".join(parts)