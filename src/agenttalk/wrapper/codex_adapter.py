"""Codex ``exec --json`` -> normalized events (CLI 0.141.0, empirically cataloged
on thread a-progress-adapter).

The stream is ITEM-LEVEL, NOT token-level: 0.141.0 emits no model_output_delta /
tool_output_delta events, so heartbeat coverage from this adapter is turn start +
tool start/finish + the final agent_message + turn completion. A long pure-
reasoning stretch can be silent until the final agent_message, so the
supervisor's conservative ``stuck_after_seconds`` REMAINS the backstop for Codex
- this adapter does not and cannot close that gap from the stream.

Pure mapper: one parsed JSON object in, zero-or-more normalized Events out. No
I/O. Unknown / metadata shapes map to [] (ignored, never raised).
"""

from __future__ import annotations

from .events import Event, EventType

CLI = "codex"


def map_event(obj: object) -> list[Event]:
    """Map ONE parsed ``codex exec --json`` object to normalized events."""
    if not isinstance(obj, dict):
        return []
    etype = obj.get("type")
    if etype == "turn.started":
        return [Event(EventType.TURN_STARTED, raw=obj)]
    if etype == "turn.completed":
        return [Event(EventType.TURN_FINISHED, raw=obj)]
    if etype == "turn.failed":
        err = obj.get("error")
        msg = err.get("message", "") if isinstance(err, dict) else ""
        # terminal failure - NOT retryable (distinct from transient transport).
        return [Event(EventType.ADAPTER_ERROR, text=msg, retryable=False, raw=obj)]
    if etype == "error":
        # top-level transport error (e.g. "Reconnecting... 2/5"): Codex is
        # self-recovering. Transient -> retryable, log only, never restart.
        return [Event(EventType.ADAPTER_ERROR, text=str(obj.get("message", "")),
                      retryable=True, raw=obj)]
    if etype in ("item.started", "item.completed"):
        return _map_item(etype, obj.get("item"), obj)
    # thread.started + any other shape: session metadata, no normalized event.
    return []


def _map_item(etype: str, item: object, obj: dict) -> list[Event]:
    if not isinstance(item, dict):
        return []
    itype = item.get("type")
    if itype == "command_execution":
        cmd = item.get("command")
        if etype == "item.started":
            return [Event(EventType.TOOL_STARTED, tool=cmd, raw=obj)]
        return [Event(EventType.TOOL_FINISHED, tool=cmd,
                      text=item.get("aggregated_output"), raw=obj)]
    if itype == "agent_message":
        # only the COMPLETED message carries text (item-level; no deltas). This
        # text is the degraded detector's scan target.
        if etype == "item.completed":
            return [Event(EventType.MODEL_OUTPUT, text=item.get("text"), raw=obj)]
        return []
    if itype == "error":
        # a structured error item INSIDE the turn (e.g. the WebSocket->HTTPS
        # fallback notice in the catalog): transient, Codex continues -> retryable.
        return [Event(EventType.ADAPTER_ERROR, text=str(item.get("message", "")),
                      retryable=True, raw=obj)]
    # unknown item type: ignore (a telemetry layer could log it).
    return []
