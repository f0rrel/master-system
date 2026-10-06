"""The visual reviewer: a vision model looks at screenshots of a visual task's result.

It wraps the acceptance verifier. Only for a task of type ``visual`` whose
acceptance **passed**, on the attempt's own committed result (not on a rebase
re-verification), it:

1. captures screenshots of the relevant screens in the attempt's worktree, with
   the worker environment (no secrets) and a phone-sized viewport;
2. stores them as task artifacts in the state directory;
3. asks a vision-capable model to judge them against the task and the project's
   direction: readability, whether the change shows, whether it fits the style.

The reviewer can turn ``pass`` into ``fail`` ("block"), never the reverse. When
it cannot review (no screens configured, capture failed, unusable answer), it
records why and leaves the verdict alone.

project.yaml::

    visual_review:
      viewport: [390, 844]
      site_dir: www                    # default: github.site_dir
      screens:
        - {name: home, url: index.html}
        - {name: game, url: "index.html?screen=game"}
      capture: "node tests/screens/capture.js {out_dir} {screens} {width} {height}"
      max_screens: 4

Without ``capture``, each screen is captured with ``npx playwright screenshot``
from ``file://<worktree>/<site_dir>/<url>``. With it, the project's own script
navigates the app and writes ``<name>.png`` files into ``{out_dir}``. A task may
name its ``screens``; otherwise all configured screens are captured.
"""

from __future__ import annotations

import json
import shlex
import shutil
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Mapping, Optional

from core.verification import VerificationResult

__all__ = ["VisualReviewVerifier", "review_settings", "review_prompt", "parse_review"]

DEFAULT_VIEWPORT = (390, 844)
CAPTURE_TIMEOUT_S = 180


def review_settings(project: Mapping) -> Optional[dict]:
    """The project's visual review settings, or None when it has no screens."""
    raw = project.get("visual_review") if isinstance(project.get("visual_review"), dict) else None
    if not raw or not raw.get("screens"):
        return None
    viewport = raw.get("viewport") or DEFAULT_VIEWPORT
    github = project.get("github") if isinstance(project.get("github"), dict) else {}
    screens = [{"name": str(s.get("name")), "url": str(s.get("url") or "index.html")}
               for s in raw["screens"] if isinstance(s, dict) and s.get("name")]
    return {"width": int(viewport[0]), "height": int(viewport[1]),
            "site_dir": str(raw.get("site_dir") or github.get("site_dir") or "."),
            "screens": screens, "capture": raw.get("capture"),
            "max_screens": int(raw.get("max_screens") or 4)}


def review_prompt(project_name: str, task: Mapping, direction: Optional[str],
                  screens: list, width: int, height: int) -> str:
    parts = [
        f'You review a visual change in the software project "{project_name}" from '
        f"screenshots taken on a phone-sized screen ({width}x{height}). The screenshots are, "
        f"in order: {', '.join(screens)}.",
        f"TASK: {task.get('title')}\n{task.get('description') or ''}".strip(),
    ]
    if task.get("manual_check"):
        parts.append(f"HOW THE OWNER CHECKS IT BY HAND:\n{task['manual_check']}")
    if direction:
        parts.append(f"PROJECT DIRECTION:\n{direction}")
    parts.append(
        "Judge three things:\n"
        "1. readability: are text and elements legible on a phone, with enough contrast, and "
        "distinguishable from each other?\n"
        "2. change_visible: does the change the task describes show in these screenshots? "
        "Answer \"unclear\" if these screens cannot show it.\n"
        "3. fits_style: does it fit the look and feel in the direction?\n"
        "Block only for clear problems: something unreadable, the change plainly missing from "
        "a screen that should show it, or a clear clash with the direction. Otherwise answer "
        "ok; minor taste differences are not a reason to block.\n"
        'Answer with ONE JSON object: {"verdict": "ok" or "block", "readability": "ok" or '
        '"problem", "change_visible": "yes", "no" or "unclear", "fits_style": "ok" or '
        '"problem", "notes": "one to three short sentences for the developer"}')
    return "\n\n".join(parts)


def parse_review(text: str) -> Optional[dict]:
    """The model's JSON answer, normalised; None when it is unusable."""
    text = str(text or "")
    try:
        value = json.loads(text[text.find("{"):text.rfind("}") + 1])
    except ValueError:
        return None
    if not isinstance(value, dict) or value.get("verdict") not in ("ok", "block"):
        return None
    return {"verdict": value["verdict"],
            "readability": str(value.get("readability") or ""),
            "change_visible": str(value.get("change_visible") or ""),
            "fits_style": str(value.get("fits_style") or ""),
            "notes": " ".join(str(value.get("notes") or "").split())[:600]}


class CaptureError(RuntimeError):
    pass


class VisualReviewVerifier:
    """VerificationBackend: the inner verifier, then (visual tasks) the reviewer."""

    def __init__(self, inner, master, judge, artifacts_root: Path, prices=None,
                 direction=None, reviewer_label: str = "reviewer"):
        self._inner = inner
        self._master = master
        #: An object with ``judge(prompt, images) -> str`` and ``last_usage``.
        self._judge = judge
        self._artifacts_root = Path(artifacts_root)
        self._prices = prices or {}
        #: project dict -> direction text or None (core.direction); injectable.
        self._direction = direction
        self._label = reviewer_label

    def verify(self, task, context, evidence=None, *, workspace) -> VerificationResult:
        result = self._inner.verify(task, context, evidence, workspace=workspace)
        if (result.verdict != "pass" or task.get("type") != "visual"
                or (evidence or {}).get("rebased_from")):
            return result
        project = self._master.project_state(context["project_id"]).project()
        settings = review_settings(project)
        if settings is None:
            return self._with(result, {"status": "skipped",
                                       "note": "the project has no visual_review screens"})
        out_dir = (self._artifacts_root / context["project_id"] / str(task.get("id"))
                   / str(workspace.result_sha or "unknown")[:12])
        try:
            shots = self._capture(workspace, settings, task, out_dir)
        except CaptureError as error:
            return self._with(result, {"status": "unavailable", "note": str(error)})
        prompt = review_prompt(project.get("name") or context["project_id"], task,
                               self._direction_text(project), [n for n, _ in shots],
                               settings["width"], settings["height"])
        try:
            answer = self._judge.judge(prompt, [path.read_bytes() for _, path in shots])
        except Exception as error:  # a reviewer outage must not block or pass anything
            return self._with(result, {"status": "unavailable", "screenshots": _names(shots),
                                       "note": f"the reviewer failed: {error}"})
        usage = getattr(self._judge, "last_usage", None) or {}
        review = parse_review(answer)
        record = {"status": "reviewed" if review else "unavailable",
                  "screenshots": _names(shots), "reasoner": self._label, "usage": usage,
                  "cost_usd": self._cost(usage)}
        if review is None:
            record["note"] = "the reviewer's answer was unusable"
            return self._with(result, record)
        record.update(review)
        if review["verdict"] != "block":
            return self._with(result, record)
        return VerificationResult(
            verdict="fail",
            summary=f"{result.summary}; the visual review blocked it: {review['notes']}",
            findings=[*result.findings, {"kind": "visual_review", "notes": review["notes"],
                                         "readability": review["readability"],
                                         "change_visible": review["change_visible"],
                                         "fits_style": review["fits_style"]}],
            evidence={**result.evidence, "visual_review": record},
        )

    # --- helpers ---

    def _direction_text(self, project):
        if self._direction is not None:
            return self._direction(project)
        from core.direction import read_direction

        direction = read_direction(project)
        return direction.text if direction else None

    def _cost(self, usage) -> Optional[float]:
        from core.report import _price, add_usage

        model = self._label.split(":", 1)[-1]
        return _price(add_usage([usage]), model, self._prices) if usage else None

    @staticmethod
    def _with(result, record) -> VerificationResult:
        return replace(result, evidence={**result.evidence, "visual_review": record})

    def _capture(self, workspace, settings, task, out_dir: Path) -> list:
        wanted = [str(s) for s in task.get("screens") or []]
        screens = [s for s in settings["screens"] if not wanted or s["name"] in wanted]
        screens = screens[:settings["max_screens"]]
        if not screens:
            raise CaptureError(f"none of the task's screens {wanted} is configured")
        with tempfile.TemporaryDirectory(prefix="ms-shots-") as tmp:
            tmp = Path(tmp)
            if settings["capture"]:
                command = settings["capture"].format(
                    out_dir=shlex.quote(str(tmp)),
                    screens=shlex.quote(",".join(s["name"] for s in screens)),
                    width=settings["width"], height=settings["height"])
                self._run(workspace, command)
            else:
                site = Path(workspace.path) / settings["site_dir"]
                for screen in screens:
                    url = (site / screen["url"]).as_uri() if "?" not in screen["url"] else \
                        (site.as_uri() + "/" + screen["url"])
                    self._run(workspace, "npx playwright screenshot --viewport-size="
                              f"{settings['width']},{settings['height']} "
                              f"--wait-for-timeout=800 {shlex.quote(url)} "
                              f"{shlex.quote(str(tmp / (screen['name'] + '.png')))}")
            out_dir.mkdir(parents=True, exist_ok=True)
            shots = []
            for screen in screens:
                source = tmp / f"{screen['name']}.png"
                if source.is_file() and source.stat().st_size > 0:
                    target = out_dir / source.name
                    shutil.copyfile(source, target)
                    shots.append((screen["name"], target))
        if not shots:
            raise CaptureError("the capture produced no screenshots")
        return shots

    @staticmethod
    def _run(workspace, command):
        outcome = workspace.run(["/bin/sh", "-c", command], timeout=CAPTURE_TIMEOUT_S)
        if outcome.timed_out or outcome.returncode != 0:
            tail = ((outcome.stdout or "") + (outcome.stderr or "")).strip()[-300:]
            raise CaptureError(f"screenshot capture failed (exit {outcome.returncode}): {tail}")


def _names(shots) -> list:
    return [str(path) for _, path in shots]
