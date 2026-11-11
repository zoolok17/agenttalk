"""#727, round 4: a stored value that is echoed in a message (not only in a command) never starts a line of its own.

Round 3 made every printed COMMAND take a stored value only as a plain token. The messages around those commands
also echo stored values: a draft path, a reason, a request id, a subject. Each such echo goes through
``printed_commands.meta_text`` (an id) or ``printed_commands.line`` (free text and paths), so a stored value with a
line break in it cannot print a line that looks like an instruction (``agenttalk FORGED...``).

The tests run the real commands and notices; the stored value is a real record of the kind the command reads.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import test_printed_commands as base

from agenttalk import cli, doctor
from agenttalk import printed_commands as pc
from agenttalk.store import Store
from agenttalk.wrapper import loop as wrapper_loop

FORGED = "FORGED_LINE_SENTINEL"
# Every character `str.splitlines` (and a terminal) takes as the end of a line, and a few more that hide text.
BREAKS = {
    "lf": "\n",
    "crlf": "\r\n",
    "cr": "\r",
    "line-separator": "\u2028",
    "paragraph-separator": "\u2029",
    "nel": "\u0085",
    "vertical-tab": "\x0b",
    "file-separator": "\x1c",
    "bidi-override": "\u202e",
}


def _forged(text: str) -> list[str]:
    """Lines of ``text`` that start with the forged sentinel, however the text was split into lines."""
    return [ln for ln in text.splitlines() if ln.strip().startswith((f"agenttalk {FORGED}", FORGED))]


def _hostile(value_name: str) -> str:
    return f"x{BREAKS[value_name]}agenttalk {FORGED}"


@pytest.fixture(params=list(BREAKS))
def hostile(request: pytest.FixtureRequest) -> str:
    return _hostile(request.param)


# --------------------------------------------------------------------------------------------- the helper


def test_line_keeps_ordinary_text_and_paths_exactly() -> None:
    for text in [
        "C:\\Users\\me\\a folder\\x.md",
        "/home/me/a folder/x.md",
        "caf\u00e9 \u4e2d\u6587",
        'it\'s "fine"; ok',
    ]:
        assert pc.line(text) == text


def test_line_writes_every_break_and_invisible_character_as_a_code(hostile: str) -> None:
    out = pc.line(hostile)

    assert len(out.splitlines()) == 1 and FORGED in out
    assert not any(c in out for c in "\n\r\u2028\u2029\u0085\x0b\x1c\u202e")


def test_line_does_not_turn_a_non_text_value_into_text() -> None:
    assert pc.line(None) == "<a NoneType, not text>" and pc.line(5) == "<a int, not text>"


# ---------------------------------------------------------------- reply-refusal show / list (P2-1, the draft path)


def _refusal(tmp_path: Path, *, draft_path: str, reason: str = "r", original_from: str = "alpha") -> tuple[Store, str]:
    from agenttalk import reply_refusals

    s = base._store(tmp_path)
    mid = "20990101-000000-000000-RFSD"
    reply_refusals.record_reply_refusal(
        s,
        agent="beta",
        original_message_id=mid,
        original_from=original_from,
        intended_kind="message",
        reason=reason,
        draft_path=draft_path,
        correlation={"request_id": "q-1"},
        at="2099-01-01T00:00:00Z",
    )
    return s, mid


def test_reply_refusal_show_prints_a_stored_draft_path_on_one_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], hostile: str
) -> None:
    s, mid = _refusal(tmp_path, draft_path=hostile)

    assert base._run(s.root, "reply-refusal", "show", "--agent", "beta", "--id", mid) == 0

    out = capsys.readouterr().out
    assert _forged(out) == [], out
    assert len([ln for ln in out.splitlines() if ln.startswith("draft path: ")]) == 1


def test_reply_refusal_show_prints_a_stored_reason_on_one_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], hostile: str
) -> None:
    s, mid = _refusal(tmp_path, draft_path="C:/ok/x.refused.md", reason=hostile)

    assert base._run(s.root, "reply-refusal", "show", "--agent", "beta", "--id", mid) == 0

    out = capsys.readouterr().out
    assert _forged(out) == [], out
    assert len([ln for ln in out.splitlines() if ln.startswith("reason: ")]) == 1


def test_reply_refusal_show_with_an_ordinary_path_and_reason_prints_them_as_they_were(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = str(tmp_path / "a folder" / "x.refused.md")
    s, mid = _refusal(tmp_path / "root", draft_path=path, reason="the reply was too long (3 MiB)")

    assert base._run(s.root, "reply-refusal", "show", "--agent", "beta", "--id", mid) == 0

    out = capsys.readouterr().out
    assert f"draft path: {path}\n" in out and "reason: the reply was too long (3 MiB)\n" in out


def test_reply_refusal_list_prints_stored_fields_on_one_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], hostile: str
) -> None:
    s, _mid = _refusal(tmp_path, draft_path="C:/ok/x.refused.md", reason=hostile, original_from=hostile)

    assert base._run(s.root, "reply-refusal", "list") == 0

    out = capsys.readouterr().out
    assert _forged(out) == [], out


# ------------------------------------------------------------------------------ sync rows (the hints' own rows)


def _sync_out(tmp_path: Path, capsys: pytest.CaptureFixture[str], *, request_id: str, subject: str) -> str:
    s = base._store(tmp_path)
    s.send(
        sender="alpha", recipient="beta", body="q", kind="question", subject=subject, meta={"request_id": request_id}
    )
    capsys.readouterr()
    assert base._run(s.root, "sync", "--for", "beta") == 0
    return capsys.readouterr().out


def test_sync_prints_a_stored_request_id_and_subject_on_one_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], hostile: str
) -> None:
    if "\r" in hostile:
        pytest.skip("a bare carriage return is not accepted in a request id by the store")
    out = _sync_out(tmp_path, capsys, request_id=hostile, subject=hostile)

    assert _forged(out) == [], out


def test_sync_row_for_an_ordinary_thread_is_unchanged(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = _sync_out(tmp_path, capsys, request_id="tk-1", subject="fix the login (urgent)")

    assert "[owed-inbound] tk-1  question  peer=alpha  owe=you" in out and '"fix the login (urgent)"' in out
    assert "agenttalk reply --to-request tk-1   # you owe a response" in out


# --------------------------------------------------------------------- status thread warnings (ids with commands)


def test_status_thread_warnings_print_a_stored_request_id_on_one_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], hostile: str
) -> None:
    s = base._store(tmp_path)
    opener = s.send(sender="alpha", recipient="beta", body="q", kind="question", meta={"request_id": hostile})
    s.send(
        sender="beta",
        recipient="alpha",
        body="a",
        kind="message",
        meta={"request_id": hostile, "in_reply_to": opener.id},
    )
    capsys.readouterr()

    assert base._run(s.root, "status", "--json") == 0

    warnings = json.loads(capsys.readouterr().out)["warnings"]
    assert any("unconsumed response" in w for w in warnings), warnings
    assert all(_forged(w) == [] for w in warnings), warnings


# ------------------------------------------------------------------------ dead-letter notice, reply-refusal notice


def _notice_body(s: Store, subject: str) -> str:
    notices = [m for m in s.messages_for("alpha") if m.subject == subject]
    assert len(notices) == 1, [m.subject for m in s.messages_for("alpha")]
    return notices[0].body


def test_the_dead_letter_notice_prints_a_stored_summary_and_id_on_one_line(tmp_path: Path, hostile: str) -> None:
    s = base._store(tmp_path)
    info = {
        "msg_id": hostile,
        "agent": "beta",
        "from": "alpha",
        "kind": "task",
        "attempts": 3,
        "failure_class": "poison",
        "summary": hostile,
    }

    assert cli._dead_letter_notifier(s, "beta")(info, disposed=True) is True

    assert _forged(_notice_body(s, "dead-letter notice")) == []


def test_the_dead_letter_notice_for_an_ordinary_message_is_unchanged(tmp_path: Path) -> None:
    s = base._store(tmp_path)
    info = {
        "msg_id": "20990101-000000-000000-AbCd",
        "agent": "beta",
        "from": "alpha",
        "kind": "task",
        "attempts": 3,
        "failure_class": "poison",
        "summary": "the child exited (code 1)",
    }

    cli._dead_letter_notifier(s, "beta")(info, disposed=True)

    body = _notice_body(s, "dead-letter notice")
    assert "message 20990101-000000-000000-AbCd from alpha" in body and "Reason: the child exited (code 1)  " in body


def test_the_reply_refusal_notice_prints_a_stored_reason_and_path_on_one_line(tmp_path: Path, hostile: str) -> None:
    s = base._store(tmp_path)
    reason_path = tmp_path / "reason.txt"
    reason_path.write_text(hostile, encoding="utf-8")
    draft = tmp_path / f"d{hostile}.md"  # the draft path itself carries the break too (a name may hold one on POSIX)

    wrapper_loop._notify_reply_refusal(
        s,
        agent="beta",
        record={"id": "20990101-000000-000000-AbCd", "from": "alpha"},
        draft=draft,
        reason_path=reason_path,
    )

    assert _forged(_notice_body(s, "reply-refusal notice")) == []


def test_the_interruption_remedy_prints_a_preserved_draft_path_on_one_line(hostile: str) -> None:
    text = wrapper_loop._interruption_remedy(
        "beta", "20990101-000000-000000-AbCd", k=3, kind="timeout", budget_seconds=60.0, preserved_draft=Path(hostile)
    )

    assert _forged(text) == []


# --------------------------------------------------------------------------------------- doctor, prompt header


def test_the_doctor_host_details_print_a_stored_path_on_one_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hostile: str
) -> None:
    import test_powershell_doctor as host_cases

    store = host_cases._store(tmp_path)
    record = host_cases._record(store)
    record["path"] = hostile
    record["_warning"] = "the selected host is older than the one installed"
    monkeypatch.setattr(doctor.supervisor_lifecycle, "read_selected_host", lambda s: record)
    monkeypatch.setattr(doctor, "_inspect_selected_task", lambda *a: {"status": "ok"})
    monkeypatch.setattr(doctor.sys, "platform", "win32")

    check = doctor._check_powershell_host(store)

    assert _forged(check.details) == [] and _forged(check.fix) == []


def test_the_prompt_header_prints_a_stored_subject_on_one_line(hostile: str) -> None:
    text = base._prompt({"subject": hostile, "correlation_id": "tk-1", "meta": {"request_id": "tk-1"}})

    assert _forged(text.split("== STRUCTURED RECORD")[0]) == []
    assert len([ln for ln in text.splitlines() if ln.startswith("subject: ")]) == 1
