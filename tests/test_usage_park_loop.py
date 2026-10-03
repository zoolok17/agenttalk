"""The wrapper loop parks a message on a proven usage limit instead of retrying it.

Fake spawners, a fake wall clock and fake sleeps only: the two real captured Claude
cases (sanitised, see ``golden_stop_retries_scenarios``) and edits of them. Nothing here starts
a model or sleeps for real.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import golden_stop_retries_scenarios as real
from agenttalk.store import Store
from agenttalk.wrapper import loop, run, session
from agenttalk.wrapper import usage_park as park

T0 = 1788900000                 # before case 1's reset (1788948000)
CASE1_RESET = 1788948000
CASE1_WAKE = CASE1_RESET + 30
CASE2_RESET = 1790370000
CASE2_NOW = 1790300000          # case 2's reset is 70000 s ahead (the weekly reset at 17 days is not)
AGENT = "beta"


class Crash(Exception):
    """A stand-in for the wrapper process dying in the middle of a turn."""


class Clock:
    """A fake wall clock: sleeping advances it; ``jumps`` {sleep number: new time} moves it."""

    def __init__(self, t, jumps=None):
        self.t = float(t)
        self.sleeps: list[float] = []
        self.jumps = dict(jumps or {})

    def now(self):
        return self.t

    def sleep(self, delay):
        self.sleeps.append(delay)
        self.t += delay
        if len(self.sleeps) in self.jumps:
            self.t = float(self.jumps[len(self.sleeps)])

    def iso(self):
        return park.epoch_iso(self.t)


class Stream(list):
    returncode = None
    watchdog_result = None


def case1():
    return copy.deepcopy(real.REAL_CASE_FIVE_HOUR)


def case2():
    return copy.deepcopy(real.REAL_CASE_SEVEN_DAY)


def ok_turn():
    return [json.loads(line) for line in real.claude_turn()]


def failed_turn():
    return [json.loads(line) for line in real.claude_turn(error=True)]


def infra_turn():
    """A provider outage that is NOT a usage limit: no rejected usage event."""
    return [{"type": "result", "subtype": "error_during_execution", "is_error": True, "api_error_status": 529}]


class Spawner:
    """Hands out one scripted stream per spawn (the last step repeats); counts spawns."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.calls = 0

    def __call__(self, argv, stdin):
        step = self.steps[min(self.calls, len(self.steps) - 1)]
        self.calls += 1
        if isinstance(step, BaseException):
            raise step
        return Stream(json.dumps(event) for event in step)


class Seen:
    """Everything a run reported through the loop's hooks, in order."""

    def __init__(self):
        self.events: list = []
        self.notices: list[dict] = []

    def names(self):
        return [e if isinstance(e, str) else e[0] for e in self.events]


def make_store(tmp_path: Path, bodies=("one",)) -> Store:
    store = Store(tmp_path)
    store.init(["alpha", AGENT])
    for body in bodies:
        store.send(sender="alpha", recipient=AGENT, body=body)
    return store


def go(store, spawner, clock, *, polls, generation="g1", park_on=True, routed=True, seen=None, **loop_kw):
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

    turns = loop.run_loop(
        store, AGENT, counted, clock=clock.now, sleep=clock.sleep, now_iso=clock.iso, max_polls=polls,
        wrapper_generation=generation, usage_limit_park=park_on, on_escalate=escalate,
        heartbeat=lambda: seen.events.append("stamp"),
        on_health_parked=lambda rec, reason: seen.events.append(("health", reason)),
        on_runtime_idle=lambda: seen.events.append("idle"), **loop_kw)
    seen.turns = turns
    return seen


def head(store):
    return store.messages_for(AGENT)[0]


def ledger(store):
    return store.attempt_record(AGENT, head(store).id)


# ------------------------------------------------------------------ the two real cases park


@pytest.mark.parametrize("events,now,window,reset", [
    (case1, T0, "five_hour", CASE1_RESET),
    (case2, CASE2_NOW, "seven_day", CASE2_RESET),
])
def test_a_real_case_parks_after_one_attempt_and_is_never_disposed(tmp_path, events, now, window, reset):
    store = make_store(tmp_path)
    spawner = Spawner(events())
    clock = Clock(now)
    seen = go(store, spawner, clock, polls=40)
    rec = ledger(store)
    assert spawner.calls == 1                                 # zero retries
    assert rec["park_state"] == "parked" and rec["limit_window"] == window
    assert (rec["reset_epoch"], rec["wake_epoch"]) == (reset, reset + 30)
    assert rec["attempts_started"] == 1 and rec["excluded_attempts"] == 1
    assert (rec["infra_failures"], rec["ambiguous_failures"], rec["poison_eligible_failures"]) == (0, 0, 0)
    assert rec["last_failure_class"] == "usage_limit" and not rec.get("escalated")
    assert store.dead_lettered_count(AGENT) == 0 and store.cursor(AGENT) == ""
    assert head(store).body == "one"                          # the message is kept at the head
    assert seen.turns == 0


def test_the_weekly_trap_is_never_poison_even_with_the_tightest_ceilings(tmp_path):
    store = make_store(tmp_path)
    spawner = Spawner(case2())
    go(store, spawner, Clock(CASE2_NOW), polls=60, k_poison=1, k_escalate=1, noninfra_sub_ceiling=1,
       infra_exhaust_min_attempts=1, infra_exhaust_after_seconds=0)
    rec = ledger(store)
    assert spawner.calls == 1 and store.dead_lettered_count(AGENT) == 0
    assert rec["poison_eligible_failures"] == 0 and rec["park_state"] == "parked"


def test_the_order_of_a_park_is_idle_then_health_then_notice_then_stamp(tmp_path):
    store = make_store(tmp_path)
    seen = go(store, Spawner(case1()), Clock(T0), polls=3)
    names = seen.names()
    first = names.index("drive-end")
    after = names[first + 1:]
    # the failed turn is already recorded; only then idle, health, notice, and the heartbeat last
    assert after[:4] == ["idle", "health", "notice", "stamp"]
    assert "drive" not in after                                # no second drive while parked
    assert ledger(store)["notice_key"] == "park:1"


def test_every_parked_poll_stamps_after_it_completes_and_sleeps_back_off(tmp_path):
    store = make_store(tmp_path)
    clock = Clock(T0)
    seen = go(store, Spawner(case1()), clock, polls=8)
    assert seen.names().count("stamp") == 8                    # one per poll, each after its work
    assert clock.sleeps[:3] == [0.3, 0.6, 1.2] and max(clock.sleeps) == 2.0


def test_health_says_outage_wait_with_its_own_reason_never_config_blocked(tmp_path):
    from agenttalk.wrapper.health import WrapperHealthWriter

    store = make_store(tmp_path)
    health = WrapperHealthWriter(store, AGENT, "claude", mode="wrapper", min_interval=0.0)
    drive = run.make_drive(
        store, AGENT, "claude", session.SessionState(cli="claude", claude_session_id="sid-1"), ["claude"],
        spawn=Spawner(case1()), clock=lambda: 0.0, render=False, heartbeat=lambda: None,
        health_writer=health, usage_limit_park=True)
    clock = Clock(T0)
    loop.run_loop(store, AGENT, drive, clock=clock.now, sleep=clock.sleep, now_iso=clock.iso, max_polls=4,
                  usage_limit_park=True, on_health_parked=health.parked, wrapper_generation="g1")
    snapshot = store.read_health_raw(AGENT)
    assert snapshot["state"] == "rate_limited_or_outage" and snapshot["reason_code"] == "usage_limit_parked"
    assert store.read_config_blocked_hold(AGENT) is None
    assert not store.usage_limit_park_path(AGENT).with_name("x").exists()


# ------------------------------------------------------------------ not a limit: nothing parks


def test_rejected_then_success_parks_nothing_and_the_turn_completes(tmp_path):
    store = make_store(tmp_path)
    events = [case1()[1]] + ok_turn()
    seen = go(store, Spawner(events), Clock(T0), polls=10, max_turns=1)
    assert seen.turns == 1 and store.attempt_record(AGENT, "x") is None
    assert not store.usage_limit_park_path(AGENT).exists()


class FailingSpawner(Spawner):
    """A spawner whose child exits nonzero or is killed by the watchdog."""

    def __init__(self, *steps, rc=None, watchdog=None):
        super().__init__(*steps)
        self.rc = rc
        self.watchdog = watchdog

    def __call__(self, argv, stdin):
        stream = super().__call__(argv, stdin)
        stream.returncode = self.rc
        stream.watchdog_result = self.watchdog
        return stream


def test_rejected_then_success_with_a_nonzero_exit_or_watchdog_keeps_todays_retrying(tmp_path):
    for label, rc, watchdog in (("exit", 1, None), ("watchdog", None, {"summary": "hung tool killed"})):
        store = make_store(tmp_path / label)
        events = case1()
        events[-1]["is_error"] = False
        spawner = FailingSpawner(events, rc=rc, watchdog=watchdog)
        go(store, spawner, Clock(T0), polls=12)
        assert spawner.calls >= 3, label                      # retried as today (a watchdog head even disposes)
        assert "usage_limit" not in json.dumps(store.dead_letter_attempts(AGENT)), label
        assert not store.usage_limit_park_path(AGENT).exists(), label


def test_no_terminal_result_parks_nothing(tmp_path):
    store = make_store(tmp_path)
    spawner = Spawner(case1()[:-1])
    go(store, spawner, Clock(T0), polls=12)
    assert spawner.calls > 3 and "park_state" not in ledger(store)


def test_prose_about_a_limit_never_parks(tmp_path):
    store = make_store(tmp_path)
    spawner = Spawner([{"type": "result", "is_error": True, "api_error_status": 429,
                        "result": "You've hit your weekly limit. Usage limit reached."}])
    go(store, spawner, Clock(T0), polls=12)
    assert spawner.calls > 3 and "park_state" not in ledger(store)


# ------------------------------------------------------------------ restarts and the wake


def parked_run(tmp_path, *, now=T0, steps=None):
    store = make_store(tmp_path)
    spawner = Spawner(*(steps or [case1()]))
    go(store, spawner, Clock(now), polls=3, generation="g1")
    return store, spawner


def test_the_marker_a_real_parked_wrapper_publishes_names_claude_and_its_version(tmp_path):
    store, _ = parked_run(tmp_path)
    marker = store.read_usage_limit_park(AGENT, now_epoch=T0)
    assert marker is not None and marker["provider"] == "claude" and marker["schema_version"] == 1


def test_a_restart_before_the_wake_gives_one_start_probe_and_keeps_the_unused_wake(tmp_path):
    store, spawner = parked_run(tmp_path)
    go(store, spawner, Clock(T0 + 1000), polls=10, generation="g2")
    rec = ledger(store)
    assert spawner.calls == 2                                  # exactly one start probe
    assert rec["wake_epoch"] == CASE1_WAKE and "probed_wake_epoch" not in rec
    assert rec["park_state"] == "parked" and rec["parked_generation"] == "g2"
    assert rec["excluded_attempts"] == 2 and rec["limit_failures"] == 2 and rec["park_count"] == 1
    go(store, spawner, Clock(T0 + 2000), polls=10, generation="g2")      # same generation: no probe
    assert spawner.calls == 2


def test_the_wake_fires_once_at_the_stated_time_within_one_run(tmp_path):
    store, spawner = parked_run(tmp_path)
    clock = Clock(T0 + 1000, jumps={3: CASE1_WAKE - 100})
    go(store, spawner, clock, polls=6, generation="g1")
    assert spawner.calls == 1 and clock.t < CASE1_WAKE         # nearly there: nothing yet
    clock = Clock(CASE1_WAKE)                                  # exactly at the wake time
    go(store, spawner, clock, polls=10, generation="g1")
    rec = ledger(store)
    assert spawner.calls == 2 and rec["probed_wake_epoch"] == CASE1_WAKE
    assert park.unused_wake(rec) is None                       # the same reset never re-arms it
    go(store, spawner, Clock(CASE1_WAKE + 99999), polls=10, generation="g1")
    assert spawner.calls == 2                                  # no loop


def test_a_wake_that_comes_during_a_run_probes_without_a_restart(tmp_path):
    store, spawner = parked_run(tmp_path)
    clock = Clock(T0 + 1000, jumps={4: CASE1_WAKE + 5})
    go(store, spawner, clock, polls=14, generation="g1")
    assert spawner.calls == 2 and ledger(store)["probed_wake_epoch"] == CASE1_WAKE


def test_a_restart_after_the_wake_consumes_both_triggers_with_one_attempt(tmp_path):
    store, spawner = parked_run(tmp_path)
    go(store, spawner, Clock(CASE1_WAKE + 600), polls=12, generation="g2")
    rec = ledger(store)
    assert spawner.calls == 2                                  # one attempt, not two
    assert rec["probed_wake_epoch"] == CASE1_WAKE and rec["excluded_attempts"] == 2


def test_a_restart_exactly_at_the_wake_time_also_probes_once(tmp_path):
    store, spawner = parked_run(tmp_path)
    go(store, spawner, Clock(CASE1_WAKE), polls=12, generation="g2")
    assert spawner.calls == 2 and ledger(store)["probed_wake_epoch"] == CASE1_WAKE


def test_a_restart_after_a_consumed_wake_gives_a_start_probe_only(tmp_path):
    store, spawner = parked_run(tmp_path)
    go(store, spawner, Clock(CASE1_WAKE + 1), polls=6, generation="g2")        # consumed
    go(store, spawner, Clock(CASE1_WAKE + 100), polls=6, generation="g3")
    rec = ledger(store)
    assert spawner.calls == 3 and rec["probed_wake_epoch"] == CASE1_WAKE and rec["excluded_attempts"] == 3


def test_a_later_reset_after_a_wake_schedules_exactly_one_new_wake(tmp_path):
    store, spawner = parked_run(tmp_path)
    later = case1()
    later[1]["rate_limit_info"]["resetsAt"] = CASE1_RESET + 18000
    later[1]["rate_limit_info"]["unifiedWindows"]["five_hour"]["resetsAt"] = CASE1_RESET + 18000
    spawner.steps = [later]
    go(store, spawner, Clock(CASE1_WAKE + 5), polls=10, generation="g1")
    rec = ledger(store)
    assert spawner.calls == 2 and rec["wake_epoch"] == CASE1_RESET + 18030
    assert park.unused_wake(rec) == CASE1_RESET + 18030
    go(store, spawner, Clock(CASE1_RESET + 18031), polls=10, generation="g1")
    assert spawner.calls == 3 and ledger(store)["probed_wake_epoch"] == CASE1_RESET + 18030


@pytest.mark.parametrize("reset", [T0 - 5, T0 + 9 * 86400, "soon", None, True])
def test_an_unusable_reset_gives_no_timed_wake(tmp_path, reset):
    store = make_store(tmp_path)
    events = case1()
    events[1]["rate_limit_info"]["resetsAt"] = reset
    events[1]["rate_limit_info"]["unifiedWindows"]["five_hour"]["resetsAt"] = reset
    spawner = Spawner(events)
    go(store, spawner, Clock(T0), polls=5)
    rec = ledger(store)
    assert rec["park_state"] == "parked" and "wake_epoch" not in rec
    go(store, spawner, Clock(T0 + 30 * 86400), polls=5, generation="g1")
    assert spawner.calls == 1                                  # no timed wake, however long it waits


def test_a_crash_during_the_wake_attempt_parks_again_and_never_repeats_the_wake(tmp_path):
    store, spawner = parked_run(tmp_path)
    crashing = Spawner(Crash("the wrapper died"))
    with pytest.raises(Crash):
        go(store, crashing, Clock(CASE1_WAKE + 1), polls=5, generation="g2")
    rec = ledger(store)
    assert rec["in_progress"] is True and rec["probe_marker"] is True
    assert rec["probed_wake_epoch"] == CASE1_WAKE                # written ahead of the launch
    again = Spawner(case1())
    go(store, again, Clock(CASE1_WAKE + 60), polls=8, generation="g3")
    rec = ledger(store)
    assert again.calls == 1                                      # one start probe; the wake is not repeated
    assert rec["ambiguous_failures"] == 0 and rec["park_state"] == "parked"
    assert rec["last_failure_class"] == "usage_limit"
    go(store, again, Clock(CASE1_WAKE + 5000), polls=8, generation="g3")
    assert again.calls == 1


# ------------------------------------------------------------------ excluded history


def test_a_probe_that_succeeds_clears_the_park_and_completes_the_turn(tmp_path):
    store, _ = parked_run(tmp_path)
    seen = go(store, Spawner(ok_turn()), Clock(T0 + 100), polls=10, generation="g2", max_turns=1)
    assert seen.turns == 1 and store.cursor(AGENT) != ""
    assert not store.usage_limit_park_path(AGENT).exists()


def test_excluded_history_then_ambiguous_failures_dispose_only_on_real_attempts(tmp_path):
    store, _ = parked_run(tmp_path)
    failing = Spawner(failed_turn())
    go(store, failing, Clock(T0 + 100), polls=60, generation="g2", k_escalate=3, k_poison=0)
    # the probe counts for nothing: the ceiling of 3 is reached by 3 ordinary attempts
    assert failing.calls == 4
    assert store.dead_lettered_count(AGENT) == 1
    assert store.list_dead_letters(AGENT)[0]["class"] == "ambiguous_or_unknown"


def test_the_probe_failure_itself_moves_no_counter_and_closes_the_park(tmp_path):
    store, _ = parked_run(tmp_path)
    failing = Spawner(failed_turn())
    go(store, failing, Clock(T0 + 100), polls=1, generation="g2", k_escalate=50, k_poison=0)
    rec = ledger(store)
    assert failing.calls == 1
    assert rec["ambiguous_failures"] == 0 and rec["infra_failures"] == 0 and "park_state" not in rec
    assert "probe_marker" not in rec and rec["last_failure_class"] == "ambiguous_or_unknown"
    assert rec["parked_seconds_total"] > 0 and not store.usage_limit_park_path(AGENT).exists()


def test_a_crash_in_the_attempt_after_a_closed_park_is_ambiguous(tmp_path):
    store, _ = parked_run(tmp_path)
    go(store, Spawner(failed_turn()), Clock(T0 + 100), polls=1, generation="g2", k_escalate=50, k_poison=0)
    with pytest.raises(Crash):
        go(store, Spawner(Crash("died")), Clock(T0 + 200), polls=1, generation="g2", k_escalate=50, k_poison=0)
    store.reconcile_crash_in_progress(AGENT, head(store).id, at="2026-09-09T05:00:00Z")
    rec = ledger(store)
    assert rec["ambiguous_failures"] == 1 and rec["last_failure_class"] == "ambiguous_or_unknown"


def test_excluded_history_then_infra_failures_do_not_trip_the_infra_exhaustion_early(tmp_path):
    store, _ = parked_run(tmp_path)
    infra = Spawner(infra_turn())
    # five hours parked, then plain infra failures: attempts count from the real ones and the
    # parked hours do not count toward the 4-hour rule (here: 1 hour)
    go(store, infra, Clock(T0 + 5 * 3600), polls=12, generation="g2", infra_exhaust_min_attempts=3,
       infra_exhaust_after_seconds=3600.0, k_escalate=0)
    assert store.dead_lettered_count(AGENT) == 0 and infra.calls >= 10


def test_the_same_infra_history_without_a_park_does_dispose(tmp_path):
    # the control: a plain history of the same attempts over the same wall time disposes
    store = make_store(tmp_path)
    infra = Spawner(infra_turn())
    clock = Clock(T0)
    go(store, infra, clock, polls=3, generation="g1", infra_exhaust_min_attempts=3,
       infra_exhaust_after_seconds=3600.0, k_escalate=0)
    clock = Clock(T0 + 5 * 3600)
    go(store, infra, clock, polls=3, generation="g1", infra_exhaust_min_attempts=3,
       infra_exhaust_after_seconds=3600.0, k_escalate=0)
    assert store.dead_lettered_count(AGENT) == 1


def test_disposal_is_never_evaluated_while_a_park_is_open_even_with_a_heavy_history(tmp_path):
    store, spawner = parked_run(tmp_path)
    data = store.dead_letter_attempts(AGENT)
    rec = data["messages"][head(store).id]
    rec.update(attempts_started=40, ambiguous_failures=39, infra_failures=0)   # 39 real earlier failures
    store._write_attempts(AGENT, data)
    go(store, spawner, Clock(T0 + 100), polls=3, generation="g2", k_escalate=20, k_poison=0,
       noninfra_sub_ceiling=5)
    assert spawner.calls == 2 and store.dead_lettered_count(AGENT) == 0      # probed, not disposed
    assert ledger(store)["park_state"] == "parked"


def test_a_history_that_never_met_a_limit_counts_exactly_as_before(tmp_path):
    store = make_store(tmp_path)
    failing = Spawner(failed_turn())
    go(store, failing, Clock(T0), polls=60, k_escalate=3, k_poison=0)
    assert failing.calls == 3 and store.dead_lettered_count(AGENT) == 1


def test_switching_off_with_a_persisted_park_drives_the_head_as_before(tmp_path):
    store, _ = parked_run(tmp_path)
    spawner = Spawner(case1())
    go(store, spawner, Clock(T0 + 100), polls=8, generation="g2", park_on=False)
    rec = ledger(store)
    assert spawner.calls == 8                                    # retried every poll, as today
    assert "park_state" not in rec and rec["parked_seconds_total"] > 0
    assert not store.usage_limit_park_path(AGENT).exists()


def test_switching_off_never_lets_parked_history_count_toward_disposal(tmp_path):
    store, _ = parked_run(tmp_path)
    spawner = Spawner(case1())
    go(store, spawner, Clock(T0 + 100), polls=40, generation="g2", park_on=False,
       infra_exhaust_min_attempts=3, infra_exhaust_after_seconds=0.0)
    assert spawner.calls == 3                                    # three real attempts, not two
    assert store.dead_lettered_count(AGENT) == 1


def test_a_negative_excluded_attempts_cannot_dispose_a_message_tried_only_once(tmp_path):
    """#311 round 2, finding 3 (connector 4175000411): a damaged or hand-edited ledger with
    attempts_started=1, excluded_attempts=-20 reads as 21 eligible attempts with the old,
    unclamped subtraction - enough to cross the default disposal limit (20) and dead-letter a
    message that had only ever been tried once, even with the switch OFF entirely."""
    store = make_store(tmp_path)
    mid = head(store).id
    store.record_attempt_start(AGENT, {"id": mid}, attempt_id="a1", at=park.epoch_iso(T0))
    store.record_attempt_result(AGENT, mid, failure_class="ambiguous_or_unknown", summary="", at=park.epoch_iso(T0))
    attempts = store.dead_letter_attempts(AGENT)
    attempts["messages"][mid]["excluded_attempts"] = -20
    store._write_attempts(AGENT, attempts)
    spawner = Spawner(ok_turn())
    go(store, spawner, Clock(T0 + 1), polls=1, park_on=False)
    assert store.dead_lettered_count(AGENT) == 0


# ------------------------------------------------------------------ the notice


def test_one_notice_per_park_transition_with_a_stable_key_and_the_usage_facts(tmp_path):
    store = make_store(tmp_path)
    seen = go(store, Spawner(case1()), Clock(T0), polls=30)
    assert len(seen.notices) == 1
    info = seen.notices[0]
    assert info["failure_class"] == "usage_limit" and info["usage_limit"]["notice_key"] == "park:1"
    assert info["usage_limit"] == {"notice_key": "park:1", "window": "five_hour", "reset_epoch": CASE1_RESET,
                                   "wake_epoch": CASE1_WAKE, "again": False}
    rec = ledger(store)
    assert rec["notice_routed"] is True and rec["notice_tries"] == 1


def test_a_probe_that_is_limited_again_sends_a_still_limited_notice(tmp_path):
    store = make_store(tmp_path)
    spawner = Spawner(case1())
    go(store, spawner, Clock(T0), polls=5, generation="g1")
    seen = go(store, spawner, Clock(T0 + 600), polls=5, generation="g2")
    assert [n["usage_limit"]["notice_key"] for n in seen.notices] == ["probe:1:2"]
    assert seen.notices[0]["usage_limit"]["again"] is True


def test_an_earlier_escalation_on_the_same_message_does_not_silence_the_notice(tmp_path):
    store = make_store(tmp_path)
    go(store, Spawner(failed_turn()), Clock(T0), polls=2, k_escalate=0, k_poison=0)
    store.mark_attempt_escalated(AGENT, head(store).id, routed=True)
    seen = go(store, Spawner(case1()), Clock(T0 + 10), polls=10, generation="g2")
    assert [n["failure_class"] for n in seen.notices] == ["usage_limit"]


def test_an_unrouted_notice_is_retried_a_bounded_number_of_times(tmp_path):
    store = make_store(tmp_path)
    clock = Clock(T0)
    seen = go(store, Spawner(case1()), clock, polls=2, routed=False)
    assert len(seen.notices) == 1
    for _ in range(11):                                            # a day of fake time, in 15-minute jumps
        clock.t += 960
        go(store, Spawner(case1()), clock, polls=1, routed=False, seen=seen, generation="g1")
    assert len(seen.notices) == park.NOTICE_MAX_TRIES
    rec = ledger(store)
    assert rec["notice_routed"] is False and rec["notice_tries"] == park.NOTICE_MAX_TRIES


def test_no_notice_target_means_no_notice_calls_and_no_crash(tmp_path):
    store = make_store(tmp_path)
    drive = run.make_drive(
        store, AGENT, "claude", session.SessionState(cli="claude", claude_session_id="sid-1"), ["claude"],
        spawn=Spawner(case1()), clock=lambda: 0.0, render=False, heartbeat=lambda: None, usage_limit_park=True)
    clock = Clock(T0)
    loop.run_loop(store, AGENT, drive, clock=clock.now, sleep=clock.sleep, now_iso=clock.iso, max_polls=5,
                  usage_limit_park=True, wrapper_generation="g1")
    assert ledger(store)["park_state"] == "parked"


def test_a_notice_that_raises_never_crashes_the_loop(tmp_path):
    store = make_store(tmp_path)

    def explode(info):
        raise RuntimeError("no route")

    drive = run.make_drive(
        store, AGENT, "claude", session.SessionState(cli="claude", claude_session_id="sid-1"), ["claude"],
        spawn=Spawner(case1()), clock=lambda: 0.0, render=False, heartbeat=lambda: None, usage_limit_park=True)
    clock = Clock(T0)
    loop.run_loop(store, AGENT, drive, clock=clock.now, sleep=clock.sleep, now_iso=clock.iso, max_polls=5,
                  usage_limit_park=True, wrapper_generation="g1", on_escalate=explode)
    assert ledger(store)["notice_routed"] is False and ledger(store)["notice_tries"] >= 1


# ------------------------------------------------------------------ the published marker


def test_the_marker_is_published_refreshed_and_removed_when_the_head_is_consumed(tmp_path):
    store = make_store(tmp_path, bodies=("one", "two"))
    clock = Clock(T0)
    go(store, Spawner(case1()), clock, polls=4)
    marker = store.read_usage_limit_park(AGENT, now_epoch=clock.t)
    assert marker["window"] == "five_hour" and marker["wake_epoch"] == CASE1_WAKE and marker["fresh"] is True
    assert marker["message_id"] == head(store).id
    first_update = marker["updated_at_epoch"]
    # a minute and a half later (same run) the marker is refreshed; and rewritten if it vanished
    clock2 = Clock(T0 + 100)
    go(store, Spawner(case1()), clock2, polls=2, generation="g1")
    assert store.read_usage_limit_park(AGENT, now_epoch=clock2.t)["updated_at_epoch"] > first_update
    store.clear_usage_limit_park(AGENT)
    go(store, Spawner(case1()), Clock(T0 + 200), polls=2, generation="g1")
    assert store.read_usage_limit_park(AGENT, now_epoch=T0 + 200) is not None
    # the operator skips the parked head: the next poll removes the marker
    store.advance_cursor(AGENT, head(store).id)
    go(store, Spawner(ok_turn()), Clock(T0 + 300), polls=3, generation="g1", max_turns=1)
    assert not store.usage_limit_park_path(AGENT).exists()


def test_a_marker_stat_failure_after_the_first_publication_never_stops_the_loop(tmp_path, monkeypatch):
    """#311 blocker 1: the marker's path lookup, existence check and write are ONE
    failure-isolated step (``loop._publish_marker``). A PermissionError/OSError from the
    existence check ALONE (not only from the write) must never escape - the heartbeat keeps
    stamping, the cursor stays unchanged (nothing is disposed) and no extra model run starts,
    even though the marker itself cannot be refreshed while the failure is injected."""
    store = make_store(tmp_path)
    clock = Clock(T0)
    go(store, Spawner(case1()), clock, polls=2)           # the first, successful publication
    assert store.read_usage_limit_park(AGENT, now_epoch=clock.t) is not None

    marker_path = store.usage_limit_park_path(AGENT)
    real_exists = Path.exists

    def denied(self):
        if self == marker_path:
            raise PermissionError("marker stat temporarily denied")
        return real_exists(self)

    cursor_before = store.cursor(AGENT)
    monkeypatch.setattr(Path, "exists", denied)
    spawner = Spawner(case1())
    seen = go(store, spawner, clock, polls=4)
    assert spawner.calls == 0                              # no extra model run while parked
    assert seen.names().count("stamp") == 4                # the heartbeat never stopped
    assert store.cursor(AGENT) == cursor_before             # the cursor never moved
    rec = ledger(store)
    assert rec["park_state"] == "parked"                   # still parked, nothing disposed


def test_a_marker_published_under_a_real_fractional_clock_is_readable(tmp_path):
    store = make_store(tmp_path)
    clock = Clock(T0 + 0.25)                       # the wrapper's own clock has microseconds
    go(store, Spawner(case1()), clock, polls=3)
    marker = store.read_usage_limit_park(AGENT, now_epoch=clock.t)
    assert marker is not None and marker["fresh"] is True and marker["wake_epoch"] == CASE1_WAKE


def test_a_stale_marker_stays_visible_and_says_it_is_stale(tmp_path):
    store = make_store(tmp_path)
    clock = Clock(T0)
    go(store, Spawner(case1()), clock, polls=3)
    # the wrapper stops; nothing refreshes the marker
    later = clock.t + 10 * 60
    marker = store.read_usage_limit_park(AGENT, now_epoch=later)
    assert marker is not None and marker["fresh"] is False and marker["wake_epoch"] == CASE1_WAKE


def test_the_marker_is_cleared_before_a_probe_starts_and_rewritten_when_it_fails(tmp_path):
    store = make_store(tmp_path)
    seen_during_probe: list = []
    spawner = Spawner(case1())

    class Watching(Spawner):
        def __call__(self, argv, stdin):
            seen_during_probe.append(store.usage_limit_park_path(AGENT).exists())
            return spawner(argv, stdin)

    watching = Watching()
    go(store, watching, Clock(T0), polls=3, generation="g1")
    go(store, watching, Clock(T0 + 100), polls=3, generation="g2")
    assert seen_during_probe == [False, False]                  # never a parked view while a turn runs
    assert store.read_usage_limit_park(AGENT, now_epoch=T0 + 100)["fresh"] is True


def test_a_new_wrapper_start_removes_a_marker_for_a_head_that_is_no_longer_parked(tmp_path):
    store = make_store(tmp_path)
    go(store, Spawner(case1()), Clock(T0), polls=3)
    store.clear_attempt(AGENT, head(store).id)                  # e.g. the record was requeued
    go(store, Spawner(ok_turn()), Clock(T0 + 100), polls=3, generation="g2", max_turns=1)
    assert not store.usage_limit_park_path(AGENT).exists()


# ------------------------------------------------------------------ not the config_blocked park


def test_a_usage_park_writes_no_config_blocked_state_and_a_gateway_hold_writes_no_usage_state(tmp_path):
    store = make_store(tmp_path)
    go(store, Spawner(case1()), Clock(T0), polls=4)
    assert store.read_config_blocked_hold(AGENT) is None
    assert ledger(store)["last_failure_class"] != loop.CLASS_CONFIG_BLOCKED

    other = make_store(tmp_path / "gw")

    def held(argv, stdin):
        raise run._GatewayChildCapUnavailable("held", transient=True)

    seen = go(other, held, Clock(T0), polls=4)
    assert not other.usage_limit_park_path(AGENT).exists()
    assert other.attempt_record(AGENT, head(other).id) is None   # the gateway hold keeps no attempt
    assert "notice" not in seen.names() or ("health", "usage_limit_parked") not in seen.events


def test_a_config_blocked_head_still_parks_as_config_blocked(tmp_path):
    store = make_store(tmp_path)

    def denied(argv, stdin):
        raise run._GatewayChildCapUnavailable("budget gone", transient=False)

    go(store, denied, Clock(T0), polls=6)
    rec = ledger(store)
    assert rec["last_failure_class"] == loop.CLASS_CONFIG_BLOCKED and "park_state" not in rec
    assert not store.usage_limit_park_path(AGENT).exists()


# ------------------------------------------------------------------ the other three drive sites


def _normalised(store, clock, seen, spawns, raised=None):
    import golden_stop_retries_scenarios as g

    ids: list[str] = []
    root = Path(store.root)
    files = {}
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        relative = path.relative_to(root).as_posix()
        if relative.startswith(".agenttalk/state/usage-limit-park"):
            files[relative] = "<USAGE-MARKER>"
            continue
        files[relative] = g.normalise_file(path.read_text(encoding="utf-8", errors="replace"), root, ids)
    numbered = {found: "<MSG%d>" % (index + 1) for index, found in enumerate(sorted(ids))}

    def renumber(text):
        return g._MSG.sub(lambda m: numbered.get(m.group(0), "<MSG?>"), text)

    files = {renumber(name): renumber(content) for name, content in files.items()}
    return {"files": files, "sleeps": clock.sleeps, "events": seen.names(), "spawns": spawns,
            "raised": renumber(raised) if raised else None}


def _with_and_without(tmp_path, *, gate, scoped):
    """Run a drive that FAILS as infra, once carrying the usage-limit fact and once not."""
    from agenttalk.wrapper.obligations import DETECTION_GRADE, DetectionCommitGate, PolicySnapshot

    results = []
    for label, fact in (("with", True), ("without", False)):
        root = tmp_path / label
        store = Store(root)
        store.init(["alpha", AGENT])
        store.send(sender="alpha", recipient=AGENT, kind="question", body="What is 19 * 21?",
                   meta={"request_id": "q-1"})
        commit_gate = None
        if gate:
            store.write_waiting(AGENT, {"mode": "wrapper-loop", "wrapper_generation": "wrapper-1",
                                        "wait_token": "wrapper-1", "pid": __import__("os").getpid()})
            commit_gate = DetectionCommitGate(
                store, AGENT, PolicySnapshot.from_mapping(
                    {"schema_version": 1, "agents": {AGENT: {"grade": DETECTION_GRADE}}}, AGENT),
                fence="wrapper-1")
        seen = Seen()
        calls = []

        def drive(record, fact=fact, calls=calls, seen=seen):
            calls.append(1)
            seen.events.append("drive")
            return loop.DriveOutcome(
                ok=False, failure_class=loop.CLASS_INFRA, summary="structured retryable rate_limit_event",
                limit_fact="usage_limit" if fact else None, limit_window="five_hour" if fact else None,
                limit_reset_epoch=CASE1_RESET if fact else None)

        clock = Clock(T0)
        raised = None
        try:
            loop.run_loop(store, AGENT, drive, clock=clock.now, sleep=clock.sleep, now_iso=clock.iso,
                          max_polls=6, wrapper_generation="wrapper-1", usage_limit_park=True,
                          only_request_id="q-1" if scoped else None, commit_gate=commit_gate,
                          heartbeat=lambda seen=seen: seen.events.append("stamp"),
                          on_health_parked=lambda r, why, seen=seen: seen.events.append(("health", why)),
                          on_runtime_idle=lambda seen=seen: seen.events.append("idle"))
        except Exception as exc:  # noqa: BLE001 - the exception IS part of what must not change
            raised = "%s: %s" % (type(exc).__name__, exc)
        results.append(_normalised(store, clock, seen, len(calls), raised))
    return results


@pytest.mark.parametrize("gate,scoped", [(True, False), (True, True), (False, True)])
def test_the_admitted_path_and_both_one_shot_sites_ignore_the_fact_byte_for_byte(tmp_path, gate, scoped):
    with_fact, without = _with_and_without(tmp_path, gate=gate, scoped=scoped)
    assert with_fact == without
    assert with_fact["spawns"] >= 1
    # (master itself refuses a second admitted dispatch here, as the observed exception shows,
    # because this fake drive never captured an operation; the fact changes none of it)
    assert not any("usage-limit-park" in name for name in with_fact["files"])
    assert not any(isinstance(event, tuple) for event in with_fact["events"])   # no park health
