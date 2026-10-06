"""The morning summary: what the service did since it started working, in one place.

When the service runs out of work (the backlog is done or waits for the owner)
or reaches the daily cap, after having worked since the last summary, it writes
one summary and sends one notification. The summary covers, per project:

* tasks done (integrated) with their manual checks;
* tasks blocked or out of attempts;
* the visual reviewer's screenshots and verdicts, per task;
* the cost, by kind (Master and planner, workers, reviewer);
* what needs the owner (``needs_you``, supplied by the caller: image picks,
  pending lessons, a release to make, ...).

It is written as text and as an HTML page with screenshot thumbnails to
``<state dir>/reports/`` (``latest.txt`` and ``latest.html`` are the newest);
``ms report --summary`` prints it.
"""

from __future__ import annotations

import html
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Optional

from core.history import EventType

__all__ = ["build_summary", "render_text", "render_html", "write_summary", "headline"]


def build_summary(master, history, since_iso: str, prices: Mapping,
                  default_model: Optional[str] = None,
                  needs_you: Callable[[str], list] = lambda project_id: [],
                  max_failures: int = 3, reason: str = "",
                  now: Optional[datetime] = None, project_ids=None) -> dict:
    from core.daemon import work_for
    from core.report import spend_since

    projects = []
    for project_id in (master.list_projects() if project_ids is None else project_ids):
        status = master.status(project_id)
        tasks = {t["id"]: t for t in status["tasks"]}
        integrated = []
        for e in history.events(project_id=project_id, types=[EventType.INTEGRATION]):
            if e.created_at >= since_iso and e.task_id in tasks \
                    and e.task_id not in integrated:
                integrated.append(e.task_id)
        done = [t for t in integrated if tasks[t].get("status") == "completed"]
        _, exhausted = work_for(status, history, max_failures)
        blocked = [t for t in tasks if tasks[t].get("status") == "blocked"]
        reviews = {}
        for e in history.events(project_id=project_id, types=[EventType.VERIFICATION]):
            review = (e.payload.get("evidence") or {}).get("visual_review") or {}
            if e.created_at >= since_iso and review.get("screenshots"):
                reviews[e.task_id] = {"verdict": review.get("verdict") or review.get("status"),
                                      "notes": review.get("notes") or review.get("note") or "",
                                      "screenshots": list(review["screenshots"])}
        projects.append({
            "project_id": project_id, "name": status.get("name") or project_id,
            "done": [{"id": t, "title": tasks[t].get("title"),
                      "manual_check": tasks[t].get("manual_check")} for t in done],
            "blocked": [{"id": t, "title": tasks[t].get("title")} for t in blocked],
            "exhausted": [{"id": t, "title": tasks[t].get("title")} for t in exhausted],
            "reviews": [{"id": t, "title": tasks.get(t, {}).get("title"), **r}
                        for t, r in reviews.items()],
            "needs_you": list(needs_you(project_id) or []),
        })
    spend = spend_since(history, since_iso, prices, default_model)
    return {"since": since_iso, "at": (now or datetime.now(timezone.utc)).isoformat(),
            "reason": reason, "projects": projects, "spend": spend}


def headline(summary: Mapping) -> str:
    done = sum(len(p["done"]) for p in summary["projects"])
    stuck = sum(len(p["blocked"]) + len(p["exhausted"]) for p in summary["projects"])
    needs = sum(len(p["needs_you"]) for p in summary["projects"])
    shots = sum(len(r["screenshots"]) for p in summary["projects"] for r in p["reviews"])
    parts = [f"{done} done", f"{stuck} blocked", f"{needs} need you"]
    if shots:
        parts.append(f"{shots} screenshots")
    return ", ".join(parts) + f". Spent ${summary['spend']['total_usd']:.3f}."


def _local(iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone().strftime("%a %d %b %H:%M")


def render_text(summary: Mapping) -> str:
    spend = summary["spend"]
    lines = [f"Summary since {_local(summary['since'])}: {headline(summary)}"]
    if summary.get("reason"):
        lines.append(f"The service stopped because {summary['reason']}.")
    for p in summary["projects"]:
        lines.append(f"\n{p['name']}")
        lines.append("  Done:" if p["done"] else "  Done: nothing")
        for t in p["done"]:
            lines.append(f"    - {t['id']} {t['title']}")
            if t.get("manual_check"):
                lines.append("      check: " + " ".join(str(t["manual_check"]).split()))
        for label, items in (("Blocked", p["blocked"]), ("Out of attempts", p["exhausted"])):
            if items:
                lines.append(f"  {label}: " + "; ".join(f"{t['id']} {t['title']}"
                                                        for t in items))
        for r in p["reviews"]:
            lines.append(f"  Screenshots of {r['id']} (review: {r['verdict']}): "
                         + ", ".join(r["screenshots"]))
            if r.get("notes"):
                lines.append(f"    reviewer: {r['notes']}")
        if p["needs_you"]:
            lines.append("  Needs you:")
            lines += [f"    - {item}" for item in p["needs_you"]]
    lines.append(f"\nCost: ${spend['total_usd']:.3f} (Master and planner "
                 f"${spend['master_usd']:.3f}, workers ${spend['worker_usd']:.3f}, reviewer "
                 f"${spend.get('reviewer_usd', 0):.3f}).")
    return "\n".join(lines)


def render_html(summary: Mapping) -> str:
    e = html.escape
    body = [f"<h1>Summary since {e(_local(summary['since']))}</h1>",
            f"<p class=lead>{e(headline(summary))}</p>"]
    if summary.get("reason"):
        body.append(f"<p>The service stopped because {e(summary['reason'])}.</p>")
    for p in summary["projects"]:
        body.append(f"<h2>{e(p['name'])}</h2>")
        if p["needs_you"]:
            body.append("<h3>Needs you</h3><ul>" + "".join(
                f"<li>{e(i)}</li>" for i in p["needs_you"]) + "</ul>")
        body.append("<h3>Done</h3>" + ("<ul>" + "".join(
            f"<li><b>{e(t['id'])}</b> {e(t['title'] or '')}"
            + (f"<br><small>{e(' '.join(str(t['manual_check']).split()))}</small>"
               if t.get("manual_check") else "") + "</li>" for t in p["done"]) + "</ul>"
            if p["done"] else "<p>Nothing.</p>"))
        stuck = p["blocked"] + p["exhausted"]
        if stuck:
            body.append("<h3>Blocked</h3><ul>" + "".join(
                f"<li>{e(t['id'])} {e(t['title'] or '')}</li>" for t in stuck) + "</ul>")
        for r in p["reviews"]:
            shots = "".join(
                f'<a href="{e(Path(s).as_uri())}"><img src="{e(Path(s).as_uri())}" '
                f'alt="{e(Path(s).stem)}"></a>' for s in r["screenshots"])
            body.append(f"<h3>{e(r['id'])} {e(r['title'] or '')} — review: "
                        f"{e(str(r['verdict']))}</h3><p>{e(r.get('notes') or '')}</p>"
                        f"<div class=shots>{shots}</div>")
    spend = summary["spend"]
    body.append(f"<p>Cost: ${spend['total_usd']:.3f} (Master and planner "
                f"${spend['master_usd']:.3f}, workers ${spend['worker_usd']:.3f}, reviewer "
                f"${spend.get('reviewer_usd', 0):.3f}).</p>")
    return ("<!doctype html><meta charset=utf-8><meta name=viewport "
            "content='width=device-width,initial-scale=1'><title>Summary</title><style>"
            "body{font-family:system-ui,sans-serif;max-width:960px;margin:1em auto;padding:0 16px}"
            ".lead{font-size:1.2em}.shots img{width:180px;margin:4px;border:1px solid #ccc;"
            "border-radius:8px}</style>" + "\n".join(body))


def write_summary(summary: Mapping, reports_dir: Path) -> dict:
    """Write text and HTML; returns their paths (and refreshes latest.*)."""
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromisoformat(summary["at"]).astimezone().strftime("%Y%m%d-%H%M")
    text, page = render_text(summary), render_html(summary)
    paths = {"text": reports_dir / f"summary-{stamp}.txt",
             "html": reports_dir / f"summary-{stamp}.html"}
    paths["text"].write_text(text + "\n")
    paths["html"].write_text(page)
    (reports_dir / "latest.txt").write_text(text + "\n")
    (reports_dir / "latest.html").write_text(page)
    return paths
