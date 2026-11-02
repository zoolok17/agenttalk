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

import contextlib
import json
import shutil
import uuid
from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime, timezone

from .. import capacity

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
    # #301: the seat's latest rate-limit reading per window, folded from its own
    # rate_limit_event lines (capacity.claude_stream_reading).
    claude_rate_limit: dict | None = None
    # #475: the last start-fresh request the wrapper handled (its id also makes a
    # marker that survived a crash between "saved" and "cleared" apply only once).
    fresh_request_id: str = ""
    fresh_request_outcome: str = ""
    fresh_request_at: str = ""
    fresh_request_reason: str = ""
    fresh_request_by: str = ""
    fresh_request_turn: int = 0
    # W1 (self-compaction pilot): the size of THIS conversation, read from the stream by
    # conversation_reading. Every transition resets it (reset_conversation); a reading whose
    # conversation_id is not the current claude_session_id counts as no reading.
    conversation_id: str = ""
    conversation_turns: int = 0
    conversation_context_tokens: int | None = None
    conversation_context_at: str = ""
    conversation_context_window: int | None = None
    conversation_baseline_tokens: int | None = None


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
    (thread.started.thread_id). The codex adapter maps thread.started to no
    normalized event, so identity is captured HERE from the raw stream - never
    parsed from human output. For claude, a rate_limit_event is folded into the
    seat's capacity reading (#301); the adapter's own handling of it is separate."""
    if not isinstance(raw, dict):
        return
    if state.cli == "claude" and raw.get("type") == "rate_limit_event":
        try:
            state.claude_rate_limit = capacity.claude_stream_reading(
                raw.get("rate_limit_info"), state.claude_rate_limit)
        except Exception:  # noqa: BLE001 - an advisory reading must never end a turn
            prev = state.claude_rate_limit  # no reading, but keep what it was taken under
            state.claude_rate_limit = {"binding": prev.get("binding")} if isinstance(prev, dict) else None
        return
    if state.cli != "codex":
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


def reset_conversation(state: SessionState, conversation_id: str = "") -> None:
    """A new conversation begins: nothing of the old one's size describes it. Called by every
    transition (a new session id, a cleared codex thread)."""
    state.conversation_id = conversation_id
    state.conversation_turns = 0
    state.conversation_context_tokens = None
    state.conversation_context_at = ""
    state.conversation_context_window = None
    state.conversation_baseline_tokens = None


def clear_reading(state: SessionState) -> None:
    """The conversation was compacted: its old size is obsolete. The baseline stays (what the
    conversation started at is still true)."""
    state.conversation_context_tokens = None
    state.conversation_context_at = ""
    state.conversation_context_window = None


def record_dispatch(state: SessionState, fold, *, finished: bool, now: str | None = None) -> None:
    """Fold one dispatch's size reading into the record (claude only).

    A compaction in the dispatch ends the old reading, finished or not. A finished turn counts
    in the conversation and, when its dispatch passes the cross-check, replaces the reading and
    its time; otherwise the old reading keeps its own time (F-12, fresh rate-limit activity).
    The baseline is the first finished turn's reading, only for a conversation seen from its start."""
    if state.cli != "claude":
        return
    if fold.compacted:
        clear_reading(state)
    if not finished:
        return
    first = state.conversation_id == state.claude_session_id and state.conversation_turns == 0
    if state.conversation_id != state.claude_session_id:  # saved before W1, or the id was changed under it
        reset_conversation(state, state.claude_session_id or "")
    state.conversation_turns += 1
    reading = fold.reading()
    if reading is None:
        return
    state.conversation_context_tokens = reading["tokens"]
    state.conversation_context_window = reading["window"]
    state.conversation_context_at = now or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    if first:
        state.conversation_baseline_tokens = reading["tokens"]


def mark_resume_unavailable(state: SessionState, reason: str) -> None:
    """A resume attempt failed / no session: force a fresh exec next turn and clear
    the stale thread_id so the next thread.started persists the new one."""
    state.resume_available = False
    state.resume_unavailable_reason = reason
    state.codex_thread_id = None
    state.continuity_lost_reason = reason
    reset_conversation(state)


def reset_claude_session(state: SessionState, reason: str) -> None:
    """A claude ``--resume`` turn failed (typically the session is FULL): mint a FRESH
    session id and force a ``--session-id`` (fresh) turn next, so the SAME record can be
    retried on a clean session before we ever classify it as poison. Mirrors the codex
    resume->fresh self-heal (:func:`mark_resume_unavailable`). A NEW uuid is required:
    re-running ``--session-id`` with the existing id would not give a clean session."""
    if state.cli != "claude":
        return
    state.claude_session_id = str(uuid.uuid4())
    reset_conversation(state, state.claude_session_id)
    state.resume_available = False
    state.resume_unavailable_reason = reason
    state.continuity_lost_reason = reason


def apply_fresh_request(store, agent: str, state: SessionState, persist=None) -> dict | None:
    """Apply a pending start-fresh request (#475), between turns only.

    Called by the wrapper itself, from its own thread, while no turn runs: when it is
    idle and again just before it builds a turn. The wrapper owns the session record
    (it loads it once and saves its own copy), so the request is a marker the wrapper
    reads, never an edit of the record from outside.

    Claude: a new session id (:func:`reset_claude_session`). Codex: the thread id is
    cleared (:func:`mark_resume_unavailable`).

    The marker is cleared only after the reset is DURABLY saved: "already applied" is
    decided from the saved record on disk, never from the in-memory request id, so a
    failed save is retried (the exception propagates, the marker stays) and a restart
    after a failed save still finds the request. The old record is copied aside first and
    the reset does not start unless that copy succeeded.
    Returns the outcome record, or None when nothing was pending (or it was already done).
    """
    marker = store.read_fresh_request(agent)
    if marker is None:
        return None
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    if "request_id" not in marker:  # the closed "unusable" fact: nothing to trust or apply
        store.clear_fresh_request(agent, None)
        state.fresh_request_id = "unreadable"
        state.fresh_request_outcome = "unreadable_request_removed"
        state.fresh_request_at = now
        state.fresh_request_reason = ""
        state.fresh_request_by = ""
        state.fresh_request_turn = state.turns
        if persist is not None:
            persist(state)
        return {"outcome": state.fresh_request_outcome}
    rid = marker["request_id"]
    if _saved_request_id(store, agent) == rid:  # saved already; only the clearing was lost
        store.clear_fresh_request(agent, rid)
        return None
    reason = " ".join(str(marker.get("reason") or "").split())[:200]
    requested_by = str(marker.get("requested_by") or "")[:64]
    before = replace(state)  # what to put back if the reset cannot be saved
    if state.fresh_request_id != rid:  # not reset in memory yet
        if state.cli in ("claude", "codex"):
            _backup_session_record(store, agent, rid)  # raises: no backup, no reset
            if state.cli == "claude":
                reset_claude_session(state, "fresh_requested")
            else:
                mark_resume_unavailable(state, "fresh_requested")
            clear_resume_attempt(state)
            state.resume_failure_count = 0
            state.resume_failure_key = ""
            state.fresh_request_outcome = "applied"
        else:
            state.fresh_request_outcome = "unsupported_cli"
        state.fresh_request_id = rid
        state.fresh_request_at = now
        state.fresh_request_reason = reason
        state.fresh_request_by = requested_by
        state.fresh_request_turn = state.turns
    if persist is not None:
        try:
            persist(state)
        except BaseException:
            if _saved_request_id(store, agent) == rid:
                # The write landed and only a later step (fsync) failed: the record on disk
                # IS the reset. Memory already holds it, so adopt it (never resume the old
                # conversation over it) and finish by clearing the marker.
                pass
            else:
                # Nothing may run on a reset that is not on disk: put the session back, keep
                # the marker, and let the caller see the failure. The next call applies it afresh.
                for f in fields(SessionState):
                    setattr(state, f.name, getattr(before, f.name))
                raise
    store.clear_fresh_request(agent, rid)
    return {"request_id": rid, "outcome": state.fresh_request_outcome, "reason": reason,
            "requested_by": requested_by}


def _saved_request_id(store, agent: str) -> str:
    """The start-fresh request id in the record ON DISK ("" when absent or unreadable)."""
    try:
        data = json.loads(_session_path(store, agent).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    value = data.get("fresh_request_id") if isinstance(data, dict) else ""
    return value if isinstance(value, str) else ""


def _backup_session_record(store, agent: str, request_id: str) -> None:
    """Copy the record the wrapper is about to replace. A failed copy raises, so the reset
    never goes ahead without its backup (the request stays pending). No record yet: nothing to keep."""
    src = _session_path(store, agent)
    if not src.exists():
        return
    dst = src.with_name(f"{agent}.wrapper-session.before-fresh-{request_id}.json")
    shutil.copyfile(src, dst)
    if dst.read_bytes() != src.read_bytes():
        raise OSError("the session record backup does not match the record")


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
                                           produced_model_output: bool = False,
                                           result_num_turns: int | None = None,
                                           usage_limit_proven: bool = False) -> bool:
    # #317: a refusal this invocation's OWN stream proves was a provider usage limit (a
    # rejected rate-limit event, then a terminal result whose is_error is exactly true -
    # see usage_park.fact_from_stream) is checked BEFORE the class/text heuristics below,
    # not folded into the known_global_infra exclusion: resuming under an active usage
    # limit can make the API's OWN response read exactly like a broken session (the
    # terminal text can legitimately contain phrasing this function would otherwise call
    # attributable), so the classifier's output is not trustworthy evidence here. The
    # caller computes this from the stream regardless of whether the stop-at-limit switch
    # is on - a usage-limit refusal says nothing about the session either way.
    if usage_limit_proven:
        return False
    if failure_class in {"known_global_infra", "config_blocked"}:
        return False
    # run.py records JSON parse disposition before bounding the diagnostic tail, so
    # parsed assistant/tool events can never be reinterpreted here after truncation.
    # We still scan raw diagnostics ONLY when the resumed turn never ran; the CLI's
    # authoritative activity signal remains defense in depth:
    #   * num_turns is the load-bearing gate. claude's stream-json ``result`` reports
    #     num_turns (a missing-resume-session failure reports num_turns == 0 - the turn
    #     never ran); num_turns > 0 means it ran. This is content-independent and does
    #     not enumerate event types, so it cannot be defeated by a model turn that emits
    #     only tool calls (no assistant text), only thinking, etc.
    #   * produced_model_output is corroboration + a fallback for CLIs/results that OMIT
    #     num_turns: it trips on ANY turn activity (model output, deltas, OR tool calls),
    #     so a tool-only turn still counts as "ran". A MISSING num_turns is treated as
    #     UNKNOWN (never as 0), so we fall back to this flag instead of wrongly enabling
    #     the scan on a ran-but-num_turns-absent turn.
    # Net: any activity OR num_turns > 0 => the turn ran => tail is NOT scanned. The
    # classified summary (built from structured JSON events, never raw model bytes)
    # is still scanned unconditionally (task #34 resume-continuity).
    turn_ran = bool(produced_model_output) or (
        isinstance(result_num_turns, int) and result_num_turns > 0)
    tail = "" if turn_ran else (raw_tail or "")
    text = f"{summary or ''}\n{tail}".casefold()
    broken_terms = (
        "no session",
        "session not found",
        "no conversation found with session id",
        "invalid session",
        "expired session",
        "broken session",
        "resume turn failed",
        "session is full",
        "prompt is too long",
        # anchored to the FULL claude diagnostic ("No conversation found with session
        # ID: <uuid>"), not a bare "no conversation found", so incidental prose is a
        # far weaker match; run.py supplies only lines rejected as non-JSON at
        # ingestion, excluding model content.
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
            if store._size_unsaved_path(agent).exists():  # an obsolete size whose end was never saved
                clear_reading(state)
            return state
        except (ValueError, OSError, TypeError):
            pass  # corrupt / unreadable -> start fresh
    state = SessionState(cli=cli)
    if cli == "claude" and not state.claude_session_id:
        state.claude_session_id = str(uuid.uuid4())
        state.conversation_id = state.claude_session_id  # a conversation the wrapper sees from its start
    return state


def save_session(store, agent: str, state: SessionState) -> None:
    """Persist SessionState atomically (sandbox-safe writer)."""
    from .. import _atomic
    _atomic.write_text(_session_path(store, agent),
                       json.dumps(asdict(state), ensure_ascii=False, indent=2))
    with contextlib.suppress(OSError):  # this save holds the true reading: the marker is obsolete
        store.clear_size_reading_unsaved(agent)
