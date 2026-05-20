"""Shared pytest fixtures.

Each test gets a fresh `tmp_path`-rooted agenttalk store via the `store`
fixture; tests that need just a plain temp dir use `tmp_path` directly.
"""

from __future__ import annotations

import os
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
    for var in ("AGENTTALK_SELF", "AGENTTALK_PEER"):
        monkeypatch.delenv(var, raising=False)
