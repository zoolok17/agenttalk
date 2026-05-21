"""Tests for CLI subcommands via the parser, exercised through main().

Most CLI logic is identity resolution + roster validation. We invoke
`main(argv)` rather than subprocess-ing to keep tests fast and to
capture stderr/stdout via capsys.
"""

from __future__ import annotations

import json
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
    lines = [l for l in path.read_text(encoding="utf-8").split("\n") if l]
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
