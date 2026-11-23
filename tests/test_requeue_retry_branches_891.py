"""Each printed retry selects its original store and keeps the requested options."""

import shlex
from pathlib import Path

import pytest

from agenttalk import cli
from agenttalk.store import Store
from test_dead_letter import _dead_payload, _requeue, _requeue_store, _send
from test_requeue_prepared_advice import refuse_lost_preparation


@pytest.mark.parametrize("branch", ["prepared", "changed", "read_error", "failed_copy"])
@pytest.mark.parametrize("resolved", [False, True])
def test_every_printed_retry_runs_from_another_store(tmp_path, monkeypatch, capsys, branch, resolved):
    selected = tmp_path / "selected store"
    selected.mkdir()
    store = _requeue_store(selected, monkeypatch, False)
    kind = "task" if branch == "failed_copy" else "message"
    original = _send(store, "body", kind=kind, meta={"request_id": "tk-original"})
    _dead_payload(store, original)
    assert _requeue(store, original) == 0
    earlier = store.messages_for("beta")[-1]
    if branch == "failed_copy":
        _dead_payload(store, earlier)
    if resolved:
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
    capsys.readouterr()
    flags = [] if branch == "failed_copy" else ["--again", "--reason", "retry reason"]
    if resolved:
        flags = ["--force-resolved", "--from", "lead", *flags]
        if branch == "failed_copy":
            flags += ["--reason", "retry reason"]
    with monkeypatch.context() as patch:
        if branch == "prepared":
            refuse_lost_preparation(patch, lambda: _requeue(store, original, *flags))
        elif branch == "changed":
            real = Store._requeue_previous
            calls = 0

            def changed(self, agent, message_id):
                nonlocal calls
                calls += 1
                return real(self, agent, message_id) if calls == 1 else "changed"

            patch.setattr(Store, "_requeue_previous", changed)
            assert _requeue(store, original, *flags) == 2
        elif branch == "read_error":
            record = store._requeue_state_path("beta", original.id)
            read_text = Path.read_text

            def unreadable(path, *args, **kwargs):
                if path == record:
                    raise PermissionError("synthetic read failure")
                return read_text(path, *args, **kwargs)

            patch.setattr(Path, "read_text", unreadable)
            assert _requeue(store, original, *flags) == 2
        else:
            assert _requeue(store, original, *flags) == 2
    error = capsys.readouterr().err
    line = next(line.strip() for line in error.splitlines() if line.strip().startswith("agenttalk --root "))
    argv = shlex.split(line)
    assert argv[argv.index("--root") + 1] == str(store.root)
    if branch != "failed_copy":
        assert "--again" in argv and argv[argv.index("--reason") + 1] == "retry reason"
    if resolved and branch != "failed_copy":
        assert "--force-resolved" in argv and argv[argv.index("--from") + 1] == "lead"
    if branch == "failed_copy":
        assert argv[argv.index("--id") + 1] == earlier.id
        assert all(flag not in argv for flag in ("--again", "--force-resolved", "--from", "--reason"))
    if branch == "prepared":
        assert "run this command once" in error
        store.reset()
    other = tmp_path / "other store"
    other.mkdir()
    wrong = Store(other)
    wrong.init(["lead", "beta"])
    monkeypatch.chdir(other)
    monkeypatch.setenv("AGENTTALK_ROOT", str(other))
    assert cli.main(argv[1:]) == 0
    assert not wrong.valid_messages()
    copy = store.messages_for("beta")[-1]
    assert copy.meta["requeued_from"] == (earlier.id if branch == "failed_copy" else original.id)
    if branch != "failed_copy":
        assert copy.meta["requeued_again"] == "retry reason"


def test_copy_resolved_after_advice_is_not_reopened(tmp_path, monkeypatch, capsys):
    store = _requeue_store(tmp_path, monkeypatch, False)
    original = _send(store, "body", kind="task", meta={"request_id": "tk-original"})
    _dead_payload(store, original)
    assert _requeue(store, original) == 0
    copy = store.messages_for("beta")[-1]
    _dead_payload(store, copy)
    resolve = ["--root", str(store.root), "dead-letter", "resolve", "--from", "lead", "--agent", "beta"]
    assert cli.main([*resolve, "--id", original.id, "--reason", "original handled"]) == 0
    capsys.readouterr()
    assert _requeue(store, original, "--force-resolved", "--from", "lead", "--reason", "original reason") == 2
    line = next(
        line.strip() for line in capsys.readouterr().err.splitlines() if line.strip().startswith("agenttalk --root ")
    )
    argv = shlex.split(line)
    assert argv[argv.index("--id") + 1] == copy.id
    assert all(flag not in argv for flag in ("--again", "--force-resolved", "--from", "--reason"))
    assert cli.main([*resolve, "--id", copy.id, "--reason", "copy handled"]) == 0
    before = cli._dead_letter_resolution_state(store)
    messages_before = list(store.valid_messages())
    capsys.readouterr()
    assert cli.main(argv[1:]) == 2
    assert "RESOLVED" in capsys.readouterr().err
    assert cli._dead_letter_resolution_state(store) == before
    assert list(store.valid_messages()) == messages_before


def test_copy_retry_builder_drops_explicit_again_even_when_requested(tmp_path):
    from types import SimpleNamespace

    store = Store(tmp_path)
    args = SimpleNamespace(agent="beta", id="original", again=True)
    note, command = cli._requeue_retry_command(
        store, args, "original reason", {"actor": "lead", "reason": "original reason"}, message_id="copy"
    )
    assert not note
    argv = shlex.split(command)
    assert argv[argv.index("--id") + 1] == "copy"
    assert all(flag not in argv for flag in ("--again", "--force-resolved", "--from", "--reason"))
