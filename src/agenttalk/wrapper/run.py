"""The wrapper's I/O shell: launch a CLI in structured-stream mode, read its
stdout, parse + adapt each line to normalized events, and drive the engine
(heartbeat / render / degraded). This is the ONLY wrapper module that touches a
subprocess, the console, or the store - everything else is pure and unit-tested
without it.

Testability: the stream SOURCE and all three sinks are injectable. Tests pass a
fake line iterator (no subprocess) and fake sinks; production defaults spawn the
real CLI and wire heartbeat -> store.write_heartbeat and degraded-escalation ->
a restart-request marker.
"""

from __future__ import annotations

import codecs
import contextlib
import errno
import io
import json
import os
from pathlib import Path
import re
import shutil
# subprocess spawns ONLY the operator-provided CLI launch command; never shell=True.
import subprocess  # nosec B404
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .turn_watchdog import TurnWatchdogConfig
from datetime import datetime, timezone

from agenttalk import health as health_model
from agenttalk.redaction import normalize_child_output_tail
from agenttalk.store import LEAD_LOOP_LEASE_ENV

from . import claude_adapter, codex_adapter
from .degraded import DegradedConfig, DegradedDetector
from .events import Event, EventType
from .framework import WrapperEngine
from .health import WrapperHealthWriter

# cli -> event mapper.
_ADAPTERS: dict[str, Callable[[object], list[Event]]] = {
    "codex": codex_adapter.map_event,
    "claude": claude_adapter.map_event,
}
# Codex has no observed real tool-call-leak signature yet -> detect + log, never
# escalate. Claude's leak (tool-call markup as assistant text) is the cataloged
# high-confidence signature -> escalation-enabled.
_TELEMETRY_ONLY = {"codex": True, "claude": False}
_NO_CHILD_WINDOW_ENV = "AGENTTALK_NO_CHILD_WINDOW"
WRAPPER_GENERATION_ENV = "AGENTTALK_WRAPPER_GENERATION"
INBOUND_REQUEST_ID_ENV = "AGENTTALK_INBOUND_REQUEST_ID"
_WRAPPER_LOG_ENV_NAMES = {
    "AGENTTALK_WRAPPER_STDOUT_LOG",
    "AGENTTALK_WRAPPER_STDERR_LOG",
    "AGENTTALK_WRAPPER_LOG_MAX_BYTES",
    "AGENTTALK_WRAPPER_LOG_SEGMENTS",
    "AGENTTALK_WRAPPER_LOG_NONCE",
}
OVH_QWEN_CLAUDE_MAX_OUTPUT = "4096"
_BENIGN_PIPE_TEARDOWN_ERRNOS = {errno.EINVAL, errno.EPIPE}
_WATCHDOG_STREAM_POLL_SECONDS = 0.05
_WATCHDOG_STREAM_READ_BYTES = 64 * 1024


class _GatewayChildCapUnavailable(RuntimeError):
    """The wrapper could not durably issue a scoped paid-child credential.

    ``transient`` marks an operator-resolvable gateway HOLD (a durable accounting hold, an
    unresolved prior attempt, or a clock rollback awaiting reconcile - i.e. any ``LedgerHold``).
    A transient cause classifies CLASS_GATEWAY_HELD: the head is parked WITHOUT persisting an
    attempt and re-drives every poll, so a worker blocked only because the gateway is held
    self-heals when the hold clears, instead of sticky-parking as config_blocked forever (#62).
    A NON-transient cause (missing message id, a local exec/config denial, ChildTurnCapExceeded
    - which is a LedgerBlocked, deliberately NOT a LedgerHold) stays config_blocked."""

    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient

# Legacy infra-like text markers. These are low-confidence hints only; they deliberately
# no longer classify known_global_infra without a structured CLI status/rate-limit fact.
# NOTE: bare "blocked" is DELIBERATELY excluded - it would shadow the content-policy
# poison markers ("blocked by safety" / "blocked by content policy").
_INFRA_ERROR_MARKERS = (
    "rate limit", "rate_limit", "ratelimit", "too many requests", "429", "529",
    "overloaded", "quota", "usage limit", "capacity",
    "timeout", "timed out", "deadline exceeded",
    "unavailable", "service unavailable", "temporarily", "try again",
    "internal server error", "bad gateway", "gateway timeout", " 500", " 502", " 503",
    " 504", "http 5", "5xx",
    "connection", "network", "econn", "reset by peer", "broken pipe",
    "unauthorized", "authentication", "invalid api key", "api key", "credential",
    "401", "403",
    # edge / WAF / reverse-proxy block (lead C4(b)): a gateway/proxy/firewall block is an
    # outage signature, not message-content - err toward INFRA (retry + escalate, never DL@3).
    "forbidden", "waf", "firewall", "proxy", "upstream",
    # token/quota RATE windows (lead 4th-verify P1 #2 + codex expansion): "maximum number of
    # tokens per minute exceeded" / "...per day" / "TPM" is a rate/quota LIMIT (outage), NOT
    # this message being too big - infra-first so it never false-DLs a healthy message during a
    # rate window. ("quota" + "usage limit" are already infra markers above.)
    "per minute", "per day", "per hour", "tokens per", "tpm", "daily token",
)
_STRUCTURED_API_INFRA_STATUSES = frozenset({429, 529})
_STRUCTURED_AUTH_STATUSES = frozenset({401, 403})
_STRUCTURED_AUTH_MARKERS = (
    "auth", "authentication", "unauthorized", "credential", "api_key", "api key",
)


_AGENTTALK_COMMAND_TOKENS = frozenset({"agenttalk", "agenttalk.cmd", "agenttalk.exe"})
_PYTHON_COMMAND_TOKENS = frozenset({"python", "python.exe", "python3", "python3.exe", "py", "py.exe"})
_PYTHON_ENV_COMMAND_TOKENS = frozenset({
    "$env:agenttalk_py",
    "%agenttalk_py%",
    "$agenttalk_py",
    "${agenttalk_py}",
    # PowerShell's braced env form. The bash `${agenttalk_py}` spelling was already
    # here, so omitting this one left `& ${env:AGENTTALK_PY} -m agenttalk reply`
    # unrecognized -- the same silent-loss class as the parenthesized form.
    "${env:agenttalk_py}",
})
_PY_LAUNCHER_COMMAND_TOKENS = frozenset({"py", "py.exe"})
_PYTHON_VERSIONED_BASENAME_RE = re.compile(
    r"^python\d+(?:\.\d+)*(?:\.exe)?$",
    re.IGNORECASE,
)
_POWERSHELL_COMMAND_TOKENS = frozenset({"pwsh", "pwsh.exe", "powershell", "powershell.exe"})
_CMD_COMMAND_TOKENS = frozenset({"cmd", "cmd.exe"})
_POSIX_SHELL_COMMAND_TOKENS = frozenset({"sh", "sh.exe", "bash", "bash.exe"})
_PYTHON_SEPARATED_VALUE_OPTIONS = frozenset({"-X", "-W", "-Q", "--check-hash-based-pycs"})
_PYTHON_TERMINATING_OPTIONS = frozenset({
    "-?", "-h", "--help", "--help-all", "--help-env", "--help-xoptions", "-V", "--version",
})
_PYTHON_FLAG_CHARS = frozenset("bBdEiIOPqRsSuvx")
_PY_LAUNCHER_SELECTOR_RE = re.compile(r"^-(?:0|32|64|\d+(?:\.\d+)?(?:-(?:32|64))?)$")
_REQUIRED_BUS_WRITE_VERBS = frozenset({"reply", "send", "escalate"})
_BEST_EFFORT_BUS_VERBS = frozenset({"composing"})
_CHECK_BUS_VERBS = frozenset({"check"})
_CLASSIFIED_BUS_VERBS = _REQUIRED_BUS_WRITE_VERBS | _BEST_EFFORT_BUS_VERBS | _CHECK_BUS_VERBS
_BUS_GLOBAL_OPTIONS_WITH_VALUE = frozenset({"--root"})
BUS_KIND_OK_OR_NO_SIGNAL = "ok_or_no_signal"
BUS_KIND_CONFIG_BLOCKED = "config_blocked"
BUS_KIND_SEMANTIC_FAILURE = "bus_write_semantic_failure"
BUS_KIND_UNKNOWN_FAILURE = "bus_write_unknown_failure"
BUS_KIND_AMBIGUOUS_TRANSIENT = "ambiguous_transient"
SETUP_KIND_FAILURE = "setup_failure"
SETUP_WORKTREE_BRANCH_ALREADY_CHECKED_OUT = "worktree_branch_already_checked_out"
_FAILED_TOOL_STATUSES = frozenset({
    "failed", "failure", "error", "errored", "cancelled", "canceled",
    "timeout", "timed_out",
})
_INTERPRETER_NOT_FOUND_RE = re.compile(
    r"(?<![\w.])(?:python(?:3)?|py)(?:\.exe)?\b.*"
    r"(not recognized|command not found|no such file|"
    r"filenotfounderror|winerror 2|createprocess error 2|could not be started)|"
    r"(not recognized|command not found|no such file).*"
    r"(?<![\w.])(?:python(?:3)?|py)(?:\.exe)?\b",
    re.IGNORECASE,
)
_AGENTTALK_ARGPARSE_RE = re.compile(
    r"usage:\s*(?:python\s+-m\s+)?agenttalk\s+[a-z0-9_-]+.*"
    r"(error:|invalid choice|unrecognized arguments)",
    re.IGNORECASE | re.DOTALL,
)
_AGENTTALK_VALIDATION_MARKERS = (
    "unknown request id",
    "no validated message carries that request_id",
    "no validated message carries",
    "recipient-domain",
    "recipient domain",
    "hmac",
    "signing",
    "retired-recipient",
    "retired recipient",
)
_AGENTTALK_CHECK_SAFETY_MARKERS = (
    "rescinded",
    "superseded",
    "unknown request",
    "unknown request id",
)
_EXEC_DENIAL_MARKERS = (
    "access is denied",
    "permission denied",
    "winerror 5",
    "win error 5",
    "createprocess error 5",
    "eacces",
)
_EXEC_DENIAL_CODE_RE = re.compile(r"\b(?:os error|error)\s+5\b", re.IGNORECASE)
_AGENTTALK_RUNTIME_RE = re.compile(
    r"\b(import\s+agenttalk|agenttalk\.__file__|agenttalk runtime|agenttalk source|"
    r"agenttalk package|src[\\/]+agenttalk[\\/]+__init__\.py|"
    r"agenttalk[\\/]+__init__\.py)\b",
    re.IGNORECASE,
)
_OUT_OF_WORKSPACE_SOURCE_MARKERS = (
    "out-of-workspace",
    "outside the workspace",
    "outside workspace",
    "sibling source",
)
_AGENTTALK_IMPORT_PROBE = (
    "import agenttalk,json; "
    "print(json.dumps({'file': getattr(agenttalk, '__file__', None)}))"
)
_AGENTTALK_MISSING_MODULE_RE = re.compile(
    r"(modulenotfounderror:.*no module named ['\"]?agenttalk['\"]?|"
    r"no module named ['\"]?agenttalk['\"]?)",
    re.IGNORECASE,
)
_AGENTTALK_BROKEN_IMPORT_RE = re.compile(
    r"(syntaxerror|importerror|traceback).*"
    r"(agenttalk[\\/]+__init__\.py|src[\\/]+agenttalk[\\/]+__init__\.py|"
    r"import\s+agenttalk)",
    re.IGNORECASE | re.DOTALL,
)
_WINDOWS_SHIM_EXTENSIONS = frozenset({".cmd", ".bat", ".ps1"})
_SPAWN_CONFIG_WINERRORS = frozenset({2, 3, 193, 267})
_NPM_CODEX_WIN32_X64_TRIPLES = frozenset({"x86_64-pc-windows-msvc"})


@dataclass(frozen=True)
class LaunchPreflightResult:
    argv: list[str]
    blocked: str | None = None


def _compact_text(value: object, limit: int = 240) -> str:
    return " ".join(str(value or "").split())[:limit]


def _format_argv(argv: list[str]) -> str:
    return " ".join(str(part) for part in argv)


def _command_text(command: object) -> str:
    if isinstance(command, (list, tuple)):
        return " ".join(str(part) for part in command)
    return str(command or "")


def _split_command_string(text: str) -> list[str] | None:
    tokens: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    in_token = False
    idx = 0
    while idx < len(text):
        ch = text[idx]
        if quote is not None:
            if ch == "\\" and idx + 1 < len(text) and text[idx + 1] == quote:
                buf.append(text[idx + 1])
                idx += 2
                continue
            if ch == quote:
                if idx + 1 < len(text) and text[idx + 1] == quote:
                    buf.append(ch)
                    idx += 2
                    continue
                quote = None
            else:
                buf.append(ch)
            idx += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            in_token = True
        elif ch.isspace():
            if in_token:
                tokens.append("".join(buf))
                buf = []
                in_token = False
        else:
            buf.append(ch)
            in_token = True
        idx += 1
    if quote is not None:
        return None
    if in_token:
        tokens.append("".join(buf))
    return tokens


def _command_tokens(command: object) -> list[str] | None:
    if isinstance(command, (list, tuple)):
        return [str(part) for part in command]
    return _split_command_string(str(command or ""))


def _clean_argument_token(token: str) -> str:
    return token.strip().strip("'\"").strip("),;")


def _clean_program_token(token: str) -> str:
    clean = _clean_argument_token(token).lstrip("&")
    if "(" in clean:
        clean = clean.rsplit("(", 1)[-1]
    return clean.strip("'\"()[]{}")


def _program_basename(token: str) -> str:
    clean = _clean_program_token(token)
    return clean.replace("\\", "/").rsplit("/", 1)[-1].casefold()


def _python_program_kind(token: str) -> str | None:
    env_token = _clean_argument_token(token).lstrip("&").casefold()
    base = _program_basename(token)
    # A PowerShell call tail can parenthesize the interpreter (`& ($env:AGENTTALK_PY)`),
    # leaving a leading paren that `_clean_argument_token()` does not strip -- so compare
    # the fully cleaned program form too. Missing this makes `_bus_command_verb()` return
    # None for a real `agenttalk reply`, and a failed bus write then goes unattributed:
    # exactly the commit-without-a-durable-reply this module exists to prevent.
    if env_token in _PYTHON_ENV_COMMAND_TOKENS or base in _PYTHON_ENV_COMMAND_TOKENS:
        return "python"
    if base in _PY_LAUNCHER_COMMAND_TOKENS:
        return "py"
    if base in _PYTHON_COMMAND_TOKENS or _PYTHON_VERSIONED_BASENAME_RE.fullmatch(base):
        return "python"
    return None


def _is_agenttalk_program(token: str) -> bool:
    return _program_basename(token) in _AGENTTALK_COMMAND_TOKENS


def _drop_shell_call_prefix(tokens: list[str]) -> list[str]:
    while tokens and _clean_argument_token(tokens[0]) == "&":
        tokens = tokens[1:]
    return tokens


def _is_python_attached_value_option(arg: str) -> bool:
    return (
        (arg.startswith("-X") and arg != "-X")
        or (arg.startswith("-W") and arg != "-W")
        or (arg.startswith("-Q") and arg != "-Q")
        or arg.startswith("--check-hash-based-pycs=")
    )


def _is_python_flag_cluster(arg: str) -> bool:
    return (
        len(arg) > 1
        and arg.startswith("-")
        and not arg.startswith("--")
        and all(ch in _PYTHON_FLAG_CHARS for ch in arg[1:])
    )


def _is_py_launcher_selector(arg: str) -> bool:
    return _PY_LAUNCHER_SELECTOR_RE.match(arg) is not None or arg.startswith("-V:")


def _bus_command_verb_after_agenttalk(args: list[str]) -> str | None:
    idx = 0
    while idx < len(args):
        token = _clean_argument_token(args[idx])
        if not token:
            idx += 1
            continue
        low = token.casefold()
        if any(low.startswith(f"{opt}=") for opt in _BUS_GLOBAL_OPTIONS_WITH_VALUE):
            idx += 1
            continue
        if low in _BUS_GLOBAL_OPTIONS_WITH_VALUE:
            if idx + 1 >= len(args):
                return None
            idx += 2
            continue
        if low.startswith("-"):
            return None
        if any(
            _clean_argument_token(arg).casefold() in {"-h", "--help"}
            for arg in args[idx + 1:]
        ):
            # argparse treats either token as the help action even when it
            # follows a write-shaped subcommand. Help never attempts a write.
            return None
        return low
    return None


def _python_module_agenttalk_verb(args: list[str], program_kind: str) -> str | None:
    # Scan only until Python's execution target is known. "-m agenttalk" counts
    # before any target; "-m" after a script, "-c", "--", or stdin belongs to
    # that target and must not classify a normal Python command as a bus write.
    # Unknown dash-options fail open instead of scanning past them to a later "-m".
    idx = 0
    while idx < len(args):
        arg = _clean_argument_token(args[idx])
        if arg == "-m":
            if idx + 1 >= len(args):
                return None
            if _clean_argument_token(args[idx + 1]).casefold() != "agenttalk":
                return None
            return _bus_command_verb_after_agenttalk(args[idx + 2:])
        if arg.startswith("-m") and arg != "-m":
            if arg[2:].casefold() != "agenttalk":
                return None
            return _bus_command_verb_after_agenttalk(args[idx + 1:])
        if arg == "-c" or (arg.startswith("-c") and arg != "-c"):
            return None
        if arg in _PYTHON_TERMINATING_OPTIONS:
            return None
        if arg in ("--", "-"):
            return None
        if program_kind == "py" and _is_py_launcher_selector(arg):
            idx += 1
            continue
        if arg in _PYTHON_SEPARATED_VALUE_OPTIONS:
            if idx + 1 >= len(args):
                return None
            idx += 2
            continue
        if _is_python_attached_value_option(arg):
            idx += 1
            continue
        if _is_python_flag_cluster(arg):
            idx += 1
            continue
        if arg.startswith("-"):
            return None
        return None
    return None


def _launcher_payload_tokens(program: str, args: list[str]) -> list[str] | None:
    base = _program_basename(program)
    if base in _POWERSHELL_COMMAND_TOKENS:
        switches = {"-command", "-c"}
    elif base in _CMD_COMMAND_TOKENS:
        switches = {"/c"}
    elif base in _POSIX_SHELL_COMMAND_TOKENS:
        switches = {"-c", "-lc", "-cl"}
    else:
        return None
    for idx, arg in enumerate(args):
        if _clean_argument_token(arg).casefold() in switches:
            return args[idx + 1:] or None
    return None


def _bus_command_verb_from_tokens(tokens: list[str], depth: int = 0) -> str | None:
    tokens = _drop_shell_call_prefix(tokens)
    if not tokens:
        return None
    program, args = tokens[0], tokens[1:]
    if _is_agenttalk_program(program):
        return _bus_command_verb_after_agenttalk(args)
    python_kind = _python_program_kind(program)
    if python_kind is not None:
        return _python_module_agenttalk_verb(args, python_kind)
    if depth >= 3:
        return None
    payload = _launcher_payload_tokens(program, args)
    if payload is None:
        return None
    if len(payload) == 1:
        payload = _split_command_string(payload[0])
        if payload is None:
            return None
    # Recognition completeness line: standard direct/python -m/py/one-level launcher
    # forms with --root and normal \" / "" quoting are classified. Unknown globals,
    # unknown interpreters, multi-level mixed escaping, pathological quotes, and
    # launcher payloads that only echo/search an agenttalk string deliberately fail
    # open; a masked odd reply is safer than false-disposing a healthy message.
    return _bus_command_verb_from_tokens(payload, depth + 1)


def _agenttalk_py() -> str:
    return str(Path(sys.executable).resolve())


def _ovh_qwen_claude_config_dir(workspace: Path) -> str:
    profile = (workspace / ".agenttalk" / "gateway" / "claude-profile").resolve()
    try:
        profile.relative_to(workspace)
    except ValueError as exc:
        raise ValueError(
            "ovh-qwen Claude profile must resolve inside the project workspace"
        ) from exc
    return str(profile)


def _child_env(
    workspace_root: str | os.PathLike[str] | None = None,
    *,
    wrapper_generation: str | None = None,
    inbound_request_id: str | None = None,
    backend_profile: str | None = None,
    profile_env: dict[str, str] | None = None,
    gateway_capability: str | None = None,
) -> dict[str, str]:
    workspace = Path(workspace_root).resolve() if workspace_root else _workspace_root()
    if backend_profile == "ovh-qwen":
        allowed_names = {
            "path",
            "systemroot",
            "windir",
            "temp",
            "tmp",
            "pythonutf8",
            "pythonioencoding",
        }
        env = {
            key: value
            for key, value in os.environ.items()
            if key.casefold() in allowed_names or key.upper().startswith("AGENTTALK_")
        }
        injected = dict(profile_env or {})
        allowed_injected = {
            "ANTHROPIC_BASE_URL",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_MODEL",
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
        }
        unexpected = set(injected) - allowed_injected
        if unexpected:
            raise ValueError(f"ovh-qwen profile env contains unsupported keys: {sorted(unexpected)}")
        if injected.get("ANTHROPIC_BASE_URL") != "http://127.0.0.1:4000":
            raise ValueError("ovh-qwen requires the pinned loopback gateway URL")
        if not injected.get("ANTHROPIC_AUTH_TOKEN"):
            raise ValueError("ovh-qwen requires an injected front token")
        if injected.get("ANTHROPIC_MODEL") not in {None, "Qwen3.5-397B-A17B"}:
            raise ValueError("ovh-qwen requires the pinned model alias")
        injected["ANTHROPIC_MODEL"] = "Qwen3.5-397B-A17B"
        if injected.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS") not in {
            None,
            OVH_QWEN_CLAUDE_MAX_OUTPUT,
        }:
            raise ValueError("ovh-qwen requires the pinned output-token cap")
        injected["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = (
            OVH_QWEN_CLAUDE_MAX_OUTPUT
        )
        injected["MAX_THINKING_TOKENS"] = "0"
        # The front token is a controller-only mint credential. It must not
        # reach executable preflight or the model child. A paid child receives
        # only the scoped capability minted immediately before its spawn.
        injected.pop("ANTHROPIC_AUTH_TOKEN")
        if gateway_capability is not None:
            from agenttalk.ovh_gateway import child_capability_from_header

            if child_capability_from_header(f"Bearer {gateway_capability}") is None:
                raise ValueError("ovh-qwen child turn capability is malformed")
            injected["ANTHROPIC_AUTH_TOKEN"] = gateway_capability
        # Never inherit provider credentials or an ambient Claude token. The
        # controller starts with the issuer token; the real paid child gets
        # only its message-scoped capability instead.
        for key in list(env):
            if key.casefold() in {
                "anthropic_api_key",
                "anthropic_auth_token",
                "ovh_key",
            }:
                env.pop(key, None)
        for key in (
            LEAD_LOOP_LEASE_ENV,
            WRAPPER_GENERATION_ENV,
            INBOUND_REQUEST_ID_ENV,
            *_WRAPPER_LOG_ENV_NAMES,
        ):
            env.pop(key, None)
        env.update(injected)
        # Claude Code must not discover the operator's normal ~/.claude profile
        # through the same-user OS home fallback. This workspace-scoped profile
        # is disposable with the watched-trial clone and contains no operator
        # credentials or history.
        env["CLAUDE_CONFIG_DIR"] = _ovh_qwen_claude_config_dir(workspace)
        # The allowlist above drops HOME/USERPROFILE/LOCALAPPDATA/APPDATA, so a bus
        # command the child shells out to (e.g. `agenttalk reply`) cannot resolve
        # Path.home() and crashes in signing.default_keys_dir() BEFORE it can even
        # decide signing is off ("RuntimeError: Could not determine home directory").
        # Point the full home set at the disposable workspace clone: gives the child a
        # resolvable home WITHOUT leaking the operator's real home to the paid external
        # worker. LOCALAPPDATA is what agenttalk's own path resolution reads today;
        # APPDATA (Roaming) is included so Windows-native child tooling that consults
        # %APPDATA% (npm, credential helpers, Python user site) also stays scoped
        # to the clone instead of falling back to the operator profile. If HMAC signing
        # is later enforced, wire the child's key via AGENTTALK_HMAC_KEY_FILE rather than
        # widening this to the operator's LOCALAPPDATA.
        _scoped_home = str(workspace)
        env["HOME"] = _scoped_home
        env["USERPROFILE"] = _scoped_home
        env["LOCALAPPDATA"] = str(workspace / "AppData" / "Local")
        env["APPDATA"] = str(workspace / "AppData" / "Roaming")
    elif backend_profile is not None:
        raise ValueError(f"unsupported backend_profile {backend_profile!r}")
    else:
        # Preserve the historical environment for every ordinary backend except
        # controller-only ownership/diagnostic markers. Wrapper-log paths and the
        # launch nonce belong to this process; model children must not reopen or
        # truncate the ring, or learn an operator-local diagnostic path.
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in {
                LEAD_LOOP_LEASE_ENV,
                WRAPPER_GENERATION_ENV,
                INBOUND_REQUEST_ID_ENV,
                *_WRAPPER_LOG_ENV_NAMES,
            }
        }
    env["AGENTTALK_PY"] = _agenttalk_py()
    env["AGENTTALK_ROOT"] = str(workspace)
    if isinstance(wrapper_generation, str) and wrapper_generation:
        env[WRAPPER_GENERATION_ENV] = wrapper_generation
    if isinstance(inbound_request_id, str) and inbound_request_id:
        env[INBOUND_REQUEST_ID_ENV] = inbound_request_id
    return env


def _child_creationflags(
    env: dict[str, str] | None = None,
    *,
    is_windows: bool | None = None,
    create_no_window: int | None = None,
) -> int:
    """Windows creation flags for wrapper-spawned CLI children.

    The supervisor uses ``Start-Process -WindowStyle Hidden`` for the wrapper
    process and sets ``AGENTTALK_NO_CHILD_WINDOW=1`` so the wrapper's own Popen
    child cannot create a visible console.
    """
    if is_windows is None:
        is_windows = os.name == "nt"
    if not is_windows:
        return 0
    marker = (env or os.environ).get(_NO_CHILD_WINDOW_ENV)
    if str(marker or "").strip().lower() not in {"1", "true", "yes", "on"}:
        return 0
    flag = create_no_window if create_no_window is not None else getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return int(flag or 0)


def _child_window_kwargs(env: dict[str, str] | None = None) -> dict[str, int]:
    flags = _child_creationflags(env)
    return {"creationflags": flags} if flags else {}


def _is_benign_pipe_teardown_error(exc: OSError) -> bool:
    """Windows overlapped stdout pipes can raise during EOF teardown."""
    return exc.errno in _BENIGN_PIPE_TEARDOWN_ERRNOS


def _iter_suppressing_benign_pipe_teardown(lines: Iterable[str]) -> Iterator[str]:
    try:
        yield from lines
    except OSError as exc:
        if not _is_benign_pipe_teardown_error(exc):
            raise


def _close_pipe_suppressing_benign_pipe_teardown(pipe) -> None:
    if pipe is None:
        return
    if getattr(pipe, "closed", False):
        return
    try:
        pipe.close()
    except OSError as exc:
        if not _is_benign_pipe_teardown_error(exc):
            raise


def _read_ready_pipe_chunk(pipe) -> bytes | None:
    """Read one available binary chunk without waiting for a line or pipe EOF.

    ``None`` means the pipe is still open but has no bytes ready; ``b""`` means
    EOF.  The watchdog stream uses this instead of a blocking TextIO iterator so
    an inherited writer cannot strand a reader thread after the protected root
    has exited.
    """
    buffered = getattr(pipe, "buffer", pipe)
    raw = getattr(buffered, "raw", buffered)
    fd = raw.fileno()
    if os.name == "nt":
        import _winapi
        import msvcrt

        try:
            available, _message_left = _winapi.PeekNamedPipe(
                msvcrt.get_osfhandle(fd), 0
            )
        except BrokenPipeError:
            return b""
        if available <= 0:
            return None
        size = min(int(available), _WATCHDOG_STREAM_READ_BYTES)
    else:
        import select

        readable, _, _ = select.select([fd], [], [], 0.0)
        if not readable:
            return None
        size = _WATCHDOG_STREAM_READ_BYTES
    try:
        return os.read(fd, size)
    except OSError as exc:
        if _is_benign_pipe_teardown_error(exc):
            return b""
        raise


def _workspace_root() -> Path:
    root = os.environ.get("AGENTTALK_ROOT")
    return Path(root).resolve() if root else Path.cwd().resolve()


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _looks_like_site_package_path(path: Path) -> bool:
    parts = {p.lower() for p in path.parts}
    return "site-packages" in parts or "dist-packages" in parts


def _is_windows_shim(path: Path) -> bool:
    return path.suffix.casefold() in _WINDOWS_SHIM_EXTENSIONS


def _resolve_path_candidate(value: str, *, workspace_root: Path) -> Path | None:
    raw = Path(value)
    if raw.is_absolute():
        candidate = raw
    elif any(sep in value for sep in ("\\", "/")):
        candidate = workspace_root / raw
    else:
        resolved = shutil.which(value)
        candidate = Path(resolved) if resolved else None
    if candidate is None:
        return None
    try:
        candidate = candidate.resolve()
    except OSError:
        candidate = candidate.absolute()
    return candidate


def _resolve_known_npm_codex_shim(shim: Path) -> Path | None:
    """Resolve only the known npm @openai/codex native Windows package layout."""
    starts: list[Path] = []
    parent = shim.parent
    if parent.name.casefold() == ".bin":
        starts.append(parent.parent / "@openai" / "codex")
    else:
        starts.append(parent / "node_modules" / "@openai" / "codex")
    matches: list[Path] = []
    for package_root in starts:
        vendor = package_root / "node_modules" / "@openai" / "codex-win32-x64" / "vendor"
        for triple in _NPM_CODEX_WIN32_X64_TRIPLES:
            candidate = vendor / triple / "bin" / "codex.exe"
            try:
                is_file = candidate.is_file()
                resolved = candidate.resolve()
            except OSError:
                continue
            if is_file:
                matches.append(resolved)
    unique = sorted({str(m): m for m in matches}.values(), key=str)
    return unique[0] if len(unique) == 1 else None


def _launch_blocked_summary(command: str, error: str, remediation: str) -> str:
    return (
        f"command={_compact_text(command)}; error={_compact_text(error)}; "
        f"remediation={remediation}"
    )


def _self_probe_blocked_summary(argv: list[str], rc: int | None, output: str) -> str | None:
    if rc == 0:
        return None
    return _launch_blocked_summary(
        _format_argv(argv),
        output or f"exit={rc if rc is not None else 'unknown'}",
        "ensure AGENTTALK_PY names the Python executable that can run -m agenttalk; "
        "for Codex workspace-write, explicitly opt in to the Python install directory "
        "with codex --add-dir or an equivalent operator-managed writable_roots entry",
    )


def preflight_launch_runtime(
    base_argv: list[str],
    cli: str,
    workspace_root: str | os.PathLike[str] | None = None,
    child_env: dict[str, str] | None = None,
    *,
    runner: Callable[[list[str], str, dict[str, str], float], tuple[int | None, str]] | None = None,
    timeout: float = 5.0,
) -> LaunchPreflightResult:
    """Resolve the per-turn CLI and probe the pinned agenttalk interpreter before any turn."""
    workspace = Path(workspace_root).resolve() if workspace_root else _workspace_root()
    env = dict(child_env) if child_env is not None else _child_env(workspace)
    if not base_argv:
        return LaunchPreflightResult([], _launch_blocked_summary(
            "",
            "missing base launch command",
            "pass the real CLI executable after --",
        ))

    raw0 = str(base_argv[0])
    override = env.get("AGENTTALK_CODEX") if cli == "codex" else None
    candidate = _resolve_path_candidate(str(override or raw0), workspace_root=workspace)
    if candidate is None:
        return LaunchPreflightResult(list(base_argv), _launch_blocked_summary(
            raw0,
            "executable not found on PATH",
            "pass an absolute real CLI executable after -- or set AGENTTALK_CODEX to the native codex.exe",
        ))
    if candidate.is_dir():
        return LaunchPreflightResult(list(base_argv), _launch_blocked_summary(
            str(candidate),
            "resolved launch command is a directory",
            "pass the real CLI executable file after --",
        ))
    if not candidate.exists():
        return LaunchPreflightResult(list(base_argv), _launch_blocked_summary(
            str(candidate),
            "resolved executable does not exist",
            "pass an absolute real CLI executable after --",
        ))
    if _is_windows_shim(candidate):
        native = _resolve_known_npm_codex_shim(candidate) if cli == "codex" else None
        if native is None:
            return LaunchPreflightResult(list(base_argv), _launch_blocked_summary(
                str(candidate),
                "resolved launch command is a .cmd/.bat/.ps1 shim, which is not directly spawnable with shell=False",
                "pass the native executable after -- or set AGENTTALK_CODEX to the native codex.exe",
            ))
        candidate = native

    resolved_argv = [str(candidate), *base_argv[1:]]
    py = env.get("AGENTTALK_PY") or _agenttalk_py()
    probe_argv = [py, "-m", "agenttalk", "--version"]

    def _default_runner(a: list[str], cwd: str, e: dict[str, str], t: float):
        try:
            proc = subprocess.run(  # noqa: S603  # nosec B603
                a, cwd=cwd, env=e, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=t,
            )
            return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
        except OSError as exc:
            return None, f"{type(exc).__name__}: {exc}"
        except subprocess.SubprocessError as exc:
            return None, f"{type(exc).__name__}: {exc}"

    run_probe = runner or _default_runner
    rc, out = run_probe(probe_argv, str(workspace), env, timeout)
    blocked = _self_probe_blocked_summary(probe_argv, rc, out)
    return LaunchPreflightResult(resolved_argv, blocked)


def agenttalk_runtime_config_blocked_summary(
    resolved_file: str | None,
    workspace_root: str | os.PathLike[str] | None = None,
) -> str | None:
    """Return a config-blocked summary when the pinned bus Python resolves badly."""
    workspace = Path(workspace_root).resolve() if workspace_root else _workspace_root()
    if not resolved_file:
        return (
            "command=$env:AGENTTALK_PY -c import agenttalk; error=agenttalk import resolved no "
            "module file; remediation=install agenttalk non-editable into the runtime "
            "Python used by AGENTTALK_PY, or run from the agenttalk workspace when "
            "intentionally developing agenttalk"
        )
    path = Path(resolved_file).resolve()
    if _looks_like_site_package_path(path):
        return None
    if _is_relative_to(path, workspace):
        return None
    return (
        f"command=$env:AGENTTALK_PY -c import agenttalk; resolved_path={_compact_text(path)}; "
        f"workspace={_compact_text(workspace)}; "
        "error=agenttalk runtime resolved to an out-of-workspace source checkout; "
        "remediation=install agenttalk non-editable into the runtime Python used by "
        "AGENTTALK_PY, or run the agent from the agenttalk workspace when "
        "intentionally developing agenttalk"
    )


def preflight_agenttalk_runtime(
    *,
    workspace_root: str | os.PathLike[str] | None = None,
    runner: Callable[[list[str], str, dict[str, str], float], tuple[int | None, str]] | None = None,
    timeout: float = 5.0,
) -> str | None:
    """Probe the pinned Python the child will use for bus commands."""
    workspace = Path(workspace_root).resolve() if workspace_root else _workspace_root()
    env = _child_env(workspace)
    argv = [env["AGENTTALK_PY"], "-c", _AGENTTALK_IMPORT_PROBE]

    def _default_runner(a: list[str], cwd: str, e: dict[str, str], t: float):
        try:
            proc = subprocess.run(  # noqa: S603,S607  # nosec B603
                a, cwd=cwd, env=e, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=t,
            )
            return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
        except OSError as exc:
            return None, f"{type(exc).__name__}: {exc}"
        except subprocess.SubprocessError as exc:
            return None, f"{type(exc).__name__}: {exc}"

    run_probe = runner or _default_runner
    try:
        rc, out = run_probe(argv, str(workspace), env, timeout)
    except OSError as exc:
        blocked = _runtime_config_blocked_summary(str(exc))
        return blocked
    if rc != 0:
        blocked = _runtime_config_blocked_summary(out)
        return blocked
    resolved: str | None = None
    for line in reversed((out or "").splitlines()):
        try:
            data = json.loads(line)
        except (ValueError, TypeError):
            continue
        value = data.get("file") if isinstance(data, dict) else None
        resolved = str(value) if value else None
        break
    if resolved is None:
        return None
    return agenttalk_runtime_config_blocked_summary(resolved, workspace)


def _looks_like_exec_denied(text: str | None) -> bool:
    t = (text or "").lower()
    return any(m in t for m in _EXEC_DENIAL_MARKERS) or _EXEC_DENIAL_CODE_RE.search(t) is not None


def _is_exec_denied_exception(exc: OSError) -> bool:
    return (
        isinstance(exc, PermissionError)
        or getattr(exc, "errno", None) in (errno.EACCES, errno.EPERM)
        or getattr(exc, "winerror", None) == 5
        or _looks_like_exec_denied(str(exc))
    )


def _spawn_config_blocked_subtype(exc: OSError) -> str | None:
    if _is_exec_denied_exception(exc):
        return "exec_denied"
    if (
        isinstance(exc, (FileNotFoundError, NotADirectoryError))
        or getattr(exc, "errno", None) in (errno.ENOENT, errno.ENOEXEC)
        or getattr(exc, "winerror", None) in _SPAWN_CONFIG_WINERRORS
    ):
        return "launch_cli"
    return None


def _spawn_config_blocked_summary(argv: list[str], exc: OSError) -> str | None:
    subtype = _spawn_config_blocked_subtype(exc)
    if subtype is None:
        return None
    return (
        f"subtype={subtype}; "
        f"command={_compact_text(_format_argv(argv))}; "
        f"error={_compact_text(exc)}; "
        "remediation=use $env:AGENTTALK_PY -m agenttalk for in-sandbox bus calls and "
        "configure the real CLI executable after --"
    )


def _terminal_bus_command_candidates(line: str) -> list[str]:
    candidates = [line]
    low = line.casefold()
    for marker in (" while running ", " running "):
        idx = low.find(marker)
        if idx >= 0:
            candidates.append(line[idx + len(marker):])
    return candidates


def _terminal_config_blocked_summary(text: str | None) -> str | None:
    """Detect an OS exec denial tightly attached to a required bus write command."""
    lines = str(text or "").splitlines() or [str(text or "")]
    for idx, line in enumerate(lines):
        command = next(
            (candidate for candidate in _terminal_bus_command_candidates(line)
             if _bus_command_verb(candidate) in _CLASSIFIED_BUS_VERBS),
            None,
        )
        if command is None:
            continue
        window = "\n".join(lines[max(0, idx - 1):idx + 2])
        bus = classify_bus_execution(command, window, "failed")
        if bus["kind"] == BUS_KIND_CONFIG_BLOCKED:
            return bus["summary"]
        if _looks_like_exec_denied(window):
            return (
                f"command={_compact_text(command)}; "
                f"error={_compact_text(window)}; "
                "remediation=use $env:AGENTTALK_PY -m agenttalk for in-sandbox bus calls"
            )
    return None


def _runtime_config_blocked_summary(text: str | None) -> str | None:
    t = str(text or "")
    low = t.lower()
    if "agenttalk" not in low:
        return None
    source_blocked = any(m in low for m in _OUT_OF_WORKSPACE_SOURCE_MARKERS)
    import_denied = _looks_like_exec_denied(low) and _AGENTTALK_RUNTIME_RE.search(t)
    missing_module = _AGENTTALK_MISSING_MODULE_RE.search(t) is not None
    broken_import = _AGENTTALK_BROKEN_IMPORT_RE.search(t) is not None
    if not source_blocked and not import_denied and not missing_module and not broken_import:
        return None
    return (
        f"command=$env:AGENTTALK_PY -c import agenttalk; error={_compact_text(t)}; "
        "remediation=install agenttalk non-editable into the runtime Python used by "
        "AGENTTALK_PY, or run from the agenttalk workspace when intentionally "
        "developing agenttalk"
    )


def _bus_command_verb(command: object) -> str | None:
    tokens = _command_tokens(command)
    if tokens is None:
        return None
    return _bus_command_verb_from_tokens(tokens)


def _raw_command_item(raw_event: object) -> dict:
    if not isinstance(raw_event, dict):
        return {}
    item = raw_event.get("item")
    return item if isinstance(item, dict) else {}


def _raw_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _raw_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _bus_exit_code(exit_status: object, raw_event: object) -> int | None:
    code = _raw_int(exit_status)
    if code is not None:
        return code
    item = _raw_command_item(raw_event)
    for key in ("exit_code", "exitCode", "return_code", "returncode"):
        code = _raw_int(item.get(key))
        if code is not None:
            return code
    return None


def _bus_tool_status(exit_status: object, raw_event: object) -> str | None:
    status = _raw_str(exit_status)
    if status is not None:
        return status
    item = _raw_command_item(raw_event)
    for key in ("status", "state", "outcome"):
        status = _raw_str(item.get(key))
        if status is not None:
            return status
    return None


def _bus_execution_failed(exit_status: object, raw_event: object) -> bool:
    code = _bus_exit_code(exit_status, raw_event)
    if code is not None:
        return code != 0
    item = _raw_command_item(raw_event)
    for key in ("success", "ok"):
        value = item.get(key)
        if isinstance(value, bool):
            return not value
    status = _bus_tool_status(exit_status, raw_event)
    return bool(status and status.casefold() in _FAILED_TOOL_STATUSES)


def _semantic_bus_failure_subtype(verb: str, output: str, exit_code: int | None) -> str | None:
    low = output.lower()
    if verb in _CHECK_BUS_VERBS and exit_code in (3, 4):
        return "check_safety_result"
    if verb in _CHECK_BUS_VERBS and any(m in low for m in _AGENTTALK_CHECK_SAFETY_MARKERS):
        return "check_safety_result"
    if _AGENTTALK_ARGPARSE_RE.search(output):
        return "argparse"
    if any(m in low for m in _AGENTTALK_VALIDATION_MARKERS):
        return "agenttalk_validation"
    return None


def _bus_result(kind: str, subtype: str, summary: str) -> dict[str, str]:
    return {"kind": kind, "subtype": subtype, "summary": summary}


def _setup_result(subtype: str, summary: str) -> dict[str, str]:
    return {"kind": SETUP_KIND_FAILURE, "subtype": subtype, "summary": summary}


def classify_setup_execution(
    command: object,
    output: object,
    exit_status: object = None,
    raw_event: object = None,
) -> dict[str, str] | None:
    """Classify failed setup/tool commands into safe, bounded reason codes."""
    if not _bus_execution_failed(exit_status, raw_event):
        return None
    command_text = _command_text(command)
    text = str(output or "")
    low = f"{command_text}\n{text}".casefold()
    if (
        "already checked out" in low
        and ("worktree" in low or "git worktree" in low)
    ):
        return _setup_result(
            SETUP_WORKTREE_BRANCH_ALREADY_CHECKED_OUT,
            (
                "subtype=worktree_branch_already_checked_out; "
                "remediation=use the existing worktree for that branch, remove the "
                "stale worktree, or create a unique branch for the reassigned work"
            ),
        )
    return None


def classify_bus_execution(
    command: object,
    output: object,
    exit_status: object = None,
    raw_event: object = None,
) -> dict[str, str]:
    """Classify a completed agenttalk bus command without inferring failure from text alone."""
    command_text = _command_text(command)
    verb = _bus_command_verb(command)
    if verb is None:
        return _bus_result(BUS_KIND_OK_OR_NO_SIGNAL, "out_of_scope", "")
    if verb not in _CLASSIFIED_BUS_VERBS:
        return _bus_result(BUS_KIND_OK_OR_NO_SIGNAL, "out_of_scope", "")
    text = str(output or "")
    exit_code = _bus_exit_code(exit_status, raw_event)
    failed = _bus_execution_failed(exit_status, raw_event)
    if not failed:
        return _bus_result(BUS_KIND_OK_OR_NO_SIGNAL, "no_failed_execution_signal", "")
    runtime_blocked = _runtime_config_blocked_summary(text)
    if runtime_blocked:
        return _bus_result(BUS_KIND_CONFIG_BLOCKED, "agenttalk_runtime", runtime_blocked)
    if _INTERPRETER_NOT_FOUND_RE.search(text):
        return _bus_result(
            BUS_KIND_CONFIG_BLOCKED,
            "interpreter_not_found",
            (
                f"command={_compact_text(command_text)}; error={_compact_text(output)}; "
                "remediation=install agenttalk non-editable into the runtime Python "
                "used by AGENTTALK_PY, or fix AGENTTALK_PY for agenttalk bus commands"
            ),
        )
    if _looks_like_exec_denied(text):
        return _bus_result(
            BUS_KIND_CONFIG_BLOCKED,
            "exec_denied",
            (
                f"command={_compact_text(command_text)}; error={_compact_text(output)}; "
                "remediation=use $env:AGENTTALK_PY -m agenttalk and fix sandbox/runtime "
                "permissions for the agenttalk bus command"
            ),
        )
    if verb in _BEST_EFFORT_BUS_VERBS:
        return _bus_result(BUS_KIND_OK_OR_NO_SIGNAL, "best_effort", "")
    semantic = _semantic_bus_failure_subtype(verb, text, exit_code)
    if semantic is not None:
        return _bus_result(
            BUS_KIND_SEMANTIC_FAILURE,
            semantic,
            (
                f"bus_write_semantic_failure subtype={semantic}; "
                f"command={_compact_text(command_text)}; error={_compact_text(output)}; "
                "remediation=correct the agenttalk bus command/request metadata and retry"
            ),
        )
    if verb in _CHECK_BUS_VERBS:
        return _bus_result(BUS_KIND_OK_OR_NO_SIGNAL, "check_non_config", "")
    if verb not in _REQUIRED_BUS_WRITE_VERBS:
        return _bus_result(BUS_KIND_OK_OR_NO_SIGNAL, "out_of_scope", "")
    return _bus_result(
        BUS_KIND_UNKNOWN_FAILURE,
        "required_write_unknown",
        (
            f"bus_write_failed_unknown; command={_compact_text(command_text)}; "
            f"exit_status={exit_code if exit_code is not None else 'failed'}; "
            f"error={_compact_text(output)}; remediation=inspect the bus command "
            "output and retry with a corrected durable write"
        ),
    )


def _looks_like_infra(text: str | None) -> bool:
    """Low-confidence legacy text marker; never authoritative for known infra."""
    t = (text or "").lower()
    return any(m in t for m in _INFRA_ERROR_MARKERS)


def _int_status(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            return int(stripped)
    return None


def _first_status(*values: object) -> int | None:
    for value in values:
        status = _int_status(value)
        if status is not None:
            return status
    return None


def _dict_or_empty(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _claude_error_fact(ev: Event) -> dict | None:
    """Extract structured Claude API facts that can classify known infra."""
    raw = ev.raw if isinstance(ev.raw, dict) else {}
    typ = raw.get("type")
    if typ == "rate_limit_event":
        info = _dict_or_empty(raw.get("rate_limit_info"))
        return {
            "source": "claude",
            "kind": "rate_limit_event",
            "retryable": bool(ev.retryable),
            "status": info.get("status"),
            "rateLimitType": info.get("rateLimitType") or info.get("rate_limit_type"),
            "utilization": info.get("utilization"),
            "retry_after": (
                info.get("retry_after")
                or info.get("retryAfter")
                or raw.get("retry_after")
                or raw.get("retryAfter")
            ),
        }
    if typ != "result" or not raw.get("is_error"):
        return None
    err = _dict_or_empty(raw.get("error"))
    api_error = _dict_or_empty(raw.get("api_error"))
    response = _dict_or_empty(raw.get("response"))
    status = _first_status(
        raw.get("api_error_status"),
        raw.get("status"),
        raw.get("status_code"),
        err.get("api_error_status"),
        err.get("status"),
        err.get("status_code"),
        api_error.get("status"),
        api_error.get("status_code"),
        response.get("status"),
        response.get("status_code"),
    )
    return {
        "source": "claude",
        "kind": "result",
        "api_error_status": status,
        "subtype": raw.get("subtype") or err.get("subtype") or api_error.get("subtype"),
        "retryable": bool(ev.retryable),
    }


def _structured_auth_outage(fact: dict) -> bool:
    status = fact.get("api_error_status")
    subtype = str(fact.get("subtype") or "").casefold()
    return status in _STRUCTURED_AUTH_STATUSES and any(
        marker in subtype for marker in _STRUCTURED_AUTH_MARKERS
    )


def _structured_infra_summary(sig: dict) -> str | None:
    for fact in sig.get("structured_errors") or []:
        if not isinstance(fact, dict):
            continue
        if fact.get("kind") == "rate_limit_event" and fact.get("retryable") is True:
            parts = ["structured retryable rate_limit_event"]
            for key in ("status", "rateLimitType", "utilization", "retry_after"):
                value = fact.get(key)
                if value not in (None, ""):
                    parts.append(f"{key}={_compact_text(value, 80)}")
            return "; ".join(parts)
        if fact.get("kind") != "result":
            continue
        status = fact.get("api_error_status")
        if (
            status in _STRUCTURED_API_INFRA_STATUSES
            or (isinstance(status, int) and 500 <= status <= 599)
            or _structured_auth_outage(fact)
        ):
            subtype = _compact_text(fact.get("subtype"), 80)
            suffix = f" subtype={subtype}" if subtype else ""
            return f"structured api error status={status}{suffix}"
    return None


# POSITIVE, message-CONTENT-attributable poison signatures (codex marker-conservatism
# ruling). A terminal failure is low-cap poison@3 ONLY with explicit evidence THIS MESSAGE's
# content is the deterministic cause (re-delivering it fails identically while OTHER messages
# succeed). Kept NARROW + unambiguous via TWO direct families + TWO qualified tokens; EVERY
# generic/global string (invalid request, malformed, 422, bare "too long", bare "violates")
# defaults to AMBIGUOUS, never poison - they also fire for GLOBAL config/outage/rate-limit
# problems that are NOT message-poison (recurring marker-breadth class, 3rd instance).
_POISON_SIZE_MARKERS = (             # DIRECT: THIS message is too large (others fit)
    "context length", "context window", "maximum context", "context_length",
    "input too large", "message too large", "request too large", "payload too large",
    # The descriptive 413 phrasings stay DIRECT (they are inherently request-size-qualified);
    # bare "413" is handled by the QUALIFIED clause below, never as a raw substring (codex).
    "request entity too large",
    # NOTE: bare "maximum number of tokens" was REMOVED (lead 4th-verify P1 #2): it also
    # matches TPM/daily RATE-LIMIT phrasings ("maximum number of tokens per minute") which
    # are outages, not poison; real context-overflow is covered by "context length"/"context
    # window"/"maximum context"/"prompt too long". Token-rate phrasings are now infra-first.
    "prompt is too long", "prompt too long",
)
# A bare "413" SUBSTRING is a collision risk (e.g. "8413 bytes" / "code 4130"), so 413 is
# poison ONLY when it is a STANDALONE numeric TOKEN (\b413\b - never inside 8413/4130; reviewer-1
# blocker) AND QUALIFIED by an HTTP/status/request-entity/payload context (codex 4th-verify).
_POISON_413_RE = re.compile(r"\b413\b")
_POISON_413_QUALIFIERS = ("http", "status", "request entity", "payload", "entity too large")
_POISON_POLICY_MARKERS = (           # DIRECT: THIS message's content is disallowed
    "content policy", "content filter", "content_filter", "safety policy",
    "blocked by safety", "blocked by content policy",
)
# QUALIFIED: "violates"/"flagged by" are poison ONLY with a content/policy/safety/filter
# qualifier - bare "violates the rate limit" / "flagged by the gateway" are NOT message-poison.
_POISON_CONTENT_QUALIFIERS = ("content", "policy", "safety", "filter")
# QUALIFIED: "exceeds the maximum" is poison ONLY with a content-SIZE object - bare
# "exceeds the maximum retries" is infra/ambiguous, never poison.
_POISON_SIZE_QUALIFIERS = ("token", "context", "input", "prompt", "message", "request",
                           "payload")


def _looks_like_content_poison(text: str | None) -> bool:
    """True iff a terminal error message is EXPLICIT, message-content-attributable poison:
    a DIRECT size/policy signature, or a QUALIFIED 'violates'/'flagged by'/'exceeds the
    maximum' (their bare forms are too broad and stay AMBIGUOUS). codex ruling."""
    t = (text or "").lower()
    if any(m in t for m in _POISON_SIZE_MARKERS):
        return True
    if any(m in t for m in _POISON_POLICY_MARKERS):
        return True
    if ("violates" in t or "flagged by" in t) and any(q in t for q in _POISON_CONTENT_QUALIFIERS):
        return True
    if "exceeds the maximum" in t and any(q in t for q in _POISON_SIZE_QUALIFIERS):
        return True
    if _POISON_413_RE.search(t) and any(q in t for q in _POISON_413_QUALIFIERS):
        return True
    return False


def parse_lines(lines: Iterable[str], mapper: Callable[[object], list[Event]]) -> Iterator[Event]:
    """Parse a raw line stream into normalized events. Blank lines and lines that
    are not valid JSON are skipped (a real stream interleaves non-JSON noise)."""
    for line in lines:
        s = line.strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
        except (ValueError, TypeError):
            continue
        for ev in mapper(obj):
            yield ev


def _default_heartbeat(store, agent: str) -> Callable[[Event, float], None]:
    def stamp(_event: Event, _now: float) -> None:
        store.write_heartbeat(agent)
    return stamp


def _default_escalate(store, agent: str, sender: str) -> Callable[[object], None]:
    def escalate(signal) -> None:
        # Dedicated degraded restart path - NOT force-expiring the heartbeat, so
        # no-progress (stale) and bad-progress (degraded) stay distinct/diagnosable.
        store.write_restart_request(agent, {
            "agent": agent,
            "request_id": "rr-" + uuid.uuid4().hex[:12],
            "source": "degraded",
            "requested_by": sender,
            "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "at_epoch": time.time(),
            "force_protected": False,
            "reason": signal.reason,
        })
    return escalate


def _default_render(event: Event) -> None:
    t = event.type.value
    if event.tool:
        sys.stdout.write(f"[{t}] {event.tool}\n")
    elif event.text:
        sys.stdout.write(f"[{t}] {event.text}\n")
    else:
        sys.stdout.write(f"[{t}]\n")
    sys.stdout.flush()


def _default_info(signal) -> None:
    sys.stderr.write(f"[degraded:{signal.confidence}] {signal.reason}\n")
    sys.stderr.flush()


def run_wrapper(
    *,
    cli: str,
    agent: str,
    argv: list[str],
    store=None,
    sender: str | None = None,
    min_interval: float = 5.0,
    render: bool = True,
    degraded_config: DegradedConfig | None = None,
    line_source: Iterable[str] | None = None,
    heartbeat_fn: Callable[[Event, float], None] | None = None,
    restart_fn: Callable[[object], None] | None = None,
    render_fn: Callable[[Event], None] | None = None,
    info_fn: Callable[[object], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Wrap one CLI run. Returns the child exit code (0 when a line_source is
    injected, i.e. tests)."""
    mapper = _ADAPTERS.get(cli)
    if mapper is None:
        raise ValueError(f"no wrapper adapter for cli {cli!r} (Phase 1 = codex)")
    cfg = degraded_config or DegradedConfig(
        telemetry_only=_TELEMETRY_ONLY.get(cli, False)
    )
    detector = DegradedDetector(cli, cfg)
    health_writer = (
        WrapperHealthWriter(store, agent, cli, mode="wrapper-one-shot",
                            min_interval=min_interval)
        if store is not None else None
    )

    if heartbeat_fn is None:
        heartbeat_fn = _default_heartbeat(store, agent)
    if restart_fn is None:
        restart_fn = _default_escalate(store, agent, sender or agent)
    if render_fn is None:
        render_fn = _default_render if render else None
    if info_fn is None:
        info_fn = _default_info

    def _restart(signal) -> None:
        if health_writer is not None:
            health_writer.degraded(signal)
        restart_fn(signal)

    def _info(signal) -> None:
        if health_writer is not None:
            health_writer.degraded(signal)
        info_fn(signal)

    engine = WrapperEngine(
        detector=detector,
        on_heartbeat=heartbeat_fn,
        on_render=render_fn,
        on_escalate=_restart,
        on_info=_info,
        min_interval=min_interval,
    )

    def _health_events(events: Iterable[Event]) -> Iterator[Event]:
        for ev in events:
            if health_writer is not None:
                health_writer.event(ev)
            yield ev

    if line_source is not None:
        if health_writer is not None:
            health_writer.turn_start(None)
        engine.run(_health_events(parse_lines(line_source, mapper)), clock)
        if (health_writer is not None
                and health_writer.state != health_model.STATE_DEGRADED_OUTPUT):
            health_writer.idle(reason_code="wrapper_completed")
        return 0

    # argv is the operator-provided launch command; never shell=True.
    # encoding/errors are EXPLICIT: codex/claude emit UTF-8, but text=True alone
    # decodes child stdout via the platform default (cp1252 on Windows) and CRASHES
    # on the first non-ASCII byte (e.g. a smart quote). errors="replace" means a
    # genuinely malformed byte renders a replacement char instead of killing the
    # wrapper. In text mode this also governs the stdin prompt write.
    # Strip the lead-loop owner-bypass token from the child env here too (parity with
    # _ProcStream), so "the model child never sees the token" holds on EVERY spawn path,
    # not only the loop path (defense-in-depth + comment accuracy).
    child_env = _child_env()
    proc = subprocess.Popen(  # noqa: S603  # nosec B603
        argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace", bufsize=1, env=child_env,
        **_child_window_kwargs(child_env),
    )
    try:
        if health_writer is not None:
            health_writer.turn_start(None)
        lines = _iter_suppressing_benign_pipe_teardown(proc.stdout or [])
        engine.run(_health_events(parse_lines(lines, mapper)), clock)
    finally:
        _close_pipe_suppressing_benign_pipe_teardown(proc.stdout)
    rc = proc.wait()
    if health_writer is not None:
        if rc == 0:
            if health_writer.state != health_model.STATE_DEGRADED_OUTPUT:
                health_writer.idle(reason_code="wrapper_completed")
        else:
            health_writer.crashed_or_exited(reason_code="wrapper_child_nonzero_exit")
    return rc


class _ProcStream:
    """Default make_drive spawner: spawn the CLI for one turn (prompt on STDIN, to
    dodge quoting), yield its stdout lines, and expose the child EXIT CODE as
    ``.returncode`` once the stream is exhausted - so the drive path can tell a
    clean completed turn from a partial stream that died with a bad exit. Tests
    inject their own spawner (a plain list = unknown exit, an object with
    ``.returncode`` = a controlled exit)."""

    def __init__(self, argv: list[str], stdin_text: str | None, *,
                 watchdog: "TurnWatchdogConfig | None" = None,
                 watchdog_snapshot_fn=None, watchdog_kill_fn=None,
                 watchdog_clock=time.monotonic, watchdog_wall_clock=time.time,
                 work_heartbeat=None, work_heartbeat_stamp=None,
                 work_heartbeat_status=None,
                 child_env: dict[str, str] | None = None,
                 on_spawn: Callable[[int, str | None], None] | None = None,
                 on_exit: Callable[[int, str | None, int], None] | None = None,
                 lease_lost_exceptions: tuple = ()) -> None:
        # argv is the operator-provided launch command; never shell=True.
        # Explicit encoding/errors (see run_wrapper): UTF-8 child output must not be
        # decoded as cp1252 on Windows (crash on the first non-ASCII byte), and a
        # malformed byte must be replaced, not fatal. Governs the stdin prompt too.
        # STRIP the lead-loop owner-bypass token from the child env (WP2 condition 3 /
        # reviewer-1 Slice-1 residual): the wrapper consumes the bus IN-PROCESS, so the
        # model child never needs AGENTTALK_LEAD_LOOP_LEASE - and leaking it would let
        # an accidental model-side `agenttalk drain` bypass the single-consumer guard.
        # Always stripped (harmless for non-lead-loop children; defense-in-depth even
        # if a future parent sets it). The child otherwise inherits the parent env.
        child_env = dict(child_env) if child_env is not None else _child_env()
        self._proc = subprocess.Popen(  # noqa: S603  # nosec B603
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
            bufsize=1, env=child_env, **_child_window_kwargs(child_env),
        )
        self.returncode: int | None = None
        self.pid = self._proc.pid
        self.pid_start: str | None = None
        self.watchdog_result: dict | None = None
        self._watchdog = None
        self._watchdog_stream_condition = threading.Condition()
        self._watchdog_stream_interrupted = False
        self._watchdog_stream_done = False
        self._watchdog_root_waiter: threading.Thread | None = None
        self.work_heartbeat_result: dict | None = None
        self._work_hb = None
        self._on_exit = on_exit
        try:
            # Record the per-turn root start-time right after spawn (~ the OS-reported create
            # time, within spawn jitter) so the watchdog can start-time-guard the kill against
            # pid reuse. Wall clock, to match the snapshot adapter's create_epoch.
            self._root_start = watchdog_wall_clock()
            if on_spawn is not None:
                from agenttalk.wrapper_runtime import process_start_token

                self.pid_start = process_start_token(self.pid)
                on_spawn(self.pid, self.pid_start)
            if self._proc.stdin is not None:
                if stdin_text is not None:
                    self._proc.stdin.write(stdin_text)
                self._proc.stdin.close()
            # Per-turn watchdog (off unless an ENABLED config is passed). A confirmed
            # root kill wakes the iterator explicitly; inherited writers may keep the
            # OS pipe open after the protected child is gone.
            if watchdog is not None and watchdog.enabled:
                from .turn_watchdog import TurnWatchdog
                self._watchdog = TurnWatchdog(
                    root_pid=self._proc.pid, root_start=self._root_start, cfg=watchdog,
                    snapshot_fn=watchdog_snapshot_fn, kill_fn=watchdog_kill_fn,
                    clock=watchdog_clock, wall_clock=watchdog_wall_clock,
                    on_fire=self._handle_watchdog_fire)
                self._watchdog.start()
            # Bounded in-turn work heartbeat (wrapped-Claude false-STUCK fix): stamps the
            # supervisor heartbeat while THIS child is alive, capped at max_turn_seconds.
            # Started LAST (after stdin close, before stdout iteration) so a constructor
            # failure never leaks a live ticker. A structured result lands on
            # .work_heartbeat_result once the stream is exhausted.
            if (work_heartbeat is not None and work_heartbeat_stamp is not None):
                from .work_heartbeat import WorkHeartbeatTicker, work_heartbeat_effectively_live
                if work_heartbeat_effectively_live(work_heartbeat):
                    self._work_hb = WorkHeartbeatTicker(
                        cfg=work_heartbeat, stamp=work_heartbeat_stamp,
                        child_alive=lambda: self._proc.poll() is None,
                        lease_lost_exceptions=lease_lost_exceptions,
                        on_status=work_heartbeat_status)
                    self._work_hb.start()
        except Exception:
            self._cleanup_after_constructor_error()
            raise

    def __iter__(self) -> Iterator[str]:
        consumer_aborted = False
        try:
            if self._watchdog is None:
                yield from _iter_suppressing_benign_pipe_teardown(
                    self._proc.stdout or []
                )
            else:
                yield from self._iter_until_watchdog_or_eof()
        finally:
            if self._watchdog is not None:
                consumer_aborted = self._cancel_watchdog_stream_after_consumer_exit()
            # Stop the work-heartbeat ticker FIRST - before this stream reads as
            # exhausted to the caller - so drive()'s failed-turn clear_heartbeat
            # (which runs after stream exhaustion) can never be overwritten by a
            # late tick: stop() synchronizes with any in-flight stamp under the
            # ticker's lock (hard no-stamp-after-stop), so even a thread that
            # outlives the bounded join below cannot stamp again.
            if self._work_hb is not None:
                self._work_hb.stop()
                self._work_hb.join(timeout=10.0)
                self.work_heartbeat_result = dict(self._work_hb.result)
            if self._proc.stdout:
                _close_pipe_suppressing_benign_pipe_teardown(self._proc.stdout)
            # Owner cancellation is authoritative and must stop the watchdog before
            # it competes with abort cleanup. Natural stdout EOF is not root exit:
            # keep the watchdog armed through the root wait so an EOF-then-hung
            # process can still reach the two-factor kill boundary.
            if self._watchdog is not None and consumer_aborted:
                self._watchdog.stop()
                self._watchdog.join(timeout=10.0)
                self.watchdog_result = self._watchdog.result
            if consumer_aborted:
                try:
                    if self._proc.poll() is None:
                        self._proc.terminate()
                except Exception:  # noqa: BLE001, S110 - preserve the consumer's failure  # nosec B110
                    pass
            try:
                if self._watchdog_stream_interrupted:
                    if consumer_aborted:
                        try:
                            self.returncode = self._proc.wait(timeout=10.0)
                        except subprocess.TimeoutExpired:
                            try:
                                self._proc.kill()
                                self.returncode = self._proc.wait(timeout=5.0)
                            except Exception:  # noqa: BLE001 - cleanup must preserve owner failure
                                self.returncode = self._proc.poll()
                    else:
                        # A watchdog wake is an exit confirmation, never merely a signal
                        # report. Fail closed if that invariant is ever violated.
                        self.returncode = self._proc.wait()
                else:
                    self.returncode = self._proc.wait()
            finally:
                if self._watchdog is not None and not consumer_aborted:
                    self._watchdog.stop()
                    # Root exit is not enough to publish this turn while the
                    # watchdog may still be finishing its start-guarded kill
                    # pass.  Production snapshot/kill operations are bounded,
                    # so wait for that pass to finish before exposing its
                    # result or allowing the next turn to start.
                    self._watchdog.join()
                    self.watchdog_result = self._watchdog.result
            # After returncode is finalized (every branch above sets it) and any
            # watchdog kill pass has finished either way - a diagnostic sink must
            # never race the authoritative watchdog/abort handling above it, and
            # must never change the child result or strand its cleanup.
            if self._on_exit is not None:
                try:
                    self._on_exit(self.pid, self.pid_start, self.returncode)
                except Exception as exc:
                    _ = exc

    def _interrupt_watchdog_stream(self) -> None:
        with self._watchdog_stream_condition:
            self._watchdog_stream_interrupted = True
            self._watchdog_stream_condition.notify_all()

    def _cancel_watchdog_stream_after_consumer_exit(self) -> bool:
        """Cancel direct pipe reads when the owner stopped before EOF or watchdog wake."""
        with self._watchdog_stream_condition:
            if self._watchdog_stream_interrupted:
                return False
            if self._watchdog_stream_done:
                return False
            self._watchdog_stream_interrupted = True
            self._watchdog_stream_condition.notify_all()
            return True

    def _handle_watchdog_fire(self, result: dict) -> None:
        """Wake a live pipe reader once the protected root has exited."""
        with self._watchdog_stream_condition:
            # Natural EOF already handed root-exit confirmation to the owner wait.
            # Starting a second Popen waiter here is unnecessary and can race the
            # owner's reap on POSIX.
            if self._watchdog_stream_interrupted or self._watchdog_stream_done:
                return
        # kill_targets reports that a start-guarded signal was requested, not that
        # process termination has completed. Never publish an idle turn while this
        # Popen handle can still be alive.
        if self._proc.poll() is not None:
            self._interrupt_watchdog_stream()
            return
        with self._watchdog_stream_condition:
            if self._watchdog_root_waiter is not None:
                return
            self._watchdog_root_waiter = threading.Thread(
                target=self._wake_after_watchdog_root_exit,
                name="turn-watchdog-root-waiter",
                daemon=True,
            )
            waiter = self._watchdog_root_waiter
        waiter.start()

    def _wake_after_watchdog_root_exit(self) -> None:
        try:
            self._proc.wait()
        except Exception:  # noqa: BLE001 - an unconfirmed root exit must stay fail-closed
            return
        self._interrupt_watchdog_stream()

    def _iter_until_watchdog_or_eof(self) -> Iterator[str]:
        stdout = self._proc.stdout
        if stdout is None:
            with self._watchdog_stream_condition:
                self._watchdog_stream_done = True
            return
        decoder = io.IncrementalNewlineDecoder(
            codecs.getincrementaldecoder("utf-8")("replace"),
            translate=True,
        )
        pending = ""
        while True:
            with self._watchdog_stream_condition:
                if self._watchdog_stream_interrupted:
                    return
            chunk = _read_ready_pipe_chunk(stdout)
            if chunk is None:
                with self._watchdog_stream_condition:
                    if self._watchdog_stream_interrupted:
                        return
                    self._watchdog_stream_condition.wait(
                        timeout=_WATCHDOG_STREAM_POLL_SECONDS
                    )
                continue
            if chunk == b"":
                pending += decoder.decode(b"", final=True)
                with self._watchdog_stream_condition:
                    self._watchdog_stream_done = True
                    self._watchdog_stream_condition.notify_all()
                if pending:
                    yield pending
                return
            pending += decoder.decode(chunk)
            while "\n" in pending:
                line, pending = pending.split("\n", 1)
                with self._watchdog_stream_condition:
                    if self._watchdog_stream_interrupted:
                        return
                yield f"{line}\n"

    def _cleanup_after_constructor_error(self) -> None:
        # Best-effort only: preserve the original constructor failure so make_drive
        # keeps its existing spawn/exec classification.
        for worker in (self._work_hb, self._watchdog):
            if worker is None:
                continue
            try:
                worker.stop()
                worker.join(timeout=10.0)
            except Exception as exc:
                _ = exc
        for pipe in (self._proc.stdin, self._proc.stdout):
            try:
                _close_pipe_suppressing_benign_pipe_teardown(pipe)
            except OSError as exc:
                _ = exc
        try:
            if self._proc.poll() is None:
                self._proc.terminate()
            self._proc.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            try:
                self._proc.kill()
                self._proc.wait(timeout=5.0)
            except Exception as exc:
                _ = exc
        except OSError as exc:
            _ = exc


def _ovh_qwen_failure_text(sig: dict) -> str:
    # ``config_blocked_text`` may contain the rendered stdout of a parsed
    # TOOL_FINISHED event. It remains authoritative for the later deterministic
    # config-blocked branch, but is not raw gateway diagnostic input.
    values = [sig.get("error"), sig.get("terminal_text")]
    tail = sig.get("discarded_output_tail")
    if isinstance(tail, dict):
        for row in tail.get("lines") or []:
            if isinstance(row, dict):
                values.append(row.get("text"))
    return " ".join(str(value) for value in values if value).casefold()


def _classify_drive_failure(
    sig: dict,
    *,
    backend_profile: str | None = None,
) -> tuple[str, str]:
    """Map failed-turn signals to the wrapper dead-letter/resume taxonomy."""
    from .loop import (
        CLASS_AMBIGUOUS,
        CLASS_CONFIG_BLOCKED,
        CLASS_GATEWAY_HELD,
        CLASS_INFRA,
        CLASS_POISON,
    )

    # A TRANSIENT, operator-resolvable gateway HOLD surfaced at mint time (#62): classify
    # CLASS_GATEWAY_HELD (parked, re-driving, NEVER dead-lettered) so the worker self-heals
    # when the hold clears. Deterministic explicit signal - checked before any text heuristics.
    if sig.get("gateway_transient_hold"):
        return CLASS_GATEWAY_HELD, "OVH gateway is temporarily held; retrying until it clears"

    if backend_profile == "ovh-qwen":
        text = _ovh_qwen_failure_text(sig)
        if "atgw_child_turn_cap_exceeded" in text:
            return CLASS_CONFIG_BLOCKED, "OVH child turn budget exhausted"
        # A ledger/policy block surfaced as terminal TEXT (a 503 seen by an ALREADY-MINTED child
        # turn mid-stream) stays config_blocked, unchanged from master. CLASS_GATEWAY_HELD is
        # scoped to the MINT-TIME hold only (the gateway_transient_hold signal above), where no
        # turn was minted so re-driving genuinely self-heals. A mid-turn hold cannot: the minted
        # turn's 300s wall-clock keeps burning during the hold, so after the operator clears it a
        # re-mint hits ChildTurnCapExceeded -> config_blocked anyway. Making the mid-turn text
        # self-heal needs a turn-reaper for held-expired rows (the #63 residual / #62 follow-up),
        # NOT a classification change here (Fable review of b173f36, MAJOR-1).
        if "atgw_policy_blocked" in text or "atgw_ledger_blocked" in text:
            return CLASS_CONFIG_BLOCKED, "OVH trial policy or accounting hold"
        if "atgw_config_error" in text or "status code: 422" in text or "http 422" in text:
            return CLASS_CONFIG_BLOCKED, "OVH gateway route or configuration error"
        infra_markers = (
            "atgw_infra_unavailable",
            "atgw_infra_busy",
            "connection refused",
            "actively refused",
            "connect timeout",
            "connection timeout",
            "status code: 429",
            "http 429",
            "status code: 500",
            "status code: 502",
            "status code: 503",
            "status code: 504",
            "http 500",
            "http 502",
            "http 503",
            "http 504",
        )
        if any(marker in text for marker in infra_markers):
            return CLASS_INFRA, "OVH gateway transport is unavailable"

    wd = sig.get("watchdog")
    if wd:
        summary = wd.get("summary") if isinstance(wd, dict) else None
        return CLASS_AMBIGUOUS, summary or "turn watchdog killed hung tool descendant"
    if sig.get("config_blocked"):
        summary = sig.get("config_blocked_text")
        return CLASS_CONFIG_BLOCKED, summary or "deterministic exec permission denied"
    bus_failure = sig.get("bus_failure")
    if isinstance(bus_failure, dict):
        return CLASS_AMBIGUOUS, bus_failure.get("summary") or "bus write failed"
    setup_failure = sig.get("setup_failure")
    if isinstance(setup_failure, dict):
        return CLASS_CONFIG_BLOCKED, setup_failure.get("summary") or "deterministic setup failure"
    structured = _structured_infra_summary(sig)
    if sig.get("error"):
        if structured:
            return CLASS_INFRA, structured
        return CLASS_AMBIGUOUS, f"turn error without structured infra status: {sig['error']}"
    if sig.get("terminal"):
        text = sig.get("terminal_text") or ""
        blocked = _terminal_config_blocked_summary(text)
        if blocked is None:
            blocked = _runtime_config_blocked_summary(text)
        if blocked:
            return CLASS_CONFIG_BLOCKED, blocked
        if structured:
            return CLASS_INFRA, structured
        if _looks_like_content_poison(text):
            return CLASS_POISON, f"terminal content-poison: {text[:160]}"
        if not sig.get("structured_errors") and _looks_like_infra(text):
            return CLASS_AMBIGUOUS, f"terminal ambiguous infra-like text: {text[:160]}"
        return CLASS_AMBIGUOUS, (f"terminal failure, unrecognized cause: {text[:160]}"
                                 if text else "terminal failure, no error text")
    if sig.get("retryable"):
        if structured:
            return CLASS_INFRA, structured
        return CLASS_AMBIGUOUS, (
            "retryable transport error without structured infra status"
            + (" after handshake" if sig.get("started") else ", no start")
        )
    if sig.get("started"):
        if not sig.get("completed"):
            return CLASS_AMBIGUOUS, ("partial stream: started, never completed "
                                     "(poison or an unrecognized drop after the handshake)")
        return CLASS_AMBIGUOUS, f"nonzero child exit (rc={sig.get('rc')}) after start"
    return CLASS_AMBIGUOUS, f"turn never started (rc={sig.get('rc')}, no clear signal)"


# Event types that prove the resumed turn actually RAN (produced real activity):
# model output/deltas OR any tool call. A missing-resume-session failure produces NONE
# of these (the turn never ran). This corroborates the authoritative num_turns gate and
# - critically - covers TOOL-ONLY turns (a model that emits only tool_use, no assistant
# text) so the guard is not a per-event-type enumeration (codex-agenttalk-reviewer-1, PR
# #51). TURN_STARTED/TURN_FINISHED are deliberately excluded: a bare handshake is not
# proof the model ran.
_TURN_ACTIVITY_EVENT_TYPES = frozenset({
    EventType.MODEL_OUTPUT,
    EventType.MODEL_OUTPUT_DELTA,
    EventType.TOOL_STARTED,
    EventType.TOOL_OUTPUT,
    EventType.TOOL_OUTPUT_DELTA,
    EventType.TOOL_FINISHED,
})


def _result_num_turns(raw: object) -> int | None:
    """The CLI's AUTHORITATIVE turn count from a terminal RESULT event, or None.

    claude's stream-json ``result`` carries ``num_turns`` (0 == the resumed turn never
    ran - the missing-session shape; >0 == it ran). Returns None when the field is
    absent/unparseable (or a bool, which is not a real count) so the caller treats it as
    UNKNOWN and falls back to the activity flag rather than assuming num_turns == 0."""
    if not isinstance(raw, dict) or raw.get("type") != "result":
        return None
    n = raw.get("num_turns")
    if isinstance(n, bool) or not isinstance(n, int):
        return None
    return n


def _child_output_tail_text(tail: object) -> str:
    """Flatten lines rejected as non-JSON at ingestion time.

    Parse disposition is recorded before the forensic tail is byte-bounded, so
    a valid but left-truncated assistant/tool event can never be reinterpreted
    later as a raw CLI diagnostic.
    """
    if not isinstance(tail, dict):
        return ""
    lines = tail.get("lines")
    if not isinstance(lines, list):
        return ""
    parts: list[str] = []
    for line in lines:
        if not (isinstance(line, dict) and isinstance(line.get("text"), str)):
            continue
        if line["text"].strip():
            parts.append(line["text"])
    return "\n".join(parts)


def make_drive(store, agent: str, cli: str, session_state, base_argv: list[str], *,
               sender: str | None = None, min_interval: float = 5.0,
               render: bool = True, rules: str | None = None,
               clock: Callable[[], float] = time.monotonic,
               spawn: Callable[[list[str], str | None], Iterable[str]] | None = None,
               agenttalk_preflight: Callable[[], str | None] | None = None,
               heartbeat: Callable[[], None] | None = None,
               persist: Callable[[object], None] | None = None,
               turn_watchdog: "TurnWatchdogConfig | None" = None,
               watchdog_snapshot_fn=None, watchdog_kill_fn=None,
               health_writer: WrapperHealthWriter | None = None,
               runtime_writer=None,
               work_heartbeat=None,
               wrapper_generation: str | None = None,
               backend_profile: str | None = None,
               profile_env: dict[str, str] | None = None,
               lifecycle_log=None,
               lease_lost_exceptions: tuple = ()) -> Callable[[dict], object]:
    """Build the per-turn ``drive(record)`` callback for loop.run_loop. Each call
    drives ONE real CLI turn and returns a :class:`loop.DriveOutcome` (ok + a failure
    CLASS on failure, for dead-letter). A turn fails if it produced no progress event or
    hit a TERMINAL (non-retryable) adapter_error (turn.failed, a nonzero-no-JSON child, a
    resume no-session). On a failed CODEX RESUME turn it marks resume unavailable,
    persists, and retries FRESH (exec --json) for the SAME record before returning;
    run_loop only commits the inbound message when the outcome is ok, so a failed turn is
    never lost (at-least-once).

    FAILURE CLASSIFICATION (dead-letter taxonomy) - see :func:`_classify`:
      * ``poison_eligible`` (low cap): a terminal turn-failure (turn.failed / is_error) whose
        error text POSITIVELY matches a message-CONTENT signature (the size family or the
        content-policy family) - explicit evidence THIS message's content is the cause. A
        terminal that is merely "not infra" is NOT poison; it is ambiguous (see below).
      * ``known_global_infra`` (never auto-DL): a structured retryable Claude
        rate-limit event or structured API status (429/529/5xx/auth outage). Legacy
        text markers are low-confidence only and stay ambiguous.
      * ``ambiguous_or_unknown`` (high ceiling): an UNRECOGNIZED terminal cause (neither
        content nor infra), a partial stream / nonzero exit after the turn STARTED (could be
        poison OR an infra drop after the handshake), crash-mid-turn, or no start with no
        clear signal - the misclassified-poison escape hatch.

    The detector + engine are created ONCE and reused across turns, so the degraded
    confirmation window (which counts degraded turns across CLI invocations) and the
    heartbeat throttle persist. ``spawn`` is injectable for tests (no real
    subprocess); ``persist`` is called after each turn to save session state."""
    from . import prompt as _prompt
    from . import session as _session
    from agenttalk import lesson_context as _lesson_context
    from .loop import (
        CLASS_AMBIGUOUS,
        CLASS_CONFIG_BLOCKED,
        DriveOutcome,
    )

    mapper = _ADAPTERS.get(cli)
    if mapper is None:
        raise ValueError(f"no wrapper adapter for cli {cli!r}")
    cfg = DegradedConfig(telemetry_only=_TELEMETRY_ONLY.get(cli, False))
    detector = DegradedDetector(cli, cfg)
    if health_writer is None:
        health_writer = WrapperHealthWriter(
            store, agent, cli, mode="wrapper-loop", min_interval=min_interval)
    restart = _default_escalate(store, agent, sender or agent)

    def _notify_resume_continuity_loss(record: dict | None) -> None:
        if not _session.should_notify_continuity_loss(session_state):
            return
        try:
            target = store.operator_facing() or store.sole_lead()
            if not target or target == agent:
                return
            rid = "esc-" + uuid.uuid4().hex[:12]
            msg_id = (record or {}).get("id")
            session_key = session_state.resume_failure_key or session_state.resume_attempt_key
            body = (
                f"[wrapper-resume-unavailable] agent {agent} gave up resuming session "
                f"{session_key} after repeated session-attributable failures. The next "
                "attempt will start a fresh session; the bus message is still pending and "
                "was not marked poison."
            )
            store.send(
                sender=agent,
                recipient=target,
                kind="question",
                subject="wrapper resume continuity loss",
                body=body,
                meta={
                    "needs_operator": "true",
                    "resume_unavailable": "true",
                    "continuity_lost": "true",
                    "msg_id": str(msg_id or ""),
                    "request_id": rid,
                },
            )
            _session.mark_continuity_loss_notified(session_state)
        except Exception:
            return

    def _health_escalate(signal) -> None:
        health_writer.degraded(signal)
        restart(signal)

    def _health_info(signal) -> None:
        health_writer.degraded(signal)
        _default_info(signal)

    engine = WrapperEngine(
        detector=detector,
        # Stamp heartbeat on streaming progress so a long SUCCESSFUL turn stays live
        # before it completes (in-turn liveness). A FAILED turn may also stamp here
        # mid-stream, so drive() CLEARS the heartbeat when the turn ends failed -
        # net: live during a successful turn, no fresh heartbeat after a failed one
        # (reviewer-1 gate: both, via stamp-during + clear-on-failure). The managed
        # lead-loop injects a combined stamp (write_heartbeat + renew lease) so the
        # lease stays fresh on streaming progress too, not only on the idle stamp
        # (WP2 condition 4); the failure-path heartbeat-clear below does not touch the
        # lease (the controller is alive while streaming, even on a turn that fails).
        on_heartbeat=((lambda _ev, _ts: heartbeat()) if heartbeat is not None
                      else _default_heartbeat(store, agent)),
        on_progress=(
            (
                lambda ev, _ts: (
                    None
                    if ev.type == EventType.TURN_STARTED
                    else runtime_writer.progress()
                )
            )
            if runtime_writer is not None
            else None
        ),
        on_render=(_default_render if render else None),
        on_escalate=_health_escalate,
        on_info=_health_info,
        min_interval=min_interval,
    )
    active_parent_request_id: str | None = None
    active_message_id: str | None = None
    if spawn is not None:
        spawner = spawn                     # tests inject their own (argv, stdin) spawner
    else:
        # The work-heartbeat ticker stamps the SAME callable as the progress/success
        # paths (the lead-loop's injected renew-then-stamp included, so the lease is
        # never outlived by a bus heartbeat) and persists a best-effort diagnostics
        # record - NOT a supervisor input in v1.
        _whb_stamp = (heartbeat if heartbeat is not None
                      else (lambda: store.write_heartbeat(agent)))

        def _whb_status(status: dict) -> None:
            store.write_work_heartbeat_status(agent, status)

        def spawner(argv, stdin_text):
            # Bind the per-turn watchdog onto the real spawner. When turn_watchdog is None
            # or disabled, _ProcStream starts no thread (zero overhead, behavior unchanged).
            # Same for a disabled/invalid work_heartbeat config.
            gateway_capability = None
            if backend_profile == "ovh-qwen":
                from agenttalk.ovh_gateway import GatewayError, LedgerHold, SpendLedger

                if not active_message_id:
                    raise _GatewayChildCapUnavailable(
                        "immutable inbound message id is unavailable"
                    )
                try:
                    gateway_capability = SpendLedger().open_child_turn(
                        agent=agent,
                        message_id=active_message_id,
                        request_id=active_parent_request_id or "",
                        issuer_token=str(
                            (profile_env or {}).get("ANTHROPIC_AUTH_TOKEN") or ""
                        ),
                    ).token
                except LedgerHold as exc:
                    # A held/unresolved/clock-rollback gateway: TRANSIENT + operator-resolvable.
                    # Mark it so the turn classifies CLASS_GATEWAY_HELD (parked, re-driving, never
                    # dead-lettered) and the worker self-heals when the hold clears, instead of
                    # sticky-parking as config_blocked forever (#62). ChildTurnCapExceeded is
                    # deliberately a LedgerBlocked (NOT a LedgerHold), so a permanently-capped
                    # message never takes this path and stays config_blocked. Ordered before the
                    # GatewayError catch below because LedgerHold IS a GatewayError subclass.
                    raise _GatewayChildCapUnavailable(
                        "durable child turn capability unavailable: "
                        f"{type(exc).__name__}",
                        transient=True,
                    ) from exc
                except (GatewayError, OSError, ValueError) as exc:
                    raise _GatewayChildCapUnavailable(
                        "durable child turn capability unavailable: "
                        f"{type(exc).__name__}"
                    ) from exc
            child_env = _child_env(
                store.root,
                wrapper_generation=wrapper_generation,
                inbound_request_id=active_parent_request_id,
                backend_profile=backend_profile,
                profile_env=profile_env,
                gateway_capability=gateway_capability,
            )
            return _ProcStream(argv, stdin_text, watchdog=turn_watchdog,
                               watchdog_snapshot_fn=watchdog_snapshot_fn,
                               watchdog_kill_fn=watchdog_kill_fn,
                               work_heartbeat=work_heartbeat,
                               work_heartbeat_stamp=_whb_stamp,
                               work_heartbeat_status=_whb_status,
                               child_env=child_env,
                               on_spawn=(
                                   runtime_writer.active
                                   if runtime_writer is not None
                                   else None
                               ),
                               on_exit=(
                                   lifecycle_log.child_exited
                                   if lifecycle_log is not None
                                   else None
                               ),
                               lease_lost_exceptions=lease_lost_exceptions)
    preflight = agenttalk_preflight
    if preflight is None and cli == "codex" and spawn is None:
        preflight = preflight_agenttalk_runtime
    preflight_ok = False

    def _run_one(
        spec,
        *,
        turn_id: str,
        after_spawn: Callable[[], None] | None = None,
    ) -> dict:
        """Spawn ONE CLI invocation for ``spec`` and process its JSONL via the engine.
        Returns the raw turn SIGNALS for classification: ``{ok, started, completed,
        terminal, retryable, rc, error}``. ok is True only if it reached a COMPLETED
        boundary (TURN_FINISHED), hit no terminal (non-retryable) adapter_error, AND the
        child exited cleanly. A spawn/exec failure (missing binary, OS error) is captured
        as ``error`` (infra), never raised, so the turn fails GRACEFULLY (no wrapper crash)."""
        argv = list(base_argv) + spec.args
        sig = {"ok": False, "started": False, "completed": False, "terminal": False,
               "retryable": False, "rc": None, "error": None, "terminal_text": "",
               "config_blocked": False, "config_blocked_text": "", "bus_failure": None,
               "setup_failure": None,
               "bus_action_attempted": False, "bus_action_infra": False,
               "bus_action_rejected": False,
               "structured_errors": [], "child_output_tail": None,
               "discarded_output_tail": None,
               "produced_model_output": False, "result_num_turns": None,
               "lesson_exposure_error": None}
        child_output_lines: list[dict[str, str]] = []
        child_output_truncated = False
        runtime_started = False

        def _finish_runtime() -> None:
            if runtime_writer is not None and runtime_started:
                runtime_writer.terminal(
                    "success" if sig["ok"] else "failed"
                )

        discarded_output_lines: list[dict[str, str]] = []
        discarded_output_truncated = False

        def _capture_child_output(
            stream_name: str,
            text: object,
            *,
            discarded: bool = False,
        ) -> None:
            nonlocal child_output_truncated, discarded_output_truncated
            tail = normalize_child_output_tail({
                "truncated": child_output_truncated,
                "lines": [*child_output_lines, {"stream": stream_name, "text": text}],
            })
            if tail is None:
                return
            child_output_lines[:] = tail["lines"]
            child_output_truncated = tail.get("truncated") is True
            if not discarded:
                return
            diagnostic_tail = normalize_child_output_tail({
                "truncated": discarded_output_truncated,
                "lines": [
                    *discarded_output_lines,
                    {"stream": stream_name, "text": text},
                ],
            })
            if diagnostic_tail is None:
                return
            discarded_output_lines[:] = diagnostic_tail["lines"]
            discarded_output_truncated = (
                diagnostic_tail.get("truncated") is True
            )

        def _finalize_child_output() -> None:
            tail = normalize_child_output_tail({
                "truncated": child_output_truncated,
                "lines": child_output_lines,
            })
            if tail is not None:
                sig["child_output_tail"] = tail
            diagnostic_tail = normalize_child_output_tail({
                "truncated": discarded_output_truncated,
                "lines": discarded_output_lines,
            })
            if diagnostic_tail is not None:
                sig["discarded_output_tail"] = diagnostic_tail

        nonlocal preflight_ok
        if preflight is not None and not preflight_ok:
            blocked = preflight()
            if blocked:
                sig["config_blocked"] = True
                sig["config_blocked_text"] = blocked
                sig["error"] = blocked
                return sig
            preflight_ok = True
        try:
            if runtime_writer is not None:
                runtime_writer.starting(
                    message_id=active_message_id,
                    turn_id=turn_id,
                )
                runtime_started = True
            stream = spawner(argv, spec.stdin)
            if after_spawn is not None:
                try:
                    after_spawn()
                except Exception as e:  # noqa: BLE001 - lesson telemetry must not kill turns
                    sig["lesson_exposure_error"] = (
                        f"{type(e).__name__}: {_compact_text(e, 160)}")
            for line in _iter_suppressing_benign_pipe_teardown(stream):
                s = line.strip()
                if not s:
                    _capture_child_output("stdout", line)
                    continue
                try:
                    raw = json.loads(s)
                except (ValueError, TypeError):
                    _capture_child_output("stdout", line, discarded=True)
                    continue
                _capture_child_output("stdout", line)
                _session.observe_event(session_state, raw)   # capture codex thread_id
                num_turns = _result_num_turns(raw)
                if num_turns is not None:
                    # Authoritative "did the turn run" signal from the terminal result
                    # (claude: num_turns==0 on a missing-resume-session). Captured here,
                    # from the FULL live stream line before the child-output tail is
                    # byte/line-bounded, so a truncated tail can never suppress it.
                    sig["result_num_turns"] = num_turns
                for ev in mapper(raw):
                    if ev.type in _TURN_ACTIVITY_EVENT_TYPES:
                        # The turn produced real activity (model output/delta OR a tool
                        # call). Corroborates the authoritative num_turns gate and covers
                        # TOOL-ONLY turns even if a CLI omits num_turns, so the guard is
                        # not a per-event-type enumeration (codex-reviewer-1, PR #51).
                        sig["produced_model_output"] = True
                    if ev.type == EventType.TURN_STARTED:
                        sig["started"] = True
                    elif ev.type == EventType.TURN_FINISHED:
                        sig["completed"] = True
                    elif ev.type == EventType.ADAPTER_ERROR:
                        fact = _claude_error_fact(ev) if cli == "claude" else None
                        if fact is not None:
                            sig["structured_errors"].append(fact)
                        if ev.retryable:
                            sig["retryable"] = True
                        else:
                            sig["terminal"] = True
                            # capture the error TEXT: a terminal turn.failed / is_error can
                            # carry an INFRA message (529/rate-limit/5xx/auth), which must NOT
                            # be classed poison (lead verify P1).
                            sig["terminal_text"] = ev.text or sig["terminal_text"]
                    elif ev.type == EventType.TOOL_FINISHED:
                        verb = _bus_command_verb(ev.tool)
                        if verb in _REQUIRED_BUS_WRITE_VERBS:
                            sig["bus_action_attempted"] = True
                        bus = classify_bus_execution(ev.tool, ev.text, ev.exit_code, ev.raw)
                        if bus["kind"] == BUS_KIND_CONFIG_BLOCKED:
                            sig["config_blocked"] = True
                            sig["config_blocked_text"] = bus["summary"]
                        elif bus["kind"] in (
                            BUS_KIND_SEMANTIC_FAILURE,
                            BUS_KIND_UNKNOWN_FAILURE,
                        ):
                            sig["bus_failure"] = bus
                            if bus["kind"] == BUS_KIND_SEMANTIC_FAILURE:
                                sig["bus_action_rejected"] = True
                            else:
                                sig["bus_action_infra"] = True
                        setup_failure = classify_setup_execution(
                            ev.tool, ev.text, ev.exit_code, ev.raw)
                        if setup_failure is not None:
                            sig["setup_failure"] = setup_failure
                    health_writer.event(ev)
                    engine.process(ev, clock())
        except _GatewayChildCapUnavailable as e:
            _capture_child_output(
                "stderr",
                f"{type(e).__name__}: {e}",
                discarded=True,
            )
            _finalize_child_output()
            if e.transient:
                # Transient, operator-resolvable gateway hold: NO child spawned, NO spend.
                # Mark it so _classify_drive_failure returns CLASS_GATEWAY_HELD -> the loop parks
                # WITHOUT persisting an attempt and re-drives next poll, self-healing when the hold
                # clears (#62). NOT config_blocked (which would sticky-park forever) and NOT INFRA
                # (which dead-letters at the infra-exhaustion backstop and withholds the heartbeat).
                sig["gateway_transient_hold"] = True
                sig["error"] = str(e)
            else:
                sig["config_blocked"] = True
                sig["config_blocked_text"] = str(e)
                sig["error"] = str(e)
            _finish_runtime()
            return sig
        except OSError as e:
            _capture_child_output(
                "stderr",
                f"{type(e).__name__}: {e}",
                discarded=True,
            )
            _finalize_child_output()
            # spawn/exec/transport OS error (missing binary, broken pipe, ...): a
            # GLOBAL/infra failure unless it is a deterministic local permission/config
            # denial. Never crash the wrapper.
            blocked = _spawn_config_blocked_summary(argv, e)
            if blocked:
                sig["config_blocked"] = True
                sig["config_blocked_text"] = blocked
                sig["error"] = blocked
            else:
                sig["error"] = f"spawn/exec error running {_compact_text(_format_argv(argv))}: {e}"
            _finish_runtime()
            return sig
        # rc is None for an injected list spawn (unknown -> trust the boundary); a real
        # _ProcStream reports the child exit code (nonzero = failed turn).
        sig["rc"] = getattr(stream, "returncode", None)
        # A fired per-turn watchdog (hung tool descendant killed) is the AUTHORITATIVE
        # cause of this turn's death. Its explicit wake ended the stream, so
        # rc/partial-stream signals are downstream noise. Carry it for _classify first.
        sig["watchdog"] = getattr(stream, "watchdog_result", None)
        _finalize_child_output()
        sig["ok"] = (
            sig["completed"]
            and not sig.get("watchdog")
            and not sig["terminal"]
            and not sig["config_blocked"]
            and sig["bus_failure"] is None
            and sig["rc"] in (0, None)
        )
        _finish_runtime()
        return sig

    def _classify(sig: dict) -> tuple[str, str]:
        return _classify_drive_failure(sig, backend_profile=backend_profile)

    def _finish_watchdog_recovery(sig: dict) -> None:
        if not sig.get("watchdog"):
            return
        if heartbeat is not None:
            heartbeat()
        else:
            store.write_heartbeat(agent)
        if runtime_writer is not None:
            # _run_one already published terminal/failed. Release the killed
            # turn's identity through the existing idle transition so the live
            # wrapper can own another turn while preserving last_outcome=failed.
            runtime_writer.idle()

    def drive(record: dict) -> DriveOutcome:
        nonlocal active_message_id, active_parent_request_id
        meta = record.get("meta") if isinstance(record.get("meta"), dict) else {}
        parent = meta.get("request_id")
        active_parent_request_id = parent if isinstance(parent, str) and parent else None
        message_id = record.get("id")
        active_message_id = message_id if isinstance(message_id, str) and message_id else None
        lesson_selection = _lesson_context.select_for_record(store, record)
        lesson_prompt = _lesson_context.render_prompt_section(lesson_selection)
        lesson_turn_id = f"turn-{uuid.uuid4().hex[:12]}"

        def _record_lesson_exposure() -> None:
            if not lesson_selection.rows:
                return
            _lesson_context.record_exposure(
                store=store,
                agent=agent,
                record=record,
                selection=lesson_selection,
                turn_id=lesson_turn_id,
            )

        prompt = _prompt.assemble_turn_prompt(
            record, rules=rules, lessons=lesson_prompt)
        spec = _session.build_turn(session_state, prompt)
        cli = session_state.cli
        # A failed RESUME turn self-heals to a fresh session before we classify (codex:
        # exec resume -> fresh exec, run.py history; claude: --resume -> fresh --session-id,
        # codex ruling #2). This is why "prompt too long" on a resume turn is NOT poison: a
        # FULL session is global session-pressure, not message-content - only a fresh-session
        # failure is attributable to the record.
        attempted_resume = ((cli == "codex" and "resume" in spec.args)
                            or (cli == "claude" and "--resume" in spec.args))
        health_writer.turn_start(record)
        if attempted_resume:
            _session.resume_attempt_start(
                session_state, agent=agent, msg_id=record.get("id"))
            if persist is not None:
                persist(session_state)
        sig = _run_one(
            spec,
            turn_id=lesson_turn_id,
            after_spawn=_record_lesson_exposure,
        )
        if not sig["ok"] and attempted_resume:
            try:
                resume_failure_class, resume_summary = _classify(sig)
            except Exception:  # noqa: BLE001 - publish unknown before preserving failure
                health_writer.unknown()
                raise
            attributable = _session.resume_failure_is_session_attributable(
                resume_failure_class, resume_summary,
                raw_tail=_child_output_tail_text(sig.get("discarded_output_tail")),
                produced_model_output=bool(sig.get("produced_model_output")),
                result_num_turns=sig.get("result_num_turns"),
            )
            if resume_failure_class == CLASS_CONFIG_BLOCKED:
                _session.clear_resume_attempt(session_state)
                store.clear_heartbeat(agent)
                engine.reset_heartbeat_throttle()
                if persist is not None:
                    persist(session_state)
                health_writer.failure(sig, resume_failure_class)
                _finish_watchdog_recovery(sig)
                return DriveOutcome(
                    ok=False,
                    failure_class=resume_failure_class,
                    summary=resume_summary,
                    child_output_tail=sig.get("child_output_tail"),
                )
            count = _session.record_resume_attempt_result(
                session_state,
                failure_class=resume_failure_class,
                summary=resume_summary,
                attributable=attributable,
            )
            if persist is not None:
                persist(session_state)
            store.clear_heartbeat(agent)
            engine.reset_heartbeat_throttle()
            if attributable and count >= 2:
                if cli == "claude":
                    _session.reset_claude_session(
                        session_state, "resume_unavailable after 2 session failures")
                else:
                    _session.mark_resume_unavailable(
                        session_state, "resume_unavailable after 2 session failures")
                session_state.resume_failure_count = 0
                _notify_resume_continuity_loss(record)
                _session.clear_resume_attempt(session_state)
                if persist is not None:
                    persist(session_state)
                health_writer.failure(sig, CLASS_AMBIGUOUS)
                _finish_watchdog_recovery(sig)
                return DriveOutcome(
                    ok=False,
                    failure_class=CLASS_AMBIGUOUS,
                    summary="resume_unavailable; next attempt will start a fresh session",
                    child_output_tail=sig.get("child_output_tail"),
                )
            health_writer.failure(sig, CLASS_AMBIGUOUS if attributable else resume_failure_class)
            _finish_watchdog_recovery(sig)
            return DriveOutcome(
                ok=False,
                failure_class=CLASS_AMBIGUOUS if attributable else resume_failure_class,
                summary=("resume attempt failed; retrying resume once before fresh session"
                         if attributable else resume_summary),
                child_output_tail=sig.get("child_output_tail"),
            )
        if sig["ok"]:
            _session.clear_resume_attempt(session_state)
            session_state.resume_failure_count = 0
            session_state.turns += 1
            if cli == "claude" and not session_state.resume_available:
                # a FRESH claude session just succeeded -> it is now resumable next turn
                # (codex re-arms via the observed thread.started; claude is minted, so set here).
                # Preserve continuity-loss audit fields before clearing the operator-facing
                # unavailable reason.
                if session_state.resume_unavailable_reason:
                    session_state.continuity_lost_reason = (
                        session_state.resume_unavailable_reason)
                    session_state.fresh_session_success_reason = "fresh_session_success"
                session_state.resume_available = True
                session_state.resume_unavailable_reason = ""
                session_state.resume_failure_key = ""
            # Unconditional: a clean completed turn ALWAYS ends with a fresh
            # heartbeat, even if the engine's min_interval throttled the in-turn
            # stamps (e.g. a quick retry right after a failure). Route through the
            # INJECTED hook when present (the lead-loop's renew+heartbeat), so the
            # final stamp also re-verifies lease ownership: a lost lease RAISES here
            # instead of stamping a fresh heartbeat with no live lease (codex WP2
            # consume-boundary blocker).
            if heartbeat is not None:
                heartbeat()
            else:
                store.write_heartbeat(agent)
            if persist is not None:
                persist(session_state)
            if health_writer.state != health_model.STATE_DEGRADED_OUTPUT:
                health_writer.idle(reason_code="turn_completed")
            return DriveOutcome(
                ok=True,
                bus_action_attempted=bool(sig.get("bus_action_attempted")),
                bus_action_infra=bool(sig.get("bus_action_infra")),
                bus_action_rejected=bool(sig.get("bus_action_rejected")),
            )
        # the turn FAILED (no completed boundary / nonzero exit / spawn error, after
        # any resume->fresh retry): undo any heartbeat its streaming progress stamped,
        # so a failed attempt leaves NO fresh heartbeat - AND reset the engine throttle
        # (reused across turns) so a successful retry within min_interval is not
        # throttled into leaving no fresh heartbeat.
        store.clear_heartbeat(agent)
        engine.reset_heartbeat_throttle()
        if persist is not None:
            persist(session_state)
        try:
            failure_class, summary = _classify(sig)
        except Exception:  # noqa: BLE001 - publish unknown before preserving failure
            health_writer.unknown()
            raise
        health_writer.failure(sig, failure_class)
        # WATCHDOG-RECOVERY ONLY (narrow path): the hung tool tree was killed and the
        # wrapper is alive + ready for the next turn, so re-stamp a fresh heartbeat (undoing
        # the clear above) - otherwise the supervisor would ALSO relaunch a healthy wrapper.
        # Ordinary failures keep the cleared heartbeat. A lost-lease raise from the injected
        # hook is NOT masked (reviewer-1 ask) - it propagates exactly like the success path.
        _finish_watchdog_recovery(sig)
        return DriveOutcome(ok=False, failure_class=failure_class, summary=summary,
                            child_output_tail=sig.get("child_output_tail"),
                            bus_action_attempted=bool(sig.get("bus_action_attempted")),
                            bus_action_infra=bool(sig.get("bus_action_infra")),
                            bus_action_rejected=bool(sig.get("bus_action_rejected")))

    return drive


def make_cadence_drive(store, agent: str, cli: str, session_state, base_argv: list[str], *,
                       sender: str | None = None, min_interval: float = 5.0,
                       render: bool = True, rules: str | None = None,
                       clock: Callable[[], float] = time.monotonic,
                       spawn: Callable[[list[str], str | None], Iterable[str]] | None = None,
                       agenttalk_preflight: Callable[[], str | None] | None = None,
                       heartbeat: Callable[[], None] | None = None,
                       persist: Callable[[object], None] | None = None,
                       runtime_writer=None,
                       work_heartbeat=None,
                       lifecycle_log=None,
                       lease_lost_exceptions: tuple = (),
                       ) -> Callable[[dict, list], bool]:
    """Build the SYNTHETIC cadence-turn drive for the managed lead-loop controller (WP3).

    ``cadence_drive(snapshot, items) -> bool`` drives ONE CLI turn whose prompt is the
    bounded read-only SNAPSHOT + the actionable items (NOT a bus record), returning True
    iff the turn reached a clean COMPLETED boundary. The cadence path still has NO
    :class:`~.loop.DriveOutcome` and NO dead-letter path - a cadence failure is just
    ``False`` (the controller treats it as controller-HEALTH trouble: back off + escalate,
    never poison). Resume failures are classified only to decide whether the session
    resume give-up ledger may count them.

    It builds its OWN engine/detector (a separate degraded window from the message drive is
    fine - cadence turns are rare) but SHARES ``session_state`` by reference, so codex
    ``thread_id`` / claude session-id continuity holds across BOTH message and cadence
    turns. ``make_drive`` is intentionally left untouched (the reviewed message hot path).
    ``heartbeat`` is the lead-loop combined renew+stamp - it RAISES on a lost lease, which
    propagates out so a lost-lease controller stops sweeping at once."""
    from . import prompt as _prompt
    from . import session as _session
    from .loop import CLASS_CONFIG_BLOCKED

    mapper = _ADAPTERS.get(cli)
    if mapper is None:
        raise ValueError(f"no wrapper adapter for cli {cli!r}")
    cfg = DegradedConfig(telemetry_only=_TELEMETRY_ONLY.get(cli, False))
    detector = DegradedDetector(cli, cfg)
    engine = WrapperEngine(
        detector=detector,
        on_heartbeat=((lambda _ev, _ts: heartbeat()) if heartbeat is not None
                      else _default_heartbeat(store, agent)),
        on_progress=(
            (
                lambda ev, _ts: (
                    None
                    if ev.type == EventType.TURN_STARTED
                    else runtime_writer.progress()
                )
            )
            if runtime_writer is not None
            else None
        ),
        on_render=(_default_render if render else None),
        on_escalate=_default_escalate(store, agent, sender or agent),
        on_info=_default_info,
        min_interval=min_interval,
    )
    if spawn is not None:
        spawner = spawn
    else:
        # Cadence turns are the lead-loop controller's OWN quiet turns: the same
        # bounded work-heartbeat applies (same stamp, same diagnostics record).
        _whb_stamp = (heartbeat if heartbeat is not None
                      else (lambda: store.write_heartbeat(agent)))

        def _whb_status(status: dict) -> None:
            store.write_work_heartbeat_status(agent, status)

        def spawner(argv, stdin_text):
            return _ProcStream(argv, stdin_text,
                               work_heartbeat=work_heartbeat,
                               work_heartbeat_stamp=_whb_stamp,
                               work_heartbeat_status=_whb_status,
                               on_spawn=(
                                   runtime_writer.active
                                   if runtime_writer is not None
                                   else None
                               ),
                               on_exit=(
                                   lifecycle_log.child_exited
                                   if lifecycle_log is not None
                                   else None
                               ),
                               lease_lost_exceptions=lease_lost_exceptions)
    preflight = agenttalk_preflight
    if preflight is None and cli == "codex" and spawn is None:
        preflight = preflight_agenttalk_runtime
    preflight_ok = False

    def _run_one(spec) -> dict:
        """Spawn ONE CLI invocation for ``spec`` and stream its JSONL through the engine.
        ok only if it reached a COMPLETED boundary, hit no terminal (non-retryable)
        adapter_error, produced no config-blocked bus tool output, and the child exited
        cleanly. A spawn/exec OSError is captured for the same classification path as
        message turns, so resume cadence never counts infra/config/kill failures toward
        the broken-session K gate."""
        argv = list(base_argv) + spec.args
        sig = {"ok": False, "started": False, "completed": False, "terminal": False,
               "retryable": False, "rc": None, "error": None, "terminal_text": "",
               "config_blocked": False, "config_blocked_text": "", "bus_failure": None,
               "setup_failure": None}
        nonlocal preflight_ok
        if preflight is not None and not preflight_ok:
            blocked = preflight()
            if blocked:
                sig["config_blocked"] = True
                sig["config_blocked_text"] = blocked
                sig["error"] = blocked
                return sig
            preflight_ok = True
        try:
            if runtime_writer is not None:
                runtime_writer.starting(
                    message_id=None,
                    turn_id=f"cadence-{uuid.uuid4().hex[:12]}",
                )
            stream = spawner(argv, spec.stdin)
            for line in stream:
                s = line.strip()
                if not s:
                    continue
                try:
                    raw = json.loads(s)
                except (ValueError, TypeError):
                    continue
                _session.observe_event(session_state, raw)
                for ev in mapper(raw):
                    if ev.type == EventType.TURN_STARTED:
                        sig["started"] = True
                    elif ev.type == EventType.TURN_FINISHED:
                        sig["completed"] = True
                    elif ev.type == EventType.ADAPTER_ERROR:
                        if ev.retryable:
                            sig["retryable"] = True
                        else:
                            sig["terminal"] = True
                            sig["terminal_text"] = ev.text or sig["terminal_text"]
                    elif ev.type == EventType.TOOL_FINISHED:
                        bus = classify_bus_execution(ev.tool, ev.text, ev.exit_code, ev.raw)
                        if bus["kind"] == BUS_KIND_CONFIG_BLOCKED:
                            sig["config_blocked"] = True
                            sig["config_blocked_text"] = bus["summary"]
                        elif bus["kind"] in (
                                BUS_KIND_SEMANTIC_FAILURE,
                                BUS_KIND_UNKNOWN_FAILURE):
                            sig["bus_failure"] = bus
                        setup_failure = classify_setup_execution(
                            ev.tool, ev.text, ev.exit_code, ev.raw)
                        if setup_failure is not None:
                            sig["setup_failure"] = setup_failure
                    engine.process(ev, clock())
        except OSError as exc:
            blocked = _spawn_config_blocked_summary(argv, exc)
            if blocked:
                sig["config_blocked"] = True
                sig["config_blocked_text"] = blocked
                sig["error"] = blocked
            else:
                sig["error"] = f"spawn/exec error running {_compact_text(_format_argv(argv))}: {exc}"
            if runtime_writer is not None:
                runtime_writer.terminal("failed")
            return sig
        rc = getattr(stream, "returncode", None)
        sig["rc"] = rc
        sig["ok"] = (
            sig["completed"]
            and not sig["terminal"]
            and not sig["config_blocked"]
            and sig["bus_failure"] is None
            and rc in (0, None)
        )
        if runtime_writer is not None:
            runtime_writer.terminal("success" if sig["ok"] else "failed")
        return sig

    def cadence_drive(snapshot: dict, items: list) -> bool:
        prompt = _prompt.assemble_cadence_prompt(snapshot, items, rules=rules)
        spec = _session.build_turn(session_state, prompt)
        cli_name = session_state.cli
        attempted_resume = ((cli_name == "codex" and "resume" in spec.args)
                            or (cli_name == "claude" and "--resume" in spec.args))
        if attempted_resume:
            _session.resume_attempt_start(session_state, agent=agent, msg_id="cadence")
            if persist is not None:
                persist(session_state)
        sig = _run_one(spec)
        ok = bool(sig["ok"])
        if not ok and attempted_resume:
            resume_failure_class, resume_summary = _classify_drive_failure(sig)
            attributable = _session.resume_failure_is_session_attributable(
                resume_failure_class, resume_summary)
            if resume_failure_class == CLASS_CONFIG_BLOCKED:
                _session.clear_resume_attempt(session_state)
                if persist is not None:
                    persist(session_state)
                count = 0
            else:
                count = _session.record_resume_attempt_result(
                    session_state,
                    failure_class=resume_failure_class,
                    summary=resume_summary,
                    attributable=attributable,
                )
            if attributable and count >= 2:
                if cli_name == "claude":
                    _session.reset_claude_session(
                        session_state, "resume_unavailable after 2 session failures")
                else:
                    _session.mark_resume_unavailable(
                        session_state, "resume_unavailable after 2 session failures")
                session_state.resume_failure_count = 0
                with contextlib.suppress(Exception):
                    target = store.operator_facing() or store.sole_lead()
                    if target and target != agent and _session.should_notify_continuity_loss(session_state):
                        rid = "esc-" + uuid.uuid4().hex[:12]
                        store.send(
                            sender=agent,
                            recipient=target,
                            kind="question",
                            subject="wrapper resume continuity loss",
                            body=(
                                f"[wrapper-resume-unavailable] agent {agent} gave up resuming "
                                "its cadence session after repeated session-attributable failures. "
                                "The next attempt will start a fresh session."
                            ),
                            meta={
                                "needs_operator": "true",
                                "resume_unavailable": "true",
                                "continuity_lost": "true",
                                "msg_id": "cadence",
                                "request_id": rid,
                            },
                        )
                        _session.mark_continuity_loss_notified(session_state)
                _session.clear_resume_attempt(session_state)
            if persist is not None:
                persist(session_state)
        if ok:
            _session.clear_resume_attempt(session_state)
            session_state.resume_failure_count = 0
            session_state.turns += 1
            if cli_name == "claude" and not session_state.resume_available:
                if session_state.resume_unavailable_reason:
                    session_state.continuity_lost_reason = (
                        session_state.resume_unavailable_reason)
                    session_state.fresh_session_success_reason = "fresh_session_success"
                session_state.resume_available = True
                session_state.resume_unavailable_reason = ""
                session_state.resume_failure_key = ""
            # a clean cadence turn ends with a fresh heartbeat; route through the injected
            # lead-loop hook (renew+stamp), which RAISES on a lost lease.
            if heartbeat is not None:
                heartbeat()
            else:
                store.write_heartbeat(agent)
        else:
            # a FAILED cadence turn leaves NO fresh heartbeat (controller-health staleness)
            # and resets the reused engine throttle - mirrors make_drive's failure path.
            store.clear_heartbeat(agent)
            engine.reset_heartbeat_throttle()
        if persist is not None:
            persist(session_state)
        return ok

    return cadence_drive
