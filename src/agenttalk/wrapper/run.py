"""The wrapper's I/O shell: launch a CLI in structured-stream mode, read its
stdout, parse + adapt each line to normalized events, and drive the engine
(heartbeat / render / degraded). This is the ONLY wrapper module that touches a
subprocess, the console, or the store - everything else is pure and unit-tested
without it.

Testability: the stream SOURCE and all three sinks are injectable. Tests pass a
fake line iterator (no subprocess) and fake sinks; production defaults spawn the
real CLI and wire heartbeat -> store.write_heartbeat and degraded-escalation ->
a restart-request marker.
"""

from __future__ import annotations

import json
import os
import re
# subprocess spawns ONLY the operator-provided CLI launch command; never shell=True.
import subprocess  # nosec B404
import sys
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .turn_watchdog import TurnWatchdogConfig
from datetime import datetime, timezone

from agenttalk import health as health_model
from agenttalk.store import LEAD_LOOP_LEASE_ENV

from . import claude_adapter, codex_adapter
from .degraded import DegradedConfig, DegradedDetector
from .events import Event, EventType
from .framework import WrapperEngine
from .health import WrapperHealthWriter

# cli -> event mapper.
_ADAPTERS: dict[str, Callable[[object], list[Event]]] = {
    "codex": codex_adapter.map_event,
    "claude": claude_adapter.map_event,
}
# Codex has no observed real tool-call-leak signature yet -> detect + log, never
# escalate. Claude's leak (tool-call markup as assistant text) is the cataloged
# high-confidence signature -> escalation-enabled.
_TELEMETRY_ONLY = {"codex": True, "claude": False}

# Known GLOBAL/INFRA signatures in a terminal turn-failure error message. A terminal
# turn.failed / is_error carrying one of these is an OUTAGE (overload, rate-limit, 5xx,
# auth, network, edge/WAF/proxy block), NOT message-poison - so it must NOT auto-dead-letter
# at the low poison cap (lead verify P1/C4). Erring toward INFRA is the SAFE direction: a
# misclassified poison just keeps retrying (with escalation at the ceiling), never false-DLs
# a healthy one during an outage. NOTE: bare "blocked" is DELIBERATELY excluded - it would
# shadow the content-policy poison markers ("blocked by safety" / "blocked by content
# policy"), which must stay POISON; the edge tokens below catch gateway/WAF blocks without it.
_INFRA_ERROR_MARKERS = (
    "rate limit", "rate_limit", "ratelimit", "too many requests", "429", "529",
    "overloaded", "quota", "usage limit", "capacity",
    "timeout", "timed out", "deadline exceeded",
    "unavailable", "service unavailable", "temporarily", "try again",
    "internal server error", "bad gateway", "gateway timeout", " 500", " 502", " 503",
    " 504", "http 5", "5xx",
    "connection", "network", "econn", "reset by peer", "broken pipe",
    "unauthorized", "authentication", "invalid api key", "api key", "credential",
    "401", "403",
    # edge / WAF / reverse-proxy block (lead C4(b)): a gateway/proxy/firewall block is an
    # outage signature, not message-content - err toward INFRA (retry + escalate, never DL@3).
    "forbidden", "waf", "firewall", "proxy", "upstream",
    # token/quota RATE windows (lead 4th-verify P1 #2 + codex expansion): "maximum number of
    # tokens per minute exceeded" / "...per day" / "TPM" is a rate/quota LIMIT (outage), NOT
    # this message being too big - infra-first so it never false-DLs a healthy message during a
    # rate window. ("quota" + "usage limit" are already infra markers above.)
    "per minute", "per day", "per hour", "tokens per", "tpm", "daily token",
)


def _looks_like_infra(text: str | None) -> bool:
    """True if a terminal error message reads as a GLOBAL/INFRA outage (vs message-poison)."""
    t = (text or "").lower()
    return any(m in t for m in _INFRA_ERROR_MARKERS)


# POSITIVE, message-CONTENT-attributable poison signatures (codex marker-conservatism
# ruling). A terminal failure is low-cap poison@3 ONLY with explicit evidence THIS MESSAGE's
# content is the deterministic cause (re-delivering it fails identically while OTHER messages
# succeed). Kept NARROW + unambiguous via TWO direct families + TWO qualified tokens; EVERY
# generic/global string (invalid request, malformed, 422, bare "too long", bare "violates")
# defaults to AMBIGUOUS, never poison - they also fire for GLOBAL config/outage/rate-limit
# problems that are NOT message-poison (recurring marker-breadth class, 3rd instance).
_POISON_SIZE_MARKERS = (             # DIRECT: THIS message is too large (others fit)
    "context length", "context window", "maximum context", "context_length",
    "input too large", "message too large", "request too large", "payload too large",
    # The descriptive 413 phrasings stay DIRECT (they are inherently request-size-qualified);
    # bare "413" is handled by the QUALIFIED clause below, never as a raw substring (codex).
    "request entity too large",
    # NOTE: bare "maximum number of tokens" was REMOVED (lead 4th-verify P1 #2): it also
    # matches TPM/daily RATE-LIMIT phrasings ("maximum number of tokens per minute") which
    # are outages, not poison; real context-overflow is covered by "context length"/"context
    # window"/"maximum context"/"prompt too long". Token-rate phrasings are now infra-first.
    "prompt is too long", "prompt too long",
)
# A bare "413" SUBSTRING is a collision risk (e.g. "8413 bytes" / "code 4130"), so 413 is
# poison ONLY when it is a STANDALONE numeric TOKEN (\b413\b - never inside 8413/4130; reviewer-1
# blocker) AND QUALIFIED by an HTTP/status/request-entity/payload context (codex 4th-verify).
_POISON_413_RE = re.compile(r"\b413\b")
_POISON_413_QUALIFIERS = ("http", "status", "request entity", "payload", "entity too large")
_POISON_POLICY_MARKERS = (           # DIRECT: THIS message's content is disallowed
    "content policy", "content filter", "content_filter", "safety policy",
    "blocked by safety", "blocked by content policy",
)
# QUALIFIED: "violates"/"flagged by" are poison ONLY with a content/policy/safety/filter
# qualifier - bare "violates the rate limit" / "flagged by the gateway" are NOT message-poison.
_POISON_CONTENT_QUALIFIERS = ("content", "policy", "safety", "filter")
# QUALIFIED: "exceeds the maximum" is poison ONLY with a content-SIZE object - bare
# "exceeds the maximum retries" is infra/ambiguous, never poison.
_POISON_SIZE_QUALIFIERS = ("token", "context", "input", "prompt", "message", "request",
                           "payload")


def _looks_like_content_poison(text: str | None) -> bool:
    """True iff a terminal error message is EXPLICIT, message-content-attributable poison:
    a DIRECT size/policy signature, or a QUALIFIED 'violates'/'flagged by'/'exceeds the
    maximum' (their bare forms are too broad and stay AMBIGUOUS). codex ruling."""
    t = (text or "").lower()
    if any(m in t for m in _POISON_SIZE_MARKERS):
        return True
    if any(m in t for m in _POISON_POLICY_MARKERS):
        return True
    if ("violates" in t or "flagged by" in t) and any(q in t for q in _POISON_CONTENT_QUALIFIERS):
        return True
    if "exceeds the maximum" in t and any(q in t for q in _POISON_SIZE_QUALIFIERS):
        return True
    if _POISON_413_RE.search(t) and any(q in t for q in _POISON_413_QUALIFIERS):
        return True
    return False


def parse_lines(lines: Iterable[str], mapper: Callable[[object], list[Event]]) -> Iterator[Event]:
    """Parse a raw line stream into normalized events. Blank lines and lines that
    are not valid JSON are skipped (a real stream interleaves non-JSON noise)."""
    for line in lines:
        s = line.strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
        except (ValueError, TypeError):
            continue
        for ev in mapper(obj):
            yield ev


def _default_heartbeat(store, agent: str) -> Callable[[Event, float], None]:
    def stamp(_event: Event, _now: float) -> None:
        store.write_heartbeat(agent)
    return stamp


def _default_escalate(store, agent: str, sender: str) -> Callable[[object], None]:
    def escalate(signal) -> None:
        # Dedicated degraded restart path - NOT force-expiring the heartbeat, so
        # no-progress (stale) and bad-progress (degraded) stay distinct/diagnosable.
        store.write_restart_request(agent, {
            "agent": agent,
            "request_id": "rr-" + uuid.uuid4().hex[:12],
            "source": "degraded",
            "requested_by": sender,
            "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "at_epoch": time.time(),
            "force_protected": False,
            "reason": signal.reason,
        })
    return escalate


def _default_render(event: Event) -> None:
    t = event.type.value
    if event.tool:
        sys.stdout.write(f"[{t}] {event.tool}\n")
    elif event.text:
        sys.stdout.write(f"[{t}] {event.text}\n")
    else:
        sys.stdout.write(f"[{t}]\n")
    sys.stdout.flush()


def _default_info(signal) -> None:
    sys.stderr.write(f"[degraded:{signal.confidence}] {signal.reason}\n")
    sys.stderr.flush()


def run_wrapper(
    *,
    cli: str,
    agent: str,
    argv: list[str],
    store=None,
    sender: str | None = None,
    min_interval: float = 5.0,
    render: bool = True,
    degraded_config: DegradedConfig | None = None,
    line_source: Iterable[str] | None = None,
    heartbeat_fn: Callable[[Event, float], None] | None = None,
    restart_fn: Callable[[object], None] | None = None,
    render_fn: Callable[[Event], None] | None = None,
    info_fn: Callable[[object], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Wrap one CLI run. Returns the child exit code (0 when a line_source is
    injected, i.e. tests)."""
    mapper = _ADAPTERS.get(cli)
    if mapper is None:
        raise ValueError(f"no wrapper adapter for cli {cli!r} (Phase 1 = codex)")
    cfg = degraded_config or DegradedConfig(
        telemetry_only=_TELEMETRY_ONLY.get(cli, False)
    )
    detector = DegradedDetector(cli, cfg)
    health_writer = (
        WrapperHealthWriter(store, agent, cli, mode="wrapper-one-shot",
                            min_interval=min_interval)
        if store is not None else None
    )

    if heartbeat_fn is None:
        heartbeat_fn = _default_heartbeat(store, agent)
    if restart_fn is None:
        restart_fn = _default_escalate(store, agent, sender or agent)
    if render_fn is None:
        render_fn = _default_render if render else None
    if info_fn is None:
        info_fn = _default_info

    def _restart(signal) -> None:
        if health_writer is not None:
            health_writer.degraded(signal)
        restart_fn(signal)

    def _info(signal) -> None:
        if health_writer is not None:
            health_writer.degraded(signal)
        info_fn(signal)

    engine = WrapperEngine(
        detector=detector,
        on_heartbeat=heartbeat_fn,
        on_render=render_fn,
        on_escalate=_restart,
        on_info=_info,
        min_interval=min_interval,
    )

    def _health_events(events: Iterable[Event]) -> Iterator[Event]:
        for ev in events:
            if health_writer is not None:
                health_writer.event(ev)
            yield ev

    if line_source is not None:
        if health_writer is not None:
            health_writer.turn_start(None)
        engine.run(_health_events(parse_lines(line_source, mapper)), clock)
        if (health_writer is not None
                and health_writer.state != health_model.STATE_DEGRADED_OUTPUT):
            health_writer.idle(reason_code="wrapper_completed")
        return 0

    # argv is the operator-provided launch command; never shell=True.
    # encoding/errors are EXPLICIT: codex/claude emit UTF-8, but text=True alone
    # decodes child stdout via the platform default (cp1252 on Windows) and CRASHES
    # on the first non-ASCII byte (e.g. a smart quote). errors="replace" means a
    # genuinely malformed byte renders a replacement char instead of killing the
    # wrapper. In text mode this also governs the stdin prompt write.
    # Strip the lead-loop owner-bypass token from the child env here too (parity with
    # _ProcStream), so "the model child never sees the token" holds on EVERY spawn path,
    # not only the loop path (defense-in-depth + comment accuracy).
    child_env = {k: v for k, v in os.environ.items() if k != LEAD_LOOP_LEASE_ENV}
    proc = subprocess.Popen(  # noqa: S603  # nosec B603
        argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace", bufsize=1, env=child_env,
    )
    try:
        if health_writer is not None:
            health_writer.turn_start(None)
        engine.run(_health_events(parse_lines(proc.stdout or [], mapper)), clock)
    finally:
        if proc.stdout:
            proc.stdout.close()
    rc = proc.wait()
    if health_writer is not None:
        if rc == 0:
            if health_writer.state != health_model.STATE_DEGRADED_OUTPUT:
                health_writer.idle(reason_code="wrapper_completed")
        else:
            health_writer.crashed_or_exited(reason_code="wrapper_child_nonzero_exit")
    return rc


class _ProcStream:
    """Default make_drive spawner: spawn the CLI for one turn (prompt on STDIN, to
    dodge quoting), yield its stdout lines, and expose the child EXIT CODE as
    ``.returncode`` once the stream is exhausted - so the drive path can tell a
    clean completed turn from a partial stream that died with a bad exit. Tests
    inject their own spawner (a plain list = unknown exit, an object with
    ``.returncode`` = a controlled exit)."""

    def __init__(self, argv: list[str], stdin_text: str | None, *,
                 watchdog: "TurnWatchdogConfig | None" = None,
                 watchdog_snapshot_fn=None, watchdog_kill_fn=None,
                 watchdog_clock=time.monotonic, watchdog_wall_clock=time.time) -> None:
        # argv is the operator-provided launch command; never shell=True.
        # Explicit encoding/errors (see run_wrapper): UTF-8 child output must not be
        # decoded as cp1252 on Windows (crash on the first non-ASCII byte), and a
        # malformed byte must be replaced, not fatal. Governs the stdin prompt too.
        # STRIP the lead-loop owner-bypass token from the child env (WP2 condition 3 /
        # reviewer-1 Slice-1 residual): the wrapper consumes the bus IN-PROCESS, so the
        # model child never needs AGENTTALK_LEAD_LOOP_LEASE - and leaking it would let
        # an accidental model-side `agenttalk drain` bypass the single-consumer guard.
        # Always stripped (harmless for non-lead-loop children; defense-in-depth even
        # if a future parent sets it). The child otherwise inherits the parent env.
        child_env = {k: v for k, v in os.environ.items() if k != LEAD_LOOP_LEASE_ENV}
        self._proc = subprocess.Popen(  # noqa: S603  # nosec B603
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
            bufsize=1, env=child_env,
        )
        # Record the per-turn root start-time right after spawn (~ the OS-reported create
        # time, within spawn jitter) so the watchdog can start-time-guard the kill against
        # pid reuse. Wall clock, to match the snapshot adapter's create_epoch.
        self._root_start = watchdog_wall_clock()
        if self._proc.stdin is not None:
            if stdin_text is not None:
                self._proc.stdin.write(stdin_text)
            self._proc.stdin.close()
        self.returncode: int | None = None
        # Per-turn watchdog (off unless an ENABLED config is passed). A structured result
        # lands on .watchdog_result iff it fires (the kill closes stdout -> __iter__ ends).
        self.watchdog_result: dict | None = None
        self._watchdog = None
        if watchdog is not None and watchdog.enabled:
            from .turn_watchdog import TurnWatchdog
            self._watchdog = TurnWatchdog(
                root_pid=self._proc.pid, root_start=self._root_start, cfg=watchdog,
                snapshot_fn=watchdog_snapshot_fn, kill_fn=watchdog_kill_fn,
                clock=watchdog_clock, wall_clock=watchdog_wall_clock)
            self._watchdog.start()

    def __iter__(self) -> Iterator[str]:
        try:
            yield from (self._proc.stdout or [])
        finally:
            if self._proc.stdout:
                self._proc.stdout.close()
            self.returncode = self._proc.wait()
            # Stop + join the watchdog AFTER the child has exited: a normal turn completion
            # makes the thread exit its wait immediately (no kill race), and a watchdog that
            # already fired published its result before we read it here.
            if self._watchdog is not None:
                self._watchdog.stop()
                self._watchdog.join(timeout=10.0)
                self.watchdog_result = self._watchdog.result


def make_drive(store, agent: str, cli: str, session_state, base_argv: list[str], *,
               sender: str | None = None, min_interval: float = 5.0,
               render: bool = True, rules: str | None = None,
               clock: Callable[[], float] = time.monotonic,
               spawn: Callable[[list[str], str | None], Iterable[str]] | None = None,
               heartbeat: Callable[[], None] | None = None,
               persist: Callable[[object], None] | None = None,
               turn_watchdog: "TurnWatchdogConfig | None" = None,
               watchdog_snapshot_fn=None, watchdog_kill_fn=None,
               health_writer: WrapperHealthWriter | None = None) -> Callable[[dict], object]:
    """Build the per-turn ``drive(record)`` callback for loop.run_loop. Each call
    drives ONE real CLI turn and returns a :class:`loop.DriveOutcome` (ok + a failure
    CLASS on failure, for dead-letter). A turn fails if it produced no progress event or
    hit a TERMINAL (non-retryable) adapter_error (turn.failed, a nonzero-no-JSON child, a
    resume no-session). On a failed CODEX RESUME turn it marks resume unavailable,
    persists, and retries FRESH (exec --json) for the SAME record before returning;
    run_loop only commits the inbound message when the outcome is ok, so a failed turn is
    never lost (at-least-once).

    FAILURE CLASSIFICATION (dead-letter taxonomy) - see :func:`_classify`:
      * ``poison_eligible`` (low cap): a terminal turn-failure (turn.failed / is_error) whose
        error text POSITIVELY matches a message-CONTENT signature (the size family or the
        content-policy family) - explicit evidence THIS message's content is the cause. A
        terminal that is merely "not infra" is NOT poison; it is ambiguous (see below).
      * ``known_global_infra`` (never auto-DL): a spawn/exec OS error; a terminal failure
        whose text matches a global-outage signature (overloaded/rate-limit/5xx/auth/edge
        block/...); or a retryable transport error with no turn start.
      * ``ambiguous_or_unknown`` (high ceiling): an UNRECOGNIZED terminal cause (neither
        content nor infra), a partial stream / nonzero exit after the turn STARTED (could be
        poison OR an infra drop after the handshake), crash-mid-turn, or no start with no
        clear signal - the misclassified-poison escape hatch.

    The detector + engine are created ONCE and reused across turns, so the degraded
    confirmation window (which counts degraded turns across CLI invocations) and the
    heartbeat throttle persist. ``spawn`` is injectable for tests (no real
    subprocess); ``persist`` is called after each turn to save session state."""
    from . import prompt as _prompt
    from . import session as _session
    from .loop import CLASS_AMBIGUOUS, CLASS_INFRA, CLASS_POISON, DriveOutcome

    mapper = _ADAPTERS.get(cli)
    if mapper is None:
        raise ValueError(f"no wrapper adapter for cli {cli!r}")
    cfg = DegradedConfig(telemetry_only=_TELEMETRY_ONLY.get(cli, False))
    detector = DegradedDetector(cli, cfg)
    if health_writer is None:
        health_writer = WrapperHealthWriter(
            store, agent, cli, mode="wrapper-loop", min_interval=min_interval)
    restart = _default_escalate(store, agent, sender or agent)

    def _health_escalate(signal) -> None:
        health_writer.degraded(signal)
        restart(signal)

    def _health_info(signal) -> None:
        health_writer.degraded(signal)
        _default_info(signal)

    engine = WrapperEngine(
        detector=detector,
        # Stamp heartbeat on streaming progress so a long SUCCESSFUL turn stays live
        # before it completes (in-turn liveness). A FAILED turn may also stamp here
        # mid-stream, so drive() CLEARS the heartbeat when the turn ends failed -
        # net: live during a successful turn, no fresh heartbeat after a failed one
        # (reviewer-1 gate: both, via stamp-during + clear-on-failure). The managed
        # lead-loop injects a combined stamp (write_heartbeat + renew lease) so the
        # lease stays fresh on streaming progress too, not only on the idle stamp
        # (WP2 condition 4); the failure-path heartbeat-clear below does not touch the
        # lease (the controller is alive while streaming, even on a turn that fails).
        on_heartbeat=((lambda _ev, _ts: heartbeat()) if heartbeat is not None
                      else _default_heartbeat(store, agent)),
        on_render=(_default_render if render else None),
        on_escalate=_health_escalate,
        on_info=_health_info,
        min_interval=min_interval,
    )
    if spawn is not None:
        spawner = spawn                     # tests inject their own (argv, stdin) spawner
    else:
        def spawner(argv, stdin_text):
            # Bind the per-turn watchdog onto the real spawner. When turn_watchdog is None
            # or disabled, _ProcStream starts no thread (zero overhead, behavior unchanged).
            return _ProcStream(argv, stdin_text, watchdog=turn_watchdog,
                               watchdog_snapshot_fn=watchdog_snapshot_fn,
                               watchdog_kill_fn=watchdog_kill_fn)

    def _run_one(spec) -> dict:
        """Spawn ONE CLI invocation for ``spec`` and process its JSONL via the engine.
        Returns the raw turn SIGNALS for classification: ``{ok, started, completed,
        terminal, retryable, rc, error}``. ok is True only if it reached a COMPLETED
        boundary (TURN_FINISHED), hit no terminal (non-retryable) adapter_error, AND the
        child exited cleanly. A spawn/exec failure (missing binary, OS error) is captured
        as ``error`` (infra), never raised, so the turn fails GRACEFULLY (no wrapper crash)."""
        argv = list(base_argv) + spec.args
        sig = {"ok": False, "started": False, "completed": False, "terminal": False,
               "retryable": False, "rc": None, "error": None, "terminal_text": ""}
        try:
            stream = spawner(argv, spec.stdin)
            for line in stream:
                s = line.strip()
                if not s:
                    continue
                try:
                    raw = json.loads(s)
                except (ValueError, TypeError):
                    continue
                _session.observe_event(session_state, raw)   # capture codex thread_id
                for ev in mapper(raw):
                    if ev.type == EventType.TURN_STARTED:
                        sig["started"] = True
                    elif ev.type == EventType.TURN_FINISHED:
                        sig["completed"] = True
                    elif ev.type == EventType.ADAPTER_ERROR:
                        if ev.retryable:
                            sig["retryable"] = True
                        else:
                            sig["terminal"] = True
                            # capture the error TEXT: a terminal turn.failed / is_error can
                            # carry an INFRA message (529/rate-limit/5xx/auth), which must NOT
                            # be classed poison (lead verify P1).
                            sig["terminal_text"] = ev.text or sig["terminal_text"]
                    health_writer.event(ev)
                    engine.process(ev, clock())
        except OSError as e:
            # spawn/exec/transport OS error (missing binary, broken pipe, ...): a
            # GLOBAL/infra failure, not poison. Never crash the wrapper.
            sig["error"] = f"spawn/exec error: {e}"
            return sig
        # rc is None for an injected list spawn (unknown -> trust the boundary); a real
        # _ProcStream reports the child exit code (nonzero = failed turn).
        sig["rc"] = getattr(stream, "returncode", None)
        # A fired per-turn watchdog (hung tool descendant killed) is the AUTHORITATIVE
        # cause of this turn's death - it closed the stream, so rc/partial-stream signals
        # are downstream noise. Carry it for _classify to check FIRST.
        sig["watchdog"] = getattr(stream, "watchdog_result", None)
        sig["ok"] = sig["completed"] and not sig["terminal"] and sig["rc"] in (0, None)
        return sig

    def _classify(sig: dict) -> tuple[str, str]:
        """Map failed-turn signals to (failure_class, summary) for the dead-letter
        taxonomy. A terminal turn-failure is low-cap POISON ONLY when its error text
        POSITIVELY matches a message-content signature (size / content-policy); an
        infra-looking terminal error, and any unrecognized/ambiguous partial/nonzero
        outcome, gets the high K_escalate ceiling so a sustained INFRA outage (or an
        unobserved cause) can never false-dead-letter a healthy message (lead verify P1)."""
        wd = sig.get("watchdog")
        if wd:
            # the per-turn watchdog killed a hung tool descendant: AMBIGUOUS (never poison),
            # checked BEFORE rc/terminal noise it caused. The message stays pending and rides
            # the K_escalate ceiling - a persistently-wedging head escalates, never auto-DLs.
            summary = wd.get("summary") if isinstance(wd, dict) else None
            return CLASS_AMBIGUOUS, summary or "turn watchdog killed hung tool descendant"
        if sig.get("error"):                       # spawn/exec OS error (missing binary, ...)
            return CLASS_INFRA, sig["error"]
        if sig.get("terminal"):
            text = sig.get("terminal_text") or ""
            if _looks_like_infra(text):
                # a terminal carrying an infra message (overloaded/rate-limit/5xx/auth/edge
                # block) is a GLOBAL outage, not poison -> never auto-DL. INFRA is checked
                # BEFORE poison ON PURPOSE: if a terminal somehow matched BOTH families, the
                # safe direction is INFRA (retry + escalate, decided by a human) over a low-cap
                # auto-dispose - a misclassified poison just disposes later at the ceiling, a
                # misclassified outage would false-DL a healthy message (fail-closed).
                return CLASS_INFRA, f"terminal infra error: {text[:160]}"
            if _looks_like_content_poison(text):
                # EXPLICIT message-content-attributable failure - the size family (this message
                # is too large) or the content-policy family (this message's content is
                # disallowed) -> deterministic poison fast-path @K_poison.
                return CLASS_POISON, f"terminal content-poison: {text[:160]}"
            # DEFAULT (codex ruling): an unrecognized terminal cause is AMBIGUOUS, not
            # poison - an UNKNOWN-infra terminal must not false-DL at the low cap. It still
            # disposes (bounded) at the high K_escalate ceiling, with escalation.
            return CLASS_AMBIGUOUS, (f"terminal failure, unrecognized cause: {text[:160]}"
                                     if text else "terminal failure, no error text")
        # An EXPLICIT recognized retryable transport error -> INFRA, checked BEFORE the
        # started/partial-stream ambiguity so a transport drop AFTER the handshake (turn
        # STARTED, recognized retryable error, then dropped with no terminal) is NOT shadowed
        # as ambiguous (lead 6th-verify P2). It is dominantly an infra drop after the
        # handshake -> keep retrying/escalating, NEVER auto-dead-letter. Covers both no-start
        # and started-then-dropped; a TERMINAL failure was already classified above by its text.
        if sig.get("retryable"):
            return CLASS_INFRA, ("retryable transport error"
                                 + (" after handshake" if sig.get("started") else ", no start"))
        if sig.get("started"):
            # started, NO terminal, NO recognized retryable signal: a partial stream / nonzero
            # exit is AMBIGUOUS - the start event may be only an API handshake (e.g. claude
            # message_start), so an UNRECOGNIZED drop MID-stream looks identical to
            # message-poison. Treat as ambiguous so a sustained OUTAGE escalates + only
            # dead-letters at the high K_escalate ceiling (never at the low poison cap).
            if not sig.get("completed"):
                return CLASS_AMBIGUOUS, ("partial stream: started, never completed "
                                         "(poison or an unrecognized drop after the handshake)")
            return CLASS_AMBIGUOUS, f"nonzero child exit (rc={sig.get('rc')}) after start"
        # the turn never started and no recognized signal: not attributable to the record.
        return CLASS_AMBIGUOUS, f"turn never started (rc={sig.get('rc')}, no clear signal)"

    def drive(record: dict) -> DriveOutcome:
        prompt = _prompt.assemble_turn_prompt(record, rules=rules)
        spec = _session.build_turn(session_state, prompt)
        cli = session_state.cli
        # A failed RESUME turn self-heals to a fresh session before we classify (codex:
        # exec resume -> fresh exec, run.py history; claude: --resume -> fresh --session-id,
        # codex ruling #2). This is why "prompt too long" on a resume turn is NOT poison: a
        # FULL session is global session-pressure, not message-content - only a fresh-session
        # failure is attributable to the record.
        attempted_resume = ((cli == "codex" and "resume" in spec.args)
                            or (cli == "claude" and "--resume" in spec.args))
        health_writer.turn_start(record)
        sig = _run_one(spec)
        if not sig["ok"] and attempted_resume:
            # the resume turn failed -> force a FRESH session for the SAME record and retry
            # inline (codex: a new thread_id will be observed + persisted; claude: a new
            # session id is minted). The whole drive() (resume -> fresh) is EXACTLY ONE attempt.
            if cli == "claude":
                _session.reset_claude_session(session_state, "resume turn failed")
            else:
                _session.mark_resume_unavailable(session_state, "resume turn failed")
            if persist is not None:
                persist(session_state)
            health_writer.turn_start(record)
            sig = _run_one(_session.build_turn(session_state, prompt))
        if sig["ok"]:
            session_state.turns += 1
            if cli == "claude" and not session_state.resume_available:
                # a FRESH claude session just succeeded -> it is now resumable next turn
                # (codex re-arms via the observed thread.started; claude is minted, so set here).
                # Clear the stale reason too, mirroring observe_event's re-arm (diagnostic
                # hygiene, both reviewers - the field is only surfaced while resume is unavailable).
                session_state.resume_available = True
                session_state.resume_unavailable_reason = ""
            # Unconditional: a clean completed turn ALWAYS ends with a fresh
            # heartbeat, even if the engine's min_interval throttled the in-turn
            # stamps (e.g. a quick retry right after a failure). Route through the
            # INJECTED hook when present (the lead-loop's renew+heartbeat), so the
            # final stamp also re-verifies lease ownership: a lost lease RAISES here
            # instead of stamping a fresh heartbeat with no live lease (codex WP2
            # consume-boundary blocker).
            if heartbeat is not None:
                heartbeat()
            else:
                store.write_heartbeat(agent)
            if persist is not None:
                persist(session_state)
            if health_writer.state != health_model.STATE_DEGRADED_OUTPUT:
                health_writer.idle(reason_code="turn_completed")
            return DriveOutcome(ok=True)
        # the turn FAILED (no completed boundary / nonzero exit / spawn error, after
        # any resume->fresh retry): undo any heartbeat its streaming progress stamped,
        # so a failed attempt leaves NO fresh heartbeat - AND reset the engine throttle
        # (reused across turns) so a successful retry within min_interval is not
        # throttled into leaving no fresh heartbeat.
        store.clear_heartbeat(agent)
        engine.reset_heartbeat_throttle()
        if persist is not None:
            persist(session_state)
        try:
            failure_class, summary = _classify(sig)
        except Exception:  # noqa: BLE001 - publish unknown before preserving failure
            health_writer.unknown()
            raise
        health_writer.failure(sig, failure_class)
        # WATCHDOG-RECOVERY ONLY (narrow path): the hung tool tree was killed and the
        # wrapper is alive + ready for the next turn, so re-stamp a fresh heartbeat (undoing
        # the clear above) - otherwise the supervisor would ALSO relaunch a healthy wrapper.
        # Ordinary failures keep the cleared heartbeat. A lost-lease raise from the injected
        # hook is NOT masked (reviewer-1 ask) - it propagates exactly like the success path.
        if sig.get("watchdog"):
            if heartbeat is not None:
                heartbeat()
            else:
                store.write_heartbeat(agent)
        return DriveOutcome(ok=False, failure_class=failure_class, summary=summary)

    return drive


def make_cadence_drive(store, agent: str, cli: str, session_state, base_argv: list[str], *,
                       sender: str | None = None, min_interval: float = 5.0,
                       render: bool = True, rules: str | None = None,
                       clock: Callable[[], float] = time.monotonic,
                       spawn: Callable[[list[str], str | None], Iterable[str]] | None = None,
                       heartbeat: Callable[[], None] | None = None,
                       persist: Callable[[object], None] | None = None,
                       ) -> Callable[[dict, list], bool]:
    """Build the SYNTHETIC cadence-turn drive for the managed lead-loop controller (WP3).

    ``cadence_drive(snapshot, items) -> bool`` drives ONE CLI turn whose prompt is the
    bounded read-only SNAPSHOT + the actionable items (NOT a bus record), returning True
    iff the turn reached a clean COMPLETED boundary. UNLIKE :func:`make_drive` there is NO
    failure CLASSIFICATION, NO :class:`~.loop.DriveOutcome`, and NO dead-letter path - a
    cadence failure is just ``False`` (the controller treats it as controller-HEALTH
    trouble: back off + escalate, never poison).

    It builds its OWN engine/detector (a separate degraded window from the message drive is
    fine - cadence turns are rare) but SHARES ``session_state`` by reference, so codex
    ``thread_id`` / claude session-id continuity holds across BOTH message and cadence
    turns. ``make_drive`` is intentionally left untouched (the reviewed message hot path).
    ``heartbeat`` is the lead-loop combined renew+stamp - it RAISES on a lost lease, which
    propagates out so a lost-lease controller stops sweeping at once."""
    from . import prompt as _prompt
    from . import session as _session

    mapper = _ADAPTERS.get(cli)
    if mapper is None:
        raise ValueError(f"no wrapper adapter for cli {cli!r}")
    cfg = DegradedConfig(telemetry_only=_TELEMETRY_ONLY.get(cli, False))
    detector = DegradedDetector(cli, cfg)
    engine = WrapperEngine(
        detector=detector,
        on_heartbeat=((lambda _ev, _ts: heartbeat()) if heartbeat is not None
                      else _default_heartbeat(store, agent)),
        on_render=(_default_render if render else None),
        on_escalate=_default_escalate(store, agent, sender or agent),
        on_info=_default_info,
        min_interval=min_interval,
    )
    spawner = spawn or _ProcStream

    def _run_one(spec) -> bool:
        """Spawn ONE CLI invocation for ``spec`` and stream its JSONL through the engine.
        True only if it reached a COMPLETED boundary, hit no terminal (non-retryable)
        adapter_error, and the child exited cleanly. A spawn/exec OSError is a False turn
        (never raised). No classification - cadence only needs ok / not-ok."""
        argv = list(base_argv) + spec.args
        started = completed = terminal = False
        try:
            stream = spawner(argv, spec.stdin)
            for line in stream:
                s = line.strip()
                if not s:
                    continue
                try:
                    raw = json.loads(s)
                except (ValueError, TypeError):
                    continue
                _session.observe_event(session_state, raw)
                for ev in mapper(raw):
                    if ev.type == EventType.TURN_STARTED:
                        started = True
                    elif ev.type == EventType.TURN_FINISHED:
                        completed = True
                    elif ev.type == EventType.ADAPTER_ERROR and not ev.retryable:
                        terminal = True
                    engine.process(ev, clock())
        except OSError:
            return False
        rc = getattr(stream, "returncode", None)
        _ = started  # observed for parity with make_drive; cadence gates on completed
        return completed and not terminal and rc in (0, None)

    def cadence_drive(snapshot: dict, items: list) -> bool:
        prompt = _prompt.assemble_cadence_prompt(snapshot, items, rules=rules)
        spec = _session.build_turn(session_state, prompt)
        cli_name = session_state.cli
        attempted_resume = ((cli_name == "codex" and "resume" in spec.args)
                            or (cli_name == "claude" and "--resume" in spec.args))
        ok = _run_one(spec)
        if not ok and attempted_resume:
            # mirror make_drive's resume self-heal: a failed RESUME turn forces a FRESH
            # session for the SAME prompt and retries inline (still ONE cadence attempt).
            if cli_name == "claude":
                _session.reset_claude_session(session_state, "resume turn failed")
            else:
                _session.mark_resume_unavailable(session_state, "resume turn failed")
            if persist is not None:
                persist(session_state)
            ok = _run_one(_session.build_turn(session_state, prompt))
        if ok:
            session_state.turns += 1
            if cli_name == "claude" and not session_state.resume_available:
                session_state.resume_available = True
                session_state.resume_unavailable_reason = ""
            # a clean cadence turn ends with a fresh heartbeat; route through the injected
            # lead-loop hook (renew+stamp), which RAISES on a lost lease.
            if heartbeat is not None:
                heartbeat()
            else:
                store.write_heartbeat(agent)
        else:
            # a FAILED cadence turn leaves NO fresh heartbeat (controller-health staleness)
            # and resets the reused engine throttle - mirrors make_drive's failure path.
            store.clear_heartbeat(agent)
            engine.reset_heartbeat_throttle()
        if persist is not None:
            persist(session_state)
        return ok

    return cadence_drive
