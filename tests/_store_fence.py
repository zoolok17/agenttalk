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

A run started under a fence (a test run that a test starts, or an xdist worker) keeps
inside it. Its basetemp must lie inside that fence and must not hold the report it was
given; otherwise the run stops before pytest makes the folder (which empties a given one
first), and the report it was given gets one line. Its fence is its own basetemp, so it
only narrows, and at its end its refusals are added to the report it was given, so the
test that started it fails too. An xdist worker's refusals reach its controller instead,
which reads each worker's report.

The main conftest calls :func:`configure` from its own ``pytest_configure``; a small suite
of its own imports the hook below. :func:`configure` registers the end-of-run check as a
plugin of its own, so a conftest's own ``pytest_sessionfinish`` cannot replace it. A suite
that imports only the fixtures gets the same from the session fixture, once its tests start.
"""

from __future__ import annotations

import contextlib
import functools
import inspect
import multiprocessing.process
import os
import subprocess
import tempfile
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
    _install(config)
    if config.pluginmanager.get_plugin(RUN_CHECK_PLUGIN) is None:
        config.pluginmanager.register(_RunCheck(), RUN_CHECK_PLUGIN)


def _install(config: pytest.Config) -> None:
    given_fence, given_report = os.environ.get(FENCE_ENV), os.environ.get(REPORT_ENV)
    if given_fence:
        _keep_inside(config, Path(given_fence).resolve(), given_report)
    basetemp = config._tmp_path_factory.getbasetemp().resolve()    # makes the folder: checked first
    _STATE["basetemp"] = basetemp
    _STATE["report"] = basetemp / REPORT_NAME
    if given_fence and given_report and not hasattr(config, "workerinput"):
        _STATE["given_report"] = Path(given_report)
    os.environ[FENCE_ENV] = str(basetemp)
    os.environ[REPORT_ENV] = str(_STATE["report"])
    _fence_child_environments()
    _fence_other_launches()


def _keep_inside(config: pytest.Config, fence: Path, report: str | None) -> None:
    """Stop a run started under ``fence`` before pytest makes a basetemp outside it, the
    fence itself, or one that holds ``report``. Without ``--basetemp`` pytest makes its
    folder under its temporary root, which must then lie inside the fence."""
    given = config.option.basetemp
    if given:
        basetemp = Path(os.path.abspath(given)).resolve()
        if fence not in basetemp.parents:
            reason = f"is not inside the fence {fence}"
        elif report and basetemp in Path(report).resolve().parents:
            reason = "holds the report of the run that started this one"
        else:
            return
    else:
        basetemp = Path(os.environ.get("PYTEST_DEBUG_TEMPROOT") or tempfile.gettempdir()).resolve()
        if basetemp == fence or fence in basetemp.parents:
            return
        reason = f"is not inside the fence {fence}"
    if report:
        with contextlib.suppress(OSError), open(report, "a", encoding="utf-8") as fh:
            fh.write(f"{basetemp} (a test run's temporary folder that {reason})\n")
    pytest.exit(
        f"store fence: this test run was started under {FENCE_ENV}, and its temporary folder "
        f"{basetemp} {reason}. Give --basetemp a new folder inside the fence.",
        returncode=pytest.ExitCode.USAGE_ERROR,
    )


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
    """``env`` with this run's settings. ``None``, the inherited environment (which
    os.posix_spawn accepts, and subprocess passes on), starts from a copy of os.environ."""
    settings = _child_settings()
    if not settings:
        return env
    source = os.environ if env is None else env
    kept = {key: value for key, value in source.items() if _name(key) not in settings}
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


def _env_argument(position: int):
    """A launch that fences the launcher's ``env`` argument however the caller passed it: by
    position, by keyword or mixed, bound against the launcher's own signature, so a call the
    launcher would refuse is refused the same way. ``position`` is where ``env`` sits for a
    launcher whose signature cannot be read: spawnve(mode, file, args, env) and spawnvpe
    have it fourth, posix_spawn(path, argv, env, ...) and posix_spawnp third."""

    def launch(original, *args, **kwargs):
        try:
            signature = inspect.signature(original)
        except (TypeError, ValueError):
            if "env" in kwargs:
                kwargs["env"] = _fenced(kwargs["env"])
            elif len(args) > position:
                args = (*args[:position], _fenced(args[position]), *args[position + 1:])
            return original(*args, **kwargs)
        bound = signature.bind(*args, **kwargs)
        bound.arguments["env"] = _fenced(bound.arguments["env"])
        return original(*bound.args, **bound.kwargs)

    return launch


def _inheriting(original, *args, **kwargs):
    with _settings_in_environ():
        return original(*args, **kwargs)


def _fence_other_launches() -> None:
    """The launches that do not go through subprocess.Popen. os.spawnl and os.spawnle call
    os.spawnv and os.spawnve, so wrapping those covers them; multiprocessing's start
    methods all take the environment as it is at the moment the child is made."""
    for name in ("spawnve", "spawnvpe"):
        _wrap(os, name, _env_argument(3))
    for name in ("posix_spawn", "posix_spawnp"):
        _wrap(os, name, _env_argument(2))
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
def _store_fence(pytestconfig: pytest.Config) -> Path:
    if "report" not in _STATE:              # a suite that imported only the fixtures
        configure(pytestconfig)
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
    and with xdist the report of each worker (``popen-gw*`` under this basetemp). A run
    started under a fence adds them to the report it was given."""
    basetemp = _STATE.get("basetemp")
    if basetemp is None:
        return
    reached = [line for report in (_STATE["report"], *sorted(basetemp.glob(f"popen-*/{REPORT_NAME}")))
               for line in _lines(report)]
    if not reached:
        return
    given_report = _STATE.get("given_report")
    if given_report is not None:
        with contextlib.suppress(OSError), open(given_report, "a", encoding="utf-8") as fh:
            fh.write("".join(line + "\n" for line in reached))
    if session.exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
    terminal = session.config.pluginmanager.get_plugin("terminalreporter")
    message = "store fence: this run reached a message store outside pytest's temporary folder: "
    if terminal is not None:
        terminal.write_line(message + ", ".join(reached), red=True)
