"""The independent delta review of the upstream round-1 fixes, kept as permanent tests (fake events and clocks only)."""
import pytest
from test_provider_wait_upstream_review import _park_then_overload
from test_provider_wait_loop import go
from test_provider_wait_drive import made_up_529
from test_usage_park_loop import (AGENT, CASE1_RESET, CASE1_WAKE, T0, Clock, Crash, Spawner, case1, head, ledger,
                                  make_store)
from agenttalk.wrapper import usage_park as park

def damage_saved(store, value):
    data = store.dead_letter_attempts(AGENT)
    rec = data['messages'][head(store).id]
    if value == 'ABSENT':
        rec.pop('quota_wake_epoch', None)
    else:
        rec['quota_wake_epoch'] = value
    store._write_attempts(AGENT, data)

@pytest.mark.parametrize('value', ['ABSENT', None, True, 'later', [], {}, -1, 0, 3.5, float('inf'), float('nan'),
                                   10**100])
def test_bad_shapes_do_not_escape_or_drop_head(tmp_path, value):
    store, clock = _park_then_overload(tmp_path)
    damage_saved(store, value)
    clock.t = ledger(store)['wake_epoch']
    go(store, Spawner(case1()), clock, polls=1, generation='g2')
    assert head(store).body == 'one' and store.dead_lettered_count(AGENT) == 0
    assert 'quota_wake_epoch' not in ledger(store)
    assert park.unused_wake(ledger(store)) is None

@pytest.mark.parametrize('value', [T0 + 1800, CASE1_WAKE + 86400, 10**12])
def test_saved_slot_cannot_invent_a_different_quota_retry(tmp_path, value):
    store, clock = _park_then_overload(tmp_path)
    damage_saved(store, value)
    clock.t = ledger(store)['wake_epoch']
    go(store, Spawner(case1()), clock, polls=1, generation='g2')
    rec = ledger(store)
    print('SAVED_SLOT', value, 'REAL_QUOTA_WAKE', CASE1_WAKE, 'RESTORED', rec.get('wake_epoch'),
          'RESET', rec.get('reset_epoch'), 'VIEW', store.usage_limit_park_view(AGENT, now_epoch=clock.t))
    clock.t = min(value, CASE1_WAKE + 1)
    spawner = Spawner(case1())
    go(store, spawner, clock, polls=2, generation='g2')
    print('CLOCK', clock.t, 'SPAWNS', spawner.calls, 'HEAD_KEPT', head(store).body,
          'DEAD_LETTERS', store.dead_lettered_count(AGENT))
    assert park.unused_wake(rec) in (None, CASE1_WAKE), 'unchecked saved slot dictates an unproven retry'

def test_saved_slot_survives_crash_and_restart_without_early_retry(tmp_path):
    store, clock = _park_then_overload(tmp_path)
    clock.t = ledger(store)['wake_epoch']
    with pytest.raises(Crash):
        go(store, Spawner(Crash('review crash')), clock, polls=1, generation='g2')
    quiet = Spawner(case1())
    go(store, quiet, clock, polls=2, generation='g3')
    assert quiet.calls == 0 and ledger(store)['quota_wake_epoch'] == CASE1_WAKE
    clock.t = ledger(store)['wake_epoch']
    go(store, quiet, clock, polls=1, generation='g3')
    assert ledger(store)['wake_epoch'] == CASE1_WAKE
    clock.t = CASE1_WAKE + 1
    go(store, quiet, clock, polls=3, generation='g3')
    assert quiet.calls == 2 and store.dead_lettered_count(AGENT) == 0
    assert head(store).body == 'one'

def test_disabled_switch_removes_saved_slot(tmp_path):
    store, clock = _park_then_overload(tmp_path)
    go(store, Spawner(made_up_529()), clock, polls=1, generation='g2', park_on=False)
    assert 'quota_wake_epoch' not in ledger(store)

def test_existing_consumed_wake_unit_test_is_clock_independent(monkeypatch):
    import test_provider_wait_rules as rules
    monkeypatch.setattr(park.time, 'time', lambda: 10_000)
    rules.test_a_proven_limit_after_a_cooldown_with_an_old_reset_leaves_no_wake_for_the_next_start()


# ---- the saved time is trusted only when it matches its reset (round 2), through the real loop ----

def damage_ledger(store, **fields):
    data = store.dead_letter_attempts(AGENT)
    data['messages'][head(store).id].update(fields)
    store._write_attempts(AGENT, data)


def _return_and_wait(store, clock, *, until):
    """The same limit comes back at the cool-down's wake; then count the tries made by ``until``."""
    clock.t = ledger(store)['wake_epoch']
    go(store, Spawner(case1()), clock, polls=1, generation='g2')
    rec = ledger(store)
    assert 'quota_wake_epoch' not in rec
    assert park.unused_wake(rec) in (None, CASE1_WAKE)
    clock.t = until
    spawner = Spawner(case1())
    go(store, spawner, clock, polls=3, generation='g2')
    return rec, spawner.calls


@pytest.mark.parametrize('value', [T0 + 1800, CASE1_WAKE - 1, CASE1_WAKE + 1, CASE1_WAKE + 86400, 10**12])
def test_an_unrelated_saved_time_neither_tries_early_nor_is_armed(tmp_path, value):
    store, clock = _park_then_overload(tmp_path)
    damage_saved(store, value)
    rec, _ = _return_and_wait(store, clock, until=min(value, CASE1_WAKE - 1))
    assert park.unused_wake(rec) is None and rec.get('wake_epoch') is None
    # at its own (unrelated) time or the real wake there is no try either: nothing was invented
    clock.t = CASE1_WAKE + 100000
    spawner = Spawner(case1())
    go(store, spawner, clock, polls=3, generation='g2')
    assert spawner.calls == 0 and head(store).body == 'one' and store.dead_lettered_count(AGENT) == 0


@pytest.mark.parametrize('bad', [None, 'x', 1.5, True, 10**12, CASE1_RESET + 1, CASE1_RESET - 1, 0, -5])
def test_a_damaged_reset_record_with_a_right_saved_time_arms_nothing(tmp_path, bad):
    store, clock = _park_then_overload(tmp_path)
    assert ledger(store)['quota_wake_epoch'] == CASE1_WAKE
    damage_ledger(store, last_reset_epoch=bad)
    rec = _return_and_wait(store, clock, until=CASE1_WAKE + 1)[0]
    assert rec.get('wake_epoch') is None or rec.get('wake_epoch') == CASE1_WAKE


def test_the_saving_side_refuses_an_unused_wake_that_does_not_match_its_reset(tmp_path):
    store = make_store(tmp_path)
    clock = Clock(T0)
    go(store, Spawner(case1()), clock, polls=1, generation='g1')
    damage_ledger(store, wake_epoch=CASE1_WAKE + 5)             # unused and valid, but not reset + 30
    go(store, Spawner(made_up_529()), clock, polls=1, generation='g2')
    rec = ledger(store)
    assert rec['park_kind'] == 'overloaded' and 'quota_wake_epoch' not in rec


def test_the_true_wake_is_tried_exactly_once_and_never_early(tmp_path):
    store, clock = _park_then_overload(tmp_path)
    clock.t = ledger(store)['wake_epoch']
    go(store, Spawner(case1()), clock, polls=1, generation='g2')
    assert ledger(store)['wake_epoch'] == CASE1_WAKE
    clock.t = CASE1_WAKE - 1
    early = Spawner(case1())
    go(store, early, clock, polls=3, generation='g2')
    assert early.calls == 0
    clock.t = CASE1_WAKE
    spawner = Spawner(case1())
    go(store, spawner, clock, polls=3, generation='g2')
    assert spawner.calls == 1
