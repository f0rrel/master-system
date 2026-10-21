"""Spend figures say what they are: an estimate from reported tokens, not an invoice."""

from core.history import EventType, InMemoryHistoryStore
from core.report import SPEND_BASIS, spend_since

PRICES = {"deepseek-flash": {"input": 0.30, "output": 1.20, "cached_input": 0.006}}
LONG_AGO = "1970-01-01T00:00:00+00:00"


def finished(history, n, model, usage):
    history.append(type=EventType.ATTEMPT_FINISHED, run_id="r", project_id="p", task_id="t1",
                   attempt_id=f"{n:032x}",
                   payload={"outcome": "finished", "worker_reported_usage": usage,
                            "artifacts": {"model": model}})


def started(history, n):
    history.append(type=EventType.ATTEMPT_STARTED, event_id=f"{n:032x}", run_id="r",
                   project_id="p", task_id="t1", payload={})


def test_spend_is_labelled_an_estimate_and_keeps_the_tools_own_figure_apart():
    history = InMemoryHistoryStore()
    started(history, 1)
    finished(history, 1, "deepseek-flash", {"input_tokens": 1_000_000, "output_tokens": 0,
                                            "reported_cost_usd": 0.05})
    started(history, 2)
    finished(history, 2, "opencode/big-pickle", {"input_tokens": 10, "reported_cost_usd": 0.0})

    spend = spend_since(history, LONG_AGO, PRICES)

    assert spend["worker_usd"] == 0.3  # priced: 1M input tokens x $0.30 from the config
    assert spend["worker_reported_usd"] == 0.05  # what the worker tool claimed for itself
    assert spend["basis"] == SPEND_BASIS and "estimate" in SPEND_BASIS
    assert "not an invoice" in SPEND_BASIS


def test_an_unpriced_model_falls_back_to_the_tools_figure_in_both_numbers():
    history = InMemoryHistoryStore()
    started(history, 1)
    finished(history, 1, "some/unlisted-model", {"input_tokens": 5, "reported_cost_usd": 0.02})

    spend = spend_since(history, LONG_AGO, PRICES)

    assert spend["worker_usd"] == spend["worker_reported_usd"] == 0.02
