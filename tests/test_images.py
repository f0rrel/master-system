"""Images for visual tasks: providers, candidates, picks, and tasks that wait for them."""

import io
import json
import urllib.error

import pytest
import yaml

from conftest import git, make_repo
from core.images import (AssetStep, CloudflareImages, ImageError, PollinationsImages,
                         build_image_providers, candidates, generate, missing_assets,
                         waiting_tasks)
from core.master import Master
from core.ms import main as ms_main
from core.paths import RuntimePaths, default_projects_root
from core.run_config import ImagesSettings
from core.task_orchestrator import default_worktrees
from core.daemon import work_for

def blob(repo, spec):
    import subprocess

    return subprocess.run(["git", "show", spec], cwd=repo, capture_output=True,
                          check=True).stdout


PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 20


class Response:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.body


def test_pollinations_sends_the_key_and_returns_the_image():
    seen = []

    def opener(request, timeout):
        seen.append(request)
        return Response(PNG)

    image = PollinationsImages("sk_test", opener=opener).generate("a blob", 512, 512, 7)
    assert image == PNG
    [request] = seen
    assert request.full_url.startswith("https://gen.pollinations.ai/image/a%20blob?")
    assert "model=zimage" in request.full_url and "seed=7" in request.full_url
    assert request.get_header("Authorization") == "Bearer sk_test"


def test_cloudflare_decodes_base64_and_errors_are_image_errors():
    import base64

    body = json.dumps({"result": {"image": base64.b64encode(PNG).decode()}}).encode()
    assert CloudflareImages("acc", "tok", opener=lambda r, timeout: Response(body)) \
        .generate("x", 512, 512, 1) == PNG

    def refuse(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 401, "no", {}, io.BytesIO(b""))

    with pytest.raises(ImageError, match="HTTP 401"):
        PollinationsImages("bad", opener=refuse).generate("x", 1, 1, 1)
    with pytest.raises(ImageError, match="did not return an image"):
        PollinationsImages("k", opener=lambda r, timeout: Response(b"<html>")).generate(
            "x", 1, 1, 1)


class Fake:
    def __init__(self, name, fail=False):
        self.name, self.fail, self.prompts = name, fail, []

    def generate(self, prompt, width, height, seed):
        self.prompts.append(prompt)
        if self.fail:
            raise ImageError(f"{self.name}: down")
        return PNG


def test_providers_fall_back_in_order_and_need_keys():
    first, second = Fake("pollinations", fail=True), Fake("cloudflare")
    assert generate([first, second], "p", 1, 1, 1) == (PNG, "cloudflare")
    with pytest.raises(ImageError, match="no image provider is set up"):
        generate([], "p", 1, 1, 1)
    settings = ImagesSettings()
    assert build_image_providers(settings, {}) == []
    names = [p.name for p in build_image_providers(settings, {
        "POLLINATIONS_API_KEY": "k", "CLOUDFLARE_ACCOUNT_ID": "a", "CLOUDFLARE_API_TOKEN": "t"})]
    assert names == ["pollinations", "cloudflare"]


def asset(name, count):
    return {"name": name, "prompt": f"a {name} icon", "path": f"www/assets/{name}.png",
            "candidates": count}


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "projects"
    (root / "app").mkdir(parents=True)
    repo = make_repo(tmp_path / "repo", {"www/index.html": "x\n"})
    git(repo, "branch", "develop")
    (root / "app" / "project.yaml").write_text(yaml.safe_dump({
        "id": "app", "name": "App", "status": "active", "repository": str(repo),
        "base_branch": "develop", "auto_integrate": True,
        "images": {"style": "cute cartoon, bright colours", "width": 512, "height": 512}}))
    (root / "app" / "milestones.yaml").write_text(
        "milestones:\n  - {id: m1, name: M1, status: planned}\n")
    (root / "app" / "tasks.yaml").write_text(yaml.safe_dump({"tasks": [
        {"id": "t-1", "milestone": "m1", "title": "Background", "status": "planned",
         "type": "visual", "acceptance": {"commands": ["true"]},
         "assets": [asset("sky", 1)]},
        {"id": "t-2", "milestone": "m1", "title": "Portraits", "status": "planned",
         "type": "visual", "acceptance": {"commands": ["true"]},
         "assets": [asset("blob", 3)]},
        {"id": "t-3", "milestone": "m1", "title": "Plain", "status": "planned",
         "acceptance": {"commands": ["true"]}}]}))
    master = Master(root)
    paths = RuntimePaths(state_dir=tmp_path / "state", worktrees_root=tmp_path / "worktrees")
    return {"master": master, "repo": repo, "paths": paths}


def step(project, provider):
    return AssetStep(project["master"], project["paths"].state_dir, [provider],
                     default_worktrees(project["master"], project["paths"]))


def test_tasks_with_missing_images_wait_and_are_not_run(project):
    master = project["master"]
    assert waiting_tasks(master, "app") == {"t-1": "waiting for images: sky",
                                            "t-2": "waiting for images: blob"}
    from core.history import InMemoryHistoryStore

    runnable, _ = work_for(master.status("app"), InMemoryHistoryStore())
    assert runnable == ["t-1", "t-2", "t-3"]  # work_for itself is unaware; the daemon filters


def test_a_single_image_is_committed_and_candidates_wait_for_a_pick(project):
    provider = Fake("pollinations")
    lines = step(project, provider)("app")
    assert "t-1: image sky committed" in lines[0]
    assert lines[1] == "t-2: 3 images to pick from for blob (ms status)"
    repo = project["repo"]
    assert blob(repo, "develop:www/assets/sky.png") == PNG
    assert "t-1: image sky" in git(repo, "log", "-1", "--format=%s", "develop")
    assert provider.prompts[0] == "cute cartoon, bright colours. a sky icon"
    state_dir = project["paths"].state_dir
    files = candidates(state_dir, "app", "t-2", "blob")
    assert [f.name for f in files] == ["candidate-1.png", "candidate-2.png", "candidate-3.png"]
    assert (files[0].parent / "index.html").read_text().count("<img") == 3
    assert json.loads((files[0].parent / "meta.json").read_text())["candidates"][2]["n"] == 3
    assert waiting_tasks(project["master"], "app") == {"t-2": "waiting for images: blob"}
    # Nothing new is generated on the next cycle.
    assert step(project, provider)("app") == []
    assert len(provider.prompts) == 4


def test_a_failed_generation_is_reported_and_retried_later(project):
    lines = step(project, Fake("pollinations", fail=True))("app")
    assert lines == ["t-1 sky: image generation failed: pollinations: down",
                     "t-2 blob: image generation failed: pollinations: down"]
    assert step(project, Fake("cloudflare"))("app")[0].startswith("t-1: image sky committed")


def test_the_orchestrator_refuses_a_task_that_waits_for_images(project):
    from core.execution_runner import ExecutionError
    from core.task_orchestrator import TaskOrchestrator

    project["master"].update_task("app", "t-2", status="in_progress")
    orchestrator = TaskOrchestrator(project["master"], None, None)
    with pytest.raises(ExecutionError, match="waits for the owner: its images"):
        orchestrator.prepare("app", "t-2")


def test_ms_pick_commits_the_chosen_candidate(tmp_path, monkeypatch):
    root = default_projects_root()
    repo = make_repo(tmp_path / "repo", {"www/index.html": "x\n"})
    project_file = root / "sample-project" / "project.yaml"
    data = yaml.safe_load(project_file.read_text())
    data.update(repository=str(repo), base_branch="main")
    project_file.write_text(yaml.safe_dump(data))
    tasks_file = root / "sample-project" / "tasks.yaml"
    tasks = yaml.safe_load(tasks_file.read_text())
    tasks["tasks"][2].update(type="visual", assets=[asset("blob", 3)])
    tasks_file.write_text(yaml.safe_dump(tasks))
    task_id = tasks["tasks"][2]["id"]
    folder = RuntimePaths.default().state_dir / "assets" / "sample-project" / task_id / "blob"
    folder.mkdir(parents=True)
    for n in (1, 2, 3):
        (folder / f"candidate-{n}.png").write_bytes(PNG + bytes([n]))
    out = io.StringIO()
    assert ms_main(["pick", "sample-project", task_id, "blob", "4"], out=out) == 1
    assert ms_main(["pick", "sample-project", task_id, "blob", "2"], out=out) == 0
    assert "Committed candidate 2" in out.getvalue()
    assert blob(repo, "main:www/assets/blob.png") == PNG + bytes([2])
    master = Master(root)
    assert missing_assets(master.project_state("sample-project").project(),
                          master.project_state("sample-project").get_task(task_id)) == []
