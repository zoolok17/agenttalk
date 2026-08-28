"""The ONE pure projection function (DESIGN-55-comprehension-plane.md,
"Contract for the migration-program UI (#208)": "A server-side
comprehension projector validates one run and emits a bounded, versioned
`GET /api/comprehension` response. The same projector backs `comprehension
report --json`, preventing CLI/UI semantic drift.")

``project_comprehension`` is the single-projector-parity anchor named in
the approved PR-B plan (C-2): PR-D's future GET-only route imports and
calls THIS function with query-derived filters, exactly as item 9's
``report --json`` CLI command will with argv-derived filters. Nothing
about this module is CLI-specific or web-specific - it is a pure function
from already-validated run records to one bounded JSON-shaped dict.

Freshness is NOT implemented this slice (PR-C's job - "whole-scope
current/stale/unknown" needs the freshness pass this producer slice does
not build). The projection's ``freshness`` field is present but always
reports ``not_evaluated`` with a named reason code, never a silently
invented ``current``/``stale`` guess - core invariant 5 applies here too.
"""

from __future__ import annotations

from typing import Any

from .dependencies_artifact import DependencyRecord
from .features_artifact import EntryPointRecord, FeatureRecord
from .modules_artifact import ModuleRecord
from .readiness_artifact import ReadinessSignal, UnitReadinessSummary

PROJECTION_SCHEMA_VERSION = 1
_MAX_ROWS_PER_SECTION = 1000


def _bounded(records: list[Any], cap: int | None = None) -> tuple[list[Any], int]:
    # cap is read from the module global at CALL time (never a def-time
    # default) so a test can monkeypatch _MAX_ROWS_PER_SECTION and see it
    # take effect.
    effective_cap = _MAX_ROWS_PER_SECTION if cap is None else cap
    if len(records) <= effective_cap:
        return records, 0
    return records[:effective_cap], len(records) - effective_cap


def _dependency_summary(dependencies: list[DependencyRecord]) -> dict[str, int]:
    summary = {"internal": 0, "external": 0, "unresolved": 0, "ambiguous": 0}
    for edge in dependencies:
        if edge.resolution_state == "resolved" and edge.target_unit_id is not None:
            summary["internal"] += 1
        elif edge.resolution_state == "resolved" and edge.target_external is not None:
            summary["external"] += 1
        elif edge.resolution_state == "ambiguous":
            summary["ambiguous"] += 1
        else:
            summary["unresolved"] += 1
    return summary


def _fan_counts(dependencies: list[DependencyRecord]) -> tuple[dict[str, int], dict[str, int]]:
    fan_out: dict[str, int] = {}
    fan_in: dict[str, int] = {}
    for edge in dependencies:
        fan_out[edge.from_unit_id] = fan_out.get(edge.from_unit_id, 0) + 1
        if edge.target_unit_id is not None:
            fan_in[edge.target_unit_id] = fan_in.get(edge.target_unit_id, 0) + 1
    return fan_out, fan_in


def project_comprehension(
    *,
    scan_id: str,
    generated_at: str,
    manifest_digest: str | None,
    status: str,
    modules: list[ModuleRecord],
    dependencies: list[DependencyRecord],
    entry_points: list[EntryPointRecord],
    features: list[FeatureRecord],
    readiness_signals: list[ReadinessSignal],
    readiness_summaries: list[UnitReadinessSummary],
    problems: list[dict[str, Any]] | None = None,
    unit_id: str | None = None,
    feature_id: str | None = None,
    readiness_state: str | None = None,
    dependencies_only: bool = False,
) -> dict[str, Any]:
    """Every filter parameter mirrors a CLI/API filter 1:1 (design:
    ``--unit ID``, ``--feature ID``, ``--readiness STATE``,
    ``--dependencies``) - this function does not know which caller is
    asking."""
    problems = problems or []

    filtered_modules = modules
    if unit_id is not None:
        filtered_modules = [m for m in filtered_modules if m.unit_id == unit_id]
    if feature_id is not None:
        feature_unit_ids = {
            u for f in features if f.feature_id == feature_id for u in f.unit_ids
        }
        filtered_modules = [m for m in filtered_modules if m.unit_id in feature_unit_ids]

    filtered_dependencies = dependencies
    if unit_id is not None:
        filtered_dependencies = [
            e for e in filtered_dependencies
            if e.from_unit_id == unit_id or e.target_unit_id == unit_id
        ]

    filtered_features = features
    if feature_id is not None:
        filtered_features = [f for f in filtered_features if f.feature_id == feature_id]

    filtered_entry_points = entry_points
    if feature_id is not None:
        filtered_entry_points = [
            e for e in filtered_entry_points if feature_id in e.feature_ids
        ]

    filtered_summaries = readiness_summaries
    filtered_signals = readiness_signals
    if readiness_state is not None:
        filtered_summaries = [
            s for s in filtered_summaries if s.stored_assessment_state == readiness_state
        ]
        allowed_unit_ids = {s.unit_id for s in filtered_summaries}
        filtered_signals = [s for s in filtered_signals if s.unit_id in allowed_unit_ids]
    if unit_id is not None:
        filtered_signals = [s for s in filtered_signals if s.unit_id == unit_id]
        filtered_summaries = [s for s in filtered_summaries if s.unit_id == unit_id]

    all_feature_unit_ids = {u for f in features for u in f.unit_ids}
    units_without_feature = [m.unit_id for m in modules if m.unit_id not in all_feature_unit_ids]
    unmapped_entry_points = [e.entry_point_id for e in entry_points if not e.feature_ids]

    fan_out, fan_in = _fan_counts(dependencies)
    high_fan_out = sorted(
        ({"unit_id": u, "count": c} for u, c in fan_out.items() if c > 5),
        key=lambda row: (-row["count"], row["unit_id"]),
    )
    high_fan_in = sorted(
        ({"unit_id": u, "count": c} for u, c in fan_in.items() if c > 5),
        key=lambda row: (-row["count"], row["unit_id"]),
    )

    units_rows, units_omitted = _bounded([m.to_json() for m in filtered_modules])
    dependency_rows, dependency_omitted = _bounded([e.to_json() for e in filtered_dependencies])
    problem_rows, problem_omitted = _bounded(problems)
    # M10 (cold-read, PR-B fix round 3): features/entry_points/readiness
    # signals+summaries had no row cap and no truncation/omitted count at
    # all - unbounded on a large repo (the reviewer measured readiness
    # alone as 6 signals per unit, ~60k rows on a 5k-file repo,
    # `truncated: false` regardless). Bounded the same way every other
    # section already is.
    feature_rows, feature_omitted = _bounded([f.to_json() for f in filtered_features])
    entry_point_rows, entry_point_omitted = _bounded(
        [e.to_json() for e in filtered_entry_points])
    readiness_signal_rows, readiness_signal_omitted = _bounded(
        [s.to_json() for s in filtered_signals])
    readiness_summary_rows, readiness_summary_omitted = _bounded(
        [s.to_json() for s in filtered_summaries])

    payload: dict[str, Any] = {
        "schema_version": PROJECTION_SCHEMA_VERSION,
        "scan_id": scan_id,
        "generated_at": generated_at,
        "manifest_digest": manifest_digest,
        "status": status,
        "freshness": {
            "state": "not_evaluated",
            "reason_code": "freshness_not_implemented_this_slice",
        },
        "counts": {
            # M10 (cold-read, PR-B fix round 3): these are WHOLE-RUN
            # totals - deliberately unaffected by unit_id/feature_id/
            # readiness_state row-level filters, since dependency_summary/
            # high_fan_*/units_without_feature below are cross-cutting
            # aggregate statistics describing the ENTIRE scan, not just
            # whatever rows this particular filtered response returns.
            # "scope" makes that explicit rather than leaving a caller to
            # infer it from a mismatch against the returned row counts.
            "scope": "whole_run",
            "units": len(modules),
            "dependencies": len(dependencies),
            "features": len(features),
            "entry_points": len(entry_points),
            "readiness_signals": len(readiness_signals),
            "problems": len(problems),
        },
        "dependency_summary": _dependency_summary(dependencies),
        "high_fan_out_units": high_fan_out[:20],
        "high_fan_in_units": high_fan_in[:20],
        "units_without_feature": sorted(units_without_feature),
        "unmapped_entry_points": sorted(unmapped_entry_points),
        "problems": problem_rows,
        "truncated": bool(
            units_omitted or dependency_omitted or problem_omitted
            or feature_omitted or entry_point_omitted
            or readiness_signal_omitted or readiness_summary_omitted
        ),
        "omitted_counts": {
            "units": units_omitted, "dependencies": dependency_omitted, "problems": problem_omitted,
            "features": feature_omitted, "entry_points": entry_point_omitted,
            "readiness_signals": readiness_signal_omitted,
            "readiness_summaries": readiness_summary_omitted,
        },
    }

    if not dependencies_only:
        payload["units"] = units_rows
        payload["features"] = feature_rows
        payload["entry_points"] = entry_point_rows
        payload["readiness"] = {
            "signals": readiness_signal_rows,
            "summaries": readiness_summary_rows,
        }
    payload["dependencies"] = dependency_rows

    return payload
