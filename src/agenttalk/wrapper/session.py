"""Per-CLI session continuity for the wrapper loop (design C, Phase B; Codex
co-designed). For wrapped agents the WRAPPER owns session continuity end-to-end
(the supervisor no longer mints session ids for them).

Codex (the rulings): turn 1 = `codex exec --json` -> capture
thread.started.thread_id -> persist; turn N = `codex exec resume --json
<thread_id>` with the prompt on STDIN (dodge quoting); stable per-agent CODEX_HOME
+ stable cwd; no --ephemeral. `resume --last` is ONLY a repair fallback when no
durable thread_id exists; on explicit/`--last` failure or no-session -> a fresh
`exec --json` + mark resume_unavailable(reason) + persist the NEW thread_id once
observed. NEVER parse human output for session identity.

Claude: turn 1 = `claude -p --output-format stream-json --verbose
--include-partial-messages --session-id <uuid>`; turn N = `--resume <uuid>`.

The argv-building + state transitions here are pure (no I/O). run.py owns the
process + the atomic persistence of SessionState.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, fields

_CLAUDE_STREAM = ["--output-format", "stream-json", "--verbose", "--include-partial-messages"]


@dataclass
class SessionState:
    cli: str
    codex_thread_id: str | None = None       # captured from thread.started (durable)
    claude_session_id: str | None = None     # minted once (uuid) by run.py for claude
    resume_available: bool = True
    resume_unavailable_reason: str = ""
    turns: int = 0


@dataclass(frozen=True)
class TurnSpec:
    """The per-turn child invocation: ``args`` are APPENDED to the agent's base
    launch argv; ``stdin`` carries the prompt (preferred over a positional arg to
    dodge shell quoting). ``note`` is a human label for logs/tests."""

    args: list[str]
    stdin: str | None
    note: str = ""


def build_turn(state: SessionState, prompt: str) -> TurnSpec:
    """Build this turn's child args (per CLI + session state). Pure."""
    if state.cli == "codex":
        if state.codex_thread_id and state.resume_available:
            return TurnSpec(["exec", "resume", "--json", state.codex_thread_id],
                            stdin=prompt, note="codex resume by thread_id")
        # turn 1, OR a resume-unavailable repair: a FRESH exec (a new thread_id will
        # be observed + persisted from thread.started).
        note = "codex fresh exec"
        if not state.resume_available:
            note += f" (resume unavailable: {state.resume_unavailable_reason})"
        return TurnSpec(["exec", "--json"], stdin=prompt, note=note)
    if state.cli == "claude":
        if not state.claude_session_id:
            raise ValueError("claude session id must be minted before build_turn")
        cont = (["--session-id", state.claude_session_id]
                if (state.turns == 0 or not state.resume_available)
                else ["--resume", state.claude_session_id])
        return TurnSpec(["-p", *_CLAUDE_STREAM, *cont], stdin=prompt, note="claude stream-json")
    raise ValueError(f"no wrapper session policy for cli {state.cli!r}")


def observe_event(state: SessionState, raw: object) -> None:
    """Capture codex's durable session id from a RAW codex stream object
    (thread.started.thread_id). No-op for claude / other shapes. The codex adapter
    maps thread.started to no normalized event, so identity is captured HERE from
    the raw stream - never parsed from human output."""
    if state.cli != "codex" or not isinstance(raw, dict):
        return
    if raw.get("type") == "thread.started":
        tid = raw.get("thread_id")
        if isinstance(tid, str) and tid:
            state.codex_thread_id = tid
            state.resume_available = True
            state.resume_unavailable_reason = ""


def mark_resume_unavailable(state: SessionState, reason: str) -> None:
    """A resume attempt failed / no session: force a fresh exec next turn and clear
    the stale thread_id so the next thread.started persists the new one."""
    state.resume_available = False
    state.resume_unavailable_reason = reason
    state.codex_thread_id = None


def reset_claude_session(state: SessionState, reason: str) -> None:
    """A claude ``--resume`` turn failed (typically the session is FULL): mint a FRESH
    session id and force a ``--session-id`` (fresh) turn next, so the SAME record can be
    retried on a clean session before we ever classify it as poison. Mirrors the codex
    resume->fresh self-heal (:func:`mark_resume_unavailable`). A NEW uuid is required:
    re-running ``--session-id`` with the existing id would not give a clean session."""
    if state.cli != "claude":
        return
    state.claude_session_id = str(uuid.uuid4())
    state.resume_available = False
    state.resume_unavailable_reason = reason


# --- persistence: the session layer owns its state, written atomically (Codex) ---

def _session_path(store, agent: str):
    return store.state_dir / f"{agent}.wrapper-session.json"


def load_session(store, agent: str, cli: str) -> SessionState:
    """Load the persisted SessionState for ``agent``, or a fresh one. A claude
    session id is minted once on first creation (used for --session-id/--resume)."""
    path = _session_path(store, agent)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            known = {f.name for f in fields(SessionState)}
            return SessionState(**{k: v for k, v in data.items() if k in known})
        except (ValueError, OSError, TypeError):
            pass  # corrupt / unreadable -> start fresh
    state = SessionState(cli=cli)
    if cli == "claude" and not state.claude_session_id:
        state.claude_session_id = str(uuid.uuid4())
    return state


def save_session(store, agent: str, state: SessionState) -> None:
    """Persist SessionState atomically (sandbox-safe writer)."""
    from .. import _atomic
    _atomic.write_text(_session_path(store, agent),
                       json.dumps(asdict(state), ensure_ascii=False, indent=2))
