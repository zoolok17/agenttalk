"""#727, round 1: an id that is ALREADY stored keeps working, and a NEW id is judged exactly as typed.

The new-id check lives only where a request is created from supplied text. These tests drive the real
recovery paths (dead-letter requeue and its preflight, a frozen broadcast resume) with a legacy id the new rule
would refuse, so applying the check at any of those boundaries fails here. They also pin what the two changed
advice sites promise ("inspect first; the requeue command refuses, and says why") against the real command in each
state a retry can be in.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import test_cli as tc
import test_dead_letter as tdl

from agenttalk import cli
from agenttalk.wrapper import recv_api
from agenttalk import printed_commands as pc
from agenttalk.store import Store
from agenttalk.wrapper import loop as wrapper_loop

OLD = "old space"  # accepted by every version before this change; the new rule refuses it


def _dead_lettered(tmp_path: Path, kind: str = "message") -> tuple[Store, object]:
    s = tdl._store(tmp_path)
    original = tdl._send(s, "poison", kind=kind, meta={"request_id": OLD})
    tdl._runloop(s, drive=tdl._always_false(), k_poison=3)
    assert s.dead_lettered_count("beta") == 1
    return s, original


def test_requeue_of_a_dead_letter_with_an_old_request_id_still_works(tmp_path: Path) -> None:
    s, original = _dead_lettered(tmp_path)

    assert cli.main(["--root", str(tmp_path), "dead-letter", "requeue", "--agent", "beta", "--id", original.id]) == 0

    nxt = recv_api.next_record(s, "beta")
    assert nxt is not None and nxt["id"] != original.id and nxt["request_id"] == OLD  # the thread is kept


def test_the_retry_preflight_accepts_an_old_request_id(tmp_path: Path) -> None:
    s, original = _dead_lettered(tmp_path)

    assert cli._retry_problem(s, "beta", original.id) is None  # "would get as far as publishing"


def test_a_frozen_broadcast_resume_of_an_old_id_still_works(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tc._role_root(tmp_path)
    # made the way an older version made it: the new-id check is off only while the batch is created
    monkeypatch.setattr(cli.pc, "new_id_refusal", lambda *a, **k: None)
    monkeypatch.setattr(Store, "send", tc._fail_at(Store, 2))  # one copy delivered, one failed
    capsys.readouterr()
    code = tc._run(
        [
            "broadcast",
            "--from",
            "lead",
            "--to-role",
            "reviewer",
            "--kind",
            "question",
            "--meta",
            f"request_id={OLD}",
            "-m",
            "x",
            "--quiet",
        ],
        root,
    )
    assert code == 5
    monkeypatch.undo()  # back to the real rules: the resume must carry the stored id

    assert tc._run(["broadcast", "--from", "lead", "--resume", OLD, "--quiet"], root) == 0

    capsys.readouterr()
    assert tc._run(["threads", "--for", "rev-b", "--json"], root) == 0
    rows = json.loads(capsys.readouterr().out)["threads"]
    assert next(r for r in rows if r["request_id"] == OLD)["state"] == "owed-inbound"


def test_a_fresh_broadcast_with_that_id_is_refused_where_it_is_created(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tc._role_root(tmp_path)
    capsys.readouterr()

    code = tc._run(
        [
            "broadcast",
            "--from",
            "lead",
            "--to-role",
            "reviewer",
            "--kind",
            "question",
            "--meta",
            f"request_id={OLD}",
            "-m",
            "x",
            "--quiet",
        ],
        root,
    )

    assert code == 2 and "refusing - the new request id" in capsys.readouterr().err
    assert not tc._msgs_on_disk(root)


# --------------------------------------- a new id is judged exactly as typed (never trimmed, then accepted)


@pytest.mark.parametrize("typed", [" id", "id ", "id\n", "\tid", "id\r\n", " id", " "])
@pytest.mark.parametrize("operation", ["send question", "propose", "reply opens proposal", "broadcast request_id"])
def test_a_new_id_with_whitespace_around_it_is_refused_not_stored_trimmed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], typed: str, operation: str
) -> None:
    s = tdl._store(tmp_path)
    s.set_role("lead", "lead")
    s.send(sender="lead", recipient="beta", body="anchor", kind="question", meta={"request_id": "q-anchor"})
    before = len(list(s.messages_dir.glob("*.json")))
    argvs = {
        "send question": ["send", "--from", "lead", "--to", "beta", "--kind", "question", "-m", "x"],
        "propose": ["propose", "--from", "lead", "--to", "beta", "-m", "x"],
        "reply opens proposal": [
            "reply",
            "--from",
            "beta",
            "--to-request",
            "q-anchor",
            "--kind",
            "proposal",
            "-m",
            "x",
        ],
        "broadcast request_id": ["broadcast", "--from", "lead", "--all", "--kind", "question", "-m", "x"],
    }
    capsys.readouterr()

    code = cli.main(["--root", str(tmp_path), *argvs[operation], "--meta", f"request_id={typed}"])

    err = capsys.readouterr().err
    assert code == 2 and "refusing - the new request id" in err and "\n" not in err.strip()
    assert len(list(s.messages_dir.glob("*.json"))) == before  # nothing stored, trimmed or otherwise


@pytest.mark.parametrize("key", ["request_id ", " request_id", "request_id\t"])
def test_a_padded_key_does_not_hide_a_bad_new_id(tmp_path: Path, capsys: pytest.CaptureFixture[str], key: str) -> None:
    s = tdl._store(tmp_path)
    capsys.readouterr()

    code = cli.main(
        [
            "--root",
            str(tmp_path),
            "send",
            "--from",
            "lead",
            "--to",
            "beta",
            "--kind",
            "question",
            "-m",
            "x",
            "--meta",
            f"{key}= padded value",
        ]
    )

    assert code == 2 and not list(s.messages_dir.glob("*.json"))


def test_the_same_id_typed_cleanly_is_accepted(tmp_path: Path) -> None:
    tdl._store(tmp_path)

    assert (
        cli.main(
            [
                "--root",
                str(tmp_path),
                "send",
                "--from",
                "lead",
                "--to",
                "beta",
                "--kind",
                "question",
                "-m",
                "x",
                "--meta",
                "request_id=id",
            ]
        )
        == 0
    )


# --------------------------- the two changed advice sites, against the real command in each state of a retry

_STATES = ["corrupt", "unreadable", "signature-invalid", "already-published"]


def _damaged_dead_letter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str):
    s = tdl._requeue_store(tmp_path, monkeypatch, state == "signature-invalid")
    original = tdl._send(s, "work", kind="task", meta={"request_id": "tk-c"})
    path = tdl._dead_payload(s, original)
    if state == "corrupt":
        path.write_text("{", encoding="utf-8")
    elif state == "unreadable":
        real = Path.read_bytes

        def read(self, *args, **kwargs):
            if self == path:
                raise PermissionError(13, "held by another program", str(self))
            return real(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_bytes", read)
    elif state == "signature-invalid":
        payload = json.loads(path.read_bytes())
        payload["body"] = "edited after signing"
        path.write_text(json.dumps(payload), encoding="utf-8")
    else:
        assert tdl._requeue(s, original) == 0  # the first requeue published it: a second must not repeat it
    return s, original


@pytest.mark.parametrize("state", _STATES)
def test_the_advice_promises_a_refusal_and_the_real_command_gives_one_in_every_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], state: str
) -> None:
    s, original = _damaged_dead_letter(tmp_path, monkeypatch, state)
    live_before = sorted(p.name for p in s.messages_dir.glob("*.json"))
    capsys.readouterr()

    code = tdl._requeue(s, original)  # the very command the advice names

    err = capsys.readouterr().err
    assert code == 2
    assert sorted(p.name for p in s.messages_dir.glob("*.json")) == live_before  # nothing was sent again
    assert err.strip(), "a refusal must say why"
    # the advice at both changed sites says exactly that, and no longer calls a retry as-is
    remedy = wrapper_loop._interruption_remedy("beta", original.id, k=3, kind="timeout", budget_seconds=60.0)
    for text in (remedy,):
        assert "inspect it first and decide" in text and "refuses, and says why" in text
        assert "requeue as-is" not in text
        assert pc.cmd("agenttalk dead-letter requeue --agent {a} --id {i}", a="beta", i=original.id) in text


@pytest.mark.parametrize("state", ["corrupt", "signature-invalid"])
def test_the_dead_letter_notice_names_inspect_before_requeue_and_never_promises_a_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    s, original = _damaged_dead_letter(tmp_path, monkeypatch, state)
    notifier = cli._dead_letter_notifier(s, "beta")
    info = {
        "msg_id": original.id,
        "agent": "beta",
        "from": "lead",
        "kind": "task",
        "attempts": 3,
        "failure_class": "poison",
        "reason": "poison",
    }

    notifier(info, disposed=True)

    notices = [m for m in s.messages_for("lead") if m.subject == "dead-letter notice"]
    assert len(notices) == 1
    body = notices[0].body
    assert body.index("Inspect:") < body.index("agenttalk dead-letter requeue")
    assert "the requeue command refuses, and says why" in body and "Requeue:" not in body
