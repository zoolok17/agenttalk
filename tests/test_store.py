"""Tests for the on-disk Store: init, send, cursors, heartbeats."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agenttalk.store import (
    COMPOSING_INTENT_STALE_SECONDS,
    CONTROL_KINDS,
    KNOWN_KINDS,
    OPENER_KINDS,
    Store,
    find_root,
    find_stores_upward,
    validate_agent_name,
    validate_agent_roster,
    validate_rescind,
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


def test_new_id_is_strictly_monotonic_under_load() -> None:
    """v0.7.1 regression: on fast hardware two _new_id() calls land
    in the same microsecond; the random 4-char suffix is NOT
    monotonic, so two messages would have ids that lexicographically
    sort opposite to send order — breaking the messages_for/dashboard
    chronology invariant. Force the timestamp to be strictly greater
    than the previous one per process to close it."""
    from agenttalk.store import _new_id
    ids = [_new_id() for _ in range(2000)]
    assert ids == sorted(ids), (
        "ids were not strictly monotonic under tight-loop generation; "
        "messages_for and the web dashboard would reorder same-microsecond "
        "messages relative to send order"
    )
    assert len(set(ids)) == len(ids), "duplicate ids generated"


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


# ======================================================================
# 0.14.0 engine foundations (WP01)
# ======================================================================

def _team_store(tmp_path: Path, agents: list[str]) -> Store:
    s = Store(tmp_path)
    s.init(agents)
    return s


# ------------------------------------------------------------- kinds (T001)

def test_rescind_is_a_known_kind() -> None:
    assert "rescind" in KNOWN_KINDS


def test_control_kinds_unchanged() -> None:
    # C-003 regression guard: rescind must stay transcript-visible.
    # Literal assertion on purpose - any addition to the control plane
    # must consciously update this test.
    assert CONTROL_KINDS == frozenset({"composing"})


def test_opener_kinds_single_source() -> None:
    # threads.py re-exports the store constant - same object, not a copy.
    from agenttalk import threads
    assert threads.OPENER_KINDS is OPENER_KINDS


def test_send_rescind_kind_accepted(store: Store) -> None:
    m = store.send(sender="alpha", recipient="beta", kind="rescind",
                   body="changed my mind", meta={"request_id": "rq-x"})
    assert m.kind == "rescind"


# --------------------------------------------------- validate_rescind (T001)

def test_validate_rescind_happy_path(store: Store) -> None:
    opener = store.send(sender="alpha", recipient="beta", kind="question",
                        body="fire?", meta={"request_id": "q-1"})
    openers = validate_rescind(store, "alpha", "q-1")
    assert [m.id for m in openers] == [opener.id]
    assert openers[0].recipient == "beta"


def test_validate_rescind_rejects_non_requester(store: Store) -> None:
    store.send(sender="alpha", recipient="beta", kind="question",
               body="fire?", meta={"request_id": "q-1"})
    with pytest.raises(ValueError, match="only the requester"):
        validate_rescind(store, "beta", "q-1")


def test_validate_rescind_rejects_unknown_rid(store: Store) -> None:
    with pytest.raises(ValueError, match="no thread with request_id"):
        validate_rescind(store, "alpha", "q-nope")


def test_validate_rescind_rejects_unknown_target_msg_id(store: Store) -> None:
    store.send(sender="alpha", recipient="beta", kind="question",
               body="fire?", meta={"request_id": "q-1"})
    with pytest.raises(ValueError, match="not a message in thread"):
        validate_rescind(store, "alpha", "q-1",
                         target_msg_id="20990101-000000-000000-XXXX")


def test_validate_rescind_thread_without_opener(store: Store) -> None:
    # A bare correlated message (orphan reply) - nothing to rescind.
    store.send(sender="alpha", recipient="beta", kind="message",
               body="orphan", meta={"request_id": "q-orphan"})
    with pytest.raises(ValueError, match="no visible opener"):
        validate_rescind(store, "alpha", "q-orphan")


def test_validate_rescind_broadcast_returns_all_opener_copies(tmp_path: Path) -> None:
    s = _team_store(tmp_path, ["lead", "w1", "w2"])
    for r in ("w1", "w2"):
        s.send(sender="lead", recipient=r, kind="question", body="status?",
               meta={"request_id": "b-1", "broadcast_id": "b-1", "audience": "all"})
    openers = validate_rescind(s, "lead", "b-1")
    assert sorted(m.recipient for m in openers) == ["w1", "w2"]
    with pytest.raises(ValueError, match="only the requester"):
        validate_rescind(s, "w1", "b-1")


# ------------------------------------- root resolution + scanner (T002)

def test_find_root_env_pin_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pinned = tmp_path / "pinned"
    pinned.mkdir()
    Store(pinned).init(["alpha", "beta"])
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setenv("AGENTTALK_ROOT", str(pinned))
    # env wins even when an upward walk from `elsewhere` would find nothing
    assert find_root(elsewhere) == pinned.resolve()


def test_find_root_env_pin_returned_even_without_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A typo'd pin must fail LOUDLY downstream (must-exist check), never
    # silently fall back to the walk - that would be a new silent fork.
    ghost = tmp_path / "ghost"
    monkeypatch.setenv("AGENTTALK_ROOT", str(ghost))
    assert find_root(tmp_path) == ghost.resolve()


def test_find_root_walk_unchanged_without_env(tmp_path: Path) -> None:
    # (AGENTTALK_ROOT is stripped by the autouse conftest fixture.)
    root = tmp_path / "proj"
    sub = root / "a" / "b"
    sub.mkdir(parents=True)
    Store(root).init(["alpha", "beta"])
    assert find_root(sub) == root.resolve()
    # no store anywhere -> falls back to start
    bare = tmp_path / "bare"
    bare.mkdir()
    assert find_root(bare) == bare.resolve()


def test_find_stores_upward(tmp_path: Path) -> None:
    outer = tmp_path / "outer"
    inner = outer / "mid" / "inner"
    inner.mkdir(parents=True)
    assert find_stores_upward(inner) == []
    Store(outer).init(["alpha", "beta"])
    assert find_stores_upward(inner) == [outer.resolve()]
    Store(inner).init(["alpha", "beta"])
    # walk order: nearest first
    assert find_stores_upward(inner) == [inner.resolve(), outer.resolve()]


# ------------------------------------------------ operator_facing (T003)

def test_operator_facing_roundtrip(store: Store) -> None:
    assert store.operator_facing() is None
    assert store.operator_facing_raw() is None
    store.set_operator_facing("alpha")
    assert store.operator_facing() == "alpha"
    assert store.operator_facing_raw() == "alpha"
    store.set_operator_facing(None)
    assert store.operator_facing() is None
    assert "operator_facing" not in store.load_config()


def test_set_operator_facing_rejects_non_roster(store: Store) -> None:
    with pytest.raises(ValueError, match="not in the roster"):
        store.set_operator_facing("ghost")


def test_operator_facing_stale_after_roster_removal(store: Store) -> None:
    store.set_operator_facing("beta")
    store.remove_agent("beta")
    # routing accessor refuses a pruned mailbox; raw keeps it for doctor
    assert store.operator_facing() is None
    assert store.operator_facing_raw() == "beta"


def test_operator_facing_tolerates_garbage_config(store: Store) -> None:
    for garbage in (None, 123, "", ["alpha"]):
        cfg = store.load_config()
        cfg["operator_facing"] = garbage
        store._write_config(cfg)
        assert store.operator_facing() is None
        assert store.operator_facing_raw() is None


# ------------------------------------------- composing intent (T003, #14)

def test_composing_intent_roundtrip_and_clear(store: Store) -> None:
    assert store.read_composing_intent("alpha") == {}
    store.write_composing_intent("alpha", "q-1", "beta")
    store.write_composing_intent("alpha", "q-2", "beta")
    threads = store.read_composing_intent("alpha")["threads"]
    assert set(threads) == {"q-1", "q-2"}
    assert threads["q-1"]["peer"] == "beta"
    assert "at" in threads["q-1"]
    store.clear_composing_intent("alpha", "q-1")
    assert set(store.read_composing_intent("alpha")["threads"]) == {"q-2"}
    store.clear_composing_intent("alpha", "q-2")
    # last entry removed -> file gone entirely
    assert store.read_composing_intent("alpha") == {}
    assert not (store.state_dir / "alpha.composing.json").exists()


def test_composing_intent_corrupt_reads_empty(store: Store) -> None:
    p = store.state_dir / "alpha.composing.json"
    p.write_text("{not json", encoding="utf-8")
    assert store.read_composing_intent("alpha") == {}
    p.write_text(json.dumps(["a", "list"]), encoding="utf-8")
    assert store.read_composing_intent("alpha") == {}


def test_composing_intent_clear_missing_is_silent(store: Store) -> None:
    store.clear_composing_intent("alpha")          # whole file, absent
    store.clear_composing_intent("alpha", "q-1")   # one rid, absent


def test_composing_intent_stale_constant_matches_cap() -> None:
    # One number, one meaning: the marker staleness horizon equals the
    # wait loop's cumulative composing-extension cap.
    from agenttalk import cli
    assert COMPOSING_INTENT_STALE_SECONDS == cli._COMPOSING_MAX_EXTEND_SECONDS


# ======================================================================
# 0.15.0 quarantine (WP01, #17)
# ======================================================================

def _seed_invalid(store: Store, name: str, payload: str) -> Path:
    p = store.messages_dir / name
    p.write_text(payload, encoding="utf-8")
    return p


def _valid_state_fingerprint(store: Store) -> dict:
    out = {}
    for p in sorted(store.messages_dir.glob("*.json")):
        out[p.name] = hashlib.sha256(p.read_bytes()).hexdigest()
    for p in sorted(store.state_dir.iterdir()):
        if p.is_file():
            out["state/" + p.name] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def test_quarantine_selection_equals_invalid_report(store: Store) -> None:
    store.send(sender="alpha", recipient="beta", body="valid one")
    _seed_invalid(store, "20990101-000000-000000-AAAA.json",
                  '{"id": "20990101-000000-000000-AAAA", "ts": "2026-01-01T00:00:00Z", '
                  '"from": "ghost", "to": "beta", "kind": "message", "subject": "", '
                  '"body": "x", "meta": {}}')   # out-of-roster sender
    _seed_invalid(store, "garbage.json", "{not json")
    report_ids = {i for i, _ in store.list_invalid_messages()}
    path_ids = {ident for _, ident, _ in store.list_invalid_message_paths()}
    assert path_ids == report_ids  # FR-011 lockstep by construction
    assert len(report_ids) == 2


def test_quarantine_dry_run_moves_nothing(store: Store) -> None:
    _seed_invalid(store, "garbage.json", "{not json")
    before = _valid_state_fingerprint(store)
    records = store.quarantine_invalid(dry_run=True)
    assert len(records) == 1
    assert records[0]["to"] is not None
    assert _valid_state_fingerprint(store) == before
    assert store.quarantined_count() == 0


def test_quarantine_moves_exactly_invalid_and_valid_untouched(store: Store) -> None:
    m = store.send(sender="alpha", recipient="beta", body="keep me")
    _seed_invalid(store, "garbage.json", "{not json")
    _seed_invalid(store, "20990101-000000-000000-BBBB.json",
                  '{"id": "20990101-000000-000000-BBBB", "ts": "2026-01-01T00:00:00Z", '
                  '"from": "ghost", "to": "beta", "kind": "message", "subject": "", '
                  '"body": "x", "meta": {}}')
    valid_before = {p.name: p.read_bytes() for p in store.messages_dir.glob("*.json")
                    if p.stem == m.id}
    records = store.quarantine_invalid()
    moved = [r for r in records if r["to"]]
    assert len(moved) == 2
    assert store.quarantined_count() == 2
    assert store.list_invalid_messages() == []           # report empty after
    # valid file byte-identical, still delivered
    valid_after = {p.name: p.read_bytes() for p in store.messages_dir.glob("*.json")}
    assert valid_after == valid_before
    assert [x.body for x in store.messages_for("beta")] == ["keep me"]


def test_quarantine_collision_never_overwrites(store: Store) -> None:
    _seed_invalid(store, "garbage.json", "{not json v1")
    store.quarantine_invalid()
    _seed_invalid(store, "garbage.json", "{not json v2")  # same name reappears
    store.quarantine_invalid()
    files = [p for p in store.quarantine_dir.iterdir() if p.is_file()]
    assert len(files) == 2  # suffixed, not overwritten
    contents = sorted(p.read_text(encoding="utf-8") for p in files)
    assert contents == ["{not json v1", "{not json v2"]


def test_quarantine_zero_invalid_noop(store: Store) -> None:
    assert store.quarantine_invalid() == []
    assert store.quarantined_count() == 0
    assert not store.quarantine_dir.exists()  # not even created


def test_quarantined_files_invisible_to_scanning(store: Store) -> None:
    _seed_invalid(store, "garbage.json", "{not json")
    store.quarantine_invalid()
    # scanning surfaces: all_messages, valid_messages, invalid report
    assert store.list_invalid_messages() == []
    assert all("garbage" not in m.id for m in store.all_messages())


def test_quarantine_embedded_id_collision_moves_invalid_only(store: Store) -> None:
    """Codex WP01 review repro: an INVALID file whose embedded id equals a
    VALID file's stem must never cause the valid file to be moved."""
    # valid file named aaa.json with id aaa (hand-built but fully valid)
    valid = store.messages_dir / "aaa.json"
    valid.write_text(
        '{"id": "aaa", "ts": "2026-01-01T00:00:00Z", "from": "alpha", '
        '"to": "beta", "kind": "message", "subject": "", "body": "keep", '
        '"meta": {}}', encoding="utf-8")
    valid_bytes = valid.read_bytes()
    # invalid file zzz.json EMBEDDING id aaa (unknown kind -> invalid)
    bad = store.messages_dir / "zzz.json"
    bad.write_text(
        '{"id": "aaa", "ts": "2026-01-01T00:00:00Z", "from": "alpha", '
        '"to": "beta", "kind": "not-a-kind", "subject": "", "body": "x", '
        '"meta": {}}', encoding="utf-8")
    # selection resolves the verdict to ITS file, not the colliding stem
    sel = {str(p): ident for p, ident, _ in store.list_invalid_message_paths()}
    assert str(bad) in sel and sel[str(bad)] == "aaa"
    assert str(valid) not in sel
    records = store.quarantine_invalid()
    assert len(records) == 1
    assert records[0]["from"] == str(bad)
    assert not bad.exists()                      # invalid moved
    assert valid.read_bytes() == valid_bytes     # valid byte-identical
    assert store.quarantined_count() == 1
