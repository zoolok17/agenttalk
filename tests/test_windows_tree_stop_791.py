"""The native Windows tree stop (#791): the product's own matching, stopping and confirming code.

Most tests run the real helper against a fake native layer (a small world of processes), so they
run on every platform and every planted fault turns one of them red. The last tests call the
real Windows layer.
"""

import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from agenttalk import windows_tree_stop as wts

WAIT_TIMEOUT = 0x102


class P:
    def __init__(self, pid, parent, created, **options):
        self.pid, self.parent, self.created = pid, parent, created
        self.alive = True
        self.listed_when_dead = False
        self.hang = False  # a stop is accepted but the process does not end
        self.options = options


class World:
    """A fake native layer: process list, handles, and hooks that let a scenario change the world."""

    def __init__(self, *specs, root=(100, 0, 1000)):
        self.procs = {}
        self.handles = {}
        self.next_handle = 1000
        self.opened, self.closed, self.terminated, self.waits = [], [], [], []
        self.snapshots = 0
        self.fail = set()
        self.on_terminate = None
        self.on_wait = None
        self.listing_hook = None
        self.add(*root)
        for spec in specs:
            self.add(*spec)
        self.root_handle = 1
        self.handles[1] = self.procs[root[0]]
        self.proc = SimpleNamespace(pid=root[0], _handle=1)

    def add(self, pid, parent, created, **options):
        self.procs[pid] = P(pid, parent, created, **options)
        return self.procs[pid]

    def alive(self, pid):
        return self.procs[pid].alive

    # --- the native layer ---
    def snapshot(self):
        self.snapshots += 1
        if "snapshot" in self.fail or f"snapshot{self.snapshots}" in self.fail:
            raise wts.NativeError("snapshot failed")
        listing = {p.pid: p.parent for p in self.procs.values() if p.alive or p.listed_when_dead}
        if self.listing_hook:
            changed = self.listing_hook(self.snapshots, listing)
            listing = listing if changed is None else changed
        return listing

    def open_process(self, pid):
        if f"open{pid}" in self.fail or pid not in self.procs:
            return None
        p = self.procs[pid]
        if not (p.alive or p.listed_when_dead):
            return None
        self.next_handle += 1
        self.handles[self.next_handle] = p
        self.opened.append(self.next_handle)
        return self.next_handle

    def creation_time(self, handle):
        p = self.handles[handle]
        return None if f"times{p.pid}" in self.fail else p.created

    def terminate(self, handle):
        p = self.handles[handle]
        self.terminated.append(p.pid)
        if f"terminate{p.pid}" in self.fail or not p.alive:
            return False  # an ended process answers "access denied" (error 5)
        if not p.hang:
            p.alive = False
        if self.on_terminate:
            self.on_terminate(self, p)
        return True

    def wait(self, handle, milliseconds):
        p = self.handles[handle]
        self.waits.append((p.pid, milliseconds))
        if self.on_wait:
            self.on_wait(self, p, milliseconds)
        return WAIT_TIMEOUT if p.alive else 0

    def close(self, handle):
        assert handle not in self.closed, "a handle was closed twice"
        self.closed.append(handle)

    def stop(self, **kwargs):
        return wts.stop_process_tree(self.proc, native=self, **kwargs)

    def assert_handles_closed(self):
        assert sorted(self.closed) == sorted(self.opened), "every handle the helper opened is closed once"
        assert self.root_handle not in self.closed, "the launcher's handle belongs to Popen"


def test_a_whole_tree_is_stopped_deepest_first_launcher_last_and_confirmed():
    world = World((101, 100, 1100), (102, 101, 1200), (103, 100, 1150))
    assert world.stop() is True
    assert not any(world.alive(pid) for pid in (100, 101, 102, 103))
    assert world.terminated[0] == 102 and world.terminated[-1] == 100  # grandchild first, launcher last
    assert world.terminated.index(102) < world.terminated.index(101)
    world.assert_handles_closed()


def test_an_older_process_naming_a_reused_parent_id_and_its_children_are_left_alone():
    world = World((101, 100, 1100), (200, 100, 900), (201, 200, 1300))
    assert world.stop() is True  # a left-out process does not stop the stop from being confirmed
    assert not world.alive(101) and not world.alive(100)
    assert world.alive(200) and world.alive(201)
    assert 200 not in world.terminated and 201 not in world.terminated
    world.assert_handles_closed()


def test_a_child_created_at_the_same_time_as_its_parent_is_rejected():
    world = World((101, 100, 1000))
    assert world.stop() is True
    assert world.alive(101) and 101 not in world.terminated


def test_an_id_that_changed_owner_between_the_open_and_the_check_is_not_stopped_and_not_confirmed():
    world = World((101, 100, 1100))

    # from snapshot 2 on (the verifying one, taken after the open) the id is listed under another parent
    world.listing_hook = lambda n, listing: {**listing, 101: 999} if n >= 2 else None
    assert world.stop() is False
    assert world.alive(101) and 101 not in world.terminated
    world.assert_handles_closed()


def test_the_verifying_snapshot_is_taken_after_the_open_at_every_level():
    world = World((101, 100, 1100), (102, 101, 1200))
    log = []
    real_snapshot, real_open = world.snapshot, world.open_process
    world.snapshot = lambda: (log.append("snapshot"), real_snapshot())[1]
    world.open_process = lambda pid: (log.append(f"open{pid}"), real_open(pid))[1]
    assert world.stop() is True
    # listing, open, verify - for each of the two levels
    assert log[:6] == ["snapshot", "open101", "snapshot", "snapshot", "open102", "snapshot"]


def test_a_candidate_that_left_before_the_check_cannot_be_verified():
    world = World((101, 100, 1100))
    world.listing_hook = lambda n, listing: {k: v for k, v in listing.items() if k != 101} if n >= 2 else None
    assert world.stop() is False
    assert world.alive(101)


def test_an_unverified_process_never_becomes_a_parent():
    world = World((101, 100, 1100), (103, 101, 1200))
    world.listing_hook = lambda n, listing: {**listing, 101: 999} if n >= 2 else None
    assert world.stop() is False
    assert world.alive(103) and 103 not in world.terminated


@pytest.mark.parametrize("failure", ["snapshot", "snapshot2", "open101", "times101", "terminate101"])
def test_a_failed_step_means_not_confirmed_and_the_rest_is_still_stopped(failure):
    world = World((101, 100, 1100))
    world.fail.add(failure)
    assert world.stop() is False
    assert not world.alive(100)  # the launcher is stopped even when a child could not be
    world.assert_handles_closed()


def test_a_launcher_whose_time_cannot_be_read_is_not_confirmed():
    world = World((101, 100, 1100))
    world.fail.add("times100")
    assert world.stop() is False


def test_a_missing_native_layer_or_launcher_handle_is_not_confirmed_and_does_not_raise(monkeypatch):
    assert wts.stop_process_tree(SimpleNamespace(pid=1), native=World()) is False  # no _handle on it

    def broken():
        raise OSError("no kernel32")

    monkeypatch.setattr(wts, "_default_native", broken)
    assert wts.stop_process_tree(World().proc) is False


def test_a_launcher_that_already_ended_is_still_walked_and_its_children_decide_the_result():
    world = World((101, 100, 1100))
    world.procs[100].alive = False
    world.procs[100].listed_when_dead = True  # the held handle keeps the launcher's id pinned
    assert world.stop() is True
    assert not world.alive(101)
    world.assert_handles_closed()


def test_a_handle_that_already_signalled_counts_as_stopped_when_the_stop_call_fails():
    world = World((101, 100, 1100))
    world.procs[101].alive = False
    world.procs[101].listed_when_dead = True
    assert world.stop() is True
    assert 101 in world.terminated  # asked, answered "failed", then found already signalled


def test_a_process_that_does_not_end_is_not_confirmed():
    world = World((101, 100, 1100))
    world.procs[101].hang = True
    assert world.stop() is False
    world.assert_handles_closed()


def test_all_waits_share_one_deadline(monkeypatch):
    world = World((101, 100, 1100), (102, 100, 1100), (103, 100, 1100))
    clock = [0.0]
    monkeypatch.setattr(wts, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    world.on_wait = lambda w, p, ms: clock.__setitem__(0, clock[0] + 3.0) if ms else None
    world.stop(deadline_seconds=10.0)
    remaining = [ms for _pid, ms in world.waits if ms]
    assert remaining[0] > remaining[-1], remaining  # a fresh limit for every handle would not shrink
    assert remaining == sorted(remaining, reverse=True) and remaining[0] <= 10_000


def test_a_child_started_after_the_first_snapshot_is_stopped_in_pass_two_and_confirmed():
    world = World((101, 100, 1100))
    spawned = []

    def late_child(w, p):
        if p.pid == 101 and not spawned:
            spawned.append(w.add(150, 101, 1500))

    world.on_terminate = late_child
    assert world.stop() is True
    assert spawned and not spawned[0].alive
    world.assert_handles_closed()


def test_a_child_that_reappears_on_every_pass_gives_not_confirmed_after_exactly_three_rechecks():
    world = World((101, 100, 1100))
    spawned = []

    def keeps_coming(w, p):
        child = w.add(200 + len(spawned), p.pid, 2000 + len(spawned))
        spawned.append(child)

    world.on_terminate = lambda w, p: keeps_coming(w, p) if p.pid == 101 or p.pid >= 200 else None
    assert world.stop() is False
    assert [c.alive for c in spawned] == [False, False, False, True]  # three re-check stops, then it gives up
    world.assert_handles_closed()


def test_a_pass_two_process_older_than_its_listed_parent_is_left_alone():
    world = World((101, 100, 1100))
    world.on_terminate = lambda w, p: w.add(150, 101, 500) if p.pid == 101 else None
    assert world.stop() is True
    assert world.alive(150) and 150 not in world.terminated


def test_a_stop_failure_in_pass_two_gives_not_confirmed():
    world = World((101, 100, 1100))

    def late(w, p):
        if p.pid == 101:
            w.add(150, 101, 1500)
            w.fail.add("terminate150")

    world.on_terminate = late
    assert world.stop() is False
    assert world.alive(150)


def test_a_snapshot_failure_in_pass_two_gives_not_confirmed():
    world = World((101, 100, 1100))
    world.fail.add("snapshot4")  # 1 list, 2 verify, 3 list of the next level, 4 the first re-check
    assert world.stop() is False


def test_an_interruption_during_the_stop_stops_what_is_verified_closes_every_handle_and_is_raised_again():
    world = World((101, 100, 1100))

    def interrupt(w, p, ms):
        raise KeyboardInterrupt

    world.on_wait = interrupt
    with pytest.raises(KeyboardInterrupt):
        world.stop()
    assert not world.alive(101)
    world.assert_handles_closed()


def test_an_interruption_between_verifying_and_stopping_still_stops_what_was_verified():
    world = World((101, 100, 1100), (102, 101, 1200))

    def interrupt(n, listing):
        if n == 3:  # the listing of the next level: 101 is verified and held, nothing is stopped yet
            raise KeyboardInterrupt
        return None

    world.listing_hook = interrupt
    with pytest.raises(KeyboardInterrupt):
        world.stop()
    assert not world.alive(101) and world.alive(102)  # 102 was never listed, so never verified
    world.assert_handles_closed()


@pytest.mark.parametrize("error", [KeyboardInterrupt, SystemExit])
def test_an_interruption_before_the_check_never_stops_an_unverified_process(error):
    world = World((101, 100, 1100))

    def interrupt(n, listing):
        if n == 2:  # the verifying snapshot: 101 is opened, not yet verified
            raise error
        return None

    world.listing_hook = interrupt
    with pytest.raises(error):
        world.stop()
    assert world.alive(101) and 101 not in world.terminated
    world.assert_handles_closed()


def test_a_failing_stop_during_an_interruption_does_not_skip_the_other_verified_processes():
    world = World((101, 100, 1100), (102, 101, 1200), (103, 100, 1150))
    real_terminate = world.terminate
    calls = {"n": 0}

    def flaky(handle):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("one stop fails")
        return real_terminate(handle)

    def interrupt(n, listing):
        if n == 5:  # after both levels were verified and held, before anything was stopped
            raise KeyboardInterrupt
        return None

    world.terminate = flaky
    world.listing_hook = interrupt
    with pytest.raises(KeyboardInterrupt):
        world.stop()
    # The first stop failed; every other verified process was still asked, and the handles are closed.
    assert calls["n"] == 4 and len(world.terminated) == 3
    world.assert_handles_closed()


def test_a_cleanup_error_during_an_interruption_never_replaces_it():
    world = World((101, 100, 1100))

    def interrupt(n, listing):
        if n == 3:
            raise KeyboardInterrupt
        return None

    world.listing_hook = interrupt
    world.terminate = lambda handle: 1 / 0
    world.close = lambda handle: (_ for _ in ()).throw(OSError("close failed"))
    with pytest.raises(KeyboardInterrupt):
        world.stop()


def test_tree_stop_object_can_stop_held_processes_after_the_run_and_closes_idempotently():
    world = World((101, 100, 1100))
    world.procs[101].hang = True  # the stop is accepted but 101 does not end: not confirmed, still held
    tree = wts.TreeStop(world.proc, native=world)
    assert tree.run() is False
    world.procs[101].hang = False
    tree.stop_held()  # what an interruption during the reap does
    assert not world.alive(101)
    tree.close()
    tree.close()
    world.assert_handles_closed()


def test_a_bug_in_the_native_layer_is_not_confirmed_not_a_crash():
    world = World((101, 100, 1100))
    world.terminate = lambda handle: 1 / 0
    assert world.stop() is False


# --- no program is launched anywhere in the stop ---

_LAUNCH_EVENTS = {
    "subprocess.Popen",
    "_winapi.CreateProcess",
    "os.system",
    "os.spawn",
    "os.exec",
    "os.startfile",
    "os.posix_spawn",
}
_LAUNCH_FUNCTIONS = {"CreateProcessW", "CreateProcessA", "ShellExecuteW", "ShellExecuteExW", "WinExec"}
_armed = {"thread": None, "seen": []}


def _launch_observer(event, args):
    if _armed["thread"] != threading.get_ident():
        return
    named = event == "ctypes.dlsym" and any(str(a) in _LAUNCH_FUNCTIONS for a in args)
    if event in _LAUNCH_EVENTS or named:
        _armed["seen"].append(event)
        raise RuntimeError(f"a program was launched while stopping a tree: {event}")


sys.addaudithook(_launch_observer)  # audit hooks cannot be removed: it only acts while armed


def arm_launch_observer():
    """Arm the launch observer for this thread; returns the list that collects what it sees."""
    _armed["seen"] = []
    _armed["thread"] = threading.get_ident()
    return _armed["seen"]


def disarm_launch_observer():
    _armed["thread"] = None


class no_launch:
    """Arm the launch observer for one block, in this thread only."""

    def __enter__(self):
        return arm_launch_observer()

    def __exit__(self, *exc):
        disarm_launch_observer()


def test_the_launch_observer_sees_every_way_a_program_can_start():
    with no_launch() as seen:
        with pytest.raises(RuntimeError):
            subprocess.run([sys.executable, "-c", "pass"], check=False)
        with pytest.raises(RuntimeError):
            os.system("exit 0")  # noqa: S605  # nosec B605
    assert seen == ["subprocess.Popen", "os.system"]


def test_stopping_a_tree_launches_no_program(monkeypatch):
    import _winapi

    if hasattr(_winapi, "CreateProcess"):
        monkeypatch.setattr(_winapi, "CreateProcess", lambda *a, **k: pytest.fail("CreateProcess was called"))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("Popen was called"))
    world = World((101, 100, 1100), (102, 101, 1200))
    with no_launch() as seen:
        assert world.stop() is True
    assert seen == []


# --- the real Windows layer ---

windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows native calls")


@windows_only
def test_real_layer_lists_this_process_with_its_parent_and_reads_its_creation_time():
    native = wts._Kernel32()
    listing = native.snapshot()
    assert os.getpid() in listing and os.getppid() in listing
    handle = native.open_process(os.getpid())
    assert handle
    try:
        assert native.creation_time(handle) > 0
        assert native.wait(handle, 0) == WAIT_TIMEOUT  # this process is still running
    finally:
        native.close(handle)
    assert native.open_process(0x7FFFFFF0) is None  # an id that does not exist: failure, not a crash


@windows_only
def test_real_layer_stops_a_real_launcher_and_its_grandchild_without_launching_anything(tmp_path):
    ready = tmp_path / "grandchild.pid"
    script = (
        "import subprocess, sys, time, pathlib\n"
        "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        f"pathlib.Path({str(ready)!r}).write_text(str(g.pid))\n"
        "time.sleep(120)\n"
    )
    launcher = subprocess.Popen([sys.executable, "-c", script])
    native = wts._Kernel32()
    grandchild = None
    try:
        deadline = time.monotonic() + 30
        while not ready.exists() or not ready.read_text():
            assert time.monotonic() < deadline, "the grandchild did not start"
            time.sleep(0.05)
        grandchild = native.open_process(int(ready.read_text()))
        assert grandchild, "the test could not open its own grandchild"
        with no_launch() as seen:
            confirmed = wts.stop_process_tree(launcher)
        assert seen == []
        assert confirmed is True
        assert native.wait(grandchild, 5000) == 0, "the grandchild must be gone"
        assert launcher.wait(timeout=10) is not None
    finally:
        if grandchild:
            native.close(grandchild)
        launcher.kill()
        launcher.wait(timeout=10)
