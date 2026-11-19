"""Deeply nested damaged files must follow the normal parse-error paths."""

import json

import pytest

from agenttalk import cli, doctor, scratch, supervisor_lifecycle
from agenttalk.store import Store


DEPTH = 30000
DEEP_JSON = "[" * DEPTH + "]" * DEPTH

try:
    json.loads(DEEP_JSON)
except RecursionError:
    DEEP_DECODER_RAISES = True
else:
    DEEP_DECODER_RAISES = False


@pytest.fixture(params=["deep", "forced"])
def bad_json(request, monkeypatch):
    if request.param == "deep":
        if not DEEP_DECODER_RAISES:
            pytest.skip("JSON decoder accepts the 30,000-level input on this runtime")
        return DEEP_JSON
    content = '{"force_recursion": true}'
    original = json.loads

    def loads(value, *args, **kwargs):
        if value == content:
            raise RecursionError("forced decoder failure")
        return original(value, *args, **kwargs)

    monkeypatch.setattr(json, "loads", loads)
    return content


def test_selection_reader_reports_nested_json(tmp_path, bad_json):
    store = Store(tmp_path)
    store.dir.mkdir(parents=True)
    path = supervisor_lifecycle.selection_path(store)
    path.write_text(bad_json, encoding="utf-8")
    assert path.stat().st_size < 64 * 1024
    with pytest.raises(supervisor_lifecycle.SupervisorLifecycleError, match="selection is malformed"):
        supervisor_lifecycle.read_selected_host_locked(store)


def test_doctor_reports_nested_selection(tmp_path, monkeypatch, bad_json):
    store = Store(tmp_path)
    store.dir.mkdir(parents=True)
    supervisor_lifecycle.selection_path(store).write_text(bad_json, encoding="utf-8")
    monkeypatch.setattr(doctor.sys, "platform", "win32")
    check = doctor._check_powershell_host(store)
    assert check.status == "error" and "selection is malformed" in check.details


def test_config_reader_reports_nested_json(tmp_path, bad_json):
    store = Store(tmp_path)
    store.init(["alpha"])
    store.config_path.write_text(bad_json, encoding="utf-8")
    with pytest.raises(ValueError, match="config"):
        store.load_config()


def test_force_init_repairs_nested_config(tmp_path, monkeypatch, bad_json, capsys):
    store = Store(tmp_path)
    store.init(["alpha"])
    store.config_path.write_text(bad_json, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    capsys.readouterr()
    assert cli.main(["init", "--here", "--agents", "alpha,beta", "--force"]) == 0
    assert json.loads(store.config_path.read_text(encoding="utf-8"))["agents"] == ["alpha", "beta"]


def test_doctor_reports_nested_config(tmp_path, bad_json):
    store = Store(tmp_path)
    store.init(["alpha"])
    store.config_path.write_text(bad_json, encoding="utf-8")
    check = doctor._check_init(store)
    assert check.status == "error" and "config" in check.details
    report = doctor.run(tmp_path)
    assert report.overall == "error"
    assert any(item.name == "store.initialized" and item.status == "error" for item in report.checks)


def test_scratch_config_ignores_nested_json(tmp_path, bad_json):
    store = Store(tmp_path)
    store.init(["alpha"])
    store.config_path.write_text(bad_json, encoding="utf-8")
    assert scratch._read_scratch_config(tmp_path) == {}


@pytest.mark.parametrize("file_kind", ["config", "selection"])
@pytest.mark.parametrize("as_json", [False, True], ids=["plain", "json"])
def test_full_doctor_reports_nested_file(tmp_path, monkeypatch, capsys, bad_json, file_kind, as_json):
    store = Store(tmp_path)
    store.init(["alpha"])
    path = store.config_path if file_kind == "config" else supervisor_lifecycle.selection_path(store)
    path.write_text(bad_json, encoding="utf-8")
    host_check = doctor._check_powershell_host

    def check_as_windows(selected_store):
        with monkeypatch.context() as host_patch:
            host_patch.setattr(doctor.sys, "platform", "win32")
            return host_check(selected_store)

    monkeypatch.setattr(doctor, "_check_powershell_host", check_as_windows)
    capsys.readouterr()
    args = ["--root", str(tmp_path), "doctor"] + (["--json"] if as_json else [])
    assert cli.main(args) == 2
    output = capsys.readouterr().out
    if as_json:
        report = json.loads(output)
        check_name = "store.initialized" if file_kind == "config" else "powershell_host"
        check = next(item for item in report["checks"] if item["name"] == check_name)
        assert check["status"] == "error"
        output = check["details"]
    assert ("config" if file_kind == "config" else "selection is malformed") in output
