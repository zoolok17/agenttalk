from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


CANDIDATE_SHA = "a" * 40
CANDIDATE_TREE = "b" * 40
VERSION = "0.82.0"
PREFIX = "release-candidate-101-1"
RUN_ID = 101
RUN_ATTEMPT = 1
WORKFLOW_REF = "owner/agenttalk/.github/workflows/release-provenance.yml@refs/heads/master"
BASE = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
LEGS = ("linux/3.12", "windows/3.10")


@pytest.fixture(scope="module")
def provenance() -> Any:
    path = Path(".github/scripts/prepare_release_provenance.py").resolve()
    name = "_test_prepare_release_provenance"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class _FakeGateContext:
    def __init__(self, candidate_sha: str = CANDIDATE_SHA) -> None:
        self.binding = SimpleNamespace(candidate_sha=candidate_sha, candidate_tree=CANDIDATE_TREE)
        self.manifest: dict[str, Any] = {}
        self.expected_legs = LEGS
        self.canonical_leg = "linux/3.12"

    @staticmethod
    def validate_run(record: dict[str, Any]) -> dict[str, Any]:
        return record

    @staticmethod
    def validate_aggregate(record: dict[str, Any]) -> dict[str, Any]:
        return record


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode()


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _stamp(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _prepare_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provenance: Any,
    *,
    finished_at: datetime = BASE + timedelta(minutes=2),
    preflight_at: datetime = BASE,
    receipt_at: datetime = BASE + timedelta(minutes=3),
    candidate_sha: str = CANDIDATE_SHA,
) -> dict[str, Any]:
    runner_temp = tmp_path / "runner"
    runner_temp.mkdir()
    repo_root = runner_temp / "repo"
    repo_root.mkdir()
    source_dist = runner_temp / "gate-run" / "dist"
    source_dist.mkdir(parents=True)
    sdist_name = f"agenttalk-{VERSION}.tar.gz"
    wheel_name = f"agenttalk-{VERSION}-py3-none-any.whl"
    package_bytes = {
        "sdist": b"exact gate-built sdist bytes\x00\x01",
        "wheel": b"exact gate-built wheel bytes\x02\x03",
    }
    source_paths = {
        "sdist": source_dist / sdist_name,
        "wheel": source_dist / wheel_name,
    }
    for kind, path in source_paths.items():
        path.write_bytes(package_bytes[kind])

    subject = {
        "candidate_sha": candidate_sha,
        "candidate_tree": CANDIDATE_TREE,
        "version": VERSION,
    }
    records: dict[str, dict[str, Any]] = {}
    raw_records: dict[str, bytes] = {}
    for leg in LEGS:
        record: dict[str, Any] = {
            "subject": subject,
            "ci_leg": leg,
            "run_id": f"run-{leg.replace('/', '-')}",
            "finished_at": _stamp(finished_at),
            "verdict": "pass",
            "runner": {"os": leg.split("/", 1)[0]},
            "external_inputs": [],
            "artifacts": {},
        }
        if leg == "linux/3.12":
            record["artifacts"] = {
                kind: {
                    "path": str(source_paths[kind].resolve()),
                    "filename": path.name,
                    "size_bytes": len(package_bytes[kind]),
                    "sha256": _sha256(package_bytes[kind]),
                }
                for kind, path in source_paths.items()
            }
        records[leg] = record
        raw_records[leg] = _json_bytes(record)

    context = _FakeGateContext(candidate_sha)
    monkeypatch.setattr(provenance, "_gate_context", lambda _root: context)
    aggregate = {
        "subject": subject,
        "finished_at": _stamp(finished_at),
        "verdict": "pass",
        "complete": True,
        "blockers": [],
        "manifest": {"sha256": "c" * 64},
        "legs": [
            {
                "ci_leg": leg,
                "run_id": records[leg]["run_id"],
                "verdict": "pass",
                "artifact_sha256": _sha256(raw_records[leg]),
            }
            for leg in LEGS
        ],
    }
    aggregate_dir = runner_temp / "aggregate"
    aggregate_dir.mkdir()
    aggregate_path = aggregate_dir / "dev-gate-aggregate.json"
    aggregate_path.write_bytes(_json_bytes(aggregate))
    provenance.write_gate_receipt(
        repo_root=repo_root,
        aggregate_path=aggregate_path,
        output_path=aggregate_dir / "release-gate-receipt.json",
        candidate_sha=candidate_sha,
        version=VERSION,
        artifact_prefix=PREFIX,
        repository="owner/agenttalk",
        workflow_ref=WORKFLOW_REF,
        workflow_sha=candidate_sha,
        actor="operator",
        run_id=RUN_ID,
        run_attempt=RUN_ATTEMPT,
        preflight_at=_stamp(preflight_at),
        created_at=receipt_at,
    )

    legs_dir = runner_temp / "legs"
    legs_dir.mkdir()
    for leg, raw in raw_records.items():
        artifact_dir = legs_dir / f"{PREFIX}-leg-{leg.replace('/', '-')}"
        artifact_dir.mkdir()
        (artifact_dir / "dev-gate-evidence.json").write_bytes(raw)

    packages_dir = runner_temp / "packages"
    provenance.export_canonical_packages(
        repo_root=repo_root,
        raw_evidence_path=legs_dir / f"{PREFIX}-leg-linux-3.12" / "dev-gate-evidence.json",
        runner_temp=runner_temp,
        output_dir=packages_dir,
        candidate_sha=candidate_sha,
        version=VERSION,
    )
    return {
        "runner_temp": runner_temp,
        "repo_root": repo_root,
        "aggregate_dir": aggregate_dir,
        "legs_dir": legs_dir,
        "packages_dir": packages_dir,
        "output_dir": runner_temp / "provenance",
        "preflight_at": preflight_at,
        "raw_records": raw_records,
        "package_bytes": package_bytes,
        "package_names": {"sdist": sdist_name, "wheel": wheel_name},
    }


def _assemble(provenance: Any, bundle: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    arguments = {
        "repo_root": bundle["repo_root"],
        "aggregate_dir": bundle["aggregate_dir"],
        "legs_dir": bundle["legs_dir"],
        "packages_dir": bundle["packages_dir"],
        "runner_temp": bundle["runner_temp"],
        "output_dir": bundle["output_dir"],
        "candidate_sha": CANDIDATE_SHA,
        "version": VERSION,
        "artifact_prefix": PREFIX,
        "repository": "owner/agenttalk",
        "workflow_ref": WORKFLOW_REF,
        "workflow_sha": CANDIDATE_SHA,
        "actor": "operator",
        "event_name": "workflow_dispatch",
        "run_id": RUN_ID,
        "run_attempt": RUN_ATTEMPT,
        "run_url": f"https://github.com/owner/agenttalk/actions/runs/{RUN_ID}/attempts/1",
        "preflight_at": _stamp(bundle["preflight_at"]),
        "codeql_result": "success",
        "now": BASE + timedelta(minutes=4),
    }
    arguments.update(overrides)
    return provenance.assemble_provenance_bundle(**arguments)


def test_exact_gate_built_bytes_and_every_evidence_record_survive_custody(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provenance: Any,
) -> None:
    bundle = _prepare_bundle(tmp_path, monkeypatch, provenance)

    record = _assemble(provenance, bundle)

    assert record["subject"]["candidate_sha"] == CANDIDATE_SHA
    assert record["retention"] == {
        "carrier": "github-actions-artifact",
        "requested_days": 90,
        "permanent": False,
        "warning": "repository policy or run deletion may shorten availability",
        "next_increment": "attach these exact bytes to the GitHub Release before expiry",
    }
    for kind, filename in bundle["package_names"].items():
        assert (bundle["output_dir"] / filename).read_bytes() == bundle["package_bytes"][kind]
        assert record["packages"][kind]["sha256"] == _sha256(bundle["package_bytes"][kind])
    for leg, raw in bundle["raw_records"].items():
        durable = bundle["output_dir"] / "gate-evidence" / f"dev-gate-leg-{leg.replace('/', '-')}.json"
        assert durable.read_bytes() == raw
    assert (bundle["output_dir"] / "gate-evidence" / "dev-gate-aggregate.json").read_bytes() == (
        bundle["aggregate_dir"] / "dev-gate-aggregate.json"
    ).read_bytes()
    assert (bundle["output_dir"] / "gate-evidence" / "release-gate-receipt.json").read_bytes() == (
        bundle["aggregate_dir"] / "release-gate-receipt.json"
    ).read_bytes()

    rows = (bundle["output_dir"] / "SHA256SUMS").read_text(encoding="ascii").splitlines()
    listed = {row.split("  ", 1)[1]: row.split("  ", 1)[0] for row in rows}
    expected_members = {
        path.relative_to(bundle["output_dir"]).as_posix(): _sha256(path.read_bytes())
        for path in bundle["output_dir"].rglob("*")
        if path.is_file() and path.name != "SHA256SUMS"
    }
    assert listed == expected_members


def test_missing_evidence_has_its_own_refusal_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provenance: Any,
) -> None:
    bundle = _prepare_bundle(tmp_path, monkeypatch, provenance)
    missing_dir = bundle["legs_dir"] / f"{PREFIX}-leg-windows-3.10"
    (missing_dir / "dev-gate-evidence.json").unlink()
    missing_dir.rmdir()

    with pytest.raises(provenance.ReleaseEvidenceError) as caught:
        _assemble(provenance, bundle)

    assert caught.value.codes == (provenance.MISSING,)


@pytest.mark.parametrize(
    ("age", "passes"),
    [(timedelta(hours=24), True), (timedelta(hours=24, seconds=1), False)],
)
def test_evidence_freshness_boundary_is_exact_and_stale_is_distinct(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provenance: Any,
    age: timedelta,
    passes: bool,
) -> None:
    finished = BASE
    bundle = _prepare_bundle(
        tmp_path,
        monkeypatch,
        provenance,
        finished_at=finished,
        preflight_at=finished,
        receipt_at=finished + age,
    )

    if passes:
        _assemble(provenance, bundle, now=finished + age)
    else:
        with pytest.raises(provenance.ReleaseEvidenceError) as caught:
            _assemble(provenance, bundle, now=finished + age)
        assert caught.value.codes == (provenance.STALE,)


def test_different_sha_has_its_own_refusal_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provenance: Any,
) -> None:
    other_sha = "d" * 40
    bundle = _prepare_bundle(tmp_path, monkeypatch, provenance, candidate_sha=other_sha)
    receipt_path = bundle["aggregate_dir"] / "release-gate-receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["github"]["workflow_sha"] = CANDIDATE_SHA
    receipt_path.write_bytes(_json_bytes(receipt))

    with pytest.raises(provenance.ReleaseEvidenceError) as caught:
        _assemble(provenance, bundle)

    assert caught.value.codes == (provenance.SHA_MISMATCH,)


def test_same_size_package_substitution_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provenance: Any,
) -> None:
    bundle = _prepare_bundle(tmp_path, monkeypatch, provenance)
    wheel = bundle["packages_dir"] / bundle["package_names"]["wheel"]
    original = wheel.read_bytes()
    wheel.write_bytes(bytes([original[0] ^ 1]) + original[1:])

    with pytest.raises(provenance.ReleaseEvidenceError) as caught:
        _assemble(provenance, bundle)

    assert caught.value.codes == (provenance.DIGEST_MISMATCH,)


def test_corrupt_receipt_schema_is_not_accepted_as_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provenance: Any,
) -> None:
    bundle = _prepare_bundle(tmp_path, monkeypatch, provenance)
    receipt_path = bundle["aggregate_dir"] / "release-gate-receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["schema_version"] = 999
    receipt_path.write_bytes(_json_bytes(receipt))

    with pytest.raises(provenance.ReleaseEvidenceError) as caught:
        _assemble(provenance, bundle)

    assert caught.value.codes == (provenance.CORRUPT,)


def test_stale_run_attempt_receipt_is_not_collapsed_into_missing_or_sha_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provenance: Any,
) -> None:
    bundle = _prepare_bundle(tmp_path, monkeypatch, provenance)
    receipt_path = bundle["aggregate_dir"] / "release-gate-receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["github"]["run_attempt"] = 2
    receipt_path.write_bytes(_json_bytes(receipt))

    with pytest.raises(provenance.ReleaseEvidenceError) as caught:
        _assemble(provenance, bundle)

    assert caught.value.codes == (provenance.STALE,)


def test_receipt_bound_to_another_workflow_sha_is_sha_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provenance: Any,
) -> None:
    bundle = _prepare_bundle(tmp_path, monkeypatch, provenance)
    receipt_path = bundle["aggregate_dir"] / "release-gate-receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["github"]["workflow_sha"] = "d" * 40
    receipt_path.write_bytes(_json_bytes(receipt))

    with pytest.raises(provenance.ReleaseEvidenceError) as caught:
        _assemble(provenance, bundle)

    assert caught.value.codes == (provenance.SHA_MISMATCH,)


def test_oversized_json_integer_is_a_typed_corrupt_refusal(tmp_path: Path, provenance: Any) -> None:
    evidence = tmp_path / "evidence.json"
    evidence.write_text('{"value": ' + "9" * 5000 + "}\n", encoding="utf-8")

    with pytest.raises(provenance.ReleaseEvidenceError) as caught:
        provenance._load_json(evidence, "test evidence")

    assert caught.value.codes == (provenance.CORRUPT,)


def _write_release_surfaces(
    root: Path,
    *,
    readme_version: str = VERSION,
    onboarding_version: str = VERSION,
) -> None:
    (root / "src" / "agenttalk").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "agenttalk"\nversion = "{VERSION}"\ndependencies = []\n',
        encoding="utf-8",
    )
    (root / "src" / "agenttalk" / "__init__.py").write_text(
        f'__version__ = "{VERSION}"\n', encoding="utf-8"
    )
    (root / "CHANGELOG.md").write_text(f"## [{VERSION}] - 2026-08-07\n", encoding="utf-8")
    (root / "README.md").write_text(
        f"pip install git+https://github.com/owner/agenttalk.git@v{readme_version}\n", encoding="utf-8"
    )
    (root / "docs" / "USER-MANUAL.md").write_text(
        f"pip install git+https://github.com/owner/agenttalk.git@v{VERSION}\n", encoding="utf-8"
    )
    (root / "docs" / "AGENTTALK-NEW-USER-MANUAL.md").write_text(
        "Last updated: 2026-08-07. "
        f"Current release baseline: v{onboarding_version}.\n"
        f"pip install git+https://github.com/owner/agenttalk.git@v{onboarding_version}\n",
        encoding="utf-8",
    )
    (root / "docs" / "AGENTTALK-NEW-USER-MANUAL.pdf").write_bytes(
        b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\nstartxref\n9\n%%EOF\n"
    )
    (root / "docs" / "ROADMAP.md").write_text(
        f"**Current shipped baseline:** v{VERSION}\n", encoding="utf-8"
    )
    (root / "docs" / "ASSURANCE.md").write_text(f"### v{VERSION}\n", encoding="utf-8")


def test_preflight_accepts_only_a_clean_monotonic_post_bump_master_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provenance: Any,
) -> None:
    repo = tmp_path / "candidate"
    repo.mkdir()
    _write_release_surfaces(repo)

    def fake_git(_root: Path, *args: str) -> str:
        return {
            ("rev-parse", "HEAD"): CANDIDATE_SHA,
            ("status", "--porcelain=v1", "--untracked-files=all"): "",
            ("tag", "--list", "v*"): "v0.81.0\n",
            (
                "diff",
                "--name-only",
                "v0.81.0..HEAD",
                "--",
                "docs/AGENTTALK-NEW-USER-MANUAL.pdf",
            ): "docs/AGENTTALK-NEW-USER-MANUAL.pdf\n",
        }[args]

    monkeypatch.setattr(provenance, "_git", fake_git)

    provenance.validate_preflight(
        repo_root=repo,
        candidate_sha=CANDIDATE_SHA,
        event_sha=CANDIDATE_SHA,
        event_ref="refs/heads/master",
        workflow_sha=CANDIDATE_SHA,
        version=VERSION,
        run_attempt=1,
    )


def test_preflight_refuses_a_stale_install_pin_with_version_specific_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provenance: Any,
) -> None:
    repo = tmp_path / "candidate"
    repo.mkdir()
    _write_release_surfaces(repo, readme_version="0.80.0")
    monkeypatch.setattr(
        provenance,
        "_git",
        lambda _root, *args: {
            ("rev-parse", "HEAD"): CANDIDATE_SHA,
            ("status", "--porcelain=v1", "--untracked-files=all"): "",
            ("tag", "--list", "v*"): "v0.81.0\n",
            (
                "diff",
                "--name-only",
                "v0.81.0..HEAD",
                "--",
                "docs/AGENTTALK-NEW-USER-MANUAL.pdf",
            ): "docs/AGENTTALK-NEW-USER-MANUAL.pdf\n",
        }[args],
    )

    with pytest.raises(provenance.ReleaseEvidenceError) as caught:
        provenance.validate_preflight(
            repo_root=repo,
            candidate_sha=CANDIDATE_SHA,
            event_sha=CANDIDATE_SHA,
            event_ref="refs/heads/master",
            workflow_sha=CANDIDATE_SHA,
            version=VERSION,
            run_attempt=1,
        )

    assert caught.value.codes == (provenance.VERSION_MISMATCH,)


def test_preflight_refuses_partial_rerun_as_stale_without_reading_candidate(
    tmp_path: Path,
    provenance: Any,
) -> None:
    with pytest.raises(provenance.ReleaseEvidenceError) as caught:
        provenance.validate_preflight(
            repo_root=tmp_path,
            candidate_sha=CANDIDATE_SHA,
            event_sha=CANDIDATE_SHA,
            event_ref="refs/heads/master",
            workflow_sha=CANDIDATE_SHA,
            version=VERSION,
            run_attempt=2,
        )

    assert caught.value.codes == (provenance.STALE,)


@pytest.mark.parametrize("surface", ["pin", "baseline"])
def test_preflight_refuses_a_stale_onboarding_manual_surface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provenance: Any,
    surface: str,
) -> None:
    repo = tmp_path / "candidate"
    repo.mkdir()
    _write_release_surfaces(repo, onboarding_version="0.80.0")
    if surface == "pin":
        manual = repo / "docs" / "AGENTTALK-NEW-USER-MANUAL.md"
        manual.write_text(
            f"Current release baseline: v{VERSION}.\n"
            "pip install git+https://github.com/owner/agenttalk.git@v0.80.0\n",
            encoding="utf-8",
        )
    monkeypatch.setattr(
        provenance,
        "_git",
        lambda _root, *args: {
            ("rev-parse", "HEAD"): CANDIDATE_SHA,
            ("status", "--porcelain=v1", "--untracked-files=all"): "",
            ("tag", "--list", "v*"): "v0.81.0\n",
        }[args],
    )

    with pytest.raises(provenance.ReleaseEvidenceError) as caught:
        provenance.validate_preflight(
            repo_root=repo,
            candidate_sha=CANDIDATE_SHA,
            event_sha=CANDIDATE_SHA,
            event_ref="refs/heads/master",
            workflow_sha=CANDIDATE_SHA,
            version=VERSION,
        )

    assert caught.value.codes == (provenance.VERSION_MISMATCH,)


def test_preflight_refuses_an_onboarding_pdf_not_regenerated_since_the_latest_tag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provenance: Any,
) -> None:
    repo = tmp_path / "candidate"
    repo.mkdir()
    _write_release_surfaces(repo)
    monkeypatch.setattr(
        provenance,
        "_git",
        lambda _root, *args: {
            ("rev-parse", "HEAD"): CANDIDATE_SHA,
            ("status", "--porcelain=v1", "--untracked-files=all"): "",
            ("tag", "--list", "v*"): "v0.81.0\n",
            (
                "diff",
                "--name-only",
                "v0.81.0..HEAD",
                "--",
                "docs/AGENTTALK-NEW-USER-MANUAL.pdf",
            ): "",
        }[args],
    )

    with pytest.raises(provenance.ReleaseEvidenceError) as caught:
        provenance.validate_preflight(
            repo_root=repo,
            candidate_sha=CANDIDATE_SHA,
            event_sha=CANDIDATE_SHA,
            event_ref="refs/heads/master",
            workflow_sha=CANDIDATE_SHA,
            version=VERSION,
        )

    assert caught.value.codes == (provenance.VERSION_MISMATCH,)


def test_preflight_refuses_changed_bytes_that_are_not_a_pdf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provenance: Any,
) -> None:
    repo = tmp_path / "candidate"
    repo.mkdir()
    _write_release_surfaces(repo)
    (repo / "docs" / "AGENTTALK-NEW-USER-MANUAL.pdf").write_bytes(b"changed, but not a PDF")
    monkeypatch.setattr(
        provenance,
        "_git",
        lambda _root, *args: {
            ("rev-parse", "HEAD"): CANDIDATE_SHA,
            ("status", "--porcelain=v1", "--untracked-files=all"): "",
            ("tag", "--list", "v*"): "v0.81.0\n",
        }[args],
    )

    with pytest.raises(provenance.ReleaseEvidenceError) as caught:
        provenance.validate_preflight(
            repo_root=repo,
            candidate_sha=CANDIDATE_SHA,
            event_sha=CANDIDATE_SHA,
            event_ref="refs/heads/master",
            workflow_sha=CANDIDATE_SHA,
            version=VERSION,
        )

    assert caught.value.codes == (provenance.VERSION_MISMATCH,)


def test_preflight_refuses_a_workflow_loaded_from_another_sha_before_git_access(
    tmp_path: Path,
    provenance: Any,
) -> None:
    with pytest.raises(provenance.ReleaseEvidenceError) as caught:
        provenance.validate_preflight(
            repo_root=tmp_path,
            candidate_sha=CANDIDATE_SHA,
            event_sha=CANDIDATE_SHA,
            event_ref="refs/heads/master",
            workflow_sha="d" * 40,
            version=VERSION,
        )

    assert caught.value.codes == (provenance.SHA_MISMATCH,)


def test_malformed_package_source_path_has_a_typed_corrupt_refusal(
    tmp_path: Path,
    provenance: Any,
) -> None:
    runner_temp = tmp_path / "runner"
    runner_temp.mkdir()

    with pytest.raises(provenance.ReleaseEvidenceError) as caught:
        provenance._copy_evidenced_package(
            source_text=str(runner_temp / "malformed\x00parent" / "agenttalk.whl"),
            destination=runner_temp / "copy.whl",
            runner_temp=runner_temp,
            expected_size=1,
            expected_digest="0" * 64,
            label="canonical wheel",
        )

    assert caught.value.codes == (provenance.CORRUPT,)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction guard")
def test_output_junction_is_refused_before_any_external_file_is_written(
    tmp_path: Path,
    provenance: Any,
) -> None:
    runner_temp = tmp_path / "runner"
    external = tmp_path / "external"
    runner_temp.mkdir()
    external.mkdir()
    output = runner_temp / "provenance"
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    assert powershell is not None, "PowerShell is required for the Windows junction contract"
    completed = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "& { param($link, $target) New-Item -ItemType Junction -Path $link -Target $target | Out-Null }",
            str(output),
            str(external),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr

    with pytest.raises(provenance.ReleaseEvidenceError) as caught:
        provenance._empty_output_directory(output, runner_temp=runner_temp)

    assert caught.value.codes == (provenance.CORRUPT,)
    assert not any(external.iterdir())
