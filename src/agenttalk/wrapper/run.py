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
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime, timezone

from . import claude_adapter, codex_adapter
from .degraded import DegradedConfig, DegradedDetector
from .events import Event
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

    proc = subprocess.Popen(  # noqa: S603 - argv is operator-provided launch command
        argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        bufsize=1,
    )
    try:
        engine.run(parse_lines(proc.stdout or [], mapper), clock)
    finally:
        if proc.stdout:
            proc.stdout.close()
    return proc.wait()


def _spawn_turn_lines(argv: list[str], stdin_text: str | None) -> Iterator[str]:
    """Spawn the CLI for one turn, feed the prompt on STDIN (dodges quoting), and
    yield its stdout lines. Default spawner for make_drive; tests inject their own."""
    proc = subprocess.Popen(  # noqa: S603 - argv is operator-provided launch command
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    if proc.stdin is not None:
        if stdin_text is not None:
            proc.stdin.write(stdin_text)
        proc.stdin.close()
    try:
        yield from (proc.stdout or [])
    finally:
        if proc.stdout:
            proc.stdout.close()
        proc.wait()


def make_drive(store, agent: str, cli: str, session_state, base_argv: list[str], *,
               sender: str | None = None, min_interval: float = 5.0,
               render: bool = True, rules: str | None = None,
               clock: Callable[[], float] = time.monotonic,
               spawn: Callable[[list[str], str | None], Iterable[str]] | None = None,
               persist: Callable[[object], None] | None = None) -> Callable[[dict], None]:
    """Build the per-turn ``drive(record)`` callback for loop.run_loop. Each call
    drives ONE real CLI turn: assemble the prompt -> build the session turn args ->
    spawn the CLI (prompt on stdin) -> process the JSONL stream via the engine
    (heartbeat-on-progress + degraded + render) while capturing codex's durable
    thread_id (session.observe_event on the RAW stream).

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
        on_heartbeat=_default_heartbeat(store, agent),
        on_render=(_default_render if render else None),
        on_escalate=_default_escalate(store, agent, sender or agent),
        on_info=_default_info,
        min_interval=min_interval,
    )
    spawner = spawn or _spawn_turn_lines

    def drive(record: dict) -> None:
        prompt = _prompt.assemble_turn_prompt(record, rules=rules)
        spec = _session.build_turn(session_state, prompt)
        argv = list(base_argv) + spec.args
        for line in spawner(argv, spec.stdin):
            s = line.strip()
            if not s:
                continue
            try:
                raw = json.loads(s)
            except (ValueError, TypeError):
                continue
            _session.observe_event(session_state, raw)   # capture codex thread_id
            for ev in mapper(raw):
                engine.process(ev, clock())
        session_state.turns += 1
        if persist is not None:
            persist(session_state)

    return drive
