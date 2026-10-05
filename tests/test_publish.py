"""core.publish and core.github: the preview site and the GitHub App, offline."""

import base64
import json
import subprocess

import pytest
import yaml

from conftest import git, make_repo
from core.github import GitHubApp, GitHubError, push
from core.history import EventType, InMemoryHistoryStore
from core.host import git as host_git
from core.master import Master
from core.publish import Publisher, build_site_commit, pages_url


@pytest.fixture
def site(tmp_path):
    repo = make_repo(tmp_path / "repo", {"www/index.html": "released\n",
                                         "www/js/app.js": "v1\n", "README.md": "x\n"})
    git(repo, "branch", "develop")
    git(repo, "checkout", "-q", "develop")
    (repo / "www" / "index.html").write_text("preview\n")
    git(repo, "commit", "-qam", "develop change")
    git(repo, "checkout", "-q", "main")
    root = tmp_path / "projects"
    project = root / "ml"
    project.mkdir(parents=True)
    (project / "project.yaml").write_text(yaml.safe_dump({
        "id": "ml", "name": "ML", "status": "active", "repository": str(repo),
        "base_branch": "develop", "auto_integrate": True,
        "github": {"repo": "f0rrel/Match_Legends_mobile_game", "release_branch": "main",
                   "site_dir": "www"}}))
    (project / "milestones.yaml").write_text(yaml.safe_dump({"milestones": []}))
    (project / "tasks.yaml").write_text(yaml.safe_dump({"tasks": []}))
    pushes = []

    def fake_push(app, repository, refspecs, askpass_dir, force_refs=()):
        pushes.append((app, list(refspecs), tuple(force_refs)))

    history = InMemoryHistoryStore()
    publisher = Publisher(Master(root), history, lambda repo_name: "APP", tmp_path / "state",
                          push=fake_push)
    return {"repo": repo, "publisher": publisher, "history": history, "pushes": pushes,
            "root": root}


def show(repo, ref, path):
    return git(repo, "show", f"{ref}:{path}")


def test_pages_url():
    assert pages_url("f0rrel/Match_Legends_mobile_game") == \
        "https://f0rrel.github.io/Match_Legends_mobile_game/"


def test_the_site_has_the_release_at_the_root_and_develop_below(site):
    lines = site["publisher"]("ml", "s1")

    repo = site["repo"]
    assert show(repo, "gh-pages", "index.html") == "released"
    assert show(repo, "gh-pages", "develop/index.html") == "preview"
    assert show(repo, "gh-pages", "develop/js/app.js") == "v1"
    assert show(repo, "gh-pages", ".nojekyll") == ""
    assert "README.md" not in git(repo, "ls-tree", "-r", "--name-only", "gh-pages")
    assert lines == ["Preview updated: https://f0rrel.github.io/Match_Legends_mobile_game/"
                     f"develop/ (develop {git(repo, 'rev-parse', 'develop')[:12]})"]
    [(app, refspecs, force)] = site["pushes"]
    assert app == "APP" and refspecs == ["develop", "gh-pages"] and force == ("gh-pages",)
    assert "main" not in refspecs
    [event] = site["history"].events(types=[EventType.PUBLISHED])
    assert event.payload["develop_sha"] == git(repo, "rev-parse", "develop")
    assert event.payload["release_sha"] == git(repo, "rev-parse", "main")


def test_nothing_is_published_when_nothing_changed(site):
    site["publisher"]("ml")
    assert site["publisher"]("ml") == []
    assert len(site["pushes"]) == 1


def test_a_new_develop_commit_republishes_on_top_of_the_old_site(site):
    site["publisher"]("ml")
    first = git(site["repo"], "rev-parse", "gh-pages")
    repo = site["repo"]
    git(repo, "checkout", "-q", "develop")
    (repo / "www" / "index.html").write_text("preview 2\n")
    git(repo, "commit", "-qam", "more")
    git(repo, "checkout", "-q", "main")

    site["publisher"]("ml")
    assert show(repo, "gh-pages", "develop/index.html") == "preview 2"
    assert git(repo, "rev-parse", "gh-pages^") == first


def test_without_the_app_nothing_is_pushed_and_the_owner_is_told(site, tmp_path):
    publisher = Publisher(Master(site["root"]), site["history"], lambda repo: None,
                          tmp_path / "state", push=lambda *a, **k: pytest.fail("pushed"))
    assert "not set up yet" in publisher("ml")[0]
    assert site["history"].events(types=[EventType.PUBLISHED]) == ()


def test_a_project_without_github_is_not_published(site, tmp_path):
    project_yaml = site["root"] / "ml" / "project.yaml"
    data = yaml.safe_load(project_yaml.read_text())
    del data["github"]
    project_yaml.write_text(yaml.safe_dump(data))
    assert site["publisher"]("ml") == [] and site["pushes"] == []
    assert site["publisher"].preview_url("ml") is None


def test_build_site_commit_is_none_when_the_tree_is_unchanged(site):
    repo = site["repo"]
    main, develop = git(repo, "rev-parse", "main"), git(repo, "rev-parse", "develop")
    first = build_site_commit(host_git, repo, main, develop, "www", None)
    assert build_site_commit(host_git, repo, main, develop, "www", first) is None


# --- GitHub App ---


class Response:
    def __init__(self, body, status=200):
        self.body, self.status = body, status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self.body).encode()


def test_the_app_mints_an_installation_token_with_a_signed_jwt(tmp_path):
    calls = []

    def opener(request, timeout):
        calls.append((request.get_method(), request.full_url,
                      request.get_header("Authorization")))
        if request.full_url.endswith("/installation"):
            return Response({"id": 42})
        return Response({"token": "ghs_secret"})

    app = GitHubApp(app_id="123", repo="o/r", key_path=tmp_path / "k.pem",
                    sign=lambda key, data: b"sig", opener=opener)
    assert app.token() == "ghs_secret"
    assert app.token() == "ghs_secret" and len(calls) == 2  # cached
    method, url, auth = calls[0]
    assert (method, url) == ("GET", "https://api.github.com/repos/o/r/installation")
    header, claims, signature = auth.removeprefix("Bearer ").split(".")
    payload = json.loads(base64.urlsafe_b64decode(claims + "=="))
    assert payload["iss"] == "123" and payload["exp"] - payload["iat"] <= 600
    assert calls[1][:2] == ("POST", "https://api.github.com/app/installations/42/access_tokens")


def test_an_api_error_is_a_github_error(tmp_path):
    import urllib.error

    def opener(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 404, "nf", {}, None)

    app = GitHubApp(app_id="1", repo="o/r", key_path=tmp_path / "k", sign=lambda k, d: b"s",
                    opener=opener)
    with pytest.raises(GitHubError):
        app.token()


def test_the_jwt_signature_is_real_rs256(tmp_path):
    key = tmp_path / "k.pem"
    subprocess.run(["openssl", "genrsa", "-out", str(key), "2048"], check=True,
                   capture_output=True)
    app = GitHubApp(app_id="7", repo="o/r", key_path=key)
    header, claims, signature = app.jwt().split(".")
    pub = tmp_path / "pub.pem"
    subprocess.run(["openssl", "rsa", "-in", str(key), "-pubout", "-out", str(pub)],
                   check=True, capture_output=True)
    sig = tmp_path / "sig"
    sig.write_bytes(base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4)))
    data = tmp_path / "data"
    data.write_bytes(f"{header}.{claims}".encode())
    verified = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(pub),
                               "-signature", str(sig), str(data)], capture_output=True)
    assert verified.returncode == 0


def test_push_puts_the_token_only_in_the_environment(tmp_path):
    seen = {}

    def fake_git(args, cwd, extra_env=None, **kwargs):
        seen.update(args=args, env=extra_env)
        return ""

    class App:
        repo = "o/r"

        def token(self):
            return "ghs_secret"

    push(App(), tmp_path, ["develop", "gh-pages"], tmp_path / "askpass", git=fake_git,
         force_refs=("gh-pages",))
    assert "ghs_secret" not in " ".join(seen["args"])
    assert seen["args"][-3:] == ["https://github.com/o/r.git", "develop", "+gh-pages"]
    assert seen["env"]["MS_GITHUB_TOKEN"] == "ghs_secret"
    helper = (tmp_path / "askpass" / "git-askpass.sh").read_text()
    assert "ghs_secret" not in helper and "MS_GITHUB_TOKEN" in helper
