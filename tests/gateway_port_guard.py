"""The opt-in guard for tests that touch the paid gateway's real, fixed ports
(127.0.0.1:4000/4001, ovh_gateway.PUBLIC_PORT/INTERNAL_PORT). Not a test module.

Such a test runs only when AGENTTALK_TEST_GATEWAY_PORTS=1 is set (CI sets it on every
dev-gate leg), and even then it is skipped when another process already holds either
port, as a running gateway does, so it can never reach a live gateway by accident.

One decision, taken once
------------------------
The question "may the gateway-port tests run now?" is answered ONCE per run, by the
process that owns the run, and everyone else is told the answer:

* a serial run answers at collection, when the first guarded test is collected, as before
  (no guarded test collected: no probe);
* under xdist the CONTROLLER answers once, in `pytest_configure`, before any worker starts, and
  hands the answer to every worker through `workerinput` (the `pytest_configure_node` hook of
  `GuardPlugin`, registered with `optionalhook=True`). The answer is cached, so a restarted worker
  gets the same one and workers never probe for the decision (the owned teardown check, below, is the only
  probing a worker does). A free-port probe is a momentary observation, not a
  reservation: it says the ports were free when the controller looked.
* a nested pytest session (PYTEST_CURRENT_TEST is set, because the session was started from a
  running test) never probes the real ports: it is told they are not its to touch, so it cannot
  become a second owner. Its guarded tests are skipped. Known limit: a nested session is recognised only by an
  inherited PYTEST_CURRENT_TEST; one started with that variable cleared looks like an ordinary run (a future
  end-to-end test of the experiment entry point, P6, is meant to cover it).

Interface (the field and option names below are fixed; other code and tests bind to them)
----------------------------------------------------------------------------------------
The cached controller decision, `DECISION_KEY = "gateway_port_decision"` in `config.workerinput`,
is a dict with exactly these fields (`DECISION_FIELDS`, in this order):

  version   int   1
  policy    str   "skip" or "error" (the policy in force; see the selector below)
  opt_in    bool  AGENTTALK_TEST_GATEWAY_PORTS == "1" in the controller's environment
  nested    bool  the controller is itself inside a running test (PYTEST_CURRENT_TEST is set)
  held      list  [[host, port], ...] still held after PROBE_ATTEMPTS tries; [] when free, not
                  opted in, or nested
  probes    int   how many probe calls the controller made (0 without the opt-in or when nested);
                  the audit counts this: it is the "one probe round" of the plan
  reason    str|None  why the guarded tests must not run (the text of the skip), or None when they may

A worker that receives a missing or malformed decision stops the run (pytest.UsageError naming
the key). The decision is never recomputed by a worker.

The selector for the strict policy
----------------------------------
The policy is "error" exactly when pytest was started WITH the option `--experiment-record`
(its destination is `experiment_record`, added by tests/experiment_record.py, PR P3; the value is
not read, only whether it is set), and "skip" otherwise. The experiment entry point (P6) puts
`--experiment-record=<folder>` into the pytest argv of both arms and of every probe, and nothing
else does; the contract's `"gateway_policy": "error"` is what it validates and reports. Where the
option does not exist (P3 not in the tree) the policy is "skip".

Under "error", the process that owns the run decides at `pytest_configure` (serial and controller
alike), and a held port ENDS THE RUN there: `pytest.exit(..., returncode=2)` (2 is the status of a
collection error), before any test is collected and before any worker starts, with the held
host:port in the message. Nothing connects to the port. A missing opt-in or a nested session
still skips, under both policies. Ordinary runs and POSIX runs never have the option and keep the
skip.

The owned teardown check
------------------------
`GuardPlugin` also fails a group member whose teardown leaves something behind: a child process
the test started (seen through `subprocess.Popen`, and `multiprocessing` children) that is still
alive, or a listener still holding one of the two ports. A surviving child process is ended; a
listener held in this process is detected, not closed. The member's teardown fails, which fails the
test and so rejects the run; it does not promise that nothing overlaps later, because the next member
can still start. The check does probe in the workers, after each guarded test that ran: it is not
the decision, which is taken once. It never probes for a test pytest reported skipped (by a marker, by
`skipif` or by `pytest.skip()` in its body); a test that ran and then xfailed is still checked.
"""

from __future__ import annotations

import multiprocessing
import os
import subprocess
import time
from collections.abc import Callable, Iterable, Mapping

import pytest

OPT_IN_VAR = "AGENTTALK_TEST_GATEWAY_PORTS"
# Under xdist two probes of the same free port can meet, so a port counts as held only if it is held
# on every attempt.
PROBE_ATTEMPTS = 3
PROBE_PAUSE_SECONDS = 0.1

NESTED_VAR = "PYTEST_CURRENT_TEST"
DECISION_KEY = "gateway_port_decision"
DECISION_VERSION = 1
DECISION_FIELDS = ("version", "policy", "opt_in", "nested", "held", "probes", "reason")
POLICY_SKIP = "skip"
POLICY_ERROR = "error"
STRICT_OPTION = "experiment_record"
PLUGIN_NAME = "agenttalk-gateway-port-guard"
SURVIVOR_WAIT_SECONDS = 5.0

Address = tuple[str, int]


def gateway_addresses() -> list[Address]:
    from agenttalk.ovh_gateway import INTERNAL_HOST, INTERNAL_PORT, PUBLIC_HOST, PUBLIC_PORT

    return [(PUBLIC_HOST, PUBLIC_PORT), (INTERNAL_HOST, INTERNAL_PORT)]


def port_is_held(host: str, port: int) -> bool:
    """Whether another socket already holds host:port, found the way the gateway checks
    its own ports (exclusive_bind_probe): a bind on a separate socket, closed at once.
    Nothing ever connects to the port and nothing is sent to it."""
    from agenttalk.ovh_gateway import GatewayConfigError
    from agenttalk.ovh_gateway_service import exclusive_bind_probe

    try:
        exclusive_bind_probe(host, port)
    except GatewayConfigError:
        return True
    return False


def decide(
    environ: Mapping[str, str],
    *,
    policy: str = POLICY_SKIP,
    addresses: Iterable[Address] | None = None,
    probe: Callable[[str, int], bool] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> dict:
    """The decision (see the module docstring for its fields). The only function that probes."""
    probe = port_is_held if probe is None else probe
    sleep = time.sleep if sleep is None else sleep
    decision: dict = {
        "version": DECISION_VERSION,
        "policy": policy,
        "opt_in": environ.get(OPT_IN_VAR) == "1",
        "nested": NESTED_VAR in environ,
        "held": [],
        "probes": 0,
        "reason": None,
    }
    if not decision["opt_in"]:
        decision["reason"] = (
            "it binds or calls the paid gateway's real ports 127.0.0.1:4000/4001; "
            f"opt-in: set {OPT_IN_VAR}=1 to run it (CI does)"
        )
        return decision
    if decision["nested"]:
        decision["reason"] = (
            "this is a nested pytest session; a nested session never touches the paid gateway's real ports "
            "127.0.0.1:4000/4001 (the run that started it owns them)"
        )
        return decision

    def counted(host: str, port: int) -> bool:
        decision["probes"] += 1
        return probe(host, port)

    held = list(gateway_addresses() if addresses is None else addresses)
    for attempt in range(PROBE_ATTEMPTS):
        held = [address for address in held if counted(*address)]
        if not held:
            return decision
        if attempt + 1 < PROBE_ATTEMPTS:
            sleep(PROBE_PAUSE_SECONDS)
    decision["held"] = [list(address) for address in held]
    ports = ", ".join(f"{host}:{port}" for host, port in held)
    decision["reason"] = f"{ports} already held by another process (a running gateway?); not run, never contacted"
    return decision


def skip_reason(
    environ: Mapping[str, str],
    *,
    addresses: Iterable[Address] | None = None,
    probe: Callable[[str, int], bool] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> str | None:
    """Why a gateway-port test must not run now, or None when it may."""
    return decide(environ, addresses=addresses, probe=probe, sleep=sleep)["reason"]


def policy_for(config) -> str:
    """ "error" when pytest was started with --experiment-record (the selector), else "skip"."""
    return POLICY_ERROR if config.getoption(STRICT_OPTION, default=None) else POLICY_SKIP


def _checked(value) -> dict:
    """The controller's decision, or a UsageError when it is missing or malformed."""
    problem = f"the controller's {DECISION_KEY} is missing or malformed"
    if not isinstance(value, dict) or tuple(value) != DECISION_FIELDS:
        raise pytest.UsageError(problem)
    held_ok = isinstance(value["held"], list) and all(
        isinstance(a, list) and len(a) == 2 and isinstance(a[0], str) and type(a[1]) is int for a in value["held"]
    )
    if not (
        value["version"] == DECISION_VERSION
        and value["policy"] in (POLICY_SKIP, POLICY_ERROR)
        and isinstance(value["opt_in"], bool)
        and isinstance(value["nested"], bool)
        and held_ok
        and type(value["probes"]) is int
        and (value["reason"] is None or isinstance(value["reason"], str))
    ):
        raise pytest.UsageError(problem)
    return value


def decision_for(config) -> dict | None:
    """The cached decision for this process: a worker's from `workerinput`, the owner's from its plugin; None when
    this process has none (a serial run answers lazily at collection)."""
    if config is None:
        return None
    workerinput = getattr(config, "workerinput", None)
    if workerinput is not None:
        return _checked(workerinput.get(DECISION_KEY))
    manager = getattr(config, "pluginmanager", None)
    plugin = manager.get_plugin(PLUGIN_NAME) if manager is not None else None
    return plugin.decision if plugin is not None else None


# -- the owned teardown check --------------------------------------------------------------------------------------

_STARTED: list[subprocess.Popen] = []
_WATCHERS = 0
_ORIGINAL_INIT = None


def _install_tracker() -> int:
    """Start recording every Popen created in this process; return where this watcher's records begin."""
    global _WATCHERS, _ORIGINAL_INIT
    if _WATCHERS == 0:
        _ORIGINAL_INIT = subprocess.Popen.__init__

        def init(self, *args, **kwargs):
            _ORIGINAL_INIT(self, *args, **kwargs)
            _STARTED.append(self)

        subprocess.Popen.__init__ = init
    _WATCHERS += 1
    return len(_STARTED)


def _remove_tracker(start: int) -> list[subprocess.Popen]:
    """Stop recording for this watcher; return the Popen objects it saw."""
    global _WATCHERS
    seen = _STARTED[start:]
    del _STARTED[start:]
    _WATCHERS -= 1
    if _WATCHERS == 0:
        subprocess.Popen.__init__ = _ORIGINAL_INIT
    return seen


def _end(survivors: list) -> None:
    """End the survivors: terminate, wait a bounded time, then kill."""
    for process in survivors:
        process.terminate()
    deadline = time.monotonic() + SURVIVOR_WAIT_SECONDS
    for process in survivors:
        if isinstance(process, subprocess.Popen):
            try:
                process.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=SURVIVOR_WAIT_SECONDS)
        else:
            process.join(timeout=max(0.0, deadline - time.monotonic()))
            if process.is_alive():
                process.kill()
                process.join(timeout=SURVIVOR_WAIT_SECONDS)


def base_test_name(item) -> str:
    return getattr(item, "originalname", None) or item.name.split("[", 1)[0]


class GuardPlugin:
    """Holds the decision (owner), hands it to workers, and runs the owned teardown check."""

    def __init__(self, names, environ, addresses, probe, sleep, teardown_probe=None) -> None:
        self.names = frozenset(names)
        self.environ = environ
        self.addresses = None if addresses is None else list(addresses)
        self.probe = probe
        self.teardown_probe = teardown_probe
        self.sleep = sleep
        self.decision: dict | None = None
        self._watching: dict[str, tuple[int, set[int]]] = {}
        self._skipped: set[str] = set()  # node ids pytest reported as skipped (xfail excepted): they ran nothing

    @pytest.hookimpl(optionalhook=True)
    def pytest_configure_node(self, node) -> None:
        node.workerinput[DECISION_KEY] = dict(self.decision) if self.decision is not None else None

    def _covers(self, item) -> bool:
        return base_test_name(item) in self.names and item.get_closest_marker("skip") is None

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_setup(self, item) -> None:
        if self._covers(item):
            before = {p.pid for p in multiprocessing.active_children()}
            self._watching[item.nodeid] = (_install_tracker(), before)

    @pytest.hookimpl(trylast=True)
    def pytest_runtest_logreport(self, report) -> None:
        if report.skipped and report.when in ("setup", "call") and not hasattr(report, "wasxfail"):
            self._skipped.add(report.nodeid)

    def _children(self, watch: tuple[int, set[int]]) -> list:
        """Stop watching and return the child processes the test started that are still alive."""
        start, before = watch
        started = _remove_tracker(start)
        children: list = [p for p in started if p.poll() is None]
        return children + [p for p in multiprocessing.active_children() if p.pid not in before]

    def _held(self) -> list[Address]:
        probe = self.teardown_probe or self.probe or port_is_held
        sleep = time.sleep if self.sleep is None else self.sleep
        held = list(gateway_addresses() if self.addresses is None else self.addresses)
        for attempt in range(PROBE_ATTEMPTS):
            held = [address for address in held if probe(*address)]
            if not held:
                break
            if attempt + 1 < PROBE_ATTEMPTS:
                sleep(PROBE_PAUSE_SECONDS)
        return held

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_teardown(self, item, nextitem):
        # A wrapper, so the watch is always closed, even when a fixture's own teardown failed first. The member's own
        # failure is never replaced: the survivors are still ended, but only a clean teardown is failed by this check.
        failed_already = False
        try:
            return (yield)
        except BaseException:
            failed_already = True
            raise
        finally:
            self._close_watch(item, failed_already)

    def _close_watch(self, item, failed_already: bool) -> None:
        watch = self._watching.pop(item.nodeid, None)
        skipped = item.nodeid in self._skipped
        self._skipped.discard(item.nodeid)
        if watch is None:
            return
        children = self._children(watch)
        if children:
            _end(children)
        if skipped:
            return  # a skipped test ran nothing: no probe, no failure
        held = self._held()
        if failed_already or not (children or held):
            return
        problems = []
        if children:
            pids = ", ".join(str(getattr(p, "pid", "?")) for p in children)
            problems.append(f"{len(children)} child process(es) it started survived its teardown (pid {pids}); ended")
        if held:
            ports = ", ".join(f"{host}:{port}" for host, port in held)
            problems.append(f"a listener still holds {ports}")
        pytest.fail(f"{item.name}: " + "; ".join(problems), pytrace=False)


def configure(
    config,
    names: Iterable[str] = (),
    *,
    environ: Mapping[str, str] | None = None,
    addresses: Iterable[Address] | None = None,
    probe: Callable[[str, int], bool] | None = None,
    sleep: Callable[[float], None] | None = None,
    teardown_probe: Callable[[str, int], bool] | None = None,
) -> GuardPlugin:
    """Register the plugin; take the decision here when this process owns the run and must decide early (an xdist
    controller, or any run under the strict policy). A held port under the strict policy ends the run."""
    existing = config.pluginmanager.get_plugin(PLUGIN_NAME)
    if existing is not None:
        return existing
    environ = os.environ if environ is None else environ
    plugin = GuardPlugin(names, environ, addresses, probe, sleep, teardown_probe)
    policy = policy_for(config)
    started_workers = getattr(config.option, "numprocesses", None) or getattr(config.option, "tx", None)
    controller = not hasattr(config, "workerinput") and bool(started_workers)
    if not hasattr(config, "workerinput") and (controller or policy == POLICY_ERROR):
        plugin.decision = decide(environ, policy=policy, addresses=addresses, probe=probe, sleep=sleep)
        if policy == POLICY_ERROR and plugin.decision["held"]:
            pytest.exit(
                f"gateway ports held: {plugin.decision['reason']}. The experiment's gateway policy is 'error': "
                "a held port stops the run instead of skipping the tests.",
                returncode=int(pytest.ExitCode.INTERRUPTED),
            )
    config.pluginmanager.register(plugin, PLUGIN_NAME)
    return plugin


def apply(
    items,
    names: frozenset[str],
    environ: Mapping[str, str] = os.environ,
    *,
    addresses: Iterable[Address] | None = None,
    config=None,
    probe: Callable[[str, int], bool] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> None:
    """Skip every collected item named in `names` unless the guard lets it run. With a `config` the cached decision
    is used (a worker never probes); without one, or in a serial ordinary run, the ports are probed here, only when
    such an item is collected and the opt-in is set."""
    guarded = [item for item in items if base_test_name(item) in names]
    if not guarded:
        return
    decision = decision_for(config)
    if decision is None:
        reason = skip_reason(environ, addresses=addresses, probe=probe, sleep=sleep)
    else:
        if decision["policy"] == POLICY_ERROR and decision["held"]:
            raise pytest.UsageError(f"gateway ports held under the 'error' policy: {decision['reason']}")
        reason = decision["reason"]
    if reason is None:
        return
    marker = pytest.mark.skip(reason=f"gateway-port test: {reason}")
    for item in guarded:
        item.add_marker(marker)
