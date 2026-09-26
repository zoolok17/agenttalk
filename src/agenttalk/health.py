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
    agenttalk_version: str | None = None,
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
    # #163: the running package version, so a roster-wide gate (e.g.
    # `agenttalk task`'s write-time check) can tell whether a peer's build
    # recognizes a kind added after theirs, without spawning a subprocess
    # probe. Optional/additive like request_id/msg_id above — an older
    # writer's snapshot simply lacks the field, read as "unknown" by any
    # consumer, never a crash.
    av = _safe_token(agenttalk_version)
    if av is not None:
        snap["agenttalk_version"] = av
    return snap


# #164: the reason code a just-sent `agenttalk progress` note stamps onto the
# sender's OWN health snapshot — distinct from `progress_event` (adapter
# stream liveness, wrapper/events.py) so a reader can tell "the CLI adapter
# emitted a stream event" from "the seat deliberately told the lead
# something". No note TEXT is ever recorded here (see build_snapshot's own
# docstring) — the text lives only in the bus message itself.
REASON_PROGRESS_NOTE = "progress_note"


def stamp_progress(raw: Any, *, now: str | None = None) -> dict[str, Any] | None:
    """Return an updated snapshot recording a progress note, or None.

    Called directly by the (stateless, one-shot) ``agenttalk progress`` CLI
    process, not by a live ``WrapperHealthWriter`` — there is no wrapper
    instance to hold onto for a plain subprocess invocation. Reads the
    CURRENT on-disk snapshot's own state/since/cli/mode/ids and only
    ever ANNOTATES last_progress_at + reason_code; it never fabricates a
    working state and is a no-op (returns None) when the agent has no
    existing health record or that record is not currently a working one —
    a progress note posted with no turn in flight has nothing to annotate.
    """
    if not isinstance(raw, dict):
        return None
    state = raw.get("state")
    if state not in (STATE_WORKING_TURN, STATE_WORKING_SILENT):
        return None
    agent = raw.get("agent")
    if not isinstance(agent, str) or not agent:
        return None
    stamp = now or now_iso()
    return build_snapshot(
        agent=agent,
        cli=raw.get("cli"),
        mode=raw.get("mode"),
        state=state,
        updated_at=stamp,
        since=raw.get("since"),
        last_progress_at=stamp,
        request_id=raw.get("request_id"),
        msg_id=raw.get("msg_id"),
        reason_code=REASON_PROGRESS_NOTE,
        source=raw.get("source") or "wrapper",
        warnings=raw.get("warnings"),
    )


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


def _stale_unknown(agent: str, warning: str, raw: dict[str, Any], state: str) -> dict[str, Any]:
    """``unknown(...)`` for a snapshot that validated but is too old to trust, plus what it said.

    Every existing key and value is exactly what ``unknown()`` returns (state stays
    ``unknown``, ``stale`` True, times None). Only new keys are appended, so no existing
    consumer sees a different value. The ``last_known_*`` fields are the validated snapshot's
    own state and timestamps (no free text): a reader can tell "this agent last reported a
    silent turn, and has written nothing since" from "no health at all". They are NOT a
    current state and must never be used as one.
    """
    out = unknown(agent, warning)
    out["last_known_state"] = state
    out["last_known_since"] = raw.get("since")
    out["last_known_updated_at"] = raw.get("updated_at")
    if raw.get("last_progress_at") is not None:
        out["last_known_progress_at"] = raw.get("last_progress_at")
    return out


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
    last_progress_at = parse_iso(last_progress) if last_progress is not None else None
    if last_progress is not None and last_progress_at is None:
        return unknown(agent, "health_invalid")
    now = time.time() if now_epoch is None else float(now_epoch)
    ttl = float(ttl_seconds)
    skew = max(0.0, float(heartbeat_skew_seconds))
    future_cutoff = now + skew
    for dt in (updated_at, since, last_progress_at):
        if dt is not None and dt.timestamp() > future_cutoff:
            return unknown(agent, "health_future_timestamp")

    age = max(0.0, now - updated_at.timestamp())
    if ttl >= 0 and age > ttl:
        return _stale_unknown(agent, "health_stale_ttl", raw, state)
    if heartbeat is not None and updated_at.timestamp() < heartbeat.timestamp() - skew:
        return _stale_unknown(agent, "health_older_than_heartbeat", raw, state)

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
        agenttalk_version=raw.get("agenttalk_version"),
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
