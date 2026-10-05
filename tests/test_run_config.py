"""run_cli configuration: TOML, defaults, validation."""

from pathlib import Path

import pytest

from core.run_config import ConfigError, RunConfig, default_config_path, load_config


def write(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


def test_a_missing_file_means_the_defaults(tmp_path):
    config = load_config(tmp_path / "nope.toml")

    assert config == RunConfig()
    assert config.master.provider == "deepseek"
    assert config.master.model == "deepseek-v4-flash"
    assert config.worker.node_min_major == 22
    assert config.run.attempt_timeout_s == 1800
    assert "deepseek-v4-flash" in config.prices


def test_the_default_path_follows_xdg_config_home(tmp_path):
    assert default_config_path({"XDG_CONFIG_HOME": str(tmp_path)}) == \
        tmp_path / "master-system" / "config.toml"


def test_a_full_file_is_read(tmp_path):
    config = load_config(write(tmp_path, '''
[master]
provider = "ollama"
model = "qwen3:8b"
base_url = "http://box:11434"
timeout_s = 60

[worker]
opencode_bin = "/opt/opencode"
model = "deepseek/deepseek-v4-flash"
home = "~/worker"
extra_args = ["--pure"]

[run]
max_steps = 30
attempt_timeout_s = 900
projects_root = "/srv/projects"

[prices]
"qwen3:8b" = { input = 0, output = 0 }
'''))

    assert (config.master.provider, config.master.model, config.master.timeout_s) == \
        ("ollama", "qwen3:8b", 60.0)
    assert config.worker.opencode_bin == Path("/opt/opencode")
    assert config.worker.home == Path.home() / "worker"
    assert config.worker.extra_args == ("--pure",)
    assert config.run.max_steps == 30 and config.run.attempt_timeout_s == 900
    assert config.run.projects_root == Path("/srv/projects")
    assert config.prices["qwen3:8b"] == {"input": 0.0, "output": 0.0}
    assert "deepseek-v4-flash" in config.prices  # defaults are kept


@pytest.mark.parametrize("text, message", [
    ('[master]\nprovider = "gpt"\n', "provider"),
    ('[master]\nmodel = ""\n', "model"),
    ('[run]\nmax_steps = 0\n', "max_steps"),
    ('[run]\nattempt_timeout_s = -1\n', "attempt_timeout_s"),
    ('[run]\nmax_retries = -1\n', "max_retries"),
    ('[worker]\nextra_args = "--auto"\n', "extra_args"),
    ('[worker]\napi_key = "sk-..."\n', "unknown keys"),
    ('[secrets]\nx = 1\n', "unknown sections"),
    ('[prices]\nm = { input = -1 }\n', "non-negative"),
    ('not toml = = =', "cannot read"),
])
def test_invalid_configuration_is_refused(tmp_path, text, message):
    with pytest.raises(ConfigError, match=message):
        load_config(write(tmp_path, text))
