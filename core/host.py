"""The only control-plane module that starts host processes (besides workers).

The background service starts runs as child processes of ``core.run_cli`` (so a
crash in a run cannot take the service down) and manages its own systemd user
unit. Workers never come here: they are started by ``worker_process`` inside a
workspace, with the allowlisted environment.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

from core.daemon import OBJECTIVE, RunRequest, load_env_file

__all__ = ["subprocess_runner", "systemctl"]

REPO = Path(__file__).resolve().parent.parent


def systemctl(*argv) -> int:
    return subprocess.run(["systemctl", "--user", *argv]).returncode


def subprocess_runner(config_path: Optional[str], env_file: Path, log_dir: Path,
                      state_dir: Path) -> Callable[[RunRequest], int]:
    """Run a session through run_cli in a child process (crash isolation).

    The Master's key comes from ``env_file`` into the child's environment only;
    run_cli gives workers an allowlisted environment without it.
    """

    def run(request: RunRequest) -> int:
        env = dict(os.environ)
        env.update(load_env_file(env_file))
        cmd = [sys.executable, "-m", "core.run_cli"]
        if config_path:
            cmd += ["--config", str(config_path)]
        cmd += ["start", request.project_id, "--session", request.session_id,
                "--objective", OBJECTIVE, "--until-stopped",
                "--max-cost-usd", str(request.max_cost_usd)]
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / f"{request.session_id}.log", "w") as log:
            child = subprocess.Popen(cmd, env=env, stdout=log, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, cwd=REPO)
            (Path(state_dir) / "run.pid").write_text(str(child.pid))
            try:
                return child.wait()
            except KeyboardInterrupt:
                child.send_signal(signal.SIGINT)
                return child.wait()
            finally:
                (Path(state_dir) / "run.pid").unlink(missing_ok=True)

    return run
