"""Quota lease binding on the gateway ledger (child-cap schema 4): the migration, one
path for every child-turn ending, receipts, the receipt page, per-row caps, the quota
lease reference, the tombstone, status in one snapshot and the start-up sweep.

All ledgers here are temporary files; nothing touches a real gateway."""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from pathlib import Path

import gateway_binding_fixtures as fx
import gateway_schema3_scenario as scenario
import pytest

from agenttalk import ovh_gateway as gateway
from agenttalk.ovh_gateway import (
    ChildTurnCapBlocked,
    ChildTurnCapExceeded,
    LedgerBlocked,
    LedgerHold,
    QuotaLeaseBindingRequired,
    QuotaLeaseReferenceMismatch,
    ReceiptPageRefused,
)

GOLDEN_SCHEMA3 = Path(__file__).resolve().parent / "golden" / "gateway_schema3_c80e1e5.json"
REPORT_KEYS = {
    "child_receipt_report_version",
    "child_receipts",
    "child_receipts_pending",
    "child_receipts_fallback",
    "child_receipts_through_seq",
}


def _schema_version(ledger) -> str:
    return fx.rows(ledger, "SELECT value FROM metadata WHERE key='child_cap_schema_version'")[0][0]


# --- AC1: a schema-3 ledger behaves exactly as on master ------------------------------------


def test_a_schema3_ledger_behaves_exactly_as_master(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway, "CHILD_TURN_MAX_CALLS", scenario.MAX_CALLS)
    clock = fx.Clock()
    ledger = fx.make_schema3_ledger(tmp_path, clock)
    record = json.loads(json.dumps(scenario.run(ledger, clock)))
    golden = json.loads(GOLDEN_SCHEMA3.read_text(encoding="utf-8"))
    assert [step[0] for step in record] == [step[0] for step in golden]
    for got, expected in zip(record, golden):
        assert got == expected, got[0]
    final_status = record[-1][2]
    assert final_status["child_cap_schema_version"] == 3
    assert not REPORT_KEYS & set(final_status)  # new keys only on schema 4 (AC5)


def test_binding_keywords_are_refused_until_binding_is_installed(tmp_path):
    ledger = fx.make_schema3_ledger(tmp_path)
    with pytest.raises(ChildTurnCapBlocked, match="not installed"):
        fx.open_bound(ledger, "msg-a")
    with pytest.raises(ChildTurnCapBlocked, match="not installed"):
        fx.close_bound(ledger, "msg-a")
    with pytest.raises(ChildTurnCapBlocked, match="not installed"):
        ledger.child_receipts_page(issuer_token=fx.ISSUER)
    with pytest.raises(ChildTurnCapBlocked, match="not installed"):
        ledger.set_quota_lease_binding_required(required=True, issuer_token=fx.ISSUER)
    assert ledger.quota_lease_binding_state(issuer_token=fx.ISSUER) == {
        "binding_installed": False,
        "quota_lease_binding_required": False,
    }
    assert ledger.sweep_child_receipts(issuer_token=fx.ISSUER)["binding_installed"] is False
    assert fx.rows(ledger, "SELECT COUNT(*) FROM child_turns") == [(0,)]


# --- 3.1: a fresh install and the migration ----------------------------------------------------


def test_a_fresh_install_creates_schema4_with_the_flag_off(tmp_path):
    ledger = fx.make_ledger(tmp_path)
    assert _schema_version(ledger) == "4"
    assert ledger.quota_lease_binding_state(issuer_token=fx.ISSUER) == {
        "binding_installed": True,
        "quota_lease_binding_required": False,
    }
    status = ledger.status()
    assert status["child_cap_schema_version"] == 4
    assert status["child_cap_policy_hash"] == gateway.child_cap_policy_hash()
    assert {key: status[key] for key in REPORT_KEYS} == {
        "child_receipt_report_version": 1,
        "child_receipts": 0,
        "child_receipts_pending": 0,
        "child_receipts_fallback": 0,
        "child_receipts_through_seq": 0,
    }


def _schema3_with_history(tmp_path, monkeypatch, clock):
    """A schema-3 ledger with one open, one closed (expired) and one capped child turn."""
    monkeypatch.setattr(gateway, "CHILD_TURN_MAX_CALLS", 1)
    ledger = fx.make_schema3_ledger(tmp_path, clock)
    fx.open_unbound(ledger, "msg-open")
    fx.open_unbound(ledger, "msg-closed")
    ledger.close_child_turn(agent="qwen-dev-1", message_id="msg-closed", reason="dead_letter",
                            issuer_token=fx.ISSUER)
    capped = fx.open_unbound(ledger, "msg-capped")
    ledger.reserve_for_child(fx.attempt(1), capability=capped.token)
    fx.settle(ledger, fx.attempt(1))
    with pytest.raises(ChildTurnCapExceeded, match="call ceiling"):
        ledger.reserve_for_child(fx.attempt(2), capability=capped.token)
    return ledger


def test_binding_install_moves_schema3_to_4_and_keeps_every_row(tmp_path, monkeypatch):
    clock = fx.Clock()
    ledger = _schema3_with_history(tmp_path, monkeypatch, clock)
    before = fx.rows(ledger, "SELECT * FROM child_turns ORDER BY message_id")
    attempts_before = fx.rows(ledger, "SELECT * FROM attempts ORDER BY attempt_id")
    clock.value += timedelta(minutes=5)

    result = ledger.install_child_cap_binding(issuer_token=fx.ISSUER)

    assert result == {
        "installed": True,
        "schema_version": 4,
        "policy_hash": gateway.child_cap_policy_hash(),
        "quota_lease_binding_required": False,
    }
    after = fx.rows(ledger, "SELECT * FROM child_turns ORDER BY message_id")
    assert [row[:10] for row in after] == before  # every key, cap, reason and time kept
    endings = {row[1]: row[10:] for row in after}
    assert endings["msg-open"] == (None, None, None, None)
    assert endings["msg-closed"] == (None, "cancelled", before[1][8], "legacy_fallback")
    assert endings["msg-capped"] == (None, "failed", before[0][8], "legacy_fallback")
    assert fx.rows(ledger, "SELECT * FROM attempts ORDER BY attempt_id") == attempts_before
    assert fx.rows(ledger, "SELECT COUNT(*) FROM child_receipts") == [(0,)]  # no reference, no receipt
    status = ledger.status()
    assert status["child_cap_schema_version"] == 4 and status["child_receipts_fallback"] == 0


def test_an_old_reader_refuses_a_schema4_ledger(tmp_path, monkeypatch):
    # The code before binding accepted exactly child-cap schema 3; emulate it here.
    ledger = fx.make_ledger(tmp_path)
    monkeypatch.setattr(gateway, "CHILD_CAP_SCHEMA_VERSION", 3)
    with pytest.raises(LedgerBlocked, match="child cap schema version is missing or mismatched"):
        ledger.status()


@pytest.mark.parametrize("step", ["begin", "copied", "renamed", "guards", "metadata"])
def test_an_interrupted_migration_leaves_schema3_whole(tmp_path, monkeypatch, step):
    clock = fx.Clock()
    ledger = _schema3_with_history(tmp_path, monkeypatch, clock)
    whole = fx.dump(ledger)
    status_before = ledger.status()

    def fault(_conn, reached):
        if reached == step:
            raise RuntimeError("injected fault")

    monkeypatch.setattr(gateway, "_binding_migration_checkpoint", fault)
    with pytest.raises(RuntimeError, match="injected fault"):
        ledger.install_child_cap_binding(issuer_token=fx.ISSUER)
    assert fx.dump(ledger) == whole
    assert ledger.status() == status_before


def test_foreign_keys_are_off_for_the_migration_and_on_afterwards(tmp_path, monkeypatch):
    ledger = fx.make_schema3_ledger(tmp_path)
    fx.open_unbound(ledger, "msg-a")
    seen = {}

    def watch(conn, step):
        seen[step] = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        if step == "guards":
            seen["check"] = conn.execute("PRAGMA foreign_key_check").fetchall()

    monkeypatch.setattr(gateway, "_binding_migration_checkpoint", watch)
    ledger.install_child_cap_binding(issuer_token=fx.ISSUER)
    assert seen["begin"] == 0 and seen["metadata"] == 0
    assert seen["check"] == []
    assert seen["foreign_keys_on"] == 1


def test_a_second_migration_changes_nothing(tmp_path, monkeypatch):
    ledger = _schema3_with_history(tmp_path, monkeypatch, fx.Clock())
    assert ledger.install_child_cap_binding(issuer_token=fx.ISSUER)["installed"] is True
    migrated = fx.dump(ledger)
    again = ledger.install_child_cap_binding(issuer_token=fx.ISSUER)
    assert again["installed"] is False and again["schema_version"] == 4
    assert fx.dump(ledger) == migrated


def test_one_unresolved_attempt_refuses_the_migration(tmp_path):
    ledger = fx.make_schema3_ledger(tmp_path)
    credential = fx.open_unbound(ledger, "msg-a")
    ledger.reserve_for_child(fx.attempt(1), capability=credential.token)
    whole = fx.dump(ledger)
    with pytest.raises(LedgerHold, match="all provider attempts resolved"):
        ledger.install_child_cap_binding(issuer_token=fx.ISSUER)
    assert fx.dump(ledger) == whole and _schema_version(ledger) == "3"


def test_a_clock_rollback_refuses_the_migration(tmp_path):
    clock = fx.Clock()
    ledger = fx.make_schema3_ledger(tmp_path, clock)
    clock.value += timedelta(minutes=10)
    fx.open_unbound(ledger, "msg-a")
    clock.value -= timedelta(minutes=5)
    whole = fx.dump(ledger)
    with pytest.raises(LedgerHold, match="clock rollback"):
        ledger.install_child_cap_binding(issuer_token=fx.ISSUER)
    assert fx.dump(ledger) == whole


def test_the_migration_needs_the_issuer_credential(tmp_path):
    ledger = fx.make_schema3_ledger(tmp_path)
    whole = fx.dump(ledger)
    for token in (None, "atgw-" + "x" * 43):
        with pytest.raises(ChildTurnCapBlocked, match="issuer"):
            ledger.install_child_cap_binding(issuer_token=token)
    assert fx.dump(ledger) == whole


def test_a_schema3_ledger_with_receipt_tables_is_refused_as_partial(tmp_path):
    ledger = fx.make_schema3_ledger(tmp_path)
    with sqlite3.connect(ledger.db_path) as conn:
        conn.execute("CREATE TABLE child_receipts (seq INTEGER)")
    with pytest.raises(LedgerBlocked, match="partial"):
        ledger.status()


def test_schema4_refuses_a_missing_guard(tmp_path):
    ledger = fx.make_ledger(tmp_path)
    with sqlite3.connect(ledger.db_path) as conn:
        conn.execute("DROP TRIGGER child_turns_ending_frozen")
    with pytest.raises(LedgerBlocked, match="guards are missing"):
        ledger.status()


@pytest.mark.parametrize("value", ["2", "", "on", None])
def test_a_malformed_flag_is_never_read_as_off(tmp_path, value):
    ledger = fx.make_ledger(tmp_path)
    with sqlite3.connect(ledger.db_path) as conn:
        if value is None:
            conn.execute("DELETE FROM metadata WHERE key='quota_lease_binding_required'")
        else:
            conn.execute(
                "UPDATE metadata SET value=? WHERE key='quota_lease_binding_required'", (value,)
            )
    with pytest.raises(LedgerBlocked, match="binding flag"):
        ledger.quota_lease_binding_state(issuer_token=fx.ISSUER)
    with pytest.raises(LedgerBlocked, match="binding flag"):
        fx.open_unbound(ledger, "msg-a")
