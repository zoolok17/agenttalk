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

#: FIX ROUND 12 (eighth cold read, F8): the closed vocabulary
#: ``stored_assessment_state``/``assessment_state`` actually publish -
#: named here as a live constant, not left implicit in a type-comment,
#: so ``--readiness``/``readiness_state`` (projector.py) has something
#: real to validate an unrecognized value against instead of silently
#: matching nothing.
ASSESSMENT_STATES = ("assessed", "needs_evidence", "blocked", "not_applicable")

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


#: FIX ROUND 13c (reviewer-3's part 2 on round 13b): round 13b's general
#: companion fix (threading a worker-recorded problem into a unit's own
#: adapter_problem_reason(s) even when the file has real declared types)
#: exposed a BLANKET rule here that treated every recorded reason as
#: "could not understand this file" - reviewer-verified against a
#: minimal control: three ordinary classes plus ONE route path written
#: as a constant (route_value_unrecoverable - a narrow, ENTRY-adjacent
#: fact, not a comprehension failure) flipped source_understood UNKNOWN
#: on all four units, a blocker-severity check degraded for an entirely
#: ordinary Java idiom, double-counting one uncertainty as two.
#:
#: An explicit map - not an if-exclusion - of which readiness check(s)
#: each closed reason_code feeds: a reason meaning "this file's content
#: itself could not be confidently processed" feeds source_understood;
#: a narrower, specific-fact reason (a route value, a cli_main
#: signature) feeds its OWN dedicated check instead (or, for the route
#: fail-safes - no dedicated check exists for them yet - feeds nothing;
#: their existing problems.json/route-absence visibility is unchanged).
#: A reason_code THIS MAP HAS NEVER HEARD OF raises (via the plain
#: KeyError below) rather than silently defaulting anywhere - the next
#: new reason a worker/adapter emits must declare its destination here
#: before it can reach any readiness check at all.
#:
#: Honest bound (reviewer-3, round 13d - keep this stated, not implied:
#: overclaimed totality is what round 13 itself was about): this map
#: guards only reasons that reach a UNIT's own ``adapter_problem_
#: reasons`` list - it says nothing about, and is never consulted for,
#: any other reason_code a producer might record elsewhere in the
#: system (a discovery-level problem, a pipeline-level refusal, ...).
#: Its closed vocabulary is exactly modules_artifact.ModuleRecord's own
#: ``adapter_problem_reasons`` contract, nothing wider.
_READINESS_CHECKS_BY_REASON_CODE: dict[str, frozenset[str]] = {
    "parse_failed": frozenset({"source_understood"}),
    "path_excluded": frozenset({"source_understood"}),
    "resource_limit": frozenset({"source_understood"}),
    "non_utf8_path": frozenset({"source_understood"}),
    "case_collision": frozenset({"source_understood"}),
    "no_types_extracted": frozenset({"source_understood"}),
    "route_annotation_unassociated": frozenset(),
    "route_value_unrecoverable": frozenset(),
    "cli_main_unrecognized": frozenset({"entry_points_mapped"}),
}


def _reasons_feeding(check: str, reasons: list[str]) -> list[str]:
    """Every reason in ``reasons`` whose declared destination(s)
    (``_READINESS_CHECKS_BY_REASON_CODE``) include ``check`` - sorted.
    A reason_code absent from that map raises a plain ``KeyError``
    (never a silent no-op) - see the map's own docstring."""
    return sorted(r for r in reasons if check in _READINESS_CHECKS_BY_REASON_CODE[r])


def _check_source_understood(unit: ModuleRecord) -> ReadinessSignal:
    """M-2 (second cold read, fix round 4; CLOSED as a class, third cold
    read, fix round 5): a file with no adapter at all reports ``unknown``,
    not a confident ``unsatisfied`` - the design's own rollup rule draws
    exactly this line: "Any required scan-time blocker that is unsatisfied
    yields `blocked`; any required scan-time UNKNOWN yields
    `needs_evidence`" (DESIGN-55-comprehension-plane.md, Artifact 4, the
    `stored_assessment_state` paragraph). "No adapter exists for this
    file" is an absence of positive evidence, not a positive claim that
    the source is definitely NOT understood.

    ``satisfied`` requires POSITIVE adapter evidence (no recorded reason
    that FEEDS this specific check - see ``_READINESS_CHECKS_BY_REASON_
    CODE`` - meaning a real :class:`~.adapters.java.JavaFileResult`
    exists for this unit) AND a known ``language`` - never derived from
    the mere ABSENCE of a specific, named failure. That inversion is
    what closes the class: a fourth worker failure kind this check has
    never heard of still comes through as unknown (its own reason_code,
    prefixed), because the default without positive evidence is
    unknown, not satisfied."""
    understanding_reasons = _reasons_feeding("source_understood", unit.adapter_problem_reasons)
    if understanding_reasons:
        return _signal(
            unit.unit_id, "source_understood", "unknown", "detected",
            f"adapter_{understanding_reasons[0]}",
        )
    if unit.language != "unknown":
        return _signal(unit.unit_id, "source_understood", "satisfied", "detected", "adapter_understood")
    return _signal(unit.unit_id, "source_understood", "unknown", "detected", "no_adapter_for_language")


#: FIX ROUND 12 (eighth cold read, F2 MAJOR + F5 folded in): the design
#: names this check for "direct INTERNAL dependencies" specifically - an
#: ``invoke`` edge is call-site behavioral evidence, not a declared
#: dependency, and the adapter has no way to recognize a JDK-only
#: qualifier (``Math``, ``String``, ``System``, ...) as external the way
#: an unresolved import IS recognizably external - it can only ever
#: report ``unresolved`` for one. Letting ``invoke`` drive this check
#: made an ORDINARY class calling ordinary JDK methods report
#: dependencies_resolved UNSATISFIED with no real dependency problem at
#: all. ``import``/``inherit`` (source-level) and ``build`` (declared
#: Maven artifact dependencies) are the relations that actually assert a
#: direct dependency; ``test``/``route``/``data``/``configuration`` are
#: each covered by their own dedicated check (or are unsupported this
#: slice) and never belong here either.
_DEPENDENCY_RESOLUTION_RELATIONS = frozenset({"import", "inherit", "build"})


def _check_dependencies_resolved(unit: ModuleRecord, outgoing: list[DependencyRecord]) -> ReadinessSignal:
    relevant = [edge for edge in outgoing if edge.relation in _DEPENDENCY_RESOLUTION_RELATIONS]
    if not relevant:
        # FIX ROUND 12b (reviewer-3 delta on round 12): renamed from
        # "no_dependencies" - that wording, read alone by a #208 consumer,
        # claims a unit depends on nothing at all. Since round 12 scoped
        # this check to import/inherit/build, a unit whose ONLY edges are
        # scoped-out invoke noise also lands here - it may have plenty of
        # real behavioral dependencies, just none of the kinds this check
        # evaluates. "no_declared_dependencies" names what was actually
        # checked, not a claim about the unit's real dependency surface.
        return _signal(
            unit.unit_id, "dependencies_resolved", "satisfied", "detected",
            "no_declared_dependencies")
    states = {edge.resolution_state for edge in relevant}
    if "ambiguous" in states:
        return _signal(unit.unit_id, "dependencies_resolved", "unknown", "detected", "ambiguous_dependency")
    if "unresolved" in states:
        return _signal(
            unit.unit_id, "dependencies_resolved", "unsatisfied", "detected", "unresolved_dependency")
    return _signal(unit.unit_id, "dependencies_resolved", "satisfied", "detected", "dependencies_resolved")


#: FIX ROUND 14 (tenth cold read, CR10-1 MAJOR): an ``import`` edge is
#: now attributed to its FILE unit, never a declared type (the adapter
#: fix - java.py's ``file_scope_qualified`` - closes the false-
#: attribution half; this closes the readiness half). A component with
#: NO edges of its own used to report a confident satisfied/
#: no_declared_dependencies regardless of whether its file actually
#: has real, unattributed import evidence - an un-evidenced positive.
#: A single-top-level-type file has no attribution ambiguity at all
#: (there is only one possible owner for the file's own imports), so
#: the component's status honestly MIRRORS the file's own aggregate
#: resolution outcome; a multi-type file's import evidence cannot be
#: honestly credited to any ONE sibling, so it degrades to unknown with
#: a named reason instead of guessing.
def _check_dependencies_resolved_for_component(
    unit: ModuleRecord, own_outgoing: list[DependencyRecord], file_unit_id: str | None,
    children_by_container: dict[str, list[str]], outgoing_by_unit: dict[str, list[DependencyRecord]],
) -> ReadinessSignal:
    own_relevant = [e for e in own_outgoing if e.relation in _DEPENDENCY_RESOLUTION_RELATIONS]
    if own_relevant or file_unit_id is None:
        return _check_dependencies_resolved(unit, own_outgoing)
    file_outgoing = outgoing_by_unit.get(file_unit_id, [])
    file_relevant = [e for e in file_outgoing if e.relation in _DEPENDENCY_RESOLUTION_RELATIONS]
    if not file_relevant:
        return _check_dependencies_resolved(unit, own_outgoing)
    top_level_siblings = len(children_by_container.get(file_unit_id, []))
    if top_level_siblings <= 1:
        return _check_dependencies_resolved(unit, file_outgoing)
    return _signal(
        unit.unit_id, "dependencies_resolved", "unknown", "detected",
        "file_scoped_dependencies_not_attributed")


def _check_dependencies_resolved_for_file(
    unit: ModuleRecord, direct_outgoing: list[DependencyRecord], contained_unit_ids: set[str],
    outgoing_by_unit: dict[str, list[DependencyRecord]],
) -> ReadinessSignal:
    """N6 (fourth cold read, fix round 6): edges attach to the declared
    TYPE a call/import/route site lives in, never to the FILE that
    happens to contain it (M-4, round 5's own containment fix didn't
    change edge attribution) - a Java file's own "file" unit therefore
    NEVER receives outgoing edges directly and always reported
    dependencies_resolved satisfied/no_declared_dependencies, a structurally
    always-on positive signal that was never actually evidence of
    anything. Derives the file's own signal from the UNION of its
    contained units' edges instead (recursing through nested types),
    PLUS any edge attributed directly to the file itself (the pom.xml/
    web.xml shape, where build/route edges attach to the file unit
    directly - there being no component-level unit for those producers).
    A file with neither any contained unit nor any direct edge of its
    own (a plain non-code file, or one the adapter never understood) is
    ``not_applicable`` - the concept of "dependencies" does not
    meaningfully apply to it, so a confident positive would be exactly
    the un-evidenced satisfied this check exists to avoid."""
    if not contained_unit_ids and not direct_outgoing:
        return _signal(
            unit.unit_id, "dependencies_resolved", "not_applicable", "detected", "no_contained_units")
    outgoing = list(direct_outgoing)
    for contained_id in contained_unit_ids:
        outgoing.extend(outgoing_by_unit.get(contained_id, []))
    return _check_dependencies_resolved(unit, outgoing)


def _check_entry_points_mapped(unit: ModuleRecord, has_entry_point: bool) -> ReadinessSignal:
    if has_entry_point:
        return _signal(unit.unit_id, "entry_points_mapped", "satisfied", "detected", "entry_point_mapped")
    # FIX ROUND 13b (reviewer-3's B1 class-closer), routed via the
    # explicit reason-class map (round 13c): a method literally named
    # main that the adapter's strict cli_main detector could not
    # confidently classify (recorded as a "cli_main_unrecognized"
    # problem, now ATTRIBUTED to this ONE unit specifically - see
    # modules_artifact.build_modules's worker_problem_reasons_by_unit)
    # must feed UNKNOWN here, never the confident "no entry point"
    # negative - the same three-state move round 11 already made for an
    # unrecoverable route value. No entry point is ever published for
    # this shape either way (a private/instance helper coincidentally
    # named "main" is never claimed as a real one) - only the
    # CONFIDENCE of the negative changes.
    entry_point_reasons = _reasons_feeding("entry_points_mapped", unit.adapter_problem_reasons)
    if entry_point_reasons:
        return _signal(
            unit.unit_id, "entry_points_mapped", "unknown", "detected", entry_point_reasons[0])
    return _signal(
        unit.unit_id, "entry_points_mapped", "not_applicable", "detected", "no_entry_point")


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

    # N6 (fourth cold read, fix round 6): direct children only (container_
    # unit_id -> unit_id) - _transitive_descendants below walks this to
    # collect a file's full descendant set, since a nested type's own
    # container is its OUTER type, not the file directly.
    children_by_container: dict[str, list[str]] = {}
    for m in modules:
        if m.container_unit_id is not None:
            children_by_container.setdefault(m.container_unit_id, []).append(m.unit_id)

    def _transitive_descendants(root_id: str) -> set[str]:
        seen: set[str] = set()
        stack = list(children_by_container.get(root_id, []))
        while stack:
            candidate = stack.pop()
            if candidate in seen:
                continue
            seen.add(candidate)
            stack.extend(children_by_container.get(candidate, []))
        return seen

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

    # FIX ROUND 14 (CR10-1): resolves a component's OWNING file, walking
    # up through nested-type containment (a nested type's own container
    # is its outer type, never the file directly - N6/round 6) until a
    # "file"-kind unit is reached.
    module_by_id = {m.unit_id: m for m in modules}

    def _owning_file_unit_id(unit: ModuleRecord) -> str | None:
        current = unit
        while current.container_unit_id is not None:
            parent = module_by_id.get(current.container_unit_id)
            if parent is None:
                return None
            if parent.kind == "file":
                return parent.unit_id
            current = parent
        return None

    all_signals: list[ReadinessSignal] = []
    summaries: list[UnitReadinessSummary] = []

    for unit in modules:
        if unit.kind == "file":
            dependencies_signal = _check_dependencies_resolved_for_file(
                unit, outgoing_by_unit.get(unit.unit_id, []),
                _transitive_descendants(unit.unit_id), outgoing_by_unit,
            )
        else:
            dependencies_signal = _check_dependencies_resolved_for_component(
                unit, outgoing_by_unit.get(unit.unit_id, []), _owning_file_unit_id(unit),
                children_by_container, outgoing_by_unit,
            )
        unit_signals = [
            _check_source_understood(unit),
            dependencies_signal,
            _check_entry_points_mapped(unit, unit.unit_id in entry_point_owner_ids),
            _check_feature_linked(unit, feature_states_by_unit.get(unit.unit_id, [])),
            _check_test_evidence_located(unit, unit.unit_id in tested_unit_ids),
            _check_boundaries_identified(unit),
        ]
        all_signals.extend(unit_signals)
        summaries.append(UnitReadinessSummary(
            unit_id=unit.unit_id, stored_assessment_state=_rollup(unit_signals)))

    return all_signals, summaries
