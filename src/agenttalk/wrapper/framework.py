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
        on_progress: Callable[[Event, float], None] | None = None,
        on_render: Callable[[Event], None] | None = None,
        on_escalate: Callable[[object], None] | None = None,
        on_info: Callable[[object], None] | None = None,
        min_interval: float = 5.0,
    ) -> None:
        self.detector = detector
        self.on_heartbeat = on_heartbeat
        self.on_progress = on_progress
        self.on_render = on_render
        self.on_escalate = on_escalate
        self.on_info = on_info
        self.min_interval = float(min_interval)
        self._last_stamp: float | None = None

    def _should_stamp(self, now: float) -> bool:
        return self._last_stamp is None or (now - self._last_stamp) >= self.min_interval

    def reset_heartbeat_throttle(self) -> None:
        """Forget the last-stamp time so the NEXT progress event stamps regardless
        of min_interval. Used when a failed turn clears the store heartbeat: the
        engine is reused across turns, so the throttle must be reset to line up with
        the cleared store state, or a successful retry within min_interval would be
        throttled and leave no fresh heartbeat."""
        self._last_stamp = None

    def process(self, event: Event, now: float) -> None:
        if self.on_render is not None:
            self.on_render(event)
        # Classify FIRST: a high-confidence degraded MODEL_OUTPUT must not stamp the
        # heartbeat (leaked tool-call markup proves the agent broken; it must not
        # also mark it healthy). Then stamp only a real, non-suppressed progress
        # event, throttled. ADAPTER_ERROR / DEGRADED_OUTPUT are not progress at all.
        result = self.detector.feed(event, now)
        if event.is_progress and not result.suppress_heartbeat:
            if self.on_progress is not None:
                self.on_progress(event, now)
            if self._should_stamp(now):
                self._last_stamp = now
                self.on_heartbeat(event, now)
        sig = result.signal
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
