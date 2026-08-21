"""#201 wrapper-owned reply delivery (freeform path).

A wrapped child whose harness statically rejects or approval-gates shell
commands (the JAWS claude seat: 5/5 turns undeliverable) answers by writing
the wrapper-declared draft file; the wrapper validates and publishes it with
exact thread correlation. Fixture Store + injected drive — NO real CLI here
(the real spawn path is covered by the stub canary's draft_only scenario).
"""

from __future__ import annotations

from pathlib import Path

from agenttalk import reply_transport
from agenttalk.store import Store
from agenttalk.wrapper import loop, prompt


def _store(tmp_path) -> Store:
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    return s


def _reply_inbox(s: Store, requester: str) -> list:
    return [m for m in s.messages_for(requester) if m.sender == "beta"]


# ------------------------------------------------- correlation echo parity

def test_echo_reply_correlation_broadcast_copy_echoes_request_id_only() -> None:
    # The #201 design-review blocker: a broadcast copy carries BOTH ids; the
    # reply must echo just request_id or the operation digest forks between
    # producers of the same reply.
    meta: dict = {}
    reply_transport.echo_reply_correlation(
        meta, anchor_id="m-1",
        anchor_meta={"request_id": "bid-7", "broadcast_id": "bid-7"},
        kind="message",
    )
    assert meta == {"in_reply_to": "m-1", "request_id": "bid-7"}


def test_echo_reply_correlation_broadcast_id_only_when_no_request_id() -> None:
    meta: dict = {}
    reply_transport.echo_reply_correlation(
        meta, anchor_id="m-2", anchor_meta={"broadcast_id": "b-9"}, kind="message",
    )
    assert meta == {"in_reply_to": "m-2", "broadcast_id": "b-9"}


def test_echo_reply_correlation_thread_opening_kinds_inherit_nothing() -> None:
    for kind in ("review-request", "proposal"):
        meta: dict = {}
        reply_transport.echo_reply_correlation(
            meta, anchor_id="m-3",
            anchor_meta={"request_id": "q-1", "broadcast_id": "b-1"},
            kind=kind,
        )
        assert meta == {"in_reply_to": "m-3"}


def test_echo_reply_correlation_explicit_meta_wins() -> None:
    meta = {"request_id": "explicit"}
    reply_transport.echo_reply_correlation(
        meta, anchor_id="m-4", anchor_meta={"request_id": "anchor"}, kind="message",
    )
    assert meta["request_id"] == "explicit"


# ------------------------------------------------------------ loop: happy

def test_clean_turn_draft_is_published_with_exact_correlation(tmp_path) -> None:
    s = _store(tmp_path)
    q = s.send(sender="alpha", recipient="beta", kind="question",
               subject="s", body="q?", meta={"request_id": "q-abc123"})
    drafts: list[Path] = []

    def drive(rec):
        assert isinstance(rec.get("reply_draft"), dict)
        p = Path(rec["reply_draft"]["path"])
        p.write_text("the answer\nline two\n", encoding="utf-8")
        drafts.append(p)
        return True

    turns = loop.run_loop(s, "beta", drive, clock=lambda: 0.0,
                          sleep=lambda d: None, max_turns=1)
    assert turns == 1
    replies = _reply_inbox(s, "alpha")
    assert len(replies) == 1
    msg = replies[0]
    assert msg.body == "the answer\nline two\n"
    assert msg.kind == "message"
    assert (msg.meta or {}).get("in_reply_to") == q.id
    assert (msg.meta or {}).get("request_id") == "q-abc123"
    assert not drafts[0].exists()          # draft consumed
    assert s.cursor("beta") == q.id        # committed


def test_broadcast_copy_reply_via_draft_echoes_request_id_only(tmp_path) -> None:
    s = _store(tmp_path)
    q = s.send(sender="alpha", recipient="beta", kind="question", body="b?",
               meta={"request_id": "bid-55", "broadcast_id": "bid-55"})

    def drive(rec):
        Path(rec["reply_draft"]["path"]).write_text("bcast answer", encoding="utf-8")
        return True

    loop.run_loop(s, "beta", drive, clock=lambda: 0.0, sleep=lambda d: None,
                  max_turns=1)
    (msg,) = _reply_inbox(s, "alpha")
    meta = msg.meta or {}
    assert meta.get("request_id") == "bid-55"
    assert "broadcast_id" not in meta
    assert meta.get("in_reply_to") == q.id


# ---------------------------------------------------------- loop: refusals

def test_dirty_turn_never_publishes_a_truncated_draft(tmp_path) -> None:
    # Design-review blocker F2: a child killed mid-Write leaves a partial,
    # valid-UTF-8 draft. A non-ok outcome must never publish it.
    s = _store(tmp_path)
    s.send(sender="alpha", recipient="beta", kind="question", body="q?",
           meta={"request_id": "q-dirty"})

    def dying_drive(rec):
        Path(rec["reply_draft"]["path"]).write_text("partial ans", encoding="utf-8")
        return loop.DriveOutcome(ok=False, failure_class=loop.CLASS_INFRA,
                                 summary="watchdog kill")

    loop.run_loop(s, "beta", dying_drive, clock=lambda: 0.0, sleep=lambda d: None,
                  max_polls=2, k_poison=0, k_escalate=0)
    assert _reply_inbox(s, "alpha") == []
    assert s.cursor("beta") == ""          # failed turn: not committed


def test_both_channels_land_exactly_one_reply(tmp_path) -> None:
    # A capable child that ran `agenttalk reply` itself AND wrote the draft:
    # the wrapper's landed-check must not publish a duplicate.
    s = _store(tmp_path)
    q = s.send(sender="alpha", recipient="beta", kind="question", body="q?",
               meta={"request_id": "q-two"})

    def drive(rec):
        s.send(sender="beta", recipient="alpha", body="cli answer",
               meta={"in_reply_to": rec["id"], "request_id": "q-two"})
        Path(rec["reply_draft"]["path"]).write_text("draft answer", encoding="utf-8")
        return True

    loop.run_loop(s, "beta", drive, clock=lambda: 0.0, sleep=lambda d: None,
                  max_turns=1)
    replies = _reply_inbox(s, "alpha")
    assert [m.body for m in replies] == ["cli answer"]
    assert not Path(
        reply_transport.reply_draft_path(s, "beta", q.id)
    ).exists()                             # residue draft cleaned


def test_no_draft_leaves_turn_behavior_unchanged(tmp_path) -> None:
    s = _store(tmp_path)
    q = s.send(sender="alpha", recipient="beta", kind="question", body="q?")

    turns = loop.run_loop(s, "beta", lambda rec: True, clock=lambda: 0.0,
                          sleep=lambda d: None, max_turns=1)
    assert turns == 1
    assert _reply_inbox(s, "alpha") == []
    assert s.cursor("beta") == q.id


def test_empty_or_oversize_draft_is_refused(tmp_path) -> None:
    s = _store(tmp_path)
    s.send(sender="alpha", recipient="beta", kind="question", body="q?")

    def drive(rec):
        Path(rec["reply_draft"]["path"]).write_text("   \n", encoding="utf-8")
        return True

    loop.run_loop(s, "beta", drive, clock=lambda: 0.0, sleep=lambda d: None,
                  max_turns=1)
    assert _reply_inbox(s, "alpha") == []

    big = s.send(sender="alpha", recipient="beta", kind="question", body="q2?")

    def big_drive(rec):
        Path(rec["reply_draft"]["path"]).write_text(
            "x" * (reply_transport.MAX_DRAFT_BYTES + 1), encoding="utf-8")
        return True

    loop.run_loop(s, "beta", big_drive, clock=lambda: 0.0, sleep=lambda d: None,
                  max_turns=1)
    assert _reply_inbox(s, "alpha") == []
    assert s.cursor("beta") == big.id      # still a clean commit


def test_typed_response_kinds_get_no_draft_channel(tmp_path) -> None:
    # A review-request needs a typed review-result; the draft channel would
    # close it with kind=message, so those records are not decorated.
    s = _store(tmp_path)
    s.send(sender="alpha", recipient="beta", kind="review-request", body="rr",
           meta={"request_id": "rq-1"})
    seen: list[dict] = []

    def drive(rec):
        seen.append(rec)
        return True

    loop.run_loop(s, "beta", drive, clock=lambda: 0.0, sleep=lambda d: None,
                  max_turns=1)
    assert seen and "reply_draft" not in seen[0]


# ------------------------------------------------------------------ prompt

def test_prompt_renders_draft_channel_when_declared() -> None:
    rec = {"from": "lead", "to": "w", "kind": "question", "body": "q",
           "correlation_id": "q-1", "request_id": "q-1", "broadcast_id": None,
           "id": "m-1", "reply_draft": {"path": "X:/drafts/m-1.md"}}
    p = prompt.assemble_turn_prompt(rec)
    assert "PREFERRED DRAFT CHANNEL" in p
    assert "X:/drafts/m-1.md" in p
    assert "Use ONE channel, never both." in p
    assert "HOW TO REPLY TO THIS MESSAGE" in p      # CLI channel still offered


def test_prompt_no_draft_section_without_declaration() -> None:
    rec = {"from": "lead", "to": "w", "kind": "question", "body": "q",
           "correlation_id": "q-1", "request_id": "q-1", "broadcast_id": None,
           "id": "m-1"}
    assert "PREFERRED DRAFT CHANNEL" not in prompt.assemble_turn_prompt(rec)


def test_stale_draft_from_failed_attempt_is_never_published(tmp_path) -> None:
    # PR #127 connector P1: attempt 1 writes a partial draft and fails; the
    # retry completes cleanly WITHOUT writing a new draft. The stale bytes
    # must not be published — decoration deletes any pre-existing draft, so
    # each draft is bound to the attempt that created it.
    s = _store(tmp_path)
    q = s.send(sender="alpha", recipient="beta", kind="question", body="q?",
               meta={"request_id": "q-stale"})
    attempts = {"n": 0}

    def drive(rec):
        attempts["n"] += 1
        if attempts["n"] == 1:
            Path(rec["reply_draft"]["path"]).write_text(
                "partial from dead attempt", encoding="utf-8")
            return loop.DriveOutcome(ok=False, failure_class=loop.CLASS_INFRA,
                                     summary="killed mid-write")
        return True                      # clean retry, no draft written

    loop.run_loop(s, "beta", drive, clock=lambda: 0.0, sleep=lambda d: None,
                  max_turns=1, max_polls=4, k_poison=0, k_escalate=0)
    assert attempts["n"] >= 2
    assert _reply_inbox(s, "alpha") == []      # stale bytes never published
    assert s.cursor("beta") == q.id            # clean retry still committed


def test_wake_kind_gets_the_draft_channel(tmp_path) -> None:
    # PR #127 connector P2: wake is an ordinary driven kind whose wk- request
    # id exists so a plain message reply can correlate — sandbox-blocked
    # seats must be able to acknowledge it via the draft channel.
    s = _store(tmp_path)
    w = s.send(sender="alpha", recipient="beta", kind="wake", body="wake up",
               meta={"request_id": "wk-1"})

    def drive(rec):
        assert isinstance(rec.get("reply_draft"), dict)
        Path(rec["reply_draft"]["path"]).write_text("awake", encoding="utf-8")
        return True

    loop.run_loop(s, "beta", drive, clock=lambda: 0.0, sleep=lambda d: None,
                  max_turns=1)
    (msg,) = _reply_inbox(s, "alpha")
    assert msg.body == "awake"
    assert (msg.meta or {}).get("request_id") == "wk-1"
    assert (msg.meta or {}).get("in_reply_to") == w.id
