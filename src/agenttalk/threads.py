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

Derivation is a PURE function of (validated messages, my cursor, now) —
no persisted state. It MUST be fed ``Store.valid_messages()`` (roster +
signature validated), never ``all_messages()``: otherwise a forged or
unsigned response could falsely close a real open thread even though
``wait`` / ``recv`` would have skipped it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from agenttalk.store import Message

# Kinds that OPEN a trackable thread, mapped to the kind(s) that COUNT
# as the peer's response. A response only closes (or bounces) a thread
# when it is one of these kinds AND flows from the opener's recipient
# back to the opener — a generic ack on the same id must not silently
# close a review request (Codex review gap #3).
OPENER_KINDS = frozenset({"review-request", "question", "proposal"})

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
        if m.kind in ("message", "note") and m.sender == responder and m.recipient == requester:
            return ("terminal", None)        # first answer closes it
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
    state: str         # reply-waiting | owed-inbound | open-outbound | closed
    age_seconds: float | None
    last_msg_id: str
    unread: bool

    def to_dict(self) -> dict:
        return {
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


def derive_threads(
    messages: list[Message],
    *,
    agent: str,
    cursor: str,
    now: datetime | None = None,
) -> list[Thread]:
    """Return one :class:`Thread` per correlated request_id involving ``agent``.

    ``messages`` MUST be the roster+signature-validated set
    (``Store.valid_messages()``). ``cursor`` is ``agent``'s ack cursor,
    used to decide ``reply-waiting`` / ``unread``. Threads where ``agent``
    is neither the opener's sender nor recipient are omitted.
    """
    now = now or datetime.now(timezone.utc)
    cursor = cursor or ""

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
        events: list[Message] = []  # post-opener messages that moved the thread
        for m in group:
            if m.id <= opener.id:
                continue
            ev = _classify_event(opener.kind, m, requester, responder, ball)
            if ev is not None:
                events.append(m)
                kind_, who = ev
                if kind_ == "terminal":
                    terminal, ball = True, None
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
                terminal, ball = False, responder
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

        last = group[-1]
        ts = _parse_ts(last.ts)
        age = (now - ts).total_seconds() if ts is not None else None
        unread = any(m.recipient == agent and m.id > cursor for m in group)

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
        ))

    # Stable, useful ordering: actionable first (by the ACTIONABLE_STATES
    # order), then closed; within a state, oldest activity first so the
    # most-stale obligation is at the top.
    state_rank = {s: i for i, s in enumerate(ACTIONABLE_STATES)}
    state_rank["closed"] = len(ACTIONABLE_STATES)
    threads.sort(key=lambda t: (
        state_rank.get(t.state, 99),
        -(t.age_seconds or 0.0),
    ))
    return threads


def counts(threads: list[Thread]) -> dict[str, int]:
    """Tally threads by state (every state key present, even at 0)."""
    out = dict.fromkeys((*ACTIONABLE_STATES, "closed"), 0)
    for t in threads:
        out[t.state] = out.get(t.state, 0) + 1
    return out
