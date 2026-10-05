"""Unit tests for the DeepSeek reasoning provider.

These tests mock the HTTP layer and never require a real API key or
network access.
"""

import json
import os
import sys
from io import BytesIO
from unittest import mock
from pathlib import Path

import pytest

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.deepseek_provider import DEFAULT_BASE_URL, DEFAULT_MODEL, DeepSeekProvider
from core.provider import ProviderError


class DummyResponse:
    def __init__(self, body: bytes, status: int = 200):
        self.body = body
        self.status = status

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False


def make_success_response(content="Hello, world"):
    payload = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": content,
                },
                "index": 0,
                "finish_reason": "stop",
            }
        ],
        "usage": {},
    }
    return DummyResponse(json.dumps(payload).encode("utf-8"))


def make_malformed_response():
    return DummyResponse(b"not valid json")


def make_empty_choices_response():
    return DummyResponse(json.dumps({"choices": []}).encode("utf-8"))


def make_no_content_response():
    return DummyResponse(
        json.dumps({"choices": [{"message": {}}]}).encode("utf-8")
    )


def test_successful_completion(monkeypatch):
    provider = DeepSeekProvider()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")

    with mock.patch("urllib.request.urlopen", return_value=make_success_response("Final answer")):
        result = provider.complete("What is 2+2?")
        assert result == "Final answer"


def test_missing_api_key(monkeypatch):
    provider = DeepSeekProvider()
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    with pytest.raises(ProviderError) as exc_info:
        provider.complete("test")

    assert "DEEPSEEK_API_KEY" in str(exc_info.value)


def test_http_error(monkeypatch):
    provider = DeepSeekProvider()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")

    def raise_http_error(request, timeout=None):
        import urllib.error

        raise urllib.error.HTTPError(
            url=request.full_url,
            code=500,
            msg="Internal Server Error",
            hdrs=None,
            fp=BytesIO(b"error"),
        )

    with mock.patch("urllib.request.urlopen", side_effect=raise_http_error):
        with pytest.raises(ProviderError) as exc_info:
            provider.complete("test")

    assert "HTTP 500" in str(exc_info.value)


def test_authentication_error(monkeypatch):
    provider = DeepSeekProvider()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "bad-key")

    def raise_auth_error(request, timeout=None):
        import urllib.error

        raise urllib.error.HTTPError(
            url=request.full_url,
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=BytesIO(b"unauthorized"),
        )

    with mock.patch("urllib.request.urlopen", side_effect=raise_auth_error):
        with pytest.raises(ProviderError) as exc_info:
            provider.complete("test")

    assert "authentication failed" in str(exc_info.value)


def test_timeout(monkeypatch):
    provider = DeepSeekProvider(timeout=5)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")

    import socket

    with mock.patch("urllib.request.urlopen", side_effect=socket.timeout("timed out")):
        with pytest.raises(ProviderError):
            provider.complete("test")


def test_malformed_api_response(monkeypatch):
    provider = DeepSeekProvider()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")

    with mock.patch("urllib.request.urlopen", return_value=make_malformed_response()):
        with pytest.raises(ProviderError) as exc_info:
            provider.complete("test")

    assert "non-JSON" in str(exc_info.value)


def test_extraction_of_final_text(monkeypatch):
    provider = DeepSeekProvider()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")

    payload = {
        "choices": [
            {
                "message": {"content": "The final answer is 42"},
                "finish_reason": "stop",
            }
        ]
    }
    response = DummyResponse(json.dumps(payload).encode("utf-8"))

    with mock.patch("urllib.request.urlopen", return_value=response):
        result = provider.complete("compute 6*7")
        assert result == "The final answer is 42"


def test_configurable_model(monkeypatch):
    provider = DeepSeekProvider(model="deepseek-flash")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")

    captured = []

    def capture_request(request, timeout=None):
        captured.append(request)
        return make_success_response("ok")

    with mock.patch("urllib.request.urlopen", side_effect=capture_request):
        provider.complete("test")

    assert len(captured) == 1
    # Check the request body contains the model
    # The request data is bytes
    body = json.loads(captured[0].data.decode("utf-8"))
    assert body["model"] == "deepseek-flash"


def test_configurable_base_url(monkeypatch):
    provider = DeepSeekProvider(base_url="https://custom.deepseek.com/v1")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")

    captured = []

    def capture_request(request, timeout=None):
        captured.append(request)
        return make_success_response("ok")

    with mock.patch("urllib.request.urlopen", side_effect=capture_request):
        provider.complete("test")

    assert len(captured) == 1
    assert "custom.deepseek.com" in captured[0].full_url


def test_empty_content_raises(monkeypatch):
    provider = DeepSeekProvider()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")

    with mock.patch("urllib.request.urlopen", return_value=make_no_content_response()):
        with pytest.raises(ProviderError):
            provider.complete("test")


# --- token usage (accounting) -------------------------------------------------


def test_usage_is_read_from_the_response(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    payload = {
        "choices": [{"message": {"role": "assistant", "content": "ok"}, "index": 0}],
        "usage": {"prompt_tokens": 5000, "completion_tokens": 300,
                  "prompt_cache_hit_tokens": 4096, "prompt_cache_miss_tokens": 904,
                  "completion_tokens_details": {"reasoning_tokens": 120}},
    }
    provider = DeepSeekProvider()
    with mock.patch("urllib.request.urlopen",
                    return_value=DummyResponse(json.dumps(payload).encode("utf-8"))):
        provider.complete("prompt")

    assert provider.last_usage == {"input_tokens": 904, "cached_input_tokens": 4096,
                                   "cache_write_tokens": 0, "output_tokens": 300,
                                   "reasoning_tokens": 120, "reported_cost_usd": 0.0}


def test_usage_is_kept_even_when_the_reply_has_no_text(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    payload = {"choices": [{"message": {"role": "assistant", "content": ""}}],
               "usage": {"prompt_tokens": 100, "completion_tokens": 0}}
    provider = DeepSeekProvider()
    with mock.patch("urllib.request.urlopen",
                    return_value=DummyResponse(json.dumps(payload).encode("utf-8"))):
        with pytest.raises(ProviderError):
            provider.complete("prompt")

    assert provider.last_usage["input_tokens"] == 100
