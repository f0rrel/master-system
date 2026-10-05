"""GitHub, as a GitHub App installed on one repository (owner decision G1).

The app's private key lives in ``~/.config/master-system/github-app.pem``
(mode 600). For each use the system signs a short JWT with it (``openssl``,
no new dependency), exchanges it for an installation token (valid one hour,
limited to the repositories the app is installed on), and uses that token for
REST calls and for ``git push`` through a GIT_ASKPASS helper that reads it from
the environment of that one git process. The token is never written to disk,
history, logs or argv, and never reaches a worker.

What the app can change is limited on GitHub's side: a ruleset on ``main``
requires a pull request and allows no bypass, so the app can never move it.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

__all__ = ["GitHubApp", "GitHubError", "default_key_path", "push"]

API = "https://api.github.com"


class GitHubError(RuntimeError):
    pass


def default_key_path() -> Path:
    from core.run_config import default_config_path

    return default_config_path().parent / "github-app.pem"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


@dataclass
class GitHubApp:
    app_id: str
    repo: str  # "owner/name"
    key_path: Path
    sign: Optional[Callable[[Path, bytes], bytes]] = None
    opener: Optional[Callable] = None
    api: str = API
    _token: Optional[str] = None
    _token_until: float = 0

    def jwt(self, now: Optional[float] = None) -> str:
        if self.sign is None:
            from core.host import openssl_sign_rs256

            self.sign = openssl_sign_rs256
        now = int(now if now is not None else time.time())
        header = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        claims = _b64(json.dumps({"iat": now - 60, "exp": now + 540,
                                  "iss": str(self.app_id)}).encode())
        signing_input = f"{header}.{claims}".encode()
        return f"{header}.{claims}.{_b64(self.sign(self.key_path, signing_input))}"

    def request(self, method: str, path: str, body=None, *, auth: Optional[str] = None):
        """One REST call; returns the decoded JSON (or None). Raises GitHubError."""
        if auth is None:
            auth = f"token {self.token()}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{self.api}{path}", data=data, method=method, headers={
            "Accept": "application/vnd.github+json", "Authorization": auth,
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "master-system"})
        opener = self.opener or urllib.request.urlopen
        try:
            with opener(req, timeout=30) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")[:300]
            raise GitHubError(f"GitHub {method} {path}: {error.code} {detail}") from None
        except OSError as error:
            raise GitHubError(f"GitHub {method} {path}: {error}") from None
        return json.loads(raw) if raw else None

    def token(self) -> str:
        """An installation token for the repository, cached until near expiry."""
        if self._token and time.time() < self._token_until:
            return self._token
        bearer = f"Bearer {self.jwt()}"
        installation = self.request("GET", f"/repos/{self.repo}/installation", auth=bearer)
        if not installation or "id" not in installation:
            raise GitHubError(f"the app is not installed on {self.repo}")
        granted = self.request("POST", f"/app/installations/{installation['id']}/access_tokens",
                               auth=bearer)
        self._token = granted["token"]
        self._token_until = time.time() + 45 * 60
        return self._token


ASKPASS = """#!/bin/sh
case "$1" in
  Username*) echo x-access-token ;;
  *) echo "$MS_GITHUB_TOKEN" ;;
esac
"""


def push(app: GitHubApp, repository: Path, refspecs, askpass_dir: Path, *,
         git=None, force_refs=()) -> None:
    """Push refspecs to https://github.com/<repo>.git with the app's token.

    The token travels only in the environment of this one git process; the
    helper script that hands it to git contains no secret.
    """
    if git is None:
        from core.host import git
    askpass = Path(askpass_dir) / "git-askpass.sh"
    if not askpass.exists():
        askpass.parent.mkdir(parents=True, exist_ok=True)
        askpass.write_text(ASKPASS)
        askpass.chmod(0o700)
    url = f"https://github.com/{app.repo}.git"
    specs = [("+" + s if s in force_refs else s) for s in refspecs]
    git(["-c", "credential.helper=", "push", "--porcelain", url, *specs], repository,
        extra_env={"GIT_ASKPASS": str(askpass), "MS_GITHUB_TOKEN": app.token()})
