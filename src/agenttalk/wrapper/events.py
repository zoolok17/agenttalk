"""Normalized progress-event contract for the agent wrappers (0.30.0).

One WRAPPER per CLI launches the CLI in a structured-stream mode and translates
its native stream into this NORMALIZED event set. Everything downstream - the
heartbeat stamper, the operator renderer, the degraded-output detector - consumes
ONLY these events, so the framework is CLI-agnostic and unit-testable without a
real subprocess. The supervisor never sees these events; it stays dumb
(heartbeat-freshness / backoff / kill only) and never parses streams.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class EventType(str, Enum):
    """The accepted normalized event set (thread a-progress-adapter)."""

    TURN_STARTED = "turn_started"
    MODEL_OUTPUT = "model_output"
    MODEL_OUTPUT_DELTA = "model_output_delta"
    TOOL_STARTED = "tool_started"
    TOOL_OUTPUT = "tool_output"
    TOOL_OUTPUT_DELTA = "tool_output_delta"
    TOOL_FINISHED = "tool_finished"
    TURN_FINISHED = "turn_finished"
    ADAPTER_ERROR = "adapter_error"
    DEGRADED_OUTPUT = "degraded_output"


# Events that prove the agent is making real progress -> stamp the heartbeat.
# ADAPTER_ERROR (transport / API noise) and DEGRADED_OUTPUT (bad protocol output)
# deliberately do NOT stamp: neither is evidence of healthy progress. In
# particular, never let leaked/degraded text refresh health as if it were good
# model output - that would prove the agent broken while marking it alive.
PROGRESS_EVENTS = frozenset(
    {
        EventType.TURN_STARTED,
        EventType.MODEL_OUTPUT,
        EventType.MODEL_OUTPUT_DELTA,
        EventType.TOOL_STARTED,
        EventType.TOOL_OUTPUT,
        EventType.TOOL_OUTPUT_DELTA,
        EventType.TOOL_FINISHED,
        EventType.TURN_FINISHED,
    }
)


@dataclass(frozen=True)
class Event:
    """A single normalized progress event.

    ``text``      - model/tool output text (rendered; MODEL_OUTPUT text is the
                    ONLY thing the degraded detector scans).
    ``tool``      - the tool/command name for TOOL_* events.
    ``retryable`` - tiers an ADAPTER_ERROR: a transient/transport error the CLI
                    self-recovers from (e.g. a WebSocket->HTTPS fallback) is
                    retryable=True (log only, never act); a terminal failure
                    (turn.failed) is retryable=False. Ignored for other types.
    ``channel``   - for model output, which stream the text is: "text" (assistant
                    output, the degraded-scan target) or "thinking" (internal
                    reasoning - NEVER scanned, but a thinking_delta IS liveness).
                    None for non-model events / CLIs without the distinction.
    ``raw``       - the original adapter payload, kept for diagnostics/render.
    """

    type: EventType
    text: str | None = None
    tool: str | None = None
    retryable: bool = False
    channel: str | None = None
    raw: dict = field(default_factory=dict)

    @property
    def is_progress(self) -> bool:
        """True iff this event should stamp the agent's activity heartbeat.

        A raw text delta does NOT stamp (ruling on the Claude adapter): a leaked
        turn streams degraded text fragments, so only the CLEAN assembled model
        output (scanned, and suppressed when degraded) may stamp text liveness.
        Thinking deltas DO stamp - they are genuine reasoning progress and close
        the pure-reasoning gap. Non-text-delta progress is unaffected.
        """
        if self.type == EventType.MODEL_OUTPUT_DELTA and self.channel == "text":
            return False
        return self.type in PROGRESS_EVENTS
