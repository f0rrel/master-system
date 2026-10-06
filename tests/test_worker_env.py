"""Worker and verification processes get an allowlisted environment: no secrets,
their own home, and a PATH that finds Node 22 without shell startup files."""

import re
import time
from pathlib import Path

import pytest
import yaml

from conftest import attach_repository
from core.autonomous_loop import AutonomousLoop
from core.execution import ExecutionResult
from core.history import EventType, InMemoryHistoryStore
from core.master import Master
from core.provider import ReasoningProvider
from core.verification import VerificationResult
from core.worker_env import SYSTEM_PATH, find_node_bin, worker_environment
from core.worker_process import run_process

SECRETS = {"DEEPSEEK_API_KEY": "sk-secret-deepseek", "GITHUB_TOKEN": "ghp_secret-github",
           "SSH_AUTH_SOCK": "/tmp/secret-agent.sock", "AWS_SECRET_ACCESS_KEY": "secret-aws",
           "OPENAI_API_KEY": "sk-secret-openai", "GIT_ASKPASS": "/bin/secret-askpass"}


def test_the_environment_is_an_allowlist(tmp_path):
    base = {"PATH": "/home/me/.nvm/bin:/usr/bin", "HOME": "/home/me", "LANG": "C.UTF-8",
            "TERM": "xterm", "EDITOR": "vim", **SECRETS}

    env = worker_environment(tmp_path / "worker-home", path_dirs=["/opt/node22/bin"],
                             base_env=base)

    assert set(env) == {"PATH", "HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
                        "XDG_CACHE_HOME", "npm_config_cache", "CI", "LANG", "TERM"}
    assert env["PATH"] == "/opt/node22/bin:" + ":".join(SYSTEM_PATH)
    assert env["HOME"] == str(tmp_path / "worker-home")
    assert env["XDG_DATA_HOME"].startswith(env["HOME"])
    assert not any(value in env.values() for value in SECRETS.values())


def test_a_secret_looking_extra_is_refused(tmp_path):
    with pytest.raises(ValueError, match="secret"):
        worker_environment(tmp_path, extra={"DEEPSEEK_API_KEY": "sk"})
    env = worker_environment(tmp_path, extra={"PLAYWRIGHT_BROWSERS_PATH": "/cache/pw"})
    assert env["PLAYWRIGHT_BROWSERS_PATH"] == "/cache/pw"


def test_node_is_found_from_nvm_layout_without_a_shell(tmp_path):
    for version in ("v18.19.1", "v22.3.0", "v22.23.3", "v20.1.0", "not-a-version"):
        bin_dir = tmp_path / "versions" / "node" / version / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "node").write_text("")

    assert find_node_bin(22, {"NVM_DIR": str(tmp_path)}) == \
        tmp_path / "versions" / "node" / "v22.23.3" / "bin"
    assert find_node_bin(23, {"NVM_DIR": str(tmp_path)}) is None
    assert find_node_bin(22, {"NVM_DIR": str(tmp_path / "missing")}) is None


def test_the_worker_environment_finds_node_22_and_npm():
    node_bin = find_node_bin(22)
    if node_bin is None:
        pytest.skip("no Node >= 22 installed through nvm on this machine")
    env = worker_environment(Path("/nonexistent-worker-home"), path_dirs=[node_bin])

    outcome = run_process(["sh", "-c", "command -v node; node --version; npm --version"],
                          cwd="/", log_dir=Path("/tmp") / f"ms-env-{time.time_ns()}",
                          deadline=time.monotonic() + 60, env=env)

    lines = outcome.stdout.split()
    assert outcome.returncode == 0, outcome.stderr
    assert lines[0] == str(node_bin / "node")
    major = int(re.match(r"v(\d+)", lines[1]).group(1))
    assert major >= 22
    assert int(lines[2].split(".")[0]) >= 10


# --- end to end: workers and verifiers see only the allowlist -------------------


class Scripted(ReasoningProvider):
    name = "scripted"

    def __init__(self, replies):
        self._replies = list(replies)

    def complete(self, prompt, schema=None):
        return self._replies.pop(0)


RUN = ('{"decision": "act", "reason": "go", "operation": {"operation": "run_task", '
       '"project_id": "alpha", "task_id": "t1"}}')
WAIT = '{"decision": "wait", "reason": "look", "operation": null}'


class DumpsEnv:
    def execute(self, task, context, *, workspace):
        out = workspace.run(["sh", "-c", "env"])
        return ExecutionResult(status="success", artifacts={"env": out.stdout})


class DumpsEnvVerifier:
    seen = None

    def verify(self, task, context, evidence=None, *, workspace):
        DumpsEnvVerifier.seen = workspace.run(["sh", "-c", "env"]).stdout
        return VerificationResult(verdict="pass")


def test_no_secret_reaches_a_worker_or_a_verifier(tmp_path, monkeypatch):
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    project = tmp_path / "alpha-project"
    project.mkdir()
    (project / "project.yaml").write_text(
        yaml.safe_dump({"id": "alpha", "name": "alpha", "status": "active"}))
    (project / "milestones.yaml").write_text(yaml.safe_dump(
        {"milestones": [{"id": "m1", "name": "M1", "status": "in_progress"}]}))
    (project / "tasks.yaml").write_text(yaml.safe_dump({"tasks": [
        {"id": "t1", "milestone": "m1", "title": "T1", "status": "in_progress"}]}))
    attach_repository(project)
    history = InMemoryHistoryStore()

    AutonomousLoop(Master(tmp_path), Scripted([RUN, WAIT]), DumpsEnv(), DumpsEnvVerifier(),
                   history=history).run("alpha")

    worker_env = history.events(types=[EventType.ATTEMPT_FINISHED])[0].payload["artifacts"]["env"]
    for dumped in (worker_env, DumpsEnvVerifier.seen):
        for value in SECRETS.values():
            assert value not in dumped
        assert "HOME=" in dumped and "/master-system-worker" in dumped


# --- Node is optional; the lookup order; extra PATH directories ---------------------


def fake_node(directory, major):
    directory.mkdir(parents=True, exist_ok=True)
    node = directory / "node"
    node.write_text(f"#!/bin/sh\necho v{major}.1.0\n")
    node.chmod(0o755)
    return directory


def nvm_with(tmp_path, major):
    fake_node(tmp_path / "nvm" / "versions" / "node" / f"v{major}.1.0" / "bin", major)
    return {"NVM_DIR": str(tmp_path / "nvm")}


def test_node_is_looked_up_in_node_bin_then_nvm_then_the_system_path(tmp_path):
    from core.host import node_major
    from core.worker_env import find_worker_node

    configured = fake_node(tmp_path / "configured", 24)
    system = fake_node(tmp_path / "system", 22)
    nvm = nvm_with(tmp_path, 23)

    def find(**kwargs):
        args = dict(configured=None, system_dirs=[system], env=nvm, version_of=node_major)
        return find_worker_node(22, **{**args, **kwargs})

    assert find(configured=configured) == configured
    assert find() == tmp_path / "nvm" / "versions" / "node" / "v23.1.0" / "bin"
    assert find(env={"NVM_DIR": str(tmp_path / "none")}) == system
    old = fake_node(tmp_path / "old", 18)
    assert find(configured=old, env={"NVM_DIR": "/none"}, system_dirs=[old]) is None


def test_without_node_the_worker_env_is_built_and_a_warning_given_once(tmp_path, capsys):
    from core import run_cli
    from core.run_config import load_config

    extra = tmp_path / "uv-bin"
    extra.mkdir()
    config_file = tmp_path / "config.toml"
    config_file.write_text(f'[worker]\nhome = "{tmp_path / "wh"}"\nnode_min_major = 99\n'
                           f'path_dirs = ["{extra}"]\n')
    config = load_config(config_file)
    run_cli._NODE_WARNINGS.clear()

    env = run_cli.build_worker_env(config)
    run_cli.build_worker_env(config)

    assert env["PATH"] == f"{extra}:" + ":".join(SYSTEM_PATH)
    warning = "no Node >= 99 for workers; Node-based acceptance commands will fail"
    assert capsys.readouterr().err.count(warning) == 1


def test_node_bin_and_path_dirs_must_be_existing_directories(tmp_path, monkeypatch):
    from core.run_config import ConfigError, load_config

    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "tools").mkdir()
    config_file = tmp_path / "config.toml"
    config_file.write_text('[worker]\nnode_bin = "~/tools"\npath_dirs = ["~/tools"]\n')
    config = load_config(config_file)
    assert config.worker.node_bin == tmp_path / "tools"
    assert config.worker.path_dirs == (tmp_path / "tools",)
    for text in ('node_bin = "~/missing"', 'path_dirs = ["~/missing"]', 'path_dirs = "~/tools"'):
        config_file.write_text(f"[worker]\n{text}\n")
        with pytest.raises(ConfigError, match="existing director"):
            load_config(config_file)


def test_doctor_warns_when_workers_have_no_node(tmp_path, monkeypatch):
    from core import ms, run_cli

    monkeypatch.setattr(run_cli, "find_node_for", lambda config: None)
    report = ms.doctor_report(type("Args", (), {"config": None})())
    assert "no Node >= 22 for workers; Node-based acceptance commands will fail" in report
