"""Shared parsing and policy admission for ``agenttalk wrap`` launches.

The real CLI and supervisor admission must use the same option grammar.  This
module owns that grammar and turns an accepted parse into an immutable typed
invocation so downstream code consumes the values argparse actually resolved
(including abbreviation and last-occurrence-wins behavior).
"""

from __future__ import annotations

import argparse
import contextlib
import io
import math
import re
from dataclasses import dataclass
from typing import Sequence, TypeAlias


_PYTHON_SEPARATED_VALUE_OPTIONS = frozenset(
    {"-X", "-W", "-Q", "--check-hash-based-pycs"}
)
_PYTHON_TERMINATING_OPTIONS = frozenset(
    {
        "-?", "-h", "--help", "--help-all", "--help-env",
        "--help-xoptions", "-V", "-VV", "--version",
    }
)
_PYTHON_FLAG_CHARS = frozenset("bBdEiIOPqRsSuvx")
_PY_LAUNCHER_SELECTOR_RE = re.compile(
    r"^-[1-9]\d*(?:\.\d+)?(?:-(?:32|64))?$"
)


@dataclass(frozen=True)
class WrapOptionOccurrence:
    """One explicit wrapper option, in parser-observed order."""

    destination: str
    option: str
    value: object


@dataclass(frozen=True)
class WrapInvocation:
    """The effective wrapper invocation produced by the real wrap grammar."""

    root: str | None
    supervisor_launch_nonce: str | None
    agent: str | None
    cli: str
    sender: str | None
    lane_id: str | None
    min_interval: float
    no_render: bool
    model: str | None
    effort: str | None
    loop: bool
    lead_loop: bool
    one_shot: bool
    to_request: str | None
    dead_letter_max_attempts: int | None
    dead_letter_escalate_after: int | None
    child_argv: tuple[str, ...]
    option_occurrences: tuple[WrapOptionOccurrence, ...]
    agenttalk_argv: tuple[str, ...] | None = None


@dataclass(frozen=True)
class WrapRefusal:
    """A stable, named refusal produced before a launch is advertised."""

    code: str
    message: str


WrapParseResult: TypeAlias = WrapInvocation | WrapRefusal


def python_agenttalk_module_argv(
    args: Sequence[str],
    *,
    program_kind: str,
) -> tuple[str, ...] | None:
    """Return argv after a real Python ``-m agenttalk`` command selector.

    Only interpreter options before Python chooses its execution target are
    scanned.  ``-m`` after a script, ``-c``, stdin, ``--``, help, or version is
    not a module dispatch.  Short options may be clustered: no-value flags are
    skipped, ``W``/``X``/``Q`` consume their attached or following value, and
    ``m`` consumes its attached or following module.  ``py.exe`` selectors are
    handled only for the launcher population.
    """

    tokens = list(args)
    index = 0
    while index < len(tokens):
        argument = tokens[index]
        if argument in _PYTHON_TERMINATING_OPTIONS:
            return None
        if argument in {"--", "-"}:
            return None
        if (
            program_kind == "py"
            and (
                _PY_LAUNCHER_SELECTOR_RE.fullmatch(argument) is not None
                or argument.startswith("-V:")
            )
        ):
            index += 1
            continue
        if argument in _PYTHON_SEPARATED_VALUE_OPTIONS:
            if index + 1 >= len(tokens):
                return None
            index += 2
            continue
        if argument.startswith("--check-hash-based-pycs="):
            index += 1
            continue
        if argument == "--safe-path":
            index += 1
            continue
        if argument.startswith("--"):
            return None
        if not argument.startswith("-"):
            return None

        cluster = argument[1:]
        position = 0
        while position < len(cluster):
            option = cluster[position]
            remainder = cluster[position + 1:]
            if option in {"h", "V", "?", "c"}:
                return None
            if option == "m":
                if remainder:
                    module = remainder
                    tail_index = index + 1
                elif index + 1 < len(tokens):
                    module = tokens[index + 1]
                    tail_index = index + 2
                else:
                    return None
                return (
                    tuple(tokens[tail_index:])
                    if module.casefold() == "agenttalk"
                    else None
                )
            if option in {"W", "X", "Q"}:
                if remainder:
                    index += 1
                elif index + 1 < len(tokens):
                    index += 2
                else:
                    return None
                break
            if option not in _PYTHON_FLAG_CHARS:
                return None
            position += 1
        else:
            index += 1
        continue
    return None


@dataclass(frozen=True)
class SupervisorWrapPolicy:
    """Population-specific constraints applied after the shared parse."""

    name: str
    require_loop: bool = False
    require_one_shot: bool = False
    forbid_one_shot: bool = False
    forbid_lead_loop: bool = False
    expected_agent: str | None = None
    expected_sender: str | None = None
    expected_cli: str | None = None
    expected_request_id: str | None = None
    expected_lane_id: str | None = None
    require_exact_lane: bool = False
    forbid_supervisor_launch_nonce: bool = True


def _record_occurrence(
    namespace: argparse.Namespace,
    *,
    destination: str,
    option: str | None,
    value: object,
) -> None:
    occurrences = getattr(namespace, "_wrap_option_occurrences", None)
    if occurrences is None:
        occurrences = []
        namespace._wrap_option_occurrences = occurrences
    occurrences.append(
        WrapOptionOccurrence(
            destination=destination,
            option=option or destination,
            value=value,
        )
    )


def _record_global_occurrence(
    namespace: argparse.Namespace,
    *,
    destination: str,
    option: str | None,
    value: object,
) -> None:
    occurrences = getattr(namespace, "_agenttalk_global_occurrences", None)
    if occurrences is None:
        occurrences = []
        namespace._agenttalk_global_occurrences = occurrences
    occurrences.append(
        WrapOptionOccurrence(
            destination=destination,
            option=option or destination,
            value=value,
        )
    )


class _TrackedStoreAction(argparse.Action):
    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: object,
        option_string: str | None = None,
    ) -> None:
        del parser
        setattr(namespace, self.dest, values)
        _record_occurrence(
            namespace,
            destination=self.dest,
            option=option_string,
            value=values,
        )


class _TrackedGlobalStoreAction(argparse.Action):
    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: object,
        option_string: str | None = None,
    ) -> None:
        del parser
        setattr(namespace, self.dest, values)
        _record_global_occurrence(
            namespace,
            destination=self.dest,
            option=option_string,
            value=values,
        )


class _TrackedStoreTrueAction(argparse.Action):
    def __init__(self, option_strings: Sequence[str], dest: str, **kwargs: object) -> None:
        super().__init__(option_strings, dest, nargs=0, const=True, **kwargs)

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: object,
        option_string: str | None = None,
    ) -> None:
        del parser, values
        setattr(namespace, self.dest, True)
        _record_occurrence(
            namespace,
            destination=self.dest,
            option=option_string,
            value=True,
        )


def add_wrap_arguments(parser: argparse.ArgumentParser) -> None:
    """Register the canonical ``wrap`` option grammar on ``parser``."""

    parser.add_argument(
        "--for",
        dest="agent",
        action=_TrackedStoreAction,
        help="Agent name (default: $AGENTTALK_SELF)",
    )
    _add_remaining_wrap_arguments(parser)


def _add_remaining_wrap_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--cli",
        default="codex",
        action=_TrackedStoreAction,
        help="Which CLI is being wrapped: 'codex' (codex exec --json) or "
        "'claude' (stream-json).",
    )
    parser.add_argument(
        "--from",
        dest="sender",
        action=_TrackedStoreAction,
        help="Identity recorded as the degraded-restart requester "
        "(default: the wrapped agent).",
    )
    parser.add_argument(
        "--lane-id",
        action=_TrackedStoreAction,
        help="Run the wrapped child from the provisioned lane worktree.",
    )
    parser.add_argument(
        "--min-interval",
        dest="min_interval",
        type=float,
        default=5.0,
        action=_TrackedStoreAction,
        help="Throttle: stamp heartbeat at most once per this many seconds (default 5).",
    )
    parser.add_argument(
        "--no-render",
        dest="no_render",
        action=_TrackedStoreTrueAction,
        help="Do not echo the agent's output to this console.",
    )
    parser.add_argument(
        "--model",
        dest="model",
        default=None,
        action=_TrackedStoreAction,
        help="(--loop) Override the per-agent supervisor.json model for the "
        "wrapped child (flag > per-agent config). Injected as a bare token; "
        "an explicit model in the launch tail still wins.",
    )
    parser.add_argument(
        "--effort",
        dest="effort",
        default=None,
        action=_TrackedStoreAction,
        help="(--loop) Override the per-agent reasoning_effort (flag > per-agent "
        "config). codex {minimal,low,medium,high,xhigh}; claude "
        "{low,medium,high,xhigh,max}. Invalid values are dropped with a warning; "
        "a launch-tail value still wins.",
    )
    parser.add_argument(
        "--loop",
        action=_TrackedStoreTrueAction,
        help="Run as the long-running SUPERVISED wrapper: own the idle bus-wait + "
        "heartbeat and drive the CLI one turn per inbound message (design C). "
        "Opt-in; manual /agenttalk.listen stays the default.",
    )
    parser.add_argument(
        "--lead-loop",
        dest="lead_loop",
        action=_TrackedStoreTrueAction,
        help="With --loop, run as the managed lead-loop CONTROLLER: acquire a "
        "renewable team-mailbox LEASE (the agent must be a configured "
        "managed-lead-loop identity) so an external consumer cannot race the "
        "bus; renew it on every heartbeat; a valid human release/end stands it "
        "down without relaunch.",
    )
    parser.add_argument(
        "--one-shot",
        dest="one_shot",
        action=_TrackedStoreTrueAction,
        help="With --loop, exit after one successful turn.",
    )
    parser.add_argument(
        "--to-request",
        dest="to_request",
        action=_TrackedStoreAction,
        help="With --one-shot, only drive the matching request_id.",
    )
    parser.add_argument(
        "--dead-letter-max-attempts",
        dest="dead_letter_max_attempts",
        type=int,
        default=None,
        action=_TrackedStoreAction,
        help="(--loop) Auto-dead-letter a POISON message after this many "
        "deterministic failures (default 3, or supervisor.json dead_letter.max_attempts; "
        "0 disables - debug only).",
    )
    parser.add_argument(
        "--dead-letter-escalate-after",
        dest="dead_letter_escalate_after",
        type=int,
        default=None,
        action=_TrackedStoreAction,
        help="(--loop) High-attempt backstop: escalate to the operator at this "
        "many attempts on one message; ambiguous/unknown repeated failures also "
        "dead-letter here (default 20, or supervisor.json "
        "dead_letter.escalate_after_attempts; 0 disables).",
    )
    parser.add_argument(
        "cmd",
        nargs=argparse.REMAINDER,
        help="-- followed by the BASE launch command (the per-turn session/stream "
        "args are appended), e.g. `-- codex -a never -s workspace-write` "
        "(loop) or `-- codex ... exec --json \"...\"` (one-shot).",
    )


def add_agenttalk_launch_arguments(parser: argparse.ArgumentParser) -> None:
    """Register global options that may precede a supervised wrap command."""

    parser.add_argument(
        "--root",
        action=_TrackedGlobalStoreAction,
        help="Project root. Resolution precedence: this flag > $AGENTTALK_ROOT > "
        "walk up from CWD looking for .agenttalk/. A pinned root (flag or env) "
        "that has no store fails loudly — it never falls back to the walk.",
    )
    parser.add_argument(
        "--supervisor-launch-nonce",
        action=_TrackedGlobalStoreAction,
        help=argparse.SUPPRESS,
    )


_AGENTTALK_LAUNCH_OPTIONS = (
    "--root",
    "--supervisor-launch-nonce",
)


def resolve_agenttalk_launch_option(argument: str) -> str | None:
    """Resolve one global launch option with argparse's abbreviation rule.

    Admission sometimes has to replace the configured root before parsing the
    final detached command.  Keep that normalization aligned with the real
    parser: an unambiguous long-option prefix such as ``--roo`` is the same
    option as ``--root``.  Ambiguous and unknown spellings resolve to ``None``
    and are left for the closed parser to refuse.
    """

    option = argument.partition("=")[0]
    if not option.startswith("--"):
        return None
    matches = tuple(
        candidate
        for candidate in _AGENTTALK_LAUNCH_OPTIONS
        if candidate == option or candidate.startswith(option)
    )
    return matches[0] if len(matches) == 1 else None


def agenttalk_launch_subcommand(own_argv: Sequence[str]) -> str | None:
    """Return the subcommand after valid shared global launch options.

    This is deliberately not a wrapper-option parser.  It answers only which
    command the shared global grammar targets, so a refused attempted ``wrap``
    cannot be mistaken for an unrelated manual agenttalk invocation.
    """

    index = 0
    while index < len(own_argv):
        argument = own_argv[index]
        if argument == "--":
            if index + 1 >= len(own_argv):
                return None
            subcommand = own_argv[index + 1]
            return (
                subcommand
                if subcommand and not subcommand.startswith("-")
                else None
            )
        option = resolve_agenttalk_launch_option(argument)
        if option is None:
            return argument if argument and not argument.startswith("-") else None
        if "=" in argument:
            if not argument.partition("=")[2]:
                return None
            index += 1
            continue
        if index + 1 >= len(own_argv):
            return None
        value = own_argv[index + 1]
        if not value or value.startswith("-"):
            return None
        index += 2
    return None


def wrap_invocation_from_namespace(
    args: argparse.Namespace,
    *,
    agenttalk_argv: Sequence[str] | None = None,
) -> WrapInvocation:
    """Normalize a namespace produced by the shared grammar."""

    child_argv = list(getattr(args, "cmd", None) or [])
    if child_argv and child_argv[0] == "--":
        child_argv = child_argv[1:]
    return WrapInvocation(
        root=getattr(args, "root", None),
        supervisor_launch_nonce=getattr(args, "supervisor_launch_nonce", None),
        agent=getattr(args, "agent", None),
        cli=str(getattr(args, "cli", "codex")),
        sender=getattr(args, "sender", None),
        lane_id=getattr(args, "lane_id", None),
        min_interval=float(getattr(args, "min_interval", 5.0)),
        no_render=bool(getattr(args, "no_render", False)),
        model=getattr(args, "model", None),
        effort=getattr(args, "effort", None),
        loop=bool(getattr(args, "loop", False)),
        lead_loop=bool(getattr(args, "lead_loop", False)),
        one_shot=bool(getattr(args, "one_shot", False)),
        to_request=getattr(args, "to_request", None),
        dead_letter_max_attempts=getattr(args, "dead_letter_max_attempts", None),
        dead_letter_escalate_after=getattr(args, "dead_letter_escalate_after", None),
        child_argv=tuple(child_argv),
        option_occurrences=(
            *getattr(args, "_agenttalk_global_occurrences", ()),
            *getattr(args, "_wrap_option_occurrences", ()),
        ),
        agenttalk_argv=(tuple(agenttalk_argv) if agenttalk_argv is not None else None),
    )


def validate_standalone_wrap(args: argparse.Namespace | WrapInvocation) -> WrapParseResult:
    """Apply the existing ``cmd_wrap`` runtime-shape contract."""

    invocation = args if isinstance(args, WrapInvocation) else wrap_invocation_from_namespace(args)
    if not invocation.child_argv:
        return WrapRefusal(
            "child_command_missing",
            "agenttalk wrap: a launch command is required after `--`",
        )
    if invocation.child_argv[0] == "":
        return WrapRefusal(
            "child_executable_empty",
            "agenttalk wrap: the child executable after `--` must not be empty",
        )
    if invocation.cli not in ("codex", "claude"):
        return WrapRefusal(
            "unsupported_cli",
            f"agenttalk wrap: no wrapper adapter for cli {invocation.cli!r}",
        )
    if invocation.one_shot and not invocation.loop:
        return WrapRefusal(
            "one_shot_without_loop",
            "agenttalk wrap: --one-shot requires --loop",
        )
    if invocation.one_shot and not invocation.to_request:
        return WrapRefusal(
            "one_shot_without_request",
            "agenttalk wrap: --one-shot requires --to-request <id>",
        )
    if invocation.lead_loop and not invocation.loop:
        return WrapRefusal(
            "lead_loop_without_loop",
            "agenttalk wrap: --lead-loop requires --loop",
        )
    if invocation.lead_loop and invocation.one_shot:
        return WrapRefusal(
            "lead_loop_one_shot",
            "agenttalk wrap: --lead-loop is a continuous controller; "
            "it cannot be combined with --one-shot",
        )
    return invocation


class _ParseExit(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class _AdmissionParser(argparse.ArgumentParser):
    def exit(self, status: int = 0, message: str | None = None) -> None:
        raise _ParseExit(status, message or "")

    def error(self, message: str) -> None:
        raise _ParseExit(2, message)


def parse_wrap_command(command_args: Sequence[str]) -> WrapParseResult:
    """Parse wrapper options and child argv with the real CLI grammar.

    ``command_args`` begins immediately after the ``wrap`` subcommand.  Parser
    help and syntax errors become data so admission has no output side effects.
    """

    parser = _AdmissionParser(prog="agenttalk wrap")
    add_wrap_arguments(parser)
    try:
        with (
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            args = parser.parse_args(list(command_args))
    except _ParseExit as exc:
        code = "help_requested" if exc.status == 0 else "wrapper_parse_error"
        detail = exc.message.strip() or (
            "help requested" if exc.status == 0 else "invalid wrapper arguments"
        )
        return WrapRefusal(code, f"agenttalk wrap: {detail}")
    return validate_standalone_wrap(args)


def parse_agenttalk_wrap_command(own_argv: Sequence[str]) -> WrapParseResult:
    """Parse argv after the interpreter prefix through the closed CLI grammar.

    Global launch options and the ``wrap`` subcommand are parsed together, so a
    caller never has to scan for global option values or guess where wrapper
    option parsing starts.
    """

    parser = _AdmissionParser(prog="agenttalk")
    add_agenttalk_launch_arguments(parser)
    subparsers = parser.add_subparsers(dest="_launch_subcommand", required=True)
    wrap_parser = subparsers.add_parser("wrap")
    add_wrap_arguments(wrap_parser)
    try:
        with (
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            args = parser.parse_args(list(own_argv))
    except _ParseExit as exc:
        code = "help_requested" if exc.status == 0 else "wrapper_parse_error"
        detail = exc.message.strip() or (
            "help requested" if exc.status == 0 else "invalid wrapper arguments"
        )
        return WrapRefusal(code, f"agenttalk wrap: {detail}")
    return validate_standalone_wrap(
        wrap_invocation_from_namespace(args, agenttalk_argv=own_argv)
    )


def apply_supervisor_policy(
    result: WrapParseResult,
    policy: SupervisorWrapPolicy,
) -> WrapParseResult:
    """Apply one population's explicit policy to a shared accepted parse."""

    if isinstance(result, WrapRefusal):
        return result
    for occurrence in result.option_occurrences:
        if isinstance(occurrence.value, str) and occurrence.value == "":
            return WrapRefusal(
                "empty_wrapper_option_value",
                f"{policy.name}: supervised launch option {occurrence.option!r} has an empty value",
            )
    if not math.isfinite(result.min_interval) or result.min_interval < 0:
        return WrapRefusal(
            "invalid_min_interval",
            f"{policy.name}: --min-interval must be a finite non-negative number",
        )
    for option, limit in (
        ("--dead-letter-max-attempts", result.dead_letter_max_attempts),
        ("--dead-letter-escalate-after", result.dead_letter_escalate_after),
    ):
        if limit is not None and limit < 0:
            return WrapRefusal(
                "invalid_dead_letter_limit",
                f"{policy.name}: {option} must be non-negative",
            )
    if policy.forbid_supervisor_launch_nonce and result.supervisor_launch_nonce is not None:
        return WrapRefusal(
            "reserved_launch_nonce_present",
            f"{policy.name}: supervised launch contains the reserved nonce option",
        )
    if result.child_argv and result.child_argv[0].startswith("-"):
        return WrapRefusal(
            "child_executable_option_like",
            f"{policy.name}: child executable must not look like an option",
        )
    if policy.require_loop and not result.loop:
        return WrapRefusal(
            "wrapped_missing_loop",
            f"{policy.name}: wrapped launch requires --loop before the child delimiter",
        )
    if policy.require_one_shot and not result.one_shot:
        return WrapRefusal(
            "wrapped_missing_one_shot",
            f"{policy.name}: wrapped launch requires --one-shot before the child delimiter",
        )
    if policy.forbid_one_shot and result.one_shot:
        return WrapRefusal(
            "wrapped_unexpected_one_shot",
            f"{policy.name}: wrapped launch must not use --one-shot",
        )
    if policy.forbid_lead_loop and result.lead_loop:
        return WrapRefusal(
            "wrapped_unexpected_lead_loop",
            f"{policy.name}: wrapped launch must not use --lead-loop",
        )
    expected = (
        ("agent", policy.expected_agent, result.agent),
        ("sender", policy.expected_sender, result.sender or result.agent),
        ("cli", policy.expected_cli, result.cli),
        ("request", policy.expected_request_id, result.to_request),
    )
    for field, wanted, actual in expected:
        if wanted is not None and actual != wanted:
            return WrapRefusal(
                f"wrapped_{field}_mismatch",
                f"{policy.name}: wrapped launch {field} does not match the admitted value",
            )
    if policy.require_exact_lane and result.lane_id != policy.expected_lane_id:
        return WrapRefusal(
            "wrapped_lane_mismatch",
            f"{policy.name}: wrapped launch lane does not match the admitted value",
        )
    return result
