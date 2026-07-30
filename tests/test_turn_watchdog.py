"""Tests for the per-turn watchdog (wrapper/turn_watchdog.py) and its wiring into
make_drive / run_loop. The OS snapshot adapter is tested with deterministic fakes; one
Windows-only integration test exercises native termination against an isolated sleeper.

Covers codex's acceptance gates: the pure two-factor discriminator matrix, config
resolution, the daemon controller's clean stop/join (no kill race) + fire/kill path, the
make_drive fake-watchdog classification (CLASS_AMBIGUOUS wins over rc/partial-stream noise),
the narrow watchdog-recovery heartbeat stamp vs the ordinary-failure clear, and the loop
invariant (cursor unchanged + message pending + one ambiguous attempt after a watchdog kill).
The held-open-pipe regression runs the real watchdog/stream/drive/loop stack in a separate
wrapper process and proves both recovery and the old no-wake failure direction.
"""
from __future__ import annotations

import contextlib
import json
import multiprocessing
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from agenttalk import wrapper_runtime as wr
from agenttalk.store import Store, _process_alive
from agenttalk.wrapper import loop, run
from agenttalk.wrapper import obligations
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


def test_controller_publishes_fire_when_root_kill_is_unconfirmed() -> None:
    callbacks: list[dict] = []
    snap = _snap((ROOT, 1, "codex.exe", 0.0), (2001, ROOT, "node.exe", 0.0))
    wd = twd.TurnWatchdog(
        root_pid=ROOT,
        root_start=0.0,
        cfg=TurnWatchdogConfig(
            enabled=True,
            turn_elapsed_seconds=0.0,
            tool_descendant_alive_seconds=0.0,
            poll_seconds=0.01,
        ),
        snapshot_fn=lambda: snap,
        kill_fn=lambda targets: [target["pid"] for target in targets if target["pid"] != ROOT],
        wall_clock=lambda: 10_000.0,
        on_fire=callbacks.append,
    )

    wd.start()
    wd.join(timeout=5.0)

    assert wd.result is not None and wd.result["fired"] is True
    assert ROOT not in wd.result["killed"]
    assert callbacks == [wd.result]


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
_COMPLETED = [
    json.dumps({"type": "turn.started"}),
    json.dumps({"type": "turn.completed"}),
]
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


def test_make_drive_watchdog_overrides_clean_completion(tmp_path: Path) -> None:
    s = _store(tmp_path)
    st = wsession.load_session(s, "beta", "codex")
    drive = run.make_drive(
        s,
        "beta",
        "codex",
        st,
        ["codex"],
        spawn=lambda _argv, _stdin: _FakeStream(
            _COMPLETED,
            returncode=0,
            watchdog_result=_WD_RESULT,
        ),
    )

    outcome = drive({"id": "m1", "body": "hi"})

    assert isinstance(outcome, DriveOutcome) and outcome.ok is False
    assert outcome.failure_class == CLASS_AMBIGUOUS


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


def test_proc_stream_watchdog_enabled_normal_exit_needs_no_wake() -> None:
    stream = run._ProcStream(
        [sys.executable, "-c", "print('done', flush=True)"],
        None,
        watchdog=TurnWatchdogConfig(
            enabled=True,
            turn_elapsed_seconds=30.0,
            poll_seconds=1.0,
        ),
        watchdog_snapshot_fn=lambda: None,
    )

    assert list(stream) == ["done\n"]
    assert stream.returncode == 0
    assert stream.watchdog_result is None


def test_proc_stream_watchdog_decoder_preserves_text_line_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = object.__new__(run._ProcStream)
    stream._proc = SimpleNamespace(stdout=object())
    stream._watchdog_stream_condition = threading.Condition()
    stream._watchdog_stream_interrupted = False
    stream._watchdog_stream_done = False
    chunks = iter((b"\xe2", b"\x80", b"\x9d\r", b"\ninvalid:\x9d", b"partial", b""))
    monkeypatch.setattr(run, "_read_ready_pipe_chunk", lambda _pipe: next(chunks))

    assert list(stream._iter_until_watchdog_or_eof()) == [
        "\u201d\n",
        "invalid:\ufffdpartial",
    ]
    assert stream._watchdog_stream_done


def test_proc_stream_watchdog_enabled_consumer_close_is_bounded() -> None:
    stream = run._ProcStream(
        [
            sys.executable,
            "-u",
            "-c",
            "while True:\n print('output', flush=True)",
        ],
        None,
        watchdog=TurnWatchdogConfig(
            enabled=True,
            turn_elapsed_seconds=30.0,
            poll_seconds=1.0,
        ),
        watchdog_snapshot_fn=lambda: None,
    )
    iterator = iter(stream)
    assert next(iterator) == "output\n"
    close_done = threading.Event()
    close_errors: list[BaseException] = []

    def close_owner() -> None:
        try:
            iterator.close()
        except BaseException as exc:  # noqa: BLE001 - report cleanup failures on test thread
            close_errors.append(exc)
        finally:
            close_done.set()

    closer = threading.Thread(target=close_owner, daemon=True)
    closer.start()
    try:
        assert close_done.wait(5.0), "watchdog-enabled consumer cleanup wedged"
    finally:
        if stream._proc.poll() is None:
            stream._proc.kill()
            stream._proc.wait(timeout=5.0)
        closer.join(5.0)

    assert not closer.is_alive()
    assert close_errors == []
    assert stream.returncode is not None


class _ObservedWatchdogProcStream(run._ProcStream):
    """Expose the watchdog callback boundary without changing its behavior."""

    def __init__(self, *args, **kwargs) -> None:
        self.watchdog_callback_done = threading.Event()
        super().__init__(*args, **kwargs)

    def _handle_watchdog_fire(self, result: dict) -> None:
        try:
            super()._handle_watchdog_fire(result)
        finally:
            self.watchdog_callback_done.set()


def _consume_proc_stream(
    stream: run._ProcStream,
    *,
    first_line: list[str],
    first_line_ready: threading.Event,
    owner_done: threading.Event,
    errors: list[BaseException],
) -> None:
    try:
        for line in stream:
            if not first_line:
                first_line.append(line)
                first_line_ready.set()
    except BaseException as exc:  # noqa: BLE001 - report owner failures on test thread
        errors.append(exc)
    finally:
        owner_done.set()


def test_proc_stream_does_not_wake_until_reported_root_kill_is_confirmed() -> None:
    first_line_ready = threading.Event()
    owner_done = threading.Event()
    first_line: list[str] = []
    errors: list[BaseException] = []
    spawned: dict[str, int] = {}

    def snapshot():
        if not first_line_ready.wait(5.0):
            return None
        root = spawned["pid"]
        return _snap(
            (root, 1, "codex.exe", 1000.0),
            (root + 1_000_000, root, "node.exe", 0.0),
        )

    # Model the real signal-vs-exit race: kill_targets reports every requested PID
    # immediately, while the actual root process remains alive.
    stream = _ObservedWatchdogProcStream(
        [
            sys.executable,
            "-u",
            "-c",
            "import threading; print('ready', flush=True); threading.Event().wait()",
        ],
        None,
        watchdog=TurnWatchdogConfig(
            enabled=True,
            turn_elapsed_seconds=0.0,
            tool_descendant_alive_seconds=0.0,
            poll_seconds=0.01,
        ),
        watchdog_snapshot_fn=snapshot,
        watchdog_kill_fn=lambda targets: [target["pid"] for target in targets],
        watchdog_wall_clock=lambda: 1000.0,
        on_spawn=lambda pid, _start: spawned.__setitem__("pid", pid),
    )
    owner = threading.Thread(
        target=_consume_proc_stream,
        kwargs={
            "stream": stream,
            "first_line": first_line,
            "first_line_ready": first_line_ready,
            "owner_done": owner_done,
            "errors": errors,
        },
        daemon=True,
    )
    owner.start()
    try:
        assert stream.watchdog_callback_done.wait(5.0), "watchdog did not fire"
        assert stream._proc.poll() is None
        assert not stream._watchdog_stream_interrupted
        assert not owner_done.is_set()
        assert stream._watchdog_root_waiter is not None
    finally:
        if stream._proc.poll() is None:
            stream._proc.kill()
        assert owner_done.wait(5.0), "owner did not resume after confirmed root exit"
        owner.join(5.0)

    assert first_line == ["ready\n"]
    assert errors == []
    assert stream.returncode is not None


def test_proc_stream_watchdog_reaps_stdout_when_inherited_writer_survives() -> None:
    first_line_ready = threading.Event()
    owner_done = threading.Event()
    first_line: list[str] = []
    errors: list[BaseException] = []
    spawned: dict[str, object] = {}
    readers_before = {
        thread.ident
        for thread in threading.enumerate()
        if thread.name == "turn-stdout-reader"
    }
    writer_pid: int | None = None

    def snapshot():
        if not first_line_ready.wait(5.0):
            return None
        root = int(spawned["pid"])
        writer = int(first_line[0].strip())
        return _snap(
            (root, 1, "codex.exe", 1000.0),
            (writer, root, "conhost.exe", 1000.0),
            (root + 1_000_000, root, "node.exe", 0.0),
        )

    def kill(targets):
        stream = spawned["stream"]
        assert isinstance(stream, run._ProcStream)
        stream._proc.kill()
        stream._proc.wait(timeout=5.0)
        return [target["pid"] for target in targets]

    writer_code = "import threading; threading.Event().wait(30.0)"
    root_code = (
        "import subprocess, sys, threading; "
        f"writer = subprocess.Popen([sys.executable, '-c', {writer_code!r}], "
        "stdout=sys.stdout, stderr=sys.stderr); "
        "print(writer.pid, flush=True); "
        "threading.Event().wait()"
    )
    stream = _ObservedWatchdogProcStream(
        [sys.executable, "-u", "-c", root_code],
        None,
        watchdog=TurnWatchdogConfig(
            enabled=True,
            turn_elapsed_seconds=0.0,
            tool_descendant_alive_seconds=0.0,
            poll_seconds=0.01,
        ),
        watchdog_snapshot_fn=snapshot,
        watchdog_kill_fn=kill,
        watchdog_wall_clock=lambda: 1000.0,
        on_spawn=lambda pid, _start: spawned.__setitem__("pid", pid),
    )
    spawned["stream"] = stream
    owner = threading.Thread(
        target=_consume_proc_stream,
        kwargs={
            "stream": stream,
            "first_line": first_line,
            "first_line_ready": first_line_ready,
            "owner_done": owner_done,
            "errors": errors,
        },
        daemon=True,
    )
    owner.start()
    try:
        assert stream.watchdog_callback_done.wait(5.0), "watchdog did not fire"
        assert owner_done.wait(5.0), "owner stayed blocked on the inherited writer"
        owner.join(5.0)
        writer_pid = int(first_line[0].strip())
        assert _process_alive(writer_pid), "fixture writer did not outlive the root"
        readers_after = {
            thread.ident
            for thread in threading.enumerate()
            if thread.name == "turn-stdout-reader"
        }
        assert readers_after <= readers_before
        assert stream._proc.stdout is not None
        assert stream._proc.stdout.closed
    finally:
        if stream._proc.poll() is None:
            stream._proc.kill()
            stream._proc.wait(timeout=5.0)
        if writer_pid is None and first_line:
            writer_pid = int(first_line[0].strip())
        if writer_pid is not None and _process_alive(writer_pid):
            twd._kill_one(writer_pid)
        reader = getattr(stream, "_watchdog_stream_reader", None)
        if reader is not None:
            reader.join(5.0)

    assert errors == []
    assert stream.returncode is not None


def test_proc_stream_waits_for_root_exit_when_kill_result_omits_root() -> None:
    root_exited = threading.Event()
    stream = object.__new__(run._ProcStream)
    stream.pid = ROOT
    stream._watchdog_stream_condition = threading.Condition()
    stream._watchdog_stream_interrupted = False
    stream._watchdog_root_waiter = None

    def wait_for_root():
        if not root_exited.wait(5.0):
            raise subprocess.TimeoutExpired("watchdog-root", 5.0)
        return -9

    stream._proc = SimpleNamespace(poll=lambda: None, wait=wait_for_root)
    stream._handle_watchdog_fire({"killed": [2001]})

    assert stream._watchdog_root_waiter is not None
    assert not stream._watchdog_stream_interrupted
    root_exited.set()
    stream._watchdog_root_waiter.join(5.0)
    assert not stream._watchdog_root_waiter.is_alive()
    assert stream._watchdog_stream_interrupted


def test_proc_stream_incomplete_owner_exit_is_cancelled() -> None:
    stream = object.__new__(run._ProcStream)
    stream._watchdog_stream_condition = threading.Condition()
    stream._watchdog_stream_interrupted = False
    stream._watchdog_stream_done = False

    assert stream._cancel_watchdog_stream_after_consumer_exit() is True
    assert stream._watchdog_stream_interrupted


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


# ----------------------------------------------------------- held-open stdout regression

class _HarnessStdin:
    def write(self, text: str) -> int:
        return len(text)

    def close(self) -> None:
        return None


class _HarnessProc:
    def __init__(self, stream_blocked, pipe_release, child_killed) -> None:
        self.pid = ROOT
        self.stdin = _HarnessStdin()
        read_fd, write_fd = os.pipe()
        self.stdout = os.fdopen(
            read_fd,
            "r",
            encoding="utf-8",
            errors="replace",
        )
        self._stdout_writer = os.fdopen(write_fd, "wb", buffering=0)
        self.returncode = None
        self._child_killed = child_killed
        stream_blocked.set()
        self._writer_closer = threading.Thread(
            target=self._close_writer_after_release,
            args=(pipe_release,),
            daemon=True,
        )
        self._writer_closer.start()

    def _close_writer_after_release(self, pipe_release) -> None:
        if pipe_release.wait(10.0):
            self._stdout_writer.close()

    def poll(self):
        return -9 if self._child_killed.is_set() else None

    def wait(self, timeout=None):
        if not self._child_killed.wait(5.0 if timeout is None else timeout):
            raise subprocess.TimeoutExpired("watchdog-harness", timeout)
        self.returncode = -9
        return self.returncode

    def terminate(self) -> None:
        self._child_killed.set()

    def kill(self) -> None:
        self._child_killed.set()


def _run_held_pipe_wrapper(
    root: str,
    stream_blocked,
    pipe_release,
    child_killed,
    failure_accounted,
    wrapper_release,
    wake_callback_reached,
    disable_wake,
    errors,
) -> None:
    """Run the real wrapper stack in a separate process with only Popen/OS probes faked."""
    try:
        store = Store(root)
        state = wsession.SessionState(
            cli="codex",
            codex_thread_id="thread-held-pipe",
            turns=1,
        )
        generation = "watchdog-held-pipe"
        writer = wr.WrapperRuntimeWriter(
            store.state_dir,
            "beta",
            generation,
        )
        proc = _HarnessProc(stream_blocked, pipe_release, child_killed)
        run.subprocess = SimpleNamespace(
            Popen=lambda *_args, **_kwargs: proc,
            PIPE=subprocess.PIPE,
            STDOUT=subprocess.STDOUT,
            CREATE_NO_WINDOW=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            TimeoutExpired=subprocess.TimeoutExpired,
        )
        wr.process_start_token = lambda _pid: None
        wake = run._ProcStream._handle_watchdog_fire

        def observed_wake(self, result):
            wake_callback_reached.set()
            if not disable_wake:
                wake(self, result)

        run._ProcStream._handle_watchdog_fire = observed_wake
        twd._START_EPS = float("inf")

        def snapshot():
            if not stream_blocked.wait(5.0):
                return None
            now = time.time()
            return _snap(
                (ROOT, 1, "codex.exe", now),
                (2001, ROOT, "node.exe", 0.0),
            )

        def kill(targets):
            child_killed.set()
            # The protected root can exit while its descendants are killed and
            # therefore be absent from kill_targets' return value.
            return [target["pid"] for target in targets if target["pid"] != ROOT]

        drive = run.make_drive(
            store,
            "beta",
            "codex",
            state,
            ["codex"],
            render=False,
            agenttalk_preflight=lambda: None,
            runtime_writer=writer,
            turn_watchdog=TurnWatchdogConfig(
                enabled=True,
                turn_elapsed_seconds=0.0,
                tool_descendant_alive_seconds=0.0,
                poll_seconds=0.01,
            ),
            watchdog_snapshot_fn=snapshot,
            watchdog_kill_fn=kill,
        )
        store.write_waiting("beta", {
            "mode": "wrapper-loop",
            "wrapper_generation": generation,
            "wait_token": generation,
            "pid": os.getpid(),
        })
        policy = obligations.PolicySnapshot.from_mapping({
            "schema_version": 1,
            "agents": {"beta": {"grade": obligations.DETECTION_GRADE}},
        }, "beta")
        gate = obligations.DetectionCommitGate(
            store,
            "beta",
            policy,
            fence=generation,
        )

        def backoff(_seconds: float) -> None:
            failure_accounted.set()
            if not wrapper_release.wait(10.0):
                raise RuntimeError("watchdog regression wrapper was not released")

        loop.run_loop(
            store,
            "beta",
            drive,
            clock=lambda: 0.0,
            sleep=backoff,
            max_polls=1,
            k_poison=3,
            k_escalate=20,
            on_runtime_idle=writer.idle,
            wrapper_generation=generation,
            commit_gate=gate,
        )
    except BaseException as exc:  # noqa: BLE001 - propagate subprocess diagnostics
        errors.put(f"{type(exc).__name__}: {exc}")


def _assert_watchdog_failure_accounted(failure_accounted, timeout: float) -> None:
    assert failure_accounted.wait(timeout), (
        "watchdog killed the child but the wrapper stayed wedged on inherited stdout"
    )


@pytest.mark.subprocess
@pytest.mark.parametrize("disable_wake", [False, True], ids=["recovers", "no-wake-control"])
def test_watchdog_kill_held_pipe_wrapper_state(
    tmp_path: Path,
    disable_wake: bool,
) -> None:
    store = _store(tmp_path)
    store.set_operator_facing("lead")
    inbound = store.send(
        sender="lead",
        recipient="beta",
        kind="question",
        body="held stdout after watchdog kill",
        meta={"request_id": f"q-watchdog-{'no-wake' if disable_wake else 'recovers'}"},
    )
    store.write_heartbeat("beta")
    heartbeat_path = store.state_dir / "beta.heartbeat"
    old_ns = 946_684_800_000_000_000
    os.utime(heartbeat_path, ns=(old_ns, old_ns))
    heartbeat_before = heartbeat_path.stat().st_mtime_ns

    ctx = multiprocessing.get_context("spawn")
    stream_blocked = ctx.Event()
    pipe_release = ctx.Event()
    child_killed = ctx.Event()
    failure_accounted = ctx.Event()
    wrapper_release = ctx.Event()
    wake_callback_reached = ctx.Event()
    errors = ctx.Queue()
    wrapper = ctx.Process(
        target=_run_held_pipe_wrapper,
        args=(
            str(tmp_path),
            stream_blocked,
            pipe_release,
            child_killed,
            failure_accounted,
            wrapper_release,
            wake_callback_reached,
            disable_wake,
            errors,
        ),
    )
    wrapper.start()
    try:
        assert child_killed.wait(5.0), "watchdog did not fire"
        assert wake_callback_reached.wait(5.0), "watchdog result was not published"
        assert wrapper.is_alive()
        if disable_wake:
            with pytest.raises(AssertionError, match="wrapper stayed wedged"):
                _assert_watchdog_failure_accounted(failure_accounted, 0.0)
            assert not failure_accounted.is_set()
            assert heartbeat_path.stat().st_mtime_ns == heartbeat_before
            runtime = wr.read_runtime(store.state_dir, "beta")
            assert runtime["status"] == wr.STATUS_VALID
            record = runtime["record"]
            assert record["phase"] == wr.PHASE_ACTIVE
            assert record["last_outcome"] is None
            assert record["turn_id"] is not None
            assert record["message_id"] == inbound.id
            assert record["cli_launcher_pid"] == ROOT
        else:
            _assert_watchdog_failure_accounted(failure_accounted, 5.0)
            assert wrapper.is_alive()
            assert heartbeat_path.stat().st_mtime_ns > heartbeat_before
            runtime = wr.read_runtime(store.state_dir, "beta")
            assert runtime["status"] == wr.STATUS_VALID
            record = runtime["record"]
            assert record["phase"] == wr.PHASE_IDLE
            assert record["last_outcome"] == wr.OUTCOME_FAILED
            assert record["turn_id"] is None
            assert record["message_id"] is None
            assert record["cli_launcher_pid"] is None
            assert record["cli_launcher_start"] is None

        assert store.cursor("beta") == ""
        assert store.dead_letter_attempts("beta")["messages"] == {}
        ledger = json.loads(
            (store.state_dir / "owed-action" / "ledger.json").read_text(encoding="utf-8")
        )
        admission = next(iter(ledger["obligations"].values()))
        assert admission["paid_dispatches_total"] == 1
        reservation = next(iter(admission["reservations"].values()))
        transitions = [row["transition"] for row in ledger["transitions"]]
        if disable_wake:
            assert reservation["state"] == "dispatching"
            assert "DISPATCH_ATTEMPT_STARTED" in transitions
            assert "OWED_ACTION_MISSING" not in transitions
        else:
            assert reservation["state"] == "completed"
            assert reservation["action_attempted"] is False
            assert transitions.index("DISPATCH_ATTEMPT_STARTED") < transitions.index(
                "OWED_ACTION_MISSING"
            )
            assert any(message.id == inbound.id for message in store.valid_messages())
    finally:
        pipe_release.set()
        wrapper_release.set()
        wrapper.join(5.0)
        if wrapper.is_alive():
            wrapper.terminate()
            wrapper.join(5.0)
    assert wrapper.exitcode == 0
    try:
        error = errors.get_nowait()
    except queue.Empty:
        error = None
    assert error is None
