"""Tests for the assurance P2 milestone/release CLOSE protocol.

Two layers, mirroring gates/build_report/plan_actions:

* PURE verdict tests - `compute_verdict` over synthetic (record, gate_check)
  inputs, one per STABLE hold code plus the GO case. No I/O.
* CLI integration - `main(argv)` against a real store, proving gate state and
  waivers drive HOLD/GO, that a refused GO does not publish, and that a GO
  publish with --bump-barrier fires the release barrier (and a HOLD never does).

Integration tests use a full 40-char SHA as --revision so revision freeze is
hermetic: pytest's tmp_path is not a git repo, so `_resolve_revision` falls
through to the pinned-SHA branch and `_worktree_clean` reports None -> clean.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading

import pytest

from agenttalk import cli, close
from agenttalk.store import Store

SHA = "a" * 40
OTHER_SHA = "b" * 40


# --------------------------------------------------------------- pure helpers

def _gate_go(gates: list[dict] | None = None) -> dict:
    return {"verdict": "GO", "required_gates": [], "blockers": [],
            "gates": gates or []}


def _gate_hold() -> dict:
    item = {"name": "ci", "status": "red", "severity": "blocker", "blocks": True}
    return {"verdict": "HOLD", "required_gates": ["ci"], "blockers": [item],
            "gates": [item]}


def _satisfied() -> dict:
    rec = close.empty_close(
        "c1", scope="release", revision=SHA, revision_kind="sha",
        gate_scope="release", opened_by="lead", opened_at="t0", epoch_at_open=None,
        required_lenses=[close.validate_lens_spec(
            {"id": "sec", "allowed_agents": ["codex"]})],
        revision_clean=True, dirty_artifact=None,
        non_lane_isolation_not_asserted=True)
    close.apply_ack(rec, lens_id="sec", status="accept", agent="codex",
                    from_role=None, at="t1", evidence={"risk_class": "none"})
    return rec


def _codes(result: dict) -> set[str]:
    return {h["code"] for h in result["holds"]}


# --------------------------------------------------------------- pure: GO

def test_satisfied_close_is_go() -> None:
    result = close.compute_verdict(_satisfied(), _gate_go())
    assert result["verdict"] == close.VERDICT_GO
    assert result["ok"] is True
    assert result["holds"] == []


# --------------------------------------------------- pure: one per HOLD code

def test_hold_malformed_state() -> None:
    result = close.compute_verdict({"not": "a close"}, _gate_go())
    assert result["verdict"] == close.VERDICT_HOLD
    assert _codes(result) == {close.HOLD_MALFORMED}  # short-circuits everything else


def test_hold_publish_not_allowed_when_published() -> None:
    rec = _satisfied()
    rec["status"] = close.PUBLISHED
    assert close.HOLD_PUBLISH_NOT_ALLOWED in _codes(
        close.compute_verdict(rec, _gate_go()))


def test_hold_revision_dirty_without_artifact() -> None:
    rec = _satisfied()
    rec["revision_clean"] = False
    rec["dirty_artifact"] = None
    assert close.HOLD_REVISION in _codes(close.compute_verdict(rec, _gate_go()))


def test_dirty_with_artifact_clears_revision_hold() -> None:
    rec = _satisfied()
    rec["revision_clean"] = False
    rec["dirty_artifact"] = "pointer:diff-1"
    assert close.compute_verdict(rec, _gate_go())["verdict"] == close.VERDICT_GO


def test_hold_revision_unresolved_when_not_full_sha() -> None:
    rec = _satisfied()
    rec["revision"] = "deadbeef"  # short / unresolved
    assert close.HOLD_REVISION in _codes(close.compute_verdict(rec, _gate_go()))


def test_hold_gate_when_gate_check_holds() -> None:
    assert close.HOLD_GATE in _codes(close.compute_verdict(_satisfied(), _gate_hold()))


def test_hold_missing_lens() -> None:
    rec = _satisfied()
    rec["lens_acks"] = {}
    assert close.HOLD_MISSING_LENS in _codes(close.compute_verdict(rec, _gate_go()))


def test_hold_unauthorized_lens_ack() -> None:
    rec = _satisfied()
    rec["lens_acks"]["sec"]["from"] = "mallory"  # not in allowed_agents
    assert close.HOLD_UNAUTHORIZED_ACK in _codes(close.compute_verdict(rec, _gate_go()))


def test_override_authorizes_otherwise_unauthorized_ack() -> None:
    rec = _satisfied()
    rec["lens_acks"]["sec"]["from"] = "mallory"
    rec["lens_acks"]["sec"]["override"] = True  # recorded lead/operator override
    assert close.compute_verdict(rec, _gate_go())["verdict"] == close.VERDICT_GO


def test_role_authorizes_lens_ack() -> None:
    rec = close.empty_close(
        "c2", scope="release", revision=SHA, revision_kind="sha",
        gate_scope="release", opened_by="lead", opened_at="t", epoch_at_open=None,
        required_lenses=[close.validate_lens_spec(
            {"id": "sec", "allowed_roles": ["reviewer"]})],
        revision_clean=True, dirty_artifact=None,
        non_lane_isolation_not_asserted=True)
    close.apply_ack(rec, lens_id="sec", status="accept", agent="anyone",
                    from_role="reviewer", at="t")
    assert close.compute_verdict(rec, _gate_go())["verdict"] == close.VERDICT_GO


def test_hold_stale_lens_ack_on_revision_change() -> None:
    rec = _satisfied()
    rec["lens_acks"]["sec"]["revision"] = OTHER_SHA  # ack reviewed a different SHA
    assert close.HOLD_STALE_ACK in _codes(close.compute_verdict(rec, _gate_go()))


def test_hold_undecided_counter() -> None:
    rec = _satisfied()
    close.apply_ack(rec, lens_id="sec", status="counter", agent="codex",
                    from_role=None, at="t2", counter_id="ctr-1",
                    evidence={"finding": "leak"})
    assert close.HOLD_UNDECIDED_COUNTER in _codes(close.compute_verdict(rec, _gate_go()))


def test_hold_accepted_counter_missing_remediation() -> None:
    rec = _satisfied()
    rec["counters"]["ctr-1"] = {
        "counter_id": "ctr-1", "lens": "sec", "decision": close.COUNTER_ACCEPTED,
        "remediation_id": None}
    assert close.HOLD_COUNTER_NO_REMEDIATION in _codes(
        close.compute_verdict(rec, _gate_go()))


def _blocker_rem_record() -> dict:
    rec = _satisfied()
    rec["counters"]["ctr-1"] = {
        "counter_id": "ctr-1", "lens": "sec", "decision": close.COUNTER_ACCEPTED,
        "remediation_id": "rem-1"}
    rec["remediation_items"]["rem-1"] = {
        "id": "rem-1", "blocker": True, "gate": "fix-gate"}
    return rec


def test_hold_open_blocker_remediation_until_gate_green() -> None:
    rec = _blocker_rem_record()
    # gate not present -> HOLD
    assert close.HOLD_OPEN_BLOCKER in _codes(close.compute_verdict(rec, _gate_go()))
    # name the gate green AND non-blocking -> GO
    green = _gate_go([{"name": "fix-gate", "status": "green", "blocks": False}])
    assert close.compute_verdict(rec, green)["verdict"] == close.VERDICT_GO
    # a waived (active) gate also resolves the blocker
    waived = _gate_go([{"name": "fix-gate", "status": "waived", "blocks": False}])
    assert close.compute_verdict(rec, waived)["verdict"] == close.VERDICT_GO


@pytest.mark.parametrize("gate", [
    {"name": "fix-gate", "status": "red", "severity": "warn", "blocks": False},
    {"name": "fix-gate", "status": "skipped", "severity": "blocker", "blocks": False},
    {"name": "fix-gate", "status": "unknown", "severity": "info", "blocks": False},
    {"name": "fix-gate", "blocks": False},  # status absent
])
def test_blocker_remediation_not_resolved_by_merely_nonblocking_gate(gate: dict) -> None:
    # codex finding: a non-blocking but NOT green/waived gate must NOT resolve a
    # blocker remediation (else a red/warn or skipped gate would yield a false GO).
    rec = _blocker_rem_record()
    result = close.compute_verdict(rec, _gate_go([gate]))
    assert result["verdict"] == close.VERDICT_HOLD
    assert close.HOLD_OPEN_BLOCKER in _codes(result)


def test_nonblocker_accepted_counter_with_remediation_is_go() -> None:
    rec = _satisfied()
    rec["counters"]["ctr-1"] = {
        "counter_id": "ctr-1", "lens": "sec", "decision": close.COUNTER_ACCEPTED,
        "remediation_id": "rem-1"}
    rec["remediation_items"]["rem-1"] = {"id": "rem-1", "blocker": False, "gate": None}
    assert close.compute_verdict(rec, _gate_go())["verdict"] == close.VERDICT_GO


# --------------------------------------------------- pure: transitions guard

def test_apply_ack_na_requires_reason() -> None:
    rec = _satisfied()
    with pytest.raises(close.CloseError):
        close.apply_ack(rec, lens_id="sec", status="na", agent="codex",
                        from_role=None, at="t")


def test_apply_ack_refused_after_publish() -> None:
    rec = _satisfied()
    rec["status"] = close.PUBLISHED
    with pytest.raises(close.CloseError):
        close.apply_ack(rec, lens_id="sec", status="accept", agent="codex",
                        from_role=None, at="t")


def test_apply_ack_rejects_duplicate_counter_id_without_mutating_ack() -> None:
    rec = _satisfied()
    close.apply_ack(rec, lens_id="sec", status="counter", agent="codex",
                    from_role=None, at="t1", counter_id="ctr-1")
    first_ack = dict(rec["lens_acks"]["sec"])

    with pytest.raises(close.CloseError, match="duplicate counter"):
        close.apply_ack(rec, lens_id="sec", status="counter", agent="codex",
                        from_role=None, at="t2", counter_id="ctr-1")

    assert rec["lens_acks"]["sec"] == first_ack


def test_decide_counter_accept_requires_remediation() -> None:
    rec = _satisfied()
    rec["counters"]["ctr-1"] = {"counter_id": "ctr-1", "decision": close.COUNTER_PENDING}
    with pytest.raises(close.CloseError):
        close.decide_counter(rec, counter_id="ctr-1", decision="accept", by="lead",
                             at="t", reason="ok", remediation=None)


def test_decide_counter_blocker_remediation_requires_gate() -> None:
    rec = _satisfied()
    rec["counters"]["ctr-1"] = {"counter_id": "ctr-1", "decision": close.COUNTER_PENDING}
    with pytest.raises(close.CloseError):
        close.decide_counter(
            rec, counter_id="ctr-1", decision="accept", by="lead", at="t",
            reason="fix it", remediation={
                "id": "rem-1", "owner": "dev", "fix": "patch",
                "verification": "tests", "blocker": True})  # no gate


def test_reopen_clears_final_and_revision_change_stales_acks() -> None:
    rec = _satisfied()
    close.record_publish(rec, verdict=close.VERDICT_GO, by="lead", at="t",
                         reason="ship", gate_check=_gate_go(), residual_risk=None,
                         barrier_epoch="ep-1")
    assert rec["status"] == close.PUBLISHED
    close.reopen(rec, by="op", at="t", revision=OTHER_SHA, revision_clean=True)
    assert rec["status"] == close.REOPENED
    assert rec["final"] is None
    # the prior ack reviewed SHA, the record now pins OTHER_SHA -> stale
    assert close.HOLD_STALE_ACK in _codes(close.compute_verdict(rec, _gate_go()))


def test_reopen_revision_change_clears_prior_dirty_artifact() -> None:
    rec = _satisfied()
    rec["dirty_artifact"] = "artifacts/old-revision.diff"

    close.reopen(rec, by="op", at="t", revision=OTHER_SHA, revision_clean=False)

    assert rec["dirty_artifact"] is None


# --------------------------------------------------------------- integration

def _init(tmp_path: Path) -> Path:
    s = Store(tmp_path)
    s.init(["lead", "codex", "dev2"])
    s.set_role("lead", "lead")
    return tmp_path


def _run(argv: list[str], root: Path) -> int:
    return cli.main(["--root", str(root), *argv])


def _open(root: Path, *extra: str) -> int:
    return _run(["close", "open", "--id", "rel", "--from", "lead",
                 "--scope", "release", "--revision", SHA,
                 "--lens", "sec", "--allow", "sec:codex",
                 "--non-lane-isolation-not-asserted", *extra], root)


def _accept(root: Path) -> int:
    return _run(["close", "ack", "--id", "rel", "--lens", "sec", "--status",
                 "accept", "--from", "codex", "--risk-class", "none",
                 "--release-blocker", "no", "--tests-referenced", "n/a",
                 "--tests-executed", "n/a", "--residual-risk", "n/a",
                 "--na-reason", "lw", "--evidence", "pointer:rq-1"], root)


def test_concurrent_close_saves_reject_one_stale_generation(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.init(["lead"])
    rec = close.empty_close(
        "rel", scope="release", revision=SHA, revision_kind="sha",
        gate_scope="release", opened_by="lead", opened_at="t0",
        epoch_at_open=None, required_lenses=[], revision_clean=True,
        dirty_artifact=None, non_lane_isolation_not_asserted=True)
    close.create_close(store, rec)
    generation = rec["generation"]
    instance_id = rec["instance_id"]
    ready = threading.Barrier(3)

    def update(body: str) -> str:
        local = close.load_close(store, "rel")
        close.set_draft(local, body=body, by="lead", at=body)
        ready.wait()
        try:
            close.save_close(
                store, local, expected_generation=generation,
                expected_instance_id=instance_id,
            )
        except close.CloseConflict:
            return "conflict"
        return "saved"

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(update, body) for body in ("first", "second")]
        ready.wait()
        outcomes = sorted(f.result() for f in futures)

    assert outcomes == ["conflict", "saved"]
    stored = close.load_close(store, "rel")
    assert stored["generation"] == generation + 1
    assert stored["draft"]["body"] in {"first", "second"}


def test_existing_close_rejects_save_without_expected_generation(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.init(["lead"])
    rec = close.empty_close(
        "rel", scope="release", revision=SHA, revision_kind="sha",
        gate_scope="release", opened_by="lead", opened_at="t0",
        epoch_at_open=None, required_lenses=[], revision_clean=True,
        dirty_artifact=None, non_lane_isolation_not_asserted=True)
    close.create_close(store, rec)
    loaded = close.load_close(store, "rel")
    close.set_draft(loaded, body="unchecked", by="lead", at="t1")

    with pytest.raises(close.CloseConflict, match="expected_generation"):
        close.save_close(store, loaded)

    assert close.load_close(store, "rel")["draft"] is None


def test_existing_close_rejects_save_without_expected_instance(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.init(["lead"])
    rec = close.empty_close(
        "rel", scope="release", revision=SHA, revision_kind="sha",
        gate_scope="release", opened_by="lead", opened_at="t0",
        epoch_at_open=None, required_lenses=[], revision_clean=True,
        dirty_artifact=None, non_lane_isolation_not_asserted=True)
    close.create_close(store, rec)
    loaded = close.load_close(store, "rel")

    with pytest.raises(close.CloseConflict, match="expected_instance_id"):
        close.save_close(store, loaded, expected_generation=loaded["generation"])


def test_delete_recreate_rejects_stale_instance_at_same_generation(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.init(["lead"])
    original = close.empty_close(
        "rel", scope="release", revision=SHA, revision_kind="sha",
        gate_scope="release", opened_by="lead", opened_at="t0",
        epoch_at_open=None, required_lenses=[], revision_clean=True,
        dirty_artifact=None, non_lane_isolation_not_asserted=True)
    close.create_close(store, original)
    stale = close.load_close(store, "rel")
    stale_generation = stale["generation"]
    stale_instance = stale["instance_id"]
    close.close_path(store, "rel").unlink()
    replacement = close.empty_close(
        "rel", scope="release", revision=OTHER_SHA, revision_kind="sha",
        gate_scope="release", opened_by="lead", opened_at="t1",
        epoch_at_open=None, required_lenses=[], revision_clean=True,
        dirty_artifact=None, non_lane_isolation_not_asserted=True)
    close.create_close(store, replacement)
    assert replacement["generation"] == stale_generation
    assert replacement["instance_id"] != stale_instance
    close.set_draft(stale, body="stale overwrite", by="lead", at="t2")

    with pytest.raises(close.CloseConflict, match="instance"):
        close.save_close(
            store, stale, expected_generation=stale_generation,
            expected_instance_id=stale_instance,
        )

    stored = close.load_close(store, "rel")
    assert stored["revision"] == OTHER_SHA
    assert stored["draft"] is None


def test_create_close_is_exclusive(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.init(["lead"])
    first = close.empty_close(
        "rel", scope="release", revision=SHA, revision_kind="sha",
        gate_scope="release", opened_by="lead", opened_at="t0",
        epoch_at_open=None, required_lenses=[], revision_clean=True,
        dirty_artifact=None, non_lane_isolation_not_asserted=True)
    second = close.empty_close(
        "rel", scope="release", revision=OTHER_SHA, revision_kind="sha",
        gate_scope="release", opened_by="lead", opened_at="t1",
        epoch_at_open=None, required_lenses=[], revision_clean=True,
        dirty_artifact=None, non_lane_isolation_not_asserted=True)
    close.create_close(store, first)

    with pytest.raises(close.CloseConflict, match="already exists"):
        close.create_close(store, second)

    assert close.load_close(store, "rel")["revision"] == SHA


def test_legacy_close_requires_in_lock_upgrade_before_checked_save(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.init(["lead"])
    rec = close.empty_close(
        "rel", scope="release", revision=SHA, revision_kind="sha",
        gate_scope="release", opened_by="lead", opened_at="t0",
        epoch_at_open=None, required_lenses=[], revision_clean=True,
        dirty_artifact=None, non_lane_isolation_not_asserted=True)
    rec.pop("generation")
    rec.pop("instance_id")
    path = close.close_path(store, "rel")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rec), encoding="utf-8")

    loaded = close.load_close(store, "rel")
    assert loaded["generation"] == 0
    assert loaded.get("instance_id") is None
    with pytest.raises(close.CloseConflict, match="legacy"):
        close.save_close(store, loaded, expected_generation=0,
                         expected_instance_id=None)

    upgraded = close.upgrade_legacy_close(store, "rel")
    assert upgraded["generation"] == 1
    assert isinstance(upgraded["instance_id"], str)
    close.set_draft(upgraded, body="migrated", by="lead", at="t1")
    close.save_close(
        store, upgraded, expected_generation=1,
        expected_instance_id=upgraded["instance_id"],
    )

    stored = close.load_close(store, "rel")
    assert stored["generation"] == 2
    assert stored["draft"]["body"] == "migrated"


def test_cli_full_go_lifecycle(tmp_path: Path) -> None:
    root = _init(tmp_path)
    assert _open(root) == 0
    assert _run(["close", "check", "--id", "rel"], root) == 3  # missing lens
    assert _accept(root) == 0
    assert _run(["close", "check", "--id", "rel"], root) == 0  # GO
    assert _run(["close", "publish", "--id", "rel", "--from", "lead",
                 "--verdict", "go"], root) == 0
    rec = close.load_close(Store(root), "rel")
    assert rec["status"] == close.PUBLISHED
    assert rec["final"]["verdict"] == close.VERDICT_GO


def test_m6_release_close_without_lane_artifact_holds(tmp_path: Path) -> None:
    root = _init(tmp_path)
    assert _run(["close", "open", "--id", "rel", "--from", "lead",
                 "--scope", "release", "--revision", SHA], root) == 0
    assert _run(["close", "check", "--id", "rel"], root) == 3
    rec = close.load_close(Store(root), "rel")
    result = close.compute_verdict(
        rec, {"verdict": "GO", "required_gates": [], "blockers": [], "gates": []})
    assert close.HOLD_WORKTREE_ISOLATION in _codes(result)


def test_release_close_waived_artifact_head_mismatch_holds() -> None:
    rec = close.empty_close(
        "rel", scope="release", revision=OTHER_SHA, revision_kind="sha",
        gate_scope="release", opened_by="lead", opened_at="t0",
        epoch_at_open=None, required_lenses=[], revision_clean=True,
        dirty_artifact=None)
    result = close.compute_verdict(
        rec, _gate_go(), worktree_eval={"status": "waived", "delivered_head": SHA})
    assert close.HOLD_WORKTREE_ISOLATION in _codes(result)


def test_cli_gate_hold_drives_close_hold(tmp_path: Path) -> None:
    root = _init(tmp_path)
    _open(root)
    _accept(root)
    # a required, red, release-scoped gate forces HOLD even with the lens satisfied
    _run(["gate", "set", "--from", "lead", "--name", "ci", "--status", "red",
          "--scope", "release", "--required"], root)
    assert _run(["close", "check", "--id", "rel", "--json"], root) == 3


def test_cli_waiver_drives_close_go(tmp_path: Path) -> None:
    root = _init(tmp_path)
    _open(root)
    _accept(root)
    _run(["gate", "set", "--from", "lead", "--name", "ci", "--status", "red",
          "--scope", "release", "--required"], root)
    assert _run(["close", "check", "--id", "rel"], root) == 3
    _run(["gate", "waive", "--from", "lead", "--name", "ci", "--operator", "boss",
          "--reason", "accepted", "--scope", "release", "--expires", "2099-01-01"], root)
    assert _run(["close", "check", "--id", "rel"], root) == 0


def test_cli_publish_go_refused_when_hold(tmp_path: Path) -> None:
    root = _init(tmp_path)
    _open(root)  # no ack yet -> HOLD
    assert _run(["close", "publish", "--id", "rel", "--from", "lead",
                 "--verdict", "go"], root) == 3
    rec = close.load_close(Store(root), "rel")
    assert rec["status"] == close.OPEN  # not published


def test_cli_publish_go_bump_barrier_fires_release_barrier(tmp_path: Path) -> None:
    root = _init(tmp_path)
    store = Store(root)
    assert store.current_epoch() is None
    _open(root)
    _accept(root)
    assert _run(["close", "publish", "--id", "rel", "--from", "lead",
                 "--verdict", "go", "--bump-barrier", "--reason", "ship"], root) == 0
    rec = close.load_close(store, "rel")
    epoch = rec["final"]["barrier_epoch"]
    assert epoch is not None
    assert Store(root).current_epoch() == epoch  # the release barrier is now current


def test_cli_publish_records_go_even_without_barrier_bump(tmp_path: Path) -> None:
    # the GO snapshot is persisted independently of the barrier bump (ordering
    # fix): publishing GO without --bump-barrier still records final GO + no epoch.
    root = _init(tmp_path)
    store = Store(root)
    _open(root)
    _accept(root)
    assert _run(["close", "publish", "--id", "rel", "--from", "lead",
                 "--verdict", "go"], root) == 0
    rec = close.load_close(store, "rel")
    assert rec["final"]["verdict"] == close.VERDICT_GO
    assert rec["final"]["barrier_epoch"] is None
    assert store.current_epoch() is None  # no barrier unless --bump-barrier


def test_cli_publish_hold_never_bumps_barrier(tmp_path: Path) -> None:
    root = _init(tmp_path)
    store = Store(root)
    _open(root)
    # publish HOLD is allowed even when check is HOLD (records the decision)
    assert _run(["close", "publish", "--id", "rel", "--from", "lead",
                 "--verdict", "hold", "--reason", "not ready"], root) == 3
    rec = close.load_close(store, "rel")
    assert rec["status"] == close.PUBLISHED
    assert rec["final"]["verdict"] == close.VERDICT_HOLD
    assert rec["final"]["barrier_epoch"] is None
    assert store.current_epoch() is None  # HOLD never bumps the global epoch


def test_cli_post_publish_ack_rejected_until_reopen(tmp_path: Path) -> None:
    root = _init(tmp_path)
    _open(root)
    _accept(root)
    _run(["close", "publish", "--id", "rel", "--from", "lead", "--verdict", "go"], root)
    # post-publish ack is refused (stale-proof without a global bump)...
    assert _run(["close", "ack", "--id", "rel", "--lens", "sec", "--status", "na",
                 "--from", "codex", "--reason", "late"], root) == 2
    # ...until a lead reopens
    assert _run(["close", "reopen", "--id", "rel", "--from", "lead"], root) == 0
    assert _run(["close", "ack", "--id", "rel", "--lens", "sec", "--status", "na",
                 "--from", "codex", "--reason", "late"], root) == 0


def test_cli_override_ignored_from_non_lead(tmp_path: Path) -> None:
    # reviewer-1 blocker: --override is a close-lead privilege. A non-lead passing
    # --override must NOT self-authorize a lens the open did not allow them.
    root = _init(tmp_path)  # lens "sec" allowed only to codex; lead has role=lead
    _open(root)
    # dev2 (not allowed, not a lead) acks with --override -> ignored -> stays HOLD
    assert _run(["close", "ack", "--id", "rel", "--lens", "sec", "--status", "na",
                 "--from", "dev2", "--reason", "x", "--override"], root) == 0
    assert _run(["close", "check", "--id", "rel"], root) == 3
    rec = close.load_close(Store(root), "rel")
    assert rec["lens_acks"]["sec"]["override"] is False
    result = close.compute_verdict(rec, _gate_go())
    assert close.HOLD_UNAUTHORIZED_ACK in _codes(result)


def test_cli_override_honored_from_lead(tmp_path: Path) -> None:
    root = _init(tmp_path)  # lead has role=lead -> in the close-lead set
    _open(root)
    # the lead overrides the lens (not in allowed_agents) -> authorized -> GO
    assert _run(["close", "ack", "--id", "rel", "--lens", "sec", "--status", "na",
                 "--from", "lead", "--reason", "lead sign-off", "--override"], root) == 0
    rec = close.load_close(Store(root), "rel")
    assert rec["lens_acks"]["sec"]["override"] is True
    assert _run(["close", "check", "--id", "rel"], root) == 0


def test_cli_open_refuses_duplicate_without_force(tmp_path: Path) -> None:
    root = _init(tmp_path)
    assert _open(root) == 0
    assert _open(root) == 2          # already exists
    assert _open(root, "--force") == 0


def test_cli_list_and_show(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    root = _init(tmp_path)
    _open(root)
    capsys.readouterr()
    assert _run(["close", "list"], root) == 0
    assert "rel" in capsys.readouterr().out
    assert _run(["close", "show", "--id", "rel"], root) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["close_id"] == "rel"
