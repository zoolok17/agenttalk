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
- Codex: newest ``$CODEX_HOME/sessions/**/rollout-*.jsonl`` record (falling
  back to ``~/.codex/sessions`` when the caller has not supplied an isolated
  home) whose
  ``payload.rate_limits`` has ``{primary,secondary}.{used_percent,window_minutes,
  resets_at}`` plus ``plan_type``/``limit_id``/``rate_limit_reached_type``.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from heapq import heappop, heappush
from pathlib import Path

# Provider window lengths in minutes. Codex reports them; Claude omits them, so
# we fill the conventional 5-hour / 7-day values.
FIVE_HOUR_MINUTES = 300
WEEKLY_MINUTES = 10080
DEFAULT_STALE_AFTER_SECONDS = 600.0
CODEX_ROLLOUT_MAX_FILES = 8
CODEX_ROLLOUT_SCAN_LIMIT = 256


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _epoch_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")


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
    observed_at: str                       # ISO-8601 Z — provider/source observation time
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
    # Context-window headroom: how full THIS agent's conversation context is —
    # the thing that triggers (auto)compaction (distinct from the rate-limit
    # budget above). ``context_used_percent`` is 0–100; ``context_tokens`` is the
    # current occupancy. A lead steers long/heavy work away from agents near
    # compaction. Advisory, same observed_at/confidence as the budget fields.
    context_used_percent: float | None = None
    context_window_size: int | None = None
    context_tokens: int | None = None
    confidence: str = "observed"           # observed | stale | unknown
    reason: str | None = None              # advisory reason for unknown snapshots

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
    def unknown(cls, source_agent: str, *, reason: str | None = None) -> "CapacitySnapshot":
        return cls(
            source_agent=source_agent, observed_at=_now_iso(), source="unknown",
            primary_used_percent=None, primary_resets_at=None, primary_window_minutes=None,
            secondary_used_percent=None, secondary_resets_at=None,
            secondary_window_minutes=None, confidence="unknown", reason=reason,
        )


def read_claude_statusline(
    source_agent: str, *, path: str | os.PathLike | None = None,
) -> CapacitySnapshot | None:
    """Parse the Claude Code status-line dump. None if absent/unreadable/empty."""
    p = Path(path) if path is not None else Path.home() / ".claude" / "statusline-last-input.json"
    try:
        raw = p.read_text(encoding="utf-8")
        observed = _epoch_iso(p.stat().st_mtime)
        data = json.loads(raw)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    # Budget (rate_limits) and context (context_window) are independent blocks;
    # a snapshot publishes from EITHER, so an absent rate_limits must not discard
    # present context data (and vice-versa).
    rl = data.get("rate_limits") if isinstance(data.get("rate_limits"), dict) else {}
    five = rl.get("five_hour") if isinstance(rl.get("five_hour"), dict) else {}
    week = rl.get("seven_day") if isinstance(rl.get("seven_day"), dict) else {}
    ctx_pct, ctx_size, ctx_tokens = _claude_context(data.get("context_window"))
    has_budget = not (five.get("used_percentage") is None and week.get("used_percentage") is None)
    if not has_budget and ctx_pct is None:
        return None  # neither budget nor context data present yet
    return CapacitySnapshot(
        source_agent=source_agent, observed_at=observed, source="claude_statusline",
        primary_used_percent=_as_float(five.get("used_percentage")),
        primary_resets_at=_as_int(five.get("resets_at")),
        primary_window_minutes=FIVE_HOUR_MINUTES,
        secondary_used_percent=_as_float(week.get("used_percentage")),
        secondary_resets_at=_as_int(week.get("resets_at")),
        secondary_window_minutes=WEEKLY_MINUTES,
        context_used_percent=ctx_pct,
        context_window_size=ctx_size,
        context_tokens=ctx_tokens,
        confidence="observed",
    )


def _claude_context(cw: object) -> tuple[float | None, int | None, int | None]:
    """Pull (used_percent, window_size, tokens) from the status-line
    ``context_window`` block. ``used_percentage`` is given directly; the token
    occupancy is the input side of ``current_usage`` (which is null right after a
    compact, until the next API call). Any piece may be None."""
    if not isinstance(cw, dict):
        return None, None, None
    pct = _as_float(cw.get("used_percentage"))
    size = _as_int(cw.get("context_window_size"))
    usage = cw.get("current_usage") if isinstance(cw.get("current_usage"), dict) else {}
    parts = [
        _as_int(usage.get("input_tokens")),
        _as_int(usage.get("cache_read_input_tokens")),
        _as_int(usage.get("cache_creation_input_tokens")),
    ]
    tokens = sum(p for p in parts if p is not None) if any(p is not None for p in parts) else None
    return pct, size, tokens


def _normalize_ts(ts: object) -> str | None:
    """Normalize a provider timestamp to UTC ISO-Z seconds, or None."""
    if not isinstance(ts, str) or not ts:
        return None
    norm = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
    # Python 3.10's fromisoformat only accepts 3- or 6-digit fractional seconds,
    # but providers emit variable precision (e.g. "...:00.0Z"); pad/truncate the
    # fraction to 6 digits so any precision parses on every supported version.
    norm = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], norm, count=1)
    try:
        dt = datetime.fromisoformat(norm)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _last_capacity_record(path: Path) -> dict | None:
    """Stream a rollout JSONL and return the LAST record carrying budget
    (``payload.rate_limits``) AND/OR context (``payload.info`` with
    ``model_context_window``) — the whole record, so the caller can read its
    ``timestamp``. Eligibility is decoupled from ``rate_limits`` so a
    context-only record still publishes. Only JSON-parses lines containing a
    marker, so a multi-MB session file stays cheap to scan.
    """
    last: dict | None = None
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                if '"rate_limits"' not in line and '"model_context_window"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                payload = rec.get("payload") if isinstance(rec, dict) else None
                if not isinstance(payload, dict):
                    continue
                has_budget = isinstance(payload.get("rate_limits"), dict)
                info = payload.get("info")
                has_context = isinstance(info, dict) and "model_context_window" in info
                if has_budget or has_context:
                    last = rec
    except OSError:
        return None
    return last


def _file_contains(path: Path, needle: str) -> bool:
    """True if ``needle`` appears anywhere in the file (streamed, stops early)."""
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                if needle in line:
                    return True
    except OSError:
        return False
    return False


def _pick_windows(prim: object, sec: object) -> tuple[dict, dict]:
    """Map the two rate-limit windows to (5-hour, weekly) by ``window_minutes``
    when present (robust to position), else fall back to primary=5h/secondary=
    weekly position order (Codex's verified default)."""
    cands = [w for w in (prim, sec) if isinstance(w, dict)]
    five = next((w for w in cands if w.get("window_minutes") == FIVE_HOUR_MINUTES), None)
    week = next((w for w in cands if w.get("window_minutes") == WEEKLY_MINUTES), None)
    if five is None and week is None:  # window_minutes absent — position fallback
        return (prim if isinstance(prim, dict) else {}), (sec if isinstance(sec, dict) else {})
    return five or {}, week or {}


def _codex_context(info: object) -> tuple[float | None, int | None, int | None]:
    """Pull (used_percent, window_size, tokens) from a token_count ``info`` block.
    Codex re-sends the full context each turn, so ``last_token_usage.input_tokens``
    is the current window occupancy; percent = tokens / model_context_window.
    NOT ``total_token_usage`` (that's cumulative across the session). Any piece
    may be None."""
    if not isinstance(info, dict):
        return None, None, None
    size = _as_int(info.get("model_context_window"))
    last = info.get("last_token_usage") if isinstance(info.get("last_token_usage"), dict) else {}
    tokens = _as_int(last.get("input_tokens"))
    pct = round(tokens / size * 100, 1) if tokens is not None and size and size > 0 else None
    return pct, size, tokens


def _codex_snapshot(source_agent: str, rec: dict) -> CapacitySnapshot | None:
    payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
    rl = payload.get("rate_limits") if isinstance(payload.get("rate_limits"), dict) else {}
    five, week = _pick_windows(rl.get("primary"), rl.get("secondary"))
    ctx_pct, ctx_size, ctx_tokens = _codex_context(payload.get("info"))
    has_budget = not (five.get("used_percent") is None and week.get("used_percent") is None)
    if not has_budget and ctx_pct is None:
        return None
    # observed_at = when CODEX took the reading (the record timestamp), not when
    # WE read the file — so staleness reflects the agent's real last activity.
    # If the timestamp is missing/malformed, do NOT fabricate a fresh time (that
    # would hide staleness); skip this record so the caller falls back to an
    # older record/file or an 'unknown' snapshot (review nit, Codex).
    observed = _normalize_ts(rec.get("timestamp"))
    if observed is None:
        return None
    return CapacitySnapshot(
        source_agent=source_agent, observed_at=observed, source="codex_rollout",
        primary_used_percent=_as_float(five.get("used_percent")),
        primary_resets_at=_as_int(five.get("resets_at")),
        primary_window_minutes=_as_int(five.get("window_minutes")) or FIVE_HOUR_MINUTES,
        secondary_used_percent=_as_float(week.get("used_percent")),
        secondary_resets_at=_as_int(week.get("resets_at")),
        secondary_window_minutes=_as_int(week.get("window_minutes")) or WEEKLY_MINUTES,
        plan_type=_as_str(rl.get("plan_type")),
        limit_id=_as_str(rl.get("limit_id")),
        rate_limit_reached_type=_as_str(rl.get("rate_limit_reached_type")),
        context_used_percent=ctx_pct,
        context_window_size=ctx_size,
        context_tokens=ctx_tokens,
        confidence="observed",
    )


def _newest_codex_rollouts(
    root: Path, *, max_files: int, max_scan_entries: int,
) -> tuple[list[Path], bool]:
    """Return rollout files ordered by file mtime.

    The bool is False when the scan budget was exhausted before traversal
    completed; callers must then fail closed instead of publishing a possibly
    stale observed value.
    """
    if max_files <= 0 or max_scan_entries <= 0:
        return [], False
    dirs: list[tuple[float, str, Path]] = []
    files: list[tuple[float, str, Path]] = []

    def push_dir(path: Path) -> None:
        try:
            heappush(dirs, (-path.stat().st_mtime, str(path), path))
        except OSError:
            return

    def keep_file(path: Path) -> None:
        try:
            item = (path.stat().st_mtime, str(path), path)
        except OSError:
            return
        if len(files) < max_files:
            heappush(files, item)
        elif item > files[0]:
            heappop(files)
            heappush(files, item)

    push_dir(root)
    scanned = 0
    complete = True
    while dirs:
        _mtime, _name, path = heappop(dirs)
        try:
            for child in path.iterdir():
                if scanned >= max_scan_entries:
                    complete = False
                    break
                scanned += 1
                try:
                    if child.is_dir():
                        push_dir(child)
                    elif child.name.startswith("rollout-") and child.name.endswith(".jsonl"):
                        keep_file(child)
                except OSError:
                    continue
        except OSError:
            continue
        if not complete:
            break
    newest = [p for _mtime, _name, p in sorted(files, reverse=True)]
    return newest, complete


def read_codex_rollout(
    source_agent: str, *, sessions_dir: str | os.PathLike | None = None,
    thread_id: str | None = None, max_files: int = CODEX_ROLLOUT_MAX_FILES,
    max_scan_entries: int = CODEX_ROLLOUT_SCAN_LIMIT,
) -> CapacitySnapshot | None:
    """Parse the CURRENT Codex session's rollout for its rate-limit budget.

    Selection (per Codex's contract): if ``CODEX_THREAD_ID`` is set, prefer
    rollout files matching that thread id (by filename, then by content), newest
    file mtime first — this avoids picking a resumed/forked sibling. Falls back
    to the newest rollout overall only when no thread id is set. Within the
    chosen file, takes the LAST record carrying budget and/or context data.
    Candidate discovery is bounded because wrapper refresh calls this
    synchronously; an incomplete scan fails closed to None/unknown.
    """
    root = _codex_sessions_root(sessions_dir)
    if not root.is_dir():
        return None
    rollouts, complete = _newest_codex_rollouts(
        root, max_files=max_files, max_scan_entries=max_scan_entries)
    if not complete:
        return None
    if not rollouts:
        return None
    tid = thread_id if thread_id is not None else os.environ.get("CODEX_THREAD_ID")
    candidates = rollouts
    if tid:
        by_name = [f for f in rollouts if tid in f.name]
        if by_name:
            candidates = by_name
        else:
            by_content = [f for f in rollouts[:max_files] if _file_contains(f, tid)]
            if by_content:
                candidates = by_content
            else:
                return None
    for f in candidates[:max_files]:
        rec = _last_capacity_record(f)
        if rec is not None:
            snap = _codex_snapshot(source_agent, rec)
            if snap is not None:
                return snap
    return None


def _codex_sessions_root(sessions_dir: str | os.PathLike | None = None) -> Path:
    if sessions_dir is not None:
        return Path(sessions_dir)
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        return Path(codex_home) / "sessions"
    return Path.home() / ".codex" / "sessions"


def read_local(
    source_agent: str, *, source: str = "auto",
    statusline_path: str | os.PathLike | None = None,
    sessions_dir: str | os.PathLike | None = None,
    thread_id: str | None = None,
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
            codex_root = _codex_sessions_root(sessions_dir)
            src = "codex" if codex_root.is_dir() else "unknown"
    snap: CapacitySnapshot | None = None
    if src == "claude":
        snap = read_claude_statusline(source_agent, path=statusline_path)
    elif src == "codex":
        snap = read_codex_rollout(source_agent, sessions_dir=sessions_dir, thread_id=thread_id)
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
