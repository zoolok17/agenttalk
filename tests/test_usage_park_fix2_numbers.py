"""Fix round 2 (usage-limit park), step 1: huge numbers in exponent form, and the stated tolerance
of the marker's freshness.

Synthetic JSON; fake clocks. The bound is ``usage_park._MAX_MAGNITUDE`` (2 ** 63) for integers and
finite floats alike, wherever a time or a utilization is read.
"""

from __future__ import annotations

import json

import pytest

import test_usage_park_drive as drive_fixture
import test_usage_park_loop as fx
from agenttalk.wrapper import usage_park as park

BOUND = park._MAX_MAGNITUDE
ABOVE = [1e19, 1e100, 1e308, -1e308, float(2 ** 64), 10 ** 400]
BELOW = [1788948000, 1788948000.0, 1.7889480e9, 2 ** 62]


@pytest.mark.parametrize("value", ABOVE)
def test_a_value_above_the_bound_is_refused_in_every_form(value):
    assert park.whole_seconds(value) is None
    assert park.usable_reset(value, 1.0) is None
    assert park.format_epoch(value) is None


@pytest.mark.parametrize("value", BELOW)
def test_a_value_below_the_bound_is_still_a_time(value):
    assert park.whole_seconds(value) == int(value)


def test_the_bound_itself_is_in_and_the_next_float_is_out():
    assert park.whole_seconds(float(BOUND)) == BOUND
    assert park.whole_seconds(float(BOUND) * 2) is None


def marker_with(tmp_path, field, raw_json):
    store = fx.make_store(tmp_path)
    store.write_usage_limit_park(fx.AGENT, window="five_hour", reset_epoch=fx.CASE1_RESET,
                                 wake_epoch=fx.CASE1_WAKE, message_id="m1", parked_at=None, now_epoch=fx.T0)
    path = store.usage_limit_park_path(fx.AGENT)
    text = path.read_text(encoding="utf-8")
    data = json.loads(text)
    old = json.dumps(data[field])
    path.write_text(text.replace('"%s": %s' % (field, old), '"%s": %s' % (field, raw_json)), encoding="utf-8")
    return store


@pytest.mark.parametrize("raw", ["1e308", "-1e308", "1e100", "1e19", "1" + "0" * 400, "9223372036854775809"])
def test_the_marker_update_time_above_the_bound_is_no_marker(tmp_path, raw):
    store = marker_with(tmp_path, "updated_at_epoch", raw)
    assert store.read_usage_limit_park(fx.AGENT, now_epoch=fx.T0) is None


@pytest.mark.parametrize("raw", ["1788900000", "1.7889e9", "1788900000.0"])
def test_the_marker_update_time_below_the_bound_reads_in_both_json_forms(tmp_path, raw):
    store = marker_with(tmp_path, "updated_at_epoch", raw)
    got = store.read_usage_limit_park(fx.AGENT, now_epoch=1788900000)
    assert got is not None and got["fresh"] is True


@pytest.mark.parametrize("field", ["reset_epoch", "wake_epoch"])
@pytest.mark.parametrize("raw,ok", [("1e308", False), ("1e19", False), ("1" + "0" * 400, False),
                                    ("1788948000", True), ("1.788948e9", True), ("1788948000.5", False)])
def test_the_marker_reset_and_wake_in_both_json_forms(tmp_path, field, raw, ok):
    store = marker_with(tmp_path, field, raw)
    got = store.read_usage_limit_park(fx.AGENT, now_epoch=fx.T0)
    assert got is not None
    assert (got[field] is not None) is ok


@pytest.mark.parametrize("raw", ["1e308", "1e19", "1e400"])
def test_the_parser_ignores_an_exponent_form_reset_or_utilization(tmp_path, raw):
    for where in ("resetsAt", "utilization", "own"):
        events = fx.case1()
        info = events[1]["rate_limit_info"]
        huge = json.loads(raw) if raw != "1e400" else float("inf")
        if where == "resetsAt":
            info["resetsAt"] = huge
        elif where == "utilization":
            info["unifiedWindows"]["seven_day"]["utilization"] = huge
        else:
            info["unifiedWindows"]["five_hour"]["resetsAt"] = huge
        text = json.dumps(events).replace("Infinity", "1e400")
        assert json.loads(text)                                       # valid JSON of the exponent form
        outcome = drive_fixture.drive_once(tmp_path / (where + raw), json.loads(text))
        off = drive_fixture.drive_once(tmp_path / (where + raw + "off"), json.loads(text), park_on=False)
        assert (outcome.failure_class, outcome.summary) == (off.failure_class, off.summary)
        assert outcome.limit_fact == "usage_limit"
        if where in ("resetsAt", "own"):
            assert outcome.limit_reset_epoch is None                 # an unusable reset: no timed wake
        else:
            assert outcome.limit_reset_epoch == fx.CASE1_RESET       # an invalid utilization is ignored


def test_the_stated_freshness_tolerance(tmp_path):
    # Both times are floored to whole seconds: written at T+0.1 and read at T+300.9 the marker is
    # 300.8 s old but still fresh (a stale label can come up to one second LATE); it never turns
    # stale EARLY: read at T+300.0 it is fresh, and from T+301.0 it is stale.
    store = fx.make_store(tmp_path)
    store.write_usage_limit_park(fx.AGENT, window="five_hour", reset_epoch=None, wake_epoch=None,
                                 message_id="m", parked_at=None, now_epoch=fx.T0 + 0.1)
    limit = park.MARKER_STALE_SECONDS
    assert store.read_usage_limit_park(fx.AGENT, now_epoch=fx.T0 + limit - 0.5)["fresh"] is True
    assert store.read_usage_limit_park(fx.AGENT, now_epoch=fx.T0 + limit + 0.9)["fresh"] is True   # late by < 1 s
    assert store.read_usage_limit_park(fx.AGENT, now_epoch=fx.T0 + limit + 1.0)["fresh"] is False
    for early in (0.0, 10.0, 299.0, 299.99):
        assert store.read_usage_limit_park(fx.AGENT, now_epoch=fx.T0 + early)["fresh"] is True, early
