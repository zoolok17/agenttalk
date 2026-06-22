"""The wrapper ENGINE: consume normalized events and drive three sinks -
heartbeat (throttled; progress events only), render (every event), and degraded
detection. Pure core - the detector, sinks, and clock are injected, so the engine
runs over a plain event iterable in tests with no subprocess, store, or console.

The supervisor stays dumb: it never sees these events. This engine owns turning
the structured stream into (a) a throttled liveness heartbeat, (b) readable
operator output, and (c) a protocol-health (degraded) signal.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from .degraded import DegradedDetector
from .events import Event


class WrapperEngine:
    def __init__(
        self,
        *,
        detector: DegradedDetector,
        on_heartbeat: Callable[[Event, float], None],
        on_render: Callable[[Event], None] | None = None,
        on_escalate: Callable[[object], None] | None = None,
        on_info: Callable[[object], None] | None = None,
        min_interval: float = 5.0,
    ) -> None:
        self.detector = detector
        self.on_heartbeat = on_heartbeat
        self.on_render = on_render
        self.on_escalate = on_escalate
        self.on_info = on_info
        self.min_interval = float(min_interval)
        self._last_stamp: float | None = None

    def _should_stamp(self, now: float) -> bool:
        return self._last_stamp is None or (now - self._last_stamp) >= self.min_interval

    def process(self, event: Event, now: float) -> None:
        if self.on_render is not None:
            self.on_render(event)
        # Heartbeat ONLY on real progress, throttled by min_interval. ADAPTER_ERROR
        # and DEGRADED_OUTPUT are not progress, so a noisy-but-broken agent never
        # refreshes its own liveness here.
        if event.is_progress and self._should_stamp(now):
            self._last_stamp = now
            self.on_heartbeat(event, now)
        sig = self.detector.feed(event, now)
        if sig is None:
            return
        if sig.level == "escalate":
            if self.on_escalate is not None:
                self.on_escalate(sig)
        elif self.on_info is not None:
            self.on_info(sig)

    def run(self, events: Iterable[Event], clock: Callable[[], float]) -> None:
        for ev in events:
            self.process(ev, clock())
