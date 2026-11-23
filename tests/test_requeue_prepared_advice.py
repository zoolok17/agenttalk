"""A vanished preparation needs kind-appropriate, executable recovery advice."""

import shlex

import pytest

from agenttalk import cli
from agenttalk import store as storage
from test_dead_letter import _dead_payload, _rec, _requeue, _requeue_store, _send


def dead_letter(store, kind):
    correlation = {"in_reply_to": "20200101-000000-000000-Abcd"}
    if kind in {"task", "review-request", "review-result", "proposal-response"}:
        correlation["request_id"] = "tk-original"
    if kind == "review-result":
        correlation["status"] = "approved"
    if kind == "proposal-response":
        correlation["status"] = "accepted"
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


@pytest.mark.parametrize("kind", ["message", "review-result", "task", "review-request", "proposal-response"])
@pytest.mark.parametrize("resolved", [False, True])
def test_prepared_replay_retry_line_preserves_again_reason(tmp_path, monkeypatch, capsys, kind, resolved):
    store = _requeue_store(tmp_path, monkeypatch, False)
    original = dead_letter(store, kind)
    assert _requeue(store, original) == 0
    earlier = store.messages_for("beta")[-1]
    if kind in {"task", "review-request"}:
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
    flags = ["--force-resolved", "--from", "lead"] if resolved else []
    capsys.readouterr()
    refuse_lost_preparation(monkeypatch, lambda: _requeue(store, original, *flags, "--again", "--reason", "try again"))
    error = capsys.readouterr().err
    line = next(line.strip() for line in error.splitlines() if line.startswith("  agenttalk "))
    argv = shlex.split(line)
    assert "--again" in argv and argv[argv.index("--reason") + 1] == "try again"
    if resolved:
        assert "--force-resolved" in argv and argv[argv.index("--from") + 1] == "lead"
    store.reset()
    assert cli.main(argv[1:]) == 0
    assert store.messages_for("beta")[-1].meta["requeued_again_after"] == earlier.id


def test_prepared_replay_unsafe_reason_uses_placeholder(tmp_path, monkeypatch, capsys):
    store = _requeue_store(tmp_path, monkeypatch, False)
    original = dead_letter(store, "message")
    assert _requeue(store, original) == 0
    capsys.readouterr()
    refuse_lost_preparation(monkeypatch, lambda: _requeue(store, original, "--again", "--reason", "unsafe; command"))
    error = capsys.readouterr().err
    line = next(line.strip() for line in error.splitlines() if line.startswith("  agenttalk "))
    assert "--again" in line and "<reason>" in line
    assert "unsafe; command" not in error and "Not a ready command:" in error


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
    assert f"--agent beta --id {original.id}" in output.err
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
    assert f"--agent beta --id {original.id} --force-resolved --from lead --reason retry" in error
    assert "new request id" not in error
    store.reset()
    assert cli.main(["--root", str(store.root), *shlex.split(command)[1:]]) == 0
    messages = store.valid_messages()
    assert len(messages) == 1
    assert messages[0].meta == {**original.meta, "requeued_from": original.id}
    assert cli._dead_letter_resolution_state(store)[("beta", original.id)] == "requeued"


@pytest.mark.parametrize("kind,resolved", [("task", False), ("review-request", False), ("review-result", True)])
def test_actual_retry_line_targets_selected_store(tmp_path, monkeypatch, capsys, kind, resolved):
    selected = tmp_path / "selected folder"
    selected.mkdir()
    store = _requeue_store(selected, monkeypatch, False)
    original = dead_letter(store, kind)
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
    flags = ["--force-resolved", "--from", "lead", "--reason", "try again"] if resolved else []
    other = tmp_path / "different store"
    other.mkdir()
    wrong = storage.Store(other)
    wrong.init(["lead", "beta"])
    monkeypatch.setenv("AGENTTALK_ROOT", str(other))
    capsys.readouterr()
    refuse_lost_preparation(monkeypatch, lambda: _requeue(store, original, *flags))
    error = capsys.readouterr().err
    line = next(line.strip() for line in error.splitlines() if line.startswith("  agenttalk "))
    argv = shlex.split(line)
    assert argv[0] == "agenttalk"
    assert "--root" in argv and argv[argv.index("--root") + 1] == str(store.root)
    store.reset()
    assert cli.main(argv[1:]) == 0
    assert len(store.valid_messages()) == 1
    assert not wrong.valid_messages()
    if resolved:
        assert cli._dead_letter_resolution_state(store)[("beta", original.id)] == "requeued"


@pytest.mark.parametrize("kind,resolved", [("task", False), ("review-request", False), ("review-result", True)])
def test_actual_retry_line_has_exact_arguments(tmp_path, monkeypatch, capsys, kind, resolved):
    store = _requeue_store(tmp_path, monkeypatch, False)
    original = dead_letter(store, kind)
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
    flags = ["--force-resolved", "--from", "lead", "--reason", "try again"] if resolved else []
    monkeypatch.setenv("AGENTTALK_ROOT", str(store.root))
    args = ["dead-letter", "requeue", "--agent", "beta", "--id", original.id, *flags]
    capsys.readouterr()
    refuse_lost_preparation(monkeypatch, lambda: cli.main(args))
    error = capsys.readouterr().err
    line = next(line.strip() for line in error.splitlines() if line.startswith("  agenttalk "))
    argv = shlex.split(line)
    assert argv[0] == "agenttalk"
    assert argv[argv.index("--id") + 1] == original.id
    if resolved:
        assert argv[argv.index("--reason") + 1] == "try again"
    store.reset()
    assert cli.main(argv[1:]) == 0
    assert len(store.valid_messages()) == 1


def test_retry_advice_unsafe_reason_uses_placeholder(tmp_path, monkeypatch, capsys):
    store = _requeue_store(tmp_path, monkeypatch, False)
    original = dead_letter(store, "review-result")
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
    refuse_lost_preparation(
        monkeypatch,
        lambda: _requeue(store, original, "--force-resolved", "--from", "lead", "--reason", "unsafe; command"),
    )
    error = capsys.readouterr().err
    line = next(line.strip() for line in error.splitlines() if line.startswith("  agenttalk "))
    assert "<reason>" in line and "unsafe; command" not in error
    assert "Not a ready command:" in error


def test_retry_advice_dash_reason_replays(tmp_path, monkeypatch, capsys):
    store = _requeue_store(tmp_path, monkeypatch, False)
    original = dead_letter(store, "review-result")
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
    refuse_lost_preparation(
        monkeypatch, lambda: _requeue(store, original, "--force-resolved", "--from", "lead", "--reason=-later")
    )
    error = capsys.readouterr().err
    line = next(line.strip() for line in error.splitlines() if line.startswith("  agenttalk "))
    argv = shlex.split(line)
    assert "--reason=-later" in argv
    store.reset()
    assert cli.main(argv[1:]) == 0


def test_retry_advice_uses_current_folder_when_root_is_implicit(tmp_path, monkeypatch, capsys):
    store = _requeue_store(tmp_path, monkeypatch, False)
    original = dead_letter(store, "task")
    monkeypatch.delenv("AGENTTALK_ROOT", raising=False)
    monkeypatch.chdir(store.root)
    capsys.readouterr()
    refuse_lost_preparation(
        monkeypatch, lambda: cli.main(["dead-letter", "requeue", "--agent", "beta", "--id", original.id])
    )
    error = capsys.readouterr().err
    line = next(line.strip() for line in error.splitlines() if line.startswith("  agenttalk "))
    argv = shlex.split(line)
    assert argv[argv.index("--root") + 1] == str(store.root)
    store.reset()
    assert cli.main(argv[1:]) == 0


def test_retry_advice_unsafe_root_uses_placeholder(tmp_path, monkeypatch, capsys):
    parent = tmp_path / "unsafe & folder"
    parent.mkdir()
    store = _requeue_store(parent, monkeypatch, False)
    original = dead_letter(store, "task")
    capsys.readouterr()
    refuse_lost_preparation(monkeypatch, lambda: _requeue(store, original))
    error = capsys.readouterr().err
    line = next(line.strip() for line in error.splitlines() if line.startswith("  agenttalk "))
    assert "--root <root>" in line and "&" not in line
    assert "Not a ready command:" in error


def test_empty_explicit_root_names_selected_store(tmp_path, monkeypatch, capsys):
    store = _requeue_store(tmp_path, monkeypatch, False)
    original = dead_letter(store, "task")
    monkeypatch.setenv("AGENTTALK_ROOT", str(store.root))
    capsys.readouterr()
    refuse_lost_preparation(
        monkeypatch, lambda: cli.main(["--root", "", "dead-letter", "requeue", "--agent", "beta", "--id", original.id])
    )
    line = next(line.strip() for line in capsys.readouterr().err.splitlines() if line.startswith("  agenttalk "))
    argv = shlex.split(line)
    assert argv[argv.index("--root") + 1] == str(store.root)
    store.reset()
    assert cli.main(argv[1:]) == 0
