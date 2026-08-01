"""PowerShell Core host policy, probing, and durable selection records.

This module is deliberately stdlib-only.  It treats a selected executable as a
same-user consistency control, not as signer, ACL, mapped-image, or DLL-tree
attestation.  Callers that combine selection with supervisor lifecycle state own
the cross-process lock order in :mod:`agenttalk.supervisor_lifecycle`.
"""

from __future__ import annotations

import contextlib
import ctypes
import hashlib
import json
import math
import os
import re
import subprocess  # nosec B404 - argv-only executable probe
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping

HARD_MIN_MAJOR = 7
RECOMMENDED_STABLE_MAJOR = 7
RECOMMENDED_STABLE_MINOR = 4
SELECTION_SCHEMA = "agenttalk.powershell-host.v1"
SELECTION_FINGERPRINT_DOMAIN = "agenttalk-powershell-selection-v1"
SELECTION_TTL_SECONDS = 24 * 60 * 60
MAX_FUTURE_SKEW_SECONDS = 5 * 60
PROBE_TIMEOUT_SECONDS = 5.0
PROBE_SENTINEL = "AGENTTALK_PWSH_PROBE_V1:"
SELECTION_FILENAME = "powershell-host.json"

INSTALL_REMEDIATION = (
    "install PowerShell Core 7+ (pwsh) from https://aka.ms/powershell, then run "
    "`agenttalk supervise --select-pwsh`"
)
REFRESH_REMEDIATION = "run `agenttalk supervise --refresh-scripts`"

_PROBE_COMMAND = (
    "$ErrorActionPreference='Stop';"
    "$v=$PSVersionTable.PSVersion;"
    "$pre=$null;"
    "if($v.PSObject.Properties.Name -contains 'PreReleaseLabel'){"
    "$pre=$v.PreReleaseLabel};"
    "$o=[ordered]@{sentinel='agenttalk-pwsh-probe-v1';"
    "edition=[string]$PSEdition;major=[int]$v.Major;minor=[int]$v.Minor;"
    "patch=[int]$v.Patch;pre_release=$pre};"
    f"Write-Output ('{PROBE_SENTINEL}'+($o|ConvertTo-Json -Compress))"
)


class PowerShellHostError(ValueError):
    """A candidate or durable selection could not be trusted for this use."""


def _noop() -> None:
    return None


@dataclass(frozen=True)
class PowerShellVersion:
    major: int
    minor: int
    patch: int
    pre_release: str | None = None

    def to_dict(self) -> dict:
        return {
            "major": self.major,
            "minor": self.minor,
            "patch": self.patch,
            "pre_release": self.pre_release,
        }

    @property
    def display(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        return f"{base}-{self.pre_release}" if self.pre_release else base


@dataclass(frozen=True)
class NativeFileIdentity:
    scheme: str
    final_path: str
    volume_serial: str
    file_id: str
    size: int
    last_write: int

    def to_dict(self) -> dict:
        return {
            "scheme": self.scheme,
            "final_path": self.final_path,
            "volume_serial": self.volume_serial,
            "file_id": self.file_id,
            "size": self.size,
            "last_write": self.last_write,
        }


@dataclass(frozen=True)
class ProbeResult:
    path: str
    source: str
    edition: str
    version: PowerShellVersion
    identity: NativeFileIdentity

    @property
    def warning(self) -> str | None:
        return host_warning(self.edition, self.version)


@dataclass(frozen=True)
class CandidateAttempt:
    path: str
    source: str
    accepted: bool
    reason: str

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "source": self.source,
            "accepted": self.accepted,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class Resolution:
    result: ProbeResult | None
    attempts: tuple[CandidateAttempt, ...]


def hard_gate_accepts(edition: object, version: PowerShellVersion) -> bool:
    return edition == "Core" and version.major >= HARD_MIN_MAJOR


def is_recommended_stable(version: PowerShellVersion) -> bool:
    if version.pre_release is not None:
        return False
    return (version.major, version.minor) >= (
        RECOMMENDED_STABLE_MAJOR,
        RECOMMENDED_STABLE_MINOR,
    )


def needs_eol_warning(version: PowerShellVersion) -> bool:
    return (
        version.pre_release is None
        and version.major == HARD_MIN_MAJOR
        and version.minor < RECOMMENDED_STABLE_MINOR
    )


def needs_prerelease_warning(version: PowerShellVersion) -> bool:
    return version.pre_release is not None


def host_warning(edition: object, version: PowerShellVersion) -> str | None:
    if not hard_gate_accepts(edition, version):
        return None
    if needs_prerelease_warning(version):
        return (
            f"PowerShell {version.display} is a prerelease; PowerShell Core 7.4+ "
            "stable is recommended"
        )
    if needs_eol_warning(version):
        return (
            f"PowerShell {version.display} is end-of-life; PowerShell Core 7.4+ "
            "stable is recommended"
        )
    return None


def generated_preamble() -> str:
    return "#requires -Version 7\n#requires -PSEdition Core\n"


def generated_runtime_guard() -> str:
    return (
        "$AgenttalkVersion = $PSVersionTable.PSVersion\n"
        "if (($PSEdition -ne 'Core') -or ($AgenttalkVersion.Major -lt 7)) {\n"
        "  throw 'agenttalk requires PowerShell Core 7+; Windows PowerShell 5.1 is unsupported'\n"
        "}\n"
        "$AgenttalkPreRelease = $null\n"
        "if ($AgenttalkVersion.PSObject.Properties.Name -contains 'PreReleaseLabel') {\n"
        "  $AgenttalkPreRelease = $AgenttalkVersion.PreReleaseLabel\n"
        "}\n"
        "if ($AgenttalkPreRelease) {\n"
        "  Write-Warning ('PowerShell {0} is a prerelease; PowerShell Core 7.4+ "
        "stable is recommended' -f $AgenttalkVersion)\n"
        "} elseif (($AgenttalkVersion.Major -eq 7) -and ($AgenttalkVersion.Minor -lt 4)) {\n"
        "  Write-Warning ('PowerShell {0} is end-of-life; PowerShell Core 7.4+ "
        "stable is recommended' -f $AgenttalkVersion)\n"
        "}\n"
    )


def explicit_select_remediation(path: str | os.PathLike[str]) -> str:
    """Return the exact operator command for selecting one already-known path."""
    return (
        "run `agenttalk supervise --select-pwsh --pwsh \""
        + os.fspath(path)
        + "\"`"
    )


def _normalize_final_path(path: str) -> str:
    text = path
    if text.startswith("\\\\?\\UNC\\"):
        text = "\\\\" + text[8:]
    elif text.startswith("\\\\?\\"):
        text = text[4:]
    return os.path.normpath(text)


def normalized_path_key(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(_normalize_final_path(os.fspath(path))).casefold()


def same_identity(left: NativeFileIdentity, right: NativeFileIdentity) -> bool:
    return (
        left.scheme == right.scheme
        and normalized_path_key(left.final_path) == normalized_path_key(right.final_path)
        and left.volume_serial == right.volume_serial
        and left.file_id == right.file_id
        and left.size == right.size
        and left.last_write == right.last_write
    )


class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", wintypes.DWORD),
        ("ftCreationTime", wintypes.FILETIME),
        ("ftLastAccessTime", wintypes.FILETIME),
        ("ftLastWriteTime", wintypes.FILETIME),
        ("dwVolumeSerialNumber", wintypes.DWORD),
        ("nFileSizeHigh", wintypes.DWORD),
        ("nFileSizeLow", wintypes.DWORD),
        ("nNumberOfLinks", wintypes.DWORD),
        ("nFileIndexHigh", wintypes.DWORD),
        ("nFileIndexLow", wintypes.DWORD),
    ]


def _open_windows_file(path: str, *, share_mode: int) -> int:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        path,
        0x0080,  # FILE_READ_ATTRIBUTES
        share_mode,
        None,
        3,  # OPEN_EXISTING
        0x00000080,  # FILE_ATTRIBUTE_NORMAL
        None,
    )
    if handle == wintypes.HANDLE(-1).value:
        raise PowerShellHostError(
            f"cannot open candidate (winerror {ctypes.get_last_error()})"
        )
    return int(handle)


def _windows_identity_from_handle(handle: int) -> NativeFileIdentity:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    native_handle = wintypes.HANDLE(handle)
    info = _BY_HANDLE_FILE_INFORMATION()
    if not kernel32.GetFileInformationByHandle(native_handle, ctypes.byref(info)):
        raise PowerShellHostError(
            f"cannot query candidate identity (winerror {ctypes.get_last_error()})"
        )
    needed = kernel32.GetFinalPathNameByHandleW(native_handle, None, 0, 0)
    if not needed:
        raise PowerShellHostError(
            f"cannot query candidate final path (winerror {ctypes.get_last_error()})"
        )
    buf = ctypes.create_unicode_buffer(needed + 1)
    if not kernel32.GetFinalPathNameByHandleW(native_handle, buf, len(buf), 0):
        raise PowerShellHostError(
            f"cannot read candidate final path (winerror {ctypes.get_last_error()})"
        )
    final_path = _normalize_final_path(buf.value)
    if not Path(final_path).is_file():
        raise PowerShellHostError("candidate is not a regular file")
    last_write = (
        int(info.ftLastWriteTime.dwHighDateTime) << 32
    ) | int(info.ftLastWriteTime.dwLowDateTime)
    size = (int(info.nFileSizeHigh) << 32) | int(info.nFileSizeLow)
    file_id = (int(info.nFileIndexHigh) << 32) | int(info.nFileIndexLow)
    return NativeFileIdentity(
        scheme="win32-file-id-v1",
        final_path=final_path,
        volume_serial=f"{int(info.dwVolumeSerialNumber):08x}",
        file_id=f"{file_id:016x}",
        size=size,
        last_write=last_write,
    )


def close_native_file_handle(handle: int) -> None:
    if handle:
        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(
            wintypes.HANDLE(handle)
        )


def open_stable_native_file_identity(
    path: str | os.PathLike[str],
) -> tuple[NativeFileIdentity, int]:
    """Hold a Windows file handle that denies image writes and replacement."""
    if os.name != "nt":
        raise PowerShellHostError("stable native file handles are Windows-only")
    raw = _validate_local_exe_shape(path, subject="process image")
    handle = _open_windows_file(raw, share_mode=0x00000001)  # FILE_SHARE_READ
    try:
        return _windows_identity_from_handle(handle), handle
    except Exception:
        close_native_file_handle(handle)
        raise


def native_file_identity(path: str | os.PathLike[str]) -> NativeFileIdentity:
    """Return a reopenable native identity for an existing regular file."""
    raw = os.fspath(path)
    if os.name != "nt":
        resolved = str(Path(raw).resolve(strict=True))
        stat = os.stat(resolved, follow_symlinks=True)
        if not Path(resolved).is_file():
            raise PowerShellHostError("candidate is not a regular file")
        return NativeFileIdentity(
            scheme="stat-v1",
            final_path=resolved,
            volume_serial=f"{stat.st_dev:x}",
            file_id=f"{stat.st_ino:x}",
            size=int(stat.st_size),
            last_write=int(stat.st_mtime_ns),
        )

    handle = _open_windows_file(
        raw,
        share_mode=0x00000001 | 0x00000002 | 0x00000004,
    )
    try:
        return _windows_identity_from_handle(handle)
    finally:
        close_native_file_handle(handle)


def _windows_drive_type(path: str) -> int:
    drive, _ = os.path.splitdrive(path)
    root = drive + "\\"
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_drive_type = kernel32.GetDriveTypeW
    get_drive_type.argtypes = [wintypes.LPCWSTR]
    get_drive_type.restype = wintypes.UINT
    return int(get_drive_type(root))


def _validate_local_exe_shape(path: str | os.PathLike[str], *, subject: str) -> str:
    raw = os.fspath(path)
    if not raw or not os.path.isabs(raw):
        raise PowerShellHostError(f"{subject} must be an absolute path")
    if raw.startswith("\\\\") or raw.startswith("//"):
        raise PowerShellHostError(f"{subject} must not be UNC")
    if Path(raw).suffix.casefold() != ".exe":
        raise PowerShellHostError(f"{subject} must name a local .exe")
    if "\\windowsapps\\" in raw.replace("/", "\\").casefold():
        raise PowerShellHostError("WindowsApps aliases are unsupported; select the real local pwsh.exe")
    if os.name == "nt":
        drive, _ = os.path.splitdrive(raw)
        if not re.fullmatch(r"[A-Za-z]:", drive):
            raise PowerShellHostError(f"{subject} must be on a fixed local drive")
        if _windows_drive_type(raw) != 3:  # DRIVE_FIXED
            raise PowerShellHostError(f"{subject} must be on a fixed local drive")
    return raw


def validate_candidate_path(path: str | os.PathLike[str]) -> NativeFileIdentity:
    raw = _validate_local_exe_shape(path, subject="candidate")
    try:
        identity = native_file_identity(raw)
    except (OSError, ValueError) as exc:
        if isinstance(exc, PowerShellHostError):
            raise
        raise PowerShellHostError(f"candidate is inaccessible: {exc}") from exc
    if not identity.final_path.casefold().endswith(".exe"):
        raise PowerShellHostError("candidate final path is not a local .exe")
    if identity.final_path.startswith("\\\\"):
        raise PowerShellHostError("candidate final path resolved to UNC")
    return identity


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _attach_kill_on_close_job(proc: subprocess.Popen) -> tuple[object, Callable[[], None]]:
    if os.name != "nt":
        return None, lambda: None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
        err = ctypes.get_last_error()
        kernel32.CloseHandle(job)
        raise OSError(err, "SetInformationJobObject failed")
    process_handle = wintypes.HANDLE(int(proc._handle))  # type: ignore[attr-defined]
    if not kernel32.AssignProcessToJobObject(job, process_handle):
        err = ctypes.get_last_error()
        kernel32.CloseHandle(job)
        raise OSError(err, "AssignProcessToJobObject failed")

    def close() -> None:
        kernel32.CloseHandle(job)

    return job, close


def _run_probe(path: str, *, timeout: float) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["POWERSHELL_UPDATECHECK"] = "Off"
    proc = subprocess.Popen(  # noqa: S603  # nosec B603
        probe_argv(path),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    close_job: Callable[[], None] = _noop
    try:
        try:
            _, close_job = _attach_kill_on_close_job(proc)
        except OSError as exc:
            # kill() can itself raise (PermissionError on Windows,
            # ProcessLookupError if the child already exited) - unguarded,
            # that secondary error would replace this OSError and skip the
            # PowerShellHostError below entirely.
            with contextlib.suppress(OSError):
                proc.kill()
            proc.communicate()
            raise PowerShellHostError(
                f"probe process containment failed: {exc}"
            ) from exc
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            close_job()
            close_job = _noop
            try:
                stdout, stderr = proc.communicate(timeout=2.0)
            except subprocess.TimeoutExpired:
                # kill() can itself raise - guarded so it can never pre-empt
                # the "probe timed out" PowerShellHostError raised below.
                with contextlib.suppress(OSError):
                    proc.kill()
                # A descendant holding the captured pipe handles open can
                # keep this blocked even though proc itself is already
                # dead - bound it and fall back to wait(), which does not
                # touch the pipes at all, so the reap still completes.
                try:
                    stdout, stderr = proc.communicate(timeout=5.0)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        proc.wait(timeout=25.0)
            raise PowerShellHostError(f"probe timed out after {timeout:g}s") from None
        return subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)
    except BaseException:
        if proc.poll() is None:
            # kill() can itself raise - unguarded, that secondary error
            # would replace the owner BaseException being handled here and
            # skip the reap fallback below entirely.
            with contextlib.suppress(OSError):
                proc.kill()
            # Close the containment job before retrying: on Windows this
            # kills any descendant holding the captured stdout/stderr pipe
            # handles open via KILL_ON_JOB_CLOSE, so communicate() below has
            # a chance to actually drain. On POSIX close_job is a no-op -
            # there is no equivalent containment - so wait() (which does
            # not touch the pipes) is what guarantees the top-level process
            # gets reaped there regardless of what a surviving descendant
            # holds open.
            close_job()
            close_job = _noop
            try:
                proc.communicate(timeout=5.0)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=25.0)
        raise
    finally:
        close_job()


def probe_argv(path: str) -> list[str]:
    return [path, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", _PROBE_COMMAND]


def _parse_probe_output(stdout: str) -> tuple[str, PowerShellVersion]:
    lines = [line[len(PROBE_SENTINEL):] for line in stdout.splitlines()
             if line.startswith(PROBE_SENTINEL)]
    if len(lines) != 1:
        raise PowerShellHostError("probe did not emit exactly one sentinel record")
    try:
        data = json.loads(lines[0])
    except (ValueError, TypeError) as exc:
        raise PowerShellHostError("probe emitted malformed JSON") from exc
    if not isinstance(data, dict) or set(data) != {
        "sentinel", "edition", "major", "minor", "patch", "pre_release"
    }:
        raise PowerShellHostError("probe JSON schema mismatch")
    if data.get("sentinel") != "agenttalk-pwsh-probe-v1":
        raise PowerShellHostError("probe sentinel mismatch")
    edition = data.get("edition")
    values = [data.get("major"), data.get("minor"), data.get("patch")]
    pre = data.get("pre_release")
    if not isinstance(edition, str):
        raise PowerShellHostError("probe edition is not a string")
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 0
           for value in values):
        raise PowerShellHostError("probe version fields must be non-negative integers")
    if pre is not None and (not isinstance(pre, str) or not pre.strip() or len(pre) > 128):
        raise PowerShellHostError("probe prerelease label is invalid")
    return edition, PowerShellVersion(values[0], values[1], values[2], pre)


def probe_candidate(
    path: str | os.PathLike[str],
    *,
    source: str,
    timeout: float = PROBE_TIMEOUT_SECONDS,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    identity_reader: Callable[[str | os.PathLike[str]], NativeFileIdentity] = validate_candidate_path,
) -> ProbeResult:
    before = identity_reader(path)
    canonical = before.final_path
    try:
        completed = (runner or _run_probe)(canonical, timeout=timeout)
    except PowerShellHostError:
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        raise PowerShellHostError(f"probe could not run: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip().splitlines()
        suffix = f": {detail[-1]}" if detail else ""
        raise PowerShellHostError(f"probe exited {completed.returncode}{suffix}")
    edition, version = _parse_probe_output(completed.stdout or "")
    if not hard_gate_accepts(edition, version):
        raise PowerShellHostError(
            f"refused {edition} PowerShell {version.display}; PowerShell Core 7+ is required"
        )
    after = identity_reader(canonical)
    if not same_identity(before, after):
        raise PowerShellHostError("candidate identity changed during probe")
    return ProbeResult(after.final_path, source, edition, version, after)


def native_program_files_roots() -> tuple[str, ...]:
    """Read machine-owned Program Files roots from the native HKLM registry view."""
    if os.name != "nt":
        return ()
    try:
        import winreg
    except ImportError:
        return ()

    key_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion"
    value_names = (
        "ProgramW6432Dir",
        "ProgramFilesDir",
        "ProgramFilesDir (Arm)",
        "ProgramFilesDir (x86)",
    )
    access_modes = (
        winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0),
        winreg.KEY_READ,
    )
    key = None
    for access in dict.fromkeys(access_modes):
        try:
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path, 0, access)
            break
        except OSError:
            continue
    if key is None:
        return ()

    roots: list[str] = []
    try:
        for name in value_names:
            try:
                root, value_type = winreg.QueryValueEx(key, name)
            except OSError:
                continue
            if (
                value_type not in {winreg.REG_SZ, winreg.REG_EXPAND_SZ}
                or not isinstance(root, str)
            ):
                continue
            root = root.strip()
            # Registry values are expected to be concrete paths. Expanding a
            # token here would reintroduce process-environment authority.
            if (
                not root
                or "%" in root
                or not os.path.isabs(root)
                or root.startswith("\\\\")
            ):
                continue
            roots.append(root)
    finally:
        winreg.CloseKey(key)
    return tuple(roots)


def program_files_candidates(
    roots: Iterable[str] | None = None,
) -> tuple[str, ...]:
    trusted_roots = native_program_files_roots() if roots is None else roots
    seen: set[str] = set()
    out: list[str] = []
    for raw_root in trusted_roots:
        root = str(raw_root).strip()
        if not root or not os.path.isabs(root) or root.startswith("\\\\"):
            continue
        candidate = os.path.join(root, "PowerShell", "7", "pwsh.exe")
        normalized = normalized_path_key(candidate)
        if normalized in seen:
            continue
        seen.add(normalized)
        out.append(candidate)
    return tuple(out)


def path_candidate_remediations(environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    env = os.environ if environ is None else environ
    out: list[str] = []
    seen: set[str] = set()

    def add(candidate: str) -> None:
        try:
            _validate_local_exe_shape(candidate, subject="PATH candidate")
        except PowerShellHostError:
            return
        key = normalized_path_key(candidate)
        if key in seen:
            return
        seen.add(key)
        try:
            exists = Path(candidate).is_file()
        except OSError:
            exists = False
        if exists:
            out.append(
                f'agenttalk supervise --select-pwsh --pwsh "{candidate}"'
            )

    for entry in (env.get("PATH") or "").split(os.pathsep):
        entry = entry.strip().strip('"')
        if not entry or not os.path.isabs(entry) or entry.startswith("\\\\"):
            continue
        add(os.path.join(entry, "pwsh.exe"))
    for name in ("ProgramW6432", "ProgramFiles", "ProgramFiles(Arm)", "ProgramFiles(x86)"):
        root = (env.get(name) or "").strip().strip('"')
        if not root or not os.path.isabs(root) or root.startswith("\\\\"):
            continue
        add(os.path.join(root, "PowerShell", "7", "pwsh.exe"))
    return tuple(out)


def resolve_candidate(
    *,
    explicit_path: str | None = None,
    current_path: str | None = None,
    environ: Mapping[str, str] | None = None,
    program_files_roots: Iterable[str] | None = None,
    probe: Callable[..., ProbeResult] = probe_candidate,
) -> Resolution:
    if explicit_path is not None and current_path is not None:
        raise PowerShellHostError("explicit and current-host modes are mutually exclusive")
    if explicit_path is not None:
        candidates: Iterable[tuple[str, str]] = ((explicit_path, "explicit"),)
        terminal = True
    elif current_path is not None:
        candidates = ((current_path, "current_host"),)
        terminal = True
    else:
        # Process environment roots are diagnostics only. Automatic execution
        # is sourced exclusively from machine-owned native registry values.
        candidates = (
            (path, "program_files")
            for path in program_files_candidates(program_files_roots)
        )
        terminal = False
    attempts: list[CandidateAttempt] = []
    for path, source in candidates:
        try:
            result = probe(path, source=source)
        except (OSError, ValueError) as exc:
            attempts.append(CandidateAttempt(str(path), source, False, str(exc)))
            if terminal:
                return Resolution(None, tuple(attempts))
            continue
        if (
            source == "program_files"
            and normalized_path_key(result.path) != normalized_path_key(path)
        ):
            attempts.append(CandidateAttempt(
                str(path), source, False,
                "automatic candidate resolved outside its canonical Program Files path",
            ))
            continue
        attempts.append(CandidateAttempt(result.path, source, True, "accepted"))
        return Resolution(result, tuple(attempts))
    return Resolution(None, tuple(attempts))


def _parse_utc_timestamp(value: object, *, now: float) -> float:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise PowerShellHostError("probed_at must be a UTC Z timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00").timestamp()
    except (ValueError, OverflowError) as exc:
        raise PowerShellHostError("probed_at is invalid") from exc
    if not math.isfinite(parsed):
        raise PowerShellHostError("probed_at must be finite")
    if parsed > now + MAX_FUTURE_SKEW_SECONDS:
        raise PowerShellHostError("probed_at is too far in the future")
    return parsed


def utc_now_text(now: float | None = None) -> str:
    dt = datetime.fromtimestamp(now, timezone.utc) if now is not None else datetime.now(timezone.utc)
    return dt.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _identity_from_dict(value: object) -> NativeFileIdentity:
    if not isinstance(value, dict) or set(value) != {
        "scheme", "final_path", "volume_serial", "file_id", "size", "last_write"
    }:
        raise PowerShellHostError("selection identity schema mismatch")
    strings = ("scheme", "final_path", "volume_serial", "file_id")
    if any(not isinstance(value.get(key), str) or not value[key] for key in strings):
        raise PowerShellHostError("selection identity strings are invalid")
    if any(not isinstance(value.get(key), int) or isinstance(value[key], bool) or value[key] < 0
           for key in ("size", "last_write")):
        raise PowerShellHostError("selection identity numeric fields are invalid")
    return NativeFileIdentity(
        value["scheme"], value["final_path"], value["volume_serial"], value["file_id"],
        value["size"], value["last_write"],
    )


def _version_from_dict(value: object) -> PowerShellVersion:
    if not isinstance(value, dict) or set(value) != {"major", "minor", "patch", "pre_release"}:
        raise PowerShellHostError("selection version schema mismatch")
    fields = [value.get("major"), value.get("minor"), value.get("patch")]
    if any(not isinstance(item, int) or isinstance(item, bool) or item < 0 for item in fields):
        raise PowerShellHostError("selection version fields are invalid")
    pre = value.get("pre_release")
    if pre is not None and (not isinstance(pre, str) or not pre.strip() or len(pre) > 128):
        raise PowerShellHostError("selection prerelease is invalid")
    return PowerShellVersion(fields[0], fields[1], fields[2], pre)


def selection_fingerprint_payload(record: Mapping[str, object]) -> dict:
    identity = record.get("identity")
    version = record.get("version")
    if not isinstance(identity, dict) or not isinstance(version, dict):
        raise PowerShellHostError("selection identity/version missing")
    return {
        "domain": SELECTION_FINGERPRINT_DOMAIN,
        "schema": record.get("schema"),
        "project_id": record.get("project_id"),
        "source": record.get("source"),
        "path": record.get("path"),
        "task_name": record.get("task_name"),
        "identity": {
            "scheme": identity.get("scheme"),
            "final_path": identity.get("final_path"),
            "volume_serial": identity.get("volume_serial"),
            "file_id": identity.get("file_id"),
            "size": identity.get("size"),
            "last_write": identity.get("last_write"),
        },
        "edition": record.get("edition"),
        "version": {
            "major": version.get("major"),
            "minor": version.get("minor"),
            "patch": version.get("patch"),
            "pre_release": version.get("pre_release"),
        },
    }


def compute_selection_fingerprint(record: Mapping[str, object]) -> str:
    encoded = json.dumps(
        selection_fingerprint_payload(record),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def selection_immutable_key(record: Mapping[str, object]) -> str:
    return compute_selection_fingerprint(record)


def make_selection_record(
    result: ProbeResult,
    *,
    project_id: str,
    previous: Mapping[str, object] | None = None,
    task_name: str | None = None,
    now: float | None = None,
) -> dict:
    base = {
        "schema": SELECTION_SCHEMA,
        "project_id": project_id,
        "path": result.path,
        "source": result.source,
        "version": result.version.to_dict(),
        "edition": result.edition,
        "probed_at": utc_now_text(now),
        "identity": result.identity.to_dict(),
        "task_name": task_name,
    }
    same = False
    previous_revision = 0
    if previous is not None:
        try:
            previous_revision = int(previous.get("selection_revision", 0))
            same = selection_immutable_key(previous) == selection_immutable_key(base)
        except (TypeError, ValueError, PowerShellHostError):
            same = False
    base["selection_revision"] = previous_revision if same and previous_revision > 0 else previous_revision + 1
    base["selection_fingerprint"] = compute_selection_fingerprint(base)
    return base


def with_task_binding(
    record: Mapping[str, object],
    task_name: str | None,
) -> dict:
    """Return the same selection with a revisioned task-binding update."""
    if task_name is not None and (
        not isinstance(task_name, str) or not task_name.strip() or len(task_name) > 256
    ):
        raise PowerShellHostError("selection task_name is invalid")
    updated = selection_public_view(record)
    if updated.get("task_name") == task_name:
        return updated
    revision = updated.get("selection_revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise PowerShellHostError("selection_revision must be a positive integer")
    updated["task_name"] = task_name
    updated["selection_revision"] = revision + 1
    updated["selection_fingerprint"] = compute_selection_fingerprint(updated)
    return updated


def validate_selection_record(
    value: object,
    *,
    project_id: str,
    now: float | None = None,
    require_fresh: bool = False,
) -> dict:
    if not isinstance(value, dict):
        raise PowerShellHostError("selection must be a JSON object")
    required = {
        "schema", "project_id", "path", "source", "version", "edition",
        "probed_at", "identity", "selection_revision", "selection_fingerprint",
    }
    allowed = required | {"task_name"}
    if not required.issubset(value) or not set(value).issubset(allowed):
        raise PowerShellHostError("selection schema mismatch")
    if value.get("schema") != SELECTION_SCHEMA or value.get("project_id") != project_id:
        raise PowerShellHostError("selection belongs to a different schema or project")
    if value.get("source") not in {"program_files", "current_host", "explicit"}:
        raise PowerShellHostError("selection source is invalid")
    if not isinstance(value.get("path"), str):
        raise PowerShellHostError("selection path must be a string")
    _validate_local_exe_shape(value["path"], subject="selection path")
    edition = value.get("edition")
    version = _version_from_dict(value.get("version"))
    identity = _identity_from_dict(value.get("identity"))
    _validate_local_exe_shape(identity.final_path, subject="selection identity final_path")
    if os.name == "nt" and identity.scheme != "win32-file-id-v1":
        raise PowerShellHostError("selection identity scheme is not a Windows file identity")
    if normalized_path_key(value["path"]) != normalized_path_key(identity.final_path):
        raise PowerShellHostError("selection path and final identity path differ")
    if not hard_gate_accepts(edition, version):
        raise PowerShellHostError("selection is not PowerShell Core 7+")
    revision = value.get("selection_revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise PowerShellHostError("selection_revision must be a positive integer")
    fingerprint = value.get("selection_fingerprint")
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise PowerShellHostError("selection_fingerprint is invalid")
    if fingerprint != compute_selection_fingerprint(value):
        raise PowerShellHostError("selection_fingerprint mismatch")
    task_name = value.get("task_name")
    if task_name is not None and (
        not isinstance(task_name, str) or not task_name.strip() or len(task_name) > 256
    ):
        raise PowerShellHostError("selection task_name is invalid")
    stamp_now = datetime.now(timezone.utc).timestamp() if now is None else now
    probed = _parse_utc_timestamp(value.get("probed_at"), now=stamp_now)
    age = max(0.0, stamp_now - probed)
    if require_fresh and age > SELECTION_TTL_SECONDS:
        raise PowerShellHostError("selection probe cache expired")
    result = dict(value)
    result["_version"] = version
    result["_identity"] = identity
    result["_age_seconds"] = age
    result["_expired"] = age > SELECTION_TTL_SECONDS
    result["_warning"] = host_warning(edition, version)
    return result


def selection_public_view(record: Mapping[str, object]) -> dict:
    return {key: value for key, value in record.items() if not key.startswith("_")}
