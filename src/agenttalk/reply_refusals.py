"""#162: a sibling sink for wrapper-refused reply drafts.

Deliberately NOT the poison-inbound `Store.dead_letter()` path: that path's own
contract is "move the original inbound message out of `messages/` and advance the
cursor past it" (a message the wrapped model failed deterministically). A refused
REPLY is a different failure domain - the inbound message was handled fine; only the
wrapper's own outbound publish of the child's draft failed. There is nothing to move
and no cursor to advance; a NEW record is created describing what was lost and where
its bytes still are.

Layout: ``<store.dir>/reply-refusals/<agent>/<original_message_id>.json`` (the
refusal record) plus, once handled, a sibling ``<original_message_id>.resolved.json``
(mirrors the dead-letter sink's own payload + sidecar pairing, `store.py`'s
`dead_letter()`/`resolve_dead_letter()` family, without sharing its code path -
this sink's own crash-safety needs are simpler: a single atomic write, no move).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agenttalk._atomic import write_text as _atomic_write_text
from agenttalk.store import validate_agent_name

if TYPE_CHECKING:  # pragma: no cover - typing only
    from agenttalk.store import Store

SCHEMA_VERSION = 1


def reply_refusal_dir(store: "Store") -> Path:
    """The sink root - a sibling of ``messages/``, ``state/`` and ``dead-letter/``."""
    return store.dir / "reply-refusals"


def _sink_for(store: "Store", agent: str) -> Path:
    return reply_refusal_dir(store) / validate_agent_name(agent)


def _record_path(store: "Store", agent: str, original_message_id: str) -> Path:
    return _sink_for(store, agent) / f"{original_message_id}.json"


def _resolved_path(store: "Store", agent: str, original_message_id: str) -> Path:
    return _sink_for(store, agent) / f"{original_message_id}.resolved.json"


def record_reply_refusal(
    store: "Store",
    *,
    agent: str,
    original_message_id: str,
    original_from: str,
    intended_kind: str,
    reason: str,
    draft_path: str,
    correlation: dict[str, Any],
    at: str,
) -> Path:
    """Write one refusal record. Never overwrites an existing record for the same
    ``original_message_id`` (a second refusal on the SAME inbound message - e.g. a
    retried draft - lands as ``<id>.<iso>.json`` alongside it, collision-safe, the
    same convention `Store.dead_letter()` uses for its own payload collisions)."""
    if not (isinstance(original_message_id, str) and original_message_id):
        raise ValueError("record_reply_refusal: original_message_id must be a non-empty string")
    sink = _sink_for(store, agent)
    sink.mkdir(parents=True, exist_ok=True)
    path = _record_path(store, agent, original_message_id)
    if path.exists():
        suffix = at.replace(":", "-")
        path = sink / f"{original_message_id}.{suffix}.json"
    payload = {
        "schema_version": SCHEMA_VERSION,
        "agent": agent,
        "original_message_id": original_message_id,
        "original_from": original_from,
        "intended_kind": intended_kind,
        "reason": reason,
        "draft_path": draft_path,
        "correlation": correlation,
        "recorded_at": at,
    }
    _atomic_write_text(path, json.dumps(payload, indent=2, ensure_ascii=False))
    return path


def _iter_records(store: "Store", agent: str | None) -> list[tuple[str, Path]]:
    root = reply_refusal_dir(store)
    if not root.is_dir():
        return []
    agents = [validate_agent_name(agent)] if agent else sorted(
        p.name for p in root.iterdir() if p.is_dir()
    )
    out: list[tuple[str, Path]] = []
    for ag in agents:
        sink = root / ag
        if not sink.is_dir():
            continue
        for p in sorted(sink.iterdir()):
            if p.name.endswith(".resolved.json"):
                continue
            if p.suffix == ".json":
                out.append((ag, p))
    return out


def list_reply_refusals(store: "Store", agent: str | None = None) -> list[dict[str, Any]]:
    """Every unresolved-or-resolved refusal record, newest evidence first read order
    (callers filter resolved state themselves via :func:`is_resolved`)."""
    items: list[dict[str, Any]] = []
    for ag, path in _iter_records(store, agent):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        data = dict(data)
        data["_agent"] = ag
        data["_record_path"] = str(path)
        items.append(data)
    return items


def reply_refusal_count(store: "Store") -> int:
    return len(_iter_records(store, None))


def read_reply_refusal(store: "Store", agent: str, original_message_id: str) -> dict[str, Any] | None:
    path = _record_path(store, agent, original_message_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def is_resolved(store: "Store", agent: str, original_message_id: str) -> bool:
    return _resolved_path(store, agent, original_message_id).is_file()


def resolve_reply_refusal(
    store: "Store",
    agent: str,
    original_message_id: str,
    *,
    reason: str,
    sender: str,
    evidence: str | None = None,
    at: str,
) -> Path:
    """Mark one refusal record handled. Audited: requires a non-empty ``reason``,
    same convention as `dead-letter resolve --reason`. Idempotent - resolving an
    already-resolved record overwrites its sidecar with the LATEST disposition,
    never raises."""
    if not (isinstance(reason, str) and reason.strip()):
        raise ValueError("resolve_reply_refusal: reason must be a non-empty string")
    if read_reply_refusal(store, agent, original_message_id) is None:
        raise FileNotFoundError(
            f"resolve_reply_refusal: no refusal record for {agent}/{original_message_id}")
    path = _resolved_path(store, agent, original_message_id)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "agent": agent,
        "original_message_id": original_message_id,
        "reason": reason,
        "resolved_by": sender,
        "evidence": evidence,
        "resolved_at": at,
    }
    _atomic_write_text(path, json.dumps(payload, indent=2, ensure_ascii=False))
    return path
