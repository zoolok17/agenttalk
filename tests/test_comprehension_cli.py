"""#55 slice-1 PR-B item 9: `agenttalk comprehension scan|status|report|
validate` CLI wiring. Exercises the real argparse parser and `cli.main`,
per this codebase's own test_cli.py convention ("invoke main(argv) rather
than subprocess-ing to keep tests fast").

The sanitized worker's subprocess boundary is monkeypatched to an
in-process call for the same reason test_comprehension_scan_pipeline.py
does - these tests are about CLI/pipeline wiring, not re-proving the
worker boundary (already covered in test_comprehension_worker.py).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agenttalk import cli
from agenttalk.comprehension import scan_pipeline
from agenttalk.comprehension import worker as workermod


@pytest.fixture(autouse=True)
def _inprocess_worker(monkeypatch):
    monkeypatch.setattr(
        scan_pipeline.worker, "run_sanitized_worker",
        lambda root, relative_paths, **_kwargs: workermod.process_paths(root, relative_paths),
    )


@pytest.fixture(autouse=True)
def _no_agent_identity(monkeypatch):
    monkeypatch.delenv("AGENTTALK_SELF", raising=False)


def _init_git_repo(root: Path, *, ignore_agenttalk: bool = True) -> None:
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)  # noqa: S603,S607  # nosec B603 B607
    subprocess.run(  # noqa: S603,S607  # nosec B603 B607
        ["git", "-C", str(root), "config", "user.email", "t@t"], check=True)
    subprocess.run(  # noqa: S603,S607  # nosec B603 B607
        ["git", "-C", str(root), "config", "user.name", "t"], check=True)
    pattern = ".agenttalk/\n" if ignore_agenttalk else "build/\n"
    (root / ".gitignore").write_text(pattern, encoding="utf-8")


def _write_sample_java_project(root: Path) -> None:
    app_dir = root / "src" / "main" / "java" / "p"
    app_dir.mkdir(parents=True)
    (app_dir / "App.java").write_text(
        "package p;\nclass App {\n  public static void main(String[] args) {}\n}\n",
        encoding="utf-8",
    )


@pytest.fixture()
def java_repo(tmp_path: Path) -> Path:
    _init_git_repo(tmp_path)
    _write_sample_java_project(tmp_path)
    return tmp_path


def _run(argv: list[str], root: Path) -> int:
    return cli.main(["--root", str(root), *argv])


# ----------------------------------------------------------- scan

def test_scan_publishes_and_prints_json(java_repo: Path, capsys) -> None:
    exit_code = _run(["comprehension", "scan", "--json"], java_repo)
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "complete"
    assert payload["scan_id"]


def test_scan_human_output(java_repo: Path, capsys) -> None:
    exit_code = _run(["comprehension", "scan"], java_repo)
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "scan_id:" in out
    assert "status:  complete" in out


def test_scan_without_privacy_proof_refuses(tmp_path: Path, capsys) -> None:
    _init_git_repo(tmp_path, ignore_agenttalk=False)
    _write_sample_java_project(tmp_path)
    exit_code = _run(["comprehension", "scan"], tmp_path)
    assert exit_code == 2
    assert "vcs_privacy_refused" in capsys.readouterr().err
    assert not (tmp_path / ".agenttalk").exists()


def test_acknowledge_without_work_id_refuses(tmp_path: Path, capsys) -> None:
    _init_git_repo(tmp_path, ignore_agenttalk=False)
    _write_sample_java_project(tmp_path)
    exit_code = _run(
        ["comprehension", "scan", "--acknowledge-unignored-private-store"], tmp_path)
    assert exit_code == 2
    assert "--work-id" in capsys.readouterr().err


def test_acknowledge_without_work_id_refuses_even_with_no_privacy_issue(
    tmp_path: Path, capsys,
) -> None:
    """FIX ROUND 28 (twenty-fourth cold read, F9, wrong-refusal-timing,
    same class found one layer down at the CLI): the sibling test above
    only exercises this pairing refusal on a repo the privacy preflight
    was ALSO going to refuse anyway (`ignore_agenttalk=False`) - the
    check used to live entirely inside `cmd_comprehension`'s own `except
    VcsPrivacyRefused` branch, reached only when the FIRST, unacknow-
    ledged `scan_pipeline.run_scan(root)` attempt actually hit a privacy
    refusal. Against a repo with NO privacy issue at all
    (`.agenttalk/` correctly ignored here), `--acknowledge-unignored-
    private-store` with no `--work-id` silently proceeded to a normal,
    successful scan - the invalid flag pairing was never even evaluated.
    The pairing is a property of the arguments themselves, never of what
    the first scan attempt happens to find - must refuse here too."""
    _init_git_repo(tmp_path, ignore_agenttalk=True)
    _write_sample_java_project(tmp_path)
    exit_code = _run(
        ["comprehension", "scan", "--acknowledge-unignored-private-store"], tmp_path)
    assert exit_code == 2
    assert "--work-id" in capsys.readouterr().err
    assert not (tmp_path / ".agenttalk").exists()


def test_acknowledge_headless_without_agent_identity_reports_and_refuses(
    tmp_path: Path, capsys,
) -> None:
    """No interactive terminal in a pytest run (stdin/stdout are not a real
    tty) and no AGENTTALK_SELF set - the CLI can neither prompt nor
    escalate, so it must refuse loudly, never silently proceed."""
    _init_git_repo(tmp_path, ignore_agenttalk=False)
    _write_sample_java_project(tmp_path)
    exit_code = _run(
        ["comprehension", "scan", "--acknowledge-unignored-private-store",
         "--work-id", "migrate-app"],
        tmp_path,
    )
    assert exit_code == 2
    err = capsys.readouterr().err
    assert "AGENTTALK_SELF" in err
    assert not (tmp_path / ".agenttalk").exists()


# ----------------------------------------------------------- --recover-stale-lock (CR17-1)

def _acquire_real_lock(root: Path):
    from agenttalk.comprehension import lock as lockmod
    from agenttalk.comprehension import paths as pathsmod
    from agenttalk.comprehension import privacy as privacymod

    comp_dir = pathsmod.comprehension_dir(root / ".agenttalk")
    privacy_result = privacymod.run_privacy_preflight(root)
    return lockmod.acquire_scan_lock(
        comp_dir, privacy=privacy_result, predecessor_index_digest=None)


def test_scan_recover_stale_lock_live_owner_non_tty_refuses_never_attendance(
    java_repo: Path, capsys,
) -> None:
    """FIX ROUND 21 (seventeenth cold read, CR17-1 BLOCKER, safety
    contract): the reader's own exact three-step repro - a live lock
    holder, the flag passed, from a non-TTY shell (every pytest run,
    by default) - must refuse outright, exit 2, and never even reach an
    attendance prompt (a live owner is refused BEFORE attendance is
    ever consulted, per the fix's own part 1) - the lock file must
    still exist afterward, never silently cleared."""
    from agenttalk.comprehension import lock as lockmod

    live = _acquire_real_lock(java_repo)
    exit_code = _run(["comprehension", "scan", "--recover-stale-lock"], java_repo)
    assert exit_code == 2
    assert "comprehension_lock_contended" in capsys.readouterr().err
    assert live.path.exists()
    lockmod.release_scan_lock(live)


def _make_unverifiable(monkeypatch, stale) -> None:
    """A same-PID-but-different-process-identity match (PID reuse) can
    never prove death - the NORMAL (non-override) acquire path correctly
    raises ScanLockUnrecoverable for it (never silently auto-reclaims,
    unlike a provably-dead owner), which is exactly the shape
    --recover-stale-lock's own attended override exists for."""
    from agenttalk.comprehension import lock as lockmod
    from agenttalk.lifecycle_lock import ProcessIdentity

    different_identity = ProcessIdentity(
        scheme=stale.process_identity.scheme, value=stale.process_identity.value + "-reused")
    monkeypatch.setattr(lockmod, "process_observation", lambda pid: ("alive", different_identity))


def test_scan_recover_stale_lock_unverifiable_owner_non_tty_refuses(
    java_repo: Path, capsys, monkeypatch,
) -> None:
    """The reader's own non-TTY refusal case for a genuinely unverifiable
    (never provably dead) owner: no attended confirmation was ever given
    (no real terminal, no AGENTTALK_SELF) - must still refuse, never
    silently recover just because the flag was passed."""
    stale = _acquire_real_lock(java_repo)
    _make_unverifiable(monkeypatch, stale)
    exit_code = _run(["comprehension", "scan", "--recover-stale-lock"], java_repo)
    assert exit_code == 2
    assert "AGENTTALK_SELF" in capsys.readouterr().err
    assert stale.path.exists()  # never cleared without attendance


def test_scan_recover_stale_lock_unverifiable_owner_attended_confirmation_recovers(
    java_repo: Path, monkeypatch,
) -> None:
    """The reader's own dead/unverifiable-owner-recovery-still-works-
    attended case: once attendance is proven (simulated here directly,
    since no test anywhere in this codebase yet simulates a real TTY),
    the scan proceeds and the forced recovery is recorded, never
    silent."""
    import json

    stale = _acquire_real_lock(java_repo)
    _make_unverifiable(monkeypatch, stale)
    monkeypatch.setattr(cli, "_comprehension_confirm_attended", lambda prompt_lines: True)
    exit_code = _run(["comprehension", "scan", "--recover-stale-lock"], java_repo)
    assert exit_code == 0
    assert not stale.path.exists()
    from agenttalk.comprehension import paths as pathsmod

    comp_dir = pathsmod.comprehension_dir(java_repo / ".agenttalk")
    status = scan_pipeline.get_status(java_repo)
    run_dir = pathsmod.run_dir(comp_dir, status["latest_scan_id"])
    problems_doc = json.loads((run_dir / "problems.json").read_text(encoding="utf-8"))
    assert any(
        p["reason_code"] == "scan_lock_forcibly_recovered" for p in problems_doc["problems"])


def test_scan_recover_stale_lock_malformed_record_still_traces_with_pid_unknown(
    java_repo: Path, monkeypatch,
) -> None:
    """FIX ROUND 21b (reviewer-3's re-delta, MINOR 1, wrong-data): a
    forced recovery over a MALFORMED/unreadable scan.lock record used
    to return None from lock.recover_stale_lock - indistinguishable
    from the genuine "no lock file at all" no-op - so the caller's own
    forced-recovery trace (CR17-1's own part 3) silently recorded
    NOTHING for the one case that is MORE safety-relevant than an
    ordinary dead-owner reclaim, not less: this run could not verify
    who, if anyone, held the lock before clearing it. Now records
    scan_lock_forcibly_recovered with pid/acquisition time both named
    unknown, never a fabricated value."""
    import json

    from agenttalk.comprehension import paths as pathsmod

    comp_dir = pathsmod.comprehension_dir(java_repo / ".agenttalk")
    comp_dir.mkdir(parents=True, exist_ok=True)
    lock_file = pathsmod.lock_path(comp_dir)
    lock_file.write_text("{not valid json", encoding="utf-8")

    monkeypatch.setattr(cli, "_comprehension_confirm_attended", lambda prompt_lines: True)
    exit_code = _run(["comprehension", "scan", "--recover-stale-lock"], java_repo)
    assert exit_code == 0
    assert not lock_file.exists()

    status = scan_pipeline.get_status(java_repo)
    run_dir = pathsmod.run_dir(comp_dir, status["latest_scan_id"])
    problems_doc = json.loads((run_dir / "problems.json").read_text(encoding="utf-8"))
    forced_recovery = [
        p for p in problems_doc["problems"] if p["reason_code"] == "scan_lock_forcibly_recovered"]
    assert len(forced_recovery) == 1
    assert "could not be parsed" in forced_recovery[0]["detail"]
    assert "pid unknown" in forced_recovery[0]["detail"]


def test_recover_stale_lock_help_text_states_the_true_refuse_then_confirm_semantics(
    capsys,
) -> None:
    """FIX ROUND 21b (reviewer-3's re-delta, MINOR 2, wrong-data): the
    flag's own --help text still said "unconditionally clear" after
    CR17-1 overturned exactly that behavior - stale documentation
    advertising the removed unsafe semantics to an operator deciding
    whether to pass this flag."""
    with pytest.raises(SystemExit) as exc:
        cli.main(["comprehension", "scan", "--help"])
    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    assert "unconditionally clear" not in help_text
    assert "refuses" in help_text
    assert "provably" in help_text


def test_comprehension_help_declares_pack_as_a_later_increment(capsys) -> None:
    """FIX ROUND 23 (nineteenth cold read, F9, completeness): the
    design's own CLI command table lists six comprehension commands;
    --help shows five (scan/status/report/validate/prune) with nothing
    declaring pack's absence anywhere a --help reader would see it -
    pack is the design's own increment 3, not yet implemented."""
    with pytest.raises(SystemExit) as exc:
        cli.main(["comprehension", "--help"])
    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    assert "pack" in help_text
    assert "later increment" in help_text


def test_comprehension_help_declares_the_api_surface_as_not_implemented(capsys) -> None:
    """FIX ROUND 36 (thirtieth cold read, F6 LOW, completeness): `pack`
    got its own declaration (see the test above), but `/api/comprehension`
    (PR-D's own increment - the HTTP surface this same data would be
    served through) is equally unimplemented this slice and had no
    declaration anywhere a --help reader would see it either."""
    with pytest.raises(SystemExit) as exc:
        cli.main(["comprehension", "--help"])
    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    assert "/api/comprehension" in help_text


def test_scan_help_declares_scope_exclude_config_as_not_implemented_this_slice(capsys) -> None:
    """FIX ROUND 26 (twenty-second cold read, F5 completeness): `pack`
    got an explicit later-increment declaration (see the test above),
    but --scope/--exclude/--config - equally design-promised for THIS
    command per DESIGN-55's own scan narrowing/config.json parsing
    sections, and equally absent this slice - had no declaration
    anywhere a --help reader would see it."""
    with pytest.raises(SystemExit) as exc:
        cli.main(["comprehension", "scan", "--help"])
    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    assert "--scope" in help_text
    assert "--exclude" in help_text
    assert "--config" in help_text
    assert "not implemented this slice" in help_text


def test_scan_help_declares_it_does_not_require_an_initialized_root(capsys) -> None:
    """FIX ROUND 29 (twenty-fifth cold read, F8a polish, declare-not-
    silently-simplify): the design's own step 1 says scan "resolves the
    INITIALIZED project root" - this slice never actually checks that,
    it just writes .agenttalk/comprehension/ under whatever root
    resolves, initialized or not. Declared rather than enforced (real
    init-checking is a repo-wide bus concern, wider than this command)."""
    with pytest.raises(SystemExit) as exc:
        cli.main(["comprehension", "scan", "--help"])
    assert exc.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "does not require the project root to already be initialized" in help_text


def test_report_help_declares_human_output_byte_identical_to_json(capsys) -> None:
    """MICRO-ROUND 28b (reviewer-3 delta on `02c6b30`, R5, OVERTURNED
    RATIONALE): dropping this fact as "redundant with round-26 F6" was
    wrong - F6 declares it on `scan --help` alone, which says nothing
    about `report`. A `report --help` reader had no way to discover
    this fact without independently finding cmd_comprehension's own
    docstring or this PR's description."""
    with pytest.raises(SystemExit) as exc:
        cli.main(["comprehension", "report", "--help"])
    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    assert "byte-for-byte" in help_text
    assert "--json" in help_text


def test_status_before_any_scan(tmp_path: Path, capsys) -> None:
    exit_code = _run(["comprehension", "status", "--json"], tmp_path)
    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == {"status": "not_scanned"}


def test_status_after_a_scan(java_repo: Path, capsys) -> None:
    _run(["comprehension", "scan", "--json"], java_repo)
    capsys.readouterr()
    exit_code = _run(["comprehension", "status", "--json"], java_repo)
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "complete"
    assert payload["latest_scan_id"]


def test_status_human_output_prints_the_artifact_integrity_hint(
    java_repo: Path, capsys,
) -> None:
    """FIX ROUND 29 (twenty-fifth cold read, F8b polish): the round-7c
    parity precedent - default human output must not tell a strictly
    LESS honest story than --json, which already carries artifact_
    integrity_hint unconditionally. It existed only in --json before
    this fix."""
    _run(["comprehension", "scan"], java_repo)
    capsys.readouterr()
    exit_code = _run(["comprehension", "status"], java_repo)
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "validate" in out
    assert scan_pipeline.STATUS_ARTIFACT_INTEGRITY_HINT in out


def test_status_human_output_is_silent_about_integrity_when_verified(
    java_repo: Path, capsys,
) -> None:
    """No new noise on the happy path (round 7c, reviewer-3 delta on
    95d9cd8) - a normally-scanned, freshly-anchored run's default human
    output must NOT print anything about scan_json_integrity at all."""
    _run(["comprehension", "scan", "--json"], java_repo)
    capsys.readouterr()
    exit_code = _run(["comprehension", "status"], java_repo)
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "scan_json_integrity" not in out


def test_status_human_output_names_an_unverified_integrity_state(
    java_repo: Path, capsys, monkeypatch,
) -> None:
    """BLOCKER (round 7c, reviewer-3 delta on 95d9cd8): the round-7b
    "unverified" integrity state existed ONLY in the JSON payload -
    status's default human output printed a normal-looking healthy run
    with zero words of caution, even with an aged-out/never-recorded
    anchor. Must now name the state on default human output."""
    monkeypatch.setattr(scan_pipeline.publish, "_INDEX_RUNS_MAX", 1)
    _run(["comprehension", "scan", "--json"], java_repo)
    capsys.readouterr()
    index_path = scan_pipeline.paths.index_path(
        scan_pipeline.paths.comprehension_dir(java_repo / ".agenttalk"))
    aged_out_scan_id = json.loads(index_path.read_text(encoding="utf-8"))["latest_scan_id"]
    _run(["comprehension", "scan", "--json"], java_repo)  # ages the first run's anchor out
    capsys.readouterr()

    exit_code = _run(["comprehension", "status", "--run", aged_out_scan_id], java_repo)
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "scan_json_integrity: unverified" in out
    assert "scan_json_index_anchor_not_recorded" in out


def test_status_human_output_names_the_reported_run_not_always_the_latest(
    java_repo: Path, capsys,
) -> None:
    """N5 (seventh cold read, fix round 11): `status --run <older-id>`'s
    human output used to print latest_scan_id unconditionally - naming
    the repo's LATEST scan even when an OLDER run was explicitly
    requested and actually reported. Must print the reported run's own
    id."""
    _run(["comprehension", "scan", "--json"], java_repo)
    first_scan_id = json.loads(capsys.readouterr().out)["scan_id"]
    _run(["comprehension", "scan", "--json"], java_repo)
    second_scan_id = json.loads(capsys.readouterr().out)["scan_id"]
    assert first_scan_id != second_scan_id

    exit_code = _run(["comprehension", "status", "--run", first_scan_id], java_repo)
    assert exit_code == 0
    out = capsys.readouterr().out
    assert f"scan_id:  {first_scan_id}" in out
    assert second_scan_id not in out


# ----------------------------------------------------------- report

def test_report_after_a_scan(java_repo: Path, capsys) -> None:
    _run(["comprehension", "scan", "--json"], java_repo)
    capsys.readouterr()
    exit_code = _run(["comprehension", "report", "--json"], java_repo)
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["counts"]["units"] > 0


def test_report_unit_filter_note_present_for_a_malformed_length_id(
    java_repo: Path, capsys,
) -> None:
    """FIX ROUND 31 (twenty-seventh cold read, N6 VERIFY): the reader
    could not reproduce this end to end and asked for it to be checked
    at the real CLI layer specifically - `report --unit <a 16-char,
    non-64-hex id>` was reported as returning a healthy empty with NO
    unit_or_feature_filter_note. Verified NOT reproducible: round 23's
    own F10 note and round 30's own F4 refinement both survive intact
    through the real CLI --json path - the note is present regardless
    of the id's own length/grammar, exactly as `--unit`/`--feature`'s
    own open-id-space ruling (round 18b) requires."""
    _run(["comprehension", "scan", "--json"], java_repo)
    capsys.readouterr()
    exit_code = _run(["comprehension", "report", "--unit", "deadbeefdeadbeef", "--json"], java_repo)
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["units"] == []
    assert "deadbeefdeadbeef" in payload["unit_or_feature_filter_note"]


def test_report_unit_filter_note_present_for_a_well_formed_absent_id(
    java_repo: Path, capsys,
) -> None:
    """FIX ROUND 31 (N6 VERIFY): the companion shape - a well-formed
    64-hex id (the real unit_id grammar) that simply matches nothing
    this run. Also present, confirming the note's own coverage does
    not depend on the caller's id happening to look malformed."""
    _run(["comprehension", "scan", "--json"], java_repo)
    capsys.readouterr()
    absent_64_hex = "de" * 32
    exit_code = _run(["comprehension", "report", "--unit", absent_64_hex, "--json"], java_repo)
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["units"] == []
    assert absent_64_hex in payload["unit_or_feature_filter_note"]


def test_report_before_any_scan_refuses(tmp_path: Path, capsys) -> None:
    exit_code = _run(["comprehension", "report", "--json"], tmp_path)
    assert exit_code == 2
    assert "no comprehension run has ever been published" in capsys.readouterr().err


# ----------------------------------------------------------- validate

def test_validate_after_a_scan(java_repo: Path, capsys) -> None:
    _run(["comprehension", "scan", "--json"], java_repo)
    capsys.readouterr()
    exit_code = _run(["comprehension", "validate", "--json"], java_repo)
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is True


def test_validate_human_output_is_silent_about_integrity_when_verified(
    java_repo: Path, capsys,
) -> None:
    _run(["comprehension", "scan", "--json"], java_repo)
    capsys.readouterr()
    exit_code = _run(["comprehension", "validate"], java_repo)
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "scan_json_integrity" not in out


def test_validate_human_output_names_an_unverified_integrity_state(
    java_repo: Path, capsys, monkeypatch,
) -> None:
    """BLOCKER (round 7c, reviewer-3 delta on 95d9cd8): validate's
    default human output printed valid:true with the FULL all-artifacts-
    verified sentence even with a falsified fingerprint/completeness and
    the anchor keys removed - zero words of caution anywhere a human
    looks. valid stays true (the boolean is right); the output must now
    name the unverified state."""
    monkeypatch.setattr(scan_pipeline.publish, "_INDEX_RUNS_MAX", 1)
    _run(["comprehension", "scan", "--json"], java_repo)
    capsys.readouterr()
    index_path = scan_pipeline.paths.index_path(
        scan_pipeline.paths.comprehension_dir(java_repo / ".agenttalk"))
    aged_out_scan_id = json.loads(index_path.read_text(encoding="utf-8"))["latest_scan_id"]
    _run(["comprehension", "scan", "--json"], java_repo)
    capsys.readouterr()

    exit_code = _run(["comprehension", "validate", "--run", aged_out_scan_id], java_repo)
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "valid:   True" in out
    assert "UNVERIFIED" in out
    assert "scan_json_integrity: unverified" in out


def test_validate_on_a_malformed_index_refuses_typed_instead_of_crashing(
    java_repo: Path, capsys,
) -> None:
    """N2 (third cold read, fix round 5): the validate action caught only
    NotScanned - a malformed index.json raises a typed ComprehensionError
    (EnvelopeError, from validate_envelope) that this catch clause let
    propagate as an unhandled traceback, unlike every sibling action
    (status/report), which both already catch ComprehensionError too."""
    _run(["comprehension", "scan", "--json"], java_repo)
    capsys.readouterr()
    index_path = java_repo / ".agenttalk" / "comprehension" / "index.json"
    index_path.write_text('{"not": "a valid index envelope"}', encoding="utf-8")

    exit_code = _run(["comprehension", "validate", "--json"], java_repo)
    assert exit_code == 2
    assert "agenttalk:" in capsys.readouterr().err


def test_report_and_validate_refuse_a_malformed_artifact_record_via_the_real_cli(
    java_repo: Path, capsys,
) -> None:
    """M-1 (fourth cold read, fix round 6): a record inside an envelope-
    valid artifact missing a required key used to raise an untyped
    KeyError straight through status/report/validate via the real CLI.
    report must now exit 2 with a typed stderr message, never a
    traceback. validate is different by design - it CATCHES this
    internally and reports valid:false (a legitimate outcome, exit 1),
    naming the malformed artifact in its own detail field rather than
    letting the exception escape as a second, indistinguishable exit 1
    (a raw traceback happened to also exit 1, which is exactly the
    ambiguity this fix removes). status is EXCLUDED here on purpose - N1
    (fourth cold read, fix round 6) restored it to the design's own
    narrower read-cost tier (index + scan.json only), so it never loads
    modules.json at all and cannot be affected by this corruption
    either way; see test_get_status_does_not_verify_unrelated_artifacts_
    by_design in test_comprehension_scan_pipeline.py."""
    import json

    _run(["comprehension", "scan", "--json"], java_repo)
    capsys.readouterr()
    scan_id = json.loads(
        (java_repo / ".agenttalk" / "comprehension" / "index.json").read_text(encoding="utf-8"),
    )["latest_scan_id"]
    modules_path = java_repo / ".agenttalk" / "comprehension" / "runs" / scan_id / "modules.json"
    doc = json.loads(modules_path.read_text(encoding="utf-8"))
    del doc["units"][0]["unit_id"]
    modules_path.write_text(json.dumps(doc), encoding="utf-8")

    exit_code = _run(["comprehension", "report", "--json"], java_repo)
    err = capsys.readouterr().err
    assert exit_code == 2, f"report exited {exit_code}, expected 2 (typed refusal)"
    assert "agenttalk:" in err, f"report produced no typed stderr message: {err!r}"

    exit_code = _run(["comprehension", "validate", "--json"], java_repo)
    out = capsys.readouterr().out
    assert exit_code == 1
    payload = json.loads(out)
    assert payload["valid"] is False
    assert "modules.json" in payload["detail"]
    assert "malformed record" in payload["detail"]


def test_status_refuses_a_malformed_scan_json_body_via_the_real_cli(
    java_repo: Path, capsys,
) -> None:
    """MAJOR 2 (fifth cold read, fix round 7): round 6's N1 fix moved
    status off the shared record-conversion loop (the one place M1's
    guard lived) so it never loads modules/dependencies/features/
    readiness/problems at all - but that exclusion left scan.json, the
    ONE artifact status still reads, with no guard of its own on the
    exact same malformed-body shape M1 fixed for everything else. status
    used to raise an untyped KeyError straight through the real CLI on a
    scan.json that is envelope-valid but missing a required body field;
    it must now exit 2 with a typed stderr message instead.

    The mutated scan.json is re-signed against index.json's own anchor
    (MAJOR 3, this same round) so this test isolates the missing-field
    guard from that separate anchor-mismatch guard - both are legitimate
    typed refusals, but a body genuinely missing a required key (e.g. a
    future writer bug) is a distinct scenario from a tampered/stale
    on-disk file, and each needs its own proof."""
    import json as jsonmod

    from agenttalk.comprehension import digests as digestmod

    _run(["comprehension", "scan", "--json"], java_repo)
    capsys.readouterr()
    comp_dir = java_repo / ".agenttalk" / "comprehension"
    index_path = comp_dir / "index.json"
    index_doc = jsonmod.loads(index_path.read_text(encoding="utf-8"))
    scan_id = index_doc["latest_scan_id"]
    scan_path = comp_dir / "runs" / scan_id / "scan.json"
    doc = jsonmod.loads(scan_path.read_text(encoding="utf-8"))
    del doc["problem_count"]
    canonical_bytes = digestmod.canonical_json_bytes(doc)
    scan_path.write_bytes(canonical_bytes)
    for run_summary in index_doc["runs"]:
        if run_summary["scan_id"] == scan_id:
            run_summary["scan_json_byte_sha256"] = digestmod.sha256_bytes(canonical_bytes)
            run_summary["scan_json_content_digest"] = digestmod.canonical_content_digest(doc)
    index_path.write_text(jsonmod.dumps(index_doc), encoding="utf-8")

    exit_code = _run(["comprehension", "status", "--json"], java_repo)
    err = capsys.readouterr().err
    assert exit_code == 2, f"status exited {exit_code}, expected 2 (typed refusal)"
    assert "agenttalk:" in err, f"status produced no typed stderr message: {err!r}"
    assert "problem_count" in err


# ----------------------------------------------------------- prune --staging (M-5)

def test_prune_staging_reclaims_a_dead_owners_abandoned_directory(
    java_repo: Path, capsys, monkeypatch,
) -> None:
    """M-5 (second cold read, PR-B fix round 4): the design's own command
    table names ``agenttalk comprehension prune --staging``, but the CLI
    only ever wired scan/status/report/validate - the manual remedy for
    an abandoned staging directory did not exist at all."""
    from agenttalk.comprehension import lock as lockmod
    from agenttalk.comprehension import privacy as privacymod
    from agenttalk.comprehension import staging as stagingmod

    comp_dir = scan_pipeline.paths.comprehension_dir(java_repo / ".agenttalk")
    privacy_result = privacymod.run_privacy_preflight(java_repo)
    abandoned_lock = lockmod.acquire_scan_lock(
        comp_dir, privacy=privacy_result, predecessor_index_digest=None)
    abandoned_handle = stagingmod.create_staging_dir(
        scan_id="20260101T000000Z-abcd1234", lock_handle=abandoned_lock)
    lockmod.release_scan_lock(abandoned_lock)
    monkeypatch.setattr(stagingmod, "process_observation", lambda pid: ("dead", None))

    exit_code = _run(["comprehension", "prune", "--staging", "--json"], java_repo)
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reclaimed"] == [abandoned_handle.path.name]
    assert not abandoned_handle.path.exists()


def test_prune_without_staging_flag_refuses(tmp_path: Path, capsys) -> None:
    exit_code = _run(["comprehension", "prune"], tmp_path)
    assert exit_code == 2
    assert "--staging" in capsys.readouterr().err


# ----------------------------------------------------------- bare subcommand

def test_bare_comprehension_with_no_subcommand_refuses(tmp_path: Path, capsys) -> None:
    exit_code = _run(["comprehension"], tmp_path)
    assert exit_code == 2
    assert "subcommand" in capsys.readouterr().err
