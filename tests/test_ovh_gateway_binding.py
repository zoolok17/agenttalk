"""Quota lease binding on the gateway ledger (child-cap schema 4): the migration, one
path for every child-turn ending, receipts, the receipt page, per-row caps, the quota
lease reference, the tombstone, status in one snapshot and the start-up sweep.

All ledgers here are temporary files; nothing touches a real gateway."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from datetime import timedelta
from pathlib import Path

import gateway_binding_fixtures as fx
import gateway_schema3_scenario as scenario
import pytest

from agenttalk import cli
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


@pytest.fixture(autouse=True)
def _never_reach_the_live_ledger(tmp_path_factory, monkeypatch) -> None:
    # A bare SpendLedger() resolves to the host's real ledger via LOCALAPPDATA.
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path_factory.mktemp("no-live-appdata")))


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
    for got, expected in zip(record, golden, strict=True):
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


# --- 3.2 and 3.3: one ending path, receipts and their totals ---------------------------------


REF_HASH = hashlib.sha256(fx.REFERENCE.encode("ascii")).hexdigest()


def _receipts(ledger) -> list[tuple]:
    return fx.rows(
        ledger,
        "SELECT seq, agent, message_id, quota_lease_ref_sha256, outcome, calls, input_tokens, "
        "output_tokens, actual_micro_eur, closed_at FROM child_receipts ORDER BY seq",
    )


def _pending(ledger) -> list[tuple]:
    return fx.rows(ledger, "SELECT agent, message_id, created_at, resolved_at FROM receipt_pending")


def _turn(ledger, message_id: str) -> dict:
    with sqlite3.connect(ledger.db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM child_turns WHERE message_id=?", (message_id,)).fetchone()
        return dict(row) if row else {}


def _bound_with_one_settled_call(ledger, message_id="msg-a", **caps):
    credential = fx.open_bound(ledger, message_id, **caps)
    ledger.reserve_for_child(fx.attempt(1), capability=credential.token)
    fx.settle(ledger, fx.attempt(1))
    return credential


def test_an_explicit_close_writes_the_receipt_with_the_callers_outcome(tmp_path):
    clock = fx.Clock()
    ledger = fx.make_ledger(tmp_path, clock)
    _bound_with_one_settled_call(ledger)
    clock.value += timedelta(minutes=3)
    assert fx.close_bound(ledger, "msg-a", outcome="completed") == "closed"
    assert _receipts(ledger) == [
        (1, "qwen-dev-1", "msg-a", REF_HASH, "completed", 1, 1_000, 100, 670,
         "2026-10-03T10:03:00.000000Z"),
    ]
    turn = _turn(ledger, "msg-a")
    assert (turn["state"], turn["terminal_outcome"], turn["terminal_source"]) == (
        "expired", "completed", "recorded")


def test_lazy_expiry_at_open_ends_cancelled_at_the_expiry_time(tmp_path):
    clock = fx.Clock()
    ledger = fx.make_ledger(tmp_path, clock)
    credential = fx.open_bound(ledger, "msg-a", ttl_seconds=600)
    clock.value += timedelta(seconds=601)
    with pytest.raises(ChildTurnCapExceeded, match="wall-time"):
        fx.open_bound(ledger, "msg-a", ttl_seconds=600)
    assert _receipts(ledger) == [
        (1, "qwen-dev-1", "msg-a", REF_HASH, "cancelled", 0, 0, 0, 0, credential.expires_at),
    ]


def test_expiry_at_reserve_ends_cancelled_at_the_expiry_time(tmp_path):
    clock = fx.Clock()
    ledger = fx.make_ledger(tmp_path, clock)
    credential = _bound_with_one_settled_call(ledger, ttl_seconds=600)
    clock.value += timedelta(seconds=700)
    with pytest.raises(ChildTurnCapExceeded, match="wall-time"):
        ledger.reserve_for_child(fx.attempt(2), capability=credential.token)
    assert _receipts(ledger)[0][4:] == ("cancelled", 1, 1_000, 100, 670, credential.expires_at)


@pytest.mark.parametrize("caps, refusal", [
    ({"max_calls": 1}, "call ceiling"),
    ({"max_micro_eur": gateway.reservation_cost_micro_eur()}, "cost ceiling"),
])
def test_a_call_or_cost_refusal_at_reserve_ends_failed_at_the_call_time(tmp_path, caps, refusal):
    clock = fx.Clock()
    ledger = fx.make_ledger(tmp_path, clock)
    credential = _bound_with_one_settled_call(ledger, **caps)
    clock.value += timedelta(minutes=7)
    with pytest.raises(ChildTurnCapExceeded, match=refusal):
        ledger.reserve_for_child(fx.attempt(2), capability=credential.token)
    assert _receipts(ledger)[0][4:] == ("failed", 1, 1_000, 100, 670, "2026-10-03T10:07:00.000000Z")
    assert _turn(ledger, "msg-a")["state"] == "capped"


def _close_case(ledger, clock):
    _bound_with_one_settled_call(ledger)
    return lambda: fx.close_bound(ledger, "msg-a")


def _open_expiry_case(ledger, clock):
    fx.open_bound(ledger, "msg-a", ttl_seconds=600)
    clock.value += timedelta(seconds=601)
    return lambda: fx.open_bound(ledger, "msg-a", ttl_seconds=600)


def _reserve_expiry_case(ledger, clock):
    credential = _bound_with_one_settled_call(ledger, ttl_seconds=600)
    clock.value += timedelta(seconds=601)
    return lambda: ledger.reserve_for_child(fx.attempt(2), capability=credential.token)


def _reserve_refusal_case(ledger, clock):
    credential = _bound_with_one_settled_call(ledger, max_calls=1)
    return lambda: ledger.reserve_for_child(fx.attempt(2), capability=credential.token)


@pytest.mark.parametrize("case", [
    _close_case, _open_expiry_case, _reserve_expiry_case, _reserve_refusal_case,
])
def test_the_ending_and_its_receipt_commit_together(tmp_path, monkeypatch, case):
    # A fault between the state update and the receipt insert rolls both back.
    clock = fx.Clock()
    ledger = fx.make_ledger(tmp_path, clock)
    act = case(ledger, clock)
    before = fx.dump(ledger)

    def fault(*_args, **_kwargs):
        raise RuntimeError("injected fault after the state update")

    monkeypatch.setattr(gateway.SpendLedger, "_ensure_custody", fault)
    with pytest.raises(RuntimeError, match="injected fault"):
        act()
    assert fx.dump(ledger) == before
    monkeypatch.undo()
    with pytest.raises(gateway.GatewayError) if case is not _close_case else _no_error():
        act()
    assert len(_receipts(ledger)) == 1


class _no_error:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_an_unresolved_attempt_makes_a_pending_note_and_settle_completes_it(tmp_path):
    clock = fx.Clock()
    ledger = fx.make_ledger(tmp_path, clock)
    credential = fx.open_bound(ledger, "msg-a")
    ledger.reserve_for_child(fx.attempt(1), capability=credential.token)
    assert fx.close_bound(ledger, "msg-a") == "closed"
    assert _receipts(ledger) == []
    assert _pending(ledger) == [("qwen-dev-1", "msg-a", "2026-10-03T10:00:00.000000Z", None)]
    # the attempt keeps its liability: no refund while it is unresolved
    assert [row["attempt_id"] for row in ledger.status()["unresolved"]] == [fx.attempt(1)]
    clock.value += timedelta(minutes=2)
    fx.settle(ledger, fx.attempt(1))
    assert _receipts(ledger)[0][4:9] == ("completed", 1, 1_000, 100, 670)
    assert _pending(ledger) == [
        ("qwen-dev-1", "msg-a", "2026-10-03T10:00:00.000000Z", "2026-10-03T10:02:00.000000Z")
    ]


def test_reconcile_completes_a_pending_note(tmp_path):
    ledger = fx.make_ledger(tmp_path)
    credential = fx.open_bound(ledger, "msg-a")
    ledger.reserve_for_child(fx.attempt(1), capability=credential.token)
    ledger.mark_uncertain(fx.attempt(1), reason="usage lost")
    fx.close_bound(ledger, "msg-a", outcome="failed")
    assert _receipts(ledger) == [] and len(_pending(ledger)) == 1
    ledger.reconcile(fx.attempt(1), outcome="charge-reserve", reason="operator")
    reserve = gateway.reservation_cost_micro_eur()
    assert _receipts(ledger)[0][4:9] == ("failed", 1, None, None, reserve)
    assert _pending(ledger)[0][3] is not None


def test_mark_uncertain_never_completes_a_receipt(tmp_path):
    ledger = fx.make_ledger(tmp_path)
    credential = fx.open_bound(ledger, "msg-a")
    ledger.reserve_for_child(fx.attempt(1), capability=credential.token)
    fx.close_bound(ledger, "msg-a")
    ledger.mark_uncertain(fx.attempt(1), reason="usage lost")
    assert _receipts(ledger) == [] and _pending(ledger)[0][3] is None


def test_unbound_and_open_child_turns_get_no_receipt_or_note(tmp_path):
    ledger = fx.make_ledger(tmp_path)
    credential = fx.open_unbound(ledger, "msg-unbound")
    ledger.reserve_for_child(fx.attempt(1), capability=credential.token)
    fx.settle(ledger, fx.attempt(1))
    ledger.close_child_turn(agent="qwen-dev-1", message_id="msg-unbound", reason="dead_letter",
                            issuer_token=fx.ISSUER)
    credential = fx.open_bound(ledger, "msg-open")
    ledger.reserve_for_child(fx.attempt(2), capability=credential.token)
    fx.settle(ledger, fx.attempt(2))
    assert _receipts(ledger) == [] and _pending(ledger) == []
    assert ledger.sweep_child_receipts(issuer_token=fx.ISSUER)["receipts_written"] == 0


def test_a_refusal_at_reserve_keeps_expired_and_its_first_reason(tmp_path):
    clock = fx.Clock()
    ledger = fx.make_ledger(tmp_path, clock)
    credential = fx.open_bound(ledger, "msg-a", ttl_seconds=600)
    clock.value += timedelta(seconds=601)
    with pytest.raises(ChildTurnCapExceeded, match="wall-time"):
        ledger.reserve_for_child(fx.attempt(1), capability=credential.token)
    first = _turn(ledger, "msg-a")
    clock.value += timedelta(minutes=1)
    with pytest.raises(ChildTurnCapExceeded):
        ledger.reserve_for_child(fx.attempt(2), capability=credential.token)
    assert _turn(ledger, "msg-a") == first  # no flip, no new reason or time
    assert first["state"] == "expired" and first["reason"] == "child turn wall-time ceiling exceeded"


def test_a_second_close_keeps_the_first_outcome_and_time(tmp_path):
    clock = fx.Clock()
    ledger = fx.make_ledger(tmp_path, clock)
    _bound_with_one_settled_call(ledger)
    fx.close_bound(ledger, "msg-a", outcome="completed")
    first = _turn(ledger, "msg-a")
    clock.value += timedelta(minutes=1)
    assert fx.close_bound(ledger, "msg-a", outcome="failed") == "already_terminal"
    assert _turn(ledger, "msg-a") == first
    assert len(_receipts(ledger)) == 1 and _receipts(ledger)[0][4] == "completed"


def _overrun(ledger, attempt_id):
    # settle above policy leaves the attempt uncertain with its usage, and holds the gateway
    ledger.settle(attempt_id, model=gateway.MODEL_ALIAS, input_tokens=1_000,
                  output_tokens=gateway.MAX_OUTPUT_TOKENS + 1)


def test_receipt_totals_for_each_kind_of_resolution(tmp_path):
    ledger = fx.make_ledger(tmp_path)
    reserve = gateway.reservation_cost_micro_eur()
    overrun_cost = gateway.settlement_cost_micro_eur(1_000, gateway.MAX_OUTPUT_TOKENS + 1)

    def child(message_id, reference, resolve):
        credential = fx.open_bound(ledger, message_id, reference=reference)
        ledger.reserve_for_child(attempt_ids[message_id], capability=credential.token)
        resolve(attempt_ids[message_id])
        fx.close_bound(ledger, message_id, reference=reference)

    attempt_ids = {f"msg-{n}": fx.attempt(n) for n in range(1, 5)}
    child("msg-1", "ref-settled", lambda a: fx.settle(ledger, a))
    child("msg-2", "ref-overrun", lambda a: (
        _overrun(ledger, a), ledger.reconcile(a, outcome="charge-reserve", reason="operator")))
    child("msg-3", "ref-no-usage", lambda a: (
        ledger.mark_uncertain(a, reason="usage lost"),
        ledger.reconcile(a, outcome="charge-reserve", reason="operator")))
    child("msg-4", "ref-no-send", lambda a: ledger.reconcile(a, outcome="no-send", reason="not sent"))
    fx.close_bound(ledger, "msg-fence", outcome="cancelled", reference="ref-fence")
    totals = {row[2]: row[5:9] for row in _receipts(ledger)}
    assert totals == {
        "msg-1": (1, 1_000, 100, 670),
        "msg-2": (1, 1_000, gateway.MAX_OUTPUT_TOKENS + 1, max(overrun_cost, reserve)),
        "msg-3": (1, None, None, reserve),
        "msg-4": (0, 0, 0, 0),
        "msg-fence": (0, 0, 0, 0),
    }


def test_receipt_totals_for_mixed_attempts(tmp_path):
    ledger = fx.make_ledger(tmp_path)
    # settled plus no-send: one call
    credential = fx.open_bound(ledger, "msg-a", reference="ref-a")
    ledger.reserve_for_child(fx.attempt(1), capability=credential.token)
    fx.settle(ledger, fx.attempt(1))
    ledger.reserve_for_child(fx.attempt(2), capability=credential.token)
    ledger.reconcile(fx.attempt(2), outcome="no-send", reason="not sent")
    fx.close_bound(ledger, "msg-a", reference="ref-a")
    # settled plus unknown usage: the money is known, the tokens are not
    credential = fx.open_bound(ledger, "msg-b", reference="ref-b")
    ledger.reserve_for_child(fx.attempt(3), capability=credential.token)
    fx.settle(ledger, fx.attempt(3))
    ledger.reserve_for_child(fx.attempt(4), capability=credential.token)
    ledger.mark_uncertain(fx.attempt(4), reason="usage lost")
    ledger.reconcile(fx.attempt(4), outcome="charge-reserve", reason="operator")
    fx.close_bound(ledger, "msg-b", reference="ref-b")
    totals = {row[2]: row[5:9] for row in _receipts(ledger)}
    assert totals["msg-a"] == (1, 1_000, 100, 670)
    assert totals["msg-b"] == (2, None, None, 670 + gateway.reservation_cost_micro_eur())


# --- 3.2 repair: a referenced row that ended without a recorded ending -------------------------
#
# The CHECK makes such a row impossible, and SQLite's quick_check (run on every
# connection since before binding) refuses a ledger that holds one. So the public
# calls refuse the whole ledger, and the repair itself is proven on a raw connection
# where only the hand-made INSERT bypasses the CHECK; the repair runs under the real
# triggers and CHECK.


def _hand_made_unrecorded_ending(conn, message_id="msg-restored", state="expired"):
    conn.execute("PRAGMA ignore_check_constraints=ON")
    conn.execute(
        """INSERT INTO child_turns(agent, message_id, request_id, state, max_calls,
               max_micro_eur, opened_at, expires_at, updated_at, reason,
               quota_lease_ref_sha256)
           VALUES ('qwen-dev-1', ?, '', ?, 5, 1000000, '2026-10-03T09:00:00.000000Z',
               '2026-10-03T19:00:00.000000Z', '2026-10-03T09:30:00.000000Z', 'restored',
               ?)""",
        (message_id, state, hashlib.sha256(message_id.encode("ascii")).hexdigest()),
    )
    conn.commit()
    conn.execute("PRAGMA ignore_check_constraints=OFF")  # the real CHECK, from here on


def _raw(ledger):
    conn = sqlite3.connect(ledger.db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def test_a_ledger_holding_an_unrecorded_ending_is_refused_whole(tmp_path):
    ledger = fx.make_ledger(tmp_path)
    conn = _raw(ledger)
    try:
        _hand_made_unrecorded_ending(conn)
    finally:
        conn.close()
    with pytest.raises(LedgerBlocked, match="integrity check"):
        ledger.status()
    with pytest.raises(LedgerBlocked, match="integrity check"):
        ledger.sweep_child_receipts(issuer_token=fx.ISSUER)


@pytest.mark.parametrize("state, fallback", [("expired", "cancelled"), ("capped", "failed")])
def test_a_missing_ending_is_repaired_with_the_fallback_and_counted(tmp_path, state, fallback):
    ledger = fx.make_ledger(tmp_path)
    conn = _raw(ledger)
    try:
        _hand_made_unrecorded_ending(conn, state=state)
        conn.execute("BEGIN IMMEDIATE")
        assert ledger._ensure_custody(
            conn, "qwen-dev-1", "msg-restored", at="2026-10-03T10:00:00.000000Z"
        ) == "receipt_written"
        conn.execute("COMMIT")
        assert conn.execute("PRAGMA quick_check(1)").fetchone()[0] == "ok"
    finally:
        conn.close()
    turn = _turn(ledger, "msg-restored")
    assert (turn["state"], turn["reason"]) == (state, "restored")  # state and reason stay
    assert (turn["terminal_outcome"], turn["terminal_at"], turn["terminal_source"]) == (
        fallback, "2026-10-03T09:30:00.000000Z", "legacy_fallback")
    assert _receipts(ledger)[0][4:] == (fallback, 0, 0, 0, 0, "2026-10-03T09:30:00.000000Z")
    status = ledger.status()  # the repaired ledger is whole again
    assert (status["child_receipts_fallback"], status["child_receipts"]) == (1, 1)


def test_a_failed_repair_rolls_back_whole(tmp_path, monkeypatch):
    ledger = fx.make_ledger(tmp_path)
    conn = _raw(ledger)

    def fault(*_args):
        raise RuntimeError("injected fault after the repair")

    monkeypatch.setattr(gateway.SpendLedger, "_child_receipt_totals", staticmethod(fault))
    try:
        _hand_made_unrecorded_ending(conn)
        before = conn.execute("SELECT * FROM child_turns").fetchall()
        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(RuntimeError, match="injected fault"):
            ledger._ensure_custody(conn, "qwen-dev-1", "msg-restored", at="2026-10-03T10:00:00.000000Z")
        assert conn.execute("SELECT terminal_source FROM child_turns").fetchone()[0] == "legacy_fallback"
        conn.execute("ROLLBACK")
        assert conn.execute("SELECT * FROM child_turns").fetchall() == before
        assert conn.execute("SELECT COUNT(*) FROM child_receipts").fetchone()[0] == 0
    finally:
        conn.close()


def test_the_guards_allow_only_the_empty_to_filled_repair(tmp_path):
    ledger = fx.make_ledger(tmp_path)
    _bound_with_one_settled_call(ledger)
    fx.close_bound(ledger, "msg-a")
    with sqlite3.connect(ledger.db_path) as conn:
        for statement in (
            "UPDATE child_turns SET terminal_outcome='failed' WHERE message_id='msg-a'",
            "UPDATE child_turns SET terminal_source='legacy_fallback' WHERE message_id='msg-a'",
            "UPDATE child_turns SET state='capped' WHERE message_id='msg-a'",
            "UPDATE child_turns SET reason='other' WHERE message_id='msg-a'",
        ):
            with pytest.raises(sqlite3.DatabaseError, match="ending is frozen"):
                conn.execute(statement)


# --- 3.4: the tables, the guards and the page -------------------------------------------------


def test_receipts_and_pending_notes_cannot_be_deleted_or_rewritten(tmp_path):
    ledger = fx.make_ledger(tmp_path)
    credential = fx.open_bound(ledger, "msg-a")
    ledger.reserve_for_child(fx.attempt(1), capability=credential.token)
    fx.close_bound(ledger, "msg-a")
    fx.settle(ledger, fx.attempt(1))
    with sqlite3.connect(ledger.db_path) as conn:
        for statement, word in (
            ("DELETE FROM child_receipts", "permanent"),
            ("UPDATE child_receipts SET calls=5", "permanent"),
            ("DELETE FROM receipt_pending", "permanent"),
            ("UPDATE receipt_pending SET resolved_at=NULL", "resolved time"),
            ("UPDATE receipt_pending SET created_at='x'", "resolved time"),
        ):
            with pytest.raises(sqlite3.DatabaseError, match=word):
                conn.execute(statement)


def test_no_module_deletes_or_compacts_receipts():
    root = Path(gateway.__file__).resolve().parent
    forbidden = re.compile(
        r"(DELETE\s+FROM|DROP\s+TABLE|TRUNCATE)\s+(IF\s+EXISTS\s+)?(child_receipts|receipt_pending)"
        r"|\bVACUUM\b",
        re.IGNORECASE,
    )
    offenders = [
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if forbidden.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def _two_receipts(ledger, clock):
    _bound_with_one_settled_call(ledger, "msg-a", reference="ref-a")
    fx.close_bound(ledger, "msg-a", reference="ref-a", outcome="completed")
    clock.value += timedelta(minutes=5)
    credential = fx.open_bound(ledger, "msg-b", reference="ref-b")
    ledger.reserve_for_child(fx.attempt(2), capability=credential.token)
    ledger.mark_uncertain(fx.attempt(2), reason="usage lost")
    ledger.reconcile(fx.attempt(2), outcome="charge-reserve", reason="operator")
    fx.close_bound(ledger, "msg-b", reference="ref-b", outcome="failed")


def test_the_page_has_exactly_the_closed_shape(tmp_path):
    clock = fx.Clock()
    ledger = fx.make_ledger(tmp_path, clock)
    _two_receipts(ledger, clock)
    page = ledger.child_receipts_page(after_seq=0, limit=100, issuer_token=fx.ISSUER)
    sha = {ref: hashlib.sha256(ref.encode("ascii")).hexdigest() for ref in ("ref-a", "ref-b")}
    assert page == {
        "after_seq": 0,
        "envelope_version": 1,
        "generation": fx.GENERATION,
        "has_more": False,
        "next_seq": 2,
        "receipts": [
            {"actual_micro_eur": 670, "calls": 1, "closed_at": "2026-10-03T10:00:00.000000Z",
             "input_tokens": 1_000, "outcome": "completed", "output_tokens": 100,
             "quota_lease_ref_sha256": sha["ref-a"], "seq": 1},
            {"actual_micro_eur": gateway.reservation_cost_micro_eur(), "calls": 1,
             "closed_at": "2026-10-03T10:05:00.000000Z", "input_tokens": None,
             "outcome": "failed", "output_tokens": None,
             "quota_lease_ref_sha256": sha["ref-b"], "seq": 2},
        ],
    }
    text = json.dumps(page, sort_keys=True, separators=(",", ":"))
    assert gateway.parse_receipt_page(text, after_seq=0, limit=100) == page


def test_an_empty_page_still_names_the_generation(tmp_path):
    ledger = fx.make_ledger(tmp_path)
    assert ledger.child_receipts_page(issuer_token=fx.ISSUER) == {
        "after_seq": 0, "envelope_version": 1, "generation": fx.GENERATION,
        "has_more": False, "next_seq": 0, "receipts": [],
    }
    assert ledger.child_receipts_page(after_seq=7, issuer_token=fx.ISSUER)["next_seq"] == 7


def test_paging_with_has_more(tmp_path):
    clock = fx.Clock()
    ledger = fx.make_ledger(tmp_path, clock)
    _two_receipts(ledger, clock)
    first = ledger.child_receipts_page(after_seq=0, limit=1, issuer_token=fx.ISSUER)
    assert [r["seq"] for r in first["receipts"]] == [1]
    assert first["has_more"] is True and first["next_seq"] == 1
    second = ledger.child_receipts_page(after_seq=1, limit=1, issuer_token=fx.ISSUER)
    assert [r["seq"] for r in second["receipts"]] == [2]
    assert second["has_more"] is False and second["next_seq"] == 2


@pytest.mark.parametrize("kwargs", [
    {"limit": 0}, {"limit": 1001}, {"limit": True}, {"limit": 1.0},
    {"after_seq": -1}, {"after_seq": True}, {"after_seq": 2**63}, {"after_seq": "0"},
])
def test_a_page_request_out_of_bounds_is_refused(tmp_path, kwargs):
    ledger = fx.make_ledger(tmp_path)
    with pytest.raises(ValueError):
        ledger.child_receipts_page(issuer_token=fx.ISSUER, **kwargs)


def test_reads_never_write(tmp_path):
    clock = fx.Clock()
    ledger = fx.make_ledger(tmp_path, clock)
    credential = fx.open_bound(ledger, "msg-a", ttl_seconds=60)
    clock.value += timedelta(seconds=61)  # expired, but only a writer or the sweep ends it
    before = fx.dump(ledger)
    ledger.child_receipts_page(issuer_token=fx.ISSUER)
    ledger.status()
    ledger.quota_lease_binding_state(issuer_token=fx.ISSUER)
    assert fx.dump(ledger) == before and credential


def test_receipt_numbers_have_no_gaps(tmp_path, monkeypatch):
    clock = fx.Clock()
    ledger = fx.make_ledger(tmp_path, clock)
    _bound_with_one_settled_call(ledger, "msg-a", reference="ref-a")
    fx.close_bound(ledger, "msg-a", reference="ref-a")
    # a duplicate close writes no second receipt
    assert fx.close_bound(ledger, "msg-a", reference="ref-a") == "already_terminal"
    # an ignored insert (a duplicate key) uses no number
    with sqlite3.connect(ledger.db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO child_receipts(agent, message_id, quota_lease_ref_sha256, "
            "outcome, calls, actual_micro_eur, closed_at) VALUES ('qwen-dev-1', 'msg-a', ?, "
            "'completed', 0, 0, '2026-10-03T10:00:00.000000Z')",
            (hashlib.sha256(b"ref-a").hexdigest(),),
        )
    # a rolled-back transaction uses no number either
    real_commit = gateway.SpendLedger._commit
    calls = {"n": 0}

    def commit_fails_once(self, conn):
        calls["n"] += 1
        if calls["n"] == 1:
            raise LedgerBlocked("injected commit failure")
        return real_commit(self, conn)

    monkeypatch.setattr(gateway.SpendLedger, "_commit", commit_fails_once)
    with pytest.raises(LedgerBlocked, match="injected"):
        fx.close_bound(ledger, "msg-fence", reference="ref-fence", outcome="cancelled")
    assert fx.close_bound(ledger, "msg-fence", reference="ref-fence", outcome="cancelled") == "fenced"
    fx.close_bound(ledger, "msg-fence-2", reference="ref-fence-2", outcome="cancelled")
    assert [row[0] for row in _receipts(ledger)] == [1, 2, 3]
    page = ledger.child_receipts_page(issuer_token=fx.ISSUER)
    assert [r["seq"] for r in page["receipts"]] == [1, 2, 3]


def test_a_page_over_a_gap_is_refused(tmp_path):
    ledger = fx.make_ledger(tmp_path)
    fx.close_bound(ledger, "msg-a", reference="ref-a", outcome="cancelled")
    with sqlite3.connect(ledger.db_path) as conn:  # damage: a receipt with a jumped number
        conn.execute(
            "INSERT INTO child_receipts(seq, agent, message_id, quota_lease_ref_sha256, outcome, "
            "calls, input_tokens, output_tokens, actual_micro_eur, closed_at) VALUES (3, 'x', 'y', "
            "?, 'cancelled', 0, 0, 0, 0, '2026-10-03T10:00:00.000000Z')",
            (hashlib.sha256(b"ref-x").hexdigest(),),
        )
    with pytest.raises(ReceiptPageRefused, match="gap"):
        ledger.child_receipts_page(issuer_token=fx.ISSUER)
    with pytest.raises(ReceiptPageRefused, match="gap"):  # a gap before the page, too
        ledger.child_receipts_page(after_seq=2, issuer_token=fx.ISSUER)


def test_an_unrepresentable_amount_refuses_the_page_and_is_kept(tmp_path):
    ledger = fx.make_ledger(tmp_path)
    with sqlite3.connect(ledger.db_path) as conn:
        conn.execute(
            "INSERT INTO child_turns(agent, message_id, request_id, state, max_calls, max_micro_eur, "
            "opened_at, expires_at, updated_at, reason, quota_lease_ref_sha256, terminal_outcome, "
            "terminal_at, terminal_source) VALUES ('qwen-dev-1', 'msg-big', '', 'fenced', 0, 0, "
            "'2026-10-03T10:00:00.000000Z', '2026-10-03T10:00:00.000000Z', "
            "'2026-10-03T10:00:00.000000Z', 'x', ?, 'cancelled', '2026-10-03T10:00:00.000000Z', "
            "'recorded')",
            (hashlib.sha256(b"ref-big").hexdigest(),),
        )
        conn.execute(
            "INSERT INTO child_receipts(seq, agent, message_id, quota_lease_ref_sha256, outcome, "
            "calls, input_tokens, output_tokens, actual_micro_eur, closed_at) VALUES (1, "
            "'qwen-dev-1', 'msg-big', ?, 'completed', 1, 0, 0, ?, '2026-10-03T10:00:00.000000Z')",
            (hashlib.sha256(b"ref-big").hexdigest(), 10**12 + 1),
        )
    with pytest.raises(ReceiptPageRefused, match="money"):
        ledger.child_receipts_page(issuer_token=fx.ISSUER)
    assert _receipts(ledger)[0][8] == 10**12 + 1  # the evidence stays as it was


# --- 3.4 page types: what a reader refuses -----------------------------------------------------


def _good_page() -> dict:
    return {
        "after_seq": 0, "envelope_version": 1, "generation": fx.GENERATION, "has_more": False,
        "next_seq": 2,
        "receipts": [
            {"actual_micro_eur": 450, "calls": 2, "closed_at": "2026-10-03T10:00:00.000000Z",
             "input_tokens": 1200, "outcome": "completed", "output_tokens": 300,
             "quota_lease_ref_sha256": "a" * 64, "seq": 1},
            {"actual_micro_eur": 120, "calls": 1, "closed_at": "2026-10-03T10:05:00.000000Z",
             "input_tokens": None, "outcome": "failed", "output_tokens": None,
             "quota_lease_ref_sha256": "b" * 64, "seq": 2},
        ],
    }


def _mutated(change):
    page = _good_page()
    change(page)
    return page


BAD_PAGES = {
    "gap": lambda p: p["receipts"][1].update(seq=3) or p.update(next_seq=3),
    "repeat": lambda p: p["receipts"][1].update(seq=1) or p.update(next_seq=1),
    "extra page key": lambda p: p.update(extra=1),
    "extra receipt key": lambda p: p["receipts"][0].update(agent="x"),
    "missing receipt key": lambda p: p["receipts"][0].pop("calls"),
    "version 2": lambda p: p.update(envelope_version=2),
    "other after_seq": lambda p: p.update(after_seq=1),
    "negative token": lambda p: p["receipts"][0].update(input_tokens=-1),
    "zero calls with null token": lambda p: p["receipts"][0].update(
        calls=0, input_tokens=None, output_tokens=0, actual_micro_eur=0),
    "zero calls with money": lambda p: p["receipts"][0].update(
        calls=0, input_tokens=0, output_tokens=0, actual_micro_eur=1),
    "boolean calls": lambda p: p["receipts"][0].update(calls=True),
    "float money": lambda p: p["receipts"][0].update(actual_micro_eur=1.0),
    "money over bound": lambda p: p["receipts"][0].update(actual_micro_eur=10**12 + 1),
    "calls over bound": lambda p: p["receipts"][0].update(calls=1_000_001),
    "empty with has_more": lambda p: p.update(receipts=[], next_seq=0, has_more=True),
    "wrong next_seq": lambda p: p.update(next_seq=1),
    "short generation": lambda p: p.update(generation="0123"),
    "upper-case hash": lambda p: p["receipts"][0].update(quota_lease_ref_sha256="A" * 64),
    "bad outcome": lambda p: p["receipts"][0].update(outcome="done"),
    "not a calendar time": lambda p: p["receipts"][0].update(closed_at="2026-02-30T10:00:00.000000Z"),
    "other time format": lambda p: p["receipts"][0].update(closed_at="2026-10-03T10:00:00Z"),
    "boolean has_more": lambda p: p.update(has_more=0),
}


@pytest.mark.parametrize("name", sorted(BAD_PAGES))
def test_a_reader_refuses_a_page_that_breaks_a_rule(name):
    page = _mutated(BAD_PAGES[name])
    with pytest.raises(ReceiptPageRefused):
        gateway.check_receipt_page(page, after_seq=0, limit=100)


def test_a_reader_refuses_more_rows_than_the_limit_duplicate_keys_and_non_finite_numbers():
    gateway.check_receipt_page(_good_page(), after_seq=0, limit=100)
    with pytest.raises(ReceiptPageRefused, match="limit"):
        gateway.check_receipt_page(_good_page(), after_seq=0, limit=1)
    text = json.dumps(_good_page(), sort_keys=True, separators=(",", ":"))
    duplicate = text.replace('"has_more":false', '"has_more":false,"has_more":false')
    with pytest.raises(ReceiptPageRefused, match="duplicate"):
        gateway.parse_receipt_page(duplicate, after_seq=0, limit=100)
    for constant in ("NaN", "Infinity", "-Infinity"):
        with pytest.raises(ReceiptPageRefused):
            gateway.parse_receipt_page(text.replace('"actual_micro_eur":450', f'"actual_micro_eur":{constant}'),
                                       after_seq=0, limit=100)
    with pytest.raises(ReceiptPageRefused, match="not JSON"):
        gateway.parse_receipt_page(text + "{}", after_seq=0, limit=100)
    empty = {"after_seq": 0, "envelope_version": 1, "generation": fx.GENERATION,
             "has_more": False, "next_seq": 0, "receipts": []}
    gateway.check_receipt_page(empty, after_seq=0, limit=100)


# --- 3.5 and 3.6: per-row caps, the reference, the flag ----------------------------------------


def test_null_caps_mean_the_ledger_ceilings_and_a_missing_ttl_24_hours(tmp_path):
    ledger = fx.make_ledger(tmp_path)
    credential = fx.open_bound(ledger, "msg-a")
    turn = _turn(ledger, "msg-a")
    assert (turn["max_calls"], turn["max_micro_eur"]) == (
        gateway.CHILD_TURN_MAX_CALLS, gateway.TRIAL_CUTOFF_MICRO_EUR)
    assert credential.expires_at == "2026-10-04T10:00:00.000000Z"
    assert turn["quota_lease_ref_sha256"] == REF_HASH


@pytest.mark.parametrize("caps, word", [
    ({"max_calls": gateway.CHILD_TURN_MAX_CALLS + 1}, "max_calls is above"),
    ({"max_micro_eur": gateway.TRIAL_CUTOFF_MICRO_EUR + 1}, "max_micro_eur is above"),
    ({"ttl_seconds": gateway.CHILD_TURN_MAX_SECONDS + 1}, "ttl_seconds is above"),
    ({"max_calls": 0}, "positive"),
    ({"max_micro_eur": -5}, "positive"),
    ({"ttl_seconds": True}, "positive"),
    ({"max_calls": 2.0}, "positive"),
])
def test_caps_above_a_ceiling_or_out_of_range_are_refused(tmp_path, caps, word):
    ledger = fx.make_ledger(tmp_path)
    with pytest.raises(ChildTurnCapBlocked, match=word):
        fx.open_bound(ledger, "msg-a", **caps)
    assert fx.rows(ledger, "SELECT COUNT(*) FROM child_turns") == [(0,)]


def test_the_ceiling_is_the_ledgers_own_pinned_value(tmp_path):
    ledger = fx.make_ledger(tmp_path, child_turn_max_micro_eur=500_000)
    with pytest.raises(ChildTurnCapBlocked, match="max_micro_eur is above"):
        fx.open_bound(ledger, "msg-a", max_micro_eur=500_001)
    fx.open_bound(ledger, "msg-a", max_micro_eur=500_000)


@pytest.mark.parametrize("cap", [{"max_calls": 5}, {"max_micro_eur": 1_000}, {"ttl_seconds": 600}])
def test_caps_without_a_reference_are_refused(tmp_path, cap):
    ledger = fx.make_ledger(tmp_path)
    with pytest.raises(ChildTurnCapBlocked, match="need a quota lease reference"):
        ledger.open_child_turn(agent="qwen-dev-1", message_id="msg-a", issuer_token=fx.ISSUER,
                               **cap)
    assert fx.rows(ledger, "SELECT COUNT(*) FROM child_turns") == [(0,)]


@pytest.mark.parametrize("reference", ["", "x" * 129, "has space", "bad/slash", "é", 7])
def test_a_malformed_reference_is_refused_without_echoing_it(tmp_path, reference):
    ledger = fx.make_ledger(tmp_path)
    with pytest.raises(ChildTurnCapBlocked) as refused:
        fx.open_bound(ledger, "msg-a", reference=reference)
    assert str(reference) not in str(refused.value) or reference == ""


def test_caps_and_the_binding_never_change_after_open(tmp_path):
    ledger = fx.make_ledger(tmp_path)
    fx.open_bound(ledger, "msg-a", max_calls=5)
    with sqlite3.connect(ledger.db_path) as conn:
        for column, value in (("max_calls", 6), ("max_micro_eur", 1), ("expires_at", "x"),
                              ("opened_at", "x"), ("request_id", "x"),
                              ("quota_lease_ref_sha256", "c" * 64), ("agent", "x"),
                              ("message_id", "x")):
            with pytest.raises(sqlite3.DatabaseError, match="immutable"):
                conn.execute(f"UPDATE child_turns SET {column}=?", (value,))  # noqa: S608 - fixed names


def test_a_retry_must_bring_the_same_binding(tmp_path):
    ledger = fx.make_ledger(tmp_path)
    fx.open_bound(ledger, "msg-a", max_calls=5, ttl_seconds=600)
    fx.open_bound(ledger, "msg-a", max_calls=5, ttl_seconds=600)  # the same: a new capability
    for refusal in (
        lambda: fx.open_unbound(ledger, "msg-a"),
        lambda: fx.open_bound(ledger, "msg-a", max_calls=6, ttl_seconds=600),
        lambda: fx.open_bound(ledger, "msg-a", max_calls=5),
        lambda: fx.open_bound(ledger, "msg-a", fx.OTHER_REFERENCE, max_calls=5, ttl_seconds=600),
    ):
        with pytest.raises(ChildTurnCapBlocked):
            refusal()
    fx.open_unbound(ledger, "msg-b")
    with pytest.raises(ChildTurnCapBlocked, match="cannot bind an already opened unbound"):
        fx.open_bound(ledger, "msg-b", fx.OTHER_REFERENCE)


def test_one_reference_binds_at_most_one_child_turn(tmp_path):
    ledger = fx.make_ledger(tmp_path)
    fx.open_bound(ledger, "msg-a")
    with pytest.raises(ChildTurnCapBlocked, match="already binds another"):
        fx.open_bound(ledger, "msg-b")
    with pytest.raises(ChildTurnCapBlocked, match="already binds another"):
        fx.close_bound(ledger, "msg-never", outcome="cancelled")


def test_with_the_flag_on_an_unbound_open_waits_and_a_bound_one_runs(tmp_path):
    ledger = fx.make_ledger(tmp_path)
    fx.open_unbound(ledger, "msg-old")
    fx.open_bound(ledger, "msg-bound")
    assert ledger.set_quota_lease_binding_required(required=True, issuer_token=fx.ISSUER) == {
        "quota_lease_binding_required": True, "changed": True}
    assert issubclass(QuotaLeaseBindingRequired, LedgerHold)  # today's wrapper parks and retries
    with pytest.raises(QuotaLeaseBindingRequired):
        fx.open_unbound(ledger, "msg-new")
    with pytest.raises(QuotaLeaseBindingRequired):
        fx.open_unbound(ledger, "msg-old")  # an existing unbound row is refused too
    fx.open_bound(ledger, "msg-bound")  # a bound retry keeps its binding
    fx.open_bound(ledger, "msg-new-bound", fx.OTHER_REFERENCE)
    assert ledger.set_quota_lease_binding_required(required=False, issuer_token=fx.ISSUER)[
        "changed"] is True
    fx.open_unbound(ledger, "msg-new")
    with pytest.raises(ChildTurnCapBlocked, match="issuer"):
        ledger.set_quota_lease_binding_required(required=True, issuer_token="atgw-" + "x" * 43)


def test_the_flag_never_lifts_the_caps_of_a_bound_turn(tmp_path):
    ledger = fx.make_ledger(tmp_path)
    credential = _bound_with_one_settled_call(ledger, max_calls=1)
    for required in (True, False):
        ledger.set_quota_lease_binding_required(required=required, issuer_token=fx.ISSUER)
        with pytest.raises(ChildTurnCapExceeded, match="call ceiling"):
            ledger.reserve_for_child(fx.attempt(9), capability=credential.token)


# --- 3.7: the tombstone and the close API ------------------------------------------------------


def test_a_close_with_a_reference_for_a_never_opened_key_fences_it_with_a_zero_receipt(tmp_path):
    ledger = fx.make_ledger(tmp_path)
    assert fx.close_bound(ledger, "msg-a", outcome="cancelled") == "fenced"
    turn = _turn(ledger, "msg-a")
    assert (turn["state"], turn["max_calls"], turn["max_micro_eur"]) == ("fenced", 0, 0)
    assert turn["expires_at"] == turn["opened_at"]
    assert _receipts(ledger)[0][4:9] == ("cancelled", 0, 0, 0, 0)
    for again in (lambda: fx.open_bound(ledger, "msg-a"), lambda: fx.open_unbound(ledger, "msg-a")):
        with pytest.raises(ChildTurnCapExceeded, match="fenced"):
            again()
    assert fx.close_bound(ledger, "msg-a", outcome="failed") == "already_terminal"
    assert len(_receipts(ledger)) == 1
    assert ledger.status()["active_child_turns"] == []  # a fence never opened


def test_the_fence_and_its_receipt_are_one_transaction(tmp_path, monkeypatch):
    ledger = fx.make_ledger(tmp_path)
    before = fx.dump(ledger)

    def fault(*_args, **_kwargs):
        raise RuntimeError("injected fault after the fence insert")

    monkeypatch.setattr(gateway.SpendLedger, "_ensure_custody", fault)
    with pytest.raises(RuntimeError):
        fx.close_bound(ledger, "msg-a", outcome="cancelled")
    assert fx.dump(ledger) == before


def test_a_close_without_a_reference_on_a_never_opened_key_is_still_a_no_op(tmp_path):
    ledger = fx.make_ledger(tmp_path)
    assert ledger.close_child_turn(agent="qwen-dev-1", message_id="msg-a", reason="dead_letter",
                                   issuer_token=fx.ISSUER) is None
    assert ledger.close_child_turn(agent="qwen-dev-1", message_id="msg-a", reason="dead_letter",
                                   issuer_token=fx.ISSUER, outcome="failed") == "not_opened"
    assert fx.rows(ledger, "SELECT COUNT(*) FROM child_turns") == [(0,)]


def test_a_fence_needs_an_explicit_outcome(tmp_path):
    ledger = fx.make_ledger(tmp_path)
    with pytest.raises(ChildTurnCapBlocked, match="explicit outcome"):
        ledger.close_child_turn(agent="qwen-dev-1", message_id="msg-a", reason="x",
                                issuer_token=fx.ISSUER, quota_lease_ref=fx.REFERENCE)


def test_todays_close_call_on_an_unbound_row_is_unchanged(tmp_path):
    ledger = fx.make_ledger(tmp_path)
    fx.open_unbound(ledger, "msg-a")
    assert ledger.close_child_turn(agent="qwen-dev-1", message_id="msg-a", reason="dead_letter",
                                   issuer_token=fx.ISSUER) is None
    turn = _turn(ledger, "msg-a")
    assert (turn["state"], turn["reason"]) == ("expired", "dead_letter")
    assert turn["terminal_outcome"] == "cancelled"  # the internal ending only
    assert _receipts(ledger) == []


def test_a_bound_row_closes_only_with_its_reference_and_an_outcome(tmp_path):
    ledger = fx.make_ledger(tmp_path)
    fx.open_bound(ledger, "msg-a")
    refusals = (
        {},
        {"outcome": "completed"},
        {"quota_lease_ref": fx.REFERENCE},
    )
    for extra in refusals:
        with pytest.raises(ChildTurnCapBlocked, match="only with its quota lease reference"):
            ledger.close_child_turn(agent="qwen-dev-1", message_id="msg-a", reason="x",
                                    issuer_token=fx.ISSUER, **extra)
    fx.close_bound(ledger, "msg-a")
    for extra in refusals:  # even when it already ended
        with pytest.raises(ChildTurnCapBlocked, match="only with its quota lease reference"):
            ledger.close_child_turn(agent="qwen-dev-1", message_id="msg-a", reason="x",
                                    issuer_token=fx.ISSUER, **extra)


def test_a_different_reference_is_reference_mismatch_and_never_echoed(tmp_path):
    ledger = fx.make_ledger(tmp_path)
    fx.open_bound(ledger, "msg-a")
    with pytest.raises(QuotaLeaseReferenceMismatch) as refused:
        fx.close_bound(ledger, "msg-a", reference=fx.OTHER_REFERENCE)
    assert str(refused.value) == "reference_mismatch"
    assert _turn(ledger, "msg-a")["state"] == "open"


def test_a_reference_on_an_unbound_row_is_ignored(tmp_path):
    ledger = fx.make_ledger(tmp_path)
    fx.open_unbound(ledger, "msg-a")
    assert fx.close_bound(ledger, "msg-a", outcome="completed") == "reference_ignored"
    turn = _turn(ledger, "msg-a")
    assert (turn["state"], turn["terminal_outcome"], turn["quota_lease_ref_sha256"]) == (
        "expired", "completed", None)
    assert _receipts(ledger) == [] and _pending(ledger) == []


def test_an_unknown_outcome_word_is_refused(tmp_path):
    ledger = fx.make_ledger(tmp_path)
    fx.open_bound(ledger, "msg-a")
    with pytest.raises(ValueError, match="closed word"):
        fx.close_bound(ledger, "msg-a", outcome="done")


# --- 3.8: status, read in one snapshot ---------------------------------------------------------


def test_status_gains_exactly_five_keys_on_schema4(tmp_path):
    schema3 = fx.make_schema3_ledger(tmp_path / "three").status()
    schema4 = fx.make_ledger(tmp_path / "four").status()
    assert set(schema4) == set(schema3) | REPORT_KEYS
    assert schema4["child_receipt_report_version"] == 1


def test_a_status_snapshot_never_mixes_a_receipt_with_missing_cost(tmp_path, monkeypatch):
    clock = fx.Clock()
    ledger = fx.make_ledger(tmp_path, clock)
    credential = fx.open_bound(ledger, "msg-a")
    ledger.reserve_for_child(fx.attempt(1), capability=credential.token)
    fx.close_bound(ledger, "msg-a")  # pending: the attempt is unresolved
    real_report = gateway.SpendLedger._receipt_report
    seen = {}

    def report_while_a_writer_tries(conn):
        # a second connection tries to settle (charge and receipt) inside this snapshot
        writer = gateway.SpendLedger(ledger.db_path, ledger.marker_path, now=clock,
                                     busy_timeout_seconds=0.2)
        try:
            fx.settle(writer, fx.attempt(1))
            seen["writer"] = "committed"
        except LedgerBlocked:
            seen["writer"] = "blocked"
        return real_report(conn)

    monkeypatch.setattr(gateway.SpendLedger, "_receipt_report", staticmethod(report_while_a_writer_tries))
    before = ledger.status()
    assert seen["writer"] == "blocked"  # no write can land inside one snapshot
    assert (before["child_receipts_through_seq"], before["current_committed_micro_eur"]) == (0, 0)
    monkeypatch.undo()
    fx.settle(ledger, fx.attempt(1))  # the writer's turn, after the snapshot
    after = ledger.status()
    assert (after["child_receipts_through_seq"], after["current_committed_micro_eur"]) == (1, 670)
    assert after["child_receipts_pending"] == 0 and after["child_receipts"] == 1


def test_a_status_snapshot_during_an_uncommitted_receipt_shows_neither(tmp_path, monkeypatch):
    clock = fx.Clock()
    ledger = fx.make_ledger(tmp_path, clock)
    credential = fx.open_bound(ledger, "msg-a")
    ledger.reserve_for_child(fx.attempt(1), capability=credential.token)
    fx.close_bound(ledger, "msg-a")
    reader = gateway.SpendLedger(ledger.db_path, ledger.marker_path, now=clock)
    real_custody = gateway.SpendLedger._ensure_custody
    seen = {}

    def custody_then_read(self, conn, agent, message_id, *, at):
        word = real_custody(self, conn, agent, message_id, at=at)
        if word == "receipt_written":  # the charge and the receipt are written, not committed
            status = reader.status()
            seen["during"] = (status["child_receipts_through_seq"],
                              status["current_committed_micro_eur"])
        return word

    monkeypatch.setattr(gateway.SpendLedger, "_ensure_custody", custody_then_read)
    fx.settle(ledger, fx.attempt(1))
    assert seen["during"] == (0, 0)
    after = reader.status()
    assert (after["child_receipts_through_seq"], after["current_committed_micro_eur"]) == (1, 670)


def test_status_counts_pending_notes_and_receipts(tmp_path):
    ledger = fx.make_ledger(tmp_path)
    credential = fx.open_bound(ledger, "msg-a")
    ledger.reserve_for_child(fx.attempt(1), capability=credential.token)
    fx.close_bound(ledger, "msg-a")
    status = ledger.status()
    assert (status["child_receipts"], status["child_receipts_pending"]) == (0, 1)


# --- 3.9: the start-up sweep -------------------------------------------------------------------


def test_the_sweep_ends_expired_bound_turns_and_completes_custody(tmp_path):
    clock = fx.Clock()
    ledger = fx.make_ledger(tmp_path, clock)
    expired = fx.open_bound(ledger, "msg-expired", reference="ref-expired", ttl_seconds=60)
    fx.open_bound(ledger, "msg-live", reference="ref-live")
    fx.open_unbound(ledger, "msg-unbound")
    clock.value += timedelta(seconds=61)
    result = ledger.sweep_child_receipts(issuer_token=fx.ISSUER)
    assert result == {"binding_installed": True, "ended": 1, "receipts_written": 1,
                      "pending": 0, "fallback": 0}
    assert _receipts(ledger)[0][2:] == ("msg-expired", hashlib.sha256(b"ref-expired").hexdigest(),
                                        "cancelled", 0, 0, 0, 0, expired.expires_at)
    assert _turn(ledger, "msg-live")["state"] == "open"
    assert _turn(ledger, "msg-unbound")["state"] == "open"
    assert ledger.sweep_child_receipts(issuer_token=fx.ISSUER)["receipts_written"] == 0


# --- AC7: privacy ------------------------------------------------------------------------------


def test_the_reference_never_leaves_the_gateway(tmp_path):
    clock = fx.Clock()
    ledger = fx.make_ledger(tmp_path, clock)
    _bound_with_one_settled_call(ledger)
    fx.close_bound(ledger, "msg-a")
    secret = fx.REFERENCE.encode("ascii")
    assert secret not in ledger.db_path.read_bytes()
    surfaces = [
        json.dumps(ledger.status()),
        json.dumps(ledger.child_receipts_page(issuer_token=fx.ISSUER)),
        json.dumps(ledger.sweep_child_receipts(issuer_token=fx.ISSUER)),
    ]
    for refusal in (
        lambda: fx.close_bound(ledger, "msg-a", reference=fx.OTHER_REFERENCE),
        lambda: fx.open_bound(ledger, "msg-b"),
        lambda: fx.open_bound(ledger, "msg-a", fx.OTHER_REFERENCE),
    ):
        with pytest.raises(gateway.GatewayError) as refused:
            refusal()
        surfaces.append(str(refused.value))
    for text in surfaces:
        assert fx.REFERENCE not in text and fx.OTHER_REFERENCE not in text


# --- the operator commands: binding-install, binding-required, receipts ----------------------


def _default_ledger(tmp_path, *, schema3=False, front_token=fx.ISSUER):
    """A ledger at the default place, which the autouse fixture moved under tmp_path,
    and the front token where the commands read it."""
    from agenttalk.store import Store

    assert Path(gateway.default_ledger_path()).is_relative_to(Path(os.environ["LOCALAPPDATA"]))
    root = tmp_path / "project"
    Store(root).init(["lead"])
    ledger = gateway.SpendLedger(gateway.default_ledger_path(), gateway.default_install_marker_path())
    ledger.initialize(opening_micro_eur=0, opening_evidence=fx.OPENING_EVIDENCE,
                      generation=fx.GENERATION, child_cap_issuer_token=fx.ISSUER)
    if schema3:
        fx.to_schema3(ledger)
    gateway.write_secret_file(gateway.default_front_token_path(), front_token)
    return root, ledger


def test_binding_install_command_migrates_and_a_second_run_changes_nothing(tmp_path, capsys):
    root, ledger = _default_ledger(tmp_path, schema3=True)
    assert cli.main(["--root", str(root), "gateway", "binding-install"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["installed"] is True and first["schema_version"] == 4
    assert first["quota_lease_binding_required"] is False
    assert _schema_version(ledger) == "4"
    assert cli.main(["--root", str(root), "gateway", "binding-install"]) == 0
    assert json.loads(capsys.readouterr().out)["installed"] is False


def test_binding_install_command_refuses_without_the_matching_front_token(tmp_path, capsys):
    root, ledger = _default_ledger(tmp_path, schema3=True, front_token="atgw-" + "x" * 43)
    assert cli.main(["--root", str(root), "gateway", "binding-install"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "atgw-" not in captured.err
    assert _schema_version(ledger) == "3"


def test_binding_required_command_turns_the_flag_on_and_off(tmp_path, capsys):
    root, ledger = _default_ledger(tmp_path)
    assert cli.main(["--root", str(root), "gateway", "binding-required", "--on"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "quota_lease_binding_required": True, "changed": True}
    assert ledger.quota_lease_binding_state(issuer_token=fx.ISSUER)[
        "quota_lease_binding_required"] is True
    assert cli.main(["--root", str(root), "gateway", "binding-required", "--off"]) == 0
    assert json.loads(capsys.readouterr().out)["quota_lease_binding_required"] is False


def test_binding_required_command_is_refused_on_schema3(tmp_path, capsys):
    root, _ledger = _default_ledger(tmp_path, schema3=True)
    assert cli.main(["--root", str(root), "gateway", "binding-required", "--on"]) == 2
    assert "not installed" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [
    ["binding-required"],
    ["binding-required", "--on", "--off"],
    ["binding-install", "--issuer-token", fx.ISSUER],
    ["binding-required", "--on", "--token", fx.ISSUER],
])
def test_the_command_parser_needs_one_flag_word_and_takes_no_token(tmp_path, capsys, argv):
    with pytest.raises(SystemExit) as refused:
        cli.main(["--root", str(tmp_path), "gateway", *argv])
    assert refused.value.code == 2


def test_receipts_command_prints_exactly_the_page(tmp_path, capsys):
    root, ledger = _default_ledger(tmp_path)
    fx.close_bound(ledger, "msg-a", outcome="cancelled")
    assert cli.main(["--root", str(root), "gateway", "receipts", "--after", "0", "--json"]) == 0
    out = capsys.readouterr().out
    page = json.loads(out)
    assert out == json.dumps(page, sort_keys=True, separators=(",", ":")) + "\n"
    assert page == ledger.child_receipts_page(issuer_token=fx.ISSUER)
    assert [r["seq"] for r in page["receipts"]] == [1]
    assert "msg-a" not in out and "qwen-dev-1" not in out and fx.REFERENCE not in out


@pytest.mark.parametrize("args", [
    ["--after", "0"],                          # --json is required
    ["--json"],                                # --after is required
    ["--after", "-1", "--json"],
    ["--after", "x", "--json"],
    ["--after", "0", "--limit", "0", "--json"],
    ["--after", "0", "--limit", "1001", "--json"],
    ["--after", "0", "--limit", "+5", "--json"],
    ["--after", "9223372036854775808", "--json"],
])
def test_receipts_command_refuses_a_bad_request_with_one_word(tmp_path, capsys, args):
    root, _ledger = _default_ledger(tmp_path)
    assert cli.main(["--root", str(root), "gateway", "receipts", *args]) == 2
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "bad_request\n")


def test_receipts_command_refuses_a_damaged_page_with_one_word(tmp_path, capsys):
    root, ledger = _default_ledger(tmp_path)
    fx.close_bound(ledger, "msg-a", outcome="cancelled")
    with sqlite3.connect(ledger.db_path) as conn:
        conn.execute(
            "INSERT INTO child_receipts(seq, agent, message_id, quota_lease_ref_sha256, outcome, "
            "calls, input_tokens, output_tokens, actual_micro_eur, closed_at) VALUES (5, 'x', 'y', "
            "?, 'cancelled', 0, 0, 0, 0, '2026-10-03T10:00:00.000000Z')",
            ("d" * 64,),
        )
    assert cli.main(["--root", str(root), "gateway", "receipts", "--after", "0", "--json"]) == 2
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "receipt_page_refused\n")


@pytest.mark.parametrize("setup", [{"schema3": True}, {"front_token": "atgw-" + "x" * 43}])
def test_receipts_command_says_unavailable_on_schema3_or_a_wrong_token(tmp_path, capsys, setup):
    root, _ledger = _default_ledger(tmp_path, **setup)
    assert cli.main(["--root", str(root), "gateway", "receipts", "--after", "0", "--json"]) == 2
    assert capsys.readouterr() == ("", "receipts_unavailable\n")
