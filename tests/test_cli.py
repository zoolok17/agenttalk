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


def _run_expect_exit(argv: list[str], root: Path, code: int) -> SystemExit:
    """Run main() and assert it raises SystemExit with the given code.
    Returns the exception so the caller can inspect anything else."""
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--root", str(root), *argv])
    assert excinfo.value.code == code, (
        f"expected SystemExit({code}), got {excinfo.value.code!r}"
    )
    return excinfo.value


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
