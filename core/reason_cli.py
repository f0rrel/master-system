"""Command line entry point for the reasoning loop: ``python -m core.reason_cli``.

Why this is not a subcommand of ``python -m core.master``
--------------------------------------------------------
``core/master.py`` is the deterministic authority, and its import graph is
deliberately tiny and enforced by tests: it may import argparse, sys, pathlib,
and the two manager layers, and nothing else. A ``reason`` subcommand would have
to pull a model provider, and therefore an HTTP client, into that module, which
would dissolve the property that makes Master trustworthy: it cannot reach the
network, a model, or execution of any kind.

So the reasoning loop gets its own entry point. The separation is not
ceremonial. ``core.master`` stays inert and testable; ``core.reason_cli`` is the
only place that knows a model exists. Both end up at the same Master.

``--provider`` chooses the backend. Each backend keeps its own defaults in its own
module, so this file only passes on what a person actually asked for; adding a
backend is one branch in :func:`build_provider` and nothing else. Nothing above
this module changes when a new one appears.

The flow, and its default
-------------------------
    reason REQUEST -> show the proposal -> ask -> execute

The approval prompt defaults to rejection. Empty input, anything that is not an
explicit yes, or end-of-input (a closed pipe) all mean no. The reasoning step and
the approval step are separate: the model can only ever produce a proposal, and
an unapproved proposal cannot execute anything.

``--dry-run`` stops after the proposal, which is also what runs when there is no
terminal available to approve anything.

Approved operations are applied while holding the project's
:class:`core.run_lock.ProjectLock`, only for the writes themselves. While an
autonomous run holds the project, execution is refused with ``EXIT_BUSY`` and
nothing changes. A ``run_task`` in a proposal is never executed here: it is a
dispatch operation, and only the autonomous loop dispatches.
"""

import argparse
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.master import Master
from core.ollama_provider import DEFAULT_BASE_URL as OLLAMA_BASE_URL
from core.ollama_provider import DEFAULT_MODEL as OLLAMA_MODEL
from core.ollama_provider import OllamaProvider
from core.opencode_provider import DEFAULT_BASE_URL as OPENCODE_BASE_URL
from core.opencode_provider import DEFAULT_MODEL_ID as OPENCODE_MODEL
from core.opencode_provider import DEFAULT_PROVIDER_ID as OPENCODE_PROVIDER_ID
from core.opencode_provider import OpenCodeProvider
from core.deepseek_provider import DEFAULT_BASE_URL as DEEPSEEK_BASE_URL
from core.deepseek_provider import DEFAULT_MODEL as DEEPSEEK_MODEL
from core.deepseek_provider import DeepSeekProvider
from core.provider import ProviderError
from core.reasoning import ApprovalState, ReasoningInterface, ResultStatus
from core.reasoning_engine import ReasoningEngine, ReasoningError
from core.run_lock import ProjectBusyError, ProjectLock

EXIT_OK = 0
EXIT_REASONING_FAILED = 1
EXIT_USAGE = 2
EXIT_BUSY = 3


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m core.reason_cli",
        description=(
            "Ask a reasoning model to propose work-system operations, then "
            "approve them before anything changes."
        ),
    )
    parser.add_argument("request", help="What you want, in your own words.")
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Projects root (default: ~/.config/master-system/projects).",
    )
    parser.add_argument(
        "--project",
        default=None,
        help="Project to reason about. Required unless exactly one exists.",
    )
    parser.add_argument(
        "--provider",
        choices=["ollama", "opencode", "deepseek"],
        default="ollama",
        help="Which reasoning backend to use.",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="Backend address. Defaults to the selected provider's own address.",
    )
    parser.add_argument(
        "--provider-id",
        default=None,
        help=(
            "Model provider to ask, for backends that name a model by a provider "
            "and a model separately, as OpenCode does. Ignored by providers that "
            "do not."
        ),
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Model to ask. Defaults to the selected provider's own default.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=180.0,
        help=(
            "Seconds to wait for the backend. For OpenCode this bounds both a "
            "single request and the whole wait for one reply."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the proposal and exit without approving or executing.",
    )
    return parser


def build_provider(args):
    """Return the configured provider.

    A single dispatch point, so adding a backend means adding a branch here and
    a class in its own module, and touching nothing else. No provider is
    referenced anywhere else in the system.

    Every default belongs to the provider it belongs to. The flags start empty and
    are filled in here, so choosing ``--provider opencode`` does not silently
    inherit Ollama's address or model name, and a flag that was given is always
    passed through untouched.
    """
    if args.provider == "ollama":
        return OllamaProvider(
            base_url=args.base_url or OLLAMA_BASE_URL,
            model=args.model or OLLAMA_MODEL,
            timeout=args.timeout,
        )
    if args.provider == "opencode":
        return OpenCodeProvider(
            base_url=args.base_url or OPENCODE_BASE_URL,
            provider_id=args.provider_id or OPENCODE_PROVIDER_ID,
            model_id=args.model or OPENCODE_MODEL,
            timeout=args.timeout,
            total_timeout=args.timeout,
        )
    if args.provider == "deepseek":
        return DeepSeekProvider(
            base_url=args.base_url or DEEPSEEK_BASE_URL,
            model=args.model or DEEPSEEK_MODEL,
            timeout=args.timeout,
        )
    raise ProviderError(f"unknown provider: {args.provider}")


def resolve_project(master, requested):
    """Choose the project to reason about, without guessing when ambiguous."""
    available = master.list_projects()

    if requested is not None:
        if requested not in available:
            raise SystemExit(
                f"error: no usable project {requested!r}; "
                f"available: {', '.join(available) or 'none'}"
            )
        return requested

    if len(available) == 1:
        return available[0]

    raise SystemExit(
        "error: specify --project; available: "
        f"{', '.join(available) or 'none'}"
    )


def render_proposal(proposal):
    """Return the human-facing text for a proposal.

    Invalid operations are printed too, with the reason. Hiding them would let a
    model appear to have proposed only the parts that happened to be valid.
    """
    lines = ["", "REASONING", "", proposal.reasoning or "(none given)", ""]
    lines.append("PROPOSED OPERATIONS")
    lines.append("")

    if not proposal.entries:
        lines.append("  (none)")
        return "\n".join(lines)

    for number, entry in enumerate(proposal.entries, start=1):
        lines.append(f"{number}. {entry.describe().upper()}")
        if entry.valid:
            for key, value in entry.operation.arguments.items():
                lines.append(f"   {key}: {value}")
        else:
            # Both the machine-readable reason and the human sentence, so a
            # reader and a script each get what they need.
            lines.append(
                f"   INVALID ({entry.error.reason.value}): {entry.error}"
            )
        lines.append("")

    return "\n".join(lines)


def render_results(batch):
    """Return the human-facing text for execution results."""
    lines = ["", "RESULTS", ""]

    for result in batch.results:
        if result.status is ResultStatus.SUCCESS:
            lines.append(f"  OK        {result.operation}")
        elif result.status is ResultStatus.REJECTED:
            lines.append(f"  REJECTED  {result.operation} ({result.reason.value})")
        else:
            lines.append(
                f"  FAILED    {result.operation} "
                f"({result.error_type}: {result.message})"
            )

    if not batch.results:
        lines.append("  (nothing was executed)")

    return "\n".join(lines)


def ask_for_approval(read_line, dry_run=False, stream=None):
    """Ask once for the whole proposal. Anything but an explicit yes means no.

    ``read_line`` returns None at end of input, which is treated as refusal, so
    a closed pipe cannot accidentally approve anything. ``--dry-run`` never asks
    at all, and so also never approves.
    """
    if dry_run:
        return False

    if stream is None:
        stream = sys.stdout

    print("Approve proposed operations? [y/N] ", end="", file=stream, flush=True)
    answer = read_line()

    if answer is None:
        print(file=stream)
        return False

    return answer.strip().lower() in ("y", "yes")


def main(argv=None, read_line=None, stdout=None, stderr=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if read_line is None:
        read_line = sys.stdin.readline
    if stdout is None:
        stdout = sys.stdout
    if stderr is None:
        stderr = sys.stderr

    def emit(text=""):
        print(text, file=stdout)

    master = Master(args.root)
    project_id = resolve_project(master, args.project)
    provider = build_provider(args)

    interface = ReasoningInterface(master)
    engine = ReasoningEngine(provider, master, interface)

    try:
        proposal = engine.reason(args.request, project_id)
    except (ReasoningError, ProviderError) as error:
        print(f"error: {error}", file=stderr)
        return EXIT_REASONING_FAILED

    emit(render_proposal(proposal))

    if proposal.is_empty:
        emit("\nNo valid operations were proposed. Nothing to approve.")
        return EXIT_OK

    if proposal.invalid_entries:
        emit(
            f"{len(proposal.invalid_entries)} proposed operation(s) were invalid "
            "and can never execute."
        )

    if not ask_for_approval(read_line, args.dry_run, stdout):
        emit("\nRejected. No changes were made.")
        return EXIT_OK

    proposal = proposal.approve()
    assert proposal.state is ApprovalState.APPROVED

    # The lock covers the writes only, not the time spent waiting for a
    # human's answer, and refuses rather than racing an autonomous run.
    try:
        with ProjectLock(
            master.project_state(project_id).project_path, holder="reason_cli"
        ):
            batch = proposal.execute(interface)
    except ProjectBusyError as error:
        print(f"error: {error}", file=stderr)
        return EXIT_BUSY
    emit(render_results(batch))

    if batch.failed:
        return EXIT_REASONING_FAILED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())