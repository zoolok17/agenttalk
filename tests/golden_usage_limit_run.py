"""A scripted run of the PROVEN usage-limit park, recorded step by step (the records it leaves, the marker, the
health and notice calls). The golden file ``tests/golden/usage_limit_run_93988c06.json`` was made by running this
very file against agenttalk 93988c06, the code before the provider cool-down existed;
``test_a_recorded_run_of_the_proven_usage_limit_is_identical_to_the_one_before_the_cooldown`` runs it against the
current code and requires the same record. Re-making the golden file from the current code is NOT allowed to be the
way a difference is made to go away.

This module imports nothing that only the cool-down adds: it uses the loop, the drive and the park rules by their
older names only, so it runs unchanged on both.
"""

from __future__ import annotations

import json
from pathlib import Path

from agenttalk.wrapper import loop, run, session
from test_usage_park_loop import (AGENT, CASE1_RESET, CASE2_NOW, T0, Clock, Seen, Spawner, case1, case2, failed_turn,
                                  head, make_store, ok_turn)

VOLATILE = ("last_attempt_id",)


def _go(store, spawner, clock, *, polls, generation, seen):
    def escalate(info):
        seen.events.append(("notice", json.dumps(info["usage_limit"], sort_keys=True)))
        return True

    drive = run.make_drive(
        store, AGENT, "claude", session.SessionState(cli="claude", claude_session_id="sid-1"), ["claude"],
        spawn=spawner, clock=clock.now, render=False, heartbeat=lambda: None, usage_limit_park=True)

    def counted(record):
        seen.events.append("drive")
        return drive(record)

    loop.run_loop(
        store, AGENT, counted, clock=clock.now, sleep=clock.sleep, now_iso=clock.iso, max_polls=polls,
        wrapper_generation=generation, usage_limit_park=True, on_escalate=escalate,
        heartbeat=lambda: seen.events.append("stamp"),
        on_health_parked=lambda rec, reason: seen.events.append(("health", reason)),
        on_runtime_idle=lambda: seen.events.append("idle"))


def _snapshot(store, clock, seen):
    mid = head(store).id if store.messages_for(AGENT) else None
    rec = store.attempt_record(AGENT, mid) if mid else None
    rec = {k: v for k, v in (rec or {}).items() if k not in VOLATILE}
    marker = None
    path = store.usage_limit_park_path(AGENT)
    if path.exists():
        marker = json.loads(path.read_text(encoding="utf-8"))
    out = {"record": rec, "marker": marker, "events": [e if isinstance(e, str) else list(e) for e in seen.events],
           "sleeps": list(clock.sleeps), "cursor_set": bool(store.cursor(AGENT)),
           "dead_letters": store.dead_lettered_count(AGENT)}
    seen.events.clear()
    text = json.dumps(out)
    return json.loads(text.replace(mid, "<MSG>") if mid else text)       # the message id is minted from the clock


def record_run(root: Path) -> dict:
    """The whole recorded run: a five-hour park, held polls, a start probe that meets the limit again, the wake
    probe that meets another class (the park closes), then a weekly limit that is probed by a restart and a success."""
    out = {}
    store = make_store(root / "five")
    seen, clock = Seen(), Clock(T0)
    spawner = Spawner(case1(), case1(), failed_turn())
    _go(store, spawner, clock, polls=3, generation="g1", seen=seen)
    out["park"] = _snapshot(store, clock, seen)
    clock.t = T0 + 120
    _go(store, spawner, clock, polls=4, generation="g1", seen=seen)
    out["held"] = _snapshot(store, clock, seen)
    _go(store, spawner, clock, polls=2, generation="g2", seen=seen)
    out["start_probe_limit_again"] = _snapshot(store, clock, seen)
    clock.t = float(CASE1_RESET + 31)
    _go(store, spawner, clock, polls=2, generation="g2", seen=seen)
    out["wake_probe_other_class"] = _snapshot(store, clock, seen)
    out["spawns"] = spawner.calls
    store = make_store(root / "seven")
    seen, clock = Seen(), Clock(CASE2_NOW)
    spawner = Spawner(case2(), ok_turn())
    _go(store, spawner, clock, polls=3, generation="g1", seen=seen)
    out["weekly_park"] = _snapshot(store, clock, seen)
    _go(store, spawner, clock, polls=2, generation="g2", seen=seen)
    out["weekly_probe_success"] = _snapshot(store, clock, seen)
    out["weekly_spawns"] = spawner.calls
    return json.loads(json.dumps(out, sort_keys=True))
