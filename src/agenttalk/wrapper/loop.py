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


def run_loop(store, agent: str, drive: Callable[[dict], None], *,
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
        drive(record)                                   # ONE turn (engine stamps heartbeat)
        recv_api.commit(store, agent, record)           # at-least-once: commit AFTER the turn
        store.write_heartbeat(agent)
        last_hb = clock()
        turns += 1
        if max_turns is not None and turns >= max_turns:
            return turns
