from __future__ import annotations

import json
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


def _mark_as_source_checkout(store: Store) -> None:
    package = store.root / "src" / "agenttalk"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")


def _unset_control_plane_contract(bundle: dict[str, bytes]) -> dict[str, object]:
    """Project the behavior carried by the pre-#93 generated artifacts."""
    script = bundle["supervisor.ps1"].decode("utf-8")
    shim_lines = bundle["bin/agenttalk.cmd"].decode("utf-8").splitlines()
    source_preflight = (
        "if ($SrcOnPyPath) { $env:PYTHONPATH = "
        "(Join-Path $Root 'src') + ';' + $env:PYTHONPATH }"
    )
    launch_base = (
        "$applied = @{ AGENTTALK_ROOT = $Root; "
        "AGENTTALK_PY = $AgenttalkPython }"
    )
    source_launch = (
        "if ($SrcOnPyPath) { $applied['PYTHONPATH'] = "
        "(Join-Path $Root 'src') + ';' + $env:PYTHONPATH }"
    )
    return {
        "agenttalk_python": next(
            line for line in script.splitlines()
            if line.startswith("$AgenttalkPython = ")
        ),
        "src_on_pythonpath": next(
            line for line in script.splitlines()
            if line.startswith("$SrcOnPyPath = ")
        ),
        "source_preflight_count": script.count(source_preflight),
        "launch_base_count": script.count(launch_base),
        "source_launch_count": script.count(source_launch),
        # Marker generation and checkout identity are deliberately dynamic.
        "shim_body": shim_lines[2:],
    }


def test_tool_runtime_config_pins_control_plane_off_checkout(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _mark_as_source_checkout(store)
    fallback = r"C:\Fallback Python\python.exe"
    tool_python = tmp_path / "tool runtime" / "Scripts" / "python.exe"
    tool_python.parent.mkdir(parents=True)
    tool_python.touch()
    (store.dir / "supervisor.json").write_text(
        json.dumps({"tool_runtime_python": str(tool_python)}),
        encoding="utf-8",
    )

    bundle = sup.render_artifact_bundle(store, python_exe=fallback)

    script = bundle["supervisor.ps1"].decode("utf-8")
    shim = bundle["bin/agenttalk.cmd"].decode("utf-8")
    escaped_tool_python = str(tool_python).replace("'", "''")
    assert f"$AgenttalkPython = '{escaped_tool_python}'" in script
    assert "$SrcOnPyPath = $false" in script
    assert f'AGENTTALK_PYTHON={tool_python}"' in shim
    assert 'set "PYTHONPATH=%~dp0..\\..\\src;%PYTHONPATH%"' not in shim
    for name in (
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONUSERBASE",
        "PYTHONPLATLIBDIR",
        "PYTHONCASEOK",
    ):
        assert f'set "{name}="' in shim
        assert f"'{name}'" in script
    for name in ("PYTHONNOUSERSITE", "PYTHONSAFEPATH"):
        assert f'set "{name}=1"' in shim
        assert f"'{name}' = '1'" in script
    assert script.count("    Add-ToolRuntimePythonEnvironment $applied") == 2
    assert script.count(
        "$savedToolRuntimeEnv = Enter-ToolRuntimePythonEnvironment"
    ) == 3


def test_tool_runtime_config_unset_matches_d5add08_control_plane_contract(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _mark_as_source_checkout(store)
    fallback = r"C:\Fallback Python\python.exe"
    bundle = sup.render_artifact_bundle(store, python_exe=fallback)
    fixture = json.loads(
        (
            Path(__file__).parent
            / "fixtures"
            / "tool-runtime-unset-d5add08.json"
        ).read_text(encoding="utf-8")
    )

    assert fixture["source_commit"] == (
        "d5add08f76f59d4aa919a1e88ee656d81d940163"
    )
    assert _unset_control_plane_contract(bundle) == fixture["contract"]
    script = bundle["supervisor.ps1"].decode("utf-8")
    assert "$ToolRuntimePinned = $false" in script


def test_tool_runtime_config_absent_and_explicit_null_are_equivalent(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _mark_as_source_checkout(store)
    fallback = r"C:\Fallback Python\python.exe"
    absent = sup.render_artifact_bundle(store, python_exe=fallback)
    (store.dir / "supervisor.json").write_text(
        json.dumps({"tool_runtime_python": None}),
        encoding="utf-8",
    )
    explicit_null = sup.render_artifact_bundle(store, python_exe=fallback)

    assert explicit_null == absent


def test_tool_runtime_generated_bundle_passes_artifact_validation(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _mark_as_source_checkout(store)
    fallback = r"C:\Fallback Python\python.exe"
    tool_python = tmp_path / "tool-runtime" / "Scripts" / "python.exe"
    tool_python.parent.mkdir(parents=True)
    tool_python.touch()
    (store.dir / "supervisor.json").write_text(
        json.dumps({"tool_runtime_python": str(tool_python)}),
        encoding="utf-8",
    )

    sup.refresh_artifacts(store, python_exe=fallback)

    result = sup.validate_artifact_bundle(store)
    assert result["ok"] is True
    shim = (store.dir / "bin" / "agenttalk.cmd").read_text(encoding="utf-8")
    assert f'AGENTTALK_PYTHON={tool_python}"' in shim


@pytest.mark.parametrize(
    "replacement",
    ["different-runtime", None],
    ids=["runtime-changed", "runtime-removed"],
)
def test_tool_runtime_config_drift_requires_artifact_refresh(
    tmp_path: Path,
    replacement: str | None,
) -> None:
    store = _store(tmp_path)
    _mark_as_source_checkout(store)
    fallback = r"C:\Fallback Python\python.exe"
    first = tmp_path / "runtime-a" / "python.exe"
    second = tmp_path / "runtime-b" / "python.exe"
    first.parent.mkdir()
    second.parent.mkdir()
    first.touch()
    second.touch()
    config_path = store.dir / "supervisor.json"
    config_path.write_text(
        json.dumps({"tool_runtime_python": str(first)}),
        encoding="utf-8",
    )
    sup.refresh_artifacts(store, python_exe=fallback)
    changed = (
        {"tool_runtime_python": str(second)}
        if replacement is not None else {}
    )
    config_path.write_text(json.dumps(changed), encoding="utf-8")

    with pytest.raises(
        sup.ArtifactValidationError,
        match="artifact generator.*tool runtime.*refresh-scripts",
    ):
        sup.validate_artifact_bundle(store)

    sup.refresh_artifacts(store, python_exe=fallback)
    assert sup.validate_artifact_bundle(store)["ok"] is True


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "python.exe",
        " C:\\Python\\python.exe",
        "C:\\Python%PATH%\\python.exe",
        7,
        [],
    ],
)
def test_tool_runtime_config_rejects_invalid_values(
    tmp_path: Path,
    value: object,
) -> None:
    store = _store(tmp_path)
    (store.dir / "supervisor.json").write_text(
        json.dumps({"tool_runtime_python": value}),
        encoding="utf-8",
    )

    with pytest.raises(sup.ArtifactValidationError, match="tool_runtime_python"):
        sup.render_artifact_bundle(store)


def test_tool_runtime_config_rejects_missing_absolute_file(tmp_path: Path) -> None:
    store = _store(tmp_path)
    missing = tmp_path / "missing-runtime" / "python.exe"
    (store.dir / "supervisor.json").write_text(
        json.dumps({"tool_runtime_python": str(missing)}),
        encoding="utf-8",
    )

    with pytest.raises(
        sup.ArtifactValidationError,
        match="must name an existing file",
    ):
        sup.render_artifact_bundle(store)


@pytest.mark.parametrize(
    "value",
    [
        r"\\server\share\python.exe",
        r"\\?\UNC\server\share\python.exe",
        r"\\?\C:\Python\python.exe",
    ],
)
def test_tool_runtime_config_rejects_unc_and_device_paths(
    tmp_path: Path,
    value: str,
) -> None:
    store = _store(tmp_path)
    (store.dir / "supervisor.json").write_text(
        json.dumps({"tool_runtime_python": value}),
        encoding="utf-8",
    )

    with pytest.raises(
        sup.ArtifactValidationError,
        match="local.*UNC|UNC.*unsupported",
    ):
        sup.render_artifact_bundle(store)


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


@pytest.mark.parametrize(
    "unknown_start",
    [False, True],
    ids=["observed-start", "unknown-start"],
)
def test_live_instance_refuses_refresh_with_zero_replacements(
    tmp_path: Path,
    unknown_start: bool,
) -> None:
    store = _store(tmp_path)
    sup.init(store, python_exe=r"C:\PythonA\python.exe")
    before = {
        relative: (store.dir / Path(relative)).read_bytes()
        for relative in sup.ARTIFACT_RELATIVE_PATHS
    }
    start = None if unknown_start else _process_start_token(os.getpid())
    claim = store.claim_supervisor_instance(pid=os.getpid(), pid_start=start)
    assert claim is not None
    assert claim["pid_start"] == start

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
