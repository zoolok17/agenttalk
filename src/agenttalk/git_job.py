"""Windows only: run a changing Git command inside its own Windows job, so that a time-out can stop every program it
started (#907).

Git is started paused (``CREATE_SUSPENDED``), put into a new job through the process handle ``subprocess.Popen`` already
holds, and only then resumed (``NtResumeProcess`` on that same handle). Every program Git starts from then on is a
member of the job, because Windows puts a child into its parent's job. Windows keeps the member list itself: nothing
here reads a process list, a parent id or a creation time, and no clock decides whom to stop. If the command runs past
its time limit, the caller stops the whole job with one system call (``GitJob.stop``). A normal run never stops it: the
job sets no limits at all (no kill-on-close, no breakaway), so whatever stays in it after a normal end, such as Git's
file-system monitor, keeps running.

Asking to break away does not take a program out of the job: Windows refuses, or, under an outer job that allows
breakaway, lets the request through and the program stays in this job. A program that makes its own inner job still
stays in this one. A program started on Git's behalf by another process, such as a service or a scheduled task, never
was in the job and is not covered.

The native layer is loaded on the first call, never at import, so importing this module (and ``cli``) on Linux and macOS
loads nothing. The layer is replaceable (``_load_native``), so tests run this module's own logic on a fake one.

Who owns what: ``start_in_job`` owns everything from the job's creation until it returns; after that the caller owns the
job and every exit path closes it inside its own handler (``GitJob.close``). A handle that was closed successfully is
never used again: its value could already belong to something else.
"""

from __future__ import annotations

import contextlib
import subprocess  # nosec B404
import time
from collections.abc import Callable
from typing import Any

CREATE_SUSPENDED = 0x00000004
JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1

CLEANUP_SECONDS = 30.0  # the longest a single pass of the time-out handling takes without a job (5 s reap + 25 s wait)
COUNT_SECONDS = 2.0  # at most this long is spent waiting for the stopped job to be empty
_COUNT_POLL_SECONDS = 0.02


class JobStartError(OSError):
    """Git could not be started in a job (a missing system call, a refused resume): a start failure, not a time-out."""


def _monotonic() -> float:
    return time.monotonic()


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


class Deadline:
    """The one cleanup deadline of a job-enabled write, from the moment its own time-out has fired. It only limits
    waiting; it never decides whom to stop."""

    def __init__(self, seconds: float = CLEANUP_SECONDS) -> None:
        self._end = _monotonic() + seconds

    def left(self) -> float:
        return self._end - _monotonic()

    def cut(self, limit: float) -> float:
        """`limit`, cut to what is left (never below 0)."""
        return max(0.0, min(limit, self.left()))


class _WindowsNative:
    """The only code that calls Windows. Plain integers go in and out; tests replace it with a fake of its shape."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        class _Accounting(ctypes.Structure):  # JOBOBJECT_BASIC_ACCOUNTING_INFORMATION
            _fields_ = [
                ("TotalUserTime", ctypes.c_int64),
                ("TotalKernelTime", ctypes.c_int64),
                ("ThisPeriodTotalUserTime", ctypes.c_int64),
                ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                ("TotalPageFaultCount", wintypes.DWORD),
                ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD),
                ("TotalTerminatedProcesses", wintypes.DWORD),
            ]

        self._ctypes, self._accounting = ctypes, _Accounting
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)  # private copies, never ctypes.windll
        ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.TerminateJobObject.restype = wintypes.BOOL
        kernel.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel.QueryInformationJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        try:
            resume = ntdll.NtResumeProcess  # looked up now: a missing symbol is a start failure before anything runs
        except AttributeError as exc:
            raise JobStartError("this Windows has no NtResumeProcess to let a paused Git run") from exc
        resume.argtypes = [wintypes.HANDLE]
        resume.restype = ctypes.c_int32  # an NTSTATUS, read as a signed 32-bit value: 0 is success
        self._kernel, self._resume = kernel, resume

    def create_job(self) -> int | None:
        handle = self._kernel.CreateJobObjectW(None, None)
        return int(handle) if handle else None

    def assign(self, job: int, process: int) -> bool:
        return bool(self._kernel.AssignProcessToJobObject(job, process))

    def resume(self, process: int) -> int:
        return int(self._resume(process))

    def terminate(self, job: int, exit_code: int) -> bool:
        return bool(self._kernel.TerminateJobObject(job, exit_code))

    def active_processes(self, job: int) -> int | None:
        info = self._accounting()
        ok = self._kernel.QueryInformationJobObject(
            job, JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION, self._ctypes.byref(info), self._ctypes.sizeof(info), None
        )
        return int(info.ActiveProcesses) if ok else None

    def close(self, handle: int) -> bool:
        return bool(self._kernel.CloseHandle(handle))


def _load_native() -> Any:
    return _WindowsNative()


# Test seam: callables run with the job just before its native close (a real test reads the job's own member list
# through the job handle here). Empty in the product.
_BEFORE_CLOSE: list[Callable[[GitJob], None]] = []


class GitJob:
    """One write's job. Owns the handle until `CloseHandle` succeeds; afterwards every call is a no-op."""

    def __init__(self, native: Any, handle: int) -> None:
        self._native = native
        self._handle: int | None = handle
        self.stop_succeeded = False  # a stop reported success at least once

    @property
    def handle(self) -> int | None:
        """The job handle, or None once closed. For tests that read the job's own members; the product never uses it."""
        return self._handle

    def stop(self) -> bool:
        """End every process in the job (`TerminateJobObject`). Safe to repeat. False: it failed, or it is closed."""
        handle = self._handle
        if handle is None:
            return False
        try:
            ok = bool(self._native.terminate(handle, 1))
        except Exception:  # noqa: BLE001  # an ordinary failure is "not stopped"; an interruption propagates
            ok = False
        if ok:
            self.stop_succeeded = True
        return ok

    def live_count(self) -> int | None:
        """How many processes are alive in the job now, or None when that could not be read. Never 0 on a failure."""
        handle = self._handle
        if handle is None:
            return None
        try:
            count = self._native.active_processes(handle)
        except Exception:  # noqa: BLE001
            return None
        return None if count is None else int(count)

    def count_after_stop(self, deadline: Deadline) -> int | None:
        """The live count after a stop and the launcher's reap, polled until it is 0 or `COUNT_SECONDS` (cut to the
        deadline) has passed. None: it could not be read, or the shared deadline had passed before a query or by the
        time its answer came back (a sleep or a query that runs late is not given the benefit of a count read after
        the limit); the caller then keeps its warning."""
        limit = deadline.cut(COUNT_SECONDS)
        if limit <= 0:
            return None
        end = _monotonic() + limit
        while True:
            if deadline.left() <= 0:  # before each query: the absolute limit, whatever the last sleep really took
                return None
            count = self.live_count()
            if deadline.left() <= 0:  # before the answer is accepted: a query that returned late does not count
                return None
            if count is None or count == 0 or _monotonic() >= end:
                return count
            _sleep(min(_COUNT_POLL_SECONDS, max(0.0, end - _monotonic())))

    def close(self) -> bool:
        """Close our handle. Never stops the job (nothing stays tied to it: no kill-on-close). True when it is closed.

        - An ordinary failure (an exception, or `CloseHandle` answering False) is swallowed and the handle stays owned,
          so a later step may close it, or stop through it, again.
        - An interruption before the native call leaves it owned; one that arrives inside the native call may come after
          the close succeeded, so the handle is given up (a leaked handle at worst, never a call through a closed one).
        """
        handle = self._handle
        if handle is None:
            return True
        for seam in list(_BEFORE_CLOSE):
            seam(self)
        try:
            closed = bool(self._native.close(handle))
        except Exception:  # noqa: BLE001
            return False
        except BaseException:
            self._handle = None
            raise
        if closed:
            self._handle = None
        return closed


def start_in_job(
    cmd: list[str], *, reap: Callable[[Any, GitJob | None], None] | None = None, **popen_kwargs: Any
) -> tuple[Any, GitJob | None]:
    """Start `cmd` paused, put it in a new job, and let it run. Returns `(proc, job)`; `job` is None when the job could
    not be used (the caller then runs as it did without one).

    Raises `JobStartError` (an `OSError`) when Git could not be started or resumed, and whatever `Popen` raises. Owns
    everything until it returns, one step at a time:

    0. loading the native layer (a missing `NtResumeProcess` is a start failure; nothing was started);
    1. `CreateJobObjectW` (a refusal: no job, launch as before);
    2. the paused `Popen` (a failure: the job is closed, the error is raised);
    3. the assignment through `Popen`'s own handle (a refusal: close the job and go on with none);
    4. `NtResumeProcess` on the same handle (anything but status 0: the launcher is stopped and reaped, the job closed,
       and it is a start failure: Git never ran);
    5. the hand-over.

    An interruption in steps 3 to 5 goes through `reap(proc, job)` (a bounded, swallowing cleanup: it stops the job,
    kills and reaps the launcher, and closes the job as its last step; Git may already be running in the job after the
    resume) and is raised again; the first interruption is the one raised.
    """
    native = _load_native()  # step 0
    try:
        handle = native.create_job()  # step 1
    except Exception:  # noqa: BLE001
        handle = None
    if not handle:
        return subprocess.Popen(cmd, **popen_kwargs), None  # exactly as without a job  # noqa: S603  # nosec B603
    job: GitJob | None = GitJob(native, handle)
    popen_kwargs["creationflags"] = popen_kwargs.get("creationflags", 0) | CREATE_SUSPENDED
    try:
        proc = subprocess.Popen(cmd, **popen_kwargs)  # step 2  # noqa: S603  # nosec B603
    except BaseException:
        with contextlib.suppress(BaseException):  # a later interruption here must not replace the first
            job.close()
        raise

    def clean_up() -> None:
        (reap or _default_reap)(proc, job)

    resume_error = None
    try:
        process = int(proc._handle)  # type: ignore[attr-defined]  # the handle Popen already holds
        try:
            assigned = bool(native.assign(handle, process))  # step 3
        except Exception:  # noqa: BLE001
            assigned = False
        if not assigned:
            job.close()  # an ordinary failure inside is swallowed; an interruption goes to the cleanup below
            job = None
        try:
            status = int(native.resume(process))  # step 4
        except Exception as exc:  # noqa: BLE001
            status, resume_error = -1, str(exc) or type(exc).__name__
        if status != 0:
            detail = resume_error or f"status {status & 0xFFFFFFFF:#010x}"
            raise JobStartError(f"could not resume it ({detail})")  # the cleanup below runs, then this is raised
        # Step 5, the hand-over, is still inside the boundary: an interruption at this very return, after Git was
        # resumed, stops the job and the launcher too, because the caller does not own them until the return is done.
        return proc, job
    except BaseException:
        clean_up()
        raise


def _default_reap(proc: Any, job: GitJob | None) -> None:
    """The cleanup used when the caller gives none: stop the job (Git may already be running in it), kill the launcher,
    then close the job. Nothing escapes."""
    if job is not None:
        with contextlib.suppress(BaseException):
            job.stop()
    with contextlib.suppress(BaseException):
        proc.kill()
    if job is not None:
        with contextlib.suppress(BaseException):
            job.close()
