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
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return p


def _token_count(rl: dict) -> dict:
    return {"timestamp": "2026-06-09T08:00:00.0Z", "type": "event_msg",
            "payload": {"type": "token_count", "info": {}, "rate_limits": rl}}


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
