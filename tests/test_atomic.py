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


def test_write_text_uses_rename_on_normal_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On a normal host write_text takes the ATOMIC temp+os.replace path - NOT
    the sandbox direct-write fallback. Asserted by spying os.replace."""
    monkeypatch.setattr("agenttalk._atomic._sandbox_direct_write", False)
    real = os.replace
    seen = {"n": 0}
    monkeypatch.setattr("os.replace", lambda s, d: (seen.__setitem__("n", seen["n"] + 1),
                                                    real(s, d))[1])
    _atomic.write_text(tmp_path / "a.txt", "data")
    assert seen["n"] == 1                                  # went through the rename
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "data"
    assert _atomic._sandbox_direct_write is False          # latch NOT tripped


def test_write_text_sandbox_fallback_on_blocked_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When os.replace persistently raises PermissionError on Windows (the Codex
    workspace-write sandbox), write_text FALLS BACK to a direct final-path write
    of the full content - no leftover temp - and latches the process-local flag.
    Models test #3's [WinError 5] cross-platform via os.name + os.replace +
    no-op sleep (so the bounded retry doesn't actually wait)."""
    monkeypatch.setattr("agenttalk._atomic._sandbox_direct_write", False)
    monkeypatch.setattr("agenttalk._atomic.os.name", "nt")
    monkeypatch.setattr("agenttalk._atomic.time.sleep", lambda _s: None)
    monkeypatch.setattr("os.replace", lambda *a, **k: (_ for _ in ()).throw(
        PermissionError("[WinError 5] Access is denied (sandbox)")))
    target = tmp_path / "m.json"
    _atomic.write_text(target, '{"id":"x"}')
    assert target.read_text(encoding="utf-8") == '{"id":"x"}'   # full content landed
    assert _atomic._sandbox_direct_write is True                # latch tripped
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith("m.json.")]
    assert leftovers == []                                      # temp cleaned up


def test_write_text_latch_skips_temp_after_sandbox_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once the sandbox is detected, subsequent writes skip the doomed temp+retry
    entirely (no mkstemp call) and go straight to direct-write."""
    monkeypatch.setattr("agenttalk._atomic._sandbox_direct_write", True)
    monkeypatch.setattr("agenttalk._atomic.tempfile.mkstemp", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("mkstemp must NOT be called once the sandbox latch is set")))
    _atomic.write_text(tmp_path / "n.txt", "direct")
    assert (tmp_path / "n.txt").read_text(encoding="utf-8") == "direct"


def test_write_text_posix_rename_failure_still_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On POSIX a genuine rename PermissionError must STILL surface - the
    direct-write fallback is Windows-sandbox-only, never a silent POSIX downgrade."""
    monkeypatch.setattr("agenttalk._atomic._sandbox_direct_write", False)
    monkeypatch.setattr("agenttalk._atomic.os.name", "posix")
    monkeypatch.setattr("os.replace", lambda *a, **k: (_ for _ in ()).throw(
        PermissionError("denied")))
    with pytest.raises(PermissionError):
        _atomic.write_text(tmp_path / "p.txt", "x")
    assert _atomic._sandbox_direct_write is False           # never latched on POSIX


def test_latch_stays_false_on_transient_winerror5_then_retry_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GUARDRAIL (codex): the latch must flip ONLY after the bounded retry is
    exhausted AND a direct-write succeeds - NEVER on a mere WinError5. An ordinary
    Windows transient sharing-violation that clears on retry keeps the ATOMIC path
    (flag stays false)."""
    monkeypatch.setattr("agenttalk._atomic._sandbox_direct_write", False)
    monkeypatch.setattr("agenttalk._atomic.os.name", "nt")
    monkeypatch.setattr("agenttalk._atomic.time.sleep", lambda _s: None)
    real = os.replace
    calls = {"n": 0}

    def flaky(src: str, dst: str) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError("transient sharing violation")
        return real(src, dst)

    monkeypatch.setattr("os.replace", flaky)
    _atomic.write_text(tmp_path / "t.txt", "data")
    assert (tmp_path / "t.txt").read_text(encoding="utf-8") == "data"
    assert calls["n"] >= 2                                 # retried (atomic) ...
    assert _atomic._sandbox_direct_write is False          # ... and did NOT latch


def test_latch_not_set_when_direct_write_fallback_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """codex regression: if the direct-write fallback itself FAILS (not a sandbox
    rename block - e.g. ACL/disk), write_text must raise loudly and the latch must
    stay FALSE, so a non-sandbox failure never makes the whole process bypass the
    atomic path. Forced by a blocked rename + a direct-write target that can't be
    opened (a directory)."""
    monkeypatch.setattr("agenttalk._atomic._sandbox_direct_write", False)
    monkeypatch.setattr("agenttalk._atomic.os.name", "nt")
    monkeypatch.setattr("agenttalk._atomic.time.sleep", lambda _s: None)
    monkeypatch.setattr("os.replace", lambda *a, **k: (_ for _ in ()).throw(
        PermissionError("[WinError 5] sandbox")))
    target = tmp_path / "is_a_dir"
    target.mkdir()                                         # open('w') on a dir -> raises
    with pytest.raises(OSError):
        _atomic.write_text(target, "x")
    assert _atomic._sandbox_direct_write is False          # NOT latched on a failed fallback


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
