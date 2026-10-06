"""A verifier for a task's human-written acceptance criteria.

It checks the attempt's committed result in a worktree the orchestrator made for
verification (a fresh checkout of ``result_sha``, never the worker's), using
only what the orchestrator provides (``workspace.path``, ``base_sha``,
``result_sha``) and what the human wrote (``task["acceptance"]``). Nothing
comes from the worker.

* No acceptance on the task -> ``unable_to_verify``: nobody defined "done".
* The worktree is not exactly the committed result -> ``unable_to_verify``.
* The diff ``base_sha..result_sha`` touches a protected path -> ``fail``. A
  worker cannot make the checks pass by editing the checks. Renames count as
  both paths; deletions count.
* With ``allowed_paths`` (frozen from the task's type), the diff changing any path
  that matches none of them -> ``fail``: an attempt stays within its type.
* Every acceptance command runs (``/bin/sh -c``) in the worktree, bounded by a
  per-command timeout and the verification deadline. ``pass`` only if no
  protected path was touched and every command exited 0.

Globs use ``fnmatch.fnmatchcase`` on repository-relative POSIX paths, so ``*``
also matches ``/``: ``tests/*`` protects everything under ``tests/``.
"""

from __future__ import annotations

from fnmatch import fnmatchcase
from typing import Mapping, Optional

from core.verification import VerificationResult

__all__ = ["AcceptanceVerifier", "COMMAND_TIMEOUT_S", "EXCERPT_CHARS"]

COMMAND_TIMEOUT_S = 600
#: How much of a failing command's output a finding keeps (its tail).
EXCERPT_CHARS = 2000
_GIT_TIMEOUT_S = 60


def _unable(summary: str, **evidence) -> VerificationResult:
    return VerificationResult(verdict="unable_to_verify", summary=summary, evidence=evidence)


class AcceptanceVerifier:
    """VerificationBackend for ``task["acceptance"]``."""

    def __init__(self, command_timeout_s: float = COMMAND_TIMEOUT_S,
                 excerpt_chars: int = EXCERPT_CHARS):
        if command_timeout_s <= 0 or excerpt_chars <= 0:
            raise ValueError("command_timeout_s and excerpt_chars must be positive")
        self._command_timeout_s = command_timeout_s
        self._excerpt_chars = excerpt_chars

    def verify(self, task: Mapping, context: Mapping,
               evidence: Optional[Mapping] = None, *, workspace) -> VerificationResult:
        acceptance = task.get("acceptance") if isinstance(task, Mapping) else None
        if not acceptance:
            return _unable("the task has no acceptance criteria")
        base, result = workspace.base_sha, workspace.result_sha
        if not result:
            return _unable("the attempt has no committed result", base_sha=base)

        status = workspace.run(["git", "status", "--porcelain"], timeout=_GIT_TIMEOUT_S)
        if status.returncode != 0 or status.timed_out:
            return _unable("could not read the worktree's status", base_sha=base,
                           result_sha=result)
        if status.stdout.strip():
            return _unable("the worktree does not match the committed result",
                           base_sha=base, result_sha=result)

        diff = workspace.run(["git", "diff", "--name-only", "--no-renames", base, result],
                             timeout=_GIT_TIMEOUT_S)
        if diff.returncode != 0 or diff.timed_out:
            return _unable("could not compare the result with its base", base_sha=base,
                           result_sha=result)
        changed = [line for line in diff.stdout.splitlines() if line]
        protected = acceptance.get("protected_paths") or []
        violations = sorted(
            path for path in changed if any(fnmatchcase(path, glob) for glob in protected)
        )
        findings = [{"kind": "protected_path", "path": path} for path in violations]
        allowed = acceptance.get("allowed_paths")
        outside = sorted(
            path for path in changed
            if allowed and not any(fnmatchcase(path, glob) for glob in allowed)
        )
        findings += [{"kind": "outside_allowed_paths", "path": path} for path in outside]

        commands_run = []
        for command in acceptance.get("commands") or []:
            outcome = workspace.run(["/bin/sh", "-c", command],
                                    timeout=self._command_timeout_s)
            commands_run.append({"command": command, "exit_code": outcome.returncode,
                                 "timed_out": outcome.timed_out})
            if outcome.timed_out or outcome.returncode != 0:
                findings.append({
                    "kind": "command_failed",
                    "command": command,
                    "exit_code": outcome.returncode,
                    "timed_out": outcome.timed_out,
                    "output_excerpt": (outcome.stdout + outcome.stderr)[-self._excerpt_chars:],
                })

        failed_commands = sum(1 for f in findings if f["kind"] == "command_failed")
        if findings:
            parts = []
            if violations:
                parts.append(f"{len(violations)} protected path(s) changed")
            if outside:
                parts.append(f"{len(outside)} path(s) outside the task type's allowed paths "
                             "changed")
            if failed_commands:
                parts.append(f"{failed_commands} of {len(commands_run)} command(s) failed")
            verdict, summary = "fail", "; ".join(parts)
        else:
            verdict, summary = "pass", f"all {len(commands_run)} command(s) passed"

        return VerificationResult(
            verdict=verdict,
            summary=summary,
            findings=findings,
            evidence={
                "base_sha": base,
                "result_sha": result,
                "commands_run": commands_run,
                "protected_violations": violations,
                "outside_allowed_paths": outside,
            },
        )
