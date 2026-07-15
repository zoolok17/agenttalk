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
from pathlib import Path
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
    epoch_seconds = ticks / 10_000_000.0 - 11644473600
    return datetime.fromtimestamp(epoch_seconds, timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


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
        identity, image_handle = psh.open_stable_native_file_identity(buf.value)
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
    if not isinstance(locator, str) or not locator:
        return False
    # .NET's round-trip ("o") format uses seven fractional-second digits;
    # Python 3.10's fromisoformat accepts at most six.  The seventh digit is
    # below the precision used by datetime, so truncate it before comparing.
    def parse(value: str) -> datetime:
        normalized = value.replace("Z", "+00:00")
        match = re.fullmatch(
            r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\.(\d+)([+-]\d{2}:\d{2})",
            normalized,
        )
        if match:
            fraction = (match.group(2) + "000000")[:6]
            normalized = f"{match.group(1)}.{fraction}{match.group(3)}"
        return datetime.fromisoformat(normalized)

    try:
        left = parse(observed)
        right = parse(locator)
    except ValueError:
        return observed == locator
    return abs((left - right).total_seconds()) <= 0.01


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


def repair_invalid_instance_marker(store: Store) -> Path | None:
    return store.quarantine_invalid_supervisor_instance(
        reason="operator acknowledged no live supervisor"
    )
