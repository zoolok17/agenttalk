"""Tests for assurance P3 specialist sign-off (the signoff layer over P2 close).

Pure tests drive `compute_verdict` with a synthetic CLI-resolved ``signoff_eval``
bundle (one per stable code + the count semantics); CLI integration drives
`main(argv)` against a real store with a `.agenttalk/signoffs.json` policy.

Like the P2 tests, integration uses a full 40-char SHA so the revision freeze is
hermetic, plus a recorded --dirty-artifact so the revision never HOLDs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agenttalk import cli, close, gates
from agenttalk.store import Store

SHA = "a" * 40
OTHER_SHA = "b" * 40


def _gate_go() -> dict:
    return {"verdict": "GO", "required_gates": [], "blockers": [], "gates": []}


def _codes(result: dict) -> set[str]:
    return {h["code"] for h in result["holds"]}


def _signoff_policy(**over) -> dict:
    base = {
        "schema_version": 1,
        "defaults": {"reviewers": {"roles": ["reviewer"]}},
        "risk_policies": {"security": [{
            "id": "sec", "required_count": 2, "candidates": {"roles": ["sec"]},
            "allow_na": False}]},
        "allow_unmapped": False,
    }
    base.update(over)
    return close.validate_signoff_policy(base)


def _signoff_record(policy: dict, inventory=None) -> dict:
    rec = close.empty_close(
        "s1", scope="release", revision=SHA, revision_kind="sha",
        gate_scope="release", opened_by="lead", opened_at="t", epoch_at_open=None,
        required_lenses=[], revision_clean=True, dirty_artifact=None,
        non_lane_isolation_not_asserted=True)
    inv = inventory if inventory is not None else [
        {"risk_class": "security", "affected_paths": ["a.py"]}]
    close.apply_signoffs(rec, policy=policy, risk_inventory=inv, derived_by="lead", at="t")
    return rec


def _ev(rec, *, candidates=None, active=None, unmapped=None, policy_error=None,
        stale=False):
    route = rec.get("signoff_route") or {}
    return {
        "policy_present": True, "policy_error": policy_error,
        "current_policy_hash": "STALE" if stale else route.get("policy_hash"),
        "current_risk_inventory_hash": route.get("risk_inventory_hash"),
        "unmapped_risks": unmapped or [],
        "resolved_candidates": (candidates if candidates is not None
                                else {"security:sec": ["alice", "bob"]}),
        "active_agents": active if active is not None else ["alice", "bob"],
    }


def _sack(rec, slot, agent, *, status="accept", reason=None, override=False):
    close.apply_ack(rec, lens_id=slot, status=status, agent=agent, from_role="sec",
                    at="t", reason=reason, override=override)


def _slots(rec):
    return rec["required_signoffs"][0]["generated_lens_ids"]


# ---- pure: policy + derivation

def test_signoff_policy_rejects_bad_inputs() -> None:
    for bad in [
        {"risk_policies": {"security": [{"id": "s", "required_count": -1}]}},
        {"risk_policies": {"security": [{"required_count": 1}]}},           # no id
        {"risk_policies": {"bogus-risk!": []}},                             # bad risk_class
        {"risk_policies": {"security": [{"id": "s", "countable_statuses": ["bogus"]}]}},
        {"risk_policies": "notalist"},
    ]:
        with pytest.raises(close.CloseError):
            close.validate_signoff_policy(bad)


@pytest.mark.parametrize("field", [
    "use_default_reviewers",
    "include_domain_reviewers",
    "allow_na",
    "override_counts",
])
def test_signoff_policy_rejects_non_boolean_set_flags(field: str) -> None:
    policy = {
        "risk_policies": {"security": [{"id": "sec", field: "false"}]},
    }
    with pytest.raises(close.CloseError, match="JSON boolean"):
        close.validate_signoff_policy(policy)


@pytest.mark.parametrize("value", ["false", 0, 1, None, [], {}])
def test_signoff_policy_rejects_non_boolean_allow_unmapped(value: object) -> None:
    with pytest.raises(close.CloseError, match="JSON boolean"):
        close.validate_signoff_policy({"risk_policies": {}, "allow_unmapped": value})


def test_close_and_gate_risk_vocabularies_match() -> None:
    assert close.CORE_RISK_CLASSES == gates.CORE_RISK_CLASSES
    for risk_class in [*sorted(gates.CORE_RISK_CLASSES), "team:runtime"]:
        assert close.validate_risk_class(risk_class) == risk_class
        gates.validate_review_result_evidence("review-result", {
            "status": "approved",
            "risk_class": risk_class,
            "release_blocker": "no",
            "tests_referenced": "tests",
            "tests_executed": "tests",
            "residual_risk": "low",
            "evidence": "pointer",
            "na_reason": "vocabulary validation",
        })


def test_derive_maps_known_and_reports_unmapped() -> None:
    pol = _signoff_policy()
    d = close.derive_required_signoffs(pol, [
        {"risk_class": "security"}, {"risk_class": "performance"}, {"risk_class": "none"}])
    assert [s["id"] for s in d["signoffs"]] == ["security:sec"]
    assert d["unmapped"] == ["performance"]


def test_derive_skips_na_dispositioned_risk() -> None:
    pol = _signoff_policy()
    d = close.derive_required_signoffs(pol, [
        {"risk_class": "security", "na_reason": "not touched"}])
    assert d["signoffs"] == [] and d["unmapped"] == []


# ---- pure: count semantics

def test_signoff_go_with_two_distinct_candidates() -> None:
    rec = _signoff_record(_signoff_policy())
    s = _slots(rec)
    _sack(rec, s[0], "alice")
    _sack(rec, s[1], "bob")
    assert close.compute_verdict(rec, _gate_go(), _ev(rec))["verdict"] == close.VERDICT_GO


def test_signoff_missing_when_too_few() -> None:
    rec = _signoff_record(_signoff_policy())
    _sack(rec, _slots(rec)[0], "alice")
    assert close.HOLD_MISSING_SIGNOFF in _codes(
        close.compute_verdict(rec, _gate_go(), _ev(rec)))


def test_signoff_unique_agent_cannot_satisfy_two() -> None:
    rec = _signoff_record(_signoff_policy())
    s = _slots(rec)
    _sack(rec, s[0], "alice")
    _sack(rec, s[1], "alice")   # same agent both slots -> counts once
    assert close.HOLD_MISSING_SIGNOFF in _codes(
        close.compute_verdict(rec, _gate_go(), _ev(rec)))


def test_signoff_unroutable_when_no_candidates() -> None:
    rec = _signoff_record(_signoff_policy())
    assert close.HOLD_UNROUTABLE_SIGNOFF in _codes(
        close.compute_verdict(rec, _gate_go(), _ev(rec, candidates={"security:sec": []})))


def test_signoff_non_candidate_ack_does_not_count() -> None:
    rec = _signoff_record(_signoff_policy())
    s = _slots(rec)
    _sack(rec, s[0], "alice")
    _sack(rec, s[1], "mallory")   # not in resolved candidates
    assert close.HOLD_MISSING_SIGNOFF in _codes(
        close.compute_verdict(rec, _gate_go(), _ev(rec)))


def test_signoff_inactive_agent_ack_does_not_count() -> None:
    rec = _signoff_record(_signoff_policy())
    s = _slots(rec)
    _sack(rec, s[0], "alice")
    _sack(rec, s[1], "bob")
    assert close.HOLD_MISSING_SIGNOFF in _codes(
        close.compute_verdict(rec, _gate_go(), _ev(rec, active=["alice"])))


def test_signoff_stale_ack_does_not_count() -> None:
    rec = _signoff_record(_signoff_policy())
    s = _slots(rec)
    _sack(rec, s[0], "alice")
    _sack(rec, s[1], "bob")
    rec["lens_acks"][s[1]]["revision"] = OTHER_SHA
    assert close.HOLD_MISSING_SIGNOFF in _codes(
        close.compute_verdict(rec, _gate_go(), _ev(rec)))


def test_signoff_na_counts_only_with_allow_na() -> None:
    rec = _signoff_record(_signoff_policy())
    s = _slots(rec)
    _sack(rec, s[0], "alice", status="na", reason="ok")
    _sack(rec, s[1], "bob", status="na", reason="ok")
    assert close.HOLD_MISSING_SIGNOFF in _codes(
        close.compute_verdict(rec, _gate_go(), _ev(rec)))
    pol = _signoff_policy(risk_policies={"security": [{
        "id": "sec", "required_count": 2, "candidates": {"roles": ["sec"]},
        "allow_na": True}]})
    rec2 = _signoff_record(pol)
    s2 = _slots(rec2)
    _sack(rec2, s2[0], "alice", status="na", reason="ok")
    _sack(rec2, s2[1], "bob", status="na", reason="ok")
    assert close.compute_verdict(rec2, _gate_go(), _ev(rec2))["verdict"] == close.VERDICT_GO


def test_signoff_override_ack_not_counted_by_default() -> None:
    rec = _signoff_record(_signoff_policy())
    s = _slots(rec)
    _sack(rec, s[0], "alice", override=True)
    _sack(rec, s[1], "bob", override=True)
    assert close.HOLD_MISSING_SIGNOFF in _codes(
        close.compute_verdict(rec, _gate_go(), _ev(rec)))


def test_signoff_override_counts_when_policy_opts_in() -> None:
    pol = _signoff_policy(risk_policies={"security": [{
        "id": "sec", "required_count": 2, "candidates": {"roles": ["sec"]},
        "override_counts": True}]})
    rec = _signoff_record(pol)
    s = _slots(rec)
    _sack(rec, s[0], "alice", override=True)
    _sack(rec, s[1], "bob", override=True)
    assert close.compute_verdict(rec, _gate_go(), _ev(rec))["verdict"] == close.VERDICT_GO


def test_signoff_counter_not_counted_by_default() -> None:
    rec = _signoff_record(_signoff_policy())
    s = _slots(rec)
    _sack(rec, s[0], "alice")
    close.apply_ack(rec, lens_id=s[1], status="counter", agent="bob", from_role="sec",
                    at="t", counter_id="c1")
    close.decide_counter(rec, counter_id="c1", decision=close.COUNTER_REJECTED,
                         by="lead", at="t", reason="n/a")
    assert close.HOLD_MISSING_SIGNOFF in _codes(
        close.compute_verdict(rec, _gate_go(), _ev(rec)))


def test_signoff_stale_route_when_policy_hash_changes() -> None:
    rec = _signoff_record(_signoff_policy())
    s = _slots(rec)
    _sack(rec, s[0], "alice")
    _sack(rec, s[1], "bob")
    assert close.HOLD_STALE_ROUTE in _codes(
        close.compute_verdict(rec, _gate_go(), _ev(rec, stale=True)))


def test_signoff_invalid_policy_holds() -> None:
    rec = _signoff_record(_signoff_policy())
    assert close.HOLD_INVALID_POLICY in _codes(
        close.compute_verdict(rec, _gate_go(), _ev(rec, policy_error="bad refset")))


def test_signoff_unmapped_risk_holds() -> None:
    rec = _signoff_record(_signoff_policy())
    s = _slots(rec)
    _sack(rec, s[0], "alice")
    _sack(rec, s[1], "bob")
    assert close.HOLD_UNMAPPED_RISK in _codes(
        close.compute_verdict(rec, _gate_go(), _ev(rec, unmapped=["performance"])))


def test_signoff_override_set_satisfies_unroutable() -> None:
    rec = _signoff_record(_signoff_policy())
    close.signoff_override(rec, set_id="security:sec", by="lead", at="t",
                           reason="no specialist available; lead accepts risk")
    assert close.compute_verdict(
        rec, _gate_go(), _ev(rec, candidates={"security:sec": []}))["verdict"] == close.VERDICT_GO


def test_signoff_stale_route_on_revision_change() -> None:
    # reviewer-1 blocker: a route derived for revision A must HOLD after the close
    # is reopened to revision B, even with fresh acks, until apply is rerun.
    rec = _signoff_record(_signoff_policy())
    rec["revision"] = OTHER_SHA   # reopened to new code; route still pins SHA
    s = _slots(rec)
    _sack(rec, s[0], "alice")
    _sack(rec, s[1], "bob")
    # the acks are at OTHER_SHA (current) so they are not ack-stale, but the ROUTE
    # is stale -> HOLD until re-apply
    ev = _ev(rec)
    ev["current_risk_inventory_hash"] = rec["signoff_route"]["risk_inventory_hash"]
    assert close.HOLD_STALE_ROUTE in _codes(close.compute_verdict(rec, _gate_go(), ev))


def test_signoff_required_count_zero_is_noop() -> None:
    pol = _signoff_policy(risk_policies={"security": [{
        "id": "sec", "required_count": 0, "candidates": {"roles": ["sec"]}}]})
    rec = _signoff_record(pol)
    assert close.compute_verdict(
        rec, _gate_go(), _ev(rec, candidates={}))["verdict"] == close.VERDICT_GO


def test_p2_only_close_ignores_signoffs() -> None:
    rec = close.empty_close(
        "p2", scope="release", revision=SHA, revision_kind="sha", gate_scope="release",
        opened_by="lead", opened_at="t", epoch_at_open=None,
        required_lenses=[], revision_clean=True, dirty_artifact=None,
        non_lane_isolation_not_asserted=True)
    assert close.compute_verdict(rec, _gate_go(), None)["verdict"] == close.VERDICT_GO


def test_fail_closed_when_signoffs_present_but_no_eval() -> None:
    rec = _signoff_record(_signoff_policy())
    assert close.HOLD_INVALID_POLICY in _codes(close.compute_verdict(rec, _gate_go(), None))


def test_allowed_groups_authorizes_p2_lens() -> None:
    lens = close.validate_lens_spec({"id": "rev", "allowed_groups": ["reviewers"]})
    ack = {"from": "alice", "from_role": None, "from_groups": ["reviewers"], "override": False}
    assert close._ack_authorized(ack, lens) is True
    ack2 = {"from": "bob", "from_role": None, "from_groups": ["devs"], "override": False}
    assert close._ack_authorized(ack2, lens) is False


# ---- CLI integration

def _run(argv: list[str], root: Path) -> int:
    return cli.main(["--root", str(root), *argv])


def _init_signoff(tmp_path: Path, policy: dict | None = None) -> Path:
    s = Store(tmp_path)
    s.init(["lead", "alice", "bob", "carol"])
    s.set_role("lead", "lead")
    s.set_role("alice", "sec")
    s.set_role("bob", "sec")
    pol = policy if policy is not None else {
        "schema_version": 1,
        "risk_policies": {"security": [{
            "id": "sec", "required_count": 2, "candidates": {"roles": ["sec"]}}]},
        "allow_unmapped": False}
    (s.dir / "signoffs.json").write_text(json.dumps(pol), encoding="utf-8")
    return tmp_path


def _open_signoff(root: Path, *risks: str) -> int:
    argv = ["close", "open", "--id", "rel", "--from", "lead", "--scope", "release",
            "--revision", SHA, "--derive-signoffs", "--changed-path", "src/a.py",
            "--dirty-artifact", "d:1", "--non-lane-isolation-not-asserted"]
    for r in risks:
        argv += ["--risk-class", r]
    return _run(argv, root)


def _sign(root: Path, agent: str, slot: str) -> int:
    return _run(["close", "ack", "--id", "rel", "--lens", slot, "--status", "accept",
                 "--from", agent, "--risk-class", "none", "--release-blocker", "no",
                 "--tests-referenced", "n/a", "--tests-executed", "n/a",
                 "--residual-risk", "n/a", "--na-reason", "lw", "--evidence", "p"], root)


def test_cli_signoff_full_go(tmp_path: Path) -> None:
    root = _init_signoff(tmp_path)
    assert _open_signoff(root, "security") == 0
    assert _run(["close", "check", "--id", "rel"], root) == 3   # no signoffs yet
    rec = close.load_close(Store(root), "rel")
    s1, s2 = rec["required_signoffs"][0]["generated_lens_ids"]
    assert _sign(root, "alice", s1) == 0
    assert _sign(root, "bob", s2) == 0
    assert _run(["close", "check", "--id", "rel"], root) == 0   # GO


def test_cli_signoff_non_candidate_refused(tmp_path: Path) -> None:
    root = _init_signoff(tmp_path)
    _open_signoff(root, "security")
    rec = close.load_close(Store(root), "rel")
    s1 = rec["required_signoffs"][0]["generated_lens_ids"][0]
    assert _sign(root, "carol", s1) == 2          # carol is not role=sec
    rec2 = close.load_close(Store(root), "rel")
    assert s1 not in rec2["lens_acks"]            # nothing recorded (no displacement)


def test_cli_signoff_plan_is_readonly(tmp_path: Path) -> None:
    root = _init_signoff(tmp_path)
    _open_signoff(root, "security")
    before = close.load_close(Store(root), "rel")
    assert _run(["close", "signoffs", "plan", "--id", "rel", "--risk-class",
                 "security", "--changed-path", "src/a.py"], root) == 0
    after = close.load_close(Store(root), "rel")
    assert before == after                        # plan mutates nothing


def test_cli_signoff_unmapped_holds(tmp_path: Path) -> None:
    root = _init_signoff(tmp_path)
    assert _open_signoff(root, "security", "performance") == 0
    assert _run(["close", "check", "--id", "rel"], root) == 3   # performance unmapped


def test_cli_signoff_all_unmapped_still_holds(tmp_path: Path) -> None:
    # a close whose ONLY risk is unmapped derives ZERO signoff sets but MUST still
    # HOLD on unmapped (the trigger is "apply ran", not "sets exist").
    root = _init_signoff(tmp_path)
    assert _open_signoff(root, "performance") == 0     # no policy mapping for it
    rec = close.load_close(Store(root), "rel")
    assert rec["required_signoffs"] == []
    assert rec["signoff_route"] is not None
    assert _run(["close", "check", "--id", "rel"], root) == 3


def test_cli_signoff_override_escape(tmp_path: Path) -> None:
    pol = {"schema_version": 1, "risk_policies": {"security": [{
        "id": "sec", "required_count": 1, "candidates": {"roles": ["ghost-role"]}}]},
        "allow_unmapped": False}
    root = _init_signoff(tmp_path, policy=pol)
    _open_signoff(root, "security")
    assert _run(["close", "check", "--id", "rel"], root) == 3   # unroutable
    assert _run(["close", "signoffs", "override", "--id", "rel", "--set",
                 "security:sec", "--from", "lead", "--reason", "no specialist"], root) == 0
    assert _run(["close", "check", "--id", "rel"], root) == 0   # override resolves


def test_cli_signoff_override_refused_from_non_lead(tmp_path: Path) -> None:
    # reviewer-1 blocker: the override escape is ENFORCED close-lead, not advisory.
    pol = {"schema_version": 1, "risk_policies": {"security": [{
        "id": "sec", "required_count": 1, "candidates": {"roles": ["ghost"]}}]},
        "allow_unmapped": False}
    root = _init_signoff(tmp_path, policy=pol)
    _open_signoff(root, "security")
    assert _run(["close", "check", "--id", "rel"], root) == 3   # unroutable
    # carol is not a lead -> refused, no override recorded, still HOLD
    assert _run(["close", "signoffs", "override", "--id", "rel", "--set",
                 "security:sec", "--from", "carol", "--reason", "sneaky"], root) == 2
    rec = close.load_close(Store(root), "rel")
    assert rec.get("signoff_overrides", {}) == {}
    assert _run(["close", "check", "--id", "rel"], root) == 3   # still HOLD
    # the lead CAN
    assert _run(["close", "signoffs", "override", "--id", "rel", "--set",
                 "security:sec", "--from", "lead", "--reason", "no specialist"], root) == 0
    assert _run(["close", "check", "--id", "rel"], root) == 0


def test_cli_signoff_reopen_to_new_revision_holds_until_reapply(tmp_path: Path) -> None:
    # full-path version of the route-revision blocker through the CLI.
    root = _init_signoff(tmp_path)
    _open_signoff(root, "security")
    rec = close.load_close(Store(root), "rel")
    s1, s2 = rec["required_signoffs"][0]["generated_lens_ids"]
    _sign(root, "alice", s1)
    _sign(root, "bob", s2)
    assert _run(["close", "check", "--id", "rel"], root) == 0     # GO at original rev
    # publish GO then reopen to a new revision (simulates re-review of new code)
    _run(["close", "publish", "--id", "rel", "--from", "lead", "--verdict", "go"], root)
    assert _run(["close", "reopen", "--id", "rel", "--from", "lead",
                 "--revision", OTHER_SHA], root) == 0
    # route still pinned to the old revision -> stale until re-apply, even though the
    # prior acks would otherwise be re-collected
    assert _run(["close", "check", "--id", "rel"], root) == 3


def test_signoff_policy_rejects_non_int_schema_version() -> None:
    with pytest.raises(close.CloseError):
        close.validate_signoff_policy({"schema_version": "bad", "risk_policies": {}})


def test_cli_malformed_policy_holds_not_crashes(tmp_path: Path) -> None:
    # codex finding: a malformed signoffs.json must surface invalid_signoff_policy
    # HOLD on `close check` (exit 3), never crash with a bare ValueError (exit 2).
    root = _init_signoff(tmp_path)
    _open_signoff(root, "security")              # derives a route with a good policy
    # now corrupt the policy underneath the close
    (Store(root).dir / "signoffs.json").write_text(
        json.dumps({"schema_version": "bad", "risk_policies": {}}), encoding="utf-8")
    assert _run(["close", "check", "--id", "rel"], root) == 3   # HOLD, no crash
    out = cli.main(["--root", str(root), "close", "check", "--id", "rel", "--json"])
    assert out == 3


def test_cli_signoff_domain_reviewers_additive(tmp_path: Path) -> None:
    pol = {"schema_version": 1, "risk_policies": {"security": [{
        "id": "sec", "required_count": 1, "candidates": {"agents": []},
        "include_domain_reviewers": True}]}, "allow_unmapped": False}
    root = _init_signoff(tmp_path, policy=pol)
    s = Store(root)
    registry = {"schema_version": 1, "domains": {"auth": {
        "title": "Auth", "owners": {"agents": ["alice"]},
        "reviewers": {"agents": ["bob"]}, "owned_globs": ["src/**"]}},
        "shared_paths": []}
    (s.dir / "domains.json").write_text(json.dumps(registry), encoding="utf-8")
    _open_signoff(root, "security")               # src/a.py matches the auth domain
    rec = close.load_close(s, "rel")
    s1 = rec["required_signoffs"][0]["generated_lens_ids"][0]
    assert _sign(root, "bob", s1) == 0            # bob is a candidate ONLY via domain
    assert _run(["close", "check", "--id", "rel"], root) == 0


def test_load_signoff_policy_tolerates_utf8_bom(tmp_path: Path) -> None:
    """signoffs.json is PROJECT-OWNED policy (README); a BOM-prefixed hand-edit must
    load, not surface invalid_signoff_policy citing 'Unexpected UTF-8 BOM' (v0.75.3, D-26)."""
    root = _init_signoff(tmp_path)
    s = Store(root)
    pol = {"schema_version": 1,
           "risk_policies": {"security": [{"id": "sec", "required_count": 2,
                                           "candidates": {"roles": ["sec"]}}]},
           "allow_unmapped": False}
    (s.dir / "signoffs.json").write_bytes(b"\xef\xbb\xbf" + json.dumps(pol).encode("utf-8"))
    policy, err = close.load_signoff_policy(s)
    assert err is None                            # BOM must not read as corrupt
    assert policy is not None
