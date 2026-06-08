"""Tests for `agenttalk doctor` health checks."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agenttalk import doctor, install_skills as iskl, signing
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


# ----- hmac check status mapping (review C*: doctor must NOT report a
# degenerate/garbage key as enabled-OK; closes doctor.py 456-472) ------

def test_doctor_hmac_reports_error_for_short_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    proj = tmp_path / "proj"
    Store(proj).init(["alpha", "beta"])
    badkey = tmp_path / "bad.key"           # OUTSIDE the project dir
    badkey.write_text("00", encoding="utf-8")  # valid hex, 1 byte
    monkeypatch.setenv("AGENTTALK_HMAC_KEY_FILE", str(badkey))
    report = doctor.run(proj)
    hmac = next(c for c in report.checks if c.name == "hmac")
    assert hmac.status == "error"
    assert "16 bytes" in hmac.details


def test_doctor_hmac_reports_error_for_garbage_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    proj = tmp_path / "proj"
    Store(proj).init(["alpha", "beta"])
    badkey = tmp_path / "bad.key"
    badkey.write_text("not hex at all", encoding="utf-8")
    monkeypatch.setenv("AGENTTALK_HMAC_KEY_FILE", str(badkey))
    report = doctor.run(proj)
    hmac = next(c for c in report.checks if c.name == "hmac")
    assert hmac.status == "error"
    assert "hex" in hmac.details


def test_doctor_hmac_reports_enabled_ok_for_good_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    proj = tmp_path / "proj"
    store = Store(proj)
    store.init(["alpha", "beta"])
    monkeypatch.setenv("AGENTTALK_HMAC_KEY_FILE", str(tmp_path / "good.key"))
    signing.init_key(store.project_id())   # full 32-byte key OUTSIDE project
    report = doctor.run(proj)
    hmac = next(c for c in report.checks if c.name == "hmac")
    assert hmac.status == "ok"
    assert "enabled" in hmac.details


def test_doctor_hmac_flags_key_inside_project_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    proj = tmp_path / "proj"
    store = Store(proj)
    store.init(["alpha", "beta"])
    inside = proj / "inside.key"
    monkeypatch.setenv("AGENTTALK_HMAC_KEY_FILE", str(inside))
    signing.init_key(store.project_id())   # writes a valid key, but in-project
    report = doctor.run(proj)
    hmac = next(c for c in report.checks if c.name == "hmac")
    assert hmac.status == "error"
    assert "INSIDE the project" in hmac.details


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

# ======================================================================
# 0.14.0 diagnostics (WP03): multi-store detection + liaison checks
# ======================================================================

def _check_by_name(report, name):
    return next(c for c in report.checks if c.name == name)


# ------------------------------------------------------- multi-store (T016)

def test_multi_store_zero_one_two(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    outer = tmp_path / "outer"
    inner = outer / "mid" / "inner"
    inner.mkdir(parents=True)
    # zero stores: quiet ok
    monkeypatch.chdir(inner)
    c = doctor._check_multi_store(inner.resolve())
    assert c.status == "ok"
    assert "no store" in c.details
    # one store: quiet ok naming it
    Store(outer).init(["alpha", "beta"])
    c = doctor._check_multi_store(outer.resolve(), cwd=inner)
    assert c.status == "ok"
    assert str(outer) in c.details
    # two stores: warn naming BOTH in walk order + remediation
    Store(inner).init(["alpha", "beta"])
    c = doctor._check_multi_store(inner.resolve(), cwd=inner)
    assert c.status == "warn"
    assert c.data["stores"] == [str(inner.resolve() / ".agenttalk"),
                                str(outer.resolve() / ".agenttalk")]
    assert "AGENTTALK_ROOT" in c.fix
    assert "deliberate" in c.fix  # fair to init --force nesting


def test_multi_store_pinned_root_note(tmp_path: Path) -> None:
    walk_target = tmp_path / "walkroot"
    pinned = tmp_path / "pinned"
    sub = walk_target / "sub"
    sub.mkdir(parents=True)
    pinned.mkdir()
    Store(walk_target).init(["alpha", "beta"])
    Store(pinned).init(["alpha", "beta"])
    c = doctor._check_multi_store(pinned.resolve(), cwd=sub)
    # one store on the walk, but the resolved root is elsewhere: ok + NOTE
    assert c.status == "ok"
    assert "pinned" in c.details
    assert str(walk_target.resolve()) in c.details


def test_multi_store_runs_even_uninitialized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    report = doctor.run(tmp_path)
    names = [c.name for c in report.checks]
    assert "multi_store" in names
    # existing contract intact: store.initialized stays the first check
    assert names[0] == "store.initialized"


# -------------------------------------------------- operator_facing (T017)

def test_operator_facing_states(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = Store(tmp_path)
    s.init(["lead", "w1"])
    monkeypatch.chdir(tmp_path)

    # not configured, no escalation traffic: ok (INFO)
    c = doctor._check_operator_facing(s)
    assert c.status == "ok"
    assert "not configured" in c.details

    # not configured + escalation traffic exists: warn
    s.send(sender="w1", recipient="lead", kind="question", body="need operator",
           meta={"request_id": "esc-1", "needs_operator": "true"})
    c = doctor._check_operator_facing(s)
    assert c.status == "warn"
    assert "set-operator-facing" in c.fix

    # configured + in roster (fresh heartbeat): ok naming the liaison
    s.set_operator_facing("lead")
    s.write_heartbeat("lead")
    c = doctor._check_operator_facing(s)
    assert c.status == "ok"
    assert "lead" in c.details

    # configured + stale heartbeat: warn
    (tmp_path / ".agenttalk" / "state" / "lead.heartbeat").write_text(
        "2026-01-01T00:00:00Z", encoding="utf-8")
    c = doctor._check_operator_facing(s)
    assert c.status == "warn"
    assert "unread" in c.details

    # configured but NOT in roster: error (the only FAIL state)
    s.remove_agent("lead")
    c = doctor._check_operator_facing(s)
    assert c.status == "error"
    assert "'lead'" in c.details


def test_operator_facing_warns_when_liaison_never_listened(tmp_path: Path) -> None:
    """A configured liaison that has NEVER listened (no heartbeat) is exactly
    the unread-escalations risk this check exists to catch — it must WARN, not
    report OK (review)."""
    s = Store(tmp_path)
    s.init(["lead", "w1"])
    s.set_operator_facing("lead")  # configured, but no heartbeat ever written
    c = doctor._check_operator_facing(s)
    assert c.status == "warn"
    assert "never listened" in c.details


# ----- devkit (dev-discipline pack) doctor check -----------------------

def _point_devkit_at(monkeypatch: pytest.MonkeyPatch, cl: Path, cx: Path) -> None:
    monkeypatch.setattr(iskl, "default_claude_skills_dir", lambda: cl)
    monkeypatch.setattr(iskl, "default_codex_skills_dir", lambda: cx)


def test_doctor_devkit_absent_is_ok_with_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full absence is OK (the pack is opt-out via --no-devkit) but surfaced
    with the install hint — not silently passed."""
    proj = tmp_path / "proj"
    Store(proj).init(["alpha", "beta"])
    _point_devkit_at(monkeypatch, tmp_path / "cl", tmp_path / "cx")
    dk = next(c for c in doctor.run(proj).checks if c.name == "devkit_skills")
    assert dk.status == "ok"
    assert "not installed" in dk.details and "install-skills" in dk.details


def test_doctor_devkit_in_sync_is_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    proj = tmp_path / "proj"
    Store(proj).init(["alpha", "beta"])
    cl, cx = tmp_path / "cl", tmp_path / "cx"
    _point_devkit_at(monkeypatch, cl, cx)
    iskl.install(claude=False, codex=False, devkit=True,
                 claude_skills_dir=cl, codex_skills_dir=cx)
    dk = next(c for c in doctor.run(proj).checks if c.name == "devkit_skills")
    assert dk.status == "ok"
    assert "in sync" in dk.details


def test_doctor_devkit_partial_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    proj = tmp_path / "proj"
    Store(proj).init(["alpha", "beta"])
    cl, cx = tmp_path / "cl", tmp_path / "cx"
    _point_devkit_at(monkeypatch, cl, cx)
    iskl.install(claude=False, codex=False, devkit=True,
                 claude_skills_dir=cl, codex_skills_dir=cx)
    (cl / "craft-code" / "SKILL.md").unlink()  # incomplete: one file gone
    dk = next(c for c in doctor.run(proj).checks if c.name == "devkit_skills")
    assert dk.status == "warn"
    assert "missing" in dk.details
    assert "--devkit-only" in (dk.fix or "")


def test_doctor_devkit_stale_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    proj = tmp_path / "proj"
    Store(proj).init(["alpha", "beta"])
    cl, cx = tmp_path / "cl", tmp_path / "cx"
    _point_devkit_at(monkeypatch, cl, cx)
    iskl.install(claude=False, codex=False, devkit=True,
                 claude_skills_dir=cl, codex_skills_dir=cx)
    (cl / "review-code" / "SKILL.md").write_text("local edit\n", encoding="utf-8")
    dk = next(c for c in doctor.run(proj).checks if c.name == "devkit_skills")
    assert dk.status == "warn"
    assert "differ" in dk.details
    assert "--force" in (dk.fix or "")


def test_operator_facing_no_enforcement_language(tmp_path: Path) -> None:
    # C-007: diagnostics phrase routing/visibility facts, never enforcement.
    s = Store(tmp_path)
    s.init(["lead", "w1"])
    s.set_operator_facing("lead")
    for _ in range(2):  # configured pass, then unset pass
        c = doctor._check_operator_facing(s)
        text = (c.details + " " + c.fix).lower()
        assert "enforce" not in text
        s.set_operator_facing(None)


# ------------------------------------------- root-first contract (T016)

def test_doctor_json_root_is_first_key(tmp_path: Path) -> None:
    Store(tmp_path).init(["alpha", "beta"])
    d = doctor.run(tmp_path).to_dict()
    assert next(iter(d)) == "project_root"


def test_doctor_human_output_first_line_is_root(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Doctor's EXIT CODE reflects host-environment health (installed
    # skills, codex config), which varies by machine — CI runners have
    # none of it. These tests assert the OUTPUT CONTRACT, so they pin the
    # environment to a deterministic broken-skills state (rc == 2) and
    # assert the root-first shape regardless. (v0.14.0 CI regression: the
    # original assertion `rc == 0` only held on hosts with skills
    # installed.)
    from agenttalk import cli
    from agenttalk import install_skills as iskl
    monkeypatch.setattr(iskl, "default_claude_dir", lambda: tmp_path / "no-claude")
    monkeypatch.setattr(iskl, "default_codex_dir", lambda: tmp_path / "no-codex")
    Store(tmp_path).init(["alpha", "beta"])
    rc = cli.main(["--root", str(tmp_path), "doctor"])
    assert rc == 2  # deterministic: skills missing -> overall error
    first_line = capsys.readouterr().out.splitlines()[0]
    assert first_line == f"root: {tmp_path.resolve()}"


def test_doctor_json_cli_first_key_is_root(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agenttalk import cli
    from agenttalk import install_skills as iskl
    monkeypatch.setattr(iskl, "default_claude_dir", lambda: tmp_path / "no-claude")
    monkeypatch.setattr(iskl, "default_codex_dir", lambda: tmp_path / "no-codex")
    Store(tmp_path).init(["alpha", "beta"])
    rc = cli.main(["--root", str(tmp_path), "doctor", "--json"])
    assert rc == 2  # deterministic: skills missing -> overall error
    out = capsys.readouterr().out
    # json.dumps preserves insertion order: the first emitted key is the root
    first_key = out.splitlines()[1].strip().split(":")[0].strip('" ')
    assert first_key == "project_root"


# ------------------------------------------- store hygiene (0.15.0, T012)

def test_store_hygiene_states(tmp_path: Path) -> None:
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    # clean
    c = doctor._check_store_hygiene(s)
    assert c.status == "ok" and c.data == {"invalid": 0, "quarantined": 0}
    # invalid present: warn, --dry-run named FIRST in the fix
    (tmp_path / ".agenttalk" / "messages" / "junk.json").write_text(
        "{not json", encoding="utf-8")
    c = doctor._check_store_hygiene(s)
    assert c.status == "warn"
    assert c.fix.index("--dry-run") < c.fix.index("quarantine (recoverable")
    assert c.data["invalid"] == 1
    # quarantined only: ok + informational count
    s.quarantine_invalid()
    c = doctor._check_store_hygiene(s)
    assert c.status == "ok"
    assert c.data == {"invalid": 0, "quarantined": 1}
    assert "recoverable" in c.details
    # NOTE: no doctor EXIT-CODE assertions on an unpinned host (the
    # 0.14.0 red-matrix CI lesson) - this test pins nothing because it
    # asserts check objects, never process exits.


def test_store_hygiene_wired_into_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    Store(tmp_path).init(["alpha", "beta"])
    monkeypatch.chdir(tmp_path)
    names = [c.name for c in doctor.run(tmp_path).checks]
    assert "store_hygiene" in names


def test_store_hygiene_combined_invalid_and_quarantined(tmp_path: Path) -> None:
    # the "both" state from the WP matrix: live invalid AND quarantined
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    (tmp_path / ".agenttalk" / "messages" / "first.json").write_text(
        "{not json", encoding="utf-8")
    s.quarantine_invalid()
    (tmp_path / ".agenttalk" / "messages" / "second.json").write_text(
        "{also not json", encoding="utf-8")
    c = doctor._check_store_hygiene(s)
    assert c.status == "warn"                      # live invalid dominates
    assert c.data == {"invalid": 1, "quarantined": 1}
    assert "already quarantined" in c.details      # both facts surfaced
    assert "--dry-run" in c.fix


# ===================================================== #19 Phase A (WP04/T019)
# Identity registry hygiene check.

def _find(report, name):
    return next(c for c in report.checks if c.name == name)


def test_identity_registry_ok_with_no_retired(tmp_path: Path) -> None:
    Store(tmp_path).init(["alpha", "beta"])
    c = _find(doctor.run(tmp_path), "identity_registry")
    assert c.status == "ok"
    assert c.data == {"active": 2, "retired": 0}


def test_identity_registry_counts_after_retire_and_rename(tmp_path: Path) -> None:
    s = Store(tmp_path)
    s.init(["alpha", "beta", "gamma"])
    s.retire_agent("gamma")                 # tombstone, renamed_to=None
    s.rename_agent("beta", "beta2")         # tombstone beta->beta2 (in roster)
    c = _find(doctor.run(tmp_path), "identity_registry")
    assert c.status == "ok"                 # beta2 IS active -> lineage resolves
    assert c.data["active"] == 2 and c.data["retired"] == 2


def test_identity_registry_warns_on_dangling_lineage(tmp_path: Path) -> None:
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    s.rename_agent("beta", "beta2")         # beta -> beta2 (active)
    s.remove_agent("beta2")                 # force-remove beta2: lineage dangles
    c = _find(doctor.run(tmp_path), "identity_registry")
    assert c.status == "warn"
    assert "dangling" in c.details and "beta->beta2" in c.details
    assert c.data["dangling"] == ["beta->beta2"]


def test_doctor_does_not_crash_on_active_retired_overlap(tmp_path: Path) -> None:
    # #19 / Codex WP04: a corrupt config (a name in BOTH agents and retired)
    # makes load_config raise. doctor must REPORT it (init check error), not
    # crash — config-dependent checks are gated on the init check.
    import json as _json
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    cfgp = tmp_path / ".agenttalk" / "config.json"
    cfg = _json.loads(cfgp.read_text(encoding="utf-8"))
    cfg["retired"] = [{"name": "beta", "retired_at": "2026-01-01T00:00:00Z",
                       "renamed_to": None, "reason": None}]  # beta is ALSO active
    cfgp.write_text(_json.dumps(cfg), encoding="utf-8")
    report = doctor.run(tmp_path)                 # must NOT raise
    assert report.overall == "error"
    init = _find(report, "store.initialized")
    assert init.status == "error" and "BOTH" in init.details
    # the config-dependent checks were skipped (no crash)
    assert not any(c.name == "identity_registry" for c in report.checks)


# ===================================== 0.18.0 (WP04): active-waiters advisory

def test_doctor_active_waiters_reports_live_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FR-009: doctor names an agent with a LIVE `.waiting` marker (PID +
    advisory), never errors, and frames it as the current owner — not a
    complete duplicate check."""
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    s.write_waiting("alpha", {"agent": "alpha", "pid": 4242,
                              "deadline_epoch": None})
    monkeypatch.setattr("agenttalk.store._process_alive",
                        lambda pid: pid == 4242)
    report = doctor.run(tmp_path)
    aw = next(c for c in report.checks if c.name == "active_waiters")
    assert aw.status == "ok"               # the advisory itself never errors
    assert "alpha (PID 4242)" in aw.details
    assert aw.data["live_waiters"] == [{"agent": "alpha", "pid": 4242}]
    # NOTE: report.overall is environment-dependent (other checks — skills,
    # codex-config — may warn/error on a bare CI checkout). What FR-009 owns
    # is that THIS check is advisory-only (status ok), asserted above.


def test_doctor_active_waiters_absent_when_no_live_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The advisory is ABSENT (no `active_waiters` check at all) when nobody
    is actively waiting — no marker or a dead marker."""
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    # (a) no marker at all
    report = doctor.run(tmp_path)
    assert all(c.name != "active_waiters" for c in report.checks)
    # (b) a marker whose pid is dead → still absent
    s.write_waiting("alpha", {"agent": "alpha", "pid": 4242,
                              "deadline_epoch": None})
    monkeypatch.setattr("agenttalk.store._process_alive", lambda pid: False)
    report = doctor.run(tmp_path)
    assert all(c.name != "active_waiters" for c in report.checks)


def test_doctor_active_waiters_malformed_marker_no_crash(tmp_path: Path) -> None:
    """A corrupt `.waiting` file reads as no waiter — doctor never crashes and
    omits the advisory."""
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    (s.state_dir / "alpha.waiting").write_text("{not json", encoding="utf-8")
    report = doctor.run(tmp_path)              # must not raise
    assert all(c.name != "active_waiters" for c in report.checks)
