"""The test suite's store fence: no test may open a message store outside pytest's
temporary folder, not even through a subprocess it starts.

``AGENTTALK_STORE_FENCE`` makes agenttalk's own commands refuse every store outside that
folder, so a test run from a checkout that holds a live ``.agenttalk`` can never reach it,
however the root is found (``--root``, ``AGENTTALK_ROOT`` or the walk up from the current
folder). It is set when pytest is configured, before any test module is imported, and
every child process gets it, also one started with an environment of its own.

Each refusal is written to ``AGENTTALK_STORE_FENCE_REPORT``. A test that caused one fails,
even when it expected a non-zero exit; a refusal outside any test (while modules are
collected, in a session fixture, or at session end) fails the whole run. A test that
probes the fence on purpose points the report at a file of its own: that is the only
way past this guard.

The main conftest calls :func:`configure` from its own ``pytest_configure`` and imports
:func:`pytest_sessionfinish`; a small suite of its own imports both hooks below.
"""

from __future__ import annotations

import functools
import os
import subprocess
from pathlib import Path

import pytest

FENCE_ENV = "AGENTTALK_STORE_FENCE"
REPORT_ENV = "AGENTTALK_STORE_FENCE_REPORT"
REPORT_NAME = "store-fence-refusals.txt"
_STATE: dict[str, Path] = {}


def configure(config: pytest.Config) -> None:
    """Fence this run to its basetemp before collection. Runs after the tmp_path
    plugin has made its factory (the hook is ``trylast``)."""
    _install(config._tmp_path_factory.getbasetemp())


def _install(basetemp: Path) -> None:
    basetemp = Path(basetemp).resolve()
    _STATE["basetemp"] = basetemp
    _STATE["report"] = basetemp / REPORT_NAME
    os.environ[FENCE_ENV] = str(basetemp)
    os.environ[REPORT_ENV] = str(_STATE["report"])
    _fence_child_environments()


def _with_fence(env):
    present = {str(key).upper() for key in env}
    extra = {name: os.environ[name] for name in (FENCE_ENV, REPORT_ENV)
             if name in os.environ and name not in present}
    return {**env, **extra} if extra else env


def _fence_child_environments() -> None:
    """A child given an environment of its own still gets the fence and the report."""
    original = subprocess.Popen.__init__
    if getattr(original, "_store_fenced", False):
        return

    @functools.wraps(original)
    def init(self, *args, **kwargs):
        if kwargs.get("env") is not None:
            kwargs["env"] = _with_fence(kwargs["env"])
        original(self, *args, **kwargs)

    init._store_fenced = True
    subprocess.Popen.__init__ = init


def _size(report: Path) -> int:
    # os and open(), not Path methods: this runs while a test's own patches are still in place
    try:
        return os.path.getsize(report)
    except OSError:
        return 0


def _lines(report: Path, offset: int = 0) -> list[str]:
    try:
        with open(report, "rb") as fh:
            fh.seek(offset)
            return fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []


@pytest.fixture(scope="session", autouse=True)
def _store_fence(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if "report" not in _STATE:              # a suite that imported only the fixtures
        _install(tmp_path_factory.getbasetemp())
    return _STATE["report"]


@pytest.fixture(autouse=True)
def _no_store_outside_the_test_folder(_store_fence: Path):
    before = _size(_store_fence)
    yield
    if _size(_store_fence) > before:
        pytest.fail(
            "this test reached a message store outside pytest's temporary folder: "
            + ", ".join(_lines(_store_fence, before)),
            pytrace=False,
        )


@pytest.hookimpl(trylast=True)
def pytest_configure(config: pytest.Config) -> None:
    configure(config)


def pytest_sessionfinish(session: pytest.Session) -> None:
    """Any refusal of this run, wherever it happened, fails it: this process's report,
    and with xdist the report of each worker (``popen-gw*`` under this basetemp)."""
    basetemp = _STATE.get("basetemp")
    if basetemp is None:
        return
    reached = [line for report in (_STATE["report"], *sorted(basetemp.glob(f"popen-*/{REPORT_NAME}")))
               for line in _lines(report)]
    if not reached:
        return
    if session.exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
    terminal = session.config.pluginmanager.get_plugin("terminalreporter")
    message = "store fence: this run reached a message store outside pytest's temporary folder: "
    if terminal is not None:
        terminal.write_line(message + ", ".join(reached), red=True)
