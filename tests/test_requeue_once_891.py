import json

import pytest

from test_dead_letter import _dead_payload, _requeue, _requeue_store, _send


def test_failed_explicit_replay_keeps_plain_requeue_refused(tmp_path, monkeypatch):
    from agenttalk.store import Store

    store = _requeue_store(tmp_path, monkeypatch, False)
    original = _send(store, "body")
    _dead_payload(store, original)
    assert _requeue(store, original) == 0
    earlier = store.messages_for("beta")[-1]
    with monkeypatch.context() as patch:
        def fail(*args, **kwargs):
            raise ValueError("injected publication failure")

        patch.setattr(Store, "_reserve_message_publication_sequence", fail)
        assert _requeue(store, original, "--again", "--reason", "repeat") == 2
    claim = store._read_requeue_state("beta", original.id)
    assert claim["state"] == "claimed"
    assert claim["requeued_again"] == "repeat" and claim["requeued_again_after"] == earlier.id
    assert _requeue(store, original) == 2
    assert [message.id for message in store.messages_for("beta")] == [earlier.id]


@pytest.mark.parametrize("fields", [
    {"requeued_again": "reason"},
    {"requeued_again_after": "none"},
    {"requeued_again": " ", "requeued_again_after": "none"},
    {"requeued_again": False, "requeued_again_after": "none"},
    {"requeued_again": "reason", "requeued_again_after": []},
    {"requeued_again": "reason", "requeued_again_after": "bad-id"},
])
def test_invalid_replay_claim_is_not_a_first_publication(tmp_path, monkeypatch, capsys, fields):
    store = _requeue_store(tmp_path, monkeypatch, False)
    original = _send(store, "body")
    _dead_payload(store, original)
    record = {
        "schema_version": 1,
        "requeued_as": "20200101-000000-000000-Abcd",
        "session_id": store.load_config()["session_id"],
        "state": "claimed",
        **fields,
    }
    store._requeue_state_path("beta", original.id).write_text(json.dumps(record), encoding="utf-8")
    assert _requeue(store, original) == 2
    assert "damaged" in capsys.readouterr().err
    assert not store.messages_for("beta")


@pytest.mark.parametrize("replay", [False, True])
def test_claim_reader_accepts_old_and_new_records(tmp_path, monkeypatch, replay):
    store = _requeue_store(tmp_path, monkeypatch, False)
    original = _send(store, "body")
    _dead_payload(store, original)
    record = {
        "schema_version": 1,
        "requeued_as": "20200101-000000-000000-Abcd",
        "session_id": store.load_config()["session_id"],
        "state": "claimed",
    }
    if replay:
        record.update(requeued_again="repeat", requeued_again_after="none")
    store._requeue_state_path("beta", original.id).write_text(json.dumps(record), encoding="utf-8")
    assert store._read_requeue_state("beta", original.id) == record
    assert store._requeue_previous("beta", original.id) == record["requeued_as"]
    assert (store._requeue_block("beta", original.id, record["session_id"]) is not None) is replay


@pytest.mark.parametrize("previous", ["none", "unreadable"])
def test_unpublished_explicit_replay_without_readable_predecessor_is_refused(tmp_path, monkeypatch, previous):
    store = _requeue_store(tmp_path, monkeypatch, False)
    original = _send(store, "body")
    _dead_payload(store, original)
    record = {
        "schema_version": 1,
        "requeued_as": "20200101-000000-000000-Abcd",
        "session_id": store.load_config()["session_id"],
        "state": "claimed",
        "requeued_again": "reason",
        "requeued_again_after": previous,
    }
    store._requeue_state_path("beta", original.id).write_text(json.dumps(record), encoding="utf-8")
    assert _requeue(store, original) == 2
    assert not store.messages_for("beta")


@pytest.mark.parametrize("kind", ["message", "review-result", "rescind", "end"])
def test_each_non_opener_is_requeued_once(tmp_path, monkeypatch, kind, capsys):
    store = _requeue_store(tmp_path, monkeypatch, False)
    original = _send(store, "body", kind=kind)
    if kind == "end":
        store.dead_letter("beta", original.to_dict(), reason="poison", failure_class="poison", at="t")
    else:
        _dead_payload(store, original)
    assert _requeue(store, original) == 0
    copy = store.messages_for("beta")[-1]
    assert _requeue(store, original) == 2
    refusal = capsys.readouterr().err
    assert copy.id in refusal
    assert "--again --reason TEXT" in refusal
    assert "new request id" not in refusal
    assert "agenttalk send" not in refusal
    assert len(store.messages_for("beta")) == 1


@pytest.mark.parametrize("reason", [None, "", "   "])
def test_again_requires_nonblank_reason(tmp_path, monkeypatch, reason):
    store = _requeue_store(tmp_path, monkeypatch, False)
    original = _send(store, "body")
    _dead_payload(store, original)
    flags = ["--again"] + (["--reason", reason] if reason is not None else [])
    assert _requeue(store, original, *flags) == 2
    assert not store.messages_for("beta")


@pytest.mark.parametrize("signed", [False, True])
def test_again_publishes_audited_copy(tmp_path, monkeypatch, capsys, signed):
    store = _requeue_store(tmp_path, monkeypatch, signed)
    original = _send(store, "body")
    _dead_payload(store, original)
    assert _requeue(store, original) == 0
    previous = store.messages_for("beta")[-1]
    assert _requeue(store, original, "--again", "--reason", "retry") == 0
    new = store.messages_for("beta")[-1]
    assert new.meta["requeued_again"] == "retry"
    assert new.meta["requeued_again_after"] == previous.id
    record = json.loads(store._requeue_state_path("beta", original.id).read_text(encoding="utf-8"))
    assert set(record) == {"schema_version", "requeued_as", "session_id", "state",
                           "requeued_again", "requeued_again_after"}
    assert record["requeued_again"] == "retry" and record["requeued_again_after"] == previous.id
    if signed:
        from agenttalk import signing

        signing.verify_message(new.to_dict(), signing.load_key(store.project_id()), expected_key_id=store.project_id())
    assert previous.id in capsys.readouterr().out


def test_old_non_opener_copy_is_accepted_once(tmp_path, monkeypatch):
    store = _requeue_store(tmp_path, monkeypatch, False)
    original = _send(store, "legacy")
    _dead_payload(store, original)
    store.send(sender="lead", recipient="beta", body="old copy", meta={"requeued_from": original.id})
    assert _requeue(store, original) == 0
    assert _requeue(store, original) == 2
    assert len(store.messages_for("beta")) == 2


@pytest.mark.parametrize("damaged", [b"{}", b"{", b"\xff"])
def test_damaged_record_needs_explicit_again(tmp_path, monkeypatch, damaged, capsys):
    store = _requeue_store(tmp_path, monkeypatch, False)
    original = _send(store, "body")
    _dead_payload(store, original)
    store._requeue_state_path("beta", original.id).write_bytes(damaged)
    assert _requeue(store, original) == 2
    refusal = capsys.readouterr().err
    assert "--again --reason TEXT" in refusal
    assert "new request id" not in refusal
    assert _requeue(store, original, "--again", "--reason", "repair") == 0
    assert store.messages_for("beta")[-1].meta["requeued_again_after"] == "unreadable"


@pytest.mark.parametrize("kind", ["message", "review-result"])
def test_non_opener_repeat_with_failed_copy_only_suggests_again(tmp_path, monkeypatch, capsys, kind):
    store = _requeue_store(tmp_path, monkeypatch, False)
    original = _send(store, "body", kind=kind)
    _dead_payload(store, original)
    assert _requeue(store, original) == 0
    copied = store.messages_for("beta")[-1]
    _dead_payload(store, copied)
    capsys.readouterr()
    assert _requeue(store, original) == 2
    refusal = capsys.readouterr().err
    assert copied.id in refusal and "--again --reason TEXT" in refusal
    assert "new request id" not in refusal and "Retry that copy instead" not in refusal


def test_again_does_not_override_live_opener(tmp_path, monkeypatch):
    store = _requeue_store(tmp_path, monkeypatch, False)
    original = _send(store, "body", kind="task", meta={"request_id": "tk-live"})
    _dead_payload(store, original)
    assert _requeue(store, original) == 0
    assert _requeue(store, original, "--again", "--reason", "retry") == 2
    assert len(store.messages_for("beta")) == 1


def test_again_for_authority_locked_reply(tmp_path, monkeypatch):
    from agenttalk.wrapper.obligations import _roster_snapshot
    from test_dead_letter import _run

    store = _requeue_store(tmp_path, monkeypatch, False)
    meta = {
        "origin_request_id": "tk-parent",
        "origin_inbound_id": "inbound-parent",
        "expected_roster_revision": _roster_snapshot(store.load_config())["revision"],
    }
    original = store.send(sender="beta", recipient="lead", kind="message", body="body", meta=meta)
    store.dead_letter("lead", original.to_dict(), reason="poison", failure_class="poison", at="t")
    command = ["dead-letter", "requeue", "--agent", "lead", "--id", original.id]
    assert _run(command, store.root) == 0
    previous = store.messages_for("lead")[-1]
    assert _run(command + ["--again", "--reason", "retry"], store.root) == 0
    assert store.messages_for("lead")[-1].meta["requeued_again_after"] == previous.id


def test_again_reason_is_safe_to_print(tmp_path, monkeypatch, capsys):
    store = _requeue_store(tmp_path, monkeypatch, False)
    original = _send(store, "body")
    _dead_payload(store, original)
    assert _requeue(store, original) == 0
    capsys.readouterr()
    reason = 'quoted "reason"\nFORGED\tLINE'
    assert _requeue(store, original, "--again", "--reason", reason) == 0
    output = capsys.readouterr().out
    assert "\nFORGED" not in output and "\tLINE" not in output
    assert r"\u000aFORGED\u0009LINE" in output
    assert '"reason"' in output
    assert store.messages_for("beta")[-1].meta["requeued_again"] == reason


@pytest.mark.parametrize("source", ["notice", "interruption"])
def test_printed_retry_command_runs_once_then_refuses(tmp_path, monkeypatch, source):
    import re
    import shlex
    from agenttalk import cli
    from agenttalk.wrapper.loop import _interruption_remedy
    from test_dead_letter import _run

    store = _requeue_store(tmp_path, monkeypatch, False)
    original = _send(store, "body")
    _dead_payload(store, original)
    if source == "notice":
        info = {
            "agent": "beta",
            "msg_id": original.id,
            "from": "lead",
            "kind": "message",
            "attempts": 1,
            "failure_class": "poison",
        }
        assert cli._dead_letter_notifier(store, "beta")(info, disposed=True)
        text = store.messages_for("lead")[-1].body
    else:
        text = _interruption_remedy("beta", original.id, k=2, kind="timeout", budget_seconds=1)
    assert "once" in text and "--again --reason TEXT" in text
    match = re.search(r"agenttalk dead-letter requeue --agent beta --id [A-Za-z0-9-]+", text)
    assert match
    command = shlex.split(match.group())[1:]
    assert _run(command, store.root) == 0
    assert _run(command, store.root) == 2


@pytest.mark.parametrize("kind", ["task", "review-request"])
def test_live_opener_refusal_names_the_copy(tmp_path, monkeypatch, capsys, kind):
    store = _requeue_store(tmp_path, monkeypatch, False)
    original = _send(store, "body", kind=kind, meta={"request_id": "tk-891"})
    _dead_payload(store, original)
    assert _requeue(store, original) == 0
    copied = store.messages_for("beta")[-1]
    capsys.readouterr()
    assert _requeue(store, original) == 2
    assert copied.id in capsys.readouterr().err


def test_resolved_non_opener_repeat_is_refused(tmp_path, monkeypatch):
    from test_dead_letter import _run

    store = _requeue_store(tmp_path, monkeypatch, False)
    original = _send(store, "resolved")
    _dead_payload(store, original)
    assert (
        _run(
            ["dead-letter", "resolve", "--agent", "beta", "--id", original.id, "--reason", "done", "--from", "lead"],
            store.root,
        )
        == 0
    )
    assert _requeue(store, original, "--force-resolved", "--reason", "retry", "--from", "lead") == 0
    assert _requeue(store, original, "--force-resolved", "--reason", "retry", "--from", "lead") == 2
    first = store.messages_for("beta")[-1]
    assert (
        _run(
            [
                "dead-letter",
                "resolve",
                "--agent",
                "beta",
                "--id",
                original.id,
                "--reason",
                "handled again",
                "--from",
                "lead",
            ],
            store.root,
        )
        == 0
    )
    assert _requeue(store, original, "--force-resolved", "--again", "--reason", "retry", "--from", "lead") == 0
    assert store.messages_for("beta")[-1].meta["requeued_again_after"] == first.id
    assert len(store.messages_for("beta")) == 2


def test_again_refuses_if_earlier_record_changes_during_preparation(tmp_path, monkeypatch):
    from agenttalk.store import Store

    store = _requeue_store(tmp_path, monkeypatch, False)
    original = _send(store, "body")
    _dead_payload(store, original)
    assert _requeue(store, original) == 0
    real = Store._requeue_previous
    calls = []

    def changed(self, agent, message_id):
        calls.append(1)
        return real(self, agent, message_id) if len(calls) == 1 else "changed"

    monkeypatch.setattr(Store, "_requeue_previous", changed)
    assert _requeue(store, original, "--again", "--reason", "retry") == 2
    assert len(store.messages_for("beta")) == 1


@pytest.mark.parametrize("resolved", [False, True])
def test_printed_reset_retry_runs_once_then_refuses(tmp_path, monkeypatch, capsys, resolved):
    import shlex
    from agenttalk import cli
    from test_dead_letter import _run
    from test_requeue_prepared_advice import dead_letter, refuse_lost_preparation

    store = _requeue_store(tmp_path, monkeypatch, False)
    original = dead_letter(store, "review-result")
    flags = []
    if resolved:
        assert (
            _run(
                [
                    "dead-letter",
                    "resolve",
                    "--agent",
                    "beta",
                    "--id",
                    original.id,
                    "--from",
                    "lead",
                    "--reason",
                    "done",
                ],
                store.root,
            )
            == 0
        )
        flags = ["--force-resolved", "--from", "lead", "--reason", "retry"]
    capsys.readouterr()
    refuse_lost_preparation(monkeypatch, lambda: _requeue(store, original, *flags))
    output = capsys.readouterr().err
    expected = ["dead-letter", "requeue", "--agent", "beta", "--id", original.id]
    if resolved:
        expected += ["--force-resolved", "--from", "lead", "--reason", "retry"]
    # The printed retry names this store (#889): agenttalk --root <store> <the command>.
    line = next(line.strip() for line in output.splitlines() if line.strip().startswith("agenttalk --root "))
    argv = shlex.split(line)
    assert argv[:3] == ["agenttalk", "--root", str(store.root)]
    assert argv[3:] == expected
    store.reset()
    assert cli.main(argv[1:]) == 0
    assert cli.main(argv[1:]) == 2


@pytest.mark.parametrize("kind", ["task", "review-request", "review-result", "rescind", "end", "message"])
def test_again_records_the_previous_copy_for_every_kind(tmp_path, monkeypatch, kind):
    store = _requeue_store(tmp_path, monkeypatch, False)
    original = _send(store, "body", kind=kind)
    store.dead_letter("beta", original.to_dict(), reason="poison", failure_class="poison", at="t")
    assert _requeue(store, original) == 0
    earlier = store.messages_for("beta")[-1]
    assert _requeue(store, original, "--again", "--reason", "intentional") == 0
    later = store.messages_for("beta")[-1]
    assert later.meta["requeued_again"] == "intentional"
    assert later.meta["requeued_again_after"] == earlier.id


@pytest.mark.parametrize("kind", ["task", "review-request"])
def test_again_for_opener_after_its_first_copy_is_dead_lettered(tmp_path, monkeypatch, kind):
    store = _requeue_store(tmp_path, monkeypatch, False)
    original = _send(store, "body", kind=kind, meta={"request_id": "tk-891-dead"})
    _dead_payload(store, original)
    assert _requeue(store, original) == 0
    first = store.messages_for("beta")[-1]
    store.dead_letter("beta", first.to_dict(), reason="poison", failure_class="poison", at="t")
    assert _requeue(store, original, "--again", "--reason", "intentional") == 0
    assert store.messages_for("beta")[-1].meta["requeued_again_after"] == first.id


def test_again_does_not_ignore_error_checking_previous_copy(tmp_path, monkeypatch):
    from agenttalk.store import Store

    store = _requeue_store(tmp_path, monkeypatch, False)
    original = _send(store, "body")
    _dead_payload(store, original)
    assert _requeue(store, original) == 0

    def unavailable(self, agent, message_id):
        raise OSError("synthetic I/O failure")

    monkeypatch.setattr(Store, "_find_message", unavailable)
    assert _requeue(store, original, "--again", "--reason", "retry") == 2
    assert len(store.messages_for("beta")) == 1


@pytest.mark.parametrize("failure_call", [1, 2, 3])
def test_again_cannot_waive_requeue_record_read_error(tmp_path, monkeypatch, capsys, failure_call):
    from pathlib import Path

    store = _requeue_store(tmp_path, monkeypatch, False)
    original = _send(store, "body")
    _dead_payload(store, original)
    assert _requeue(store, original) == 0
    record = store._requeue_state_path("beta", original.id)
    previous = record.read_bytes()
    before = {message.id for message in store.valid_messages()}
    read_text = Path.read_text
    reads = 0

    def unreadable(path, *args, **kwargs):
        nonlocal reads
        if path == record:
            reads += 1
            if reads == failure_call:
                raise PermissionError("synthetic read failure")
        return read_text(path, *args, **kwargs)

    capsys.readouterr()
    monkeypatch.setattr(Path, "read_text", unreadable)
    assert _requeue(store, original, "--again", "--reason", "retry") == 2
    output = capsys.readouterr()
    assert "cannot be read" in output.err
    assert "--again" not in output.err
    assert record.read_bytes() == previous
    assert {message.id for message in store.valid_messages()} == before


@pytest.mark.parametrize("kind", ["message", "review-result", "rescind", "end"])
@pytest.mark.parametrize("again", [False, True])
def test_non_opener_read_error_names_record_and_retry_without_fresh_send(tmp_path, monkeypatch, capsys, kind, again):
    import shlex
    from pathlib import Path
    from agenttalk import printed_commands as pc
    from test_dead_letter import _run

    store = _requeue_store(tmp_path, monkeypatch, False)
    original = _send(store, "body", kind=kind)
    store.dead_letter("beta", original.to_dict(), reason="poison", failure_class="poison", at="t")
    assert _requeue(store, original) == 0
    record = store._requeue_state_path("beta", original.id)
    previous = record.read_bytes()
    before = {message.id for message in store.valid_messages()}
    read_text = Path.read_text

    def unreadable(path, *args, **kwargs):
        if path == record:
            raise PermissionError(13, "synthetic read failure")
        return read_text(path, *args, **kwargs)

    capsys.readouterr()
    monkeypatch.setattr(Path, "read_text", unreadable)
    flags = ["--again", "--reason", "retry"] if again else []
    assert _requeue(store, original, *flags) == 2
    error = capsys.readouterr().err
    assert "new request id" not in error and "agenttalk send" not in error
    assert pc.shown(str(record), pc.PATH_SHOWN_MAX_CHARACTERS,
                    source="is the requeue record beside this dead letter in the store") in error
    assert "access" in error and "retry" in error.lower()
    assert f"agenttalk dead-letter requeue --agent beta --id {original.id}" in error
    assert record.read_bytes() == previous
    assert {message.id for message in store.valid_messages()} == before
    printed = error.split("Then retry the same dead letter: ", 1)[1].strip().removesuffix(".")
    monkeypatch.setattr(Path, "read_text", read_text)
    assert _run(shlex.split(printed)[1:], store.root) == 2
    assert "--again --reason TEXT" in capsys.readouterr().err


def test_non_opener_read_error_retry_retains_resolved_options(tmp_path, monkeypatch, capsys):
    from pathlib import Path
    from test_dead_letter import _run

    store = _requeue_store(tmp_path, monkeypatch, False)
    original = _send(store, "body", kind="review-result")
    _dead_payload(store, original)
    assert _requeue(store, original) == 0
    assert _run(["dead-letter", "resolve", "--agent", "beta", "--id", original.id,
                 "--from", "lead", "--reason", "done"], store.root) == 0
    record = store._requeue_state_path("beta", original.id)
    read_text = Path.read_text

    def unreadable(path, *args, **kwargs):
        if path == record:
            raise PermissionError(13, "synthetic read failure")
        return read_text(path, *args, **kwargs)

    capsys.readouterr()
    monkeypatch.setattr(Path, "read_text", unreadable)
    assert _requeue(store, original, "--force-resolved", "--from", "lead", "--reason", "retry") == 2
    error = capsys.readouterr().err
    assert f"agenttalk dead-letter requeue --agent beta --id {original.id}" in error
    assert "--force-resolved --from lead --reason retry" in error
    assert "new request id" not in error and "agenttalk send" not in error
