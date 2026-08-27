"""#55 slice-1 PR-A: publication (rename staging -> runs/<scan-id>/, then
CAS-replace index.json) per DESIGN-55-comprehension-plane.md's "Local
storage model" publish sequence. Crash injection, predecessor-CAS, and a
Windows sharing-violation fixture are the PR-A dispatch's named acceptance
evidence for this module.

Every ``acquire_scan_lock``/``create_staging_dir`` call threads a REAL
``PrivacyPreflightResult`` (reviewer-3 B-1 on PR-A, rq-5bd5427ad64d), and
every ``publish_run`` call declares ``record_counts`` explicitly (F-1: an
unmeasured artifact now REFUSES rather than defaulting to 0 records).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agenttalk.comprehension import lock as lockmod
from agenttalk.comprehension import publish as pub
from agenttalk.comprehension import staging as stg
from agenttalk.comprehension.errors import ComprehensionError
from agenttalk.comprehension.privacy import PrivacyPreflightResult


def _stage(
    root: Path, privacy: PrivacyPreflightResult, scan_id: str, content: str = "hello",
) -> tuple:
    lock = lockmod.acquire_scan_lock(root, privacy=privacy, predecessor_index_digest=None)
    staging = stg.create_staging_dir(scan_id=scan_id, lock_handle=lock)
    (staging.path / "scan.json").write_text(content, encoding="utf-8")
    return lock, staging


_COUNTS = {"scan.json": 1}


# ----------------------------------------------------------- happy path, first publish

def test_publish_run_first_ever_publish(
    comprehension_dir: Path, comprehension_privacy: PrivacyPreflightResult,
) -> None:
    lock, staging = _stage(comprehension_dir, comprehension_privacy, "scan-1")
    result = pub.publish_run(
        staging_handle=staging, lock_handle=lock,
        run_summary={"scan_id": "scan-1", "status": "complete"},
        predecessor_index_digest=None, record_counts=_COUNTS,
    )
    run_dir = comprehension_dir / "runs" / "scan-1"
    assert run_dir.is_dir()
    assert (run_dir / "scan.json").read_text(encoding="utf-8") == "hello"
    assert not staging.path.exists()  # renamed away, not copied
    assert result["latest_scan_id"] == "scan-1"
    assert result["predecessor_digest"] is None
    assert result["runs"] == [{"scan_id": "scan-1", "status": "complete"}]
    index_doc = json.loads((comprehension_dir / "index.json").read_text(encoding="utf-8"))
    assert index_doc == result
    assert not lock.path.exists()  # released


def test_publish_run_second_publish_appends_and_chains_predecessor_digest(
    comprehension_dir: Path, comprehension_privacy: PrivacyPreflightResult,
) -> None:
    lock1, staging1 = _stage(comprehension_dir, comprehension_privacy, "scan-1")
    pub.publish_run(
        staging_handle=staging1, lock_handle=lock1,
        run_summary={"scan_id": "scan-1"}, predecessor_index_digest=None, record_counts=_COUNTS,
    )
    _doc, digest1 = pub.read_current_index(comprehension_dir)

    lock2 = lockmod.acquire_scan_lock(
        comprehension_dir, privacy=comprehension_privacy,
        predecessor_index_digest=digest1)
    staging2 = stg.create_staging_dir(scan_id="scan-2", lock_handle=lock2)
    (staging2.path / "scan.json").write_text("world", encoding="utf-8")
    second = pub.publish_run(
        staging_handle=staging2, lock_handle=lock2,
        run_summary={"scan_id": "scan-2"}, predecessor_index_digest=digest1,
        record_counts=_COUNTS,
    )
    assert second["latest_scan_id"] == "scan-2"
    assert second["predecessor_digest"] == digest1
    assert second["runs"] == [{"scan_id": "scan-2"}, {"scan_id": "scan-1"}]
    assert (comprehension_dir / "runs" / "scan-1").is_dir()
    assert (comprehension_dir / "runs" / "scan-2").is_dir()


def test_index_runs_list_is_bounded(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(pub, "_INDEX_RUNS_MAX", 3)
    digest = None
    for i in range(5):
        pub.publish_index_cas(
            tmp_path, scan_id=f"scan-{i}", run_summary={"scan_id": f"scan-{i}"},
            predecessor_index_digest=digest,
        )
        _doc, digest = pub.read_current_index(tmp_path)
    doc, _digest = pub.read_current_index(tmp_path)
    assert [r["scan_id"] for r in doc["runs"]] == ["scan-4", "scan-3", "scan-2"]


# ----------------------------------------------------------- ownership cross-check (finding 2)

def test_rename_refuses_a_staging_handle_from_a_different_lock(
    comprehension_dir: Path, comprehension_privacy: PrivacyPreflightResult,
) -> None:
    """reviewer-1 cold-read finding 2 on PR-A (rq-6cc5560b62f6): "cross-check
    staging_handle.scan_id + owner token + lock/root identity at rename" —
    a staging directory created under one lock's acquisition must never be
    published under a DIFFERENT lock's authority, even at the same root."""
    lock1, staging1 = _stage(comprehension_dir, comprehension_privacy, "scan-1")
    lockmod.release_scan_lock(lock1)
    lock2 = lockmod.acquire_scan_lock(
        comprehension_dir, privacy=comprehension_privacy, predecessor_index_digest=None)
    with pytest.raises(pub.StagingOwnershipMismatch):
        pub.rename_staging_to_run(staging1, lock2)
    assert staging1.path.exists()  # untouched
    lockmod.release_scan_lock(lock2)


def test_rename_refuses_a_forged_handle_naming_an_external_directory(
    comprehension_dir: Path, comprehension_privacy: PrivacyPreflightResult,
) -> None:
    """reviewer-1 cold-read finding 2 on PR-A, round 2 (rq-6cc5560b62f6),
    reproduced: ``StagingHandle`` is a public, trivially-constructible
    dataclass — a handle naming an EXTERNAL directory (never created by
    ``create_staging_dir``, outside ``.staging/`` entirely), carrying a
    COPIED real ``owner_token``, was previously accepted by the
    owner-token string comparison alone; the external directory was
    removed from its own location and its content published under
    ``runs/``. The fix must confine the source and re-derive trust from
    the actual owner.json on disk, so this is refused even though the
    forged handle's ``owner_token`` field genuinely matches the lock."""
    lock = lockmod.acquire_scan_lock(
        comprehension_dir, privacy=comprehension_privacy, predecessor_index_digest=None)
    external = comprehension_dir.parent.parent / "outside-comprehension-entirely"
    external.mkdir(parents=True)
    (external / "scan.json").write_text("stolen content", encoding="utf-8")
    forged = stg.StagingHandle(path=external, scan_id="scan-1", owner_token=lock.owner_token)

    with pytest.raises(pub.StagingSourceEscapesRoot):
        pub.rename_staging_to_run(forged, lock)

    assert external.exists()  # never moved
    assert (external / "scan.json").read_text(encoding="utf-8") == "stolen content"
    assert not (comprehension_dir / "runs" / "scan-1").exists()
    lockmod.release_scan_lock(lock)


def test_publish_run_cross_checks_run_summary_scan_id_against_staging(
    comprehension_dir: Path, comprehension_privacy: PrivacyPreflightResult,
) -> None:
    """The published index must never claim a different scan happened than
    what was actually renamed into runs/ — a mismatched
    ``run_summary["scan_id"]`` refuses before anything is renamed."""
    lock, staging = _stage(comprehension_dir, comprehension_privacy, "scan-1")
    with pytest.raises(ComprehensionError, match="scan_id"):
        pub.publish_run(
            staging_handle=staging, lock_handle=lock,
            run_summary={"scan_id": "scan-999"},
            predecessor_index_digest=None, record_counts=_COUNTS,
        )
    assert staging.path.exists()  # never renamed
    assert not (comprehension_dir / "runs" / "scan-1").exists()
    assert not lock.path.exists()  # released despite the failure (design step 3)


# ----------------------------------------------------------- never replaces a run dir

def test_rename_refuses_when_run_directory_already_exists(
    comprehension_dir: Path, comprehension_privacy: PrivacyPreflightResult,
) -> None:
    (comprehension_dir / "runs" / "scan-1").mkdir(parents=True)
    lock, staging = _stage(comprehension_dir, comprehension_privacy, "scan-1")
    with pytest.raises(pub.RunDirectoryExists):
        pub.rename_staging_to_run(staging, lock)
    assert staging.path.exists()  # untouched
    lockmod.release_scan_lock(lock)


# ----------------------------------------------------------- crash injection

def test_crash_after_rename_before_index_write_leaves_a_valid_unindexed_run(
    comprehension_dir: Path, comprehension_privacy: PrivacyPreflightResult,
) -> None:
    """design: 'A crash after step 1 but before step 2 leaves a valid
    unindexed run, never a half-current run; v1 does not silently adopt
    it.' Simulated by calling ONLY step 1 and then stopping, exactly as a
    real crash would."""
    lock, staging = _stage(comprehension_dir, comprehension_privacy, "scan-1")
    pub.rename_staging_to_run(staging, lock)
    # "crash" here: publish_index_cas and release_scan_lock never run.
    run_dir = comprehension_dir / "runs" / "scan-1"
    assert run_dir.is_dir()
    assert (run_dir / "scan.json").read_text(encoding="utf-8") == "hello"
    assert not (comprehension_dir / "index.json").exists()  # never indexed
    assert lock.path.exists()  # lock never released — provable stale evidence


def test_crash_after_rename_when_a_prior_run_was_already_indexed(
    comprehension_dir: Path, comprehension_privacy: PrivacyPreflightResult,
) -> None:
    """Same crash point, but there WAS a previously-published generation —
    the old index must still name ONLY the old scan, never the new one."""
    lock1, staging1 = _stage(comprehension_dir, comprehension_privacy, "scan-1")
    pub.publish_run(
        staging_handle=staging1, lock_handle=lock1,
        run_summary={"scan_id": "scan-1"}, predecessor_index_digest=None, record_counts=_COUNTS,
    )
    _doc, digest1 = pub.read_current_index(comprehension_dir)

    lock2 = lockmod.acquire_scan_lock(
        comprehension_dir, privacy=comprehension_privacy,
        predecessor_index_digest=digest1)
    staging2 = stg.create_staging_dir(scan_id="scan-2", lock_handle=lock2)
    (staging2.path / "scan.json").write_text("world", encoding="utf-8")
    pub.rename_staging_to_run(staging2, lock2)
    # "crash" — index.json is never touched for scan-2.

    doc = json.loads((comprehension_dir / "index.json").read_text(encoding="utf-8"))
    assert doc["latest_scan_id"] == "scan-1"  # still the OLD generation
    assert (comprehension_dir / "runs" / "scan-2").is_dir()  # valid, just unindexed
    assert lock2.path.exists()


def test_crash_after_index_write_before_release_leaves_lock_held(
    comprehension_dir: Path, comprehension_privacy: PrivacyPreflightResult,
) -> None:
    lock, staging = _stage(comprehension_dir, comprehension_privacy, "scan-1")
    pub.rename_staging_to_run(staging, lock)
    pub.publish_index_cas(
        comprehension_dir, scan_id="scan-1", run_summary={"scan_id": "scan-1"},
        predecessor_index_digest=None,
    )
    # "crash" — release_scan_lock never runs.
    assert lock.path.exists()
    doc = json.loads((comprehension_dir / "index.json").read_text(encoding="utf-8"))
    assert doc["latest_scan_id"] == "scan-1"  # this generation IS fully current


def test_reported_failure_still_releases_the_lock(
    comprehension_dir: Path, comprehension_privacy: PrivacyPreflightResult,
) -> None:
    """design step 3: release 'only after the index replacement OR a
    reported failure' — a CAUGHT failure (unlike a crash) still releases."""
    lock, staging = _stage(comprehension_dir, comprehension_privacy, "scan-1")
    (comprehension_dir / "runs" / "scan-1").mkdir(parents=True)  # force rename to fail
    with pytest.raises(pub.RunDirectoryExists):
        pub.publish_run(
            staging_handle=staging, lock_handle=lock,
            run_summary={"scan_id": "scan-1"},
            predecessor_index_digest=None, record_counts=_COUNTS,
        )
    assert not lock.path.exists()  # released despite the failure


# ----------------------------------------------------------- predecessor-CAS conflict

def test_predecessor_cas_conflict_leaves_prior_index_untouched(
    comprehension_dir: Path, comprehension_privacy: PrivacyPreflightResult,
) -> None:
    lock1, staging1 = _stage(comprehension_dir, comprehension_privacy, "scan-1")
    pub.publish_run(
        staging_handle=staging1, lock_handle=lock1,
        run_summary={"scan_id": "scan-1"}, predecessor_index_digest=None, record_counts=_COUNTS,
    )
    _doc, digest_after_scan1 = pub.read_current_index(comprehension_dir)
    # A second writer publishes scan-2 concurrently, advancing the index...
    lock2, staging2 = _stage(comprehension_dir, comprehension_privacy, "scan-2")
    pub.publish_run(
        staging_handle=staging2, lock_handle=lock2,
        run_summary={"scan_id": "scan-2"}, predecessor_index_digest=digest_after_scan1,
        record_counts=_COUNTS,
    )
    # ...while a THIRD writer had captured the predecessor digest from
    # BEFORE scan-2 landed (stale by the time it tries to publish).
    lock3, _staging3 = _stage(comprehension_dir, comprehension_privacy, "scan-3")
    with pytest.raises(pub.IndexCasConflict):
        pub.publish_index_cas(
            comprehension_dir, scan_id="scan-3", run_summary={"scan_id": "scan-3"},
            # stale — captured right after scan-1, but scan-2 has since landed
            predecessor_index_digest=digest_after_scan1,
        )
    doc = json.loads((comprehension_dir / "index.json").read_text(encoding="utf-8"))
    assert doc["latest_scan_id"] == "scan-2"  # untouched by the conflicting attempt
    lockmod.release_scan_lock(lock3)


def test_publish_index_cas_rejects_a_run_summary_without_scan_id(tmp_path: Path) -> None:
    with pytest.raises(ComprehensionError, match="scan_id"):
        pub.publish_index_cas(
            tmp_path, scan_id="scan-1", run_summary={"status": "complete"},
            predecessor_index_digest=None,
        )


# ----------------------------------------------------------- read_current_index

def test_read_current_index_on_absent_index(tmp_path: Path) -> None:
    assert pub.read_current_index(tmp_path) == (None, None)


def test_read_current_index_matches_the_published_digest(
    comprehension_dir: Path, comprehension_privacy: PrivacyPreflightResult,
) -> None:
    lock, staging = _stage(comprehension_dir, comprehension_privacy, "scan-1")
    pub.publish_run(
        staging_handle=staging, lock_handle=lock,
        run_summary={"scan_id": "scan-1"}, predecessor_index_digest=None, record_counts=_COUNTS,
    )
    doc, digest = pub.read_current_index(comprehension_dir)
    assert doc["latest_scan_id"] == "scan-1"
    from agenttalk.comprehension.digests import canonical_content_digest
    assert digest == canonical_content_digest(doc)


# ----------------------------------------------------------- Windows sharing-violation fixture

def test_rename_retries_a_transient_windows_sharing_violation_then_succeeds(
    comprehension_dir: Path, comprehension_privacy: PrivacyPreflightResult, monkeypatch,
) -> None:
    lock, staging = _stage(comprehension_dir, comprehension_privacy, "scan-1")
    monkeypatch.setattr(pub, "_is_windows", lambda: True)
    real_rename = os.rename
    calls = {"n": 0}

    def flaky_rename(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("simulated sharing violation")
        real_rename(src, dst)

    monkeypatch.setattr(pub.os, "rename", flaky_rename)
    result = pub.rename_staging_to_run(staging, lock)
    assert calls["n"] == 3
    assert result.is_dir()
    lockmod.release_scan_lock(lock)


def test_rename_fails_after_exhausting_the_retry_window(
    comprehension_dir: Path, comprehension_privacy: PrivacyPreflightResult, monkeypatch,
) -> None:
    lock, staging = _stage(comprehension_dir, comprehension_privacy, "scan-1")
    monkeypatch.setattr(pub, "_is_windows", lambda: True)
    monkeypatch.setattr(pub, "_RENAME_RETRY_TIMEOUT_SECONDS", 0.05)  # keep the test fast

    def always_fails(src, dst):
        raise PermissionError("simulated persistent sharing violation")

    monkeypatch.setattr(pub.os, "rename", always_fails)
    with pytest.raises(pub.RenamePublishFailed):
        pub.rename_staging_to_run(staging, lock)
    assert staging.path.exists()  # never moved
    assert not (comprehension_dir / "runs" / "scan-1").exists()
    lockmod.release_scan_lock(lock)


# ----------------------------------------------------------- old-generation concurrent readers

def test_a_reader_bound_to_the_old_generation_is_undisturbed_by_a_concurrent_publish(
    comprehension_dir: Path, comprehension_privacy: PrivacyPreflightResult,
) -> None:
    """design: 'A concurrent scan cannot disturb readers of the prior
    published generation' and 'a reader that already loaded the old
    catalog keeps a complete old generation.' Simulated the way a real
    reader would experience it: read index.json, resolve scan-1's run
    directory, hold that content — THEN a second scan publishes scan-2 —
    then re-read what the first reader already resolved and prove it is
    byte-identical to what it read before scan-2 ever landed."""
    lock1, staging1 = _stage(
        comprehension_dir, comprehension_privacy, "scan-1", content="generation one")
    pub.publish_run(
        staging_handle=staging1, lock_handle=lock1,
        run_summary={"scan_id": "scan-1"}, predecessor_index_digest=None, record_counts=_COUNTS,
    )

    # The reader: loads the catalog, resolves ITS scan's run directory and
    # content, and holds onto both — exactly what a caller bound to an
    # exact scan_id (e.g. a brief-time context pack) would do.
    reader_index_doc, digest_after_scan1 = pub.read_current_index(comprehension_dir)
    reader_scan_id = reader_index_doc["latest_scan_id"]
    reader_run_dir = comprehension_dir / "runs" / reader_scan_id
    reader_content_before = (reader_run_dir / "scan.json").read_text(encoding="utf-8")

    # A second scan publishes concurrently, advancing the catalog.
    lock2, staging2 = _stage(
        comprehension_dir, comprehension_privacy, "scan-2", content="generation two")
    pub.publish_run(
        staging_handle=staging2, lock_handle=lock2,
        run_summary={"scan_id": "scan-2"}, predecessor_index_digest=digest_after_scan1,
        record_counts=_COUNTS,
    )

    # The reader's OWN generation is completely undisturbed: same directory,
    # same content, still resolvable, even though the catalog has moved on.
    assert reader_run_dir.is_dir()
    assert (reader_run_dir / "scan.json").read_text(encoding="utf-8") == reader_content_before
    assert reader_content_before == "generation one"

    # The catalog itself DID advance — the reader just never re-read it.
    live_doc, _digest = pub.read_current_index(comprehension_dir)
    assert live_doc["latest_scan_id"] == "scan-2"
    assert live_doc["latest_scan_id"] != reader_scan_id
