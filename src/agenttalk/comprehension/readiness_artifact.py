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

FIX ROUND 14 (tenth cold read, CR10-11, declared): the same permanent
``unknown`` makes THREE of ``ASSESSMENT_STATES``'s four values structurally
unreachable this slice, not just one. ``assessed`` cannot be reached (a
permanently-unknown signal always exists); ``blocked`` cannot be reached
(the ONLY blocker-severity check, ``source_understood``, never returns
``unsatisfied`` - an absence of adapter evidence is unknown, never a
confident negative, since round 5); and ``not_applicable`` cannot be
reached either (``_rollup``'s own ``not_applicable`` branch requires EVERY
signal to be ``not_applicable``, which can never happen while
``boundaries_identified`` is unconditionally ``unknown``). Practical
consequence, stated plainly rather than left to be independently
discovered: ``assessment_state`` is currently a CONSTANT
(``needs_evidence``) for every unit this slice, however evidenced or
unevidenced its own individual signals are - it carries no discriminating
information yet. This will change the moment a later slice adds the
``data``/``configuration`` producer ``boundaries_identified`` needs.
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

#: FIX ROUND 16 (twelfth cold read, N2 MINOR): CR10-11's own finding
#: (``assessment_state`` is currently a CONSTANT - ``needs_evidence`` -
#: for every unit this slice, since ``boundaries_identified`` has no
#: producer yet and is unconditionally ``unknown``) lived only in this
#: module's own docstring and in two test docstrings - a reader of
#: readiness.json alone (or the projection) had no way to discover it
#: without already knowing to check the source. Published as a real
#: field (``scan_pipeline.py`` writes it onto readiness.json) rather
#: than left implicit, the same "declare the honest gap, don't bury it"
#: discipline ``freshness``'s own ``not_evaluated``/named-reason-code
#: shape already follows.
ASSESSMENT_STATE_CAVEAT = (
    "assessment_state is currently a constant (needs_evidence) for every "
    "unit this slice - boundaries_identified has no producer yet (the "
    "data/configuration relations item 3 names as explicit coverage "
    "gaps) and is unconditionally unknown, non-blocker severity, so no "
    "unit can roll up to assessed or not_applicable until a later slice "
    "adds that producer. blocked is equally unreachable this slice, for "
    "a separate reason: the only blocker-severity check, "
    "source_understood, never returns unsatisfied - an absence of "
    "adapter evidence is unknown, never a confident negative."
)

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
#: FIX ROUND 16 (twelfth cold read, M2 MAJOR, wrong-data): every reason
#: meaning "this file's content itself could not be confidently
#: processed" used to feed ONLY source_understood - dependencies_resolved
#: and entry_points_mapped kept computing their own confident answer from
#: whatever (necessarily empty, since nothing ever looked) evidence
#: existed, publishing a wrong confident positive/negative rather than
#: honestly propagating the SAME evidence gap. Widened to feed all three
#: - a reader who trusts source_understood=unknown must not then find
#: two adjacent checks on the identical unit confidently answered from a
#: parsed prefix that never actually existed. ``route_annotation_
#: unassociated``/``route_value_unrecoverable``/``cli_main_unrecognized``
#: stay narrowly scoped (unchanged) - each is a fact about ONE already-
#: understood construct within a file the adapter DID successfully
#: parse, never a whole-file evidence gap.
_READINESS_CHECKS_BY_REASON_CODE: dict[str, frozenset[str]] = {
    "parse_failed": frozenset({"source_understood", "dependencies_resolved", "entry_points_mapped"}),
    "path_excluded": frozenset({"source_understood", "dependencies_resolved", "entry_points_mapped"}),
    "resource_limit": frozenset({"source_understood", "dependencies_resolved", "entry_points_mapped"}),
    "non_utf8_path": frozenset({"source_understood", "dependencies_resolved", "entry_points_mapped"}),
    "case_collision": frozenset({"source_understood", "dependencies_resolved", "entry_points_mapped"}),
    "no_types_extracted": frozenset({
        "source_understood", "dependencies_resolved", "entry_points_mapped"}),
    # FIX ROUND 20 (sixteenth cold read, M3 MAJOR, wrong-data): these
    # two used to feed NOTHING at all - round 13c's own scoping (away
    # from source_understood, a whole-file evidence gap these are NOT)
    # overshot into feeding zero checks whatsoever, rather than the
    # narrower, entry-adjacent check they actually ARE evidence about.
    # A path-constants class (@Path(ApiPaths.ORDERS), a common idiom)
    # published entry_points_mapped/feature_linked as CONFIDENT
    # NEGATIVES while the run itself recorded it could not read the
    # route - the same "trust the recorded gap, don't recompute a
    # confident answer from evidence that never existed" principle
    # round 16's own M2 already applied to source_understood/
    # dependencies_resolved/entry_points_mapped, extended to these two
    # reasons' own correct, narrower destination.
    "route_annotation_unassociated": frozenset({"entry_points_mapped", "feature_linked"}),
    "route_value_unrecoverable": frozenset({"entry_points_mapped", "feature_linked"}),
    "cli_main_unrecognized": frozenset({"entry_points_mapped"}),
    # FIX ROUND 17 (thirteenth cold read, CR13-3 MAJOR, part (b) - THE
    # CLASS-CLOSER): a class carrying a recognized-but-unsupported
    # route-like annotation (JAX-WS's own @WebMethod) must feed
    # entry_points_mapped UNKNOWN - never the confident not_applicable/
    # no_entry_point negative a class that genuinely serves no route at
    # all correctly gets.
    "unsupported_entry_point_shape": frozenset({"entry_points_mapped"}),
    # FIX ROUND 14 (tenth cold read, CR10-5 JUDGE, completeness): a
    # recognized-but-unsupported source language (worker.py) is exactly
    # "this file's content itself could not be confidently processed" -
    # the same bucket parse_failed/no_types_extracted already feed.
    "unsupported_language": frozenset({
        "source_understood", "dependencies_resolved", "entry_points_mapped"}),
}


def _reasons_feeding(check: str, reasons: list[str]) -> list[str]:
    """Every reason in ``reasons`` whose declared destination(s)
    (``_READINESS_CHECKS_BY_REASON_CODE``) include ``check`` - sorted.
    A reason_code absent from that map raises a plain ``KeyError``
    (never a silent no-op) - see the map's own docstring."""
    return sorted(r for r in reasons if check in _READINESS_CHECKS_BY_REASON_CODE[r])


#: FIX ROUND 16c (reviewer-3's LOW on round 16b - "take it, it names the
#: exact silent-divergence defect just fixed"): the "adapter_" prefix
#: rule used to live in TWO separately-maintained formulations -
#: ``_check_source_understood``/``_check_dependencies_resolved`` (and its
#: file-kind sibling) prefixed UNCONDITIONALLY (every reason reaching
#: them, by construction, already feeds source_understood too), while
#: ``_check_entry_points_mapped`` tested map membership explicitly (since
#: it can ALSO receive a narrowly-scoped reason - ``cli_main_unrecognized``
#: - that must stay bare). The two formulations agreed today only by
#: coincidence of the CURRENT reason set; nothing enforced that a future
#: reason feeding both dependencies_resolved and entry_points_mapped
#: (without also feeding source_understood) would get the SAME spelling
#: from both. One shared predicate now, called by every check that might
#: report a propagated whole-file evidence gap - source_understood always
#: qualifies by construction (a reason reaches it only because it feeds
#: source_understood in the first place), so this is a safe drop-in for
#: its own unconditional prefix too, not just entry_points_mapped's
#: guarded one.
def _propagated_reason_spelling(reason: str) -> str:
    """A reason that ALSO feeds source_understood is a PROPAGATED
    whole-file evidence gap - the identical fact about the identical
    unit, spelled the SAME way on every check it reaches
    (``adapter_X``). A reason that does not (``cli_main_unrecognized``,
    ``route_annotation_unassociated``, ...) is NATIVE to whichever
    narrower check it feeds and stays bare - the same spelling
    ``problems.json``'s own ``reason_code`` already uses for it, an
    existing join key a reader relies on to correlate a signal with its
    concrete problem row."""
    if "source_understood" in _READINESS_CHECKS_BY_REASON_CODE[reason]:
        return f"adapter_{reason}"
    return reason


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
            _propagated_reason_spelling(understanding_reasons[0]),
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


#: FIX ROUND 15 (eleventh cold read, M6 JUDGE - taken): round 12's F2
#: scoped this check away from ``invoke`` specifically to stop ordinary
#: JDK/library calls (``Math.max``, ``String.valueOf``) - which always
#: resolve ``unresolved`` (a java.lang/javax call has ZERO in-scan
#: candidates to tie on) - from crying a false ``unsatisfied`` on
#: entirely healthy code. An ``ambiguous`` resolution is a fundamentally
#: DIFFERENT claim: it only ever fires when the scanner found 2+ REAL,
#: same-simple-name in-scan candidates and genuinely could not tell
#: which one a call targets - never JDK noise, always a substantive
#: uncertainty about this codebase's own structure. A unit whose only
#: cross-unit dependency happens to be an ambiguous invoke/inherit
#: reported satisfied/no_declared_dependencies - an honest reason code
#: over a real unknown. Checked across relations regardless of
#: ``_DEPENDENCY_RESOLUTION_RELATIONS`` scoping, EXCEPT ``test``: a test
#: edge is a name-derived CONVENTION GUESS (F4), never a real declared
#: dependency of the unit it is attached to - its own ambiguity is a
#: fact about the pairing guess, not about this unit's dependency
#: surface, and must not flip an otherwise-unrelated check.
_AMBIGUOUS_DEPENDENCY_EXCLUDED_RELATIONS = frozenset({"test"})


def _check_dependencies_resolved(
    unit: ModuleRecord, outgoing: list[DependencyRecord], externality_poisoned: bool = False,
) -> ReadinessSignal:
    # FIX ROUND 16 (twelfth cold read, M2 MAJOR, wrong-data): mirrors
    # _check_source_understood's own "no positive claim without positive
    # evidence" discipline - a file the adapter never successfully read
    # or parsed (or degraded away entirely) has ZERO real edges to
    # examine below not because it genuinely declares no dependencies,
    # but because nothing ever looked. A confident satisfied/
    # no_declared_dependencies over an EVIDENCE GAP is exactly the
    # un-evidenced positive this check exists to avoid.
    understanding_reasons = _reasons_feeding("dependencies_resolved", unit.adapter_problem_reasons)
    if understanding_reasons:
        return _signal(
            unit.unit_id, "dependencies_resolved", "unknown", "detected",
            _propagated_reason_spelling(understanding_reasons[0]),
        )
    relevant = [edge for edge in outgoing if edge.relation in _DEPENDENCY_RESOLUTION_RELATIONS]
    any_ambiguous = any(
        edge.resolution_state == "ambiguous"
        and edge.relation not in _AMBIGUOUS_DEPENDENCY_EXCLUDED_RELATIONS
        for edge in outgoing
    )
    if any_ambiguous:
        return _signal(unit.unit_id, "dependencies_resolved", "unknown", "detected", "ambiguous_dependency")
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
    unresolved_edges = [edge for edge in relevant if edge.resolution_state == "unresolved"]
    if unresolved_edges:
        # FIX ROUND 20c (readiness carry, inherited from round 20 - THE
        # MAJOR): on a poisoned run, a healthy unit whose ONLY unresolved
        # edges are externality misses (org.slf4j, ...) used to publish
        # UNSATISFIED/unresolved_dependency - a blocker-severity
        # we-looked-and-found-a-deficiency claim, when this producer
        # actually ABSTAINED from a positive external claim because this
        # run's own external surface is unknown (round 20's own POISON
        # RULE). Distinguished per-edge via DependencyRecord.
        # externality_suppressed (set ONLY by the poison branch of
        # _classify_registry_miss/its wildcard twin, never by a genuine
        # registry miss) - never guessed from resolution_state alone,
        # which cannot tell the two apart. A unit that ALSO has a genuine
        # unresolved dependency (externality_suppressed=False on at
        # least one edge) keeps the existing UNSATISFIED claim - that
        # claim is still true and wins.
        if externality_poisoned and all(edge.externality_suppressed for edge in unresolved_edges):
            return _signal(
                unit.unit_id, "dependencies_resolved", "unknown", "detected",
                "externality_suppressed")
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
    externality_poisoned: bool = False,
) -> ReadinessSignal:
    own_relevant = [e for e in own_outgoing if e.relation in _DEPENDENCY_RESOLUTION_RELATIONS]
    if own_relevant or file_unit_id is None:
        return _check_dependencies_resolved(unit, own_outgoing, externality_poisoned)
    file_outgoing = outgoing_by_unit.get(file_unit_id, [])
    file_relevant = [e for e in file_outgoing if e.relation in _DEPENDENCY_RESOLUTION_RELATIONS]
    if not file_relevant:
        return _check_dependencies_resolved(unit, own_outgoing, externality_poisoned)
    top_level_siblings = len(children_by_container.get(file_unit_id, []))
    if top_level_siblings <= 1:
        return _check_dependencies_resolved(unit, file_outgoing, externality_poisoned)
    return _signal(
        unit.unit_id, "dependencies_resolved", "unknown", "detected",
        "file_scoped_dependencies_not_attributed")


def _check_dependencies_resolved_for_file(
    unit: ModuleRecord, direct_outgoing: list[DependencyRecord], contained_unit_ids: set[str],
    outgoing_by_unit: dict[str, list[DependencyRecord]], externality_poisoned: bool = False,
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
    the un-evidenced satisfied this check exists to avoid.

    FIX ROUND 16 (twelfth cold read, M2 MAJOR, wrong-data): a parse-
    failed (or otherwise adapter-never-ran) file has NO contained units
    and NO direct outgoing edges for the exact same "nothing ever
    looked" reason a healthy, genuinely typeless file does - the two
    were indistinguishable here, so the confident not_applicable/
    no_contained_units default below used to cover BOTH, an
    un-evidenced claim exactly like the satisfied one this check
    already guards against elsewhere. Checked FIRST, unconditionally -
    "no contained units" is not independently knowable when the parse
    that would have found them never completed."""
    understanding_reasons = _reasons_feeding("dependencies_resolved", unit.adapter_problem_reasons)
    if understanding_reasons:
        return _signal(
            unit.unit_id, "dependencies_resolved", "unknown", "detected",
            _propagated_reason_spelling(understanding_reasons[0]),
        )
    if not contained_unit_ids and not direct_outgoing:
        return _signal(
            unit.unit_id, "dependencies_resolved", "not_applicable", "detected", "no_contained_units")
    outgoing = list(direct_outgoing)
    for contained_id in contained_unit_ids:
        outgoing.extend(outgoing_by_unit.get(contained_id, []))
    return _check_dependencies_resolved(unit, outgoing, externality_poisoned)


def _check_entry_points_mapped(unit: ModuleRecord, has_entry_point: bool) -> ReadinessSignal:
    # FIX ROUND 13b (reviewer-3's B1 class-closer), routed via the
    # explicit reason-class map (round 13c): a method literally named
    # main that the adapter's strict cli_main detector could not
    # confidently classify (recorded as a "cli_main_unrecognized"
    # problem, now ATTRIBUTED to this ONE unit specifically - see
    # modules_artifact.build_modules's worker_problem_reasons_by_unit)
    # must feed UNKNOWN here, never the confident "no entry point"
    # negative - the same three-state move round 11 already made for an
    # unrecoverable route value.
    #
    # FIX ROUND 18 (fourteenth cold read, F2 MAJOR, wrong-data): this
    # reason check used to run ONLY when ``has_entry_point`` was already
    # False - true for every reason this map fed here UNTIL this round
    # (each one's own genuine entry point count was always exactly
    # zero), but a MIXED JAX-RS class breaks that assumption: it
    # publishes a real, composed route for ONE method while ALSO
    # carrying an attributed unsupported_entry_point_shape problem for
    # ANOTHER, uncomposed one in the SAME unit - the old ordering let
    # the genuine route win outright, publishing the confident
    # SATISFIED negative over a unit two-thirds unmapped. An attributed
    # reason now always wins over a bare has_entry_point=True - never
    # masked by an unrelated real entry point elsewhere in the same
    # unit - checked first, unconditionally.
    entry_point_reasons = _reasons_feeding("entry_points_mapped", unit.adapter_problem_reasons)
    if entry_point_reasons:
        # FIX ROUND 16c (reviewer-3's LOW on round 16b): routed through
        # the shared `_propagated_reason_spelling` predicate - see its
        # own docstring for why this must never be a second, separately-
        # maintained formulation of the identical rule.
        return _signal(
            unit.unit_id, "entry_points_mapped", "unknown", "detected",
            _propagated_reason_spelling(entry_point_reasons[0]),
        )
    if has_entry_point:
        return _signal(unit.unit_id, "entry_points_mapped", "satisfied", "detected", "entry_point_mapped")
    return _signal(
        unit.unit_id, "entry_points_mapped", "not_applicable", "detected", "no_entry_point")


def _check_feature_linked(unit: ModuleRecord, feature_states: list[str]) -> ReadinessSignal:
    # FIX ROUND 20 (sixteenth cold read, M3 MAJOR, wrong-data): a class
    # whose own route the adapter could not read (route_value_
    # unrecoverable/route_annotation_unassociated - see
    # _READINESS_CHECKS_BY_REASON_CODE) used to report the CONFIDENT
    # negative "no feature link" for the identical reason
    # entry_points_mapped now reports unknown for - the same evidence
    # gap, two disagreeing confidences about it. Checked first, the
    # same "an attributed reason wins over a bare positive/negative
    # signal" discipline _check_entry_points_mapped already follows.
    feature_link_reasons = _reasons_feeding("feature_linked", unit.adapter_problem_reasons)
    if feature_link_reasons:
        return _signal(
            unit.unit_id, "feature_linked", "unknown", "detected",
            _propagated_reason_spelling(feature_link_reasons[0]),
        )
    if not feature_states:
        return _signal(unit.unit_id, "feature_linked", "unsatisfied", "detected", "no_feature_link")
    if "confirmed" in feature_states:
        return _signal(unit.unit_id, "feature_linked", "satisfied", "declared", "feature_confirmed")
    return _signal(unit.unit_id, "feature_linked", "unknown", "detected", "feature_not_confirmed")


def _check_test_evidence_located(
    unit: ModuleRecord, is_tested: bool, has_inferred_pairing: bool,
) -> ReadinessSignal:
    # FIX ROUND 14 (tenth cold read, CR10-7 MINOR, wrong-data - the
    # tautology half): a unit classified "test" used to satisfy THIS
    # check about ITSELF, which is meaningless - "test evidence located"
    # asks whether a PRODUCTION unit has a test pairing with it; a test
    # class is not a production unit that could ever need one. Never
    # applicable to a test unit's own record; only a production unit
    # actually targeted by a "test" relation edge satisfies it.
    if "test" in unit.classification:
        return _signal(
            unit.unit_id, "test_evidence_located", "not_applicable", "detected",
            "unit_is_itself_a_test")
    if is_tested:
        return _signal(
            unit.unit_id, "test_evidence_located", "satisfied", "detected", "test_evidence_located")
    # FIX ROUND 17 (thirteenth cold read, CR13-7 MINOR): "no_test_evidence_
    # found" is a FALSE statement when this run's own dependencies.json
    # already holds a test-relation pairing naming this unit - it read
    # SOMETHING, just not enough to satisfy this check (the inferred
    # name-pairing guess alone, round 15's own fx4 shape, with no
    # corroborating extracted/declared invoke/import reference). Split
    # by what was actually found, wording only - the stored_status stays
    # unknown either way, the 16b (b)-pin (same-package-no-import stays
    # unknown, never satisfied via the inferred edge alone) is unaffected.
    if has_inferred_pairing:
        return _signal(
            unit.unit_id, "test_evidence_located", "unknown", "detected",
            "insufficient_test_evidence")
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
    externality_poisoned: bool = False,
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

    # FIX ROUND 14 (CR10-1): resolves a component's OWNING file, walking
    # up through nested-type containment (a nested type's own container
    # is its outer type, never the file directly - N6/round 6) until a
    # "file"-kind unit is reached.
    module_by_id = {m.unit_id: m for m in modules}

    # FIX ROUND 15 (eleventh cold read, F4 MAJOR, wrong-data): a "test"
    # relation edge derived from stripping a naming CONVENTION
    # (Test/Tests/IT) and guessing the remainder resolves is published
    # `evidence_class="inferred"` (adapters.java) - the target identifier
    # never actually appears in the test file's own source, so it must
    # never drive test_evidence_located past "unknown" on its own.
    #
    # FIX ROUND 15b (reviewer-3's F4 leg 3, MAJOR - closing an
    # unreachable branch this same round introduced): the ONLY test-edge
    # producer emits "inferred" - round 15's own requirement of
    # extracted/declared on the TEST relation made "satisfied"
    # unreachable on any real run, so `no_test_evidence_found` (a
    # POSITIVE claim: we looked, this class has no test) published for
    # every production unit in every repo, even one whose real JUnit
    # test class genuinely calls it. The data already exists: an
    # invoke/import edge is real, extracted evidence of what a test
    # class's body actually references - counting an EXTRACTED (or
    # DECLARED) invoke/import edge FROM a test-classified unit TO this
    # unit as real test evidence closes the branch without touching the
    # "inferred" name-pairing edge at all (still published, still never
    # sufficient alone - round 15's own fx4 shape, a name-pairing guess
    # with no real reference, must keep failing toward unknown).
    test_unit_ids = {m.unit_id for m in modules if "test" in m.classification}
    tested_unit_ids = {
        edge.target_unit_id for edge in dependencies
        if edge.relation == "test" and edge.target_unit_id is not None
        and edge.evidence_class in ("extracted", "declared")
    } | {
        edge.target_unit_id for edge in dependencies
        if edge.relation in ("invoke", "import")
        and edge.target_unit_id is not None
        and edge.evidence_class in ("extracted", "declared")
        and edge.from_unit_id in test_unit_ids
    }

    # FIX ROUND 17 (thirteenth cold read, CR13-7 MINOR): every unit named
    # by a "test" relation edge at all, regardless of evidence_class -
    # since the only test-edge producer emits "inferred" and a real
    # extracted/declared one would already be in tested_unit_ids above
    # (making is_tested True, the check below never reached), reaching
    # this set with is_tested False means the edge that names this unit
    # IS the inferred-only pairing - "we looked, found a name-derived
    # guess, just not enough to satisfy the check" is a materially
    # different, more honest statement than "no_test_evidence_found"'s
    # own "we looked and found nothing at all."
    inferred_test_pairing_unit_ids = {
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
                externality_poisoned,
            )
        else:
            dependencies_signal = _check_dependencies_resolved_for_component(
                unit, outgoing_by_unit.get(unit.unit_id, []), _owning_file_unit_id(unit),
                children_by_container, outgoing_by_unit, externality_poisoned,
            )
        # FIX ROUND 15 (eleventh cold read, F4 MAJOR part 3, wrong-data):
        # a "test" edge always resolves to the CONTAINED TYPE (a test
        # relation is a per-type fact), never the file unit directly -
        # so a file's own test_evidence_located used to report unknown
        # while its own contained type, for the identical underlying
        # fact, reported satisfied: two disagreeing answers about one
        # thing. A file-kind unit now also counts as tested when ANY of
        # its transitive descendants does - the same "roll a per-type
        # fact up to its owning file" idiom CR10-1's dependencies_signal
        # already established for this exact unit/file relationship.
        is_tested = unit.unit_id in tested_unit_ids
        has_inferred_pairing = unit.unit_id in inferred_test_pairing_unit_ids
        if unit.kind == "file":
            descendants = _transitive_descendants(unit.unit_id)
            if not is_tested:
                is_tested = bool(tested_unit_ids & descendants)
            if not has_inferred_pairing:
                has_inferred_pairing = bool(inferred_test_pairing_unit_ids & descendants)
        unit_signals = [
            _check_source_understood(unit),
            dependencies_signal,
            _check_entry_points_mapped(unit, unit.unit_id in entry_point_owner_ids),
            _check_feature_linked(unit, feature_states_by_unit.get(unit.unit_id, [])),
            _check_test_evidence_located(unit, is_tested, has_inferred_pairing),
            _check_boundaries_identified(unit),
        ]
        all_signals.extend(unit_signals)
        summaries.append(UnitReadinessSummary(
            unit_id=unit.unit_id, stored_assessment_state=_rollup(unit_signals)))

    return all_signals, summaries
