"""The drive attaches a cool-down fact (``throttled`` or ``overloaded``) BESIDE the unchanged failure class.

Fake spawners only. The two real captured Claude cases prove what they prove (a usage limit); the 529 result and
the plain-429 result below are MADE UP: no real sample of either exists, and every fixture says so. ``allowed_warning``
is harmless because of the code's existing rule (``tests/test_usage_park_rules.py``), not because of a capture.
"""

from __future__ import annotations

import copy
import json

import pytest

import golden_stop_retries_scenarios as real
from agenttalk.wrapper import loop, run, session
from agenttalk.store import Store
from test_usage_park_drive import RECORD, Stream, case1, case2, drive_once, lines_of

SYSTEM = {"type": "system", "subtype": "status", "status": "requesting"}


def made_up_529(subtype=None):
    """MADE UP: a terminal 529 result with no rate_limit_event (no real sample of an overload exists)."""
    result = {"type": "result", "subtype": subtype or "success", "is_error": True, "api_error_status": 529,
              "result": "made-up overload text"}
    return [copy.deepcopy(SYSTEM), result]


def made_up_plain_429():
    """MADE UP: the real five-hour case with its rate_limit_event deleted, a plain 429 result."""
    return [e for e in case1() if e.get("type") != "rate_limit_event"]


def event(status, window="five_hour", **extra):
    info = {"status": status, "rateLimitType": window, "resetsAt": 1788948000}
    info.update(extra)
    return {"type": "rate_limit_event", "rate_limit_info": info}


def result(is_error, status=None, **extra):
    doc = {"type": "result", "subtype": "success", "is_error": is_error}
    if status is not None:
        doc["api_error_status"] = status
    doc.update(extra)
    return doc


def facts(outcome):
    return (outcome.limit_fact, outcome.limit_window, outcome.limit_reset_epoch, outcome.limit_detail)


def same_as_today(tmp_path, events, **kwargs):
    """Class and summary are what they are with the whole feature switched off."""
    on = drive_once(tmp_path / "on", events, **kwargs)
    off = drive_once(tmp_path / "off", events, park_on=False, **kwargs)
    assert (on.failure_class, on.summary) == (off.failure_class, off.summary)
    assert facts(off) == (None, None, None, None)
    return on


# --------------------------------------------------------------------------- the two facts


def test_a_made_up_529_result_is_overloaded_with_the_closed_detail_and_no_window(tmp_path):
    outcome = same_as_today(tmp_path, made_up_529())
    assert outcome.failure_class == loop.CLASS_INFRA
    assert facts(outcome) == ("overloaded", None, None, "status_529")
    assert outcome.limit_provider == "claude"


def test_a_made_up_529_with_its_own_subtype_names_it_in_the_detail(tmp_path):
    outcome = same_as_today(tmp_path, made_up_529("overloaded_error"))
    assert outcome.limit_fact == "overloaded" and outcome.limit_detail == "status_529.overloaded_error"


def test_a_subtype_that_is_not_ours_is_never_copied_into_the_detail(tmp_path):
    outcome = same_as_today(tmp_path, made_up_529("SECRET-looking_subtype_9f3a"))
    assert outcome.limit_fact == "overloaded" and outcome.limit_detail == "status_529"


def test_a_made_up_plain_429_result_is_throttled(tmp_path):
    outcome = same_as_today(tmp_path, made_up_plain_429())
    assert outcome.failure_class == loop.CLASS_INFRA
    assert facts(outcome) == ("throttled", None, None, "status_429")


def test_the_two_real_cases_are_still_usage_limit_and_carry_no_cooldown_detail(tmp_path):
    for name, events, window in (("a", case1(), "five_hour"), ("b", case2(), "seven_day")):
        outcome = same_as_today(tmp_path / name, events)
        assert (outcome.limit_fact, outcome.limit_window, outcome.limit_detail) == ("usage_limit", window, None)


def test_429_and_529_together_are_throttled(tmp_path):
    for order in ((429, 529), (529, 429)):
        events = [SYSTEM, result(True, order[0]), result(True, order[1])]
        assert drive_once(tmp_path / str(order[0]), events).limit_fact == "throttled", order


# ------------------------------------------------------------------ no fitting detail word: absent, never invented


def test_an_unknown_window_name_is_throttled_with_no_detail(tmp_path):
    events = [SYSTEM, event("rejected", "a_window_we_do_not_know"), result(True)]
    outcome = same_as_today(tmp_path, events)
    assert facts(outcome) == ("throttled", None, None, None)


def test_an_unknown_status_word_is_throttled_with_no_detail(tmp_path):
    events = [SYSTEM, event("a_status_we_do_not_know"), result(True)]
    assert facts(same_as_today(tmp_path, events)) == ("throttled", None, None, None)


def test_a_rejection_never_proven_by_an_error_result_is_throttled_with_no_detail(tmp_path):
    outcome = same_as_today(tmp_path, case1()[:-1])                    # the real event, no result after it
    assert facts(outcome) == ("throttled", None, None, None)


def test_a_known_window_with_a_non_boolean_result_is_throttled_with_no_detail(tmp_path):
    events = case1()
    events[-1]["is_error"] = "true"
    events[-1].pop("api_error_status")
    assert facts(drive_once(tmp_path, events)) == ("throttled", None, None, None)


# --------------------------------------------------------------------------- what is not a fact


def test_allowed_status_words_are_harmless(tmp_path):
    for word in ("allowed", "allowed_warning"):
        done = [SYSTEM, event(word)] + [json.loads(line) for line in real.claude_turn()]
        assert drive_once(tmp_path / word, done).limit_fact is None
        # a harmless warning followed by an unrelated failure: today's retry class, still no fact
        failed = [SYSTEM, event(word), result(True, 500)]
        assert same_as_today(tmp_path / (word + "-500"), failed).limit_fact is None


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_other_server_errors_are_untouched(tmp_path, status):
    outcome = same_as_today(tmp_path, [SYSTEM, result(True, status)])
    assert outcome.limit_fact is None and outcome.failure_class == loop.CLASS_INFRA


def test_an_auth_outage_and_a_transport_drop_are_untouched(tmp_path):
    auth = [SYSTEM, result(True, 401, subtype="authentication_error")]
    assert same_as_today(tmp_path / "auth", auth).limit_fact is None
    assert same_as_today(tmp_path / "drop", [SYSTEM], returncode=1).limit_fact is None


def test_a_result_with_no_status_and_no_event_is_no_fact(tmp_path):
    assert same_as_today(tmp_path, [SYSTEM, result(True)]).limit_fact is None


# ------------------------------------------------------------------ the success veto (the order of the stream decides)


def test_an_event_then_a_successful_result_then_exit_one_gives_no_fact_and_todays_class(tmp_path):
    events = [SYSTEM, event("a_status_we_do_not_know"), result(False)]
    outcome = same_as_today(tmp_path, events, returncode=1)
    assert outcome.ok is False and outcome.limit_fact is None


def test_the_same_veto_holds_with_the_watchdog_and_never_gives_a_fact(tmp_path):
    events = [SYSTEM, event("a_status_we_do_not_know"), result(False)]
    outcome = drive_once(tmp_path, events, watchdog={"summary": "turn watchdog killed a hung tool"})
    assert outcome.limit_fact is None and outcome.interrupted is True


@pytest.mark.parametrize("status", [429, 529])
def test_an_error_result_then_a_later_successful_result_gives_no_fact(tmp_path, status):
    outcome = drive_once(tmp_path, [SYSTEM, result(True, status), result(False)], returncode=1)
    assert outcome.limit_fact is None


def test_a_successful_result_before_the_event_still_gives_the_fact(tmp_path):
    events = [SYSTEM, result(False), event("a_status_we_do_not_know"), result(True)]
    assert drive_once(tmp_path, events).limit_fact == "throttled"


def test_an_event_then_the_process_died_is_still_a_suspected_limit(tmp_path):
    outcome = drive_once(tmp_path, [SYSTEM, event("a_status_we_do_not_know")], returncode=1)
    assert outcome.limit_fact == "throttled"


def test_a_529_error_result_then_a_nonzero_exit_still_gives_overloaded(tmp_path):
    outcome = drive_once(tmp_path, made_up_529(), returncode=1)
    assert outcome.limit_fact == "overloaded"


def test_the_veto_changes_neither_the_merged_fold_nor_its_cases(tmp_path):
    """The merged rule is untouched: the last result after the rejection decides (rejected, false, true proves)."""
    events = case1()
    events.insert(-1, result(False))
    outcome = drive_once(tmp_path, events)
    assert (outcome.limit_fact, outcome.limit_window) == ("usage_limit", "five_hour")


def drive_lines(tmp_path, lines, *, park_on, returncode=None):
    store = Store(tmp_path)
    store.init(["alpha", "beta"])
    drive = run.make_drive(
        store, "beta", "claude", session.SessionState(cli="claude", claude_session_id="sid-1"), ["claude"],
        spawn=lambda argv, stdin: Stream(lines, returncode=returncode), clock=lambda: 0.0, render=False,
        heartbeat=lambda: None, usage_limit_park=park_on)
    return drive(dict(RECORD))


def test_an_unparsed_limit_like_line_then_a_parsed_success_with_exit_one_is_today_s_retry(tmp_path):
    """The partial-stream trace the scope cut pins: no message_start, limit-like text, a parsed success."""
    lines = ["You've hit your usage limit 429 overloaded", json.dumps(result(False))]
    on = drive_lines(tmp_path / "s1", lines, park_on=True, returncode=1)
    off = drive_lines(tmp_path / "s2", lines, park_on=False, returncode=1)
    assert on.limit_fact is None and (on.failure_class, on.summary) == (off.failure_class, off.summary)


# --------------------------------------------------------------------------- local causes, the switch, Codex, and text


def test_a_watchdog_keeps_its_own_class_and_never_gets_a_cooldown_fact(tmp_path):
    outcome = drive_once(tmp_path, made_up_529(), watchdog={"summary": "turn watchdog killed a hung tool"})
    assert outcome.limit_fact is None and outcome.interrupted is True


def test_the_switch_off_gives_no_cooldown_fact_for_any_case(tmp_path):
    cases = {"529": made_up_529(), "429": made_up_plain_429(),
             "unknown": [SYSTEM, event("a_status_we_do_not_know"), result(True)], "event-only": case1()[:-1]}
    for name, events in cases.items():
        off = drive_once(tmp_path / name, events, park_on=False)
        assert facts(off) == (None, None, None, None), name


def test_a_codex_seat_gets_no_cooldown_fact_from_claude_shaped_lines(tmp_path):
    for name, events in (("529", made_up_529()), ("429", made_up_plain_429()),
                         ("unknown", [SYSTEM, event("a_status_we_do_not_know"), result(True)])):
        store = Store(tmp_path / name)
        store.init(["alpha", "beta"])
        drive = run.make_drive(store, "beta", "codex", session.SessionState(cli="codex"), ["codex"],
                               spawn=lambda argv, stdin, e=events: Stream(lines_of(e), returncode=1),
                               clock=lambda: 0.0, render=False, heartbeat=lambda: None, usage_limit_park=True)
        outcome = drive(dict(RECORD))
        assert facts(outcome) == (None, None, None, None), name


def test_no_text_is_read_for_the_decision(tmp_path):
    events = made_up_529()
    first = drive_once(tmp_path / "a", events)
    for item in events:
        for key in ("result", "compact_error"):
            if key in item:
                item[key] = "unrelated words"
    second = drive_once(tmp_path / "b", events)
    assert facts(first) == facts(second) == ("overloaded", None, None, "status_529")
    words = made_up_plain_429()
    words[-1]["result"] = "unrelated words"
    assert drive_once(tmp_path / "c", words).limit_fact == "throttled"
