"""Small shared primitives for durable append-only JSONL ledgers."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Callable


def iter_lines(path: Path) -> Iterator[tuple[int, str | None]]:
    """Yield decoded physical lines; an undecodable line is represented by ``None``."""
    with path.open("rb") as fh:
        for line_number, line in enumerate(fh, start=1):
            try:
                decoded = line.decode("utf-8")
            except UnicodeDecodeError:
                decoded = None
            yield line_number, decoded


def _write_all(fd: int, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        written = os.write(fd, remaining)
        if written <= 0:
            raise OSError("short write while appending JSONL record")
        remaining = remaining[written:]


def _fsync_parent(path: Path) -> None:
    try:
        dfd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def append_record(
    path: Path,
    record: Any,
    *,
    default: Callable[[Any], Any] | None = None,
) -> None:
    """Durably append one JSON record, isolating any unterminated prior tail.

    The caller owns inter-process serialization. If a crashed or hand-written
    record did not end with a newline, insert the missing delimiter before the
    new record so the old tail cannot absorb it.
    """
    payload = json.dumps(
        record,
        ensure_ascii=False,
        sort_keys=True,
        default=default,
    ).encode("utf-8") + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | os.O_APPEND
    flags |= getattr(os, "O_BINARY", 0)
    fd = os.open(str(path), flags, 0o600)
    try:
        size = os.lseek(fd, 0, os.SEEK_END)
        if size:
            os.lseek(fd, -1, os.SEEK_END)
            last_byte = os.read(fd, 1)
            os.lseek(fd, 0, os.SEEK_END)
            if last_byte != b"\n":
                _write_all(fd, b"\n")
        _write_all(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    _fsync_parent(path.parent)
