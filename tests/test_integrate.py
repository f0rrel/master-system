"""INTEGRATE: models can only request it; humans integrate by fast-forward."""

import json

import pytest
import yaml

from conftest import attach_repository, git
from core import attempts
from core.attempts import IntegrationRefused, integrate
from core.autonomous_loop import STOP_APPROVAL, AutonomousLoop
from core.execution import ExecutionResult
from core.history import EventType, InMemoryHistoryStore
from core.master import Master
from core.paths import RuntimePaths
from core.provider import ReasoningProvider
from core.reasoning import Operation, ReasoningInterface, RequestError, ResultStatus
from core.run_lock import ProjectBusyError, ProjectLock
from core.sqlite_history import SQLiteHistoryStore
from core.task_orchestrator import TaskOrchestrator
from core.verification import VerificationResult


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "projects"
    project = root / "alpha-project"
    project.mkdir(parents=True)
    (project / "project.yaml").write_text(
        yaml.safe_dump({"id": "alpha", "name": "alpha", "status": "active"}))
    (project / "milestones.yaml").write_text(yaml.safe_dump(
        {"milestones": [{"id": "m1", "name": "M1", "status": "in_progress"}]}))
    (project / "tasks.yaml").write_text(yaml.safe_dump({"tasks": [
        {"id": "t1", "milestone": "m1", "title": "T1", "status": "in_progress"}]}))
    repo = attach_repository(project)
    paths = RuntimePaths.default()
    history = SQLiteHistoryStore(paths.history_path)
    return {"root": root, "project": project, "repo": repo, "paths": paths,
            "history": history, "master": Master(root)}


class Writes:
    def execute(self, task, context, *, workspace):
        (workspace.path / "feature.txt").write_text("new feature\n")
        return ExecutionResult(status="success")


class Verdict:
    def __init__(self, verdict="pass"):
        self.verdict = verdict

    def verify(self, task, context, evidence=None, *, workspace):
        return VerificationResult(verdict=self.verdict, summary="checked")


def make_attempt(setup, verdict="pass", worker=None):
    result = TaskOrchestrator(setup["master"], worker or Writes(), Verdict(verdict),
                              history=setup["history"]).orchestrate(
        "alpha", "t1", run_id="r1")
    return result["attempt_id"], result["result_sha"]


def integration_events(setup):
    return setup["history"].events(types=[EventType.INTEGRATION])


# --- the operation ------------------------------------------------------------


class Scripted(ReasoningProvider):
    name = "scripted"

    def __init__(self, replies):
        self._replies = list(replies)

    def complete(self, prompt, schema=None):
        return self._replies.pop(0)


def test_master_can_only_request_integration(setup):
    request = json.dumps({"decision": "act", "reason": "ship it", "operation": {
        "operation": "integrate_attempt", "project_id": "alpha", "task_id": "t1",
        "attempt_id": "a" * 32}})
    loop = AutonomousLoop(setup["master"], Scripted([request]), Writes(), Verdict(),
                          history=InMemoryHistoryStore())

    result = loop.run("alpha")

    assert result.stop_reason == STOP_APPROVAL
    assert result.approval_reason == "policy"
    assert git(setup["repo"], "log", "--oneline").count("\n") == 0


def test_even_an_approved_integration_is_never_executed_by_the_interface(setup):
    operation = Operation.propose({"operation": "integrate_attempt", "project_id": "alpha",
                                   "task_id": "t1", "attempt_id": "a" * 32}).approve()

    outcome = ReasoningInterface(setup["master"]).execute(operation)

    assert outcome.status is ResultStatus.REJECTED
    assert outcome.reason is RequestError.HUMAN_ONLY


# --- the human command ----------------------------------------------------------


def test_a_verified_attempt_is_fast_forwarded_into_the_checked_out_base(setup):
    attempt_id, result_sha = make_attempt(setup)
    base = git(setup["repo"], "rev-parse", "main")

    result = integrate(setup["master"], setup["history"], "alpha", attempt_id)

    assert result.method == "ff_merge"
    assert git(setup["repo"], "rev-parse", "main") == result_sha
    # The owner's checkout of main is consistent with the new tip.
    assert (setup["repo"] / "feature.txt").read_text() == "new feature\n"
    assert git(setup["repo"], "status", "--porcelain") == ""
    event = integration_events(setup)[0]
    assert event.attempt_id == attempt_id and event.task_id == "t1"
    assert event.payload["previous_sha"] == base
    assert event.payload["result_sha"] == result_sha
    assert event.payload["actor"] == "human-cli"
    # Integration never touches task status.
    assert setup["master"].status("alpha")["tasks"][0]["status"] == "in_progress"


def test_integrating_twice_is_a_no_op(setup):
    attempt_id, _ = make_attempt(setup)
    integrate(setup["master"], setup["history"], "alpha", attempt_id)

    again = integrate(setup["master"], setup["history"], "alpha", attempt_id)

    assert again.method == "already_integrated"
    assert len(integration_events(setup)) == 1


def test_a_base_that_is_not_checked_out_is_moved_by_ref(setup):
    attempt_id, result_sha = make_attempt(setup)
    git(setup["repo"], "checkout", "-q", "-b", "elsewhere")

    result = integrate(setup["master"], setup["history"], "alpha", attempt_id)

    assert result.method == "update_ref"
    assert git(setup["repo"], "rev-parse", "main") == result_sha


def test_a_dirty_checkout_of_the_base_is_refused(setup):
    attempt_id, _ = make_attempt(setup)
    (setup["repo"] / "app.py").write_text("uncommitted\n")

    with pytest.raises(IntegrationRefused) as refused:
        integrate(setup["master"], setup["history"], "alpha", attempt_id)

    assert refused.value.reason == "git_refused"
    assert integration_events(setup) == ()


def test_a_base_that_moved_on_is_refused(setup):
    attempt_id, _ = make_attempt(setup)
    (setup["repo"] / "other.txt").write_text("someone else\n")
    git(setup["repo"], "add", "other.txt")
    git(setup["repo"], "commit", "-q", "-m", "moved on")

    with pytest.raises(IntegrationRefused) as refused:
        integrate(setup["master"], setup["history"], "alpha", attempt_id)

    assert refused.value.reason == "base_moved"


@pytest.mark.parametrize("verdict", ["fail", "needs_human", "unable_to_verify"])
def test_an_unverified_attempt_is_refused(setup, verdict):
    attempt_id, _ = make_attempt(setup, verdict=verdict)

    with pytest.raises(IntegrationRefused) as refused:
        integrate(setup["master"], setup["history"], "alpha", attempt_id)

    assert refused.value.reason == "not_verified"


def test_an_attempt_for_a_changed_spec_is_refused(setup):
    attempt_id, _ = make_attempt(setup)
    setup["master"].update_task("alpha", "t1", title="Different now")

    with pytest.raises(IntegrationRefused) as refused:
        integrate(setup["master"], setup["history"], "alpha", attempt_id)

    assert refused.value.reason == "spec_changed"


def test_an_attempt_that_did_not_finish_is_refused(setup):
    class Raises:
        def execute(self, task, context, *, workspace):
            raise RuntimeError("worker died")

    with pytest.raises(RuntimeError):
        make_attempt(setup, worker=Raises())
    attempt_id = setup["history"].events(types=[EventType.ATTEMPT_STARTED])[-1].attempt_id

    with pytest.raises(IntegrationRefused) as refused:
        integrate(setup["master"], setup["history"], "alpha", attempt_id)

    assert refused.value.reason == "not_finished"


def test_an_unknown_attempt_is_refused(setup):
    with pytest.raises(IntegrationRefused) as refused:
        integrate(setup["master"], setup["history"], "alpha", "f" * 32)

    assert refused.value.reason == "unknown_attempt"


def test_integration_waits_for_the_project_lock(setup):
    attempt_id, _ = make_attempt(setup)

    with ProjectLock(setup["project"], holder="a run"):
        with pytest.raises(ProjectBusyError):
            integrate(setup["master"], setup["history"], "alpha", attempt_id)

    assert integration_events(setup) == ()


def test_the_cli_integrates_and_reports(setup, capsys):
    attempt_id, result_sha = make_attempt(setup)

    code = attempts.main(["--root", str(setup["root"]), "integrate", "alpha", attempt_id])

    assert code == 0
    assert result_sha[:12] in capsys.readouterr().out
    assert attempts.main(["--root", str(setup["root"]), "integrate", "alpha",
                          attempt_id]) == 0
    assert "already integrated" in capsys.readouterr().out


def test_the_cli_reports_a_refusal(setup, capsys):
    attempt_id, _ = make_attempt(setup, verdict="fail")

    code = attempts.main(["--root", str(setup["root"]), "integrate", "alpha", attempt_id])

    assert code == 1
    assert "not_verified" in capsys.readouterr().err


# --- --rebase: replay onto a moved base, re-verify, integrate exactly that ---------------


def move_base(setup, name="other.txt", content="someone else\n"):
    (setup["repo"] / name).write_text(content)
    git(setup["repo"], "add", name)
    git(setup["repo"], "commit", "-q", "-m", "moved on")
    return git(setup["repo"], "rev-parse", "main")


def test_a_moved_base_is_rebased_reverified_and_integrated(setup):
    attempt_id, result_sha = make_attempt(setup)
    tip = move_base(setup)

    result = integrate(setup["master"], setup["history"], "alpha", attempt_id,
                       rebase=True, verifier=Verdict("pass"))

    new_sha = git(setup["repo"], "rev-parse", "main")
    assert result.method == "rebased_ff_merge"
    assert result.result_sha == new_sha != result_sha
    assert result.rebased_from == result_sha
    assert git(setup["repo"], "rev-parse", f"{new_sha}^") == tip
    assert (setup["repo"] / "feature.txt").exists() and (setup["repo"] / "other.txt").exists()
    reverify = setup["history"].events(types=[EventType.VERIFICATION])[-1]
    assert reverify.attempt_id == attempt_id
    assert reverify.payload["result_sha"] == new_sha
    assert reverify.payload["base_sha"] == tip
    assert reverify.payload["rebased_from"] == result_sha
    assert reverify.payload["verdict"] == "pass"
    [event] = integration_events(setup)
    assert event.payload["result_sha"] == new_sha
    assert event.payload["rebased_from"] == result_sha
    assert reverify.seq < event.seq


def test_a_rebased_integration_is_idempotent(setup):
    attempt_id, _ = make_attempt(setup)
    move_base(setup)
    integrate(setup["master"], setup["history"], "alpha", attempt_id, rebase=True,
              verifier=Verdict("pass"))

    again = integrate(setup["master"], setup["history"], "alpha", attempt_id, rebase=True,
                      verifier=Verdict("pass"))

    assert again.method == "already_integrated"
    assert len(integration_events(setup)) == 1


def test_a_conflicting_rebase_is_refused_and_changes_nothing(setup):
    attempt_id, _ = make_attempt(setup)
    tip = move_base(setup, "feature.txt", "a different feature\n")

    with pytest.raises(IntegrationRefused) as refused:
        integrate(setup["master"], setup["history"], "alpha", attempt_id, rebase=True,
                  verifier=Verdict("pass"))

    assert refused.value.reason == "rebase_conflict"
    assert git(setup["repo"], "rev-parse", "main") == tip
    assert integration_events(setup) == ()


def test_a_rebased_result_that_fails_reverification_is_refused(setup):
    attempt_id, _ = make_attempt(setup)
    tip = move_base(setup)

    with pytest.raises(IntegrationRefused) as refused:
        integrate(setup["master"], setup["history"], "alpha", attempt_id, rebase=True,
                  verifier=Verdict("fail"))

    assert refused.value.reason == "reverification_failed"
    assert git(setup["repo"], "rev-parse", "main") == tip
    assert integration_events(setup) == ()
    failed = setup["history"].events(types=[EventType.VERIFICATION])[-1].payload
    assert failed["verdict"] == "fail" and failed["rebased_from"]


def test_reverification_runs_the_real_acceptance_on_the_rebased_commit(setup):
    setup["master"].set_task_acceptance("alpha", "t1", {
        "commands": ["test -f feature.txt", "test -f other.txt"]})
    attempt_id, _ = make_attempt(setup)  # the attempt's base has no other.txt yet
    move_base(setup)
    from core.acceptance_verifier import AcceptanceVerifier

    result = integrate(setup["master"], setup["history"], "alpha", attempt_id, rebase=True,
                       verifier=AcceptanceVerifier())

    assert result.method == "rebased_ff_merge"
    reverify = setup["history"].events(types=[EventType.VERIFICATION])[-1].payload
    assert reverify["verdict"] == "pass"
    assert [c["exit_code"] for c in reverify["evidence"]["commands_run"]] == [0, 0]


def test_without_rebase_a_moved_base_still_refuses_with_a_hint(setup):
    attempt_id, _ = make_attempt(setup)
    move_base(setup)

    with pytest.raises(IntegrationRefused, match="--rebase") as refused:
        integrate(setup["master"], setup["history"], "alpha", attempt_id)

    assert refused.value.reason == "base_moved"


# --- short attempt ids ------------------------------------------------------------------


def test_the_12_character_id_the_report_prints_is_accepted(setup):
    attempt_id, result_sha = make_attempt(setup)

    result = integrate(setup["master"], setup["history"], "alpha", attempt_id[:12])

    assert result.attempt_id == attempt_id
    assert git(setup["repo"], "rev-parse", "main") == result_sha
    assert integration_events(setup)[0].attempt_id == attempt_id


def test_a_too_short_prefix_is_refused(setup):
    attempt_id, _ = make_attempt(setup)

    with pytest.raises(IntegrationRefused) as refused:
        integrate(setup["master"], setup["history"], "alpha", attempt_id[:6])

    assert refused.value.reason == "attempt_id_too_short"


def test_an_ambiguous_prefix_is_refused(setup):
    from core.attempts import resolve_attempt_id

    for chosen in ("abcdef0123456789" + "0" * 16, "abcdef0123456789" + "1" * 16):
        setup["history"].append(type=EventType.ATTEMPT_STARTED, run_id="r", project_id="alpha",
                                task_id="t1", event_id=chosen)

    with pytest.raises(IntegrationRefused) as refused:
        resolve_attempt_id(setup["history"], "alpha", "abcdef012345")

    assert refused.value.reason == "ambiguous_attempt"
    assert resolve_attempt_id(setup["history"], "alpha", "abcdef0123456789" + "1") == \
        "abcdef0123456789" + "1" * 16


def test_the_integrated_commit_reads_task_id_and_title(setup):
    attempt_id, _ = make_attempt(setup)
    move_base(setup)

    integrate(setup["master"], setup["history"], "alpha", attempt_id, rebase=True,
              verifier=Verdict("pass"))

    assert git(setup["repo"], "log", "-1", "--format=%s", "main") == "t1: T1"
