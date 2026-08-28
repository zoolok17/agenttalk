"""#55 slice-1 PR-B item 9: end-to-end scan pipeline orchestration
(DESIGN-55-comprehension-plane.md, "Scan behavior"). Every test threads a
REAL PrivacyPreflightResult against a real git repo, per the same
discipline test_comprehension_lock.py established for PR-A.

The sanitized worker's OWN subprocess boundary is already covered by
test_comprehension_worker.py; these tests monkeypatch
scan_pipeline.worker.run_sanitized_worker to call worker.process_paths
in-process directly (same return type, same logic) so pipeline
orchestration is tested without depending on this dev host's ambient
`agenttalk` install being importable under a stripped, no-PYTHONPATH
subprocess environment.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agenttalk.comprehension import scan_pipeline
from agenttalk.comprehension import worker as workermod
from agenttalk.comprehension.ceilings import ArtifactLimitExceeded
from agenttalk.comprehension.errors import VcsPrivacyRefused


@pytest.fixture(autouse=True)
def _inprocess_worker(monkeypatch):
    monkeypatch.setattr(
        scan_pipeline.worker, "run_sanitized_worker",
        lambda root, relative_paths, **_kwargs: workermod.process_paths(root, relative_paths),
    )


def _init_git_repo(root: Path) -> None:
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)  # noqa: S603,S607  # nosec B603 B607
    subprocess.run(  # noqa: S603,S607  # nosec B603 B607
        ["git", "-C", str(root), "config", "user.email", "t@t"], check=True)
    subprocess.run(  # noqa: S603,S607  # nosec B603 B607
        ["git", "-C", str(root), "config", "user.name", "t"], check=True)
    (root / ".gitignore").write_text(".agenttalk/\n", encoding="utf-8")


def _write_sample_java_project(root: Path) -> None:
    app_dir = root / "src" / "main" / "java" / "p"
    app_dir.mkdir(parents=True)
    (app_dir / "App.java").write_text(
        "package p;\nclass App {\n  public static void main(String[] args) {}\n}\n",
        encoding="utf-8",
    )
    (root / "pom.xml").write_text(
        "<project><dependencies><dependency>"
        "<groupId>org.springframework</groupId><artifactId>spring-core</artifactId>"
        "</dependency></dependencies></project>",
        encoding="utf-8",
    )


@pytest.fixture()
def java_repo(tmp_path: Path) -> Path:
    _init_git_repo(tmp_path)
    _write_sample_java_project(tmp_path)
    return tmp_path


# ----------------------------------------------------------- run_scan

def test_run_scan_publishes_a_complete_run(java_repo: Path) -> None:
    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "complete"
    assert outcome.run_dir.is_dir()
    assert (outcome.run_dir / "modules.json").exists()
    assert (outcome.run_dir / "dependencies.json").exists()
    assert (outcome.run_dir / "features.json").exists()
    assert (outcome.run_dir / "readiness.json").exists()
    assert (outcome.run_dir / "scan.json").exists()


def test_run_scan_carries_the_pom_xml_build_edge_through_the_worker(java_repo: Path) -> None:
    """B-3 (reviewer-3, PR-B delta review): pom.xml's build edge must
    reach dependencies.json via the sanitized worker's own java_results
    channel - not a direct parent-process read of the file."""
    import json

    outcome = scan_pipeline.run_scan(java_repo)
    doc = json.loads((outcome.run_dir / "dependencies.json").read_text(encoding="utf-8"))
    build_edges = [e for e in doc["edges"] if e["relation"] == "build"]
    assert build_edges and build_edges[0]["target_external"] == "org.springframework:spring-core"


def test_run_scan_publishes_problems_json_and_it_reaches_the_report(
    java_repo: Path, monkeypatch,
) -> None:
    """B2 (cold-read, PR-B fix round 3): every problem record used to be
    computed then discarded - problems.json was never written, and a
    degraded run published no account of what degraded it. Force a
    problem by making the worker report one, and assert it survives all
    the way through to get_report()."""
    from agenttalk.comprehension import worker as workermod2

    real_run = workermod2.process_paths

    def _inject_a_problem(root, relative_paths, **_kwargs):
        result = real_run(root, relative_paths)
        result.problems.append(workermod2.WorkerProblem(
            reason_code="parse_failed", relative_path="src/main/java/p/App.java",
            detail="synthetic problem for the B2 regression test"))
        return result

    monkeypatch.setattr(scan_pipeline.worker, "run_sanitized_worker", _inject_a_problem)

    import json

    outcome = scan_pipeline.run_scan(java_repo)
    assert outcome.status == "degraded"
    assert (outcome.run_dir / "problems.json").exists()
    doc = json.loads((outcome.run_dir / "problems.json").read_text(encoding="utf-8"))
    assert doc["problems"] == [{
        "reason_code": "parse_failed", "path": "src/main/java/p/App.java",
        "detail": "synthetic problem for the B2 regression test",
    }]

    report = scan_pipeline.get_report(java_repo)
    assert report["problems"] == doc["problems"]
    assert report["counts"]["problems"] == 1


def test_run_scan_does_not_publish_owner_json_into_the_run(java_repo: Path) -> None:
    """M4 (cold-read, PR-B fix round 3): owner.json (host identity, PID,
    and the writer lock's own owner token) repeats the lock's identity
    for staging reclaim - it must never survive into the published,
    immutable run directory."""
    outcome = scan_pipeline.run_scan(java_repo)
    assert not (outcome.run_dir / "owner.json").exists()


def test_run_scan_refuses_an_empty_scope(tmp_path: Path) -> None:
    """M3 (cold-read, PR-B fix round 3): a scope with nothing addressable
    enumerated at all is a command error (wrong --root, or an over-broad
    exclusion policy), never a valid, publishable, complete zero-unit
    run. Privacy proof comes from ``.git/info/exclude`` (private git
    metadata, never tracked content) rather than a ``.gitignore`` file -
    the latter would itself be one enumerable file, defeating the "truly
    empty scope" scenario this test needs."""
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)  # noqa: S603,S607  # nosec B603 B607
    subprocess.run(  # noqa: S603,S607  # nosec B603 B607
        ["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(  # noqa: S603,S607  # nosec B603 B607
        ["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    (tmp_path / ".git" / "info" / "exclude").write_text(".agenttalk/\n", encoding="utf-8")

    with pytest.raises(scan_pipeline.ScanRefused, match="no files were enumerated"):
        scan_pipeline.run_scan(tmp_path)
    from agenttalk.comprehension import publish

    comp_dir = scan_pipeline.paths.comprehension_dir(tmp_path / ".agenttalk")
    doc, _digest = publish.read_current_index(comp_dir)
    assert doc is None  # no run published


# ----------------------------------------------------------- M1: read-path run-id confinement

def test_get_status_rejects_a_run_id_outside_the_runs_tree(java_repo: Path, tmp_path: Path) -> None:
    """M1 (cold-read, PR-B fix round 3): the write path validates and
    resolve-confines a scan_id under runs/ before it ever touches disk -
    every read path must do the same, so a caller-supplied --run value
    can never open a document sitting outside the published-runs tree."""
    scan_pipeline.run_scan(java_repo)
    outside = tmp_path / "outside-runs-tree"
    outside.mkdir()
    (outside / "scan.json").write_text("{}", encoding="utf-8")
    with pytest.raises(scan_pipeline.EnvelopeError):
        scan_pipeline.get_status(java_repo, run_id="../../outside-runs-tree")


def test_get_report_rejects_a_malformed_run_id(java_repo: Path) -> None:
    scan_pipeline.run_scan(java_repo)
    with pytest.raises(scan_pipeline.EnvelopeError):
        scan_pipeline.get_report(java_repo, run_id="../../../etc/passwd")


def test_validate_run_reports_invalid_for_a_malformed_run_id(java_repo: Path) -> None:
    """validate_run's own contract catches ComprehensionError (which
    EnvelopeError is a subclass of) and reports it via the return value
    rather than raising - M1's confinement still holds here: the
    malformed id is rejected before any document outside runs/ is ever
    opened, just surfaced as valid=False instead of a raised exception."""
    scan_pipeline.run_scan(java_repo)
    result = scan_pipeline.validate_run(java_repo, run_id="not/a/real/scan/id")
    assert result["valid"] is False


def test_run_scan_refuses_without_privacy_proof_and_writes_nothing(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    (tmp_path / ".gitignore").write_text("build/\n", encoding="utf-8")  # does NOT ignore .agenttalk/
    _write_sample_java_project(tmp_path)
    with pytest.raises(VcsPrivacyRefused):
        scan_pipeline.run_scan(tmp_path)
    assert not (tmp_path / ".agenttalk").exists()


def test_run_scan_with_acknowledge_but_no_work_id_refuses(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    (tmp_path / ".gitignore").write_text("build/\n", encoding="utf-8")
    _write_sample_java_project(tmp_path)
    with pytest.raises(scan_pipeline.ScanRefused, match="work-id"):
        scan_pipeline.run_scan(tmp_path, acknowledge_unignored=True)


def test_run_scan_with_acknowledge_and_work_id_proceeds(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    (tmp_path / ".gitignore").write_text("build/\n", encoding="utf-8")
    _write_sample_java_project(tmp_path)
    outcome = scan_pipeline.run_scan(
        tmp_path, acknowledge_unignored=True, work_id="migrate-app")
    assert outcome.status == "complete"


def test_a_second_scan_chains_the_predecessor_digest(java_repo: Path) -> None:
    first = scan_pipeline.run_scan(java_repo)
    (java_repo / "src" / "main" / "java" / "p" / "Other.java").write_text(
        "package p;\nclass Other {\n}\n", encoding="utf-8")
    second = scan_pipeline.run_scan(java_repo)
    assert second.index["predecessor_digest"] is not None
    assert second.index["latest_scan_id"] == second.scan_id
    assert second.scan_id != first.scan_id


def test_recover_stale_lock_flag_clears_an_existing_lock(java_repo: Path) -> None:
    from agenttalk.comprehension import lock as lockmod
    from agenttalk.comprehension import paths as pathsmod
    from agenttalk.comprehension import privacy as privacymod

    comp_dir = pathsmod.comprehension_dir(java_repo / ".agenttalk")
    privacy_result = privacymod.run_privacy_preflight(java_repo)
    stale = lockmod.acquire_scan_lock(comp_dir, privacy=privacy_result, predecessor_index_digest=None)
    assert stale.path.exists()

    outcome = scan_pipeline.run_scan(java_repo, recover_stale_lock=True)
    assert outcome.status == "complete"


# ----------------------------------------------------------- get_status

def test_get_status_reports_the_latest_scan(java_repo: Path) -> None:
    outcome = scan_pipeline.run_scan(java_repo)
    status = scan_pipeline.get_status(java_repo)
    assert status["latest_scan_id"] == outcome.scan_id
    assert status["status"] == "complete"
    assert status["freshness"]["state"] == "not_evaluated"


def test_get_status_before_any_scan_raises_not_scanned(tmp_path: Path) -> None:
    with pytest.raises(scan_pipeline.NotScanned):
        scan_pipeline.get_status(tmp_path)


# ----------------------------------------------------------- get_report

def test_get_report_returns_the_projection(java_repo: Path) -> None:
    scan_pipeline.run_scan(java_repo)
    report = scan_pipeline.get_report(java_repo)
    assert report["status"] == "complete"
    assert report["counts"]["units"] > 0
    assert any(f["label"] == "App" for f in report["features"])


def test_get_report_unit_filter_narrows_the_projection(java_repo: Path) -> None:
    scan_pipeline.run_scan(java_repo)
    full_report = scan_pipeline.get_report(java_repo)
    one_unit_id = full_report["units"][0]["unit_id"]
    filtered = scan_pipeline.get_report(java_repo, unit_id=one_unit_id)
    assert [u["unit_id"] for u in filtered["units"]] == [one_unit_id]


# ----------------------------------------------------------- validate_run

def test_validate_run_reports_valid_for_a_healthy_run(java_repo: Path) -> None:
    scan_pipeline.run_scan(java_repo)
    result = scan_pipeline.validate_run(java_repo)
    assert result["valid"] is True
    assert result["external_revalidation"] == {
        "performed": False, "reason_code": "no_external_evidence_pointers_this_slice",
    }


def test_validate_run_before_any_scan_raises_not_scanned(tmp_path: Path) -> None:
    with pytest.raises(scan_pipeline.NotScanned):
        scan_pipeline.validate_run(tmp_path)


# ----------------------------------------------------------- failure-path lock release (F-2)

def test_run_scan_failure_surfaces_the_original_error_even_if_release_also_fails(
    java_repo: Path, monkeypatch,
) -> None:
    """F-2 (reviewer-3, PR-B delta review): if the lock release ITSELF
    refuses while unwinding from an original failure, the ORIGINAL failure
    must still be what the caller sees - never silently replaced by the
    release refusal. The release refusal is attached as the cause, not
    substituted for the original exception."""
    from agenttalk.comprehension import lock as lockmod
    from agenttalk.comprehension import modules_artifact

    class _OriginalFailure(RuntimeError):
        pass

    class _ReleaseFailure(RuntimeError):
        pass

    def _boom_build_modules(*_args, **_kwargs):
        raise _OriginalFailure("original pipeline failure")

    def _boom_release(*_args, **_kwargs):
        raise _ReleaseFailure("release also refused")

    monkeypatch.setattr(modules_artifact, "build_modules", _boom_build_modules)
    monkeypatch.setattr(lockmod, "release_scan_lock", _boom_release)

    with pytest.raises(_OriginalFailure) as excinfo:
        scan_pipeline.run_scan(java_repo)
    assert isinstance(excinfo.value.__cause__, _ReleaseFailure)


# ----------------------------------------------------------- ceilings integration

def test_run_scan_refuses_and_publishes_no_run_when_a_ceiling_is_exceeded(
    java_repo: Path, monkeypatch,
) -> None:
    from agenttalk.comprehension import ceilings

    monkeypatch.setattr(ceilings, "PER_ARTIFACT_BYTES_MAX", 1)
    with pytest.raises(ArtifactLimitExceeded):
        scan_pipeline.run_scan(java_repo)
    from agenttalk.comprehension import publish

    comp_dir = scan_pipeline.paths.comprehension_dir(java_repo / ".agenttalk")
    doc, _digest = publish.read_current_index(comp_dir)
    assert doc is None  # no run published
