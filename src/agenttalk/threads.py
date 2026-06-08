"""Derive open request/reply *threads* from the message log.

The bus has always had the data needed to answer "did the reviewer
ever respond to what I sent?" — every review-request / question /
proposal carries a ``meta.request_id`` that the reply echoes back. But
nothing surfaced it, so an implementer could send work for review and
simply forget to come back for the verdict (the motivating bug for
0.10.0). This module turns the raw log into a small set of *threads*,
each with a single ``state`` from the perspective of one agent:

    reply-waiting   a valid correlated response addressed to me is sitting
                    unread past my cursor — go consume it.
    owed-inbound    the ball is on me: the peer opened a thread I haven't
                    answered, OR a review-result(needs-info) bounced it back.
    open-outbound   the ball is on the peer: my opener has no response yet,
                    OR I asked for info (needs-info) and await it.
    closed          a terminal correlated response exists.
    closed-superseded  the requester rescinded the request (0.14.0): a
                    valid `rescind` from the thread's requester postdates
                    the opener (or the message it pins via
                    meta.target_msg_id). Terminal for every perspective;
                    a re-ask on the same request_id does NOT reopen it —
                    a fresh exchange needs a new request_id.

Derivation is a PURE function of (validated messages, my cursor, now) —
no persisted state. It MUST be fed ``Store.valid_messages()`` (roster +
signature validated), never ``all_messages()``: otherwise a forged or
unsigned response could falsely close a real open thread even though
``wait`` / ``recv`` would have skipped it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from agenttalk.store import CONTROL_KINDS, Message, OPENER_KINDS

# OPENER_KINDS — kinds that OPEN a trackable thread, mapped to the
# kind(s) that COUNT as the peer's response — moved to store.py in
# 0.14.0 (rescind validation needs it and store cannot import threads);
# re-exported here so existing importers keep working. A response only
# closes (or bounces) a thread when it is one of these kinds AND flows
# from the opener's recipient back to the opener — a generic ack on the
# same id must not silently close a review request (Codex review gap #3).

# Expected-response map (documented contract — the actual transitions
# live in `_classify_event`):
#   review-request -> review-result   (approved/rejected close; needs-info
#                                       bounces the ball back to the requester,
#                                       who answers with a message/note, which
#                                       bounces it back to the reviewer — a
#                                       ping-pong, not a single response)
#   proposal       -> proposal-response (accepted/rejected/countered all close)
#   question       -> message/note      (the first answer closes it)
ACTIONABLE_STATES = ("reply-waiting", "owed-inbound", "open-outbound")


def _classify_event(opener_kind: str, m: Message, requester: str, responder: str, ball: str):
    """Classify a post-opener message as a thread *event*.

    ``requester`` is the opener's sender; ``responder`` is its recipient.
    Returns ``None`` (not an event — chatter), ``("terminal", None)`` (a
    terminal response that closes the thread), or ``("ball", who)`` (the
    obligation passes to ``who``). Only messages flowing in the right
    direction for the current ball owner count, so a responder chatting
    on a thread can't close their own obligation and a requester's
    pre-verdict aside can't masquerade as the needs-info answer.
    """
    meta = m.meta or {}
    if opener_kind == "review-request":
        if m.kind == "review-result" and m.sender == responder and m.recipient == requester:
            if meta.get("status") == "needs-info":
                return ("ball", requester)   # reviewer kicked it back to requester
            return ("terminal", None)        # approved / rejected (or any verdict)
        # The requester answering a needs-info: a plain message/note from
        # requester -> responder, but ONLY while the ball is on the requester.
        if (
            m.kind in ("message", "note")
            and m.sender == requester and m.recipient == responder
            and ball == requester
        ):
            return ("ball", responder)       # answered — back to the reviewer
        return None
    if opener_kind == "proposal":
        if m.kind == "proposal-response" and m.sender == responder and m.recipient == requester:
            return ("terminal", None)        # accepted/rejected/countered all close
        return None
    if opener_kind == "question":
        # A question is open-ended: ANY non-control response from the asked
        # party closes it, regardless of kind. (0.12.0 — fixes the
        # production case where a `review-result` was the real answer to a
        # broadcast question but a strict message/note check left it owed.)
        if m.kind not in CONTROL_KINDS and m.sender == responder and m.recipient == requester:
            return ("terminal", None)
        return None
    return None


@dataclass
class Thread:
    request_id: str
    opener_kind: str
    subject: str
    opener_sender: str
    opener_recipient: str
    peer: str
    role: str          # "opener" | "responder" (relative to perspective)
    state: str         # reply-waiting | owed-inbound | open-outbound | closed | closed-superseded
    age_seconds: float | None
    last_msg_id: str
    unread: bool
    # Broadcast (multi-party) fields — populated only when is_broadcast.
    is_broadcast: bool = False
    audience: list[str] = field(default_factory=list)
    responded: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    # 0.18.0: audience members who have since been retired. The frozen
    # `audience` (immutable history) still lists them, but they are excluded
    # from `pending`/`next_owner` (a tombstone can never reply, so it is not an
    # owed obligation). Surfaced additively for observability.
    audience_retired: list[str] = field(default_factory=list)
    # Supersession (0.14.0, #12) — populated only when state is
    # closed-superseded: who rescinded, with what message, when, and why.
    rescind_msg_id: str | None = None
    rescinded_by: str | None = None
    rescind_at: str | None = None
    rescind_reason: str | None = None
    # Operator escalation labels (0.14.0, #18) — populated only when the
    # opener carried meta.needs_operator. Pure labeling on top of the
    # existing closure mechanics; never affects state computation.
    # "answered" = the liaison actually replied (FR-014); "closed" =
    # terminal without an answer (manual ack / supersession) — leaves
    # the pending bucket without fabricating an operator answer.
    needs_operator: bool = False
    operator_state: str | None = None   # "pending" | "answered" | "closed"
    # Team-scope labels (0.15.0, #15/#16) — pure labeling, additive:
    # responded_na: broadcast members whose closing reply was marked
    # not-applicable (subset of `responded`); na_response: the pairwise
    # thread's terminal reply was not-applicable; batch_total /
    # audience_kind: frozen fan-out facts passed through from the opener
    # meta for display + the incomplete-batch warning. Obligation
    # derivation is untouched — it stays opener-copy-based (C-004).
    responded_na: list[str] = field(default_factory=list)
    na_response: bool = False
    batch_total: int | None = None
    audience_kind: str | None = None
    # Read-only "who owes the next move" hint (0.16.0, #19 Phase A, FR-014/015).
    # A PURE projection of the derived state — never sender-settable, never
    # affects delivery/unread/closure. `next_owner` is an agent name, or (for an
    # outstanding broadcast) the list of non-responders. Omitted on terminal
    # threads. `next_action` is a closed vocabulary of the values actually
    # produced by `_derive_next`: reply | read-reply | await-reply | answer-operator.
    next_owner: str | list[str] | None = None
    next_action: str | None = None

    def to_dict(self) -> dict:
        d = {
            "request_id": self.request_id,
            "opener_kind": self.opener_kind,
            "subject": self.subject,
            "peer": self.peer,
            "role": self.role,
            "state": self.state,
            "age_seconds": (round(self.age_seconds, 3)
                            if self.age_seconds is not None else None),
            "last_msg_id": self.last_msg_id,
            "unread": self.unread,
        }
        # Keep pairwise output byte-identical; only broadcasts add fields.
        if self.is_broadcast:
            d["is_broadcast"] = True
            d["audience"] = self.audience
            d["responded"] = self.responded
            d["pending"] = self.pending
            # Additive (0.18.0): only when a frozen audience member is now
            # retired — keeps clean-broadcast output byte-identical.
            if self.audience_retired:
                d["audience_retired"] = self.audience_retired
        # Additive: superseded threads carry the rescind provenance.
        if self.state == "closed-superseded":
            d["rescind"] = {
                "id": self.rescind_msg_id,
                "by": self.rescinded_by,
                "at": self.rescind_at,
                "reason": self.rescind_reason,
            }
        # Additive: escalation threads carry the operator labels.
        if self.needs_operator:
            d["needs_operator"] = True
            d["operator_state"] = self.operator_state
        # Additive (0.15.0): NA labels + frozen fan-out facts — emitted
        # only when present so pre-0.15 shapes stay byte-identical.
        if self.responded_na:
            d["responded_na"] = self.responded_na
        if self.na_response:
            d["na_response"] = True
        if self.batch_total is not None:
            d["batch_total"] = self.batch_total
        if self.audience_kind is not None:
            d["audience_kind"] = self.audience_kind
        # NOTE (0.16.0, #19): next_owner / next_action are derived onto the
        # Thread object (see `_derive_next`) but are deliberately NOT emitted
        # here. They appear on EVERY open thread, so emitting them from to_dict
        # would change the baseline thread JSON shape. Surfacing into
        # `threads`/`sync --json` is done at the CLI layer (WP03/T015), which
        # also owns the additivity gate tests — keeping the shape change in one
        # place. Read `t.next_owner` / `t.next_action` off the Thread directly.
        return d


def _is_na(m: Message) -> bool:
    """True when a message is marked not-applicable (#15)."""
    return (m.meta or {}).get("response") == "not-applicable"


def _derive_next(t: "Thread", agent: str,
                 retired: set[str] | None = None) -> tuple[str | None, object]:
    """Map a derived thread to ``(next_action, next_owner)`` — who owes the next
    move and what it is (0.16.0, #19 Phase A, FR-014/015).

    A PURE projection of already-computed fields (``state``, ``needs_operator``,
    ``operator_state``, ``peer``, broadcast ``pending``); it never reads
    sender-supplied input and never affects delivery/unread/closure. Terminal
    threads owe nothing → ``(None, None)``.

    Note the state semantics (see ``derive_threads``): ``reply-waiting`` means a
    reply addressed to ``agent`` is sitting UNREAD — the ball is back with
    ``agent`` — whereas ``open-outbound`` means ``agent`` is waiting on the peer.

    Produced ``next_action`` vocabulary (closed set — only these are emitted):
    ``reply`` | ``read-reply`` | ``await-reply`` | ``answer-operator``.
    """
    retired = retired or set()
    if t.state in ("closed", "closed-superseded"):
        return None, None
    # An open operator escalation dominates: `agent` must get the operator's
    # answer before the thread can progress.
    if t.needs_operator and t.operator_state == "pending":
        return "answer-operator", agent
    if t.state == "owed-inbound":
        return "reply", agent
    if t.state == "reply-waiting":
        # a reply to `agent` is unread — the ball is back with self
        return "read-reply", agent
    if t.state == "open-outbound":
        if t.is_broadcast:
            return "await-reply", (list(t.pending) if t.pending else None)
        # Pairwise: don't name a RETIRED peer as the awaited owner — a
        # tombstone can never reply, so pointing the requester at it is a
        # stranded obligation (review M3). Mirror the broadcast path, which
        # already drops retired members from `pending`/next_owner. The thread
        # stays open-outbound (observability), but the hint owner is None so
        # nobody is told to wait on a dead identity. Gating callers
        # (_open_thread_for/_drain_check) pass retired=set(), so they are
        # unaffected and open threads stay visible for forwarding/drain.
        return "await-reply", (None if t.peer in retired else t.peer)
    return None, None


def _find_superseding_rescind(
    group: list[Message], requester: str, anchor_id: str,
) -> Message | None:
    """Return the first valid rescind that supersedes this thread, or None.

    The D2 ordering rule: a thread is superseded when any rescind on its
    request_id, **sent by the thread's requester**, is newer (by message
    id — the bus's total order) than the anchor: the opener, or the
    message a rescind explicitly pins via ``meta.target_msg_id``. The
    first qualifying rescind decides; later duplicates are idempotent.
    The requester-only rule means a responder cannot cancel its own
    obligations, and (because callers feed ``valid_messages()``) a
    forged rescind is gated by the same roster/HMAC rules as any other
    message.
    """
    for r in group:  # group is id-sorted: first qualifying == decider
        if r.kind != "rescind" or r.sender != requester:
            continue
        tgt = (r.meta or {}).get("target_msg_id")
        threshold = tgt if isinstance(tgt, str) and tgt else anchor_id
        if r.id > threshold:
            return r
    return None


def _needs_operator(opener: Message) -> bool:
    """True when the opener carries the escalation discriminator."""
    v = (opener.meta or {}).get("needs_operator")
    return v is True or (isinstance(v, str) and v.lower() == "true")


def _parse_ts(ts: str) -> datetime | None:
    """Parse a message ``ts`` (ISO-8601, trailing Z) to an aware datetime."""
    if not isinstance(ts, str) or not ts:
        return None
    normalized = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt


def _derive_broadcast(
    rid: str,
    group: list[Message],
    openers: list[Message],
    *,
    agent: str,
    cursor: str,
    now: datetime,
    forced_closed: bool = False,
    retired: set[str] | None = None,
) -> Thread | None:
    """Derive the multi-party thread for a broadcast (fan-out) correlation.

    ``openers`` are the per-recipient copies the broadcaster fanned out
    (same sender, distinct recipients, all carrying ``meta.audience``).
    A response is any non-control message from an audience member back to
    the broadcaster echoing ``rid``. ``forced_closed`` (the agent
    explicitly ack'd the thread) overrides the computed state to
    ``closed``. Returns ``None`` if ``agent`` is neither the broadcaster
    nor an audience member.
    """
    sender = openers[0].sender
    audience = sorted({m.recipient for m in openers})
    opener_kind = openers[0].kind
    audience_label = (openers[0].meta or {}).get("audience") or "all"
    first_opener_id = min(m.id for m in openers)

    responded: set[str] = set()
    last_na: dict[str, bool] = {}  # member -> was their LATEST reply NA?
    responses: list[Message] = []
    for m in group:  # id-sorted (chronological): a later reply overwrites earlier
        if m.id <= first_opener_id:
            continue
        # A broadcast question is open-ended: any non-control reply from a
        # member counts as that member responding (e.g. a review-result).
        # A rescind is an event ABOUT the thread, not a response on it —
        # without this exclusion a (forged/odd) member rescind would count
        # as that member answering.
        if (m.kind not in CONTROL_KINDS and m.kind != "rescind"
                and m.sender in audience and m.recipient == sender):
            responded.add(m.sender)
            # NA closes like any answer; the label (#15) lets the broadcaster
            # distinguish "answered" from "not my role". Last-write-wins per
            # member, so a later substantive reply CLEARS a prior accidental NA
            # (and vice versa) — the rollup reflects the member's latest answer
            # instead of stickily flagging NA forever (review nit).
            last_na[m.sender] = _is_na(m)
            responses.append(m)
    responded_na = {who for who, na in last_na.items() if na}
    retired = retired or set()
    # 0.18.0: a retired audience member can never reply, so it is not an owed
    # obligation — exclude it from `pending`/`next_owner`. The frozen
    # `audience` (history) still lists it; `audience_retired` surfaces it.
    audience_retired = [a for a in audience if a in retired]
    pending = [a for a in audience if a not in responded and a not in retired]

    # Frozen fan-out facts (0.15.0, #16) — display/warning passthrough
    # only; obligations above derive from the COPIES, never this meta.
    opener_meta = openers[0].meta or {}
    try:
        batch_total = int(opener_meta.get("batch_total", ""))
    except (TypeError, ValueError):
        batch_total = None
    audience_kind = opener_meta.get("audience_kind")
    if not isinstance(audience_kind, str) or not audience_kind:
        audience_kind = None

    # Supersession (D2): only the broadcaster (the thread's requester)
    # can rescind its own fan-out; doing so closes the whole thread for
    # every perspective — remaining obligations are void.
    superseding = _find_superseding_rescind(group, sender, first_opener_id)

    if agent == sender:
        role, peer = "opener", f"@{audience_label}"
        unconsumed = any(m.recipient == sender and m.id > cursor for m in responses)
        if unconsumed:
            state = "reply-waiting"
            last = group[-1]
        elif pending:
            state = "open-outbound"
            # Age from the BROADCAST itself (oldest opener), not the latest
            # partial reply — otherwise a half-answered broadcast whose
            # remaining members go silent would never trip the stale-thread
            # warning (its age would reset on every reply received).
            last = min(openers, key=lambda m: m.id)
        else:
            state = "closed"
            last = group[-1]
        unread = any(m.recipient == sender and m.id > cursor for m in group)
    elif agent in audience:
        role, peer = "responder", sender
        # The broadcast question addressed to me is an opener, not a
        # response — so it's owed-inbound until I reply, then closed.
        state = "closed" if agent in responded else "owed-inbound"
        # `last` spans both my inbound copy AND my own reply (which is
        # addressed to the sender, not me) so a closed thread's age/id
        # reflect my answer, not the original ask. `unread` stays
        # inbound-only.
        mine_in = [m for m in group if m.recipient == agent]
        mine_all = [
            m for m in group
            if m.recipient == agent or (m.sender == agent and m.recipient == sender)
        ]
        last = mine_all[-1] if mine_all else group[-1]
        unread = any(m.id > cursor for m in mine_in)
    else:
        return None  # not my thread

    if forced_closed:
        state = "closed"  # explicit ack --to-request override
    if superseding is not None and not forced_closed:
        # Supersession overrides DERIVED states (incl. partial-response
        # progress), but never relabels a per-agent manual ack: the agent
        # explicitly handled the thread, and existing closure paths stay
        # untouched (WP01 contract). The rescind itself remains in the
        # validated log either way — `check --to-request` computes
        # supersession directly and is unaffected by view labels.
        state = "closed-superseded"

    ts = _parse_ts(last.ts)
    age = (now - ts).total_seconds() if ts is not None else None
    return Thread(
        request_id=rid,
        opener_kind=opener_kind,
        subject=openers[0].subject or "",
        opener_sender=sender,
        opener_recipient=audience_label,
        peer=peer,
        role=role,
        state=state,
        age_seconds=age,
        last_msg_id=last.id,
        unread=unread,
        is_broadcast=True,
        audience=audience,
        responded=sorted(responded),
        pending=pending,
        audience_retired=audience_retired,
        rescind_msg_id=superseding.id if superseding else None,
        rescinded_by=superseding.sender if superseding else None,
        rescind_at=superseding.ts if superseding else None,
        rescind_reason=(superseding.body or "") if superseding else None,
        responded_na=sorted(responded_na),
        batch_total=batch_total,
        audience_kind=audience_kind,
    )


def derive_threads(
    messages: list[Message],
    *,
    agent: str,
    cursor: str,
    now: datetime | None = None,
    closed_rids: set[str] | None = None,
    retired: set[str] | None = None,
) -> list[Thread]:
    """Return one :class:`Thread` per correlated request_id involving ``agent``.

    ``messages`` MUST be the roster+signature-validated set
    (``Store.valid_messages()``). ``cursor`` is ``agent``'s ack cursor,
    used to decide ``reply-waiting`` / ``unread``. ``closed_rids`` are
    request_ids ``agent`` has explicitly closed (``ack --to-request``):
    those threads report ``closed`` regardless of derived state — the
    manual escape hatch for off-contract / already-handled threads.
    Threads where ``agent`` is neither the opener's sender nor recipient
    are omitted.
    """
    now = now or datetime.now(timezone.utc)
    cursor = cursor or ""
    closed_rids = closed_rids or set()
    retired = retired or set()

    # Group by correlation id. Messages without a request_id are
    # untracked by design (you can't correlate what was never tagged).
    groups: dict[str, list[Message]] = {}
    for m in messages:
        rid = (m.meta or {}).get("request_id")
        if isinstance(rid, str) and rid:
            groups.setdefault(rid, []).append(m)

    threads: list[Thread] = []
    for rid, group in groups.items():
        group.sort(key=lambda m: m.id)
        # Broadcast (multi-party) thread: question copies carrying a
        # broadcast_id. Gate PRECISELY — `agenttalk broadcast` only ever
        # fans out questions (message/note broadcasts aren't openers) and
        # always sets broadcast_id, so this matches real broadcasts and
        # nothing else. Keying on `audience` alone would misroute a plain
        # `send --kind review-request --meta audience=...` into the
        # multi-party path (where a review-result wouldn't close it).
        bcast_openers = [
            m for m in group
            if m.kind == "question" and (m.meta or {}).get("broadcast_id")
        ]
        if bcast_openers:
            t = _derive_broadcast(
                rid, group, bcast_openers, agent=agent, cursor=cursor, now=now,
                forced_closed=(rid in closed_rids), retired=retired,
            )
            if t is not None:
                threads.append(t)
            continue
        opener = next((m for m in group if m.kind in OPENER_KINDS), None)
        if opener is None:
            # Orphan responses (opener in an archived/older session, or a
            # bare reply that never had an opener) — nothing to track.
            continue
        requester, responder = opener.sender, opener.recipient
        if agent not in (requester, responder):
            continue  # not my thread

        # Replay the thread chronologically, tracking who owes the next
        # move (`ball`) and whether a terminal response has closed it.
        # `_classify_event` encodes the per-kind transitions, including
        # the review-request needs-info ping-pong (reviewer -> requester
        # -> reviewer). A single-response model is NOT enough: after a
        # `needs-info`, the requester answers with a plain message and the
        # ball must swing back to the reviewer, or the reviewer is left
        # told to wait while the answer sits unread in their inbox.
        ball: str | None = responder
        terminal = False
        terminal_msg: Message | None = None  # the event that closed it (0.15.0 NA label)
        events: list[Message] = []  # post-opener messages that moved the thread
        for m in group:
            if m.id <= opener.id:
                continue
            # Rescinds are events ABOUT the thread, handled separately by
            # the supersession rule below — they must never classify as a
            # response (a question's open-ended closure would otherwise
            # treat a responder's rescind as the answer) nor as a re-ask.
            if m.kind == "rescind":
                continue
            ev = _classify_event(opener.kind, m, requester, responder, ball)
            if ev is not None:
                events.append(m)
                kind_, who = ev
                if kind_ == "terminal":
                    terminal, ball, terminal_msg = True, None, m
                else:
                    terminal, ball = False, who
                continue
            # A re-ask on the same request_id (same opener kind,
            # requester -> responder) after a close re-opens the thread —
            # the ball is the peer's again. This is the multi-round consult
            # convention, where the follow-up reuses the original request_id.
            if (
                terminal
                and m.kind == opener.kind
                and m.sender == requester
                and m.recipient == responder
            ):
                terminal, ball, terminal_msg = False, responder, None
                events.append(m)

        # reply-waiting: an unconsumed thread *event* addressed to me (a
        # response, a needs-info answer, or a re-ask). The opener itself is
        # NOT an event — an unread opener is owed-inbound, not reply-waiting.
        unconsumed_to_me = any(
            m.recipient == agent and m.id > cursor for m in events
        )

        if unconsumed_to_me:
            state = "reply-waiting"
        elif terminal:
            state = "closed"
        elif ball == agent:
            state = "owed-inbound"
        else:
            state = "open-outbound"
        if rid in closed_rids:
            state = "closed"  # explicit ack --to-request override

        # Supersession (D2) outranks every DERIVED state above, including
        # the re-ask reopen: it is a global thread fact every perspective
        # derives identically from the validated log, and
        # `closed-superseded` (vs plain `closed`) tells an agent WHY it
        # must not act. A re-ask on a superseded rid does NOT reopen — a
        # fresh exchange needs a new request_id, the same contract as
        # manual ack closure. The one thing it does NOT relabel is an
        # explicit per-agent ack (`rid in closed_rids`): the agent
        # already handled the thread and existing closure paths stay
        # untouched. The barrier is unaffected — `check --to-request`
        # computes supersession from the log, not from view labels.
        superseding = _find_superseding_rescind(group, requester, opener.id)
        if superseding is not None and rid not in closed_rids:
            state = "closed-superseded"

        last = group[-1]
        ts = _parse_ts(last.ts)
        age = (now - ts).total_seconds() if ts is not None else None
        unread = any(m.recipient == agent and m.id > cursor for m in group)

        # Escalation labels (#18): pure labeling. FR-014 — `answered`
        # means exactly "the liaison sent a correlated non-control reply
        # to the requester", which is precisely a replay-terminal event
        # (`_classify_event`'s question closure). A thread that went
        # terminal WITHOUT such a reply (manual ack, supersession) is
        # `closed`: it leaves the pending bucket, but never fabricates
        # an operator answer that didn't happen.
        needs_op = _needs_operator(opener)
        op_state = None
        if needs_op:
            if terminal:
                op_state = "answered"
            elif state in ("closed", "closed-superseded"):
                op_state = "closed"
            else:
                op_state = "pending"

        threads.append(Thread(
            request_id=rid,
            opener_kind=opener.kind,
            subject=opener.subject or "",
            opener_sender=requester,
            opener_recipient=responder,
            peer=(responder if agent == requester else requester),
            role=("opener" if agent == requester else "responder"),
            state=state,
            age_seconds=age,
            last_msg_id=last.id,
            unread=unread,
            rescind_msg_id=superseding.id if superseding else None,
            rescinded_by=superseding.sender if superseding else None,
            rescind_at=superseding.ts if superseding else None,
            rescind_reason=(superseding.body or "") if superseding else None,
            needs_operator=needs_op,
            operator_state=op_state,
            # NA label (#15): the reply that closed this thread was marked
            # not-applicable. Labeling only — closure mechanics unchanged.
            na_response=bool(terminal_msg is not None and _is_na(terminal_msg)),
        ))

    # Read-only next-owner / next-action hint (#19 Phase A): a post-pass over
    # the assembled rows (covers pairwise AND broadcast) — a pure projection of
    # state, so it cannot affect any derivation above.
    for t in threads:
        t.next_action, t.next_owner = _derive_next(t, agent, retired)

    # Stable, useful ordering: actionable first (by the ACTIONABLE_STATES
    # order), then terminal (closed / closed-superseded share a rank);
    # within a state, oldest activity first so the most-stale obligation
    # is at the top.
    state_rank = {s: i for i, s in enumerate(ACTIONABLE_STATES)}
    state_rank["closed"] = len(ACTIONABLE_STATES)
    state_rank["closed-superseded"] = len(ACTIONABLE_STATES)
    threads.sort(key=lambda t: (
        state_rank.get(t.state, 99),
        -(t.age_seconds or 0.0),
    ))
    return threads


def counts(threads: list[Thread]) -> dict[str, int]:
    """Tally threads by state (every state key present, even at 0)."""
    out = dict.fromkeys((*ACTIONABLE_STATES, "closed", "closed-superseded"), 0)
    for t in threads:
        out[t.state] = out.get(t.state, 0) + 1
    return out
