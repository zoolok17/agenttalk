"""The ledger-only report (`agenttalk gateway report`) and a receipt's charge period.

A report reader computes, per month, the spend that no quota lease owns: the month's
committed money, less the receipts the report covers whose charge period is that
month, less the bound money not yet on a receipt. The five cases below prove that each
micro-euro is counted exactly once, with synthetic figures. Then: the receipt-order
guarantee, one snapshot, nothing written, no names, the closed shape, and the command
run from a folder with no project, in a bare environment.

All ledgers here are temporary files; nothing touches a real gateway."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import gateway_binding_fixtures as fx
import pytest

from agenttalk import cli
from agenttalk import ovh_gateway as gateway
from agenttalk.ovh_gateway import GatewayReportRefused, LedgerBlocked, LedgerHold

SEPTEMBER, OCTOBER = "2026-09", "2026-10"
ONE_CALL = gateway.settlement_cost_micro_eur(1_000, 100)
RESERVED = gateway.reservation_cost_micro_eur()
# settlement over the reservation: the attempt stays uncertain with its actual
OVER_TOKENS = (gateway.MAX_CONTEXT_TOKENS * 3, 100)
OVER_ACTUAL = gateway.settlement_cost_micro_eur(*OVER_TOKENS)
# too many output tokens, but a cost under the reservation
OVERRUN_TOKENS = (1_000, gateway.MAX_OUTPUT_TOKENS + 1)
OVERRUN_ACTUAL = gateway.settlement_cost_micro_eur(*OVERRUN_TOKENS)


@pytest.fixture(autouse=True)
def _never_reach_the_live_ledger(tmp_path_factory, monkeypatch) -> None:
    # A bare SpendLedger() resolves to the host's real ledger via LOCALAPPDATA.
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path_factory.mktemp("no-live-appdata")))


def test_the_synthetic_figures_have_the_shape_each_case_needs():
    assert OVER_ACTUAL > RESERVED > OVERRUN_ACTUAL > ONE_CALL > 0


# --- a reader, as the report's contract describes it --------------------------------------------


def _pages(ledger) -> list[dict]:
    """Every receipt page from the start, one receipt per page."""
    pages, after = [], 0
    while True:
        page = ledger.child_receipts_page(after_seq=after, limit=1, issuer_token=fx.ISSUER)
        pages.append(page)
        if not page["has_more"]:
            return pages
        after = page["next_seq"]


def _unowned(report: dict, pages: list[dict], month: str) -> int:
    """Spend no lease owns in `month`, from one report and the receipt pages: only
    receipts numbered up to the report's own through_seq, of the report's generation."""
    committed = sum(row["committed_micro_eur"] for row in report["periods"] if row["period"] == month)
    covered = 0
    for page in pages:
        assert page["generation"] == report["generation"]
        covered += sum(
            receipt["actual_micro_eur"]
            for receipt in page["receipts"]
            if receipt["seq"] <= report["child_receipts_through_seq"]
            and receipt["charge_period"] == month
        )
    pending = sum(
        row["micro_eur"] for row in report["unreceipted_bound_actual"] if row["period"] == month
    )
    return committed - covered - pending


def _receipts(pages: list[dict]) -> list[tuple]:
    return [
        (receipt["calls"], receipt["actual_micro_eur"], receipt["charge_period"])
        for page in pages
        for receipt in page["receipts"]
    ]


def _unowned_call(ledger, n: int) -> None:
    """A call that belongs to no lease: no child turn at all."""
    ledger.reserve(fx.attempt(n))
    fx.settle(ledger, fx.attempt(n))


def _child_call(ledger, credential, n: int, tokens: tuple[int, int] = (1_000, 100)) -> None:
    ledger.reserve_for_child(fx.attempt(n), capability=credential.token)
    fx.settle(ledger, fx.attempt(n), *tokens)


# --- the five cases ----------------------------------------------------------------------------


def test_case_1_a_same_month_turn_moves_its_money_from_unreceipted_to_its_receipt(tmp_path):
    ledger = fx.make_ledger(tmp_path)
    _unowned_call(ledger, 1)
    credential = fx.open_bound(ledger, "msg-a")
    _child_call(ledger, credential, 2)
    first = ledger.report()
    assert first["unreceipted_bound_actual"] == [{"period": OCTOBER, "micro_eur": ONE_CALL}]
    assert _unowned(first, _pages(ledger), OCTOBER) == ONE_CALL
    _child_call(ledger, credential, 3)
    second = ledger.report()
    assert second["unreceipted_bound_actual"] == [{"period": OCTOBER, "micro_eur": 2 * ONE_CALL}]
    assert _unowned(second, _pages(ledger), OCTOBER) == ONE_CALL
    fx.close_bound(ledger, "msg-a")
    ended, pages = ledger.report(), _pages(ledger)
    assert ended["unreceipted_bound_actual"] == []
    assert ended["child_receipts_through_seq"] == 1
    assert _receipts(pages) == [(2, 2 * ONE_CALL, OCTOBER)]
    assert _unowned(ended, pages, OCTOBER) == ONE_CALL


def test_case_2_a_turn_across_two_months_counts_each_part_once_in_its_own_month(tmp_path):
    clock = fx.Clock(datetime(2026, 9, 30, 22, tzinfo=timezone.utc))
    ledger = fx.make_ledger(tmp_path, clock)
    credential = fx.open_bound(ledger, "msg-a")
    clock.value = datetime(2026, 9, 30, 23, tzinfo=timezone.utc)
    _child_call(ledger, credential, 1)
    clock.value = datetime(2026, 10, 1, 0, 30, tzinfo=timezone.utc)
    _unowned_call(ledger, 2)
    _child_call(ledger, credential, 3)
    running = ledger.report()
    assert running["unreceipted_bound_actual"] == [
        {"period": SEPTEMBER, "micro_eur": ONE_CALL},
        {"period": OCTOBER, "micro_eur": ONE_CALL},
    ]
    pages = _pages(ledger)
    assert (_unowned(running, pages, SEPTEMBER), _unowned(running, pages, OCTOBER)) == (0, ONE_CALL)
    fx.close_bound(ledger, "msg-a")
    ended, pages = ledger.report(), _pages(ledger)
    # the receipt cannot be split, so it names no month: each part stays unowned in its own
    assert _receipts(pages) == [(2, 2 * ONE_CALL, None)]
    assert ended["unreceipted_bound_actual"] == []
    assert (_unowned(ended, pages, SEPTEMBER), _unowned(ended, pages, OCTOBER)) == (
        ONE_CALL, 2 * ONE_CALL)
    committed = {row["period"]: row["committed_micro_eur"] for row in ended["periods"]}
    assert committed == {SEPTEMBER: ONE_CALL, OCTOBER: 2 * ONE_CALL}


def test_case_3_a_receipt_with_no_charge_names_no_month_and_changes_nothing(tmp_path):
    ledger = fx.make_ledger(tmp_path)
    _unowned_call(ledger, 1)
    fx.open_bound(ledger, "msg-empty", reference="ref-empty")
    fx.close_bound(ledger, "msg-empty", reference="ref-empty", outcome="cancelled")
    credential = fx.open_bound(ledger, "msg-no-send", reference="ref-no-send")
    ledger.reserve_for_child(fx.attempt(2), capability=credential.token)
    ledger.reconcile(fx.attempt(2), outcome="no-send", reason="never sent")
    fx.close_bound(ledger, "msg-no-send", reference="ref-no-send", outcome="failed")
    fx.close_bound(ledger, "msg-fence", reference="ref-fence", outcome="cancelled")  # never opened
    report, pages = ledger.report(), _pages(ledger)
    assert _receipts(pages) == [(0, 0, None), (0, 0, None), (0, 0, None)]
    assert report["unreceipted_bound_actual"] == []
    assert _unowned(report, pages, OCTOBER) == ONE_CALL


@pytest.mark.parametrize("tokens, actual, increment", [
    (OVER_TOKENS, OVER_ACTUAL, 0),
    (OVERRUN_TOKENS, OVERRUN_ACTUAL, RESERVED - OVERRUN_ACTUAL),
], ids=["cost-over-the-reservation", "tokens-over-the-limit"])
def test_case_4_an_uncertain_charged_attempt_is_counted_once(tmp_path, tokens, actual, increment):
    ledger = fx.make_ledger(tmp_path)
    _unowned_call(ledger, 1)
    credential = fx.open_bound(ledger, "msg-a")
    _child_call(ledger, credential, 2, tokens)
    held = ledger.report()
    assert held["unresolved"] == [
        {"state": "uncertain", "reserved_micro_eur": RESERVED, "actual_micro_eur": actual,
         "period": OCTOBER},
    ]
    assert (held["service_hold"], held["service_hold_reason"]) == (True, "attempt_over_reservation")
    assert held["unreceipted_bound_actual"] == [{"period": OCTOBER, "micro_eur": actual}]
    assert _unowned(held, _pages(ledger), OCTOBER) == ONE_CALL
    fx.close_bound(ledger, "msg-a")  # the receipt waits for the attempt
    pending = ledger.report()
    assert (pending["child_receipts_pending"], pending["child_receipts_through_seq"]) == (1, 0)
    assert _unowned(pending, _pages(ledger), OCTOBER) == ONE_CALL
    ledger.reconcile(fx.attempt(2), outcome="charge-reserve", reason="operator")
    done, pages = ledger.report(), _pages(ledger)
    assert done["unresolved"] == [] and done["service_hold"] is False
    assert done["service_hold_reason"] is None
    # the reconciliation raised the attempt and its month by the same increment
    assert done["periods"] == [{"period": OCTOBER, "committed_micro_eur": ONE_CALL + actual + increment}]
    assert _receipts(pages) == [(1, actual + increment, OCTOBER)]
    assert _unowned(done, pages, OCTOBER) == ONE_CALL


def test_case_5_a_charge_between_the_report_and_the_page_read_waits_for_the_next_report(tmp_path):
    ledger = fx.make_ledger(tmp_path)
    _unowned_call(ledger, 1)
    credential = fx.open_bound(ledger, "msg-a")
    _child_call(ledger, credential, 2)
    report = ledger.report()
    assert report["child_receipts_through_seq"] == 0
    _child_call(ledger, credential, 3)  # after the report: one more call, and the turn ends
    fx.close_bound(ledger, "msg-a")
    pages = _pages(ledger)
    assert _receipts(pages) == [(2, 2 * ONE_CALL, OCTOBER)]
    assert _unowned(report, pages, OCTOBER) == ONE_CALL  # the receipt is past this report
    every_receipt = sum(receipt[1] for receipt in _receipts(pages))
    committed = report["periods"][0]["committed_micro_eur"]
    pending = report["unreceipted_bound_actual"][0]["micro_eur"]
    assert committed - every_receipt - pending == ONE_CALL - 2 * ONE_CALL  # counted twice
    later = ledger.report()
    assert later["child_receipts_through_seq"] == 1
    assert _unowned(later, pages, OCTOBER) == ONE_CALL


def test_a_call_of_an_unbound_child_turn_is_spend_no_lease_owns(tmp_path):
    ledger = fx.make_ledger(tmp_path)
    _child_call(ledger, fx.open_unbound(ledger, "msg-unbound"), 1)
    report = ledger.report()
    assert report["unreceipted_bound_actual"] == []
    assert _unowned(report, _pages(ledger), OCTOBER) == ONE_CALL


def test_an_uncertain_attempt_without_a_recorded_cost_is_not_yet_money(tmp_path):
    ledger = fx.make_ledger(tmp_path)
    credential = fx.open_bound(ledger, "msg-a")
    ledger.reserve_for_child(fx.attempt(1), capability=credential.token)
    ledger.mark_uncertain(fx.attempt(1), reason="usage lost")
    report = ledger.report()
    assert report["unresolved"] == [
        {"state": "uncertain", "reserved_micro_eur": RESERVED, "actual_micro_eur": None,
         "period": OCTOBER},
    ]
    assert report["unreceipted_bound_actual"] == []
    assert report["periods"] == [{"period": OCTOBER, "committed_micro_eur": 0}]


# --- the charge period -------------------------------------------------------------------------


def test_a_no_send_attempt_in_another_month_is_no_charge(tmp_path):
    clock = fx.Clock(datetime(2026, 9, 30, 22, tzinfo=timezone.utc))
    ledger = fx.make_ledger(tmp_path, clock)
    credential = fx.open_bound(ledger, "msg-a")
    ledger.reserve_for_child(fx.attempt(1), capability=credential.token)
    ledger.reconcile(fx.attempt(1), outcome="no-send", reason="never sent")
    clock.value = datetime(2026, 10, 1, 0, 30, tzinfo=timezone.utc)
    _child_call(ledger, credential, 2)
    fx.close_bound(ledger, "msg-a")
    assert _receipts(_pages(ledger)) == [(1, ONE_CALL, OCTOBER)]


def test_the_charge_period_is_derived_never_stored(tmp_path):
    ledger = fx.make_ledger(tmp_path)
    columns = [row[1] for row in fx.rows(ledger, "PRAGMA table_info(child_receipts)")]
    assert columns == [
        "seq", "agent", "message_id", "quota_lease_ref_sha256", "outcome", "calls",
        "input_tokens", "output_tokens", "actual_micro_eur", "closed_at",
    ]


# --- the receipt-order guarantee and the one snapshot ---------------------------------------------


def test_a_receipt_written_after_a_report_is_numbered_above_its_through_seq(tmp_path):
    ledger = fx.make_ledger(tmp_path)
    for n, message_id in ((1, "msg-a"), (2, "msg-b")):
        credential = fx.open_bound(ledger, message_id, reference=f"ref-{n}")
        _child_call(ledger, credential, n)
        fx.close_bound(ledger, message_id, reference=f"ref-{n}")
    report = ledger.report()
    covered = ledger.child_receipts_page(after_seq=0, limit=100, issuer_token=fx.ISSUER)["receipts"]
    for n, message_id in ((3, "msg-c"), (4, "msg-d")):
        credential = fx.open_bound(ledger, message_id, reference=f"ref-{n}")
        _child_call(ledger, credential, n)
        fx.close_bound(ledger, message_id, reference=f"ref-{n}")
    after = ledger.child_receipts_page(after_seq=0, limit=100, issuer_token=fx.ISSUER)["receipts"]
    through = report["child_receipts_through_seq"]
    assert through == 2
    assert [receipt for receipt in after if receipt["seq"] <= through] == covered
    assert [receipt["seq"] for receipt in after if receipt["seq"] > through] == [3, 4]


def test_no_write_lands_inside_one_report_snapshot(tmp_path, monkeypatch):
    clock = fx.Clock()
    ledger = fx.make_ledger(tmp_path, clock)
    credential = fx.open_bound(ledger, "msg-a")
    ledger.reserve_for_child(fx.attempt(1), capability=credential.token)
    fx.close_bound(ledger, "msg-a")  # pending: the attempt is unresolved
    real = gateway.SpendLedger._unreceipted_bound_actual
    seen = {}

    def read_while_a_writer_tries(conn):
        # a second connection tries to settle (the charge and the receipt) mid-snapshot
        writer = gateway.SpendLedger(ledger.db_path, ledger.marker_path, now=clock,
                                     busy_timeout_seconds=0.2)
        try:
            fx.settle(writer, fx.attempt(1))
            seen["writer"] = "committed"
        except LedgerBlocked:
            seen["writer"] = "blocked"
        return real(conn)

    monkeypatch.setattr(gateway.SpendLedger, "_unreceipted_bound_actual",
                        staticmethod(read_while_a_writer_tries))
    during = ledger.report()
    assert seen["writer"] == "blocked"
    assert (during["child_receipts_through_seq"], during["child_receipts_pending"]) == (0, 1)
    assert during["periods"] == [{"period": OCTOBER, "committed_micro_eur": 0}]
    monkeypatch.undo()
    fx.settle(ledger, fx.attempt(1))
    after = ledger.report()
    assert (after["child_receipts_through_seq"], after["child_receipts_pending"]) == (1, 0)
    assert after["periods"] == [{"period": OCTOBER, "committed_micro_eur": ONE_CALL}]
    assert after["unreceipted_bound_actual"] == []


def test_a_report_during_an_uncommitted_receipt_sees_neither_the_charge_nor_the_receipt(
        tmp_path, monkeypatch):
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
            report = reader.report()
            seen["during"] = (report["child_receipts_through_seq"], report["periods"],
                              report["unreceipted_bound_actual"])
        return word

    monkeypatch.setattr(gateway.SpendLedger, "_ensure_custody", custody_then_read)
    fx.settle(ledger, fx.attempt(1))
    assert seen["during"] == (0, [{"period": OCTOBER, "committed_micro_eur": 0}], [])


def test_the_report_writes_nothing_and_never_ends_a_turn(tmp_path):
    clock = fx.Clock()
    ledger = fx.make_ledger(tmp_path, clock)
    credential = fx.open_bound(ledger, "msg-a")
    _child_call(ledger, credential, 1)
    clock.value = fx.START + timedelta(days=2)  # past the turn's 24 hours
    before = fx.dump(ledger)
    report = ledger.report()
    assert fx.dump(ledger) == before
    assert (report["open_child_turns"], report["open_child_turns_expired"]) == (1, 1)
    assert report["earliest_open_expiry"] == "2026-10-04T10:00:00.000000Z"
    assert report["observed_at"] == "2026-10-05T10:00:00.000000Z"
    assert report["unreceipted_bound_actual"] == [{"period": OCTOBER, "micro_eur": ONE_CALL}]
    assert report["child_receipts_through_seq"] == 0
    assert ledger.report() == report


@pytest.mark.parametrize("offset, expired", [
    (timedelta(microseconds=-1), 0), (timedelta(0), 1),
], ids=["just-before", "at-expiry"])
def test_a_turn_counts_as_expired_from_its_expiry_time_on(tmp_path, offset, expired):
    clock = fx.Clock()
    ledger = fx.make_ledger(tmp_path, clock)
    fx.open_bound(ledger, "msg-a", ttl_seconds=60)
    clock.value = fx.START + timedelta(seconds=60) + offset
    report = ledger.report()
    assert (report["open_child_turns"], report["open_child_turns_expired"]) == (1, expired)


def test_a_ledger_the_status_refuses_gives_no_report(tmp_path):
    clock = fx.Clock()
    ledger = fx.make_ledger(tmp_path, clock)
    _unowned_call(ledger, 1)
    clock.value = fx.START - timedelta(minutes=1)
    with pytest.raises(LedgerHold):
        ledger.status()
    with pytest.raises(LedgerHold):
        ledger.report()


# --- stored money is read as stored, never converted ---------------------------------------------
#
# SQLite's INTEGER affinity keeps a fractional REAL, a non-numeric TEXT or an
# integer out of the report's bounds as it is; int() would have turned 1.5 into 1.

_A_BOUND, _A_OPEN = fx.attempt(1), fx.attempt(2)
_MONEY_COLUMNS = {
    # source: (raw update, how SQLite stored it, which row)
    "committed": ("UPDATE periods SET committed_micro_eur=?",
                  "SELECT typeof(committed_micro_eur) FROM periods", ()),
    "reserved": ("UPDATE attempts SET reserved_micro_eur=? WHERE attempt_id=?",
                 "SELECT typeof(reserved_micro_eur) FROM attempts WHERE attempt_id=?", (_A_OPEN,)),
    "actual": ("UPDATE attempts SET actual_micro_eur=? WHERE attempt_id=?",
               "SELECT typeof(actual_micro_eur) FROM attempts WHERE attempt_id=?", (_A_OPEN,)),
    "unreceipted": ("UPDATE attempts SET actual_micro_eur=? WHERE attempt_id=?",
                    "SELECT typeof(actual_micro_eur) FROM attempts WHERE attempt_id=?", (_A_BOUND,)),
}
_MALFORMED_MONEY = [(1.5, "real"), ("seven", "text"), (10**12 + 1, "integer"), (1e20, "real")]
_MALFORMED_IDS = ["fraction", "text", "too-large", "huge-real"]


def _every_money_source(ledger) -> None:
    """One stored amount behind each money figure: a bound open turn with a settled
    call (unreceipted money), and an uncertain attempt of no turn (reserved, actual)."""
    _child_call(ledger, fx.open_bound(ledger, "msg-a"), 1)
    ledger.reserve(_A_OPEN)
    ledger.mark_uncertain(_A_OPEN, reason="usage lost")


def _store(ledger, source: str, value) -> str:
    """Write `value` raw behind one money figure; return SQLite's storage class for it."""
    update, storage, row = _MONEY_COLUMNS[source]
    with sqlite3.connect(ledger.db_path) as conn:
        conn.execute(update, (value, *row))
        stored = conn.execute(storage, row).fetchone()[0]
    return stored


def _figure(report: dict, source: str):
    return {
        "committed": lambda: report["periods"][0]["committed_micro_eur"],
        "reserved": lambda: report["unresolved"][0]["reserved_micro_eur"],
        "actual": lambda: report["unresolved"][0]["actual_micro_eur"],
        "unreceipted": lambda: report["unreceipted_bound_actual"][0]["micro_eur"],
    }[source]()


@pytest.mark.parametrize("value, stored", _MALFORMED_MONEY, ids=_MALFORMED_IDS)
@pytest.mark.parametrize("source", sorted(_MONEY_COLUMNS))
def test_malformed_stored_money_refuses_the_report(tmp_path, source, value, stored):
    ledger = fx.make_ledger(tmp_path)
    _every_money_source(ledger)
    ledger.report()  # the same ledger reports before the damage
    assert _store(ledger, source, value) == stored
    # the report's own check, or the ledger's integrity check for the opening month:
    # both are the ledger's closed refusal, never a raw conversion error
    with pytest.raises(LedgerBlocked):
        ledger.report()


@pytest.mark.parametrize("source", sorted(_MONEY_COLUMNS))
def test_a_whole_real_is_stored_as_an_integer_and_reported_unchanged(tmp_path, source):
    """INTEGER affinity stores 2.0 as the exact integer 2, so the reader receives an
    integer: there is no float left to refuse, and nothing is converted."""
    ledger = fx.make_ledger(tmp_path)
    _every_money_source(ledger)
    assert _store(ledger, source, 2.0) == "integer"
    figure = _figure(ledger.report(), source)
    assert (type(figure), figure) == (int, 2)


@pytest.mark.parametrize("value", [
    1.5, 2.0, 0.0, float("nan"), float("inf"), "7", "seven", b"7", True, False, None, -1, 10**12 + 1,
])
def test_only_a_whole_int_in_bounds_is_stored_money(value):
    with pytest.raises(GatewayReportRefused):
        gateway._stored_money(value)


@pytest.mark.parametrize("value", [0, 1, 10**12])
def test_a_whole_int_in_bounds_is_stored_money_unchanged(value):
    assert gateway._stored_money(value) is value


# --- no names, closed words, schema 3 ------------------------------------------------------------


def test_the_report_names_no_agent_message_attempt_reference_or_path(tmp_path):
    ledger = fx.make_ledger(tmp_path)
    credential = fx.open_bound(ledger, "msg-bound-secret")
    _child_call(ledger, credential, 1)
    fx.open_unbound(ledger, "msg-unbound-secret")
    ledger.place_hold(reason="operator note with private words")
    text = json.dumps(ledger.report())
    for private in ("qwen-dev-1", "msg-bound-secret", "msg-unbound-secret", fx.attempt(1),
                    fx.REFERENCE, gateway.quota_lease_ref_sha256(fx.REFERENCE),
                    "private words", fx.OPENING_EVIDENCE, str(tmp_path), tmp_path.name):
        assert private not in text


@pytest.mark.parametrize("make_hold, word", [
    (lambda ledger: None, None),
    (lambda ledger: ledger.place_hold(reason="checking the invoice"), "manual"),
    (lambda ledger: _child_call(ledger, fx.open_bound(ledger, "msg-a"), 2, OVER_TOKENS),
     "attempt_over_reservation"),
    (lambda ledger: ledger.verify_dashboard_canary(fx.attempt(1), observed_delta_micro_eur=1),
     "dashboard_canary_mismatch"),
    (lambda ledger: _set_hold_text(ledger, "a hold text no version writes"), "other"),
], ids=["none", "manual", "over-reservation", "canary", "other"])
def test_the_hold_is_a_flag_and_a_closed_word_never_its_text(tmp_path, make_hold, word):
    ledger = fx.make_ledger(tmp_path)
    _unowned_call(ledger, 1)
    make_hold(ledger)
    report = ledger.report()
    assert (report["service_hold"], report["service_hold_reason"]) == (word is not None, word)
    status_text = ledger.status()["service_hold"] or ""
    if status_text and status_text != word:  # the canary's text is its own closed word
        assert status_text not in json.dumps(report)


def _set_hold_text(ledger, text: str) -> None:
    with sqlite3.connect(ledger.db_path) as conn:
        conn.execute("UPDATE metadata SET value=? WHERE key='service_hold'", (text,))


def test_a_schema3_ledger_reports_without_receipt_figures(tmp_path):
    ledger = fx.make_schema3_ledger(tmp_path)
    _unowned_call(ledger, 1)
    fx.open_unbound(ledger, "msg-a")
    report = ledger.report()
    assert report["child_cap_ready"] is True
    assert report["child_cap_policy_hash"] == ledger.status()["child_cap_policy_hash"]
    assert (report["open_child_turns"], report["open_child_turns_expired"]) == (1, 0)
    assert (report["child_receipt_report_version"], report["child_receipts_through_seq"],
            report["child_receipts_pending"]) == (None, None, None)
    assert report["unreceipted_bound_actual"] == []
    assert report["periods"] == [{"period": OCTOBER, "committed_micro_eur": ONE_CALL}]


def test_the_report_agrees_with_the_status_snapshot(tmp_path):
    ledger = fx.make_ledger(tmp_path)
    _unowned_call(ledger, 1)
    credential = fx.open_bound(ledger, "msg-a")
    ledger.reserve_for_child(fx.attempt(2), capability=credential.token)
    report, status = ledger.report(), ledger.status()
    assert report["generation"] == status["generation"]
    assert report["generation"] == _pages(ledger)[0]["generation"]
    assert report["policy_hash"] == status["policy_hash"]
    assert report["child_cap_policy_hash"] == status["child_cap_policy_hash"]
    assert report["periods"] == status["periods"]
    assert report["unresolved"] == [
        {key: row[key] for key in ("state", "reserved_micro_eur", "actual_micro_eur", "period")}
        for row in status["unresolved"]
    ]
    assert report["child_receipt_report_version"] == status["child_receipt_report_version"] == 2


# --- the closed shape ----------------------------------------------------------------------------


def _good_report() -> dict:
    return {
        "child_cap_policy_hash": "c" * 64, "child_cap_ready": True,
        "child_receipt_report_version": 2, "child_receipts_pending": 1,
        "child_receipts_through_seq": 4, "earliest_open_expiry": "2026-10-04T10:00:00.000000Z",
        "gateway_report_version": 1, "generation": fx.GENERATION,
        "observed_at": "2026-10-03T10:00:00.000000Z", "open_child_turns": 2,
        "open_child_turns_expired": 1,
        "periods": [{"committed_micro_eur": 10, "period": SEPTEMBER},
                    {"committed_micro_eur": 20, "period": OCTOBER}],
        "policy_hash": "a" * 64, "service_hold": True,
        "service_hold_reason": "attempt_over_reservation",
        "unreceipted_bound_actual": [{"micro_eur": 5, "period": OCTOBER}],
        "unresolved": [{"actual_micro_eur": 7, "period": OCTOBER, "reserved_micro_eur": 6,
                        "state": "uncertain"}],
    }


def _changed(change) -> dict:
    report = _good_report()
    change(report)
    return report


def _no_child_cap(report: dict) -> None:
    report.update(child_cap_ready=False, child_cap_policy_hash=None, open_child_turns=None,
                  open_child_turns_expired=None, earliest_open_expiry=None,
                  child_receipt_report_version=None, child_receipts_through_seq=None,
                  child_receipts_pending=None, unreceipted_bound_actual=[])


GOOD_REPORTS = {
    "full": lambda r: None,
    "no child cap": _no_child_cap,
    "schema 3": lambda r: r.update(child_receipt_report_version=None, child_receipts_through_seq=None,
                                   child_receipts_pending=None, unreceipted_bound_actual=[]),
    "no open turn": lambda r: r.update(open_child_turns=0, open_child_turns_expired=0,
                                       earliest_open_expiry=None),
    "no hold": lambda r: r.update(service_hold=False, service_hold_reason=None),
}

BAD_REPORTS = {
    "extra key": lambda r: r.update(ready=True),
    "missing key": lambda r: r.pop("periods"),
    "version 2": lambda r: r.update(gateway_report_version=2),
    "hold text": lambda r: r.update(service_hold_reason="attempt 0123 exceeded the reserved policy"),
    "hold without word": lambda r: r.update(service_hold_reason=None),
    "word without hold": lambda r: r.update(service_hold=False),
    "hold as text": lambda r: r.update(service_hold="manual: x"),
    "attempt id in a row": lambda r: r["unresolved"][0].update(attempt_id=fx.attempt(1)),
    "resolved state": lambda r: r["unresolved"][0].update(state="settled"),
    "unknown state": lambda r: r["unresolved"][0].update(state="pending"),
    "state as a list": lambda r: r["unresolved"][0].update(state=[]),
    "state as an object": lambda r: r["unresolved"][0].update(state={}),
    "hold word as a list": lambda r: r.update(service_hold_reason=[]),
    "hold word as an object": lambda r: r.update(service_hold_reason={}),
    "periods out of order": lambda r: r["periods"].reverse(),
    "a month twice": lambda r: r["periods"][0].update(period=OCTOBER),
    "not a month": lambda r: r["periods"][0].update(period="2026-13"),
    "other digits": lambda r: r["periods"][0].update(period="２０２６-09"),
    "negative money": lambda r: r["periods"][0].update(committed_micro_eur=-1),
    "float money": lambda r: r["unreceipted_bound_actual"][0].update(micro_eur=5.0),
    "boolean money": lambda r: r["unresolved"][0].update(reserved_micro_eur=True),
    "money over bound": lambda r: r["unresolved"][0].update(actual_micro_eur=10**12 + 1),
    "unreceipted without receipts": lambda r: r.update(
        child_receipt_report_version=None, child_receipts_through_seq=None,
        child_receipts_pending=None),
    "receipts without child cap": lambda r: _no_child_cap(r) or r.update(child_receipts_pending=0),
    "turns without child cap": lambda r: _no_child_cap(r) or r.update(open_child_turns=0),
    "hash without child cap": lambda r: _no_child_cap(r) or r.update(child_cap_policy_hash="c" * 64),
    "no hash with child cap": lambda r: r.update(child_cap_policy_hash=None),
    "more expired than open": lambda r: r.update(open_child_turns_expired=3),
    "expiry without an open turn": lambda r: r.update(open_child_turns=0, open_child_turns_expired=0),
    "no expiry with an open turn": lambda r: r.update(earliest_open_expiry=None),
    "old receipt version": lambda r: r.update(child_receipt_report_version=1),
    "short generation": lambda r: r.update(generation="0123"),
    "upper-case policy hash": lambda r: r.update(policy_hash="A" * 64),
    "other time format": lambda r: r.update(observed_at="2026-10-03T10:00:00Z"),
    "readiness as a figure": lambda r: r.update(child_cap_ready=1),
}


@pytest.mark.parametrize("name", sorted(GOOD_REPORTS))
def test_a_report_in_the_closed_shape_passes(name):
    gateway.check_gateway_report(_changed(GOOD_REPORTS[name]))


@pytest.mark.parametrize("name", sorted(BAD_REPORTS))
def test_a_report_outside_the_closed_shape_is_refused(name):
    with pytest.raises(GatewayReportRefused):
        gateway.check_gateway_report(_changed(BAD_REPORTS[name]))


def test_a_report_the_checker_refuses_is_never_returned(tmp_path, monkeypatch):
    ledger = fx.make_ledger(tmp_path)
    monkeypatch.setattr(gateway, "_hold_word", lambda value: "free text the checker refuses")
    with pytest.raises(GatewayReportRefused):
        ledger.report()


# --- the command ---------------------------------------------------------------------------------


def _ledger_in(home: Path, monkeypatch, *, localappdata: bool, schema3: bool, with_token: bool):
    """A temporary ledger at the default place for a process with `home`: on schema 4,
    one receipt and one bound call still waiting for its receipt."""
    if localappdata:
        monkeypatch.setenv("LOCALAPPDATA", str(home / "local"))
    else:
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        monkeypatch.setenv("HOME", str(home))
    ledger = gateway.SpendLedger(gateway.default_ledger_path(), gateway.default_install_marker_path())
    assert Path(ledger.db_path).is_relative_to(home)
    ledger.initialize(opening_micro_eur=0, opening_evidence=fx.OPENING_EVIDENCE,
                      generation=fx.GENERATION, child_cap_issuer_token=fx.ISSUER)
    if schema3:
        fx.to_schema3(ledger)
        fx.open_unbound(ledger, "msg-a")
    else:
        fx.close_bound(ledger, "msg-fence", outcome="cancelled", reference=fx.OTHER_REFERENCE)
        _child_call(ledger, fx.open_bound(ledger, "msg-a"), 1)
    if with_token:
        gateway.write_secret_file(gateway.default_front_token_path(), fx.ISSUER)
    return ledger


def _report_process(folder: Path, env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 - fixed argv, test-only, no shell
        [sys.executable, "-m", "agenttalk", "gateway", "report", *args],
        cwd=folder, env=env, capture_output=True, text=True, timeout=120, check=False,
    )


def _without_time(report: dict) -> dict:
    return {key: value for key, value in report.items() if key != "observed_at"}


_HOME_VARIANTS = [True] if os.name == "nt" else [True, False]


@pytest.mark.parametrize("with_token", [True, False], ids=["token", "no-token"])
@pytest.mark.parametrize("schema3", [False, True], ids=["schema4", "schema3"])
@pytest.mark.parametrize("localappdata", _HOME_VARIANTS, ids=lambda v: "localappdata" if v else "home")
def test_the_report_runs_from_a_folder_with_no_project(tmp_path, monkeypatch, localappdata, schema3,
                                                       with_token):
    home = tmp_path / "home"
    folder = tmp_path / "ordinary-folder"
    folder.mkdir()
    ledger = _ledger_in(home, monkeypatch, localappdata=localappdata, schema3=schema3,
                        with_token=with_token)
    env = fx.bare_environment(home, localappdata=localappdata)
    assert "AGENTTALK_ROOT" not in env
    done = _report_process(folder, env, "--json")
    assert (done.returncode, done.stderr) == (0, "")
    report = json.loads(done.stdout)
    assert done.stdout == json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n"
    assert _without_time(report) == _without_time(ledger.report())
    if schema3:
        assert report["child_receipts_through_seq"] is None
    else:
        assert report["child_receipts_through_seq"] == 1
        assert report["unreceipted_bound_actual"] == [{"period": report["periods"][0]["period"],
                                                       "micro_eur": ONE_CALL}]
    for private in ("qwen-dev-1", "msg-a", str(home), str(folder)):
        assert private not in done.stdout
    assert not (folder / ".agenttalk").exists()


@pytest.mark.parametrize("setup, args, word", [
    ("no ledger", ("--json",), "report_unavailable"),
    ("half a ledger", ("--json",), "report_unavailable"),
    ("ledger", (), "bad_request"),
])
def test_every_report_failure_outside_a_project_is_one_closed_word(tmp_path, monkeypatch, setup,
                                                                   args, word):
    home = tmp_path / "home"
    folder = tmp_path / "ordinary-folder"
    folder.mkdir()
    if setup != "no ledger":
        ledger = _ledger_in(home, monkeypatch, localappdata=True, schema3=False, with_token=False)
        if setup == "half a ledger":
            Path(ledger.db_path).unlink()
    done = _report_process(folder, fx.bare_environment(home, localappdata=True), *args)
    assert (done.returncode, done.stdout, done.stderr) == (2, "", word + "\n")
    if setup != "ledger":  # a report never creates a ledger
        assert not list(home.rglob("*.sqlite3"))


def _default_ledger_with_every_money_source(home: Path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(home / "local"))
    ledger = gateway.SpendLedger(gateway.default_ledger_path(), gateway.default_install_marker_path())
    assert Path(ledger.db_path).is_relative_to(home)
    ledger.initialize(opening_micro_eur=0, opening_evidence=fx.OPENING_EVIDENCE,
                      generation=fx.GENERATION, child_cap_issuer_token=fx.ISSUER)
    _every_money_source(ledger)
    return ledger


@pytest.mark.parametrize("value", [value for value, _stored in _MALFORMED_MONEY], ids=_MALFORMED_IDS)
@pytest.mark.parametrize("source", sorted(_MONEY_COLUMNS))
def test_the_command_refuses_malformed_stored_money_with_one_word(tmp_path, monkeypatch, source, value):
    home = tmp_path / "home"
    folder = tmp_path / "ordinary-folder"
    folder.mkdir()
    ledger = _default_ledger_with_every_money_source(home, monkeypatch)
    _store(ledger, source, value)
    done = _report_process(folder, fx.bare_environment(home, localappdata=True), "--json")
    assert (done.returncode, done.stdout, done.stderr) == (2, "", "report_unavailable\n")


@pytest.mark.parametrize("source", sorted(_MONEY_COLUMNS))
def test_the_command_reports_a_whole_real_stored_as_an_integer(tmp_path, monkeypatch, source):
    home = tmp_path / "home"
    folder = tmp_path / "ordinary-folder"
    folder.mkdir()
    ledger = _default_ledger_with_every_money_source(home, monkeypatch)
    _store(ledger, source, 2.0)
    done = _report_process(folder, fx.bare_environment(home, localappdata=True), "--json")
    assert (done.returncode, done.stderr) == (0, "")
    assert _figure(json.loads(done.stdout), source) == 2


# --- the report advertises only receipts the pages can supply (fix round 2) -------------------


def _renumber_receipts(ledger, numbers: list[int]) -> None:
    """Restored or repaired data: give the receipts these numbers, with the update
    guard lifted only for the edit and put back exactly as it was."""
    with sqlite3.connect(ledger.db_path) as conn:
        guard = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='child_receipts_no_update'"
        ).fetchone()[0]
        conn.execute("DROP TRIGGER child_receipts_no_update")
        current = [row[0] for row in conn.execute("SELECT seq FROM child_receipts ORDER BY seq")]
        for seq in current:  # out of the way first, so no two rows ever share a number
            conn.execute("UPDATE child_receipts SET seq=? WHERE seq=?", (-1000 - seq, seq))
        for seq, number in zip(current, numbers, strict=True):
            conn.execute("UPDATE child_receipts SET seq=? WHERE seq=?", (number, -1000 - seq))
        conn.execute(guard)


def _two_zero_receipts(ledger) -> None:
    fx.close_bound(ledger, "msg-one", outcome="cancelled", reference="ref-one")
    fx.close_bound(ledger, "msg-two", outcome="cancelled", reference="ref-two")


_BROKEN_SEQUENCES = {"a gap": [1, 3], "from zero": [0, 2], "from two": [2, 3]}


@pytest.mark.parametrize("numbers", list(_BROKEN_SEQUENCES.values()), ids=list(_BROKEN_SEQUENCES))
def test_the_report_refuses_a_receipt_sequence_the_pages_refuse(tmp_path, numbers):
    ledger = fx.make_ledger(tmp_path)
    _two_zero_receipts(ledger)
    _renumber_receipts(ledger, numbers)
    with pytest.raises(gateway.ReceiptPageRefused, match="gap"):
        ledger.child_receipts_page(issuer_token=fx.ISSUER)
    with pytest.raises(GatewayReportRefused, match="gap"):
        ledger.report()


def test_a_contiguous_sequence_is_advertised_and_readable(tmp_path):
    ledger = fx.make_ledger(tmp_path)
    _two_zero_receipts(ledger)
    _renumber_receipts(ledger, [1, 2])  # the same edit, keeping the numbers whole
    page = ledger.child_receipts_page(issuer_token=fx.ISSUER)
    assert [receipt["seq"] for receipt in page["receipts"]] == [1, 2]
    assert ledger.report()["child_receipts_through_seq"] == 2


@pytest.mark.parametrize("numbers, report_word, receipts_word", [
    ([1, 3], "report_unavailable", "receipt_page_refused"),
    ([1, 2], None, None),
], ids=["gap", "contiguous"])
def test_both_commands_agree_on_the_receipt_sequence(tmp_path, monkeypatch, numbers, report_word,
                                                     receipts_word):
    home = tmp_path / "home"
    folder = tmp_path / "ordinary-folder"
    folder.mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(home / "local"))
    ledger = gateway.SpendLedger(gateway.default_ledger_path(), gateway.default_install_marker_path())
    assert Path(ledger.db_path).is_relative_to(home)
    ledger.initialize(opening_micro_eur=0, opening_evidence=fx.OPENING_EVIDENCE,
                      generation=fx.GENERATION, child_cap_issuer_token=fx.ISSUER)
    gateway.write_secret_file(gateway.default_front_token_path(), fx.ISSUER)
    _two_zero_receipts(ledger)
    _renumber_receipts(ledger, numbers)
    env = fx.bare_environment(home, localappdata=True)
    report = _report_process(folder, env, "--json")
    receipts = subprocess.run(  # noqa: S603 - fixed argv, test-only, no shell
        [sys.executable, "-m", "agenttalk", "gateway", "receipts", "--after", "0", "--json"],
        cwd=folder, env=env, capture_output=True, text=True, timeout=120, check=False,
    )
    if report_word is None:
        assert (report.returncode, receipts.returncode) == (0, 0)
        assert json.loads(report.stdout)["child_receipts_through_seq"] == 2
        assert json.loads(receipts.stdout)["next_seq"] == 2
    else:
        assert (report.returncode, report.stdout, report.stderr) == (2, "", report_word + "\n")
        assert (receipts.returncode, receipts.stdout, receipts.stderr) == (2, "", receipts_word + "\n")


# --- a gateway module that cannot be imported is still one closed word (fix round 2) -----------

_BROKEN_GATEWAY_IMPORT = """
import importlib.abc
import runpy
import sys


class BrokenGateway(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "agenttalk.ovh_gateway":
            raise RuntimeError("synthetic import failure in a private source path")
        return None


sys.meta_path.insert(0, BrokenGateway())
sys.argv = ["agenttalk", *sys.argv[1:]]
runpy.run_module("agenttalk", run_name="__main__")
"""


def _with_broken_gateway_import(tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    """The real module entry point, from an ordinary folder, with an import hook that
    fails only for the gateway module."""
    folder = tmp_path / "ordinary-folder"
    folder.mkdir()
    return subprocess.run(  # noqa: S603 - fixed argv, test-only, no shell
        [sys.executable, "-c", _BROKEN_GATEWAY_IMPORT, *args],
        cwd=folder, env=fx.bare_environment(tmp_path / "home", localappdata=True),
        capture_output=True, text=True, timeout=120, check=False,
    )


@pytest.mark.parametrize("args, word", [
    (("gateway", "report", "--json"), "report_unavailable"),
    (("gateway", "receipts", "--after", "0", "--json"), "receipts_unavailable"),
], ids=["report", "receipts"])
def test_a_gateway_module_that_cannot_import_is_still_one_closed_word(tmp_path, args, word):
    done = _with_broken_gateway_import(tmp_path, *args)
    assert (done.returncode, done.stdout, done.stderr) == (2, "", word + "\n")


def test_building_the_parser_never_imports_the_gateway_module(tmp_path):
    done = _with_broken_gateway_import(tmp_path, "gateway", "init", "--help")
    assert (done.returncode, done.stderr) == (0, "")
    assert "--cutoff-eur" in done.stdout


@pytest.mark.parametrize("flags, envelope", [
    ((), (gateway.TRIAL_CUTOFF_MICRO_EUR, gateway.SOFT_STOP_MICRO_EUR,
          gateway.EXTERNAL_CEILING_MICRO_EUR)),
    (("--cutoff-eur", "40", "--soft-stop-eur", "35", "--ceiling-eur", "60"),
     (40_000_000, 35_000_000, 60_000_000)),
], ids=["pinned-defaults", "explicit"])
def test_gateway_init_still_gets_the_pinned_envelope_by_default(tmp_path, monkeypatch, capsys, flags,
                                                                envelope):
    from agenttalk import ovh_gateway_service as service
    from agenttalk.store import Store

    root = tmp_path / "project"
    Store(root).init(["lead"])
    seen = {}

    def install(_root, **kwargs):
        seen.update(kwargs)
        return {"installed": True}

    monkeypatch.setattr(service, "initialize_install", install)
    assert cli.main(["--root", str(root), "gateway", "init", "--litellm-executable", "litellm",
                     "--opening-eur", "0", "--opening-evidence", "test", *flags]) == 0
    assert (seen["trial_cutoff_micro_eur"], seen["soft_stop_micro_eur"],
            seen["external_ceiling_micro_eur"]) == envelope


def test_an_unexpected_report_failure_is_still_one_closed_word(tmp_path, monkeypatch, capsys):
    def fail(*_args, **_kwargs):
        raise RuntimeError(f"unexpected failure under {tmp_path}")

    monkeypatch.setattr(gateway.SpendLedger, "report", fail)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["gateway", "report", "--json"]) == 2
    assert capsys.readouterr() == ("", "report_unavailable\n")


def test_a_held_ledger_with_an_unresolved_attempt_still_reports_with_exit_0(tmp_path, monkeypatch,
                                                                            capsys):
    ledger = gateway.SpendLedger(gateway.default_ledger_path(), gateway.default_install_marker_path())
    ledger.initialize(opening_micro_eur=0, opening_evidence=fx.OPENING_EVIDENCE,
                      generation=fx.GENERATION, child_cap_issuer_token=fx.ISSUER)
    ledger.reserve(fx.attempt(1))
    ledger.place_hold(reason="operator check")
    monkeypatch.chdir(tmp_path)
    assert cli.main(["gateway", "report", "--json"]) == 0
    out, err = capsys.readouterr()
    report = json.loads(out)
    assert err == ""
    assert (report["service_hold"], report["service_hold_reason"]) == (True, "manual")
    assert [row["state"] for row in report["unresolved"]] == ["reserved"]
    assert "operator check" not in out
