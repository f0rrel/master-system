"""Ollama adapter: a local model runtime, reachable over plain HTTP.

This is one concrete implementation of :class:`core.provider.ReasoningProvider`.
It exists because this repository already talks to a local Ollama runtime
elsewhere (``archive/bob.py`` and ``archive/agents/sergei`` both POST to ``localhost:11434``),
so demonstrating the reasoning loop needs no new service, no account, and no
secret. ``qwen2.5-coder:7b`` is the model ``archive/bob.py`` already used; the default
here is the other small model present on this machine, and both the address and
the model name are constructor arguments rather than constants the rest of the
code depends on.

Two choices are deliberate.

Standard library only
---------------------
``archive/bob.py`` uses ``requests``, which is installed in the agents' own virtual
environments but is not a declared dependency of this project. The declared
dependencies are PyYAML and pytest, so this adapter uses ``urllib.request`` and
adds no dependency to install, pin, or audit. Swapping in a provider that needs
an SDK is a new adapter's problem, not this package's.

No credentials
--------------
A local Ollama runtime is unauthenticated, so this adapter reads no token, no
environment variable, and no credential file, and there is nowhere in this
repository for a key to hide. A hosted provider would need authentication, and
that belongs in its own adapter reading configuration from outside the
repository rather than in a shared module.

Determinism
-----------
Generation is pinned to temperature 0 so the same request against the same model
produces the same proposal. This is a local model, so that is achievable and
worth having: a proposal a human reviewed should be reproducible. It is not a
guarantee the system relies on for correctness, which is why every operation is
still validated after it arrives.
"""

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.usage import from_counts
from core.provider import ProviderError, ReasoningProvider

__all__ = ["DEFAULT_BASE_URL", "DEFAULT_MODEL", "OllamaProvider"]

DEFAULT_BASE_URL = "http://localhost:11434"
DEFAULT_MODEL = "qwen2.5-coder:7b"

# Temperature 0 keeps a reviewed proposal reproducible on a local model.
GENERATION_OPTIONS = {"temperature": 0}


class OllamaProvider(ReasoningProvider):
    """Ask a model served by a local Ollama runtime to complete a prompt."""

    def __init__(self, base_url=DEFAULT_BASE_URL, model=DEFAULT_MODEL, timeout=180):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    @property
    def name(self):
        return f"ollama:{self.model}"

    def complete(self, prompt, schema=None):
        """POST the prompt to /api/chat and return the reply text.

        When schema is supplied it is passed as Ollama's structured-output
        constraint, which makes a small local model far more likely to answer
        with parseable JSON. Correctness does not depend on it: the engine
        parses and validates the reply regardless.
        """
        request_body = {
            "model": self.model,
            "stream": False,
            "options": dict(GENERATION_OPTIONS),
            "messages": [{"role": "user", "content": prompt}],
        }
        if schema is not None:
            request_body["format"] = schema

        self.last_usage = None
        payload = self._post("/api/chat", request_body)
        if isinstance(payload, dict):
            self.last_usage = from_counts(
                input=payload.get("prompt_eval_count") or 0,
                output=payload.get("eval_count") or 0,
            )

        try:
            content = payload["message"]["content"]
        except (KeyError, TypeError) as error:
            raise ProviderError(
                f"Ollama returned no message content: {error}"
            ) from error

        if not isinstance(content, str):
            raise ProviderError("Ollama returned non-text message content")

        return content

    def list_models(self):
        """Return the model names this runtime reports."""
        payload = self._get("/api/tags")
        models = payload.get("models")
        if not isinstance(models, list):
            raise ProviderError("Ollama returned no model list")

        return [
            entry.get("name")
            for entry in models
            if isinstance(entry, dict) and entry.get("name")
        ]

    def is_available(self):
        """Whether the runtime answers and serves the configured model.

        Returns False instead of raising, so a caller can turn a missing runtime
        into a readable instruction instead of a traceback.
        """
        try:
            return self.model in self.list_models()
        except ProviderError:
            return False

    def _post(self, path, body):
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return self._send(request)

    def _get(self, path):
        return self._send(urllib.request.Request(f"{self.base_url}{path}"))

    def _send(self, request):
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            raise ProviderError(
                f"Ollama returned HTTP {error.code} for {request.full_url}"
            ) from error
        except urllib.error.URLError as error:
            raise ProviderError(
                f"Cannot reach Ollama at {request.full_url}: {error.reason}. "
                "Start it with 'ollama serve' or pass --base-url."
            ) from error
        except OSError as error:
            raise ProviderError(
                f"Cannot reach Ollama at {request.full_url}: {error}"
            ) from error

        try:
            return json.loads(raw)
        except ValueError as error:
            raise ProviderError(
                f"Ollama returned a non-JSON reply: {error}"
            ) from error