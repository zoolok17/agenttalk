"""#305: health classifies a retryable adapter error from structured evidence first,
text matching only as a narrow, whole-phrase last resort - never the bare substring
"rate" (which also matched "generate", "separate", "iterate"). A short, closed-
vocabulary excerpt travels with the reason; the label clears on the next success;
an older on-disk record with no reason_detail at all still reads.
"""

from __future__ import annotations

import time
from pathlib import Path

from agenttalk import cli, health as hm
from agenttalk.store import Store
from agenttalk.wrapper.events import Event, EventType
from agenttalk.wrapper.health import WrapperHealthWriter, classify_failure
from agenttalk.wrapper.loop import CLASS_CONFIG_BLOCKED, CLASS_INFRA


def _store(tmp_path: Path) -> Store:
    s = Store(tmp_path)
    s.init(["beta"])
    return s


def _writer(tmp_path: Path, cli: str = "claude") -> tuple[Store, WrapperHealthWriter]:
    store = _store(tmp_path)
    return store, WrapperHealthWriter(store, "beta", cli, mode="wrapper-loop", min_interval=0.0)


def _rate_limit_event_raw(status: str, window: str | None = "five_hour", reset: int | None = 1788948000) -> dict:
    info: dict = {"status": status}
    if window is not None:
        info["rateLimitType"] = window
    if reset is not None:
        info["resetsAt"] = reset
    return {"type": "rate_limit_event", "rate_limit_info": info}


# ------------------------------------------------------------------ event(): per-stream-event


def test_ordinary_words_give_no_rate_limit_reason(tmp_path):
    """The bare substring "rate" used to match "generate"/"separate"/"iterate"."""
    _, writer = _writer(tmp_path)
    for word in ("Let me generate a plan for this.", "Trying to iterate over the results.",
                 "These are kept separate on purpose."):
        writer.event(Event(EventType.ADAPTER_ERROR, text=word, retryable=True, raw={}))
        snap = writer.store.read_health_raw("beta")
        assert snap["reason_code"] == "adapter_retryable_error", word
        assert snap.get("reason_detail") is None, word


def test_a_rejected_rate_limit_event_gives_the_usage_limit_reason(tmp_path):
    store, writer = _writer(tmp_path)
    raw = _rate_limit_event_raw("rejected", window="five_hour")
    writer.event(Event(EventType.ADAPTER_ERROR, text="rate_limit: rejected", retryable=True, raw=raw))
    snap = store.read_health_raw("beta")
    assert snap["state"] == hm.STATE_RATE_LIMITED_OR_OUTAGE
    assert snap["reason_code"] == "usage_limit_rejected"
    assert snap["reason_detail"] == "rate_limit_event.rejected.five_hour"


def test_a_rejected_event_with_an_unknown_window_does_not_claim_usage_limit(tmp_path):
    store, writer = _writer(tmp_path)
    raw = _rate_limit_event_raw("rejected", window="some_future_window")
    writer.event(Event(EventType.ADAPTER_ERROR, text="rate_limit: rejected", retryable=True, raw=raw))
    snap = store.read_health_raw("beta")
    assert snap["reason_code"] != "usage_limit_rejected"


def test_the_usage_limit_proof_only_applies_to_claude(tmp_path):
    """The SAME rate_limit_event shape from a non-Claude CLI is not trusted as the
    usage-limit proof (the proof is Claude-specific, per usage_park.py)."""
    store, writer = _writer(tmp_path, cli="codex")
    raw = _rate_limit_event_raw("rejected", window="five_hour")
    writer.event(Event(EventType.ADAPTER_ERROR, text="hit a rate limit", retryable=True, raw=raw))
    snap = store.read_health_raw("beta")
    assert snap["reason_code"] != "usage_limit_rejected"
    # it still falls through to the (also narrowed) text match: "rate limit" is a whole phrase.
    assert snap["reason_code"] == "adapter_rate_limit"


def test_whole_phrase_text_markers_are_the_last_resort(tmp_path):
    store, writer = _writer(tmp_path)
    for text in ("Error: rate limit exceeded", "429 too many requests", "over quota for this month"):
        writer.event(Event(EventType.ADAPTER_ERROR, text=text, retryable=True, raw={}))
        snap = store.read_health_raw("beta")
        assert snap["reason_code"] == "adapter_rate_limit", text
        assert isinstance(snap.get("reason_detail"), str), text


def test_an_unrecognized_retryable_error_keeps_the_existing_generic_reason(tmp_path):
    store, writer = _writer(tmp_path)
    writer.event(Event(EventType.ADAPTER_ERROR, text="a transient hiccup", retryable=True, raw={}))
    snap = store.read_health_raw("beta")
    assert snap["reason_code"] == "adapter_retryable_error"
    assert snap.get("reason_detail") is None


# ------------------------------------------------------------------ classify_failure(): end of turn


def _structured(kind="result", **over):
    base = {"source": "claude", "kind": kind, "retryable": False}
    base.update(over)
    return base


def test_a_429_terminal_result_gives_throttled():
    sig = {"structured_errors": [_structured(api_error_status=429, subtype="rate_limit_error")]}
    state, reason, detail = classify_failure(sig, CLASS_INFRA)
    assert state == hm.STATE_RATE_LIMITED_OR_OUTAGE
    assert reason == "throttled"
    assert detail is not None and "429" in detail


def test_a_529_terminal_result_gives_overloaded():
    sig = {"structured_errors": [_structured(api_error_status=529, subtype="overloaded_error")]}
    state, reason, detail = classify_failure(sig, CLASS_INFRA)
    assert state == hm.STATE_RATE_LIMITED_OR_OUTAGE
    assert reason == "overloaded"
    assert detail is not None and "529" in detail


def test_subtype_alone_is_enough_without_a_numeric_status():
    sig = {"structured_errors": [_structured(api_error_status=None, subtype="overloaded_error")]}
    state, reason, _detail = classify_failure(sig, CLASS_INFRA)
    assert (state, reason) == (hm.STATE_RATE_LIMITED_OR_OUTAGE, "overloaded")


def test_infra_with_no_structured_match_keeps_the_existing_generic_reasons():
    assert classify_failure({"retryable": True}, CLASS_INFRA)[:2] == (
        hm.STATE_RATE_LIMITED_OR_OUTAGE, "retryable_transport_error")
    assert classify_failure({"retryable": False}, CLASS_INFRA)[:2] == (
        hm.STATE_RATE_LIMITED_OR_OUTAGE, "terminal_infra_error")


def test_an_unrelated_structured_error_does_not_falsely_claim_throttled():
    sig = {"structured_errors": [_structured(api_error_status=500, subtype="internal_server_error")],
           "retryable": True}
    state, reason, detail = classify_failure(sig, CLASS_INFRA)
    assert (state, reason, detail) == (hm.STATE_RATE_LIMITED_OR_OUTAGE, "retryable_transport_error", None)


def test_an_unrecognized_subtype_never_reaches_the_stored_detail():
    """Fix round 1, connector 4177637208: shape is not privacy - a token-shaped but
    UNRECOGNIZED subtype must be dropped even though the numeric status alone is enough
    to classify the reason."""
    sig = {"structured_errors": [_structured(api_error_status=429, subtype="synthetic-private-marker")]}
    state, reason, detail = classify_failure(sig, CLASS_INFRA)
    assert (state, reason) == (hm.STATE_RATE_LIMITED_OR_OUTAGE, "throttled")
    assert detail == "status_429"
    assert "synthetic-private-marker" not in (detail or "")


def test_a_recognized_subtype_with_no_numeric_status_is_still_closed_vocabulary():
    sig = {"structured_errors": [_structured(api_error_status="not-a-number", subtype="overloaded_error")]}
    state, reason, detail = classify_failure(sig, CLASS_INFRA)
    assert (state, reason, detail) == (hm.STATE_RATE_LIMITED_OR_OUTAGE, "overloaded", "overloaded_error")


# ------------------------------------------------------------------ classify_failure(): usage-limit
# evidence from EARLIER this turn must survive terminal classification (fix round 1,
# connector 4177637201), but ONLY when the COMPLETE current stream also confirms it (fix
# round 2, connector 4177952848 / F7): a later genuine success on the same stream, or a
# stream that never reached a terminal result at all, vetoes it instead.


def _confirmed_usage_stream(window, *, reset_epoch=1788948000, result_is_error=True):
    """The SAME ``sig["usage_stream"]`` shape ``usage_park.note_stream_event`` folds and
    ``fact_from_stream`` reads - the stream-completeness proof F7 revalidates the retained
    evidence against. ``result_is_error``: ``True`` confirms the rejection (the provider's
    last word was an error), ``False`` is a later genuine success (vetoes it), ``None`` is
    an incomplete stream (no terminal result read at all - decides nothing either way)."""
    return {"rejected": {"window": window, "reset_epoch": reset_epoch}, "result_is_error": result_is_error}


def test_validated_usage_limit_evidence_survives_a_429_seen_later_the_same_turn():
    """The exact shape of the connector's repro: an earlier rate_limit_event proved a
    five-hour usage-limit rejection; a later terminal 429 on the SAME stream must not
    overwrite it with the weaker, unrelated "throttled" reason."""
    sig = {"structured_errors": [_structured(api_error_status=429, subtype="rate_limit_error")],
           "usage_stream": _confirmed_usage_stream("five_hour")}
    evidence = ("usage_limit_rejected", "rate_limit_event.rejected.five_hour")
    state, reason, detail = classify_failure(sig, CLASS_INFRA, evidence)
    assert (state, reason, detail) == (
        hm.STATE_RATE_LIMITED_OR_OUTAGE, "usage_limit_rejected", "rate_limit_event.rejected.five_hour")


def test_validated_usage_limit_evidence_survives_a_plain_stream_close_with_no_429():
    """The weekly-window repro: the stream simply ends with NO terminal 429/529 at all
    (the old code fell through to the generic "retryable_transport_error")."""
    sig = {"retryable": True, "usage_stream": _confirmed_usage_stream("seven_day")}
    evidence = ("usage_limit_rejected", "rate_limit_event.rejected.seven_day")
    state, reason, detail = classify_failure(sig, CLASS_INFRA, evidence)
    assert (state, reason, detail) == (
        hm.STATE_RATE_LIMITED_OR_OUTAGE, "usage_limit_rejected", "rate_limit_event.rejected.seven_day")


def test_a_later_genuine_success_vetoes_the_retained_evidence():
    """#305 F7 (fix round 2, connector 4177952848): the connector's exact repro shape -
    the provider's own LAST word on the stream was a genuine success (``result_is_error``
    exactly ``False``), so the retained rejection must not win even though it is still
    sitting on the writer from earlier in the same turn."""
    sig = {"rc": 1, "usage_stream": _confirmed_usage_stream("five_hour", result_is_error=False)}
    evidence = ("usage_limit_rejected", "rate_limit_event.rejected.five_hour")
    state, reason, _detail = classify_failure(sig, CLASS_INFRA, evidence)
    assert reason != "usage_limit_rejected"


def test_an_incomplete_stream_does_not_claim_the_limit_either():
    """No terminal result was ever read (``result_is_error`` stays unset/None) - neither
    confirmed nor vetoed, so this decides nothing: falls through exactly as if there were
    no retained evidence at all, same as the brief's explicit incomplete-stream case."""
    sig = {"retryable": True, "usage_stream": _confirmed_usage_stream("five_hour", result_is_error=None)}
    evidence = ("usage_limit_rejected", "rate_limit_event.rejected.five_hour")
    state, reason, detail = classify_failure(sig, CLASS_INFRA, evidence)
    assert (state, reason, detail) == classify_failure(sig, CLASS_INFRA, None)
    assert reason != "usage_limit_rejected"


def test_evidence_with_no_usage_stream_at_all_is_not_claimed():
    """A ``sig`` carrying no ``usage_stream`` key at all (e.g. a non-Claude CLI, or any
    caller that never folded one) never confirms the retained evidence - fails closed,
    never open."""
    sig = {"retryable": True}
    evidence = ("usage_limit_rejected", "rate_limit_event.rejected.five_hour")
    state, reason, detail = classify_failure(sig, CLASS_INFRA, evidence)
    assert reason != "usage_limit_rejected"


def test_a_fired_watchdog_still_wins_over_usage_limit_evidence():
    sig = {"watchdog": True}
    evidence = ("usage_limit_rejected", "rate_limit_event.rejected.five_hour")
    state, reason, _detail = classify_failure(sig, CLASS_INFRA, evidence)
    assert (state, reason) == (hm.STATE_STUCK_SUSPECTED, "turn_watchdog_fired")


def test_a_config_blocked_failure_still_wins_over_usage_limit_evidence():
    sig = {}
    evidence = ("usage_limit_rejected", "rate_limit_event.rejected.five_hour")
    state, reason, _detail = classify_failure(sig, CLASS_CONFIG_BLOCKED, evidence)
    assert (state, reason) == (hm.STATE_ERRORED_AMBIGUOUS, "config_blocked")


def test_with_no_usage_limit_evidence_classification_is_unchanged():
    sig = {"structured_errors": [_structured(api_error_status=429, subtype="rate_limit_error")]}
    assert classify_failure(sig, CLASS_INFRA, None) == classify_failure(sig, CLASS_INFRA)


def test_end_to_end_the_writer_keeps_the_rejected_event_through_failure(tmp_path):
    """The writer's own `_usage_limit_evidence`, set by `event()`, carried through to a
    later `failure()` call in the SAME turn - the exact sequence `run.py` drives - when the
    complete stream (`sig["usage_stream"]`) also confirms it."""
    store, writer = _writer(tmp_path)
    writer.turn_start({"id": "m1", "request_id": "r1"})
    writer.event(Event(EventType.ADAPTER_ERROR, text="rate_limit: rejected", retryable=True,
                       raw=_rate_limit_event_raw("rejected", window="five_hour")))
    assert store.read_health_raw("beta")["reason_code"] == "usage_limit_rejected"
    sig = {"structured_errors": [_structured(api_error_status=429, subtype="rate_limit_error")],
           "usage_stream": _confirmed_usage_stream("five_hour")}
    writer.failure(sig, CLASS_INFRA)
    snap = store.read_health_raw("beta")
    assert snap["reason_code"] == "usage_limit_rejected"
    assert snap["reason_detail"] == "rate_limit_event.rejected.five_hour"


def test_end_to_end_a_later_success_on_the_stream_vetoes_it_through_the_writer(tmp_path):
    """#305 F7, the writer-level mirror of the connector's real-drive repro: the SAME
    rejected event fires mid-turn, but the complete stream's own fold shows the provider's
    last word was success - the retained evidence must not win at `failure()`."""
    store, writer = _writer(tmp_path)
    writer.turn_start({"id": "m1", "request_id": "r1"})
    writer.event(Event(EventType.ADAPTER_ERROR, text="rate_limit: rejected", retryable=True,
                       raw=_rate_limit_event_raw("rejected", window="five_hour")))
    assert store.read_health_raw("beta")["reason_code"] == "usage_limit_rejected"
    sig = {"rc": 1, "usage_stream": _confirmed_usage_stream("five_hour", result_is_error=False)}
    writer.failure(sig, CLASS_INFRA)
    snap = store.read_health_raw("beta")
    assert snap["reason_code"] != "usage_limit_rejected"


def test_the_evidence_does_not_leak_into_the_next_turn(tmp_path):
    """Scoped to ONE turn: a fresh `turn_start` clears it, so an unrelated failure in the
    NEXT turn is classified on its own merits, never the previous turn's proof."""
    store, writer = _writer(tmp_path)
    writer.turn_start({"id": "m1", "request_id": "r1"})
    writer.event(Event(EventType.ADAPTER_ERROR, text="rate_limit: rejected", retryable=True,
                       raw=_rate_limit_event_raw("rejected", window="five_hour")))
    writer.failure({"structured_errors": [], "usage_stream": _confirmed_usage_stream("five_hour")}, CLASS_INFRA)
    assert store.read_health_raw("beta")["reason_code"] == "usage_limit_rejected"
    writer.turn_start({"id": "m2", "request_id": "r2"})
    writer.failure({"retryable": True}, CLASS_INFRA)
    snap = store.read_health_raw("beta")
    assert snap["reason_code"] == "retryable_transport_error"


# ------------------------------------------------------------------ event(): the phrase fallback
# only on a real WORD boundary (fix round 1, connector 4177637214).


def test_a_phrase_embedded_inside_a_larger_word_is_not_a_match(tmp_path):
    store, writer = _writer(tmp_path)
    for text in ("corporate limit exceeded", "quotable statement"):
        writer.event(Event(EventType.ADAPTER_ERROR, text=text, retryable=True, raw={}))
        snap = store.read_health_raw("beta")
        assert snap["reason_code"] == "adapter_retryable_error", text
        assert snap.get("reason_detail") is None, text


def test_the_genuine_phrases_still_match_beside_their_near_misses(tmp_path):
    store, writer = _writer(tmp_path)
    for text, label in (("Error: rate limit exceeded", "rate_limit"),
                        ("over quota for this month", "quota")):
        writer.event(Event(EventType.ADAPTER_ERROR, text=text, retryable=True, raw={}))
        snap = store.read_health_raw("beta")
        assert snap["reason_code"] == "adapter_rate_limit", text
        assert snap["reason_detail"] == f"text_match.{label}", text


# ------------------------------------------------------------------ the record, and clearing it


def test_the_record_carries_the_detail_and_no_private_text(tmp_path):
    store, writer = _writer(tmp_path)
    secret = "C:\\Users\\someone\\.ssh\\id_rsa and sk-ANTHROPIC1234567890ABCDEFGHIJKLMNOP"
    writer.event(Event(EventType.ADAPTER_ERROR, text=f"rate limit hit while reading {secret}",
                       retryable=True, raw={}))
    snap = store.read_health_raw("beta")
    assert snap["reason_code"] == "adapter_rate_limit"
    detail = snap.get("reason_detail")
    assert detail is not None
    assert "ssh" not in detail and "sk-ANTHROPIC" not in detail and "\\" not in detail
    assert detail == "text_match.rate_limit"


def test_the_label_clears_as_soon_as_the_next_turn_succeeds(tmp_path):
    store, writer = _writer(tmp_path)
    writer.event(Event(EventType.ADAPTER_ERROR, text="rate limit exceeded", retryable=True, raw={}))
    assert store.read_health_raw("beta")["reason_code"] == "adapter_rate_limit"
    writer.event(Event(EventType.TURN_STARTED))
    snap = store.read_health_raw("beta")
    assert snap["state"] == hm.STATE_WORKING_TURN
    assert snap["reason_code"] == "progress_event"
    assert snap.get("reason_detail") is None


def test_an_old_record_with_no_reason_detail_still_reads(tmp_path):
    store = _store(tmp_path)
    old = hm.build_snapshot(
        agent="beta", cli="claude", mode="wrapper-loop", state=hm.STATE_RATE_LIMITED_OR_OUTAGE,
        reason_code="adapter_rate_limit",
    )
    assert "reason_detail" not in old
    store.write_health("beta", old)
    view = store.read_health("beta", now_epoch=time.time())
    assert view["state"] == hm.STATE_RATE_LIMITED_OR_OUTAGE
    assert view["reason_code"] == "adapter_rate_limit"
    assert "reason_detail" not in view


# ------------------------------------------------------------------ plain-words status/supervisor flag


def test_the_status_flag_names_the_structured_reason_in_plain_words():
    assert cli._rate_limit_reason_flag(
        {"reason_code": "usage_limit_rejected", "reason_detail": "rate_limit_event.rejected.five_hour"}
    ) == "rate_limited(usage_limit window=five_hour)"
    assert cli._rate_limit_reason_flag({"reason_code": "throttled"}) == "rate_limited(throttled)"
    assert cli._rate_limit_reason_flag({"reason_code": "overloaded"}) == "rate_limited(overloaded)"


def test_the_status_flag_is_silent_for_legacy_and_unclassified_reasons():
    assert cli._rate_limit_reason_flag({"reason_code": "adapter_rate_limit"}) is None
    assert cli._rate_limit_reason_flag({"reason_code": "adapter_retryable_error"}) is None
    assert cli._rate_limit_reason_flag({"reason_code": "lock_contention"}) is None
    assert cli._rate_limit_reason_flag(None) is None
    assert cli._rate_limit_reason_flag({}) is None
