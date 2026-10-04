"""A message parked on a provider usage limit and the turn journal do not contradict each other.

What the journal records for a park, through the REAL loop and the real drive (fake spawner, fake clock and
sleeps; nothing starts a model):

* the failed turn that proved the limit is ONE launched, failed dispatch, with the same closed failure class it has
  when the park switch is off; the park changes nothing about what the journal says of that turn;
* a park is not a consumption: while it lasts the journal hears nothing more about the message, and no second
  dispatch starts (the seat is waiting, not retrying);
* when the park ends in a successful probe, the journal records a new dispatch for the same message and then
  exactly one disposition, ``completed``;
* a project that does not use the journal reads exactly as before, parked or not.
"""

from __future__ import annotations

from typing import Any

import pytest

from agenttalk import cli
from agenttalk import turn_events as te
from agenttalk.wrapper import loop, run, session

from test_usage_park_loop import AGENT, T0, Clock, Spawner, case1, make_store, ok_turn


@pytest.fixture(autouse=True)
def _journal_root_in_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv(te.ENV_TURN_EVENTS_DIR, str(tmp_path / "journal-root"))
    monkeypatch.delenv(te.ENV_TURN_EVENTS, raising=False)


class Sink:
    """Stands in for the journal: keeps what the hooks emit."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, kind: str, **fields: Any) -> None:
        self.events.append((kind, dict(fields)))

    def of(self, kind: str) -> list[dict]:
        return [fields for k, fields in self.events if k == kind]

    def kinds(self) -> list[str]:
        return [k for k, _ in self.events]


def run_once(store, spawner, clock, *, polls, sink, disposed, park_on=True, generation="g1", max_turns=None):
    drive = run.make_drive(
        store, AGENT, "claude", session.SessionState(cli="claude", claude_session_id="sid-1"), ["claude"],
        spawn=spawner, clock=clock.now, render=False, usage_limit_park=park_on, turn_events=sink)
    return loop.run_loop(
        store, AGENT, drive, clock=clock.now, sleep=clock.sleep, now_iso=clock.iso, max_polls=polls,
        wrapper_generation=generation, usage_limit_park=park_on, max_turns=max_turns,
        on_message_disposed=lambda rec, disposition, facts: disposed.append((rec["id"], disposition, dict(facts))))


def valid(kind: str, fields: dict) -> None:
    te.validate_event({
        "v": 1, "kind": kind, "event_id": "00000000-0000-4000-8000-000000000000", "stream": "beta.g1",
        "at": "2026-10-03T00:00:00.000Z", "agent": "beta", "dropped_total": 0, "seq": 1, **fields})


def test_the_failed_turn_that_proved_the_limit_is_one_launched_failed_dispatch(tmp_path):
    store = make_store(tmp_path)
    sink, disposed = Sink(), []
    run_once(store, Spawner(case1()), Clock(T0), polls=3, sink=sink, disposed=disposed)
    assert sink.kinds() == ["dispatch_started", "dispatch_ended"]            # one turn; none while parked
    started, ended = sink.of("dispatch_started")[0], sink.of("dispatch_ended")[0]
    assert ended["turn_id"] == started["turn_id"]
    assert (ended["launched"], ended["outcome"], ended["exit"]) == (True, "failed", "normal")
    assert ended["failure_class"] in te.FAILURE_CLASSES
    valid("dispatch_started", started)
    valid("dispatch_ended", ended)


def test_the_park_changes_nothing_the_journal_says_of_that_turn(tmp_path):
    ended = {}
    for name, park_on in (("on", True), ("off", False)):
        store = make_store(tmp_path / name)
        sink = Sink()
        run_once(store, Spawner(case1()), Clock(T0), polls=1, sink=sink, disposed=[], park_on=park_on)
        ended[name] = sink.of("dispatch_ended")[0]
    for key in ("launched", "outcome", "exit", "failure_class", "usage"):
        assert ended["on"].get(key) == ended["off"].get(key), key


def test_a_park_is_not_a_consumption_and_the_journal_hears_nothing_while_it_lasts(tmp_path):
    store = make_store(tmp_path)
    sink, disposed = Sink(), []
    spawner = Spawner(case1())
    run_once(store, spawner, Clock(T0), polls=12, sink=sink, disposed=disposed)
    assert disposed == []                                                    # nothing was consumed
    assert spawner.calls == 1                                                # and nothing was retried
    assert sink.kinds() == ["dispatch_started", "dispatch_ended"]
    assert len(store.messages_for(AGENT)) == 1                               # the message is kept at the head


def test_a_successful_probe_gives_a_new_dispatch_and_exactly_one_completed_disposition(tmp_path):
    store = make_store(tmp_path)
    sink, disposed = Sink(), []
    run_once(store, Spawner(case1()), Clock(T0), polls=3, sink=sink, disposed=disposed)
    message_id = store.messages_for(AGENT)[0].id
    run_once(store, Spawner(ok_turn()), Clock(T0 + 100), polls=10, sink=sink, disposed=disposed,
             generation="g2", max_turns=1)
    assert sink.kinds() == ["dispatch_started", "dispatch_ended"] * 2
    first, second = sink.of("dispatch_ended")
    assert (first["outcome"], second["outcome"]) == ("failed", "success")
    assert {f["turn_id"] for f in sink.of("dispatch_started")} != {sink.of("dispatch_started")[0]["turn_id"]}
    assert [(i, d) for i, d, _f in disposed] == [(message_id, "completed")]  # one message, one disposition


def test_the_wakes_second_failure_does_not_become_a_disposition_either(tmp_path):
    store = make_store(tmp_path)
    sink, disposed = Sink(), []
    run_once(store, Spawner(case1()), Clock(T0), polls=3, sink=sink, disposed=disposed)
    run_once(store, Spawner(case1()), Clock(T0 + 100), polls=6, sink=sink, disposed=disposed, generation="g2")
    assert disposed == []
    assert [f["outcome"] for f in sink.of("dispatch_ended")] == ["failed", "failed"]


def test_a_project_that_does_not_use_the_journal_reads_as_before_parked_or_not(tmp_path):
    store = make_store(tmp_path)
    run_once(store, Spawner(case1()), Clock(T0), polls=3, sink=Sink(), disposed=[])
    health = store.read_health_raw(AGENT)
    assert cli._turn_journal_label(store, AGENT, health) is None
    view = store.usage_limit_park_view(AGENT, health=health, now_epoch=float(T0))
    assert view is not None and view["present"] is True
