"""The pure rules of the usage-limit park and the attempt-record transitions they drive
(``wrapper.usage_park`` and the ledger methods of the store).

Streams are the two real captured Claude cases (sanitised, kept as printed in
``golden_stop_retries_scenarios``) and synthetic edits of them; nothing here calls a model.
"""

from __future__ import annotations

import copy

import pytest

import golden_stop_retries_scenarios as real
from agenttalk.wrapper import usage_park as park

AGENT = "beta"
T0 = 1788900000          # the fake wall clock: before both real reset times
CASE1_RESET = 1788948000
CASE2_RESET = 1790370000


def fact_of(events) -> dict | None:
    state: dict = {}
    for event in events:
        park.note_stream_event(state, event)
    return park.fact_from_stream(state)


def case1() -> list[dict]:
    return copy.deepcopy(real.REAL_CASE_FIVE_HOUR)


def case2() -> list[dict]:
    return copy.deepcopy(real.REAL_CASE_SEVEN_DAY)


# ------------------------------------------------------------------ the switch


@pytest.mark.parametrize("value,expected", [
    (None, True), ("1", True), ("", True), ("yes", True), ("0", False), (" 0 ", False),
    ("false", False), ("OFF", False), ("no", False),
])
def test_the_switch_is_on_unless_set_to_zero(value, expected):
    env = {} if value is None else {park.SWITCH_ENV: value}
    assert park.enabled(env) is expected


def test_the_switch_reads_the_process_environment_by_default(monkeypatch):
    monkeypatch.setenv(park.SWITCH_ENV, "0")
    assert park.enabled() is False
    monkeypatch.delenv(park.SWITCH_ENV)
    assert park.enabled() is True


# ------------------------------------------------------------------ the stream fact


def test_real_case_five_hour_is_a_usage_limit_with_its_reset():
    assert fact_of(case1()) == {"window": "five_hour", "reset_epoch": CASE1_RESET}


def test_real_case_seven_day_is_a_usage_limit_with_its_reset():
    assert fact_of(case2()) == {"window": "seven_day", "reset_epoch": CASE2_RESET}


def test_the_decision_never_reads_text():
    events = case2()
    for event in events:
        for key in ("result", "compact_error"):
            if key in event:
                event[key] = "unrelated words"
        if event.get("type") == "assistant":
            event["message"]["content"][0]["text"] = "unrelated words"
    assert fact_of(events) == {"window": "seven_day", "reset_epoch": CASE2_RESET}


def test_the_same_text_without_the_rejected_event_is_no_fact():
    events = [e for e in case2() if e.get("type") != "rate_limit_event"]
    assert fact_of(events) is None


def test_subtype_success_with_is_error_true_is_still_the_proof():
    events = case1()
    assert events[-1]["subtype"] == "success" and events[-1]["is_error"] is True
    assert fact_of(events) is not None
    events[-1]["subtype"] = "error_during_execution"
    assert fact_of(events) is not None


def test_a_later_success_result_vetoes_the_fact():
    events = case1()
    events[-1]["is_error"] = False
    assert fact_of(events) is None


@pytest.mark.parametrize("flag", ["true", 1, "yes", None, [True]])
def test_a_result_flag_that_is_not_a_json_boolean_is_no_fact(flag):
    events = case1()
    events[-1]["is_error"] = flag
    assert fact_of(events) is None


def test_the_missing_flag_is_no_fact():
    events = case1()
    del events[-1]["is_error"]
    assert fact_of(events) is None


def test_no_terminal_result_is_no_fact():
    assert fact_of(case1()[:-1]) is None


def test_a_result_before_the_rejected_event_does_not_count():
    events = case1()
    events.insert(0, {"type": "result", "is_error": True})
    assert fact_of(events[:-1]) is None


def test_the_last_result_after_the_event_decides():
    events = case1() + [{"type": "result", "is_error": False}]
    assert fact_of(events) is None
    events = case1()
    events.insert(-1, {"type": "result", "is_error": False})
    assert fact_of(events) is not None


@pytest.mark.parametrize("status", ["allowed", "allowed_warning", "Rejected", "REJECTED", "", None, True, 1])
def test_only_the_exact_rejected_status_is_the_proof(status):
    events = case1()
    events[1]["rate_limit_info"]["status"] = status
    assert fact_of(events) is None


@pytest.mark.parametrize("window", ["monthly", "five_hours", "", None, 5, ["five_hour"]])
def test_an_unknown_window_name_is_no_fact(window):
    events = case1()
    events[1]["rate_limit_info"]["rateLimitType"] = window
    assert fact_of(events) is None


def test_the_other_known_window_name_is_accepted():
    events = case1()
    events[1]["rate_limit_info"]["rateLimitType"] = "seven_day"
    assert fact_of(events)["window"] == "seven_day"


def test_non_object_lines_are_ignored():
    state: dict = {}
    for junk in (None, 5, "x", [1], {"type": 7}, {"type": "rate_limit_event", "rate_limit_info": "no"}):
        park.note_stream_event(state, junk)
    assert park.fact_from_stream(state) is None
    assert park.fact_from_stream(None) is None


# ------------------------------------------------------------------ the stated reset


@pytest.mark.parametrize("bad", ["1788948000", True, False, 0, -5, 1788948000.5, float("nan"),
                                 float("inf"), None, [1788948000], {"a": 1}])
def test_a_reset_that_is_not_whole_seconds_gives_no_reset_but_the_fact_stands(bad):
    events = case1()
    events[1]["rate_limit_info"]["resetsAt"] = bad
    fact = fact_of(events)
    assert fact == {"window": "five_hour", "reset_epoch": None}


def test_an_exhausted_window_with_a_bad_reset_gives_no_reset():
    events = case1()
    events[1]["rate_limit_info"]["unifiedWindows"]["five_hour"]["resetsAt"] = "soon"
    assert fact_of(events) == {"window": "five_hour", "reset_epoch": None}


def test_the_latest_exhausted_reset_wins():
    events = case1()
    windows = events[1]["rate_limit_info"]["unifiedWindows"]
    windows["seven_day"]["utilization"] = 1.0
    assert fact_of(events)["reset_epoch"] == 1789160400      # the weekly reset is later


def test_a_window_below_full_use_is_not_exhausted():
    events = case1()
    windows = events[1]["rate_limit_info"]["unifiedWindows"]
    windows["seven_day"]["utilization"] = 0.99
    windows["seven_day"]["resetsAt"] = "garbage"           # not exhausted: never read
    assert fact_of(events)["reset_epoch"] == CASE1_RESET


def test_unknown_windows_and_non_numeric_use_are_ignored():
    events = case1()
    windows = events[1]["rate_limit_info"]["unifiedWindows"]
    windows["monthly"] = {"utilization": 5, "resetsAt": 9999999999}
    windows["seven_day"]["utilization"] = "1.5"
    assert fact_of(events)["reset_epoch"] == CASE1_RESET


def test_unified_windows_that_are_not_an_object_are_ignored():
    events = case1()
    events[1]["rate_limit_info"]["unifiedWindows"] = ["five_hour"]
    assert fact_of(events)["reset_epoch"] == CASE1_RESET


@pytest.mark.parametrize("reset,expected", [
    (T0 + 10, T0 + 10),
    (T0 + 8 * 86400, T0 + 8 * 86400),
    (T0 + 8 * 86400 + 1, None),
    (T0, None),
    (T0 - 1, None),
    ("x", None),
    (True, None),
])
def test_a_usable_reset_is_in_the_future_and_at_most_eight_days_ahead(reset, expected):
    assert park.usable_reset(reset, T0) == expected


# ------------------------------------------------------------------ the attempt record


def start(store, mid, *, at="2026-09-09T03:00:00Z", probe=None):
    record = {"id": mid, "kind": "message", "from": "alpha", "to": AGENT}
    return store.record_attempt_start(AGENT, record, attempt_id="a1", at=at, usage_probe=probe)


def limit(store, mid, *, at="2026-09-09T03:00:01Z", generation="g1", window="five_hour", reset=None):
    return store.record_attempt_result(
        AGENT, mid, failure_class=park.CLASS_USAGE_LIMIT, summary="limit", at=at,
        usage_limit={"generation": generation, "window": window, "reset_epoch": reset})


def test_the_first_limit_result_parks_and_moves_no_failure_counter(store):
    start(store, "m1")
    rec = limit(store, "m1", reset=CASE1_RESET)
    assert rec["park_state"] == "parked" and rec["parked_generation"] == "g1"
    assert rec["park_count"] == 1 and rec["limit_failures"] == 1
    assert rec["excluded_attempts"] == 1 and rec["attempts_started"] == 1
    assert (rec["infra_failures"], rec["ambiguous_failures"], rec["poison_eligible_failures"]) == (0, 0, 0)
    assert rec["last_failure_class"] == "usage_limit"
    assert rec["in_progress"] is False
    assert (rec["reset_epoch"], rec["wake_epoch"]) == (CASE1_RESET, CASE1_RESET + 30)
    assert rec["notice_key"] == "park:1" and rec["notice_routed"] is False
    assert park.disposal_attempts(rec) == 0


def test_a_first_limit_without_a_usable_reset_has_no_wake(store):
    start(store, "m1")
    rec = limit(store, "m1", reset=None)
    assert "wake_epoch" not in rec and "reset_epoch" not in rec


def test_a_start_probe_is_marked_and_excluded_in_the_same_write_as_the_attempt(store):
    start(store, "m1")
    limit(store, "m1", reset=CASE1_RESET)
    rec = start(store, "m1", probe={"generation": "g2", "consumed_wake": None})
    assert rec["park_state"] == "probing" and rec["probe_marker"] is True and rec["in_progress"] is True
    assert rec["parked_generation"] == "g2" and rec["excluded_attempts"] == 2
    assert rec["attempts_started"] == 2 and park.disposal_attempts(rec) == 0
    assert "probed_wake_epoch" not in rec                      # the wake was not due: kept


def test_a_probe_answered_with_the_same_reset_keeps_the_unused_wake(store):
    start(store, "m1")
    limit(store, "m1", reset=CASE1_RESET)
    start(store, "m1", probe={"generation": "g2", "consumed_wake": None})
    rec = limit(store, "m1", generation="g2", reset=CASE1_RESET)
    assert rec["park_state"] == "parked" and rec["probe_marker"] is False
    assert rec["wake_epoch"] == CASE1_RESET + 30 and park.unused_wake(rec) == CASE1_RESET + 30
    assert rec["limit_failures"] == 2 and rec["park_count"] == 1 and rec["excluded_attempts"] == 2
    assert rec["notice_key"] == "probe:1:2"


def test_a_probe_answered_with_an_earlier_reset_keeps_the_wake_too(store):
    start(store, "m1")
    limit(store, "m1", reset=CASE1_RESET)
    start(store, "m1", probe={"generation": "g2", "consumed_wake": None})
    rec = limit(store, "m1", generation="g2", reset=CASE1_RESET - 3600)
    assert rec["wake_epoch"] == CASE1_RESET + 30 and rec["last_reset_epoch"] == CASE1_RESET


def test_a_later_reset_replaces_the_wake(store):
    start(store, "m1")
    limit(store, "m1", reset=CASE1_RESET)
    start(store, "m1", probe={"generation": "g2", "consumed_wake": CASE1_RESET + 30})
    rec = limit(store, "m1", generation="g2", reset=CASE1_RESET + 18000)
    assert rec["wake_epoch"] == CASE1_RESET + 18030 and rec["last_reset_epoch"] == CASE1_RESET + 18000
    assert park.unused_wake(rec) == CASE1_RESET + 18030


def test_a_consumed_wake_is_never_rearmed_by_the_same_reset(store):
    start(store, "m1")
    limit(store, "m1", reset=CASE1_RESET)
    start(store, "m1", probe={"generation": "g1", "consumed_wake": CASE1_RESET + 30})
    rec = limit(store, "m1", reset=CASE1_RESET)
    assert park.unused_wake(rec) is None
    assert park.wake_due(rec, CASE1_RESET + 10**6) is False


def test_a_probe_with_no_usable_reset_leaves_the_wake_as_it_is(store):
    start(store, "m1")
    limit(store, "m1", reset=CASE1_RESET)
    start(store, "m1", probe={"generation": "g2", "consumed_wake": None})
    rec = limit(store, "m1", generation="g2", reset=None)
    assert park.unused_wake(rec) == CASE1_RESET + 30


def test_wake_due_needs_a_wake_that_has_come_and_is_unused(store):
    start(store, "m1")
    rec = limit(store, "m1", reset=CASE1_RESET)
    assert park.wake_due(rec, CASE1_RESET + 29) is False
    assert park.wake_due(rec, CASE1_RESET + 30) is True
    assert park.wake_due(rec, CASE1_RESET + 99999) is True
    assert park.wake_due({"park_state": "parked"}, CASE1_RESET) is False


def test_a_probe_met_by_another_class_closes_the_park_and_moves_no_counter(store):
    start(store, "m1", at="2026-09-09T03:00:00Z")
    limit(store, "m1", at="2026-09-09T03:00:01Z", reset=CASE1_RESET)
    start(store, "m1", at="2026-09-09T05:00:00Z", probe={"generation": "g2", "consumed_wake": None})
    rec = store.record_attempt_result(AGENT, "m1", failure_class="ambiguous_or_unknown", summary="x",
                                      at="2026-09-09T05:00:05Z", interrupted=True,
                                      interruption_kind="turn_watchdog")
    for key in ("park_state", "probe_marker", "wake_epoch", "reset_epoch", "notice_key", "parked_at"):
        assert key not in rec
    assert rec["ambiguous_failures"] == 0 and rec["infra_failures"] == 0
    assert rec.get("interrupted_watchdog_consecutive", 0) == 0 and rec.get("interrupted_consecutive", 0) == 0
    assert rec["last_failure_class"] == "ambiguous_or_unknown"
    assert rec["parked_seconds_total"] == pytest.approx(2 * 3600 + 4)
    assert rec["excluded_attempts"] == 2 and rec["attempts_started"] == 2
    assert rec["last_reset_epoch"] == CASE1_RESET


def test_the_attempt_after_a_closed_park_counts_normally_and_a_crash_in_it_is_ambiguous(store):
    start(store, "m1")
    limit(store, "m1", reset=CASE1_RESET)
    start(store, "m1", probe={"generation": "g2", "consumed_wake": None})
    store.record_attempt_result(AGENT, "m1", failure_class="known_global_infra", summary="x",
                                at="2026-09-09T05:00:05Z")
    rec = start(store, "m1", at="2026-09-09T05:00:07Z")          # an ordinary attempt
    assert "probe_marker" not in rec and park.disposal_attempts(rec) == 1
    assert store.reconcile_crash_in_progress(AGENT, "m1", at="2026-09-09T05:01:00Z") is True
    rec = store.attempt_record(AGENT, "m1")
    assert rec["ambiguous_failures"] == 1 and rec["last_failure_class"] == "ambiguous_or_unknown"
    assert "park_state" not in rec


def test_a_crash_during_a_probe_parks_again_and_is_never_ambiguous(store):
    start(store, "m1")
    limit(store, "m1", reset=CASE1_RESET)
    start(store, "m1", probe={"generation": "g2", "consumed_wake": CASE1_RESET + 30})
    assert store.reconcile_crash_in_progress(AGENT, "m1", at="2026-09-09T05:01:00Z") is True
    rec = store.attempt_record(AGENT, "m1")
    assert rec["park_state"] == "parked" and rec["probe_marker"] is False and rec["in_progress"] is False
    assert rec["ambiguous_failures"] == 0 and rec["last_failure_class"] == "usage_limit"
    assert rec["parked_generation"] == "g2" and rec["probed_wake_epoch"] == CASE1_RESET + 30
    assert park.wake_due(rec, CASE1_RESET + 10**6) is False    # the wake is not repeated


def test_a_crash_in_an_ordinary_attempt_is_still_ambiguous(store):
    start(store, "m1")
    assert store.reconcile_crash_in_progress(AGENT, "m1", at="2026-09-09T05:01:00Z") is True
    assert store.attempt_record(AGENT, "m1")["ambiguous_failures"] == 1


def test_a_second_park_after_a_closed_one_counts_and_keeps_the_latest_reset(store):
    start(store, "m1")
    limit(store, "m1", reset=CASE1_RESET)
    start(store, "m1", probe={"generation": "g2", "consumed_wake": None})
    store.record_attempt_result(AGENT, "m1", failure_class="known_global_infra", summary="x",
                                at="2026-09-09T05:00:05Z")
    start(store, "m1", at="2026-09-09T05:00:07Z")
    rec = limit(store, "m1", at="2026-09-09T05:00:08Z", reset=CASE1_RESET)
    assert rec["park_count"] == 2 and rec["notice_key"] == "park:2"
    assert "wake_epoch" not in rec                       # the same reset never re-arms a wake
    assert rec["excluded_attempts"] == 3                 # first find, the probe, the second find


def test_closing_a_park_with_the_switch_off_adds_the_time_and_clears_the_marker_flag(store):
    start(store, "m1")
    limit(store, "m1", at="2026-09-09T03:00:01Z", reset=CASE1_RESET)
    assert store.close_usage_park(AGENT, "m1", at="2026-09-09T04:00:01Z") is True
    rec = store.attempt_record(AGENT, "m1")
    assert "park_state" not in rec and rec["parked_seconds_total"] == pytest.approx(3600)
    assert store.close_usage_park(AGENT, "m1", at="2026-09-09T04:00:02Z") is False
    assert store.close_usage_park(AGENT, "nope", at="2026-09-09T04:00:02Z") is False


def test_a_history_that_never_met_a_limit_reads_exactly_as_before(store):
    rec = start(store, "m1")
    assert park.disposal_attempts(rec) == 1 and park.parked_seconds(rec) == 0.0
    rec = store.record_attempt_result(AGENT, "m1", failure_class="known_global_infra", summary="x",
                                      at="2026-09-09T03:00:09Z")
    assert rec["infra_failures"] == 1
    assert not any(key in rec for key in ("park_state", "excluded_attempts", "probe_marker"))


def test_the_notice_bookkeeping_is_its_own(store):
    start(store, "m1")
    limit(store, "m1", reset=CASE1_RESET)
    store.mark_usage_notice(AGENT, "m1", routed=False, next_at_epoch=T0 + 900)
    rec = store.attempt_record(AGENT, "m1")
    assert (rec["notice_routed"], rec["notice_tries"], rec["notice_next_at"]) == (False, 1, T0 + 900)
    assert not rec.get("escalated") and not rec.get("escalation_routed")
    store.mark_usage_notice(AGENT, "m1", routed=True, next_at_epoch=None)
    assert store.attempt_record(AGENT, "m1")["notice_routed"] is True
    store.mark_usage_notice(AGENT, "other", routed=True, next_at_epoch=None)      # no record: no-op


# ------------------------------------------------------------------ the published marker


def publish(store, **over):
    args = {"window": "five_hour", "reset_epoch": CASE1_RESET, "wake_epoch": CASE1_RESET + 30,
            "message_id": "m1", "parked_at": "2026-09-09T03:00:01Z", "wrapper_generation": "g1",
            "now_epoch": T0}
    args.update(over)
    store.write_usage_limit_park(AGENT, **args)


def test_a_fresh_marker_reads_back_validated(store):
    publish(store)
    got = store.read_usage_limit_park(AGENT, now_epoch=T0 + 10)
    assert got["fresh"] is True and got["state"] == "usage_limit_parked"
    assert (got["window"], got["reset_epoch"], got["wake_epoch"]) == ("five_hour", CASE1_RESET, CASE1_RESET + 30)
    assert got["message_id"] == "m1" and got["age_seconds"] == pytest.approx(10)


def test_a_marker_written_with_a_fractional_clock_reading_reads_back(store):
    # the wrapper's clock has microseconds: the update time is not a whole number of seconds
    publish(store, now_epoch=T0 + 0.123456)
    got = store.read_usage_limit_park(AGENT, now_epoch=T0 + 5.5)
    assert got is not None and got["fresh"] is True and got["age_seconds"] == 5     # whole seconds, floored


def test_a_stale_marker_is_still_returned_and_says_it_is_stale(store):
    publish(store)
    got = store.read_usage_limit_park(AGENT, now_epoch=T0 + park.MARKER_STALE_SECONDS + 1)
    assert got is not None and got["fresh"] is False
    assert got["wake_epoch"] == CASE1_RESET + 30
    assert store.read_usage_limit_park(AGENT, now_epoch=T0 + park.MARKER_STALE_SECONDS)["fresh"] is True


def test_a_marker_without_a_known_time_reads_back_without_one(store):
    publish(store, reset_epoch=None, wake_epoch=None)
    got = store.read_usage_limit_park(AGENT, now_epoch=T0)
    assert got["reset_epoch"] is None and got["wake_epoch"] is None


def test_no_marker_is_none_and_clearing_is_quiet(store):
    assert store.read_usage_limit_park(AGENT) is None
    store.clear_usage_limit_park(AGENT)
    publish(store)
    store.clear_usage_limit_park(AGENT)
    assert store.read_usage_limit_park(AGENT) is None


@pytest.mark.parametrize("text", ["", "null", "[]", "{", '{"agent": "beta"}',
                                  '{"agent": "alpha", "state": "usage_limit_parked", "message_id": "m",'
                                  ' "updated_at_epoch": 5}',
                                  '{"agent": "beta", "state": "config_blocked", "message_id": "m",'
                                  ' "updated_at_epoch": 5}',
                                  '{"agent": "beta", "state": "usage_limit_parked", "message_id": "",'
                                  ' "updated_at_epoch": 5}',
                                  '{"agent": "beta", "state": "usage_limit_parked", "message_id": "m",'
                                  ' "updated_at_epoch": "5"}',
                                  '{"agent": "beta", "state": "usage_limit_parked", "message_id": "m",'
                                  ' "updated_at_epoch": 5, "window": "monthly"}'])
def test_an_invalid_marker_reads_as_none(store, text):
    path = store.usage_limit_park_path(AGENT)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    assert store.read_usage_limit_park(AGENT, now_epoch=10) is None


def test_the_marker_carries_no_text_fields(store):
    publish(store)
    import json
    data = json.loads(store.usage_limit_park_path(AGENT).read_text(encoding="utf-8"))
    assert set(data) == {"agent", "state", "window", "reset_epoch", "wake_epoch", "message_id", "parked_at",
                         "wrapper_generation", "updated_at_epoch"}
