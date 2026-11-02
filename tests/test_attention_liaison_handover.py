"""CLI reproduction of questions hidden when the liaison changes."""
import json
from dataclasses import replace

import pytest

from agenttalk import cli, overview, threads, web
from agenttalk.store import Store


def _command(root, *args):
    return cli.main(["--root", str(root), *args])


@pytest.mark.parametrize("operation", ["view", "answered-elsewhere", "defer"])
def test_pending_question_survives_liaison_change(tmp_path, capsys, operation):
    assert _command(tmp_path, "init", "--agents", "worker,first,second") == 0
    assert _command(tmp_path, "roster", "set-operator-facing", "first") == 0
    assert _command(tmp_path, "escalate", "--from", "worker", "-m", "Choose an option") == 0
    capsys.readouterr()
    assert _command(tmp_path, "attention", "--json") == 0
    items = json.loads(capsys.readouterr().out)["items"]
    item = next(row for row in items if row["source"] == "needs_operator")
    assert _command(tmp_path, "roster", "set-operator-facing", "second") == 0
    capsys.readouterr()
    if operation == "view":
        assert _command(tmp_path, "attention", "--json") == 0
        rows = json.loads(capsys.readouterr().out)["items"]
        assert item["item_id"] in [row["item_id"] for row in rows]
    else:
        until = ["--until", "2099-01-01T00:00:00Z"] if operation == "defer" else []
        assert _command(tmp_path, "attention", operation, "--from", "second",
                        "--item", item["item_id"], "--reason", "Discussed elsewhere", *until) == 0


def test_saved_question_does_not_record_operator_route(tmp_path, capsys):
    records = []
    for name, liaison, explicit in [("routed", "first", False), ("direct", "second", True)]:
        root = tmp_path / name
        assert _command(root, "init", "--agents", "worker,first,second") == 0
        assert _command(root, "roster", "set-operator-facing", liaison) == 0
        target = ["--to", "first"] if explicit else []
        assert _command(root, "escalate", "--from", "worker", *target,
                        "-m", "Choose an option", "--meta", "request_id=esc-example") == 0
        assert _command(root, "roster", "set-operator-facing", "second") == 0
        record = Store(root).valid_messages()[0].to_dict()
        records.append({key: value for key, value in record.items() if key not in ("id", "ts")})
    capsys.readouterr()
    assert records[0] == records[1]


def _explicit_question(root, capsys):
    assert _command(root, "init", "--agents", "worker,first,second") == 0
    assert _command(root, "roster", "set-operator-facing", "second") == 0
    assert _command(root, "escalate", "--from", "worker", "--to", "first",
                    "-m", "Choose an option", "--meta", "request_id=esc-explicit") == 0
    capsys.readouterr()
    return Store(root)


def _attention(root, capsys):
    assert _command(root, "attention", "--json") == 0
    return json.loads(capsys.readouterr().out)


def test_explicit_escalation_visible_with_original_recipient(tmp_path, capsys):
    _explicit_question(tmp_path, capsys)
    item = _attention(tmp_path, capsys)["items"][0]
    assert item["item_id"] == "needs_operator:esc-explicit"
    assert item["original_recipient"] == "first"
    assert "originally to first" in item["title"]
    assert _command(tmp_path, "attention") == 0
    assert "originally to first" in capsys.readouterr().out


def test_readers_agree_and_disposition_does_not_close_thread(tmp_path, capsys):
    store = _explicit_question(tmp_path, capsys)
    before = {p.name: p.read_bytes() for p in store.messages_dir.glob("*.json")}
    for disposed in (False, True):
        if disposed:
            assert _command(tmp_path, "attention", "answered-elsewhere", "--from", "second",
                            "--item", "needs_operator:esc-explicit", "--reason", "Discussed") == 0
            capsys.readouterr()
        attention = _attention(tmp_path, capsys)
        assert _command(tmp_path, "status", "--json") == 0
        status = json.loads(capsys.readouterr().out)
        dashboard = web.build_attention(web.RootDescriptor(store=store, label="trial"), [])
        gathered = overview.gather(store, label="trial")
        for payload in (attention, status, dashboard, gathered["attention"]):
            assert payload["operator_questions_pending"] == 1
        assert len(attention["items"]) == (0 if disposed else 1)
        assert "1 operator question(s) pending" in overview.render(gathered)
    assert {p.name: p.read_bytes() for p in store.messages_dir.glob("*.json")} == before
    rows = threads.derive_threads(store.valid_messages(), agent="first", cursor="")
    assert rows[0].operator_state == "pending"


@pytest.mark.parametrize("close", ["reply", "rescind", "ack"])
def test_closed_questions_and_ordinary_questions_stay_out(tmp_path, capsys, close):
    store = _explicit_question(tmp_path, capsys)
    assert len(_attention(tmp_path, capsys)["items"]) == 1
    if close == "reply":
        args = ["reply", "--from", "first", "--to-request", "esc-explicit", "-m", "Done"]
    elif close == "rescind":
        args = ["rescind", "--from", "worker", "--to-request", "esc-explicit", "-m", "Cancelled"]
    else:
        args = ["ack", "--for", "first", "--to-request", "esc-explicit"]
    assert _command(tmp_path, *args) == 0
    store.send(sender="worker", recipient="first", kind="question", body="Ordinary",
               meta={"request_id": "ordinary"})
    capsys.readouterr()
    assert _attention(tmp_path, capsys)["items"] == []


def test_inherited_card_does_not_offer_unauthorized_answer(tmp_path, capsys):
    store = _explicit_question(tmp_path, capsys)
    store.send(sender="worker", recipient="first", kind="question", body="Choose",
               subject="A" * 500, meta={"needs_operator": "true", "request_id": "esc-long"})
    payload = web.build_attention(web.RootDescriptor(store=store, label="trial"), [],
                                  actions_enabled=True)
    card = next(row for row in payload["items"] if row["id"] == "needs_operator:esc-explicit")
    assert "originally to first" in card["title"]
    assert not card.get("answerable")
    assert "answer_escalation" not in card.get("actions", {})
    long_card = next(row for row in payload["items"] if row["id"] == "needs_operator:esc-long")
    assert "originally to first" in long_card["title"]


def test_only_current_liaison_disposes_inherited_question(tmp_path, capsys):
    _explicit_question(tmp_path, capsys)
    for actor in ("first", "worker"):
        assert _command(tmp_path, "attention", "answered-elsewhere", "--from", actor,
                        "--item", "needs_operator:esc-explicit", "--reason", "Discussed") == 2
    capsys.readouterr()
    assert _command(tmp_path, "attention", "dismiss", "--from", "second",
                    "--item", "needs_operator:esc-explicit", "--reason", "Discussed") == 2
    assert "not allowed" in capsys.readouterr().err


def test_ordinary_question_is_not_an_operator_question(tmp_path, capsys):
    store = _explicit_question(tmp_path, capsys)
    store.send(sender="worker", recipient="first", kind="question", body="Ordinary",
               meta={"request_id": "ordinary"})
    assert [row["item_id"] for row in _attention(tmp_path, capsys)["items"]] == [
        "needs_operator:esc-explicit"]


def test_explicit_recipient_view_and_answer_remain_scoped(tmp_path, capsys):
    store = _explicit_question(tmp_path, capsys)
    store.send(sender="worker", recipient="second", kind="question", body="Choose",
               meta={"needs_operator": "true", "request_id": "esc-second"})
    assert _command(tmp_path, "attention", "--for", "first", "--json") == 0
    assert [row["item_id"] for row in json.loads(capsys.readouterr().out)["items"]] == [
        "needs_operator:esc-explicit"]
    chat = web._lead_chat_pending_decisions(store, "first", "worker")
    assert [row["request_id"] for row in chat] == ["esc-explicit"]
    payload = web.build_attention(web.RootDescriptor(store=store, label="trial"), [],
                                  actions_enabled=True)
    card = next(row for row in payload["items"] if row["id"] == "needs_operator:esc-second")
    assert card["answerable"] is True


def test_recipient_scan_is_once_per_recipient_and_uses_validated_input(tmp_path, capsys, monkeypatch):
    store = _explicit_question(tmp_path, capsys)
    for index in range(5):
        store.send(sender="worker", recipient="first", kind="question", body="Choose",
                   meta={"needs_operator": "true", "request_id": f"esc-{index}"})
    store.send(sender="worker", recipient="second", kind="question", body="Choose",
               meta={"needs_operator": "true", "request_id": "esc-second"})
    messages = store.valid_messages()
    calls = []
    real_derive = threads.derive_threads

    def derive(*args, **kwargs):
        calls.append(kwargs["agent"])
        assert kwargs["grouped"] is not None
        return real_derive(*args, **kwargs)

    monkeypatch.setattr(threads, "derive_threads", derive)
    rows = threads.pending_operator_questions(store, messages, for_agent="second")
    assert len(rows) == 7
    assert sorted(calls) == ["first", "second"]
    calls.clear()
    assert threads.pending_operator_questions(store, [], for_agent="second") == []
    assert calls == []


def test_status_warns_current_liaison_about_stale_other_recipient_question(tmp_path, capsys):
    store = _explicit_question(tmp_path, capsys)
    messages = [replace(message, ts="2000-01-01T00:00:00Z") for message in store.valid_messages()]
    warnings = cli._thread_warnings(store, store.load_config(), valid_msgs=messages)
    warning = next(text for text in warnings if "operator escalation(s) pending" in text)
    assert warning.startswith("second: 1 operator escalation(s) pending")
    assert "agenttalk attention" in warning
