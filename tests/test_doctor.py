"""Tests for `agenttalk doctor` health checks."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agenttalk import doctor
from agenttalk.store import Store


def test_doctor_on_uninitialized_project_reports_error(tmp_path: Path) -> None:
    """If `.agenttalk/` is missing, the first check should fail with
    an `error` and the overall status should be `error`."""
    report = doctor.run(tmp_path)
    assert report.overall == "error"
    init_check = report.checks[0]
    assert init_check.name == "store.initialized"
    assert init_check.status == "error"
    assert "agenttalk init" in init_check.fix


def test_doctor_on_initialized_project_includes_all_check_categories(
    tmp_path: Path,
) -> None:
    """Once init has run, doctor should produce checks for: store
    init, claude_skills, codex_skills, codex_config, and one heartbeat
    check per agent."""
    Store(tmp_path).init(["alpha", "beta"])
    report = doctor.run(tmp_path)
    names = {c.name for c in report.checks}
    assert "store.initialized" in names
    assert "claude_skills" in names
    assert "codex_skills" in names
    assert "codex_config" in names
    assert "heartbeat.alpha" in names
    assert "heartbeat.beta" in names


def test_doctor_heartbeat_check_distinguishes_fresh_stale_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three states per agent: ok (fresh), warn (stale > 5min), warn
    (no heartbeat at all)."""
    s = Store(tmp_path)
    s.init(["fresh", "stale", "absent"])
    # fresh: just write a heartbeat now
    s.write_heartbeat("fresh")
    # stale: hand-write a heartbeat with an ancient timestamp
    (tmp_path / ".agenttalk" / "state" / "stale.heartbeat").write_text(
        "2026-05-20T22:00:00Z", encoding="utf-8"
    )
    # absent: don't write a heartbeat
    report = doctor.run(tmp_path)
    hb_by_name = {c.name: c for c in report.checks if c.name.startswith("heartbeat.")}
    assert hb_by_name["heartbeat.fresh"].status == "ok"
    assert hb_by_name["heartbeat.stale"].status == "warn"
    assert "stale" in hb_by_name["heartbeat.stale"].details
    assert hb_by_name["heartbeat.absent"].status == "warn"
    assert "no heartbeat" in hb_by_name["heartbeat.absent"].details


def test_doctor_skill_check_warns_when_target_differs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a user has edited their installed skill file, doctor should
    warn (not error) and tell them to --force re-install."""
    from agenttalk import install_skills as iskl
    fake_claude = tmp_path / "fake-claude-dir"
    fake_codex = tmp_path / "fake-codex-dir"
    # First, install bundled to the fake dirs so the install is "complete"
    iskl.install(claude_dir=fake_claude, codex_dir=fake_codex)
    # Then, hand-mutate one file to simulate user edits
    (fake_claude / "agenttalk.send.md").write_text("user edits\n", encoding="utf-8")
    # Redirect doctor's default-dir lookups
    monkeypatch.setattr(iskl, "default_claude_dir", lambda: fake_claude)
    monkeypatch.setattr(iskl, "default_codex_dir", lambda: fake_codex)
    # Init so the rest of the checks run too
    Store(tmp_path).init(["alpha", "beta"])
    report = doctor.run(tmp_path)
    by_name = {c.name: c for c in report.checks}
    assert by_name["claude_skills"].status == "warn"
    assert "differ" in by_name["claude_skills"].details
    # v0.7.2: details name the file(s) so the user doesn't have to
    # re-run install-skills just to find out which.
    assert "agenttalk.send.md" in by_name["claude_skills"].details
    # v0.7.2: fix hint leads with --dry-run --force (preview) before
    # the destructive --force step.
    fix = by_name["claude_skills"].fix
    assert "--dry-run" in fix and "--force" in fix
    # v0.7.2: per-file payload available in JSON for scripting/loops.
    data = by_name["claude_skills"].data
    assert data is not None
    assert data["differs"] == ["agenttalk.send.md"]
    assert data["missing"] == []
    # claude side ships consult, handoff, listen, propose, send, sk-loop;
    # 1 differs (we mutated send), the rest are unchanged. Derive the
    # total from the bundled source so adding a skill doesn't break this.
    from agenttalk.install_skills import SKILLS_ROOT
    bundled_claude = len(list((SKILLS_ROOT / "claude").glob("*.md")))
    assert data["total"] == bundled_claude
    assert by_name["codex_skills"].status == "ok"


def test_doctor_skill_check_errors_when_target_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the install dir is empty (skills never installed), error."""
    from agenttalk import install_skills as iskl
    fake_claude = tmp_path / "missing-claude"
    fake_codex = tmp_path / "missing-codex"
    monkeypatch.setattr(iskl, "default_claude_dir", lambda: fake_claude)
    monkeypatch.setattr(iskl, "default_codex_dir", lambda: fake_codex)
    Store(tmp_path).init(["alpha", "beta"])
    report = doctor.run(tmp_path)
    by_name = {c.name: c for c in report.checks}
    assert by_name["claude_skills"].status == "error"
    assert by_name["codex_skills"].status == "error"
    assert "install-skills" in by_name["claude_skills"].fix


def test_doctor_codex_config_warn_when_block_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If `~/.codex/config.toml` exists but has no per-project block
    for this project, doctor warns + suggests `codex-config --enable`."""
    from agenttalk import codex_config as cxc
    cfg_path = tmp_path / "fake-codex-config.toml"
    cfg_path.write_text("model = \"gpt-5.5\"\n", encoding="utf-8")
    monkeypatch.setattr(cxc, "default_config_path", lambda: cfg_path)
    Store(tmp_path).init(["alpha", "beta"])
    report = doctor.run(tmp_path)
    by_name = {c.name: c for c in report.checks}
    cc = by_name["codex_config"]
    assert cc.status == "warn"
    assert "codex-config --enable" in cc.fix


def test_doctor_to_dict_round_trip_through_json(tmp_path: Path) -> None:
    """The --json output must be a valid serialization of the report
    and round-trip through json.loads cleanly."""
    Store(tmp_path).init(["alpha", "beta"])
    report = doctor.run(tmp_path)
    payload = report.to_dict()
    raw = json.dumps(payload)
    restored = json.loads(raw)
    assert restored["overall"] in ("ok", "warn", "error")
    assert restored["agenttalk_version"] == report.agenttalk_version
    assert len(restored["checks"]) == len(report.checks)


def test_doctor_overall_resolves_to_error_when_any_check_errors(
    tmp_path: Path,
) -> None:
    """Single error check should propagate to overall=error, even if
    other checks pass."""
    report = doctor.run(tmp_path)
    # Uninitialized projects produce exactly one error check + nothing else
    assert report.overall == "error"
    assert any(c.status == "error" for c in report.checks)