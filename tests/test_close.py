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

import json
from pathlib import Path

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
        revision_clean=True, dirty_artifact=None)
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
        revision_clean=True, dirty_artifact=None)
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
                 "--lens", "sec", "--allow", "sec:codex", *extra], root)


def _accept(root: Path) -> int:
    return _run(["close", "ack", "--id", "rel", "--lens", "sec", "--status",
                 "accept", "--from", "codex", "--risk-class", "none",
                 "--release-blocker", "no", "--tests-referenced", "n/a",
                 "--tests-executed", "n/a", "--residual-risk", "n/a",
                 "--na-reason", "lw", "--evidence", "pointer:rq-1"], root)


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
