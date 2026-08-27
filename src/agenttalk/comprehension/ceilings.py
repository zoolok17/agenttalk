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
section pending a measured Amperian corpus run (PR-B's exit gate) — this
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
    """
    measurements = []
    for path in sorted(staging_dir.iterdir()):
        if not path.is_file() or path.name in _NON_ARTIFACT_FILENAMES:
            continue
        if path.name not in record_counts:
            raise ArtifactLimitExceeded(
                f"{path.name} has no declared record count - an unmeasured artifact "
                "cannot be admitted under this fail-closed ceiling; the producer must "
                "declare it explicitly (even as 0), per scan.json's own contract")
        measurements.append(ArtifactMeasurement(
            name=path.name,
            byte_count=path.stat().st_size,
            record_count=record_counts[path.name],
        ))
    return measurements


def enforce_artifact_ceilings(measurements: list[ArtifactMeasurement]) -> None:
    """Raises :class:`ArtifactLimitExceeded` on the FIRST ceiling any
    measurement or the run total crosses — per-artifact ceilings first
    (so the message names the one specific oversized artifact when
    possible), then the whole-run totals."""
    for m in measurements:
        if m.byte_count > PER_ARTIFACT_BYTES_MAX:
            raise ArtifactLimitExceeded(
                f"{m.name} is {m.byte_count} bytes, exceeding the "
                f"{PER_ARTIFACT_BYTES_MAX}-byte per-artifact ceiling — narrow "
                "--scope and rescan")
        if m.record_count > PER_ARTIFACT_RECORDS_MAX:
            raise ArtifactLimitExceeded(
                f"{m.name} has {m.record_count} records, exceeding the "
                f"{PER_ARTIFACT_RECORDS_MAX}-record per-artifact ceiling — narrow "
                "--scope and rescan")
    total_bytes = sum(m.byte_count for m in measurements)
    if total_bytes > RUN_BYTES_MAX:
        raise ArtifactLimitExceeded(
            f"this run's durable artifacts total {total_bytes} bytes, exceeding "
            f"the {RUN_BYTES_MAX}-byte whole-run ceiling — narrow --scope and rescan")
    total_records = sum(m.record_count for m in measurements)
    if total_records > RUN_RECORDS_MAX:
        raise ArtifactLimitExceeded(
            f"this run's durable artifacts total {total_records} records, "
            f"exceeding the {RUN_RECORDS_MAX}-record whole-run ceiling — narrow "
            "--scope and rescan")
