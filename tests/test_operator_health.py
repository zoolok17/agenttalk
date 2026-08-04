"""Specification tests for operator-surface health composition."""

from __future__ import annotations

import pytest

from agenttalk import operator_health as oh


OBSERVATIONS = {
    "fine": "working_turn",
    "unknown": "unknown",
    "attention": "stuck_suspected",
    "danger": "degraded_output",
}
SUPERVISORS = {
    "healthy": {"state": "HEALTHY_WORKING", "action": "none", "reason": "bound"},
    "grace": {"state": "LAUNCHING", "action": "none", "reason": "launch grace"},
    "advisory": {
        "state": "RESTART_COOLDOWN", "action": "backoff_wait", "reason": "cooldown",
    },
    "live_stalled": {
        "state": "CLI_CHILD_STALLED", "action": "stuck_recover", "reason": "live stalled",
    },
    "failed": {"state": "TURN_FAILED", "action": "none", "reason": "turn failed"},
    "lost_binding": {
        "state": "CLI_CHILD_DEAD", "action": "relaunch", "reason": "binding lost",
    },
}
SUPERVISOR_URGENCIES = {
    "healthy": "fine",
    "grace": "fine",
    "advisory": "attention",
    "live_stalled": "danger",
    "failed": "danger",
    "lost_binding": "danger",
    "unconfirmed": "danger",
    "unavailable": "attention",
}

# Literal expectations are derived from the prose invariant, not from the
# implementation.  Each row names the clause that determines its outcome.
COMPOSITION_CASES = (
    ("fine", "healthy", "fine", "observation", "ok", "equal_urgency_observation_tie"),
    ("fine", "grace", "fine", "observation", "ok", "equal_urgency_observation_tie"),
    ("fine", "advisory", "attention", "supervisor", "warn", "maximum_urgency"),
    ("fine", "live_stalled", "danger", "supervisor", "error", "maximum_urgency"),
    ("fine", "failed", "danger", "supervisor", "error", "maximum_urgency"),
    ("fine", "lost_binding", "danger", "supervisor", "error", "maximum_urgency"),
    ("fine", "unavailable", "attention", "supervisor", "warn", "unavailable_never_healthy"),
    ("unknown", "healthy", "unknown", "observation", "warn", "healthy_never_erases"),
    ("unknown", "grace", "unknown", "observation", "warn", "grace_never_erases"),
    ("unknown", "advisory", "attention", "supervisor", "warn", "maximum_urgency"),
    ("unknown", "live_stalled", "danger", "supervisor", "error", "maximum_urgency"),
    ("unknown", "failed", "danger", "supervisor", "error", "maximum_urgency"),
    ("unknown", "lost_binding", "danger", "supervisor", "error", "maximum_urgency"),
    ("unknown", "unavailable", "attention", "supervisor", "warn", "unavailable_never_healthy"),
    ("attention", "healthy", "attention", "observation", "warn", "healthy_never_erases"),
    ("attention", "grace", "attention", "observation", "warn", "grace_never_erases"),
    ("attention", "advisory", "attention", "observation", "warn", "equal_urgency_observation_tie"),
    ("attention", "live_stalled", "danger", "supervisor", "error", "maximum_urgency"),
    ("attention", "failed", "danger", "supervisor", "error", "maximum_urgency"),
    ("attention", "lost_binding", "danger", "supervisor", "error", "maximum_urgency"),
    ("attention", "unavailable", "attention", "observation", "warn",
     "equal_urgency_observation_tie+facts_remain_visible"),
    ("danger", "healthy", "danger", "observation", "error", "healthy_never_erases"),
    ("danger", "grace", "danger", "observation", "error", "grace_never_erases"),
    ("danger", "advisory", "danger", "observation", "error", "maximum_urgency"),
    ("danger", "live_stalled", "danger", "observation", "error", "equal_urgency_observation_tie"),
    ("danger", "failed", "danger", "observation", "error", "equal_urgency_observation_tie"),
    ("danger", "lost_binding", "danger", "observation", "error", "equal_urgency_observation_tie"),
    ("danger", "unavailable", "danger", "observation", "error",
     "maximum_urgency+facts_remain_visible"),
)


@pytest.mark.parametrize(
    ("observation", "supervisor", "urgency", "primary_source", "doctor_status", "clause"),
    COMPOSITION_CASES,
)
def test_composition_cross_product_is_invariant_derived(
    observation: str,
    supervisor: str,
    urgency: str,
    primary_source: str,
    doctor_status: str,
    clause: str,
) -> None:
    decision = SUPERVISORS.get(supervisor)
    unavailable_reason = "assessment_failed" if supervisor == "unavailable" else None

    got = oh.compose_health_evidence(
        OBSERVATIONS[observation],
        decision=decision,
        unavailable_reason=unavailable_reason,
    )

    assert got["observation"] == {
        "source": "wrapper_health",
        "state": OBSERVATIONS[observation],
        "urgency": observation,
    }
    assert got["urgency"] == urgency, clause
    assert got["primary_source"] == primary_source, clause
    assert oh.doctor_status(got) == doctor_status, clause
    assert got["supervisor"]["urgency"] == SUPERVISOR_URGENCIES[supervisor]
    assert got["supervisor"]["kind"] == supervisor
    if supervisor == "unavailable":
        assert got["supervisor"]["reason"] == "assessment_failed"
    else:
        assert got["supervisor"] == {
            "source": "supervisor_decision",
            "kind": supervisor,
            **decision,
            "urgency": got["supervisor"]["urgency"],
        }


def test_action_does_not_change_supervisor_urgency() -> None:
    base = {"state": "RESTART_COOLDOWN", "reason": "cooldown"}

    none = oh.compose_health_evidence("working_turn", decision={**base, "action": "none"})
    relaunch = oh.compose_health_evidence(
        "working_turn", decision={**base, "action": "relaunch"})

    assert none["urgency"] == relaunch["urgency"] == "attention"
    assert none["supervisor"]["kind"] == relaunch["supervisor"]["kind"] == "advisory"


@pytest.mark.parametrize(
    ("state", "urgency"),
    [
        ("idle_waiting", "fine"),
        ("working_turn", "fine"),
        ("working_silent", "fine"),
        ("unknown", "unknown"),
        ("stuck_suspected", "attention"),
        ("errored_poison", "attention"),
        ("crashed_or_exited", "attention"),
        ("rate_limited_or_outage", "danger"),
        ("degraded_output", "danger"),
        ("errored_ambiguous", "danger"),
    ],
)
def test_observation_vocabulary_has_explicit_urgency(state: str, urgency: str) -> None:
    assert oh.classify_observation_state(state) == urgency


def test_composition_copies_producer_facts_instead_of_mutating_them() -> None:
    decision = {"state": "HEALTHY_WORKING", "action": "none", "reason": "bound"}

    got = oh.compose_health_evidence("degraded_output", decision=decision)
    got["supervisor"]["state"] = "CHANGED_IN_PRESENTATION"

    assert decision == {"state": "HEALTHY_WORKING", "action": "none", "reason": "bound"}


@pytest.mark.parametrize(
    ("state", "kind"),
    [
        ("HEALTHY_IDLE", "healthy"),
        ("HEALTHY_WORKING", "healthy"),
        ("CLI_CHILD_STARTING", "grace"),
        ("LAUNCHING", "grace"),
        ("CLI_CHILD_STALLED", "live_stalled"),
        ("CLI_CHILD_NO_PROGRESS", "live_stalled"),
        ("TURN_FAILED", "failed"),
        ("READINESS_GAVE_UP", "failed"),
        ("CLI_CHILD_DEAD", "lost_binding"),
        ("WRAPPER_MISSING", "lost_binding"),
        ("CLI_CHILD_UNKNOWN", "unconfirmed"),
        ("STUCK_OR_DEAD", "unconfirmed"),
        ("CLI_CHILD_MISSING", "advisory"),
        ("CLI_CHILD_STALL_SUSPECT", "advisory"),
        ("RESTART_COOLDOWN", "advisory"),
        ("ACTIVE_OR_BUSY", "advisory"),
        ("REFUSE_PROTECTED", "advisory"),
        ("RESTART_UNAUTHORIZED", "advisory"),
        ("LIVE_PROTECTED_REFUSED", "advisory"),
        ("MANUAL_RESTART", "advisory"),
        ("CONFIG_BLOCKED", "advisory"),
        ("LEAD_LOOP_STOOD_DOWN", "advisory"),
        ("LEAD_LOOP_BLOCKED", "advisory"),
        ("UNACCOUNTED_LIVE_DESCENDANT", "advisory"),
        ("PROCESS_TREE_INVALID", "advisory"),
        ("PROCESS_TREE_TRUNCATED", "advisory"),
    ],
)
def test_supervisor_kind_preserves_producer_meaning(state: str, kind: str) -> None:
    got = oh.compose_health_evidence(
        "working_turn", decision={"state": state, "action": "none", "reason": "test"})

    assert got["supervisor"]["kind"] == kind
    assert got["supervisor"]["urgency"] == SUPERVISOR_URGENCIES[kind]
