"""Tests for CLI subcommands via the parser, exercised through main().

Most CLI logic is identity resolution + roster validation. We invoke
`main(argv)` rather than subprocess-ing to keep tests fast and to
capture stderr/stdout via capsys.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from agenttalk import cli
from agenttalk.store import Store


# Helper: run main() under a fixed root so we don't depend on cwd
def _run(argv: list[str], root: Path) -> int:
    return cli.main(["--root", str(root), *argv])


def _run_expect_exit(argv: list[str], root: Path, code: int) -> None:
    """Run main() and assert it exits with the given code.

    Handles both error paths: `sys.exit(N)` raises SystemExit, while
    `raise ValueError(...)` is caught in main() and returned as an
    integer. Either way, the wrapper script (`sys.exit(main())`) ends
    up with the same OS-level exit code, so the test treats them
    equivalently.
    """
    try:
        rc = cli.main(["--root", str(root), *argv])
    except SystemExit as e:
        actual = 0 if e.code is None else int(e.code)
    else:
        actual = int(rc)
    assert actual == code, f"expected exit code {code}, got {actual}"


# ----------------------------------------------------- init: hint emission

def test_init_prints_concrete_env_hint_for_two_agents(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    rc = cli.main(["init", "--path", str(tmp_path), "--agents", "claude,codex"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "AGENTTALK_SELF='claude'" in out
    assert "AGENTTALK_PEER='codex'" in out
    assert "Terminal A" in out
    assert "Terminal B" in out


def test_init_uses_generic_hint_for_three_agents(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    rc = cli.main(["init", "--path", str(tmp_path), "--agents", "a,b,c"])
    assert rc == 0
    out = capsys.readouterr().out
    # Generic guidance, no Terminal A/B with specific names
    assert "<name>" in out
    assert "Terminal A" not in out


# ------------------------------------------- identity resolution: explicit

def test_send_with_explicit_from_and_to(store_root: Path, capsys: pytest.CaptureFixture) -> None:
    rc = _run(["send", "--from", "alpha", "--to", "beta", "-m", "hi"], store_root)
    assert rc == 0
    msgs = list((store_root / ".agenttalk" / "messages").glob("*.json"))
    assert len(msgs) == 1
    data = json.loads(msgs[0].read_text(encoding="utf-8"))
    assert data["from"] == "alpha"
    assert data["to"] == "beta"


# --------------------------------------------- identity resolution: env

def test_send_picks_up_env_self_and_peer(
    store_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTTALK_SELF", "beta")
    monkeypatch.setenv("AGENTTALK_PEER", "alpha")
    rc = _run(["send", "-m", "via env"], store_root)
    assert rc == 0
    msgs = list((store_root / ".agenttalk" / "messages").glob("*.json"))
    data = json.loads(msgs[0].read_text(encoding="utf-8"))
    assert data["from"] == "beta"
    assert data["to"] == "alpha"


def test_send_auto_peer_in_two_agent_roster(
    store_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTTALK_SELF", "alpha")
    rc = _run(["send", "-m", "auto-peered"], store_root)
    assert rc == 0
    msgs = list((store_root / ".agenttalk" / "messages").glob("*.json"))
    data = json.loads(msgs[0].read_text(encoding="utf-8"))
    assert data["from"] == "alpha"
    assert data["to"] == "beta"


# ----------------------------------------------- resolution: failure modes

def test_send_exits_2_when_no_self_anywhere(
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    _run_expect_exit(["send", "-m", "no self"], store_root, 2)
    err = capsys.readouterr().err
    assert "AGENTTALK_SELF" in err


def test_send_exits_2_when_self_not_in_roster(
    store_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    monkeypatch.setenv("AGENTTALK_SELF", "typo")
    _run_expect_exit(["send", "-m", "typo"], store_root, 2)
    err = capsys.readouterr().err
    assert "'typo' is not in the project roster" in err


def test_send_rejects_self_mail(
    store_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    monkeypatch.setenv("AGENTTALK_SELF", "alpha")
    monkeypatch.setenv("AGENTTALK_PEER", "alpha")
    _run_expect_exit(["send", "-m", "self mail"], store_root, 2)
    err = capsys.readouterr().err
    assert "self-message" in err or "same as self" in err


def test_send_exits_2_when_peer_ambiguous_in_3_agent_roster(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    Store(tmp_path).init(["a", "b", "c"])
    monkeypatch.setenv("AGENTTALK_SELF", "a")
    _run_expect_exit(["send", "-m", "no peer"], tmp_path, 2)
    err = capsys.readouterr().err
    assert "AGENTTALK_PEER" in err


def test_wait_exits_2_before_loop_on_unknown_self(
    store_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """Regression for Codex blocker: unknown self must exit 2 before the
    poll loop and must NOT write a phantom heartbeat file."""
    monkeypatch.setenv("AGENTTALK_SELF", "typo")
    _run_expect_exit(
        ["wait", "--timeout", "0.3", "--heartbeat-interval", "0.1", "--quiet"],
        store_root,
        2,
    )
    assert not (store_root / ".agenttalk" / "state" / "typo.heartbeat").exists()


def test_invalid_meta_exits_2_not_1(
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """Regression: --meta without `=` used to raise SystemExit(str),
    which exits 1 — that collides with `agenttalk wait`'s timeout
    signal and would confuse the sk-loop. Must exit 2 instead.
    """
    _run_expect_exit(
        ["send", "--from", "alpha", "--to", "beta", "-m", "x", "--meta", "bad_no_equals"],
        store_root,
        2,
    )
    err = capsys.readouterr().err
    assert "--meta expects key=value" in err


def test_init_rejects_path_traversal_in_agent_name(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """Regression for the v0.2.0 review blocker: path-like agent names
    must not be allowed to escape .agenttalk/state/ during init."""
    _run_expect_exit(
        ["init", "--path", str(tmp_path), "--agents", "alpha,..\\..\\outside"],
        tmp_path,
        2,
    )
    err = capsys.readouterr().err
    assert "not a safe identifier" in err
    # And no escaped file was created
    assert not (tmp_path.parent / "outside.cursor").exists()
    assert not (tmp_path / "outside.cursor").exists()


def test_init_rejects_duplicate_agents(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    _run_expect_exit(
        ["init", "--path", str(tmp_path), "--agents", "alpha,alpha"],
        tmp_path,
        2,
    )
    err = capsys.readouterr().err
    assert "more than once" in err


def test_unsafe_env_self_exits_2(
    store_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """A malicious AGENTTALK_SELF env var (e.g. ../outside) must be
    rejected with exit 2 before any filesystem interpolation, even if
    it would also fail the roster check later."""
    monkeypatch.setenv("AGENTTALK_SELF", "../outside")
    _run_expect_exit(["recv"], store_root, 2)
    err = capsys.readouterr().err
    assert "not a safe identifier" in err


# ----------------------------------------------------- recv / status flow

def test_recv_then_status_shows_unread_count(
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    _run(["send", "--from", "alpha", "--to", "beta", "-m", "one"], store_root)
    _run(["send", "--from", "alpha", "--to", "beta", "-m", "two"], store_root)
    capsys.readouterr()  # discard
    _run(["status"], store_root)
    out = capsys.readouterr().out
    assert "unread=2" in out


def test_recv_ack_advances_cursor(
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    _run(["send", "--from", "alpha", "--to", "beta", "-m", "one"], store_root)
    rc = _run(["recv", "--for", "beta", "--ack"], store_root)
    assert rc == 0
    capsys.readouterr()
    _run(["status"], store_root)
    out = capsys.readouterr().out
    assert "unread=0" in out


# ------------------------------------------------------ status --json output

def test_status_json_schema(
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """status --json must emit a stable contract that the consult skill
    and any other automation can parse without regex-ing the human text."""
    import json as _json
    _run(["send", "--from", "alpha", "--to", "beta", "-m", "hello"], store_root)
    capsys.readouterr()
    rc = _run(["status", "--json"], store_root)
    assert rc == 0
    payload = _json.loads(capsys.readouterr().out)
    # Required top-level keys
    assert payload["root"] == str(store_root)
    assert payload["session_id"]
    assert payload["message_count"] == 1
    assert payload["stale_threshold_seconds"] == 60.0
    # Per-agent shape
    names = {a["name"] for a in payload["agents"]}
    assert names == {"alpha", "beta"}
    for a in payload["agents"]:
        # heartbeat / last_seen / stale tri-null when no wait has run
        assert a["heartbeat"] is None
        assert a["last_seen_seconds"] is None
        assert a["stale"] is None
        # cursor is None until something ack'd
        assert a["cursor"] is None
    # beta got the message, so beta is unread=1
    beta = next(a for a in payload["agents"] if a["name"] == "beta")
    assert beta["unread"] == 1


def test_status_json_includes_heartbeat_when_set(
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """If an agent has a heartbeat on disk, status --json must surface
    its ISO timestamp + computed age + stale flag."""
    import json as _json
    from agenttalk.store import Store
    s = Store(store_root)
    s.write_heartbeat("alpha")
    capsys.readouterr()
    _run(["status", "--json"], store_root)
    payload = _json.loads(capsys.readouterr().out)
    alpha = next(a for a in payload["agents"] if a["name"] == "alpha")
    assert alpha["heartbeat"] is not None
    assert alpha["heartbeat"].endswith("Z")
    assert isinstance(alpha["last_seen_seconds"], float)
    assert alpha["last_seen_seconds"] < 5
    assert alpha["stale"] is False


def test_status_human_output_unchanged_for_no_heartbeat(
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """Backwards-compat: bare `status` (no --json) still emits the
    same human format we had before."""
    _run(["status"], store_root)
    out = capsys.readouterr().out
    assert "root:" in out
    assert "session_id:" in out
    assert "agents:" in out
    assert "(no heartbeat)" in out


# ---------------------------------------------------------- cmd_end / transcript

def test_end_sends_kind_end_to_peers_and_exports_transcript(
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """`agenttalk end` must (a) send kind=end to every other agent so
    their listen loops exit, and (b) export a markdown transcript."""
    from agenttalk.store import Store
    _run(["send", "--from", "alpha", "--to", "beta", "-m", "hi"], store_root)
    capsys.readouterr()
    rc = _run(["end", "--from", "alpha", "--reason", "wrapping up"], store_root)
    assert rc == 0
    out = capsys.readouterr().out
    assert "transcript at" in out
    # The peer agent should have an unread kind=end message
    s = Store(store_root)
    end_msgs = [m for m in s.messages_for("beta") if m.kind == "end"]
    assert len(end_msgs) == 1
    assert end_msgs[0].body == "wrapping up"
    # And a transcript file was written under sessions/
    transcripts = list((store_root / ".agenttalk" / "sessions").glob("transcript-*.md"))
    assert len(transcripts) == 1


def test_end_with_no_reason_uses_default_body(
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    from agenttalk.store import Store
    _run(["end", "--from", "alpha"], store_root)
    s = Store(store_root)
    end_msgs = [m for m in s.messages_for("beta") if m.kind == "end"]
    assert end_msgs[0].body == "session ended"


def test_transcript_subcommand_writes_markdown_by_default(
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    _run(["send", "--from", "alpha", "--to", "beta", "-m", "one"], store_root)
    capsys.readouterr()
    rc = _run(["transcript"], store_root)
    assert rc == 0
    path = Path(capsys.readouterr().out.strip())
    assert path.exists()
    assert path.suffix == ".md"
    assert "alpha → beta" in path.read_text(encoding="utf-8")


def test_transcript_jsonl_format(
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    import json as _json
    _run(["send", "--from", "alpha", "--to", "beta", "-m", "one"], store_root)
    capsys.readouterr()
    _run(["transcript", "--format", "jsonl"], store_root)
    path = Path(capsys.readouterr().out.strip())
    assert path.suffix == ".jsonl"
    lines = [ln for ln in path.read_text(encoding="utf-8").split("\n") if ln]
    assert len(lines) == 1
    assert _json.loads(lines[0])["body"] == "one"


# ------------------------------------------------------------- cmd_wait

def test_wait_returns_0_when_message_already_present(
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """If a message is already waiting, `wait` returns 0 immediately."""
    _run(["send", "--from", "alpha", "--to", "beta", "-m", "queued"], store_root)
    capsys.readouterr()
    rc = _run(["wait", "--for", "beta", "--timeout", "1",
               "--heartbeat-interval", "0"], store_root)
    assert rc == 0
    out = capsys.readouterr().out
    assert "RECEIVED" in out
    assert "queued" in out


def test_wait_returns_1_on_timeout(
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """Empty queue + short timeout = exit 1 (the reserved timeout
    signal). Critical so loop skills can distinguish it from errors."""
    rc = _run(["wait", "--for", "beta", "--timeout", "0.5",
               "--heartbeat-interval", "0", "--quiet"], store_root)
    assert rc == 1


def test_wait_writes_heartbeat_at_configured_interval(
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """The wait subprocess stamps `.agenttalk/state/<agent>.heartbeat`
    so peers can see who's actively listening."""
    rc = _run(["wait", "--for", "beta", "--timeout", "0.5",
               "--heartbeat-interval", "0.1", "--quiet"], store_root)
    assert rc == 1
    hb = store_root / ".agenttalk" / "state" / "beta.heartbeat"
    assert hb.exists()
    content = hb.read_text(encoding="utf-8").strip()
    assert content.endswith("Z")  # ISO 8601 UTC


def test_wait_advances_cursor_on_received_message(
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """Default --ack advances the cursor so the same message is
    not re-delivered on the next wait."""
    from agenttalk.store import Store
    _run(["send", "--from", "alpha", "--to", "beta", "-m", "first"], store_root)
    capsys.readouterr()
    _run(["wait", "--for", "beta", "--timeout", "1",
          "--heartbeat-interval", "0"], store_root)
    s = Store(store_root)
    assert s.cursor("beta") != ""
    # Second wait with no new messages should time out
    capsys.readouterr()
    rc = _run(["wait", "--for", "beta", "--timeout", "0.3",
               "--heartbeat-interval", "0", "--quiet"], store_root)
    assert rc == 1


def test_wait_no_ack_keeps_message_unread(
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """`--no-ack` returns the message but leaves the cursor; useful
    for browsing / re-handling."""
    from agenttalk.store import Store
    _run(["send", "--from", "alpha", "--to", "beta", "-m", "browse"], store_root)
    capsys.readouterr()
    _run(["wait", "--for", "beta", "--timeout", "1",
          "--heartbeat-interval", "0", "--no-ack"], store_root)
    s = Store(store_root)
    assert s.cursor("beta") == ""
    assert len(s.unread_for("beta")) == 1


# ----------------------------------------- cmd_wait: composing + post-timeout grace

def test_wait_post_timeout_grace_returns_late_message(
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """A real reply landing during the post-timeout grace window is still
    delivered with exit 0, not lost to exit 1. Regression for the
    "reply landed 12s after wait timed out" report that motivated 0.8.0."""
    import threading
    from agenttalk.store import Store

    s = Store(store_root)

    def _inject_after_deadline() -> None:
        # 1s timeout, message lands at ~1.3s, grace window is 2s wide
        # so the post-timeout scan catches it.
        time.sleep(1.3)
        s.send(sender="alpha", recipient="beta",
               body="just barely in time", kind="message")

    t = threading.Thread(target=_inject_after_deadline, daemon=True)
    t.start()
    try:
        rc = _run(["wait", "--for", "beta", "--timeout", "1",
                   "--grace", "2",
                   "--heartbeat-interval", "0"], store_root)
        assert rc == 0
        out = capsys.readouterr().out
        assert "RECEIVED" in out
        assert "just barely in time" in out
    finally:
        t.join(timeout=5)


def test_wait_grace_zero_returns_immediately_on_deadline(
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """--grace 0 reproduces the pre-0.8.0 hard-edge behavior: deadline
    fires, wait exits 1 with no post-scan."""
    rc = _run(["wait", "--for", "beta", "--timeout", "0.3",
               "--grace", "0",
               "--heartbeat-interval", "0", "--quiet"], store_root)
    assert rc == 1


def test_wait_composing_extends_deadline_and_returns_real_reply(
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """A `composing` ping resets the waiter's clock without being
    returned as a reply; the subsequent real message is what `wait`
    surfaces."""
    import threading
    from agenttalk.store import Store

    s = Store(store_root)

    def _inject() -> None:
        # composing arrives at 0.4s (extends 0.5s deadline by another 2s)
        time.sleep(0.4)
        s.send(sender="alpha", recipient="beta",
               body="hold on", kind="composing")
        # real reply arrives at 1.2s — would have timed out at 0.5s + grace
        # without the composing extension.
        time.sleep(0.8)
        s.send(sender="alpha", recipient="beta",
               body="here's the real answer", kind="message")

    t = threading.Thread(target=_inject, daemon=True)
    t.start()
    try:
        rc = _run(["wait", "--for", "beta", "--timeout", "0.5",
                   "--grace", "0",
                   "--composing-extend", "2",
                   "--heartbeat-interval", "0"], store_root)
        assert rc == 0
        out = capsys.readouterr().out
        # The composing log line + the real reply, NOT the composing body
        # as a "received" payload.
        assert "composing from alpha" in out
        assert "RECEIVED" in out
        assert "here's the real answer" in out
        assert "hold on" not in out  # composing body never surfaced
    finally:
        t.join(timeout=5)


def test_wait_composing_extension_disabled_with_zero(
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """--composing-extend 0 means composing pings are still consumed
    (won't surface as replies) but don't extend the deadline."""
    import threading
    from agenttalk.store import Store

    s = Store(store_root)

    def _inject() -> None:
        time.sleep(0.2)
        s.send(sender="alpha", recipient="beta",
               body="hold on", kind="composing")

    t = threading.Thread(target=_inject, daemon=True)
    t.start()
    try:
        rc = _run(["wait", "--for", "beta", "--timeout", "0.5",
                   "--grace", "0",
                   "--composing-extend", "0",
                   "--heartbeat-interval", "0", "--quiet"], store_root)
        assert rc == 1  # timed out — no extension, no real reply
    finally:
        t.join(timeout=5)


def test_wait_duplicate_composing_counted_only_once(
    store: Store,
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """The same composing message id must extend the deadline only on
    its first appearance — the wait loop re-scans the inbox on every
    poll iteration, so we must dedupe by id."""
    # Pre-write a composing so the very first scan sees it.
    store.send(sender="alpha", recipient="beta",
               body="hold on", kind="composing")
    capsys.readouterr()
    # Deadline 0.5s + one extension of 0.5s = 1.0s effective.
    # If the same composing extended on every poll iteration (every 0.1s),
    # the wait would never time out. With dedup, it times out around 1.0s.
    started = time.time()
    rc = _run(["wait", "--for", "beta", "--timeout", "0.5",
               "--grace", "0",
               "--composing-extend", "0.5",
               "--interval", "0.1",
               "--heartbeat-interval", "0"], store_root)
    elapsed = time.time() - started
    assert rc == 1
    # Should land between ~1.0s (one extension) and ~2.5s (slack for CI).
    # Critically, NOT > 5s (which would indicate runaway extension).
    assert elapsed < 4.0, f"wait extended runaway: {elapsed:.2f}s"


def test_wait_consumed_composing_does_not_extend_subsequent_wait(
    store: Store,
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """A composing ping consumed in one wait must not survive to extend
    the NEXT wait — the on-disk cursor advances past it under --ack
    (the default). Regression for Codex's iter-1 BLOCKER #2:
    "single stale composing makes every later wait pay the extension
    again, contradicting the recv --ack rationale that stale control
    pings should not pin the cursor."
    """
    # One stale composing in the inbox.
    store.send(sender="alpha", recipient="beta",
               body="hold on", kind="composing")
    capsys.readouterr()
    # First wait — short timeout, short extension. The composing gets
    # consumed for extension AND the cursor advances past it.
    rc1 = _run(["wait", "--for", "beta", "--timeout", "0.2",
                "--grace", "0",
                "--composing-extend", "0.5",
                "--heartbeat-interval", "0", "--quiet"], store_root)
    assert rc1 == 1
    assert Store(store_root).cursor("beta") != ""
    # Second wait — same stale composing, but cursor is now past it.
    # If the bug existed, this would also extend by ~0.5s; the assertion
    # is "elapsed near the raw 0.2s timeout, not near 0.7s".
    started = time.time()
    rc2 = _run(["wait", "--for", "beta", "--timeout", "0.2",
                "--grace", "0",
                "--composing-extend", "0.5",
                "--heartbeat-interval", "0", "--quiet"], store_root)
    elapsed = time.time() - started
    assert rc2 == 1
    assert elapsed < 0.5, (
        f"second wait re-extended on a stale composing: {elapsed:.2f}s "
        f"(expected ~0.2s)"
    )


def test_wait_no_ack_leaves_consumed_composings_in_place(
    store: Store,
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """`--no-ack` documents that the user wants the cursor untouched.
    Under --no-ack, a consumed composing is intentionally NOT
    cursor-advanced — symmetric with how --no-ack treats real
    messages. This pins the tradeoff as a deliberate choice."""
    store.send(sender="alpha", recipient="beta",
               body="hold on", kind="composing")
    capsys.readouterr()
    rc = _run(["wait", "--for", "beta", "--timeout", "0.2",
               "--grace", "0",
               "--composing-extend", "0.5",
               "--heartbeat-interval", "0", "--no-ack", "--quiet"], store_root)
    assert rc == 1
    assert Store(store_root).cursor("beta") == ""


# ------------------------------------------------------------- cmd_composing

def test_composing_subcommand_writes_composing_kind(
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    from agenttalk.store import Store
    rc = _run(["composing", "--from", "alpha", "--to", "beta",
               "-m", "still drafting"], store_root)
    assert rc == 0
    msgs = Store(store_root).all_messages()
    assert len(msgs) == 1
    assert msgs[0].kind == "composing"
    assert msgs[0].sender == "alpha"
    assert msgs[0].recipient == "beta"
    assert msgs[0].body == "still drafting"


def test_composing_subcommand_default_body(
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    rc = _run(["composing", "--from", "alpha", "--to", "beta"], store_root)
    assert rc == 0
    msgs = Store(store_root).all_messages()
    assert msgs[0].body.startswith("still drafting")


# ------------------------------------------------------------- cmd_recv: control filter

def test_recv_hides_composing_by_default(
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """composing kind is wait-only flow control; recv should not surface
    it (or count it as a "new message") by default."""
    from agenttalk.store import Store
    s = Store(store_root)
    s.send(sender="alpha", recipient="beta",
           body="still drafting", kind="composing")
    capsys.readouterr()
    rc = _run(["recv", "--for", "beta"], store_root)
    assert rc == 0
    out = capsys.readouterr().out
    assert "no new messages" in out
    assert "still drafting" not in out


def test_recv_include_control_shows_composing(
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    from agenttalk.store import Store
    s = Store(store_root)
    s.send(sender="alpha", recipient="beta",
           body="still drafting", kind="composing")
    capsys.readouterr()
    rc = _run(["recv", "--for", "beta", "--include-control"], store_root)
    assert rc == 0
    out = capsys.readouterr().out
    assert "still drafting" in out


def test_recv_ack_advances_past_composing_even_when_hidden(
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """A stale composing ping shouldn't pin the cursor forever — --ack
    advances past it even though the visible view was empty."""
    from agenttalk.store import Store
    s = Store(store_root)
    msg = s.send(sender="alpha", recipient="beta",
                 body="ping", kind="composing")
    capsys.readouterr()
    rc = _run(["recv", "--for", "beta", "--ack"], store_root)
    assert rc == 0
    assert Store(store_root).cursor("beta") == msg.id


# ----------------------------------------------------------- agenttalk --version

def test_version_flag_prints_current_version(
    capsys: pytest.CaptureFixture,
) -> None:
    """`agenttalk --version` is part of the support contract; argparse
    raises SystemExit(0) after printing."""
    from agenttalk import __version__
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert __version__ in out
    assert "agenttalk" in out


# ============================================================ issue #5: v0.9.0
# recv footgun + drain + .waiting markers + status warnings + request_id
# ---------------------------------------------------------------------------

# ----------------------------------------------------------- drain command

def test_drain_consumes_and_advances_to_newest(
    store: Store,
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """`drain` prints all unread AND moves the cursor to the newest id —
    the single 'consume my inbox' verb issue #5 found missing."""
    _run(["send", "--from", "alpha", "--to", "beta", "-m", "one"], store_root)
    m2 = store.send(sender="alpha", recipient="beta", body="two", kind="message")
    capsys.readouterr()
    rc = _run(["drain", "--for", "beta"], store_root)
    assert rc == 0
    out = capsys.readouterr().out
    assert "one" in out and "two" in out
    assert store.cursor("beta") == m2.id
    # A second drain has nothing left to consume.
    rc = _run(["drain", "--for", "beta"], store_root)
    assert rc == 0
    assert "no new messages" in capsys.readouterr().out


def test_drain_advances_past_hidden_control_only_unread(
    store: Store,
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """When the only unread is a hidden composing ping, drain still moves
    the cursor past it (clears stale-control backlog) even though the
    visible output is empty."""
    cmp_msg = store.send(sender="alpha", recipient="beta",
                         body="hold on", kind="composing")
    capsys.readouterr()
    rc = _run(["drain", "--for", "beta"], store_root)
    assert rc == 0
    out = capsys.readouterr().out
    assert "no new messages" in out  # composing hidden from view
    assert "hold on" not in out
    assert store.cursor("beta") == cmp_msg.id  # but cursor advanced past it


def test_drain_include_control_shows_composing(
    store: Store,
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """--include-control surfaces the otherwise-hidden composing body."""
    store.send(sender="alpha", recipient="beta", body="hold on", kind="composing")
    capsys.readouterr()
    rc = _run(["drain", "--for", "beta", "--include-control"], store_root)
    assert rc == 0
    assert "hold on" in capsys.readouterr().out


def test_drain_quiet_suppresses_empty_notice_but_still_acks(
    store: Store,
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    cmp_msg = store.send(sender="alpha", recipient="beta",
                         body="hold on", kind="composing")
    capsys.readouterr()
    rc = _run(["drain", "--for", "beta", "--quiet"], store_root)
    assert rc == 0
    assert capsys.readouterr().out == ""
    assert store.cursor("beta") == cmp_msg.id


# ----------------------------------------------------------- recv hint

def test_recv_hint_fires_on_plain_peek(
    store: Store,
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """Plain `recv` (no --ack, no --since) that shows messages nudges the
    user toward the consuming verbs — and leaves the cursor untouched."""
    store.send(sender="alpha", recipient="beta", body="one", kind="message")
    capsys.readouterr()
    rc = _run(["recv", "--for", "beta"], store_root)
    assert rc == 0
    captured = capsys.readouterr()
    assert "one" in captured.out
    assert "hint:" in captured.err
    assert "drain" in captured.err
    assert store.cursor("beta") == ""  # peek did not move the cursor


def test_recv_hint_suppressed_with_ack(
    store: Store,
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    store.send(sender="alpha", recipient="beta", body="one", kind="message")
    capsys.readouterr()
    rc = _run(["recv", "--for", "beta", "--ack"], store_root)
    assert rc == 0
    assert "hint:" not in capsys.readouterr().err


def test_recv_hint_suppressed_with_since(
    store: Store,
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """Explicit --since is deliberate history inspection; don't nag."""
    store.send(sender="alpha", recipient="beta", body="one", kind="message")
    capsys.readouterr()
    rc = _run(["recv", "--for", "beta", "--since", ""], store_root)
    assert rc == 0
    assert "hint:" not in capsys.readouterr().err


def test_recv_hint_suppressed_when_quiet(
    store: Store,
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    store.send(sender="alpha", recipient="beta", body="one", kind="message")
    capsys.readouterr()
    rc = _run(["recv", "--for", "beta", "--quiet"], store_root)
    assert rc == 0
    assert "hint:" not in capsys.readouterr().err


def test_recv_hint_absent_when_nothing_visible(
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    capsys.readouterr()
    rc = _run(["recv", "--for", "beta"], store_root)
    assert rc == 0
    assert "hint:" not in capsys.readouterr().err


# ----------------------------------------------------- .waiting markers

def test_wait_writes_and_clears_waiting_marker_on_message(
    store_root: Path,
) -> None:
    """`wait` stamps .waiting while blocking and clears it once a real
    message is delivered."""
    import threading

    s = Store(store_root)
    saw_marker: dict = {}

    def _inject() -> None:
        time.sleep(0.4)
        saw_marker["mid_wait"] = s.read_waiting("beta")
        s.send(sender="alpha", recipient="beta", body="hi", kind="message")

    t = threading.Thread(target=_inject, daemon=True)
    t.start()
    try:
        rc = _run(["wait", "--for", "beta", "--timeout", "3",
                   "--heartbeat-interval", "0", "--quiet"], store_root)
        assert rc == 0
    finally:
        t.join(timeout=5)
    assert saw_marker["mid_wait"] is not None
    assert saw_marker["mid_wait"]["agent"] == "beta"
    assert "pid" in saw_marker["mid_wait"]
    assert s.read_waiting("beta") is None  # cleared on exit


def test_wait_clears_waiting_marker_on_timeout(
    store_root: Path,
) -> None:
    s = Store(store_root)
    rc = _run(["wait", "--for", "beta", "--timeout", "0.3", "--grace", "0",
               "--heartbeat-interval", "0", "--quiet"], store_root)
    assert rc == 1
    assert s.read_waiting("beta") is None


# ------------------------------------------------- status actionable warnings

def test_status_warns_never_acked_unread(
    store: Store,
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """An agent with unread but cursor=(none) is flagged as never-acked."""
    store.send(sender="alpha", recipient="beta", body="one", kind="message")
    capsys.readouterr()
    rc = _run(["status"], store_root)
    assert rc == 0
    out = capsys.readouterr().out
    assert "WARN" in out
    assert "never acked" in out
    assert "drain --for beta" in out


def test_status_json_exposes_warnings_and_waiting_keys(
    store: Store,
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """JSON gains `warnings` (top level) and `waiting`/`waiting_stale`
    (per agent) without dropping any pre-existing agent fields."""
    store.send(sender="alpha", recipient="beta", body="one", kind="message")
    capsys.readouterr()
    rc = _run(["status", "--json"], store_root)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert "warnings" in payload
    assert any("never acked" in w for w in payload["warnings"])
    for a in payload["agents"]:
        # additive only — old consumers still find these
        assert "cursor" in a and "unread" in a and "stale" in a
        assert "waiting" in a and "waiting_stale" in a
        assert a["waiting"] is None  # nobody is waiting in this test


def test_status_detects_soft_deadlock_between_two_waiters(
    store: Store,
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """Two live waiters at once = soft-deadlock; status names both and
    points at the remedy. We simulate live waits by writing fresh
    heartbeats + waiting markers directly (no real blocking)."""
    now_epoch = time.time()
    for name in ("alpha", "beta"):
        s_path = store.state_dir / f"{name}.heartbeat"
        from datetime import datetime, timezone
        s_path.write_text(
            datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            encoding="utf-8",
        )
        store.write_waiting(name, {
            "agent": name, "pid": 1234, "since": "now",
            "cursor_at_start": "", "timeout_seconds": 120.0,
            "deadline_epoch": now_epoch + 120,
        })
    capsys.readouterr()
    rc = _run(["status"], store_root)
    assert rc == 0
    out = capsys.readouterr().out
    assert "soft-deadlock" in out
    assert "alpha" in out and "beta" in out


def test_status_ignores_stale_waiting_marker_for_deadlock(
    store: Store,
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """A waiting marker whose deadline has long passed (orphan from a
    crashed shell, no heartbeat) must NOT count toward a soft-deadlock."""
    for name in ("alpha", "beta"):
        store.write_waiting(name, {
            "agent": name, "pid": 1234, "since": "old",
            "cursor_at_start": "", "timeout_seconds": 1.0,
            "deadline_epoch": time.time() - 10_000,
        })
    capsys.readouterr()
    rc = _run(["status"], store_root)
    assert rc == 0
    out = capsys.readouterr().out
    assert "soft-deadlock" not in out
    # And the per-agent line marks the marker stale.
    assert "waiting(stale)" in out


# ----------------------------------------------------- request_id correlation

def test_send_review_request_autogenerates_request_id(
    store: Store,
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """A review-request with no explicit request_id gets one minted +
    printed, so the peer's review-result has something to echo."""
    rc = _run(["send", "--from", "alpha", "--to", "beta",
               "--kind", "review-request", "-m", "please review"], store_root)
    assert rc == 0
    assert "auto request_id" in capsys.readouterr().out
    msgs = store.messages_for("beta")
    assert msgs[-1].meta.get("request_id", "").startswith("rq-")


def test_send_review_request_preserves_explicit_request_id(
    store: Store,
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    rc = _run(["send", "--from", "alpha", "--to", "beta",
               "--kind", "review-request", "--meta", "request_id=mine-123",
               "-m", "please review"], store_root)
    assert rc == 0
    out = capsys.readouterr().out
    assert "auto request_id" not in out
    assert store.messages_for("beta")[-1].meta["request_id"] == "mine-123"


def test_send_review_result_without_request_id_warns_soft(
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """Missing request_id on a review-result is a soft stderr warning,
    exit still 0 (don't break mixed-version peers)."""
    rc = _run(["send", "--from", "alpha", "--to", "beta",
               "--kind", "review-result", "-m", "looks good"], store_root)
    assert rc == 0
    assert "no request_id" in capsys.readouterr().err


def test_reply_review_request_autogenerates_request_id(
    store: Store,
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """A reply that is itself a fresh review-request (no id to echo from
    the original) mints one too."""
    store.send(sender="alpha", recipient="beta", body="ping", kind="message")
    capsys.readouterr()
    rc = _run(["reply", "--from", "beta", "--kind", "review-request",
               "-m", "now review my counter-work"], store_root)
    assert rc == 0
    assert "auto request_id" in capsys.readouterr().out
    assert store.messages_for("alpha")[-1].meta.get("request_id", "").startswith("rq-")


def test_reply_review_request_does_not_inherit_original_request_id(
    store: Store,
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """Regression (issue #5 / Codex R5 blocker): a reply that is itself a
    review-request opens a NEW correlation thread, so it must MINT a fresh
    request_id rather than echo the request_id of the message it replies
    to — otherwise two distinct request/result pairs alias each other."""
    # alpha sends a review-request that already carries a request_id.
    _run(["send", "--from", "alpha", "--to", "beta",
          "--kind", "review-request", "--meta", "request_id=orig-123",
          "-m", "review my work"], store_root)
    capsys.readouterr()
    # beta hands back a COUNTER review-request via reply.
    rc = _run(["reply", "--from", "beta", "--kind", "review-request",
               "-m", "ok, now review mine"], store_root)
    assert rc == 0
    new_rid = store.messages_for("alpha")[-1].meta.get("request_id", "")
    assert new_rid != "orig-123"      # did NOT inherit the original id
    assert new_rid.startswith("rq-")  # minted a fresh one


def test_reply_review_result_still_echoes_request_id(
    store: Store,
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """Counterpart to the above: a review-RESULT reply MUST still echo the
    original request_id so the verdict correlates to the open request."""
    _run(["send", "--from", "alpha", "--to", "beta",
          "--kind", "review-request", "--meta", "request_id=orig-456",
          "-m", "review my work"], store_root)
    capsys.readouterr()
    rc = _run(["reply", "--from", "beta", "--kind", "review-result",
               "--meta", "status=approved", "-m", "looks good"], store_root)
    assert rc == 0
    assert store.messages_for("alpha")[-1].meta.get("request_id") == "orig-456"


# ============================ 0.10.0: proposals ============================

def test_propose_mints_pp_id_and_proposal_kind(
    store: Store, store_root: Path, capsys: pytest.CaptureFixture,
) -> None:
    rc = _run(["propose", "--from", "alpha", "--to", "beta",
               "--subject", "use X", "-m", "## Problem\nneed X"], store_root)
    assert rc == 0
    msg = store.messages_for("beta")[-1]
    assert msg.kind == "proposal"
    assert msg.meta.get("request_id", "").startswith("pp-")


def test_propose_print_id_outputs_request_id(
    store: Store, store_root: Path, capsys: pytest.CaptureFixture,
) -> None:
    rc = _run(["propose", "--from", "alpha", "--to", "beta",
               "-m", "do X", "--print-id", "--quiet"], store_root)
    assert rc == 0
    out = capsys.readouterr().out.strip()
    # --quiet suppresses the render + the "(proposal id: ...)" line, so the
    # only stdout is the bare correlation id for capture.
    assert out.startswith("pp-")
    assert store.messages_for("beta")[-1].meta["request_id"] == out


def test_propose_in_reply_to_sets_meta(
    store: Store, store_root: Path, capsys: pytest.CaptureFixture,
) -> None:
    rc = _run(["propose", "--from", "alpha", "--to", "beta",
               "--in-reply-to", "pp-old123", "-m", "counter"], store_root)
    assert rc == 0
    assert store.messages_for("beta")[-1].meta.get("in_reply_to") == "pp-old123"


def test_propose_empty_body_errors(store_root: Path) -> None:
    _run_expect_exit(["propose", "--from", "alpha", "--to", "beta"], store_root, 2)


def test_send_question_autogen_q_request_id(
    store: Store, store_root: Path, capsys: pytest.CaptureFixture,
) -> None:
    rc = _run(["send", "--from", "alpha", "--to", "beta",
               "--kind", "question", "-m", "what now?"], store_root)
    assert rc == 0
    assert store.messages_for("beta")[-1].meta.get("request_id", "").startswith("q-")


def test_reply_proposal_response_echoes_request_id(
    store: Store, store_root: Path, capsys: pytest.CaptureFixture,
) -> None:
    _run(["propose", "--from", "alpha", "--to", "beta",
          "--meta", "request_id=pp-abc", "-m", "do X"], store_root)
    capsys.readouterr()
    rc = _run(["reply", "--from", "beta", "--kind", "proposal-response",
               "--meta", "status=accepted", "-m", "agreed"], store_root)
    assert rc == 0
    resp = store.messages_for("alpha")[-1]
    assert resp.kind == "proposal-response"
    assert resp.meta.get("request_id") == "pp-abc"
    assert resp.meta.get("status") == "accepted"


def test_reply_counter_proposal_opens_fresh_thread(
    store: Store, store_root: Path, capsys: pytest.CaptureFixture,
) -> None:
    """A reply whose own kind is `proposal` (a counter) must NOT inherit the
    anchored proposal's request_id — it opens a new thread."""
    _run(["propose", "--from", "alpha", "--to", "beta",
          "--meta", "request_id=pp-abc", "-m", "do X"], store_root)
    capsys.readouterr()
    rc = _run(["reply", "--from", "beta", "--kind", "proposal",
               "-m", "do Y instead"], store_root)
    assert rc == 0
    new_rid = store.messages_for("alpha")[-1].meta.get("request_id", "")
    assert new_rid != "pp-abc"
    assert new_rid.startswith("pp-")


def test_proposal_response_missing_request_id_warns(
    store_root: Path, capsys: pytest.CaptureFixture,
) -> None:
    rc = _run(["send", "--from", "beta", "--to", "alpha",
               "--kind", "proposal-response", "--meta", "status=accepted",
               "-m", "ok"], store_root)
    assert rc == 0
    err = capsys.readouterr().err
    assert "proposal-response has no request_id" in err


# ====================== 0.10.0: anchored reply ============================

def test_reply_to_id_anchors_specific_message(
    store: Store, store_root: Path, capsys: pytest.CaptureFixture,
) -> None:
    # Two threads open toward alpha, each from a different peer-side msg.
    _run(["send", "--from", "beta", "--to", "alpha", "--kind", "review-request",
          "--meta", "request_id=first", "-m", "thread one"], store_root)
    first_id = store.messages_for("alpha")[0].id
    _run(["send", "--from", "beta", "--to", "alpha", "--kind", "question",
          "--meta", "request_id=second", "-m", "thread two"], store_root)
    capsys.readouterr()
    # Anchor explicitly to the FIRST (older) message, not the most recent.
    rc = _run(["reply", "--from", "alpha", "--to-id", first_id,
               "--kind", "review-result", "--meta", "status=approved",
               "-m", "verdict for thread one"], store_root)
    assert rc == 0
    reply = store.messages_for("beta")[-1]
    assert reply.meta.get("request_id") == "first"  # echoed the anchor's id
    assert reply.recipient == "beta"


def test_reply_to_id_not_found_errors(store_root: Path) -> None:
    _run_expect_exit(
        ["reply", "--from", "alpha", "--to-id", "nope-404", "-m", "x"],
        store_root, 2,
    )


def test_reply_to_id_and_to_request_are_mutually_exclusive(store_root: Path) -> None:
    # Supplying both anchors must be a usage error, not a silent pick.
    _run_expect_exit(
        ["reply", "--from", "alpha", "--to-id", "x", "--to-request", "y", "-m", "z"],
        store_root, 2,
    )


def test_propose_no_longer_accepts_allow_empty(store_root: Path) -> None:
    # --allow-empty was removed from propose (a proposal must have a body).
    _run_expect_exit(
        ["propose", "--from", "alpha", "--to", "beta", "--allow-empty"],
        store_root, 2,
    )


def test_reply_to_request_anchors_by_request_id(
    store: Store, store_root: Path, capsys: pytest.CaptureFixture,
) -> None:
    _run(["send", "--from", "beta", "--to", "alpha", "--kind", "review-request",
          "--meta", "request_id=first", "-m", "thread one"], store_root)
    _run(["send", "--from", "beta", "--to", "alpha", "--kind", "question",
          "--meta", "request_id=second", "-m", "thread two"], store_root)
    capsys.readouterr()
    rc = _run(["reply", "--from", "alpha", "--to-request", "first",
               "--kind", "review-result", "--meta", "status=approved",
               "-m", "verdict"], store_root)
    assert rc == 0
    assert store.messages_for("beta")[-1].meta.get("request_id") == "first"


# ============================ 0.10.0: threads =============================

def test_threads_json_open_outbound(
    store_root: Path, capsys: pytest.CaptureFixture,
) -> None:
    _run(["propose", "--from", "alpha", "--to", "beta",
          "--meta", "request_id=pp-1", "-m", "do X", "--quiet"], store_root)
    capsys.readouterr()
    rc = _run(["threads", "--for", "alpha", "--json"], store_root)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["agent"] == "alpha"
    assert payload["counts"]["open-outbound"] == 1
    assert payload["threads"][0]["request_id"] == "pp-1"
    assert payload["threads"][0]["state"] == "open-outbound"


def test_threads_default_hides_closed_all_shows(
    store: Store, store_root: Path, capsys: pytest.CaptureFixture,
) -> None:
    _run(["propose", "--from", "alpha", "--to", "beta",
          "--meta", "request_id=pp-1", "-m", "do X", "--quiet"], store_root)
    _run(["reply", "--from", "beta", "--kind", "proposal-response",
          "--meta", "status=accepted", "-m", "ok"], store_root)
    # alpha consumes the verdict so the thread is closed for them.
    _run(["drain", "--for", "alpha", "--quiet"], store_root)
    capsys.readouterr()
    # default: no actionable rows
    _run(["threads", "--for", "alpha", "--json"], store_root)
    default = json.loads(capsys.readouterr().out)
    assert default["threads"] == []
    assert default["counts"]["closed"] == 1
    # --all: the closed thread shows
    _run(["threads", "--for", "alpha", "--all", "--json"], store_root)
    allrows = json.loads(capsys.readouterr().out)
    assert len(allrows["threads"]) == 1
    assert allrows["threads"][0]["state"] == "closed"


def test_status_warns_about_unconsumed_reply(
    store: Store, store_root: Path, capsys: pytest.CaptureFixture,
) -> None:
    _run(["propose", "--from", "alpha", "--to", "beta",
          "--meta", "request_id=pp-1", "-m", "do X", "--quiet"], store_root)
    _run(["reply", "--from", "beta", "--kind", "proposal-response",
          "--meta", "status=accepted", "-m", "ok"], store_root)
    capsys.readouterr()
    _run(["status", "--json"], store_root)
    payload = json.loads(capsys.readouterr().out)
    warnings = " ".join(payload["warnings"])
    # alpha has an unconsumed proposal-response sitting in the inbox.
    assert "unconsumed response" in warnings
    assert "pp-1" in warnings


# ======================== 0.13.0: ergonomics (#6/#7/#8) ====================

def test_reply_dry_run_resolves_without_sending(
    store: Store, store_root: Path, capsys: pytest.CaptureFixture,
) -> None:
    _run(["send", "--from", "beta", "--to", "alpha", "--kind", "review-request",
          "--meta", "request_id=r1", "-m", "review"], store_root)
    before = len(list((store_root / ".agenttalk" / "messages").glob("*.json")))
    capsys.readouterr()
    # No body on purpose: --dry-run must NOT require one (it sends nothing).
    rc = _run(["reply", "--from", "alpha", "--dry-run", "--kind", "review-result",
               "--meta", "status=approved"], store_root)
    assert rc == 0
    out = capsys.readouterr().out
    assert "dry-run" in out and "r1" in out and "-> beta" in out
    after = len(list((store_root / ".agenttalk" / "messages").glob("*.json")))
    assert after == before  # nothing was sent


def test_file_dash_reads_body_from_stdin(
    store: Store, store_root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO("body via stdin\n"))
    rc = _run(["send", "--from", "alpha", "--to", "beta", "--kind", "note",
               "--file", "-", "--quiet"], store_root)
    assert rc == 0
    assert store.messages_for("beta")[-1].body == "body via stdin\n"


def test_whoami_json_shows_identity_and_warns_off_roster(
    store_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    monkeypatch.setenv("AGENTTALK_SELF", "alpha")
    capsys.readouterr()
    _run(["whoami", "--json"], store_root)
    p = json.loads(capsys.readouterr().out)
    assert p["self"] == "alpha" and p["self_in_roster"] is True
    assert p["peer"] == "beta" and p["root"]
    # a self not in the roster (likely wrong --root) warns
    monkeypatch.setenv("AGENTTALK_SELF", "ghost")
    capsys.readouterr()
    _run(["whoami", "--json"], store_root)
    p2 = json.loads(capsys.readouterr().out)
    assert p2["self_in_roster"] is False
    assert any("NOT in the roster" in w for w in p2["warnings"])




# ======================================================================
# 0.14.0 CLI surface (WP02): rescind / check / wait-wake / escalate /
# init guard / operator-facing / composing sugar / display additions
# ======================================================================

def _send_q(root: Path, sender: str, recipient: str, rid: str, body: str = "q") -> None:
    rc = _run(["send", "--from", sender, "--to", recipient, "--kind", "question",
               "--meta", f"request_id={rid}", "-m", body, "--quiet"], root)
    assert rc == 0


def _team_root(tmp_path: Path, agents: str = "lead,w1,w2") -> Path:
    rc = cli.main(["init", "--path", str(tmp_path), "--agents", agents])
    assert rc == 0
    return tmp_path


# ----------------------------------------------------------- rescind (T007)

def test_rescind_happy_path_and_thread_state(store_root: Path, capsys) -> None:
    _send_q(store_root, "alpha", "beta", "q-1", "fire the launch")
    rc = _run(["rescind", "--from", "alpha", "--to-request", "q-1",
               "-m", "new data - hold"], store_root)
    assert rc == 0
    out = capsys.readouterr().out
    assert "RESCIND" in out
    data = json.loads(_threads_json(store_root, "alpha"))
    row = next(t for t in data["threads"] if t["request_id"] == "q-1")
    assert row["state"] == "closed-superseded"
    assert row["rescind"]["by"] == "alpha"
    assert row["rescind"]["reason"] == "new data - hold"


def _threads_json(root: Path, agent: str) -> str:
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = _run(["threads", "--for", agent, "--all", "--json"], root)
    assert rc == 0
    return buf.getvalue()


def test_rescind_refusal_matrix(store_root: Path) -> None:
    _send_q(store_root, "alpha", "beta", "q-1")
    # non-requester
    _run_expect_exit(["rescind", "--from", "beta", "--to-request", "q-1"],
                     store_root, 2)
    # unknown rid
    _run_expect_exit(["rescind", "--from", "alpha", "--to-request", "q-nope"],
                     store_root, 2)
    # bad --to-id
    _run_expect_exit(["rescind", "--from", "alpha", "--to-request", "q-1",
                      "--to-id", "20990101-000000-000000-XXXX"], store_root, 2)


def test_rescind_already_superseded_is_idempotent_audit(store_root: Path, capsys) -> None:
    _send_q(store_root, "alpha", "beta", "q-1")
    assert _run(["rescind", "--from", "alpha", "--to-request", "q-1",
                 "-m", "first", "--quiet"], store_root) == 0
    rc = _run(["rescind", "--from", "alpha", "--to-request", "q-1",
               "-m", "second", "--quiet"], store_root)
    assert rc == 0
    err = capsys.readouterr().err
    assert "already superseded" in err
    # first rescind remains the decider
    row = next(t for t in json.loads(_threads_json(store_root, "alpha"))["threads"]
               if t["request_id"] == "q-1")
    assert row["rescind"]["reason"] == "first"


def test_rescind_broadcast_fans_to_all_recipients(tmp_path: Path) -> None:
    root = _team_root(tmp_path)
    for r in ("w1", "w2"):
        rc = _run(["send", "--from", "lead", "--to", r, "--kind", "question",
                   "--meta", "request_id=b-1", "--meta", "broadcast_id=b-1",
                   "--meta", "audience=all", "-m", "status?", "--quiet"], root)
        assert rc == 0
    assert _run(["rescind", "--from", "lead", "--to-request", "b-1",
                 "--quiet"], root) == 0
    msgs = [json.loads(p.read_text(encoding="utf-8"))
            for p in (root / ".agenttalk" / "messages").glob("*.json")]
    rescinds = [m for m in msgs if m["kind"] == "rescind"]
    assert sorted(m["to"] for m in rescinds) == ["w1", "w2"]
    assert all(m["meta"]["request_id"] == "b-1" for m in rescinds)


# ------------------------------------------------------------- check (T008)

def test_check_exit_codes_and_json(store_root: Path, capsys) -> None:
    _send_q(store_root, "alpha", "beta", "q-1")
    assert _run(["check", "--for", "beta", "--to-request", "q-1"], store_root) == 0
    assert "current" in capsys.readouterr().out
    _run_expect_exit(["check", "--for", "beta", "--to-request", "q-ghost"],
                     store_root, 4)
    assert _run(["rescind", "--from", "alpha", "--to-request", "q-1",
                 "-m", "hold", "--quiet"], store_root) == 0
    capsys.readouterr()
    rc = _run(["check", "--for", "beta", "--to-request", "q-1", "--json"], store_root)
    assert rc == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == "superseded"
    assert payload["rescind"]["by"] == "alpha"
    assert payload["rescind"]["reason"] == "hold"


def test_check_is_read_only(store_root: Path) -> None:
    _send_q(store_root, "alpha", "beta", "q-1")
    state_dir = store_root / ".agenttalk" / "state"
    before = {p.name: p.read_bytes() for p in state_dir.iterdir()}
    assert _run(["check", "--for", "beta", "--to-request", "q-1"], store_root) == 0
    after = {p.name: p.read_bytes() for p in state_dir.iterdir()}
    assert before == after


def test_check_not_masked_by_local_ack(store_root: Path) -> None:
    # The barrier rule: a local ack closes the VIEW, never the fact.
    _send_q(store_root, "alpha", "beta", "q-1")
    assert _run(["rescind", "--from", "alpha", "--to-request", "q-1",
                 "--quiet"], store_root) == 0
    assert _run(["ack", "--for", "beta", "--to-request", "q-1"], store_root) == 0
    rc = _run(["check", "--for", "beta", "--to-request", "q-1"], store_root)
    assert rc == 3


# ------------------------------------------------- scoped-wait wake (T009)

def test_scoped_wait_wakes_rescinded_immediately(store_root: Path, capsys) -> None:
    _send_q(store_root, "alpha", "beta", "q-1")
    assert _run(["rescind", "--from", "alpha", "--to-request", "q-1",
                 "-m", "hold", "--quiet"], store_root) == 0
    t0 = time.time()
    rc = _run(["wait", "--for", "beta", "--to-request", "q-1",
               "--timeout", "30", "--heartbeat-interval", "0"], store_root)
    assert rc == 3
    assert time.time() - t0 < 5  # immediate, not a timeout
    out = capsys.readouterr().out
    assert "RESCINDED" in out
    assert "hold" in out


def test_scoped_wait_rescind_beats_kind_filter(store_root: Path) -> None:
    _send_q(store_root, "alpha", "beta", "q-1")
    assert _run(["rescind", "--from", "alpha", "--to-request", "q-1",
                 "--quiet"], store_root) == 0
    rc = _run(["wait", "--for", "beta", "--to-request", "q-1",
               "--kind", "review-result", "--timeout", "30",
               "--heartbeat-interval", "0"], store_root)
    assert rc == 3


def test_scoped_wait_timeout_stays_exit_1(store_root: Path) -> None:
    # C-005: exit 1 remains timeout-exclusive on a live (non-rescinded) thread.
    _send_q(store_root, "alpha", "beta", "q-1")
    rc = _run(["wait", "--for", "alpha", "--to-request", "q-1",
               "--timeout", "0.3", "--grace", "0",
               "--heartbeat-interval", "0"], store_root)
    assert rc == 1


def test_scoped_wait_does_not_consume_on_rescind_wake(store_root: Path) -> None:
    _send_q(store_root, "alpha", "beta", "q-1")
    assert _run(["rescind", "--from", "alpha", "--to-request", "q-1",
                 "--quiet"], store_root) == 0
    s = Store(store_root)
    cursor_before = s.cursor("beta")
    rc = _run(["wait", "--for", "beta", "--to-request", "q-1",
               "--timeout", "30", "--heartbeat-interval", "0"], store_root)
    assert rc == 3
    assert s.cursor("beta") == cursor_before  # delivery untouched
    assert len(s.unread_for("beta")) >= 2     # question + rescind still unread


# ---------------------------------------------------------- escalate (T013)

def test_escalate_routes_to_liaison_and_prints_rid(tmp_path: Path, capsys) -> None:
    root = _team_root(tmp_path)
    assert _run(["roster", "set-operator-facing", "lead"], root) == 0
    capsys.readouterr()
    rc = _run(["escalate", "--from", "w1", "-m", "Deploy today or tomorrow?"], root)
    assert rc == 0
    out = capsys.readouterr().out
    rid_lines = [ln for ln in out.splitlines() if ln.startswith("request_id=")]
    assert len(rid_lines) == 1
    rid = rid_lines[0].split("=", 1)[1]
    assert rid.startswith("esc-")
    msgs = [json.loads(p.read_text(encoding="utf-8"))
            for p in (root / ".agenttalk" / "messages").glob("*.json")]
    esc = next(m for m in msgs if m["meta"].get("needs_operator") == "true")
    assert esc["to"] == "lead"
    assert esc["kind"] == "question"
    assert esc["meta"]["request_id"] == rid


def test_escalate_refusal_matrix(tmp_path: Path, capsys) -> None:
    root = _team_root(tmp_path)
    # no liaison configured
    _run_expect_exit(["escalate", "--from", "w1", "-m", "ping"], root, 2)
    assert "set-operator-facing" in capsys.readouterr().err
    # --to override works without a liaison
    assert _run(["escalate", "--from", "w1", "--to", "lead", "-m", "ping",
                 "--quiet"], root) == 0
    # liaison self-escalation refused
    assert _run(["roster", "set-operator-facing", "lead"], root) == 0
    capsys.readouterr()
    _run_expect_exit(["escalate", "--from", "lead", "-m", "self"], root, 2)
    assert "operator channel" in capsys.readouterr().err
    # configured liaison gone from roster
    assert _run(["roster", "set-operator-facing", "w2"], root) == 0
    assert _run(["roster", "remove", "w2", "--force"], root) == 0  # #19: --force
    capsys.readouterr()
    _run_expect_exit(["escalate", "--from", "w1", "-m", "ping"], root, 2)
    assert "not in" in capsys.readouterr().err
    # empty body
    _run_expect_exit(["escalate", "--from", "w1"], root, 2)


def test_escalation_lifecycle_in_sync_bucket(tmp_path: Path, capsys) -> None:
    root = _team_root(tmp_path)
    assert _run(["roster", "set-operator-facing", "lead"], root) == 0
    assert _run(["escalate", "--from", "w1", "-m", "Need a decision",
                 "--quiet"], root) == 0
    capsys.readouterr()
    assert _run(["sync", "--for", "lead", "--json"], root) == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["escalations"]) == 1
    rid = payload["escalations"][0]["request_id"]
    assert payload["escalations"][0]["operator_state"] == "pending"
    # liaison answers -> bucket empties
    assert _run(["send", "--from", "lead", "--to", "w1", "--kind", "message",
                 "--meta", f"request_id={rid}",
                 "--meta", "operator_answer=true",
                 "-m", "Operator says: tomorrow.", "--quiet"], root) == 0
    capsys.readouterr()
    assert _run(["sync", "--for", "lead", "--json"], root) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["escalations"] == []
    # requester sees answered
    row = next(t for t in json.loads(_threads_json(root, "w1"))["threads"]
               if t["request_id"] == rid)
    assert row["operator_state"] == "answered"


def test_sync_escalations_key_only_for_liaison(tmp_path: Path, capsys) -> None:
    root = _team_root(tmp_path)
    assert _run(["roster", "set-operator-facing", "lead"], root) == 0
    assert _run(["escalate", "--from", "w1", "-m", "x", "--quiet"], root) == 0
    capsys.readouterr()
    assert _run(["sync", "--for", "w2", "--json"], root) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "escalations" not in payload


# -------------------------------------------------------- init guard (T011)

def test_init_refuses_nested_store(store_root: Path, capsys) -> None:
    sub = store_root / "nested" / "deeper"
    sub.mkdir(parents=True)
    rc = cli.main(["init", "--path", str(sub), "--agents", "a,b"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "up-tree" in err
    assert "--force" in err
    assert not (sub / ".agenttalk").exists()


def test_init_force_allows_deliberate_nesting(store_root: Path) -> None:
    sub = store_root / "sandbox"
    sub.mkdir()
    rc = cli.main(["init", "--path", str(sub), "--agents", "a,b", "--force"])
    assert rc == 0
    assert (sub / ".agenttalk").is_dir()


def test_init_reinit_at_same_root_unchanged(store_root: Path) -> None:
    # A store at the target itself keeps the idempotent re-init behavior.
    rc = cli.main(["init", "--path", str(store_root), "--agents", "alpha,beta"])
    assert rc == 0


# --------------------------------------- roster set-operator-facing (T012)

def test_set_operator_facing_roundtrip_and_displays(tmp_path: Path, capsys) -> None:
    root = _team_root(tmp_path)
    assert _run(["roster", "set-operator-facing", "lead"], root) == 0
    capsys.readouterr()
    assert _run(["roster"], root) == 0
    assert "[operator-facing]" in capsys.readouterr().out
    assert _run(["roster", "--json"], root) == 0
    assert json.loads(capsys.readouterr().out)["operator_facing"] == "lead"
    assert _run(["whoami", "--for", "lead", "--json"], root) == 0
    w = json.loads(capsys.readouterr().out)
    assert w["operator_facing"] is True and w["liaison"] == "lead"
    assert _run(["status", "--json"], root) == 0
    srow = next(a for a in json.loads(capsys.readouterr().out)["agents"]
                if a["name"] == "lead")
    assert srow["operator_facing"] is True
    # clear
    assert _run(["roster", "set-operator-facing", "--clear"], root) == 0
    capsys.readouterr()
    assert _run(["roster", "--json"], root) == 0
    assert json.loads(capsys.readouterr().out)["operator_facing"] is None


def test_set_operator_facing_refusals(tmp_path: Path) -> None:
    root = _team_root(tmp_path)
    _run_expect_exit(["roster", "set-operator-facing", "ghost"], root, 2)
    _run_expect_exit(["roster", "set-operator-facing"], root, 2)
    _run_expect_exit(["roster", "set-operator-facing", "lead", "--clear"], root, 2)


# ------------------------------------------------- composing sugar (T014)

def test_composing_to_request_sets_meta_and_marker(store_root: Path) -> None:
    _send_q(store_root, "alpha", "beta", "q-1")
    rc = _run(["composing", "--from", "beta", "--to", "alpha",
               "--to-request", "q-1", "--quiet"], store_root)
    assert rc == 0
    s = Store(store_root)
    intent = s.read_composing_intent("beta")
    assert "q-1" in intent["threads"]
    assert intent["threads"]["q-1"]["peer"] == "alpha"
    msgs = [json.loads(p.read_text(encoding="utf-8"))
            for p in (store_root / ".agenttalk" / "messages").glob("*.json")]
    comp = next(m for m in msgs if m["kind"] == "composing")
    assert comp["meta"]["request_id"] == "q-1"


def test_composing_to_request_refusals(store_root: Path) -> None:
    _send_q(store_root, "alpha", "beta", "q-1")
    # unknown rid
    _run_expect_exit(["composing", "--from", "beta", "--to", "alpha",
                      "--to-request", "q-ghost", "--quiet"], store_root, 2)
    # conflicting explicit meta
    _run_expect_exit(["composing", "--from", "beta", "--to", "alpha",
                      "--to-request", "q-1", "--meta", "request_id=q-other",
                      "--quiet"], store_root, 2)
    # closed thread
    assert _run(["send", "--from", "beta", "--to", "alpha", "--kind", "message",
                 "--meta", "request_id=q-1", "-m", "answer", "--quiet"],
                store_root) == 0
    _run_expect_exit(["composing", "--from", "beta", "--to", "alpha",
                      "--to-request", "q-1", "--quiet"], store_root, 2)


def test_reply_in_flight_annotation_and_stale_suppression(store_root: Path, capsys) -> None:
    _send_q(store_root, "alpha", "beta", "q-1")
    assert _run(["composing", "--from", "beta", "--to", "alpha",
                 "--to-request", "q-1", "--quiet"], store_root) == 0
    capsys.readouterr()
    assert _run(["threads", "--for", "alpha", "--json"], store_root) == 0
    row = next(t for t in json.loads(capsys.readouterr().out)["threads"]
               if t["request_id"] == "q-1")
    assert row.get("reply_in_flight") is True


# ------------------------------------------------ display additivity (NFR-001)

def test_json_outputs_have_no_new_keys_without_new_features(store_root: Path, capsys) -> None:
    # A store using only pre-0.14.0 surface: every new key must be ABSENT
    # (strict additivity), not null.
    _send_q(store_root, "alpha", "beta", "q-plain")
    capsys.readouterr()
    assert _run(["threads", "--for", "alpha", "--json"], store_root) == 0
    row = json.loads(capsys.readouterr().out)["threads"][0]
    for key in ("rescind", "needs_operator", "operator_state", "reply_in_flight"):
        assert key not in row
    assert _run(["sync", "--for", "alpha", "--json"], store_root) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "rescinded" not in payload
    assert "escalations" not in payload
    assert _run(["status", "--json"], store_root) == 0
    for a in json.loads(capsys.readouterr().out)["agents"]:
        assert "operator_facing" not in a


def test_sync_flags_unconsumed_rescind(store_root: Path, capsys) -> None:
    _send_q(store_root, "alpha", "beta", "q-1")
    assert _run(["rescind", "--from", "alpha", "--to-request", "q-1",
                 "-m", "hold", "--quiet"], store_root) == 0
    capsys.readouterr()
    assert _run(["sync", "--for", "beta", "--json"], store_root) == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["rescinded"]) == 1
    assert payload["rescinded"][0]["request_id"] == "q-1"
    # after draining (rescind consumed), the flag stops nagging
    assert _run(["drain", "--for", "beta", "--quiet"], store_root) == 0
    capsys.readouterr()
    assert _run(["sync", "--for", "beta", "--json"], store_root) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "rescinded" not in payload


# ------------------- composing sugar: review-blocker regressions (T014)

def test_composing_to_request_single_argument_multi_agent(tmp_path: Path) -> None:
    # WP02 review blocker 1: the rid identifies the counterparty - no
    # --to / AGENTTALK_PEER needed even in a >2-agent roster.
    root = _team_root(tmp_path)
    _send_q(root, "lead", "w1", "q-1")
    rc = _run(["composing", "--from", "w1", "--to-request", "q-1", "--quiet"], root)
    assert rc == 0
    msgs = [json.loads(p.read_text(encoding="utf-8"))
            for p in (root / ".agenttalk" / "messages").glob("*.json")]
    comp = next(m for m in msgs if m["kind"] == "composing")
    assert comp["to"] == "lead"  # derived from the thread row
    assert Store(root).read_composing_intent("w1")["threads"]["q-1"]["peer"] == "lead"


def test_composing_to_request_rejects_outbound_view(store_root: Path, capsys) -> None:
    # WP02 review blocker 2: the requester (open-outbound) is not drafting
    # a reply - composing marks YOUR in-flight reply, not the peer's.
    _send_q(store_root, "alpha", "beta", "q-1")
    _run_expect_exit(["composing", "--from", "alpha", "--to", "beta",
                      "--to-request", "q-1", "--quiet"], store_root, 2)
    assert "do not owe a reply" in capsys.readouterr().err


def test_composing_to_request_rejects_mismatched_to(tmp_path: Path, capsys) -> None:
    root = _team_root(tmp_path)
    _send_q(root, "lead", "w1", "q-1")
    _run_expect_exit(["composing", "--from", "w1", "--to", "w2",
                      "--to-request", "q-1", "--quiet"], root, 2)
    assert "disagrees" in capsys.readouterr().err


def test_composing_to_request_allows_needs_info_requester(store_root: Path) -> None:
    # The needs-info ping-pong: after a review-result(needs-info) the ball
    # is on the REQUESTER, who drafts the answer on the same rid. A
    # role-based gate would break this; the state-based gate allows it.
    rc = _run(["send", "--from", "alpha", "--to", "beta",
               "--kind", "review-request", "--meta", "request_id=rq-1",
               "-m", "please review", "--quiet"], store_root)
    assert rc == 0
    rc = _run(["send", "--from", "beta", "--to", "alpha",
               "--kind", "review-result", "--meta", "request_id=rq-1",
               "--meta", "status=needs-info", "-m", "which env?", "--quiet"],
              store_root)
    assert rc == 0
    # Until alpha READS the needs-info it is reply-waiting (you cannot be
    # drafting a reply to something unread) - composing refuses.
    _run_expect_exit(["composing", "--from", "alpha", "--to-request", "rq-1",
                      "--quiet"], store_root, 2)
    assert _run(["drain", "--for", "alpha", "--quiet"], store_root) == 0
    # Now alpha (the requester) owes the answer - composing must work,
    # and the derived recipient is beta.
    rc = _run(["composing", "--from", "alpha", "--to-request", "rq-1",
               "--quiet"], store_root)
    assert rc == 0
    msgs = [json.loads(p.read_text(encoding="utf-8"))
            for p in (store_root / ".agenttalk" / "messages").glob("*.json")]
    comp = next(m for m in msgs if m["kind"] == "composing")
    assert comp["to"] == "beta"


# ======================================================================
# 0.15.0 CLI surface (WP02): --to-role / exit 5 / reply --na / prune
# ======================================================================

def _role_root(tmp_path: Path) -> Path:
    root = _team_root(tmp_path, "lead,rev-a,rev-b,impl-c")
    for a, r in (("rev-a", "reviewer"), ("rev-b", "reviewer"),
                 ("impl-c", "implementer")):
        assert _run(["roster", "set-role", a, r], root) == 0
    return root


def _msgs_on_disk(root: Path) -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8"))
            for p in (root / ".agenttalk" / "messages").glob("*.json")]


# -------------------------------------------------- --to-role (T006)

def test_broadcast_to_role_routes_and_freezes(tmp_path: Path) -> None:
    root = _role_root(tmp_path)
    rc = _run(["broadcast", "--from", "lead", "--to-role", "reviewer",
               "--kind", "question", "-m", "fresh eyes?", "--quiet"], root)
    assert rc == 0
    copies = [m for m in _msgs_on_disk(root) if m["kind"] == "question"]
    assert sorted(m["to"] for m in copies) == ["rev-a", "rev-b"]
    for m in copies:
        assert m["meta"]["audience_kind"] == "role"
        assert m["meta"]["audience_role"] == "reviewer"
        assert m["meta"]["audience_resolved"] == "rev-a,rev-b"
        assert m["meta"]["batch_total"] == "2"
    # the implementer neither receives nor owes
    s = Store(root)
    assert s.messages_for("impl-c") == []


def test_broadcast_to_role_refusals(tmp_path: Path, capsys) -> None:
    root = _role_root(tmp_path)
    _run_expect_exit(["broadcast", "--from", "lead", "--to-role", "ghost",
                      "-m", "x", "--quiet"], root, 2)
    assert "reviewer" in capsys.readouterr().err  # known roles named
    # sender is the only member of the role -> empty after exclusion
    assert _run(["roster", "set-role", "lead", "lonely"], root) == 0
    _run_expect_exit(["broadcast", "--from", "lead", "--to-role", "lonely",
                      "-m", "x", "--quiet"], root, 2)
    assert "no members besides" in capsys.readouterr().err


def test_broadcast_group_copies_also_freeze(tmp_path: Path) -> None:
    root = _role_root(tmp_path)
    assert _run(["roster", "set-group", "pair", "rev-a,impl-c"], root) == 0
    assert _run(["broadcast", "--from", "lead", "--to-group", "pair",
                 "-m", "fyi", "--quiet"], root) == 0
    copies = [m for m in _msgs_on_disk(root) if m["meta"].get("broadcast_id")]
    assert all(m["meta"]["audience_kind"] == "group" for m in copies)
    assert all(m["meta"]["batch_total"] == "2" for m in copies)
    assert all("audience_role" not in m["meta"] for m in copies)


# --------------------------------------------- partial fan-out (T007)

def _fail_at(store_cls, k: int):
    """Monkeypatch helper: make the k-th (1-based) Store.send call raise."""
    calls = {"n": 0}
    original = store_cls.send

    def wrapper(self, **kwargs):
        calls["n"] += 1
        if calls["n"] == k:
            raise OSError("disk full (injected)")
        return original(self, **kwargs)

    return wrapper


def test_broadcast_partial_failure_exit5_manifest(tmp_path: Path, capsys,
                                                  monkeypatch) -> None:
    root = _role_root(tmp_path)
    monkeypatch.setattr(Store, "send", _fail_at(Store, 2))
    rc = _run(["broadcast", "--from", "lead", "--to-role", "reviewer",
               "--kind", "question", "-m", "x", "--quiet"], root)
    assert rc == 5
    captured = capsys.readouterr()
    assert "delivered=[rev-a]" in captured.out
    assert "missed=[rev-b]" in captured.out
    assert "--resume" in captured.err             # the one-command recovery
    assert "rescind" in captured.err
    monkeypatch.undo()
    # exactly one copy on disk
    assert len([m for m in _msgs_on_disk(root) if m["kind"] == "question"]) == 1


def test_broadcast_partial_failure_json_manifest(tmp_path: Path, capsys,
                                                 monkeypatch) -> None:
    root = _role_root(tmp_path)
    monkeypatch.setattr(Store, "send", _fail_at(Store, 1))  # zero delivered
    capsys.readouterr()  # flush roster-setup output before parsing JSON
    rc = _run(["broadcast", "--from", "lead", "--to-role", "reviewer",
               "-m", "x", "--json", "--quiet"], root)
    assert rc == 5
    payload = json.loads(capsys.readouterr().out)
    assert payload["delivered"] == []
    assert payload["missed"] == ["rev-a", "rev-b"]
    assert payload["batch_id"].startswith("b-")


def test_incomplete_batch_warning_lifecycle(tmp_path: Path, capsys,
                                            monkeypatch) -> None:
    root = _role_root(tmp_path)
    monkeypatch.setattr(Store, "send", _fail_at(Store, 2))
    capsys.readouterr()
    rc = _run(["broadcast", "--from", "lead", "--to-role", "reviewer",
               "--kind", "question", "-m", "x", "--quiet"], root)
    assert rc == 5
    # recover the bid from the delivered copy on disk
    bid = next(m["meta"]["broadcast_id"] for m in _msgs_on_disk(root)
               if m["meta"].get("broadcast_id"))
    monkeypatch.undo()
    capsys.readouterr()
    assert _run(["status", "--json"], root) == 0
    warnings = json.loads(capsys.readouterr().out)["warnings"]
    hit = [w for w in warnings if "incomplete fan-out" in w]
    assert len(hit) == 1
    assert "rev-b" in hit[0]            # missed member named
    # resolution path A: follow the PRINTED remediation - one command
    assert _run(["broadcast", "--from", "lead", "--resume", bid,
                 "--quiet"], root) == 0
    capsys.readouterr()
    assert _run(["status", "--json"], root) == 0
    warnings = json.loads(capsys.readouterr().out)["warnings"]
    assert not [w for w in warnings if "incomplete fan-out" in w]
    # ...and the recovered member actually OWES the thread now
    assert _run(["threads", "--for", "rev-b", "--json"], root) == 0
    # (flush handled by next readouterr)
    rows = json.loads(capsys.readouterr().out)["threads"]
    row = next(r for r in rows if r["request_id"] == bid)
    assert row["state"] == "owed-inbound"


def test_incomplete_batch_warning_suppressed_by_rescind(tmp_path: Path, capsys,
                                                        monkeypatch) -> None:
    root = _role_root(tmp_path)
    monkeypatch.setattr(Store, "send", _fail_at(Store, 2))
    assert _run(["broadcast", "--from", "lead", "--to-role", "reviewer",
                 "--kind", "question", "-m", "x", "--quiet"], root) == 5
    monkeypatch.undo()
    bid = next(m["meta"]["broadcast_id"] for m in _msgs_on_disk(root)
               if m["meta"].get("broadcast_id"))
    assert _run(["rescind", "--from", "lead", "--to-request", bid,
                 "--quiet"], root) == 0
    capsys.readouterr()
    assert _run(["status", "--json"], root) == 0
    warnings = json.loads(capsys.readouterr().out)["warnings"]
    assert not [w for w in warnings if "incomplete fan-out" in w]


# ------------------------------------------------------ reply --na (T008)

def test_reply_na_closes_with_label(tmp_path: Path, capsys) -> None:
    root = _role_root(tmp_path)
    assert _run(["broadcast", "--from", "lead", "--to-role", "reviewer",
                 "--kind", "question", "--meta", "request_id=b-na1",
                 "-m", "thoughts?", "--quiet"], root) == 0
    rc = _run(["reply", "--from", "rev-b", "--to-request", "b-na1", "--na",
               "--quiet"], root)
    assert rc == 0
    msgs = _msgs_on_disk(root)
    na = next(m for m in msgs if m["meta"].get("response") == "not-applicable")
    assert na["kind"] == "message" and na["body"] == "n/a"
    capsys.readouterr()
    assert _run(["threads", "--for", "lead", "--json"], root) == 0
    row = next(t for t in json.loads(capsys.readouterr().out)["threads"]
               if t["request_id"] == "b-na1")
    assert row["responded_na"] == ["rev-b"]
    assert "rev-b" in row["responded"]
    assert row["pending"] == ["rev-a"]


def test_reply_na_refusals(tmp_path: Path, capsys) -> None:
    root = _role_root(tmp_path)
    # review-request thread -> typed response required (FR-006)
    assert _run(["send", "--from", "lead", "--to", "rev-a",
                 "--kind", "review-request", "--meta", "request_id=rq-x",
                 "-m", "review", "--quiet"], root) == 0
    _run_expect_exit(["reply", "--from", "rev-a", "--to-request", "rq-x",
                      "--na", "--quiet"], root, 2)
    assert "review-result" in capsys.readouterr().err
    # proposal thread
    assert _run(["send", "--from", "lead", "--to", "rev-a",
                 "--kind", "proposal", "--meta", "request_id=pp-x",
                 "-m", "plan", "--quiet"], root) == 0
    _run_expect_exit(["reply", "--from", "rev-a", "--to-request", "pp-x",
                      "--na", "--quiet"], root, 2)
    assert "proposal-response" in capsys.readouterr().err
    # --kind conflict
    assert _run(["send", "--from", "lead", "--to", "rev-a",
                 "--kind", "question", "--meta", "request_id=q-x",
                 "-m", "q", "--quiet"], root) == 0
    _run_expect_exit(["reply", "--from", "rev-a", "--to-request", "q-x",
                      "--na", "--kind", "note", "--quiet"], root, 2)
    assert "mutually exclusive" in capsys.readouterr().err


def test_reply_na_pairwise_question_with_body(tmp_path: Path, capsys) -> None:
    root = _role_root(tmp_path)
    assert _run(["send", "--from", "lead", "--to", "impl-c",
                 "--kind", "question", "--meta", "request_id=q-p",
                 "-m", "deploy steps?", "--quiet"], root) == 0
    assert _run(["reply", "--from", "impl-c", "--to-request", "q-p", "--na",
                 "-m", "reviewer territory - not my lane", "--quiet"], root) == 0
    # consume the answer: an unread reply is (correctly) reply-waiting
    assert _run(["drain", "--for", "lead", "--quiet"], root) == 0
    capsys.readouterr()
    assert _run(["threads", "--for", "lead", "--all", "--json"], root) == 0
    row = next(t for t in json.loads(capsys.readouterr().out)["threads"]
               if t["request_id"] == "q-p")
    assert row["state"] == "closed"
    assert row["na_response"] is True


# ----------------------------------------------------------- prune (T009)

def test_prune_flow_and_json(store_root: Path, capsys) -> None:
    (store_root / ".agenttalk" / "messages" / "junk.json").write_text(
        "{not json", encoding="utf-8")
    # bare prune refuses
    _run_expect_exit(["prune"], store_root, 2)
    # dry run lists, moves nothing
    capsys.readouterr()
    assert _run(["prune", "--invalid", "--dry-run", "--json"], store_root) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert len(payload["selected"]) == 1 and payload["moved"] == []
    assert (store_root / ".agenttalk" / "messages" / "junk.json").exists()
    # real run moves
    capsys.readouterr()
    assert _run(["prune", "--invalid", "--json"], store_root) == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["moved"]) == 1
    assert not (store_root / ".agenttalk" / "messages" / "junk.json").exists()
    # zero invalid -> friendly no-op
    capsys.readouterr()
    assert _run(["prune", "--invalid"], store_root) == 0
    assert "nothing to prune" in capsys.readouterr().out


def test_status_quarantined_count_additive(store_root: Path, capsys) -> None:
    capsys.readouterr()
    assert _run(["status", "--json"], store_root) == 0
    assert "quarantined" not in json.loads(capsys.readouterr().out)  # absent at 0
    (store_root / ".agenttalk" / "messages" / "junk.json").write_text(
        "{not json", encoding="utf-8")
    assert _run(["prune", "--invalid", "--quiet"], store_root) == 0
    capsys.readouterr()
    assert _run(["status", "--json"], store_root) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["quarantined"] == 1
    assert payload["invalid_messages"] == []


# ------------------------------------------- additivity + exit codes

def test_no_feature_store_emits_no_new_keys_0150(store_root: Path, capsys) -> None:
    _send_q(store_root, "alpha", "beta", "q-plain")
    capsys.readouterr()
    assert _run(["threads", "--for", "alpha", "--json"], store_root) == 0
    row = json.loads(capsys.readouterr().out)["threads"][0]
    for k in ("responded_na", "na_response", "batch_total", "audience_kind"):
        assert k not in row
    assert _run(["status", "--json"], store_root) == 0
    assert "quarantined" not in json.loads(capsys.readouterr().out)


def test_reply_na_explicit_kind_message_conflicts(tmp_path: Path, capsys) -> None:
    # WP02 review blocker 2: even `--kind message` is an explicit --kind.
    root = _role_root(tmp_path)
    assert _run(["send", "--from", "lead", "--to", "rev-a",
                 "--kind", "question", "--meta", "request_id=q-k",
                 "-m", "q", "--quiet"], root) == 0
    _run_expect_exit(["reply", "--from", "rev-a", "--to-request", "q-k",
                      "--na", "--kind", "message", "--quiet"], root, 2)
    assert "mutually exclusive" in capsys.readouterr().err


def test_broadcast_resume_edge_cases(tmp_path: Path, capsys) -> None:
    root = _role_root(tmp_path)
    # unknown bid
    _run_expect_exit(["broadcast", "--from", "lead", "--resume", "b-ghost",
                      "--quiet"], root, 2)
    # complete batch -> friendly no-op
    assert _run(["broadcast", "--from", "lead", "--to-role", "reviewer",
                 "--kind", "question", "--meta", "request_id=b-full",
                 "-m", "x", "--quiet"], root) == 0
    capsys.readouterr()
    assert _run(["broadcast", "--from", "lead", "--resume", "b-full"], root) == 0
    assert "nothing to resume" in capsys.readouterr().out
    # non-broadcaster refused
    _run_expect_exit(["broadcast", "--from", "rev-a", "--resume", "b-full",
                      "--quiet"], root, 2)
    # overrides refused
    _run_expect_exit(["broadcast", "--from", "lead", "--resume", "b-full",
                      "-m", "new body", "--quiet"], root, 2)


def test_zero_delivered_fanout_advises_rerun_not_resume(tmp_path: Path, capsys,
                                                        monkeypatch) -> None:
    # fresh-eyes 0.15.0 note 1: nothing on disk -> resume/rescind advice
    # would be un-actionable; advise re-running instead.
    root = _role_root(tmp_path)
    monkeypatch.setattr(Store, "send", _fail_at(Store, 1))
    capsys.readouterr()
    rc = _run(["broadcast", "--from", "lead", "--to-role", "reviewer",
               "-m", "x", "--quiet"], root)
    assert rc == 5
    err = capsys.readouterr().err
    assert "re-run the" in err
    assert "--resume" not in err


def test_to_role_empty_string_role_shaped_error(tmp_path: Path, capsys) -> None:
    # fresh-eyes 0.15.0 note 2: explicit empty role must not fall into
    # the group branch.
    root = _role_root(tmp_path)
    _run_expect_exit(["broadcast", "--from", "lead", "--to-role", "",
                      "-m", "x", "--quiet"], root, 2)
    err = capsys.readouterr().err
    assert "--to-role" in err
    assert "group" not in err


# ===================================================== #19 Phase A (WP03/T016)
# Roster retire/rename/remove/forward, barrier bump, check --epoch, json next_*.

def _epoch_team(tmp_path: Path) -> Path:
    return _team_root(tmp_path, "alpha,beta,gamma")


def test_roster_retire_and_refusals(tmp_path: Path, capsys) -> None:
    root = _epoch_team(tmp_path)
    assert _run(["roster", "retire", "gamma", "--reason", "left"], root) == 0
    assert "tombstone" in capsys.readouterr().out
    # already retired -> exit 2
    _run_expect_exit(["roster", "retire", "gamma"], root, 2)
    # retired identity cannot send (FR-004) with a tombstone-specific message
    _run_expect_exit(["send", "--from", "gamma", "--to", "alpha", "-m", "hi"], root, 2)
    assert "retired" in capsys.readouterr().err


def test_roster_retire_json(tmp_path: Path, capsys) -> None:
    # contract: `roster retire --json` returns the updated {"retired": [...]} slice.
    root = _epoch_team(tmp_path)
    capsys.readouterr()
    assert _run(["roster", "retire", "gamma", "--reason", "left", "--json"], root) == 0
    out = json.loads(capsys.readouterr().out)
    assert [e["name"] for e in out["retired"]] == ["gamma"]
    assert out["retired"][0]["reason"] == "left"


def test_roster_rename_carryover_and_drain_check(tmp_path: Path, capsys) -> None:
    root = _epoch_team(tmp_path)
    assert _run(["roster", "set-role", "gamma", "reviewer"], root) == 0
    assert _run(["roster", "set-operator-facing", "gamma"], root) == 0
    capsys.readouterr()
    assert _run(["roster", "rename", "gamma", "gamma-rev"], root) == 0
    cfg = Store(root).load_config()
    assert "gamma-rev" in cfg["agents"] and "gamma" not in cfg["agents"]
    assert cfg["roles"]["gamma-rev"] == "reviewer"
    assert cfg["operator_facing"] == "gamma-rev"
    # non-rebindable: cannot rename to a tombstone
    _run_expect_exit(["roster", "rename", "alpha", "gamma"], root, 2)


def test_roster_rename_drain_check_blocks(tmp_path: Path, capsys) -> None:
    root = _epoch_team(tmp_path)
    # an open review-request owed to gamma
    assert _run(["send", "--from", "alpha", "--to", "gamma",
                 "--kind", "review-request", "--meta", "request_id=r1",
                 "-m", "review", "--quiet"], root) == 0
    capsys.readouterr()
    _run_expect_exit(["roster", "rename", "gamma", "gx", "--drain-check"], root, 2)
    assert "open thread" in capsys.readouterr().err
    # gamma was NOT renamed
    assert "gamma" in Store(root).load_config()["agents"]


def test_roster_remove_force_gate(tmp_path: Path, capsys) -> None:
    root = _epoch_team(tmp_path)
    _run_expect_exit(["roster", "remove", "gamma"], root, 2)
    assert "roster retire" in capsys.readouterr().err
    assert _run(["roster", "remove", "gamma", "--force"], root) == 0
    assert "FAIL roster validation" in capsys.readouterr().err
    assert "gamma" not in Store(root).load_config()["agents"]


def test_roster_forward(tmp_path: Path, capsys) -> None:
    root = _epoch_team(tmp_path)
    assert _run(["send", "--from", "alpha", "--to", "gamma",
                 "--kind", "review-request", "--meta", "request_id=rf",
                 "-m", "review", "--quiet"], root) == 0
    assert _run(["roster", "retire", "gamma"], root) == 0
    capsys.readouterr()
    assert _run(["roster", "forward", "gamma", "--to", "beta",
                 "--to-request", "rf", "--from", "alpha"], root) == 0
    assert "forwarded" in capsys.readouterr().out
    # second hop refused
    _run_expect_exit(["roster", "forward", "gamma", "--to", "beta",
                      "--to-request", "rf", "--from", "alpha"], root, 2)


def test_barrier_bump(tmp_path: Path, capsys) -> None:
    root = _epoch_team(tmp_path)
    capsys.readouterr()  # flush init output before reading the JSON
    assert _run(["barrier", "bump", "--from", "alpha", "--scope", "global",
                 "-m", "void", "--json"], root) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["scope"] == "global" and out["epoch"]
    # bad scope -> exit 2
    _run_expect_exit(["barrier", "bump", "--from", "alpha", "--scope", "local",
                      "-m", "x"], root, 2)
    # retired bumper -> exit 2
    assert _run(["roster", "retire", "gamma"], root) == 0
    _run_expect_exit(["barrier", "bump", "--from", "gamma", "--scope", "global",
                      "-m", "x"], root, 2)


def _open_req(root: Path, rid: str) -> None:
    _run(["send", "--from", "alpha", "--to", "beta", "--kind", "review-request",
          "--meta", f"request_id={rid}", "-m", "x", "--quiet"], root)


def test_check_epoch_states(tmp_path: Path, capsys) -> None:
    root = _epoch_team(tmp_path)
    # no barrier yet -> current
    _open_req(root, "r1")
    assert _run(["check", "--for", "beta", "--to-request", "r1", "--epoch"], root) == 0
    # fire barrier, open a NEW request under it -> current
    assert _run(["barrier", "bump", "--from", "alpha", "--scope", "global",
                 "-m", "e1"], root) == 0
    _open_req(root, "r2")
    assert _run(["check", "--for", "beta", "--to-request", "r2", "--epoch"], root) == 0
    # r1 predates the barrier (epoch_at_send null) -> previous-epoch, exit 3
    _run_expect_exit(["check", "--for", "beta", "--to-request", "r1", "--epoch"], root, 3)
    # a second barrier makes r2 previous-epoch too
    assert _run(["barrier", "bump", "--from", "beta", "--scope", "global",
                 "-m", "e2"], root) == 0
    _run_expect_exit(["check", "--for", "beta", "--to-request", "r2", "--epoch"], root, 3)
    # unknown rid -> exit 4
    _run_expect_exit(["check", "--for", "beta", "--to-request", "nope", "--epoch"], root, 4)


def test_check_epoch_json_shape(tmp_path: Path, capsys) -> None:
    root = _epoch_team(tmp_path)
    _open_req(root, "r1")
    assert _run(["barrier", "bump", "--from", "alpha", "--scope", "global",
                 "-m", "e"], root) == 0
    capsys.readouterr()
    _run_expect_exit(["check", "--for", "beta", "--to-request", "r1",
                      "--epoch", "--json"], root, 3)
    out = json.loads(capsys.readouterr().out)
    assert out["state"] == "current"          # rescind dimension unchanged
    assert out["epoch"]["state"] == "previous-epoch"
    # non --epoch check stays byte-shape stable (no 'epoch' key)
    _run(["check", "--for", "beta", "--to-request", "r1", "--json"], root)
    out2 = json.loads(capsys.readouterr().out)
    assert "epoch" not in out2


def test_threads_json_next_fields(tmp_path: Path, capsys) -> None:
    root = _epoch_team(tmp_path)
    _open_req(root, "r1")
    rows = _run_json(["threads", "--for", "beta", "--all", "--json"], root, capsys)
    row = [r for r in rows["threads"] if r["request_id"] == "r1"][0]
    assert row["next_action"] == "reply" and row["next_owner"] == "beta"
    # closed thread omits next_*
    _run(["reply", "--to-request", "r1", "--from", "beta",
          "--kind", "review-result", "--meta", "status=approved",
          "-m", "lgtm"], root)
    _run(["ack", "--for", "beta", "--to-request", "r1"], root)
    rows = _run_json(["threads", "--for", "beta", "--all", "--json"], root, capsys)
    row = [r for r in rows["threads"] if r["request_id"] == "r1"][0]
    assert "next_action" not in row and "next_owner" not in row


def _run_json(argv: list[str], root: Path, capsys) -> dict:
    capsys.readouterr()
    assert _run(argv, root) == 0
    return json.loads(capsys.readouterr().out)
