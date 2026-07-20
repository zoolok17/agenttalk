"""Shared pytest fixtures.

Each test gets a fresh `tmp_path`-rooted agenttalk store via the `store`
fixture; tests that need just a plain temp dir use `tmp_path` directly.
"""

from __future__ import annotations

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
import agenttalk as _agenttalk_pkg


def _running_against_installed_wheel() -> bool:
    """True when agenttalk is imported from OUTSIDE this checkout (an installed
    wheel), as in dev-gate's wheel-isolation mode; False for a source/editable
    layout (agenttalk resolves inside the repo tree)."""
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
