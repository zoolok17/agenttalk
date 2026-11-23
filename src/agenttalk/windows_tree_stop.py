"""Stop a Windows process and the programs it started, without launching any program (#791).

``stop_process_tree`` is for the one place that needs it: a Git command that ran past its
time limit. It replaces ``taskkill.exe``, whose launch can itself fail with a modal dialog
under heavy load (0xc0000142, the problem the turn watchdog already avoids).

It is best effort and says so. A process snapshot is not containment: a process can start a
child after the snapshot, and a middle process that exited earlier hides its children. The
answer is ``True`` only when every process the helper could verify has ended and a final
look found nothing more. Any doubt answers ``False``; the caller then keeps its warning.

How a process is accepted as a descendant (this is what protects unrelated programs):

* Windows never updates a recorded parent id, and it reuses freed ids. A process counts as a
  child only if its parent id names a process this helper holds a handle to (so that id cannot
  be reused meanwhile) and it was created strictly later than that parent.
* The candidate is opened first. Only then is a fresh snapshot taken, and the candidate is
  accepted only if that snapshot still lists its id under the same parent. This closes the gap
  in which a real child exits and an unrelated new program takes its id before the open.
* A candidate that cannot be opened, or cannot be verified, makes the stop "not confirmed".
  An older process that merely names a reused parent id is left alone and does not.

All native calls sit behind a small replaceable layer (``_Kernel32``), so tests can run the
product's own matching code against fake process lists.
"""

from __future__ import annotations

import contextlib
import ctypes
import time
from dataclasses import dataclass, field
from typing import Any

# Access rights for the one handle taken per process: PROCESS_QUERY_LIMITED_INFORMATION,
# PROCESS_TERMINATE and SYNCHRONIZE.
_ACCESS = 0x1000 | 0x0001 | 0x00100000
_TH32CS_SNAPPROCESS = 0x00000002
_ERROR_NO_MORE_FILES = 18
_WAIT_OBJECT_0 = 0

MAX_RECHECK_PASSES = 3
STOP_DEADLINE_SECONDS = 10.0


class NativeError(OSError):
    """A native call failed in a way that means the process list cannot be trusted."""


class _Kernel32:
    """The only code that calls Windows. Tests replace it with a fake of the same shape."""

    def __init__(self) -> None:
        from ctypes import wintypes

        k = ctypes.WinDLL("kernel32", use_last_error=True)  # private copy, never ctypes.windll
        self._k = k
        self._wintypes = wintypes

        class ProcessEntry(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260),
            ]

        self._entry = ProcessEntry
        k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        k.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
        k.Process32FirstW.restype = wintypes.BOOL
        k.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
        k.Process32NextW.restype = wintypes.BOOL
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.OpenProcess.restype = wintypes.HANDLE
        k.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        k.GetProcessTimes.restype = wintypes.BOOL
        k.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k.TerminateProcess.restype = wintypes.BOOL
        k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k.WaitForSingleObject.restype = wintypes.DWORD
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.CloseHandle.restype = wintypes.BOOL

    def snapshot(self) -> dict[int, int]:
        """Every listed process id with its recorded parent id. Raises ``NativeError`` on any failure."""
        k = self._k
        handle = k.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        if handle is None or handle == self._wintypes.HANDLE(-1).value:
            raise NativeError("could not take a process snapshot")
        try:
            entry = self._entry()
            entry.dwSize = ctypes.sizeof(entry)
            parents: dict[int, int] = {}
            ok = k.Process32FirstW(handle, ctypes.byref(entry))
            while True:
                if not ok:
                    if ctypes.get_last_error() == _ERROR_NO_MORE_FILES:
                        return parents
                    raise NativeError("could not read the process snapshot")
                parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                ok = k.Process32NextW(handle, ctypes.byref(entry))
        finally:
            k.CloseHandle(handle)

    def open_process(self, pid: int) -> int | None:
        handle = self._k.OpenProcess(_ACCESS, False, pid)
        return int(handle) if handle else None

    def creation_time(self, handle: int) -> int | None:
        """Creation time in 100 ns ticks, or None when it cannot be read."""
        creation, exited, kernel, user = (self._wintypes.FILETIME() for _ in range(4))
        ok = self._k.GetProcessTimes(
            handle, ctypes.byref(creation), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)
        )
        if not ok:
            return None
        return (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)

    def terminate(self, handle: int) -> bool:
        return bool(self._k.TerminateProcess(handle, 1))

    def wait(self, handle: int, milliseconds: int) -> int:
        return int(self._k.WaitForSingleObject(handle, milliseconds))

    def close(self, handle: int) -> None:
        self._k.CloseHandle(handle)


@dataclass
class _Node:
    pid: int
    handle: int
    created: int
    depth: int
    owned: bool  # False for the launcher: Popen owns that handle and closes it itself


@dataclass
class _Walk:
    """What one stop attempt holds. Everything here is verified, except ``pending``."""

    native: Any
    deadline: float
    nodes: list[_Node] = field(default_factory=list)
    pending: list[int] = field(default_factory=list)  # opened, not yet verified: never stopped
    ok: bool = True  # False after the first failure of any step: the result can no longer be "confirmed"

    def held_pids(self) -> dict[int, _Node]:
        return {n.pid: n for n in self.nodes}

    def remaining_ms(self) -> int:
        return max(0, int((self.deadline - time.monotonic()) * 1000))

    def discover(self, parents: list[_Node]) -> list[_Node]:
        """Find, verify and hold every descendant of ``parents`` (and theirs), level by level."""
        found: list[_Node] = []
        frontier = list(parents)
        while frontier:
            try:
                listing = self.native.snapshot()
            except Exception:
                self.ok = False
                return found
            held = self.held_pids()
            by_parent = {n.pid: n for n in frontier}
            candidates = [
                (pid, ppid) for pid, ppid in listing.items() if ppid in by_parent and pid != ppid and pid not in held
            ]
            opened: list[tuple[int, int, int]] = []
            for pid, ppid in candidates:
                try:
                    handle = self.native.open_process(pid)
                except Exception:
                    handle = None
                if handle is None:
                    self.ok = False  # gone or closed to us: it cannot be verified
                    continue
                self.pending.append(handle)
                opened.append((pid, ppid, handle))
            if not opened:
                return found
            # The verifying snapshot always comes after every open of this level.
            try:
                verify = self.native.snapshot()
            except Exception:
                self.ok = False
                return found
            next_frontier: list[_Node] = []
            for pid, ppid, handle in opened:
                parent = by_parent[ppid]
                keep = False
                try:
                    if verify.get(pid) != ppid:
                        self.ok = False  # the id changed owner, or it left: not verified, never stopped
                    else:
                        created = self.native.creation_time(handle)
                        if created is None:
                            self.ok = False
                        elif created > parent.created:  # strictly later; an older process is not ours
                            node = _Node(pid, handle, created, parent.depth + 1, True)
                            self.nodes.append(node)
                            found.append(node)
                            next_frontier.append(node)
                            keep = True
                finally:
                    self.pending.remove(handle)
                    if not keep:
                        self._close(handle)
            frontier = next_frontier
        return found

    def _close(self, handle: int) -> None:
        with contextlib.suppress(Exception):
            self.native.close(handle)

    def stop(self, nodes: list[_Node]) -> None:
        """Stop ``nodes`` deepest first (the launcher last), then wait once, on one shared deadline."""
        for node in sorted(nodes, key=lambda n: n.depth, reverse=True):
            try:
                stopped = self.native.terminate(node.handle)
                if not stopped:
                    # An already-ended process answers "failed" to a stop; it still counts as stopped.
                    stopped = self.native.wait(node.handle, 0) == _WAIT_OBJECT_0
            except Exception:
                stopped = False
            if not stopped:
                self.ok = False
        for node in nodes:
            try:
                if self.native.wait(node.handle, self.remaining_ms()) != _WAIT_OBJECT_0:
                    self.ok = False
            except Exception:
                self.ok = False

    def stop_held(self) -> None:
        for node in sorted(self.nodes, key=lambda n: n.depth, reverse=True):
            with contextlib.suppress(Exception):
                self.native.terminate(node.handle)

    def release(self) -> None:
        for handle in self.pending:
            self._close(handle)
        self.pending.clear()
        for node in self.nodes:
            if node.owned:
                self._close(node.handle)
        self.nodes.clear()


class TreeStop:
    """One stop attempt, split so the caller can still act on what was verified.

    ``run()`` stops the tree and answers whether the stop is confirmed. The handles stay open
    afterwards, so that an interruption in the caller's own follow-up (the reap) can still call
    ``stop_held()``; the caller must call ``close()`` when it is done. ``run()`` never raises
    ``Exception``. On an interruption (``KeyboardInterrupt``, ``SystemExit``) it stops what is
    already verified and raises the interruption again; handles stay open for ``close()``.
    """

    def __init__(
        self,
        proc: Any,
        *,
        native: Any = None,
        deadline_seconds: float = STOP_DEADLINE_SECONDS,
        recheck_passes: int = MAX_RECHECK_PASSES,
    ) -> None:
        self._proc = proc
        self._native = native
        self._deadline_seconds = deadline_seconds
        self._recheck_passes = recheck_passes
        self._walk: _Walk | None = None

    def run(self) -> bool:
        try:
            launcher = int(self._proc._handle)  # noqa: SLF001  # the handle Popen already holds
            native = self._native if self._native is not None else _default_native()
            walk = _Walk(native, time.monotonic() + self._deadline_seconds)
            created = native.creation_time(launcher)
        except Exception:
            return False
        if created is None:
            return False
        self._walk = walk
        root = _Node(int(self._proc.pid), launcher, created, 0, False)
        walk.nodes.append(root)
        try:
            # The launcher is walked even if it has already ended: a child holding the output
            # pipes is the usual reason the reap after a timeout fails.
            walk.stop(walk.discover([root]) + [root])
            for _ in range(self._recheck_passes):
                fresh = walk.discover(list(walk.nodes))
                if not fresh:
                    return walk.ok  # a pass that accepted nothing: confirmed unless a step failed
                walk.stop(fresh)
            return False  # still finding processes after the last pass
        except Exception:
            return False
        except BaseException:
            self.stop_held()
            raise

    def stop_held(self) -> None:
        """Ask every verified process to stop, deepest first. One failure never skips the rest."""
        if self._walk is not None:
            self._walk.stop_held()

    def close(self) -> None:
        if self._walk is not None:
            self._walk.release()


def stop_process_tree(proc: Any, **options: Any) -> bool:
    """Stop ``proc`` and its descendants and close every handle. True only if the stop is confirmed."""
    tree = TreeStop(proc, **options)
    try:
        return tree.run()
    finally:
        tree.close()


def _default_native() -> Any:
    return _Kernel32()
