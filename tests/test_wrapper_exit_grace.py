"""#683: once the model child has exited and its turn has finished, a leftover process that inherited
the wrapper's output pipe must not keep `agenttalk wrap` (plain or --loop) waiting for end of output.

Every test runs the wrapper in a thread with its own hard timeout, so a regression fails instead of
hanging, and ends every process it started by its exact process id."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from agenttalk.store import Store
from agenttalk.wrapper import run, session
from agenttalk.wrapper.events import EventType
from agenttalk.wrapper.turn_watchdog import TurnWatchdogConfig

pytestmark = pytest.mark.subprocess

# getattr keeps these tests runnable against a wrapper without the grace, where they must fail.
GRACE = getattr(run, "OUTPUT_GRACE_AFTER_EXIT_SECONDS", 2.0)
HARD_TIMEOUT = GRACE + 10.0
SLOW = GRACE + 2.0  # longer than the grace, so a grace started too early ends the read too soon

# A stand-in model: it prints a `codex exec --json` turn, then does what its mode asks.
MODEL = textwrap.dedent(r'''
    import json, subprocess, sys, time

    mode, pidfile, slow = sys.argv[1], sys.argv[2], float(sys.argv[3])
    SLEEPER = [sys.executable, "-c", "import time; time.sleep(120)"]
    NULL = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def say(obj):
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()

    def message(text):
        say({"type": "item.completed", "item": {"type": "agent_message", "text": text}})

    def turn():
        say({"type": "turn.started"})
        message("done")
        say({"type": "turn.completed"})

    def spawn(argv, **kwargs):
        child = subprocess.Popen(argv, **kwargs)
        with open(pidfile, "a", encoding="utf-8") as handle:
            handle.write(f"{child.pid}\n")

    def again(next_mode):
        return [sys.executable, __file__, next_mode, pidfile, str(slow)]

    if mode.startswith("survivor-"):
        turn()
        spawn(SLEEPER, **{
            "survivor-nofds-null": dict(close_fds=False, **NULL),
            "survivor-nofds-inherit": dict(close_fds=False),
            "survivor-default-inherit": {},
            "survivor-default-null": NULL,
        }[mode])
    elif mode == "handoff":           # a launcher that hands the turn to a child and exits at once
        spawn(again("brain"))
    elif mode == "brain":
        say({"type": "turn.started"})
        time.sleep(slow)
        message("done")
        say({"type": "turn.completed"})
    elif mode == "late-tail":         # a helper still writing after the model has exited
        turn()
        spawn(again("tail-after"))
    elif mode == "tail-after":
        time.sleep(0.5)
        message("late")
    elif mode == "quiet-then-tail":   # the model finishes its turn but keeps running, quietly
        turn()
        time.sleep(slow)
        message("tail")
    elif mode == "partial-survivor":  # a last line without a newline, then a survivor
        turn()
        sys.stdout.write("partial")
        sys.stdout.flush()
        spawn(SLEEPER)
''')

SURVIVORS = [
    "survivor-nofds-null",      # close_fds=False, stdio on null: shares the pipe on Windows
    "survivor-nofds-inherit",   # close_fds=False, stdio inherited
    "survivor-default-inherit",  # default close_fds, stdio inherited: Windows, and stdout on POSIX
]
CONTROL = "survivor-default-null"  # stdio on null: never shares the pipe


@pytest.fixture
def model(tmp_path: Path) -> Path:
    path = tmp_path / "model.py"
    path.write_text(MODEL, encoding="utf-8")
    return path


def _argv(model: Path, mode: str, pidfile: Path) -> list[str]:
    return [sys.executable, str(model), mode, str(pidfile), str(SLOW)]


def _pids(pidfile: Path) -> list[int]:
    return [int(line) for line in pidfile.read_text(encoding="utf-8").split()] if pidfile.exists() else []


def _alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        kernel = ctypes.windll.kernel32
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _end_all(pidfile: Path) -> None:
    """Stop every process the stand-in started, by its exact id, and prove each one is gone."""
    for pid in _pids(pidfile):
        if _alive(pid):
            os.kill(pid, signal.SIGTERM)  # TerminateProcess on Windows
        deadline = time.monotonic() + 10.0
        while _alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _alive(pid), f"process {pid} started by the test is still running"


def _bounded(call):
    """Run call in a thread for at most HARD_TIMEOUT; a wrapper still waiting is a failure, not a hang."""
    box: dict = {}

    def target() -> None:
        try:
            box["result"] = call()
        except BaseException as exc:  # noqa: BLE001 - reported by the test
            box["error"] = exc

    worker = threading.Thread(target=target, daemon=True)
    started = time.monotonic()
    worker.start()
    worker.join(HARD_TIMEOUT)
    return worker, box, time.monotonic() - started


def _run_plain(model: Path, mode: str, pidfile: Path, seen: list) -> int:
    return run.run_wrapper(cli="codex", agent="worker", argv=_argv(model, mode, pidfile), render=False,
                           min_interval=0.0, heartbeat_fn=lambda event, now: seen.append(event),
                           restart_fn=lambda sig: None)


def _finished_messages(seen: list) -> tuple[bool, list[str]]:
    texts = [((event.raw or {}).get("item") or {}).get("text") for event in seen]
    return any(event.type == EventType.TURN_FINISHED for event in seen), [t for t in texts if t]


@pytest.mark.parametrize("mode", [*SURVIVORS, CONTROL])
def test_plain_wrap_stops_waiting_after_the_grace_when_a_survivor_holds_the_pipe(
        model: Path, tmp_path: Path, mode: str) -> None:
    pidfile, seen = tmp_path / "pids.txt", []
    try:
        worker, box, elapsed = _bounded(lambda: _run_plain(model, mode, pidfile, seen))
        survivors_alive = [pid for pid in _pids(pidfile) if _alive(pid)]
    finally:
        _end_all(pidfile)
        worker.join(10.0)
    assert not worker.is_alive() and elapsed < HARD_TIMEOUT, "wrap was still waiting for end of output"
    assert box.get("result") == 0, box
    assert _finished_messages(seen) == (True, ["done"])
    assert len(survivors_alive) == 1, "the survivor is left running, not killed"
    if mode == CONTROL:
        assert elapsed < GRACE, f"a normal end of output must not wait for the grace ({elapsed:.2f} s)"


def _drive(store: Store, model: Path, mode: str, pidfile: Path, *, watchdog=None):
    def spawn(argv, stdin):  # the stand-in ignores the prompt, so none is written to it
        return run._ProcStream(_argv(model, mode, pidfile), None, watchdog=watchdog,
                               watchdog_snapshot_fn=lambda: None, watchdog_kill_fn=lambda pids: [])

    drive = run.make_drive(store, "beta", "codex", session.SessionState(cli="codex"), ["codex"], spawn=spawn,
                           clock=time.monotonic, render=False)
    return drive({"id": "msg-1", "from": "alpha", "to": "beta", "kind": "question", "subject": "s",
                  "body": "b", "correlation_id": "q-1", "request_id": "q-1", "broadcast_id": None,
                  "meta": {"request_id": "q-1"}})


@pytest.mark.parametrize("watchdog", [None, TurnWatchdogConfig(enabled=True)], ids=["plain-stream", "watchdog"])
@pytest.mark.parametrize("mode", [*SURVIVORS, CONTROL])
def test_loop_turn_stops_waiting_after_the_grace_when_a_survivor_holds_the_pipe(
        model: Path, tmp_path: Path, mode: str, watchdog) -> None:
    store = Store(tmp_path / "store")
    store.init(["alpha", "beta"])
    pidfile = tmp_path / "pids.txt"
    try:
        worker, box, elapsed = _bounded(lambda: _drive(store, model, mode, pidfile, watchdog=watchdog))
        survivors_alive = [pid for pid in _pids(pidfile) if _alive(pid)]
    finally:
        _end_all(pidfile)
        worker.join(10.0)
    assert not worker.is_alive() and elapsed < HARD_TIMEOUT, "wrap --loop was still waiting for end of output"
    assert box.get("result") is not None and box["result"].ok is True, box
    assert len(survivors_alive) == 1, "the survivor is left running, not killed"
    if mode == CONTROL:
        assert elapsed < GRACE + 1.0, f"a normal turn must not wait for the grace ({elapsed:.2f} s)"


def test_plain_wrap_reads_a_handed_off_turn_to_its_end(model: Path, tmp_path: Path) -> None:
    """A launcher that hands the turn to a child and exits at once: its exit alone must not start the
    grace, or the turn would be cut off while the real model is still working."""
    pidfile, seen = tmp_path / "pids.txt", []
    try:
        worker, box, elapsed = _bounded(lambda: _run_plain(model, "handoff", pidfile, seen))
    finally:
        _end_all(pidfile)
        worker.join(10.0)
    assert not worker.is_alive(), "wrap was still waiting for end of output"
    assert box.get("result") == 0, box
    assert _finished_messages(seen) == (True, ["done"]), "the handed-off turn was cut short"
    assert elapsed >= SLOW


def _read_lines(model: Path, mode: str, pidfile: Path) -> tuple[bool, dict]:
    """Read the stand-in's output as the wrapper does, with the turn already finished."""
    proc = subprocess.Popen(_argv(model, mode, pidfile), stdout=subprocess.PIPE, text=True, encoding="utf-8")
    try:
        worker, box, _elapsed = _bounded(lambda: list(run._iter_output_lines(proc, lambda: True)))
        timed_out = worker.is_alive()
    finally:
        _end_all(pidfile)
        proc.wait(10.0)
        worker.join(10.0)  # never close a pipe under a reader that is still running
        proc.stdout.close()
    return timed_out, box


def test_output_still_arriving_after_the_model_exits_is_kept(model: Path, tmp_path: Path) -> None:
    timed_out, box = _read_lines(model, "late-tail", tmp_path / "pids.txt")
    assert not timed_out
    assert any('"late"' in line for line in box["result"]), "output that arrived within the grace was dropped"


def test_the_grace_does_not_start_while_the_model_is_still_running(model: Path, tmp_path: Path) -> None:
    timed_out, box = _read_lines(model, "quiet-then-tail", tmp_path / "pids.txt")
    assert not timed_out
    assert any('"tail"' in line for line in box["result"]), "the read ended before the model exited"


def test_a_last_line_without_a_newline_is_kept_when_the_grace_ends(model: Path, tmp_path: Path) -> None:
    timed_out, box = _read_lines(model, "partial-survivor", tmp_path / "pids.txt")
    assert not timed_out, "the read was still waiting for end of output"
    assert box["result"][-1] == "partial"
