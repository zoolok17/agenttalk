"""The park's wiring: the switch is read once and shared, the health writer says "outage
wait" with its own reason, and the notice is plain words that never pose as a dead letter.
"""

from __future__ import annotations

import pytest

from agenttalk import cli
from agenttalk.store import Store
from agenttalk.wrapper import loop, run
from agenttalk.wrapper import usage_park as park
from agenttalk.wrapper.health import WrapperHealthWriter

WAKE = 1788948030


def make_store(tmp_path) -> Store:
    store = Store(tmp_path)
    store.init(["alpha", "beta", "lead"])
    store.set_operator_facing("lead")
    return store


def info(**over):
    base = {"agent": "beta", "msg_id": "20260909-030000-000001-Aaaa", "from": "alpha", "kind": "message",
            "failure_class": "usage_limit", "attempts": 1,
            "usage_limit": {"notice_key": "park:1", "window": "five_hour", "reset_epoch": WAKE - 30,
                            "wake_epoch": WAKE, "again": False}}
    base.update(over)
    return base


# ------------------------------------------------------------------ the switch is read once


@pytest.mark.parametrize("value,expected", [(None, True), ("1", True), ("0", False)])
def test_the_drive_and_the_loop_get_the_same_flag(tmp_path, monkeypatch, value, expected):
    store = make_store(tmp_path)
    seen: dict = {}

    def fake_make_drive(*args, **kwargs):
        seen["drive"] = kwargs.get("usage_limit_park")
        monkeypatch.setenv(park.SWITCH_ENV, "0" if expected else "1")      # a change AFTER the start

        return lambda rec: True

    def fake_run_loop(store, agent, drive, **kwargs):
        seen["loop"] = kwargs.get("usage_limit_park")
        return 0

    if value is None:
        monkeypatch.delenv(park.SWITCH_ENV, raising=False)
    else:
        monkeypatch.setenv(park.SWITCH_ENV, value)
    monkeypatch.setattr(run, "make_drive", fake_make_drive)
    monkeypatch.setattr(loop, "run_loop", fake_run_loop)
    rc = cli._wrap_loop_mode(store, "beta", cli="claude", base_argv=["claude"], sender="beta",
                             min_interval=0.0, render=False)
    assert rc == 0
    assert seen == {"drive": expected, "loop": expected}


# ------------------------------------------------------------------ health


def writer(tmp_path, **kw):
    store = make_store(tmp_path)
    return store, WrapperHealthWriter(store, "beta", "claude", mode="wrapper", min_interval=kw.get("gap", 0.0))


def test_a_usage_park_is_an_outage_wait_with_its_own_reason(tmp_path):
    store, health = writer(tmp_path)
    health.parked({"id": "m1", "request_id": "rq-1"}, park.REASON_PARKED)
    snapshot = store.read_health_raw("beta")
    assert snapshot["state"] == "rate_limited_or_outage" and snapshot["reason_code"] == "usage_limit_parked"
    assert snapshot["msg_id"] == "m1" and snapshot["request_id"] == "rq-1"


def test_a_config_park_is_still_an_ambiguous_error_state(tmp_path):
    store, health = writer(tmp_path)
    health.parked({"id": "m1"})
    snapshot = store.read_health_raw("beta")
    assert snapshot["state"] == "errored_ambiguous" and snapshot["reason_code"] == "config_blocked"


def test_a_usage_park_changes_the_reason_at_once_and_throttles_repeats(tmp_path):
    store, health = writer(tmp_path, gap=3600.0)
    health.failure({"retryable": True}, loop.CLASS_INFRA)         # what the failed turn wrote
    assert store.read_health_raw("beta")["reason_code"] == "retryable_transport_error"
    health.parked({"id": "m1"}, park.REASON_PARKED)                # same state, new reason: written now
    assert store.read_health_raw("beta")["reason_code"] == "usage_limit_parked"
    before = store.read_health_raw("beta")["updated_at"]
    health.parked({"id": "m1"}, park.REASON_PARKED)                # a repeat: throttled
    assert store.read_health_raw("beta")["updated_at"] == before


def test_a_usage_park_after_an_idle_write_is_written_again(tmp_path):
    store, health = writer(tmp_path, gap=3600.0)
    health.parked({"id": "m1"}, park.REASON_PARKED)
    health.idle()
    health.parked({"id": "m1"}, park.REASON_PARKED)
    assert store.read_health_raw("beta")["state"] == "rate_limited_or_outage"


# ------------------------------------------------------------------ the notice


def sent(store):
    return list(store.messages_for("lead"))


def test_the_notice_goes_to_the_liaison_in_plain_words_with_its_facts(tmp_path):
    store = make_store(tmp_path)
    assert cli._dead_letter_notifier(store, "beta")(info(), disposed=False) is True
    (message,) = sent(store)
    body = message.body
    assert body.startswith("[usage-limit-parked] Seat beta is parked")
    assert "5-hour allowance is used up until 2026-09-09 10:00 UTC" in body
    assert "30 seconds after that time" in body and "each time it is started again" in body
    assert "NOT lost" in body and "NOT dead-lettered" in body
    assert "agenttalk request-restart --for beta" in body and "--force-protected" in body
    assert "--acknowledge-live-protected-kill" in body
    assert "agenttalk ack --for beta --id 20260909-030000-000001-Aaaa" in body
    assert "leaves no dead-letter record" in body and "managed lead-loop agent" in body
    assert message.meta["needs_operator"] == "true" and message.meta["usage_limit_park"] == "true"
    assert "dead_letter" not in message.meta and "config_blocked" not in message.meta
    assert "Inspect: agenttalk dead-letter" not in body


def test_the_notice_without_a_time_says_it_waits_for_a_start(tmp_path):
    store = make_store(tmp_path)
    facts = {"notice_key": "park:1", "window": "seven_day", "reset_epoch": None, "wake_epoch": None,
             "again": False}
    assert cli._dead_letter_notifier(store, "beta")(info(usage_limit=facts), disposed=False) is True
    body = sent(store)[0].body
    assert "weekly allowance is used up and no usable reset time was stated" in body
    assert "30 seconds" not in body and "each time it is started again" in body


def test_a_still_limited_notice_says_so(tmp_path):
    store = make_store(tmp_path)
    facts = {"notice_key": "probe:1:2", "window": "five_hour", "reset_epoch": WAKE - 30, "wake_epoch": WAKE,
             "again": True}
    cli._dead_letter_notifier(store, "beta")(info(usage_limit=facts), disposed=False)
    assert sent(store)[0].body.startswith("[usage-limit-parked] still limited: ")


def test_the_request_id_is_stable_for_one_transition_and_new_for_the_next(tmp_path):
    store = make_store(tmp_path)
    notify = cli._dead_letter_notifier(store, "beta")
    notify(info(), disposed=False)
    notify(info(), disposed=False)                      # a repeat after a crash: the same thread
    again = {"notice_key": "probe:1:2", "window": "five_hour", "reset_epoch": None, "wake_epoch": None,
             "again": True}
    notify(info(usage_limit=again), disposed=False)
    first, second, third = [m.meta["request_id"] for m in sent(store)]
    assert first == second != third and first.startswith("esc-")


def test_the_notice_carries_no_provider_text(tmp_path):
    store = make_store(tmp_path)
    cli._dead_letter_notifier(store, "beta")(info(summary="You've hit your weekly limit"), disposed=False)
    assert "weekly limit" not in sent(store)[0].body and "hit your" not in sent(store)[0].body


def test_no_target_means_not_routed(tmp_path):
    store = Store(tmp_path)
    store.init(["alpha", "beta"])
    assert cli._dead_letter_notifier(store, "beta")(info(), disposed=False) is False


def test_the_parked_agent_being_the_liaison_falls_back_to_a_separate_sole_lead(tmp_path):
    """#311 connector 4174800518: ``operator_facing() or sole_lead()`` short-circuits on the
    liaison even when the liaison IS the parked agent itself, so a separate sole lead was
    never even consulted and the notice went unrouted. The liaison must be excluded from its
    own fallback, the same way the final target is excluded from itself."""
    store = Store(tmp_path)
    store.init(["alpha", "beta", "lead_agent"])
    store.set_role("lead_agent", "lead")
    store.set_operator_facing("beta")                   # the parked agent is its own liaison
    assert cli._dead_letter_notifier(store, "beta")(info(), disposed=False) is True
    (message,) = sent_to(store, "lead_agent")
    assert message.body.startswith("[usage-limit-parked] Seat beta is parked")


def sent_to(store, recipient):
    return list(store.messages_for(recipient))


def test_the_config_blocked_notice_is_unchanged(tmp_path):
    store = make_store(tmp_path)
    cli._dead_letter_notifier(store, "beta")(
        {"agent": "beta", "msg_id": "m9", "from": "alpha", "kind": "message", "attempts": 2,
         "failure_class": "config_blocked", "summary": "exec denied"}, disposed=False)
    (message,) = sent(store)
    assert message.body.startswith("[wrapper-config-blocked]")
    assert message.meta["config_blocked"] == "true" and "usage_limit_park" not in message.meta
