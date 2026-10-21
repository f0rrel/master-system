"""ms doctor lists what a worker (the owner's user) can reach, read-only."""

import os

from core.worker_exposure import render_exposure, worker_exposure


def home_with(tmp_path, *, keys=(), browser=None, gh=False, secrets=()):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    for name in keys:
        (home / ".ssh" / name).write_text("not a real key")
    config = home / ".config" / "master-system"
    config.mkdir(parents=True)
    for name in secrets:
        (config / name).write_text("value-that-must-never-be-printed")
        os.chmod(config / name, 0o600)
    if browser:
        (home / browser).mkdir(parents=True)
    if gh:
        (home / ".config" / "gh").mkdir(parents=True)
    return home


def test_a_clean_account_reports_nothing_and_does_not_claim_a_sandbox(tmp_path):
    home = tmp_path / "empty"
    home.mkdir()
    found = worker_exposure(home=home, group_names=["users"],
                            docker_socket=str(tmp_path / "no-socket"))
    assert found == []
    assert "does not make workers sandboxed" in render_exposure(found)


def test_docker_and_sudo_groups_are_critical(tmp_path):
    home = home_with(tmp_path)
    found = worker_exposure(home=home, group_names=["docker", "sudo", "users"],
                            docker_socket=str(tmp_path / "no-socket"))
    critical = [e.what for e in found if e.severity == "critical"]
    assert any("Docker" in c for c in critical) and any("'sudo'" in c for c in critical)
    assert [e.severity for e in found][:2] == ["critical", "critical"]  # most serious first


def test_a_writable_docker_socket_is_critical_even_without_the_group(tmp_path):
    socket_file = tmp_path / "docker.sock"
    socket_file.write_text("")
    home = home_with(tmp_path)
    found = worker_exposure(home=home, group_names=[], docker_socket=str(socket_file))
    assert [e.severity for e in found] == ["critical"]


def test_credentials_are_reported_by_name_and_mode_never_by_content(tmp_path):
    home = home_with(tmp_path, keys=["id_ed25519", "id_ed25519.pub", "known_hosts"],
                     browser=".mozilla", gh=True, secrets=["github-app.pem", "master.env"])
    found = worker_exposure(home=home, group_names=[], docker_socket=str(tmp_path / "none"),
                            config_dir=home / ".config" / "master-system")
    text = render_exposure(found)
    assert "1 SSH private key" in text  # the .pub and known_hosts are not keys
    assert "github-app.pem (mode 600)" in text.replace(str(home / ".config" / "master-system") + "/", "")
    assert "master.env (mode 600)" in text.replace(str(home / ".config" / "master-system") + "/", "")
    assert "~/.mozilla" in text and "GitHub CLI" in text
    assert "value-that-must-never-be-printed" not in text
    assert "not a real key" not in text
