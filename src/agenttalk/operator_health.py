"""Monotone composition of wrapper observations and supervisor decisions.

This module governs presentation only.  The two evidence producers remain the
authorities for their own facts; operator surfaces use the derived fields here
to choose urgency and primary wording without changing either fact.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agenttalk import health as health_model
from agenttalk import supervisor as supervisor_model


URGENCY_FINE = "fine"
URGENCY_UNKNOWN = "unknown"
URGENCY_ATTENTION = "attention"
URGENCY_DANGER = "danger"

_URGENCY_RANK = {
    URGENCY_FINE: 0,
    URGENCY_UNKNOWN: 1,
    URGENCY_ATTENTION: 2,
    URGENCY_DANGER: 3,
}

_FINE_OBSERVATION_STATES = frozenset({
    health_model.STATE_IDLE_WAITING,
    health_model.STATE_WORKING_TURN,
    health_model.STATE_WORKING_SILENT,
})
_DANGER_OBSERVATION_STATES = frozenset({
    health_model.STATE_RATE_LIMITED_OR_OUTAGE,
    health_model.STATE_DEGRADED_OUTPUT,
    health_model.STATE_ERRORED_AMBIGUOUS,
    # Older snapshots and the console vocabulary still carry this state.
    "errored_fatal",
})
_ATTENTION_OBSERVATION_STATES = frozenset({
    health_model.STATE_STUCK_SUSPECTED,
    health_model.STATE_ERRORED_POISON,
    health_model.STATE_CRASHED_OR_EXITED,
    # Older snapshots and the console vocabulary still carry this state.
    "errored_recoverable",
})

_LOST_BINDING_DECISION_STATES = frozenset({
    "CLI_CHILD_DEAD",
    "WRAPPER_MISSING",
})
_LIVE_STALLED_DECISION_STATES = frozenset({
    "CLI_CHILD_STALLED",
    "CLI_CHILD_NO_PROGRESS",
})
_FAILED_DECISION_STATES = frozenset({
    "TURN_FAILED",
    "READINESS_GAVE_UP",
})
_UNCONFIRMED_DANGER_DECISION_STATES = frozenset({
    "CLI_CHILD_UNKNOWN",
    "STUCK_OR_DEAD",
})


def classify_observation_state(state: object) -> str:
    """Return the operator urgency of one wrapper-health observation."""
    if not isinstance(state, str):
        return URGENCY_UNKNOWN
    if state in _FINE_OBSERVATION_STATES:
        return URGENCY_FINE
    if state in _DANGER_OBSERVATION_STATES:
        return URGENCY_DANGER
    if state in _ATTENTION_OBSERVATION_STATES:
        return URGENCY_ATTENTION
    return URGENCY_UNKNOWN


def _supervisor_kind_and_urgency(state: object) -> tuple[str, str]:
    if not isinstance(state, str):
        return "advisory", URGENCY_ATTENTION
    if state in supervisor_model.HEALTHY_DECISION_STATES:
        return "healthy", URGENCY_FINE
    if state in supervisor_model.GRACE_DECISION_STATES:
        return "grace", URGENCY_FINE
    if state in _LOST_BINDING_DECISION_STATES:
        return "lost_binding", URGENCY_DANGER
    if state in _LIVE_STALLED_DECISION_STATES:
        return "live_stalled", URGENCY_DANGER
    if state in _FAILED_DECISION_STATES:
        return "failed", URGENCY_DANGER
    if state in _UNCONFIRMED_DANGER_DECISION_STATES:
        return "unconfirmed", URGENCY_DANGER
    return "advisory", URGENCY_ATTENTION


def compose_health_evidence(
    observation_state: object,
    *,
    decision: Mapping[str, Any] | None = None,
    unavailable_reason: str | None = None,
) -> dict[str, Any]:
    """Compose source-labelled facts using their maximum operator urgency.

    Equal-urgency ties deliberately keep the observation primary.  A decision
    and an unavailable reason are mutually exclusive producer outcomes.
    """
    if decision is not None and unavailable_reason is not None:
        raise ValueError("decision and unavailable_reason are mutually exclusive")

    state = observation_state if isinstance(observation_state, str) else "unknown"
    observation = {
        "source": "wrapper_health",
        "state": state,
        "urgency": classify_observation_state(state),
    }
    result: dict[str, Any] = {
        "observation": observation,
        "urgency": observation["urgency"],
        "primary_source": "observation",
        "disagreement": False,
    }

    supervisor: dict[str, Any] | None = None
    if decision is not None:
        decision_state = decision.get("state")
        kind, urgency = _supervisor_kind_and_urgency(decision_state)
        supervisor = {
            "source": "supervisor_decision",
            "kind": kind,
            "state": decision_state,
            "action": decision.get("action"),
            "reason": decision.get("reason"),
            "urgency": urgency,
        }
    elif unavailable_reason is not None:
        supervisor = {
            "source": "supervisor_decision",
            "kind": "unavailable",
            "reason": unavailable_reason,
            "urgency": URGENCY_ATTENTION,
        }

    if supervisor is None:
        return result

    result["supervisor"] = supervisor
    result["disagreement"] = supervisor["urgency"] != observation["urgency"]
    if _URGENCY_RANK[supervisor["urgency"]] > _URGENCY_RANK[observation["urgency"]]:
        result["urgency"] = supervisor["urgency"]
        result["primary_source"] = "supervisor"
    return result


def doctor_status(composition: Mapping[str, Any]) -> str:
    """Project composed urgency onto doctor's ok/warn/error vocabulary."""
    urgency = composition.get("urgency")
    if urgency == URGENCY_FINE:
        return "ok"
    if urgency == URGENCY_DANGER:
        return "error"
    return "warn"
