"""#55 slice-1 PR-A: publication (rename staging -> runs/<scan-id>/, then
CAS-replace index.json) per DESIGN-55-comprehension-plane.md's "Local
storage model" publish sequence. Crash injection, predecessor-CAS, and a
Windows sharing-violation fixture are the PR-A dispatch's named acceptance
evidence for this module.
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


def _stage(tmp_path: Path, scan_id: str, content: str = "hello") -> tuple:
    lock = lockmod.acquire_scan_lock(tmp_path, predecessor_index_digest=None)
    staging = stg.create_staging_dir(tmp_path, scan_id=scan_id, owner_token=lock.owner_token)
    (staging.path / "scan.json").write_text(content, encoding="utf-8")
    return lock, staging


# ----------------------------------------------------------- happy path, first publish

def test_publish_run_first_ever_publish(tmp_path: Path) -> None:
    lock, staging = _stage(tmp_path, "scan-1")
    result = pub.publish_run(
        tmp_path, staging_handle=staging, lock_handle=lock, scan_id="scan-1",
        run_summary={"scan_id": "scan-1", "status": "complete"},
        predecessor_index_digest=None,
    )
    run_dir = tmp_path / "runs" / "scan-1"
    assert run_dir.is_dir()
    assert (run_dir / "scan.json").read_text(encoding="utf-8") == "hello"
    assert not staging.path.exists()  # renamed away, not copied
    assert result["latest_scan_id"] == "scan-1"
    assert result["predecessor_digest"] is None
    assert result["runs"] == [{"scan_id": "scan-1", "status": "complete"}]
    index_doc = json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))
    assert index_doc == result
    assert not lock.path.exists()  # released


def test_publish_run_second_publish_appends_and_chains_predecessor_digest(
    tmp_path: Path,
) -> None:
    lock1, staging1 = _stage(tmp_path, "scan-1")
    pub.publish_run(
        tmp_path, staging_handle=staging1, lock_handle=lock1, scan_id="scan-1",
        run_summary={"scan_id": "scan-1"}, predecessor_index_digest=None,
    )
    _doc, digest1 = pub.read_current_index(tmp_path)

    lock2 = lockmod.acquire_scan_lock(tmp_path, predecessor_index_digest=digest1)
    staging2 = stg.create_staging_dir(tmp_path, scan_id="scan-2", owner_token=lock2.owner_token)
    (staging2.path / "scan.json").write_text("world", encoding="utf-8")
    second = pub.publish_run(
        tmp_path, staging_handle=staging2, lock_handle=lock2, scan_id="scan-2",
        run_summary={"scan_id": "scan-2"}, predecessor_index_digest=digest1,
    )
    assert second["latest_scan_id"] == "scan-2"
    assert second["predecessor_digest"] == digest1
    assert second["runs"] == [{"scan_id": "scan-2"}, {"scan_id": "scan-1"}]
    assert (tmp_path / "runs" / "scan-1").is_dir()
    assert (tmp_path / "runs" / "scan-2").is_dir()


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


# ----------------------------------------------------------- never replaces a run dir

def test_rename_refuses_when_run_directory_already_exists(tmp_path: Path) -> None:
    (tmp_path / "runs" / "scan-1").mkdir(parents=True)
    lock, staging = _stage(tmp_path, "scan-1")
    with pytest.raises(pub.RunDirectoryExists):
        pub.rename_staging_to_run(tmp_path, staging, scan_id="scan-1")
    assert staging.path.exists()  # untouched
    lockmod.release_scan_lock(lock)


# ----------------------------------------------------------- crash injection

def test_crash_after_rename_before_index_write_leaves_a_valid_unindexed_run(
    tmp_path: Path,
) -> None:
    """design: 'A crash after step 1 but before step 2 leaves a valid
    unindexed run, never a half-current run; v1 does not silently adopt
    it.' Simulated by calling ONLY step 1 and then stopping, exactly as a
    real crash would."""
    lock, staging = _stage(tmp_path, "scan-1")
    pub.rename_staging_to_run(tmp_path, staging, scan_id="scan-1")
    # "crash" here: publish_index_cas and release_scan_lock never run.
    run_dir = tmp_path / "runs" / "scan-1"
    assert run_dir.is_dir()
    assert (run_dir / "scan.json").read_text(encoding="utf-8") == "hello"
    assert not (tmp_path / "index.json").exists()  # never indexed
    assert lock.path.exists()  # lock never released — provable stale evidence


def test_crash_after_rename_when_a_prior_run_was_already_indexed(tmp_path: Path) -> None:
    """Same crash point, but there WAS a previously-published generation —
    the old index must still name ONLY the old scan, never the new one."""
    lock1, staging1 = _stage(tmp_path, "scan-1")
    pub.publish_run(
        tmp_path, staging_handle=staging1, lock_handle=lock1, scan_id="scan-1",
        run_summary={"scan_id": "scan-1"}, predecessor_index_digest=None,
    )
    _doc, digest1 = pub.read_current_index(tmp_path)

    lock2 = lockmod.acquire_scan_lock(tmp_path, predecessor_index_digest=digest1)
    staging2 = stg.create_staging_dir(tmp_path, scan_id="scan-2", owner_token=lock2.owner_token)
    (staging2.path / "scan.json").write_text("world", encoding="utf-8")
    pub.rename_staging_to_run(tmp_path, staging2, scan_id="scan-2")
    # "crash" — index.json is never touched for scan-2.

    doc = json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))
    assert doc["latest_scan_id"] == "scan-1"  # still the OLD generation
    assert (tmp_path / "runs" / "scan-2").is_dir()  # the new run is valid, just unindexed
    assert lock2.path.exists()


def test_crash_after_index_write_before_release_leaves_lock_held(tmp_path: Path) -> None:
    lock, staging = _stage(tmp_path, "scan-1")
    pub.rename_staging_to_run(tmp_path, staging, scan_id="scan-1")
    pub.publish_index_cas(
        tmp_path, scan_id="scan-1", run_summary={"scan_id": "scan-1"},
        predecessor_index_digest=None,
    )
    # "crash" — release_scan_lock never runs.
    assert lock.path.exists()
    doc = json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))
    assert doc["latest_scan_id"] == "scan-1"  # this generation IS fully current


def test_reported_failure_still_releases_the_lock(tmp_path: Path) -> None:
    """design step 3: release 'only after the index replacement OR a
    reported failure' — a CAUGHT failure (unlike a crash) still releases."""
    lock, staging = _stage(tmp_path, "scan-1")
    (tmp_path / "runs" / "scan-1").mkdir(parents=True)  # force rename to fail
    with pytest.raises(pub.RunDirectoryExists):
        pub.publish_run(
            tmp_path, staging_handle=staging, lock_handle=lock, scan_id="scan-1",
            run_summary={"scan_id": "scan-1"}, predecessor_index_digest=None,
        )
    assert not lock.path.exists()  # released despite the failure


# ----------------------------------------------------------- predecessor-CAS conflict

def test_predecessor_cas_conflict_leaves_prior_index_untouched(tmp_path: Path) -> None:
    lock1, staging1 = _stage(tmp_path, "scan-1")
    pub.publish_run(
        tmp_path, staging_handle=staging1, lock_handle=lock1, scan_id="scan-1",
        run_summary={"scan_id": "scan-1"}, predecessor_index_digest=None,
    )
    _doc, digest_after_scan1 = pub.read_current_index(tmp_path)
    # A second writer publishes scan-2 concurrently, advancing the index...
    lock2, staging2 = _stage(tmp_path, "scan-2")
    pub.publish_run(
        tmp_path, staging_handle=staging2, lock_handle=lock2, scan_id="scan-2",
        run_summary={"scan_id": "scan-2"}, predecessor_index_digest=digest_after_scan1,
    )
    # ...while a THIRD writer had captured the predecessor digest from
    # BEFORE scan-2 landed (stale by the time it tries to publish).
    lock3, staging3 = _stage(tmp_path, "scan-3")
    with pytest.raises(pub.IndexCasConflict):
        pub.publish_index_cas(
            tmp_path, scan_id="scan-3", run_summary={"scan_id": "scan-3"},
            # stale — captured right after scan-1, but scan-2 has since landed
            predecessor_index_digest=digest_after_scan1,
        )
    doc = json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))
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


def test_read_current_index_matches_the_published_digest(tmp_path: Path) -> None:
    lock, staging = _stage(tmp_path, "scan-1")
    pub.publish_run(
        tmp_path, staging_handle=staging, lock_handle=lock, scan_id="scan-1",
        run_summary={"scan_id": "scan-1"}, predecessor_index_digest=None,
    )
    doc, digest = pub.read_current_index(tmp_path)
    assert doc["latest_scan_id"] == "scan-1"
    from agenttalk.comprehension.digests import canonical_content_digest
    assert digest == canonical_content_digest(doc)


# ----------------------------------------------------------- Windows sharing-violation fixture

def test_rename_retries_a_transient_windows_sharing_violation_then_succeeds(
    tmp_path: Path, monkeypatch,
) -> None:
    lock, staging = _stage(tmp_path, "scan-1")
    monkeypatch.setattr(pub, "_is_windows", lambda: True)
    real_rename = os.rename
    calls = {"n": 0}

    def flaky_rename(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("simulated sharing violation")
        real_rename(src, dst)

    monkeypatch.setattr(pub.os, "rename", flaky_rename)
    result = pub.rename_staging_to_run(tmp_path, staging, scan_id="scan-1")
    assert calls["n"] == 3
    assert result.is_dir()
    lockmod.release_scan_lock(lock)


def test_rename_fails_after_exhausting_the_retry_window(tmp_path: Path, monkeypatch) -> None:
    lock, staging = _stage(tmp_path, "scan-1")
    monkeypatch.setattr(pub, "_is_windows", lambda: True)
    monkeypatch.setattr(pub, "_RENAME_RETRY_TIMEOUT_SECONDS", 0.05)  # keep the test fast

    def always_fails(src, dst):
        raise PermissionError("simulated persistent sharing violation")

    monkeypatch.setattr(pub.os, "rename", always_fails)
    with pytest.raises(pub.RenamePublishFailed):
        pub.rename_staging_to_run(tmp_path, staging, scan_id="scan-1")
    assert staging.path.exists()  # never moved
    assert not (tmp_path / "runs" / "scan-1").exists()
    lockmod.release_scan_lock(lock)
