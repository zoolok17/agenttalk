"""Pure advisory coordination-stall detection.

Only explicit wait edges are considered.  Generic wrapper idleness, arbitrary
open outbound threads, and global all-idle/cycle inference are intentionally out
of scope until the repository has a cross-subsystem progress contract.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from agenttalk import health as health_model
from agenttalk import supervisor
from agenttalk import threads
from agenttalk.store import OPENER_KINDS, Store, _process_alive


SCHEMA_VERSION = 1
KIND_WAIT_TARGET_UNAVAILABLE = "wait_target_unavailable"
KIND_MANUAL_RESTART_BLOCKED = "manual_restart_blocked"
SOURCE_MANUAL_WAIT = "manual_wait"
SOURCE_WRAPPED_AWAIT = "wrapped_await"
_CONFIRMATION_POLLS = 2


@dataclass(frozen=True)
class WaitEdge:
    waiter: str
    request_id: str
    responders: tuple[str, ...]
    source: str
    started_at: str | None


def _closed_rids(store: Store, agent: str) -> set[str]:
    try:
        return {
            rid
            for rid, row in store.read_threadstate(agent).items()
            if isinstance(row, dict) and row.get("closed") is True
        }
    except Exception:
        return set()


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _age_seconds(value: str | None, now_epoch: float) -> float:
    parsed = _parse_iso(value)
    if parsed is None:
        return 0.0
    try:
        age = float(now_epoch) - parsed.timestamp()
    except (OSError, OverflowError, ValueError):
        return 0.0
    return max(0.0, age) if math.isfinite(age) else 0.0


def _hash(payload: object) -> str:
    return hashlib.sha256(json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _load_supervisor_config(store: Store) -> dict:
    try:
        return supervisor.load_supervisor_config(store.dir / "supervisor.json")
    except Exception:
        return {}


def _load_supervisor_state(store: Store) -> dict:
    try:
        return supervisor.load_supervisor_state(store.dir / "supervisor-state.json")
    except Exception:
        return {}


def _thread_rows(store: Store, messages: list, waiter: str, now: datetime) -> list:
    return threads.derive_threads(
        messages,
        agent=waiter,
        cursor=store.cursor(waiter) or "",
        now=now,
        closed_rids=_closed_rids(store, waiter),
        retired=set(store.retired_agents()),
    )


def _row_for(rows: list, request_id: str):
    return next((row for row in rows if row.request_id == request_id), None)


def _responders(row) -> tuple[str, ...]:
    if row is None:
        return ()
    raw = row.pending if row.is_broadcast else [row.peer]
    return tuple(sorted({value for value in raw if isinstance(value, str) and value}))


def _outbound_opener(messages: list, waiter: str, request_id: str):
    matches = [
        message
        for message in messages
        if message.sender == waiter
        and message.kind in OPENER_KINDS
        and (message.meta or {}).get("request_id") == request_id
    ]
    return matches[-1] if matches else None


def _parent_owned_by_waiter(rows: list, opener) -> bool:
    parent = (opener.meta or {}).get("parent_request") if opener is not None else None
    if parent is None:
        return True
    if not isinstance(parent, str) or not parent:
        return False
    row = _row_for(rows, parent)
    return row is not None and row.state == "owed-inbound" and row.next_owner == opener.sender


def _wrapped_waiter_idle(report_agent: dict | None) -> bool:
    if not isinstance(report_agent, dict) or report_agent.get("heartbeat_stale") is not False:
        return False
    health = report_agent.get("health") if isinstance(report_agent.get("health"), dict) else {}
    return (
        health_model.label(health) == health_model.STATE_IDLE_WAITING
        and health.get("stale") is not True
    )


def _manual_marker_active(marker: dict, now_epoch: float) -> bool:
    pid = marker.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or not _process_alive(pid):
        return False
    deadline = marker.get("deadline_epoch")
    if isinstance(deadline, (int, float)) and not isinstance(deadline, bool):
        if not math.isfinite(float(deadline)) or now_epoch > float(deadline):
            return False
    return True


def _wait_edges(
    store: Store,
    messages: list,
    report_agents: dict,
    *,
    now_epoch: float,
) -> tuple[list[WaitEdge], list[dict]]:
    now = datetime.fromtimestamp(now_epoch, timezone.utc)
    cfg = store.load_config()
    roster = cfg.get("agents") if isinstance(cfg.get("agents"), list) else []
    rows_by_waiter: dict[str, list] = {}

    def rows(waiter: str) -> list:
        if waiter not in rows_by_waiter:
            rows_by_waiter[waiter] = _thread_rows(store, messages, waiter, now)
        return rows_by_waiter[waiter]

    edges: list[WaitEdge] = []
    for waiter in roster:
        marker = store.read_waiting(waiter)
        if not isinstance(marker, dict):
            continue
        request_id = marker.get("to_request")
        if not isinstance(request_id, str) or not request_id:
            continue
        row = _row_for(rows(waiter), request_id)
        if (
            row is None
            or row.state != "open-outbound"
            or row.opener_sender != waiter
            or not _manual_marker_active(marker, now_epoch)
        ):
            continue
        responders = _responders(row)
        if responders:
            edges.append(WaitEdge(
                waiter=waiter,
                request_id=request_id,
                responders=responders,
                source=SOURCE_MANUAL_WAIT,
                started_at=marker.get("since") if isinstance(marker.get("since"), str) else None,
            ))

    awaiting, diagnostics = store.list_awaiting()
    for record in awaiting:
        waiter = record["agent"]
        if store.wrapper_wait_generation(waiter) != record["wrapper_generation"]:
            continue
        if not _wrapped_waiter_idle(report_agents.get(waiter)):
            continue
        request_id = record["request_id"]
        waiter_rows = rows(waiter)
        row = _row_for(waiter_rows, request_id)
        opener = _outbound_opener(messages, waiter, request_id)
        if (
            row is None
            or row.state != "open-outbound"
            or row.opener_sender != waiter
            or opener is None
            or not _parent_owned_by_waiter(waiter_rows, opener)
        ):
            continue
        started = _parse_iso(record["started_at"])
        if started is None or started.timestamp() > now_epoch + health_model.DEFAULT_HEARTBEAT_SKEW_SECONDS:
            continue
        responders = _responders(row)
        if responders:
            edges.append(WaitEdge(
                waiter=waiter,
                request_id=request_id,
                responders=responders,
                source=SOURCE_WRAPPED_AWAIT,
                started_at=record["started_at"],
            ))

    deduped: dict[tuple[str, str, tuple[str, ...]], WaitEdge] = {}
    for edge in edges:
        key = (edge.waiter, edge.request_id, edge.responders)
        prior = deduped.get(key)
        if prior is None or edge.source == SOURCE_WRAPPED_AWAIT:
            deduped[key] = edge
    return list(deduped.values()), diagnostics


def _availability_map(report: dict, plan: dict, config: dict, store: Store) -> dict[str, dict]:
    report_agents = report.get("agents") if isinstance(report.get("agents"), dict) else {}
    plan_agents = plan.get("agents") if isinstance(plan.get("agents"), dict) else {}
    config_agents = config.get("agents") if isinstance(config.get("agents"), dict) else {}
    retired = set(store.retired_agents())
    names = set(report_agents) | set(config_agents) | retired
    return {
        name: supervisor.project_coordination_availability(
            name,
            report_agents.get(name),
            plan_agents.get(name),
            config_agents.get(name),
            retired=name in retired,
        )
        for name in names
    }


def _confirmed(
    availability: dict,
    observation: dict | None,
    *,
    now_epoch: float,
    poll_seconds: float,
) -> bool:
    state = availability.get("state")
    if state not in {supervisor.AVAILABILITY_UNAVAILABLE, supervisor.AVAILABILITY_TERMINAL}:
        return False
    if not isinstance(observation, dict):
        return False
    if any(observation.get(key) != availability.get(key) for key in (
        "state", "subtype", "evidence_code")):
        return False
    if int(observation.get("consecutive", 0)) < _CONFIRMATION_POLLS:
        return False
    last = observation.get("last_observed_epoch")
    return (
        isinstance(last, (int, float))
        and not isinstance(last, bool)
        and math.isfinite(float(last))
        and 0 <= now_epoch - float(last) <= max(60.0, poll_seconds * 3.0)
    )


def _wait_item(
    edge: WaitEdge,
    responder_availability: list[dict],
    *,
    now_epoch: float,
) -> dict:
    identity = {
        "kind": KIND_WAIT_TARGET_UNAVAILABLE,
        "waiter": edge.waiter,
        "request_id": edge.request_id,
        "responders": list(edge.responders),
    }
    identity_hash = _hash(identity)
    semantic = {
        **identity,
        "blockers": [
            {
                "agent": row["agent"],
                "state": row["state"],
                "subtype": row["subtype"],
                "evidence_code": row["evidence_code"],
            }
            for row in responder_availability
        ],
    }
    terminal = all(
        row["state"] == supervisor.AVAILABILITY_TERMINAL
        for row in responder_availability
    )
    if len(edge.responders) == 1:
        target = edge.responders[0]
        if terminal:
            reason = (
                f"{edge.waiter} is waiting for {target}, which is no longer active. "
                "Reassign the request."
            )
        else:
            reason = (
                f"{edge.waiter} is waiting for {target}, which is unavailable. "
                f"Reassign the request or restore {target}."
            )
    else:
        joined = ", ".join(edge.responders)
        reason = (
            f"{edge.waiter} is waiting for {joined}; every pending responder is unavailable. "
            "Reassign the request or restore a responder."
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "stall_id": identity_hash[:24],
        "identity_hash": identity_hash,
        "content_hash": _hash(semantic),
        "kind": KIND_WAIT_TARGET_UNAVAILABLE,
        "source": edge.source,
        "waiter": edge.waiter,
        "request_id": edge.request_id,
        "responders": list(edge.responders),
        "reason": reason,
        "action": "Reassign the request or restore the unavailable responder.",
        "age_seconds": _age_seconds(edge.started_at, now_epoch),
        "advisory": True,
        "human_can_unblock_now": True,
        "evidence": semantic["blockers"],
    }


def _restart_items(
    store: Store,
    report: dict,
    config: dict,
    *,
    now_epoch: float,
) -> tuple[list[dict], list[dict]]:
    observations, diagnostics = supervisor.read_coordination_barrier_observations(store)
    report_agents = report.get("agents") if isinstance(report.get("agents"), dict) else {}
    grace = config.get("launch_grace_seconds", 120)
    grace = float(grace) if isinstance(grace, (int, float)) and not isinstance(grace, bool) else 120.0
    items: list[dict] = []
    for observation in observations:
        agent = observation["agent"]
        report_agent = report_agents.get(agent)
        marker = report_agent.get("restart_request") if isinstance(report_agent, dict) else None
        if not isinstance(marker, dict):
            continue
        if (
            marker.get("source") != "manual"
            or marker.get("request_id") != observation["request_id"]
            or marker.get("at_epoch") != observation["marker_epoch"]
            or observation["consecutive_blocked_polls"] < _CONFIRMATION_POLLS
            or observation["first_observed_epoch"] < observation["marker_epoch"]
            or observation["last_observed_epoch"] > now_epoch
            or now_epoch - observation["marker_epoch"] < grace
        ):
            continue
        identity = {
            "kind": KIND_MANUAL_RESTART_BLOCKED,
            "agent": agent,
            "request_id": observation["request_id"],
        }
        semantic = {
            **identity,
            "barrier_reason": observation["barrier_reason"],
            "survivor_fingerprint": observation["survivor_fingerprint"],
        }
        identity_hash = _hash(identity)
        items.append({
            "schema_version": SCHEMA_VERSION,
            "stall_id": identity_hash[:24],
            "identity_hash": identity_hash,
            "content_hash": _hash(semantic),
            "kind": KIND_MANUAL_RESTART_BLOCKED,
            "source": "supervisor_launch_barrier",
            "agent": agent,
            "request_id": observation["request_id"],
            "responders": [],
            "reason": f"Restart of {agent} is still blocked by an older live process.",
            "action": "Inspect and stop the older attributed process, then retry the restart.",
            "age_seconds": max(0.0, now_epoch - observation["marker_epoch"]),
            "advisory": True,
            "human_can_unblock_now": True,
            "evidence": [{
                "barrier_reason": observation["barrier_reason"],
                "survivor_fingerprint": observation["survivor_fingerprint"],
            }],
        })
    return items, diagnostics


def build_snapshot(
    store: Store,
    *,
    now_epoch: float | None = None,
    supervisor_config: dict | None = None,
    supervisor_state: dict | None = None,
    process_snapshot: list[dict] | None = None,
    valid_messages: list | None = None,
) -> dict:
    """Build one read-only, fail-safe coordination snapshot.

    ``valid_messages``, when given, is a caller-supplied
    ``store.valid_messages()`` result — skips this function's own call so
    a caller that already computed one (e.g. `agenttalk status`) doesn't
    pay for a second full store scan (#184). Tier 3
    (``supervisor.build_report``'s own internal message reads) is
    unchanged/out of scope for this fix.
    """
    now = time.time() if now_epoch is None else float(now_epoch)
    config = supervisor_config if isinstance(supervisor_config, dict) else _load_supervisor_config(store)
    state = supervisor_state if isinstance(supervisor_state, dict) else _load_supervisor_state(store)
    try:
        report = supervisor.build_report(
            store,
            now_epoch=now,
            state=state,
            supervisor_config=config,
        )
        plan = supervisor.plan_actions(
            report,
            state,
            config,
            now_epoch=now,
            snapshot=process_snapshot,
        )
        messages = valid_messages if valid_messages is not None else store.valid_messages()
        report_agents = report.get("agents") if isinstance(report.get("agents"), dict) else {}
        edges, diagnostics = _wait_edges(
            store,
            messages,
            report_agents,
            now_epoch=now,
        )
        availability = _availability_map(report, plan, config, store)
        observed, observation_problems = supervisor.read_coordination_availability_observation(store)
        diagnostics.extend(observation_problems)
        poll = config.get("poll_seconds", 15)
        poll = float(poll) if isinstance(poll, (int, float)) and not isinstance(poll, bool) else 15.0
        items: list[dict] = []
        for edge in edges:
            rows = [availability.get(agent, {
                "agent": agent,
                "state": supervisor.AVAILABILITY_UNKNOWN,
                "subtype": "missing",
                "evidence_code": "missing",
            }) for agent in edge.responders]
            if rows and all(
                _confirmed(row, observed.get(row["agent"]), now_epoch=now, poll_seconds=poll)
                for row in rows
            ):
                items.append(_wait_item(edge, rows, now_epoch=now))
        restart_items, restart_problems = _restart_items(
            store,
            report,
            config,
            now_epoch=now,
        )
        items.extend(restart_items)
        diagnostics.extend(restart_problems)
        items.sort(key=lambda item: (item["kind"], item["identity_hash"]))
        return {
            "schema_version": SCHEMA_VERSION,
            "items": items,
            "diagnostics": diagnostics,
        }
    except Exception as exc:
        return {
            "schema_version": SCHEMA_VERSION,
            "items": [],
            "diagnostics": [{
                "code": "coordination_snapshot_unavailable",
                "detail": type(exc).__name__,
            }],
        }


def clear_terminal_awaits(store: Store, request_id: str) -> None:
    """Best-effort physical cleanup after a validated reply/rescind append.

    Thread derivation remains the authority: a stale record is already inactive
    even if this cleanup loses a race or the process crashes.
    """
    try:
        records, _ = store.list_awaiting()
        records = [row for row in records if row.get("request_id") == request_id]
        if not records:
            return
        messages = store.valid_messages()
        now = datetime.now(timezone.utc)
        for record in records:
            rows = _thread_rows(store, messages, record["agent"], now)
            row = _row_for(rows, request_id)
            if row is None or row.state != "open-outbound":
                store.clear_awaiting_if_token(record["agent"], record["wait_token"])
    except Exception:
        return
