"""Supervisor lifecycle orchestration for the selected PowerShell host.

The owning operations in this module acquire locks only in the global order
``supervisor lifecycle -> PowerShell selection -> config``.  The generic Store
claim remains host-agnostic for supported Python-only executor paths; generated
PowerShell supervisors use :func:`claim_powershell_supervisor`.
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import os
import re
import tempfile
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Callable, Iterator, Mapping

from agenttalk import powershell_host as psh
from agenttalk.store import Store, _owner_identity_gone


class SupervisorLifecycleError(RuntimeError):
    """A lifecycle/selection operation was refused without mutation."""


@dataclass
class ProcessObservation:
    pid: int
    parent_pid: int
    path: str
    creation_token: str
    creation_ticks: int
    identity: psh.NativeFileIdentity
    handle: int
    image_handle: int = 0


def selection_path(store: Store) -> Path:
    return store.dir / psh.SELECTION_FILENAME


def _atomic_write_selection(path: Path, record: Mapping[str, object]) -> None:
    """Publish a complete selection in the destination directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(
        psh.selection_public_view(record),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n").encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _read_selection_bytes(path: Path) -> object:
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise SupervisorLifecycleError(
            f"no PowerShell host is selected; {psh.INSTALL_REMEDIATION}"
        ) from exc
    except OSError as exc:
        raise SupervisorLifecycleError(
            f"PowerShell host selection is unreadable: {exc}; {psh.INSTALL_REMEDIATION}"
        ) from exc
    if not raw or len(raw) > 64 * 1024 or raw.startswith(b"\xef\xbb\xbf"):
        raise SupervisorLifecycleError(
            f"PowerShell host selection is malformed; {psh.INSTALL_REMEDIATION}"
        )
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise SupervisorLifecycleError(
            f"PowerShell host selection is malformed: {exc}; {psh.INSTALL_REMEDIATION}"
        ) from exc


def _result_matches_record(result: psh.ProbeResult, record: Mapping[str, object]) -> bool:
    version = record.get("_version")
    identity = record.get("_identity")
    return (
        isinstance(version, psh.PowerShellVersion)
        and isinstance(identity, psh.NativeFileIdentity)
        and psh.normalized_path_key(result.path) == psh.normalized_path_key(str(record.get("path")))
        and result.source == record.get("source")
        and result.edition == record.get("edition")
        and result.version == version
        and psh.same_identity(result.identity, identity)
    )


def _read_valid_selection_locked(
    store: Store,
    *,
    now: float | None = None,
    permit_expired_probe: bool = True,
) -> dict:
    value = _read_selection_bytes(selection_path(store))
    stamp = datetime.now(timezone.utc).timestamp() if now is None else now
    try:
        record = psh.validate_selection_record(value, project_id=store.project_id(), now=stamp)
    except psh.PowerShellHostError as exc:
        raise SupervisorLifecycleError(
            f"PowerShell host selection is invalid: {exc}; {psh.INSTALL_REMEDIATION}"
        ) from exc
    identity = record["_identity"]
    try:
        current_identity = psh.native_file_identity(str(record["path"]))
    except (OSError, ValueError) as exc:
        raise SupervisorLifecycleError(
            f"selected PowerShell host is inaccessible: {exc}; "
            f"{psh.explicit_select_remediation(str(record['path']))}"
        ) from exc
    if not psh.same_identity(identity, current_identity):
        raise SupervisorLifecycleError(
            "selected PowerShell host identity changed; "
            f"{psh.explicit_select_remediation(str(record['path']))}"
        )
    if record["_expired"]:
        if not permit_expired_probe:
            raise SupervisorLifecycleError("selected PowerShell probe cache expired")
        try:
            reprobed = psh.probe_candidate(
                str(record["path"]), source=str(record["source"])
            )
        except psh.PowerShellHostError as exc:
            raise SupervisorLifecycleError(
                f"selected PowerShell host revalidation failed: {exc}; "
                f"{psh.explicit_select_remediation(str(record['path']))}"
            ) from exc
        if not _result_matches_record(reprobed, record):
            raise SupervisorLifecycleError(
                "selected PowerShell host changed during TTL revalidation; "
                f"{psh.explicit_select_remediation(str(record['path']))}"
            )
    return record


def read_selected_host(store: Store, *, now: float | None = None) -> dict:
    """Validate the durable selection and native identity under its read lock."""
    with store._powershell_selection_lock():
        return _read_valid_selection_locked(store, now=now)


def read_selected_host_locked(store: Store, *, now: float | None = None) -> dict:
    """Locked primitive for callers that linearize validation with a spawn."""
    return _read_valid_selection_locked(store, now=now)


@contextlib.contextmanager
def selected_host_for_spawn(
    store: Store,
    *,
    now: float | None = None,
) -> Iterator[dict]:
    """Linearize final selected-host identity validation with one spawn."""
    with store._powershell_selection_lock():
        record = _read_valid_selection_locked(store, now=now)
        yield record


def _strict_marker_allows_mutation_locked(store: Store) -> None:
    status, record, detail = store._read_supervisor_instance_strict_locked()
    if status == "invalid":
        raise SupervisorLifecycleError(
            "supervisor instance marker is invalid or unreadable; run "
            "`agenttalk supervise --repair-instance-marker --quarantine "
            "--acknowledge-no-live-supervisor`"
        )
    if status == "absent":
        return
    if record is None:
        raise SupervisorLifecycleError(
            "supervisor instance marker validation returned no owner record"
        )
    if not _owner_identity_gone(record.get("pid"), record.get("pid_start")):
        raise SupervisorLifecycleError(
            "a live or unqueryable supervisor owns this project; stop it, wait until "
            "the process exits, then retry"
        )
    store._quarantine_supervisor_instance_locked(
        reason=f"confirmed stale owner before lifecycle mutation ({detail or 'valid marker'})"
    )


def assert_lifecycle_mutation_allowed_locked(store: Store) -> None:
    """Public locked primitive used by selection and artifact refresh owners."""
    _strict_marker_allows_mutation_locked(store)


def select_powershell_host(
    store: Store,
    *,
    explicit_path: str | None = None,
    current_pid: int | None = None,
    current_pid_start: object = None,
    task_name: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[dict, tuple[psh.CandidateAttempt, ...]]:
    """Probe one terminal explicit/current candidate or automatic Program Files.

    Probing happens before the writer locks.  Publication rechecks the exact
    native identity while lifecycle+selection are held; no failed request writes.
    """
    if os.name != "nt":
        raise SupervisorLifecycleError("PowerShell host selection is Windows-only")
    if explicit_path is not None and current_pid is not None:
        raise SupervisorLifecycleError("--pwsh and current-host selection are mutually exclusive")
    current_observation: ProcessObservation | None = None
    try:
        if current_pid is not None:
            current_observation = _open_process_observation(current_pid)
            if not start_tokens_match(current_observation.creation_token, current_pid_start):
                raise SupervisorLifecycleError("current PowerShell pid/start locator was reused or ambiguous")
            resolution = psh.resolve_candidate(current_path=current_observation.path)
        else:
            resolution = psh.resolve_candidate(explicit_path=explicit_path, environ=environ)
        if resolution.result is None:
            reason = resolution.attempts[-1].reason if resolution.attempts else "no trusted candidate found"
            suggestions = psh.path_candidate_remediations(environ)
            hint = f"; try `{suggestions[0]}`" if suggestions else f"; {psh.INSTALL_REMEDIATION}"
            raise SupervisorLifecycleError(f"PowerShell Core host selection failed: {reason}{hint}")
        result = resolution.result
        if current_observation is not None:
            if not psh.same_identity(result.identity, current_observation.identity):
                raise SupervisorLifecycleError("current PowerShell process image changed during probe")
            _require_process_active(current_observation)
        with store._supervisor_lifecycle_lock():
            _strict_marker_allows_mutation_locked(store)
            with store._powershell_selection_lock():
                try:
                    final_identity = psh.native_file_identity(result.path)
                except (OSError, ValueError) as exc:
                    raise SupervisorLifecycleError(
                        f"PowerShell host changed before selection write: {exc}"
                    ) from exc
                if not psh.same_identity(result.identity, final_identity):
                    raise SupervisorLifecycleError(
                        "PowerShell host identity changed before selection write"
                    )
                if current_observation is not None:
                    _require_process_active(current_observation)
                previous: Mapping[str, object] | None = None
                existing_path = selection_path(store)
                if existing_path.exists():
                    try:
                        existing_value = _read_selection_bytes(existing_path)
                        previous = psh.validate_selection_record(
                            existing_value, project_id=store.project_id()
                        )
                    except (psh.PowerShellHostError, SupervisorLifecycleError):
                        # This is an explicit writer operation under both locks.
                        # The freshly probed result repairs an invalid record;
                        # consumers still reject it until this replace commits.
                        previous = None
                record = psh.make_selection_record(
                    result,
                    project_id=store.project_id(),
                    previous=previous,
                    task_name=task_name if task_name is not None else (
                        previous.get("task_name") if previous else None
                    ),
                )
                _atomic_write_selection(selection_path(store), record)
                return psh.selection_public_view(record), resolution.attempts
    finally:
        if current_observation is not None:
            _close_process_observation(current_observation)


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


def _process_parent_map() -> dict[int, int]:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    invalid = wintypes.HANDLE(-1).value
    if snapshot == invalid:
        raise SupervisorLifecycleError("could not snapshot process ancestry")
    parents: dict[int, int] = {}
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return parents


def _filetime_ticks(value: wintypes.FILETIME) -> int:
    return (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)


def _ticks_to_token(ticks: int) -> str:
    unix_ticks = ticks - 116_444_736_000_000_000
    seconds, fraction = divmod(unix_ticks, 10_000_000)
    stamp = datetime.fromtimestamp(seconds, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    return f"{stamp}.{fraction:07d}Z"


def _open_process_observation(pid: int, *, parents: Mapping[int, int] | None = None) -> ProcessObservation:
    if os.name != "nt" or not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise SupervisorLifecycleError("PowerShell process observation is Windows-only")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        raise SupervisorLifecycleError(
            f"cannot query process {pid} (winerror {ctypes.get_last_error()})"
        )
    image_handle = 0
    try:
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
            raise SupervisorLifecycleError(f"cannot query process {pid} creation time")
        capacity = wintypes.DWORD(32768)
        buf = ctypes.create_unicode_buffer(capacity.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(capacity)):
            raise SupervisorLifecycleError(f"cannot query process {pid} image path")
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:
            raise SupervisorLifecycleError(f"process {pid} is not active")
        identity, image_handle = _open_stable_process_image_identity(
            buf.value,
            int(handle),
        )
        parent_map = parents if parents is not None else _process_parent_map()
        ticks = _filetime_ticks(creation)
        return ProcessObservation(
            pid=pid,
            parent_pid=int(parent_map.get(pid, 0)),
            path=identity.final_path,
            creation_token=_ticks_to_token(ticks),
            creation_ticks=ticks,
            identity=identity,
            handle=int(handle),
            image_handle=image_handle,
        )
    except Exception:
        psh.close_native_file_handle(image_handle)
        kernel32.CloseHandle(handle)
        raise


def _close_process_observation(observation: ProcessObservation) -> None:
    if observation.image_handle:
        psh.close_native_file_handle(observation.image_handle)
        observation.image_handle = 0
    if observation.handle:
        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(
            wintypes.HANDLE(observation.handle)
        )
        observation.handle = 0


def _require_process_active(observation: ProcessObservation) -> None:
    code = wintypes.DWORD()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if not observation.handle or not kernel32.GetExitCodeProcess(
        wintypes.HANDLE(observation.handle), ctypes.byref(code)
    ) or code.value != 259:
        raise SupervisorLifecycleError(f"process {observation.pid} exited during validation")
    creation = wintypes.FILETIME()
    exit_time = wintypes.FILETIME()
    kernel = wintypes.FILETIME()
    user = wintypes.FILETIME()
    if not kernel32.GetProcessTimes(
        wintypes.HANDLE(observation.handle),
        ctypes.byref(creation), ctypes.byref(exit_time), ctypes.byref(kernel), ctypes.byref(user),
    ) or _filetime_ticks(creation) != observation.creation_ticks:
        raise SupervisorLifecycleError(f"process {observation.pid} identity changed")


def start_tokens_match(observed: str, locator: object) -> bool:
    left = _start_token_filetime(observed)
    right = _start_token_filetime(locator)
    return left is not None and left == right


def _start_token_filetime(value: object) -> int | None:
    """Parse one exact .NET round-trip process-start token to FILETIME ticks."""
    if not isinstance(value, str):
        return None
    match = re.fullmatch(
        r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"
        r"(?:\.(\d{1,7}))?(Z|[+-]\d{2}:\d{2})",
        value,
    )
    if match is None:
        return None
    prefix, fraction, zone = match.groups()
    try:
        stamp = datetime.fromisoformat(
            prefix + ("+00:00" if zone == "Z" else zone)
        ).astimezone(timezone.utc)
    except (OverflowError, ValueError):
        return None
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = stamp - epoch
    seconds = delta.days * 86_400 + delta.seconds
    fractional_ticks = int((fraction or "0").ljust(7, "0"))
    return 116_444_736_000_000_000 + seconds * 10_000_000 + fractional_ticks


def _system_cmd_path() -> str:
    """Return the native system-directory command interpreter."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_system_directory = kernel32.GetSystemDirectoryW
    get_system_directory.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    get_system_directory.restype = wintypes.UINT
    capacity = 32768
    buf = ctypes.create_unicode_buffer(capacity)
    length = int(get_system_directory(buf, capacity))
    if length <= 0 or length >= capacity:
        raise SupervisorLifecycleError("cannot resolve the Windows system directory")
    return str(Path(buf.value) / "cmd.exe")


def _process_machines(process_handle: int) -> tuple[int, int]:
    """Return the emulated and native PE machines for one live process."""
    if not process_handle:
        raise SupervisorLifecycleError("cannot identify process architecture")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    try:
        is_wow64_process2 = kernel32.IsWow64Process2
    except AttributeError:
        try:
            is_wow64_process = kernel32.IsWow64Process
        except AttributeError as exc:
            raise SupervisorLifecycleError(
                "cannot resolve the Windows process-architecture API"
            ) from exc
        is_wow64_process.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.BOOL),
        ]
        is_wow64_process.restype = wintypes.BOOL
        emulated = wintypes.BOOL()
        if not is_wow64_process(
            wintypes.HANDLE(process_handle),
            ctypes.byref(emulated),
        ):
            raise SupervisorLifecycleError(
                "cannot identify process architecture "
                f"(winerror {ctypes.get_last_error()})"
            ) from None
        return (0x014C if emulated.value else 0), 0
    is_wow64_process2.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.WORD),
        ctypes.POINTER(wintypes.WORD),
    ]
    is_wow64_process2.restype = wintypes.BOOL
    process_machine = wintypes.WORD()
    native_machine = wintypes.WORD()
    if not is_wow64_process2(
        wintypes.HANDLE(process_handle),
        ctypes.byref(process_machine),
        ctypes.byref(native_machine),
    ):
        raise SupervisorLifecycleError(
            "cannot identify process architecture "
            f"(winerror {ctypes.get_last_error()})"
        )
    return int(process_machine.value), int(native_machine.value)


def _wow64_system_directory(machine: int) -> str:
    """Return the OS-owned system directory for one emulated PE machine."""
    library = None
    function = None
    for dll_name in (
        "api-ms-win-core-wow64-l1-1-1.dll",
        "kernelbase.dll",
        "kernel32.dll",
    ):
        try:
            candidate = ctypes.WinDLL(dll_name, use_last_error=True)
            resolved = candidate.GetSystemWow64Directory2W
        except (AttributeError, OSError):
            continue
        library = candidate
        function = resolved
        break
    capacity = 32767
    if library is not None and function is not None:
        function.argtypes = [wintypes.LPWSTR, wintypes.UINT, wintypes.WORD]
        function.restype = wintypes.UINT
        buf = ctypes.create_unicode_buffer(capacity)
        length = int(function(buf, capacity, machine))
        if 0 < length < capacity:
            return buf.value
    if machine != 0x014C:
        raise SupervisorLifecycleError(
            "cannot resolve the Windows emulated system directory"
        )
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    try:
        legacy = kernel32.GetSystemWow64DirectoryW
    except AttributeError as exc:
        raise SupervisorLifecycleError(
            "cannot resolve the Windows emulated-system-directory API"
        ) from exc
    legacy.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    legacy.restype = wintypes.UINT
    buf = ctypes.create_unicode_buffer(capacity)
    length = int(legacy(buf, capacity))
    if length <= 0 or length >= capacity:
        raise SupervisorLifecycleError(
            "cannot resolve the Windows emulated system directory"
        )
    return buf.value


def _windows_directory() -> str:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_windows_directory = kernel32.GetWindowsDirectoryW
    get_windows_directory.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    get_windows_directory.restype = wintypes.UINT
    capacity = 32767
    buf = ctypes.create_unicode_buffer(capacity)
    length = int(get_windows_directory(buf, capacity))
    if length <= 0 or length >= capacity:
        raise SupervisorLifecycleError("cannot resolve the Windows directory")
    return buf.value


def _current_process_handle() -> int:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_current_process = kernel32.GetCurrentProcess
    get_current_process.argtypes = []
    get_current_process.restype = wintypes.HANDLE
    return int(get_current_process())


def _process_image_path_for_reopen(image_path: str, process_handle: int) -> str:
    """Map a native System32 image through Sysnative when Python is WOW64."""
    process_machine, _ = _process_machines(process_handle)
    current_machine, _ = _process_machines(_current_process_handle())
    if process_machine or not current_machine:
        return image_path
    windows = PureWindowsPath(_windows_directory())
    system = windows / "System32"
    image = PureWindowsPath(image_path)
    system_parts = tuple(part.casefold() for part in system.parts)
    image_parts = tuple(part.casefold() for part in image.parts)
    if image_parts[:len(system_parts)] != system_parts:
        return image_path
    relative = image.parts[len(system_parts):]
    if not relative:
        raise SupervisorLifecycleError("process image path is not a file")
    return str(windows / "Sysnative" / PureWindowsPath(*relative))


def _open_stable_process_image_identity(
    image_path: str,
    process_handle: int,
) -> tuple[psh.NativeFileIdentity, int]:
    return psh.open_stable_native_file_identity(
        _process_image_path_for_reopen(image_path, process_handle)
    )


def _system_cmd_path_for_observation(observation: ProcessObservation) -> str:
    """Resolve the OS-owned ``cmd.exe`` for the observed process architecture."""
    if not observation.handle:
        return _system_cmd_path()
    process_machine, _ = _process_machines(observation.handle)
    if process_machine:
        return str(Path(_wow64_system_directory(process_machine)) / "cmd.exe")
    current_machine, _ = _process_machines(_current_process_handle())
    if current_machine:
        return str(Path(_windows_directory()) / "Sysnative" / "cmd.exe")
    return _system_cmd_path()


def _require_observed_image(
    observation: ProcessObservation,
    expected_path: str,
    *,
    subject: str,
) -> None:
    try:
        expected = psh.native_file_identity(expected_path)
    except (OSError, ValueError, psh.PowerShellHostError) as exc:
        raise SupervisorLifecycleError(f"cannot identify {subject}: {exc}") from exc
    if not psh.same_identity(observation.identity, expected):
        raise SupervisorLifecycleError(f"{subject} image identity does not match")


def _validate_ancestry(host: ProcessObservation) -> tuple[ProcessObservation, ...]:
    parents = _process_parent_map()
    current = _open_process_observation(os.getpid(), parents=parents)
    opened: list[ProcessObservation] = [current]
    try:
        if current.parent_pid == host.pid:
            if host.creation_ticks > current.creation_ticks:
                raise SupervisorLifecycleError("PowerShell ancestor started after Python")
            return tuple(opened)
        if current.parent_pid <= 0:
            raise SupervisorLifecycleError("manual --claim-instance has no PowerShell ancestor")
        cmd = _open_process_observation(current.parent_pid, parents=parents)
        opened.append(cmd)
        if Path(cmd.path).name.casefold() != "cmd.exe" or cmd.parent_pid != host.pid:
            raise SupervisorLifecycleError(
                "manual --claim-instance is unsupported without a direct or cmd-hop PowerShell ancestor"
            )
        _require_observed_image(
            cmd,
            _system_cmd_path_for_observation(cmd),
            subject="command-interpreter ancestor",
        )
        if not (host.creation_ticks <= cmd.creation_ticks <= current.creation_ticks):
            raise SupervisorLifecycleError("PowerShell/cmd/Python ancestry start times are inconsistent")
        return tuple(opened)
    except Exception:
        for observation in opened:
            _close_process_observation(observation)
        raise


def _host_matches_selection(host: ProcessObservation, record: Mapping[str, object]) -> bool:
    identity = record.get("_identity")
    return (
        isinstance(identity, psh.NativeFileIdentity)
        and psh.normalized_path_key(host.path) == psh.normalized_path_key(str(record.get("path")))
        and psh.same_identity(host.identity, identity)
    )


def _revalidate_process_image(
    host: ProcessObservation,
    record: Mapping[str, object],
) -> None:
    try:
        current = psh.native_file_identity(host.path)
    except (OSError, ValueError) as exc:
        raise SupervisorLifecycleError(
            f"PowerShell process image became inaccessible: {exc}"
        ) from exc
    selected = record.get("_identity")
    if (
        not isinstance(selected, psh.NativeFileIdentity)
        or not psh.same_identity(host.identity, current)
        or not psh.same_identity(current, selected)
        or psh.normalized_path_key(current.final_path)
        != psh.normalized_path_key(str(record.get("path")))
    ):
        raise SupervisorLifecycleError(
            "PowerShell process image identity changed during supervisor claim"
        )


def validate_current_powershell(
    store: Store,
    *,
    pid: int,
    pid_start: object,
) -> dict:
    """Validate a generated script's actual host against the current selection."""
    host = _open_process_observation(pid)
    try:
        if not start_tokens_match(host.creation_token, pid_start):
            raise SupervisorLifecycleError("PowerShell pid/start locator was reused or ambiguous")
        with store._powershell_selection_lock():
            record = _read_valid_selection_locked(store)
            if not _host_matches_selection(host, record):
                raise SupervisorLifecycleError(
                    "this script is running under a different PowerShell host than the project selection"
                )
            _require_process_active(host)
            return record
    finally:
        _close_process_observation(host)


def _probe_observed_current_host(host: ProcessObservation) -> psh.ProbeResult:
    resolution = psh.resolve_candidate(current_path=host.path)
    if resolution.result is None:
        reason = resolution.attempts[-1].reason if resolution.attempts else "probe failed"
        raise SupervisorLifecycleError(f"current PowerShell host validation failed: {reason}")
    if not psh.same_identity(resolution.result.identity, host.identity):
        raise SupervisorLifecycleError("current PowerShell process image changed during probe")
    _require_process_active(host)
    return resolution.result


def prepare_task_install(
    store: Store,
    *,
    pid: int,
    pid_start: object,
    task_name: str,
    validate_artifacts: Callable[[], None],
) -> dict:
    """Read-only first phase for task registration under the current Core host."""
    host = _open_process_observation(pid)
    try:
        if not start_tokens_match(host.creation_token, pid_start):
            raise SupervisorLifecycleError("current PowerShell pid/start locator was reused")
        result = _probe_observed_current_host(host)
        with store._supervisor_lifecycle_lock():
            validate_artifacts()
            _strict_marker_allows_mutation_locked(store)
            with store._powershell_selection_lock():
                selection_file = selection_path(store)
                if selection_file.exists():
                    try:
                        existing = psh.validate_selection_record(
                            _read_selection_bytes(selection_file),
                            project_id=store.project_id(),
                        )
                    except psh.PowerShellHostError as exc:
                        raise SupervisorLifecycleError(
                            f"existing PowerShell selection is invalid: {exc}"
                        ) from exc
                    identity = existing["_identity"]
                    if (
                        psh.normalized_path_key(str(existing["path"]))
                        != psh.normalized_path_key(result.path)
                        or not psh.same_identity(identity, result.identity)
                    ):
                        raise SupervisorLifecycleError(
                            "task install cannot replace a different PowerShell selection; "
                            f"{psh.explicit_select_remediation(result.path)} first"
                        )
                    existing_task_name = existing.get("task_name")
                    if existing_task_name not in {None, task_name}:
                        raise SupervisorLifecycleError(
                            "task install found a different Scheduled Task binding "
                            f"{existing_task_name!r}; stop and uninstall it before "
                            f"installing {task_name!r}"
                        )
                    revision = existing["selection_revision"]
                    fingerprint = existing["selection_fingerprint"]
                    source = str(existing["source"])
                else:
                    revision = 0
                    fingerprint = "absent"
                    source = "current_host"
                _require_process_active(host)
                return {
                    "path": result.path,
                    "source": source,
                    "version": result.version.to_dict(),
                    "edition": result.edition,
                    "warning": result.warning,
                    "selection_revision": revision,
                    "selection_fingerprint": fingerprint,
                    "task_name": task_name,
                }
    finally:
        _close_process_observation(host)


def commit_task_install(
    store: Store,
    *,
    pid: int,
    pid_start: object,
    task_name: str,
    expected_revision: int,
    expected_fingerprint: str,
    validate_artifacts: Callable[[], None],
) -> dict:
    """Commit task binding metadata only after Scheduled Task registration."""
    host = _open_process_observation(pid)
    try:
        if not start_tokens_match(host.creation_token, pid_start):
            raise SupervisorLifecycleError("current PowerShell pid/start locator was reused")
        result = _probe_observed_current_host(host)
        with store._supervisor_lifecycle_lock():
            validate_artifacts()
            _strict_marker_allows_mutation_locked(store)
            with store._powershell_selection_lock():
                selection_file = selection_path(store)
                previous: Mapping[str, object] | None = None
                if selection_file.exists():
                    try:
                        previous = psh.validate_selection_record(
                            _read_selection_bytes(selection_file),
                            project_id=store.project_id(),
                        )
                    except psh.PowerShellHostError as exc:
                        raise SupervisorLifecycleError(
                            f"existing PowerShell selection is invalid: {exc}"
                        ) from exc
                    if (
                        expected_revision < 1
                        or previous["selection_revision"] != expected_revision
                        or previous["selection_fingerprint"] != expected_fingerprint
                    ):
                        raise SupervisorLifecycleError(
                            "PowerShell host selection changed during task registration"
                        )
                    identity = previous["_identity"]
                    if (
                        psh.normalized_path_key(str(previous["path"]))
                        != psh.normalized_path_key(result.path)
                        or not psh.same_identity(identity, result.identity)
                    ):
                        raise SupervisorLifecycleError(
                            "registered task host no longer matches the project selection"
                        )
                    previous_task_name = previous.get("task_name")
                    if previous_task_name not in {None, task_name}:
                        raise SupervisorLifecycleError(
                            "PowerShell task binding changed during task registration"
                        )
                    result = psh.ProbeResult(
                        result.path,
                        str(previous["source"]),
                        result.edition,
                        result.version,
                        result.identity,
                    )
                elif expected_revision != 0 or expected_fingerprint != "absent":
                    raise SupervisorLifecycleError(
                        "PowerShell host selection disappeared during task registration"
                    )
                _require_process_active(host)
                record = psh.make_selection_record(
                    result,
                    project_id=store.project_id(),
                    previous=previous,
                    task_name=task_name,
                )
                _atomic_write_selection(selection_file, record)
                post = _read_valid_selection_locked(store)
                if post.get("task_name") != task_name:
                    raise SupervisorLifecycleError("task binding post-check failed")
                return psh.selection_public_view(post)
    finally:
        _close_process_observation(host)


def clear_task_binding(store: Store, *, task_name: str) -> dict:
    """Clear exactly the binding an operator has explicitly uninstalled."""
    with store._supervisor_lifecycle_lock():
        _strict_marker_allows_mutation_locked(store)
        with store._powershell_selection_lock():
            record = _read_valid_selection_locked(store)
            existing = record.get("task_name")
            if existing is None:
                return psh.selection_public_view(record)
            if existing != task_name:
                raise SupervisorLifecycleError(
                    f"cannot clear Scheduled Task binding {task_name!r}; "
                    f"the selection records {existing!r}"
                )
            updated = psh.with_task_binding(record, None)
            _atomic_write_selection(selection_path(store), updated)
            return updated


def claim_powershell_supervisor(
    store: Store,
    *,
    pid: int,
    pid_start: object,
    validate_artifacts: Callable[[], None],
) -> dict | None:
    """Atomically authorize the real PowerShell ancestor and claim the marker."""
    with store._supervisor_lifecycle_lock():
        validate_artifacts()
        marker_status, _marker, marker_detail = (
            store._read_supervisor_instance_strict_locked()
        )
        if marker_status == "invalid":
            raise SupervisorLifecycleError(
                "supervisor instance marker is invalid or unreadable "
                f"({marker_detail or 'unknown error'}); run "
                "`agenttalk supervise --repair-instance-marker --quarantine "
                "--acknowledge-no-live-supervisor`"
            )
        with store._powershell_selection_lock():
            first = _read_valid_selection_locked(store)
            host = _open_process_observation(pid)
            ancestry: tuple[ProcessObservation, ...] = ()
            try:
                if not start_tokens_match(host.creation_token, pid_start):
                    raise SupervisorLifecycleError(
                        "PowerShell pid/start locator was reused or ambiguous"
                    )
                if not _host_matches_selection(host, first):
                    raise SupervisorLifecycleError(
                        "claiming PowerShell process does not match the current host selection"
                    )
                ancestry = _validate_ancestry(host)
                for observation in ancestry:
                    _require_process_active(observation)
                _require_process_active(host)
                second = _read_valid_selection_locked(store)
                if (
                    second["selection_revision"] != first["selection_revision"]
                    or second["selection_fingerprint"] != first["selection_fingerprint"]
                    or not _host_matches_selection(host, second)
                ):
                    raise SupervisorLifecycleError(
                        "PowerShell host selection changed during supervisor claim"
                    )
                with store._config_lock():
                    for observation in ancestry:
                        _require_process_active(observation)
                    _require_process_active(host)
                    final = _read_valid_selection_locked(store)
                    if (
                        final["selection_revision"] != second["selection_revision"]
                        or final["selection_fingerprint"] != second["selection_fingerprint"]
                        or not _host_matches_selection(host, final)
                    ):
                        raise SupervisorLifecycleError(
                            "PowerShell host selection changed before supervisor marker write"
                        )
                    _revalidate_process_image(host, final)
                    _require_process_active(host)
                    return store._claim_supervisor_instance_locked(
                        pid=pid,
                        pid_start=pid_start,
                    )
            finally:
                for observation in ancestry:
                    _close_process_observation(observation)
                _close_process_observation(host)


@contextlib.contextmanager
def authorized_executable_poll(
    store: Store,
    *,
    instance_token: object,
) -> Iterator[Callable[[], None]]:
    """Hold one executable poll under the exact live PowerShell owner.

    The marker token correlates the request with the current instance; it is not
    host authentication. Authority comes from OS observations: the marker's
    owner must still be the selected PowerShell process, and this Python process
    must be its direct child or its exact OS-owned ``cmd.exe`` hop. The handles
    and locks remain held through plan serialization and event publication. The
    yielded callback rechecks every live identity immediately before anything is
    published.
    """
    with store._supervisor_lifecycle_lock():
        status, marker, detail = store._read_supervisor_instance_strict_locked()
        if status != "valid" or not isinstance(marker, dict):
            raise SupervisorLifecycleError(
                "executable poll requires a valid live supervisor instance marker"
                + (f" ({detail})" if detail else "")
            )
        token = marker.get("token")
        if (
            not isinstance(instance_token, str)
            or not isinstance(token, str)
            or instance_token != token
        ):
            raise SupervisorLifecycleError(
                "executable poll token does not match the current supervisor instance"
            )
        pid = marker.get("pid")
        pid_start = marker.get("pid_start")
        if (
            not isinstance(pid, int)
            or isinstance(pid, bool)
            or pid <= 0
            or not isinstance(pid_start, str)
            or not pid_start
        ):
            raise SupervisorLifecycleError(
                "executable poll owner lacks an exact process identity"
            )
        with store._powershell_selection_lock():
            selected = _read_valid_selection_locked(store)
            host = _open_process_observation(pid)
            ancestry: tuple[ProcessObservation, ...] = ()
            try:
                if not start_tokens_match(host.creation_token, pid_start):
                    raise SupervisorLifecycleError(
                        "executable poll owner pid/start was reused or ambiguous"
                    )
                if not _host_matches_selection(host, selected):
                    raise SupervisorLifecycleError(
                        "executable poll owner is not the selected PowerShell host"
                    )
                try:
                    ancestry = _validate_ancestry(host)
                except SupervisorLifecycleError as exc:
                    raise SupervisorLifecycleError(
                        "executable poll caller is unrelated to the live supervisor host: "
                        f"{exc}"
                    ) from exc
                for observation in ancestry:
                    _require_process_active(observation)
                _require_process_active(host)
                _revalidate_process_image(host, selected)

                def recheck_before_publication() -> None:
                    for observation in ancestry:
                        _require_process_active(observation)
                    _require_process_active(host)
                    _revalidate_process_image(host, selected)

                yield recheck_before_publication
            finally:
                for observation in ancestry:
                    _close_process_observation(observation)
                _close_process_observation(host)


def repair_invalid_instance_marker(store: Store) -> Path | None:
    return store.quarantine_invalid_supervisor_instance(
        reason="operator acknowledged no live supervisor"
    )
