import sys
from unittest import mock

import pytest

sys.path.insert(0, ".")

from core.master import Master
from core.provider import ProviderError
from core.reasoning import ReasoningInterface, Decision
from core.reasoning_engine import ReasoningEngine, ReasoningError


class FakeProvider:
    def __init__(self, reply=None, error=None, calls=0):
        self._reply = reply
        self._error = error
        self.calls = calls

    def complete(self, prompt, schema=None):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._reply


def make_engine(reply, master=None):
    if master is None:
        master = Master("projects")
    p = FakeProvider(reply=reply)
    return ReasoningEngine(p, master, ReasoningInterface(master)), p


def test_decision_act():
    reply = '{"decision":"act","reason":"do it","operation":{"operation":"create_task","project_id":"ai-system","task_id":"t1","milestone":"ai-system","title":"T"}}'
    e, p = make_engine(reply)
    prop = e.parse(reply)
    assert prop.reasoning == "do it"
    assert len(prop.entries) == 1
    assert p.calls == 0  # we didn't call complete via parse; separate


def test_decision_wait():
    reply = '{"decision":"wait","reason":"no action","operation":null}'
    e, _ = make_engine(reply)
    prop = e.parse(reply)
    assert prop.reasoning == "no action"
    assert len(prop.entries) == 0


def test_decision_blocked():
    reply = '{"decision":"blocked","reason":"blocked","operation":null}'
    e, _ = make_engine(reply)
    prop = e.parse(reply)
    assert prop.reasoning == "blocked"
    assert len(prop.entries) == 0


def test_decision_needs_information():
    reply = '{"decision":"needs_information","reason":"need info","operation":null}'
    e, _ = make_engine(reply)
    prop = e.parse(reply)
    assert prop.reasoning == "need info"
    assert len(prop.entries) == 0


def test_decision_request_approval():
    reply = '{"decision":"request_approval","reason":"needs approval","operation":{"operation":"create_task","project_id":"ai-system","task_id":"t2","milestone":"ai-system","title":"T2"}}'
    e, _ = make_engine(reply)
    prop = e.parse(reply)
    assert len(prop.entries) == 1


def test_invalid_decision_type():
    reply = '{"decision":"invalid","reason":"x","operation":null}'
    e, _ = make_engine(reply)
    with pytest.raises(ReasoningError):
        e.parse(reply)


def test_act_without_operation_invalid():
    reply = '{"decision":"act","reason":"x","operation":null}'
    e, _ = make_engine(reply)
    with pytest.raises(ReasoningError):
        e.parse(reply)


def test_wait_with_operation_invalid():
    reply = '{"decision":"wait","reason":"x","operation":{"operation":"create_task","project_id":"ai-system","task_id":"t3","milestone":"ai-system","title":"T3"}}'
    e, _ = make_engine(reply)
    with pytest.raises(ReasoningError):
        e.parse(reply)


def test_blocked_with_operation_invalid():
    reply = '{"decision":"blocked","reason":"x","operation":{"operation":"create_task","project_id":"ai-system","task_id":"t3","milestone":"ai-system","title":"T3"}}'
    e, _ = make_engine(reply)
    with pytest.raises(ReasoningError):
        e.parse(reply)


def test_request_approval_without_operation_invalid():
    reply = '{"decision":"request_approval","reason":"x","operation":null}'
    e, _ = make_engine(reply)
    with pytest.raises(ReasoningError):
        e.parse(reply)


def test_provider_called_at_most_once():
    reply = '{"decision":"wait","reason":"ok","operation":null}'
    master = Master("projects")
    p = FakeProvider(reply=reply)
    e = ReasoningEngine(p, master, ReasoningInterface(master))
    prop = e.reason("do nothing", "ai-system")
    assert p.calls == 1


def test_no_automatic_retry():
    master = Master("projects")
    p = FakeProvider(error=ProviderError("fail"))
    e = ReasoningEngine(p, master, ReasoningInterface(master))
    with pytest.raises(ReasoningError):
        e.reason("test", "ai-system")
    assert p.calls == 1
