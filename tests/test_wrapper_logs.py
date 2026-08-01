from __future__ import annotations

import argparse
import io
import json
import os
import signal
import stat
from pathlib import Path

import pytest

from agenttalk import cli, wrapper_logs
from agenttalk import wrapper_runtime as runtime


NOW = 1_800_000_000.0


def test_default_wrapper_log_root_is_project_scoped_and_outside_checkout(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    local = tmp_path / "local-state"

    resolved = wrapper_logs.default_wrapper_log_root(
        checkout,
        platform="posix",
        environ={"XDG_STATE_HOME": str(local)},
    )

    assert resolved.parent == local / "agenttalk" / "wrapper-logs"
    assert len(resolved.name) == 64
    assert checkout.resolve() not in resolved.resolve().parents


def test_default_wrapper_log_root_rejects_relative_ambient_state_path(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    home = tmp_path / "home"

    resolved = wrapper_logs.default_wrapper_log_root(
        checkout,
        platform="posix",
        environ={"XDG_STATE_HOME": ".agenttalk-logs", "HOME": str(home)},
    )

    assert resolved.parent == home / ".local" / "state" / "agenttalk" / "wrapper-logs"
    assert checkout.resolve() not in resolved.resolve().parents


def test_default_wrapper_log_root_rejects_absolute_state_path_inside_checkout(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    home = tmp_path / "home"

    resolved = wrapper_logs.default_wrapper_log_root(
        checkout,
        platform="posix",
        environ={
            "XDG_STATE_HOME": str(checkout / "logs"),
            "HOME": str(home),
        },
    )

    assert resolved.parent == home / ".local" / "state" / "agenttalk" / "wrapper-logs"
    assert checkout.resolve() not in resolved.resolve().parents


def test_default_wrapper_log_root_uses_independent_fallback_when_home_is_checkout(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()

    resolved = wrapper_logs.default_wrapper_log_root(
        checkout,
        platform="posix",
        environ={
            "XDG_STATE_HOME": str(checkout / "state"),
            "HOME": str(checkout / "home"),
        },
    )

    assert checkout.resolve() not in resolved.resolve().parents


def test_default_wrapper_log_root_tolerates_unresolvable_home_when_configured_path_is_valid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    local = tmp_path / "local-state"

    def _raise_no_home() -> Path:
        raise RuntimeError("could not determine home directory")

    monkeypatch.setattr(wrapper_logs.Path, "home", staticmethod(_raise_no_home))

    resolved = wrapper_logs.default_wrapper_log_root(
        checkout,
        platform="posix",
        environ={"XDG_STATE_HOME": str(local)},
    )

    assert resolved.parent == local / "agenttalk" / "wrapper-logs"
    assert checkout.resolve() not in resolved.resolve().parents


def test_default_wrapper_log_root_falls_back_to_tempdir_when_home_is_unresolvable_and_no_state_path_is_configured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()

    def _raise_no_home() -> Path:
        raise RuntimeError("could not determine home directory")

    monkeypatch.setattr(wrapper_logs.Path, "home", staticmethod(_raise_no_home))

    resolved = wrapper_logs.default_wrapper_log_root(
        checkout,
        platform="posix",
        environ={},
    )

    assert checkout.resolve() not in resolved.resolve().parents


def test_bounded_stream_tee_keeps_each_file_and_generation_within_cap(
    tmp_path: Path,
) -> None:
    original = io.StringIO()
    base = tmp_path / "stdout.log"
    tee = wrapper_logs.BoundedStreamTee(
        original,
        base,
        max_bytes=4096,
        segment_count=4,
    )

    tee.write("old-" * 3000)
    tee.write("TERMINAL-SENTINEL\n")
    tee.flush()
    tee.close()

    files = sorted(tmp_path.glob("stdout.log*"))
    assert files
    assert all(path.stat().st_size <= 1024 for path in files)
    assert sum(path.stat().st_size for path in files) <= 3072
    assert len(original.getvalue().encode("utf-8")) <= 1024
    assert "TERMINAL-SENTINEL" in "".join(
        path.read_text(encoding="utf-8") for path in files
    )


def test_bounded_stream_tee_newline_heavy_stream_stays_within_cap_on_disk(
    tmp_path: Path,
) -> None:
    """Finding B (PR 98 connector re-review, head 4323e20): the budget must be
    measured against what actually lands on disk, not the pre-translation
    UTF-8 length. A REAL text-mode file (unlike io.StringIO, used elsewhere in
    this file) applies the platform's newline translation on write - on
    Windows each accounted "\\n" becomes two bytes ("\\r\\n") on disk, so a
    newline-heavy stream could blow the per-file cap by nearly 2x while the
    accounting still believed it was exactly at the cap."""
    original_path = tmp_path / "original-stdout.txt"
    original = original_path.open("w", encoding="utf-8", newline=None)
    base = tmp_path / "stdout.log"
    tee = wrapper_logs.BoundedStreamTee(
        original,
        base,
        max_bytes=4096,
        segment_count=4,
    )

    tee.write("x\n" * 3000)
    tee.flush()
    tee.close()
    original.close()

    assert original_path.stat().st_size <= tee.segment_bytes


def test_bounded_stream_tee_line_buffered_original_flushes_on_newline(
    tmp_path: Path,
) -> None:
    """I4 (PR 98 cold review): writing straight to self._original.buffer
    (added to fix the CRLF cap overrun above) bypasses TextIOWrapper's own
    line-buffering entirely - stderr is line-buffered by default, so a
    diagnostic line written just before an uncatchable SIGKILL would sit
    unflushed in the underlying BufferedWriter's own (larger, not
    newline-triggered) buffer and never reach disk, defeating the entire
    reason this module exists. Read back through a SEPARATE file handle,
    with no explicit flush() call anywhere in this test, to prove the bytes
    actually reached the OS level rather than merely Python's own buffer."""
    original_path = tmp_path / "original-stderr.txt"
    original = original_path.open(
        "w", encoding="utf-8", newline=None, buffering=1
    )
    assert original.line_buffering
    base = tmp_path / "stderr.log"
    tee = wrapper_logs.BoundedStreamTee(
        original,
        base,
        max_bytes=4096,
        segment_count=4,
    )

    tee.write("final diagnostic before SIGKILL\n")
    # No tee.flush() / original.flush() here - simulating the kill landing
    # immediately after this write, before anything explicit could flush.
    on_disk = original_path.read_text(encoding="utf-8")

    tee.close()
    original.close()

    assert "final diagnostic before SIGKILL" in on_disk


def test_bounded_stream_tee_tail_flushes_after_each_write(tmp_path: Path) -> None:
    """I4 remnant (PR 98 cold review, round 3): the sibling test above fixed
    the ORIGINAL stream's flush - the tail ring's own files are raw binary
    BufferedWriters with no per-write flush of their own. Once the base
    segment's forwarding budget is spent, every further diagnostic write
    lands ONLY in the tail ring, so a small write can sit in that
    BufferedWriter's own internal buffer and never reach disk until an
    explicit flush()/close() or the buffer filling completely - the exact
    same durability gap, just one layer over. Read back through a SEPARATE
    file handle with zero explicit flush() calls anywhere in this test."""
    original = io.StringIO()
    base = tmp_path / "stderr.log"
    tee = wrapper_logs.BoundedStreamTee(
        original,
        base,
        max_bytes=4096,
        segment_count=4,
    )

    tee.write("tail diagnostic\n")
    tail_path = tmp_path / "stderr.log.1"
    on_disk = tail_path.read_text(encoding="utf-8")

    tee.close()

    assert "tail diagnostic" in on_disk


def test_bounded_stream_tee_failure_discards_excess_without_breaking_wrapper(
    tmp_path: Path,
) -> None:
    original = io.StringIO()
    blocked_parent = tmp_path / "blocked"
    blocked_parent.write_text("not a directory", encoding="utf-8")
    tee = wrapper_logs.BoundedStreamTee(
        original,
        blocked_parent / "stdout.log",
        max_bytes=4096,
        segment_count=4,
    )

    assert tee.write("x" * 10_000) == 10_000
    tee.flush()
    tee.close()

    assert len(original.getvalue().encode("utf-8")) == 1024


def test_bounded_stream_tee_tail_accounts_before_writing_not_after(
    tmp_path: Path,
) -> None:
    """Finding C (PR 98 connector re-review, head 6495534): a terminating
    signal's handler runs between bytecode instructions, so it can only land
    in the gap between the tail write and the size accounting that follows
    it - never inside the write call itself. Accounting AFTER the write
    leaves self._tail_size understated once the real bytes are already on
    disk if a signal lands in that gap, so the next write believes it has
    more room than it does and can push a segment past segment_bytes by up
    to another chunk. This interacts with the prior round's SIGTERM fix -
    making a terminating signal actually unwind (and log) promptly, instead
    of hanging, means a diagnostic write reaching this exact gap is now
    something normal termination can hit, not an exotic timing accident.

    Simulated directly rather than via a real OS signal: a tail whose
    write() raises must still have updated the accounting BEFORE that
    raise, proving accounting is ordered ahead of the write - the same
    ordering a signal's handler firing right after write() returns would
    otherwise be able to slip between."""
    original = io.StringIO()
    base = tmp_path / "stdout.log"
    tee = wrapper_logs.BoundedStreamTee(
        original,
        base,
        max_bytes=4096,
        segment_count=4,
    )
    tee._open_tail()
    real_tail = tee._tail

    class RaisingTail:
        def write(self, data: object) -> None:
            raise OSError("simulated interruption during the tail write")

    tee._tail = RaisingTail()
    size_before = tee._tail_size
    with pytest.raises(OSError):
        tee._write_tail(b"x" * 100)
    assert tee._tail_size == size_before + 100, (
        "the tail's size accounting was not updated before the write that "
        "raised - a signal landing in that gap would understate it instead"
    )

    tee._tail = real_tail
    tee.close()


def test_bounded_stream_tee_tail_rotates_before_splitting_a_utf8_code_point(
    tmp_path: Path,
) -> None:
    """Round 11 connector finding, the serious one: _write_tail sliced the
    encoded byte buffer at `available` with no regard for UTF-8 code point
    boundaries. When a multi-byte character's leading byte is the LAST byte
    that fits in the current segment, the old code split its encoded bytes
    across two files - the first segment ends with a dangling lead byte,
    the next begins with an orphaned continuation byte, and a strict UTF-8
    reader then fails to open EITHER file - not merely mis-render one
    character, the whole diagnostic becomes unopenable.

    Constructed at the byte level, not hoped into: 127 ASCII bytes exactly
    fill the segment to available=1, so e-acute (2-byte UTF-8) lands with
    its lead byte as the very last byte that would fit. Asserted on the
    raw bytes read back from disk, each segment decoded standalone - a
    string round-trip through Python would paper over exactly this."""
    original = io.StringIO()
    base = tmp_path / "stdout.log"
    tee = wrapper_logs.BoundedStreamTee(
        original,
        base,
        max_bytes=4096,
        segment_count=32,  # segment_bytes = 4096 // 32 = 128
    )
    assert tee.segment_bytes == 128

    payload = ("A" * 127) + "é" + "BB"  # 127 + 2 + 2 = 131 encoded bytes
    tee.write(payload)
    tee.close()

    seg1 = (tmp_path / "stdout.log.1").read_bytes()
    seg2 = (tmp_path / "stdout.log.2").read_bytes()

    seg1.decode("utf-8")
    seg2.decode("utf-8")

    assert seg1 == b"A" * 127
    assert seg2 == "éBB".encode("utf-8")


def test_signal_diagnostic_is_deferred_until_stream_write_unwinds(
    tmp_path: Path,
) -> None:
    original = io.StringIO()
    tee = wrapper_logs.BoundedStreamTee(
        original,
        tmp_path / "stderr.log",
        max_bytes=4096,
        segment_count=4,
    )
    lifecycle = wrapper_logs.WrapperLifecycleLog(
        "worker",
        stream=tee,
        clock=lambda: NOW,
    )

    lifecycle.defer_signal(signal.SIGTERM, terminating=True)

    tee._lock.acquire()
    try:
        assert original.getvalue() == ""
    finally:
        tee._lock.release()
    lifecycle.flush_deferred_signal()
    try:
        assert '"event":"wrapper_signal_received"' in original.getvalue()
    finally:
        tee.close()


def test_runtime_transitions_emit_only_factual_lifecycle_lines(
    tmp_path: Path,
) -> None:
    stream = io.StringIO()
    lifecycle = wrapper_logs.WrapperLifecycleLog(
        "worker",
        stream=stream,
        clock=lambda: NOW,
    )
    writer = runtime.WrapperRuntimeWriter(
        tmp_path,
        "worker",
        "generation-1",
        wrapper_pid=123,
        wrapper_start="start-123",
        clock=lambda: NOW,
        on_transition=lifecycle.runtime_transition,
    )

    writer.idle()
    writer.starting(message_id="msg-1", turn_id="turn-1")
    writer.active(456, "start-456")
    writer.progress()
    lifecycle.child_exited(456, "start-456", 7)
    writer.terminal(runtime.OUTCOME_FAILED)
    writer.idle()
    lifecycle.wrapper_exited(0, reason="loop_returned")

    rows = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [row["event"] for row in rows] == [
        "waiting_for_mail",
        "turn_started",
        "child_spawned",
        "child_exited",
        "turn_ended",
        "waiting_for_mail",
        "wrapper_exited",
    ]
    child_exit = rows[3]
    assert child_exit["agent"] == "worker"
    assert child_exit["wrapper_pid"] == 123
    assert child_exit["turn_generation"] == 1
    assert child_exit["turn_id"] == "turn-1"
    assert child_exit["cli_launcher_pid"] == 456
    assert child_exit["progress_sequence"] == 1
    assert child_exit["last_progress_at"] is not None
    assert child_exit["child_pid"] == 456
    assert child_exit["return_code"] == 7
    assert rows[4]["last_outcome"] == runtime.OUTCOME_FAILED
    assert all(
        forbidden not in stream.getvalue().casefold()
        for forbidden in (
            '"healthy"',
            '"ok"',
            '"alive"',
            '"progressing"',
            "working normally",
        )
    )


def test_lifecycle_sink_failure_never_breaks_runtime_transition(
    tmp_path: Path,
) -> None:
    class BrokenStream:
        def write(self, _value: str) -> int:
            raise OSError("simulated full disk")

        def flush(self) -> None:
            raise OSError("simulated full disk")

    lifecycle = wrapper_logs.WrapperLifecycleLog(
        "worker",
        stream=BrokenStream(),
        clock=lambda: NOW,
    )
    writer = runtime.WrapperRuntimeWriter(
        tmp_path,
        "worker",
        "generation-1",
        wrapper_pid=123,
        wrapper_start="start-123",
        clock=lambda: NOW,
        on_transition=lifecycle.runtime_transition,
    )

    record = writer.idle()

    assert record["phase"] == runtime.PHASE_IDLE
    assert runtime.read_runtime(
        tmp_path,
        "worker",
        now_epoch=NOW,
    )["status"] == runtime.STATUS_VALID


def test_mid_turn_exception_trail_keeps_turn_and_child_without_fabricated_end(
    tmp_path: Path,
) -> None:
    stream = io.StringIO()
    lifecycle = wrapper_logs.WrapperLifecycleLog(
        "worker",
        stream=stream,
        clock=lambda: NOW,
    )
    writer = runtime.WrapperRuntimeWriter(
        tmp_path,
        "worker",
        "generation-1",
        wrapper_pid=123,
        wrapper_start="start-123",
        clock=lambda: NOW,
        on_transition=lifecycle.runtime_transition,
    )
    writer.starting(message_id="msg-1", turn_id="turn-41")
    writer.active(456, "start-456")

    lifecycle.wrapper_exception(RuntimeError("simulated abrupt wrapper failure"))

    rows = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [row["event"] for row in rows] == [
        "turn_started",
        "child_spawned",
        "wrapper_exception",
    ]
    assert rows[-1]["turn_id"] == "turn-41"
    assert rows[-1]["cli_launcher_pid"] == 456
    assert rows[-1]["exception_type"] == "RuntimeError"
    assert "turn_ended" not in {row["event"] for row in rows}


def test_dead_letter_disposition_is_not_reported_as_a_driven_turn(
    tmp_path: Path,
) -> None:
    stream = io.StringIO()
    lifecycle = wrapper_logs.WrapperLifecycleLog(
        "worker",
        stream=stream,
        clock=lambda: NOW,
    )
    writer = runtime.WrapperRuntimeWriter(
        tmp_path,
        "worker",
        "generation-1",
        wrapper_pid=123,
        wrapper_start="start-123",
        clock=lambda: NOW,
        on_transition=lifecycle.runtime_transition,
    )

    writer.idle()
    writer.dead_letter(message_id="msg-dead")

    rows = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [row["event"] for row in rows] == [
        "waiting_for_mail",
        "message_dead_lettered",
    ]
    assert rows[-1]["message_id"] == "msg-dead"
    assert rows[-1]["last_outcome"] == runtime.OUTCOME_DEAD_LETTER


@pytest.mark.skipif(not hasattr(signal, "SIGTERM"), reason="SIGTERM unavailable")
def test_signal_logging_is_deferred_and_existing_handler_is_restored() -> None:
    stream = io.StringIO()
    lifecycle = wrapper_logs.WrapperLifecycleLog(
        "worker",
        stream=stream,
        clock=lambda: NOW,
        wrapper_pid=123,
    )
    calls: list[tuple[int, int]] = []

    def prior(signum: int, frame: object) -> None:
        calls.append((signum, len(stream.getvalue().splitlines())))

    old = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, prior)
    try:
        with wrapper_logs.capture_termination_signals(lifecycle):
            installed = signal.getsignal(signal.SIGTERM)
            assert callable(installed)
            installed(signal.SIGTERM, None)
        assert signal.getsignal(signal.SIGTERM) is prior
    finally:
        signal.signal(signal.SIGTERM, old)

    assert calls == [(signal.SIGTERM, 0)]
    row = json.loads(stream.getvalue())
    assert row["event"] == "wrapper_signal_received"
    assert row["signal"] == int(signal.SIGTERM)
    assert row["terminating"] is False
    assert lifecycle.terminal_emitted is False


@pytest.mark.skipif(not hasattr(signal, "SIGTERM"), reason="SIGTERM unavailable")
def test_ignored_signal_is_not_logged_as_wrapper_termination() -> None:
    stream = io.StringIO()
    lifecycle = wrapper_logs.WrapperLifecycleLog(
        "worker",
        stream=stream,
        clock=lambda: NOW,
        wrapper_pid=123,
    )
    old = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    try:
        with wrapper_logs.capture_termination_signals(lifecycle):
            installed = signal.getsignal(signal.SIGTERM)
            assert callable(installed)
            installed(signal.SIGTERM, None)
    finally:
        signal.signal(signal.SIGTERM, old)

    assert stream.getvalue() == ""
    assert lifecycle.terminal_emitted is False


def test_stream_environment_context_restores_process_streams(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_path = tmp_path / "stdout.log"
    err_path = tmp_path / "stderr.log"
    monkeypatch.setenv(wrapper_logs.ENV_STDOUT_PATH, str(out_path))
    monkeypatch.setenv(wrapper_logs.ENV_STDERR_PATH, str(err_path))
    monkeypatch.setenv(wrapper_logs.ENV_LAUNCH_NONCE, "a" * 32)
    before_out = io.StringIO()
    before_err = io.StringIO()
    monkeypatch.setattr("sys.stdout", before_out)
    monkeypatch.setattr("sys.stderr", before_err)

    with wrapper_logs.installed_standard_streams_from_environment(
        expected_nonce="a" * 32,
    ):
        import sys

        assert sys.stdout is not before_out
        assert sys.stderr is not before_err
        sys.stdout.write("child-output\n")
        sys.stderr.write("child-error\n")

    import sys

    assert sys.stdout is before_out
    assert sys.stderr is before_err
    assert "child-output" in "".join(
        path.read_text(encoding="utf-8") for path in tmp_path.glob("stdout.log*")
    )
    assert "child-error" in "".join(
        path.read_text(encoding="utf-8") for path in tmp_path.glob("stderr.log*")
    )
    assert os.environ[wrapper_logs.ENV_STDOUT_PATH] == str(out_path)


def test_cmd_wrap_records_setup_exception_before_loop_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_path = tmp_path / "stdout.log"
    err_path = tmp_path / "stderr.log"
    nonce = "a" * 32
    monkeypatch.setenv(wrapper_logs.ENV_STDOUT_PATH, str(out_path))
    monkeypatch.setenv(wrapper_logs.ENV_STDERR_PATH, str(err_path))
    monkeypatch.setenv(wrapper_logs.ENV_LAUNCH_NONCE, nonce)
    monkeypatch.setattr("sys.stdout", io.StringIO())
    monkeypatch.setattr("sys.stderr", io.StringIO())

    def fail_setup(_args: argparse.Namespace) -> int:
        raise RuntimeError("setup failed before run_loop")

    monkeypatch.setattr(cli, "_cmd_wrap_with_logging", fail_setup)
    args = argparse.Namespace(agent="worker", supervisor_launch_nonce=nonce)

    # New area (cold review): a bare re-raise here lets the original
    # RuntimeError reach main()'s uncaught-exception path, which prints its
    # OWN traceback AFTER streams are restored - to the unbounded raw file.
    # That is not a regression: the bounded diagnostic capture below (the
    # traceback that ends up IN the wrapper log tail) already happened,
    # via traceback.print_exc(file=sys.stderr), while sys.stderr was still
    # the bounded tee - BEFORE this raise. A second, redundant traceback on
    # the raw console afterward is exactly what an uncaught exception
    # should show an operator; #117's bounded capture does not depend on
    # suppressing it. Round 18: cmd_wrap propagates the ORIGINAL exception
    # here (not a converted SystemExit(1)) so an embedder or test runner
    # calling cli.main([...]) can catch RuntimeError specifically, inspect
    # it, and retry - the same contract main() had before this PR ever
    # touched cmd_wrap.
    with pytest.raises(RuntimeError, match="setup failed before run_loop"):
        cli.cmd_wrap(args)

    tail = "".join(
        path.read_text(encoding="utf-8") for path in tmp_path.glob("stderr.log*")
    )
    rows = [json.loads(line) for line in tail.splitlines() if line.startswith("{")]
    assert [row["event"] for row in rows] == ["wrapper_exception"]
    assert rows[0]["exception_type"] == "RuntimeError"
    # The traceback itself must also survive - it is captured through the
    # bounded stream before cmd_wrap's context manager restores raw streams.
    assert "setup failed before run_loop" in tail
    assert "Traceback (most recent call last)" in tail


def test_cmd_wrap_routine_system_exit_emits_exited_fact_without_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # cli._get_store's "not initialized" path writes its own actionable
    # diagnostic and calls sys.exit(2) directly - a routine, already-
    # explained exit, not a crash. Two regressions, in sequence:
    # (1) cmd_wrap's except block used to record wrapper_exception and
    # print a full Python traceback on top of it regardless of exception
    # type, turning a one-line diagnostic into crash-report noise; fixing
    # that by bare-raising on any SystemExit (2) overcorrected into
    # emitting NO lifecycle fact at all for this shape - no deferred signal
    # exists to have recorded one, unlike the signal-driven SystemExit
    # case - so the trail ended with no termination fact whatsoever,
    # indistinguishable from an OOM or a hard kill when reading the JSON
    # lines. Every termination path must emit exactly one termination
    # fact: here, a normalized wrapper_exited - not wrapper_exception,
    # since this was never an unexplained exception - and still no
    # traceback.
    out_path = tmp_path / "stdout.log"
    err_path = tmp_path / "stderr.log"
    nonce = "e" * 32
    monkeypatch.setenv(wrapper_logs.ENV_STDOUT_PATH, str(out_path))
    monkeypatch.setenv(wrapper_logs.ENV_STDERR_PATH, str(err_path))
    monkeypatch.setenv(wrapper_logs.ENV_LAUNCH_NONCE, nonce)
    monkeypatch.setattr("sys.stdout", io.StringIO())
    monkeypatch.setattr("sys.stderr", io.StringIO())

    def not_initialized(_args: argparse.Namespace) -> int:
        import sys

        sys.stderr.write(
            "agenttalk: not initialized at X\n"
            "Run `agenttalk init --here` from the project root.\n"
        )
        raise SystemExit(2)

    monkeypatch.setattr(cli, "_cmd_wrap_with_logging", not_initialized)
    args = argparse.Namespace(agent="worker", supervisor_launch_nonce=nonce)

    with pytest.raises(SystemExit) as exc_info:
        cli.cmd_wrap(args)
    assert exc_info.value.code == 2

    tail = "".join(
        path.read_text(encoding="utf-8") for path in tmp_path.glob("stderr.log*")
    )
    rows = [json.loads(line) for line in tail.splitlines() if line.startswith("{")]
    assert [row["event"] for row in rows] == ["wrapper_exited"]
    assert rows[0]["exit_code"] == 2
    assert rows[0]["reason"] == "system_exit"
    assert "not initialized" in tail
    assert "Traceback (most recent call last)" not in tail


@pytest.mark.parametrize(
    "raise_exc,expected_code,expected_reason,expected_text",
    [
        (lambda: KeyboardInterrupt(), 130, "keyboard_interrupt", "interrupted"),
        (lambda: ValueError("bad value"), 2, "mapped_cli_exception", "bad value"),
        (lambda: FileNotFoundError("missing.toml"), 2, "mapped_cli_exception",
         "missing.toml"),
        (lambda: OSError("disk full"), 2, "mapped_cli_exception", "disk full"),
    ],
    ids=["KeyboardInterrupt", "ValueError", "FileNotFoundError", "OSError"],
)
def test_cmd_wrap_routine_exception_types_skip_crash_reporting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raise_exc,
    expected_code: int,
    expected_reason: str,
    expected_text: str,
) -> None:
    # I2's shape, one layer up: main() already has a concise, actionable
    # diagnostic for KeyboardInterrupt and (ValueError, FileNotFoundError,
    # OSError) - cmd_wrap's except block converts them to the SAME exit
    # codes at the bottom, but the crash-reporting block above that
    # conversion ran unconditionally for anything that wasn't SystemExit,
    # so an OSError got wrapper_exception + a full Python traceback BEFORE
    # being converted to the CLI's normal one-line error. The property:
    # the crash path runs ONLY for exceptions not in this known,
    # concise-diagnostic set - enumerated as a set (this parametrize), not
    # patched type by type. A future exception type that gains a concise
    # diagnostic elsewhere and is not added here must fail this test, not
    # silently fall through to the crash path.
    #
    # Round 17 connector finding: this used to assert cmd_wrap RAISES
    # SystemExit(expected_code) for all four - which was itself the bug.
    # main() previously RETURNED an int for exactly these two classes
    # (KeyboardInterrupt, and ValueError/FileNotFoundError/OSError); a
    # raised SystemExit is not an Exception subclass, so it bypasses
    # main()'s own except clauses entirely and can escape a caller that
    # invokes cli.main([...]) programmatically expecting an int back
    # (an embedder, or a test runner). cmd_wrap now RETURNS the code
    # instead, restoring that contract regardless of whether cmd_wrap's
    # own exception handling sits in front of main()'s.
    out_path = tmp_path / "stdout.log"
    err_path = tmp_path / "stderr.log"
    nonce = "f" * 32
    monkeypatch.setenv(wrapper_logs.ENV_STDOUT_PATH, str(out_path))
    monkeypatch.setenv(wrapper_logs.ENV_STDERR_PATH, str(err_path))
    monkeypatch.setenv(wrapper_logs.ENV_LAUNCH_NONCE, nonce)
    monkeypatch.setattr("sys.stdout", io.StringIO())
    monkeypatch.setattr("sys.stderr", io.StringIO())

    def raiser(_args: argparse.Namespace) -> int:
        raise raise_exc()

    monkeypatch.setattr(cli, "_cmd_wrap_with_logging", raiser)
    args = argparse.Namespace(agent="worker", supervisor_launch_nonce=nonce)

    assert cli.cmd_wrap(args) == expected_code

    tail = "".join(
        path.read_text(encoding="utf-8") for path in tmp_path.glob("stderr.log*")
    )
    rows = [json.loads(line) for line in tail.splitlines() if line.startswith("{")]
    assert [row["event"] for row in rows] == ["wrapper_exited"]
    assert rows[0]["exit_code"] == expected_code
    assert rows[0]["reason"] == expected_reason
    assert expected_text in tail
    assert "Traceback (most recent call last)" not in tail


@pytest.mark.parametrize(
    "raise_exc,expected_return,expected_raise",
    [
        (lambda: SystemExit(2), None, SystemExit),
        (lambda: KeyboardInterrupt(), 130, None),
        (lambda: ValueError("bad value"), 2, None),
        (lambda: FileNotFoundError("missing.toml"), 2, None),
        (lambda: OSError("disk full"), 2, None),
        (lambda: RuntimeError("truly unexpected"), None, RuntimeError),
    ],
    ids=["SystemExit", "KeyboardInterrupt", "ValueError", "FileNotFoundError",
         "OSError", "RuntimeError"],
)
def test_cmd_wrap_and_main_exception_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raise_exc,
    expected_return: int | None,
    expected_raise: type[BaseException] | None,
) -> None:
    """Round 17/18 connector findings: the stated contract, tested end to
    end through cli.main() - not just cmd_wrap in isolation - because the
    property that actually matters is what an embedder or a test runner
    calling cli.main([...]) programmatically observes, and that can only
    be proven by calling main() itself.

    THE CONTRACT (exception classes that can reach cmd_wrap's handler):

    | class                        | cmd_wrap/main()   | lifecycle fact       | tb  |
    |-------------------------------|-------------------|-----------------------|-----|
    | SystemExit (deliberate)       | raises (same)     | wrapper_exited(code,"system_exit") | no |
    | KeyboardInterrupt             | returns 130       | wrapper_exited(130,"keyboard_interrupt") | no |
    | ValueError/FileNotFoundError/ | returns 2         | wrapper_exited(2,"mapped_cli_exception") | no |
    | OSError                       |                   |                       |     |
    | anything else (unexpected)    | raises (original) | wrapper_exception(exc) | yes |

    Every row now preserves main()'s PRE-#117 contract exactly - no
    behavior change anywhere except that rows 2 and 3 are now actually
    correct (they used to raise SystemExit, breaking main()'s contract
    for those two classes specifically):

    - SystemExit: main() has never caught bare SystemExit (no except
      clause for it, ever) - letting it propagate matches what happens
      with no exception handling here at all.
    - KeyboardInterrupt, ValueError/FileNotFoundError/OSError: main()
      RETURNED an int for these before this PR touched cmd_wrap.
      Raising SystemExit(code) for them bypassed main()'s own except
      clauses (SystemExit is not an Exception subclass) and let it
      escape a caller expecting an int back - round 17's fix.
    - Anything else (unexpected): main() had no RETURN contract for this
      class either - it let the ORIGINAL exception type propagate
      uncaught. Converting it to SystemExit(1) (round 17's own crash-
      design choice) destroyed the type information an embedder needs
      and substituted a class an ordinary `except Exception` would miss
      - round 18's fix: propagate the original after recording the
      crash fact, restoring the SAME pre-#117 status quo, not a new
      contract. The "one known exit code for any crash" goal was a
      CONSOLE concern the console gets for free (Python exits 1 on any
      uncaught exception regardless), so nothing is lost there either."""
    out_path = tmp_path / "stdout.log"
    err_path = tmp_path / "stderr.log"
    nonce = "3" * 32
    monkeypatch.setenv(wrapper_logs.ENV_STDOUT_PATH, str(out_path))
    monkeypatch.setenv(wrapper_logs.ENV_STDERR_PATH, str(err_path))
    monkeypatch.setenv(wrapper_logs.ENV_LAUNCH_NONCE, nonce)
    monkeypatch.setattr("sys.stdout", io.StringIO())
    monkeypatch.setattr("sys.stderr", io.StringIO())

    def raiser(_args: argparse.Namespace) -> int:
        raise raise_exc()

    monkeypatch.setattr(cli, "_cmd_wrap_with_logging", raiser)

    argv = ["--supervisor-launch-nonce", nonce, "wrap", "--for", "worker"]
    if expected_raise is SystemExit:
        with pytest.raises(SystemExit) as exc_info:
            cli.main(argv)
        assert exc_info.value.code == 2
    elif expected_raise is not None:
        with pytest.raises(expected_raise):
            cli.main(argv)
    else:
        assert cli.main(argv) == expected_return


def test_cmd_wrap_unclassified_exception_still_gets_crash_reporting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The other half of the property: an exception type NOT in the known,
    # concise-diagnostic set must still fall through to the crash path -
    # this is the visible-omission guard the enumeration exists for.
    #
    # Round 18: this used to assert cmd_wrap converts the exception to
    # SystemExit(1) - the same contract violation as rows 2/3 (KeyboardInterrupt,
    # ValueError/FileNotFoundError/OSError), one row down. main() never had
    # a RETURN contract for an unexpected type - it would have let the
    # ORIGINAL type propagate uncaught - so cmd_wrap must propagate the
    # original RuntimeError here too, after recording the crash fact, not
    # substitute a SystemExit an embedder's `except Exception` would miss.
    out_path = tmp_path / "stdout.log"
    err_path = tmp_path / "stderr.log"
    nonce = "9" * 32
    monkeypatch.setenv(wrapper_logs.ENV_STDOUT_PATH, str(out_path))
    monkeypatch.setenv(wrapper_logs.ENV_STDERR_PATH, str(err_path))
    monkeypatch.setenv(wrapper_logs.ENV_LAUNCH_NONCE, nonce)
    monkeypatch.setattr("sys.stdout", io.StringIO())
    monkeypatch.setattr("sys.stderr", io.StringIO())

    def raiser(_args: argparse.Namespace) -> int:
        raise RuntimeError("truly unexpected")

    monkeypatch.setattr(cli, "_cmd_wrap_with_logging", raiser)
    args = argparse.Namespace(agent="worker", supervisor_launch_nonce=nonce)

    with pytest.raises(RuntimeError, match="truly unexpected"):
        cli.cmd_wrap(args)

    tail = "".join(
        path.read_text(encoding="utf-8") for path in tmp_path.glob("stderr.log*")
    )
    rows = [json.loads(line) for line in tail.splitlines() if line.startswith("{")]
    assert [row["event"] for row in rows] == ["wrapper_exception"]
    assert rows[0]["exception_type"] == "RuntimeError"
    assert "Traceback (most recent call last)" in tail


def test_cmd_wrap_entry_bounds_python_level_wrapper_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_path = tmp_path / "stdout.log"
    err_path = tmp_path / "stderr.log"
    out_path.write_text("B" * 1024, encoding="utf-8")
    err_path.write_text("", encoding="utf-8")
    nonce = "c" * 32
    monkeypatch.setenv(wrapper_logs.ENV_STDOUT_PATH, str(out_path))
    monkeypatch.setenv(wrapper_logs.ENV_STDERR_PATH, str(err_path))
    monkeypatch.setenv(wrapper_logs.ENV_LAUNCH_NONCE, nonce)
    monkeypatch.setenv(wrapper_logs.ENV_MAX_BYTES, "4096")
    monkeypatch.setenv(wrapper_logs.ENV_SEGMENT_COUNT, "4")
    monkeypatch.setattr("sys.stdout", io.StringIO())
    monkeypatch.setattr("sys.stderr", io.StringIO())

    def emit_output(_args: argparse.Namespace) -> int:
        import sys

        sys.stdout.write("x" * 20_000)
        sys.stdout.write("FINAL-SENTINEL\n")
        return 0

    monkeypatch.setattr(cli, "_cmd_wrap_with_logging", emit_output)
    args = argparse.Namespace(agent="worker", supervisor_launch_nonce=nonce)

    assert cli.cmd_wrap(args) == 0

    files = sorted(tmp_path.glob("stdout.log*"))
    assert all(path.stat().st_size <= 1024 for path in files)
    assert sum(path.stat().st_size for path in files) <= 4096
    assert "FINAL-SENTINEL" in "".join(
        path.read_text(encoding="utf-8") for path in files
    )


def test_cmd_wrap_uncaught_exception_traceback_is_bounded_and_in_tail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_path = tmp_path / "stdout.log"
    err_path = tmp_path / "stderr.log"
    out_path.write_text("", encoding="utf-8")
    err_path.write_text("E" * 1024, encoding="utf-8")
    nonce = "d" * 32
    monkeypatch.setenv(wrapper_logs.ENV_STDOUT_PATH, str(out_path))
    monkeypatch.setenv(wrapper_logs.ENV_STDERR_PATH, str(err_path))
    monkeypatch.setenv(wrapper_logs.ENV_LAUNCH_NONCE, nonce)
    monkeypatch.setenv(wrapper_logs.ENV_MAX_BYTES, "4096")
    monkeypatch.setenv(wrapper_logs.ENV_SEGMENT_COUNT, "4")
    monkeypatch.setattr("sys.stdout", io.StringIO())
    monkeypatch.setattr("sys.stderr", io.StringIO())

    def blow_up(_args: argparse.Namespace) -> int:
        import sys

        # The initial segment is already full; more chatter before the crash
        # forces several rotations, so the eventual traceback lands in a
        # tail segment rather than the (already-evicted) first one.
        sys.stderr.write("x" * 20_000)
        raise RuntimeError("TRACEBACK-SENTINEL-BOOM")

    monkeypatch.setattr(cli, "_cmd_wrap_with_logging", blow_up)
    args = argparse.Namespace(agent="worker", supervisor_launch_nonce=nonce)

    # Round 18: cmd_wrap now propagates the ORIGINAL RuntimeError rather
    # than converting it to SystemExit(1) - the bounded capture below is
    # unaffected either way, since it happens before this raise.
    with pytest.raises(RuntimeError, match="TRACEBACK-SENTINEL-BOOM"):
        cli.cmd_wrap(args)

    files = sorted(tmp_path.glob("stderr.log*"))
    assert all(path.stat().st_size <= 1024 for path in files)
    assert sum(path.stat().st_size for path in files) <= 4096
    tail = "".join(
        path.read_text(encoding="utf-8", errors="replace") for path in files
    )
    assert "TRACEBACK-SENTINEL-BOOM" in tail
    assert "Traceback (most recent call last)" in tail


def test_print_bounded_uncaught_exception_writes_into_tail_ring_when_config_is_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_path = tmp_path / "stdout.log"
    err_path = tmp_path / "stderr.log"
    err_path.write_text("E" * 1024, encoding="utf-8")
    nonce = "f" * 32
    monkeypatch.setenv(wrapper_logs.ENV_STDOUT_PATH, str(out_path))
    monkeypatch.setenv(wrapper_logs.ENV_STDERR_PATH, str(err_path))
    monkeypatch.setenv(wrapper_logs.ENV_LAUNCH_NONCE, nonce)
    monkeypatch.setenv(wrapper_logs.ENV_MAX_BYTES, "4096")
    monkeypatch.setenv(wrapper_logs.ENV_SEGMENT_COUNT, "4")
    monkeypatch.setattr("sys.stdout", io.StringIO())
    monkeypatch.setattr("sys.stderr", io.StringIO())
    monkeypatch.setattr(wrapper_logs, "_LAST_STDERR_LOG_CONFIG", None)

    # Simulate cmd_wrap's own tee lifecycle: installed, then torn down
    # normally by the unconditional finally in installed_standard_streams_
    # from_environment - exactly the state the tee is in by the time
    # agenttalk/__main__.py's top-level fallback would ever run.
    with wrapper_logs.installed_standard_streams_from_environment(
        expected_nonce=nonce,
    ):
        pass

    try:
        raise RuntimeError("TOP-LEVEL-SENTINEL-BOOM")
    except RuntimeError:
        wrapper_logs.print_bounded_uncaught_exception()

    files = sorted(tmp_path.glob("stderr.log*"))
    assert all(path.stat().st_size <= 1024 for path in files)
    assert sum(path.stat().st_size for path in files) <= 4096
    tail = "".join(
        path.read_text(encoding="utf-8", errors="replace") for path in files
    )
    assert "TOP-LEVEL-SENTINEL-BOOM" in tail
    assert "Traceback (most recent call last)" in tail
    # The raw file the supervisor redirects to must not receive this second
    # copy directly - only the bounded tail ring does.
    assert "TOP-LEVEL-SENTINEL-BOOM" not in err_path.read_text(encoding="utf-8")


def test_print_bounded_uncaught_exception_prints_normally_when_no_wrapper_log_is_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wrapper_logs, "_LAST_STDERR_LOG_CONFIG", None)
    captured = io.StringIO()
    monkeypatch.setattr("sys.stderr", captured)

    try:
        raise RuntimeError("MANUAL-RUN-SENTINEL")
    except RuntimeError:
        wrapper_logs.print_bounded_uncaught_exception()

    assert "MANUAL-RUN-SENTINEL" in captured.getvalue()
    assert "Traceback (most recent call last)" in captured.getvalue()


def test_top_level_boundary_keeps_second_traceback_within_cap_and_preserves_exception_type(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_path = tmp_path / "stdout.log"
    err_path = tmp_path / "stderr.log"
    err_path.write_text("E" * 1024, encoding="utf-8")
    nonce = "a" * 32
    monkeypatch.setenv(wrapper_logs.ENV_STDOUT_PATH, str(out_path))
    monkeypatch.setenv(wrapper_logs.ENV_STDERR_PATH, str(err_path))
    monkeypatch.setenv(wrapper_logs.ENV_LAUNCH_NONCE, nonce)
    monkeypatch.setenv(wrapper_logs.ENV_MAX_BYTES, "4096")
    monkeypatch.setenv(wrapper_logs.ENV_SEGMENT_COUNT, "4")
    monkeypatch.setattr("sys.stdout", io.StringIO())
    monkeypatch.setattr("sys.stderr", io.StringIO())
    monkeypatch.setattr(wrapper_logs, "_LAST_STDERR_LOG_CONFIG", None)

    def blow_up(_args: argparse.Namespace) -> int:
        import sys

        sys.stderr.write("x" * 20_000)
        raise RuntimeError("BOUNDARY-SENTINEL-BOOM " + "z" * 20_000)

    monkeypatch.setattr(cli, "_cmd_wrap_with_logging", blow_up)

    # Mirrors agenttalk/__main__.py's own dispatch: everything but SystemExit
    # that escapes main() is genuinely uncaught, so this is the boundary
    # print_bounded_uncaught_exception exists for. cli.main() itself is
    # untouched by this fix - an embedder calling it directly still gets the
    # original exception type back, which this also confirms.
    try:
        cli.main(
            ["--supervisor-launch-nonce", nonce, "wrap", "--for", "worker"]
        )
    except SystemExit as exc:
        raise AssertionError(
            "an unexpected exception must not be converted to SystemExit"
        ) from exc
    except RuntimeError as exc:
        assert "BOUNDARY-SENTINEL-BOOM" in str(exc)
        wrapper_logs.print_bounded_uncaught_exception()
    else:
        raise AssertionError("cli.main should have raised")

    files = sorted(tmp_path.glob("stderr.log*"))
    assert all(path.stat().st_size <= 1024 for path in files)
    assert sum(path.stat().st_size for path in files) <= 4096


def test_cmd_wrap_records_terminating_signal_without_exception_duplicate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_path = tmp_path / "stdout.log"
    err_path = tmp_path / "stderr.log"
    nonce = "b" * 32
    monkeypatch.setenv(wrapper_logs.ENV_STDOUT_PATH, str(out_path))
    monkeypatch.setenv(wrapper_logs.ENV_STDERR_PATH, str(err_path))
    monkeypatch.setenv(wrapper_logs.ENV_LAUNCH_NONCE, nonce)
    monkeypatch.setattr("sys.stdout", io.StringIO())
    monkeypatch.setattr("sys.stderr", io.StringIO())

    def signal_during_setup(_args: argparse.Namespace) -> int:
        installed = signal.getsignal(signal.SIGTERM)
        assert callable(installed)
        installed(signal.SIGTERM, None)
        raise AssertionError("terminating signal handler returned")

    monkeypatch.setattr(cli, "_cmd_wrap_with_logging", signal_during_setup)
    args = argparse.Namespace(agent="worker", supervisor_launch_nonce=nonce)

    with pytest.raises(SystemExit):
        cli.cmd_wrap(args)

    rows = [
        json.loads(line)
        for path in tmp_path.glob("stderr.log*")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert [row["event"] for row in rows] == ["wrapper_signal_received"]
    assert rows[0]["signal"] == int(signal.SIGTERM)
    assert rows[0]["terminating"] is True


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_stream_environment_hardens_sensitive_log_permissions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation = tmp_path / "project" / "worker" / "generation"
    generation.mkdir(parents=True, mode=0o755)
    out_path = generation / "stdout.log"
    err_path = generation / "stderr.log"
    out_path.write_text("", encoding="utf-8")
    err_path.write_text("", encoding="utf-8")
    out_path.chmod(0o644)
    err_path.chmod(0o644)
    monkeypatch.setenv(wrapper_logs.ENV_STDOUT_PATH, str(out_path))
    monkeypatch.setenv(wrapper_logs.ENV_STDERR_PATH, str(err_path))
    monkeypatch.setenv(wrapper_logs.ENV_LAUNCH_NONCE, "a" * 32)
    monkeypatch.setattr("sys.stdout", io.StringIO())
    monkeypatch.setattr("sys.stderr", io.StringIO())

    with wrapper_logs.installed_standard_streams_from_environment(
        expected_nonce="a" * 32,
    ):
        pass

    assert stat.S_IMODE(out_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(err_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(generation.stat().st_mode) == 0o700
    assert stat.S_IMODE(generation.parent.stat().st_mode) == 0o700


def test_ambient_log_paths_without_matching_launch_nonce_are_ignored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_path = tmp_path / "must-not-open.stdout"
    err_path = tmp_path / "must-not-open.stderr"
    monkeypatch.setenv(wrapper_logs.ENV_STDOUT_PATH, str(out_path))
    monkeypatch.setenv(wrapper_logs.ENV_STDERR_PATH, str(err_path))
    monkeypatch.setenv(wrapper_logs.ENV_LAUNCH_NONCE, "a" * 32)
    before_out = io.StringIO()
    before_err = io.StringIO()
    monkeypatch.setattr("sys.stdout", before_out)
    monkeypatch.setattr("sys.stderr", before_err)

    with wrapper_logs.installed_standard_streams_from_environment(
        expected_nonce="b" * 32,
    ):
        import sys

        assert sys.stdout is before_out
        assert sys.stderr is before_err

    assert not out_path.exists()
    assert not err_path.exists()
    lifecycle = wrapper_logs.WrapperLifecycleLog.from_environment(
        "worker",
        expected_nonce="b" * 32,
    )
    assert lifecycle.enabled is False
