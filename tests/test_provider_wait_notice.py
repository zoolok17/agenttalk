"""The notice for a message that is cooling down because the provider is overloaded or throttling.

Plain words, closed facts only, never provider text; it never calls an overload a usage limit, and it does not
promise that starting the seat again tries sooner (the saved retry time wins over a restart).
"""

from __future__ import annotations

from pathlib import Path

from agenttalk import cli
from agenttalk.store import Store
from agenttalk.wrapper import loop
from agenttalk.wrapper import usage_park as park

WAKE = 1_790_000_000


def _setup(tmp_path: Path):
    store = Store(tmp_path)
    store.init(["lead", "beta"])
    store.set_operator_facing("lead")
    sent = store.send(sender="lead", recipient="beta", body="work")
    return store, sent.id


def _info(msg_id, kind, key="rev:1", **extra):
    facts = {"notice_key": key, "window": None, "reset_epoch": None, "wake_epoch": WAKE, "again": False,
             "kind": kind, "next_try_epoch": WAKE}
    facts.update(extra)
    return {"agent": "beta", "msg_id": msg_id, "from": "lead", "kind": "message", "attempts": 1,
            "failure_class": loop.CLASS_USAGE_LIMIT, "usage_limit": facts}


def _notice(store):
    return store.messages_for("lead")[-1]


def test_an_overload_notice_says_waiting_the_schedule_and_only_the_skip_move(tmp_path):
    store, mid = _setup(tmp_path)
    assert cli._dead_letter_notifier(store, "beta")(_info(mid, "overloaded"), disposed=False) is True
    body = _notice(store).body
    when = park.format_epoch(WAKE)
    assert body.startswith("[provider-wait-parked] Seat beta is waiting: its AI provider looks overloaded.")
    assert f"Message {mid} from lead was NOT lost and was NOT dead-lettered." in body
    assert f"It tries again at {when}, then 30 minutes after that, then every hour." in body
    assert f"agenttalk ack --for beta --id {mid}" in body and "leaves no dead-letter record" in body
    lowered = body.lower()
    for forbidden in ("usage limit", "usage-limit", "allowance", "request-restart", "start it again now", "edit"):
        assert forbidden not in lowered, forbidden


def test_a_throttle_notice_says_it_looks_like_a_usage_limit_but_could_not_be_confirmed(tmp_path):
    store, mid = _setup(tmp_path)
    cli._dead_letter_notifier(store, "beta")(_info(mid, "throttled"), disposed=False)
    body = _notice(store).body
    assert body.startswith("[provider-wait-parked] Seat beta is waiting: its provider returned an error that looks "
                           "like a usage limit but could not be confirmed.")
    assert "request-restart" not in body and "start it again now" not in body


def test_without_a_next_try_time_it_says_fifteen_minutes(tmp_path):
    store, mid = _setup(tmp_path)
    cli._dead_letter_notifier(store, "beta")(_info(mid, "overloaded", next_try_epoch=None), disposed=False)
    assert "It tries again in 15 minutes, then 30 minutes after that, then every hour." in _notice(store).body


def test_the_notice_is_informational_and_its_request_id_follows_the_transition(tmp_path):
    store, mid = _setup(tmp_path)
    emit = cli._dead_letter_notifier(store, "beta")
    emit(_info(mid, "overloaded", key="rev:1"), disposed=False)
    first = _notice(store)
    emit(_info(mid, "overloaded", key="rev:1"), disposed=False)          # the same transition again (a crash repeat)
    emit(_info(mid, "throttled", key="rev:2"), disposed=False)           # a change of kind: its own notice
    again, second = store.messages_for("lead")[-2:]
    assert first.meta["request_id"] == again.meta["request_id"] != second.meta["request_id"]
    assert "needs_operator" not in first.meta and first.meta["usage_limit_park"] == "true"
    assert first.meta["request_id"].startswith("esc-")


def test_the_notice_is_unrouted_when_nobody_can_receive_it(tmp_path):
    store = Store(tmp_path)
    store.init(["alpha", "beta"])                                        # no liaison and no sole lead
    sent = store.send(sender="alpha", recipient="beta", body="work")
    assert cli._dead_letter_notifier(store, "beta")(_info(sent.id, "overloaded"), disposed=False) is False


def test_a_usage_limit_notice_is_byte_for_byte_what_it_was(tmp_path):
    store, mid = _setup(tmp_path)
    info = _info(mid, "usage_limit", key="park:1", window="five_hour")
    info["usage_limit"].pop("kind")
    info["usage_limit"].pop("next_try_epoch")
    wake = WAKE
    cli._dead_letter_notifier(store, "beta")(info, disposed=False)
    reset = park.format_epoch(wake - 30)
    expected = (
        "[usage-limit-parked] Seat beta is parked: its Claude 5-hour allowance is used up until "
        f"{reset}. It tries once more 30 seconds after that time, and once each time it is started again. "
        f"Message {mid} from lead was NOT lost and was NOT dead-lettered. " + park.recovery_text("beta", mid))
    assert _notice(store).body == expected
    assert "request-restart --for beta" in expected                       # the usage-limit recovery text is unchanged


def test_the_recovery_text_of_a_cooldown_never_promises_a_try_now(tmp_path):
    text = park.recovery_text("beta", "m1", "overloaded")
    assert "keeps its saved retry time" in text and "agenttalk ack --for beta --id m1" in text
    assert "request-restart" not in text and "start it again now" not in text
    assert park.recovery_text("beta", "m1", "usage_limit") == park.recovery_text("beta", "m1")


def test_a_message_with_unknown_kind_words_in_the_facts_is_a_plain_usage_limit_notice(tmp_path):
    store, mid = _setup(tmp_path)
    info = _info(mid, "something_else", key="park:1", window="five_hour")
    cli._dead_letter_notifier(store, "beta")(info, disposed=False)
    assert _notice(store).body.startswith("[usage-limit-parked]")
