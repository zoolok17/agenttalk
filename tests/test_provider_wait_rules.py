"""The pure rules of the two cool-down kinds (``overloaded`` and ``throttled``) in ``wrapper.usage_park``.

Plain values only: no clock, no store, no model. Every 529 and plain-429 result line used here is
MADE UP (no real sample of either exists); each such test says so in its name or a comment.
"""

from __future__ import annotations

import math

import pytest

from agenttalk.wrapper import usage_park as park

AT = "2026-10-05T00:00:00Z"
GEN = "g1"


def _event(status, window="five_hour"):
    return {"type": "rate_limit_event", "rate_limit_info": {"status": status, "rateLimitType": window}}


def _result(is_error):
    return {"type": "result", "is_error": is_error}


def fold(*items):
    """Fold (raw, http_status) pairs through BOTH the merged proven fold and the new one."""
    proven, soft = {}, {}
    for raw, status in items:
        park.note_stream_event(proven, raw)
        park.note_soft_event(soft, raw, status=status)
    return proven, soft


# --------------------------------------------------------------------------- the second fold (the success veto)


def test_the_merged_proven_fold_still_proves_rejected_false_true():
    """Characterization (the merged rule, unchanged): the LAST result after the saved rejection decides."""
    rejected = _event("rejected")
    proven, soft = fold((rejected, None), (_result(False), None), (_result(True), None))
    assert park.fact_from_stream(proven) == {"window": "five_hour", "reset_epoch": None}
    assert park.soft_fact_from_stream(soft) is None or park.soft_fact_from_stream(soft) == park.FACT_THROTTLED
    # the proven fact wins in the drive; the soft fold does not replace or alter the merged state
    assert proven["result_is_error"] is True and "success_at" in soft


def test_an_unknown_status_word_is_a_suspected_limit_and_a_known_one_is_not():
    for word in ("blocked", "weird_new_status", "rejected"):
        _p, soft = fold((_event(word, "mystery_window"), None))
        assert park.soft_fact_from_stream(soft) == park.FACT_THROTTLED, word
    for word in ("allowed", "allowed_warning"):
        _p, soft = fold((_event(word), None))
        assert park.soft_fact_from_stream(soft) is None, word


def test_event_then_a_successful_result_then_exit_gives_no_fact():
    _p, soft = fold((_event("blocked"), None), (_result(False), None))
    assert park.soft_fact_from_stream(soft) is None


def test_event_then_no_result_at_all_is_still_a_suspected_limit_no_proof():
    _p, soft = fold((_event("blocked"), None))
    assert park.soft_fact_from_stream(soft) == park.FACT_THROTTLED


def test_event_then_an_error_result_is_a_suspected_limit():
    _p, soft = fold((_event("blocked"), None), (_result(True), None))
    assert park.soft_fact_from_stream(soft) == park.FACT_THROTTLED


def test_a_made_up_529_result_alone_is_overloaded_and_a_429_is_throttled():
    _p, soft = fold((_result(True), 529))          # MADE UP: no real 529 sample exists
    assert park.soft_fact_from_stream(soft) == park.FACT_OVERLOADED
    _p, soft = fold((_result(True), 429))          # MADE UP: a plain 429 with no rate_limit_event
    assert park.soft_fact_from_stream(soft) == park.FACT_THROTTLED


def test_429_and_529_together_are_throttled():
    _p, soft = fold((_result(True), 529), (_result(True), 429))
    assert park.soft_fact_from_stream(soft) == park.FACT_THROTTLED
    _p, soft = fold((_result(True), 429), (_result(True), 529))
    assert park.soft_fact_from_stream(soft) == park.FACT_THROTTLED


def test_an_error_result_then_a_later_success_gives_none():
    for status in (429, 529):
        _p, soft = fold((_result(True), status), (_result(False), None))
        assert park.soft_fact_from_stream(soft) is None, status


def test_a_success_before_the_event_still_gives_the_fact():
    _p, soft = fold((_result(False), None), (_event("blocked"), None))
    assert park.soft_fact_from_stream(soft) == park.FACT_THROTTLED


def test_a_result_with_a_non_boolean_is_error_is_neither_veto_nor_proof():
    for flag in ("true", 1, 0, None):
        _p, soft = fold((_result(flag), 529))
        assert park.soft_fact_from_stream(soft) is None, flag
        _p, soft = fold((_event("blocked"), None), (_result(flag), None))
        assert park.soft_fact_from_stream(soft) == park.FACT_THROTTLED, flag


def test_a_bool_or_text_status_is_not_a_429_or_a_529():
    for status in (True, "429", "529", 4.29e2, None):
        _p, soft = fold((_result(True), status))
        assert park.soft_fact_from_stream(soft) is None, status


def test_the_fold_keeps_only_numbers_and_never_text():
    raw = _event("blocked")
    raw["rate_limit_info"]["message"] = "SECRET-WORDS-FROM-THE-PROVIDER"
    _p, soft = fold((raw, None), (_result(True), 529))
    assert all(isinstance(v, int) and not isinstance(v, bool) for v in soft.values())
    assert "SECRET" not in repr(soft)


def test_the_folds_are_independent():
    proven, soft = fold((_event("rejected"), None), (_result(True), None))
    assert park.fact_from_stream(proven) is not None
    again = {}
    for raw in (_event("rejected"), _result(True)):
        park.note_stream_event(again, raw)
    assert again == proven                       # the soft fold never touched the merged state


def test_garbage_is_ignored_and_never_raises():
    state = {}
    junk = (None, 5, "x", [], {"type": 5}, {"type": "result", "is_error": []},
            {"type": "rate_limit_event", "rate_limit_info": "no"})
    for raw in junk:
        park.note_soft_event(state, raw, status={"a": 1})
    assert park.soft_fact_from_stream(state) is None
    assert park.soft_fact_from_stream(None) is None and park.soft_fact_from_stream("x") is None


# --------------------------------------------------------------------------- the one arming function


def test_the_schedule_is_15_30_60_60_minutes():
    waits = []
    for step in range(5):
        rec = {}
        park.arm_cooldown_wake(rec, now_epoch=1_000_000, step=step)
        waits.append(rec["wake_epoch"] - 1_000_000)
    assert waits == [900, 1800, 3600, 3600, 3600]


def test_a_fractional_clock_gives_a_whole_wake_that_every_reader_accepts():
    rec = {}
    park.arm_cooldown_wake(rec, now_epoch=1000.9, step=0)
    assert rec["wake_epoch"] == 1900 and park.whole_seconds(rec["wake_epoch"]) == 1900


def test_the_rollback_collision_moves_the_wake_one_second():
    """A probe used wake W; the clock went back so that now + delay == W again: W + 1 is written."""
    rec = {}
    park.arm_cooldown_wake(rec, now_epoch=5000, step=0)
    wake = rec["wake_epoch"]
    rec["probed_wake_epoch"] = wake                        # the probe consumed it
    park.arm_cooldown_wake(rec, now_epoch=wake - 900, step=0)
    assert rec["wake_epoch"] == wake + 1
    assert park.unused_wake(rec) == wake + 1 and park.wake_due(rec, wake + 1)


def test_a_clock_reading_of_two_to_the_63_is_recomputed_once_from_the_real_time():
    rec = {}
    park.arm_cooldown_wake(rec, now_epoch=2 ** 63, step=0)
    assert park.displayable_epoch(rec["wake_epoch"]) is not None
    assert abs(rec["wake_epoch"] - (math.floor(__import__("time").time()) + 900)) <= 5


def test_a_candidate_past_the_displayable_bound_is_recomputed():
    rec = {}
    park.arm_cooldown_wake(rec, now_epoch=park.MAX_DISPLAYABLE_EPOCH - 10, step=1)       # + 1800 would pass the bound
    assert park.displayable_epoch(rec["wake_epoch"]) is not None
    assert rec["wake_epoch"] < park.MAX_DISPLAYABLE_EPOCH - 10


def test_a_damaged_clock_reading_falls_back_to_the_real_time():
    for reading in (None, "later", -5, 0, float("nan"), float("inf"), True, [1]):
        rec = {}
        park.arm_cooldown_wake(rec, now_epoch=reading, step=0)
        assert park.displayable_epoch(rec["wake_epoch"]) is not None, reading


def test_an_invalid_step_counts_as_zero():
    for step in (None, -1, 1.5, "2", True, [0], float("nan")):
        rec = {}
        park.arm_cooldown_wake(rec, now_epoch=10_000, step=step)
        assert rec["wake_epoch"] == 10_900, step


@pytest.mark.parametrize("rec,now,needs", [
    ({}, 1000, True),                                                       # absent
    ({"wake_epoch": "soon"}, 1000, True),                                   # text
    ({"wake_epoch": -5}, 1000, True),                                       # negative
    ({"wake_epoch": True}, 1000, True),                                     # boolean
    ({"wake_epoch": 0}, 1000, True),                                        # zero
    ({"wake_epoch": 1000.5}, 1000, True),                                   # a fraction
    ({"wake_epoch": 1900, "probed_wake_epoch": 1900}, 2000, True),          # consumed
    ({"wake_epoch": 1000 + 3600 + 121}, 1000, True),                        # beyond the repair bound
    ({"wake_epoch": 1000 + 3600 + 120}, 1000, False),                       # just inside the bound: left alone
    ({"wake_epoch": 1900}, 1000, False),                                    # valid, in the future: held
    ({"wake_epoch": 1900}, 2000, False),                                    # valid, due and unused: probed, not armed
])
def test_the_entry_rule_arms_only_a_wake_that_cannot_be_trusted(rec, now, needs):
    assert park.wake_needs_arming(rec, now) is needs


# --------------------------------------------------------------------------- the record: kinds, steps and park_rev


def test_a_first_failure_parks_at_step_zero_with_a_fifteen_minute_wake():
    rec = {"attempts_started": 1}
    park.apply_cooldown_result(rec, at=AT, generation=GEN, kind="throttled", now_epoch=10_000)
    assert (rec["park_state"], rec["park_kind"], rec["cooldown_step"], rec["wake_epoch"]) == (
        "parked", "throttled", 0, 10_900)
    assert (rec["park_count"], rec["excluded_attempts"], rec["park_rev"], rec["notice_key"]) == (1, 1, 1, "rev:1")
    assert "reset_epoch" not in rec and "limit_failures" not in rec          # no usage-limit fields


def test_the_same_kind_again_is_the_next_step_and_changes_neither_rev_nor_notice():
    rec = {}
    park.apply_cooldown_result(rec, at=AT, generation=GEN, kind="overloaded", now_epoch=10_000)
    rec["notice_routed"], rec["notice_tries"] = True, 1
    rec["park_state"] = park.PROBING                             # a probe is in flight
    park.apply_cooldown_result(rec, at="later", generation=GEN, kind="overloaded", now_epoch=11_000)
    assert (rec["cooldown_step"], rec["wake_epoch"], rec["park_rev"]) == (1, 11_000 + 1800, 1)
    assert (rec["notice_key"], rec["notice_routed"], rec["notice_tries"]) == ("rev:1", True, 1)
    assert rec["parked_at"] == AT and rec["park_count"] == 1 and rec["excluded_attempts"] == 1


def test_a_change_of_kind_restarts_the_steps_raises_the_rev_and_renews_the_notice_but_keeps_parked_at():
    rec = {}
    park.apply_cooldown_result(rec, at=AT, generation=GEN, kind="overloaded", now_epoch=10_000, detail="status_529")
    rec["notice_routed"], rec["notice_tries"] = True, 4
    park.apply_cooldown_result(rec, at="later", generation=GEN, kind="throttled", now_epoch=20_000)
    assert (rec["park_kind"], rec["cooldown_step"], rec["wake_epoch"], rec["park_rev"]) == ("throttled", 0, 20_900, 2)
    assert (rec["notice_key"], rec["notice_routed"], rec["notice_tries"], rec["notice_next_at"]) == (
        "rev:2", False, 0, None)
    assert rec["parked_at"] == AT and "park_detail" not in rec
    park.apply_cooldown_result(rec, at="later", generation=GEN, kind="overloaded", now_epoch=30_000)       # A, B, A
    assert (rec["park_rev"], rec["notice_key"]) == (3, "rev:3")


def test_the_detail_is_a_closed_token_or_absent_and_is_never_free_text():
    rec = {}
    park.apply_cooldown_result(rec, at=AT, generation=GEN, kind="throttled", now_epoch=10_000,
                               detail="status_429.rate_limit_error")
    assert rec["park_detail"] == "status_429.rate_limit_error"
    for bad in (None, "has space", "x" * 300, 7, ["a"], ""):
        park.apply_cooldown_result(rec, at=AT, generation=GEN, kind="throttled", now_epoch=10_000, detail=bad)
        assert "park_detail" not in rec, bad


def test_an_unknown_kind_is_refused():
    with pytest.raises(ValueError):
        park.apply_cooldown_result({}, at=AT, generation=GEN, kind="usage_limit", now_epoch=1)


def test_a_usage_limit_only_history_never_gets_a_rev_or_a_kind():
    rec = {}
    park.apply_limit_result(rec, at=AT, generation=GEN, window="five_hour", reset_epoch=50_000, provider="claude")
    assert "park_rev" not in rec and "park_kind" not in rec and rec["notice_key"] == "park:1"
    rec["park_state"] = park.PROBING
    park.apply_limit_result(rec, at=AT, generation=GEN, window="five_hour", reset_epoch=60_000, provider="claude")
    assert "park_rev" not in rec and rec["notice_key"].startswith("probe:")


def test_usage_limit_then_a_cooldown_then_usage_limit_each_change_raises_the_rev():
    rec = {}
    park.apply_limit_result(rec, at=AT, generation=GEN, window="five_hour", reset_epoch=50_000, provider="claude")
    rec["park_state"] = park.PROBING
    park.apply_cooldown_result(rec, at=AT, generation=GEN, kind="throttled", now_epoch=10_000)
    assert rec["park_rev"] == 1 and "limit_window" not in rec and rec["last_reset_epoch"] == 50_000
    assert rec["parked_at"] == AT and rec["park_count"] == 1                            # one park, one wait
    rec["park_state"] = park.PROBING
    park.apply_limit_result(rec, at=AT, generation=GEN, window="seven_day", reset_epoch=70_000, provider="claude")
    assert (rec["park_rev"], rec["notice_key"]) == (2, "rev:2")
    assert "park_kind" not in rec and "cooldown_step" not in rec
    assert rec["wake_epoch"] == 70_030          # the old wake is cleared; the reset rule applies
    assert park.park_kind(rec) == "usage_limit"


def test_a_proven_limit_after_a_cooldown_with_an_old_reset_leaves_no_wake_for_the_next_start():
    rec = {}
    park.apply_limit_result(rec, at=AT, generation=GEN, window="five_hour", reset_epoch=50_000, provider="claude")
    rec["park_state"] = park.PROBING
    park.apply_cooldown_result(rec, at=AT, generation=GEN, kind="overloaded", now_epoch=10_000)
    rec["park_state"] = park.PROBING
    park.apply_limit_result(rec, at=AT, generation=GEN, window="five_hour", reset_epoch=50_000, provider="claude")
    assert "wake_epoch" not in rec                       # the same reset as before: a consumed wake is never re-armed


def test_a_new_usage_limit_park_in_a_history_that_used_a_cooldown_is_a_new_transition():
    rec = {}
    park.apply_cooldown_result(rec, at=AT, generation=GEN, kind="throttled", now_epoch=10_000)
    park.apply_park_close(rec, at_epoch=None)
    assert "park_kind" not in rec and "cooldown_step" not in rec and "wake_epoch" not in rec and rec["park_rev"] == 1
    park.apply_limit_result(rec, at=AT, generation=GEN, window="five_hour", reset_epoch=None, provider="claude")
    assert (rec["park_rev"], rec["notice_key"]) == (2, "rev:2")


def test_closing_a_park_removes_every_cooldown_field_but_keeps_the_lifetime_counters():
    rec = {"attempts_started": 3}
    park.apply_cooldown_result(rec, at=AT, generation=GEN, kind="overloaded", now_epoch=10_000, detail="status_529")
    park.apply_park_close(rec, at_epoch=None)
    for key in ("park_state", "park_kind", "cooldown_step", "park_detail", "wake_epoch", "notice_key", "parked_at"):
        assert key not in rec, key
    assert rec["park_rev"] == 1 and rec["park_count"] == 1 and rec["excluded_attempts"] == 1


def test_a_crash_during_a_cooldown_probe_arms_the_wake_again_at_the_same_step():
    rec = {}
    park.apply_cooldown_result(rec, at=AT, generation=GEN, kind="throttled", now_epoch=1000)
    park.apply_cooldown_result(rec, at=AT, generation=GEN, kind="throttled", now_epoch=2000)           # step 1
    rec.update({"in_progress": True, "probe_marker": True, "park_state": park.PROBING})
    rec["probed_wake_epoch"] = rec["wake_epoch"]
    park.apply_crash_reconcile(rec, 5000)
    assert (rec["park_state"], rec["in_progress"], rec["cooldown_step"]) == ("parked", False, 1)
    assert rec["wake_epoch"] == 5000 + 1800 and park.unused_wake(rec) == rec["wake_epoch"]


def test_a_crash_with_the_clock_back_never_writes_the_wake_the_dead_probe_used():
    rec = {}
    park.apply_cooldown_result(rec, at=AT, generation=GEN, kind="overloaded", now_epoch=1000)
    used = rec["wake_epoch"]
    rec.update({"in_progress": True, "probe_marker": True, "park_state": park.PROBING, "probed_wake_epoch": used})
    park.apply_crash_reconcile(rec, used - 900)
    assert rec["wake_epoch"] == used + 1 and park.wake_due(rec, used + 1)


def test_a_crash_during_a_usage_limit_probe_is_unchanged():
    rec = {"park_state": park.PROBING, "probe_marker": True, "in_progress": True, "wake_epoch": 5}
    park.apply_crash_reconcile(rec)
    assert rec == {"park_state": "parked", "probe_marker": False, "in_progress": False, "wake_epoch": 5}


def test_the_kind_reads_as_usage_limit_unless_it_is_one_of_the_two_words():
    for value in (None, "usage_limit", "other", 7, ["overloaded"]):
        assert park.park_kind({"park_kind": value}) == "usage_limit"
    assert park.park_kind(None) == "usage_limit" and park.park_kind({}) == "usage_limit"
    assert park.park_kind({"park_kind": "overloaded"}) == "overloaded" and park.is_cooldown({"park_kind": "throttled"})
    assert not park.is_cooldown({"park_kind": "usage_limit"})


def test_the_cooldown_changes_no_failure_counter():
    rec = {"attempts_started": 5, "infra_failures": 2, "ambiguous_failures": 1, "poison_eligible_failures": 0}
    before = dict(rec)
    park.apply_cooldown_result(rec, at=AT, generation=GEN, kind="throttled", now_epoch=1000)
    for key in ("attempts_started", "infra_failures", "ambiguous_failures", "poison_eligible_failures"):
        assert rec[key] == before[key]
    assert park.disposal_attempts(rec) == 4                 # the attempt that found the wait is excluded
