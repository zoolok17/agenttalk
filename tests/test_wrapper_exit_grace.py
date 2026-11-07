"""#683: once the model child has exited and its turn has finished, a leftover process that inherited
the wrapper's output pipe must not keep `agenttalk wrap` (plain or --loop) waiting for end of output.

Every test runs the wrapper in a thread with its own hard timeout, so a regression fails instead of
hanging, and ends every process it started by its exact process id.

Stated limit: a model that exits WITHOUT finishing its turn (no turn-end event) while a leftover process
holds the pipe still keeps the wrapper waiting, as before; the supervisor's stuck-turn recovery is its bound.
The grace cannot start at exit alone, because a launcher can hand off and exit early (see the hand-off test)."""

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
    # Logs a line every 0.3 s, like a dev server. Once nobody reads its output it ignores the failed
    # writes and keeps running, so a test can see that the wrapper did not stop it.
    WRITER = [sys.executable, "-c", "import time\nfor i in range(400):\n    try:\n"
              "        print('GET /', i, flush=True)\n    except OSError:\n        pass\n    time.sleep(0.3)"]
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
    elif mode == "writing":  # a leftover that keeps writing after the turn's end
        turn()
        spawn(WRITER)
    elif mode == "burst-then-writing":  # many last lines just before the end event, then a writer
        say({"type": "turn.started"})
        for i in range(3000):
            message(f"line {i}")
        say({"type": "turn.completed"})
        spawn(WRITER)
    elif mode == "claude-handoff":    # a Claude launcher that hands off; the turn calls a tool first
        spawn(again("claude-brain"))
    elif mode == "claude-brain":
        def stream(event):
            say({"type": "stream_event", "event": event})
        stream({"type": "message_start"})
        stream({"type": "content_block_start", "content_block": {"type": "tool_use", "name": "Bash"}})
        stream({"type": "message_stop"})  # the tool-use message ends; the invocation does not
        time.sleep(slow)                   # the tool runs for longer than the grace
        stream({"type": "message_start"})
        say({"type": "assistant", "message": {"content": [{"type": "text", "text": "final answer"}]}})
        stream({"type": "message_stop"})
        say({"type": "result", "subtype": "success", "is_error": False, "result": "final answer"})
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
    try:  # a zombie has ended: a container's PID 1 may never reap the helpers it adopts
        with open(f"/proc/{pid}/stat", encoding="ascii", errors="replace") as stat:
            return stat.read().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
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


def _run_plain(model: Path, mode: str, pidfile: Path, seen: list, cli: str = "codex") -> int:
    return run.run_wrapper(cli=cli, agent="worker", argv=_argv(model, mode, pidfile), render=False,
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


def _drive(store: Store, model: Path, mode: str, pidfile: Path, *, watchdog=None, cli: str = "codex"):
    def spawn(argv, stdin):  # the stand-in ignores the prompt, so none is written to it
        return run._ProcStream(_argv(model, mode, pidfile), None, watchdog=watchdog,
                               watchdog_snapshot_fn=lambda: None, watchdog_kill_fn=lambda pids: [])

    state = session.SessionState(cli=cli, claude_session_id="sid-1" if cli == "claude" else None)
    drive = run.make_drive(store, "beta", cli, state, [cli], spawn=spawn,
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


@pytest.mark.parametrize("how", ["plain", "loop"])
def test_a_handed_off_claude_turn_is_read_past_its_tool_use_to_the_final_answer(
        model: Path, tmp_path: Path, how: str) -> None:
    """Claude reports turn_finished for each assistant message, a tool-use one too. Only the final
    `result` ends the invocation, so a tool that runs longer than the grace must not end the read."""
    pidfile, seen = tmp_path / "pids.txt", []
    if how == "plain":
        def call():
            return _run_plain(model, "claude-handoff", pidfile, seen, cli="claude")
    else:
        store = Store(tmp_path / "store")
        store.init(["alpha", "beta"])

        def call():
            return _drive(store, model, "claude-handoff", pidfile, cli="claude")
    try:
        worker, box, elapsed = _bounded(call)
    finally:
        _end_all(pidfile)
        worker.join(10.0)
    assert not worker.is_alive(), "wrap was still waiting for end of output"
    assert elapsed >= SLOW, f"the read ended before the final answer ({elapsed:.2f} s)"
    if how == "plain":
        assert box.get("result") == 0, box
        assert any(event.text == "final answer" for event in seen), "the final answer was dropped"
    else:
        assert box["result"].ok is True, box


@pytest.mark.parametrize("how", ["plain", "loop", "loop-watchdog"])
def test_a_survivor_that_keeps_writing_does_not_keep_wrap_waiting(model: Path, tmp_path: Path, how: str) -> None:
    """The grace is a hard bound from the turn's end: output still arriving then belongs to the survivor."""
    pidfile, seen = tmp_path / "pids.txt", []
    if how == "plain":
        def call():
            return _run_plain(model, "writing", pidfile, seen)
    else:
        store = Store(tmp_path / "store")
        store.init(["alpha", "beta"])
        watchdog = TurnWatchdogConfig(enabled=True) if how == "loop-watchdog" else None

        def call():
            return _drive(store, model, "writing", pidfile, watchdog=watchdog)
    try:
        worker, box, elapsed = _bounded(call)
        survivors_alive = [pid for pid in _pids(pidfile) if _alive(pid)]
    finally:
        _end_all(pidfile)
        worker.join(10.0)
    assert not worker.is_alive() and elapsed < GRACE + 3.0, f"wrap kept reading a writing survivor ({elapsed:.2f} s)"
    assert box.get("result") == 0 if how == "plain" else box["result"].ok is True, box
    assert len(survivors_alive) == 1, "the writing survivor is left running, not stopped"


def test_the_last_lines_before_the_turn_end_are_kept(model: Path, tmp_path: Path) -> None:
    pidfile, seen = tmp_path / "pids.txt", []
    try:
        worker, box, elapsed = _bounded(lambda: _run_plain(model, "burst-then-writing", pidfile, seen))
    finally:
        _end_all(pidfile)
        worker.join(10.0)
    assert not worker.is_alive() and elapsed < GRACE + 3.0, f"wrap kept reading a writing survivor ({elapsed:.2f} s)"
    assert box.get("result") == 0, box
    assert _finished_messages(seen) == (True, [f"line {i}" for i in range(3000)])


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
