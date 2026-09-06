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
import subprocess  # nosec B404 - invokes the real `mklink /J` binary to build a Windows
                    # directory junction fixture; no shell, no untrusted input
import sys
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


def test_staging_handle_is_not_publicly_constructible() -> None:
    """reviewer-1 cold-read finding 2 on PR-A, round 2 (rq-6cc5560b62f6):
    per the lead's dispatch, make ``StagingHandle`` non-forgeable the same
    way ``PrivacyPreflightResult`` was in round 3 (module-private
    construction) — a publicly constructible handle previously let a
    caller name an arbitrary directory and have it accepted by
    publish-time checks that trusted the handle's claimed fields."""
    with pytest.raises(TypeError):
        stg.StagingHandle(path=Path("/somewhere"), scan_id="scan-1", owner_token="tok")


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


@pytest.mark.skipif(sys.platform != "win32", reason="Windows directory junctions only")
def test_a_junction_at_staging_itself_is_refused_not_followed(
    comprehension_dir: Path, comprehension_privacy: PrivacyPreflightResult, tmp_path: Path,
) -> None:
    """MICRO-ROUND 50 (Cluster 0, B1 BLOCKER, the worst finding of the
    arc): a reparse point placed AT ``.staging/`` itself (never a file
    beneath it) used to redirect the ENTIRE staging write outside the
    pinned store root, silently - ``resolve_under_root``'s old confinement
    check resolved ``root`` (here, ``.staging/``) FIRST, baking the
    redirection into its own idea of "root" before ever comparing anything
    against it, so the comparison could never catch what it was resolving
    away. Reproduced verbatim: ``.staging`` is a junction to an external
    directory before the very first ``create_staging_dir`` call ever
    runs. Must refuse outright, and nothing may land at the junction's
    real target - the whole point of the fix is that NO bytes ever reach
    an unpinned location, not merely that the caller is told about it
    afterward."""
    outside = tmp_path.parent / "outside-staging-junction-target"
    outside.mkdir(exist_ok=True)
    junction = comprehension_dir / ".staging"
    junction.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(  # noqa: S603,S607  # nosec B603 B607
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
        check=True, capture_output=True, text=True,
    )
    lock = _lock(comprehension_dir, comprehension_privacy)
    with pytest.raises(EnvelopeError, match="reparse point/junction"):
        stg.create_staging_dir(scan_id="scan-1", lock_handle=lock)
    assert list(outside.iterdir()) == []
    lockmod.release_scan_lock(lock)


def test_a_benign_staging_dir_with_no_junction_is_unaffected(
    comprehension_dir: Path, comprehension_privacy: PrivacyPreflightResult,
) -> None:
    """MICRO-ROUND 50 (Cluster 0, benign control): the round-50 reparse-
    point check must never fire for an ORDINARY ``.staging/`` directory -
    same assertions as the pre-existing
    ``test_create_staging_dir_creates_directory_and_owner_json``, kept
    here as the paired control for the junction test immediately above so
    the two live side by side."""
    lock = _lock(comprehension_dir, comprehension_privacy)
    handle = stg.create_staging_dir(scan_id="scan-1", lock_handle=lock)
    assert handle.path.is_dir()
    assert handle.path.parent == comprehension_dir / ".staging"
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


def test_reclaim_raises_a_named_refusal_when_the_directory_cannot_be_deleted(
    comprehension_dir: Path, comprehension_privacy: PrivacyPreflightResult, monkeypatch,
) -> None:
    """MICRO-ROUND 50 (Cluster 5, the staging brick BLOCKER): a
    definitely-dead owner's own staging directory that cannot actually be
    deleted (measured: a raw PermissionError [WinError 5] on Windows, a
    file still held open) used to propagate a raw, unhandled OSError.
    Converted to a named StagingReclaimFailed refusal carrying the OS's
    own bounded, path-free reason plus a concrete remedy - never a raw
    traceback naming an absolute local path."""
    lock = _lock(comprehension_dir, comprehension_privacy)
    handle = stg.create_staging_dir(scan_id="scan-1", lock_handle=lock)
    monkeypatch.setattr(stg, "process_observation", lambda pid: ("dead", None))
    monkeypatch.setattr(
        stg.shutil, "rmtree",
        lambda path, ignore_errors=False: (_ for _ in ()).throw(
            PermissionError(5, "Access is denied")),
    )
    with pytest.raises(stg.StagingReclaimFailed, match="Access is denied"):
        stg.reclaim_abandoned_staging(comprehension_dir)
    assert handle.path.exists()  # never removed - the delete itself failed
    lockmod.release_scan_lock(lock)


def test_reclaim_treats_a_concurrently_removed_directory_as_already_reclaimed(
    comprehension_dir: Path, comprehension_privacy: PrivacyPreflightResult, monkeypatch,
) -> None:
    """MICRO-ROUND 50 (Cluster 5, concurrent prune): a FileNotFoundError
    from shutil.rmtree is a genuinely BENIGN race - this same function
    runs at every lock acquisition AND from the explicit
    ``prune --staging`` command, so an overlapping call (an automatic
    reclaim racing an operator's manual prune, or two manual prunes) can
    both select the identical dead-owner directory; by the time THIS
    call's own rmtree runs, the other one already removed it. The
    outcome this call wanted ("this directory no longer exists") is
    already true - counted as reclaimed, never raised as a failure."""
    lock = _lock(comprehension_dir, comprehension_privacy)
    handle = stg.create_staging_dir(scan_id="scan-1", lock_handle=lock)
    monkeypatch.setattr(stg, "process_observation", lambda pid: ("dead", None))
    monkeypatch.setattr(
        stg.shutil, "rmtree",
        lambda path, ignore_errors=False: (_ for _ in ()).throw(
            FileNotFoundError(2, "No such file or directory")),
    )
    report = stg.reclaim_abandoned_staging(comprehension_dir)
    assert report.reclaimed == [handle.path.name]
    assert report.retained == []
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
        # it this SKIPS. C-1 / #213 (PR-B fix round): conftest.py's
        # session-scoped `_enable_windows_symlink_creation_without_elevation`
        # fixture now enables Developer Mode on hosted Windows CI runners
        # (which run elevated already), so this executes there instead of
        # skipping. This branch only still fires on a genuinely
        # non-elevated local dev machine, where that fixture's registry
        # write itself fails silently and this remains a graceful local
        # skip.
        pytest.skip("symlink creation is not permitted in this environment")
    report = stg.reclaim_abandoned_staging(tmp_path)
    assert report.reclaimed == []
    assert outside.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows directory junctions only")
def test_reclaim_never_deletes_through_a_junctioned_staging_root(
    tmp_path: Path, monkeypatch,
) -> None:
    """MICRO-ROUND 50 (Cluster 0, B1-adjacent): the OLD check resolved
    ``staging_root`` first (the same vacuous-by-construction pattern the
    write path had) - a dead-owner-shaped directory found by iterating
    THROUGH a junctioned ``.staging/`` could reach ``shutil.rmtree`` on
    content entirely outside the pinned store. Every entry now retains
    whenever ``staging_root`` itself is a reparse point, so nothing under
    the junction's real target is ever deleted - forced DOWN the "would
    reclaim" path with a real, matching host identity and a monkeypatched
    ``process_observation`` reporting definitely dead (the same technique
    ``test_reclaim_removes_a_definitely_dead_owners_staging_dir`` uses),
    so a passing test here is attributable to the round-50 junction guard
    and not merely an unrelated host/liveness mismatch."""
    outside = tmp_path.parent / "outside-staging-reclaim-junction-target"
    outside.mkdir(exist_ok=True)
    dead_looking = outside / "scan-1-abcdef"
    dead_looking.mkdir(exist_ok=True)
    (dead_looking / "owner.json").write_text(
        json.dumps({
            "schema_version": 1, "scan_id": "scan-1", "owner_token": "tok",
            "pid": 99999999, "host_identity": stg.host_identity(),
            "created_at": "2020-01-01T00:00:00Z",
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(stg, "process_observation", lambda pid: ("dead", None))
    junction = tmp_path / ".staging"
    subprocess.run(  # noqa: S603,S607  # nosec B603 B607
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
        check=True, capture_output=True, text=True,
    )
    report = stg.reclaim_abandoned_staging(tmp_path)
    assert report.reclaimed == []
    assert dead_looking.exists()


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
