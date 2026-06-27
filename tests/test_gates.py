"""C1 (0.40.0): gate fail-closed hardening + the dedicated gate test surface.

These exercise gates.py CORE directly (pure load/normalize/verdict + the new
mutation fail-closed guards) plus the CLI lock/refuse path. The CHECK-side
fail-closed (corrupt state -> __gate_state__ HOLD) is covered in test_cli.py; this
file covers the MUTATION side (refuse-on-load-error so a corrupt file is never
silently overwritten), the scoped-required-gate blocker (present-but-wrong-scope is
not absence=pass), and skipped=not-run (a blocker clears only on validated green or
an active waiver).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agenttalk import cli, gates
from agenttalk.store import Store


def _root(tmp_path: Path) -> Path:
    Store(tmp_path).init(["alpha", "beta"])
    return tmp_path


def _gp(root: Path) -> Path:
    return gates.gates_path(root)


def _run(argv: list[str], root: Path) -> int:
    return cli.main(["--root", str(root), *argv])


# --------------------------------------------- mutation fail-closed (the C1 core)

def test_set_gate_refuses_on_corrupt_state_and_leaves_file_intact(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _gp(root).write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="refusing to overwrite"):
        gates.set_gate(root, name="g1", status="red", severity="blocker",
                       scope="global", actor="alpha", evidence_source="local_command")
    assert _gp(root).read_text(encoding="utf-8") == "{not json"   # NOT clobbered


def test_waive_gate_refuses_on_corrupt_state(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _gp(root).write_text("{bad", encoding="utf-8")
    with pytest.raises(ValueError, match="refusing to overwrite"):
        gates.waive_gate(root, name="g1", operator="op", reason="r", scope="global",
                         expires="2999-01-01")
    assert _gp(root).read_text(encoding="utf-8") == "{bad"


def test_set_gate_refuses_on_malformed_shape(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _gp(root).write_text(json.dumps({"required_gates": "notalist", "gates": {}}),
                         encoding="utf-8")
    with pytest.raises(ValueError):
        gates.set_gate(root, name="g1", status="red", severity="blocker",
                       scope="global", actor="a", evidence_source="local_command")


def test_cli_gate_set_refuses_corrupt_exit_2(tmp_path: Path, capsys) -> None:
    root = _root(tmp_path)
    _gp(root).write_text("{nope", encoding="utf-8")
    rc = _run(["gate", "set", "--from", "alpha", "--name", "g1", "--status", "red",
               "--severity", "blocker", "--scope", "global",
               "--evidence-source", "local_command"], root)
    assert rc == 2
    assert "refusing to overwrite" in capsys.readouterr().err


def test_set_then_set_preserves_prior_gate(tmp_path: Path) -> None:
    # No read-modify-write loss: a second set keeps the first gate (the CLI wraps the
    # whole load->mutate->write in a single _config_lock; the core never drops state).
    root = _root(tmp_path)
    gates.set_gate(root, name="g1", status="red", severity="blocker", scope="global",
                   actor="a", evidence_source="local_command")
    gates.set_gate(root, name="g2", status="skipped", severity="warn", scope="global",
                   actor="a", evidence_source="local_command")
    assert set(gates.load_gate_state(root)["gates"]) == {"g1", "g2"}


# --------------------------------------------- scoped required gate (present-but-wrong-scope)

def test_required_gate_absent_blocks(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _gp(root).write_text(json.dumps({"schema_version": 1,
                                     "required_gates": ["mustexist"], "gates": {}}),
                         encoding="utf-8")
    res = gates.check_gates(root, scope="release")
    assert res["verdict"] == "HOLD"
    blk = next(b for b in res["blockers"] if b["name"] == "mustexist")
    assert "missing" in blk["reason"] and blk["blocks"] is True


def test_required_gate_present_wrong_scope_still_blocks(tmp_path: Path) -> None:
    # A required gate recorded under scope "release" but checked under "lane:x" used
    # to PASS the missing-check by presence and then get scope-filtered away -> a
    # false GO. It must block explicitly (fail-closed).
    root = _root(tmp_path)
    gates.set_gate(root, name="req", status="green", severity="blocker",
                   scope="release", actor="ci", evidence_source="automation_ci",
                   evidence=["http://ci/1"], required=True)
    res = gates.check_gates(root, scope="lane:x")
    assert res["verdict"] == "HOLD"
    blk = next(b for b in res["blockers"] if b["name"] == "req")
    assert "not applicable" in blk["reason"] and blk["blocks"] is True


def test_required_gate_global_scope_satisfies_scoped_check(tmp_path: Path) -> None:
    root = _root(tmp_path)
    gates.set_gate(root, name="req", status="green", severity="blocker",
                   scope="global", actor="ci", evidence_source="automation_ci",
                   evidence=["ci"], required=True)
    assert gates.check_gates(root, scope="lane:x")["verdict"] == "GO"


def test_required_gate_matching_scope_satisfies(tmp_path: Path) -> None:
    root = _root(tmp_path)
    gates.set_gate(root, name="req", status="green", severity="blocker",
                   scope="lane:x", actor="ci", evidence_source="automation_ci",
                   evidence=["ci"], required=True)
    assert gates.check_gates(root, scope="lane:x")["verdict"] == "GO"


# --------------------------------------------- skipped = not-run (not not-applicable)

def test_skipped_blocker_holds(tmp_path: Path) -> None:
    root = _root(tmp_path)
    gates.set_gate(root, name="s", status="skipped", severity="blocker",
                   scope="global", actor="a", evidence_source="local_command")
    res = gates.check_gates(root)
    assert res["verdict"] == "HOLD"
    blk = next(b for b in res["blockers"] if b["name"] == "s")
    assert "skipped" in blk["reason"]


def test_skipped_warn_does_not_block(tmp_path: Path) -> None:
    root = _root(tmp_path)
    gates.set_gate(root, name="s", status="skipped", severity="warn",
                   scope="global", actor="a", evidence_source="local_command")
    assert gates.check_gates(root)["verdict"] == "GO"


def test_red_blocker_holds(tmp_path: Path) -> None:
    root = _root(tmp_path)
    gates.set_gate(root, name="r", status="red", severity="blocker",
                   scope="global", actor="a", evidence_source="local_command")
    assert gates.check_gates(root)["verdict"] == "HOLD"


def test_unknown_blocker_holds(tmp_path: Path) -> None:
    root = _root(tmp_path)
    gates.set_gate(root, name="u", status="unknown", severity="blocker",
                   scope="global", actor="a", evidence_source="local_command")
    assert gates.check_gates(root)["verdict"] == "HOLD"


# --------------------------------------------- waiver active / expired / parse

def test_active_waiver_clears_blocker(tmp_path: Path) -> None:
    root = _root(tmp_path)
    future = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
    gates.waive_gate(root, name="w", operator="op", reason="ok", scope="global",
                     expires=future)
    assert gates.check_gates(root)["verdict"] == "GO"


def test_expired_waiver_blocks_blocker(tmp_path: Path) -> None:
    root = _root(tmp_path)
    past = (datetime.now(timezone.utc) - timedelta(days=2)).date().isoformat()
    gates.waive_gate(root, name="w", operator="op", reason="ok", scope="global",
                     expires=past)
    res = gates.check_gates(root)
    assert res["verdict"] == "HOLD"
    blk = next(b for b in res["blockers"] if b["name"] == "w")
    assert "expired" in blk["reason"]


def test_waive_gate_rejects_unparseable_expiry(tmp_path: Path) -> None:
    root = _root(tmp_path)
    with pytest.raises(ValueError):
        gates.waive_gate(root, name="w", operator="op", reason="ok", scope="global",
                         expires="not-a-date")


# --------------------------------------------- blocker-green evidence source rules

def test_blocker_green_requires_ci_or_waiver_source(tmp_path: Path) -> None:
    root = _root(tmp_path)
    with pytest.raises(ValueError, match="automation_ci"):
        gates.set_gate(root, name="g", status="green", severity="blocker",
                       scope="global", actor="a", evidence_source="manual_review",
                       evidence=["x"])


def test_blocker_green_needs_evidence(tmp_path: Path) -> None:
    root = _root(tmp_path)
    with pytest.raises(ValueError, match="evidence"):
        gates.set_gate(root, name="g", status="green", severity="blocker",
                       scope="global", actor="a", evidence_source="automation_ci")


def test_warn_green_does_not_block_and_needs_no_ci(tmp_path: Path) -> None:
    root = _root(tmp_path)
    gates.set_gate(root, name="g", status="green", severity="warn", scope="global",
                   actor="a", evidence_source="manual_review")
    assert gates.check_gates(root)["verdict"] == "GO"


# --------------------------------------------- load / normalize branches

def test_load_error_on_mismatched_gate_name(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _gp(root).write_text(json.dumps({
        "required_gates": [],
        "gates": {"g1": {"name": "different", "status": "red", "severity": "warn",
                         "scope": "global"}}}), encoding="utf-8")
    assert gates.load_gate_state(root).get("load_error")


def test_load_error_on_stored_green_blocker_without_evidence(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _gp(root).write_text(json.dumps({
        "required_gates": [],
        "gates": {"g": {"name": "g", "status": "green", "severity": "blocker",
                        "scope": "global", "evidence_source": "automation_ci"}}}),
        encoding="utf-8")
    assert gates.load_gate_state(root).get("load_error")


def test_missing_file_is_empty_go(tmp_path: Path) -> None:
    root = _root(tmp_path)
    res = gates.check_gates(root, scope="release")
    assert res["verdict"] == "GO" and res["required_gates"] == []
