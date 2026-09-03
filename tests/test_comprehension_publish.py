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
from agenttalk.comprehension import privacy as privacymod
from agenttalk.comprehension import publish as pub
from agenttalk.comprehension import staging as stg
from agenttalk.comprehension.errors import ComprehensionError, VcsPrivacyRefused
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


def test_publish_index_cas_refuses_a_prior_index_missing_runs_instead_of_crashing(
    tmp_path: Path,
) -> None:
    """MAJOR 2 (fifth cold read, fix round 8): the WRITE path
    (_build_successor_index) read a PRIOR index.json's own "runs" field
    with a raw, unguarded subscript - a malformed-but-envelope-valid
    prior index.json missing "runs" (envelope validation only requires
    schema_version/artifact_type/scan_id/generated_at, never index.json's
    OWN fields) raised an untyped KeyError in the middle of publishing a
    brand-new, otherwise-healthy scan, rather than the same typed
    refusal a malformed document gets everywhere else in this package."""
    index_path = tmp_path / "index.json"
    malformed = {
        "schema_version": pub.INDEX_SCHEMA_VERSION,
        "artifact_type": pub.INDEX_ARTIFACT_TYPE,
        "scan_id": "scan-0",
        "generated_at": "2026-01-01T00:00:00Z",
        "latest_scan_id": "scan-0",
        "predecessor_digest": None,
        # "runs" deliberately omitted.
    }
    index_path.write_text(json.dumps(malformed), encoding="utf-8")
    _doc, digest = pub.read_current_index(tmp_path)

    with pytest.raises(ComprehensionError, match="runs"):
        pub.publish_index_cas(
            tmp_path, scan_id="scan-1", run_summary={"scan_id": "scan-1"},
            predecessor_index_digest=digest,
        )


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


def test_rename_refuses_a_handle_whose_path_names_an_external_directory(
    comprehension_dir: Path, comprehension_privacy: PrivacyPreflightResult,
) -> None:
    """reviewer-1 cold-read finding 2 on PR-A, round 2 (rq-6cc5560b62f6),
    reproduced: a handle naming an EXTERNAL directory (never created by
    ``create_staging_dir``, outside ``.staging/`` entirely), carrying a
    COPIED real ``owner_token``, was previously accepted by the
    owner-token string comparison alone; the external directory was
    removed from its own location and its content published under
    ``runs/``. ``StagingHandle`` is no longer publicly constructible (see
    the test above), so this defense-in-depth path is exercised via a
    legitimately-issued handle whose frozen ``path`` field is tampered
    with post-construction — the confine-and-cross-check-against-disk fix
    must catch this regardless of how ``path`` came to disagree with
    where the handle was actually created."""
    lock = lockmod.acquire_scan_lock(
        comprehension_dir, privacy=comprehension_privacy, predecessor_index_digest=None)
    real = stg.create_staging_dir(scan_id="scan-1", lock_handle=lock)
    external = comprehension_dir.parent.parent / "outside-comprehension-entirely"
    external.mkdir(parents=True)
    (external / "scan.json").write_text("stolen content", encoding="utf-8")
    object.__setattr__(real, "path", external)

    with pytest.raises(pub.StagingSourceEscapesRoot):
        pub.rename_staging_to_run(real, lock)

    assert external.exists()  # never moved
    assert (external / "scan.json").read_text(encoding="utf-8") == "stolen content"
    assert not (comprehension_dir / "runs" / "scan-1").exists()
    lockmod.release_scan_lock(lock)


def test_rename_refuses_a_handle_whose_path_name_has_the_wrong_scan_id_nonce_shape(
    comprehension_dir: Path, comprehension_privacy: PrivacyPreflightResult,
) -> None:
    """A handle confined correctly under ``.staging/`` but whose directory
    name does not match the exact ``<scan_id>-<nonce>`` shape
    ``create_staging_dir`` names it with must also be refused — confinement
    alone does not prove the directory is what it claims to be."""
    lock = lockmod.acquire_scan_lock(
        comprehension_dir, privacy=comprehension_privacy, predecessor_index_digest=None)
    real = stg.create_staging_dir(scan_id="scan-1", lock_handle=lock)
    wrongly_named = real.path.parent / "scan-1-not-a-hex-nonce"
    real.path.rename(wrongly_named)
    object.__setattr__(real, "path", wrongly_named)

    with pytest.raises(pub.StagingSourceEscapesRoot, match="nonce"):
        pub.rename_staging_to_run(real, lock)

    assert wrongly_named.exists()  # never moved
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


# --------------------- FIX ROUND 34 (reviewer-3's re-delta on round 33's
# own R1 fix - THE HOLE): round 33's own ground-truth check only ever
# enumerated runs/<scan_id>/ - index.json (written by the OTHER publish
# step, at the store root) sat outside its reach. Replaced by ONE store-
# wide check (no enumeration anywhere) run after BOTH publish steps
# complete, with rollback of both the run directory AND index.json.

def test_publish_run_store_wide_check_passes_when_still_ignored(
    comprehension_dir: Path, comprehension_privacy: PrivacyPreflightResult,
) -> None:
    """Regression control: the store-wide check must not spuriously
    refuse an ordinary publish when the comprehension dir is still
    genuinely ignored exactly as the preflight proved."""
    lock, staging = _stage(comprehension_dir, comprehension_privacy, "scan-1")
    pub.publish_run(
        staging_handle=staging, lock_handle=lock,
        run_summary={"scan_id": "scan-1"}, predecessor_index_digest=None,
        record_counts=_COUNTS, privacy_result=comprehension_privacy,
    )
    assert (comprehension_dir / "runs" / "scan-1").is_dir()
    assert (comprehension_dir / "index.json").exists()


def test_publish_run_store_wide_check_refuses_and_rolls_back_mid_run_gitignore_removal(
    comprehension_privacy_root: Path, comprehension_dir: Path,
    comprehension_privacy: PrivacyPreflightResult,
) -> None:
    """The preflight proved ``.agenttalk/`` ignored at lock-acquisition
    time, but a ``.gitignore`` removal DURING the run (a TOCTOU-shaped gap)
    must still be caught - checked once, after both publish steps
    complete. On refusal, the just-published run directory AND index.json
    are both rolled back - nothing is left stageable anywhere in the
    store."""
    lock, staging = _stage(comprehension_dir, comprehension_privacy, "scan-1")
    (comprehension_privacy_root / ".gitignore").unlink()
    with pytest.raises(VcsPrivacyRefused):
        pub.publish_run(
            staging_handle=staging, lock_handle=lock,
            run_summary={"scan_id": "scan-1"}, predecessor_index_digest=None,
            record_counts=_COUNTS, privacy_result=comprehension_privacy,
        )
    assert not staging.path.exists()  # renamed away, never restored
    assert not (comprehension_dir / "runs" / "scan-1").exists()  # rolled back
    assert not (comprehension_dir / "index.json").exists()  # first-ever publish: removed, not restored
    assert not lock.path.exists()  # still released despite the refusal (design step 3)


def test_publish_run_store_wide_check_refuses_defeat_1_filename_specific_reinclusion(
    comprehension_privacy_root: Path, comprehension_dir: Path,
    comprehension_privacy: PrivacyPreflightResult,
) -> None:
    """Round 32's own DEFEAT 1 (reviewer-3's delta): a rule re-including a
    real artifact filename specifically ("modules.json") - the store-wide
    check asks git directly what is stageable, never enumerates a
    directory by hand, so it catches this the same way it catches
    everything else."""
    (comprehension_privacy_root / ".gitignore").write_text(
        ".agenttalk/**\n"
        "!.agenttalk/comprehension/\n"
        "!.agenttalk/comprehension/runs/\n"
        "!.agenttalk/comprehension/runs/scan-1/\n"
        "!.agenttalk/comprehension/runs/scan-1/modules.json\n",
        encoding="utf-8",
    )
    lock = lockmod.acquire_scan_lock(
        comprehension_dir, privacy=comprehension_privacy, predecessor_index_digest=None)
    staging = stg.create_staging_dir(scan_id="scan-1", lock_handle=lock)
    (staging.path / "scan.json").write_text("hello", encoding="utf-8")
    (staging.path / "modules.json").write_text("leaked", encoding="utf-8")
    with pytest.raises(VcsPrivacyRefused):
        pub.publish_run(
            staging_handle=staging, lock_handle=lock,
            run_summary={"scan_id": "scan-1"}, predecessor_index_digest=None,
            record_counts={"scan.json": 1, "modules.json": 1}, privacy_result=comprehension_privacy,
        )
    assert not (comprehension_dir / "runs" / "scan-1").exists()  # rolled back


def test_publish_run_store_wide_check_refuses_defeat_2_scan_id_shape_reinclusion(
    comprehension_privacy_root: Path, comprehension_dir: Path,
    comprehension_privacy: PrivacyPreflightResult,
) -> None:
    """Round 32's own DEFEAT 2 (reviewer-3's delta, THE WORST one): a rule
    keyed on the REAL scan-id's own shape (a date-prefixed pattern,
    ``runs/2026*/``) - the store-wide check has no probe id to defeat at
    all. Uses a realistic, date-shaped scan_id (unlike this file's own
    bare "scan-1" convention elsewhere) so the shape-keyed rule has
    something real to match against."""
    real_scan_id = "20260901T120000000000Z-a1b2c3d4"
    (comprehension_privacy_root / ".gitignore").write_text(
        ".agenttalk/**\n"
        "!.agenttalk/comprehension/\n"
        "!.agenttalk/comprehension/runs/\n"
        "!.agenttalk/comprehension/runs/2026*/\n"
        "!.agenttalk/comprehension/runs/2026*/**\n",
        encoding="utf-8",
    )
    lock = lockmod.acquire_scan_lock(
        comprehension_dir, privacy=comprehension_privacy, predecessor_index_digest=None)
    staging = stg.create_staging_dir(scan_id=real_scan_id, lock_handle=lock)
    (staging.path / "scan.json").write_text("hello", encoding="utf-8")
    with pytest.raises(VcsPrivacyRefused):
        pub.publish_run(
            staging_handle=staging, lock_handle=lock,
            run_summary={"scan_id": real_scan_id}, predecessor_index_digest=None,
            record_counts={"scan.json": 1}, privacy_result=comprehension_privacy,
        )
    assert not (comprehension_dir / "runs" / real_scan_id).exists()  # rolled back


def test_publish_run_store_wide_check_refuses_defeat_3_index_json_only_reinclusion(
    comprehension_privacy_root: Path, comprehension_dir: Path,
    comprehension_privacy: PrivacyPreflightResult,
) -> None:
    """THE HOLE round 34 itself closes: a rule re-including ONLY
    index.json (never touching runs/**) leaked it under round 33's own
    check, since that check only ever enumerated ``runs/<scan_id>/`` -
    index.json is written by the OTHER publish step, at the store root,
    never inside that directory. Asserts the FULL rollback contract: the
    run directory is removed, the PRE-write index.json bytes are restored
    byte-exact (there was already a published generation before this
    attempt), and a real ``git ls-files --others --exclude-standard``
    query against the whole store proves NOTHING is left stageable
    afterward - not just a disposition check, the actual ground truth."""
    import subprocess

    lock1, staging1 = _stage(comprehension_dir, comprehension_privacy, "scan-1")
    pub.publish_run(
        staging_handle=staging1, lock_handle=lock1,
        run_summary={"scan_id": "scan-1"}, predecessor_index_digest=None,
        record_counts=_COUNTS, privacy_result=comprehension_privacy,
    )
    prior_index_bytes = (comprehension_dir / "index.json").read_bytes()
    _doc, predecessor_digest = pub.read_current_index(comprehension_dir)

    (comprehension_privacy_root / ".gitignore").write_text(
        ".agenttalk/**\n"
        "!.agenttalk/comprehension/\n"
        "!.agenttalk/comprehension/index.json\n",
        encoding="utf-8",
    )
    lock2, staging2 = _stage(comprehension_dir, comprehension_privacy, "scan-2")
    with pytest.raises(VcsPrivacyRefused, match="index.json"):
        pub.publish_run(
            staging_handle=staging2, lock_handle=lock2,
            run_summary={"scan_id": "scan-2"}, predecessor_index_digest=predecessor_digest,
            record_counts=_COUNTS, privacy_result=comprehension_privacy,
        )
    assert not (comprehension_dir / "runs" / "scan-2").exists()  # rolled back
    assert (comprehension_dir / "runs" / "scan-1").exists()  # the OLD generation is untouched
    assert (comprehension_dir / "index.json").read_bytes() == prior_index_bytes  # byte-exact restore

    # The refused attempt's own residual exposure (never the operator's own
    # still-broken .gitignore, a separate real-world problem outside this
    # fix's scope) - restore the good rule and prove nothing is left over
    # from the rolled-back scan-2 attempt.
    (comprehension_privacy_root / ".gitignore").write_text(".agenttalk/\n", encoding="utf-8")
    result = subprocess.run(
        ["git", "-C", str(comprehension_privacy_root), "ls-files", "--others",
         "--exclude-standard", "--", ".agenttalk/comprehension"],
        capture_output=True, text=True, encoding="utf-8", check=True,
    )
    assert result.stdout.strip() == ""  # git add -A would stage nothing from the store


def test_publish_run_does_not_brick_when_only_scan_lock_is_stageable(
    comprehension_privacy_root: Path, comprehension_dir: Path,
    comprehension_privacy: PrivacyPreflightResult,
) -> None:
    """FIX ROUND 35 (twenty-ninth cold read, F2 MAJOR part (b), JUDGE -
    taken, .cr29-deadend verbatim): a .gitignore matching everything BUT
    scan.lock's own name (the scanner's own transient process-identity
    file, still on disk when the store-wide check runs, before publish_
    run's own finally releases it) must not brick an otherwise-genuinely-
    private publish - the lock is process metadata, never client data."""
    (comprehension_privacy_root / ".gitignore").write_text(
        ".agenttalk/**\n"
        "!.agenttalk/comprehension/\n"
        "!.agenttalk/comprehension/scan.lock\n",
        encoding="utf-8",
    )
    lock, staging = _stage(comprehension_dir, comprehension_privacy, "scan-1")
    pub.publish_run(
        staging_handle=staging, lock_handle=lock,
        run_summary={"scan_id": "scan-1"}, predecessor_index_digest=None,
        record_counts=_COUNTS, privacy_result=comprehension_privacy,
    )
    assert (comprehension_dir / "runs" / "scan-1").is_dir()


def test_publish_run_store_wide_check_refuses_on_an_unanticipated_new_file(
    comprehension_privacy_root: Path, comprehension_dir: Path,
    comprehension_privacy: PrivacyPreflightResult,
) -> None:
    """The class-closure proof: a file no enumeration ever anticipated
    (dropped directly into the store, never named by any probe or any
    prior fix) must still be caught - the store-wide check never
    enumerates anything, it asks git what is stageable, period."""
    (comprehension_privacy_root / ".gitignore").write_text(
        ".agenttalk/**\n"
        "!.agenttalk/comprehension/\n"
        "!.agenttalk/comprehension/a-file-nobody-named.txt\n",
        encoding="utf-8",
    )
    lock, staging = _stage(comprehension_dir, comprehension_privacy, "scan-1")
    (comprehension_dir / "a-file-nobody-named.txt").write_text("surprise", encoding="utf-8")
    with pytest.raises(VcsPrivacyRefused, match="a-file-nobody-named.txt"):
        pub.publish_run(
            staging_handle=staging, lock_handle=lock,
            run_summary={"scan_id": "scan-1"}, predecessor_index_digest=None,
            record_counts=_COUNTS, privacy_result=comprehension_privacy,
        )
    assert not (comprehension_dir / "runs" / "scan-1").exists()  # rolled back


def test_publish_run_store_wide_check_skipped_for_acknowledged_unignored_disposition(
    comprehension_privacy_root: Path, comprehension_dir: Path,
) -> None:
    """An operator who explicitly ACKNOWLEDGED an unignored store already
    accepted this exact risk for this one run - the store-wide check only
    applies to the automatic "ignored" disposition and must not
    spuriously re-refuse a publish that was never claiming "ignored" in
    the first place."""
    acknowledged = privacymod.acknowledge_unignored_private_store(
        comprehension_privacy_root, vcs_kind="git", work_id="w1")
    (comprehension_privacy_root / ".gitignore").unlink()  # genuinely unignored
    lock, staging = _stage(comprehension_dir, acknowledged, "scan-1")
    pub.publish_run(
        staging_handle=staging, lock_handle=lock,
        run_summary={"scan_id": "scan-1"}, predecessor_index_digest=None,
        record_counts=_COUNTS, privacy_result=acknowledged,
    )
    assert (comprehension_dir / "runs" / "scan-1").is_dir()


def test_rename_has_no_privacy_parameter_and_publish_run_still_works_without_one(
    comprehension_dir: Path, comprehension_privacy: PrivacyPreflightResult,
) -> None:
    """FIX ROUND 34: ``rename_staging_to_run`` no longer takes a
    ``privacy_result`` at all (the whole-store check lives solely in
    ``publish_run`` now) - tests exercising the rename mechanics alone
    still need construct nothing privacy-related."""
    lock, staging = _stage(comprehension_dir, comprehension_privacy, "scan-1")
    result = pub.rename_staging_to_run(staging, lock)
    assert result == comprehension_dir / "runs" / "scan-1"
    lockmod.release_scan_lock(lock)


def test_publish_run_store_wide_check_skipped_when_no_privacy_result_passed(
    comprehension_dir: Path, comprehension_privacy: PrivacyPreflightResult,
) -> None:
    """``privacy_result`` is optional (default ``None``) precisely so tests
    exercising ``publish_run`` alone need not construct one."""
    lock, staging = _stage(comprehension_dir, comprehension_privacy, "scan-1")
    pub.publish_run(
        staging_handle=staging, lock_handle=lock,
        run_summary={"scan_id": "scan-1"}, predecessor_index_digest=None,
        record_counts=_COUNTS,
    )
    assert (comprehension_dir / "runs" / "scan-1").is_dir()


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
    # M6 (cold-read PR-B fix round 47): the CAS digest is an EXACT-BYTE
    # digest of index.json's own on-disk bytes now, never
    # canonical_content_digest (which strips scan_id and would blind the
    # CAS to exactly the edit it exists to catch — see
    # test_read_current_index_digest_changes_when_only_a_stripped_
    # generation_identity_field_changes below).
    from agenttalk.comprehension.digests import sha256_file
    assert digest == sha256_file(comprehension_dir / "index.json")


def test_read_current_index_digest_changes_when_only_a_stripped_generation_identity_field_changes(
    comprehension_dir: Path, comprehension_privacy: PrivacyPreflightResult,
) -> None:
    """M6 (cold-read PR-B fix round 47, BLOCKER): the CAS precondition used
    to be ``canonical_content_digest(doc)``, which strips
    ``GENERATION_IDENTITY_KEYS`` (including ``scan_id``) at every nesting
    depth before hashing — so a concurrent hand-edit that changed ONLY a
    stored run entry's own ``scan_id`` (a stripped field, and simultaneously
    the CAS's own anchor/identity key) would leave the canonical digest
    unchanged and silently pass the CAS check, even though index.json's
    bytes genuinely differ. Reproduces the exact mechanism: publish once,
    capture the digest, hand-edit only the stored run entry's ``scan_id``
    on disk, and prove the digest now DOES change (byte-level, never blind
    to a stripped-field-only edit)."""
    lock, staging = _stage(comprehension_dir, comprehension_privacy, "scan-1")
    pub.publish_run(
        staging_handle=staging, lock_handle=lock,
        run_summary={"scan_id": "scan-1"}, predecessor_index_digest=None, record_counts=_COUNTS,
    )
    _doc, digest_before = pub.read_current_index(comprehension_dir)
    index_path = comprehension_dir / "index.json"
    doc = json.loads(index_path.read_text(encoding="utf-8"))
    doc["runs"][0]["scan_id"] = "scan-1-tampered"
    index_path.write_text(
        json.dumps(doc, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    _doc, digest_after = pub.read_current_index(comprehension_dir)
    assert digest_after != digest_before
    with pytest.raises(pub.IndexCasConflict):
        pub.publish_index_cas(
            comprehension_dir, scan_id="scan-2", run_summary={"scan_id": "scan-2"},
            predecessor_index_digest=digest_before,
        )


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
