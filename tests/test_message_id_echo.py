"""Message-ID displays keep lookup values intact and identify the source of cut text."""

import json

import pytest

from agenttalk import cli, printed_commands as pc
from agenttalk.store import RequeueRefused, Store
from test_printed_commands import _run, _store


@pytest.mark.parametrize("separator,escaped", [("\n", r"\u000a"), ("\u2028", r"\u2028")], ids=["LF", "U2028"])
def test_purge_refusal_shows_the_listed_id_on_one_line_and_json_keeps_it(
    tmp_path, monkeypatch, capsys, separator, escaped
):
    store = _store(tmp_path)
    message_id = "listed" + separator + "FORGED"
    entry = {"agent": "beta", "message_id": message_id, "sha256": "a" * 64}
    monkeypatch.setattr(Store, "list_dead_letters", lambda *args: [entry])
    monkeypatch.setattr(cli, "_dead_letter_resolution_state", lambda *args: {("beta", message_id): "resolved"})
    monkeypatch.setattr(cli, "_recorded_dead_letter_resolution", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "_close_dead_letter_notice_threads", lambda *args, **kwargs: 0)
    monkeypatch.setattr(cli, "_pending_dead_letter_notices", lambda *args, **kwargs: [])
    before = list(store.valid_messages())
    capsys.readouterr()

    assert _run(store.root, "dead-letter", "purge", "--resolved", "--from", "alpha") == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "agenttalk dead-letter purge: refusing to archive resolved item(s) with pending wrapper notice thread(s): "
        f'beta/"listed{escaped}FORGED" (no recorded resolution)\n'
    )
    assert separator not in captured.err.removesuffix("\n") and len(captured.err.splitlines()) == 1
    assert list(store.valid_messages()) == before and entry["message_id"] == message_id
    assert _run(store.root, "dead-letter", "purge", "--resolved", "--dry-run", "--json", "--from", "alpha") == 0
    assert json.loads(capsys.readouterr().out)["items"][0]["message_id"] == message_id


@pytest.mark.parametrize("separator,escaped", [("\n", r"\u000a"), ("\u2028", r"\u2028")], ids=["LF", "U2028"])
def test_unretryable_copy_advice_shows_the_stored_id_without_publishing(
    tmp_path, monkeypatch, capsys, separator, escaped
):
    store = _store(tmp_path)
    original = store.send(sender="alpha", recipient="beta", body="saved evidence", kind="message")
    payload = store.dead_letter(
        "beta", original.to_dict(), reason="test", failure_class="poison", at="2099-01-01T00:00:00Z"
    )
    retry = "copy" + separator + "FORGED"
    before_payload = payload.read_bytes()
    before = list(store.valid_messages())

    def refuse_send(self, **kwargs):
        raise RequeueRefused("previous copy needs attention; nothing was published.", retry=retry)

    monkeypatch.setattr(Store, "send", refuse_send)
    monkeypatch.setattr(cli, "_retry_problem", lambda *args: "saved payload" + separator + "FORGED_REASON")
    monkeypatch.setattr(cli, "_requeue_route", lambda *args: "Send fresh work by hand.")
    capsys.readouterr()

    assert _run(store.root, "dead-letter", "requeue", "--agent", "beta", "--id", original.id) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "agenttalk dead-letter requeue: previous copy needs attention; nothing was published. "
        f'The copy "copy{escaped}FORGED" cannot be retried as it is (saved payload{escaped}FORGED_REASON); '
        "it needs attention. Send fresh work by hand.\n"
    )
    assert separator not in captured.err.removesuffix("\n") and len(captured.err.splitlines()) == 1
    assert payload.read_bytes() == before_payload and list(store.valid_messages()) == before


@pytest.mark.parametrize("length", [60, 61, 96])
@pytest.mark.parametrize("plain", [False, True], ids=["nonplain", "plain"])
@pytest.mark.parametrize("command", ["reply-refusal-show", "dead-show", "dead-resolve", "dead-requeue"])
def test_typed_id_cut_note_points_to_supplied_text_and_does_not_change_the_lookup(
    tmp_path, monkeypatch, capsys, length, plain, command
):
    store = _store(tmp_path)
    value = ("typed-" if plain else "typed ") + "x" * (length - 6)
    looked_up = []
    if command == "reply-refusal-show":
        from agenttalk import reply_refusals

        def missing(store, agent, message_id):
            looked_up.append(message_id)
            return None

        monkeypatch.setattr(reply_refusals, "read_reply_refusal", missing)
        argv = ["reply-refusal", "show", "--agent", "beta", "--id", value]
    else:

        def absent(store, agent, message_id):
            looked_up.append(message_id)
            return False

        if command == "dead-show":
            monkeypatch.setattr(
                Store, "read_dead_letter_payload", lambda self, agent, message_id: looked_up.append(message_id)
            )
        else:
            monkeypatch.setattr(cli, "_is_listed_dead_letter", absent)
        argv = ["dead-letter", command.removeprefix("dead-"), "--agent", "beta", "--id", value]
        if command == "dead-resolve":
            argv += ["--from", "alpha", "--reason", "handled"]
    before = list(store.valid_messages())
    capsys.readouterr()

    assert _run(store.root, *argv) == 2

    captured = capsys.readouterr()
    shown = value if plain else '"' + value[:60] + '"'
    if not plain and length > 60:
        shown += f" [cut here: {length} characters in all; the whole value is the text you supplied for this command]"
    assert captured.out == "" and f"beta/{shown}.\n" in captured.err
    assert len(captured.err.splitlines()) == 1 and "stored record" not in captured.err
    assert looked_up and all(item == value for item in looked_up)
    assert list(store.valid_messages()) == before


def test_meta_text_keeps_stored_cut_notes_and_accepts_a_supplied_source():
    value = "value " + "x" * 55
    assert "the whole value is in the stored record this message is built from" in pc.meta_text(value)
    assert "the whole value is the text you supplied for this command" in pc.meta_text(value, source=pc.SUPPLIED_SOURCE)
    assert pc.meta_text("x" * 96, source=pc.SUPPLIED_SOURCE) == "x" * 96


def test_purge_missing_resolution_cut_notes_point_to_the_stored_record(tmp_path, monkeypatch, capsys):
    store = _store(tmp_path)
    message_id = "listed " + "x" * 55
    entry = {"agent": "beta", "message_id": message_id, "sha256": "a" * 64}
    monkeypatch.setattr(Store, "list_dead_letters", lambda *args: [entry])
    monkeypatch.setattr(cli, "_dead_letter_resolution_state", lambda *args: {("beta", message_id): "resolved"})
    monkeypatch.setattr(cli, "_recorded_dead_letter_resolution", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "_pending_dead_letter_notices", lambda *args, **kwargs: [])
    capsys.readouterr()

    assert _run(store.root, "dead-letter", "purge", "--resolved", "--from", "alpha") == 2

    captured = capsys.readouterr()
    assert captured.out == "" and len(captured.err.splitlines()) == 2
    assert captured.err.count("the whole value is in the stored record this message is built from") == 2
    assert "supplied" not in captured.err and entry["message_id"] == message_id


@pytest.mark.parametrize("exists", [False, True], ids=["missing", "recorded"])
def test_reply_refusal_resolve_cut_note_points_to_the_supplied_id(tmp_path, capsys, exists):
    from agenttalk import reply_refusals

    store = _store(tmp_path)
    message_id = "typed " + "x" * 90
    if exists:
        record_path = reply_refusals.record_reply_refusal(
            store,
            agent="beta",
            original_message_id=message_id,
            original_from="alpha",
            intended_kind="message",
            reason="saved refusal",
            draft_path="saved.md",
            correlation={},
            at="2099-01-01T00:00:00Z",
        )
        before = record_path.read_bytes()
    capsys.readouterr()

    assert _run(
        store.root,
        "reply-refusal",
        "resolve",
        "--agent",
        "beta",
        "--id",
        message_id,
        "--reason",
        "handled",
        "--from",
        "alpha",
    ) == (0 if exists else 2)

    captured = capsys.readouterr()
    output = captured.out if exists else captured.err
    assert (captured.err if exists else captured.out) == ""
    assert 'beta/"' + message_id[:60] + '"' in output
    assert "[cut here: 96 characters in all; the whole value is the text you supplied for this command]" in output
    assert "stored record" not in output and len(output.splitlines()) == 1
    assert list(store.valid_messages()) == []
    if exists:
        assert record_path.read_bytes() == before
        resolved = json.loads(record_path.with_suffix(".resolved.json").read_text(encoding="utf-8"))
        assert resolved["original_message_id"] == message_id and resolved["reason"] == "handled"
    else:
        assert not (store.dir / "reply-refusals").exists()


@pytest.mark.parametrize("separator", ["\n", "\r\n", "\u2028"], ids=["LF", "CRLF", "U2028"])
def test_dead_letter_resolve_refuses_a_broken_agent_name_without_changes(tmp_path, capsys, separator):
    store = _store(tmp_path)
    original = store.send(sender="alpha", recipient="beta", body="saved evidence", kind="message")
    store.dead_letter("beta", original.to_dict(), reason="test", failure_class="poison", at="2099-01-01T00:00:00Z")
    agent = "beta" + separator + "FORGED"

    def files():
        return {str(p.relative_to(store.dir)): p.read_bytes() for p in store.dir.rglob("*") if p.is_file()}

    before = files()
    capsys.readouterr()

    assert (
        _run(
            store.root,
            "dead-letter",
            "resolve",
            "--agent",
            agent,
            "--id",
            original.id,
            "--reason",
            "handled",
            "--from",
            "alpha",
        )
        == 2
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "not a safe identifier" in captured.err
    assert separator not in captured.err.removesuffix("\n") and len(captured.err.splitlines()) == 1
    assert files() == before
