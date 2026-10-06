"""Findings from the upstream cold read of the cool-down patch (round 1), kept as permanent tests.

1. A temporary overload must not erase a quota retry that was never used.
2. A notice delivered late must name only the saved next retry, never a schedule after it.
3. A cool-down notice is not called a usage limit.
"""
import json

from agenttalk import cli
from agenttalk.wrapper import usage_park as park
from test_provider_wait_drive import made_up_529, made_up_plain_429
from test_provider_wait_loop import go
from test_usage_park_loop import (CASE1_RESET, CASE1_WAKE, T0, Clock, Spawner, case1, failed_turn, ledger,
                                  make_store)


def _case1_reset(reset):
    return json.loads(json.dumps(case1()).replace(str(CASE1_RESET), str(reset)))


def _park_then_overload(tmp_path, *, start=T0):
    """A proven five-hour limit, then a restart probe before its wake that meets a made-up 529."""
    store = make_store(tmp_path)
    clock = Clock(start)
    go(store, Spawner(case1()), clock, polls=1, generation='g1')
    assert 'quota_wake_epoch' not in ledger(store)
    go(store, Spawner(made_up_529()), clock, polls=1, generation='g2')
    return store, clock


# ------------------------------------------------------------------ F1: the unused quota retry


def test_unused_quota_wake_survives_cooldown(tmp_path):
    store = make_store(tmp_path)
    clock = Clock(T0)
    go(store, Spawner(case1()), clock, polls=1, generation='g1')
    original = ledger(store)
    assert original['wake_epoch'] == CASE1_WAKE
    assert original.get('probed_wake_epoch') != CASE1_WAKE
    go(store, Spawner(made_up_529()), clock, polls=1, generation='g2')
    clock.t = ledger(store)['wake_epoch']
    assert clock.t < CASE1_WAKE
    go(store, Spawner(case1()), clock, polls=1, generation='g2')
    clock.t = CASE1_WAKE + 1
    spawner = Spawner(case1())
    go(store, spawner, clock, polls=2, generation='g2')
    assert spawner.calls == 1, 'the never-used quota wake was discarded and the seat stays parked'


def test_the_set_aside_wake_is_written_once_and_a_cooldown_never_changes_it(tmp_path):
    store, clock = _park_then_overload(tmp_path)
    assert ledger(store)['quota_wake_epoch'] == CASE1_WAKE and ledger(store)['park_kind'] == 'overloaded'
    for _ in range(2):                                                         # later cool-down steps
        clock.t = ledger(store)['wake_epoch']
        go(store, Spawner(made_up_529()), clock, polls=1, generation='g2')
    assert ledger(store)['cooldown_step'] == 2 and ledger(store)['quota_wake_epoch'] == CASE1_WAKE
    clock.t = ledger(store)['wake_epoch']
    go(store, Spawner(made_up_plain_429()), clock, polls=1, generation='g2')   # a change of cool-down kind
    assert ledger(store)['park_kind'] == 'throttled' and ledger(store)['quota_wake_epoch'] == CASE1_WAKE


def test_the_same_limit_back_restores_the_set_aside_wake_and_the_field_goes(tmp_path):
    store, clock = _park_then_overload(tmp_path)
    clock.t = ledger(store)['wake_epoch']
    go(store, Spawner(case1()), clock, polls=1, generation='g2')
    rec = ledger(store)
    assert rec['wake_epoch'] == CASE1_WAKE and rec['reset_epoch'] == CASE1_RESET
    assert 'quota_wake_epoch' not in rec and 'park_kind' not in rec and 'cooldown_step' not in rec


def test_a_quota_wake_already_used_by_a_probe_is_never_set_aside_or_re_armed(tmp_path):
    store = make_store(tmp_path)
    clock = Clock(T0)
    go(store, Spawner(case1()), clock, polls=1, generation='g1')
    clock.t = CASE1_WAKE + 1
    go(store, Spawner(case1()), clock, polls=1, generation='g1')               # the wake probe uses it
    assert ledger(store)['probed_wake_epoch'] == CASE1_WAKE
    go(store, Spawner(made_up_529()), clock, polls=1, generation='g2')
    assert 'quota_wake_epoch' not in ledger(store)
    clock.t = ledger(store)['wake_epoch']
    go(store, Spawner(case1()), clock, polls=1, generation='g2')
    assert 'quota_wake_epoch' not in ledger(store) and park.unused_wake(ledger(store)) is None
    clock.t += 200000
    spawner = Spawner(case1())
    go(store, spawner, clock, polls=3, generation='g2')
    assert spawner.calls == 0


def test_a_set_aside_wake_already_in_the_past_when_the_limit_returns_is_not_restored(tmp_path):
    store, clock = _park_then_overload(tmp_path, start=CASE1_WAKE - 100)
    assert ledger(store)['quota_wake_epoch'] == CASE1_WAKE
    clock.t = ledger(store)['wake_epoch']
    assert clock.t > CASE1_WAKE
    go(store, Spawner(case1()), clock, polls=1, generation='g2')
    rec = ledger(store)
    assert 'quota_wake_epoch' not in rec and 'wake_epoch' not in rec
    clock.t += 100000
    spawner = Spawner(case1())
    go(store, spawner, clock, polls=3, generation='g2')
    assert spawner.calls == 0


def test_a_strictly_later_reset_on_return_replaces_the_set_aside_wake(tmp_path):
    store, clock = _park_then_overload(tmp_path)
    later = CASE1_RESET + 3600
    clock.t = ledger(store)['wake_epoch']
    go(store, Spawner(_case1_reset(later)), clock, polls=1, generation='g2')
    rec = ledger(store)
    assert rec['wake_epoch'] == later + 30 and 'quota_wake_epoch' not in rec
    clock.t = later + 31
    spawner = Spawner(_case1_reset(later))
    go(store, spawner, clock, polls=1, generation='g2')
    assert spawner.calls == 1


def test_the_field_goes_when_the_park_closes_during_the_cooldown(tmp_path):
    store, clock = _park_then_overload(tmp_path)
    assert ledger(store)['quota_wake_epoch'] == CASE1_WAKE
    clock.t = ledger(store)['wake_epoch']
    go(store, Spawner(failed_turn()), clock, polls=1, generation='g2')         # an ordinary failure closes it
    rec = ledger(store)
    assert 'park_state' not in rec and 'quota_wake_epoch' not in rec


def test_a_history_with_no_cooldown_never_has_the_field(tmp_path):
    store = make_store(tmp_path)
    clock = Clock(T0)
    go(store, Spawner(case1()), clock, polls=1, generation='g1')
    go(store, Spawner(case1()), clock, polls=1, generation='g2')
    clock.t = CASE1_WAKE + 1
    go(store, Spawner(case1()), clock, polls=2, generation='g2')
    assert 'quota_wake_epoch' not in ledger(store) and 'park_rev' not in ledger(store)


# ------------------------------------------------------------------ F2: no schedule after the saved time


def test_late_notice_does_not_promise_wrong_subsequent_delay(tmp_path):
    store = make_store(tmp_path)
    clock = Clock(T0)
    go(store, Spawner(made_up_plain_429()), clock, polls=1, routed=False)
    clock.t = ledger(store)['wake_epoch']
    late = go(store, Spawner(made_up_plain_429()), clock, polls=1, routed=True)
    body = cli._usage_limit_notice_body(late.notices[-1])
    wake = ledger(store)['wake_epoch']
    assert f'It tries again at {park.format_epoch(wake)}. ' in body, body
    assert '30 minutes' not in body and 'minutes after that' not in body, body


def test_a_late_notice_at_a_later_step_names_only_the_saved_time(tmp_path):
    store = make_store(tmp_path)
    clock = Clock(T0)
    go(store, Spawner(made_up_plain_429()), clock, polls=1, routed=False)      # step 0, notice not routed
    for _ in range(2):                                                         # step 1, then step 2
        clock.t = ledger(store)['wake_epoch']
        go(store, Spawner(made_up_plain_429()), clock, polls=1, routed=False)
    assert ledger(store)['cooldown_step'] == 2
    clock.t = ledger(store)['wake_epoch']
    late = go(store, Spawner(made_up_plain_429()), clock, polls=1, routed=True)
    body = cli._usage_limit_notice_body(late.notices[-1])
    assert f"It tries again at {park.format_epoch(ledger(store)['wake_epoch'])}. " in body
    assert 'minutes' not in body and 'every hour' not in body, body


# ------------------------------------------------------------------ F3: the subject


def _notice_subject_and_body(tmp_path, kind):
    from test_provider_wait_notice import _info, _notice, _setup
    store, mid = _setup(tmp_path)
    cli._dead_letter_notifier(store, 'beta')(_info(mid, kind), disposed=False)
    notice = _notice(store)
    return notice.subject, notice.body


def test_overload_notice_subject_does_not_call_it_a_usage_limit(tmp_path):
    subject, _ = _notice_subject_and_body(tmp_path, 'overloaded')
    assert 'usage-limit' not in subject and 'provider-wait' in subject


def test_each_kind_of_notice_has_its_own_subject_and_the_usage_limit_one_is_unchanged(tmp_path):
    over = _notice_subject_and_body(tmp_path / 'a', 'overloaded')
    thr = _notice_subject_and_body(tmp_path / 'b', 'throttled')
    lim = _notice_subject_and_body(tmp_path / 'c', 'usage_limit')
    assert over[0] == 'provider-wait park notice' and over[1].startswith('[provider-wait-parked] ')
    assert thr[0] == 'provider-wait park notice' and thr[1].startswith('[provider-wait-parked] ')
    assert lim[0] == 'usage-limit park notice' and lim[1].startswith('[usage-limit-parked] ')
