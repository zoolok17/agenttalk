"""Durable-artifact size ceilings, enforced at publish time.

DESIGN-55-comprehension-plane.md, "Validation tiers and size ceilings":

    The v1 single-document format has hard publish ceilings: 16 MiB and
    100,000 records per artifact, and 64 MiB and 250,000 records for all
    durable artifacts in one run. The lower limit wins. `scan.json`
    declares the measured byte and record counts. Exceeding a ceiling
    publishes no run and reports `artifact_limit` with a request to
    narrow scope; it never allocates or parses an unbounded document on a
    normal read path.

These constants are PROVISIONAL per the design's own "Scan behavior"
section pending a measured client-corpus run (PR-B's exit gate) — this
module enforces whatever the constants say, not a claim that these exact
numbers are final.

Record counting is schema-specific (each artifact type knows its own
record list shape) and PR-A owns no artifact schema yet — that is PR-B/C
producer territory. So this module measures what it CAN own generically
(on-disk byte size) and accepts each artifact's record count as a
caller-supplied measurement, keeping the ceiling ARITHMETIC here as one
shared, un-driftable place for every future producer to call into.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .errors import ComprehensionError

PER_ARTIFACT_BYTES_MAX = 16 * 1024 * 1024
PER_ARTIFACT_RECORDS_MAX = 100_000
RUN_BYTES_MAX = 64 * 1024 * 1024
RUN_RECORDS_MAX = 250_000

#: Never a "durable artifact" subject to these ceilings — it repeats the
#: lock token for staging reclaim, not scan content (see staging.py).
_NON_ARTIFACT_FILENAMES = frozenset({"owner.json"})


class ArtifactLimitExceeded(ComprehensionError):
    """design: "Exceeding a ceiling publishes no run and reports
    `artifact_limit` with a request to narrow scope." Raised before
    publication ever begins — the caller's staging directory is simply
    abandoned (never renamed into `runs/`), so nothing durable is ever
    partially written."""

    reason_code = "artifact_limit"


@dataclass(frozen=True)
class ArtifactMeasurement:
    name: str
    byte_count: int
    record_count: int


def measure_staging_artifacts(
    staging_dir: Path, *, record_counts: dict[str, int],
) -> list[ArtifactMeasurement]:
    """One measurement per durable artifact file directly inside
    ``staging_dir`` (non-recursive — v1's artifacts are flat files per the
    storage layout). ``record_counts`` maps filename -> record count.

    A file with NO entry in ``record_counts`` REFUSES (reviewer-3 F-1 on
    PR-A, rq-5bd5427ad64d: defaulting an unmeasured artifact to 0 records
    is fail-OPEN inside an otherwise fail-closed ceiling — a producer that
    forgets to declare a count silently skips the record ceiling entirely,
    the opposite of the byte ceiling's behavior, which is always measured
    from disk and cannot be skipped). The design's own contract is that
    ``scan.json`` declares the measured record counts, so an unmeasured
    artifact is a producer bug, not a legitimately-empty one — a
    genuinely empty artifact still gets an explicit ``0`` entry.

    A declared count that is not a non-negative ``int`` REFUSES before any
    ceiling arithmetic runs (reviewer-1 cold-read finding 4 on PR-A,
    rq-6cc5560b62f6, reproduced: a negative count let the per-artifact
    total UNDERSTATE the true whole-run total, hiding an over-cap run
    behind a declared sum that stayed within it). A symlinked staging
    entry REFUSES rather than being measured (reviewer-1 cold-read
    low-confidence residual on PR-A, same request: a staged symlink could
    otherwise resolve outside the private tree and be admitted as a
    durable published artifact) — checked with ``is_symlink()`` BEFORE
    ``is_file()``/``stat()``, since both of those follow a symlink rather
    than reporting on the link itself.
    """
    measurements = []
    for path in sorted(staging_dir.iterdir()):
        if path.name in _NON_ARTIFACT_FILENAMES:
            continue
        if path.is_symlink():
            raise ArtifactLimitExceeded(
                f"{path.name} is a symlink - a staged artifact must be a real file, "
                "never a link that could resolve outside the private staging tree")
        if not path.is_file():
            continue
        if path.name not in record_counts:
            raise ArtifactLimitExceeded(
                f"{path.name} has no declared record count - an unmeasured artifact "
                "cannot be admitted under this fail-closed ceiling; the producer must "
                "declare it explicitly (even as 0), per scan.json's own contract")
        record_count = record_counts[path.name]
        if (
            not isinstance(record_count, int)
            or isinstance(record_count, bool)
            or record_count < 0
        ):
            raise ArtifactLimitExceeded(
                f"{path.name}'s declared record count must be a non-negative integer, "
                f"got {record_count!r}")
        measurements.append(ArtifactMeasurement(
            name=path.name,
            byte_count=path.stat().st_size,
            record_count=record_count,
        ))
    return measurements


#: Note 6 (second cold read, fix round 4): refusal messages previously
#: said "narrow --scope and rescan" - no such flag exists this slice
#: (config.json parsing / --scope / --exclude narrowing is a named,
#: deferred gap - see scan_pipeline.py's own module docstring). Naming a
#: nonexistent remedy is worse than naming none; this states what IS
#: actually true and actionable this slice.
#:
#: N1 (fifth cold read, fix round 8): "--root" is the GLOBAL flag (added
#: by launch_admission.add_agenttalk_launch_arguments to the top-level
#: parser, before subparsers) - it must precede the "comprehension"
#: subcommand itself (empirically verified: `agenttalk --root <path>
#: comprehension scan` works; `agenttalk comprehension scan --root
#: <path>` fails with "unrecognized arguments", since comprehension's
#: own subparser defines no --root of its own). Bare "--root" invited a
#: reader to place it after the subcommand instead - named explicitly
#: here so this refusal's own remedy actually works if followed.
_NARROW_SCOPE_HINT = (
    "this slice has no --scope/--exclude narrowing yet - point the global --root at a "
    "smaller project (e.g. `agenttalk --root <path> comprehension scan` - --root must "
    "precede the comprehension subcommand), or split the scan"
)


def enforce_artifact_ceilings(measurements: list[ArtifactMeasurement]) -> None:
    """Raises :class:`ArtifactLimitExceeded` on the FIRST ceiling any
    measurement or the run total crosses — per-artifact ceilings first
    (so the message names the one specific oversized artifact when
    possible), then the whole-run totals."""
    for m in measurements:
        if m.byte_count > PER_ARTIFACT_BYTES_MAX:
            raise ArtifactLimitExceeded(
                f"{m.name} is {m.byte_count} bytes, exceeding the "
                f"{PER_ARTIFACT_BYTES_MAX}-byte per-artifact ceiling — {_NARROW_SCOPE_HINT}")
        if m.record_count > PER_ARTIFACT_RECORDS_MAX:
            raise ArtifactLimitExceeded(
                f"{m.name} has {m.record_count} records, exceeding the "
                f"{PER_ARTIFACT_RECORDS_MAX}-record per-artifact ceiling — {_NARROW_SCOPE_HINT}")
    total_bytes = sum(m.byte_count for m in measurements)
    if total_bytes > RUN_BYTES_MAX:
        raise ArtifactLimitExceeded(
            f"this run's durable artifacts total {total_bytes} bytes, exceeding "
            f"the {RUN_BYTES_MAX}-byte whole-run ceiling — {_NARROW_SCOPE_HINT}")
    total_records = sum(m.record_count for m in measurements)
    if total_records > RUN_RECORDS_MAX:
        raise ArtifactLimitExceeded(
            f"this run's durable artifacts total {total_records} records, "
            f"exceeding the {RUN_RECORDS_MAX}-record whole-run ceiling — {_NARROW_SCOPE_HINT}")
