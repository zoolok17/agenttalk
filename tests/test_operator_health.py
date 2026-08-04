"""Specification tests for operator-surface health composition."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from itertools import product

import pytest

from agenttalk import cli, doctor, health as health_model, operator_health as oh, web
from agenttalk.store import Store


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
    "readiness_exhausted": {
        "state": "READINESS_GAVE_UP",
        "action": "readiness_gave_up",
        "reason": "never reached first heartbeat",
    },
    "lost_binding": {
        "state": "CLI_CHILD_DEAD", "action": "relaunch", "reason": "binding lost",
    },
    "unconfirmed": {
        "state": "CLI_CHILD_UNKNOWN", "action": "none", "reason": "binding unknown",
    },
}
SUPERVISOR_URGENCIES = {
    "healthy": "fine",
    "grace": "fine",
    "advisory": "attention",
    "live_stalled": "danger",
    "failed": "danger",
    "readiness_exhausted": "danger",
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
    ("fine", "readiness_exhausted", "danger", "supervisor", "error", "maximum_urgency"),
    ("fine", "lost_binding", "danger", "supervisor", "error", "maximum_urgency"),
    ("fine", "unconfirmed", "danger", "supervisor", "error", "maximum_urgency"),
    ("fine", "unavailable", "attention", "supervisor", "warn", "unavailable_never_healthy"),
    ("unknown", "healthy", "unknown", "observation", "warn", "healthy_never_erases"),
    ("unknown", "grace", "unknown", "observation", "warn", "grace_never_erases"),
    ("unknown", "advisory", "attention", "supervisor", "warn", "maximum_urgency"),
    ("unknown", "live_stalled", "danger", "supervisor", "error", "maximum_urgency"),
    ("unknown", "failed", "danger", "supervisor", "error", "maximum_urgency"),
    ("unknown", "readiness_exhausted", "danger", "supervisor", "error", "maximum_urgency"),
    ("unknown", "lost_binding", "danger", "supervisor", "error", "maximum_urgency"),
    ("unknown", "unconfirmed", "danger", "supervisor", "error", "maximum_urgency"),
    ("unknown", "unavailable", "attention", "supervisor", "warn", "unavailable_never_healthy"),
    ("attention", "healthy", "attention", "observation", "warn", "healthy_never_erases"),
    ("attention", "grace", "attention", "observation", "warn", "grace_never_erases"),
    ("attention", "advisory", "attention", "observation", "warn", "equal_urgency_observation_tie"),
    ("attention", "live_stalled", "danger", "supervisor", "error", "maximum_urgency"),
    ("attention", "failed", "danger", "supervisor", "error", "maximum_urgency"),
    ("attention", "readiness_exhausted", "danger", "supervisor", "error", "maximum_urgency"),
    ("attention", "lost_binding", "danger", "supervisor", "error", "maximum_urgency"),
    ("attention", "unconfirmed", "danger", "supervisor", "error", "maximum_urgency"),
    ("attention", "unavailable", "attention", "observation", "warn",
     "equal_urgency_observation_tie+facts_remain_visible"),
    ("danger", "healthy", "danger", "observation", "error", "healthy_never_erases"),
    ("danger", "grace", "danger", "observation", "error", "grace_never_erases"),
    ("danger", "advisory", "danger", "observation", "error", "maximum_urgency"),
    ("danger", "live_stalled", "danger", "observation", "error", "equal_urgency_observation_tie"),
    ("danger", "failed", "danger", "observation", "error", "equal_urgency_observation_tie"),
    ("danger", "readiness_exhausted", "danger", "observation", "error",
     "equal_urgency_observation_tie"),
    ("danger", "lost_binding", "danger", "observation", "error", "equal_urgency_observation_tie"),
    ("danger", "unconfirmed", "danger", "observation", "error",
     "equal_urgency_observation_tie"),
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


def test_composition_cross_product_covers_every_declared_semantic_kind() -> None:
    expected = set(product(OBSERVATIONS, SUPERVISOR_URGENCIES))
    actual = {(observation, supervisor) for observation, supervisor, *_ in COMPOSITION_CASES}

    assert actual == expected


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
        ("READINESS_GAVE_UP", "readiness_exhausted"),
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


_SURFACE_NOW = 1_700_000_000.0


class _FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        fixed = datetime.fromtimestamp(_SURFACE_NOW, timezone.utc)
        return fixed if tz is not None else fixed.replace(tzinfo=None)


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def _freeze_surface_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "datetime", _FixedDateTime)
    monkeypatch.setattr(doctor, "datetime", _FixedDateTime)
    monkeypatch.setattr(web, "datetime", _FixedDateTime)
    monkeypatch.setattr(web.time, "time", lambda: _SURFACE_NOW)


def _surface_views(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict, dict, dict, object]:
    _freeze_surface_time(monkeypatch)

    def assessment_failed(*_args, **_kwargs):
        raise ValueError("forced assessment failure")

    monkeypatch.setattr(cli.sup, "build_supervisor_observation", assessment_failed)

    cli_row = cli._gather_status(store)["agents"][0]
    web_root = web.build_state([
        web.RootDescriptor(store=store, label="root"),
    ])["roots"][0]
    web_row = web_root["agents"][0]
    api_status_health = web.status_payload(store)["agent_health"]["alpha"]
    (doctor_check,) = doctor._check_wrapper_child_health(store)
    return cli_row, web_row, api_status_health, doctor_check


def _configured_surface_store(tmp_path, *, health_age: float,
                              heartbeat_age: float | None,
                              ttl_seconds: float,
                              heartbeat_skew_seconds: float,
                              per_agent_health: bool = False) -> Store:
    store = Store(tmp_path)
    store.init(["alpha"])
    updated_at = _iso(_SURFACE_NOW - health_age)
    store.write_health("alpha", health_model.build_snapshot(
        agent="alpha",
        cli="claude",
        mode="wrapper-loop",
        state=health_model.STATE_DEGRADED_OUTPUT,
        updated_at=updated_at,
        since=updated_at,
        reason_code="degraded_output_detected",
    ))
    if heartbeat_age is not None:
        (store.state_dir / "alpha.heartbeat").write_text(
            _iso(_SURFACE_NOW - heartbeat_age), encoding="utf-8")
    health_timing = {
        "ttl_seconds": ttl_seconds,
        "heartbeat_skew_seconds": heartbeat_skew_seconds,
    }
    agent_config = {"wrapped": True, "auto_restart": True}
    supervisor_config = {
        "schema_version": 2,
        "agents": {"alpha": agent_config},
    }
    if per_agent_health:
        agent_config["health"] = health_timing
    else:
        supervisor_config["health"] = health_timing
    (store.dir / "supervisor.json").write_text(
        json.dumps(supervisor_config), encoding="utf-8")
    return store


def test_operator_surfaces_share_configured_health_ttl_on_assessment_failure(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _configured_surface_store(
        tmp_path,
        health_age=400.0,
        heartbeat_age=None,
        ttl_seconds=600.0,
        heartbeat_skew_seconds=30.0,
        per_agent_health=True,
    )

    cli_row, web_row, api_status_health, doctor_check = _surface_views(store, monkeypatch)

    assert cli_row["health"] == web_row["health"] == api_status_health
    assert cli_row["health"]["state"] == "degraded_output"
    assert cli_row["health"]["age_seconds"] == 400.0
    assert cli_row["health"]["stale"] is False
    assert cli_row["health_composition"] == web_row["health_composition"]
    assert cli_row["health_composition"]["urgency"] == "danger"
    assert cli_row["health_composition"]["primary_source"] == "observation"
    assert web_row["wrapped"] is True
    assert doctor_check.status == "error"
    assert doctor_check.details.startswith("self-reported health=degraded_output")


def test_operator_surfaces_share_configured_heartbeat_skew_on_assessment_failure(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _configured_surface_store(
        tmp_path,
        health_age=20.0,
        heartbeat_age=0.0,
        ttl_seconds=600.0,
        heartbeat_skew_seconds=0.0,
    )

    cli_row, web_row, api_status_health, doctor_check = _surface_views(store, monkeypatch)

    assert cli_row["health"] == web_row["health"] == api_status_health
    assert cli_row["health"]["state"] == "unknown"
    assert cli_row["health"]["reason_code"] == "health_older_than_heartbeat"
    assert cli_row["health_composition"] == web_row["health_composition"]
    assert cli_row["health_composition"]["urgency"] == "attention"
    assert cli_row["health_composition"]["primary_source"] == "supervisor"
    assert web_row["wrapped"] is True
    assert doctor_check.status == "warn"
    assert doctor_check.details.startswith("supervisor=UNAVAILABLE(assessment_failed)")
    assert "self-reported health=unknown" in doctor_check.details


def test_operator_surfaces_do_not_guess_when_health_timing_policy_is_invalid(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = Store(tmp_path)
    store.init(["alpha"])
    updated_at = _iso(_SURFACE_NOW - 20.0)
    store.write_health("alpha", health_model.build_snapshot(
        agent="alpha",
        cli="claude",
        mode="wrapper-loop",
        state=health_model.STATE_DEGRADED_OUTPUT,
        updated_at=updated_at,
        since=updated_at,
        reason_code="degraded_output_detected",
    ))
    (store.dir / "supervisor.json").write_text("{not valid json", encoding="utf-8")
    _freeze_surface_time(monkeypatch)

    cli_row = cli._gather_status(store)["agents"][0]
    web_root = web.build_state([
        web.RootDescriptor(store=store, label="root"),
    ])["roots"][0]
    web_row = web_root["agents"][0]
    api_status_health = web.status_payload(store)["agent_health"]["alpha"]
    (doctor_check,) = doctor._check_wrapper_child_health(store)

    assert cli_row["health"] == web_row["health"] == api_status_health
    assert cli_row["health"]["state"] == "unknown"
    assert cli_row["health"]["reason_code"] == "health_timing_policy_unavailable"
    assert doctor_check.name == "wrapper_child_health.supervisor_config"
    assert doctor_check.status == "warn"
    assert "health timing policy is unavailable" in doctor_check.details
