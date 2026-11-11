"""#683: once the model child has exited and its turn has finished, a leftover process that inherited
the wrapper's output pipe must not keep `agenttalk wrap` (plain or --loop) waiting for end of output.

Every test runs the wrapper in a thread with its own hard timeout, so a regression fails instead of
hanging, and ends every process it started by its exact process id.

Stated limit: a model that exits WITHOUT finishing its turn (no turn-end event) while a leftover process
holds the pipe still keeps the wrapper waiting, as before; the supervisor's stuck-turn recovery is its bound.
The grace cannot start at exit alone, because a launcher can hand off and exit early (see the hand-off test).
The end-of-run record must also arrive as a complete, newline-terminated line for the grace to start: a final
record without its line ending, while a leftover process holds the pipe, still waits, as on master."""

from __future__ import annotations

import errno
import gc
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
import types
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
    elif mode == "odd-codex":         # events whose type is a list or an object, then a valid turn
        say({"type": []})
        say({"type": {}})
        turn()
    elif mode == "odd-claude":
        say({"type": []})
        say({"type": {}})
        say({"type": "stream_event", "event": {"type": "message_start"}})
        say({"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}]}})
        say({"type": "stream_event", "event": {"type": "message_stop"}})
        say({"type": "result", "subtype": "success", "is_error": False, "result": "done"})
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


def _state(pid: int) -> str:
    """ "alive", "ended" or "unknown". A process counts as ended only on positive evidence; a probe that could not
    look (access denied, a failed query) says "unknown", and the clean-up never takes that for stopped."""
    if os.name == "nt":
        import ctypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            # ERROR_INVALID_PARAMETER (87) is Windows saying that no process has this id: it has ended (unless the id
            # was handed to a new process, which the handle-based check below then reports as alive).
            return "ended" if ctypes.get_last_error() == 87 else "unknown"
        try:
            code = ctypes.c_ulong()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                return "unknown"
            return "alive" if code.value == 259 else "ended"  # 259 = STILL_ACTIVE
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "ended"
    except PermissionError:  # the process exists; it is only not ours to signal
        return "alive"
    except OSError:
        return "unknown"
    try:  # a zombie has ended: a container's PID 1 may never reap the helpers it adopts
        with open(f"/proc/{pid}/stat", encoding="ascii", errors="replace") as stat:
            return "ended" if stat.read().rsplit(")", 1)[1].split()[0] == "Z" else "alive"
    except (OSError, IndexError):
        return "alive"


def _alive(pid: int) -> bool:
    return _state(pid) == "alive"


def _listed_as_running(pid: int) -> bool | None:
    """Whether the platform's own process list has this id, asked without going through `_state`:
    True or False, or None when the list cannot be read."""
    if os.name == "nt":
        import ctypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        ulong, ulong_p = ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)
        kernel.K32EnumProcesses.argtypes = [ulong_p, ulong, ulong_p]
        size = 4096
        while True:
            ids, returned = (ctypes.c_ulong * size)(), ctypes.c_ulong()
            if not kernel.K32EnumProcesses(ids, ctypes.sizeof(ids), ctypes.byref(returned)):
                return None
            count = returned.value // ctypes.sizeof(ctypes.c_ulong)
            if count < size:  # a full array may have been cut short: ask again with room to spare
                return pid in ids[:count]
            size *= 2
    return _listed_in_proc(pid) if os.path.isdir("/proc/self") else _listed_by_ps(pid)


def _proc_mount_hides_processes(mounts: str) -> bool:
    """True if the text of /proc/mounts shows /proc mounted with hidepid (other users' processes are then not
    listed at all, so an id that is missing from the list proves nothing). The last /proc mount is the one in force."""
    hidden = False
    for line in mounts.splitlines():
        fields = line.split()
        if len(fields) >= 4 and fields[1] == "/proc":
            hidden = any(o.startswith("hidepid=") and o[8:] not in ("0", "off") for o in fields[3].split(","))
    return hidden


def _listed_in_proc(pid: int, proc_root: str = "/proc") -> bool | None:
    """Linux: only a confirmed absence (FileNotFoundError) says unused. Any other error, a /proc mounted with hidepid
    or a mount table that cannot be read says None. A zombie is still listed: counted as in use, the safe way."""
    try:
        with open(os.path.join(proc_root, "mounts"), encoding="utf-8", errors="replace") as mounts:
            if _proc_mount_hides_processes(mounts.read()):
                return None
        os.stat(os.path.join(proc_root, str(pid)))
    except FileNotFoundError as exc:
        return False if exc.filename is not None and os.path.basename(str(exc.filename)) == str(pid) else None
    except OSError:
        return None
    return True


def _listed_by_ps(pid: int) -> bool | None:
    """No /proc (macOS, BSD): ps exits 1 with nothing at all printed when no process has the id. A listed id stays
    listed: exit 0 with the id on stdout is True, even if ps also printed a warning on stderr (True never makes the
    caller pick an id). A "not listed" answer needs a silent stderr: any error text on it, any other exit code or
    empty output with exit 0 means the query failed, and the answer is None."""
    try:
        ps = subprocess.run(["ps", "-p", str(pid), "-o", "pid="], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if ps.returncode == 0 and ps.stdout.strip():
        return True
    return False if ps.returncode == 1 and not ps.stdout.strip() and not ps.stderr.strip() else None


def _proven_unused_pid(candidates=range(4_000_000, 4_000_040, 4)) -> int | None:
    """An id that the process list shows nothing is using, or None. Not a fixed number: on Linux pid_max can
    reach 4,194,304, so a busy machine can be using the id this test once took for granted. Known limit: another
    process can still take the id between this check and the `_state` call that follows it."""
    return next((pid for pid in candidates if _listed_as_running(pid) is False), None)


def _end_all(pidfile: Path) -> None:
    """Stop every process the stand-in started, by its exact id, and prove each one has ended."""
    for pid in _pids(pidfile):
        if _state(pid) != "ended":
            try:
                os.kill(pid, signal.SIGTERM)  # TerminateProcess on Windows
            except ProcessLookupError:  # it ended between the check and the kill: nothing is left to stop
                pass
            except OSError as exc:
                # Windows says the same thing as WinError 87 (no such process id), or WinError 5 (access denied)
                # while a handle to the ended process is still open. Accept that only when the process is now
                # positively ended; anything else, or a process still running or not inspectable, fails.
                if getattr(exc, "winerror", None) not in (5, 87) or _state(pid) != "ended":
                    raise
        deadline = time.monotonic() + 10.0
        while _state(pid) != "ended" and time.monotonic() < deadline:
            time.sleep(0.05)
        assert _state(pid) == "ended", f"process {pid} started by the test is still running or cannot be inspected"


def _fast_clock(monkeypatch):
    """A clock that jumps, so the 10 second wait in _end_all does not take 10 seconds."""
    ticks = iter(range(1000))
    clock = types.SimpleNamespace(monotonic=lambda: next(ticks), sleep=lambda seconds: None)
    monkeypatch.setattr(sys.modules[__name__], "time", clock)


def _states(monkeypatch, first, then):
    """_state says `first` once (the check before the kill), then `then`, which may be a name or the real probe."""
    real, seen = _state, []

    def state(pid):
        seen.append(pid)
        return first if len(seen) == 1 else (real(pid) if then is None else then)

    monkeypatch.setattr(sys.modules[__name__], "_state", state)
    return seen


def _kill_raises(monkeypatch, error):
    def kill(pid, sig):
        raise error

    monkeypatch.setattr(os, "kill", kill)


@pytest.mark.parametrize("handle", ["open", "closed"])
def test_end_all_treats_a_process_that_ended_before_the_kill_as_stopped(tmp_path, monkeypatch, handle):
    # The race: the process is alive when checked and gone when killed. Windows reports that as access denied while a
    # handle to the ended process is still open, and as "parameter is incorrect" once none is; Linux as no such process.
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    pidfile = tmp_path / "pids"
    pidfile.write_text(str(child.pid), encoding="utf-8")
    if handle == "closed":
        del child
        gc.collect()
    seen = _states(monkeypatch, "alive", None)

    _end_all(pidfile)  # must not raise

    assert len(seen) >= 3  # the check said "alive" once, then the wait loop and the final check ran


def test_end_all_still_fails_for_a_process_that_is_really_still_running(tmp_path, monkeypatch):
    pidfile = tmp_path / "pids"
    pidfile.write_text("4242", encoding="utf-8")
    _states(monkeypatch, "alive", "alive")
    _kill_raises(monkeypatch, ProcessLookupError())  # the kill finds nothing, yet the process is reported alive
    _fast_clock(monkeypatch)

    with pytest.raises(AssertionError, match="still running"):
        _end_all(pidfile)


@pytest.mark.parametrize("kill", ["denied", "works"])
def test_end_all_never_takes_a_process_it_cannot_inspect_for_stopped(tmp_path, monkeypatch, kill):
    # The probe is denied after the first look (WinError 5 on Windows). Whether the kill is refused too or goes through,
    # "could not look" is not "gone": the clean-up must not return normally.
    pidfile = tmp_path / "pids"
    pidfile.write_text("4242", encoding="utf-8")
    _states(monkeypatch, "alive", "unknown")
    if kill == "denied":
        _kill_raises(monkeypatch, PermissionError(13, "Access is denied", None, 5))
    else:
        monkeypatch.setattr(os, "kill", lambda pid, sig: None)
    _fast_clock(monkeypatch)

    with pytest.raises(PermissionError if kill == "denied" else AssertionError):
        _end_all(pidfile)


def test_end_all_lets_an_unlisted_error_escape_even_when_the_process_is_confirmed_gone(tmp_path, monkeypatch):
    pidfile = tmp_path / "pids"
    pidfile.write_text("4242", encoding="utf-8")
    _states(monkeypatch, "alive", "ended")
    _kill_raises(monkeypatch, OSError(errno.EIO, "Input/output error"))  # neither "no such process" nor a listed code

    with pytest.raises(OSError, match="Input/output error"):
        _end_all(pidfile)


@pytest.mark.skipif(os.name != "nt", reason="the access-denied and invalid-parameter codes are Windows errors")
def test_end_all_does_not_swallow_a_windows_error_while_the_process_is_still_alive(tmp_path, monkeypatch):
    pidfile = tmp_path / "pids"
    pidfile.write_text("4242", encoding="utf-8")
    _states(monkeypatch, "alive", "alive")
    _kill_raises(monkeypatch, PermissionError(13, "Access is denied", None, 5))

    with pytest.raises(PermissionError):
        _end_all(pidfile)


def test_the_probe_tells_running_and_ended_processes_apart():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert _state(child.pid) == "alive"
    finally:
        child.kill()
        child.wait()
    assert _state(child.pid) == "ended"
    del child
    gc.collect()


def test_the_probe_says_ended_for_an_id_that_the_process_list_shows_unused():
    unused = _proven_unused_pid()
    if unused is None:
        pytest.skip("no candidate id could be shown unused by the platform's process list")

    assert _state(unused) == "ended"  # no such process (WinError 87 / ProcessLookupError)


def test_an_id_that_is_in_use_is_never_taken_for_unused():
    listed = _listed_as_running(os.getpid())
    if listed is None:
        pytest.skip("the platform's process list cannot be read here")

    assert listed is True
    assert _proven_unused_pid([os.getpid()]) is None


@pytest.mark.parametrize(
    "answers,expected",
    [
        ([None, None, None], None),  # nothing could be read: no id is proven unused
        ([True, True, True], None),  # everything is in use
        ([True, None, True], None),  # in use or unknown, never absent
        ([None, False, True], 11),  # an earlier unknown is passed over, never taken
        ([True, None, False, False], 12),  # the first confirmed absence, not a later one
        ([False, None, None], 10),
        ([], None),
    ],
)
def test_the_selector_takes_only_a_confirmed_absence_never_an_unknown_answer(monkeypatch, answers, expected):
    ids = [10 + i for i in range(len(answers))]
    asked = []

    def listed(pid):
        asked.append(pid)
        return answers[ids.index(pid)]

    monkeypatch.setattr(sys.modules[__name__], "_listed_as_running", listed)

    assert _proven_unused_pid(ids) == expected
    if expected is not None:
        assert asked == ids[: ids.index(expected) + 1]  # it stops at the first confirmed absence


@pytest.mark.parametrize(
    "mounts,hidden",
    [
        ("proc /proc proc rw,nosuid,nodev,noexec,relatime 0 0\n", False),
        ("proc /proc proc rw,hidepid=2 0 0\n", True),
        ("proc /proc proc rw,hidepid=invisible,gid=100 0 0\n", True),
        ("proc /proc proc rw,hidepid=0 0 0\n", False),
        ("proc /proc proc rw,hidepid=off 0 0\n", False),
        ("proc /other proc rw,hidepid=2 0 0\nproc /proc proc rw,hidepid=1 0 0\nproc /proc proc rw 0 0\n", False),
        ("", False),
    ],
)
def test_a_proc_mounted_with_hidepid_is_recognised(mounts, hidden):
    assert _proc_mount_hides_processes(mounts) is hidden


def _fake_proc(tmp_path, mounts="proc /proc proc rw 0 0\n", live=()):
    (tmp_path / "mounts").write_text(mounts, encoding="utf-8")
    for pid in live:
        (tmp_path / str(pid)).mkdir()
    return str(tmp_path)


def test_proc_says_in_use_for_a_listed_id_and_unused_only_for_a_confirmed_absence(tmp_path):
    root = _fake_proc(tmp_path, live=[7])

    assert _listed_in_proc(7, root) is True
    assert _listed_in_proc(8, root) is False


def test_proc_says_nothing_when_an_entry_cannot_be_read(tmp_path, monkeypatch):
    root = _fake_proc(tmp_path, live=[7])
    real_stat = os.stat

    def stat(path, *args, **kwargs):
        if os.path.basename(str(path)) == "7":
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", stat)

    assert _listed_in_proc(7, root) is None


def test_proc_says_nothing_under_hidepid_or_without_a_mount_table(tmp_path):
    hidden = _fake_proc(tmp_path, mounts="proc /proc proc rw,hidepid=2 0 0\n")
    assert _listed_in_proc(8, hidden) is None  # absence proves nothing: other users' processes are not listed
    (tmp_path / "mounts").unlink()
    assert _listed_in_proc(8, hidden) is None


def _ps_answers(monkeypatch, returncode, stdout="", stderr=""):
    done = subprocess.CompletedProcess(["ps"], returncode, stdout, stderr)
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: done)


@pytest.mark.parametrize(
    "returncode,stdout,stderr,expected",
    [
        (0, "  4000000\n", "", True),
        (0, "  4000000\n", "ps: warning: some entries were skipped\n", True),  # listed stays listed
        (1, "", "", False),  # the no-match answer
        (1, "", "ps: cannot read process table: Permission denied\n", None),  # a failed query, same exit code
        (0, "", "", None),
        (2, "", "", None),
    ],
)
def test_ps_tells_a_no_match_answer_from_a_failed_query(monkeypatch, returncode, stdout, stderr, expected):
    _ps_answers(monkeypatch, returncode, stdout, stderr)

    assert _listed_by_ps(4_000_000) is expected


def test_ps_that_cannot_run_says_nothing(monkeypatch):
    def run(*args, **kwargs):
        raise FileNotFoundError(errno.ENOENT, "ps")

    monkeypatch.setattr(subprocess, "run", run)

    assert _listed_by_ps(4_000_000) is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX: how os.kill(pid, 0) errors are read")
@pytest.mark.parametrize(
    "error,expected",
    [(PermissionError(errno.EPERM, "not permitted"), "alive"), (OSError(errno.EINVAL, "bad"), "unknown")],
)
def test_the_posix_probe_reads_a_permission_error_as_alive_and_any_other_error_as_unknown(monkeypatch, error, expected):
    _kill_raises(monkeypatch, error)

    assert _state(1) == expected


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
@pytest.mark.parametrize("cli", ["codex", "claude"])
def test_an_event_whose_type_is_a_list_or_an_object_is_ignored_as_on_master(
        model: Path, tmp_path: Path, cli: str, how: str) -> None:
    """The end-of-run check reads a raw JSON value: a list or an object there must not raise."""
    pidfile, seen, mode = tmp_path / "pids.txt", [], f"odd-{cli}"
    if how == "plain":
        def call():
            return _run_plain(model, mode, pidfile, seen, cli=cli)
    else:
        store = Store(tmp_path / "store")
        store.init(["alpha", "beta"])

        def call():
            return _drive(store, model, mode, pidfile, cli=cli)
    try:
        worker, box, _elapsed = _bounded(call)
    finally:
        _end_all(pidfile)
        worker.join(10.0)
    assert "error" not in box, box
    assert box.get("result") == 0 if how == "plain" else box["result"].ok is True, box


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


FD_SETSIZE = 1024  # select.select refuses a descriptor at or above this


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor numbers; Windows checks the pipe with PeekNamedPipe")
def test_plain_wrap_reads_a_turn_whose_pipe_descriptor_is_above_fd_setsize(
        model: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import resource

    need = FD_SETSIZE + 64
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    raise_soft = soft != resource.RLIM_INFINITY and soft < need
    if raise_soft and hard != resource.RLIM_INFINITY and hard < need:
        pytest.skip(f"RLIMIT_NOFILE allows only {hard} open descriptors; this test needs {need}")
    real_read, pipe_fds = run._read_ready_pipe_chunk, []

    def reading(pipe):
        pipe_fds.append(pipe.fileno())
        return real_read(pipe)

    monkeypatch.setattr(run, "_read_ready_pipe_chunk", reading)
    pidfile, seen, fillers, worker = tmp_path / "pids.txt", [], [], None
    if raise_soft:
        resource.setrlimit(resource.RLIMIT_NOFILE, (need, hard))
    try:
        while not fillers or fillers[-1] < FD_SETSIZE + 8:  # every lower number taken: the pipe gets a higher one
            fillers.append(os.open(os.devnull, os.O_RDONLY))
        worker, box, _elapsed = _bounded(lambda: _run_plain(model, CONTROL, pidfile, seen))
    finally:
        for fd in fillers:
            os.close(fd)
        _end_all(pidfile)
        if worker is not None:
            worker.join(10.0)
        if raise_soft:
            resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
    assert pipe_fds and min(pipe_fds) >= FD_SETSIZE, f"the pipe got descriptor(s) {set(pipe_fds)}: nothing proven"
    assert not worker.is_alive(), "wrap was still waiting for end of output"
    assert box.get("result") == 0, box
    assert _finished_messages(seen) == (True, ["done"])
