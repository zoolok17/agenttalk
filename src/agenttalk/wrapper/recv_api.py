"""Machine-readable recv for the wrapper (0.30.0 design C, Phase A).

The wrapper must NOT shell-parse human ``agenttalk wait``/``recv`` output (Codex
CRITICAL note). This module is the IN-PROCESS structured-recv path the wrapper
calls directly; ``agenttalk recv --json`` is a thin CLI MIRROR over the SAME
functions here (never a second implementation, and the wrapper never shells it).

Cursor semantics MIRROR the existing primitives EXACTLY - this module only
SURFACES them structured, it never changes them:
  * GLOBAL (consuming): floor at ``store.cursor(agent)``; ``commit`` advances the
    GLOBAL cursor (``advance_cursor``) - the same path ``recv``/``wait`` use.
  * SCOPED (--to-request): floor at ``max(store.thread_seen(agent, rid),
    store.cursor(agent))``; ``commit`` calls ``mark_thread_seen`` ONLY - it NEVER
    advances the global cursor and NEVER closes the thread (parity with ``wait
    --to-request``, which is why scoped waits do not eat the global inbox).
The wrapper is the ONLY active consumer of a wrapped agent's inbox; a concurrent
human ``wait``/``drain`` is documented operator interference, not a supported
multi-consumer model.
"""

from __future__ import annotations

from agenttalk.store import CONTROL_KINDS

GLOBAL = "global"
SCOPED = "scoped"


def to_record(msg, *, mode: str, cursor_before: str, cursor_after: str,
              scoped: dict | None = None, thread: dict | None = None) -> dict:
    """Serialize a Message to the structured recv schema.

    ``request_id`` + ``broadcast_id`` are lifted out of ``meta`` (``meta`` itself
    is returned unchanged); ``correlation_id`` = request_id or broadcast_id.
    """
    rid = msg.meta.get("request_id")
    bid = msg.meta.get("broadcast_id")
    return {
        "id": msg.id,
        "ts": msg.ts,
        "from": msg.sender,
        "to": msg.recipient,
        "kind": msg.kind,
        "subject": msg.subject,
        "body": msg.body,
        "meta": dict(msg.meta),
        "request_id": rid,
        "broadcast_id": bid,
        "correlation_id": rid or bid,
        "mode": mode,
        "cursor": {"before": cursor_before, "after": cursor_after},
        "scoped": scoped,
        "thread": thread,
    }


def _terminal_state(store, agent: str, rid: str) -> tuple[bool, bool]:
    """(closed, superseded) for a scoped thread - best-effort, READ-ONLY. Derived
    once per recv (not per idle-poll), so the cost is bounded. Correctness must not
    depend on this (Codex): it only lets the wrapper stop waiting on a dead thread.
    """
    from agenttalk import threads as _threads

    ts = store.read_threadstate(agent)
    closed_rids = {r for r, e in ts.items()
                   if isinstance(e, dict) and e.get("closed") is True}
    rows = _threads.derive_threads(
        store.valid_messages(), agent=agent,
        cursor=store.cursor(agent), closed_rids=closed_rids,
    )
    for t in rows:
        if t.request_id == rid:
            return (t.state in ("closed", "closed-superseded"),
                    t.state == "closed-superseded")
    return (False, False)


def _requester_terminal(store, agent: str, rid: str) -> dict | None:
    from .obligations import requester_terminal_for

    return requester_terminal_for(store, rid, agent)


def _requester_broadcast_state(store, agent: str, rid: str) -> str | None:
    from .obligations import requester_broadcast_policy_state

    return requester_broadcast_policy_state(store, rid, agent)


def _delivery_failed(terminal: dict | None) -> dict | None:
    if terminal is None or terminal.get("state") != "delivery_failed":
        return None
    return terminal


def records(store, agent: str, *, scoped_request_id: str | None = None,
            since: str | None = None, include_control: bool = False) -> list[dict]:
    """All currently-unread messages for ``agent`` as structured records (oldest
    first), NON-consuming. Control kinds (composing) are filtered unless
    ``include_control``. ``since`` overrides the GLOBAL floor (history inspection);
    it is ignored in scoped mode (scoped floors at max(thread_seen, cursor))."""
    if scoped_request_id is None:
        before = since if since is not None else store.cursor(agent)
        msgs = store.messages_for(agent, since_id=before or None)
        if not include_control:
            msgs = [m for m in msgs if m.kind not in CONTROL_KINDS]
        return [to_record(m, mode=GLOBAL, cursor_before=before, cursor_after=m.id)
                for m in msgs]
    rid = scoped_request_id
    seen = store.thread_seen(agent, rid)
    gcur = store.cursor(agent)
    floor = max(seen, gcur)
    msgs = store.messages_for(agent, since_id=floor or None)
    if not include_control:
        msgs = [m for m in msgs if m.kind not in CONTROL_KINDS]
    msgs = [m for m in msgs if m.meta.get("request_id") == rid]
    closed, superseded = _terminal_state(store, agent, rid)
    terminal = _requester_terminal(store, agent, rid)
    broadcast_state = _requester_broadcast_state(store, agent, rid)
    if broadcast_state in {"open", "blocked"} and terminal is None:
        closed = superseded
    failed = _delivery_failed(terminal)
    closed = closed or terminal is not None
    out = []
    for m in msgs:
        scoped = {"request_id": rid, "seen_before": seen, "seen_after": m.id,
                  "closed": closed, "superseded": superseded,
                  "delivery_terminal": terminal,
                  "delivery_failed": failed}
        # cursor before==after: scoped recv NEVER moves the global cursor.
        out.append(to_record(m, mode=SCOPED, cursor_before=gcur, cursor_after=gcur,
                             scoped=scoped))
    return out


def poll(store, agent: str, *, scoped_request_id: str | None = None,
         since: str | None = None, include_control: bool = False) -> dict:
    """A receive ENVELOPE: the unread ``records`` PLUS, for scoped mode, the
    thread's TERMINAL control state (closed/superseded) INDEPENDENT of message
    delivery - so a wrapper learns a thread was rescinded/closed even when no new
    message is pending (parity with scoped wait's entry-check; Codex carry-forward
    #1: closed/superseded is terminal control state, learnable with no message).
    """
    recs = records(store, agent, scoped_request_id=scoped_request_id,
                   since=since, include_control=include_control)
    env = {
        "mode": SCOPED if scoped_request_id is not None else GLOBAL,
        "record": recs[0] if recs else None,
        "records": recs,
        "scoped": None,
    }
    if scoped_request_id is not None:
        closed, superseded = _terminal_state(store, agent, scoped_request_id)
        terminal = _requester_terminal(store, agent, scoped_request_id)
        broadcast_state = _requester_broadcast_state(
            store,
            agent,
            scoped_request_id,
        )
        if broadcast_state in {"open", "blocked"} and terminal is None:
            closed = superseded
        failed = _delivery_failed(terminal)
        closed = closed or terminal is not None
        env["scoped"] = {
            "request_id": scoped_request_id,
            "seen": store.thread_seen(agent, scoped_request_id),
            "closed": closed,
            "superseded": superseded,
            "delivery_terminal": terminal,
            "delivery_failed": failed,
        }
    return env


def next_record(store, agent: str, *, scoped_request_id: str | None = None) -> dict | None:
    """The NEXT (oldest) unread message as a structured record, or None. A PEEK -
    call ``commit`` to consume. For scoped mode, prefer ``poll`` when you also need
    to learn terminal (rescinded/closed) state with no message pending."""
    recs = records(store, agent, scoped_request_id=scoped_request_id)
    return recs[0] if recs else None


def consume_boundary_complete(store, agent: str, record: dict) -> bool:
    """Whether the authoritative cursor projection already covers ``record``."""
    inbound_id = record.get("id")
    if not isinstance(inbound_id, str) or not inbound_id:
        return False
    if record.get("mode") == SCOPED:
        scoped = record.get("scoped")
        if not isinstance(scoped, dict):
            return False
        request_id = scoped.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            return False
        return max(
            store.cursor(agent),
            store.thread_seen(agent, request_id),
        ) >= inbound_id
    return store.cursor(agent) >= inbound_id


def commit(store, agent: str, record: dict) -> None:
    """Consume a record: GLOBAL advances the global cursor; SCOPED advances ONLY
    the per-thread seen pointer (never the global cursor, never closes the thread).
    """
    if record.get("mode") == SCOPED:
        store.mark_thread_seen(agent, record["scoped"]["request_id"], record["id"])
    else:
        store.advance_cursor(agent, record["id"])
