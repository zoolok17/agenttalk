"""Degraded-output detection: protocol-health, DISTINCT from heartbeat liveness.

Heartbeat answers "is the agent making progress / idle-ready?"; this answers "is
the agent's protocol output VALID?". An agent can emit bytes (so its heartbeat
stays fresh via the independent wait-waiter) while those bytes prove it is no
longer executing the tool protocol - e.g. a Claude turn that leaks literal
tool-call markup as assistant TEXT instead of a structured tool call (the action
is silently dropped). That is noisy-but-broken, which heartbeat-staleness alone
can never catch.

Field intel (do NOT regress this): a single malformed tool call is USUALLY
SELF-HEALING - the agent retries next turn and the independent wait-waiter keeps
the heartbeat fresh. So detection must NOT immediately restart (that recreates
the test #4-#6 false-kill class). On first high-confidence detection we emit an
INFORMATIONAL signal and open a CONFIRMATION WINDOW; we ESCALATE to a dedicated
``request-restart --reason degraded-output`` ONLY if no clean progress event
arrives within the window. We never force-expire the heartbeat (no-progress vs
bad-progress stay distinct) and never let leaked text stamp progress-heartbeat.

Division of labor with the supervisor's heartbeat-staleness:
  * garble-then-SILENCE  -> heartbeat owns it (the waiter dies, heartbeat goes
                            stale at ``stuck_after_seconds``, default 120s).
  * noisy-but-bad        -> this detector owns it (the agent keeps emitting
                            degraded text so staleness would never fire).
The time backstop here defaults to 180s (>= 120s) so the two never race on pure
silence; the turn-count (default 2) is the fast path for the noisy case.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from .events import Event, EventType

CLAUDE = "claude"
CODEX = "codex"

# Claude high-confidence signatures: literal tool-call XML markup that should only
# ever arrive as a STRUCTURED tool event, never as assistant prose. Matched only
# against the agent's OWN model-output text (the adapter has already separated it
# from user / bus / tool content), with fenced code stripped first.
_OPEN_INVOKE = re.compile(r"<\s*invoke\b", re.IGNORECASE)
_CLOSE_INVOKE = re.compile(r"<\s*/\s*invoke\s*>", re.IGNORECASE)
_FUNCTION_CALLS = re.compile(r"<\s*function_calls\b", re.IGNORECASE)
_PARAMETER = re.compile(r"<\s*parameter\s+name\s*=", re.IGNORECASE)
_FENCE_BLOCK = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`[^`]*`")


@dataclass
class DegradedConfig:
    """Per-CLI policy. ``telemetry_only`` (Codex default) detects + logs but never
    escalates - we have no real Codex leak signature yet."""

    window_turns: int = 2
    window_seconds: float = 180.0  # keep >= supervisor stuck_after_seconds (120s)
    telemetry_only: bool = False


@dataclass(frozen=True)
class DegradedSignal:
    """A noteworthy degraded-output event.

    level      - "informational" (first sight / candidate / telemetry) or
                 "escalate" (confirmation window closed -> request restart).
    confidence - "high" (multi-token structural) or "candidate" (a lone tag).
    reason     - a REDACTED reason string (signature label + short hash), safe to
                 put in a restart marker / log; never the full leaked payload.
    """

    level: str
    confidence: str
    reason: str


@dataclass(frozen=True)
class FeedResult:
    """What ``DegradedDetector.feed`` returns for one event.

    signal             - a DegradedSignal to route (or None).
    suppress_heartbeat - True iff THIS event is high-confidence degraded model
                         output, so the engine must NOT let it stamp the heartbeat
                         (leaked text proves the agent broken; it must not also
                         mark it healthy). Only ever set for MODEL_OUTPUT(_DELTA).
    """

    signal: DegradedSignal | None = None
    suppress_heartbeat: bool = False


def _strip_code(text: str) -> str:
    """Remove fenced + inline code so quoted markup (like this very docstring's
    examples, or an agent legitimately discussing tool syntax in a code block)
    does not trip the detector."""
    return _INLINE_CODE.sub("", _FENCE_BLOCK.sub("", text))


def classify_text(text: str | None) -> str | None:
    """Classify assistant-output text for leaked tool-call markup.

    Returns "high" for a multi-token STRUCTURAL signature (an invoke-open tag with
    a parameter-name tag or a matching close, or a function_calls tag), "candidate"
    for a lone invoke-open tag, or None. Pure + side-effect free for easy testing.
    """
    if not text:
        return None
    scan = _strip_code(text)
    if not scan:
        return None
    has_open = bool(_OPEN_INVOKE.search(scan))
    has_close = bool(_CLOSE_INVOKE.search(scan))
    has_params = bool(_PARAMETER.search(scan))
    has_fcalls = bool(_FUNCTION_CALLS.search(scan))
    if has_fcalls or (has_open and (has_close or has_params)):
        return "high"
    if has_open or has_close or has_params:
        return "candidate"
    return None


def _redacted_reason(confidence: str, text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]
    return f"degraded-output: leaked tool-call markup ({confidence}, sig={digest})"


class DegradedDetector:
    """Per-agent confirmation-window state machine. Fed normalized events in order
    (plus a monotonic ``now`` for the time backstop). Returns a DegradedSignal when
    it has something to report; the runner performs the actual request-restart so
    this stays pure/testable (no I/O here)."""

    def __init__(self, cli: str, config: DegradedConfig | None = None) -> None:
        self.cli = cli
        self.config = config or DegradedConfig()
        self._tool_started_this_turn = False
        self._degraded_this_turn = False
        self._degraded_turns = 0          # consecutive high-confidence degraded turns
        self._window_open_ts: float | None = None
        self._escalated = False           # once-escalated latch (escalate EXACTLY once)
        self._last_reason = ""

    def _reset_counters(self) -> None:
        self._degraded_turns = 0
        self._window_open_ts = None

    def _self_heal(self) -> None:
        # Clean progress (a real tool firing or a clean turn) resets the window AND
        # clears the once-escalated latch, so a LATER fresh degraded burst escalates
        # again as a new incident (latched until clean self-healing, then re-arms).
        self._reset_counters()
        self._escalated = False

    def feed(self, event: Event, now: float) -> FeedResult:
        etype = event.type
        if etype == EventType.TURN_STARTED:
            self._tool_started_this_turn = False
            self._degraded_this_turn = False
            return FeedResult()
        if etype == EventType.TOOL_STARTED:
            # A real structured tool firing IS clean progress: the agent is
            # executing the protocol. It self-heals the window AND the CURRENT turn
            # (a high-confidence leak earlier this turn no longer counts), and
            # clears the escalation latch.
            self._tool_started_this_turn = True
            self._degraded_this_turn = False
            self._self_heal()
            return FeedResult()
        if etype in (EventType.MODEL_OUTPUT, EventType.MODEL_OUTPUT_DELTA):
            return self._scan_model_output(event, now)
        if etype == EventType.TURN_FINISHED:
            return FeedResult(signal=self._on_turn_finished(now))
        return FeedResult()

    def _scan_model_output(self, event: Event, now: float) -> FeedResult:
        confidence = classify_text(event.text)
        if confidence is None:
            return FeedResult()
        # A real structured tool already fired this turn -> the markup is far more
        # likely commentary; downgrade a "high" hit to candidate.
        if self._tool_started_this_turn and confidence == "high":
            confidence = "candidate"
        reason = _redacted_reason(confidence, event.text or "")
        self._last_reason = reason
        if confidence == "candidate":
            # candidate-only: warn/telemetry, does NOT fuel escalation and does
            # NOT suppress the heartbeat (too weak to call the agent broken).
            return FeedResult(signal=DegradedSignal("informational", "candidate", reason))
        # high-confidence: this turn is degraded (the window opens at turn finish),
        # AND this very event must NOT stamp the heartbeat - leaked tool-call markup
        # must never refresh health as if it were good model progress.
        self._degraded_this_turn = True
        return FeedResult(suppress_heartbeat=True)

    def _on_turn_finished(self, now: float) -> DegradedSignal | None:
        if not self._degraded_this_turn:
            # a clean turn (no high-confidence leak) = self-heal: reset the window
            # and clear the escalation latch.
            self._self_heal()
            return None
        # this turn was degraded: extend / open the window.
        first = self._degraded_turns == 0
        self._degraded_turns += 1
        if self._window_open_ts is None:
            self._window_open_ts = now
        if self.config.telemetry_only:
            # Codex: detect + log, never escalate (no real leak signature yet).
            return DegradedSignal("informational", "high", self._last_reason) if first else None
        if self._escalated:
            # already escalated for this sustained incident: keep detecting but do
            # NOT re-escalate. Escalate EXACTLY ONCE until a clean self-heal re-arms
            # us (otherwise a sustained bad stream would spam restarts / overwrite
            # the marker every window_turns).
            return None
        # explicit None-check: window_open_ts can legitimately be 0.0 (falsy), so
        # `ts or now` would wrongly treat a window opened at t=0 as unset.
        base = self._window_open_ts if self._window_open_ts is not None else now
        elapsed = now - base
        if self._degraded_turns >= self.config.window_turns or elapsed >= self.config.window_seconds:
            reason = self._last_reason
            self._reset_counters()      # reset counting, but KEEP the latch set
            self._escalated = True
            return DegradedSignal("escalate", "high", reason)
        # first degraded turn, window now open: informational only.
        return DegradedSignal("informational", "high", self._last_reason) if first else None
