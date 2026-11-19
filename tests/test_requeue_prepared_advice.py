"""A vanished preparation needs kind-appropriate, executable recovery advice."""

import shlex

import pytest

from agenttalk import cli
from agenttalk import store as storage
from test_dead_letter import _rec, _requeue, _requeue_store, _send


def dead_letter(store, kind):
    correlation = {"in_reply_to": "20200101-000000-000000-Abcd"}
    if kind in {"task", "review-request", "review-result"}:
        correlation["request_id"] = "tk-original"
    if kind == "review-result":
        correlation["status"] = "approved"
    original = _send(store, "original body", kind=kind, meta=correlation)
    record = _rec(store)
    assert record["id"] == original.id
    store.dead_letter("beta", record, reason="fixture", failure_class="poison", at="2026-11-16T00:00:00Z")
    return original


def refuse_lost_preparation(monkeypatch, action):
    # Reproduce reset's removal at the actual prepare/publication boundary.
    # Run reset itself after requeue releases its acceptance/retirement locks.
    write = storage._write_text_exclusive

    def lose(path, text):
        identity = write(path, text)
        if path.suffix == ".pending":
            path.unlink()
        return identity

    with monkeypatch.context() as patch:
        patch.setattr(storage, "_write_text_exclusive", lose)
        assert action() == 2


@pytest.mark.parametrize("kind", ["review-request", "review-result", "rescind", "end"])
def test_vanished_requeue_advice_preserves_the_right_route(tmp_path, monkeypatch, capsys, kind):
    store = _requeue_store(tmp_path, monkeypatch, False)
    original = dead_letter(store, kind)
    payload = store.read_dead_letter_payload("beta", original.id)
    assert cli._dead_letter_resolution_state(store).get(("beta", original.id)) != "resolved"
    capsys.readouterr()
    refuse_lost_preparation(monkeypatch, lambda: _requeue(store, original))
    output = capsys.readouterr()
    assert not output.out
    assert "agenttalk dead-letter requeue: The prepared message is missing" in output.err
    assert not store.valid_messages()
    assert store._read_requeue_state("beta", original.id) is None
    assert cli._dead_letter_resolution_state(store).get(("beta", original.id)) != "resolved"
    assert store.read_dead_letter_payload("beta", original.id) == payload

    command = f"agenttalk dead-letter requeue --agent beta --id {original.id}"
    assert command in output.err
    assert "after the reset" in output.err.lower()
    assert "new request id" not in output.err
    assert "--meta request_id=" not in output.err
    assert "agenttalk send" not in output.err
    store.reset()
    assert cli.main(["--root", str(store.root), *shlex.split(command)[1:]]) == 0
    messages = store.valid_messages()
    assert len(messages) == 1
    copy = messages[0]
    assert (copy.kind, copy.sender, copy.recipient, copy.body) == (
        original.kind,
        original.sender,
        original.recipient,
        original.body,
    )
    assert copy.meta == {**original.meta, "requeued_from": original.id}
    assert copy.id != original.id
    assert store.read_dead_letter_payload("beta", original.id) == payload


@pytest.mark.parametrize("kind", ["review-result", "review-request", "task"])
def test_vanished_resolved_reply_keeps_the_original_reopen_options(tmp_path, monkeypatch, capsys, kind):
    store = _requeue_store(tmp_path, monkeypatch, False)
    original = dead_letter(store, kind)
    assert (
        cli.main(
            [
                "--root",
                str(store.root),
                "dead-letter",
                "resolve",
                "--from",
                "lead",
                "--agent",
                "beta",
                "--id",
                original.id,
                "--reason",
                "handled",
            ]
        )
        == 0
    )
    before = cli._dead_letter_resolution_state(store)
    assert before[("beta", original.id)] == "resolved"
    capsys.readouterr()
    refuse_lost_preparation(
        monkeypatch, lambda: _requeue(store, original, "--force-resolved", "--from", "lead", "--reason", "retry")
    )
    error = capsys.readouterr().err
    assert cli._dead_letter_resolution_state(store) == before
    assert store._read_requeue_state("beta", original.id) is None
    assert not store.valid_messages()
    assert "unresolved" not in error.lower()
    command = (
        f"agenttalk dead-letter requeue --agent beta --id {original.id} --force-resolved --from lead --reason retry"
    )
    assert command in error
    assert "new request id" not in error
    store.reset()
    assert cli.main(["--root", str(store.root), *shlex.split(command)[1:]]) == 0
    messages = store.valid_messages()
    assert len(messages) == 1
    assert messages[0].meta == {**original.meta, "requeued_from": original.id}
    assert cli._dead_letter_resolution_state(store)[("beta", original.id)] == "requeued"
