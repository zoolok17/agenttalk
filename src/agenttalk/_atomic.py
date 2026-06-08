"""Shared atomic-write helper.

Both the message store and the codex-config editor need to update
files in a way that survives a crash or concurrent reader without
ever leaving a half-written file on disk. The implementation is
"write to a sibling temp file, then ``os.replace``", which is atomic
on both NTFS and POSIX.
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path


def write_text(path: Path, text: str, *, encoding: str = "utf-8",
               newline: str = "\n") -> None:
    """Atomically and durably write ``text`` to ``path``.

    Writes to a temp file in the same directory, fsyncs it, then
    ``os.replace``s it into place. A concurrent reader therefore always
    sees either the old file or the complete new one (never a partial),
    and the new contents are on stable storage before the rename makes
    them visible — so the result survives a crash/power loss. On POSIX
    the parent directory is fsynced after the rename so the rename itself
    is durable; on Windows ``os.replace`` is retried briefly to ride out a
    transient sharing violation from a reader holding the destination.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline=newline) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())  # data durable before the rename exposes it
        _replace_with_retry(tmp, path)
        _fsync_dir(path.parent)   # make the rename itself durable (POSIX)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _replace_with_retry(src: str, dst: Path) -> None:
    """``os.replace``, retried on Windows to survive a transient error.

    On Windows a concurrent reader holding ``dst`` open without
    FILE_SHARE_DELETE makes ``os.replace`` raise PermissionError
    (ERROR_ACCESS_DENIED / SHARING_VIOLATION). Readers here hold the file
    only briefly, so a short bounded backoff rides it out instead of
    surfacing a spurious crash. POSIX renames don't hit this, so they
    replace once and let any error propagate.
    """
    if os.name != "nt":
        os.replace(src, dst)
        return
    for delay in (0.01, 0.02, 0.04, 0.08, 0.16):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            time.sleep(delay)
    os.replace(src, dst)  # final attempt — let it raise if still contended


def _fsync_dir(directory: Path) -> None:
    """Best-effort fsync of a directory so a rename into it is durable.

    POSIX-only and fail-quiet: a directory fd can't be fsynced on Windows
    (and isn't required there), and a failure here must never turn an
    otherwise-successful write into an error.
    """
    if os.name == "nt":
        return
    try:
        dfd = os.open(str(directory), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass
