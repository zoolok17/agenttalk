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

    with pytest.raises(RuntimeError, match="setup failed"):
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
