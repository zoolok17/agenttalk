"""An opt-in journal of what each agent's turns did.

In plain words: when switched on, a wrapper writes one small append-only file
set per agent that says, in order, "a turn was dispatched", "a model process
was launched", "the turn ended (and how many tokens it used)" and "a message
was finally dealt with". It is meant for anything that wants to count turns,
runs or token usage afterwards. It is off unless someone turns it on.

The one promise that shapes the design: **a turn never waits for the journal.**
The turn path only drops a small note into a bounded in-memory queue
(``TurnEventSink.emit``). A separate writer thread does every file, status and
logging operation. If the queue is full, the disk is full or the disk hangs,
notes are dropped and counted; the turn does not notice.

Because notes can be lost, the journal says so honestly instead of pretending
to be complete:

* every event carries ``seq``, a number assigned when it is queued, so a lost
  event leaves a visible gap; numbers are never reused or reordered;
* ``dropped_total`` counts what was lost before an event;
* ``stream_started`` (the first line of every segment file) and
  ``stream_closed`` (written on a clean close) carry no ``seq``; the closing
  record says how far the numbering went, so a missing tail shows;
* ``streams.jsonl`` lists every stream ever started, so a stream whose files
  were deleted can still be seen.

Files, per agent, under :func:`default_turn_events_root`::

    <agent>/streams.jsonl                one line per stream, never rotated
    <agent>/<generation>-<n>.jsonl       the segments of one stream
    <agent>/status-<generation>.json     the writer's own status, replaced atomically

``generation`` names one writer lifetime; the stream is ``<agent>.<generation>``.
Nothing here ever holds prompt or reply text, file paths, command lines,
environment values, secrets, keys, model names or exception text; every field
is a number, a flag, a closed word, an id already visible on the bus, or a
time.
"""

from __future__ import annotations

import contextlib
import hashlib
import datetime as _dt
import errno
import json
import os
import queue
import re
import secrets
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

ENV_TURN_EVENTS_DIR = "AGENTTALK_TURN_EVENTS_DIR"

#: How many events may wait for the writer before new ones are dropped.
TURN_EVENTS_QUEUE_MAX = 1024
#: A segment file is closed and a new one opened at about this size.
TURN_EVENTS_SEGMENT_BYTES = 1024 * 1024
#: The files of one agent may total this much; the oldest segments go first.
TURN_EVENTS_MAX_BYTES = 64 * 1024 * 1024
#: At start-up, the longest the caller waits for the stream to be registered.
TURN_EVENTS_START_SECONDS = 2.0
#: The whole budget for ``close``: drain, closing record and join.
TURN_EVENTS_CLOSE_SECONDS = 2.0
#: Status is rewritten at most this often while events flow.
TURN_EVENTS_STATUS_SECONDS = 5.0
#: Written events are forced to disk at least this often (and at every disposition).
TURN_EVENTS_SYNC_SECONDS = 2.0
MAX_LINE_BYTES = 4096
INT64_MAX = 2**63 - 1

KIND_STREAM_STARTED = "stream_started"
KIND_STREAM_CLOSED = "stream_closed"
KIND_DISPATCH_STARTED = "dispatch_started"
KIND_DISPATCH_ENDED = "dispatch_ended"
KIND_MESSAGE_DISPOSED = "message_disposed"
EVENT_KINDS = (KIND_DISPATCH_STARTED, KIND_DISPATCH_ENDED, KIND_MESSAGE_DISPOSED)

DISPOSITIONS = ("completed", "dead_lettered", "delivery_failed", "outcome_unknown")
OUTCOMES = ("success", "failed", "not_launched")
EXITS = ("normal", "spawn_error", "exception")
CLIS = ("claude", "codex")
SESSIONS = ("fresh", "resume")
FAILURE_CLASSES = (
    "poison_eligible",
    "known_global_infra",
    "ambiguous_or_unknown",
    "config_blocked",
    "infra_retry_exhausted",
    "gateway_held",
    "other",
)
USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
#: The gate's raw terminal facts, each true, false or null (null: no such fact on that path).
RAW_FACT_FIELDS = ("consumed", "landed", "compliance_success", "dead_lettered", "terminal_failure")

#: Why observation is off, as closed words. They are also what the status record says.
OFF_START_FAILED = "start_failed"
OFF_START_TIMEOUT = "start_timeout"
OFF_CLOSE_TIMEOUT = "close_timeout"
OFF_WRITER_ERROR = "writer_error"
OFF_REASONS = (OFF_START_FAILED, OFF_START_TIMEOUT, OFF_CLOSE_TIMEOUT, OFF_WRITER_ERROR)

#: Fault words, counted in the status record. Never free text.
FAULTS = ("write_failed", "disk_full", "open_failed", "status_failed", "remove_failed", "registration_failed")

_COMMON = frozenset({"v", "kind", "event_id", "stream", "at", "agent", "dropped_total"})
_SHAPES: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    KIND_STREAM_STARTED: (
        frozenset({"agent_version", "previous_stream", "mode", "unmanaged", "segment", "after_seq"}),
        frozenset(),
    ),
    KIND_STREAM_CLOSED: (frozenset({"last_seq"}), frozenset()),
    KIND_DISPATCH_STARTED: (
        frozenset({"seq", "message_id", "turn_id", "cli", "cli_session", "message_at"}),
        frozenset({"attempts_recorded"}),
    ),
    KIND_DISPATCH_ENDED: (
        frozenset({"seq", "message_id", "turn_id", "launched", "outcome", "exit", "duration_ms", "usage"}),
        frozenset({"failure_class"}),
    ),
    KIND_MESSAGE_DISPOSED: (
        frozenset({"seq", "message_id", "disposition", "message_at", *RAW_FACT_FIELDS}),
        frozenset({"attempts_recorded"}),
    ),
}
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,39}$")
_SENTINEL = object()
#: The keys a caller may pass to ``emit`` for each kind. Everything else (``seq``,
#: ``stream``, ``agent``, ``event_id``, ``v``, ``at``, ``dropped_total``) belongs to the writer.
_EMIT_KEYS: dict[str, frozenset[str]] = {
    kind: (_SHAPES[kind][0] | _SHAPES[kind][1]) - _COMMON - {"seq"} for kind in EVENT_KINDS
}


class _Cancelled(BaseException):  # noqa: N818 - control flow inside the writer, never leaves it
    """The run was cancelled (a deadline passed): nothing new may start.

    A BaseException on purpose: the broad ``except Exception`` blocks around file work
    must not turn a cancellation into a counted fault."""


class TurnEventError(ValueError):
    """A journal record that is not valid. The message never carries its content."""


class UnsupportedSchemaVersion(TurnEventError):
    """A record written under another schema version."""


def _bad() -> None:
    raise TurnEventError("the turn-event record is invalid")


def _is_int(value: object, low: int = 0) -> bool:
    return type(value) is int and low <= value <= INT64_MAX


def _name_ok(value: object) -> bool:
    return isinstance(value, str) and _NAME.match(value) is not None


def format_time(ms: int) -> str:
    moment = _dt.datetime.fromtimestamp(ms / 1000, tz=_dt.timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (ms % 1000)


def parse_time(text: object) -> int | None:
    """An ISO-8601 time with a zone, as epoch milliseconds, or None."""
    if not isinstance(text, str) or not text or len(text) > 40:
        return None
    try:
        parsed = _dt.datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    delta = parsed - _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)
    return (delta.days * 86_400 + delta.seconds) * 1000 + delta.microseconds // 1000


# --- the closed schema ---------------------------------------------------


def validate_event(obj: object) -> dict[str, Any]:
    """Check one record against the closed schema; return it unchanged.

    Raises :class:`UnsupportedSchemaVersion` for another version and
    :class:`TurnEventError` for anything else that does not fit: a missing or
    extra key, a wrong type or an unknown word.
    """
    if not isinstance(obj, dict):
        _bad()
    if "v" in obj and (type(obj["v"]) is not int or obj["v"] != SCHEMA_VERSION):
        raise UnsupportedSchemaVersion("the turn-event schema version is not supported")
    kind = obj.get("kind")
    # A kind that is not even text (a list, an object) is a damaged record, not a lookup error.
    if not isinstance(kind, str) or kind not in _SHAPES:
        _bad()
    required, optional = _SHAPES[kind]
    keys = set(obj)
    if not (_COMMON | required) <= keys or keys - _COMMON - required - optional:
        _bad()
    try:
        uuid.UUID(obj["event_id"])
    except (ValueError, AttributeError, TypeError):
        _bad()
    if not _name_ok(obj["stream"]) or not _name_ok(obj["agent"]):
        _bad()
    if not obj["stream"].startswith(obj["agent"] + "."):
        _bad()
    if not _is_int(obj["dropped_total"]) or parse_time(obj["at"]) is None:
        _bad()
    if "seq" in required and not _is_int(obj["seq"], 1):
        _bad()
    if "attempts_recorded" in obj and not _is_int(obj["attempts_recorded"]):
        _bad()
    if kind == KIND_STREAM_STARTED:
        _check_stream_started(obj)
    elif kind == KIND_STREAM_CLOSED:
        if not _is_int(obj["last_seq"]):
            _bad()
    else:
        if not _name_ok(obj["message_id"]):
            _bad()
        if kind != KIND_DISPATCH_ENDED:
            at = obj["message_at"]
            if at is not None and (not isinstance(at, str) or len(at) > 40 or not at.isascii()):
                _bad()
        if kind == KIND_DISPATCH_STARTED:
            if not (_name_ok(obj["turn_id"]) and obj["cli"] in CLIS and obj["cli_session"] in SESSIONS):
                _bad()
        elif kind == KIND_DISPATCH_ENDED:
            _check_dispatch_ended(obj)
        else:
            if obj["disposition"] not in DISPOSITIONS:
                _bad()
            for name in RAW_FACT_FIELDS:
                if obj[name] is not None and type(obj[name]) is not bool:
                    _bad()
    return obj


def _check_stream_started(obj: dict[str, Any]) -> None:
    if not _is_int(obj["segment"], 1) or not _is_int(obj["after_seq"]):
        _bad()
    previous = obj["previous_stream"]
    if previous is not None and not _name_ok(previous):
        _bad()
    if obj["mode"] != "loop" or not isinstance(obj["unmanaged"], list):
        _bad()
    if any(item != "cadence" for item in obj["unmanaged"]):
        _bad()
    if not isinstance(obj["agent_version"], str) or len(obj["agent_version"]) > 40:
        _bad()


def _check_dispatch_ended(obj: dict[str, Any]) -> None:
    if not (
        _name_ok(obj["turn_id"])
        and type(obj["launched"]) is bool
        and obj["outcome"] in OUTCOMES
        and obj["exit"] in EXITS
        and _is_int(obj["duration_ms"])
    ):
        _bad()
    if obj.get("failure_class") not in (None, *FAILURE_CLASSES):
        _bad()
    # A turn that never launched did not succeed; one that succeeded did launch.
    if (obj["outcome"] == "not_launched") == obj["launched"]:
        _bad()
    usage = obj["usage"]
    if usage is not None:
        if not isinstance(usage, dict) or set(usage) != set(USAGE_KEYS):
            _bad()
        if any(value is not None and not _is_int(value) for value in usage.values()):
            _bad()


def encode_line(obj: Mapping[str, Any]) -> bytes:
    """One record as one newline-terminated line (sorted keys, ASCII, bounded)."""
    data = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii") + b"\n"
    if len(data) > MAX_LINE_BYTES:
        _bad()
    return data


def parse_line(raw: bytes) -> dict[str, Any]:
    """One journal line (with or without its newline) -> a checked record."""
    raw = raw.rstrip(b"\n")
    if len(raw) > MAX_LINE_BYTES:
        _bad()
    try:
        obj = json.loads(raw.decode("ascii"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        _bad()
    return validate_event(obj)


# --- where the files live --------------------------------------------------


def default_turn_events_root(
    project_root: str | os.PathLike[str],
    *,
    platform: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """The per-user, per-project folder of the journal.

    It sits beside the wrapper logs and reuses their project naming, so a path
    never appears in a name: ``.../agenttalk/turn-events/<project id>``.
    ``AGENTTALK_TURN_EVENTS_DIR`` (an absolute path) replaces the whole folder.
    """
    from agenttalk.wrapper_logs import default_wrapper_log_root

    env = os.environ if environ is None else environ
    override = env.get(ENV_TURN_EVENTS_DIR)
    if override:
        candidate = Path(override).expanduser()
        if candidate.is_absolute():
            return candidate
    logs = default_wrapper_log_root(project_root, platform=platform, environ=environ)
    return logs.parent.parent / "turn-events" / logs.name


# --- reading (for tests and for any consumer) ------------------------------


class SegmentRead:
    """What :func:`read_segment` found."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        self.end_offset = 0  # just after the last complete line
        self.torn = False  # bytes follow the last newline (a half-written line)
        self.damaged = 0  # complete lines that did not fit the schema


def read_segment(path: str | os.PathLike[str], offset: int = 0) -> SegmentRead:
    """Read complete lines from ``offset``; an unknown version raises."""
    result = SegmentRead()
    result.end_offset = offset
    with open(path, "rb") as handle:
        handle.seek(offset)
        data = handle.read()
    cut = data.rfind(b"\n")
    if cut < 0:
        result.torn = bool(data)
        return result
    result.end_offset = offset + cut + 1
    result.torn = len(data) > cut + 1
    for line in data[:cut].split(b"\n"):
        try:
            result.records.append(parse_line(line))
        except UnsupportedSchemaVersion:
            raise
        except Exception:  # any unreadable line is damage, never a stall
            result.damaged += 1
    return result


def read_streams_checked(agent_dir: str | os.PathLike[str]) -> tuple[list[dict[str, Any]], int]:
    """The agent's ``streams.jsonl``: well-formed records and a count of damaged lines.

    A last line without its newline (a torn write) is damage too: it counts, it is
    skipped, and a later registration starts on its own line after it."""
    try:
        with open(Path(agent_dir) / "streams.jsonl", "rb") as handle:
            data = handle.read()
    except FileNotFoundError:
        return [], 0
    records: list[dict[str, Any]] = []
    damaged = 0
    lines = data.split(b"\n")
    torn = lines.pop()  # what follows the last newline (empty when the file ends cleanly)
    if torn:
        damaged += 1
    for line in lines:
        if not line:
            continue
        try:
            obj = json.loads(line.decode("ascii"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            damaged += 1
            continue
        if (
            isinstance(obj, dict)
            and set(obj) == {"stream", "previous_stream", "started_at"}
            and _name_ok(obj["stream"])
            and parse_time(obj["started_at"]) is not None
        ):
            records.append(obj)
        else:
            damaged += 1
    return records, damaged


def read_streams(agent_dir: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """The agent's ``streams.jsonl``: one dict per well-formed complete line."""
    return read_streams_checked(agent_dir)[0]


def list_segments(agent_dir: str | os.PathLike[str]) -> list[tuple[str, int, Path]]:
    """``(generation, segment number, path)`` for every segment file, in order."""
    found = []
    try:
        names = os.listdir(agent_dir)
    except FileNotFoundError:
        return []
    for name in names:
        if not name.endswith(".jsonl") or name == "streams.jsonl":
            continue
        generation, _dash, number = name[: -len(".jsonl")].rpartition("-")
        if generation and number.isdigit() and int(number) >= 1:
            found.append((generation, int(number), Path(agent_dir) / name))
    return sorted(found)


def iter_records(
    agent_dir: str | os.PathLike[str],
    cursor: dict[tuple[str, int], int] | None = None,
) -> Iterator[tuple[str, int, dict[str, Any]]]:
    """Yield ``(generation, segment, record)`` for every complete record after ``cursor``.

    ``cursor`` maps ``(generation, segment)`` to a byte offset and is updated
    in place to the new end of each segment read, so calling again continues
    where the last call stopped. A half-written last line is left for later.
    """
    cursor = {} if cursor is None else cursor
    for generation, number, path in list_segments(agent_dir):
        try:
            read = read_segment(path, cursor.get((generation, number), 0))
        except FileNotFoundError:
            continue
        cursor[(generation, number)] = read.end_offset
        for record in read.records:
            yield generation, number, record


# --- the files seam (tests replace it to block or fail operations) ----------


class JournalFiles:
    """Every file operation the writer thread performs, in one place.

    The writer calls nothing else, so a test can subclass this to block, fail or
    count an operation. Nothing here is used on the turn path.
    """

    def makedirs(self, path: str) -> None:
        os.makedirs(path, exist_ok=True)

    def open_append(self, path: str) -> Any:
        return open(path, "ab")

    def write(self, handle: Any, data: bytes) -> None:
        handle.write(data)
        handle.flush()

    def sync(self, handle: Any) -> None:
        handle.flush()
        os.fsync(handle.fileno())

    def close(self, handle: Any) -> None:
        handle.close()

    def append_line(self, path: str, data: bytes) -> None:
        """Append to a small shared file and force it to disk."""
        with open(path, "ab") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())

    def read_bytes(self, path: str) -> bytes:
        with open(path, "rb") as handle:
            return handle.read()

    def write_atomic(self, path: str, data: bytes) -> None:
        temp = path + ".tmp"
        with open(temp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)

    def listdir(self, path: str) -> list[str]:
        return os.listdir(path)

    def stat(self, path: str) -> tuple[int, int]:
        info = os.stat(path)
        return info.st_size, info.st_mtime_ns

    def remove(self, path: str) -> None:
        os.remove(path)


class _GatedFiles:
    """The only way the writer thread reaches a ``JournalFiles``.

    Every helper that STARTS I/O first asks the sink whether cancellation is latched
    (a timeout, a close deadline, a writer error) and, if so, raises ``_Cancelled``
    without calling the seam: once cancelled, the writer begins no new step. The gate
    checks each helper ONCE, at its entry, so a step already begun (an append, the atomic
    status write, a sync, a registration read with its read-only process-identity lookup)
    may finish on the writer's own thread; no caller ever waits for it. ``close`` only lets
    go of a handle and is never refused. The one exception is the status note after a start
    that failed or timed out, which the sink allows only until close's own deadline has
    passed: after close times out, no new startup status write begins; a status write
    already started may finish on the writer's own thread (see ``_status_permitted``).
    """

    def __init__(self, files: JournalFiles, sink: "TurnEventSink") -> None:
        self._files = files
        self._sink = sink

    def _go(self) -> None:
        if not self._sink._may_write():  # noqa: SLF001 - the gate and the sink are one unit
            raise _Cancelled

    def makedirs(self, path: str) -> None:
        self._go()
        self._files.makedirs(path)

    def open_append(self, path: str) -> Any:
        self._go()
        return self._files.open_append(path)

    def write(self, handle: Any, data: bytes) -> None:
        self._go()
        self._files.write(handle, data)

    def sync(self, handle: Any) -> None:
        self._go()
        self._files.sync(handle)

    def close(self, handle: Any) -> None:
        self._files.close(handle)

    def append_line(self, path: str, data: bytes) -> None:
        self._go()
        self._files.append_line(path, data)

    def read_bytes(self, path: str) -> bytes:
        self._go()
        return self._files.read_bytes(path)

    def write_atomic(self, path: str, data: bytes) -> None:
        if not self._sink._status_permitted():  # noqa: SLF001
            raise _Cancelled
        self._files.write_atomic(path, data)

    def listdir(self, path: str) -> list[str]:
        self._go()
        return self._files.listdir(path)

    def stat(self, path: str) -> tuple[int, int]:
        self._go()
        return self._files.stat(path)

    def remove(self, path: str) -> None:
        self._go()
        self._files.remove(path)


# --- the sink ----------------------------------------------------------------


def _snapshot(kind: object, fields: object) -> dict[str, Any] | None:
    """A copy of the caller's fields in the one shape ``emit`` accepts, or None.

    None means "refuse this event": an unknown kind, a key the writer owns or that the
    kind does not have, or a value that is not text, a whole number, a flag or null
    (and, for ``usage``, anything but exactly its four counts). Nothing is deep-copied."""
    allowed = _EMIT_KEYS.get(kind) if type(kind) is str else None
    if allowed is None or type(fields) is not dict or not fields.keys() <= allowed:
        return None
    out: dict[str, Any] = {}
    for key, value in fields.items():
        if key == "usage" and value is not None:
            if type(value) is not dict or set(value) != set(USAGE_KEYS):
                return None
            counts: dict[str, Any] = {}
            for name in USAGE_KEYS:
                count = value[name]
                if count is not None and type(count) is not int:
                    return None
                counts[name] = count
            out[key] = counts
        elif type(value) in (str, int, bool, type(None)):
            out[key] = value
        else:
            return None
    return out


class TurnEventSink:
    """Accepts events without ever making the caller wait, and journals them.

    Usage: ``sink = TurnEventSink(directory, agent); sink.start()`` once before
    the first message, ``sink.emit(kind, **fields)`` from the turn path, and
    ``sink.close()`` on the way out. ``start`` may return False (the journal is
    off for this run); everything else is then a no-op. Nothing here raises
    into the caller.
    """

    def __init__(
        self,
        directory: str | os.PathLike[str],
        agent: str,
        *,
        agent_version: str = "unknown",
        mode: str = "loop",
        unmanaged: tuple[str, ...] = (),
        queue_max: int = TURN_EVENTS_QUEUE_MAX,
        segment_bytes: int = TURN_EVENTS_SEGMENT_BYTES,
        max_bytes: int = TURN_EVENTS_MAX_BYTES,
        start_seconds: float = TURN_EVENTS_START_SECONDS,
        close_seconds: float = TURN_EVENTS_CLOSE_SECONDS,
        status_seconds: float = TURN_EVENTS_STATUS_SECONDS,
        sync_seconds: float = TURN_EVENTS_SYNC_SECONDS,
        files: JournalFiles | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.agent = agent
        self.directory = str(directory)
        self._agent_dir = os.path.join(self.directory, agent)
        self.generation = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-%d-%s" % (
            os.getpid(),
            secrets.token_hex(2),
        )
        self.stream = agent + "." + self.generation
        # Start-up metadata is checked BEFORE any serialization: a value that fails
        # never reaches a file, a status record or a log; it is replaced by a closed word.
        try:
            unmanaged_ok = isinstance(unmanaged, (tuple, list)) and all(item == "cadence" for item in unmanaged)
        except Exception:  # noqa: BLE001
            unmanaged_ok = False
        self._startup_ok = bool(
            mode == "loop"
            and unmanaged_ok
            and isinstance(agent_version, str)
            and _VERSION.match(agent_version) is not None
        )
        self._agent_version = agent_version if self._startup_ok else "unknown"
        self._mode = "loop" if self._startup_ok else "invalid"
        self._unmanaged = ["cadence" for _ in unmanaged] if self._startup_ok else []
        self._segment_bytes = max(256, int(segment_bytes))
        self._max_bytes = int(max_bytes)
        self._start_seconds = start_seconds
        self._close_seconds = close_seconds
        self._status_seconds = status_seconds
        self._sync_seconds = sync_seconds
        self._files = _GatedFiles(files or JournalFiles(), self)
        self._clock = clock
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=max(1, int(queue_max)))
        # Guards only the two integers below; no I/O is ever done while held.
        self._lock = threading.Lock()
        self._seq = 0
        self._queue_full = 0
        self._state = "new"  # new | starting | on | off | closed
        self._off_reason: str | None = None
        self._ack = threading.Event()
        self._cancel = threading.Event()
        self._closing = False
        self._deadline: float | None = None
        self._thread: threading.Thread | None = None
        # Writer-thread state; read by the turn path never.
        self._processed_seq = 0
        self._written_seq = 0
        self._dropped = 0
        self._written = 0
        self._write_failures = 0
        self._sync_failures = 0
        self._invalid = 0
        self._segments_opened = 0
        self._segments_removed = 0
        self._faults: dict[str, int] = {}
        self._last_fault: str | None = None
        self._handle: Any = None
        self._segment_no = 0
        self._segment_path = ""
        self._segment_size = 0
        self._header_size = 0
        self._unsynced = False
        self._last_sync = 0.0
        self._last_status = 0.0
        self._started_at = 0
        self._token: str | None = None
        self._previous: str | None = None
        self._closed_cleanly = False
        self._status_path = os.path.join(self._agent_dir, "status-%s.json" % self.generation)

    # -- the caller's side ---------------------------------------------------

    @property
    def state(self) -> str:
        return self._state

    @property
    def off_reason(self) -> str | None:
        return self._off_reason

    @property
    def status_path(self) -> str:
        return self._status_path

    def start(self) -> bool:
        """Register the stream; True when the journal is on for this run.

        The caller waits at most ``start_seconds``, once, before its first
        message. On failure or timeout observation is switched off for good
        (a late success never turns it back on) and the reason is a closed
        word in :attr:`off_reason` and in the status record. After a timeout the
        writer begins no new step; a step already begun (an append, a registration
        read with its process-identity lookup) may finish on the writer's own
        thread, and the caller never waits for it.
        """
        try:
            with self._lock:
                if self._state != "new":
                    return self._state == "on"
                self._state = "starting"
            if not _name_ok(self.agent) or not self._startup_ok:
                self._latch(OFF_START_FAILED)
                return False
            try:
                thread = threading.Thread(target=self._run, name="agenttalk-turn-events", daemon=True)
                self._thread = thread
                thread.start()
            except Exception:  # failing to start must never reach the wrapper
                self._thread = None
                self._latch(OFF_START_FAILED)
                return False
            if not self._ack.wait(self._start_seconds):
                self._latch(OFF_START_TIMEOUT)
                return False
            return self._state == "on"
        except Exception:
            self._latch(OFF_START_FAILED)
            return False

    def emit(self, kind: str, **fields: Any) -> None:
        """Queue one event. Never waits, never does I/O, never raises."""
        try:
            if self._state != "on" or self._closing:
                return
            with self._lock:
                self._seq += 1
                seq = self._seq
            try:
                # A small, known shape is copied here (scalars, and usage's four counts), so
                # nothing the caller keeps changing can alter an event waiting in the queue.
                snapshot = _snapshot(kind, fields)
                self._queue.put_nowait((seq, int(self._clock() * 1000), kind, snapshot))
            except queue.Full:
                with self._lock:
                    self._queue_full += 1
        except Exception:  # noqa: S110 - the journal must never disturb a turn
            pass

    def close(self, timeout: float | None = None) -> None:
        """Stop within one deadline: drain, write the closing record, join.

        If the deadline passes first the writer begins no new step (the stream then counts
        as unclean to a reader; a step already begun may finish on the writer's own thread)
        and ``close`` returns anyway. Never raises.
        """
        try:
            with self._lock:
                if self._closing or self._state == "closed":
                    return
                self._closing = True
            seconds = self._close_seconds if timeout is None else timeout
            self._deadline = time.monotonic() + seconds
            thread = self._thread
            if thread is None:
                with self._lock:
                    if self._state != "off":
                        self._state = "closed"
                return
            with contextlib.suppress(queue.Full):
                self._queue.put_nowait(_SENTINEL)
            thread.join(max(0.0, self._deadline - time.monotonic()))
            if thread.is_alive():
                self._latch(OFF_CLOSE_TIMEOUT)
            else:
                with self._lock:
                    if self._state == "on":
                        self._state = "closed"
        except Exception:  # noqa: S110
            pass

    def _latch(self, reason: str) -> None:
        """Switch observation off for the rest of this run. The first reason stays."""
        with self._lock:
            if self._state != "off":
                self._state = "off"
                self._off_reason = reason
        self._cancel.set()

    # -- the writer thread ----------------------------------------------------

    def _may_write(self) -> bool:
        if self._cancel.is_set():
            return False
        return self._deadline is None or time.monotonic() < self._deadline

    def _status_permitted(self) -> bool:
        """May a status record be written now? Normally only while writing is allowed. After
        a start that failed or timed out it still may (that record is how `status` learns why
        the journal is off) but only until close's deadline has passed: close overrides that
        exception."""
        if self._may_write():
            return True
        if self._off_reason not in (OFF_START_TIMEOUT, OFF_START_FAILED):
            return False
        return self._deadline is None or time.monotonic() < self._deadline

    def _check(self) -> None:
        """Called after every operation that can block, before the next step: once cancelled
        or past the deadline, the writer begins no new step. A step that was already begun may
        have finished on this thread; that is all, and no caller waits for it."""
        if not self._may_write():
            raise _Cancelled

    def _fault(self, word: str) -> None:
        self._faults[word] = self._faults.get(word, 0) + 1
        self._last_fault = word
        # Counted for the status record only: the journal never logs through the wrapper's
        # own logging, whose streams the turn path needs.

    def _fault_for(self, exc: BaseException, default: str) -> str:
        return "disk_full" if getattr(exc, "errno", None) == errno.ENOSPC else default

    def _run(self) -> None:
        try:
            if not self._register():
                return
            self._loop()
        except _Cancelled:
            pass  # a deadline passed: nothing new starts, and that is not a fault
        except Exception:  # nothing may escape the thread
            self._latch(OFF_WRITER_ERROR)
        finally:
            self._finish()

    def _register(self) -> bool:
        """Create the folder, list the stream, open segment 1. Sets the ack."""
        try:
            self._check()
            self._files.makedirs(self._agent_dir)
            self._check()
            self._previous = self._previous_stream()
            self._started_at = int(self._clock() * 1000)
            self._token = _start_token()
            self._check()
            record = {
                "stream": self.stream,
                "previous_stream": self._previous,
                "started_at": format_time(self._started_at),
            }
            line = encode_line(record)
            registry = os.path.join(self._agent_dir, "streams.jsonl")
            # A torn last line (a crash mid-write) must not swallow this record: start a new line.
            self._files.append_line(registry, (b"\n" if self._registry_is_torn(registry) else b"") + line)
            self._check()
            if not self._registered(registry):
                raise OSError("the stream record cannot be read back")
            self._check()
            self._open_segment()
        except _Cancelled:
            return False  # cancelled (a start timeout, usually): the status in _finish says why
        except Exception:
            self._fault("registration_failed")
            if not self._cancel.is_set():
                self._latch(OFF_START_FAILED)
                self._write_status()  # so `status` can already say why the journal is off
            self._ack.set()
            return False
        with self._lock:
            if self._state == "starting":
                self._state = "on"
        # The caller is let go first: a slow status write must never hold up the start.
        # The first status record follows at once (and a late success after a timeout
        # writes nothing: the state is already off).
        self._ack.set()
        if self._may_write():
            self._write_status()
            self._prune_old_status()
        return self._state == "on"

    def _registry_is_torn(self, registry: str) -> bool:
        try:
            data = self._files.read_bytes(registry)
        except FileNotFoundError:
            return False
        return bool(data) and not data.endswith(b"\n")

    def _registered(self, registry: str) -> bool:
        """True when the newest complete line of the registry is this stream's record."""
        try:
            data = self._files.read_bytes(registry)
        except OSError:
            return False
        if not data.endswith(b"\n"):
            return False
        try:
            last = json.loads(data[:-1].rsplit(b"\n", 1)[-1].decode("ascii"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            return False
        return isinstance(last, dict) and last.get("stream") == self.stream

    def _previous_stream(self) -> str | None:
        try:
            data = self._files.read_bytes(os.path.join(self._agent_dir, "streams.jsonl"))
        except FileNotFoundError:
            return None
        cut = data.rfind(b"\n")
        for line in reversed(data[: cut + 1].split(b"\n") if cut >= 0 else []):
            try:
                obj = json.loads(line.decode("ascii"))
            except (UnicodeDecodeError, ValueError):
                continue
            if isinstance(obj, dict) and _name_ok(obj.get("stream")):
                return obj["stream"]
        return None

    def _loop(self) -> None:
        while True:
            if not self._may_write():
                return
            try:
                item = self._queue.get(timeout=min(self._status_seconds, 0.25))
            except queue.Empty:
                self._periodic()
                if self._closing and self._queue.empty():
                    break
                continue
            if item is _SENTINEL:
                break
            self._process(item)
            self._periodic()
            if self._closing and self._queue.empty():
                break
        self._close_stream()

    def _periodic(self) -> None:
        now = time.monotonic()
        if self._unsynced and now - self._last_sync >= self._sync_seconds and self._may_write():
            self._sync_now()
        if now - self._last_status >= self._status_seconds and self._may_write():
            self._write_status()

    def _sync_now(self) -> None:
        if self._handle is None:
            return
        try:
            self._files.sync(self._handle)
            self._unsynced = False
            self._last_sync = time.monotonic()
        except Exception as exc:
            self._fault(self._fault_for(exc, "write_failed"))
            self._sync_failures += 1
            self._abandon_segment()

    def _process(self, item: tuple[int, int, str, dict[str, Any] | None]) -> None:
        seq, at_ms, kind, fields = item
        # Numbers skipped between two queued events were dropped by the full queue.
        if seq > self._processed_seq + 1:
            self._dropped += seq - 1 - self._processed_seq
        self._processed_seq = seq
        obj: dict[str, Any] = {
            "v": SCHEMA_VERSION,
            "kind": kind,
            "event_id": str(uuid.uuid4()),
            "stream": self.stream,
            "at": format_time(at_ms),
            "agent": self.agent,
            "dropped_total": self._dropped,
            "seq": seq,
        }
        try:
            if fields is None or kind not in EVENT_KINDS:
                _bad()
            obj.update(fields)  # only keys the writer does not own (see _snapshot)
            validate_event(obj)
            data = encode_line(obj)
        except Exception:  # a bad event is dropped and counted, never raised
            self._invalid += 1
            self._dropped += 1
            return
        if not self._may_write():
            return
        try:
            self._write_event(data, seq, kind)
        except _Cancelled:
            return  # a deadline passed: nothing more is written, and this is not a fault
        except Exception as exc:
            self._fault(self._fault_for(exc, "write_failed"))
            self._write_failures += 1
            self._dropped += 1
            self._abandon_segment()

    def _write_event(self, data: bytes, seq: int, kind: str) -> None:
        if self._handle is not None and self._segment_size > self._header_size:
            if self._segment_size + len(data) > self._segment_bytes:
                self._rotate()
        self._check()
        if self._handle is None:
            self._open_segment()
        self._check()
        self._files.write(self._handle, data)
        self._segment_size += len(data)
        self._written_seq = seq
        self._written += 1
        self._unsynced = True
        if kind == KIND_MESSAGE_DISPOSED:
            self._sync_now()

    def _rotate(self) -> None:
        """Close the full segment. If its final sync fails that is a counted fault and the
        segment's durability stays unproven: nothing retries it (the next segment starts
        clean). Its readable events are not counted as lost; "complete" only ever means
        "no loss the journal can see". `status` keeps the fault and its count."""
        handle, self._handle = self._handle, None
        try:
            self._files.sync(handle)
        except Exception as exc:
            self._fault(self._fault_for(exc, "write_failed"))
            self._sync_failures += 1
        finally:
            with contextlib.suppress(Exception):
                self._files.close(handle)

    def _abandon_segment(self) -> None:
        """After a failed write: leave the file as it is, start a new one next time."""
        handle, self._handle = self._handle, None
        if handle is not None:
            with contextlib.suppress(Exception):
                self._files.close(handle)

    def _open_segment(self) -> None:
        """Open the next segment and write its header (no ``seq`` of its own)."""
        self._segment_no += 1
        path = os.path.join(self._agent_dir, "%s-%d.jsonl" % (self.generation, self._segment_no))
        handle = None
        try:
            handle = self._files.open_append(path)
            self._check()  # the open may have taken a long time: nothing new after a deadline
            header = {
                "v": SCHEMA_VERSION,
                "kind": KIND_STREAM_STARTED,
                "event_id": str(uuid.uuid4()),
                "stream": self.stream,
                "at": format_time(int(self._clock() * 1000)),
                "agent": self.agent,
                "dropped_total": self._dropped,
                "agent_version": self._agent_version,
                "previous_stream": self._previous,
                "mode": self._mode,
                "unmanaged": self._unmanaged,
                "segment": self._segment_no,
                "after_seq": self._written_seq,
            }
            data = encode_line(validate_event(header))
            self._files.write(handle, data)
            self._check()
            self._files.sync(handle)
        except _Cancelled:
            if handle is not None:
                with contextlib.suppress(Exception):
                    self._files.close(handle)
            raise
        except Exception as exc:
            if handle is not None:
                with contextlib.suppress(Exception):
                    self._files.close(handle)
            self._fault(self._fault_for(exc, "open_failed"))
            raise
        self._handle = handle
        self._segment_path = path
        self._segment_size = self._header_size = len(data)
        self._segments_opened += 1
        self._unsynced = False
        self._enforce_cap()

    def _enforce_cap(self) -> None:
        """Remove the oldest segments while the folder is over its cap; never the open one."""
        try:
            entries = []
            for name in self._files.listdir(self._agent_dir):
                if not name.endswith(".jsonl") or name == "streams.jsonl":
                    continue
                path = os.path.join(self._agent_dir, name)
                try:
                    size, mtime = self._files.stat(path)
                except OSError:
                    continue
                entries.append((mtime, name, path, size))
            total = sum(entry[3] for entry in entries)
            for _mtime, _name, path, size in sorted(entries):
                if total <= self._max_bytes:
                    break
                if path == self._segment_path:
                    continue
                self._check()
                try:
                    self._files.remove(path)
                except OSError:
                    self._fault("remove_failed")
                    continue
                total -= size
                self._segments_removed += 1
        except _Cancelled:
            return
        except Exception:
            self._fault("remove_failed")

    def _close_stream(self) -> None:
        """The clean end: count the tail, write ``stream_closed``, sync."""
        if not self._may_write():
            return
        with self._lock:
            last_seq = self._seq
        if last_seq > self._processed_seq:
            self._dropped += last_seq - self._processed_seq
            self._processed_seq = last_seq
        record = {
            "v": SCHEMA_VERSION,
            "kind": KIND_STREAM_CLOSED,
            "event_id": str(uuid.uuid4()),
            "stream": self.stream,
            "at": format_time(int(self._clock() * 1000)),
            "agent": self.agent,
            "dropped_total": self._dropped,
            "last_seq": last_seq,
        }
        try:
            data = encode_line(validate_event(record))
            if self._handle is None:
                self._open_segment()
            self._check()
            self._files.write(self._handle, data)
            self._check()
            self._files.sync(self._handle)
            self._files.close(self._handle)
            self._handle = None
        except _Cancelled:
            return
        except Exception as exc:
            self._fault(self._fault_for(exc, "write_failed"))
            self._abandon_segment()
            return
        self._closed_cleanly = True

    def _finish(self) -> None:
        """The last status record, then let go of the file.

        After a cancellation nothing is written, with ONE explicit exception (see
        ``_status_permitted``): a start that timed out or failed still gets its status
        record, because that record is how `status` learns why the journal is off (the
        caller never waited for it)."""
        try:
            if self._off_reason == OFF_START_TIMEOUT:
                self._write_status()
            elif self._may_write():
                self._write_status(final=True)
        except _Cancelled:
            pass  # the gate refused: close's deadline has passed
        self._abandon_segment()

    def _write_status(self, final: bool = False) -> None:
        self._last_status = time.monotonic()
        state = self._state
        if final and state == "on":
            state = "closed" if self._closed_cleanly else "on"
        status = {
            "schema_version": SCHEMA_VERSION,
            "agent": self.agent,
            "stream": self.stream,
            "mode": self._mode,
            "unmanaged": self._unmanaged,
            "pid": os.getpid(),
            "process_start_token": self._token,
            "started_at": format_time(self._started_at) if self._started_at else None,
            "updated_at": format_time(int(self._clock() * 1000)),
            "state": state,
            "off_reason": self._off_reason,
            "last_fault": self._last_fault,
            "counts": {
                "emitted": self._seq,
                "written": self._written,
                "dropped": self._dropped,
                "queue_full": self._queue_full,
                "invalid": self._invalid,
                "write_failures": self._write_failures,
                "sync_failures": self._sync_failures,
                "segments_opened": self._segments_opened,
                "segments_removed": self._segments_removed,
            },
            "faults": dict(self._faults),
        }
        try:
            self._files.write_atomic(
                self._status_path, json.dumps(status, sort_keys=True, separators=(",", ":")).encode("ascii")
            )
        except Exception:
            self._fault("status_failed")

    def _prune_old_status(self) -> None:
        """Keep the newest few status files of earlier runs; they are tiny but never end."""
        try:
            mine = os.path.basename(self._status_path)
            old = []
            for name in self._files.listdir(self._agent_dir):
                if name.startswith("status-") and name.endswith(".json") and name != mine:
                    path = os.path.join(self._agent_dir, name)
                    with contextlib.suppress(OSError):
                        old.append((self._files.stat(path)[1], path))
            for _mtime, path in sorted(old)[:-4]:
                if not self._may_write():
                    return
                with contextlib.suppress(OSError):
                    self._files.remove(path)
        except Exception:  # noqa: S110
            pass


def _start_token() -> str | None:
    """The process start token the wrapper's runtime record already uses, or None."""
    try:
        from agenttalk.wrapper_runtime import process_start_token

        return process_start_token(os.getpid())
    except Exception:
        return None


# --- the switch and the live status label ---------------------------------


ENV_TURN_EVENTS = "AGENTTALK_TURN_EVENTS"
#: A status record not rewritten for this long, from a live process, is "not responding".
TURN_EVENTS_STATUS_STALE_SECONDS = 30.0

LABEL_ON = "on (loop)"
LABEL_ON_CADENCE = "on (loop); cadence turns unmanaged"
LABEL_NOT_RESPONDING = "writer not responding"
LABEL_OFF = "off"
LABEL_ENDED = "ended"
LABEL_UNMANAGED_ONE_SHOT = "unmanaged (one_shot)"
LABEL_UNMANAGED_PLAIN = "unmanaged (plain)"


def turn_events_requested(flag: bool, environ: Mapping[str, str] | None = None) -> bool:
    """True when the journal was asked for: the wrap flag, or ``AGENTTALK_TURN_EVENTS=1``."""
    env = os.environ if environ is None else environ
    return bool(flag) or env.get(ENV_TURN_EVENTS) == "1"


def _read_status_file(path: Path) -> dict[str, Any] | None:
    try:
        raw = path.read_bytes()
        obj = json.loads(raw.decode("ascii"))
    except (OSError, UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(obj, dict) or obj.get("schema_version") != SCHEMA_VERSION:
        return None
    pid = obj.get("pid")
    if type(pid) is not int or pid <= 0 or obj.get("state") not in ("on", "off", "closed"):
        return None
    updated = parse_time(obj.get("updated_at"))
    if updated is None:
        return None
    unmanaged = obj.get("unmanaged")
    return {
        "pid": pid,
        "token": obj.get("process_start_token") if isinstance(obj.get("process_start_token"), str) else None,
        "state": obj["state"],
        "off_reason": obj.get("off_reason") if obj.get("off_reason") in OFF_REASONS else None,
        "unmanaged": [u for u in unmanaged if u == "cadence"] if isinstance(unmanaged, list) else [],
        "updated_ms": updated,
    }


def status_records(agent_dir: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """Every well-formed status record of an agent, in no particular order."""
    try:
        names = os.listdir(agent_dir)
    except OSError:
        return []
    found = []
    for name in names:
        if name.startswith("status-") and name.endswith(".json"):
            record = _read_status_file(Path(agent_dir) / name)
            if record is not None:
                found.append(record)
    return found


def newest_status(agent_dir: str | os.PathLike[str]) -> dict[str, Any] | None:
    """The most recently updated well-formed status record of an agent, or None."""
    return max(status_records(agent_dir), key=lambda r: r["updated_ms"], default=None)


def journal_in_use(project_root: str | os.PathLike[str], environ: Mapping[str, str] | None = None) -> bool:
    """True when this project has a turn-journal folder (so status has something to say)."""
    try:
        return default_turn_events_root(project_root, environ=environ).is_dir()
    except Exception:  # noqa: BLE001
        return False


#: The wrapper's own health record carries this word when the writer never ran
#: (the thread could not start, or the start bound passed): a closed fact in a write
#: the wrapper already makes, so a failed start is visible without any new I/O.
HEALTH_WARNING_PREFIX = "turn_journal_"
_START_WARNINGS = {"turn_journal_start_failed": "start_failed", "turn_journal_start_timeout": "start_timeout"}


def owner_tag(pid: object, token: object) -> str:
    """Who wrote a warning: the writing process's pid and a short digest of its start token
    ("none" when it has no token). Safe characters only, so it fits a health warning word."""
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:16] if isinstance(token, str) and token else "none"
    return "%s:%s" % (pid, digest)


def start_failure_warning(off_reason: object, pid: object = None, token: object = None) -> str | None:
    """The health-record word for a journal that never ran, or None. It carries its owner
    (``word:pid:digest``), so a later wrapper never shows a previous wrapper's failure."""
    for word, reason in _START_WARNINGS.items():
        if reason == off_reason:
            return "%s:%s" % (word, owner_tag(pid, token))
    return None


def _owned_start_failure(warnings: Sequence[object] | None, pid: object, token: object) -> str | None:
    """The start-failure reason whose owner is exactly this process, else None."""
    mine = owner_tag(pid, token)
    for word in warnings or ():
        if not isinstance(word, str):
            continue
        head, _sep, owner = word.partition(":")
        if head in _START_WARNINGS and owner == mine:
            return _START_WARNINGS[head]
    return None


def status_label(
    project_root: str | os.PathLike[str],
    agent: str,
    *,
    health_mode: str | None,
    runtime_record: Mapping[str, Any] | None = None,
    now_epoch: float | None = None,
    environ: Mapping[str, str] | None = None,
    start_token: Callable[[int], str | None] | None = None,
    health_warnings: Sequence[object] | None = None,
    process_running: Callable[[int], bool] | None = None,
) -> str:
    """What `status` shows about an agent's turn journal, from LIVE facts.

    Liveness is proven, never assumed: a process counts as running only when it is
    still running (an affirmative answer; on Windows an exited child whose parent holds its
    handle still has a readable start time, so identity alone is not enough) AND its start
    token equals the one in the record. An unknown or unreadable answer is "not running".
    Among the records of the agent, those of the current wrapper (when its runtime record
    proves it alive) are chosen first, then the newest. ``health_mode``
    is the wrapper's own health mode, ``runtime_record`` its runtime record and
    ``health_warnings`` the warning words of its health record.
    """
    if start_token is None:
        from agenttalk.wrapper_runtime import process_start_token as start_token

        if process_running is None:
            from agenttalk.store import _process_alive as process_running
    elif process_running is None:
        # A fake process table (tests): a pid is running when it has a start token.
        table = start_token

        def process_running(pid: int) -> bool:
            return table(pid) is not None

    def proven_alive(pid: object, token: object) -> bool:
        if type(pid) is not int or pid <= 0 or not isinstance(token, str):
            return False
        try:
            if process_running(pid) is not True:
                return False
            current = start_token(pid)
        except Exception:  # noqa: BLE001
            return False
        return isinstance(current, str) and current == token

    current_pid = None
    if runtime_record:
        pid = runtime_record.get("wrapper_pid")
        if proven_alive(pid, runtime_record.get("wrapper_start")):
            current_pid = pid
    if health_mode == "wrapper-one-shot":
        # Plain `wrap` and `wrap --loop --one-shot` both report this mode; only the
        # loop form has a live wrapper runtime record.
        return LABEL_UNMANAGED_ONE_SHOT if current_pid is not None else LABEL_UNMANAGED_PLAIN
    if health_mode not in ("wrapper-loop", "lead-loop"):
        return LABEL_OFF
    try:
        directory = default_turn_events_root(project_root, environ=environ) / agent
    except Exception:  # noqa: BLE001
        return LABEL_OFF
    records = status_records(directory)
    if current_pid is not None:
        # The owner first: a record from another run, whatever its timestamp, is not
        # this wrapper's.
        owned = [r for r in records if r["pid"] == current_pid]
        if runtime_record and isinstance(runtime_record.get("wrapper_start"), str):
            owned = [r for r in owned if r["token"] == runtime_record["wrapper_start"]]
        found = max(owned, key=lambda r: r["updated_ms"], default=None)
        if found is None:
            # The writer never ran for this wrapper: its start failure, if any, is in the
            # wrapper's own health record.
            reason = _owned_start_failure(
                health_warnings, current_pid, runtime_record.get("wrapper_start") if runtime_record else None
            )
            return LABEL_OFF + (" (%s)" % reason if reason else "")
    else:
        # No proven current wrapper: prefer a record whose writer is provably running.
        running = [r for r in records if proven_alive(r["pid"], r["token"])]
        found = max(running or records, key=lambda r: r["updated_ms"], default=None)
        if found is None:
            return LABEL_OFF
    if found["state"] == "off":
        return LABEL_OFF + (" (%s)" % found["off_reason"] if found["off_reason"] else "")
    if found["state"] == "closed" or not proven_alive(found["pid"], found["token"]):
        return LABEL_ENDED
    now_ms = int((time.time() if now_epoch is None else now_epoch) * 1000)
    age = max(0.0, (now_ms - found["updated_ms"]) / 1000.0)
    if age > TURN_EVENTS_STATUS_STALE_SECONDS:
        return LABEL_NOT_RESPONDING
    cadence = health_mode == "lead-loop" or bool(found["unmanaged"])
    return LABEL_ON_CADENCE if cadence else LABEL_ON


__all__ = [
    "CLIS",
    "DISPOSITIONS",
    "ENV_TURN_EVENTS",
    "ENV_TURN_EVENTS_DIR",
    "EVENT_KINDS",
    "EXITS",
    "FAILURE_CLASSES",
    "JournalFiles",
    "OFF_REASONS",
    "OUTCOMES",
    "RAW_FACT_FIELDS",
    "SCHEMA_VERSION",
    "SegmentRead",
    "TURN_EVENTS_CLOSE_SECONDS",
    "TURN_EVENTS_MAX_BYTES",
    "TURN_EVENTS_QUEUE_MAX",
    "TURN_EVENTS_START_SECONDS",
    "TurnEventError",
    "TurnEventSink",
    "UnsupportedSchemaVersion",
    "journal_in_use",
    "HEALTH_WARNING_PREFIX",
    "newest_status",
    "start_failure_warning",
    "status_records",
    "status_label",
    "turn_events_requested",
    "default_turn_events_root",
    "encode_line",
    "iter_records",
    "list_segments",
    "parse_line",
    "read_segment",
    "read_streams",
    "read_streams_checked",
    "validate_event",
]
