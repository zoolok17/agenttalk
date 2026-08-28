"""``readiness.json`` record assembly (DESIGN-55-comprehension-plane.md,
Artifact 4: migration-readiness signals).

"readiness.json records a signal matrix per unit. It deliberately does not
store a percentage or an opaque 'migration score'." The default policy
covers exactly six checks named in the design's "Artifact 4" section; this
module implements all six against whatever items 4-6 already assembled
(modules, dependencies, features) - no new extraction, purely a policy
evaluation over already-normalized records.

Severity assignment per check is this module's own judgment call (the
design names the three severities and the six checks, but does not pin
one to the other) - flagged for review, not a blocking fork:
``source_understood`` is a blocker (nothing else can be assessed without
it); ``dependencies_resolved`` and ``feature_linked`` are warnings;
``entry_points_mapped``, ``test_evidence_located``, and
``boundaries_identified`` are informational. Adjust if review disagrees;
nothing downstream is coupled to these specific values yet.

``boundaries_identified`` is ALWAYS ``unknown`` this slice, for every unit,
with ``basis: "detected"`` never claimed - the design's own honesty rule:
this check needs the ``data``/``configuration`` relations item 3 names as
explicit coverage gaps (``UNSUPPORTED_RELATIONS``), so no unit can
honestly claim this evidence exists yet.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import digests
from .dependencies_artifact import DependencyRecord
from .features_artifact import FeatureRecord
from .modules_artifact import ModuleRecord

POLICY_VERSION = 1
POLICY_ID = "agenttalk.comprehension.readiness.default_policy"

CHECKS = (
    "source_understood",
    "dependencies_resolved",
    "entry_points_mapped",
    "feature_linked",
    "test_evidence_located",
    "boundaries_identified",
)

_SEVERITY_BY_CHECK = {
    "source_understood": "blocker",
    "dependencies_resolved": "warning",
    "entry_points_mapped": "information",
    "feature_linked": "warning",
    "test_evidence_located": "information",
    "boundaries_identified": "information",
}


@dataclass(frozen=True)
class ReadinessSignal:
    signal_id: str
    unit_id: str
    check: str
    stored_status: str  # "satisfied" | "unsatisfied" | "unknown" | "not_applicable"
    severity: str
    basis: str  # "detected" | "declared" | "verified_external_evidence"
    reason_code: str
    confidence: str | None = None
    producers: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    policy: dict[str, Any] = field(default_factory=lambda: {
        "policy_id": POLICY_ID, "policy_version": POLICY_VERSION,
    })

    def to_json(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "unit_id": self.unit_id,
            "check": self.check,
            "stored_status": self.stored_status,
            "severity": self.severity,
            "basis": self.basis,
            "confidence": self.confidence,
            "reason_code": self.reason_code,
            "evidence": self.evidence,
            "policy": self.policy,
            "producers": self.producers,
        }


@dataclass(frozen=True)
class UnitReadinessSummary:
    unit_id: str
    stored_assessment_state: str  # "assessed" | "needs_evidence" | "blocked" | "not_applicable"

    def to_json(self) -> dict[str, Any]:
        return {"unit_id": self.unit_id, "stored_assessment_state": self.stored_assessment_state}


def readiness_signal_from_json(payload: dict[str, Any]) -> ReadinessSignal:
    return ReadinessSignal(
        signal_id=payload["signal_id"], unit_id=payload["unit_id"], check=payload["check"],
        stored_status=payload["stored_status"], severity=payload["severity"],
        basis=payload["basis"], reason_code=payload["reason_code"],
        confidence=payload.get("confidence"), producers=list(payload.get("producers", [])),
        evidence=list(payload.get("evidence", [])), policy=dict(payload.get("policy", {})),
    )


def unit_readiness_summary_from_json(payload: dict[str, Any]) -> UnitReadinessSummary:
    return UnitReadinessSummary(
        unit_id=payload["unit_id"], stored_assessment_state=payload["stored_assessment_state"],
    )


def _signal(unit_id: str, check: str, stored_status: str, basis: str, reason_code: str) -> ReadinessSignal:
    return ReadinessSignal(
        signal_id=digests.signal_id(unit_id=unit_id, check=check, policy_version=POLICY_VERSION),
        unit_id=unit_id, check=check, stored_status=stored_status,
        severity=_SEVERITY_BY_CHECK[check], basis=basis, reason_code=reason_code,
    )


def _check_source_understood(unit: ModuleRecord) -> ReadinessSignal:
    """M-2 (second cold read, fix round 4): a file with no adapter at all
    reports ``unknown``, not a confident ``unsatisfied`` - the design's
    own rollup rule draws exactly this line: "Any required scan-time
    blocker that is unsatisfied yields `blocked`; any required scan-time
    UNKNOWN yields `needs_evidence`" (DESIGN-55-comprehension-plane.md,
    Artifact 4, the `stored_assessment_state` paragraph). "No adapter
    exists for this file" is an absence of positive evidence, not a
    positive claim that the source is definitely NOT understood - on a
    real repo, most files are non-code (docs, config, `.gitignore`), and
    reporting a confident blocker for every one of them would make
    `blocked` the default headline state for most units, a stronger
    negative than this slice's own "nothing reaches assessed" narrative
    (every unit's rollup routes through the same any-required-unknown
    path `boundaries_identified` already keeps permanently open this
    slice)."""
    if unit.adapter_parse_failed:
        # B3 (cold-read, PR-B fix round 3): the adapter attempted (or the
        # worker could not even read the bytes) and failed - also
        # genuinely unknown, for the same reason as the no-adapter case
        # below, never a confident "satisfied" merely because the
        # extension maps to a known language.
        return _signal(unit.unit_id, "source_understood", "unknown", "detected", "adapter_parse_failed")
    if unit.language != "unknown":
        return _signal(unit.unit_id, "source_understood", "satisfied", "detected", "adapter_understood")
    return _signal(unit.unit_id, "source_understood", "unknown", "detected", "no_adapter_for_language")


def _check_dependencies_resolved(unit: ModuleRecord, outgoing: list[DependencyRecord]) -> ReadinessSignal:
    if not outgoing:
        return _signal(unit.unit_id, "dependencies_resolved", "satisfied", "detected", "no_dependencies")
    states = {edge.resolution_state for edge in outgoing}
    if "ambiguous" in states:
        return _signal(unit.unit_id, "dependencies_resolved", "unknown", "detected", "ambiguous_dependency")
    if "unresolved" in states:
        return _signal(
            unit.unit_id, "dependencies_resolved", "unsatisfied", "detected", "unresolved_dependency")
    return _signal(unit.unit_id, "dependencies_resolved", "satisfied", "detected", "dependencies_resolved")


def _check_entry_points_mapped(unit: ModuleRecord, has_entry_point: bool) -> ReadinessSignal:
    if not has_entry_point:
        return _signal(
            unit.unit_id, "entry_points_mapped", "not_applicable", "detected", "no_entry_point")
    return _signal(unit.unit_id, "entry_points_mapped", "satisfied", "detected", "entry_point_mapped")


def _check_feature_linked(unit: ModuleRecord, feature_states: list[str]) -> ReadinessSignal:
    if not feature_states:
        return _signal(unit.unit_id, "feature_linked", "unsatisfied", "detected", "no_feature_link")
    if "confirmed" in feature_states:
        return _signal(unit.unit_id, "feature_linked", "satisfied", "declared", "feature_confirmed")
    return _signal(unit.unit_id, "feature_linked", "unknown", "detected", "feature_not_confirmed")


def _check_test_evidence_located(unit: ModuleRecord, is_tested: bool) -> ReadinessSignal:
    if "test" in unit.classification or is_tested:
        return _signal(
            unit.unit_id, "test_evidence_located", "satisfied", "detected", "test_evidence_located")
    return _signal(unit.unit_id, "test_evidence_located", "unknown", "detected", "no_test_evidence_found")


def _check_boundaries_identified(unit: ModuleRecord) -> ReadinessSignal:
    return _signal(
        unit.unit_id, "boundaries_identified", "unknown", "detected",
        "data_and_configuration_relations_unsupported_this_slice",
    )


def _rollup(signals: list[ReadinessSignal]) -> str:
    applicable = [s for s in signals if s.stored_status != "not_applicable"]
    if not applicable:
        return "not_applicable"
    if any(s.severity == "blocker" and s.stored_status == "unsatisfied" for s in applicable):
        return "blocked"
    if any(s.stored_status == "unknown" for s in applicable):
        return "needs_evidence"
    return "assessed"


def build_readiness(
    modules: list[ModuleRecord],
    dependencies: list[DependencyRecord],
    features: list[FeatureRecord],
) -> tuple[list[ReadinessSignal], list[UnitReadinessSummary]]:
    outgoing_by_unit: dict[str, list[DependencyRecord]] = {}
    for edge in dependencies:
        outgoing_by_unit.setdefault(edge.from_unit_id, []).append(edge)

    tested_unit_ids = {
        edge.target_unit_id for edge in dependencies
        if edge.relation == "test" and edge.target_unit_id is not None
    }

    entry_point_owner_ids = {
        unit_id for feature in features for unit_id in feature.unit_ids
    }
    feature_states_by_unit: dict[str, list[str]] = {}
    for feature in features:
        for unit_id in feature.unit_ids:
            feature_states_by_unit.setdefault(unit_id, []).append(feature.state)

    all_signals: list[ReadinessSignal] = []
    summaries: list[UnitReadinessSummary] = []

    for unit in modules:
        unit_signals = [
            _check_source_understood(unit),
            _check_dependencies_resolved(unit, outgoing_by_unit.get(unit.unit_id, [])),
            _check_entry_points_mapped(unit, unit.unit_id in entry_point_owner_ids),
            _check_feature_linked(unit, feature_states_by_unit.get(unit.unit_id, [])),
            _check_test_evidence_located(unit, unit.unit_id in tested_unit_ids),
            _check_boundaries_identified(unit),
        ]
        all_signals.extend(unit_signals)
        summaries.append(UnitReadinessSummary(
            unit_id=unit.unit_id, stored_assessment_state=_rollup(unit_signals)))

    return all_signals, summaries
