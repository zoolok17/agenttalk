"""Tests for the per-turn watchdog (wrapper/turn_watchdog.py) and its wiring into
make_drive / run_loop. The OS adapter (real snapshot/kill) is NOT exercised - everything
uses a deterministic FAKE snapshot/spawner so no real process is ever killed.

Covers codex's acceptance gates: the pure two-factor discriminator matrix, config
resolution, the daemon controller's clean stop/join (no kill race) + fire/kill path, the
make_drive fake-watchdog classification (CLASS_AMBIGUOUS wins over rc/partial-stream noise),
the narrow watchdog-recovery heartbeat stamp vs the ordinary-failure clear, and the loop
invariant (cursor unchanged + message pending + one ambiguous attempt after a watchdog kill).
"""
from __future__ import annotations

import json
from pathlib import Path

from agenttalk.store import Store
from agenttalk.wrapper import loop, run
from agenttalk.wrapper import session as wsession
from agenttalk.wrapper import turn_watchdog as twd
from agenttalk.wrapper.loop import CLASS_AMBIGUOUS, DriveOutcome
from agenttalk.wrapper.turn_watchdog import TurnWatchdogConfig, evaluate

ROOT = 1000
CFG = TurnWatchdogConfig(enabled=True, turn_elapsed_seconds=1800.0,
                         tool_descendant_alive_seconds=600.0, poll_seconds=10.0)


def _snap(*procs) -> dict:
    """procs = (pid, ppid, name, create_epoch) tuples -> snapshot dict."""
    return {pid: {"ppid": ppid, "name": name, "create_epoch": ce}
            for (pid, ppid, name, ce) in procs}


# ----------------------------------------------------------- pure discriminator matrix

def test_within_elapsed_never_fires() -> None:
    snap = _snap((ROOT, 1, "codex.exe", 0.0), (2001, ROOT, "node.exe", 0.0))
    d = evaluate(snap, root_pid=ROOT, root_start=0.0, elapsed=10.0, now=10_000.0, cfg=CFG)
    assert not d.fire and "within elapsed" in d.reason


def test_root_only_never_fires() -> None:
    snap = _snap((ROOT, 1, "codex.exe", 0.0))
    d = evaluate(snap, root_pid=ROOT, root_start=0.0, elapsed=9999.0, now=10_000.0, cfg=CFG)
    assert not d.fire and "no aged tool descendant" in d.reason


def test_codex_internal_descendants_only_never_fire() -> None:
    # the brain's own runner / a console host are EXCLUDED even when old
    snap = _snap((ROOT, 1, "codex.exe", 0.0),
                 (2001, ROOT, "codex-command-runner.exe", 0.0),
                 (2002, ROOT, "conhost.exe", 0.0))
    d = evaluate(snap, root_pid=ROOT, root_start=0.0, elapsed=9999.0, now=10_000.0, cfg=CFG)
    assert not d.fire


def test_non_codex_descendant_below_age_does_not_fire() -> None:
    # node alive only 100s (< 600s threshold): NOT yet a hang
    snap = _snap((ROOT, 1, "codex.exe", 0.0), (2001, ROOT, "node.exe", 9900.0))
    d = evaluate(snap, root_pid=ROOT, root_start=0.0, elapsed=9999.0, now=10_000.0, cfg=CFG)
    assert not d.fire and "no aged tool descendant" in d.reason


def test_non_codex_descendant_above_age_fires_and_kills_leaves_first() -> None:
    # codex -> pwsh -> node, both alive the whole turn (>= 600). Fire on the first aged tool
    # descendant (pwsh, shallowest in pre-order); kill order is leaves-first.
    snap = _snap((ROOT, 1, "codex.exe", 0.0),
                 (2001, ROOT, "pwsh.exe", 0.0),
                 (2002, 2001, "node.exe", 9000.0))
    d = evaluate(snap, root_pid=ROOT, root_start=0.0, elapsed=9999.0, now=10_000.0, cfg=CFG)
    assert d.fire and d.trigger["name"] in ("pwsh.exe", "node.exe")
    assert d.kill_order[-1] == ROOT                      # root is killed LAST
    assert d.kill_order.index(2002) < d.kill_order.index(2001)  # deepest first


def test_only_deep_descendant_aged_fires_on_it() -> None:
    # pwsh is YOUNG (recent create), only the node REPL is aged -> node is the trigger
    snap = _snap((ROOT, 1, "codex.exe", 0.0),
                 (2001, ROOT, "pwsh.exe", 9950.0),
                 (2002, 2001, "node.exe", 9000.0))
    d = evaluate(snap, root_pid=ROOT, root_start=0.0, elapsed=9999.0, now=10_000.0, cfg=CFG)
    assert d.fire and d.trigger["name"] == "node.exe"


def test_snapshot_unavailable_fails_open() -> None:
    assert not evaluate(None, root_pid=ROOT, root_start=0.0, elapsed=9999.0,
                        now=10_000.0, cfg=CFG).fire
    assert not evaluate({}, root_pid=ROOT, root_start=0.0, elapsed=9999.0,
                        now=10_000.0, cfg=CFG).fire


def test_root_start_mismatch_fails_open() -> None:
    # the recorded root pid was REUSED (snapshot create_epoch != recorded start) -> never kill
    snap = _snap((ROOT, 1, "codex.exe", 5000.0), (2001, ROOT, "node.exe", 5000.0))
    d = evaluate(snap, root_pid=ROOT, root_start=0.0, elapsed=9999.0, now=10_000.0, cfg=CFG)
    assert not d.fire and "start mismatch" in d.reason


def test_descendant_pid_reuse_reads_young_via_create_time() -> None:
    # a descendant pid present with a RECENT create_epoch (pid reused) reads as young even if
    # it was first-seen long ago -> create-time wins -> not eligible.
    snap = _snap((ROOT, 1, "codex.exe", 0.0), (2002, ROOT, "node.exe", 9950.0))
    d = evaluate(snap, root_pid=ROOT, root_start=0.0, elapsed=9999.0, now=10_000.0,
                 cfg=CFG, first_seen={2002: 0.0})   # first-seen old, but create-time recent
    assert not d.fire


def test_unnamed_descendant_is_not_a_candidate() -> None:
    snap = _snap((ROOT, 1, "codex.exe", 0.0), (2001, ROOT, "", 0.0))
    assert not evaluate(snap, root_pid=ROOT, root_start=0.0, elapsed=9999.0,
                        now=10_000.0, cfg=CFG).fire


def test_unknown_named_descendant_is_a_candidate() -> None:
    snap = _snap((ROOT, 1, "codex.exe", 0.0), (2001, ROOT, "weird-tool.exe", 0.0))
    assert evaluate(snap, root_pid=ROOT, root_start=0.0, elapsed=9999.0,
                    now=10_000.0, cfg=CFG).fire


def test_first_seen_fallback_ages_when_create_time_missing() -> None:
    snap = _snap((ROOT, 1, "codex.exe", 0.0), (2001, ROOT, "node.exe", None))
    # no create_epoch -> use first_seen (700s ago >= 600) to age it
    d = evaluate(snap, root_pid=ROOT, root_start=0.0, elapsed=9999.0, now=10_000.0,
                 cfg=CFG, first_seen={2001: 9300.0})
    assert d.fire


# ----------------------------------------------------------- config resolution

def test_resolve_defaults_and_default_enabled() -> None:
    assert twd.resolve_turn_watchdog({}, {}).enabled is False
    assert twd.resolve_turn_watchdog({}, {}, default_enabled=True).enabled is True
    c = twd.resolve_turn_watchdog({}, {})
    assert (c.turn_elapsed_seconds, c.tool_descendant_alive_seconds, c.poll_seconds) == (
        1800.0, 600.0, 10.0)


def test_resolve_per_agent_overrides_global_and_explicit_enabled_wins() -> None:
    cfg = {"turn_watchdog": {"enabled": True, "turn_elapsed_seconds": 1234}}
    agent = {"turn_watchdog": {"enabled": False, "tool_descendant_alive_seconds": 42}}
    c = twd.resolve_turn_watchdog(cfg, agent, default_enabled=True)
    assert c.enabled is False                          # explicit per-agent wins over default
    assert c.tool_descendant_alive_seconds == 42.0     # per-agent
    assert c.turn_elapsed_seconds == 1234.0            # falls back to global


def test_resolve_tolerates_corrupt_config() -> None:
    assert twd.resolve_turn_watchdog("nope", 5, default_enabled=True).enabled is True
    assert twd.resolve_turn_watchdog({"turn_watchdog": 7}, {"turn_watchdog": "x"}).enabled is False


# ----------------------------------------------------------- daemon controller

def test_controller_stops_clean_on_normal_completion_no_kill() -> None:
    # gate #1: a normal (short) turn -> stop()+join() exits the initial wait at once; the
    # watchdog never snapshots or kills, leaves no result, no thread leak.
    killed: list = []
    snaps = {"n": 0}

    def snapshot_fn():
        snaps["n"] += 1
        return _snap((ROOT, 1, "codex.exe", 0.0), (2001, ROOT, "node.exe", 0.0))

    wd = twd.TurnWatchdog(root_pid=ROOT, root_start=0.0,
                          cfg=TurnWatchdogConfig(enabled=True, turn_elapsed_seconds=100.0),
                          snapshot_fn=snapshot_fn, kill_fn=lambda pids: killed.extend(pids) or pids)
    wd.start()
    wd.stop()
    wd.join(timeout=5.0)
    assert wd.result is None and killed == [] and snaps["n"] == 0


def test_controller_fires_and_kills_when_descendant_hangs() -> None:
    killed: list = []
    snap = _snap((ROOT, 1, "codex.exe", 0.0),
                 (2001, ROOT, "pwsh.exe", 0.0),
                 (2002, 2001, "node.exe", 9000.0))
    wd = twd.TurnWatchdog(
        root_pid=ROOT, root_start=0.0,
        # turn_elapsed=0 -> skip the initial wait, poll immediately and fire on the first snap
        cfg=TurnWatchdogConfig(enabled=True, turn_elapsed_seconds=0.0, poll_seconds=0.01),
        snapshot_fn=lambda: snap, kill_fn=lambda pids: killed.extend(pids) or pids,
        wall_clock=lambda: 10_000.0)
    wd.start()
    wd.join(timeout=5.0)
    assert wd.result is not None and wd.result["fired"] is True
    assert wd.result["trigger"]["name"] in ("pwsh.exe", "node.exe")
    assert killed[-1] == ROOT and 2002 in killed       # root killed, leaves included
    assert "watchdog killed" in wd.result["summary"]


# ----------------------------------------------------------- make_drive wiring

class _FakeStream:
    """A spawner result: yields the given JSONL lines, then exposes returncode +
    watchdog_result exactly like _ProcStream does after iteration."""

    def __init__(self, lines, *, returncode=-1, watchdog_result=None):
        self._lines = lines
        self.returncode = returncode
        self.watchdog_result = watchdog_result

    def __iter__(self):
        return iter(self._lines)


def _store(tmp_path: Path) -> Store:
    s = Store(tmp_path)
    s.init(["lead", "beta"])
    return s


_PARTIAL = [json.dumps({"type": "turn.started"})]   # started, never completed
_WD_RESULT = {"fired": True, "summary": "turn watchdog killed hung tool descendant: "
              "name=node.exe pid=2002 age=900s elapsed=1800s", "trigger": {"name": "node.exe"}}


def test_make_drive_watchdog_classifies_ambiguous_and_wins_over_noise(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.send(sender="lead", recipient="beta", body="hi")
    st = wsession.load_session(s, "beta", "codex")
    # the stream both looks like a partial/nonzero failure AND carries a watchdog result;
    # the watchdog must WIN the classification (and supply the summary).
    drive = run.make_drive(s, "beta", "codex", st, ["codex"],
                           spawn=lambda a, i: _FakeStream(_PARTIAL, returncode=-1,
                                                          watchdog_result=_WD_RESULT))
    rec = {"id": "m1", "body": "hi"}
    outcome = drive(rec)
    assert isinstance(outcome, DriveOutcome) and outcome.ok is False
    assert outcome.failure_class == CLASS_AMBIGUOUS
    assert "node.exe" in outcome.summary                # the watchdog summary, not "partial stream"


def test_make_drive_watchdog_recovery_stamps_heartbeat(tmp_path: Path) -> None:
    s = _store(tmp_path)
    st = wsession.load_session(s, "beta", "codex")
    beats = {"n": 0}
    # an EMPTY stream produces no streaming-progress heartbeat, so the ONLY stamp is the
    # narrow watchdog-recovery one in drive()'s failure path.
    drive = run.make_drive(s, "beta", "codex", st, ["codex"],
                           heartbeat=lambda: beats.__setitem__("n", beats["n"] + 1),
                           spawn=lambda a, i: _FakeStream([], watchdog_result=_WD_RESULT))
    drive({"id": "m1", "body": "x"})
    assert beats["n"] == 1                              # narrow watchdog-recovery stamp fired


def test_make_drive_ordinary_failure_clears_heartbeat(tmp_path: Path) -> None:
    s = _store(tmp_path)
    st = wsession.load_session(s, "beta", "codex")
    s.write_heartbeat("beta")
    assert s.read_heartbeat("beta") is not None
    # an ORDINARY partial-stream failure (NO watchdog_result) must leave NO fresh heartbeat
    drive = run.make_drive(s, "beta", "codex", st, ["codex"],
                           spawn=lambda a, i: _FakeStream(_PARTIAL, returncode=-1))
    out = drive({"id": "m1", "body": "x"})
    assert out.ok is False and s.read_heartbeat("beta") is None


def test_make_drive_watchdog_default_path_stamps_store_heartbeat(tmp_path: Path) -> None:
    s = _store(tmp_path)
    st = wsession.load_session(s, "beta", "codex")
    s.clear_heartbeat("beta")
    # no injected heartbeat hook -> watchdog recovery stamps store.write_heartbeat directly
    drive = run.make_drive(s, "beta", "codex", st, ["codex"],
                           spawn=lambda a, i: _FakeStream(_PARTIAL, watchdog_result=_WD_RESULT))
    drive({"id": "m1", "body": "x"})
    assert s.read_heartbeat("beta") is not None


# ----------------------------------------------------------- loop integration

def test_loop_watchdog_failure_keeps_message_pending_one_ambiguous_attempt(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.send(sender="lead", recipient="beta", body="hung")
    st = wsession.load_session(s, "beta", "codex")
    drive = run.make_drive(s, "beta", "codex", st, ["codex"],
                           spawn=lambda a, i: _FakeStream(_PARTIAL, watchdog_result=_WD_RESULT))
    before = s.cursor("beta")
    loop.run_loop(s, "beta", drive, clock=lambda: 0.0, sleep=lambda d: None,
                  now_iso=lambda: "t", max_polls=1, k_poison=3, k_escalate=20)
    assert s.cursor("beta") == before                  # cursor UNCHANGED (message still pending)
    from agenttalk.wrapper import recv_api
    nxt = recv_api.next_record(s, "beta")
    assert nxt is not None and nxt["body"] == "hung"    # the message is re-peekable
    rec = s.attempt_record("beta", nxt["id"])
    assert int(rec["attempts_started"]) == 1           # exactly one attempt recorded
    assert int(rec.get("poison_eligible_failures") or 0) == 0   # ambiguous, NOT poison
    assert s.dead_lettered_count("beta") == 0
