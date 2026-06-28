"""Managed lead-loop Slice 1: the renewable team-mailbox LEASE + lead_unarmed
visibility + the single-consumer verb-guard.

cli-AGNOSTIC by construction: the lease / guard / visibility key off the AGENT
NAME + its managed_lead_loop config, never a cli. A codex-flavored identity is
exercised alongside the generic ones to pin that.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agenttalk import cli, doctor
from agenttalk import supervisor as sup
from agenttalk.store import (
    LEAD_LOOP_TTL_DEFAULT,
    Store,
    validate_managed_lead_loop,
)

ALIVE = os.getpid()          # this test process is alive
DEAD = 2 ** 31 - 1           # a pid that is (practically) never alive


def _store(tmp_path: Path, agents=("lead", "beta")) -> Store:
    s = Store(tmp_path)
    s.init(list(agents))
    return s


def _run(argv: list[str], root: Path) -> int:
    return cli.main(["--root", str(root), *argv])


def _set_hb(s: Store, agent: str, epoch: float) -> None:
    """Write a heartbeat with a chosen wall-clock so staleness is deterministic."""
    iso = (datetime.fromtimestamp(epoch, timezone.utc)
           .isoformat(timespec="microseconds").replace("+00:00", "Z"))
    (s.state_dir / f"{agent}.heartbeat").write_text(iso, encoding="utf-8")


# ----------------------------------------------------------- lease lifecycle

def test_lease_acquire_renew_release(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.set_managed_lead_loop("beta")
    lease = s.acquire_lead_loop_lease("beta", owner_pid=ALIVE, now=1000.0)
    assert lease and lease["lease_id"] and lease["mode"] == "lead-loop"
    assert lease["expires_at"] == 1000.0 + LEAD_LOOP_TTL_DEFAULT
    assert s.read_lead_loop_lease("beta")["lease_id"] == lease["lease_id"]
    # renew by the owner extends; a non-owner renew is refused (None)
    renewed = s.renew_lead_loop_lease("beta", lease_id=lease["lease_id"], now=2000.0)
    assert renewed["expires_at"] == 2000.0 + LEAD_LOOP_TTL_DEFAULT
    assert s.renew_lead_loop_lease("beta", lease_id="not-the-owner", now=2100.0) is None
    # release by a non-owner is refused; by the owner clears the lease
    assert s.release_lead_loop_lease("beta", lease_id="not-the-owner") is False
    assert s.release_lead_loop_lease("beta", lease_id=lease["lease_id"]) is True
    assert s.read_lead_loop_lease("beta") is None


def test_lease_mirrors_into_waiting_observational_without_token(tmp_path: Path) -> None:
    # the lease is the correctness state; it MIRRORS into .waiting for status/UX -
    # but the mirror MUST NOT carry the lease_id (status returns .waiting verbatim,
    # so leaking it would expose the guard's owner-bypass token; reviewer-1 blocker).
    s = _store(tmp_path)
    s.set_managed_lead_loop("beta")
    lease = s.acquire_lead_loop_lease("beta", owner_pid=ALIVE, now=1000.0)
    w = s.read_waiting("beta")
    assert w and w.get("lead_loop") is True
    assert w.get("pid") == ALIVE and w.get("deadline_epoch") == lease["expires_at"]
    assert "lease_id" not in w  # the bypass token is NOT mirrored
    # releasing the lease clears the lead-loop mirror
    s.release_lead_loop_lease("beta", lease_id=lease["lease_id"])
    assert s.read_waiting("beta") is None


def test_status_does_not_leak_lease_id(tmp_path: Path) -> None:
    # reviewer-1 blocker: read-only status (allowed for non-owners) must NOT expose
    # the lease_id, else a non-owner could read it + set the env + bypass the guard.
    import json as _json
    s = _store(tmp_path)
    s.set_managed_lead_loop("beta")
    s.acquire_lead_loop_lease("beta", owner_pid=ALIVE, now=time.time())
    blob = _json.dumps(cli._gather_status(s))
    assert "lease_id" not in blob  # token never appears anywhere in status output


def test_concurrent_acquire_yields_exactly_one_owner(tmp_path: Path) -> None:
    # reviewer-1 blocker: acquire is ATOMIC under the per-agent lease lock - two
    # contenders can NEVER both acquire an empty lease. The loser is blocked (None).
    import threading
    s = _store(tmp_path)
    s.set_managed_lead_loop("beta")
    results: dict[str, object] = {}
    barrier = threading.Barrier(2)
    now = time.time()

    def worker(lid: str) -> None:
        barrier.wait()
        results[lid] = s.acquire_lead_loop_lease("beta", owner_pid=ALIVE, now=now,
                                                 lease_id=lid)

    threads = [threading.Thread(target=worker, args=(lid,)) for lid in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    winners = [lid for lid, r in results.items() if r is not None]
    assert len(winners) == 1                                          # exactly one acquired
    assert s.read_lead_loop_lease("beta")["lease_id"] == winners[0]   # disk == winner


def test_clear_releases_lease_and_unguards(tmp_path: Path, monkeypatch) -> None:
    # reviewer-1 major: clearing the managed config must release the lease so the
    # now-unmanaged identity is no longer guarded (guard also requires current
    # managed config - defense-in-depth).
    s = _store(tmp_path)
    s.set_managed_lead_loop("beta")
    s.acquire_lead_loop_lease("beta", owner_pid=ALIVE, now=time.time())
    monkeypatch.delenv("AGENTTALK_LEAD_LOOP_LEASE", raising=False)
    assert _run(["recv", "--for", "beta"], tmp_path) == 7      # guarded while managed
    s.set_managed_lead_loop("beta", enabled=False)             # unmanage -> release
    assert s.read_lead_loop_lease("beta") is None              # lease force-released
    assert _run(["recv", "--for", "beta"], tmp_path) != 7      # no longer guarded


def test_guard_requires_managed_config(tmp_path: Path, monkeypatch) -> None:
    # the guard only blocks a CONFIGURED managed identity; a stray lease on an
    # un-managed agent is never guarded (a leftover file can't wedge a mailbox).
    s = _store(tmp_path)  # beta NOT set managed
    s.acquire_lead_loop_lease("beta", owner_pid=ALIVE, now=time.time())
    monkeypatch.delenv("AGENTTALK_LEAD_LOOP_LEASE", raising=False)
    assert _run(["recv", "--for", "beta"], tmp_path) != 7


# ----------------------------------------------------------- steal semantics

def test_steal_healthy_long_turn_not_stolen(tmp_path: Path) -> None:
    # MANAGED + lease EXPIRED but owner ALIVE + heartbeat FRESH -> NOT stolen
    # (a long healthy turn renews; expiry alone is never enough).
    s = _store(tmp_path)
    s.set_managed_lead_loop("beta")
    s.acquire_lead_loop_lease("beta", owner_pid=ALIVE, ttl_seconds=10, now=1000.0,
                              lease_id="owner")
    _set_hb(s, "beta", 1000.0)  # fresh relative to now=1015
    blocked = s.acquire_lead_loop_lease("beta", owner_pid=ALIVE, now=1015.0,
                                        lease_id="thief")
    assert blocked is None
    assert s.read_lead_loop_lease("beta")["lease_id"] == "owner"


def test_steal_expired_and_stale_heartbeat(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.set_managed_lead_loop("beta")
    s.acquire_lead_loop_lease("beta", owner_pid=ALIVE, ttl_seconds=10, now=1000.0,
                              lease_id="owner")
    _set_hb(s, "beta", 1000.0)
    # now far past expiry AND heartbeat stale (age 1000 > 120) -> stealable
    stolen = s.acquire_lead_loop_lease("beta", owner_pid=ALIVE, now=2000.0,
                                       lease_id="thief")
    assert stolen and stolen["lease_id"] == "thief"


def test_steal_expired_and_dead_owner(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.set_managed_lead_loop("beta")
    s.acquire_lead_loop_lease("beta", owner_pid=DEAD, ttl_seconds=10, now=1000.0,
                              lease_id="owner")
    _set_hb(s, "beta", 1010.0)  # fresh, but the owner pid is dead
    stolen = s.acquire_lead_loop_lease("beta", owner_pid=ALIVE, now=1015.0,
                                       lease_id="thief")
    assert stolen and stolen["lease_id"] == "thief"


def test_manual_identity_never_auto_stolen(tmp_path: Path) -> None:
    # an agent NOT configured managed: a stray lease (expired + dead owner) is
    # NEVER auto-stolen (steal is gated on the CONFIGURED managed flag).
    s = _store(tmp_path)  # beta is NOT set managed
    s.acquire_lead_loop_lease("beta", owner_pid=DEAD, ttl_seconds=10, now=1000.0,
                              lease_id="owner")
    _set_hb(s, "beta", 1000.0)
    blocked = s.acquire_lead_loop_lease("beta", owner_pid=ALIVE, now=2000.0,
                                        lease_id="thief")
    assert blocked is None
    assert s.read_lead_loop_lease("beta")["lease_id"] == "owner"


# ----------------------------------------------------------- single-consumer guard

def test_guard_rejects_external_consumers(tmp_path: Path, monkeypatch) -> None:
    s = _store(tmp_path)
    s.set_managed_lead_loop("beta")
    lease = s.acquire_lead_loop_lease("beta", owner_pid=ALIVE, now=time.time())
    monkeypatch.delenv("AGENTTALK_LEAD_LOOP_LEASE", raising=False)
    # cursor-consuming verbs are rejected (exit 7) for a non-owner
    for verb in (["recv", "--for", "beta"], ["drain", "--for", "beta"],
                 ["ack", "--for", "beta"], ["wait", "--for", "beta", "--timeout", "0"]):
        assert _run(verb, tmp_path) == 7, verb
    # read-only verbs stay allowed
    assert _run(["sync", "--for", "beta"], tmp_path) == 0
    assert _run(["threads", "--for", "beta"], tmp_path) == 0
    assert _run(["status"], tmp_path) == 0
    # the lease OWNER (presents the live lease_id via env) is allowed through
    monkeypatch.setenv("AGENTTALK_LEAD_LOOP_LEASE", lease["lease_id"])
    assert _run(["recv", "--for", "beta"], tmp_path) == 0


def test_guard_precedes_refuse_stacked_wait(tmp_path: Path, monkeypatch) -> None:
    # the lead-loop guard (exit 7) fires BEFORE the weaker --refuse-stacked-wait
    # check (exit 6): a managed mailbox rejects any non-owner outright.
    s = _store(tmp_path)
    s.set_managed_lead_loop("beta")
    s.acquire_lead_loop_lease("beta", owner_pid=ALIVE, now=time.time())
    monkeypatch.delenv("AGENTTALK_LEAD_LOOP_LEASE", raising=False)
    assert _run(["wait", "--for", "beta", "--timeout", "0", "--refuse-stacked-wait"],
                tmp_path) == 7


def test_guard_allows_when_owner_dead(tmp_path: Path, monkeypatch) -> None:
    # a dead-owner lease is ORPHANED, not a live owner -> NOT guarded (recoverable).
    s = _store(tmp_path)
    s.set_managed_lead_loop("beta")
    s.acquire_lead_loop_lease("beta", owner_pid=DEAD, now=time.time())
    monkeypatch.delenv("AGENTTALK_LEAD_LOOP_LEASE", raising=False)
    assert _run(["recv", "--for", "beta"], tmp_path) != 7


# ----------------------------------------------------------- lead_unarmed visibility

def test_doctor_managed_unarmed_is_error(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.set_managed_lead_loop("beta")  # managed but NO lease -> NOT armed
    rep = doctor.run(tmp_path)
    lc = next((c for c in rep.checks if c.name == "lead_loop"), None)
    assert lc is not None and lc.status == "error" and "beta" in lc.details


def test_doctor_managed_armed_no_error(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.set_managed_lead_loop("beta")
    s.acquire_lead_loop_lease("beta", owner_pid=ALIVE, now=time.time())
    _set_hb(s, "beta", time.time())  # fresh -> armed
    rep = doctor.run(tmp_path)
    lc = next((c for c in rep.checks if c.name == "lead_loop"), None)
    assert lc is None or lc.status != "error"


def test_managed_armed_when_owner_alive_and_hb_fresh_even_if_expired(tmp_path: Path) -> None:
    # verify vis-P1: armed mirrors the GUARD's owner-alive test - a healthy long turn
    # whose lease momentarily lapsed (owner alive + heartbeat fresh) is STILL armed,
    # NOT a false ERROR. Expiry drives STEAL only, never armed.
    s = _store(tmp_path)
    s.set_managed_lead_loop("beta")
    s.acquire_lead_loop_lease("beta", owner_pid=ALIVE, ttl_seconds=10, now=1000.0)  # exp 1010
    _set_hb(s, "beta", 5000.0)  # fresh relative to now=5005
    st = s.lead_loop_state("beta", now=5005.0)
    assert st["expired"] is True and st["owner_alive"] is True and st["armed"] is True
    assert s.lead_loop_active_owner("beta") is not None  # guard still protects it
    # and an expired-but-healthy lease is NOT stealable (owner alive + hb fresh)
    assert s.acquire_lead_loop_lease("beta", owner_pid=ALIVE, now=5005.0,
                                     lease_id="thief") is None


def test_doctor_legacy_liaison_fresh_hb_no_false_fire(tmp_path: Path) -> None:
    # a BUSY legacy liaison (fresh heartbeat) with open work must NOT warn -
    # agent_active short-circuits, so a busy free-form liaison is never flagged.
    s = _store(tmp_path)
    s.set_role("lead", "lead")
    s.set_operator_facing("lead")
    s.send(sender="beta", recipient="lead", kind="question", body="?",
           meta={"request_id": "q-1"})
    _set_hb(s, "lead", time.time())  # fresh -> live
    rep = doctor.run(tmp_path)
    lc = next((c for c in rep.checks if c.name == "lead_loop"), None)
    assert lc is None or "lead" not in lc.details


def test_doctor_legacy_liaison_idle_with_work_warns_non_gating(tmp_path: Path) -> None:
    # an IDLE legacy liaison (stale heartbeat, no waiter) WITH open team work gets a
    # WARN - never an ERROR (non-gating; the legacy path is best-effort).
    s = _store(tmp_path)
    s.set_role("lead", "lead")
    s.set_operator_facing("lead")
    s.send(sender="beta", recipient="lead", kind="question", body="?",
           meta={"request_id": "q-1"})
    _set_hb(s, "lead", time.time() - 10_000)  # stale, no waiter
    rep = doctor.run(tmp_path)
    lc = next((c for c in rep.checks if c.name == "lead_loop"), None)
    assert lc is not None and lc.status == "warn" and "lead" in lc.details


def test_status_surfaces_lead_loop_additively(tmp_path: Path, capsys) -> None:
    s = _store(tmp_path)
    s.set_managed_lead_loop("beta")
    s.acquire_lead_loop_lease("beta", owner_pid=ALIVE, now=time.time())
    payload = cli._gather_status(s)
    beta = next(r for r in payload["agents"] if r["name"] == "beta")
    lead = next(r for r in payload["agents"] if r["name"] == "lead")
    assert "lead_loop" in beta and beta["lead_loop"]["present"] is True
    assert "lead_loop" not in lead  # additive: absent for an un-managed agent


def test_supervisor_report_surfaces_lead_loop(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.set_managed_lead_loop("beta")
    s.acquire_lead_loop_lease("beta", owner_pid=ALIVE, now=time.time())
    _set_hb(s, "beta", time.time())
    rep = sup.build_report(s, now_epoch=time.time())
    assert rep["agents"]["beta"]["lead_loop"]["armed"] is True
    assert "lead_loop" not in rep["agents"]["lead"]  # additive


# ----------------------------------------------------------- genericity (no cli)

def test_generic_codex_flavored_identity_is_managed_identically(tmp_path: Path,
                                                                monkeypatch) -> None:
    # cli-AGNOSTIC: a codex-flavored NAME behaves exactly like any other - the
    # lease / guard / visibility never read a cli.
    s = _store(tmp_path, agents=("lead", "codex-loop"))
    s.set_managed_lead_loop("codex-loop")
    lease = s.acquire_lead_loop_lease("codex-loop", owner_pid=ALIVE, now=time.time())
    monkeypatch.delenv("AGENTTALK_LEAD_LOOP_LEASE", raising=False)
    assert _run(["recv", "--for", "codex-loop"], tmp_path) == 7
    monkeypatch.setenv("AGENTTALK_LEAD_LOOP_LEASE", lease["lease_id"])
    assert _run(["recv", "--for", "codex-loop"], tmp_path) == 0
    # release -> no lease -> managed-unarmed ERROR, identical to any other identity
    s.release_lead_loop_lease("codex-loop", lease_id=lease["lease_id"])
    rep = doctor.run(tmp_path)
    lc = next((c for c in rep.checks if c.name == "lead_loop"), None)
    assert lc is not None and lc.status == "error" and "codex-loop" in lc.details


# ----------------------------------------------------------- reset + CLI config

def test_reset_clears_lease_preserves_dead_letter_sink(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.set_managed_lead_loop("beta")
    s.acquire_lead_loop_lease("beta", owner_pid=ALIVE, now=time.time())
    assert s.read_lead_loop_lease("beta") is not None
    sink = s.dead_letter_dir / "beta"
    sink.mkdir(parents=True, exist_ok=True)
    (sink / "20260101-000000-000000-aaaa.json").write_text("{}", encoding="utf-8")
    s.reset()
    assert s.read_lead_loop_lease("beta") is None                       # lease cleared
    assert (sink / "20260101-000000-000000-aaaa.json").exists()         # sink preserved


def test_cli_managed_lead_loop_set_clear_list(tmp_path: Path) -> None:
    _store(tmp_path)  # init only; the CLI path reads the store from --root
    assert _run(["managed-lead-loop", "set", "beta", "--ttl", "900", "--cadence", "300"],
                tmp_path) == 0
    assert Store(tmp_path).is_managed_lead_loop("beta") is True
    assert _run(["managed-lead-loop", "list"], tmp_path) == 0
    # ttl must exceed cadence (validation, fail-closed)
    assert _run(["managed-lead-loop", "set", "beta", "--ttl", "100", "--cadence", "300"],
                tmp_path) == 2
    assert _run(["managed-lead-loop", "clear", "beta"], tmp_path) == 0
    assert Store(tmp_path).is_managed_lead_loop("beta") is False


def test_slice1b_turn_end_audit_deferral_recorded(tmp_path: Path) -> None:
    # The post-turn audit (Slice 1b) is NOT buildable with current PostToolUse-only
    # hooks (no reliable post-final-answer hook can see a backgrounded armed wait).
    # The managed lead_unarmed detector is the SUBSTITUTE; the verdict is recorded
    # in docs/ISSUES.md. This test pins that the verdict stays documented.
    issues = Path(__file__).resolve().parents[1] / "docs" / "ISSUES.md"
    text = issues.read_text(encoding="utf-8").lower()
    assert "slice 1b" in text and "post-turn" in text and "defer" in text


# ----------------------------------------------------------- roster ops don't brick (lead P1)

def test_remove_managed_agent_does_not_brick_config(tmp_path: Path) -> None:
    # removing a managed agent must drop its managed_lead_loop key, NOT leave a
    # dangling key that makes validate_managed_lead_loop raise on every load_config.
    s = _store(tmp_path)
    s.set_managed_lead_loop("beta")
    s.remove_agent("beta")
    cfg = s.load_config()  # must NOT raise
    assert "beta" not in (cfg.get("managed_lead_loop") or {})
    assert s.is_managed_lead_loop("beta") is False


def test_retire_managed_agent_does_not_brick_config(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.set_managed_lead_loop("beta")
    s.retire_agent("beta", reason="done")
    cfg = s.load_config()  # must NOT raise
    assert "beta" not in (cfg.get("managed_lead_loop") or {})


def test_rename_managed_agent_carries_flag_and_loads(tmp_path: Path) -> None:
    # rename must CARRY the managed spec onto the new name (parity with role/group)
    # AND not leave a dangling old key.
    s = _store(tmp_path)
    s.set_managed_lead_loop("beta", ttl_seconds=800, cadence_seconds=200)
    s.rename_agent("beta", "beta2")
    cfg = s.load_config()  # must NOT raise
    assert "beta" not in (cfg.get("managed_lead_loop") or {})
    assert s.is_managed_lead_loop("beta2") is True
    spec = s.managed_lead_loop_spec("beta2")
    assert spec["ttl_seconds"] == 800 and spec["cadence_seconds"] == 200


def test_load_config_self_heals_dangling_managed_key(tmp_path: Path) -> None:
    # a config ALREADY bricked by a pre-fix roster remove (dangling managed key) must
    # recover IN-TOOL: load_config prunes the non-roster key in-memory (+ warns) so
    # the tool stays usable instead of exiting 2 on every command.
    s = _store(tmp_path)
    import json
    cfg = json.loads(s.config_path.read_text(encoding="utf-8"))
    cfg["managed_lead_loop"] = {"ghost": {"enabled": True}}  # ghost is NOT in the roster
    s.config_path.write_text(json.dumps(cfg), encoding="utf-8")
    with pytest.warns(UserWarning, match="self-heal"):
        healed = s.load_config()  # must NOT raise
    assert "ghost" not in (healed.get("managed_lead_loop") or {})


# ----------------------------------------------------------- NaN/inf bounds (lead P2)

def test_validate_rejects_nan_and_inf_bounds(tmp_path: Path) -> None:
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError, match="finite positive number"):
            validate_managed_lead_loop({"beta": {"ttl_seconds": bad}}, ["beta"])
        with pytest.raises(ValueError, match="finite positive number"):
            validate_managed_lead_loop({"beta": {"cadence_seconds": bad}}, ["beta"])


def test_cli_managed_lead_loop_rejects_nan_inf(tmp_path: Path) -> None:
    _store(tmp_path)
    assert _run(["managed-lead-loop", "set", "beta", "--ttl", "nan", "--cadence", "10"],
                tmp_path) == 2
    assert _run(["managed-lead-loop", "set", "beta", "--ttl", "inf", "--cadence", "10"],
                tmp_path) == 2
    assert Store(tmp_path).is_managed_lead_loop("beta") is False  # never persisted


# ----------------------------------------------- armed = NOT(expired AND hb-stale) (lead P2)

def test_armed_within_ttl_despite_stale_heartbeat(tmp_path: Path) -> None:
    # a long healthy turn: owner alive, lease WITHIN TTL, but the heartbeat lapsed
    # (>120s). The prior hb-only rule false-ERRORed here; now it is still ARMED.
    # Uses real-now-relative epochs so doctor.run() (which reads time.time()) sees
    # the SAME within-TTL / stale-heartbeat state as the unit check.
    s = _store(tmp_path)
    s.set_managed_lead_loop("beta")
    now = time.time()
    s.acquire_lead_loop_lease("beta", owner_pid=ALIVE, ttl_seconds=900, now=now)  # exp now+900
    _set_hb(s, "beta", now - 200.0)  # heartbeat age 200s (> 120 window), lease NOT expired
    st = s.lead_loop_state("beta")
    assert st["expired"] is False and st["heartbeat_stale"] is True and st["armed"] is True
    rep = doctor.run(tmp_path)
    lc = next((c for c in rep.checks if c.name == "lead_loop"), None)
    assert lc is None or lc.status != "error"  # no false ERROR for a healthy long turn


def test_unarmed_only_when_expired_and_heartbeat_stale(tmp_path: Path) -> None:
    # BOTH dimensions stale (expired AND heartbeat-stale) = a genuinely down
    # controller -> unarmed -> doctor ERROR. This is the exact complement of steal.
    s = _store(tmp_path)
    s.set_managed_lead_loop("beta")
    s.acquire_lead_loop_lease("beta", owner_pid=ALIVE, ttl_seconds=10, now=1000.0)  # exp 1010
    _set_hb(s, "beta", 1000.0)
    st = s.lead_loop_state("beta", now=2000.0)  # expired AND hb age 1000 > 120
    assert st["expired"] is True and st["heartbeat_stale"] is True and st["armed"] is False
    rep = doctor.run(tmp_path)
    lc = next((c for c in rep.checks if c.name == "lead_loop"), None)
    assert lc is not None and lc.status == "error" and "beta" in lc.details


# ----------------------------------------------- active_owner config-gate (lead convergence)

def test_active_owner_config_gated_for_manual_identity(tmp_path: Path) -> None:
    # a stray lease file for a NON-managed identity must never report a guarding
    # owner at the store layer (mirrors _lease_stealable's config gate).
    s = _store(tmp_path)  # beta is NOT set managed
    s.acquire_lead_loop_lease("beta", owner_pid=ALIVE, now=time.time())  # stray lease
    assert s.read_lead_loop_lease("beta") is not None
    assert s.lead_loop_active_owner("beta") is None  # config-gated -> not guarded
    # once configured managed, the same live lease DOES guard
    s.set_managed_lead_loop("beta")
    assert s.lead_loop_active_owner("beta") is not None


# --------------------------------------- dead owner within TTL is recoverable (reviewer-1 P1)

def test_dead_owner_within_ttl_is_stealable_immediately(tmp_path: Path) -> None:
    # reviewer-1 release-blocker: a DEAD owner inside TTL was unarmed (detector) AND
    # unguarded (active_owner) yet UN-stealable until TTL expiry -> a down-but-
    # unrecoverable limbo. A provably dead owner must be stealable IMMEDIATELY, and
    # the detector (unarmed) must be the EXACT complement of stealability.
    s = _store(tmp_path)
    s.set_managed_lead_loop("beta")
    now = time.time()
    s.acquire_lead_loop_lease("beta", owner_pid=DEAD, ttl_seconds=900, now=now,
                              lease_id="crashed")  # exp now+900 -> NOT expired
    _set_hb(s, "beta", now)  # heartbeat fresh; only the owner pid is dead
    # the limbo state (pre-recovery): unarmed + unguarded, but the complement now
    # holds because the SAME lease is immediately stealable (no TTL wait).
    st = s.lead_loop_state("beta", now=now)
    assert st["expired"] is False and st["owner_alive"] is False and st["armed"] is False
    assert s.lead_loop_active_owner("beta") is None  # dead owner is not a live guard
    assert s._lease_stealable(s.read_lead_loop_lease("beta"), "beta", now=now,
                              heartbeat_stale_after=None) is True
    rep = doctor.run(tmp_path)  # while the owner is still dead -> ERROR
    lc = next((c for c in rep.checks if c.name == "lead_loop"), None)
    assert lc is not None and lc.status == "error" and "beta" in lc.details
    # a restarted controller takes over IMMEDIATELY (the recovery the feature exists for)
    replacement = s.acquire_lead_loop_lease("beta", owner_pid=ALIVE, now=now,
                                            lease_id="replacement")
    assert replacement and replacement["lease_id"] == "replacement"
    assert s.lead_loop_active_owner("beta") is not None  # recovered -> guarded again
    rep2 = doctor.run(tmp_path)
    lc2 = next((c for c in rep2.checks if c.name == "lead_loop"), None)
    assert lc2 is None or lc2.status != "error"  # no longer unarmed


# ------------------- tri-state liveness: UNKNOWN never false-steals (codex P1 / lead D-12 A)

def test_process_liveness_tristate(tmp_path: Path) -> None:
    # the authority probe distinguishes CONFIRMED-dead from uncertain: the live test
    # pid is ALIVE; a never-running pid is DEAD; a non-positive/None pid is UNKNOWN
    # (NOT dead - only a definitive OS signal authorizes an immediate steal).
    from agenttalk.store import (
        PROC_ALIVE, PROC_DEAD, PROC_UNKNOWN, _process_liveness,
    )
    assert _process_liveness(ALIVE) == PROC_ALIVE
    assert _process_liveness(DEAD) == PROC_DEAD
    assert _process_liveness(0) == PROC_UNKNOWN
    assert _process_liveness(-1) == PROC_UNKNOWN
    assert _process_liveness(None) == PROC_UNKNOWN


def test_unknown_liveness_never_false_steals_live_owner(tmp_path: Path, monkeypatch) -> None:
    # codex release-blocker: _process_alive is fail-quiet (uncertain -> False), so the
    # immediate-steal must NOT fire on an uncertain probe. With the tri-state, an
    # UNKNOWN owner on a fresh within-TTL lease is probably-alive -> armed, guarded,
    # and NOT stealable. This reproduces codex's forced-false-dead probe and asserts
    # the live owner is never displaced.
    import agenttalk.store as store_mod
    s = _store(tmp_path)
    s.set_managed_lead_loop("beta")
    now = time.time()
    s.acquire_lead_loop_lease("beta", owner_pid=ALIVE, ttl_seconds=900, now=now,
                              lease_id="owner")
    _set_hb(s, "beta", now)
    monkeypatch.setattr(store_mod, "_process_liveness",
                        lambda pid: store_mod.PROC_UNKNOWN)
    st = s.lead_loop_state("beta", now=now)
    assert st["owner_liveness"] == "unknown" and st["owner_alive"] is False
    assert st["armed"] is True  # probably-alive -> armed (no false unarmed)
    assert s.lead_loop_active_owner("beta") is not None  # guarded (probably-alive)
    assert s._lease_stealable(s.read_lead_loop_lease("beta"), "beta", now=now,
                              heartbeat_stale_after=None) is False  # NOT stealable
    thief = s.acquire_lead_loop_lease("beta", owner_pid=ALIVE, now=now,
                                      lease_id="thief")
    assert thief is None  # the live owner is NEVER displaced by an uncertain probe
    rep = doctor.run(tmp_path)
    lc = next((c for c in rep.checks if c.name == "lead_loop"), None)
    assert lc is None or lc.status != "error"  # UNKNOWN within TTL is not a false error


def test_unknown_liveness_recovers_via_expiry_and_stale_heartbeat(tmp_path: Path,
                                                                  monkeypatch) -> None:
    # An UNKNOWN owner is never immediately stolen, but a genuinely stuck one STILL
    # recovers via the expired-AND-heartbeat-stale fallback (bounded, no limbo).
    import agenttalk.store as store_mod
    s = _store(tmp_path)
    s.set_managed_lead_loop("beta")
    s.acquire_lead_loop_lease("beta", owner_pid=ALIVE, ttl_seconds=10, now=1000.0,
                              lease_id="owner")  # exp 1010
    _set_hb(s, "beta", 1000.0)
    monkeypatch.setattr(store_mod, "_process_liveness",
                        lambda pid: store_mod.PROC_UNKNOWN)
    # at now=2000: expired AND heartbeat stale (age 1000 > 120) -> stealable
    assert s._lease_stealable(s.read_lead_loop_lease("beta"), "beta", now=2000.0,
                              heartbeat_stale_after=None) is True
    stolen = s.acquire_lead_loop_lease("beta", owner_pid=ALIVE, now=2000.0,
                                       lease_id="thief")
    assert stolen and stolen["lease_id"] == "thief"


def test_unknown_owner_guarded_against_external_consumer(tmp_path: Path,
                                                         monkeypatch) -> None:
    # an UNKNOWN-liveness owner must still GUARD its mailbox (probably-alive), so an
    # external consumer cannot race a possibly-live controller.
    import agenttalk.store as store_mod
    s = _store(tmp_path)
    s.set_managed_lead_loop("beta")
    s.acquire_lead_loop_lease("beta", owner_pid=ALIVE, now=time.time())
    monkeypatch.delenv("AGENTTALK_LEAD_LOOP_LEASE", raising=False)
    monkeypatch.setattr(store_mod, "_process_liveness",
                        lambda pid: store_mod.PROC_UNKNOWN)
    assert _run(["recv", "--for", "beta"], tmp_path) == 7  # guarded despite UNKNOWN
