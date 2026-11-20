"""#874: a message file nested too deeply is invalid, exactly like a malformed one.

One message file holding JSON nested thousands of levels deep used to stop every bus command with a Python
traceback (``RecursionError``), because the readers of message files only expected plainly invalid text. Every
reader must give the same answer for a too-deep file as for plain damaged text, and every command must carry on:
the good message beside it is still delivered. The expected value is never written down twice: each test builds
two identical stores, one with ``MALFORMED`` text and one with the deep file, runs the same call and compares.
"""

import json
import re
import time

import pytest

from agenttalk import checkpoint, cli, web
from agenttalk.store import Store

MALFORMED = "{not json"
DEEP = "[" * 200000 + "]" * 200000
BAD_NAME = "20261120-000000-000000-Bad0.json"
GOOD_BODY = "a good message beside the bad file"

FORCED_TEXT = '{"force_recursion": true}'


def _force(monkeypatch):
    """A decoder made to fail on one text, so a whole command can be run quickly and on every Python version."""
    original = json.loads

    def loads(value, *args, **kwargs):
        if value == FORCED_TEXT:
            raise RecursionError("forced decoder failure")
        return original(value, *args, **kwargs)

    monkeypatch.setattr(json, "loads", loads)
    return FORCED_TEXT


@pytest.fixture(params=["deep", "forced"])
def too_deep(request, monkeypatch):
    """The text of a file the JSON decoder cannot read: really 200,000 levels deep, or a decoder made to fail."""
    if request.param == "deep":
        try:
            json.loads(DEEP)
        except RecursionError:
            return DEEP
        pytest.skip("JSON decoder accepts the 200,000-level input on this runtime")
    return _force(monkeypatch)


@pytest.fixture
def forced_deep(monkeypatch):
    return _force(monkeypatch)


def _world(root, bad_text):
    """A store with one good message from claude to codex and one bad file in the message folder."""
    store = Store(root)
    store.init(["claude", "codex"])
    good = store.send(sender="claude", recipient="codex", kind="note", body=GOOD_BODY)
    (store.messages_dir / BAD_NAME).write_text(bad_text, encoding="utf-8")
    return store, good


def _both(tmp_path, too_deep):
    """The same world twice: (label, store, good message) for the malformed file and for the deep one."""
    return [(label, *_world(tmp_path / label, text)) for label, text in (("malformed", MALFORMED), ("deep", too_deep))]


_ID = re.compile(r"\d{8}-\d{6}-\d{6}-[A-Za-z0-9]{4}")
_SESSION = re.compile(r"\d{8}T\d{6}-[A-Za-z0-9]{4,6}")
_HEX = re.compile(r"[0-9a-f]{64}")  # the project id: new for every store
_TIME = re.compile(r"\d{4}-\d\d-\d\d[T ][\d:.+-]+Z?")
# What the parser says about the file is the one thing that may differ; everything around it must match.
_DETAIL = (
    re.compile(r"(invalid JSON:) [^\n\"]*"),
    re.compile(r"(corrupt payload) \([^\n]*\)"),
    re.compile(r"(cannot be read) \([^\n]*\)"),
)


def _scrub(value, root):
    """Text with this store's folder, message ids, times and the parser's own wording removed."""
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)
    for form in (str(root), str(root).replace("\\", "\\\\"), str(root).replace("\\", "/")):
        text = text.replace(form, "<root>")
    for pattern in _DETAIL:
        text = pattern.sub(lambda m: m.group(1) + " <detail>", text)
    text = text.replace(BAD_NAME[:-5], "<bad>")  # the name the test chose stays readable
    text = _HEX.sub("<hex>", _SESSION.sub("<session>", text))
    return _TIME.sub("<time>", _ID.sub("<id>", text))


def _run(root, argv, capsys):
    capsys.readouterr()
    code = cli.main(["--root", str(root), *argv])
    captured = capsys.readouterr()
    return code, _scrub(captured.out, root), _scrub(captured.err, root)


# --- the shared scan: the one place every delivery, status, doctor and dashboard read comes from -----------------


def test_the_scan_lists_a_nested_file_as_invalid_like_a_malformed_one_and_returns_the_good_message(tmp_path, too_deep):
    seen = {}
    for label, store, good in _both(tmp_path, too_deep):
        valid, invalid = store._scan_messages_with_paths()
        assert [m.id for m, _ in valid] == [good.id]
        seen[label] = [(path.name, ident, reason) for path, ident, reason in invalid]
    assert (
        [(n, i) for n, i, _ in seen["deep"]] == [(n, i) for n, i, _ in seen["malformed"]] == [(BAD_NAME, BAD_NAME[:-5])]
    )
    assert seen["malformed"][0][2].startswith("invalid JSON:")
    assert seen["deep"][0][2] == "invalid JSON: nested too deeply"  # the same prefix; the wording is stable everywhere


def test_the_scan_gives_the_nested_file_the_stable_problem_code_of_a_malformed_one(tmp_path, too_deep):
    codes = {}
    for label, store, _good in _both(tmp_path, too_deep):
        problems = store._validated_message_snapshot(collect_problems=True)[1]
        codes[label] = [(p["id"], p["reason"]) for p in problems]
    assert codes["deep"] == codes["malformed"] == [(BAD_NAME[:-5], "message_json_invalid")]


@pytest.mark.parametrize("compacted", [False, True], ids=["refresh", "archive-folder"])
def test_a_refresh_or_archive_scan_of_a_nested_file_matches_a_malformed_one(tmp_path, too_deep, compacted):
    seen = {}
    for label, store, _good in _both(tmp_path, too_deep):
        bad = store.messages_dir / BAD_NAME
        if compacted:
            store.compacted_dir.mkdir(parents=True, exist_ok=True)
            bad = bad.replace(store.compacted_dir / BAD_NAME)
        valid, invalid = store._scan_messages_with_paths(paths=[bad], compacted=compacted)
        seen[label] = (valid, [(p.name, i, r.split(":")[0]) for p, i, r in invalid])
    assert seen["deep"] == seen["malformed"] == ([], [(BAD_NAME, BAD_NAME[:-5], "invalid JSON")])


def test_delivery_gives_only_the_good_message_for_a_nested_file(tmp_path, too_deep):
    for _label, store, good in _both(tmp_path, too_deep):
        assert [m.id for m in store.messages_for("codex")] == [good.id]
        assert [m.id for m in store.valid_messages()] == [good.id]
        assert [m.id for m in store.all_messages()] == [good.id]
        assert [i for i, _r in store.list_invalid_messages()] == [BAD_NAME[:-5]]


# --- every command from the issue: it runs, and the good message beside the bad file still arrives ---------------

COMMANDS = {
    "status": ["status"],
    "status --json": ["status", "--json"],
    "doctor": ["doctor"],
    "threads": ["threads", "--for", "claude"],
    "drain": ["drain", "--for", "codex", "--limit", "5"],
    "recv": ["recv", "--for", "codex"],
    "send": ["send", "--from", "claude", "--to", "codex", "--kind", "note", "-m", "hi"],
    "prune --invalid --dry-run": ["prune", "--invalid", "--dry-run"],
    "prune --invalid": ["prune", "--invalid"],
}


@pytest.mark.parametrize("name", sorted(COMMANDS))
def test_command_treats_a_nested_message_file_like_a_malformed_one(tmp_path, capsys, too_deep, name):
    results = {}
    for label, store, _good in _both(tmp_path, too_deep):
        results[label] = _run(tmp_path / label, COMMANDS[name], capsys)
        # What is left afterwards, so a command that moved or dropped anything shows up too.
        results[label] += (
            sorted(_scrub(p.name, tmp_path) for p in store.messages_dir.iterdir()),
            sorted(_scrub(p.name, tmp_path) for p in store.quarantine_dir.iterdir())
            if store.quarantine_dir.exists()
            else [],
            len(store.messages_for("codex")),
        )
    assert results["deep"] == results["malformed"]


def test_status_json_lists_the_nested_file_among_the_invalid_messages(tmp_path, capsys, too_deep):
    store, good = _world(tmp_path, too_deep)
    capsys.readouterr()
    assert cli.main(["--root", str(tmp_path), "status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [m["id"] for m in payload["invalid_messages"]] == [BAD_NAME[:-5]]
    assert payload["invalid_messages"][0]["reason"].startswith("invalid JSON:")
    assert payload["message_count"] == 1


@pytest.mark.parametrize("command", ["drain", "recv"])
def test_drain_and_recv_deliver_the_good_message_beside_a_nested_file(tmp_path, capsys, too_deep, command):
    store, good = _world(tmp_path, too_deep)
    argv = {"drain": ["drain", "--for", "codex", "--limit", "5"], "recv": ["recv", "--for", "codex"]}[command]
    capsys.readouterr()
    assert cli.main(["--root", str(tmp_path), *argv]) == 0
    assert good.id in capsys.readouterr().out


def test_send_works_and_the_new_message_arrives_beside_a_nested_file(tmp_path, capsys, too_deep):
    store, good = _world(tmp_path, too_deep)
    capsys.readouterr()
    assert cli.main(["--root", str(tmp_path), *COMMANDS["send"]]) == 0
    assert [m.body for m in store.messages_for("codex")] == [GOOD_BODY, "hi"]


def test_prune_invalid_moves_only_the_nested_file_into_quarantine(tmp_path, capsys, too_deep):
    store, good = _world(tmp_path, too_deep)
    capsys.readouterr()
    assert cli.main(["--root", str(tmp_path), "prune", "--invalid", "--dry-run"]) == 0
    assert (store.messages_dir / BAD_NAME).exists() and not store.quarantine_dir.exists()  # a dry run moves nothing
    assert cli.main(["--root", str(tmp_path), "prune", "--invalid"]) == 0
    assert not (store.messages_dir / BAD_NAME).exists()
    assert [p.name for p in store.quarantine_dir.iterdir()] == [BAD_NAME]  # recoverable, never deleted
    assert [m.id for m in store.messages_for("codex")] == [good.id]


def test_a_nested_field_the_parser_still_accepts_is_ignored_as_before(tmp_path, capsys):
    """A message that is otherwise valid keeps working when a field of it is deep but within the parser's limit."""
    store, good = _world(tmp_path, MALFORMED)
    path = store.messages_dir / f"{good.id}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps(data)[:-1] + ', "zz": ' + "[" * 500 + "]" * 500 + "}", encoding="utf-8")
    for argv in (["status", "--json"], ["threads", "--for", "claude"], ["drain", "--for", "codex", "--limit", "5"]):
        assert cli.main(["--root", str(tmp_path), *argv]) == 0, argv
    capsys.readouterr()


# --- the dashboard, checkpoints and the archive step -----------------------------------------------------------


def test_the_dashboard_message_list_carries_on_over_a_nested_file(tmp_path, too_deep):
    seen = {}
    for label, store, _good in _both(tmp_path, too_deep):
        seen[label] = [m.body for m in web._all_messages(store)]
    assert seen["deep"] == seen["malformed"] == [GOOD_BODY]


def test_the_checkpoint_message_read_skips_a_nested_file_like_a_malformed_one(tmp_path, too_deep):
    seen = {}
    for label, store, _good in _both(tmp_path, too_deep):
        messages, _config, truncated = checkpoint._bounded_valid_messages(store, deadline=time.monotonic() + 30)
        seen[label] = ([m.body for m in messages], truncated)
    assert seen["deep"] == seen["malformed"]
    assert seen["deep"][0] == [GOOD_BODY]


@pytest.mark.parametrize("dry_run", [True, False], ids=["plan", "move"])
def test_the_archive_step_leaves_a_nested_file_where_it_is_like_a_malformed_one(tmp_path, too_deep, dry_run):
    seen = {}
    for label, store, _good in _both(tmp_path, too_deep):
        records = store.archive_messages_below("99999999-999999-999999-zzzz", dry_run=dry_run)
        root = tmp_path / label
        seen[label] = (
            _scrub(records, root),
            sorted(_scrub(p.name, root) for p in store.messages_dir.iterdir()),
            sorted(_scrub(p.name, root) for p in store.compacted_dir.iterdir()) if store.compacted_dir.exists() else [],
        )
    assert seen["deep"] == seen["malformed"]
    assert "<bad>.json" in seen["deep"][1]  # the bad file stays where it is, still reportable


# --- dead letters: the payload, the note beside it, and the requeue record -------------------------------------


def _dead_letter(root, *, kind="note", payload=None, note=None, record=None):
    """A store with one dead letter for codex, and its id. The payload, the note file beside it and the requeue
    record can each be replaced. Only a task or review request has a requeue record: ``kind="task"`` for that."""
    store = Store(root)
    store.init(["claude", "codex"])
    meta = {"request_id": "tk-0123456789ab"} if kind == "task" else None
    sent = store.send(sender="claude", recipient="codex", kind=kind, body="x", meta=meta)
    sink = store.dead_letter_dir / "codex"
    sink.mkdir(parents=True)
    original = store.messages_dir / f"{sent.id}.json"
    stored = sink / f"{sent.id}.json"
    original.replace(stored)  # the real dead letter: the original bytes moved out of the message folder
    if payload is not None:
        stored.write_text(payload, encoding="utf-8")
    (sink / f"{sent.id}.deadletter.json").write_text(
        json.dumps({"message_id": sent.id, "agent": "codex"}) if note is None else note, encoding="utf-8"
    )
    if record is not None:
        path = store._requeue_state_path("codex", sent.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(record, encoding="utf-8")
    return store, sent.id


DEAD_LETTER_PARTS = ["payload", "note", "record"]
DEAD_LETTER_COMMANDS = {
    "list": lambda mid: ["dead-letter", "list"],
    "show": lambda mid: ["dead-letter", "show", "--agent", "codex", "--id", mid],
    "show --json": lambda mid: ["dead-letter", "show", "--agent", "codex", "--id", mid, "--json"],
    "requeue": lambda mid: ["dead-letter", "requeue", "--agent", "codex", "--id", mid],
}


@pytest.mark.parametrize("command", sorted(DEAD_LETTER_COMMANDS))
@pytest.mark.parametrize("part", DEAD_LETTER_PARTS)
def test_dead_letter_command_treats_a_nested_file_like_a_malformed_one(tmp_path, capsys, too_deep, part, command):
    results = {}
    for label, text in (("malformed", MALFORMED), ("deep", too_deep)):
        root = tmp_path / label
        store, mid = _dead_letter(root, kind="task", **{part: text})
        results[label] = _run(root, DEAD_LETTER_COMMANDS[command](mid), capsys) + (len(store.messages_for("codex")),)
    assert results["deep"] == results["malformed"]


def test_dead_letter_show_gives_an_unreadable_payload_for_a_nested_one(tmp_path, capsys, too_deep):
    _store, mid = _dead_letter(tmp_path, payload=too_deep)
    capsys.readouterr()
    assert cli.main(["--root", str(tmp_path), *DEAD_LETTER_COMMANDS["show --json"](mid)]) == 0
    assert json.loads(capsys.readouterr().out)["body"] == "<unreadable payload>"


def test_dead_letter_requeue_refuses_a_nested_payload_and_publishes_nothing(tmp_path, capsys, too_deep):
    store, mid = _dead_letter(tmp_path, payload=too_deep)
    capsys.readouterr()
    assert cli.main(["--root", str(tmp_path), *DEAD_LETTER_COMMANDS["requeue"](mid)]) == 2
    assert "corrupt payload" in capsys.readouterr().err
    assert store.messages_for("codex") == []


def test_dead_letter_list_still_shows_a_letter_whose_note_is_nested_and_flags_the_note(tmp_path, too_deep):
    seen = {}
    for label, text in (("malformed", MALFORMED), ("deep", too_deep)):
        store, mid = _dead_letter(tmp_path / label, note=text)
        seen[label] = _scrub(store.list_dead_letters("codex"), tmp_path)
        assert [m["message_id"] for m in store.list_dead_letters("codex")] == [mid]
        assert store.list_dead_letters("codex")[0]["sidecar_unreadable"] is True
    assert seen["deep"] == seen["malformed"]


def test_dead_letter_requeue_refuses_a_nested_requeue_record_and_publishes_nothing(tmp_path, capsys, too_deep):
    store, mid = _dead_letter(tmp_path, kind="task", record=too_deep)
    capsys.readouterr()
    assert cli.main(["--root", str(tmp_path), *DEAD_LETTER_COMMANDS["requeue"](mid)]) == 2
    assert "cannot be read" in capsys.readouterr().err
    assert store.messages_for("codex") == []
