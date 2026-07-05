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


def _artifact_from_cli(root: Path, profile: str = "change") -> dict:
    out = root / ".agenttalk" / "assurance" / "runs"
    rc = assurance.main(["--root", str(root), "--profile", profile, "--out", str(out), "--json-only"])
    assert rc == 0
    artifacts = sorted(out.glob("*/artifact.json"))
    assert artifacts
    return json.loads(artifacts[-1].read_text(encoding="utf-8"))


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


def test_skill_warning_uses_utf8_em_dash_without_replacement_char() -> None:
    skill = Path("src/agenttalk/skills/devkit/assurance-scan/SKILL.md").read_text(encoding="utf-8")
    expected = (
        "the scan is a cheap uniform FLOOR — the worst real bugs we shipped/nearly-shipped "
        "($args binding, dir-vs-file mtime ordering, the lane-approval bypass chain) were caught by "
        "EXECUTED tests + adversarial review, NOT by any scanner. ASSURANCE.md must NEVER read scanned == assured."
    )
    assert expected in skill
    assert "FLOOR ?" not in skill
    assert "\ufffd" not in skill


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


def test_malformed_acceptance_expires_produces_validation_artifact(tmp_path: Path) -> None:
    _baseline(tmp_path)
    _manifest(
        tmp_path,
        {
            "accepted_findings": [
                {
                    "fingerprint": "abc",
                    "reason": "reviewed",
                    "owner": "lead",
                    "scope": "src",
                    "expires": "not-a-date",
                }
            ]
        },
    )

    artifact = _artifact_from_cli(tmp_path)

    assert any(
        f["tool_id"] == "manifest-validate"
        and f["rule_id"] == "schema"
        and "invalid expires" in f["message"]
        and f["blocking"]
        for f in artifact["findings"]
    )


def test_manifest_unknown_top_level_and_profile_keys_fail_closed(tmp_path: Path) -> None:
    _make_python_project(tmp_path)
    _baseline(tmp_path)

    _manifest(tmp_path, {"required_tools": ["bandit"]})
    artifact = _artifact_from_cli(tmp_path)
    assert any(
        f["tool_id"] == "manifest-validate"
        and f["rule_id"] == "schema"
        and "unknown top-level key" in f["message"]
        and f["blocking"]
        for f in artifact["findings"]
    )

    inner = tmp_path / "inner"
    inner.mkdir()
    _make_python_project(inner)
    _baseline(inner)
    _manifest(inner, {"profiles": {"change": {"required_tool": ["bandit"]}}})
    artifact = _artifact_from_cli(inner)
    assert any(
        f["tool_id"] == "manifest-validate"
        and f["rule_id"] == "schema"
        and "profiles.change has unknown key" in f["message"]
        and f["blocking"]
        for f in artifact["findings"]
    )


def test_profile_scope_keys_fail_closed_instead_of_silent_ignore(tmp_path: Path) -> None:
    _make_python_project(tmp_path)
    _baseline(tmp_path)
    _manifest(tmp_path, {"profiles": {"change": {"exclude_paths": ["src/generated"]}}})

    artifact = _artifact_from_cli(tmp_path)

    assert any(
        f["tool_id"] == "manifest-validate"
        and f["rule_id"] == "schema"
        and "exclude_paths" in f["message"]
        and f["blocking"]
        for f in artifact["findings"]
    )


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
        tools_run=[
            {
                "tool_id": "bandit",
                "dimension": "security",
                "command": ["bandit"],
                "version": None,
                "status": "pass",
                "required": False,
                "exit_code": 0,
                "duration_ms": 0,
                "network_allowed": False,
                "raw_log": None,
            }
        ],
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


def test_untracked_baseline_is_self_waiver_evidence(tmp_path: Path) -> None:
    _make_python_project(tmp_path)
    bandit = _fake_tool(
        tmp_path / "fake_bandit.py",
        "import json, sys\n"
        "print(json.dumps({'findings':[{'rule_id':'B999','severity':'high','path':'src/samplepkg/__init__.py','line':1,'message':'bad'}]}))\n"
        "sys.exit(1)\n",
    )
    _manifest(tmp_path, {"tools": {"bandit": {"command": [sys.executable, str(bandit)]}}})
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "base")

    fp = assurance.finding_fingerprint("bandit", "B999", "src/samplepkg/__init__.py", "bad")
    _baseline(
        tmp_path,
        {
            "findings": [
                {
                    "fingerprint": fp,
                    "severity": "high",
                    "dimension": "security",
                    "tool": "bandit",
                    "rule_id": "B999",
                    "path": "src/samplepkg/__init__.py",
                }
            ]
        },
    )

    result = _run_scan(tmp_path)
    assert result.provenance["baseline_changed_in_scan_range"] is True
    assert ".agenttalk/assurance/baseline.json" in result.provenance["changed_files"]
    assert result.artifact["verdict_summary"]["manifest_self_waiver_risk"] is True
    assert any(f["rule_id"] == "baseline-changed-in-range" and f["blocking"] for f in result.findings)


def test_baseline_finding_is_not_fixed_when_tool_did_not_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_python_project(tmp_path)
    empty_path = tmp_path / "empty-path"
    empty_path.mkdir()
    monkeypatch.setenv("PATH", str(empty_path))
    fp = assurance.finding_fingerprint("gitleaks", "generic-api-key", "secret.txt", "secret")
    _manifest(tmp_path)
    _baseline(
        tmp_path,
        {
            "findings": [
                {
                    "fingerprint": fp,
                    "severity": "high",
                    "dimension": "secrets",
                    "tool": "gitleaks",
                    "rule_id": "generic-api-key",
                    "path": "secret.txt",
                }
            ]
        },
    )

    result = _run_scan(tmp_path)

    baseline_item = [f for f in result.findings if f["fingerprint"] == fp][0]
    assert baseline_item["status"] == "unchanged"
    assert "not reassessed" in baseline_item["message"]
    assert any(
        risk.get("tool_id") == "gitleaks" and "not reassessed" in risk["reason"] for risk in result.residual_risk
    )


def test_git_status_changed_files_preserves_porcelain_paths(tmp_path: Path) -> None:
    _manifest(tmp_path)
    _baseline(tmp_path)
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "top.txt").write_text("one\n", encoding="utf-8")
    (tmp_path / "src" / "pkg" / "foo.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "base")

    (tmp_path / "top.txt").write_text("two\n", encoding="utf-8")
    (tmp_path / "src" / "pkg" / "foo.py").write_text("VALUE = 2\n", encoding="utf-8")

    changed = assurance.collect_provenance(
        tmp_path,
        assurance.load_manifest(tmp_path),
        "change",
        assurance.load_baseline(tmp_path),
    )["changed_files"]
    assert "top.txt" in changed
    assert "src/pkg/foo.py" in changed
    assert "op.txt" not in changed
    assert "rc/pkg/foo.py" not in changed


def test_accepted_scope_mismatch_does_not_suppress_finding(tmp_path: Path) -> None:
    fp = assurance.finding_fingerprint("bandit", "B321", "src/app.py", "bad")
    manifest = assurance._default_manifest()
    manifest["accepted_findings"] = [
        {
            "fingerprint": fp,
            "reason": "known",
            "owner": "lead",
            "scope": "other",
            "dimension": "security",
            "expires": "2999-01-01",
        }
    ]
    result = assurance.ScanResult(
        root=tmp_path,
        profile="change",
        manifest=manifest,
        baseline=assurance._default_baseline(),
        detection={"stacks": [], "monorepo_children": []},
        provenance={"manifest_changed_in_scan_range": False, "baseline_changed_in_scan_range": False},
        tools_considered=["bandit"],
        tools_run=[],
        tools_skipped=[],
        required_missing=[],
        findings=[
            {
                "fingerprint": fp,
                "dimension": "security",
                "severity": "high",
                "tool_id": "bandit",
                "rule_id": "B321",
                "path": "src/app.py",
                "line": 1,
                "message": "bad",
                "raw_ref": None,
            }
        ],
        residual_risk=[],
        runner_errors=[],
        run_id="test-scope",
    )

    result = assurance.apply_baseline(result, assurance._default_baseline(), manifest, [])
    status_by_rule = {finding["rule_id"]: finding["status"] for finding in result.findings}
    assert status_by_rule["B321"] == "new"
    assert status_by_rule["accepted-scope-mismatch"] == "new"
    assert any(f["rule_id"] == "accepted-scope-mismatch" and f["blocking"] for f in result.findings)


def test_accepted_scope_allows_dot_slash_prefix(tmp_path: Path) -> None:
    fp = assurance.finding_fingerprint("bandit", "B321", "src/app.py", "bad")
    manifest = assurance._default_manifest()
    manifest["accepted_findings"] = [
        {
            "fingerprint": fp,
            "reason": "known",
            "owner": "lead",
            "scope": "./src/app.py",
            "dimension": "security",
            "expires": "2999-01-01",
        }
    ]
    result = assurance.ScanResult(
        root=tmp_path,
        profile="change",
        manifest=manifest,
        baseline=assurance._default_baseline(),
        detection={"stacks": [], "monorepo_children": []},
        provenance={"manifest_changed_in_scan_range": False, "baseline_changed_in_scan_range": False},
        tools_considered=["bandit"],
        tools_run=[],
        tools_skipped=[],
        required_missing=[],
        findings=[
            {
                "fingerprint": fp,
                "dimension": "security",
                "severity": "high",
                "tool_id": "bandit",
                "rule_id": "B321",
                "path": "src/app.py",
                "line": 1,
                "message": "bad",
                "raw_ref": None,
            }
        ],
        residual_risk=[],
        runner_errors=[],
        run_id="test-scope-dot-slash",
    )

    result = assurance.apply_baseline(result, assurance._default_baseline(), manifest, [])

    status_by_rule = {finding["rule_id"]: finding["status"] for finding in result.findings}
    assert status_by_rule["B321"] == "accepted-applied"
    assert "accepted-scope-mismatch" not in status_by_rule


@pytest.mark.parametrize("scope", ["ALL", "Global/**/*", "./src/**"])
def test_accepted_finding_rejects_casefolded_blanket_scopes(tmp_path: Path, scope: str) -> None:
    _manifest(
        tmp_path,
        {
            "accepted_findings": [
                {
                    "fingerprint": "abc",
                    "reason": "known",
                    "owner": "lead",
                    "scope": scope,
                    "expires": "2999-01-01",
                }
            ]
        },
    )
    with pytest.raises(assurance.AssuranceUsageError):
        assurance.load_manifest(tmp_path)


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


def test_encoding_hygiene_skips_binary_nuls_and_scans_nested_build_package(tmp_path: Path) -> None:
    (tmp_path / "font.woff").write_bytes(b"\x00\x01font")
    nested = tmp_path / "src" / "pkg" / "build"
    nested.mkdir(parents=True)
    (nested / "bad.py").write_bytes(b"print('x')\x00\n")

    findings = assurance._encoding_findings(tmp_path, assurance.load_manifest(tmp_path))

    assert not any(f["path"] == "font.woff" and f["rule_id"] == "nul-byte" for f in findings)
    assert any(f["path"] == "src/pkg/build/bad.py" and f["rule_id"] == "nul-byte" for f in findings)


@pytest.mark.parametrize(
    "artifact",
    [
        {"id": "helper", "path": "scripts/helper.ps1", "kind": "powershell", "executed_by_tests": []},
        {"id": "helper", "path": "scripts/helper.ps1", "kind": "ps1", "executed_by_tests": []},
        {"id": "helper", "path": "scripts/helper.ps1", "executed_by_tests": []},
    ],
)
def test_generated_artifact_without_execution_blocks_release(tmp_path: Path, artifact: dict) -> None:
    _manifest(
        tmp_path,
        {"generated_artifacts": [artifact]},
    )
    _baseline(tmp_path)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "helper.ps1").write_text("Write-Output ok\n", encoding="utf-8")
    result = _run_scan(tmp_path, "release")
    generated = [f for f in result.findings if f["rule_id"] == "declared-unexecuted"]
    assert generated
    assert generated[0]["blocking"] is True


def test_generated_artifact_unknown_kind_is_manifest_validation_error(tmp_path: Path) -> None:
    _manifest(
        tmp_path,
        {
            "generated_artifacts": [
                {"id": "helper", "path": "scripts/helper.txt", "kind": "ps", "executed_by_tests": []}
            ]
        },
    )
    _baseline(tmp_path)
    artifact = _artifact_from_cli(tmp_path, "release")
    assert any(
        f["tool_id"] == "manifest-validate"
        and f["rule_id"] == "schema"
        and "kind is unknown" in f["message"]
        and f["blocking"]
        for f in artifact["findings"]
    )


def test_secure_is_unknown_without_executed_security_scanner(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    _manifest(tmp_path)
    _baseline(tmp_path)
    result = _run_scan(tmp_path)
    assert result.artifact["attestation"]["SECURE"] == "unknown"
    assert any("missing executed" in reason for reason in result.artifact["attestation"]["reasons"])


def test_secure_requires_security_deps_and_secrets_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_python_project(tmp_path)
    empty_path = tmp_path / "empty-path"
    empty_path.mkdir()
    monkeypatch.setenv("PATH", str(empty_path))
    bandit = _fake_tool(tmp_path / "fake_bandit.py", "import sys\nsys.exit(0)\n")
    _manifest(tmp_path, {"tools": {"bandit": {"command": [sys.executable, str(bandit)]}}})
    _baseline(tmp_path)

    result = _run_scan(tmp_path)

    assert result.artifact["attestation"]["SECURE"] == "unknown"
    assert any(
        "missing executed deps, secrets evidence" in reason for reason in result.artifact["attestation"]["reasons"]
    )


def test_robust_is_not_good_from_vacuous_generated_artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_python_project(tmp_path)
    empty_path = tmp_path / "empty-path"
    empty_path.mkdir()
    monkeypatch.setenv("PATH", str(empty_path))
    _manifest(tmp_path)
    _baseline(tmp_path)

    result = _run_scan(tmp_path)

    assert result.artifact["attestation"]["ROBUST"] == "not_assessed"


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
    required_tools = {item["tool_id"] for item in result.required_missing}
    assert "osv-scanner" not in required_tools
    assert "pip-audit" not in required_tools
    assert summary["skipped_optional_count"] >= 1
    assert result.artifact["attestation"]["SECURE"] == "unknown"
    assert not any(
        reason.startswith("SECURE: required scan skipped") for reason in result.artifact["attestation"]["reasons"]
    )


def test_git_diff_check_without_git_checkout_is_not_a_required_pass(tmp_path: Path) -> None:
    _make_python_project(tmp_path)
    _manifest(tmp_path)
    _baseline(tmp_path)

    result = _run_scan(tmp_path)

    git_diff = [run for run in result.tools_run if run["tool_id"] == "git-diff-check"][0]
    assert git_diff["status"] == "skipped-not-applicable"
    assert any(item["tool_id"] == "git-diff-check" for item in result.required_missing)
    assert result.artifact["attestation"]["GOOD"] == "unknown"


def test_semgrep_remote_config_is_network_required(tmp_path: Path) -> None:
    _make_python_project(tmp_path)
    _manifest(tmp_path, {"tools": {"semgrep": {"config": "p/ci"}}})
    _baseline(tmp_path)

    result = _run_scan(tmp_path)

    semgrep = [item for item in result.tools_skipped if item["tool_id"] == "semgrep"][0]
    assert semgrep["status"] == "skipped-network-disabled"


def test_osv_severity_list_uses_max_cvss_score() -> None:
    findings = assurance._osv_findings(
        {
            "results": [
                {
                    "packages": [
                        {
                            "package": {"name": "demo", "ecosystem": "PyPI"},
                            "version": "1",
                            "vulnerabilities": [
                                {
                                    "id": "OSV-1",
                                    "summary": "vuln",
                                    "severity": [
                                        {"type": "CVSS_V3", "score": "9.8"},
                                        {"type": "CVSS_V3", "score": "7.1"},
                                    ],
                                }
                            ],
                        }
                    ]
                }
            ]
        }
    )
    assert findings[0]["severity"] == "critical"


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
