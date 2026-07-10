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
