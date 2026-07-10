"""WP1 - lead-loop opening hardening foundation.

The single ``_lead_loop_authority`` source of truth (steal/armed/guard can never
disagree), read-boundary ``expires_at`` normalization, the confirmed-dead-only
lease authority, and the non-store timing resolver. cli-AGNOSTIC by construction.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import agenttalk.store as store_mod
from agenttalk import cli
from agenttalk import doctor
from agenttalk import lead_loop_runtime as llr
from agenttalk import supervisor as sup
from agenttalk.store import (
    ACTIVE_WITHIN_SECONDS,
    PROC_ALIVE,
    PROC_DEAD,
    PROC_UNKNOWN,
    Store,
)

ALIVE = os.getpid()


def _store(tmp_path: Path, agents=("lead", "beta")) -> Store:
    s = Store(tmp_path)
    s.init(list(agents))
    return s


def _run(argv: list[str], root: Path) -> int:
    return cli.main(["--root", str(root), *argv])


def _set_hb(s: Store, agent: str, epoch: float) -> None:
    iso = (datetime.fromtimestamp(epoch, timezone.utc)
           .isoformat(timespec="microseconds").replace("+00:00", "Z"))
    (s.state_dir / f"{agent}.heartbeat").write_text(iso, encoding="utf-8")


# --------------------------------------------- single authority: property cross-product

def test_authority_property_cross_product(tmp_path: Path, monkeypatch) -> None:
    # The CORE WP1 invariant, exhaustively: for a present MANAGED lease,
    # armed == (not stealable) AND guarded == (liveness != DEAD), across the full
    # cross-product of liveness x expiry x heartbeat-staleness. An UNMANAGED stray
    # lease is inert. This pins the single source against the drift that bit twice.
    s = _store(tmp_path)
    box = {"liveness": PROC_ALIVE}
    monkeypatch.setattr(store_mod, "_process_liveness", lambda pid: box["liveness"])
    now = 10_000.0
    stale_after = 120.0
    expiries = {"future": now + 1000.0, "past": now - 1000.0, "none": None}
    for managed in (True, False):
        if managed:
            s.set_managed_lead_loop("beta")
        elif s.is_managed_lead_loop("beta"):
            s.set_managed_lead_loop("beta", enabled=False)
        for liveness in (PROC_ALIVE, PROC_DEAD, PROC_UNKNOWN):
            box["liveness"] = liveness
            for ekind, exp in expiries.items():
                for hb_fresh in (True, False):
                    _set_hb(s, "beta", now if hb_fresh else now - 10_000.0)
                    lease = {"managed": True, "owner_pid": 4321, "expires_at": exp}
                    auth = s._lead_loop_authority(
                        "beta", lease, now=now, heartbeat_stale_after=stale_after)
                    ctx = (managed, liveness, ekind, hb_fresh)
                    if not managed:
                        assert auth["managed"] is False, ctx
                        assert auth["armed"] is False and auth["stealable"] is False, ctx
                        assert auth["guarded"] is False, ctx
                        assert auth["reason"] == "not managed", ctx
                        continue
                    # exact-complement invariant + guard rule
                    assert auth["armed"] == (not auth["stealable"]), ctx
                    assert auth["guarded"] == (liveness != PROC_DEAD), ctx
                    expired = (exp is not None) and (now > exp)
                    hb_stale = not hb_fresh
                    expected_stealable = (liveness == PROC_DEAD) or (expired and hb_stale)
                    assert auth["stealable"] is expected_stealable, ctx
                    assert auth["expired"] is expired, ctx
                    assert auth["heartbeat_stale"] is hb_stale, ctx


def test_three_consumers_agree_with_authority(tmp_path: Path, monkeypatch) -> None:
    # _lease_stealable, lead_loop_state, and lead_loop_active_owner must all match
    # the single authority - no per-caller liveness branch (dev-2's main correction).
    s = _store(tmp_path)
    s.set_managed_lead_loop("beta")
    now = time.time()
    s.acquire_lead_loop_lease("beta", owner_pid=ALIVE, ttl_seconds=900, now=now,
                              lease_id="owner")
    for liveness in (PROC_ALIVE, PROC_DEAD, PROC_UNKNOWN):
        monkeypatch.setattr(store_mod, "_process_liveness", lambda pid, _v=liveness: _v)
        _set_hb(s, "beta", now)
        lease = s.read_lead_loop_lease("beta")
        auth = s._lead_loop_authority("beta", lease, now=now,
                                      heartbeat_stale_after=ACTIVE_WITHIN_SECONDS)
        assert s._lease_stealable(lease, "beta", now=now,
                                  heartbeat_stale_after=None) is auth["stealable"]
        st = s.lead_loop_state("beta", now=now)
        assert st["armed"] is auth["armed"]
        assert (s.lead_loop_active_owner("beta", now=now) is not None) is auth["guarded"]


# --------------------------------------------- read-boundary expires_at normalization

def test_read_normalizes_bad_expires_at(tmp_path: Path, monkeypatch) -> None:
    # NaN / +-inf / non-numeric / null expires_at all normalize to None at the read
    # boundary; an alive owner + stale heartbeat + normalized-None expiry is NOT
    # expired -> NOT stealable -> armed (never a false ERROR / false steal).
    s = _store(tmp_path)
    s.set_managed_lead_loop("beta")
    monkeypatch.setattr(store_mod, "_process_liveness", lambda pid: PROC_ALIVE)
    path = s.lead_loop_lease_path("beta")
    for bad in (float("nan"), float("inf"), float("-inf"), "xyz", None, True):
        lease = {"schema_version": 1, "managed": True, "agent": "beta",
                 "owner_pid": 4321, "lease_id": "x", "expires_at": bad}
        path.write_text(json.dumps(lease), encoding="utf-8")
        got = s.read_lead_loop_lease("beta")
        assert got["expires_at"] is None, bad
        auth = s._lead_loop_authority("beta", got, now=1e9,
                                      heartbeat_stale_after=120.0)
        assert auth["expired"] is False and auth["stealable"] is False, bad
        assert auth["armed"] is True, bad


def test_finite_expires_at_preserved_as_float(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.set_managed_lead_loop("beta")
    s.acquire_lead_loop_lease("beta", owner_pid=ALIVE, ttl_seconds=900, now=1000.0)
    got = s.read_lead_loop_lease("beta")
    assert isinstance(got["expires_at"], float) and got["expires_at"] == 1900.0


# --------------------------------------------- triple-fault edge (documented limitation)

def test_triple_fault_unknown_corrupt_expiry_stale_hb_not_stealable(
        tmp_path: Path, monkeypatch) -> None:
    # Documented edge: UNKNOWN liveness + corrupt/None expiry + stale heartbeat ->
    # NOT stealable (normalized None = not-expired). Delayed recovery, NEVER a false
    # steal of a maybe-live owner. Pins the conservative behavior.
    s = _store(tmp_path)
    s.set_managed_lead_loop("beta")
    monkeypatch.setattr(store_mod, "_process_liveness", lambda pid: PROC_UNKNOWN)
    _set_hb(s, "beta", 0.0)  # ancient -> stale
    lease = {"managed": True, "owner_pid": 4321, "expires_at": None}
    auth = s._lead_loop_authority("beta", lease, now=1e9, heartbeat_stale_after=120.0)
    assert auth["owner_liveness"] == "unknown" and auth["expired"] is False
    assert auth["stealable"] is False and auth["armed"] is True


# --------------------------------------------- config-gated armed (unmanaged inert)

def test_config_gated_armed_unmanaged_stray_lease(tmp_path: Path) -> None:
    s = _store(tmp_path)  # beta NOT managed
    s.acquire_lead_loop_lease("beta", owner_pid=ALIVE, now=time.time())  # stray lease
    st = s.lead_loop_state("beta")
    assert st["present"] is True and st["managed"] is False
    assert st["armed"] is False and st["reason"] == "not managed"
    assert s._lease_stealable(s.read_lead_loop_lease("beta"), "beta",
                              now=time.time(), heartbeat_stale_after=None) is False
    assert s.lead_loop_active_owner("beta") is None


# --------------------------------------------- persistent OS lock marker

def test_lead_loop_lock_recovers_stale_owner_metadata(tmp_path: Path) -> None:
    # The persistent marker is advisory only. A crashed process leaves stale JSON,
    # but its OS lock is released automatically and the next holder can acquire.
    s = _store(tmp_path)
    lock = s.state_dir / "beta.lead-loop-lease.lock"
    lock.write_text(json.dumps({"pid": 2 ** 31 - 1}), encoding="utf-8")

    with s._lead_loop_lease_lock("beta"):
        pass

    marker = json.loads(lock.read_text(encoding="utf-8"))
    assert marker["pid"] == os.getpid()
    assert isinstance(marker["generation"], str)


# --------------------------------------------- timing resolver (non-store module)

def test_resolve_timing_default(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.set_managed_lead_loop("beta", ttl_seconds=800, cadence_seconds=200)
    t = llr.resolve_timing(s, "beta")
    assert t["ttl_seconds"] == 800.0 and t["cadence_seconds"] == 200.0
    assert t["heartbeat_stale_after"] == ACTIVE_WITHIN_SECONDS  # store default


def test_resolve_timing_supervised_matches_stuck_after(tmp_path: Path) -> None:
    # CONTRACT: with supervisor config, heartbeat_stale_after == the supervisor's
    # resolved stuck_after, so a duplicate controller never steals EARLIER than the
    # supervisor would declare the owner stuck.
    s = _store(tmp_path)
    s.set_managed_lead_loop("beta")
    sup_cfg = {"agents": {"beta": {"wrapped": True, "cli": "codex"}}}
    expected = sup.resolve_stuck_after(sup_cfg, sup_cfg["agents"]["beta"])
    t = llr.resolve_timing(s, "beta", supervisor_config=sup_cfg)
    assert t["heartbeat_stale_after"] == expected
    # a wrapped codex threshold is materially higher than the bare store default
    assert t["heartbeat_stale_after"] > ACTIVE_WITHIN_SECONDS


def test_visibility_paths_use_resolved_window_not_default(tmp_path: Path, monkeypatch) -> None:
    # codex P1 regression: build_report / status / doctor must use the resolver's
    # window for a WRAPPED agent, not the 120s store default - else they false-unarm a
    # within-window controller. Setup: wrapped codex (window 2400), an EXPIRED lease, a
    # heartbeat ~300s old -> stale@120 (would be unarmed) but fresh@2400 (armed).
    s = _store(tmp_path)
    s.set_managed_lead_loop("beta")
    monkeypatch.setattr(store_mod, "_process_liveness", lambda pid: PROC_ALIVE)
    now = time.time()
    s.acquire_lead_loop_lease("beta", owner_pid=ALIVE, ttl_seconds=10, now=now - 5000,
                              lease_id="owner")  # long expired
    _set_hb(s, "beta", now - 300.0)
    sup_cfg = {"agents": {"beta": {"wrapped": True, "cli": "codex"}}}
    window = sup.resolve_stuck_after(sup_cfg, sup_cfg["agents"]["beta"])
    assert window > 300.0  # premise: the wrapped window exceeds the heartbeat age
    (s.dir / "supervisor.json").write_text(json.dumps(sup_cfg), encoding="utf-8")
    # control: with the bare 120s default the same snapshot is UNARMED (skew source)
    assert s.lead_loop_state("beta", now=now)["armed"] is False
    # 1) build_report (config in hand): nested lead_loop armed AND matches top-level
    rep = sup.build_report(s, now_epoch=now, supervisor_config=sup_cfg)
    assert rep["agents"]["beta"]["lead_loop"]["armed"] is True
    assert rep["agents"]["beta"]["heartbeat_stale"] is False
    # 2) status reads supervisor.json from store.dir -> armed
    beta = next(r for r in cli._gather_status(s)["agents"] if r["name"] == "beta")
    assert beta["lead_loop"]["armed"] is True
    # 3) doctor reads supervisor.json -> no lead_loop error
    lc = next((c for c in doctor.run(tmp_path).checks if c.name == "lead_loop"), None)
    assert lc is None or lc.status != "error"


def test_resolve_timing_tolerates_corrupt_per_agent_entry(tmp_path: Path) -> None:
    # P2 (lead verify): a TRUTHY non-dict per-agent supervisor.json entry (an operator
    # typo, e.g. {"agents": {"beta": "wrapped"}}) must NOT crash resolve_timing /
    # status / doctor - `... or {}` only rescued falsy values, letting a string slip
    # into resolve_stuck_after's cfg_agent.get(...) -> AttributeError. Now it falls
    # back to the store default instead of raising.
    s = _store(tmp_path)
    s.set_managed_lead_loop("beta")
    for bad in ("wrapped", ["wrapped"], 5, True):
        cfg = {"agents": {"beta": bad}}
        t = llr.resolve_timing(s, "beta", supervisor_config=cfg)  # must NOT raise
        assert t["heartbeat_stale_after"] == ACTIVE_WITHIN_SECONDS, bad


def test_resolve_stuck_after_tolerates_non_dict_inputs() -> None:
    # Belt-and-suspenders: resolve_stuck_after must not crash on a non-dict config or
    # cfg_agent (also fixes the pre-existing supervise --report crash on a malformed
    # per-agent entry); a bool stuck_after_seconds is ignored (bool-is-int guard).
    default = sup.resolve_stuck_after({}, {})
    assert sup.resolve_stuck_after("garbage", "wrapped") == default  # both non-dict
    assert sup.resolve_stuck_after({"agents": {}}, ["x"]) == default  # cfg_agent a list
    assert sup.resolve_stuck_after({}, {"stuck_after_seconds": True}) == default  # bool


def test_resolve_dead_letter_caps_tolerates_non_dict_inputs() -> None:
    # codex catch (same corrupt-config class): a truthy non-dict per-agent entry must
    # NOT crash dead-letter cap resolution - this is what `wrap --loop` startup calls
    # via cmd_wrap, so an AttributeError here took down the wrapped controller before
    # the loop even started. Coercion at config / cfg_agent / nested dead_letter blocks.
    default = sup.resolve_dead_letter_caps({}, {})
    assert sup.resolve_dead_letter_caps({"agents": {"beta": "wrapped"}}, "wrapped") == default
    assert sup.resolve_dead_letter_caps("garbage", ["x"]) == default
    # a non-dict nested dead_letter block is ignored, not crashed-on
    assert sup.resolve_dead_letter_caps({"dead_letter": "oops"},
                                        {"dead_letter": ["oops"]}) == default


def test_session_args_tolerates_non_dict_cfg_agent() -> None:
    # Complete the corrupt-config class: session_args (launch arg resolution) must not
    # crash on a truthy non-dict per-agent entry - it falls back to the default tokens.
    good = sup.session_args("codex", "fresh", None, cfg_agent={})
    assert sup.session_args("codex", "fresh", None, cfg_agent="wrapped") == good
    assert sup.session_args("claude", "resume", "sid", cfg_agent=["x"]) == \
        sup.session_args("claude", "resume", "sid", cfg_agent={})


def test_resolve_timing_contract_steal_and_view_use_same_window(
        tmp_path: Path, monkeypatch) -> None:
    # The resolver is the single source so steal AND view agree. Drive both paths
    # with the SAME resolved heartbeat_stale_after and assert armed == not stealable.
    s = _store(tmp_path)
    s.set_managed_lead_loop("beta")
    sup_cfg = {"agents": {"beta": {"wrapped": True, "cli": "codex"}}}
    hsa = llr.resolve_timing(s, "beta", supervisor_config=sup_cfg)["heartbeat_stale_after"]
    now = 1_000_000.0
    s.acquire_lead_loop_lease("beta", owner_pid=ALIVE, ttl_seconds=10, now=now - 5000,
                              lease_id="owner")  # long expired
    _set_hb(s, "beta", now - (hsa / 2.0))  # heartbeat fresh relative to the LARGE window
    lease = s.read_lead_loop_lease("beta")
    stealable = s._lease_stealable(lease, "beta", now=now, heartbeat_stale_after=hsa)
    st = s.lead_loop_state("beta", now=now, heartbeat_stale_after=hsa)
    assert st["armed"] is (not stealable)
    # within the large supervised window the heartbeat is fresh -> not stealable/armed
    assert stealable is False and st["armed"] is True


# --------------------------------------------- cursor invariance under a live lease

def test_cursor_invariance_under_live_managed_lease(tmp_path: Path, monkeypatch) -> None:
    s = _store(tmp_path)
    s.set_managed_lead_loop("beta")
    s.acquire_lead_loop_lease("beta", owner_pid=ALIVE, now=time.time())  # live owner
    s.send(sender="lead", recipient="beta", kind="question", body="hi",
           meta={"request_id": "q-1"})
    monkeypatch.delenv("AGENTTALK_LEAD_LOOP_LEASE", raising=False)
    before = s.cursor("beta")
    # all four read-only verbs named in the gate (incl `check`) must not consume
    for verb in (["sync", "--for", "beta"], ["threads", "--for", "beta"], ["status"],
                 ["check", "--for", "beta", "--to-request", "q-1"]):
        _run(verb, tmp_path)  # exit code varies for check; only cursor-invariance matters
    assert s.cursor("beta") == before  # read-only verbs never consume
    assert _run(["recv", "--for", "beta"], tmp_path) == 7  # guarded
    assert s.cursor("beta") == before  # a guarded consume does not advance the cursor
