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
from datetime import datetime, timezone

_CLAUDE_STREAM = ["--output-format", "stream-json", "--verbose", "--include-partial-messages"]


@dataclass
class SessionState:
    cli: str
    codex_thread_id: str | None = None       # captured from thread.started (durable)
    claude_session_id: str | None = None     # minted once (uuid) by run.py for claude
    resume_available: bool = True
    resume_unavailable_reason: str = ""
    turns: int = 0
    resume_failure_count: int = 0
    resume_failure_key: str = ""
    resume_attempt_state: str = ""
    resume_attempt_key: str = ""
    resume_attempt_id: str = ""
    resume_attempt_started_at: str = ""
    resume_attempt_msg_id: str = ""
    continuity_lost_reason: str = ""
    continuity_loss_notified_key: str = ""
    fresh_session_success_reason: str = ""
    # v0.75.0 runtime ergonomics: the EFFECTIVE model/effort the child launched
    # with (scanned from the FINAL injected argv) + their normalized fingerprint.
    # A PRESENT-but-DIFFERENT fingerprint on relaunch forces a fresh session
    # (runtime_config_changed); an ABSENT fingerprint is adopted silently (D5).
    model: str | None = None
    reasoning_effort: str | None = None
    runtime_fingerprint: str | None = None


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
            if state.resume_unavailable_reason:
                state.continuity_lost_reason = state.resume_unavailable_reason
                state.fresh_session_success_reason = "fresh_session_success"
                state.resume_unavailable_reason = ""
            state.resume_failure_count = 0
            state.resume_failure_key = ""


def mark_resume_unavailable(state: SessionState, reason: str) -> None:
    """A resume attempt failed / no session: force a fresh exec next turn and clear
    the stale thread_id so the next thread.started persists the new one."""
    state.resume_available = False
    state.resume_unavailable_reason = reason
    state.codex_thread_id = None
    state.continuity_lost_reason = reason


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
    state.continuity_lost_reason = reason


def compute_runtime_fingerprint(model: str | None, effort: str | None) -> str:
    """The normalized ``(model, effort)`` fingerprint of the EFFECTIVE child argv
    (v0.75.0). A plain debuggable string (not a hash; never exposed on the wire):
    model compared as-is (alias vs full-id counts as drift — documented), effort
    case-folded + trimmed. Pure."""
    return f"{(model or '').strip()}|{(effort or '').strip().lower()}"


def reconcile_runtime_fingerprint(state: SessionState, current_fp: str) -> str:
    """Compare the launch fingerprint against the persisted one and apply the
    ADOPT / UNCHANGED / RESET policy (v0.75.0, D5). Pure — no I/O; the caller
    persists. Returns the action taken.

    - ``runtime_fingerprint is None`` (first launch OR pre-v0.75.0 upgrade) ->
      **adopt**: stamp it silently, NO reset, NO continuity_lost_reason.
    - equal -> **unchanged**: resume normally.
    - PRESENT-but-DIFFERENT -> **reset**: force a fresh session for this CLI
      (claude: new session id; codex: cleared thread_id) with
      ``continuity_lost_reason == "runtime_config_changed"``, then re-stamp.
    """
    prior = state.runtime_fingerprint
    if prior is None:
        state.runtime_fingerprint = current_fp
        return "adopt"
    if prior == current_fp:
        return "unchanged"
    if state.cli == "claude":
        reset_claude_session(state, "runtime_config_changed")
    else:
        mark_resume_unavailable(state, "runtime_config_changed")
    state.runtime_fingerprint = current_fp
    return "reset"


def _safe_int(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def resume_session_key(state: SessionState, agent: str) -> str:
    ident = state.codex_thread_id if state.cli == "codex" else state.claude_session_id
    return f"{agent}:{state.cli}:{ident or 'none'}"


def reconcile_resume_attempt(state: SessionState) -> None:
    if state.resume_attempt_state == "in_progress":
        state.resume_attempt_state = "ambiguous"
        state.resume_attempt_msg_id = ""


def resume_attempt_start(state: SessionState, *, agent: str, msg_id: object) -> None:
    state.resume_attempt_key = resume_session_key(state, agent)
    state.resume_attempt_id = uuid.uuid4().hex[:12]
    state.resume_attempt_started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    state.resume_attempt_msg_id = str(msg_id or "")
    state.resume_attempt_state = "in_progress"


def resume_failure_is_session_attributable(failure_class: str | None,
                                           summary: str | None,
                                           raw_tail: str | None = None,
                                           *,
                                           produced_model_output: bool = False) -> bool:
    if failure_class in {"known_global_infra", "config_blocked"}:
        return False
    # The raw child-output tail is a BYTE/LINE-BOUNDED capture (max_bytes/max_lines
    # with a truncated flag). A model's JSON assistant line truncated at the tail
    # boundary becomes INVALID JSON, so it would pass run.py's non-JSON-only filter and
    # be treated as a trusted CLI diagnostic - re-opening the content-spoof hole
    # (codex-agenttalk-reviewer-1 REQUEST-CHANGES on PR #51): a truncated model fragment
    # quoting "no conversation found with session id" could spoof a session failure on a
    # turn where the model ACTUALLY RAN. The load-bearing guard is CONTENT-INDEPENDENT:
    # a real missing-resume-session failure produces ZERO model output (claude's result
    # reports num_turns==0 - the turn never ran - and no MODEL_OUTPUT event occurs),
    # whereas a turn that merely QUOTES the phrase produced model output. So the raw tail
    # is scanned ONLY when the turn produced no model output; a turn that produced model
    # output is NEVER session-attributable from tail text, regardless of truncation. This
    # makes the truncation-spoof structurally impossible, not merely harder. The classified
    # summary (built from structured JSON events, never raw model bytes) is still scanned
    # unconditionally - model content cannot reach it (task #34 resume-continuity).
    tail = "" if produced_model_output else (raw_tail or "")
    text = f"{summary or ''}\n{tail}".casefold()
    broken_terms = (
        "no session",
        "session not found",
        "invalid session",
        "expired session",
        "broken session",
        "resume turn failed",
        "session is full",
        "prompt is too long",
        # anchored to the FULL claude diagnostic ("No conversation found with session
        # ID: <uuid>"), not a bare "no conversation found", so incidental prose is a
        # far weaker match; combined with the non-JSON-only raw_tail scan in run.py
        # (model content is JSON-wrapped, excluded), this closes the content-spoof
        # vector codex-agenttalk-reviewer-1 raised on PR #51.
        "no conversation found with session id",
    )
    return (
        failure_class == "poison_eligible"
        or (
            failure_class in {"ambiguous_or_unknown", "broken_session"}
            and any(t in text for t in broken_terms)
        )
    )


def record_resume_attempt_result(state: SessionState, *, failure_class: str | None,
                                 summary: str | None,
                                 attributable: bool) -> int:
    state.resume_attempt_state = "failed"
    state.resume_attempt_msg_id = ""
    if attributable:
        key = state.resume_attempt_key
        if key != state.resume_failure_key:
            state.resume_failure_count = 0
            state.resume_failure_key = key
        state.resume_failure_count = _safe_int(state.resume_failure_count) + 1
    else:
        state.resume_failure_count = 0
        state.resume_failure_key = ""
    return state.resume_failure_count


def clear_resume_attempt(state: SessionState) -> None:
    state.resume_attempt_state = ""
    state.resume_attempt_id = ""
    state.resume_attempt_started_at = ""
    state.resume_attempt_msg_id = ""


def should_notify_continuity_loss(state: SessionState) -> bool:
    key = state.resume_failure_key or state.resume_attempt_key
    return bool(key and state.continuity_loss_notified_key != key)


def mark_continuity_loss_notified(state: SessionState) -> None:
    key = state.resume_failure_key or state.resume_attempt_key
    if key:
        state.continuity_loss_notified_key = key


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
            state = SessionState(**{k: v for k, v in data.items() if k in known})
            state.resume_failure_count = _safe_int(state.resume_failure_count)
            reconcile_resume_attempt(state)
            return state
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
