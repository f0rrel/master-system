"""Workers and verifiers cannot start processes except through the workspace."""

import ast
from pathlib import Path

CORE = Path(__file__).resolve().parent.parent / "core"

#: Modules that implement ExecutionBackend or VerificationBackend.
BACKENDS = ["opencode_backend.py", "ollama_backend.py", "acceptance_verifier.py"]
#: The only core modules allowed to start processes. ``host.py`` starts the
#: control plane's own runs and systemd unit (the background service), never workers.
PROCESS_STARTERS = {"worker_process.py", "workspace.py", "attempts.py", "host.py"}


def imported(path):
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_no_backend_or_verifier_imports_subprocess():
    for name in BACKENDS:
        path = CORE / name
        if path.exists():
            assert "subprocess" not in imported(path), name


def test_only_the_designated_modules_start_processes():
    starters = {
        path.name for path in CORE.glob("*.py")
        if imported(path) & {"subprocess"} or "os.system" in path.read_text()
    }

    assert starters <= PROCESS_STARTERS, starters - PROCESS_STARTERS
