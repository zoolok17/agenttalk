"""``dependencies.json`` record assembly (DESIGN-55-comprehension-plane.md,
Artifact 2: dependency edges).

"An adapter may emit a relation only when its versioned extraction rule
names a producer for that relation. Unsupported relation types remain
coverage gaps; they are never coerced into `data` or another
healthy-looking generic edge." This module is also where cross-file
target resolution happens (design step 6: "Normalize records, resolve only
evidenced edges") - the Java adapter (item 3) deliberately emits LOCAL,
unresolved-target candidate claims; this is the later, global step that
reconciles them against every file's declared types in the same scan.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from . import digests
from .adapters import java as java_adapter
from .errors import ComprehensionError

DEPENDENCIES_ARTIFACT_TYPE = "agenttalk.comprehension.dependencies"
DEPENDENCIES_SCHEMA_VERSION = 1

#: DESIGN-55-comprehension-plane.md, Artifact 2: "a closed relation from
#: the coarse slice-1 vocabulary."
CLOSED_RELATIONS = frozenset({
    "import", "include", "inherit", "invoke", "route", "data", "configuration",
    "build", "test",
})


class UnsupportedRelationClaimed(ComprehensionError):
    """A producer emitted a relation outside the closed S1 vocabulary -
    an adapter bug, not a legitimate edge. Caught here so dependencies.json
    can never silently carry an out-of-contract relation."""

    reason_code = "comprehension_unsupported_relation_claimed"


@dataclass(frozen=True)
class DependencyRecord:
    edge_id: str
    from_unit_id: str
    relation: str
    phase: str
    optional: bool
    evidence_class: str
    resolution_state: str
    target_unit_id: str | None = None
    target_external: str | None = None
    target_unresolved: str | None = None
    confidence: str | None = None
    producers: list[dict[str, Any]] = field(default_factory=list)
    conflict_id: str | None = None
    evidence: list[dict[str, Any]] = field(default_factory=list)
    # FIX ROUND 15 (eleventh cold read, M4 JUDGE - taken): the design's
    # own wording is "Ambiguous resolution creates an unresolved edge
    # WITH CANDIDATES" - an `ambiguous` record used to carry none at all,
    # even though the registry always knows exactly which units tied at
    # resolution time. Populated ONLY for `resolution_state == "ambiguous"`
    # (empty otherwise, the same list-shaped default every other optional
    # field here already uses); a real registry collision (two units
    # declaring the identical qualified name) and a same-simple-name-
    # across-packages tie both publish their own real candidate set.
    candidate_unit_ids: list[str] = field(default_factory=list)
    # FIX ROUND 20c (readiness carry, inherited from round 20): True only
    # when this specific edge's `unresolved` resolution_state came from
    # the poison rule (round 20's M1+M2) - never from a genuine registry
    # miss (a duplicate name, an unexpanded Maven property, a degraded-
    # file suffix match). The readiness layer (readiness_artifact.py's
    # own _check_dependencies_resolved) reads this to distinguish "this
    # producer abstained from a positive external claim because this
    # run's own external surface is unknown" from "this producer found a
    # real, unresolved dependency" - the two used to collapse into the
    # identical unsatisfied/unresolved_dependency signal.
    externality_suppressed: bool = False
    #: FIX ROUND 29 (twenty-fifth cold read, F4 MAJOR, completeness):
    #: ``dependency_summary.routes`` (projector.py) used to count EVERY
    #: route-relation edge as one bucket - both a served route AND an
    #: intercepting filter (micro-round 27b's own JUDGE ruling keeps
    #: ``relation`` itself frozen at ``"route"`` for both, the served-
    #: vs-intercepts distinction living on the paired entry point's own
    #: ``kind`` instead) - but nothing let a consumer tell the two
    #: apart from the pre-aggregated integer alone, unlike
    #: ``entry_points_by_kind``'s own already-separated ``http_route``/
    #: ``http_filter`` counts for the identical fact. ``"http_route"``/
    #: ``"http_filter"`` (mirroring ``JavaEntryPointClaim.kind``'s own
    #: vocabulary exactly), ``None`` for every non-route-relation edge -
    #: set once, here, from the adapter's own emission-site knowledge
    #: (``JavaEdgeClaim.target_kind``, ``"external_route"``/``"external_
    #: filter"``), never re-derived by guessing from an edge's own
    #: target string.
    route_kind: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "edge_id": self.edge_id,
            "from_unit_id": self.from_unit_id,
            "target_unit_id": self.target_unit_id,
            "target_external": self.target_external,
            "target_unresolved": self.target_unresolved,
            "relation": self.relation,
            "phase": self.phase,
            "optional": self.optional,
            "evidence_class": self.evidence_class,
            "resolution_state": self.resolution_state,
            "confidence": self.confidence,
            "producers": self.producers,
            "conflict_id": self.conflict_id,
            "evidence": self.evidence,
            "candidate_unit_ids": self.candidate_unit_ids,
            "externality_suppressed": self.externality_suppressed,
            "route_kind": self.route_kind,
        }


def dependency_record_from_json(payload: dict[str, Any]) -> DependencyRecord:
    """The inverse of :meth:`DependencyRecord.to_json`."""
    return DependencyRecord(
        edge_id=payload["edge_id"], from_unit_id=payload["from_unit_id"],
        relation=payload["relation"], phase=payload["phase"], optional=payload["optional"],
        evidence_class=payload["evidence_class"], resolution_state=payload["resolution_state"],
        target_unit_id=payload.get("target_unit_id"), target_external=payload.get("target_external"),
        target_unresolved=payload.get("target_unresolved"), confidence=payload.get("confidence"),
        producers=list(payload.get("producers", [])), conflict_id=payload.get("conflict_id"),
        evidence=list(payload.get("evidence", [])),
        candidate_unit_ids=list(payload.get("candidate_unit_ids", [])),
        externality_suppressed=payload.get("externality_suppressed", False),
        route_kind=payload.get("route_kind"),
    )


def _java_component_unit_id(relative_path: str, qualified_name: str) -> str:
    return digests.unit_id(kind="component", paths=[relative_path], qualified_name=qualified_name)


def _java_file_unit_id(relative_path: str) -> str:
    return digests.unit_id(kind="file", paths=[relative_path], qualified_name=None)


def _build_registry(
    java_results: dict[str, java_adapter.JavaFileResult],
) -> tuple[
    dict[str, str], dict[str, list[str]], dict[str, str], set[str], dict[str, list[str]],
    set[str],
]:
    """M12 (cold-read, PR-B fix round 3): two units DECLARING the same
    fully-qualified name (a real collision, not merely a same-SIMPLE-name
    coincidence) used to last-wins into ``by_qualified_name`` with no
    collision handling - a later scan-order-dependent unit would silently
    replace an earlier one, and every edge resolving to that name would
    confidently pick whichever happened to win, never reporting the
    conflict. The full conflict/merge machinery (``conflict_id``) stays
    out of this slice (named PR-C entry item), but a duplicate qualified
    name must never resolve confidently either way: it is removed from
    ``by_qualified_name`` entirely once a second claimant is seen, so an
    exact-name lookup finds nothing there and any resolution instead falls
    through to the existing simple-name-ambiguity path below (which
    already reports ``ambiguous`` for 2+ same-simple-name candidates -
    true here by construction, since a duplicate qualified name shares its
    own simple name with itself)."""
    by_qualified_name: dict[str, str] = {}
    duplicate_qualified_names: set[str] = set()
    by_simple_name: dict[str, list[str]] = {}
    file_unit_id_by_path: dict[str, str] = {}
    # FIX ROUND 15 (eleventh cold read, M4 JUDGE - taken): unlike
    # `by_qualified_name` (which drops a name entirely once a second
    # claimant appears, so it can never answer "who are the candidates"
    # for a real registry collision), this accumulates EVERY unit_id per
    # qualified name unconditionally - the one place that still knows
    # the exact candidate set an `ambiguous` registry-collision record
    # needs to publish.
    unit_ids_by_qualified_name: dict[str, list[str]] = {}
    # FIX ROUND 16 (twelfth cold read, B3 BLOCKER): every package with at
    # least one in-scan component, keyed by the qualified name's own
    # dotted prefix - the one place that can answer "does a wildcard
    # import's package actually name part of this scan" without
    # reopening the per-member ambiguity round 12b's named limit
    # deliberately left closed.
    in_scan_packages: set[str] = set()
    for path, result in java_results.items():
        file_unit_id_by_path[path] = _java_file_unit_id(path)
        for unit_claim in result.units:
            uid = _java_component_unit_id(path, unit_claim.qualified_name)
            if unit_claim.qualified_name in by_qualified_name:
                duplicate_qualified_names.add(unit_claim.qualified_name)
            else:
                by_qualified_name[unit_claim.qualified_name] = uid
            by_simple_name.setdefault(unit_claim.simple_name, []).append(uid)
            unit_ids_by_qualified_name.setdefault(unit_claim.qualified_name, []).append(uid)
            if "." in unit_claim.qualified_name:
                in_scan_packages.add(unit_claim.qualified_name.rsplit(".", 1)[0])
    for name in duplicate_qualified_names:
        by_qualified_name.pop(name, None)
    return (
        by_qualified_name, by_simple_name, file_unit_id_by_path, duplicate_qualified_names,
        unit_ids_by_qualified_name, in_scan_packages,
    )


def _exact_qualified_lookup(target: str, by_qualified_name: dict[str, str]) -> str | None:
    """The exact, non-fuzzy half of internal resolution: ``target`` names
    an in-scan type's fully-qualified name, verbatim - never a similarity
    guess. Shared verbatim by both :func:`_resolve_internal_candidate`
    (whose ``extends``/``implements``/test-pairing callers also need the
    simple-name fallback below, for unqualified references like a bare
    ``extends Base``) and the import path (D-1, reviewer-3 PR-B delta
    review round 2), whose target is already fully qualified and so never
    needs - and must never receive - that fallback: reusing it for imports
    would risk a genuinely-external import matching a same-named LOCAL
    type by coincidence, a false positive the import path must not
    produce."""
    return by_qualified_name.get(target)


def _resolve_internal_candidate(
    target: str, by_qualified_name: dict[str, str], by_simple_name: dict[str, list[str]],
    duplicate_qualified_names: set[str], *, local_qualified_by_simple: dict[str, str],
    import_qualified_by_simple: dict[str, str], package: str | None,
    unit_ids_by_qualified_name: dict[str, list[str]] | None = None,
) -> tuple[str, str | None, str | None, str | None, list[str] | None]:
    """Returns ``(resolution_state, target_unit_id, target_unresolved,
    confidence, candidate_unit_ids)`` - the last element is only ever
    non-``None`` when ``resolution_state == "ambiguous"`` (FIX ROUND 15,
    M4 JUDGE - taken: the design's own text names an ambiguous edge as
    carrying candidates; this is the record-shape addition that
    publishes them).

    FIX ROUND 12 (eighth cold read, F1 BLOCKER): this used to fall back to
    a GLOBAL simple-name match across the whole scan whenever exactly one
    declared type anywhere shared ``target``'s bare name - even when the
    source file's OWN evidence (an inline fully-qualified spelling, or its
    own ``import``) named a DIFFERENT, unrelated package verbatim. That
    discarded the real target's spelling for a name-similarity guess -
    exactly what design line 418 forbids ("The scanner never invents an
    internal target because names look similar. Ambiguous resolution
    creates an unresolved edge with candidates."). Resolution now consults
    the file's OWN evidence FIRST, in Java's actual scoping order, and a
    bare name with no supporting evidence is unresolved - never a guess,
    regardless of how many (0 or 1) same-named candidates happen to exist
    elsewhere in the scan:

    1. an inline fully-qualified target (``target`` itself contains a
       dot) resolves by EXACT match only - a miss is unresolved with the
       source's own spelling retained, never a simple-name fallback (a
       genuine registry collision - two files declaring the identical
       qualified name - is the one exception: that IS two real
       candidates, correctly ``ambiguous``, not zero);
    2. a bare name declared as a type IN THIS SAME FILE resolves via that
       declaration's own qualified name - guaranteed correct, not a
       guess (Java's own shadowing rule);
    3. else a bare name this file's own ``import`` names resolves ONLY
       via that import - exact match against in-scan qualified names; a
       miss is unresolved with the IMPORTED spelling retained (the
       reader can see exactly which package was meant, even though nothing
       in-scan answers it);
    4. else a bare name matching ``{this file's package}.{name}`` may
       resolve as an implicit same-package sibling - Java's actual rule,
       not similarity (a JDK/``java.lang`` default identifier falls
       through here too: it has no in-scan candidate under any package,
       so it lands unresolved on its own, never a false capture);
    5. anything else is unresolved (or ``ambiguous`` if 2+ same-simple-name
       candidates exist in-scan with no supporting evidence either way) -
       target spelling retained.

    NAMED LIMIT (round 12b, reviewer-3): a WILDCARD import (``import
    com.acme.util.*;``) never populates ``import_qualified_by_simple``
    (it names a package, not a single type, and the adapter itself
    classifies it ``external`` rather than ``internal_exact_or_external``
    - see ``parse_java_source``) - so a bare name real Java WOULD resolve
    through that wildcard, and that genuinely IS declared in-scan, can
    still land unresolved here if it is not also a same-file declaration
    or a same-package sibling. This is a deliberate, safe UNDER-claim
    (the direct, documented consequence of deleting the global bare-name
    fallback that made F1's false positives possible in the first place)
    - resolving a wildcard import correctly would require checking every
    wildcard-imported package's actual membership, reintroducing the same
    kind of cross-package ambiguity this fix exists to close. Out of
    scope this slice.
    """
    unit_ids_by_qualified_name = unit_ids_by_qualified_name or {}
    if "." in target:
        exact = _exact_qualified_lookup(target, by_qualified_name)
        if exact is not None:
            return "resolved", exact, None, "high", None
        if target in duplicate_qualified_names:
            candidates = sorted(unit_ids_by_qualified_name.get(target, []))
            return "ambiguous", None, target, None, candidates
        return "unresolved", None, target, None, None

    if target in local_qualified_by_simple:
        exact = _exact_qualified_lookup(local_qualified_by_simple[target], by_qualified_name)
        if exact is not None:
            return "resolved", exact, None, "high", None

    if target in import_qualified_by_simple:
        imported = import_qualified_by_simple[target]
        exact = _exact_qualified_lookup(imported, by_qualified_name)
        if exact is not None:
            return "resolved", exact, None, "high", None
        return "unresolved", None, imported, None, None

    if package:
        exact = _exact_qualified_lookup(f"{package}.{target}", by_qualified_name)
        if exact is not None:
            return "resolved", exact, None, "medium", None

    candidates = by_simple_name.get(target, [])
    if len(candidates) > 1:
        return "ambiguous", None, target, None, sorted(candidates)
    return "unresolved", None, target, None, None


def _bare_head_qualified_name(
    head: str, by_qualified_name: dict[str, str], *, local_qualified_by_simple: dict[str, str],
    import_qualified_by_simple: dict[str, str], package: str | None,
) -> str | None:
    """FIX ROUND 14 (tenth cold read, CR10-3 MAJOR): resolves a BARE
    (single-segment) name to its REAL, full qualified name via the same
    evidence order the bare-name ladder in :func:`_resolve_internal_
    candidate` uses (same-file declaration, then this file's own
    import, then an implicit same-package sibling) - returns the
    qualified name string on success, ``None`` otherwise. Used by
    :func:`_resolve_internal_candidate_chain` to re-attach a dotted
    chain's remaining segments onto the HEAD's own real qualified name
    (never the bare head alone) when checking whether a longer prefix
    is a genuine NESTED type."""
    if head in local_qualified_by_simple:
        candidate = local_qualified_by_simple[head]
        if _exact_qualified_lookup(candidate, by_qualified_name) is not None:
            return candidate
        return None
    if head in import_qualified_by_simple:
        candidate = import_qualified_by_simple[head]
        if _exact_qualified_lookup(candidate, by_qualified_name) is not None:
            return candidate
        return None
    if package:
        candidate = f"{package}.{head}"
        if _exact_qualified_lookup(candidate, by_qualified_name) is not None:
            return candidate
    return None


def _resolve_internal_candidate_chain(
    target: str, by_qualified_name: dict[str, str], by_simple_name: dict[str, list[str]],
    duplicate_qualified_names: set[str], *, local_qualified_by_simple: dict[str, str],
    import_qualified_by_simple: dict[str, str], package: str | None,
    unit_ids_by_qualified_name: dict[str, list[str]] | None = None,
) -> tuple[str, str | None, str | None, str | None, list[str] | None]:
    """FIX ROUND 14 (tenth cold read, CR10-3 MAJOR): a dotted invoke
    qualifier may be a MEMBER-NAVIGATION CHAIN, not a type reference at
    all - ``Status.ACTIVE.code()`` captures qualifier ``"Status.ACTIVE"``
    (an enum CONSTANT, not a dependency target); ``Config.Defaults.
    timeout()`` captures ``"Config.Defaults"`` even when ``Config``'s
    nested ``Defaults`` type genuinely IS a published unit of this same
    run. Publishing either verbatim as an unresolved target FABRICATES a
    target that was never a type at all (the enum constant case) or
    silently drops a real, resolvable dependency (the nested-type case,
    whose fan-in then wrongly stays zero).

    Tries every dotted prefix of ``target``, LONGEST first - both the
    prefix AS WRITTEN (a literal fully-qualified spelling) and the
    prefix with its OWN head segment resolved to its real qualified name
    and the remaining segments re-attached (catches a nested type
    reached through an unqualified or same-package head, e.g. ``Config.
    Defaults`` when ``Config`` itself resolves via an import) - the
    first prefix that resolves (or is a genuine registry collision) wins,
    on the theory that everything AFTER it was a member/field/constant
    access, never a nested type. Falls through to the ordinary bare-name
    ladder for just the head segment alone if no dotted prefix resolves.
    Never truncates the PUBLISHED spelling on failure - an unresolved
    outcome always retains the FULL original chain, never a partial
    guess at which segment was "the real" boundary."""
    unit_ids_by_qualified_name = unit_ids_by_qualified_name or {}
    if "." not in target:
        return _resolve_internal_candidate(
            target, by_qualified_name, by_simple_name, duplicate_qualified_names,
            local_qualified_by_simple=local_qualified_by_simple,
            import_qualified_by_simple=import_qualified_by_simple, package=package,
            unit_ids_by_qualified_name=unit_ids_by_qualified_name,
        )
    segments = target.split(".")
    head_qualified = _bare_head_qualified_name(
        segments[0], by_qualified_name, local_qualified_by_simple=local_qualified_by_simple,
        import_qualified_by_simple=import_qualified_by_simple, package=package,
    )
    for i in range(len(segments), 1, -1):
        prefix_as_written = ".".join(segments[:i])
        exact = _exact_qualified_lookup(prefix_as_written, by_qualified_name)
        if exact is not None:
            return "resolved", exact, None, "high", None
        if prefix_as_written in duplicate_qualified_names:
            candidates = sorted(unit_ids_by_qualified_name.get(prefix_as_written, []))
            return "ambiguous", None, target, None, candidates
        if head_qualified is not None:
            reattached = head_qualified + "." + ".".join(segments[1:i])
            exact = _exact_qualified_lookup(reattached, by_qualified_name)
            if exact is not None:
                return "resolved", exact, None, "high", None
    # Nothing dotted resolved - fall through to the ordinary bare-name
    # ladder for the head segment ALONE (e.g. "Status.ACTIVE" reduces to
    # just "Status", dropping the constant-access tail entirely). Any
    # outcome other than a clean resolve retains the FULL original
    # chain as the published spelling - never the truncated head alone.
    state, unit_id, _unresolved, confidence, candidates = _resolve_internal_candidate(
        segments[0], by_qualified_name, by_simple_name, duplicate_qualified_names,
        local_qualified_by_simple=local_qualified_by_simple,
        import_qualified_by_simple=import_qualified_by_simple, package=package,
        unit_ids_by_qualified_name=unit_ids_by_qualified_name,
    )
    if state == "resolved":
        return "resolved", unit_id, None, confidence, None
    return state, None, target, None, candidates


def _producer(
    *, name: str, version: int, rule_version: int, source_digest: str | None, basis: str,
) -> dict[str, Any]:
    """FIX ROUND 37 (thirty-first cold read, F5 MAJOR, wrong-data):
    ``basis`` used to be the hardcoded literal ``"extracted"`` here,
    regardless of the SAME record's own ``evidence_class`` field - the
    design defines ``basis`` as extracted-vs-inferred-vs-declared, and
    ``JavaEdgeClaim.evidence_class`` already carries EXACTLY that same
    three-way fact for this exact edge (measured: 65 records published
    ``evidence_class: "declared"``/4 ``"inferred"`` beside a ``basis``
    claiming ``"extracted"`` - two contradictory provenance claims about
    the SAME record). The caller passes its own edge's ``evidence_class``
    straight through - never re-derived, so the two can never drift."""
    return {
        "producer": name, "producer_version": version, "rule_version": rule_version,
        "basis": basis, "source_digest": source_digest,
    }


def _degraded_java_suffix_match(qualified_name: str, degraded_paths: frozenset[str]) -> bool:
    """FIX ROUND 12 (eighth cold read, F2 MAJOR): an import naming a type
    this SAME run could not fully read or parse (a worker-level problem -
    an adapter resource cap, a read/parse failure - names the exact
    relative path) must never be stamped a confident, positive EXTERNAL
    claim just because the in-scan registry has no entry for it - the
    registry has no entry BECAUSE the file degraded away, not because
    the type is genuinely third-party. Matched by path SUFFIX, never an
    exact string: a qualified name alone does not know which source root
    (``src/main/java``, ``src/test/java``, none at all) the file actually
    lives under, but Java's own rule that a public top-level type's file
    is named exactly ``{SimpleName}.java`` makes the suffix match
    unambiguous in practice."""
    suffix = qualified_name.replace(".", "/") + ".java"
    return any(path == suffix or path.endswith("/" + suffix) for path in degraded_paths)


#: FIX ROUND 20b (seventeenth-round dispatch, THE ASK - taken), corrected
#: round 20c (its own imprecision, owned): these three namespaces are NOT
#: all reserved the same way, and the exemption's own strength differs
#: per prefix accordingly:
#:
#: - ``java.*`` is RUNTIME-ENFORCED - the JLS reserves it, and the JVM's
#:   own classloader rejects any attempt to define a class under it
#:   ("Prohibited package name: java.*") outside the bootstrap classpath.
#:   No vendored or excluded region this run scanned can EVER legitimately
#:   contain a real ``java.*`` declaration - the exemption here is total.
#: - ``javax.*``/``jakarta.*`` are CONVENTION-RESERVED, not enforced by
#:   any runtime - they are the servlet/EE and Jakarta EE specs' own
#:   namespace conventions, but the classloader will happily load a class
#:   under either. ``javax.servlet`` itself is ordinary, VENDORABLE
#:   third-party code (the servlet-api jar) - nothing stops a vendored
#:   copy of it, or a first-party shim placed under the same convention,
#:   from sitting inside an excluded region.
#:
#: NAMED RESIDUAL (three conditions, mild consequence): first-party code
#: that (1) is genuinely placed under one of these reserved-looking
#: namespaces, AND (2) sits inside a region this run excluded, AND (3)
#: this run is poisoned - publishes as a confident EXTERNAL claim instead
#: of the poison rule's own honest unresolved. All three conditions must
#: hold simultaneously, and the consequence is mild: a ``javax.*``/
#: ``jakarta.*`` type reported external is exactly what a migration
#: reader already expects to see for that namespace, not a surprising or
#: actionable-looking claim. Keeping all three exemptions (rather than
#: narrowing to ``java.*`` alone) is the deliberate, recommended trade -
#: narrowing would re-noise every legacy poisoned run's own javax.*
#: imports (the single most common reserved-namespace import in any pre-
#: Jakarta-EE-9 codebase) for a residual this narrow and this mild.
_PLATFORM_RESERVED_NAMESPACE_PREFIXES = ("java.", "javax.", "jakarta.")


def _is_platform_reserved_namespace(qualified_name: str) -> bool:
    return any(qualified_name.startswith(prefix) for prefix in _PLATFORM_RESERVED_NAMESPACE_PREFIXES)


def _classify_registry_miss(
    qualified_name: str, *, duplicate_qualified_names: set[str],
    unit_ids_by_qualified_name: dict[str, list[str]], degraded_paths: frozenset[str],
    externality_poisoned: bool,
) -> tuple[str, list[str] | None, bool]:
    """FIX ROUND 16 (twelfth cold read, B1+B2 BLOCKERS - the shared
    class): called ONLY once an exact registry lookup for
    ``qualified_name`` has already MISSED. Never itself performs the
    exact-match check - callers keep that (it needs their own
    type-prefix-vs-full-target distinction). Returns
    ``(outcome, candidate_unit_ids, externality_suppressed)`` where ``outcome`` is one of
    ``"ambiguous"`` / ``"unresolved"`` / ``"external"`` - the class the
    round-16 dispatch itself names: an import target may publish
    EXTERNAL only on POSITIVE grounds (not in-scan AND not a duplicate
    qualified name AND not under an excluded region) - any other miss is
    ``unresolved`` (or ``ambiguous`` with candidates), spelling retained,
    never a confident external claim over an absent registry entry that
    is actually just a naming collision or a directory this run never
    walked.

    FIX ROUND 19 (fifteenth cold read, F2 MAJOR, wrong-data, HARD RULE):
    a target still carrying an UNEXPANDED Maven property placeholder
    (``${...}``, e.g. ``${project.groupId}:billing-core`` - Maven's own
    documented sibling-dependency idiom) can never satisfy the positive-
    grounds test, regardless of what the rest of this function would
    otherwise conclude - it is not a real, resolvable coordinate at all,
    a fabricated string this producer has no evidence describes any
    actual in-scan or third-party artifact. Checked FIRST,
    unconditionally: ``unresolved``, spelling retained verbatim (never
    silently rewritten or guessed at), even ahead of the duplicate-name
    check (a qualified name containing ``${`` can never legitimately
    collide with a real one either). ``parse_maven_pom`` (java.py)
    already expands the two self-referential properties resolvable from
    the SAME file before constructing the edge; any OTHER property
    reaches this rule.

    FIX ROUND 20 (sixteenth cold read, M1+M2 MAJOR, wrong-data - THE
    POISON RULE): the excluded-region CONTAINMENT question used to be
    answered by string-matching a target's own qualified name against
    an excluded root's own path (``_excluded_region_match``, now
    retired) - inert for the mainstream Maven layout, where discovery
    records an excluded root as its bare DIRECTORY NAME (``vendor``)
    while the unwalked source it swallows lives arbitrarily deeper
    (``vendor/<module>/src/main/java/<pkg>/...``), a relationship no
    string comparison over the qualified name alone can ever recover -
    a vendored, in-reactor module inside an excluded tree published as
    a confident third-party EXTERNAL claim, the exact over-claim round
    18's own F3 named for a different mechanism. Replaced with evidence
    the run already collects instead of trying to string-match the
    unknowable: ``externality_poisoned`` is True when discovery's own
    peek (run for EVERY generated/vendor exclusion, not just ones under
    a recognized source root - see ``discovery.DiscoveryResult.
    excluded_region_may_contain_target``) found - or could not rule
    out, on truncation - an adapter-handled/tier-2 code-bearing file
    inside ANY excluded region this run swallowed, OR the reactor rule
    (scan_pipeline.py) found a pom's own declared ``<module>`` resolving
    into one. Deliberately BLUNT and run-wide, not per-target: a miss
    may publish EXTERNAL only when EVERY excluded root this run recorded
    was verified code-free - true for an ordinary repo's own target/
    full of compiled output, false the moment ANY excluded region is
    even suspected of holding first-party source. ``_degraded_java_
    suffix_match`` above is UNCHANGED (kept - it answers a different,
    EXACT question: a specific file this run tried and failed to read,
    never a containment guess).

    FIX ROUND 20b (seventeenth-round dispatch, THE ASK - taken): a
    poisoned run still resolves a target under one of ``java.*``/
    ``javax.*``/``jakarta.*`` as EXTERNAL - ``java.*`` is RUNTIME-ENFORCED
    (the classloader itself rejects a non-bootstrap declaration there);
    ``javax.*``/``jakarta.*`` are convention-reserved only, not enforced
    (``javax.servlet`` is itself ordinary, vendorable third-party code) -
    see ``_is_platform_reserved_namespace``'s own docstring for the named
    residual this weaker pair still carries (first-party code genuinely
    placed under one of them, inside an excluded region, on a poisoned
    run, publishes external - three conditions, a mild consequence, kept
    anyway rather than re-noising every legacy poisoned run's own
    javax.* imports). Checked LAST, after every OTHER positive-grounds
    exclusion above still applies unchanged.

    FIX ROUND 20c (readiness carry, inherited from round 20): the
    third return element - ``externality_suppressed`` - is True ONLY
    when THIS specific miss was classified unresolved BY THE POISON
    RULE, never for the ``${...}``/duplicate-name/degraded-suffix
    branches above (each answers a genuinely different question - a
    real problem this run found, not an abstention). Callers thread
    this onto the published ``DependencyRecord`` so the readiness layer
    can honestly distinguish "abstained from a positive claim because
    this run's external surface is unknown" from "found a real,
    unresolved dependency" - the two used to collapse into the
    identical ``unsatisfied``/``unresolved_dependency`` signal, a
    blocker-severity found-a-problem claim over a producer that
    actually never looked."""
    if "${" in qualified_name:
        return "unresolved", None, False
    if qualified_name in duplicate_qualified_names:
        return "ambiguous", sorted(unit_ids_by_qualified_name.get(qualified_name, [])), False
    if _degraded_java_suffix_match(qualified_name, degraded_paths):
        return "unresolved", None, False
    if externality_poisoned and not _is_platform_reserved_namespace(qualified_name):
        return "unresolved", None, True
    return "external", None, False


def build_dependencies(
    java_results: dict[str, java_adapter.JavaFileResult],
    *, file_digests: dict[str, str] | None = None,
    degraded_paths: frozenset[str] = frozenset(),
    externality_poisoned: bool = False,
) -> list[DependencyRecord]:
    """``java_results`` carries every producer's claims uniformly, keyed
    by relative path - including a ``pom.xml``'s ``build`` edges (B-3,
    reviewer-3 PR-B delta review round 1: routed through the sanitized
    worker's ``process_paths`` on the same already-read bytes, the same
    way every other adapter claim is). A pom.xml's ``JavaFileResult`` has
    empty ``units`` and just its ``edges`` populated - these are always
    ``target_kind: external`` and never need the cross-file registry
    below, but fall out of the same loop naturally since nothing here
    depends on the producing file being a ``.java`` source.

    (dead-parameter removal, reviewer-3 PR-B delta review round 2: this
    function previously took a second, separate ``build_edges_by_path``
    parameter for exactly this pom.xml case, from when scan_pipeline.py
    read pom.xml directly in the parent process; once B-3 routed it
    through the worker instead, that parameter had no production caller
    left.)

    ``file_digests`` maps a relative path to discovery's own content
    digest for that file (M7, cold-read PR-B fix round 3: ``source_digest``
    was set to ``None`` once per file and never actually assigned from it -
    easy to miss since modules_artifact.py populates its OWN producers'
    source_digest correctly, so only this module's own producers stayed
    silently unpopulated).

    ``degraded_paths`` (FIX ROUND 12, F2 MAJOR) names every relative path
    this SAME run recorded a worker-level problem for (an adapter
    resource cap, a read/parse failure - never merely "not imported by
    anything") - queryable here so an import naming one of those files'
    declared types resolves ``unresolved``, never a false-positive
    ``resolved``/``target_external``.

    ``externality_poisoned`` (FIX ROUND 20, sixteenth cold read, M1+M2
    MAJOR - THE POISON RULE, superseding round 16's own B2 BLOCKER
    string-matching approach) is True when this run cannot vouch that
    EVERY excluded region it swallowed was genuinely code-free (see
    ``_classify_registry_miss``'s own docstring) - queryable here for
    the identical reason ``degraded_paths`` is: a registry miss must
    never be stamped a confident external claim while any excluded
    region might hold the very first-party source it is missing.
    """
    file_digests = file_digests or {}
    (
        by_qualified_name, by_simple_name, file_unit_id_by_path, duplicate_qualified_names,
        unit_ids_by_qualified_name, in_scan_packages,
    ) = _build_registry(java_results)
    records: list[DependencyRecord] = []

    for path, result in java_results.items():
        source_digest = file_digests.get(path)
        local_qualified_by_simple = {u.simple_name: u.qualified_name for u in result.units}
        # F1 BLOCKER (eighth cold read): the file's OWN import evidence -
        # a plain, non-static, non-wildcard import binds a bare simple
        # name to exactly one fully-qualified spelling, in THIS file only.
        import_qualified_by_simple = {
            e.target.rsplit(".", 1)[-1]: e.target for e in result.edges
            if e.relation == "import" and e.target_kind == "internal_exact_or_external"
        }
        for edge in result.edges:
            if edge.relation not in CLOSED_RELATIONS:
                raise UnsupportedRelationClaimed(
                    f"{java_adapter.ADAPTER_NAME} claimed unsupported relation "
                    f"{edge.relation!r} for {path}")
            from_unit_id = (
                by_qualified_name.get(edge.from_qualified_name)
                or file_unit_id_by_path[path]
            )
            package = (
                edge.from_qualified_name.rsplit(".", 1)[0]
                if "." in edge.from_qualified_name else None
            )
            record = _edge_claim_to_record(
                edge, from_unit_id=from_unit_id, source_digest=source_digest,
                by_qualified_name=by_qualified_name, by_simple_name=by_simple_name,
                duplicate_qualified_names=duplicate_qualified_names,
                local_qualified_by_simple=local_qualified_by_simple,
                import_qualified_by_simple=import_qualified_by_simple,
                package=package, degraded_paths=degraded_paths,
                unit_ids_by_qualified_name=unit_ids_by_qualified_name,
                externality_poisoned=externality_poisoned, in_scan_packages=in_scan_packages,
            )
            records.append(record)

    return _coalesce_by_edge_id(records)


def _coalesce_by_edge_id(records: list[DependencyRecord]) -> list[DependencyRecord]:
    """M6 (cold-read, PR-B fix round 3): "byte-identical claims must
    coalesce to one record per edge_id with merged producer lists" (design
    merge rule) - e.g. three identical calls to the same method at the
    same call site pattern (``Foo.bar(); Foo.bar(); Foo.bar();``) all
    produce the SAME ``edge_id`` (from_unit_id + relation + target +
    phase - the adapter records no per-call-site distinguishing evidence
    this slice), so without this step they inflated record_counts,
    ceilings, and every fan-in/fan-out count with pure duplicates.
    Preserves first-seen order (a plain dict is insertion-ordered) so
    coalescing never perturbs an otherwise-deterministic record order."""
    merged: dict[str, DependencyRecord] = {}
    for record in records:
        existing = merged.get(record.edge_id)
        if existing is None:
            merged[record.edge_id] = record
            continue
        combined_producers = list(existing.producers)
        for producer in record.producers:
            if producer not in combined_producers:
                combined_producers.append(producer)
        merged[record.edge_id] = replace(existing, producers=combined_producers)
    return list(merged.values())


#: FIX ROUND 14 (tenth cold read, CR10-4 MAJOR + judgment call): round
#: 12's F5 scoped ``invoke`` noise out of ``dependencies_resolved``, but
#: ``inherit`` has the IDENTICAL property - java.lang needs no import,
#: so every custom exception (``extends RuntimeException``) and every
#: ``Runnable``/``Comparable``/``AutoCloseable`` implementor published a
#: confident ``unresolved_dependency`` on entirely healthy, ordinary
#: code, since the resolution ladder has no local declaration, no
#: import, and no same-package sibling to find. Judged (per the
#: reviewer's own invitation): recognize a CLOSED, well-known set of
#: java.lang simple names as KNOWN-EXTERNAL for BOTH relations (they
#: share one resolution ladder since CR10-2) rather than merely scoping
#: the readiness check further - this also fixes invoke's own dependency_
#: summary noise (``Math``/``String``/... publishing permanently
#: unresolved rather than the genuinely external JDK reference they are),
#: not just inherit's readiness signal. PROVISIONAL, like every other
#: bound/closed-set constant in this package - the common, everyday
#: java.lang surface, not an exhaustive enumeration of the whole package
#: (an obscure or newly-added java.lang type absent from this set simply
#: stays unresolved, the same safe under-claim as today - never wrong,
#: just not yet recognized).
_JAVA_LANG_SIMPLE_NAMES = frozenset({
    "Object", "String", "StringBuilder", "StringBuffer", "CharSequence",
    "Boolean", "Byte", "Short", "Integer", "Long", "Float", "Double", "Number",
    "Character", "Void", "Math", "StrictMath", "System", "Runtime", "Process",
    "ProcessBuilder", "Thread", "ThreadGroup", "ThreadLocal", "Runnable",
    "Comparable", "Iterable", "AutoCloseable", "Cloneable", "Class",
    "ClassLoader", "Enum", "Record", "Package", "Module", "SecurityManager",
    "Throwable", "Exception", "RuntimeException", "Error",
    "IllegalArgumentException", "IllegalStateException", "NullPointerException",
    "IndexOutOfBoundsException", "ArrayIndexOutOfBoundsException",
    "StringIndexOutOfBoundsException", "ClassCastException",
    "NumberFormatException", "UnsupportedOperationException",
    "ArithmeticException", "NegativeArraySizeException", "ArrayStoreException",
    "CloneNotSupportedException", "InterruptedException", "OutOfMemoryError",
    "StackOverflowError", "AssertionError", "NoSuchFieldException",
    "NoSuchMethodException", "SecurityException",
})


def _java_lang_known_external(target_unresolved: str | None) -> str | None:
    """Returns the canonical ``java.lang.NAME`` spelling when
    ``target_unresolved`` (bare, e.g. ``"RuntimeException"``, or already
    spelled ``"java.lang.RuntimeException"``) names one of
    ``_JAVA_LANG_SIMPLE_NAMES`` - ``None`` otherwise (including for any
    OTHER dotted spelling, e.g. an unrelated package's own same-named
    class, which this never touches). Callers apply this ONLY after the
    resolution ladder already reported ``unresolved`` - a local
    declaration, an import, or a same-package sibling sharing a
    java.lang name always wins first, exactly Java's own shadowing rule;
    ``ambiguous`` (a real in-scan naming collision) is never overridden
    either, since that is more informative than a java.lang guess."""
    if target_unresolved is None:
        return None
    simple = target_unresolved.rsplit(".", 1)[-1]
    if target_unresolved not in (simple, f"java.lang.{simple}"):
        return None
    if simple in _JAVA_LANG_SIMPLE_NAMES:
        return f"java.lang.{simple}"
    return None


def _edge_claim_to_record(
    edge: java_adapter.JavaEdgeClaim, *, from_unit_id: str, source_digest: str | None,
    by_qualified_name: dict[str, str], by_simple_name: dict[str, list[str]],
    duplicate_qualified_names: set[str], local_qualified_by_simple: dict[str, str],
    import_qualified_by_simple: dict[str, str], package: str | None,
    degraded_paths: frozenset[str], unit_ids_by_qualified_name: dict[str, list[str]],
    externality_poisoned: bool = False,
    in_scan_packages: set[str] = frozenset(),
) -> DependencyRecord:
    target_unit_id = target_external = target_unresolved = confidence = None
    candidate_unit_ids: list[str] = []
    # FIX ROUND 20c (readiness carry): set True only by the poison
    # branch of _classify_registry_miss (or its inline wildcard-import
    # twin below) - never by the `${...}`/duplicate-name/degraded-
    # suffix branches, which each report a genuine, different problem.
    externality_suppressed = False
    if edge.target_kind == "internal_candidate":
        # FIX ROUND 14 (CR10-3): chain-aware - a dotted target may be a
        # member-navigation chain (an enum constant, a nested type
        # reached through an unqualified head), never assumed to be a
        # type reference verbatim just because it contains dots.
        resolution_state, target_unit_id, target_unresolved, confidence, candidates = (
            _resolve_internal_candidate_chain(
                edge.target, by_qualified_name, by_simple_name, duplicate_qualified_names,
                local_qualified_by_simple=local_qualified_by_simple,
                import_qualified_by_simple=import_qualified_by_simple, package=package,
                unit_ids_by_qualified_name=unit_ids_by_qualified_name,
            )
        )
        if candidates is not None:
            candidate_unit_ids = candidates
        if resolution_state == "unresolved":
            # FIX ROUND 14 (CR10-4): the ladder has no local/import/
            # same-package rung that could ever find a java.lang type -
            # recognized as the LAST resort, never ahead of real
            # evidence (a shadowing local/imported/same-package
            # declaration already won above, or this line never runs).
            known_external = _java_lang_known_external(target_unresolved)
            if known_external is not None:
                resolution_state, target_unresolved = "resolved", None
                target_external = known_external
        if (
            resolution_state == "unresolved"
            and edge.relation in ("inherit", "invoke")
            and edge.target in import_qualified_by_simple
        ):
            # FIX ROUND 15 (eleventh cold read, M8 MAJOR, wrong-data,
            # promoted from polish - same class as CR10-4): a target
            # fully qualified THROUGH THIS FILE'S OWN IMPORT (e.g.
            # `extends HttpServlet` with `import javax.servlet.http.
            # HttpServlet;`) stayed unresolved here even though the
            # IMPORT EDGE for the identical qualified name independently
            # resolves target_external (`internal_exact_or_external`
            # below) - two contradictory facts about the same
            # dependency in one run, and a confident deficiency
            # (dependencies_resolved unsatisfied) on every servlet
            # subclass, healthy code. The ladder's own import-miss
            # branch already computed this exact qualified spelling as
            # `target_unresolved` (guarded by the equality check, so a
            # coincidental same-spelling miss from a DIFFERENT ladder
            # rung - e.g. a dotted target - is never mistaken for this
            # one) - consult it the same way the import edge itself
            # does. Scoped to inherit/invoke ONLY - a "test" relation
            # edge is a CONVENTION GUESS (F4), never a real reference the
            # source actually makes; confidently resolving a guessed
            # name external would be exactly the overclaim F4 exists to
            # prevent.
            #
            # FIX ROUND 16b (reviewer-3's rejection of round 16, BLOCKER
            # 2 - "the predicate bypass"): this used to call
            # `_degraded_java_suffix_match` INLINE - the exact same
            # narrower check the round-16 dispatch's own headline
            # mechanism (_classify_registry_miss) replaced everywhere
            # else, missing the duplicate-FQN and excluded-region
            # checks this bypassed entirely. Reproduced: two in-scan
            # `p.Base` + `import p.Base;` + `extends Base` published the
            # IMPORT edge ambiguous (2 candidates) while the INHERIT
            # edge for the identical qualified name published resolved/
            # external - two contradictory facts about one dependency in
            # the same run, this round's own class reopened on the one
            # inline caller `_classify_registry_miss` was meant to
            # replace. Now calls the shared predicate and honors EVERY
            # outcome, not just the degraded one.
            imported = import_qualified_by_simple[edge.target]
            if imported == target_unresolved:
                outcome, candidates, poisoned_miss = _classify_registry_miss(
                    imported, duplicate_qualified_names=duplicate_qualified_names,
                    unit_ids_by_qualified_name=unit_ids_by_qualified_name,
                    degraded_paths=degraded_paths, externality_poisoned=externality_poisoned,
                )
                externality_suppressed = poisoned_miss
                if outcome == "ambiguous":
                    resolution_state, candidate_unit_ids = "ambiguous", candidates
                elif outcome == "external":
                    resolution_state, target_unresolved = "resolved", None
                    target_external = imported
                # outcome == "unresolved": already unresolved with
                # target_unresolved == imported - nothing to change.
    elif edge.target_kind == "internal_exact_or_external":
        # D-1 (reviewer-3, PR-B delta review round 2): an import's target
        # is already fully qualified - an EXACT registry hit means it
        # names an in-scan type and resolves internally exactly like the
        # identical name would via `extends`; anything else is a genuinely
        # external dependency, never a simple-name guess.
        exact_unit_id = _exact_qualified_lookup(edge.target, by_qualified_name)
        if exact_unit_id is not None:
            resolution_state, target_unit_id, confidence = "resolved", exact_unit_id, "high"
        else:
            outcome, candidates, poisoned_miss = _classify_registry_miss(
                edge.target, duplicate_qualified_names=duplicate_qualified_names,
                unit_ids_by_qualified_name=unit_ids_by_qualified_name,
                degraded_paths=degraded_paths, externality_poisoned=externality_poisoned,
            )
            externality_suppressed = poisoned_miss
            if outcome == "ambiguous":
                resolution_state, target_unresolved, candidate_unit_ids = (
                    "ambiguous", edge.target, candidates)
            elif outcome == "unresolved":
                resolution_state, target_unresolved = "unresolved", edge.target
            else:
                resolution_state, target_external = "resolved", edge.target
    elif edge.target_kind == "internal_static_import_exact_or_external":
        # N5 (fourth cold read, fix round 6): a static import's target is
        # a member path (Type.MEMBER) or a static-member wildcard
        # (Type.*) - the TYPE PREFIX (everything but the last segment) is
        # what might be in-scan, exact-matched the same way D-1 already
        # does for a plain import; the member itself is never resolved
        # (out of scope). The UNRESOLVED/ambiguous path keeps the full
        # original spelling, for evidence (the reader can see exactly
        # which member was meant, even though nothing in-scan answers
        # for the type) - only the LOOKUP key strips the trailing
        # member/wildcard segment.
        type_prefix = edge.target[:-2] if edge.target.endswith(".*") else edge.target.rsplit(".", 1)[0]
        exact_unit_id = _exact_qualified_lookup(type_prefix, by_qualified_name)
        if exact_unit_id is not None:
            resolution_state, target_unit_id, confidence = "resolved", exact_unit_id, "high"
        else:
            outcome, candidates, poisoned_miss = _classify_registry_miss(
                type_prefix, duplicate_qualified_names=duplicate_qualified_names,
                unit_ids_by_qualified_name=unit_ids_by_qualified_name,
                degraded_paths=degraded_paths, externality_poisoned=externality_poisoned,
            )
            externality_suppressed = poisoned_miss
            if outcome == "ambiguous":
                resolution_state, target_unresolved, candidate_unit_ids = (
                    "ambiguous", edge.target, candidates)
            elif outcome == "unresolved":
                resolution_state, target_unresolved = "unresolved", edge.target
            else:
                # FIX ROUND 15 (eleventh cold read, N1 MINOR): a
                # genuinely EXTERNAL static import used to publish the
                # full member path (e.g. "org.junit.Assert.
                # assertEquals") as target_external - the member was
                # already stripped for the RESOLUTION lookup key above;
                # the published external name now matches it, naming
                # the TYPE the dependency actually is
                # (org.junit.Assert), never a member path masquerading
                # as one.
                resolution_state, target_external = "resolved", type_prefix
    elif edge.target_kind == "external_wildcard_import":
        # FIX ROUND 16 (twelfth cold read, B3 BLOCKER, wrong-data): a
        # plain wildcard import (`import com.acme.util.*;`) names a
        # PACKAGE, not a type - it cannot be exact-matched against the
        # unit registry the way a plain/static import's own type
        # spelling can (the named limit round 12b documented). But a
        # wildcard whose OWN package prefix matches a package this run
        # actually scanned is not "genuinely external" either - it is
        # importing THIS repo's own package, wholesale, and the
        # publisher has no way to know which specific in-scan member(s)
        # it actually uses (the documented bare-name-via-wildcard
        # under-claim limit - resolving that would require checking
        # every wildcard-imported package's real membership, reopening
        # the same cross-package ambiguity round 12's F1 closed).
        # Published unresolved/wildcard-scoped instead - a named,
        # honest "cannot tell, but not third-party either" rather than
        # a confident external claim that undercounts this package's
        # own fan-in.
        #
        # FIX ROUND 16c (reviewer-3's approval-conditioned minor on
        # round 16b - "the LAST door"): this branch consulted ONLY
        # in_scan_packages, never the excluded-region check every OTHER
        # registry-miss caller already goes through - `import target.
        # gen.*` with `target/` excluded published resolved/external
        # while `import target.Stub` in the SAME file (the non-wildcard
        # twin) correctly published unresolved: two different answers
        # about one excluded tree, in the same run. Checked here too.
        #
        # FIX ROUND 20 (sixteenth cold read, M1+M2 MAJOR - THE POISON
        # RULE): the excluded-region check itself is now the SAME
        # run-wide poison flag `_classify_registry_miss` uses, never a
        # per-target string match against an excluded root's own path -
        # see that function's own docstring for why the string-matching
        # approach was retired.
        package_prefix = edge.target[:-2]
        if package_prefix in in_scan_packages:
            resolution_state, target_unresolved = "unresolved", edge.target
        elif externality_poisoned and not _is_platform_reserved_namespace(package_prefix):
            # FIX ROUND 20b (THE ASK - taken): same reserved-namespace
            # exemption as _classify_registry_miss - a wildcard import of
            # java.*/javax.*/jakarta.* still resolves external under
            # poison.
            resolution_state, target_unresolved = "unresolved", edge.target
            # FIX ROUND 20c (readiness carry): unlike the in_scan_packages
            # branch above (a genuinely different, non-poison reason this
            # stays unresolved), this branch's own unresolved outcome IS
            # the poison rule.
            externality_suppressed = True
        else:
            resolution_state, target_external = "resolved", edge.target
    elif edge.target_kind == "internal_pom_coordinate_or_external":
        # FIX ROUND 17 (thirteenth cold read, CR13-4 MAJOR, wrong-data):
        # a pom <dependency>'s groupId:artifactId used to publish
        # resolved/external UNCONDITIONALLY - a multi-module reactor's
        # module-to-module dependency (the single most migration-
        # relevant internal edge a pom can declare) published external
        # even when the SIBLING pom declaring that exact coordinate sits
        # in the same scan, because nothing ever registered a pom's own
        # coordinate as a resolvable unit. Exact-matched against the
        # SAME registry every other producer's units already build
        # through (parse_maven_pom now publishes a pom's own coordinate
        # as a real unit claim) - a miss falls through to the SAME
        # shared predicate every other import-shaped edge in this file
        # already uses, consistent with B1 (a duplicate pom coordinate
        # is ambiguous+candidates, never a silent pick).
        exact_unit_id = _exact_qualified_lookup(edge.target, by_qualified_name)
        if exact_unit_id is not None:
            resolution_state, target_unit_id, confidence = "resolved", exact_unit_id, "high"
        else:
            outcome, candidates, poisoned_miss = _classify_registry_miss(
                edge.target, duplicate_qualified_names=duplicate_qualified_names,
                unit_ids_by_qualified_name=unit_ids_by_qualified_name,
                degraded_paths=degraded_paths, externality_poisoned=externality_poisoned,
            )
            externality_suppressed = poisoned_miss
            if outcome == "ambiguous":
                resolution_state, target_unresolved, candidate_unit_ids = (
                    "ambiguous", edge.target, candidates)
            elif outcome == "unresolved":
                resolution_state, target_unresolved = "unresolved", edge.target
            else:
                resolution_state, target_external = "resolved", edge.target
    else:
        resolution_state = "resolved"
        target_external = edge.target

    # FIX ROUND 29 (F4 MAJOR, completeness): the adapter's own emission-
    # site knowledge (target_kind), never re-derived - see DependencyRecord.
    # route_kind's own docstring.
    route_kind = {
        "external_route": "http_route", "external_filter": "http_filter",
    }.get(edge.target_kind)

    return DependencyRecord(
        edge_id=digests.edge_id(
            from_unit_id=from_unit_id, relation=edge.relation, target=edge.target,
            phase=edge.phase,
        ),
        from_unit_id=from_unit_id,
        relation=edge.relation,
        phase=edge.phase,
        # M3 (fourth cold read, fix round 6): hardcoded False regardless
        # of what the adapter actually claimed - a Maven <dependency>'s
        # own <optional>true</optional> was read past and discarded,
        # publishing every pom edge as a positive "not optional" fact.
        optional=edge.optional,
        evidence_class=edge.evidence_class,
        resolution_state=resolution_state,
        target_unit_id=target_unit_id,
        target_external=target_external,
        target_unresolved=target_unresolved,
        confidence=confidence,
        producers=[_producer(
            name=java_adapter.ADAPTER_NAME, version=java_adapter.ADAPTER_VERSION,
            rule_version=java_adapter.RULE_VERSION, source_digest=source_digest,
            basis=edge.evidence_class,
        )],
        candidate_unit_ids=candidate_unit_ids,
        externality_suppressed=externality_suppressed,
        route_kind=route_kind,
    )
