import sys
from pathlib import Path
import copy

sys.path.insert(0, ".")

from core.verification import VerificationResult, VerificationBackend, VALID_VERDICTS
from core.master import Master


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


class DummyVerifier:
    def verify(self, task, context, evidence=None, workspace=None):
        return VerificationResult(
            verdict="pass",
            summary="ok",
            findings=[{"type": "test", "detail": "all pass"}],
            evidence={"tests": 5},
        )


class AnotherVerifier:
    def verify(self, task, context, evidence=None, workspace=None):
        return VerificationResult(verdict="fail", summary="issues found")


def test_verification_result_valid_verdicts():
    for v in VALID_VERDICTS:
        r = VerificationResult(verdict=v)
        assert r.verdict == v


def test_invalid_verdict_rejected():
    try:
        VerificationResult(verdict="unknown")
        assert False
    except ValueError:
        pass


def test_findings_evidence_normalized():
    r = VerificationResult(
        verdict="needs_human",
        summary="review",
        findings=[{"k": "v"}],
        evidence={"a": 1},
    )
    assert r.findings[0]["k"] == "v"
    assert r.evidence["a"] == 1
    # immutable-ish
    assert isinstance(r.findings, tuple)


def test_verifier_implements_interface():
    v = DummyVerifier()
    assert isinstance(v, VerificationBackend) or hasattr(v, "verify")


def test_verifiers_substitutable():
    v1 = DummyVerifier()
    v2 = AnotherVerifier()
    assert hasattr(v1, "verify")
    assert hasattr(v2, "verify")
    r1 = v1.verify({}, {})
    r2 = v2.verify({}, {})
    assert r1.verdict != r2.verdict


def test_verifier_does_not_mutate_state(tmp_path):
    root = make_project(tmp_path, [{"id": "t1", "status": "planned"}])
    m = Master(root)
    before = m.status("p")
    v = DummyVerifier()
    task = before["tasks"][0]
    ctx = {"project_id": "p"}
    v.verify(task, ctx, evidence={})
    after = m.status("p")
    assert before == after
