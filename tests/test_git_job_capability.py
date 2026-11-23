"""#907 host capability check: can Git be started paused, put in a Windows job, and let go, on this host?

This tests the operating system and the host, not the product (it passes on a tree that has no job code at all). It is
the first commit of the change, before any product change: if the host refuses the assignment or the resume, the
helper is dropped. Run it with ``-m windows_capability -s``, so the facts it records are shown, not captured. It also
writes them to a small JSON artifact (``AGENTTALK_JOB_CAPABILITY_ARTIFACT`` names the file; the default is next to the
test's temporary folder).

It records: the Windows version, whether pytest already runs inside a job (``IsProcessInJob`` with no job), the session
id (0 is a service session), and the outer job's limit flags when there is an outer job. When pytest runs in no job, a
Python child is started inside an outer job of its own and runs the same check there, so a nested job is always tried.
"""

import ctypes
import json
import os
import platform
import shutil
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path

import pytest

CREATE_SUSPENDED = 0x00000004
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows job objects")


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_uint64)
        for name in (
            "ReadOperationCount",
            "WriteOperationCount",
            "OtherOperationCount",
            "ReadTransferCount",
            "WriteTransferCount",
            "OtherTransferCount",
        )
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _libraries():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
    kernel.IsProcessInJob.restype = wintypes.BOOL
    kernel.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel.QueryInformationJobObject.restype = wintypes.BOOL
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    kernel.ProcessIdToSessionId.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    ntdll.NtResumeProcess.restype = ctypes.c_int32  # an NTSTATUS: 0 is success
    return kernel, ntdll


def host_facts(kernel) -> dict:
    """What this process is running in (never raises)."""
    facts = {
        "windows": platform.platform(),
        "windows_build": sys.getwindowsversion().build,
        "python": platform.python_version(),
    }
    inside = wintypes.BOOL(0)
    if kernel.IsProcessInJob(kernel.GetCurrentProcess(), None, ctypes.byref(inside)):
        facts["pytest_process_in_a_job"] = bool(inside.value)
    else:
        facts["pytest_process_in_a_job"] = f"IsProcessInJob failed ({ctypes.get_last_error()})"
    session = wintypes.DWORD(0)
    if kernel.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)):
        facts["session_id"] = int(session.value)
    else:
        facts["session_id"] = f"ProcessIdToSessionId failed ({ctypes.get_last_error()})"
    if facts["pytest_process_in_a_job"] is True:
        limits, returned = _ExtendedLimits(), wintypes.DWORD(0)
        if kernel.QueryInformationJobObject(
            None,
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
            ctypes.byref(returned),
        ):
            facts["outer_job_limit_flags"] = int(limits.BasicLimitInformation.LimitFlags)
        else:
            facts["outer_job_limit_flags"] = f"QueryInformationJobObject failed ({ctypes.get_last_error()})"
    return facts


def run_capability_check(git: str) -> dict:
    """`git --version` started paused, put in a fresh job, resumed through the process handle, and waited for."""
    kernel, ntdll = _libraries()
    result = host_facts(kernel)
    job = kernel.CreateJobObjectW(None, None)
    result["job_created"] = bool(job)
    if not job:
        result["error"] = f"CreateJobObjectW failed ({ctypes.get_last_error()})"
        return result
    proc = None
    try:
        proc = subprocess.Popen(  # noqa: S603  # nosec B603
            [git, "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=CREATE_SUSPENDED,
        )
        handle = int(proc._handle)  # the handle Popen holds
        result["assigned"] = bool(kernel.AssignProcessToJobObject(job, handle))
        if not result["assigned"]:
            result["error"] = f"AssignProcessToJobObject refused ({ctypes.get_last_error()})"
            return result
        inside = wintypes.BOOL(0)
        result["git_in_our_job"] = bool(kernel.IsProcessInJob(handle, job, ctypes.byref(inside))) and bool(inside.value)
        result["resume_status"] = int(ntdll.NtResumeProcess(handle))
        if result["resume_status"] != 0:
            result["error"] = f"NtResumeProcess returned {result['resume_status']:#x}"
            return result
        out, _err = proc.communicate(timeout=60)
        result["git_exit_code"] = proc.returncode
        result["git_output"] = out.strip()
        return result
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=30)
        kernel.CloseHandle(job)


def run_inside_an_outer_job(git: str) -> dict:
    """The same check in a Python child that was itself started in an outer job of our own (so the job is nested)."""
    kernel, ntdll = _libraries()
    outer = kernel.CreateJobObjectW(None, None)
    assert outer, f"CreateJobObjectW failed ({ctypes.get_last_error()})"
    code = (
        "import json, sys; sys.path.insert(0, sys.argv[1]); import test_git_job_capability as m; "
        "print(json.dumps(m.run_capability_check(sys.argv[2])))"
    )
    child = subprocess.Popen(  # noqa: S603  # nosec B603
        [sys.executable, "-c", code, str(Path(__file__).resolve().parent), git],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=CREATE_SUSPENDED,
    )
    try:
        handle = int(child._handle)
        assert kernel.AssignProcessToJobObject(outer, handle), f"outer job refused ({ctypes.get_last_error()})"
        assert ntdll.NtResumeProcess(handle) == 0
        out, err = child.communicate(timeout=120)
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=30)
        kernel.CloseHandle(outer)
    assert child.returncode == 0, err
    return json.loads(out.strip().splitlines()[-1])


def _assert_capable(facts: dict) -> None:
    assert facts.get("job_created"), facts
    assert facts.get("assigned"), facts  # a refused assignment: the helper is dropped (the kill signal)
    assert facts.get("git_in_our_job") is True, facts
    assert facts.get("resume_status") == 0, facts  # a refused resume: the helper is dropped
    assert facts.get("git_exit_code") == 0 and facts.get("git_output", "").startswith("git version"), facts


@pytest.mark.windows_capability
def test_check_git_job_capability(tmp_path, capsys):
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not on PATH")
    plain = run_capability_check(git)
    report = {"host": host_facts(_libraries()[0]), "direct": plain}
    if plain.get("pytest_process_in_a_job") is False:
        report["nested_in_an_outer_job"] = run_inside_an_outer_job(git)  # no outer job here: make one
    with capsys.disabled():  # shown even without -s: this is the record
        print("\ngit-job-capability: " + json.dumps(report, sort_keys=True))
    artifact = Path(os.environ.get("AGENTTALK_JOB_CAPABILITY_ARTIFACT") or tmp_path / "git-job-capability.json")
    artifact.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    _assert_capable(plain)
    if "nested_in_an_outer_job" in report:
        _assert_capable(report["nested_in_an_outer_job"])
        assert report["nested_in_an_outer_job"]["pytest_process_in_a_job"] is True  # the child really was nested
