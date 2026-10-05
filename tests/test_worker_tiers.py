"""Worker tiers: the orchestrator picks the profile from history; escalation never goes down."""

import pytest

from core.history import EventType, InMemoryHistoryStore
from core.report import tier_stats
from core.run_config import ConfigError, load_config
from core.worker_tiers import choose_tier
from test_m2_dry_run import toy  # noqa: F401

LADDER = ("tier0", "tier1", "tier2")


def attempt(history, n, tier, outcome="finished", verdict="fail", task_id="t1"):
    attempt_id = f"{n:032x}"
    ids = dict(run_id="r", project_id="p", task_id=task_id, attempt_id=attempt_id)
    history.append(type=EventType.ATTEMPT_STARTED, event_id=attempt_id, run_id="r",
                   project_id="p", task_id=task_id,
                   payload={"worker_tier": tier, "worker_profile": LADDER[tier],
                            "worker_model": [None, "deepseek/deepseek-v4-flash",
                                             "deepseek/deepseek-v4-pro"][tier]})
    history.append(type=EventType.ATTEMPT_FINISHED, **ids, payload={
        "outcome": outcome, "worker_reported_usage": {"input_tokens": 1_000_000,
                                                      "output_tokens": 0}})
    if verdict:
        history.append(type=EventType.VERIFICATION, **ids, payload={"verdict": verdict})


@pytest.mark.parametrize("size,tier", [(None, 0), ("small", 0), ("medium", 1), ("hard", 2)])
def test_size_sets_the_starting_tier(size, tier):
    task = {"id": "t1", "size": size} if size else {"id": "t1"}
    assert choose_tier(InMemoryHistoryStore(), "p", task, LADDER).tier == tier


def test_a_short_ladder_caps_the_start():
    assert choose_tier(InMemoryHistoryStore(), "p", {"id": "t1", "size": "hard"},
                       ("tier0",)).profile == "tier0"


def test_two_failures_move_up_one_tier():
    history = InMemoryHistoryStore()
    attempt(history, 1, 0)
    assert choose_tier(history, "p", {"id": "t1"}, LADDER).tier == 0
    attempt(history, 2, 0, outcome="timed_out", verdict=None)
    choice = choose_tier(history, "p", {"id": "t1"}, LADDER)
    assert (choice.tier, choice.profile, choice.escalated_from) == (1, "tier1", 0)


def test_infrastructure_failures_do_not_escalate():
    history = InMemoryHistoryStore()
    attempt(history, 1, 0, outcome="error", verdict=None)
    attempt(history, 2, 0, outcome="interrupted", verdict=None)
    attempt(history, 3, 0)
    assert choose_tier(history, "p", {"id": "t1"}, LADDER).tier == 0


def test_the_tier_never_goes_down():
    history = InMemoryHistoryStore()
    attempt(history, 1, 1)
    assert choose_tier(history, "p", {"id": "t1", "size": "small"}, LADDER).tier == 1


def test_the_top_tier_stays_at_the_top():
    history = InMemoryHistoryStore()
    for n in range(4):
        attempt(history, n + 1, 2)
    choice = choose_tier(history, "p", {"id": "t1"}, LADDER)
    assert choice.tier == 2 and choice.escalated_from is None


def test_tier_stats_per_tier():
    history = InMemoryHistoryStore()
    attempt(history, 1, 0)
    attempt(history, 2, 0)
    attempt(history, 3, 1, verdict="pass")
    prices = {"deepseek-v4-flash": {"input": 0.44, "output": 1.32}}
    rows = tier_stats(history, prices, project_id="p")
    assert [(r["tier"], r["attempts"], r["passes"]) for r in rows] == [(0, 2, 0), (1, 1, 1)]
    assert rows[1]["cost_usd"] == pytest.approx(0.44) and rows[1]["success_rate"] == 1.0
    assert rows[0]["model"] == "worker default model"


def test_ladder_config(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text('[worker]\nladder = ["tier0", "tier1"]\n'
                    '[worker.profiles.tier0]\nhome = "~/w0"\n'
                    '[worker.profiles.tier1]\nmodel = "deepseek/deepseek-v4-flash"\n'
                    'home = "~/w1"\n')
    config = load_config(path)
    assert config.worker.ladder == ("tier0", "tier1")
    assert config.worker.profiles["tier1"]["model"] == "deepseek/deepseek-v4-flash"
    assert config.worker.profiles["tier0"]["model"] is None
    path.write_text('[worker]\nladder = ["ollama"]\n')
    with pytest.raises(ConfigError):
        load_config(path)


def test_the_default_ladder_is_empty_and_never_ollama():
    assert load_config("/nonexistent").worker.ladder == ()


def test_masters_context_never_names_a_profile():
    from core.evidence import attempts_for_task

    history = InMemoryHistoryStore()
    attempt(history, 1, 1)
    [summary] = attempts_for_task(history, "p", "t1")
    context = repr(summary.to_context())
    assert "tier" not in context and "deepseek" not in context


def test_a_real_run_records_the_profile_on_the_attempt(toy, monkeypatch):  # noqa: F811
    from core import run_cli
    from core.sqlite_history import SQLiteHistoryStore
    from core.worker_env import find_node_bin
    from test_m2_dry_run import ScriptedMaster, act, cli

    if find_node_bin(22) is None:
        monkeypatch.setattr(run_cli, "build_worker_env",
                            lambda config, home=None: {"PATH": "/usr/bin:/bin",
                                                       "HOME": str(home or "/tmp")})
    config = toy["config"]
    config.write_text(config.read_text().replace(
        "[prices]", 'ladder = ["tier0", "tier1"]\n\n'
        f'[worker.profiles.tier0]\nhome = "{config.parent / "w0"}"\n\n'
        f'[worker.profiles.tier1]\nmodel = "toy/paid"\nhome = "{config.parent / "w1"}"\n\n'
        "[prices]", 1))
    assert cli(toy, "task", "set-acceptance", "toy", "fix-greet",
               "--command", "grep -q \"'Hi '\" greet.py")[0] == 0
    master = ScriptedMaster([
        act({"operation": "update_task", "project_id": "toy", "task_id": "fix-greet",
             "status": "in_progress"}),
        act({"operation": "run_task", "project_id": "toy", "task_id": "fix-greet"}),
        act({"operation": "update_task", "project_id": "toy", "task_id": "fix-greet",
             "status": "completed"})])
    monkeypatch.setattr(run_cli, "build_provider", lambda c: master)
    cli(toy, "start", "toy", "--objective", "x", "--session", "tiers")
    history = SQLiteHistoryStore(toy["state"] / "history.sqlite")
    [started] = history.events(types=[EventType.ATTEMPT_STARTED])
    assert started.payload["worker_profile"] == "tier0"
    assert started.payload["worker_tier"] == 0 and started.payload["worker_model"] is None
