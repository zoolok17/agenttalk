"""Class B: a saved observation expires at every reader, without a new publication (#301)."""

import http.client
import io
import json
from contextlib import redirect_stdout
from datetime import timedelta

import pytest

from agenttalk import capacity as cap, checkpoint, cli, web
from capacity_class_helpers import AT, Clock, isolate, statusline

CONSUMERS = (
    "cli-text", "status-row", "attention-budget", "attention-context", "attention-refusal",
    "web", "checkpoint-file", "checkpoint-sidecar", "cli-command", "web-route",
)


@pytest.fixture
def observations(tmp_path, monkeypatch, store):
    isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(web, "datetime", Clock)
    home = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    # The manual checkpoint currently reads the caller's file (#408). Keep this
    # an own-seat fixture: expiry is separate from the strict xfail for another seat.
    monkeypatch.setenv("AGENTTALK_SELF", "alpha")
    statusline(home, 97)
    (tmp_path / "cc-ctx-target-session.json").write_text(json.dumps({
        "context_pct": 97, "context_limit": 1000, "context_used": 970,
        "updated_at": AT.isoformat(),
    }), encoding="utf-8")
    return store


def _saved(consumer, *, mixed=False):
    snap = cap.CapacitySnapshot.unknown("alpha").to_dict()
    snap.update(source="claude_stream", confidence="observed", observed_at=AT.isoformat(),
                primary_observed_at=AT.isoformat(), primary_used_percent=99,
                primary_resets_at=int(AT.timestamp()) + 3600,
                context_used_percent=97, context_window_size=1000, context_tokens=970)
    if consumer == "attention-context":
        snap["primary_used_percent"] = 12
    if consumer == "attention-refusal" or mixed:
        snap.update(primary_status="rejected", rate_limit_reached_type="five_hour",
                    last_status="rejected", last_status_window="five_hour", last_status_at=AT.isoformat())
    if mixed:
        snap.update(secondary_used_percent=20, secondary_status="allowed",
                    secondary_observed_at=(AT + timedelta(seconds=600)).isoformat())
    return snap


def _route_capacity(store):
    server, thread, _ = web.serve_in_thread(store, host="127.0.0.1", port=0)
    connection = http.client.HTTPConnection(*server.server_address, timeout=5)
    try:
        connection.request("GET", "/api/state")
        response = connection.getresponse()
        assert response.status == 200, "web route request failed"
        payload = json.loads(response.read())
        return next(agent["capacity"] for agent in payload["roots"][0]["agents"]
                    if agent["name"] == "alpha")
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive(), "web server did not stop"


def _check(consumer, store, *, expired, mixed=False):
    snap = store.read_capacity("alpha")
    live = not expired or mixed
    if consumer in ("cli-text", "cli-command"):
        if consumer == "cli-command":
            output = io.StringIO()
            with redirect_stdout(output):
                result = cli.main(["--root", str(store.root), "capacity", "show"])
            assert result == 0, "capacity show failed"
            text = output.getvalue()
        else:
            text = cli._capacity_text(snap, threshold=90, reset_soon_min=30)
        assert ("99% used" in text) == (not expired), "expiry: CLI budget"
        assert ("context 97%" in text) == (not expired), "expiry: CLI context"
        if mixed:
            assert "20% used" in text, "expiry: valid weekly reading dropped"
            assert "rejected" not in text, "expiry: old refusal shown"
        else:
            assert ("stale" in text) == expired, "expiry: CLI confidence"
    elif consumer == "status-row":
        row = next(row for row in cli._gather_status(store)["agents"] if row["name"] == "alpha")
        assert row["capacity"]["state"] == ("observed" if live else "stale"), "expiry: status row"
    elif consumer.startswith("attention"):
        signals = cli._tripped_capacity_signals(store)
        expected = [] if expired else [{
            "agent": "alpha", "kind": consumer.removeprefix("attention-"),
        }]
        if consumer == "attention-refusal" and expected:
            expected[0]["kind"] = "rate_limit"
        assert [{k: s[k] for k in ("agent", "kind")} for s in signals] == expected, "expiry: attention"
    elif consumer in ("web", "web-route"):
        entry = (_route_capacity(store) if consumer == "web-route"
                 else web._capacity_entry(snap, now=Clock.instant))
        assert entry["rate_used_pct"] == (None if expired else 99), "expiry: web budget"
        assert entry["context_used_pct"] == (None if expired else 97), "expiry: web context"
        assert entry["confidence"] == ("fresh" if live else "stale"), "expiry: web confidence"
        if expired:
            assert entry.get("context") is None, "expiry: web context detail"
            assert (entry.get("primary") or {}).get("resets_at") is None, "expiry: web reset"
            assert entry.get("rate_limit_reached_type") is None, "expiry: web refusal"
        if mixed:
            assert entry["secondary"]["used_pct"] == 20, "expiry: valid web weekly reading dropped"
    else:
        result = checkpoint.collect_context(
            "alpha", source="claude", session_id="target-session",
            session_scoped=consumer == "checkpoint-sidecar")
        assert result == ({"pct": None, "limit": None, "used": None, "source": None} if expired else {
            "pct": 97, "limit": 1000, "used": 970, "source": "sidecar",
        }), "expiry: checkpoint context"


@pytest.mark.parametrize("consumer", CONSUMERS)
def test_expiry_at_every_consumer(consumer, observations, monkeypatch):
    store = observations
    store.write_capacity("alpha", _saved(consumer))
    # The fixed 600-second contract is independent of the implementation's default.
    for seconds, expired in ((0, False), (600, False), (601, True)):
        monkeypatch.setattr(Clock, "instant", AT + timedelta(seconds=seconds))
        _check(consumer, store, expired=expired)


@pytest.mark.parametrize("consumer", ("cli-text", "status-row", "attention-refusal", "web"))
def test_new_weekly_reading_does_not_revive_expired_parts(consumer, observations, monkeypatch):
    observations.write_capacity("alpha", _saved(consumer, mixed=True))
    monkeypatch.setattr(Clock, "instant", AT + timedelta(seconds=601))
    _check(consumer, observations, expired=True, mixed=True)


@pytest.mark.parametrize("consumer", CONSUMERS)
def test_seeded_freshness_bypass_is_detected(consumer, observations, monkeypatch):
    observations.write_capacity("alpha", _saved(consumer))
    _check(consumer, observations, expired=False)
    monkeypatch.setattr(Clock, "instant", AT + timedelta(seconds=601))
    _check(consumer, observations, expired=True)
    with pytest.MonkeyPatch.context() as mutation:
        if consumer.startswith("checkpoint"):
            mutation.setattr(cap, "for_publication", lambda snap, **kwargs: snap)
        elif consumer == "cli-command":
            # Simulate a display handler using the stored figures without checking age.
            mutation.setattr(cli, "_capacity_text", lambda snap, **kwargs:
                             f"{snap['primary_used_percent']}% used; context {snap['context_used_percent']}%")
        elif consumer == "web-route":
            original = web._capacity_entry
            mutation.setattr(web, "_capacity_entry", lambda snap, **kwargs: original(snap, now=AT))
        else:
            mutation.setattr(cap, "current_view", lambda snap, **kwargs: dict(snap))
        with pytest.raises(AssertionError, match="expiry:"):
            _check(consumer, observations, expired=True)
    _check(consumer, observations, expired=True)
