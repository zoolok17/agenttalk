"""Tests for the 0.12.0 coordination-recovery features: per-agent
threadstate, scoped (non-consuming) wait, explicit ack --to-request
closure, and the `sync` rejoin digest."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agenttalk import cli
from agenttalk.store import Store


def _run(argv: list[str], root: Path) -> int:
    return cli.main(["--root", str(root), *argv])


def _run_expect_exit(argv: list[str], root: Path, code: int) -> None:
    try:
        rc = cli.main(["--root", str(root), *argv])
    except SystemExit as e:
        rc = 0 if e.code is None else int(e.code)
    assert int(rc) == code


# --------------------------------------------------------- threadstate (store)

def test_threadstate_seen_is_monotonic(store: Store) -> None:
    store.mark_thread_seen("alpha", "r1", "002")
    assert store.thread_seen("alpha", "r1") == "002"
    store.mark_thread_seen("alpha", "r1", "001")  # older — ignored
    assert store.thread_seen("alpha", "r1") == "002"
    assert store.thread_closed("alpha", "r1") is False


def test_close_thread_sets_closed_and_seen(store: Store) -> None:
    store.close_thread("alpha", "r1", seen_msg_id="005", reason="manual")
    assert store.thread_closed("alpha", "r1") is True
    assert store.thread_seen("alpha", "r1") == "005"
    entry = store.read_threadstate("alpha")["r1"]
    assert entry["closed_reason"] == "manual" and "closed_at" in entry


def test_closed_rids_requires_strict_boolean(store: Store) -> None:
    """A malformed non-boolean `closed` must NOT count as closed (hardening)."""
    from agenttalk.cli import _closed_rids
    store._write_threadstate("alpha", {
        "r1": {"seen_msg_id": "x", "closed": "true"},  # string — not closed
        "r2": {"closed": True},                          # real closure
    })
    assert _closed_rids(store, "alpha") == {"r2"}


def test_read_threadstate_missing_or_corrupt_is_empty(store: Store, store_root: Path) -> None:
    assert store.read_threadstate("alpha") == {}
    p = store_root / ".agenttalk" / "state" / "alpha.threadstate.json"
    p.write_text("{ not json", encoding="utf-8")
    assert store.read_threadstate("alpha") == {}  # never raises


# ------------------------------------------------------ scoped wait (CLI)

def test_scoped_wait_is_non_consuming(store: Store, store_root: Path) -> None:
    store.send(sender="beta", recipient="alpha", kind="review-request",
               body="review", meta={"request_id": "r1"})
    store.send(sender="beta", recipient="alpha", kind="note", body="unrelated")
    assert store.cursor("alpha") == ""
    # match already exists → returns 0 immediately
    rc = _run(["wait", "--for", "alpha", "--to-request", "r1", "--timeout", "5", "--quiet"], store_root)
    assert rc == 0
    # global cursor UNMOVED; both messages still unread
    assert store.cursor("alpha") == ""
    assert len(store.unread_for("alpha")) == 2
    # only the per-thread pointer advanced
    r1_id = [m.id for m in store.messages_for("alpha") if m.meta.get("request_id") == "r1"][0]
    assert store.thread_seen("alpha", "r1") == r1_id
    assert store.thread_closed("alpha", "r1") is False  # seen != handled


def test_scoped_wait_kind_filter(store: Store, store_root: Path) -> None:
    store.send(sender="beta", recipient="alpha", kind="note",
               body="note on r1", meta={"request_id": "r1"})
    # no review-result on r1 yet → scoped wait restricted to that kind times out
    rc = _run(["wait", "--for", "alpha", "--to-request", "r1", "--kind", "review-result",
               "--timeout", "0.3", "--grace", "0", "--quiet"], store_root)
    assert rc == 1


def test_scoped_wait_kind_requires_to_request(store_root: Path) -> None:
    _run_expect_exit(["wait", "--for", "alpha", "--kind", "note", "--timeout", "1"], store_root, 2)


def test_scoped_wait_ignores_stale_composing(store: Store, store_root: Path) -> None:
    """Regression (Codex review): a STALE composing ping (present before the
    wait, and never consumed because scoped wait is non-consuming) must NOT
    extend a later scoped wait. With the bug it would extend by
    --composing-extend each time → the wait runs far past its timeout."""
    import time as _t
    store.send(sender="beta", recipient="alpha", kind="composing", body="drafting")
    t0 = _t.monotonic()
    rc = _run(["wait", "--for", "alpha", "--to-request", "r1", "--timeout", "0.1",
               "--grace", "0", "--interval", "0.05", "--composing-extend", "5",
               "--quiet"], store_root)
    elapsed = _t.monotonic() - t0
    assert rc == 1
    assert elapsed < 2.0, f"stale composing extended the scoped wait ({elapsed:.1f}s)"


# ------------------------------------------------ ack --to-request (closure)

def test_ack_to_request_closes_thread(store: Store, store_root: Path) -> None:
    store.send(sender="beta", recipient="alpha", kind="review-request",
               body="review", meta={"request_id": "r1"})
    # alpha owes a review-result → owed-inbound until explicitly closed
    _run(["ack", "--for", "alpha", "--to-request", "r1"], store_root)
    assert store.thread_closed("alpha", "r1") is True
    out_rc = _run(["threads", "--for", "alpha", "--json"], store_root)
    assert out_rc == 0


def test_ack_to_request_makes_threads_report_closed(
    store: Store, store_root: Path, capsys: pytest.CaptureFixture,
) -> None:
    store.send(sender="beta", recipient="alpha", kind="review-request",
               body="review", meta={"request_id": "r1"})
    store.close_thread("alpha", "r1", seen_msg_id=None, reason="manual")
    capsys.readouterr()
    _run(["threads", "--for", "alpha", "--all", "--json"], store_root)
    payload = json.loads(capsys.readouterr().out)
    assert payload["counts"]["closed"] == 1
    assert payload["counts"]["owed-inbound"] == 0


# ----------------------------------------------------------- sync digest

def test_sync_separates_owed_work_from_fyi(
    store: Store, store_root: Path, capsys: pytest.CaptureFixture,
) -> None:
    store.send(sender="beta", recipient="alpha", kind="review-request",
               body="please review", subject="WP1", meta={"request_id": "r1"})
    store.send(sender="beta", recipient="alpha", kind="note", body="just an fyi")
    capsys.readouterr()
    _run(["sync", "--for", "alpha", "--json"], store_root)
    payload = json.loads(capsys.readouterr().out)
    assert payload["agent"] == "alpha"
    assert payload["counts"]["owed-inbound"] == 1
    # the owed thread is actionable, with owe=you and a reply hint
    t = payload["threads"][0]
    assert t["request_id"] == "r1" and t["owe"] == "you"
    assert "reply --to-request r1" in t["hint"]
    # the note is FYI, kept OUT of owed work
    fyi_kinds = [f["kind"] for f in payload["unread_fyi"]]
    assert "note" in fyi_kinds
    assert all(f.get("kind") != "review-request" for f in payload["unread_fyi"])


def test_sync_reply_waiting_hints_read_not_reply(
    store: Store, store_root: Path, capsys: pytest.CaptureFixture,
) -> None:
    """A reply-waiting thread (a reply landed, unread) should tell the agent
    to READ it (drain), not fire off another message."""
    store.send(sender="alpha", recipient="beta", kind="review-request",
               body="review", meta={"request_id": "r1"})
    store.send(sender="beta", recipient="alpha", kind="review-result",
               body="approved", meta={"request_id": "r1", "status": "approved"})
    # alpha has NOT consumed the verdict → reply-waiting
    capsys.readouterr()
    _run(["sync", "--for", "alpha", "--json"], store_root)
    payload = json.loads(capsys.readouterr().out)
    t = payload["threads"][0]
    assert t["state"] == "reply-waiting"
    assert t["owe"] == "read"
    assert "drain" in t["hint"] and "reply --to-request" not in t["hint"]


def test_sync_caught_up_has_no_actionable(store: Store, store_root: Path,
                                          capsys: pytest.CaptureFixture) -> None:
    capsys.readouterr()
    _run(["sync", "--for", "alpha", "--json"], store_root)
    payload = json.loads(capsys.readouterr().out)
    assert payload["threads"] == []
    assert payload["counts"]["owed-inbound"] == 0
