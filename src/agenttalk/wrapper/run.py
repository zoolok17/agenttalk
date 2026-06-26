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
# subprocess spawns ONLY the operator-provided CLI launch command; never shell=True.
import subprocess  # nosec B404
import sys
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime, timezone

from . import claude_adapter, codex_adapter
from .degraded import DegradedConfig, DegradedDetector
from .events import Event, EventType
from .framework import WrapperEngine

# cli -> event mapper.
_ADAPTERS: dict[str, Callable[[object], list[Event]]] = {
    "codex": codex_adapter.map_event,
    "claude": claude_adapter.map_event,
}
# Codex has no observed real tool-call-leak signature yet -> detect + log, never
# escalate. Claude's leak (tool-call markup as assistant text) is the cataloged
# high-confidence signature -> escalation-enabled.
_TELEMETRY_ONLY = {"codex": True, "claude": False}


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

    if heartbeat_fn is None:
        heartbeat_fn = _default_heartbeat(store, agent)
    if restart_fn is None:
        restart_fn = _default_escalate(store, agent, sender or agent)
    if render_fn is None:
        render_fn = _default_render if render else None
    if info_fn is None:
        info_fn = _default_info

    engine = WrapperEngine(
        detector=detector,
        on_heartbeat=heartbeat_fn,
        on_render=render_fn,
        on_escalate=restart_fn,
        on_info=info_fn,
        min_interval=min_interval,
    )

    if line_source is not None:
        engine.run(parse_lines(line_source, mapper), clock)
        return 0

    # argv is the operator-provided launch command; never shell=True.
    # encoding/errors are EXPLICIT: codex/claude emit UTF-8, but text=True alone
    # decodes child stdout via the platform default (cp1252 on Windows) and CRASHES
    # on the first non-ASCII byte (e.g. a smart quote). errors="replace" means a
    # genuinely malformed byte renders a replacement char instead of killing the
    # wrapper. In text mode this also governs the stdin prompt write.
    proc = subprocess.Popen(  # noqa: S603  # nosec B603
        argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace", bufsize=1,
    )
    try:
        engine.run(parse_lines(proc.stdout or [], mapper), clock)
    finally:
        if proc.stdout:
            proc.stdout.close()
    return proc.wait()


class _ProcStream:
    """Default make_drive spawner: spawn the CLI for one turn (prompt on STDIN, to
    dodge quoting), yield its stdout lines, and expose the child EXIT CODE as
    ``.returncode`` once the stream is exhausted - so the drive path can tell a
    clean completed turn from a partial stream that died with a bad exit. Tests
    inject their own spawner (a plain list = unknown exit, an object with
    ``.returncode`` = a controlled exit)."""

    def __init__(self, argv: list[str], stdin_text: str | None) -> None:
        # argv is the operator-provided launch command; never shell=True.
        # Explicit encoding/errors (see run_wrapper): UTF-8 child output must not be
        # decoded as cp1252 on Windows (crash on the first non-ASCII byte), and a
        # malformed byte must be replaced, not fatal. Governs the stdin prompt too.
        self._proc = subprocess.Popen(  # noqa: S603  # nosec B603
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
            bufsize=1,
        )
        if self._proc.stdin is not None:
            if stdin_text is not None:
                self._proc.stdin.write(stdin_text)
            self._proc.stdin.close()
        self.returncode: int | None = None

    def __iter__(self) -> Iterator[str]:
        try:
            yield from (self._proc.stdout or [])
        finally:
            if self._proc.stdout:
                self._proc.stdout.close()
            self.returncode = self._proc.wait()


def make_drive(store, agent: str, cli: str, session_state, base_argv: list[str], *,
               sender: str | None = None, min_interval: float = 5.0,
               render: bool = True, rules: str | None = None,
               clock: Callable[[], float] = time.monotonic,
               spawn: Callable[[list[str], str | None], Iterable[str]] | None = None,
               persist: Callable[[object], None] | None = None) -> Callable[[dict], bool]:
    """Build the per-turn ``drive(record)`` callback for loop.run_loop. Each call
    drives ONE real CLI turn and returns whether it SUCCEEDED. A turn fails if it
    produced no progress event or hit a TERMINAL (non-retryable) adapter_error
    (turn.failed, a nonzero-no-JSON child, a resume no-session). On a failed CODEX
    RESUME turn it marks resume unavailable, persists, and retries FRESH (exec
    --json) for the SAME record before returning; run_loop only commits the inbound
    message when drive returns True, so a failed turn is never lost (at-least-once).

    The detector + engine are created ONCE and reused across turns, so the degraded
    confirmation window (which counts degraded turns across CLI invocations) and the
    heartbeat throttle persist. ``spawn`` is injectable for tests (no real
    subprocess); ``persist`` is called after each turn to save session state."""
    from . import prompt as _prompt
    from . import session as _session

    mapper = _ADAPTERS.get(cli)
    if mapper is None:
        raise ValueError(f"no wrapper adapter for cli {cli!r}")
    cfg = DegradedConfig(telemetry_only=_TELEMETRY_ONLY.get(cli, False))
    detector = DegradedDetector(cli, cfg)
    engine = WrapperEngine(
        detector=detector,
        # Stamp heartbeat on streaming progress so a long SUCCESSFUL turn stays live
        # before it completes (in-turn liveness). A FAILED turn may also stamp here
        # mid-stream, so drive() CLEARS the heartbeat when the turn ends failed -
        # net: live during a successful turn, no fresh heartbeat after a failed one
        # (reviewer-1 gate: both, via stamp-during + clear-on-failure).
        on_heartbeat=_default_heartbeat(store, agent),
        on_render=(_default_render if render else None),
        on_escalate=_default_escalate(store, agent, sender or agent),
        on_info=_default_info,
        min_interval=min_interval,
    )
    spawner = spawn or _ProcStream

    def _run_one(spec) -> bool:
        """Spawn ONE CLI invocation for ``spec`` and process its JSONL via the
        engine. A turn SUCCEEDS only if it reached a COMPLETED turn boundary
        (TURN_FINISHED: codex turn.completed / claude message_stop), hit no terminal
        (non-retryable) adapter_error, AND the child exited cleanly. Partial progress
        (turn started then the stream died / nonzero exit before completion) is NOT
        success - so the message is never committed for an incomplete turn."""
        argv = list(base_argv) + spec.args
        stream = spawner(argv, spec.stdin)
        saw_completion = False
        saw_terminal = False
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
                if ev.type == EventType.TURN_FINISHED:
                    saw_completion = True
                elif ev.type == EventType.ADAPTER_ERROR and not ev.retryable:
                    saw_terminal = True
                engine.process(ev, clock())
        # rc is None for an injected list spawn (unknown -> trust the boundary); a
        # real _ProcStream reports the child exit code (nonzero = failed turn).
        rc = getattr(stream, "returncode", None)
        return saw_completion and not saw_terminal and rc in (0, None)

    def drive(record: dict) -> bool:
        prompt = _prompt.assemble_turn_prompt(record, rules=rules)
        spec = _session.build_turn(session_state, prompt)
        attempted_resume = session_state.cli == "codex" and "resume" in spec.args
        success = _run_one(spec)
        if not success and attempted_resume:
            # the resume turn failed -> force a FRESH exec for the SAME record and
            # retry inline (a new thread_id will be observed + persisted).
            _session.mark_resume_unavailable(session_state, "resume turn failed")
            if persist is not None:
                persist(session_state)
            success = _run_one(_session.build_turn(session_state, prompt))
        if success:
            session_state.turns += 1
            # Unconditional: a clean completed turn ALWAYS ends with a fresh
            # heartbeat, even if the engine's min_interval throttled the in-turn
            # stamps (e.g. a quick retry right after a failure).
            store.write_heartbeat(agent)
        else:
            # the turn FAILED (no completed boundary / nonzero exit, after any
            # resume->fresh retry): undo any heartbeat its streaming progress
            # stamped, so a failed attempt leaves NO fresh heartbeat - AND reset the
            # engine throttle (reused across turns) so a successful retry within
            # min_interval is not throttled into leaving no fresh heartbeat.
            store.clear_heartbeat(agent)
            engine.reset_heartbeat_throttle()
        if persist is not None:
            persist(session_state)
        return success

    return drive
