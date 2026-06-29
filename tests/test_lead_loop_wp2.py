"""WP2 - the managed lead-loop CONTROLLER (wrap --loop --lead-loop) + its exit
markers and the supervisor relaunch rules. cli-AGNOSTIC by construction.
"""
from __future__ import annotations

import os
from pathlib import Path

import agenttalk.store as store_mod
from agenttalk import cli
from agenttalk.store import PROC_ALIVE, Store

ALIVE = os.getpid()
DEAD = 2 ** 31 - 1


def _store(tmp_path: Path, agents=("lead", "beta")) -> Store:
    s = Store(tmp_path)
    s.init(list(agents))
    return s


# ----------------------------------------------------------- controller exit markers

def test_lead_loop_exit_marker_roundtrip_and_clear(tmp_path: Path) -> None:
    s = _store(tmp_path)
    assert s.read_lead_loop_exit("beta") is None  # absent -> None
    s.write_lead_loop_exit("beta", state=s.LEAD_LOOP_EXIT_STOOD_DOWN,
                           owner_pid=4321, reason="human release")
    m = s.read_lead_loop_exit("beta")
    assert m is not None and m["state"] == "stood_down"
    assert m["owner_pid"] == 4321 and m["reason"] == "human release"
    s.clear_lead_loop_exit("beta")
    assert s.read_lead_loop_exit("beta") is None  # cleared


def test_lead_loop_exit_marker_degrade_safe(tmp_path: Path) -> None:
    s = _store(tmp_path)
    # a torn / non-dict marker reads as None, never raises
    s.lead_loop_exit_path("beta").write_text("{not json", encoding="utf-8")
    assert s.read_lead_loop_exit("beta") is None
    s.lead_loop_exit_path("beta").write_text("[]", encoding="utf-8")
    assert s.read_lead_loop_exit("beta") is None


def test_lead_loop_exit_marker_cleared_by_reset(tmp_path: Path) -> None:
    # the exit marker lives in state/ -> reset clears it (like the lease), but reset
    # PRESERVES the dead-letter sink.
    s = _store(tmp_path)
    s.write_lead_loop_exit("beta", state=s.LEAD_LOOP_EXIT_BLOCKED, owner_pid=4321)
    sink = s.dead_letter_dir / "beta"
    sink.mkdir(parents=True, exist_ok=True)
    (sink / "20260101-000000-000000-aaaa.json").write_text("{}", encoding="utf-8")
    s.reset()
    assert s.read_lead_loop_exit("beta") is None                    # marker cleared
    assert (sink / "20260101-000000-000000-aaaa.json").exists()     # sink preserved


# ----------------------------------------------------------- controller lifecycle

def test_lead_loop_heartbeat_renews_lease_and_lease_owns_waiting(tmp_path, monkeypatch):
    # condition 4: the combined heartbeat renews the lease on the idle stamp; and
    # manage_waiting=False means the LEASE owns the .waiting mirror (run_loop does not
    # write the generic wrapper-loop marker that would clobber it).
    from agenttalk.wrapper import loop as wloop
    s = _store(tmp_path)
    s.set_managed_lead_loop("beta")
    monkeypatch.setattr(store_mod, "_process_liveness", lambda pid: PROC_ALIVE)
    s.acquire_lead_loop_lease("beta", owner_pid=ALIVE, ttl_seconds=900, now=1000.0,
                              lease_id="owner")

    def hb():
        s.write_heartbeat("beta")
        s.renew_lead_loop_lease("beta", lease_id="owner", ttl_seconds=900, now=2000.0)

    wloop.run_loop(s, "beta", lambda r: True, max_polls=1, heartbeat=hb,
                   manage_waiting=False, clock=lambda: 0.0, sleep=lambda _s: None)
    assert s.read_lead_loop_lease("beta")["expires_at"] == 2900.0  # renewed by the stamp
    w = s.read_waiting("beta")
    assert w is not None and w.get("lead_loop") is True  # lease mirror, not wrapper-loop


def test_lead_loop_acquire_blocked_exits_blocked_with_marker(tmp_path, monkeypatch):
    # condition 5: another LIVE owner holds the lease -> wrap --lead-loop stands down
    # with the dedicated blocked exit code + a blocked marker (no real CLI spawn: it
    # returns before the loop).
    s = _store(tmp_path)
    s.set_managed_lead_loop("beta")
    monkeypatch.setattr(store_mod, "_process_liveness", lambda pid: PROC_ALIVE)
    s.acquire_lead_loop_lease("beta", owner_pid=ALIVE, now=__import__("time").time(),
                              lease_id="incumbent")
    rc = cli.main(["--root", str(tmp_path), "wrap", "--for", "beta", "--cli", "codex",
                   "--loop", "--lead-loop", "--", "python", "-c", "pass"])
    assert rc == cli._LEAD_LOOP_BLOCKED_EXIT
    m = Store(tmp_path).read_lead_loop_exit("beta")
    assert m is not None and m["state"] == "blocked"
    # the incumbent lease is untouched (we did NOT steal a live owner)
    assert Store(tmp_path).read_lead_loop_lease("beta")["lease_id"] == "incumbent"


def test_lead_loop_clean_return_is_stand_down(tmp_path, monkeypatch):
    # condition 4/(a): a CLEAN run_loop return (valid human release/end) -> release the
    # lease + write a stood-down marker + the dedicated stand-down exit code (so the
    # supervisor does NOT relaunch the deliberate stand-down).
    from agenttalk.wrapper import loop as wloop
    s = _store(tmp_path)
    s.set_managed_lead_loop("beta")
    monkeypatch.setattr(store_mod, "_process_liveness", lambda pid: PROC_ALIVE)
    monkeypatch.setattr(wloop, "run_loop", lambda *a, **k: 0)  # simulate a clean stop
    rc = cli._wrap_loop_mode(s, "beta", cli="codex", base_argv=["python", "-c", "pass"],
                             sender="beta", min_interval=5.0, render=False, lead_loop=True)
    assert rc == cli._LEAD_LOOP_STOOD_DOWN_EXIT
    assert s.read_lead_loop_lease("beta") is None  # lease released
    m = s.read_lead_loop_exit("beta")
    assert m is not None and m["state"] == "stood_down"


def test_lead_loop_crash_releases_lease_no_marker_relaunch(tmp_path, monkeypatch):
    # condition (a): a CRASH (run_loop raises) -> best-effort lease release + NO exit
    # marker (so the supervisor RELAUNCHES + the relaunch re-acquires). The exception
    # propagates.
    from agenttalk.wrapper import loop as wloop
    import pytest
    s = _store(tmp_path)
    s.set_managed_lead_loop("beta")
    monkeypatch.setattr(store_mod, "_process_liveness", lambda pid: PROC_ALIVE)

    def boom(*a, **k):
        raise RuntimeError("controller crash")
    monkeypatch.setattr(wloop, "run_loop", boom)
    with pytest.raises(RuntimeError):
        cli._wrap_loop_mode(s, "beta", cli="codex", base_argv=["python", "-c", "pass"],
                            sender="beta", min_interval=5.0, render=False, lead_loop=True)
    assert s.read_lead_loop_lease("beta") is None        # best-effort released
    assert s.read_lead_loop_exit("beta") is None          # NO stand-down marker -> relaunch


def test_lead_loop_requires_managed_identity(tmp_path):
    # condition 1: --lead-loop on a NON-managed agent exits 2 (config error), no lease.
    s = _store(tmp_path)  # beta NOT managed
    rc = cli.main(["--root", str(tmp_path), "wrap", "--for", "beta", "--cli", "codex",
                   "--loop", "--lead-loop", "--", "python", "-c", "pass"])
    assert rc == 2
    assert s.read_lead_loop_lease("beta") is None


def test_lead_loop_requires_loop_and_not_one_shot(tmp_path):
    # --lead-loop requires --loop and is incompatible with --one-shot.
    s = _store(tmp_path)
    s.set_managed_lead_loop("beta")
    assert cli.main(["--root", str(tmp_path), "wrap", "--for", "beta", "--cli", "codex",
                     "--lead-loop", "--", "python", "-c", "pass"]) == 2  # no --loop
    assert cli.main(["--root", str(tmp_path), "wrap", "--for", "beta", "--cli", "codex",
                     "--loop", "--lead-loop", "--one-shot", "--to-request", "q-1",
                     "--", "python", "-c", "pass"]) == 2  # one-shot incompatible


def test_lead_loop_acquire_uses_resolved_window(tmp_path, monkeypatch):
    # condition 2 / residual #3: the controller threads resolve_timing's
    # heartbeat_stale_after into acquire_lead_loop_lease (NOT the 120s default). Capture
    # the kwarg the controller passes.
    from agenttalk.wrapper import loop as wloop
    s = _store(tmp_path)
    s.set_managed_lead_loop("beta")
    captured = {}
    real_acquire = s.acquire_lead_loop_lease

    def spy(agent, **kw):
        captured.update(kw)
        return real_acquire(agent, **kw)
    monkeypatch.setattr(s, "acquire_lead_loop_lease", spy)
    monkeypatch.setattr(wloop, "run_loop", lambda *a, **k: 0)
    sup_cfg = {"agents": {"beta": {"wrapped": True, "cli": "codex"}}}  # codex window 2400 (v0.46.0)
    cli._wrap_loop_mode(s, "beta", cli="codex", base_argv=["python", "-c", "pass"],
                        sender="beta", min_interval=5.0, render=False, lead_loop=True,
                        supervisor_config=sup_cfg)
    # the steal-window tracks the wrapped-codex stuck_after (raised to 2400 so the supervisor
    # never preempts the per-turn watchdog), NOT the 120s default.
    assert captured.get("heartbeat_stale_after") == 2400.0


# ----------------------------------------------------------- token-strip (condition 3, security)

def test_lead_loop_child_env_strips_lease_token(tmp_path, monkeypatch):
    # The model child must NEVER see AGENTTALK_LEAD_LOOP_LEASE - else an accidental
    # model-side `agenttalk drain` could bypass the single-consumer guard (reviewer-1
    # Slice-1 residual). The wrapper strips it from the child env at the single Popen
    # choke point (unconditional, defense-in-depth). Definitive real-subprocess check.
    import sys as _sys
    from agenttalk.store import LEAD_LOOP_LEASE_ENV
    from agenttalk.wrapper.run import _ProcStream
    monkeypatch.setenv(LEAD_LOOP_LEASE_ENV, "secret-bypass-token")
    code = ("import os, sys; "
            "sys.stdout.write('HAS' if os.environ.get('AGENTTALK_LEAD_LOOP_LEASE') "
            "else 'NONE')")
    out = "".join(_ProcStream([_sys.executable, "-c", code], None))
    assert "NONE" in out and "HAS" not in out  # token stripped from the child env


# ----------------------------------------- codex WP2 review folds (lost-lease / re-arm)

def test_lead_loop_heartbeat_raises_hard_on_lost_lease(tmp_path, monkeypatch):
    # codex blocker 1 (refined): a lost lease (renew -> None) is a HARD signal - the
    # combined heartbeat RAISES (does not merely skip the stamp), so the loop STOPS at
    # once instead of continuing to run/consume unguarded until the stale threshold.
    import pytest
    from agenttalk.wrapper import loop as wloop
    s = _store(tmp_path)
    s.set_managed_lead_loop("beta")
    monkeypatch.setattr(store_mod, "_process_liveness", lambda pid: PROC_ALIVE)
    captured = {}
    monkeypatch.setattr(wloop, "run_loop", lambda *a, **k: captured.update(k) or 0)
    cli._wrap_loop_mode(s, "beta", cli="codex", base_argv=["python", "-c", "pass"],
                        sender="beta", min_interval=5.0, render=False, lead_loop=True)
    hb = captured["heartbeat"]
    (s.state_dir / "beta.heartbeat").unlink(missing_ok=True)
    assert s.read_lead_loop_lease("beta") is None  # clean return released the lease
    with pytest.raises(cli._LeadLoopLeaseLost):
        hb()
    assert s.read_heartbeat("beta") is None  # never stamped on a lost lease


def test_lead_loop_lost_lease_stops_consuming_and_exits_no_marker(tmp_path, monkeypatch):
    # codex blocker 1: the ownership GATE re-verifies the lease BEFORE consuming each
    # record, so a lost lease stops consumption IMMEDIATELY: the gated drive raises, the
    # controller exits with the lease-lost code + NO marker (supervisor relaunches ->
    # re-acquire / HOLD), and the real model drive is NEVER invoked for that record.
    from agenttalk.wrapper import loop as wloop
    s = _store(tmp_path)
    s.set_managed_lead_loop("beta")
    monkeypatch.setattr(store_mod, "_process_liveness", lambda pid: PROC_ALIVE)

    def fake_run_loop(store_, agent_, drive_, **k):
        store_.release_lead_loop_lease(agent_)   # lease lost (force-released) mid-run
        drive_({"id": "m1"})                     # the ownership gate must raise here
        return 0                                 # unreachable
    monkeypatch.setattr(wloop, "run_loop", fake_run_loop)
    rc = cli._wrap_loop_mode(s, "beta", cli="codex", base_argv=["python", "-c", "pass"],
                             sender="beta", min_interval=5.0, render=False, lead_loop=True)
    assert rc == cli._LEAD_LOOP_LEASE_LOST_EXIT
    assert s.read_lead_loop_exit("beta") is None  # NO marker -> supervisor relaunches


def test_clear_restart_supersedes_lead_loop_exit_marker(tmp_path):
    # codex blocker 2: an operator re-arm (request-restart -> the .ps1 calls
    # `supervise --clear-restart` on the confirmed relaunch, BEFORE the child acquires)
    # must supersede a stale stood_down/blocked exit marker - else a relaunch that fails
    # before acquire re-triggers the HOLD rule forever.
    s = _store(tmp_path)
    s.set_managed_lead_loop("beta")
    s.write_restart_request("beta", {"request_id": "rr-x"})
    s.write_lead_loop_exit("beta", state=s.LEAD_LOOP_EXIT_STOOD_DOWN, owner_pid=4321)
    rc = cli.main(["--root", str(tmp_path), "supervise", "--clear-restart",
                   "--for", "beta", "--request-id", "rr-x"])
    assert rc == 0
    assert s.read_lead_loop_exit("beta") is None  # superseded by the operator re-arm


def test_clear_restart_no_match_does_not_clear_exit_marker(tmp_path):
    # codex over-clear blocker: a clear-restart that matches NOTHING (stale/typo rid)
    # must NOT delete a deliberate stood_down marker - no re-arm without a CONFIRMED
    # restart. Only a matched/cleared restart marker supersedes the exit marker.
    s = _store(tmp_path)
    s.set_managed_lead_loop("beta")
    s.write_lead_loop_exit("beta", state=s.LEAD_LOOP_EXIT_STOOD_DOWN, owner_pid=4321)
    rc = cli.main(["--root", str(tmp_path), "supervise", "--clear-restart",
                   "--for", "beta", "--request-id", "rr-missing"])  # no marker present
    assert rc == 0
    assert s.read_lead_loop_exit("beta") is not None  # NOT cleared (no confirmed restart)


# ------------------- consume-boundary ownership guard (codex deeper blocker)

def test_run_loop_pre_commit_guards_success_commit(tmp_path, monkeypatch):
    # codex deeper blocker: a lease lost DURING a successful turn must block the cursor
    # advance. run_loop calls pre_commit BEFORE the success commit, so a renew-or-raise
    # hook stops the advance even though drive returned ok.
    import pytest
    from agenttalk.wrapper import loop as wloop
    s = _store(tmp_path)
    s.set_managed_lead_loop("beta")
    monkeypatch.setattr(store_mod, "_process_liveness", lambda pid: PROC_ALIVE)
    s.acquire_lead_loop_lease("beta", owner_pid=ALIVE, ttl_seconds=900, lease_id="owner")
    s.send(sender="lead", recipient="beta", kind="message", body="hi")
    before = s.cursor("beta")

    class _Lost(Exception):
        pass

    def pre_commit():
        if s.renew_lead_loop_lease("beta", lease_id="owner") is None:
            raise _Lost()

    def drive(_record):
        s.release_lead_loop_lease("beta")  # lease stolen/released DURING the turn
        return True                        # ...but the turn "succeeds"

    with pytest.raises(_Lost):
        wloop.run_loop(s, "beta", drive, max_polls=5, pre_commit=pre_commit,
                       manage_waiting=False, clock=lambda: 0.0, sleep=lambda _x: None)
    assert s.cursor("beta") == before  # commit BLOCKED -> cursor not advanced


def test_run_loop_pre_commit_guards_non_drive_control_commit(tmp_path, monkeypatch):
    # codex: the gap also exists for consume paths that never call drive. An invalid
    # control record is committed without driving - that commit is guarded too, so a
    # lost lease blocks the advance there as well.
    import pytest
    from agenttalk.wrapper import loop as wloop
    s = _store(tmp_path)
    s.set_managed_lead_loop("beta")
    monkeypatch.setattr(store_mod, "_process_liveness", lambda pid: PROC_ALIVE)
    s.acquire_lead_loop_lease("beta", owner_pid=ALIVE, ttl_seconds=900, lease_id="owner")
    # an UNAUTHORIZED release (no authority envelope) -> classify_loop_control = invalid
    s.send(sender="lead", recipient="beta", kind="release", body="no-marker")
    before = s.cursor("beta")
    s.release_lead_loop_lease("beta")  # lease lost before the loop reaches the control commit

    class _Lost(Exception):
        pass

    def pre_commit():
        if s.renew_lead_loop_lease("beta", lease_id="owner") is None:
            raise _Lost()

    def drive(_record):
        raise AssertionError("control path must not drive a turn")

    with pytest.raises(_Lost):
        wloop.run_loop(s, "beta", drive, max_polls=5, pre_commit=pre_commit,
                       manage_waiting=False, clock=lambda: 0.0, sleep=lambda _x: None)
    assert s.cursor("beta") == before  # non-drive control commit BLOCKED by the lost lease
