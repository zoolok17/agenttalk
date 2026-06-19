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


# Process-local "the rename path is blocked" latch (0.28.1 / Codex sandbox).
# A SUPERVISED codex agent runs in a workspace-write sandbox that holds a
# write-tracking handle on every file the process creates, for the PROCESS
# LIFETIME - so the temp + os.replace below can NEVER complete the rename
# ([WinError 5]). Once we observe that ONCE, every later write in this process
# skips the futile temp+retry and goes straight to a direct final-path write.
# Atomic-first on normal hosts; fast-direct once the sandbox is detected.
_sandbox_direct_write = False


def write_text(path: Path, text: str, *, encoding: str = "utf-8",
               newline: str = "\n") -> None:
    """Durably write ``text`` to ``path`` - atomic where the platform allows it.

    Normal path (POSIX + ordinary Windows): write a sibling temp file, fsync it,
    then ``os.replace`` it into place, so a concurrent reader always sees either
    the old file or the complete new one (never a partial) and the result
    survives a crash. On POSIX the parent dir is fsynced; on Windows the rename
    is retried briefly to ride out a transient reader sharing-violation.

    Codex-sandbox fallback (Windows ONLY, after the bounded retry still fails
    with PermissionError/[WinError 5]): the workspace-write sandbox makes the
    rename impossible, so fall back to a DIRECT final-path write of the same
    content (open + write + flush + fsync) and latch ``_sandbox_direct_write`` so
    subsequent writes skip the doomed temp+retry. This is the ONLY in-workspace
    write primitive that works under that sandbox; crash-atomicity is weaker on
    this path (a reader can catch a half-write), which is acceptable because the
    bus's readers tolerate a torn read (cursor() validates the id; the .heartbeat
    /.waiting readers degrade to "no signal"; a torn message json fails its load
    and is skipped). NOT taken on POSIX / normal Windows - a real rename failure
    there still raises.
    """
    global _sandbox_direct_write
    path.parent.mkdir(parents=True, exist_ok=True)
    if _sandbox_direct_write:
        _direct_write(path, text, encoding=encoding, newline=newline)
        return
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline=newline) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())  # data durable before the rename exposes it
        try:
            _replace_with_retry(tmp, path)
        except PermissionError:
            if os.name != "nt":
                raise   # a genuine POSIX rename failure must still surface
            # The bounded retry is exhausted (a transient reader violation would
            # have cleared inside _replace_with_retry, keeping the atomic path) -
            # the Windows sandbox blocks the rename for good. Direct-write the
            # final path; LATCH the flag ONLY after that SUCCEEDS (Codex
            # guardrail: never flip on a mere WinError5). A failing direct-write
            # raises loudly + leaves the flag false.
            _direct_write(path, text, encoding=encoding, newline=newline)
            _sandbox_direct_write = True
            try:
                os.unlink(tmp)
            except OSError:
                pass            # in-sandbox unlink may be blocked too; harmless
            return
        _fsync_dir(path.parent)   # make the rename itself durable (POSIX)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _direct_write(path: Path, text: str, *, encoding: str, newline: str) -> None:
    """Direct (non-atomic) overwrite of the FINAL path - no temp, no rename, no
    delete. The Codex-sandbox fallback write primitive (see write_text)."""
    with open(path, "w", encoding=encoding, newline=newline) as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())


def _replace_with_retry(src: str, dst: Path) -> None:
    """``os.replace``, retried briefly on Windows then RAISING on a persistent
    PermissionError (the caller decides whether to fall back).

    On Windows a concurrent reader holding ``dst`` open without
    FILE_SHARE_DELETE makes ``os.replace`` raise PermissionError
    (ERROR_ACCESS_DENIED / SHARING_VIOLATION). A transient reader is gone within
    a few ms, so a SHORT bounded backoff (<200ms total) rides it out; a PERSISTENT
    failure (the Codex sandbox's process-lifetime handle) then propagates so
    ``write_text`` can fall back to a direct write. POSIX renames don't hit this
    and replace once.
    """
    if os.name != "nt":
        os.replace(src, dst)
        return
    for delay in (0.01, 0.02, 0.04, 0.08):   # ~0.15s total, < 200ms
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            time.sleep(delay)
    os.replace(src, dst)  # final attempt — raises PermissionError if still blocked


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
