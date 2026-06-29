"""Managed lead-loop CADENCE TICK brain (lead-loop Slice 2 WP3).

The proactive sweep the controller drives when the bus is QUIET and the cadence
interval has elapsed. This module is the PURE / ADAPTER-layer logic:

  * :func:`cadence_due`            - is a sweep due (interval elapsed AND not backing off)?
  * :func:`build_cadence_snapshot` - a BOUNDED, READ-ONLY point-in-time view for the model
                                     (ids + summaries, NEVER transcripts, NEVER the lease token);
  * :func:`cadence_actionable`     - the actionability rules (which snapshot items justify a
                                     model turn) - returns [] when nothing is actionable, so the
                                     controller spends NO model turn;
  * :func:`apply_tick_success` / :func:`apply_tick_failure` - the cadence-state transitions
                                     (reminder/escalation dedup; failure backoff; one
                                     controller-health escalation at the threshold).

ISOLATION INVARIANT (WP3 condition 1): a cadence tick is a SYNTHETIC wrapper-owned
event, not a bus record. NOTHING here advances a cursor, records an attempt, or
touches the dead-letter path - it only READS the store and computes. The controller
(cli._wrap_loop_mode) owns the SENDs and the single write of the cadence state.

ADAPTER layer: reads the store and may import :mod:`lead_loop_runtime` (which may
lazily import :mod:`supervisor`). ``store.py`` never imports this module (the pure-core
/ adapter split).
"""
from __future__ import annotations

from datetime import datetime, timezone

from agenttalk import store as _store
from agenttalk import threads as _threads


def _f(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def cadence_due(cstate: dict, *, now_epoch: float, cadence_seconds: float) -> bool:
    """True when a proactive sweep is due: the cadence interval has elapsed since the
    last sweep AND any failure backoff has passed. The controller gates EVERY idle poll
    through this (it is cheap), so a not-due tick returns ``ran=False`` and spends nothing."""
    last = _f(cstate.get("last_tick_epoch"))
    backoff_until = _f(cstate.get("backoff_until_epoch"))
    return (now_epoch - last) >= cadence_seconds and now_epoch >= backoff_until


def _iso_to_epoch(value: object) -> float | None:
    """Best-effort ISO-8601 -> epoch seconds. Unparseable -> None (treated by callers as
    'no usable marker', matching the store's missing/corrupt-reads-as-no-marker rule)."""
    if not isinstance(value, str) or not value:
        return None
    txt = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(txt)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _peer_composing_fresh(store, peer: str, request_id: str, *, now_epoch: float) -> bool:
    """True when ``peer`` has a FRESH composing marker for ``request_id`` (a reply is in
    flight). Reuses the store's composing staleness rule: a marker older than
    ``COMPOSING_INTENT_STALE_SECONDS`` (or missing / corrupt / unparseable) is NOT fresh."""
    try:
        marker = store.read_composing_intent(peer)
    except Exception:  # noqa: BLE001 - observational only; degrade to "no marker"
        return False
    threads = marker.get("threads") if isinstance(marker, dict) else None
    entry = threads.get(request_id) if isinstance(threads, dict) else None
    if not isinstance(entry, dict):
        return False
    at_epoch = _iso_to_epoch(entry.get("at"))
    if at_epoch is None:
        return False
    return (now_epoch - at_epoch) < _store.COMPOSING_INTENT_STALE_SECONDS


def _trunc(value: object, cap: int) -> str:
    s = "" if value is None else str(value)
    return s if len(s) <= cap else s[:cap]


def build_cadence_snapshot(store, agent: str, *, now_epoch: float,
                           supervisor_config: dict | None = None,
                           max_threads: int = 20, max_items: int = 20,
                           body_cap: int = 200) -> dict:
    """A BOUNDED, READ-ONLY point-in-time snapshot for the synthetic cadence turn.

    Carries ids + summaries (NOT full transcripts), caps every collection, and truncates
    free text. Reads only - mutates NOTHING. The lease TOKEN is never included (only an
    ``has_lease`` bool + ``owner_pid``), so the model child can never learn it. Each
    subsystem read is independently degrade-guarded so one unreadable source yields an
    empty/None field instead of failing the whole snapshot (a partial snapshot still drives;
    a TOTAL failure is caught by the controller and treated as a cadence failure)."""
    snap: dict = {"agent": agent, "now_epoch": now_epoch}

    # lease (token-free) + timing
    try:
        lease = store.read_lead_loop_lease(agent)
        snap["lease"] = {"has_lease": bool(lease),
                         "owner_pid": (lease or {}).get("owner_pid"),
                         "expires_at": (lease or {}).get("expires_at")}
    except Exception:  # noqa: BLE001
        snap["lease"] = {"has_lease": False, "owner_pid": None, "expires_at": None}
    try:
        from agenttalk import lead_loop_runtime
        snap["timing"] = lead_loop_runtime.resolve_timing(
            store, agent, supervisor_config=supervisor_config)
    except Exception:  # noqa: BLE001
        snap["timing"] = {}

    # lead-loop / health (the WP1 authority view - already token-free)
    try:
        snap["lead_loop_health"] = store.lead_loop_state(agent)
    except Exception:  # noqa: BLE001
        snap["lead_loop_health"] = {}

    # threads (derived exactly as threads/sync do): summaries only, capped, terminal dropped
    thread_summaries: list[dict] = []
    operator_pending: list[dict] = []
    try:
        now_dt = datetime.fromtimestamp(now_epoch, tz=timezone.utc)
        cursor = store.cursor(agent) or ""
        closed = {rid for rid, e in store.read_threadstate(agent).items()
                  if isinstance(e, dict) and e.get("closed") is True}
        retired = set(store.retired_agents())
        rows = _threads.derive_threads(
            store.valid_messages(), agent=agent, cursor=cursor, now=now_dt,
            closed_rids=closed, retired=retired)
        for t in rows:
            if t.state in ("closed", "closed-superseded"):
                continue
            peer = t.peer
            summary = {
                "request_id": t.request_id,
                "state": t.state,
                "peer": peer,
                "role": t.role,
                "age_seconds": t.age_seconds,
                "last_msg_id": t.last_msg_id,
                "unread": t.unread,
                "needs_operator": t.needs_operator,
                "operator_state": t.operator_state,
                "next_action": t.next_action,
                "next_owner": t.next_owner,
                "subject": _trunc(t.subject, body_cap),
                "peer_composing_fresh": _peer_composing_fresh(
                    store, peer, t.request_id, now_epoch=now_epoch)
                if isinstance(peer, str) and peer else False,
            }
            if len(thread_summaries) < max_threads:
                thread_summaries.append(summary)
            if t.needs_operator and t.operator_state == "pending":
                operator_pending.append(summary)
    except Exception:  # noqa: BLE001
        thread_summaries = []
        operator_pending = []
    snap["threads"] = thread_summaries
    snap["operator_pending"] = operator_pending[:max_items]

    # supervisor restart / launch state
    try:
        snap["restart_request"] = store.read_restart_request(agent)
    except Exception:  # noqa: BLE001
        snap["restart_request"] = None
    try:
        snap["launch_requests"] = [
            {"request_id": (lr or {}).get("request_id"),
             "status": (lr or {}).get("status"),
             "for": (lr or {}).get("for") or (lr or {}).get("agent")}
            for lr in (store.list_launch_requests() or [])[:max_items]]
    except Exception:  # noqa: BLE001
        snap["launch_requests"] = []

    # dead-letter sink + unrouted operator escalations (due immediately; deduped downstream)
    try:
        items = []
        for dl in (store.list_dead_letters(agent) or [])[:max_items]:
            mid = dl.get("message_id") or dl.get("id")
            items.append({"message_id": mid, "from": dl.get("from"),
                          "subject": _trunc(dl.get("subject"), body_cap),
                          "failure_class": dl.get("failure_class"), "at": dl.get("at")})
        snap["dead_letters"] = {"count": store.dead_lettered_count(agent), "items": items}
    except Exception:  # noqa: BLE001
        snap["dead_letters"] = {"count": 0, "items": []}
    try:
        snap["unrouted_escalations"] = list(
            store.list_unrouted_escalations() or [])[:max_items]
    except Exception:  # noqa: BLE001
        snap["unrouted_escalations"] = []

    return snap


def cadence_actionable(snapshot: dict, cstate: dict, *, now_epoch: float,
                       reminder_after_seconds: float) -> list[dict]:
    """The actionability rules (WP3 condition 3). Returns the items that JUSTIFY a model
    turn; an empty list means the controller spends NO model turn this cadence.

    Categories (faithful to the condition - unread / reply-waiting / owed-inbound are NOT
    cadence work, those are the message path's job; operator-blocked is tracked context, not
    its own nudge):

      * ``outbound_reminder`` - an open-outbound thread older than ``reminder_after_seconds``,
        with NO fresh peer composing marker, fired ONCE per (request_id, last_msg_id);
      * ``dead_letter``        - each dead-lettered message, due immediately, deduped;
      * ``unrouted_escalation``- each unrouted operator escalation, due immediately, deduped.
    """
    items: list[dict] = []
    last_reminded = cstate.get("last_reminded")
    last_reminded = last_reminded if isinstance(last_reminded, dict) else {}
    dedup = cstate.get("escalation_dedup")
    dedup = dedup if isinstance(dedup, dict) else {}

    for t in snapshot.get("threads") or []:
        if t.get("state") != "open-outbound":
            continue
        age = t.get("age_seconds")
        if age is None or _f(age) < reminder_after_seconds:
            continue
        if t.get("peer_composing_fresh"):
            continue
        rid = t.get("request_id")
        last_msg_id = t.get("last_msg_id")
        if not rid:
            continue
        if last_reminded.get(rid) == last_msg_id:   # already reminded for THIS thread state
            continue
        items.append({"type": "outbound_reminder", "request_id": rid,
                      "last_msg_id": last_msg_id, "peer": t.get("peer"),
                      "subject": t.get("subject")})

    for dl in (snapshot.get("dead_letters") or {}).get("items") or []:
        mid = dl.get("message_id")
        key = f"dl:{mid}"
        if mid is None or dedup.get(key):
            continue
        items.append({"type": "dead_letter", "key": key, "message_id": mid,
                      "from": dl.get("from"), "subject": dl.get("subject"),
                      "failure_class": dl.get("failure_class")})

    for esc in snapshot.get("unrouted_escalations") or []:
        mid = esc.get("message_id")
        ag = esc.get("agent") or snapshot.get("agent")
        key = f"esc:{ag}:{mid}"
        if mid is None or dedup.get(key):
            continue
        items.append({"type": "unrouted_escalation", "key": key, "agent": ag,
                      "message_id": mid, "last_failure_class": esc.get("last_failure_class")})

    return items


def apply_tick_success(cstate: dict, *, now_epoch: float,
                       reminded_keys: list, escalation_keys: list) -> dict:
    """Record a completed sweep (a no-op sweep also calls this with empty lists so the
    snapshot is not rebuilt until the next interval). Resets the failure/backoff state,
    advances the sweep clock, and latches the reminder / escalation dedup so the same
    nudge does not repeat. Returns a NEW dict (does not mutate ``cstate``)."""
    last_reminded = dict(cstate.get("last_reminded") or {})
    for rid, last_msg_id in reminded_keys:
        last_reminded[rid] = last_msg_id
    dedup = dict(cstate.get("escalation_dedup") or {})
    for key in escalation_keys:
        dedup[key] = True
    return {
        "last_tick_epoch": now_epoch,
        "last_reminded": last_reminded,
        "escalation_dedup": dedup,
        "cadence_fails": 0,
        "backoff_until_epoch": 0.0,
        "health_escalated": False,
    }


def apply_tick_failure(cstate: dict, *, now_epoch: float, base: float,
                       max_backoff: float, health_threshold: int) -> tuple[dict, bool]:
    """Record a FAILED sweep (controller-HEALTH, never message poison): increment the
    consecutive-failure count, set an exponential backoff, and do NOT advance the sweep
    clock (so the tick retries once the backoff elapses). Returns ``(new_state,
    should_escalate)``; ``should_escalate`` is True whenever the failure count is at/above
    ``health_threshold`` AND a routed escalation has not yet been latched
    (``health_escalated`` is still False).

    The returned state PRESERVES the prior ``health_escalated`` - it does NOT self-latch.
    The CALLER latches ``health_escalated`` only AFTER the notice actually ROUTES, so an
    unrouted escalation (no operator-facing / sole-lead target) is RETRIED on the next
    failure instead of being silently dropped forever (mirrors the dead-letter
    ``escalation_routed`` discipline - codex WP3 MAJOR). Returns a NEW dict (does not
    mutate ``cstate``)."""
    fails = 0
    try:
        fails = int(cstate.get("cadence_fails") or 0)
    except (TypeError, ValueError):
        fails = 0
    fails += 1
    backoff = min(max_backoff, base * (2 ** (fails - 1)))
    already = bool(cstate.get("health_escalated"))
    should_escalate = (fails >= health_threshold) and not already
    new = {
        "last_tick_epoch": _f(cstate.get("last_tick_epoch")),   # NOT advanced -> retries
        "last_reminded": dict(cstate.get("last_reminded") or {}),
        "escalation_dedup": dict(cstate.get("escalation_dedup") or {}),
        "cadence_fails": fails,
        "backoff_until_epoch": now_epoch + backoff,
        "health_escalated": already,   # latched by the CALLER, only after a ROUTED notice
    }
    return new, should_escalate
