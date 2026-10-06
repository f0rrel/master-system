"""Generated images for visual tasks, made by the orchestrator (workers hold no keys).

A visual task may declare the images it needs (part of its spec)::

    assets:
      - name: robot-portrait
        prompt: "a friendly round robot waving, front view"
        path: public/assets/avatars/robot.png
        candidates: 3          # 1: committed directly; 2-4: the owner picks one

Before such a task can run, its images must exist on the base branch. The
service generates the candidates with the project's fixed style
(project.yaml ``images.style``, put in front of every prompt) and either commits
the single candidate, or stores the candidates with a contact sheet in the state
directory and waits: the task is "waiting for you" until the owner runs
``ms pick <project> <task> <asset> <n>``, which commits the chosen image
(recorded as a human action).

Providers, tried in order ([images] providers): Pollinations
(``POLLINATIONS_API_KEY``) and Cloudflare Workers AI (``CLOUDFLARE_ACCOUNT_ID``,
``CLOUDFLARE_API_TOKEN``), keys from master.env. No scraping.
"""

from __future__ import annotations

import base64
import html
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Optional

from core.run_config import ImagesSettings


__all__ = ["ImageError", "PollinationsImages", "CloudflareImages", "build_image_providers",
           "generate", "task_assets", "asset_problems", "missing_assets", "waiting_tasks",
           "candidate_dir", "candidates", "commit_asset", "AssetStep", "MAX_CANDIDATES"]

MAX_CANDIDATES = 4
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")
IDENTITY = {"GIT_AUTHOR_NAME": "Master System images",
            "GIT_AUTHOR_EMAIL": "master-system@localhost",
            "GIT_COMMITTER_NAME": "Master System images",
            "GIT_COMMITTER_EMAIL": "master-system@localhost"}


class ImageError(RuntimeError):
    pass


# --- providers (adapters) -------------------------------------------------------------------


class PollinationsImages:
    name = "pollinations"

    def __init__(self, api_key: str, model: str = ImagesSettings.pollinations_model,
                 timeout: float = 120,
                 opener=urllib.request.urlopen):
        self._key, self.model, self._timeout, self._open = api_key, model, timeout, opener

    def generate(self, prompt: str, width: int, height: int, seed: int) -> bytes:
        query = urllib.parse.urlencode({"model": self.model, "width": width, "height": height,
                                        "seed": seed, "nologo": "true"})
        url = f"https://gen.pollinations.ai/image/{urllib.parse.quote(prompt)}?{query}"
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {self._key}"})
        return _image_bytes(_fetch(self._open, request, self._timeout, self.name))


class CloudflareImages:
    name = "cloudflare"

    def __init__(self, account_id: str, api_token: str,
                 model: str = ImagesSettings.cloudflare_model, timeout: float = 120,
                 opener=urllib.request.urlopen):
        self._account, self._token, self.model = account_id, api_token, model
        self._timeout, self._open = timeout, opener

    def generate(self, prompt: str, width: int, height: int, seed: int) -> bytes:
        url = (f"https://api.cloudflare.com/client/v4/accounts/{self._account}/ai/run/"
               f"{self.model}")
        body = json.dumps({"prompt": prompt, "steps": 4, "seed": seed, "width": width,
                           "height": height}).encode()
        request = urllib.request.Request(url, data=body, method="POST", headers={
            "Authorization": f"Bearer {self._token}", "Content-Type": "application/json"})
        raw = _fetch(self._open, request, self._timeout, self.name)
        try:
            image = json.loads(raw)["result"]["image"]
            return _image_bytes(base64.b64decode(image))
        except (ValueError, KeyError, TypeError) as error:
            if raw[:4] in (b"\x89PNG", b"\xff\xd8\xff\xe0", b"\xff\xd8\xff\xe1"):
                return raw
            raise ImageError(f"cloudflare returned no image ({error})") from error


def _fetch(opener, request, timeout, name) -> bytes:
    try:
        with opener(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        raise ImageError(f"{name}: HTTP {error.code}") from error
    except (urllib.error.URLError, OSError) as error:
        raise ImageError(f"{name}: {error}") from error


def _image_bytes(data: bytes) -> bytes:
    if data[:4] == b"\x89PNG" or data[:3] == b"\xff\xd8\xff" or data[8:12] == b"WEBP":
        return data
    raise ImageError("the provider did not return an image")


def build_image_providers(settings, env: Mapping[str, str]) -> list:
    """Configured providers whose keys are present, in order."""
    providers = []
    for name in settings.providers:
        if name == "pollinations" and env.get("POLLINATIONS_API_KEY"):
            providers.append(PollinationsImages(env["POLLINATIONS_API_KEY"],
                                                settings.pollinations_model, settings.timeout_s))
        elif (name == "cloudflare" and env.get("CLOUDFLARE_ACCOUNT_ID")
              and env.get("CLOUDFLARE_API_TOKEN")):
            providers.append(CloudflareImages(env["CLOUDFLARE_ACCOUNT_ID"],
                                              env["CLOUDFLARE_API_TOKEN"],
                                              settings.cloudflare_model, settings.timeout_s))
    return providers


def generate(providers, prompt, width, height, seed) -> tuple:
    """(image bytes, provider name) from the first provider that succeeds."""
    if not providers:
        raise ImageError("no image provider is set up: add POLLINATIONS_API_KEY (or "
                         "CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN) to master.env")
    errors = []
    for provider in providers:
        try:
            return provider.generate(prompt, width, height, seed), provider.name
        except ImageError as error:
            errors.append(str(error))
    raise ImageError("; ".join(errors))


# --- assets on tasks ------------------------------------------------------------------------


def task_assets(task: Mapping) -> list:
    return [a for a in task.get("assets") or [] if isinstance(a, dict)]


def asset_problems(assets, where: str = "task") -> list:
    """Why a task's ``assets`` list is malformed."""
    if assets in (None, []):
        return []
    if not isinstance(assets, list):
        return [f"{where}: assets must be a list"]
    problems, names = [], set()
    for asset in assets:
        if not isinstance(asset, dict):
            problems.append(f"{where}: an asset is not a mapping")
            continue
        name = str(asset.get("name", ""))
        if not NAME.match(name) or name in names:
            problems.append(f"{where}: asset name {name!r} is invalid or repeated")
        names.add(name)
        prompt = asset.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 1000:
            problems.append(f"{where}: asset {name} needs a prompt of at most 1000 characters")
        path = str(asset.get("path", ""))
        if (not path or path.startswith("/") or ".." in Path(path).parts
                or not path.lower().endswith(SUFFIXES)):
            problems.append(f"{where}: asset {name} needs a relative image path "
                            f"({', '.join(SUFFIXES)})")
        count = asset.get("candidates", 1)
        if not isinstance(count, int) or isinstance(count, bool) \
                or not 1 <= count <= MAX_CANDIDATES:
            problems.append(f"{where}: asset {name}: candidates must be 1 to {MAX_CANDIDATES}")
    return problems


def missing_assets(project: Mapping, task: Mapping, git: Optional[Callable] = None) -> list:
    """The task's assets not yet on the project's base branch."""
    assets = task_assets(task)
    if not assets:
        return []
    if git is None:
        from core.host import git
    repo = Path(project["repository"]).expanduser()
    missing = []
    for asset in assets:
        try:
            git(["cat-file", "-e", f"refs/heads/{project['base_branch']}:{asset['path']}"], repo)
        except RuntimeError:
            missing.append(asset)
    return missing


def waiting_tasks(master, project_id: str, git: Optional[Callable] = None) -> dict:
    """task id -> why it waits for images (open tasks only)."""
    project = master.project_state(project_id).project()
    waiting = {}
    for task in master.status(project_id)["tasks"]:
        if task.get("status") not in ("planned", "in_progress") or not task_assets(task):
            continue
        missing = missing_assets(project, task, git)
        if missing:
            waiting[task["id"]] = "waiting for images: " + ", ".join(a["name"] for a in missing)
    return waiting


def candidate_dir(state_dir: Path, project_id: str, task_id: str, asset_name: str) -> Path:
    return Path(state_dir) / "assets" / project_id / task_id / asset_name


def candidates(state_dir, project_id, task_id, asset_name) -> list:
    folder = candidate_dir(state_dir, project_id, task_id, asset_name)
    return sorted(folder.glob("candidate-*.png"))


def _contact_sheet(folder: Path, task: Mapping, asset: Mapping, files: list) -> Path:
    cells = "".join(
        f'<figure><img src="{html.escape(p.name)}" alt="candidate {n}">'
        f"<figcaption>{n}</figcaption></figure>" for n, p in enumerate(files, 1))
    page = (f"<!doctype html><meta charset=utf-8><title>{html.escape(asset['name'])}</title>"
            "<style>body{font-family:sans-serif;background:#222;color:#eee}"
            "img{width:280px;border-radius:12px}figure{display:inline-block;margin:8px;"
            "text-align:center}figcaption{font-size:28px}</style>"
            f"<h1>{html.escape(str(task.get('id')))}: {html.escape(asset['name'])}</h1>"
            f"<p>{html.escape(asset['prompt'])}</p>{cells}"
            f"<p>Pick one: <code>ms pick &lt;project&gt; {html.escape(str(task.get('id')))} "
            f"{html.escape(asset['name'])} &lt;n&gt;</code></p>")
    sheet = folder / "index.html"
    sheet.write_text(page)
    return sheet


def commit_asset(master, project_id: str, task: Mapping, asset: Mapping, image: bytes,
                 worktrees, git: Optional[Callable] = None) -> str:
    """Commit one image to the base branch at the asset's path. Caller holds the lock."""
    from core.workspace import branch_tip, fast_forward

    if git is None:
        from core.host import git
    project = master.project_state(project_id).project()
    repo = Path(project["repository"]).expanduser()
    branch = project["base_branch"]
    tip = branch_tip(repo, branch)
    name = f"asset-{uuid.uuid4().hex[:12]}"
    path = worktrees.free_path(project_id, name)
    worktrees.create(repo, path, name, tip, branch=f"asset/{path.name}")
    target = path / asset["path"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(image)
    git(["add", "--", asset["path"]], path)
    git(["commit", "-q", "-m", f"{task['id']}: image {asset['name']}\n\n{asset['prompt']}",
         "--", asset["path"]], path, extra_env=IDENTITY)
    commit = git(["rev-parse", "HEAD"], path)
    fast_forward(repo, branch, tip, commit)
    return commit


# --- the service's step ---------------------------------------------------------------------


class AssetStep:
    """Each service cycle: generate missing images; commit single ones; wait for picks."""

    def __init__(self, master, state_dir: Path, providers, worktrees, git=None, lock=None,
                 now=lambda: datetime.now(timezone.utc)):
        self._master = master
        self._state_dir = Path(state_dir)
        self._providers = providers
        self._worktrees = worktrees
        self._git = git
        #: project_path -> context manager holding the project lock (non-blocking).
        self._lock = lock
        self._now = now

    def __call__(self, project_id: str) -> list:
        project = self._master.project_state(project_id).project()
        settings = project.get("images") if isinstance(project.get("images"), dict) else {}
        style = str(settings.get("style") or "").strip()
        width, height = int(settings.get("width") or 768), int(settings.get("height") or 768)
        lines = []
        for task in self._master.status(project_id)["tasks"]:
            if task.get("status") not in ("planned", "in_progress"):
                continue
            for asset in missing_assets(project, task, self._git):
                folder = candidate_dir(self._state_dir, project_id, task["id"], asset["name"])
                count = int(asset.get("candidates", 1))
                existing = candidates(self._state_dir, project_id, task["id"], asset["name"])
                if len(existing) < count:
                    try:
                        existing = self._generate(folder, task, asset, style, width, height,
                                                  count, len(existing))
                    except ImageError as error:
                        lines.append(f"{task['id']} {asset['name']}: image generation failed: "
                                     f"{error}")
                        continue
                    if count > 1:
                        lines.append(f"{task['id']}: {count} images to pick from for "
                                     f"{asset['name']} (ms status)")
                if count == 1:
                    lines += self._commit_single(project_id, task, asset, existing[0])
        return lines

    def _generate(self, folder, task, asset, style, width, height, count, have) -> list:
        folder.mkdir(parents=True, exist_ok=True)
        prompt = f"{style}. {asset['prompt']}" if style else asset["prompt"]
        meta = {"task_id": task["id"], "asset": asset["name"], "prompt": prompt,
                "path": asset["path"], "candidates": []}
        for n in range(have + 1, count + 1):
            seed = int(uuid.uuid4().int % 2_000_000_000)
            image, provider = generate(self._providers, prompt, width, height, seed)
            (folder / f"candidate-{n}.png").write_bytes(image)
            meta["candidates"].append({"n": n, "seed": seed, "provider": provider,
                                       "at": self._now().isoformat(timespec="seconds")})
        (folder / "meta.json").write_text(json.dumps(meta, indent=2))
        files = sorted(folder.glob("candidate-*.png"))
        _contact_sheet(folder, task, asset, files)
        return files

    def _commit_single(self, project_id, task, asset, image_path) -> list:
        state = self._master.project_state(project_id)
        if self._lock is None:
            from core.run_lock import ProjectLock

            lock = ProjectLock(state.project_path, holder="images")
        else:
            lock = self._lock(state.project_path)
        from core.run_lock import ProjectBusyError

        try:
            with lock:
                commit = commit_asset(self._master, project_id, task, asset,
                                      image_path.read_bytes(), self._worktrees, self._git)
        except ProjectBusyError:
            return []
        return [f"{task['id']}: image {asset['name']} committed ({commit[:12]})"]
