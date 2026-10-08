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
paths, token bodies, file paths, prompts, or session contents. ``account`` is
provider + OS user, plus a short hash (never the path) of a non-default home.

Sources (verified 2026-06-09):
- Claude Code, the seat's own stream (#301, preferred): ``rate_limit_event`` →
  ``rate_limit_info`` (see :func:`claude_stream_reading`).
- Claude Code: ``~/.claude/statusline-last-input.json`` →
  ``rate_limits.{five_hour,seven_day}.{used_percentage,resets_at}``.
- Claude session context: ``%TEMP%/cc-ctx-<session_id>.json`` emitted by the
  status line, which alone sees the authoritative model context limit.
- Codex: newest ``$CODEX_HOME/sessions/**/rollout-*.jsonl`` record (falling
  back to ``~/.codex/sessions`` when the caller has not supplied an isolated
  home) whose
  ``payload.rate_limits`` has ``{primary,secondary}.{used_percent,window_minutes,
  resets_at}`` plus ``plan_type``/``limit_id``/``rate_limit_reached_type``.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import math
import os
import re
import stat
import tempfile
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from heapq import heappop, heappush
from pathlib import Path

from agenttalk._jsonl import iter_lines

# Provider window lengths in minutes. Codex reports them; Claude omits them, so
# we fill the conventional 5-hour / 7-day values.
FIVE_HOUR_MINUTES = 300
WEEKLY_MINUTES = 10080
DEFAULT_STALE_AFTER_SECONDS = 600.0
CODEX_ROLLOUT_MAX_FILES = 8
CODEX_ROLLOUT_SCAN_LIMIT = 256
CODEX_SHARED_SCAN_LIMIT = 4096  # the shared ~/.codex: every session of the OS user
# Layout version of the capacity file. A file without the field predates #301.
CAPACITY_SCHEMA_VERSION = 2
_CLAUDE_WINDOWS = {"five_hour": "primary", "seven_day": "secondary"}
_WINDOW_MINUTES = {"primary": FIVE_HOUR_MINUTES, "secondary": WEEKLY_MINUTES}
CLAUDE_CONTEXT_SIDECAR_MAX_BYTES = 64 * 1024
_CLAUDE_SESSION_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,200}")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _epoch_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")


def _as_float(v: object) -> float | None:
    """A finite number, else None: an advisory reading never carries NaN or infinity."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        f = float(v)
    except OverflowError:  # an int too large for a float
        return None
    return f if math.isfinite(f) else None


def _as_int(v: object) -> int | None:
    if isinstance(v, int) and not isinstance(v, bool):
        return v
    f = _as_float(v)
    return int(f) if f is not None else None


_LATEST_EPOCH = 253402300799  # 9999-12-31T23:59:59Z, the last second a date can show


def usable_epoch(v: object) -> int | None:
    """A reset time a reader can use: whole seconds since 1970, from 0 up to the last second a
    date can show. Anything else (a huge or negative number, NaN, a boolean) is no reset time."""
    n = _as_int(v)
    return n if n is not None and 0 <= n <= _LATEST_EPOCH else None


def _as_exact_int(v: object) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and math.isfinite(v) and v.is_integer():
        return int(v)
    return None


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
    # #301: per window, the provider's verdict (allowed | allowed_warning | rejected)
    # and whether its length was reported ("measured") or filled in ("assumed").
    primary_status: str | None = None
    primary_window_basis: str | None = None
    secondary_status: str | None = None
    secondary_window_basis: str | None = None
    # When each window's figures were observed (the seat's own stream can differ
    # per window), and the verdict on the latest request, which may name no window.
    primary_observed_at: str | None = None
    secondary_observed_at: str | None = None
    last_status: str | None = None
    last_status_at: str | None = None
    last_status_window: str | None = None  # the window that verdict named, if any
    # The 5h/weekly figures belong to the account named here, never to one seat;
    # the context_* fields are this seat's own.
    scope: str = "account"
    account: str | None = None
    schema_version: int = CAPACITY_SCHEMA_VERSION

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
    def unknown(cls, source_agent: str, *, reason: str | None = None,
                account: str | None = None) -> "CapacitySnapshot":
        return cls(
            source_agent=source_agent, observed_at=_now_iso(), source="unknown",
            primary_used_percent=None, primary_resets_at=None, primary_window_minutes=None,
            secondary_used_percent=None, secondary_resets_at=None,
            secondary_window_minutes=None, confidence="unknown", reason=reason,
            account=account,
        )


def account_key(provider: str, home: str | os.PathLike) -> str:
    """Provider + OS user + a short hash of the folder the reading comes from (never the
    path itself). Every folder may hold its own login, the default one too: two seats of
    one OS user with different user homes have two different default folders."""
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 - no user name must not stop a reading
        user = "unknown-user"
    norm = os.path.normcase(os.path.realpath(home))
    return f"{provider}:{user}:home-{hashlib.sha256(norm.encode('utf-8')).hexdigest()[:8]}"


def claude_stream_reading(
    info: object, previous: object = None, *, observed_at: str | None = None,
) -> dict:
    """Fold one ``rate_limit_event``'s ``rate_limit_info`` into the seat's latest
    reading per window. The event has no time of its own: each window it names
    gets ``observed_at`` (default now, when the wrapper read the line). The
    top-level status/resetsAt/utilization describe the ``rateLimitType`` window;
    ``unifiedWindows`` give per-window figures. A percentage is recorded only
    when given (utilization 1.0 = full), never carried over; ``binding`` is.
    """
    old = previous if isinstance(previous, dict) else {}
    windows = dict(old["windows"]) if isinstance(old.get("windows"), dict) else {}
    reading: dict = {"windows": windows}
    reading.update({k: old[k] for k in ("last", "binding") if k in old})
    at = observed_at or _now_iso()
    if isinstance(info, dict):
        named = info.get("rateLimitType")
        if _as_str(info.get("status")):  # the real "allowed" event carries only this
            reading["last"] = {"status": info["status"], "window": _as_str(named), "observed_at": at}
        unified = info.get("unifiedWindows") if isinstance(info.get("unifiedWindows"), dict) else {}
        for name in _CLAUDE_WINDOWS:
            given = unified.get(name)
            if not isinstance(given, dict) and name != named:
                continue
            top = info if name == named else {}
            w = given if isinstance(given, dict) else {}
            util = _as_float(w.get("utilization", top.get("utilization")))
            windows[name] = {
                "status": _as_str(top.get("status")),
                "used_percent": round(util * 100, 1) if util is not None and math.isfinite(util) else None,
                "resets_at": usable_epoch(w.get("resetsAt", top.get("resetsAt"))),
                "observed_at": at,
            }
    return reading


def read_claude_stream(
    source_agent: str, reading: object, *, now: datetime | None = None,
) -> CapacitySnapshot | None:
    """Snapshot of the seat's own rate-limit events: each window and the latest
    verdict with its own time, ``observed_at`` the newest of them. Readers judge
    each part's age (:func:`current_view`). None when the seat has seen no event."""
    windows = reading.get("windows") if isinstance(reading, dict) else None
    parts = dict(windows) if isinstance(windows, dict) else {}
    parts["last"] = reading.get("last") if isinstance(reading, dict) else None
    seen = {name: w for name, w in parts.items()
            if name in (*_CLAUDE_WINDOWS, "last") and isinstance(w, dict)
            and age_seconds(w.get("observed_at", ""), now=now) is not None}
    if not seen:
        return None
    newest = min(seen.values(), key=lambda w: age_seconds(w["observed_at"], now=now))
    snap = replace(CapacitySnapshot.unknown(source_agent), source="claude_stream",
                   confidence="observed", observed_at=newest["observed_at"])
    last = seen.pop("last", None)
    if last is not None:
        snap.last_status, snap.last_status_at = _as_str(last.get("status")), last["observed_at"]
        snap.last_status_window = _as_str(last.get("window"))
    for name, w in seen.items():
        prefix = _CLAUDE_WINDOWS[name]
        setattr(snap, f"{prefix}_used_percent", _as_float(w.get("used_percent")))
        setattr(snap, f"{prefix}_resets_at", usable_epoch(w.get("resets_at")))
        setattr(snap, f"{prefix}_status", _as_str(w.get("status")))
        setattr(snap, f"{prefix}_window_minutes", _WINDOW_MINUTES[prefix])
        setattr(snap, f"{prefix}_window_basis", "assumed")
        setattr(snap, f"{prefix}_observed_at", w["observed_at"])
        if w.get("status") == "rejected":
            snap.rate_limit_reached_type = name
    if all(w.get("used_percent") is None for w in seen.values()):
        snap.reason = "no_figures_in_event"
    return snap


def read_claude_statusline(
    source_agent: str, *, path: str | os.PathLike | None = None,
    session_id: str | None = None,
) -> CapacitySnapshot | None:
    """Parse the Claude Code status-line dump. None if absent/unreadable/empty.
    With ``session_id``, the conversation fill is kept only when the dump names
    that session: the dump is per OS user and may come from any session."""
    p = Path(path) if path is not None else Path.home() / ".claude" / "statusline-last-input.json"
    try:
        # The time and the figures must come from the same version of the file:
        # read between two looks at it, and read again if it changed meanwhile.
        for _attempt in range(3):
            before = p.stat()
            raw = p.read_text(encoding="utf-8")
            after = p.stat()
            if (before.st_mtime_ns, before.st_size) == (after.st_mtime_ns, after.st_size):
                break
        else:
            return None
        observed = _epoch_iso(after.st_mtime)
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
    if session_id is not None and data.get("session_id") != session_id:
        ctx_pct = ctx_size = ctx_tokens = None
    has_budget = not (five.get("used_percentage") is None and week.get("used_percentage") is None)
    if not has_budget and ctx_pct is None:
        return None  # neither budget nor context data present yet
    return CapacitySnapshot(
        source_agent=source_agent, observed_at=observed, source="claude_statusline",
        primary_used_percent=_as_float(five.get("used_percentage")),
        primary_resets_at=usable_epoch(five.get("resets_at")),
        primary_window_minutes=FIVE_HOUR_MINUTES,
        secondary_used_percent=_as_float(week.get("used_percentage")),
        secondary_resets_at=usable_epoch(week.get("resets_at")),
        secondary_window_minutes=WEEKLY_MINUTES,
        context_used_percent=ctx_pct,
        context_window_size=ctx_size,
        context_tokens=ctx_tokens,
        confidence="observed",
        primary_window_basis="assumed",
        secondary_window_basis="assumed",
    )


def read_claude_context_sidecar(
    source_agent: str,
    *,
    session_id: str,
    temp_dir: str | os.PathLike | None = None,
) -> CapacitySnapshot | None:
    """Read the session-scoped context signal emitted by the status line.

    The status-line process alone sees the model's authoritative context limit,
    including 1M-tier model ids. It tees that derived value to
    ``%TEMP%/cc-ctx-<session>.json``. Keep the path derivation and schema parser
    here so checkpoint hooks and later wrapper integration share one authority.
    """
    if not isinstance(session_id, str) or _CLAUDE_SESSION_ID_RE.fullmatch(session_id) is None:
        return None
    base = Path(temp_dir) if temp_dir is not None else Path(tempfile.gettempdir())
    path = base / f"cc-ctx-{session_id}.json"
    descriptor = -1
    try:
        before = path.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size > CLAUDE_CONTEXT_SIDECAR_MAX_BYTES
        ):
            return None
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NONBLOCK", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_size > CLAUDE_CONTEXT_SIDECAR_MAX_BYTES
        ):
            return None
        chunks: list[bytes] = []
        remaining = CLAUDE_CONTEXT_SIDECAR_MAX_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        observed_mtime = opened.st_mtime
        if len(raw) > CLAUDE_CONTEXT_SIDECAR_MAX_BYTES:
            return None
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
    if not isinstance(payload, dict):
        return None
    pct = _as_float(payload.get("context_pct"))
    limit = _as_exact_int(payload.get("context_limit"))
    used = _as_exact_int(payload.get("context_used"))
    if (
        pct is None
        or not math.isfinite(pct)
        or not 0 <= pct <= 100
        or limit is None
        or limit <= 0
        or used is None
        or used < 0
    ):
        return None
    observed_at = _as_str(payload.get("updated_at")) or _epoch_iso(observed_mtime)
    return CapacitySnapshot(
        source_agent=source_agent,
        observed_at=observed_at,
        source="claude_context_sidecar",
        primary_used_percent=None,
        primary_resets_at=None,
        primary_window_minutes=None,
        secondary_used_percent=None,
        secondary_resets_at=None,
        secondary_window_minutes=None,
        context_used_percent=pct,
        context_window_size=limit,
        context_tokens=used,
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


def _last_capacity_snapshot(path: Path, source_agent: str) -> CapacitySnapshot | None:
    """Stream a rollout JSONL and retain the LAST usable capacity snapshot.

    Marker keys only make a record worth parsing. The semantic parser decides
    eligibility, so an empty or malformed newer candidate cannot hide an
    earlier usable budget or context snapshot in the same file.
    """
    last: CapacitySnapshot | None = None
    try:
        for _line_number, line in iter_lines(path):
            if line is None:
                continue
            if '"rate_limits"' not in line and '"model_context_window"' not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            snapshot = _codex_snapshot(source_agent, rec)
            if snapshot is not None:
                last = snapshot
    except OSError:
        return None
    return last


def _rollout_session_id(path: Path) -> str | None:
    """The session id a rollout declares in its ``session_meta`` record, never a
    mention of an id somewhere in another session's text."""
    try:
        for _line_number, line in iter_lines(path):
            if line is None or '"session_meta"' not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            payload = rec.get("payload")
            if rec.get("type") == "session_meta" and isinstance(payload, dict):
                return _as_str(payload.get("id"))
    except OSError:
        return None
    return None


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
    primary_used = _as_float(five.get("used_percent"))
    secondary_used = _as_float(week.get("used_percent"))
    has_budget = primary_used is not None or secondary_used is not None
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
        primary_used_percent=primary_used,
        primary_resets_at=usable_epoch(five.get("resets_at")),
        primary_window_minutes=_as_int(five.get("window_minutes")) or FIVE_HOUR_MINUTES,
        primary_window_basis="measured" if _as_int(five.get("window_minutes")) else "assumed",
        secondary_used_percent=secondary_used,
        secondary_resets_at=usable_epoch(week.get("resets_at")),
        secondary_window_minutes=_as_int(week.get("window_minutes")) or WEEKLY_MINUTES,
        secondary_window_basis="measured" if _as_int(week.get("window_minutes")) else "assumed",
        plan_type=_as_str(rl.get("plan_type")),
        limit_id=_as_str(rl.get("limit_id")),
        rate_limit_reached_type=_as_str(rl.get("rate_limit_reached_type")),
        context_used_percent=ctx_pct,
        context_window_size=ctx_size,
        context_tokens=ctx_tokens,
        confidence="observed",
    )


def _newest_codex_rollouts(
    root: Path, *, max_files: int, max_scan_entries: int, name_suffix: str | None = None,
) -> tuple[list[Path], bool]:
    """Return rollout files ordered by file mtime, only those whose name ends
    with ``name_suffix`` when it is given.

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
                    elif (child.name.startswith("rollout-") and child.name.endswith(".jsonl")
                          and (name_suffix is None or child.name.endswith(name_suffix))):
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
    tid = thread_id if thread_id is not None else os.environ.get("CODEX_THREAD_ID")
    # A thread's file is found by its name anywhere in the tree (in a shared home
    # it need not be among the newest), else by its declared session id among
    # the newest files.
    candidates, complete = _newest_codex_rollouts(
        root, max_files=max_files, max_scan_entries=max_scan_entries,
        name_suffix=f"-{tid}.jsonl" if tid else None)
    if complete and tid and not candidates:
        newest, complete = _newest_codex_rollouts(
            root, max_files=max_files, max_scan_entries=max_scan_entries)
        candidates = [f for f in newest if _rollout_session_id(f) == tid]
    if not complete or not candidates:
        return None
    for f in candidates[:max_files]:
        snapshot = _last_capacity_snapshot(f, source_agent)
        if snapshot is not None:
            return snapshot
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
    stream: object = None,
    now: datetime | None = None,
    claude_home: str | os.PathLike | None = None,
    provider: str = "claude",
    session_id: str | None = None,
    any_session_context: bool = False,
) -> CapacitySnapshot:
    """Read THIS agent's budget snapshot, auto-detecting the runtime.

    Never returns None: an undetectable / unreadable source yields an
    ``unknown`` snapshot so callers always get something publishable.
    Claude: the seat's own ``stream`` reading while current, else the newer of
    the two, read from one config folder (``claude_home``, else the status-line
    file's folder, ``$CLAUDE_CONFIG_DIR`` or ``~/.claude``) that also names the
    ``provider`` account. Codex: ``sessions_dir``, ``$CODEX_HOME/sessions`` or the
    shared home, where only the seat's own thread is ever read.
    The status line's conversation fill counts only for ``session_id``; with no id it is
    dropped, unless ``any_session_context`` (a manual checkpoint, as before #301).
    """
    src = detect_source(sessions_dir) if source == "auto" else source
    snap: CapacitySnapshot | None = None
    account = reason = None
    if src == "claude":
        account, home = claude_account(claude_home, provider=provider,
                                       statusline_path=statusline_path)
        reason = f"{provider}_source_not_configured"
        if not isinstance(stream, dict) or stream.get("binding") != account:
            stream = None  # taken under another provider or home: never relabelled
        snap = read_claude_stream(source_agent, stream, now=now)
        if snap is None or effective_confidence(snap.to_dict(), now=now) != "observed":
            line = read_claude_statusline(
                source_agent, path=statusline_path or home / "statusline-last-input.json",
                session_id=None if any_session_context and not session_id else session_id or "")
            if line is not None and (snap is None or age_seconds(line.observed_at, now=now)
                                     < age_seconds(snap.observed_at, now=now)):
                snap = line
    elif src == "codex":
        root = _codex_sessions_root(sessions_dir)
        shared = _same_path(root, Path.home() / ".codex" / "sessions")
        account = account_key("codex", Path.home() / ".codex" if shared else root.parent)
        tid = thread_id if thread_id is not None else os.environ.get("CODEX_THREAD_ID")
        reason = "codex_no_thread_yet" if shared and not tid else "codex_no_reading"
        if tid or not shared:  # the shared home holds every session: only the seat's own counts
            snap = read_codex_rollout(
                source_agent, sessions_dir=root, thread_id=tid or "",
                max_scan_entries=CODEX_SHARED_SCAN_LIMIT if shared else CODEX_ROLLOUT_SCAN_LIMIT)
    snap = snap or CapacitySnapshot.unknown(source_agent, reason=reason)
    snap.account = account
    return snap


def detect_source(sessions_dir: str | os.PathLike | None = None) -> str:
    """``auto``: Claude inside a Claude session, else Codex when a sessions folder exists."""
    if os.environ.get("CLAUDECODE"):
        return "claude"
    return "codex" if _codex_sessions_root(sessions_dir).is_dir() else "unknown"


def claude_account(
    claude_home: str | os.PathLike | None = None, *, provider: str = "claude",
    statusline_path: str | os.PathLike | None = None,
) -> tuple[str, Path]:
    """A Claude reading's one binding (account, config folder): ``claude_home``, else the
    status-line file's folder, ``$CLAUDE_CONFIG_DIR`` or ``~/.claude``."""
    if claude_home is None:
        claude_home = (Path(statusline_path).parent if statusline_path is not None
                       else os.environ.get("CLAUDE_CONFIG_DIR") or None)
    home = Path(claude_home) if claude_home else Path.home() / ".claude"
    return account_key(provider, home), home


def _same_path(a: str | os.PathLike, b: str | os.PathLike) -> bool:
    """The same folder, also when one name is a link (junction or symbolic link) to the other:
    a home that only points at the shared one is the shared one (#301)."""
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


# The parts of a reading, each with the field that holds its own time.
_PARTS = (
    ("primary_observed_at", ("primary_used_percent", "primary_resets_at", "primary_status")),
    ("secondary_observed_at", ("secondary_used_percent", "secondary_resets_at", "secondary_status")),
    ("last_status_at", ("last_status", "last_status_window")),
    ("observed_at", ("context_used_percent", "context_window_size", "context_tokens")),
)


def current_view(
    snap: object, *, now: datetime | None = None,
    stale_after: float = DEFAULT_STALE_AFTER_SECONDS,
) -> dict:
    """A snapshot as every reader may use it now (#301): a part (window, latest
    verdict, conversation fill) older than ``stale_after`` or undated loses its
    figures, keeping source and times; ``confidence`` is observed while any part is current."""
    view = dict(snap) if isinstance(snap, dict) else {}
    if view.get("confidence") == "unknown":
        return view
    seen = current = False
    live: dict[str, bool] = {}  # each part that had figures: still current?
    for time_key, keys in _PARTS:
        if all(view.get(k) is None for k in keys):
            continue
        seen = True
        at = view.get(time_key) or view.get("observed_at")
        age = age_seconds(at if isinstance(at, str) else "", now=now)
        live[time_key] = age is not None and age <= stale_after
        current = current or live[time_key]
        if not live[time_key]:
            view.update(dict.fromkeys(keys))
    # The refusal flag expires with the window it came from; a flag that names no
    # known window lasts only while every window that had figures is current.
    if not isinstance(view.get("rate_limit_reached_type"), str):
        view["rate_limit_reached_type"] = None  # a saved list or object names no window
    own = {"five_hour": "primary_observed_at", "seven_day": "secondary_observed_at"}.get(
        view.get("rate_limit_reached_type"))
    windows = [live[k] for k in ("primary_observed_at", "secondary_observed_at") if k in live]
    if not (live.get(own, False) if own else windows and all(windows)):
        view["rate_limit_reached_type"] = None
    view["confidence"] = (("observed" if current else "stale") if seen
                          else effective_confidence(view, now=now, stale_after=stale_after))
    if seen and not current:
        view["reason"] = f"{view.get('source')}_stale"
    return view


def for_publication(
    snap: CapacitySnapshot, *, now: datetime | None = None,
    stale_after: float = DEFAULT_STALE_AFTER_SECONDS,
) -> CapacitySnapshot:
    """The snapshot as written to the capacity file: what :func:`current_view`
    keeps of it now, so the file itself says stale plainly."""
    view = current_view(snap.to_dict(), now=now, stale_after=stale_after)
    return CapacitySnapshot.from_dict(view) or snap


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
