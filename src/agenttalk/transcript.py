"""Export the full conversation as markdown or JSONL."""

from __future__ import annotations

import json
from pathlib import Path

from agenttalk._atomic import write_text as _atomic_write_text
from agenttalk.store import Message, Store


def to_markdown(msgs: list[Message]) -> str:
    if not msgs:
        return "# agenttalk transcript\n\n_(no messages)_\n"
    lines = ["# agenttalk transcript", ""]
    for m in msgs:
        head = f"## {m.ts} — {m.sender} → {m.recipient}"
        if m.kind and m.kind != "message":
            head += f"  *({m.kind})*"
        lines.append(head)
        if m.subject:
            lines.append(f"**Subject:** {m.subject}")
        if m.meta:
            meta = ", ".join(f"`{k}={v}`" for k, v in m.meta.items())
            lines.append(f"**Meta:** {meta}")
        lines.append("")
        body = m.body.rstrip() if m.body else "_(empty body)_"
        lines.append(body)
        lines.append("")
        lines.append("---")
        lines.append("")
    return "\n".join(lines)


def to_jsonl(msgs: list[Message]) -> str:
    return "\n".join(json.dumps(m.to_dict(), ensure_ascii=False) for m in msgs) + ("\n" if msgs else "")


def export(store: Store, *, fmt: str = "md", out: Path | None = None) -> Path:
    msgs = store.all_messages()
    if fmt == "md":
        text = to_markdown(msgs)
        default_name = f"transcript-{store.load_config().get('session_id', 'session')}.md"
    elif fmt == "jsonl":
        text = to_jsonl(msgs)
        default_name = f"transcript-{store.load_config().get('session_id', 'session')}.jsonl"
    else:
        raise ValueError(f"unknown format: {fmt!r}")
    out = out or (store.sessions_dir / default_name)
    # Atomic + crash-safe like every other bus write — `agenttalk end` always
    # writes here, so a crash mid-export must not leave a truncated session
    # record (review batch-3 nit).
    _atomic_write_text(out, text)
    return out
