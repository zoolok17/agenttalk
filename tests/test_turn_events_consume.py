"""Every way the wrapper loop durably consumes a message, and the one event each
produces for the turn journal (``run_loop(on_message_disposed=...)``).

Two kinds of proof, both through the REAL loop:

* the existing end-to-end scenarios of the commit-gate and loop tests are re-run
  with the observer attached, so the real gate produces the resolutions;
* a scripted gate drives the retry and exhaustion paths that need exact control.

Each row names the site that owns the emission (the ``site`` argument the loop
passes), the disposition, and the raw facts. Exactly one event per message: the
observer is also checked against a second delivery attempt of the same message.
"""

from __future__ import annotations

import importlib
import inspect
import sys
from pathlib import Path
from typing import Any

import pytest

from agenttalk import turn_events as te
from agenttalk.store import Store
from agenttalk.wrapper import loop
from agenttalk.wrapper.obligations import Resolution, ResolverState


@pytest.fixture(autouse=True)
def _journal_root_in_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv(te.ENV_TURN_EVENTS_DIR, str(tmp_path / "journal-root"))


# --- the observer harness -----------------------------------------------------


class _Seen:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []
        self.sites: list[str | None] = []

    def dispositions(self) -> list[str]:
        return [d for _i, d, _f in self.events]

    def ids(self) -> list[str]:
        return [i for i, _d, _f in self.events]


def _observe(monkeypatch) -> _Seen:
    """Attach the observer to every run_loop call and record each owner site."""
    seen = _Seen()
    real_run, real_report = loop.run_loop, loop._disposal_report

    def spy_report(**kw):
        result = real_report(**kw)
        seen.sites.append(kw.get("site"))
        return result

    def spy_run(*args, **kw):
        assert "on_message_disposed" not in kw
        kw["on_message_disposed"] = lambda rec, disposition, facts: seen.events.append(
            (rec["id"], disposition, dict(facts))
        )
        return real_run(*args, **kw)

    monkeypatch.setattr(loop, "run_loop", spy_run)
    monkeypatch.setattr(loop, "_disposal_report", spy_report)
    return seen


def _scenario(module: str, name: str, tmp_path, monkeypatch, **extra: Any) -> _Seen:
    """Run an existing end-to-end test function with the observer attached."""
    tests_dir = str(Path(__file__).resolve().parent)
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)
    fn = getattr(importlib.import_module(module), name)
    seen = _observe(monkeypatch)
    params = {}
    for pname in inspect.signature(fn).parameters:
        if pname == "tmp_path":
            params[pname] = tmp_path
        elif pname == "monkeypatch":
            params[pname] = monkeypatch
        else:
            params[pname] = extra[pname]
    fn(**params)
    return seen


def _facts(consumed=True, landed=None, compliance=None, dead=False, failure=None) -> dict:
    return {
        "consumed": consumed,
        "landed": landed,
        "compliance_success": compliance,
        "dead_lettered": dead,
        "terminal_failure": failure,
    }


OWED = "test_owed_action_detection"
LOOP = "test_wrapper_loop"


# --- rows driven by the real gate and the real loop -------------------------------


def test_plain_commit_a_clean_turn_without_a_gate_resolution(tmp_path, monkeypatch):
    seen = _scenario(LOOP, "test_loop_drives_each_message_and_commits", tmp_path, monkeypatch)
    assert seen.sites == ["plain_commit", "plain_commit"]
    assert seen.dispositions() == ["completed", "completed"]
    assert len(set(seen.ids())) == 2
    assert all(f == _facts() for _i, _d, f in seen.events)


def test_plain_commit_with_an_unconfigured_gate_counts_the_turn(tmp_path, monkeypatch):
    seen = _scenario(OWED, "test_unconfigured_gate_continuous_success_commits_once", tmp_path, monkeypatch)
    assert seen.sites == ["plain_commit"] and seen.dispositions() == ["completed"]
    assert seen.events[0][2] == _facts()


def test_a_stand_down_control_record_is_consumed_without_an_event(tmp_path, monkeypatch):
    seen = _scenario(LOOP, "test_continuous_loop_clears_waiting_on_stop", tmp_path, monkeypatch)
    assert seen.events == []  # the control record is consumed, but it is not work


def test_an_unmarked_end_is_consumed_as_control_and_not_reported(tmp_path, monkeypatch):
    seen = _scenario(LOOP, "test_loop_ignores_unmarked_end", tmp_path, monkeypatch)
    assert seen.events == []


def test_a_failed_turn_is_not_consumed_so_it_is_not_reported(tmp_path, monkeypatch):
    seen = _scenario(LOOP, "test_loop_does_not_commit_failed_turn", tmp_path, monkeypatch)
    assert seen.events == []


def test_a_config_blocked_park_is_not_a_consumption(tmp_path, monkeypatch):
    seen = _scenario(LOOP, "test_config_blocked_head_parks_with_visible_health_not_frozen_idle", tmp_path, monkeypatch)
    assert seen.events == []


def test_a_gateway_hold_is_not_a_consumption_and_the_redriven_turn_reports_once(tmp_path, monkeypatch):
    seen = _scenario(LOOP, "test_gateway_held_head_redrives_and_self_heals_when_the_hold_clears", tmp_path, monkeypatch)
    assert seen.dispositions() == ["completed"] and len(seen.ids()) == 1


def test_dead_letter_after_a_failed_drive(tmp_path, monkeypatch):
    seen = _scenario(
        LOOP, "test_third_consecutive_watchdog_interruption_dead_letters_with_remedy", tmp_path, monkeypatch
    )
    assert seen.sites[0] == "dead_letter"
    first = seen.events[0]
    assert first[1] == "dead_lettered"
    assert first[2] == _facts(dead=True) | {"attempts_recorded": 3}


def test_dead_letter_on_entry_without_a_drive(tmp_path, monkeypatch):
    seen = _scenario(LOOP, "test_head_at_interruption_ceiling_on_entry_disposes_without_a_drive", tmp_path, monkeypatch)
    assert seen.sites == ["dead_letter"] and seen.dispositions() == ["dead_lettered"]
    assert seen.events[0][2]["dead_lettered"] is True


def test_admission_terminal_a_landed_terminal_found_at_admission_is_completed(tmp_path, monkeypatch):
    seen = _scenario(OWED, "test_exact_terminal_at_disposal_boundary_prevents_dead_letter", tmp_path, monkeypatch)
    assert seen.sites == ["admission_terminal"] and seen.dispositions() == ["completed"]
    facts = seen.events[0][2]
    assert facts["landed"] is True and facts["terminal_failure"] is False and facts["dead_lettered"] is False


def test_admitted_terminal_a_drive_that_lands_its_reply(tmp_path, monkeypatch):
    seen = _scenario(OWED, "test_reply_lands_then_nonzero_child_commits_without_duplicate", tmp_path, monkeypatch)
    assert seen.sites == ["admitted_terminal"] and seen.dispositions() == ["completed"]
    assert seen.events[0][2] == _facts(landed=True, compliance=True, failure=False)


def test_dispatch_exhausted_after_a_drive_is_a_failed_delivery(tmp_path, monkeypatch):
    seen = _scenario(
        OWED, "test_print_not_run_is_never_committed_and_caps_at_two_paid_dispatches", tmp_path, monkeypatch
    )
    assert seen.sites == ["dispatch_exhausted_after_drive"] and seen.dispositions() == ["delivery_failed"]
    assert seen.events[0][2] == _facts(landed=False, compliance=False, failure=True)


def test_each_exhausted_message_is_reported_once(tmp_path, monkeypatch):
    seen = _scenario(OWED, "test_compliance_breaker_trips_after_three_dominant_exhaustions", tmp_path, monkeypatch)
    assert seen.dispositions() == ["delivery_failed"] * 3 and len(set(seen.ids())) == 3


def test_a_raced_successful_terminal_inside_the_retry_helper_is_completed_not_failed(tmp_path, monkeypatch):
    seen = _scenario(OWED, "test_operation_cap_fresh_replay_lets_concurrent_terminal_win", tmp_path, monkeypatch)
    assert seen.sites == ["settle_helper"] and seen.dispositions() == ["completed"]
    assert seen.events[0][2]["terminal_failure"] is False and seen.events[0][2]["landed"] is True


def test_a_raced_terminal_after_repeated_finalization_misses_is_completed(tmp_path, monkeypatch):
    seen = _scenario(
        OWED, "test_twelfth_finalization_miss_replays_latest_terminal_and_finalizes", tmp_path, monkeypatch
    )
    assert seen.sites == ["settle_helper"] and seen.dispositions() == ["completed"]


def test_a_landed_reply_found_before_any_drive_is_a_counted_completed_turn(tmp_path, monkeypatch):
    seen = _scenario(
        OWED, "test_exact_landed_review_result_recovers_before_redrive", tmp_path, monkeypatch, scoped=False
    )
    assert seen.sites == ["landed_commit"] and seen.dispositions() == ["completed"]
    assert seen.events[0][2]["landed"] is True


def test_a_reply_published_by_the_drive_is_found_and_committed(tmp_path, monkeypatch):
    seen = _scenario(
        OWED,
        "test_same_policy_legacy_drive_can_publish_and_finalize_exact_terminal",
        tmp_path,
        monkeypatch,
        scoped=False,
    )
    assert seen.sites == ["landed_commit"] and seen.dispositions() == ["completed"]


def test_no_admission_pending_a_retained_success_with_no_landed_proof_in_hand_is_unknown(tmp_path, monkeypatch):
    # The loop holds no landed proof here and reads nothing more from disk for the
    # observer: the honest record is "outcome unknown" with the raw facts it has.
    seen = _scenario(OWED, "test_no_admission_disposition_crash_replays_without_redriving_model", tmp_path, monkeypatch)
    assert seen.sites == ["no_admission_pending"] and seen.dispositions() == ["outcome_unknown"]
    assert seen.events[0][2]["consumed"] is True and seen.events[0][2]["landed"] is not True


def test_the_observer_adds_no_ledger_read_to_the_gate():
    from agenttalk.wrapper.obligations import DetectionCommitGate

    assert not hasattr(DetectionCommitGate, "no_admission_work_proof")
    assert "no_admission_work_proof" not in Path(loop.__file__).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "updated_agents",
    [
        {"beta": {"grade": "detection"}, "gamma": {"grade": "detection"}, "lead": {"grade": "detection"}},
        {"beta": {"grade": "detection"}},
        {"beta": {"grade": "detection"}, "gamma": {"grade": "security"}},
    ],
    ids=["unrelated-agent-edit", "active-to-no-grade", "active-to-security"],
)
def test_a_transfer_target_abort_proves_no_work_so_it_is_not_completed(tmp_path, monkeypatch, updated_agents):
    seen = _scenario(
        OWED,
        "test_open_transfer_target_abort_is_zero_work_terminal_across_policy_drift",
        tmp_path,
        monkeypatch,
        updated_agents=updated_agents,
    )
    assert seen.sites == ["no_admission_pending"] and seen.dispositions() == ["outcome_unknown"]
    assert seen.events[0][2]["consumed"] is True and seen.events[0][2]["terminal_failure"] is False


def test_c22_a_terminal_found_when_authorizing_the_drive(tmp_path, monkeypatch):
    seen = _scenario(
        OWED,
        "test_terminal_published_during_drive_authorization_counts_as_recovered",
        tmp_path,
        monkeypatch,
        scoped=False,
    )
    assert seen.sites == ["no_admission_authorized_terminal"]
    # a landed terminal is a counted turn; the disposition follows the landed fact
    assert seen.dispositions() == ["completed"] and seen.events[0][2]["landed"] is True


# --- the rows that need an exactly scripted gate -----------------------------------


KEY = "key-1"


class _Gate:
    """A commit gate whose every call must be expected: anything else fails the test."""

    fence = "scripted-gate"

    def __init__(self, store: Store, agent: str) -> None:
        self.store = store
        self.agent = agent
        self.calls: list[str] = []

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)

        def unexpected(*a, **k):
            raise AssertionError("unexpected gate call: " + name)

        return unexpected

    def _consume(self, record: dict) -> None:
        self.store.advance_cursor(self.agent, record["id"])

    def owed(self, record: dict) -> Resolution:
        return Resolution(ResolverState.OWED_UNSATISFIED, "owed", key=KEY, scoped_revision=1, ledger_revision=1)


def _terminal(state: ResolverState, *, landed: bool, compliance: bool = False) -> Resolution:
    return Resolution(
        state,
        "done",
        key=KEY,
        evidence_id="ev-1",
        scoped_revision=2,
        ledger_revision=2,
        compliance_success=compliance,
        landed_evidence_id="ev-1" if landed else None,
    )


def _run(tmp_path, gate_cls, *, drive=None, polls=4):
    store = Store(tmp_path)
    store.init(["alpha", "beta"])
    record = store.send(sender="alpha", recipient="beta", body="work", meta={"request_id": "q-1"})
    gate = gate_cls(store, "beta")
    seen: list[tuple[str, str, dict]] = []
    reports: list[str | None] = []
    real_report = loop._disposal_report

    def spy(**kw):
        reports.append(kw.get("site"))
        return real_report(**kw)

    original = loop._disposal_report
    loop._disposal_report = spy
    try:
        loop.run_loop(
            store,
            "beta",
            drive or (lambda rec: loop.DriveOutcome(ok=True)),
            commit_gate=gate,
            clock=lambda: 0.0,
            sleep=lambda _d: None,
            max_polls=polls,
            on_message_disposed=lambda rec, d, f: seen.append((rec["id"], d, dict(f))),
        )
    finally:
        loop._disposal_report = original
    return store, record, seen, reports, gate


class _AtAdmission(_Gate):
    def admit_or_finalize(self, record):
        return self.owed(record)

    def captured_operation(self, key):
        return None

    def next_dispatch_purpose(self, key):
        return None


def test_dispatch_exhausted_at_admission_is_a_failed_delivery(tmp_path):
    class Gate(_AtAdmission):
        def dispatch_exhausted(self, key):
            return True

        def fail_delivery_or_block(self, record, key, *, reason, expected_revision):
            self._consume(record)
            return _terminal(ResolverState.DELIVERY_EXHAUSTED, landed=False)

    _store, record, seen, reports, _g = _run(tmp_path, Gate)
    assert seen == [(record.id, "delivery_failed", _facts(landed=False, compliance=False, failure=True))]
    assert reports == ["dispatch_exhausted_at_admission"]


def test_a_raced_success_inside_fail_delivery_is_completed_not_failed(tmp_path):
    class Gate(_AtAdmission):
        def dispatch_exhausted(self, key):
            return True

        def fail_delivery_or_block(self, record, key, *, reason, expected_revision):
            self._consume(record)  # the gate found a concurrently landed success first
            return _terminal(ResolverState.SATISFIED, landed=True, compliance=True)

    _store, record, seen, _reports, _g = _run(tmp_path, Gate)
    assert seen == [(record.id, "completed", _facts(landed=True, compliance=True, failure=False))]


def test_compliance_success_without_a_landed_reply_is_outcome_unknown(tmp_path):
    class Gate(_AtAdmission):
        def dispatch_exhausted(self, key):
            return True

        def fail_delivery_or_block(self, record, key, *, reason, expected_revision):
            self._consume(record)
            return _terminal(ResolverState.SATISFIED, landed=False, compliance=True)

    _store, record, seen, _reports, _g = _run(tmp_path, Gate)
    assert seen == [(record.id, "outcome_unknown", _facts(landed=False, compliance=True, failure=False))]


class _Captured(_Gate):
    """An obligation whose captured bus operation is being retried."""

    def admit_or_finalize(self, record):
        return self.owed(record)

    def captured_operation(self, key):
        return "captured-permit"

    def cleanup_permit(self, permit):
        pass

    def mark_satisfied(self, key):
        pass


def test_captured_terminal_a_retried_operation_resolves_the_obligation(tmp_path):
    class Gate(_Captured):
        def record_retry_barrier(self, key, *, category, expected_revision):
            return True

        def retry_captured_operation(self, permit, record):
            return True

        def mark_captured_operation_succeeded(self, permit):
            pass

        def resolve(self, record):
            return _terminal(ResolverState.SATISFIED, landed=True, compliance=True)

        def finalize(self, record, resolution, *, expected_revision=None):
            self._consume(record)
            return resolution

    _store, record, seen, reports, _g = _run(tmp_path, Gate)
    assert seen == [(record.id, "completed", _facts(landed=True, compliance=True, failure=False))]
    assert reports == ["captured_terminal"]


def test_captured_terminal_without_a_landed_reply_is_outcome_unknown(tmp_path):
    class Gate(_Captured):
        def record_retry_barrier(self, key, *, category, expected_revision):
            return True

        def retry_captured_operation(self, permit, record):
            return True

        def mark_captured_operation_succeeded(self, permit):
            pass

        def resolve(self, record):
            return _terminal(ResolverState.OPERATOR_RESOLVED, landed=False)

        def finalize(self, record, resolution, *, expected_revision=None):
            self._consume(record)
            return resolution

    _store, record, seen, _reports, _g = _run(tmp_path, Gate)
    assert seen == [(record.id, "outcome_unknown", _facts(landed=False, compliance=False, failure=False))]


class _Exhausted(_Captured):
    """The captured operation exhausted its durable retry bound."""

    def record_retry_barrier(self, key, *, category, expected_revision):
        return False

    def resolve(self, record):
        return Resolution(ResolverState.BLOCKED, "blocked", key=KEY)

    def settle_retry_exhaustion(self, record, key, *, category, reason, permit=None):
        self.settled = True
        return _terminal(ResolverState.DELIVERY_EXHAUSTED, landed=False)


def test_the_retry_helper_owns_an_exhaustion_it_consumed_itself(tmp_path):
    class Gate(_Exhausted):
        def settle_retry_exhaustion(self, record, key, *, category, reason, permit=None):
            self._consume(record)
            return super().settle_retry_exhaustion(record, key, category=category, reason=reason, permit=permit)

    _store, record, seen, reports, _g = _run(tmp_path, Gate)
    assert seen == [(record.id, "delivery_failed", _facts(landed=False, compliance=False, failure=True))]
    assert reports == ["settle_helper"]  # the second check at the call site found a guard, not a second event


def test_captured_exhausted_at_admission_is_owned_by_the_call_site_when_it_sees_the_consumption(tmp_path):
    class Gate(_Exhausted):
        def cleanup_permit(self, permit):
            self._consume_pending()  # the consumption lands after the helper's check

        def _consume_pending(self):
            self.store.advance_cursor(self.agent, self.record_id)

    store = Store(tmp_path)
    store.init(["alpha", "beta"])
    record = store.send(sender="alpha", recipient="beta", body="work", meta={"request_id": "q-1"})
    gate = Gate(store, "beta")
    gate.record_id = record.id
    seen: list[tuple[str, str, dict]] = []
    reports: list[str | None] = []
    real_report = loop._disposal_report
    loop._disposal_report = lambda **kw: reports.append(kw.get("site")) or real_report(**kw)
    try:
        loop.run_loop(
            store,
            "beta",
            lambda rec: True,
            commit_gate=gate,
            clock=lambda: 0.0,
            sleep=lambda _d: None,
            max_polls=3,
            on_message_disposed=lambda rec, d, f: seen.append((rec["id"], d, dict(f))),
        )
    finally:
        loop._disposal_report = real_report
    assert seen == [(record.id, "delivery_failed", _facts(landed=False, compliance=False, failure=True))]
    assert reports == ["captured_exhausted_at_admission"]


class _AfterDrive(_Gate):
    """A drive that hit an operation infrastructure failure on an owed obligation."""

    def admit_or_finalize(self, record):
        return self.owed(record)

    def captured_operation(self, key):
        return None

    def next_dispatch_purpose(self, key):
        return "initial"

    def reserve_dispatch(self, resolution, *, purpose):
        return "permit-1"

    def dispatch_record(self, record, permit):
        return dict(record)

    def mark_dispatch_result(self, permit, **kw):
        pass

    def cleanup_permit(self, permit):
        pass

    def mark_satisfied(self, key):
        pass

    def resolve(self, record):
        return self.owed(record)


def _infra_drive(rec):
    return loop.DriveOutcome(ok=False, failure_class=loop.CLASS_AMBIGUOUS, bus_action_infra=True)


def test_admitted_retry_terminal_a_retry_after_a_drive_resolves_the_obligation(tmp_path):
    class Gate(_AfterDrive):
        resolved = 0

        def record_retry_barrier(self, key, *, category, expected_revision):
            return True

        def retry_captured_operation(self, permit, record):
            return True

        def mark_captured_operation_succeeded(self, permit):
            pass

        def resolve(self, record):
            self.resolved += 1
            if self.resolved >= 2:  # after the successful retry
                return _terminal(ResolverState.SATISFIED, landed=True, compliance=True)
            return self.owed(record)

        def finalize(self, record, resolution, *, expected_revision=None):
            self._consume(record)
            return resolution

    _store, record, seen, reports, _g = _run(tmp_path, Gate, drive=_infra_drive)
    assert seen == [(record.id, "completed", _facts(landed=True, compliance=True, failure=False))]
    assert reports == ["admitted_retry_terminal"]


def test_captured_exhausted_after_a_drive_is_owned_by_the_call_site(tmp_path):
    class Gate(_AfterDrive):
        def record_retry_barrier(self, key, *, category, expected_revision):
            return False

        def retry_bound_exhausted(self, key, *, category):
            return True

        def settle_retry_exhaustion(self, record, key, *, category, reason, permit=None):
            return _terminal(ResolverState.DELIVERY_EXHAUSTED, landed=False)

        def cleanup_permit(self, permit):
            self.store.advance_cursor(self.agent, self.record_id)

        def resolve(self, record):
            return Resolution(ResolverState.BLOCKED, "blocked", key=KEY)

    store = Store(tmp_path)
    store.init(["alpha", "beta"])
    record = store.send(sender="alpha", recipient="beta", body="work", meta={"request_id": "q-1"})
    gate = Gate(store, "beta")
    gate.record_id = record.id
    seen: list[tuple[str, str, dict]] = []
    reports: list[str | None] = []
    real_report = loop._disposal_report
    loop._disposal_report = lambda **kw: reports.append(kw.get("site")) or real_report(**kw)
    try:
        loop.run_loop(
            store,
            "beta",
            _infra_drive,
            commit_gate=gate,
            clock=lambda: 0.0,
            sleep=lambda _d: None,
            max_polls=3,
            on_message_disposed=lambda rec, d, f: seen.append((rec["id"], d, dict(f))),
        )
    finally:
        loop._disposal_report = real_report
    assert seen == [(record.id, "delivery_failed", _facts(landed=False, compliance=False, failure=True))]
    assert reports == ["captured_exhausted_after_drive"]


# --- the pure mapping, row by row --------------------------------------------------


def _report(**kw):
    disposition, facts = loop._disposal_report(**kw)
    return disposition, facts


def test_the_disposition_follows_what_was_committed_not_which_helper_ran():
    landed = _terminal(ResolverState.SATISFIED, landed=True, compliance=True)
    assert _report(resolution=landed, finalized=landed)[0] == "completed"
    # compliance success without landed evidence proves nothing about the work
    unlanded = _terminal(ResolverState.SATISFIED, landed=False, compliance=True)
    disposition, facts = _report(resolution=unlanded, finalized=unlanded)
    assert disposition == "outcome_unknown" and facts["compliance_success"] is True and facts["landed"] is False
    failed = _terminal(ResolverState.DELIVERY_EXHAUSTED, landed=False)
    assert _report(resolution=failed, finalized=failed)[0] == "delivery_failed"
    for state in (
        ResolverState.TRANSFERRED,
        ResolverState.OPERATOR_RESOLVED,
        ResolverState.SUPERSEDED,
        ResolverState.BROADCAST_POLICY_SATISFIED,
    ):
        term = _terminal(state, landed=False)
        assert _report(resolution=term, finalized=term)[0] == "outcome_unknown"
    assert _report(dead_lettered=True, attempts=4) == ("dead_lettered", _facts(dead=True) | {"attempts_recorded": 4})
    assert _report(counted_turn=True)[0] == "completed"
    assert _report(counted_turn=False)[0] == "outcome_unknown"
    # a dead letter wins over everything else
    assert _report(resolution=failed, finalized=failed, dead_lettered=True)[0] == "dead_lettered"


def test_every_report_is_a_valid_journal_record():
    landed = _terminal(ResolverState.SATISFIED, landed=True, compliance=True)
    cases = [
        _report(resolution=landed, finalized=landed),
        _report(dead_lettered=True, attempts=2),
        _report(counted_turn=True),
        _report(resolution=_terminal(ResolverState.DELIVERY_EXHAUSTED, landed=False), finalized=None),
    ]
    for disposition, facts in cases:
        record = {
            "v": 1,
            "kind": "message_disposed",
            "event_id": "00000000-0000-4000-8000-000000000000",
            "stream": "beta.g1",
            "at": "2026-10-03T00:00:00.000Z",
            "agent": "beta",
            "dropped_total": 0,
            "seq": 1,
            "message_id": "m-1",
            "disposition": disposition,
            "message_at": None,
            **facts,
        }
        te.validate_event(record)


def test_a_message_reaches_the_observer_at_most_once_even_if_a_site_runs_twice(tmp_path):
    # the retry helper's own check and the call site's check both see the consumption
    class Gate(_Exhausted):
        def settle_retry_exhaustion(self, record, key, *, category, reason, permit=None):
            self._consume(record)
            return super().settle_retry_exhaustion(record, key, category=category, reason=reason, permit=permit)

    _s, _record, seen, reports, _g = _run(tmp_path, Gate)
    assert len(seen) == 1 and reports == ["settle_helper"]


def test_an_observer_that_raises_never_changes_what_the_loop_does(tmp_path):
    store = Store(tmp_path)
    store.init(["alpha", "beta"])
    m1 = store.send(sender="alpha", recipient="beta", body="one")
    m2 = store.send(sender="alpha", recipient="beta", body="two")

    def boom(rec, disposition, facts):
        raise RuntimeError("observer broke")

    turns = loop.run_loop(
        store, "beta", lambda rec: True, clock=lambda: 0.0, sleep=lambda _d: None, max_turns=2, on_message_disposed=boom
    )
    assert turns == 2 and store.cursor("beta") == m2.id and m1.id < m2.id


def test_off_the_loop_calls_no_observer_and_reads_no_ledger(tmp_path):
    class Gate(_AtAdmission):
        def dispatch_exhausted(self, key):
            return True

        def fail_delivery_or_block(self, record, key, *, reason, expected_revision):
            self._consume(record)
            return _terminal(ResolverState.DELIVERY_EXHAUSTED, landed=False)

    store = Store(tmp_path)
    store.init(["alpha", "beta"])
    store.send(sender="alpha", recipient="beta", body="work", meta={"request_id": "q-1"})
    loop.run_loop(
        store,
        "beta",
        lambda rec: True,
        commit_gate=Gate(store, "beta"),
        clock=lambda: 0.0,
        sleep=lambda _d: None,
        max_polls=3,
    )
    assert store.cursor("beta")
