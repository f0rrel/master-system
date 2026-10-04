import os
import sys
from unittest import mock

import pytest

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent) if False else ".")

sys.path.insert(0, '.')

from core.deepseek_provider import (
    DEFAULT_MAX_COMPLETION_TOKENS,
    DEFAULT_REASONING_EFFORT,
    DeepSeekProvider,
)
from core.provider import ProviderError


class DummyResponse:
    def __init__(self, body: bytes = b'{"choices":[{"message":{"content":"ok"}}]}', status: int = 200):
        self.body = body
        self.status = status

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False


def make_success(content="ok"):
    import json
    payload = {"choices": [{"message": {"content": content}}]}
    return DummyResponse(json.dumps(payload).encode("utf-8"))


def test_default_reasoning_effort_is_low(monkeypatch):
    provider = DeepSeekProvider()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    captured = []

    def capture(req, timeout=None):
        captured.append(req)
        return make_success()

    with mock.patch("urllib.request.urlopen", side_effect=capture):
        provider.complete("hi")
    body = __import__("json").loads(captured[0].data.decode("utf-8"))
    assert body["reasoning_effort"] == DEFAULT_REASONING_EFFORT
    assert body["reasoning_effort"] == "low"


def test_reasoning_effort_configurable(monkeypatch):
    provider = DeepSeekProvider(reasoning_effort="medium")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    captured = []

    def capture(req, timeout=None):
        captured.append(req)
        return make_success()

    with mock.patch("urllib.request.urlopen", side_effect=capture):
        provider.complete("hi")
    body = __import__("json").loads(captured[0].data.decode("utf-8"))
    assert body["reasoning_effort"] == "medium"


def test_max_completion_tokens_passed(monkeypatch):
    provider = DeepSeekProvider(max_completion_tokens=1500)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    captured = []

    def capture(req, timeout=None):
        captured.append(req)
        return make_success()

    with mock.patch("urllib.request.urlopen", side_effect=capture):
        provider.complete("hi")
    body = __import__("json").loads(captured[0].data.decode("utf-8"))
    assert body["max_completion_tokens"] == 1500


def test_provider_failure_produces_controlled_failure(monkeypatch):
    provider = DeepSeekProvider()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")

    def fail(req, timeout=None):
        raise RuntimeError("boom")

    with mock.patch("urllib.request.urlopen", side_effect=fail):
        with pytest.raises(ProviderError):
            provider.complete("hi")


def test_timeout_produces_controlled_failure(monkeypatch):
    import socket

    provider = DeepSeekProvider(timeout=1)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")

    with mock.patch("urllib.request.urlopen", side_effect=socket.timeout("t")):
        with pytest.raises(ProviderError):
            provider.complete("hi")


def test_one_call_per_complete(monkeypatch):
    provider = DeepSeekProvider()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    calls = [0]

    def capture(req, timeout=None):
        calls[0] += 1
        return make_success()

    with mock.patch("urllib.request.urlopen", side_effect=capture):
        provider.complete("hi")
    assert calls[0] == 1
