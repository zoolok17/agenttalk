"""#727: a printed command is ready to run only when every value in it is a plain token; a new request id is
checked where it is created, by what the operation does.

Three groups: the helper itself (what is plain, how an odd value is shown, the shape of a new id), the printing
sites (one golden per ordinary output and one hostile value per site: the hostile value is never in a ready
command and never raw on a line of its own), and the creation table (which operations open a new request and
must refuse a bad supplied id, which carry an id that is already stored and must keep working).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agenttalk import cli, doctor, ovh_gateway_service, powershell_host
from agenttalk import printed_commands as pc
from agenttalk import supervisor as sup
from agenttalk.store import Store
from agenttalk.wrapper import loop as wrapper_loop
from agenttalk.wrapper import usage_park
from agenttalk.wrapper.prompt import assemble_turn_prompt

SENTINEL = "SECOND_COMMAND_SENTINEL"
HOSTILE_IDS = [
    "fix login",  # a space: two arguments
    f"a; echo {SENTINEL}",  # a separator
    f"a&echo.{SENTINEL}",  # the launcher's separator
    "a%PATH%b",
    "$(whoami)",
    "`whoami`",
    "-legacy",  # an old id that would be read as an option
    "it's",
    'say "hi"',
    "curly\u2019quote",
    "line1\nline2",
    "line1\r\nline2",
    "tail\n",
    "nel\u0085x",
    "sep\u2028x",
    "para\u2029x",
    "bidi\u202eevil",
    "\u0661\u0662\u0663",  # Unicode digits
    "caf\u00e9",
    "",
]


# ------------------------------------------------------------------------------------------------ the helper


@pytest.mark.parametrize("value", ["tk-0be0b135c334", "alpha", "a", "A1", "a.b_c-d", "rq-1", "x" * 200])
def test_a_plain_token_is_plain(value: str) -> None:
    assert pc.is_plain(value)


@pytest.mark.parametrize("value", [*HOSTILE_IDS, ".hidden", "_x", None, 5, ["a"], b"abc"])
def test_anything_else_is_not_plain(value: object) -> None:
    assert not pc.is_plain(value)


def test_the_whole_string_must_match_not_a_prefix_or_all_but_a_final_newline() -> None:
    assert not pc.is_plain("abc\n")
    assert not pc.is_plain("abc def")
    assert pc.is_plain("abc")


@pytest.mark.parametrize("value", HOSTILE_IDS)
def test_a_shown_value_is_one_ascii_line_with_no_shell_metacharacter(value: str) -> None:
    text = pc.shown(value)

    assert text.isascii() and text.isprintable()  # no control character, so no new line and no hidden text
    assert not set(text) & set(";&|$`'<>()%^!*?~#{}[]\\") - {"\\"}  # only the \uXXXX escapes use a backslash
    assert '"' == text[0] and text.count('"') == 2  # one quoted string; the value's own quotes are escaped


def test_a_shown_value_can_be_read_back_exactly_so_two_values_never_look_alike() -> None:
    shown = {pc.shown(v) for v in ("a\nb", "a\\nb", "a\u2028b", "a b")}

    assert len(shown) == 4


def test_a_long_value_is_cut_visibly_with_its_full_length() -> None:
    text = pc.shown("x" * 500)

    assert "[cut here: 500 characters in all" in text
    assert len(text) < 200


def test_a_value_that_is_not_text_is_not_turned_into_executable_text() -> None:
    assert pc.shown(["a;b"]) == "<a list, not text>"
    assert pc.shown(None) == "<a NoneType, not text>"


def test_an_ordinary_command_prints_exactly_as_before() -> None:
    assert pc.cmd("agenttalk drain --for {n}", n="alpha") == "agenttalk drain --for alpha"
    assert (
        pc.cmd("agenttalk reply --to-request {r}", r="tk-0be0b135c334")
        == "agenttalk reply --to-request tk-0be0b135c334"
    )


@pytest.mark.parametrize("value", [v for v in HOSTILE_IDS if v])
def test_a_command_with_an_odd_value_is_never_ready(value: str) -> None:
    text = pc.cmd("agenttalk reply --to-request {r}", r=value, lookup="list the threads with: agenttalk threads --json")

    assert text.startswith("agenttalk reply --to-request <r>  (not a ready command:")
    assert pc.shown(value) in text  # the value is in it only as the escaped display
    assert value not in text or pc.shown(value) == '"' + value + '"'  # (a value of safe characters shows as it is)
    assert "list the threads with: agenttalk threads --json" in text
    assert "\n" not in text and "\r" not in text and "\u2028" not in text


def test_one_odd_value_takes_the_whole_command_with_it() -> None:
    text = pc.cmd("agenttalk send --from {a} --to {b}", a="alpha", b="b b")

    assert "--from alpha --to <b>" in text and "not a ready command" in text


def test_the_two_commands_of_a_pair_share_one_note() -> None:
    commands, note = pc.cmds(["agenttalk a {x}", "agenttalk b {x}"], x="a b")

    assert commands == ["agenttalk a <x>", "agenttalk b <x>"] and note and note.count("<x> is") == 1
    assert pc.cmds(["agenttalk a {x}"], x="ok") == (["agenttalk a ok"], None)


def test_a_header_value_is_plain_text_when_plain_and_escaped_otherwise() -> None:
    assert pc.meta_text("tk-1") == "tk-1" and pc.meta_text(None) == "None"
    assert "\n" not in pc.meta_text("a\nb: forged line") and "forged" in pc.meta_text("a\nb: forged line")


@pytest.mark.parametrize("value", ["a", "tk-0be0b135c334", "A1.b_c-d", "x" * 96])
def test_a_new_id_may_be_a_letter_or_digit_then_letters_digits_dot_underscore_hyphen_up_to_96(value: str) -> None:
    assert pc.new_id_refusal("send", value) is None


@pytest.mark.parametrize("value", [*HOSTILE_IDS, "x" * 97, ".a", "_a", None, 7, ["a"]])
def test_any_other_new_id_is_refused_with_one_escaped_line(value: object) -> None:
    text = pc.new_id_refusal("send", value)

    assert text is not None and text.startswith("agenttalk send: refusing - the new request id ")
    assert "\n" not in text and "\r" not in text and "\u2028" not in text
    assert pc.shown(value) in text  # only ever the escaped display
    assert not isinstance(value, str) or value not in text or pc.shown(value) == '"' + value + '"'


# ------------------------------------------------------------------------- the printing sites (one table)


def _store(tmp_path: Path) -> Store:
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    s.set_role("alpha", "lead")
    return s


def _run(root: Path, *argv: str) -> int:
    return cli.main(["--root", str(root), *argv])


def _hint_text(tmp_path: Path, capsys: pytest.CaptureFixture[str], request_id: str) -> str:
    s = _store(tmp_path)
    s.send(sender="alpha", recipient="beta", body="q", kind="question", meta={"request_id": request_id})
    capsys.readouterr()
    assert _run(s.root, "sync", "--for", "beta", "--json") == 0
    out = json.loads(capsys.readouterr().out)
    return "\n".join(str(t.get("hint")) for t in out["threads"])


def test_site_thread_hint_ordinary_output_is_unchanged(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert "agenttalk reply --to-request tk-0be0b135c334   # you owe a response" in _hint_text(
        tmp_path, capsys, "tk-0be0b135c334"
    )


@pytest.mark.parametrize("request_id", [i for i in HOSTILE_IDS if i and "\r" not in i])
def test_site_thread_hint_with_an_odd_request_id_is_not_a_ready_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], request_id: str
) -> None:
    text = _hint_text(tmp_path, capsys, request_id)

    assert "agenttalk reply --to-request <r>" in text and "agenttalk threads --json" in text
    assert f"reply --to-request {request_id}" not in text
    command_part = text.split("  (not a ready command")[0]  # what a paste would run: the placeholder form only
    assert SENTINEL not in command_part and "&" not in command_part and ";" not in command_part


def _prompt(record: dict, shell: str = "powershell", seat: str | None = "claude-dev") -> str:
    base = {
        "from": "claude-lead",
        "to": "claude-dev",
        "kind": "task",
        "id": "20261111-143102-123456-AbCd",
        "body": "do it",
        "meta": {},
    }
    return assemble_turn_prompt({**base, **record}, reply_shell=shell, seat=seat)


@pytest.mark.parametrize(
    "shell,prefix", [("powershell", '& "$env:AGENTTALK_PY" -m agenttalk'), ("bash", '"$AGENTTALK_PY" -m agenttalk')]
)
def test_site_prompt_ordinary_reply_line_is_unchanged_in_both_shells(shell: str, prefix: str) -> None:
    text = _prompt({"correlation_id": "tk-1", "meta": {"request_id": "tk-1"}}, shell)

    assert f"{prefix} reply --from claude-dev --to-request tk-1 --kind task-response -m 'your answer here'" in text


@pytest.mark.parametrize("shell", ["powershell", "bash"])
@pytest.mark.parametrize("request_id", [i for i in HOSTILE_IDS if i])
def test_site_prompt_an_odd_request_id_gives_way_to_the_plain_message_id(shell: str, request_id: str) -> None:
    text = _prompt({"correlation_id": request_id, "meta": {"request_id": request_id}}, shell)
    reply_lines = [ln for ln in text.splitlines() if "agenttalk reply" in ln and "--kind task-response" in ln]

    assert reply_lines and all("--to-id 20261111-143102-123456-AbCd" in ln for ln in reply_lines)
    assert not any(request_id in ln for ln in reply_lines if request_id.strip())
    # the header echo is one line: the id is escaped, so a newline in it cannot forge a line of instructions
    header = [ln for ln in text.splitlines() if ln.startswith("correlation_id:")]
    assert len(header) == 1 and "\u2028" not in header[0]
    assert not any(ln.strip().startswith(("line2", "nel", "sep", "para", "bidi", "tail")) for ln in text.splitlines())


@pytest.mark.parametrize("shell", ["powershell", "bash"])
def test_site_prompt_with_no_plain_id_at_all_prints_a_placeholder_that_cannot_run(shell: str) -> None:
    text = _prompt({"id": "a b", "correlation_id": "c d", "meta": {"request_id": "c d"}}, shell)

    assert "--to-id <message id: not a plain token here" in text
    assert "--to-request c d" not in text and "--to-id a b" not in text


@pytest.mark.parametrize("seat", ["bad seat", "a;b"])
def test_site_prompt_a_seat_that_is_not_plain_is_not_put_in_the_command(seat: str) -> None:
    text = _prompt({"correlation_id": "tk-1", "meta": {"request_id": "tk-1"}}, seat=seat)

    assert "--from <seat: not a plain token>" in text and f"--from {seat}" not in text


def test_site_reply_refusal_show_recipe_is_a_placeholder_because_a_path_is_never_plain(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from agenttalk import reply_refusals

    s = _store(tmp_path / "my folder")
    mid = "20990101-000000-000000-RFSD"
    reply_refusals.record_reply_refusal(
        s,
        agent="beta",
        original_message_id=mid,
        original_from="alpha",
        intended_kind="message",
        reason="r",
        draft_path=str(s.dir / "reply-drafts" / "beta" / f"{mid}.refused.md"),
        correlation={"request_id": "q-1"},
        at="2099-01-01T00:00:00Z",
    )

    assert _run(s.root, "reply-refusal", "show", "--agent", "beta", "--id", mid) == 0
    out = capsys.readouterr().out

    recipe = [ln for ln in out.splitlines() if ln.startswith("agenttalk reply --from beta")]
    assert len(recipe) == 1 and f"--to-id {mid} --file <f>  (not a ready command: <f> is " in recipe[0]
    assert "\\u005c" in recipe[0] and "\\" not in recipe[0].replace("\\u005c", "")  # backslashes shown as codes


def test_site_interruption_remedy_ordinary_output_and_no_unconditional_retry() -> None:
    text = wrapper_loop._interruption_remedy(
        "beta", "20990101-000000-000000-AbCd", k=3, kind="timeout", budget_seconds=60.0
    )

    assert "agenttalk dead-letter show --agent beta --id 20990101-000000-000000-AbCd" in text
    assert "agenttalk dead-letter requeue --agent beta --id 20990101-000000-000000-AbCd" in text
    assert "requeue as-is" not in text  # the old assurance that a retry is usable is gone
    assert "inspect it first and decide" in text and "refuses, and says why" in text


def test_site_interruption_remedy_with_an_odd_id_has_no_ready_command() -> None:
    text = wrapper_loop._interruption_remedy("beta", f"a; echo {SENTINEL}", k=3, kind="timeout", budget_seconds=60.0)

    assert "--id <i>" in text and f"; echo {SENTINEL}" not in text and "\\u003b" in text


def test_site_usage_park_text_ordinary_and_hostile() -> None:
    plain = usage_park.recovery_text("beta", "20990101-000000-000000-AbCd")
    odd = usage_park.recovery_text("beta", "a b", kind=usage_park.KIND_USAGE_LIMIT)

    assert "agenttalk request-restart --for beta (it needs a running supervisor" in plain
    assert "agenttalk ack --for beta --id 20990101-000000-000000-AbCd (it skips the message" in plain
    assert "--id <m>" in odd and "--id a b" not in odd


def test_site_web_dead_letter_instructions_ordinary_and_hostile() -> None:
    from agenttalk import web

    assert web._dead_letter_group_cli_instructions("beta")[:2] == [
        "agenttalk dead-letter list --agent beta",
        "agenttalk dead-letter show --agent beta --id <message_id>",
    ]
    odd = web._dead_letter_group_cli_instructions(f"a;echo {SENTINEL}")
    assert odd[0].startswith("agenttalk dead-letter list --agent <a>  (not a ready command:")
    assert all("--agent a;echo" not in line for line in odd)


def test_site_powershell_host_selection_commands_for_a_path() -> None:
    text = powershell_host.explicit_select_remediation("C:/Program Files/PowerShell/7/pwsh.exe")

    assert text.startswith("run `agenttalk supervise --select-pwsh --pwsh <p>  (not a ready command: <p> is ")
    assert "Program Files" in text and '--pwsh "C:/' not in text


def test_site_scheduled_task_recovery_shares_one_note_and_never_quotes(tmp_path: Path) -> None:
    s = _store(tmp_path)
    text = sup.task_recovery_remediation(s, "C:/Program Files/PowerShell/7/pwsh.exe", "agenttalk-supervisor")

    assert text.count("not a ready command") == 1  # one note for the five commands
    assert "-Action stop -TaskName agenttalk-supervisor" in text and "<p> is " in text
    assert "'" not in text.split("(not a ready command")[0]  # nothing is quoted: no quoting is right everywhere


def test_site_gateway_rebind_command_for_paths() -> None:
    text = ovh_gateway_service._runtime_rebind_command(Path("C:/a b"), Path("C:/lite llm/x.exe"))

    assert text.startswith("agenttalk --root <r> gateway runtime-rebind --litellm-executable <e>  (not a ready")
    assert "$" not in text and '"' not in text.split("  (not a ready")[0]


def test_site_doctor_select_hint_for_a_path_is_a_placeholder() -> None:
    from agenttalk import powershell_host as psh

    text = doctor.pc.cmd("agenttalk supervise --select-pwsh --pwsh {p}", p="C:/x y")  # the form the check builds
    assert text.startswith("agenttalk supervise --select-pwsh --pwsh <p>  (not a ready command")
    assert psh.explicit_select_remediation("C:/x y").startswith("run `agenttalk supervise --select-pwsh --pwsh <p>")


@pytest.mark.parametrize("hook_state", ["neutral", "fallback-other"])
def test_site_doctor_hook_branches_ordinary_and_hostile(hook_state: str) -> None:
    ordinary = doctor._interactive_hook_warning(
        target="beta", descriptor="d", heartbeat_state="h", hook_state=hook_state
    ).fix
    odd = doctor._interactive_hook_warning(
        target=f"b; echo {SENTINEL}", descriptor="d", heartbeat_state="h", hook_state=hook_state
    ).fix

    assert "agenttalk supervise --install-activity-hook --interactive-for beta" in ordinary
    assert "--interactive-for <t>" in odd and f"; echo {SENTINEL}" not in odd and "\\u003b" in odd
    if hook_state == "neutral":
        assert "set AGENTTALK_SELF=beta before" in ordinary and "set AGENTTALK_SELF=<t>" in odd


@pytest.mark.parametrize("root", ["C:/project folder", "/home/me/my project", "C:/caf\u00e9/\u4e2d\u6587", "plain"])
def test_site_doctor_full_list_hint_never_puts_a_root_in_a_command(root: str) -> None:
    hint = doctor.full_list_hint(root)

    assert hint.startswith("full list: agenttalk doctor --json, run with --root set to the project at ")
    assert "--root '" not in hint and "agenttalk --root " not in hint  # no ready command, whatever the root holds


def test_site_lanes_restamp_hint_in_the_real_verdict() -> None:
    import test_lanes as tl

    verdict = tl._ev(
        tl._lane(epoch_at_assign="e1"), tl._changed("src/core/a.py"), tl._cls(["core"]), current_epoch="e2"
    )

    details = [h["detail"] for h in verdict["holds"]]
    assert "re-stamp the lane: agenttalk lane restamp --id l1 --reason <why>" in " ".join(details)


def test_site_lanes_restamp_hint_with_an_odd_lane_id_has_no_ready_command() -> None:
    import test_lanes as tl

    verdict = tl._ev(
        tl._lane(epoch_at_assign="e1", lane_id=f"l 1; echo {SENTINEL}"),
        tl._changed("src/core/a.py"),
        tl._cls(["core"]),
        current_epoch="e2",
    )

    details = " ".join(h["detail"] for h in verdict["holds"])
    assert f"; echo {SENTINEL}" not in details  # whether the lane is held as malformed or told to re-stamp


# -------------------------------------------------- the relay notice: every value of every printed command


def _obligation(seat: str, request_id: str = "tk-1", result_id: str | None = "res-1", state: str = "owed"):
    from agenttalk import lead_relay

    return lead_relay.Obligation(
        lead="lead",
        seat=seat,
        request_id=request_id,
        result_id=result_id,
        state=state,
        since="2026-01-01T00:00:00Z",
        declaration_id="d-1",
    )


def _relay_lines(**kw) -> list[str]:
    from agenttalk import lead_relay

    return lead_relay.notice_lines([_obligation(**kw)], now_epoch=1.8e9)


def test_site_relay_forward_command_ordinary_output_is_unchanged() -> None:
    lines = _relay_lines(seat="builder")

    assert any(
        ln == '  forward it: agenttalk relay result --result-id res-1 --to builder -m "<decision>"'
        '   or   --none-needed --reason "<why>"'
        for ln in lines
    ), lines


@pytest.mark.parametrize("field", ["seat", "result_id"])
@pytest.mark.parametrize("value", [f"builder; echo {SENTINEL}; #", f"a&echo.{SENTINEL}", "$(whoami)", "a b", "-x"])
def test_site_relay_forward_command_with_a_stored_odd_value_is_not_ready_and_is_escaped(field: str, value: str) -> None:
    kw = {"seat": "builder", "result_id": "res-1", field: value}
    lines = _relay_lines(**kw)
    joined = "\n".join(lines)

    forward = next(ln for ln in lines if ln.startswith("  forward it: "))
    assert "<s>" in forward or "<r>" in forward
    assert f"; echo {SENTINEL}" not in joined and f"&echo.{SENTINEL}" not in joined and "$(whoami)" not in joined
    assert not any(
        SENTINEL in ln and ln.startswith("  forward it: agenttalk relay result --result-id " + value) for ln in lines
    )


def test_site_relay_diagnostic_echo_lines_are_one_escaped_line_each() -> None:
    lines = _relay_lines(seat="a\nb: forged", request_id="x\u2028y", result_id=None, state="unresolved")

    assert all("\n" not in ln for ln in lines) and not any(ln.startswith("b: forged") for ln in lines)
    assert any("\\u000a" in ln for ln in lines) and any("\\u2028" in ln for ln in lines)


@pytest.mark.skipif(os.name != "nt", reason="the launcher that re-reads quotes is Windows only")
@pytest.mark.parametrize("shell", ["pwsh", "powershell"])
def test_launcher_the_relay_forward_line_with_a_stored_separator_runs_no_second_command(
    tmp_path: Path, shell: str
) -> None:
    exe = _windows_shell(shell)
    env, argv_file = _windows_env(tmp_path)
    line = next(ln for ln in _relay_lines(seat=f"builder; echo {SENTINEL}; #") if ln.startswith("  forward it: "))

    done = _run_line(exe, line.removeprefix("  forward it: "), env, tmp_path)

    assert SENTINEL not in done.stdout and _calls(argv_file) == []


def test_bash_the_relay_forward_line_with_a_stored_separator_runs_no_second_command(tmp_path: Path) -> None:
    line = next(ln for ln in _relay_lines(seat=f"builder; echo {SENTINEL}; #") if ln.startswith("  forward it: "))

    done, argv_file = _bash_run(tmp_path, line.removeprefix("  forward it: "))

    assert SENTINEL not in done.stdout and _calls(argv_file) == []


# ------------------------------------- a cut diagnostic always says how to get the complete value


def test_a_cut_value_says_where_the_whole_value_is() -> None:
    assert "the whole value is in the stored record" in pc.shown("x" * 500)
    text = pc.cmd("agenttalk x --id {i}", i="y " * 250, lookup="the whole id is listed by: agenttalk threads --json")
    assert "[cut here: 500 characters in all" in text and "the whole id is listed by: agenttalk threads --json" in text


def test_a_path_up_to_the_windows_limit_is_shown_whole_and_a_longer_one_says_where_it_is() -> None:
    whole = "C:/" + "d" * 150 + "/pwsh.exe"  # 160 characters: the old 60-character display cut it
    text = pc.cmd("agenttalk supervise --select-pwsh --pwsh {p}", p=whole, shown_limit=pc.PATH_SHOWN_MAX_CHARACTERS)

    assert "[cut here" not in text and whole.replace("/", "/") in text.replace("\\u005c", "\\")
    longer = pc.cmd(
        "agenttalk supervise --select-pwsh --pwsh {p}",
        p="C:/" + "d d" * 150 + "/pwsh.exe",
        shown_limit=pc.PATH_SHOWN_MAX_CHARACTERS,
        lookup="the whole path is the pwsh.exe you chose",
    )
    assert "[cut here: 462 characters in all" in longer and "the whole path is the pwsh.exe you chose" in longer


def test_site_powershell_host_path_candidate_with_a_long_path_is_shown_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = "C:\\" + "d" * 150
    monkeypatch.setattr(powershell_host.Path, "is_file", lambda self: True)
    monkeypatch.setattr(powershell_host, "_validate_local_exe_shape", lambda *a, **k: None)

    commands = powershell_host.path_candidate_remediations({"PATH": folder})

    assert len(commands) == 1 and "d" * 150 in commands[0] and "[cut here" not in commands[0]
    assert "Get-Command pwsh -All" in commands[0]


# ------------------------------------------------------------------------------------ the creation table
#
# What counts is what an operation DOES. An operation that opens a NEW request with an id the caller supplied must
# refuse a bad id before anything is written; an operation that carries an id that is already stored (answering an
# old question, a plain reply, a requeue, a retry, a broadcast resume) keeps working with any old id.

BAD_NEW_ID = "new space"
REFUSAL = "refusing - the new "


def _count(s: Store) -> int:
    return len(list(s.messages_dir.glob("*.json")))


def _stderr(capsys: pytest.CaptureFixture[str]) -> str:
    return capsys.readouterr().err


def _opener_argvs(root: Path, rid: str) -> dict[str, list[str]]:
    meta = f"request_id={rid}"
    send = ["send", "--from", "alpha", "--to", "beta", "-m", "x", "--meta", meta]
    return {
        "send question": [*send, "--kind", "question"],
        "send review-request": [*send, "--kind", "review-request"],
        "send proposal": [*send, "--kind", "proposal"],
        "propose": ["propose", "--from", "alpha", "--to", "beta", "-m", "x", "--meta", meta],
        "task": ["task", "--from", "alpha", "--to", "beta", "-m", "x", "--meta", meta],
        "escalate": ["escalate", "--from", "alpha", "-m", "x", "--meta", meta],
        "reply opens review-request": [
            "reply",
            "--from",
            "beta",
            "--to-request",
            "q-anchor",
            "--kind",
            "review-request",
            "-m",
            "x",
            "--meta",
            meta,
        ],
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
            "--meta",
            meta,
        ],
        "reply opens question": [
            "reply",
            "--from",
            "beta",
            "--to-request",
            "q-anchor",
            "--kind",
            "question",
            "-m",
            "x",
            "--meta",
            meta,
        ],
        "broadcast request_id": [
            "broadcast",
            "--from",
            "alpha",
            "--all",
            "--kind",
            "question",
            "-m",
            "x",
            "--meta",
            meta,
        ],
        "broadcast broadcast_id": [
            "broadcast",
            "--from",
            "alpha",
            "--all",
            "--kind",
            "question",
            "-m",
            "x",
            "--meta",
            f"broadcast_id={rid}",
        ],
    }


@pytest.fixture()
def table_store(tmp_path: Path) -> Store:
    s = _store(tmp_path)
    s.send(sender="alpha", recipient="beta", body="anchor", kind="question", meta={"request_id": "q-anchor"})
    return s


@pytest.mark.parametrize("operation", list(_opener_argvs(Path("."), "x")))
def test_creation_every_operation_that_opens_a_request_refuses_a_bad_supplied_id_before_any_write(
    table_store: Store, capsys: pytest.CaptureFixture[str], operation: str
) -> None:
    before = _count(table_store)
    capsys.readouterr()

    assert _run(table_store.root, *_opener_argvs(table_store.root, BAD_NEW_ID)[operation]) == 2

    err = _stderr(capsys)
    assert REFUSAL in err and "\n" not in err.strip()
    assert _count(table_store) == before  # nothing was written


@pytest.mark.parametrize(
    "operation",
    ["send question", "propose", "reply opens review-request", "reply opens proposal", "broadcast request_id"],
)
def test_creation_a_good_supplied_id_is_not_refused_by_the_check(
    table_store: Store, capsys: pytest.CaptureFixture[str], operation: str
) -> None:
    capsys.readouterr()

    code = _run(table_store.root, *_opener_argvs(table_store.root, "good-id.1")[operation])

    assert code == 0 and REFUSAL not in _stderr(capsys)


@pytest.mark.parametrize("length,refused", [(96, False), (97, True)])
def test_creation_the_96_97_boundary_applies_to_new_ids_only(
    table_store: Store, capsys: pytest.CaptureFixture[str], length: int, refused: bool
) -> None:
    capsys.readouterr()
    code = _run(table_store.root, *_opener_argvs(table_store.root, "a" * length)["send question"])

    assert (code == 2 and REFUSAL in _stderr(capsys)) if refused else code == 0
    # a stored id of any length is still readable by every reader
    table_store.send(sender="alpha", recipient="beta", body="old", kind="question", meta={"request_id": "b" * 300})
    assert _run(table_store.root, "threads", "--for", "beta", "--json") == 0


def test_creation_an_empty_supplied_id_is_refused_but_an_absent_one_gets_a_minted_id(
    table_store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    argv = ["send", "--from", "alpha", "--to", "beta", "--kind", "question", "-m", "x"]

    assert _run(table_store.root, *argv, "--meta", "request_id=") == 2 and REFUSAL in _stderr(capsys)
    assert _run(table_store.root, *argv) == 0  # no id supplied: the code mints a plain one


def test_creation_old_ids_keep_working_where_the_operation_only_carries_a_stored_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    s = _store(tmp_path)
    old = "old space"
    s.send(sender="alpha", recipient="beta", body="legacy question", kind="question", meta={"request_id": old})
    capsys.readouterr()

    # send --kind message answering the old question: a send, but it opens nothing
    assert (
        _run(
            s.root,
            "send",
            "--from",
            "beta",
            "--to",
            "alpha",
            "--kind",
            "message",
            "-m",
            "answer",
            "--meta",
            f"request_id={old}",
        )
        == 0
    )
    # a plain reply echoes the stored id
    assert _run(s.root, "reply", "--from", "beta", "--to-request", old, "-m", "answer 2") == 0
    assert REFUSAL not in _stderr(capsys)
    # and the stored old thread is still readable and listed
    assert _run(s.root, "threads", "--for", "beta", "--json") == 0


def test_creation_the_check_is_not_in_store_send_so_library_callers_and_readers_are_untouched(tmp_path: Path) -> None:
    s = _store(tmp_path)

    s.send(sender="alpha", recipient="beta", body="x", kind="question", meta={"request_id": BAD_NEW_ID})

    assert any((m.meta or {}).get("request_id") == BAD_NEW_ID for m in s.valid_messages())


def test_creation_the_refusal_message_never_carries_the_raw_value_or_a_second_line(
    table_store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    argv = _opener_argvs(table_store.root, f"x forged line\n{SENTINEL}")["send question"]
    # a literal newline cannot come through argv on every platform; a Unicode separator can
    assert _run(table_store.root, *argv) == 2
    err = capsys.readouterr().err

    assert len(err.strip().splitlines()) == 1 and " " not in err and "u2028" in err


# --------------------------------------------- the real launcher and shells, feeding an argument recorder


def _recorder(tmp_path: Path) -> tuple[Path, Path]:
    """A stand-in `agenttalk` package that writes the argv it received to a file, one JSON list per call."""
    package = tmp_path / "recorder" / "agenttalk"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "__main__.py").write_text(
        "import json, os, sys\n"
        "with open(os.environ['ARGV_OUT'], 'a', encoding='utf-8') as out:\n"
        "    out.write(json.dumps(sys.argv[1:]) + '\\n')\n",
        encoding="utf-8",
    )
    return tmp_path / "recorder", tmp_path / "argv.jsonl"


def _calls(argv_file: Path) -> list[list[str]]:
    return (
        [json.loads(line) for line in argv_file.read_text(encoding="utf-8").splitlines()] if argv_file.exists() else []
    )


def _windows_shell(name: str) -> str:
    candidates = {
        "pwsh": [shutil.which("pwsh"), r"C:\Program Files\PowerShell\7\pwsh.exe"],
        "powershell": [
            os.path.join(
                os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0", "powershell.exe"
            )
        ],
    }[name]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    pytest.skip(f"{name} is not installed here")


def _windows_env(tmp_path: Path) -> tuple[dict[str, str], Path]:
    recorder_dir, argv_file = _recorder(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = sup.agenttalk_shim(sys.executable).replace("__AGENTTALK_GENERATION__", "0" * 64)
    (bin_dir / "agenttalk.cmd").write_bytes(shim.encode("utf-8"))  # the project's own launcher, as it is written
    env = {
        **os.environ,
        "PATH": f"{bin_dir};{os.environ['PATH']}",
        "PYTHONPATH": str(recorder_dir),
        "ARGV_OUT": str(argv_file),
    }
    env.pop("AGENTTALK_PYTHON", None)
    return env, argv_file


def _run_line(shell_exe: str, line: str, env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [shell_exe, "-NoProfile", "-NonInteractive", "-Command", line],
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd,
        timeout=120,
        check=False,
    )


@pytest.mark.skipif(os.name != "nt", reason="agenttalk.cmd, the launcher that re-reads quotes, is Windows only")
@pytest.mark.parametrize("shell", ["pwsh", "powershell"])
def test_launcher_a_plain_value_reaches_the_program_as_exactly_one_argument(tmp_path: Path, shell: str) -> None:
    exe = _windows_shell(shell)
    env, argv_file = _windows_env(tmp_path)

    done = _run_line(exe, pc.cmd("agenttalk reply --to-request {r}", r="tk-0be0b135c334"), env, tmp_path)

    assert done.returncode == 0, done.stderr
    assert _calls(argv_file) == [["reply", "--to-request", "tk-0be0b135c334"]]


@pytest.mark.skipif(os.name != "nt", reason="agenttalk.cmd, the launcher that re-reads quotes, is Windows only")
@pytest.mark.parametrize("shell", ["pwsh", "powershell"])
@pytest.mark.parametrize("value", [f"a&echo.{SENTINEL}", f"a; echo {SENTINEL}", "a%PATH%b", 'say "hi"', "it's", ""])
def test_launcher_an_odd_value_printed_by_the_helper_runs_nothing_and_leaks_no_second_command(
    tmp_path: Path, shell: str, value: str
) -> None:
    exe = _windows_shell(shell)
    env, argv_file = _windows_env(tmp_path)

    done = _run_line(exe, pc.cmd("agenttalk reply --to-request {r}", r=value), env, tmp_path)

    assert _calls(argv_file) == []  # the program was never called, with this value or any other
    assert SENTINEL not in done.stdout  # and no second command ran


@pytest.mark.skipif(os.name != "nt", reason="agenttalk.cmd, the launcher that re-reads quotes, is Windows only")
@pytest.mark.parametrize("shell", ["pwsh", "powershell"])
def test_launcher_the_old_quoting_really_did_run_a_second_command_so_this_test_can_see_the_problem(
    tmp_path: Path, shell: str
) -> None:
    exe = _windows_shell(shell)
    env, _argv_file = _windows_env(tmp_path)

    done = _run_line(exe, f"agenttalk reply --to-request 'a&echo.{SENTINEL}'", env, tmp_path)

    assert SENTINEL in done.stdout  # the single-quoted form was split by cmd.exe: the very defect #727 closes


def _bash() -> str:
    candidates = [shutil.which("bash")]
    if os.name == "nt":
        candidates = [os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Git", "bin", "bash.exe")]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    pytest.skip("bash is not installed here")


def _bash_run(tmp_path: Path, line: str) -> tuple[subprocess.CompletedProcess[str], Path]:
    recorder_dir, argv_file = _recorder(tmp_path)
    env = {**os.environ, "PYTHONPATH": str(recorder_dir), "ARGV_OUT": str(argv_file), "PYEXE": sys.executable}
    script = 'agenttalk() { "$PYEXE" -m agenttalk "$@"; }; ' + line
    done = subprocess.run(
        [_bash(), "-c", script], capture_output=True, text=True, env=env, cwd=tmp_path, timeout=120, check=False
    )
    return done, argv_file


def test_bash_a_plain_value_reaches_the_program_as_exactly_one_argument(tmp_path: Path) -> None:
    done, argv_file = _bash_run(tmp_path, pc.cmd("agenttalk reply --to-request {r}", r="tk-0be0b135c334"))

    assert done.returncode == 0, done.stderr
    assert _calls(argv_file) == [["reply", "--to-request", "tk-0be0b135c334"]]


@pytest.mark.parametrize("value", [f"a; echo {SENTINEL}", f"a&echo {SENTINEL}", "$(echo " + SENTINEL + ")", "it's", ""])
def test_bash_an_odd_value_printed_by_the_helper_runs_nothing_and_leaks_no_second_command(
    tmp_path: Path, value: str
) -> None:
    done, argv_file = _bash_run(tmp_path, pc.cmd("agenttalk reply --to-request {r}", r=value))

    assert _calls(argv_file) == [] and SENTINEL not in done.stdout


def test_zsh_smoke_a_plain_value_is_one_argument_where_zsh_is_installed(tmp_path: Path) -> None:
    zsh = shutil.which("zsh")
    if zsh is None:
        pytest.skip("zsh is not installed here")
    recorder_dir, argv_file = _recorder(tmp_path)
    env = {**os.environ, "PYTHONPATH": str(recorder_dir), "ARGV_OUT": str(argv_file), "PYEXE": sys.executable}
    line = 'agenttalk() { "$PYEXE" -m agenttalk "$@"; }; ' + pc.cmd("agenttalk reply --to-request {r}", r="tk-1")

    subprocess.run([zsh, "-c", line], env=env, cwd=tmp_path, timeout=120, check=True)

    assert _calls(argv_file) == [["reply", "--to-request", "tk-1"]]
