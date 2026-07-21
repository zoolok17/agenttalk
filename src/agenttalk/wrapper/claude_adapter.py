"""Claude `--output-format stream-json --verbose --include-partial-messages` ->
normalized events. Empirically verified with a live probe (Phase-2 build plan):
the stream is JSONL, Anthropic streaming events are wrapped in a `stream_event`
envelope, and THINKING streams incrementally as thinking_delta DURING reasoning -
so unlike Codex's item-level stream, the Claude stream carries liveness through a
long no-tool reasoning stretch (it closes the pure-reasoning gap).

Pure mapper: one parsed JSON object in, zero-or-more normalized Events out. No I/O.

Heartbeat / degraded-scan policy (lead ruling):
- thinking_delta -> model_output_delta(channel=thinking): STAMPS liveness (reasoning
  progress); NEVER scanned (internal reasoning may legitimately discuss tool syntax).
- text_delta -> model_output_delta(channel=text): does NOT stamp (a leaked turn
  streams degraded fragments and the signature spans deltas); rendered only.
- the assembled `assistant` TEXT block -> model_output(channel=text): the degraded
  SCAN target and the ONLY text event that stamps liveness - and only when CLEAN
  (the engine suppresses the stamp when the detector flags it degraded).
"""

from __future__ import annotations

from .events import Event, EventType

CLI = "claude"


def _result_error_text(obj: dict) -> str:
    result = obj.get("result")
    if result not in (None, ""):
        return str(result)

    errors = obj.get("errors")
    if isinstance(errors, str) and errors:
        return errors
    if isinstance(errors, list):
        for item in errors:
            if isinstance(item, str) and item:
                return item
            if isinstance(item, dict):
                message = item.get("message")
                if message not in (None, ""):
                    return str(message)

    return str(obj.get("subtype") or "error")


def map_event(obj: object) -> list[Event]:
    """Map ONE parsed Claude stream-json object to normalized events."""
    if not isinstance(obj, dict):
        return []
    t = obj.get("type")
    if t == "stream_event":
        return _map_stream_event(obj.get("event"), obj)
    if t == "assistant":
        return _map_assistant(obj.get("message"), obj)
    if t == "user":
        return _map_user(obj.get("message"), obj)
    if t == "result":
        # success: message_stop already emitted turn_finished; the result is a
        # terminal summary. An error result is a terminal adapter_error.
        if obj.get("is_error"):
            text = _result_error_text(obj)
            return [Event(EventType.ADAPTER_ERROR, text=text, retryable=False, raw=obj)]
        return []
    if t == "rate_limit_event":
        info = obj.get("rate_limit_info") or {}
        status = info.get("status")
        if status not in (None, "allowed"):
            # throttled/blocked: transient transport-ish -> log, do not act.
            return [Event(EventType.ADAPTER_ERROR, text=f"rate_limit: {status}",
                          retryable=True, raw=obj)]
        return []
    # system (init / status / thinking_tokens) + any other top-level: metadata.
    return []


def _map_stream_event(ev: object, obj: dict) -> list[Event]:
    if not isinstance(ev, dict):
        return []
    et = ev.get("type")
    if et == "message_start":
        return [Event(EventType.TURN_STARTED, raw=obj)]
    if et == "message_stop":
        return [Event(EventType.TURN_FINISHED, raw=obj)]
    if et == "content_block_start":
        cb = ev.get("content_block") or {}
        if cb.get("type") == "tool_use":
            return [Event(EventType.TOOL_STARTED, tool=cb.get("name"), raw=obj)]
        # thinking / text block starts: the deltas carry the content.
        return []
    if et == "content_block_delta":
        d = ev.get("delta") or {}
        dt = d.get("type")
        if dt == "text_delta":
            return [Event(EventType.MODEL_OUTPUT_DELTA, text=d.get("text"),
                          channel="text", raw=obj)]
        if dt == "thinking_delta":
            return [Event(EventType.MODEL_OUTPUT_DELTA, text=d.get("thinking"),
                          channel="thinking", raw=obj)]
        if dt == "input_json_delta":
            return [Event(EventType.TOOL_OUTPUT_DELTA, text=d.get("partial_json"), raw=obj)]
        # signature_delta + any other delta: metadata.
        return []
    # content_block_stop / message_delta / ping: boundary / usage.
    return []


def _map_assistant(msg: object, obj: dict) -> list[Event]:
    if not isinstance(msg, dict):
        return []
    out: list[Event] = []
    for block in msg.get("content") or []:
        if not isinstance(block, dict):
            continue
        bt = block.get("type")
        if bt == "text":
            # the assembled assistant text: the degraded SCAN target.
            out.append(Event(EventType.MODEL_OUTPUT, text=block.get("text"),
                             channel="text", raw=obj))
        elif bt == "thinking":
            out.append(Event(EventType.MODEL_OUTPUT, text=block.get("thinking"),
                             channel="thinking", raw=obj))
        # tool_use blocks already produced TOOL_STARTED from the stream_event
        # content_block_start; skip here to avoid a double tool_started.
    return out


def _map_user(msg: object, obj: dict) -> list[Event]:
    if not isinstance(msg, dict):
        return []
    out: list[Event] = []
    for block in msg.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            content = block.get("content")
            text = content if isinstance(content, str) else None
            out.append(Event(EventType.TOOL_FINISHED, text=text, raw=obj))
    return out
