"""core.daemon and the `ms` service commands: the hands-off background loop."""

import io
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import yaml

from core import ms
from core.daemon import (Daemon, RunRequest, failed_attempts_since_human, load_env_file,
                         start_of_today_utc, work_for)
from core.history import EventType
from core.master import Master
from core.notify import Notifier
from core.run_config import load_config

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
ACCEPT = {"commands": ["npm test"], "protected_paths": ["tests/*"]}


class FakeHistory:
    def __init__(self):
        self.rows = []

    def add(self, type, task_id="t1", attempt_id=None, payload=None, created_at=None):
        self.rows.append(SimpleNamespace(
            seq=len(self.rows) + 1, type=type, project_id="alpha", task_id=task_id,
            attempt_id=attempt_id, payload=payload or {},
            created_at=created_at or NOW.isoformat()))

    def events(self, project_id=None, task_id=None, types=None, **_):
        return tuple(e for e in self.rows
                     if (project_id is None or e.project_id == project_id)
                     and (task_id is None or e.task_id == task_id)
                     and (types is None or e.type in types))


def write_project(root, tasks):
    project = root / "alpha-project"
    project.mkdir(parents=True, exist_ok=True)
    (project / "project.yaml").write_text(
        yaml.safe_dump({"id": "alpha", "name": "alpha", "status": "active"}))
    (project / "milestones.yaml").write_text(yaml.safe_dump(
        {"milestones": [{"id": "m1", "name": "M1", "status": "in_progress"}]}))
    (project / "tasks.yaml").write_text(yaml.safe_dump({"tasks": tasks}))
    return project


def task(task_id, status="planned", **extra):
    return {"id": task_id, "milestone": "m1", "title": task_id.upper(), "status": status,
            "acceptance": ACCEPT, **extra}


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "projects"
    project = write_project(root, [task("t1"), task("t2", depends_on=["t1"]),
                                   task("t3", status="completed")])
    history = FakeHistory()
    notifier = Notifier(topic=None)
    runs = []

    def run(request):
        runs.append(request)
        return 0

    daemon = Daemon(master=Master(root), history=history,
                    config=load_config(tmp_path / "none.toml"), state_dir=tmp_path / "state",
                    notifier=notifier, run=run, now=lambda: NOW)
    return SimpleNamespace(root=root, project=project, history=history, notifier=notifier,
                           runs=runs, daemon=daemon)


def test_work_is_ready_tasks_with_acceptance(env):
    status = env.daemon.master.status("alpha")
    assert work_for(status, env.history) == (["t1"], [])


def test_a_task_without_acceptance_is_not_work(env):
    write_project(env.root, [{"id": "t1", "milestone": "m1", "title": "T1",
                              "status": "planned"}])
    assert work_for(env.daemon.master.status("alpha"), env.history) == ([], [])


def test_failures_since_the_last_human_action_exhaust_a_task(env):
    for n in range(3):
        env.history.add(EventType.ATTEMPT_FINISHED, attempt_id=f"a{n}")
    assert failed_attempts_since_human(env.history, "alpha", "t1") == 3
    assert work_for(env.daemon.master.status("alpha"), env.history) == ([], ["t1"])

    env.history.add(EventType.HUMAN_ACTION, payload={"action": "set_description"})
    assert work_for(env.daemon.master.status("alpha"), env.history) == (["t1"], [])


def test_a_passed_attempt_is_not_a_failure(env):
    env.history.add(EventType.ATTEMPT_FINISHED, attempt_id="a1")
    env.history.add(EventType.VERIFICATION, attempt_id="a1", payload={"verdict": "pass"})
    assert failed_attempts_since_human(env.history, "alpha", "t1") == 0


def test_a_cycle_runs_one_session_per_project_with_work(env):
    log = env.daemon.cycle()

    [request] = env.runs
    assert request == RunRequest("alpha", "alpha-auto-20261006-120000", 0.2)
    assert "ran alpha-auto-20261006-120000" in log[0]
    assert env.notifier.sent[-1]["title"] == "alpha: run finished"


def test_a_finished_task_is_reported_as_done(env):
    def run(request):
        write_project(env.root, [task("t1", status="completed"), task("t2", depends_on=["t1"]),
                                 task("t3", status="completed")])
        return 0

    env.daemon.run = run
    env.daemon.cycle()
    sent = env.notifier.sent[-1]
    assert sent["title"] == "alpha: batch done"
    assert "Done: t1 T1" in sent["message"]


def test_a_blocked_task_is_reported_with_high_priority(env):
    def run(request):
        write_project(env.root, [task("t1", status="blocked"), task("t2", depends_on=["t1"]),
                                 task("t3", status="completed")])
        return 0

    env.daemon.run = run
    env.daemon.cycle()
    assert "Blocked: t1 T1" in env.notifier.sent[-1]["message"]
    assert env.notifier.sent[-1]["priority"] == "high"


def test_nothing_runs_while_paused(env):
    env.daemon.pause_flag.parent.mkdir(parents=True)
    env.daemon.pause_flag.write_text("x")
    assert env.daemon.cycle() == ["paused"] and env.runs == []


def test_nothing_runs_on_a_busy_project(env):
    env.daemon.is_busy = lambda project_id: True
    assert env.daemon.cycle() == ["alpha: busy"] and env.runs == []


def test_the_daily_cap_stops_work_and_notifies_once(env):
    env.history.add(EventType.DECISION, payload={
        "usage": {"input_tokens": 2_000_000, "output_tokens": 0},
        "reasoner": "deepseek:deepseek-v4-flash"})
    assert env.daemon.spent_today() >= 0.5

    assert "daily cap reached" in env.daemon.cycle()[0]
    assert "daily cap reached" in env.daemon.cycle()[0]
    assert env.runs == []
    assert [s["title"] for s in env.notifier.sent] == ["Daily budget reached"]


def test_spend_before_today_does_not_count(env):
    env.history.add(EventType.DECISION, created_at="2026-10-01T10:00:00+00:00", payload={
        "usage": {"input_tokens": 2_000_000, "output_tokens": 0},
        "reasoner": "deepseek:deepseek-v4-flash"})
    assert env.daemon.spent_today() == 0


def test_the_run_cap_is_what_is_left_today(env):
    env.history.add(EventType.DECISION, payload={
        "usage": {"input_tokens": 1_000_000, "output_tokens": 0},
        "reasoner": "deepseek:deepseek-v4-flash"})
    env.daemon.cycle()
    assert env.runs[0].max_cost_usd == pytest.approx(0.06)


def test_an_exhausted_task_asks_for_the_owner_once(env):
    for n in range(3):
        env.history.add(EventType.ATTEMPT_FINISHED, attempt_id=f"a{n}")
    env.daemon.cycle()
    env.daemon.cycle()
    titles = [s["title"] for s in env.notifier.sent]
    assert titles.count("alpha: t1 needs you") == 1 and env.runs == []


def test_a_failing_after_run_hook_is_reported_not_raised(env):
    def broken(project_id, session_id):
        raise RuntimeError("push refused")

    env.daemon.after_run = [broken]
    env.daemon.cycle()
    assert "after-run step failed: push refused" in env.notifier.sent[-1]["message"]


def test_start_of_today_is_local_midnight_in_utc():
    value = start_of_today_utc(NOW)
    assert value.endswith("+00:00")
    assert datetime.fromisoformat(value) <= NOW


def test_env_file_parsing(tmp_path):
    path = tmp_path / "master.env"
    path.write_text("# comment\nexport A=1\nB='two words'\nC=\"3\"\n\nbroken\n")
    assert load_env_file(path) == {"A": "1", "B": "two words", "C": "3"}
    assert load_env_file(tmp_path / "missing") == {}


# --- notifier ---


def test_notifier_posts_title_and_message():
    seen = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def opener(request, timeout):
        seen.append(request)
        return Response()

    notifier = Notifier("https://ntfy.example/", "topic-1", opener=opener)
    assert notifier.send("Batch done", "Done: ml-6", tags="tada", click="https://x/develop/")
    [request] = seen
    assert request.full_url == "https://ntfy.example/topic-1"
    assert request.data == b"Done: ml-6"
    assert request.get_header("Title") == "Batch done"
    assert request.get_header("Click") == "https://x/develop/"


def test_notifier_never_raises():
    def opener(request, timeout):
        raise OSError("offline")

    assert Notifier("https://ntfy.sh", "t", opener=opener).send("a", "b") is False
    assert Notifier("https://ntfy.sh", None).send("a", "b") is False


# --- ms commands ---


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    return tmp_path


def run_ms(*argv):
    out = io.StringIO()
    code = ms.main(list(argv), out=out)
    return code, out.getvalue()


def test_pause_and_resume(home):
    assert run_ms("pause")[0] == 0
    flag = home / "data" / "master-system" / "paused"
    assert flag.exists()
    assert run_ms("resume")[0] == 0
    assert not flag.exists()


def test_stop_without_a_run_pauses(home):
    code, out = run_ms("stop")
    assert code == 0 and "No run is active" in out
    assert (home / "data" / "master-system" / "paused").exists()


def test_notify_setup_creates_a_private_topic(home):
    code, out = run_ms("notify", "setup")
    path = home / "config" / "master-system" / "ntfy-topic"
    topic = path.read_text().strip()
    assert code == 0 and topic.startswith("ms-") and len(topic) > 20
    assert oct(path.stat().st_mode)[-3:] == "600"
    assert topic in out
    assert run_ms("notify", "setup")[0] == 0 and path.read_text().strip() == topic


def test_notify_test_without_setup_says_what_to_do(home):
    code, out = run_ms("notify", "test")
    assert code == 1 and "ms notify setup" in out


def test_the_service_unit_runs_the_daemon_and_reports_bad_stops(tmp_path):
    unit = ms.service_unit("/repo/.venv/bin/python", tmp_path, "/cfg/overnight.toml")
    assert "ExecStart=/repo/.venv/bin/python -m core.ms --config /cfg/overnight.toml daemon" in unit
    assert "ExecStopPost=/repo/.venv/bin/python -m core.ms --config /cfg/overnight.toml service stopped" in unit
    assert "WantedBy=default.target" in unit and "Restart=on-failure" in unit
    assert "master.env" not in unit
