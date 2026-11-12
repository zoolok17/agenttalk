"""#727, round 4: a stored value that is echoed in a message (not only in a command) never starts a line of its own.

Round 3 made every printed COMMAND take a stored value only as a plain token. The messages around those commands
also echo stored values: a draft path, a reason, a request id, a subject. Each such echo goes through
``printed_commands.meta_text`` (an id) or ``printed_commands.line`` (free text and paths), so a stored value with a
line break in it cannot print a line that looks like an instruction (``agenttalk FORGED...``).

The tests run the real commands and notices; the stored value is a real record of the kind the command reads.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
import test_printed_commands as base

from agenttalk import cli, display, doctor, transcript
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


# ======================================================================================================== round 5
#
# Accepted stored metadata that the display derives labels from: a broadcast's `audience` label and `audience_resolved`
# list, a thread's request id, a rescind's reason. `Store.valid_messages` accepts a line break in all of them, so every
# place that DISPLAYS one has to escape it (the stored record and the JSON output keep the original).

PLAIN_BROADCAST_AUDIENCE = "reviewers"


def _fanout_store(
    tmp_path: Path,
    *,
    audience: str,
    resolved: str = "beta,gamma",
    request_id: str = "b-1",
    subject: str = "",
    batch_total: str = "2",
    recipients: tuple[str, ...] = ("beta", "gamma"),
) -> Store:
    s = Store(tmp_path)
    s.init(["alpha", "beta", "gamma"])
    s.set_role("alpha", "lead")
    for r in recipients:
        s.send(
            sender="alpha",
            recipient=r,
            body="q",
            kind="question",
            subject=subject,
            meta={
                "request_id": request_id,
                "broadcast_id": request_id,
                "audience": audience,
                "audience_resolved": resolved,
                "batch_total": batch_total,
            },
        )
    return s


def _warnings(capsys: pytest.CaptureFixture[str], root: Path) -> list[str]:
    capsys.readouterr()
    assert base._run(root, "status", "--json") == 0
    return json.loads(capsys.readouterr().out)["warnings"]


def test_status_prints_a_stored_broadcast_audience_label_on_one_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, hostile: str
) -> None:
    monkeypatch.setattr(cli, "OPEN_OUTBOUND_STALE_SECONDS", -1.0)  # every open thread counts as stale
    s = _fanout_store(tmp_path, audience=hostile)

    warnings = _warnings(capsys, s.root)

    assert any("outbound" in w and "still awaiting" in w for w in warnings), warnings
    assert all(_forged(w) == [] for w in warnings), warnings


def test_status_prints_a_stored_missed_audience_on_one_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], hostile: str
) -> None:
    s = _fanout_store(
        tmp_path, audience=PLAIN_BROADCAST_AUDIENCE, resolved=f"beta,{hostile}", batch_total="3", recipients=("beta",)
    )

    warnings = _warnings(capsys, s.root)

    assert any("incomplete fan-out" in w and "missed:" in w for w in warnings), warnings
    assert all(_forged(w) == [] for w in warnings), warnings


def test_status_prints_stale_operator_escalation_ids_on_one_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, hostile: str
) -> None:
    monkeypatch.setattr(cli, "OPEN_OUTBOUND_STALE_SECONDS", -1.0)
    s = base._store(tmp_path)
    s.set_operator_facing("alpha")
    s.send(
        sender="beta",
        recipient="alpha",
        body="q",
        kind="question",
        meta={"request_id": hostile, "needs_operator": "true"},
    )

    warnings = _warnings(capsys, s.root)

    assert any("operator escalation(s) pending" in w for w in warnings), warnings
    assert all(_forged(w) == [] for w in warnings), warnings


def test_threads_listing_prints_stored_labels_on_one_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], hostile: str
) -> None:
    s = _fanout_store(tmp_path, audience=hostile, request_id=hostile, subject=hostile)
    capsys.readouterr()

    assert base._run(s.root, "threads", "--for", "alpha") == 0

    assert _forged(capsys.readouterr().out) == []


def test_sync_row_and_wait_hint_print_a_stored_broadcast_label_on_one_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], hostile: str
) -> None:
    s = _fanout_store(tmp_path, audience=hostile)
    capsys.readouterr()

    assert base._run(s.root, "sync", "--for", "alpha") == 0

    out = capsys.readouterr().out
    assert "awaiting" in out and _forged(out) == [], out


def test_sync_row_for_an_ordinary_broadcast_is_unchanged(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    s = _fanout_store(tmp_path, audience=PLAIN_BROADCAST_AUDIENCE)
    capsys.readouterr()

    assert base._run(s.root, "sync", "--for", "alpha") == 0

    out = capsys.readouterr().out
    assert "peer=@reviewers" in out and "# awaiting @reviewers" in out


def test_a_rescind_reason_is_printed_on_one_line_by_check_and_wait(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], hostile: str
) -> None:
    s = base._store(tmp_path)
    s.send(sender="alpha", recipient="beta", body="q", kind="question", meta={"request_id": "tk-1"})
    assert base._run(s.root, "rescind", "--from", "alpha", "--to-request", "tk-1", "-m", hostile) == 0
    capsys.readouterr()

    assert base._run(s.root, "check", "--for", "beta", "--to-request", "tk-1") == 3

    assert _forged(capsys.readouterr().out) == []


def test_a_received_message_prints_its_subject_and_meta_on_one_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], hostile: str
) -> None:
    s = base._store(tmp_path)
    s.send(sender="alpha", recipient="beta", body="hello", kind="message", subject=hostile, meta={"note": hostile})
    capsys.readouterr()

    assert base._run(s.root, "recv", "--for", "beta") == 0

    out = capsys.readouterr().out
    assert "hello" in out and _forged(out) == [], out


def test_the_message_header_for_ordinary_values_is_unchanged(tmp_path: Path) -> None:
    s = base._store(tmp_path)
    msg = s.send(
        sender="alpha",
        recipient="beta",
        body="hello",
        kind="message",
        subject="fix the login (urgent)",
        meta={"note": "a b; c"},
    )

    text = display.render(msg)

    assert "  subject fix the login (urgent)\n" in text and "note=a b; c" in text


def test_the_dead_letter_list_prints_a_stored_reason_on_one_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], hostile: str
) -> None:
    import test_dead_letter as tdl

    s = tdl._store(tmp_path)
    original = tdl._send(s, "poison")
    tdl._runloop(s, drive=tdl._always_false(), k_poison=1, max_polls=1)
    sidecar = s.dead_letter_dir / "beta" / f"{original.id}.deadletter.json"
    record = json.loads(sidecar.read_text(encoding="utf-8"))
    record["last_reason"] = hostile
    sidecar.write_text(json.dumps(record), encoding="utf-8")
    capsys.readouterr()

    assert base._run(s.root, "dead-letter", "list") == 0

    out = capsys.readouterr().out
    assert original.id in out and _forged(out) == [], out


# ---------------------------------------------------------- the wrapper prompt's declared draft path (round 5, P2-2)


def _draft_prompt(path: str, shell: str = "powershell") -> str:
    return base._prompt(
        {"correlation_id": "tk-1", "meta": {"request_id": "tk-1"}, "reply_draft": {"path": path}}, shell
    )


def test_the_prompt_draft_path_is_one_line_and_the_exact_path_stays_in_the_record(hostile: str) -> None:
    path = f"C:/projects/{hostile}/drafts/m.md"

    text = _draft_prompt(path)

    assert _forged(text) == [], text
    assert [ln for ln in text.splitlines() if "m.md" in ln and "reply_draft" not in ln and ln.startswith("  C:")]
    record = json.loads(text.split("```json\n", 1)[1].split("\n```", 1)[0])
    assert record["reply_draft"]["path"] == path  # the structured field decodes to the exact path
    assert "exact path" in text  # and the one-line form says where it is


def test_the_prompt_draft_path_for_an_ordinary_path_is_unchanged() -> None:
    path = "C:\\projects\\a folder\\drafts\\m.md"

    text = _draft_prompt(path)

    assert f"\n  {path}\n" in text and "exact path" not in text


def test_the_prompt_structured_record_is_one_json_block_whatever_the_record_holds(hostile: str) -> None:
    text = base._prompt({"correlation_id": "tk-1", "meta": {"request_id": "tk-1", "note": hostile}})

    block = text.split("```json\n", 1)[1].split("\n```", 1)[0]
    assert json.loads(block)["meta"]["note"] == hostile  # still the exact value once decoded
    assert _forged(block) == []


def test_the_real_wrapper_draft_path_in_a_store_under_an_odd_folder_is_one_line(tmp_path: Path, hostile: str) -> None:
    folder = tmp_path / hostile.replace("\r", "").replace("\n", "").replace("\x0b", "").replace("\x1c", "")
    if "\u2028" not in hostile and "\u2029" not in hostile and "\u0085" not in hostile:
        pytest.skip("a file name cannot hold this break here; the path text itself is covered above")
    folder.mkdir()
    s = base._store(folder)
    record = {
        "id": "20990101-000000-000000-AbCd",
        "from": "alpha",
        "to": "beta",
        "kind": "question",
        "body": "q",
        "meta": {"request_id": "tk-1"},
    }

    decorated = wrapper_loop._with_reply_draft(s, "beta", record)
    text = base.assemble_turn_prompt(decorated, reply_shell="powershell", seat="beta")

    assert "reply_draft" in decorated and _forged(text) == [], text


def test_a_refused_live_copy_error_prints_a_stored_request_id_on_one_line(tmp_path: Path, hostile: str) -> None:
    s = base._store(tmp_path)
    meta = {"request_id": hostile, "work_item": "w-1", "stage": "build"}
    s.send(sender="alpha", recipient="beta", body="work", kind="task", meta=dict(meta))

    with pytest.raises(ValueError) as err:
        s.send(sender="alpha", recipient="beta", body="work", kind="task", meta=dict(meta), _refuse_live_request=True)

    assert "already has a live copy" in str(err.value)
    assert _forged(str(err.value)) == []


# ======================================================================================================== round 6
#
# 1. The prompt's JSON writes a character above U+FFFF as the surrogate pair JSON needs (`\\ud83d\\ude00` style), so the
#    value a reader decodes is the exact stored value (a four-digit `\\u` escape for a five-digit code point decodes to
#    a different character).
# 2. The time stamp, sender, recipient, kind and id of a message are echoed by the header of `recv` and by the
#    transcript, and they are free-form in a stored record (the transcript reads the store without roster checks).

ABOVE_BMP = {
    "tag-latin-a": "\U000e0001",
    "tag-space": "\U000e0020",
    "cancel-tag": "\U000e007f",
}


@pytest.fixture(params=list(ABOVE_BMP))
def above_bmp(request: pytest.FixtureRequest) -> str:
    return ABOVE_BMP[request.param]


def _json_of(text: str) -> dict:
    return json.loads(text.split("```json\n", 1)[1].split("\n```", 1)[0])


def test_the_prompt_json_round_trips_a_character_above_u_ffff_exactly(above_bmp: str) -> None:
    path = f"C:/projects/a{above_bmp}b/drafts/m.md"

    text = _draft_prompt(path)

    assert _json_of(text)["reply_draft"]["path"] == path  # the exact target, not a different folder
    # never a five-digit escape, which a JSON reader takes as a different character
    assert "\\u" + format(ord(above_bmp), "x") not in text


def test_the_prompt_json_keeps_an_ordinary_supplementary_character_as_it_is() -> None:
    text = base._prompt({"correlation_id": "tk-1", "meta": {"request_id": "tk-1", "note": "ok \U0001f600"}})

    assert _json_of(text)["meta"]["note"] == "ok \U0001f600" and "\U0001f600" in text


def test_the_prompt_json_round_trips_every_escaped_class_exactly(hostile: str, above_bmp: str) -> None:
    value = f"a{hostile}b{above_bmp}c"

    text = base._prompt({"correlation_id": "tk-1", "meta": {"request_id": "tk-1", "note": value}})

    assert _json_of(text)["meta"]["note"] == value and _forged(text.split("== STRUCTURED RECORD")[1]) == []


def test_the_real_wrapper_draft_target_under_a_folder_with_a_character_above_u_ffff_decodes_exactly(
    tmp_path: Path, above_bmp: str
) -> None:
    folder = tmp_path / f"proj{above_bmp}x"
    folder.mkdir()
    s = base._store(folder)
    record = {
        "id": "20990101-000000-000000-AbCd",
        "from": "alpha",
        "to": "beta",
        "kind": "question",
        "body": "q",
        "meta": {"request_id": "tk-1"},
    }

    decorated = wrapper_loop._with_reply_draft(s, "beta", record)
    text = base.assemble_turn_prompt(decorated, reply_shell="powershell", seat="beta")

    target = _json_of(text)["reply_draft"]["path"]
    assert target == decorated["reply_draft"]["path"] and above_bmp in target
    assert Path(target).parent.is_dir()  # the decoded target names the folder that exists


# ----------------------------------------------------------------- the rest of a message's header (round 6, P2-2)


def _rewrite_stored(s: Store, msg_id: str, **fields: str) -> None:
    path = s.messages_dir / f"{msg_id}.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record.update(fields)
    path.write_text(json.dumps(record), encoding="utf-8")


def test_a_received_message_prints_a_stored_time_stamp_on_one_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], hostile: str
) -> None:
    s = base._store(tmp_path)
    msg = s.send(sender="alpha", recipient="beta", body="hello", kind="message")
    _rewrite_stored(s, msg.id, ts=hostile)
    capsys.readouterr()

    assert base._run(s.root, "recv", "--for", "beta") == 0

    out = capsys.readouterr().out
    assert "hello" in out and _forged(out) == [], out


@pytest.mark.parametrize("field", ["ts", "sender", "recipient", "kind", "id"])
def test_the_message_header_prints_every_stored_field_on_one_line(field: str, hostile: str) -> None:
    msg = dataclasses.replace(_a_message(), **{field: hostile})

    text = display.render(msg)

    assert _forged(text) == [], text
    assert "hello" in text


def _a_message():
    from agenttalk.store import Message

    return Message(
        id="20990101-000000-000000-AbCd",
        ts="2099-01-01T00:00:00Z",
        sender="alpha",
        recipient="beta",
        kind="question",
        subject="s",
        body="hello",
        meta={},
    )


@pytest.mark.parametrize("field", ["ts", "sender", "recipient", "kind"])
def test_the_markdown_transcript_prints_every_header_field_on_one_line(field: str, hostile: str) -> None:
    msg = dataclasses.replace(_a_message(), **{field: hostile})

    assert _forged(transcript.to_markdown([msg])) == []


@pytest.mark.parametrize("field", ["ts", "sender", "recipient", "kind"])
def test_the_real_transcript_export_prints_stored_header_fields_on_one_line(
    tmp_path: Path, field: str, hostile: str
) -> None:
    s = base._store(tmp_path)
    msg = s.send(sender="alpha", recipient="beta", body="hello", kind="question")
    _rewrite_stored(
        s,
        msg.id,
        **({"from": hostile} if field == "sender" else {"to": hostile} if field == "recipient" else {field: hostile}),
    )

    out = transcript.export(s, fmt="md")

    assert "hello" in out.read_text(encoding="utf-8") and _forged(out.read_text(encoding="utf-8")) == []


def test_the_message_header_and_transcript_for_ordinary_values_are_unchanged() -> None:
    msg = _a_message()

    shown = display.render(msg)
    markdown = transcript.to_markdown([msg])

    assert "AGENTTALK :: alpha  ->  beta" in shown and "  id      20990101-000000-000000-AbCd" in shown
    assert "  at      2099-01-01T00:00:00Z" in shown
    assert "## 2099-01-01T00:00:00Z \u2014 alpha \u2192 beta  *(question)*" in markdown


def test_tail_prints_an_invalid_message_warning_on_one_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], hostile: str
) -> None:
    s = base._store(tmp_path)
    msg = s.send(sender="alpha", recipient="beta", body="hello", kind="message")
    _rewrite_stored(s, msg.id, **{"from": hostile})  # a sender that is not in the roster: shown as an INVALID line
    capsys.readouterr()

    assert base._run(s.root, "tail", "--from-start", "--timeout", "1", "--interval", "0.1") == 0

    captured = capsys.readouterr()
    assert _forged(captured.err) == [] and _forged(captured.out) == []
