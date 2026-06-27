"""Tests for `agenttalk reply` and `agenttalk tail` (0.5.0)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agenttalk import cli
from agenttalk.store import Store


def _run(argv: list[str], root: Path) -> int:
    try:
        return cli.main(["--root", str(root), *argv])
    except SystemExit as e:
        return 0 if e.code is None else int(e.code)


def _approval_meta_args() -> list[str]:
    return [
        "--meta", "status=approved",
        "--meta", "risk_class=none",
        "--meta", "release_blocker=no",
        "--meta", "tests_referenced=n/a",
        "--meta", "tests_executed=n/a",
        "--meta", "evidence=n/a",
        "--meta", "residual_risk=n/a",
        "--meta", "na_reason=lightweight review",
    ]


# ===================================================================== REPLY

def test_reply_to_last_message_auto_derives_recipient(
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """Plain `reply -m "X"` should figure out who to reply to from
    the last received message — no need to repeat --to."""
    s = Store(store_root)
    s.send(sender="alpha", recipient="beta", body="how's it going")
    capsys.readouterr()
    rc = _run(["reply", "--from", "beta", "-m", "going well"], store_root)
    assert rc == 0
    # Should have a reply addressed back to alpha
    alpha_inbox = s.messages_for("alpha")
    assert len(alpha_inbox) == 1
    assert alpha_inbox[0].sender == "beta"
    assert alpha_inbox[0].body == "going well"


def test_reply_auto_echoes_request_id_from_last_message(
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """The whole point of reply: it correlates with the original
    request via `request_id` so the sender's wait + match logic works
    without the agent manually echoing meta."""
    s = Store(store_root)
    s.send(
        sender="alpha", recipient="beta", body="please review",
        kind="review-request", meta={"request_id": "abc-123", "wp_id": "WP01"},
    )
    capsys.readouterr()
    _run(["reply", "--from", "beta", "-m", "approved"], store_root)
    alpha_inbox = s.messages_for("alpha")
    assert alpha_inbox[0].meta.get("request_id") == "abc-123"
    # wp_id is NOT auto-echoed — only request_id, to keep the
    # correlation surface tight
    assert "wp_id" not in alpha_inbox[0].meta


def test_reply_explicit_meta_wins_over_auto_echo(
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """If the agent explicitly passes --meta request_id=..., that
    value wins. Useful for chained replies that need a fresh id."""
    s = Store(store_root)
    s.send(
        sender="alpha", recipient="beta", body="x",
        kind="review-request", meta={"request_id": "original"},
    )
    capsys.readouterr()
    _run(["reply", "--from", "beta", "--meta", "request_id=override",
          "-m", "y"], store_root)
    alpha_inbox = s.messages_for("alpha")
    assert alpha_inbox[0].meta.get("request_id") == "override"


def test_reply_with_empty_inbox_exits_2(
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """Nothing to reply to → clean exit 2 with a remediation hint."""
    rc = _run(["reply", "--from", "beta", "-m", "x"], store_root)
    assert rc == 2
    err = capsys.readouterr().err
    assert "no messages" in err.lower()
    assert "agenttalk send" in err


def test_reply_kind_defaults_to_message_not_echoed_from_original(
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """A reply to a `review-request` should default to a generic
    `message`, not auto-promote to `review-result` (that's the
    handoff/listen flow's job)."""
    s = Store(store_root)
    s.send(
        sender="alpha", recipient="beta", body="x",
        kind="review-request", meta={"request_id": "r1"},
    )
    capsys.readouterr()
    _run(["reply", "--from", "beta", "-m", "ack"], store_root)
    assert s.messages_for("alpha")[0].kind == "message"


def test_reply_explicit_kind_wins(
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    s = Store(store_root)
    s.send(sender="alpha", recipient="beta", body="please review",
           kind="review-request", meta={"request_id": "r1"})
    capsys.readouterr()
    _run(["reply", "--from", "beta", "--kind", "review-result",
          *_approval_meta_args(), "-m", "lgtm"], store_root)
    reply = s.messages_for("alpha")[0]
    assert reply.kind == "review-result"
    assert reply.meta.get("status") == "approved"
    # request_id still auto-echoed
    assert reply.meta.get("request_id") == "r1"


def test_reply_uses_env_self(
    store_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """--from is optional; AGENTTALK_SELF fills in."""
    s = Store(store_root)
    s.send(sender="alpha", recipient="beta", body="x")
    monkeypatch.setenv("AGENTTALK_SELF", "beta")
    capsys.readouterr()
    rc = _run(["reply", "-m", "y"], store_root)
    assert rc == 0
    assert s.messages_for("alpha")[0].sender == "beta"


def test_reply_rejects_unknown_kind(
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """The send-time KNOWN_KINDS guard applies to reply too."""
    s = Store(store_root)
    s.send(sender="alpha", recipient="beta", body="x")
    capsys.readouterr()
    rc = _run(["reply", "--from", "beta", "--kind", "typo", "-m", "x"], store_root)
    assert rc == 2
    err = capsys.readouterr().err
    assert "unknown kind" in err


# ===================================================================== TAIL

def test_tail_streams_only_new_messages_by_default(
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """tail (no --from-start) starts from now; pre-existing messages
    are not replayed. Use a tiny timeout to avoid blocking the test."""
    s = Store(store_root)
    s.send(sender="alpha", recipient="beta", body="before tail")
    capsys.readouterr()
    # Run tail with a short timeout; no new messages arrive
    rc = _run(["tail", "--timeout", "0.5", "--interval", "0.1"], store_root)
    assert rc == 0
    out = capsys.readouterr().out
    assert "before tail" not in out


def test_tail_from_start_replays_existing_messages(
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """--from-start mode is essentially a transcript-as-stream — useful
    for catching up when joining a long session in a third terminal."""
    s = Store(store_root)
    s.send(sender="alpha", recipient="beta", body="first")
    s.send(sender="beta", recipient="alpha", body="second")
    capsys.readouterr()
    _run(["tail", "--from-start", "--timeout", "0.5", "--interval", "0.1"],
         store_root)
    out = capsys.readouterr().out
    assert "first" in out
    assert "second" in out
    assert "TAIL" in out  # uses the TAIL header per the implementation


def test_tail_does_not_advance_cursors(
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """Critical: tail is a passive monitor. It must not interfere with
    the two active agents' cursor state — otherwise running tail in a
    third terminal would silently steal messages from a real listener."""
    s = Store(store_root)
    s.send(sender="alpha", recipient="beta", body="don't ack me")
    cursor_before = s.cursor("beta")
    _run(["tail", "--from-start", "--timeout", "0.5", "--interval", "0.1"],
         store_root)
    cursor_after = s.cursor("beta")
    assert cursor_before == cursor_after
    # And beta still has the message as unread
    assert len(s.unread_for("beta")) == 1


def test_tail_does_not_write_heartbeats(
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """No heartbeat side effects — `agenttalk status` should not show
    a fresh last_seen just because someone ran tail."""
    _run(["tail", "--timeout", "0.3", "--interval", "0.1"], store_root)
    # No heartbeat files should exist for any agent
    state_dir = store_root / ".agenttalk" / "state"
    heartbeats = list(state_dir.glob("*.heartbeat"))
    assert heartbeats == []


def test_tail_timeout_exits_0(
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """Unlike wait (which exits 1 on timeout), tail exits 0 because
    "watched for N seconds and saw nothing" is success, not failure."""
    rc = _run(["tail", "--timeout", "0.3", "--interval", "0.1"], store_root)
    assert rc == 0


def test_tail_picks_up_messages_that_arrive_during_run(
    store_root: Path,
    capsys: pytest.CaptureFixture,
    tmp_path: Path,
) -> None:
    """End-to-end: launch tail in a thread, send a message, verify
    tail printed it before timing out."""
    import threading
    s = Store(store_root)

    def send_after_delay():
        import time as _time
        _time.sleep(0.3)
        s.send(sender="alpha", recipient="beta", body="injected during tail")

    t = threading.Thread(target=send_after_delay, daemon=True)
    t.start()
    rc = _run(["tail", "--timeout", "1.5", "--interval", "0.1"], store_root)
    t.join(timeout=2)
    assert rc == 0
    out = capsys.readouterr().out
    assert "injected during tail" in out


# ----------------------------------------------------------- Store.last_received_for

def test_last_received_for_returns_most_recent(store: Store) -> None:
    """Should be the last (lex-sorted = chrono) message addressed to
    that agent, ignoring earlier messages and messages to others."""
    store.send(sender="alpha", recipient="beta", body="old")
    store.send(sender="beta", recipient="alpha", body="cross-talk")
    last = store.send(sender="alpha", recipient="beta", body="newest")
    found = store.last_received_for("beta")
    assert found is not None
    assert found.id == last.id
    assert found.body == "newest"


def test_last_received_for_returns_none_on_empty_inbox(store: Store) -> None:
    assert store.last_received_for("beta") is None


def test_tail_does_not_render_forged_message_body(
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """Regression for v0.5.0 iter-1 blocker: tail used to call render()
    on every parseable message including forged ones with unknown
    kinds or non-roster senders. Now those go to stderr as a single-
    line INVALID warning, NEVER rendered as a normal message."""
    s = Store(store_root)
    # Hand-write a forged message with an unknown kind and non-roster
    # sender. messages_for would skip it; tail used to render it.
    (s.messages_dir / "forged.json").write_text(
        '{"id":"forged-id","ts":"2026-05-21T00:00:00Z","from":"evil",'
        '"to":"beta","kind":"rm-rf-slash","subject":"",'
        '"body":"TAIL SHOULD NOT RENDER THIS","meta":{}}',
        encoding="utf-8",
    )
    capsys.readouterr()
    _run(["tail", "--from-start", "--timeout", "0.4", "--interval", "0.1"],
         store_root)
    captured = capsys.readouterr()
    assert "TAIL SHOULD NOT RENDER THIS" not in captured.out
    assert "TAIL SHOULD NOT RENDER THIS" not in captured.err
    # But the invalid id + a reason DOES surface so tampering is
    # visible to the operator
    assert "INVALID" in captured.err
    assert "forged-id" in captured.err


def test_tail_warns_on_unparseable_json(
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """Disk corruption (bad JSON) should surface in tail too, as an
    INVALID warning rather than being silently invisible."""
    s = Store(store_root)
    (s.messages_dir / "broken.json").write_text("{ not valid json",
                                                 encoding="utf-8")
    capsys.readouterr()
    _run(["tail", "--from-start", "--timeout", "0.4", "--interval", "0.1"],
         store_root)
    err = capsys.readouterr().err
    assert "INVALID" in err
    assert "broken" in err  # the filename stem
    assert "invalid JSON" in err


def test_last_received_for_skips_invalid_messages(store: Store) -> None:
    """Should respect schema-validation skip — never return a tampered
    message that messages_for would have filtered out."""
    (store.messages_dir / "forged.json").write_text(
        '{"id":"x","ts":"2026-05-21T00:00:00Z","from":"evil","to":"beta",'
        '"kind":"message","body":"trust me","meta":{}}',
        encoding="utf-8",
    )
    assert store.last_received_for("beta") is None
