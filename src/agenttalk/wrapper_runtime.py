"""Strict wrapper-turn health observation.

``wrapper-runtime.json`` is written only by the wrapper process and consumed by
supervisor health (#72).  It does not authorize bus commit/cursor decisions;
#73 independently validates the bus for those.  This is a consistency record,
not an authentication boundary.  The codec is intentionally closed and
fail-closed: a reader either receives one complete validated record or no
usable observation.
"""

from __future__ import annotations

import errno
import json
import math
import os
import re
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from agenttalk.store import validate_agent_name

SCHEMA_VERSION = 1
MAX_RECORD_BYTES = 16 * 1024
# The generated supervisor captures integer ``--now`` before starting Python,
# while the wrapper can publish concurrently.  Bound that observation race
# without letting a genuinely future-dated record authorize health.
MAX_FUTURE_SKEW_SECONDS = 30.0

PHASE_IDLE = "idle"
PHASE_STARTING = "starting"
PHASE_ACTIVE = "active"
PHASE_TERMINAL = "terminal"
PHASES = frozenset({
    PHASE_IDLE,
    PHASE_STARTING,
    PHASE_ACTIVE,
    PHASE_TERMINAL,
})

OUTCOME_SUCCESS = "success"
OUTCOME_FAILED = "failed"
OUTCOME_DEAD_LETTER = "dead_letter"
OUTCOMES = frozenset({
    OUTCOME_SUCCESS,
    OUTCOME_FAILED,
    OUTCOME_DEAD_LETTER,
})

STATUS_VALID = "valid"
STATUS_ABSENT = "absent"
STATUS_INVALID = "invalid"

RECORD_KEYS = frozenset({
    "schema_version",
    "agent",
    "wrapper_pid",
    "wrapper_start",
    "wrapper_generation",
    "phase",
    "turn_generation",
    "turn_id",
    "message_id",
    "cli_launcher_pid",
    "cli_launcher_start",
    "progress_sequence",
    "last_progress_at",
    "last_outcome",
    "updated_at",
})

_SAFE_GENERATION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_UNSET = object()


class RuntimeRecordError(ValueError):
    """The record is malformed, inconsistent, or outside its closed schema."""


class RuntimeWriteError(OSError):
    """The wrapper could not durably publish a lifecycle transition."""


def _utc_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def _parse_utc(value: object, *, field: str) -> float:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise RuntimeRecordError(f"{field} must be a bounded UTC timestamp")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise RuntimeRecordError(f"{field} must be a UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeRecordError(f"{field} must include a UTC offset")
    if parsed.utcoffset().total_seconds() != 0:
        raise RuntimeRecordError(f"{field} must be UTC")
    epoch = parsed.timestamp()
    if not math.isfinite(epoch):
        raise RuntimeRecordError(f"{field} must be finite")
    return epoch


def _safe_optional_text(value: object, *, field: str, limit: int = 256) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or len(value) > limit
        or any(ord(char) < 32 for char in value)
    ):
        raise RuntimeRecordError(f"{field} must be null or bounded text")
    return value


def _nonnegative_int(value: object, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise RuntimeRecordError(f"{field} must be a non-negative integer")
    return value


def _positive_pid(value: object, *, field: str, optional: bool = False) -> int | None:
    if optional and value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        suffix = " or null" if optional else ""
        raise RuntimeRecordError(f"{field} must be a positive integer{suffix}")
    return value


def runtime_path(state_dir: str | os.PathLike[str], agent: str) -> Path:
    try:
        return (
            Path(state_dir)
            / f"{validate_agent_name(agent)}.wrapper-runtime.json"
        )
    except ValueError as exc:
        raise RuntimeRecordError("agent is not a safe identity") from exc


def validate_record(
    raw: object,
    *,
    expected_agent: str | None = None,
    now_epoch: float | None = None,
) -> dict:
    """Return a normalized copy or raise :class:`RuntimeRecordError`."""
    if not isinstance(raw, dict):
        raise RuntimeRecordError("record must be an object")
    unknown = set(raw) - RECORD_KEYS
    missing = RECORD_KEYS - set(raw)
    if unknown:
        raise RuntimeRecordError(
            "record has unknown keys: " + ", ".join(sorted(unknown))
        )
    if missing:
        raise RuntimeRecordError(
            "record is missing keys: " + ", ".join(sorted(missing))
        )
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeRecordError("unsupported schema_version")

    try:
        agent = validate_agent_name(raw.get("agent"))
    except ValueError as exc:
        raise RuntimeRecordError("agent is not a safe identity") from exc
    if expected_agent is not None and agent != expected_agent:
        raise RuntimeRecordError("record agent does not match the requested identity")

    wrapper_generation = raw.get("wrapper_generation")
    if (
        not isinstance(wrapper_generation, str)
        or _SAFE_GENERATION.fullmatch(wrapper_generation) is None
    ):
        raise RuntimeRecordError("wrapper_generation is invalid")
    phase = raw.get("phase")
    if phase not in PHASES:
        raise RuntimeRecordError("phase is invalid")

    normalized = {
        "schema_version": SCHEMA_VERSION,
        "agent": agent,
        "wrapper_pid": _positive_pid(raw.get("wrapper_pid"), field="wrapper_pid"),
        "wrapper_start": _safe_optional_text(
            raw.get("wrapper_start"), field="wrapper_start"
        ),
        "wrapper_generation": wrapper_generation,
        "phase": phase,
        "turn_generation": _nonnegative_int(
            raw.get("turn_generation"), field="turn_generation"
        ),
        "turn_id": _safe_optional_text(raw.get("turn_id"), field="turn_id"),
        "message_id": _safe_optional_text(raw.get("message_id"), field="message_id"),
        "cli_launcher_pid": _positive_pid(
            raw.get("cli_launcher_pid"),
            field="cli_launcher_pid",
            optional=True,
        ),
        "cli_launcher_start": _safe_optional_text(
            raw.get("cli_launcher_start"), field="cli_launcher_start"
        ),
        "progress_sequence": _nonnegative_int(
            raw.get("progress_sequence"), field="progress_sequence"
        ),
        "last_progress_at": _safe_optional_text(
            raw.get("last_progress_at"), field="last_progress_at", limit=64
        ),
        "last_outcome": raw.get("last_outcome"),
        "updated_at": _safe_optional_text(
            raw.get("updated_at"), field="updated_at", limit=64
        ),
    }
    if normalized["updated_at"] is None:
        raise RuntimeRecordError("updated_at is required")
    updated_epoch = _parse_utc(normalized["updated_at"], field="updated_at")
    progress_epoch = None
    if normalized["last_progress_at"] is not None:
        progress_epoch = _parse_utc(
            normalized["last_progress_at"], field="last_progress_at"
        )
    if normalized["last_outcome"] is not None and normalized["last_outcome"] not in OUTCOMES:
        raise RuntimeRecordError("last_outcome is invalid")

    now = time.time() if now_epoch is None else float(now_epoch)
    if not math.isfinite(now):
        raise RuntimeRecordError("now_epoch must be finite")
    if updated_epoch > now + MAX_FUTURE_SKEW_SECONDS:
        raise RuntimeRecordError("updated_at is too far in the future")
    if progress_epoch is not None and progress_epoch > now + MAX_FUTURE_SKEW_SECONDS:
        raise RuntimeRecordError("last_progress_at is too far in the future")
    if progress_epoch is not None and progress_epoch > updated_epoch + 0.001:
        raise RuntimeRecordError("last_progress_at cannot be after updated_at")

    if phase == PHASE_IDLE:
        if any(
            normalized[key] is not None
            for key in (
                "turn_id",
                "message_id",
                "cli_launcher_pid",
                "cli_launcher_start",
            )
        ):
            raise RuntimeRecordError("idle phase carries active-turn fields")
    else:
        if normalized["turn_generation"] <= 0 or normalized["turn_id"] is None:
            raise RuntimeRecordError("non-idle phase requires a turn identity")
    if phase == PHASE_STARTING:
        if (
            normalized["cli_launcher_pid"] is not None
            or normalized["cli_launcher_start"] is not None
            or normalized["last_outcome"] is not None
            or normalized["last_progress_at"] is not None
        ):
            raise RuntimeRecordError("starting phase fields are inconsistent")
    elif phase == PHASE_ACTIVE:
        if normalized["cli_launcher_pid"] is None:
            raise RuntimeRecordError("active phase requires cli_launcher_pid")
        if normalized["last_outcome"] is not None:
            raise RuntimeRecordError("active phase cannot carry an outcome")
    elif phase == PHASE_TERMINAL:
        if normalized["last_outcome"] not in OUTCOMES:
            raise RuntimeRecordError("terminal phase requires an outcome")

    normalized["_updated_epoch"] = updated_epoch
    normalized["_last_progress_epoch"] = progress_epoch
    return normalized


def read_runtime(
    state_dir: str | os.PathLike[str],
    agent: str,
    *,
    now_epoch: float | None = None,
) -> dict:
    """Strict singleton read with bounded, non-sensitive failure codes."""
    try:
        path = runtime_path(state_dir, agent)
    except RuntimeRecordError:
        return {"status": STATUS_INVALID, "error": "unsafe_agent"}
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return {"status": STATUS_ABSENT, "error": "missing"}
    except OSError:
        return {"status": STATUS_INVALID, "error": "unreadable"}
    if size <= 0 or size > MAX_RECORD_BYTES:
        return {"status": STATUS_INVALID, "error": "size"}

    def _closed_object(pairs: list[tuple[str, object]]) -> dict:
        out: dict = {}
        for key, value in pairs:
            if key in out:
                raise RuntimeRecordError("duplicate JSON key")
            out[key] = value
        return out

    try:
        payload = path.read_bytes()
        if not payload or len(payload) > MAX_RECORD_BYTES:
            raise RuntimeRecordError("record size changed during read")
        if payload.startswith(b"\xef\xbb\xbf"):
            raise RuntimeRecordError("BOM is not allowed")
        raw = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_closed_object,
        )
        record = validate_record(
            raw,
            expected_agent=agent,
            now_epoch=now_epoch,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, RuntimeRecordError):
        return {"status": STATUS_INVALID, "error": "malformed"}

    now = time.time() if now_epoch is None else float(now_epoch)
    progress_epoch = record.pop("_last_progress_epoch")
    updated_epoch = record.pop("_updated_epoch")
    return {
        "status": STATUS_VALID,
        "record": record,
        "updated_age_seconds": max(0.0, now - updated_epoch),
        "progress_age_seconds": (
            None if progress_epoch is None else max(0.0, now - progress_epoch)
        ),
    }


def process_start_token(pid: int) -> str | None:
    """Use the store's OS-specific anti-PID-reuse token without duplicating it."""
    from agenttalk.store import _process_start_token

    return _process_start_token(pid)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    unsupported = {
        errno.EINVAL,
        getattr(errno, "ENOTSUP", errno.EINVAL),
        getattr(errno, "EOPNOTSUPP", errno.EINVAL),
    }
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno in unsupported:
            return
        raise
    try:
        os.fsync(fd)
    except OSError as exc:
        if exc.errno in unsupported:
            return
        raise
    finally:
        os.close(fd)


def _atomic_write(path: Path, record: dict) -> None:
    data = (
        json.dumps(record, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    if len(data) > MAX_RECORD_BYTES:
        raise RuntimeWriteError("wrapper runtime record exceeds its size bound")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeWriteError("cannot create wrapper runtime directory") from exc
    temporary = path.parent / (
        f".{path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp"
    )
    fd: int | None = None
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            fd = None
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except OSError as exc:
        raise RuntimeWriteError("cannot durably publish wrapper runtime record") from exc
    finally:
        if fd is not None:
            os.close(fd)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


class WrapperRuntimeWriter:
    """Single-process lifecycle writer with monotonic turn/progress generations."""

    _DEFAULT_PROGRESS_WRITE_INTERVAL_SECONDS = 5.0

    def __init__(
        self,
        state_dir: str | os.PathLike[str],
        agent: str,
        wrapper_generation: str,
        *,
        wrapper_pid: int | None = None,
        wrapper_start: str | None | object = _UNSET,
        clock: Callable[[], float] = time.time,
        progress_write_interval_seconds: float = (
            _DEFAULT_PROGRESS_WRITE_INTERVAL_SECONDS
        ),
    ) -> None:
        self.path = runtime_path(state_dir, agent)
        if (
            not isinstance(wrapper_generation, str)
            or _SAFE_GENERATION.fullmatch(wrapper_generation) is None
        ):
            raise RuntimeRecordError("wrapper_generation is invalid")
        self.agent = agent
        self.wrapper_generation = wrapper_generation
        self.wrapper_pid = os.getpid() if wrapper_pid is None else wrapper_pid
        if (
            not isinstance(self.wrapper_pid, int)
            or isinstance(self.wrapper_pid, bool)
            or self.wrapper_pid <= 0
        ):
            raise RuntimeRecordError("wrapper_pid must be positive")
        self.wrapper_start = (
            process_start_token(self.wrapper_pid)
            if wrapper_start is _UNSET
            else wrapper_start
        )
        if self.wrapper_start is not None:
            _safe_optional_text(self.wrapper_start, field="wrapper_start")
        if (
            not isinstance(progress_write_interval_seconds, (int, float))
            or isinstance(progress_write_interval_seconds, bool)
            or not math.isfinite(float(progress_write_interval_seconds))
            or float(progress_write_interval_seconds) < 0
        ):
            raise RuntimeRecordError(
                "progress_write_interval_seconds must be a finite number >= 0"
            )
        self._clock = clock
        self._progress_write_interval_seconds = float(
            progress_write_interval_seconds
        )
        self._lock = threading.Lock()
        self._turn_generation = 0
        self._progress_sequence = 0
        self._turn_id: str | None = None
        self._message_id: str | None = None
        self._launcher_pid: int | None = None
        self._launcher_start: str | None = None
        self._last_progress_at: str | None = None
        self._last_progress_write_epoch: float | None = None
        self._last_outcome: str | None = None
        self._phase = PHASE_IDLE

    def _record(self, at: float) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "agent": self.agent,
            "wrapper_pid": self.wrapper_pid,
            "wrapper_start": self.wrapper_start,
            "wrapper_generation": self.wrapper_generation,
            "phase": self._phase,
            "turn_generation": self._turn_generation,
            "turn_id": self._turn_id,
            "message_id": self._message_id,
            "cli_launcher_pid": self._launcher_pid,
            "cli_launcher_start": self._launcher_start,
            "progress_sequence": self._progress_sequence,
            "last_progress_at": self._last_progress_at,
            "last_outcome": self._last_outcome,
            "updated_at": _utc_iso(at),
        }

    def _write(self, at: float) -> dict:
        record = self._record(at)
        validate_record(record, expected_agent=self.agent, now_epoch=at)
        _atomic_write(self.path, record)
        return dict(record)

    def idle(self) -> dict:
        with self._lock:
            now = float(self._clock())
            self._phase = PHASE_IDLE
            self._turn_id = None
            self._message_id = None
            self._launcher_pid = None
            self._launcher_start = None
            return self._write(now)

    def starting(
        self,
        *,
        message_id: str | None,
        turn_id: str | None = None,
    ) -> dict:
        with self._lock:
            now = float(self._clock())
            self._turn_generation += 1
            self._turn_id = turn_id or f"turn-{uuid.uuid4().hex[:12]}"
            _safe_optional_text(self._turn_id, field="turn_id")
            self._message_id = _safe_optional_text(message_id, field="message_id")
            self._launcher_pid = None
            self._launcher_start = None
            self._last_progress_at = None
            self._last_progress_write_epoch = None
            self._last_outcome = None
            self._phase = PHASE_STARTING
            return self._write(now)

    def active(
        self,
        launcher_pid: int,
        launcher_start: str | None | object = _UNSET,
    ) -> dict:
        with self._lock:
            if self._phase != PHASE_STARTING:
                raise RuntimeRecordError("active transition requires starting phase")
            _positive_pid(launcher_pid, field="cli_launcher_pid")
            start = (
                process_start_token(launcher_pid)
                if launcher_start is _UNSET
                else launcher_start
            )
            if start is not None:
                _safe_optional_text(start, field="cli_launcher_start")
            self._launcher_pid = launcher_pid
            self._launcher_start = start
            self._phase = PHASE_ACTIVE
            return self._write(float(self._clock()))

    def progress(self) -> dict:
        with self._lock:
            if self._phase != PHASE_ACTIVE:
                raise RuntimeRecordError("progress transition requires active phase")
            now = float(self._clock())
            self._progress_sequence += 1
            self._last_progress_at = _utc_iso(now)
            elapsed = (
                None
                if self._last_progress_write_epoch is None
                else now - self._last_progress_write_epoch
            )
            if (
                elapsed is not None
                and 0 <= elapsed < self._progress_write_interval_seconds
            ):
                return self._record(now)
            record = self._write(now)
            self._last_progress_write_epoch = now
            return record

    def _terminal_locked(self, outcome: str) -> dict:
        if outcome not in OUTCOMES:
            raise RuntimeRecordError("terminal outcome is invalid")
        if self._phase not in {PHASE_STARTING, PHASE_ACTIVE, PHASE_TERMINAL}:
            raise RuntimeRecordError("terminal transition requires a live turn")
        now = float(self._clock())
        self._last_outcome = outcome
        self._phase = PHASE_TERMINAL
        return self._write(now)

    def terminal(self, outcome: str) -> dict:
        with self._lock:
            return self._terminal_locked(outcome)

    def dead_letter(self, *, message_id: str | None = None) -> dict:
        """Publish disposal even when this process did not drive the failed turn.

        Crash reconciliation can reach a durable attempt ceiling before the new
        wrapper launches a child.  That disposition is still a terminal
        wrapper-turn lifecycle event, so create a bounded synthetic turn rather
        than requiring a preceding ``starting`` transition.
        """
        with self._lock:
            if self._phase == PHASE_IDLE:
                self._turn_generation += 1
                self._turn_id = f"disposition-{uuid.uuid4().hex[:12]}"
                self._message_id = _safe_optional_text(
                    message_id, field="message_id"
                )
                self._launcher_pid = None
                self._launcher_start = None
                self._last_progress_at = None
                self._last_outcome = None
                self._phase = PHASE_STARTING
            return self._terminal_locked(OUTCOME_DEAD_LETTER)
