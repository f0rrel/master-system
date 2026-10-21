"""A secret echoed in a provider's error text never reaches limit state, probe output or history."""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from core.worker_limits import LimitState, classify_failure, describe, probe_profile
from test_worker_probe import FakeOpenCode, reply

NOW = datetime(2026, 10, 6, 2, 10, tzinfo=timezone.utc)
SECRET = "sk-" + "a1b2c3d4e5f6a7b8c9d0e1f2"


def error_event(message):
    return json.dumps({"type": "error", "error": {"name": "APIError",
                                                   "data": {"message": message}}})


@pytest.mark.parametrize("stdout, stderr, kind", [
    (error_event(f"429 rate limit exceeded; key {SECRET} rejected"), "", "rate_limited"),
    ("", f"Error: 429 Too Many Requests\nDEEPSEEK_API_KEY={SECRET}", "rate_limited"),
    (error_event(f"Payment Required for Authorization: Bearer {SECRET}zzzz"), "",
     "model_unavailable"),
    (error_event(f"Unexpected failure calling https://bot:{SECRET}@api.example.com/v1"), "",
     "provider_error"),
])
def test_the_limit_detail_is_redacted_where_it_is_made(stdout, stderr, kind):
    limit = classify_failure(stdout, stderr, 1, 0, NOW)

    assert limit["kind"] == kind
    assert SECRET not in json.dumps(limit)
    assert limit["detail"]  # still says what happened


def test_the_diagnosis_survives_redaction():
    limit = classify_failure(error_event(f"429 rate limit exceeded for key {SECRET}"), "", 1, 0,
                             NOW)
    assert "rate limit exceeded" in limit["detail"] and "<redacted>" in limit["detail"]


def test_a_secret_handed_to_the_limit_state_is_not_written_to_disk(tmp_path):
    state = LimitState(tmp_path, now=lambda: NOW)
    info = {"kind": "provider_error", "reset_at": "unknown",
            "detail": f"upstream said: OPENAI_API_KEY={SECRET} is invalid"}

    record = state.record_limit("alpha", "free", info)

    assert SECRET not in json.dumps(record)
    on_disk = "".join(p.read_text() for p in Path(tmp_path).rglob("*") if p.is_file())
    assert on_disk and SECRET not in on_disk
    assert "is invalid" in record["detail"]


def test_what_the_owner_is_shown_about_a_limit_has_no_secret(tmp_path):
    state = LimitState(tmp_path, now=lambda: NOW)
    state.record_limit("alpha", "free", {"kind": "rate_limited", "reset_at": "unknown",
                                         "detail": f"429 for key {SECRET}"})

    line = describe("alpha", state.get("alpha"), {"free": "Free"}, NOW)

    assert line and SECRET not in line


@pytest.mark.parametrize("fake", [
    FakeOpenCode("", returncode=1, stderr=f"429 Too Many Requests, key {SECRET}"),
    FakeOpenCode("", returncode=3, stderr=f"fatal: could not log in with {SECRET}"),
    FakeOpenCode(reply(f"My key is {SECRET}, how can I help?")),
])
def test_a_probe_result_shown_or_recorded_has_no_secret(fake, tmp_path):
    result = probe_profile({"model": "acme/free-1", "home": None, "paid": False, "label": "A"},
                           opencode_bin=Path("/opt/opencode"), extra_args=(),
                           env={"HOME": str(tmp_path / "home"), "PATH": "/usr/bin"},
                           log_dir=tmp_path / "logs", run=fake)

    assert result["ok"] is False and result["reason"]
    # `ms worker` prints result["reason"] and stores the whole result as a human_action.
    assert SECRET not in json.dumps(result)


def test_a_clean_probe_failure_keeps_its_wording(tmp_path):
    fake = FakeOpenCode("", returncode=2, stderr="connection refused")
    result = probe_profile({"model": "acme/free-1", "home": None, "paid": False, "label": "A"},
                           opencode_bin=Path("/opt/opencode"), extra_args=(),
                           env={"HOME": str(tmp_path / "home"), "PATH": "/usr/bin"},
                           log_dir=tmp_path / "logs", run=fake)
    assert "connection refused" in result["reason"]
