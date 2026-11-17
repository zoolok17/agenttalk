"""Tests for `agenttalk doctor` health checks."""

from __future__ import annotations

import contextlib
import inspect
import json
import os
import re
from pathlib import Path

import pytest

from agenttalk import cli, doctor, install_skills as iskl, ovh_gateway_service, signing
from agenttalk import supervisor as sup
from agenttalk.store import Store
from agenttalk.wrapper.obligations import POLICY_ENV


def _all_planner_states() -> list[str]:
    """Every literal state string `supervisor._plan_one` can emit, extracted
    directly from the LIVE source (never hand-copied into this test).

    round-2 review: the #105 verdict mapping hand-enumerated 3 of the 20+
    states the planner actually emits, so 6 more "gone" states (plus every
    unrelated action/hold state) fell through and rendered healthy - a
    hand-copied list in THIS test would carry the identical staleness risk.
    Scanning the source instead means a state the planner grows tomorrow is
    automatically exercised here, with no list to remember to update.

    round-3 review minor: a bare ``len(states) >= 20`` floor could silently
    tolerate coverage SHRINKING by several states (a broken regex still
    clears 20). Assert the healthy allowlist is a SUBSET of what was
    extracted instead - a rename/typo that drops an allowlisted state out of
    the extraction fails this positively, not just quietly under-counts.
    """
    src = inspect.getsource(sup._plan_one)
    states = set(re.findall(r'state\s*=\s*"([A-Z][A-Z0-9_]+)"', src))
    states.update(re.findall(r'_healthy\(\s*"([A-Z][A-Z0-9_]+)"', src))
    assert sup.CLI_CHILD_HEALTHY_STATES <= states, (
        f"extraction missed a known-healthy state: "
        f"{sup.CLI_CHILD_HEALTHY_STATES - states} not found in {sorted(states)}")
    return sorted(states)


def test_doctor_on_uninitialized_project_reports_error(tmp_path: Path) -> None:
    """If `.agenttalk/` is missing, the first check should fail with
    an `error` and the overall status should be `error`."""
    report = doctor.run(tmp_path)
    assert report.overall == "error"
    init_check = report.checks[0]
    assert init_check.name == "store.initialized"
    assert init_check.status == "error"
    assert "agenttalk init" in init_check.fix


@pytest.mark.parametrize("json_output", [False, True])
def test_doctor_leaves_empty_folders_unset_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
    json_output: bool,
) -> None:
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    monkeypatch.delenv("AGENTTALK_ROOT", raising=False)
    for folder in (parent, child):
        monkeypatch.chdir(folder)
        assert cli.main(["doctor", *(["--json"] if json_output else [])]) == 2
        output = capsys.readouterr().out
        if json_output:
            report = json.loads(output)
            assert report["project_root"] == str(folder.resolve())
            assert report["overall"] == "error"
            checks = {c["name"]: c for c in report["checks"]}
            assert checks["store.initialized"]["status"] == "error"
            assert checks["multi_store"]["data"]["stores"] == []
        else:
            assert f"root: {folder.resolve()}" in output
            assert f".agenttalk/config.json not found at {folder.resolve()}" in output
        assert not (folder / ".agenttalk").exists()
        assert sorted(parent.rglob("*")) == [child]


@pytest.mark.parametrize("initialized", [False, True])
def test_doctor_powershell_check_keeps_result_and_locks_only_set_up_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    initialized: bool,
) -> None:
    store = Store(tmp_path)
    if initialized:
        store.init(["alpha"])
    lock_entries = []
    real_lock = store._powershell_selection_lock

    @contextlib.contextmanager
    def tracked_lock():
        with real_lock():
            lock_entries.append(True)
            yield

    monkeypatch.setattr(store, "_powershell_selection_lock", tracked_lock)
    monkeypatch.setattr(doctor.sys, "platform", "win32")
    # This test is about locking, not about where PowerShell is installed: no host found.
    monkeypatch.setattr(doctor.psh, "program_files_candidates", lambda *a, **k: ())
    monkeypatch.setattr(doctor.psh, "path_candidates", lambda *a, **k: ())
    check = doctor._check_powershell_host(store)
    assert check == doctor.Check(
        name="powershell_host",
        status="error",
        details=(
            "PowerShell Core host unavailable: no PowerShell host is selected; "
            f"{doctor.psh.INSTALL_REMEDIATION}; task status UNKNOWN"
        ),
        fix=doctor.psh.INSTALL_REMEDIATION,
        data={"status": "error", "task_status": "unknown"},
    )
    assert len(lock_entries) == int(initialized)
    if not initialized:
        assert list(tmp_path.iterdir()) == []


def _windows_without_selection(monkeypatch: pytest.MonkeyPatch, program_files, on_path=()):
    """Windows branch forced (the check returns early on other platforms); the two candidate
    searches `supervise --select-pwsh` uses are replaced by what the test wants them to find.
    ``on_path=None`` keeps the REAL PATH search (the test sets PATH itself)."""
    monkeypatch.setattr(doctor.sys, "platform", "win32")
    monkeypatch.setattr(doctor.psh, "program_files_candidates", lambda *a, **k: tuple(program_files))
    if on_path is not None:
        monkeypatch.setattr(doctor.psh, "path_candidates", lambda *a, **k: tuple(on_path))


@pytest.mark.parametrize("content", [b"", b"not a real program"], ids=["zero-byte", "not-a-program"])
def test_doctor_powershell_check_says_found_not_verified_when_pwsh_is_present_but_not_selected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: bytes
) -> None:
    pwsh = tmp_path / "pf" / "PowerShell" / "7" / "pwsh.exe"
    pwsh.parent.mkdir(parents=True)
    pwsh.write_bytes(content)  # only its existence is tested; nothing is run, so it cannot be vouched for
    _windows_without_selection(monkeypatch, [str(pwsh)])
    check = doctor._check_powershell_host(Store(tmp_path / "project"))
    assert (check.name, check.status, check.data) == (
        "powershell_host", "error", {"status": "error", "task_status": "unknown"},
    )
    assert f"found at {pwsh}" in check.fix
    assert "`agenttalk supervise --select-pwsh` to check and select it" in check.fix
    # finding a file is not proof of a working install, and doctor never runs it to find out
    assert "installed" not in check.fix
    assert "install PowerShell" not in check.fix and "aka.ms" not in check.fix
    # the message no longer tells the reader to install what is there
    assert "no PowerShell host is selected" in check.details
    assert "aka.ms" not in check.details
    assert check.details.endswith("; task status UNKNOWN")


def _pwsh_only_on_a_temp_path(monkeypatch: pytest.MonkeyPatch, folder: Path) -> Path:
    """A real pwsh.exe file in ``folder``, which is the whole PATH, and no Program Files variable."""
    folder.mkdir(parents=True)
    pwsh = folder / "pwsh.exe"
    pwsh.write_bytes(b"")
    monkeypatch.setenv("PATH", str(folder))
    for name in ("ProgramW6432", "ProgramFiles", "ProgramFiles(Arm)", "ProgramFiles(x86)"):
        monkeypatch.delenv(name, raising=False)
    return pwsh


@pytest.mark.parametrize("folder_name", ["tools", "tools dir"], ids=["no-space", "with-space"])
def test_doctor_powershell_check_names_the_found_path_for_a_path_only_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, folder_name: str
) -> None:
    # The REAL search runs on a real temp PATH. A real absolute path is never a plain token, so what it prints is
    # not a ready command: doctor shows the path itself and says how to use it, and never shows a placeholder as
    # something to run.
    pwsh = _pwsh_only_on_a_temp_path(monkeypatch, tmp_path / folder_name)
    _windows_without_selection(monkeypatch, [], on_path=None)
    check = doctor._check_powershell_host(Store(tmp_path / "project"))
    assert check.status == "error"
    assert f"found at {pwsh};" in check.fix
    assert "--select-pwsh --pwsh followed by that path" in check.fix
    assert "not printed as a ready command" in check.fix
    assert "<p>" not in check.fix and "`agenttalk supervise --select-pwsh --pwsh" not in check.fix
    assert "installed" not in check.fix
    assert "aka.ms" not in check.fix


def test_doctor_powershell_check_runs_the_path_command_only_when_it_really_is_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A plain token is the one case where the command is ready; no real path is one, so the search is replaced here.
    _windows_without_selection(monkeypatch, [], on_path=["pwsh.exe"])
    check = doctor._check_powershell_host(Store(tmp_path))
    assert "found at pwsh.exe;" in check.fix
    assert "run `agenttalk supervise --select-pwsh --pwsh pwsh.exe` to check and select it" in check.fix
    assert "not printed as a ready command" not in check.fix


def test_doctor_powershell_check_echoes_a_found_path_safely(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The path came from the machine: it goes through the echo rule, so it cannot start a line of its own.
    odd = "C:\\pf\\Power\nShell\\7\\pwsh.exe"
    _windows_without_selection(monkeypatch, [odd])
    monkeypatch.setattr(doctor.os.path, "isfile", lambda path: path == odd)
    check = doctor._check_powershell_host(Store(tmp_path / "project"))
    assert "\n" not in check.fix
    assert "found at C:\\pf\\Power\\u000aShell\\7\\pwsh.exe;" in check.fix


def test_doctor_powershell_check_echoes_a_path_only_hit_safely(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    odd = "C:\\tools\\pw\nsh\\pwsh.exe"
    _windows_without_selection(monkeypatch, [], on_path=[odd])
    check = doctor._check_powershell_host(Store(tmp_path / "project"))
    assert "\n" not in check.fix
    assert "found at C:\\tools\\pw\\u000ash\\pwsh.exe;" in check.fix


def test_doctor_powershell_check_keeps_install_advice_when_no_pwsh_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "pf" / "PowerShell" / "7" / "pwsh.exe"  # never created
    _windows_without_selection(monkeypatch, [str(missing)])
    check = doctor._check_powershell_host(Store(tmp_path / "project"))
    assert check.status == "error"
    assert check.fix == doctor.psh.INSTALL_REMEDIATION
    assert doctor.psh.INSTALL_REMEDIATION in check.details


def test_doctor_powershell_check_never_runs_the_pwsh_it_finds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pwsh = tmp_path / "pwsh.exe"
    pwsh.write_bytes(b"")
    _windows_without_selection(monkeypatch, [str(pwsh)])

    def boom(*args, **kwargs):
        raise AssertionError("doctor must only test that the file exists")

    monkeypatch.setattr(doctor.psh, "probe_candidate", boom)
    monkeypatch.setattr(doctor.psh, "resolve_candidate", boom)
    monkeypatch.setattr(doctor.psh.subprocess, "run", boom)
    monkeypatch.setattr(doctor.psh.subprocess, "Popen", boom)
    assert doctor._check_powershell_host(Store(tmp_path / "project")).status == "error"


def test_doctor_powershell_install_advice_constant_is_unchanged() -> None:
    # supervisor_lifecycle and `supervise --select-pwsh` share this text; doctor must not edit it.
    assert doctor.psh.INSTALL_REMEDIATION == (
        "install PowerShell Core 7+ (pwsh) from https://aka.ms/powershell, then run "
        "`agenttalk supervise --select-pwsh`"
    )


def test_doctor_not_set_up_check_says_not_set_up_yet(tmp_path: Path) -> None:
    check = doctor._check_init(Store(tmp_path))
    assert (check.name, check.status) == ("store.initialized", "error")
    assert check.details == f"not set up yet: .agenttalk/config.json not found at {tmp_path}"
    assert "`agenttalk init --here --agents <your agent names>`" in check.fix
    assert "claude,codex" not in check.fix


def test_doctor_partly_set_up_folder_names_the_missing_config_file(tmp_path: Path) -> None:
    """Store.initialized() is true only when .agenttalk/config.json exists, so a folder that has a
    .agenttalk/ but no config.json is still "not set up", and the text must say which file is missing
    rather than claim the whole folder is absent."""
    (tmp_path / ".agenttalk").mkdir()
    check = doctor._check_init(Store(tmp_path))
    assert (check.name, check.status) == ("store.initialized", "error")
    assert check.details == f"not set up yet: .agenttalk/config.json not found at {tmp_path}"
    assert ".agenttalk/ found" not in check.details
    assert "`agenttalk init --here --agents <your agent names>`" in check.fix
    assert [p.name for p in (tmp_path / ".agenttalk").iterdir()] == []  # a diagnostic adds nothing


def test_doctor_not_set_up_run_keeps_result_exit_code_and_json_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.delenv("AGENTTALK_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["doctor", "--json"]) == 2
    report = json.loads(capsys.readouterr().out)
    check = report["checks"][0]
    assert report["overall"] == "error"
    assert set(check) == {"name", "status", "details", "fix", "data"}  # the same keys as before
    assert check["data"] is None
    assert (check["name"], check["status"]) == ("store.initialized", "error")
    assert check["details"].startswith("not set up yet: .agenttalk/config.json not found at ")
    assert cli.main(["doctor"]) == 2
    text = capsys.readouterr().out
    assert "not set up yet" in text
    assert "overall: ERROR" in text
    # no extra line promises the ERROR goes away after `init`: on Windows powershell_host
    # still fails until `supervise --select-pwsh`
    assert "will clear" not in text and "then run doctor again" not in text
    assert not (tmp_path / ".agenttalk").exists()


def _stub_skill_findings(monkeypatch: pytest.MonkeyPatch, findings) -> None:
    from agenttalk import skill_currency as skc

    monkeypatch.setattr(skc, "check_bundled_skills", lambda *a, **k: list(findings))


def _lag(n: int):
    from agenttalk import skill_currency as skc

    return [
        skc.Finding(f"skills/s{i}.md", 0, "reviewed-against",
                    "reviewed-against 0.92 lags package 0.104 (re-review against current CLI)",
                    level="warn")
        for i in range(n)
    ]


def test_skill_currency_advisory_only_is_a_note_for_the_maintainers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    findings = _lag(41)
    _stub_skill_findings(monkeypatch, findings)
    check = doctor._check_skill_currency()
    assert check.status == "warn"
    assert check.details.startswith("41 bundled skills were last reviewed against an older agenttalk")
    assert check.fix == "nothing to do: this is a note for the maintainers of the bundled skills"
    assert "re-stamp" not in check.details + check.fix
    # every finding stays in the JSON data
    assert len(check.data["findings"]) == 41
    assert check.data["findings"][0] == {
        "file": "skills/s0.md", "line": 0, "token": "reviewed-against",
        "reason": findings[0].reason, "level": "warn",
    }


def test_skill_currency_with_blocking_findings_also_names_the_maintainers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agenttalk import skill_currency as skc

    blocking = skc.Finding("skills/a.md", 3, "agenttalk nope", "unknown CLI command", level="error")
    _stub_skill_findings(monkeypatch, [blocking, *_lag(2)])
    check = doctor._check_skill_currency()
    assert check.status == "warn"
    assert check.details.startswith("1 blocking + 2 advisory skill-currency issue(s): ")
    assert "skills/a.md:3 unknown CLI command" in check.details
    # the maintainer advice is kept, but it now says who it is for and what the reader should do
    assert "maintainers" in check.fix
    assert "report it" in check.fix
    assert "fix blocking drift" in check.fix
    assert "re-stamp reviewed-against" in check.fix
    assert len(check.data["findings"]) == 3


def test_skill_currency_with_no_findings_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_skill_findings(monkeypatch, [])
    check = doctor._check_skill_currency()
    assert check.status == "ok" and check.fix == ""
    assert check.details == "bundled skills pass currency lint (frontmatter + stamps + CLI tokens)"


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


def test_doctor_warns_when_supervisor_deadman_config_is_ignored(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.init(["alpha", "beta"])
    (store.dir / "supervisor.json").write_text(
        json.dumps({"deadman": {"mail_age_slo_seconds": 60}}),
        encoding="utf-8",
    )

    report = doctor.run(tmp_path)

    check = next(c for c in report.checks if c.name == "deadman_config_source")
    assert check.status == "warn"
    assert "supervisor.json contains a deadman block that is ignored" in check.details
    assert "config.json" in check.details
    assert ".agenttalk/config.json" in check.fix


def test_doctor_ovh_qwen_gateway_is_allowlisted_and_checks_ambient_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = Store(tmp_path)
    store.init(["lead", "qwen-dev-1"])
    store.set_role("lead", "lead")
    store.set_operator_facing("lead")
    store.set_trust_class("qwen-dev-1", "external-worker")
    (store.dir / "supervisor.json").write_text(
        json.dumps({
            "schema_version": 2,
            "agents": {
                "qwen-dev-1": {
                    "backend_profile": "ovh-qwen",
                    "trust_class": "external-worker",
                },
            },
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        ovh_gateway_service,
        "gateway_status",
        lambda _root: {
            "ready": True,
            "errors": [],
            "price_policy_hash": "a" * 64,
            "committed_micro_eur": 123,
            "unresolved_count": 0,
            "ledger": {
                "opening_micro_eur": 580_000,
                "opening_evidence": (
                    "OVH AI Endpoints dashboard, observed 2026-07-16 morning"
                ),
                "opening_observed_at": "2026-07-16T08:00:00.000000Z",
                "opening_period": "2026-07",
            },
        },
    )
    monkeypatch.delenv("OVH_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    check = doctor._check_ovh_qwen_gateway(store)

    assert check is not None
    assert check.status == "ok"
    assert set(check.data or {}) == {
        "ready",
        "errors",
        "price_policy_hash",
        "committed_micro_eur",
        "unresolved_count",
        "ledger",
    }
    assert check.data["ledger"]["opening_micro_eur"] == 580_000
    assert "OVH AI Endpoints dashboard" in check.data["ledger"]["opening_evidence"]

    ovh_secret = "must-not-be-reported-ovh-secret"
    anthropic_secret = "must-not-be-reported-anthropic-secret"
    monkeypatch.setenv("OVH_KEY", ovh_secret)
    monkeypatch.setenv("ANTHROPIC_API_KEY", anthropic_secret)
    check = doctor._check_ovh_qwen_gateway(store)
    assert check is not None
    assert check.status == "error"
    assert "supervisor_ambient_provider_key" in check.details
    rendered = json.dumps({
        "details": check.details,
        "fix": check.fix,
        "data": check.data,
    })
    assert ovh_secret not in rendered
    assert anthropic_secret not in rendered
    assert "OVH_KEY" not in rendered
    assert "ANTHROPIC_API_KEY" not in rendered


def _external_worker_commit_gate_check(report: doctor.Report) -> doctor.Check:
    return next(c for c in report.checks if c.name == "external_worker_commit_gate")


def _write_doctor_commit_gate_policy(
    path: Path,
    agents: dict[str, dict],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema_version": 1,
        "agents": agents,
    }), encoding="utf-8")


def _write_doctor_supervisor(store: Store, agents: dict[str, dict]) -> None:
    (store.dir / "supervisor.json").write_text(
        json.dumps({"agents": agents}),
        encoding="utf-8",
    )


def test_doctor_warns_external_worker_without_commit_gate_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = Store(tmp_path)
    store.init(["lead", "qwen-dev-1", "remote-dev-2"])
    store.set_trust_class("qwen-dev-1", "external-worker")
    store.set_trust_class("remote-dev-2", "external-worker")
    monkeypatch.delenv(POLICY_ENV, raising=False)

    check = _external_worker_commit_gate_check(doctor.run(tmp_path))

    assert check.status == "warn"
    assert "qwen-dev-1" in check.details
    assert "remote-dev-2" in check.details
    assert check.data["ungated_agents"] == ["qwen-dev-1", "remote-dev-2"]


def test_doctor_gated_supervisor_external_worker_and_normal_agent_do_not_warn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = Store(tmp_path)
    store.init(["lead", "qwen-dev-1", "native-dev"])
    policy = tmp_path / "commit-gate-policy.json"
    policy.write_text(json.dumps({
        "schema_version": 1,
        "agents": {"qwen-dev-1": {"grade": "detection", "enabled": True}},
    }), encoding="utf-8")
    (store.dir / "supervisor.json").write_text(json.dumps({
        "agents": {
            "qwen-dev-1": {
                "trust_class": "external-worker",
                "env": {POLICY_ENV: str(policy)},
            },
            "native-dev": {},
        },
    }), encoding="utf-8")
    monkeypatch.delenv(POLICY_ENV, raising=False)

    check = _external_worker_commit_gate_check(doctor.run(tmp_path))

    assert check.status == "ok"
    assert check.data["external_workers"] == ["qwen-dev-1"]
    assert check.data["ungated_agents"] == []
    assert "native-dev" not in check.details


def test_doctor_external_worker_commit_gate_absent_configs_do_not_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Store(tmp_path).init(["lead", "native-dev"])
    monkeypatch.delenv(POLICY_ENV, raising=False)

    report = doctor.run(tmp_path)

    assert all(c.name != "external_worker_commit_gate" for c in report.checks)


def test_doctor_external_worker_commit_gate_unreadable_supervisor_does_not_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = Store(tmp_path)
    store.init(["lead", "qwen-dev-1"])
    store.set_trust_class("qwen-dev-1", "external-worker")
    (store.dir / "supervisor.json").write_text("{", encoding="utf-8")
    monkeypatch.delenv(POLICY_ENV, raising=False)

    check = _external_worker_commit_gate_check(doctor.run(tmp_path))

    assert check.status == "warn"
    assert check.data["ungated_agents"] == ["qwen-dev-1"]


def test_doctor_supervisor_only_external_worker_blocked_policy_warns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = Store(tmp_path)
    store.init(["lead"])
    policy = tmp_path / "commit-gate-policy.json"
    policy.write_text("{", encoding="utf-8")
    (store.dir / "supervisor.json").write_text(json.dumps({
        "agents": {
            "remote-dev": {
                "trust_class": "external-worker",
                "env": {POLICY_ENV: str(policy)},
            },
        },
    }), encoding="utf-8")
    monkeypatch.delenv(POLICY_ENV, raising=False)

    check = _external_worker_commit_gate_check(doctor.run(tmp_path))

    assert check.status == "warn"
    assert "present but unusable" in check.details
    assert "policy_status=blocked_policy" in check.details
    assert check.data["ungated_agents"] == ["remote-dev"]
    assert check.data["unusable_policy_agents"] == ["remote-dev"]


def test_doctor_malformed_external_worker_policy_path_warns_without_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = Store(tmp_path)
    store.init(["lead"])
    (store.dir / "supervisor.json").write_text(json.dumps({
        "agents": {
            "remote-dev": {
                "trust_class": "external-worker",
                "env": {POLICY_ENV: "\0"},
            },
        },
    }), encoding="utf-8")
    monkeypatch.delenv(POLICY_ENV, raising=False)
    original_resolve = Path.resolve

    def resolve(path: Path, *args: object, **kwargs: object) -> Path:
        if "\0" in str(path):
            raise OSError("unresolvable policy path")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)

    report = doctor.run(tmp_path)
    check = _external_worker_commit_gate_check(report)

    assert check.status == "warn"
    assert check.data["ungated_agents"] == ["remote-dev"]
    assert check.data["unusable_policy_agents"] == ["remote-dev"]


def test_doctor_unresolvable_roster_policy_keeps_both_checks_non_green(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = Store(tmp_path)
    store.init(["lead", "qwen-dev-1"])
    store.set_trust_class("qwen-dev-1", "external-worker")
    policy = tmp_path / "commit-gate-policy.json"
    monkeypatch.setenv(POLICY_ENV, str(policy))
    original_resolve = Path.resolve

    def resolve(path: Path, *args: object, **kwargs: object) -> Path:
        if path == policy:
            raise OSError("unresolvable policy path")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)

    report = doctor.run(tmp_path)
    wrapped = next(c for c in report.checks if c.name == "wrapped_commit_gate")
    external = _external_worker_commit_gate_check(report)

    assert wrapped.status == "error"
    assert external.status == "warn"
    assert "policy_status=blocked_policy" in external.details
    assert external.data["ungated_agents"] == ["qwen-dev-1"]
    assert external.data["unusable_policy_agents"] == ["qwen-dev-1"]
    assert external.data["stronger_error_agents"] == ["qwen-dev-1"]


def test_doctor_supervisor_relative_policy_uses_each_worker_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = Store(tmp_path)
    store.init(["lead"])
    relative_policy = Path("policies") / "commit-gate.json"
    ready_cwd = tmp_path / "ready-worker"
    missing_cwd = tmp_path / "missing-worker"
    _write_doctor_commit_gate_policy(
        ready_cwd / relative_policy,
        {"remote-ready": {"grade": "detection", "enabled": True}},
    )
    _write_doctor_commit_gate_policy(
        tmp_path / relative_policy,
        {"remote-root": {"grade": "detection", "enabled": True}},
    )
    _write_doctor_supervisor(store, {
        "remote-ready": {
            "trust_class": "external-worker",
            "cwd": str(ready_cwd),
            "env": {POLICY_ENV: str(relative_policy)},
        },
        "remote-root": {
            "trust_class": "external-worker",
            "env": {POLICY_ENV: str(relative_policy)},
        },
        "remote-missing": {
            "trust_class": "external-worker",
            "cwd": str(missing_cwd),
            "env": {POLICY_ENV: str(relative_policy)},
        },
    })
    monkeypatch.delenv(POLICY_ENV, raising=False)

    check = _external_worker_commit_gate_check(doctor.run(tmp_path))
    policies = {row["agent"]: row for row in check.data["policies"]}

    assert check.status == "warn"
    assert check.data["ungated_agents"] == ["remote-missing"]
    assert policies["remote-ready"]["policy_status"] == "active"
    assert policies["remote-root"]["policy_status"] == "active"
    assert policies["remote-missing"]["policy_status"] == "blocked_policy"


def test_doctor_supervisor_policy_override_beats_stale_ambient_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = Store(tmp_path)
    store.init(["lead"])
    worker_cwd = tmp_path / "worker"
    relative_policy = Path("config") / "commit-gate.json"
    _write_doctor_commit_gate_policy(
        worker_cwd / relative_policy,
        {"remote-dev": {"grade": "detection", "enabled": True}},
    )
    _write_doctor_supervisor(store, {
        "remote-dev": {
            "trust_class": "external-worker",
            "cwd": str(worker_cwd),
            "env": {POLICY_ENV: str(relative_policy)},
        },
    })
    monkeypatch.setenv(POLICY_ENV, str(tmp_path / "stale-policy.json"))

    check = _external_worker_commit_gate_check(doctor.run(tmp_path))

    assert check.status == "ok"
    assert check.data["ungated_agents"] == []
    assert check.data["policies"][0]["policy_path"] == str(relative_policy)


def test_doctor_lowercase_supervisor_policy_override_disables_ambient_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = Store(tmp_path)
    store.init(["lead"])
    ambient_policy = tmp_path / "ambient-policy.json"
    _write_doctor_commit_gate_policy(
        ambient_policy,
        {"remote-dev": {"grade": "detection", "enabled": True}},
    )
    _write_doctor_supervisor(store, {
        "remote-dev": {
            "trust_class": "external-worker",
            "env": {POLICY_ENV.lower(): ""},
        },
    })
    monkeypatch.setenv(POLICY_ENV, str(ambient_policy))

    check = _external_worker_commit_gate_check(doctor.run(tmp_path))

    assert check.status == "warn"
    assert check.data["ungated_agents"] == ["remote-dev"]
    assert check.data["policies"][0]["policy_path"] == ""
    assert check.data["policies"][0]["policy_status"] == "not_owed"


def test_doctor_supervisor_missing_override_is_not_masked_by_valid_ambient(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = Store(tmp_path)
    store.init(["lead"])
    ambient_policy = tmp_path / "ambient-policy.json"
    _write_doctor_commit_gate_policy(
        ambient_policy,
        {"remote-dev": {"grade": "detection", "enabled": True}},
    )
    _write_doctor_supervisor(store, {
        "remote-dev": {
            "trust_class": "external-worker",
            "cwd": str(tmp_path / "worker"),
            "env": {POLICY_ENV: str(Path("config") / "missing-policy.json")},
        },
    })
    monkeypatch.setenv(POLICY_ENV, str(ambient_policy))

    check = _external_worker_commit_gate_check(doctor.run(tmp_path))

    assert check.status == "warn"
    assert check.data["ungated_agents"] == ["remote-dev"]
    assert check.data["unusable_policy_agents"] == ["remote-dev"]


def test_doctor_supervisor_external_worker_blocked_security_policy_warns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = Store(tmp_path)
    store.init(["lead"])
    policy = tmp_path / "security-policy.json"
    _write_doctor_commit_gate_policy(
        policy,
        {"remote-dev": {"grade": "security", "enabled": True}},
    )
    _write_doctor_supervisor(store, {
        "remote-dev": {
            "trust_class": "external-worker",
            "env": {POLICY_ENV: str(policy)},
        },
    })
    monkeypatch.delenv(POLICY_ENV, raising=False)

    check = _external_worker_commit_gate_check(doctor.run(tmp_path))
    policy_row = check.data["policies"][0]

    assert check.status == "warn"
    assert "policy_status=blocked" in check.details
    assert check.data["ungated_agents"] == ["remote-dev"]
    assert check.data["unusable_policy_agents"] == ["remote-dev"]
    assert policy_row["policy_status"] == "blocked"


def test_doctor_supervisor_external_worker_disabled_policy_reports_inactive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = Store(tmp_path)
    store.init(["lead"])
    policy = tmp_path / "disabled-policy.json"
    _write_doctor_commit_gate_policy(
        policy,
        {"remote-dev": {"grade": "detection", "enabled": False}},
    )
    _write_doctor_supervisor(store, {
        "remote-dev": {
            "trust_class": "external-worker",
            "env": {POLICY_ENV: str(policy)},
        },
    })
    monkeypatch.delenv(POLICY_ENV, raising=False)

    check = _external_worker_commit_gate_check(doctor.run(tmp_path))
    policy_row = check.data["policies"][0]

    assert check.status == "warn"
    assert "policy_status=inactive" in check.details
    assert check.data["ungated_agents"] == ["remote-dev"]
    assert policy_row["policy_status"] == "inactive"


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


def test_doctor_heartbeat_check_downgrades_on_strict_cli_child_verdict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#105: a fresh heartbeat is the wrapper's OWN self-report - it cannot
    notice its own CLI child dying, so `doctor` would otherwise keep calling
    it "ok". A verdict the CLI-child-or-wrapper-is-GONE predicate recognizes
    (round-2 review: matched by the CLI_CHILD_* family + named terminal set,
    not a hand list) downgrades this to `error`; any OTHER not-confirmed-
    healthy verdict (an operational state the fail-closed allowlist still
    does not call healthy) downgrades to at least `warn`, distinct from a
    genuinely healthy agent. The heartbeat detail is kept as secondary
    context, not dropped."""
    s = Store(tmp_path)
    s.init(["dead", "held", "fine"])
    s.write_heartbeat("dead")
    s.write_heartbeat("held")
    s.write_heartbeat("fine")

    def fake_observation(store, *, now_epoch, state, supervisor_config,
                         snapshot, event_limit):
        assert snapshot is None, "#105: must never scan the OS process tree"
        return {"agents": [
            {"name": "dead", "decision": {"state": "STUCK_OR_DEAD", "action": "warn_only"}},
            {"name": "held", "decision": {"state": "CONFIG_BLOCKED", "action": "none"}},
        ]}

    monkeypatch.setattr(doctor.sup, "build_supervisor_observation", fake_observation)
    report = doctor.run(tmp_path)
    hb_by_name = {c.name: c for c in report.checks if c.name.startswith("heartbeat.")}

    assert hb_by_name["heartbeat.dead"].status == "error"
    assert "STUCK_OR_DEAD" in hb_by_name["heartbeat.dead"].details
    assert "last seen" in hb_by_name["heartbeat.dead"].details  # demoted, not dropped

    # CONFIG_BLOCKED is not in the CLI_CHILD_*/terminal "gone" family, but it
    # is ALSO not one of the two confirmed-healthy states - the fail-closed
    # default must still downgrade it, never fall through to "ok".
    assert hb_by_name["heartbeat.held"].status == "warn"
    assert "CONFIG_BLOCKED" in hb_by_name["heartbeat.held"].details

    # An agent with no strict verdict at all (unmanaged) keeps today's
    # heartbeat-only behavior - a fresh heartbeat is still "ok".
    assert hb_by_name["heartbeat.fine"].status == "ok"

    # A confirmed-dead heartbeat check must fail the whole run (exit 2).
    assert report.overall == "error"


@pytest.mark.parametrize("verdict_state", _all_planner_states())
def test_doctor_heartbeat_check_never_reports_ok_for_a_non_healthy_verdict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    verdict_state: str,
) -> None:
    """#105 round-2/3: drive EVERY planner-emittable state through the
    doctor heartbeat check with an otherwise-perfectly-fresh heartbeat. Only
    the two states that confirm the child healthy (sup.CLI_CHILD_HEALTHY_STATES)
    may leave the check at "ok"; the gone family (minus the CLI_CHILD_STARTING
    display exception - round-3 review: it is the SAME lifecycle moment as
    LAUNCHING, not a sign of trouble) downgrades to "error"; everything else,
    known or not, downgrades to at least "warn". Parametrized over the
    extracted state list so a new state the planner grows is exercised
    automatically, not just the ones this test's author happened to think of."""
    s = Store(tmp_path)
    s.init(["alpha"])
    s.write_heartbeat("alpha")

    def fake_observation(store, *, now_epoch, state, supervisor_config,
                         snapshot, event_limit):
        return {"agents": [
            {"name": "alpha", "decision": {"state": verdict_state, "action": "none"}},
        ]}

    monkeypatch.setattr(doctor.sup, "build_supervisor_observation", fake_observation)
    report = doctor.run(tmp_path)
    check = next(c for c in report.checks if c.name == "heartbeat.alpha")

    if sup.cli_child_verdict_is_healthy(verdict_state):
        assert check.status == "ok", (verdict_state, check)
    elif (sup.cli_child_verdict_is_gone(verdict_state)
          and not sup.cli_child_verdict_is_launching(verdict_state)):
        assert check.status == "error", (verdict_state, check)
    else:
        assert check.status == "warn", (verdict_state, check)


def test_doctor_heartbeat_check_treats_cli_child_starting_like_launching(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """round-3 review minor: CLI_CHILD_STARTING and LAUNCHING are the SAME
    lifecycle moment (an agent coming up normally), but CLI_CHILD_STARTING
    used to get "error" (it falls inside the gone family's CLI_CHILD_*
    prefix match, which the planner's own bookkeeping depends on) while
    LAUNCHING got "warn" - display-only misalignment, not a planner change."""
    s = Store(tmp_path)
    s.init(["starting", "launching"])
    s.write_heartbeat("starting")
    s.write_heartbeat("launching")

    def fake_observation(store, *, now_epoch, state, supervisor_config,
                         snapshot, event_limit):
        return {"agents": [
            {"name": "starting", "decision": {"state": "CLI_CHILD_STARTING", "action": "none"}},
            {"name": "launching", "decision": {"state": "LAUNCHING", "action": "none"}},
        ]}

    monkeypatch.setattr(doctor.sup, "build_supervisor_observation", fake_observation)
    report = doctor.run(tmp_path)
    hb_by_name = {c.name: c for c in report.checks if c.name.startswith("heartbeat.")}

    assert hb_by_name["heartbeat.starting"].status == "warn"
    assert hb_by_name["heartbeat.launching"].status == "warn"


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
    # claude side ships challenge, consult, handoff, lead, listen, propose,
    # send; 1 differs (we mutated send), the rest are unchanged. Derive the
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


def _write_stale_heartbeat(store: Store, agent: str = "lead") -> None:
    (store.state_dir / f"{agent}.heartbeat").write_text(
        "2026-01-01T00:00:00Z", encoding="utf-8")


def _write_claude_post_tool_hook(root: Path, command: str) -> None:
    settings = root / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({"hooks": {"PostToolUse": [
        {"matcher": "*", "hooks": [{"type": "command", "command": command}]},
    ]}}), encoding="utf-8")


def test_operator_facing_missing_heartbeat_no_hook_suggests_interactive_install(
    tmp_path: Path,
) -> None:
    s = Store(tmp_path)
    s.init(["lead", "w1"])
    s.set_operator_facing("lead")

    c = doctor._check_operator_facing(s)

    assert c.status == "warn"
    assert "never listened" in c.details
    assert "interactive-for lead" in c.fix
    assert "wait --for lead" in c.fix


def test_operator_facing_stale_neutral_hook_mentions_agenttalk_self(
    tmp_path: Path,
) -> None:
    s = Store(tmp_path)
    s.init(["lead", "w1"])
    s.set_operator_facing("lead")
    _write_stale_heartbeat(s)
    _write_claude_post_tool_hook(tmp_path, "agenttalk heartbeat --hook")

    c = doctor._check_operator_facing(s)

    assert c.status == "warn"
    assert "neutral" in c.details
    assert "AGENTTALK_SELF" in c.fix
    assert "interactive-for lead" in c.fix


def test_operator_facing_stale_wrong_fallback_warns_wrong_identity(
    tmp_path: Path,
) -> None:
    s = Store(tmp_path)
    s.init(["lead", "w1"])
    s.set_operator_facing("lead")
    _write_stale_heartbeat(s)
    _write_claude_post_tool_hook(tmp_path, "agenttalk heartbeat --hook --fallback-for w1")

    c = doctor._check_operator_facing(s)

    assert c.status == "warn"
    assert "wrong identity" in c.details
    assert "interactive-for lead" in c.fix


def test_operator_facing_stale_matching_fallback_is_soft_reload_hint(
    tmp_path: Path,
) -> None:
    s = Store(tmp_path)
    s.init(["lead", "w1"])
    s.set_operator_facing("lead")
    _write_stale_heartbeat(s)
    _write_claude_post_tool_hook(tmp_path, "agenttalk heartbeat --hook --fallback-for lead")

    c = doctor._check_operator_facing(s)

    assert c.status == "warn"
    assert "fallback hook is installed" in c.details
    assert "reload" in c.fix


def test_operator_facing_fresh_suppresses_interactive_hook_warning(
    tmp_path: Path,
) -> None:
    s = Store(tmp_path)
    s.init(["lead", "w1"])
    s.set_operator_facing("lead")
    s.write_heartbeat("lead")
    _write_claude_post_tool_hook(tmp_path, "agenttalk heartbeat --hook")

    c = doctor._check_operator_facing(s)

    assert c.status == "ok"


def test_operator_facing_managed_or_wrapped_suppresses_interactive_hook_warning(
    tmp_path: Path,
) -> None:
    managed = Store(tmp_path / "managed")
    managed.init(["lead", "w1"])
    managed.set_operator_facing("lead")
    managed.set_managed_lead_loop("lead")
    assert doctor._check_operator_facing(managed).status == "ok"

    wrapped = Store(tmp_path / "wrapped")
    wrapped.init(["lead", "w1"])
    wrapped.set_operator_facing("lead")
    (wrapped.dir / "supervisor.json").write_text(
        json.dumps({"agents": {"lead": {"wrapped": True}}}), encoding="utf-8")
    assert doctor._check_operator_facing(wrapped).status == "ok"


def test_operator_facing_unreadable_hook_settings_warns_without_traceback(
    tmp_path: Path,
) -> None:
    s = Store(tmp_path)
    s.init(["lead", "w1"])
    s.set_operator_facing("lead")
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text("{not json", encoding="utf-8")

    c = doctor._check_operator_facing(s)

    assert c.status == "warn"
    assert "settings.json" in c.details
    assert "Traceback" not in c.details


def test_operator_facing_sole_lead_missing_heartbeat_uses_interactive_hint(
    tmp_path: Path,
) -> None:
    s = Store(tmp_path)
    s.init(["lead", "w1"])
    s.set_role("lead", "lead")

    c = doctor._check_operator_facing(s)

    assert c.status == "warn"
    assert "sole lead" in c.details
    assert "interactive-for lead" in c.fix


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


# --- 0.24.0: escalation-target nudge (WP01, FR-009) -----------------------

def _esc_check(report):
    return next((c for c in report.checks if c.name == "escalation_target"), None)


def test_doctor_escalation_target_warns_when_no_liaison_no_lead(tmp_path: Path) -> None:
    Store(tmp_path).init(["alpha", "beta"])
    chk = _esc_check(doctor.run(tmp_path))
    assert chk is not None and chk.status == "warn"
    assert "set-operator-facing" in chk.fix and "set-role" in chk.fix


def test_doctor_escalation_target_absent_with_liaison(tmp_path: Path) -> None:
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    s.set_operator_facing("alpha")
    assert _esc_check(doctor.run(tmp_path)) is None


def test_doctor_escalation_target_absent_with_lead(tmp_path: Path) -> None:
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    s.set_role("alpha", "lead")
    assert _esc_check(doctor.run(tmp_path)) is None


def test_doctor_escalation_target_absent_for_solo(tmp_path: Path) -> None:
    Store(tmp_path).init(["alpha"])
    assert _esc_check(doctor.run(tmp_path)) is None


# ============================ #163: task work orders ========================

def _task_avail_check(report):
    return next((c for c in report.checks if c.name == "task_kind_availability"), None)


def _declined_tasks_check(report):
    return next((c for c in report.checks if c.name == "declined_lead_tasks"), None)


def test_doctor_task_kind_availability_warns_with_no_lead_no_liaison(tmp_path: Path) -> None:
    Store(tmp_path).init(["alpha", "beta"])
    chk = _task_avail_check(doctor.run(tmp_path))
    assert chk is not None and chk.status == "warn"
    assert "no agent holds role=lead" in chk.details
    assert "set-role" in chk.fix and "set-operator-facing" in chk.fix


def test_doctor_task_kind_availability_absent_with_lead(tmp_path: Path) -> None:
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    s.set_role("alpha", "lead")
    assert _task_avail_check(doctor.run(tmp_path)) is None


def test_doctor_task_kind_availability_absent_with_liaison(tmp_path: Path) -> None:
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    s.set_operator_facing("alpha")
    assert _task_avail_check(doctor.run(tmp_path)) is None


def test_doctor_declined_lead_tasks_absent_with_no_task_threads(tmp_path: Path) -> None:
    Store(tmp_path).init(["alpha", "beta"])
    assert _declined_tasks_check(doctor.run(tmp_path)) is None


def test_doctor_declined_lead_tasks_warns_on_declined_fixture(tmp_path: Path) -> None:
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    s.set_role("alpha", "lead")
    opener = s.send(sender="alpha", recipient="beta", body="do X", kind="task",
                    subject="", meta={"request_id": "tk-fixture-1"})
    s.send(sender="beta", recipient="alpha", body="not doing this",
          kind="task-response",
          meta={"request_id": opener.meta["request_id"], "status": "declined",
                "reason": "out of scope"})
    chk = _declined_tasks_check(doctor.run(tmp_path))
    assert chk is not None and chk.status == "warn"
    assert "1 task(s) declined" in chk.details
    assert chk.data["declined"][0]["reason"] == "out of scope"
    assert chk.data["stale"] == []


def test_doctor_declined_lead_tasks_errors_on_unanswered_past_staleness(
    tmp_path: Path,
) -> None:
    from datetime import datetime, timedelta, timezone

    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    s.set_role("alpha", "lead")
    s.send(sender="alpha", recipient="beta", body="do X", kind="task",
          meta={"request_id": "tk-fixture-2"})
    # Backdate the opener's own on-disk message ts well past the staleness
    # window instead of monkeypatching time - `derive_threads` computes
    # age off the message's own timestamp. Exactly one message exists at
    # this point (the opener, no response yet).
    path = next(s.messages_dir.glob("*.json"))
    raw = json.loads(path.read_text(encoding="utf-8"))
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat().replace(
        "+00:00", "Z")
    raw["ts"] = old_ts
    path.write_text(json.dumps(raw), encoding="utf-8")
    chk = _declined_tasks_check(doctor.run(tmp_path))
    assert chk is not None and chk.status == "error"
    assert "unanswered past" in chk.details
    assert chk.data["stale"][0]["request_id"] == "tk-fixture-2"
    assert chk.data["stale"][0]["from"] == "alpha"


# ----- 0.55.1: supervised-codex L4 observability -----------------------

def _write_supervisor(store: Store, agents: dict) -> None:
    (store.dir / "supervisor.json").write_text(
        json.dumps({"agents": agents}), encoding="utf-8")


def _fake_exe(path: Path) -> str:
    path.write_text("fake", encoding="utf-8")
    return str(path)


def _write_agenttalk_cmd(store: Store, py: str) -> None:
    p = store.dir / "bin" / "agenttalk.cmd"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f'@echo off\r\nif not defined AGENTTALK_PYTHON set "AGENTTALK_PYTHON={py}"\r\n',
                 encoding="utf-8")


def _seed_codex_home(store: Store, agent: str) -> Path:
    p = store.dir / "codex-home" / agent
    p.mkdir(parents=True)
    return p


def test_doctor_supervised_codex_absent_without_supervisor_json(tmp_path: Path) -> None:
    s = Store(tmp_path)
    s.init(["worker"])
    assert doctor._check_supervised_codex(s) is None     # no supervisor.json -> additive absent


@pytest.mark.parametrize("payload", [[1, 2, 3], "not an object"])
def test_doctor_supervised_codex_ignores_non_dict_supervisor_json(tmp_path: Path, payload: object) -> None:
    s = Store(tmp_path)
    s.init(["cdx"])
    (s.dir / "supervisor.json").write_text(json.dumps(payload), encoding="utf-8")

    assert doctor._check_supervised_codex(s) is None
    report = doctor.run(tmp_path)
    assert not any(c.name == "supervised_codex" for c in report.checks)


def test_doctor_supervisor_script_guard_absent_for_new_generated_script(tmp_path: Path) -> None:
    s = Store(tmp_path)
    s.init(["worker"])
    assert cli.main(["--root", str(tmp_path), "supervise", "--init"]) == 0

    assert doctor._check_supervisor_script_guard(s) is None


def test_doctor_supervisor_script_missing_singleton_guard_warns_advisory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    s = Store(tmp_path)
    s.init(["worker"])
    (s.dir / "supervisor.ps1").write_text(
        "# agenttalk supervisor\n"
        "& $AgenttalkCmd --root $Root supervise --plan\n",
        encoding="utf-8",
    )

    chk = doctor._check_supervisor_script_guard(s)
    assert chk is not None
    assert chk.status == "warn"
    assert chk.name == "supervisor_script"
    assert "--claim-instance" in chk.details
    assert "supervise --init --force" in chk.fix

    # The full doctor exit code and overall severity describe the whole host
    # environment. On a clean CI runner, unrelated missing skill installs may
    # make the process exit ERROR; this test only owns the advisory check above.
    cli.main(["--root", str(tmp_path), "doctor"])
    out = capsys.readouterr().out
    assert "supervisor_script" in out


def test_doctor_supervised_codex_ok_requires_full_env_mirror(tmp_path: Path) -> None:
    s = Store(tmp_path)
    s.init(["cdx"])
    codex = _fake_exe(tmp_path / "codex.exe")
    py = _fake_exe(tmp_path / "python.exe")
    cwd = tmp_path / "work"
    cwd.mkdir()
    (tmp_path / "src" / "agenttalk").mkdir(parents=True)
    (tmp_path / "src" / "agenttalk" / "__init__.py").write_text("", encoding="utf-8")
    home = _seed_codex_home(s, "cdx")
    _write_agenttalk_cmd(s, py)
    _write_supervisor(s, {"cdx": {"cli": "codex",
                                  "cwd": str(cwd),
                                  "launch": {"windows_file": codex}}})
    calls: list[dict] = []

    def runner(exe, args, timeout, call_cwd, env):
        assert timeout == 5.0
        calls.append({"exe": exe, "args": args, "cwd": call_cwd, "env": env})
        assert args != ["sandbox", "--help"]
        if args == ["--version"]:
            return (0, "codex-cli 0.142.3\n")
        if args == ["-m", "agenttalk", "--version"]:
            return (0, "agenttalk 0.55.1\n")
        raise AssertionError(args)

    chk = doctor._check_supervised_codex(s, runner=runner)
    assert chk.status == "ok"
    entry = chk.data["codex"][0]
    assert entry["base_cli"] == codex
    assert entry["version"] == "codex-cli 0.142.3"
    assert entry["agenttalk_py"] == py
    assert entry["agenttalk_py_provenance"] == "agenttalk.cmd"
    assert entry["codex_home_status"] == "existing"
    assert entry["env_mirror"] == "full"
    assert entry["sandbox_probe_status"] == "skipped"
    assert {tuple(c["args"]) for c in calls} == {
        ("--version",), ("-m", "agenttalk", "--version"),
    }
    for c in calls:
        assert c["cwd"] == cwd
        assert c["env"]["AGENTTALK_ROOT"] == str(tmp_path.resolve())
        assert c["env"]["AGENTTALK_PY"] == py
        assert c["env"]["CODEX_HOME"] == str(home)
        assert c["env"]["PYTHONPATH"].split(os.pathsep)[0] == str(tmp_path / "src")


@pytest.mark.parametrize("env_key", ["AGENTTALK_PY", "agenttalk_py", "Agenttalk_Py"])
def test_doctor_supervised_codex_agent_env_agenttalk_py_override_warns(
    tmp_path: Path, env_key: str,
) -> None:
    s = Store(tmp_path)
    s.init(["cdx"])
    codex = _fake_exe(tmp_path / "codex.exe")
    py = _fake_exe(tmp_path / "python.exe")
    missing_py = str(tmp_path / "missing-python.exe")
    _write_agenttalk_cmd(s, py)
    _seed_codex_home(s, "cdx")
    _write_supervisor(s, {"cdx": {
        "cli": "codex",
        "env": {env_key: missing_py},
        "launch": {"windows_file": codex},
    }})
    agenttalk_probe_exes: list[str] = []

    def runner(exe, args, timeout, cwd, env):
        assert env["AGENTTALK_PY"] == missing_py
        if args == ["--version"]:
            return (0, "codex-cli 0.142.3\n")
        if args == ["-m", "agenttalk", "--version"]:
            agenttalk_probe_exes.append(exe)
            return (None, "FileNotFoundError")
        raise AssertionError(args)

    chk = doctor._check_supervised_codex(s, runner=runner)
    entry = chk.data["codex"][0]
    assert chk.status == "warn"
    assert entry["agenttalk_py"] == py
    assert entry["agenttalk_probe_status"] == "warn"
    assert entry["env_mirror"] == "partial"
    assert agenttalk_probe_exes == [missing_py]
    assert "AGENTTALK_PY" in chk.details


@pytest.mark.parametrize("env_key", ["CODEX_HOME", "codex_home"])
def test_doctor_supervised_codex_agent_env_codex_home_override_warns(
    tmp_path: Path, env_key: str,
) -> None:
    s = Store(tmp_path)
    s.init(["cdx"])
    codex = _fake_exe(tmp_path / "codex.exe")
    py = _fake_exe(tmp_path / "python.exe")
    expected_home = _seed_codex_home(s, "cdx")
    override_home = tmp_path / "override-codex-home"
    override_home.mkdir()
    _write_agenttalk_cmd(s, py)
    _write_supervisor(s, {"cdx": {
        "cli": "codex",
        "env": {env_key: str(override_home)},
        "launch": {"windows_file": codex},
    }})
    seen_homes: list[str] = []

    def runner(exe, args, timeout, cwd, env):
        seen_homes.append(env["CODEX_HOME"])
        return (0, "codex-cli 0.142.3\n") if args == ["--version"] else (0, "agenttalk 0.55.1\n")

    chk = doctor._check_supervised_codex(s, runner=runner)
    entry = chk.data["codex"][0]
    assert chk.status == "warn"
    assert entry["codex_home_path"] == str(expected_home)
    assert entry["env_mirror"] == "partial"
    assert seen_homes == [str(override_home), str(override_home)]
    assert "CODEX_HOME" in chk.details


def test_doctor_supervised_codex_wrapped_probes_base_tail_and_wrapper_python(tmp_path: Path) -> None:
    s = Store(tmp_path)
    s.init(["wcdx"])
    codex = _fake_exe(tmp_path / "real-codex.exe")
    wrapper_py = _fake_exe(tmp_path / "wrapper-python.exe")
    _seed_codex_home(s, "wcdx")
    _write_supervisor(s, {"wcdx": {
        "cli": "codex", "wrapped": True,
        "launch": {"windows_file": wrapper_py,
                   "windows_args": ["-m", "agenttalk", "wrap", "--for", "wcdx",
                                    "--cli", "codex", "--loop", "--", codex,
                                    "--disable", "hooks"]}}})
    calls: list[tuple[str, tuple[str, ...]]] = []

    def runner(exe, args, timeout, cwd, env):
        calls.append((exe, tuple(args)))
        if args == ["--version"]:
            return (0, "codex-cli 0.142.3\n")
        if args == ["-m", "agenttalk", "--version"]:
            return (0, "agenttalk 0.55.1\n")
        raise AssertionError(args)

    chk = doctor._check_supervised_codex(s, runner=runner)
    assert chk.status == "ok"
    entry = chk.data["codex"][0]
    assert entry["base_cli"] == codex
    assert entry["wrapper_python"] == wrapper_py
    assert entry["agenttalk_py"] == wrapper_py
    assert entry["agenttalk_py_provenance"] == "launch.windows_file"
    assert (codex, ("--version",)) in calls
    assert (wrapper_py, ("-m", "agenttalk", "--version")) in calls


def _wrapped_codex_runner(exe, args, timeout, cwd, env):
    if args == ["--version"]:
        return (0, "codex-cli 0.142.3\n")
    if args == ["-m", "agenttalk", "--version"]:
        return (0, "agenttalk 0.58.1\n")
    raise AssertionError(args)


def _write_wrapped_codex(s: Store, tmp_path: Path) -> None:
    codex = _fake_exe(tmp_path / "real-codex.exe")
    wrapper_py = _fake_exe(tmp_path / "wrapper-python.exe")
    _seed_codex_home(s, "wcdx")
    _write_supervisor(s, {"wcdx": {
        "cli": "codex", "wrapped": True,
        "launch": {"windows_file": wrapper_py,
                   "windows_args": ["-m", "agenttalk", "wrap", "--for", "wcdx",
                                    "--cli", "codex", "--loop", "--", codex]}}})


def test_doctor_supervised_codex_errors_on_runtime_preflight_blocker(tmp_path: Path) -> None:
    """dev-3 sign-off / launch-hardening wiring: a WRAPPED supervised Codex whose
    agenttalk runtime preflight returns a blocker makes supervised_codex an ERROR
    (launch-blocking) — blocker in details + fix + data[agenttalk_runtime], entries kept."""
    s = Store(tmp_path)
    s.init(["wcdx"])
    _write_wrapped_codex(s, tmp_path)
    blocker = "agenttalk import failed: out-of-workspace source checkout"
    seen: dict = {}

    def runtime_checker(root):
        seen["root"] = root
        return blocker

    chk = doctor._check_supervised_codex(
        s, runner=_wrapped_codex_runner, runtime_checker=runtime_checker)
    assert seen["root"] == s.root                       # passed store.root
    assert chk.status == "error"                        # launch-blocking, not warn
    assert "agenttalk-runtime-preflight-FAILED" in chk.details
    assert blocker in chk.details
    assert chk.fix == blocker
    assert chk.data["agenttalk_runtime"] == blocker
    assert chk.data["codex"]                            # entries preserved for observability


def test_doctor_supervised_codex_clean_runtime_preflight_no_error(tmp_path: Path) -> None:
    """A clean runtime preflight (checker returns None) does NOT force an error — the
    check falls through to its usual ok/warn verdict."""
    s = Store(tmp_path)
    s.init(["wcdx"])
    _write_wrapped_codex(s, tmp_path)
    chk = doctor._check_supervised_codex(
        s, runner=_wrapped_codex_runner, runtime_checker=lambda root: None)
    assert chk.status != "error"


def test_doctor_supervised_codex_non_wrapped_skips_runtime_preflight(tmp_path: Path) -> None:
    """The runtime preflight only gates WRAPPED codex agents — a non-wrapped agent is
    never launch-blocked by it, and the checker is not even consulted."""
    s = Store(tmp_path)
    s.init(["cdx"])
    codex = _fake_exe(tmp_path / "codex.exe")
    py = _fake_exe(tmp_path / "python.exe")
    cwd = tmp_path / "work"
    cwd.mkdir()
    (tmp_path / "src" / "agenttalk").mkdir(parents=True)
    (tmp_path / "src" / "agenttalk" / "__init__.py").write_text("", encoding="utf-8")
    _seed_codex_home(s, "cdx")
    _write_agenttalk_cmd(s, py)
    _write_supervisor(s, {"cdx": {"cli": "codex", "cwd": str(cwd),
                                  "launch": {"windows_file": codex}}})
    called = {"n": 0}

    def runtime_checker(root):
        called["n"] += 1
        return "should-not-block-a-non-wrapped-agent"

    chk = doctor._check_supervised_codex(
        s, runner=_wrapped_codex_runner, runtime_checker=runtime_checker)
    assert called["n"] == 0                             # not consulted for a non-wrapped agent
    assert chk.status != "error"


@pytest.mark.parametrize("windows_args, expected", [
    (["-m", "agenttalk", "wrap"], "missing --"),
    (["-m", "agenttalk", "wrap", "--"], "no real CLI tail"),
    (["-m", "agenttalk", "wrap", "--", "REPLACE: codex.exe"], "REPLACE"),
    (["-m", "agenttalk", "wrap", "--", "missing-codex.exe"], "not found"),
])
def test_doctor_supervised_codex_wrapped_bad_tail_warns(
    tmp_path: Path, windows_args: list[str], expected: str,
) -> None:
    s = Store(tmp_path)
    s.init(["wcdx"])
    wrapper_py = _fake_exe(tmp_path / "wrapper-python.exe")
    _seed_codex_home(s, "wcdx")
    _write_supervisor(s, {"wcdx": {
        "cli": "codex", "wrapped": True,
        "launch": {"windows_file": wrapper_py, "windows_args": windows_args}}})

    def runner(exe, args, timeout, cwd, env):
        return (0, "agenttalk 0.55.1\n")

    chk = doctor._check_supervised_codex(s, runner=runner)
    assert chk.status == "warn"
    assert expected in chk.details
    assert chk.data["codex"][0]["base_cli_status"] == "warn"


def test_doctor_supervised_codex_wrapped_shim_tail_warns(tmp_path: Path) -> None:
    s = Store(tmp_path)
    s.init(["wcdx"])
    wrapper_py = _fake_exe(tmp_path / "wrapper-python.exe")
    shim = _fake_exe(tmp_path / "codex.cmd")
    _seed_codex_home(s, "wcdx")
    _write_supervisor(s, {"wcdx": {
        "cli": "codex", "wrapped": True,
        "launch": {"windows_file": wrapper_py,
                   "windows_args": ["-m", "agenttalk", "wrap", "--", shim]}}})

    def runner(exe, args, timeout, cwd, env):
        return (0, "agenttalk 0.55.1\n")

    chk = doctor._check_supervised_codex(s, runner=runner)
    assert chk.status == "warn"
    assert "shim" in chk.details
    assert chk.data["codex"][0]["base_cli_status"] == "warn"


def test_doctor_supervised_codex_missing_codex_home_warns_partial_env(tmp_path: Path) -> None:
    s = Store(tmp_path)
    s.init(["cdx"])
    codex = _fake_exe(tmp_path / "codex.exe")
    py = _fake_exe(tmp_path / "python.exe")
    _write_agenttalk_cmd(s, py)
    _write_supervisor(s, {"cdx": {"cli": "codex", "launch": {"windows_file": codex}}})

    def runner(exe, args, timeout, cwd, env):
        return (0, "codex-cli 0.142.3\n") if args == ["--version"] else (0, "agenttalk 0.55.1\n")

    chk = doctor._check_supervised_codex(s, runner=runner)
    entry = chk.data["codex"][0]
    assert chk.status == "warn"
    assert entry["codex_home_status"] == "missing_expected"
    assert entry["env_mirror"] == "partial"


def test_doctor_supervised_codex_fallback_python_warns_doctor_fallback(tmp_path: Path) -> None:
    s = Store(tmp_path)
    s.init(["cdx"])
    codex = _fake_exe(tmp_path / "codex.exe")
    _write_supervisor(s, {"cdx": {
        "cli": "codex", "codex_home_isolation": False,
        "launch": {"windows_file": codex}}})

    def runner(exe, args, timeout, cwd, env):
        if args == ["-m", "agenttalk", "--version"]:
            assert env["AGENTTALK_PY"] == exe
        return (0, "codex-cli 0.142.3\n") if args == ["--version"] else (0, "agenttalk 0.55.1\n")

    chk = doctor._check_supervised_codex(s, runner=runner)
    entry = chk.data["codex"][0]
    assert chk.status == "warn"
    assert entry["agenttalk_py_provenance"] == "sys.executable"
    assert entry["env_mirror"] == "doctor_fallback"


@pytest.mark.parametrize("result, expected", [
    ((0, ""), "UNVERSIONED"),
    ((7, "boom"), "failed"),
])
def test_doctor_supervised_codex_runtime_probe_failures_warn_never_error(
    tmp_path: Path, result: tuple[int, str], expected: str,
) -> None:
    s = Store(tmp_path)
    s.init(["cdx"])
    codex = _fake_exe(tmp_path / "codex.exe")
    py = _fake_exe(tmp_path / "python.exe")
    _write_agenttalk_cmd(s, py)
    _seed_codex_home(s, "cdx")
    _write_supervisor(s, {"cdx": {"cli": "codex", "launch": {"windows_file": codex}}})

    def runner(exe, args, timeout, cwd, env):
        if args == ["--version"]:
            return result
        return (0, "agenttalk 0.55.1\n")

    chk = doctor._check_supervised_codex(s, runner=runner)
    assert chk.status == "warn"
    assert "error" not in chk.status
    assert expected in chk.details


def test_doctor_config_blocked_holds_warns_separately_and_ignores_malformed(tmp_path: Path) -> None:
    s = Store(tmp_path)
    s.init(["worker", "other"])
    s.write_config_blocked_hold("worker", summary="missing native codex path")
    bad = s.config_blocked_hold_path("other")
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text(json.dumps({"agent": "other", "state": "wrong"}), encoding="utf-8")

    chk = doctor._check_config_blocked_holds(s)
    assert chk.status == "warn"
    assert chk.name == "config_blocked_holds"
    assert "worker" in chk.details and "missing native codex path" in chk.details
    assert "request-restart" in chk.fix
    assert [h["agent"] for h in chk.data["holds"]] == ["worker"]
    assert any(c.name == "config_blocked_holds" for c in doctor.run(tmp_path).checks)


def test_check_codex_config_warns_on_duplicate_tables(tmp_path: Path, monkeypatch) -> None:
    """`doctor` must WARN (with a repair hint) when a codex config.toml holds duplicate
    [projects] tables — invalid TOML the codex CLI rejects — instead of returning ok as
    if the per-project block were healthy (codex-reviewer-1 P1, v0.75.3)."""
    from agenttalk import codex_config as cxc
    proj = tmp_path / "proj"
    proj.mkdir()
    cfg = tmp_path / "config.toml"
    cxc.enable_project(cfg, proj)
    block = cfg.read_text(encoding="utf-8-sig")
    cfg.write_text(block.strip("\n") + "\n\n" + block.strip("\n") + "\n", encoding="utf-8")
    monkeypatch.setattr(cxc, "default_config_path", lambda: cfg)
    chk = doctor._check_codex_config(proj)
    assert chk.status == "warn"
    assert "duplicate" in chk.details.lower()
    assert "codex-config --enable" in chk.fix


def test_check_scratch_hygiene_uses_configured_tmp_root_never_the_real_one(tmp_path: Path) -> None:
    """#148 fix round: the scratch-hygiene check must scan the CONFIGURED
    tmp_root, not silently fall through to the real OS temp directory in a
    project that pins one - proves the check completes correctly and fast
    against a small, controlled tree rather than the real (potentially
    huge, unbounded) system temp root."""
    proj = tmp_path / "proj"
    (proj / ".agenttalk").mkdir(parents=True)
    fake_tmp_root = tmp_path / "fake-tmp"
    fake_tmp_root.mkdir()
    (proj / ".agenttalk" / "config.json").write_text(
        json.dumps({"agents": ["a", "b"], "scratch": {"tmp_root": str(fake_tmp_root)}}),
        encoding="utf-8",
    )
    check = doctor._check_scratch_hygiene(proj)
    assert check is None  # nothing to warn about in an empty, controlled tree


def test_check_scratch_hygiene_warns_and_names_access_errors(tmp_path: Path, monkeypatch) -> None:
    """#148 fix round R2/P5: an unlistable location surfaces through the
    doctor check as a named warning, never silently swallowed (the
    original bug this check exists to prevent) and never a raised
    exception out of doctor.run()."""
    proj = tmp_path / "proj"
    (proj / ".agenttalk").mkdir(parents=True)
    fake_tmp_root = tmp_path / "fake-tmp"
    fake_tmp_root.mkdir()
    (proj / ".agenttalk" / "config.json").write_text(
        json.dumps({"agents": ["a", "b"], "scratch": {"tmp_root": str(fake_tmp_root)}}),
        encoding="utf-8",
    )

    def _raise_iterdir(self):
        raise PermissionError("simulated access denied")

    monkeypatch.setattr(Path, "iterdir", _raise_iterdir)
    result = doctor._check_scratch_hygiene(proj)
    assert result is not None
    assert result.status == "warn"
    assert "could not be listed" in result.details
