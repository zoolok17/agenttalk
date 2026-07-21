from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agenttalk import ovh_gateway as gateway
from agenttalk.ovh_gateway import (
    CANARY_TOLERANCE_BPS,
    EXTERNAL_CEILING_MICRO_EUR,
    INPUT_RATE_MICRO_EUR,
    MAX_CONTEXT_TOKENS,
    MAX_OUTPUT_TOKENS,
    MODEL_ALIAS,
    OUTPUT_RATE_MICRO_EUR,
    POLICY_CURRENCY,
    RESERVE_INPUT_RATE_MICRO_EUR,
    RESERVE_OUTPUT_RATE_MICRO_EUR,
    SOFT_STOP_MICRO_EUR,
    TRIAL_CUTOFF_MICRO_EUR,
    LedgerBlocked,
    LedgerHold,
    PolicyBlocked,
    SpendLedger,
    price_policy_hash,
    render_litellm_config,
    reservation_cost_micro_eur,
    settlement_cost_micro_eur,
)


TEST_OPENING_EVIDENCE = "test dashboard, observed 2026-07-15T12:00:00Z"
TEST_CHILD_CAP_ISSUER = "atgw-" + "i" * 43


class Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def make_ledger(tmp_path, clock: Clock | None = None) -> SpendLedger:
    clock = clock or Clock(datetime(2026, 7, 15, 12, tzinfo=timezone.utc))
    ledger = SpendLedger(
        tmp_path / "ledger.sqlite3",
        tmp_path / "install.json",
        now=clock,
    )
    ledger.initialize(
        opening_micro_eur=0,
        opening_evidence=TEST_OPENING_EVIDENCE,
        generation="a" * 32,
        child_cap_issuer_token=TEST_CHILD_CAP_ISSUER,
    )
    return ledger


def downgrade_to_v1_without_child_caps(ledger: SpendLedger) -> None:
    """Build the exact pre-cap ledger shape exercised by the migration."""
    with sqlite3.connect(ledger.db_path) as conn:
        conn.execute("DROP TABLE child_attempts")
        conn.execute("DROP TABLE child_capabilities")
        conn.execute("DROP TABLE child_turns")
        conn.execute(
            "DELETE FROM metadata WHERE key IN (?, ?, ?)",
            (
                "child_cap_schema_version",
                "child_cap_policy_hash",
                "child_cap_issuer_sha256",
            ),
        )
        conn.execute(
            "UPDATE metadata SET value='1' WHERE key='schema_version'"
        )
    marker = json.loads(ledger.marker_path.read_text(encoding="utf-8"))
    marker["ledger_schema_version"] = 1
    ledger.marker_path.write_text(json.dumps(marker), encoding="utf-8")


def test_exact_price_policy_and_charge_fixture() -> None:
    assert POLICY_CURRENCY == "EUR"
    assert INPUT_RATE_MICRO_EUR == 600_000
    assert OUTPUT_RATE_MICRO_EUR == 3_600_000
    assert RESERVE_INPUT_RATE_MICRO_EUR == 720_000
    assert RESERVE_OUTPUT_RATE_MICRO_EUR == 4_320_000
    assert MAX_CONTEXT_TOKENS == 262_144
    assert MAX_OUTPUT_TOKENS == 4_096
    assert TRIAL_CUTOFF_MICRO_EUR == 25_000_000
    assert SOFT_STOP_MICRO_EUR == 20_000_000
    assert EXTERNAL_CEILING_MICRO_EUR == 100_000_000
    assert CANARY_TOLERANCE_BPS == 1_000
    assert settlement_cost_micro_eur(1_000, 100) == 960
    assert reservation_cost_micro_eur() == 206_439
    assert len(price_policy_hash()) == 64
    assert gateway.child_cap_policy() == {
        "schema_version": 1,
        "max_calls": 8,
        "max_micro_eur": 500_000,
        "max_seconds": 300,
        "reservation_micro_eur": 206_439,
    }
    assert len(gateway.child_cap_policy_hash()) == 64


def test_litellm_config_is_single_model_callback_free_and_chat_completions() -> None:
    rendered = render_litellm_config(api_base="http://127.0.0.1:18080/v1")
    assert rendered.count("model_name:") == 1
    assert MODEL_ALIAS in rendered
    assert rendered.count("store: false") == 2
    assert "max_retries: 0" in rendered
    assert "num_retries: 0" in rendered
    litellm_settings = rendered.split("litellm_settings:\n", 1)[1].split(
        "router_settings:\n", 1
    )[0]
    assert "use_chat_completions_url_for_anthropic_messages: true" in litellm_settings
    assert "callback" not in rendered
    assert "/v1/responses" not in rendered


def test_initialize_is_explicit_and_partial_or_missing_state_fails_closed(tmp_path) -> None:
    ledger = SpendLedger(tmp_path / "ledger.sqlite3", tmp_path / "install.json")
    with pytest.raises(LedgerBlocked, match="not initialized"):
        ledger.status()
    marker = ledger.initialize(
        opening_micro_eur=0,
        opening_evidence=TEST_OPENING_EVIDENCE,
        generation="b" * 32,
        child_cap_issuer_token=TEST_CHILD_CAP_ISSUER,
    )
    assert marker["price_policy_hash"] == price_policy_hash()
    with pytest.raises(LedgerBlocked, match="requires both"):
        ledger.initialize(
            opening_micro_eur=0,
            opening_evidence=TEST_OPENING_EVIDENCE,
            child_cap_issuer_token=TEST_CHILD_CAP_ISSUER,
        )

    (tmp_path / "install.json").unlink()
    with pytest.raises(LedgerBlocked, match="partial"):
        ledger.status()


def test_initialize_durability_failure_never_creates_complete_install(tmp_path) -> None:
    def fail_barrier(_path) -> None:
        raise OSError("fake flush failure")

    ledger = SpendLedger(
        tmp_path / "ledger.sqlite3",
        tmp_path / "install.json",
        durability_barrier=fail_barrier,
    )
    with pytest.raises(OSError, match="flush failure"):
        ledger.initialize(
            opening_micro_eur=0,
            opening_evidence=TEST_OPENING_EVIDENCE,
            generation="a" * 32,
            child_cap_issuer_token=TEST_CHILD_CAP_ISSUER,
        )
    assert ledger.installation_state() == "absent"


def test_initialize_removes_temporary_persist_journal(tmp_path) -> None:
    ledger = SpendLedger(tmp_path / "ledger.sqlite3", tmp_path / "install.json")
    ledger.initialize(
        opening_micro_eur=0,
        opening_evidence=TEST_OPENING_EVIDENCE,
        generation="a" * 32,
        child_cap_issuer_token=TEST_CHILD_CAP_ISSUER,
    )

    assert not [
        path.name
        for path in tmp_path.iterdir()
        if path.name.startswith(".ledger.sqlite3.")
    ]


def test_corrupt_marker_or_database_fails_closed(tmp_path) -> None:
    ledger = make_ledger(tmp_path)
    ledger.marker_path.write_text("not-json", encoding="utf-8")
    with pytest.raises(LedgerBlocked, match="cannot read"):
        ledger.reserve("1" * 32)

    ledger = make_ledger(tmp_path / "other")
    ledger.db_path.write_bytes(b"not sqlite")
    with pytest.raises(LedgerBlocked):
        ledger.status()


def test_opening_balance_is_bound_seeded_and_surfaced(tmp_path) -> None:
    clock = Clock(datetime(2026, 7, 16, 8, 0, tzinfo=timezone.utc))
    ledger = SpendLedger(
        tmp_path / "ledger.sqlite3",
        tmp_path / "install.json",
        now=clock,
    )
    evidence = "OVH AI Endpoints dashboard, observed 2026-07-16 morning"

    marker = ledger.initialize(
        opening_micro_eur=580_000,
        opening_evidence=evidence,
        generation="b" * 32,
        child_cap_issuer_token=TEST_CHILD_CAP_ISSUER,
    )

    assert marker["opening_micro_eur"] == 580_000
    status = ledger.status()
    assert status["opening_micro_eur"] == 580_000
    assert status["opening_evidence"] == evidence
    assert status["opening_observed_at"] == "2026-07-16T08:00:00.000000Z"
    assert status["opening_period"] == "2026-07"
    assert status["current_committed_micro_eur"] == 580_000
    assert status["current_trial_committed_micro_eur"] == 0
    assert status["external_ceiling_micro_eur"] == 100_000_000

    ledger.reserve("1" * 32)
    ledger.settle(
        "1" * 32,
        model=MODEL_ALIAS,
        input_tokens=1_000,
        output_tokens=100,
    )
    status = ledger.status()
    assert status["current_committed_micro_eur"] == 580_960
    assert status["current_trial_committed_micro_eur"] == 960


def test_opening_balance_external_envelope_blocks_init_and_readiness(tmp_path) -> None:
    unsafe_opening = (
        EXTERNAL_CEILING_MICRO_EUR
        - TRIAL_CUTOFF_MICRO_EUR
        - reservation_cost_micro_eur()
        + 1
    )
    ledger = SpendLedger(tmp_path / "ledger.sqlite3", tmp_path / "install.json")
    with pytest.raises(PolicyBlocked, match="external account ceiling"):
        ledger.initialize(
            opening_micro_eur=unsafe_opening,
            opening_evidence=TEST_OPENING_EVIDENCE,
            child_cap_issuer_token=TEST_CHILD_CAP_ISSUER,
        )
    assert ledger.installation_state() == "absent"

    ledger.initialize(
        opening_micro_eur=0,
        opening_evidence=TEST_OPENING_EVIDENCE,
        generation="a" * 32,
        child_cap_issuer_token=TEST_CHILD_CAP_ISSUER,
    )
    marker = json.loads(ledger.marker_path.read_text(encoding="utf-8"))
    marker["opening_micro_eur"] = unsafe_opening
    ledger.marker_path.write_text(json.dumps(marker), encoding="utf-8")
    with sqlite3.connect(ledger.db_path) as conn:
        conn.execute(
            "UPDATE metadata SET value=? WHERE key='opening_micro_eur'",
            (str(unsafe_opening),),
        )
        conn.execute(
            "UPDATE periods SET committed_micro_eur=? WHERE period='2026-07'",
            (unsafe_opening,),
        )
    with pytest.raises(LedgerBlocked, match="opening balance envelope"):
        ledger.status()


def test_reserve_then_settle_commits_exact_actual(tmp_path) -> None:
    ledger = make_ledger(tmp_path)
    reservation = ledger.reserve("1" * 32)
    assert reservation.reserved_micro_eur == 206_439
    assert ledger.status()["ready"] is False

    result = ledger.settle(
        "1" * 32,
        model=MODEL_ALIAS,
        input_tokens=1_000,
        output_tokens=100,
    )
    assert result["actual_micro_eur"] == 960
    status = ledger.status()
    assert status["ready"] is True
    assert status["current_committed_micro_eur"] == 960
    assert status["unresolved"] == []


def test_child_turn_call_cap_is_durable_and_fail_closed(tmp_path) -> None:
    ledger = make_ledger(tmp_path)
    credential = ledger.open_child_turn(
        agent="qwen-dev-1",
        message_id="20260719-120000-000000-test",
        request_id="q-child-cap",
        issuer_token=TEST_CHILD_CAP_ISSUER,
    )
    assert credential.expires_at == "2026-07-15T12:05:00.000000Z"

    for ordinal in range(gateway.CHILD_TURN_MAX_CALLS):
        attempt_id = f"{ordinal + 1:032x}"
        ledger.reserve_for_child(attempt_id, capability=credential.token)
        ledger.settle(
            attempt_id,
            model=MODEL_ALIAS,
            input_tokens=1_000,
            output_tokens=100,
        )

    restarted = SpendLedger(ledger.db_path, ledger.marker_path, now=ledger.now)
    replay = restarted.open_child_turn(
        agent="qwen-dev-1",
        message_id="20260719-120000-000000-test",
        request_id="q-child-cap",
        issuer_token=TEST_CHILD_CAP_ISSUER,
    )
    with pytest.raises(gateway.ChildTurnCapExceeded, match="call ceiling"):
        restarted.reserve_for_child("f" * 32, capability=replay.token)

    status = restarted.status()
    assert status["child_cap_ready"] is True
    assert status["active_child_turns"][0]["attempt_count"] == (
        gateway.CHILD_TURN_MAX_CALLS
    )
    assert all(row["attempt_id"] != "f" * 32 for row in status["unresolved"])


def test_child_turn_mint_requires_non_inherited_controller_issuer(tmp_path) -> None:
    ledger = make_ledger(tmp_path)
    untrusted_child = SpendLedger(ledger.db_path, ledger.marker_path, now=ledger.now)

    with pytest.raises(gateway.ChildTurnCapBlocked, match="issuer"):
        untrusted_child.open_child_turn(
            agent="qwen-dev-1",
            message_id="invented-fresh-scope",
            request_id="q-bypass",
        )
    with pytest.raises(gateway.ChildTurnCapBlocked, match="issuer"):
        untrusted_child.open_child_turn(
            agent="qwen-dev-1",
            message_id="invented-fresh-scope",
            request_id="q-bypass",
            issuer_token="atgw-" + "x" * 43,
        )

    with sqlite3.connect(ledger.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM child_turns").fetchone()[0] == 0


def test_child_turn_cost_cap_and_scope_isolation(tmp_path) -> None:
    ledger = make_ledger(tmp_path)
    first = ledger.open_child_turn(
        agent="qwen-dev-1",
        message_id="message-a",
        request_id="same-request",
        issuer_token=TEST_CHILD_CAP_ISSUER,
    )
    for ordinal in range(2):
        attempt_id = f"{ordinal + 1:032x}"
        ledger.reserve_for_child(attempt_id, capability=first.token)
        ledger.settle(
            attempt_id,
            model=MODEL_ALIAS,
            input_tokens=MAX_CONTEXT_TOKENS,
            output_tokens=MAX_OUTPUT_TOKENS,
        )
    with pytest.raises(gateway.ChildTurnCapExceeded, match="cost ceiling"):
        ledger.reserve_for_child("3" * 32, capability=first.token)

    # A different immutable inbound message gets an independent bucket even when
    # request_id repeats; another child is independent too.
    second_message = ledger.open_child_turn(
        agent="qwen-dev-1",
        message_id="message-b",
        request_id="same-request",
        issuer_token=TEST_CHILD_CAP_ISSUER,
    )
    other_child = ledger.open_child_turn(
        agent="qwen-review-1",
        message_id="message-a",
        request_id="same-request",
        issuer_token=TEST_CHILD_CAP_ISSUER,
    )
    ledger.reserve_for_child("4" * 32, capability=second_message.token)
    ledger.settle(
        "4" * 32,
        model=MODEL_ALIAS,
        input_tokens=1_000,
        output_tokens=100,
    )
    ledger.reserve_for_child("5" * 32, capability=other_child.token)


def test_child_turn_expiry_and_forged_capability_block_before_reserve(tmp_path) -> None:
    clock = Clock(datetime(2026, 7, 15, 12, tzinfo=timezone.utc))
    ledger = make_ledger(tmp_path, clock)
    credential = ledger.open_child_turn(
        agent="qwen-dev-1",
        message_id="message-a",
        request_id="q-expiry",
        issuer_token=TEST_CHILD_CAP_ISSUER,
    )

    with pytest.raises(gateway.ChildTurnCapBlocked, match="capability"):
        ledger.reserve_for_child("1" * 32, capability="atgw-child-forged")
    assert ledger.status()["unresolved"] == []

    clock.value += timedelta(seconds=gateway.CHILD_TURN_MAX_SECONDS + 1)
    with pytest.raises(gateway.ChildTurnCapExceeded, match="wall-time"):
        ledger.reserve_for_child("2" * 32, capability=credential.token)
    assert ledger.status()["unresolved"] == []


def test_child_turn_clock_rollback_cannot_extend_wall_time(tmp_path) -> None:
    clock = Clock(datetime(2026, 7, 15, 12, tzinfo=timezone.utc))
    ledger = make_ledger(tmp_path, clock)
    clock.value += timedelta(hours=1)
    credential = ledger.open_child_turn(
        agent="qwen-dev-1",
        message_id="message-clock-rollback",
        request_id="q-clock-rollback",
        issuer_token=TEST_CHILD_CAP_ISSUER,
    )

    # This is still later than the ledger initialization clock, but earlier than
    # the durable turn opening. It must not extend the turn's wall-time budget.
    clock.value -= timedelta(minutes=59)
    with pytest.raises(LedgerHold, match="clock rollback"):
        ledger.reserve_for_child("1" * 32, capability=credential.token)

    with sqlite3.connect(ledger.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM child_attempts").fetchone()[0] == 0


def test_child_turn_clock_rollback_after_capability_reissue_blocks_reserve(
    tmp_path,
) -> None:
    clock = Clock(datetime(2026, 7, 15, 12, tzinfo=timezone.utc))
    ledger = make_ledger(tmp_path, clock)
    ledger.open_child_turn(
        agent="qwen-dev-1",
        message_id="message-reissued-clock",
        request_id="q-reissued-clock",
        issuer_token=TEST_CHILD_CAP_ISSUER,
    )
    clock.value += timedelta(minutes=4)
    replay = ledger.open_child_turn(
        agent="qwen-dev-1",
        message_id="message-reissued-clock",
        request_id="q-reissued-clock",
        issuer_token=TEST_CHILD_CAP_ISSUER,
    )

    clock.value -= timedelta(minutes=3)
    with pytest.raises(LedgerHold, match="clock rollback"):
        ledger.reserve_for_child("1" * 32, capability=replay.token)
    with sqlite3.connect(ledger.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM child_attempts").fetchone()[0] == 0


def test_child_turn_clock_rollback_after_settlement_blocks_more_transport(
    tmp_path,
) -> None:
    clock = Clock(datetime(2026, 7, 15, 12, tzinfo=timezone.utc))
    ledger = make_ledger(tmp_path, clock)
    credential = ledger.open_child_turn(
        agent="qwen-dev-1",
        message_id="message-settlement-clock",
        request_id="q-settlement-clock",
        issuer_token=TEST_CHILD_CAP_ISSUER,
    )
    ledger.reserve_for_child("1" * 32, capability=credential.token)
    clock.value += timedelta(minutes=4)
    ledger.settle(
        "1" * 32,
        model=MODEL_ALIAS,
        input_tokens=1_000,
        output_tokens=100,
    )

    clock.value -= timedelta(minutes=3)
    with pytest.raises(LedgerHold, match="clock rollback"):
        ledger.reserve_for_child("2" * 32, capability=credential.token)
    with sqlite3.connect(ledger.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM attempts WHERE attempt_id=?", ("2" * 32,)
        ).fetchone()[0] == 0


def test_child_cap_install_migrates_old_ledger_atomically_under_hold(tmp_path) -> None:
    ledger = make_ledger(tmp_path)
    ledger.place_hold(reason="install cap before any more paid work")
    downgrade_to_v1_without_child_caps(ledger)

    with pytest.raises(LedgerBlocked, match="marker ledger_schema_version mismatch"):
        ledger.status()
    installed = ledger.install_child_caps(issuer_token=TEST_CHILD_CAP_ISSUER)
    repeated = ledger.install_child_caps(issuer_token=TEST_CHILD_CAP_ISSUER)

    assert installed["installed"] is True
    assert repeated["installed"] is False
    after = ledger.status()
    assert after["child_cap_ready"] is True
    assert after["service_hold"] == "manual: install cap before any more paid work"
    assert after["worker_spend_ready"] is False


def test_child_cap_migration_fences_old_static_token_ledger_code(
    tmp_path, monkeypatch
) -> None:
    ledger = make_ledger(tmp_path)
    ledger.place_hold(reason="migration fence")
    downgrade_to_v1_without_child_caps(ledger)

    assert ledger.install_child_caps(
        issuer_token=TEST_CHILD_CAP_ISSUER
    )["installed"] is True
    marker = json.loads(ledger.marker_path.read_text(encoding="utf-8"))
    assert marker["ledger_schema_version"] == 2
    with sqlite3.connect(ledger.db_path) as conn:
        assert conn.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()[0] == "2"

    # The pre-cap front calls the old ledger's reserve() directly. Its v1
    # marker/schema authority must reject this migrated install before reserve.
    monkeypatch.setattr(gateway, "LEDGER_SCHEMA_VERSION", 1)
    old_ledger = SpendLedger(ledger.db_path, ledger.marker_path, now=ledger.now)
    with pytest.raises(LedgerBlocked, match="marker ledger_schema_version mismatch"):
        old_ledger.reserve("f" * 32)
    with sqlite3.connect(ledger.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0


def test_child_cap_migration_refuses_unresolved_attempt_without_partial_upgrade(
    tmp_path,
) -> None:
    ledger = make_ledger(tmp_path)
    ledger.reserve("a" * 32)
    downgrade_to_v1_without_child_caps(ledger)

    with pytest.raises(LedgerHold, match="all provider attempts reconciled"):
        ledger.install_child_caps(issuer_token=TEST_CHILD_CAP_ISSUER)

    marker = json.loads(ledger.marker_path.read_text(encoding="utf-8"))
    assert marker["ledger_schema_version"] == 1
    with sqlite3.connect(ledger.db_path) as conn:
        assert conn.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()[0] == "1"
        child_tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'child_%'"
        ).fetchall()
        assert child_tables == []


def test_child_cap_migration_recovers_after_marker_projection_failure(
    tmp_path, monkeypatch
) -> None:
    ledger = make_ledger(tmp_path)
    ledger.place_hold(reason="migration recovery")
    downgrade_to_v1_without_child_caps(ledger)
    durable_write = gateway._durable_write_json

    def fail_marker_projection(_path, _value) -> None:
        raise OSError("injected marker projection failure")

    monkeypatch.setattr(gateway, "_durable_write_json", fail_marker_projection)
    with pytest.raises(OSError, match="projection failure"):
        ledger.install_child_caps(issuer_token=TEST_CHILD_CAP_ISSUER)

    marker = json.loads(ledger.marker_path.read_text(encoding="utf-8"))
    assert marker["ledger_schema_version"] == 1
    with sqlite3.connect(ledger.db_path) as conn:
        assert conn.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()[0] == "2"
    with pytest.raises(LedgerBlocked, match="marker ledger_schema_version mismatch"):
        ledger.status()

    monkeypatch.setattr(gateway, "_durable_write_json", durable_write)
    assert ledger.install_child_caps(
        issuer_token=TEST_CHILD_CAP_ISSUER
    )["installed"] is True
    assert ledger.status()["child_cap_ready"] is True


def test_partial_child_cap_schema_fails_closed(tmp_path) -> None:
    ledger = make_ledger(tmp_path)
    with sqlite3.connect(ledger.db_path) as conn:
        conn.execute("DROP TABLE child_attempts")

    with pytest.raises(LedgerBlocked, match="partial"):
        ledger.status()


def test_child_capability_is_hash_only_and_global_rejection_consumes_no_slot(
    tmp_path,
) -> None:
    ledger = make_ledger(tmp_path)
    credential = ledger.open_child_turn(
        agent="qwen-dev-1",
        message_id="message-a",
        request_id="q-atomic",
        issuer_token=TEST_CHILD_CAP_ISSUER,
    )
    assert credential.token.encode("ascii") not in ledger.db_path.read_bytes()
    with sqlite3.connect(ledger.db_path) as conn:
        conn.execute(
            "UPDATE periods SET committed_micro_eur=? WHERE period='2026-07'",
            (TRIAL_CUTOFF_MICRO_EUR,),
        )

    with pytest.raises(PolicyBlocked, match="trial spend cutoff"):
        ledger.reserve_for_child("1" * 32, capability=credential.token)

    with sqlite3.connect(ledger.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM child_attempts").fetchone()[0] == 0
        conn.execute(
            "UPDATE periods SET committed_micro_eur=0 WHERE period='2026-07'"
        )
    ledger.reserve_for_child("2" * 32, capability=credential.token)
    with sqlite3.connect(ledger.db_path) as conn:
        assert conn.execute(
            "SELECT ordinal FROM child_attempts WHERE attempt_id=?", ("2" * 32,)
        ).fetchone()[0] == 1


def test_child_turn_last_slot_race_never_exceeds_ceiling(
    tmp_path, monkeypatch
) -> None:
    ledger = make_ledger(tmp_path)
    credential = ledger.open_child_turn(
        agent="qwen-dev-1",
        message_id="message-race",
        request_id="q-race",
        issuer_token=TEST_CHILD_CAP_ISSUER,
    )
    for ordinal in range(gateway.CHILD_TURN_MAX_CALLS - 1):
        attempt_id = f"{ordinal + 1:032x}"
        ledger.reserve_for_child(attempt_id, capability=credential.token)
        ledger.settle(
            attempt_id,
            model=MODEL_ALIAS,
            input_tokens=1_000,
            output_tokens=100,
        )

    # Isolate the child-cap CAS from the separate single-unresolved-attempt
    # safety gate, so the losing writer must observe the durable call ceiling.
    monkeypatch.setattr(
        SpendLedger, "_unresolved", staticmethod(lambda _conn: [])
    )

    barrier = threading.Barrier(2)
    results: list[tuple[str, str]] = []

    def reserve_last(attempt_id: str) -> None:
        contender = SpendLedger(ledger.db_path, ledger.marker_path, now=ledger.now)
        barrier.wait(timeout=5)
        try:
            contender.reserve_for_child(attempt_id, capability=credential.token)
        except gateway.ChildTurnCapExceeded:
            results.append(("blocked", attempt_id))
        else:
            results.append(("reserved", attempt_id))

    workers = [
        threading.Thread(target=reserve_last, args=("a" * 32,)),
        threading.Thread(target=reserve_last, args=("b" * 32,)),
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)

    assert sorted(state for state, _attempt_id in results) == ["blocked", "reserved"]
    blocked_id = next(
        attempt_id for state, attempt_id in results if state == "blocked"
    )
    with sqlite3.connect(ledger.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM child_attempts").fetchone()[0] == (
            gateway.CHILD_TURN_MAX_CALLS
        )
        assert conn.execute(
            "SELECT COALESCE(MAX(ordinal), 0) FROM child_attempts"
        ).fetchone()[0] == gateway.CHILD_TURN_MAX_CALLS
        assert conn.execute(
            "SELECT COUNT(*) FROM attempts WHERE attempt_id=?", (blocked_id,)
        ).fetchone()[0] == 0
        turn = conn.execute(
            "SELECT state, reason FROM child_turns WHERE message_id='message-race'"
        ).fetchone()
        assert turn == ("capped", "child turn call ceiling exceeded")


def test_sqlite_full_commit_is_the_reservation_durability_authority(tmp_path) -> None:
    ledger = make_ledger(tmp_path)

    def fail_barrier(_path) -> None:
        raise OSError("fake flush failure")

    failing = SpendLedger(
        ledger.db_path,
        ledger.marker_path,
        now=ledger.now,
        durability_barrier=fail_barrier,
    )
    failing.reserve("1" * 32)
    status = ledger.status()
    assert status["ready"] is False
    assert status["unresolved"][0]["attempt_id"] == "1" * 32


def test_settle_does_not_depend_on_a_fallible_post_commit_barrier(tmp_path) -> None:
    ledger = make_ledger(tmp_path)
    ledger.reserve("1" * 32)

    def fail_barrier(_path) -> None:
        raise OSError("post-commit barrier must not run")

    failing = SpendLedger(
        ledger.db_path,
        ledger.marker_path,
        now=ledger.now,
        durability_barrier=fail_barrier,
    )
    result = failing.settle(
        "1" * 32,
        model=MODEL_ALIAS,
        input_tokens=1_000,
        output_tokens=100,
    )

    assert result["actual_micro_eur"] == 960
    assert ledger.status()["ready"] is True


def test_reconcile_does_not_depend_on_a_fallible_post_commit_barrier(tmp_path) -> None:
    ledger = make_ledger(tmp_path)
    ledger.reserve("1" * 32)

    def fail_barrier(_path) -> None:
        raise OSError("post-commit barrier must not run")

    failing = SpendLedger(
        ledger.db_path,
        ledger.marker_path,
        now=ledger.now,
        durability_barrier=fail_barrier,
    )
    result = failing.reconcile(
        "1" * 32,
        outcome="no-send",
        reason="provider proves transport never started",
    )

    assert result["total_actual_micro_eur"] == 0
    assert ledger.status()["ready"] is True


def test_clear_hold_does_not_depend_on_a_fallible_post_commit_barrier(tmp_path) -> None:
    ledger = make_ledger(tmp_path)
    ledger.place_hold(reason="dashboard mismatch")

    def fail_barrier(_path) -> None:
        raise OSError("post-commit barrier must not run")

    failing = SpendLedger(
        ledger.db_path,
        ledger.marker_path,
        now=ledger.now,
        durability_barrier=fail_barrier,
    )
    result = failing.clear_hold(reason="operator completed reconciliation")

    assert result["held"] is False
    assert ledger.status()["ready"] is True


def test_unresolved_attempt_blocks_restart_until_explicit_reconcile(tmp_path) -> None:
    ledger = make_ledger(tmp_path)
    ledger.reserve("1" * 32)
    restarted = SpendLedger(ledger.db_path, ledger.marker_path, now=ledger.now)
    with pytest.raises(LedgerHold, match="unresolved"):
        restarted.reserve("2" * 32)
    restarted.mark_uncertain("1" * 32, reason="process killed after send")
    with pytest.raises(LedgerHold, match="unresolved"):
        restarted.reserve("2" * 32)

    result = restarted.reconcile(
        "1" * 32,
        outcome="charge-reserve",
        reason="operator could not prove no-send",
    )
    assert result["total_actual_micro_eur"] == reservation_cost_micro_eur()
    restarted.reserve("2" * 32)


def test_reconcile_never_accepts_a_caller_supplied_actual_cost(tmp_path) -> None:
    ledger = make_ledger(tmp_path)
    ledger.reserve("1" * 32)

    with pytest.raises(ValueError, match="no-send or charge-reserve"):
        ledger.reconcile(
            "1" * 32,
            outcome="charge-actual",
            reason="caller supplied dashboard value",
        )

    result = ledger.reconcile(
        "1" * 32,
        outcome="charge-reserve",
        reason="provider disposition remains ambiguous",
    )
    assert result["total_actual_micro_eur"] == reservation_cost_micro_eur()


def test_process_exit_after_durable_reserve_retains_global_restart_hold(tmp_path) -> None:
    clock = Clock(datetime(2026, 7, 15, 12, tzinfo=timezone.utc))
    ledger = make_ledger(tmp_path, clock)
    source_root = str((Path(__file__).resolve().parents[1] / "src"))
    code = (
        "import os,sys; from datetime import datetime,timezone; from pathlib import Path; "
        "from agenttalk.ovh_gateway import SpendLedger; "
        "now=lambda: datetime(2026,7,15,12,tzinfo=timezone.utc); "
        "SpendLedger(Path(sys.argv[1]),Path(sys.argv[2]),now=now).reserve('1'*32); "
        "os._exit(0)"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = source_root

    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code, str(ledger.db_path), str(ledger.marker_path)],
        check=False,
        env=env,
        timeout=30,
    )

    assert completed.returncode == 0
    restarted = SpendLedger(ledger.db_path, ledger.marker_path, now=clock)
    status = restarted.status()
    assert status["ready"] is False
    assert status["unresolved"][0]["attempt_id"] == "1" * 32
    with pytest.raises(LedgerHold, match="unresolved"):
        restarted.reserve("2" * 32)


def test_manual_price_mismatch_hold_requires_explicit_clear(tmp_path) -> None:
    ledger = make_ledger(tmp_path)
    placed = ledger.place_hold(reason="live dashboard price mismatch")
    assert placed["held"] is True
    with pytest.raises(LedgerHold, match="durable accounting hold"):
        ledger.reserve("1" * 32)
    assert ledger.status()["service_hold"] == "manual: live dashboard price mismatch"

    cleared = ledger.clear_hold(reason="operator approved corrected price policy")
    assert cleared["held"] is False
    ledger.reserve("1" * 32)


def test_attempt_reconcile_does_not_clear_independent_manual_hold(tmp_path) -> None:
    ledger = make_ledger(tmp_path)
    ledger.reserve("1" * 32)
    ledger.place_hold(reason="dashboard mismatch")
    ledger.reconcile("1" * 32, outcome="charge-reserve", reason="unknown disposition")
    assert ledger.status()["service_hold"] == "manual: dashboard mismatch"


def test_missing_or_invalid_usage_retains_reservation_and_sets_hold(tmp_path) -> None:
    ledger = make_ledger(tmp_path)
    ledger.reserve("1" * 32)
    with pytest.raises(LedgerHold, match="usage is invalid"):
        ledger.settle(
            "1" * 32,
            model=MODEL_ALIAS,
            input_tokens=0,
            output_tokens=0,
        )
    status = ledger.status()
    assert status["unresolved"][0]["state"] == "uncertain"
    assert status["current_committed_micro_eur"] == 0


@pytest.mark.parametrize(("input_tokens", "output_tokens"), [(0, 1), (1, 0)])
def test_zero_component_usage_is_not_authoritative(
    tmp_path,
    input_tokens,
    output_tokens,
) -> None:
    ledger = make_ledger(tmp_path)
    ledger.reserve("1" * 32)
    with pytest.raises(LedgerHold, match="usage is invalid"):
        ledger.settle(
            "1" * 32,
            model=MODEL_ALIAS,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
    assert ledger.status()["unresolved"][0]["state"] == "uncertain"


def test_usage_over_reservation_is_charged_and_holds_service(tmp_path) -> None:
    ledger = make_ledger(tmp_path)
    ledger.reserve("1" * 32)
    result = ledger.settle(
        "1" * 32,
        model=MODEL_ALIAS,
        input_tokens=MAX_CONTEXT_TOKENS + 1,
        output_tokens=MAX_OUTPUT_TOKENS,
    )
    assert result["held"] is True
    status = ledger.status()
    assert status["service_hold"]
    assert status["unresolved"][0]["state"] == "uncertain"


def test_cutoff_counts_committed_and_the_new_worst_case_reservation(tmp_path) -> None:
    ledger = make_ledger(tmp_path)
    with sqlite3.connect(ledger.db_path) as conn:
        conn.execute(
            "UPDATE periods SET committed_micro_eur=?",
            (TRIAL_CUTOFF_MICRO_EUR - reservation_cost_micro_eur() + 1,),
        )
    with pytest.raises(PolicyBlocked, match="cutoff"):
        ledger.reserve("1" * 32)


def test_running_external_ceiling_counts_committed_across_all_periods(tmp_path) -> None:
    clock = Clock(datetime(2026, 11, 1, 0, 1, tzinfo=timezone.utc))
    ledger = make_ledger(tmp_path, clock)
    with sqlite3.connect(ledger.db_path) as conn:
        conn.execute("DELETE FROM periods")
        conn.executemany(
            "INSERT INTO periods(period, committed_micro_eur) VALUES (?, ?)",
            [
                ("2026-07", TRIAL_CUTOFF_MICRO_EUR),
                ("2026-08", TRIAL_CUTOFF_MICRO_EUR),
                ("2026-09", TRIAL_CUTOFF_MICRO_EUR),
                ("2026-10", TRIAL_CUTOFF_MICRO_EUR),
                ("2026-11", 0),
            ],
        )
        conn.execute(
            "UPDATE metadata SET value='2026-11' WHERE key='last_accepted_period'"
        )
        conn.execute(
            "UPDATE metadata SET value=? WHERE key='last_accepted_utc'",
            ("2026-11-01T00:00:00.000000Z",),
        )

    with pytest.raises(PolicyBlocked, match="external ceiling"):
        ledger.reserve("1" * 32)


def test_dashboard_canary_enforces_nonzero_delta_and_numeric_tolerance(tmp_path) -> None:
    ledger = make_ledger(tmp_path)
    ledger.reserve("1" * 32)
    ledger.settle(
        "1" * 32,
        model=MODEL_ALIAS,
        input_tokens=1_000,
        output_tokens=100,
    )

    accepted = ledger.verify_dashboard_canary(
        "1" * 32,
        observed_delta_micro_eur=1_056,
    )
    assert accepted["accepted"] is True
    assert accepted["expected_micro_eur"] == 960
    assert accepted["tolerance_micro_eur"] == 96

    other = make_ledger(tmp_path / "zero")
    other.reserve("2" * 32)
    other.settle(
        "2" * 32,
        model=MODEL_ALIAS,
        input_tokens=1_000,
        output_tokens=100,
    )
    rejected = other.verify_dashboard_canary(
        "2" * 32,
        observed_delta_micro_eur=0,
    )
    assert rejected["accepted"] is False
    assert other.status()["service_hold"] == "dashboard_canary_mismatch"


def test_dashboard_canary_gates_worker_spend_readiness_until_accepted(tmp_path) -> None:
    ledger = make_ledger(tmp_path)
    initial = ledger.status()
    assert initial["ready"] is True
    assert initial["worker_spend_ready"] is False
    assert initial["worker_spend_errors"] == ["dashboard_canary_absent"]

    ledger.reserve("1" * 32)
    ledger.settle(
        "1" * 32,
        model=MODEL_ALIAS,
        input_tokens=1_000,
        output_tokens=100,
    )
    ledger.verify_dashboard_canary("1" * 32, observed_delta_micro_eur=0)
    mismatched = ledger.status()
    assert mismatched["ready"] is False
    assert mismatched["worker_spend_ready"] is False
    assert mismatched["worker_spend_errors"] == [
        "ledger_not_ready",
        "dashboard_canary_mismatch",
    ]

    ledger.clear_hold(reason="operator inspected mismatch")
    cleared = ledger.status()
    assert cleared["ready"] is True
    assert cleared["dashboard_canary"]["status"] == "mismatch"
    assert cleared["worker_spend_ready"] is False
    assert cleared["worker_spend_errors"] == ["dashboard_canary_mismatch"]

    ledger.verify_dashboard_canary("1" * 32, observed_delta_micro_eur=960)
    accepted = ledger.status()
    assert accepted["ready"] is True
    assert accepted["dashboard_canary"]["status"] == "accepted"
    assert accepted["worker_spend_ready"] is True
    assert accepted["worker_spend_errors"] == []


def test_dashboard_canary_readiness_rejects_stale_attempt_policy_binding(tmp_path) -> None:
    ledger = make_ledger(tmp_path)
    ledger.reserve("1" * 32)
    ledger.settle(
        "1" * 32,
        model=MODEL_ALIAS,
        input_tokens=1_000,
        output_tokens=100,
    )
    ledger.verify_dashboard_canary("1" * 32, observed_delta_micro_eur=960)
    with sqlite3.connect(ledger.db_path) as conn:
        conn.execute(
            "UPDATE attempts SET policy_hash='stale' WHERE attempt_id=?",
            ("1" * 32,),
        )

    with pytest.raises(LedgerBlocked, match="canary attempt binding"):
        ledger.status()


def test_begin_immediate_serializes_concurrent_admission(tmp_path) -> None:
    ledger = make_ledger(tmp_path)
    outcomes: list[str] = []
    barrier = threading.Barrier(3)

    def reserve(attempt_id: str) -> None:
        barrier.wait()
        try:
            ledger.reserve(attempt_id)
            outcomes.append("reserved")
        except LedgerHold:
            outcomes.append("held")

    threads = [
        threading.Thread(target=reserve, args=("1" * 32,)),
        threading.Thread(target=reserve, args=("2" * 32,)),
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=10)
    assert sorted(outcomes) == ["held", "reserved"]
    assert len(ledger.status()["unresolved"]) == 1


def test_begin_immediate_serializes_cross_process_admission(tmp_path) -> None:
    clock = Clock(datetime(2026, 7, 15, 12, tzinfo=timezone.utc))
    ledger = make_ledger(tmp_path, clock)
    start = tmp_path / "start"
    outputs = [tmp_path / "one.out", tmp_path / "two.out"]
    source_root = str(Path(__file__).resolve().parents[1] / "src")
    code = """
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from agenttalk.ovh_gateway import SpendLedger

start = Path(sys.argv[3])
while not start.exists():
    time.sleep(0.01)
now = lambda: datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
ledger = SpendLedger(Path(sys.argv[1]), Path(sys.argv[2]), now=now)
out = Path(sys.argv[5])
try:
    ledger.reserve(sys.argv[4])
except Exception as exc:
    out.write_text(type(exc).__name__, encoding="ascii")
else:
    out.write_text("reserved", encoding="ascii")
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = source_root
    children = [
        subprocess.Popen(  # noqa: S603
            [
                sys.executable,
                "-c",
                code,
                str(ledger.db_path),
                str(ledger.marker_path),
                str(start),
                attempt,
                str(output),
            ],
            env=env,
        )
        for attempt, output in zip(("1" * 32, "2" * 32), outputs, strict=True)
    ]
    start.write_text("go", encoding="ascii")
    for child in children:
        assert child.wait(timeout=30) == 0

    assert sorted(output.read_text(encoding="ascii") for output in outputs) == [
        "LedgerHold",
        "reserved",
    ]
    assert len(ledger.status()["unresolved"]) == 1


def test_period_is_pinned_at_admission_across_rollover(tmp_path) -> None:
    clock = Clock(datetime(2026, 12, 31, 23, 59, tzinfo=timezone.utc))
    ledger = make_ledger(tmp_path, clock)
    reservation = ledger.reserve("1" * 32)
    assert reservation.period == "2026-12"
    clock.value = datetime(2027, 1, 1, 0, 1, tzinfo=timezone.utc)
    ledger.settle(
        "1" * 32,
        model=MODEL_ALIAS,
        input_tokens=100,
        output_tokens=10,
    )
    periods = {row["period"]: row["committed_micro_eur"] for row in ledger.status()["periods"]}
    assert periods["2026-12"] == settlement_cost_micro_eur(100, 10)
    assert "2027-01" not in periods


def test_clock_rollback_and_impossible_jump_fail_closed(tmp_path) -> None:
    clock = Clock(datetime(2026, 7, 15, 12, tzinfo=timezone.utc))
    ledger = make_ledger(tmp_path, clock)
    clock.value -= timedelta(seconds=1)
    with pytest.raises(LedgerHold, match="rollback"):
        ledger.reserve("1" * 32)

    clock.value = datetime(2026, 9, 1, tzinfo=timezone.utc)
    with pytest.raises(LedgerHold, match="jumped implausibly"):
        ledger.reserve("2" * 32)


def test_clock_rollback_blocks_status_and_service_restart(tmp_path) -> None:
    clock = Clock(datetime(2026, 7, 15, 12, tzinfo=timezone.utc))
    ledger = make_ledger(tmp_path, clock)
    clock.value -= timedelta(microseconds=1)
    with pytest.raises(LedgerHold, match="rollback"):
        ledger.status()


def test_policy_hash_tamper_blocks_all_calls(tmp_path) -> None:
    ledger = make_ledger(tmp_path)
    marker = json.loads(ledger.marker_path.read_text(encoding="utf-8"))
    marker["price_policy_hash"] = "0" * 64
    ledger.marker_path.write_text(json.dumps(marker), encoding="utf-8")
    with pytest.raises(LedgerBlocked, match="price_policy_hash mismatch"):
        ledger.status()
