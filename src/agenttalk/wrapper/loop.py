"""The wrapper-owned listen loop (design C, Phase B): the long-running supervised
process. IDLE on the bus (write_waiting + write_heartbeat + adaptive backoff) until
an inbound message; drive the CLI for ONE turn; COMMIT (advance the cursor) AFTER
the turn (at-least-once - a crash mid-turn re-delivers on restart); return to idle.

The supervisor stays dumb (heartbeat/backoff/kill). The heartbeat stays fresh both
IDLE (here) and DURING a turn (the per-turn engine stamps on progress events), so a
healthy blocked-or-working wrapped agent never looks stale to the supervisor
(Codex flagged this as required).

Pure orchestration: the per-turn ``drive`` callback, the clock, and sleep are
injected, so the loop is unit-testable with a fixture Store and NO real CLI.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from . import recv_api


def is_terminal_control(record: dict) -> bool:
    """A scoped record on a rescinded/closed thread is TERMINAL control state
    (Codex carry-forward): consume it, NEVER drive a turn on it as ordinary work."""
    sc = record.get("scoped")
    return bool(sc and (sc.get("closed") or sc.get("superseded")))


def is_stop_signal(store, record: dict) -> bool:
    """A loop-exit control message: ``kind=end`` (graceful shutdown) or
    ``kind=release`` from a lead / operator-facing sender. The wrapped MODEL is a
    PURE per-turn handler now (it no longer runs the listen loop), so the WRAPPER
    must obey a lead's release/end and STAND DOWN - the model never exits the loop
    itself. ``release`` is gated on a protected sender (operator-facing UNION leads)
    so a non-lead cannot stand down a supervised wrapper; ``end`` is the canonical
    shutdown kind and is honored from any (bus-authenticated) sender."""
    kind = record.get("kind")
    if kind == "end":
        return True
    if kind == "release":
        return record.get("from") in store.protected_agents()
    return False


def run_loop(store, agent: str, drive: Callable[[dict], bool], *,
             idle_interval: float = 0.3, max_idle_interval: float = 2.0,
             heartbeat_interval: float = 10.0,
             clock: Callable[[], float] = time.monotonic,
             sleep: Callable[[float], None] = time.sleep,
             max_turns: int | None = None, max_polls: int | None = None) -> int:
    """Run the wrapper listen loop. ``drive(record)`` handles ONE turn (injected).
    Returns the number of turns driven. ``max_turns`` / ``max_polls`` bound the
    loop for tests (both None = run forever)."""
    store.write_waiting(agent, {"agent": agent, "mode": "wrapper-loop"})
    turns = 0
    polls = 0
    last_hb: float | None = None
    cur_sleep = idle_interval
    fail_sleep = idle_interval
    while True:
        if max_polls is not None and polls >= max_polls:
            return turns
        polls += 1
        record = recv_api.next_record(store, agent)
        now = clock()
        if record is None:
            # IDLE: keep the heartbeat fresh, then back off while the bus is quiet.
            if last_hb is None or (now - last_hb) >= heartbeat_interval:
                store.write_heartbeat(agent)
                last_hb = now
            sleep(cur_sleep)
            cur_sleep = min(max_idle_interval, cur_sleep * 2.0)
            continue
        cur_sleep = idle_interval                       # reset backoff on activity
        if is_terminal_control(record):
            recv_api.commit(store, agent, record)       # consume + skip (control)
            continue
        if is_stop_signal(store, record):
            # release (from a lead/liaison) or end: the wrapper owns loop-exit now
            # that the model is a pure handler. Consume it and STAND DOWN.
            recv_api.commit(store, agent, record)
            return turns
        # Drive ONE turn. Commit the inbound message ONLY when the turn SUCCEEDS.
        # drive() stamps the heartbeat itself on a clean completed turn (and NOT on
        # a failed turn), so the loop does not stamp here - a failed turn leaves no
        # fresh heartbeat (reviewer-1 gate).
        if drive(record):
            recv_api.commit(store, agent, record)
            last_hb = clock()                           # drive already stamped on success
            fail_sleep = idle_interval                  # reset failure backoff
            turns += 1
            if max_turns is not None and turns >= max_turns:
                return turns
        else:
            # FAILED turn: do NOT commit (re-delivers, at-least-once) and do NOT
            # stamp the heartbeat - so a persistent no-progress failure goes stale
            # and the supervisor restarts us. BACK OFF before retrying so we never
            # hammer the CLI in a hot spawn loop (a process storm) before that.
            sleep(fail_sleep)
            fail_sleep = min(max_idle_interval, fail_sleep * 2.0)
