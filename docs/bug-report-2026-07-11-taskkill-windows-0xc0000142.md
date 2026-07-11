# Bug report — Windows `taskkill.exe 0xc0000142` popups from the wrapper turn-watchdog

> **Upstream resolution note — added 2026-07-11 by the agenttalk maintainers
> (editorial; the original reporter text below is preserved verbatim).**
>
> - **Fixed in v0.74.0:** the per-turn watchdog no longer launches the
>   `taskkill.exe` subprocess at all. Verified targets are terminated natively
>   via `os.kill(pid, signal.SIGTERM)` (abrupt `TerminateProcess` on Windows),
>   which removes the exact subprocess path that produced the reported popup.
> - **Root-cause status:** the reporter's desktop-heap exhaustion attribution is
>   a plausible field diagnosis consistent with the observed `0xc0000142`
>   pattern, but it was **not reproduced or proven upstream**; the fix stands on
>   removing the failing subprocess, not on confirming that attribution.
> - **Known residuals (follow-up hardening, not part of this fix):** the
>   watchdog's process snapshot and start-time helpers still launch
>   PowerShell/CIM subprocesses under the same theoretical pressure; a PID can
>   be reused after the pre-kill recheck; and leaf-first snapshot termination is
>   best-effort, not an atomic tree kill.
> - **Downstream status:** the reporter's workaround (supervisor off,
>   hand-managed fleet) has **not been retested** against the fixed build; the
>   deployment can resume supervised operation on v0.74.0 with the residuals
>   above understood.

**From:** Polaris (Claude interactive lead, orbitlauncher deployment)
**agenttalk version:** 0.73.0 (installed to site-packages, not editable)
**Severity:** HIGH (interrupts the operator with modal OS dialogs; recurs whenever the supervised fleet is active)
**Reported by owner:** "taskkill popup appeared again ... could be an agenttalk bug?"

## Symptom
Recurring **modal Windows dialog**: *"taskkill.exe - Application Error — The application was unable to
start correctly (0xc0000142). Click OK to close the application."* Appears repeatedly while the
supervised fleet runs; stops entirely when the supervisor is stopped + kill-switched; returns on
supervisor restart. RAM is NOT the constraint (24–36% free when it fires).

## Root cause (confirmed)
`src/agenttalk/wrapper/turn_watchdog.py` → `_kill_one(pid)` (≈L293–305) shells out to a **new
console process** on Windows:

```python
def _kill_one(pid: int) -> bool:
    try:
        if sys.platform.startswith("win"):
            return subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],       # <-- spawns taskkill.exe
                capture_output=True, text=True, timeout=10, check=False,
            ).returncode == 0
        import os, signal
        os.kill(pid, signal.SIGKILL)                        # POSIX already native
        return True
    except (OSError, subprocess.SubprocessError, ProcessLookupError):
        return False
```

Two problems, both Windows-only:
1. **It launches a console app to kill a process.** Under a busy fleet (many wrapped agents + their
   conhosts + concurrent Gradle builds) the fixed ~20 MB Windows **interactive desktop heap**
   (`HKLM\...\Session Manager\SubSystems\Windows` → `SharedSection=1024,20480,768`, default) is
   exhausted. New process launches then fail DLL init with **`0xc0000142` (STATUS_DLL_INIT_FAILED)** —
   independent of RAM. `taskkill.exe` is the frequent victim.
2. **On failure it shows a modal WER dialog.** Because taskkill is a console app whose init fails, the
   OS surfaces the interactive error dialog to the operator — repeatedly, since the watchdog retries.

Note the supervisor's own kill path (`supervisor.ps1` → `Stop-Tree`, native `Stop-Process`) is
CORRECT and does not exhibit this. It is specifically the wrapper watchdog.

## Fix (one function, ~6 lines) — make Windows native like POSIX
`os.kill()` on Windows maps any non-CTRL signal to `TerminateProcess()`, i.e. the same effect as
`taskkill /F /PID` — with NO console spawn and NO dialog:

```python
def _kill_one(pid: int) -> bool:
    import os, signal
    try:
        # Native terminate on both platforms. os.kill maps SIGTERM->TerminateProcess on Windows;
        # avoids spawning taskkill.exe, which under desktop-heap pressure fails to init (0xc0000142)
        # and shows a modal error dialog.
        os.kill(pid, signal.SIGTERM if sys.platform.startswith("win") else signal.SIGKILL)
        return True
    except (OSError, ProcessLookupError):
        return False
```
(If the watchdog ever needs a whole tree, prefer `psutil` child enumeration + native kill over
`taskkill /T`.)

## Secondary mitigation (environment, optional)
Even with the native fix, sustained heavy fleets approach the interactive desktop-heap limit. Optional
hardening: raise `SharedSection` 2nd value (interactive heap) from 20480 → e.g. 40960 KB (registry;
requires reboot), and/or cap concurrent wrapped agents + concurrent Gradle builds. These reduce the
underlying `0xc0000142` exposure for *all* process launches, not just taskkill.

## Workaround in effect (orbitlauncher deployment)
Per owner decision (2026-07-11): NOT patching the local agenttalk install; running the fleet **lean
with the supervisor OFF + kill-switch present**, hand-managing the ~4 active agents. Popups stopped.
Awaiting the upstream fix to resume the fully-autonomous supervised fleet.
