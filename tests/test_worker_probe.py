"""A worker profile the owner switches to must answer one small probe first."""

import io
import json
from pathlib import Path

import pytest

from core.history import EventType
from core.ms import main as ms_main
from core.paths import RuntimePaths
from core.sqlite_history import SQLiteHistoryStore
from core.worker_limits import PROBE_ANSWER, LimitState, probe_profile
from core.worker_process import ProcessOutcome


def events(*items):
    return "\n".join(json.dumps(item) for item in items)


def reply(text=PROBE_ANSWER, model=None, cost=0.0):
    info = {"type": "step_start", "part": {"providerID": model.split("/")[0],
                                           "modelID": model.split("/", 1)[1]}} if model else {}
    return events(*([info] if info else []),
                  {"type": "text", "part": {"text": text}},
                  {"type": "step_finish", "part": {"tokens": {"input": 50, "output": 5},
                                                   "cost": cost}})


class FakeOpenCode:
    """Stands in for run_process: records the call and answers like `opencode run`."""

    def __init__(self, stdout=None, returncode=0, stderr="", timed_out=False):
        self.stdout = reply() if stdout is None else stdout
        self.returncode, self.stderr, self.timed_out = returncode, stderr, timed_out
        self.calls = []

    def __call__(self, argv, *, cwd, deadline, log_dir, env=None, **_):
        self.calls.append({"argv": list(argv), "cwd": Path(cwd), "env": dict(env or {}),
                           "files": sorted(p.name for p in Path(cwd).iterdir()),
                           "log_dir": Path(log_dir), "deadline": deadline})
        return ProcessOutcome(self.returncode, self.stdout, self.stderr, self.timed_out,
                              1, 1)


PROFILE = {"model": "acme/free-1", "home": None, "paid": False, "label": "Acme Free"}


def probe(fake, tmp_path, profile=PROFILE):
    return probe_profile(profile, opencode_bin=Path("/opt/opencode"), extra_args=(),
                         env={"HOME": str(tmp_path / "home"), "PATH": "/usr/bin"},
                         log_dir=tmp_path / "logs", run=fake)


def test_a_probe_runs_the_profiles_model_in_an_empty_directory_without_write_tools(tmp_path):
    fake = FakeOpenCode(reply(model="acme/free-1"))
    result = probe(fake, tmp_path)
    assert result["ok"] is True and result["reason"] is None
    [call] = fake.calls
    assert call["argv"][0] == "/opt/opencode" and "--model" in call["argv"]
    assert call["argv"][call["argv"].index("--model") + 1] == "acme/free-1"
    assert call["argv"][call["argv"].index("--dir") + 1] == str(call["cwd"])
    assert call["files"] == [] and not call["cwd"].exists()  # empty, then removed
    assert not str(call["cwd"]).startswith(str(RuntimePaths.default().worktrees_root))
    assert call["env"]["HOME"] == str(tmp_path / "home")
    config = json.loads(call["env"]["OPENCODE_CONFIG_CONTENT"])
    for tool in ("edit", "write", "patch", "webfetch"):
        assert config["tools"][tool] is False
    assert config["permission"]["edit"] == "deny"
    assert PROBE_ANSWER in call["argv"][-1]
    assert result["usage"]["input_tokens"] == 50


@pytest.mark.parametrize("fake, kind, words", [
    (FakeOpenCode(reply("Sure! How can I help?")), None, "did not give the expected answer"),
    (FakeOpenCode(events({"type": "error", "error": {"data": {
        "message": "Rate limit exceeded", "statusCode": 429}}}), returncode=1),
     "rate_limited", "rate_limited"),
    (FakeOpenCode("", returncode=1, stderr="Error: Provider not found: acme"),
     "not_configured", "not_configured"),
    (FakeOpenCode(reply(model="acme/other-2")), "model_unavailable", "acme/other-2"),
    (FakeOpenCode("", returncode=124, timed_out=True), None, "did not answer within"),
])
def test_a_failed_probe_says_why(tmp_path, fake, kind, words):
    result = probe(fake, tmp_path)
    assert result["ok"] is False and result["kind"] == kind
    assert words in result["reason"]


CONFIG = """
[worker]
opencode_bin = "/opt/opencode"
workers = ["free-a", "free-b", "paid-c"]
[worker.profiles.free-a]
model = "acme/free-1"
label = "Free A"
[worker.profiles.free-b]
model = "acme/free-2"
label = "Free B"
[worker.profiles.paid-c]
model = "acme/paid-3"
label = "Paid C"
paid = true
[prices."acme/paid-3"]
input = 1.0
output = 2.0
"""


@pytest.fixture
def limited(tmp_path, monkeypatch):
    """sample-project waits for the owner after free-a hit a long limit."""
    import core.worker_process

    config = tmp_path / "config.toml"
    config.write_text(CONFIG)
    state_dir = RuntimePaths.default().state_dir
    LimitState(state_dir).record_limit("sample-project", "free-a",
                                       {"kind": "model_unavailable", "reset_at": "unknown"})
    fake = FakeOpenCode()
    monkeypatch.setattr(core.worker_process, "run_process", fake)
    return {"config": str(config), "fake": fake, "limits": LimitState(state_dir)}


def run_limit(limited, choice):
    out = io.StringIO()
    code = ms_main(["--config", limited["config"], "limit", "sample-project", choice], out=out)
    return code, out.getvalue()


def probes():
    return [e.payload for e in SQLiteHistoryStore(RuntimePaths.default().history_path).events(
        project_id="sample-project", types=[EventType.HUMAN_ACTION])
        if e.payload.get("action") == "worker_probe"]


def test_a_passing_probe_switches_the_worker(limited):
    code, out = run_limit(limited, "free")
    assert code == 0, out
    assert "Free B answered a test request" in out and "continues with Free B" in out
    assert limited["limits"].override("sample-project") == "free-b"
    assert limited["limits"].paused("sample-project") is None
    [call] = limited["fake"].calls
    assert call["argv"][call["argv"].index("--model") + 1] == "acme/free-2"
    [record] = probes()
    assert record["profile"] == "free-b" and record["ok"] is True
    assert record["model"] == "acme/free-2" and record["choice"] == "free"


def test_a_failing_probe_keeps_the_project_paused_and_says_why(limited):
    limited["fake"].stdout = events({"type": "error", "error": {"data": {
        "message": "Rate limit exceeded", "statusCode": 429}}})
    limited["fake"].returncode = 1
    code, out = run_limit(limited, "free")
    assert code == 1
    assert "Free B failed a test request (rate_limited)" in out
    assert "Not switched; the project stays paused" in out
    assert "ms limit sample-project paid" in out  # the next choice
    assert limited["limits"].override("sample-project") is None
    assert limited["limits"].paused("sample-project") == "needs_choice"
    [record] = probes()
    assert record["ok"] is False and record["kind"] == "rate_limited"
    assert not [e for e in SQLiteHistoryStore(RuntimePaths.default().history_path).events(
        types=[EventType.HUMAN_ACTION]) if e.payload.get("action") == "worker_limit_choice"]


def test_a_paid_probe_counts_toward_the_daily_cap(limited):
    from core.report import spend_since

    limited["fake"].stdout = reply(cost=0.5)
    code, out = run_limit(limited, "paid")
    assert code == 0, out
    assert "the test request counts toward the daily cap" in out
    assert limited["limits"].override("sample-project") == "paid-c"
    history = SQLiteHistoryStore(RuntimePaths.default().history_path)
    prices = {"acme/paid-3": {"input": 1.0, "output": 2.0}}
    spend = spend_since(history, "2000-01-01", prices)
    assert spend["worker_usd"] == pytest.approx((50 * 1.0 + 5 * 2.0) / 1_000_000)


def test_waiting_needs_no_probe(limited):
    code, _ = run_limit(limited, "wait")
    assert code == 0 and limited["fake"].calls == [] and probes() == []


def test_the_telegram_limit_button_uses_the_same_probe(limited):
    from core.telegram import TelegramState
    from core.telegram_bot import BotOps

    limited["fake"].stdout = reply("no idea")
    ops = BotOps(TelegramState(RuntimePaths.default().state_dir), config_path=limited["config"])
    [answer] = ops.limit({"project": "sample-project", "choice": "free"})
    assert "failed a test request" in answer["text"]
    assert len(limited["fake"].calls) == 1
    assert limited["limits"].override("sample-project") is None
    assert probes()[0]["actor"] == "telegram"
