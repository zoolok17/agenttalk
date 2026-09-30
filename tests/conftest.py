"""Shared pytest fixtures.

Each test gets a fresh `tmp_path`-rooted agenttalk store via the `store`
fixture; tests that need just a plain temp dir use `tmp_path` directly.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agenttalk.comprehension.privacy import VcsPrivacyRefused, run_privacy_preflight
from agenttalk.store import Store


@pytest.fixture(scope="session")
def acceptance_project_template(tmp_path_factory):
    """Copied, never shared, by acceptance tests needing a synthetic Git root."""
    project = tmp_path_factory.mktemp("acceptance-project-template")

    def git(*args):
        return subprocess.check_output(["git", "-C", str(project), *args],
                                       stderr=subprocess.STDOUT).decode().strip()

    git("init", "-q")
    # Disable automatic writers before the first commit. check_output waits for
    # each foreground Git command; neither this template nor its candidate copy
    # may leave detached maintenance changing .git while copytree reads it.
    git("config", "gc.auto", "0")
    git("config", "maintenance.auto", "false")
    git("config", "gc.autoDetach", "false")
    git("config", "user.name", "Synthetic Author")
    git("config", "user.email", "synthetic@example.invalid")
    (project / "source.txt").write_text("synthetic source\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-qm", "synthetic fixture")
    return project, git("rev-parse", "HEAD")


@pytest.fixture(scope="session")
def acceptance_project_identity(acceptance_project_template):
    from agenttalk import acceptance
    project, sha = acceptance_project_template
    return acceptance.project_id(acceptance.verify_project(project, sha))


@pytest.fixture(scope="session")
def acceptance_candidate_template(acceptance_project_template, tmp_path_factory):
    """A second snapshot of the same template history for schema-3 fixtures."""
    template, _ = acceptance_project_template
    project = tmp_path_factory.mktemp("acceptance-candidate-template") / "project"
    shutil.copytree(template, project)
    # The copied config already disables maintenance before this commit too.
    (project / "source.txt").write_text("synthetic candidate\n", encoding="utf-8")
    subprocess.check_output(["git", "-C", str(project), "commit", "-qam", "candidate from verified base"],
                            stderr=subprocess.STDOUT)
    sha = subprocess.check_output(["git", "-C", str(project), "rev-parse", "HEAD"],
                                  stderr=subprocess.STDOUT).decode().strip()
    return project, sha


_SYMLINK_DEVMODE_SUBKEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\AppModelUnlock"
_SYMLINK_DEVMODE_VALUE = "AllowDevelopmentWithoutDevicePrivilege"
#: Dedicated, repo-specific opt-in - required IN ADDITION to CI/
#: GITHUB_ACTIONS (C-1a, reviewer-3 PR-B delta review round 2): those two
#: platform variables are also set by common local Actions emulators
#: (e.g. `act`), which would otherwise mutate a maintainer's real machine
#: - exactly the outcome this gate exists to prevent. Same dedicated-
#: opt-in-variable pattern test_comprehension_network_deny.py already
#: uses for its own OS-mutating tests; set explicitly only in the CI
#: job(s) that need the symlink tests to execute (tests.yml's Windows leg).
_SYMLINK_DEVMODE_AUTH_VAR = "AGENTTALK_AUTHORIZE_SYMLINK_DEVMODE"

#: Sentinel for "reading the prior value failed unexpectedly" (C-1b) -
#: distinct from `None`, which means "confirmed absent".
_READ_FAILED = object()


def _symlink_devmode_authorized() -> bool:
    return (
        os.environ.get("CI", "").lower() == "true"
        and os.environ.get("GITHUB_ACTIONS", "").lower() == "true"
        and os.environ.get(_SYMLINK_DEVMODE_AUTH_VAR) == "1"
    )


def _windows_symlink_devmode_session(winreg_module=None):
    """The core setup/restore generator, factored out of the pytest
    fixture below so it can be driven directly (with a fake ``winreg_module``
    substitute) by ``test_conftest_windows_symlink_devmode.py`` without
    needing a real elevated Windows box or a real GitHub Actions run to
    prove the enable/restore logic itself is correct.

    Exactly one ``yield`` on every path (required for a generator-based
    pytest fixture) - setup runs before it, teardown/restore after."""
    if sys.platform != "win32" or not _symlink_devmode_authorized():
        yield
        return
    winreg = winreg_module
    if winreg is None:
        import winreg  # noqa: PLC0414 - shadows the parameter name deliberately

    try:
        key = winreg.CreateKeyEx(
            winreg.HKEY_LOCAL_MACHINE, _SYMLINK_DEVMODE_SUBKEY,
            0, winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE,
        )
    except OSError:
        yield
        return

    wrote = False
    try:
        try:
            prior_value, prior_type = winreg.QueryValueEx(key, _SYMLINK_DEVMODE_VALUE)
        except FileNotFoundError:
            prior_value, prior_type = None, None
        except OSError:
            # C-1b (reviewer-3, PR-B delta review round 2): an unexpected
            # read failure here (not "value absent", something else) means
            # nothing has been written yet - degrade to a graceful no-op
            # exactly like the neighbouring CreateKeyEx failure path above,
            # never error the whole test session over it.
            prior_value = _READ_FAILED
        if prior_value is not _READ_FAILED:
            try:
                winreg.SetValueEx(key, _SYMLINK_DEVMODE_VALUE, 0, winreg.REG_DWORD, 1)
                wrote = True
            except OSError:
                pass
    finally:
        winreg.CloseKey(key)

    yield

    if not wrote:
        return

    try:
        restore_key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, _SYMLINK_DEVMODE_SUBKEY, 0, winreg.KEY_SET_VALUE)
    except OSError as exc:
        print(  # noqa: T201 - disclosed best-effort restore failure, never swallowed
            f"WARNING: could not reopen {_SYMLINK_DEVMODE_SUBKEY} to restore "
            f"{_SYMLINK_DEVMODE_VALUE} to its prior state: {exc}")
        return
    try:
        if prior_value is None:
            try:
                winreg.DeleteValue(restore_key, _SYMLINK_DEVMODE_VALUE)
            except FileNotFoundError:
                pass
            except OSError as exc:
                print(  # noqa: T201
                    f"WARNING: could not delete {_SYMLINK_DEVMODE_VALUE} to restore its "
                    f"prior absent state: {exc}")
        else:
            try:
                winreg.SetValueEx(restore_key, _SYMLINK_DEVMODE_VALUE, 0, prior_type, prior_value)
            except OSError as exc:
                print(  # noqa: T201
                    f"WARNING: could not restore {_SYMLINK_DEVMODE_VALUE} to its prior "
                    f"value {prior_value!r}: {exc}")
    finally:
        winreg.CloseKey(restore_key)


@pytest.fixture(scope="session", autouse=True)
def _enable_windows_symlink_creation_without_elevation():
    """C-1 / #213 (lead's PR-B fix-round dispatch, 2026-08-27, corrected
    per the lead's follow-up, 2026-08-27): four comprehension tests create
    a symlink to prove a boundary guard, and skip when that fails - a debt
    several PR-A/PR-B reviewers flagged as a "fast-follow: run CI's
    Windows job with Developer Mode (or an elevated runner)" so this
    executes instead of silently skipping.

    Sets the same registry policy the Settings app's "Developer Mode"
    toggle sets (``AllowDevelopmentWithoutDevicePrivilege``), which lets an
    unprivileged process create a symlink without
    ``SeCreateSymbolicLinkPrivilege`` - takes effect immediately, no
    reboot.

    STRICTLY gated on actually running under GitHub Actions (``CI=true``
    and ``GITHUB_ACTIONS=true``) AND this repo's own dedicated opt-in
    variable (``AGENTTALK_AUTHORIZE_SYMLINK_DEVMODE=1``, set explicitly
    only in the CI job(s) that need these tests to execute) - the same
    dedicated-opt-in-variable discipline test_comprehension_network_deny.py
    uses for its own OS-mutating tests. A local run, even an elevated one
    on a maintainer's real machine, MUST NOT mutate a machine-global
    registry policy as a side effect of running the test suite - that was
    the lead's first correction to this fixture. The dedicated variable is
    a SECOND correction (C-1a): ``CI``/``GITHUB_ACTIONS`` alone are also
    set by common local Actions emulators (e.g. `act`), which would
    otherwise trip the same unwanted mutation on a maintainer's machine.

    Restores whatever this exact value was before this session touched it
    (absent, or some other value) once the session ends - best-effort,
    with a disclosed warning printed if the restore itself fails, never
    silently swallowed. Disposable hosted runners make the restore largely
    moot in practice, but this fixture must still be correct on every
    machine it could actually run on. See
    ``_windows_symlink_devmode_session`` for the actual logic and
    ``test_conftest_windows_symlink_devmode.py`` for its direct,
    fake-winreg-backed unit tests.
    """
    yield from _windows_symlink_devmode_session()


@pytest.fixture
def store_root(tmp_path: Path) -> Path:
    """Return a fresh project root with an initialized .agenttalk/ store."""
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    return tmp_path


@pytest.fixture
def store(store_root: Path) -> Store:
    return Store(store_root)


@pytest.fixture
def comprehension_privacy_root(tmp_path: Path) -> Path:
    """A real git repo with ``.agenttalk/`` gitignored — for #55 comprehension
    tests that need ``run_privacy_preflight`` to genuinely succeed.

    reviewer-3's B-1 finding on PR-A (rq-5bd5427ad64d) required the
    privacy preflight to be a real PRECONDITION, proven against a real
    git fixture, with no permissive test-only constructor standing in for
    it. This fixture sets up the git state; it does not fabricate a
    result.
    """
    if shutil.which("git") is None:
        pytest.skip("git is required for comprehension privacy fixtures")
    subprocess.run(  # noqa: S603,S607  # nosec B603 B607
        ["git", "-C", str(tmp_path), "init", "-q"], check=True)
    subprocess.run(  # noqa: S603,S607  # nosec B603 B607
        ["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(  # noqa: S603,S607  # nosec B603 B607
        ["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    (tmp_path / ".gitignore").write_text(".agenttalk/\n", encoding="utf-8")
    return tmp_path


#: #223 fix round (PR #228): the substrings every TRANSIENT
#: ``VcsPrivacyRefused`` message carries. ``"could not be trusted"`` covers
#: the ls-files/check-ignore probes timing out; ``"is not inside a Git
#: worktree"`` covers the FIRST git call (``rev-parse
#: --is-inside-work-tree``) itself timing out — ``_run_git`` returns
#: ``None``, ``_is_git_worktree`` returns ``False``, and
#: ``run_privacy_preflight`` refuses with that message before it ever
#: reaches the "could not be trusted" checks later in the same function.
#: That message is ALSO the genuine, correct refusal for a root that
#: really is not a git repo — treating it as transient here is safe ONLY
#: because of this helper's one actual caller's own invariant: see the
#: docstring below.
_TRANSIENT_VCS_PRIVACY_REFUSAL_MARKERS = (
    "could not be trusted",
    "is not inside a Git worktree",
)


def _run_privacy_preflight_with_bounded_retry(root: Path, *, attempts: int = 4, delay: float = 0.5):
    """#223: ``run_privacy_preflight`` shells out to real ``git`` (rev-parse,
    ls-files, check-ignore), each bounded by ``privacy.GIT_TIMEOUT_SECONDS``
    (2s). On a loaded Windows CI runner that timeout can occasionally trip
    on a transient scheduling delay, not a real problem with the git
    repo — ``subprocess.TimeoutExpired`` is a ``SubprocessError``, so
    ``privacy._run_git``/``_run_git_with_stdin`` swallow it into ``None``
    ("untrustworthy"), and the preflight correctly (fail-closed, product-
    correct) refuses with a "could not be trusted" ``VcsPrivacyRefused``.

    #223 fix round (PR #228): the SAME transient timeout can also hit the
    very FIRST git call this function makes (``_is_git_worktree`` ->
    ``rev-parse --is-inside-work-tree``), which produces a DIFFERENT
    refusal message — "... is not inside a Git worktree (or git is
    unavailable)" — that does not contain "could not be trusted" at all,
    so the original (first fix round) retry missed this path entirely.
    This helper's one actual caller, the ``comprehension_privacy``
    fixture, is only ever handed :func:`comprehension_privacy_root`,
    which has JUST run ``git init`` successfully against that exact
    directory — so for THIS root, "not inside a Git worktree" can only
    mean the rev-parse call itself timed out, never a genuine non-repo.
    Treating it as transient is safe under that specific invariant, NOT
    in general (a root that really isn't a git repo gets this exact same
    message, correctly, and this helper would be wrong to retry that
    case blindly — it just never sees that case in practice, because its
    only caller never hands it one).

    Ideally this would branch on a structured reason the exception
    carries rather than matching message text — ``VcsPrivacyRefused``
    carries only a free-text ``detail`` today, no machine-readable
    "transient vs. genuine" marker. Adding one is a product-code change,
    out of scope for this tests-only fix; worth a follow-up if this
    class of flake needs a third fix round.

    This fixture's whole job is to hand back a REAL, proven result for
    tests whose subject is something else entirely (locking, publishing,
    escalation, ...) — for THEM, an occasional transient refusal of
    either shape is pure flake, not a signal. Retry the whole (idempotent,
    read-only) preflight a bounded number of times before giving up, but
    ONLY for the two transient marker strings above. A genuine refusal
    (not ignored, already tracked, stageable, ...) is a real test-setup
    bug, never transient, and must NOT be retried into silence: it
    re-raises immediately, unbounded-retry-free, same as before this
    fixture existed.
    """
    for attempt in range(attempts):
        try:
            return run_privacy_preflight(root)
        except VcsPrivacyRefused as exc:
            transient = any(marker in str(exc) for marker in _TRANSIENT_VCS_PRIVACY_REFUSAL_MARKERS)
            if not transient or attempt + 1 >= attempts:
                raise
            time.sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


@pytest.fixture
def comprehension_privacy(comprehension_privacy_root: Path):
    """A REAL, proven ``PrivacyPreflightResult`` (``vcs_privacy ==
    "ignored"``), obtained by actually running ``run_privacy_preflight``
    against :func:`comprehension_privacy_root`'s git fixture. #223: bounded
    retry absorbs a transient "git could not be trusted" timeout on a
    loaded CI host — see :func:`_run_privacy_preflight_with_bounded_retry`.
    """
    return _run_privacy_preflight_with_bounded_retry(comprehension_privacy_root)


@pytest.fixture
def comprehension_dir(comprehension_privacy_root: Path) -> Path:
    """The ``.agenttalk/comprehension/`` directory implied by
    :func:`comprehension_privacy_root`'s project root, matching the
    ``paths.comprehension_dir(agenttalk_dir)`` / ``store.DIRNAME``
    real-layout convention (``root/.agenttalk/comprehension``). Tests must
    acquire locks and stage/publish under THIS path, not the bare project
    root — :func:`comprehension_privacy`'s proof is bound to the project
    root via ``paths.project_root_from_comprehension_dir``, which climbs
    exactly two levels up from a comprehension dir (reviewer-1 cold-read
    finding 1 on PR-A, rq-6cc5560b62f6); a flat, unnested test directory
    would climb to the wrong place and make every root-binding check
    spuriously fail.
    """
    return comprehension_privacy_root / ".agenttalk" / "comprehension"


@pytest.fixture(autouse=True)
def _clear_agenttalk_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip AGENTTALK_* env vars between tests so resolution behavior is
    deterministic regardless of what the host shell has set."""
    for var in (
        "AGENTTALK_SELF",
        "AGENTTALK_PEER",
        "AGENTTALK_ROOT",
        "AGENTTALK_WRAPPER_GENERATION",
        "AGENTTALK_INBOUND_REQUEST_ID",
    ):
        monkeypatch.delenv(var, raising=False)


# --- #50: wheel-isolation test scoping ------------------------------------
def _running_against_installed_wheel() -> bool:
    """True when agenttalk is imported from OUTSIDE this checkout (an installed
    wheel), as in dev-gate's wheel-isolation mode; False for a source/editable
    layout (agenttalk resolves inside the repo tree)."""
    import agenttalk as _agenttalk_pkg  # local: keeps this off the module top (ruff E402)

    repo_root = Path(__file__).resolve().parents[1]
    try:
        pkg = Path(_agenttalk_pkg.__file__).resolve()
    except (AttributeError, TypeError):
        return False
    try:
        pkg.relative_to(repo_root)
        return False
    except ValueError:
        return True


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "source_layout: test spawns real subprocesses and asserts on the "
        "launched process identity (PID / live process tree), which requires a "
        "real (non-launcher) interpreter. Skipped under wheel-isolation, where a "
        "venv launcher gives the child a different PID; runs in source mode (#50).",
    )


# CI (#232 probe, ci-xdist-posix): two pytest-xdist workers under --dist
# loadgroup contend for real, machine-wide resources (a PowerShell-host
# startup slot, the OVH gateway's fixed 127.0.0.1:4000/4001 ports) when a
# test that touches one lands on a DIFFERENT worker than another one -
# round 1 of the probe measured this as intermittent failures/timeouts,
# never a real product bug. Every item below touches a real, unmocked
# instance of one of those two resources (verified by reading each helper,
# not just grepping for a keyword) and is forced into the matching shared
# xdist_group so every member always lands on the SAME one worker,
# serialized against each other; every other test keeps its per-file
# group, reproducing today's --dist loadfile behavior exactly. Linux/macOS
# only actually distribute under xdist today (see pytest_collection_
# modifyitems below); on Windows these markers are harmless no-ops.
_PWSH_SPAWNING_TEST_NAMES = frozenset(
    {
        # tests/test_powershell_functional.py - module-skipped off Windows /
        # without a real `pwsh` on PATH; every remaining test spawns one.
        "test_real_core_parses_and_runs_all_generated_harmless_paths",
        "test_real_core_accepts_direct_powershell_to_python_claim_chain",
        "test_real_core_override_claim_uses_baked_fallback_for_validation",
        "test_windows_powershell_51_rejects_each_script_before_sentinel",
        "test_real_selected_host_record_is_discrete_core_version",
        "test_real_probe_timeout_job_reaps_descendant",
        # tests/test_supervisor.py - each of these calls a real pwsh/powershell.exe
        # (a live probe, a live config-transport read, or a generated .ps1
        # actually executed), not a monkeypatched stand-in.
        "test_checkpoint_hook_guard_masks_legacy_exit_two_in_pwsh",
        "test_generated_ps1_is_bom_ascii_and_parses",
        "test_supervisor_config_transport_preserves_unicode_environment_names",
        "test_supervisor_config_transport_rejects_ambiguous_environment_names",
        "test_ephemeral_launcher_applies_environment_names_literally",
        "test_generated_helper_ps1_are_bom_ascii_and_parse",
        "test_generated_ps1_holds_malformed_config_poll_until_refresh_recovers",
        "test_generated_ps1_hot_adds_agent_across_live_polls",
        "test_generated_ps1_holds_poll_when_preplan_state_save_is_contended",
        "test_ps_state_atomic_swap_retries_windows_sharing_violation",
        "test_ps_poll_state_save_warns_and_survives_persistent_contention",
        "test_ps_poll_state_save_only_softens_sharing_and_lock_violations",
        "test_spawned_launch_is_not_acknowledged_when_record_launch_cannot_commit",
        "test_generated_ps1_two_polls_do_not_duplicate_launch_after_postspawn_contention",
        "test_ps_state_helpers_recover_backup_and_refuse_two_corrupt_copies",
        "test_ps_set_agent_state_adds_new_agent_to_fresh_and_reloaded_state",
        "test_generated_ps1_runs_bus_calls_without_console_script_on_path",
        "test_proc_start_falls_back_to_get_process_when_cim_denied",
        "test_generated_proc_snapshot_emits_exact_live_filetime",
        "test_stop_tree_rejects_rounded_start_collision_by_exact_filetime",
        "test_stop_tree_verifies_and_terminates_through_one_native_handle",
        "test_generated_ps1_tamper_refuses_before_claim",
        "test_generated_ps1_quiet_suppresses_warning_path",
        "test_generated_ps1_quiet_suppresses_relaunch_helper_warnings",
        "test_generated_ps1_quotes_args_with_spaces_as_single_arg",
        "test_ps_wrapper_log_targets_preserve_output_and_prune_old_generations",
        "test_ps_wrapper_log_retention_settles_one_over_quota_not_at_quota",
        "test_ps_wrapper_log_prune_survives_backward_clock_correction",
        "test_ps_wrapper_log_sequence_survives_failover_to_fallback_root",
        "test_ps_wrapper_log_attempt_cleaned_up_when_pending_marker_write_fails",
        "test_ps_wrapper_log_prune_refuses_when_root_scan_is_uncertain",
        "test_ps_wrapper_log_sequence_not_uncertain_when_root_has_no_agent_dir",
        "test_ps_wrapper_log_prune_bound_recovers_from_persistent_uncertainty",
        "test_ps_wrapper_log_sequence_write_failure_marks_uncertain_and_defers_prune",
        "test_ps_wrapper_log_sequence_uncertainty_persists_to_the_next_launch",
        "test_ps_wrapper_log_security_does_not_depend_on_ambient_os_marker",
        "test_ps_wrapper_log_cleanup_failure_uses_new_generation_and_still_launches",
        "test_ps_wrapper_log_retention_is_global_across_primary_and_fallback",
        "test_ps_failed_launch_generations_never_evict_prior_evidence",
        "test_ps_locked_uncommitted_generation_never_displaces_real_evidence",
        "test_ps_markerless_failed_generation_never_displaces_real_evidence",
        "test_ps_wrapper_log_agent_paths_do_not_alias_windows_names",
        "test_ps_wrapper_log_root_reparse_is_rejected_before_traversal",
        "test_ps_wrapper_redirect_closes_supervisor_capture_pipes_before_child_exit",
        "test_launch_environment_apply_failure_restores_parent_without_spawn",
        "test_supervisor_launch_nonce_injection_consumes_typed_position",
        "test_ps_regular_wrapped_launch_consumes_planned_loop_admission",
        "test_launchers_consume_accepted_admission_argv_without_dropping_empty_argument",
        "test_launchers_refuse_unusable_admission_before_environment_or_spawn",
        "test_launch_admission_resolver_rejects_type_coercion_and_shape_drift",
        "test_supervisor_wrapper_logging_is_driven_by_typed_admission",
        "test_ps_start_wrapper_process_fallback_strips_logging_env_vars",
        "test_ps_start_wrapper_process_reports_unredirected_when_both_sides_degrade",
        "test_ps_start_wrapper_process_reports_redirected_when_one_side_degrades",
        "test_ps_start_wrapper_process_strips_env_before_both_degraded_fallback",
        "test_ps_launch_discards_targets_when_fallback_is_unredirected",
        "test_launch_rechecks_kill_switch_after_branch_guard",
        "test_stop_tree_kills_real_two_level_tree_start_guarded",
        "test_seed_codex_home_provisions_and_fails_closed",
        "test_preflight_wrapped_codex_validates_python_not_codex_sandbox",
        "test_preflight_wrapped_console_entry_uses_direct_version_probe",
        "test_preflight_wrapped_smoke_test_uses_admitted_prefix",
        # tests/test_supervisor_spawn_seam.py - real spawn-seam tests only
        # (the pure PS_TEMPLATE text/regression-tripwire tests in the same
        # file are deliberately left OUT: they never spawn anything).
        "test_spawn_seam_refuses_invalid_precreate_input_with_closed_result",
        "test_spawn_seam_reports_unknown_without_a_null_result",
        "test_spawn_seam_start_process_path_records_exact_identity",
        "test_spawn_seam_real_wrapper_reaches_readiness_with_one_exact_identity",
        # tests/test_ephemeral_reviewers.py
        "test_prepare_cli_preserves_unicode_profile_environment_names",
        "test_prepare_cli_rejects_colliding_environment_names_before_effects",
        "test_prepare_cli_requires_powershell_accepted_config_before_effects",
        # tests/test_coverage_producer.py - each junctions a real reparse point
        # via a short-lived `powershell -Command New-Item -ItemType Junction`.
        "test_dangling_selected_reparse_object_refuses_default",
        "test_agenttalk_runtime_junction_is_not_scanner_owned",
        "test_scanner_leaf_below_runtime_junction_is_not_exempt",
        "test_coverage_lock_refuses_reparse_parent_without_touching_target",
        "test_stored_coverage_lock_context_revalidates_replaced_parent",
        "test_artifact_writer_refuses_reparse_output_parent",
    }
)

# test_prepare_cli_requires_powershell_accepted_config_before_effects is
# parametrized over 3 config-drift scenarios; only "transport_ambiguous"
# actually reaches the real powershell-config-transport probe - the
# "missing"/"changed" scenarios are rejected earlier, before any process is
# spawned, and must stay free to run on either worker.
_PWSH_SPAWNING_ONLY_PARAM_IDS = {
    "test_prepare_cli_requires_powershell_accepted_config_before_effects": {"transport_ambiguous"},
}

# The OVH gateway's real ports are FIXED module-level constants
# (127.0.0.1:4000/4001, ovh_gateway.PUBLIC_PORT/INTERNAL_PORT) - every
# unmocked call into exclusive_bind_probe/_both_sockets_free (reached from
# gateway_status, reconfigure_endpoint, rebind_runtime, the real service
# runner, and the task-start path in ovh_gateway_service.py) does a REAL
# socket bind-probe against them. Two of these landing on different xdist
# workers at the same moment collide. Identified by tracing every call
# site, not by grepping for the port constants (which alone would have
# missed test_ovh_gateway_cli.py - it never mentions PUBLIC_PORT/
# INTERNAL_PORT by name, but its bare `agenttalk gateway status` CLI test
# calls the unmocked `service.gateway_status()`, which unconditionally
# probes PUBLIC_PORT). None of these 12 tests are parametrized. This group
# is disjoint from "pwsh" (no test is both) - if that ever changes, "pwsh"
# wins, since PowerShell-host startup contention was the FIRST flake found
# and its probe timeout is tighter (5s) than any gateway-port failure mode.
_GATEWAY_PORT_TEST_NAMES = frozenset(
    {
        # tests/test_ovh_gateway_lifecycle_integration.py - both tests: each
        # spawns real subprocesses that call unmocked rebind_runtime/
        # reconfigure_endpoint/run_service; the second test's subprocess
        # actually binds and listens on both ports for the test's duration.
        "test_real_process_rebind_serializes_reconfigure_without_stale_manifest_write",
        "test_real_process_service_startup_excludes_rebind_until_sockets_are_owned",
        # tests/test_ovh_gateway_service.py - each reaches gateway_status()
        # or stop_task()/_service_absent() without mocking
        # exclusive_bind_probe/_both_sockets_free first.
        "test_operator_stop_uses_gateway_kill_switch_before_bounded_task_end",
        "test_linux_operator_stop_uses_gateway_kill_switch_before_bounded_unit_stop",
        "test_linux_status_reports_absent_unit_before_any_install",
        "test_forced_stop_removes_only_stale_marker_after_both_sockets_are_free",
        "test_manifest_and_task_written_under_one_envelope_fail_against_another_ledger",
        "test_status_reports_no_policy_hash_when_the_ledger_is_unavailable",
        "test_init_with_reasoning_params_renders_them_and_records_them_in_the_manifest",
        "test_default_install_has_no_reasoning_params_anywhere",
        "test_a_tampered_manifest_reasoning_param_fails_closed",
        # tests/test_ovh_gateway_cli.py - the one CLI test that never mocks
        # gateway_status before invoking `agenttalk gateway status`.
        "test_gateway_status_not_ready_uses_operational_error_exit",
    }
)


def _pwsh_group_for(item: pytest.Item) -> str | None:
    name = getattr(item, "originalname", None) or item.name.split("[", 1)[0]
    if name not in _PWSH_SPAWNING_TEST_NAMES:
        return None
    only_ids = _PWSH_SPAWNING_ONLY_PARAM_IDS.get(name)
    if only_ids is not None:
        callspec_id = getattr(getattr(item, "callspec", None), "id", None)
        if callspec_id not in only_ids:
            return None
    return "pwsh"


def _xdist_group_for(item: pytest.Item) -> str | None:
    name = getattr(item, "originalname", None) or item.name.split("[", 1)[0]
    pwsh_group = _pwsh_group_for(item)
    if pwsh_group is not None:
        return pwsh_group
    if name in _GATEWAY_PORT_TEST_NAMES:
        return "gateway-ports"
    return None


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items) -> None:
    """#50: process-orchestration tests marked source_layout cannot pass under
    an installed-wheel run through a venv launcher (Popen.pid != child pid), and
    add no packaging coverage (they force PYTHONPATH=src for their own
    subprocess). Skip them only when running against an installed wheel; source
    mode runs them.

    CI (#232 probe -> ci-xdist-posix): under `--dist loadgroup` (Linux/macOS
    legs only - see dev_gate.py's AGENTTALK_DEV_GATE_POSIX_PARALLEL), every
    item is forced into the "pwsh" or "gateway-ports" group (see above) if
    it touches one of those two shared, machine-wide resources for real,
    else its own test file - reproducing today's `--dist loadfile` behavior
    for everything else. Only meaningful with the xdist plugin loaded and
    `-n`/`--dist` actually passed; a plain `pytest` run, or a Windows leg
    (xdist never distributes there), collects these markers as inert no-ops.

    `@pytest.hookimpl(tryfirst=True)` above is REQUIRED, not decorative
    (#232 probe round 6, reproduced directly: a 3-item minimal case ran on
    two DIFFERENT xdist workers without it, and on one worker, with the
    group suffix visible in the reported test ids, after adding it).
    `xdist.remote.WorkerInteractor` (loaded only inside each xdist WORKER
    subprocess) has its OWN plain-priority `pytest_collection_modifyitems`
    that reads each item's `xdist_group` markers and appends `@<group>` to
    the item's nodeid - that suffix, not the marker object itself, is what
    the scheduler actually groups on. With default hook priority, pytest
    calls xdist's implementation BEFORE this one (no marker exists yet when
    xdist looks), so `--dist loadgroup` silently falls back to per-item
    scheduling - group membership is correct, but has zero scheduling
    effect, unless this hook runs first."""
    if not _running_against_installed_wheel():
        pass
    else:
        skip = pytest.mark.skip(
            reason="source_layout: process-orchestration test needs a real-interpreter "
            "layout; not run under wheel-isolation (#50)"
        )
        for item in items:
            if "source_layout" in item.keywords:
                item.add_marker(skip)

    if config.pluginmanager.hasplugin("xdist"):
        for item in items:
            group = _xdist_group_for(item) or item.location[0]
            item.add_marker(pytest.mark.xdist_group(name=group))
