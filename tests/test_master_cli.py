import ast
import contextlib
import io
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

from core.master import Master, build_parser, main
from core.project_state import ProjectState
from core.run_lock import LOCK_FILENAME


REPO_ROOT = Path(__file__).parent.parent
MASTER_SOURCE_PATH = REPO_ROOT / "core" / "master.py"

MILESTONE_ALPHA = {"id": "alpha", "name": "Alpha", "status": "in_progress"}
MILESTONE_BETA = {"id": "beta", "name": "Beta", "status": "planned"}

TASK_ONE = {
    "id": "task-001",
    "milestone": "alpha",
    "title": "First",
    "status": "completed",
    "assigned_to": "master",
}
TASK_TWO = {
    "id": "task-002",
    "milestone": "beta",
    "title": "Second",
    "status": "planned",
}

STATE_FILES = ["milestones.yaml", "project.yaml", "tasks.yaml"]


def write_project(
    root,
    directory="demo",
    project_id="demo",
    name="Demo Project",
    status="active",
    milestones=None,
    tasks=None,
):
    path = root / directory
    path.mkdir(parents=True, exist_ok=True)

    files = {
        "project.yaml": {"id": project_id, "name": name, "status": status},
        "milestones.yaml": {"milestones": milestones or []},
        "tasks.yaml": {"tasks": tasks or []},
    }

    for filename, data in files.items():
        (path / filename).write_text(yaml.safe_dump(data), encoding="utf-8")

    return path


def snapshot(root):
    """Every project file's bytes. The mutation lock file is not project data."""
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != LOCK_FILENAME
    }


def run_cli(*args, root=None):
    """Invoke the CLI in-process and capture its streams and exit status."""
    argv = (["--root", str(root)] if root is not None else []) + list(args)
    out, err = io.StringIO(), io.StringIO()

    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)

    return code, out.getvalue(), err.getvalue()


def run_module(*args):
    """Invoke the real `python -m core.master` entry point."""
    result = subprocess.run(
        [sys.executable, "-m", "core.master", *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=60,
    )

    return result.returncode, result.stdout, result.stderr


@pytest.fixture
def projects_root(tmp_path):
    root = tmp_path / "projects"
    write_project(
        root,
        milestones=[MILESTONE_ALPHA, MILESTONE_BETA],
        tasks=[TASK_ONE, TASK_TWO],
    )
    write_project(
        root,
        directory="other",
        project_id="other",
        name="Other Project",
        status="paused",
    )

    return root


@pytest.fixture
def demo_path(projects_root):
    return projects_root / "demo"


def test_module_entry_point_help_runs():
    code, out, _ = run_module("--help")

    assert code == 0

    for command in (
        "projects",
        "overview",
        "status",
        "create-milestone",
        "update-milestone",
        "create-task",
        "update-task",
    ):
        assert command in out

    assert "--root" in out


def test_module_entry_point_executes_a_command(projects_root):
    code, out, _ = run_module("--root", str(projects_root), "projects")

    assert code == 0
    assert out.split() == ["demo", "other"]


@pytest.mark.parametrize(
    "command",
    [
        "projects",
        "overview",
        "status",
        "create-milestone",
        "update-milestone",
        "create-task",
        "update-task",
    ],
)
def test_every_subcommand_has_its_own_help(command):
    code, out, _ = run_module(command, "--help")

    assert code == 0
    assert "usage:" in out
    assert command in out


def test_missing_command_is_a_usage_error():
    with pytest.raises(SystemExit) as exit_info:
        main([])

    assert exit_info.value.code == 2


def test_unknown_command_is_a_usage_error():
    with pytest.raises(SystemExit) as exit_info:
        main(["teleport"])

    assert exit_info.value.code == 2


def test_create_task_requires_milestone_and_title():
    with pytest.raises(SystemExit) as exit_info:
        main(["create-task", "demo", "task-9"])

    assert exit_info.value.code == 2


def test_projects_lists_only_usable_projects(projects_root):
    code, out, err = run_cli("projects", root=projects_root)

    assert code == 0
    assert err == ""
    assert out.split() == ["demo", "other"]


def test_projects_reports_empty_root(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()

    code, out, _ = run_cli("projects", root=empty)

    assert code == 0
    assert "no usable projects" in out


def test_projects_excludes_malformed_projects(projects_root):
    write_project(
        projects_root,
        directory="broken",
        project_id="broken",
        milestones=[MILESTONE_ALPHA],
        tasks=[dict(TASK_ONE, status="finished-ish")],
    )

    code, out, _ = run_cli("projects", root=projects_root)

    assert code == 0
    assert out.split() == ["demo", "other"]


def test_overview_reports_totals_and_status_counts(projects_root):
    code, out, err = run_cli("overview", root=projects_root)

    assert code == 0
    assert err == ""

    assert "PROJECTS (2)" in out
    assert "Demo Project" in out
    assert "Other Project" in out
    assert "TASKS" in out
    assert "MILESTONES" in out
    assert "TASK STATUS" in out
    assert "MILESTONE STATUS" in out
    assert "in_progress" in out
    assert "PROBLEMS (0)" in out


def test_overview_surfaces_problems(projects_root):
    write_project(
        projects_root,
        directory="broken",
        project_id="broken",
        tasks=[dict(TASK_ONE, status="finished-ish")],
    )

    code, out, _ = run_cli("overview", root=projects_root)

    assert code == 0
    assert "PROBLEMS (1)" in out
    assert "unreadable" in out
    assert "broken" in out


def test_status_shows_identity_progress_milestones_and_tasks(projects_root):
    code, out, err = run_cli("status", "demo", root=projects_root)

    assert code == 0
    assert err == ""

    assert "id" in out
    assert "demo" in out
    assert "Demo Project" in out
    assert "active" in out
    assert "TOTALS" in out
    assert "tasks" in out
    assert "milestones" in out
    assert "MILESTONES (2)" in out
    assert "TASKS (2)" in out
    assert "alpha" in out
    assert "task-001" in out
    assert "First" in out
    assert "in_progress" in out


def test_status_shows_assignments_and_placeholder(projects_root):
    code, out, _ = run_cli("status", "demo", root=projects_root)

    assert code == 0
    assert "ASSIGNEE" in out
    assert "master" in out
    assert "-" in out


def test_status_of_unknown_project_fails_readably(projects_root):
    code, out, err = run_cli("status", "ghost", root=projects_root)

    assert code == 1
    assert out == ""
    assert "error:" in err
    assert "Unknown project: ghost" in err


def test_status_of_malformed_project_fails_readably(projects_root):
    write_project(
        projects_root,
        directory="broken",
        project_id="broken",
        milestones=[MILESTONE_ALPHA],
        tasks=[dict(TASK_ONE, status="finished-ish")],
    )

    code, _, err = run_cli("status", "broken", root=projects_root)

    assert code == 1
    assert "error:" in err
    assert "broken" in err


def test_create_milestone_through_the_cli(projects_root, demo_path):
    code, out, err = run_cli(
        "create-milestone",
        "demo",
        "gamma",
        "Gamma Milestone",
        root=projects_root,
    )

    assert code == 0
    assert err == ""
    assert "MILESTONE CREATED" in out
    assert "gamma" in out
    assert "Gamma Milestone" in out

    state = ProjectState(demo_path)

    assert state.get_milestone("gamma") == {
        "id": "gamma",
        "name": "Gamma Milestone",
        "status": "planned",
    }
    assert state.validate() is True


def test_create_milestone_accepts_an_explicit_status(projects_root, demo_path):
    code, _, _ = run_cli(
        "create-milestone",
        "demo",
        "gamma",
        "Gamma",
        "--status",
        "in_progress",
        root=projects_root,
    )

    assert code == 0
    assert ProjectState(demo_path).get_milestone("gamma")["status"] == (
        "in_progress"
    )


def test_update_milestone_through_the_cli(projects_root, demo_path):
    code, out, err = run_cli(
        "update-milestone",
        "demo",
        "beta",
        "--name",
        "Beta Renamed",
        "--status",
        "completed",
        root=projects_root,
    )

    assert code == 0
    assert err == ""
    assert "MILESTONE UPDATED" in out
    assert ProjectState(demo_path).get_milestone("beta") == {
        "id": "beta",
        "name": "Beta Renamed",
        "status": "completed",
    }


def test_update_milestone_with_no_fields_is_refused(projects_root):
    before = snapshot(projects_root)

    code, out, err = run_cli(
        "update-milestone",
        "demo",
        "beta",
        root=projects_root,
    )

    assert code == 1
    assert out == ""
    assert "nothing to update" in err
    assert snapshot(projects_root) == before


def test_create_task_through_the_cli(projects_root, demo_path):
    code, out, err = run_cli(
        "create-task",
        "demo",
        "task-003",
        "--milestone",
        "alpha",
        "--title",
        "Third",
        "--assignee",
        "worker",
        root=projects_root,
    )

    assert code == 0
    assert err == ""
    assert "TASK CREATED" in out

    record = ProjectState(demo_path).get_task("task-003")

    assert record == {
        "id": "task-003",
        "milestone": "alpha",
        "title": "Third",
        "status": "planned",
        "assigned_to": "worker",
    }


def test_create_task_without_assignee_omits_the_field(projects_root, demo_path):
    code, _, _ = run_cli(
        "create-task",
        "demo",
        "task-004",
        "--milestone",
        "alpha",
        "--title",
        "Fourth",
        root=projects_root,
    )

    assert code == 0
    assert "assigned_to" not in ProjectState(demo_path).get_task("task-004")


def test_update_task_through_the_cli(projects_root, demo_path):
    code, out, err = run_cli(
        "update-task",
        "demo",
        "task-002",
        "--status",
        "blocked",
        "--title",
        "Second revised",
        root=projects_root,
    )

    assert code == 0
    assert err == ""
    assert "TASK UPDATED" in out

    record = ProjectState(demo_path).get_task("task-002")

    assert record["status"] == "blocked"
    assert record["title"] == "Second revised"
    assert record["milestone"] == "beta"


def test_update_task_with_no_fields_is_refused(projects_root):
    before = snapshot(projects_root)

    code, _, err = run_cli("update-task", "demo", "task-002", root=projects_root)

    assert code == 1
    assert "nothing to update" in err
    assert snapshot(projects_root) == before


def test_cli_mutations_are_visible_through_master(projects_root):
    run_cli(
        "create-milestone",
        "demo",
        "gamma",
        "Gamma",
        root=projects_root,
    )
    run_cli(
        "create-task",
        "demo",
        "task-003",
        "--milestone",
        "gamma",
        "--title",
        "Third",
        root=projects_root,
    )
    run_cli(
        "update-task",
        "demo",
        "task-003",
        "--status",
        "in_progress",
        root=projects_root,
    )

    master = Master(projects_root)
    status = master.status("demo")

    assert status["progress"]["total_milestones"] == 3
    assert status["progress"]["total_tasks"] == 3
    assert status["progress"]["task_status_counts"]["in_progress"] == 1
    assert status["progress"]["task_status_counts"]["completed"] == 1

    task = next(
        item for item in status["tasks"] if item["id"] == "task-003"
    )

    assert task["status"] == "in_progress"
    assert task["milestone"] == "gamma"


def test_read_only_commands_change_nothing(projects_root):
    before = snapshot(projects_root)

    for args in (
        ("projects",),
        ("overview",),
        ("status", "demo"),
    ):
        code, _, err = run_cli(*args, root=projects_root)

        assert code == 0
        assert err == ""

    assert snapshot(projects_root) == before
    assert sorted(
        path.name for path in (projects_root / "demo").iterdir()
    ) == STATE_FILES


def test_failed_mutations_change_nothing(projects_root):
    before = snapshot(projects_root)

    for args in (
        ("status", "ghost"),
        ("create-milestone", "demo", "alpha", "Duplicate"),
        ("create-task", "demo", "task-001", "--milestone", "alpha",
         "--title", "Duplicate"),
        ("create-task", "demo", "task-9", "--milestone", "ghost",
         "--title", "Bad reference"),
        ("update-task", "demo", "ghost", "--status", "completed"),
        ("update-milestone", "demo", "ghost", "--status", "completed"),
    ):
        code, out, err = run_cli(*args, root=projects_root)

        assert code == 1, args
        assert out == "", args
        assert err.startswith("error:"), args

    assert snapshot(projects_root) == before
    assert ProjectState(projects_root / "demo").validate() is True


def test_invalid_task_status_is_rejected_by_the_domain_layer(projects_root):
    before = snapshot(projects_root)

    code, _, err = run_cli(
        "update-task",
        "demo",
        "task-002",
        "--status",
        "finished-ish",
        root=projects_root,
    )

    assert code == 1
    assert "Invalid task status" in err
    assert "Valid statuses" in err
    assert snapshot(projects_root) == before


def test_invalid_milestone_status_is_rejected_by_the_domain_layer(projects_root):
    before = snapshot(projects_root)

    code, _, err = run_cli(
        "update-milestone",
        "demo",
        "beta",
        "--status",
        "blocked",
        root=projects_root,
    )

    assert code == 1
    assert "Invalid milestone status" in err
    assert snapshot(projects_root) == before


def test_missing_root_reports_an_error(tmp_path):
    code, _, err = run_cli("projects", root=tmp_path / "absent")

    assert code == 1
    assert "Projects root not found" in err


def test_root_flag_isolates_the_cli_from_the_real_projects(projects_root):
    code, out, _ = run_cli("projects", root=projects_root)

    assert code == 0
    assert "ai-system" not in out


def test_output_is_deterministic(projects_root):
    first = run_cli("overview", root=projects_root)
    second = run_cli("overview", root=projects_root)
    status_first = run_cli("status", "demo", root=projects_root)
    status_second = run_cli("status", "demo", root=projects_root)

    assert first == second
    assert status_first == status_second


def test_unexpected_errors_are_not_swallowed(projects_root, monkeypatch):
    def exploding_status(self, project_id):
        raise RuntimeError("unexpected internal failure")

    monkeypatch.setattr(Master, "status", exploding_status)

    with pytest.raises(RuntimeError, match="unexpected internal failure"):
        run_cli("status", "demo", root=projects_root)


def test_cli_does_not_import_yaml_or_project_state():
    tree = ast.parse(MASTER_SOURCE_PATH.read_text(encoding="utf-8"))
    modules = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)

    assert "yaml" not in modules
    assert "core.project_state" not in modules


def test_cli_writes_no_files_itself():
    """Every mutation must reach disk through WorkManager."""
    source = MASTER_SOURCE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    called = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            function = node.func

            if isinstance(function, ast.Attribute):
                called.add(function.attr)
            elif isinstance(node.func, ast.Name):
                called.add(node.func.id)

    for forbidden in (
        "write_yaml_atomically",
        "write_text",
        "write_bytes",
        "open",
        "mkstemp",
        "fsync",
        "unlink",
        "mkdir",
    ):
        assert forbidden not in called, f"CLI calls {forbidden}()"


def test_cli_cannot_write_because_it_imports_no_write_primitives():
    """The structural guarantee behind 'the CLI never writes'.

    Without os and tempfile there is no way to open, truncate or replace a
    project file, so the no-write claim does not rest on a list of method
    names that could be spelled differently.
    """
    tree = ast.parse(MASTER_SOURCE_PATH.read_text(encoding="utf-8"))
    modules = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)

    assert modules & {"os", "tempfile", "shutil", "pathlib2"} == set()


def test_cli_does_not_duplicate_the_status_vocabulary():
    """argparse `choices` would restate WorkManager's status lists.

    The CLI must let the domain layer reject a bad status, otherwise the
    vocabulary would live in two places and drift.
    """
    tree = ast.parse(MASTER_SOURCE_PATH.read_text(encoding="utf-8"))
    choice_keywords = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.keyword) and node.arg == "choices"
    ]

    assert choice_keywords == []


def test_cli_does_not_duplicate_the_status_vocabulary():
    """The CLI must not restate which statuses are valid.

    Passing a default such as "planned" straight through to WorkManager is
    fine and deliberate. What must not exist is a second copy of the
    vocabulary to validate against, because it would drift from the domain
    layer's list.
    """
    source = MASTER_SOURCE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    vocabulary = {
        "planned",
        "in_progress",
        "blocked",
        "cancelled",
        "completed",
    }

    collections = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.Set, ast.List, ast.Tuple))
    ]

    for node in collections:
        literals = {
            element.value
            for element in node.elts
            if isinstance(element, ast.Constant)
            and isinstance(element.value, str)
        }

        assert len(literals & vocabulary) < 2, (
            f"CLI defines a status vocabulary: {sorted(literals & vocabulary)}"
        )

    assert "VALID_TASK_STATUSES" not in source
    assert "VALID_MILESTONE_STATUSES" not in source


def test_cli_defines_no_project_field_whitelist():
    """Update flags are the CLI's field list; it must not validate values."""
    parser = build_parser()

    update_task = next(
        action
        for action in parser._subparsers._group_actions[0].choices.values()
        if action.prog.endswith("update-task")
    )

    options = {
        option
        for action in update_task._actions
        for option in action.option_strings
        if option.startswith("--")
    }

    assert options == {"--help", "--milestone", "--title", "--status",
                       "--assignee"}


def test_parser_exposes_exactly_the_documented_commands():
    parser = build_parser()

    actions = [
        action
        for action in parser._subparsers._group_actions
        if hasattr(action, "choices") and action.choices
    ]

    assert len(actions) == 1

    assert set(actions[0].choices) == {
        "projects",
        "overview",
        "status",
        "create-milestone",
        "update-milestone",
        "create-task",
        "update-task",
        "set-acceptance",
    }


def test_real_projects_are_untouched_by_the_test_suite():
    """Guards against a CLI test defaulting to the repository root."""
    repository_root = Master().root
    before = snapshot(repository_root)

    with tempfile.TemporaryDirectory() as directory:
        scratch = Path(directory) / "projects"
        write_project(scratch)

        run_cli("projects", root=scratch)
        run_cli("status", "demo", root=scratch)

    assert snapshot(repository_root) == before