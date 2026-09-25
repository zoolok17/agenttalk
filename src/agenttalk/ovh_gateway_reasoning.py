"""Reasoning handling for the ovh-qwen route.

Two independent pieces, both free of ledger and network state:

* A validated, closed grammar for the fixed reasoning request parameters that
  ``render_litellm_config`` places under the deployment's ``extra_body``.
* ``SseReasoningStripper`` / ``strip_reasoning_json``: remove Anthropic
  ``thinking`` content blocks from the response before the Claude CLI sees them,
  so reasoning is never carried forward as conversation input on later calls.
  Usage events are never touched, so the ledger still settles on the provider's
  real (reasoning-inclusive) token counts.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

_NAME_RE = re.compile(r"^(?:chat_template_kwargs\.)?[a-z][a-z0-9_]{0,47}$")
_INT_RE = re.compile(r"^-?[0-9]{1,7}$")
_WORD_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_NESTED_PREFIX = "chat_template_kwargs."

# Request keys that must never be settable through this route: they change what
# is sent, where it goes, or how it is accounted, not how hard the model thinks.
RESERVED_PARAM_NAMES = frozenset({
    "api_base", "api_key", "base_url", "custom_llm_provider", "deployment_id",
    "extra_body", "extra_headers", "fallbacks", "function_call", "functions",
    "headers", "input", "max_completion_tokens", "max_retries", "max_tokens",
    "messages", "metadata", "model", "model_list", "n", "num_retries",
    "response_format", "router_settings", "store", "stream", "stream_options",
    "tool_choice", "tools", "user", "chat_template_kwargs",
})

ReasoningValue = bool | int | str


class ReasoningParamError(ValueError):
    """A reasoning request parameter is malformed or not allowed."""


def _parse_value(text: str) -> ReasoningValue:
    if text == "true":
        return True
    if text == "false":
        return False
    if _INT_RE.fullmatch(text):
        return int(text)
    if _WORD_RE.fullmatch(text):
        return text
    raise ReasoningParamError(
        "reasoning parameter value must be true, false, an integer of at most "
        "seven digits, or a short lowercase word"
    )


def _check_name(name: object) -> str:
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise ReasoningParamError(
            "reasoning parameter name must be lowercase letters, digits and "
            "underscores, optionally prefixed 'chat_template_kwargs.'"
        )
    bare = name[len(_NESTED_PREFIX):] if name.startswith(_NESTED_PREFIX) else name
    if name in RESERVED_PARAM_NAMES or (
        not name.startswith(_NESTED_PREFIX) and bare in RESERVED_PARAM_NAMES
    ):
        raise ReasoningParamError(f"reasoning parameter name {name!r} is reserved")
    return name


def parse_reasoning_param(text: str) -> tuple[str, ReasoningValue]:
    """Parse one ``NAME=VALUE`` command-line item."""
    if not isinstance(text, str) or "=" not in text or len(text) > 96:
        raise ReasoningParamError("reasoning parameter must be NAME=VALUE")
    name, _, raw = text.partition("=")
    return _check_name(name.strip()), _parse_value(raw.strip())


def validate_reasoning_params(params: object) -> dict[str, ReasoningValue]:
    """Return a normalised, name-sorted copy, or raise ``ReasoningParamError``."""
    if params is None:
        return {}
    if not isinstance(params, Mapping):
        raise ReasoningParamError("reasoning parameters must be a mapping")
    if len(params) > 8:
        raise ReasoningParamError("at most eight reasoning parameters are allowed")
    out: dict[str, ReasoningValue] = {}
    for name in sorted(params):
        value = params[name]
        _check_name(name)
        if isinstance(value, bool):
            out[name] = value
        elif isinstance(value, int):
            if not -9_999_999 <= value <= 9_999_999:
                raise ReasoningParamError("reasoning parameter integer is out of range")
            out[name] = value
        elif isinstance(value, str) and _WORD_RE.fullmatch(value):
            out[name] = value
        else:
            raise ReasoningParamError("reasoning parameter value is not allowed")
    return out


def parse_reasoning_params(items: list[str] | None) -> dict[str, ReasoningValue]:
    parsed: dict[str, ReasoningValue] = {}
    for item in items or []:
        name, value = parse_reasoning_param(item)
        if name in parsed:
            raise ReasoningParamError(f"reasoning parameter {name!r} given twice")
        parsed[name] = value
    return validate_reasoning_params(parsed)


def _yaml_scalar(value: ReasoningValue) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return json.dumps(value)


def render_extra_body_lines(
    params: Mapping[str, ReasoningValue] | None, *, indent: str
) -> str:
    """YAML lines for the validated parameters, one line per top-level key."""
    checked = validate_reasoning_params(params)
    top = {k: v for k, v in checked.items() if not k.startswith(_NESTED_PREFIX)}
    nested = {
        k[len(_NESTED_PREFIX):]: v
        for k, v in checked.items()
        if k.startswith(_NESTED_PREFIX)
    }
    lines = [f"{indent}{k}: {_yaml_scalar(v)}\n" for k, v in top.items()]
    if nested:
        lines.append(f"{indent}chat_template_kwargs:\n")
        lines.extend(f"{indent}  {k}: {_yaml_scalar(v)}\n" for k, v in nested.items())
    return "".join(lines)


# --------------------------------------------------------------------------
# Response side: drop thinking blocks, keep everything else byte-for-byte.

THINKING_BLOCK_TYPES = frozenset({"thinking", "redacted_thinking"})
THINKING_DELTA_TYPES = frozenset({"thinking_delta", "signature_delta"})
MAX_EVENT_BYTES = 1024 * 1024
# Well inside the Claude CLI's stream-idle watchdogs (minutes), well outside
# per-event chatter.
PING_INTERVAL_SECONDS = 15.0


@dataclass
class ReasoningStats:
    blocks: int = 0
    chars: int = 0

    def __bool__(self) -> bool:
        return bool(self.blocks or self.chars)


class SseReasoningStripper:
    """Incremental SSE filter that removes Anthropic thinking blocks.

    Feed upstream bytes in any chunking; each call returns the bytes to forward.
    Events that are not thinking blocks pass through unchanged, except that
    ``index`` is renumbered so the kept blocks stay contiguous. If a single event
    exceeds ``MAX_EVENT_BYTES`` the filter stops filtering and forwards raw
    bytes from then on, so a parsing surprise can never stall or corrupt a turn.

    While reasoning is being dropped the client would otherwise see no bytes at
    all for the whole reasoning stretch, and the Claude CLI has stream-idle
    watchdogs. So a standard Anthropic ``ping`` event is emitted in place of a
    dropped event whenever nothing has been forwarded for ``ping_interval``
    seconds; clients ignore pings by protocol.
    """

    def __init__(
        self,
        *,
        ping_interval: float = PING_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.stats = ReasoningStats()
        self._buffer = bytearray()
        self._dropped: set[int] = set()
        self._map: dict[int, int] = {}
        self._next = 0
        self._passthrough = False
        self._ping_interval = ping_interval
        self._clock = clock
        self._last_out = clock()

    def _after_drop(self, terminator: bytes) -> bytes:
        now = self._clock()
        if now - self._last_out < self._ping_interval:
            return b""
        self._last_out = now
        eol = b"\r\n" if terminator.startswith(b"\r\n") else b"\n"
        return b"event: ping" + eol + b'data: {"type": "ping"}' + terminator

    @staticmethod
    def _boundary(buffer: bytearray) -> tuple[int, int] | None:
        candidates = []
        for sep in (b"\n\n", b"\r\n\r\n"):
            at = buffer.find(sep)
            if at != -1:
                candidates.append((at, len(sep)))
        return min(candidates) if candidates else None

    def feed(self, chunk: bytes) -> bytes:
        if self._passthrough:
            return bytes(chunk)
        self._buffer.extend(chunk)
        out = bytearray()
        while True:
            found = self._boundary(self._buffer)
            if found is None:
                break
            at, size = found
            block = bytes(self._buffer[: at + size])
            del self._buffer[: at + size]
            out.extend(self._process(block, block[at:]))
        if len(self._buffer) > MAX_EVENT_BYTES:
            self._passthrough = True
            out.extend(self._buffer)
            self._buffer.clear()
        if out:
            self._last_out = self._clock()
        return bytes(out)

    def finish(self) -> bytes:
        rest = bytes(self._buffer)
        self._buffer.clear()
        return rest

    def _process(self, block: bytes, terminator: bytes) -> bytes:
        try:
            text = block[: len(block) - len(terminator)].decode("utf-8")
        except UnicodeDecodeError:
            return block
        eol = "\r\n" if terminator.startswith(b"\r\n") else "\n"
        lines = text.split(eol)
        data_at = [i for i, line in enumerate(lines) if line.startswith("data:")]
        if not data_at:
            return block
        try:
            obj = json.loads("\n".join(lines[i][5:].strip() for i in data_at))
        except ValueError:
            return block
        if not isinstance(obj, dict):
            return block
        kind = obj.get("type")
        index = obj.get("index")
        if kind == "content_block_start":
            start = obj.get("content_block")
            if isinstance(start, dict) and start.get("type") in THINKING_BLOCK_TYPES:
                if isinstance(index, int):
                    self._dropped.add(index)
                self.stats.blocks += 1
                return self._after_drop(terminator)
            if isinstance(index, int):
                new_index = self._next
                self._next += 1
                self._map[index] = new_index
                return self._rewrite(block, lines, data_at, obj, new_index, eol, terminator)
            return block
        if kind == "content_block_delta":
            delta = obj.get("delta")
            delta_type = delta.get("type") if isinstance(delta, dict) else None
            if delta_type in THINKING_DELTA_TYPES:
                thinking = delta.get("thinking") if isinstance(delta, dict) else None
                if isinstance(thinking, str):
                    self.stats.chars += len(thinking)
                return self._after_drop(terminator)
            if isinstance(index, int) and index in self._dropped:
                return self._after_drop(terminator)
            if isinstance(index, int) and index in self._map:
                return self._rewrite(
                    block, lines, data_at, obj, self._map[index], eol, terminator
                )
            return block
        if kind == "content_block_stop":
            if isinstance(index, int) and index in self._dropped:
                self._dropped.discard(index)
                return self._after_drop(terminator)
            if isinstance(index, int) and index in self._map:
                return self._rewrite(
                    block, lines, data_at, obj, self._map.pop(index), eol, terminator
                )
            return block
        return block

    @staticmethod
    def _rewrite(
        block: bytes,
        lines: list[str],
        data_at: list[int],
        obj: dict,
        new_index: int,
        eol: str,
        terminator: bytes,
    ) -> bytes:
        if obj.get("index") == new_index:
            return block
        obj = dict(obj)
        obj["index"] = new_index
        payload = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
        rebuilt: list[str] = []
        for i, line in enumerate(lines):
            if i == data_at[0]:
                rebuilt.append("data: " + payload)
            elif i not in data_at:
                rebuilt.append(line)
        return eol.join(rebuilt).encode("utf-8") + terminator


def strip_reasoning_json(body: bytes) -> tuple[bytes, ReasoningStats]:
    """Remove thinking blocks from a non-streaming Anthropic message."""
    stats = ReasoningStats()
    try:
        obj = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return body, stats
    if not isinstance(obj, dict) or not isinstance(obj.get("content"), list):
        return body, stats
    kept = []
    for block in obj["content"]:
        if isinstance(block, dict) and block.get("type") in THINKING_BLOCK_TYPES:
            stats.blocks += 1
            thinking = block.get("thinking")
            if isinstance(thinking, str):
                stats.chars += len(thinking)
            continue
        kept.append(block)
    if not stats:
        return body, stats
    obj["content"] = kept
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8"), stats


class ReasoningStripLog:
    """Bounded, content-free record of how much reasoning was removed per attempt."""

    MAX_BYTES = 1024 * 1024

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def record(self, attempt_id: str, stats: ReasoningStats) -> None:
        line = json.dumps(
            {
                "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "attempt_id": attempt_id,
                "blocks": stats.blocks,
                "chars": stats.chars,
            },
            separators=(",", ":"),
        )
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size >= self.MAX_BYTES:
                    os.replace(self.path, self.path.with_name(self.path.name + ".1"))
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError:
                return
