"""The wrapper loop cools a message down when the provider is overloaded or throttling, instead of retrying it hard.

Fake spawners, a fake wall clock and fake sleeps only. The 529 result and the plain-429 result are MADE UP (no real
sample of either exists); the two real captured Claude cases appear only where a proven usage limit is the point.
Nothing here starts a model or sleeps for real.
"""

from __future__ import annotations

import json

import pytest

from agenttalk.wrapper import loop, run, session
from agenttalk.wrapper import usage_park as park
from test_provider_wait_drive import event, made_up_529, made_up_plain_429, result
from test_usage_park_loop import (AGENT, CASE1_RESET, CASE1_WAKE, T0, Clock, Crash, Seen, Spawner, case1, failed_turn,
                                  head, infra_turn, ledger, make_store, ok_turn)


def go(store, spawner, clock, *, polls, generation="g1", park_on=True, routed=True, seen=None, now_iso=None,
       **loop_kw):
    """Run the loop for ``polls`` polls; the health hook accepts the optional cause detail."""
    seen = seen or Seen()

    def escalate(info):
        seen.notices.append(info)
        seen.events.append(("notice", info.get("failure_class")))
        return routed

    drive = run.make_drive(
        store, AGENT, "claude", session.SessionState(cli="claude", claude_session_id="sid-1"), ["claude"],
        spawn=spawner, clock=clock.now, render=False, heartbeat=lambda: seen.events.append("drive-stamp"),
        usage_limit_park=park_on)

    def counted(record):
        seen.events.append("drive")
        outcome = drive(record)
        seen.events.append("drive-end")
        return outcome

    def health(rec, reason, reason_detail=None):
        seen.events.append(("health", reason, reason_detail))

    seen.turns = loop.run_loop(
        store, AGENT, counted, clock=clock.now, sleep=clock.sleep, now_iso=now_iso or clock.iso, max_polls=polls,
        wrapper_generation=generation, usage_limit_park=park_on, on_escalate=escalate,
        heartbeat=lambda: seen.events.append("stamp"), on_health_parked=health,
        on_runtime_idle=lambda: seen.events.append("idle"), **loop_kw)
    return seen


def throttled_run(tmp_path, *, now=T0, polls=6):
    store = make_store(tmp_path)
    spawner = Spawner(made_up_plain_429())
    clock = Clock(now)
    seen = go(store, spawner, clock, polls=polls)
    return store, spawner, clock, seen


def parked_overloaded(tmp_path, *, now=T0):
    store = make_store(tmp_path)
    spawner = Spawner(made_up_529())
    clock = Clock(now)
    seen = go(store, spawner, clock, polls=12)
    return store, spawner, clock, seen


# --------------------------------------------------------------------------- the quick retries and the first park


def test_a_throttle_parks_on_the_first_failure_with_no_retry_and_no_marker(tmp_path):
    store, spawner, clock, seen = throttled_run(tmp_path, polls=30)
    rec = ledger(store)
    assert spawner.calls == 1
    assert (rec["park_state"], rec["park_kind"], rec["cooldown_step"], rec["wake_epoch"]) == (
        "parked", "throttled", 0, T0 + 900)
    assert (rec["attempts_started"], rec["excluded_attempts"], rec["park_rev"], rec["notice_key"]) == (1, 1, 1, "rev:1")
    assert (rec["infra_failures"], rec["ambiguous_failures"], rec["poison_eligible_failures"]) == (0, 0, 0)
    assert rec["last_failure_class"] == "known_global_infra" and "limit_window" not in rec and "reset_epoch" not in rec
    assert not store.usage_limit_park_path(AGENT).exists()              # a cool-down writes no marker
    assert store.dead_lettered_count(AGENT) == 0 and head(store).body == "one" and store.cursor(AGENT) == ""
    assert seen.turns == 0


def test_health_says_provider_wait_with_the_closed_cause_and_never_usage_limit(tmp_path):
    _store, _spawner, _clock, seen = throttled_run(tmp_path, polls=4)
    healths = [e for e in seen.events if isinstance(e, tuple) and e[0] == "health"]
    assert healths and set(healths) == {("health", "provider_wait_parked", "status_429")}
    assert not any(e[1] == "usage_limit_parked" for e in healths)


def test_the_order_of_a_cooldown_is_idle_then_health_then_notice_then_stamp(tmp_path):
    _store, _spawner, _clock, seen = throttled_run(tmp_path, polls=3)
    names = seen.names()
    after = names[names.index("drive-end") + 1:]
    assert after[:4] == ["idle", "health", "notice", "stamp"] and "drive" not in after


def test_one_notice_on_entering_the_kind_and_none_on_later_polls(tmp_path):
    _store, _spawner, _clock, seen = throttled_run(tmp_path, polls=30)
    assert len(seen.notices) == 1
    facts = seen.notices[0]["usage_limit"]
    assert (facts["kind"], facts["notice_key"], facts["next_try_epoch"], facts["window"]) == (
        "throttled", "rev:1", T0 + 900, None)


def test_an_overload_gets_exactly_two_quick_retries_then_parks_on_the_third(tmp_path):
    store = make_store(tmp_path)
    spawner = Spawner(made_up_529())
    clock = Clock(T0)
    go(store, spawner, clock, polls=1)
    rec = ledger(store)
    assert (spawner.calls, rec.get("soft_run"), rec["attempts_started"], rec["infra_failures"]) == (1, 1, 1, 1)
    assert "park_state" not in rec
    go(store, spawner, clock, polls=1)
    rec = ledger(store)
    assert (spawner.calls, rec.get("soft_run"), rec["attempts_started"], rec["infra_failures"]) == (2, 2, 2, 2)
    assert "park_state" not in rec
    go(store, spawner, clock, polls=1)
    rec = ledger(store)
    assert spawner.calls == 3 and rec["park_state"] == "parked" and rec["park_kind"] == "overloaded"
    assert (rec["attempts_started"], rec["excluded_attempts"], rec["infra_failures"]) == (3, 1, 2)
    assert "soft_run" not in rec and rec["cooldown_step"] == 0 and rec["park_detail"] == "status_529"
    # nothing more is spawned while it cools down
    go(store, spawner, clock, polls=20)
    assert spawner.calls == 3 and store.dead_lettered_count(AGENT) == 0


def test_the_quick_retries_are_ordinary_attempts_with_the_loops_own_back_off_and_no_notice(tmp_path):
    store = make_store(tmp_path)
    clock = Clock(T0)
    seen = go(store, Spawner(made_up_529()), clock, polls=2)
    assert clock.sleeps == [0.3, 0.6] and not seen.notices
    assert [e for e in seen.events if isinstance(e, tuple)] == []


def test_any_other_result_ends_the_run_of_overloads(tmp_path):
    store = make_store(tmp_path)
    spawner = Spawner(made_up_529(), infra_turn(), made_up_529(), made_up_529(), made_up_529())
    clock = Clock(T0)
    go(store, spawner, clock, polls=1)
    assert ledger(store)["soft_run"] == 1
    go(store, spawner, clock, polls=1)                                   # a plain 500
    assert "soft_run" not in ledger(store)
    go(store, spawner, clock, polls=1)
    assert ledger(store)["soft_run"] == 1 and "park_state" not in ledger(store)
    go(store, spawner, clock, polls=1)
    assert ledger(store)["soft_run"] == 2 and "park_state" not in ledger(store)
    go(store, spawner, clock, polls=1)
    assert ledger(store)["park_state"] == "parked"


def test_an_overload_that_succeeds_on_a_quick_retry_clears_the_record(tmp_path):
    store = make_store(tmp_path)
    spawner = Spawner(made_up_529(), ok_turn())
    go(store, spawner, Clock(T0), polls=2)
    assert store.cursor(AGENT) != "" and store.attempt_record(AGENT, store.cursor(AGENT)) is None


# --------------------------------------------------------------------------- the ceilings (revision 1, B2)


def _prior_history(store, **fields):
    record = head(store)
    store.record_attempt_start(AGENT, {"id": record.id, "request_id": None}, attempt_id="a" * 12,
                               at="2026-09-09T00:00:00Z")
    data = store.dead_letter_attempts(AGENT)
    rec = data["messages"][record.id]
    rec.update(in_progress=False, **fields)
    store._write_attempts(AGENT, data)


def test_with_nineteen_prior_eligible_attempts_the_first_quick_retry_reaches_the_escalation(tmp_path):
    store = make_store(tmp_path)
    _prior_history(store, attempts_started=19, infra_failures=19, last_failure_class="known_global_infra")
    go(store, Spawner(made_up_529()), Clock(T0), polls=1, k_escalate=20, k_poison=0)
    rec = ledger(store)
    # asserted, so that a later change of this ceiling is deliberate
    assert rec["attempts_started"] == 20 and rec.get("escalated") is True
    assert store.dead_lettered_count(AGENT) == 0


def test_with_ninety_eight_prior_attempts_and_four_hours_the_second_quick_retry_reaches_the_disposal(tmp_path):
    store = make_store(tmp_path)
    _prior_history(store, attempts_started=98, infra_failures=98, last_failure_class="known_global_infra",
                   first_started_at="2026-09-08T00:00:00Z")
    spawner = Spawner(made_up_529())
    clock = Clock(T0)
    go(store, spawner, clock, polls=1, k_escalate=0, k_poison=0)
    assert store.dead_lettered_count(AGENT) == 0 and ledger(store)["attempts_started"] == 99
    go(store, spawner, clock, polls=1, k_escalate=0, k_poison=0)
    assert store.dead_lettered_count(AGENT) == 1


def test_with_a_clean_history_neither_quick_retry_reaches_a_ceiling(tmp_path):
    store = make_store(tmp_path)
    go(store, Spawner(made_up_529()), Clock(T0), polls=3, k_escalate=20, k_poison=0, infra_exhaust_min_attempts=100)
    rec = ledger(store)
    assert rec["park_state"] == "parked" and not rec.get("escalated") and store.dead_lettered_count(AGENT) == 0


def test_once_the_cooldown_has_started_nothing_disposes_whatever_the_history(tmp_path):
    store, spawner, clock, _seen = parked_overloaded(tmp_path)
    data = store.dead_letter_attempts(AGENT)
    rec = data["messages"][head(store).id]
    rec.update(attempts_started=98, infra_failures=97, excluded_attempts=1, first_started_at="2026-09-08T00:00:00Z")
    store._write_attempts(AGENT, data)
    for _ in range(6):
        clock.t = float(ledger(store)["wake_epoch"])
        go(store, spawner, clock, polls=3, k_escalate=3, k_poison=2, noninfra_sub_ceiling=2,
           infra_exhaust_min_attempts=1, infra_exhaust_after_seconds=1.0)
        assert store.dead_lettered_count(AGENT) == 0 and ledger(store)["park_state"] == "parked"


def test_many_hours_of_cooldown_move_no_disposal_counter(tmp_path):
    """T-F3: with a fake clock that spends many hours cooling down, every disposal rule reads only eligible values."""
    store, spawner, clock, _seen = throttled_run(tmp_path)
    for _ in range(9):                                       # about eight hours of 15/30/60-minute waits
        clock.t = float(ledger(store)["wake_epoch"])
        go(store, spawner, clock, polls=1, k_escalate=3, k_poison=2, noninfra_sub_ceiling=2,
           infra_exhaust_min_attempts=1, infra_exhaust_after_seconds=1.0)
    rec = ledger(store)
    assert clock.t - T0 > 6 * 3600 and rec["park_state"] == "parked" and store.dead_lettered_count(AGENT) == 0
    assert rec["attempts_started"] == 10 and rec["excluded_attempts"] == 10 and park.disposal_attempts(rec) == 0
    assert (rec["infra_failures"], rec["ambiguous_failures"], rec["poison_eligible_failures"]) == (0, 0, 0)
    assert not loop._infra_retry_exhausted(rec, now_text=clock.iso(), after_seconds=1.0, min_attempts=1)
    # then an ambiguous failure closes the park: the whole wait stays out of the 4-hour rule
    go(store, Spawner(failed_turn()), Clock(float(rec["wake_epoch"])), polls=1, k_escalate=0, k_poison=0)
    closed = ledger(store)
    assert "park_state" not in closed and closed["parked_seconds_total"] > 6 * 3600
    assert not loop._infra_retry_exhausted(
        closed, now_text=clock.iso(), after_seconds=14400.0, min_attempts=1)


# --------------------------------------------------------------------------- the schedule and the saved time


def test_the_waits_are_15_30_60_60_60_minutes_and_each_wake_is_probed_once(tmp_path):
    store, spawner, clock, _seen = throttled_run(tmp_path)
    calls = spawner.calls
    wakes, steps = [ledger(store)["wake_epoch"]], [0]
    for _ in range(4):
        clock.t = float(wakes[-1] - 1)
        go(store, spawner, clock, polls=3)
        assert spawner.calls == calls, "tried before it was due"
        clock.t = float(wakes[-1])
        go(store, spawner, clock, polls=5)                                   # many polls at the due time: one probe
        calls += 1
        assert spawner.calls == calls
        rec = ledger(store)
        assert rec["probed_wake_epoch"] == wakes[-1] and rec["park_state"] == "parked"
        wakes.append(rec["wake_epoch"])
        steps.append(rec["cooldown_step"])
    assert [b - a for a, b in zip(wakes, wakes[1:], strict=False)] == [1800, 3600, 3600, 3600]
    assert steps == [0, 1, 2, 3, 4]
    rec = ledger(store)
    assert (rec["attempts_started"], rec["excluded_attempts"], rec["park_rev"]) == (5, 5, 1)   # same kind: one revision
    assert rec["notice_key"] == "rev:1" and rec["parked_at"] == park.epoch_iso(T0)


def test_a_restart_before_the_saved_time_keeps_cooling_and_does_not_probe(tmp_path):
    store, spawner, clock, _seen = throttled_run(tmp_path)
    clock.t = T0 + 60
    go(store, spawner, clock, polls=6, generation="g2")
    assert spawner.calls == 1 and ledger(store)["park_state"] == "parked"


def test_a_restart_after_the_saved_time_probes_once(tmp_path):
    store, spawner, clock, _seen = throttled_run(tmp_path)
    clock.t = float(T0 + 901)
    go(store, spawner, clock, polls=6, generation="g2")
    assert spawner.calls == 2


def test_a_crash_during_the_probe_arms_the_wake_again_at_the_same_step_and_is_not_ambiguous(tmp_path):
    store, spawner, clock, _seen = throttled_run(tmp_path)
    wake = ledger(store)["wake_epoch"]
    clock.t = float(wake)
    with pytest.raises(Crash):
        go(store, Spawner(Crash("died")), clock, polls=1, generation="g2")
    clock.t = float(wake + 5)
    quiet = Spawner(made_up_plain_429())
    go(store, quiet, clock, polls=4, generation="g3")                        # the next start: re-armed, held
    rec = ledger(store)
    assert quiet.calls == 0 and rec["park_state"] == "parked" and rec["cooldown_step"] == 0
    assert rec["wake_epoch"] == wake + 5 + 900 and rec["ambiguous_failures"] == 0
    assert rec["probed_wake_epoch"] == wake


def test_a_crash_with_the_clock_gone_back_never_writes_the_wake_the_dead_probe_used(tmp_path):
    store, spawner, clock, _seen = throttled_run(tmp_path)
    wake = ledger(store)["wake_epoch"]
    clock.t = float(wake)
    with pytest.raises(Crash):
        go(store, Spawner(Crash("died")), clock, polls=1, generation="g2")
    clock.t = float(wake - 900)
    quiet = Spawner(made_up_plain_429())
    go(store, quiet, clock, polls=3, generation="g3")
    rec = ledger(store)
    assert rec["wake_epoch"] == wake + 1 and quiet.calls == 0 and park.wake_due(rec, wake + 1)


@pytest.mark.parametrize("damage", [
    {"wake_epoch": None}, {"wake_epoch": "soon"}, {"wake_epoch": -4}, {"wake_epoch": True}, {"wake_epoch": 0},
    {"wake_epoch": 1788900000.5}, {"wake_epoch": 10 ** 12}, {"probed_wake_epoch": "same-as-wake"}])
def test_a_wake_that_cannot_be_trusted_is_armed_again_once_and_the_head_is_not_probed_at_once(tmp_path, damage):
    store, spawner, clock, _seen = throttled_run(tmp_path)
    data = store.dead_letter_attempts(AGENT)
    rec = data["messages"][head(store).id]
    for key, value in damage.items():
        if value is None:
            rec.pop(key, None)
        elif value == "same-as-wake":
            rec[key] = rec["wake_epoch"]
        else:
            rec[key] = value
    store._write_attempts(AGENT, data)
    clock.t = float(T0 + 20)
    go(store, spawner, clock, polls=6, generation="g2")
    rec = ledger(store)
    assert spawner.calls == 1, "probed at once"
    assert rec["wake_epoch"] == T0 + 20 + 900 and rec["cooldown_step"] == 0
    before = rec["wake_epoch"]
    go(store, spawner, clock, polls=6, generation="g2")                      # the next polls hold: never re-armed again
    assert ledger(store)["wake_epoch"] == before and spawner.calls == 1


def test_a_valid_wake_is_never_armed_again_on_a_poll(tmp_path):
    store, spawner, clock, _seen = throttled_run(tmp_path)
    before = ledger(store)["wake_epoch"]
    for step in range(5):
        clock.t = T0 + 30 * step
        go(store, spawner, clock, polls=2, generation="g2")
    assert ledger(store)["wake_epoch"] == before


# --------------------------------------------------------------------------- the kinds change and close


def test_a_proven_usage_limit_on_a_probe_upgrades_the_kind_clears_the_old_wake_and_brings_the_marker_back(tmp_path):
    store, spawner, clock, _seen = throttled_run(tmp_path)
    wake = ledger(store)["wake_epoch"]
    clock.t = float(wake)
    seen = go(store, Spawner(case1()), clock, polls=2, generation="g1")
    rec = ledger(store)
    assert "park_kind" not in rec and "cooldown_step" not in rec and "park_detail" not in rec
    assert (rec["limit_window"], rec["reset_epoch"], rec["wake_epoch"]) == ("five_hour", CASE1_RESET, CASE1_WAKE)
    assert (rec["park_rev"], rec["notice_key"], rec["parked_at"]) == (2, "rev:2", park.epoch_iso(T0))
    assert store.read_usage_limit_park(AGENT, now_epoch=wake) is not None
    assert ("health", "usage_limit_parked") in [e[:2] for e in seen.events if isinstance(e, tuple) and e[0] == "health"]


def test_a_probe_of_a_usage_limit_that_meets_an_overload_downgrades_it_and_drops_the_marker(tmp_path):
    store = make_store(tmp_path)
    clock = Clock(T0)
    go(store, Spawner(case1()), clock, polls=2)
    first = ledger(store)
    assert store.usage_limit_park_path(AGENT).exists()
    go(store, Spawner(made_up_529()), clock, polls=1, generation="g2")        # the start probe meets an overload
    rec = ledger(store)
    assert (rec["park_kind"], rec["cooldown_step"], rec["park_rev"]) == ("overloaded", 0, 1)
    assert rec["last_reset_epoch"] == CASE1_RESET and "limit_window" not in rec and "reset_epoch" not in rec
    assert (rec["parked_at"], rec["park_count"]) == (first["parked_at"], 1)
    assert rec["wake_epoch"] == T0 + 900 and not store.usage_limit_park_path(AGENT).exists()
    assert (rec["notice_key"], rec["notice_routed"], rec["notice_tries"]) == ("rev:1", True, 1)


def test_a_probe_that_meets_another_fact_changes_the_kind_and_renews_the_notice(tmp_path):
    store, spawner, clock, seen = parked_overloaded(tmp_path)
    assert [n["usage_limit"]["kind"] for n in seen.notices] == ["overloaded"]
    clock.t = float(ledger(store)["wake_epoch"])
    seen = go(store, Spawner(made_up_plain_429()), clock, polls=2, seen=seen)
    rec = ledger(store)
    assert (rec["park_kind"], rec["cooldown_step"], rec["park_rev"], rec["notice_key"]) == ("throttled", 0, 2, "rev:2")
    assert [n["usage_limit"]["kind"] for n in seen.notices] == ["overloaded", "throttled"]


def test_an_exhausted_or_routed_notice_never_silences_the_next_kind(tmp_path):
    store, spawner, clock, seen = parked_overloaded(tmp_path)
    data = store.dead_letter_attempts(AGENT)
    data["messages"][head(store).id].update(notice_routed=True, notice_tries=park.NOTICE_MAX_TRIES)
    store._write_attempts(AGENT, data)
    clock.t = float(ledger(store)["wake_epoch"])
    seen = go(store, Spawner(made_up_plain_429()), clock, polls=2, seen=seen)
    assert seen.notices[-1]["usage_limit"]["kind"] == "throttled"


def test_a_probe_with_no_fact_closes_the_park_and_the_head_is_driven_normally(tmp_path):
    for label, steps in (("ambiguous", failed_turn()), ("plain 500", infra_turn())):
        store, spawner, clock, _seen = throttled_run(tmp_path / label.replace(" ", "-"))
        clock.t = float(ledger(store)["wake_epoch"])
        go(store, Spawner(steps), clock, polls=1)
        rec = ledger(store)
        for key in ("park_state", "park_kind", "cooldown_step", "park_detail", "wake_epoch", "notice_key", "parked_at"):
            assert key not in rec, (label, key)
        assert rec["parked_seconds_total"] > 0 and rec["park_rev"] == 1 and rec["excluded_attempts"] == 2, label
        assert rec["ambiguous_failures"] == 0 and rec["infra_failures"] == 0, label      # the probe moved no counter


def test_a_probe_that_succeeds_clears_the_record(tmp_path):
    store, _spawner, clock, _seen = throttled_run(tmp_path)
    clock.t = float(ledger(store)["wake_epoch"])
    go(store, Spawner(ok_turn()), clock, polls=1)
    assert store.cursor(AGENT) != "" and not store.dead_letter_attempts(AGENT)["messages"]


def test_the_operator_skipping_the_message_drops_the_record(tmp_path):
    store, _spawner, _clock, _seen = throttled_run(tmp_path)
    mid = head(store).id
    store.set_cursor(AGENT, mid)
    store.gc_attempts_below(AGENT, store.cursor(AGENT))
    assert store.attempt_record(AGENT, mid) is None


# --------------------------------------------------------------------------- the switch off


def test_with_the_switch_off_a_throttle_and_an_overload_retry_as_today(tmp_path):
    for label, events in (("429", made_up_plain_429()), ("529", made_up_529()),
                          ("unknown", [event("a_status_we_do_not_know"), result(True)])):
        store = make_store(tmp_path / label)
        spawner = Spawner(events)
        go(store, spawner, Clock(T0), polls=12, park_on=False)
        rec = ledger(store)
        assert spawner.calls >= 4 and "park_state" not in rec and "park_kind" not in rec, label
        assert "soft_run" not in rec and "park_rev" not in rec, label


def test_with_the_switch_off_a_persisted_cooldown_is_closed_and_the_head_is_driven(tmp_path):
    store, _spawner, clock, _seen = throttled_run(tmp_path)
    spawner = Spawner(infra_turn())
    go(store, spawner, clock, polls=2, park_on=False)
    rec = ledger(store)
    assert spawner.calls >= 1
    for key in ("park_state", "park_kind", "cooldown_step", "wake_epoch", "park_detail"):
        assert key not in rec, key
    assert rec["excluded_attempts"] == 1 and rec["parked_seconds_total"] > 0
    assert park.disposal_attempts(rec) == rec["attempts_started"] - 1


# --------------------------------------------------------------------------- local causes keep their class


def test_a_watchdog_never_starts_a_cooldown(tmp_path):
    store = make_store(tmp_path)
    drive_calls = []

    def drive(record):
        drive_calls.append(1)
        return loop.DriveOutcome(ok=False, failure_class=loop.CLASS_AMBIGUOUS, summary="watchdog", interrupted=True,
                                 interruption_kind="turn_watchdog", limit_fact=None)

    clock = Clock(T0)
    loop.run_loop(store, AGENT, drive, clock=clock.now, sleep=clock.sleep, now_iso=clock.iso, max_polls=1,
                  wrapper_generation="g1", usage_limit_park=True)
    rec = ledger(store)
    assert "park_state" not in rec and "park_kind" not in rec and len(drive_calls) == 1


def test_a_cooldown_fact_on_an_interrupted_or_non_infra_outcome_is_ignored(tmp_path):
    for label, kwargs in (("interrupted", {"failure_class": loop.CLASS_INFRA, "interrupted": True}),
                          ("ambiguous", {"failure_class": loop.CLASS_AMBIGUOUS})):
        store = make_store(tmp_path / label)

        def drive(record, kwargs=kwargs):
            return loop.DriveOutcome(ok=False, summary="x", limit_fact="throttled", limit_provider="claude", **kwargs)

        clock = Clock(T0)
        loop.run_loop(store, AGENT, drive, clock=clock.now, sleep=clock.sleep, now_iso=clock.iso, max_polls=2,
                      wrapper_generation="g1", usage_limit_park=True)
        assert "park_state" not in ledger(store), label


# --------------------------------------------------------------------------- the record on disk


def test_the_cooldown_record_is_plain_json_with_closed_words_and_numbers_only(tmp_path):
    store, _spawner, _clock, _seen = throttled_run(tmp_path)
    text = json.dumps(ledger(store))
    for forbidden in ("made-up", "overload text", "hit your"):
        assert forbidden not in text
    assert ledger(store)["park_detail"] == "status_429"


# --------------------------------------------------------------------------- fix round 1


REAL_NOW = 1791200000                       # a "real" clock reading, fixed for the test
FAR_FUTURE = "9999-12-31T23:59:59Z"         # a clock reading no cool-down wake can be built from


def _real_clock(monkeypatch):
    state = {"t": float(REAL_NOW)}
    monkeypatch.setattr(park.time, "time", lambda: state["t"])
    return state


def _far():
    return FAR_FUTURE


def test_a_far_future_clock_reading_never_starts_a_try_from_a_repaired_wake(tmp_path, monkeypatch):
    """F1: the wake is armed from the real time, so it is compared with the real time, never the rejected reading."""
    state = _real_clock(monkeypatch)
    store, spawner, clock, _seen = throttled_run(tmp_path)
    data = store.dead_letter_attempts(AGENT)
    data["messages"][head(store).id].pop("wake_epoch")                              # the wake is absent
    store._write_attempts(AGENT, data)
    calls = spawner.calls
    go(store, spawner, clock, polls=6, generation="g2", now_iso=_far)               # several polls with the bad reading
    assert spawner.calls == calls, "a repaired wake started a try at once"
    assert ledger(store)["wake_epoch"] == REAL_NOW + 900
    go(store, spawner, clock, polls=4, generation="g2", now_iso=_far)           # still held: not due by the real time
    assert spawner.calls == calls
    state["t"] = float(REAL_NOW + 901)
    go(store, spawner, clock, polls=1, generation="g2", now_iso=_far)           # due by the validated clock: one try
    assert spawner.calls == calls + 1
    go(store, spawner, clock, polls=4, generation="g2", now_iso=_far)
    assert spawner.calls == calls + 1 and ledger(store)["park_state"] == "parked"


def test_the_same_through_the_crash_re_arm_path(tmp_path, monkeypatch):
    state = _real_clock(monkeypatch)
    store, spawner, clock, _seen = throttled_run(tmp_path)
    clock.t = float(ledger(store)["wake_epoch"])
    with pytest.raises(Crash):
        go(store, Spawner(Crash("died")), clock, polls=1, generation="g2")         # a crash in the probe, normal clock
    quiet = Spawner(made_up_plain_429())
    go(store, quiet, clock, polls=5, generation="g3", now_iso=_far)                # the next start reads the bad clock
    rec = ledger(store)
    assert quiet.calls == 0 and rec["park_state"] == "parked"
    assert rec["wake_epoch"] == REAL_NOW + 900 and rec["ambiguous_failures"] == 0
    state["t"] = float(REAL_NOW + 901)
    go(store, quiet, clock, polls=1, generation="g3", now_iso=_far)
    assert quiet.calls == 1


def test_a_normal_clock_reading_is_used_as_it_is(tmp_path, monkeypatch):
    _real_clock(monkeypatch)
    store, spawner, clock, _seen = throttled_run(tmp_path)
    wake = ledger(store)["wake_epoch"]
    assert wake == T0 + 900                                                         # armed from the reading
    clock.t = float(wake - 1)
    go(store, spawner, clock, polls=3)
    assert spawner.calls == 1
    clock.t = float(wake)
    go(store, spawner, clock, polls=1)
    assert spawner.calls == 2


def _notices(seen):
    return [n["usage_limit"]["notice_key"] for n in seen.notices]


def test_after_a_return_to_a_proven_limit_a_same_kind_probe_keeps_the_notice_tuple(tmp_path):
    """F2: with park_rev active, the notice bookkeeping is renewed only on a transition."""
    store, spawner, clock, seen = throttled_run(tmp_path)                           # throttled: rev:1
    clock.t = float(ledger(store)["wake_epoch"])
    go(store, Spawner(case1()), clock, polls=2, generation="g1", seen=seen)          # a proven limit: rev:2
    rec = ledger(store)
    assert (rec["park_rev"], rec["notice_key"], rec["notice_routed"], rec["notice_tries"]) == (2, "rev:2", True, 1)
    for generation in ("g2", "g3"):                                                  # two same-kind probes (restarts)
        go(store, Spawner(case1()), clock, polls=2, generation=generation, seen=seen)
        again = ledger(store)
        assert again["park_rev"] == 2
        assert (again["notice_key"], again["notice_routed"], again["notice_tries"]) == ("rev:2", True, 1)
    assert _notices(seen) == ["rev:1", "rev:2"]                                      # no notice on a same-kind probe


def test_a_usage_limit_only_history_still_gets_a_probe_notice_key_each_time(tmp_path):
    store = make_store(tmp_path)
    clock = Clock(T0)
    seen = go(store, Spawner(case1()), clock, polls=2)
    go(store, Spawner(case1()), clock, polls=2, generation="g2", seen=seen)
    rec = ledger(store)
    assert "park_rev" not in rec and rec["notice_key"] == "probe:1:2"
    assert _notices(seen) == ["park:1", "probe:1:2"]
