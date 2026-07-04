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


def test_start_init_if_absent_requires_explicit_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    monkeypatch.chdir(tmp_path)

    rc = cli.main(["start", "--init-if-absent", "--agents", "alpha,beta", "--dry-run"])

    assert rc == 2
    assert not (tmp_path / ".agenttalk").exists()
    assert "requires an explicit location" in capsys.readouterr().err


def test_start_init_if_absent_here_dry_run_bootstraps_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    monkeypatch.chdir(tmp_path)

    rc = cli.main(["start", "--init-if-absent", "--here", "--agents", "alpha,beta", "--dry-run"])

    assert rc == 0
    assert (tmp_path / ".agenttalk" / "config.json").exists()
    out = capsys.readouterr().out
    assert '"initialized": true' in out


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
        # Sync on the wait's ACTUAL deadline via the .waiting marker's
        # deadline_epoch (NOT a fixed thread sleep, which races a late arm and
        # could land before the real deadline -> caught by the normal scan, not
        # grace). Send just AFTER the deadline but well within the 3s grace
        # window, so the message is delivered ONLY by the post-timeout grace
        # scan — deterministically exercising the grace path.
        deadline_epoch = None
        poll_until = time.monotonic() + 2.0
        while time.monotonic() < poll_until:
            m = s.read_waiting("beta")
            if m is not None and isinstance(m.get("deadline_epoch"), (int, float)):
                deadline_epoch = m["deadline_epoch"]
                break
            time.sleep(0.02)
        assert deadline_epoch is not None, "wait never published a deadline"
        while time.time() <= deadline_epoch + 0.2:  # past the deadline, in grace
            time.sleep(0.02)
        s.send(sender="alpha", recipient="beta",
               body="just barely in time", kind="message")

    t = threading.Thread(target=_inject_after_deadline, daemon=True)
    t.start()
    try:
        rc = _run(["wait", "--for", "beta", "--timeout", "1",
                   "--grace", "3",
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
    """A `composing` ping extends the deadline so a later real reply is still
    surfaced (and the composing body itself is never returned as a payload).

    PRE-STAGE the composing so the very first poll is guaranteed to see it and
    extend the 0.5s deadline by 2s — no fixed-sleep-vs-deadline race on a tight
    margin (the prior 0.4s-vs-0.5s injection flaked on a loaded runner). The
    real reply is then injected from a thread at a modest 0.3s, which dwarfs
    scheduler jitter inside the ~2.5s extended window. Same model as
    test_wait_duplicate_composing_counted_only_once."""
    import threading
    from agenttalk.store import Store

    s = Store(store_root)
    # Pre-staged: present before the wait arms, so the first scan extends.
    s.send(sender="alpha", recipient="beta", body="hold on", kind="composing")

    def _inject_reply() -> None:
        time.sleep(0.3)  # well inside the +2s extension; not racing a deadline
        s.send(sender="alpha", recipient="beta",
               body="here's the real answer", kind="message")

    t = threading.Thread(target=_inject_reply, daemon=True)
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


def test_wait_composing_extends_deadline_duration(
    store_root: Path,
) -> None:
    """Prove the composing actually extends the deadline BY ITS AMOUNT (not
    just that a reply lands). Pre-stage one composing, send NO real reply, and
    assert the wait runs at least base_timeout + composing_extend before timing
    out. The lower bound is race-free — the loop never returns before its
    (extended) deadline and sleeps never undershoot — so this can't flake, and
    a base-only timeout (~0.5s) would fall well short of the 0.85s floor."""
    from agenttalk.store import Store

    s = Store(store_root)
    s.send(sender="alpha", recipient="beta", body="hold on", kind="composing")
    started = time.time()
    rc = _run(["wait", "--for", "beta", "--timeout", "0.5",
               "--grace", "0",
               "--composing-extend", "0.5",  # extends 0.5 -> ~1.0s effective
               "--heartbeat-interval", "0", "--quiet"], store_root)
    elapsed = time.time() - started
    assert rc == 1  # no real reply: still times out, just later
    assert elapsed >= 0.85, (
        f"composing did not extend the deadline by its amount: {elapsed:.2f}s "
        "(base 0.5 + extend 0.5 should be ~1.0s)"
    )


def test_wait_composing_extension_disabled_with_zero(
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """--composing-extend 0: a composing is still consumed (never surfaced as a
    reply) but does NOT extend the deadline.

    PRE-STAGE the composing so the consume-but-don't-extend path is exercised
    on the first scan deterministically; with no extension and no real reply
    the wait times out at its base 0.5s deadline (rc 1) regardless of jitter.
    No thread, no race."""
    from agenttalk.store import Store

    s = Store(store_root)
    s.send(sender="alpha", recipient="beta", body="hold on", kind="composing")
    rc = _run(["wait", "--for", "beta", "--timeout", "0.5",
               "--grace", "0",
               "--composing-extend", "0",
               "--heartbeat-interval", "0", "--quiet"], store_root)
    assert rc == 1  # consumed, not extended, no real reply -> timeout


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
    the NEXT wait. Under --ack (the default) the first wait advances the
    on-disk cursor past the consumed composing, so a later wait never
    re-sees it and cannot re-extend. Regression for Codex's iter-1
    BLOCKER #2: "single stale composing makes every later wait pay the
    extension again, contradicting the recv --ack rationale that stale
    control pings should not pin the cursor."

    Asserted on the extension's OBSERVABLE output: `wait` prints a
    "composing from <peer>" notice exactly when a ping extends its
    deadline. This is fully deterministic. We deliberately do NOT assert
    on wall-clock elapsed (absolute or relative): the poll loop's
    `time.sleep` can be descheduled and overshoot under CI load, which
    makes any timing threshold flaky while proving nothing the output
    does not already prove.
    """
    # One stale composing in the inbox.
    store.send(sender="alpha", recipient="beta",
               body="hold on", kind="composing")
    capsys.readouterr()
    # First wait: the composing is fresh, so it is consumed for extension
    # (the "composing from alpha" notice prints) AND --ack advances the
    # cursor past it. Run non-quiet so the notice is observable.
    rc1 = _run(["wait", "--for", "beta", "--timeout", "0.2",
                "--grace", "0",
                "--composing-extend", "0.5",
                "--heartbeat-interval", "0"], store_root)
    out1 = capsys.readouterr().out
    assert rc1 == 1
    assert "composing from alpha" in out1            # fresh ping DID extend
    assert Store(store_root).cursor("beta") != ""    # ...and advanced the cursor
    # Second wait: same stale composing, but the cursor is now past it, so it
    # must not be re-seen and must not extend. If the bug existed, the notice
    # would print again.
    rc2 = _run(["wait", "--for", "beta", "--timeout", "0.2",
                "--grace", "0",
                "--composing-extend", "0.5",
                "--heartbeat-interval", "0"], store_root)
    out2 = capsys.readouterr().out
    assert rc2 == 1
    assert "composing from alpha" not in out2, (
        f"second wait re-extended on a stale composing: {out2!r}"
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
        # POLL for the marker over a bounded window instead of reading once at
        # a fixed offset — arm-time work (foreign-pid scan, soft-cap, optional
        # auto-compact) can delay the marker write on a slow/loaded runner, and
        # a single fixed read raced it (3.12/windows CI flake). Bounded well
        # under the wait's 3s timeout so a genuinely missing marker still fails.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            marker = s.read_waiting("beta")
            if marker is not None:
                saw_marker["mid_wait"] = marker
                break
            time.sleep(0.02)
        s.send(sender="alpha", recipient="beta", body="hi", kind="message")

    t = threading.Thread(target=_inject, daemon=True)
    t.start()
    try:
        rc = _run(["wait", "--for", "beta", "--timeout", "3",
                   "--heartbeat-interval", "0", "--quiet"], store_root)
        assert rc == 0
    finally:
        t.join(timeout=5)
    assert saw_marker.get("mid_wait") is not None
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


def test_wait_clears_marker_on_pre_loop_error(
    store_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: an error AFTER the early marker write but BEFORE the poll
    loop (here the second cursor() read raises) must still clear the .waiting
    marker — the top-level try/finally covers the widened arm window. Without
    it, moving the marker earlier would leak a ghost waiter on a setup error."""
    orig_cursor = Store.cursor
    calls = {"n": 0}

    def flaky_cursor(self: Store, agent: str) -> str:
        calls["n"] += 1
        if calls["n"] >= 2:          # the 2nd read (cursor_at_start) blows up
            raise OSError("boom")
        return orig_cursor(self, agent)

    monkeypatch.setattr(Store, "cursor", flaky_cursor)
    rc = _run(["wait", "--for", "beta", "--timeout", "3",
               "--heartbeat-interval", "0", "--quiet"], store_root)
    assert rc == 2                    # OSError surfaces as a usage/error exit
    assert calls["n"] >= 2            # we really hit the raising read
    monkeypatch.undo()
    assert Store(store_root).read_waiting("beta") is None  # NOT leaked


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
               *_approval_meta_args(), "-m", "looks good"], store_root)
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
               "--kind", "review-result", *_approval_meta_args(),
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
               "--kind", "review-result", *_approval_meta_args(),
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


def test_relay_operator_answer_uses_shared_resolver_parity(tmp_path: Path, capsys) -> None:
    root = _team_root(tmp_path)
    assert _run(["roster", "set-operator-facing", "lead"], root) == 0
    assert _run(["escalate", "--from", "w1", "-m", "Need a decision",
                 "--quiet"], root) == 0
    capsys.readouterr()
    assert _run(["sync", "--for", "lead", "--json"], root) == 0
    payload = json.loads(capsys.readouterr().out)
    rid = payload["escalations"][0]["request_id"]

    rc = _run(["relay", "operator-answer", "--from", "lead",
               "--to-request", rid, "-m", "Operator says go.",
               "--meta", "request_id=q-forged",
               "--meta", "operator_origin=forged",
               "--quiet"], root)

    assert rc == 0
    msgs = [json.loads(p.read_text(encoding="utf-8"))
            for p in (root / ".agenttalk" / "messages").glob("*.json")]
    answer = next(m for m in msgs if m["from"] == "lead" and m["to"] == "w1"
                  and m["meta"].get("operator_answer") == "true")
    assert answer["kind"] == "message"
    assert answer["meta"]["request_id"] == rid
    assert answer["meta"]["operator_origin"] == "lead"
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


# --------------------------------------- init honors global --root / env
# (review H2: every other command honors --root/AGENTTALK_ROOT; init must
# too, or it silently creates a SECOND store in the wrong dir = split-brain)

def test_init_honors_global_root_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)              # cwd is a clean temp dir
    target = tmp_path / "proj"
    target.mkdir()
    rc = cli.main(["--root", str(target), "init", "--agents", "a,b"])
    assert rc == 0
    assert (target / ".agenttalk").is_dir()           # created at --root
    assert not (tmp_path / ".agenttalk").exists()     # NOT in cwd


def test_init_honors_agenttalk_root_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "proj2"
    target.mkdir()
    monkeypatch.setenv("AGENTTALK_ROOT", str(target))
    rc = cli.main(["init", "--agents", "a,b"])
    assert rc == 0
    assert (target / ".agenttalk").is_dir()
    assert not (tmp_path / ".agenttalk").exists()


def test_init_path_wins_over_global_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    via_path = tmp_path / "viapath"
    via_path.mkdir()
    via_root = tmp_path / "viaroot"
    via_root.mkdir()
    rc = cli.main(["--root", str(via_root), "init", "--path", str(via_path),
                   "--agents", "a,b"])
    assert rc == 0
    assert (via_path / ".agenttalk").is_dir()         # explicit --path wins
    assert not (via_root / ".agenttalk").exists()


def test_read_body_explicit_empty_message_short_circuits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`-m ""` is a deliberate empty body — _read_body must return it without
    consulting stdin (which could hang on an open pipe). Pre-fix the falsy ""
    fell through to the stdin sniff (review nit)."""
    import argparse
    args = argparse.Namespace(message="", file=None)
    monkeypatch.setattr("sys.stdin", None)  # any stdin access would AttributeError
    assert cli._read_body(args) == ""


def test_send_rejects_kind_rescind(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """`send --kind rescind` is rejected — rescind has a dedicated command that
    handles fan-out/anchoring (review nit)."""
    cli.main(["init", "--path", str(tmp_path), "--agents", "alpha,beta"])
    rc = cli.main(["--root", str(tmp_path), "send", "--from", "alpha",
                   "--to", "beta", "-m", "x", "--kind", "rescind"])
    assert rc == 2
    assert "dedicated" in capsys.readouterr().err


def test_send_rejects_kind_end(tmp_path: Path) -> None:
    cli.main(["init", "--path", str(tmp_path), "--agents", "alpha,beta"])
    _run_expect_exit(["send", "--from", "alpha", "--to", "beta", "-m", "x",
                      "--kind", "end"], tmp_path, 2)


# --------------------------------------- capacity (advisory budget awareness)

def test_cli_capacity_refresh_and_show(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    cli.main(["init", "--path", str(tmp_path), "--agents", "alpha,beta"])
    sl = tmp_path / "statusline.json"
    sl.write_text(json.dumps({"rate_limits": {
        "five_hour": {"used_percentage": 85.0, "resets_at": 9999999999},
        "seven_day": {"used_percentage": 40.0, "resets_at": 9999999999}},
        "context_window": {"context_window_size": 200000, "used_percentage": 90}}), encoding="utf-8")
    rc = cli.main(["--root", str(tmp_path), "capacity", "refresh", "--for", "alpha",
                   "--source", "claude", "--statusline-path", str(sl)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "alpha" in out and "claude_statusline" in out
    rc2 = cli.main(["--root", str(tmp_path), "capacity"])
    assert rc2 == 0
    out2 = capsys.readouterr().out
    assert "5h 85% used" in out2 and "⚠" in out2  # near-cap flagged with ⚠
    assert "context 90%" in out2 and "near compaction" in out2  # context headroom shown + flagged


def test_cli_capacity_show_empty(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    cli.main(["init", "--path", str(tmp_path), "--agents", "alpha,beta"])
    rc = cli.main(["--root", str(tmp_path), "capacity"])
    assert rc == 0
    assert "no budgets published" in capsys.readouterr().out


def test_cli_capacity_refresh_unknown_when_no_signal(
    tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    cli.main(["init", "--path", str(tmp_path), "--agents", "alpha,beta"])
    rc = cli.main(["--root", str(tmp_path), "capacity", "refresh", "--for", "alpha",
                   "--source", "claude", "--statusline-path", str(tmp_path / "nope.json")])
    assert rc == 0
    assert "unknown" in capsys.readouterr().out.lower()


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


def test_gate_check_empty_required_set_is_go(store_root: Path, capsys) -> None:
    assert _run(["gate", "check", "--release", "--json"], store_root) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["verdict"] == "GO"
    assert out["required_gates"] == []


def test_gate_check_corrupt_state_fails_closed(store_root: Path, capsys) -> None:
    (store_root / ".agenttalk" / "gates.json").write_text("{not json", encoding="utf-8")
    assert _run(["gate", "check", "--release", "--json"], store_root) == 3
    out = json.loads(capsys.readouterr().out)
    assert out["verdict"] == "HOLD"
    assert out["blockers"][0]["name"] == "__gate_state__"


def test_gate_check_malformed_state_shape_fails_closed(store_root: Path, capsys) -> None:
    (store_root / ".agenttalk" / "gates.json").write_text(
        json.dumps({"required_gates": "connected-l1", "gates": []}),
        encoding="utf-8",
    )
    assert _run(["gate", "check", "--release", "--json"], store_root) == 3
    out = json.loads(capsys.readouterr().out)
    assert out["verdict"] == "HOLD"
    assert out["blockers"][0]["name"] == "__gate_state__"
    assert "required_gates" in out["blockers"][0]["reason"]


def test_gate_check_invalid_stored_gate_fails_closed(store_root: Path, capsys) -> None:
    (store_root / ".agenttalk" / "gates.json").write_text(
        json.dumps({
            "required_gates": ["connected-l1"],
            "gates": {
                "connected-l1": {
                    "name": "connected-l1",
                    "status": "banana",
                    "severity": "blocker",
                    "scope": "release",
                },
            },
        }),
        encoding="utf-8",
    )
    assert _run(["gate", "check", "--release", "--json"], store_root) == 3
    out = json.loads(capsys.readouterr().out)
    assert out["verdict"] == "HOLD"
    assert out["blockers"][0]["name"] == "__gate_state__"
    assert "invalid status" in out["blockers"][0]["reason"]


def test_gate_check_green_blocker_without_stored_evidence_fails_closed(
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    (store_root / ".agenttalk" / "gates.json").write_text(
        json.dumps({
            "required_gates": ["connected-l1"],
            "gates": {
                "connected-l1": {
                    "name": "connected-l1",
                    "status": "green",
                    "severity": "blocker",
                    "scope": "release",
                    "evidence_source": "automation_ci",
                },
            },
        }),
        encoding="utf-8",
    )
    assert _run(["gate", "check", "--release", "--json"], store_root) == 3
    out = json.loads(capsys.readouterr().out)
    assert out["blockers"][0]["name"] == "__gate_state__"
    assert "missing evidence" in out["blockers"][0]["reason"]


def test_gate_blocker_green_rejects_manual_review_source(store_root: Path, capsys) -> None:
    _run_expect_exit([
        "gate", "set", "--from", "alpha",
        "--name", "connected-l1",
        "--status", "green",
        "--severity", "blocker",
        "--evidence", "ci://connected-l1",
    ], store_root, 2)
    assert "automation_ci" in capsys.readouterr().err


def test_gate_set_operator_waiver_must_use_waive(store_root: Path, capsys) -> None:
    _run_expect_exit([
        "gate", "set", "--from", "alpha",
        "--name", "connected-l1",
        "--status", "green",
        "--severity", "blocker",
        "--scope", "release",
        "--evidence-source", "operator_waiver",
        "--evidence", "operator://approval",
        "--required",
    ], store_root, 2)
    assert "gate waive" in capsys.readouterr().err


def test_gate_red_blocker_holds_and_check_gates_blocks(
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    _run(["send", "--from", "alpha", "--to", "beta", "--kind", "review-request",
          "--meta", "request_id=r-gate", "-m", "review", "--quiet"], store_root)
    assert _run([
        "gate", "set", "--from", "alpha",
        "--name", "connected-l1",
        "--status", "red",
        "--severity", "blocker",
        "--scope", "release",
        "--reason", "connected lane failed",
        "--required",
    ], store_root) == 0
    capsys.readouterr()
    assert _run(["gate", "check", "--release", "--json"], store_root) == 3
    gate_out = json.loads(capsys.readouterr().out)
    assert gate_out["verdict"] == "HOLD"
    assert gate_out["blockers"][0]["name"] == "connected-l1"
    assert _run(["check", "--for", "beta", "--to-request", "r-gate",
                 "--gates", "--json"], store_root) == 3
    check_out = json.loads(capsys.readouterr().out)
    assert check_out["gates"]["verdict"] == "HOLD"


def test_gate_blocker_green_from_automation_goes_green(store_root: Path, capsys) -> None:
    assert _run([
        "gate", "set", "--from", "alpha",
        "--name", "connected-l1",
        "--status", "green",
        "--severity", "blocker",
        "--scope", "release",
        "--evidence-source", "automation_ci",
        "--evidence", "ci://connected-l1/123",
        "--required",
        "--json",
    ], store_root) == 0
    gate = json.loads(capsys.readouterr().out)
    assert gate["status"] == "green"
    assert _run(["gate", "check", "--release", "--json"], store_root) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["verdict"] == "GO"


def test_gate_waiver_allows_until_expired(store_root: Path, capsys) -> None:
    assert _run([
        "gate", "set", "--from", "alpha",
        "--name", "release-artifact",
        "--status", "red",
        "--severity", "blocker",
        "--scope", "release",
        "--reason", "debug artifact",
        "--required",
    ], store_root) == 0
    assert _run([
        "gate", "waive",
        "--name", "release-artifact",
        "--operator", "operator",
        "--reason", "private dogfood only",
        "--scope", "release",
        "--expires", "2999-01-01",
    ], store_root) == 0
    capsys.readouterr()
    assert _run(["gate", "check", "--release", "--json"], store_root) == 0
    assert json.loads(capsys.readouterr().out)["verdict"] == "GO"
    assert _run([
        "gate", "waive",
        "--name", "release-artifact",
        "--operator", "operator",
        "--reason", "expired waiver",
        "--scope", "release",
        "--expires", "2000-01-01",
    ], store_root) == 0
    capsys.readouterr()
    assert _run(["gate", "check", "--release", "--json"], store_root) == 3
    out = json.loads(capsys.readouterr().out)
    assert out["verdict"] == "HOLD"
    assert out["blockers"][0]["reason"] == "waiver expired or invalid"


def test_review_result_approval_requires_typed_evidence(
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    _run(["send", "--from", "alpha", "--to", "beta", "--kind", "review-request",
          "--meta", "request_id=r-evidence", "-m", "review", "--quiet"], store_root)
    _run_expect_exit([
        "reply", "--from", "beta", "--to-request", "r-evidence",
        "--kind", "review-result", "--meta", "status=approved",
        "-m", "approved",
    ], store_root, 2)
    assert "typed evidence" in capsys.readouterr().err
    assert _run([
        "reply", "--from", "beta", "--to-request", "r-evidence",
        "--kind", "review-result", *_approval_meta_args(),
        "-m", "approved",
    ], store_root) == 0


def test_threads_json_next_fields(tmp_path: Path, capsys) -> None:
    root = _epoch_team(tmp_path)
    _open_req(root, "r1")
    rows = _run_json(["threads", "--for", "beta", "--all", "--json"], root, capsys)
    row = [r for r in rows["threads"] if r["request_id"] == "r1"][0]
    assert row["next_action"] == "reply" and row["next_owner"] == "beta"
    # closed thread omits next_*
    _run(["reply", "--to-request", "r1", "--from", "beta",
          "--kind", "review-result", *_approval_meta_args(),
          "-m", "lgtm"], root)
    _run(["ack", "--for", "beta", "--to-request", "r1"], root)
    rows = _run_json(["threads", "--for", "beta", "--all", "--json"], root, capsys)
    row = [r for r in rows["threads"] if r["request_id"] == "r1"][0]
    assert "next_action" not in row and "next_owner" not in row


def _run_json(argv: list[str], root: Path, capsys) -> dict:
    capsys.readouterr()
    assert _run(argv, root) == 0
    return json.loads(capsys.readouterr().out)


# ================================================ 0.17.0 dashboard CLI (WP02)
#
# Contract: kitty-specs/obligation-dashboard-0170-01KTHADQ/contracts/
# cli-surface.md. `dashboard` is an alias to the same server code as
# `serve` (multi-root via --store, lands on /dashboard, NO --host);
# bind failures exit 2 with an actionable message on both spellings.

class _FakeServer:
    """Stands in for ThreadingHTTPServer in CLI tests: web-layer behavior
    is WP01-tested; here we only verify the wiring around it."""

    def __init__(self) -> None:
        self.server_address = ("127.0.0.1", 43210)

    def serve_forever(self) -> None:
        raise KeyboardInterrupt  # immediately "Ctrl-C" out of the loop

    def server_close(self) -> None:
        pass


def _exit_code(argv: list[str]) -> tuple[int, None]:
    try:
        return int(cli.main(argv)), None
    except SystemExit as e:
        return (0 if e.code is None else int(e.code)), None


def test_dashboard_help_surface(capsys: pytest.CaptureFixture) -> None:
    code, _ = _exit_code(["dashboard", "--help"])
    assert code == 0
    out = capsys.readouterr().out
    assert "--store" in out and "--port" in out and "--access-log" in out
    assert "--host" not in out


def test_dashboard_rejects_host_option(
    store_root: Path, capsys: pytest.CaptureFixture,
) -> None:
    """NFR-002(a): the alias has NO host surface at all — `--host` is an
    unknown option (usage exit 2), not a refused value."""
    code, _ = _exit_code(["--root", str(store_root), "dashboard",
                          "--host", "0.0.0.0", "--port", "0"])  # noqa: S104 — proving the option is REJECTED
    assert code == 2
    assert "--host" in capsys.readouterr().err


def test_serve_parser_unchanged(capsys: pytest.CaptureFixture) -> None:
    code, _ = _exit_code(["serve", "--help"])
    assert code == 0
    out = capsys.readouterr().out
    assert "--host" in out and "--port" in out and "--access-log" in out
    assert "--store" not in out


def test_bind_failure_exit2_both_spellings(
    store_root: Path, capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FR-010 (research D10; live WinError 10013 repro 2026-06-07): a
    bind OSError becomes exit 2 + an actionable message naming the
    spelling, the host:port, and the --port remedies.

    Raised via monkeypatch rather than a real second bind: HTTPServer
    sets SO_REUSEADDR, whose Windows semantics let it bind straight
    over a plain listener — a real-socket repro is not deterministic
    cross-platform, and the contract under test is OUR handling, not
    OS bind semantics."""
    from agenttalk import web as _web

    def boom(*a, **k):
        raise OSError("[WinError 10013] An attempt was made to access a "
                      "socket in a way forbidden by its access permissions")

    monkeypatch.setattr(_web, "make_server", boom)
    rc = cli.main(["--root", str(store_root), "serve", "--port", "8765"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "agenttalk serve: could not bind 127.0.0.1:8765" in err
    assert "--port 0" in err
    rc = cli.main(["--root", str(store_root), "dashboard", "--port", "8765"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "agenttalk dashboard: could not bind 127.0.0.1:8765" in err
    assert "--port 0" in err


def test_serve_nonloopback_host_still_exits2(
    store_root: Path, capsys: pytest.CaptureFixture,
) -> None:
    """The pre-0.17.0 ValueError path is untouched (and ordered before
    the new OSError handler)."""
    rc = cli.main(["--root", str(store_root), "serve",
                   "--host", "0.0.0.0", "--port", "0"])  # noqa: S104 — proving the host is REFUSED
    assert rc == 2
    err = capsys.readouterr().err
    assert "agenttalk serve:" in err and "loopback" in err


def test_dashboard_store_plumbing(
    tmp_path: Path, capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agenttalk import web as _web
    a = tmp_path / "proj-a"
    a.mkdir()
    Store(a).init(["alpha", "beta"])
    b = tmp_path / "proj-b"
    b.mkdir()
    Store(b).init(["lead", "dev"])
    captured: dict = {}

    def fake_make_server(store, host, port, *, quiet=True, extra=None,
                         enable_actions=False):
        captured.update(store=store, host=host, port=port,
                        extra=list(extra or []),
                        enable_actions=enable_actions)
        return _FakeServer()

    monkeypatch.setattr(_web, "make_server", fake_make_server)
    rc = cli.main(["dashboard", "--store", str(a), "--store", str(b),
                   "--port", "0"])
    assert rc == 0
    assert captured["host"] == "127.0.0.1"
    assert captured["enable_actions"] is False
    assert captured["store"].root == a.resolve()  # first --store is root[0]
    assert [d.store.root for d in captured["extra"]] == [b.resolve()]
    assert [d.label for d in captured["extra"]] == ["proj-b"]
    err = capsys.readouterr().err
    assert "obligation dashboard" in err
    assert "/dashboard" in err  # the alias lands on the hierarchy view


def test_dashboard_missing_store_warns_not_fatal(
    tmp_path: Path, capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Research D4: a --store path without .agenttalk WARNS and still
    becomes a (degraded) root — a viewer observes, it does not gate."""
    from agenttalk import web as _web
    good = tmp_path / "good"
    good.mkdir()
    Store(good).init(["alpha", "beta"])
    empty = tmp_path / "empty"
    empty.mkdir()
    captured: dict = {}

    def fake_make_server(store, host, port, *, quiet=True, extra=None,
                         enable_actions=False):
        captured.update(extra=list(extra or []), enable_actions=enable_actions)
        return _FakeServer()

    monkeypatch.setattr(_web, "make_server", fake_make_server)
    rc = cli.main(["dashboard", "--store", str(good), "--store", str(empty),
                   "--port", "0"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "no .agenttalk store yet" in err
    assert str(empty.resolve()) in err
    # the missing store is still passed through as a root descriptor
    assert [d.store.root for d in captured["extra"]] == [empty.resolve()]


# ===================================== 0.18.0 (WP03): review-hardening CLI

def test_tail_shows_retired_history(tmp_path: Path, capsys) -> None:
    """FR-004: a retired identity's history prints in `tail` (known roster),
    not as TAIL INVALID."""
    root = _team_root(tmp_path, agents="lead,beta")
    assert _run(["send", "--from", "beta", "--to", "lead",
                 "-m", "HISTORY_FROM_BETA", "--quiet"], root) == 0
    assert _run(["roster", "retire", "beta"], root) == 0
    capsys.readouterr()
    # bounded timeout: --from-start replays existing messages on the first
    # scan, then the loop exits at the deadline. (timeout 0 means "forever".)
    assert _run(["tail", "--from-start", "--timeout", "1",
                 "--interval", "0.1"], root) == 0
    captured = capsys.readouterr()
    assert "HISTORY_FROM_BETA" in captured.out
    assert "INVALID" not in captured.out and "INVALID" not in captured.err


def _partial_broadcast(root: Path, bid: str, members) -> None:
    """Send a frozen-audience broadcast to only the FIRST member, leaving the
    rest 'missed' (audience_resolved names all)."""
    resolved = ",".join(members)
    _run(["send", "--from", "lead", "--to", members[0], "--kind", "question",
          "--meta", f"request_id={bid}", "--meta", f"broadcast_id={bid}",
          "--meta", "audience=all", "--meta", f"audience_resolved={resolved}",
          "-m", "status?", "--quiet"], root)


def test_resume_all_retired_exit0_json_parseable(tmp_path: Path, capsys) -> None:
    """FR-005: when every still-missing recipient is retired, resume resolves
    (exit 0) and `--json` stdout is a single parseable manifest with dropped."""
    root = _team_root(tmp_path, agents="lead,w1,w2")
    _partial_broadcast(root, "b-1", ["w1", "w2"])   # only w1 delivered
    assert _run(["roster", "retire", "w2"], root) == 0
    capsys.readouterr()
    rc = _run(["broadcast", "--from", "lead", "--resume", "b-1", "--json"], root)
    assert rc == 0
    out = capsys.readouterr().out
    manifest = json.loads(out)   # stdout must be ONLY JSON
    assert manifest["dropped"] == ["w2"]
    assert manifest["missed"] == []
    # no new copy was sent to the retired recipient
    msgs = [json.loads(p.read_text(encoding="utf-8"))
            for p in (root / ".agenttalk" / "messages").glob("*.json")]
    assert all(m["to"] != "w2" for m in msgs)


def test_resume_mixed_active_and_retired_json_parseable(tmp_path: Path, capsys) -> None:
    """FR-005: a mix of one active-missing + one retired recipient → the
    active copy is (re)sent, the retired one dropped, exit 0, and `--json`
    success stdout is a single parseable manifest carrying both."""
    root = _team_root(tmp_path, agents="lead,w1,w2,w3")
    _partial_broadcast(root, "b-mix", ["w1", "w2", "w3"])  # only w1 delivered
    assert _run(["roster", "retire", "w3"], root) == 0     # w2 active, w3 retired
    capsys.readouterr()
    rc = _run(["broadcast", "--from", "lead", "--resume", "b-mix", "--json"], root)
    assert rc == 0
    manifest = json.loads(capsys.readouterr().out)   # stdout must be ONLY JSON
    assert "w2" in manifest["delivered"]
    assert manifest["dropped"] == ["w3"]
    msgs = [json.loads(p.read_text(encoding="utf-8"))
            for p in (root / ".agenttalk" / "messages").glob("*.json")]
    assert any(m["to"] == "w2" and m["meta"].get("broadcast_id") == "b-mix"
               for m in msgs)
    assert all(m["to"] != "w3" for m in msgs)


def test_resume_active_recipient_present_completes(tmp_path: Path, capsys) -> None:
    """A still-active missing recipient is actually (re)sent on resume."""
    root = _team_root(tmp_path, agents="lead,w1,w2")
    _partial_broadcast(root, "b-2", ["w1", "w2"])   # w2 still missing + active
    capsys.readouterr()
    rc = _run(["broadcast", "--from", "lead", "--resume", "b-2"], root)
    assert rc == 0
    msgs = [json.loads(p.read_text(encoding="utf-8"))
            for p in (root / ".agenttalk" / "messages").glob("*.json")]
    assert any(m["to"] == "w2" and m["meta"].get("broadcast_id") == "b-2"
               for m in msgs)


def test_resume_complete_batch_json_parseable(tmp_path: Path, capsys) -> None:
    """Fresh-eyes M1: `--resume --json` on an already-complete batch emits a
    single parseable manifest, not a human line."""
    root = _team_root(tmp_path, agents="lead,w1")
    # full single-recipient broadcast (audience_resolved == delivered)
    _run(["send", "--from", "lead", "--to", "w1", "--kind", "question",
          "--meta", "request_id=b-done", "--meta", "broadcast_id=b-done",
          "--meta", "audience=all", "--meta", "audience_resolved=w1",
          "-m", "q", "--quiet"], root)
    capsys.readouterr()
    rc = _run(["broadcast", "--from", "lead", "--resume", "b-done", "--json"], root)
    assert rc == 0
    manifest = json.loads(capsys.readouterr().out)   # stdout must be ONLY JSON
    assert manifest["delivered"] == ["w1"]
    assert manifest["missed"] == []


def test_wait_warns_on_live_duplicate(tmp_path: Path, capsys,
                                      monkeypatch: pytest.MonkeyPatch) -> None:
    """FR-007: `wait` warns when foreign_wait_pid reports a live duplicate;
    the warning never changes the exit code."""
    root = _team_root(tmp_path, agents="lead,beta")
    monkeypatch.setattr("agenttalk.store.Store.foreign_wait_pid",
                        lambda self, agent, pid, **kw: 4242)
    capsys.readouterr()
    # timeout 1 so the wait returns (exit 1, the documented wait-timeout)
    rc = _run(["wait", "--for", "lead", "--timeout", "1",
               "--heartbeat-interval", "0"], root)
    err = capsys.readouterr().err
    assert "another live process (PID 4242)" in err
    assert "one window per agent" in err.lower()
    assert rc == 1   # warning did NOT change the exit code


def test_wait_no_warning_when_no_duplicate(tmp_path: Path, capsys,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    root = _team_root(tmp_path, agents="lead,beta")
    monkeypatch.setattr("agenttalk.store.Store.foreign_wait_pid",
                        lambda self, agent, pid, **kw: None)
    capsys.readouterr()
    rc = _run(["wait", "--for", "lead", "--timeout", "1",
               "--heartbeat-interval", "0"], root)
    err = capsys.readouterr().err
    assert "another live process" not in err
    assert rc == 1


# --- 0.24.0: escalate lead-fallback (WP02, FR-001/002/003) ----------------

def _lead_team(tmp_path: Path) -> Path:
    Store(tmp_path).init(["alpha", "beta", "gamma"])
    return tmp_path


def test_escalate_falls_back_to_lead(tmp_path: Path, capsys) -> None:
    root = _lead_team(tmp_path)
    Store(root).set_role("beta", "lead")
    rc = _run(["escalate", "--from", "alpha", "-m", "need a ruling"], root)
    out = capsys.readouterr()
    assert rc == 0
    assert "routing to the lead 'beta'" in out.err
    assert "request_id=esc-" in out.out


def test_escalate_no_liaison_no_lead_exits_2_with_both_remediations(
    tmp_path: Path, capsys,
) -> None:
    root = _lead_team(tmp_path)
    rc = _run(["escalate", "--from", "alpha", "-m", "x"], root)
    err = capsys.readouterr().err
    assert rc == 2
    assert "set-operator-facing" in err and "set-role" in err


def test_escalate_liaison_takes_precedence_over_lead(tmp_path: Path, capsys) -> None:
    root = _lead_team(tmp_path)
    s = Store(root)
    s.set_operator_facing("gamma")
    s.set_role("beta", "lead")
    rc = _run(["escalate", "--from", "alpha", "-m", "x"], root)
    out = capsys.readouterr()
    assert rc == 0
    assert "routing to the lead" not in out.err  # liaison wins


def test_escalate_to_override_beats_lead(tmp_path: Path, capsys) -> None:
    root = _lead_team(tmp_path)
    Store(root).set_role("beta", "lead")
    rc = _run(["escalate", "--from", "alpha", "--to", "gamma", "-m", "x"], root)
    assert rc == 0
    assert "routing to the lead" not in capsys.readouterr().err


def test_escalate_two_legacy_leads_exits_2(tmp_path: Path, capsys) -> None:
    root = _lead_team(tmp_path)
    s = Store(root)
    cfg = s.load_config()
    cfg["roles"] = {"beta": "lead", "gamma": "lead"}
    s._write_config(cfg)
    rc = _run(["escalate", "--from", "alpha", "-m", "x"], root)
    assert rc == 2  # sole_lead() ambiguous -> None -> refuse


# --- 0.24.0: roster set-role demote/promote notice (WP02, FR-005) ---------

def test_roster_set_role_lead_prints_demote_promote(tmp_path: Path, capsys) -> None:
    root = _lead_team(tmp_path)
    _run(["roster", "set-role", "alpha", "lead"], root)
    capsys.readouterr()
    rc = _run(["roster", "set-role", "beta", "lead"], root)
    out = capsys.readouterr().out
    assert rc == 0
    assert "demoted alpha, promoted beta to lead" in out


def test_roster_set_role_lead_idempotent_no_demote_line(tmp_path: Path, capsys) -> None:
    root = _lead_team(tmp_path)
    _run(["roster", "set-role", "alpha", "lead"], root)
    capsys.readouterr()
    _run(["roster", "set-role", "alpha", "lead"], root)
    assert "demoted" not in capsys.readouterr().out


# --- 0.24.0: wake wk- correlation id (WP02, FR-010/011) -------------------

def test_send_wake_mints_wk_id(tmp_path: Path, capsys) -> None:
    root = _lead_team(tmp_path)
    rc = _run(["send", "--from", "alpha", "--to", "beta", "--kind", "wake",
               "-m", "resume"], root)
    out = capsys.readouterr().out
    assert rc == 0
    assert "wk-" in out
    wake = [m for m in Store(root).valid_messages() if m.kind == "wake"][-1]
    assert (wake.meta or {}).get("request_id", "").startswith("wk-")


def test_send_wake_honors_explicit_request_id(tmp_path: Path) -> None:
    root = _lead_team(tmp_path)
    rc = _run(["send", "--from", "alpha", "--to", "beta", "--kind", "wake",
               "--meta", "request_id=mine-123", "-m", "resume"], root)
    assert rc == 0
    wake = [m for m in Store(root).valid_messages() if m.kind == "wake"][-1]
    assert (wake.meta or {}).get("request_id") == "mine-123"


# --- 0.24.0: owed-inbound pre-send warning (WP02, FR-012/013/014) ---------

def test_send_warns_when_owing_peer_a_proposal(tmp_path: Path, capsys) -> None:
    root = _lead_team(tmp_path)
    _run(["propose", "--from", "beta", "--to", "alpha", "-m", "a vs b"], root)
    capsys.readouterr()
    rc = _run(["send", "--from", "alpha", "--to", "beta", "--kind", "note",
               "-m", "ping"], root)
    out = capsys.readouterr()
    assert rc == 0  # send still succeeds
    assert "you owe beta an open decision-request" in out.err
    assert "proposal pp-" in out.err


def test_send_no_warning_when_replying_same_request_id(tmp_path: Path, capsys) -> None:
    root = _lead_team(tmp_path)
    _run(["propose", "--from", "beta", "--to", "alpha", "-m", "a vs b"], root)
    pp = [m for m in Store(root).valid_messages()
          if m.kind == "proposal"][-1].meta["request_id"]
    capsys.readouterr()
    _run(["send", "--from", "alpha", "--to", "beta", "--kind", "note",
          "--meta", f"request_id={pp}", "-m", "go with a"], root)
    assert "you owe" not in capsys.readouterr().err


def test_send_no_warning_for_non_decision_owed(tmp_path: Path, capsys) -> None:
    root = _lead_team(tmp_path)
    _run(["send", "--from", "beta", "--to", "alpha", "--kind", "question",
          "-m", "status?"], root)
    capsys.readouterr()
    _run(["send", "--from", "alpha", "--to", "beta", "--kind", "note",
          "-m", "hi"], root)
    assert "you owe" not in capsys.readouterr().err


def test_send_owed_warning_is_best_effort(tmp_path: Path, capsys, monkeypatch) -> None:
    root = _lead_team(tmp_path)

    def _boom(*a, **k):
        raise RuntimeError("derivation exploded")

    monkeypatch.setattr(cli.th, "derive_threads", _boom)
    rc = _run(["send", "--from", "alpha", "--to", "beta", "--kind", "note",
               "-m", "hi"], root)
    assert rc == 0  # the send is never disturbed by a derivation failure


def test_roster_add_with_role_lead_demotes_prior(tmp_path: Path) -> None:
    # review BLOCKING #1: the add path must honor at-most-one-lead too.
    root = _lead_team(tmp_path)
    _run(["roster", "set-role", "alpha", "lead"], root)
    _run(["roster", "add", "delta", "--role", "lead"], root)
    assert Store(root).sole_lead() == "delta"
