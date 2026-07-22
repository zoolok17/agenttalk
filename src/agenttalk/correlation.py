"""Resolve a message's correlation ids from any valid location (#17).

On real bus messages the tracked id lives in ``meta["request_id"]`` and the
top-level ``request_id`` key is absent; ``recv_api.to_record`` normalizes it to
top-level; ``correlation_id`` is a third derived form. Reading only one location
is the #60/#61 bug class. These helpers resolve from all valid locations so a
reader is correct regardless of record shape.

Only ``request_id`` / ``broadcast_id`` / ``correlation_id`` are consulted — the
deliberately-distinct keys ``origin_request_id`` / ``parent_request`` /
``forwarded_request_id`` are never folded in.
"""

from __future__ import annotations


def resolve_request_id(record: dict) -> str | None:
    """request_id from top-level, then ``meta``, then ``correlation_id``."""
    return (record.get("request_id")
            or (record.get("meta") or {}).get("request_id")
            or record.get("correlation_id"))


def resolve_broadcast_id(record: dict) -> str | None:
    """broadcast_id from top-level, then ``meta``."""
    return record.get("broadcast_id") or (record.get("meta") or {}).get("broadcast_id")


def resolve_correlation_id(record: dict) -> str | None:
    """The correlation token: request_id if present, else broadcast_id."""
    return resolve_request_id(record) or resolve_broadcast_id(record)
