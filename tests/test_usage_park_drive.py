"""The drive attaches the usage-limit fact BESIDE the unchanged failure class.

Fake spawners only: the two real captured Claude cases (sanitised, see
``golden_stop_retries_scenarios``) and edits of them. Nothing here starts a model.
"""

from __future__ import annotations

import copy
import json

import pytest

import golden_stop_retries_scenarios as real
from agenttalk.store import Store
from agenttalk.wrapper import loop, run, session
from agenttalk.wrapper import usage_park as park

RECORD = {"id": "20260909-030000-000001-Aaaa", "kind": "message", "from": "alpha", "to": "beta",
          "subject": "", "body": "do the thing"}


class Stream(list):
    """A fake child stream: its lines, plus the exit code and watchdog a real one carries."""

    def __init__(self, lines, *, returncode=None, watchdog=None):
        super().__init__(lines)
        self.returncode = returncode
        self.watchdog_result = watchdog


def lines_of(events):
    return [json.dumps(event) for event in events]


def drive_once(tmp_path, events, *, park_on=True, turns=0, returncode=None, watchdog=None):
    store = Store(tmp_path)
    store.init(["alpha", "beta"])
    drive = run.make_drive(
        store, "beta", "claude", session.SessionState(cli="claude", claude_session_id="sid-1", turns=turns),
        ["claude"], spawn=lambda argv, stdin: Stream(lines_of(events), returncode=returncode, watchdog=watchdog),
        clock=lambda: 0.0, render=False, heartbeat=lambda: None, usage_limit_park=park_on)
    return drive(dict(RECORD))


def case1():
    return copy.deepcopy(real.REAL_CASE_FIVE_HOUR)


def case2():
    return copy.deepcopy(real.REAL_CASE_SEVEN_DAY)


def test_the_two_real_cases_carry_the_fact_and_keep_their_class(tmp_path):
    for events, window, reset in ((case1(), "five_hour", 1788948000), (case2(), "seven_day", 1790370000)):
        on = drive_once(tmp_path / window, events)
        off = drive_once(tmp_path / (window + "-off"), events, park_on=False)
        assert on.ok is False and on.failure_class == loop.CLASS_INFRA
        assert (on.limit_fact, on.limit_window, on.limit_reset_epoch) == ("usage_limit", window, reset)
        # beside the class, never instead of it: class and summary are what they were
        assert (on.failure_class, on.summary) == (off.failure_class, off.summary)
        assert (off.limit_fact, off.limit_window, off.limit_reset_epoch) == (None, None, None)


def test_the_weekly_trap_is_infra_never_poison_and_the_text_is_never_read(tmp_path):
    events = case2()
    assert events[-1]["terminal_reason"] == "blocking_limit" and events[-1]["api_error_status"] is None
    first = drive_once(tmp_path / "a", events)
    assert first.failure_class == loop.CLASS_INFRA and first.limit_fact == "usage_limit"
    for event in events:
        for key in ("result", "compact_error"):
            if key in event:
                event[key] = "unrelated words"
        if event.get("type") == "assistant":
            event["message"]["content"][0]["text"] = "unrelated words"
    second = drive_once(tmp_path / "b", events)
    assert (second.failure_class, second.limit_fact, second.limit_reset_epoch) == (
        loop.CLASS_INFRA, "usage_limit", 1790370000)
    # the same words with the rejected event removed prove nothing
    bare = [e for e in case2() if e.get("type") != "rate_limit_event"]
    third = drive_once(tmp_path / "c", bare)
    assert third.limit_fact is None and third.failure_class != "usage_limit"


def test_a_rejected_event_in_a_turn_that_then_succeeds_parks_nothing(tmp_path):
    events = [case1()[1]] + [json.loads(line) for line in real.claude_turn()]
    outcome = drive_once(tmp_path, events)
    assert outcome.ok is True and outcome.limit_fact is None


def test_a_later_success_result_vetoes_the_fact_even_when_the_turn_failed_locally(tmp_path):
    events = case1()
    events[-1]["is_error"] = False
    for label, kwargs in (("nonzero exit", {"returncode": 1}),
                          ("watchdog", {"watchdog": {"summary": "turn watchdog killed a hung tool"}})):
        path = tmp_path / label.replace(" ", "-")
        outcome = drive_once(path, events, **kwargs)
        today = drive_once(tmp_path / (label.replace(" ", "-") + "-off"), events, park_on=False, **kwargs)
        assert outcome.ok is False and outcome.limit_fact is None, label
        assert (outcome.failure_class, outcome.summary) == (today.failure_class, today.summary), label
        # #305 F7 (fix round 2, connector 4177952848): the earlier rejected rate_limit_event
        # must not win just because it happened somewhere in the stream - the later success
        # vetoes it the same way it vetoes usage_park's own limit_fact, above. A red test
        # without the health.py fix: health kept claiming reason_code=="usage_limit_rejected"
        # here even though limit_fact was already (correctly) None.
        health = Store(path).read_health_raw("beta")
        assert health["reason_code"] != "usage_limit_rejected", label


def test_a_watchdog_keeps_its_own_class_and_never_gets_the_fact(tmp_path):
    outcome = drive_once(tmp_path, case1(), watchdog={"summary": "turn watchdog killed a hung tool"})
    assert outcome.failure_class == loop.CLASS_AMBIGUOUS and outcome.interrupted is True
    assert outcome.limit_fact is None


def test_a_nonzero_exit_after_a_provider_error_result_still_carries_the_fact(tmp_path):
    outcome = drive_once(tmp_path, case1(), returncode=1)
    assert outcome.failure_class == loop.CLASS_INFRA and outcome.limit_fact == "usage_limit"


def test_no_terminal_result_means_no_usage_limit_fact(tmp_path):
    outcome = drive_once(tmp_path, case1()[:-1])
    # A rejection never proven by an error result is only a suspected limit: the cool-down fact.
    assert outcome.ok is False and outcome.limit_fact == "throttled"
    assert outcome.limit_window is None and outcome.limit_reset_epoch is None


def test_a_result_flag_that_is_not_a_json_boolean_is_no_usage_limit_proof(tmp_path):
    for index, flag in enumerate(("true", 1, None)):
        events = case1()
        events[-1]["is_error"] = flag
        # Not proven (the merged rule needs exactly true). The rejected event is still unproven
        # evidence of trouble, so the cool-down fact stands (a non-boolean result is no veto).
        assert drive_once(tmp_path / str(index), events).limit_fact == "throttled"


def test_subtype_success_with_is_error_true_parks(tmp_path):
    events = case1()
    assert events[-1]["subtype"] == "success"
    assert drive_once(tmp_path, events).limit_fact == "usage_limit"


def test_a_resumed_turn_that_hits_the_limit_carries_the_fact_too(tmp_path):
    outcome = drive_once(tmp_path, case1(), turns=1)
    assert outcome.failure_class == loop.CLASS_INFRA and outcome.limit_fact == "usage_limit"
    assert outcome.limit_reset_epoch == 1788948000


def test_prose_alone_never_carries_the_fact(tmp_path):
    for index, text in enumerate(("usage limit reached", "quota exceeded", "429 Too Many Requests",
                                  "overloaded", "You've hit your weekly limit")):
        events = [{"type": "result", "is_error": True, "result": text, "api_error_status": 429}]
        outcome = drive_once(tmp_path / str(index), events)
        # Never the usage-limit fact. The structured 429 is a cool-down fact whatever the words say.
        assert outcome.limit_fact == "throttled", text
        words = [{**events[0], "result": "unrelated words"}]
        assert drive_once(tmp_path / (str(index) + "-words"), words).limit_fact == "throttled", text


@pytest.mark.parametrize("subtype,reason", [("overloaded_error", "overloaded"), ("rate_limit_error", "throttled")])
def test_a_terminal_subtype_with_no_http_status_still_reaches_the_right_health_reason(tmp_path, subtype, reason):
    """#305 F8 (fix round 2, connector 4177952837): a terminal result naming the provider's
    own throttle/overload subtype, with NO numeric HTTP status, used to fall through every
    upstream classification branch to CLASS_AMBIGUOUS/errored_ambiguous - hiding a genuine
    throttle/overload behind a meaningless "ambiguous failure" label. Driven through the
    real adapter and drive (never handing CLASS_INFRA to the health mapper directly): a red
    test without the run.py fix, since the upstream classifier only recognised a numeric
    429/529 status or an auth-outage shape, never these two subtypes alone."""
    events = [{"type": "result", "subtype": subtype, "is_error": True, "result": "synthetic failure"}]
    path = tmp_path / subtype
    outcome = drive_once(path, events)
    assert outcome.failure_class == loop.CLASS_INFRA, subtype
    health = Store(path).read_health_raw("beta")
    assert health["state"] == "rate_limited_or_outage", subtype
    assert health["reason_code"] == reason, subtype


def test_the_fact_needs_a_provider_side_failure_class():
    sig = {"usage_stream": {"rejected": {"window": "five_hour", "reset_epoch": 5},
                            "result_is_error": True}}
    assert run._usage_limit_fact(sig, loop.CLASS_INFRA) == {"window": "five_hour", "reset_epoch": 5}
    for other in (loop.CLASS_AMBIGUOUS, loop.CLASS_POISON, loop.CLASS_CONFIG_BLOCKED, loop.CLASS_GATEWAY_HELD):
        assert run._usage_limit_fact(sig, other) is None


@pytest.mark.parametrize("cause", [
    {"watchdog": {"summary": "x"}}, {"config_blocked": True}, {"bus_failure": {"summary": "x"}},
    {"setup_failure": {"summary": "x"}}, {"gateway_transient_hold": True},
])
def test_a_local_cause_wins_even_when_the_class_is_infra(cause):
    sig = {"usage_stream": {"rejected": {"window": "five_hour", "reset_epoch": 5}, "result_is_error": True}}
    sig.update(cause)
    assert run._usage_limit_fact(sig, loop.CLASS_INFRA) is None


def test_a_drive_with_the_park_off_never_looks_at_the_stream(tmp_path):
    store = Store(tmp_path)
    store.init(["alpha", "beta"])
    drive = run.make_drive(
        store, "beta", "claude", session.SessionState(cli="claude", claude_session_id="sid-1"), ["claude"],
        spawn=lambda argv, stdin: Stream(lines_of(case1())), clock=lambda: 0.0, render=False,
        heartbeat=lambda: None, usage_limit_park=False)
    assert drive(dict(RECORD)).limit_fact is None


def test_the_environment_switch_decides_when_the_argument_is_absent(tmp_path, monkeypatch):
    store = Store(tmp_path)
    store.init(["alpha", "beta"])

    def build():
        return run.make_drive(
            store, "beta", "claude", session.SessionState(cli="claude", claude_session_id="sid-1"),
            ["claude"], spawn=lambda argv, stdin: Stream(lines_of(case1())), clock=lambda: 0.0,
            render=False, heartbeat=lambda: None)

    monkeypatch.setenv(park.SWITCH_ENV, "0")
    assert build()(dict(RECORD)).limit_fact is None
    monkeypatch.delenv(park.SWITCH_ENV)
    assert build()(dict(RECORD)).limit_fact == "usage_limit"


def test_a_codex_drive_never_carries_the_fact(tmp_path):
    store = Store(tmp_path)
    store.init(["alpha", "beta"])
    drive = run.make_drive(
        store, "beta", "codex", session.SessionState(cli="codex"), ["codex"],
        spawn=lambda argv, stdin: Stream(lines_of(case1())), clock=lambda: 0.0, render=False,
        heartbeat=lambda: None, usage_limit_park=True)
    assert drive(dict(RECORD)).limit_fact is None
