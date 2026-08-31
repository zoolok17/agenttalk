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
from .features_artifact import EntryPointRecord, FeatureRecord
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

#: FIX ROUND 24 (twentieth cold read, F8b, declare-not-silently-guess):
#: the design's own "Fact provenance and canonical merge" section
#: promises every producer entry names "the bundled adapter and
#: version, extraction-rule version, grammar/indexer version when
#: applicable, effective configuration or policy digest, source content
#: digest, and capture time", and every dependency/readiness record
#: carries its own evidence pointers - none of that is populated this
#: slice (every readiness signal's own ``producers``/``evidence`` field
#: is an empty list; producer identity never carries a configuration/
#: policy digest or a capture time either), and neither of this
#: package's own two existing structural declarations (``ASSESSMENT_
#: STATE_CAVEAT``/``features_artifact.FEATURES_STRUCTURAL_CAVEAT``)
#: says so - a consumer reading an empty ``producers``/``evidence`` list
#: has no way to tell "not implemented this slice" from "genuinely no
#: evidence exists," the exact ambiguity this whole declared-absence
#: idiom exists to close. The FULL provenance implementation stays the
#: existing, already-named fast-follow carry (config.json parsing,
#: `conflict_id` population, per-producer digest/capture-time plumbing
#: across all four artifact builders) - this is only the machine-
#: readable DECLARATION of that gap, published once in scan.json,
#: the same "declare it, don't leave it to be independently
#: rediscovered" discipline ``ASSESSMENT_STATE_CAVEAT`` already follows.
PROVENANCE_CAVEAT = (
    "readiness signal producers and dependency/readiness evidence pointers are "
    "empty for every record this slice, and producer identity never carries a "
    "configuration/policy digest or a capture time - all design-promised "
    "('Fact provenance and canonical merge'), none populated yet. An empty "
    "producers/evidence list here means 'not implemented this slice', never "
    "'genuinely no evidence exists' - the full provenance implementation is a "
    "known fast-follow carry, not a silent gap. scan.json's own VCS revision "
    "and dirty-state binding is the identical shape of gap (design-promised, "
    "not implemented this slice, the standing PR-C entry criterion named "
    "elsewhere in this PR) - every OTHER unimplemented promise this producer "
    "makes is declared in-artifact; this one now is too, rather than being the "
    "one silent exception.\n\n"
    "FIX ROUND 28 (twenty-fourth cold read, F10, completeness): problems.json's "
    "own records are a DIFFERENT shape of the same gap, not merely an unlisted "
    "instance of it - a modules/dependencies/features/readiness record carries "
    "an empty producers/evidence list (the field exists, unpopulated); a "
    "problems.json record has no producers/evidence field AT ALL (structurally "
    "absent, never merely empty) - the same 'not implemented this slice, never "
    "genuinely no provenance' honesty applies to it too, stated here so a "
    "consumer checking this caveat for problems.json's own coverage does not "
    "have to independently notice the field is missing rather than empty."
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
#: FIX ROUND 27 (twenty-third cold read, F2 MAJOR, wrong-data): every
#: whole-file evidence-gap reason below used to feed source_understood/
#: dependencies_resolved/entry_points_mapped but OMIT feature_linked -
#: while the NARROWER in-file gaps (route_annotation_unassociated/
#: route_value_unrecoverable, below) correctly included it. A file the
#: run RECORDED it could not read (a Latin-1-encoded controller, a
#: tier-2 JSP/SQL/Kotlin file with no adapter) then published the
#: CONFIDENT unsatisfied/no_feature_link negative - the reader's own
#: control is airtight: byte-identical controllers differing only in
#: encoding flip feature_linked from unknown to confident unsatisfied.
#: Blast radius: every non-UTF-8 file plus every tier-2 file - the bulk
#: of a real target estate. Trusts the recorded gap here too (the SAME
#: "don't recompute a confident answer from evidence that never
#: existed" principle round 16's own M2 already applied to the other
#: three checks, and round 20's own M3 already applied to the two
#: narrower reasons).
#:
#: MICRO-ROUND 27b (JUDGE, small, taken): ``test_evidence_located`` was
#: still omitted - a whole-file-gap unit's own status was ALREADY
#: ``unknown`` there regardless (``_check_test_evidence_located`` never
#: reports a confident value without either a real test-pairing edge or
#: an inferred one, and a file the adapter never read has neither), but
#: the REASON was the misleading ``no_test_evidence_found`` - "we looked
#: for test evidence and found none," when the true fact is "this file
#: was never read at all." Reviewer-3's own measurement: the remaining
#: sixth check, ``boundaries_identified``, is unconditionally ``unknown``
#: for every unit regardless of any reason code (it has no producer this
#: slice at all - see its own module docstring) - already covered by its
#: own, separate, stronger guarantee, so it is deliberately NOT added
#: here; adding it would be a no-op wearing the same name as this fix
#: for a check that consults no reason at all.
_WHOLE_FILE_EVIDENCE_GAP_CHECKS = frozenset({
    "source_understood", "dependencies_resolved", "entry_points_mapped", "feature_linked",
    "test_evidence_located",
})
_READINESS_CHECKS_BY_REASON_CODE: dict[str, frozenset[str]] = {
    "parse_failed": _WHOLE_FILE_EVIDENCE_GAP_CHECKS,
    # FIX ROUND 21 (seventeenth cold read, CR17-4 MAJOR, wrong-data): an
    # undecodable-as-UTF-8 file (Latin-1/CP1252 source, most commonly)
    # skips adapter analysis entirely - the same genuine whole-file
    # evidence gap parse_failed already is, never a narrower fact.
    "encoding_undecodable": _WHOLE_FILE_EVIDENCE_GAP_CHECKS,
    "path_excluded": _WHOLE_FILE_EVIDENCE_GAP_CHECKS,
    "resource_limit": _WHOLE_FILE_EVIDENCE_GAP_CHECKS,
    "non_utf8_path": _WHOLE_FILE_EVIDENCE_GAP_CHECKS,
    "case_collision": _WHOLE_FILE_EVIDENCE_GAP_CHECKS,
    "no_types_extracted": _WHOLE_FILE_EVIDENCE_GAP_CHECKS,
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
    "unsupported_language": _WHOLE_FILE_EVIDENCE_GAP_CHECKS,
    # FIX ROUND 24 (twentieth cold read, F1b, wrong-data): a pom.xml's
    # own analogue of `no_types_extracted` - a parse that succeeded but
    # registered no coordinate/edge/reactor-module fact at all (worker.py).
    "no_pom_facts_extracted": _WHOLE_FILE_EVIDENCE_GAP_CHECKS,
    # FIX ROUND 24 (twentieth cold read, F4 MINOR, wrong-data): a fact
    # about ONE dependency within an otherwise-understood pom - never a
    # whole-file evidence gap (the same narrow scoping `route_value_
    # unrecoverable` already gets, restated for the dependency side).
    "dependency_value_unrecoverable": frozenset({"dependencies_resolved"}),
    # FIX ROUND 24 (micro-round 24b, item 1, wrong-data): web.xml's own
    # analogue of `no_pom_facts_extracted` - a parse that succeeded but
    # yielded zero entry points and zero problems over a root that is
    # not genuinely empty (worker.py).
    "no_web_xml_facts_extracted": _WHOLE_FILE_EVIDENCE_GAP_CHECKS,
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
    CODE``) AND a known ``language`` - never derived from the mere
    ABSENCE of a specific, named failure alone. The positive-evidence
    half of that guarantee is enforced by CONVENTION at every worker.py
    dispatch branch, not by this function inspecting a raw adapter
    result directly: every producer that runs to completion without
    raising and extracts genuinely nothing real is required to record
    its own reason (``no_types_extracted`` for a .java file, ``no_pom_
    facts_extracted`` for a pom.xml - FIX ROUND 24, twentieth cold read,
    F1b - closing the identical gap round 8's own BLOCKER 1b already
    closed for .java, never previously extended to pom.xml, which let a
    namespace-prefixed pom this round's own F1 tag-stack bug silently
    emptied read as a confident satisfied). That inversion is what
    closes the class: a future worker failure kind this check has never
    heard of still comes through as unknown (its own reason_code,
    prefixed), because the default without a POSITIVE, producer-
    declared reason is unknown, not satisfied.

    A genuinely blank/comment-only Java file (or ``package-info.java``/
    ``module-info.java``) is DELIBERATELY NOT covered by this same
    discipline - it is a NAMED, EXPLICIT non-problem (``is_effectively_
    empty_java_source``/``_LEGITIMATELY_TYPELESS_BASENAMES``), not an
    unrecognized shape the adapter silently missed. ``satisfied`` is the
    CORRECT claim for it: there is genuinely nothing in the file to
    misunderstand, the vacuous-but-true positive a real parse/coverage
    gap is not. Unlike a pom.xml (which always carries SOME identity,
    own or ``<parent>``-inherited, under Maven's own model), "nothing at
    all" is a legitimate, common Java shape - reclassifying it as
    unknown would manufacture a false negative for every such file
    rather than close a real gap."""
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
#: FIX ROUND 21 (seventeenth cold read, CR17-2 MAJOR, wrong-data): a
#: confidence ranking for the three real outcomes `_check_dependencies_
#: resolved` ever returns - used to combine a component's own verdict
#: with its single-top-level-type file's verdict, the worse (higher-
#: ranked) of the two winning. `satisfied` is never the winner over a
#: real `unknown`/`unsatisfied` elsewhere in the SAME file.
_DEPENDENCIES_RESOLVED_RANK = {"satisfied": 0, "unknown": 1, "unsatisfied": 2}


def _check_dependencies_resolved_for_component(
    unit: ModuleRecord, own_outgoing: list[DependencyRecord], file_unit_id: str | None,
    children_by_container: dict[str, list[str]], outgoing_by_unit: dict[str, list[DependencyRecord]],
    externality_poisoned: bool = False,
) -> ReadinessSignal:
    own_relevant = [e for e in own_outgoing if e.relation in _DEPENDENCY_RESOLUTION_RELATIONS]
    if file_unit_id is None:
        return _check_dependencies_resolved(unit, own_outgoing, externality_poisoned)
    file_outgoing = outgoing_by_unit.get(file_unit_id, [])
    file_relevant = [e for e in file_outgoing if e.relation in _DEPENDENCY_RESOLUTION_RELATIONS]
    if not file_relevant:
        # The file has no relevant edges of its own either - nothing to
        # combine with; the component's own verdict (its own edges, or
        # the honest no_declared_dependencies default) already reflects
        # everything real evidence exists for.
        return _check_dependencies_resolved(unit, own_outgoing, externality_poisoned)
    top_level_siblings = len(children_by_container.get(file_unit_id, []))
    if top_level_siblings > 1:
        if not own_relevant:
            # Multi-type file, this component has no edges of its own -
            # the file's edges cannot be honestly credited to any ONE
            # sibling. Unchanged from before this round.
            return _signal(
                unit.unit_id, "dependencies_resolved", "unknown", "detected",
                "file_scoped_dependencies_not_attributed")
        # Multi-type file, but THIS component has real edges of its
        # own - those are unambiguously its own regardless of its
        # siblings; the file's OTHER, unattributable edges are a
        # separate question this check does not borrow into a status
        # about THIS component specifically. Unchanged from before this
        # round.
        return _check_dependencies_resolved(unit, own_outgoing, externality_poisoned)
    # Single-top-level-type file: the component's own verdict used to be
    # computed from own_outgoing ALONE whenever it had at least one
    # relevant edge of its own - a component with one resolved `inherit`
    # edge (every servlet/exception/DAO in real Java) published
    # dependencies_resolved SATISFIED while its OWN FILE published
    # unsatisfied/unknown for an import edge attached to the FILE unit
    # instead (imports attach file-scoped, per CR10-1/round 14) - two
    # contradictory published facts about the identical single-type
    # file, in the same run, reproduced via three distinct mechanisms
    # (a wildcard-unresolved import, an ambiguous import, an
    # externality-suppressed import). A single-top-level-type file has
    # no attribution ambiguity at all (there is only one real candidate
    # for either edge set), so the component's own verdict must never
    # be MORE CONFIDENT than the file's: the worse of the two wins,
    # satisfied only when BOTH are clean. When this component has no
    # edges of its own, the file's own verdict is the ONLY real
    # evidence either way and is returned as-is (unchanged from before
    # this round - preserves the file's own real reason_code rather
    # than substituting the component's own uninformative
    # no_declared_dependencies default).
    component_signal = _check_dependencies_resolved(unit, own_outgoing, externality_poisoned)
    file_signal = _check_dependencies_resolved(unit, file_outgoing, externality_poisoned)
    if not own_relevant:
        return file_signal
    if (
        _DEPENDENCIES_RESOLVED_RANK[file_signal.stored_status]
        > _DEPENDENCIES_RESOLVED_RANK[component_signal.stored_status]
    ):
        return _signal(
            unit.unit_id, "dependencies_resolved", file_signal.stored_status, "detected",
            file_signal.reason_code)
    return component_signal


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
    PLUS any edge attributed directly to the file itself (the pom.xml
    shape, where BUILD edges attach to the file unit directly - there
    being no component-level unit for that producer).

    FIX ROUND 27 (twenty-third cold read, F1 BLOCKER, correction): this
    docstring used to also claim web.xml's own ROUTE entry points attach
    to the file unit directly, the same way build edges do - FALSE once
    the reader measured it: a web.xml-declared route's OWNERSHIP moves
    to its implementing servlet class whenever that class resolves
    in-scan (CR13-2, round 17) - only pom.xml's build edges genuinely
    stay file-attached unconditionally. See `_check_entry_points_mapped`/
    `_check_feature_linked`'s own `declared_in_unit_id` handling in
    `build_readiness` for how the declaring file still gets credited
    even though ownership itself moved elsewhere - a DIFFERENT mechanism
    from this function's own direct-edge union, not the same one.
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
    # FIX ROUND 21b (reviewer-3's re-delta, THE MAJOR's own entry_points_
    # mapped question, DECIDED - satisfied-with-the-distinct-kind, not a
    # filter-specific treatment): ``has_entry_point`` is kind-agnostic by
    # construction (derived from ``features.py``'s own unit-owns-a-
    # feature rollup, which does not look at kind either) - a unit whose
    # ONLY entry point is now a ``http_filter`` (round 21b's own new
    # kind) still reports ``satisfied`` here, unchanged. This signal
    # answers the design's own question ("externally visible entry
    # points are mapped") - whether THIS run found a real, evidenced,
    # boundary-crossing construct for the unit - not "does this unit
    # serve a complete, servable HTTP route." A declared servlet filter
    # genuinely IS such a construct (every matching request passes
    # through it) even though it does not itself serve one; a NEW
    # filter-specific readiness value would need its own vocabulary
    # entry for a claim ("mapped, but only to an interception point")
    # this slice's own six-signal floor does not need to make. Consistent
    # with ``cli_main``/``http_route`` already sharing this same
    # heterogeneous "is-there-an-entry-point" signal today.
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


def _aggregate_file_signal_from_components(
    unit: ModuleRecord, check: str, component_signals: list[ReadinessSignal],
) -> ReadinessSignal:
    """FIX ROUND 22 (eighteenth cold read, F1 MAJOR, wrong-data): a
    file's own ``entry_points_mapped``/``feature_linked`` used to be
    computed directly from ``entry_point_owner_ids``/``feature_states_
    by_unit`` - but ``features_artifact.build_features`` attaches an
    entry point or feature to the COMPONENT's own unit_id whenever its
    qualified name resolves (the common case for any real, in-repo
    class), never the file's - so a file whose single contained class
    genuinely serves a real, mapped entry point still reported the
    confident negative (``not_applicable``/``no_entry_point``,
    ``unsatisfied``/``no_feature_link``) - two contradictory published
    facts about the identical single-type file, the exact class CR10-1
    (``dependencies_resolved``) and round 15's own F4
    (``test_evidence_located``) already closed for their own checks.

    A single component child MIRRORS its own signal exactly - no
    attribution ambiguity at all (there is only one real candidate).
    Two or more component children aggregate worse-of, but NOT via one
    linear confidence spectrum the way ``_DEPENDENCIES_RESOLVED_RANK``
    orders its own three states: an attributed ``unknown`` anywhere
    among the children wins outright (this file's own real evidence is
    genuinely unresolved for at least one of its declared types - the
    same "never MORE CONFIDENT than a component genuinely unsure of
    itself" discipline CR17-2 already established, just generalized
    from a two-way to an N-way comparison); failing that, a real
    ``satisfied`` claim from ANY child wins over an unrelated sibling's
    mere absence (a different, non-conflicting fact about a DIFFERENT
    declared type - one class having no entry point of its own never
    invalidates another class's real, mapped one); only when EVERY
    child genuinely has none does the file keep its own honest absence
    claim (``not_applicable``/``unsatisfied``, unchanged from before
    this round) - the "genuinely entry-point-free file keeps its honest
    negative" case. Reuses each winning component's OWN already-final
    ``reason_code`` verbatim (never re-derived) - it is already the
    correct, most specific citation for whichever real fact won."""
    if len(component_signals) == 1:
        only = component_signals[0]
        return _signal(unit.unit_id, check, only.stored_status, only.basis, only.reason_code)
    unknown_signals = sorted(
        (s for s in component_signals if s.stored_status == "unknown"),
        key=lambda s: s.reason_code,
    )
    if unknown_signals:
        chosen = unknown_signals[0]
        return _signal(unit.unit_id, check, "unknown", chosen.basis, chosen.reason_code)
    satisfied_signals = [s for s in component_signals if s.stored_status == "satisfied"]
    if satisfied_signals:
        chosen = satisfied_signals[0]
        return _signal(unit.unit_id, check, "satisfied", chosen.basis, chosen.reason_code)
    absence_signal = next(
        s for s in component_signals if s.stored_status not in ("unknown", "satisfied"))
    return _signal(
        unit.unit_id, check, absence_signal.stored_status, absence_signal.basis,
        absence_signal.reason_code)


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
    # MICRO-ROUND 27b (JUDGE, small, taken): a whole-file-gap unit (the
    # adapter never successfully read it - Latin-1, a tier-2 language
    # with no adapter, a resource-cap skip, ...) has neither a real nor
    # an inferred test-pairing edge, by construction - the fall-through
    # below already reported unknown/no_test_evidence_found for it, an
    # HONEST status with a MISLEADING reason ("we looked for test
    # evidence and found none" when the true fact is "this file was
    # never read at all"). Checked AFTER the test-classification branch
    # above (classification is a path-based fact, independent of
    # whether the adapter could read the file - a test-classified unit
    # stays not_applicable regardless) but before is_tested/has_
    # inferred_pairing are consulted, the same "trust the recorded gap"
    # precedent every other whole-file-gap check already follows.
    understanding_reasons = _reasons_feeding("test_evidence_located", unit.adapter_problem_reasons)
    if understanding_reasons:
        return _signal(
            unit.unit_id, "test_evidence_located", "unknown", "detected",
            _propagated_reason_spelling(understanding_reasons[0]),
        )
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
    entry_points: list[EntryPointRecord] = (),
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
    # FIX ROUND 27 (twenty-third cold read, F1 BLOCKER, wrong-data): a
    # file that DECLARED a published entry point but does not OWN it
    # (ownership resolved to a different in-scan unit - CR13-2's own
    # web.xml <servlet-class> join is the common real shape: the
    # implementing servlet class becomes the owner, but web.xml itself
    # is what declared the route) used to have ZERO evidence here at
    # all - `entry_point_owner_ids`/`feature_states_by_unit` only ever
    # reflected the RESOLVED owner. `EntryPointRecord.declared_in_unit_
    # id` (features_artifact.py) names the declaring file unconditionally
    # - added here ADDITIVELY (never replacing the owner-based evidence
    # above) so a file with a real, published route it declared is
    # satisfied regardless of where ownership resolved.
    #
    # CORRECTION (round 28's own F6, twenty-fourth cold read): this
    # comment used to claim that for an annotation-based route
    # "declared_in and the resolved owner are already the same unit" -
    # FALSE (declared_in_unit_id is always the FILE; the resolved owner
    # is the COMPONENT, a different unit_id, whenever the class
    # resolves in-scan). This addition is a no-op for the annotation
    # case for an UNRELATED reason: whenever the file has a component
    # child, round 22's own containment rollup (below, `_aggregate_
    # file_signal_from_components`) OVERWRITES the file's own
    # precomputed entry_points_mapped/feature_linked signal regardless
    # of what this set contains for it - so adding the file's own
    # declared_in_unit_id here changes nothing DOWNSTREAM for that
    # shape, not because the two ids coincide. It only changes anything
    # for a producer like web.xml, which declares routes with no
    # component-kind unit of its own to roll up through at all - the
    # XML-declared-route asymmetry this round's own fixture measures.
    feature_state_by_feature_id = {f.feature_id: f.state for f in features}
    for entry_point in entry_points:
        entry_point_owner_ids.add(entry_point.declared_in_unit_id)
        for feature_id in entry_point.feature_ids:
            state = feature_state_by_feature_id.get(feature_id)
            if state is not None:
                feature_states_by_unit.setdefault(
                    entry_point.declared_in_unit_id, []).append(state)

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

    # FIX ROUND 22 (eighteenth cold read, F1 MAJOR, wrong-data): computed
    # for EVERY unit first (component and file identically, exactly as
    # before this round), so the file-level override pass below can look
    # up each file's own DIRECT top-level component children's ALREADY-
    # COMPUTED verdicts - see _aggregate_file_signal_from_components.
    entry_points_mapped_by_unit_id: dict[str, ReadinessSignal] = {
        unit.unit_id: _check_entry_points_mapped(unit, unit.unit_id in entry_point_owner_ids)
        for unit in modules
    }
    feature_linked_by_unit_id: dict[str, ReadinessSignal] = {
        unit.unit_id: _check_feature_linked(unit, feature_states_by_unit.get(unit.unit_id, []))
        for unit in modules
    }
    # FIX ROUND 22 (eighteenth cold read, F4, wrong-data, narrow
    # trigger): a unit carrying a conflict_id (modules_artifact.py's own
    # duplicate-qualified-name collision tag) published CONFIDENT
    # readiness on every signal that depends on knowing WHICH real
    # unit's own identity/relationships are under discussion - the
    # design's own merge rule 4 says a verbatim dependent's readiness
    # stays unknown until an explicit declaration resolves the conflict.
    # Compounded by round 21c's own 2+-claimant skip: a cross-file
    # entry-point/feature reason genuinely meant for one of two
    # identically-named claimants reaches NEITHER (deliberately, to
    # avoid guessing which one) - so a confident no_entry_point is
    # PROVABLY false for at least one of them. Overridden BEFORE the
    # file-aggregation pass below runs, so a file containing a
    # conflicted component correctly inherits the same "never MORE
    # CONFIDENT than a component genuinely unsure of itself" unknown via
    # the existing worse-of aggregation for entry_points_mapped/
    # feature_linked SPECIFICALLY.
    #
    # FIX ROUND 23 (nineteenth cold read, F4 MINOR, wrong-data + a
    # stale claim in this SAME comment, corrected): this comment
    # previously claimed all four signals - dependencies_resolved/
    # entry_points_mapped/feature_linked/test_evidence_located -
    # inherit via "the existing worse-of aggregation" once this loop
    # runs. FALSE for two of them: dependencies_resolved/test_evidence_
    # located are computed in the MAIN per-unit loop below, straight
    # from the unit's OWN edges/test-pairing facts - neither one ever
    # consults a CONTAINED component's own conflict_id at all, so a
    # file containing a conflicted component kept publishing CONFIDENT
    # dependencies_resolved/test_evidence_located while the component
    # itself correctly reported unknown. The override immediately below
    # applies to entry_points_mapped/feature_linked ONLY (matching what
    # the file-aggregation pass right after it actually consults) -
    # dependencies_resolved/test_evidence_located get their OWN,
    # separate extension in the main loop below - MICRO-ROUND 23b widened
    # this from round 23's own single-top-level-type-file-only scoping
    # to ANY conflicted component anywhere in the file's own containment
    # chain (reviewer-3's own R4 consistency ask), matching entry_points_
    # mapped/feature_linked's identical "an attributed unknown anywhere
    # wins outright" policy for a 2+-children file exactly. source_
    # understood deliberately stays untouched throughout - the adapter
    # genuinely parsed this exact file/class; what is ambiguous is
    # cross-file IDENTITY, not comprehension, a different fact entirely.
    # boundaries_identified is unaffected either way (always unknown
    # regardless, unrelated to identity).
    for unit in modules:
        if unit.conflict_id is None:
            continue
        entry_points_mapped_by_unit_id[unit.unit_id] = _signal(
            unit.unit_id, "entry_points_mapped", "unknown", "detected",
            "duplicate_qualified_name")
        feature_linked_by_unit_id[unit.unit_id] = _signal(
            unit.unit_id, "feature_linked", "unknown", "detected", "duplicate_qualified_name")
    # FIX ROUND 24 (twentieth cold read, F6 MINOR, consistency): round
    # 22's own F1 invariant ("never MORE CONFIDENT than your components")
    # was only ever applied at the FILE level - a COMPONENT with its own
    # NESTED component descendant (a statically nested class one level
    # further in) kept publishing its OWN direct, un-aggregated signal
    # regardless of a nested descendant's real facts (the reader's own
    # `.cr20-nest`: `Outer` published a confident `satisfied`/`no_entry_
    # point` pair while its own nested `Inner` was conflicted and
    # reported `unknown` for the identical underlying identity question -
    # the enclosing FILE correctly rolled up to `unknown` via the
    # existing file-level aggregation below, but `Outer` itself, one
    # level in, did not). Aggregates `Outer`'s own ALREADY-COMPUTED
    # direct signal together with its own nested descendants' signals
    # via the identical worse-of ranking - unlike the file-level
    # aggregation (which never independently owns entry-point evidence
    # of its own, so a lone child's signal is mirrored exactly), a
    # component's own DIRECT evidence is real and independent (`Outer`
    # itself may be its own real `@WebServlet`) and must not be silently
    # discarded - it is included in the comparison, not replaced by it.
    for unit in modules:
        if unit.kind != "component":
            continue
        nested_component_descendants = [
            module_by_id[child_id] for child_id in _transitive_descendants(unit.unit_id)
            if module_by_id.get(child_id) is not None and module_by_id[child_id].kind == "component"
        ]
        if not nested_component_descendants:
            continue
        entry_points_mapped_by_unit_id[unit.unit_id] = _aggregate_file_signal_from_components(
            unit, "entry_points_mapped",
            [entry_points_mapped_by_unit_id[unit.unit_id]]
            + [entry_points_mapped_by_unit_id[c.unit_id] for c in nested_component_descendants],
        )
        feature_linked_by_unit_id[unit.unit_id] = _aggregate_file_signal_from_components(
            unit, "feature_linked",
            [feature_linked_by_unit_id[unit.unit_id]]
            + [feature_linked_by_unit_id[c.unit_id] for c in nested_component_descendants],
        )
    for unit in modules:
        if unit.kind != "file":
            continue
        # FIX ROUND 22b (reviewer-3's delta on round 22, R1, wrong-data -
        # SCOPE overturned by one step): round 22's own F1 fix consulted
        # DIRECT top-level children only - a statically NESTED entry-
        # point-carrying class (e.g. `class Host { @WebListener static
        # class Inner {} }`) is never a direct child of the FILE unit at
        # all (N6/round 6: a nested type's own container is its OUTER
        # type, never the file directly) - so `Host.java` still
        # published the confident `no_entry_point` negative while
        # `p.Host.Inner` correctly reported `unknown` in the SAME run.
        # Walks the FULL containment chain via the existing
        # `_transitive_descendants` helper instead - the exact same one
        # round 15b already uses to roll tested-status up to the owning
        # file for this identical unit/file relationship. The ratified
        # ranking semantics (attributed unknown > real satisfied >
        # honest absence; a single descendant mirrors exactly) are
        # UNCHANGED - only which units are gathered as "this file's own
        # descendants" changes.
        component_children = [
            module_by_id[child_id] for child_id in _transitive_descendants(unit.unit_id)
            if module_by_id.get(child_id) is not None and module_by_id[child_id].kind == "component"
        ]
        if not component_children:
            continue
        entry_points_mapped_by_unit_id[unit.unit_id] = _aggregate_file_signal_from_components(
            unit, "entry_points_mapped",
            [entry_points_mapped_by_unit_id[c.unit_id] for c in component_children],
        )
        feature_linked_by_unit_id[unit.unit_id] = _aggregate_file_signal_from_components(
            unit, "feature_linked",
            [feature_linked_by_unit_id[c.unit_id] for c in component_children],
        )

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
        # FIX ROUND 24 (F6 MINOR, consistency): computed for BOTH kinds
        # now (the conflict-descendant check just below needs it for
        # "component" units too) - the test-status ROLLUP immediately
        # below stays FILE-only, unchanged; F6 does not touch that
        # separate mechanism.
        own_transitive_descendants = (
            _transitive_descendants(unit.unit_id) if unit.kind in ("file", "component") else frozenset()
        )
        is_tested = unit.unit_id in tested_unit_ids
        has_inferred_pairing = unit.unit_id in inferred_test_pairing_unit_ids
        if unit.kind == "file":
            if not is_tested:
                is_tested = bool(tested_unit_ids & own_transitive_descendants)
            if not has_inferred_pairing:
                has_inferred_pairing = bool(inferred_test_pairing_unit_ids & own_transitive_descendants)
        test_evidence_signal = _check_test_evidence_located(unit, is_tested, has_inferred_pairing)
        # FIX ROUND 22 (F4, wrong-data, narrow trigger): see the
        # entry_points_mapped_by_unit_id/feature_linked_by_unit_id
        # override above - dependencies_resolved/test_evidence_located
        # get the identical conflict-driven override here, the two
        # remaining identity-dependent signals this per-unit loop (not
        # the precomputed dicts above) computes.
        if unit.conflict_id is not None:
            dependencies_signal = _signal(
                unit.unit_id, "dependencies_resolved", "unknown", "detected",
                "duplicate_qualified_name")
            test_evidence_signal = _signal(
                unit.unit_id, "test_evidence_located", "unknown", "detected",
                "duplicate_qualified_name")
        # FIX ROUND 23 (nineteenth cold read, F4 MINOR, wrong-data),
        # EXTENDED micro-round 23b (reviewer-3's own R4 consistency ask,
        # taken): a FILE unit never carries a conflict_id itself (only
        # "component"-kind records do), so the override above never
        # applies to a file directly - a file containing a conflicted
        # component kept publishing CONFIDENT dependencies_resolved/
        # test_evidence_located while the component itself correctly
        # reported unknown. Round 23's own fix scoped this to a SINGLE
        # top-level-type file only (direct children, CR17-2's own
        # terminology) - but entry_points_mapped/feature_linked's own
        # file-aggregation above already extends the identical "an
        # attributed unknown anywhere wins outright" policy across ALL
        # transitive descendants, for ANY number of children (round
        # 22b's own R1) - two different policies for the SAME 2+-
        # children-file shape on one record was an inconsistency, not a
        # deliberate distinction (the reviewer's own Multi.java repro:
        # dependencies_resolved published a confident satisfied - TRUE
        # about the file's own edges - while a sibling component's own
        # IDENTITY is unknown, the exact fact entry_points_mapped/
        # feature_linked already surface as unknown for the SAME file).
        # Widened to match: ANY conflicted component anywhere in the
        # file's own containment chain (not just a lone direct child)
        # now overrides both signals, the identical descendant walk
        # entry_points_mapped/feature_linked's own aggregation already
        # uses above - never guessing which sibling's facts are real
        # when one sibling's own identity is itself unresolved.
        # FIX ROUND 24 (twentieth cold read, F6 MINOR, consistency):
        # widened from FILE-only to ALSO cover a "component"-kind unit
        # with its own nested component descendant - round-22 F1's own
        # invariant ("never more confident than your components")
        # applies one level in too, the identical descendant walk and
        # ranking, restated for dependencies_resolved/test_evidence_
        # located instead of entry_points_mapped/feature_linked.
        if unit.kind in ("file", "component"):
            conflicted_descendants = [
                module_by_id[child_id] for child_id in own_transitive_descendants
                if module_by_id.get(child_id) is not None
                and module_by_id[child_id].kind == "component"
                and module_by_id[child_id].conflict_id is not None
            ]
            if conflicted_descendants:
                dependencies_signal = _signal(
                    unit.unit_id, "dependencies_resolved", "unknown", "detected",
                    "duplicate_qualified_name")
                test_evidence_signal = _signal(
                    unit.unit_id, "test_evidence_located", "unknown", "detected",
                    "duplicate_qualified_name")
        unit_signals = [
            _check_source_understood(unit),
            dependencies_signal,
            entry_points_mapped_by_unit_id[unit.unit_id],
            feature_linked_by_unit_id[unit.unit_id],
            test_evidence_signal,
            _check_boundaries_identified(unit),
        ]
        all_signals.extend(unit_signals)
        summaries.append(UnitReadinessSummary(
            unit_id=unit.unit_id, stored_assessment_state=_rollup(unit_signals)))

    return all_signals, summaries
