"""Advisory wrapper-health snapshots.

The activity heartbeat remains the liveness authority. This module only defines
the adjacent ``state/<agent>.health.json`` schema and the degrade-safe reader
used by status/report/supervisor views.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = 1

STATE_IDLE_WAITING = "idle_waiting"
STATE_WORKING_TURN = "working_turn"
STATE_WORKING_SILENT = "working_silent"
STATE_STUCK_SUSPECTED = "stuck_suspected"
STATE_RATE_LIMITED_OR_OUTAGE = "rate_limited_or_outage"
STATE_DEGRADED_OUTPUT = "degraded_output"
STATE_ERRORED_POISON = "errored_poison"
STATE_ERRORED_AMBIGUOUS = "errored_ambiguous"
STATE_CRASHED_OR_EXITED = "crashed_or_exited"
STATE_UNKNOWN = "unknown"

HEALTH_STATES = frozenset(
    {
        STATE_IDLE_WAITING,
        STATE_WORKING_TURN,
        STATE_WORKING_SILENT,
        STATE_STUCK_SUSPECTED,
        STATE_RATE_LIMITED_OR_OUTAGE,
        STATE_DEGRADED_OUTPUT,
        STATE_ERRORED_POISON,
        STATE_ERRORED_AMBIGUOUS,
        STATE_CRASHED_OR_EXITED,
        STATE_UNKNOWN,
    }
)

DEFAULT_TTL_SECONDS = 300.0
DEFAULT_HEARTBEAT_SKEW_SECONDS = 30.0

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,96}$")

SCHEMA_KEYS = (
    "schema_version",
    "agent",
    "cli",
    "mode",
    "state",
    "updated_at",
    "since",
    "last_progress_at",
    "reason_code",
    "source",
    "warnings",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt


def _safe_id(value: Any) -> str | None:
    if isinstance(value, str) and _SAFE_ID_RE.fullmatch(value):
        return value
    return None


def _safe_token(value: Any) -> str | None:
    if isinstance(value, str) and _SAFE_TOKEN_RE.fullmatch(value):
        return value
    return None


def _safe_warnings(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    out: list[str] = []
    for value in values:
        token = _safe_token(value)
        if token is not None:
            out.append(token)
    return out


def build_snapshot(
    *,
    agent: str,
    cli: str | None,
    mode: str | None,
    state: str,
    updated_at: str | None = None,
    since: str | None = None,
    last_progress_at: str | None = None,
    request_id: str | None = None,
    msg_id: str | None = None,
    reason_code: str | None = None,
    source: str = "wrapper",
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Build the on-disk schema, omitting unsafe optional ids.

    The snapshot deliberately has no free-form text fields: no message body,
    model output, prompt, tool command, or tool output can be represented here.
    """
    now = updated_at or now_iso()
    clean_state = state if state in HEALTH_STATES else STATE_UNKNOWN
    snap: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "agent": agent,
        "cli": _safe_token(cli),
        "mode": _safe_token(mode),
        "state": clean_state,
        "updated_at": now,
        "since": since or now,
        "last_progress_at": last_progress_at,
        "reason_code": _safe_token(reason_code),
        "source": _safe_token(source) or "wrapper",
        "warnings": _safe_warnings(warnings or []),
    }
    rid = _safe_id(request_id)
    if rid is not None:
        snap["request_id"] = rid
    mid = _safe_id(msg_id)
    if mid is not None:
        snap["msg_id"] = mid
    return snap


def unknown(agent: str, warning: str) -> dict[str, Any]:
    snap = build_snapshot(
        agent=agent,
        cli=None,
        mode=None,
        state=STATE_UNKNOWN,
        updated_at=None,
        since=None,
        last_progress_at=None,
        reason_code=warning,
        source="health-reader",
        warnings=[warning],
    )
    snap["updated_at"] = None
    snap["since"] = None
    snap["age_seconds"] = None
    snap["stale"] = True
    snap["advisory"] = True
    return snap


def normalize(
    raw: Any,
    *,
    agent: str,
    now_epoch: float | None = None,
    heartbeat: datetime | None = None,
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
    heartbeat_skew_seconds: float = DEFAULT_HEARTBEAT_SKEW_SECONDS,
) -> dict[str, Any]:
    """Validate and freshness-check a raw health snapshot.

    Missing, malformed, stale, or torn files degrade to ``state=unknown`` with a
    local warning. They never raise and never infer liveness from health alone.
    """
    if raw is None:
        return unknown(agent, "health_missing")
    if not isinstance(raw, dict):
        return unknown(agent, "health_invalid")
    if raw.get("schema_version") != SCHEMA_VERSION or raw.get("agent") != agent:
        return unknown(agent, "health_invalid")
    state = raw.get("state")
    updated_at = parse_iso(raw.get("updated_at"))
    since = parse_iso(raw.get("since"))
    if state not in HEALTH_STATES or updated_at is None or since is None:
        return unknown(agent, "health_invalid")
    last_progress = raw.get("last_progress_at")
    if last_progress is not None and parse_iso(last_progress) is None:
        return unknown(agent, "health_invalid")
    now = time.time() if now_epoch is None else float(now_epoch)
    age = max(0.0, now - updated_at.timestamp())
    ttl = float(ttl_seconds)
    skew = float(heartbeat_skew_seconds)
    if ttl >= 0 and age > ttl:
        return unknown(agent, "health_stale_ttl")
    if heartbeat is not None and updated_at.timestamp() < heartbeat.timestamp() - skew:
        return unknown(agent, "health_older_than_heartbeat")

    out = build_snapshot(
        agent=agent,
        cli=raw.get("cli"),
        mode=raw.get("mode"),
        state=state,
        updated_at=raw.get("updated_at"),
        since=raw.get("since"),
        last_progress_at=raw.get("last_progress_at"),
        request_id=raw.get("request_id"),
        msg_id=raw.get("msg_id"),
        reason_code=raw.get("reason_code"),
        source=raw.get("source") or "wrapper",
        warnings=_safe_warnings(raw.get("warnings")),
    )
    out["age_seconds"] = round(age, 3)
    out["stale"] = False
    out["advisory"] = True
    return out


def label(view: dict[str, Any] | None) -> str:
    if not isinstance(view, dict):
        return STATE_UNKNOWN
    state = view.get("state")
    return state if isinstance(state, str) and state in HEALTH_STATES else STATE_UNKNOWN
