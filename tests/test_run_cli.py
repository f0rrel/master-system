"""core.run_cli: the composition root for real runs."""

import io

import pytest
import yaml

from core import run_cli
from core.evidence import spec_hash
from core.history import EventType
from core.paths import RuntimePaths
from core.project_state import ProjectState
from core.run_lock import ProjectLock
from core.sqlite_history import SQLiteHistoryStore


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
        {"id": "t1", "milestone": "m1", "title": "T1", "status": "planned"}]}))
    state = tmp_path / "state"
    return {"root": root, "project": project, "state": state,
            "history": lambda: SQLiteHistoryStore(state / "history.sqlite")}


def cli(setup, *argv):
    out = io.StringIO()
    code = run_cli.main(["--config", str(setup["root"] / "no-config.toml"),
                         "--root", str(setup["root"]), "--state-dir", str(setup["state"]),
                         *argv], out=out)
    return code, out.getvalue()


def human_actions(setup):
    return [e for e in setup["history"]().events(types=[EventType.HUMAN_ACTION])]


def test_describing_a_task_is_written_and_recorded(setup):
    before = spec_hash(ProjectState(setup["project"]).get_task("t1"))

    code, out = cli(setup, "task", "describe", "alpha", "t1", "--text", "Add greet().")

    task = ProjectState(setup["project"]).get_task("t1")
    assert code == 0 and "recorded" in out
    assert task["description"] == "Add greet()."
    [event] = human_actions(setup)
    assert event.task_id == "t1"
    assert event.payload == {"actor": "human-cli", "action": "set_description",
                             "spec_hash_before": before, "spec_hash_after": spec_hash(task)}


def test_setting_and_clearing_acceptance_are_recorded(setup):
    assert cli(setup, "task", "set-acceptance", "alpha", "t1", "--command", "npm test",
               "--protect", "tests/*")[0] == 0
    assert ProjectState(setup["project"]).get_task("t1")["acceptance"] == {
        "commands": ["npm test"], "protected_paths": ["tests/*"]}
    assert cli(setup, "task", "set-acceptance", "alpha", "t1", "--clear")[0] == 0

    assert [e.payload["action"] for e in human_actions(setup)] == ["set_acceptance",
                                                                   "clear_acceptance"]
    assert "acceptance" not in ProjectState(setup["project"]).get_task("t1")


def test_an_edit_is_refused_while_a_run_holds_the_project(setup):
    with ProjectLock(setup["project"], holder="session=s1"):
        code, _ = cli(setup, "task", "describe", "alpha", "t1", "--text", "x")

    assert code == 1
    assert "description" not in ProjectState(setup["project"]).get_task("t1")
    assert human_actions(setup) == []


def test_an_invalid_edit_is_refused_and_not_recorded(setup):
    code, _ = cli(setup, "task", "describe", "alpha", "nope", "--text", "x")

    assert code == 1
    assert human_actions(setup) == []


@pytest.mark.parametrize("argv", [
    ["task", "describe", "alpha", "t1"],
    ["task", "describe", "alpha", "t1", "--text", "x", "--clear"],
    ["task", "set-acceptance", "alpha", "t1"],
    ["task", "set-acceptance", "alpha", "t1", "--clear", "--command", "x"],
])
def test_bad_usage_is_refused(setup, argv):
    assert cli(setup, *argv)[0] == 2
    assert human_actions(setup) == []


def test_state_defaults_to_the_xdg_state_dir(setup, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    out = io.StringIO()

    run_cli.main(["--root", str(setup["root"]), "task", "describe", "alpha", "t1",
                  "--text", "x"], out=out)

    history = SQLiteHistoryStore(RuntimePaths.default().history_path)
    assert len(history.events(types=[EventType.HUMAN_ACTION])) == 1


# --- start / resume / status / --until-stopped --------------------------------------

import json

from conftest import attach_repository
from core.execution import ExecutionResult
from core.provider import ReasoningProvider
from core.session_store import FileSessionStore
from core.verification import VerificationResult
from core.work_session import SessionStatus


def act(operation):
    return json.dumps({"decision": "act", "reason": "go", "operation": operation})


START = act({"operation": "update_task", "project_id": "alpha", "task_id": "t1",
             "status": "in_progress"})
RUN = act({"operation": "run_task", "project_id": "alpha", "task_id": "t1"})
DONE = act({"operation": "update_task", "project_id": "alpha", "task_id": "t1",
            "status": "completed"})
WAIT = json.dumps({"decision": "wait", "reason": "enough", "operation": None})


class Scripted(ReasoningProvider):
    name = "scripted:test"

    def __init__(self, replies):
        self.replies = list(replies)

    def complete(self, prompt, schema=None):
        self.last_usage = {"input_tokens": 100, "output_tokens": 10}
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


class Writes:
    def execute(self, task, context, *, workspace):
        (workspace.path / "done.txt").write_text("done\n")
        return ExecutionResult(status="success", usage={"input_tokens": 5000})


class Passes:
    def verify(self, task, context, evidence=None, *, workspace):
        return VerificationResult(verdict="pass", summary="ok")


@pytest.fixture
def wired(setup, monkeypatch):
    attach_repository(setup["project"])

    def install(replies):
        provider = Scripted(replies)
        monkeypatch.setattr(run_cli, "build_provider", lambda config: provider)
        monkeypatch.setattr(run_cli, "build_worker", lambda config: Writes())
        monkeypatch.setattr(run_cli, "build_verifier", lambda config, *rest: Passes())
        monkeypatch.setattr(run_cli, "build_worker_env",
                            lambda config: {"PATH": "/usr/bin:/bin", "HOME": "/tmp"})
        return provider

    return install


def config_file(setup, text):
    path = setup["state"].parent / "config.toml"
    path.write_text(text)
    return path


def cli_with(setup, config, *argv):
    out = io.StringIO()
    code = run_cli.main(["--config", str(config), "--root", str(setup["root"]),
                         "--state-dir", str(setup["state"]), *argv], out=out)
    return code, out.getvalue()


def sessions(setup):
    return FileSessionStore(setup["state"] / "sessions")


def test_start_runs_a_task_end_to_end(setup, wired):
    wired([START, RUN, DONE])

    code, out = cli(setup, "start", "alpha", "--objective", "Finish t1", "--session", "s1")

    assert code == 0, out
    session = sessions(setup).load("s1")
    assert session.last_stop_reason == "no_actionable_work"
    assert ProjectState(setup["project"]).get_task("t1")["status"] == "completed"
    assert "session s1: stopped" in out


def test_until_stopped_resumes_after_each_step_limit(setup, wired):
    wired([START, RUN, WAIT])
    config = config_file(setup, "[run]\nmax_steps = 1\n")

    code, out = cli_with(setup, config, "start", "alpha", "--objective", "o",
                         "--session", "s1", "--until-stopped")

    assert code == 0
    assert out.count("session s1:") == 3
    assert sessions(setup).load("s1").last_stop_reason == "master_stop"


def test_until_stopped_respects_max_runs(setup, wired):
    wired([START, RUN, WAIT])
    config = config_file(setup, "[run]\nmax_steps = 1\n")

    cli_with(setup, config, "start", "alpha", "--objective", "o", "--session", "s1",
             "--until-stopped", "--max-runs", "2")

    assert sessions(setup).load("s1").last_stop_reason == "step_limit"


def test_without_until_stopped_one_run_only(setup, wired):
    wired([START, RUN, WAIT])
    config = config_file(setup, "[run]\nmax_steps = 1\n")

    _, out = cli_with(setup, config, "start", "alpha", "--objective", "o", "--session", "s1")

    assert out.count("session s1:") == 1


def test_resume_runs_again(setup, wired):
    wired([START, WAIT, RUN, WAIT])
    cli(setup, "start", "alpha", "--objective", "o", "--session", "s1")

    code, out = cli(setup, "resume", "s1")

    assert code == 0
    assert sessions(setup).load("s1").steps_completed == 4


def test_ctrl_c_is_recorded_and_the_session_stopped(setup, wired):
    wired([START, KeyboardInterrupt()])

    code, _ = cli(setup, "start", "alpha", "--objective", "o", "--session", "s1")

    assert code == 130
    session = sessions(setup).load("s1")
    assert session.status is SessionStatus.STOPPED and session.last_stop_reason == "error"
    assert setup["history"]().events(types=[EventType.RUN_ERROR])


def test_status_shows_tasks_sessions_and_events(setup, wired):
    wired([START, RUN, WAIT])
    cli(setup, "start", "alpha", "--objective", "o", "--session", "s1")

    code, out = cli(setup, "status", "alpha")

    assert code == 0
    assert "PROJECT alpha  (idle)" in out
    assert "task t1" in out and "in_progress" in out
    assert "session s1" in out
    assert "verification" in out


def test_status_shows_who_holds_the_project(setup, wired):
    with ProjectLock(setup["project"], holder="session=s9"):
        _, out = cli(setup, "status", "alpha")

    assert "running:" in out and "session=s9" in out


def test_a_missing_master_key_is_a_setup_error(setup, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    code, _ = cli(setup, "start", "alpha", "--objective", "o")

    assert code == 2
    assert sessions(setup).list_sessions() == ()


def test_a_missing_worker_binary_is_a_setup_error(setup, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    config = config_file(setup, '[worker]\nopencode_bin = "/nonexistent/opencode"\n')

    code, _ = cli_with(setup, config, "start", "alpha", "--objective", "o")

    assert code == 2


def test_no_suitable_node_is_a_setup_error(setup, monkeypatch, tmp_path):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    fake_bin = tmp_path / "opencode"
    fake_bin.write_text("#!/bin/sh\n")
    config = config_file(setup, f'[worker]\nopencode_bin = "{fake_bin}"\nnode_min_major = 99\n')

    code, _ = cli_with(setup, config, "start", "alpha", "--objective", "o")

    assert code == 2


def test_the_real_composition_wires_the_configured_parts(setup, monkeypatch, tmp_path):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    fake_bin = tmp_path / "opencode"
    fake_bin.write_text("#!/bin/sh\n")
    config = run_cli.load_config(config_file(
        setup, f'[worker]\nopencode_bin = "{fake_bin}"\nmodel = "p/m"\nhome = "{tmp_path / "wh"}"\n'))

    provider = run_cli.build_provider(config)
    worker = run_cli.build_worker(config)

    assert provider.name == "deepseek:deepseek-v4-flash"
    assert worker.command(tmp_path, "P")[worker.command(tmp_path, "P").index("--model") + 1] == "p/m"
    from core.worker_env import find_node_bin
    if find_node_bin(22) is not None:
        env = run_cli.build_worker_env(config)
        assert env["HOME"] == str(tmp_path / "wh") and "DEEPSEEK_API_KEY" not in env


# --- report: from history alone ------------------------------------------------------

import shutil


def test_the_report_is_built_from_history_alone(setup, wired):
    wired([START, RUN, DONE])
    cli(setup, "task", "describe", "alpha", "t1", "--text", "Write done.txt")
    cli(setup, "start", "alpha", "--objective", "o", "--session", "s1")
    config = config_file(setup, '[prices]\n"test" = { input = 1.0, output = 2.0 }\n')
    shutil.rmtree(setup["root"])  # no project files at all

    code, out = cli_with(setup, config, "report", "s1", "--json")

    assert code == 0, out
    report = json.loads(out)
    assert report["steps"] == 3 and len(report["runs"]) == 1
    assert report["runs"][0]["stop_reason"] == "no_actionable_work"
    [attempt] = report["attempts"]
    assert (attempt["task_id"], attempt["outcome"], attempt["verdict"]) == ("t1", "finished", "pass")
    assert report["wall_clock_seconds"] is not None
    [master] = report["master_usage"]
    assert master["reasoner"] == "scripted:test"
    assert master["usage"]["input_tokens"] == 300 and master["usage"]["output_tokens"] == 30
    assert master["cost_usd"] == round((300 * 1.0 + 30 * 2.0) / 1e6, 6)
    assert report["worker_usage"][0]["usage"]["input_tokens"] == 5000
    assert report["worker_usage"][0]["claimed"] is True
    assert [h["action"] for h in report["human_touches"]] == ["set_description"]
    assert report["unexplained_spec_changes"] == []
    [trace] = report["completions"]
    assert trace["attempt_id"] == attempt["attempt_id"]
    assert trace["result_sha"] == attempt["result_sha"] and trace["verdict"] == "pass"
    assert trace["integrated_sha"] is None


def test_the_text_report_reads_well(setup, wired):
    wired([START, RUN, DONE])
    cli(setup, "start", "alpha", "--objective", "o", "--session", "s1")

    code, out = cli(setup, "report", "s1")

    assert code == 0
    for heading in ("REPORT", "Runs: 1", "Attempts: 1", "Tokens and cost", "Human touches",
                    "Completions: 1", "(claimed)"):
        assert heading in out


def test_an_edit_outside_run_cli_is_reported_as_unexplained(setup, wired):
    wired([START, RUN, WAIT, RUN, WAIT])
    cli(setup, "start", "alpha", "--objective", "o", "--session", "s1")
    from core.master import Master
    Master(setup["root"]).update_task("alpha", "t1", title="Renamed behind its back")
    cli(setup, "resume", "s1")

    _, out = cli(setup, "report", "s1", "--json")

    [change] = json.loads(out)["unexplained_spec_changes"]
    assert change["task_id"] == "t1"


def test_an_unknown_session_has_no_report(setup):
    code, _ = cli(setup, "report", "nope")

    assert code == 1


def test_a_retitle_by_masters_own_operation_is_not_unexplained(setup, wired):
    retitle = act({"operation": "update_task", "project_id": "alpha", "task_id": "t1",
                   "title": "A clearer title"})
    wired([START, RUN, retitle, RUN, WAIT])
    cli(setup, "start", "alpha", "--objective", "o", "--session", "s1")

    _, out = cli(setup, "report", "s1", "--json")

    assert json.loads(out)["unexplained_spec_changes"] == []


# --- the Master spending cap ------------------------------------------------------------


def test_max_cost_stops_before_the_next_paid_call(setup, wired):
    provider = wired([START, RUN, WAIT])
    # Each decision: 100 input + 10 output tokens at $1000/M and $1000/M = $0.11.
    config = config_file(setup, '[master]\nmodel = "test"\n'
                                '[prices]\n"test" = { input = 1000.0, output = 1000.0 }\n')

    code, out = cli_with(setup, config, "start", "alpha", "--objective", "o",
                         "--session", "s1", "--max-cost-usd", "0.20")

    assert code == 0
    session = sessions(setup).load("s1")
    assert session.last_stop_reason == "budget_exhausted"
    assert session.steps_completed == 2          # the third call was never made
    assert provider.replies == [WAIT]
    stopped = setup["history"]().events(types=[EventType.RUN_STOPPED])[-1].payload
    assert "reached the cap" in stopped["detail"]


def test_max_cost_needs_a_price_for_the_master_model(setup, wired):
    wired([START])
    config = config_file(setup, '[master]\nmodel = "no-price-model"\n')

    code, _ = cli_with(setup, config, "start", "alpha", "--objective", "o",
                       "--max-cost-usd", "0.20")

    assert code == 2
    assert sessions(setup).list_sessions() == ()


def test_without_a_cap_nothing_is_checked(setup, wired):
    wired([START, RUN, WAIT])
    config = config_file(setup, '[master]\nmodel = "no-price-model"\n')

    code, _ = cli_with(setup, config, "start", "alpha", "--objective", "o", "--session", "s1")

    assert code == 0
    assert sessions(setup).load("s1").last_stop_reason == "master_stop"


# --- manual_check: how to check by hand, shown with each completion -------------------


def test_manual_check_is_human_only_recorded_and_shown_in_the_report(setup, wired):
    wired([START, RUN, DONE])
    before = spec_hash(ProjectState(setup["project"]).get_task("t1"))
    assert cli(setup, "task", "manual-check", "alpha", "t1", "--text",
               "Open the game.\nPlay one move and check the score.")[0] == 0
    assert spec_hash(ProjectState(setup["project"]).get_task("t1")) == before
    cli(setup, "start", "alpha", "--objective", "o", "--session", "s1")

    report = json.loads(cli(setup, "report", "s1", "--json")[1])
    text = cli(setup, "report", "s1")[1]

    assert report["completions"][0]["manual_check"].startswith("Open the game.")
    assert "how to check by hand:" in text and "Play one move" in text
    assert [h["action"] for h in report["human_touches"]] == ["set_manual_check"]


def test_models_cannot_set_manual_check(setup):
    from core.master import Master
    from core.reasoning import Operation, ReasoningInterface, ResultStatus

    outcome = ReasoningInterface(Master(setup["root"])).execute(Operation.propose(
        {"operation": "update_task", "project_id": "alpha", "task_id": "t1",
         "manual_check": "anything"}))

    assert outcome.status is ResultStatus.DOMAIN_ERROR


def test_a_malformed_manual_check_is_refused(setup):
    assert cli(setup, "task", "manual-check", "alpha", "t1", "--text", "   ")[0] == 1
    assert human_actions(setup) == []
