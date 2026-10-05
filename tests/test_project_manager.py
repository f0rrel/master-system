import pathlib
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from core.project_manager import (
    DEFAULT_PROJECTS_ROOT,
    ProjectManager,
    UnknownProjectError,
)
from core.project_state import ProjectState


REPO_ROOT = Path(__file__).parent.parent
MODULE_PATH = REPO_ROOT / "core" / "project_manager.py"

ALPHA_MILESTONE = {"id": "alpha", "name": "Alpha", "status": "in_progress"}
BETA_MILESTONE = {"id": "beta", "name": "Beta", "status": "planned"}

ALPHA_TASK = {
    "id": "task-001",
    "milestone": "alpha",
    "title": "First",
    "status": "completed",
}
BETA_TASK = {
    "id": "task-002",
    "milestone": "beta",
    "title": "Second",
    "status": "in_progress",
}


def write_project(
    root,
    directory,
    project=None,
    milestones=None,
    tasks=None,
):
    path = root / directory
    path.mkdir(parents=True, exist_ok=True)

    files = {
        "project.yaml": project
        if project is not None
        else {
            "id": directory,
            "name": directory.title(),
            "status": "active",
        },
        "milestones.yaml": {"milestones": milestones or []},
        "tasks.yaml": {"tasks": tasks or []},
    }

    for filename, data in files.items():
        (path / filename).write_text(yaml.safe_dump(data), encoding="utf-8")

    return ProjectState(path)


def snapshot_tree(root):
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


@pytest.fixture
def projects_root(tmp_path):
    root = tmp_path / "projects"
    root.mkdir()

    return root


@pytest.fixture
def two_projects(projects_root):
    write_project(
        projects_root,
        "alpha",
        project={
            "id": "alpha",
            "name": "Alpha Project",
            "status": "active",
        },
        milestones=[ALPHA_MILESTONE],
        tasks=[ALPHA_TASK],
    )
    write_project(
        projects_root,
        "beta",
        project={"id": "beta", "name": "Beta Project", "status": "paused"},
        milestones=[BETA_MILESTONE],
        tasks=[BETA_TASK],
    )

    return projects_root


def test_lists_every_discovered_project(two_projects):
    assert ProjectManager(two_projects).list_projects() == ["alpha", "beta"]


def test_default_root_is_the_repository_projects_directory():
    assert DEFAULT_PROJECTS_ROOT == REPO_ROOT / "projects"
    assert ProjectManager().list_projects() == ["ai-system", "match-legends"]


def test_root_is_configurable(tmp_path):
    root = tmp_path / "elsewhere"
    root.mkdir()
    write_project(root, "solo")

    assert ProjectManager(root).list_projects() == ["solo"]
    assert ProjectManager(str(root)).list_projects() == ["solo"]


def test_missing_root_is_rejected_loudly(tmp_path):
    with pytest.raises(FileNotFoundError, match="Projects root not found"):
        ProjectManager(tmp_path / "does-not-exist")


def test_get_project_returns_project_state(two_projects):
    project = ProjectManager(two_projects).get_project("alpha")

    assert isinstance(project, ProjectState)
    assert project.project_path == two_projects / "alpha"
    assert project.project()["name"] == "Alpha Project"


def test_get_unknown_project_raises(two_projects):
    with pytest.raises(UnknownProjectError, match="Unknown project: gamma"):
        ProjectManager(two_projects).get_project("gamma")


def test_empty_projects_directory(projects_root):
    manager = ProjectManager(projects_root)

    assert manager.list_projects() == []
    assert manager.problems() == []

    overview = manager.overview()

    assert overview["project_count"] == 0
    assert overview["projects"] == []
    assert overview["problem_count"] == 0
    assert overview["totals"]["tasks"] == 0
    assert overview["totals"]["milestones"] == 0


def test_directories_without_project_yaml_are_ignored(projects_root):
    (projects_root / "not-a-project").mkdir()
    (projects_root / "stray.yaml").write_text("x: 1\n", encoding="utf-8")
    write_project(projects_root, "solo")

    manager = ProjectManager(projects_root)

    assert manager.list_projects() == ["solo"]
    assert manager.problems() == []


def test_overview_reports_per_project_details(two_projects):
    overview = ProjectManager(two_projects).overview()

    assert overview["root"] == str(two_projects)
    assert overview["project_count"] == 2

    by_id = {item["project_id"]: item for item in overview["projects"]}

    assert by_id["alpha"]["project_name"] == "Alpha Project"
    assert by_id["alpha"]["project_status"] == "active"
    assert by_id["alpha"]["total_tasks"] == 1
    assert by_id["alpha"]["task_status_counts"]["completed"] == 1
    assert by_id["alpha"]["total_milestones"] == 1
    assert by_id["alpha"]["milestone_status_counts"]["in_progress"] == 1
    assert by_id["alpha"]["path"] == str(two_projects / "alpha")

    assert by_id["beta"]["project_status"] == "paused"
    assert by_id["beta"]["task_status_counts"]["in_progress"] == 1
    assert by_id["beta"]["milestone_status_counts"]["planned"] == 1


def test_overview_aggregates_totals_across_projects(two_projects):
    totals = ProjectManager(two_projects).overview()["totals"]

    assert totals["projects"] == 2
    assert totals["tasks"] == 2
    assert totals["milestones"] == 2
    assert totals["task_status_counts"]["completed"] == 1
    assert totals["task_status_counts"]["in_progress"] == 1
    assert totals["task_status_counts"]["planned"] == 0
    assert totals["task_status_counts"]["blocked"] == 0
    assert totals["milestone_status_counts"]["in_progress"] == 1
    assert totals["milestone_status_counts"]["planned"] == 1
    assert totals["milestone_status_counts"]["completed"] == 0


def test_malformed_project_does_not_break_discovery(projects_root):
    write_project(
        projects_root,
        "alpha",
        milestones=[ALPHA_MILESTONE],
        tasks=[dict(ALPHA_TASK, status="finished-ish")],
    )
    write_project(
        projects_root,
        "beta",
        project={"id": "beta", "name": "Beta", "status": "active"},
        milestones=[BETA_MILESTONE],
        tasks=[BETA_TASK],
    )

    manager = ProjectManager(projects_root)

    assert manager.list_projects() == ["beta"]

    problems = manager.problems()

    assert len(problems) == 1
    assert problems[0]["kind"] == "unreadable"
    assert problems[0]["path"] == str(projects_root / "alpha")
    assert problems[0]["project_id"] == "alpha"
    assert "unknown status 'finished-ish'" in problems[0]["error"]

    overview = manager.overview()

    assert overview["project_count"] == 1
    assert overview["problem_count"] == 1
    assert overview["totals"]["tasks"] == 1
    assert overview["totals"]["task_status_counts"]["completed"] == 0


def test_unparseable_yaml_is_reported_rather_than_raised(projects_root):
    broken = projects_root / "broken"
    broken.mkdir()
    (broken / "project.yaml").write_text("id: [unclosed\n", encoding="utf-8")
    write_project(projects_root, "beta")

    manager = ProjectManager(projects_root)

    assert manager.list_projects() == ["beta"]
    assert manager.problems()[0]["kind"] == "unreadable"
    assert manager.problems()[0]["path"] == str(broken)


def test_project_missing_a_state_file_is_reported(projects_root):
    partial = projects_root / "partial"
    partial.mkdir()
    (partial / "project.yaml").write_text(
        yaml.safe_dump({"id": "partial", "name": "Partial", "status": "active"}),
        encoding="utf-8",
    )
    write_project(projects_root, "beta")

    manager = ProjectManager(projects_root)

    assert manager.list_projects() == ["beta"]

    problem = manager.problems()[0]

    assert problem["path"] == str(partial)
    assert "milestones.yaml" in problem["error"]


def test_duplicate_project_ids_exclude_every_copy(projects_root):
    write_project(
        projects_root,
        "alpha-copy",
        project={"id": "alpha", "name": "Alpha", "status": "active"},
    )
    write_project(
        projects_root,
        "alpha-original",
        project={"id": "alpha", "name": "Alpha", "status": "active"},
    )
    write_project(projects_root, "beta")

    manager = ProjectManager(projects_root)

    assert manager.list_projects() == ["beta"]

    duplicates = [
        problem
        for problem in manager.problems()
        if problem["kind"] == "duplicate_id"
    ]

    assert len(duplicates) == 2
    assert {problem["project_id"] for problem in duplicates} == {"alpha"}

    assert manager.overview()["project_count"] == 1


def test_get_project_explains_why_a_project_is_unavailable(projects_root):
    write_project(
        projects_root,
        "one",
        project={"id": "dup", "name": "One", "status": "active"},
    )
    write_project(
        projects_root,
        "two",
        project={"id": "dup", "name": "Two", "status": "active"},
    )

    with pytest.raises(UnknownProjectError, match="Duplicate project id 'dup'"):
        ProjectManager(projects_root).get_project("dup")


def test_malformed_projects_are_absent_from_overview_projects(projects_root):
    write_project(projects_root, "alpha", tasks=[dict(ALPHA_TASK)])
    write_project(
        projects_root,
        "beta",
        project={"id": "beta", "name": "Beta", "status": "active"},
    )

    overview = ProjectManager(projects_root).overview()

    assert [
        item["project_id"] for item in overview["projects"]
    ] == ["beta"]


def test_valid_project_reports_its_own_id(projects_root):
    write_project(
        projects_root,
        "healthy",
        milestones=[ALPHA_MILESTONE],
        tasks=[dict(ALPHA_TASK)],
    )

    manager = ProjectManager(projects_root)

    assert manager.list_projects() == ["healthy"]
    assert manager.problems() == []
    assert manager.get_project("healthy").project()["id"] == "healthy"


def test_malformed_project_reports_its_declared_id(projects_root):
    write_project(
        projects_root,
        "alpha",
        milestones=[ALPHA_MILESTONE],
        tasks=[dict(ALPHA_TASK, status="finished-ish")],
    )

    problems = ProjectManager(projects_root).problems()

    assert len(problems) == 1
    assert problems[0]["project_id"] == "alpha"
    assert problems[0]["kind"] == "unreadable"
    assert "unknown status 'finished-ish'" in problems[0]["error"]


def test_declared_id_is_recovered_even_when_other_files_fail(projects_root):
    write_project(
        projects_root,
        "missing-milestones",
        project={"id": "gone-miles", "name": "Gone", "status": "active"},
    )
    (projects_root / "missing-milestones" / "milestones.yaml").unlink()

    problems = ProjectManager(projects_root).problems()

    assert len(problems) == 1
    assert problems[0]["project_id"] == "gone-miles"
    assert "milestones.yaml" in problems[0]["error"]


def test_malformed_project_is_never_listed_as_usable(projects_root):
    write_project(
        projects_root,
        "alpha",
        milestones=[ALPHA_MILESTONE],
        tasks=[dict(ALPHA_TASK, status="finished-ish")],
    )
    write_project(projects_root, "beta")

    manager = ProjectManager(projects_root)

    assert manager.list_projects() == ["beta"]
    assert "alpha" not in manager.list_projects()
    assert manager.overview()["project_count"] == 1
    assert [
        item["project_id"] for item in manager.overview()["projects"]
    ] == ["beta"]


def test_get_project_still_returns_valid_projects(projects_root):
    write_project(
        projects_root,
        "alpha",
        milestones=[ALPHA_MILESTONE],
        tasks=[dict(ALPHA_TASK, status="finished-ish")],
    )
    write_project(projects_root, "beta", milestones=[BETA_MILESTONE], tasks=[dict(BETA_TASK)])

    state = ProjectManager(projects_root).get_project("beta")

    assert isinstance(state, ProjectState)
    assert state.project()["id"] == "beta"
    assert state.validate() is True


def test_get_project_distinguishes_broken_from_absent(projects_root):
    write_project(
        projects_root,
        "alpha",
        milestones=[ALPHA_MILESTONE],
        tasks=[dict(ALPHA_TASK, status="finished-ish")],
    )

    manager = ProjectManager(projects_root)

    with pytest.raises(UnknownProjectError) as broken:
        manager.get_project("alpha")

    with pytest.raises(UnknownProjectError) as absent:
        manager.get_project("never-existed")

    broken_message = str(broken.value)
    absent_message = str(absent.value)

    assert broken_message != absent_message
    assert "Project 'alpha' is not available" in broken_message
    assert "finished-ish" in broken_message
    assert absent_message == "Unknown project: never-existed"
    assert "is not available" not in absent_message


def test_broken_project_id_is_reported_for_every_kind_of_state_failure(
    projects_root,
):
    write_project(
        projects_root,
        "bad-status",
        milestones=[ALPHA_MILESTONE],
        tasks=[dict(ALPHA_TASK, status="finished-ish")],
    )
    write_project(
        projects_root,
        "bad-ref",
        milestones=[ALPHA_MILESTONE],
        tasks=[dict(BETA_TASK, milestone="ghost")],
    )
    write_project(projects_root, "no-name", project={"id": "no-name"})

    problems = ProjectManager(projects_root).problems()

    assert {problem["project_id"] for problem in problems} == {
        "bad-status",
        "bad-ref",
        "no-name",
    }


def test_unparseable_project_yaml_leaves_the_id_unrecoverable(projects_root):
    garbled = projects_root / "garbled"
    garbled.mkdir()
    (garbled / "project.yaml").write_text("id: [unclosed\n", encoding="utf-8")
    write_project(projects_root, "beta")

    manager = ProjectManager(projects_root)

    assert manager.list_projects() == ["beta"]

    problem = manager.problems()[0]

    assert problem["project_id"] is None
    assert problem["kind"] == "unreadable"

    with pytest.raises(UnknownProjectError, match="Unknown project: garbled"):
        manager.get_project("garbled")


def test_project_yaml_without_a_usable_id_is_handled_safely(projects_root):
    for directory, document in (
        ("no-id", {"name": "No Id", "status": "active"}),
        ("blank-id", {"id": "   ", "name": "Blank", "status": "active"}),
        ("list-id", ["not", "a", "mapping"]),
    ):
        path = projects_root / directory
        path.mkdir()
        (path / "project.yaml").write_text(
            yaml.safe_dump(document), encoding="utf-8"
        )

    write_project(projects_root, "beta")

    manager = ProjectManager(projects_root)

    assert manager.list_projects() == ["beta"]

    problems = manager.problems()

    assert len(problems) == 3
    assert {problem["project_id"] for problem in problems} == {None}
    assert all(problem["kind"] == "unreadable" for problem in problems)


def test_missing_project_yaml_is_not_a_project(projects_root):
    (projects_root / "empty-dir").mkdir()
    write_project(projects_root, "beta")

    manager = ProjectManager(projects_root)

    assert manager.list_projects() == ["beta"]
    assert manager.problems() == []


def test_recovered_id_does_not_make_a_project_usable(projects_root):
    """Recoverable identity is not permission to bypass validation."""
    write_project(
        projects_root,
        "alpha",
        milestones=[ALPHA_MILESTONE],
        tasks=[dict(ALPHA_TASK, status="finished-ish")],
    )

    manager = ProjectManager(projects_root)

    assert manager.problems()[0]["project_id"] == "alpha"

    for _ in range(3):
        assert manager.list_projects() == []
        assert manager.overview()["project_count"] == 0

        with pytest.raises(UnknownProjectError, match="is not available"):
            manager.get_project("alpha")

        assert manager.problems()[0]["project_id"] == "alpha"


def test_broken_and_valid_projects_are_isolated_from_each_other(projects_root):
    write_project(
        projects_root,
        "alpha",
        milestones=[ALPHA_MILESTONE],
        tasks=[dict(ALPHA_TASK, status="finished-ish")],
    )
    write_project(
        projects_root,
        "beta",
        milestones=[BETA_MILESTONE],
        tasks=[dict(BETA_TASK)],
    )

    before = snapshot_tree(projects_root)
    manager = ProjectManager(projects_root)

    manager.list_projects()
    manager.overview()
    manager.problems()

    with pytest.raises(UnknownProjectError):
        manager.get_project("alpha")

    assert manager.get_project("beta").validate() is True
    assert snapshot_tree(projects_root) == before


def test_discovery_still_writes_nothing(projects_root):
    write_project(
        projects_root,
        "alpha",
        milestones=[ALPHA_MILESTONE],
        tasks=[dict(ALPHA_TASK, status="finished-ish")],
    )

    before = snapshot_tree(projects_root)
    manager = ProjectManager(projects_root)

    manager.list_projects()
    manager.overview()
    manager.problems()

    with pytest.raises(UnknownProjectError):
        manager.get_project("alpha")

    assert snapshot_tree(projects_root) == before


def test_valid_and_valid_duplicate_ids_are_both_excluded(projects_root):
    write_project(
        projects_root,
        "twin-one",
        project={"id": "twin", "name": "Twin One", "status": "active"},
    )
    write_project(
        projects_root,
        "twin-two",
        project={"id": "twin", "name": "Twin Two", "status": "active"},
    )

    manager = ProjectManager(projects_root)

    assert manager.list_projects() == []

    duplicates = [p for p in manager.problems() if p["kind"] == "duplicate_id"]

    assert len(duplicates) == 2
    assert {p["project_id"] for p in duplicates} == {"twin"}


def test_valid_and_malformed_duplicate_ids_are_both_excluded(projects_root):
    write_project(
        projects_root,
        "twin-good",
        project={"id": "twin", "name": "Twin Good", "status": "active"},
        milestones=[ALPHA_MILESTONE],
        tasks=[dict(ALPHA_TASK)],
    )
    write_project(
        projects_root,
        "twin-broken",
        project={"id": "twin", "name": "Twin Broken", "status": "active"},
        milestones=[ALPHA_MILESTONE],
        tasks=[dict(ALPHA_TASK, status="finished-ish")],
    )

    manager = ProjectManager(projects_root)

    assert manager.list_projects() == []
    assert manager.overview()["project_count"] == 0

    duplicates = [p for p in manager.problems() if p["kind"] == "duplicate_id"]

    assert {p["project_id"] for p in duplicates} == {"twin"}
    assert {pathlib.Path(p["path"]).name for p in duplicates} == {
        "twin-good",
        "twin-broken",
    }

    unreadable = [p for p in manager.problems() if p["kind"] == "unreadable"]

    assert len(unreadable) == 1
    assert pathlib.Path(unreadable[0]["path"]).name == "twin-broken"


def test_malformed_and_malformed_duplicate_ids_are_both_excluded(projects_root):
    for directory in ("twin-broken-one", "twin-broken-two"):
        write_project(
            projects_root,
            directory,
            project={"id": "twin", "name": directory, "status": "active"},
            milestones=[ALPHA_MILESTONE],
            tasks=[dict(ALPHA_TASK, status="finished-ish")],
        )

    manager = ProjectManager(projects_root)

    assert manager.list_projects() == []

    duplicates = [p for p in manager.problems() if p["kind"] == "duplicate_id"]

    assert len(duplicates) == 2
    assert {p["project_id"] for p in duplicates} == {"twin"}

    unreadable = [p for p in manager.problems() if p["kind"] == "unreadable"]

    assert len(unreadable) == 2


@pytest.mark.parametrize(
    "broken",
    [(False, True), (True, False), (True, True)],
    ids=["valid+valid", "valid+malformed", "malformed+malformed"],
)
def test_get_project_never_selects_an_arbitrary_duplicate(
    projects_root,
    broken,
):
    first_broken, second_broken = broken

    write_project(
        projects_root,
        "twin-one",
        project={"id": "twin", "name": "Twin One", "status": "active"},
        milestones=[ALPHA_MILESTONE],
        tasks=[
            dict(ALPHA_TASK, status="finished-ish" if first_broken else "planned")
        ],
    )
    write_project(
        projects_root,
        "twin-two",
        project={"id": "twin", "name": "Twin Two", "status": "active"},
        milestones=[BETA_MILESTONE],
        tasks=[
            dict(BETA_TASK, status="finished-ish" if second_broken else "planned")
        ],
    )

    manager = ProjectManager(projects_root)

    with pytest.raises(UnknownProjectError) as error:
        manager.get_project("twin")

    message = str(error.value)

    assert "Duplicate project id 'twin'" in message
    assert "2 directories" in message

    for _ in range(3):
        with pytest.raises(UnknownProjectError):
            manager.get_project("twin")


def test_duplicate_count_reflects_every_claiming_directory(projects_root):
    for directory in ("twin-one", "twin-two", "twin-three"):
        write_project(
            projects_root,
            directory,
            project={"id": "twin", "name": directory, "status": "active"},
        )

    manager = ProjectManager(projects_root)

    assert manager.list_projects() == []
    assert len(manager.problems()) == 3

    for problem in manager.problems():
        assert "3 directories" in problem["error"]


def test_unrelated_projects_stay_usable_beside_a_duplicate_pair(projects_root):
    write_project(
        projects_root,
        "twin-good",
        project={"id": "twin", "name": "Twin Good", "status": "active"},
    )
    write_project(
        projects_root,
        "twin-broken",
        project={"id": "twin", "name": "Twin Broken", "status": "active"},
        tasks=[dict(ALPHA_TASK, status="finished-ish")],
    )
    write_project(
        projects_root,
        "bystander",
        milestones=[ALPHA_MILESTONE],
        tasks=[dict(ALPHA_TASK)],
    )

    manager = ProjectManager(projects_root)

    assert manager.list_projects() == ["bystander"]

    state = manager.get_project("bystander")

    assert state.validate() is True
    assert state.project()["id"] == "bystander"
    assert manager.overview()["project_count"] == 1


def test_duplicate_claims_without_a_recoverable_id_are_safe(projects_root):
    garbled = projects_root / "garbled-one"
    garbled.mkdir()
    (garbled / "project.yaml").write_text("id: [unclosed\n", encoding="utf-8")

    other = projects_root / "garbled-two"
    other.mkdir()
    (other / "project.yaml").write_text("id: [also unclosed\n", encoding="utf-8")

    write_project(projects_root, "bystander")

    manager = ProjectManager(projects_root)

    assert manager.list_projects() == ["bystander"]

    problems = manager.problems()

    assert len(problems) == 2
    assert {p["project_id"] for p in problems} == {None}
    assert all(p["kind"] == "unreadable" for p in problems)

    with pytest.raises(UnknownProjectError, match="Unknown project"):
        manager.get_project("garbled-one")


def test_an_unrecoverable_id_does_not_collide_with_a_usable_id(projects_root):
    nameless = projects_root / "nameless"
    nameless.mkdir()
    (nameless / "project.yaml").write_text(
        yaml.safe_dump({"name": "No Id Here", "status": "active"}),
        encoding="utf-8",
    )
    write_project(projects_root, "twin")

    manager = ProjectManager(projects_root)

    assert manager.list_projects() == ["twin"]
    assert manager.get_project("twin").validate() is True
    assert [p["project_id"] for p in manager.problems()] == [None]


def test_three_way_malformed_duplicate_is_reported_once_per_directory(
    projects_root,
):
    for directory in ("one", "two", "three"):
        write_project(
            projects_root,
            directory,
            project={"id": "twin", "name": directory, "status": "active"},
            tasks=[dict(ALPHA_TASK, status="finished-ish")],
        )

    manager = ProjectManager(projects_root)

    assert manager.list_projects() == []
    assert len(manager.problems()) == 6
    assert {p["path"] for p in manager.problems()} == {
        str(projects_root / name) for name in ("one", "two", "three")
    }


def test_overview_of_repository_project():
    overview = ProjectManager().overview()

    assert overview["project_count"] == 2
    assert overview["problem_count"] == 0
    assert [p["project_id"] for p in overview["projects"]] == ["ai-system", "match-legends"]
    assert all(p["project_status"] == "active" for p in overview["projects"])
    assert overview["totals"]["tasks"] == 4 + 8
    assert overview["totals"]["milestones"] == 8 + 2


def test_manager_reads_without_writing(projects_root):
    write_project(projects_root, "alpha")
    write_project(projects_root, "beta")

    before = snapshot_tree(projects_root)

    manager = ProjectManager(projects_root)
    manager.list_projects()
    manager.problems()
    manager.overview()

    for project_id in manager.list_projects():
        manager.get_project(project_id).progress()

    assert snapshot_tree(projects_root) == before


def test_reads_repository_projects_without_writing():
    before = snapshot_tree(DEFAULT_PROJECTS_ROOT)

    manager = ProjectManager()
    manager.list_projects()
    manager.problems()
    manager.overview()

    assert snapshot_tree(DEFAULT_PROJECTS_ROOT) == before


def test_demo_does_not_modify_project_files():
    before = snapshot_tree(DEFAULT_PROJECTS_ROOT)

    result = subprocess.run(
        [sys.executable, str(MODULE_PATH)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "PROJECTS ROOT" in result.stdout
    assert "ai-system" in result.stdout
    assert "OVERVIEW" in result.stdout
    assert "PROBLEMS" in result.stdout
    assert snapshot_tree(DEFAULT_PROJECTS_ROOT) == before


def test_demo_runs_as_a_module():
    result = subprocess.run(
        [sys.executable, "-m", "core.project_manager"],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )

    assert result.returncode == 0, result.stderr
    assert "ai-system" in result.stdout
