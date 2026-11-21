import copy
import json
import os
from types import SimpleNamespace

import pytest

from agenttalk import cli, powershell_host, printed_commands


@pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
def test_real_escaped_warnings_hide_home_but_show_personal_preserves_it(
    tmp_path, monkeypatch, capsys, as_json
):
    home = r"C:\Users\invented-é~(login)"
    host = home + r"\scoop\pwsh.exe"
    outside = r"D:\Tools\pwsh.exe"
    explicit = powershell_host.explicit_select_remediation(host)
    command, note = powershell_host.select_pwsh_commands(host)
    outside_warning = powershell_host.explicit_select_remediation(outside)
    assert note is not None
    advice = cli._drain_advice(host)
    status_advice = cli._status_warnings(
        [{"name": host, "unread": 1, "cursor": None, "capacity": {"state": "observed"}}], {host}
    )[0]
    fake_store = SimpleNamespace(
        operator_facing=lambda: None, cursor=lambda _: None, retired_agents=lambda: []
    )
    row = SimpleNamespace(state="reply-waiting", request_id=host, needs_operator=False, is_broadcast=False)
    monkeypatch.setattr(cli, "_closed_rids", lambda *_: set())
    monkeypatch.setattr(cli.th, "group_messages_by_request_id", lambda _: {})
    monkeypatch.setattr(cli.th, "derive_threads", lambda *_, **__: [row])
    thread_advice = cli._thread_warnings(
        fake_store, {"agents": ["alpha"]}, valid_msgs=[], operator_questions=[]
    )[0]
    assert printed_commands.shown(host) in explicit
    assert printed_commands.shown(host) in note
    assert printed_commands.shown(host) in advice
    assert printed_commands.shown(host) in status_advice
    assert printed_commands.shown(host) in thread_advice
    warning = command + " " + note
    payload = {
        "root": str(tmp_path),
        "project_id": "invented-project",
        "session_id": "s",
        "signing_enforced": False,
        "message_count": 0,
        "invalid_messages": [],
        "agents": [],
        "warnings": [explicit, warning, advice, status_advice, thread_advice, outside_warning],
        "powershell_host": {"status": "warn", "warning": explicit, "path": host},
    }
    assert cli.main(["init", "--path", str(tmp_path), "--agents", "alpha"]) == 0
    capsys.readouterr()
    monkeypatch.setattr(cli, "_gather_status", lambda store: copy.deepcopy(payload))
    args = ["--root", str(tmp_path), "status", *(["--json"] if as_json else [])]
    with monkeypatch.context() as patch:
        patch.setattr(cli.os.path, "expanduser", lambda _: home)
        patch.setenv("USERPROFILE", home)
        patch.setenv("LOCALAPPDATA", home + r"\AppData\Local")
        assert cli.main(args) == 0
        result = capsys.readouterr().out
        if as_json:
            data = json.loads(result)
            messages = [data["powershell_host"]["warning"], *data["warnings"]]
            assert data["warnings"][-1] == outside_warning
        else:
            messages = [result]
            assert outside_warning in result
        for message in messages:
            assert "invented-" not in message
        assert "<home>" in result
        assert cli.main(args + ["--show-personal"]) == 0
        result = capsys.readouterr().out
        if as_json:
            data = json.loads(result)
            assert data["powershell_host"]["warning"] == explicit
            assert data["warnings"] == payload["warnings"]
        else:
            assert explicit in result and warning in result and advice in result


@pytest.mark.parametrize("upper_hex", [False, True])
def test_shown_escaped_home_accepts_both_hex_cases(monkeypatch, upper_hex):
    home = r"C:\Users\invented-é~(login)"
    text = printed_commands.shown(home + r"\pwsh.exe")
    if upper_hex:
        text = text.replace("005c", "005C").replace("00e9", "00E9").replace("007e", "007E")
    with monkeypatch.context() as patch:
        patch.setattr(cli.os.path, "expanduser", lambda _: home)
        patch.setenv("LOCALAPPDATA", home + r"\AppData\Local")
        assert cli._status_redact_home(text).startswith('"<home>')
        outside = printed_commands.shown(home + r"-other\pwsh.exe")
        assert cli._status_redact_home(outside) == outside


@pytest.mark.skipif(os.name != "nt", reason="Windows paths are case-insensitive")
def test_shown_home_respects_windows_non_ascii_case(monkeypatch):
    home = r"C:\Users\invented-É~(login)"
    text = printed_commands.shown(r"c:\users\invented-é~(login)\pwsh.exe")
    monkeypatch.setattr(cli.os.path, "expanduser", lambda _: home)
    monkeypatch.setenv("LOCALAPPDATA", home + r"\AppData\Local")
    assert cli._status_redact_home(text).startswith('"<home>')
