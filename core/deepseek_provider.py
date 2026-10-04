"""DeepSeek adapter: hosted reasoning model via DeepSeek's HTTP API.

This is a concrete implementation of :class:`core.provider.ReasoningProvider`.
It connects directly to DeepSeek's REST API using Python's standard library,
with no additional SDK dependencies.

Architecture notes
-----------------
This adapter follows the same thin-adapter pattern as the other providers:
it receives a prompt, returns text, and converts all failures to
:class:`ProviderError`. It has no knowledge of operations, project state,
approval flow, or Master.

Authentication
-------------
The API key is read exclusively from the ``DEEPSEEK_API_KEY`` environment
variable. The key is never logged, written to disk, or exposed in error
messages.

API usage
---------
Uses DeepSeek's chat completions API (OpenAI-compatible format). The adapter
enables the model's reasoning/thinking capability where supported, but only
returns the final textual answer - internal reasoning content is not exposed
as the provider's result.

Timeout conventions
-------------------
Respects the provider's timeout parameter for HTTP requests, consistent with
other provider implementations.
"""

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.provider import ProviderError, ReasoningProvider

__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "DEFAULT_REASONING_EFFORT",
    "DEFAULT_MAX_COMPLETION_TOKENS",
    "DeepSeekProvider",
]

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-pro"
DEFAULT_REASONING_EFFORT = "low"
DEFAULT_MAX_COMPLETION_TOKENS = 4000

# Temperature 0 for reproducible behavior when possible
GENERATION_OPTIONS = {"temperature": 0}


class DeepSeekProvider(ReasoningProvider):
    """Ask a reasoning model served by DeepSeek's API to complete a prompt."""

    def __init__(self, base_url=DEFAULT_BASE_URL, model=DEFAULT_MODEL, timeout=180,
                 reasoning_effort=DEFAULT_REASONING_EFFORT,
                 max_completion_tokens=DEFAULT_MAX_COMPLETION_TOKENS):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.reasoning_effort = reasoning_effort
        self.max_completion_tokens = max_completion_tokens

    @property
    def name(self):
        return f"deepseek:{self.model}"

    def complete(self, prompt, schema=None):
        """POST the prompt to DeepSeek's chat completions API and return final text.

        When schema is supplied, it may be passed if supported by the API;
        however, correctness does not depend on it as the engine validates
        the reply regardless. The provider returns only the final textual
        answer, not internal reasoning/thinking content.
        """
        api_key = os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            raise ProviderError("DEEPSEEK_API_KEY environment variable is not set")

        request_body = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            **GENERATION_OPTIONS,
        }

        # Enable reasoning/thinking where supported.
        request_body["thinking"] = {"type": "enabled"}
        request_body["reasoning_effort"] = self.reasoning_effort
        if self.max_completion_tokens is not None:
            request_body["max_completion_tokens"] = self.max_completion_tokens

        # Do not force response_format with json_schema as it can be incompatible
        # with current DeepSeek API versions; schema is advisory and engine
        # validates the reply. Only use json_object if the prompt explicitly
        # asks for JSON.
        if schema is not None and "json" in str(prompt).lower():
            try:
                request_body["response_format"] = {"type": "json_object"}
            except Exception:
                pass

        payload = self._post("/chat/completions", request_body, api_key)

        try:
            choices = payload["choices"]
            if not choices:
                raise KeyError("empty choices")
            choice = choices[0]
            message = choice["message"]
            content = message.get("content") or message.get("text")
        except (KeyError, TypeError, IndexError) as error:
            raise ProviderError(
                f"DeepSeek returned an unexpected response structure: {error}"
            ) from error

        if not isinstance(content, str) or not content:
            # Sometimes reasoning models might put answer differently; try to
            # extract from reasoning_content if content is empty
            try:
                message = payload["choices"][0]["message"]
                # Don't expose reasoning content - only use if explicitly
                # structured as final answer, but per requirement we must not
                # expose internal reasoning as the returned answer
                content = message.get("content")
            except Exception:
                pass

        if not isinstance(content, str) or not content:
            raise ProviderError("DeepSeek returned no usable text content")

        return content

    def _post(self, path, body, api_key):
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        return self._send(request)

    def _send(self, request):
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            status = error.code
            try:
                body = error.read()
                if body:
                    try:
                        parsed = json.loads(body)
                        msg = parsed.get("error", {}).get("message") or parsed.get("message") or body.decode("utf-8", errors="replace")
                    except Exception:
                        msg = body.decode("utf-8", errors="replace")
                else:
                    msg = ""
            except Exception:
                msg = ""
            if status in (401, 403):
                raise ProviderError(
                    f"DeepSeek authentication failed (HTTP {status}): {msg}"
                ) from error
            # Include API error details for debugging, but avoid sensitive data
            if msg:
                raise ProviderError(
                    f"DeepSeek returned HTTP {status} for {request.full_url}: {msg}"
                ) from error
            raise ProviderError(
                f"DeepSeek returned HTTP {status} for {request.full_url}"
            ) from error
        except urllib.error.URLError as error:
            reason = getattr(error, "reason", str(error))
            raise ProviderError(
                f"Cannot reach DeepSeek at {request.full_url}: {reason}"
            ) from error
        except OSError as error:
            raise ProviderError(
                f"Cannot reach DeepSeek at {request.full_url}: {error}"
            ) from error
        except Exception as error:
            raise ProviderError(
                f"DeepSeek request failed: {error}"
            ) from error

        try:
            return json.loads(raw)
        except ValueError as error:
            raise ProviderError(
                f"DeepSeek returned a non-JSON reply: {error}"
            ) from error
