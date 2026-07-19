"""Fail-closed validation snapshot coverage (GH #44)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agenttalk import signing
from agenttalk.store import Store


def _message_path(store: Store, message_id: str) -> Path:
    return store.messages_dir / f"{message_id}.json"


def test_snapshot_reports_schema_reject_without_changing_valid_messages(
    store: Store,
) -> None:
    opener = store.send(
        sender="alpha",
        recipient="beta",
        kind="review-request",
        body="review this",
        meta={"request_id": "r-schema"},
    )
    rejection = store.send(
        sender="beta",
        recipient="alpha",
        kind="review-result",
        body="blocking finding",
        meta={"request_id": "r-schema", "status": "rejected"},
    )
    rejection_path = _message_path(store, rejection.id)
    raw = json.loads(rejection_path.read_text(encoding="utf-8"))
    raw["body"] = 17
    rejection_path.write_text(json.dumps(raw), encoding="utf-8")

    before = store.valid_messages()
    valid, problems = store.validated_messages_with_problems()
    after = store.valid_messages()

    assert [message.id for message in before] == [opener.id]
    assert valid == before == after
    assert problems == [
        {
            "id": rejection.id,
            "path": str(rejection_path),
            "reason": "message_schema_invalid",
        }
    ]


def test_snapshot_reports_sender_removed_from_roster(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTTALK_HMAC_KEY_FILE", str(tmp_path / "signing.key"))
    store = Store(tmp_path)
    store.init(["alpha", "beta"])
    signing.init_key(store.project_id())
    message = store.send(sender="alpha", recipient="beta", body="signed history")
    store.remove_agent("alpha")

    valid, problems = store.validated_messages_with_problems()

    assert valid == []
    assert problems == [
        {
            "id": message.id,
            "path": str(_message_path(store, message.id)),
            "reason": "message_roster_invalid",
        }
    ]
    assert store.valid_messages() == []


def test_snapshot_reports_unsigned_message_when_signing_becomes_enforced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTTALK_HMAC_KEY_FILE", str(tmp_path / "signing.key"))
    store = Store(tmp_path)
    store.init(["alpha", "beta"])
    message = store.send(sender="alpha", recipient="beta", body="legacy unsigned")
    signing.init_key(store.project_id())

    valid, problems = store.validated_messages_with_problems()

    assert valid == []
    assert problems == [
        {
            "id": message.id,
            "path": str(_message_path(store, message.id)),
            "reason": "message_signature_invalid",
        }
    ]
    assert store.valid_messages() == []


def test_snapshot_surfaces_ordered_but_absent_without_healing(store: Store) -> None:
    survivor = store.send(sender="alpha", recipient="beta", body="keep")
    deleted = store.send(sender="beta", recipient="alpha", body="delete")
    deleted_path = _message_path(store, deleted.id)
    order_before = store._message_publication_order_path.read_bytes()
    anchor_before = store._message_publication_order_anchor_path.read_bytes()
    deleted_path.unlink()

    valid, problems = store.validated_messages_with_problems()

    assert [message.id for message in valid] == [survivor.id]
    assert problems == [
        {
            "id": deleted.id,
            "path": str(deleted_path),
            "reason": "publication_ordered_message_absent",
        }
    ]
    assert store._message_publication_order_path.read_bytes() == order_before
    assert store._message_publication_order_anchor_path.read_bytes() == anchor_before


def test_snapshot_uses_one_canonical_message_traversal(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store.send(sender="alpha", recipient="beta", body="one walk")
    original = store._scan_messages_with_paths
    call_count = 0

    def counted_scan(
        *, since_id: str | None = None,
    ) -> tuple[list[tuple[object, Path]], list[tuple[Path, str, str]]]:
        nonlocal call_count
        call_count += 1
        return original(since_id=since_id)

    monkeypatch.setattr(store, "_scan_messages_with_paths", counted_scan)

    valid, problems = store.validated_messages_with_problems()

    assert len(valid) == 1
    assert problems == []
    assert call_count == 1
