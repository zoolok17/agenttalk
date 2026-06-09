"""Advisory capacity (rate-limit budget) snapshots for budget-aware coordination.

Both Claude Code and Codex expose their own 5-hour + weekly rate-limit status
(percent-used + reset time) in local files. This module reads the LOCAL agent's
signal and normalizes it to a :class:`CapacitySnapshot` that agents publish to
the bus (``Store.write_capacity``) so a lead can factor remaining budget into
how it organizes work.

STRICTLY ADVISORY and best-effort: it is percent + reset time (NOT exact
tokens), plan-specific (Pro/Max), lags ~1 turn behind real usage, and degrades
to ``confidence="unknown"`` when absent/unreadable. It must NEVER gate protocol
progress.

Privacy: only DERIVED budget metadata is emitted — never account ids, auth
paths, token bodies, file paths, prompts, or session contents.

Sources (verified 2026-06-09):
- Claude Code: ``~/.claude/statusline-last-input.json`` →
  ``rate_limits.{five_hour,seven_day}.{used_percentage,resets_at}``.
- Codex: newest ``~/.codex/sessions/**/rollout-*.jsonl`` record whose
  ``payload.rate_limits`` has ``{primary,secondary}.{used_percent,window_minutes,
  resets_at}`` plus ``plan_type``/``limit_id``/``rate_limit_reached_type``.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

# Provider window lengths in minutes. Codex reports them; Claude omits them, so
# we fill the conventional 5-hour / 7-day values.
FIVE_HOUR_MINUTES = 300
WEEKLY_MINUTES = 10080
DEFAULT_STALE_AFTER_SECONDS = 600.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _as_float(v: object) -> float | None:
    if isinstance(v, bool):
        return None
    return float(v) if isinstance(v, (int, float)) else None


def _as_int(v: object) -> int | None:
    if isinstance(v, bool):
        return None
    return int(v) if isinstance(v, (int, float)) else None


def _as_str(v: object) -> str | None:
    return v if isinstance(v, str) and v else None


@dataclass
class CapacitySnapshot:
    """Normalized, privacy-safe budget snapshot for one agent.

    ``primary`` = the 5-hour rolling window; ``secondary`` = the weekly window.
    Percentages are 0–100 *used*; ``*_resets_at`` are unix epoch seconds.
    """

    source_agent: str
    observed_at: str                       # ISO-8601 Z — when WE read the source
    source: str                            # claude_statusline | codex_rollout | unknown
    primary_used_percent: float | None
    primary_resets_at: int | None
    primary_window_minutes: int | None
    secondary_used_percent: float | None
    secondary_resets_at: int | None
    secondary_window_minutes: int | None
    plan_type: str | None = None
    limit_id: str | None = None
    rate_limit_reached_type: str | None = None
    confidence: str = "observed"           # observed | stale | unknown

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: object) -> "CapacitySnapshot | None":
        if not isinstance(d, dict):
            return None
        try:
            return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})
        except TypeError:
            return None  # missing a required field — treat as unparseable

    @classmethod
    def unknown(cls, source_agent: str) -> "CapacitySnapshot":
        return cls(
            source_agent=source_agent, observed_at=_now_iso(), source="unknown",
            primary_used_percent=None, primary_resets_at=None, primary_window_minutes=None,
            secondary_used_percent=None, secondary_resets_at=None,
            secondary_window_minutes=None, confidence="unknown",
        )


def read_claude_statusline(
    source_agent: str, *, path: str | os.PathLike | None = None,
) -> CapacitySnapshot | None:
    """Parse the Claude Code status-line dump. None if absent/unreadable/empty."""
    p = Path(path) if path is not None else Path.home() / ".claude" / "statusline-last-input.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    rl = data.get("rate_limits") if isinstance(data, dict) else None
    if not isinstance(rl, dict):
        return None
    five = rl.get("five_hour") if isinstance(rl.get("five_hour"), dict) else {}
    week = rl.get("seven_day") if isinstance(rl.get("seven_day"), dict) else {}
    if five.get("used_percentage") is None and week.get("used_percentage") is None:
        return None  # rate_limits present but carries no window data yet
    return CapacitySnapshot(
        source_agent=source_agent, observed_at=_now_iso(), source="claude_statusline",
        primary_used_percent=_as_float(five.get("used_percentage")),
        primary_resets_at=_as_int(five.get("resets_at")),
        primary_window_minutes=FIVE_HOUR_MINUTES,
        secondary_used_percent=_as_float(week.get("used_percentage")),
        secondary_resets_at=_as_int(week.get("resets_at")),
        secondary_window_minutes=WEEKLY_MINUTES,
        confidence="observed",
    )


def _last_rate_limits_in(path: Path) -> dict | None:
    """Stream a rollout JSONL and return the LAST ``payload.rate_limits`` dict.

    Streams line-by-line and only JSON-parses lines containing the marker, so a
    multi-MB session file stays cheap to scan.
    """
    last: dict | None = None
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                if '"rate_limits"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                payload = rec.get("payload") if isinstance(rec, dict) else None
                rl = payload.get("rate_limits") if isinstance(payload, dict) else None
                if isinstance(rl, dict):
                    last = rl
    except OSError:
        return None
    return last


def _codex_snapshot(source_agent: str, rl: dict) -> CapacitySnapshot | None:
    prim = rl.get("primary") if isinstance(rl.get("primary"), dict) else {}
    sec = rl.get("secondary") if isinstance(rl.get("secondary"), dict) else {}
    if prim.get("used_percent") is None and sec.get("used_percent") is None:
        return None
    return CapacitySnapshot(
        source_agent=source_agent, observed_at=_now_iso(), source="codex_rollout",
        primary_used_percent=_as_float(prim.get("used_percent")),
        primary_resets_at=_as_int(prim.get("resets_at")),
        primary_window_minutes=_as_int(prim.get("window_minutes")) or FIVE_HOUR_MINUTES,
        secondary_used_percent=_as_float(sec.get("used_percent")),
        secondary_resets_at=_as_int(sec.get("resets_at")),
        secondary_window_minutes=_as_int(sec.get("window_minutes")) or WEEKLY_MINUTES,
        plan_type=_as_str(rl.get("plan_type")),
        limit_id=_as_str(rl.get("limit_id")),
        rate_limit_reached_type=_as_str(rl.get("rate_limit_reached_type")),
        confidence="observed",
    )


def read_codex_rollout(
    source_agent: str, *, sessions_dir: str | os.PathLike | None = None,
    max_files: int = 8,
) -> CapacitySnapshot | None:
    """Parse the newest Codex rollout that carries rate-limit data.

    Scans up to ``max_files`` most-recently-modified ``rollout-*.jsonl`` files
    (the active session appends to its rollout, so newest-mtime is the current
    one) and returns the last rate-limit record found.
    """
    root = Path(sessions_dir) if sessions_dir is not None else Path.home() / ".codex" / "sessions"
    if not root.is_dir():
        return None
    try:
        rollouts = sorted(
            root.rglob("rollout-*.jsonl"),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )
    except OSError:
        return None
    for f in rollouts[:max_files]:
        rl = _last_rate_limits_in(f)
        if rl is not None:
            snap = _codex_snapshot(source_agent, rl)
            if snap is not None:
                return snap
    return None


def read_local(
    source_agent: str, *, source: str = "auto",
    statusline_path: str | os.PathLike | None = None,
    sessions_dir: str | os.PathLike | None = None,
) -> CapacitySnapshot:
    """Read THIS agent's budget snapshot, auto-detecting the runtime.

    Never returns None: an undetectable / unreadable source yields an
    ``unknown`` snapshot so callers always get something publishable.
    """
    src = source
    if src == "auto":
        if os.environ.get("CLAUDECODE"):
            src = "claude"
        else:
            codex_root = Path(sessions_dir) if sessions_dir is not None else Path.home() / ".codex" / "sessions"
            src = "codex" if codex_root.is_dir() else "unknown"
    snap: CapacitySnapshot | None = None
    if src == "claude":
        snap = read_claude_statusline(source_agent, path=statusline_path)
    elif src == "codex":
        snap = read_codex_rollout(source_agent, sessions_dir=sessions_dir)
    return snap or CapacitySnapshot.unknown(source_agent)


def age_seconds(observed_at: str, *, now: datetime | None = None) -> float | None:
    """Seconds since ``observed_at`` (ISO-Z), or None if unparseable."""
    if not isinstance(observed_at, str) or not observed_at:
        return None
    normalized = observed_at[:-1] + "+00:00" if observed_at.endswith("Z") else observed_at
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    now = now or datetime.now(timezone.utc)
    return (now - dt).total_seconds()


def effective_confidence(
    snap: dict, *, now: datetime | None = None,
    stale_after: float = DEFAULT_STALE_AFTER_SECONDS,
) -> str:
    """Confidence a READER should trust: downgrade ``observed`` to ``stale``
    once the snapshot is older than ``stale_after`` (clock skew → ``observed``
    is kept). ``unknown`` and a missing/garbage observed_at stay/flip to their
    safe value."""
    base = snap.get("confidence") if isinstance(snap, dict) else None
    if base == "unknown":
        return "unknown"
    age = age_seconds(snap.get("observed_at", "") if isinstance(snap, dict) else "", now=now)
    if age is None:
        return "unknown"
    if age > stale_after:
        return "stale"
    return base if base in ("observed", "stale") else "observed"
