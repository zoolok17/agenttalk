"""WP3 - the managed lead-loop CADENCE TICK (the proactive sweep).

Covers the cadence-state persistence (store), the snapshot + actionability + state
transitions (lead_loop_cadence), the run_loop idle-branch integration (loop), and the
synthetic cadence drive (run). The load-bearing NEGATIVE assertions (a cadence tick
NEVER advances the cursor / records an attempt / dead-letters; a failed tick withholds
the heartbeat; cadence is not consulted when a message is pending) are here too.
cli-AGNOSTIC by construction (no real CLI; spawn/clock/sleep injected).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import agenttalk.store as store_mod
from agenttalk import cli, lead_loop_cadence as cad
from agenttalk.store import PROC_ALIVE, Store
from agenttalk.wrapper import loop, run, session


def _store(tmp_path: Path, agents=("alpha", "beta", "lead")) -> Store:
    s = Store(tmp_path)
    s.init(list(agents))
    return s


def _set_hb(s: Store, agent: str, epoch: float) -> None:
    """Write ``agent``'s heartbeat at a SPECIFIC epoch (mirrors the WP1 test helper)."""
    iso = (datetime.fromtimestamp(epoch, timezone.utc)
           .isoformat(timespec="microseconds").replace("+00:00", "Z"))
    (s.state_dir / f"{agent}.heartbeat").write_text(iso, encoding="utf-8")


# ----------------------------------------------------------- cadence STATE (store)

def test_cadence_state_default_and_roundtrip(tmp_path: Path) -> None:
    s = _store(tmp_path)
    d = s.read_lead_loop_cadence("beta")           # absent -> fresh default
    assert d["last_tick_epoch"] == 0.0 and d["last_reminded"] == {}
    assert d["escalation_dedup"] == {} and d["cadence_fails"] == 0
    assert d["backoff_until_epoch"] == 0.0 and d["health_escalated"] is False
    s.write_lead_loop_cadence("beta", {
        "last_tick_epoch": 123.0, "last_reminded": {"q": "m"},
        "escalation_dedup": {"k": True}, "cadence_fails": 2,
        "backoff_until_epoch": 500.0, "health_escalated": True})
    d2 = s.read_lead_loop_cadence("beta")
    assert d2["last_tick_epoch"] == 123.0 and d2["last_reminded"] == {"q": "m"}
    assert d2["escalation_dedup"] == {"k": True} and d2["cadence_fails"] == 2
    assert d2["backoff_until_epoch"] == 500.0 and d2["health_escalated"] is True


def test_cadence_state_degrade_safe(tmp_path: Path) -> None:
    s = _store(tmp_path)
    p = s.lead_loop_cadence_path("beta")
    p.write_text("{not json", encoding="utf-8")           # torn -> default
    assert s.read_lead_loop_cadence("beta")["last_tick_epoch"] == 0.0
    p.write_text("[]", encoding="utf-8")                  # non-dict -> default
    assert s.read_lead_loop_cadence("beta")["last_reminded"] == {}
    # forward-incompatible / hand-edited field VALUES coerce SAFE, never raise
    p.write_text(json.dumps({"last_tick_epoch": "x", "cadence_fails": "y",
                             "last_reminded": [], "escalation_dedup": 5,
                             "backoff_until_epoch": None}), encoding="utf-8")
    d = s.read_lead_loop_cadence("beta")
    assert d["last_tick_epoch"] == 0.0 and d["cadence_fails"] == 0
    assert d["last_reminded"] == {} and d["escalation_dedup"] == {}
    assert d["backoff_until_epoch"] == 0.0


def test_cadence_state_cleared_by_reset_preserves_sink(tmp_path: Path) -> None:
    # the cadence file lives in state/ -> reset clears it (like the lease), but reset
    # PRESERVES the dead-letter sink (condition 4 + the constraint).
    s = _store(tmp_path)
    s.write_lead_loop_cadence("beta", {"last_tick_epoch": 1.0})
    sink = s.dead_letter_dir / "beta"
    sink.mkdir(parents=True, exist_ok=True)
    (sink / "20260101-000000-000000-aaaa.json").write_text("{}", encoding="utf-8")
    assert s.lead_loop_cadence_path("beta").exists()
    s.reset()
    assert not s.lead_loop_cadence_path("beta").exists()              # cleared
    assert (sink / "20260101-000000-000000-aaaa.json").exists()       # sink preserved


# ----------------------------------------------------------- cadence_due

def test_cadence_due_interval_and_backoff() -> None:
    cstate = {"last_tick_epoch": 1000.0, "backoff_until_epoch": 0.0}
    assert cad.cadence_due(cstate, now_epoch=1100.0, cadence_seconds=300.0) is False
    assert cad.cadence_due(cstate, now_epoch=1300.0, cadence_seconds=300.0) is True
    # a failure backoff blocks the tick even after the interval elapsed
    backed = {"last_tick_epoch": 1000.0, "backoff_until_epoch": 5000.0}
    assert cad.cadence_due(backed, now_epoch=1300.0, cadence_seconds=300.0) is False
    assert cad.cadence_due(backed, now_epoch=5000.0, cadence_seconds=300.0) is True


# ----------------------------------------------------------- actionability (pure)

def _ob_thread(**over) -> dict:
    t = {"request_id": "q-1", "state": "open-outbound", "peer": "alpha",
         "role": "opener", "last_msg_id": "m1", "age_seconds": 2000.0,
         "unread": False, "needs_operator": False, "operator_state": None,
         "next_action": "await-reply", "next_owner": "alpha",
         "subject": "need x", "peer_composing_fresh": False}
    t.update(over)
    return t


def _snap(threads=None, dead_letters=None, unrouted=None) -> dict:
    dl = dead_letters or []
    return {"agent": "beta", "now_epoch": 10000.0, "threads": threads or [],
            "operator_pending": [],
            "dead_letters": {"count": len(dl), "items": dl},
            "unrouted_escalations": unrouted or []}


def test_actionable_outbound_reminder_window() -> None:
    cstate = {"last_reminded": {}, "escalation_dedup": {}}
    # below the reminder window -> NOT actionable
    young = cad.cadence_actionable(_snap([_ob_thread(age_seconds=100.0)]), cstate,
                                   now_epoch=10000.0, reminder_after_seconds=1800.0)
    assert young == []
    # past the window -> a single outbound_reminder
    out = cad.cadence_actionable(_snap([_ob_thread()]), cstate,
                                 now_epoch=10000.0, reminder_after_seconds=1800.0)
    assert len(out) == 1 and out[0]["type"] == "outbound_reminder"
    assert out[0]["request_id"] == "q-1" and out[0]["last_msg_id"] == "m1"


def test_actionable_reminder_dedup_and_composing() -> None:
    snap = _snap([_ob_thread()])
    # already reminded for THIS (request_id, last_msg_id) -> not actionable
    seen = {"last_reminded": {"q-1": "m1"}, "escalation_dedup": {}}
    assert cad.cadence_actionable(snap, seen, now_epoch=10000.0,
                                  reminder_after_seconds=1800.0) == []
    # a NEW last_msg_id (thread advanced) re-arms the reminder
    snap2 = _snap([_ob_thread(last_msg_id="m2")])
    out = cad.cadence_actionable(snap2, seen, now_epoch=10000.0,
                                 reminder_after_seconds=1800.0)
    assert len(out) == 1 and out[0]["last_msg_id"] == "m2"
    # a fresh peer composing marker suppresses the reminder
    fresh = _snap([_ob_thread(peer_composing_fresh=True)])
    assert cad.cadence_actionable(fresh, {"last_reminded": {}, "escalation_dedup": {}},
                                  now_epoch=10000.0, reminder_after_seconds=1800.0) == []


def test_actionable_dead_letter_and_unrouted_deduped() -> None:
    snap = _snap(dead_letters=[{"message_id": "dl1", "from": "x", "subject": "s",
                                "failure_class": "poison_eligible"}],
                 unrouted=[{"agent": "beta", "message_id": "u1",
                            "last_failure_class": "known_global_infra"}])
    fresh = {"last_reminded": {}, "escalation_dedup": {}}
    out = cad.cadence_actionable(snap, fresh, now_epoch=0.0, reminder_after_seconds=1800.0)
    assert {i["type"] for i in out} == {"dead_letter", "unrouted_escalation"}
    keys = {i["key"] for i in out}
    assert keys == {"dl:dl1", "esc:beta:u1"}
    # both deduped once their keys are latched
    done = {"last_reminded": {}, "escalation_dedup": {"dl:dl1": True, "esc:beta:u1": True}}
    assert cad.cadence_actionable(snap, done, now_epoch=0.0,
                                  reminder_after_seconds=1800.0) == []


def test_actionable_ignores_non_cadence_states() -> None:
    # unread / reply-waiting / owed-inbound are the MESSAGE path's job, never cadence work;
    # an operator-pending thread is tracked context, not its own nudge.
    threads = [
        _ob_thread(state="reply-waiting", unread=True),
        _ob_thread(state="owed-inbound"),
        _ob_thread(state="closed"),
    ]
    cstate = {"last_reminded": {}, "escalation_dedup": {}}
    assert cad.cadence_actionable(_snap(threads), cstate, now_epoch=10000.0,
                                  reminder_after_seconds=1800.0) == []


# ----------------------------------------------------------- state transitions (pure)

def test_apply_tick_success_resets_and_records() -> None:
    cstate = {"last_tick_epoch": 0.0, "last_reminded": {"q-0": "m0"},
              "escalation_dedup": {"dl:x": True}, "cadence_fails": 3,
              "backoff_until_epoch": 999.0, "health_escalated": True}
    new = cad.apply_tick_success(cstate, now_epoch=500.0,
                                 reminded_keys=[("q-1", "m1")], escalation_keys=["dl:y"])
    assert new["last_tick_epoch"] == 500.0 and new["cadence_fails"] == 0
    assert new["backoff_until_epoch"] == 0.0 and new["health_escalated"] is False
    assert new["last_reminded"] == {"q-0": "m0", "q-1": "m1"}
    assert new["escalation_dedup"] == {"dl:x": True, "dl:y": True}
    assert cstate["cadence_fails"] == 3                 # input not mutated


def test_apply_tick_failure_backoff_and_retriable_escalation() -> None:
    # apply_tick_failure does NOT self-latch health_escalated - the CALLER latches it only
    # after the notice ROUTES, so an unrouted escalation is RETRIED (codex WP3 MAJOR).
    base, mx, thr = 60.0, 1800.0, 3
    n1, e1 = cad.apply_tick_failure({}, now_epoch=100.0, base=base,
                                    max_backoff=mx, health_threshold=thr)
    assert n1["cadence_fails"] == 1 and e1 is False
    assert n1["backoff_until_epoch"] == 160.0 and n1["last_tick_epoch"] == 0.0  # NOT advanced
    n2, e2 = cad.apply_tick_failure(n1, now_epoch=200.0, base=base,
                                    max_backoff=mx, health_threshold=thr)
    assert n2["cadence_fails"] == 2 and e2 is False and n2["backoff_until_epoch"] == 320.0
    n3, e3 = cad.apply_tick_failure(n2, now_epoch=300.0, base=base,
                                    max_backoff=mx, health_threshold=thr)
    assert n3["cadence_fails"] == 3 and e3 is True
    assert n3["health_escalated"] is False           # NOT self-latched (caller latches on route)
    # still UNROUTED (health_escalated stays False) -> the NEXT failure RE-escalates
    n4, e4 = cad.apply_tick_failure(n3, now_epoch=400.0, base=base,
                                    max_backoff=mx, health_threshold=thr)
    assert n4["cadence_fails"] == 4 and e4 is True
    # once the caller latches health_escalated (a routed notice), it stops re-escalating
    latched = dict(n4, health_escalated=True)
    n5, e5 = cad.apply_tick_failure(latched, now_epoch=500.0, base=base,
                                    max_backoff=mx, health_threshold=thr)
    assert e5 is False and n5["health_escalated"] is True
    # backoff is capped
    capped, _ = cad.apply_tick_failure({"cadence_fails": 20}, now_epoch=0.0, base=base,
                                       max_backoff=mx, health_threshold=thr)
    assert capped["backoff_until_epoch"] == 1800.0


# ----------------------------------------------------------- snapshot (read-only)

def test_snapshot_structure_and_no_lease_token(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.set_managed_lead_loop("beta")
    lease = s.acquire_lead_loop_lease("beta", owner_pid=os.getpid(), ttl_seconds=900,
                                      now=1000.0, heartbeat_stale_after=120.0)
    token = lease["lease_id"]
    s.send(sender="beta", recipient="alpha", kind="question", subject="need x",
           body="?", meta={"request_id": "q-1"})            # an open-outbound thread
    snap = cad.build_cadence_snapshot(s, "beta", now_epoch=2000.0)
    assert snap["agent"] == "beta"
    assert snap["lease"]["has_lease"] is True
    assert snap["lease"]["owner_pid"] == os.getpid()
    assert token not in json.dumps(snap)                    # lease TOKEN never leaked
    assert "open-outbound" in {t["state"] for t in snap["threads"]}
    for key in ("timing", "lead_loop_health", "dead_letters", "unrouted_escalations",
                "launch_requests", "restart_request", "operator_pending"):
        assert key in snap


def test_snapshot_is_read_only(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.send(sender="alpha", recipient="beta", body="x")      # an inbound message
    cur_before = s.cursor("beta")
    files_before = sorted(p.name for p in s.state_dir.iterdir())
    cad.build_cadence_snapshot(s, "beta", now_epoch=1000.0)
    assert s.cursor("beta") == cur_before                   # cursor untouched
    assert sorted(p.name for p in s.state_dir.iterdir()) == files_before  # wrote nothing


def test_snapshot_per_field_degrades(tmp_path, monkeypatch) -> None:
    s = _store(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("subsystem down")

    monkeypatch.setattr(s, "list_dead_letters", boom)
    snap = cad.build_cadence_snapshot(s, "beta", now_epoch=1000.0)
    assert snap["dead_letters"] == {"count": 0, "items": []}   # degraded, did not crash


def test_snapshot_health_uses_resolved_window_not_default(tmp_path, monkeypatch) -> None:
    # reviewer-1 consolidated blocker: the cadence snapshot's lead_loop_health must use the
    # RESOLVED heartbeat_stale_after (the WP1 window), NOT the 120s store default - else a
    # wrapped controller's snapshot false-unarms a still-armed lease (the threshold-skew class
    # WP1 closed, resurfacing in the cadence visibility view). Mirrors the WP1 skew case.
    import time

    from agenttalk import supervisor as sup
    s = _store(tmp_path, agents=("lead", "beta"))
    s.set_managed_lead_loop("beta")
    monkeypatch.setattr(store_mod, "_process_liveness", lambda pid: PROC_ALIVE)
    now = time.time()
    s.acquire_lead_loop_lease("beta", owner_pid=os.getpid(), ttl_seconds=10,
                              now=now - 5000, lease_id="owner")        # long expired
    _set_hb(s, "beta", now - 300.0)                  # hb age 300s: strictly 120 < 300 < 900
    sup_cfg = {"agents": {"beta": {"wrapped": True, "cli": "codex"}}}
    window = sup.resolve_stuck_after(sup_cfg, sup_cfg["agents"]["beta"])
    assert window > 300.0                            # premise: wrapped window exceeds the hb age
    # control: at the bare 120s default the same lease is UNARMED + heartbeat-stale (skew source)
    bare = s.lead_loop_state("beta", now=now)
    assert bare["armed"] is False and bare["heartbeat_stale"] is True
    # the cadence snapshot health MUST agree with the WP1 resolver at the 900 window, not 120
    snap = cad.build_cadence_snapshot(s, "beta", now_epoch=now, supervisor_config=sup_cfg)
    assert snap["timing"]["heartbeat_stale_after"] == window
    assert snap["lead_loop_health"]["armed"] is True
    assert snap["lead_loop_health"]["heartbeat_stale"] is False


# ----------------------------------------------------------- loop integration (run_loop)

def test_cadence_tick_never_advances_cursor_or_dead_letters(tmp_path: Path) -> None:
    # CONDITION 1/6: a synthetic tick must NEVER advance the cursor, record an attempt,
    # or dead-letter - even a "successful" tick that SENDS.
    s = _store(tmp_path)              # no inbound messages -> pure idle
    calls = {"n": 0}

    def cadence():
        calls["n"] += 1
        s.send(sender="beta", recipient="alpha", body="proactive nudge")  # a tick may SEND
        return loop.CadenceResult(ran=True, ok=True, drove_turn=True)

    turns = loop.run_loop(s, "beta", lambda rec: True, clock=lambda: 0.0,
                          sleep=lambda d: None, max_polls=4, cadence=cadence)
    assert turns == 0
    assert calls["n"] == 4                                   # consulted every idle poll
    assert s.cursor("beta") == ""                            # cursor NEVER advanced
    assert s.dead_lettered_count("beta") == 0                # NEVER dead-lettered
    assert s.dead_letter_attempts("beta")["messages"] == {}  # NEVER recorded an attempt


def test_cadence_failed_tick_withholds_heartbeat(tmp_path: Path) -> None:
    # CONDITION 6: a failed cadence tick withholds the heartbeat so the controller goes
    # stale (the supervisor notices controller-HEALTH trouble); a not-due tick does not.
    s = _store(tmp_path)
    hb = {"n": 0}

    class _Clk:
        t = 0.0

        def __call__(self):
            self.t += 100.0           # advance past heartbeat_interval each poll
            return self.t

    loop.run_loop(s, "beta", lambda rec: True, clock=_Clk(), sleep=lambda d: None,
                  max_polls=3, heartbeat_interval=10.0, heartbeat=lambda: hb.__setitem__("n", hb["n"] + 1),
                  cadence=lambda: loop.CadenceResult(ran=True, ok=False))
    assert hb["n"] == 0               # every failed tick withheld the heartbeat

    hb2 = {"n": 0}
    loop.run_loop(s, "beta", lambda rec: True, clock=_Clk(), sleep=lambda d: None,
                  max_polls=3, heartbeat_interval=10.0,
                  heartbeat=lambda: hb2.__setitem__("n", hb2["n"] + 1),
                  cadence=lambda: loop.CadenceResult(ran=False))
    assert hb2["n"] >= 1              # a not-due tick lets the heartbeat stamp


def test_cadence_not_consulted_when_message_pending(tmp_path: Path) -> None:
    # CONDITION 2: a message present -> the per-message path runs; cadence is NOT consulted
    # for that poll (cadence is the timeout/idle branch only).
    s = _store(tmp_path)
    s.send(sender="alpha", recipient="beta", body="real work")
    consulted = {"n": 0}
    seen: list[str] = []

    def cadence():
        consulted["n"] += 1
        return loop.CadenceResult(ran=False)

    def drive(rec):
        seen.append(rec["body"])
        return True

    turns = loop.run_loop(s, "beta", drive, clock=lambda: 0.0, sleep=lambda d: None,
                          max_turns=1, cadence=cadence)
    assert turns == 1 and seen == ["real work"]
    assert consulted["n"] == 0         # the only poll had a record -> cadence skipped


# ----------------------------------------------------------- synthetic cadence drive (run)

def _codex_completed_lines() -> list[str]:
    return [json.dumps(o) for o in [
        {"type": "thread.started", "thread_id": "t-cad"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "swept"}},
        {"type": "turn.completed"},
    ]]


def test_make_cadence_drive_completed_true_no_ledger(tmp_path: Path) -> None:
    s = _store(tmp_path)
    st = session.SessionState(cli="codex")
    cd = run.make_cadence_drive(s, "beta", "codex", st, ["codex"],
                                spawn=lambda a, i: _codex_completed_lines(),
                                clock=lambda: 0.0, render=False)
    assert cd({"agent": "beta"}, [{"type": "dead_letter", "key": "dl:1"}]) is True
    # the synthetic drive NEVER touches the cursor / attempt ledger / dead-letter sink
    assert s.cursor("beta") == ""
    assert s.dead_lettered_count("beta") == 0
    assert s.dead_letter_attempts("beta")["messages"] == {}


def test_make_cadence_drive_incomplete_false(tmp_path: Path) -> None:
    s = _store(tmp_path)
    st = session.SessionState(cli="codex")
    cd = run.make_cadence_drive(s, "beta", "codex", st, ["codex"],
                                spawn=lambda a, i: [], clock=lambda: 0.0, render=False)
    assert cd({"agent": "beta"}, [{"type": "dead_letter"}]) is False   # no completed boundary
    assert s.dead_letter_attempts("beta")["messages"] == {}            # still no ledger touch


# ----------------------------------------------------------- cli wiring (the real hook)

def test_wrap_loop_mode_builds_working_cadence_hook(tmp_path, monkeypatch) -> None:
    # Exercise the REAL cli-assembled `_cadence` closure (not just the units): with the
    # lease held, a DUE sweep with nothing actionable records the sweep, drives NO model
    # turn, and never advances the cursor / dead-letters. We invoke the captured hook
    # from inside the (monkeypatched) run_loop so the lease is still owned.
    from agenttalk.wrapper import loop as wloop
    s = _store(tmp_path)
    s.set_managed_lead_loop("beta")
    monkeypatch.setattr(store_mod, "_process_liveness", lambda pid: PROC_ALIVE)
    captured: dict = {}

    def capture_run_loop(store, agent, drive, **kw):
        hook = kw.get("cadence")
        assert hook is not None                       # cadence wired for the lead-loop
        captured["res"] = hook()                      # invoke while the lease is held
        return 0                                      # then a clean stop (stand-down)

    monkeypatch.setattr(wloop, "run_loop", capture_run_loop)
    rc = cli._wrap_loop_mode(s, "beta", cli="codex", base_argv=["python", "-c", "pass"],
                             sender="beta", min_interval=5.0, render=False, lead_loop=True)
    assert rc == cli._LEAD_LOOP_STOOD_DOWN_EXIT
    res = captured["res"]
    assert res.ran is True and res.drove_turn is False    # due, but nothing actionable
    assert s.cursor("beta") == ""                          # cursor untouched
    assert s.dead_lettered_count("beta") == 0              # never dead-lettered
    assert s.read_lead_loop_cadence("beta")["last_tick_epoch"] > 0   # sweep recorded


def test_cadence_health_escalation_retries_until_routed(tmp_path, monkeypatch) -> None:
    # codex WP3 MAJOR regression: a controller-health escalation that cannot ROUTE (no
    # operator-facing / sole-lead target) must NOT latch health_escalated - it retries on
    # every subsequent failure until it routes, then latches (and stops spamming).
    from agenttalk.wrapper import loop as wloop
    s = _store(tmp_path)
    s.set_managed_lead_loop("beta")
    monkeypatch.setattr(store_mod, "_process_liveness", lambda pid: PROC_ALIVE)
    # force every tick DUE and force the sweep to FAIL (so _fail() runs each call)
    monkeypatch.setattr(cad, "cadence_due", lambda *a, **k: True)

    def _boom(*a, **k):
        raise RuntimeError("snapshot down")

    monkeypatch.setattr(cad, "build_cadence_snapshot", _boom)
    # no operator route until phase 2 (sole_lead pinned None so operator_facing is the only route)
    monkeypatch.setattr(s, "sole_lead", lambda: None)

    def _health_notices() -> int:
        return sum(1 for m in s.valid_messages()
                   if (m.meta or {}).get("cadence_health") == "true")

    phase: dict = {}

    def capture_run_loop(store, agent, drive, **kw):
        hook = kw["cadence"]
        for _ in range(7):                 # > threshold (K=5), UNROUTED the whole time
            r = hook()
            assert r.ran is True and r.ok is False
        cst = store.read_lead_loop_cadence(agent)
        phase["unrouted_fails"] = cst["cadence_fails"]
        phase["unrouted_latched"] = cst["health_escalated"]
        phase["unrouted_notices"] = _health_notices()
        store.set_operator_facing("alpha")     # NOW a route exists
        hook()
        cst2 = store.read_lead_loop_cadence(agent)
        phase["routed_latched"] = cst2["health_escalated"]
        phase["routed_notices"] = _health_notices()
        hook()                                  # already latched -> no new notice
        phase["after_latch_notices"] = _health_notices()
        return 0

    monkeypatch.setattr(wloop, "run_loop", capture_run_loop)
    cli._wrap_loop_mode(s, "beta", cli="codex", base_argv=["python", "-c", "pass"],
                        sender="beta", min_interval=5.0, render=False, lead_loop=True)
    # UNROUTED: never latched, no notice landed, kept counting (retriable)
    assert phase["unrouted_fails"] == 7
    assert phase["unrouted_latched"] is False
    assert phase["unrouted_notices"] == 0
    # ROUTED: the next failure routes -> ONE notice + latched
    assert phase["routed_latched"] is True
    assert phase["routed_notices"] == 1
    # once latched, further failures do not re-spam the operator
    assert phase["after_latch_notices"] == 1
