"""Bounded in-turn work heartbeat (wrapped-Claude false-STUCK fix): pure resolver +
guards, deterministic ticker units (injected clock/wait, run synchronously), the
threaded hard no-stamp-after-stop race, real-subprocess make_drive wiring (a long
silent turn stays live; a FAILED turn with an intentionally racing ticker still ends
with NO heartbeat), and the cmd_wrap enabled-but-invalid config-blocked path."""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

from agenttalk import cli
from agenttalk.store import Store
from agenttalk.wrapper import run
from agenttalk.wrapper import session as wsession
from agenttalk.wrapper import work_heartbeat as whb
from agenttalk.wrapper.work_heartbeat import (
    STOPPED_CAP,
    STOPPED_CHILD_EXIT,
    STOPPED_LOST_LEASE,
    STOPPED_TURN_END,
    WorkHeartbeatConfig,
    WorkHeartbeatTicker,
)

# ------------------------------------------------------------------ resolver / guards


def test_resolve_defaults_wrapped_claude_loop_enabled() -> None:
    cfg = whb.resolve_work_heartbeat({}, {}, cli="claude", mode="wrapper-loop")
    assert cfg.enabled is True
    assert cfg.interval_seconds == 30.0
    assert cfg.max_turn_seconds == 900.0
    assert cfg.config_errors == ()
    assert whb.work_heartbeat_effectively_live(cfg) is True


def test_resolve_default_enable_matrix() -> None:
    # claude: continuous loop + managed lead-loop ON; one-shot OFF (no supervisor
    # stale-heartbeat consumer for ephemerals); codex OFF in every mode (its 2400s
    # stuck_after / watchdog-preemption math is untouched by this feature).
    on = whb.default_work_heartbeat_enabled
    assert on("claude", "wrapper-loop") is True
    assert on("claude", "lead-loop") is True
    assert on("claude", "wrapper-one-shot") is False
    assert on("codex", "wrapper-loop") is False
    assert on("codex", "lead-loop") is False
    assert on("codex", "wrapper-one-shot") is False
    assert whb.resolve_work_heartbeat({}, {}, cli="codex", mode="wrapper-loop").enabled is False
    assert whb.resolve_work_heartbeat(
        {}, {}, cli="claude", mode="wrapper-one-shot").enabled is False


def test_resolve_explicit_enabled_overrides_default() -> None:
    # an explicit config flag wins over the per-CLI/mode default, both directions.
    assert whb.resolve_work_heartbeat(
        {"work_heartbeat": {"enabled": True}}, {}, cli="codex", mode="wrapper-loop",
    ).enabled is True
    assert whb.resolve_work_heartbeat(
        {}, {"work_heartbeat": {"enabled": False}}, cli="claude", mode="wrapper-loop",
    ).enabled is False


def test_resolve_per_agent_wins_over_global() -> None:
    cfg = whb.resolve_work_heartbeat(
        {"work_heartbeat": {"interval_seconds": 40, "max_turn_seconds": 1200}},
        {"work_heartbeat": {"interval_seconds": 20}},
        cli="claude", mode="wrapper-loop")
    assert cfg.interval_seconds == 20.0          # per-agent wins
    assert cfg.max_turn_seconds == 1200.0        # global fills the unset key


def test_resolve_corrupt_config_tolerated() -> None:
    # truthy non-dict config / per-agent entry / block never crash resolution.
    for config, cfg_agent in (
        ("corrupt", "corrupt"),
        ({"work_heartbeat": "corrupt"}, {"work_heartbeat": 7}),
        (None, None),
    ):
        cfg = whb.resolve_work_heartbeat(config, cfg_agent, cli="claude", mode="wrapper-loop")
        assert cfg.interval_seconds == 30.0 and cfg.config_errors == ()


def test_resolve_explicit_invalid_values_are_errors_never_coerced() -> None:
    # non-positive and non-numeric EXPLICIT values are recorded as config errors
    # (fail visibly at startup), not silently dropped to the default.
    cfg = whb.resolve_work_heartbeat(
        {"work_heartbeat": {"interval_seconds": -5}}, {}, cli="claude", mode="wrapper-loop")
    assert any("interval_seconds" in e for e in cfg.config_errors)
    assert whb.work_heartbeat_effectively_live(cfg) is False
    cfg = whb.resolve_work_heartbeat(
        {}, {"work_heartbeat": {"max_turn_seconds": "tall"}}, cli="claude", mode="wrapper-loop")
    assert any("max_turn_seconds" in e for e in cfg.config_errors)
    assert whb.work_heartbeat_effectively_live(cfg) is False
    # zero is non-positive too
    cfg = whb.resolve_work_heartbeat(
        {}, {"work_heartbeat": {"interval_seconds": 0}}, cli="claude", mode="wrapper-loop")
    assert cfg.config_errors and whb.work_heartbeat_effectively_live(cfg) is False
    cfg = whb.resolve_work_heartbeat(
        {},
        {"work_heartbeat": {"max_turn_seconds": float("inf")}},
        cli="claude",
        mode="wrapper-loop",
    )
    assert any("max_turn_seconds" in e for e in cfg.config_errors)
    assert whb.work_heartbeat_effectively_live(cfg) is False


def test_low_max_turn_is_allowed_not_rejected() -> None:
    # a LOW max_turn_seconds narrows the masking window (safe direction): allowed.
    cfg = whb.resolve_work_heartbeat(
        {"work_heartbeat": {"max_turn_seconds": 60}}, {}, cli="claude", mode="wrapper-loop")
    assert cfg.max_turn_seconds == 60.0 and cfg.config_errors == ()
    assert whb.work_heartbeat_effectively_live(cfg) is True


def test_interval_violation_vs_resolved_stuck_after() -> None:
    ok = WorkHeartbeatConfig(enabled=True, interval_seconds=30.0)
    assert whb.interval_violation(ok, stuck_after_seconds=180.0) is None
    # bound = min(60, stuck_after/3): 180 -> 60; 90 -> 30
    high = WorkHeartbeatConfig(enabled=True, interval_seconds=90.0)
    v = whb.interval_violation(high, stuck_after_seconds=180.0)
    assert v is not None and "allow_high_interval" in v
    tight = WorkHeartbeatConfig(enabled=True, interval_seconds=31.0)
    assert whb.interval_violation(tight, stuck_after_seconds=90.0) is not None
    assert whb.interval_violation(
        WorkHeartbeatConfig(enabled=True, interval_seconds=30.0),
        stuck_after_seconds=90.0) is None
    # explicit reviewed override + disabled config: no violation
    assert whb.interval_violation(
        WorkHeartbeatConfig(enabled=True, interval_seconds=90.0, allow_high_interval=True),
        stuck_after_seconds=180.0) is None
    assert whb.interval_violation(
        WorkHeartbeatConfig(enabled=False, interval_seconds=90.0),
        stuck_after_seconds=180.0) is None


# ------------------------------------------------------------------ ticker units
#
# Deterministic: the ticker's _run is executed SYNCHRONOUSLY with an injected clock
# and wait script (wait returns True = stop requested, False = interval elapsed).


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _cfg(**kw) -> WorkHeartbeatConfig:
    base = {"enabled": True, "interval_seconds": 30.0, "max_turn_seconds": 900.0}
    base.update(kw)
    return WorkHeartbeatConfig(**base)


def test_ticker_immediate_first_stamp_then_stop() -> None:
    stamps: list[float] = []
    clock = _Clock()
    t = WorkHeartbeatTicker(cfg=_cfg(), stamp=lambda: stamps.append(clock.t),
                            child_alive=lambda: True, clock=clock,
                            wait=lambda _s: True)          # stop right after first stamp
    t._run()
    assert stamps == [0.0]                                 # stamped BEFORE any wait
    assert t.result["stamps"] == 1
    assert t.result["stopped"] == STOPPED_TURN_END


def test_ticker_stamps_each_interval_while_child_alive() -> None:
    clock = _Clock()
    waits = {"n": 0}

    def wait(seconds: float) -> bool:
        waits["n"] += 1
        clock.t += seconds
        return waits["n"] > 3                              # 3 intervals, then stop

    t = WorkHeartbeatTicker(cfg=_cfg(), stamp=lambda: None,
                            child_alive=lambda: True, clock=clock, wait=wait)
    t._run()
    assert t.result["stamps"] == 4                         # immediate + one per interval
    assert t.result["stopped"] == STOPPED_TURN_END


def test_ticker_stops_when_child_exits() -> None:
    clock = _Clock()
    alive = {"v": True}

    def wait(seconds: float) -> bool:
        clock.t += seconds
        alive["v"] = False                                 # child dies during the wait
        return False

    t = WorkHeartbeatTicker(cfg=_cfg(), stamp=lambda: None,
                            child_alive=lambda: alive["v"], clock=clock, wait=wait)
    t._run()
    assert t.result["stamps"] == 1                         # only the immediate stamp
    assert t.result["stopped"] == STOPPED_CHILD_EXIT


def test_ticker_stops_permanently_at_cap_no_stamp_after() -> None:
    clock = _Clock()

    def wait(seconds: float) -> bool:
        clock.t += seconds
        return False

    t = WorkHeartbeatTicker(cfg=_cfg(interval_seconds=100.0, max_turn_seconds=250.0),
                            stamp=lambda: None, child_alive=lambda: True,
                            clock=clock, wait=wait)
    t._run()
    # stamps at t=0, 100, 200; the t=300 check hits the cap BEFORE stamping.
    assert t.result["stamps"] == 3
    assert t.result["stopped"] == STOPPED_CAP
    assert clock.t == 300.0                                # loop exited at the cap check


def test_ticker_stamp_errors_caught_and_recorded_keeps_ticking() -> None:
    clock = _Clock()
    calls = {"n": 0}

    def stamp() -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("transient store hiccup")

    waits = {"n": 0}

    def wait(seconds: float) -> bool:
        waits["n"] += 1
        clock.t += seconds
        return waits["n"] > 2

    t = WorkHeartbeatTicker(cfg=_cfg(), stamp=stamp, child_alive=lambda: True,
                            clock=clock, wait=wait)
    t._run()                                               # never raises
    assert calls["n"] == 3                                 # kept ticking past the error
    assert t.result["stamps"] == 2
    assert t.result["stamp_errors"] == 1
    assert "transient store hiccup" in t.result["last_error"]


class _LeaseLost(Exception):
    pass


def test_ticker_typed_lease_loss_stops_permanently(tmp_path: Path) -> None:
    # the combined lead-loop callable is renew-THEN-stamp: a renew failure raises
    # BEFORE any bus write, so the ticker must record lost_lease, stop permanently,
    # and leave NO heartbeat file behind.
    s = Store(tmp_path)
    s.init(["beta", "lead"])
    calls = {"n": 0}

    def combined() -> None:
        calls["n"] += 1
        if calls["n"] >= 2:
            raise _LeaseLost("stolen")
        s.write_heartbeat("beta")

    clock = _Clock()
    waits = {"n": 0}

    def wait(seconds: float) -> bool:
        waits["n"] += 1
        clock.t += seconds
        return waits["n"] > 5                              # would allow 5 more intervals

    t = WorkHeartbeatTicker(cfg=_cfg(), stamp=combined, child_alive=lambda: True,
                            clock=clock, wait=wait, lease_lost_exceptions=(_LeaseLost,))
    t._run()
    assert calls["n"] == 2                                 # NO further attempt after the loss
    assert t.result["stopped"] == STOPPED_LOST_LEASE
    assert t.result["stamps"] == 1
    s.clear_heartbeat("beta")
    # a fresh renew failure writes NO bus heartbeat (renew-then-stamp ordering)
    try:
        combined()
    except _LeaseLost:
        pass
    assert s.read_heartbeat("beta") is None


def test_ticker_hard_no_stamp_after_stop_race() -> None:
    # THE release-blocker invariant: stop() synchronizes with an IN-FLIGHT stamp and,
    # once it returns, no further stamp can execute - even though the thread is alive.
    entered = threading.Event()
    release = threading.Event()
    count = {"n": 0}

    def slow_stamp() -> None:
        count["n"] += 1
        entered.set()
        release.wait(5.0)                                  # hold the stamp in flight

    t = WorkHeartbeatTicker(cfg=_cfg(interval_seconds=0.01), stamp=slow_stamp,
                            child_alive=lambda: True)
    t.start()
    assert entered.wait(5.0)                               # first stamp is in flight
    stopped = threading.Event()

    def do_stop() -> None:
        t.stop()                                           # must block on the in-flight stamp
        stopped.set()

    stopper = threading.Thread(target=do_stop, daemon=True)
    stopper.start()
    assert not stopped.wait(0.2)                           # stop() is synchronizing, not done
    release.set()                                          # let the in-flight stamp finish
    assert stopped.wait(5.0)                               # now stop() returns
    n_at_stop = count["n"]
    time.sleep(0.1)                                        # many 0.01s intervals elapse
    assert count["n"] == n_at_stop == 1                    # NO stamp after stop() returned
    t.join(timeout=5.0)
    assert t.result["stopped"] == STOPPED_TURN_END


def test_ticker_status_callback_failures_swallowed() -> None:
    def bad_status(_s: dict) -> None:
        raise OSError("diagnostics disk full")

    t = WorkHeartbeatTicker(cfg=_cfg(), stamp=lambda: None, child_alive=lambda: True,
                            wait=lambda _s: True, on_status=bad_status)
    t._run()                                               # never raises
    assert t.result["stamps"] == 1


# ------------------------------------------------------------------ make_drive wiring
#
# REAL _ProcStream + a real python child (the ticker only exists on the real spawner).


_SILENT_OK = (
    "import sys, time\n"
    "sys.stdin.read()\n"                                   # drain the prompt (pipe etiquette)
    "time.sleep(0.5)\n"
    "print('{\"type\":\"stream_event\",\"event\":{\"type\":\"message_start\"}}', flush=True)\n"
    "print('{\"type\":\"stream_event\",\"event\":{\"type\":\"message_stop\"}}', flush=True)\n"
)
_SILENT_FAIL = (
    "import sys, time\n"
    "sys.stdin.read()\n"
    "time.sleep(0.3)\n"
    "sys.exit(3)\n"
)


def _drive_for(store: Store, agent: str, script: str, *, interval: float = 0.05,
               max_turn: float = 30.0):
    st = wsession.load_session(store, agent, "claude")
    cfg = _cfg(interval_seconds=interval, max_turn_seconds=max_turn)
    return run.make_drive(store, agent, "claude", st,
                          [sys.executable, "-X", "utf8", "-c", script],
                          render=False, work_heartbeat=cfg)


def test_make_drive_long_silent_claude_turn_stays_live_and_ends_fresh(tmp_path: Path) -> None:
    s = Store(tmp_path)
    s.init(["beta", "lead"])
    s.clear_heartbeat("beta")
    drive = _drive_for(s, "beta", _SILENT_OK)
    out = drive({"id": "m1", "body": "hi"})
    assert out.ok is True
    assert s.read_heartbeat("beta") is not None            # turn-boundary stamp
    status = s.read_work_heartbeat_status("beta")
    assert status is not None
    assert status["stamps"] >= 2                           # in-turn liveness during silence
    assert status["stopped"] in (STOPPED_TURN_END, STOPPED_CHILD_EXIT)
    # the ticker does NOT refresh health: the final snapshot is the ordinary
    # turn-completed idle write, with no ticker-origin reason anywhere.
    raw = s.read_health_raw("beta") or {}
    assert raw.get("reason_code") == "turn_completed"
    assert "work_heartbeat" not in (raw.get("reason_code") or "")


def test_make_drive_failed_turn_with_racing_ticker_ends_with_no_heartbeat(tmp_path: Path) -> None:
    # REQUIRED regression (release blocker): the ticker races the failure path at a
    # 10ms interval; drive()'s clear_heartbeat runs only after _ProcStream stopped the
    # ticker (stop-before-exhausted + no-stamp-after-stop), so the failed turn must
    # ALWAYS end with NO fresh heartbeat.
    s = Store(tmp_path)
    s.init(["beta", "lead"])
    for attempt in range(3):
        drive = _drive_for(s, "beta", _SILENT_FAIL, interval=0.01)
        out = drive({"id": f"m{attempt}", "body": "x"})
        assert out.ok is False
        assert s.read_heartbeat("beta") is None, f"fresh heartbeat after failed turn #{attempt}"
        status = s.read_work_heartbeat_status("beta")
        assert status is not None and status["stamps"] >= 1   # the race was real


def test_make_drive_disabled_or_invalid_config_starts_no_ticker(tmp_path: Path) -> None:
    s = Store(tmp_path)
    s.init(["beta", "lead"])
    st = wsession.load_session(s, "beta", "claude")
    for cfg in (WorkHeartbeatConfig(enabled=False),
                WorkHeartbeatConfig(enabled=True, config_errors=("bad",))):
        drive = run.make_drive(s, "beta", "claude", st,
                               [sys.executable, "-X", "utf8", "-c", _SILENT_FAIL],
                               render=False, work_heartbeat=cfg)
        drive({"id": "m1", "body": "x"})
        assert s.read_work_heartbeat_status("beta") is None    # ticker never ran
        assert s.read_heartbeat("beta") is None


def test_proc_stream_over_cap_silent_child_stops_stamping(tmp_path: Path) -> None:
    # over-cap: stamps stop at the cap while the child is still alive; recovery is
    # then the supervisor's stale-heartbeat path at cap + stuck_after (no new kill).
    stamps = {"n": 0}
    stream = run._ProcStream(
        [sys.executable, "-X", "utf8", "-c", "import time; time.sleep(1.0)"], None,
        work_heartbeat=_cfg(interval_seconds=0.05, max_turn_seconds=0.15),
        work_heartbeat_stamp=lambda: stamps.__setitem__("n", stamps["n"] + 1))
    list(stream)                                           # drain to child exit (~1s)
    res = stream.work_heartbeat_result
    assert res is not None and res["stopped"] == STOPPED_CAP
    # ~3 stamps fit under a 0.15s cap at 0.05s cadence; the 1s child got none after.
    assert res["stamps"] <= 4
    assert stamps["n"] == res["stamps"]


# ------------------------------------------------------------------ cmd_wrap guards


def test_cmd_wrap_enabled_invalid_config_blocks_visibly(tmp_path: Path) -> None:
    import json as _json
    s = Store(tmp_path)
    s.init(["beta", "lead"])
    (s.dir / "supervisor.json").write_text(_json.dumps({
        "agents": {"beta": {"wrapped": True, "cli": "claude",
                            "work_heartbeat": {"enabled": True, "interval_seconds": -1}}},
    }), encoding="utf-8")
    rc = cli.main(["--root", str(tmp_path), "wrap", "--for", "beta", "--cli", "claude",
                   "--loop", "--", sys.executable, "-c", "pass"])
    assert rc == 1                                         # launch config-blocked path
    assert s.read_config_blocked_hold("beta") is not None  # durable hold, no silent coerce


def test_cmd_wrap_interval_violation_blocks_visibly(tmp_path: Path) -> None:
    import json as _json
    s = Store(tmp_path)
    s.init(["beta", "lead"])
    # wrapped claude resolves stuck_after=180 -> bound = min(60, 60) = 60; 90 violates.
    (s.dir / "supervisor.json").write_text(_json.dumps({
        "agents": {"beta": {"wrapped": True, "cli": "claude",
                            "work_heartbeat": {"interval_seconds": 90}}},
    }), encoding="utf-8")
    rc = cli.main(["--root", str(tmp_path), "wrap", "--for", "beta", "--cli", "claude",
                   "--loop", "--", sys.executable, "-c", "pass"])
    assert rc == 1
    hold = s.read_config_blocked_hold("beta")
    assert hold is not None and "work_heartbeat" in (hold.get("summary") or "")


def test_cmd_wrap_codex_default_off_is_not_validated_or_started(tmp_path: Path) -> None:
    # codex stays default-OFF: an interval that WOULD violate the codex bound must not
    # block a codex launch (the block is disabled), preserving today's codex behavior.
    import json as _json
    s = Store(tmp_path)
    s.init(["beta", "lead"])
    (s.dir / "supervisor.json").write_text(_json.dumps({
        "agents": {"beta": {"wrapped": True, "cli": "codex",
                            "work_heartbeat": {"interval_seconds": 4000}}},
    }), encoding="utf-8")
    cfg_agent = {"wrapped": True, "cli": "codex",
                 "work_heartbeat": {"interval_seconds": 4000}}
    cfg = whb.resolve_work_heartbeat({}, cfg_agent, cli="codex", mode="wrapper-loop")
    assert cfg.enabled is False
    assert whb.interval_violation(cfg, stuck_after_seconds=2400.0) is None


# ------------------------------------------------------------------ store diagnostics


def test_store_work_heartbeat_status_roundtrip_and_corrupt(tmp_path: Path) -> None:
    s = Store(tmp_path)
    s.init(["beta", "lead"])
    assert s.read_work_heartbeat_status("beta") is None
    s.write_work_heartbeat_status("beta", {"stamps": 3, "stopped": STOPPED_TURN_END})
    got = s.read_work_heartbeat_status("beta")
    assert got == {"stamps": 3, "stopped": STOPPED_TURN_END}
    s.work_heartbeat_status_path("beta").write_text("{torn", encoding="utf-8")
    assert s.read_work_heartbeat_status("beta") is None    # corrupt reads as absent
