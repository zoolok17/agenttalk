"""Body-source selection through the public CLI, using isolated stores."""

import io
import pytest

from agenttalk import cli
from agenttalk.store import Store


@pytest.fixture
def body_store(store: Store) -> Store:
    store.set_role("alpha", "lead")
    store.set_operator_facing("alpha")
    store.send(sender="beta", recipient="alpha", kind="question", body="decision?",
               meta={"request_id": "incoming", "needs_operator": True})
    store.send(sender="alpha", recipient="beta", kind="question", body="review?",
               meta={"request_id": "outgoing"})
    return store


BODY_COMMANDS = [
    ["send", "--to", "beta"],
    ["progress"],
    ["rescind", "--to-request", "outgoing"],
    ["escalate", "--to", "beta"],
    ["relay", "operator-answer", "--to-request", "incoming"],
    ["relay", "operator-command", "--to", "beta"],
    ["relay", "result", "--to", "beta", "--result-id", "result"],
    ["relay", "result", "--to", "beta", "--result-id", "result", "--none-needed"],
    ["relay", "result", "--to", "beta", "--request-id", "gone", "--unresolved"],
    ["propose", "--to", "beta"],
    ["task", "--to", "beta"],
    ["broadcast", "--all"],
    ["reply", "--to-request", "incoming"],
    ["reply", "--to-request", "incoming", "--na"],
    ["release", "--to", "beta", "--relay-human"],
    ["request-launch", "--profile", "review", "--skill", "review-code", "--revision", "HEAD"],
]


@pytest.mark.parametrize(
    "options, error",
    [
        (["-m", "", "--file", ""], "use either -m or --file, not both"),
        (["-m", ""], "it takes no body/subject/kind/meta overrides"),
        (["--file", ""], "it takes no body/subject/kind/meta overrides"),
    ],
)
def test_broadcast_resume_empty_body_overrides_send_nothing(options, error, store, monkeypatch, capsys):
    store.send(
        sender="alpha",
        recipient="beta",
        body="original",
        meta={
            "broadcast_id": "b-partial",
            "audience_resolved": "alpha,beta",
            "audience": "all",
        },
    )
    before = list(store.valid_messages())
    monkeypatch.setattr("sys.stdin", None)

    rc = cli.main(["--root", str(store.root), "broadcast", "--from", "alpha", "--resume", "b-partial", *options])

    captured = capsys.readouterr()
    assert rc == 2, (rc, captured.out, captured.err)
    assert error in captured.err
    assert captured.out == ""
    assert list(store.valid_messages()) == before


@pytest.mark.parametrize("command", BODY_COMMANDS, ids=lambda argv: " ".join(argv))
@pytest.mark.parametrize("message", ["note", ""])
@pytest.mark.parametrize("file_source", ["file", "missing", "-"])
def test_message_and_file_refused_without_sending(
    command, message, file_source, body_store, tmp_path, monkeypatch, capsys,
):
    body_file = tmp_path / "brief.txt"
    body_file.write_text("the complete brief", encoding="utf-8")
    source = {"file": str(body_file), "missing": str(tmp_path / "absent.txt"), "-": "-"}[file_source]
    # The revision lookup is unrelated to body selection; no Git process is needed.
    if command[0] == "request-launch":
        monkeypatch.setattr(cli, "_resolve_revision", lambda *_args: ("a" * 40, "commit"))
    monkeypatch.setattr("sys.stdin", None)  # even --file - must be rejected before reading
    argv = [*command, "--from", "alpha", "-m", message, "--file", source]
    if command[0] == "progress":
        argv.extend(["--to-id", body_store.messages_for("alpha")[0].id])
    before = list(body_store.valid_messages())

    rc = cli.main(["--root", str(body_store.root), *argv])

    assert rc == 2
    assert "use either -m or --file, not both" in capsys.readouterr().err
    assert list(body_store.valid_messages()) == before


@pytest.mark.parametrize("source", ["message", "empty", "file", "stdin-file", "stdin"])
def test_single_body_source_preserved(source, store, tmp_path, monkeypatch):
    body_file = tmp_path / "brief.txt"
    body_file.write_text("complete brief", encoding="utf-8")
    argv = {
        "message": ["-m", "complete brief"],
        "empty": ["-m", "", "--allow-empty"],
        "file": ["--file", str(body_file)],
        "stdin-file": ["--file", "-"],
        "stdin": [],
    }[source]
    monkeypatch.setattr("sys.stdin", io.StringIO("complete brief") if "stdin" in source else None)

    rc = cli.main(["--root", str(store.root), "send", "--from", "alpha", "--to", "beta", *argv])

    assert rc == 0
    assert store.messages_for("beta")[-1].body == ("" if source == "empty" else "complete brief")


def test_release_reason_alias_and_file_refused(body_store, tmp_path, capsys):
    body_file = tmp_path / "brief.txt"
    body_file.write_text("full reason", encoding="utf-8")
    before = list(body_store.valid_messages())

    rc = cli.main(["--root", str(body_store.root), "release", "--from", "alpha", "--to", "beta",
                   "--relay-human", "--reason", "note", "--file", str(body_file)])

    assert rc == 2
    assert "use either -m or --file, not both" in capsys.readouterr().err
    assert list(body_store.valid_messages()) == before


@pytest.mark.parametrize("command", BODY_COMMANDS, ids=lambda argv: " ".join(argv))
@pytest.mark.parametrize("message", ["note", ""])
def test_empty_file_path_conflicts_with_message(command, message, body_store, monkeypatch, capsys):
    if command[0] == "request-launch":
        monkeypatch.setattr(cli, "_resolve_revision", lambda *_args: ("a" * 40, "commit"))
    monkeypatch.setattr("sys.stdin", None)
    argv = [*command, "--from", "alpha", "-m", message, "--file", ""]
    if command[0] == "progress":
        argv.extend(["--to-id", body_store.messages_for("alpha")[0].id])
    before = list(body_store.valid_messages())

    rc = cli.main(["--root", str(body_store.root), *argv])

    assert rc == 2
    assert "use either -m or --file, not both" in capsys.readouterr().err
    assert list(body_store.valid_messages()) == before


@pytest.mark.parametrize("command, kind, body", [
    (["rescind", "--to-request", "outgoing"], "rescind", ""),
    (["reply", "--to-request", "incoming", "--na"], "message", "n/a"),
])
@pytest.mark.parametrize("options", [[], ["--file", ""], ["-m", ""]], ids=["bare", "empty-file", "empty-message"])
def test_optional_body_defaults_preserved_without_stdin(command, kind, body, options, body_store, monkeypatch):
    monkeypatch.setattr("sys.stdin", None)
    before = {message.id for message in body_store.valid_messages()}

    rc = cli.main(["--root", str(body_store.root), *command, "--from", "alpha", *options])

    assert rc == 0
    sent = [message for message in body_store.valid_messages() if message.id not in before]
    assert len(sent) == 1
    assert (sent[0].sender, sent[0].recipient, sent[0].kind, sent[0].body) == ("alpha", "beta", kind, body)


@pytest.mark.parametrize("source", ["--reason", "-m", "--file"])
def test_release_single_source_preserves_reason(source, body_store, tmp_path, monkeypatch):
    reason = "operator decided to stop\n"
    body_file = tmp_path / "reason.txt"
    body_file.write_text(reason, encoding="utf-8")
    value = str(body_file) if source == "--file" else reason
    monkeypatch.setattr("sys.stdin", None)
    before = {message.id for message in body_store.valid_messages()}

    rc = cli.main(["--root", str(body_store.root), "release", "--from", "alpha", "--to", "beta",
                   "--relay-human", source, value])

    assert rc == 0
    sent = [message for message in body_store.valid_messages() if message.id not in before]
    assert len(sent) == 1
    message = sent[0]
    assert (message.sender, message.recipient, message.kind, message.body) == ("alpha", "beta", "release", reason)
    assert message.meta["authority_reason"] == reason
    assert message.meta["release_authority"] == "human"


@pytest.mark.parametrize("source", ["--reason", "-m"])
def test_release_empty_inline_reason_sends_nothing(source, body_store, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", None)  # an explicit empty value must not sniff stdin
    before = list(body_store.valid_messages())

    rc = cli.main(["--root", str(body_store.root), "release", "--from", "alpha", "--to", "beta",
                   "--relay-human", source, ""])

    assert rc == 2
    assert "--reason (-m) is required" in capsys.readouterr().err
    assert list(body_store.valid_messages()) == before
