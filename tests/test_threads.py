"""Tests for thread derivation (the `agenttalk threads` engine).

The state machine is the riskiest new logic in 0.10.0, so it gets
exhaustive unit coverage against hand-built Message lists, plus a
store-level check that derivation only ever sees *validated* messages.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from agenttalk.store import Message, Store
from agenttalk.threads import counts, derive_threads


# --------------------------------------------------------------- helpers

_BASE = datetime(2026, 6, 2, 12, 0, 0, tzinfo=timezone.utc)


def _msg(
    mid: str,
    sender: str,
    recipient: str,
    kind: str = "message",
    *,
    rid: str | None = None,
    status: str | None = None,
    subject: str = "",
    ts: datetime | None = None,
    audience: str | None = None,
) -> Message:
    meta: dict = {}
    if rid is not None:
        meta["request_id"] = rid
    if status is not None:
        meta["status"] = status
    if audience is not None:
        meta["audience"] = audience
        meta["broadcast_id"] = rid
    when = (ts or _BASE).isoformat().replace("+00:00", "Z")
    # ids are zero-padded so lexicographic order == intended order
    return Message(
        id=mid, ts=when, sender=sender, recipient=recipient,
        kind=kind, subject=subject, body="x", meta=meta,
    )


def _states(threads):
    return {t.request_id: t.state for t in threads}


# ------------------------------------------------------ basic open thread

def test_open_outbound_and_owed_inbound_two_perspectives() -> None:
    msgs = [_msg("001", "alpha", "beta", "review-request", rid="r1", subject="WP1")]
    a = derive_threads(msgs, agent="alpha", cursor="", now=_BASE)
    b = derive_threads(msgs, agent="beta", cursor="", now=_BASE)
    assert len(a) == 1 and a[0].state == "open-outbound" and a[0].role == "opener"
    assert a[0].peer == "beta"
    assert len(b) == 1 and b[0].state == "owed-inbound" and b[0].role == "responder"
    assert b[0].peer == "alpha"


# ------------------------------------------------------- response closes

def test_review_result_closes_for_requester_once_consumed() -> None:
    msgs = [
        _msg("001", "alpha", "beta", "review-request", rid="r1"),
        _msg("002", "beta", "alpha", "review-result", rid="r1", status="approved"),
    ]
    # Before consuming: the verdict is sitting unread → reply-waiting.
    pending = derive_threads(msgs, agent="alpha", cursor="", now=_BASE)
    assert pending[0].state == "reply-waiting"
    assert pending[0].unread is True
    # After consuming (cursor past the verdict): closed.
    done = derive_threads(msgs, agent="alpha", cursor="002", now=_BASE)
    assert done[0].state == "closed"
    assert done[0].unread is False
    # The reviewer (beta) sees it closed regardless of cursor — they sent
    # the terminal response.
    bclosed = derive_threads(msgs, agent="beta", cursor="", now=_BASE)
    assert bclosed[0].state == "closed"


# --------------------------------------------------- needs-info bounces

def test_needs_info_flips_obligation_back_to_requester() -> None:
    msgs = [
        _msg("001", "alpha", "beta", "review-request", rid="r1"),
        _msg("002", "beta", "alpha", "review-result", rid="r1", status="needs-info"),
    ]
    # Reviewer (beta) is now waiting on the requester → open-outbound.
    b = derive_threads(msgs, agent="beta", cursor="002", now=_BASE)
    assert b[0].state == "open-outbound"
    # Requester (alpha), having consumed the needs-info, owes the answer.
    a = derive_threads(msgs, agent="alpha", cursor="002", now=_BASE)
    assert a[0].state == "owed-inbound"
    # ...but before consuming it, it's reply-waiting (go read it).
    a_unread = derive_threads(msgs, agent="alpha", cursor="", now=_BASE)
    assert a_unread[0].state == "reply-waiting"


def test_needs_info_answer_swings_ball_back_to_reviewer() -> None:
    """After needs-info, the requester answers with a plain message; the
    ball must swing back to the reviewer (regression: the bounce used to
    be one-way, stranding the reviewer in a misleading open-outbound)."""
    msgs = [
        _msg("001", "alpha", "beta", "review-request", rid="r1"),
        _msg("002", "beta", "alpha", "review-result", rid="r1", status="needs-info"),
        _msg("003", "alpha", "beta", "message", rid="r1"),  # the supplied info
    ]
    # Reviewer beta has NOT read the answer yet → it's waiting in the inbox.
    b_unread = derive_threads(msgs, agent="beta", cursor="002", now=_BASE)
    assert b_unread[0].state == "reply-waiting"
    # Reviewer beta consumed the answer → beta now owes the re-review.
    b_seen = derive_threads(msgs, agent="beta", cursor="003", now=_BASE)
    assert b_seen[0].state == "owed-inbound"
    # Requester alpha, having answered, is back to waiting on beta.
    a = derive_threads(msgs, agent="alpha", cursor="003", now=_BASE)
    assert a[0].state == "open-outbound"


def test_multi_round_consult_reopens_thread() -> None:
    """The consult convention reuses the SAME request_id for round 2. A
    re-ask after the round-1 answer must re-open the thread, not leave it
    stuck closed (workflow review finding)."""
    msgs = [
        _msg("001", "alpha", "beta", "question", rid="c1"),
        _msg("002", "beta", "alpha", "message", rid="c1"),       # round-1 answer
        _msg("003", "alpha", "beta", "question", rid="c1"),      # round-2 re-ask
    ]
    # Round-1 only: closed once the asker consumed the answer.
    r1 = derive_threads(msgs[:2], agent="alpha", cursor="002", now=_BASE)
    assert r1[0].state == "closed"
    # After re-asking: alpha awaits the round-2 answer.
    a = derive_threads(msgs, agent="alpha", cursor="003", now=_BASE)
    assert a[0].state == "open-outbound"
    # beta owes the round-2 answer (reply-waiting until it reads the re-ask).
    b_unread = derive_threads(msgs, agent="beta", cursor="002", now=_BASE)
    assert b_unread[0].state == "reply-waiting"
    b_seen = derive_threads(msgs, agent="beta", cursor="003", now=_BASE)
    assert b_seen[0].state == "owed-inbound"


# ---------------------------------------------------- proposal counter

def test_countered_proposal_closes_this_thread() -> None:
    msgs = [
        _msg("001", "alpha", "beta", "proposal", rid="p1"),
        _msg("002", "beta", "alpha", "proposal-response", rid="p1", status="countered"),
    ]
    a = derive_threads(msgs, agent="alpha", cursor="002", now=_BASE)
    assert a[0].state == "closed"  # the counter is a fresh proposal thread
    b = derive_threads(msgs, agent="beta", cursor="002", now=_BASE)
    assert b[0].state == "closed"


def test_accepted_proposal_closes() -> None:
    msgs = [
        _msg("001", "alpha", "beta", "proposal", rid="p1"),
        _msg("002", "beta", "alpha", "proposal-response", rid="p1", status="accepted"),
    ]
    assert _states(derive_threads(msgs, agent="alpha", cursor="002"))["p1"] == "closed"


# -------------------------------------------------- question answered

def test_question_closed_by_plain_message() -> None:
    msgs = [
        _msg("001", "alpha", "beta", "question", rid="q1"),
        _msg("002", "beta", "alpha", "message", rid="q1"),
    ]
    assert _states(derive_threads(msgs, agent="alpha", cursor="002"))["q1"] == "closed"


# ----------------------------------- expected-response map is enforced

def test_generic_message_does_not_close_review_request() -> None:
    """A chat message echoing the request_id must NOT close a review
    request — only a review-result does (Codex review gap #3)."""
    msgs = [
        _msg("001", "alpha", "beta", "review-request", rid="r1"),
        _msg("002", "beta", "alpha", "message", rid="r1"),  # not a review-result
    ]
    a = derive_threads(msgs, agent="alpha", cursor="002", now=_BASE)
    assert a[0].state == "open-outbound"  # still waiting on a real verdict


def test_response_from_wrong_direction_does_not_close() -> None:
    """A 'response' must flow recipient->opener. One sent opener->recipient
    (wrong way) does not count."""
    msgs = [
        _msg("001", "alpha", "beta", "review-request", rid="r1"),
        _msg("002", "alpha", "beta", "review-result", rid="r1", status="approved"),
    ]
    a = derive_threads(msgs, agent="alpha", cursor="002", now=_BASE)
    assert a[0].state == "open-outbound"


# ------------------------------------------------------- edge: untracked

def test_messages_without_request_id_are_untracked() -> None:
    msgs = [_msg("001", "alpha", "beta", "message")]  # no rid
    assert derive_threads(msgs, agent="alpha", cursor="") == []


def test_orphan_response_without_opener_is_skipped() -> None:
    msgs = [_msg("001", "beta", "alpha", "review-result", rid="r9", status="approved")]
    assert derive_threads(msgs, agent="alpha", cursor="") == []


def test_not_my_thread_excluded() -> None:
    msgs = [_msg("001", "gamma", "delta", "review-request", rid="r1")]
    assert derive_threads(msgs, agent="alpha", cursor="") == []


# --------------------------------------------------------- ordering / counts

def test_actionable_sorted_before_closed_and_stale_first() -> None:
    old = _BASE - timedelta(minutes=30)
    msgs = [
        # closed thread
        _msg("001", "alpha", "beta", "review-request", rid="closed1"),
        _msg("002", "beta", "alpha", "review-result", rid="closed1", status="approved"),
        # a fresh open-outbound
        _msg("010", "alpha", "beta", "review-request", rid="fresh", ts=_BASE),
        # a stale open-outbound (older activity)
        _msg("005", "alpha", "beta", "proposal", rid="stale", ts=old),
    ]
    rows = derive_threads(msgs, agent="alpha", cursor="002", now=_BASE)
    states = [t.state for t in rows]
    # closed is last
    assert states[-1] == "closed"
    # among the two open-outbound rows, the stale (older) one comes first
    open_ids = [t.request_id for t in rows if t.state == "open-outbound"]
    assert open_ids == ["stale", "fresh"]


def test_counts_has_all_keys() -> None:
    c = counts([])
    assert c == {"reply-waiting": 0, "owed-inbound": 0, "open-outbound": 0, "closed": 0}


# ----------------------------------------- multi-party broadcast threads

def _broadcast(bid, sender, members, *, kind="question", start="001"):
    """Build the fan-out opener copies for a broadcast."""
    out = []
    n = int(start)
    for m in members:
        out.append(_msg(f"{n:03d}", sender, m, kind, rid=bid,
                        audience="reviewers", subject="review WP7"))
        n += 1
    return out, n


def test_broadcast_broadcaster_and_member_views() -> None:
    msgs, _ = _broadcast("b-1", "lead", ["dev1", "dev2"])
    # Broadcaster sees one multi-party thread, nobody responded yet.
    L = derive_threads(msgs, agent="lead", cursor="", now=_BASE)
    assert len(L) == 1
    t = L[0]
    assert t.is_broadcast and t.role == "opener" and t.state == "open-outbound"
    assert t.peer == "@reviewers"
    assert t.audience == ["dev1", "dev2"]
    assert t.responded == [] and t.pending == ["dev1", "dev2"]
    # A member owes a reply.
    d1 = derive_threads(msgs, agent="dev1", cursor="", now=_BASE)
    assert d1[0].is_broadcast and d1[0].state == "owed-inbound" and d1[0].peer == "lead"


def test_broadcast_partial_then_full_responses() -> None:
    msgs, nxt = _broadcast("b-1", "lead", ["dev1", "dev2"])
    msgs.append(_msg(f"{nxt:03d}", "dev1", "lead", "message", rid="b-1"))  # dev1 replies
    # Before lead consumes dev1's reply: reply-waiting, 1 of 2, dev2 pending.
    L = derive_threads(msgs, agent="lead", cursor="", now=_BASE)
    assert L[0].state == "reply-waiting"
    assert L[0].responded == ["dev1"] and L[0].pending == ["dev2"]
    # After consuming, still open-outbound (dev2 outstanding).
    L2 = derive_threads(msgs, agent="lead", cursor=f"{nxt:03d}", now=_BASE)
    assert L2[0].state == "open-outbound" and L2[0].pending == ["dev2"]
    # dev1's own slice is closed; dev2 still owes.
    assert derive_threads(msgs, agent="dev1", cursor=f"{nxt:03d}")[0].state == "closed"
    assert derive_threads(msgs, agent="dev2", cursor="")[0].state == "owed-inbound"
    # dev2 replies too → fully closed for the broadcaster.
    msgs.append(_msg(f"{nxt + 1:03d}", "dev2", "lead", "message", rid="b-1"))
    done = derive_threads(msgs, agent="lead", cursor=f"{nxt + 1:03d}", now=_BASE)
    assert done[0].state == "closed"
    assert done[0].responded == ["dev1", "dev2"] and done[0].pending == []


def test_note_broadcast_is_not_tracked() -> None:
    """A note/message broadcast is FYI fan-out — no obligation, no thread."""
    msgs, _ = _broadcast("b-9", "lead", ["dev1", "dev2"], kind="note")
    assert derive_threads(msgs, agent="lead", cursor="") == []
    assert derive_threads(msgs, agent="dev1", cursor="") == []


def test_broadcast_excludes_non_participant() -> None:
    msgs, _ = _broadcast("b-1", "lead", ["dev1", "dev2"])
    assert derive_threads(msgs, agent="outsider", cursor="") == []


def test_broadcast_open_outbound_age_from_broadcast_not_partial_reply() -> None:
    """Regression: a half-answered broadcast that then goes silent must still
    age from the original ask, so the stale-thread warning can fire — not
    reset to 'time since the last reply'."""
    old = _BASE - timedelta(days=10)
    recent = _BASE - timedelta(seconds=5)
    msgs = [
        _msg("001", "lead", "dev1", "question", rid="b-1", audience="all", ts=old),
        _msg("002", "lead", "dev2", "question", rid="b-1", audience="all", ts=old),
        _msg("003", "dev1", "lead", "message", rid="b-1", ts=recent),  # dev1 replied
    ]
    # lead consumed dev1's reply; dev2 silent → open-outbound.
    t = derive_threads(msgs, agent="lead", cursor="003", now=_BASE)[0]
    assert t.state == "open-outbound" and t.pending == ["dev2"]
    assert t.age_seconds > 9 * 86400      # ~10 days, NOT ~5 seconds
    assert t.last_msg_id in ("001", "002")  # an opener, not the reply


def test_broadcast_member_closed_last_is_own_reply() -> None:
    msgs, nxt = _broadcast("b-1", "lead", ["dev1", "dev2"])
    reply_id = f"{nxt:03d}"
    msgs.append(_msg(reply_id, "dev1", "lead", "message", rid="b-1"))
    t = derive_threads(msgs, agent="dev1", cursor=reply_id, now=_BASE)[0]
    assert t.state == "closed"
    assert t.last_msg_id == reply_id  # my reply, not the original opener


# --------------------------------------- store-level: validated input only

def test_valid_messages_spans_recipients_and_excludes_garbage(
    store: Store, store_root: Path,
) -> None:
    store.send(sender="alpha", recipient="beta", body="to beta", kind="message")
    store.send(sender="beta", recipient="alpha", body="to alpha", kind="message")
    # Drop a corrupt file straight into the message dir — it must never
    # reach derivation.
    (store_root / ".agenttalk" / "messages" / "zzz-garbage.json").write_text(
        "{ not valid json", encoding="utf-8",
    )
    msgs = store.valid_messages()
    recipients = sorted(m.recipient for m in msgs)
    assert recipients == ["alpha", "beta"]  # both directions, garbage gone


def test_threads_derives_from_validated_set(store: Store) -> None:
    """End-to-end: a proposal then its acceptance closes the thread when
    read through the store's validated view."""
    store.send(sender="alpha", recipient="beta", body="p", kind="proposal",
               meta={"request_id": "pp-x"})
    rows = derive_threads(store.valid_messages(), agent="beta", cursor="")
    assert rows[0].state == "owed-inbound"
    store.send(sender="beta", recipient="alpha", body="ok", kind="proposal-response",
               meta={"request_id": "pp-x", "status": "accepted"})
    rows2 = derive_threads(
        store.valid_messages(), agent="alpha",
        cursor=store.all_messages()[-1].id,
    )
    assert rows2[0].state == "closed"
