"""Shared pytest fixtures.

Each test gets a fresh `tmp_path`-rooted agenttalk store via the `store`
fixture; tests that need just a plain temp dir use `tmp_path` directly.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from agenttalk.store import Store


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
