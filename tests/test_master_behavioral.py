import sys
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from core.master import Master
from core.reasoning import ReasoningInterface, Decision
from core.reasoning_engine import ReasoningEngine, ReasoningError


class ScriptedProvider:
    def __init__(self, reply=None, error=None):
        self.reply = reply if reply is not None else '{"decision":"wait","reason":"ok","operation":null}'
        self.error = error
        self.prompts = []
        self.schemas = []
        self.calls = 0

    def complete(self, prompt, schema=None):
        self.calls += 1
        self.prompts.append(prompt)
        self.schemas.append(schema)
        if self.error is not None:
            raise self.error
        return self.reply


def write_min_project(tmp_path, project_id="testp"):
    proj = tmp_path / "projects" / project_id
    proj.mkdir(parents=True)
    (proj / "project.yaml").write_text(
        f"id: {project_id}\nname: Test\nstatus: active\n"
    )
    (proj / "milestones.yaml").write_text(
        "milestones:\n  - id: m1\n    name: M1\n    status: in_progress\n"
    )
    (proj / "tasks.yaml").write_text(
        "tasks:\n"
        "  - id: t1\n    milestone: m1\n    title: T1\n    status: planned\n    assigned_to: master\n"
        "  - id: t2\n    milestone: m1\n    title: T2\n    status: in_progress\n    assigned_to: master\n"
    )
    return tmp_path / "projects"


@pytest.fixture
def small_root(tmp_path):
    return write_min_project(tmp_path)


def engine_with(reply, root):
    m = Master(root)
    p = ScriptedProvider(reply=reply)
    e = ReasoningEngine(p, m, ReasoningInterface(m))
    return e, p, m


def act_reply(op_payload):
    import json

    return json.dumps(
        {
            "decision": "act",
            "reason": "actionable",
            "operation": op_payload,
        }
    )


def nonact(decision, reason="ok"):
    import json

    assert decision in ("wait", "blocked", "needs_information")
    return json.dumps({"decision": decision, "reason": reason, "operation": None})


def req_app(op_payload, reason="needs approval"):
    import json

    return json.dumps(
        {
            "decision": "request_approval",
            "reason": reason,
            "operation": op_payload,
        }
    )


def test_actionable_work_act(small_root):
    op = {
        "operation": "update_task",
        "project_id": "testp",
        "task_id": "t2",
        "status": "completed",
    }
    e, p, _ = engine_with(act_reply(op), small_root)
    prop = e.reason("complete t2", "testp")
    assert prop._decision is not None
    assert prop._decision.decision is Decision.ACT
    assert prop._decision.operation is not None
    assert p.calls == 1


def test_nothing_useful_wait(small_root):
    e, p, _ = engine_with(nonact("wait", "nothing useful"), small_root)
    prop = e.reason("do nothing", "testp")
    assert prop._decision.decision is Decision.WAIT
    assert prop._decision.operation is None
    assert p.calls == 1


def test_explicitly_blocked(small_root):
    e, p, _ = engine_with(nonact("blocked", "blocked"), small_root)
    prop = e.reason("handle blocked", "testp")
    assert prop._decision.decision is Decision.BLOCKED
    assert prop._decision.operation is None
    assert p.calls == 1


def test_missing_information(small_root):
    e, p, _ = engine_with(nonact("needs_information", "need info"), small_root)
    prop = e.reason("clarify", "testp")
    assert prop._decision.decision is Decision.NEEDS_INFORMATION
    assert prop._decision.operation is None
    assert p.calls == 1


def test_request_approval(small_root):
    op = {
        "operation": "create_task",
        "project_id": "testp",
        "task_id": "t3",
        "milestone": "m1",
        "title": "T3",
        "status": "planned",
        "assigned_to": "master",
    }
    e, p, _ = engine_with(req_app(op), small_root)
    prop = e.reason("propose new task", "testp")
    assert prop._decision.decision is Decision.REQUEST_APPROVAL
    assert prop._decision.operation is not None
    assert p.calls == 1


def test_multiple_possible_actions_choice(small_root):
    op = {
        "operation": "update_task",
        "project_id": "testp",
        "task_id": "t2",
        "status": "completed",
    }
    e, p, _ = engine_with(act_reply(op), small_root)
    prop = e.reason("pick smallest useful next step", "testp")
    assert prop._decision.decision is Decision.ACT
    assert prop._decision.operation is not None
    assert p.calls == 1


def test_goal_reprioritization(small_root):
    e, p, _ = engine_with(nonact("wait"), small_root)
    e.reason("goal A", "testp")
    goal_a = p.prompts[0] if p.prompts else ""
    e2, p2, _ = engine_with(nonact("wait"), small_root)
    e2.reason("goal B", "testp")
    goal_b = p2.prompts[0] if p2.prompts else ""
    assert "goal B" in goal_b
    assert "goal A" not in goal_b
    assert p.calls == 1 and p2.calls == 1
