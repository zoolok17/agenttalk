"""Tests for HMAC-SHA256 signing.

Covers the canonicalization-as-wire-format invariant (a "golden"
test), key file I/O and modes, sign/verify round-trip, the
specific failure modes (missing/wrong-version/wrong-key), the
Store integration (sign-on-send + verify-on-read when
``require_signatures`` is enabled), and the threat-model
limits documented in SECURITY.md.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agenttalk import signing
from agenttalk.store import Store


# ----------------------------------------------------- canonical payload

def test_canonical_payload_format_is_stable() -> None:
    """Golden test: lock the canonical-JSON format that gets HMAC'd.
    A regression here is a WIRE-FORMAT change that breaks every
    deployed signed message — never break this lightly.
    """
    msg = {
        "id": "20260521-130000-000000-AAAA",
        "ts": "2026-05-21T13:00:00.000000Z",
        "from": "alpha",
        "to": "beta",
        "kind": "message",
        "subject": "",
        "body": "hello",
        "meta": {"request_id": "abc-123"},
    }
    expected = (
        b'{"body":"hello","from":"alpha","id":"20260521-130000-000000-AAAA",'
        b'"kind":"message","meta":{"request_id":"abc-123"},"subject":"",'
        b'"to":"beta","ts":"2026-05-21T13:00:00.000000Z"}'
    )
    assert signing.canonical_payload(msg) == expected


def test_canonical_payload_excludes_signature_field_only() -> None:
    """Signature field is removed from the signed bytes; other meta
    keys (signature_version, alg, key_id, signed_at) ARE signed."""
    msg = {
        "id": "x", "ts": "y", "from": "alpha", "to": "beta",
        "kind": "message", "subject": "", "body": "",
        "meta": {
            "signature_version": "v1",
            "signature_alg": "hmac-sha256",
            "key_id": "fixed-id",
            "signed_at": "2026-05-21T13:00:00Z",
            "signature": "SHOULD NOT BE IN OUTPUT",
        },
    }
    out = signing.canonical_payload(msg)
    assert b"signature_version" in out
    assert b"hmac-sha256" in out
    assert b"fixed-id" in out
    assert b"SHOULD NOT BE IN OUTPUT" not in out


def test_canonical_payload_preserves_unicode() -> None:
    """ensure_ascii=False so byte-equality holds across decoders."""
    msg = {
        "id": "x", "ts": "y", "from": "alpha", "to": "beta",
        "kind": "message", "subject": "", "body": "approved → ship",
        "meta": {},
    }
    out = signing.canonical_payload(msg)
    assert "approved → ship".encode("utf-8") in out
    assert b"\\u2192" not in out


def test_canonical_payload_meta_signature_removal_is_pure() -> None:
    """Caller's dict must NOT be mutated by canonicalization."""
    msg = {"id": "x", "ts": "y", "from": "a", "to": "b",
           "kind": "message", "subject": "", "body": "",
           "meta": {"signature": "x", "other": "y"}}
    signing.canonical_payload(msg)
    assert msg["meta"] == {"signature": "x", "other": "y"}


# ------------------------------------------------------ sign / verify

def _msg_dict(**over) -> dict:
    base = {
        "id": "20260521-130000-000000-AAAA",
        "ts": "2026-05-21T13:00:00.000000Z",
        "from": "alpha",
        "to": "beta",
        "kind": "message",
        "subject": "",
        "body": "hi",
        "meta": {},
    }
    base.update(over)
    return base


def test_sign_then_verify_round_trips() -> None:
    key = b"\x00" * 32
    signed = signing.sign_message(_msg_dict(), key, key_id="kid-1")
    signing.verify_message(signed, key, expected_key_id="kid-1")  # no raise


def test_sign_attaches_all_v1_metadata_fields() -> None:
    key = b"\x00" * 32
    signed = signing.sign_message(_msg_dict(), key, key_id="kid-1")
    meta = signed["meta"]
    assert meta["signature_version"] == "v1"
    assert meta["signature_alg"] == "hmac-sha256"
    assert meta["key_id"] == "kid-1"
    assert "signed_at" in meta
    assert meta["signature"]  # hex-encoded HMAC


def test_sign_is_idempotent_replaces_existing_signature() -> None:
    key = b"\x00" * 32
    first = signing.sign_message(_msg_dict(), key, key_id="kid-1")
    second = signing.sign_message(first, key, key_id="kid-1",
                                  signed_at=first["meta"]["signed_at"])
    # Same input + same signed_at => identical signature
    assert first["meta"]["signature"] == second["meta"]["signature"]


def test_verify_rejects_missing_signature() -> None:
    key = b"\x00" * 32
    with pytest.raises(ValueError, match="missing signature"):
        signing.verify_message(_msg_dict(), key)


def test_verify_rejects_wrong_key() -> None:
    real = b"\x00" * 32
    impostor = b"\x01" * 32
    signed = signing.sign_message(_msg_dict(), real, key_id="kid-1")
    with pytest.raises(ValueError, match="signature mismatch"):
        signing.verify_message(signed, impostor)


def test_verify_rejects_tampered_body() -> None:
    """Flipping a single bit in the body invalidates the signature —
    the whole point of the defense."""
    key = b"\x00" * 32
    signed = signing.sign_message(_msg_dict(body="legit"), key, key_id="kid-1")
    tampered = dict(signed)
    tampered["body"] = "tampered"
    with pytest.raises(ValueError, match="signature mismatch"):
        signing.verify_message(tampered, key)


def test_verify_rejects_unsupported_version() -> None:
    key = b"\x00" * 32
    signed = signing.sign_message(_msg_dict(), key, key_id="kid-1")
    signed["meta"]["signature_version"] = "v999"
    with pytest.raises(ValueError, match="unsupported signature_version"):
        signing.verify_message(signed, key)


def test_verify_rejects_wrong_key_id_when_expected() -> None:
    key = b"\x00" * 32
    signed = signing.sign_message(_msg_dict(), key, key_id="kid-1")
    with pytest.raises(ValueError, match="key_id mismatch"):
        signing.verify_message(signed, key, expected_key_id="other-kid")


def test_sign_rejects_short_key() -> None:
    with pytest.raises(ValueError, match="at least 16 bytes"):
        signing.sign_message(_msg_dict(), b"short", key_id="x")


# ---------------------------------------------------------- key file I/O

def test_init_key_writes_a_readable_hex_key(tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTTALK_HMAC_KEY_FILE", str(tmp_path / "test.key"))
    path = signing.init_key("pid-1")
    assert path.exists()
    key = signing.load_key("pid-1")
    assert len(key) == 32


def test_init_key_refuses_to_overwrite_without_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTTALK_HMAC_KEY_FILE", str(tmp_path / "k.key"))
    signing.init_key("pid-1")
    with pytest.raises(FileExistsError, match="key already exists"):
        signing.init_key("pid-1")


def test_init_key_overwrites_with_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTTALK_HMAC_KEY_FILE", str(tmp_path / "k.key"))
    signing.init_key("pid-1")
    first = signing.load_key("pid-1")
    signing.init_key("pid-1", force=True)
    second = signing.load_key("pid-1")
    assert first != second


@pytest.mark.skipif(os.name == "nt",
                    reason="POSIX file mode bits don't apply on Windows")
def test_init_key_sets_posix_mode_0600(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTTALK_HMAC_KEY_FILE", str(tmp_path / "k.key"))
    path = signing.init_key("pid-1")
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600, (
        f"key file mode is {oct(mode)}; must be 0o600 — broader perms "
        f"defeat the HMAC defense"
    )


def test_load_key_raises_on_missing(tmp_path: Path,
                                     monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTTALK_HMAC_KEY_FILE", str(tmp_path / "ghost.key"))
    with pytest.raises(FileNotFoundError, match="no key file at"):
        signing.load_key("pid-1")


def test_load_key_rejects_garbage_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    p = tmp_path / "bad.key"
    p.write_text("not hex at all", encoding="utf-8")
    monkeypatch.setenv("AGENTTALK_HMAC_KEY_FILE", str(p))
    with pytest.raises(ValueError, match="not a valid hex"):
        signing.load_key("pid-1")


# ----- weak-key rejection (review C*: sign enforces >=16 bytes, the
# verify/load/inspect side must too, else a degenerate key yields a
# forgeable-but-"enforced" state and a falsely-green doctor) ----------

def test_load_key_rejects_empty_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    p = tmp_path / "empty.key"
    p.write_text("", encoding="utf-8")  # decodes to b"" — too short
    monkeypatch.setenv("AGENTTALK_HMAC_KEY_FILE", str(p))
    with pytest.raises(ValueError, match="16 bytes"):
        signing.load_key("pid-1")


def test_load_key_rejects_whitespace_only_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    p = tmp_path / "ws.key"
    p.write_text("   \n", encoding="utf-8")  # strips to "" -> b""
    monkeypatch.setenv("AGENTTALK_HMAC_KEY_FILE", str(p))
    with pytest.raises(ValueError, match="16 bytes"):
        signing.load_key("pid-1")


def test_load_key_rejects_short_hex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    p = tmp_path / "short.key"
    p.write_text("00", encoding="utf-8")  # valid hex, 1 byte — too short
    monkeypatch.setenv("AGENTTALK_HMAC_KEY_FILE", str(p))
    with pytest.raises(ValueError, match="16 bytes"):
        signing.load_key("pid-1")


def test_verify_message_rejects_short_key() -> None:
    """verify_message must reject an undersized key the same way
    sign_message does, so the invariant is local to verification."""
    with pytest.raises(ValueError, match="16 bytes"):
        signing.verify_message({"meta": {}}, b"short")


def test_inspect_key_flags_empty_key_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    p = tmp_path / "empty.key"
    p.write_text("", encoding="utf-8")
    monkeypatch.setenv("AGENTTALK_HMAC_KEY_FILE", str(p))
    h = signing.inspect_key("pid-1", project_root=tmp_path.parent)
    assert h.exists and h.readable
    assert not h.valid
    assert h.key_error and "16 bytes" in h.key_error


def test_inspect_key_flags_garbage_key_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    p = tmp_path / "bad.key"
    p.write_text("not hex at all", encoding="utf-8")
    monkeypatch.setenv("AGENTTALK_HMAC_KEY_FILE", str(p))
    h = signing.inspect_key("pid-1", project_root=tmp_path.parent)
    assert h.exists and h.readable
    assert not h.valid
    assert h.key_error and "hex" in h.key_error


def test_inspect_key_good_key_is_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTTALK_HMAC_KEY_FILE", str(tmp_path / "k.key"))
    signing.init_key("pid-1")
    h = signing.inspect_key("pid-1", project_root=tmp_path.parent)
    assert h.exists and h.readable
    assert h.valid
    assert h.key_error is None


# ---------------------------------------------------- inspect_key health

def test_inspect_key_reports_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTTALK_HMAC_KEY_FILE", str(tmp_path / "absent.key"))
    h = signing.inspect_key("pid-1", project_root=tmp_path)
    assert not h.exists
    assert not h.readable
    assert h.mode_warning is None


def test_inspect_key_reports_present_and_secure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTTALK_HMAC_KEY_FILE", str(tmp_path / "k.key"))
    signing.init_key("pid-1")
    h = signing.inspect_key("pid-1", project_root=tmp_path.parent)
    assert h.exists
    assert h.readable
    assert h.mode_warning is None


def test_inspect_key_flags_when_inside_project_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Storing the key INSIDE the project dir defeats the threat
    model — doctor/status should flag this loudly."""
    project_root = tmp_path / "myproject"
    project_root.mkdir()
    key_path = project_root / ".agenttalk" / "leaked.key"
    monkeypatch.setenv("AGENTTALK_HMAC_KEY_FILE", str(key_path))
    signing.init_key("pid-1")
    h = signing.inspect_key("pid-1", project_root=project_root)
    assert h.in_project_dir


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes only")
def test_inspect_key_warns_on_loose_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    p = tmp_path / "loose.key"
    p.write_text("00" * 32, encoding="utf-8")
    p.chmod(0o644)
    monkeypatch.setenv("AGENTTALK_HMAC_KEY_FILE", str(p))
    h = signing.inspect_key("pid-1", project_root=tmp_path)
    assert h.mode_warning is not None
    assert "0o600" in h.mode_warning


# ---------------------------------------------------------- Store integration

def test_store_init_no_longer_writes_project_id_to_config(tmp_path: Path) -> None:
    """v0.6.0-iter-2 review found that storing project_id in
    attacker-writable config.json was bypassable. project_id is
    now path-derived; config.json doesn't carry it."""
    s = Store(tmp_path)
    cfg = s.init(["alpha", "beta"])
    assert "project_id" not in cfg
    # But the Store still has one (derived from self.root)
    assert s.project_id()  # non-empty
    # Enforcement is OFF until a key file exists
    assert s.signing_enforced() is False


def test_project_id_is_stable_across_calls(tmp_path: Path) -> None:
    """Same root => same project_id, every call."""
    s1 = Store(tmp_path)
    s1.init(["alpha", "beta"])
    s2 = Store(tmp_path)
    assert s1.project_id() == s2.project_id()


def test_project_id_differs_between_unrelated_projects(tmp_path: Path) -> None:
    """Two project roots get distinct IDs so their keys don't collide."""
    a = tmp_path / "proj-a"
    b = tmp_path / "proj-b"
    a.mkdir()
    b.mkdir()
    Store(a).init(["alpha", "beta"])
    Store(b).init(["alpha", "beta"])
    assert Store(a).project_id() != Store(b).project_id()


def test_signing_enforced_flips_on_when_key_file_appears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for v0.6.0 iter-1 BLOCKER: enforcement must NOT
    depend on .agenttalk/config.json (which is attacker-writable).
    The trust anchor is the per-user key file's existence at the
    PATH-DERIVED project_id."""
    monkeypatch.setenv("AGENTTALK_HMAC_KEY_FILE", str(tmp_path / "k.key"))
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    assert s.signing_enforced() is False
    signing.init_key(s.project_id())  # path-derived, not from config
    assert s.signing_enforced() is True


def test_key_path_probe_error_does_not_admit_unsigned_messages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    from agenttalk.store import _new_id, _now_iso
    forged = {
        "id": _new_id(), "ts": _now_iso(),
        "from": "alpha", "to": "beta", "kind": "message",
        "subject": "", "body": "UNSIGNED PROBE BYPASS", "meta": {},
    }
    (s.messages_dir / f"{forged['id']}.json").write_text(
        json.dumps(forged), encoding="utf-8")

    def fail_probe(_project_id: str, **_kwargs) -> Path:
        raise OSError("injected key-path probe failure")

    monkeypatch.setattr(signing, "resolve_key_path", fail_probe)
    assert s.messages_for("beta") == []
    assert any("signature" in reason for _, reason in s.list_invalid_messages())


def test_dangling_key_symlink_does_not_admit_unsigned_messages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_path = tmp_path / "dangling.key"
    try:
        key_path.symlink_to(tmp_path / "missing-target.key")
    except OSError:
        if os.name == "nt":
            pytest.skip("creating symlinks may require Windows developer mode or privilege")
        raise
    monkeypatch.setenv("AGENTTALK_HMAC_KEY_FILE", str(key_path))
    s = Store(tmp_path / "project")
    s.init(["alpha", "beta"])
    from agenttalk.store import _new_id, _now_iso
    forged = {
        "id": _new_id(), "ts": _now_iso(),
        "from": "alpha", "to": "beta", "kind": "message",
        "subject": "", "body": "UNSIGNED DANGLING SYMLINK", "meta": {},
    }
    (s.messages_dir / f"{forged['id']}.json").write_text(
        json.dumps(forged), encoding="utf-8")

    assert s.signing_enforced() is True
    assert s.messages_for("beta") == []


def test_config_tampering_cannot_disable_enforcement_via_require_signatures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Iter-1 repro: editing require_signatures=false in config.json
    used to disable enforcement. Now ignored."""
    monkeypatch.setenv("AGENTTALK_HMAC_KEY_FILE", str(tmp_path / "k.key"))
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    signing.init_key(s.project_id())
    raw = json.loads(s.config_path.read_text(encoding="utf-8"))
    raw["require_signatures"] = False
    s.config_path.write_text(json.dumps(raw), encoding="utf-8")
    assert s.signing_enforced() is True  # path-derived; config can't disable
    from agenttalk.store import _new_id, _now_iso
    forged = {
        "id": _new_id(), "ts": _now_iso(),
        "from": "alpha", "to": "beta",
        "kind": "end", "subject": "", "body": "UNSIGNED CONFIG BYPASS",
        "meta": {},
    }
    (s.messages_dir / f"{forged['id']}.json").write_text(
        json.dumps(forged), encoding="utf-8")
    assert s.messages_for("beta") == []
    invalid = s.list_invalid_messages()
    assert any("missing signature" in r for _, r in invalid)


def test_config_tampering_cannot_disable_enforcement_via_project_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Iter-2 BLOCKER repro: editing project_id in config.json to a
    UUID with no key file used to make signing_enforced() return
    False. Now project_id is path-derived — config edits don't
    affect what the verifier checks."""
    monkeypatch.setenv("AGENTTALK_HMAC_KEY_FILE", str(tmp_path / "k.key"))
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    signing.init_key(s.project_id())
    # Attacker drops a fake project_id into config.json
    raw = json.loads(s.config_path.read_text(encoding="utf-8"))
    raw["project_id"] = "00000000-0000-0000-0000-000000000000"
    s.config_path.write_text(json.dumps(raw), encoding="utf-8")
    # Path-derived project_id is unchanged
    assert s.project_id() != "00000000-0000-0000-0000-000000000000"
    # Enforcement is still on
    assert s.signing_enforced() is True
    # Forged unsigned end addressed to beta gets rejected
    from agenttalk.store import _new_id, _now_iso
    forged = {
        "id": _new_id(), "ts": _now_iso(),
        "from": "alpha", "to": "beta",
        "kind": "end", "subject": "", "body": "UNSIGNED PROJECT_ID BYPASS",
        "meta": {},
    }
    (s.messages_dir / f"{forged['id']}.json").write_text(
        json.dumps(forged), encoding="utf-8")
    assert s.messages_for("beta") == []
    invalid = s.list_invalid_messages()
    assert any("missing signature" in r for _, r in invalid)


def test_send_attaches_signature_when_key_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTTALK_HMAC_KEY_FILE", str(tmp_path / "k.key"))
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    signing.init_key(s.project_id())
    msg = s.send(sender="alpha", recipient="beta", body="signed!")
    assert "signature" in msg.meta
    assert msg.meta["signature_alg"] == "hmac-sha256"
    assert msg.meta["key_id"] == s.project_id()


def test_send_with_key_present_but_unreadable_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The key path is set but the file is missing — refuse to send
    rather than silently drop the signature."""
    bad_path = tmp_path / "absent.key"
    monkeypatch.setenv("AGENTTALK_HMAC_KEY_FILE", str(bad_path))
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    signing.init_key(s.project_id())
    bad_path.unlink()
    with pytest.raises(ValueError, match="cannot sign"):
        s.send(sender="alpha", recipient="beta", body="x", sign=True)


def test_messages_for_skips_unsigned_when_key_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTTALK_HMAC_KEY_FILE", str(tmp_path / "k.key"))
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    s.send(sender="alpha", recipient="beta", body="unsigned legacy msg")
    assert len(s.messages_for("beta")) == 1
    signing.init_key(s.project_id())
    assert s.signing_enforced()
    assert s.messages_for("beta") == []
    invalid = s.list_invalid_messages()
    assert any("missing signature" in r for _, r in invalid)


def test_messages_for_skips_forged_signature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTTALK_HMAC_KEY_FILE", str(tmp_path / "k.key"))
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    signing.init_key(s.project_id())
    from agenttalk.store import _new_id, _now_iso
    forged = {
        "id": _new_id(), "ts": _now_iso(),
        "from": "alpha", "to": "beta",
        "kind": "message", "subject": "", "body": "forged",
        "meta": {
            "signature_version": "v1",
            "signature_alg": "hmac-sha256",
            "key_id": s.project_id(),
            "signed_at": "2026-05-21T00:00:00Z",
            "signature": "deadbeef" * 8,
        },
    }
    (s.messages_dir / f"{forged['id']}.json").write_text(
        json.dumps(forged), encoding="utf-8")
    assert s.messages_for("beta") == []
    invalid = s.list_invalid_messages()
    assert any("signature mismatch" in r for _, r in invalid)


def test_messages_for_accepts_valid_signature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTTALK_HMAC_KEY_FILE", str(tmp_path / "k.key"))
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    signing.init_key(s.project_id())
    s.send(sender="alpha", recipient="beta", body="legit")
    msgs = s.messages_for("beta")
    assert len(msgs) == 1
    assert msgs[0].body == "legit"


def test_tail_renders_valid_signed_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """Regression for v0.6.0 iter-3 BLOCKER: when the project_id
    refactor removed cfg["project_id"], cmd_tail was still reading
    it from there. A fresh signed project then had no project_id,
    so the key load failed and every valid signed message got
    marked invalid. Tail must use store.project_id() like every
    other verify path."""
    monkeypatch.setenv("AGENTTALK_HMAC_KEY_FILE", str(tmp_path / "k.key"))
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    signing.init_key(s.project_id())
    s.send(sender="alpha", recipient="beta", body="VALID_SIGNED_MSG")
    capsys.readouterr()
    from agenttalk import cli
    cli.main(["--root", str(tmp_path), "tail", "--from-start",
              "--timeout", "0.3", "--interval", "0.1"])
    out = capsys.readouterr()
    assert "VALID_SIGNED_MSG" in out.out, (
        "tail should have rendered the valid signed message — "
        f"got stdout={out.out!r} stderr={out.err!r}"
    )
    assert "TAIL INVALID" not in out.err


def test_tail_does_not_render_invalid_signature_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """Regression for v0.6.0 iter-1 BLOCKER #2: cmd_tail used to
    call _scan_messages + m.validate(roster) and render directly,
    bypassing the HMAC verifier. Forged-signature bodies are now
    surfaced as TAIL INVALID warnings on stderr, never rendered."""
    monkeypatch.setenv("AGENTTALK_HMAC_KEY_FILE", str(tmp_path / "k.key"))
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    signing.init_key(s.project_id())
    from agenttalk.store import _new_id, _now_iso
    forged_id = _new_id()
    forged = {
        "id": forged_id, "ts": _now_iso(),
        "from": "alpha", "to": "beta",
        "kind": "message", "subject": "", "body": "TAIL SIGNATURE BYPASS",
        "meta": {
            "signature_version": "v1",
            "signature_alg": "hmac-sha256",
            "key_id": s.project_id(),
            "signed_at": "2026-05-21T00:00:00Z",
            "signature": "deadbeef" * 8,
        },
    }
    (s.messages_dir / f"{forged_id}.json").write_text(
        json.dumps(forged), encoding="utf-8")
    capsys.readouterr()
    from agenttalk import cli
    cli.main(["--root", str(tmp_path), "tail", "--from-start",
              "--timeout", "0.3", "--interval", "0.1"])
    captured = capsys.readouterr()
    assert "TAIL SIGNATURE BYPASS" not in captured.out
    assert "TAIL SIGNATURE BYPASS" not in captured.err
    assert "TAIL INVALID" in captured.err
    assert forged_id in captured.err


def test_signatures_off_default_still_works(tmp_path: Path) -> None:
    """Zero-setup path: a fresh project with default config never
    asks about signatures or keys. Backwards compat with 0.5.x."""
    s = Store(tmp_path)
    s.init(["alpha", "beta"])  # no require_signatures
    s.send(sender="alpha", recipient="beta", body="ordinary")
    msgs = s.messages_for("beta")
    assert len(msgs) == 1
    assert "signature" not in msgs[0].meta


# ------------------------------------ 0.18.0 BLOCKER: non-string signature

def test_verify_rejects_non_string_signature_as_valueerror() -> None:
    """A non-string signature value (e.g. a JSON list smuggled into a
    file) must raise ValueError, NOT a TypeError out of compare_digest.
    The TypeError would escape every read-path `except ValueError` and
    crash the whole bus (0.18.0 BLOCKER)."""
    key = b"\x00" * 32
    signed = signing.sign_message(_msg_dict(), key, key_id="kid-1")
    signed["meta"]["signature"] = [1, 2, 3]   # wrong JSON type
    with pytest.raises(ValueError, match="signature is not a string"):
        signing.verify_message(signed, key)


def test_poison_signature_degrades_gracefully(tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    """With signing enforced, a poison-signature file must NOT crash the
    read paths; it is reported invalid and a legit message still delivers."""
    monkeypatch.setenv("AGENTTALK_HMAC_KEY_FILE", str(tmp_path / "k.key"))
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    signing.init_key(s.project_id())
    s.send(sender="alpha", recipient="beta", body="legit")
    poison = {
        "id": "20990101-000000-000000-PSNx", "ts": "2099-01-01T00:00:00Z",
        "from": "alpha", "to": "beta", "kind": "message", "subject": "",
        "body": "x", "meta": {"signature": [1, 2, 3], "signature_version": "v1",
                              "signature_alg": "hmac-sha256",
                              "key_id": s.project_id()},
    }
    (s.messages_dir / "20990101-000000-000000-PSNx.json").write_text(
        json.dumps(poison), encoding="utf-8")
    # none of these crash:
    assert any(m.body == "legit" for m in s.messages_for("beta"))
    assert s.current_epoch() is None
    invalid_ids = {mid for mid, _ in s.list_invalid_messages()}
    assert "20990101-000000-000000-PSNx" in invalid_ids
