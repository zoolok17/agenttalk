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

from agenttalk.ovh_gateway import (
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
    )
    return ledger


def test_exact_price_policy_and_charge_fixture() -> None:
    assert POLICY_CURRENCY == "EUR"
    assert INPUT_RATE_MICRO_EUR == 710_000
    assert OUTPUT_RATE_MICRO_EUR == 4_250_000
    assert RESERVE_INPUT_RATE_MICRO_EUR == 852_000
    assert RESERVE_OUTPUT_RATE_MICRO_EUR == 5_100_000
    assert MAX_CONTEXT_TOKENS == 262_144
    assert MAX_OUTPUT_TOKENS == 4_096
    assert TRIAL_CUTOFF_MICRO_EUR == 25_000_000
    assert SOFT_STOP_MICRO_EUR == 20_000_000
    assert EXTERNAL_CEILING_MICRO_EUR == 100_000_000
    assert settlement_cost_micro_eur(1_000, 100) == 1_135
    assert reservation_cost_micro_eur() == 244_237
    assert len(price_policy_hash()) == 64


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
    )
    assert marker["price_policy_hash"] == price_policy_hash()
    with pytest.raises(LedgerBlocked, match="requires both"):
        ledger.initialize(
            opening_micro_eur=0,
            opening_evidence=TEST_OPENING_EVIDENCE,
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
        )
    assert ledger.installation_state() == "absent"


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
    assert status["current_committed_micro_eur"] == 581_135
    assert status["current_trial_committed_micro_eur"] == 1_135


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
        )
    assert ledger.installation_state() == "absent"

    ledger.initialize(
        opening_micro_eur=0,
        opening_evidence=TEST_OPENING_EVIDENCE,
        generation="a" * 32,
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
    assert reservation.reserved_micro_eur == 244_237
    assert ledger.status()["ready"] is False

    result = ledger.settle(
        "1" * 32,
        model=MODEL_ALIAS,
        input_tokens=1_000,
        output_tokens=100,
    )
    assert result["actual_micro_eur"] == 1_135
    status = ledger.status()
    assert status["ready"] is True
    assert status["current_committed_micro_eur"] == 1_135
    assert status["unresolved"] == []


def test_reservation_barrier_failure_never_allows_transport_and_remains_held(tmp_path) -> None:
    ledger = make_ledger(tmp_path)

    def fail_barrier(_path) -> None:
        raise OSError("fake flush failure")

    failing = SpendLedger(
        ledger.db_path,
        ledger.marker_path,
        now=ledger.now,
        durability_barrier=fail_barrier,
    )
    with pytest.raises(LedgerBlocked, match="durability barrier"):
        failing.reserve("1" * 32)
    status = ledger.status()
    assert status["ready"] is False
    assert status["unresolved"][0]["attempt_id"] == "1" * 32


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
    assert result["total_actual_micro_eur"] == 244_237
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
