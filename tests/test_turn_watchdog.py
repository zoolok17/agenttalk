"""Tests for the per-turn watchdog (wrapper/turn_watchdog.py) and its wiring into
make_drive / run_loop. The OS snapshot adapter is tested with deterministic fakes; one
Windows-only integration test exercises native termination against an isolated sleeper.

Covers codex's acceptance gates: the pure two-factor discriminator matrix, config
resolution, the daemon controller's clean stop/join (no kill race) + fire/kill path, the
make_drive fake-watchdog classification (CLASS_AMBIGUOUS wins over rc/partial-stream noise),
the narrow watchdog-recovery heartbeat stamp vs the ordinary-failure clear, and the loop
invariant (cursor unchanged + message pending + one ambiguous attempt after a watchdog kill).
"""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

import pytest

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


class _WatchdogSelectionStore:
    def __init__(self, root: Path):
        self.root = root

    def _powershell_selection_lock(self):
        return contextlib.nullcontext()


def _watchdog_selection(path: str, revision: int, fingerprint: str) -> dict:
    return {
        "path": path,
        "selection_revision": revision,
        "selection_fingerprint": fingerprint,
    }


def test_watchdog_cache_uses_new_selection_or_unavailable_never_cached_a(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTTALK_ROOT", str(tmp_path))
    monkeypatch.setattr(twd, "Store", _WatchdogSelectionStore)
    twd._PWSH_CACHE = None
    spawned: list[str] = []
    monkeypatch.setattr(
        twd,
        "_run",
        lambda argv, timeout, env=None: spawned.append(argv[0]) or "ok",
    )
    a = _watchdog_selection(r"C:\PowerShellA\pwsh.exe", 1, "a" * 64)
    b = _watchdog_selection(r"D:\PowerShellB\pwsh.exe", 2, "b" * 64)
    reads = iter((a, a, b, b))
    monkeypatch.setattr(
        twd.supervisor_lifecycle,
        "read_selected_host_locked",
        lambda store: next(reads),
    )

    assert twd._run_selected_pwsh("one", 1.0) == "ok"
    assert twd._run_selected_pwsh("two", 1.0) == "ok"
    assert spawned == [a["path"], b["path"]]
    assert twd._PWSH_CACHE["key"][1:] == (2, "b" * 64)


def test_watchdog_selection_change_during_final_recheck_fails_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTTALK_ROOT", str(tmp_path))
    monkeypatch.setattr(twd, "Store", _WatchdogSelectionStore)
    twd._PWSH_CACHE = None
    spawned: list[str] = []
    monkeypatch.setattr(
        twd,
        "_run",
        lambda argv, timeout, env=None: spawned.append(argv[0]) or "unexpected",
    )
    b = _watchdog_selection(r"D:\PowerShellB\pwsh.exe", 2, "b" * 64)
    c = _watchdog_selection(r"E:\PowerShellC\pwsh.exe", 3, "c" * 64)
    reads = iter((b, c))
    monkeypatch.setattr(
        twd.supervisor_lifecycle,
        "read_selected_host_locked",
        lambda store: next(reads),
    )

    assert twd._run_selected_pwsh("probe", 1.0) is None
    assert spawned == []
    assert twd._PWSH_CACHE is None


@pytest.mark.parametrize("reason", ["unreadable", "future", "expired", "identity changed"])
def test_watchdog_selection_validation_error_fails_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
) -> None:
    monkeypatch.setenv("AGENTTALK_ROOT", str(tmp_path))
    monkeypatch.setattr(twd, "Store", _WatchdogSelectionStore)
    twd._PWSH_CACHE = {"key": ("old", 1, "a" * 64), "path": "A"}
    monkeypatch.setattr(
        twd.supervisor_lifecycle,
        "read_selected_host_locked",
        lambda store: (_ for _ in ()).throw(
            twd.supervisor_lifecycle.SupervisorLifecycleError(reason)
        ),
    )
    monkeypatch.setattr(
        twd,
        "_run",
        lambda *args, **kwargs: pytest.fail("an invalid selection must never execute"),
    )
    assert twd._run_selected_pwsh("probe", 1.0) is None
    assert twd._PWSH_CACHE is None


def test_windows_proc_start_selection_failure_is_no_kill_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(twd.sys, "platform", "win32")
    monkeypatch.setattr(twd, "_run_selected_pwsh", lambda command, timeout: None)
    assert twd.proc_start(1234) is None


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
    pids = [t["pid"] for t in d.kill_order]
    assert pids[-1] == ROOT                              # root is killed LAST
    assert pids.index(2002) < pids.index(2001)           # deepest first
    # each target carries its observed start for the kill-time recheck
    assert all("start" in t for t in d.kill_order)


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
        snapshot_fn=lambda: snap,
        kill_fn=lambda targets: [killed.append(t["pid"]) or t["pid"] for t in targets],
        wall_clock=lambda: 10_000.0)
    wd.start()
    wd.join(timeout=5.0)
    assert wd.result is not None and wd.result["fired"] is True
    assert wd.result["trigger"]["name"] in ("pwsh.exe", "node.exe")
    assert killed[-1] == ROOT and 2002 in killed       # root killed, leaves included
    assert "watchdog killed" in wd.result["summary"]


# ----------------------------------------------------------- kill-time start guard (Issue A)

def test_kill_one_windows_uses_sigterm_without_subprocess_or_sigkill_lookup(
        monkeypatch: pytest.MonkeyPatch) -> None:
    killed: list[tuple[int, int]] = []
    sigterm = signal.SIGTERM
    monkeypatch.setattr(twd.sys, "platform", "win32")
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.delattr(signal, "SIGKILL", raising=False)
    monkeypatch.setattr(
        twd.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("Windows termination must not launch taskkill"),
    )

    assert twd._kill_one(42) is True
    assert killed == [(42, sigterm)]


def test_kill_one_posix_uses_sigkill(monkeypatch: pytest.MonkeyPatch) -> None:
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(twd.sys, "platform", "linux")
    monkeypatch.setattr(signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, sig)))

    assert twd._kill_one(42) is True
    assert killed == [(42, 9)]


@pytest.mark.parametrize("error", [OSError(), ProcessLookupError(), PermissionError()])
def test_kill_one_failure_returns_false(
        monkeypatch: pytest.MonkeyPatch, error: OSError) -> None:
    def fail_kill(pid: int, sig: int) -> None:
        raise error

    monkeypatch.setattr(twd.sys, "platform", "win32")
    monkeypatch.setattr(os, "kill", fail_kill)
    monkeypatch.setattr(
        twd.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("Windows termination must not launch taskkill"),
    )

    assert twd._kill_one(42) is False


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows-only termination path")
def test_kill_one_windows_real_no_window_sleeper_exits_with_sigterm() -> None:
    sleeper = subprocess.Popen(  # noqa: S603  # nosec B603
        [sys.executable, "-c", "import time; time.sleep(60)"],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        assert twd._kill_one(sleeper.pid) is True
        assert sleeper.wait(timeout=10) == 15
    finally:
        if sleeper.poll() is None:
            sleeper.kill()
        sleeper.wait(timeout=10)


def test_kill_targets_kills_matching_start() -> None:
    killed: list = []
    out = twd.kill_targets([{"pid": 42, "start": 100.0}],
                           start_fn=lambda pid: 100.0,           # live start MATCHES
                           killer=lambda pid: killed.append(pid) or True)
    assert out == [42] and killed == [42]


def test_kill_targets_skips_reused_pid() -> None:
    killed: list = []
    twd.kill_targets([{"pid": 42, "start": 100.0}],
                     start_fn=lambda pid: 999.0,                 # live start CHANGED (reuse)
                     killer=lambda pid: killed.append(pid) or True)
    assert killed == []                                          # NOT killed


def test_kill_targets_skips_when_live_start_unavailable() -> None:
    killed: list = []
    twd.kill_targets([{"pid": 42, "start": 100.0}],
                     start_fn=lambda pid: None,                  # gone / unconfirmable
                     killer=lambda pid: killed.append(pid) or True)
    assert killed == []


def test_kill_targets_skips_when_expected_start_missing() -> None:
    killed: list = []
    twd.kill_targets([{"pid": 42, "start": None}],              # no observed start to verify
                     start_fn=lambda pid: 100.0,
                     killer=lambda pid: killed.append(pid) or True)
    assert killed == []


def test_kill_targets_leaves_first_only_matching() -> None:
    # a 3-target tree where the middle pid was reused -> it is skipped, the others killed
    killed: list = []
    starts = {2002: 9000.0, 2001: 7777.0, ROOT: 0.0}            # 2001 live start differs below
    twd.kill_targets(
        [{"pid": 2002, "start": 9000.0}, {"pid": 2001, "start": 0.0}, {"pid": ROOT, "start": 0.0}],
        start_fn=lambda pid: starts[pid], killer=lambda pid: killed.append(pid) or True)
    assert killed == [2002, ROOT]                               # 2001 (reused) skipped


# ----------------------------------------------------------- shared live predicate (Issue B)

def test_watchdog_effectively_live() -> None:
    live = twd.watchdog_effectively_live
    assert live(TurnWatchdogConfig(enabled=True)) is True
    assert live(TurnWatchdogConfig(enabled=False)) is False
    # sub-floor turn_elapsed WITHOUT opt-in -> NOT live (cmd_wrap disables it)
    assert live(TurnWatchdogConfig(enabled=True, turn_elapsed_seconds=1100.0)) is False
    # sub-floor WITH opt-in -> live
    assert live(TurnWatchdogConfig(enabled=True, turn_elapsed_seconds=1100.0,
                                   allow_low_turn_elapsed=True)) is True


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
