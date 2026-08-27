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

from dataclasses import dataclass, field
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
        }


def _java_component_unit_id(relative_path: str, qualified_name: str) -> str:
    return digests.unit_id(kind="component", paths=[relative_path], qualified_name=qualified_name)


def _java_file_unit_id(relative_path: str) -> str:
    return digests.unit_id(kind="file", paths=[relative_path], qualified_name=None)


def _build_registry(
    java_results: dict[str, java_adapter.JavaFileResult],
) -> tuple[dict[str, str], dict[str, list[str]], dict[str, str]]:
    by_qualified_name: dict[str, str] = {}
    by_simple_name: dict[str, list[str]] = {}
    file_unit_id_by_path: dict[str, str] = {}
    for path, result in java_results.items():
        file_unit_id_by_path[path] = _java_file_unit_id(path)
        for unit_claim in result.units:
            uid = _java_component_unit_id(path, unit_claim.qualified_name)
            by_qualified_name[unit_claim.qualified_name] = uid
            by_simple_name.setdefault(unit_claim.simple_name, []).append(uid)
    return by_qualified_name, by_simple_name, file_unit_id_by_path


def _resolve_internal_candidate(
    target: str, by_qualified_name: dict[str, str], by_simple_name: dict[str, list[str]],
) -> tuple[str, str | None, str | None, str | None]:
    """Returns ``(resolution_state, target_unit_id, target_unresolved,
    confidence)``. Never invents a match: an unqualified name matching
    more than one declared type across the scan is ``ambiguous``, not a
    guess at which one was meant (design: "The scanner never invents an
    internal target because names look similar. Ambiguous resolution
    creates an unresolved edge with candidates.")."""
    if target in by_qualified_name:
        return "resolved", by_qualified_name[target], None, "high"
    simple = target.rsplit(".", 1)[-1]
    candidates = by_simple_name.get(simple, [])
    if len(candidates) == 1:
        return "resolved", candidates[0], None, "medium"
    if len(candidates) > 1:
        return "ambiguous", None, target, None
    return "unresolved", None, target, None


def _producer(*, name: str, version: int, rule_version: int, source_digest: str | None) -> dict[str, Any]:
    return {
        "producer": name, "producer_version": version, "rule_version": rule_version,
        "basis": "extracted", "source_digest": source_digest,
    }


def build_dependencies(
    java_results: dict[str, java_adapter.JavaFileResult],
    build_edges_by_path: dict[str, list[java_adapter.JavaEdgeClaim]] | None = None,
) -> list[DependencyRecord]:
    """``build_edges_by_path`` carries edges from non-``.java`` producers
    (e.g. :func:`adapters.java.parse_maven_pom`'s ``pom.xml`` dependency
    edges) keyed by the FROM path (the pom.xml itself, not a Java
    unit) - these are always ``target_kind: external`` and never need the
    cross-file registry below.
    """
    by_qualified_name, by_simple_name, file_unit_id_by_path = _build_registry(java_results)
    records: list[DependencyRecord] = []

    for path, result in java_results.items():
        source_digest = None
        for edge in result.edges:
            if edge.relation not in CLOSED_RELATIONS:
                raise UnsupportedRelationClaimed(
                    f"{java_adapter.ADAPTER_NAME} claimed unsupported relation "
                    f"{edge.relation!r} for {path}")
            from_unit_id = (
                by_qualified_name.get(edge.from_qualified_name)
                or file_unit_id_by_path[path]
            )
            record = _edge_claim_to_record(
                edge, from_unit_id=from_unit_id, source_digest=source_digest,
                by_qualified_name=by_qualified_name, by_simple_name=by_simple_name,
            )
            records.append(record)

    for path, edges in (build_edges_by_path or {}).items():
        from_unit_id = digests.unit_id(kind="file", paths=[path], qualified_name=None)
        for edge in edges:
            if edge.relation not in CLOSED_RELATIONS:
                raise UnsupportedRelationClaimed(
                    f"{java_adapter.ADAPTER_NAME} claimed unsupported relation "
                    f"{edge.relation!r} for {path}")
            records.append(_edge_claim_to_record(
                edge, from_unit_id=from_unit_id, source_digest=None,
                by_qualified_name=by_qualified_name, by_simple_name=by_simple_name,
            ))

    return records


def _edge_claim_to_record(
    edge: java_adapter.JavaEdgeClaim, *, from_unit_id: str, source_digest: str | None,
    by_qualified_name: dict[str, str], by_simple_name: dict[str, list[str]],
) -> DependencyRecord:
    target_unit_id = target_external = target_unresolved = confidence = None
    if edge.target_kind == "internal_candidate":
        resolution_state, target_unit_id, target_unresolved, confidence = (
            _resolve_internal_candidate(edge.target, by_qualified_name, by_simple_name)
        )
    else:
        resolution_state = "resolved"
        target_external = edge.target

    return DependencyRecord(
        edge_id=digests.edge_id(
            from_unit_id=from_unit_id, relation=edge.relation, target=edge.target,
            phase=edge.phase,
        ),
        from_unit_id=from_unit_id,
        relation=edge.relation,
        phase=edge.phase,
        optional=False,
        evidence_class=edge.evidence_class,
        resolution_state=resolution_state,
        target_unit_id=target_unit_id,
        target_external=target_external,
        target_unresolved=target_unresolved,
        confidence=confidence,
        producers=[_producer(
            name=java_adapter.ADAPTER_NAME, version=java_adapter.ADAPTER_VERSION,
            rule_version=java_adapter.RULE_VERSION, source_digest=source_digest,
        )],
    )
