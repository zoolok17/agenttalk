"""#55 slice-1 PR-B item 7: readiness.json record assembly
(DESIGN-55-comprehension-plane.md, Artifact 4). Builds ModuleRecord/
DependencyRecord/FeatureRecord fixtures directly rather than through the
full adapter pipeline, to isolate the policy-evaluation logic itself.
"""

from __future__ import annotations

from agenttalk.comprehension import readiness_artifact as ra
from agenttalk.comprehension.dependencies_artifact import DependencyRecord
from agenttalk.comprehension.features_artifact import EntryPointRecord, FeatureRecord
from agenttalk.comprehension.modules_artifact import ModuleRecord


def _unit(
    unit_id: str, *, language: str = "java", classification: str = "production",
    adapter_problem_reason: str | None = None, adapter_problem_reasons: list[str] | None = None,
    container_unit_id: str | None = None,
) -> ModuleRecord:
    return ModuleRecord(
        unit_id=unit_id, kind="component", display_name=unit_id, language=language,
        paths=[f"{unit_id}.java"], source_digests={}, classification=[classification],
        container_unit_id=container_unit_id, producers=[],
        adapter_problem_reason=adapter_problem_reason,
        adapter_problem_reasons=adapter_problem_reasons or [],
    )


def _edge(
    from_unit_id: str, *, relation: str = "invoke", resolution_state: str, target_unit_id=None,
    evidence_class: str = "extracted", externality_suppressed: bool = False,
) -> DependencyRecord:
    return DependencyRecord(
        edge_id=f"edge-{from_unit_id}-{relation}-{resolution_state}-{evidence_class}"
                f"-{externality_suppressed}",
        from_unit_id=from_unit_id,
        relation=relation, phase="runtime", optional=False, evidence_class=evidence_class,
        resolution_state=resolution_state, target_unit_id=target_unit_id,
        externality_suppressed=externality_suppressed,
    )


def _feature(label: str, state: str, unit_ids: list[str]) -> FeatureRecord:
    return FeatureRecord(
        feature_id=f"feature-{label}", label=label, state=state, origin="detected",
        unit_ids=unit_ids, entry_point_ids=[],
    )


def _signal_by_check(signals, check):
    return next(s for s in signals if s.check == check)


# ----------------------------------------------------------- source_understood

def test_source_understood_satisfied_for_a_known_language_unit():
    signals, _summaries = ra.build_readiness([_unit("u1")], [], [])
    assert _signal_by_check(signals, "source_understood").stored_status == "satisfied"


def test_assessed_is_currently_unreachable_this_slice() -> None:
    """Documents an intentional, honest consequence of the design rather
    than a bug: boundaries_identified is ALWAYS unknown this slice (no
    data/configuration producer exists yet), so no unit - however
    thoroughly evidenced everywhere else - can roll up to "assessed" until
    a later slice adds that producer. "Unknown is first-class... never
    collapse to ready" (design core invariant 5) applies to the rollup
    itself, not just individual signals."""
    edges = [_edge("u1", resolution_state="resolved", target_unit_id="u2")]
    features = [_feature("Foo", "confirmed", ["u1"])]
    signals, summaries = ra.build_readiness(
        [_unit("u1", classification="test")], edges, features)
    assert all(
        s.stored_status in ("satisfied", "not_applicable")
        for s in signals if s.check != "boundaries_identified"
    )
    assert _signal_by_check(signals, "boundaries_identified").stored_status == "unknown"
    assert summaries[0].stored_assessment_state == "needs_evidence"


def test_not_applicable_is_also_currently_unreachable_this_slice() -> None:
    """FIX ROUND 14 (tenth cold read, CR10-11, declared): the same
    structural reason "assessed" is unreachable (boundaries_identified is
    ALWAYS unknown, never not_applicable, and is not blocker-severity)
    also rules out the rollup's "not_applicable" branch - that branch
    only fires when EVERY signal is not_applicable, which can never
    happen while one signal is permanently, unconditionally "unknown".
    "blocked" was already declared unreachable (round 10 N1, no check
    but source_understood is blocker-severity, and it never returns
    unsatisfied). Combined with the existing
    test_assessed_is_currently_unreachable_this_slice, this pins the
    practical consequence: assessment_state is currently a CONSTANT
    ("needs_evidence") for every unit this slice - it carries no
    discriminating information yet, however evidenced or unevidenced a
    unit's OWN individual signals are."""
    minimal = _unit("u1")  # no edges, no features, no entry points, no problems
    signals, summaries = ra.build_readiness([minimal], [], [])
    assert summaries[0].stored_assessment_state == "needs_evidence"
    fully_evidenced = _unit("u1", classification="test")
    edges = [_edge("u1", resolution_state="resolved", target_unit_id="u2")]
    features = [_feature("Foo", "confirmed", ["u1"])]
    signals, summaries = ra.build_readiness([fully_evidenced], edges, features)
    assert summaries[0].stored_assessment_state == "needs_evidence"


def test_source_understood_unknown_and_needs_evidence_with_no_adapter():
    """M-2 (second cold read, fix round 4): "no adapter for this
    language" is an ABSENCE of positive evidence, not a positive claim
    that the source is definitely not understood - the design's own
    rollup rule draws this exact line (DESIGN-55-comprehension-plane.md,
    Artifact 4: "any required scan-time blocker that is UNSATISFIED
    yields blocked; any required scan-time UNKNOWN yields
    needs_evidence"). Reports unknown/needs_evidence, never the stronger
    unsatisfied/blocked - a confident blocker for every non-code file
    would make blocked the default headline state on a real repo, where
    most files are non-code."""
    signals, summaries = ra.build_readiness([_unit("u1", language="unknown")], [], [])
    signal = _signal_by_check(signals, "source_understood")
    assert signal.stored_status == "unknown"
    assert signal.reason_code == "no_adapter_for_language"
    assert summaries[0].stored_assessment_state == "needs_evidence"


def test_source_understood_unknown_when_the_adapter_failed_to_parse():
    """B3 (cold-read, PR-B fix round 3): a file the adapter attempted and
    failed to parse (or could not even read) must be genuinely UNKNOWN,
    never "satisfied" just because its extension maps to a known
    language - "satisfied" is a positive claim this unit never earned."""
    signals, summaries = ra.build_readiness(
        [_unit(
            "u1", language="java", adapter_problem_reason="parse_failed",
            adapter_problem_reasons=["parse_failed"],
        )], [], [])
    signal = _signal_by_check(signals, "source_understood")
    assert signal.stored_status == "unknown"
    assert signal.reason_code == "adapter_parse_failed"
    assert summaries[0].stored_assessment_state == "needs_evidence"


def test_dependencies_resolved_and_entry_points_mapped_also_unknown_when_parse_failed():
    """FIX ROUND 16 (twelfth cold read, M2 MAJOR, wrong-data): a parse-
    failed unit used to report dependencies_resolved satisfied/
    no_declared_dependencies and entry_points_mapped not_applicable/
    no_entry_point - both confidently derived from the necessarily EMPTY
    evidence a failed parse leaves behind (zero edges, zero features),
    never from anything the source actually says. A reader who trusts
    source_understood=unknown must not then find two adjacent checks on
    the SAME unit confidently answered from a parsed prefix that never
    existed - both must be unknown too, for the identical reason."""
    signals, summaries = ra.build_readiness(
        [_unit(
            "u1", language="java", adapter_problem_reason="parse_failed",
            adapter_problem_reasons=["parse_failed"],
        )], [], [])
    dependencies_signal = _signal_by_check(signals, "dependencies_resolved")
    assert dependencies_signal.stored_status == "unknown"
    assert dependencies_signal.reason_code == "adapter_parse_failed"
    entry_points_signal = _signal_by_check(signals, "entry_points_mapped")
    assert entry_points_signal.stored_status == "unknown"
    # FIX ROUND 16b (LOW - unify the two spellings): a whole-file
    # evidence-gap reason now publishes the SAME "adapter_X" spelling on
    # every check it feeds - source_understood, dependencies_resolved,
    # and entry_points_mapped alike, never two spellings for one fact
    # about one unit.
    assert entry_points_signal.reason_code == "adapter_parse_failed"
    assert summaries[0].stored_assessment_state == "needs_evidence"


def test_dependencies_resolved_and_entry_points_mapped_spell_the_same_propagated_reason():
    """FIX ROUND 16c (reviewer-3's LOW on round 16b): the "adapter_"
    prefix rule used to live in TWO separately-maintained formulations
    (dependencies_resolved prefixed unconditionally, entry_points_mapped
    tested map membership) that agreed today only by coincidence of the
    current reason set - nothing enforced that they always would. Both
    now route through the ONE shared `_propagated_reason_spelling`
    predicate; this pins the parity directly (not just incidentally, via
    one specific reason code) so a future reason feeding both checks
    cannot make them disagree again."""
    signal_a = ra._check_dependencies_resolved(_unit(
        "u1", adapter_problem_reasons=["parse_failed"]), [])
    signal_b = ra._check_entry_points_mapped(_unit(
        "u1", adapter_problem_reasons=["parse_failed"]), False)
    assert signal_a.reason_code == signal_b.reason_code == "adapter_parse_failed"


def test_file_unit_dependencies_resolved_unknown_not_not_applicable_when_parse_failed():
    """FIX ROUND 16 (M2 MAJOR): the FILE-kind path
    (_check_dependencies_resolved_for_file) has its OWN early return for
    "no contained units and no direct edges" - not_applicable, a claim
    that "dependencies" does not meaningfully apply here. That claim is
    not independently knowable when the parse that would have found any
    contained units or edges never completed - a parse-failed file with
    zero of either must report unknown, never the confident not_
    applicable a genuinely typeless (but successfully parsed) file
    earns."""
    from dataclasses import replace
    file_unit = _unit(
        "u-file", language="java", adapter_problem_reason="parse_failed",
        adapter_problem_reasons=["parse_failed"],
    )
    file_unit = replace(file_unit, kind="file")
    signals, _ = ra.build_readiness([file_unit], [], [])
    signal = _signal_by_check(signals, "dependencies_resolved")
    assert signal.stored_status == "unknown"
    assert signal.reason_code == "adapter_parse_failed"


def test_source_understood_unknown_when_the_file_is_an_unsupported_language():
    """FIX ROUND 14 (tenth cold read, CR10-5 JUDGE, completeness): a
    recognized-but-unsupported source shape (JSP/properties/Spring-XML/
    SQL) is a NEW reason_code worker.py now records - it must be named in
    the closed _READINESS_CHECKS_BY_REASON_CODE map (else this dict
    lookup raises KeyError for every such file) and feed source_understood
    the same way parse_failed/resource_limit already do."""
    signals, summaries = ra.build_readiness(
        [_unit(
            "u1", language="unknown", adapter_problem_reason="unsupported_language",
            adapter_problem_reasons=["unsupported_language"],
        )], [], [])
    signal = _signal_by_check(signals, "source_understood")
    assert signal.stored_status == "unknown"
    assert signal.reason_code == "adapter_unsupported_language"
    assert summaries[0].stored_assessment_state == "needs_evidence"


def test_source_understood_unknown_when_the_adapter_work_resource_cap_skipped_it():
    """M-2 (third cold read, fix round 5): CLOSES THE CLASS - round 3
    threaded ONLY the ``parse_failed`` reason; a file the worker skipped
    for the per-file adapter-work resource cap fell through the exact
    same "no positive evidence, but reported satisfied anyway" gap a
    second time. source_understood must derive from the PRESENCE of
    positive adapter evidence, never from the absence of one specific,
    named failure kind - so this (and any future worker failure reason)
    is unknown by construction, with a reason_code that still names the
    real cause."""
    signals, summaries = ra.build_readiness(
        [_unit(
            "u1", language="java", adapter_problem_reason="resource_limit",
            adapter_problem_reasons=["resource_limit"],
        )], [], [])
    signal = _signal_by_check(signals, "source_understood")
    assert signal.stored_status == "unknown"
    assert signal.reason_code == "adapter_resource_limit"
    assert summaries[0].stored_assessment_state == "needs_evidence"


# ----------------------------------------------------------- dependencies_resolved

def test_dependencies_resolved_satisfied_with_no_outgoing_edges():
    signals, _ = ra.build_readiness([_unit("u1")], [], [])
    assert _signal_by_check(signals, "dependencies_resolved").stored_status == "satisfied"


def test_dependencies_resolved_unsatisfied_when_an_edge_is_unresolved():
    edges = [_edge("u1", relation="import", resolution_state="unresolved")]
    signals, _ = ra.build_readiness([_unit("u1")], edges, [])
    assert _signal_by_check(signals, "dependencies_resolved").stored_status == "unsatisfied"


def test_dependencies_resolved_unknown_with_externality_suppressed_reason_on_a_poisoned_run():
    """FIX ROUND 20c (readiness carry, inherited from round 20 - THE
    MAJOR): on a POISONED run, a healthy unit whose ONLY unresolved edge
    is an externality miss (org.slf4j, marked externality_suppressed by
    dependencies_artifact.py's own poison-rule branch) must never
    publish the blocker-severity UNSATISFIED/unresolved_dependency claim
    - the producer ABSTAINED from a positive external claim, it did not
    find a real dependency problem."""
    edges = [_edge(
        "u1", relation="import", resolution_state="unresolved", externality_suppressed=True)]
    signals, _ = ra.build_readiness([_unit("u1")], edges, [], externality_poisoned=True)
    signal = _signal_by_check(signals, "dependencies_resolved")
    assert signal.stored_status == "unknown"
    assert signal.reason_code == "externality_suppressed"


def test_dependencies_resolved_satisfied_on_a_clean_run_with_the_same_shape():
    """Companion negative case: the identical unit/edge shape, but this
    run was never poisoned (externality_poisoned left at its default
    False) - an all-resolved unit still reports satisfied as before."""
    edges = [_edge("u1", relation="import", resolution_state="resolved")]
    signals, _ = ra.build_readiness([_unit("u1")], edges, [])
    signal = _signal_by_check(signals, "dependencies_resolved")
    assert signal.stored_status == "satisfied"


def test_dependencies_resolved_stays_unsatisfied_on_a_poisoned_run_with_a_genuine_internal_miss():
    """FIX ROUND 20c: a unit with BOTH an externality miss AND a genuine
    unresolved INTERNAL dependency (externality_suppressed=False) must
    keep the existing UNSATISFIED claim - that claim is still true (a
    real dependency problem exists) and wins over the poison-caused
    abstention on the OTHER edge."""
    edges = [
        _edge("u1", relation="import", resolution_state="unresolved", externality_suppressed=True),
        _edge("u1", relation="build", resolution_state="unresolved", externality_suppressed=False),
    ]
    signals, _ = ra.build_readiness([_unit("u1")], edges, [], externality_poisoned=True)
    signal = _signal_by_check(signals, "dependencies_resolved")
    assert signal.stored_status == "unsatisfied"
    assert signal.reason_code == "unresolved_dependency"


def test_dependencies_resolved_satisfied_for_a_reserved_namespace_import_under_poison():
    """FIX ROUND 20c: a reserved-namespace import (java.*/javax.*/
    jakarta.*) resolves EXTERNAL even under poison (round 20b's own THE
    ASK) - it never reaches this check as an unresolved edge at all, so
    the unit stays satisfied-eligible, unaffected by poisoning."""
    edges = [_edge("u1", relation="import", resolution_state="resolved")]
    signals, _ = ra.build_readiness([_unit("u1")], edges, [], externality_poisoned=True)
    signal = _signal_by_check(signals, "dependencies_resolved")
    assert signal.stored_status == "satisfied"


def test_dependencies_resolved_unknown_when_an_edge_is_ambiguous():
    edges = [_edge("u1", relation="import", resolution_state="ambiguous")]
    signals, _ = ra.build_readiness([_unit("u1")], edges, [])
    assert _signal_by_check(signals, "dependencies_resolved").stored_status == "unknown"


def test_dependencies_resolved_satisfied_when_an_unresolved_edge_is_invoke_only():
    """FIX ROUND 12 (eighth cold read, F2/F5 folded in): dependencies_
    resolved is scoped to import/inherit/build relations - the design's
    "direct internal dependencies" - so an unresolved ``invoke`` edge
    (the JDK-noise shape: an unqualified call the adapter cannot
    recognize as external, like ``Math.max(...)``) must never drive an
    ordinary class to unsatisfied."""
    edges = [_edge("u1", relation="invoke", resolution_state="unresolved")]
    signals, _ = ra.build_readiness([_unit("u1")], edges, [])
    signal = _signal_by_check(signals, "dependencies_resolved")
    assert signal.stored_status == "satisfied"
    assert signal.reason_code == "no_declared_dependencies"


def test_dependencies_resolved_unknown_when_the_only_edge_is_an_ambiguous_invoke():
    """FIX ROUND 15 (eleventh cold read, M6 JUDGE - taken): a unit whose
    ONLY cross-unit dependency is an ambiguous INVOKE used to report
    satisfied/no_declared_dependencies - an honest reason code over a
    real unknown. An ambiguous resolution is never JDK/library noise
    (that's always "unresolved", zero in-scan candidates to tie on) -
    it only fires when the scanner found 2+ REAL in-scan candidates and
    genuinely could not tell which one this call targets, a substantive
    uncertainty about the codebase's own structure. Checked regardless
    of _DEPENDENCY_RESOLUTION_RELATIONS scoping."""
    edges = [_edge("u1", relation="invoke", resolution_state="ambiguous")]
    signals, _ = ra.build_readiness([_unit("u1")], edges, [])
    signal = _signal_by_check(signals, "dependencies_resolved")
    assert signal.stored_status == "unknown"
    assert signal.reason_code == "ambiguous_dependency"


def test_dependencies_resolved_unaffected_by_an_ambiguous_test_edge():
    """FIX ROUND 15 (M6 control): a "test" edge is a name-derived
    CONVENTION GUESS (F4), never a real declared dependency of the unit
    it is attached to - its own ambiguity is a fact about the pairing
    guess, not about this unit's dependency surface, and must not flip
    an otherwise-clean dependencies_resolved."""
    edges = [_edge("u1", relation="test", resolution_state="ambiguous")]
    signals, _ = ra.build_readiness([_unit("u1")], edges, [])
    signal = _signal_by_check(signals, "dependencies_resolved")
    assert signal.stored_status == "satisfied"
    assert signal.reason_code == "no_declared_dependencies"


def test_dependencies_resolved_satisfied_when_all_edges_resolved():
    """FIX ROUND 12b (reviewer-3): this used to rely on `_edge`'s default
    relation ("invoke") - post-scoping (round 12's F2/F5 fix), an invoke
    edge is filtered out entirely, so this test passed via the
    no-qualifying-edges/no_declared_dependencies branch, byte-identical to a unit
    with zero edges at all, and would have survived deletion of the
    all-resolved branch it names. relation="import" (like its moved
    siblings) makes this a real dependency edge that actually reaches
    and exercises that branch."""
    edges = [_edge("u1", relation="import", resolution_state="resolved", target_unit_id="u2")]
    signals, _ = ra.build_readiness([_unit("u1")], edges, [])
    signal = _signal_by_check(signals, "dependencies_resolved")
    assert signal.stored_status == "satisfied"
    assert signal.reason_code == "dependencies_resolved"


def _file_unit(unit_id: str) -> ModuleRecord:
    return ModuleRecord(
        unit_id=unit_id, kind="file", display_name=unit_id, language="java",
        paths=[f"{unit_id}.java"], source_digests={}, classification=["production"],
        container_unit_id=None, producers=[],
    )


# --------------------------------- dependencies_resolved, CR10-1 (round 14): file-scoped imports

def test_component_with_no_own_edges_but_a_single_sibling_free_file_inherits_the_files_status():
    """FIX ROUND 14 (CR10-1): a component with NO edges of its own, in a
    file with exactly ONE top-level declared type, has no attribution
    ambiguity at all - the file's own import edges honestly ARE this
    type's own dependency picture. Must mirror the file's aggregate
    status, never a vacuous satisfied."""
    file_unit = _file_unit("Solo")
    component = _unit("comp1", container_unit_id="Solo")
    edges = [_edge("Solo", relation="import", resolution_state="unresolved")]
    signals, _ = ra.build_readiness([file_unit, component], edges, [])
    component_signal = next(
        s for s in signals if s.unit_id == "comp1" and s.check == "dependencies_resolved")
    assert component_signal.stored_status == "unsatisfied"


def test_component_with_no_own_edges_and_multiple_siblings_reports_unknown_not_satisfied():
    """FIX ROUND 14 (CR10-1 MAJOR, the reviewer's Service/ServiceCache
    shape): a component with no edges of its own, sharing a file with
    ANOTHER top-level declared type, cannot honestly credit the file's
    import evidence to just this one sibling - never a vacuous
    satisfied/no_declared_dependencies (the un-evidenced positive the
    readiness policy refuses everywhere else), degrades to unknown with
    a named reason instead."""
    file_unit = _file_unit("Multi")
    service = _unit("Service", container_unit_id="Multi")
    service_cache = _unit("ServiceCache", container_unit_id="Multi")
    edges = [_edge("Multi", relation="import", resolution_state="resolved", target_unit_id="ext")]
    signals, _ = ra.build_readiness([file_unit, service, service_cache], edges, [])
    for unit_id in ("Service", "ServiceCache"):
        signal = next(
            s for s in signals if s.unit_id == unit_id and s.check == "dependencies_resolved")
        assert signal.stored_status == "unknown", unit_id
        assert signal.reason_code == "file_scoped_dependencies_not_attributed", unit_id


def test_component_with_its_own_edges_is_unaffected_by_file_scoped_imports():
    """FIX ROUND 14 (CR10-1 control): a component with its OWN real
    body evidence (an invoke/inherit/test edge attributed to it
    directly) must keep reporting on that evidence alone, regardless of
    what the file's own import edges say - own evidence always wins,
    never overridden by file-level corroboration."""
    file_unit = _file_unit("Multi")
    service = _unit("Service", container_unit_id="Multi")
    service_cache = _unit("ServiceCache", container_unit_id="Multi")
    edges = [
        _edge("Multi", relation="import", resolution_state="unresolved"),
        _edge("Service", relation="inherit", resolution_state="resolved", target_unit_id="ext"),
    ]
    signals, _ = ra.build_readiness([file_unit, service, service_cache], edges, [])
    service_signal = next(
        s for s in signals if s.unit_id == "Service" and s.check == "dependencies_resolved")
    assert service_signal.stored_status == "satisfied"


# ------------------- dependencies_resolved, CR17-2 (round 21): single-type component-vs-file

def test_single_type_component_with_own_resolved_edge_still_mirrors_a_files_unresolved_import():
    """FIX ROUND 21 (seventeenth cold read, CR17-2 MAJOR, wrong-data):
    the component-mirrors-file rule (CR10-1, round 14) fired ONLY when
    the component had ZERO own edges - a component with its own real
    edge (every servlet/exception/DAO with an `extends`/`implements`)
    flipped it off the mirror path entirely, so the component published
    SATISFIED while its own FILE published unsatisfied for an import
    edge attached to the file unit instead (imports attach file-scoped,
    CR10-1) - two contradictory published facts about the identical
    single-type file. A single-top-level-type file has no attribution
    ambiguity; the component's own verdict must never be MORE CONFIDENT
    than the file's - the worse of the two wins."""
    file_unit = _file_unit("Solo")
    component = _unit("comp1", container_unit_id="Solo")
    edges = [
        _edge("Solo", relation="import", resolution_state="unresolved"),
        _edge("comp1", relation="inherit", resolution_state="resolved", target_unit_id="ext"),
    ]
    signals, _ = ra.build_readiness([file_unit, component], edges, [])
    component_signal = next(
        s for s in signals if s.unit_id == "comp1" and s.check == "dependencies_resolved")
    assert component_signal.stored_status == "unsatisfied"


def test_single_type_component_with_own_resolved_edge_mirrors_a_files_ambiguous_import():
    """The reader's second reproduced mechanism: an ambiguous (not
    merely unresolved) file-scoped import edge must also drag the
    component's own otherwise-clean verdict down to unknown."""
    file_unit = _file_unit("Solo")
    component = _unit("comp1", container_unit_id="Solo")
    edges = [
        _edge("Solo", relation="import", resolution_state="ambiguous"),
        _edge("comp1", relation="inherit", resolution_state="resolved", target_unit_id="ext"),
    ]
    signals, _ = ra.build_readiness([file_unit, component], edges, [])
    component_signal = next(
        s for s in signals if s.unit_id == "comp1" and s.check == "dependencies_resolved")
    assert component_signal.stored_status == "unknown"
    assert component_signal.reason_code == "ambiguous_dependency"


def test_single_type_component_with_own_resolved_edge_mirrors_a_files_externality_suppressed_import():
    """The reader's third reproduced mechanism: a poisoned run's own
    externality-suppressed file-scoped import (round 20c's own
    unknown/externality_suppressed signal, never unsatisfied) must
    still drag an otherwise-satisfied component down to unknown, not
    leave it wrongly satisfied."""
    file_unit = _file_unit("Solo")
    component = _unit("comp1", container_unit_id="Solo")
    edges = [
        _edge(
            "Solo", relation="import", resolution_state="unresolved",
            externality_suppressed=True),
        _edge("comp1", relation="inherit", resolution_state="resolved", target_unit_id="ext"),
    ]
    signals, _ = ra.build_readiness([file_unit, component], edges, [], externality_poisoned=True)
    component_signal = next(
        s for s in signals if s.unit_id == "comp1" and s.check == "dependencies_resolved")
    assert component_signal.stored_status == "unknown"
    assert component_signal.reason_code == "externality_suppressed"


def test_single_type_component_and_file_both_clean_stays_satisfied():
    """Companion clean control: a single-top-level-type file where BOTH
    the component's own edge and the file's own edge resolve cleanly
    must still report satisfied - CR17-2's fix combines statuses, it
    does not manufacture a problem where none exists."""
    file_unit = _file_unit("Solo")
    component = _unit("comp1", container_unit_id="Solo")
    edges = [
        _edge("Solo", relation="import", resolution_state="resolved", target_unit_id="ext1"),
        _edge("comp1", relation="inherit", resolution_state="resolved", target_unit_id="ext2"),
    ]
    signals, _ = ra.build_readiness([file_unit, component], edges, [])
    component_signal = next(
        s for s in signals if s.unit_id == "comp1" and s.check == "dependencies_resolved")
    assert component_signal.stored_status == "satisfied"


# ------------------------------------------- dependencies_resolved, file units (N6, round 6)

def test_file_units_dependencies_resolved_derives_from_contained_units_not_vacuously_satisfied():
    """N6 (fourth cold read, fix round 6): edges attach to the declared
    TYPE, never the FILE that contains it - a file unit's own
    dependencies_resolved used to always be satisfied/no_declared_dependencies
    (it never receives outgoing edges directly), a structurally
    always-on positive signal that was never actually evidence of
    anything. A file whose CONTAINED component unit has an unresolved
    edge must roll that up onto the file's own signal, not report a
    vacuous satisfied."""
    component = ModuleRecord(
        unit_id="comp1", kind="component", display_name="comp1", language="java",
        paths=["file1.java"], source_digests={}, classification=["production"],
        container_unit_id="file1", producers=[],
    )
    file_unit = _file_unit("file1")
    edges = [_edge("comp1", relation="import", resolution_state="unresolved")]
    signals, _ = ra.build_readiness([file_unit, component], edges, [])
    file_signal = next(
        s for s in signals if s.unit_id == "file1" and s.check == "dependencies_resolved")
    assert file_signal.stored_status == "unsatisfied"


def test_file_units_with_no_contained_units_and_no_direct_edges_are_not_applicable():
    """N6 (fourth cold read, fix round 6): a plain non-code file (or one
    the adapter never understood) has no meaningful "dependencies"
    concept at all - not_applicable, never a confident positive earned
    by nothing."""
    file_unit = _file_unit("file1")
    signals, _ = ra.build_readiness([file_unit], [], [])
    file_signal = next(
        s for s in signals if s.unit_id == "file1" and s.check == "dependencies_resolved")
    assert file_signal.stored_status == "not_applicable"


def test_file_units_with_a_direct_edge_of_their_own_are_still_evaluated():
    """N6 (fourth cold read, fix round 6): a pom.xml-style file has no
    component children at all, yet build edges attach DIRECTLY to its
    own file unit (there being no component-level unit for that
    producer) - this must still be evaluated on its own direct edges,
    not swept into not_applicable just because it has no CONTAINED
    units."""
    file_unit = _file_unit("pom_xml")
    edges = [_edge("pom_xml", relation="build", resolution_state="unresolved")]
    signals, _ = ra.build_readiness([file_unit], edges, [])
    file_signal = next(
        s for s in signals if s.unit_id == "pom_xml" and s.check == "dependencies_resolved")
    assert file_signal.stored_status == "unsatisfied"


# ----------------------------------------------------------- entry_points_mapped / feature_linked

def test_entry_points_mapped_not_applicable_without_a_feature_link():
    signals, _ = ra.build_readiness([_unit("u1")], [], [])
    assert _signal_by_check(signals, "entry_points_mapped").stored_status == "not_applicable"


def test_entry_points_mapped_satisfied_when_the_unit_owns_a_feature():
    features = [_feature("Foo", "candidate", ["u1"])]
    signals, _ = ra.build_readiness([_unit("u1")], [], features)
    assert _signal_by_check(signals, "entry_points_mapped").stored_status == "satisfied"


def test_entry_points_mapped_unknown_when_an_unrecognized_main_like_shape_was_recorded():
    """FIX ROUND 13b (reviewer-3's B1 class-closer): a unit whose file
    carries the adapter's "cli_main_unrecognized" problem (a method
    literally named main, returning void, that the strict cli_main
    detector could not confidently classify) must report entry_points_
    mapped UNKNOWN, never the confident not_applicable/no_entry_point
    negative - the same three-state move round 11 already made for an
    unrecoverable route value."""
    unit = _unit("u1", adapter_problem_reasons=["cli_main_unrecognized"])
    signals, _ = ra.build_readiness([unit], [], [])
    signal = _signal_by_check(signals, "entry_points_mapped")
    assert signal.stored_status == "unknown"
    assert signal.reason_code == "cli_main_unrecognized"


def test_feature_linked_unsatisfied_with_no_feature_at_all():
    signals, _ = ra.build_readiness([_unit("u1")], [], [])
    assert _signal_by_check(signals, "feature_linked").stored_status == "unsatisfied"


def test_feature_linked_unknown_when_only_a_candidate_feature_links_it():
    features = [_feature("Foo", "candidate", ["u1"])]
    signals, summaries = ra.build_readiness([_unit("u1")], [], features)
    assert _signal_by_check(signals, "feature_linked").stored_status == "unknown"
    assert summaries[0].stored_assessment_state == "needs_evidence"


def test_feature_linked_satisfied_when_a_confirmed_feature_links_it():
    features = [_feature("Foo", "confirmed", ["u1"])]
    signals, _ = ra.build_readiness([_unit("u1")], [], features)
    assert _signal_by_check(signals, "feature_linked").stored_status == "satisfied"


# ------------------------ entry_points_mapped / feature_linked, declared_in (round 27 F1 BLOCKER)

def _entry_point(
    entry_point_id: str, *, owning_unit_id: str, declared_in_unit_id: str,
    feature_ids: list[str],
) -> EntryPointRecord:
    return EntryPointRecord(
        entry_point_id=entry_point_id, kind="http_route", name="/checkout",
        owning_unit_id=owning_unit_id, feature_ids=feature_ids,
        evidence_class="declared", declared_in_unit_id=declared_in_unit_id,
    )


def test_entry_points_mapped_satisfied_for_the_declaring_file_even_when_ownership_resolved_elsewhere():
    """FIX ROUND 27 (twenty-third cold read, F1 BLOCKER, wrong-data,
    .cr23-webxml-neg): a web.xml that DECLARES a route whose <servlet-
    class> resolves in-scan used to publish entry_points_mapped not_
    applicable/no_entry_point on a complete/0-problem run - ownership
    moves to the implementing class (CR13-2), and nothing previously
    credited the DECLARING file with the evidence at all. The declaring
    file (u_webxml, never the owner u_servlet) must be satisfied too -
    it is the exact real-world JEE shape (a web.xml naming an in-repo
    servlet class), not an edge case."""
    features = [_feature("Checkout", "candidate", ["u_servlet"])]
    entry_points = [_entry_point(
        "ep1", owning_unit_id="u_servlet", declared_in_unit_id="u_webxml",
        feature_ids=["feature-Checkout"])]
    signals, _ = ra.build_readiness(
        [_unit("u_webxml"), _unit("u_servlet")], [], features, entry_points)
    webxml_signal = next(s for s in signals if s.unit_id == "u_webxml" and s.check == "entry_points_mapped")
    servlet_signal = next(s for s in signals if s.unit_id == "u_servlet" and s.check == "entry_points_mapped")
    assert webxml_signal.stored_status == "satisfied"
    assert servlet_signal.stored_status == "satisfied"


def test_entry_points_mapped_stays_the_honest_negative_for_a_file_that_declared_nothing():
    """Companion control: a file that neither owns nor declared any
    entry point (a genuinely route-free web.xml, or an unrelated file)
    keeps its honest not_applicable/no_entry_point negative - the F1
    fix only ADDS evidence for a real declaring file, it never
    fabricates evidence for one that published nothing at all."""
    features = [_feature("Checkout", "candidate", ["u_servlet"])]
    entry_points = [_entry_point(
        "ep1", owning_unit_id="u_servlet", declared_in_unit_id="u_webxml",
        feature_ids=["feature-Checkout"])]
    signals, _ = ra.build_readiness(
        [_unit("u_webxml"), _unit("u_servlet"), _unit("u_unrelated")],
        [], features, entry_points)
    unrelated_signal = next(
        s for s in signals if s.unit_id == "u_unrelated" and s.check == "entry_points_mapped")
    assert unrelated_signal.stored_status == "not_applicable"


def test_feature_linked_reflects_the_declaring_files_own_feature_state_even_when_ownership_resolved_elsewhere():
    """FIX ROUND 27 (F1 BLOCKER): the same fix, feature_linked side -
    readiness ~608's own stale comment claimed route edges (and, by the
    same wrong model, feature linkage) always attach to the file unit
    directly; corrected. The declaring file inherits its own feature's
    state (candidate here, so unknown/feature_not_confirmed - never the
    confident unsatisfied/no_feature_link negative a file with zero
    evidence gets)."""
    features = [_feature("Checkout", "candidate", ["u_servlet"])]
    entry_points = [_entry_point(
        "ep1", owning_unit_id="u_servlet", declared_in_unit_id="u_webxml",
        feature_ids=["feature-Checkout"])]
    signals, _ = ra.build_readiness(
        [_unit("u_webxml"), _unit("u_servlet")], [], features, entry_points)
    webxml_signal = next(s for s in signals if s.unit_id == "u_webxml" and s.check == "feature_linked")
    assert webxml_signal.stored_status == "unknown"
    assert webxml_signal.reason_code == "feature_not_confirmed"


def test_feature_linked_satisfied_for_the_declaring_file_when_the_feature_is_confirmed():
    """A confirmed (not merely candidate) feature the declaring file
    itself never owns must still let that file report satisfied."""
    features = [_feature("Checkout", "confirmed", ["u_servlet"])]
    entry_points = [_entry_point(
        "ep1", owning_unit_id="u_servlet", declared_in_unit_id="u_webxml",
        feature_ids=["feature-Checkout"])]
    signals, _ = ra.build_readiness(
        [_unit("u_webxml"), _unit("u_servlet")], [], features, entry_points)
    webxml_signal = next(s for s in signals if s.unit_id == "u_webxml" and s.check == "feature_linked")
    assert webxml_signal.stored_status == "satisfied"


# -------------------------- feature_linked whole-file evidence-gap map (round 27 F2 MAJOR)

def test_feature_linked_unknown_not_confident_negative_for_an_encoding_undecodable_file():
    """FIX ROUND 27 (twenty-third cold read, F2 MAJOR, wrong-data,
    .cr23-featgap): feature_linked used to OMIT every whole-file
    evidence-gap reason (encoding_undecodable, parse_failed,
    unsupported_language, ...) from its own reason map, while entry_
    points_mapped/dependencies_resolved/source_understood correctly
    routed to unknown for the identical reason - the reader's own
    control is airtight: byte-identical controllers differing only in
    encoding flip feature_linked from unknown to a CONFIDENT unsatisfied/
    no_feature_link. Trust the recorded gap here too."""
    unit = _unit("u1", adapter_problem_reasons=["encoding_undecodable"])
    signals, _ = ra.build_readiness([unit], [], [])
    signal = _signal_by_check(signals, "feature_linked")
    assert signal.stored_status == "unknown"
    assert signal.reason_code == "adapter_encoding_undecodable"


def test_feature_linked_unknown_not_confident_negative_for_an_unsupported_language_file():
    """FIX ROUND 27 (F2 MAJOR): the tier-2 JSP/SQL/Kotlin twin of the
    encoding case above - the reader's own .cr23-big resource_limit
    shape and the tier-2 unsupported_language shape are the SAME class
    of gap, both must route to unknown here."""
    unit = _unit("u1", adapter_problem_reasons=["unsupported_language"])
    signals, _ = ra.build_readiness([unit], [], [])
    signal = _signal_by_check(signals, "feature_linked")
    assert signal.stored_status == "unknown"
    assert signal.reason_code == "adapter_unsupported_language"


def test_feature_linked_unknown_not_confident_negative_for_a_resource_limit_file():
    """FIX ROUND 27 (F2 MAJOR, .cr23-big): a file this run skipped
    adapter analysis for entirely because it exceeded the per-file
    resource cap must not publish a confident feature_linked negative
    either."""
    unit = _unit("u1", adapter_problem_reasons=["resource_limit"])
    signals, _ = ra.build_readiness([unit], [], [])
    signal = _signal_by_check(signals, "feature_linked")
    assert signal.stored_status == "unknown"
    assert signal.reason_code == "adapter_resource_limit"


def test_feature_linked_unaffected_control_a_genuinely_understood_feature_free_unit_stays_negative():
    """Companion control: a unit the adapter genuinely UNDERSTOOD, with
    no recorded evidence gap and no feature link at all, must keep the
    honest confident negative - the F2 fix only routes RECORDED gaps to
    unknown, it never manufactures unknown for real, evidenced
    absence."""
    signals, _ = ra.build_readiness([_unit("u1")], [], [])
    signal = _signal_by_check(signals, "feature_linked")
    assert signal.stored_status == "unsatisfied"
    assert signal.reason_code == "no_feature_link"


# ----------------------------------------------------------- test_evidence_located

def test_test_evidence_located_not_applicable_for_a_test_classified_unit():
    """FIX ROUND 14 (CR10-7, the tautology half): a test class satisfying
    this check about ITSELF is meaningless - the check's subject is the
    PRODUCTION unit a test pairs to, never the test class's own record."""
    signals, _ = ra.build_readiness([_unit("u1", classification="test")], [], [])
    assert _signal_by_check(signals, "test_evidence_located").stored_status == "not_applicable"


def test_test_evidence_located_satisfied_when_targeted_by_a_declared_or_extracted_test_edge():
    """An extracted/declared TEST-relation edge (real evidence, not a
    convention guess) still satisfies - no producer emits this shape
    today (round 15b's own finding: the only test-edge producer emits
    "inferred"), but the check must honor it correctly if one ever
    does, the same way it already honors every other closed evidence
    class."""
    edges = [_edge(
        "u2", relation="test", resolution_state="resolved", target_unit_id="u1",
        evidence_class="declared")]
    signals, _ = ra.build_readiness([_unit("u1")], edges, [])
    assert _signal_by_check(signals, "test_evidence_located").stored_status == "satisfied"


def test_test_evidence_located_satisfied_when_a_test_unit_actually_invokes_the_target():
    """FIX ROUND 15b (reviewer-3's F4 leg 3, MAJOR - closing an
    unreachable branch round 15 itself introduced): round 15's own
    requirement of extracted/declared evidence on the TEST relation made
    "satisfied" UNREACHABLE on any real run, since the only test-edge
    producer emits "inferred" - no_test_evidence_found (a POSITIVE claim)
    published for every production unit in every repo, even one whose
    real JUnit test class genuinely calls it. The reviewer's own remedy:
    the data already exists - an EXTRACTED invoke/import edge FROM a
    test-classified unit TO a production unit IS real evidence the test
    body actually references the target. BillingEngineTest (a real JUnit
    class) calling BillingEngine.charge() must satisfy BillingEngine's
    own test_evidence_located."""
    edges = [_edge(
        "test-unit", relation="invoke", resolution_state="resolved", target_unit_id="prod-unit",
        evidence_class="extracted")]
    modules = [
        _unit("test-unit", classification="test"),
        _unit("prod-unit", classification="production"),
    ]
    signals, _ = ra.build_readiness(modules, edges, [])
    signal = next(
        s for s in signals if s.unit_id == "prod-unit" and s.check == "test_evidence_located")
    assert signal.stored_status == "satisfied"


def test_test_evidence_located_unknown_when_a_test_unit_does_not_reference_the_target():
    """FIX ROUND 15b control: a test-classified unit's name-pairing guess
    ALONE (round 15's fx4 shape - the "inferred" convention edge, no real
    invoke/import evidence of any kind from the test unit) must KEEP
    failing toward unknown. This is the exact regression this round's
    fix must not reopen."""
    edges = [_edge(
        "test-unit", relation="test", resolution_state="resolved", target_unit_id="prod-unit",
        evidence_class="inferred")]
    modules = [
        _unit("test-unit", classification="test"),
        _unit("prod-unit", classification="production"),
    ]
    signals, _ = ra.build_readiness(modules, edges, [])
    signal = next(
        s for s in signals if s.unit_id == "prod-unit" and s.check == "test_evidence_located")
    assert signal.stored_status == "unknown"


def test_test_evidence_located_unaffected_by_an_ordinary_production_to_production_invoke():
    """FIX ROUND 15b control: the new invoke/import-from-test-unit rule
    is gated on the SOURCE unit's own test classification - an ordinary
    production class invoking another production class is real evidence
    of an ordinary call, not test coverage, and must never satisfy
    test_evidence_located for the callee."""
    edges = [_edge(
        "prod-caller", relation="invoke", resolution_state="resolved", target_unit_id="prod-unit",
        evidence_class="extracted")]
    modules = [
        _unit("prod-caller", classification="production"),
        _unit("prod-unit", classification="production"),
    ]
    signals, _ = ra.build_readiness(modules, edges, [])
    signal = next(
        s for s in signals if s.unit_id == "prod-unit" and s.check == "test_evidence_located")
    assert signal.stored_status == "unknown"


def test_test_evidence_located_reason_code_names_an_inferred_pairing_when_one_exists():
    """FIX ROUND 17 (thirteenth cold read, CR13-7 MINOR): "no_test_
    evidence_found" is a FALSE statement when this run's own
    dependencies.json already holds an inferred test-pairing edge naming
    this unit - it read SOMETHING, just not enough. Split by what was
    actually found: insufficient_test_evidence (an inferred-only pairing
    exists) vs no_test_evidence_found (nothing at all). stored_status
    stays unknown either way."""
    edges = [_edge(
        "test-unit", relation="test", resolution_state="resolved", target_unit_id="prod-unit",
        evidence_class="inferred")]
    modules = [
        _unit("test-unit", classification="test"),
        _unit("prod-unit", classification="production"),
    ]
    signals, _ = ra.build_readiness(modules, edges, [])
    signal = next(
        s for s in signals if s.unit_id == "prod-unit" and s.check == "test_evidence_located")
    assert signal.stored_status == "unknown"
    assert signal.reason_code == "insufficient_test_evidence"


def test_test_evidence_located_reason_code_is_no_test_evidence_found_with_nothing_at_all():
    """Companion negative case: a production unit named by NO test-
    relation edge at all (not even an inferred one) keeps the ORIGINAL
    no_test_evidence_found spelling - "we looked, found nothing," a
    materially different, weaker claim than "found an inferred pairing
    but not enough"."""
    signals, _ = ra.build_readiness([_unit("prod-unit", classification="production")], [], [])
    signal = _signal_by_check(signals, "test_evidence_located")
    assert signal.stored_status == "unknown"
    assert signal.reason_code == "no_test_evidence_found"


def test_test_evidence_located_stays_unknown_for_an_inferred_convention_only_pairing():
    """FIX ROUND 15 (eleventh cold read, F4 MAJOR, wrong-data, cr11-fx4
    verbatim): the name-derived test pairing (strip Test/Tests/IT,
    resolve the remainder) is a CONVENTION GUESS - the target identifier
    never actually appears in the test file's own source, and adapters.
    java now publishes it evidence_class="inferred" rather than
    "extracted". A convention-only pairing must never drive
    test_evidence_located past unknown - IntegrationTests (which
    actually exercises only BillingEngine) must not stamp coverage onto
    a same-named "Integration" that was never really tested."""
    edges = [_edge(
        "u2", relation="test", resolution_state="resolved", target_unit_id="u1",
        evidence_class="inferred")]
    signals, _ = ra.build_readiness([_unit("u1")], edges, [])
    assert _signal_by_check(signals, "test_evidence_located").stored_status == "unknown"


def test_test_evidence_located_agrees_between_a_file_and_its_own_contained_type():
    """FIX ROUND 15 (eleventh cold read, F4 MAJOR part 3, wrong-data): a
    "test" edge always resolves to the CONTAINED TYPE (a per-type fact),
    never the file unit directly - so the file's own test_evidence_located
    used to report unknown while its own contained type, for the
    IDENTICAL underlying fact, reported satisfied. The file must now
    inherit its contained type's own tested status, the same "roll a
    per-type fact up to the owning file" idiom CR10-1 already established
    for dependencies_resolved."""
    edges = [_edge(
        "u-tester", relation="test", resolution_state="resolved", target_unit_id="u-component")]
    file_unit = _unit("u-file", container_unit_id=None)
    # ModuleRecord's own `kind` defaults to "component" via _unit's helper
    # signature - build a real file-kind unit directly, matching what
    # modules_artifact actually publishes for a file with one contained
    # type.
    from dataclasses import replace
    file_unit = replace(file_unit, kind="file")
    component_unit = _unit("u-component", container_unit_id="u-file")
    signals, _ = ra.build_readiness([file_unit, component_unit], edges, [])
    component_signal = next(
        s for s in signals if s.unit_id == "u-component" and s.check == "test_evidence_located")
    file_signal = next(
        s for s in signals if s.unit_id == "u-file" and s.check == "test_evidence_located")
    assert component_signal.stored_status == "satisfied"
    assert file_signal.stored_status == "satisfied"


def test_test_evidence_located_unknown_otherwise():
    signals, _ = ra.build_readiness([_unit("u1")], [], [])
    assert _signal_by_check(signals, "test_evidence_located").stored_status == "unknown"


# ----------------------------------------------------------- boundaries_identified (always unknown)

def test_boundaries_identified_is_always_unknown_this_slice():
    signals, _ = ra.build_readiness([_unit("u1")], [], [])
    boundary_signal = _signal_by_check(signals, "boundaries_identified")
    assert boundary_signal.stored_status == "unknown"
    assert boundary_signal.basis == "detected"


# ----------------------------------------------------------- determinism

def test_signal_id_is_deterministic():
    first, _ = ra.build_readiness([_unit("u1")], [], [])
    second, _ = ra.build_readiness([_unit("u1")], [], [])
    assert {s.signal_id for s in first} == {s.signal_id for s in second}


def test_every_signal_names_the_default_policy():
    signals, _ = ra.build_readiness([_unit("u1")], [], [])
    assert all(s.policy["policy_id"] == ra.POLICY_ID for s in signals)
    assert all(s.policy["policy_version"] == ra.POLICY_VERSION for s in signals)
