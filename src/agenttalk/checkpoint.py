"""Durable external-state checkpoints for context compaction hooks.

This module deliberately captures only state AgentTalk can observe
deterministically: the shared capacity signal, validated bus threads, and Git
plumbing. It does not attempt to infer or serialize model reasoning.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess  # nosec B404 - fixed Git argv lists; shell is never used
import sys
import threading
import time
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import BinaryIO, Callable, TextIO, TypeVar

from agenttalk import capacity as capmod
from agenttalk import signing as signing_mod
from agenttalk import threads as th
from agenttalk._atomic import write_text as _atomic_write_text
from agenttalk.store import (
    Message,
    Store,
    _ID_RE,
    validate_agent_name,
    validate_agent_roster,
    validate_retired,
)


HISTORY_LIMIT = 10
HOOK_STDIN_LIMIT = 1024 * 1024
HOOK_STDIN_TIMEOUT_SECONDS = 1.0
CHECKPOINT_READ_LIMIT = 4 * 1024 * 1024
CHECKPOINT_CONFIG_READ_LIMIT = 1024 * 1024
SIGNING_KEY_READ_LIMIT = 4096
GIT_TIMEOUT_SECONDS = 2.0
BUS_DEADLINE_SECONDS = 2.0
BUS_MAX_FILES = 512
BUS_MAX_TOTAL_BYTES = 8 * 1024 * 1024
BUS_MAX_MESSAGE_BYTES = 512 * 1024
CURSOR_READ_LIMIT = 256
THREADSTATE_READ_LIMIT = 1024 * 1024
RELOAD_POINTERS: tuple[str, ...] = ()
SUMMARY_ID_LIMIT = 96
_SUMMARY_ID_RE = re.compile(
    rf"\A[A-Za-z0-9][A-Za-z0-9_.-]{{0,{SUMMARY_ID_LIMIT - 1}}}\Z"
)
EMPTY_SESSION_START_OUTPUT = (
    '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":""}}'
)
_T = TypeVar("_T")


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def run_bounded(
    callback: Callable[[], _T],
    *,
    timeout: float,
) -> tuple[_T | None, BaseException | None, bool]:
    """Run callback in a daemon thread and stop waiting at ``timeout``.

    The thread may remain blocked in an OS read, but it cannot keep the hook
    process alive. Callers treat timeout exactly like unavailable input.
    """
    result: list[tuple[_T | None, BaseException | None]] = []

    def invoke() -> None:
        try:
            result.append((callback(), None))
        except BaseException as exc:  # hook boundaries include interrupts
            result.append((None, exc))

    worker = threading.Thread(target=invoke, daemon=True)
    try:
        worker.start()
        worker.join(max(0.0, timeout))
    except BaseException as exc:
        return None, exc, False
    if worker.is_alive():
        return None, TimeoutError(f"operation exceeded {timeout:g}s"), True
    if not result:
        return None, RuntimeError("bounded operation ended without a result"), False
    value, error = result[0]
    return value, error, False


def _read_hook_descriptor(descriptor: int) -> bytes:
    """Read from a detached descriptor without retaining ``sys.stdin``."""
    try:
        chunks: list[bytes] = []
        remaining = HOOK_STDIN_LIMIT + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


def read_hook_payload(stream: TextIO | None = None) -> dict:
    """Read one bounded hook JSON object, returning ``{}`` on malformed input."""
    stream = stream or sys.stdin
    raw_stream: BinaryIO | TextIO = getattr(stream, "buffer", stream)
    descriptor: int | None = None
    try:
        descriptor = os.dup(raw_stream.fileno())
    except (AttributeError, OSError, ValueError):
        reader = partial(raw_stream.read, HOOK_STDIN_LIMIT + 1)
    else:
        reader = partial(_read_hook_descriptor, descriptor)
    raw, error, _timed_out = run_bounded(
        reader,
        timeout=HOOK_STDIN_TIMEOUT_SECONDS,
    )
    if error is not None:
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


def _read_regular_bytes(path: Path, *, max_bytes: int) -> bytes | None:
    """Read a bounded regular file without ever opening a known special file."""
    try:
        before = path.stat(follow_symlinks=False)
    except (OSError, ValueError):
        return None
    if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
        return None
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except (OSError, ValueError):
        return None
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_size > max_bytes:
            return None
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        return raw if len(raw) <= max_bytes else None
    except OSError:
        return None
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


def read_checkpoint_config(store: Store) -> dict:
    """Read the small roster subset needed by hooks from a regular file."""
    raw = _read_regular_bytes(
        store.config_path,
        max_bytes=CHECKPOINT_CONFIG_READ_LIMIT,
    )
    if raw is None:
        raise ValueError("checkpoint config is absent, special, or oversized")
    try:
        config = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise ValueError("checkpoint config is malformed") from exc
    if not isinstance(config, dict) or not isinstance(config.get("agents"), list):
        raise ValueError("checkpoint config has no valid agent roster")
    validate_agent_roster(config["agents"])
    if config.get("retired") is not None:
        validate_retired(config["retired"], config["agents"])
    return config


def _checkpoint_signing_key(store: Store) -> tuple[bool, str, bytes | None]:
    project_id = store.project_id()
    path = signing_mod.resolve_key_path(project_id)
    try:
        path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return False, project_id, None
    except (OSError, ValueError):
        return True, project_id, None
    raw = _read_regular_bytes(path, max_bytes=SIGNING_KEY_READ_LIMIT)
    if raw is None:
        return True, project_id, None
    try:
        key = bytes.fromhex(raw.decode("ascii").strip())
    except (UnicodeError, ValueError):
        return True, project_id, None
    return True, project_id, key if len(key) >= 16 else None


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


def _empty_bus(*, truncated: bool) -> dict:
    payload = {
        "unread": None if truncated else 0,
        "owed_out": [],
        "owed_in": [],
        "reply_waiting": [],
        "in_flight_threads": [],
    }
    if truncated:
        payload["truncated"] = True
    return payload


def _cursor_snapshot(store: Store, agent: str) -> tuple[str, bool]:
    path = store.state_dir / (validate_agent_name(agent) + ".cursor")
    try:
        path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return "", False
    except (OSError, ValueError):
        return "", True
    raw = _read_regular_bytes(path, max_bytes=CURSOR_READ_LIMIT)
    if raw is None:
        return "", True
    try:
        value = raw.decode("utf-8").strip()
    except UnicodeError:
        return "", True
    if value and _ID_RE.fullmatch(value) is None:
        return "", True
    return value, False


def _closed_request_ids(store: Store, agent: str) -> tuple[set[str], bool]:
    path = store.state_dir / (validate_agent_name(agent) + ".threadstate.json")
    try:
        path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return set(), False
    except (OSError, ValueError):
        return set(), True
    raw = _read_regular_bytes(path, max_bytes=THREADSTATE_READ_LIMIT)
    if raw is None:
        return set(), True
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        return set(), True
    if not isinstance(payload, dict):
        return set(), True
    return {
        request_id
        for request_id, state in payload.items()
        if isinstance(request_id, str)
        and isinstance(state, dict)
        and state.get("closed") is True
    }, False


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


def _bounded_valid_messages(
    store: Store,
    *,
    deadline: float,
) -> tuple[list[Message], dict, bool]:
    """Read a trust-gated, resource-bounded message snapshot."""
    try:
        config = read_checkpoint_config(store)
        roster = Store._known_roster(config)  # noqa: SLF001 - canonical history roster
    except (OSError, ValueError):
        return [], {}, True
    if not roster:
        return [], config, True
    require_signature, project_id, key = _checkpoint_signing_key(store)
    if require_signature and key is None:
        return [], config, True

    messages: list[Message] = []
    truncated = False
    files_seen = 0
    bytes_seen = 0
    try:
        entries = os.scandir(store.messages_dir)
    except (OSError, ValueError):
        return [], config, True
    with entries:
        for entry in entries:
            if time.monotonic() >= deadline:
                truncated = True
                break
            if not entry.name.endswith(".json"):
                continue
            if files_seen >= BUS_MAX_FILES:
                truncated = True
                break
            files_seen += 1
            path = Path(entry.path)
            try:
                info = path.stat(follow_symlinks=False)
            except (OSError, ValueError):
                truncated = True
                continue
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_size > BUS_MAX_MESSAGE_BYTES
            ):
                truncated = True
                continue
            if bytes_seen + info.st_size > BUS_MAX_TOTAL_BYTES:
                truncated = True
                break
            raw = _read_regular_bytes(path, max_bytes=BUS_MAX_MESSAGE_BYTES)
            if raw is None:
                truncated = True
                continue
            bytes_seen += len(raw)
            try:
                payload = json.loads(raw.decode("utf-8"))
                message = Message.from_raw(payload)
                if path.stem != message.id:
                    continue
                message.validate(roster)
                if require_signature:
                    signing_mod.verify_message(
                        message.to_dict(),
                        key,
                        expected_key_id=project_id,
                    )
            except (UnicodeError, ValueError):
                continue
            messages.append(message)

    messages.sort(key=lambda message: message.id)
    deduped: list[Message] = []
    seen_ids: set[str] = set()
    for message in messages:
        if message.id not in seen_ids:
            seen_ids.add(message.id)
            deduped.append(message)
    return deduped, config, truncated


def _collect_bus_state(store: Store, agent: str, *, deadline: float) -> dict:
    """Project the same validated thread derivation used by sync/status."""
    messages, config, truncated = _bounded_valid_messages(
        store,
        deadline=deadline,
    )
    if time.monotonic() >= deadline:
        return _empty_bus(truncated=True)
    cursor, cursor_truncated = _cursor_snapshot(store, agent)
    closed_rids, state_truncated = _closed_request_ids(store, agent)
    truncated = truncated or cursor_truncated or state_truncated
    rows = th.derive_threads(
        messages,
        agent=agent,
        cursor=cursor,
        closed_rids=closed_rids,
        retired=set(Store._retired_names(config)),  # noqa: SLF001
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
        if row.state == "owed-inbound"
    ]
    reply_waiting = [
        {
            "id": row.request_id,
            "from": row.peer,
            "kind": row.opener_kind,
        }
        for row in actionable
        if row.state == "reply-waiting"
    ]
    payload = {
        "unread": sum(
            1
            for message in messages
            if message.recipient == agent and message.id > cursor
        ),
        "owed_out": owed_out,
        "owed_in": owed_in,
        "reply_waiting": reply_waiting,
        "in_flight_threads": [row.request_id for row in actionable],
    }
    if truncated:
        payload["truncated"] = True
    return payload


def collect_bus_state(store: Store, agent: str) -> dict:
    """Return a wall-clock-bounded best-effort bus projection."""
    deadline = time.monotonic() + BUS_DEADLINE_SECONDS
    value, error, _timed_out = run_bounded(
        lambda: _collect_bus_state(store, agent, deadline=deadline),
        timeout=BUS_DEADLINE_SECONDS,
    )
    if error is not None or not isinstance(value, dict):
        return _empty_bus(truncated=True)
    return value


def _git_output(root: Path, *args: str) -> str | None:
    try:
        completed = subprocess.run(  # noqa: S603,S607  # nosec B603 B607
            ["git", "-C", str(root), *args],
            check=False,
            capture_output=True,
            stdin=subprocess.DEVNULL,
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
    config = (
        read_checkpoint_config(store)
        if session_scoped_context
        else store.load_config()
    )
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
        bus_state = _empty_bus(truncated=True)
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
        "reload_pointers": [
            checkpoint_path(store, agent).relative_to(store.root).as_posix(),
        ],
    }


def _checkpoint_dir(store: Store) -> Path:
    return store.dir / "checkpoints"


def checkpoint_path(store: Store, agent: str) -> Path:
    return _checkpoint_dir(store) / (validate_agent_name(agent) + ".json")


def _read_json_object(path: Path, *, expected_agent: str) -> dict | None:
    raw = _read_regular_bytes(path, max_bytes=CHECKPOINT_READ_LIMIT)
    if raw is None:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
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


def _make_history_room(history_dir: Path) -> bool:
    """Best-effort prune before admitting one more retained checkpoint."""
    target = max(0, HISTORY_LIMIT - 1)
    while True:
        history = sorted(history_dir.glob("*.json"), key=lambda path: path.name)
        if len(history) <= target:
            return True
        removed = False
        for stale in history:
            try:
                stale.unlink()
            except FileNotFoundError:
                removed = True
                break
            except OSError:
                continue
            else:
                removed = True
                break
        if not removed:
            return False


def save_checkpoint(store: Store, agent: str, payload: dict) -> Path:
    """Atomically replace latest and retain the previous ten snapshots."""
    safe_agent = validate_agent_name(agent)
    if not isinstance(payload, dict) or payload.get("agent") != agent:
        raise ValueError("checkpoint payload agent does not match target")
    serialized = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    latest = checkpoint_path(store, agent)
    history_dir = _checkpoint_dir(store) / "history" / safe_agent
    with store.config_lock(timeout=2.0, poll=0.02):
        prior = _read_json_object(latest, expected_agent=agent)
        history_dir.mkdir(parents=True, exist_ok=True)
        if prior is not None and HISTORY_LIMIT > 0 and _make_history_room(history_dir):
            archive = _history_path(history_dir, prior)
            try:
                _atomic_write_text(
                    archive,
                    json.dumps(prior, indent=2, ensure_ascii=False) + "\n",
                )
            except OSError:
                pass
        _atomic_write_text(latest, serialized)
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


def _summary_id(value: object) -> str:
    """Return an inert, bounded token for system-reminder ID summaries."""
    if not isinstance(value, str) or not value:
        return ""
    if _SUMMARY_ID_RE.fullmatch(value) is not None:
        return value
    digest = hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"unsafe-id-{digest}"


def _summary_ids(rows: object, *, key: str = "id") -> str:
    if not isinstance(rows, list):
        return "-"
    values: list[str] = []
    for row in rows[:50]:
        value = row.get(key) if isinstance(row, dict) else row
        clean = _summary_id(value)
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
    reply_waiting = (
        bus.get("reply_waiting")
        if isinstance(bus.get("reply_waiting"), list)
        else []
    )
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
        reload_instruction = (
            "Before continuing, re-read the project's durable control-plane "
            "and memory files."
        )
    else:
        reload_instruction = (
            f"Before continuing, re-read {pointer_text} and the project's "
            "durable control-plane and memory files."
        )
    reply_waiting_hint = (
        " (read these replies first)" if reply_waiting else ""
    )
    truncation_warning = (
        "Bus snapshot was truncated; refresh AgentTalk status before acting."
        if bus.get("truncated") is True
        else ""
    )
    closing_instruction = (
        f"{truncation_warning} {reload_instruction}"
        if truncation_warning
        else reload_instruction
    )
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
        f"reply_waiting:{len(reply_waiting)} "
        f"[{_summary_ids(reply_waiting)}]{reply_waiting_hint}, "
        f"owed_out:{len(owed_out)} [{_summary_ids(owed_out)}], "
        f"in_flight:[{_summary_ids(in_flight)}]. "
        f"{closing_instruction}"
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
        try:
            info = log_path.stat(follow_symlinks=False)
        except FileNotFoundError:
            info = None
        if info is not None and not stat.S_ISREG(info.st_mode):
            return
        line = (
            f"{utc_now()} action={_summary_value(action)} "
            f"error={type(error).__name__}:"
            f"{_summary_value(str(error), fallback='unknown')}\n"
        )
        mode = "w" if info is not None and info.st_size > 256 * 1024 else "a"
        with log_path.open(mode, encoding="utf-8", newline="\n") as handle:
            handle.write(line)
    except BaseException:  # hook diagnostics must never block an interrupt either
        return
