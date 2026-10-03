"""Fix round 1 (usage-limit park), finding 2: a huge JSON number never raises.

Fake spawners and fake clocks only; streams are the two real captured Claude cases with
numbers edited (synthetic where they are not the real lines).
"""

from __future__ import annotations

import json

import pytest

import test_usage_park_drive as drive_fixture
import test_usage_park_loop as fx
from agenttalk.wrapper import usage_park as park

HUGE = 10 ** 400
HUGE_NEG = -(10 ** 400)




@pytest.mark.parametrize("field", ["resetsAt", "unified-utilization", "unified-resetsAt", "own-utilization"])
@pytest.mark.parametrize("huge", [HUGE, HUGE_NEG])
def test_a_huge_json_number_never_raises_in_the_drive_with_the_switch_on_or_off(tmp_path, field, huge):
    events = fx.case1()
    info = events[1]["rate_limit_info"]
    if field == "resetsAt":
        info["resetsAt"] = huge
    elif field == "unified-utilization":
        info["unifiedWindows"]["five_hour"]["utilization"] = huge
    elif field == "unified-resetsAt":
        info["unifiedWindows"]["five_hour"]["resetsAt"] = huge
    else:
        info["utilization"] = huge
    json.loads(json.dumps(events))                                         # ordinary valid JSON
    off = drive_fixture.drive_once(tmp_path / "off", events, park_on=False)
    on = drive_fixture.drive_once(tmp_path / "on", events, park_on=True)
    assert off.ok is False and on.ok is False
    assert (on.failure_class, on.summary) == (off.failure_class, off.summary)


def test_a_huge_reset_means_no_timed_wake_and_the_fact_stands(tmp_path):
    events = fx.case1()
    events[1]["rate_limit_info"]["resetsAt"] = HUGE
    outcome = drive_fixture.drive_once(tmp_path, events)
    assert outcome.limit_fact == "usage_limit" and outcome.limit_reset_epoch is None


def test_a_huge_utilization_is_ignored_the_reset_still_comes_from_the_valid_fields(tmp_path):
    events = fx.case1()
    events[1]["rate_limit_info"]["unifiedWindows"]["seven_day"]["utilization"] = HUGE
    outcome = drive_fixture.drive_once(tmp_path, events)
    assert outcome.limit_fact == "usage_limit" and outcome.limit_reset_epoch == fx.CASE1_RESET


def test_the_whole_run_with_a_huge_reset_parks_without_a_wake(tmp_path):
    events = fx.case1()
    events[1]["rate_limit_info"]["resetsAt"] = HUGE
    store = fx.make_store(tmp_path)
    spawner = fx.Spawner(events)
    fx.go(store, spawner, fx.Clock(fx.T0), polls=6)
    rec = fx.ledger(store)
    assert spawner.calls == 1 and rec["park_state"] == "parked" and "wake_epoch" not in rec


@pytest.mark.parametrize("value", [HUGE, HUGE_NEG, 2 ** 63 + 1, 2 ** 64, float("inf"), float("nan"), 1e400])
def test_the_conversion_helpers_never_raise(value):
    assert park.whole_seconds(value) is None
    assert park.usable_reset(value, 1.0) is None
    assert park.marker_time(value) > 0
    state: dict = {}
    park.note_stream_event(state, {"type": "rate_limit_event", "rate_limit_info": {
        "status": "rejected", "rateLimitType": "five_hour", "resetsAt": value,
        "unifiedWindows": {"five_hour": {"utilization": value, "resetsAt": value}}}})
    park.note_stream_event(state, {"type": "result", "is_error": True})
    assert park.fact_from_stream(state) == {"window": "five_hour", "reset_epoch": None}


def test_ledger_fields_holding_huge_numbers_never_raise():
    rec = {"wake_epoch": HUGE, "probed_wake_epoch": HUGE, "reset_epoch": HUGE, "parked_seconds_total": HUGE,
           "attempts_started": HUGE, "excluded_attempts": HUGE, "parked_at": "2026-09-09T03:00:00Z"}
    assert park.wake_due(rec, 1.0) is False and park.unused_wake(rec) is None
    assert park.parked_seconds(rec) == 0.0
    assert park.disposal_attempts(rec) >= 0


@pytest.mark.parametrize("field", ["updated_at_epoch", "reset_epoch", "wake_epoch"])
def test_the_marker_reader_never_raises_on_a_huge_number(tmp_path, field):
    store = fx.make_store(tmp_path)
    store.write_usage_limit_park(fx.AGENT, window="five_hour", reset_epoch=fx.CASE1_RESET,
                                 wake_epoch=fx.CASE1_WAKE, message_id="m1", parked_at=None, now_epoch=fx.T0)
    path = store.usage_limit_park_path(fx.AGENT)
    for huge in (HUGE, HUGE_NEG):
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
        data[field] = huge
        path.write_text(json.dumps(data), encoding="utf-8")
        got = store.read_usage_limit_park(fx.AGENT, now_epoch=fx.T0)
        if field == "updated_at_epoch":
            assert got is None
        else:
            assert got is not None and got[field] is None
        store.usage_limit_park_view(fx.AGENT, now_epoch=fx.T0)                    # must not raise either


def test_the_marker_reader_never_raises_on_a_hostile_file_or_name(tmp_path):
    store = fx.make_store(tmp_path)
    path = store.usage_limit_park_path(fx.AGENT)
    path.parent.mkdir(parents=True, exist_ok=True)
    for text in ("[" * 100000, '{"agent": "beta", "state": "usage_limit_parked", "message_id": "m", '
                 '"updated_at_epoch": 1e999}', "\x00\x01"):
        path.write_text(text, encoding="utf-8")
        assert store.read_usage_limit_park(fx.AGENT, now_epoch=fx.T0) is None
    assert store.read_usage_limit_park("../escape", now_epoch=fx.T0) is None
    assert store.usage_limit_park_view("../escape", now_epoch=fx.T0) is None


