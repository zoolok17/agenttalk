from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from agenttalk import assurance


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _manifest(root: Path, data: dict | None = None) -> Path:
    payload = {"schema_version": 1}
    if data:
        payload.update(data)
    path = root / ".agenttalk" / "assurance.json"
    _write_json(path, payload)
    return path


def _baseline(root: Path, data: dict | None = None) -> Path:
    payload = {"schema_version": 1, "baseline_id": "test", "findings": []}
    if data:
        payload.update(data)
    path = root / ".agenttalk" / "assurance" / "baseline.json"
    _write_json(path, payload)
    return path


def _make_python_project(root: Path) -> None:
    (root / "pyproject.toml").write_text("[project]\nname = 'samplepkg'\n", encoding="utf-8")
    pkg = root / "src" / "samplepkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")


def _run_scan(root: Path, profile: str = "change") -> assurance.ScanResult:
    manifest = assurance.load_manifest(root)
    baseline = assurance.load_baseline(root)
    detection = assurance.detect_project(root, manifest)
    provenance = assurance.collect_provenance(root, manifest, profile, baseline)
    plan = assurance.build_plan(root, profile, manifest, detection, baseline, provenance)
    result = assurance.run_plan(plan)
    return assurance.apply_baseline(result, baseline, manifest, provenance["changed_files"])


def _fake_tool(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def _git(root: Path, *args: str) -> str:
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not installed")
    completed = subprocess.run(
        [git, *args],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def test_status_vocabulary_is_exact() -> None:
    assert assurance.STATUS_VOCAB == (
        "pass",
        "fail-blocking",
        "fail-advisory",
        "skipped-not-installed",
        "skipped-not-applicable",
        "skipped-network-disabled",
        "error-required-tool",
        "error-optional-tool",
        "timeout-required",
        "timeout-optional",
    )


def test_manifest_parser_accepts_valid_json_and_rejects_malformed_acceptance(tmp_path: Path) -> None:
    good = _manifest(
        tmp_path,
        {
            "accepted_findings": [
                {
                    "fingerprint": "abc",
                    "reason": "reviewed",
                    "owner": "lead",
                    "scope": "src",
                    "expires": "2999-01-01",
                }
            ]
        },
    )
    assert assurance.load_manifest(tmp_path, good)["accepted_findings"][0]["owner"] == "lead"

    bad = _manifest(tmp_path, {"accepted_findings": [{"fingerprint": "abc", "scope": "*"}]})
    with pytest.raises(assurance.AssuranceUsageError):
        assurance.load_manifest(tmp_path, bad)


def test_runner_does_not_require_tomllib() -> None:
    source = Path(assurance.__file__).read_text(encoding="utf-8")
    assert "tomllib" not in source


def test_detection_identifies_python_js_go_and_rust(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "go.mod").write_text("module example.com/x\n", encoding="utf-8")
    (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\n", encoding="utf-8")
    detection = assurance.detect_project(tmp_path, assurance.load_manifest(tmp_path))
    assert {"python", "js_ts", "go", "rust"} <= {stack["id"] for stack in detection["stacks"]}


def test_monorepo_detection_emits_children(tmp_path: Path) -> None:
    child = tmp_path / "packages" / "api"
    child.mkdir(parents=True)
    (child / "pyproject.toml").write_text("[project]\nname = 'api'\n", encoding="utf-8")
    _manifest(tmp_path, {"monorepo": {"packages": [{"path": "packages/api", "name": "api"}]}})
    detection = assurance.detect_project(tmp_path, assurance.load_manifest(tmp_path))
    assert detection["monorepo_children"]
    assert detection["monorepo_children"][0]["package_name"] == "api"


def test_missing_optional_required_network_and_timeout_statuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_python_project(tmp_path)
    empty_path = tmp_path / "empty-path"
    empty_path.mkdir()
    monkeypatch.setenv("PATH", str(empty_path))

    _manifest(tmp_path, {"profiles": {"change": {"required_tools": ["bandit", "osv-scanner"]}}})
    result = _run_scan(tmp_path)
    skipped = {item["tool_id"]: item["status"] for item in result.tools_skipped}
    assert skipped["bandit"] == "error-required-tool"
    assert skipped["osv-scanner"] == "skipped-network-disabled"

    tool = _fake_tool(
        tmp_path / "slow.py",
        "import time\ntime.sleep(5)\n",
    )
    _manifest(
        tmp_path,
        {
            "tools": {
                "bandit": {
                    "command": [sys.executable, str(tool)],
                    "timeout_seconds": 1,
                    "required": True,
                },
                "gitleaks": {
                    "command": [sys.executable, str(tool)],
                    "timeout_seconds": 1,
                },
            }
        },
    )
    result = _run_scan(tmp_path)
    run_status = {item["tool_id"]: item["status"] for item in result.tools_run}
    assert run_status["bandit"] == "timeout-required"
    assert run_status["gitleaks"] == "timeout-optional"


def test_artifact_writer_emits_lowercase_json_and_required_blocks(tmp_path: Path) -> None:
    _make_python_project(tmp_path)
    _manifest(tmp_path)
    _baseline(tmp_path)
    result = _run_scan(tmp_path)
    paths = assurance.write_artifact(result, tmp_path / ".agenttalk" / "assurance" / "runs")
    assert paths.artifact.name == "artifact.json"
    artifact = json.loads(paths.artifact.read_text(encoding="utf-8"))
    for key in (
        "schema_version",
        "artifact_type",
        "scanner",
        "provenance",
        "detection",
        "tools",
        "findings",
        "attestation",
        "verdict_summary",
        "residual_risk",
    ):
        assert key in artifact
    assert artifact["schema_version"] == 1
    assert artifact["artifact_type"] == "assurance-scan-run"


def test_provenance_records_hashes_dirty_state_and_import_outside_repo(tmp_path: Path) -> None:
    _make_python_project(tmp_path)
    _manifest(tmp_path, {"python": {"packages": ["json"]}})
    _baseline(tmp_path)
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "base")
    (tmp_path / "src" / "samplepkg" / "dirty.py").write_text("x = 1\n", encoding="utf-8")

    manifest = assurance.load_manifest(tmp_path)
    baseline = assurance.load_baseline(tmp_path)
    provenance = assurance.collect_provenance(tmp_path, manifest, "change", baseline)
    assert provenance["git_sha"]
    assert provenance["git_dirty"] is True
    assert provenance["manifest_sha256"]
    assert provenance["baseline_sha256"]
    json_path = [p for p in provenance["resolved_package_paths"] if p["package"] == "json"][0]
    assert json_path["import_path"]
    assert json_path["expected_under_root"] is False

    result = _run_scan(tmp_path)
    assert any(f["rule_id"] == "import-outside-repo" for f in result.findings)


def test_manifest_changed_in_diff_is_self_waiver_evidence(tmp_path: Path) -> None:
    _manifest(tmp_path)
    _baseline(tmp_path)
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "base")
    _manifest(tmp_path, {"profiles": {"change": {"required_tools": []}}})

    manifest = assurance.load_manifest(tmp_path)
    baseline = assurance.load_baseline(tmp_path)
    provenance = assurance.collect_provenance(tmp_path, manifest, "change", baseline)
    plan = assurance.build_plan(
        tmp_path,
        "change",
        manifest,
        assurance.detect_project(tmp_path, manifest),
        baseline,
        provenance,
    )
    result = assurance.apply_baseline(assurance.run_plan(plan), baseline, manifest, provenance["changed_files"])
    assert provenance["manifest_changed_in_scan_range"] is True
    assert result.artifact["verdict_summary"]["manifest_self_waiver_risk"] is True
    assert any(f["rule_id"] == "manifest-changed-in-range" for f in result.findings)


def test_baseline_delta_classifies_all_states(tmp_path: Path) -> None:
    today = date.today().isoformat()
    future = "2999-01-01"
    expired = "2000-01-01"
    unchanged = assurance.finding_fingerprint("bandit", "B101", "a.py", "assert used")
    worsened = assurance.finding_fingerprint("bandit", "B102", "b.py", "exec used")
    accepted = assurance.finding_fingerprint("bandit", "B103", "c.py", "accepted")
    expired_fp = assurance.finding_fingerprint("bandit", "B104", "d.py", "expired")
    fixed = "fixed-fingerprint"
    manifest = assurance._default_manifest()
    manifest["accepted_findings"] = [
        {"fingerprint": accepted, "reason": "known", "owner": "lead", "scope": "c.py", "expires": future},
        {"fingerprint": expired_fp, "reason": "old", "owner": "lead", "scope": "d.py", "expires": expired},
    ]
    baseline = assurance._default_baseline()
    baseline["findings"] = [
        {
            "fingerprint": unchanged,
            "severity": "medium",
            "dimension": "security",
            "tool": "bandit",
            "rule_id": "B101",
            "path": "a.py",
        },
        {
            "fingerprint": worsened,
            "severity": "low",
            "dimension": "security",
            "tool": "bandit",
            "rule_id": "B102",
            "path": "b.py",
        },
        {
            "fingerprint": fixed,
            "severity": "high",
            "dimension": "security",
            "tool": "bandit",
            "rule_id": "B999",
            "path": "z.py",
        },
    ]
    result = assurance.ScanResult(
        root=tmp_path,
        profile="change",
        manifest=manifest,
        baseline=baseline,
        detection={"stacks": [], "monorepo_children": []},
        provenance={"manifest_changed_in_scan_range": False, "baseline_changed_in_scan_range": False},
        tools_considered=["bandit"],
        tools_run=[],
        tools_skipped=[],
        required_missing=[],
        findings=[
            {
                "fingerprint": unchanged,
                "dimension": "security",
                "severity": "medium",
                "tool_id": "bandit",
                "rule_id": "B101",
                "path": "a.py",
                "line": 1,
                "message": "assert used",
                "raw_ref": None,
            },
            {
                "fingerprint": worsened,
                "dimension": "security",
                "severity": "high",
                "tool_id": "bandit",
                "rule_id": "B102",
                "path": "b.py",
                "line": 1,
                "message": "exec used",
                "raw_ref": None,
            },
            {
                "dimension": "security",
                "severity": "high",
                "tool_id": "bandit",
                "rule_id": "B105",
                "path": "n.py",
                "line": 1,
                "message": "new",
                "raw_ref": None,
            },
            {
                "fingerprint": accepted,
                "dimension": "security",
                "severity": "high",
                "tool_id": "bandit",
                "rule_id": "B103",
                "path": "c.py",
                "line": 1,
                "message": "accepted",
                "raw_ref": None,
            },
            {
                "fingerprint": expired_fp,
                "dimension": "security",
                "severity": "high",
                "tool_id": "bandit",
                "rule_id": "B104",
                "path": "d.py",
                "line": 1,
                "message": "expired",
                "raw_ref": None,
            },
        ],
        residual_risk=[],
        runner_errors=[],
        run_id=f"test-{today}",
    )
    result = assurance.apply_baseline(result, baseline, manifest, [])
    statuses = {finding["rule_id"]: finding["status"] for finding in result.findings}
    assert statuses["B101"] == "unchanged"
    assert statuses["B102"] == "worsened"
    assert statuses["B105"] == "new"
    assert statuses["B103"] == "accepted-applied"
    assert statuses["B104"] == "accepted-expired"
    assert any(f["status"] == "fixed" and f["fingerprint"] == fixed for f in result.findings)
    assert result.accepted_findings["applied"]
    assert result.accepted_findings["expired"]
    assert any(f["blocking"] for f in result.findings if f["rule_id"] == "B104")


def test_encoding_hygiene_catches_nul_and_git_diff_check(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_bytes(b"print('x')\x00\n")
    findings = assurance._encoding_findings(tmp_path, assurance.load_manifest(tmp_path))
    assert any(f["rule_id"] == "nul-byte" for f in findings)

    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "clean.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "clean.py")
    _git(tmp_path, "commit", "-m", "base")
    (tmp_path / "clean.py").write_text("x = 1  \n", encoding="utf-8")
    diff_findings = assurance._git_diff_findings(tmp_path)
    assert any(f["rule_id"] == "diff-check" for f in diff_findings)


def test_generated_artifact_without_execution_blocks_release(tmp_path: Path) -> None:
    _manifest(
        tmp_path,
        {
            "generated_artifacts": [
                {"id": "helper", "path": "scripts/helper.ps1", "kind": "powershell", "executed_by_tests": []}
            ]
        },
    )
    _baseline(tmp_path)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "helper.ps1").write_text("Write-Output ok\n", encoding="utf-8")
    result = _run_scan(tmp_path, "release")
    generated = [f for f in result.findings if f["rule_id"] == "declared-unexecuted"]
    assert generated
    assert generated[0]["blocking"] is True


def test_secure_is_unknown_without_executed_security_scanner(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    _manifest(tmp_path)
    _baseline(tmp_path)
    result = _run_scan(tmp_path)
    assert result.artifact["attestation"]["SECURE"] == "unknown"
    assert any("no executed security" in reason for reason in result.artifact["attestation"]["reasons"])


def test_optional_network_disabled_scanners_are_not_required_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_python_project(tmp_path)
    _manifest(tmp_path)
    _baseline(tmp_path)
    empty_path = tmp_path / "empty-path"
    empty_path.mkdir()
    monkeypatch.setenv("PATH", str(empty_path))

    result = _run_scan(tmp_path)

    summary = result.artifact["verdict_summary"]
    assert summary["skipped_required_count"] == 0
    assert summary["skipped_optional_count"] >= 1
    assert result.artifact["attestation"]["SECURE"] == "unknown"
    assert not any("required scan skipped" in reason for reason in result.artifact["attestation"]["reasons"])


def test_fake_tools_map_findings_raw_logs_and_errors(tmp_path: Path) -> None:
    _make_python_project(tmp_path)
    bandit = _fake_tool(
        tmp_path / "fake_bandit.py",
        "import json, sys\n"
        "print(json.dumps({'findings':[{'rule_id':'B999','severity':'high','path':'src/samplepkg/__init__.py','line':1,'message':'bad'}]}))\n"
        "sys.exit(1)\n",
    )
    gitleaks = _fake_tool(
        tmp_path / "fake_gitleaks.py",
        "import json, sys\n"
        "print(json.dumps([{'RuleID':'generic-api-key','File':'secret.txt','StartLine':1,'Description':'secret'}]))\n"
        "sys.exit(1)\n",
    )
    osv = _fake_tool(
        tmp_path / "fake_osv.py",
        "import json, sys\n"
        "print(json.dumps({'results':[{'packages':[{'package':{'name':'demo','ecosystem':'PyPI'},'version':'1','vulnerabilities':[{'id':'OSV-1','summary':'vuln'}]}]}]}))\n"
        "sys.exit(1)\n",
    )
    error_tool = _fake_tool(tmp_path / "fake_error.py", "import sys\nsys.stderr.write('boom')\nsys.exit(2)\n")
    _manifest(
        tmp_path,
        {
            "profiles": {"change": {"network_allowed": {"osv-scanner": True}}},
            "tools": {
                "bandit": {"command": [sys.executable, str(bandit)]},
                "gitleaks": {"command": [sys.executable, str(gitleaks)]},
                "osv-scanner": {"command": [sys.executable, str(osv)]},
                "semgrep": {"command": [sys.executable, str(error_tool)]},
            },
        },
    )
    _baseline(tmp_path)
    result = _run_scan(tmp_path)
    status_by_tool = {item["tool_id"]: item["status"] for item in result.tools_run}
    assert status_by_tool["bandit"] == "fail-blocking"
    assert status_by_tool["gitleaks"] == "fail-blocking"
    assert status_by_tool["osv-scanner"] == "fail-blocking"
    assert status_by_tool["semgrep"] == "error-optional-tool"
    assert any(f["tool_id"] == "osv-scanner" and f["dimension"] == "deps" for f in result.findings)

    paths = assurance.write_artifact(result, tmp_path / ".agenttalk" / "assurance" / "runs")
    artifact = json.loads(paths.artifact.read_text(encoding="utf-8"))
    assert any(f["tool_id"] == "bandit" and f["raw_ref"] for f in artifact["findings"])
    assert any(run["tool_id"] == "bandit" and run["raw_log"] for run in artifact["tools"]["run"])


def test_main_produces_artifact_when_optional_tools_are_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_python_project(tmp_path)
    _manifest(tmp_path)
    _baseline(tmp_path)
    empty_path = tmp_path / "empty-path"
    empty_path.mkdir()
    monkeypatch.setenv("PATH", str(empty_path))
    rc = assurance.main(
        ["--root", str(tmp_path), "--out", str(tmp_path / ".agenttalk" / "assurance" / "runs"), "--json-only"]
    )
    assert rc == 0
    artifacts = list((tmp_path / ".agenttalk" / "assurance" / "runs").glob("*/artifact.json"))
    assert artifacts
    artifact = json.loads(artifacts[0].read_text(encoding="utf-8"))
    assert artifact["attestation"]["SECURE"] == "unknown"
