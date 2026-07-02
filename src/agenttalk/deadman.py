"""Content-blind mail-age SLO checker.

The deadman is intentionally independent of the supervisor process and its
``supervisor-state.json`` ledger. It derives obligations from the same
request/reply projector used by ``threads`` and exits nonzero when owed work has
gone stale.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from agenttalk import threads as th
from agenttalk.store import Message, Store

DEFAULT_THRESHOLD_SECONDS = 900.0
CONTROL_ALARM_KINDS = frozenset({"wake"})


class DeadmanInvalidMessagesError(RuntimeError):
    def __init__(self, count: int) -> None:
        super().__init__("invalid message files present")
        self.count = count


class DeadmanUnknownAgeError(RuntimeError):
    pass


def _parse_ts(ts: str) -> datetime | None:
    if not isinstance(ts, str) or not ts:
        return None
    text = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _closed_rids(store: Store, agent: str) -> set[str]:
    return {
        rid for rid, entry in store.read_threadstate(agent).items()
        if isinstance(entry, dict) and entry.get("closed") is True
    }


def _deadman_config(cfg: dict[str, Any]) -> dict[str, Any]:
    raw = cfg.get("deadman", {})
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("deadman config must be an object")
    threshold = raw.get("mail_age_slo_seconds", DEFAULT_THRESHOLD_SECONDS)
    if (
        not isinstance(threshold, (int, float))
        or isinstance(threshold, bool)
        or threshold <= 0
    ):
        raise ValueError("deadman.mail_age_slo_seconds must be a positive number")
    alarm_unread = raw.get("alarm_unread_response", False)
    if not isinstance(alarm_unread, bool):
        raise ValueError("deadman.alarm_unread_response must be a boolean")
    return {
        "threshold_seconds": float(threshold),
        "alarm_unread_response": alarm_unread,
    }


def _coerce_threshold(value: float | None) -> float | None:
    if value is None:
        return None
    threshold = float(value)
    if threshold <= 0:
        raise ValueError("threshold_seconds must be positive")
    return threshold


def resolve_options(
    store: Store,
    *,
    threshold_seconds: float | None = None,
    alarm_unread_response: bool | None = None,
) -> dict[str, Any]:
    cfg = store.load_config()
    opts = _deadman_config(cfg)
    threshold = _coerce_threshold(threshold_seconds)
    if threshold is not None:
        opts["threshold_seconds"] = threshold
    if alarm_unread_response is not None:
        opts["alarm_unread_response"] = bool(alarm_unread_response)
    opts["config"] = cfg
    return opts


def _thread_item(agent: str, row: th.Thread, bucket: str) -> dict[str, Any]:
    item = {
        "agent": agent,
        "bucket": bucket,
        "state": row.state,
        "kind": row.opener_kind,
        "request_id": row.request_id,
        "message_id": row.last_msg_id,
        "age_seconds": round(float(row.age_seconds or 0.0), 3),
    }
    if row.is_broadcast:
        item["broadcast"] = True
        item["pending"] = list(row.pending)
    if row.needs_operator:
        item["needs_operator"] = True
        item["operator_state"] = row.operator_state
    return item


def _control_item(agent: str, msg: Message, age: float) -> dict[str, Any]:
    rid = (msg.meta or {}).get("request_id")
    item = {
        "agent": agent,
        "bucket": "stale_control",
        "state": "unread-control",
        "kind": msg.kind,
        "message_id": msg.id,
        "age_seconds": round(age, 3),
    }
    if isinstance(rid, str) and rid:
        item["request_id"] = rid
    return item


def _fail_safe(report: dict[str, Any], exc: Exception) -> tuple[int, dict[str, Any]]:
    report["status"] = "error"
    report["counts"]["errors"] = 1
    item = {"class": type(exc).__name__}
    count = getattr(exc, "count", None)
    if isinstance(count, int):
        item["count"] = count
    report["errors"].append(item)
    return 3, report


def check(
    store: Store,
    *,
    threshold_seconds: float | None = None,
    alarm_unread_response: bool | None = None,
    now: datetime | None = None,
) -> tuple[int, dict[str, Any]]:
    """Return ``(exit_code, report)`` for the mail-age SLO.

    Exit 3 is fail-safe: stale obligations/control messages alarm, and scan or
    config errors alarm with an error class instead of silently passing.
    """
    now_dt = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    base_report: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": now_dt.isoformat().replace("+00:00", "Z"),
        "threshold_seconds": None,
        "alarm_unread_response": False,
        "status": "ok",
        "counts": {
            "stale_obligation": 0,
            "stale_unread_response": 0,
            "stale_control": 0,
            "errors": 0,
        },
        "buckets": {
            "stale_obligation": [],
            "stale_unread_response": [],
            "stale_control": [],
        },
        "errors": [],
    }
    try:
        opts = resolve_options(
            store,
            threshold_seconds=threshold_seconds,
            alarm_unread_response=alarm_unread_response,
        )
        cfg = opts["config"]
        threshold = float(opts["threshold_seconds"])
        alarm_replies = bool(opts["alarm_unread_response"])
        invalid = store.list_invalid_messages()
        if invalid:
            raise DeadmanInvalidMessagesError(len(invalid))
        messages = store.valid_messages()

        base_report["threshold_seconds"] = threshold
        base_report["alarm_unread_response"] = alarm_replies
        agents = [a for a in (cfg.get("agents") or []) if isinstance(a, str)]
        retired = set(store.retired_agents())
        by_recipient: dict[str, list[Message]] = {a: [] for a in agents}
        for msg in messages:
            if msg.recipient in by_recipient:
                by_recipient[msg.recipient].append(msg)

        for agent in agents:
            cursor = store.cursor(agent)
            rows = th.derive_threads(
                messages,
                agent=agent,
                cursor=cursor,
                now=now_dt,
                closed_rids=_closed_rids(store, agent),
                retired=retired,
            )
            for row in rows:
                if row.state in ("owed-inbound", "reply-waiting") and row.age_seconds is None:
                    raise DeadmanUnknownAgeError
                if row.age_seconds is None or row.age_seconds < threshold:
                    continue
                if row.state == "owed-inbound":
                    base_report["buckets"]["stale_obligation"].append(
                        _thread_item(agent, row, "stale_obligation"))
                elif row.state == "reply-waiting":
                    base_report["buckets"]["stale_unread_response"].append(
                        _thread_item(agent, row, "stale_unread_response"))
            for msg in by_recipient.get(agent, []):
                if msg.id <= cursor or msg.kind not in CONTROL_ALARM_KINDS:
                    continue
                ts = _parse_ts(msg.ts)
                if ts is None:
                    raise DeadmanUnknownAgeError
                age = (now_dt - ts).total_seconds()
                if age >= threshold:
                    base_report["buckets"]["stale_control"].append(
                        _control_item(agent, msg, age))

        for key in ("stale_obligation", "stale_unread_response", "stale_control"):
            base_report["counts"][key] = len(base_report["buckets"][key])
        alarm = (
            base_report["counts"]["stale_obligation"] > 0
            or base_report["counts"]["stale_control"] > 0
            or (alarm_replies and base_report["counts"]["stale_unread_response"] > 0)
        )
        if alarm:
            base_report["status"] = "alarm"
            return 3, base_report
        return 0, base_report
    except Exception as exc:  # noqa: BLE001 - deadman must fail safe, not crash
        return _fail_safe(base_report, exc)
