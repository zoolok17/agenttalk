"""The turn admission hook runner (build 6a-2a).

A small, neutral module: it runs an operator-configured local program with
one JSON request on standard input and reads one JSON answer from standard
output, under strict bounds. It does not touch the wrapper loop or the
gateway - those are separate patches (6a-2b and 6a-1) that call into this
module's public functions.

Words this module deliberately avoids: a bare "lease" (agenttalk already
uses that word for the lead-loop lease) - always "turn admission" or "quota
lease reference" instead.

Configuration (`TurnAdmissionConfig`) is accepted only as an explicit
parameter, built by the caller from the operator's own local wrapper
configuration. This module never reads a bus message, an agent-authored
value, task text, or an environment variable to decide what to run, with
what arguments, or under what bounds - there is no code path here that
could. The only environment values that ever reach the child process are
the operator's own fixed `env` entries plus the small, hardcoded set of
system names a process needs just to start (`_SYSTEM_ENV_DEFAULTS`);
nothing is read from, or inherited from, this process's own environment for
configuration purposes.
"""

from __future__ import annotations

import contextlib
import ctypes
import hashlib
import json
import os
import re
import signal
import subprocess  # nosec B404 - shell=False always; see _spawn
import threading
import time
from dataclasses import dataclass, field

from agenttalk import store as store_mod
from agenttalk.powershell_host import _attach_kill_on_close_job

# --------------------------------------------------------------- closed words

#: The eight closed failure words (spec 4.4). None of these, or anything
#: derived from them, ever carries program text, a reference, or any other
#: value the program produced - the word alone is the whole report.
FAILURE_WORDS = frozenset({
    "not_started", "pin_mismatch", "timeout", "exit_code",
    "not_json", "bad_shape", "out_of_bounds", "too_large",
})

#: The six closed `held` reasons an `admit` answer may give (spec 4.2).
HELD_REASONS = frozenset({
    "coordinator_unreachable", "refused", "parked",
    "context_unprovable", "store_busy", "not_configured",
})

OPERATIONS = frozenset({"admit", "recall", "pending", "closed"})

PROTOCOL_VERSION = 1

_DEFAULT_TIMEOUT_SECONDS = 25.0
_MAX_TIMEOUT_SECONDS = 30.0

#: Per-operation answer size cap, enforced WHILE READING (spec 4.3) - not a
#: single universal cap: `pending`'s answer can legitimately hold many
#: entries, so it gets its own, much larger, proven bound.
_ANSWER_CAPS = {"admit": 512, "recall": 512, "closed": 512, "pending": 9216}

# A handful of names a child process needs just to start (spec 4.1's "plus
# the few names any program needs to start"). Deliberately tiny, and never
# used to smuggle anything beyond a well-known, non-secret system path:
# these are read from THIS process's own environment because they are not
# secrets (a filesystem path to the Windows install, not a token), with a
# conservative fallback if even that is absent. Everything else in `env`
# comes only from the operator's own fixed name=value pairs - nothing else
# is inherited. On Windows this is the ONLY exception to the allowlist;
# POSIX has none of its own, though the Python interpreter the operator
# configures as `command` may still add locale state after exec (see the
# test suite's own allowance for that, which is a runtime fact about the
# interpreter, not something this module adds).
if os.name == "nt":
    _SYSTEM_ENV_DEFAULTS = {"SystemRoot": r"C:\Windows"}
else:
    _SYSTEM_ENV_DEFAULTS = {}


class TurnAdmissionFailure(Exception):
    """One closed failure word (spec 4.4). Never carries program text."""

    def __init__(self, word: str) -> None:
        if word not in FAILURE_WORDS:
            raise ValueError(f"not a closed turn admission failure word: {word!r}")
        super().__init__(word)
        self.word = word


# --------------------------------------------------------------- configuration

#: Windows file-name suffixes the OS may run through a shell even when a
#: caller sets shell=False (fix round 1, item: ".bat/.cmd refusal"; Python's
#: own subprocess docs name this exact caveat). Native executables only.
_WINDOWS_SHELL_SUFFIXES = (".bat", ".cmd")

#: Python's own os.path.isabs() accepted a single-leading-backslash Windows
#: path (drive resolved from the CALLER's current drive at runtime) through
#: 3.12; fixed in 3.13. CI runs 3.10-3.13 (fix round 1, finding 7). A fixed
#: program identity must never depend on which drive happened to be current
#: when the operator wrote the config or when the wrapper runs - require a
#: drive letter or a UNC root explicitly, on every supported version alike.
_WINDOWS_DRIVE_ABSOLUTE_RE = re.compile(r"\A[A-Za-z]:[\\/]")
_WINDOWS_UNC_ABSOLUTE_RE = re.compile(r"\A[\\/]{2}[^\\/]")


def _is_robust_absolute_path(path: str) -> bool:
    if os.name == "nt":
        return bool(_WINDOWS_DRIVE_ABSOLUTE_RE.match(path)) or bool(_WINDOWS_UNC_ABSOLUTE_RE.match(path))
    return os.path.isabs(path)


def _windows_execution_target(path: str) -> str:
    """Windows itself strips trailing dots and spaces from the final path
    segment before deciding which file actually runs (a `CreateProcess`
    behavior, not something this module invents) - fix round 2, finding 5.
    A raw suffix check on the operator's literal string disagrees with
    what the OS executes: `"helper.cmd "` and `"helper.cmd."` both still
    run `helper.cmd`. Check the suffix of THIS normalized form, never the
    literal configured string, so a trailing space or dot cannot smuggle a
    refused suffix past the native-executable rule."""
    return path.rstrip(" .")


def _is_valid_env_entry(name: str, value: str) -> bool:
    """A name/value pair `subprocess.Popen` can actually place in a child's
    environment block without raising (fix round 1, finding 6): no '=' or
    NUL in the name (NUL is never valid in either), and a non-empty name."""
    if not name or "\x00" in name or "\x00" in value:
        return False
    return "=" not in name


@dataclass(frozen=True)
class TurnAdmissionConfig:
    """Operator-only configuration (spec 4.1, AC3).

    Build this ONLY from the operator's own local wrapper configuration
    (e.g. a key in `supervisor.json`) - never from a bus message, an agent,
    task text, or an agent-settable environment variable. This dataclass
    itself enforces nothing about WHERE its values came from; that
    guarantee is a property of what calls `from_mapping`, which is why the
    acceptance test for AC3 shows those other sources are never consulted
    anywhere in this module.
    """

    command: str
    args: tuple[str, ...]
    cwd: str
    env: dict[str, str]
    timeout_seconds: float
    sha256: str | None = field(default=None)

    @staticmethod
    def from_mapping(raw: dict) -> "TurnAdmissionConfig":
        """Validate and build a config from an operator-supplied mapping.

        Raises `ValueError` (never one of the eight runtime failure words -
        those describe a CALL's own outcome, not a malformed operator
        config) with a plain description of what is wrong; these messages
        never contain configured values that could be a token, because none
        of `command`/`args`/`cwd`/`env` are ever echoed - only which field
        and what kind of problem.
        """
        if not isinstance(raw, dict):
            raise ValueError("turn admission configuration must be an object")
        command = raw.get("command")
        if not isinstance(command, str) or not command:
            raise ValueError("turn admission 'command' must be a non-empty string")
        if "\x00" in command:
            raise ValueError("turn admission 'command' must not contain a NUL character")
        if not _is_robust_absolute_path(command):
            raise ValueError(
                "turn admission 'command' must be an absolute path (a drive letter or UNC "
                "root on Windows), never searched on PATH"
            )
        # Fix round 2, finding 5: check the NORMALIZED execution target (the
        # suffix Windows itself will see after stripping trailing dots and
        # spaces), never the operator's literal string.
        if os.name == "nt" and _windows_execution_target(command).casefold().endswith(_WINDOWS_SHELL_SUFFIXES):
            raise ValueError(
                "turn admission 'command' must be a native executable - "
                "Windows can run .bat/.cmd through a shell even with shell=False"
            )
        args_raw = raw.get("args", [])
        if not isinstance(args_raw, list) or not all(isinstance(a, str) for a in args_raw):
            raise ValueError("turn admission 'args' must be a list of strings")
        if any("\x00" in a for a in args_raw):
            raise ValueError("turn admission 'args' must not contain a NUL character")
        cwd = raw.get("cwd")
        if not isinstance(cwd, str) or not cwd:
            raise ValueError(
                "turn admission 'cwd' must be an absolute path (a drive letter or UNC root on Windows)"
            )
        if "\x00" in cwd:
            raise ValueError("turn admission 'cwd' must not contain a NUL character")
        if not _is_robust_absolute_path(cwd):
            raise ValueError(
                "turn admission 'cwd' must be an absolute path (a drive letter or UNC root on Windows)"
            )
        env_raw = raw.get("env", {})
        if not isinstance(env_raw, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in env_raw.items()):
            raise ValueError("turn admission 'env' must be a mapping of strings to strings")
        if not all(_is_valid_env_entry(k, v) for k, v in env_raw.items()):
            raise ValueError(
                "turn admission 'env' has an invalid entry (a name must be non-empty, "
                "contain no '=', and neither a name nor a value may contain a NUL)"
            )
        if os.name == "nt":
            # Fix round 2, finding 7: Windows environment names are
            # case-insensitive, but a Python dict key is not - two names
            # that collide under Windows' own rules (e.g. "SYSTEMROOT" and
            # "SystemRoot") would both reach Popen as distinct keys,
            # leaving it ambiguous which value actually wins.
            casefolded_names = [name.casefold() for name in env_raw]
            if len(casefolded_names) != len(set(casefolded_names)):
                raise ValueError(
                    "turn admission 'env' has two names that differ only by case "
                    "(Windows environment names are case-insensitive)"
                )
        timeout_raw = raw.get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS)
        if isinstance(timeout_raw, bool) or not isinstance(timeout_raw, (int, float)):
            raise ValueError("turn admission 'timeout_seconds' must be a number")
        timeout_seconds = float(timeout_raw)
        if not (0 < timeout_seconds <= _MAX_TIMEOUT_SECONDS):
            raise ValueError(
                f"turn admission 'timeout_seconds' must be > 0 and at most {_MAX_TIMEOUT_SECONDS}"
            )
        sha256 = raw.get("sha256")
        if sha256 is not None:
            if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", sha256):
                raise ValueError("turn admission 'sha256' pin must be 64 hexadecimal characters")
            sha256 = sha256.lower()
        return TurnAdmissionConfig(
            command=command, args=tuple(args_raw), cwd=cwd,
            env=dict(env_raw), timeout_seconds=timeout_seconds, sha256=sha256,
        )


def _build_child_env(config: TurnAdmissionConfig) -> dict[str, str]:
    env = dict(config.env)
    # Fix round 2, finding 7: compare case-insensitively - an operator env
    # entry that already names a system default under a different case
    # (Windows environment names are case-insensitive; `from_mapping`
    # already rejects two OPERATOR names colliding this way) must still be
    # recognized here, or this function would add a second, differently
    # cased key for the same conceptual variable.
    existing_casefolded = {name.casefold() for name in env}
    for name, default in _SYSTEM_ENV_DEFAULTS.items():
        if name.casefold() not in existing_casefolded:
            env[name] = os.environ.get(name, default)
    return env


# --------------------------------------------------------------- strict JSON


class _JsonStrictnessError(ValueError):
    """Internal sentinel: duplicate key or a non-finite numeric constant."""


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    out: dict[str, object] = {}
    for key, value in pairs:
        if key in out:
            raise _JsonStrictnessError(f"duplicate key: {key!r}")
        out[key] = value
    return out


def _reject_constant(token: str) -> None:
    raise _JsonStrictnessError(f"non-finite constant: {token}")


def _dumps_compact(obj: dict) -> bytes:
    """Compact, sorted-key, ASCII-only JSON (spec 4.2) - what WE send."""
    text = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return text.encode("ascii")


def _strict_loads(data: bytes) -> dict:
    """Strict JSON parse of what the PROGRAM sent (spec 4.2).

    Refuses non-ASCII bytes, duplicate object keys, NaN/Infinity/-Infinity,
    and trailing data after the single JSON value. Raises
    `TurnAdmissionFailure("not_json")` for any violation - never `ValueError`
    directly, so a caller cannot mistake a malformed-program-output failure
    for a bug in this module's own request construction.
    """
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        raise TurnAdmissionFailure("not_json") from None
    decoder = json.JSONDecoder(object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_constant)
    try:
        obj, end = decoder.raw_decode(text)
    except (ValueError, _JsonStrictnessError):
        raise TurnAdmissionFailure("not_json") from None
    if text[end:].strip():
        raise TurnAdmissionFailure("not_json")
    if not isinstance(obj, dict):
        raise TurnAdmissionFailure("not_json")
    return obj


# --------------------------------------------------------------- field bounds

_MESSAGE_ID_RE = re.compile(r"\A[A-Za-z0-9._:-]{1,32}\Z")
_PARENT_REQUEST_ID_RE = re.compile(r"\A[A-Za-z0-9._:-]{1,32}\Z")
_PROFILE_RE = re.compile(r"\A[a-z0-9-]{1,32}\Z")
_REFERENCE_RE = re.compile(r"\A[A-Za-z0-9._:-]{1,64}\Z")


def _is_plain_int(value: object) -> bool:
    """True only for a real int - booleans and floats are refused (spec 4.2's
    "Integer fields, including v=1, refuse booleans and floats")."""
    return isinstance(value, int) and not isinstance(value, bool)


def _require(condition: bool, word: str = "out_of_bounds") -> None:
    if not condition:
        raise TurnAdmissionFailure(word)


def _validate_agent(agent: str) -> str:
    try:
        return store_mod.validate_agent_name(agent)
    except ValueError:
        raise TurnAdmissionFailure("out_of_bounds") from None


def _validate_message_id(message_id: str) -> str:
    _require(isinstance(message_id, str) and bool(_MESSAGE_ID_RE.match(message_id)))
    return message_id


def _validate_parent_request_id(parent_request_id: str) -> str:
    _require(isinstance(parent_request_id, str)
              and (parent_request_id == "" or bool(_PARENT_REQUEST_ID_RE.match(parent_request_id))))
    return parent_request_id


def _validate_profile(profile: str) -> str:
    _require(isinstance(profile, str) and bool(_PROFILE_RE.match(profile)))
    return profile


def _validate_mode(mode: str) -> str:
    _require(mode in ("fresh", "resume"))
    return mode


def _validate_dispatch_bytes(dispatch_bytes: int) -> int:
    _require(_is_plain_int(dispatch_bytes) and 0 <= dispatch_bytes <= 10**12 - 1)
    return dispatch_bytes


def _validate_limit(limit: int) -> int:
    _require(_is_plain_int(limit) and 1 <= limit <= 64)
    return limit


def _validate_reference(reference: str) -> str:
    _require(isinstance(reference, str) and bool(_REFERENCE_RE.match(reference)))
    return reference


def _validate_optional_cap(value: int | None, *, upper: int) -> int | None:
    if value is None:
        return None
    _require(_is_plain_int(value) and 1 <= value <= upper)
    return value


def _validate_ttl_seconds(ttl_seconds: int) -> int:
    _require(_is_plain_int(ttl_seconds) and 1 <= ttl_seconds <= 86400)
    return ttl_seconds


# --------------------------------------------------------------- answer shapes

def _bad_shape(condition: bool) -> None:
    if not condition:
        raise TurnAdmissionFailure("bad_shape")


def _check_keys(answer: dict, required: set[str]) -> None:
    _bad_shape(set(answer.keys()) == required)


def _check_v(answer: dict) -> None:
    _bad_shape(_is_plain_int(answer.get("v")) and answer["v"] == PROTOCOL_VERSION)


def _validate_admitted_answer(answer: dict) -> dict:
    _check_keys(answer, {"max_calls", "max_micro_eur", "reference", "status", "ttl_seconds", "v"})
    _check_v(answer)
    _bad_shape(answer.get("status") == "admitted")
    max_calls = answer.get("max_calls")
    _bad_shape(max_calls is None or (_is_plain_int(max_calls) and 1 <= max_calls <= 100_000))
    max_micro_eur = answer.get("max_micro_eur")
    _bad_shape(max_micro_eur is None or (_is_plain_int(max_micro_eur) and 1 <= max_micro_eur <= 10**12 - 1))
    reference = answer.get("reference")
    _bad_shape(isinstance(reference, str) and bool(_REFERENCE_RE.match(reference)))
    ttl_seconds = answer.get("ttl_seconds")
    _bad_shape(_is_plain_int(ttl_seconds) and 1 <= ttl_seconds <= 86400)
    return answer


def _validate_held_answer(answer: dict) -> dict:
    _check_keys(answer, {"reason", "status", "v"})
    _check_v(answer)
    _bad_shape(answer.get("status") == "held")
    # Fix round 1, finding 6: `reason` must be checked as a string BEFORE
    # the membership test - `[] in HELD_REASONS` raises TypeError (a
    # frozenset membership test hashes its argument), which would escape
    # the closed failure vocabulary entirely for a held answer carrying a
    # non-string reason (a list, a number, null...).
    reason = answer.get("reason")
    _bad_shape(isinstance(reason, str) and reason in HELD_REASONS)
    return answer


def _validate_admit_answer(answer: dict) -> dict:
    status = answer.get("status")
    if status == "admitted":
        return _validate_admitted_answer(answer)
    if status == "held":
        return _validate_held_answer(answer)
    raise TurnAdmissionFailure("bad_shape")


def _validate_recall_answer(answer: dict) -> dict:
    status = answer.get("status")
    if status == "admitted":
        return _validate_admitted_answer(answer)
    if status == "unknown":
        _check_keys(answer, {"status", "v"})
        _check_v(answer)
        return answer
    raise TurnAdmissionFailure("bad_shape")


def _validate_pending_answer(answer: dict, *, limit: int) -> dict:
    _check_keys(answer, {"messages", "status", "v"})
    _check_v(answer)
    _bad_shape(answer.get("status") == "ok")
    messages = answer.get("messages")
    _bad_shape(isinstance(messages, list) and 0 <= len(messages) <= limit)
    for entry in messages:
        _bad_shape(isinstance(entry, dict) and set(entry.keys()) == {"message_id", "reference"})
        _bad_shape(isinstance(entry["message_id"], str) and bool(_MESSAGE_ID_RE.match(entry["message_id"])))
        _bad_shape(isinstance(entry["reference"], str) and bool(_REFERENCE_RE.match(entry["reference"])))
    return answer


def _validate_closed_answer(answer: dict) -> dict:
    _check_keys(answer, {"status", "v"})
    _check_v(answer)
    _bad_shape(answer.get("status") == "ok")
    return answer


# --------------------------------------------------------------- the runner

def _verify_pin(config: TurnAdmissionConfig, deadline: float) -> None:
    """Verify the executable's SHA-256 pin, when one is configured (AC4),
    before every call. Bounded by the SAME overall deadline as everything
    else (fix round 1, finding 4) - this used to run before the deadline
    even existed, so a slow read got a free extension. Any failure to even
    read the file is reported as `not_started` (the program is not usable);
    a computed mismatch is `pin_mismatch`; running past the deadline is
    `timeout`. Never reports the hash, matched or not.

    The pin is a PRE-LAUNCH check, not protection against the file being
    replaced between this check and the launch a moment later (item 10) -
    see the README for the operator-protected-folder requirement this
    implies.
    """
    if config.sha256 is None:
        return
    if time.monotonic() > deadline:
        raise TurnAdmissionFailure("timeout")
    try:
        with open(config.command, "rb") as f:  # noqa: PTH123 - config.command is an absolute path, validated at config time
            digest = hashlib.sha256()
            while True:
                if time.monotonic() > deadline:
                    raise TurnAdmissionFailure("timeout")
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError:
        raise TurnAdmissionFailure("not_started") from None
    if digest.hexdigest() != config.sha256:
        raise TurnAdmissionFailure("pin_mismatch")


if os.name == "nt":
    _ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    _ntdll.NtResumeProcess.argtypes = [ctypes.c_void_p]
    _ntdll.NtResumeProcess.restype = ctypes.c_uint32
    # The Win32 CREATE_SUSPENDED process-creation flag. Not consistently
    # exposed as subprocess.CREATE_SUSPENDED across Python versions (absent
    # on the interpreter this was built against) - the numeric value is a
    # stable, documented part of the CreateProcess API, not an
    # implementation detail that could drift.
    _CREATE_SUSPENDED = getattr(subprocess, "CREATE_SUSPENDED", 0x00000004)


def _resume_suspended_process(proc: subprocess.Popen) -> None:
    """Resume a process created with CREATE_SUSPENDED (fix round 1, finding
    1). `subprocess.Popen` closes the main thread's handle immediately
    after `CreateProcess` returns (CPython's `subprocess.py` does this on
    every Windows launch, not just this one), so `ResumeThread` has nothing
    to call it on. `NtResumeProcess` resumes every thread in a process
    given only the PROCESS handle, which `Popen` does keep - the documented
    alternative the finding names."""
    status = _ntdll.NtResumeProcess(ctypes.c_void_p(int(proc._handle)))  # noqa: SLF001 - the only process handle Popen exposes on Windows
    if status != 0:
        raise OSError(f"NtResumeProcess failed: NTSTATUS=0x{status:08x}")


def _spawn(config: TurnAdmissionConfig) -> subprocess.Popen:
    popen_kwargs: dict[str, object] = {
        "cwd": config.cwd,
        "env": _build_child_env(config),
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.DEVNULL,  # discarded without accumulating (spec 4.4, AC7)
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }
    if os.name == "nt":
        # Fix round 1, finding 1: contain the process BEFORE its first
        # instruction runs. Created suspended here; the caller must assign
        # it to the kill-on-close job and then explicitly resume it
        # (`_resume_suspended_process`) - never left running un-contained,
        # and never left suspended forever either.
        popen_kwargs["creationflags"] |= _CREATE_SUSPENDED
    else:
        # POSIX has no equivalent race: `start_new_session=True` establishes
        # the new session/process group in the forked child BEFORE exec()
        # replaces it with the configured program, so containment is
        # already in place before the program's first instruction runs.
        popen_kwargs["start_new_session"] = True
    try:
        return subprocess.Popen(  # nosec B603 - shell=False; command is an operator-configured absolute path, args are a fixed operator list, no task data
            [config.command, *config.args], **popen_kwargs
        )
    except (OSError, ValueError):
        # Fix round 2, finding 4: configuration-time validation rejects NULs
        # in every string that reaches here, but this is defense in depth -
        # `subprocess.Popen` itself raises a bare `ValueError` (not
        # `OSError`) for some malformed launch strings (e.g. "embedded null
        # character"), which must never reach a caller with its own text.
        raise TurnAdmissionFailure("not_started") from None


def _kill_tree(proc: subprocess.Popen, close_job, cleanup_deadline: float) -> None:
    """Terminate and reap the WHOLE process tree by exact process id - a
    process group on POSIX, the Job Object on Windows (spec 4.4). Never by
    image name. Bounded by `cleanup_deadline` (the SAME absolute
    `time.monotonic()` end time `_call` computed once at its own start -
    fix round 2's structural fix - never a fresh "now plus a constant"
    budget of its own; fix round 1, finding 4 first tried a shared budget
    but still created it anew after each failure, which a fake clock
    showed could still add a full extra allowance past the configured
    timeout). Best-effort beyond this point: a process that has already
    exited must never turn a clean failure into a crash here.

    POSIX container precondition (item 11): reaping an orphaned grandchild
    after its own parent is killed requires PID 1 (or another designated
    reaper) to adopt and wait() it - run under an init that reaps, for
    example `docker run --init`, or a grandchild can linger as a zombie
    this function has no standing to wait() on directly (only an ancestor
    may).
    """
    with contextlib.suppress(OSError):
        close_job()
    if os.name != "nt":
        # SIGKILL is always 9 on POSIX; the name is absent from Windows' signal module.
        with contextlib.suppress(ProcessLookupError, OSError):
            os.killpg(proc.pid, getattr(signal, "SIGKILL", 9))
    with contextlib.suppress(OSError):
        proc.kill()
    with contextlib.suppress(subprocess.TimeoutExpired, OSError):
        proc.wait(timeout=max(0.0, cleanup_deadline - time.monotonic()))


def _call(config: TurnAdmissionConfig, op: str, request: dict, validate) -> dict:
    """Run one operation end to end (spec 4.4): verify the pin, spawn,
    write the request, read the answer under its cap, validate its
    operation-specific shape, apply one overall deadline to all of pin
    verification / writing / reading / exit / cleanup, and reap the whole
    process tree by exact pid on any failure or abandonment.

    Fix round 2's structural fix: ONE absolute end time (`deadline`) is
    computed here, once, before the pin check and before spawning - never
    recomputed, and never replaced by a fresh "now plus a constant" budget
    after a failure. Cleanup (terminate + reap + join the I/O threads)
    draws from whatever is LEFT of this SAME `deadline`, never a budget of
    its own - so the total of a failed call, cleanup included, can never
    exceed the configured timeout (a fake clock proved the previous round's
    "shared" budget still added a full fresh allowance after the operation
    already gave up: a 0.1-second call took 2.1 seconds. Reusing one
    deadline throughout makes that structurally impossible: whatever time
    the operation spent is time cleanup no longer has).

    `proc = _spawn(config)` is the very FIRST statement inside the owning
    try/finally (fix round 2, finding 1): an interruption on literally the
    next line still reaches the finally block and reaps the process,
    where the previous round's `proc = _spawn(config)` sat BEFORE its own
    try, leaving a window where cancellation right after spawn escaped
    cleanup entirely. `validate` (the operation-specific answer-shape
    check) is also called INSIDE this same try, before `success` is ever
    set - success is decided by the full validated shape, not merely that
    the bytes read were valid JSON, so an otherwise-well-formed answer
    with an unexpected field still tears down the process tree."""
    if op not in OPERATIONS:
        raise ValueError(f"not a turn admission operation: {op!r}")
    deadline = time.monotonic() + config.timeout_seconds

    _verify_pin(config, deadline)
    payload = _dumps_compact(request)
    cap = _ANSWER_CAPS[op]

    close_job = lambda: None  # noqa: E731
    writer_thread: threading.Thread | None = None
    reader_thread: threading.Thread | None = None
    success = False
    proc: subprocess.Popen | None = None
    try:
        proc = _spawn(config)
        if os.name == "nt":
            try:
                _, close_job = _attach_kill_on_close_job(proc)
                _resume_suspended_process(proc)
            except OSError:
                raise TurnAdmissionFailure("not_started") from None

        write_done = threading.Event()
        write_ok = threading.Event()
        read_done = threading.Event()
        read_ok = threading.Event()
        read_overflow = threading.Event()
        output = bytearray()

        def writer() -> None:
            try:
                proc.stdin.write(payload)
                proc.stdin.close()
                write_ok.set()
            except (OSError, ValueError):
                pass
            finally:
                write_done.set()

        def reader() -> None:
            try:
                remaining = cap + 1
                while remaining > 0:
                    chunk = proc.stdout.read(min(4096, remaining))
                    if not chunk:
                        read_ok.set()
                        break
                    output.extend(chunk)
                    remaining -= len(chunk)
                else:
                    read_overflow.set()
                if len(output) > cap:
                    read_overflow.set()
            except (OSError, ValueError):
                # Fix round 2, finding 2: a read error at ANY point - even
                # after a complete, legal answer already arrived - must
                # never be silently discarded. `read_ok` is only ever set
                # on a clean EOF above; it stays unset here, so the answer
                # is vetoed below exactly like a failed write.
                pass
            finally:
                read_done.set()

        try:
            writer_thread = threading.Thread(target=writer, daemon=True)
            reader_thread = threading.Thread(target=reader, daemon=True)
            writer_thread.start()
            reader_thread.start()
        except RuntimeError:
            raise TurnAdmissionFailure("not_started") from None

        timed_out = False
        while True:
            remaining_time = deadline - time.monotonic()
            if read_overflow.is_set():
                break
            if write_done.is_set() and read_done.is_set():
                break
            if remaining_time <= 0:
                timed_out = True
                break
            wait_slice = min(0.02, max(remaining_time, 0.0))
            write_done.wait(timeout=wait_slice)
            read_done.wait(timeout=wait_slice)

        returncode = None
        if not timed_out and not read_overflow.is_set():
            remaining_time = max(deadline - time.monotonic(), 0.0)
            try:
                returncode = proc.wait(timeout=remaining_time)
            except subprocess.TimeoutExpired:
                timed_out = True

        if timed_out:
            raise TurnAdmissionFailure("timeout")
        if read_overflow.is_set():
            raise TurnAdmissionFailure("too_large")
        # Fix round 1, finding 3 (write) and fix round 2, finding 2 (read):
        # either half of the exchange failing must veto success even if
        # the program still produced a valid-looking answer and exited
        # zero - mapped to `not_started` ("the program did not start"
        # generalizes to "the program never actually completed a call").
        if not write_ok.is_set():
            raise TurnAdmissionFailure("not_started")
        if not read_ok.is_set():
            raise TurnAdmissionFailure("not_started")
        if returncode != 0:
            raise TurnAdmissionFailure("exit_code")

        answer = _strict_loads(bytes(output))
        validated = validate(answer)
        success = True
        return validated
    finally:
        if proc is not None:
            if success:
                with contextlib.suppress(OSError):
                    close_job()
                if os.name != "nt":
                    # Fix round 2 (the connector's successful-call point):
                    # a successful call can still leave descendants behind
                    # on POSIX, since only the root was waited on - close
                    # the group the same way Windows closes its job, on
                    # every outcome, not only a failed one.
                    with contextlib.suppress(ProcessLookupError, OSError):
                        os.killpg(proc.pid, getattr(signal, "SIGKILL", 9))
            else:
                _kill_tree(proc, close_job, deadline)
            if writer_thread is not None:
                with contextlib.suppress(RuntimeError):
                    writer_thread.join(timeout=max(0.0, deadline - time.monotonic()))
            if reader_thread is not None:
                with contextlib.suppress(RuntimeError):
                    reader_thread.join(timeout=max(0.0, deadline - time.monotonic()))


# --------------------------------------------------------------- public API

def admit(
    config: TurnAdmissionConfig, *, agent: str, message_id: str, parent_request_id: str,
    profile: str, mode: str, dispatch_bytes: int,
) -> dict:
    """Ask whether a paid turn may run, and under what limits (spec 4.2).

    Returns the validated answer dict (`status` is `"admitted"` or
    `"held"`). Raises `TurnAdmissionFailure` with one of the eight closed
    words on any failure - the caller decides what that means (AC2, 5.3).
    """
    agent = _validate_agent(agent)
    message_id = _validate_message_id(message_id)
    parent_request_id = _validate_parent_request_id(parent_request_id)
    profile = _validate_profile(profile)
    mode = _validate_mode(mode)
    dispatch_bytes = _validate_dispatch_bytes(dispatch_bytes)
    request = {
        "agent": agent, "context": {"dispatch_bytes": dispatch_bytes, "mode": mode},
        "message_id": message_id, "op": "admit", "parent_request_id": parent_request_id,
        "profile": profile, "v": PROTOCOL_VERSION,
    }
    return _call(config, "admit", request, _validate_admit_answer)


def recall(config: TurnAdmissionConfig, *, agent: str, message_id: str) -> dict:
    """Recall a stored admission; never creates one (spec 4.2)."""
    agent = _validate_agent(agent)
    message_id = _validate_message_id(message_id)
    request = {"agent": agent, "message_id": message_id, "op": "recall", "v": PROTOCOL_VERSION}
    return _call(config, "recall", request, _validate_recall_answer)


def pending(config: TurnAdmissionConfig, *, agent: str, limit: int) -> dict:
    """List admissions not yet acknowledged as closed, oldest first
    (spec 4.2); never creates anything."""
    agent = _validate_agent(agent)
    limit = _validate_limit(limit)
    request = {"agent": agent, "limit": limit, "op": "pending", "v": PROTOCOL_VERSION}
    return _call(config, "pending", request, lambda answer: _validate_pending_answer(answer, limit=limit))


def closed(config: TurnAdmissionConfig, *, agent: str, message_id: str) -> dict:
    """Acknowledge that the gateway close has committed (spec 4.2)."""
    agent = _validate_agent(agent)
    message_id = _validate_message_id(message_id)
    request = {"agent": agent, "message_id": message_id, "op": "closed", "v": PROTOCOL_VERSION}
    return _call(config, "closed", request, _validate_closed_answer)


def doctor_line(config: TurnAdmissionConfig | None) -> str:
    """A `doctor`-style line (spec 4.5): configured or not, pinned or not -
    never the command path's private parts or the pin's value.

    This is a tested FORMATTER only in this patch - nothing calls it yet.
    `agenttalk doctor` wiring (reading the operator's configuration and
    printing this line, plus the turn-journal check spec 4.5 also asks
    for) is deferred to 6a-2b, where that configuration is actually read.
    """
    if config is None:
        return "turn admission: not configured"
    pinned = "pinned" if config.sha256 is not None else "not pinned"
    return f"turn admission: configured ({pinned})"
