"""#305 F10 (recast): the seat-level recovery time is computed ONCE, server-side
(``web._rate_limit_recovery_epoch``), by the SAME rule the usage-limit park decision
trusts (``usage_park.latest_exhausted_reset`` - the latest reset across every window
CURRENTLY exhausted) - never a console picking one named window's own reset. Three
rounds of console-side "which window do I believe" patches each found a new way to
show the wrong recovery time; this is the rule, implemented once, that replaces them.
"""

from __future__ import annotations

from agenttalk import web


NOW = 1788900000  # a fixed wall clock; matches tests/console2_fixtures.mjs's NOW in spirit


def _health(reason_code, reason_detail=None):
    h = {"state": "rate_limited_or_outage", "reason_code": reason_code}
    if reason_detail is not None:
        h["reason_detail"] = reason_detail
    return h


def _cap(*, confidence="fresh", primary=None, secondary=None):
    cap: dict = {"confidence": confidence}
    if primary is not None:
        cap["primary"] = {"label": "5h", "used_pct": primary[0], "resets_at": primary[1]}
    if secondary is not None:
        cap["secondary"] = {"label": "weekly", "used_pct": secondary[0], "resets_at": secondary[1]}
    return cap


def test_both_windows_exhausted_the_named_window_does_not_decide_it():
    """The connector's exact F10 repro: a five_hour rejection, but BOTH windows read 100%
    right now - the weekly reset (three days out) must win, same as for a seven_day-named
    record with the identical capacity evidence. The named window never decides this."""
    cap = _cap(primary=(100, NOW + 3600), secondary=(100, NOW + 3 * 86400))
    for window in ("five_hour", "seven_day"):
        health = _health("usage_limit_rejected", f"rate_limit_event.rejected.{window}")
        assert web._rate_limit_recovery_epoch(health, cap, now_epoch=NOW) == NOW + 3 * 86400, window


def test_one_window_exhausted_its_own_reset_is_used():
    cap = _cap(primary=(100, NOW + 3600), secondary=(50, NOW + 3 * 86400))
    health = _health("usage_limit_rejected", "rate_limit_event.rejected.five_hour")
    assert web._rate_limit_recovery_epoch(health, cap, now_epoch=NOW) == NOW + 3600


def test_no_window_exhausted_right_now_gives_no_recovery_time():
    cap = _cap(primary=(50, NOW + 3600), secondary=(99, NOW + 3 * 86400))
    health = _health("usage_limit_rejected", "rate_limit_event.rejected.five_hour")
    assert web._rate_limit_recovery_epoch(health, cap, now_epoch=NOW) is None


def test_stale_capacity_evidence_gives_no_recovery_time_even_at_100_percent():
    cap = _cap(confidence="stale", primary=(100, NOW + 3600))
    health = _health("usage_limit_rejected", "rate_limit_event.rejected.five_hour")
    assert web._rate_limit_recovery_epoch(health, cap, now_epoch=NOW) is None


def test_absent_capacity_gives_no_recovery_time():
    health = _health("usage_limit_rejected", "rate_limit_event.rejected.five_hour")
    assert web._rate_limit_recovery_epoch(health, None, now_epoch=NOW) is None


def test_an_older_record_with_no_window_still_gets_a_recovery_time_from_current_evidence():
    """No `reason_detail` at all (an older record) still gets the rule's benefit - it is
    keyed on `reason_code`, never on having a named window."""
    cap = _cap(primary=(100, NOW + 3600))
    health = _health("usage_limit_rejected")
    assert web._rate_limit_recovery_epoch(health, cap, now_epoch=NOW) == NOW + 3600


def test_throttled_and_overloaded_never_get_an_allowance_recovery_time():
    cap = _cap(primary=(100, NOW + 3600), secondary=(100, NOW + 3 * 86400))
    for reason in ("throttled", "overloaded"):
        health = _health(reason)
        assert web._rate_limit_recovery_epoch(health, cap, now_epoch=NOW) is None, reason


def test_a_legacy_or_unclassified_reason_never_gets_a_recovery_time():
    cap = _cap(primary=(100, NOW + 3600))
    for reason in ("adapter_rate_limit", "adapter_retryable_error", "lock_contention", None):
        health = _health(reason)
        assert web._rate_limit_recovery_epoch(health, cap, now_epoch=NOW) is None, reason


def test_a_malformed_health_or_capacity_value_gives_no_recovery_time():
    cap = _cap(primary=(100, NOW + 3600))
    assert web._rate_limit_recovery_epoch(None, cap, now_epoch=NOW) is None
    assert web._rate_limit_recovery_epoch("not-a-dict", cap, now_epoch=NOW) is None
    health = _health("usage_limit_rejected")
    assert web._rate_limit_recovery_epoch(health, "not-a-dict", now_epoch=NOW) is None
