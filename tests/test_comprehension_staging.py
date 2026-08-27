"""#55 slice-1 PR-A: .staging/<scan-id>-<nonce>/ + owner.json reclaim/prune
(DESIGN-55-comprehension-plane.md, "Local storage model").

``create_staging_dir`` now requires a real ``lock.ScanLockHandle`` (closing
the same door reviewer-3's B-1 finding on PR-A, rq-5bd5427ad64d, opened for
``acquire_scan_lock``), which in turn requires a real
``PrivacyPreflightResult`` — so every test below acquires a REAL lock
(via the ``comprehension_privacy``/``comprehension_dir``
fixtures, which run the real preflight against a real git repo) before
creating a staging directory.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agenttalk.comprehension import lock as lockmod
from agenttalk.comprehension import staging as stg
from agenttalk.comprehension.envelope import EnvelopeError
from agenttalk.comprehension.privacy import PrivacyPreflightResult


def _lock(root: Path, privacy: PrivacyPreflightResult) -> lockmod.ScanLockHandle:
    return lockmod.acquire_scan_lock(root, privacy=privacy, predecessor_index_digest=None)


# ----------------------------------------------------------- create_staging_dir

def test_create_staging_dir_creates_directory_and_owner_json(
    comprehension_dir: Path, comprehension_privacy: PrivacyPreflightResult,
) -> None:
    lock = _lock(comprehension_dir, comprehension_privacy)
    handle = stg.create_staging_dir(scan_id="scan-1", lock_handle=lock)
    assert handle.path.is_dir()
    assert handle.path.parent == comprehension_dir / ".staging"
    assert handle.path.name.startswith("scan-1-")
    owner = json.loads((handle.path / "owner.json").read_text(encoding="utf-8"))
    assert owner["scan_id"] == "scan-1"
    assert owner["owner_token"] == lock.owner_token
    assert owner["pid"] == os.getpid()
    lockmod.release_scan_lock(lock)


def test_create_staging_dir_nonces_are_unique(
    comprehension_dir: Path, comprehension_privacy: PrivacyPreflightResult,
) -> None:
    lock = _lock(comprehension_dir, comprehension_privacy)
    first = stg.create_staging_dir(scan_id="scan-1", lock_handle=lock)
    second = stg.create_staging_dir(scan_id="scan-1", lock_handle=lock)
    assert first.path != second.path
    assert first.path.exists() and second.path.exists()
    lockmod.release_scan_lock(lock)


def test_traversal_scan_id_is_rejected_before_any_path_is_built(
    comprehension_dir: Path, comprehension_privacy: PrivacyPreflightResult,
) -> None:
    """reviewer-1 cold-read finding 2 on PR-A (rq-6cc5560b62f6), reproduced
    as a permanent regression test: ``scan_id="../../../../escaped"``
    previously wrote ``owner.json`` OUTSIDE the protected project root.
    The closed scan-ID grammar must refuse it before any filesystem path
    is even constructed, leaving zero bytes written anywhere."""
    lock = _lock(comprehension_dir, comprehension_privacy)
    project_root = comprehension_dir.parent.parent
    with pytest.raises(EnvelopeError, match="scan_id"):
        stg.create_staging_dir(scan_id="../../../../escaped", lock_handle=lock)
    for path in project_root.rglob("*"):
        assert "escaped" not in path.name
    assert not (project_root.parent / "escaped").exists()
    lockmod.release_scan_lock(lock)


# ----------------------------------------------------------- reclaim: empty/absent

def test_reclaim_on_absent_staging_dir_is_a_silent_no_op(tmp_path: Path) -> None:
    report = stg.reclaim_abandoned_staging(tmp_path)
    assert report.reclaimed == []
    assert report.retained == []


def test_reclaim_on_empty_staging_dir_is_a_no_op(tmp_path: Path) -> None:
    (tmp_path / ".staging").mkdir()
    report = stg.reclaim_abandoned_staging(tmp_path)
    assert report.reclaimed == []
    assert report.retained == []


# ----------------------------------------------------------- reclaim: live owner retained

def test_reclaim_retains_a_live_owners_staging_dir(
    comprehension_dir: Path, comprehension_privacy: PrivacyPreflightResult,
) -> None:
    lock = _lock(comprehension_dir, comprehension_privacy)
    handle = stg.create_staging_dir(scan_id="scan-1", lock_handle=lock)
    report = stg.reclaim_abandoned_staging(comprehension_dir)
    assert report.reclaimed == []
    assert report.retained == [(handle.path.name, f"owner pid {os.getpid()} is alive")]
    assert handle.path.exists()
    lockmod.release_scan_lock(lock)


# ----------------------------------------------------------- reclaim: definitely dead

def test_reclaim_removes_a_definitely_dead_owners_staging_dir(
    comprehension_dir: Path, comprehension_privacy: PrivacyPreflightResult, monkeypatch,
) -> None:
    lock = _lock(comprehension_dir, comprehension_privacy)
    handle = stg.create_staging_dir(scan_id="scan-1", lock_handle=lock)
    monkeypatch.setattr(stg, "process_observation", lambda pid: ("dead", None))
    report = stg.reclaim_abandoned_staging(comprehension_dir)
    assert report.reclaimed == [handle.path.name]
    assert report.retained == []
    assert not handle.path.exists()
    lockmod.release_scan_lock(lock)


def test_reclaim_removes_only_the_dead_ones_among_several(
    comprehension_dir: Path, comprehension_privacy: PrivacyPreflightResult, monkeypatch,
) -> None:
    lock = _lock(comprehension_dir, comprehension_privacy)
    dead = stg.create_staging_dir(scan_id="scan-dead", lock_handle=lock)
    alive = stg.create_staging_dir(scan_id="scan-alive", lock_handle=lock)

    # Both fixtures recorded THIS test process's real (alive) pid at
    # creation time; give "dead" a distinct, guaranteed-fake pid so the two
    # fixtures can be told apart by a single process_observation stub.
    owner_file = dead.path / "owner.json"
    doc = json.loads(owner_file.read_text(encoding="utf-8"))
    doc["pid"] = 999999
    owner_file.write_text(json.dumps(doc), encoding="utf-8")

    monkeypatch.setattr(
        stg, "process_observation",
        lambda pid: ("dead", None) if pid == 999999 else ("alive", None),
    )

    report = stg.reclaim_abandoned_staging(comprehension_dir)
    assert report.reclaimed == [dead.path.name]
    assert not dead.path.exists()
    assert alive.path.exists()
    lockmod.release_scan_lock(lock)


# ----------------------------------------------------------- reclaim: ambiguous retained

def test_reclaim_retains_a_directory_with_missing_owner_json(tmp_path: Path) -> None:
    staging = tmp_path / ".staging" / "scan-x-abcdef"
    staging.mkdir(parents=True)
    report = stg.reclaim_abandoned_staging(tmp_path)
    assert report.reclaimed == []
    assert len(report.retained) == 1
    name, reason = report.retained[0]
    assert name == "scan-x-abcdef"
    assert "missing or malformed" in reason
    assert staging.exists()


def test_reclaim_retains_a_directory_with_malformed_owner_json(tmp_path: Path) -> None:
    staging = tmp_path / ".staging" / "scan-x-abcdef"
    staging.mkdir(parents=True)
    (staging / "owner.json").write_text("not json", encoding="utf-8")
    report = stg.reclaim_abandoned_staging(tmp_path)
    assert report.reclaimed == []
    assert "missing or malformed" in report.retained[0][1]
    assert staging.exists()


def test_reclaim_retains_a_directory_recorded_on_a_different_host(
    comprehension_dir: Path, comprehension_privacy: PrivacyPreflightResult, monkeypatch,
) -> None:
    lock = _lock(comprehension_dir, comprehension_privacy)
    monkeypatch.setattr(stg, "host_identity", lambda: "host-a")
    handle = stg.create_staging_dir(scan_id="scan-1", lock_handle=lock)
    monkeypatch.setattr(stg, "host_identity", lambda: "host-b")
    report = stg.reclaim_abandoned_staging(comprehension_dir)
    assert report.reclaimed == []
    assert "different host" in report.retained[0][1]
    assert handle.path.exists()
    lockmod.release_scan_lock(lock)


def test_reclaim_retains_a_directory_with_unknown_liveness(
    comprehension_dir: Path, comprehension_privacy: PrivacyPreflightResult, monkeypatch,
) -> None:
    lock = _lock(comprehension_dir, comprehension_privacy)
    handle = stg.create_staging_dir(scan_id="scan-1", lock_handle=lock)
    monkeypatch.setattr(stg, "process_observation", lambda pid: ("unknown", None))
    report = stg.reclaim_abandoned_staging(comprehension_dir)
    assert report.reclaimed == []
    assert "could not be observed" in report.retained[0][1]
    assert handle.path.exists()
    lockmod.release_scan_lock(lock)


def test_reclaim_retains_a_non_directory_entry(tmp_path: Path) -> None:
    staging_root = tmp_path / ".staging"
    staging_root.mkdir()
    (staging_root / "stray-file").write_text("x", encoding="utf-8")
    report = stg.reclaim_abandoned_staging(tmp_path)
    assert report.reclaimed == []
    assert report.retained == [("stray-file", "not a directory")]


def test_reclaim_never_touches_runs(
    comprehension_dir: Path, comprehension_privacy: PrivacyPreflightResult, monkeypatch,
) -> None:
    """Reclaim is scoped to .staging/ only — a same-named directory under
    runs/ (published, immutable project memory) must never be inspected or
    removed."""
    runs_scan_dir = comprehension_dir / "runs" / "scan-1-abcdef"
    runs_scan_dir.mkdir(parents=True)
    (runs_scan_dir / "scan.json").write_text("{}", encoding="utf-8")
    lock = _lock(comprehension_dir, comprehension_privacy)
    handle = stg.create_staging_dir(scan_id="scan-1", lock_handle=lock)
    monkeypatch.setattr(stg, "process_observation", lambda pid: ("dead", None))
    stg.reclaim_abandoned_staging(comprehension_dir)
    assert not handle.path.exists()
    assert runs_scan_dir.exists()
    assert (runs_scan_dir / "scan.json").exists()
    lockmod.release_scan_lock(lock)


def test_reclaim_retains_an_entry_that_resolves_outside_staging(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    staging_root = tmp_path / ".staging"
    staging_root.mkdir()
    link = staging_root / "scan-1-abcdef"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        # CAVEAT (reviewer-3 F-3 on PR-A, rq-5bd5427ad64d): on Windows, an
        # unprivileged process can only create a symlink with Developer
        # Mode enabled (or SeCreateSymbolicLinkPrivilege granted) — without
        # it this SKIPS. The design lists symlink/root-escape as required
        # increment-1 evidence and treats Windows as a first-class
        # platform, so on a default Windows runner this guard is
        # UNVERIFIED here, not merely covered elsewhere. Fast-follow: run
        # CI's Windows job with Developer Mode (or an elevated runner).
        pytest.skip("symlink creation is not permitted in this environment")
    report = stg.reclaim_abandoned_staging(tmp_path)
    assert report.reclaimed == []
    assert outside.exists()


# ----------------------------------------------------------- prune_staging

def test_prune_staging_delegates_to_the_same_reclaim_logic(
    comprehension_dir: Path, comprehension_privacy: PrivacyPreflightResult, monkeypatch,
) -> None:
    lock = _lock(comprehension_dir, comprehension_privacy)
    handle = stg.create_staging_dir(scan_id="scan-1", lock_handle=lock)
    monkeypatch.setattr(stg, "process_observation", lambda pid: ("dead", None))
    report = stg.prune_staging(comprehension_dir)
    assert report.reclaimed == [handle.path.name]
    assert not handle.path.exists()
    lockmod.release_scan_lock(lock)
