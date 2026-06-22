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

from . import codex_adapter
from .degraded import DegradedConfig, DegradedDetector
from .events import Event
from .framework import WrapperEngine

# cli -> (event mapper, default telemetry_only). Claude is Phase 2.
_ADAPTERS: dict[str, Callable[[object], list[Event]]] = {
    "codex": codex_adapter.map_event,
}
# Codex has no observed real tool-call-leak signature yet -> detect + log, never
# escalate. The Claude adapter (Phase 2) will flip this to escalation-enabled.
_TELEMETRY_ONLY = {"codex": True}


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
