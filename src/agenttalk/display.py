"""Pretty-print messages so both CLIs render them legibly in their terminals."""

from __future__ import annotations

from agenttalk.store import Message

_BAR = "=" * 72


def render(msg: Message, *, header: str | None = None) -> str:
    """Return a multi-line block suitable for printing to a terminal."""
    title = header or f"AGENTTALK :: {msg.sender}  ->  {msg.recipient}"
    meta_bits = []
    if msg.kind and msg.kind != "message":
        meta_bits.append(f"kind={msg.kind}")
    if msg.meta:
        for k, v in msg.meta.items():
            meta_bits.append(f"{k}={v}")
    meta_line = "  ".join(meta_bits)
    parts = [
        _BAR,
        title,
        f"  id      {msg.id}",
        f"  at      {msg.ts}",
    ]
    if msg.subject:
        parts.append(f"  subject {msg.subject}")
    if meta_line:
        parts.append(f"  meta    {meta_line}")
    parts.append("-" * 72)
    parts.append(msg.body.rstrip() if msg.body else "(empty body)")
    parts.append(_BAR)
    return "\n".join(parts)
