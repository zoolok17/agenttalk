"""Progress-adapter wrapper (0.30.0): one wrapper per CLI launches the agent in a
structured-stream mode and turns its native stream into (1) a throttled liveness
heartbeat, (2) readable operator output, and (3) a protocol-health degraded
signal - while the supervisor stays dumb (heartbeat / backoff / kill only).

Phase 1 ships the framework + the Codex adapter (mapped to the empirical
`codex exec --json` 0.141.0 catalog). The Claude stream-json adapter is Phase 2.
"""

from __future__ import annotations

from .degraded import DegradedConfig, DegradedDetector, DegradedSignal, classify_text
from .events import PROGRESS_EVENTS, Event, EventType
from .framework import WrapperEngine

__all__ = [
    "Event",
    "EventType",
    "PROGRESS_EVENTS",
    "WrapperEngine",
    "DegradedDetector",
    "DegradedConfig",
    "DegradedSignal",
    "classify_text",
]
