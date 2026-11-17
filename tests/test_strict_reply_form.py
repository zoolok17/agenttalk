"""A wrapped Claude seat in a permission mode other than bypassPermissions is told to run agenttalk with the
interpreter's written-out path, because that mode refuses a command that starts with a variable (#822)."""

from __future__ import annotations

import os

import pytest

from agenttalk import cli
from agenttalk.wrapper import prompt, run, session
from test_wrapper_loop import _codex_turn_lines, _store

STRICT = ["claude", "--permission-mode", "dontAsk"]
TASK = {"id": "m-1", "from": "lead", "to": "beta", "kind": "task", "body": "do it",
        "correlation_id": "tk-1", "request_id": "tk-1", "broadcast_id": None, "meta": {"request_id": "tk-1"}}


def _python(tmp_path, folder="with space"):
    path = tmp_path / folder / "Scripts" / "python.exe"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"")
    return str(path)


def _written(path: str) -> str:
    return '"' + (path.replace("\\", "/") if os.name == "nt" else path) + '" -m agenttalk'


def test_strict_mode_renders_the_written_out_path_with_a_space(tmp_path) -> None:
    py = _python(tmp_path)
    invocation = prompt.strict_reply_invocation("claude", STRICT, py)
    assert invocation == _written(py) and " " in invocation and "\\" not in invocation

    text = prompt.assemble_turn_prompt(TASK, reply_shell="bash", reply_invocation=invocation, seat="beta")

    assert "AGENTTALK_PY" not in text and '& "' not in text
    assert f"  {invocation} reply --from beta --to-request tk-1 --kind task-response" in text
    assert text.endswith("say so in your reply.")
    assert f"Run every agenttalk command with the Bash tool, in the form shown above: `{invocation} " in text
    cadence = prompt.assemble_cadence_prompt({}, [], reply_shell="powershell", reply_invocation=invocation)
    assert "AGENTTALK_PY" not in cadence and f"`{invocation} escalate`" in cadence


@pytest.mark.skipif(os.name != "nt", reason="a Windows path is written with backslashes")
def test_a_windows_path_is_written_with_forward_slashes(tmp_path) -> None:
    py = _python(tmp_path, "plain")
    assert "\\" in py
    assert prompt.strict_reply_invocation("claude", ["claude", "--permission-mode=acceptEdits"], py) == (
        '"' + py.replace("\\", "/") + '" -m agenttalk')


@pytest.mark.skipif(os.name == "nt", reason="Windows cannot name a file with a backslash")
def test_a_posix_path_holding_a_backslash_keeps_the_old_form(tmp_path) -> None:
    py = _python(tmp_path, "back\\slash")
    assert prompt.strict_reply_invocation("claude", STRICT, py) is None


@pytest.mark.parametrize("folder", ['quote"d', "dollar$x", "back`tick"])
def test_a_path_unsafe_in_double_quotes_keeps_the_old_form(tmp_path, folder) -> None:
    if os.name == "nt" and '"' in folder:
        pytest.skip("Windows cannot name a folder with a double quote")
    assert prompt.strict_reply_invocation("claude", STRICT, _python(tmp_path, folder)) is None


@pytest.mark.parametrize("cli_name,argv", [
    ("claude", ["claude"]),
    ("claude", ["claude", "--permission-mode", "bypassPermissions"]),
    ("claude", ["claude", "--permission-mode=bypassPermissions"]),
    ("claude", ["claude", "--permission-mode"]),
    ("codex", ["codex", "--permission-mode", "dontAsk"]),
])
@pytest.mark.parametrize("reply_shell", ["bash", "powershell"])
def test_bypass_and_other_launches_stay_byte_identical(tmp_path, cli_name, argv, reply_shell) -> None:
    invocation = prompt.strict_reply_invocation(cli_name, argv, _python(tmp_path))
    assert invocation is None
    before = prompt.assemble_turn_prompt(TASK, seat="beta")  # the PowerShell form, as before #822
    expected = before.replace(prompt._POWERSHELL_INVOCATION, prompt._BASH_INVOCATION) if reply_shell == "bash" \
        else before
    assert prompt.assemble_turn_prompt(TASK, reply_shell=reply_shell, reply_invocation=invocation,
                                       seat="beta") == expected


@pytest.mark.parametrize("value", [None, "", "python.exe", "Scripts/python.exe", 5])
def test_an_unset_or_relative_agenttalk_py_keeps_the_old_form(value) -> None:
    assert prompt.strict_reply_invocation("claude", STRICT, value) is None


def test_a_missing_file_or_a_folder_keeps_the_old_form(tmp_path) -> None:
    assert prompt.strict_reply_invocation("claude", STRICT, str(tmp_path / "missing.exe")) is None
    assert prompt.strict_reply_invocation("claude", STRICT, str(tmp_path)) is None


def test_the_turn_and_the_cadence_drive_carry_the_written_out_form(tmp_path) -> None:
    s = _store(tmp_path)
    invocation = _written(_python(tmp_path))
    stdins: list[str] = []

    def spawn(argv, stdin):
        stdins.append(stdin or "")
        return _codex_turn_lines()

    drive = run.make_drive(s, "beta", "codex", session.SessionState(cli="codex"), ["codex"], spawn=spawn,
                           clock=lambda: 0.0, render=False, reply_shell="bash", reply_invocation=invocation)
    drive(dict(TASK, kind="question"))
    cadence = run.make_cadence_drive(s, "beta", "codex", session.SessionState(cli="codex"), ["codex"],
                                     spawn=spawn, clock=lambda: 0.0, render=False, reply_invocation=invocation)
    cadence({"agent": "beta"}, [{"type": "dead_letter", "key": "dl:1"}])

    assert len(stdins) == 2
    for stdin in stdins:
        assert f"{invocation} " in stdin and "AGENTTALK_PY" not in stdin


def test_the_wrap_loop_hands_the_form_to_the_turn_and_the_cadence_drive(tmp_path, monkeypatch) -> None:
    import agenttalk.store as store_mod
    from agenttalk.wrapper import loop
    from test_lead_loop_wp3 import _store as _lead_store

    s = _lead_store(tmp_path)
    s.set_managed_lead_loop("beta")
    monkeypatch.setattr(store_mod, "_process_liveness", lambda pid: store_mod.PROC_ALIVE)
    seen: dict = {}
    monkeypatch.setattr(run, "make_drive",
                        lambda *a, **kw: seen.__setitem__("turn", kw["reply_invocation"]) or (lambda rec: True))
    monkeypatch.setattr(run, "make_cadence_drive",
                        lambda *a, **kw: seen.__setitem__("cadence", kw["reply_invocation"]) or (lambda *x: True))
    monkeypatch.setattr(loop, "run_loop", lambda store, agent, drive, **kw: 0)

    invocation = '"/env/bin/python" -m agenttalk'
    cli._wrap_loop_mode(s, "beta", cli="codex", base_argv=["python", "-c", "pass"], sender="beta",
                        min_interval=5.0, render=False, lead_loop=True, reply_invocation=invocation)

    assert seen == {"turn": invocation, "cadence": invocation}


def _wrap(tmp_path, monkeypatch, py, *tail):
    _store(tmp_path)
    seen: dict = {}
    monkeypatch.setattr(run, "_agenttalk_py", lambda: py)
    monkeypatch.setattr(run, "preflight_launch_runtime",
                        lambda argv, _cli, _root, _env: run.LaunchPreflightResult(list(argv)))
    monkeypatch.setattr(cli, "_wrap_loop_mode", lambda store, agent, **kwargs: seen.update(kwargs) or 0)
    claude = tmp_path / "claude.exe"
    claude.write_bytes(b"")
    assert cli.main(["--root", str(tmp_path), "wrap", "--for", "beta", "--cli", "claude", "--loop", "--",
                     str(claude), *tail]) == 0
    return seen


def test_wrap_names_the_allow_rule_and_hands_the_form_to_the_loop(tmp_path, monkeypatch, capsys) -> None:
    py = _python(tmp_path)
    seen = _wrap(tmp_path, monkeypatch, py, "--permission-mode", "dontAsk")

    invocation = _written(py)
    assert seen["reply_invocation"] == invocation
    err = capsys.readouterr().err
    assert f"add this allow rule to its settings: Bash({invocation} *)\n" in err
    assert err.count("allow rule") == 1


@pytest.mark.parametrize("tail", [(), ("--permission-mode", "bypassPermissions")])
def test_wrap_in_bypass_mode_prints_no_rule_and_keeps_the_old_form(tmp_path, monkeypatch, capsys, tail) -> None:
    seen = _wrap(tmp_path, monkeypatch, _python(tmp_path), *tail)

    assert seen["reply_invocation"] is None
    assert "allow rule" not in capsys.readouterr().err
