"""Bounded wrapper process logs and factual lifecycle diagnostics.

The generated supervisor redirects each wrapper launch to a unique generation
directory.  Once ``agenttalk wrap`` is running, this module keeps the redirected
bootstrap stream bounded and mirrors the newest output through a fixed segment
ring.  The lifecycle JSONL is diagnostic output only: it is never read by the
supervisor and carries no health or restart authority.

The bound is Python-level only: it wraps ``sys.stdout``/``sys.stderr`` and
bounds whatever text is written through those objects.  A write that reaches
file descriptor 1/2 directly - bypassing the Python stream objects entirely -
is not intercepted. No such writer exists in this project today (no third-party
dependencies, no direct ``os.write(1/2, ...)`` call, and the wrapped model
child's own stdout/stderr are piped rather than inherited), so the gap is
tracked but not yet closed.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import stat
import sys
import tempfile
import threading
import traceback
from collections.abc import Iterator, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

from agenttalk.signing import project_id_for_root


ENV_STDOUT_PATH = "AGENTTALK_WRAPPER_STDOUT_LOG"
ENV_STDERR_PATH = "AGENTTALK_WRAPPER_STDERR_LOG"
ENV_MAX_BYTES = "AGENTTALK_WRAPPER_LOG_MAX_BYTES"
ENV_SEGMENT_COUNT = "AGENTTALK_WRAPPER_LOG_SEGMENTS"
ENV_LAUNCH_NONCE = "AGENTTALK_WRAPPER_LOG_NONCE"

WRAPPER_LOG_GENERATIONS = 4
WRAPPER_LOG_MAX_BYTES = 1024 * 1024
WRAPPER_LOG_SEGMENT_COUNT = 4
_MIN_MAX_BYTES = 4 * 1024
_MAX_MAX_BYTES = 64 * 1024 * 1024
_MIN_SEGMENTS = 2
_MAX_SEGMENTS = 32

_RUNTIME_FIELDS = (
    "phase",
    "turn_generation",
    "turn_id",
    "message_id",
    "cli_launcher_pid",
    "progress_sequence",
    "last_progress_at",
    "last_outcome",
)
_TRANSITION_EVENTS = {
    "idle": "waiting_for_mail",
    "starting": "turn_started",
    "active": "child_spawned",
    "terminal": "turn_ended",
    "dead_letter": "message_dead_lettered",
}


def _authenticated_environment(
    environ: Mapping[str, str],
    expected_nonce: str | None,
) -> bool:
    return bool(
        isinstance(expected_nonce, str)
        and len(expected_nonce) == 32
        and all(char in "0123456789abcdef" for char in expected_nonce)
        and environ.get(ENV_LAUNCH_NONCE) == expected_nonce
        and environ.get(ENV_STDOUT_PATH)
        and environ.get(ENV_STDERR_PATH)
    )


def _home_state_root(home_env: str | None, *parts: str) -> Path | None:
    """Resolve a home-relative state directory, tolerating an unresolvable
    home (no HOME/USERPROFILE and no passwd entry) by treating it as an
    unavailable candidate rather than raising."""
    try:
        home = Path(home_env) if home_env else Path.home()
    except RuntimeError:
        return None
    return home.expanduser().resolve().joinpath(*parts)


def _candidate_state_roots(
    configured: Path | None,
    home_env: str | None,
    home_parts: tuple[str, ...],
    project: Path,
) -> Iterator[Path]:
    # Each candidate is resolved only once the previous one has been tried
    # and rejected, so an unresolvable home fallback is never even attempted
    # - let alone allowed to raise - while a valid configured path (the
    # preferred input) is still available and unconsulted.
    if configured is not None and configured.is_absolute():
        yield configured
    home_root = _home_state_root(home_env, *home_parts)
    if home_root is not None:
        yield home_root
    yield Path(tempfile.gettempdir()).resolve()
    yield project.parent / ".agenttalk-wrapper-logs"


def default_wrapper_log_root(
    project_root: str | os.PathLike[str],
    *,
    platform: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Return a persistent per-user, per-project log root outside the checkout."""
    env = os.environ if environ is None else environ
    target = os.name if platform is None else platform
    if target == "nt":
        raw = env.get("LOCALAPPDATA")
        home_env, home_parts = env.get("USERPROFILE"), ("AppData", "Local")
    else:
        raw = env.get("XDG_STATE_HOME")
        home_env, home_parts = env.get("HOME"), (".local", "state")
    # A relative state-directory value is interpreted against the process cwd.
    # The supervisor must not let ambient cwd move diagnostic logs back into the
    # checkout, so only absolute platform state roots are eligible.
    configured = Path(raw).expanduser() if raw else None
    project = Path(project_root).resolve()
    project_id = project_id_for_root(project)
    seen: set[Path] = set()
    for candidate in _candidate_state_roots(configured, home_env, home_parts, project):
        if candidate in seen:
            continue
        seen.add(candidate)
        result = candidate / "agenttalk" / "wrapper-logs" / project_id
        if not result.resolve().is_relative_to(project):
            return result
    # A checkout rooted at the filesystem anchor has no same-volume "outside".
    # Keep supervision usable; the generated script still has its independent
    # temporary fallback and logging remains fail-soft.
    return Path(tempfile.gettempdir()).resolve() / "agenttalk" / "wrapper-logs" / project_id


def _bounded_int(
    value: object,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return default
    return parsed if minimum <= parsed <= maximum else default


def _utf8_prefix(text: str, budget: int) -> str:
    if budget <= 0:
        return ""
    raw = text.encode("utf-8", "replace")
    if len(raw) <= budget:
        return text
    return raw[:budget].decode("utf-8", "ignore")


def _utf8_chunk_boundary(raw, limit: int) -> int:
    """Largest n <= min(limit, len(raw)) such that raw[:n] does not end
    mid-code-point. Assumes raw is itself well-formed UTF-8 as a whole (as
    produced by str.encode("utf-8", ...)), not that every prefix of it is -
    only the last, possibly-truncated code point within the limit matters."""
    n = min(limit, len(raw))
    if n <= 0:
        return 0
    start = n - 1
    while start > 0 and (raw[start] & 0xC0) == 0x80:
        start -= 1
    lead = raw[start]
    if lead < 0x80:
        seq_len = 1
    elif lead & 0xE0 == 0xC0:
        seq_len = 2
    elif lead & 0xF0 == 0xE0:
        seq_len = 3
    elif lead & 0xF8 == 0xF0:
        seq_len = 4
    else:
        seq_len = 1
    return n if start + seq_len <= n else start


def _harden_posix_log_paths(*paths: Path) -> None:
    if os.name == "nt":
        return
    directories: set[Path] = set()
    for path in paths:
        directories.add(path.parent)
        directories.add(path.parent.parent)
        directories.add(path.parent.parent.parent)
        with contextlib.suppress(OSError):
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    for directory in directories:
        with contextlib.suppress(OSError):
            os.chmod(directory, stat.S_IRWXU)


class BoundedStreamTee:
    """Text stream that bounds the inherited redirect and keeps a newest-output ring.

    ``base_path`` is the file PowerShell supplied to
    ``-RedirectStandardOutput``/``-RedirectStandardError``.  The original stream
    continues feeding that file only for one segment.  Suffix segments
    ``.1`` ... ``.<N-1>`` are owned directly by this process and cycle in place.
    Thus every file is bounded and the complete generation stays under
    ``max_bytes`` during normal operation.

    If the suffix ring becomes unwritable, the wrapper continues and the
    original stream remains capped; excess diagnostic output is discarded
    rather than turning a logging failure into either a launch failure or an
    unbounded disk write.
    """

    def __init__(
        self,
        original: TextIO,
        base_path: str | os.PathLike[str],
        *,
        max_bytes: int = WRAPPER_LOG_MAX_BYTES,
        segment_count: int = WRAPPER_LOG_SEGMENT_COUNT,
    ) -> None:
        if max_bytes < _MIN_MAX_BYTES or max_bytes > _MAX_MAX_BYTES:
            raise ValueError("max_bytes is outside the supported bound")
        if segment_count < _MIN_SEGMENTS or segment_count > _MAX_SEGMENTS:
            raise ValueError("segment_count is outside the supported bound")
        self._original = original
        self.base_path = Path(base_path)
        self.max_bytes = int(max_bytes)
        self.segment_count = int(segment_count)
        self.segment_bytes = max(1, self.max_bytes // self.segment_count)
        with contextlib.suppress(OSError, ValueError):
            self._original.flush()
        try:
            existing_base_bytes = self.base_path.stat().st_size
        except OSError:
            existing_base_bytes = 0
        self._forward_remaining = max(
            0,
            self.segment_bytes - existing_base_bytes,
        )
        self._tail_count = self.segment_count - 1
        self._tail_index = 0
        self._tail_size = 0
        self._tail = None
        self._tail_failed = False
        self._closed = False
        self._lock = threading.Lock()

    @property
    def encoding(self) -> str:
        return getattr(self._original, "encoding", None) or "utf-8"

    @property
    def errors(self) -> str:
        return getattr(self._original, "errors", None) or "replace"

    @property
    def closed(self) -> bool:
        return self._closed

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        return self._original.fileno()

    def writable(self) -> bool:
        return not self._closed

    def _tail_path(self) -> Path:
        return self.base_path.with_name(
            f"{self.base_path.name}.{self._tail_index + 1}"
        )

    def _open_tail(self) -> None:
        self.base_path.parent.mkdir(parents=True, exist_ok=True)
        path = self._tail_path()
        self._tail = path.open("wb")
        self._tail_size = 0
        if os.name != "nt":
            with contextlib.suppress(OSError):
                os.chmod(path, 0o600)

    def _advance_tail(self) -> None:
        if self._tail is not None:
            self._tail.flush()
            self._tail.close()
        self._tail_index = (self._tail_index + 1) % self._tail_count
        self._tail = None
        self._open_tail()

    def _write_tail(self, raw: bytes) -> None:
        remaining = memoryview(raw)
        while remaining:
            if self._tail is None:
                self._open_tail()
            if self._tail_size >= self.segment_bytes:
                self._advance_tail()
            available = self.segment_bytes - self._tail_size
            boundary = _utf8_chunk_boundary(remaining, available)
            if boundary == 0:
                # The next code point does not fit in what is left of this
                # segment at all - not only right after a rotation, but any
                # time a nearly-full segment's remaining room is narrower
                # than the code point landing there. Rotating to a fresh
                # segment (segment_bytes is bounded well above 4 bytes, the
                # max UTF-8 sequence length) gives it room to fit whole,
                # rather than splitting its encoded bytes across files, and
                # rather than looping forever re-deriving the same
                # zero-byte boundary against an unchanged buffer.
                self._advance_tail()
                available = self.segment_bytes
                boundary = _utf8_chunk_boundary(remaining, available)
                if boundary == 0:
                    # Unreachable given the enforced segment_bytes bound -
                    # a last resort so a future bound change fails soft
                    # (one split code point) rather than hanging.
                    boundary = 1
            chunk = remaining[:boundary]
            tail = self._tail
            if tail is None:
                raise OSError("wrapper log tail is unavailable")
            # Account for the write BEFORE issuing it, not after: a
            # terminating signal's handler runs between bytecode
            # instructions, so it can only land in the gap between this
            # call and the next one - never inside the write() call itself.
            # Accounting first means a signal landing in that gap leaves
            # self._tail_size OVERSTATED relative to the file (nothing was
            # actually written yet), which only rotates a little early on
            # the next write. Accounting after leaves it UNDERSTATED once
            # the bytes are already on disk, so the next write believes it
            # has more room than it does and can push the segment past
            # segment_bytes by up to another chunk.
            self._tail_size += len(chunk)
            tail.write(chunk)
            # I4 (cold review, round 3): once the base segment's forwarding
            # budget is spent, EVERY further diagnostic write lands only
            # here - the tail ring is the sole surviving copy at that
            # point, so it needs the same durability guarantee the base
            # segment's own flush-on-newline fix gives the original
            # stream. tail is a raw binary file (no line-buffering concept
            # to replicate); flush unconditionally rather than trying to
            # infer "was that a diagnostic line" from raw bytes.
            tail.flush()
            remaining = remaining[len(chunk):]

    def _write_original(self, text: str, *, bounded: bool) -> None:
        if bounded:
            prefix = _utf8_prefix(text, self._forward_remaining)
            if not prefix:
                return
            text = prefix
        encoded = text.encode("utf-8", "replace")
        if bounded:
            self._forward_remaining -= len(encoded)
        try:
            # Writing through self._original's own .write() would let its text
            # mode translate "\n" to the platform line separator (CRLF on
            # Windows) before it hits disk - the actual bytes written would
            # then exceed the pre-translation length just budgeted above,
            # letting a newline-heavy stream blow the cap by nearly 2x. Write
            # the already-UTF-8-encoded bytes straight to the underlying
            # buffer instead, so the accounted length is the written length.
            buffer = getattr(self._original, "buffer", None)
            if buffer is not None:
                buffer.write(encoded)
                # I4: bypassing .write() also bypasses line buffering's own
                # flush-on-newline - stderr is line-buffered by default, so
                # a diagnostic line written just before an uncatchable
                # SIGKILL would otherwise sit unflushed in the buffer's own
                # (larger, not newline-triggered) internal buffer and never
                # reach disk at all, defeating the reason this module
                # exists. Replicate exactly the condition TextIOWrapper
                # itself would have flushed on.
                if getattr(self._original, "line_buffering", False) and (
                    b"\n" in encoded
                ):
                    buffer.flush()
            else:
                self._original.write(text)
        except (OSError, ValueError):
            pass

    def write(self, value: object) -> int:
        if self._closed:
            raise ValueError("I/O operation on closed wrapper log stream")
        text = str(value)
        raw = text.encode("utf-8", "replace")
        with self._lock:
            if not self._tail_failed:
                try:
                    self._write_tail(raw)
                except (OSError, ValueError):
                    self._tail_failed = True
                    if self._tail is not None:
                        with contextlib.suppress(OSError, ValueError):
                            self._tail.close()
                    self._tail = None
            self._write_original(text, bounded=True)
        return len(text)

    def writelines(self, lines) -> None:
        for line in lines:
            self.write(line)

    def flush(self) -> None:
        with self._lock:
            with contextlib.suppress(OSError, ValueError):
                self._original.flush()
            if self._tail is not None:
                with contextlib.suppress(OSError, ValueError):
                    self._tail.flush()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._tail is not None:
                with contextlib.suppress(OSError, ValueError):
                    self._tail.flush()
                    self._tail.close()
            self._tail = None
            self._closed = True


# Set by installed_standard_streams_from_environment right before it installs
# the bounded tees, and deliberately never cleared: it is read exactly once,
# by print_bounded_uncaught_exception at the true top-level script boundary
# (agenttalk/__main__.py), which runs strictly after this module's own
# context manager has already restored sys.stdout/sys.stderr and is no
# longer reachable from that call site. Holding onto a resolved path and two
# bounded integers - not a stream or hook - so a stale value left over after
# a normal exit is inert rather than something that could leak behavior into
# unrelated later code.
_LAST_STDERR_LOG_CONFIG: tuple[str, int, int] | None = None


@contextlib.contextmanager
def installed_standard_streams_from_environment(
    environ: Mapping[str, str] | None = None,
    *,
    expected_nonce: str | None = None,
) -> Iterator[None]:
    """Install bounded stdout/stderr mirrors only for supervisor-marked wrappers."""
    global _LAST_STDERR_LOG_CONFIG
    env = os.environ if environ is None else environ
    if not _authenticated_environment(env, expected_nonce):
        yield
        return
    stdout_path = env.get(ENV_STDOUT_PATH)
    stderr_path = env.get(ENV_STDERR_PATH)
    if not stdout_path or not stderr_path:
        yield
        return
    _harden_posix_log_paths(Path(stdout_path), Path(stderr_path))
    max_bytes = _bounded_int(
        env.get(ENV_MAX_BYTES),
        default=WRAPPER_LOG_MAX_BYTES,
        minimum=_MIN_MAX_BYTES,
        maximum=_MAX_MAX_BYTES,
    )
    segment_count = _bounded_int(
        env.get(ENV_SEGMENT_COUNT),
        default=WRAPPER_LOG_SEGMENT_COUNT,
        minimum=_MIN_SEGMENTS,
        maximum=_MAX_SEGMENTS,
    )
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    stdout = BoundedStreamTee(
        original_stdout,
        stdout_path,
        max_bytes=max_bytes,
        segment_count=segment_count,
    )
    stderr = BoundedStreamTee(
        original_stderr,
        stderr_path,
        max_bytes=max_bytes,
        segment_count=segment_count,
    )
    sys.stdout = stdout
    sys.stderr = stderr
    _LAST_STDERR_LOG_CONFIG = (stderr_path, max_bytes, segment_count)
    try:
        yield
    finally:
        sys.stdout = original_stdout
        sys.stderr = original_stderr
        stdout.close()
        stderr.close()


def print_bounded_uncaught_exception() -> None:
    """Print the currently-propagating exception the way Python's own
    top-level printer would - call only from the real script entry point
    (agenttalk/__main__.py), from an except block, once main() has let an
    exception escape uncaught.

    By the time control reaches here, installed_standard_streams_from_
    environment's own finally has already restored sys.stdout/sys.stderr -
    cmd_wrap's bare re-raise of an unexpected exception had to leave that
    restore unconditional so an embedder catching the same exception gets
    back clean, unmodified stream state. For a supervisor-launched wrapper,
    the restored stderr IS the raw file the supervisor redirected to, with
    no cap of its own - letting Python's default printer write a second,
    unbounded copy of a traceback there (the first copy already landed,
    correctly bounded, inside cmd_wrap before this ever runs) can push that
    file past the advertised cap. Route this copy through the same bounded
    tail-ring mechanism instead; a manual run with no wrapper log installed
    prints normally, unchanged.
    """
    config = _LAST_STDERR_LOG_CONFIG
    if config is None:
        traceback.print_exc(file=sys.stderr)
        return
    stderr_path, max_bytes, segment_count = config
    try:
        tee = BoundedStreamTee(
            io.StringIO(),
            stderr_path,
            max_bytes=max_bytes,
            segment_count=segment_count,
        )
        try:
            traceback.print_exc(file=tee)
        finally:
            tee.close()
    except Exception:
        # Best-effort: a failure bounding this redundant second copy must
        # not swallow the crash itself.
        with contextlib.suppress(Exception):
            traceback.print_exc(file=sys.stderr)


def _utc_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


class WrapperLifecycleLog:
    """Fail-soft JSONL of facts observed by one wrapper process."""

    def __init__(
        self,
        agent: str,
        *,
        stream: TextIO | None = None,
        clock=None,
        wrapper_pid: int | None = None,
        enabled: bool = True,
    ) -> None:
        import time

        self.agent = agent
        self.stream = sys.stderr if stream is None else stream
        self._clock = time.time if clock is None else clock
        self._wrapper_pid = os.getpid() if wrapper_pid is None else wrapper_pid
        self.enabled = bool(enabled)
        self._runtime: dict = {}
        self._pending_signal: tuple[int, bool] | None = None
        self.terminal_emitted = False

    @classmethod
    def from_environment(
        cls,
        agent: str,
        *,
        expected_nonce: str | None = None,
    ) -> "WrapperLifecycleLog":
        enabled = _authenticated_environment(os.environ, expected_nonce)
        return cls(agent, enabled=enabled)

    def _emit(self, event: str, **facts: object) -> None:
        if not self.enabled:
            return
        row: dict[str, object] = {
            "at": _utc_iso(float(self._clock())),
            "event": event,
            "agent": self.agent,
            "wrapper_pid": self._runtime.get(
                "wrapper_pid",
                self._wrapper_pid,
            ),
        }
        for field in _RUNTIME_FIELDS:
            row[field] = self._runtime.get(field)
        row.update(facts)
        try:
            self.stream.write(
                json.dumps(
                    row,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            )
            self.stream.flush()
        except (OSError, ValueError):
            pass

    def runtime_transition(self, transition: str, record: dict) -> None:
        self._runtime = dict(record)
        event = _TRANSITION_EVENTS.get(transition)
        if event is not None:
            self._emit(event)

    def child_exited(
        self,
        child_pid: int,
        child_start: str | None,
        return_code: int,
    ) -> None:
        self._emit(
            "child_exited",
            child_pid=child_pid,
            child_start=child_start,
            return_code=return_code,
        )

    def wrapper_exited(self, exit_code: int, *, reason: str) -> None:
        self.terminal_emitted = True
        self._emit("wrapper_exited", exit_code=exit_code, reason=reason)

    def wrapper_exception(self, exc: BaseException) -> None:
        self.terminal_emitted = True
        self._emit("wrapper_exception", exception_type=type(exc).__name__)

    def signal_received(self, signum: int, *, terminating: bool = True) -> None:
        if terminating:
            self.terminal_emitted = True
        try:
            signal_name = signal.Signals(signum).name
        except (ValueError, TypeError):
            signal_name = None
        self._emit(
            "wrapper_signal_received",
            signal=int(signum),
            signal_name=signal_name,
            terminating=terminating,
        )

    def defer_signal(self, signum: int, *, terminating: bool) -> None:
        # A Python signal handler can interrupt a stream write between its file
        # write and accounting update. Record only the scalar here; emit after
        # the interrupted stack has unwound out of the signal context.
        self._pending_signal = (int(signum), bool(terminating))

    def flush_deferred_signal(self) -> None:
        pending = self._pending_signal
        self._pending_signal = None
        if pending is not None:
            signum, terminating = pending
            self.signal_received(signum, terminating=terminating)


@contextlib.contextmanager
def capture_termination_signals(
    lifecycle: WrapperLifecycleLog,
) -> Iterator[None]:
    """Log supported catchable signals, then preserve their prior behavior."""
    if not lifecycle.enabled or threading.current_thread() is not threading.main_thread():
        yield
        return
    supported = []
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        value = getattr(signal, name, None)
        if value is not None and value not in supported:
            supported.append(value)
    previous: dict[int, object] = {}

    def _handler(signum: int, frame: object) -> None:
        prior = previous.get(signum, signal.SIG_DFL)
        if callable(prior):
            try:
                prior(signum, frame)
            except BaseException:
                lifecycle.defer_signal(signum, terminating=True)
                raise
            lifecycle.defer_signal(signum, terminating=False)
            return
        if prior == signal.SIG_IGN:
            return
        lifecycle.defer_signal(signum, terminating=True)
        if signum == getattr(signal, "SIGINT", None):
            raise KeyboardInterrupt
        raise SystemExit(128 + int(signum))

    try:
        for signum in supported:
            try:
                previous[int(signum)] = signal.getsignal(signum)
                signal.signal(signum, _handler)
            except (OSError, ValueError):
                previous.pop(int(signum), None)
        yield
    finally:
        for signum, prior in previous.items():
            with contextlib.suppress(OSError, ValueError):
                signal.signal(signum, prior)
        lifecycle.flush_deferred_signal()
