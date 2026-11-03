"""The size of one Claude conversation, read from the stream of one dispatch (self-compaction
pilot W1; the completion rule is probe (a) of docs/design/self-compaction-pilot-w0.md).

Pure and bounded: it sees only the stream lines the wrapper already parses. In memory, for the
length of one dispatch, it keeps per call the message id, the model name and four counts, and from
the result event the counts, ``num_turns`` and the context window per model name. It keeps no
message, tool or result text, and persists nothing itself. It is the one reader of this figure;
every view reads the outcome from the session record (``session.record_dispatch``), none computes
its own.

A call is identified by its ``message_start``; its output comes from the last ``message_delta``;
it is complete only with start, at least one delta and stop, in that order. ``assistant`` events
never count a call (one call can produce several). A reading exists only when every call was
complete and has its own message id (a missing or reused id is a conflict), they add up to the
dispatch's one ``result`` event in all four counts (a second ``result`` is a conflict), their number
equals ``num_turns`` and no call is left open. A call that began before a compaction never supplies
the reading after it. Anything else gives no reading, and an absent reading is never zero.
"""
from __future__ import annotations

_INPUT = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def _count(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


class DispatchFold:
    """Fold the parsed stream lines of ONE dispatch; ask :meth:`reading` after the last line."""

    def __init__(self, on_compaction=None) -> None:
        self._on_compaction = on_compaction  # called once, when the first compaction marker is seen
        self.calls: list[dict] = []   # complete calls: model, input fields, output
        self.compacted = False        # the conversation was compacted during this dispatch
        self._first_after_compact = 0
        self._open: dict | None = None
        self._bad = False             # something was incomplete, orphaned or contradictory
        self._result: dict | None = None
        self._starts: dict[str, dict] = {}
        self._results = 0

    def note(self, raw: object) -> None:
        if not isinstance(raw, dict):
            return
        kind = raw.get("type")
        if kind == "result":
            self._results += 1
            usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
            models = raw.get("modelUsage") if isinstance(raw.get("modelUsage"), dict) else {}
            self._result = {"num_turns": _count(raw.get("num_turns")),
                            "usage": {k: _count(usage.get(k)) for k in (*_INPUT, "output_tokens")},
                            "windows": {m: _count(v.get("contextWindow")) for m, v in models.items()
                                        if isinstance(v, dict)}}
        elif raw.get("parent_tool_use_id"):
            return  # a sub-agent's call: not this conversation (a result that counts it fails the cross-check)
        elif kind == "system" and raw.get("subtype") == "compact_boundary":
            # a call still open began before the boundary: it counts, but never supplies the reading
            self._first_after_compact = len(self.calls) + (self._open is not None)
            first = not self.compacted
            self.compacted = True
            if first and self._on_compaction is not None:
                self._on_compaction()
        elif kind == "stream_event" and isinstance(raw.get("event"), dict):
            self._stream_event(raw["event"])
        elif kind == "assistant":
            self._assistant(raw.get("message"))

    def _stream_event(self, ev: dict) -> None:
        kind = ev.get("type")
        if kind == "message_start":
            if self._open is not None:
                self._bad = True  # the previous call never stopped
            msg = ev.get("message") if isinstance(ev.get("message"), dict) else {}
            mid = msg.get("id")
            if not isinstance(mid, str) or not mid or mid in self._starts:
                self._bad = True  # no identity, or the same identity started twice
            usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else {}
            fields = {k: _count(usage.get(k)) for k in _INPUT}
            self._open = {"model": msg.get("model"), "stage": "start", "output": None,
                          "ok": None not in fields.values(), **fields}
            if isinstance(mid, str) and mid:
                self._starts.setdefault(mid, self._open)
        elif kind == "message_delta":
            usage = ev.get("usage") if isinstance(ev.get("usage"), dict) else {}
            if self._open is None:
                self._bad = True
                return
            self._open["output"] = _count(usage.get("output_tokens"))  # cumulative: the last one wins
            self._open["stage"] = "delta"
        elif kind == "message_stop":
            call, self._open = self._open, None
            if call is None or call["stage"] != "delta" or call["output"] is None or not call["ok"]:
                self._bad = True
            else:
                self.calls.append(call)

    def _assistant(self, msg: object) -> None:
        """Only checked against its call's start; never counted."""
        usage = msg.get("usage") if isinstance(msg, dict) and isinstance(msg.get("usage"), dict) else None
        start = self._starts.get(msg.get("id")) if usage is not None and isinstance(msg.get("id"), str) else None
        if start is not None and any(_count(usage.get(k)) != start[k] for k in _INPUT):
            self._bad = True

    def reading(self) -> dict | None:
        """``{"tokens", "window"}`` for the last complete call, or None. ``window`` is None unless
        the result event states it for that call's own model (never guessed)."""
        res = self._result
        if res is None or self._results != 1 or self._open is not None or self._bad:
            return None
        if res["num_turns"] != len(self.calls):
            return None
        expected = res["usage"]
        if None in expected.values() or any(
                sum(c.get(k) or 0 for c in self.calls) != expected[k] for k in _INPUT) or (
                sum(c["output"] for c in self.calls) != expected["output_tokens"]):
            return None
        current = self.calls[self._first_after_compact:]
        if not current:
            return None
        last = current[-1]
        window = res["windows"].get(last["model"]) if isinstance(last["model"], str) else None
        return {"tokens": sum(last[k] for k in _INPUT), "window": window or None}
