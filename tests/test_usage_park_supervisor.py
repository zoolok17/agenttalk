"""A parked seat is alive: the supervisor leaves it alone and spends no restart budget,
and a parked wrapper that has gone stale is still recovered like any dead one.

The runtime record is the REAL one a wrapper writes (``WrapperRuntimeWriter``), read back
and handed to the supervisor's planner with fake clocks and fake process snapshots.
"""

from __future__ import annotations

import inspect
import json

import golden_stop_retries_scenarios as real
import test_supervisor as ts
from agenttalk import supervisor as sup
from agenttalk import wrapper_runtime as wrt
from agenttalk.store import Store
from agenttalk.wrapper import loop, run, session

AGENT = "worker"
NOW = ts.NOW


class Stream(list):
    returncode = None
    watchdog_result = None


def parked_runtime_views(tmp_path):
    """Run the loop to a park with a real runtime writer; return the runtime view the
    supervisor would read just BEFORE the park marks the runtime idle, and just AFTER."""
    store = Store(tmp_path)
    store.init(["alpha", AGENT])
    store.send(sender="alpha", recipient=AGENT, body="one")
    writer = wrt.WrapperRuntimeWriter(
        store.state_dir, AGENT, "wrapper-1", wrapper_pid=ts.WRAP_LAUNCHER_PID, wrapper_start=ts.WRAP_START,
        clock=lambda: NOW - 1.0)
    views: dict = {}

    calls = {"n": 0}

    def idle_hook():
        calls["n"] += 1                           # the first call is the loop starting up
        if calls["n"] == 2:
            views["before"] = wrt.read_runtime(store.state_dir, AGENT, now_epoch=NOW)
        writer.idle()
        if calls["n"] == 2:
            views["after"] = wrt.read_runtime(store.state_dir, AGENT, now_epoch=NOW)

    def spawn(argv, stdin):
        writer.active(456, "start-456")           # what the real spawn hook records for the child
        return Stream(json.dumps(e) for e in real.REAL_CASE_FIVE_HOUR)

    drive = run.make_drive(
        store, AGENT, "claude", session.SessionState(cli="claude", claude_session_id="sid-1"), ["claude"],
        spawn=spawn, clock=lambda: 0.0, render=False, heartbeat=lambda: None, runtime_writer=writer,
        usage_limit_park=True)
    loop.run_loop(store, AGENT, drive, clock=lambda: 0.0, sleep=lambda d: None, max_polls=4,
                  now_iso=lambda: "2026-09-08T10:00:00Z", wrapper_generation="wrapper-1",
                  usage_limit_park=True, on_runtime_idle=idle_hook)
    return views


def plan(view, *, stale=False):
    return ts._plan_wrap(
        ts._report(heartbeat_stale=stale, heartbeat_age_seconds=3000.0 if stale else 1.0,
                   wrapper_runtime=view),
        {"agents": {AGENT: ts._wrap_ready(backoff_next_epoch=0.0)}},
        snapshot=ts._wrap_snap()[:1])


def test_the_runtime_record_after_a_park_is_idle_and_remembers_the_failed_turn(tmp_path):
    views = parked_runtime_views(tmp_path)
    after = views["after"]["record"]
    assert after["phase"] == "idle" and after["last_outcome"] == "failed"
    # the failed turn had already written its terminal state before the park said idle
    assert views["before"]["record"]["phase"] == "terminal"
    assert views["before"]["record"]["last_outcome"] == "failed"


def test_a_parked_seat_with_a_fresh_heartbeat_is_healthy_idle_and_spends_no_budget(tmp_path):
    views = parked_runtime_views(tmp_path)
    result = plan(views["after"])
    assert result["action"] == sup.NONE and result["state"] == "HEALTHY_IDLE"
    assert "restart_budget_relaunches" not in json.dumps(result["next_state"])


def test_without_the_idle_mark_the_same_seat_would_read_as_a_failed_turn(tmp_path):
    # the control: why the park marks the runtime idle after the failed turn is recorded
    terminal = ts._wrapper_runtime_view(phase="terminal", updated_age=1.0, progress_age=3000.0,
                                        progress_sequence=4, turn_generation=8, outcome="failed")
    assert plan(terminal)["state"] == "TURN_FAILED"


def test_a_parked_wrapper_that_went_stale_is_still_recovered(tmp_path):
    views = parked_runtime_views(tmp_path)
    stale = plan(views["after"], stale=True)
    assert stale["action"] == sup.STUCK_RECOVER and stale["state"] == "STUCK_OR_DEAD"


def test_the_planner_never_reads_the_park_marker():
    # the marker is advisory: nothing in the supervisor may let it suppress stale recovery
    source = inspect.getsource(sup)
    assert "usage_limit_park" not in source and "usage-limit-park" not in source
