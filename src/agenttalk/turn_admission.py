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
system names a process needs just to start (`_SYSTEM_ENV_NAMES`); nothing
is read from, or inherited from, this process's own environment for
configuration purposes.
"""

from __future__ import annotations

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
# is inherited.
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
        if not os.path.isabs(command):
            raise ValueError("turn admission 'command' must be an absolute path, never searched on PATH")
        args_raw = raw.get("args", [])
        if not isinstance(args_raw, list) or not all(isinstance(a, str) for a in args_raw):
            raise ValueError("turn admission 'args' must be a list of strings")
        cwd = raw.get("cwd")
        if not isinstance(cwd, str) or not cwd or not os.path.isabs(cwd):
            raise ValueError("turn admission 'cwd' must be an absolute path")
        env_raw = raw.get("env", {})
        if not isinstance(env_raw, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in env_raw.items()):
            raise ValueError("turn admission 'env' must be a mapping of strings to strings")
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
    for name, default in _SYSTEM_ENV_DEFAULTS.items():
        if name not in env:
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
    _bad_shape(answer.get("reason") in HELD_REASONS)
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

def _verify_pin(config: TurnAdmissionConfig) -> None:
    """Verify the executable's SHA-256 pin, when one is configured (AC4),
    before every call. Any failure to even read the file is reported as
    `not_started` (the program is not usable); a computed mismatch is
    `pin_mismatch`. Never reports the hash, matched or not."""
    if config.sha256 is None:
        return
    import hashlib
    try:
        with open(config.command, "rb") as f:  # noqa: PTH123 - config.command is an absolute path, validated at config time
            digest = hashlib.sha256()
            while chunk := f.read(1 << 20):
                digest.update(chunk)
    except OSError:
        raise TurnAdmissionFailure("not_started") from None
    if digest.hexdigest() != config.sha256:
        raise TurnAdmissionFailure("pin_mismatch")


def _spawn(config: TurnAdmissionConfig) -> subprocess.Popen:
    popen_kwargs: dict[str, object] = {
        "cwd": config.cwd,
        "env": _build_child_env(config),
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.DEVNULL,  # discarded without accumulating (spec 4.4, AC7)
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }
    if os.name != "nt":
        popen_kwargs["start_new_session"] = True  # own process group, for exact-pid group kill
    try:
        return subprocess.Popen(  # nosec B603 - shell=False; command is an operator-configured absolute path, args are a fixed operator list, no task data
            [config.command, *config.args], **popen_kwargs
        )
    except OSError:
        raise TurnAdmissionFailure("not_started") from None


def _kill_tree(proc: subprocess.Popen, close_job) -> None:
    """Terminate and reap the WHOLE process tree by exact process id - a
    process group on POSIX, the Job Object on Windows (spec 4.4). Never by
    image name. Best-effort beyond this point: a process that has already
    exited must never turn a clean failure into a crash here."""
    try:
        close_job()
    except OSError:
        pass
    if os.name != "nt":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            pass
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        pass


def _call(config: TurnAdmissionConfig, op: str, request: dict) -> dict:
    """Run one operation end to end (spec 4.4): verify the pin, spawn,
    write the request, read the answer under its cap, apply one overall
    deadline to all of writing / reading / exit / cleanup, and reap the
    whole process tree by exact pid on any failure or abandonment."""
    if op not in OPERATIONS:
        raise ValueError(f"not a turn admission operation: {op!r}")
    _verify_pin(config)
    payload = _dumps_compact(request)
    deadline = time.monotonic() + config.timeout_seconds
    cap = _ANSWER_CAPS[op]

    proc = _spawn(config)
    close_job = lambda: None  # noqa: E731
    if os.name == "nt":
        try:
            _, close_job = _attach_kill_on_close_job(proc)
        except OSError:
            _kill_tree(proc, close_job)
            raise TurnAdmissionFailure("not_started") from None

    write_done = threading.Event()
    write_error = threading.Event()
    read_done = threading.Event()
    read_overflow = threading.Event()
    output = bytearray()

    def writer() -> None:
        try:
            proc.stdin.write(payload)
            proc.stdin.close()
        except (OSError, ValueError):
            write_error.set()
        finally:
            write_done.set()

    def reader() -> None:
        try:
            while True:
                chunk = proc.stdout.read(4096)
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > cap:
                    read_overflow.set()
                    break
        except (OSError, ValueError):
            pass
        finally:
            read_done.set()

    writer_thread = threading.Thread(target=writer, daemon=True)
    reader_thread = threading.Thread(target=reader, daemon=True)
    writer_thread.start()
    reader_thread.start()

    timed_out = False
    try:
        while True:
            remaining = deadline - time.monotonic()
            if read_overflow.is_set():
                break
            if write_done.is_set() and read_done.is_set():
                break
            if remaining <= 0:
                timed_out = True
                break
            write_done.wait(timeout=min(0.05, max(remaining, 0.0)))
            read_done.wait(timeout=min(0.05, max(remaining, 0.0)))
        returncode = None
        if not timed_out and not read_overflow.is_set():
            remaining = max(deadline - time.monotonic(), 0.0)
            try:
                returncode = proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                timed_out = True
    finally:
        if timed_out or read_overflow.is_set():
            _kill_tree(proc, close_job)
        else:
            try:
                close_job()
            except OSError:
                pass
        # These threads only ever block on I/O against a process we have
        # just ensured is gone (or already finished) - bounded joins are a
        # belt-and-suspenders reap, never the primary termination signal.
        writer_thread.join(timeout=5.0)
        reader_thread.join(timeout=5.0)

    if timed_out:
        raise TurnAdmissionFailure("timeout")
    if read_overflow.is_set():
        raise TurnAdmissionFailure("too_large")
    if returncode != 0:
        raise TurnAdmissionFailure("exit_code")

    answer = _strict_loads(bytes(output))
    return answer


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
    answer = _call(config, "admit", request)
    return _validate_admit_answer(answer)


def recall(config: TurnAdmissionConfig, *, agent: str, message_id: str) -> dict:
    """Recall a stored admission; never creates one (spec 4.2)."""
    agent = _validate_agent(agent)
    message_id = _validate_message_id(message_id)
    request = {"agent": agent, "message_id": message_id, "op": "recall", "v": PROTOCOL_VERSION}
    answer = _call(config, "recall", request)
    return _validate_recall_answer(answer)


def pending(config: TurnAdmissionConfig, *, agent: str, limit: int) -> dict:
    """List admissions not yet acknowledged as closed, oldest first
    (spec 4.2); never creates anything."""
    agent = _validate_agent(agent)
    limit = _validate_limit(limit)
    request = {"agent": agent, "limit": limit, "op": "pending", "v": PROTOCOL_VERSION}
    answer = _call(config, "pending", request)
    return _validate_pending_answer(answer, limit=limit)


def closed(config: TurnAdmissionConfig, *, agent: str, message_id: str) -> dict:
    """Acknowledge that the gateway close has committed (spec 4.2)."""
    agent = _validate_agent(agent)
    message_id = _validate_message_id(message_id)
    request = {"agent": agent, "message_id": message_id, "op": "closed", "v": PROTOCOL_VERSION}
    answer = _call(config, "closed", request)
    return _validate_closed_answer(answer)


def doctor_line(config: TurnAdmissionConfig | None) -> str:
    """One `doctor` line (spec 4.5): configured or not, pinned or not -
    never the command path's private parts or the pin's value."""
    if config is None:
        return "turn admission: not configured"
    pinned = "pinned" if config.sha256 is not None else "not pinned"
    return f"turn admission: configured ({pinned})"
