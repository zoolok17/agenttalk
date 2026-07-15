from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agenttalk import powershell_host as psh
from agenttalk import supervisor as sup
from agenttalk import supervisor_lifecycle as lifecycle
from agenttalk.store import Store


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell integration")


def _store(tmp_path: Path, pwsh: str) -> Store:
    store = Store(tmp_path)
    store.init(["lead", "worker"])
    sup.init(store, python_exe=sys.executable)
    lifecycle.select_powershell_host(store, explicit_path=pwsh)
    return store


def _environment() -> dict[str, str]:
    env = os.environ.copy()
    source = str(Path(__file__).resolve().parents[1] / "src")
    env["PYTHONPATH"] = source + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["AGENTTALK_PYTHON"] = sys.executable
    env["POWERSHELL_UPDATECHECK"] = "Off"
    return env


def _quote(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _process_is_active(pid: int) -> bool:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
    finally:
        kernel32.CloseHandle(handle)


def test_real_core_parses_and_runs_all_generated_harmless_paths(tmp_path: Path) -> None:
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell Core is not installed")
    store = _store(tmp_path, pwsh)
    env = _environment()
    scripts = [
        (store.dir / "supervisor-task.ps1", ["-Action", "status"]),
        (store.dir / "deadman.ps1", ["-ThresholdSeconds", "999999", "-Json"]),
        # Non-dry-run proves the generated shim -> cmd.exe -> Python claim chain,
        # including the retained AGENTTALK_PYTHON override contract.
        (store.dir / "supervisor.ps1", ["-Once", "-Quiet"]),
    ]
    for script, arguments in scripts:
        result = subprocess.run(  # noqa: S603  # nosec B603
            [pwsh, "-NoLogo", "-NoProfile", "-NonInteractive", "-File",
             str(script), *arguments],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=tmp_path,
            env=env,
        )
        assert result.returncode == 0, f"{script.name}: {result.stdout}{result.stderr}"
    assert not store.supervisor_instance_path().exists()


def test_real_core_accepts_direct_powershell_to_python_claim_chain(tmp_path: Path) -> None:
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell Core is not installed")
    store = _store(tmp_path, pwsh)
    harness = tmp_path / "direct-claim.ps1"
    harness.write_text(
        "\n".join([
            "$ErrorActionPreference = 'Stop'",
            "$start = ([datetimeoffset](Get-Process -Id $PID).StartTime).ToString('o')",
            f"$out = & {_quote(sys.executable)} -m agenttalk --root {_quote(tmp_path)} "
            "supervise --claim-instance --pid $PID --pid-start $start",
            "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }",
            "$claim = $out | ConvertFrom-Json",
            f"& {_quote(sys.executable)} -m agenttalk --root {_quote(tmp_path)} "
            "supervise --release-instance --instance-token $claim.token "
            "--pid $PID --pid-start $start | Out-Null",
            "exit $LASTEXITCODE",
        ]) + "\n",
        encoding="utf-8",
    )
    result = subprocess.run(  # noqa: S603  # nosec B603
        [pwsh, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(harness)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=tmp_path,
        env=_environment(),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not store.supervisor_instance_path().exists()


def test_windows_powershell_51_rejects_each_script_before_sentinel(tmp_path: Path) -> None:
    pwsh = shutil.which("pwsh")
    desktop = Path(os.environ.get("SystemRoot", r"C:\Windows")) / (
        r"System32\WindowsPowerShell\v1.0\powershell.exe"
    )
    if not pwsh or not desktop.is_file():
        pytest.skip("both PowerShell Core and Windows PowerShell 5.1 are required")
    store = _store(tmp_path, pwsh)
    cases = [
        (store.dir / "supervisor-task.ps1", "-Action status"),
        (store.dir / "deadman.ps1", "-ThresholdSeconds 999999 -Json"),
        (store.dir / "supervisor.ps1", "-Once -Quiet"),
    ]
    for index, (script, arguments) in enumerate(cases):
        direct = subprocess.run(  # noqa: S603  # nosec B603
            [str(desktop), "-NoLogo", "-NoProfile", "-NonInteractive", "-File",
             str(script), *arguments.split()],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=tmp_path,
            env=_environment(),
        )
        assert direct.returncode != 0
        assert "#requires" in (direct.stdout + direct.stderr).casefold()

        sentinel = tmp_path / f"desktop-sentinel-{index}.txt"
        wrapper = tmp_path / f"desktop-wrapper-{index}.ps1"
        wrapper.write_text(
            "\n".join([
                "$ErrorActionPreference = 'Stop'",
                f". {_quote(script)} {arguments}",
                f"[IO.File]::WriteAllText({_quote(sentinel)}, 'body-ran')",
            ]) + "\n",
            encoding="utf-8",
        )
        subprocess.run(  # noqa: S603  # nosec B603
            [str(desktop), "-NoLogo", "-NoProfile", "-NonInteractive", "-File",
             str(wrapper)],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=tmp_path,
            env=_environment(),
            check=False,
        )
        assert not sentinel.exists(), f"{script.name} body ran under Windows PowerShell 5.1"
    assert not store.supervisor_instance_path().exists()


def test_real_selected_host_record_is_discrete_core_version(tmp_path: Path) -> None:
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell Core is not installed")
    store = _store(tmp_path, pwsh)
    record = json.loads(lifecycle.selection_path(store).read_text(encoding="utf-8"))
    assert record["edition"] == "Core"
    assert record["version"]["major"] >= 7
    assert set(record["version"]) == {"major", "minor", "patch", "pre_release"}


def test_real_probe_timeout_job_reaps_descendant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell Core is not installed")
    child_pid_file = tmp_path / "probe-child.pid"
    command = ";".join([
        "$psi=[Diagnostics.ProcessStartInfo]::new()",
        "$psi.FileName=(Join-Path $PSHOME 'pwsh.exe')",
        "$psi.UseShellExecute=$false",
        "$psi.ArgumentList.Add('-NoLogo')",
        "$psi.ArgumentList.Add('-NoProfile')",
        "$psi.ArgumentList.Add('-NonInteractive')",
        "$psi.ArgumentList.Add('-Command')",
        "$psi.ArgumentList.Add('Start-Sleep -Seconds 60')",
        "$child=[Diagnostics.Process]::Start($psi)",
        f"[IO.File]::WriteAllText({_quote(child_pid_file)},[string]$child.Id)",
        "Start-Sleep -Seconds 60",
    ])
    monkeypatch.setattr(psh, "_PROBE_COMMAND", command)
    with pytest.raises(psh.PowerShellHostError, match="timed out"):
        psh._run_probe(pwsh, timeout=3.0)
    assert child_pid_file.is_file(), "probe did not spawn its descendant before timeout"
    child_pid = int(child_pid_file.read_text(encoding="utf-8"))
    for _ in range(50):
        if not _process_is_active(child_pid):
            break
        import time

        time.sleep(0.1)
    assert not _process_is_active(child_pid), "Job Object close left the probe child alive"
