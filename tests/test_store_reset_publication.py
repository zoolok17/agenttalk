"""A prepared send must survive the wait for publication before it books history."""

import contextlib
import json
import os
import threading

import pytest

from agenttalk import cli, signing
from agenttalk import store as storage
from agenttalk.store import Store


def refusal(reset=False):
    cause = "The store was reset" if reset else "The store may have been reset"
    return (
        "The prepared message is missing or has changed while waiting to be published; "
        f"nothing was published. {cause}; send it again."
    )


@pytest.fixture
def waiting_send(store, monkeypatch):
    """Pause only the selected sender at entry, before taking the real lock."""
    ready, resume = threading.Event(), threading.Event()
    staged, result = {}, {}
    write, publication = storage._write_text_exclusive, Store._message_publication_lock
    worker = None

    def capture(path, text):
        identity = write(path, text)
        if threading.current_thread() is worker and path.suffix == ".pending":
            staged.update(path=path, identity=identity, payload=text)
        return identity

    @contextlib.contextmanager
    def gate(self):
        if threading.current_thread() is worker and staged and not ready.is_set():
            ready.set()
            assert resume.wait(15), "sender was not released"
        with publication(self):
            yield

    monkeypatch.setattr(storage, "_write_text_exclusive", capture)
    monkeypatch.setattr(Store, "_message_publication_lock", gate)

    def start(action=None, **kwargs):
        nonlocal worker

        def run():
            try:
                result["value"] = (
                    action()
                    if action
                    else store.send(sender="alpha", recipient="beta", body="waiting payload", **kwargs)
                )
            except Exception as exc:
                result["error"] = exc

        worker = threading.Thread(target=run)
        worker.start()
        assert ready.wait(15), result
        assert staged["path"].is_file()
        return staged

    def finish():
        resume.set()
        worker.join(15)
        assert not worker.is_alive(), "sender did not finish"
        return result

    yield start, finish
    resume.set()
    if worker:
        worker.join(15)
        assert not worker.is_alive()


def publication_snapshot(store):
    return tuple(
        p.read_bytes() if p.exists() else None
        for p in (store._message_publication_order_path, store._message_publication_order_anchor_path)
    )


@pytest.mark.parametrize("archive", [False, True])
def test_reset_refuses_without_booking_history(store, waiting_send, monkeypatch, archive):
    start, finish = waiting_send
    staged = start()
    store.reset(archive=archive)
    before = publication_snapshot(store)
    reservations = []
    reserve = store._reserve_message_publication_sequence

    def record(*args):
        reservations.append(args)
        return reserve(*args)

    monkeypatch.setattr(store, "_reserve_message_publication_sequence", record)
    result = finish()
    assert publication_snapshot(store) == before
    assert not reservations
    assert isinstance(result.get("error"), ValueError)
    assert str(result["error"]) == refusal(True)
    assert not staged["path"].exists()
    assert store.validated_messages_with_problems()[1] == []
    fresh = store.send(sender="alpha", recipient="beta", body="fresh")
    order = json.loads(store._message_publication_order_path.read_text())
    assert len(order["messages"]) == 1
    assert order["messages"] == {fresh.id: 1}


@pytest.mark.parametrize("replacement", ["file", "symlink"])
def test_replacement_is_refused_and_preserved(store, waiting_send, replacement):
    start, finish = waiting_send
    staged = start()
    path = staged["path"]
    other = path.with_suffix(".kept")
    if replacement == "file":
        other.write_bytes(b"replacement evidence")
        os.replace(other, path)
        expected = b"replacement evidence"
    else:
        os.replace(path, other)
        try:
            path.symlink_to(other)
        except OSError as exc:
            pytest.skip(f"symbolic links unavailable: {exc}")
        expected = other.read_bytes()
    before = publication_snapshot(store)
    result = finish()
    assert path.read_bytes() == expected
    assert publication_snapshot(store) == before
    assert isinstance(result.get("error"), ValueError)
    assert str(result["error"]) == refusal()
    assert not list(store.messages_dir.glob("*.json"))


@pytest.mark.parametrize("replace", [False, True])
def test_lost_requeue_preparation_makes_no_claim(store, waiting_send, monkeypatch, replace):
    calls = []
    claim = store._claim_requeue

    def record(*args):
        calls.append(args)
        return claim(*args)

    monkeypatch.setattr(store, "_claim_requeue", record)
    start, finish = waiting_send
    staged = start(_requeue_source=("beta", "dead-772"))
    path = staged["path"]
    if replace:
        other = path.with_suffix(".replacement")
        other.write_bytes(b"keep")
        os.replace(other, path)
    else:
        path.unlink()
    before = publication_snapshot(store)
    state = store._requeue_state_path("beta", "dead-772")
    result = finish()
    assert not calls
    assert not state.exists()
    assert publication_snapshot(store) == before
    assert str(result.get("error")) == refusal()
    assert not list(store.messages_dir.glob("*.json"))


def test_identity_read_error_is_not_diagnosed_as_reset(store, waiting_send, monkeypatch):
    start, finish = waiting_send
    staged = start()
    lstat = os.lstat

    def denied(path, *args, **kwargs):
        if path == staged["path"]:
            raise PermissionError("identity read denied")
        return lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", denied)
    before = publication_snapshot(store)
    result = finish()
    assert publication_snapshot(store) == before
    assert type(result.get("error")) is PermissionError
    assert str(result["error"]) == "identity read denied"
    assert not list(store.messages_dir.glob("*.json"))


@pytest.mark.parametrize("signed", [False, True])
def test_healthy_payload_is_accepted_after_writing(store, monkeypatch, tmp_path, signed):
    monkeypatch.setenv("AGENTTALK_HMAC_KEY_FILE", str(tmp_path / "key"))
    if signed:
        signing.init_key(store.project_id())
    for body in ("first payload", "second payload"):
        msg = store.send(sender="alpha", recipient="beta", body=body, sign=signed)
        assert msg.body == body
    messages, problems = store.validated_messages_with_problems()
    assert not problems
    assert [m.body for m in messages] == ["first payload", "second payload"]
    assert len(json.loads(store._message_publication_order_path.read_text())["messages"]) == 2
    assert not list(store.messages_dir.glob("*.pending"))


def test_intact_preparation_reaches_earlier_session_requeue_refusal(store):
    state = store._requeue_state_path("beta", "dead-772")
    store._write_requeue_state(
        "beta",
        "dead-772",
        {"state": "claimed", "requeued_as": "20200101-000000-000000-Abcd", "session_id": "20200101T000000Z"},
    )
    before = state.read_bytes()
    with pytest.raises(storage.RequeueRefused, match="earlier session"):
        store.send(sender="alpha", recipient="beta", body="retry", _requeue_source=("beta", "dead-772"))
    assert state.read_bytes() == before
    assert publication_snapshot(store) == (None, None)


def test_final_rename_denied_preserves_payload_and_order(store, monkeypatch):
    replace = os.replace

    def denied(source, target):
        if str(source).endswith(".pending"):
            raise PermissionError("message rename denied")
        return replace(source, target)

    monkeypatch.setattr(os, "replace", denied)
    msg = store.send(sender="alpha", recipient="beta", body="fallback payload")
    messages, problems = store.validated_messages_with_problems()
    assert not problems
    assert [(m.id, m.body) for m in messages] == [(msg.id, "fallback payload")]
    assert json.loads(store._message_publication_order_path.read_text())["messages"] == {msg.id: 1}


def test_intact_requeue_reloads_in_place_config_revision(store, waiting_send):
    store._write_requeue_state(
        "beta",
        "dead-772",
        {
            "state": "claimed",
            "requeued_as": "20200101-000000-000000-Abcd",
            "session_id": store.load_config()["session_id"],
        },
    )
    state = store._requeue_state_path("beta", "dead-772")
    before = state.read_bytes()
    start, finish = waiting_send
    start(_requeue_source=("beta", "dead-772"))
    revision = store.config_path.stat()
    payload = store.config_path.read_bytes()
    old_session = store.load_config()["session_id"]
    store.config_path.write_bytes(payload.replace(old_session.encode(), ("2000" + old_session[4:]).encode()))
    os.utime(store.config_path, ns=(revision.st_atime_ns, revision.st_mtime_ns))
    assert storage._file_revision(store.config_path.stat()) == storage._file_revision(revision)
    result = finish()
    assert isinstance(result.get("error"), storage.RequeueRefused)
    assert "earlier session" in str(result["error"])
    assert state.read_bytes() == before
    assert publication_snapshot(store) == (None, None)


def test_cli_reset_refusal_has_no_success(store, waiting_send, capsys):
    start, finish = waiting_send
    start(
        action=lambda: cli.main(["--root", str(store.root), "send", "--from", "alpha", "--to", "beta", "-m", "waiting"])
    )
    store.reset()
    before = publication_snapshot(store)
    result = finish()
    output = capsys.readouterr()
    assert publication_snapshot(store) == before
    assert result == {"value": 2}
    assert refusal(True) in output.err
    assert not output.out


def test_requeue_cli_refusal_includes_route(tmp_path, monkeypatch, capsys):
    from test_dead_letter import _dead_payload, _requeue, _requeue_store, _send

    store = _requeue_store(tmp_path, monkeypatch, False)
    original = _send(store, "work", kind="task", meta={"request_id": "tk-772"})
    _dead_payload(store, original)
    capsys.readouterr()
    write = storage._write_text_exclusive

    def lose_preparation(path, text):
        identity = write(path, text)
        if path.suffix == ".pending":
            path.unlink()
        return identity

    monkeypatch.setattr(storage, "_write_text_exclusive", lose_preparation)
    before = publication_snapshot(store)
    assert _requeue(store, original) == 2
    output = capsys.readouterr()
    assert "agenttalk dead-letter requeue: " + refusal() in output.err
    assert "new request" in output.err
    assert publication_snapshot(store) == before
    assert not store._requeue_state_path("beta", original.id).exists()
