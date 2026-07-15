from __future__ import annotations

import contextlib
import json
import subprocess
from pathlib import Path

from agenttalk import doctor
from agenttalk import powershell_host as psh
from agenttalk import supervisor as sup
from agenttalk import supervisor_lifecycle as lifecycle
from agenttalk.store import Store


PWSH = r"C:\Program Files\PowerShell\7\pwsh.exe"


def _store(tmp_path: Path) -> Store:
    store = Store(tmp_path)
    store.init(["lead", "worker"])
    return store


def _record(store: Store, *, task_name: str | None = "custom-task") -> dict:
    identity = psh.NativeFileIdentity(
        "win32-file-id-v1", PWSH, "aabbccdd", "01", 123, 456,
    )
    version = psh.PowerShellVersion(7, 6, 3)
    return {
        "schema": psh.SELECTION_SCHEMA,
        "project_id": store.project_id(),
        "path": PWSH,
        "source": "program_files",
        "version": version.to_dict(),
        "edition": "Core",
        "probed_at": "2026-07-15T12:00:00.000000Z",
        "identity": identity.to_dict(),
        "task_name": task_name,
        "selection_revision": 1,
        "selection_fingerprint": "a" * 64,
        "_version": version,
        "_identity": identity,
        "_warning": None,
        "_age_seconds": 1.0,
    }


def _query_output(tasks: list[dict]) -> str:
    payload = {"sentinel": "agenttalk-task-query-v1", "tasks": tasks}
    return doctor._TASK_QUERY_SENTINEL + json.dumps(payload) + "\n"


def _task(store: Store, *, execute: str = PWSH) -> dict:
    expected = sup.expected_task_action(store)
    return {
        "name": "custom-task",
        "path": "\\",
        "state": "Ready",
        "actions": [{
            "execute": execute,
            "arguments": expected["arguments"],
            "working_directory": expected["working_directory"],
        }],
    }


def test_doctor_queries_task_only_through_selected_host_and_treats_action_as_data(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = _store(tmp_path)
    record = _record(store)
    evil = r"C:\Users\operator\evil.exe"
    calls = []

    @contextlib.contextmanager
    def selected(_store):
        yield record

    monkeypatch.setattr(lifecycle, "selected_host_for_spawn", selected)

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, _query_output([_task(store, execute=evil)]), "")

    result = doctor._inspect_selected_task(store, record, runner=runner)
    assert result["status"] == "mismatch"
    assert "Execute" in result["detail"]
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv[0] == PWSH
    assert evil not in argv
    assert kwargs["check"] is False
    assert "shell" not in kwargs
    assert kwargs["env"]["AGENTTALK_TASK_NAME"] == "custom-task"


def test_doctor_task_status_ok_missing_ambiguous_and_garbage(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = _store(tmp_path)
    record = _record(store)

    @contextlib.contextmanager
    def selected(_store):
        yield record

    monkeypatch.setattr(lifecycle, "selected_host_for_spawn", selected)

    def result(stdout: str, returncode: int = 0):
        return lambda argv, **kwargs: subprocess.CompletedProcess(argv, returncode, stdout, "")

    assert doctor._inspect_selected_task(
        store, record, runner=result(_query_output([_task(store)])),
    )["status"] == "ok"
    assert doctor._inspect_selected_task(
        store, record, runner=result(_query_output([])),
    )["status"] == "missing"
    assert doctor._inspect_selected_task(
        store, record, runner=result(_query_output([_task(store), _task(store)])),
    )["status"] == "ambiguous"
    assert doctor._inspect_selected_task(
        store, record, runner=result("garbage"),
    )["status"] == "unknown"
    assert doctor._inspect_selected_task(
        store, record, runner=result("", returncode=3),
    )["status"] == "unknown"


def test_doctor_host_warning_and_task_mismatch_are_durable_in_data(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = _store(tmp_path)
    record = _record(store)
    record["_version"] = psh.PowerShellVersion(7, 3, 0)
    record["version"] = record["_version"].to_dict()
    record["_warning"] = psh.host_warning("Core", record["_version"])
    monkeypatch.setattr(lifecycle, "read_selected_host", lambda store: record)
    monkeypatch.setattr(
        doctor,
        "_inspect_selected_task",
        lambda store, record: {"status": "mismatch", "detail": "Execute differs"},
    )
    monkeypatch.setattr(doctor.sys, "platform", "win32")

    check = doctor._check_powershell_host(store)
    assert check.status == "error"
    assert check.data["warning"] and "end-of-life" in check.data["warning"]
    assert check.data["task_status"] == "mismatch"
    assert "-Action stop" in check.fix
    assert "--refresh-scripts" in check.fix


def test_doctor_without_selection_reports_task_unknown(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = _store(tmp_path)
    monkeypatch.setattr(doctor.sys, "platform", "win32")
    monkeypatch.setattr(
        lifecycle,
        "read_selected_host",
        lambda store: (_ for _ in ()).throw(lifecycle.SupervisorLifecycleError("absent")),
    )
    check = doctor._check_powershell_host(store)
    assert check.status == "error"
    assert check.data["task_status"] == "unknown"
    assert "select-pwsh" in check.fix
