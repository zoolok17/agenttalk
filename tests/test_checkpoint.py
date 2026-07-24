"""Checkpoint-before-compact command coverage (#71).

The hook fixture is a captured Claude Code ``PreCompact`` stdin payload. Tests
feed its raw bytes through ``sys.stdin.buffer`` so the hook boundary, rather
than a hand-built Python mapping, is exercised.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from agenttalk import capacity as capmod
from agenttalk import checkpoint, cli
from agenttalk.store import Store


FIXTURE = Path(__file__).parent / "fixtures" / "claude-precompact-hook-captured.json"
CONTEXT_FIXTURE = (
    Path(__file__).parent / "fixtures" / "claude-context-sidecar-captured.json"
)


class _BinaryStdin:
    def __init__(self, data: bytes) -> None:
        self.buffer = io.BytesIO(data)


def _run(root: Path, *argv: str) -> int:
    return cli.main(["--root", str(root), "checkpoint", *argv])


def _observed_capacity(agent: str = "alpha") -> capmod.CapacitySnapshot:
    return capmod.CapacitySnapshot(
        source_agent=agent,
        observed_at="2026-07-24T10:00:00Z",
        source="claude_statusline",
        primary_used_percent=None,
        primary_resets_at=None,
        primary_window_minutes=None,
        secondary_used_percent=None,
        secondary_resets_at=None,
        secondary_window_minutes=None,
        context_used_percent=78.4,
        context_window_size=1_000_000,
        context_tokens=784_000,
    )


def _payload(*, saved_at: str = "2026-07-24T10:00:00Z") -> dict:
    return {
        "agent": "alpha",
        "session_id": "session-1",
        "trigger": "auto",
        "saved_at": saved_at,
        "context": {
            "pct": 78.4,
            "limit": 1_000_000,
            "used": 784_000,
            "source": "sidecar",
        },
        "git": {
            "head": "abcdef1234567890",
            "branch": "master",
            "dirty_files": 2,
        },
        "bus": {
            "unread": 3,
            "owed_out": [
                {"id": "q-out", "to": "beta", "kind": "question", "age": "2m"},
            ],
            "owed_in": [
                {"id": "q-in", "from": "beta", "kind": "question"},
            ],
            "in_flight_threads": ["q-in", "q-out"],
        },
        "reload_pointers": [
            "memory/dashboard-control-plane.md",
            "memory/MEMORY.md",
        ],
    }


def test_checkpoint_save_uses_captured_hook_payload_and_shared_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    store = Store(tmp_path)
    store.init(["alpha", "beta"])
    store.send(
        sender="beta",
        recipient="alpha",
        body="answer this",
        kind="question",
        meta={"request_id": "q-in"},
    )
    store.send(
        sender="alpha",
        recipient="beta",
        body="review this",
        kind="question",
        meta={"request_id": "q-out"},
    )
    captured = json.loads(FIXTURE.read_text(encoding="utf-8"))
    sidecar_dir = tmp_path / "hook-temp"
    sidecar_dir.mkdir()
    (sidecar_dir / f"cc-ctx-{captured['session_id']}.json").write_bytes(
        CONTEXT_FIXTURE.read_bytes(),
    )
    monkeypatch.setattr(capmod.tempfile, "gettempdir", lambda: str(sidecar_dir))
    monkeypatch.setattr(
        checkpoint,
        "collect_git_state",
        lambda _root: {
            "head": "0123456789abcdef",
            "branch": "feat/checkpoint",
            "dirty_files": 4,
        },
    )
    monkeypatch.setattr("sys.stdin", _BinaryStdin(FIXTURE.read_bytes()))

    assert _run(tmp_path, "save", "--hook", "--for", "alpha") == 0
    assert capsys.readouterr() == ("", "")

    saved = json.loads(
        (tmp_path / ".agenttalk" / "checkpoints" / "alpha.json").read_text(
            encoding="utf-8",
        )
    )
    assert saved["agent"] == "alpha"
    assert saved["session_id"] == captured["session_id"]
    assert saved["trigger"] == captured["trigger"]
    assert saved["context"] == {
        "pct": 12.1,
        "limit": 1_000_000,
        "used": 120_729,
        "source": "sidecar",
    }
    assert saved["git"] == {
        "head": "0123456789abcdef",
        "branch": "feat/checkpoint",
        "dirty_files": 4,
    }
    assert saved["bus"]["unread"] == 1
    assert saved["bus"]["owed_in"] == [
        {"id": "q-in", "from": "beta", "kind": "question"},
    ]
    assert saved["bus"]["owed_out"][0]["id"] == "q-out"
    assert saved["bus"]["owed_out"][0]["to"] == "beta"
    assert saved["bus"]["in_flight_threads"] == ["q-in", "q-out"]
    assert saved["reload_pointers"] == [
        "memory/dashboard-control-plane.md",
        "memory/MEMORY.md",
    ]


def test_checkpoint_save_without_git_or_sidecar_is_null_not_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Store(tmp_path).init(["alpha", "beta"])
    monkeypatch.setattr(
        checkpoint.capmod,
        "read_local",
        lambda agent, **_kwargs: capmod.CapacitySnapshot.unknown(agent),
    )

    assert _run(tmp_path, "save", "--for", "alpha") == 0
    saved = checkpoint.read_checkpoint(Store(tmp_path), "alpha")
    assert saved is not None
    assert saved["context"] == {
        "pct": None,
        "limit": None,
        "used": None,
        "source": None,
    }
    assert saved["git"] == {
        "head": None,
        "branch": None,
        "dirty_files": None,
    }


def test_claude_context_sidecar_reader_rejects_bad_identity_and_values(
    tmp_path: Path,
) -> None:
    assert (
        capmod.read_claude_context_sidecar(
            "alpha",
            session_id="../escape",
            temp_dir=tmp_path,
        )
        is None
    )
    invalid = tmp_path / "cc-ctx-session-invalid.json"
    invalid.write_text(
        json.dumps(
            {
                "context_limit": 1_000_000.5,
                "context_used": 100.25,
                "context_pct": 10,
            }
        ),
        encoding="utf-8",
    )
    assert (
        capmod.read_claude_context_sidecar(
            "alpha",
            session_id="session-invalid",
            temp_dir=tmp_path,
        )
        is None
    )
    invalid.write_text(
        json.dumps(
            {
                "context_limit": 1_000_000,
                "context_used": 100,
                "context_pct": 101,
            }
        ),
        encoding="utf-8",
    )
    assert (
        capmod.read_claude_context_sidecar(
            "alpha",
            session_id="session-invalid",
            temp_dir=tmp_path,
        )
        is None
    )


def test_checkpoint_save_preserves_partial_snapshot_on_malformed_bus_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Store(tmp_path).init(["alpha", "beta"])
    monkeypatch.setattr(
        checkpoint,
        "collect_bus_state",
        lambda *_args: (_ for _ in ()).throw(ValueError("malformed state")),
    )
    monkeypatch.setattr(
        checkpoint.capmod,
        "read_local",
        lambda agent, **_kwargs: _observed_capacity(agent),
    )

    assert _run(tmp_path, "save", "--for", "alpha") == 0
    saved = checkpoint.read_checkpoint(Store(tmp_path), "alpha")
    assert saved is not None
    assert saved["context"]["pct"] == 78.4
    assert saved["bus"] == {
        "unread": None,
        "owed_out": [],
        "owed_in": [],
        "in_flight_threads": [],
    }


@pytest.mark.parametrize("stdin_bytes", [b"", b"{not-json", b'{"trigger":"\xff"}'])
def test_checkpoint_save_hook_tolerates_bad_stdin(
    tmp_path: Path,
    stdin_bytes: bytes,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    Store(tmp_path).init(["alpha", "beta"])
    monkeypatch.setattr("sys.stdin", _BinaryStdin(stdin_bytes))

    assert _run(tmp_path, "save", "--hook", "--for", "alpha") == 0
    assert capsys.readouterr() == ("", "")
    saved = checkpoint.read_checkpoint(Store(tmp_path), "alpha")
    assert saved is not None
    assert saved["trigger"] == "manual"


def test_checkpoint_save_hook_is_silent_and_fail_soft_on_unresolved_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    Store(tmp_path).init(["alpha", "beta"])
    monkeypatch.setattr("sys.stdin", _BinaryStdin(FIXTURE.read_bytes()))

    assert _run(tmp_path, "save", "--hook") == 0
    assert capsys.readouterr() == ("", "")
    assert not (tmp_path / ".agenttalk" / "checkpoints" / "alpha.json").exists()


def test_checkpoint_save_hook_logs_internal_error_without_blocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    Store(tmp_path).init(["alpha", "beta"])
    monkeypatch.setattr("sys.stdin", _BinaryStdin(FIXTURE.read_bytes()))
    monkeypatch.setattr(
        checkpoint,
        "save_checkpoint",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    assert _run(tmp_path, "save", "--hook", "--for", "alpha") == 0
    assert capsys.readouterr() == ("", "")
    error_log = tmp_path / ".agenttalk" / "checkpoints" / "checkpoint-errors.log"
    assert "disk full" in error_log.read_text(encoding="utf-8")


def test_checkpoint_resume_hook_emits_exact_sessionstart_injection(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    store = Store(tmp_path)
    store.init(["alpha", "beta"])
    checkpoint.save_checkpoint(store, "alpha", _payload())

    assert _run(tmp_path, "resume", "--hook", "--for", "alpha") == 0
    expected_context = (
        "Checkpoint reload for alpha: trigger=auto; "
        "saved_at=2026-07-24T10:00:00Z; "
        "git=master@abcdef1234567890 (dirty_files=2); "
        "context=78.4% (784000/1000000, source=sidecar); "
        "bus=unread:3, owed_in:1 [q-in], owed_out:1 [q-out], "
        "in_flight:[q-in,q-out]. Before continuing, re-read "
        "memory/dashboard-control-plane.md + memory/MEMORY.md."
    )
    expected = json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": expected_context,
            }
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    assert capsys.readouterr() == (expected + "\n", "")


def test_checkpoint_resume_hook_without_checkpoint_emits_empty_context(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    Store(tmp_path).init(["alpha", "beta"])

    assert _run(tmp_path, "resume", "--hook", "--for", "alpha") == 0
    assert json.loads(capsys.readouterr().out) == {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "",
        }
    }


def test_checkpoint_resume_hook_treats_corrupt_latest_as_absent(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    Store(tmp_path).init(["alpha", "beta"])
    latest = tmp_path / ".agenttalk" / "checkpoints" / "alpha.json"
    latest.parent.mkdir(parents=True)
    latest.write_bytes(b"\xff{torn")

    assert _run(tmp_path, "resume", "--hook", "--for", "alpha") == 0
    assert json.loads(capsys.readouterr().out) == {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "",
        }
    }


def test_checkpoint_latest_wins_and_history_is_capped(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.init(["alpha", "beta"])

    for index in range(checkpoint.HISTORY_LIMIT + 3):
        payload = _payload(saved_at=f"2026-07-24T10:00:{index:02d}Z")
        payload["session_id"] = f"session-{index}"
        checkpoint.save_checkpoint(store, "alpha", payload)

    latest = checkpoint.read_checkpoint(store, "alpha")
    assert latest is not None
    assert latest["session_id"] == f"session-{checkpoint.HISTORY_LIMIT + 2}"
    history = list(
        (tmp_path / ".agenttalk" / "checkpoints" / "history" / "alpha").glob(
            "*.json",
        )
    )
    assert len(history) == checkpoint.HISTORY_LIMIT
    archived_sessions = {
        json.loads(path.read_text(encoding="utf-8"))["session_id"]
        for path in history
    }
    assert "session-0" not in archived_sessions
    assert f"session-{checkpoint.HISTORY_LIMIT + 1}" in archived_sessions


def test_checkpoint_show_json_reads_latest(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    store = Store(tmp_path)
    store.init(["alpha", "beta"])
    checkpoint.save_checkpoint(store, "alpha", _payload())

    assert _run(tmp_path, "show", "--for", "alpha", "--json") == 0
    assert json.loads(capsys.readouterr().out) == _payload()


def test_checkpoint_hook_fallback_identity_is_hook_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    Store(tmp_path).init(["alpha", "beta"])
    monkeypatch.setattr("sys.stdin", _BinaryStdin(FIXTURE.read_bytes()))

    assert _run(tmp_path, "save", "--hook", "--fallback-for", "alpha") == 0
    assert checkpoint.read_checkpoint(Store(tmp_path), "alpha") is not None
    assert capsys.readouterr() == ("", "")

    assert _run(tmp_path, "save", "--fallback-for", "alpha") == 2
    assert "--fallback-for requires --hook" in capsys.readouterr().err
