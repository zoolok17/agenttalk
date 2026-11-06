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
import os
import stat
import subprocess
from collections.abc import Iterator
from pathlib import Path

IN_USE = "in use"
UNKNOWN = "could not be established"
_HELD: dict[str, int] = {}  # lane id -> the open file, kept until this process ends (closing is the only release)


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
