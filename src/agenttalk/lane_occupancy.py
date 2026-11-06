"""The lane occupancy token (#659): a running `wrap --lane-id` session keeps its lane "in use".

One empty file per lane, `<store>/lane-occupancy/lane-<id>.token`, never deleted. The wrapper takes the SHARED side
before it reads the lane and hands the open file to the model child, so the system releases it when the last holder
exits (a crash cleans up by itself). Every cleanup path takes the EXCLUSIVE side before it changes anything and leaves
the lane alone unless that succeeds. Neither side ever waits, so no lock order is added. Release is by closing only,
never by an unlock call, because an inherited copy of the file would otherwise be unlocked with it.

  POSIX    `flock` on one open file: `LOCK_SH|LOCK_NB` / `LOCK_EX|LOCK_NB`; a refusal is `BlockingIOError`.
  Windows  `CreateFileW`: the wrapper reads and shares read and write but never delete; cleanup opens with no
           sharing (error 32 = in use). Both refuse a reparse point.

The answer is "free" (the claim is yielded as None), "in use", or "could not be established": anything that is not a
plain refusal by a holder (a folder, a link, a pipe, denied access, a vanished parent) is never read as free.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import platform
import re
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

IN_USE = "in use"
UNKNOWN = "could not be established"
_HELD: dict[str, int] = {}  # lane id -> the open file, kept until this process ends (closing is the only release)
_PROOF_CLI: str | None = None  # "codex" when the program of this lane-bound session must be proved before every spawn
MIN_CODEX = (0, 163, 0)  # the lowest Codex release the real-install runs cover (tests, group 9)


class LaneOccupied(OSError):
    """The wrapper may not take the lane: it is being cleaned up, or its token cannot be taken."""


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateFileW.restype = wintypes.HANDLE
    _k32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]

    def _open(path: Path, shared: bool):
        # GENERIC_READ (| DELETE for cleanup); share read+write, or nothing; OPEN_ALWAYS; NORMAL | OPEN_REPARSE_POINT
        access, share = (0x80000000, 3) if shared else (0x80000000 | 0x00010000, 0)
        handle = _k32.CreateFileW(str(path), access, share, None, 4, 0x80 | 0x00200000, None)
        if handle is None or handle == wintypes.HANDLE(-1).value:
            code = ctypes.get_last_error()
            return None, IN_USE if code == 32 else f"{UNKNOWN}: Windows error {code}"
        if getattr(os.lstat(path), "st_file_attributes", 0) & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT
            _k32.CloseHandle(handle)
            return None, f"{UNKNOWN}: the token is a link"
        return handle, None

    def _close(handle) -> None:
        _k32.CloseHandle(handle)
else:
    import fcntl

    def _open(path: Path, shared: bool):
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600)
        except OSError as error:
            return None, f"{UNKNOWN}: {error.strerror or error}"
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError("not a regular file")
            fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return None, IN_USE
        except OSError as error:
            os.close(fd)
            return None, f"{UNKNOWN}: {error.strerror or error}"
        return fd, None

    def _close(handle) -> None:
        os.close(handle)


def token_path(store, lane_id: str) -> Path:
    """The token's place, after making sure its folder is a plain folder (a link or file there is an error)."""
    folder = store.dir / "lane-occupancy"
    store._ensure_plain_lock_directory(folder, what="lane occupancy directory")
    return folder / f"lane-{lane_id}.token"


def hold(store, lane_id: str) -> None:
    """The wrapper's side, taken BEFORE the lane is read. Raises `LaneOccupied` when cleanup holds the lane or the token
    cannot be taken; otherwise keeps the token until this process ends."""
    try:
        handle, reason = _open(token_path(store, lane_id), shared=True)
    except OSError as error:
        handle, reason = None, f"{UNKNOWN}: {error}"
    if handle is None:
        what = "is being cleaned up" if reason == IN_USE else f"cannot be taken ({reason})"
        raise LaneOccupied(f"lane {lane_id!r} {what}; nothing was started")
    if os.name == "nt":
        os.set_handle_inheritable(handle, True)
    _HELD[lane_id] = handle


def held() -> bool:
    return bool(_HELD)


def require_proof(cli: str | None) -> None:
    """Say whether this lane-bound session must prove its Codex program (`cli == "codex"`) before every spawn."""
    global _PROOF_CLI
    _PROOF_CLI = cli if _HELD else None


def proof_required() -> bool:
    return _PROOF_CLI == "codex"


def _machine_triples() -> frozenset[str]:
    arch = {"amd64": "x86_64", "x86_64": "x86_64", "arm64": "aarch64", "aarch64": "aarch64"}.get(
        platform.machine().lower()
    )
    if arch is None:
        return frozenset()
    if os.name == "nt":
        return frozenset({f"{arch}-pc-windows-msvc"})
    if sys.platform == "darwin":
        return frozenset({f"{arch}-apple-darwin"})
    return frozenset({f"{arch}-unknown-linux-musl", f"{arch}-unknown-linux-gnu"})


def _release(text: object) -> tuple[int, int, int] | None:
    found = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", text) if isinstance(text, str) else None
    return tuple(int(part) for part in found.groups()) if found else None


def codex_proof(program) -> str | None:
    """None when `program` is the real native Codex, proved by the package record (`codex-package.json`) two folders
    above its real path; otherwise why not. The proof is of the installation, not protection against tampering: the
    record must be strict (layoutVersion the integer 1, an entrypoint that names this same file inside the package, this
    machine's target, a plain version at or above the lowest one covered) and, on the npm route, match the launcher
    package."""
    try:
        real = Path(program).resolve(strict=True)
        folder = real.parent.parent
        record = json.loads((folder / "codex-package.json").read_text(encoding="utf-8"))
        if type(record.get("layoutVersion")) is not int or record["layoutVersion"] != 1:
            return "its package record has no layoutVersion 1"
        entry = record.get("entrypoint")
        if not isinstance(entry, str) or not entry or ".." in Path(entry).parts or Path(entry).is_absolute():
            return "its package record names no entrypoint inside the package"
        if (folder / entry).resolve(strict=True) != real:
            return "its package record names another file as the entrypoint"
        if record.get("target") not in _machine_triples():
            return "its package record is for another machine"
        version = _release(record.get("version"))
        if version is None or version < MIN_CODEX:
            floor = ".".join(map(str, MIN_CODEX))
            return f"its version {record.get('version')!r} is not a plain release at or above {floor}"
        launcher = real.parents[6] / "package.json" if len(real.parents) > 6 else None
        if launcher is not None and launcher.is_file():  # the npm route: the launcher package must be the same release
            package = json.loads(launcher.read_text(encoding="utf-8"))
            if package.get("name") == "@openai/codex" and package.get("version") != record["version"]:
                return "its package record and its launcher package are different releases"
    except (OSError, ValueError, AttributeError, TypeError):
        return "it has no readable package record (codex-package.json) two folders above it"
    return None


def codex_error(why: str) -> str:
    return f"a Codex session on a lane cannot be proved to be the real native program: {why}"


CODEX_REMEDIATION = (
    "give the full path of the native codex (the one with codex-package.json two folders above it) after -- or in "
    "AGENTTALK_CODEX. Started directly, Codex does not get the CODEX_MANAGED_* variables the npm launcher sets, so it "
    "may not know how it was installed and its update action can be missing (PATH is not changed)"
)


def current_ephemeral_launch(store, request_id, agent: str, lane_id: str) -> bool:
    """Positive evidence that this session is the CURRENT supervised ephemeral launch of `request_id`: the
    launch-request marker is readable, valid and still active, names this lane (top level and scope may not
    disagree), and the
    supervisor's own active record for it is for this agent, in the prepared or launched phase, within its deadline,
    with a launch binding that matches the marker. Missing, terminal, contradictory, stale or unreadable evidence is
    False."""
    from agenttalk import ephemeral, supervisor

    if not isinstance(request_id, str) or not ephemeral.is_safe_id(request_id):
        return False
    try:
        marker = store.read_launch_request(request_id)
        state, _problem = supervisor._read_supervisor_state_copy(store.dir / "supervisor-state.json", source="state")
        record = ((state or {}).get("ephemeral_reviewers") or {}).get("active", {}).get(request_id)
        scope = marker.get("scope") if isinstance(marker.get("scope"), dict) else {}
        named = {marker.get("lane_id"), scope.get("lane_id")} - {None}
        binding = ephemeral.validate_effective_launch_binding(record.get("effective_launch_binding"))
        return bool(
            not ephemeral.validate_marker(marker)
            and marker["request_id"] == request_id
            and marker.get("state", ephemeral.STATE_QUEUED) in ephemeral.ACTIVE_STATES
            and named == {lane_id}
            and record.get("request_id") == request_id
            and record.get("agent") == agent
            and record.get("phase") in (ephemeral.STATE_REQUESTED, ephemeral.STATE_LAUNCHED)
            and isinstance(record.get("deadline_epoch"), (int, float))
            and record["deadline_epoch"] > time.time()
            and binding is not None
            and binding["request_sha256"] == ephemeral.effective_launch_request_digest(marker)
        )
    except Exception:  # noqa: BLE001 - evidence that cannot be read is not evidence
        return False


def guard_spawn(argv) -> None:
    """Called right before every real spawn: a lane-bound Codex must still be the proved program."""
    if proof_required():
        why = codex_proof(argv[0])
        if why is not None:
            raise PermissionError(errno.EACCES, f"{codex_error(why)}; {CODEX_REMEDIATION}")


def child_kwargs() -> dict:
    """`Popen` keyword arguments that hand the token to the model child only (empty when no lane is held)."""
    if not _HELD:
        return {}
    if os.name == "nt":
        return {"startupinfo": subprocess.STARTUPINFO(lpAttributeList={"handle_list": list(_HELD.values())})}
    return {"pass_fds": tuple(_HELD.values())}


@contextlib.contextmanager
def cleanup_claim(store, lane_id: str) -> Iterator[str | None]:
    """The cleanup side for one lane: yields None while the exclusive claim is held (kept for the whole block), or the
    reason it is not free. Never waits. Taking it creates a missing token, which then means nobody holds it."""
    handle = None
    try:
        handle, reason = _open(token_path(store, lane_id), shared=False)
    except OSError as error:
        reason = f"{UNKNOWN}: {error}"
    try:
        yield None if handle is not None else reason
    finally:
        if handle is not None:
            _close(handle)


def peek(store, lane_id: str) -> str | None:
    """A look that creates nothing, for a listing: None when no token exists or the claim is free, else the reason."""
    if not os.path.lexists(store.dir / "lane-occupancy" / f"lane-{lane_id}.token"):
        return None
    with cleanup_claim(store, lane_id) as busy:
        return busy


def refusal(lane_id: str, reason: str) -> str:
    """Why a cleanup is not done, for the person: names what is known and what to do."""
    if reason == IN_USE:
        return (
            f"lane {lane_id!r} is in use: a `wrap --lane-id` session (or something it started) still holds it; "
            "stop that session first"
        )
    return f"whether a session is running on lane {lane_id!r} {reason}; nothing was changed"
