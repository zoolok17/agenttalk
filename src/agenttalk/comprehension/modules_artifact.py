"""``modules.json`` record assembly (DESIGN-55-comprehension-plane.md,
Artifact 1: module inventory).

This module assembles in-memory records only - the JSON envelope, ceiling
enforcement, and publish/staging wiring are a later step (PR-B item 9),
reusing PR-A's existing envelope/ceilings/publish machinery unchanged.

Scope simplification for this slice (Java-only adapter, flagged for
review, not a blocking fork): no package/module/service-level container
unit is synthesized above a file's own declared types - the design
permits an adapter to identify "a package, module, component, or service"
additionally containing file units, but the bundled Java adapter (item 3)
only identifies per-type ``component`` units this slice. A future adapter
or a Java package-grouping producer can add that container tier without
changing this module's shape.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from . import digests
from .adapters import java as java_adapter
from .discovery import DiscoveryResult

MODULES_ARTIFACT_TYPE = "agenttalk.comprehension.modules"
MODULES_SCHEMA_VERSION = 1

_LANGUAGE_BY_EXTENSION = {".java": "java"}
_TEST_PATH_SEGMENT = re.compile(r"(?:^|/)(?:src/test|test)/")


@dataclass(frozen=True)
class ModuleRecord:
    unit_id: str
    kind: str
    display_name: str
    language: str
    paths: list[str]
    source_digests: dict[str, str]
    classification: list[str]
    container_unit_id: str | None
    producers: list[dict[str, Any]]
    conflict_id: str | None = None
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "kind": self.kind,
            "display_name": self.display_name,
            "language": self.language,
            "paths": sorted(self.paths),
            "source_digests": dict(sorted(self.source_digests.items())),
            "classification": sorted(self.classification),
            "container_unit_id": self.container_unit_id,
            "producers": self.producers,
            "conflict_id": self.conflict_id,
            "evidence": self.evidence,
        }


def module_record_from_json(payload: dict[str, Any]) -> ModuleRecord:
    """The inverse of :meth:`ModuleRecord.to_json` - reconstructs a record
    from a persisted ``modules.json`` row, e.g. for ``report``/``status``
    to feed back into :func:`projector.project_comprehension`."""
    return ModuleRecord(
        unit_id=payload["unit_id"], kind=payload["kind"], display_name=payload["display_name"],
        language=payload["language"], paths=list(payload["paths"]),
        source_digests=dict(payload["source_digests"]),
        classification=list(payload["classification"]),
        container_unit_id=payload["container_unit_id"], producers=list(payload["producers"]),
        conflict_id=payload.get("conflict_id"), evidence=list(payload.get("evidence", [])),
    )


def _language_for_path(relative_path: str) -> str:
    for ext, lang in _LANGUAGE_BY_EXTENSION.items():
        if relative_path.endswith(ext):
            return lang
    return "unknown"


def _default_classification(relative_path: str) -> str:
    if _TEST_PATH_SEGMENT.search(relative_path.replace("\\", "/")):
        return "test"
    return "production"


def _producer(
    *, name: str, version: int, source_digest: str | None,
    basis: str, rule_version: int | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "producer": name, "producer_version": version, "basis": basis,
        "source_digest": source_digest,
    }
    if rule_version is not None:
        entry["rule_version"] = rule_version
    return entry


def _display_name(qualified_name: str) -> str:
    return qualified_name.rsplit(".", 1)[-1]


def _parent_qualified_name(qualified_name: str, known_names: set[str]) -> str | None:
    if "." not in qualified_name:
        return None
    candidate = qualified_name.rsplit(".", 1)[0]
    return candidate if candidate in known_names else None


def build_modules(
    discovery: DiscoveryResult, java_results: dict[str, java_adapter.JavaFileResult],
) -> list[ModuleRecord]:
    """``java_results`` maps a ``.java`` file's relative path to its
    already-parsed :class:`~.adapters.java.JavaFileResult` (item 3) -
    parsing happens once, upstream; this function only assembles records
    from what was already extracted."""
    records: list[ModuleRecord] = []

    for file_entry in discovery.files:
        relative_path = file_entry.relative_path
        java_result = java_results.get(relative_path)

        if java_result is None or not java_result.units:
            records.append(ModuleRecord(
                unit_id=digests.unit_id(kind="file", paths=[relative_path], qualified_name=None),
                kind="file",
                display_name=relative_path.rsplit("/", 1)[-1],
                language=_language_for_path(relative_path),
                paths=[relative_path],
                source_digests={relative_path: file_entry.content_digest},
                classification=[_default_classification(relative_path)],
                container_unit_id=None,
                producers=[_producer(
                    name="discovery", version=1, source_digest=file_entry.content_digest,
                    basis="extracted",
                )],
            ))
            continue

        qualified_names_in_file = {u.qualified_name for u in java_result.units}
        unit_id_by_qualified_name: dict[str, str] = {}
        for unit_claim in java_result.units:
            unit_id_by_qualified_name[unit_claim.qualified_name] = digests.unit_id(
                kind="component", paths=[relative_path], qualified_name=unit_claim.qualified_name,
            )

        for unit_claim in java_result.units:
            parent_name = _parent_qualified_name(unit_claim.qualified_name, qualified_names_in_file)
            container_unit_id = (
                unit_id_by_qualified_name[parent_name] if parent_name is not None else None
            )
            records.append(ModuleRecord(
                unit_id=unit_id_by_qualified_name[unit_claim.qualified_name],
                kind="component",
                display_name=_display_name(unit_claim.qualified_name),
                language="java",
                paths=[relative_path],
                source_digests={relative_path: file_entry.content_digest},
                classification=[unit_claim.classification],
                container_unit_id=container_unit_id,
                producers=[_producer(
                    name=java_adapter.ADAPTER_NAME, version=java_adapter.ADAPTER_VERSION,
                    rule_version=java_adapter.RULE_VERSION,
                    source_digest=file_entry.content_digest, basis="extracted",
                )],
            ))

        primary_qualified_name = java_result.units[0].qualified_name
        top_level_container = unit_id_by_qualified_name.get(primary_qualified_name)
        records.append(ModuleRecord(
            unit_id=digests.unit_id(kind="file", paths=[relative_path], qualified_name=None),
            kind="file",
            display_name=relative_path.rsplit("/", 1)[-1],
            language="java",
            paths=[relative_path],
            source_digests={relative_path: file_entry.content_digest},
            classification=[java_result.units[0].classification],
            container_unit_id=top_level_container,
            producers=[_producer(
                name="discovery", version=1, source_digest=file_entry.content_digest,
                basis="extracted",
            )],
        ))

    return records
