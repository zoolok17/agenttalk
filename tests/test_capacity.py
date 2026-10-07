"""Tests for capacity.py — the advisory budget-snapshot parsers.

These read real provider formats (Claude status-line dump, Codex rollout
JSONL), normalize to CapacitySnapshot, and must degrade to None/unknown on
anything missing or malformed — never raise (the signal is advisory).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agenttalk import capacity as cap

_CLAUDE_JSON = {
    "model": {"id": "claude-opus-4-8", "display_name": "Opus 4.8"},
    "rate_limits": {
        "five_hour": {"used_percentage": 23.5, "resets_at": 1738425600},
        "seven_day": {"used_percentage": 41.2, "resets_at": 1738857600},
    },
}

_CODEX_RL = {
    "limit_id": "codex", "limit_name": None,
    "primary": {"used_percent": 12.0, "window_minutes": 300, "resets_at": 1781005233},
    "secondary": {"used_percent": 41.0, "window_minutes": 10080, "resets_at": 1781137669},
    "credits": None, "individual_limit": None,
    "plan_type": "pro", "rate_limit_reached_type": None,
}


def _write_claude(tmp: Path, payload: dict) -> Path:
    p = tmp / "statusline-last-input.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def _write_codex_rollout(sessions: Path, name: str, *records: dict) -> Path:
    d = sessions / "2026" / "06" / "09"
    return _write_codex_rollout_file(d / name, *records)


def _write_codex_rollout_file(p: Path, *records: dict) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return p


def _write_codex_rollout_with_invalid_utf8(
    sessions: Path, name: str, before: dict, after: dict,
) -> Path:
    p = sessions / "2026" / "06" / "09" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(
        json.dumps(before).encode("utf-8") + b"\n"
        + b'{"invalid":"\xff"}\n'
        + json.dumps(after).encode("utf-8") + b"\n"
    )
    return p


_CODEX_INFO = {
    "total_token_usage": {"input_tokens": 141298351, "total_tokens": 141709863},
    "last_token_usage": {"input_tokens": 125244, "total_tokens": 125386},
    "model_context_window": 258400,
}

_CLAUDE_CONTEXT = {
    "context_window_size": 1000000, "used_percentage": 21,
    "current_usage": {"input_tokens": 2, "cache_read_input_tokens": 205000,
                      "cache_creation_input_tokens": 1186, "output_tokens": 500},
}


@pytest.fixture(autouse=True)
def _clear_codex_thread_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)


def _token_count(rl: dict, info: dict | None = None) -> dict:
    return {"timestamp": "2026-06-09T08:00:00.0Z", "type": "event_msg",
            "payload": {"type": "token_count", "info": info or {}, "rate_limits": rl}}


# ----------------------------------------------------------- snapshot model

def test_snapshot_roundtrips_through_dict() -> None:
    snap = cap.read_claude_statusline("claude", path=None) or cap.CapacitySnapshot.unknown("claude")
    d = snap.to_dict()
    again = cap.CapacitySnapshot.from_dict(d)
    assert again is not None and again.to_dict() == d


def test_from_dict_rejects_non_dict_and_missing_required() -> None:
    assert cap.CapacitySnapshot.from_dict("nope") is None
    assert cap.CapacitySnapshot.from_dict({"source_agent": "x"}) is None  # missing required


def test_unknown_snapshot_is_safe() -> None:
    u = cap.CapacitySnapshot.unknown("alpha")
    assert u.source == "unknown" and u.confidence == "unknown"
    assert u.primary_used_percent is None and u.secondary_used_percent is None


# --------------------------------------------------- Claude status-line read

def test_read_claude_statusline_parses_both_windows(tmp_path: Path) -> None:
    p = _write_claude(tmp_path, _CLAUDE_JSON)
    snap = cap.read_claude_statusline("claude", path=p)
    assert snap is not None
    assert snap.source == "claude_statusline" and snap.confidence == "observed"
    assert snap.primary_used_percent == 23.5
    assert snap.primary_resets_at == 1738425600
    assert snap.primary_window_minutes == cap.FIVE_HOUR_MINUTES
    assert snap.secondary_used_percent == 41.2
    assert snap.secondary_window_minutes == cap.WEEKLY_MINUTES


def test_read_claude_statusline_observed_at_uses_file_mtime(tmp_path: Path) -> None:
    p = _write_claude(tmp_path, _CLAUDE_JSON)
    mtime = datetime(2026, 6, 9, 7, 30, 0, tzinfo=timezone.utc).timestamp()
    os.utime(p, (mtime, mtime))

    snap = cap.read_claude_statusline("claude", path=p)

    assert snap is not None
    assert snap.observed_at == "2026-06-09T07:30:00Z"


def test_read_claude_statusline_none_on_absent_or_garbage(tmp_path: Path) -> None:
    assert cap.read_claude_statusline("claude", path=tmp_path / "nope.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert cap.read_claude_statusline("claude", path=bad) is None
    no_rl = _write_claude(tmp_path, {"model": {"id": "x"}})  # no rate_limits
    assert cap.read_claude_statusline("claude", path=no_rl) is None


def test_read_claude_statusline_none_when_windows_empty(tmp_path: Path) -> None:
    p = _write_claude(tmp_path, {"rate_limits": {"five_hour": {}, "seven_day": {}}})
    assert cap.read_claude_statusline("claude", path=p) is None


def test_read_claude_statusline_parses_context_window(tmp_path: Path) -> None:
    p = _write_claude(tmp_path, dict(_CLAUDE_JSON, context_window=_CLAUDE_CONTEXT))
    snap = cap.read_claude_statusline("claude", path=p)
    assert snap is not None
    assert snap.context_used_percent == 21.0
    assert snap.context_window_size == 1000000
    assert snap.context_tokens == 2 + 205000 + 1186  # input side only; output excluded


def test_read_claude_statusline_context_only_without_budget(tmp_path: Path) -> None:
    """Context present with NO rate_limits block still yields a snapshot —
    budget and context are independent; either alone is publishable."""
    p = _write_claude(tmp_path, {"context_window": {"context_window_size": 200000,
                                                    "used_percentage": 60}})
    snap = cap.read_claude_statusline("claude", path=p)
    assert snap is not None
    assert snap.context_used_percent == 60.0 and snap.primary_used_percent is None
    # an empty/placeholder rate_limits block alongside context behaves the same
    p2 = _write_claude(tmp_path, {"rate_limits": {"five_hour": {}, "seven_day": {}},
                                  "context_window": {"context_window_size": 200000, "used_percentage": 60}})
    assert cap.read_claude_statusline("claude", path=p2) is not None


# --------------------------------------------------------- Codex rollout read

def test_read_codex_rollout_parses_primary_secondary(tmp_path: Path) -> None:
    _write_codex_rollout(tmp_path, "rollout-a.jsonl", _token_count(_CODEX_RL))
    snap = cap.read_codex_rollout("codex", sessions_dir=tmp_path)
    assert snap is not None and snap.source == "codex_rollout"
    assert snap.primary_used_percent == 12.0 and snap.primary_window_minutes == 300
    assert snap.secondary_used_percent == 41.0 and snap.secondary_window_minutes == 10080
    assert snap.plan_type == "pro" and snap.limit_id == "codex"


def test_read_codex_rollout_takes_last_record(tmp_path: Path) -> None:
    early = dict(_CODEX_RL, primary={"used_percent": 5.0, "window_minutes": 300, "resets_at": 1})
    late = dict(_CODEX_RL, primary={"used_percent": 88.0, "window_minutes": 300, "resets_at": 2})
    _write_codex_rollout(tmp_path, "rollout-a.jsonl",
                         _token_count(early), {"type": "other"}, _token_count(late))
    snap = cap.read_codex_rollout("codex", sessions_dir=tmp_path)
    assert snap is not None and snap.primary_used_percent == 88.0  # last wins


def test_read_local_codex_isolates_invalid_utf8_physical_line(tmp_path: Path) -> None:
    thread_id = "thread-after-invalid-byte"
    early = _token_count(dict(
        _CODEX_RL,
        primary={"used_percent": 11.0, "window_minutes": 300, "resets_at": 1},
    ))
    late = _token_count(dict(
        _CODEX_RL,
        primary={"used_percent": 77.0, "window_minutes": 300, "resets_at": 2},
    ))
    late["thread_id"] = thread_id
    _write_codex_rollout_with_invalid_utf8(
        tmp_path, f"rollout-2026-06-09T08-00-00-{thread_id}.jsonl", early, late,
    )

    snap = cap.read_local(
        "codex", source="codex", sessions_dir=tmp_path, thread_id=thread_id,
    )

    assert snap.source == "codex_rollout"
    assert snap.primary_used_percent == 77.0


def test_read_codex_rollout_prefers_newest_file(tmp_path: Path) -> None:
    old = dict(_CODEX_RL, primary={"used_percent": 5.0, "window_minutes": 300, "resets_at": 1})
    new = dict(_CODEX_RL, primary={"used_percent": 77.0, "window_minutes": 300, "resets_at": 2})
    f_old = _write_codex_rollout(tmp_path, "rollout-old.jsonl", _token_count(old))
    f_new = _write_codex_rollout(tmp_path, "rollout-new.jsonl", _token_count(new))
    import os
    os.utime(f_old, (1_000_000, 1_000_000))      # old mtime
    os.utime(f_new, (2_000_000, 2_000_000))      # newer mtime
    snap = cap.read_codex_rollout("codex", sessions_dir=tmp_path)
    assert snap is not None and snap.primary_used_percent == 77.0


def test_read_codex_rollout_orders_by_file_mtime_not_parent_dir_mtime(tmp_path: Path) -> None:
    stale = dict(_CODEX_RL, primary={"used_percent": 12.0, "window_minutes": 300, "resets_at": 1})
    fresh = dict(_CODEX_RL, primary={"used_percent": 88.0, "window_minutes": 300, "resets_at": 2})
    stale_dir = tmp_path / "newer-parent"
    fresh_dir = tmp_path / "older-parent"
    f_stale = _write_codex_rollout_file(stale_dir / "rollout-stale.jsonl", _token_count(stale))
    f_fresh = _write_codex_rollout_file(fresh_dir / "rollout-fresh.jsonl", _token_count(fresh))
    os.utime(f_fresh, (3_000, 3_000))
    os.utime(f_stale, (1_000, 1_000))
    os.utime(fresh_dir, (100, 100))
    os.utime(stale_dir, (200, 200))

    snap = cap.read_codex_rollout("codex", sessions_dir=tmp_path)
    assert snap is not None and snap.primary_used_percent == 88.0


def test_read_codex_rollout_uses_bounded_walk_not_rglob(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_codex_rollout(tmp_path, "rollout-a.jsonl", _token_count(_CODEX_RL))

    def fail_rglob(self: Path, pattern: str):  # noqa: ANN202 - monkeypatched test guard
        raise AssertionError(f"unbounded rglob called for {pattern}")

    monkeypatch.setattr(Path, "rglob", fail_rglob)
    snap = cap.read_codex_rollout("codex", sessions_dir=tmp_path)
    assert snap is not None and snap.primary_used_percent == 12.0


def test_read_codex_rollout_scan_limit_bounds_candidate_walk(tmp_path: Path) -> None:
    _write_codex_rollout(tmp_path, "rollout-a.jsonl", _token_count(_CODEX_RL))
    assert cap.read_codex_rollout("codex", sessions_dir=tmp_path, max_scan_entries=1) is None

    snap = cap.read_codex_rollout("codex", sessions_dir=tmp_path, max_scan_entries=4)
    assert snap is not None and snap.primary_used_percent == 12.0


def test_read_codex_rollout_incomplete_scan_fails_closed_after_candidate(tmp_path: Path) -> None:
    _write_codex_rollout_file(tmp_path / "rollout-seen.jsonl", _token_count(_CODEX_RL))
    hidden = tmp_path / "more" / "rollout-hidden.jsonl"
    _write_codex_rollout_file(hidden, _token_count(_CODEX_RL))

    assert cap.read_codex_rollout("codex", sessions_dir=tmp_path, max_scan_entries=2) is None


def test_read_codex_rollout_none_on_absent_or_no_ratelimits(tmp_path: Path) -> None:
    assert cap.read_codex_rollout("codex", sessions_dir=tmp_path / "missing") is None
    _write_codex_rollout(tmp_path, "rollout-x.jsonl", {"type": "other", "payload": {}})
    assert cap.read_codex_rollout("codex", sessions_dir=tmp_path) is None


def test_read_codex_rollout_thread_id_wins_over_newest(tmp_path: Path) -> None:
    """A NEWER rollout without the thread id loses to an older one whose
    filename carries it (Codex contract: don't pick a resumed/forked sibling)."""
    newest = dict(_CODEX_RL, primary={"used_percent": 99.0, "window_minutes": 300, "resets_at": 1})
    match = dict(_CODEX_RL, primary={"used_percent": 33.0, "window_minutes": 300, "resets_at": 2})
    f_new = _write_codex_rollout(tmp_path, "rollout-newest.jsonl", _token_count(newest))
    f_match = _write_codex_rollout(tmp_path, "rollout-2026-THREADXYZ.jsonl", _token_count(match))
    os.utime(f_new, (3_000_000, 3_000_000))      # newest mtime, NO thread id
    os.utime(f_match, (2_000_000, 2_000_000))     # older, has thread id in name
    snap = cap.read_codex_rollout("codex", sessions_dir=tmp_path, thread_id="THREADXYZ")
    assert snap is not None and snap.primary_used_percent == 33.0


def test_read_codex_rollout_uses_thread_id_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    other = dict(_CODEX_RL, primary={"used_percent": 99.0, "window_minutes": 300, "resets_at": 1})
    match = dict(_CODEX_RL, primary={"used_percent": 22.0, "window_minutes": 300, "resets_at": 2})
    f_other = _write_codex_rollout(tmp_path, "rollout-other.jsonl", _token_count(other))
    f_match = _write_codex_rollout(tmp_path, "rollout-TID123.jsonl", _token_count(match))
    os.utime(f_other, (3_000_000, 3_000_000))
    os.utime(f_match, (2_000_000, 2_000_000))
    monkeypatch.setenv("CODEX_THREAD_ID", "TID123")
    snap = cap.read_codex_rollout("codex", sessions_dir=tmp_path)  # thread_id resolved from env
    assert snap is not None and snap.primary_used_percent == 22.0


def test_read_codex_rollout_thread_id_miss_fails_closed(tmp_path: Path) -> None:
    _write_codex_rollout(tmp_path, "rollout-other.jsonl", _token_count(_CODEX_RL))
    assert cap.read_codex_rollout("codex", sessions_dir=tmp_path, thread_id="MISSING") is None


def test_read_codex_rollout_falls_back_to_newest_without_thread_id(tmp_path: Path) -> None:
    old = dict(_CODEX_RL, primary={"used_percent": 5.0, "window_minutes": 300, "resets_at": 1})
    new = dict(_CODEX_RL, primary={"used_percent": 70.0, "window_minutes": 300, "resets_at": 2})
    f_old = _write_codex_rollout(tmp_path, "rollout-old.jsonl", _token_count(old))
    f_new = _write_codex_rollout(tmp_path, "rollout-new.jsonl", _token_count(new))
    os.utime(f_old, (1_000_000, 1_000_000))
    os.utime(f_new, (2_000_000, 2_000_000))
    snap = cap.read_codex_rollout("codex", sessions_dir=tmp_path, thread_id=None)
    assert snap is not None and snap.primary_used_percent == 70.0  # newest overall


def test_read_codex_rollout_observed_at_from_record_timestamp(tmp_path: Path) -> None:
    rec = {"timestamp": "2026-06-09T08:30:00Z", "type": "event_msg",
           "payload": {"type": "token_count", "rate_limits": _CODEX_RL}}
    _write_codex_rollout(tmp_path, "rollout-a.jsonl", rec)
    snap = cap.read_codex_rollout("codex", sessions_dir=tmp_path)
    assert snap is not None and snap.observed_at == "2026-06-09T08:30:00Z"


def test_read_codex_rollout_skips_record_without_trustworthy_timestamp(tmp_path: Path) -> None:
    """A missing/garbage record timestamp must not be papered over with a fresh
    observed_at (that would hide staleness) — the record is skipped (review nit)."""
    no_ts = {"type": "event_msg", "payload": {"type": "token_count", "rate_limits": _CODEX_RL}}
    _write_codex_rollout(tmp_path, "rollout-a.jsonl", no_ts)
    assert cap.read_codex_rollout("codex", sessions_dir=tmp_path) is None
    bad_ts = dict(no_ts, timestamp="not-a-date")
    _write_codex_rollout(tmp_path, "rollout-b.jsonl", bad_ts)
    assert cap.read_codex_rollout("codex", sessions_dir=tmp_path) is None


def test_read_codex_rollout_falls_back_when_latest_eligible_timestamp_is_malformed(
    tmp_path: Path,
) -> None:
    earlier = _token_count(dict(
        _CODEX_RL,
        primary={"used_percent": 23.0, "window_minutes": 300, "resets_at": 1},
    ))
    malformed_latest = _token_count(dict(
        _CODEX_RL,
        primary={"used_percent": 99.0, "window_minutes": 300, "resets_at": 2},
    ))
    malformed_latest["timestamp"] = "not-a-date"
    _write_codex_rollout(tmp_path, "rollout-a.jsonl", earlier, malformed_latest)

    snapshot = cap.read_codex_rollout("codex", sessions_dir=tmp_path)

    assert snapshot is not None
    assert snapshot.primary_used_percent == 23.0


@pytest.mark.parametrize("unusable_payload", [
    {
        "type": "token_count",
        "rate_limits": {},
        "info": {"model_context_window": 258400},
    },
    {
        "type": "token_count",
        "rate_limits": {"primary": {"used_percent": "not-a-number"}},
    },
])
def test_read_codex_rollout_falls_back_when_latest_candidate_has_no_usable_signal(
    tmp_path: Path, unusable_payload: dict,
) -> None:
    earlier = _token_count(dict(
        _CODEX_RL,
        primary={"used_percent": 23.0, "window_minutes": 300, "resets_at": 1},
    ))
    unusable_latest = {
        "timestamp": "2026-06-09T08:30:00Z",
        "type": "event_msg",
        "payload": unusable_payload,
    }
    _write_codex_rollout(tmp_path, "rollout-a.jsonl", earlier, unusable_latest)

    snapshot = cap.read_codex_rollout("codex", sessions_dir=tmp_path)

    assert snapshot is not None
    assert snapshot.primary_used_percent == 23.0


def test_normalize_ts_accepts_any_fractional_precision() -> None:
    """Providers emit variable sub-second precision (e.g. Codex's "...:00.0Z").
    Python 3.10's fromisoformat only accepts 3- or 6-digit fractions, so the
    parser must pad/truncate; every precision normalizes to the same UTC second
    on all supported Pythons (regression guard for the 3.10-only CI failure)."""
    for frac in ("", ".0", ".12", ".123", ".123456", ".1234567"):
        assert cap._normalize_ts(f"2026-06-09T08:00:00{frac}Z") == "2026-06-09T08:00:00Z"
    assert cap._normalize_ts("2026-06-09T08:00:00.0+02:00") == "2026-06-09T06:00:00Z"  # offset honored
    assert cap._normalize_ts("not-a-date") is None        # garbage -> None
    assert cap._normalize_ts("2026-06-09T08:00:00") is None  # naive (no tz) -> None


def test_read_codex_rollout_maps_windows_by_minutes_not_position(tmp_path: Path) -> None:
    """Windows are classified by window_minutes (300=5h, 10080=weekly), so a
    primary/secondary swap still lands in the right slots."""
    rl = dict(_CODEX_RL,
              primary={"used_percent": 41.0, "window_minutes": 10080, "resets_at": 1},
              secondary={"used_percent": 12.0, "window_minutes": 300, "resets_at": 2})
    _write_codex_rollout(tmp_path, "rollout-a.jsonl", _token_count(rl))
    snap = cap.read_codex_rollout("codex", sessions_dir=tmp_path)
    assert snap is not None
    assert snap.primary_used_percent == 12.0 and snap.primary_window_minutes == 300
    assert snap.secondary_used_percent == 41.0 and snap.secondary_window_minutes == 10080


# --------------------------------------------------------- context headroom

def test_read_codex_rollout_parses_context(tmp_path: Path) -> None:
    _write_codex_rollout(tmp_path, "rollout-a.jsonl", _token_count(_CODEX_RL, _CODEX_INFO))
    snap = cap.read_codex_rollout("codex", sessions_dir=tmp_path)
    assert snap is not None
    assert snap.context_window_size == 258400
    assert snap.context_tokens == 125244          # last_token_usage.input_tokens, NOT cumulative
    assert snap.context_used_percent == round(125244 / 258400 * 100, 1)  # ~48.5


def test_read_codex_rollout_context_only_without_budget(tmp_path: Path) -> None:
    """A token_count record with context (info) but NO rate_limits key is still
    eligible — record selection is decoupled from rate_limits."""
    rec = {"timestamp": "2026-06-09T08:30:00Z", "type": "event_msg",
           "payload": {"type": "token_count", "info": _CODEX_INFO}}
    _write_codex_rollout(tmp_path, "rollout-a.jsonl", rec)
    snap = cap.read_codex_rollout("codex", sessions_dir=tmp_path)
    assert snap is not None
    assert snap.context_used_percent is not None and snap.primary_used_percent is None


def test_read_codex_rollout_no_info_leaves_context_none(tmp_path: Path) -> None:
    _write_codex_rollout(tmp_path, "rollout-a.jsonl", _token_count(_CODEX_RL))  # info={}
    snap = cap.read_codex_rollout("codex", sessions_dir=tmp_path)
    assert snap is not None
    assert snap.context_used_percent is None and snap.context_tokens is None


def test_context_helpers_guard_bad_input() -> None:
    assert cap._codex_context(None) == (None, None, None)
    # zero/garbage window must not divide-by-zero — percent stays None
    assert cap._codex_context({"model_context_window": 0,
                               "last_token_usage": {"input_tokens": 5}}) == (None, 0, 5)
    assert cap._claude_context(None) == (None, None, None)
    assert cap._claude_context({"used_percentage": 50}) == (50.0, None, None)


# ------------------------------------------------------------- read_local

def test_read_local_auto_detects_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = _write_claude(tmp_path, _CLAUDE_JSON)
    monkeypatch.setenv("CLAUDECODE", "1")
    snap = cap.read_local("claude", source="auto", statusline_path=p)
    assert snap.source == "claude_statusline"


def test_read_local_auto_detects_codex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_codex_rollout(tmp_path, "rollout-a.jsonl", _token_count(_CODEX_RL))
    monkeypatch.delenv("CLAUDECODE", raising=False)
    snap = cap.read_local("codex", source="auto", sessions_dir=tmp_path)
    assert snap.source == "codex_rollout"


def test_read_local_auto_detects_codex_home_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home = tmp_path / "codex-home"
    _write_codex_rollout(codex_home / "sessions", "rollout-home.jsonl", _token_count(_CODEX_RL))
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    snap = cap.read_local("codex", source="auto")

    assert snap.source == "codex_rollout"
    assert snap.primary_used_percent == 12.0


def test_unknown_snapshot_can_carry_reason() -> None:
    snap = cap.CapacitySnapshot.unknown("codex", reason="codex_home_missing")

    assert snap.source == "unknown"
    assert snap.confidence == "unknown"
    assert snap.reason == "codex_home_missing"


def test_read_local_returns_unknown_when_undetectable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDECODE", raising=False)
    snap = cap.read_local("ghost", source="auto", sessions_dir=tmp_path / "no-codex")
    assert snap.source == "unknown" and snap.confidence == "unknown"


# ------------------------------------------------------ staleness evaluation

def test_effective_confidence_observed_then_stale() -> None:
    now = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)
    fresh = {"confidence": "observed",
             "observed_at": (now - timedelta(seconds=60)).isoformat().replace("+00:00", "Z")}
    old = {"confidence": "observed",
           "observed_at": (now - timedelta(seconds=4000)).isoformat().replace("+00:00", "Z")}
    assert cap.effective_confidence(fresh, now=now) == "observed"
    assert cap.effective_confidence(old, now=now) == "stale"


def test_effective_confidence_unknown_and_garbage() -> None:
    assert cap.effective_confidence({"confidence": "unknown", "observed_at": "x"}) == "unknown"
    assert cap.effective_confidence({"confidence": "observed", "observed_at": "garbage"}) == "unknown"
    assert cap.effective_confidence({}) == "unknown"


# ---------------------------------------- #301: real, per-account, stale-marked readings

_NOW = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def _rate_limit_info(case: list[dict]) -> dict:
    return next(e for e in case if e.get("type") == "rate_limit_event")["rate_limit_info"]


@pytest.fixture
def _os_user(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(cap.getpass, "getuser", lambda: "tester")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return "tester"


def _home(monkeypatch: pytest.MonkeyPatch, home: Path) -> Path:
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    return home


def test_301_a_rate_limit_event_with_figures_updates_the_reading() -> None:
    import golden_stop_retries_scenarios as real

    reading = cap.claude_stream_reading(
        _rate_limit_info(real.REAL_CASE_FIVE_HOUR), observed_at=_iso(_NOW))
    snap = cap.read_claude_stream("seat", reading, now=_NOW)

    assert snap is not None and snap.source == "claude_stream"
    assert snap.observed_at == _iso(_NOW)          # when the seat saw the event
    assert snap.primary_used_percent == 103.0      # utilization 1.03 is a fraction
    assert snap.primary_resets_at == 1788948000
    assert snap.primary_status == "rejected"
    assert snap.secondary_used_percent == 86.0
    assert snap.secondary_resets_at == 1789160400
    assert snap.secondary_status is None           # the event judged only the 5-hour window
    assert snap.rate_limit_reached_type == "five_hour"
    assert snap.primary_window_basis == "assumed"  # Claude names a window, never its length
    assert snap.reason is None

    later = cap.claude_stream_reading(
        _rate_limit_info(real.REAL_CASE_SEVEN_DAY), reading,
        observed_at=_iso(_NOW + timedelta(seconds=30)))
    snap2 = cap.read_claude_stream("seat", later, now=_NOW + timedelta(seconds=40))
    assert snap2 is not None
    assert snap2.primary_used_percent == 0.0 and snap2.secondary_used_percent == 100.0
    assert snap2.secondary_status == "rejected" and snap2.primary_status is None


def test_301_an_allowed_event_without_figures_records_no_percentage() -> None:
    import golden_stop_retries_scenarios as real

    earlier = cap.claude_stream_reading(
        _rate_limit_info(real.REAL_CASE_FIVE_HOUR), observed_at=_iso(_NOW))
    allowed = {"status": "allowed", "resetsAt": 1788990000, "rateLimitType": "five_hour"}
    reading = cap.claude_stream_reading(
        allowed, earlier, observed_at=_iso(_NOW + timedelta(seconds=60)))
    snap = cap.read_claude_stream("seat", reading, now=_NOW + timedelta(seconds=90))

    assert snap is not None
    assert snap.primary_status == "allowed"
    assert snap.primary_resets_at == 1788990000
    assert snap.primary_used_percent is None       # no figure given, and the old 103% is gone
    assert snap.rate_limit_reached_type is None
    assert snap.secondary_used_percent == 86.0     # a window the event did not mention stays

    only = cap.claude_stream_reading(allowed, observed_at=_iso(_NOW))
    alone = cap.read_claude_stream("seat", only, now=_NOW)
    assert alone is not None
    assert alone.primary_used_percent is None and alone.secondary_used_percent is None
    assert alone.reason == "no_figures_in_event"   # says so, instead of a made-up number


def test_301_a_window_older_than_the_bound_is_left_out() -> None:
    old = {"utilization": 0.5, "resetsAt": 1788948000}
    first = cap.claude_stream_reading(
        {"status": "allowed", "rateLimitType": "seven_day",
         "unifiedWindows": {"seven_day": old}},
        observed_at=_iso(_NOW - timedelta(minutes=30)))
    reading = cap.claude_stream_reading(
        {"status": "allowed", "rateLimitType": "five_hour",
         "unifiedWindows": {"five_hour": {"utilization": 0.2, "resetsAt": 1788948000}}},
        first, observed_at=_iso(_NOW))

    snap = cap.read_claude_stream("seat", reading, now=_NOW)
    assert snap is not None and snap.observed_at == _iso(_NOW)
    view = cap.current_view(snap.to_dict(), now=_NOW)
    assert view["primary_used_percent"] == 20.0
    assert view["secondary_used_percent"] is None  # 30 minutes old: not shown as current

    later = cap.current_view(snap.to_dict(), now=_NOW + timedelta(hours=1))
    assert later["primary_used_percent"] is None and later["confidence"] == "stale"


def test_301_a_missing_claude_source_says_not_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _os_user: str,
) -> None:
    _home(monkeypatch, tmp_path / "home")
    snap = cap.read_local("seat", source="claude")

    assert snap.source == "unknown" and snap.confidence == "unknown"
    assert snap.reason == "claude_source_not_configured"
    assert snap.primary_used_percent is None
    assert snap.account == "claude:tester"


def test_301_an_old_status_line_is_published_stale_without_numbers(
    tmp_path: Path, _os_user: str,
) -> None:
    p = _write_claude(tmp_path, _CLAUDE_JSON)
    old = datetime(2026, 6, 3, 14, 48, 54, tzinfo=timezone.utc).timestamp()
    os.utime(p, (old, old))

    snap = cap.for_publication(
        cap.read_local("seat", source="claude", statusline_path=p, now=_NOW), now=_NOW)

    assert snap.confidence == "stale"
    assert snap.reason == "claude_statusline_stale"
    assert snap.observed_at == "2026-06-03T14:48:54Z"   # the real age stays visible
    assert snap.primary_used_percent is None and snap.secondary_used_percent is None
    assert snap.primary_resets_at is None and snap.context_used_percent is None
    old_verdict = cap.CapacitySnapshot.unknown("seat")
    old_verdict.confidence, old_verdict.observed_at = "observed", "2026-06-03T14:48:54Z"
    old_verdict.last_status = "rejected"
    assert cap.for_publication(old_verdict, now=_NOW).last_status is None
    fresh = cap.read_local("seat", source="claude", statusline_path=_write_claude(
        tmp_path, _CLAUDE_JSON))
    assert cap.for_publication(fresh).primary_used_percent == 23.5


def test_301_the_seat_stream_is_preferred_over_the_status_line(
    tmp_path: Path, _os_user: str,
) -> None:
    import golden_stop_retries_scenarios as real

    p = _write_claude(tmp_path, _CLAUDE_JSON)         # fresh: written just now
    now = datetime.now(timezone.utc)
    binding = {"binding": cap.account_key("claude", tmp_path)}
    reading = cap.claude_stream_reading(
        _rate_limit_info(real.REAL_CASE_FIVE_HOUR), binding, observed_at=_iso(now))

    snap = cap.read_local("seat", source="claude", statusline_path=p, stream=reading)
    assert snap.source == "claude_stream" and snap.primary_used_percent == 103.0
    assert snap.account == cap.account_key("claude", tmp_path)   # the folder actually read

    old_reading = cap.claude_stream_reading(
        _rate_limit_info(real.REAL_CASE_FIVE_HOUR), binding,
        observed_at=_iso(now - timedelta(hours=2)))
    snap2 = cap.read_local("seat", source="claude", statusline_path=p, stream=old_reading)
    assert snap2.source == "claude_statusline" and snap2.primary_used_percent == 23.5

    june = datetime(2026, 6, 3, 14, 48, 54, tzinfo=timezone.utc).timestamp()
    os.utime(p, (june, june))                        # both old: the seat's own, newer, stays
    snap3 = cap.read_local("seat", source="claude", statusline_path=p, stream=old_reading)
    assert snap3.source == "claude_stream"
    assert snap3.observed_at == _iso(now - timedelta(hours=2))


def test_301_status_line_time_and_content_come_from_one_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    p = _write_claude(tmp_path, _CLAUDE_JSON)
    old = datetime(2026, 6, 3, 14, 48, 54, tzinfo=timezone.utc).timestamp()
    os.utime(p, (old, old))
    newer = json.loads(json.dumps(_CLAUDE_JSON))
    newer["rate_limits"]["five_hour"]["used_percentage"] = 77.0
    new_time = datetime(2026, 10, 7, 11, 59, 0, tzinfo=timezone.utc).timestamp()
    real_read_text = Path.read_text
    calls: list[int] = []

    def read_then_replace(self: Path, *args: object, **kwargs: object) -> str:
        text = real_read_text(self, *args, **kwargs)
        if self == p and not calls:               # the file is replaced mid-read, once
            calls.append(1)
            p.write_text(json.dumps(newer), encoding="utf-8")
            os.utime(p, (new_time, new_time))
        return text

    monkeypatch.setattr(Path, "read_text", read_then_replace)
    snap = cap.read_claude_statusline("seat", path=p)

    assert snap is not None
    assert (snap.primary_used_percent, snap.observed_at) == (77.0, "2026-10-07T11:59:00Z")


def test_301_codex_says_whether_a_window_length_was_measured(tmp_path: Path) -> None:
    _write_codex_rollout(tmp_path / "a", "rollout-a.jsonl", _token_count(_CODEX_RL))
    snap = cap.read_codex_rollout("codex", sessions_dir=tmp_path / "a")
    assert snap is not None
    assert snap.primary_window_basis == "measured" and snap.secondary_window_basis == "measured"

    bare = {"primary": {"used_percent": 5.0}, "secondary": {"used_percent": 9.0}}
    _write_codex_rollout(tmp_path / "b", "rollout-b.jsonl", _token_count(bare))
    snap2 = cap.read_codex_rollout("codex", sessions_dir=tmp_path / "b")
    assert snap2 is not None
    assert snap2.primary_window_minutes == cap.FIVE_HOUR_MINUTES
    assert snap2.primary_window_basis == "assumed" and snap2.secondary_window_basis == "assumed"


def test_301_every_reading_names_its_scope_account_and_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _os_user: str,
) -> None:
    home = _home(monkeypatch, tmp_path / "home")
    own = tmp_path / "own-codex-home"
    _write_codex_rollout(own / "sessions", "rollout-own.jsonl", _token_count(_CODEX_RL))
    _write_codex_rollout(home / ".codex" / "sessions", "rollout-2026-06-09T08-00-00-SEAT-A.jsonl",
                         _token_count(_CODEX_RL))

    shared = cap.read_local("codex-a", source="codex", thread_id="SEAT-A")
    separate = cap.read_local("codex-b", source="codex", sessions_dir=own / "sessions")
    d = shared.to_dict()

    assert shared.source == "codex_rollout"
    assert d["schema_version"] == cap.CAPACITY_SCHEMA_VERSION == 2
    assert d["scope"] == "account"
    assert shared.account == "codex:tester"
    assert separate.account is not None and separate.account.startswith("codex:tester:home-")
    assert own.name not in json.dumps(separate.to_dict())   # a hash, never the path


def test_301_codex_shared_home_finds_the_seat_thread_among_newer_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _os_user: str,
) -> None:
    home = _home(monkeypatch, tmp_path / "home")
    sessions = home / ".codex" / "sessions"
    own = _write_codex_rollout(sessions, "rollout-2026-10-07T09-00-00-THREAD-SEAT.jsonl",
                               _token_count(_CODEX_RL))
    os.utime(own, (1_000_000, 1_000_000))
    other_rl = dict(_CODEX_RL, primary=dict(_CODEX_RL["primary"], used_percent=99.0))
    for i in range(cap.CODEX_ROLLOUT_MAX_FILES + 2):
        _write_codex_rollout(sessions, f"rollout-other-{i}.jsonl", _token_count(other_rl))

    snap = cap.read_local("seat", source="codex", thread_id="THREAD-SEAT")

    assert snap.source == "codex_rollout" and snap.primary_used_percent == 12.0


def test_301_codex_shared_home_reads_a_real_sized_sessions_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _os_user: str,
) -> None:
    home = _home(monkeypatch, tmp_path / "home")
    sessions = home / ".codex" / "sessions"
    day = sessions / "2026" / "06" / "09"
    day.mkdir(parents=True)
    for i in range(cap.CODEX_ROLLOUT_SCAN_LIMIT + 50):
        (day / f"note-{i}.txt").write_text("", encoding="utf-8")
    _write_codex_rollout(sessions, "rollout-THREAD-SEAT.jsonl", _token_count(_CODEX_RL))

    snap = cap.read_local("seat", source="codex", thread_id="THREAD-SEAT")

    assert snap.source == "codex_rollout" and snap.primary_used_percent == 12.0
    no_reading = cap.read_local("seat", source="codex", thread_id="NO-SUCH-THREAD")
    assert no_reading.source == "unknown" and no_reading.reason == "codex_no_reading"


# ------------------------------------------------ #301 fix round 1 (tk-f6c2bd539ea2)

def _real_allowed_info() -> dict:
    from test_wrapper_claude import PROBE

    return next(e for e in PROBE if e.get("type") == "rate_limit_event")["rate_limit_info"]


def test_301_r1_non_finite_numbers_never_raise() -> None:
    for bad in (float("inf"), float("-inf"), float("nan")):
        reading = cap.claude_stream_reading(
            {"status": "allowed", "rateLimitType": "five_hour", "resetsAt": bad,
             "utilization": bad, "unifiedWindows": {"seven_day": {"utilization": bad,
                                                                  "resetsAt": bad}}},
            observed_at=_iso(_NOW))
        snap = cap.read_claude_stream("seat", reading, now=_NOW)
        assert snap is not None
        assert snap.primary_resets_at is None and snap.primary_used_percent is None
        assert snap.secondary_resets_at is None and snap.secondary_used_percent is None
        assert snap.primary_status == "allowed"


def test_301_r1_the_real_allowed_event_without_a_window_is_kept(
    tmp_path: Path, _os_user: str,
) -> None:
    info = _real_allowed_info()
    assert info == {"status": "allowed"}            # the recorded real shape
    reading = cap.claude_stream_reading(info, observed_at=_iso(_NOW))
    snap = cap.read_claude_stream("seat", reading, now=_NOW)

    assert snap is not None and snap.source == "claude_stream"
    assert (snap.last_status, snap.last_status_at) == ("allowed", _iso(_NOW))
    assert snap.observed_at == _iso(_NOW) and snap.reason == "no_figures_in_event"
    assert snap.primary_used_percent is None and snap.primary_status is None
    assert snap.secondary_used_percent is None and snap.rate_limit_reached_type is None

    reading["binding"] = cap.account_key("claude", tmp_path)
    published = cap.read_local("seat", source="claude", stream=reading, now=_NOW,
                               statusline_path=tmp_path / "absent.json")
    assert published.source == "claude_stream" and published.last_status == "allowed"


def test_301_r1_the_claude_account_and_the_file_read_share_one_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _os_user: str,
) -> None:
    home = _home(monkeypatch, tmp_path / "home")
    own = tmp_path / "own-claude"
    for folder, used in ((home / ".claude", 12.0), (own, 91.0)):
        folder.mkdir(parents=True)
        five = {"five_hour": {"used_percentage": used, "resets_at": 1}}
        (folder / "statusline-last-input.json").write_text(
            json.dumps({"rate_limits": five}), encoding="utf-8")

    default = cap.read_local("seat", source="claude")
    assert (default.primary_used_percent, default.account) == (12.0, "claude:tester")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(own))
    by_env = cap.read_local("seat", source="claude")
    by_arg = cap.read_local("seat", source="claude", claude_home=own, provider="ovh-qwen")
    by_path = cap.read_local("seat", source="claude",
                             statusline_path=home / ".claude" / "statusline-last-input.json")

    assert by_env.primary_used_percent == 91.0 and by_env.account.startswith("claude:tester:home-")
    assert by_arg.primary_used_percent == 91.0 and by_arg.account.startswith("ovh-qwen:tester:home-")
    assert (by_path.primary_used_percent, by_path.account) == (12.0, "claude:tester")


def test_301_r1_when_both_sources_are_old_the_newer_one_is_named(
    tmp_path: Path, _os_user: str,
) -> None:
    import golden_stop_retries_scenarios as real

    p = _write_claude(tmp_path, _CLAUDE_JSON)
    twenty_ago = (_NOW - timedelta(minutes=20)).timestamp()
    os.utime(p, (twenty_ago, twenty_ago))
    reading = cap.claude_stream_reading(
        _rate_limit_info(real.REAL_CASE_FIVE_HOUR), {"binding": cap.account_key("claude", tmp_path)},
        observed_at=_iso(_NOW - timedelta(hours=2)))

    snap = cap.for_publication(cap.read_local(
        "seat", source="claude", statusline_path=p, stream=reading, now=_NOW), now=_NOW)
    assert snap.source == "claude_statusline" and snap.confidence == "stale"
    assert snap.observed_at == _iso(_NOW - timedelta(minutes=20))


def _codex_session(sessions: Path, name: str, used: float, *, session_id: str,
                   extra: list[dict] | None = None) -> Path:
    meta = {"timestamp": "2026-10-07T09:00:00Z", "type": "session_meta",
            "payload": {"id": session_id}}
    rl = dict(_CODEX_RL, primary=dict(_CODEX_RL["primary"], used_percent=used))
    info = {"model_context_window": 1000, "last_token_usage": {"input_tokens": 900}}
    return _write_codex_rollout(sessions, name, meta, _token_count(rl, info), *(extra or []))


def test_301_r1_the_shared_codex_home_never_lends_another_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _os_user: str,
) -> None:
    home = _home(monkeypatch, tmp_path / "home")
    sessions = home / ".codex" / "sessions"
    _codex_session(sessions, "rollout-2026-10-07T09-00-00-OTHER.jsonl", 88.0, session_id="OTHER",
                   extra=[{"type": "message", "text": "Please look at TARGET-THREAD"}])
    _codex_session(sessions, "rollout-2026-10-07T09-00-00-TARGET-THREAD-2.jsonl", 66.0,
                   session_id="TARGET-THREAD-2")

    no_thread = cap.read_local("seat", source="codex")
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))      # the default home, named
    named_default = cap.read_local("seat", source="codex", thread_id="")
    mentioned = cap.read_local("seat", source="codex", thread_id="TARGET-THREAD")

    for snap in (no_thread, named_default):
        assert snap.source == "unknown" and snap.reason == "codex_no_thread_yet"
        assert snap.account == "codex:tester" and snap.context_used_percent is None
    assert mentioned.source == "unknown" and mentioned.primary_used_percent is None

    _codex_session(sessions, "rollout-renamed.jsonl", 37.0, session_id="TARGET-THREAD")
    found = cap.read_local("seat", source="codex", thread_id="TARGET-THREAD")
    assert found.source == "codex_rollout" and found.primary_used_percent == 37.0


def test_301_r1_status_line_context_counts_only_for_the_seat_session(tmp_path: Path) -> None:
    payload = dict(_CLAUDE_JSON, session_id="OTHER", context_window={"used_percentage": 91})
    p = _write_claude(tmp_path, payload)

    unbound = cap.read_local("seat", source="claude", statusline_path=p)
    other = cap.read_local("seat", source="claude", statusline_path=p, session_id="MINE")
    mine = cap.read_local("seat", source="claude", statusline_path=p, session_id="OTHER")

    assert unbound.context_used_percent is None and other.context_used_percent is None
    assert unbound.primary_used_percent == 23.5     # the account figures still count
    assert mine.context_used_percent == 91.0


# ------------------------------------------------ #301 fix round 2 (tk-120e69a492b9)

def test_301_r2_each_part_expires_on_its_own_at_read_time() -> None:
    reading = cap.claude_stream_reading(
        {"status": "allowed", "rateLimitType": "five_hour", "utilization": 0.1},
        observed_at=_iso(_NOW - timedelta(minutes=9)))
    reading = cap.claude_stream_reading(
        {"status": "allowed", "rateLimitType": "seven_day", "utilization": 0.97}, reading,
        observed_at=_iso(_NOW))
    published = cap.for_publication(cap.read_claude_stream("seat", reading), now=_NOW).to_dict()
    assert published["primary_used_percent"] == 10.0 and published["observed_at"] == _iso(_NOW)

    later = cap.current_view(published, now=_NOW + timedelta(minutes=2))
    assert later["confidence"] == "observed"
    assert later["secondary_used_percent"] == 97.0         # still current: kept
    assert later["primary_used_percent"] is None           # 11 minutes old: hidden
    expired = cap.current_view(published, now=_NOW + timedelta(minutes=11))
    assert expired["confidence"] == "stale" and expired["secondary_used_percent"] is None
    assert expired["source"] == "claude_stream" and expired["observed_at"] == _iso(_NOW)
    assert published["secondary_used_percent"] == 97.0     # the reader's copy, not the file


def test_301_r2_a_reading_from_another_binding_is_never_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _os_user: str,
) -> None:
    _home(monkeypatch, tmp_path / "home")
    info = {"status": "allowed", "rateLimitType": "five_hour", "utilization": 0.91}
    now = datetime.now(timezone.utc)
    bound = cap.claude_stream_reading(info, {"binding": "claude:tester"}, observed_at=_iso(now))
    legacy = cap.claude_stream_reading(info, observed_at=_iso(now))
    assert bound["binding"] == "claude:tester" and "binding" not in legacy
    again = cap.claude_stream_reading(info, bound, observed_at=_iso(now))
    assert again["binding"] == "claude:tester"            # kept from event to event

    same = cap.read_local("seat", source="claude", stream=bound)
    gateway = cap.read_local("seat", source="claude", stream=bound, provider="ovh-qwen",
                             claude_home=tmp_path / "gateway-profile")
    unbound = cap.read_local("seat", source="claude", stream=legacy)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "other-home"))
    moved = cap.read_local("seat", source="claude", stream=bound)

    assert same.source == "claude_stream" and same.primary_used_percent == 91.0
    for snap in (gateway, unbound, moved):
        assert snap.source == "unknown" and snap.primary_used_percent is None


def test_301_r2_malformed_records_before_a_valid_identity_are_skipped(tmp_path: Path) -> None:
    meta = {"type": "session_meta", "payload": {"id": "TARGET"}}
    p = tmp_path / "2026" / "10" / "07" / "rollout-renamed.jsonl"
    p.parent.mkdir(parents=True)
    p.write_text("\n".join([
        '["session_meta"]', '"session_meta"', "42", '{"type": "session_meta", "payload": "x"}',
        json.dumps(meta), json.dumps(_token_count(_CODEX_RL)),
    ]) + "\n", encoding="utf-8")

    snap = cap.read_codex_rollout("codex", sessions_dir=tmp_path, thread_id="TARGET")
    assert snap is not None and snap.primary_used_percent == 12.0
