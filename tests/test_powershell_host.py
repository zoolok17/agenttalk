from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from agenttalk import powershell_host as psh


PWSH = (
    r"C:\Program Files\PowerShell\7\pwsh.exe"
    if sys.platform == "win32"
    else "/opt/microsoft/powershell/7/pwsh.exe"
)
IDENTITY_SCHEME = "win32-file-id-v1" if sys.platform == "win32" else "stat-v1"
WINDOWS_ONLY = pytest.mark.skipif(
    sys.platform != "win32",
    reason="PowerShell host is Windows-only",
)


def _identity(path: str = PWSH, *, file_id: str = "01"):
    return psh.NativeFileIdentity(
        scheme=IDENTITY_SCHEME,
        final_path=path,
        volume_serial="aabbccdd",
        file_id=file_id,
        size=123,
        last_write=456,
    )


def _result(
    version: psh.PowerShellVersion | None = None,
    *,
    path: str = PWSH,
    source: str = "program_files",
    edition: str = "Core",
    file_id: str = "01",
) -> psh.ProbeResult:
    version = version or psh.PowerShellVersion(7, 6, 3)
    return psh.ProbeResult(path, source, edition, version, _identity(path, file_id=file_id))


@pytest.mark.parametrize(
    ("edition", "version", "accepted", "warning"),
    [
        ("Core", psh.PowerShellVersion(7, 0, 0), True, "end-of-life"),
        ("Core", psh.PowerShellVersion(7, 3, 9), True, "end-of-life"),
        ("Core", psh.PowerShellVersion(7, 4, 0), True, None),
        ("Core", psh.PowerShellVersion(7, 3, 0, "rc.1"), True, "prerelease"),
        ("Core", psh.PowerShellVersion(7, 4, 0, "preview.1"), True, "prerelease"),
        ("Core", psh.PowerShellVersion(8, 0, 0, "rc.1"), True, "prerelease"),
        ("Core", psh.PowerShellVersion(8, 0, 0), True, None),
        ("Core", psh.PowerShellVersion(6, 2, 0), False, None),
        ("Desktop", psh.PowerShellVersion(5, 1, 0), False, None),
    ],
)
def test_policy_table(edition, version, accepted, warning) -> None:
    assert psh.hard_gate_accepts(edition, version) is accepted
    text = psh.host_warning(edition, version)
    assert (warning is None and text is None) or warning in text


def test_generated_guard_is_core_seven_with_param_left_to_renderer() -> None:
    assert psh.generated_preamble() == (
        "#requires -Version 7\n#requires -PSEdition Core\n"
    )
    guard = psh.generated_runtime_guard()
    assert "$PSEdition -ne 'Core'" in guard
    assert "$AgenttalkVersion.Major -lt 7" in guard
    assert "7.4+ stable is recommended" in guard


def test_probe_argv_is_profile_free_and_structured() -> None:
    argv = psh.probe_argv(r"C:\PowerShell\pwsh.exe")
    assert argv[:5] == [
        r"C:\PowerShell\pwsh.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command"
    ]
    assert "PreReleaseLabel" in argv[-1]
    assert "ConvertTo-Json -Compress" in argv[-1]
    assert psh.PROBE_SENTINEL in argv[-1]


def test_probe_parses_only_one_sentinel_and_checks_identity() -> None:
    ident = _identity()
    calls = []

    def identity_reader(path):
        calls.append(str(path))
        return ident

    payload = {
        "sentinel": "agenttalk-pwsh-probe-v1",
        "edition": "Core",
        "major": 7,
        "minor": 10,
        "patch": 2,
        "pre_release": "rc.1",
    }

    def runner(path, *, timeout):
        assert path == ident.final_path
        assert timeout == psh.PROBE_TIMEOUT_SECONDS
        return subprocess.CompletedProcess(
            [], 0, "profile noise\n" + psh.PROBE_SENTINEL + json.dumps(payload) + "\n", ""
        )

    result = psh.probe_candidate(
        ident.final_path, source="explicit", runner=runner, identity_reader=identity_reader
    )
    assert result.version == psh.PowerShellVersion(7, 10, 2, "rc.1")
    assert calls == [ident.final_path, ident.final_path]


@pytest.mark.parametrize(
    "stdout",
    [
        "garbage",
        psh.PROBE_SENTINEL + "{}",
        psh.PROBE_SENTINEL + "{}\n" + psh.PROBE_SENTINEL + "{}",
    ],
)
def test_probe_rejects_missing_wrong_or_duplicate_sentinel(stdout: str) -> None:
    def runner(path, *, timeout):
        return subprocess.CompletedProcess([], 0, stdout, "")

    with pytest.raises(psh.PowerShellHostError):
        psh.probe_candidate(
            _identity().final_path,
            source="explicit",
            runner=runner,
            identity_reader=lambda path: _identity(),
        )


def test_explicit_candidate_failure_is_terminal_even_with_program_files() -> None:
    calls: list[tuple[str, str]] = []

    def probe(path, *, source):
        calls.append((path, source))
        raise psh.PowerShellHostError("wrong edition")

    resolved = psh.resolve_candidate(
        explicit_path=r"D:\portable\pwsh.exe",
        program_files_roots=(r"C:\Program Files",),
        probe=probe,
    )
    assert resolved.result is None
    assert calls == [(r"D:\portable\pwsh.exe", "explicit")]


def test_current_candidate_failure_is_terminal_even_with_program_files() -> None:
    calls: list[tuple[str, str]] = []

    def probe(path, *, source):
        calls.append((path, source))
        raise psh.PowerShellHostError("identity race")

    resolved = psh.resolve_candidate(
        current_path=r"D:\current\pwsh.exe",
        program_files_roots=(r"C:\Program Files",),
        probe=probe,
    )
    assert resolved.result is None
    assert calls == [(r"D:\current\pwsh.exe", "current_host")]


@WINDOWS_ONLY
def test_automatic_candidates_continue_in_native_order() -> None:
    calls = []

    def probe(path, *, source):
        calls.append(path)
        if len(calls) == 1:
            raise psh.PowerShellHostError("missing")
        return _result(path=path, source=source)

    resolved = psh.resolve_candidate(
        program_files_roots=(r"C:\Native", r"D:\Arm", r"D:\Arm", r"C:\x86"),
        probe=probe,
    )
    assert resolved.result is not None
    assert calls == [
        r"C:\Native\PowerShell\7\pwsh.exe",
        r"D:\Arm\PowerShell\7\pwsh.exe",
    ]


@WINDOWS_ONLY
def test_automatic_candidate_cannot_redirect_outside_program_files() -> None:
    calls: list[str] = []

    def probe(path, *, source):
        calls.append(path)
        if len(calls) == 1:
            return _result(path=r"D:\Portable\pwsh.exe", source=source)
        return _result(path=path, source=source)

    resolved = psh.resolve_candidate(
        program_files_roots=(r"C:\Native", r"D:\Program Files"),
        probe=probe,
    )
    assert resolved.result is not None
    assert resolved.result.path == r"D:\Program Files\PowerShell\7\pwsh.exe"
    assert resolved.attempts[0].accepted is False
    assert "outside" in resolved.attempts[0].reason


def test_automatic_discovery_never_trusts_environment_program_files_root(
    tmp_path: Path,
) -> None:
    injected_root = tmp_path / "OwnedRoot"
    calls: list[str] = []

    def probe(path, *, source):
        calls.append(path)
        raise psh.PowerShellHostError("missing")

    psh.resolve_candidate(
        environ={"ProgramW6432": str(injected_root)},
        program_files_roots=(),
        probe=probe,
    )

    injected_key = psh.normalized_path_key(injected_root)
    assert all(not psh.normalized_path_key(path).startswith(injected_key) for path in calls)
    assert calls == []


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("pwsh.exe", "absolute"),
        (r".\pwsh.exe", "absolute"),
    ],
)
def test_candidate_path_rejects_relative_shapes_before_identity(
    path: str,
    reason: str,
) -> None:
    with pytest.raises(psh.PowerShellHostError, match=reason):
        psh.validate_candidate_path(path)


@WINDOWS_ONLY
@pytest.mark.parametrize(
    ("path", "reason"),
    [
        (r"\\server\share\pwsh.exe", "UNC"),
        (r"C:\Tools\pwsh.cmd", ".exe"),
        (r"C:\Users\me\AppData\Local\Microsoft\WindowsApps\pwsh.exe", "WindowsApps"),
    ],
)
def test_candidate_path_rejects_windows_shapes_before_identity(
    path: str,
    reason: str,
) -> None:
    with pytest.raises(psh.PowerShellHostError, match=reason):
        psh.validate_candidate_path(path)


@WINDOWS_ONLY
def test_candidate_path_rejects_mapped_drive_before_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(psh, "_windows_drive_type", lambda path: 4)  # DRIVE_REMOTE
    with pytest.raises(psh.PowerShellHostError, match="fixed local drive"):
        psh.validate_candidate_path(r"Z:\PowerShell\pwsh.exe")


def test_selection_fingerprint_revision_and_a_to_b_to_a() -> None:
    a1 = psh.make_selection_record(_result(), project_id="project", now=1000.0)
    a_refresh = psh.make_selection_record(
        _result(), project_id="project", previous=a1, now=2000.0
    )
    b = psh.make_selection_record(
        _result(path=r"D:\PowerShell\pwsh.exe", source="explicit", file_id="02"),
        project_id="project",
        previous=a_refresh,
        now=3000.0,
    )
    a2 = psh.make_selection_record(
        _result(), project_id="project", previous=b, now=4000.0
    )
    assert a1["selection_revision"] == a_refresh["selection_revision"] == 1
    assert b["selection_revision"] == 2
    assert a2["selection_revision"] == 3
    assert a1["selection_fingerprint"] == a_refresh["selection_fingerprint"]
    assert a1["selection_fingerprint"] == a2["selection_fingerprint"]
    assert a1["probed_at"] != a_refresh["probed_at"]


def test_selection_validation_rejects_fingerprint_tamper_future_and_expiry() -> None:
    record = psh.make_selection_record(_result(), project_id="project", now=1000.0)
    valid = psh.validate_selection_record(record, project_id="project", now=1001.0)
    assert valid["_expired"] is False

    tampered = copy.deepcopy(record)
    tampered["version"]["minor"] = 5
    with pytest.raises(psh.PowerShellHostError, match="fingerprint"):
        psh.validate_selection_record(tampered, project_id="project", now=1001.0)

    with pytest.raises(psh.PowerShellHostError, match="future"):
        psh.validate_selection_record(record, project_id="project", now=0.0)

    expired = psh.validate_selection_record(
        record, project_id="project", now=1000.0 + psh.SELECTION_TTL_SECONDS + 1
    )
    assert expired["_expired"] is True
    with pytest.raises(psh.PowerShellHostError, match="expired"):
        psh.validate_selection_record(
            record,
            project_id="project",
            now=1000.0 + psh.SELECTION_TTL_SECONDS + 1,
            require_fresh=True,
        )


def test_selection_validation_rejects_wrong_project_and_schema() -> None:
    record = psh.make_selection_record(_result(), project_id="project-a", now=1000.0)
    with pytest.raises(psh.PowerShellHostError, match="different schema or project"):
        psh.validate_selection_record(record, project_id="project-b", now=1001.0)

    record["schema"] = "agenttalk.powershell-host.v999"
    record["selection_fingerprint"] = psh.compute_selection_fingerprint(record)
    with pytest.raises(psh.PowerShellHostError, match="different schema or project"):
        psh.validate_selection_record(record, project_id="project-a", now=1001.0)


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        (r"\\server\share\pwsh.exe", "UNC"),
        (r"C:\Tools\pwsh.cmd", ".exe"),
        (r"C:\Users\me\AppData\Local\Microsoft\WindowsApps\pwsh.exe", "WindowsApps"),
    ],
)
@WINDOWS_ONLY
def test_selection_validation_rejects_ineligible_serialized_paths(
    path: str,
    reason: str,
) -> None:
    record = psh.make_selection_record(
        _result(path=path, source="explicit"),
        project_id="project",
        now=1000.0,
    )
    with pytest.raises(psh.PowerShellHostError, match=reason):
        psh.validate_selection_record(record, project_id="project", now=1001.0)


@pytest.mark.parametrize("task_name", ["", "   ", 123, "x" * 257])
def test_selection_validation_rejects_invalid_task_name(task_name: object) -> None:
    record = psh.make_selection_record(_result(), project_id="project", now=1000.0)
    record["task_name"] = task_name
    record["selection_fingerprint"] = psh.compute_selection_fingerprint(record)
    with pytest.raises(psh.PowerShellHostError, match="task_name"):
        psh.validate_selection_record(record, project_id="project", now=1001.0)


def test_selection_fingerprint_excludes_probe_time_and_includes_task_name() -> None:
    record = psh.make_selection_record(_result(), project_id="project", now=1000.0)
    changed_time = copy.deepcopy(record)
    changed_time["probed_at"] = "2030-01-01T00:00:00.000000Z"
    changed_task = copy.deepcopy(record)
    changed_task["task_name"] = "custom-task"
    assert psh.compute_selection_fingerprint(record) == psh.compute_selection_fingerprint(
        changed_time
    )
    assert psh.compute_selection_fingerprint(record) != psh.compute_selection_fingerprint(
        changed_task
    )

    rebound = psh.make_selection_record(
        _result(),
        project_id="project",
        previous=record,
        task_name="custom-task",
        now=2000.0,
    )
    assert rebound["selection_revision"] == record["selection_revision"] + 1


def test_path_diagnostics_are_data_only(tmp_path: Path) -> None:
    candidate = tmp_path / "pwsh.exe"
    candidate.write_bytes(b"")
    commands = psh.path_candidate_remediations({"PATH": str(tmp_path)})
    assert commands == (
        f'agenttalk supervise --select-pwsh --pwsh "{candidate}"',
    )


def test_environment_program_files_root_is_diagnostic_only(tmp_path: Path) -> None:
    candidate = tmp_path / "PowerShell" / "7" / "pwsh.exe"
    candidate.parent.mkdir(parents=True)
    candidate.write_bytes(b"")

    commands = psh.path_candidate_remediations({"ProgramW6432": str(tmp_path)})

    assert commands == (
        f'agenttalk supervise --select-pwsh --pwsh "{candidate}"',
    )


def test_path_diagnostics_do_not_recommend_windowsapps_alias(tmp_path: Path) -> None:
    windowsapps = tmp_path / "Microsoft" / "WindowsApps"
    windowsapps.mkdir(parents=True)
    (windowsapps / "pwsh.exe").write_bytes(b"")
    assert psh.path_candidate_remediations({"PATH": str(windowsapps)}) == ()


def test_run_probe_kills_and_reaps_child_on_base_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # _run_probe already kills+reaps on an OSError from job-attach and on a
    # communicate() timeout. A termination signal (or any other
    # BaseException, e.g. SystemExit/KeyboardInterrupt) arriving while
    # communicate() is blocked skipped both of those paths entirely and left
    # the probe child running, unreaped.
    class _FakeProc:
        def __init__(self) -> None:
            self.killed = False
            self.communicate_calls = 0
            self._returncode: int | None = None
            self.args = ["pwsh"]

        def poll(self):
            return self._returncode

        def kill(self):
            self.killed = True
            self._returncode = -9

        def communicate(self, timeout=None):
            self.communicate_calls += 1
            if self.communicate_calls == 1:
                raise SystemExit(143)
            return "", ""

    created: list[_FakeProc] = []

    def _fake_popen(*args, **kwargs):
        proc = _FakeProc()
        created.append(proc)
        return proc

    monkeypatch.setattr(psh.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(psh, "_attach_kill_on_close_job", lambda proc: (None, lambda: None))

    with pytest.raises(SystemExit):
        psh._run_probe("pwsh", timeout=5.0)

    proc = created[0]
    assert proc.killed is True
    assert proc.communicate_calls == 2


def test_run_probe_closes_containment_job_before_retrying_on_base_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 10 connector finding: closing the containment job is what
    actually frees a descendant holding the probe's stdout/stderr pipe
    handles open (on Windows, via KILL_ON_JOB_CLOSE). The old code called
    close_job() only in the outer finally, after the post-kill retry had
    already been attempted - so the retry ran while the descendant was
    still alive and holding the pipes. This fake models that dependency
    directly: the retry can only drain once the job has been closed."""
    job_state = {"closed": False}

    class _FakeProc:
        def __init__(self) -> None:
            self.killed = False
            self.communicate_calls = 0
            self.retry_saw_job_closed: bool | None = None
            self._returncode: int | None = None
            self.args = ["pwsh"]

        def poll(self):
            return self._returncode

        def kill(self):
            self.killed = True
            self._returncode = -9

        def communicate(self, timeout=None):
            self.communicate_calls += 1
            if self.communicate_calls == 1:
                raise SystemExit(143)
            self.retry_saw_job_closed = job_state["closed"]
            if not job_state["closed"]:
                raise subprocess.TimeoutExpired(cmd=self.args, timeout=timeout or 0)
            return "", ""

    created: list[_FakeProc] = []

    def _fake_popen(*args, **kwargs):
        proc = _FakeProc()
        created.append(proc)
        return proc

    def _close() -> None:
        job_state["closed"] = True

    monkeypatch.setattr(psh.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(psh, "_attach_kill_on_close_job", lambda proc: (object(), _close))

    with pytest.raises(SystemExit):
        psh._run_probe("pwsh", timeout=5.0)

    proc = created[0]
    assert proc.killed is True
    assert proc.communicate_calls == 2
    assert proc.retry_saw_job_closed is True


def test_run_probe_falls_back_to_wait_when_retry_times_out_again_on_base_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 10 connector finding, sibling of _git_write's own
    reap-after-stuck-retry: if the post-kill retry itself times out again
    (e.g. on POSIX, where close_job is a no-op and a descendant can survive
    regardless), the old code silently swallowed the second
    TimeoutExpired without ever confirming the process was reaped."""
    class _FakeProc:
        def __init__(self) -> None:
            self.killed = False
            self.communicate_calls = 0
            self.wait_calls = 0
            self._returncode: int | None = None
            self.args = ["pwsh"]

        def poll(self):
            return self._returncode

        def kill(self):
            self.killed = True

        def communicate(self, timeout=None):
            self.communicate_calls += 1
            if self.communicate_calls == 1:
                raise SystemExit(143)
            raise subprocess.TimeoutExpired(cmd=self.args, timeout=timeout or 0)

        def wait(self, timeout=None):
            self.wait_calls += 1
            self._returncode = -9
            return self._returncode

    created: list[_FakeProc] = []

    def _fake_popen(*args, **kwargs):
        proc = _FakeProc()
        created.append(proc)
        return proc

    monkeypatch.setattr(psh.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(psh, "_attach_kill_on_close_job", lambda proc: (None, lambda: None))

    with pytest.raises(SystemExit):
        psh._run_probe("pwsh", timeout=5.0)

    proc = created[0]
    assert proc.killed is True
    assert proc.communicate_calls == 2
    assert proc.wait_calls == 1


def test_run_probe_bounds_the_routine_double_timeout_retry_and_reaps_via_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sibling of the same gap, found while surveying this fix's blast
    radius: the routine double-timeout branch (both the requested timeout
    and the 2s post-kill retry expire) fell back to an UNBOUNDED
    communicate() - if a descendant retained the pipe handles, this could
    block _run_probe forever even though the top-level probe was already
    killed. Bound it and reap via wait(), same pattern as the
    BaseException branch above."""
    class _FakeProc:
        def __init__(self) -> None:
            self.kill_calls = 0
            self.communicate_calls = 0
            self.wait_calls = 0
            self._returncode: int | None = None
            self.args = ["pwsh"]

        def poll(self):
            return self._returncode

        def kill(self):
            self.kill_calls += 1

        def communicate(self, timeout=None):
            self.communicate_calls += 1
            raise subprocess.TimeoutExpired(cmd=self.args, timeout=timeout or 0)

        def wait(self, timeout=None):
            self.wait_calls += 1
            self._returncode = -9
            return self._returncode

    created: list[_FakeProc] = []

    def _fake_popen(*args, **kwargs):
        proc = _FakeProc()
        created.append(proc)
        return proc

    monkeypatch.setattr(psh.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(psh, "_attach_kill_on_close_job", lambda proc: (None, lambda: None))

    with pytest.raises(psh.PowerShellHostError, match="probe timed out"):
        psh._run_probe("pwsh", timeout=5.0)

    proc = created[0]
    assert proc.communicate_calls == 3
    assert proc.wait_calls == 1


def test_run_probe_kill_raising_does_not_replace_the_owner_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 11 connector finding, sibling of the same pattern reported for
    wrapper/run.py: kill() can itself raise (PermissionError on Windows,
    ProcessLookupError if the child already exited). Unguarded, that
    secondary error would replace the SystemExit/KeyboardInterrupt being
    cleaned up after and skip the reap fallback entirely."""
    class _FakeProc:
        def __init__(self) -> None:
            self.kill_called = False
            self.communicate_calls = 0
            self._returncode: int | None = None
            self.args = ["pwsh"]

        def poll(self):
            return self._returncode

        def kill(self):
            self.kill_called = True
            raise PermissionError("simulated: process handle already closing")

        def communicate(self, timeout=None):
            self.communicate_calls += 1
            if self.communicate_calls == 1:
                raise SystemExit(143)
            return "", ""

    created: list[_FakeProc] = []

    def _fake_popen(*args, **kwargs):
        proc = _FakeProc()
        created.append(proc)
        return proc

    monkeypatch.setattr(psh.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(psh, "_attach_kill_on_close_job", lambda proc: (None, lambda: None))

    with pytest.raises(SystemExit):
        psh._run_probe("pwsh", timeout=5.0)

    proc = created[0]
    assert proc.kill_called is True


def test_run_probe_job_attach_failure_cleanup_is_bounded_when_kill_also_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 12 connector finding: suppressing kill()'s own OSError is not
    itself free. If kill() fails, proc is still running, and the unbounded
    communicate() that used to follow would then wait forever on a live
    process - strictly worse than the crash it replaced. Bound the cleanup
    and fall back to wait(), while still raising the original containment
    OSError as the cause."""
    class _FakeProc:
        def __init__(self) -> None:
            self.kill_called = False
            self.communicate_calls = 0
            self.wait_calls = 0
            self._returncode: int | None = None
            self.args = ["pwsh"]

        def poll(self):
            return self._returncode

        def kill(self):
            self.kill_called = True
            raise PermissionError("simulated: kill also failed")

        def communicate(self, timeout=None):
            self.communicate_calls += 1
            raise subprocess.TimeoutExpired(cmd=self.args, timeout=timeout or 0)

        def wait(self, timeout=None):
            self.wait_calls += 1
            self._returncode = -9
            return self._returncode

    created: list[_FakeProc] = []

    def _fake_popen(*args, **kwargs):
        proc = _FakeProc()
        created.append(proc)
        return proc

    def _raise_containment(proc):
        raise OSError("simulated: job attach failed")

    monkeypatch.setattr(psh.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(psh, "_attach_kill_on_close_job", _raise_containment)

    with pytest.raises(psh.PowerShellHostError, match="probe process containment failed"):
        psh._run_probe("pwsh", timeout=5.0)

    proc = created[0]
    assert proc.kill_called is True
    assert proc.wait_calls == 1
