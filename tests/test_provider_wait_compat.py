"""What the provider cool-down must NOT change, and the one writer of its wake.

* a recorded run of the proven usage-limit park (made from agenttalk 93988c06, the code before the cool-down) is
  identical on the current code;
* the version-1 marker file is not touched and a cool-down writes none;
* the health file of a cooling-down seat says ``provider_wait_parked`` on the same state, with the cause as a closed
  detail word when there is one and none when the closed vocabulary has no fitting word;
* no code path writes ``wake_epoch`` for the two new kinds except the one arming function.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import golden_usage_limit_run as recorded
from agenttalk.store import Store
from agenttalk.wrapper import loop, run, session
from agenttalk.wrapper import usage_park as park
from agenttalk.wrapper.health import WrapperHealthWriter
from test_provider_wait_drive import SYSTEM, event, made_up_529, made_up_plain_429, result
from test_usage_park_loop import AGENT, T0, Clock, Spawner, case1, head, ledger, make_store

GOLDEN = json.loads((Path(__file__).parent / "golden" / "usage_limit_run_93988c06.json").read_text(encoding="utf-8"))


def test_a_recorded_run_of_the_proven_usage_limit_is_identical_to_the_one_before_the_cooldown(tmp_path):
    assert recorded.record_run(tmp_path) == GOLDEN


def test_the_recorded_run_really_holds_a_marker_a_probe_and_a_closed_park():
    assert GOLDEN["park"]["marker"]["state"] == "usage_limit_parked"
    assert GOLDEN["park"]["record"]["park_state"] == "parked"
    assert GOLDEN["start_probe_limit_again"]["record"]["notice_key"] == "probe:1:2"
    assert "park_state" not in GOLDEN["wake_probe_other_class"]["record"]
    assert GOLDEN["weekly_probe_success"]["record"] == {}
    assert not any("park_rev" in snap["record"] or "park_kind" in snap["record"]
                   for snap in GOLDEN.values() if isinstance(snap, dict))


# --------------------------------------------------------------------------- the real health file


def _health_run(tmp_path, events, *, polls=3):
    store = make_store(tmp_path)
    health = WrapperHealthWriter(store, AGENT, "claude", mode="wrapper", min_interval=0.0)
    drive = run.make_drive(
        store, AGENT, "claude", session.SessionState(cli="claude", claude_session_id="sid-1"), ["claude"],
        spawn=Spawner(events), clock=lambda: 0.0, render=False, heartbeat=lambda: None,
        health_writer=health, usage_limit_park=True)
    clock = Clock(T0)
    loop.run_loop(store, AGENT, drive, clock=clock.now, sleep=clock.sleep, now_iso=clock.iso, max_polls=polls,
                  usage_limit_park=True, on_health_parked=health.parked, wrapper_generation="g1")
    return store, store.read_health_raw(AGENT)


def test_a_cooling_down_seat_says_provider_wait_parked_on_the_same_state_with_the_closed_cause(tmp_path):
    for label, events, detail in (("529", made_up_529(), "status_529"), ("429", made_up_plain_429(), "status_429")):
        store, snap = _health_run(tmp_path / label, events, polls=4)
        assert snap["state"] == "rate_limited_or_outage", label
        assert (snap["reason_code"], snap.get("reason_detail")) == ("provider_wait_parked", detail), label
        assert "usage_limit" not in json.dumps(snap), label
        assert not store.usage_limit_park_path(AGENT).exists(), label
        assert store.read_config_blocked_hold(AGENT) is None, label


def test_when_the_closed_vocabulary_has_no_fitting_word_the_detail_is_absent_not_invented(tmp_path):
    cases = {"unknown status": [SYSTEM, event("a_status_we_do_not_know"), result(True)],
             "unknown window": [SYSTEM, event("rejected", "a_window_we_do_not_know"), result(True)],
             "unproven rejection": case1()[:-1]}
    for label, events in cases.items():
        store, snap = _health_run(tmp_path / label.replace(" ", "-"), events, polls=4)
        assert snap["reason_code"] == "provider_wait_parked", label
        assert "reason_detail" not in snap, label
        assert "park_detail" not in ledger(store), label


def test_a_usage_limit_park_still_says_usage_limit_parked_with_no_detail(tmp_path):
    store, snap = _health_run(tmp_path, case1(), polls=4)
    assert (snap["reason_code"], snap.get("reason_detail")) == ("usage_limit_parked", None)
    assert store.usage_limit_park_path(AGENT).exists()


def test_a_change_of_cause_is_written_at_once(tmp_path):
    store = make_store(tmp_path)
    health = WrapperHealthWriter(store, AGENT, "claude", mode="wrapper", min_interval=3600.0)
    record = {"id": "m1", "request_id": None}
    health.parked(record, park.REASON_PROVIDER_WAIT, reason_detail="status_529")
    assert store.read_health_raw(AGENT)["reason_detail"] == "status_529"
    health.parked(record, park.REASON_PROVIDER_WAIT, reason_detail="status_429")        # throttled by a long interval
    assert store.read_health_raw(AGENT)["reason_detail"] == "status_429"
    health.parked(record, park.REASON_PARKED)
    snap = store.read_health_raw(AGENT)
    assert snap["reason_code"] == "usage_limit_parked" and "reason_detail" not in snap


def test_an_unsafe_detail_is_dropped_by_the_health_writer_not_written(tmp_path):
    store = make_store(tmp_path)
    health = WrapperHealthWriter(store, AGENT, "claude", mode="wrapper", min_interval=0.0)
    health.parked({"id": "m1"}, park.REASON_PROVIDER_WAIT, reason_detail="has spaces and SECRET text")
    snap = store.read_health_raw(AGENT)
    assert snap["reason_code"] == "provider_wait_parked" and "reason_detail" not in snap


# --------------------------------------------------------------------------- the one writer of the wake


def test_no_other_code_path_writes_the_wake_of_a_cooldown(tmp_path, monkeypatch):
    """Every persisted record of a cool-down kind carries a wake that the arming function produced (or none), across a
    full scenario: park, probes, a change of kind, a crash and an entry repair."""
    produced = set()
    original = park.arm_cooldown_wake

    def watching(rec, *, now_epoch, step):
        original(rec, now_epoch=now_epoch, step=step)
        if "wake_epoch" in rec:
            produced.add(rec["wake_epoch"])

    monkeypatch.setattr(park, "arm_cooldown_wake", watching)
    written = []
    real_write = Store._write_attempts

    def spying(self, agent, data):
        for rec in (data.get("messages") or {}).values():
            if isinstance(rec, dict) and park.is_cooldown(rec) and rec.get("park_state") in park.PARK_STATES:
                written.append(rec.get("wake_epoch"))
        return real_write(self, agent, data)

    monkeypatch.setattr(Store, "_write_attempts", spying)
    from test_provider_wait_loop import Crash, go

    store = make_store(tmp_path)
    clock = Clock(T0)
    go(store, Spawner(made_up_plain_429()), clock, polls=3)                              # park
    clock.t = float(ledger(store)["wake_epoch"])
    go(store, Spawner(made_up_529()), clock, polls=1)                                    # probe: another kind
    clock.t = float(ledger(store)["wake_epoch"])
    with pytest.raises(Crash):
        go(store, Spawner(Crash("died")), clock, polls=1, generation="g2")              # crash in a probe
    clock.t += 5
    go(store, Spawner(made_up_529()), clock, polls=2, generation="g3")                   # re-armed at the same step
    data = store.dead_letter_attempts(AGENT)
    data["messages"][head(store).id]["wake_epoch"] = "damaged"
    store._write_attempts(AGENT, data)
    written.clear()
    go(store, Spawner(made_up_529()), clock, polls=2, generation="g3")                   # entry repair
    assert produced and written
    assert all(wake is None or wake in produced for wake in written), (sorted(produced), written)
