"""Fix round 1 (usage-limit park), finding 1: one precision for the marker's time.

Fake spawners and fake clocks only; streams are the two real captured Claude cases with
numbers edited (synthetic where they are not the real lines).
"""

from __future__ import annotations

import json

import test_usage_park_loop as fx
from agenttalk import attention as A
from agenttalk import cli
from agenttalk.wrapper import usage_park as park




def test_the_real_loop_with_a_fractional_clock_publishes_a_marker_that_reads_back_fresh_and_stale(tmp_path):
    store = fx.make_store(tmp_path)
    clock = fx.Clock(fx.T0 + 0.123456)
    fx.go(store, fx.Spawner(fx.case1()), clock, polls=3)
    raw = json.loads(store.usage_limit_park_path(fx.AGENT).read_text(encoding="utf-8"))
    assert isinstance(raw["updated_at_epoch"], int)                        # whole seconds, floored
    assert raw["updated_at_epoch"] <= int(clock.t) and raw["updated_at_epoch"] >= fx.T0
    fresh = store.read_usage_limit_park(fx.AGENT, now_epoch=clock.now())
    assert fresh is not None and fresh["fresh"] is True and fresh["wake_epoch"] == fx.CASE1_WAKE
    later = clock.now() + park.MARKER_STALE_SECONDS + 5.75
    stale = store.read_usage_limit_park(fx.AGENT, now_epoch=later)
    assert stale is not None and stale["fresh"] is False and stale["wake_epoch"] == fx.CASE1_WAKE


def test_the_writer_floors_and_the_reader_accepts_only_what_the_writer_writes(tmp_path):
    store = fx.make_store(tmp_path)
    store.write_usage_limit_park(fx.AGENT, provider="claude", window="five_hour", reset_epoch=None, wake_epoch=None,
                                 message_id="m1", parked_at=None, now_epoch=1788900000.987654)
    raw = json.loads(store.usage_limit_park_path(fx.AGENT).read_text(encoding="utf-8"))
    assert raw["updated_at_epoch"] == 1788900000
    assert store.read_usage_limit_park(fx.AGENT, now_epoch=1788900000.5)["age_seconds"] == 0
    assert park.marker_time(None) > 0 and park.marker_time(float("nan")) > 0 and park.marker_time(10 ** 400) > 0


def test_the_step_two_readers_show_the_same_marker(tmp_path):
    store = fx.make_store(tmp_path)
    clock = fx.Clock(fx.T0 + 0.123456)
    fx.go(store, fx.Spawner(fx.case1()), clock, polls=3)
    now = clock.now()
    # #311 recast fix round 4, finding 1: freshness now also needs a real heartbeat, on
    # the matching-marker path too - this fixture's simulated loop substitutes a no-op
    # recorder for `heartbeat` (by design, to isolate the park/ledger logic it is really
    # testing from real file I/O), so one is written directly here, at the SAME fake-clock
    # moment the marker itself was published - the reader below cares what the wrapper's
    # evidence looked like AT THAT TIME, not at real wall-clock "now".
    (store.state_dir / f"{fx.AGENT}.heartbeat").write_text(park.epoch_iso(now), encoding="utf-8")
    view = store.usage_limit_park_view(fx.AGENT, now_epoch=now)
    assert view is not None and view["state"] == "parked" and view["wake_epoch"] == fx.CASE1_WAKE
    items = A.usage_limit_park_items([{"agent": fx.AGENT, **view}])
    assert items[0]["title"].startswith("beta: parked on a usage limit until ")
    # read much later by the real command (the fake clock is long past): it stays VISIBLE as stale
    rows = cli._usage_limit_park_views(store, ["alpha", "beta"])
    assert [r["agent"] for r in rows] == ["beta"] and rows[0]["state"] == "stale"
    assert cli._usage_limit_park_flag(rows[0]) == "usage_limit_parked(wrapper_not_responding)"


