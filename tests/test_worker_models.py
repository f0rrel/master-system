"""The weekly check of free worker models, and limits seen by the OpenCode backend."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from core.notify import Notifier
from core.run_config import WorkerConfig
from core.worker_models import FreeModelCheck, parse_models

NOW = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)
FREE = {"cost": {"input": 0, "output": 0}, "status": "active"}


def listing(models):
    return "\n".join(f"{name}\n{json.dumps(meta, indent=2)}" for name, meta in models.items())


def config(**profiles):
    return SimpleNamespace(worker=WorkerConfig(profiles=profiles, model_check_days=7))


def test_the_verbose_model_listing_is_parsed():
    models = parse_models(listing({"opencode/big-pickle": FREE, "opencode/x-free": {}}))
    assert models["opencode/big-pickle"]["cost"]["input"] == 0
    assert set(models) == {"opencode/big-pickle", "opencode/x-free"}


def test_free_models_are_checked_weekly_and_problems_notified(tmp_path):
    cfg = config(**{
        "big-pickle": {"model": "opencode/big-pickle", "label": "Big Pickle"},
        "bunny": {"model": "opencode/space-bunny-free", "label": "Space Bunny"},
        "gone": {"model": "opencode/gone-free", "label": "Gone"},
        "deepseek": {"model": "deepseek/deepseek-flash", "paid": True}})
    models = {"opencode/big-pickle": {**FREE, "cost": {"input": 0.2, "output": 1}},
              "opencode/space-bunny-free": FREE}
    calls, clock, notifier = [], SimpleNamespace(now=NOW), Notifier(topic=None)

    def lister(provider, home):
        calls.append(provider)
        return listing(models)

    check = FreeModelCheck(cfg, tmp_path, lister, notifier, now=lambda: clock.now)
    lines = check()
    assert calls == ["opencode"]  # paid profiles are not checked; one listing per provider
    [sent] = notifier.sent
    assert "Big Pickle (opencode/big-pickle) is no longer free" in sent["message"]
    assert "Gone (opencode/gone-free) is no longer offered" in sent["message"]
    assert "Space Bunny" not in sent["message"]
    assert lines[0].startswith("free worker models:")
    clock.now = NOW + timedelta(days=6)
    assert check() == [] and len(calls) == 1
    clock.now = NOW + timedelta(days=7)
    check()
    assert len(calls) == 2


def test_an_unlistable_provider_is_a_problem(tmp_path):
    cfg = config(bp={"model": "opencode/big-pickle"})
    notifier = Notifier(topic=None)
    FreeModelCheck(cfg, tmp_path, lambda p, h: None, notifier, now=lambda: NOW)()
    assert "could not list opencode models" in notifier.sent[0]["message"]


def test_the_backend_reports_a_limit(tmp_path):
    from conftest import workspace_for
    from core.opencode_backend import OpenCodeCliBackend

    script = tmp_path / "opencode"
    script.write_text("#!/bin/sh\necho '{\"type\":\"error\",\"error\":{\"name\":\"APIError\","
                      "\"data\":{\"message\":\"Rate limit exceeded, try again in 30 minutes\","
                      "\"statusCode\":429}}}'\nexit 1\n")
    script.chmod(0o755)
    result = OpenCodeCliBackend(opencode_bin=script).execute(
        {"id": "t1", "title": "T"}, {}, workspace=workspace_for(tmp_path / "w"))
    assert result.status == "failed" and "limited (rate_limited)" in result.reason
    assert result.artifacts["limit"]["kind"] == "rate_limited"
    assert result.artifacts["limit"]["reset_at"] != "unknown"
