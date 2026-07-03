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

import errno
import json
import os
from pathlib import Path
import re
import shutil
# subprocess spawns ONLY the operator-provided CLI launch command; never shell=True.
import subprocess  # nosec B404
import sys
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .turn_watchdog import TurnWatchdogConfig
from datetime import datetime, timezone

from agenttalk import health as health_model
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

# Known GLOBAL/INFRA signatures in a terminal turn-failure error message. A terminal
# turn.failed / is_error carrying one of these is an OUTAGE (overload, rate-limit, 5xx,
# auth, network, edge/WAF/proxy block), NOT message-poison - so it must NOT auto-dead-letter
# at the low poison cap (lead verify P1/C4). Erring toward INFRA is the SAFE direction: a
# misclassified poison just keeps retrying (with escalation at the ceiling), never false-DLs
# a healthy one during an outage. NOTE: bare "blocked" is DELIBERATELY excluded - it would
# shadow the content-policy poison markers ("blocked by safety" / "blocked by content
# policy"), which must stay POISON; the edge tokens below catch gateway/WAF blocks without it.
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


_AGENTTALK_COMMAND_TOKENS = frozenset({"agenttalk", "agenttalk.cmd", "agenttalk.exe"})
_PYTHON_COMMAND_TOKENS = frozenset({"python", "python.exe", "python3", "python3.exe", "py", "py.exe"})
_PYTHON_ENV_COMMAND_TOKENS = frozenset({"$env:agenttalk_py", "%agenttalk_py%"})
_PY_LAUNCHER_COMMAND_TOKENS = frozenset({"py", "py.exe"})
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
_FAILED_TOOL_STATUSES = frozenset({
    "failed", "failure", "error", "errored", "cancelled", "canceled",
    "timeout", "timed_out",
})
_INTERPRETER_NOT_FOUND_RE = re.compile(
    r"(python(?:3)?|py).*(not recognized|command not found|no such file|"
    r"filenotfounderror|winerror 2|createprocess error 2|could not be started)|"
    r"(not recognized|command not found|no such file).*python(?:3)?",
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
    clean = _clean_program_token(token).casefold()
    base = _program_basename(token)
    if clean in _PYTHON_ENV_COMMAND_TOKENS:
        return "python"
    if base in _PY_LAUNCHER_COMMAND_TOKENS:
        return "py"
    if base in _PYTHON_COMMAND_TOKENS:
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
        switches = {"-c"}
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


def _child_env(workspace_root: str | os.PathLike[str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != LEAD_LOOP_LEASE_ENV}
    workspace = Path(workspace_root).resolve() if workspace_root else _workspace_root()
    env["AGENTTALK_PY"] = _agenttalk_py()
    env["AGENTTALK_ROOT"] = str(workspace)
    return env


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
        bus = classify_bus_execution(command, window)
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
    exit_code = _bus_exit_code(exit_status, raw_event)
    failed = _bus_execution_failed(exit_status, raw_event)
    semantic = _semantic_bus_failure_subtype(verb, text, exit_code)
    if semantic is not None and failed:
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
    if failed:
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
    return _bus_result(BUS_KIND_OK_OR_NO_SIGNAL, "no_failed_execution_signal", "")


def _tool_config_blocked_summary(tool: object, output: object) -> str | None:
    command = _command_text(tool)
    classification = classify_bus_execution(command, output)
    if classification["kind"] == BUS_KIND_CONFIG_BLOCKED:
        return classification["summary"]
    if _bus_command_verb(command) not in _CLASSIFIED_BUS_VERBS:
        return None
    if not _looks_like_exec_denied(str(output or "")):
        return None
    return (
        f"command={_compact_text(command)}; "
        f"error={_compact_text(output)}; "
        "remediation=use $env:AGENTTALK_PY -m agenttalk for in-sandbox bus calls"
    )


def _looks_like_infra(text: str | None) -> bool:
    """True if a terminal error message reads as a GLOBAL/INFRA outage (vs message-poison)."""
    t = (text or "").lower()
    return any(m in t for m in _INFRA_ERROR_MARKERS)


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
    proc = subprocess.Popen(  # noqa: S603  # nosec B603
        argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace", bufsize=1, env=_child_env(),
    )
    try:
        if health_writer is not None:
            health_writer.turn_start(None)
        engine.run(_health_events(parse_lines(proc.stdout or [], mapper)), clock)
    finally:
        if proc.stdout:
            proc.stdout.close()
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
        self._proc = subprocess.Popen(  # noqa: S603  # nosec B603
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
            bufsize=1, env=_child_env(),
        )
        # Record the per-turn root start-time right after spawn (~ the OS-reported create
        # time, within spawn jitter) so the watchdog can start-time-guard the kill against
        # pid reuse. Wall clock, to match the snapshot adapter's create_epoch.
        self._root_start = watchdog_wall_clock()
        if self._proc.stdin is not None:
            if stdin_text is not None:
                self._proc.stdin.write(stdin_text)
            self._proc.stdin.close()
        self.returncode: int | None = None
        # Per-turn watchdog (off unless an ENABLED config is passed). A structured result
        # lands on .watchdog_result iff it fires (the kill closes stdout -> __iter__ ends).
        self.watchdog_result: dict | None = None
        self._watchdog = None
        if watchdog is not None and watchdog.enabled:
            from .turn_watchdog import TurnWatchdog
            self._watchdog = TurnWatchdog(
                root_pid=self._proc.pid, root_start=self._root_start, cfg=watchdog,
                snapshot_fn=watchdog_snapshot_fn, kill_fn=watchdog_kill_fn,
                clock=watchdog_clock, wall_clock=watchdog_wall_clock)
            self._watchdog.start()
        # Bounded in-turn work heartbeat (wrapped-Claude false-STUCK fix): stamps the
        # supervisor heartbeat while THIS child is alive, capped at max_turn_seconds.
        # Started LAST (after stdin close, before stdout iteration) so a constructor
        # failure never leaks a live ticker. A structured result lands on
        # .work_heartbeat_result once the stream is exhausted.
        self.work_heartbeat_result: dict | None = None
        self._work_hb = None
        if (work_heartbeat is not None and work_heartbeat_stamp is not None):
            from .work_heartbeat import WorkHeartbeatTicker, work_heartbeat_effectively_live
            if work_heartbeat_effectively_live(work_heartbeat):
                self._work_hb = WorkHeartbeatTicker(
                    cfg=work_heartbeat, stamp=work_heartbeat_stamp,
                    child_alive=lambda: self._proc.poll() is None,
                    lease_lost_exceptions=lease_lost_exceptions,
                    on_status=work_heartbeat_status)
                self._work_hb.start()

    def __iter__(self) -> Iterator[str]:
        try:
            yield from (self._proc.stdout or [])
        finally:
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
                self._proc.stdout.close()
            self.returncode = self._proc.wait()
            # Stop + join the watchdog AFTER the child has exited: a normal turn completion
            # makes the thread exit its wait immediately (no kill race), and a watchdog that
            # already fired published its result before we read it here.
            if self._watchdog is not None:
                self._watchdog.stop()
                self._watchdog.join(timeout=10.0)
                self.watchdog_result = self._watchdog.result


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
               work_heartbeat=None,
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
      * ``known_global_infra`` (never auto-DL): a spawn/exec OS error; a terminal failure
        whose text matches a global-outage signature (overloaded/rate-limit/5xx/auth/edge
        block/...); or a retryable transport error with no turn start.
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
    from .loop import (
        CLASS_AMBIGUOUS,
        CLASS_CONFIG_BLOCKED,
        CLASS_INFRA,
        CLASS_POISON,
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
        on_render=(_default_render if render else None),
        on_escalate=_health_escalate,
        on_info=_health_info,
        min_interval=min_interval,
    )
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
            return _ProcStream(argv, stdin_text, watchdog=turn_watchdog,
                               watchdog_snapshot_fn=watchdog_snapshot_fn,
                               watchdog_kill_fn=watchdog_kill_fn,
                               work_heartbeat=work_heartbeat,
                               work_heartbeat_stamp=_whb_stamp,
                               work_heartbeat_status=_whb_status,
                               lease_lost_exceptions=lease_lost_exceptions)
    preflight = agenttalk_preflight
    if preflight is None and cli == "codex" and spawn is None:
        preflight = preflight_agenttalk_runtime
    preflight_ok = False

    def _run_one(spec) -> dict:
        """Spawn ONE CLI invocation for ``spec`` and process its JSONL via the engine.
        Returns the raw turn SIGNALS for classification: ``{ok, started, completed,
        terminal, retryable, rc, error}``. ok is True only if it reached a COMPLETED
        boundary (TURN_FINISHED), hit no terminal (non-retryable) adapter_error, AND the
        child exited cleanly. A spawn/exec failure (missing binary, OS error) is captured
        as ``error`` (infra), never raised, so the turn fails GRACEFULLY (no wrapper crash)."""
        argv = list(base_argv) + spec.args
        sig = {"ok": False, "started": False, "completed": False, "terminal": False,
               "retryable": False, "rc": None, "error": None, "terminal_text": "",
               "config_blocked": False, "config_blocked_text": "", "bus_failure": None}
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
            stream = spawner(argv, spec.stdin)
            for line in stream:
                s = line.strip()
                if not s:
                    continue
                try:
                    raw = json.loads(s)
                except (ValueError, TypeError):
                    continue
                _session.observe_event(session_state, raw)   # capture codex thread_id
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
                            # capture the error TEXT: a terminal turn.failed / is_error can
                            # carry an INFRA message (529/rate-limit/5xx/auth), which must NOT
                            # be classed poison (lead verify P1).
                            sig["terminal_text"] = ev.text or sig["terminal_text"]
                    elif ev.type == EventType.TOOL_FINISHED:
                        bus = classify_bus_execution(ev.tool, ev.text, ev.exit_code, ev.raw)
                        if bus["kind"] == BUS_KIND_CONFIG_BLOCKED:
                            sig["config_blocked"] = True
                            sig["config_blocked_text"] = bus["summary"]
                        elif bus["kind"] in (
                            BUS_KIND_SEMANTIC_FAILURE,
                            BUS_KIND_UNKNOWN_FAILURE,
                        ):
                            sig["bus_failure"] = bus
                    health_writer.event(ev)
                    engine.process(ev, clock())
        except OSError as e:
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
            return sig
        # rc is None for an injected list spawn (unknown -> trust the boundary); a real
        # _ProcStream reports the child exit code (nonzero = failed turn).
        sig["rc"] = getattr(stream, "returncode", None)
        # A fired per-turn watchdog (hung tool descendant killed) is the AUTHORITATIVE
        # cause of this turn's death - it closed the stream, so rc/partial-stream signals
        # are downstream noise. Carry it for _classify to check FIRST.
        sig["watchdog"] = getattr(stream, "watchdog_result", None)
        sig["ok"] = (
            sig["completed"]
            and not sig["terminal"]
            and not sig["config_blocked"]
            and sig["bus_failure"] is None
            and sig["rc"] in (0, None)
        )
        return sig

    def _classify(sig: dict) -> tuple[str, str]:
        """Map failed-turn signals to (failure_class, summary) for the dead-letter
        taxonomy. A terminal turn-failure is low-cap POISON ONLY when its error text
        POSITIVELY matches a message-content signature (size / content-policy); an
        infra-looking terminal error, and any unrecognized/ambiguous partial/nonzero
        outcome, gets the high K_escalate ceiling so a sustained INFRA outage (or an
        unobserved cause) can never false-dead-letter a healthy message (lead verify P1)."""
        wd = sig.get("watchdog")
        if wd:
            # the per-turn watchdog killed a hung tool descendant: AMBIGUOUS (never poison),
            # checked BEFORE rc/terminal noise it caused. The message stays pending and rides
            # the K_escalate ceiling - a persistently-wedging head escalates, never auto-DLs.
            summary = wd.get("summary") if isinstance(wd, dict) else None
            return CLASS_AMBIGUOUS, summary or "turn watchdog killed hung tool descendant"
        if sig.get("config_blocked"):
            summary = sig.get("config_blocked_text")
            return CLASS_CONFIG_BLOCKED, summary or "deterministic exec permission denied"
        bus_failure = sig.get("bus_failure")
        if isinstance(bus_failure, dict):
            return CLASS_AMBIGUOUS, bus_failure.get("summary") or "bus write failed"
        if sig.get("error"):                       # spawn/exec OS error (missing binary, ...)
            return CLASS_INFRA, sig["error"]
        if sig.get("terminal"):
            text = sig.get("terminal_text") or ""
            blocked = _terminal_config_blocked_summary(text)
            if blocked is None:
                blocked = _runtime_config_blocked_summary(text)
            if blocked:
                return CLASS_CONFIG_BLOCKED, blocked
            if _looks_like_infra(text):
                # a terminal carrying an infra message (overloaded/rate-limit/5xx/auth/edge
                # block) is a GLOBAL outage, not poison -> never auto-DL. INFRA is checked
                # BEFORE poison ON PURPOSE: if a terminal somehow matched BOTH families, the
                # safe direction is INFRA (retry + escalate, decided by a human) over a low-cap
                # auto-dispose - a misclassified poison just disposes later at the ceiling, a
                # misclassified outage would false-DL a healthy message (fail-closed).
                return CLASS_INFRA, f"terminal infra error: {text[:160]}"
            if _looks_like_content_poison(text):
                # EXPLICIT message-content-attributable failure - the size family (this message
                # is too large) or the content-policy family (this message's content is
                # disallowed) -> deterministic poison fast-path @K_poison.
                return CLASS_POISON, f"terminal content-poison: {text[:160]}"
            # DEFAULT (codex ruling): an unrecognized terminal cause is AMBIGUOUS, not
            # poison - an UNKNOWN-infra terminal must not false-DL at the low cap. It still
            # disposes (bounded) at the high K_escalate ceiling, with escalation.
            return CLASS_AMBIGUOUS, (f"terminal failure, unrecognized cause: {text[:160]}"
                                     if text else "terminal failure, no error text")
        # An EXPLICIT recognized retryable transport error -> INFRA, checked BEFORE the
        # started/partial-stream ambiguity so a transport drop AFTER the handshake (turn
        # STARTED, recognized retryable error, then dropped with no terminal) is NOT shadowed
        # as ambiguous (lead 6th-verify P2). It is dominantly an infra drop after the
        # handshake -> keep retrying/escalating, NEVER auto-dead-letter. Covers both no-start
        # and started-then-dropped; a TERMINAL failure was already classified above by its text.
        if sig.get("retryable"):
            return CLASS_INFRA, ("retryable transport error"
                                 + (" after handshake" if sig.get("started") else ", no start"))
        if sig.get("started"):
            # started, NO terminal, NO recognized retryable signal: a partial stream / nonzero
            # exit is AMBIGUOUS - the start event may be only an API handshake (e.g. claude
            # message_start), so an UNRECOGNIZED drop MID-stream looks identical to
            # message-poison. Treat as ambiguous so a sustained OUTAGE escalates + only
            # dead-letters at the high K_escalate ceiling (never at the low poison cap).
            if not sig.get("completed"):
                return CLASS_AMBIGUOUS, ("partial stream: started, never completed "
                                         "(poison or an unrecognized drop after the handshake)")
            return CLASS_AMBIGUOUS, f"nonzero child exit (rc={sig.get('rc')}) after start"
        # the turn never started and no recognized signal: not attributable to the record.
        return CLASS_AMBIGUOUS, f"turn never started (rc={sig.get('rc')}, no clear signal)"

    def drive(record: dict) -> DriveOutcome:
        prompt = _prompt.assemble_turn_prompt(record, rules=rules)
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
        sig = _run_one(spec)
        if not sig["ok"] and attempted_resume:
            try:
                resume_failure_class, resume_summary = _classify(sig)
            except Exception:  # noqa: BLE001 - publish unknown before preserving failure
                health_writer.unknown()
                raise
            if resume_failure_class == CLASS_CONFIG_BLOCKED:
                store.clear_heartbeat(agent)
                engine.reset_heartbeat_throttle()
                if persist is not None:
                    persist(session_state)
                health_writer.failure(sig, resume_failure_class)
                return DriveOutcome(
                    ok=False,
                    failure_class=resume_failure_class,
                    summary=resume_summary,
                )
            # the resume turn failed -> force a FRESH session for the SAME record and retry
            # inline (codex: a new thread_id will be observed + persisted; claude: a new
            # session id is minted). The whole drive() (resume -> fresh) is EXACTLY ONE attempt.
            if cli == "claude":
                _session.reset_claude_session(session_state, "resume turn failed")
            else:
                _session.mark_resume_unavailable(session_state, "resume turn failed")
            if persist is not None:
                persist(session_state)
            health_writer.turn_start(record)
            sig = _run_one(_session.build_turn(session_state, prompt))
        if sig["ok"]:
            session_state.turns += 1
            if cli == "claude" and not session_state.resume_available:
                # a FRESH claude session just succeeded -> it is now resumable next turn
                # (codex re-arms via the observed thread.started; claude is minted, so set here).
                # Clear the stale reason too, mirroring observe_event's re-arm (diagnostic
                # hygiene, both reviewers - the field is only surfaced while resume is unavailable).
                session_state.resume_available = True
                session_state.resume_unavailable_reason = ""
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
            return DriveOutcome(ok=True)
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
        if sig.get("watchdog"):
            if heartbeat is not None:
                heartbeat()
            else:
                store.write_heartbeat(agent)
        return DriveOutcome(ok=False, failure_class=failure_class, summary=summary)

    return drive


def make_cadence_drive(store, agent: str, cli: str, session_state, base_argv: list[str], *,
                       sender: str | None = None, min_interval: float = 5.0,
                       render: bool = True, rules: str | None = None,
                       clock: Callable[[], float] = time.monotonic,
                       spawn: Callable[[list[str], str | None], Iterable[str]] | None = None,
                       agenttalk_preflight: Callable[[], str | None] | None = None,
                       heartbeat: Callable[[], None] | None = None,
                       persist: Callable[[object], None] | None = None,
                       work_heartbeat=None,
                       lease_lost_exceptions: tuple = (),
                       ) -> Callable[[dict, list], bool]:
    """Build the SYNTHETIC cadence-turn drive for the managed lead-loop controller (WP3).

    ``cadence_drive(snapshot, items) -> bool`` drives ONE CLI turn whose prompt is the
    bounded read-only SNAPSHOT + the actionable items (NOT a bus record), returning True
    iff the turn reached a clean COMPLETED boundary. UNLIKE :func:`make_drive` there is NO
    failure CLASSIFICATION, NO :class:`~.loop.DriveOutcome`, and NO dead-letter path - a
    cadence failure is just ``False`` (the controller treats it as controller-HEALTH
    trouble: back off + escalate, never poison).

    It builds its OWN engine/detector (a separate degraded window from the message drive is
    fine - cadence turns are rare) but SHARES ``session_state`` by reference, so codex
    ``thread_id`` / claude session-id continuity holds across BOTH message and cadence
    turns. ``make_drive`` is intentionally left untouched (the reviewed message hot path).
    ``heartbeat`` is the lead-loop combined renew+stamp - it RAISES on a lost lease, which
    propagates out so a lost-lease controller stops sweeping at once."""
    from . import prompt as _prompt
    from . import session as _session

    mapper = _ADAPTERS.get(cli)
    if mapper is None:
        raise ValueError(f"no wrapper adapter for cli {cli!r}")
    cfg = DegradedConfig(telemetry_only=_TELEMETRY_ONLY.get(cli, False))
    detector = DegradedDetector(cli, cfg)
    engine = WrapperEngine(
        detector=detector,
        on_heartbeat=((lambda _ev, _ts: heartbeat()) if heartbeat is not None
                      else _default_heartbeat(store, agent)),
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
                               lease_lost_exceptions=lease_lost_exceptions)
    preflight = agenttalk_preflight
    if preflight is None and cli == "codex" and spawn is None:
        preflight = preflight_agenttalk_runtime
    preflight_ok = False

    def _run_one(spec) -> bool:
        """Spawn ONE CLI invocation for ``spec`` and stream its JSONL through the engine.
        True only if it reached a COMPLETED boundary, hit no terminal (non-retryable)
        adapter_error, produced no config-blocked bus tool output, and the child exited
        cleanly. A spawn/exec OSError is a False turn (never raised). Cadence still returns
        only ok / not-ok; classification is used only to avoid false success."""
        argv = list(base_argv) + spec.args
        started = completed = terminal = False
        bus_failed = False
        nonlocal preflight_ok
        if preflight is not None and not preflight_ok:
            if preflight():
                return False
            preflight_ok = True
        try:
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
                        started = True
                    elif ev.type == EventType.TURN_FINISHED:
                        completed = True
                    elif ev.type == EventType.ADAPTER_ERROR and not ev.retryable:
                        terminal = True
                    elif ev.type == EventType.TOOL_FINISHED:
                        bus = classify_bus_execution(ev.tool, ev.text, ev.exit_code, ev.raw)
                        if bus["kind"] in (
                            BUS_KIND_CONFIG_BLOCKED,
                            BUS_KIND_SEMANTIC_FAILURE,
                            BUS_KIND_UNKNOWN_FAILURE,
                        ):
                            bus_failed = True
                    engine.process(ev, clock())
        except OSError as exc:
            _ = _spawn_config_blocked_summary(argv, exc)
            return False
        rc = getattr(stream, "returncode", None)
        _ = started  # observed for parity with make_drive; cadence gates on completed
        return completed and not terminal and not bus_failed and rc in (0, None)

    def cadence_drive(snapshot: dict, items: list) -> bool:
        prompt = _prompt.assemble_cadence_prompt(snapshot, items, rules=rules)
        spec = _session.build_turn(session_state, prompt)
        cli_name = session_state.cli
        attempted_resume = ((cli_name == "codex" and "resume" in spec.args)
                            or (cli_name == "claude" and "--resume" in spec.args))
        ok = _run_one(spec)
        if not ok and attempted_resume:
            # mirror make_drive's resume self-heal: a failed RESUME turn forces a FRESH
            # session for the SAME prompt and retries inline (still ONE cadence attempt).
            if cli_name == "claude":
                _session.reset_claude_session(session_state, "resume turn failed")
            else:
                _session.mark_resume_unavailable(session_state, "resume turn failed")
            if persist is not None:
                persist(session_state)
            ok = _run_one(_session.build_turn(session_state, prompt))
        if ok:
            session_state.turns += 1
            if cli_name == "claude" and not session_state.resume_available:
                session_state.resume_available = True
                session_state.resume_unavailable_reason = ""
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
