"""Exact-process, cross-process lifecycle serialization.

The persistent artifact is never replaced.  Its last byte is reserved for the
kernel lock; bounded JSON authority remains readable while another process owns
that byte.  A crashed holder leaves an exact PID/start record behind, and a new
holder may replace it only after proving that exact process dead or PID-reused.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import json
import os
from pathlib import Path
import re
import stat
import sys
import threading
import time
from typing import Iterator
import uuid


_SCHEMA_VERSION = 1
_ARTIFACT_BYTES = 4096
_LOCK_OFFSET = _ARTIFACT_BYTES - 1
_METADATA_BYTES = _LOCK_OFFSET
_MAX_WINDOWS_PID = (1 << 32) - 1
_MAX_FILETIME = (1 << 64) - 1
_SAFE_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_HEX_GENERATION = re.compile(r"[0-9a-f]{32}")
_UTC_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z"
)
_BOOT_ID = re.compile(r"[0-9A-Fa-f-]{32,64}")
_LOCAL_LOCKS_GUARD = threading.Lock()
_LOCAL_LOCKS: dict[str, threading.Lock] = {}


@dataclass(frozen=True)
class ProcessIdentity:
    """One exact OS process-start identity."""

    scheme: str
    value: str


class LifecycleLockError(RuntimeError):
    """Base class for typed lifecycle-lock refusals."""

    retryable = True


class LifecycleLockContended(LifecycleLockError):
    """A known exact holder retained the lifecycle authority."""

    reason_code = "lifecycle_lock_contended"

    def __init__(self, record: dict) -> None:
        identity = record["process_identity"]
        self.holder_pid = record["pid"]
        self.holder_identity = dict(identity)
        self.holder_operation = record["operation"]
        self.holder_since = record["acquired_at"]
        super().__init__(
            f"{self.reason_code}: held by pid {self.holder_pid} "
            f"({identity['scheme']}:{identity['value']}) for "
            f"{self.holder_operation} since {self.holder_since}; wait for that "
            "operation to finish, then retry"
        )


class LifecycleLockUnknown(LifecycleLockError):
    """The lifecycle authority could not be classified safely."""

    reason_code = "lifecycle_lock_unknown"

    def __init__(self, path: Path, detail: str) -> None:
        self.path = path
        self.detail = detail
        super().__init__(
            f"{self.reason_code}: {detail}; confirm no lifecycle operation is "
            f"running, then remove {path} and retry"
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _released_record(authority: str) -> dict:
    return {
        "authority": authority,
        "schema_version": _SCHEMA_VERSION,
        "state": "released",
    }


def _identity_value_valid(identity: object) -> bool:
    if not isinstance(identity, dict) or set(identity) != {"scheme", "value"}:
        return False
    scheme = identity.get("scheme")
    value = identity.get("value")
    if scheme == "win32-filetime-v1":
        if not isinstance(value, str) or re.fullmatch(r"[1-9][0-9]{0,19}", value) is None:
            return False
        return int(value) <= _MAX_FILETIME
    if scheme == "linux-proc-start-v1":
        if not isinstance(value, str) or ":" not in value:
            return False
        boot_id, start_ticks = value.rsplit(":", 1)
        return _BOOT_ID.fullmatch(boot_id) is not None and bool(
            re.fullmatch(r"[1-9][0-9]*", start_ticks)
        )
    if scheme == "darwin-proc-bsdinfo-v1":
        if not isinstance(value, str) or ":" not in value:
            return False
        seconds, microseconds = value.split(":", 1)
        return bool(re.fullmatch(r"[1-9][0-9]*", seconds)) and bool(
            re.fullmatch(r"(?:0|[1-9][0-9]{0,5})", microseconds)
        ) and int(microseconds) <= 999_999
    return False


def _validate_record(value: object, authority: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError("lock metadata is not an object")
    if type(value.get("schema_version")) is not int or value["schema_version"] != _SCHEMA_VERSION:
        raise ValueError("lock metadata schema is corrupt")
    if value.get("authority") != authority:
        raise ValueError("lock metadata authority is corrupt")
    state = value.get("state")
    if state == "released":
        if set(value) != {"authority", "schema_version", "state"}:
            raise ValueError("released lock metadata is corrupt")
        return value
    expected = {
        "authority",
        "schema_version",
        "state",
        "generation",
        "pid",
        "process_identity",
        "operation",
        "acquired_at",
    }
    if state != "held" or set(value) != expected:
        raise ValueError("held lock metadata is corrupt")
    generation = value.get("generation")
    pid = value.get("pid")
    operation = value.get("operation")
    acquired_at = value.get("acquired_at")
    if not isinstance(generation, str) or _HEX_GENERATION.fullmatch(generation) is None:
        raise ValueError("lock generation is corrupt")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise ValueError("lock holder pid is corrupt")
    if os.name == "nt" and pid > _MAX_WINDOWS_PID:
        raise ValueError("lock holder pid is corrupt")
    if not _identity_value_valid(value.get("process_identity")):
        raise ValueError("lock holder process identity is corrupt")
    if not isinstance(operation, str) or _SAFE_LABEL.fullmatch(operation) is None:
        raise ValueError("lock operation is corrupt")
    if not isinstance(acquired_at, str) or _UTC_TIMESTAMP.fullmatch(acquired_at) is None:
        raise ValueError("lock acquisition time is corrupt")
    try:
        datetime.fromisoformat(acquired_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("lock acquisition time is corrupt") from exc
    return value


def _decode_record(raw: bytes, authority: str) -> dict:
    if len(raw) != _METADATA_BYTES:
        raise ValueError("lock metadata length is corrupt")
    payload, separator, padding = raw.partition(b"\0")
    if not separator or any(padding):
        raise ValueError("lock metadata padding is corrupt")
    if not payload.endswith(b"\n"):
        raise ValueError("lock metadata terminator is corrupt")

    def strict_object(pairs: list[tuple[str, object]]) -> dict:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("lock metadata contains a duplicate key")
            value[key] = item
        return value

    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=strict_object,
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ValueError("lock metadata JSON is corrupt") from exc
    return _validate_record(value, authority)


def _encode_record(value: dict) -> bytes:
    payload = (
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    if len(payload) >= _METADATA_BYTES:
        raise ValueError("lock metadata exceeds its fixed bound")
    return payload + b"\0" * (_METADATA_BYTES - len(payload))


def _windows_process_observation(pid: int) -> tuple[str, ProcessIdentity | None]:
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return ("dead", None) if ctypes.get_last_error() == 87 else ("unknown", None)
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return "unknown", None
            if exit_code.value != 259:
                return "dead", None
            creation = wintypes.FILETIME()
            exit_time = wintypes.FILETIME()
            kernel = wintypes.FILETIME()
            user = wintypes.FILETIME()
            if not kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                return "unknown", None
            ticks = (int(creation.dwHighDateTime) << 32) | int(
                creation.dwLowDateTime
            )
            if ticks <= 0:
                return "unknown", None
            return "alive", ProcessIdentity("win32-filetime-v1", str(ticks))
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001 - authority ambiguity is UNKNOWN
        return "unknown", None


def _linux_process_observation(pid: int) -> tuple[str, ProcessIdentity | None]:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "dead", None
    except PermissionError:
        pass
    except (OSError, OverflowError):
        return "unknown", None
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        closing = text.rfind(")")
        if closing < 0:
            return "unknown", None
        fields = text[closing + 2 :].split()
        if len(fields) < 20 or re.fullmatch(r"[1-9][0-9]*", fields[19]) is None:
            return "unknown", None
        if fields[0] == "Z":
            return "dead", None
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(
            encoding="utf-8"
        ).strip()
        if _BOOT_ID.fullmatch(boot_id) is None:
            return "unknown", None
        return "alive", ProcessIdentity(
            "linux-proc-start-v1", f"{boot_id}:{fields[19]}"
        )
    except FileNotFoundError:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return "dead", None
        except (OSError, OverflowError):
            pass
        return "unknown", None
    except (OSError, UnicodeError):
        return "unknown", None


def _darwin_process_observation(pid: int) -> tuple[str, ProcessIdentity | None]:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "dead", None
    except PermissionError:
        pass
    except (OSError, OverflowError):
        return "unknown", None
    try:
        import ctypes

        class ProcBSDInfo(ctypes.Structure):
            _fields_ = [
                ("pbi_flags", ctypes.c_uint32),
                ("pbi_status", ctypes.c_uint32),
                ("pbi_xstatus", ctypes.c_uint32),
                ("pbi_pid", ctypes.c_uint32),
                ("pbi_ppid", ctypes.c_uint32),
                ("pbi_uid", ctypes.c_uint32),
                ("pbi_gid", ctypes.c_uint32),
                ("pbi_ruid", ctypes.c_uint32),
                ("pbi_rgid", ctypes.c_uint32),
                ("pbi_svuid", ctypes.c_uint32),
                ("pbi_svgid", ctypes.c_uint32),
                ("rfu_1", ctypes.c_uint32),
                ("pbi_comm", ctypes.c_char * 16),
                ("pbi_name", ctypes.c_char * 32),
                ("pbi_nfiles", ctypes.c_uint32),
                ("pbi_pgid", ctypes.c_uint32),
                ("pbi_pjobc", ctypes.c_uint32),
                ("e_tdev", ctypes.c_uint32),
                ("e_tpgid", ctypes.c_uint32),
                ("pbi_nice", ctypes.c_int32),
                ("pbi_start_tvsec", ctypes.c_uint64),
                ("pbi_start_tvusec", ctypes.c_uint64),
            ]

        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        libproc.proc_pidinfo.restype = ctypes.c_int
        libproc.proc_pidinfo.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        info = ProcBSDInfo()
        received = libproc.proc_pidinfo(
            pid,
            3,  # PROC_PIDTBSDINFO
            0,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if received != ctypes.sizeof(info):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return "dead", None
            except (OSError, OverflowError):
                pass
            return "unknown", None
        if info.pbi_pid != pid:
            return "unknown", None
        if info.pbi_status == 5:  # SZOMB
            return "dead", None
        if info.pbi_status not in {1, 2, 3, 4}:  # SIDL, SRUN, SSLEEP, SSTOP
            return "unknown", None
        seconds = int(info.pbi_start_tvsec)
        microseconds = int(info.pbi_start_tvusec)
        if seconds <= 0 or not 0 <= microseconds <= 999_999:
            return "unknown", None
        return "alive", ProcessIdentity(
            "darwin-proc-bsdinfo-v1", f"{seconds}:{microseconds}"
        )
    except Exception:  # noqa: BLE001 - authority ambiguity is UNKNOWN
        return "unknown", None


def _process_observation(pid: object) -> tuple[str, ProcessIdentity | None]:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return "unknown", None
    if os.name == "nt":
        if pid > _MAX_WINDOWS_PID:
            return "unknown", None
        return _windows_process_observation(pid)
    if sys_platform_linux():
        return _linux_process_observation(pid)
    if sys_platform_darwin():
        return _darwin_process_observation(pid)
    return "unknown", None


def sys_platform_linux() -> bool:
    """Keep the unsupported-platform refusal explicit and easy to test."""
    return sys.platform.startswith("linux")


def sys_platform_darwin() -> bool:
    """Return whether the native ``proc_pidinfo`` identity source is available."""
    return sys.platform == "darwin"


def process_identity(pid: int) -> ProcessIdentity | None:
    """Return an exact live-process identity, or ``None`` on any ambiguity."""
    status, identity = _process_observation(pid)
    return identity if status == "alive" else None


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _is_reparse(value: os.stat_result) -> bool:
    return bool(getattr(value, "st_file_attributes", 0) & 0x400)


def _validate_parent(path: Path) -> None:
    try:
        value = os.lstat(path)
    except OSError as exc:
        raise ValueError(f"lock parent is unavailable: {exc}") from exc
    if not stat.S_ISDIR(value.st_mode) or stat.S_ISLNK(value.st_mode) or _is_reparse(value):
        raise ValueError("lock parent must be a plain directory")


def _validate_artifact_stat(value: os.stat_result) -> None:
    if (
        not stat.S_ISREG(value.st_mode)
        or stat.S_ISLNK(value.st_mode)
        or _is_reparse(value)
        or value.st_nlink != 1
    ):
        raise ValueError("lock artifact must be a plain single-link regular file")


def _local_lock(path: Path) -> threading.Lock:
    key = os.path.normcase(os.path.abspath(path))
    with _LOCAL_LOCKS_GUARD:
        return _LOCAL_LOCKS.setdefault(key, threading.Lock())


def _try_os_lock(fd: int) -> bool:
    os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                return False
            raise
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def _unlock_os(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_UN)


class CrossProcessLifecycleLock:
    """One bounded, exact-owner lifecycle lock over a stable artifact."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        authority: str,
        timeout_seconds: float = 10.0,
        poll_seconds: float = 0.05,
    ) -> None:
        if _SAFE_LABEL.fullmatch(authority) is None:
            raise ValueError("lifecycle lock authority is invalid")
        if timeout_seconds < 0 or poll_seconds <= 0:
            raise ValueError("lifecycle lock timing bounds are invalid")
        self.path = Path(os.path.abspath(path))
        self.authority = authority
        self.timeout_seconds = float(timeout_seconds)
        self.poll_seconds = float(poll_seconds)

    def _unknown(self, detail: str) -> LifecycleLockUnknown:
        return LifecycleLockUnknown(self.path, detail)

    def _open_artifact(self) -> tuple[int, bool, os.stat_result]:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            _validate_parent(self.path.parent)
        except (OSError, ValueError) as exc:
            raise self._unknown(str(exc)) from exc
        flags = os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        created = False
        fd = -1
        try:
            fd = os.open(self.path, flags | nofollow | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
            os.ftruncate(fd, _ARTIFACT_BYTES)
            os.fsync(fd)
        except FileExistsError:
            try:
                before = os.lstat(self.path)
                _validate_artifact_stat(before)
                fd = os.open(self.path, flags | nofollow)
            except (OSError, ValueError) as exc:
                raise self._unknown(str(exc)) from exc
        except OSError as exc:
            if fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(fd)
            raise self._unknown(f"lock artifact could not be created: {exc}") from exc
        try:
            opened = os.fstat(fd)
            current = os.lstat(self.path)
            _validate_artifact_stat(opened)
            _validate_artifact_stat(current)
            if not _same_file(opened, current):
                raise ValueError("lock artifact pathname changed during open")
            return fd, created, opened
        except (OSError, ValueError) as exc:
            os.close(fd)
            raise self._unknown(str(exc)) from exc

    def _recheck_artifact(self, opened: os.stat_result) -> None:
        try:
            current = os.lstat(self.path)
            _validate_parent(self.path.parent)
            _validate_artifact_stat(current)
            if not _same_file(opened, current):
                raise ValueError("lock artifact pathname changed while held")
            if current.st_size != _ARTIFACT_BYTES:
                raise ValueError("lock artifact length is corrupt")
        except (OSError, ValueError) as exc:
            raise self._unknown(str(exc)) from exc

    def _read_record_fd(self, fd: int) -> dict:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            raw = bytearray()
            while len(raw) < _METADATA_BYTES:
                chunk = os.read(fd, _METADATA_BYTES - len(raw))
                if not chunk:
                    break
                raw.extend(chunk)
            return _decode_record(bytes(raw), self.authority)
        except (OSError, ValueError) as exc:
            raise self._unknown(str(exc)) from exc

    def _read_record_path(self) -> dict:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        try:
            before = os.lstat(self.path)
            _validate_artifact_stat(before)
            fd = os.open(self.path, flags | nofollow)
            try:
                opened = os.fstat(fd)
                _validate_artifact_stat(opened)
                if not _same_file(before, opened) or opened.st_size != _ARTIFACT_BYTES:
                    raise ValueError("lock artifact changed during observation")
                os.lseek(fd, 0, os.SEEK_SET)
                raw = bytearray()
                while len(raw) < _METADATA_BYTES:
                    chunk = os.read(fd, _METADATA_BYTES - len(raw))
                    if not chunk:
                        break
                    raw.extend(chunk)
            finally:
                os.close(fd)
            return _decode_record(bytes(raw), self.authority)
        except (OSError, ValueError) as exc:
            raise self._unknown(f"lock metadata is corrupt or unreadable: {exc}") from exc

    def _write_record_fd(self, fd: int, opened: os.stat_result, value: dict) -> None:
        self._recheck_artifact(opened)
        try:
            raw = _encode_record(value)
            os.lseek(fd, 0, os.SEEK_SET)
            written = 0
            while written < len(raw):
                count = os.write(fd, raw[written:])
                if count <= 0:
                    raise OSError("lock metadata write made no progress")
                written += count
            os.fsync(fd)
        except (OSError, ValueError) as exc:
            raise self._unknown(f"lock metadata write failed: {exc}") from exc
        self._recheck_artifact(opened)

    def _held_record(self, operation: str, identity: ProcessIdentity) -> dict:
        return {
            "authority": self.authority,
            "schema_version": _SCHEMA_VERSION,
            "state": "held",
            "generation": uuid.uuid4().hex,
            "pid": os.getpid(),
            "process_identity": {
                "scheme": identity.scheme,
                "value": identity.value,
            },
            "operation": operation,
            "acquired_at": _utc_now(),
        }

    def _classify_recorded_owner(self, record: dict) -> str:
        status, observed = _process_observation(record["pid"])
        if status == "dead":
            return "gone"
        if status != "alive" or observed is None:
            return "unknown"
        recorded = record["process_identity"]
        return (
            "same"
            if observed.scheme == recorded["scheme"] and observed.value == recorded["value"]
            else "reused"
        )

    def _raise_wait_outcome(self, record: dict | None, problem: str | None) -> None:
        if record is not None and record.get("state") == "held":
            classification = self._classify_recorded_owner(record)
            if classification == "same":
                raise LifecycleLockContended(record)
            if classification in {"gone", "reused"}:
                raise self._unknown(
                    "the OS lock remained occupied after its recorded exact holder was gone"
                )
            raise self._unknown("the recorded holder identity could not be observed exactly")
        if problem:
            raise self._unknown(problem)
        raise self._unknown("the OS lock is held without valid held-owner metadata")

    @contextlib.contextmanager
    def hold(self, operation: str) -> Iterator[dict]:
        """Acquire, identify, and durably release one lifecycle operation."""
        if _SAFE_LABEL.fullmatch(operation) is None:
            raise ValueError("lifecycle lock operation is invalid")
        self_identity = process_identity(os.getpid())
        if self_identity is None:
            raise self._unknown("the caller process identity could not be observed exactly")
        deadline = time.monotonic() + self.timeout_seconds
        local = _local_lock(self.path)
        if not local.acquire(timeout=max(0.0, deadline - time.monotonic())):
            try:
                record = self._read_record_path()
            except LifecycleLockUnknown as exc:
                self._raise_wait_outcome(None, exc.detail)
            self._raise_wait_outcome(record, None)
        fd = -1
        os_locked = False
        claim: dict | None = None
        try:
            fd, created, opened = self._open_artifact()
            last_record: dict | None = None
            last_problem: str | None = None
            while True:
                if not os_locked:
                    try:
                        os_locked = _try_os_lock(fd)
                    except OSError as exc:
                        raise self._unknown(
                            f"kernel lock acquisition failed: {exc}"
                        ) from exc
                if os_locked:
                    try:
                        self._recheck_artifact(opened)
                        if created:
                            self._write_record_fd(
                                fd, opened, _released_record(self.authority)
                            )
                            created = False
                        record = self._read_record_fd(fd)
                    except LifecycleLockUnknown as exc:
                        last_problem = exc.detail
                        try:
                            _unlock_os(fd)
                        except OSError as unlock_exc:
                            raise self._unknown(
                                f"kernel lock release failed: {unlock_exc}"
                            ) from unlock_exc
                        os_locked = False
                        if time.monotonic() >= deadline:
                            raise self._unknown(last_problem) from exc
                        time.sleep(
                            min(self.poll_seconds, max(0.0, deadline - time.monotonic()))
                        )
                        continue
                    if record["state"] == "held":
                        classification = self._classify_recorded_owner(record)
                        if classification == "same":
                            raise self._unknown(
                                "the kernel lock was free while its exact recorded holder was alive"
                            )
                        if classification == "unknown":
                            raise self._unknown(
                                "the abandoned holder identity could not be observed exactly"
                            )
                    claim = self._held_record(operation, self_identity)
                    self._write_record_fd(fd, opened, claim)
                    break
                try:
                    last_record = self._read_record_path()
                    last_problem = None
                except LifecycleLockUnknown as exc:
                    last_record = None
                    last_problem = exc.detail
                if time.monotonic() >= deadline:
                    # One final kernel attempt closes the release-at-deadline race.
                    try:
                        os_locked = _try_os_lock(fd)
                    except OSError as exc:
                        raise self._unknown(f"kernel lock acquisition failed: {exc}") from exc
                    if os_locked:
                        continue
                    try:
                        last_record = self._read_record_path()
                        last_problem = None
                    except LifecycleLockUnknown as exc:
                        last_record = None
                        last_problem = exc.detail
                    self._raise_wait_outcome(last_record, last_problem)
                time.sleep(
                    min(self.poll_seconds, max(0.0, deadline - time.monotonic()))
                )
            try:
                yield dict(claim)
            finally:
                release_error: BaseException | None = None
                try:
                    self._recheck_artifact(opened)
                    current = self._read_record_fd(fd)
                    if (
                        current.get("state") != "held"
                        or current.get("generation") != claim["generation"]
                    ):
                        raise self._unknown(
                            "lock authority changed before its holder could release it"
                        )
                    self._write_record_fd(fd, opened, _released_record(self.authority))
                except BaseException as exc:  # noqa: BLE001 - release must still unlock
                    release_error = exc
                try:
                    if os_locked:
                        _unlock_os(fd)
                        os_locked = False
                except OSError as exc:
                    release_error = release_error or self._unknown(
                        f"kernel lock release failed: {exc}"
                    )
                if release_error is not None:
                    raise release_error
        finally:
            if os_locked and fd >= 0:
                with contextlib.suppress(OSError):
                    _unlock_os(fd)
            if fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(fd)
            local.release()
