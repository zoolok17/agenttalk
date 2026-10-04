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
from agenttalk.wrapper.loop import CLASS_INFRA


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
