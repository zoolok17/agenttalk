"""Shared redaction helpers for bounded diagnostic text."""

from __future__ import annotations

import re
from typing import Any

CHILD_OUTPUT_TAIL_MAX_BYTES = 4096
CHILD_OUTPUT_TAIL_MAX_LINES = 64

_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[-_ ]?key|token|secret|password|credential)"
    r"(\s*[:=]\s*)"
    r"(?:[A-Za-z]+\s+(?:[A-Za-z0-9._~+/=-]{8,}|\[REDACTED\])|"
    r"\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s'\",;]+)"
)
_AUTHORIZATION_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(authorization)(\s*[:=]\s*)"
    r"(?:\"[^\"\r\n;]*\"|'[^'\r\n;]*'|[^,;\r\n]+)"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[^\s,;\r\n]+")
_OPENAI_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9._-]{8,}\b")


def redact_diagnostic_text(value: object) -> str:
    """Redact common secret-bearing tokens while preserving diagnostic context."""
    text = str(value or "")
    text = _AUTHORIZATION_ASSIGNMENT_RE.sub(
        lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]",
        text,
    )
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    text = _SECRET_ASSIGNMENT_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", text)
    return _OPENAI_KEY_RE.sub("sk-[REDACTED]", text)


def _tail_bytes(lines: list[dict[str, str]]) -> int:
    return sum(
        len(f"[{line['stream']}] {line['text']}\n".encode("utf-8", "replace"))
        for line in lines
    )


def _trim_left_to_bytes(text: str, budget: int) -> str:
    marker = "[truncated] "
    marker_bytes = marker.encode("utf-8")
    raw = text.encode("utf-8", "replace")
    if len(raw) <= budget:
        return text
    if budget <= len(marker_bytes):
        return marker[:budget]
    kept = raw[-(budget - len(marker_bytes)):].decode("utf-8", "replace")
    return marker + kept


def _normalize_tail_line(item: object) -> dict[str, str] | None:
    stream = "stdout"
    text: object = ""
    if isinstance(item, dict):
        raw_stream = item.get("stream")
        if raw_stream in {"stdout", "stderr"}:
            stream = raw_stream
        text = item.get("text")
        if text is None:
            text = item.get("line", "")
    else:
        text = item
    clean = redact_diagnostic_text(text).rstrip("\r\n")
    if not clean:
        return None
    return {"stream": stream, "text": clean}


def normalize_child_output_tail(
    value: object,
    *,
    max_bytes: int = CHILD_OUTPUT_TAIL_MAX_BYTES,
    max_lines: int = CHILD_OUTPUT_TAIL_MAX_LINES,
) -> dict[str, Any] | None:
    """Return a bounded, redacted child-output tail schema, or None when empty."""
    items: object
    prior_truncated = False
    if isinstance(value, dict):
        items = value.get("lines")
        if items is None:
            items = value.get("items")
        prior_truncated = value.get("truncated") is True
    else:
        items = value
    if not isinstance(items, list):
        return None
    lines = [line for line in (_normalize_tail_line(item) for item in items) if line is not None]
    if not lines:
        return None
    max_lines = max(1, int(max_lines))
    max_bytes = max(256, int(max_bytes))
    truncated = prior_truncated
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
        truncated = True
    while lines and _tail_bytes(lines) > max_bytes:
        truncated = True
        if len(lines) == 1:
            overhead = _tail_bytes([{"stream": lines[0]["stream"], "text": ""}])
            lines[0]["text"] = _trim_left_to_bytes(lines[0]["text"], max(0, max_bytes - overhead))
            break
        lines.pop(0)
    return {
        "schema_version": 1,
        "max_bytes": max_bytes,
        "max_lines": max_lines,
        "truncated": truncated,
        "lines": lines,
        "bytes": _tail_bytes(lines),
    }
