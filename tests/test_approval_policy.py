"""Tests for the approval policy: which operations run without a human.

The policy is a pure function of the operation, so most of what matters here is
that it ignores everything else -- model, provider, worker, QA verdict, and the
project the operation targets -- and denies by default.
"""

from types import MappingProxyType

import pytest
import yaml

from core import reasoning
from core.master import Master
from core.reasoning import (
    SPECS,
    ApprovalState,
    Decision,
    ImpactLevel,
    MasterDecision,
    Operation,
    OperationSpec,
    ReasoningInterface,
    RequestError,
    ResultStatus,
    requires_approval,
)

ALPHA = {"id": "alpha", "name": "Alpha", "status": "in_progress"}


def write_project(root, project_id="alpha"):
    """Create a project directory directly under root, which is Master's root."""
    project = root / f"{project_id}-project"
    project.mkdir(parents=True)
    (project / "project.yaml").write_text(
        yaml.safe_dump({"id": project_id, "name": project_id, "status": "active"})
    )
    (project / "milestones.yaml").write_text(yaml.safe_dump({"milestones": [ALPHA]}))
    (project / "tasks.yaml").write_text(yaml.safe_dump({"tasks": []}))
    return project


@pytest.fixture
def master(tmp_path):
    write_project(tmp_path)
    return Master(tmp_path)


@pytest.fixture
def interface(master):
    return ReasoningInterface(master)


@pytest.fixture
def gated_specs(monkeypatch):
    """Widen the allowlist with one ELEVATED and one CRITICAL operation.

    Neither exists for real: nothing in the allowlist can reach outside
    ProjectState. These stand in for the high-impact operations that will exist
    when workspace promotion or merge becomes reachable.
    """
    widened = MappingProxyType(
        {
            **SPECS,
            "elevated_change": OperationSpec(
                "update_task",
                ("project_id", "task_id"),
                variadic=True,
                impact=ImpactLevel.ELEVATED,
            ),
            "critical_change": OperationSpec(
                "update_task",
                ("project_id", "task_id"),
                variadic=True,
                impact=ImpactLevel.CRITICAL,
            ),
        }
    )
    monkeypatch.setattr(reasoning, "SPECS", widened)
    return widened


# --- the allowlist stays routine, and stays explicit ---------------------


def test_every_existing_operation_is_routine():
    assert {spec.impact for spec in SPECS.values()} == {ImpactLevel.ROUTINE}


def test_the_allowlist_is_unchanged_by_the_impact_field():
    # Impact classifies; it does not widen or narrow what may be called.
    assert set(SPECS) == {
        "inspect_project",
        "create_milestone",
        "update_milestone",
        "create_task",
        "update_task",
        "run_task",
    }


def test_an_operation_without_a_declared_impact_is_gated_by_default():
    # A new capability added without a decision about its impact must not
    # become autonomous by being overlooked.
    spec = OperationSpec("update_task", ("project_id", "task_id"), variadic=True)

    assert spec.impact is ImpactLevel.CRITICAL


# --- requires_approval classifies the operation, nothing else -----------


def test_routine_operations_need_no_approval():
    # Structurally valid argument sets for each allowlisted operation.
    valid = {
        "inspect_project": {"project_id": "alpha"},
        "create_milestone": {"project_id": "alpha", "milestone_id": "m1", "name": "M"},
        "update_milestone": {"project_id": "alpha", "milestone_id": "m1", "status": "done"},
        "create_task": {"project_id": "alpha", "task_id": "t1", "milestone": "alpha",
                        "title": "T"},
        "update_task": {"project_id": "alpha", "task_id": "t1", "status": "in_progress"},
        "run_task": {"project_id": "alpha", "task_id": "t1"},
    }

    assert set(valid) == set(SPECS)
    for name, arguments in valid.items():
        assert requires_approval(Operation.propose(
            dict(operation=name, **arguments)
        )) is False


def test_elevated_and_critical_operations_are_gated(interface, gated_specs):
    for name in ("elevated_change", "critical_change"):
        operation = interface.propose(
            dict(operation=name, project_id="alpha", task_id="t1", title="X")
        )
        assert requires_approval(operation) is True


def test_an_operation_absent_from_the_allowlist_is_gated():
    # Built directly rather than proposed, because propose refuses unknown
    # operations earlier. Deny by default, not deny by omission.
    unknown = Operation(operation="delete_everything", arguments={})

    assert requires_approval(unknown) is True


def test_requires_approval_rejects_a_non_operation():
    with pytest.raises(TypeError):
        requires_approval({"operation": "update_task"})


def test_the_project_being_targeted_does_not_change_the_verdict(tmp_path):
    write_project(tmp_path, "alpha")
    write_project(tmp_path, "master-system")
    for project_id in ("alpha", "master-system"):
        operation = Operation.propose(
            dict(operation="create_task", project_id=project_id, task_id="t1",
                 milestone="alpha", title="T")
        )
        assert requires_approval(operation) is False


# --- routine operations execute without a human -------------------------


def test_a_routine_operation_executes_with_no_manual_approval(interface):
    operation = interface.propose(
        dict(operation="create_task", project_id="alpha", task_id="t1",
             milestone="alpha", title="First")
    )
    assert operation.state is ApprovalState.PROPOSED

    result = interface.execute(operation)

    assert result.status is ResultStatus.SUCCESS
    assert interface.master.status("alpha")["tasks"][0]["id"] == "t1"


def test_the_whole_lifecycle_runs_without_a_human(interface):
    def run(operation_name, **arguments):
        return interface.execute(
            interface.propose(dict(operation=operation_name, **arguments))
        )

    assert run(
        "create_milestone", project_id="alpha", milestone_id="m1", name="M1"
    ).status is ResultStatus.SUCCESS
    assert run(
        "create_task", project_id="alpha", task_id="t1", milestone="alpha", title="T"
    ).status is ResultStatus.SUCCESS
    assert run(
        "update_task", project_id="alpha", task_id="t1", status="in_progress"
    ).status is ResultStatus.SUCCESS
    assert run(
        "update_task", project_id="alpha", task_id="t1", status="completed"
    ).status is ResultStatus.SUCCESS

    assert interface.master.status("alpha")["tasks"][0]["status"] == "completed"


# --- gated operations need a human --------------------------------------


@pytest.mark.parametrize("name", ["elevated_change", "critical_change"])
def test_a_gated_operation_does_not_execute_unapproved(interface, gated_specs, name):
    operation = interface.propose(
        dict(operation=name, project_id="alpha", task_id="t1", title="X")
    )

    result = interface.execute(operation)

    assert result.status is ResultStatus.REJECTED
    assert result.reason is RequestError.NOT_APPROVED
    assert interface.master.status("alpha")["tasks"] == []


def test_an_unclassifiable_operation_does_not_execute(interface):
    unknown = Operation(operation="delete_everything", arguments={})

    result = interface.execute(unknown)

    assert result.status is ResultStatus.REJECTED
    assert result.reason is RequestError.NOT_APPROVED


def test_policy_does_not_overrule_an_explicit_rejection(interface):
    # A ROUTINE operation, refused by a human, stays refused.
    operation = interface.propose(
        dict(operation="create_task", project_id="alpha", task_id="t1",
             milestone="alpha", title="T")
    ).reject()

    result = interface.execute(operation)

    assert result.status is ResultStatus.REJECTED
    assert result.reason is RequestError.EXPLICITLY_REJECTED
    assert interface.master.status("alpha")["tasks"] == []


# --- a model cannot talk its way past the policy ------------------------


def test_a_model_emitting_act_does_not_bypass_approval(interface, gated_specs):
    operation = interface.propose(
        dict(operation="critical_change", project_id="alpha", task_id="t1", title="X")
    )
    # The model asserts authority by choosing ACT.
    decision = MasterDecision(Decision.ACT, reason="I am confident.", operation=operation)

    result = interface.execute(decision.operation)

    assert decision.decision is Decision.ACT
    assert decision.pending_approval is True
    assert result.status is ResultStatus.REJECTED
    assert interface.master.status("alpha")["tasks"] == []


def test_a_model_emitting_act_on_a_routine_operation_is_not_pending(interface):
    operation = interface.propose(
        dict(operation="create_task", project_id="alpha", task_id="t1",
             milestone="alpha", title="T")
    )
    decision = MasterDecision(Decision.ACT, reason="Proceed.", operation=operation)

    assert decision.pending_approval is False
    assert interface.execute(decision.operation).status is ResultStatus.SUCCESS


def test_request_approval_on_a_routine_operation_stays_conservative(interface):
    operation = interface.propose(
        dict(operation="create_task", project_id="alpha", task_id="t1",
             milestone="alpha", title="T")
    )
    decision = MasterDecision(
        Decision.REQUEST_APPROVAL, reason="Please confirm.", operation=operation
    )

    # Unapproved, so policy still supplies approval, but the caller is free to
    # hold it back; nothing about a conservative model breaks.
    assert decision.pending_approval is False


def test_a_decision_without_an_operation_is_never_pending():
    assert MasterDecision(Decision.WAIT, reason="Not yet.").pending_approval is False
    assert MasterDecision(Decision.BLOCKED, reason="Blocked.").pending_approval is False


# --- both routes converge on one path ------------------------------------


def test_policy_and_human_approval_converge_on_the_same_path(interface):
    for task_id in ("t1", "t2"):
        interface.execute(interface.propose(dict(
            operation="create_task", project_id="alpha", task_id=task_id,
            milestone="alpha", title="T",
        )))

    policy_run = interface.execute(
        interface.propose(
            dict(operation="update_task", project_id="alpha", task_id="t1",
                 title="From policy")
        )
    )
    human_run = interface.execute(
        interface.propose(
            dict(operation="update_task", project_id="alpha", task_id="t2",
                 title="From a human")
        ).approve()
    )

    assert policy_run.status is human_run.status is ResultStatus.SUCCESS
    assert policy_run.operation == human_run.operation


def test_the_master_method_called_is_the_same_either_way(tmp_path):
    calls = []

    class RecordingMaster(Master):
        def create_task(self, *arguments, **keywords):
            calls.append(keywords)
            return super().create_task(*arguments, **keywords)

    write_project(tmp_path)
    master = RecordingMaster(tmp_path)
    interface = ReasoningInterface(master)

    for approval in (False, True):
        operation = interface.propose(
            dict(operation="create_task", project_id="alpha",
                 task_id=f"t{int(approval)}", milestone="alpha", title="T")
        )
        if approval:
            operation = operation.approve()
        interface.execute(operation)

    assert calls == [
        {"project_id": "alpha", "task_id": "t0", "milestone": "alpha", "title": "T"},
        {"project_id": "alpha", "task_id": "t1", "milestone": "alpha", "title": "T"},
    ]