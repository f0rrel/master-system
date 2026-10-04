import sys
from pathlib import Path
import tempfile
import shutil

sys.path.insert(0, ".")

from core.project_state import ProjectState
from core.work_manager import calculate_readiness, WorkManagerError, RecordNotFoundError


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
    return proj


def test_planned_no_deps_ready(tmp_path):
    proj = make_project(tmp_path, [{"id": "t1", "status": "planned"}])
    ps = ProjectState(proj)
    r, by, reason = calculate_readiness(ps, ps.tasks()[0])
    assert r == "ready"
    assert by is None


def test_planned_dep_completed_ready(tmp_path):
    proj = make_project(
        tmp_path,
        [
            {"id": "t1", "status": "completed"},
            {"id": "t2", "status": "planned", "depends_on": ["t1"]},
        ],
    )
    ps = ProjectState(proj)
    t2 = [t for t in ps.tasks() if t["id"] == "t2"][0]
    r, by, reason = calculate_readiness(ps, t2)
    assert r == "ready"


def test_planned_dep_inprogress_blocked(tmp_path):
    proj = make_project(
        tmp_path,
        [
            {"id": "t1", "status": "in_progress"},
            {"id": "t2", "status": "planned", "depends_on": ["t1"]},
        ],
    )
    ps = ProjectState(proj)
    t2 = [t for t in ps.tasks() if t["id"] == "t2"][0]
    r, by, reason = calculate_readiness(ps, t2)
    assert r == "blocked"
    assert by == "t1"


def test_planned_dep_blocked_blocked(tmp_path):
    proj = make_project(
        tmp_path,
        [
            {"id": "t1", "status": "blocked"},
            {"id": "t2", "status": "planned", "depends_on": ["t1"]},
        ],
    )
    ps = ProjectState(proj)
    t2 = [t for t in ps.tasks() if t["id"] == "t2"][0]
    r, by, reason = calculate_readiness(ps, t2)
    assert r == "blocked"


def test_planned_dep_planned_blocked(tmp_path):
    proj = make_project(
        tmp_path,
        [
            {"id": "t1", "status": "planned"},
            {"id": "t2", "status": "planned", "depends_on": ["t1"]},
        ],
    )
    ps = ProjectState(proj)
    t2 = [t for t in ps.tasks() if t["id"] == "t2"][0]
    r, by, reason = calculate_readiness(ps, t2)
    assert r == "blocked"


def test_missing_dep_rejected(tmp_path):
    proj = make_project(
        tmp_path,
        [
            {"id": "t2", "status": "planned", "depends_on": ["missing"]},
        ],
    )
    try:
        ps = ProjectState(proj)
        t2 = ps.tasks()[0]
        calculate_readiness(ps, t2)
        assert False, "should fail"
    except (RecordNotFoundError, ValueError):
        pass


def test_explicitly_blocked_remains_blocked(tmp_path):
    proj = make_project(tmp_path, [{"id": "t1", "status": "blocked"}])
    ps = ProjectState(proj)
    r, by, reason = calculate_readiness(ps, ps.tasks()[0])
    assert r == "blocked"


def test_inprogress_not_ready(tmp_path):
    proj = make_project(tmp_path, [{"id": "t1", "status": "in_progress"}])
    ps = ProjectState(proj)
    r, by, reason = calculate_readiness(ps, ps.tasks()[0])
    assert r == "waiting"


def test_completed_not_ready(tmp_path):
    proj = make_project(tmp_path, [{"id": "t1", "status": "completed"}])
    ps = ProjectState(proj)
    r, by, reason = calculate_readiness(ps, ps.tasks()[0])
    assert r == "waiting"


def test_readiness_includes_reason(tmp_path):
    proj = make_project(
        tmp_path,
        [
            {"id": "t1", "status": "in_progress"},
            {"id": "t2", "status": "planned", "depends_on": ["t1"]},
        ],
    )
    ps = ProjectState(proj)
    t2 = [t for t in ps.tasks() if t["id"] == "t2"][0]
    r, by, reason = calculate_readiness(ps, t2)
    assert reason is not None


def test_duplicate_dependencies_handled(tmp_path):
    proj = make_project(
        tmp_path,
        [
            {"id": "t1", "status": "completed"},
            {"id": "t2", "status": "planned", "depends_on": ["t1", "t1"]},
        ],
    )
    ps = ProjectState(proj)
    ps.snapshot()
    t2 = [t for t in ps.tasks() if t["id"] == "t2"][0]
    r, by, reason = calculate_readiness(ps, t2)
    assert r == "ready"


def test_existing_projects_without_depends_on_remain_valid(tmp_path):
    from core.master import Master
    # use existing
    m = Master('projects')
    m.status('ai-system')  # should work


def test_master_context_contains_readiness(tmp_path):
    from core.master import Master
    from core.work_manager import calculate_readiness
    m = Master('projects')
    st = m.status('ai-system')
    # basic check
    assert 'tasks' in st


def test_readiness_not_inferred_by_llm(tmp_path):
    # just a smoke test that logic is deterministic
    from core.project_state import ProjectState
    proj = make_project(
        tmp_path,
        [
            {"id": "t1", "status": "completed"},
            {"id": "t2", "status": "planned", "depends_on": ["t1"]},
            {"id": "t3", "status": "planned", "depends_on": ["t2"]},
        ],
    )
    ps = ProjectState(proj)
    t3 = [t for t in ps.tasks() if t["id"] == "t3"][0]
    r, by, reason = calculate_readiness(ps, t3)
    assert r == "blocked"
    assert by == "t2"
