"""Tests for `agenttalk reset` / Store.reset session lifecycle."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agenttalk import checkpoint, cli
from agenttalk.store import Store, validate_agent_name


def _run(argv: list[str], root: Path) -> int:
    try:
        return cli.main(["--root", str(root), *argv])
    except SystemExit as e:
        return 0 if e.code is None else int(e.code)


# ----------------------------------------------------------- Store.reset

def test_reset_clears_messages_cursors_heartbeats(store_root: Path) -> None:
    s = Store(store_root)
    # Build up some state
    msg = s.send(sender="alpha", recipient="beta", body="first")
    s.advance_cursor("beta", msg.id)
    s.write_heartbeat("alpha")
    s.write_heartbeat("beta")
    assert s.all_messages()
    assert s.cursor("beta") != ""
    assert s.read_heartbeat("alpha") is not None

    cfg, archive_path = s.reset()
    assert archive_path is None
    # State is gone
    assert s.all_messages() == []
    assert s.cursor("beta") == ""
    assert s.read_heartbeat("alpha") is None
    # Config preserved (roster); session_id is new
    assert cfg["agents"] == ["alpha", "beta"]
    assert cfg["session_id"]  # truthy


def test_reset_preserves_session_transcripts_by_default(store_root: Path) -> None:
    """Regression for v0.4.0 iter-1 blocker: default reset must NOT
    silently delete exported transcripts under .agenttalk/sessions/.
    Those are user-visible historical artifacts, not active bus state.
    """
    from agenttalk import transcript as tx
    s = Store(store_root)
    s.send(sender="alpha", recipient="beta", body="archive me")
    transcript_path = tx.export(s, fmt="md")
    assert transcript_path.exists()
    s.reset()  # default (no --archive)
    # Transcript file must still be there
    assert transcript_path.exists(), (
        "default reset deleted an exported transcript — that's a "
        "regression of the v0.4.0 iter-1 blocker"
    )


def test_reset_archive_moves_transcripts_too(store_root: Path) -> None:
    """--archive moves everything including past transcripts into
    archived/<session_id>/sessions/, so users can recover the whole
    prior session."""
    from agenttalk import transcript as tx
    s = Store(store_root)
    s.send(sender="alpha", recipient="beta", body="hi")
    tx.export(s, fmt="md")
    _, archive_path = s.reset(archive=True)
    assert archive_path is not None
    archived_transcripts = list((archive_path / "sessions").glob("transcript-*"))
    assert len(archived_transcripts) == 1


@pytest.mark.parametrize("archive", [False, True])
def test_reset_clears_or_archives_checkpoints(
    store_root: Path,
    archive: bool,
) -> None:
    store = Store(store_root)
    payload = {
        "agent": "alpha",
        "session_id": "session-before-reset",
    }
    checkpoint.save_checkpoint(store, "alpha", payload)

    _, archive_path = store.reset(archive=archive)

    assert not (store.dir / "checkpoints").exists()
    if archive:
        assert archive_path is not None
        archived = archive_path / "checkpoints" / "alpha.json"
        assert json.loads(archived.read_text(encoding="utf-8")) == payload
    else:
        assert archive_path is None


def test_reset_bumps_session_id(store_root: Path) -> None:
    s = Store(store_root)
    old_session = s.load_config()["session_id"]
    new_cfg, _ = s.reset()
    assert new_cfg["session_id"] != old_session


def test_reset_preserves_avatar_preferences(store_root: Path) -> None:
    s = Store(store_root)
    s.set_avatar("alpha", "codex-dev")
    s.set_operator_avatar("operator")

    cfg, _ = s.reset()

    assert cfg["avatars"] == {"alpha": "codex-dev", "operator": "operator"}
    assert s.load_config()["avatars"] == cfg["avatars"]


def test_reset_with_archive_preserves_old_state(store_root: Path) -> None:
    s = Store(store_root)
    s.send(sender="alpha", recipient="beta", body="archived me")
    old_session = s.load_config()["session_id"]
    cfg, archive_path = s.reset(archive=True)
    assert archive_path is not None
    assert archive_path.exists()
    assert archive_path.name == old_session
    # Old message survived under the archive
    archived_messages = list((archive_path / "messages").glob("*.json"))
    assert len(archived_messages) == 1
    assert json.loads(archived_messages[0].read_text(encoding="utf-8"))["body"] == "archived me"
    # And the live bus is clean
    assert s.all_messages() == []


def test_reset_recreates_empty_cursor_files(store_root: Path) -> None:
    """Right after reset the bus must be usable — fresh empty cursors
    exist for each roster agent so status displays correctly."""
    s = Store(store_root)
    s.reset()
    for agent in s.load_config()["agents"]:
        cursor_file = s.state_dir / (
            validate_agent_name(agent) + ".cursor"
        )
        assert cursor_file.exists()
        assert cursor_file.read_text(encoding="utf-8") == ""


def test_reset_on_uninitialized_store_raises(tmp_path: Path) -> None:
    s = Store(tmp_path)
    with pytest.raises(FileNotFoundError, match="not initialized"):
        s.reset()


def test_reset_twice_with_archive_does_not_clobber_first_archive(
    store_root: Path,
) -> None:
    """Two resets with the same session_id (shouldn't happen in
    practice but guard against it) must not destroy the earlier
    archive."""
    s = Store(store_root)
    s.send(sender="alpha", recipient="beta", body="round1")
    cfg1, archive_path1 = s.reset(archive=True)
    s.send(sender="alpha", recipient="beta", body="round2")
    # Hand-set session_id back so the second archive collides
    cfg = s.load_config()
    cfg["session_id"] = archive_path1.name
    s.config_path.write_text(json.dumps(cfg), encoding="utf-8")
    _, archive_path2 = s.reset(archive=True)
    assert archive_path2 == archive_path1  # same parent
    # Both archives still exist
    msg_dirs = list(archive_path1.iterdir())
    assert any(d.name == "messages" for d in msg_dirs)
    assert any("messages." in d.name for d in msg_dirs)  # the timestamped one


# ----------------------------------------------------------- CLI

def test_cmd_reset_default_deletes_and_reports(
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    s = Store(store_root)
    s.send(sender="alpha", recipient="beta", body="goodbye")
    rc = _run(["reset"], store_root)
    assert rc == 0
    out = capsys.readouterr().out
    assert "previous session deleted" in out
    assert "new session_id:" in out
    assert s.all_messages() == []


def test_cmd_reset_archive_flag_preserves_state(
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    s = Store(store_root)
    s.send(sender="alpha", recipient="beta", body="preserve me")
    rc = _run(["reset", "--archive"], store_root)
    assert rc == 0
    out = capsys.readouterr().out
    assert "archived previous session to:" in out
    # Archive dir exists with one message
    archived_msgs = list((store_root / ".agenttalk" / "archived").glob("*/messages/*.json"))
    assert len(archived_msgs) == 1


def test_cmd_reset_on_uninitialized_returns_2(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """No `.agenttalk/` in this tmp dir; reset should exit 2 with the
    standard "not initialized" message."""
    rc = _run(["reset"], tmp_path)
    assert rc == 2
    err = capsys.readouterr().err
    assert "not initialized" in err


# ----------------------------------------- session_id traversal blocker

def test_reset_archive_rejects_traversing_session_id(store_root: Path) -> None:
    """Regression for v0.4.0 iter-1 blocker (Codex). A corrupted
    config with `session_id="..\\..\\escaped-archive"` used to make
    `reset --archive` write outside `.agenttalk/archived/`. Now the
    corrupt config is rejected at load time.
    """
    import json as _json
    s = Store(store_root)
    cfg = _json.loads(s.config_path.read_text(encoding="utf-8"))
    cfg["session_id"] = "..\\..\\escaped-archive"
    s.config_path.write_text(_json.dumps(cfg), encoding="utf-8")
    with pytest.raises(ValueError, match="corrupt config"):
        s.reset(archive=True)
    # And nothing was archived outside the archive root
    escaped_path = store_root.parent / "escaped-archive"
    assert not escaped_path.exists()
    assert not (store_root / "escaped-archive").exists()


@pytest.mark.parametrize("bad_session_id", [
    "../escape",
    "..\\escape",
    "ok/with/slash",
    "name with space",
    "name'with'quote",
    "20260521T120000Z\nnewline",
    "",
])
def test_load_config_rejects_unsafe_session_ids(
    store_root: Path, bad_session_id: str,
) -> None:
    """Any non-conforming session_id should fail load-time validation
    rather than smuggle a path component through to reset."""
    import json as _json
    s = Store(store_root)
    cfg = _json.loads(s.config_path.read_text(encoding="utf-8"))
    cfg["session_id"] = bad_session_id
    s.config_path.write_text(_json.dumps(cfg), encoding="utf-8")
    with pytest.raises(ValueError, match="corrupt config"):
        s.load_config()


@pytest.mark.parametrize("good_session_id", [
    "20260521T120000Z",          # old 0.3.x format
    "20260521T120000-AbCdZ",     # new 0.4.x format with suffix
    "20260521T235959-0z9AZ",
])
def test_load_config_accepts_both_old_and_new_session_id_formats(
    store_root: Path, good_session_id: str,
) -> None:
    """Backwards-compat: 0.3.x configs in the wild must still load."""
    import json as _json
    s = Store(store_root)
    cfg = _json.loads(s.config_path.read_text(encoding="utf-8"))
    cfg["session_id"] = good_session_id
    s.config_path.write_text(_json.dumps(cfg), encoding="utf-8")
    loaded = s.load_config()
    assert loaded["session_id"] == good_session_id


# ----------------------------------------------- init --force preservation

def test_init_force_does_NOT_clear_messages(store_root: Path) -> None:
    """The whole point of the new `--force` doc + the existence of
    reset: `init --force` rewrites the config but keeps messages.
    Users who want a clean slate must call reset explicitly."""
    s = Store(store_root)
    s.send(sender="alpha", recipient="beta", body="survive init --force")
    s.init(["gamma", "delta"], force=True)
    # Messages survived
    assert len(s.all_messages()) == 1
    # But config changed
    assert s.load_config()["agents"] == ["gamma", "delta"]


def test_init_force_help_text_points_at_reset(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """The --force help text must mention reset so users discover
    the right command for nuking state. Argparse line-wraps based
    on terminal width, so normalize whitespace before checking."""
    with pytest.raises(SystemExit):
        cli.main(["init", "--help"])
    out = capsys.readouterr().out
    normalized = " ".join(out.split())  # collapse all whitespace
    assert "agenttalk reset" in normalized
    assert "Does NOT clear" in normalized
