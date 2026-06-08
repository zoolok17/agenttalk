"""Tests for the shared atomic-write helper.

_atomic.write_text is the single write path for ALL bus state (config,
cursors, threadstate, heartbeats, messages, the codex config, exported
transcripts), so its durability + crash-cleanup behavior is load-bearing
yet was previously untested (review batch-3).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agenttalk import _atomic, transcript
from agenttalk.store import Store


def test_write_text_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "a.txt"
    _atomic.write_text(p, "hello\nworld")
    assert p.read_text(encoding="utf-8") == "hello\nworld"


def test_write_text_creates_parent_dirs(tmp_path: Path) -> None:
    p = tmp_path / "deep" / "nested" / "b.txt"
    _atomic.write_text(p, "x")
    assert p.read_text(encoding="utf-8") == "x"
    assert p.parent.is_dir()


def test_write_text_fsyncs_for_durability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The temp file must be fsynced before the rename exposes it."""
    calls: list[int] = []
    real_fsync = os.fsync
    monkeypatch.setattr("os.fsync", lambda fd: (calls.append(fd), real_fsync(fd))[1])
    _atomic.write_text(tmp_path / "d.txt", "data")
    assert calls  # fsync was invoked at least for the file fd
    assert (tmp_path / "d.txt").read_text(encoding="utf-8") == "data"


def test_write_text_cleanup_on_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the write fails mid-way, the original file is untouched and no
    temp file is leaked (exercises the except/unlink branch)."""
    target = tmp_path / "f.txt"
    target.write_text("ORIGINAL", encoding="utf-8")

    def boom(_fd: int) -> None:
        raise OSError("induced fsync failure")

    monkeypatch.setattr("os.fsync", boom)
    with pytest.raises(OSError, match="induced"):
        _atomic.write_text(target, "NEW CONTENT")
    assert target.read_text(encoding="utf-8") == "ORIGINAL"  # never replaced
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith("f.txt.")]
    assert leftovers == []  # temp file cleaned up, no debris


@pytest.mark.skipif(os.name != "nt", reason="Windows-only os.replace retry path")
def test_replace_retry_rides_out_transient_permission_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "r.txt"
    target.write_text("OLD", encoding="utf-8")
    real_replace = os.replace
    calls = {"n": 0}

    def flaky(src: str, dst: str) -> None:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError("simulated sharing violation")
        return real_replace(src, dst)

    monkeypatch.setattr("os.replace", flaky)
    _atomic.write_text(target, "NEW")
    assert target.read_text(encoding="utf-8") == "NEW"
    assert calls["n"] >= 3  # retried past the transient failures


def test_transcript_export_is_atomic_no_temp_leak(tmp_path: Path) -> None:
    """transcript.export must write through _atomic (no half-written
    session record, no leftover temp file)."""
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    s.send(sender="alpha", recipient="beta", body="hi")
    out = transcript.export(s, fmt="md")
    assert out.is_file()
    text = out.read_text(encoding="utf-8")
    assert "agenttalk transcript" in text and "hi" in text
    leftovers = [p.name for p in out.parent.iterdir()
                 if p.name.startswith(out.name + ".")]
    assert leftovers == []
