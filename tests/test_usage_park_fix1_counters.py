"""Fix round 1 (usage-limit park), finding 3: a usage-limit result changes no eligible counter.

Fake spawners and fake clocks only; streams are the two real captured Claude cases with
numbers edited (synthetic where they are not the real lines).
"""

from __future__ import annotations

import test_usage_park_loop as fx
from agenttalk.wrapper import loop
from agenttalk.wrapper import usage_park as park




def watchdog_history(store, n=2):
    mid = fx.head(store).id
    record = fx.head(store).to_dict()
    for k in range(n):
        store.record_attempt_start(fx.AGENT, record, attempt_id=f"{k:012x}", at=park.epoch_iso(fx.T0 - 100 + k))
        store.record_attempt_result(fx.AGENT, mid, failure_class=loop.CLASS_AMBIGUOUS, summary="watchdog",
                                    at=park.epoch_iso(fx.T0 - 90 + k), interrupted=True,
                                    interruption_kind="turn_watchdog")
    return mid


COUNTERS = ("interrupted_consecutive", "interrupted_watchdog_consecutive", "poison_eligible_failures",
            "infra_failures", "ambiguous_failures", "never_started_first_at", "never_started_consecutive")


def counters(rec):
    return {name: rec.get(name) for name in COUNTERS}


def test_the_first_proven_limit_keeps_the_earlier_watchdog_history(tmp_path):
    store = fx.make_store(tmp_path)
    mid = watchdog_history(store)
    before = counters(store.attempt_record(fx.AGENT, mid))
    assert before["interrupted_watchdog_consecutive"] == 2 and before["interrupted_consecutive"] == 2
    fx.go(store, fx.Spawner(fx.case1()), fx.Clock(fx.T0), polls=3)
    after = store.attempt_record(fx.AGENT, mid)
    assert after["park_state"] == park.PARKED
    assert counters(after) == before


def test_a_probe_of_any_class_keeps_every_eligible_counter(tmp_path):
    store = fx.make_store(tmp_path)
    mid = watchdog_history(store)
    spawner = fx.Spawner(fx.case1())
    fx.go(store, spawner, fx.Clock(fx.T0), polls=2, generation="g1")
    before = counters(store.attempt_record(fx.AGENT, mid))
    fx.go(store, spawner, fx.Clock(fx.T0 + 60), polls=2, generation="g2")             # a limited probe
    assert counters(store.attempt_record(fx.AGENT, mid)) == before
    fx.go(store, fx.FailingSpawner(fx.case1()[:-1], watchdog={"summary": "hung tool killed"}),
          fx.Clock(fx.T0 + 120), polls=1, generation="g3", k_interrupted=50)               # a killed probe
    assert counters(store.attempt_record(fx.AGENT, mid)) == before


def test_the_next_ordinary_failure_behaves_as_master_would_with_that_history(tmp_path):
    store = fx.make_store(tmp_path)
    watchdog_history(store)                                                            # 2 kills already
    fx.go(store, fx.Spawner(fx.case1()), fx.Clock(fx.T0), polls=2, generation="g1")    # the first limit: parks
    killed = fx.FailingSpawner(fx.case1()[:-1], watchdog={"summary": "hung tool killed"})
    # the switch off: the head is driven as before; the THIRD kill reaches k_interrupted = 3
    fx.go(store, killed, fx.Clock(fx.T0 + 60), polls=10, generation="g2", park_on=False,
          interruption_redrive_seconds=0.0)
    assert killed.calls == 1
    assert store.dead_lettered_count(fx.AGENT) == 1
    assert store.list_dead_letters(fx.AGENT)[0]["last_reason"] == loop.INTERRUPTION_BUDGET_EXHAUSTED


def test_the_next_ordinary_failure_after_a_closed_park_counts_from_that_history(tmp_path):
    store = fx.make_store(tmp_path)
    watchdog_history(store)
    spawner = fx.Spawner(fx.case1())
    fx.go(store, spawner, fx.Clock(fx.T0), polls=2, generation="g1")
    # a restart probe meets another class (a plain failure): the park closes, nothing counted
    fx.go(store, fx.Spawner(fx.failed_turn()), fx.Clock(fx.T0 + 60), polls=1, generation="g2",
          k_escalate=50, k_poison=0)
    killed = fx.FailingSpawner(fx.case1()[:-1], watchdog={"summary": "hung tool killed"})
    fx.go(store, killed, fx.Clock(fx.T0 + 120), polls=10, generation="g2", interruption_redrive_seconds=0.0)
    assert killed.calls == 1 and store.dead_lettered_count(fx.AGENT) == 1


def test_a_consecutive_poison_run_and_a_never_started_run_survive_a_limit(tmp_path):
    store = fx.make_store(tmp_path)
    mid = fx.head(store).id
    record = fx.head(store).to_dict()
    store.record_attempt_start(fx.AGENT, record, attempt_id="aaaaaaaaaaaa", at=park.epoch_iso(fx.T0 - 50))
    store.record_attempt_result(fx.AGENT, mid, failure_class=loop.CLASS_POISON, summary="p",
                                at=park.epoch_iso(fx.T0 - 40), never_started_first_at="2026-09-08T00:00:00Z",
                                never_started_consecutive=1)
    before = counters(store.attempt_record(fx.AGENT, mid))
    assert before["poison_eligible_failures"] == 1 and before["never_started_consecutive"] == 1
    fx.go(store, fx.Spawner(fx.case1()), fx.Clock(fx.T0), polls=2)
    assert counters(store.attempt_record(fx.AGENT, mid)) == before


def test_an_ordinary_non_usage_result_still_moves_the_counters_as_before(tmp_path):
    store = fx.make_store(tmp_path)
    mid = watchdog_history(store, n=1)
    rec = store.attempt_record(fx.AGENT, mid)
    assert rec["interrupted_watchdog_consecutive"] == 1 and rec["ambiguous_failures"] == 1
    record = fx.head(store).to_dict()
    store.record_attempt_start(fx.AGENT, record, attempt_id="222222222222", at=park.epoch_iso(fx.T0))
    rec = store.record_attempt_result(fx.AGENT, mid, failure_class=loop.CLASS_INFRA, summary="x",
                                      at=park.epoch_iso(fx.T0 + 1))
    assert rec["interrupted_watchdog_consecutive"] == 0 and rec["interrupted_consecutive"] == 0
    assert rec["infra_failures"] == 1 and rec["poison_eligible_failures"] == 0
