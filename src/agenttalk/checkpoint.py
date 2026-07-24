"""Durable external-state checkpoints for context compaction hooks.

This module deliberately captures only state AgentTalk can observe
deterministically: the shared capacity signal, validated bus threads, and Git
plumbing. It does not attempt to infer or serialize model reasoning.
"""

from __future__ import annotations

import json
import re
import subprocess  # nosec B404 - fixed Git argv lists; shell is never used
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, TextIO

from agenttalk import capacity as capmod
from agenttalk import threads as th
from agenttalk._atomic import write_text as _atomic_write_text
from agenttalk.store import Store, validate_agent_name


HISTORY_LIMIT = 10
HOOK_STDIN_LIMIT = 1024 * 1024
CHECKPOINT_READ_LIMIT = 4 * 1024 * 1024
GIT_TIMEOUT_SECONDS = 2.0
RELOAD_POINTERS = (
    "memory/dashboard-control-plane.md",
    "memory/MEMORY.md",
)


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def read_hook_payload(stream: TextIO | None = None) -> dict:
    """Read one bounded hook JSON object, returning ``{}`` on malformed input."""
    stream = stream or sys.stdin
    raw_stream: BinaryIO | TextIO = getattr(stream, "buffer", stream)
    try:
        raw = raw_stream.read(HOOK_STDIN_LIMIT + 1)
    except (OSError, ValueError):
        return {}
    if isinstance(raw, str):
        text = raw
        size = len(raw.encode("utf-8", errors="replace"))
    elif isinstance(raw, bytes):
        size = len(raw)
        text = raw.decode("utf-8", errors="replace")
    else:
        return {}
    if size > HOOK_STDIN_LIMIT or not text.strip():
        return {}
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def collect_context(
    agent: str,
    *,
    source: str,
    session_id: str | None = None,
    session_scoped: bool = False,
) -> dict:
    """Project Phase-1's capacity snapshot into the checkpoint contract."""
    try:
        if session_scoped:
            snapshot = (
                capmod.read_claude_context_sidecar(
                    agent,
                    session_id=session_id,
                )
                if session_id is not None
                else None
            )
            snapshot = snapshot or capmod.CapacitySnapshot.unknown(agent)
        else:
            snapshot = capmod.read_local(agent, source=source)
    except Exception:  # noqa: BLE001 - a missing signal never blocks compaction
        snapshot = capmod.CapacitySnapshot.unknown(agent)
    pct = snapshot.context_used_percent
    limit = snapshot.context_window_size
    used = snapshot.context_tokens
    has_context = any(value is not None for value in (pct, limit, used))
    return {
        "pct": pct,
        "limit": limit,
        "used": used,
        "source": "sidecar" if has_context else None,
    }


def _closed_request_ids(store: Store, agent: str) -> set[str]:
    return {
        request_id
        for request_id, state in store.read_threadstate(agent).items()
        if isinstance(state, dict) and state.get("closed") is True
    }


def _format_age(age_seconds: float | None) -> str:
    if age_seconds is None or age_seconds < 0:
        return "?"
    seconds = int(age_seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h"
    return f"{hours // 24}d"


def collect_bus_state(store: Store, agent: str) -> dict:
    """Project the same validated thread derivation used by sync/status."""
    rows = th.derive_threads(
        store.valid_messages(),
        agent=agent,
        cursor=store.cursor(agent),
        closed_rids=_closed_request_ids(store, agent),
        retired=set(store.retired_agents()),
    )
    actionable = [row for row in rows if row.state in th.ACTIONABLE_STATES]
    owed_out = [
        {
            "id": row.request_id,
            "to": row.peer,
            "kind": row.opener_kind,
            "age": _format_age(row.age_seconds),
        }
        for row in actionable
        if row.state == "open-outbound"
    ]
    owed_in = [
        {
            "id": row.request_id,
            "from": row.peer,
            "kind": row.opener_kind,
        }
        for row in actionable
        if row.state in {"owed-inbound", "reply-waiting"}
    ]
    return {
        "unread": len(store.unread_for(agent)),
        "owed_out": owed_out,
        "owed_in": owed_in,
        "in_flight_threads": [row.request_id for row in actionable],
    }


def _git_output(root: Path, *args: str) -> str | None:
    try:
        completed = subprocess.run(  # noqa: S603,S607  # nosec B603 B607
            ["git", "-C", str(root), *args],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def collect_git_state(root: Path) -> dict:
    """Read cheap Git plumbing, degrading every unavailable field to null."""
    head = _git_output(root, "rev-parse", "--verify", "HEAD")
    if not head:
        return {"head": None, "branch": None, "dirty_files": None}
    branch = _git_output(root, "rev-parse", "--abbrev-ref", "HEAD")
    status = _git_output(
        root,
        "status",
        "--porcelain",
        "--untracked-files=normal",
    )
    return {
        "head": head,
        "branch": branch or None,
        "dirty_files": len(status.splitlines()) if status is not None else None,
    }


def _safe_hook_text(value: object, *, limit: int = 512) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    clean = "".join(ch for ch in value if ch >= " " and ch != "\x7f").strip()
    return clean[:limit] or None


def build_checkpoint(
    store: Store,
    agent: str,
    *,
    hook_payload: dict | None = None,
    trigger: str = "manual",
    capacity_source: str = "auto",
    session_scoped_context: bool = False,
) -> dict:
    validate_agent_name(agent)
    hook_payload = hook_payload if isinstance(hook_payload, dict) else {}
    hook_trigger = hook_payload.get("trigger")
    if hook_trigger in {"auto", "manual"}:
        trigger = hook_trigger
    if trigger not in {"auto", "manual"}:
        trigger = "manual"
    config = store.load_config()
    session_id = _safe_hook_text(hook_payload.get("session_id"))
    if session_id is None:
        session_id = _safe_hook_text(config.get("session_id"))
    try:
        git_state = collect_git_state(store.root)
    except Exception:  # noqa: BLE001 - preserve a partial checkpoint
        git_state = {"head": None, "branch": None, "dirty_files": None}
    try:
        bus_state = collect_bus_state(store, agent)
    except Exception:  # noqa: BLE001 - preserve a partial checkpoint
        bus_state = {
            "unread": None,
            "owed_out": [],
            "owed_in": [],
            "in_flight_threads": [],
        }
    return {
        "agent": agent,
        "session_id": session_id,
        "trigger": trigger,
        "saved_at": utc_now(),
        "context": collect_context(
            agent,
            source=capacity_source,
            session_id=session_id,
            session_scoped=session_scoped_context,
        ),
        "git": git_state,
        "bus": bus_state,
        "reload_pointers": list(RELOAD_POINTERS),
    }


def _checkpoint_dir(store: Store) -> Path:
    return store.dir / "checkpoints"


def checkpoint_path(store: Store, agent: str) -> Path:
    validate_agent_name(agent)
    return _checkpoint_dir(store) / f"{agent}.json"


def _read_json_object(path: Path, *, expected_agent: str) -> dict | None:
    try:
        with path.open("rb") as handle:
            raw = handle.read(CHECKPOINT_READ_LIMIT + 1)
        if len(raw) > CHECKPOINT_READ_LIMIT:
            return None
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("agent") != expected_agent:
        return None
    return payload


def read_checkpoint(store: Store, agent: str) -> dict | None:
    return _read_json_object(
        checkpoint_path(store, agent),
        expected_agent=agent,
    )


def _history_path(history_dir: Path, prior: dict) -> Path:
    stamp = prior.get("saved_at")
    stamp = stamp if isinstance(stamp, str) and stamp else "unknown-time"
    base = re.sub(r"[^A-Za-z0-9_.-]+", "-", stamp).strip("-") or "checkpoint"
    candidate = history_dir / f"{base}.json"
    suffix = 1
    while candidate.exists():
        candidate = history_dir / f"{base}-{suffix:03d}.json"
        suffix += 1
    return candidate


def save_checkpoint(store: Store, agent: str, payload: dict) -> Path:
    """Atomically replace latest and retain the previous ten snapshots."""
    validate_agent_name(agent)
    if not isinstance(payload, dict) or payload.get("agent") != agent:
        raise ValueError("checkpoint payload agent does not match target")
    serialized = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    latest = checkpoint_path(store, agent)
    history_dir = _checkpoint_dir(store) / "history" / agent
    with store.config_lock(timeout=2.0, poll=0.02):
        prior = _read_json_object(latest, expected_agent=agent)
        history_dir.mkdir(parents=True, exist_ok=True)
        if prior is not None:
            archive = _history_path(history_dir, prior)
            _atomic_write_text(
                archive,
                json.dumps(prior, indent=2, ensure_ascii=False) + "\n",
            )
        _atomic_write_text(latest, serialized)
        history = sorted(history_dir.glob("*.json"), key=lambda path: path.name)
        for stale in history[:-HISTORY_LIMIT]:
            try:
                stale.unlink()
            except FileNotFoundError:
                pass
    return latest


def _summary_value(value: object, *, fallback: str = "unknown") -> str:
    if isinstance(value, bool) or value is None:
        return fallback
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, str):
        clean = "".join(
            ch if ch >= " " and ch != "\x7f" else " " for ch in value
        )
        clean = " ".join(clean.split())
        return clean[:256] or fallback
    return fallback


def _summary_ids(rows: object, *, key: str = "id") -> str:
    if not isinstance(rows, list):
        return "-"
    values: list[str] = []
    for row in rows[:50]:
        value = row.get(key) if isinstance(row, dict) else row
        clean = _summary_value(value, fallback="")
        if clean:
            values.append(clean)
    if len(rows) > 50:
        values.append(f"+{len(rows) - 50}more")
    return ",".join(values) or "-"


def render_resume_context(payload: dict | None) -> str:
    if not isinstance(payload, dict):
        return ""
    agent = _summary_value(payload.get("agent"))
    trigger = _summary_value(payload.get("trigger"))
    saved_at = _summary_value(payload.get("saved_at"))
    git = payload.get("git") if isinstance(payload.get("git"), dict) else {}
    context = (
        payload.get("context") if isinstance(payload.get("context"), dict) else {}
    )
    bus = payload.get("bus") if isinstance(payload.get("bus"), dict) else {}
    owed_in = bus.get("owed_in") if isinstance(bus.get("owed_in"), list) else []
    owed_out = bus.get("owed_out") if isinstance(bus.get("owed_out"), list) else []
    in_flight = (
        bus.get("in_flight_threads")
        if isinstance(bus.get("in_flight_threads"), list)
        else []
    )
    pointers = payload.get("reload_pointers")
    pointers = pointers if isinstance(pointers, list) else list(RELOAD_POINTERS)
    pointer_text = " + ".join(
        value
        for value in (
            _summary_value(pointer, fallback="") for pointer in pointers[:10]
        )
        if value
    )
    if not pointer_text:
        pointer_text = " + ".join(RELOAD_POINTERS)
    return (
        f"Checkpoint reload for {agent}: trigger={trigger}; saved_at={saved_at}; "
        f"git={_summary_value(git.get('branch'))}@"
        f"{_summary_value(git.get('head'))} "
        f"(dirty_files={_summary_value(git.get('dirty_files'))}); "
        f"context={_summary_value(context.get('pct'))}% "
        f"({_summary_value(context.get('used'))}/"
        f"{_summary_value(context.get('limit'))}, "
        f"source={_summary_value(context.get('source'))}); "
        f"bus=unread:{_summary_value(bus.get('unread'))}, "
        f"owed_in:{len(owed_in)} [{_summary_ids(owed_in)}], "
        f"owed_out:{len(owed_out)} [{_summary_ids(owed_out)}], "
        f"in_flight:[{_summary_ids(in_flight)}]. "
        f"Before continuing, re-read {pointer_text}."
    )


def session_start_output(payload: dict | None) -> str:
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": render_resume_context(payload),
            }
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def log_hook_error(root: Path | None, action: str, error: BaseException) -> None:
    """Best-effort hook diagnostics; never writes to stdout/stderr or raises."""
    if root is None:
        return
    try:
        store_dir = root / ".agenttalk"
        if not store_dir.is_dir():
            return
        log_path = store_dir / "checkpoints" / "checkpoint-errors.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        line = (
            f"{utc_now()} action={_summary_value(action)} "
            f"error={type(error).__name__}:"
            f"{_summary_value(str(error), fallback='unknown')}\n"
        )
        mode = "w" if log_path.exists() and log_path.stat().st_size > 256 * 1024 else "a"
        with log_path.open(mode, encoding="utf-8", newline="\n") as handle:
            handle.write(line)
    except Exception:  # noqa: BLE001 - diagnostics must never block a hook
        return
