"""#55 slice-1 PR-A: durable-artifact size ceilings, enforced at publish
time (DESIGN-55-comprehension-plane.md, "Validation tiers and size
ceilings"). The PR-A dispatch names "artifact ceilings enforced against
fixtures" as explicit acceptance evidence.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agenttalk.comprehension import ceilings as ceil
from agenttalk.comprehension import lock as lockmod
from agenttalk.comprehension import publish as pub
from agenttalk.comprehension import staging as stg
from agenttalk.comprehension.privacy import PrivacyPreflightResult


# ----------------------------------------------------------- measure_staging_artifacts

def test_measure_staging_artifacts_skips_owner_json(tmp_path: Path) -> None:
    (tmp_path / "owner.json").write_text('{"x": 1}', encoding="utf-8")
    (tmp_path / "scan.json").write_text("abc", encoding="utf-8")
    measurements = ceil.measure_staging_artifacts(tmp_path, record_counts={"scan.json": 3})
    assert [m.name for m in measurements] == ["scan.json"]
    assert measurements[0].byte_count == 3
    assert measurements[0].record_count == 3


def test_measure_staging_artifacts_refuses_an_unmeasured_artifact(tmp_path: Path) -> None:
    """F-1 (reviewer-3 on PR-A, rq-5bd5427ad64d): defaulting a missing
    record count to 0 was fail-OPEN inside an otherwise fail-closed
    ceiling. An artifact with no entry in record_counts now refuses
    outright — a genuinely empty artifact must declare 0 explicitly."""
    (tmp_path / "scan.json").write_text("abc", encoding="utf-8")
    with pytest.raises(ceil.ArtifactLimitExceeded, match="no declared record count"):
        ceil.measure_staging_artifacts(tmp_path, record_counts={})


def test_measure_staging_artifacts_accepts_an_explicit_zero_record_count(tmp_path: Path) -> None:
    (tmp_path / "scan.json").write_text("abc", encoding="utf-8")
    measurements = ceil.measure_staging_artifacts(tmp_path, record_counts={"scan.json": 0})
    assert measurements[0].record_count == 0


def test_measure_staging_artifacts_ignores_subdirectories(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "nested.json").write_text("{}", encoding="utf-8")
    (tmp_path / "scan.json").write_text("abc", encoding="utf-8")
    measurements = ceil.measure_staging_artifacts(tmp_path, record_counts={"scan.json": 0})
    assert [m.name for m in measurements] == ["scan.json"]


# ----------------------------------------------------------- enforce_artifact_ceilings

def test_enforce_ceilings_accepts_small_artifacts() -> None:
    measurements = [ceil.ArtifactMeasurement(name="scan.json", byte_count=100, record_count=10)]
    ceil.enforce_artifact_ceilings(measurements)  # must not raise


def test_enforce_ceilings_rejects_a_single_oversized_artifact_by_bytes() -> None:
    measurements = [ceil.ArtifactMeasurement(
        name="modules.json", byte_count=ceil.PER_ARTIFACT_BYTES_MAX + 1, record_count=1)]
    with pytest.raises(ceil.ArtifactLimitExceeded, match="modules.json"):
        ceil.enforce_artifact_ceilings(measurements)


def test_enforce_ceilings_rejects_a_single_artifact_with_too_many_records() -> None:
    measurements = [ceil.ArtifactMeasurement(
        name="modules.json", byte_count=100, record_count=ceil.PER_ARTIFACT_RECORDS_MAX + 1)]
    with pytest.raises(ceil.ArtifactLimitExceeded, match="records"):
        ceil.enforce_artifact_ceilings(measurements)


def test_enforce_ceilings_rejects_whole_run_byte_total_even_if_no_single_artifact_exceeds() -> None:
    # Each artifact sits exactly AT (never over) the per-artifact ceiling;
    # enough of them still cross the separate whole-run ceiling.
    count = ceil.RUN_BYTES_MAX // ceil.PER_ARTIFACT_BYTES_MAX + 1
    measurements = [
        ceil.ArtifactMeasurement(name=f"artifact-{i}.json",
                                  byte_count=ceil.PER_ARTIFACT_BYTES_MAX, record_count=1)
        for i in range(count)
    ]
    assert all(m.byte_count <= ceil.PER_ARTIFACT_BYTES_MAX for m in measurements)
    with pytest.raises(ceil.ArtifactLimitExceeded, match="whole-run"):
        ceil.enforce_artifact_ceilings(measurements)


def test_enforce_ceilings_rejects_whole_run_record_total() -> None:
    per_artifact_records = ceil.RUN_RECORDS_MAX // 3 + 1
    measurements = [
        ceil.ArtifactMeasurement(name=f"artifact-{i}.json", byte_count=10,
                                  record_count=per_artifact_records)
        for i in range(3)
    ]
    assert all(m.record_count <= ceil.PER_ARTIFACT_RECORDS_MAX for m in measurements)
    with pytest.raises(ceil.ArtifactLimitExceeded, match="whole-run"):
        ceil.enforce_artifact_ceilings(measurements)


# ----------------------------------------------------------- integration: publish_run refuses

def test_publish_run_refuses_and_publishes_no_run_when_a_ceiling_is_exceeded(
    comprehension_privacy_root: Path, comprehension_privacy: PrivacyPreflightResult, monkeypatch,
) -> None:
    """design: 'Exceeding a ceiling publishes no run.' Shrink the ceiling
    (rather than writing a real 16 MiB fixture) so the test stays fast
    while still exercising the REAL enforcement call site inside
    publish_run, not just the standalone ceilings function."""
    monkeypatch.setattr(ceil, "PER_ARTIFACT_BYTES_MAX", 4)
    lock = lockmod.acquire_scan_lock(
        comprehension_privacy_root, privacy=comprehension_privacy,
        predecessor_index_digest=None)
    staging = stg.create_staging_dir(
        comprehension_privacy_root, scan_id="scan-1", lock_handle=lock)
    (staging.path / "scan.json").write_text("this is way more than 4 bytes", encoding="utf-8")

    with pytest.raises(ceil.ArtifactLimitExceeded):
        pub.publish_run(
            comprehension_privacy_root, staging_handle=staging, lock_handle=lock,
            scan_id="scan-1", run_summary={"scan_id": "scan-1"},
            predecessor_index_digest=None, record_counts={"scan.json": 1},
        )
    assert not (comprehension_privacy_root / "runs" / "scan-1").exists()  # no run published
    assert staging.path.exists()  # staging left in place, not silently discarded
    assert not lock.path.exists()  # still a REPORTED failure — lock released


def test_publish_run_enforces_record_counts_from_the_caller(
    comprehension_privacy_root: Path, comprehension_privacy: PrivacyPreflightResult, monkeypatch,
) -> None:
    monkeypatch.setattr(ceil, "PER_ARTIFACT_RECORDS_MAX", 2)
    lock = lockmod.acquire_scan_lock(
        comprehension_privacy_root, privacy=comprehension_privacy,
        predecessor_index_digest=None)
    staging = stg.create_staging_dir(
        comprehension_privacy_root, scan_id="scan-1", lock_handle=lock)
    (staging.path / "modules.json").write_text("[]", encoding="utf-8")

    with pytest.raises(ceil.ArtifactLimitExceeded, match="records"):
        pub.publish_run(
            comprehension_privacy_root, staging_handle=staging, lock_handle=lock,
            scan_id="scan-1", run_summary={"scan_id": "scan-1"},
            predecessor_index_digest=None, record_counts={"modules.json": 3},
        )
    assert not (comprehension_privacy_root / "runs" / "scan-1").exists()


def test_publish_run_refuses_when_a_staged_artifact_has_no_declared_record_count(
    comprehension_privacy_root: Path, comprehension_privacy: PrivacyPreflightResult,
) -> None:
    """F-1 integration: publish_run's own default (record_counts omitted)
    must refuse rather than silently admit an unmeasured artifact."""
    lock = lockmod.acquire_scan_lock(
        comprehension_privacy_root, privacy=comprehension_privacy,
        predecessor_index_digest=None)
    staging = stg.create_staging_dir(
        comprehension_privacy_root, scan_id="scan-1", lock_handle=lock)
    (staging.path / "scan.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ceil.ArtifactLimitExceeded, match="no declared record count"):
        pub.publish_run(
            comprehension_privacy_root, staging_handle=staging, lock_handle=lock,
            scan_id="scan-1", run_summary={"scan_id": "scan-1"},
            predecessor_index_digest=None,
        )
    assert not (comprehension_privacy_root / "runs" / "scan-1").exists()
