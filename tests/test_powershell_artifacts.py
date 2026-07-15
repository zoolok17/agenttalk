from __future__ import annotations

import os
from pathlib import Path

import pytest

from agenttalk import supervisor as sup
from agenttalk import supervisor_lifecycle
from agenttalk.store import Store, _process_start_token


def _store(tmp_path: Path) -> Store:
    store = Store(tmp_path)
    store.init(["lead", "worker"])
    return store


def test_rendered_artifacts_share_derived_marker_and_are_bom_free(tmp_path: Path) -> None:
    store = _store(tmp_path)
    sup.init(store, python_exe=r"C:\Python\python.exe")
    result = sup.validate_artifact_bundle(
        store, python_exe=r"C:\Python\python.exe"
    )
    generations = {
        marker["generator_generation"] for marker in result["markers"].values()
    }
    assert len(generations) == 1
    for relative in sup.ARTIFACT_RELATIVE_PATHS:
        raw = (store.dir / Path(relative)).read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf")
    for relative in ("supervisor.ps1", "supervisor-task.ps1", "deadman.ps1"):
        text = (store.dir / relative).read_text(encoding="utf-8")
        first_executable = next(
            line for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
        assert first_executable.startswith("param(")
        assert "#requires -Version 7" in text.splitlines()[:8]
        assert "#requires -PSEdition Core" in text.splitlines()[:8]


def test_generation_changes_with_python_pin_and_is_deterministic(tmp_path: Path) -> None:
    store = _store(tmp_path)
    one = sup.render_artifact_bundle(store, python_exe=r"C:\PythonA\python.exe")
    again = sup.render_artifact_bundle(store, python_exe=r"C:\PythonA\python.exe")
    two = sup.render_artifact_bundle(store, python_exe=r"C:\PythonB\python.exe")
    assert one == again
    assert one != two


def test_validation_uses_baked_python_pin_not_runtime_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    sup.init(store, python_exe=r"C:\PythonA\python.exe")
    monkeypatch.setattr(sup.sys, "executable", r"C:\PythonB\python.exe")

    sup.validate_artifact_bundle(store, boundary="supervisor")


def test_generation_changes_with_checkout_identity(tmp_path: Path) -> None:
    first = _store(tmp_path / "first")
    second = _store(tmp_path / "second")
    one = sup.render_artifact_bundle(first, python_exe=r"C:\Python\python.exe")
    two = sup.render_artifact_bundle(second, python_exe=r"C:\Python\python.exe")
    assert one != two
    for relative in sup.ARTIFACT_RELATIVE_PATHS:
        assert b"__AGENTTALK_CHECKOUT_ID__" not in one[relative]


def test_artifact_validation_rejects_content_drift_and_wrong_shim_pin(tmp_path: Path) -> None:
    store = _store(tmp_path)
    sup.init(store, python_exe=r"C:\Python\python.exe")
    script = store.dir / "supervisor.ps1"
    script.write_bytes(script.read_bytes() + b"# retained drift\n")
    with pytest.raises(sup.ArtifactValidationError, match="rendered content"):
        sup.validate_artifact_bundle(store, python_exe=r"C:\Python\python.exe")

    sup.refresh_artifacts(store, python_exe=r"C:\Python\python.exe")
    shim = store.dir / "bin" / "agenttalk.cmd"
    shim.write_bytes(shim.read_bytes().replace(b"C:\\Python", b"D:\\Python"))
    with pytest.raises(sup.ArtifactValidationError, match="rendered content"):
        sup.validate_artifact_bundle(store, python_exe=r"C:\Python\python.exe")


def test_artifact_validation_is_ps_bom_tolerant_but_cmd_bom_strict(tmp_path: Path) -> None:
    store = _store(tmp_path)
    sup.init(store, python_exe=r"C:\Python\python.exe")
    ps1 = store.dir / "supervisor.ps1"
    ps1.write_bytes(b"\xef\xbb\xbf" + ps1.read_bytes())
    sup.validate_artifact_bundle(store, python_exe=r"C:\Python\python.exe")

    shim = store.dir / "bin" / "agenttalk.cmd"
    shim.write_bytes(b"\xef\xbb\xbf" + shim.read_bytes())
    with pytest.raises(sup.ArtifactValidationError, match="BOM-free"):
        sup.validate_artifact_bundle(store, python_exe=r"C:\Python\python.exe")


def test_boundary_matrix_only_requires_the_declared_artifacts(tmp_path: Path) -> None:
    store = _store(tmp_path)
    sup.init(store, python_exe=r"C:\Python\python.exe")
    task = store.dir / "supervisor-task.ps1"
    task.write_bytes(task.read_bytes() + b"# stale\n")
    sup.validate_artifact_bundle(
        store, boundary="supervisor", python_exe=r"C:\Python\python.exe"
    )
    sup.validate_artifact_bundle(
        store, boundary="deadman", python_exe=r"C:\Python\python.exe"
    )
    with pytest.raises(sup.ArtifactValidationError):
        sup.validate_artifact_bundle(
            store, boundary="task", python_exe=r"C:\Python\python.exe"
        )
    with pytest.raises(sup.ArtifactValidationError):
        sup.validate_artifact_bundle(
            store, boundary="full", python_exe=r"C:\Python\python.exe"
        )


def test_force_refresh_preserves_config_and_runtime_state_bytes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    sup.init(store, python_exe=r"C:\PythonA\python.exe")
    config = store.dir / "supervisor.json"
    state = store.dir / "supervisor-state.json"
    config_bytes = b'\xef\xbb\xbf{"operator":"configured","agents":{}}\r\n'
    state_bytes = b'{"runtime":"untouched"}\n'
    config.write_bytes(config_bytes)
    state.write_bytes(state_bytes)

    sup.init(store, force=True, python_exe=r"C:\PythonB\python.exe")

    assert config.read_bytes() == config_bytes
    assert state.read_bytes() == state_bytes
    sup.validate_artifact_bundle(store, python_exe=r"C:\PythonB\python.exe")


@pytest.mark.parametrize("relative", sup.ARTIFACT_RELATIVE_PATHS)
@pytest.mark.parametrize("phase", ["before", "after"])
def test_partial_replace_is_detected_cleaned_and_rerunnable(
    tmp_path: Path, relative: str, phase: str,
) -> None:
    store = _store(tmp_path)
    sup.init(store, python_exe=r"C:\PythonA\python.exe")

    def fail_at(actual_phase: str, actual_relative: str) -> None:
        if (actual_phase, actual_relative) == (phase, relative):
            raise OSError("injected replacement failure")

    with pytest.raises(OSError, match="injected"):
        sup.refresh_artifacts(
            store,
            python_exe=r"C:\PythonB\python.exe",
            replace_hook=fail_at,
        )
    assert not list(store.dir.rglob("*.tmp"))
    inspected = sup.inspect_artifact_bundle(
        store, python_exe=r"C:\PythonB\python.exe"
    )
    # An exception after the final replacement leaves a complete current set;
    # every genuinely partial failure point is detected as stale/mixed.
    if not (phase == "after" and relative == sup.ARTIFACT_RELATIVE_PATHS[-1]):
        assert not inspected["ok"]

    sup.refresh_artifacts(store, python_exe=r"C:\PythonB\python.exe")
    sup.validate_artifact_bundle(store, python_exe=r"C:\PythonB\python.exe")


def test_live_instance_refuses_refresh_with_zero_replacements(tmp_path: Path) -> None:
    store = _store(tmp_path)
    sup.init(store, python_exe=r"C:\PythonA\python.exe")
    before = {
        relative: (store.dir / Path(relative)).read_bytes()
        for relative in sup.ARTIFACT_RELATIVE_PATHS
    }
    start = _process_start_token(os.getpid())
    assert start is not None
    assert store.claim_supervisor_instance(pid=os.getpid(), pid_start=start)

    with pytest.raises(supervisor_lifecycle.SupervisorLifecycleError, match="live or unqueryable"):
        sup.refresh_artifacts(store, python_exe=r"C:\PythonB\python.exe")
    assert before == {
        relative: (store.dir / Path(relative)).read_bytes()
        for relative in sup.ARTIFACT_RELATIVE_PATHS
    }


def test_invalid_marker_requires_explicit_repair_before_refresh(tmp_path: Path) -> None:
    store = _store(tmp_path)
    sup.init(store, python_exe=r"C:\PythonA\python.exe")
    marker = store.supervisor_instance_path()
    marker.write_text("{broken", encoding="utf-8")
    with pytest.raises(supervisor_lifecycle.SupervisorLifecycleError, match="repair-instance-marker"):
        sup.refresh_artifacts(store, python_exe=r"C:\PythonB\python.exe")

    quarantined = supervisor_lifecycle.repair_invalid_instance_marker(store)
    assert quarantined is not None and quarantined.exists()
    assert not marker.exists()
    sup.refresh_artifacts(store, python_exe=r"C:\PythonB\python.exe")
