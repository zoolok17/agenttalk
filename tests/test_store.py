"""Tests for the on-disk Store: init, send, cursors, heartbeats."""

from __future__ import annotations

import hashlib
import json
import os as _os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agenttalk.store import (
    COMPOSING_INTENT_STALE_SECONDS,
    CONTROL_KINDS,
    KNOWN_KINDS,
    OPENER_KINDS,
    Store,
    _ID_RE,
    _new_id,
    _process_alive,
    find_root,
    find_stores_upward,
    validate_agent_name,
    validate_agent_roster,
    validate_rescind,
    validate_retired,
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


@pytest.mark.parametrize("n", [10, 100, 300])
def test_messages_for_since_id_does_not_open_old_files(
    store: Store, monkeypatch: pytest.MonkeyPatch, n: int
) -> None:
    """Perf fix #1 guard: messages_for(since_id=newest) must open ZERO
    message files regardless of store size, so a caught-up poller's
    per-poll cost is independent of how big messages/ has grown. The
    contrast assertion (a full scan opens all N) keeps this honest — it
    fails loudly if the skip ever silently degrades to a no-op."""
    ids = [store.send(sender="alpha", recipient="beta", body=str(i)).id
           for i in range(n)]
    newest = ids[-1]

    opened: list[str] = []
    real_read_text = Path.read_text

    def counting_read_text(self: Path, *a: object, **k: object) -> str:
        if self.parent == store.messages_dir:
            opened.append(self.name)
        return real_read_text(self, *a, **k)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", counting_read_text)

    # Caught up: nothing strictly newer than the cursor → no file opened.
    assert store.messages_for("beta", since_id=newest) == []
    assert opened == [], (
        f"hot path opened {len(opened)} message files for N={n}; "
        "per-poll cost must be independent of store size"
    )

    # Honesty check: a full scan really does open every file, so the
    # zero above is a real skip, not a vacuous truth.
    opened.clear()
    assert len(store.messages_for("beta")) == n
    assert len(opened) == n


def test_messages_for_since_id_is_exclusive_after_skip(store: Store) -> None:
    """The filename fast-skip must not change EXCLUSIVE since_id semantics:
    the cursor message itself is never redelivered, the next one is."""
    m1 = store.send(sender="alpha", recipient="beta", body="one")
    m2 = store.send(sender="alpha", recipient="beta", body="two")
    got = store.messages_for("beta", since_id=m1.id)
    assert [m.id for m in got] == [m2.id]


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
    VALID file's stem must never cause the valid file to be moved.

    (0.18.0: ids must now be generated-shape, so both the valid stem and the
    colliding embedded id use a real id shape — the collision being tested is
    the path-vs-embedded-id pairing, not the id format.)"""
    cid = "20260101-000000-000000-AAAA"   # the colliding id
    # valid file whose STEM is the colliding id, fully valid
    valid = store.messages_dir / f"{cid}.json"
    valid.write_text(
        '{"id": "' + cid + '", "ts": "2026-01-01T00:00:00Z", "from": "alpha", '
        '"to": "beta", "kind": "message", "subject": "", "body": "keep", '
        '"meta": {}}', encoding="utf-8")
    valid_bytes = valid.read_bytes()
    # invalid file (its own valid-shape stem) EMBEDDING the colliding id
    bad = store.messages_dir / "20260101-000000-000000-ZZZZ.json"
    bad.write_text(
        '{"id": "' + cid + '", "ts": "2026-01-01T00:00:00Z", "from": "alpha", '
        '"to": "beta", "kind": "not-a-kind", "subject": "", "body": "x", '
        '"meta": {}}', encoding="utf-8")
    # selection resolves the verdict to ITS file, not the colliding stem
    sel = {str(p): ident for p, ident, _ in store.list_invalid_message_paths()}
    assert str(bad) in sel and sel[str(bad)] == cid
    assert str(valid) not in sel
    records = store.quarantine_invalid()
    assert len(records) == 1
    assert records[0]["from"] == str(bad)
    assert not bad.exists()                      # invalid moved
    assert valid.read_bytes() == valid_bytes     # valid byte-identical
    assert store.quarantined_count() == 1


# ======================================================================
# review H1: filename-stem must match embedded id; validated set sorted
# by id (cursor-poisoning / wrong-replay-order defense)
# ======================================================================

def test_filename_stem_mismatch_is_invalid_and_not_delivered(store: Store) -> None:
    """A file whose name does not equal its embedded id is forged/corrupt:
    quarantinable, never delivered, and unable to poison a cursor.

    Vector: a low-sorting filename embedding a valid-shape HIGH/future id.
    The embedded id passes _ID_RE (shape), so only a stem==id check stops
    it from being delivered and advancing the cursor past real messages."""
    forged_id = "29990101-000000-000000-AAAA"   # valid shape, far future
    p = store.messages_dir / "00000000-000000-000000-zzzz.json"  # low filename
    p.write_text(json.dumps({
        "id": forged_id, "ts": "2026-01-01T00:00:00Z", "from": "alpha",
        "to": "beta", "kind": "message", "subject": "", "body": "forged",
        "meta": {},
    }), encoding="utf-8")
    invalid = dict(store.list_invalid_messages())
    assert forged_id in invalid
    assert "stem" in invalid[forged_id] and "does not match" in invalid[forged_id]
    # never delivered
    assert all(m.id != forged_id for m in store.messages_for("beta"))
    # cursor not poisoned: a real (lower-id) message still delivers afterward
    store.send(sender="alpha", recipient="beta", body="REAL")
    unread = store.unread_for("beta")
    assert any(m.body == "REAL" for m in unread)
    assert all(m.id != forged_id for m in unread)


def test_validated_messages_returned_in_id_order(store: Store) -> None:
    """valid_messages()/_validated_messages() contract says id order
    (chronological). Pin it so a future change can't silently regress to
    raw filesystem-iteration order."""
    sent = [store.send(sender="alpha", recipient="beta", body=str(i)).id
            for i in range(6)]
    got = [m.id for m in store.valid_messages()]
    assert got == sorted(got)
    assert set(sent).issubset(set(got))


def test_corrupt_config_delivers_nothing_failclosed(store: Store) -> None:
    """A corrupt/unloadable config (empty roster) must deliver NOTHING, not
    fall through to validate's empty-roster fail-open and deliver forged or
    off-roster messages (review L)."""
    store.send(sender="alpha", recipient="beta", body="hi")
    assert any(m.body == "hi" for m in store.messages_for("beta"))  # normal path
    store.config_path.write_text("{ not valid json", encoding="utf-8")
    assert store.messages_for("beta") == []   # fail-closed on corrupt roster
    assert store.valid_messages() == []


def test_message_body_roundtrips_unicode_crlf_multiline(store: Store) -> None:
    """Real agent messages carry code blocks, multiline plans, and non-ASCII
    text. A body must survive the send()->disk->messages_for() round trip
    byte-for-byte (review test-coverage gap)."""
    body = "approved -> ship · 我们 \U0001f389\r\nline2\nline3\ttab"
    store.send(sender="alpha", recipient="beta", body=body)
    reload = store.messages_for("beta")[-1]
    assert reload.body == body


def test_capacity_roundtrip_and_read_all(store: Store) -> None:
    snap = {"source_agent": "alpha", "observed_at": "2026-06-09T08:00:00Z",
            "source": "codex_rollout", "primary_used_percent": 12.0,
            "confidence": "observed"}
    store.write_capacity("alpha", snap)
    assert store.read_capacity("alpha") == snap
    store.write_capacity("beta", {"source_agent": "beta", "confidence": "unknown"})
    allc = store.read_all_capacities()
    assert set(allc) == {"alpha", "beta"}
    assert allc["alpha"]["primary_used_percent"] == 12.0


def test_capacity_absent_and_corrupt_never_raise(store: Store) -> None:
    assert store.read_capacity("alpha") is None          # absent
    p = store.state_dir / "alpha.capacity.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json", encoding="utf-8")
    assert store.read_capacity("alpha") is None          # corrupt -> None
    assert store.read_all_capacities() == {}             # corrupt skipped


# ============================================================= #19 Phase A
# Identity registry, retirement & epoch store layer (WP01).

def _store3(tmp_path: Path) -> Store:
    """A fresh 3-agent store (alpha, beta, gamma)."""
    s = Store(tmp_path)
    s.init(["alpha", "beta", "gamma"])
    return s


def _opener(store: Store, sender: str, recipient: str, rid: str):
    return store.send(sender=sender, recipient=recipient, kind="review-request",
                      subject="r", body="please review", meta={"request_id": rid})


# --- T001: retired registry validation ------------------------------------

def test_validate_retired_accepts_well_formed() -> None:
    validate_retired(
        [{"name": "codex", "retired_at": "2026-01-01T00:00:00Z",
          "renamed_to": "codex-rev", "reason": "renamed"}],
        ["claude", "codex-rev"],
    )


def test_validate_retired_rejects_overlap_with_active() -> None:
    with pytest.raises(ValueError, match="active XOR retired"):
        validate_retired([{"name": "alpha"}], ["alpha", "beta"])


def test_validate_retired_rejects_duplicate_tombstone() -> None:
    with pytest.raises(ValueError, match="duplicate tombstone"):
        validate_retired([{"name": "x"}, {"name": "X"}], [])  # case-variant dup


def test_validate_retired_rejects_unsafe_name_and_renamed_to() -> None:
    with pytest.raises(ValueError):
        validate_retired([{"name": "../escape"}], [])
    with pytest.raises(ValueError):
        validate_retired([{"name": "ok", "renamed_to": "../bad"}], [])


def test_load_config_rejects_corrupt_retired(tmp_path: Path) -> None:
    s = _store3(tmp_path)
    cfg = s.load_config()
    cfg["retired"] = [{"name": "alpha"}]  # alpha is active -> overlap
    s._write_config(cfg)
    with pytest.raises(ValueError, match="corrupt config"):
        s.load_config()


# --- T002: active / retired / known roster + history validation -----------

def test_roster_views_and_history_validation(tmp_path: Path) -> None:
    s = _store3(tmp_path)
    _opener(s, "gamma", "alpha", "rid-hist")          # gamma authored history
    s.retire_agent("gamma")
    assert "gamma" not in s.active_agents()
    assert "gamma" in s.retired_agents()
    assert "gamma" in s.known_agents()
    # gamma's historical opener still validates (known roster), not quarantined
    rids = {(m.meta or {}).get("request_id") for m in s.valid_messages()}
    assert "rid-hist" in rids
    assert s.quarantined_count() == 0
    assert not s.list_invalid_messages()


# --- T003: retire / rename ------------------------------------------------

def test_retire_agent_drops_role_group_liaison(tmp_path: Path) -> None:
    s = _store3(tmp_path)
    s.set_role("gamma", "reviewer")
    s.set_group("revs", ["gamma"])
    s.set_operator_facing("gamma")
    s.retire_agent("gamma", reason="left")
    cfg = s.load_config()
    assert "gamma" not in cfg["agents"]
    assert cfg["roles"].get("gamma") is None
    assert "gamma" not in cfg["groups"].get("revs", [])
    assert cfg.get("operator_facing") is None
    tomb = [e for e in cfg["retired"] if e["name"] == "gamma"][0]
    assert tomb["renamed_to"] is None and tomb["reason"] == "left"


def test_retire_agent_refuses_unknown_or_already_retired(tmp_path: Path) -> None:
    s = _store3(tmp_path)
    with pytest.raises(ValueError, match="not in the active roster"):
        s.retire_agent("nobody")
    s.retire_agent("gamma")
    with pytest.raises(ValueError, match="already retired"):
        s.retire_agent("gamma")


def test_rename_agent_carries_role_and_liaison(tmp_path: Path) -> None:
    s = _store3(tmp_path)
    s.set_role("gamma", "reviewer")
    s.set_group("revs", ["gamma"])
    s.set_operator_facing("gamma")
    s.rename_agent("gamma", "gamma-rev", reason="role split")
    cfg = s.load_config()
    assert "gamma-rev" in cfg["agents"] and "gamma" not in cfg["agents"]
    assert cfg["roles"]["gamma-rev"] == "reviewer"
    assert "gamma-rev" in cfg["groups"]["revs"]
    assert cfg["operator_facing"] == "gamma-rev"
    tomb = [e for e in cfg["retired"] if e["name"] == "gamma"][0]
    assert tomb["renamed_to"] == "gamma-rev"
    # new identity got a cursor file
    assert (s.state_dir / "gamma-rev.cursor").exists()


def test_rename_refuses_old_not_active_or_new_already_known(tmp_path: Path) -> None:
    s = _store3(tmp_path)
    with pytest.raises(ValueError, match="not in the active roster"):
        s.rename_agent("nobody", "x")
    with pytest.raises(ValueError, match="already an active identity"):
        s.rename_agent("gamma", "alpha")
    s.retire_agent("beta")
    with pytest.raises(ValueError, match="retired tombstone"):
        s.rename_agent("gamma", "beta")        # non-rebindable to a tombstone


# --- B2: add_agent / non-rebindable guard ---------------------------------

def test_add_agent_refuses_retired_tombstone(tmp_path: Path) -> None:
    s = _store3(tmp_path)
    s.retire_agent("gamma")
    with pytest.raises(ValueError, match="retired tombstone"):
        s.add_agent("gamma")
    with pytest.raises(ValueError, match="retired tombstone"):
        s.add_agent("GAMMA")                   # case-insensitive
    # config never got a name in both lists
    cfg = s.load_config()
    assert "gamma" not in cfg["agents"]


def test_force_removed_name_is_re_addable(tmp_path: Path) -> None:
    # remove_agent leaves NO tombstone, so the name stays re-addable (the
    # documented distinction from retire). The CLI gates --force; the store
    # primitive removes mechanically.
    s = _store3(tmp_path)
    s.remove_agent("gamma")
    assert "gamma" not in s.active_agents()
    assert "gamma" not in s.retired_agents()
    s.add_agent("gamma")                       # no tombstone -> allowed
    assert "gamma" in s.active_agents()


# --- T003: _drain_check ---------------------------------------------------

def test_drain_check_reports_owed_then_empty(tmp_path: Path) -> None:
    s = _store3(tmp_path)
    _opener(s, "alpha", "gamma", "rid-owed")   # gamma owes a review-result
    owed = s._drain_check("gamma")
    assert any(r["request_id"] == "rid-owed" for r in owed)
    # alpha gets its review-result -> thread closes -> no longer owed
    s.send(sender="gamma", recipient="alpha", kind="review-result",
           subject="r", body="lgtm",
           meta={"request_id": "rid-owed", "status": "approved"})
    assert s._drain_check("gamma") == []


# --- T004: retired-send refusal -------------------------------------------

def test_retired_cannot_send_or_receive(tmp_path: Path) -> None:
    s = _store3(tmp_path)
    s.retire_agent("gamma")
    with pytest.raises(ValueError, match="retired .*cannot send"):
        s.send(sender="gamma", recipient="alpha", body="hi")
    with pytest.raises(ValueError, match="retired .*cannot receive"):
        s.send(sender="alpha", recipient="gamma", body="hi")


# --- T005: single-hop retired forwarding (B4) -----------------------------

def test_forward_retired_happy_path(tmp_path: Path) -> None:
    s = _store3(tmp_path)
    _opener(s, "alpha", "gamma", "rid-fwd")    # owed to/from gamma
    s.retire_agent("gamma")
    msg = s.forward_retired("gamma", "beta", "rid-fwd", from_agent="alpha",
                            reason="gamma left")
    assert msg.kind == "note" and msg.recipient == "beta" and msg.sender == "alpha"
    assert msg.meta["forwarded_from"] == "gamma"
    assert msg.meta["forwarded_request_id"] == "rid-fwd"
    assert msg.meta["forward"]["hop"] == 1


def test_forward_retired_uses_liaison_when_no_from(tmp_path: Path) -> None:
    s = _store3(tmp_path)
    _opener(s, "beta", "gamma", "rid-l")
    s.set_operator_facing("alpha")
    s.retire_agent("gamma")
    msg = s.forward_retired("gamma", "beta", "rid-l")
    assert msg.sender == "alpha"               # operator_facing default


def test_forward_retired_refusals(tmp_path: Path) -> None:
    s = _store3(tmp_path)
    _opener(s, "alpha", "gamma", "rid-x")
    # active source refused
    with pytest.raises(ValueError, match="not a retired identity"):
        s.forward_retired("gamma", "beta", "rid-x", from_agent="alpha")
    s.retire_agent("gamma")
    # non-owed request refused (gamma is not a participant in rid-other)
    _opener(s, "alpha", "beta", "rid-other")
    with pytest.raises(ValueError, match="owed to/from"):
        s.forward_retired("gamma", "beta", "rid-other", from_agent="alpha")
    # missing sender (no --from, no liaison) refused
    with pytest.raises(ValueError, match="explicit --from"):
        s.forward_retired("gamma", "beta", "rid-x")
    # target must be active
    with pytest.raises(ValueError, match="not in the active roster"):
        s.forward_retired("gamma", "nobody", "rid-x", from_agent="alpha")


def test_forward_retired_refuses_closed_thread(tmp_path: Path) -> None:
    # Codex WP01 B1: a request that is no longer outstanding cannot be
    # forwarded — there is no obligation to redirect.
    s = _store3(tmp_path)
    _opener(s, "alpha", "gamma", "rid-done")
    s.send(sender="gamma", recipient="alpha", kind="review-result",
           subject="r", body="lgtm",
           meta={"request_id": "rid-done", "status": "approved"})
    s.retire_agent("gamma")
    with pytest.raises(ValueError, match="not an open thread"):
        s.forward_retired("gamma", "beta", "rid-done", from_agent="alpha")


def test_forward_retired_refuses_second_hop(tmp_path: Path) -> None:
    # Codex WP01 B2: a request may be forwarded at most once.
    s = _store3(tmp_path)
    _opener(s, "alpha", "gamma", "rid-once")
    s.retire_agent("gamma")
    s.forward_retired("gamma", "beta", "rid-once", from_agent="alpha")
    with pytest.raises(ValueError, match="already forwarded"):
        s.forward_retired("gamma", "beta", "rid-once", from_agent="alpha")


# --- T006: current_epoch + epoch_at_send three-state ----------------------

def _barrier(store: Store, sender: str = "alpha"):
    return store.send(sender=sender, recipient=sender, kind="message",
                      subject="epoch bump", body="void prior run",
                      meta={"barrier": {"version": 1, "scope": "global",
                                        "type": "epoch-bump"}})


def test_current_epoch_none_then_latest(tmp_path: Path) -> None:
    s = _store3(tmp_path)
    assert s.current_epoch() is None
    b1 = _barrier(s)
    assert s.current_epoch() == b1.id
    b2 = _barrier(s, "beta")
    assert s.current_epoch() == b2.id          # latest by id order


def test_current_epoch_ignores_malformed_barrier(tmp_path: Path) -> None:
    s = _store3(tmp_path)
    s.send(sender="alpha", recipient="alpha", kind="message", body="x",
           meta={"barrier": {"scope": "local"}})   # not global / no version
    assert s.current_epoch() is None


def test_epoch_at_send_three_state(tmp_path: Path) -> None:
    s = _store3(tmp_path)
    # non-opener -> no key
    note = s.send(sender="alpha", recipient="beta", kind="note", body="hi")
    assert "epoch_at_send" not in note.meta
    # opener, no barrier yet -> null (key present, value None)
    o1 = _opener(s, "alpha", "beta", "r1")
    assert "epoch_at_send" in o1.meta and o1.meta["epoch_at_send"] is None
    # opener after a barrier -> stamped with the barrier id
    b = _barrier(s)
    o2 = _opener(s, "alpha", "beta", "r2")
    assert o2.meta["epoch_at_send"] == b.id


def test_epoch_at_send_respects_supplied_value(tmp_path: Path) -> None:
    # B3 precondition: a caller-supplied epoch_at_send is NOT overwritten.
    s = _store3(tmp_path)
    _barrier(s)
    o = s.send(sender="alpha", recipient="beta", kind="review-request",
               subject="r", body="x",
               meta={"request_id": "rb", "epoch_at_send": "PINNED"})
    assert o.meta["epoch_at_send"] == "PINNED"


def test_init_force_preserves_tombstones_and_refuses_rebind(tmp_path: Path) -> None:
    # fresh-eyes BLOCKER: init --force must NOT silently resurrect a retired
    # tombstone (FR-002 / SC-003). It preserves `retired` and refuses a roster
    # that collides with a tombstone.
    s = Store(tmp_path)
    s.init(["alpha", "beta", "gamma"])
    s.retire_agent("gamma")
    # re-binding gamma via init --force is refused (case-insensitive)
    with pytest.raises(ValueError, match="retired tombstone"):
        s.init(["alpha", "GAMMA"], force=True)
    # a force re-init with a non-colliding roster preserves the tombstone
    cfg = s.init(["alpha", "delta"], force=True)
    assert "gamma" in [e["name"] for e in cfg.get("retired", [])]
    assert "gamma" not in cfg["agents"]
    # and gamma is still non-rebindable afterward
    with pytest.raises(ValueError, match="retired tombstone"):
        Store(tmp_path).add_agent("gamma")


def test_init_force_preserves_tombstone_from_validation_failed_config(tmp_path: Path) -> None:
    # Codex review of the fresh-eyes fix: a tombstone PRESENT in a
    # validation-FAILED config (the retired name also re-added to `agents`)
    # must still be preserved + protected — init --force must not drop it.
    import json as _json
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    s.retire_agent("beta")
    # corrupt: put beta back into active agents while keeping the tombstone
    cfgp = tmp_path / ".agenttalk" / "config.json"
    cfg = _json.loads(cfgp.read_text(encoding="utf-8"))
    cfg["agents"] = ["alpha", "beta"]              # now active∩retired overlap
    cfgp.write_text(_json.dumps(cfg), encoding="utf-8")
    # the config is now validation-failed
    with pytest.raises(ValueError):
        s.load_config()
    # force re-init that re-binds beta is REFUSED (tombstone read defensively)
    with pytest.raises(ValueError, match="retired tombstone"):
        s.init(["alpha", "beta"], force=True)
    # a non-colliding force re-init preserves the beta tombstone + yields a
    # loadable config
    cfg2 = s.init(["alpha", "gamma"], force=True)
    assert "beta" in [e["name"] for e in cfg2.get("retired", [])]
    assert "beta" not in cfg2["agents"]
    Store(tmp_path).load_config()                  # no longer corrupt


# ======================================================================
# 0.18.0 review-hardening (WP01): id-shape validation + liveness primitive
# ======================================================================


def test_id_re_accepts_all_generated_ids() -> None:
    """The validator is built from _ID_ALPHABET so it must accept every
    id _new_id can emit — including the monotonic +1us bump near rollovers."""
    for _ in range(3000):
        assert _ID_RE.match(_new_id())


def test_id_re_rejects_malformed() -> None:
    for bad in ("zzzz", "", "20260607", "20260607-150219-427413",
                "20260607-150219-427413-mZ", "20260607-150219-427413-mZLg!",
                "xxxxxxxx-xxxxxx-xxxxxx-mZLg", "20260607-150219-427413-mZLg-x"):
        assert not _ID_RE.match(bad), bad


def test_malformed_id_is_invalid_not_delivered(tmp_path: Path) -> None:
    """A roster-valid file with a non-generated id must be classified
    invalid (quarantinable), never delivered, and never poison a cursor."""
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    _seed_invalid(s, "zzzz.json", json.dumps({
        "id": "zzzz", "ts": "2025-01-01T00:00:00Z", "from": "alpha",
        "to": "beta", "kind": "message", "subject": "", "body": "x", "meta": {},
    }))
    invalid_ids = {mid for mid, _ in s.list_invalid_messages()}
    assert "zzzz" in invalid_ids
    assert all(m.id != "zzzz" for m in s.messages_for("beta"))
    # a real message still delivers (cursor not poisoned by zzzz)
    s.send(sender="alpha", recipient="beta", body="REAL")
    assert any(m.body == "REAL" for m in s.unread_for("beta"))


def test_process_alive_basic() -> None:
    assert _process_alive(_os.getpid()) is True
    assert _process_alive(2 ** 31 - 1) is False   # almost-certainly dead
    assert _process_alive(0) is False
    assert _process_alive(-1) is False
    assert _process_alive("x") is False           # never raises


def test_foreign_wait_pid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    # same pid -> None (it's me, not a duplicate)
    s.write_waiting("alpha", {"agent": "alpha", "pid": _os.getpid(),
                              "deadline_epoch": None})
    assert s.foreign_wait_pid("alpha", _os.getpid()) is None
    # foreign + dead -> None
    s.write_waiting("alpha", {"agent": "alpha", "pid": 2 ** 31 - 1,
                              "deadline_epoch": None})
    assert s.foreign_wait_pid("alpha", _os.getpid()) is None
    # foreign + alive (monkeypatched) + fresh -> returns it
    monkeypatch.setattr("agenttalk.store._process_alive", lambda pid: True)
    s.write_waiting("alpha", {"agent": "alpha", "pid": 999999,
                              "deadline_epoch": None})
    assert s.foreign_wait_pid("alpha", _os.getpid()) == 999999
    # foreign + alive but STALE (deadline far past) -> None
    s.write_waiting("alpha", {"agent": "alpha", "pid": 999999,
                              "deadline_epoch": 1000.0})
    assert s.foreign_wait_pid("alpha", _os.getpid(),
                              now=1_000_000_000.0, stale_after=60.0) is None
    # no marker -> None
    s.clear_waiting("alpha")
    assert s.foreign_wait_pid("alpha", _os.getpid()) is None


def test_clear_dead_waiter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """fix #4b: a CONFIRMED-DEAD foreign marker is reaped; a live one, our
    own, or an absent one is left untouched."""
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    live = 4242
    monkeypatch.setattr("agenttalk.store._process_alive", lambda pid: pid == live)
    # foreign + dead -> cleared
    s.write_waiting("alpha", {"agent": "alpha", "pid": 999, "deadline_epoch": None})
    assert s.clear_dead_waiter("alpha", _os.getpid()) is True
    assert s.read_waiting("alpha") is None
    # foreign + alive -> NOT cleared
    s.write_waiting("alpha", {"agent": "alpha", "pid": live, "deadline_epoch": None})
    assert s.clear_dead_waiter("alpha", _os.getpid()) is False
    assert s.read_waiting("alpha") is not None
    # our own pid -> NOT cleared (we're about to overwrite it ourselves)
    s.write_waiting("alpha", {"agent": "alpha", "pid": _os.getpid(),
                              "deadline_epoch": None})
    assert s.clear_dead_waiter("alpha", _os.getpid()) is False
    assert s.read_waiting("alpha") is not None
    # absent -> False
    s.clear_waiting("alpha")
    assert s.clear_dead_waiter("alpha", _os.getpid()) is False


def test_live_waiter_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """fix #4c soft-cap signal: counts every FRESH+LIVE waiter marker, skips
    dead-owner and stale-past-deadline ones."""
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    # pid 999 is the only dead process; everything else is alive.
    monkeypatch.setattr("agenttalk.store._process_alive", lambda pid: pid != 999)
    assert s.live_waiter_count() == 0
    s.write_waiting("alpha", {"agent": "alpha", "pid": 111, "deadline_epoch": None})
    s.write_waiting("beta", {"agent": "beta", "pid": 222, "deadline_epoch": None})
    assert s.live_waiter_count() == 2
    # dead owner -> not counted
    s.write_waiting("gamma", {"agent": "gamma", "pid": 999, "deadline_epoch": None})
    assert s.live_waiter_count() == 2
    # alive but STALE (deadline far past + threshold) -> not counted
    s.write_waiting("delta", {"agent": "delta", "pid": 333, "deadline_epoch": 1000.0})
    assert s.live_waiter_count(now=1_000_000_000.0, stale_after=60.0) == 2


# --- 0.24.0: at-most-one-lead invariant + sole_lead (WP01) ----------------

def test_set_role_lead_is_unique_demotes_prior(tmp_path: Path) -> None:
    s = _store3(tmp_path)
    assert s.set_role("alpha", "lead") == []          # first lead, nothing demoted
    demoted = s.set_role("beta", "lead")              # move the lead
    assert demoted == ["alpha"]                       # prior lead reported
    roles = s.load_config().get("roles", {})
    leads = [a for a, r in roles.items() if r == "lead"]
    assert leads == ["beta"]                          # exactly one lead remains


def test_set_role_lead_idempotent_self_set(tmp_path: Path) -> None:
    s = _store3(tmp_path)
    s.set_role("alpha", "lead")
    assert s.set_role("alpha", "lead") == []          # re-setting self: no demotion
    assert s.sole_lead() == "alpha"


def test_set_role_lead_case_insensitive(tmp_path: Path) -> None:
    s = _store3(tmp_path)
    s.set_role("alpha", "lead")
    demoted = s.set_role("beta", "Lead")              # different casing, same role
    assert demoted == ["alpha"]
    assert s.sole_lead() == "beta"
    assert s.load_config()["roles"]["beta"] == "Lead"  # stored verbatim


def test_set_role_zero_leads_is_valid(tmp_path: Path) -> None:
    s = _store3(tmp_path)
    s.set_role("alpha", "lead")
    s.set_role("alpha", "reviewer")                   # demote the lead to another role
    assert s.sole_lead() is None                      # zero leads: allowed, no error


def test_sole_lead_resolution(tmp_path: Path) -> None:
    s = _store3(tmp_path)
    assert s.sole_lead() is None                      # zero leads
    s.set_role("alpha", "lead")
    assert s.sole_lead() == "alpha"                   # exactly one
    # Legacy/hand-edited config with TWO leads -> ambiguous -> None (not a guess)
    cfg = s.load_config()
    cfg["roles"] = {"alpha": "lead", "beta": "lead"}
    s._write_config(cfg)
    assert s.sole_lead() is None


def test_add_agent_lead_also_enforces_uniqueness(tmp_path: Path) -> None:
    # review BLOCKING #1: `add --role lead` must not bypass the invariant.
    s = _store3(tmp_path)
    s.set_role("alpha", "lead")
    s.add_agent("delta", role="lead")                 # new agent added AS lead
    assert s.sole_lead() == "delta"                   # prior lead demoted
    roles = s.load_config().get("roles", {})
    assert [a for a, r in roles.items() if r == "lead"] == ["delta"]


# ----------------------------------------- sandbox-safe observational writes

def test_bus_writes_survive_blocked_rename_in_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A SUPERVISED codex agent's workspace-write sandbox blocks the temp+rename
    in _atomic.write_text with [WinError 5] (test #3). The fallback lives in the
    SHARED write_text, so ALL bus writes survive: heartbeat, waiting, AND a
    message publish (send) + its cursor advance - not just observational files.
    Simulated cross-platform via os.name='nt' + os.replace->PermissionError +
    no-op sleep."""
    import agenttalk._atomic as _atomic
    monkeypatch.setattr("agenttalk._atomic._sandbox_direct_write", False)
    monkeypatch.setattr("agenttalk._atomic.os.name", "nt")
    monkeypatch.setattr("agenttalk._atomic.time.sleep", lambda _s: None)
    s = Store(tmp_path)
    s.init(["alpha", "codex-test"])
    monkeypatch.setattr("os.replace", lambda *a, **k: (_ for _ in ()).throw(
        PermissionError("[WinError 5] Access is denied (sandbox)")))
    s.write_heartbeat("codex-test")
    s.write_waiting("codex-test", {"agent": "codex-test", "pid": _os.getpid(),
                                   "deadline_epoch": 1})
    # a message PUBLISH also survives (the write_text fallback covers it, not just
    # the observational markers)
    s.send(sender="codex-test", recipient="alpha", body="hi from the sandbox")
    assert _atomic._sandbox_direct_write is True            # latch tripped once
    assert s.read_heartbeat("codex-test") is not None
    assert s.read_waiting("codex-test")["pid"] == _os.getpid()
    got = s.messages_for("alpha")                           # the published msg landed
    assert any(m.body == "hi from the sandbox" for m in got)


def test_cursor_rejects_torn_id_biasing_to_duplicate(tmp_path: Path) -> None:
    """cursor() must reject a torn/partial id (non-empty + not the id regex) as
    no-cursor - biasing to re-seeing a message (duplicate), never skipping."""
    s = Store(tmp_path)
    s.init(["alpha"])
    cp = s.state_dir / "alpha.cursor"
    cp.write_text("20260619-114133-097", encoding="utf-8")  # truncated id (a prefix)
    assert s.cursor("alpha") == ""                          # treated as no-cursor
    cp.write_text("garbage not an id", encoding="utf-8")
    assert s.cursor("alpha") == ""
    # a VALID id round-trips
    msg = s.send(sender="alpha", recipient="alpha", body="x")
    s.advance_cursor("alpha", msg.id)
    assert s.cursor("alpha") == msg.id


def test_read_heartbeat_waiting_tolerate_torn_read(tmp_path: Path) -> None:
    """A direct write is NOT atomic, so a reader can catch a half-write. The
    readers must treat a truncated/garbage file as 'no signal' (None), never
    throw - so a torn read never breaks a wait loop."""
    s = Store(tmp_path)
    s.init(["codex-test"])
    (s.state_dir / "codex-test.heartbeat").write_text("2026-06-19T09:99",
                                                      encoding="utf-8")  # bad ISO
    assert s.read_heartbeat("codex-test") is None
    (s.state_dir / "codex-test.waiting").write_text('{"agent":"codex-test","pi',
                                                    encoding="utf-8")  # half JSON
    assert s.read_waiting("codex-test") is None
    # empty (caught mid-truncate) also reads as None, not a crash
    (s.state_dir / "codex-test.heartbeat").write_text("", encoding="utf-8")
    assert s.read_heartbeat("codex-test") is None
