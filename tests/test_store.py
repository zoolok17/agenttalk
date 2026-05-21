"""Tests for the on-disk Store: init, send, cursors, heartbeats."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agenttalk.store import (
    Store,
    find_root,
    validate_agent_name,
    validate_agent_roster,
)


# --------------------------------------------------------------------- init

def test_init_creates_expected_layout(tmp_path: Path) -> None:
    s = Store(tmp_path)
    cfg = s.init(["alpha", "beta"])
    assert (tmp_path / ".agenttalk").is_dir()
    assert (tmp_path / ".agenttalk" / "messages").is_dir()
    assert (tmp_path / ".agenttalk" / "state").is_dir()
    assert (tmp_path / ".agenttalk" / "sessions").is_dir()
    assert (tmp_path / ".agenttalk" / "config.json").is_file()
    assert cfg["agents"] == ["alpha", "beta"]
    assert "created_at" in cfg
    assert "session_id" in cfg


def test_init_is_idempotent_without_force(tmp_path: Path) -> None:
    s = Store(tmp_path)
    cfg1 = s.init(["a", "b"])
    cfg2 = s.init(["a", "b"])  # second call should not overwrite
    assert cfg1 == cfg2


def test_init_with_force_overwrites_config(tmp_path: Path) -> None:
    s = Store(tmp_path)
    cfg1 = s.init(["a", "b"])
    cfg2 = s.init(["c", "d"], force=True)
    assert cfg2["agents"] == ["c", "d"]
    assert cfg2["session_id"] != cfg1["session_id"] or cfg2["created_at"] != cfg1["created_at"]


# --------------------------------------------------------------------- send

def test_send_writes_a_message_file(store: Store) -> None:
    msg = store.send(sender="alpha", recipient="beta", body="hi")
    p = store.messages_dir / f"{msg.id}.json"
    assert p.is_file()
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["from"] == "alpha"
    assert data["to"] == "beta"
    assert data["body"] == "hi"
    assert data["kind"] == "message"


def test_send_rejects_unknown_sender(store: Store) -> None:
    with pytest.raises(ValueError):
        store.send(sender="ghost", recipient="beta", body="x")


def test_send_rejects_unknown_recipient(store: Store) -> None:
    with pytest.raises(ValueError):
        store.send(sender="alpha", recipient="ghost", body="x")


def test_messages_ids_are_lexicographic(store: Store) -> None:
    ids = []
    for i in range(5):
        ids.append(store.send(sender="alpha", recipient="beta", body=str(i)).id)
    assert ids == sorted(ids)


# ------------------------------------------------------------ recv / cursor

def test_recv_returns_messages_for_agent(store: Store) -> None:
    store.send(sender="alpha", recipient="beta", body="one")
    store.send(sender="alpha", recipient="beta", body="two")
    store.send(sender="beta", recipient="alpha", body="reply")
    bound_for_beta = store.messages_for("beta")
    assert [m.body for m in bound_for_beta] == ["one", "two"]
    bound_for_alpha = store.messages_for("alpha")
    assert [m.body for m in bound_for_alpha] == ["reply"]


def test_cursor_advance_filters_unread(store: Store) -> None:
    m1 = store.send(sender="alpha", recipient="beta", body="one")
    m2 = store.send(sender="alpha", recipient="beta", body="two")
    assert len(store.unread_for("beta")) == 2
    store.advance_cursor("beta", m1.id)
    unread = store.unread_for("beta")
    assert len(unread) == 1
    assert unread[0].id == m2.id


def test_advance_cursor_never_moves_backwards(store: Store) -> None:
    m1 = store.send(sender="alpha", recipient="beta", body="one")
    m2 = store.send(sender="alpha", recipient="beta", body="two")
    store.advance_cursor("beta", m2.id)
    store.advance_cursor("beta", m1.id)  # should be a no-op
    assert store.cursor("beta") == m2.id


# --------------------------------------------------------------- heartbeat

def test_heartbeat_round_trip(store: Store) -> None:
    assert store.read_heartbeat("alpha") is None
    store.write_heartbeat("alpha")
    hb = store.read_heartbeat("alpha")
    assert hb is not None
    age = (datetime.now(timezone.utc) - hb).total_seconds()
    assert age < 5  # just written


def test_heartbeat_rejects_naive_timestamp(store: Store) -> None:
    """A malformed (timezone-less) heartbeat file must NOT crash callers
    via aware-vs-naive datetime subtraction. read_heartbeat returns None."""
    p = store.state_dir / "alpha.heartbeat"
    p.write_text("2026-05-20T22:20:00", encoding="utf-8")  # no tz
    assert store.read_heartbeat("alpha") is None


def test_heartbeat_accepts_z_form(store: Store) -> None:
    p = store.state_dir / "alpha.heartbeat"
    p.write_text("2026-05-20T22:20:00.000000Z", encoding="utf-8")
    hb = store.read_heartbeat("alpha")
    assert hb is not None
    assert hb.tzinfo is not None


def test_heartbeat_returns_none_on_garbage(store: Store) -> None:
    p = store.state_dir / "alpha.heartbeat"
    p.write_text("not a date", encoding="utf-8")
    assert store.read_heartbeat("alpha") is None


# --------------------------------------------------------------- find_root

def test_find_root_walks_up_to_locate_agenttalk(tmp_path: Path) -> None:
    root = tmp_path / "project"
    nested = root / "src" / "deep" / "path"
    nested.mkdir(parents=True)
    Store(root).init(["a", "b"])
    found = find_root(nested)
    assert found == root.resolve()


def test_find_root_falls_back_to_start_when_no_store(tmp_path: Path) -> None:
    found = find_root(tmp_path)
    assert found == tmp_path.resolve()


# ----------------------------------------------------- agent name validation

@pytest.mark.parametrize("name", [
    "claude", "codex", "claude-a", "claude_a", "claude.dev",
    "a", "A1", "agent-1.0_pre",
    "a" * 64,                       # max length exactly
])
def test_validate_agent_name_accepts_safe_identifiers(name: str) -> None:
    assert validate_agent_name(name) == name


@pytest.mark.parametrize("name", [
    "",                              # empty
    " ",                             # whitespace
    " claude",                       # leading space
    "claude ",                       # trailing space
    "-claude",                       # leading punctuation
    ".claude",                       # leading dot (would be a hidden filename)
    "_claude",                       # leading underscore
    "claude/dev",                    # forward slash
    "claude\\dev",                   # backslash
    "..",                            # path traversal
    "../outside",                    # path traversal (Codex's repro)
    "..\\..\\outside",               # Windows path traversal
    "a" * 65,                        # too long
    "claude'a",                      # quote
    'claude"a',                      # double-quote
    "claude\na",                     # newline
    "claude\ta",                     # tab
    "claude:dev",                    # colon (drive separator on Windows)
    "claude\n",                      # trailing newline (Python's $ anchor would miss this)
    "claude\r\n",                    # trailing CRLF
    "claude\r",                      # trailing CR
    "\nclaude",                      # leading newline
])
def test_validate_agent_name_rejects_unsafe_names(name: str) -> None:
    with pytest.raises(ValueError):
        validate_agent_name(name)


def test_validate_agent_roster_rejects_duplicates() -> None:
    with pytest.raises(ValueError, match="appears more than once"):
        validate_agent_roster(["alpha", "alpha"])


@pytest.mark.parametrize("names", [
    ["Alpha", "alpha"],
    ["CODex", "codex"],
    ["claude", "CLAUDE"],
    ["claude-a", "Claude-A"],
])
def test_validate_agent_roster_rejects_case_only_duplicates(names: list[str]) -> None:
    """Regression for Codex's iter-1 blocker: case-insensitive
    filesystems (NTFS, default macOS) alias `Alpha.cursor` and
    `alpha.cursor` to the same path. Two logical agents would share
    state. Reject at roster-validation time.
    """
    with pytest.raises(ValueError, match="differ by case"):
        validate_agent_roster(names)


def test_init_rejects_path_traversal_agent_name(tmp_path: Path) -> None:
    """The path-traversal blocker from the v0.2.0 review.

    Before the fix, `init --agents 'alpha,..\\..\\outside'` created
    `outside.cursor` outside `.agenttalk/state/`. Now it must raise.
    """
    s = Store(tmp_path)
    with pytest.raises(ValueError):
        s.init(["alpha", "..\\..\\outside"])
    # And no escaped file was created
    assert not (tmp_path.parent / "outside.cursor").exists()


def test_load_config_rejects_unsafe_agent_in_existing_file(tmp_path: Path) -> None:
    """A malformed config on disk must not smuggle unsafe names through."""
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    # Hand-corrupt the config to inject a path-traversal name
    import json as _json
    cfg = _json.loads(s.config_path.read_text(encoding="utf-8"))
    cfg["agents"] = ["alpha", "../escape"]
    s.config_path.write_text(_json.dumps(cfg), encoding="utf-8")
    with pytest.raises(ValueError, match="corrupt config"):
        s.load_config()


def test_load_config_rejects_trailing_newline_in_agent_name(tmp_path: Path) -> None:
    """Regression for the Python `$` regex anchor gotcha: `"claude\\n"`
    would pass `re.match(...$)` (because `$` matches before a final
    newline) and end up as a real filename like `claude\\n.cursor`.
    Caught in v0.2.1 iteration-2 review. Validator must use `\\Z`."""
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    import json as _json
    cfg = _json.loads(s.config_path.read_text(encoding="utf-8"))
    cfg["agents"] = ["alpha", "beta\n"]
    s.config_path.write_text(_json.dumps(cfg), encoding="utf-8")
    with pytest.raises(ValueError, match="corrupt config"):
        s.load_config()


def test_load_config_rejects_non_list_agents(tmp_path: Path) -> None:
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    import json as _json
    cfg = _json.loads(s.config_path.read_text(encoding="utf-8"))
    cfg["agents"] = "alpha,beta"  # str, not list
    s.config_path.write_text(_json.dumps(cfg), encoding="utf-8")
    with pytest.raises(ValueError, match="must be a list"):
        s.load_config()
