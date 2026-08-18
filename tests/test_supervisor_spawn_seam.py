from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agenttalk import cli, lifecycle_lock, supervisor, wrapper_runtime
from agenttalk.store import Store


STUB_CLI = Path(__file__).parent / "support" / "stub_cli.py"


def _powershell_hosts() -> tuple[str | None, ...]:
    candidates = [
        shutil.which("pwsh"),
        shutil.which("powershell"),
        str(
            Path(os.environ.get("SystemRoot", r"C:\Windows"))
            / "System32"
            / "WindowsPowerShell"
            / "v1.0"
            / "powershell.exe"
        ),
    ]
    hosts: list[str] = []
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and candidate.casefold() not in {
            host.casefold() for host in hosts
        }:
            hosts.append(candidate)
    return tuple(hosts) or (None,)


def _direct_python_executable() -> str:
    # A copied Windows venv may expose a redirector whose CreateProcess PID is
    # not the interpreter PID. The seam's identity contract needs the owner.
    return getattr(sys, "_base_executable", None) or sys.executable


def _ps_literal(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _region(text: str, name: str) -> str:
    start_marker = f"# region {name}"
    end_marker = f"# endregion {name}"
    start = text.index(start_marker)
    end = text.index(end_marker, start) + len(end_marker)
    return text[start:end]


def _generated_executor(tmp_path: Path) -> tuple[Store, str]:
    store = Store(tmp_path)
    store.init(["lead", "worker"])
    store.set_operator_facing("lead")
    assert cli.main(["--root", str(tmp_path), "supervise", "--init"]) == 0
    return store, (store.dir / "supervisor.ps1").read_text(encoding="utf-8-sig")


def _runtime_env() -> dict[str, str]:
    env = os.environ.copy()
    source = str(Path(__file__).resolve().parents[1] / "src")
    inherited = env.get("PYTHONPATH")
    env["PYTHONPATH"] = source if not inherited else source + os.pathsep + inherited
    env["AGENTTALK_PYTHON"] = _direct_python_executable()
    return env


def _wait_until(predicate, *, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except (OSError, json.JSONDecodeError):
            pass
        time.sleep(0.05)
    pytest.fail("timed out waiting for subprocess evidence")


def _wait_identity_gone(pid: int, *, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if lifecycle_lock.process_identity(pid) is None:
            return True
        time.sleep(0.05)
    return lifecycle_lock.process_identity(pid) is None


def _stop_exact_process_tree(
    pid: int | None,
    identity: lifecycle_lock.ProcessIdentity | None,
) -> None:
    if not pid or identity is None:
        return
    if lifecycle_lock.process_identity(pid) != identity:
        return
    taskkill = shutil.which("taskkill") or str(
        Path(os.environ.get("SystemRoot", r"C:\Windows"))
        / "System32"
        / "taskkill.exe"
    )
    subprocess.run(
        [taskkill, "/PID", str(pid), "/T", "/F"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def test_generated_supervisor_has_one_spawn_door_for_both_populations() -> None:
    text = supervisor.PS_TEMPLATE
    seam = _region(text, "supervisor-spawn-step")
    outside = text.replace(seam, "", 1)
    regular = text[
        text.index("function Launch($name") : text.index("function Launch-Spec")
    ]
    ephemeral = text[
        text.index("function Launch-Spec") : text.index("# Console action log")
    ]

    assert seam.count("Start-WrapperProcess $startArgs") == 1
    assert "Start-WrapperProcess $startArgs" not in outside
    assert regular.count("Invoke-SupervisorSpawnStep $startArgs") == 1
    assert ephemeral.count("Invoke-SupervisorSpawnStep $startArgs") == 1


def test_exact_launcher_samples_filetime_from_original_handle_before_close() -> None:
    text = supervisor.PS_TEMPLATE
    launcher = _region(text, "wrapper-log-helpers")
    created = launcher.index("processCreated = true;")
    observed = launcher.index("GetProcessTimes(processInfo.hProcess", created)
    resumed = launcher.index("ResumeThread(processInfo.hThread)", observed)
    closed = launcher.index("CloseHandle(processInfo.hProcess)", resumed)

    assert created < observed < resumed < closed
    assert "CreationFiletime" in launcher
    assert 'scheme = "win32-filetime-v1"' not in launcher


@pytest.mark.parametrize(
    "shell",
    _powershell_hosts(),
    ids=lambda value: Path(value).stem if value else "unavailable",
)
def test_spawn_seam_refuses_invalid_precreate_input_with_closed_result(
    tmp_path: Path,
    shell: str | None,
) -> None:
    if shell is None:
        pytest.skip("Windows PowerShell hosts are unavailable")
    _, text = _generated_executor(tmp_path)
    output = tmp_path / "spawn-refused.json"
    harness = tmp_path / "spawn-refused.ps1"
    harness.write_text(
        "\n".join(
            [
                "$ErrorActionPreference = 'Stop'",
                "$WrapperLogEnvKeys = @()",
                _region(text, "exec-helpers"),
                _region(text, "wrapper-log-helpers"),
                _region(text, "supervisor-spawn-step"),
                "$result = Invoke-SupervisorSpawnStep @{} 'test-invalid'",
                "$result | Select-Object schema,schema_version,outcome,"
                "reason_code,remedy,cleanup_status,pid,process_identity,"
                "redirected | ConvertTo-Json -Depth 5 | "
                f"Set-Content {_ps_literal(output)} -Encoding utf8",
            ]
        ),
        encoding="utf-8-sig",
    )

    result = subprocess.run(
        [shell, "-NoProfile", "-File", str(harness)],
        capture_output=True,
        text=True,
        timeout=120,
        env=_runtime_env(),
    )

    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    payload = json.loads(output.read_text(encoding="utf-8-sig"))
    assert payload == {
        "schema": "agenttalk-supervisor-spawn-step",
        "schema_version": 1,
        "outcome": "refused",
        "reason_code": "spawn_args_invalid",
        "remedy": "regenerate the supervisor script and retry the launch",
        "cleanup_status": "not_started",
        "pid": None,
        "process_identity": None,
        "redirected": False,
    }


@pytest.mark.parametrize(
    "shell",
    _powershell_hosts(),
    ids=lambda value: Path(value).stem if value else "unavailable",
)
@pytest.mark.parametrize(
    ("failure_mode", "reason_code", "compatibility_disposition"),
    [
        ("no-pid", "spawn_pid_unavailable", "no_pid"),
        ("exception", "spawn_step_exception", "rethrow"),
    ],
)
def test_spawn_seam_reports_unknown_without_a_null_result(
    tmp_path: Path,
    shell: str | None,
    failure_mode: str,
    reason_code: str,
    compatibility_disposition: str,
) -> None:
    if shell is None:
        pytest.skip("Windows PowerShell hosts are unavailable")
    output = tmp_path / f"spawn-unknown-{failure_mode}.json"
    harness = tmp_path / f"spawn-unknown-{failure_mode}.ps1"
    fake = (
        "function Start-WrapperProcess([hashtable]$startArgs) { "
        "throw 'injected launch-boundary failure' }"
        if failure_mode == "exception"
        else "function Start-WrapperProcess([hashtable]$startArgs) { "
        "return [pscustomobject]@{ Process = $null; "
        "CreationFiletime = $null; Redirected = $false } }"
    )
    harness.write_text(
        "\n".join(
            [
                "$ErrorActionPreference = 'Stop'",
                fake,
                _region(supervisor.PS_TEMPLATE, "supervisor-spawn-step"),
                "$result = Invoke-SupervisorSpawnStep "
                "@{ FilePath = 'candidate.exe' } 'test-unknown'",
                "$result | Select-Object schema,schema_version,outcome,"
                "reason_code,remedy,cleanup_status,pid,process_identity,"
                "redirected,_compatibility_disposition | ConvertTo-Json "
                f"-Depth 5 | Set-Content {_ps_literal(output)} -Encoding utf8",
            ]
        ),
        encoding="utf-8-sig",
    )

    result = subprocess.run(
        [shell, "-NoProfile", "-File", str(harness)],
        capture_output=True,
        text=True,
        timeout=30,
        env=_runtime_env(),
    )

    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    payload = json.loads(output.read_text(encoding="utf-8-sig"))
    assert payload["schema"] == "agenttalk-supervisor-spawn-step"
    assert payload["schema_version"] == 1
    assert payload["outcome"] == "unknown"
    assert payload["reason_code"] == reason_code
    assert payload["remedy"]
    assert payload["cleanup_status"] == "unknown"
    assert payload["pid"] is None
    assert payload["process_identity"] is None
    assert payload["_compatibility_disposition"] == compatibility_disposition


@pytest.mark.subprocess
@pytest.mark.parametrize(
    "shell",
    _powershell_hosts(),
    ids=lambda value: Path(value).stem if value else "unavailable",
)
def test_spawn_seam_start_process_path_records_exact_identity(
    tmp_path: Path,
    shell: str | None,
) -> None:
    if shell is None:
        pytest.skip("Windows PowerShell hosts are unavailable")
    _, text = _generated_executor(tmp_path)
    direct_python = _direct_python_executable()
    child_record = tmp_path / "start-process-child.json"
    result_path = tmp_path / "start-process-result.json"
    child = tmp_path / "start-process-child.py"
    child.write_text(
        "\n".join(
            [
                "import json, os, sys, time",
                "from pathlib import Path",
                "from agenttalk import lifecycle_lock",
                "identity = lifecycle_lock.process_identity(os.getpid())",
                "assert identity is not None",
                "Path(sys.argv[1]).write_text(json.dumps({",
                "    'pid': os.getpid(),",
                "    'process_identity': {",
                "        'scheme': identity.scheme, 'value': identity.value},",
                "}), encoding='utf-8')",
                "time.sleep(0.5)",
                "",
            ]
        ),
        encoding="utf-8",
    )
    argument_line = subprocess.list2cmdline(
        ["-X", "utf8", str(child), str(child_record)]
    )
    harness = tmp_path / "start-process-identity.ps1"
    harness.write_text(
        "\n".join(
            [
                "$ErrorActionPreference = 'Stop'",
                "$WrapperLogEnvKeys = @()",
                _region(text, "exec-helpers"),
                _region(text, "wrapper-log-helpers"),
                _region(text, "supervisor-spawn-step"),
                "$startArgs = @{",
                f"  FilePath = {_ps_literal(direct_python)}",
                f"  ArgumentList = {_ps_literal(argument_line)}",
                f"  WorkingDirectory = {_ps_literal(tmp_path)}",
                "  WindowStyle = 'Hidden'",
                "  PassThru = $true",
                "}",
                "$result = Invoke-SupervisorSpawnStep "
                "$startArgs 'test-start-process'",
                "$result | Select-Object schema,schema_version,outcome,"
                "reason_code,remedy,cleanup_status,pid,process_identity,"
                "redirected | ConvertTo-Json -Depth 5 | "
                f"Set-Content {_ps_literal(result_path)} -Encoding utf8",
            ]
        ),
        encoding="utf-8-sig",
    )

    result = subprocess.run(
        [shell, "-NoProfile", "-File", str(harness)],
        capture_output=True,
        text=True,
        timeout=30,
        env=_runtime_env(),
        cwd=str(tmp_path),
    )

    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    _wait_until(child_record.exists, timeout=5)
    child_payload = json.loads(child_record.read_text(encoding="utf-8"))
    payload = json.loads(result_path.read_text(encoding="utf-8-sig"))
    assert payload["outcome"] == "spawned", payload
    assert payload["pid"] == child_payload["pid"]
    assert payload["process_identity"] == child_payload["process_identity"]
    assert payload["redirected"] is False
    assert _wait_identity_gone(int(payload["pid"]), timeout=5)


@pytest.mark.subprocess
@pytest.mark.parametrize(
    "shell",
    _powershell_hosts(),
    ids=lambda value: Path(value).stem if value else "unavailable",
)
def test_spawn_seam_real_wrapper_reaches_readiness_with_one_exact_identity(
    tmp_path: Path,
    shell: str | None,
) -> None:
    if shell is None:
        pytest.skip("Windows PowerShell hosts are unavailable")
    store, text = _generated_executor(tmp_path)
    direct_python = _direct_python_executable()
    journal = tmp_path / "spawn-journal.txt"
    result_path = tmp_path / "spawn-result.json"
    bootstrap = tmp_path / "wrapper-bootstrap.py"
    bootstrap.write_text(
        "\n".join(
            [
                "import os",
                "from pathlib import Path",
                "from agenttalk.cli import console_main",
                "with Path(os.environ['AGENTTALK_TEST_SPAWN_JOURNAL']).open('a', encoding='utf-8') as stream:",
                "    stream.write(f'{os.getpid()}\\n')",
                "raise SystemExit(console_main())",
                "",
            ]
        ),
        encoding="utf-8",
    )
    wrapper_argv = [
        "-X",
        "utf8",
        str(bootstrap),
        "--root",
        str(tmp_path),
        "wrap",
        "--for",
        "worker",
        "--cli",
        "claude",
        "--loop",
        "--",
        direct_python,
        str(STUB_CLI),
    ]
    stdout_path = tmp_path / "wrapper-stdout.log"
    stderr_path = tmp_path / "wrapper-stderr.log"
    harness = tmp_path / "spawn-real-wrapper.ps1"
    harness.write_text(
        "\n".join(
            [
                "$ErrorActionPreference = 'Stop'",
                "$WrapperLogEnvKeys = @(",
                "  'AGENTTALK_WRAPPER_STDOUT_LOG',",
                "  'AGENTTALK_WRAPPER_STDERR_LOG',",
                "  'AGENTTALK_WRAPPER_LOG_MAX_BYTES',",
                "  'AGENTTALK_WRAPPER_LOG_SEGMENTS',",
                "  'AGENTTALK_WRAPPER_LOG_NONCE')",
                _region(text, "exec-helpers"),
                _region(text, "wrapper-log-helpers"),
                _region(text, "supervisor-spawn-step"),
                "$startArgs = @{",
                f"  FilePath = {_ps_literal(direct_python)}",
                f"  ArgumentList = {_ps_literal(subprocess.list2cmdline(wrapper_argv))}",
                f"  WorkingDirectory = {_ps_literal(tmp_path)}",
                "  WindowStyle = 'Hidden'",
                "  PassThru = $true",
                f"  RedirectStandardOutput = {_ps_literal(stdout_path)}",
                f"  RedirectStandardError = {_ps_literal(stderr_path)}",
                "}",
                "$result = Invoke-SupervisorSpawnStep $startArgs 'test-real-wrapper'",
                "$result | Select-Object schema,schema_version,outcome,"
                "reason_code,remedy,cleanup_status,pid,process_identity,"
                "redirected | ConvertTo-Json -Depth 5 | "
                f"Set-Content {_ps_literal(result_path)} -Encoding utf8",
            ]
        ),
        encoding="utf-8-sig",
    )

    guard_release = tmp_path / "guard.release"
    guard_ready = tmp_path / "guard.ready"
    guard_code = (
        "import os,time; from pathlib import Path; "
        f"Path({str(guard_ready)!r}).write_text(str(os.getpid()), encoding='utf-8'); "
        f"release=Path({str(guard_release)!r}); "
        "\nwhile not release.exists(): time.sleep(0.02)"
    )
    env = _runtime_env()
    env["AGENTTALK_TEST_SPAWN_JOURNAL"] = str(journal)
    guard = subprocess.Popen(
        [direct_python, "-X", "utf8", "-c", guard_code],
        env=env,
        cwd=str(tmp_path),
    )
    target_pid: int | None = None
    target_identity: lifecycle_lock.ProcessIdentity | None = None
    guard_identity: lifecycle_lock.ProcessIdentity | None = None
    try:
        _wait_until(guard_ready.exists)
        guard_identity = lifecycle_lock.process_identity(guard.pid)
        assert guard_identity is not None

        launched = subprocess.run(
            [shell, "-NoProfile", "-File", str(harness)],
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
            cwd=str(tmp_path),
        )
        assert launched.returncode == 0, f"{launched.stdout}{launched.stderr}"
        payload = json.loads(result_path.read_text(encoding="utf-8-sig"))
        assert payload["outcome"] == "spawned", payload
        assert payload["reason_code"] is None
        assert payload["remedy"] is None
        assert payload["cleanup_status"] == "not_needed"
        target_pid = int(payload["pid"])
        target_identity = lifecycle_lock.process_identity(target_pid)
        assert target_identity is not None
        assert payload["process_identity"] == {
            "scheme": target_identity.scheme,
            "value": target_identity.value,
        }

        def _ready() -> bool:
            waiting = store.read_waiting("worker")
            runtime = wrapper_runtime.read_runtime(store.state_dir, "worker")
            return bool(
                waiting
                and waiting.get("pid") == target_pid
                and store.read_heartbeat("worker") is not None
                and runtime.get("status") == wrapper_runtime.STATUS_VALID
                and runtime["record"].get("phase") == wrapper_runtime.PHASE_IDLE
                and runtime["record"].get("wrapper_pid") == target_pid
            )

        _wait_until(_ready)
        rows = [line for line in journal.read_text(encoding="utf-8").splitlines() if line]
        assert rows == [str(target_pid)]
        assert lifecycle_lock.process_identity(guard.pid) == guard_identity

        store.send(
            sender="lead",
            recipient="worker",
            kind="release",
            body="increment-1 spawn-seam test complete",
            meta={
                "release_authority": "human",
                "operator_decision": "true",
                "authority_reason": "targeted spawn-seam cleanup",
            },
        )
        assert _wait_identity_gone(target_pid)
        assert lifecycle_lock.process_identity(guard.pid) == guard_identity
    finally:
        _stop_exact_process_tree(target_pid, target_identity)
        guard_release.write_text("release", encoding="utf-8")
        try:
            guard.wait(timeout=10)
        except subprocess.TimeoutExpired:
            if lifecycle_lock.process_identity(guard.pid) == guard_identity:
                guard.terminate()
            guard.wait(timeout=10)
