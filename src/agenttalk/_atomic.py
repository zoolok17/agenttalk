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
from pathlib import Path


def write_text(path: Path, text: str, *, encoding: str = "utf-8",
               newline: str = "\n") -> None:
    """Atomically write ``text`` to ``path``.

    Creates parent directories if needed. Uses a temp file in the same
    directory so the final ``os.replace`` is a same-filesystem rename
    (atomic on Windows and POSIX).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline=newline) as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
