"""Per-turn watchdog for a wrapped CLI turn (used by ``wrapper/run.py`` ``_ProcStream``).

The wrapped-Codex first-turn hang: a supervised ``agenttalk wrap --loop --cli codex``
turn can wedge forever when Codex launches a hung tool descendant
(``codex.exe -> pwsh -> node`` REPL) that never exits, so the per-turn child's stdout
never closes and the turn never completes. A pure no-output timer is WRONG - wrapped
Codex is legitimately silent during long pure reasoning - so the discriminator is
TWO-FACTOR and BOTH are required:

  1. the turn has been running >= ``turn_elapsed_seconds`` (default 1800), AND
  2. at least one LIVE non-Codex tool-like DESCENDANT of the per-turn root has been
     alive >= ``tool_descendant_alive_seconds`` (default 600).

Pure reasoning has no live tool descendant, so factor 2 never fires on it. On fire the
watchdog kills ONLY the per-turn root's process tree (descendants first, start-time
guarded against pid reuse, then the root); never ancestors/siblings. It NEVER kills on
a missing/failed snapshot or a root start-time mismatch (FAIL-OPEN). The kill closes
the child's stdout, so ``_ProcStream``'s stream ends and the turn is classified
``CLASS_AMBIGUOUS`` (never poison) - the inbound message stays pending and rides the
high ``K_escalate`` ceiling.

Layering: the discriminator + target selection are PURE (a snapshot dict in, a decision
out) and fully unit-tested with fake snapshots; only the thin OS adapter
(:func:`snapshot_processes` / :func:`kill_pids`) touches the OS, and it fails open. No
``psutil`` dependency and no import of the supervisor PowerShell - the Stop-Tree
SEMANTICS (leaves-first, start-time guarded) are mirrored here in pure Python.
"""
from __future__ import annotations

import subprocess  # nosec B404
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

# A snapshot maps pid -> {"ppid": int, "name": str, "create_epoch": float | None}.
# ``create_epoch`` is POSIX seconds (the adapter normalizes OS-specific time); None when
# the OS did not report it (then age falls back to first-seen, best-effort).
Snapshot = dict


@dataclass(frozen=True)
class TurnWatchdogConfig:
    """Resolved per-agent watchdog knobs. ``enabled`` is OFF by default; cmd_wrap turns
    it on only for continuous ``wrap --loop --cli codex``."""

    enabled: bool = False
    turn_elapsed_seconds: float = 1800.0
    tool_descendant_alive_seconds: float = 600.0
    poll_seconds: float = 10.0
    allow_low_turn_elapsed: bool = False


# Below this floor a turn_elapsed bound is too aggressive (it could clip a legitimately
# long reasoning+tool turn): cmd_wrap/resolve REFUSE it unless allow_low_turn_elapsed is
# set, mirroring the supervisor's allow_low_stuck_after guard. Never silently coerced.
SAFE_TURN_ELAPSED_FLOOR = 1200.0


def watchdog_effectively_live(cfg: TurnWatchdogConfig) -> bool:
    """The SINGLE source of truth for "will the per-turn watchdog actually run for this
    resolved config" - imported by BOTH cmd_wrap (its start/disable decision) AND the
    supervisor planner (apply the restart-preemption guard ONLY when the watchdog is live),
    so the two layers can NEVER disagree (the bug where the wrapper disabled the watchdog but
    the supervisor still refused restart-on-stale, leaving a wedge with no recovery). A
    sub-floor turn_elapsed WITHOUT allow_low_turn_elapsed is NOT live (cmd_wrap disables it)."""
    if not cfg.enabled:
        return False
    return not (cfg.turn_elapsed_seconds < SAFE_TURN_ELAPSED_FLOOR
                and not cfg.allow_low_turn_elapsed)


# Names that are the Codex brain / its internal runner / a console host - NEVER candidates
# (killing them is killing the turn's own engine, not a hung tool). Everything else that
# is a live descendant is a candidate (conservative: unknown-but-named = candidate). A
# process we cannot positively NAME is NOT a candidate (mirrors the supervisor's
# never-kill-the-unidentifiable posture).
_EXCLUDED_DESCENDANT_NAMES = frozenset({
    "codex.exe", "codex",
    "codex-command-runner.exe", "codex-command-runner",
    "conhost.exe",
})
_EXCLUDED_NAME_PREFIXES = ("openai-codex",)

# create_epoch float compare tolerance (clock granularity / reporting jitter).
_START_EPS = 2.0


def _norm_name(name: object) -> str:
    return str(name).strip().lower() if name is not None else ""


def is_tool_like_descendant(name: object) -> bool:
    """True if ``name`` is a killable tool descendant (not the Codex brain/runner/console
    and not empty). Unknown-but-named processes ARE candidates by design; an unnamed
    process is not (we never kill what we cannot identify)."""
    n = _norm_name(name)
    if not n:
        return False
    if n in _EXCLUDED_DESCENDANT_NAMES:
        return False
    return not any(n.startswith(p) for p in _EXCLUDED_NAME_PREFIXES)


def _start_matches(observed: object, recorded: object) -> bool:
    """The recorded root start-time still matches the snapshot (defeats pid reuse). If
    EITHER side is unknown we cannot prove identity -> treat as NOT matching (fail-open:
    we refuse to kill a root we cannot confirm)."""
    if observed is None or recorded is None:
        return False
    try:
        return abs(float(observed) - float(recorded)) <= _START_EPS
    except (TypeError, ValueError):
        return observed == recorded


def descendants_of(snapshot: Snapshot, root_pid: int) -> list[int]:
    """PIDs whose parent-chain reaches ``root_pid`` (the root itself excluded). Pure and
    cycle-safe (a malformed snapshot with a parent cycle terminates via ``seen``)."""
    children: dict[int, list[int]] = {}
    for pid, info in snapshot.items():
        ppid = (info or {}).get("ppid")
        children.setdefault(ppid, []).append(pid)
    out: list[int] = []
    seen: set[int] = set()
    stack = list(children.get(root_pid, []))
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        out.append(pid)
        stack.extend(children.get(pid, []))
    return out


def _descendant_age(info: dict, now: float, first_seen: float | None) -> float | None:
    """Age in seconds of a descendant. PREFER OS create-time (always current truth, so a
    reused pid reads as young); fall back to first-seen only when create-time is missing.
    None when neither is known (then it cannot satisfy the age gate)."""
    ce = (info or {}).get("create_epoch")
    if isinstance(ce, (int, float)) and not isinstance(ce, bool):
        return max(0.0, now - float(ce))
    if first_seen is not None:
        return max(0.0, now - first_seen)
    return None


@dataclass(frozen=True)
class WatchdogDecision:
    fire: bool
    reason: str
    trigger: dict | None = None          # {pid, name, age} of the descendant that fired
    kill_order: list = field(default_factory=list)  # leaves-first, then the root


def evaluate(snapshot: Snapshot | None, *, root_pid: int, root_start: object,
             elapsed: float, now: float, cfg: TurnWatchdogConfig,
             first_seen: dict | None = None) -> WatchdogDecision:
    """PURE two-factor decision. FAIL-OPEN: returns ``fire=False`` whenever the snapshot is
    missing, the root is gone, the root start-time does not match (pid reuse), or no aged
    tool descendant is present."""
    first_seen = first_seen or {}
    if elapsed < cfg.turn_elapsed_seconds:
        return WatchdogDecision(False, "turn within elapsed bound")
    if not snapshot:
        return WatchdogDecision(False, "snapshot unavailable")        # fail-open
    root_info = snapshot.get(root_pid)
    if root_info is None:
        return WatchdogDecision(False, "root not in snapshot")        # root already gone
    if not _start_matches(root_info.get("create_epoch"), root_start):
        return WatchdogDecision(False, "root start mismatch (pid reuse)")
    desc = descendants_of(snapshot, root_pid)
    trigger = None
    for pid in desc:
        info = snapshot[pid]
        if not is_tool_like_descendant(info.get("name")):
            continue
        age = _descendant_age(info, now, first_seen.get(pid))
        if age is not None and age >= cfg.tool_descendant_alive_seconds:
            trigger = {"pid": pid, "name": info.get("name"), "age": age}
            break
    if trigger is None:
        return WatchdogDecision(False, "no aged tool descendant")
    # Leaves-first: descendants_of yields a pre-order (parents before children); reversing
    # kills the deepest first, then the root last - the supervisor Stop-Tree semantics. EACH
    # target carries its OBSERVED start so the kill adapter can re-verify it at KILL time
    # (the snapshot->kill window is otherwise a pid-reuse hole), exactly like the supervisor
    # Proc-Start recheck.
    kill_order = [{"pid": pid, "start": snapshot[pid].get("create_epoch")}
                  for pid in reversed(desc)]
    kill_order.append({"pid": root_pid, "start": root_info.get("create_epoch")})
    return WatchdogDecision(True, "two-factor fire", trigger=trigger, kill_order=kill_order)


# --------------------------------------------------------------- OS adapter (fail-open)

def snapshot_processes(timeout: float = 5.0) -> Snapshot | None:
    """Best-effort {pid: {ppid, name, create_epoch}} for the whole machine. Returns None on
    ANY error or timeout (the caller fails open and never kills). Windows uses a single
    ``Get-CimInstance Win32_Process`` query; POSIX uses ``ps`` with elapsed seconds."""
    try:
        if sys.platform.startswith("win"):
            return _snapshot_windows(timeout)
        return _snapshot_posix(timeout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _run(argv: list[str], timeout: float) -> str | None:
    try:
        out = subprocess.run(  # noqa: S603  # nosec B603
            argv, capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def _snapshot_windows(timeout: float) -> Snapshot | None:
    # One CIM query; CreationDate -> Unix seconds via [datetimeoffset]. Pipe-delimited so
    # the parse never depends on locale/whitespace in process names.
    ps = (
        "Get-CimInstance Win32_Process | ForEach-Object { "
        "\"$($_.ProcessId)|$($_.ParentProcessId)|$($_.Name)|"
        "$(if ($_.CreationDate) { ([datetimeoffset]$_.CreationDate).ToUnixTimeSeconds() })\" }"
    )
    out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], timeout)
    if out is None:
        return None
    snap: Snapshot = {}
    for line in out.splitlines():
        parts = line.rstrip("\r").split("|", 3)
        if len(parts) != 4:
            continue
        pid_s, ppid_s, name, ce_s = parts
        try:
            pid, ppid = int(pid_s), int(ppid_s)
        except ValueError:
            continue
        ce: float | None
        try:
            ce = float(ce_s) if ce_s.strip() else None
        except ValueError:
            ce = None
        snap[pid] = {"ppid": ppid, "name": name, "create_epoch": ce}
    return snap


def _snapshot_posix(timeout: float) -> Snapshot | None:
    # etimes = elapsed seconds since start -> create_epoch = now - etimes (uniform with
    # the Windows path so the pure layer is OS-agnostic).
    out = _run(["ps", "-axo", "pid=,ppid=,etimes=,comm="], timeout)
    if out is None:
        return None
    now = time.time()
    snap: Snapshot = {}
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        pid_s, ppid_s, et_s, name = parts
        try:
            pid, ppid, et = int(pid_s), int(ppid_s), int(et_s)
        except ValueError:
            continue
        snap[pid] = {"ppid": ppid, "name": name, "create_epoch": now - et}
    return snap


def proc_start(pid: int) -> float | None:
    """LIVE create_epoch (POSIX seconds) for a SINGLE pid, or None if it is gone / unknown /
    the query errors. Used to re-verify a kill target the instant before killing it, so a pid
    that exited and was REUSED between the firing snapshot and the kill is not killed. Fails
    closed (None) - the caller then skips the target."""
    try:
        if sys.platform.startswith("win"):
            ps = (f"Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}' | "
                  "ForEach-Object { ([datetimeoffset]$_.CreationDate).ToUnixTimeSeconds() }")
            out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], 5.0)
            if not out or not out.strip():
                return None
            return float(out.strip().splitlines()[0])
        out = _run(["ps", "-o", "etimes=", "-p", str(int(pid))], 5.0)
        if not out or not out.strip():
            return None
        return time.time() - int(out.strip().split()[0])
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


def _kill_one(pid: int) -> bool:
    try:
        import os
        import signal
        if sys.platform.startswith("win"):
            os.kill(pid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGKILL)
        return True
    except (OSError, subprocess.SubprocessError, ProcessLookupError):
        return False


def kill_targets(targets: list, *, start_fn=proc_start, killer=None) -> list[int]:
    """Best-effort kill of each ``{pid, start}`` target in order (leaves-first). For EACH
    target re-read the LIVE start-time and kill ONLY if it still matches the observed start;
    on mismatch (pid reuse) OR an unavailable/missing start, SKIP the target (fail-open - we
    never kill a pid we cannot positively confirm is still the same process). Mirrors the
    supervisor Stop-Tree Proc-Start recheck. Returns the pids actually killed."""
    killer = killer or _kill_one
    killed: list[int] = []
    for t in targets:
        pid = t.get("pid")
        expected = t.get("start")
        if pid is None:
            continue
        live = start_fn(pid)
        if live is None or expected is None or not _start_matches(live, expected):
            continue                       # gone / reused / unconfirmable -> never kill
        if killer(pid):
            killed.append(pid)
    return killed


# --------------------------------------------------------------- daemon controller

class TurnWatchdog:
    """A daemon thread that watches ONE per-turn child. It polls only NEAR the deadline
    (create-time gives exact descendant age, so polling the whole turn is unnecessary -
    gate condition), fires at most once, and exposes a structured ``result``. ``stop()`` +
    ``join()`` are race-free against a normal turn completion (the thread exits its wait
    immediately on the stop event and never kills after stop)."""

    def __init__(self, *, root_pid: int, root_start: object, cfg: TurnWatchdogConfig,
                 snapshot_fn: Callable[[], Snapshot | None] | None = None,
                 kill_fn: Callable[[list[int]], list[int]] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], float] = time.time) -> None:
        self._root_pid = root_pid
        self._root_start = root_start
        self._cfg = cfg
        self._snapshot_fn = snapshot_fn or snapshot_processes
        self._kill_fn = kill_fn or kill_targets
        self._clock = clock
        self._wall = wall_clock
        self._stop = threading.Event()
        self._first_seen: dict[int, float] = {}
        self.result: dict | None = None
        self._thread = threading.Thread(target=self._run, name="turn-watchdog", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def join(self, timeout: float | None = None) -> None:
        if self._thread.is_alive():
            self._thread.join(timeout)

    def _run(self) -> None:
        start = self._clock()
        # Sleep in ONE interruptible wait until ~poll_seconds before the deadline, so a
        # normal (short) turn never snapshots the OS at all - it just stops + joins.
        lead = self._cfg.turn_elapsed_seconds - self._cfg.poll_seconds
        if lead > 0 and self._stop.wait(lead):
            return                                   # turn finished before the deadline
        while not self._stop.is_set():
            elapsed = self._clock() - start
            snap = self._snapshot_fn()
            now = self._wall()
            if snap:
                for pid in snap:
                    self._first_seen.setdefault(pid, now)
            decision = evaluate(snap, root_pid=self._root_pid, root_start=self._root_start,
                                elapsed=elapsed, now=now, cfg=self._cfg,
                                first_seen=self._first_seen)
            if decision.fire and not self._stop.is_set():
                self._fire(decision, elapsed)
                return
            if self._stop.wait(self._cfg.poll_seconds):
                return

    def _fire(self, decision: WatchdogDecision, elapsed: float) -> None:
        killed: list[int] = []
        kill_error = None
        try:
            killed = self._kill_fn(decision.kill_order)
        except Exception as e:  # noqa: BLE001 - a kill failure must not crash the daemon
            kill_error = str(e)
        self.result = {
            "fired": True,
            "root_pid": self._root_pid,
            "root_start": self._root_start,
            "trigger": decision.trigger,
            "elapsed": elapsed,
            "kill_order": decision.kill_order,
            "killed": killed,
            "kill_error": kill_error,
            "summary": _fire_summary(decision, elapsed),
        }


def _fire_summary(decision: WatchdogDecision, elapsed: float) -> str:
    t = decision.trigger or {}
    return (f"turn watchdog killed hung tool descendant: name={t.get('name')} "
            f"pid={t.get('pid')} age={float(t.get('age', 0)):.0f}s elapsed={elapsed:.0f}s")


def resolve_turn_watchdog(config: dict, cfg_agent: dict, *,
                          default_enabled: bool = False) -> TurnWatchdogConfig:
    """PURE per-agent watchdog config (per-agent ``turn_watchdog`` block wins, else the
    global block, else defaults). ``default_enabled`` is the fallback for ``enabled`` when
    NO config explicitly sets it - cmd_wrap passes True for a continuous wrapped-codex loop
    (default-ON) and False elsewhere. Mirrors :func:`supervisor.resolve_dead_letter_caps`'s
    corrupt-config tolerance: a truthy non-dict config/per-agent entry must never crash
    resolution (which would take down ``wrap --loop`` startup)."""
    config = config if isinstance(config, dict) else {}
    cfg_agent = cfg_agent if isinstance(cfg_agent, dict) else {}
    gcfg = config.get("turn_watchdog")
    gcfg = gcfg if isinstance(gcfg, dict) else {}
    acfg = cfg_agent.get("turn_watchdog")
    acfg = acfg if isinstance(acfg, dict) else {}
    d = TurnWatchdogConfig()

    def _num(key: str, default: float) -> float:
        for src in (acfg, gcfg):
            v = src.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
                return float(v)
        return default

    def _flag(key: str, default: bool) -> bool:
        for src in (acfg, gcfg):
            v = src.get(key)
            if isinstance(v, bool):
                return v
        return default

    return TurnWatchdogConfig(
        enabled=_flag("enabled", default_enabled),
        turn_elapsed_seconds=_num("turn_elapsed_seconds", d.turn_elapsed_seconds),
        tool_descendant_alive_seconds=_num(
            "tool_descendant_alive_seconds", d.tool_descendant_alive_seconds),
        poll_seconds=_num("poll_seconds", d.poll_seconds),
        allow_low_turn_elapsed=_flag("allow_low_turn_elapsed", d.allow_low_turn_elapsed),
    )
