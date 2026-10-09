"""The test suite's store fence: no test may open a message store outside pytest's
temporary folder, not even through a subprocess it starts.

``AGENTTALK_STORE_FENCE`` makes agenttalk's own commands refuse every store outside that
folder, so a test run from a checkout that holds a live ``.agenttalk`` can never reach it,
however the root is found (``--root``, ``AGENTTALK_ROOT`` or the walk up from the current
folder). It is set when pytest is configured, before any test module is imported.

Every child process gets this run's fence and report, whatever environment it is given:
inherited, passed by keyword or in Popen's positional slot, keyed as text or as bytes,
without the two settings, or with an empty or different value. That holds for
subprocess.Popen and everything built on it, the os.spawn family, os.posix_spawn,
os.system and multiprocessing. Within a test that set a narrower fence for itself, the
child gets that one; a test that lifted its own fence (to model an unfenced shell, as
tests/test_probe_store.py does) still cannot pass the lift to a child. A process started
some other way (ctypes, an external program that clears its environment) is outside this
guard: it protects against our own tests' mistakes, not against a deliberate escape.

Each refusal is written to ``AGENTTALK_STORE_FENCE_REPORT``. A test that caused one fails,
even when it expected a non-zero exit; a refusal outside any test (while modules are
collected, in a session fixture, or at session end) fails the whole run. A test that
probes the fence on purpose points the report at a file of its own: that is the only
way past this guard.

The main conftest calls :func:`configure` from its own ``pytest_configure``; a small suite
of its own imports the hook below. :func:`configure` registers the end-of-run check as a
plugin of its own, so a conftest's own ``pytest_sessionfinish`` cannot replace it.
"""

from __future__ import annotations

import contextlib
import functools
import inspect
import multiprocessing.process
import os
import subprocess
from pathlib import Path

import pytest

FENCE_ENV = "AGENTTALK_STORE_FENCE"
REPORT_ENV = "AGENTTALK_STORE_FENCE_REPORT"
REPORT_NAME = "store-fence-refusals.txt"
RUN_CHECK_PLUGIN = "agenttalk-store-fence-run-check"
_STATE: dict[str, Path] = {}


def configure(config: pytest.Config) -> None:
    """Fence this run to its basetemp before collection. Runs after the tmp_path
    plugin has made its factory (the hook is ``trylast``)."""
    _install(config._tmp_path_factory.getbasetemp())
    if config.pluginmanager.get_plugin(RUN_CHECK_PLUGIN) is None:
        config.pluginmanager.register(_RunCheck(), RUN_CHECK_PLUGIN)


def _install(basetemp: Path) -> None:
    basetemp = Path(basetemp).resolve()
    _STATE["basetemp"] = basetemp
    _STATE["report"] = basetemp / REPORT_NAME
    os.environ[FENCE_ENV] = str(basetemp)
    os.environ[REPORT_ENV] = str(_STATE["report"])
    _fence_child_environments()
    _fence_other_launches()


def _child_settings() -> dict[str, str]:
    """The fence and report every child gets: the test's own fence when it is set and
    inside this run's fence, otherwise this run's; the test's report, otherwise this run's."""
    run_fence = _STATE.get("basetemp")
    if run_fence is None:
        return {}
    fence = run_fence
    current = os.environ.get(FENCE_ENV)
    if current:
        narrower = Path(current).resolve()
        if narrower == run_fence or run_fence in narrower.parents:
            fence = narrower
    return {FENCE_ENV: str(fence), REPORT_ENV: os.environ.get(REPORT_ENV) or str(_STATE["report"])}


def _name(key) -> str:
    """An environment key as the name it sets, whether given as text or as bytes."""
    return (os.fsdecode(key) if isinstance(key, bytes) else str(key)).upper()


def _fenced(env) -> dict:
    settings = _child_settings()
    if not settings:
        return env
    kept = {key: value for key, value in env.items() if _name(key) not in settings}
    return {**kept, **settings}


@contextlib.contextmanager
def _settings_in_environ():
    """For a launch that takes the environment as it is: put this run's fence and report
    there for the moment of the launch, then put back what the test had."""
    settings = _child_settings()
    saved = {key: os.environ.get(key) for key in settings}
    os.environ.update(settings)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _wrap(owner, name: str, launch) -> None:
    original = getattr(owner, name, None)
    if original is None or getattr(original, "_store_fenced", False):
        return
    @functools.wraps(original)
    def wrapped(*args, **kwargs):          # a function, so it binds as a method on a class too
        return launch(original, *args, **kwargs)

    wrapped._store_fenced = True
    setattr(owner, name, wrapped)


def _env_last(original, *args):       # spawnve(mode, path, args, env), spawnvpe likewise
    return original(*args[:-1], _fenced(args[-1]))


def _env_third(original, path, argv, env, *args, **kwargs):     # posix_spawn(path, argv, env, ...)
    return original(path, argv, _fenced(env), *args, **kwargs)


def _inheriting(original, *args, **kwargs):
    with _settings_in_environ():
        return original(*args, **kwargs)


def _fence_other_launches() -> None:
    """The launches that do not go through subprocess.Popen. os.spawnl and os.spawnle call
    os.spawnv and os.spawnve, so wrapping those covers them; multiprocessing's start
    methods all take the environment as it is at the moment the child is made."""
    for name in ("spawnve", "spawnvpe"):
        _wrap(os, name, _env_last)
    for name in ("posix_spawn", "posix_spawnp"):
        _wrap(os, name, _env_third)
    for name in ("spawnv", "spawnvp", "system"):
        _wrap(os, name, _inheriting)
    _wrap(multiprocessing.process.BaseProcess, "start", _inheriting)


def _fence_child_environments() -> None:
    """Bind Popen's own arguments, so a positional env is seen too, and give every child
    this run's fence and report (see the module docstring)."""
    original = subprocess.Popen.__init__
    if getattr(original, "_store_fenced", False):
        return
    signature = inspect.signature(original)

    @functools.wraps(original)
    def init(self, *args, **kwargs):
        bound = signature.bind(self, *args, **kwargs)
        env = bound.arguments.get("env")
        if env is not None:
            bound.arguments["env"] = _fenced(env)
        elif any(os.environ.get(key) != value for key, value in _child_settings().items()):
            bound.arguments["env"] = _fenced(dict(os.environ))    # inheriting would drop or change them
        original(*bound.args, **bound.kwargs)

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


class _RunCheck:
    """The end-of-run check as a plugin: a conftest that defines ``pytest_sessionfinish``
    would replace a hook imported into it under that name."""

    def pytest_sessionfinish(self, session: pytest.Session) -> None:
        fail_run_on_refusals(session)


def fail_run_on_refusals(session: pytest.Session) -> None:
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
