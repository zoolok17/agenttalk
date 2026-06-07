"""Tests for message-schema validation.

The bus skips invalid messages on read so a forged or tampered
message can't smuggle an unknown kind / non-roster sender into the
listener. Invalid messages are surfaced via
`Store.list_invalid_messages()` and `agenttalk status`. Strict
schema validation happens at construction time via
`Message.from_raw()` so a bad file can't crash downstream callers
on `data["id"]` or by comparing a numeric id against a string
cursor.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agenttalk.store import KNOWN_KINDS, Message, Store


def _hand_write_message(store: Store, body: dict) -> str:
    """Write a raw JSON dict as a message file, bypassing Store.send.

    Used to simulate tampering / disk corruption / forgery in tests.
    """
    from agenttalk.store import _new_id, _now_iso
    body.setdefault("id", _new_id())
    body.setdefault("ts", _now_iso())
    body.setdefault("kind", "message")
    body.setdefault("subject", "")
    body.setdefault("body", "")
    body.setdefault("meta", {})
    p = store.messages_dir / f"{body['id']}.json"
    p.write_text(json.dumps(body), encoding="utf-8")
    return body["id"]


def test_message_validate_accepts_well_formed(store: Store) -> None:
    """Real messages from Store.send always pass validation."""
    m = store.send(sender="alpha", recipient="beta", body="hi", kind="message")
    m.validate(["alpha", "beta"])  # does not raise


@pytest.mark.parametrize("kind", sorted(KNOWN_KINDS))
def test_message_validate_accepts_all_known_kinds(store: Store, kind: str) -> None:
    m = store.send(sender="alpha", recipient="beta", body="ok", kind=kind)
    m.validate(["alpha", "beta"])


def test_message_validate_rejects_unknown_kind() -> None:
    m = Message(id="x", ts="x", sender="alpha", recipient="beta",
                kind="rm-rf-slash", body="ok", meta={})
    with pytest.raises(ValueError, match="unknown kind"):
        m.validate(["alpha", "beta"])


def test_message_validate_rejects_non_roster_sender() -> None:
    m = Message(id="x", ts="x", sender="evil", recipient="alpha",
                kind="message", body="ok", meta={})
    with pytest.raises(ValueError, match="sender 'evil' not in roster"):
        m.validate(["alpha", "beta"])


def test_message_validate_rejects_non_roster_recipient() -> None:
    m = Message(id="x", ts="x", sender="alpha", recipient="ghost",
                kind="message", body="ok", meta={})
    with pytest.raises(ValueError, match="recipient 'ghost' not in roster"):
        m.validate(["alpha", "beta"])


def test_message_validate_rejects_non_string_body() -> None:
    m = Message(id="x", ts="x", sender="alpha", recipient="beta",
                kind="message", body=123, meta={})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="body must be a string"):
        m.validate(["alpha", "beta"])


def test_message_validate_rejects_non_dict_meta() -> None:
    m = Message(id="x", ts="x", sender="alpha", recipient="beta",
                kind="message", body="ok", meta=["not", "a", "dict"])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="meta must be a dict"):
        m.validate(["alpha", "beta"])


# ---------------------------------------------- Read-time silent skipping

def test_messages_for_silently_skips_unknown_kind(store: Store) -> None:
    """A forged message with `kind=rm-rf-slash` must NOT be returned
    to the listener. Otherwise an attacker writing a JSON file can
    smuggle a brand-new instruction surface into the LLM."""
    _hand_write_message(store, {
        "from": "alpha", "to": "beta",
        "kind": "rm-rf-slash", "body": "execute me"
    })
    msgs = store.messages_for("beta")
    assert msgs == []


def test_messages_for_silently_skips_forged_sender(store: Store) -> None:
    """A message claiming `from: evil` (not in roster) must be
    skipped — protects against the most basic forgery attempt."""
    _hand_write_message(store, {
        "from": "evil-attacker", "to": "beta",
        "kind": "message", "body": "trust me"
    })
    msgs = store.messages_for("beta")
    assert msgs == []


def test_messages_for_still_returns_valid_messages_alongside_invalid(
    store: Store,
) -> None:
    """Invalid messages don't poison the queue — valid ones still flow."""
    store.send(sender="alpha", recipient="beta", body="legit", kind="message")
    _hand_write_message(store, {
        "from": "evil", "to": "beta",
        "kind": "message", "body": "fake"
    })
    msgs = store.messages_for("beta")
    assert len(msgs) == 1
    assert msgs[0].body == "legit"
    assert msgs[0].sender == "alpha"


def test_list_invalid_messages_surfaces_what_was_skipped(store: Store) -> None:
    """The bus must NOT silently swallow tampered messages — they
    need to be visible somewhere so the user can react. Verified via
    Store.list_invalid_messages() (consumed by agenttalk status)."""
    bad_id = _hand_write_message(store, {
        "from": "evil", "to": "beta",
        "kind": "message", "body": "fake"
    })
    invalid = store.list_invalid_messages()
    assert any(mid == bad_id and "evil" in reason for mid, reason in invalid)


# ----------------------------------------- Raw-JSON construction safety

def test_status_does_not_crash_on_missing_id_field(store: Store) -> None:
    """Regression for Codex's iter-1 blocker: a file with no `id`
    used to KeyError out of Message.from_dict and crash status."""
    (store.messages_dir / "noid.json").write_text(
        '{"from":"alpha","to":"beta","kind":"message","body":"x","meta":{}}',
        encoding="utf-8",
    )
    # Must not crash
    invalid = store.list_invalid_messages()
    assert any("missing required field" in r for _, r in invalid)
    # And messages_for must also tolerate
    assert store.messages_for("beta") == []


def test_status_does_not_crash_on_numeric_id(store: Store) -> None:
    """Regression: a numeric id passed Message construction and then
    crashed Store.advance_cursor's `int > str` comparison."""
    (store.messages_dir / "numid.json").write_text(
        '{"id":123,"ts":"2026-05-21T12:00:00Z","from":"alpha","to":"beta",'
        '"kind":"message","body":"x","meta":{}}',
        encoding="utf-8",
    )
    invalid = store.list_invalid_messages()
    assert any("must be a non-empty string" in r for _, r in invalid)
    assert store.messages_for("beta") == []


def test_status_does_not_crash_on_non_dict_meta(store: Store) -> None:
    """Regression: `meta: []` (truthy-falsey list) used to be coerced
    to `{}` and silently delivered as valid."""
    (store.messages_dir / "badmeta.json").write_text(
        '{"id":"20260521-120000-000000-AAAA","ts":"2026-05-21T12:00:00Z",'
        '"from":"alpha","to":"beta","kind":"message","body":"x","meta":[]}',
        encoding="utf-8",
    )
    invalid = store.list_invalid_messages()
    assert any("'meta' must be a dict" in r for _, r in invalid)
    assert store.messages_for("beta") == []


def test_status_does_not_crash_on_invalid_json(store: Store) -> None:
    """Regression: list_invalid_messages used to only see schema
    failures, not JSON parse failures."""
    (store.messages_dir / "broken.json").write_text("{ not valid json", encoding="utf-8")
    invalid = store.list_invalid_messages()
    assert any("invalid JSON" in r for _, r in invalid)


def test_status_does_not_crash_on_non_dict_root(store: Store) -> None:
    """Regression: a JSON file whose top-level value is an array used
    to KeyError on `data["id"]`."""
    (store.messages_dir / "list.json").write_text('["just","an","array"]', encoding="utf-8")
    invalid = store.list_invalid_messages()
    assert any("top-level value must be a JSON object" in r for _, r in invalid)


def test_status_does_not_crash_on_mixed_good_and_bad(store: Store) -> None:
    """The bus must continue processing valid messages even if some
    files alongside are corrupt."""
    store.send(sender="alpha", recipient="beta", body="legit")
    (store.messages_dir / "broken.json").write_text("garbage", encoding="utf-8")
    (store.messages_dir / "noid.json").write_text('{"foo":"bar"}', encoding="utf-8")
    msgs = store.messages_for("beta")
    assert len(msgs) == 1
    assert msgs[0].body == "legit"
    invalid = store.list_invalid_messages()
    assert len(invalid) == 2


# --------------------------------------------- Send-time kind validation

def test_send_rejects_unknown_kind(store: Store) -> None:
    """Regression for Codex's iter-1 blocker: `agenttalk send --kind
    typo` used to exit 0 + write a message that no listener would
    ever deliver. Now it raises at write time so the sender sees the
    failure immediately."""
    with pytest.raises(ValueError, match="unknown kind 'typo'"):
        store.send(sender="alpha", recipient="beta", body="x", kind="typo")


@pytest.mark.parametrize("kind", sorted(KNOWN_KINDS))
def test_send_accepts_every_known_kind(store: Store, kind: str) -> None:
    msg = store.send(sender="alpha", recipient="beta", body="x", kind=kind)
    assert msg.kind == kind


def test_cmd_send_unknown_kind_exits_2(
    store_root: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """End-to-end: CLI surfaces unknown-kind ValueError as exit 2."""
    from agenttalk import cli
    try:
        rc = cli.main(["--root", str(store_root), "send",
                       "--from", "alpha", "--to", "beta",
                       "--kind", "typo", "-m", "x"])
    except SystemExit as e:
        rc = 0 if e.code is None else int(e.code)
    assert rc == 2
    err = capsys.readouterr().err
    assert "unknown kind" in err


# ------------------------------------------------------ existing test

def test_cmd_status_surfaces_invalid_count(store_root: Path,
                                            capsys: pytest.CaptureFixture) -> None:
    """agenttalk status human output flags invalid-message counts so
    operators notice tampering during routine checks."""
    from agenttalk import cli
    s = Store(store_root)
    _hand_write_message(s, {"from": "evil", "to": "beta",
                            "kind": "message", "body": "fake"})
    cli.main(["--root", str(store_root), "status"])
    out = capsys.readouterr().out
    assert "INVALID:" in out
    assert "schema/roster validation" in out
    assert "status --json" in out