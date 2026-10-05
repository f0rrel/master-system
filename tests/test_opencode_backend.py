import sys
from pathlib import Path
import tempfile

sys.path.insert(0, ".")

from core.execution import ExecutionBackend, ExecutionResult
from core.master import Master
from core.execution_runner import TaskExecutionRunner
from core.opencode_backend import OpenCodeCliBackend
from conftest import workspace_for


def make_project(root, tasks):
    proj = Path(root) / "projects" / "p"
    proj.mkdir(parents=True)
    (proj / "project.yaml").write_text("id: p\nname: P\nstatus: active\n")
    (proj / "milestones.yaml").write_text(
        "milestones:\n  - id: m1\n    name: M1\n    status: in_progress\n"
    )
    lines = ["tasks:"]
    for t in tasks:
        lines.append(f"  - id: {t['id']}")
        lines.append(f"    milestone: {t.get('milestone','m1')}")
        lines.append(f"    title: {t.get('title', t['id'])}")
        lines.append(f"    status: {t['status']}")
        lines.append(f"    assigned_to: {t.get('assigned_to','master')}")
        if t.get("depends_on"):
            lines.append("    depends_on:")
            for d in t["depends_on"]:
                lines.append(f"      - {d}")
    (proj / "tasks.yaml").write_text("\n".join(lines) + "\n")
    return Path(root) / "projects"


def fake_opencode(tmp_path, body="echo done"):
    """An executable standing in for the opencode CLI."""
    script = tmp_path / "fake-opencode"
    script.write_text("#!/bin/sh\n" + body + "\n")
    script.chmod(0o755)
    return script


def run_through_runner(tmp_path, body="echo done", seconds=60):
    root = make_project(tmp_path, [{"id": "t1", "status": "in_progress", "title": "Do X"}])
    m = Master(root)
    work = tmp_path / "ws"
    work.mkdir()
    backend = OpenCodeCliBackend(opencode_bin=fake_opencode(tmp_path, body))
    result = TaskExecutionRunner(m, backend).execute(
        "p", "t1", workspace=workspace_for(work, seconds=seconds, grace=1)
    )
    return m, work, result


def test_backend_implements_interface(tmp_path):
    b = OpenCodeCliBackend()
    assert isinstance(b, ExecutionBackend)


def test_valid_task_reaches_opencode(tmp_path):
    _, work, res = run_through_runner(tmp_path, 'echo "$@" > args')
    assert res.status == "success"
    assert "Title: Do X" in (work / "args").read_text()


def test_opencode_runs_in_the_given_workspace(tmp_path):
    _, work, res = run_through_runner(tmp_path, "pwd > where")
    assert (work / "where").read_text().strip() == str(work.resolve())


def test_success_maps_to_success(tmp_path):
    assert run_through_runner(tmp_path, "exit 0")[2].status == "success"


def test_failure_maps_to_failed(tmp_path):
    assert run_through_runner(tmp_path, "exit 5")[2].status == "failed"


def test_timeout_maps_to_failed(tmp_path):
    res = run_through_runner(tmp_path, "sleep 60", seconds=1)[2]
    assert res.status == "failed"
    assert res.artifacts["timeout"] is True


def test_backend_does_not_mutate_state(tmp_path):
    m, _, _ = run_through_runner(tmp_path, "echo 'status: completed' > tasks.yaml")
    assert m.status("p")["tasks"][0]["status"] == "in_progress"


def test_state_updates_never_applied(tmp_path):
    m, _, res = run_through_runner(tmp_path)
    assert isinstance(res.state_updates, dict)
    assert m.status("p")["tasks"][0]["status"] == "in_progress"


# --- JSON events, usage, prompt (M2) -------------------------------------------

FIXTURE = Path(__file__).parent / "fixtures" / "opencode_run_events.jsonl"


def test_the_recorded_event_stream_is_parsed():
    from core.opencode_backend import parse_events

    summary, usage, counts = parse_events(FIXTURE.read_text())

    assert summary == "Created `hello.txt` containing `hi`."
    assert usage == {"input_tokens": 6326, "output_tokens": 100, "reasoning_tokens": 0,
                     "cached_input_tokens": 9472, "cache_write_tokens": 0,
                     "reported_cost_usd": 0.0, "steps": 2}
    assert counts == {"step_start": 2, "tool_use": 1, "step_finish": 2, "text": 1}


def test_garbage_lines_are_counted_not_fatal():
    from core.opencode_backend import parse_events

    summary, usage, counts = parse_events('not json\n[1,2]\n\n{"type":"text","part":{"text":"ok"}}\n')

    assert summary == "ok" and counts == {"unparsed": 2, "text": 1}
    assert usage["steps"] == 0


def test_the_worker_reports_usage_and_its_summary(tmp_path):
    body = f"cat {FIXTURE}"
    _, _, res = run_through_runner(tmp_path, body)

    assert res.status == "success"
    assert res.usage["input_tokens"] == 6326
    assert res.artifacts["summary"].startswith("Created")


def test_the_command_uses_json_format_the_model_and_extra_args(tmp_path):
    backend = OpenCodeCliBackend(opencode_bin="/x/opencode", model="prov/model",
                                 extra_args=["--pure"])

    cmd = backend.command(tmp_path, "PROMPT")

    assert cmd[:4] == ["/x/opencode", "run", "--format", "json"]
    assert cmd[cmd.index("--dir") + 1] == str(tmp_path)
    assert cmd[cmd.index("--model") + 1] == "prov/model"
    assert cmd[-2:] == ["--pure", "PROMPT"]
    assert "--auto" not in cmd


def test_the_prompt_carries_the_human_spec():
    from core.opencode_backend import build_prompt

    prompt = build_prompt({
        "id": "ml-1", "title": "Seeded rng", "description": "Add createRng(seed).",
        "acceptance": {"commands": ["npm ci", "node --test tests/tasks/ml-1.test.js"],
                       "protected_paths": ["tests/*", "package.json"]},
    })

    assert "Add createRng(seed)." in prompt
    assert "node --test tests/tasks/ml-1.test.js" in prompt
    assert "tests/*" in prompt and "package.json" in prompt
    assert "Do not push" in prompt


def test_a_task_without_acceptance_gets_no_acceptance_section():
    from core.opencode_backend import build_prompt

    prompt = build_prompt({"id": "t1", "title": "Do X"})

    assert "accepted only if" not in prompt
