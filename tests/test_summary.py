"""The morning summary: done, blocked, screenshots, cost and what needs the owner."""

import io
from datetime import datetime, timezone

import yaml

from core.history import EventType, InMemoryHistoryStore
from core.master import Master
from core.ms import main as ms_main
from core.paths import RuntimePaths
from core.summary import build_summary, headline, render_html, render_text, write_summary



def project(tmp_path):
    root = tmp_path / "projects"
    (root / "app").mkdir(parents=True)
    (root / "app" / "project.yaml").write_text(yaml.safe_dump(
        {"id": "app", "name": "App", "status": "active"}))
    (root / "app" / "milestones.yaml").write_text("milestones:\n  - {id: m1, name: M, status: planned}\n")
    (root / "app" / "tasks.yaml").write_text(yaml.safe_dump({"tasks": [
        {"id": "t-1", "milestone": "m1", "title": "Tiles", "status": "completed",
         "manual_check": "1. Open the preview.", "acceptance": {"commands": ["true"]}},
        {"id": "t-2", "milestone": "m1", "title": "Avatars", "status": "blocked",
         "acceptance": {"commands": ["true"]}}]}))
    return Master(root)


def test_the_summary_collects_done_blocked_screenshots_cost_and_needs(tmp_path):
    master = project(tmp_path)
    history = InMemoryHistoryStore()
    history.append(type=EventType.INTEGRATION, run_id="r", project_id="app", task_id="t-1",
                   attempt_id="a1", payload={"result_sha": "abc"})
    shot = tmp_path / "home.png"
    shot.write_bytes(b"png")
    history.append(type=EventType.VERIFICATION, run_id="r", project_id="app", task_id="t-1",
                   attempt_id="a1", payload={"verdict": "pass", "evidence": {"visual_review": {
                       "status": "reviewed", "verdict": "ok", "notes": "Readable.",
                       "screenshots": [str(shot)], "reasoner": "deepseek:deepseek-flash",
                       "usage": {"input_tokens": 10000, "output_tokens": 1000}}}})
    summary = build_summary(master, history, "2000-01-01T00:00:00+00:00",
                            {"deepseek-flash": {"input": 0.30, "output": 1.20}},
                            needs_you=lambda p: ["t-3: pick an image"], reason="test",
                            now=datetime(2026, 10, 7, 6, tzinfo=timezone.utc))
    [p] = summary["projects"]
    assert [t["id"] for t in p["done"]] == ["t-1"]
    assert [t["id"] for t in p["blocked"]] == ["t-2"]
    assert p["reviews"][0]["screenshots"] == [str(shot)]
    assert headline(summary) == "1 done, 1 blocked, 1 need you, 1 screenshots. Spent $0.004."
    text = render_text(summary)
    assert "check: 1. Open the preview." in text and "reviewer: Readable." in text
    assert "reviewer $0.004" in text and "- t-3: pick an image" in text
    page = render_html(summary)
    assert f'<img src="{shot.as_uri()}"' in page and "Needs you" in page
    paths = write_summary(summary, tmp_path / "reports")
    assert paths["html"].read_text() == (tmp_path / "reports" / "latest.html").read_text()


def test_only_events_since_the_start_count(tmp_path):
    master = project(tmp_path)
    history = InMemoryHistoryStore()
    history.append(type=EventType.INTEGRATION, run_id="r", project_id="app", task_id="t-1",
                   attempt_id="a1", payload={})
    summary = build_summary(master, history, "2999-01-01T00:00:00+00:00", {})
    assert summary["projects"][0]["done"] == []


def test_ms_report_summary_prints_the_latest():
    out = io.StringIO()
    assert ms_main(["report", "--summary"], out=out) == 0
    assert "No summary yet" in out.getvalue()
    reports = RuntimePaths.default().state_dir / "reports"
    reports.mkdir(parents=True)
    (reports / "latest.txt").write_text("Summary since …\n")
    out = io.StringIO()
    ms_main(["report", "--summary"], out=out)
    assert out.getvalue().startswith("Summary since") and "latest.html" in out.getvalue()
