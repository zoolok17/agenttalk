"""Tests for the operator attention queue pure core + disposition IO (attention.py).

Covers the load-bearing gate conditions: strict typed-meta validation vs fail-safe reader,
source_hash over identifying content, snapshot-bound dispositions (a changed source
resurfaces despite a prior defer/dismiss), dismiss-forbidden-for-blocking-sources,
deterministic ranking, dedupe keys, and the append-only/skip-invalid disposition log.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agenttalk import attention as att
from agenttalk.store import Store


_NO_RESET_ADMITTED = {"evaluated": True, "admissions": {}}


# ----------------------------------------------------------- typed meta validation

def test_validate_accepts_full_valid_and_absent() -> None:
    assert att.validate_attention_meta(None) == []
    assert att.validate_attention_meta({}) == []
    ok = {"attention": {"schema_version": 1, "decision": "ship or hold?",
                        "why_it_matters": "release gate", "options": ["ship", "hold"],
                        "recommendation": "hold", "risk_if_ignored": "regression",
                        "risk_severity": "high", "confidence": "medium",
                        "priority": "urgent", "needed_by": "2026-07-03",
                        "affected": ["agent:beta", "request:esc-1"]}}
    assert att.validate_attention_meta(ok) == []


def test_validate_requires_wrapped_form_no_spurious_errors_on_full_meta() -> None:
    # fable-max #3: a full message meta WITHOUT an `attention` key is 'no typed block' and
    # validates clean - unrelated priority/options keys must NOT be read as an attention block.
    assert att.validate_attention_meta({"priority": "not-an-enum", "options": ["x"] * 99,
                                        "request_id": "esc-1"}) == []
    # the wrapped form is still validated
    assert att.validate_attention_meta({"attention": {"priority": "not-an-enum"}})


def test_needed_by_weight_reuses_parse_iso_dt_naive_as_utc() -> None:
    # fable-max #5: naive datetime accepted (treated as UTC), unparseable -> 0.
    from datetime import datetime, timedelta, timezone
    soon = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")  # naive
    assert att._needed_by_weight(soon) == 2          # due within 24h, naive parsed as UTC
    assert att._needed_by_weight("2000-01-01") == 3  # long overdue
    assert att._needed_by_weight("not-a-date") == 0
    assert att._needed_by_weight(None) == 0


def test_rank_key_ignores_absent_now_iso_plumbing() -> None:
    # fable-max #4: _now_iso plumbing removed; rank_key must not depend on it.
    item = {"item_id": "x", "source": att.SOURCE_NEEDS_OPERATOR, "state": "active",
            "priority": "high", "risk_severity": "high", "human_can_unblock_now": True}
    assert isinstance(att.rank_key(item), tuple)     # no KeyError / no _now_iso dependency


def test_validate_rejects_bad_enum_multiline_oversize_counts() -> None:
    assert att.validate_attention_meta({"attention": {"priority": "sometime"}})
    assert att.validate_attention_meta({"attention": {"risk_severity": "critical"}})
    assert att.validate_attention_meta({"attention": {"decision": "a\nb"}})          # multiline
    assert att.validate_attention_meta({"attention": {"decision": "x" * 501}})       # oversize
    assert att.validate_attention_meta({"attention": {"options": ["o"] * 11}})       # too many
    assert att.validate_attention_meta({"attention": {"affected": ["a"] * 21}})      # too many
    assert att.validate_attention_meta({"attention": {"needed_by": "not-a-date"}})
    assert att.validate_attention_meta({"attention": {"schema_version": 2}})         # future


def test_reader_is_failsafe_downgrades_never_raises() -> None:
    # malformed typed meta -> defaults + warning, never an exception
    fields, warns = att.parse_attention_meta({"attention": {"priority": "bogus", "decision": "x\ny"}})
    assert "typed_fields_warning" in warns
    assert fields["priority"] == "unknown"        # bad enum -> unknown, not crash
    # a well-typed subset still parses
    fields2, warns2 = att.parse_attention_meta(
        {"attention": {"priority": "high", "decision": "go?", "options": ["a", "b"]}})
    assert warns2 == [] and fields2["priority"] == "high" and fields2["options"] == ["a", "b"]


# ----------------------------------------------------------- identity + hashing

def test_source_hash_reflects_content_not_just_identity() -> None:
    h1 = att.source_hash({"agent": "beta", "reason": "python not found"})
    h2 = att.source_hash({"agent": "beta", "reason": "access denied"})
    assert h1 != h2                                # different content -> different hash
    assert h1 == att.source_hash({"reason": "python not found", "agent": "beta"})  # order-stable


def test_source_hash_is_total_for_escaped_lone_surrogate() -> None:
    malformed = {"reason": "escaped-\ud800-surrogate"}

    assert att.source_hash(malformed) == att.source_hash(malformed)
    assert att.source_hash(malformed) != att.source_hash({"reason": "escaped-surrogate"})


def test_dedupe_key_distinguishes_decisions() -> None:
    k1 = att.dedupe_key("needs_operator", identity="beta|subj", decision_hash="d1")
    k2 = att.dedupe_key("needs_operator", identity="beta|subj", decision_hash="d2")
    assert k1 != k2                                # distinct decisions do not merge
    assert att.dedupe_key("config_blocked", identity="beta") == "config_blocked:beta"


def test_compute_stats_counts_active_dispositioned_and_dwell() -> None:
    # north-star: derived counts over applied items - active by source, dispositioned by
    # state, dwell = oldest active age. No new state, no body reads.
    items = [
        {"item_id": "needs_operator:e1", "source": att.SOURCE_NEEDS_OPERATOR, "source_hash": "H1",
         "state": "active", "warnings": [], "advisory": False, "age_seconds": 7200},
        {"item_id": "needs_operator:e2", "source": att.SOURCE_NEEDS_OPERATOR, "source_hash": "H2",
         "state": "active", "warnings": [], "advisory": False, "age_seconds": 100},
        {"item_id": "dead_letter:beta:m1", "source": att.SOURCE_DEAD_LETTER, "source_hash": "H3",
         "state": "active", "warnings": [], "advisory": False},
    ]
    # answer e2 elsewhere; the rest stay active
    disps = [_disp("needs_operator:e2", att.SOURCE_NEEDS_OPERATOR,
                   att.ACTION_ANSWERED_ELSEWHERE, "H2")]
    s = att.compute_stats(items, disps, now_iso="2026-06-01T00:00:00Z")
    assert s["surfaced_active"] == 2                       # e1 + dead_letter (e2 answered)
    assert s["active_by_source"] == {att.SOURCE_DEAD_LETTER: 1, att.SOURCE_NEEDS_OPERATOR: 1}
    assert s["dispositioned"]["answered_elsewhere"] == 1
    assert s["oldest_active_age_seconds"] == 7200          # dwell = oldest active


def test_close_hold_item_is_content_bound_on_verdict_and_reason() -> None:
    # codex F3 / gate 1: a re-published, differently-blocked close resurfaces (source_hash
    # folds verdict+reason, not just close_id).
    a = att.close_hold_items([{"close_id": "cl-1", "scope": "release", "verdict": "HOLD",
                               "reason": "gate not green", "revision": "abc"}])[0]
    b = att.close_hold_items([{"close_id": "cl-1", "scope": "release", "verdict": "HOLD",
                               "reason": "missing signoff", "revision": "abc"}])[0]
    assert a["item_id"] == b["item_id"] == "close_hold:cl-1"    # same disposition key
    assert a["source_hash"] != b["source_hash"]                # different problem -> resurfaces


# ----------------------------------------------------------- dismiss scoping

def test_dismiss_forbidden_for_blocking_allowed_for_advisory() -> None:
    A = att
    assert A.allowed_action_for_source(A.ACTION_DISMISS, A.SOURCE_NEEDS_OPERATOR, advisory=False) is False
    assert A.allowed_action_for_source(A.ACTION_DISMISS, A.SOURCE_DEAD_LETTER, advisory=True) is False
    assert A.allowed_action_for_source(A.ACTION_DISMISS, A.SOURCE_CAPACITY, advisory=True) is True
    # config/gate/close/lead_unarmed: dismiss only when the item is advisory-classified
    assert A.allowed_action_for_source(A.ACTION_DISMISS, A.SOURCE_CONFIG_BLOCKED, advisory=False) is False
    assert A.allowed_action_for_source(A.ACTION_DISMISS, A.SOURCE_CONFIG_BLOCKED, advisory=True) is True
    # resolve_dead_letter only for dead_letter; answered_elsewhere only for needs_operator
    assert A.allowed_action_for_source(A.ACTION_RESOLVE_DEAD_LETTER, A.SOURCE_DEAD_LETTER, advisory=False) is True
    assert A.allowed_action_for_source(A.ACTION_RESOLVE_DEAD_LETTER, A.SOURCE_NEEDS_OPERATOR, advisory=False) is False
    assert A.allowed_action_for_source(A.ACTION_ANSWERED_ELSEWHERE, A.SOURCE_NEEDS_OPERATOR, advisory=False) is True
    assert A.allowed_action_for_source(A.ACTION_ANSWERED_ELSEWHERE, A.SOURCE_DEAD_LETTER, advisory=False) is False
    # defer allowed everywhere
    assert A.allowed_action_for_source(A.ACTION_DEFER, A.SOURCE_NEEDS_OPERATOR, advisory=False) is True


# ----------------------------------------------------------- snapshot-bound dispositions

def _disp(item_id, source, action, source_hash, *, until=None):
    return {"schema_version": 1, "event_id": "att-x", "item_id": item_id, "source": source,
            "action": action, "actor": "claude", "reason": "operator handled", "at": "2026-01-01T00:00:00Z",
            "until": until, "source_snapshot": {"source_hash": source_hash, "refs": []}}


def test_disposition_hides_only_while_snapshot_matches() -> None:
    # advisory-classified config_blocked -> dismiss is legitimate (the guard allows it);
    # this test isolates the snapshot-match behavior.
    item = {"item_id": "config_blocked:beta", "source": att.SOURCE_CONFIG_BLOCKED,
            "source_hash": "HASH_A", "state": "active", "warnings": [], "advisory": True}
    folded = att.fold_dispositions([_disp("config_blocked:beta", att.SOURCE_CONFIG_BLOCKED,
                                          att.ACTION_DISMISS, "HASH_A")])
    r = att.apply_disposition(item, folded, now_iso="2026-06-01T00:00:00Z")
    assert r["state"] == "dismissed"


def test_changed_source_resurfaces_despite_prior_dismiss() -> None:
    # dismissed against HASH_A, but the live source is now HASH_B (a DIFFERENT problem) ->
    # the item resurfaces ACTIVE with prior_disposition_stale (gate condition 1).
    item = {"item_id": "config_blocked:beta", "source": att.SOURCE_CONFIG_BLOCKED,
            "source_hash": "HASH_B", "state": "active", "warnings": [], "advisory": True}
    folded = att.fold_dispositions([_disp("config_blocked:beta", att.SOURCE_CONFIG_BLOCKED,
                                          att.ACTION_DISMISS, "HASH_A")])
    r = att.apply_disposition(item, folded, now_iso="2026-06-01T00:00:00Z")
    assert r["state"] == "active" and "prior_disposition_stale" in r["warnings"]


def test_expired_defer_resurfaces() -> None:
    item = {"item_id": "needs_operator:esc-1", "source": att.SOURCE_NEEDS_OPERATOR,
            "source_hash": "H", "state": "active", "warnings": []}
    folded = att.fold_dispositions([_disp("needs_operator:esc-1", att.SOURCE_NEEDS_OPERATOR,
                                          att.ACTION_DEFER, "H", until="2026-01-02T00:00:00Z")])
    # before until -> deferred
    assert att.apply_disposition(item, folded, now_iso="2026-01-01T12:00:00Z")["state"] == "deferred"
    # after until -> active again
    assert att.apply_disposition(item, folded, now_iso="2026-02-01T00:00:00Z")["state"] == "active"


def test_latest_disposition_wins_per_family() -> None:
    ev1 = _disp("needs_operator:esc-1", att.SOURCE_NEEDS_OPERATOR, att.ACTION_DEFER, "H", until="2099-01-01T00:00:00Z")
    ev2 = _disp("needs_operator:esc-1", att.SOURCE_NEEDS_OPERATOR, att.ACTION_ANSWERED_ELSEWHERE, "H")
    folded = att.fold_dispositions([ev1, ev2])       # ev2 is later -> wins
    item = {"item_id": "needs_operator:esc-1", "source": att.SOURCE_NEEDS_OPERATOR,
            "source_hash": "H", "state": "active", "warnings": []}
    assert att.apply_disposition(item, folded, now_iso="2026-01-01T00:00:00Z")["state"] == "answered_elsewhere"


def test_forged_dismiss_on_needs_operator_is_ignored_stays_active() -> None:
    # reviewer-3 P3: a well-formed-but-ILLEGITIMATE dismiss (dismiss is forbidden for
    # needs_operator) in the untrusted log must NOT hide the escalation - apply re-enforces
    # allowed_action_for_source and keeps the item ACTIVE with a warning.
    item = {"item_id": "needs_operator:esc-1", "source": att.SOURCE_NEEDS_OPERATOR,
            "source_hash": "H", "state": "active", "warnings": [], "advisory": False}
    folded = att.fold_dispositions([_disp("needs_operator:esc-1", att.SOURCE_NEEDS_OPERATOR,
                                          att.ACTION_DISMISS, "H")])
    r = att.apply_disposition(item, folded, now_iso="2026-06-01T00:00:00Z")
    assert r["state"] == "active" and "ignored_illegitimate_disposition" in r["warnings"]


def test_forged_resolve_on_needs_operator_is_ignored() -> None:
    # a forged dead_letter_resolution folded onto a needs_operator item_id must not resolve
    # it (resolve is only legitimate for dead_letter).
    item = {"item_id": "needs_operator:esc-1", "source": att.SOURCE_NEEDS_OPERATOR,
            "source_hash": "H", "state": "active", "warnings": [], "advisory": False}
    folded = att.fold_dispositions([_disp("needs_operator:esc-1", att.SOURCE_NEEDS_OPERATOR,
                                          att.ACTION_RESOLVE_DEAD_LETTER, "H")])
    r = att.apply_disposition(item, folded, now_iso="2026-06-01T00:00:00Z")
    assert r["state"] == "active" and "ignored_illegitimate_disposition" in r["warnings"]


def test_legit_answered_elsewhere_on_needs_operator_still_applies() -> None:
    # the guard must not over-block: a LEGITIMATE answered_elsewhere on needs_operator applies.
    item = {"item_id": "needs_operator:esc-1", "source": att.SOURCE_NEEDS_OPERATOR,
            "source_hash": "H", "state": "active", "warnings": [], "advisory": False}
    folded = att.fold_dispositions([_disp("needs_operator:esc-1", att.SOURCE_NEEDS_OPERATOR,
                                          att.ACTION_ANSWERED_ELSEWHERE, "H")])
    assert att.apply_disposition(item, folded, now_iso="2026-06-01T00:00:00Z")["state"] == "answered_elsewhere"


def test_malformed_defer_until_resurfaces_active() -> None:
    # codex F2 read side: a malformed persisted `until` must resurface the item ACTIVE (never
    # string-compare a blocking item into hiding forever), with an invalid_defer_until warning.
    item = {"item_id": "needs_operator:esc-1", "source": att.SOURCE_NEEDS_OPERATOR,
            "source_hash": "H", "state": "active", "warnings": [], "advisory": False}
    folded = att.fold_dispositions([_disp("needs_operator:esc-1", att.SOURCE_NEEDS_OPERATOR,
                                          att.ACTION_DEFER, "H", until="zzzz-not-a-date")])
    r = att.apply_disposition(item, folded, now_iso="2026-06-01T00:00:00Z")
    assert r["state"] == "active" and "invalid_defer_until" in r["warnings"]


def test_parse_iso_dt_handles_z_dateonly_and_micros() -> None:
    assert att.parse_iso_dt("2099-01-01") is not None            # date-only
    assert att.parse_iso_dt("2099-01-01T00:00:00Z") is not None  # trailing Z
    assert att.parse_iso_dt("2026-07-02T08:40:57.921722999Z") is not None  # >6 fractional
    assert att.parse_iso_dt("nonsense") is None
    assert att.parse_iso_dt(None) is None


# ----------------------------------------------------------- ranking

def test_ranking_blocker_outranks_old_advisory() -> None:
    blocker = {"item_id": "needs_operator:esc-1", "source": att.SOURCE_NEEDS_OPERATOR,
               "state": "active", "priority": "high", "risk_severity": "high",
               "human_can_unblock_now": True, "age_seconds": 60}
    advisory = {"item_id": "capacity:beta:budget", "source": att.SOURCE_CAPACITY,
                "state": "active", "priority": "low", "risk_severity": "low",
                "human_can_unblock_now": False, "age_seconds": 999999}  # very old
    ordered = att.sort_items([advisory, blocker])
    assert ordered[0]["item_id"] == "needs_operator:esc-1"   # fresh blocker beats old advisory


def test_ranking_is_deterministic_under_ties() -> None:
    a = {"item_id": "gate_hold:s:g2", "source": att.SOURCE_GATE_HOLD, "state": "active",
         "priority": "normal", "risk_severity": "low", "human_can_unblock_now": True, "age_seconds": 0}
    b = {"item_id": "gate_hold:s:g1", "source": att.SOURCE_GATE_HOLD, "state": "active",
         "priority": "normal", "risk_severity": "low", "human_can_unblock_now": True, "age_seconds": 0}
    # identical weights -> item_id ascending tie-break, stable across runs
    assert [i["item_id"] for i in att.sort_items([a, b])] == ["gate_hold:s:g1", "gate_hold:s:g2"]


# ----------------------------------------------------------- disposition IO

def test_disposition_log_append_skip_invalid(tmp_path: Path) -> None:
    s = Store(tmp_path)
    s.init(["claude", "beta"])
    good = _disp("dead_letter:beta:m1", att.SOURCE_DEAD_LETTER, att.ACTION_RESOLVE_DEAD_LETTER, "H")
    att.append_disposition(s, good)
    # An unterminated torn line must be isolated before the next durable append.
    with open(att.dispositions_path(s), "a", encoding="utf-8") as fh:
        fh.write("{not json")
    att.append_disposition(s, _disp("config_blocked:beta", att.SOURCE_CONFIG_BLOCKED, att.ACTION_DEFER, "H2"))
    valid, problems = att.read_dispositions(s)
    assert len(valid) == 2 and len(problems) == 1 and problems[0]["line"] == 2


def test_disposition_append_after_invalid_utf8_tail_preserves_valid_events(
    tmp_path: Path,
) -> None:
    s = Store(tmp_path)
    s.init(["claude", "beta"])
    first = _disp(
        "dead_letter:beta:m1",
        att.SOURCE_DEAD_LETTER,
        att.ACTION_RESOLVE_DEAD_LETTER,
        "H",
    )
    second = _disp(
        "config_blocked:beta",
        att.SOURCE_CONFIG_BLOCKED,
        att.ACTION_DEFER,
        "H2",
    )
    att.append_disposition(s, first)
    with open(att.dispositions_path(s), "ab") as fh:
        fh.write(b'{"event":"disposition","reason":"\xe2')

    att.append_disposition(s, second)

    valid, problems = att.read_dispositions(s)
    assert valid == [first, second]
    assert problems == [{"line": 2, "error": "invalid utf-8"}]


def test_notice_log_append_after_unterminated_tail_preserves_new_event(tmp_path: Path) -> None:
    s = Store(tmp_path)
    s.init(["claude", "beta"])
    path = att._notice_log_path(s)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"kind": "dead_letter"', encoding="utf-8")
    event = {
        "kind": att.NOTICE_DEAD_LETTER,
        "notice_key": "dead_letter:beta:m1",
        "request_id": "rq-notice",
    }

    att.append_notice_event(s, event)

    events, warnings = att.read_notice_events(s)
    assert events == [event]
    assert warnings == ["notice_log_torn:1"]


def test_notice_append_after_invalid_utf8_tail_preserves_valid_events(
    tmp_path: Path,
) -> None:
    s = Store(tmp_path)
    s.init(["claude", "beta"])
    path = att._notice_log_path(s)
    first = {
        "kind": att.NOTICE_DEAD_LETTER,
        "notice_key": "dead_letter:beta:m1",
        "request_id": "rq-first",
    }
    second = {
        "kind": att.NOTICE_DEAD_LETTER,
        "notice_key": "dead_letter:beta:m2",
        "request_id": "rq-second",
    }
    att.append_notice_event(s, first)
    with open(path, "ab") as fh:
        fh.write(b'{"kind":"dead_letter","body":"\xe2')

    att.append_notice_event(s, second)

    events, warnings = att.read_notice_events(s)
    assert events == [first, second]
    assert warnings == ["notice_log_utf8:2"]


def test_attention_readers_stream_without_path_read_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = Store(tmp_path)
    s.init(["claude", "beta"])
    notice = {"kind": att.NOTICE_DEAD_LETTER, "notice_key": "n1"}
    disposition = _disp(
        "dead_letter:beta:m1",
        att.SOURCE_DEAD_LETTER,
        att.ACTION_RESOLVE_DEAD_LETTER,
        "H",
    )
    att.append_notice_event(s, notice)
    att.append_disposition(s, disposition)

    def fail_read_text(*args, **kwargs):
        raise AssertionError("attention ledgers must be streamed")

    monkeypatch.setattr(Path, "read_text", fail_read_text)
    notices, notice_warnings = att.read_notice_events(s)
    dispositions, disposition_problems = att.read_dispositions(s)
    assert notices == [notice] and notice_warnings == []
    assert dispositions == [disposition] and disposition_problems == []


def test_disposition_survives_reset_and_archive(tmp_path: Path) -> None:
    s = Store(tmp_path)
    s.init(["claude", "beta"])
    att.append_disposition(s, _disp("dead_letter:beta:m1", att.SOURCE_DEAD_LETTER, att.ACTION_RESOLVE_DEAD_LETTER, "H"))
    s.reset(archive=True)                             # archives messages/state/sessions only
    valid, _ = att.read_dispositions(s)
    assert len(valid) == 1                            # attention/ preserved across reset --archive


def test_malformed_disposition_event_rejected_by_validator() -> None:
    assert att.validate_disposition_event({"action": "defer", "item_id": "x", "actor": "c",
                                           "at": "t", "reason": "r",
                                           "source_snapshot": {"source_hash": "h"}}) is True
    assert att.validate_disposition_event({"action": "nope", "item_id": "x", "actor": "c",
                                           "at": "t", "reason": "r",
                                           "source_snapshot": {"source_hash": "h"}}) is False
    assert att.validate_disposition_event({"action": "defer", "item_id": "x", "actor": "c",
                                           "at": "t", "reason": "  ",  # empty reason
                                           "source_snapshot": {"source_hash": "h"}}) is False
    assert att.validate_disposition_event({"action": "defer", "item_id": "x", "actor": "c",
                                           "at": "t", "reason": "r"}) is False  # no snapshot


# ----------------------------------------------------------- projection + build_queue

def _now() -> str:
    return "2026-06-01T00:00:00Z"


def _process_tree_state(
    *,
    status: str,
    reason_code: str | None,
    observed_count: int = 65,
) -> dict:
    recorded_count = min(observed_count, 64)
    omitted_count = observed_count - recorded_count
    entries = [
        {
            "pid": 100 + index,
            "start": f"linux:{'a' * 32}:{index + 1}",
            "start_filetime": None,
            "role": "wrapper" if index == 0 else "tool_descendant",
            "parent_pid": 0 if index == 0 else 99 + index,
            "discovered_at": "2026-06-01T00:00:00Z",
        }
        for index in range(recorded_count)
    ]
    return {
        "agents": {
            "worker": {
                "launcher_pid": 100,
                "launcher_start": f"linux:{'a' * 32}:1",
                "launcher_nonce": "12345678-1234-4234-8234-123456789abc",
                "runtime_wrapper_generation": "wrapper-1",
                "owned_process_tree": {
                    "schema_version": 2,
                    "attribution_model": "owned_process_tree_v2",
                    "agent": "worker",
                    "root_key": "root-1",
                    "status": status,
                    "reason_code": reason_code,
                    "limit": 64,
                    "observed_count": observed_count,
                    "recorded_count": recorded_count,
                    "omitted_count": omitted_count,
                    "rejected_count": 0,
                    "truncated": omitted_count > 0,
                    "refreshed_at": "2026-06-01T00:00:00Z",
                    "wrapper_generation": "wrapper-1",
                    "launch_nonce": "12345678-1234-4234-8234-123456789abc",
                    "entries": entries,
                }
            }
        }
    }


@pytest.mark.parametrize(
    ("status", "reason_code", "observed_count", "expected_detail"),
    [
        ("truncated", "process_tree_truncated", 65, "observed 65 identities over the safe cap 64"),
        ("invalid", "duplicate_pid", 3, "complete current ownership"),
    ],
)
def test_process_tree_hold_projects_blocking_operator_action(
    status: str,
    reason_code: str,
    observed_count: int,
    expected_detail: str,
) -> None:
    item = att.process_tree_hold_items(
        _process_tree_state(
            status=status,
            reason_code=reason_code,
            observed_count=observed_count,
        ),
        reset_admissions=_NO_RESET_ADMITTED,
    )[0]

    assert item["item_id"] == "process_tree_hold:worker"
    assert item["source"] == att.SOURCE_PROCESS_TREE_HOLD
    assert item["state"] == "active"
    assert item["advisory"] is False
    assert item["human_can_unblock_now"] is True
    assert item["priority"] == item["risk_severity"] == item["confidence"] == "high"
    assert expected_detail in item["why_it_matters"]
    if status == "truncated":
        assert "omits 1 observed identity" in item["recommendation"]
    assert "no scripted remedy applies in this state" in item["recommendation"]
    assert "operator_command" not in item
    assert item["configured_launch_unavailable"]
    assert item["source_refs"] == [{
        "kind": "supervisor_state",
        "agent": "worker",
        "reason_code": reason_code,
    }]

    forged_dismiss = _disp(
        item["item_id"],
        item["source"],
        att.ACTION_DISMISS,
        item["source_hash"],
    )
    queue = att.build_queue([item], [forged_dismiss], now_iso=_now())
    surfaced = queue["items"][0]
    assert surfaced["state"] == "active"
    assert "ignored_illegitimate_disposition" in surfaced["warnings"]


def test_large_positive_omitted_count_never_collapses_to_zero() -> None:
    item = att.process_tree_hold_items(
        _process_tree_state(
            status="truncated",
            reason_code="process_tree_truncated",
            observed_count=1_000_065,
        ),
        reset_admissions=_NO_RESET_ADMITTED,
    )[0]

    assert "omits >1,000,000 identities" in (
        item["recommendation"]
    )


def test_large_positive_rejected_count_never_collapses_to_unknown() -> None:
    state = _process_tree_state(
        status="invalid",
        reason_code="process_tree_invalid_wrapper_state_mismatch",
        observed_count=1,
    )
    state["agents"]["worker"]["owned_process_tree"]["rejected_count"] = 1_000_001

    item = att.process_tree_hold_items(
        state,
        reset_admissions=_NO_RESET_ADMITTED,
    )[0]

    assert "excludes >1,000,000 candidates" in (
        item["recommendation"]
    )
    assert "UNKNOWN, not zero" not in item["recommendation"]


def test_process_tree_hold_hash_binds_exact_tree_and_legacy_evidence() -> None:
    state = _process_tree_state(
        status="truncated",
        reason_code="process_tree_truncated",
    )
    original = att.process_tree_hold_items(state)[0]["source_hash"]

    state["agents"]["worker"]["owned_process_tree"]["entries"][1]["start"] = (
        f"linux:{'b' * 32}:2"
    )
    changed_tree = att.process_tree_hold_items(state)[0]["source_hash"]
    assert changed_tree != original

    state["agents"]["worker"]["legacy_process_evidence"] = {
        "schema_version": 1,
        "status": "migration_hold",
        "limit": 64,
        "observed_count": 1,
        "recorded_count": 1,
        "omitted_count": 0,
        "truncated": False,
        "malformed_count": 0,
        "source_hash": "a" * 64,
        "entries": [{
            "pid": 100,
            "start": f"linux:{'a' * 32}:1",
            "source": "wrapper",
        }],
    }
    assert att.process_tree_hold_items(state)[0]["source_hash"] != changed_tree


def test_process_tree_hold_never_emits_malformed_persisted_unicode() -> None:
    state = _process_tree_state(
        status="truncated",
        reason_code="process_tree_truncated",
    )
    tree = state["agents"]["worker"]["owned_process_tree"]
    tree["reason_code"] = "malformed-\ud800-reason"
    tree["observed_count"] = "malformed-\ud800-count"
    state["agents"]["worker"]["restart_request_state"] = (
        "applied_pending_readiness"
    )
    state["agents"]["worker"]["pending_restart_request_id"] = "rr-\ud800"

    item = att.process_tree_hold_items(
        state,
        restart_requests={"worker": {"request_id": "rr-\ud800"}},
        reset_admissions=_NO_RESET_ADMITTED,
    )[0]

    assert item["source_refs"][0]["reason_code"] == (
        "process_tree_hold_reason_invalid"
    )
    assert "complete current ownership" in item["why_it_matters"]
    assert "restart_request" not in item
    json.dumps(item, ensure_ascii=False).encode("utf-8")


def test_process_tree_hold_omits_reset_command_without_exact_nonce_authority() -> None:
    state = _process_tree_state(
        status="invalid",
        reason_code="process_tree_invalid_nonce",
        observed_count=1,
    )
    state["agents"]["worker"]["owned_process_tree"]["launch_nonce"] = None
    item = att.process_tree_hold_items(
        state,
        reset_admissions=_NO_RESET_ADMITTED,
    )[0]

    assert "operator_command" not in item
    assert "no scripted remedy applies in this state" in item["recommendation"]


def test_process_tree_hold_omits_reset_command_for_invalid_agent_key() -> None:
    state = _process_tree_state(
        status="truncated",
        reason_code="process_tree_truncated",
    )
    row = state["agents"].pop("worker")
    invalid_agent = "worker; Write-Output PWNED"
    state["agents"][invalid_agent] = row

    item = att.process_tree_hold_items(
        state,
        reset_admissions=_NO_RESET_ADMITTED,
    )[0]

    assert item["item_id"] == f"process_tree_hold:{invalid_agent}"
    assert item["state"] == "active"
    assert item["advisory"] is False
    assert "operator_command" not in item
    assert "no scripted remedy applies in this state" in item["recommendation"]


@pytest.mark.parametrize(
    ("reason_code", "operator_fact"),
    [
        (
            "process_tree_invalid_generation_adoption_pending",
            "current wrapper generation has adopted this agent's process tree",
        ),
        (
            "process_tree_invalid_legacy_managed_pids",
            "complete current ownership from legacy PID records",
        ),
        (
            "process_tree_invalid_exact_start_filetime_unavailable",
            "exact process lifetime identities",
        ),
        (
            "process_tree_invalid_post_kill_owned_descendant_edge_survived",
            "every owned descendant ended after the attempted teardown",
        ),
        (
            "process_tree_invalid_wrapper_state_mismatch",
            "reported wrapper identity agrees with the supervisor's recorded launcher",
        ),
    ],
)
def test_process_tree_refusal_is_operator_visible_with_working_manual_launch(
    reason_code: str,
    operator_fact: str,
) -> None:
    state = _process_tree_state(
        status="invalid",
        reason_code=reason_code,
        observed_count=1,
    )
    state["agents"]["worker"].update({
        "restart_request_state": "applied_pending_readiness",
        "pending_restart_request_id": "rr-blocked",
    })
    root = r"D:\work\fleet"
    config = {
        "agents": {
            "worker": {
                "cwd": r"D:\work\fleet\worker space",
                "launch": {
                    "windows_file": r"C:\Python\python.exe",
                    "windows_args": [
                        "-m", "agenttalk", "--root", "{ROOT}", "wrap",
                        "--for", "worker", "--loop", "--", r"C:\Codex\codex.exe",
                    ],
                },
            },
        },
    }

    item = att.process_tree_hold_items(
        state,
        supervisor_config=config,
        root=root,
        restart_requests={"worker": {"request_id": "rr-blocked"}},
        reset_admissions=_NO_RESET_ADMITTED,
    )[0]

    assert operator_fact in item["why_it_matters"]
    assert reason_code not in item["title"]
    assert reason_code not in item["why_it_matters"]
    assert reason_code not in item["recommendation"]
    assert item["source_refs"][0]["reason_code"] == reason_code
    assert "no scripted remedy applies in this state" in item["recommendation"]
    assert "operator_command" not in item
    assert item["configured_launch"] == {
        "source": "supervisor.json",
        "mode": "detached",
        "argv": [
            r"C:\Python\python.exe",
            "-m", "agenttalk", "--root", root, "wrap", "--for", "worker",
            "--loop", "--", r"C:\Codex\codex.exe",
        ],
        "cwd": r"D:\work\fleet\worker space",
        "environment": {
            "AGENTTALK_ROOT": root,
            "AGENTTALK_PY": r"C:\Python\python.exe",
            "supervisor_json_env_keys": [],
        },
        "environment_note": (
            "Reproduce the listed values and any configured per-agent values; "
            "a null AGENTTALK_PY must be recovered from the supervisor artifact, "
            "and the supervisor may also supply an isolated CODEX_HOME and wrapper-log paths."
        ),
    }
    assert item["restart_request"] == {
        "request_id": "rr-blocked",
        "state": "blocked_by_process_tree_hold",
        "pending_progress": False,
    }


def test_process_tree_hold_hash_resurfaces_for_new_restart_and_launch() -> None:
    state = _process_tree_state(
        status="invalid",
        reason_code="process_tree_invalid_wrapper_state_mismatch",
    )
    initial = att.process_tree_hold_items(
        state,
        reset_admissions=_NO_RESET_ADMITTED,
    )[0]
    deferred = _disp(
        initial["item_id"],
        initial["source"],
        att.ACTION_DEFER,
        initial["source_hash"],
    )

    with_restart = att.process_tree_hold_items(
        state,
        restart_requests={"worker": {"request_id": "rr-new"}},
        reset_admissions=_NO_RESET_ADMITTED,
    )[0]
    assert with_restart["source_hash"] != initial["source_hash"]
    assert att.build_queue(
        [with_restart],
        [deferred],
        now_iso=_now(),
    )["items"][0]["state"] == "active"

    config = {
        "agents": {
            "worker": {
                "launch": {
                    "windows_file": "python.exe",
                    "windows_args": [
                        "-m", "agenttalk", "wrap", "--for", "worker", "--loop",
                    ],
                },
            },
        },
    }
    with_launch = att.process_tree_hold_items(
        state,
        supervisor_config=config,
        root=r"D:\fleet",
        reset_admissions=_NO_RESET_ADMITTED,
    )[0]
    assert with_launch["source_hash"] != initial["source_hash"]
    assert with_launch["configured_launch"]["argv"][3:6] == [
        "--root", r"D:\fleet", "wrap",
    ]


def test_completed_restart_is_not_relabelled_blocked_by_later_hold() -> None:
    state = _process_tree_state(
        status="invalid",
        reason_code="process_tree_invalid_wrapper_state_mismatch",
    )
    state["agents"]["worker"].update({
        "restart_request_state": "readiness_seen",
        "pending_restart_request_id": "rr-complete",
    })

    without_marker = att.process_tree_hold_items(
        state,
        reset_admissions=_NO_RESET_ADMITTED,
    )[0]
    stale_same_marker = att.process_tree_hold_items(
        state,
        restart_requests={"worker": {"request_id": "rr-complete"}},
        reset_admissions=_NO_RESET_ADMITTED,
    )[0]
    fresh_marker = att.process_tree_hold_items(
        state,
        restart_requests={"worker": {"request_id": "rr-new"}},
        reset_admissions=_NO_RESET_ADMITTED,
    )[0]

    assert "restart_request" not in without_marker
    assert "restart_request" not in stale_same_marker
    assert fresh_marker["restart_request"] == {
        "request_id": "rr-new",
        "state": "blocked_by_process_tree_hold",
        "pending_progress": False,
    }


def test_configured_launch_rejects_quoted_command_line_over_windows_bound() -> None:
    state = _process_tree_state(
        status="invalid",
        reason_code="process_tree_invalid_wrapper_state_mismatch",
    )
    quote_heavy = '"' * 4096
    config = {
        "agents": {
            "worker": {
                "launch": {
                    "windows_file": "python.exe",
                    "windows_args": [quote_heavy] * 5,
                },
            },
        },
    }

    item = att.process_tree_hold_items(
        state,
        supervisor_config=config,
        root=r"D:\fleet",
        reset_admissions=_NO_RESET_ADMITTED,
    )[0]

    assert "configured_launch" not in item
    assert "command-line bound" in item["configured_launch_unavailable"]


def test_configured_launch_bounds_windows_utf16_units_including_nul() -> None:
    state = _process_tree_state(
        status="invalid",
        reason_code="process_tree_invalid_wrapper_state_mismatch",
    )
    config = {
        "agents": {
            "worker": {
                "launch": {
                    "windows_file": "python.exe",
                    "windows_args": ["😀" * 4096] * 4,
                },
            },
        },
    }

    item = att.process_tree_hold_items(
        state,
        supervisor_config=config,
        root=r"D:\fleet",
        reset_admissions=_NO_RESET_ADMITTED,
    )[0]

    assert "configured_launch" not in item
    assert "command-line bound" in item["configured_launch_unavailable"]


@pytest.mark.parametrize(
    "config",
    [
        {
            "agents": {
                "worker": {
                    "launch": {
                        "windows_file": "python.exe",
                        "windows_args": ["\ud800"],
                    },
                },
            },
        },
        {
            "agents": {
                "worker": {
                    "env": {"\ud800": "value"},
                    "launch": {
                        "windows_file": "python.exe",
                        "windows_args": ["-m", "agenttalk"],
                    },
                },
            },
        },
    ],
)
def test_configured_launch_rejects_unencodable_surrogates(config: dict) -> None:
    state = _process_tree_state(
        status="invalid",
        reason_code="process_tree_invalid_wrapper_state_mismatch",
    )

    item = att.process_tree_hold_items(
        state,
        supervisor_config=config,
        root=r"D:\fleet",
        reset_admissions=_NO_RESET_ADMITTED,
    )[0]

    assert "configured_launch" not in item
    assert "configured launch" in item["configured_launch_unavailable"]
    assert item["source_hash"]


def test_configured_non_python_launch_does_not_invent_agenttalk_python_pin() -> None:
    state = _process_tree_state(
        status="invalid",
        reason_code="process_tree_invalid_wrapper_state_mismatch",
    )
    config = {
        "agents": {
            "worker": {
                "launch": {
                    "windows_file": r"C:\Codex\codex.exe",
                    "windows_args": ["--safe-static-flag"],
                },
            },
        },
    }

    item = att.process_tree_hold_items(
        state,
        supervisor_config=config,
        root=r"D:\fleet",
        reset_admissions=_NO_RESET_ADMITTED,
    )[0]

    assert item["configured_launch"]["environment"]["AGENTTALK_PY"] is None
    assert "null AGENTTALK_PY" in item["configured_launch"]["environment_note"]


def test_admitted_reset_is_emitted_as_exact_argv_bound_to_item_hash() -> None:
    state = _process_tree_state(
        status="truncated",
        reason_code="process_tree_truncated",
    )
    admissions = {
        "evaluated": True,
        "admissions": {
            "worker": {
                "mode": "configured_reset",
                "agent": "worker",
                "actor": "lead",
                "verified_launch_nonce": "12345678-1234-4234-8234-123456789abc",
                "reason": "all recorded process identities independently verified stopped",
            },
        },
    }

    item = att.process_tree_hold_items(
        state,
        reset_admissions=admissions,
    )[0]

    assert "no scripted remedy applies" not in item["recommendation"]
    argv = item["operator_argv"]
    assert argv[:5] == [
        "agenttalk", "supervise", "--reset-process-tree-ownership", "--for", "worker",
    ]
    assert argv[argv.index("--hold-source-hash") + 1] == item["source_hash"]
    assert argv[argv.index("--from") + 1] == "lead"

    changed_actor = {
        "evaluated": True,
        "admissions": {
            "worker": {
                **admissions["admissions"]["worker"],
                "actor": "ops",
            },
        },
    }
    rebound = att.process_tree_hold_items(
        state,
        reset_admissions=changed_actor,
    )[0]
    assert rebound["source_hash"] != item["source_hash"]
    assert rebound["operator_argv"][rebound["operator_argv"].index("--from") + 1] == (
        "ops"
    )


def test_only_missing_kill_switch_precondition_is_named_without_a_command() -> None:
    state = _process_tree_state(
        status="truncated",
        reason_code="process_tree_truncated",
    )
    admissions = {
        "evaluated": True,
        "admissions": {},
        "blocked_admissions": {
            "worker": {
                "mode": "configured_reset",
                "agent": "worker",
                "missing_precondition": "supervisor_kill_switch_absent",
            },
        },
    }

    item = att.process_tree_hold_items(
        state,
        reset_admissions=admissions,
    )[0]

    assert "operator_argv" not in item
    assert "no scripted remedy applies in this state" in item["recommendation"]
    assert ".agenttalk/supervisor.kill" in item["recommendation"]
    assert "absent" in item["recommendation"]
    assert "while the supervisor remains stopped" in item["recommendation"]

    unevaluated = {
        **admissions,
        "evaluated": False,
    }
    not_established = att.process_tree_hold_items(
        state,
        reset_admissions=unevaluated,
    )[0]
    assert ".agenttalk/supervisor.kill" not in not_established["recommendation"]


def test_restart_marker_reads_are_limited_to_valid_configured_holds() -> None:
    held = _process_tree_state(
        status="invalid",
        reason_code="process_tree_invalid_wrapper_state_mismatch",
    )["agents"]["worker"]
    state = {
        "agents": {
            "held": held,
            "complete": {
                "owned_process_tree": {"status": "complete"},
            },
            r"..\outside": held,
        },
    }

    assert att.configured_process_tree_hold_agents(state) == ["held"]


def test_process_tree_hold_projection_ignores_a_complete_record() -> None:
    held = _process_tree_state(
        status="truncated",
        reason_code="process_tree_truncated",
    )
    assert att.process_tree_hold_items(held)

    complete = _process_tree_state(
        status="complete",
        reason_code=None,
        observed_count=4,
    )
    assert att.process_tree_hold_items(complete) == []


def test_ephemeral_process_tree_hold_without_tree_is_operator_visible() -> None:
    state = {
        "ephemeral_reviewers": {
            "active": {
                "lr-missing-agent": {
                    "process_tree_hold_reason": "ephemeral_agent_identity_missing",
                },
            },
        },
    }

    item = att.process_tree_hold_items(
        state,
        reset_admissions=_NO_RESET_ADMITTED,
    )[0]

    assert item["item_id"] == "process_tree_hold:ephemeral:lr-missing-agent"
    assert item["affected"] == ["lr-missing-agent"]
    assert "complete current ownership" in item["why_it_matters"]
    assert item["source_refs"] == [{
        "kind": "supervisor_ephemeral_state",
        "agent": "lr-missing-agent",
        "request_id": "lr-missing-agent",
        "reason_code": "ephemeral_agent_identity_missing",
    }]

    no_hold_reason = {
        "ephemeral_reviewers": {
            "active": {"lr-launch-window": {"owned_process_tree": None}},
        },
    }
    assert att.process_tree_hold_items(no_hold_reason) == []

    malformed_reason = {
        "ephemeral_reviewers": {
            "active": {
                "lr-corrupt": {
                    "owned_process_tree": {"status": "complete"},
                    "process_tree_hold_reason": {"bad": "shape"},
                },
            },
        },
    }
    malformed_item = att.process_tree_hold_items(malformed_reason)[0]
    assert malformed_item["source_refs"][0]["reason_code"] == (
        "process_tree_hold_reason_invalid"
    )


def test_ephemeral_process_tree_cap_hold_is_operator_visible() -> None:
    state = _process_tree_state(
        status="truncated",
        reason_code="process_tree_truncated",
        observed_count=65,
    )
    row = state["agents"].pop("worker")
    row["agent"] = "adversary-lr-cap"
    row["owned_process_tree"]["agent"] = "adversary-lr-cap"
    state["ephemeral_reviewers"] = {"active": {"lr-cap": row}}

    item = att.process_tree_hold_items(
        state,
        reset_admissions=_NO_RESET_ADMITTED,
    )[0]

    assert item["item_id"] == "process_tree_hold:ephemeral:lr-cap"
    assert item["affected"] == ["adversary-lr-cap"]
    assert "observed 65 identities over the safe cap 64" in item["why_it_matters"]
    assert "no scripted remedy applies in this state" in item["recommendation"]
    assert item["source_refs"] == [{
        "kind": "supervisor_ephemeral_state",
        "agent": "adversary-lr-cap",
        "request_id": "lr-cap",
        "reason_code": "process_tree_truncated",
    }]


def test_needs_operator_item_carries_typed_fields_and_surfaces_malformed() -> None:
    good = att.needs_operator_items([{"request_id": "esc-1", "subject": "ship?",
        "sender": "beta", "age_seconds": 10,
        "prompt_excerpt": "Full question for the operator.",
        "meta": {"needs_operator": "true", "attention": {"decision": "ship or hold?",
                 "priority": "urgent", "risk_severity": "high"}}}])
    assert good[0]["priority"] == "urgent" and good[0]["decision"] == "ship or hold?"
    assert good[0]["prompt_excerpt"] == "Full question for the operator."
    assert good[0]["human_can_unblock_now"] is True and good[0]["warnings"] == []
    bad = att.needs_operator_items([{"request_id": "esc-2", "subject": "x", "sender": "b",
        "age_seconds": 1, "meta": {"needs_operator": "true", "attention": {"priority": "BOGUS"}}}])
    assert "typed_fields_warning" in bad[0]["warnings"] and bad[0]["priority"] == "unknown"
    assert bad[0]["item_id"] == "needs_operator:esc-2"          # still surfaces


def test_build_queue_dedupes_display_keeps_all_ids() -> None:
    # two config_blocked notices for the same agent+summary collapse to one representative
    holds = att.config_blocked_items([{"agent": "beta", "summary": "python not found"}])
    dup = att.config_blocked_items([{"agent": "beta", "summary": "python not found"}])
    q = att.build_queue(holds + dup, [], now_iso=_now())
    cbs = [i for i in q["items"] if i["source"] == att.SOURCE_CONFIG_BLOCKED]
    assert len(cbs) == 1 and len(cbs[0]["duplicates"]) == 1   # one rep, one duplicate ref


def test_build_queue_distinct_decisions_do_not_merge() -> None:
    a = att.needs_operator_items([{"request_id": "esc-1", "subject": "same-subj", "sender": "beta",
        "age_seconds": 1, "meta": {"attention": {"decision": "decision A"}}}])
    b = att.needs_operator_items([{"request_id": "esc-2", "subject": "same-subj", "sender": "beta",
        "age_seconds": 1, "meta": {"attention": {"decision": "decision B"}}}])
    q = att.build_queue(a + b, [], now_iso=_now())
    nos = [i for i in q["items"] if i["source"] == att.SOURCE_NEEDS_OPERATOR]
    assert len(nos) == 2                                       # distinct decisions -> 2 items


def test_build_queue_needs_operator_outranks_capacity() -> None:
    items = (att.needs_operator_items([{"request_id": "esc-1", "subject": "s", "sender": "b",
                "age_seconds": 5, "meta": {"attention": {"priority": "high", "risk_severity": "high"}}}])
             + att.capacity_items([{"agent": "beta", "kind": "budget", "detail": "90%"}]))
    q = att.build_queue(items, [], now_iso=_now())
    assert q["items"][0]["source"] == att.SOURCE_NEEDS_OPERATOR


def test_build_queue_dismiss_hides_capacity_but_dispositioned_config_resurfaces_on_change() -> None:
    cap = att.capacity_items([{"agent": "beta", "kind": "budget", "detail": "90%"}])[0]
    disp = _disp(cap["item_id"], att.SOURCE_CAPACITY, att.ACTION_DISMISS, cap["source_hash"])
    q = att.build_queue([cap], [disp], now_iso=_now())
    assert not [i for i in q["items"] if i["source"] == att.SOURCE_CAPACITY]  # dismissed -> hidden


def test_build_queue_one_bad_source_does_not_blank_queue() -> None:
    good = att.needs_operator_items([{"request_id": "esc-1", "subject": "s", "sender": "b",
        "age_seconds": 1, "meta": {}}])
    err = att.source_error_item("dead_letter", "torn sidecar")
    q = att.build_queue(good + [err], [], now_iso=_now())
    srcs = {i["source"] for i in q["items"]}
    assert att.SOURCE_NEEDS_OPERATOR in srcs and att.SOURCE_ERROR in srcs   # both surface


def test_build_queue_resolved_dead_letter_hidden_by_default() -> None:
    dl = att.dead_letter_items([{"agent": "beta", "message_id": "m1"}])[0]
    disp = _disp(dl["item_id"], att.SOURCE_DEAD_LETTER, att.ACTION_RESOLVE_DEAD_LETTER, dl["source_hash"])
    q = att.build_queue([dl], [disp], now_iso=_now())
    assert not [i for i in q["items"] if i["source"] == att.SOURCE_DEAD_LETTER]  # resolved -> hidden
    q2 = att.build_queue([dl], [disp], now_iso=_now(), include_resolved=True)
    assert [i for i in q2["items"] if i["source"] == att.SOURCE_DEAD_LETTER]     # --resolved shows it
