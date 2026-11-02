"""An allowance warning must not become a wrapper-health outage (#597)."""

import json

import pytest

from agenttalk import health
from agenttalk.store import Store
from agenttalk.wrapper import claude_adapter, run, session
from agenttalk.wrapper.events import EventType
from agenttalk.wrapper.health import WrapperHealthWriter


def _allowance(status):
    return {"type": "rate_limit_event", "rate_limit_info": {
        "status": status, "rateLimitType": "seven_day", "utilization": 0.91,
    }}


def test_allowed_warning_during_successful_session_never_reports_outage(tmp_path, monkeypatch):
    store = Store(tmp_path)
    store.init(["beta"])
    state = session.SessionState(cli="claude", claude_session_id="test-session")
    snapshots = []
    write_health = store.write_health

    def capture(agent, snapshot):
        snapshots.append(snapshot.copy())
        write_health(agent, snapshot)

    monkeypatch.setattr(store, "write_health", capture)
    stream = [
        {"type": "stream_event", "event": {"type": "message_start"}},
        _allowance("allowed_warning"),
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Done."}]}},
        {"type": "stream_event", "event": {"type": "message_stop"}},
        {"type": "result", "is_error": False, "num_turns": 1},
        _allowance("allowed_warning"),
    ]
    drive = run.make_drive(
        store, "beta", "claude", state, ["claude"],
        spawn=lambda _argv, _stdin: [json.dumps(raw) for raw in stream],
        clock=lambda: 0.0, render=False,
    )
    for _ in range(2):
        outcome = drive({"body": "Complete a normal turn."})
        assert outcome.ok
        assert store.read_health_raw("beta")["state"] == health.STATE_IDLE_WAITING
    assert state.turns == 2
    assert state.claude_rate_limit["last"]["status"] == "allowed_warning"
    assert state.claude_rate_limit["windows"]["seven_day"]["used_percent"] == 91.0
    assert health.STATE_WORKING_TURN in {snap["state"] for snap in snapshots}
    assert all(snap["state"] != health.STATE_RATE_LIMITED_OR_OUTAGE for snap in snapshots)


@pytest.mark.parametrize("status", [None, "allowed", "allowed_warning"])
def test_benign_allowance_status_is_metadata(status):
    assert claude_adapter.map_event(_allowance(status)) == []


@pytest.mark.parametrize("status", ["rejected", "throttled", "blocked", "future_status", "", 0, False])
def test_other_allowance_status_still_reports_retryable_outage(tmp_path, status):
    store = Store(tmp_path)
    store.init(["beta"])
    writer = WrapperHealthWriter(store, "beta", "claude", mode="wrapper-loop")
    events = claude_adapter.map_event(_allowance(status))
    assert len(events) == 1
    assert events[0].type == EventType.ADAPTER_ERROR
    assert events[0].retryable is True
    writer.event(events[0])
    assert store.read_health_raw("beta")["state"] == health.STATE_RATE_LIMITED_OR_OUTAGE


@pytest.mark.parametrize("status,subtype,reason", [
    (429, "rate_limit_error", "throttled"),
    (529, "overloaded_error", "overloaded"),
])
def test_warning_does_not_hide_a_failed_turn(tmp_path, status, subtype, reason):
    store = Store(tmp_path)
    store.init(["beta"])
    state = session.SessionState(cli="claude", claude_session_id="test-session")
    stream = [
        _allowance("allowed_warning"),
        {"type": "result", "is_error": True, "api_error_status": status,
         "subtype": subtype, "result": "Request refused."},
    ]
    drive = run.make_drive(
        store, "beta", "claude", state, ["claude"],
        spawn=lambda _argv, _stdin: [json.dumps(raw) for raw in stream],
        clock=lambda: 0.0, render=False,
    )
    assert not drive({"body": "Try a turn."}).ok
    snapshot = store.read_health_raw("beta")
    assert snapshot["state"] == health.STATE_RATE_LIMITED_OR_OUTAGE
    assert snapshot["reason_code"] == reason
