"""Shared pytest fixtures.

Each test gets a fresh `tmp_path`-rooted agenttalk store via the `store`
fixture; tests that need just a plain temp dir use `tmp_path` directly.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agenttalk.store import Store

_SYMLINK_DEVMODE_SUBKEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\AppModelUnlock"
_SYMLINK_DEVMODE_VALUE = "AllowDevelopmentWithoutDevicePrivilege"


def _running_on_github_actions() -> bool:
    return (
        os.environ.get("CI", "").lower() == "true"
        and os.environ.get("GITHUB_ACTIONS", "").lower() == "true"
    )


def _windows_symlink_devmode_session(winreg_module=None):
    """The core setup/restore generator, factored out of the pytest
    fixture below so it can be driven directly (with a fake ``winreg_module``
    substitute) by ``test_conftest_windows_symlink_devmode.py`` without
    needing a real elevated Windows box or a real GitHub Actions run to
    prove the enable/restore logic itself is correct.

    Exactly one ``yield`` on every path (required for a generator-based
    pytest fixture) - setup runs before it, teardown/restore after."""
    if sys.platform != "win32" or not _running_on_github_actions():
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

    STRICTLY gated on actually running under GitHub Actions
    (``CI=true`` and ``GITHUB_ACTIONS=true``, both set by the platform
    itself, never by this repo) - the same opt-in discipline
    test_comprehension_network_deny.py uses for its own OS-mutating tests.
    A local run, even an elevated one on a maintainer's real machine, MUST
    NOT mutate a machine-global registry policy as a side effect of
    running the test suite - that was the lead's correction to this
    fixture's first version, which wrote unconditionally whenever it
    happened to have the rights to.

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


@pytest.fixture
def comprehension_privacy(comprehension_privacy_root: Path):
    """A REAL, proven ``PrivacyPreflightResult`` (``vcs_privacy ==
    "ignored"``), obtained by actually running ``run_privacy_preflight``
    against :func:`comprehension_privacy_root`'s git fixture."""
    from agenttalk.comprehension.privacy import run_privacy_preflight

    return run_privacy_preflight(comprehension_privacy_root)


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


def pytest_collection_modifyitems(config, items) -> None:
    """#50: process-orchestration tests marked source_layout cannot pass under
    an installed-wheel run through a venv launcher (Popen.pid != child pid), and
    add no packaging coverage (they force PYTHONPATH=src for their own
    subprocess). Skip them only when running against an installed wheel; source
    mode runs them."""
    if not _running_against_installed_wheel():
        return
    skip = pytest.mark.skip(
        reason="source_layout: process-orchestration test needs a real-interpreter "
        "layout; not run under wheel-isolation (#50)"
    )
    for item in items:
        if "source_layout" in item.keywords:
            item.add_marker(skip)
