"""Bounded in-turn work heartbeat for a wrapped CLI turn (used by ``run.py``
``_ProcStream``, beside the per-turn watchdog).

The wrapped-Claude false-STUCK kill: during a long NON-STREAMING turn the wrapper's
idle heartbeat is blocked (the loop is inside ``drive``) and the framework heartbeat
stamps only on streaming PROGRESS events, so a wrapped Claude with
``stuck_after_seconds=180`` can be STUCK_RECOVERed by the supervisor mid-turn during
legitimate >180s silent work. The fix is a small daemon TICKER that stamps the SAME
supervisor heartbeat while the per-turn child is demonstrably alive - BOUNDED by
``max_turn_seconds`` so a genuinely hung child is never masked forever:

  * immediate first stamp at turn start, then one stamp every ``interval_seconds``
    while the child process is alive AND the turn age is within ``max_turn_seconds``;
  * after the cap the ticker STOPS PERMANENTLY for that turn - only real streaming
    progress (and the turn-boundary success stamp) may refresh liveness, and the
    supervisor's existing stale-heartbeat recovery remains the recovery path (the
    worst-case silent-hang recovery is ``max_turn_seconds + stuck_after_seconds``);
  * NO new kill path, NO health writes (a ticker-fresh ``working_silent`` would feed
    the planner's health-delays-recovery branch and stretch the recovery bound), NO
    WrapperEngine/progress coupling.

HARD no-stamp-after-stop invariant (release blocker): drive()'s failed-turn cleanup
(``store.clear_heartbeat``) runs after the stream is exhausted, so a ticker stamp
landing after that clear would leave a FAILED turn with a fresh heartbeat - the exact
state the clear exists to prevent. ONE lock guards both stamp execution and the stop
flag: ``stop()`` sets the flag and then takes the lock, synchronizing with any
in-flight stamp, so after ``stop()`` returns no further stamp can execute even if the
thread outlives a bounded ``join``.

Semantics note (recorded in docs/DESIGN.md): with the ticker live, a fresh heartbeat
during a turn means "wrapper process + per-turn child alive", not "turn observably
progressing". A wedged stream reader with a live child is masked only until the cap.

V1 scope: default-ON only for a wrapped CLAUDE continuous loop and managed lead-loop.
Wrapped Codex stays default-OFF (its 2400s ``stuck_after`` / watchdog-preemption math
is untouched). One-shot stays default-OFF: the ephemeral one-shot lifecycle is
deadline/completion/process-liveness driven (``supervisor._plan_ephemeral_active``),
with NO stale-heartbeat consumer - a one-shot ticker would be dead liveness writes.
The diagnostics status record is NOT a supervisor input in v1.
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Callable

# Modes as spelled by cmd_wrap/_wrap_loop_mode (the health-writer mode strings).
MODE_LOOP = "wrapper-loop"
MODE_ONE_SHOT = "wrapper-one-shot"
MODE_LEAD_LOOP = "lead-loop"

# The interval must sit comfortably below the resolved supervisor stale threshold:
# above min(60, stuck_after / 3) a single missed/slow tick could straddle the stale
# window. Rejected at wrapper startup (never silently coerced) unless the operator
# opts in with ``allow_high_interval=true``.
INTERVAL_CEILING_SECONDS = 60.0
INTERVAL_STUCK_AFTER_DIVISOR = 3.0

# Ticker stop reasons (the diagnostics status record's ``stopped`` field).
STOPPED_TURN_END = "stopped_turn_end"        # external stop(): the stream ended
STOPPED_CAP = "stopped_cap"                  # max_turn_seconds reached (no-masking bound)
STOPPED_CHILD_EXIT = "stopped_child_exit"    # per-turn child no longer alive
STOPPED_LOST_LEASE = "lost_lease"            # typed lease-loss from the combined stamp


@dataclass(frozen=True)
class WorkHeartbeatConfig:
    """Resolved per-agent work-heartbeat knobs. ``config_errors`` carries the
    validation failures of EXPLICIT config values (never silently coerced): an
    ENABLED config with errors must be rejected visibly at wrapper startup, and
    :func:`work_heartbeat_effectively_live` is False for it."""

    enabled: bool = False
    interval_seconds: float = 30.0
    max_turn_seconds: float = 900.0
    allow_high_interval: bool = False
    config_errors: tuple[str, ...] = ()


def default_work_heartbeat_enabled(cli: str, mode: str) -> bool:
    """V1 default-enable policy: wrapped CLAUDE, continuous loop + managed lead-loop
    only. Codex default-OFF (2400s stuck_after / watchdog math untouched); one-shot
    default-OFF (no supervisor stale-heartbeat consumer for ephemerals - verified
    against ``supervisor._plan_ephemeral_active``, which acts on deadline_epoch /
    typed completion / process liveness, never heartbeat age)."""
    return cli == "claude" and mode in (MODE_LOOP, MODE_LEAD_LOOP)


def resolve_work_heartbeat(config: dict, cfg_agent: dict, *, cli: str = "claude",
                           mode: str = MODE_LOOP) -> WorkHeartbeatConfig:
    """PURE per-agent work-heartbeat config (per-agent ``work_heartbeat`` block wins,
    else the global block, else defaults). Mirrors :func:`turn_watchdog.
    resolve_turn_watchdog`'s corrupt-config tolerance (a truthy non-dict config /
    per-agent entry / block never crashes resolution) with ONE deliberate difference:
    an EXPLICIT invalid value (non-numeric or non-positive) is recorded in
    ``config_errors`` instead of being silently dropped to the default - the guards
    reject an enabled-but-invalid config visibly at startup (never coerce)."""
    config = config if isinstance(config, dict) else {}
    cfg_agent = cfg_agent if isinstance(cfg_agent, dict) else {}
    gcfg = config.get("work_heartbeat")
    gcfg = gcfg if isinstance(gcfg, dict) else {}
    acfg = cfg_agent.get("work_heartbeat")
    acfg = acfg if isinstance(acfg, dict) else {}
    d = WorkHeartbeatConfig()
    errors: list[str] = []

    def _num(key: str, default: float) -> float:
        # First source (per-agent, then global) that CARRIES the key wins - an
        # explicit invalid value is an ERROR, never a silent fall-through.
        for src in (acfg, gcfg):
            if key not in src:
                continue
            v = src.get(key)
            if (
                isinstance(v, (int, float))
                and not isinstance(v, bool)
                and math.isfinite(float(v))
                and v > 0
            ):
                return float(v)
            errors.append(
                f"work_heartbeat.{key} must be a finite number > 0 (got {v!r})"
            )
            return default
        return default

    def _flag(key: str, default: bool) -> bool:
        for src in (acfg, gcfg):
            v = src.get(key)
            if isinstance(v, bool):
                return v
        return default

    return WorkHeartbeatConfig(
        enabled=_flag("enabled", default_work_heartbeat_enabled(cli, mode)),
        interval_seconds=_num("interval_seconds", d.interval_seconds),
        max_turn_seconds=_num("max_turn_seconds", d.max_turn_seconds),
        allow_high_interval=_flag("allow_high_interval", d.allow_high_interval),
        config_errors=tuple(errors),
    )


def interval_bound_seconds(stuck_after_seconds: float) -> float:
    """The maximum safe ticker interval for a resolved stale threshold."""
    return min(INTERVAL_CEILING_SECONDS,
               float(stuck_after_seconds) / INTERVAL_STUCK_AFTER_DIVISOR)


def interval_violation(cfg: WorkHeartbeatConfig, *,
                       stuck_after_seconds: float) -> str | None:
    """The interval-vs-stale-threshold guard (PURE). Returns a human diagnostic when
    an ENABLED config's interval exceeds :func:`interval_bound_seconds` for the
    agent's RESOLVED ``stuck_after_seconds`` (the same value the supervisor planner
    uses), or None when acceptable / disabled / explicitly overridden."""
    if not cfg.enabled or cfg.allow_high_interval:
        return None
    bound = interval_bound_seconds(stuck_after_seconds)
    if cfg.interval_seconds > bound:
        return (f"work_heartbeat.interval_seconds={cfg.interval_seconds:g}s exceeds "
                f"min({INTERVAL_CEILING_SECONDS:g}, stuck_after_seconds/"
                f"{INTERVAL_STUCK_AFTER_DIVISOR:g}) = {bound:g}s for "
                f"stuck_after_seconds={stuck_after_seconds:g}s; lower the interval or "
                f"set allow_high_interval=true to opt in")
    return None


def work_heartbeat_effectively_live(cfg: WorkHeartbeatConfig) -> bool:
    """The shared "will the ticker actually run for this resolved config" predicate.
    NOT a supervisor planner input in v1 (diagnostics/doctor + cmd_wrap only): the
    planner's stale-recovery math is deliberately unchanged, so relaxing any guard on
    an assumed-live ticker (config-says vs runtime-does drift) is a future, separately
    gated change."""
    return bool(cfg.enabled) and not cfg.config_errors


class WorkHeartbeatTicker:
    """A daemon thread that stamps the supervisor heartbeat while ONE per-turn child
    is alive, bounded by ``max_turn_seconds``. NON-KILLING and fail-safe: an ordinary
    stamp exception is counted + recorded and ticking continues (a transient store
    hiccup must not stop in-turn liveness); an exception whose type is listed in
    ``lease_lost_exceptions`` (the lead-loop's typed lease-loss from the combined
    renew-then-stamp callable) stops stamping PERMANENTLY for the turn - a controller
    that lost mailbox ownership must not keep advertising bus liveness, and the
    ``pre_commit`` ownership gate remains the authority at the consume boundary.

    ``stop()`` is the HARD no-stamp-after-stop boundary: it sets the stop flag and
    then takes the stamp lock, so any in-flight stamp completes BEFORE stop()
    returns and no later stamp can start (the loop re-checks the flag under the same
    lock immediately before every stamp). ``on_status`` (best-effort diagnostics
    persistence) is called at start and on every terminal transition; it must never
    raise into the ticker."""

    def __init__(self, *, cfg: WorkHeartbeatConfig, stamp: Callable[[], None],
                 child_alive: Callable[[], bool],
                 clock: Callable[[], float] = time.monotonic,
                 wait: Callable[[float], bool] | None = None,
                 lease_lost_exceptions: tuple = (),
                 on_status: Callable[[dict], None] | None = None) -> None:
        self._cfg = cfg
        self._stamp = stamp
        self._child_alive = child_alive
        self._clock = clock
        self._stop = threading.Event()
        # ``wait`` is injectable for deterministic tests; the default is the stop
        # event's own wait (True = stop requested, False = interval elapsed).
        self._wait = wait if wait is not None else self._stop.wait
        self._lease_lost = tuple(lease_lost_exceptions)
        self._on_status = on_status
        self._lock = threading.Lock()      # guards stamp execution + stop state
        self._started_at: float | None = None
        self._thread = threading.Thread(
            target=self._run, name="work-heartbeat", daemon=True)
        self.result: dict = {
            "enabled": True,
            "interval_seconds": cfg.interval_seconds,
            "max_turn_seconds": cfg.max_turn_seconds,
            "stamps": 0,
            "stamp_errors": 0,
            "last_error": None,
            "stopped": None,
        }

    def start(self) -> None:
        self._started_at = self._clock()
        self._publish_status()
        self._thread.start()

    def stop(self) -> None:
        """Set the stop flag, then synchronize with any in-flight stamp (take the
        stamp lock). After this returns, no further stamp can execute - even if the
        thread outlives a bounded join (the loop re-checks the flag under the lock)."""
        self._stop.set()
        with self._lock:
            if self.result["stopped"] is None:
                self.result["stopped"] = STOPPED_TURN_END
        self._publish_status()

    def join(self, timeout: float | None = None) -> None:
        if self._thread.is_alive():
            self._thread.join(timeout)

    # ------------------------------------------------------------------ internals

    def _publish_status(self) -> None:
        if self._on_status is None:
            return
        try:
            self._on_status(dict(self.result))
        except Exception:  # noqa: BLE001, S110 - diagnostics never crash the ticker  # nosec B110
            pass

    def _finish(self, reason: str) -> None:
        with self._lock:
            if self.result["stopped"] is None:
                self.result["stopped"] = reason
        self._publish_status()

    def _try_stamp(self) -> bool:
        """Stamp under the lock. False = stop ticking (stopped externally, or a typed
        lease-loss made stamping permanently wrong for this turn)."""
        with self._lock:
            if self._stop.is_set():
                return False
            try:
                self._stamp()
            except self._lease_lost as e:
                # typed loss of ownership: permanently stop (never advertise bus
                # liveness without the lease); the consume-boundary pre_commit gate
                # raises the loss to the loop. NO health write, NO child kill.
                self._stop.set()
                self.result["stopped"] = STOPPED_LOST_LEASE
                self.result["last_error"] = f"{type(e).__name__}: {e}"
                self._publish_status()
                return False
            except Exception as e:  # noqa: BLE001 - a stamp error must not kill liveness
                self.result["stamp_errors"] += 1
                self.result["last_error"] = f"{type(e).__name__}: {e}"
                self._publish_status()
                return True
            self.result["stamps"] += 1
            return True

    def _run(self) -> None:
        start = self._started_at if self._started_at is not None else self._clock()
        if not self._try_stamp():            # immediate first stamp at turn start
            return
        while True:
            if self._wait(self._cfg.interval_seconds):
                self._finish(STOPPED_TURN_END)
                return
            if self._clock() - start >= self._cfg.max_turn_seconds:
                # the no-masking bound: past the cap only real progress / the
                # turn-boundary stamp may refresh; supervisor stale recovery applies.
                self._finish(STOPPED_CAP)
                return
            if not self._child_alive():
                self._finish(STOPPED_CHILD_EXIT)
                return
            if not self._try_stamp():
                return
