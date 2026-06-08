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


def test_open_outbound_to_retired_peer_omits_tombstone_as_next_owner() -> None:
    """review M3: alpha awaits beta, but beta has been retired — a tombstone
    can never reply, so the requester's next_owner hint must NOT name it
    (mirrors the broadcast path's retired-exclusion)."""
    msgs = [_msg("001", "alpha", "beta", "review-request", rid="r1", subject="WP1")]
    with_retired = derive_threads(msgs, agent="alpha", cursor="", now=_BASE,
                                  retired={"beta"})
    t = with_retired[0]
    assert t.state == "open-outbound"      # still visible (observability)
    assert t.next_action == "await-reply"
    assert t.next_owner is None            # tombstone NOT named as owner
    # regression guard: a LIVE peer is still named
    live = derive_threads(msgs, agent="alpha", cursor="", now=_BASE)[0]
    assert live.next_owner == "beta"


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
    # 0.14.0: "closed-superseded" added (rescind support, #12) — the one
    # explicitly-adjusted assertion under NFR-001's "asserts absence of
    # new output" carve-out. Additive only: all prior keys unchanged.
    c = counts([])
    assert c == {"reply-waiting": 0, "owed-inbound": 0, "open-outbound": 0,
                 "closed": 0, "closed-superseded": 0}


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


def test_question_with_stray_audience_but_no_broadcast_id_is_pairwise() -> None:
    """Fresh-review finding: detection must require broadcast_id, not just
    audience — else a plain `send --kind question --meta audience=foo` (or a
    review-request carrying audience) gets misrouted into the multi-party
    path where its real response kind can't close it."""
    m = Message(
        id="001", ts=_BASE.isoformat().replace("+00:00", "Z"),
        sender="alpha", recipient="beta", kind="question",
        subject="", body="x", meta={"request_id": "q1", "audience": "foo"},
    )
    rows = derive_threads([m], agent="alpha", cursor="", now=_BASE)
    assert rows[0].is_broadcast is False        # pairwise, not broadcast
    assert rows[0].state == "open-outbound"


def test_note_broadcast_is_not_tracked() -> None:
    """A note/message broadcast is FYI fan-out — no obligation, no thread."""
    msgs, _ = _broadcast("b-9", "lead", ["dev1", "dev2"], kind="note")
    assert derive_threads(msgs, agent="lead", cursor="") == []
    assert derive_threads(msgs, agent="dev1", cursor="") == []


def test_broadcast_excludes_non_participant() -> None:
    msgs, _ = _broadcast("b-1", "lead", ["dev1", "dev2"])
    assert derive_threads(msgs, agent="outsider", cursor="") == []


# ------------------------------------ 0.12.0: closed override + question closure

def test_closed_rids_override_forces_closed() -> None:
    msgs = [_msg("001", "alpha", "beta", "review-request", rid="r1")]
    # beta normally owes a review-result.
    assert derive_threads(msgs, agent="beta", cursor="")[0].state == "owed-inbound"
    # beta explicitly closed r1 (ack --to-request) → reported closed.
    closed = derive_threads(msgs, agent="beta", cursor="", closed_rids={"r1"})
    assert closed[0].state == "closed"


def test_closed_rids_override_forces_broadcast_closed() -> None:
    msgs, _ = _broadcast("b-1", "lead", ["dev1", "dev2"])
    assert derive_threads(msgs, agent="lead", cursor="")[0].state == "open-outbound"
    closed = derive_threads(msgs, agent="lead", cursor="", closed_rids={"b-1"})
    assert closed[0].state == "closed"


def test_review_result_closes_a_question() -> None:
    """0.12.0: a question is open-ended — any non-control reply from the
    asked party closes it, including a review-result (the production bug)."""
    msgs = [
        _msg("001", "alpha", "beta", "question", rid="q1"),
        _msg("002", "beta", "alpha", "review-result", rid="q1", status="approved"),
    ]
    assert derive_threads(msgs, agent="alpha", cursor="002")[0].state == "closed"


def test_broadcast_question_member_review_result_counts() -> None:
    msgs, nxt = _broadcast("b-1", "lead", ["dev1", "dev2"])
    msgs.append(_msg(f"{nxt:03d}", "dev1", "lead", "review-result",
                     rid="b-1", status="approved"))
    t = derive_threads(msgs, agent="lead", cursor=f"{nxt:03d}")[0]
    assert "dev1" in t.responded and t.pending == ["dev2"]


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


# ======================================================================
# Supersession (0.14.0, #12) - the D2 ordering rule
# ======================================================================

def _rescind(mid: str, sender: str, recipient: str, rid: str, *,
             target: str | None = None, body: str = "hold") -> "Message":
    m = _msg(mid, sender, recipient, "rescind", rid=rid)
    m.body = body
    if target is not None:
        m.meta["target_msg_id"] = target
    return m


def test_rescind_supersedes_for_both_perspectives() -> None:
    msgs = [
        _msg("001", "alpha", "beta", "question", rid="q1", subject="fire?"),
        _rescind("002", "alpha", "beta", "q1", body="new data - hold"),
    ]
    for agent in ("alpha", "beta"):
        t = derive_threads(msgs, agent=agent, cursor="", now=_BASE)[0]
        assert t.state == "closed-superseded"
        d = t.to_dict()
        assert d["rescind"]["id"] == "002"
        assert d["rescind"]["by"] == "alpha"
        assert d["rescind"]["reason"] == "new data - hold"


def test_rescind_by_non_requester_is_ignored() -> None:
    # Derivation-level guard (validate_rescind blocks honest sends; this
    # guards against anything that lands in the log another way).
    msgs = [
        _msg("001", "alpha", "beta", "question", rid="q1"),
        _rescind("002", "beta", "alpha", "q1"),
    ]
    t = derive_threads(msgs, agent="alpha", cursor="", now=_BASE)[0]
    assert t.state != "closed-superseded"


def test_responder_rescind_does_not_answer_a_question() -> None:
    # The open-ended question rule ("any non-control reply closes")
    # must NOT treat a rescind as the answer.
    msgs = [
        _msg("001", "alpha", "beta", "question", rid="q1"),
        _rescind("002", "beta", "alpha", "q1"),
    ]
    t = derive_threads(msgs, agent="beta", cursor="", now=_BASE)[0]
    assert t.state == "owed-inbound"  # beta still owes the answer


def test_rescind_target_msg_id_pinning() -> None:
    # Pinned anchor OLDER than the rescind -> supersedes.
    msgs = [
        _msg("001", "alpha", "beta", "question", rid="q1"),
        _rescind("003", "alpha", "beta", "q1", target="001"),
    ]
    assert derive_threads(msgs, agent="alpha", cursor="", now=_BASE)[0].state == "closed-superseded"
    # Pinned anchor NEWER than the rescind -> does not supersede.
    msgs = [
        _msg("001", "alpha", "beta", "question", rid="q1"),
        _rescind("002", "alpha", "beta", "q1", target="005"),
        _msg("005", "alpha", "beta", "question", rid="q1"),  # re-ask after
    ]
    t = derive_threads(msgs, agent="alpha", cursor="", now=_BASE)[0]
    assert t.state != "closed-superseded"


def test_reply_after_rescind_stays_superseded() -> None:
    msgs = [
        _msg("001", "alpha", "beta", "question", rid="q1"),
        _rescind("002", "alpha", "beta", "q1"),
        _msg("003", "beta", "alpha", "message", rid="q1"),  # late answer
    ]
    for agent in ("alpha", "beta"):
        t = derive_threads(msgs, agent=agent, cursor="", now=_BASE)[0]
        assert t.state == "closed-superseded"


def test_reask_does_not_reopen_superseded_thread() -> None:
    msgs = [
        _msg("001", "alpha", "beta", "question", rid="q1"),
        _msg("002", "beta", "alpha", "message", rid="q1"),   # answered
        _rescind("003", "alpha", "beta", "q1"),
        _msg("004", "alpha", "beta", "question", rid="q1"),  # re-ask
    ]
    for agent in ("alpha", "beta"):
        t = derive_threads(msgs, agent=agent, cursor="004", now=_BASE)[0]
        assert t.state == "closed-superseded"


def test_manual_ack_label_survives_rescind() -> None:
    # Codex WP01 review blocker 1: a per-agent manual ack is an explicit
    # "I handled this" - supersession overrides DERIVED states but never
    # relabels an ack (existing closure paths untouched). The other,
    # non-acking party still sees closed-superseded, and the check gate
    # computes supersession from the log regardless of view labels.
    msgs = [
        _msg("001", "alpha", "beta", "question", rid="q1"),
        _rescind("002", "alpha", "beta", "q1"),
    ]
    acked = derive_threads(msgs, agent="beta", cursor="", now=_BASE,
                           closed_rids={"q1"})[0]
    assert acked.state == "closed"
    assert "rescind" not in acked.to_dict()
    other = derive_threads(msgs, agent="alpha", cursor="", now=_BASE)[0]
    assert other.state == "closed-superseded"


def test_duplicate_rescinds_first_decides() -> None:
    msgs = [
        _msg("001", "alpha", "beta", "question", rid="q1"),
        _rescind("002", "alpha", "beta", "q1", body="first"),
        _rescind("003", "alpha", "beta", "q1", body="second"),
    ]
    t = derive_threads(msgs, agent="alpha", cursor="", now=_BASE)[0]
    assert t.state == "closed-superseded"
    assert t.to_dict()["rescind"]["id"] == "002"
    assert t.to_dict()["rescind"]["reason"] == "first"


def test_rescind_supersedes_broadcast_for_all_perspectives() -> None:
    msgs = [
        _msg("001", "lead", "w1", "question", rid="b1", audience="all"),
        _msg("002", "lead", "w2", "question", rid="b1", audience="all"),
        _msg("003", "w1", "lead", "message", rid="b1"),   # w1 answered
        _rescind("004", "lead", "w1", "b1"),
    ]
    for agent in ("lead", "w1", "w2"):
        t = derive_threads(msgs, agent=agent, cursor="", now=_BASE)[0]
        assert t.state == "closed-superseded", agent
        assert t.to_dict()["rescind"]["id"] == "004"
    # the pre-rescind answer still shows w1 as having responded
    t = derive_threads(msgs, agent="lead", cursor="", now=_BASE)[0]
    assert t.responded == ["w1"]


def test_member_rescind_does_not_count_as_broadcast_response() -> None:
    msgs = [
        _msg("001", "lead", "w1", "question", rid="b1", audience="all"),
        _msg("002", "lead", "w2", "question", rid="b1", audience="all"),
        _rescind("003", "w1", "lead", "b1"),  # member "rescind" (invalid use)
    ]
    t = derive_threads(msgs, agent="lead", cursor="", now=_BASE)[0]
    assert t.state == "open-outbound"        # not superseded (wrong sender)
    assert t.responded == []                 # and not an answer either
    assert sorted(t.pending) == ["w1", "w2"]


def test_counts_include_closed_superseded() -> None:
    msgs = [
        _msg("001", "alpha", "beta", "question", rid="q1"),
        _rescind("002", "alpha", "beta", "q1"),
        _msg("003", "alpha", "beta", "question", rid="q2"),
    ]
    c = counts(derive_threads(msgs, agent="alpha", cursor="", now=_BASE))
    assert c["closed-superseded"] == 1
    assert c["open-outbound"] == 1
    assert c["closed"] == 0
    # the key exists even when zero
    assert "closed-superseded" in counts([])


def test_superseded_threads_sort_with_closed() -> None:
    msgs = [
        _msg("001", "alpha", "beta", "question", rid="q1"),
        _rescind("002", "alpha", "beta", "q1"),
        _msg("003", "alpha", "beta", "question", rid="q2"),  # actionable
    ]
    threads = derive_threads(msgs, agent="alpha", cursor="", now=_BASE)
    assert [t.request_id for t in threads] == ["q2", "q1"]  # actionable first


def test_pairwise_to_dict_unchanged_without_new_features() -> None:
    # NFR-001 / strict additivity: a plain thread's JSON has no new keys.
    msgs = [_msg("001", "alpha", "beta", "question", rid="q1")]
    d = derive_threads(msgs, agent="alpha", cursor="", now=_BASE)[0].to_dict()
    assert "rescind" not in d
    assert "needs_operator" not in d
    assert "operator_state" not in d


# ======================================================================
# Escalation labels (0.14.0, #18)
# ======================================================================

def _escalation(mid: str, sender: str, recipient: str, rid: str) -> "Message":
    m = _msg(mid, sender, recipient, "question", rid=rid)
    m.meta["needs_operator"] = "true"
    return m


def test_escalation_pending_then_answered() -> None:
    msgs = [_escalation("001", "w1", "lead", "esc-1")]
    for agent, in (("w1",), ("lead",)):
        t = derive_threads(msgs, agent=agent, cursor="", now=_BASE)[0]
        assert t.needs_operator is True
        assert t.operator_state == "pending"
        d = t.to_dict()
        assert d["needs_operator"] is True and d["operator_state"] == "pending"
    answered = msgs + [_msg("002", "lead", "w1", "message", rid="esc-1")]
    t = derive_threads(answered, agent="lead", cursor="", now=_BASE)[0]
    assert t.operator_state == "answered"
    assert t.state == "closed"


def test_escalation_third_party_reply_does_not_answer() -> None:
    msgs = [
        _escalation("001", "w1", "lead", "esc-1"),
        _msg("002", "w2", "w1", "message", rid="esc-1"),  # not the liaison
    ]
    t = derive_threads(msgs, agent="w1", cursor="002", now=_BASE)[0]
    assert t.operator_state == "pending"


def test_escalation_superseded_leaves_pending_bucket() -> None:
    # A worker rescinding its own escalation removes the obligation -
    # but it was never ANSWERED (Codex WP01 review blocker 2 / FR-014):
    # terminal-without-an-answer is labeled "closed", not "answered".
    msgs = [
        _escalation("001", "w1", "lead", "esc-1"),
        _rescind("002", "w1", "lead", "esc-1"),
    ]
    t = derive_threads(msgs, agent="lead", cursor="", now=_BASE)[0]
    assert t.state == "closed-superseded"
    assert t.operator_state == "closed"    # left the bucket, no fabricated answer


def test_escalation_acked_is_closed_not_answered() -> None:
    # Same FR-014 rule for the manual-ack path: the liaison acking the
    # thread view does not fabricate an operator answer.
    msgs = [_escalation("001", "w1", "lead", "esc-1")]
    t = derive_threads(msgs, agent="lead", cursor="", now=_BASE,
                       closed_rids={"esc-1"})[0]
    assert t.state == "closed"
    assert t.operator_state == "closed"


# ======================================================================
# 0.15.0 NA labels + frozen fan-out facts (WP01, #15/#16)
# ======================================================================

def _na(mid: str, sender: str, recipient: str, rid: str) -> "Message":
    m = _msg(mid, sender, recipient, "message", rid=rid)
    m.meta["response"] = "not-applicable"
    return m


def test_broadcast_responded_na_both_perspectives() -> None:
    msgs = [
        _msg("001", "lead", "w1", "question", rid="b1", audience="all"),
        _msg("002", "lead", "w2", "question", rid="b1", audience="all"),
        _na("003", "w1", "lead", "b1"),
        _msg("004", "w2", "lead", "message", rid="b1"),  # substantive
    ]
    t = derive_threads(msgs, agent="lead", cursor="004", now=_BASE)[0]
    assert t.responded == ["w1", "w2"]
    assert t.responded_na == ["w1"]
    assert t.to_dict()["responded_na"] == ["w1"]
    # member perspective: closure unchanged (NA is a label, not mechanics)
    t1 = derive_threads(msgs, agent="w1", cursor="004", now=_BASE)[0]
    assert t1.state == "closed"


def test_pairwise_na_response_label() -> None:
    msgs = [
        _msg("001", "alpha", "beta", "question", rid="q1"),
        _na("002", "beta", "alpha", "q1"),
    ]
    t = derive_threads(msgs, agent="alpha", cursor="002", now=_BASE)[0]
    assert t.state == "closed"          # mechanics unchanged
    assert t.na_response is True
    assert t.to_dict()["na_response"] is True
    # a substantive answer does NOT carry the label
    msgs2 = [
        _msg("001", "alpha", "beta", "question", rid="q2"),
        _msg("002", "beta", "alpha", "message", rid="q2"),
    ]
    t2 = derive_threads(msgs2, agent="alpha", cursor="002", now=_BASE)[0]
    assert t2.na_response is False
    assert "na_response" not in t2.to_dict()


def test_na_label_cleared_by_reask() -> None:
    # reopened thread: the OLD NA terminal must not label the new round
    msgs = [
        _msg("001", "alpha", "beta", "question", rid="q1"),
        _na("002", "beta", "alpha", "q1"),
        _msg("003", "alpha", "beta", "question", rid="q1"),  # re-ask
    ]
    t = derive_threads(msgs, agent="alpha", cursor="003", now=_BASE)[0]
    assert t.state == "open-outbound"
    assert t.na_response is False


def test_batch_and_audience_kind_passthrough() -> None:
    msgs = [
        _msg("001", "lead", "w1", "question", rid="b1", audience="reviewer"),
        _msg("002", "lead", "w2", "question", rid="b1", audience="reviewer"),
    ]
    for m in msgs:
        m.meta["batch_total"] = "3"          # one copy missing!
        m.meta["audience_kind"] = "role"
    t = derive_threads(msgs, agent="lead", cursor="", now=_BASE)[0]
    assert t.batch_total == 3
    assert t.audience_kind == "role"
    d = t.to_dict()
    assert d["batch_total"] == 3 and d["audience_kind"] == "role"
    # garbage batch_total degrades to None (absent in dict)
    for m in msgs:
        m.meta["batch_total"] = "many"
    t2 = derive_threads(msgs, agent="lead", cursor="", now=_BASE)[0]
    assert t2.batch_total is None
    assert "batch_total" not in t2.to_dict()


def test_freeze_independence_roles_change_after_send() -> None:
    # C-004 structural guard: derivation reads MESSAGES only; there is no
    # config argument to drift. Same message set -> identical output, by
    # signature. (The e2e WP re-proves this through the CLI.)
    msgs = [
        _msg("001", "lead", "w1", "question", rid="b1", audience="reviewer"),
        _msg("002", "lead", "w2", "question", rid="b1", audience="reviewer"),
    ]
    a = derive_threads(msgs, agent="lead", cursor="", now=_BASE)[0].to_dict()
    b = derive_threads(msgs, agent="lead", cursor="", now=_BASE)[0].to_dict()
    assert a == b
    assert sorted(a["pending"]) == ["w1", "w2"]


def test_plain_threads_emit_no_new_keys() -> None:
    msgs = [_msg("001", "alpha", "beta", "question", rid="q1")]
    d = derive_threads(msgs, agent="alpha", cursor="", now=_BASE)[0].to_dict()
    for k in ("responded_na", "na_response", "batch_total", "audience_kind"):
        assert k not in d


# ===================================================== #19 Phase A (WP02)
# Read-only next_owner / next_action derivation.

def test_next_owed_inbound_is_self_reply() -> None:
    msgs = [_msg("001", "alpha", "beta", "review-request", rid="r1")]
    b = derive_threads(msgs, agent="beta", cursor="", now=_BASE)[0]
    assert b.next_action == "reply" and b.next_owner == "beta"
    # Surfacing into JSON is the CLI's job (WP03); to_dict stays shape-stable.
    assert "next_action" not in b.to_dict()


def test_next_open_outbound_is_await_peer() -> None:
    msgs = [_msg("001", "alpha", "beta", "review-request", rid="r1")]
    a = derive_threads(msgs, agent="alpha", cursor="", now=_BASE)[0]
    assert a.state == "open-outbound"
    assert a.next_action == "await-reply" and a.next_owner == "beta"


def test_next_reply_waiting_is_self_read() -> None:
    msgs = [
        _msg("001", "alpha", "beta", "review-request", rid="r1"),
        _msg("002", "beta", "alpha", "review-result", rid="r1", status="approved"),
    ]
    a = derive_threads(msgs, agent="alpha", cursor="", now=_BASE)[0]
    assert a.state == "reply-waiting"
    assert a.next_action == "read-reply" and a.next_owner == "alpha"


def test_next_omitted_on_closed_thread() -> None:
    msgs = [
        _msg("001", "alpha", "beta", "review-request", rid="r1"),
        _msg("002", "beta", "alpha", "review-result", rid="r1", status="approved"),
    ]
    # consumed by alpha (cursor past the verdict) -> closed
    a = derive_threads(msgs, agent="alpha", cursor="002", now=_BASE)[0]
    assert a.state == "closed"
    assert a.next_action is None and a.next_owner is None


def test_next_answer_operator_when_escalation_pending() -> None:
    opener = _msg("001", "alpha", "beta", "question", rid="r1")
    opener.meta["needs_operator"] = True
    b = derive_threads([opener], agent="beta", cursor="", now=_BASE)[0]
    assert b.needs_operator and b.operator_state == "pending"
    assert b.next_action == "answer-operator" and b.next_owner == "beta"


def test_next_broadcast_open_outbound_lists_non_responders() -> None:
    # alpha broadcasts to beta + gamma; beta answers AND alpha has consumed
    # that reply (cursor past it) -> gamma still owes -> open-outbound.
    msgs = [
        _msg("001", "alpha", "beta", "question", rid="b1", audience="all"),
        _msg("002", "alpha", "gamma", "question", rid="b1", audience="all"),
        _msg("003", "beta", "alpha", "message", rid="b1"),
    ]
    a = derive_threads(msgs, agent="alpha", cursor="003", now=_BASE)[0]
    assert a.is_broadcast and a.state == "open-outbound"
    assert a.next_action == "await-reply"
    assert a.next_owner == ["gamma"]          # exactly the non-responder


def test_to_dict_never_emits_next_fields() -> None:
    # to_dict() stays shape-stable for ALL thread states — next_* live on the
    # Thread object and are surfaced into JSON by the CLI layer (WP03), not
    # here. This keeps the 0.15.0 additivity gates green at the library layer.
    open_msgs = [_msg("001", "alpha", "beta", "review-request", rid="r1")]
    closed_msgs = open_msgs + [
        _msg("002", "beta", "alpha", "review-result", rid="r1", status="approved"),
    ]
    for msgs, cur in ((open_msgs, ""), (closed_msgs, "002")):
        d = derive_threads(msgs, agent="alpha", cursor=cur, now=_BASE)[0].to_dict()
        assert "next_action" not in d and "next_owner" not in d


# ===================================== 0.18.0 (WP02): retired audience members

def test_broadcast_excludes_retired_from_pending_and_owner() -> None:
    """A retired audience member can never reply, so it must not appear in
    `pending` or the await-reply `next_owner` — but it stays in the frozen
    `audience` and is surfaced via `audience_retired` (0.18.0, FR-006)."""
    msgs, nxt = _broadcast("b-1", "lead", ["dev1", "dev2", "dev3"])
    msgs.append(_msg(f"{nxt:03d}", "dev1", "lead", "message", rid="b-1"))  # dev1 replies
    L = derive_threads(msgs, agent="lead", cursor=f"{nxt:03d}", now=_BASE,
                       retired={"dev3"})
    t = L[0]
    assert t.audience == ["dev1", "dev2", "dev3"]          # frozen, unchanged
    assert t.audience_retired == ["dev3"]
    assert "dev3" not in t.pending                          # tombstone not owed
    assert t.pending == ["dev2"]
    # next_owner (await-reply on the pending set) never names the tombstone
    if isinstance(t.next_owner, list):
        assert "dev3" not in t.next_owner
    d = t.to_dict()
    assert d["audience_retired"] == ["dev3"]


def test_broadcast_no_audience_retired_key_when_none() -> None:
    """Additivity: a clean broadcast emits no `audience_retired` key."""
    msgs, _ = _broadcast("b-1", "lead", ["dev1", "dev2"])
    L = derive_threads(msgs, agent="lead", cursor="", now=_BASE)
    assert L[0].audience_retired == []
    assert "audience_retired" not in L[0].to_dict()
