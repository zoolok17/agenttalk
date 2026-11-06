"""#318: the tests that touch the paid gateway's real, fixed ports are opt-in, and skip
while another process holds a port. These tests check the guard itself and never
touch the real ports: they use ephemeral ports they open themselves, a fake probe, or
a child pytest run without the opt-in, in which the guarded test is skipped before it
can run and no port is probed."""

from __future__ import annotations

import ast
import contextlib
import json
import os
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree

import experiment_record
import gateway_port_guard as guard
import pytest

pytest_plugins = ["pytester"]

REPO = Path(__file__).resolve().parents[1]
# One real guarded test; the child runs below only ever see it skipped.
GUARDED_NODE = (
    "tests/test_ovh_gateway_cli.py::test_gateway_status_not_ready_uses_operational_error_exit"
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def held_port():
    """An ephemeral loopback port another socket holds (listening) for the test."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        sock.setblocking(False)
        yield sock


def _never_called(host, port):
    raise AssertionError(f"the ports must not be probed without the opt-in ({host}:{port})")


def test_without_the_opt_in_the_tests_are_skipped_and_nothing_is_probed():
    for environ in ({}, {guard.OPT_IN_VAR: "0"}, {guard.OPT_IN_VAR: "true"}):
        reason = guard.skip_reason(environ, probe=_never_called)
        assert reason is not None and f"{guard.OPT_IN_VAR}=1" in reason


def test_with_the_opt_in_and_free_ports_the_tests_run():
    free = [("127.0.0.1", _free_port()), ("127.0.0.1", _free_port())]
    assert guard.skip_reason({guard.OPT_IN_VAR: "1"}, addresses=free) is None


def test_with_the_opt_in_a_held_port_skips_and_is_never_contacted(held_port):
    host, port = held_port.getsockname()
    reason = guard.skip_reason({guard.OPT_IN_VAR: "1"}, addresses=[(host, port)])
    assert reason is not None and f"{host}:{port} already held" in reason
    with pytest.raises(BlockingIOError):
        held_port.accept()  # the check bound a separate socket; nothing connected


def test_a_port_counts_as_held_only_when_it_is_held_on_every_attempt():
    """Under xdist two workers can probe the same free port at the same moment."""
    answers = iter([True, False])
    pauses = []
    assert guard.skip_reason(
        {guard.OPT_IN_VAR: "1"}, addresses=[("127.0.0.1", 1)],
        probe=lambda host, port: next(answers), sleep=pauses.append,
    ) is None
    assert pauses == [guard.PROBE_PAUSE_SECONDS]
    calls = []
    reason = guard.skip_reason(
        {guard.OPT_IN_VAR: "1"}, addresses=[("127.0.0.1", 1)],
        probe=lambda host, port: calls.append(port) or True, sleep=lambda _seconds: None,
    )
    assert reason is not None and len(calls) == guard.PROBE_ATTEMPTS


def test_the_guarded_ports_are_the_gateways_own():
    from agenttalk.ovh_gateway import INTERNAL_HOST, INTERNAL_PORT, PUBLIC_HOST, PUBLIC_PORT

    assert guard.gateway_addresses() == [(PUBLIC_HOST, PUBLIC_PORT), (INTERNAL_HOST, INTERNAL_PORT)]


def _item(name: str):
    markers = []
    return SimpleNamespace(name=name, originalname=None, add_marker=markers.append, markers=markers)


def test_only_the_named_tests_are_skipped_by_default():
    guarded, other = _item("test_touches_ports"), _item("test_plain")
    guard.apply([guarded, other], frozenset({"test_touches_ports"}), environ={})
    assert [mark.name for mark in guarded.markers] == ["skip"]
    assert other.markers == []


def test_an_opted_in_run_with_free_ports_marks_nothing():
    guarded = _item("test_touches_ports")
    guard.apply([guarded], frozenset({"test_touches_ports"}), environ={guard.OPT_IN_VAR: "1"},
                addresses=[("127.0.0.1", _free_port())])
    assert guarded.markers == []


def test_an_opted_in_run_with_a_held_port_skips_the_named_tests(held_port):
    guarded = _item("test_touches_ports")
    guard.apply([guarded], frozenset({"test_touches_ports"}), environ={guard.OPT_IN_VAR: "1"},
                addresses=[held_port.getsockname()])
    assert [mark.name for mark in guarded.markers] == ["skip"]
    assert "already held" in guarded.markers[0].kwargs["reason"]


def test_nothing_is_probed_when_no_named_test_is_collected(monkeypatch):
    monkeypatch.setattr(guard, "port_is_held", _never_called)
    guard.apply([_item("test_plain")], frozenset({"test_touches_ports"}),
                environ={guard.OPT_IN_VAR: "1"})


def test_the_real_conftest_skips_a_gateway_port_test_by_default(tmp_path):
    """The child run has no opt-in, so its guard probes nothing and the guarded test
    is skipped before it can run."""
    env = {key: value for key, value in os.environ.items()
           if key not in (guard.OPT_IN_VAR, "PYTEST_ADDOPTS")}
    done = subprocess.run(  # noqa: S603 - fixed argv, test-only, no shell
        [sys.executable, "-m", "pytest", GUARDED_NODE, "-q", "-rs", "-p", "no:cacheprovider",
         "--basetemp", str(tmp_path / "inner")],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=300, check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "1 skipped" in done.stdout
    assert f"opt-in: set {guard.OPT_IN_VAR}=1" in done.stdout


# --- P1: one port decision, taken once by the controller ------------------------------------------------------------

TESTS_DIR = Path(__file__).resolve().parent

INNER_CONFTEST = """
import json
import os

import gateway_port_guard as guard
import pytest

NAMES = frozenset({"test_guarded_a", "test_guarded_b", "test_guarded_c"})


def pytest_addoption(parser):
    parser.addoption("--experiment-record", dest="experiment_record", default=None)


def _addresses():
    return [tuple(address) for address in json.loads(os.environ["P1_ADDRESSES"])]


def _probe(tag, host, port):
    with open(f"{os.environ['P1_PROBE_LOG']}.{os.getpid()}", "a", encoding="utf-8") as handle:
        handle.write(f"{os.getpid()} {tag} {port}\\n")
    flag = os.environ.get("P1_HELD_FLAG")
    if flag and os.path.exists(flag):
        return True
    return guard.port_is_held(host, port) if os.environ.get("P1_REAL_PROBE") == "1" else False


def counting_probe(host, port):
    return _probe("decision", host, port)


def teardown_probe(host, port):
    return _probe("teardown", host, port)


class _QueryFails:
    # kernel32 with one call failing; everything else is the real thing
    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    def QueryInformationJobObject(self, *args):
        return 0


def owners(host, port):
    # Real lookup when the test uses real sockets; otherwise a fake: P1_OWNERS is "self" (the default: a listener of
    # this process), "other" (some other process) or "unknown" (the lookup cannot say).
    if os.environ.get("P1_REAL_PROBE") == "1" or os.environ.get("P1_REAL_OWNERS") == "1":
        found = guard.listener_owners(host, port)
    else:
        flag = os.environ.get("P1_HELD_FLAG")
        mode = os.environ.get("P1_OWNERS", "self")
        if not (flag and os.path.exists(flag)):
            found = set()  # nothing is held: nobody owns anything (the check asks about ports the probe calls free too)
        else:
            found = None if mode == "unknown" else {os.getpid() if mode == "self" else 1}
    with open(f"{os.environ['P1_PROBE_LOG']}.{os.getpid()}", "a", encoding="utf-8") as handle:
        handle.write(f"{os.getpid()} owners {port}\\n")
    return found


def pytest_configure(config):
    guard.configure(config, NAMES, environ=os.environ, addresses=_addresses(), probe=counting_probe,
                    sleep=lambda seconds: None, teardown_probe=teardown_probe, owners=owners)
    if os.environ.get("P1_FORCE_UNDECIDED") == "1":
        guard._classify = lambda pid, evidence: guard.UNDECIDED       # the window has "no proof" for any process
    if os.environ.get("P1_NOT_ENDED") == "1":
        guard._terminate_processes = lambda pids: set(pids)          # the exit of a process is never confirmed
    if os.environ.get("P1_REAL_PROBE") != "1" and os.environ.get("P1_REAL_OWNERS") != "1":
        guard._own_verdict = lambda host, port, before: guard.MINE    # the fake names this process: a new one
    if os.environ.get("P1_INVENTORY_FAILS") == "1":                   # the sockets the process held cannot be listed
        guard._listening_sockets = lambda: None
    if os.environ.get("P1_READ_FAILS"):                               # one live socket cannot be read: "start" or "end"
        real_listens, real_inventory, inside = guard._listens, guard._listening_sockets, []

        def inventory():
            inside.append(1)                                          # the start inventory is being taken
            try:
                return real_inventory()
            finally:
                inside.pop()

        def flaky(sock):
            if os.path.exists(os.environ["P1_FAIL_FILE"]) and bool(inside) == (os.environ["P1_READ_FAILS"] == "start"):
                with open(os.environ["P1_FAIL_FILE"], encoding="utf-8") as handle:
                    if sock.fileno() == int(handle.read() or -2):
                        raise OSError("this live socket cannot be read")
            return real_listens(sock)

        guard._listening_sockets, guard._listens = inventory, flaky
    if os.environ.get("P1_OWNERS_EMPTY") == "1":                      # a lookup that finds no owner for a busy port
        guard._owners_windows = guard._owners_linux = guard._owners_lsof = lambda host, port: set()
    if os.environ.get("P1_FD_UNREADABLE") == "1":                     # Linux: no process's descriptors can be read
        guard._fd_targets = lambda fd_dir: []
    if os.environ.get("P1_QUERY_FAILS") == "1":                       # the query of a job's members fails natively
        real_kernel32 = guard._kernel32
        guard._kernel32 = lambda: _QueryFails(real_kernel32())


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    guard.apply(items, NAMES, os.environ, addresses=_addresses(), config=config, probe=counting_probe,
                sleep=lambda seconds: None)
"""

QUICK_TESTS = """
import json
import os


def _note(request):
    seen = getattr(request.config, "workerinput", {}).get("gateway_port_decision")
    with open(f"{os.environ['P1_DECISION_LOG']}.{os.getpid()}", "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"pid": os.getpid(), "decision": seen}) + "\\n")


def test_guarded_a(request):
    _note(request)


def test_guarded_b(request):
    _note(request)


def test_guarded_c(request):
    _note(request)


def test_plain():
    pass
"""


@pytest.fixture
def project(pytester, monkeypatch, tmp_path):
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("PYTHONPATH", str(TESTS_DIR))
    monkeypatch.setenv(guard.OPT_IN_VAR, "1")
    monkeypatch.setenv("P1_PROBE_LOG", str(tmp_path / "probes.log"))
    monkeypatch.setenv("P1_DECISION_LOG", str(tmp_path / "decisions.log"))
    monkeypatch.setenv("P1_RUN_LOG", str(tmp_path / "runs.log"))
    monkeypatch.setenv("P1_ADDRESSES", json.dumps([["127.0.0.1", 4000], ["127.0.0.1", 4001]]))
    monkeypatch.delenv("P1_HELD_FLAG", raising=False)
    monkeypatch.delenv("P1_REAL_PROBE", raising=False)
    pytester.makeconftest(INNER_CONFTEST)
    pytester.log = tmp_path
    return pytester


def run_inner(project, monkeypatch, *args, workers=0, dist="loadgroup", timeout=180):
    # The outer test sets PYTEST_CURRENT_TEST; an inner run must not look nested unless a test says so.
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    extra = ["-p", "xdist.plugin", "-n", str(workers), "--dist", dist] if workers else []
    return project.runpytest_subprocess(*extra, *args, timeout=timeout)


def per_process_lines(project, name):
    """The lines of every `<name>.<pid>` file (one file per process, so concurrent workers cannot lose each other's)."""
    return [line for path in sorted(project.log.glob(f"{name}.*")) for line in lines_of(path)]


def decision_probes(project):
    """[(pid, port)] of the probes made to take THE decision (the teardown check's probes are tagged apart)."""
    rows = [line.split() for line in per_process_lines(project, "probes.log")]
    return [(row[0], int(row[2])) for row in rows if row[1] == "decision"]


def lines_of(path):
    path = Path(path)
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


# -- the decision itself (no child run) ----------------------------------------------------------------------------


def test_the_decision_has_the_documented_fields_and_no_others():
    # The module docstring names these fields; a worker receives exactly this dict under workerinput.
    decision = guard.decide({guard.OPT_IN_VAR: "1"}, addresses=[("127.0.0.1", 1)], probe=lambda host, port: False)
    assert tuple(decision) == guard.DECISION_FIELDS
    assert guard.DECISION_KEY == "gateway_port_decision"
    assert decision == {
        "version": 1,
        "policy": "skip",
        "opt_in": True,
        "nested": False,
        "held": [],
        "probes": 1,
        "reason": None,
    }


def test_without_the_opt_in_the_decision_probes_nothing():
    decision = guard.decide({}, addresses=[("127.0.0.1", 1)], probe=_never_called)
    assert decision["opt_in"] is False and decision["probes"] == 0 and decision["held"] == []
    assert f"{guard.OPT_IN_VAR}=1" in decision["reason"]


def test_a_nested_pytest_session_never_probes_the_real_ports():
    # Proof 2 of the plan: a nested session must not become a second owner. PYTEST_CURRENT_TEST is set in every
    # process started from inside a running test.
    environ = {guard.OPT_IN_VAR: "1", "PYTEST_CURRENT_TEST": "tests/test_x.py::test_y (call)"}
    decision = guard.decide(environ, addresses=[("127.0.0.1", 1)], probe=_never_called)
    assert decision["nested"] is True and decision["probes"] == 0
    assert "nested" in decision["reason"]


def test_a_held_port_is_recorded_in_the_decision(held_port):
    host, port = held_port.getsockname()
    decision = guard.decide(
        {guard.OPT_IN_VAR: "1"}, addresses=[(host, port)], probe=guard.port_is_held, sleep=lambda seconds: None
    )
    assert decision["held"] == [[host, port]] and decision["probes"] == guard.PROBE_ATTEMPTS
    assert f"{host}:{port} already held" in decision["reason"]
    with pytest.raises(BlockingIOError):
        held_port.accept()


@pytest.mark.parametrize(
    ("option", "expected"),
    [(None, "skip"), ("", "skip"), ("somewhere", "error")],
)
def test_the_strict_policy_is_selected_by_the_experiment_record_option(option, expected):
    config = SimpleNamespace(getoption=lambda name, default=None: option if name == "experiment_record" else default)
    assert guard.policy_for(config) == expected


def test_the_policy_is_skip_when_the_option_does_not_exist():
    config = SimpleNamespace(getoption=lambda name, default=None: default)
    assert guard.policy_for(config) == "skip"


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        {"version": 2},
        {"version": 1, "reason": 5},
        "held",
        {"version": 1, "policy": "skip", "opt_in": True, "nested": False, "held": "x", "probes": 1, "reason": None},
    ],
)
def test_a_worker_that_gets_a_bad_decision_stops_the_run(bad):
    config = SimpleNamespace(workerinput={guard.DECISION_KEY: bad})
    with pytest.raises(pytest.UsageError, match=guard.DECISION_KEY):
        guard.apply([_item("test_touches_ports")], frozenset({"test_touches_ports"}), environ={}, config=config)


def test_a_worker_without_a_decision_stops_the_run():
    config = SimpleNamespace(workerinput={})
    with pytest.raises(pytest.UsageError, match=guard.DECISION_KEY):
        guard.apply([_item("test_touches_ports")], frozenset({"test_touches_ports"}), environ={}, config=config)


def test_a_worker_applies_the_controllers_decision_without_probing():
    held = {
        "version": 1,
        "policy": "skip",
        "opt_in": True,
        "nested": False,
        "held": [["127.0.0.1", 4000]],
        "probes": 3,
        "reason": "127.0.0.1:4000 already held by another process (a running gateway?); not run",
    }
    guarded, other = _item("test_touches_ports"), _item("test_plain")
    config = SimpleNamespace(workerinput={guard.DECISION_KEY: held})
    guard.apply(
        [guarded, other],
        frozenset({"test_touches_ports"}),
        environ={guard.OPT_IN_VAR: "1"},
        config=config,
        probe=_never_called,
    )
    assert [mark.name for mark in guarded.markers] == ["skip"] and other.markers == []
    assert "already held" in guarded.markers[0].kwargs["reason"]


# -- plan tests 1-3, 6: pytester runs with a counting fake probe ---------------------------------------------------


def test_two_workers_make_one_probe_round_and_get_the_same_decision(project, monkeypatch):
    # Plan test 1. Red with a per-worker probe: each worker probes at its own collection.
    project.makepyfile(test_quick=QUICK_TESTS)

    result = run_inner(project, monkeypatch, workers=2)

    result.assert_outcomes(passed=4)
    probes = decision_probes(project)
    assert sorted(port for _pid, port in probes) == [4000, 4001], probes  # one round, one call per port
    assert len({pid for pid, _port in probes}) == 1, "more than one process probed"
    decisions = [json.loads(line) for line in per_process_lines(project, "decisions.log")]
    assert len(decisions) == 3 and len({json.dumps(d["decision"], sort_keys=True) for d in decisions}) == 1
    assert decisions[0]["decision"]["probes"] == 2 and decisions[0]["decision"]["reason"] is None
    assert {pid for pid, _port in probes}.isdisjoint({str(d["pid"]) for d in decisions}), "a worker probed"


CRASH_TESTS = """
import os


def test_guarded_a():
    open(os.environ["P1_HELD_FLAG"], "w").close()      # from now on a probe sees the port held
    with open(os.environ["P1_RUN_LOG"], "a", encoding="utf-8") as handle:
        handle.write("a\\n")
    os._exit(7)


def test_guarded_b():
    os.remove(os.environ["P1_HELD_FLAG"])             # the 'child' is gone again before this member's teardown check
    with open(os.environ["P1_RUN_LOG"], "a", encoding="utf-8") as handle:
        handle.write("b\\n")
"""


def test_a_restarted_worker_applies_the_cached_decision_and_the_crashed_item_runs_once(project, monkeypatch):
    # Plan tests 2 and 6. While worker 1 dies, 'a child holds the port' (the flag file). A replacement worker that
    # probed again would see it held and skip the next member (red); with the cached decision it runs. The crashed
    # item is reported once, as failed, and is not run again.
    monkeypatch.setenv("P1_HELD_FLAG", str(project.log / "held.flag"))
    project.makepyfile(test_crash=CRASH_TESTS)

    # --dist load, not loadgroup: with xdist 3.8's loadgroup scheduler a restarted worker is handed the crashed item
    # again, crashes again, and the run hangs. That is the restart behaviour the experiment forbids (it runs with
    # --max-worker-restart=0); here the cached decision is what is under test.
    result = run_inner(project, monkeypatch, workers=1, dist="load")

    result.assert_outcomes(passed=1, failed=1)
    assert lines_of(project.log / "runs.log") == ["a", "b"]  # a once, b once, on the replacement worker
    output = result.stdout.str()
    assert output.count("worker 'gw0' crashed while running 'test_crash.py::test_guarded_a'") == 1
    assert output.count("FAILED test_crash.py::test_guarded_a") == 1
    assert "skipped" not in result.stdout.str().splitlines()[-1]
    probes = decision_probes(project)
    assert len(probes) == 2 and len({pid for pid, _port in probes}) == 1, probes  # the controller's one round


GATE_GROUP = """

@pytest.hookimpl(tryfirst=True)
def pytest_itemcollected(item):
    # The gate puts every test that touches the gateway ports into one group; xdist's worker then appends `@group`.
    item.add_marker(pytest.mark.xdist_group(name="gateway-ports"))
"""


def gate_repair_source():
    """The gate's grouped-ID repair hook, copied exactly as `tests/conftest.py` has it (decorator included), so the
    inner run has the same repair without importing the whole conftest."""
    source = (TESTS_DIR / "conftest.py").read_text(encoding="utf-8")
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef) and node.name == "pytest_collection_finish":
            first = min([decorator.lineno for decorator in node.decorator_list] + [node.lineno])
            return "\n".join(source.splitlines()[first - 1 : node.end_lineno])
    raise AssertionError("tests/conftest.py has no pytest_collection_finish repair any more: update this test")


@pytest.mark.parametrize("restarts", ["default", "none"], ids=["default-restarts", "no-restarts"])
def test_a_crash_under_loadgroup_with_the_gates_repair_ends_and_the_decision_is_not_taken_again(
    project, monkeypatch, restarts
):
    # The path the gate runs: --dist loadgroup, the tests grouped, the grouped-ID repair loaded (pytest 9.2 reads
    # `_id`, xdist 3.8 writes `_nodeid`; on 9.1.1 the repair is inert). Without the repair on 9.2 the run hangs, so
    # the run is bounded by a timeout that fails the test. Under loadgroup xdist hands a crashed item to every
    # replacement worker until its restart limit, so the item is run (and reported failed) once per worker and the
    # second test never runs; each replacement must still apply the controller's one decision and not probe again.
    # The gate does not pass --max-worker-restart=0 yet (#582 is open); with it there is no replacement worker, the
    # item runs once and the run fails, so only the default-restarts case shows a replacement applying the decision.
    monkeypatch.setenv("P1_HELD_FLAG", str(project.log / "held.flag"))
    project.makeconftest(INNER_CONFTEST + GATE_GROUP + "\n\n" + gate_repair_source() + "\n")
    project.makepyfile(test_crash=CRASH_TESTS)
    extra = ["--max-worker-restart=0"] if restarts == "none" else []

    result = run_inner(project, monkeypatch, *extra, workers=1, timeout=90)

    output = result.stdout.str()
    runs = lines_of(project.log / "runs.log")
    assert result.ret == 1, output
    assert runs and set(runs) == {"a"}, runs  # "b" is behind the crashed item in the same group and never runs
    if restarts == "none":
        assert runs == ["a"]
    result.assert_outcomes(failed=len(runs))
    assert output.count("crashed while running 'test_crash.py::test_guarded_a@gateway-ports'") == len(runs), output
    assert "skipped" not in output.splitlines()[-1]  # a replacement that probed again would see the port held
    probes = decision_probes(project)
    assert len(probes) == 2 and len({pid for pid, _port in probes}) == 1, probes  # the controller's one round


@pytest.mark.parametrize("workers", [0, 2], ids=["serial", "parallel"])
def test_a_port_held_from_outside_stops_an_experiment_run_before_any_test_and_without_contact(
    project, monkeypatch, held_port, workers
):
    # Plan test 3 (experiment policy): a collection-time stop, not a skip; the held port is never connected to.
    host, port = held_port.getsockname()
    monkeypatch.setenv("P1_ADDRESSES", json.dumps([[host, port]]))
    monkeypatch.setenv("P1_REAL_PROBE", "1")
    project.makepyfile(test_quick=QUICK_TESTS)

    result = run_inner(project, monkeypatch, "--experiment-record=somewhere", workers=workers)

    assert result.ret == 2, result.stdout.str() + result.stderr.str()
    assert f"{host}:{port} already held" in result.stdout.str() + result.stderr.str()
    assert per_process_lines(project, "decisions.log") == [], "a test body ran"
    with pytest.raises(BlockingIOError):
        held_port.accept()


@pytest.mark.parametrize("workers", [0, 2], ids=["serial", "parallel"])
def test_a_port_held_from_outside_skips_an_ordinary_run_as_before(project, monkeypatch, held_port, workers):
    # Plan test 3 (ordinary policy): today's skip, still without contact.
    host, port = held_port.getsockname()
    monkeypatch.setenv("P1_ADDRESSES", json.dumps([[host, port]]))
    monkeypatch.setenv("P1_REAL_PROBE", "1")
    project.makepyfile(test_quick=QUICK_TESTS)

    result = run_inner(project, monkeypatch, "-rs", workers=workers)

    result.assert_outcomes(passed=1, skipped=3)
    assert f"{host}:{port} already held" in result.stdout.str()
    with pytest.raises(BlockingIOError):
        held_port.accept()


def test_an_ordinary_serial_run_probes_nothing_until_a_guarded_test_is_collected(project, monkeypatch):
    project.makepyfile(test_plain="def test_plain():\n    pass\n")
    run_inner(project, monkeypatch).assert_outcomes(passed=1)
    assert per_process_lines(project, "probes.log") == []


def test_an_experiment_serial_run_probes_once_at_configure(project, monkeypatch):
    project.makepyfile(test_plain="def test_plain():\n    pass\n")
    run_inner(project, monkeypatch, "--experiment-record=somewhere").assert_outcomes(passed=1)
    assert sorted(port for _pid, port in decision_probes(project)) == [4000, 4001]


def test_guarded_tests_skipped_for_the_opt_in_are_not_probed_in_teardown(project, monkeypatch):
    monkeypatch.delenv(guard.OPT_IN_VAR)
    project.makepyfile(test_quick=QUICK_TESTS)
    run_inner(project, monkeypatch).assert_outcomes(passed=1, skipped=3)
    assert per_process_lines(project, "probes.log") == []


def test_a_nested_run_collects_and_probes_nothing_of_the_gateway(project, monkeypatch):
    project.makepyfile(test_quick=QUICK_TESTS)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "outer.py::test_outer (call)")
    result = project.runpytest_subprocess("-rs", timeout=180)  # nested: the variable stays set
    result.assert_outcomes(passed=1, skipped=3)
    assert "nested" in result.stdout.str()
    assert per_process_lines(project, "probes.log") == []


# -- plan test 5: the owned teardown check ------------------------------------------------------------------------

SURVIVOR_TESTS = """
import os
import subprocess
import sys
import time

CHILD = (
    "import sys, time\\n"
    "stop = time.time() + 60\\n"
    "while time.time() < stop:\\n"
    "    open(sys.argv[1], 'a').write('x')\\n"
    "    time.sleep(0.1)\\n"
)


def test_guarded_a():
    subprocess.Popen([sys.executable, "-c", CHILD, os.environ["P1_BEAT"]])      # left running on purpose
    deadline = time.time() + 20
    while not os.path.exists(os.environ["P1_BEAT"]) and time.time() < deadline:   # wait for its first heartbeat
        time.sleep(0.05)


def test_guarded_b():
    pass


def test_plain():
    subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3)"])       # not a group member: not checked
"""


def test_a_child_left_by_a_group_member_fails_that_members_teardown_and_is_ended(project, monkeypatch):
    beat = project.log / "beat.txt"
    monkeypatch.setenv("P1_BEAT", str(beat))
    project.makepyfile(test_survivor=SURVIVOR_TESTS)

    result = run_inner(project, monkeypatch)

    result.assert_outcomes(passed=3, errors=1)
    output = result.stdout.str()
    assert "test_guarded_a" in output and "survived" in output
    assert any("test_survivor.py::test_guarded_a" in line for line in _guard_lines(output)), output
    size = beat.stat().st_size
    time.sleep(0.8)
    assert beat.stat().st_size == size, "the survivor was left running"


LISTENER_TESTS = """
import os
import socket

KEPT = []


def test_guarded_a():
    sock = socket.socket()
    sock.bind(("127.0.0.1", int(os.environ["P1_LISTEN_PORT"])))
    sock.listen(1)
    KEPT.append(sock)                                                            # left listening on purpose


def test_guarded_b():
    for sock in KEPT:
        sock.close()
"""


def test_a_listener_left_by_a_group_member_fails_that_members_teardown(project, monkeypatch):
    port = _free_port()
    monkeypatch.setenv("P1_LISTEN_PORT", str(port))
    monkeypatch.setenv("P1_ADDRESSES", json.dumps([["127.0.0.1", port]]))
    monkeypatch.setenv("P1_REAL_PROBE", "1")
    project.makepyfile(test_listener=LISTENER_TESTS)

    result = run_inner(project, monkeypatch)

    result.assert_outcomes(passed=2, errors=1)
    assert f"127.0.0.1:{port}" in result.stdout.str() and "listener" in result.stdout.str()
    assert any("test_listener.py::test_guarded_a" in line for line in _guard_lines(result.stdout.str()))


SKIPPED_TESTS = """
import pytest


@pytest.mark.skipif(True, reason="never runs")
def test_guarded_a():
    pass


def test_guarded_b():
    pytest.skip("skipped in its body")


@pytest.mark.parametrize(
    "value", ["ran", pytest.param("xfails", marks=pytest.mark.xfail(reason="ran, then failed as expected"))]
)
def test_guarded_c(value):
    assert value == "ran"
"""

HOLD_AFTER_THE_DECISION = """

def pytest_collection_finish(session):
    open(os.environ["P1_HELD_FLAG"], "w").close()  # something else holds the ports from now on
"""


@pytest.mark.parametrize("workers", [0, 1], ids=["serial", "parallel"])
def test_a_guarded_test_skipped_by_skipif_or_in_its_body_is_not_probed_or_failed_at_teardown(
    project, monkeypatch, workers
):
    # N1: the watch looked only for a `skip` marker, so a test skipped by skipif or by pytest.skip() was still watched,
    # its teardown probed the ports and failed it with "a listener still holds". Here something else holds the ports
    # from collection on (after the one decision). Only the tests that ran are checked: the xfail one ran its body
    # (pytest turns its failed teardown into a second xfail, so it is counted as one, but it was probed) and the plain
    # one ran, and its teardown error is the one error.
    monkeypatch.setenv("P1_HELD_FLAG", str(project.log / "held.flag"))
    project.makeconftest(INNER_CONFTEST + HOLD_AFTER_THE_DECISION)
    project.makepyfile(test_skips=SKIPPED_TESTS)

    result = run_inner(project, monkeypatch, workers=workers)

    output = result.stdout.str()
    result.assert_outcomes(skipped=2, xfailed=2, passed=1, errors=1)
    assert re.findall(r"ERROR at teardown of (\S+)", output) == ["test_guarded_c[ran]"], output
    rows = [line.split() for line in per_process_lines(project, "probes.log")]
    teardown = [row for row in rows if row[1] == "teardown"]
    assert len(teardown) == 2 * guard.PROBE_ATTEMPTS * 2, teardown  # two members ran: three attempts at two ports each


def test_xdist_started_with_tx_and_no_n_is_a_controller_too(project, monkeypatch):
    # N3: `--tx popen` starts workers without `-n`; the owner of that run must still decide once and hand it on.
    project.makepyfile(test_quick=QUICK_TESTS)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    result = project.runpytest_subprocess("-p", "xdist.plugin", "--tx", "popen", "--dist", "load", timeout=180)

    result.assert_outcomes(passed=4)
    probes = decision_probes(project)
    assert sorted(port for _pid, port in probes) == [4000, 4001] and len({pid for pid, _port in probes}) == 1, probes


def test_the_docstring_says_what_the_teardown_check_does_and_what_it_does_not():
    flat = " ".join((TESTS_DIR / "gateway_port_guard.py").read_text(encoding="utf-8").split())
    for text in (
        "workers never probe for the decision",
        "this check probes in the workers, after each test",
        "a listener held in this process is detected, not closed",
        "does not promise that nothing overlaps later",
        "a nested session is recognised only by an inherited PYTEST_CURRENT_TEST",
    ):
        assert text in flat, text


# -- round 3: a survivor rejects the run, whatever pytest does with the test -------------------------------------

HEARTBEAT_CHILD = """
import os
import subprocess
import sys
import time

import pytest

CHILD = (
    "import sys, time\\n"
    "stop = time.time() + 60\\n"
    "while time.time() < stop:\\n"
    "    open(sys.argv[1], 'a').write('x')\\n"
    "    time.sleep(0.1)\\n"
)


def start_child():
    subprocess.Popen([sys.executable, "-c", CHILD, os.environ["P1_BEAT"]])      # left running on purpose
    deadline = time.time() + 20
    while not os.path.exists(os.environ["P1_BEAT"]) and time.time() < deadline:   # wait for its first heartbeat
        time.sleep(0.05)
"""

XFAIL_SURVIVOR_TESTS = (
    HEARTBEAT_CHILD
    + """

@pytest.mark.xfail(reason="expected to fail")
def test_guarded_a():
    start_child()
    assert False


def test_guarded_b():
    pass
"""
)

SKIP_AFTER_CHILD_TESTS = (
    HEARTBEAT_CHILD
    + """

def test_guarded_a():
    start_child()
    pytest.skip("skipped after real work")


def test_guarded_b():
    pass
"""
)

SKIP_ONLY_TESTS = """
import pytest


@pytest.mark.skipif(True, reason="never runs")
def test_guarded_a():
    pass


def test_guarded_b():
    pytest.skip("nothing to do")
"""

XFAIL_THEN_EXIT_TESTS = (
    HEARTBEAT_CHILD
    + """

@pytest.mark.xfail(reason="expected to fail")
def test_guarded_a():
    start_child()
    assert False


def test_guarded_b():
    pytest.exit("stop here", returncode=3)
"""
)


OWN_FAILURE_TESTS = (
    HEARTBEAT_CHILD
    + """

@pytest.fixture
def breaks_at_teardown():
    yield
    raise RuntimeError("the member's own teardown failure")


def test_guarded_a(breaks_at_teardown):
    start_child()


def test_guarded_b():
    pass
"""
)


def _heartbeat_stopped(beat):
    size = beat.stat().st_size
    time.sleep(0.8)
    return beat.stat().st_size == size


def _guard_lines(output):
    return [line for line in output.splitlines() if "gateway port guard" in line]


@pytest.mark.parametrize("workers", [0, 2], ids=["serial", "parallel"])
def test_an_xfail_cannot_hide_a_survivor_the_run_is_rejected_and_names_it(project, monkeypatch, workers):
    # The xfail marker turns the failed teardown into a second xfail, so the run used to exit 0 with a child found.
    beat = project.log / "beat.txt"
    monkeypatch.setenv("P1_BEAT", str(beat))
    project.makepyfile(test_xfail=XFAIL_SURVIVOR_TESTS)

    result = run_inner(project, monkeypatch, workers=workers)

    result.assert_outcomes(passed=1, xfailed=2)  # the marker absorbed the teardown failure
    assert result.ret == 1, result.stdout.str()
    lines = _guard_lines(result.stdout.str())
    assert any("test_xfail.py::test_guarded_a" in line and "child process" in line for line in lines), lines
    assert _heartbeat_stopped(beat), "the survivor was left running"


CLEANUP_FAILS = """

LEFT = []


PLUGINS = []


def _fail_cleanup(children):
    LEFT.extend(children)
    recorded = bool(PLUGINS[0].survivors)  # is the survivor already on the session's list when the cleanup runs?
    raise RuntimeError(f"synthetic termination failure; recorded={recorded}")


def pytest_sessionstart(session):
    PLUGINS.append(session.config.pluginmanager.get_plugin(guard.PLUGIN_NAME))
    guard._end = _fail_cleanup


def pytest_unconfigure(config):
    for process in LEFT:  # the test's own clean-up: the injected failure left the child running on purpose
        process.kill()
"""


@pytest.mark.parametrize("workers", [0, 2], ids=["serial", "parallel"])
def test_a_survivor_is_recorded_before_its_cleanup_and_a_failing_cleanup_is_named(project, monkeypatch, workers):
    # codex-rev's P1 (round 3): the cleanup ran first and could raise; an xfail absorbed the error, the survivor was
    # never recorded, and the run exited 0 with the child alive and no guard line.
    beat = project.log / "beat.txt"
    monkeypatch.setenv("P1_BEAT", str(beat))
    project.makeconftest(INNER_CONFTEST + CLEANUP_FAILS)
    project.makepyfile(test_xfail=XFAIL_SURVIVOR_TESTS)

    result = run_inner(project, monkeypatch, workers=workers)

    result.assert_outcomes(passed=1, xfailed=2)  # the marker absorbed the teardown failure
    assert result.ret == 1, result.stdout.str()
    lines = _guard_lines(result.stdout.str())
    line = next((line for line in lines if "test_xfail.py::test_guarded_a" in line), "")
    assert "child process" in line, lines
    assert "not ended: RuntimeError: synthetic termination failure; recorded=True" in line, lines
    assert "; ended" not in line, "a failed cleanup must never claim the child was ended"


@pytest.mark.parametrize("workers", [0, 2], ids=["serial", "parallel"])
def test_a_skipped_test_that_left_a_child_is_ended_and_rejects_the_run(project, monkeypatch, workers):
    beat = project.log / "beat.txt"
    monkeypatch.setenv("P1_BEAT", str(beat))
    project.makepyfile(test_skipchild=SKIP_AFTER_CHILD_TESTS)

    result = run_inner(project, monkeypatch, workers=workers)

    result.assert_outcomes(skipped=1, passed=1)
    assert result.ret == 1, result.stdout.str()
    lines = _guard_lines(result.stdout.str())
    named = [line for line in lines if "test_skipchild.py::test_guarded_a" in line]
    assert named and all("skipped" in line and "child process" in line for line in named), lines
    assert _heartbeat_stopped(beat), "the child of the skipped test was left running"


@pytest.mark.parametrize("workers", [0, 2], ids=["serial", "parallel"])
def test_skipped_tests_without_a_child_leave_the_run_passing_even_with_the_ports_held(project, monkeypatch, workers):
    monkeypatch.setenv("P1_HELD_FLAG", str(project.log / "held.flag"))
    project.makeconftest(INNER_CONFTEST + HOLD_AFTER_THE_DECISION)
    project.makepyfile(test_skips=SKIP_ONLY_TESTS)

    result = run_inner(project, monkeypatch, workers=workers)

    result.assert_outcomes(skipped=2)
    assert result.ret == 0 and _guard_lines(result.stdout.str()) == []
    assert [row for row in per_process_lines(project, "probes.log") if row.split()[1] == "teardown"] == []


@pytest.mark.parametrize("workers", [0, 2], ids=["serial", "parallel"])
def test_a_clean_run_of_guarded_tests_exits_zero_and_prints_no_guard_line(project, monkeypatch, workers):
    project.makepyfile(test_quick=QUICK_TESTS)

    result = run_inner(project, monkeypatch, workers=workers)

    result.assert_outcomes(passed=4)
    assert result.ret == 0 and _guard_lines(result.stdout.str()) == []


def test_a_survivor_is_named_even_when_the_members_own_teardown_failed_first(project, monkeypatch):
    beat = project.log / "beat.txt"
    monkeypatch.setenv("P1_BEAT", str(beat))
    project.makepyfile(test_own=OWN_FAILURE_TESTS)

    result = run_inner(project, monkeypatch)

    result.assert_outcomes(passed=2, errors=1)  # only the member's own failure is reported on the test
    lines = _guard_lines(result.stdout.str())
    assert any("test_own.py::test_guarded_a" in line and "child process" in line for line in lines), lines
    assert "the member's own teardown failure" in result.stdout.str()
    assert _heartbeat_stopped(beat)


def test_a_worse_exit_status_is_kept_but_the_survivor_is_still_named(project, monkeypatch):
    beat = project.log / "beat.txt"
    monkeypatch.setenv("P1_BEAT", str(beat))
    project.makepyfile(test_worse=XFAIL_THEN_EXIT_TESTS)

    result = run_inner(project, monkeypatch)

    assert result.ret == 3, result.stdout.str()  # pytest.exit(returncode=3) is worse than a plain failure
    lines = _guard_lines(result.stdout.str())
    assert any("test_worse.py::test_guarded_a" in line for line in lines), lines
    assert _heartbeat_stopped(beat)


# -- round 5: the experiment journal records the status the process returns --------------------------------------------

_GUARD_IMPORT = "import gateway_port_guard as guard\n"
_ADD_OPTION = (
    "def pytest_addoption(parser):\n"
    '    parser.addoption("--experiment-record", dest="experiment_record", default=None)\n'
)
_CONFIGURE = "def pytest_configure(config):\n    guard.configure("

RECORDER_CONFTEST = (
    INNER_CONFTEST.replace(_GUARD_IMPORT, "import experiment_record\n" + _GUARD_IMPORT)
    .replace(_ADD_OPTION, "def pytest_addoption(parser):\n    experiment_record.add_option(parser)\n")
    .replace(_CONFIGURE, "def pytest_configure(config):\n    experiment_record.configure(config)\n    guard.configure(")
)  # the real recorder, registered just before the guard, as tests/conftest.py does
assert RECORDER_CONFTEST.count("experiment_record.configure(config)") == 1
assert "experiment_record.add_option(parser)" in RECORDER_CONFTEST
_CALL_END = "teardown_probe=teardown_probe, owners=owners)\n"
_RECORDER_LINE = "    experiment_record.configure(config)\n"
GUARD_FIRST_CONFTEST = RECORDER_CONFTEST.replace(_RECORDER_LINE, "").replace(_CALL_END, _CALL_END + _RECORDER_LINE)
# the other order: the guard is registered before the recorder
assert GUARD_FIRST_CONFTEST.index("guard.configure(") < GUARD_FIRST_CONFTEST.index(_RECORDER_LINE.strip())


def journal_statuses(folder):
    """{role: [(exit-status record values, trailer status), ...]} read straight from the journals in `folder`."""
    found = {}
    for path in sorted(Path(folder).iterdir()):
        if not path.is_file():
            continue
        records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        role = next((r["role"] for r in records if r.get("role")), None)
        exits = [r["exitstatus"] for r in records if r.get("kind") == "exit-status"]
        trailers = [r["exitstatus"] for r in records if r.get("kind") == "trailer"]
        found.setdefault(role, []).append((exits, trailers))
    return found


def run_recorded(project, monkeypatch, *args, workers=0, conftest=None, dist="loadgroup"):
    project.makeconftest(conftest or RECORDER_CONFTEST)
    folder = project.log / "journals"
    result = run_inner(project, monkeypatch, f"--experiment-record={folder}", *args, workers=workers, dist=dist)
    return result, journal_statuses(folder)


@pytest.mark.parametrize("workers", [0, 2], ids=["serial", "parallel"])
@pytest.mark.parametrize("kind", ["xfail", "skip"])
@pytest.mark.parametrize("order", ["recorder-first", "guard-first"])
def test_the_journal_records_the_status_the_process_returns_for_a_survivor(
        project, monkeypatch, workers, kind, order):
    # The bot on #667: pytest computed the `exitstatus` hook argument before the guard raised the status, so the
    # recorder wrote 0 to the exit-status record and the trailer while the process exited 1. Both registration orders
    # of the two plugins: recorder before the guard (as tests/conftest.py does) and the guard before the recorder.
    beat = project.log / "beat.txt"
    monkeypatch.setenv("P1_BEAT", str(beat))
    if kind == "xfail":
        project.makepyfile(test_survivor=XFAIL_SURVIVOR_TESTS)
    else:
        project.makepyfile(test_survivor=SKIP_AFTER_CHILD_TESTS)

    conftest = RECORDER_CONFTEST if order == "recorder-first" else GUARD_FIRST_CONFTEST
    result, journals = run_recorded(project, monkeypatch, workers=workers, conftest=conftest)

    assert result.ret == 1, result.stdout.str() + result.stderr.str()
    root = journals["controller" if workers else "serial"]
    assert root == [([1], [1])], journals  # the exit-status record and the trailer both say what the process returned
    assert _heartbeat_stopped(beat)


@pytest.mark.parametrize("workers", [0, 2], ids=["serial", "parallel"])
def test_a_clean_run_still_records_zero(project, monkeypatch, workers):
    project.makepyfile(test_quick=QUICK_TESTS)

    result, journals = run_recorded(project, monkeypatch, workers=workers)

    result.assert_outcomes(passed=4)
    assert result.ret == 0
    assert journals["controller" if workers else "serial"] == [([0], [0])], journals


def test_a_run_that_already_failed_keeps_its_worse_status_in_the_journal(project, monkeypatch):
    beat = project.log / "beat.txt"
    monkeypatch.setenv("P1_BEAT", str(beat))
    project.makepyfile(test_worse=XFAIL_THEN_EXIT_TESTS)

    result, journals = run_recorded(project, monkeypatch)

    assert result.ret == 3, result.stdout.str()  # pytest.exit(returncode=3): worse than a plain failure, kept
    assert journals["serial"] == [([3], [3])], journals
    assert _heartbeat_stopped(beat)


def test_the_docstring_states_the_survivor_rules_and_the_limit():
    flat = " ".join((TESTS_DIR / "gateway_port_guard.py").read_text(encoding="utf-8").split())
    for text in (
        "for a test that ran",
        "for a test pytest reported skipped",
        "records every survivor",
        "the exit status becomes 1 (unless it is already worse)",
        "an xfail cannot hide a survivor",
        "so a cleanup that raises cannot lose the record",
        "never claims the child was ended",
        "a skipped test that left a listener on the port is not probed",
    ):
        assert text in flat, text


# -- plan test 4 and 7 ------------------------------------------------------------------------------------------------


def _string_set(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return {n.value for n in ast.walk(node.value) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    raise AssertionError(f"{name} not found")


def test_the_nested_sessions_of_the_dev_gate_tests_collect_none_of_the_twelve(tmp_path):
    # Plan test 4 (and proof 2): the node ids those nested pytest runs select (test_dev_gate.py) contain no test of the
    # gateway-ports group, read from the group's own list, then checked by a real collection.
    twelve = _string_set(ast.parse((TESTS_DIR / "conftest.py").read_text(encoding="utf-8")), "_GATEWAY_PORT_TEST_NAMES")
    names = {n for n in twelve if n.startswith("test_")}
    assert len(names) == 12
    tree = ast.parse((TESTS_DIR / "test_dev_gate.py").read_text(encoding="utf-8"))
    function = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef)
        and n.name == "test_xdist_grouping_is_active_under_the_gates_real_pytest_invocation"
    )
    node_ids = sorted(
        {
            n.value
            for n in ast.walk(function)
            if isinstance(n, ast.Constant)
            and isinstance(n.value, str)
            and "::" in n.value
            and n.value.startswith("tests/")
        }
    )
    assert node_ids, "no nested node ids found"
    assert not {nid.split("::")[1].split("[")[0] for nid in node_ids} & names

    env = {k: v for k, v in os.environ.items() if k != "PYTEST_ADDOPTS"}
    done = subprocess.run(  # noqa: S603 - fixed argv, test-only, no shell
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            "--basetemp",
            str(tmp_path / "inner"),
            *node_ids,
        ],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert not any(name in done.stdout for name in names)


def test_the_experiments_parallel_arm_never_allows_a_worker_restart():
    # Plan test 7. The data file is P6's (a separate pull request); this checks it as soon as it is in the tree.
    path = REPO / "dev-gate-experiment.json"
    if not path.is_file():
        pytest.skip("dev-gate-experiment.json is not in this tree yet (P6 is a separate pull request)")
    arms = json.loads(path.read_text(encoding="utf-8"))["arms"]
    assert "--max-worker-restart=0" in arms["parallel"]


def test_the_popen_tracker_is_removed_when_the_last_watcher_is_done():
    original = subprocess.Popen.__init__
    first = guard._install_tracker()
    second = guard._install_tracker()
    assert subprocess.Popen.__init__ is not original
    guard._remove_tracker(second)
    assert subprocess.Popen.__init__ is not original, "removed while a watcher is still active"
    guard._remove_tracker(first)
    assert subprocess.Popen.__init__ is original


def test_the_real_conftest_registers_the_guard_and_hands_it_its_config(tmp_path):
    # The wiring in tests/conftest.py: the plugin is registered at configure, and collection passes the config, so a
    # worker uses the controller's decision. (A guarded test is never run here: no opt-in, and it is only collected.)
    source = (TESTS_DIR / "conftest.py").read_text(encoding="utf-8")
    assert "gateway_port_guard.configure(config, _GATEWAY_PORT_TEST_NAMES)" in source
    assert "gateway_port_guard.apply(items, _GATEWAY_PORT_TEST_NAMES, config=config)" in source
    env = {k: v for k, v in os.environ.items() if k not in (guard.OPT_IN_VAR, "PYTEST_ADDOPTS")}
    done = subprocess.run(  # noqa: S603 - fixed argv, test-only, no shell
        [
            sys.executable,
            "-m",
            "pytest",
            GUARDED_NODE,
            "--collect-only",
            "-q",
            "--trace-config",
            "-p",
            "no:cacheprovider",
            "--basetemp",
            str(tmp_path / "inner"),
        ],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "GuardPlugin" in done.stdout  # --trace-config prints each registered plugin object


# -- round 7: blame only what the test provably owns; never probe a test whose body never ran -------------------------

WAIT_HELPERS = """
import glob
import os
import time


def wait_for(path, seconds=30):
    deadline = time.time() + seconds
    while not os.path.exists(path) and time.time() < deadline:
        time.sleep(0.05)
    return os.path.exists(path)


def another_process_looked_up_the_owner(seconds=30):
    deadline = time.time() + seconds
    while time.time() < deadline:
        for path in glob.glob(os.environ["P1_PROBE_LOG"] + ".*"):
            if not path.endswith("." + str(os.getpid())):
                with open(path, encoding="utf-8") as handle:
                    if any(" owners " in line for line in handle):
                        return True
        time.sleep(0.05)
    return False
"""

HANDSHAKE_A = (
    WAIT_HELPERS
    + """
import socket

KEPT = []


def test_guarded_a():
    sock = socket.socket()
    sock.bind(("127.0.0.1", int(os.environ["P1_LISTEN_PORT"])))
    sock.listen(1)
    with open(os.environ["P1_BOUND"], "w", encoding="utf-8") as handle:
        handle.write(str(os.getpid()))
    if os.environ["P1_A_MODE"] == "keep":
        KEPT.append(sock)                                    # left listening on purpose
        return
    another_process_looked_up_the_owner()                    # B's teardown check has run: now close
    sock.close()
"""
)

HANDSHAKE_B = (
    WAIT_HELPERS
    + """

def test_guarded_b():
    assert wait_for(os.environ["P1_BOUND"])                  # A holds the port; B itself holds nothing
    with open(os.environ["P1_BOUND"] + ".b", "w", encoding="utf-8") as handle:
        handle.write(str(os.getpid()))
"""
)


def _two_worker_handshake(project, monkeypatch, mode):
    port = _free_port()
    monkeypatch.setenv("P1_LISTEN_PORT", str(port))
    monkeypatch.setenv("P1_ADDRESSES", json.dumps([["127.0.0.1", port]]))
    monkeypatch.setenv("P1_REAL_PROBE", "1")
    monkeypatch.setenv("P1_BOUND", str(project.log / "bound.txt"))
    monkeypatch.setenv("P1_A_MODE", mode)
    project.makepyfile(test_a=HANDSHAKE_A, test_b=HANDSHAKE_B)
    result = run_inner(project, monkeypatch, workers=2, dist="loadfile")
    a_pid = (project.log / "bound.txt").read_text(encoding="utf-8")
    b_pid = (project.log / "bound.txt.b").read_text(encoding="utf-8")
    assert a_pid != b_pid, "the two tests must run in two different workers"
    return result, port


def test_a_clean_test_is_not_blamed_for_a_port_another_worker_holds(project, monkeypatch):
    # zteam's P2: worker A holds the port for its own legitimate reason while worker B's clean test finishes. B's
    # teardown check used to find the port busy and fail B (and the run). B does not own the listener, so the port is
    # only named, as held by another process; A closes it after its own teardown, so nothing is left.
    result, port = _two_worker_handshake(project, monkeypatch, "close")

    output = result.stdout.str()
    result.assert_outcomes(passed=2)
    assert result.ret == 0, output
    lines = _guard_lines(output)
    assert any("test_b.py::test_guarded_b" in line and "held by another process" in line for line in lines), lines
    assert not any(" left: " in line for line in lines), lines


def test_the_worker_that_leaves_its_listener_open_is_still_caught_and_the_other_one_is_not(project, monkeypatch):
    result, port = _two_worker_handshake(project, monkeypatch, "keep")

    output = result.stdout.str()
    assert result.ret == 1, output
    left = [line for line in _guard_lines(output) if " left: " in line]
    assert len(left) == 1 and "test_a.py::test_guarded_a" in left[0], left
    assert f"127.0.0.1:{port}" in left[0] and "a listener still holds" in left[0], left
    assert re.findall(r"ERROR at teardown of (\S+)", output) == ["test_guarded_a"], output


OUTSIDE_PROGRAM = """
import os
import socket
import sys
import time

port, go, bound, stop = int(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
host = sys.argv[5] if len(sys.argv) > 5 else "127.0.0.1"
deadline = time.time() + 60
while not os.path.exists(go) and time.time() < deadline:
    time.sleep(0.05)
sock = socket.socket()
sock.bind((host, port))
sock.listen(1)
with open(bound, "w", encoding="utf-8") as handle:
    handle.write(str(os.getpid()))
while not os.path.exists(stop) and time.time() < deadline:
    time.sleep(0.05)
"""

OUTSIDE_TESTS = (
    WAIT_HELPERS
    + """

def test_guarded_a():
    open(os.environ["P1_GO"], "w").close()                   # the outside program takes the port now, mid-test
    assert wait_for(os.environ["P1_BOUND"])
"""
)


def test_a_gateway_that_starts_outside_during_a_test_is_named_but_does_not_fail_the_run(project, monkeypatch):
    # The limit the docs used to describe ("the failure names that test as having left the listener, although it did
    # not start it"): a program outside the run takes the free port while a clean test runs.
    port = _free_port()
    go, bound, stop = (project.log / name for name in ("go.txt", "outside-bound.txt", "stop.txt"))
    monkeypatch.setenv("P1_ADDRESSES", json.dumps([["127.0.0.1", port]]))
    monkeypatch.setenv("P1_REAL_PROBE", "1")
    monkeypatch.setenv("P1_GO", str(go))
    monkeypatch.setenv("P1_BOUND", str(bound))
    project.makepyfile(test_outside=OUTSIDE_TESTS)
    outside = subprocess.Popen(  # noqa: S603 - fixed argv, test-only, no shell
        [sys.executable, "-c", OUTSIDE_PROGRAM, str(port), str(go), str(bound), str(stop)]
    )
    try:
        result = run_inner(project, monkeypatch)
    finally:
        stop.write_text("stop", encoding="utf-8")
        outside.wait(timeout=60)

    output = result.stdout.str()
    result.assert_outcomes(passed=1)
    assert result.ret == 0, output
    lines = _guard_lines(output)
    assert any(f"127.0.0.1:{port}" in line and "held by another process" in line for line in lines), lines
    assert not any(" left: " in line for line in lines), lines


@pytest.mark.parametrize("workers", [0, 1], ids=["serial", "parallel"])
@pytest.mark.parametrize(
    ("owners", "expected"),
    [("other", "pid 1"), ("unknown", "could not tell whose listener")],
)
def test_a_port_whose_owner_is_another_process_or_unknown_is_never_blamed_on_the_test(
    project, monkeypatch, workers, owners, expected
):
    # An owner that cannot be found out is "not proven to be this test's": named, not blamed.
    monkeypatch.setenv("P1_HELD_FLAG", str(project.log / "held.flag"))
    monkeypatch.setenv("P1_OWNERS", owners)
    project.makeconftest(INNER_CONFTEST + HOLD_AFTER_THE_DECISION)
    project.makepyfile(test_plain_guarded="def test_guarded_a():\n    pass\n")

    result = run_inner(project, monkeypatch, workers=workers)

    output = result.stdout.str()
    result.assert_outcomes(passed=1)
    assert result.ret == 0, output
    lines = _guard_lines(output)
    assert any("test_plain_guarded.py::test_guarded_a" in line and expected in line for line in lines), lines
    assert not any(" left: " in line for line in lines), lines


NEVER_RAN_TESTS = """
import pytest


@pytest.mark.xfail(run=False, reason="not run")
def test_guarded_a():
    raise AssertionError("never runs")


@pytest.fixture
def stops_in_setup():
    pytest.skip("skipped in setup")


def test_guarded_b(stops_in_setup):
    raise AssertionError("never runs")


def test_plain():
    pass
"""


@pytest.mark.parametrize("workers", [0, 1], ids=["serial", "parallel"])
def test_a_test_stopped_before_its_body_ran_is_not_probed_even_when_the_ports_are_held_later(
    project, monkeypatch, workers
):
    # zteam's P2: `xfail(run=False)` is reported as skipped with `wasxfail`, so it used to be probed after a body that
    # never ran, and the xfail marker then hid the "listener" failure as a second xfail while the run exited 1.
    monkeypatch.setenv("P1_HELD_FLAG", str(project.log / "held.flag"))
    project.makeconftest(INNER_CONFTEST + HOLD_AFTER_THE_DECISION)
    project.makepyfile(test_never=NEVER_RAN_TESTS)

    result = run_inner(project, monkeypatch, workers=workers)

    output = result.stdout.str()
    result.assert_outcomes(xfailed=1, skipped=1, passed=1)
    assert result.ret == 0, output
    teardown = [row for row in per_process_lines(project, "probes.log") if row.split()[1] == "teardown"]
    assert teardown == [], teardown
    assert _guard_lines(output) == [], output


XFAIL_LISTENER_TESTS = """
import os
import socket

import pytest

KEPT = []


@pytest.mark.xfail(reason="expected to fail")
def test_guarded_a():
    sock = socket.socket()
    sock.bind(("127.0.0.1", int(os.environ["P1_LISTEN_PORT"])))
    sock.listen(1)
    KEPT.append(sock)                                        # left listening on purpose
    assert False
"""


@pytest.mark.parametrize("workers", [0, 1], ids=["serial", "parallel"])
def test_an_executed_xfail_that_leaves_a_listener_still_fails_the_run(project, monkeypatch, workers):
    # The never-ran rule must not widen into "any xfail": a body that ran and then failed as expected keeps the check.
    port = _free_port()
    monkeypatch.setenv("P1_LISTEN_PORT", str(port))
    monkeypatch.setenv("P1_ADDRESSES", json.dumps([["127.0.0.1", port]]))
    monkeypatch.setenv("P1_REAL_PROBE", "1")
    project.makepyfile(test_xl=XFAIL_LISTENER_TESTS)

    result = run_inner(project, monkeypatch, workers=workers)

    output = result.stdout.str()
    assert result.ret == 1, output
    lines = _guard_lines(output)
    assert any("test_xl.py::test_guarded_a" in line and f"127.0.0.1:{port}" in line for line in lines), lines


def test_a_test_skipped_in_setup_after_a_child_started_still_has_the_child_ended_and_counted(project, monkeypatch):
    # Never-ran tests are not probed for ports; the child check is the one that stays.
    beat = project.log / "beat.txt"
    monkeypatch.setenv("P1_BEAT", str(beat))
    project.makepyfile(
        test_child=HEARTBEAT_CHILD
        + """

@pytest.fixture
def starts_a_child_then_skips():
    start_child()
    pytest.skip("skipped in setup after real work")


def test_guarded_a(starts_a_child_then_skips):
    pass
"""
    )

    result = run_inner(project, monkeypatch)

    assert result.ret == 1, result.stdout.str()
    assert any("test_child.py::test_guarded_a (skipped)" in line for line in _guard_lines(result.stdout.str()))
    assert _heartbeat_stopped(beat)


# -- the owner lookup itself ------------------------------------------------------------------------------------------

PROC_NET_TCP = """  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 0100007F:0FA0 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 12345 1 0 100 0 0
   1: 0100007F:0FA0 0100007F:C350 01 00000000:00000000 00:00000000 00000000  1000        0 777 1 0 100 0 0
   2: 0100007F:0FA1 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 999 1 0 100 0 0
"""


def test_the_proc_table_parser_picks_only_the_listener_on_the_port():
    links = {10: ["socket:[12345]", "pipe:[1]"], 11: ["socket:[777]"], 12: ["socket:[999]"], 13: []}
    # 0x0FA0; 777 is an open connection only
    assert guard._listener_pids_from_proc(PROC_NET_TCP, "127.0.0.1", 4000, links) == {10}
    assert guard._listener_pids_from_proc(PROC_NET_TCP, "127.0.0.1", 4001, links) == {12}
    assert guard._listener_pids_from_proc(PROC_NET_TCP, "127.0.0.1", 4002, links) == set()


OWNER_CHILD = """
import os
import socket
import time

sock = socket.socket()
sock.bind(("127.0.0.1", 0))
sock.listen(1)
print(os.getpid(), sock.getsockname()[1], flush=True)
time.sleep(60)
"""


def test_the_owner_lookup_names_the_process_that_listens_not_this_one():
    child = subprocess.Popen(  # noqa: S603 - fixed argv, test-only, no shell
        [sys.executable, "-c", OWNER_CHILD], stdout=subprocess.PIPE, text=True
    )
    try:
        listener_pid, port = (int(part) for part in child.stdout.readline().split())
        found = guard.listener_owners("127.0.0.1", port)
        if found is None and not (sys.platform == "win32" or sys.platform.startswith("linux")):
            pytest.skip("no owner lookup on this platform (needs lsof)")
        assert found == {listener_pid}, found
    finally:
        child.kill()
        child.wait(timeout=30)


def test_the_owner_lookup_of_a_free_port_finds_nothing_and_of_a_lookup_that_fails_says_unknown(monkeypatch):
    port = _free_port()
    assert guard.listener_owners("127.0.0.1", port) in (set(), None)
    for name in ("_owners_linux", "_owners_windows", "_owners_lsof"):
        monkeypatch.setattr(guard, name, lambda host, port: 1 / 0)
    assert guard.listener_owners("127.0.0.1", port) is None


def test_the_docstring_states_whose_listener_is_blamed_and_which_tests_are_never_probed():
    flat = " ".join((TESTS_DIR / "gateway_port_guard.py").read_text(encoding="utf-8").split())
    for text in (
        "a port is blamed on a test only when its owner is found and is this process or a child the test started",
        "is held by another process",
        "stopped before its body ran",
        "A test whose body never ran is not probed",
        "an xfail whose body did run keeps the full check",
        "An owner counts only for the exact endpoint the guard probed, not for the port number alone",
        "The same rule holds for the pytest process's own sockets",
        "is the very same socket object, still listening, is not the test's",
        "a new socket on the same port after the old one was closed is a different object",
        "the guard also asks the system how many listening sockets this process holds on the endpoint",
        "A live socket that cannot be read is a failed observation, not an absent one",
        "the rule follows socket objects, not the kernel's sockets",
        "The two numbers are in the same unit, kernel sockets, not objects",
        "and it stays so to the final decision: the claim is never dropped",
        "so the cached answer is valid only for workers started there",
        "An `lsof` run that reports an error is a count that could not be read, not a count of zero",
        "an IPv6 `::` listener owns it only when it is dual-stack",
        "so a real leftover listener can be missed when the system lookup fails too",
        "which can include a real leftover",
        'so "not held" is not proof that nothing listens',
        "the owner lookup is also asked about the guarded endpoints the probe called free",
        "never used the other way",
        "a launcher that spawns the provider and ends",
        "a real leftover can pass on those platforms",
        "only if it was born during that test, from that test",
        "nothing is arranged at configure or collection, and pytest itself never joins a job",
        "UNDECIDED is never ended",
        '"ended" is printed only after the exit is confirmed',
        "The guard never waits on a process it did not start",
        "Any failure to observe is UNDECIDED, never another process's and never the test's",
        "A chain that reaches this process itself proves nothing",
        "An owner that cannot be found out (which can include a real leftover) is UNDECIDED",
        "A table claim for this process that the scan cannot confirm is UNDECIDED",
        "an empty answer means the owner was not seen",
        "never `isinstance`, which follows a dead weak proxy",
        "JUnit's failure counts, the terminal summary count,",
        "the exit codes of xdist workers and `--last-failed` still show success",
    ):
        assert text in flat, text


# -- round 7 addition: what a guard-only failure looks like to each reader --------------------------------------------


@pytest.mark.parametrize("workers", [0, 2], ids=["serial", "parallel"])
@pytest.mark.parametrize("kind", ["xfail", "skip"])
def test_the_reducer_reports_the_status_the_process_returned_for_a_survivor(project, monkeypatch, workers, kind):
    # The experiment's reducer is the other reader of the journals. Its root process row (serial, or the xdist
    # controller) must carry the status the process returned, and no problem may say the journals disagree.
    beat = project.log / "beat.txt"
    monkeypatch.setenv("P1_BEAT", str(beat))
    project.makepyfile(test_survivor=XFAIL_SURVIVOR_TESTS if kind == "xfail" else SKIP_AFTER_CHILD_TESTS)

    result, _journals = run_recorded(project, monkeypatch, workers=workers)

    assert result.ret == 1, result.stdout.str() + result.stderr.str()
    reduced = experiment_record.reduce_journals(project.log / "journals")
    roots = [row["exitstatus"] for row in reduced["processes"] if row["role"] in ("serial", "controller")]
    assert roots == [1], reduced["processes"]
    assert [problem["kind"] for problem in reduced["problems"] if "exit-status" in problem["kind"]] == [], reduced
    assert reduced["first_error"] is None  # status 1 is "the tests failed", not an error of the run
    assert _heartbeat_stopped(beat)


@pytest.mark.parametrize("workers", [0, 2], ids=["serial", "parallel"])
def test_a_survivor_alone_leaves_the_junit_counts_green_while_the_process_returns_one(project, monkeypatch, workers):
    # The limit DEV-GATE.md states: pytest has already computed its counts when the guard rejects the run at the end,
    # and that end makes no failed report. Only the process exit status (and the guard's lines) carry the failure.
    # If this test starts to fail because the counts DO show it, update the sentence in DEV-GATE.md and the docstring.
    beat = project.log / "beat.txt"
    monkeypatch.setenv("P1_BEAT", str(beat))
    project.makepyfile(test_xfail=XFAIL_SURVIVOR_TESTS)
    junit = project.log / "junit.xml"

    result = run_inner(project, monkeypatch, f"--junitxml={junit}", workers=workers)

    assert result.ret == 1, result.stdout.str()
    suites = ElementTree.parse(junit).getroot().iter("testsuite")  # noqa: S314 - the test's own file
    assert [(suite.get("failures"), suite.get("errors")) for suite in suites] == [("0", "0")]
    assert _heartbeat_stopped(beat)


# -- round 8: the Linux lookup must find a listener of this very process ----------------------------------------------


def test_the_owner_lookup_finds_a_listener_held_by_this_very_process():
    # The guard's main case: the test's own process holds the port. (On Linux the first version missed it: the fd
    # directory listing of /proc/self/fd includes the descriptor used for the listing itself, which is gone by the time
    # it is read, and one unreadable descriptor dropped the whole process.)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        found = guard.listener_owners("127.0.0.1", sock.getsockname()[1])
        if found is None and not (sys.platform == "win32" or sys.platform.startswith("linux")):
            pytest.skip("no owner lookup on this platform (needs lsof)")
        assert found is not None and os.getpid() in found, found


def test_the_linux_lookup_survives_a_descriptor_that_vanishes_while_a_process_is_read(tmp_path, monkeypatch):
    # A fake /proc: process 100 holds the listener (inode 12345) and also a descriptor that is gone when read (as the
    # listing's own descriptor is in /proc/self/fd); process 200 holds only a pipe. Process 100 must still be found.
    (tmp_path / "net").mkdir()
    (tmp_path / "net" / "tcp").write_text(PROC_NET_TCP, encoding="utf-8")
    for pid, names in (("100", ("0", "3")), ("200", ("3",))):
        (tmp_path / pid / "fd").mkdir(parents=True)
        for name in names:
            (tmp_path / pid / "fd" / name).write_text("", encoding="utf-8")
    real_readlink = os.readlink

    def readlink(path, *args, **kwargs):
        link = Path(path)
        if link.parent.parent.name not in ("100", "200"):
            return real_readlink(path, *args, **kwargs)
        if link.name == "0":
            raise FileNotFoundError(path)  # gone since the listing
        return "socket:[12345]" if link.parent.parent.name == "100" else "pipe:[1]"

    monkeypatch.setattr(os, "readlink", readlink)

    assert guard._owners_linux("127.0.0.1", 4000, proc=str(tmp_path)) == {100}
    assert guard._owners_linux("127.0.0.1", 4002, proc=str(tmp_path)) == set()


def _second_loopback_works() -> bool:
    """Whether 127.0.0.2 can be bound here (macOS has only 127.0.0.1 on its loopback by default)."""
    try:
        with socket.socket() as sock:
            sock.bind(("127.0.0.2", 0))
    except OSError:
        return False
    return True


def _no_platform_lookup(monkeypatch):
    for name in ("_owners_linux", "_owners_windows", "_owners_lsof"):
        monkeypatch.setattr(guard, name, lambda host, port: None)


def test_a_listener_of_this_process_is_found_even_when_the_platform_lookup_cannot_say(monkeypatch):
    # The guard must always find a listener it can see in its own process, whatever the system lookup does (for
    # example `lsof` missing or refused on macOS): this process's open Python sockets are checked on every platform.
    _no_platform_lookup(monkeypatch)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        assert guard.listener_owners("127.0.0.1", port) == {os.getpid()}
    assert guard.listener_owners("127.0.0.1", port) is None  # closed again: nothing of ours, and the lookup cannot say


def test_a_socket_of_this_process_that_does_not_listen_is_not_its_listener(monkeypatch):
    _no_platform_lookup(monkeypatch)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))  # bound, not listening
        assert guard.listener_owners("127.0.0.1", sock.getsockname()[1]) is None


# -- round 9: an owner counts only for the exact endpoint the guard probes (family and address, not the port alone) --


def _ipv6_works() -> bool:
    if not socket.has_ipv6:
        return False
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as sock:
            sock.bind(("::1", 0))  # only whether IPv6 works here: the loopback address, closed at once
    except OSError:
        return False
    return True


@pytest.mark.parametrize(
    ("host", "bound", "dual_stack", "expected"),
    [
        ("127.0.0.1", "127.0.0.1", False, True),
        ("127.0.0.1", guard.ANY_ADDRESS, False, True),  # the IPv4 wildcard reaches the loopback address
        ("127.0.0.1", "127.0.0.2", False, False),  # another local address
        ("127.0.0.1", "::", True, True),  # an IPv6 wildcard, dual-stack
        ("127.0.0.1", "::", False, False),  # an IPv6-only wildcard does not
        ("127.0.0.1", "::1", True, False),  # the IPv6 loopback is not the IPv4 one
        ("127.0.0.1", "::ffff:127.0.0.1", False, True),  # an IPv4-mapped address exists only dual-stack
        ("127.0.0.1", "::ffff:127.0.0.2", True, False),
        ("::1", "::1", False, True),
        ("::1", "::", False, True),
        ("::1", guard.ANY_ADDRESS, False, False),  # an IPv4 listener never reaches an IPv6 endpoint
        ("not-an-address", "127.0.0.1", False, False),
    ],
)
def test_a_listener_reaches_an_endpoint_only_by_family_and_address(host, bound, dual_stack, expected):
    assert guard._reaches(host, bound, dual_stack) is expected


def _proc_rows(rows):
    """A /proc/net/tcp(6) text: only the columns the parser reads (state, local address, inode) carry real values."""
    head = "  sl  local_address rem_address st tx_queue rx_queue tr tm->when retrnsmt uid timeout inode\n"
    lines = (f"   {n}: {local}:0FA0 0:0 0A 0:0 0:0 0 1000 0 {inode} 1 0\n" for n, (local, inode) in enumerate(rows))
    return head + "".join(lines)


# 0x0FA0 is port 4000; 0100007F is 127.0.0.1 and 0200007F 127.0.0.2 (host byte order, little-endian)
PROC_TCP_ENDPOINTS = _proc_rows([("0100007F", 101), ("0200007F", 102), ("00000000", 103)])
PROC_TCP6_ENDPOINTS = _proc_rows(
    [
        ("0" * 32, 201),  # ::
        ("0" * 16 + "FFFF0000" + "0100007F", 202),  # ::ffff:127.0.0.1
        ("0" * 24 + "01000000", 203),  # ::1
        ("0" * 16 + "FFFF0000" + "0200007F", 204),  # ::ffff:127.0.0.2
    ]
)


def _fake_proc_with_sockets(tmp_path, monkeypatch, inodes_by_pid):
    (tmp_path / "net").mkdir()
    (tmp_path / "net" / "tcp").write_text(PROC_TCP_ENDPOINTS, encoding="utf-8")
    (tmp_path / "net" / "tcp6").write_text(PROC_TCP6_ENDPOINTS, encoding="utf-8")
    for pid, inodes in inodes_by_pid.items():
        (tmp_path / str(pid) / "fd").mkdir(parents=True)
        for number, _inode in enumerate(inodes):
            (tmp_path / str(pid) / "fd" / str(number)).write_text("", encoding="utf-8")
    real_readlink = os.readlink

    def readlink(path, *args, **kwargs):
        link = Path(path)
        pid = link.parent.parent.name
        if not pid.isdigit() or int(pid) not in inodes_by_pid:
            return real_readlink(path, *args, **kwargs)
        return f"socket:[{inodes_by_pid[int(pid)][int(link.name)]}]"

    monkeypatch.setattr(os, "readlink", readlink)


@pytest.mark.parametrize(
    ("inode", "owns"),
    [
        (101, True),  # 127.0.0.1:4000
        (102, False),  # 127.0.0.2:4000: another local address
        (103, True),  # 0.0.0.0:4000: the IPv4 wildcard
        (201, False),  # [::]:4000: /proc cannot tell dual-stack from IPv6-only, so it does not count
        (202, True),  # [::ffff:127.0.0.1]:4000: an IPv4-mapped address exists only on a dual-stack socket
        (203, False),  # [::1]:4000: the IPv6 loopback
        (204, False),  # [::ffff:127.0.0.2]:4000
    ],
)
def test_the_linux_lookup_matches_the_address_and_the_family_of_the_probed_endpoint(tmp_path, monkeypatch, inode, owns):
    # A fake /proc with tcp and tcp6 rows for port 4000 (0x0FA0). One process holds exactly one of them; the endpoint
    # asked about is 127.0.0.1:4000 (the old lookup matched the port alone and returned the process every time).
    _fake_proc_with_sockets(tmp_path, monkeypatch, {300: [inode]})

    found = guard._owners_linux("127.0.0.1", 4000, proc=str(tmp_path))

    assert found == ({300} if owns else set())


LSOF_ENDPOINTS = """p10
f5
tIPv4
n127.0.0.1:4000
p11
f5
tIPv4
n127.0.0.2:4000
p12
f5
tIPv4
n*:4000
p13
f5
tIPv6
n*:4000
p14
f6
tIPv6
n[::1]:4000
f7
tIPv4
n127.0.0.1:4000
"""


def test_the_lsof_lookup_matches_the_name_field_to_the_probed_endpoint():
    # `lsof -F` lists p<pid> then, per descriptor, f<fd>, t<type> and n<name>; the owners of 127.0.0.1:4000 are the
    # exact IPv4 address and the IPv4 wildcard (10, 12, 14's second descriptor), not another address (11), not an IPv6
    # wildcard (13: lsof cannot tell dual-stack from IPv6-only) and not the IPv6 loopback (14's first descriptor).
    assert guard._lsof_owners(LSOF_ENDPOINTS, "127.0.0.1", 4000) == {10, 12, 14}
    assert guard._lsof_owners(LSOF_ENDPOINTS, "::1", 4000) == {13, 14}  # an IPv6 endpoint: its own address, or [::]
    assert guard._lsof_owners(LSOF_ENDPOINTS, "127.0.0.1", 4001) == set()


OWN_SOCKET_TESTS = (
    WAIT_HELPERS
    + """
import socket

KEPT = []


def test_guarded_a():
    port = int(os.environ["P1_LISTEN_PORT"])
    if os.environ.get("P1_GO"):
        open(os.environ["P1_GO"], "w").close()                 # the outside program takes 127.0.0.1:port now
        assert wait_for(os.environ["P1_BOUND"])
    kind = os.environ["P1_OWN_KIND"]
    if kind == "none":
        return
    # The wildcard binds below are the point of the test and cannot be narrowed: the bind probe (on Windows) is blind
    # only beside a listener on the IPv4 wildcard or a dual-stack `::` (checked: a dual-stack socket on the narrower
    # ::ffff:127.0.0.1 is seen by the probe, and one on ::1 does not reach 127.0.0.1). Each socket lives only as
    # long as this child pytest run (it listens on an ephemeral port, nothing connects), and the run ends with the test.
    if kind in ("v6only", "dualstack"):
        sock = socket.socket(socket.AF_INET6)
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1 if kind == "v6only" else 0)
        address = ("::", port)
    else:
        sock = socket.socket()
        address = {"other-address": "127.0.0.2", "wildcard4": "0.0.0.0", "exact": "127.0.0.1"}[kind]  # noqa: S104
        address = (address, port)
    sock.bind(address)
    sock.listen(1)
    KEPT.append(sock)                                          # left listening on purpose
"""
)


def _run_with_own_socket(project, monkeypatch, kind, *, outside, outside_host="127.0.0.1", workers=0, recorded=False):
    """A child pytest run of one guarded test that holds `kind` of listener (or none), with the REAL bind probe and the
    REAL owner lookup: nothing is faked, so what the run returns is what the guard does."""
    port = _free_port()
    monkeypatch.setenv("P1_LISTEN_PORT", str(port))
    monkeypatch.setenv("P1_ADDRESSES", json.dumps([["127.0.0.1", port]]))
    monkeypatch.setenv("P1_REAL_PROBE", "1")
    monkeypatch.setenv("P1_OWN_KIND", kind)
    go, bound, stop = (project.log / name for name in ("go.txt", "outside-bound.txt", "stop.txt"))
    project.makepyfile(test_own=OWN_SOCKET_TESTS)
    process = None
    if outside:
        monkeypatch.setenv("P1_GO", str(go))
        monkeypatch.setenv("P1_BOUND", str(bound))
        process = subprocess.Popen(  # noqa: S603 - fixed argv, test-only, no shell
            [sys.executable, "-c", OUTSIDE_PROGRAM, str(port), str(go), str(bound), str(stop), outside_host]
        )
    try:
        if recorded:
            result, journals = run_recorded(project, monkeypatch, workers=workers)
            return result, port, journals
        return run_inner(project, monkeypatch, workers=workers), port, None
    finally:
        stop.write_text("stop", encoding="utf-8")
        if process is not None:
            process.wait(timeout=60)


@pytest.mark.parametrize("kind", ["v6only", "other-address"])
def test_a_listener_of_the_test_that_does_not_reach_the_endpoint_is_not_blamed_for_it(project, monkeypatch, kind):
    # codex-arch's two reproductions. An outside program holds 127.0.0.1:P. The test itself holds P only on an IPv6-only
    # wildcard (a different family) or only on 127.0.0.2 (a different address): neither is the endpoint the guard
    # probed, so the clean run is not failed for the outside listener.
    if kind == "v6only" and not _ipv6_works():
        pytest.skip("no IPv6 here")
    if kind == "other-address" and not _second_loopback_works():
        pytest.skip("this loopback has only 127.0.0.1")

    result, port, _journals = _run_with_own_socket(project, monkeypatch, kind, outside=True)

    output = result.stdout.str()
    result.assert_outcomes(passed=1)
    assert result.ret == 0, output
    lines = _guard_lines(output)
    assert any(f"127.0.0.1:{port}" in line and "held by another process" in line for line in lines), lines
    assert not any(" left: " in line for line in lines), lines


@pytest.mark.parametrize("workers", [0, 2], ids=["serial", "parallel"])
@pytest.mark.parametrize("kind", ["exact", "wildcard4", "dualstack"])
def test_a_leftover_listener_of_the_test_is_found_even_when_the_bind_probe_says_free(
    project, monkeypatch, kind, workers
):
    """667-R33-01 (codex-arch): on Windows a bind to 127.0.0.1 succeeds beside a listener on the IPv4 wildcard or a
    dual-stack `::`, so the probe says "free" and the owner lookup was never asked. The run exits 1 for a leftover the
    lookup proves is the test's own. Nothing is faked: real probe, real lookup, the real recorder next to the guard."""
    if kind == "dualstack" and not _ipv6_works():
        pytest.skip("no IPv6 here")

    result, port, journals = _run_with_own_socket(
        project, monkeypatch, kind, outside=False, workers=workers, recorded=True
    )

    output = result.stdout.str()
    assert result.ret == 1, output
    left = [line for line in _guard_lines(output) if " left: " in line]
    assert len(left) == 1 and "test_own.py::test_guarded_a" in left[0] and f"127.0.0.1:{port}" in left[0], left
    assert journals["controller" if workers else "serial"] == [([1], [1])], journals


def test_an_outside_wildcard_listener_is_not_blamed_on_a_clean_test_whatever_the_probe_says(project, monkeypatch):
    # The owner pass after the probe must not turn a port held by someone else into a failure (the false-blame class).
    # Nothing is faked: where the probe calls the port busy (POSIX) it is named, where it says free (Windows) it is not.
    result, port, _journals = _run_with_own_socket(
        project, monkeypatch, "none", outside=True, outside_host=guard.ANY_ADDRESS
    )

    output = result.stdout.str()
    result.assert_outcomes(passed=1)
    assert result.ret == 0, output
    assert not any(" left: " in line for line in _guard_lines(output)), output


@pytest.mark.parametrize(
    ("kind", "found"),
    [("v4-exact", True), ("v4-wildcard", True), ("v4-other", False), ("v6-dual", True), ("v6-only", False)],
)
def test_the_fallback_over_this_process_sockets_applies_the_same_rule(monkeypatch, kind, found):
    _no_platform_lookup(monkeypatch)
    if kind.startswith("v6") and not _ipv6_works():
        pytest.skip("no IPv6 here")
    if kind == "v4-other" and not _second_loopback_works():
        pytest.skip("this loopback has only 127.0.0.1")
    family = socket.AF_INET6 if kind.startswith("v6") else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:  # closed at the end of the block: the test's own lifetime
        if family == socket.AF_INET6:
            # An IPv6 wildcard on purpose: the rule under test is about `::` with and without IPV6_V6ONLY, which no
            # narrower address can show. Ephemeral port, nothing connects to it.
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0 if kind == "v6-dual" else 1)
            sock.bind(("::", 0))
        else:
            sock.bind(({"v4-exact": "127.0.0.1", "v4-wildcard": guard.ANY_ADDRESS, "v4-other": "127.0.0.2"}[kind], 0))
        sock.listen(1)
        owners = guard.listener_owners("127.0.0.1", sock.getsockname()[1])

    assert owners == ({os.getpid()} if found else None)


# -- round 11: a listener started by the test's descendant, whose parent already exited, is the test's own -------------

LAUNCHER_TESTS = (
    WAIT_HELPERS
    + """
import subprocess
import sys

LISTENER = (
    "import os, socket, sys, time; s = socket.socket(); s.bind(('127.0.0.1', int(sys.argv[1]))); s.listen(1); "
    "open(sys.argv[2], 'w').write(str(os.getpid())); time.sleep(120)"
)
# The launcher starts the listener and ends at once (the shape of a runner that starts the provider and exits).
LAUNCHER = "import subprocess, sys; subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2], sys.argv[3]])"


def test_guarded_a():
    launcher = subprocess.Popen(
        [sys.executable, "-c", LAUNCHER, LISTENER, os.environ["P1_LISTEN_PORT"], os.environ["P1_LISTENER_PID"]]
    )
    launcher.wait(timeout=60)                                    # the launcher is gone; its listener lives on
    assert wait_for(os.environ["P1_LISTENER_PID"])
"""
)


def _stop_listener(pid_file):
    if pid_file.exists():
        with contextlib.suppress(OSError, ValueError):
            os.kill(int(pid_file.read_text(encoding="utf-8")), signal.SIGTERM)


def _port_is_listened_on(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=2).close()
    except OSError:
        return False
    return True


@pytest.mark.parametrize("workers", [0, 2], ids=["serial", "parallel"])
def test_a_listener_started_by_a_launcher_that_already_exited_is_the_tests_own_leftover(project, monkeypatch, workers):
    """The GitHub reviewer on #667 (P1): the launcher's tracked Popen is gone by teardown and the listener's parent
    link is lost, so the listener was called "another process" and the run stayed 0. Real end to end: real probe, real
    owner lookup, real recorder. The run exits 1, the guard line names the test, the journal records 1, and the
    listener is ended."""
    port = _free_port()
    pid_file = project.log / "listener.pid"
    monkeypatch.setenv("P1_LISTEN_PORT", str(port))
    monkeypatch.setenv("P1_LISTENER_PID", str(pid_file))
    monkeypatch.setenv("P1_ADDRESSES", json.dumps([["127.0.0.1", port]]))
    monkeypatch.setenv("P1_REAL_PROBE", "1")
    project.makepyfile(test_launch=LAUNCHER_TESTS)
    try:
        result, journals = run_recorded(project, monkeypatch, workers=workers)

        output = result.stdout.str()
        assert result.ret == 1, output
        left = [line for line in _guard_lines(output) if " left: " in line]
        assert len(left) == 1 and "test_launch.py::test_guarded_a" in left[0], left
        if sys.platform != "win32":
            # Without a job a listener whose launcher has exited cannot be tied to the test (the worker's adoption
            # erased the edge): refused in an evidence run (this one records), never ended. The `finally` stops it.
            assert f"127.0.0.1:{port}" in left[0] and "could not tell whose" in left[0], left
            assert journals["controller" if workers else "serial"] == [([1], [1])], journals
            assert _port_is_listened_on(port)
            return
        assert f"127.0.0.1:{port}" in left[0] and "ended" in left[0], left
        assert journals["controller" if workers else "serial"] == [([1], [1])], journals
        assert not _port_is_listened_on(port), "the listener was left running"
    finally:
        _stop_listener(pid_file)


# -- round 12: an expired object is never a test error; a completed socket handoff is not the sender's leftover -------

DEAD_PROXY_TESTS = """
import os
import socket
import weakref

KEPT = []


def test_guarded_a():
    sock = socket.socket()
    KEPT.append(weakref.proxy(sock))                 # a weak proxy that stays behind, dead once the socket is gone
    del sock
    if os.environ.get("P1_LISTEN_PORT"):
        live = socket.socket()
        live.bind(("127.0.0.1", int(os.environ["P1_LISTEN_PORT"])))
        live.listen(1)
        KEPT.append(live)
        KEPT.append(weakref.proxy(live))             # and a live proxy to a real listener, which is only noise
"""


@pytest.mark.parametrize("workers", [0, 2], ids=["serial", "parallel"])
def test_a_retained_dead_weak_proxy_is_not_a_test_error(project, monkeypatch, workers):
    """zteam's P2 (4b7008e3): `isinstance` follows a dead `weakref.proxy` and raises ReferenceError, and the owner pass
    reaches it even when no port is busy, so a clean test failed. Looking at an unrelated or expired object can never
    become a test error. Real probe, real lookup."""
    port = _free_port()
    monkeypatch.setenv("P1_ADDRESSES", json.dumps([["127.0.0.1", port]]))
    monkeypatch.setenv("P1_REAL_PROBE", "1")
    monkeypatch.delenv("P1_LISTEN_PORT", raising=False)
    project.makepyfile(test_proxy=DEAD_PROXY_TESTS)

    result = run_inner(project, monkeypatch, workers=workers)

    result.assert_outcomes(passed=1)
    assert result.ret == 0, result.stdout.str()
    assert _guard_lines(result.stdout.str()) == []


@pytest.mark.parametrize("workers", [0, 2], ids=["serial", "parallel"])
def test_a_real_listener_is_still_found_among_dead_and_live_weak_proxies(project, monkeypatch, workers):
    port = _free_port()
    monkeypatch.setenv("P1_ADDRESSES", json.dumps([["127.0.0.1", port]]))
    monkeypatch.setenv("P1_REAL_PROBE", "1")
    monkeypatch.setenv("P1_LISTEN_PORT", str(port))
    project.makepyfile(test_proxy=DEAD_PROXY_TESTS)

    result, journals = run_recorded(project, monkeypatch, workers=workers)

    assert result.ret == 1, result.stdout.str()
    left = [line for line in _guard_lines(result.stdout.str()) if " left: " in line]
    assert len(left) == 1 and f"127.0.0.1:{port}" in left[0], left
    assert journals["controller" if workers else "serial"] == [([1], [1])], journals


def test_the_scan_of_this_process_sockets_survives_a_dead_proxy_and_a_hostile_object():
    import gc
    import weakref

    class Hostile:
        def __getattribute__(self, name):
            raise RuntimeError("will not be looked at")

    sock = socket.socket()
    dead = weakref.proxy(sock)
    del sock
    hostile = Hostile()
    gc.collect()

    assert guard._this_process_listens("127.0.0.1", _free_port()) is False
    assert dead is not None and hostile is not None


SHARE_HELPER = """
import os
import socket
import sys
import time

ready, handoff, taken, stop = sys.argv[1:5]
with open(ready, "w", encoding="utf-8") as handle:
    handle.write(str(os.getpid()))
deadline = time.time() + 90
while not os.path.exists(handoff) and time.time() < deadline:
    time.sleep(0.05)
while True:
    try:
        with open(handoff, encoding="utf-8") as handle:
            record = handle.read()
        break
    except OSError:  # Windows: a file only just renamed into place can be held for a moment by another program
        if time.time() > deadline:
            raise
        time.sleep(0.05)
kept = socket.fromshare(bytes.fromhex(record))
with open(taken, "w", encoding="utf-8") as handle:
    handle.write("taken")
while not os.path.exists(stop) and time.time() < deadline:
    time.sleep(0.05)
kept.close()
"""

SHARE_TESTS = (
    WAIT_HELPERS
    + """
import socket


def test_guarded_a():
    assert wait_for(os.environ["P1_READY"])                    # the helper already runs (not started by this test)
    with open(os.environ["P1_READY"], encoding="utf-8") as handle:
        helper = int(handle.read())
    sock = socket.socket()
    sock.bind(("127.0.0.1", int(os.environ["P1_LISTEN_PORT"])))
    sock.listen(1)
    temporary = os.environ["P1_HANDOFF"] + ".part"
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(sock.share(helper).hex())
        handle.flush()
        time.sleep(float(os.environ.get("P1_PUBLISH_DELAY", "0")))   # a slow writer: the record is not complete yet
    os.replace(temporary, os.environ["P1_HANDOFF"])           # published whole: the helper never sees half a record
    assert wait_for(os.environ["P1_TAKEN"])                    # the helper now holds the listener
    sock.close()                                               # and the sender lets go of its own descriptor
"""
)


@pytest.mark.skipif(os.name != "nt", reason="socket.share / fromshare exist on Windows only")
@pytest.mark.parametrize("evidence", [False, True], ids=["ordinary", "evidence"])
@pytest.mark.parametrize("workers", [0, 2], ids=["serial", "parallel"])
def test_a_listener_handed_to_a_running_helper_is_not_the_senders_leftover(project, monkeypatch, workers, evidence):
    """zteam's P2 (4b7008e3): after a completed `socket.share` / `socket.fromshare` handoff the Windows TCP table still
    names the process that created the socket. The sender's own scan finds nothing, its descriptor is closed: the table
    claim alone is not proof, so the helper's listener is not the sender's leftover. The table names nobody else, so no
    owner can be established: UNDECIDED (named in an ordinary run, refused in an evidence run), never ended. The record
    is published whole (a slow publication, set here, must not matter: codex-arch R39-02)."""
    port = _free_port()
    ready, handoff, taken, stop = (project.log / name for name in ("ready.txt", "handoff.txt", "taken.txt", "stop.txt"))
    monkeypatch.setenv("P1_LISTEN_PORT", str(port))
    monkeypatch.setenv("P1_ADDRESSES", json.dumps([["127.0.0.1", port]]))
    monkeypatch.setenv("P1_REAL_PROBE", "1")
    monkeypatch.setenv("P1_READY", str(ready))
    monkeypatch.setenv("P1_HANDOFF", str(handoff))
    monkeypatch.setenv("P1_TAKEN", str(taken))
    monkeypatch.setenv("P1_PUBLISH_DELAY", "0.5")
    project.makepyfile(test_share=SHARE_TESTS)
    helper = subprocess.Popen(  # noqa: S603 - fixed argv, test-only, no shell
        [sys.executable, "-c", SHARE_HELPER, str(ready), str(handoff), str(taken), str(stop)]
    )
    try:
        if evidence:
            result, journals = run_recorded(project, monkeypatch, workers=workers)
        else:
            result, journals = run_inner(project, monkeypatch, workers=workers), None
        alive = _port_is_listened_on(port)
    finally:
        stop.write_text("stop", encoding="utf-8")
        helper.wait(timeout=60)

    output = result.stdout.str()
    lines = _guard_lines(output)
    assert alive, "an undecided listener must never be ended"
    if evidence:
        assert result.ret == 1, output
        assert any(" left: " in line and "could not tell whose" in line for line in lines), lines
        assert journals["controller" if workers else "serial"] == [([1], [1])], journals
    else:
        result.assert_outcomes(passed=1)
        assert result.ret == 0, output
        assert any(f"127.0.0.1:{port}" in line and "could not tell whose listener" in line for line in lines), lines
        assert not any(" left: " in line for line in lines), lines


# -- round 13: a listener is the test's only if it was born during that test, from that test ---------------------------

LISTENER_SOURCE = (
    "import os, socket, sys, time; s = socket.socket(); s.bind(('127.0.0.1', int(sys.argv[1]))); s.listen(1); "
    "open(sys.argv[2], 'w').write(str(os.getpid())); time.sleep(120)"
)
LAUNCHER_SOURCE = (
    "import subprocess, sys; subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2], sys.argv[3]])"
)
# a launcher of a launcher: two parents are gone by the time the listener is checked
LAUNCHER2_SOURCE = (
    "import subprocess, sys; "
    "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]])"
)


def _listener_env(project, monkeypatch):
    port = _free_port()
    pid_file = project.log / "listener.pid"
    monkeypatch.setenv("P1_LISTEN_PORT", str(port))
    monkeypatch.setenv("P1_LISTENER_PID", str(pid_file))
    monkeypatch.setenv("P1_ADDRESSES", json.dumps([["127.0.0.1", port]]))
    monkeypatch.setenv("P1_REAL_PROBE", "1")
    return port, pid_file


EARLIER_UNGUARDED_TESTS = (
    WAIT_HELPERS
    + """
import socket
import subprocess
import sys

LISTENER = %r
LAUNCHER = %r


def test_guarded_a():
    pass                                                         # a guarded warm-up: a window opens and closes


def test_starts_a_listener():                                    # NOT guarded: nothing watches it
    args = [LISTENER, os.environ["P1_LISTEN_PORT"], os.environ["P1_LISTENER_PID"]]
    if os.environ["P1_MODE"] == "direct":
        subprocess.Popen([sys.executable, "-c", *args])
    else:
        subprocess.Popen([sys.executable, "-c", LAUNCHER, *args]).wait(timeout=60)
    assert wait_for(os.environ["P1_LISTENER_PID"])


def test_guarded_b():
    pass                                                         # clean, and a leftover of an EARLIER test is busy now


def test_the_listener_is_still_alive():                          # NOT guarded
    socket.create_connection(("127.0.0.1", int(os.environ["P1_LISTEN_PORT"])), timeout=5).close()
"""
    % (LISTENER_SOURCE, LAUNCHER_SOURCE)
)


@pytest.mark.parametrize("mode", ["direct", "launcher"])
def test_a_listener_an_earlier_unguarded_test_started_survives_a_clean_guarded_test(project, monkeypatch, mode):
    """codex-arch R35-01: the worker-wide job (Windows) or subreaper (Linux) made every process the worker ever started
    "ours", so a clean guarded test killed a listener that an EARLIER, unguarded test had started. A listener is the
    test's only if it was born during that test, from that test."""
    port, pid_file = _listener_env(project, monkeypatch)
    monkeypatch.setenv("P1_MODE", mode)
    project.makepyfile(test_earlier=EARLIER_UNGUARDED_TESTS)
    try:
        result = run_inner(project, monkeypatch)

        output = result.stdout.str()
        result.assert_outcomes(passed=4)
        assert result.ret == 0, output
        assert not any(" left: " in line for line in _guard_lines(output)), output
    finally:
        _stop_listener(pid_file)


# -- the window exists only while a guarded test runs --------------------------------------------------------------

WINDOW_TESTS = (
    WAIT_HELPERS
    + """
import ctypes
import sys


def membership():
    if sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        value = ctypes.c_int(-1)
        libc.prctl(37, ctypes.byref(value), 0, 0, 0)             # PR_GET_CHILD_SUBREAPER
        return value.value
    if sys.platform == "win32":
        import gateway_port_guard as guard
        window = getattr(guard, "_CURRENT_WINDOW", None)           # the guard's window, if one is open right now
        if window is None:
            return "no-window"
        members = guard._job_pids(window.job) if window.job else set()
        return f"window-self-in-job={os.getpid() in members}"       # pytest itself must not be in the job
    return "n/a"


def record(name):
    with open(os.environ["P1_WINDOW_LOG"], "a", encoding="utf-8") as handle:
        handle.write(f"{name} {membership()}\\n")


if os.environ.get("P1_PRESET_SUBREAPER") == "1" and sys.platform.startswith("linux"):
    ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0)       # the flag was already set before the run


def test_before():
    record("before")


def test_guarded_a():
    record("during")


def test_after():
    record("after")
"""
)


@pytest.mark.skipif(not (sys.platform == "win32" or sys.platform.startswith("linux")), reason="no window to see here")
@pytest.mark.parametrize("preset", [False, True], ids=["flag-off", "flag-already-on"])
def test_tracking_is_on_only_while_a_guarded_test_runs(project, monkeypatch, preset):
    """The arrangement exists only while a guarded test runs. Linux: the subreaper flag reads 0, 1, 0 (an already-set
    flag stays 1 throughout). Windows: a job exists only while the test runs, and pytest itself is never in it."""
    if preset and not sys.platform.startswith("linux"):
        pytest.skip("the subreaper flag is Linux only")
    log = project.log / "window.log"
    monkeypatch.setenv("P1_WINDOW_LOG", str(log))
    monkeypatch.setenv("P1_PRESET_SUBREAPER", "1" if preset else "0")
    monkeypatch.setenv("P1_ADDRESSES", json.dumps([["127.0.0.1", _free_port()]]))
    project.makepyfile(test_window=WINDOW_TESTS)

    result = run_inner(project, monkeypatch)

    result.assert_outcomes(passed=3)
    seen = dict(line.split(" ", 1) for line in lines_of(log))
    if sys.platform.startswith("linux"):
        expected = {"before": "1", "during": "1", "after": "1"}
        if not preset:
            expected = {"before": "0", "during": "1", "after": "0"}
        assert seen == expected
    else:
        expected = {"before": "no-window", "during": "window-self-in-job=False", "after": "no-window"}
        assert seen == expected


def test_nothing_is_arranged_with_the_opt_in_off(monkeypatch):
    """With the gateway opt-in off no window is opened: the Popen tracker is not installed, no process list is read, no
    flag or job is touched."""
    monkeypatch.delenv(guard.OPT_IN_VAR, raising=False)
    original = subprocess.Popen.__init__
    reads = []
    monkeypatch.setattr(guard, "_parent_links", lambda: reads.append("read") or {})
    plugin = guard.GuardPlugin(
        frozenset({"test_guarded_a"}), {}, [("127.0.0.1", 1)], lambda host, port: False, lambda seconds: None
    )
    item = SimpleNamespace(name="test_guarded_a", nodeid="t.py::test_guarded_a", get_closest_marker=lambda name: None)

    plugin.pytest_runtest_setup(item)

    assert subprocess.Popen.__init__ is original and reads == [] and plugin._watching == {}


# -- "ended" only after the exit is confirmed --------------------------------------------------------------------


class FakeKernel:
    """The few kernel32 calls `_terminate_windows` makes, with a scripted outcome per process id."""

    def __init__(self, script):
        self.script = script
        self.error = 0

    def OpenProcess(self, access, inherit, pid):  # noqa: N802 - the kernel's name
        step = self.script[pid]
        self.error = step.get("open_error", 0)
        return 0 if step.get("open_error") else pid

    def TerminateProcess(self, handle, code):  # noqa: N802
        return 1 if self.script[handle].get("terminate", True) else 0

    def WaitForSingleObject(self, handle, milliseconds):  # noqa: N802
        return self.script[handle].get("wait", 0)

    def CloseHandle(self, handle):  # noqa: N802
        return 1


@pytest.mark.parametrize(
    ("step", "ended"),
    [
        ({"open_error": 87}, True),  # ERROR_INVALID_PARAMETER: there is no such process any more
        ({"open_error": 5}, False),  # access denied: nothing is known
        ({"terminate": False, "wait": 0x102}, False),  # the termination failed and the wait timed out
        ({"terminate": False, "wait": 0}, True),  # the termination failed, but the process has really exited
        ({"terminate": True, "wait": 0x102}, False),  # terminated, but the exit was not seen in time
        ({"terminate": True, "wait": 0}, True),
    ],
    ids=["gone", "refused", "failed-and-timeout", "failed-but-exited", "terminated-but-unseen", "terminated"],
)
def test_ended_is_said_only_after_the_exit_is_confirmed_on_windows(step, ended):
    """codex-arch R35-02: a failed native termination plus a timed-out wait still printed "ended"."""
    kernel = FakeKernel({7: step})

    left = guard._terminate_windows([7], kernel, lambda: kernel.error)

    assert left == (set() if ended else {7})


@pytest.mark.parametrize(
    ("kill_error", "still_there", "ended"),
    [(ProcessLookupError, False, True), (PermissionError, False, False), (None, True, False), (None, False, True)],
    ids=["already-gone", "permission-error", "still-alive-at-deadline", "gone-after-kill"],
)
def test_ended_is_said_only_after_the_exit_is_confirmed_on_posix(kill_error, still_there, ended):
    def kill(pid, sig):
        if kill_error is not None:
            raise kill_error()

    left = guard._terminate_posix([7], kill, lambda pid: not still_there, wait=0.05)

    assert left == (set() if ended else {7})


def test_the_end_of_a_descendant_that_is_not_confirmed_is_said_plainly(project, monkeypatch):
    """End to end: the exit of the ended descendant is never confirmed (faked in the child run), so the line says
    "not ended", and the run fails all the same."""
    port, pid_file = _listener_env(project, monkeypatch)
    monkeypatch.setenv("P1_NOT_ENDED", "1")
    project.makepyfile(test_far=LIVE_ROOT_TESTS)  # a listener under a child that is still alive: the test's everywhere
    try:
        result = run_inner(project, monkeypatch)

        left = [line for line in _guard_lines(result.stdout.str()) if " left: " in line]
        assert result.ret == 1 and len(left) == 1 and "not ended" in left[0], result.stdout.str()
    finally:
        _stop_listener(pid_file)


# -- the guard never waits on a process it did not start --------------------------------------------------------


@pytest.mark.skipif(os.name != "posix", reason="POSIX reaping")
@pytest.mark.parametrize("platform", [None, "darwin"], ids=["this-platform", "macos-path"])
def test_the_guard_never_waits_on_a_process_it_did_not_start(monkeypatch, platform):
    """codex-arch R35-03: the guard waited on every pid it ended, which reaps (or steals the exit status of) a process
    some other part of the run started. It only signals, and looks the process up.

    The look-up is where the platforms differ: Linux reads /proc, but elsewhere (macOS) `_ended` runs `ps`, and
    `subprocess.run` waits for ITS OWN `ps` child. That is a process the guard started, so it is allowed; only a wait on
    a process the guard did not start (the target) is a fault. The macOS path is forced here (a patched `sys.platform`),
    so it is checked on every POSIX machine, not only on a Mac."""
    if platform == "darwin" and shutil.which("ps") is None:
        pytest.skip("the macOS look-up runs ps")
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])  # noqa: S603
    waited, started = [], []
    real_waitpid, real_init = os.waitpid, subprocess.Popen.__init__

    def tracking_init(self, *args, **kwargs):
        real_init(self, *args, **kwargs)
        started.append(self.pid)

    monkeypatch.setattr(os, "waitpid", lambda pid, options: waited.append(pid) or real_waitpid(pid, options))
    monkeypatch.setattr(subprocess.Popen, "__init__", tracking_init)
    if platform:
        monkeypatch.setattr(sys, "platform", platform)
    try:
        left = guard._terminate_processes([child.pid])
    finally:
        monkeypatch.undo()
        child.kill()
    assert child.pid not in waited, waited
    assert set(waited) <= set(started), (waited, started)  # whatever was waited for, the guard had started it itself
    assert left in (set(), {child.pid})
    assert child.wait(timeout=30) is not None  # the exit status is still ours to read


# -- undecided is never ended -------------------------------------------------------------------------------------

UNDECIDED_TESTS = (
    WAIT_HELPERS
    + """

def test_guarded_a():
    open(os.environ["P1_GO"], "w").close()                       # the outside program takes the port now, mid-test
    assert wait_for(os.environ["P1_BOUND"])
"""
)


def _outside_alive(pid_file):
    try:
        pid = int(pid_file.read_text(encoding="utf-8"))
        return _pid_is_alive(pid)
    except (OSError, ValueError):
        return False


def _pid_is_alive(pid):
    if sys.platform == "win32":
        import ctypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = ctypes.c_void_p
        handle = k32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return False
        try:
            return k32.WaitForSingleObject(ctypes.c_void_p(handle), 0) == 0x102  # WAIT_TIMEOUT: still running
        finally:
            k32.CloseHandle(ctypes.c_void_p(handle))
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


@pytest.mark.parametrize("evidence", [False, True], ids=["ordinary", "evidence"])
def test_an_undecided_listener_is_never_ended(project, monkeypatch, evidence):
    """The plan's three answers: mine / other / undecided. The window is forced to "no proof" here on every platform. An
    ordinary run names the port and stays green; an evidence run (`--experiment-record`) refuses it; neither ends it."""
    port = _free_port()
    go, bound, stop = (project.log / name for name in ("go.txt", "outside-bound.txt", "stop.txt"))
    monkeypatch.setenv("P1_ADDRESSES", json.dumps([["127.0.0.1", port]]))
    monkeypatch.setenv("P1_REAL_PROBE", "1")
    monkeypatch.setenv("P1_FORCE_UNDECIDED", "1")
    monkeypatch.setenv("P1_GO", str(go))
    monkeypatch.setenv("P1_BOUND", str(bound))
    project.makepyfile(test_undecided=UNDECIDED_TESTS)
    outside = subprocess.Popen(  # noqa: S603 - fixed argv, test-only, no shell
        [sys.executable, "-c", OUTSIDE_PROGRAM, str(port), str(go), str(bound), str(stop)]
    )
    try:
        if evidence:
            result, journals = run_recorded(project, monkeypatch)
        else:
            result, journals = run_inner(project, monkeypatch), None
        alive = _outside_alive(bound)
    finally:
        stop.write_text("stop", encoding="utf-8")
        outside.wait(timeout=60)

    output = result.stdout.str()
    lines = _guard_lines(output)
    assert alive, "an undecided listener must never be ended"
    if evidence:
        assert result.ret == 1, output
        assert any(" left: " in line and "could not tell whose" in line for line in lines), lines
        assert journals["serial"] == [([1], [1])], journals
    else:
        result.assert_outcomes(passed=1)
        assert result.ret == 0, output
        assert any("could not tell whose listener" in line for line in lines), lines
        assert not any(" left: " in line for line in lines), lines


# -- the chain rule ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pid", "links", "before", "tracked", "subreaper", "answer"),
    [
        (30, {30: 20, 20: 10}, set(), {20}, False, "mine"),  # a chain up to a child the test started (pid 20)
        (30, {30: 25, 25: 20, 20: 10}, set(), {20}, False, "mine"),  # through new processes only
        (20, {20: 10}, set(), {20}, False, "mine"),  # the tracked child itself
        (30, {30: 10}, set(), set(), True, "undecided"),  # reaches the worker: adopted, the old edge is gone
        (30, {30: 20, 20: 10}, set(), set(), True, "undecided"),  # through new processes, but to the worker itself
        (30, {30: 20, 20: 10}, {30}, {20}, True, "other"),  # alive when the test began: an earlier test's
        (30, {30: 20, 20: 10}, {20}, set(), True, "other"),  # parent already there: with the flag, no orphan of ours
        (30, {30: 20, 20: 10}, {20}, set(), False, "undecided"),  # no flag: the older parent may be an orphan of ours
        (30, {30: 20, 20: 10}, {20}, {20}, True, "mine"),  # a tracked child is the test's even if listed before
        (30, {30: 20}, set(), set(), False, "undecided"),  # a parent that is not in the process list: unknown
        (30, {30: 20, 20: 30}, set(), set(), False, "undecided"),  # a loop
        (30, {30: 20, 20: 10}, None, {20}, False, "undecided"),  # the start was not seen
        (30, None, set(), {20}, False, "undecided"),  # the process list was not read
    ],
    ids=[
        "chain-to-a-started-child",
        "through-new-processes",
        "the-started-child",
        "adopted-by-the-worker",
        "new-chain-to-the-worker",
        "alive-before",
        "older-parent-flag",
        "older-parent-no-flag",
        "started-child-wins",
        "unknown-parent",
        "loop",
        "no-snapshot",
        "no-links",
    ],
)
def test_the_chain_rule(pid, links, before, tracked, subreaper, answer):
    assert guard._tie(pid, links, before, tracked, 10, subreaper) == answer


# -- the listening check ---------------------------------------------------------------------------------------


def test_the_listening_check_answers_on_this_platform():
    """Real sockets, every platform: a bound socket does not listen, a listening one does. On macOS the old check
    (SO_ACCEPTCONN) is refused by the kernel, which is what made the macOS legs of CI fail."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        assert guard._listens(sock) is False
        sock.listen(1)
        assert guard._listens(sock) is True


class FakeStateSocket:
    def __init__(self, state):
        self.state = state

    def getsockopt(self, level, option, size=0):
        assert (level, option, size) == (socket.IPPROTO_TCP, 0x106, 1)  # TCP_CONNECTION_INFO, its first byte
        return bytes([self.state])


@pytest.mark.parametrize(("state", "listening"), [(1, True), (4, False)], ids=["TCPS_LISTEN", "TCPS_ESTABLISHED"])
def test_macos_reads_the_tcp_state(monkeypatch, state, listening):
    monkeypatch.setattr(sys, "platform", "darwin")

    assert guard._listens(FakeStateSocket(state)) is listening


TWO_HOP_TESTS = (
    WAIT_HELPERS
    + """
import subprocess
import sys

LISTENER = %r
LAUNCHER = %r
LAUNCHER2 = %r


def test_guarded_a():
    args = [LISTENER, os.environ["P1_LISTEN_PORT"], os.environ["P1_LISTENER_PID"]]
    subprocess.Popen([sys.executable, "-c", LAUNCHER2, LAUNCHER, *args]).wait(timeout=60)   # two parents gone
    assert wait_for(os.environ["P1_LISTENER_PID"])
"""
    % (LISTENER_SOURCE, LAUNCHER_SOURCE, LAUNCHER2_SOURCE)
)

LIVE_ROOT_TESTS = (
    WAIT_HELPERS
    + """
import subprocess
import sys

LISTENER = %r
ROOT = (
    "import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2], sys.argv[3]]); "
    "time.sleep(120)"
)


def test_guarded_a():
    # a child that stays alive and has started the listener under it: ending the child orphans the listener
    args = [LISTENER, os.environ["P1_LISTEN_PORT"], os.environ["P1_LISTENER_PID"]]
    subprocess.Popen([sys.executable, "-c", ROOT, *args])
    assert wait_for(os.environ["P1_LISTENER_PID"])
"""
    % (LISTENER_SOURCE,)
)


@pytest.mark.parametrize("shape", ["two-hops", "live-root"])
def test_a_listener_far_from_the_test_is_the_tests_own_leftover(project, monkeypatch, shape):
    """Two dead launchers away: the test's own only where a job lists it (Windows); on Linux and macOS the edge is gone
    and it is undecided. A listener under a child that is still alive when the guard ends it is the test's everywhere
    (the chain is read before the child is ended)."""
    port, pid_file = _listener_env(project, monkeypatch)
    project.makepyfile(test_far=TWO_HOP_TESTS if shape == "two-hops" else LIVE_ROOT_TESTS)
    try:
        result, journals = run_recorded(project, monkeypatch)

        output = result.stdout.str()
        assert result.ret == 1, output
        left = [line for line in _guard_lines(output) if " left: " in line]
        assert len(left) == 1 and "test_far.py::test_guarded_a" in left[0], left
        assert journals["serial"] == [([1], [1])], journals
        if shape == "two-hops" and sys.platform != "win32":
            assert f"127.0.0.1:{port}" in left[0] and "could not tell whose" in left[0], left
            assert _port_is_listened_on(port)  # undecided is never ended
            return
        assert f"127.0.0.1:{port}" in left[0] and left[0].rstrip().endswith("ended"), left
        assert not _port_is_listened_on(port), "the listener was left running"
    finally:
        _stop_listener(pid_file)


# -- round 14: a failure to observe is never proof (ownership, exit and adoption) -------------------------------------


@pytest.mark.parametrize(
    ("answers", "expected"),
    [
        ([(0, 5)], None),  # the query fails (access denied)
        ([(0, 234)] * 6, None),  # the list never fits: capacity exhausted
        ([(1, 0)], set()),  # a job that really has no member is an empty set, not "not known"
    ],
    ids=["query-fails", "list-never-fits", "really-empty"],
)
def test_a_failed_job_query_is_not_known_and_an_empty_job_is_empty(answers, expected):
    """codex-arch R38-01: a failed native query became an empty set, i.e. "nobody is in the job"."""
    steps = iter(answers)
    state = {}

    class Kernel:
        def QueryInformationJobObject(self, job, klass, buffer, size, returned):  # noqa: N802
            ok, state["error"] = next(steps)
            if ok:
                struct.pack_into("<II", buffer, 0, 0, 0)  # zero processes listed
            return ok

    assert guard._job_pids(1, Kernel(), lambda: state["error"]) == expected


@pytest.mark.skipif(sys.platform != "win32", reason="a job object is Windows only")
@pytest.mark.parametrize("evidence", [False, True], ids=["ordinary", "evidence"])
def test_a_failed_job_query_is_undecided_and_never_an_empty_job(project, monkeypatch, evidence):
    """The real descendant listener of a launcher (really a job member), with the query of the job's members failing at
    the native boundary: the answer is UNDECIDED. An ordinary run names it and passes; an evidence run refuses it
    (exit 1, journal 1); neither ends the listener."""
    port, pid_file = _listener_env(project, monkeypatch)
    monkeypatch.setenv("P1_QUERY_FAILS", "1")
    project.makepyfile(test_launch=LAUNCHER_TESTS)
    try:
        if evidence:
            result, journals = run_recorded(project, monkeypatch)
        else:
            result, journals = run_inner(project, monkeypatch), None
        alive = _port_is_listened_on(port)

        output = result.stdout.str()
        lines = _guard_lines(output)
        assert alive, "an undecided listener must never be ended"
        if evidence:
            assert result.ret == 1, output
            assert any(" left: " in line and "could not tell whose" in line for line in lines), lines
            assert journals["serial"] == [([1], [1])], journals
        else:
            result.assert_outcomes(passed=1)
            assert result.ret == 0, output
            assert any("could not tell whose listener" in line for line in lines), lines
    finally:
        _stop_listener(pid_file)


@pytest.mark.parametrize(
    "platform, failure",
    [("darwin", "ps-fails"), ("linux", "listing-fails")],
    ids=["ps-fails", "proc-listing-fails"],
)
def test_a_failed_read_of_the_process_list_is_not_an_empty_list(monkeypatch, platform, failure):
    """codex-arch R38-01 (POSIX): `ps` failing with an empty output, or an unreadable /proc, was an empty snapshot."""
    monkeypatch.setattr(sys, "platform", platform)
    if failure == "ps-fails":
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=2, stdout="", stderr="ps: failed")
        )
    else:

        def refused(path):
            raise PermissionError(path)

        monkeypatch.setattr(os, "listdir", refused)

    assert guard._parent_links() is None


def test_a_failed_start_snapshot_makes_every_untracked_owner_undecided(monkeypatch):
    """With the start of the test unseen (`before` is None), even an owner whose chain reaches a child the test started
    is UNDECIDED, not the test's: nothing says it was not already there."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(guard, "_parent_links", lambda: None)
    window = guard.Window()
    window.open()
    try:
        assert window.before is None
        monkeypatch.setattr(guard, "_parent_links", lambda: {40: 20, 20: 10})
        evidence = window.evidence({20})
        assert guard._classify(40, evidence) == guard.UNDECIDED
    finally:
        window.close()


class _FileRead:
    """`open` for a /proc stat file with a scripted outcome; any other path is the real thing."""

    def __init__(self, outcome):
        self.outcome = outcome
        self.real = open

    def __call__(self, path, *args, **kwargs):
        if str(path).startswith("/proc/"):
            if isinstance(self.outcome, bytes):
                import io

                return io.BytesIO(self.outcome)
            raise self.outcome
        return self.real(path, *args, **kwargs)


@pytest.mark.parametrize(
    ("outcome", "ended"),
    [
        (FileNotFoundError(2, "gone"), True),
        (PermissionError(13, "denied"), False),
        (OSError(5, "input/output error"), False),
        (b"7 (x) Z 1 7", True),
        (b"7 (x) X 1 7", True),
        (b"7 (x) S 1 7", False),
        (b"", False),
    ],
    ids=["gone", "denied", "io-error", "zombie", "dead", "sleeping", "unreadable"],
)
def test_a_proc_state_that_cannot_be_read_does_not_confirm_an_exit(monkeypatch, outcome, ended):
    """codex-arch R38-02: any OSError on /proc/<pid>/stat was "the process is gone"."""
    import builtins

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(builtins, "open", _FileRead(outcome))

    assert guard._ended(7) is ended


@pytest.mark.parametrize(
    ("result", "ended"),
    [
        (SimpleNamespace(returncode=0, stdout="Z\n", stderr=""), True),
        (SimpleNamespace(returncode=0, stdout="S\n", stderr=""), False),
        (SimpleNamespace(returncode=1, stdout="", stderr=""), True),  # "no such process", and nothing else
        (SimpleNamespace(returncode=2, stdout="", stderr="usage"), False),
        (SimpleNamespace(returncode=1, stdout="", stderr="ps: not permitted"), False),
        (SimpleNamespace(returncode=0, stdout="", stderr=""), False),
        (OSError("cannot run ps"), False),
        (subprocess.TimeoutExpired("ps", 10), False),
    ],
    ids=["zombie", "running", "no-such-process", "ps-error", "ps-complains", "empty-success", "cannot-run", "timeout"],
)
def test_a_failing_ps_does_not_confirm_an_exit(monkeypatch, result, ended):
    monkeypatch.setattr(sys, "platform", "darwin")

    def run(*args, **kwargs):
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(subprocess, "run", run)

    assert guard._ended(7) is ended


def test_an_unconfirmed_exit_on_posix_is_not_ended(monkeypatch):
    """Below the `ended` predicate: the real observer cannot read the state, so the real terminate says "not ended"."""
    import builtins

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(builtins, "open", _FileRead(PermissionError(13, "denied")))

    assert guard._terminate_posix([7], lambda pid, sig: None, guard._ended, wait=0.05) == {7}


ADOPTED_TESTS = (
    WAIT_HELPERS
    + """
import subprocess
import sys

LISTENER = %r
LAUNCHER_SCRIPT = '''
import os, subprocess, sys, time
deadline = time.time() + 60
while not os.path.exists(sys.argv[1]) and time.time() < deadline:
    time.sleep(0.05)
subprocess.Popen([sys.executable, "-c", sys.argv[2], sys.argv[3], sys.argv[4]])
'''
LAUNCHER = []


def test_starts_a_launcher_that_waits():                        # NOT guarded: an earlier test's process
    script = os.environ["P1_LAUNCHER_FILE"]
    with open(script, "w", encoding="utf-8") as handle:
        handle.write(LAUNCHER_SCRIPT)
    LAUNCHER.append(subprocess.Popen(
        [sys.executable, script, os.environ["P1_GO"], LISTENER, os.environ["P1_LISTEN_PORT"],
         os.environ["P1_LISTENER_PID"]]
    ))


def test_guarded_a():
    open(os.environ["P1_GO"], "w").close()                       # now the earlier test's launcher starts a listener...
    assert wait_for(os.environ["P1_LISTENER_PID"])
    LAUNCHER[0].wait(timeout=60)                                 # ...and exits: the worker adopts the listener
"""
    % (LISTENER_SOURCE,)
)


@pytest.mark.skipif(sys.platform == "win32", reason="adoption by a subreaper is a POSIX matter")
@pytest.mark.parametrize("evidence", [False, True], ids=["ordinary", "evidence"])
def test_an_earlier_tests_launcher_adopted_by_the_worker_does_not_make_its_new_listener_this_tests(
    project, monkeypatch, evidence
):
    """codex-arch R38-03: a launcher that an EARLIER test started is alive when the window opens, starts a listener
    during it and exits; the worker (a subreaper) adopts the listener and the edge to the launcher is gone. The listener
    is new, its chain reaches the worker: that used to make it this test's, and it was killed. It is UNDECIDED: named in
    an ordinary run, refused in an evidence run, alive in both."""
    port, pid_file = _listener_env(project, monkeypatch)
    monkeypatch.setenv("P1_GO", str(project.log / "go-launcher.txt"))
    monkeypatch.setenv("P1_LAUNCHER_FILE", str(project.log / "slow_launcher.py"))
    project.makepyfile(test_adopted=ADOPTED_TESTS)
    try:
        if evidence:
            result, journals = run_recorded(project, monkeypatch)
        else:
            result, journals = run_inner(project, monkeypatch), None
        alive = _port_is_listened_on(port)

        output = result.stdout.str()
        lines = _guard_lines(output)
        assert alive, "a listener that is not provably this test's must never be ended"
        if evidence:
            assert result.ret == 1, output
            assert any(" left: " in line and "could not tell whose" in line for line in lines), lines
            assert journals["serial"] == [([1], [1])], journals
        else:
            result.assert_outcomes(passed=2)
            assert result.ret == 0, output
            assert not any(" left: " in line for line in lines), lines
    finally:
        _stop_listener(pid_file)


# -- round 15: a busy port whose owner could not be found is undecided, never another process's --------------------


@pytest.mark.parametrize("injection", ["no-owner-found", "fd-unreadable"])
@pytest.mark.parametrize("evidence", [False, True], ids=["ordinary", "evidence"])
def test_a_busy_port_whose_owner_cannot_be_found_is_undecided_never_another_process(
    project, monkeypatch, evidence, injection
):
    """codex-arch R39-01: on Linux an unreadable `/proc/<pid>/fd` became an empty owner set, which `_judge` then called
    "held by another process" for a port the bind probe had just shown to be busy. A real outside listener holds the
    port; the lookup finds no owner (injected at the lookup, or, on Linux, at the descriptor read): UNDECIDED. An
    ordinary run names it; an evidence run refuses it (exit 1, journal 1); neither ends it."""
    if injection == "fd-unreadable" and not sys.platform.startswith("linux"):
        pytest.skip("descriptors are read through /proc, on Linux")
    port = _free_port()
    go, bound, stop = (project.log / name for name in ("go.txt", "outside-bound.txt", "stop.txt"))
    monkeypatch.setenv("P1_ADDRESSES", json.dumps([["127.0.0.1", port]]))
    monkeypatch.setenv("P1_REAL_PROBE", "1")
    monkeypatch.setenv("P1_OWNERS_EMPTY" if injection == "no-owner-found" else "P1_FD_UNREADABLE", "1")
    monkeypatch.setenv("P1_GO", str(go))
    monkeypatch.setenv("P1_BOUND", str(bound))
    project.makepyfile(test_empty=UNDECIDED_TESTS)
    outside = subprocess.Popen(  # noqa: S603 - fixed argv, test-only, no shell
        [sys.executable, "-c", OUTSIDE_PROGRAM, str(port), str(go), str(bound), str(stop)]
    )
    try:
        if evidence:
            result, journals = run_recorded(project, monkeypatch)
        else:
            result, journals = run_inner(project, monkeypatch), None
        alive = _outside_alive(bound)
    finally:
        stop.write_text("stop", encoding="utf-8")
        outside.wait(timeout=60)

    output = result.stdout.str()
    lines = _guard_lines(output)
    assert alive, "a listener whose owner is not known must never be ended"
    if evidence:
        assert result.ret == 1, output
        assert any(" left: " in line and "could not tell whose" in line for line in lines), lines
        assert journals["serial"] == [([1], [1])], journals
    else:
        result.assert_outcomes(passed=1)
        assert result.ret == 0, output
        assert any("could not tell whose listener" in line for line in lines), lines
        assert not any(" left: " in line for line in lines), lines


def test_an_empty_owner_set_is_undecided_for_a_busy_port_and_ignored_for_a_free_one():
    plugin = guard.GuardPlugin(
        frozenset(),
        {},
        [("127.0.0.1", 1)],
        lambda host, port: True,
        lambda seconds: None,
        owners=lambda host, port: set(),
    )
    evidence = guard.Evidence("none")

    busy = plugin._judge(("127.0.0.1", 1), {os.getpid()}, evidence)
    free = plugin._judge(("127.0.0.1", 1), {os.getpid()}, evidence, free=True)

    assert busy[0] == guard.UNDECIDED and busy[1] == set()
    assert free[0] == guard.OTHER and free[2] == ""  # nothing is known and nothing is made busy: not even a note


def test_an_unreadable_ipv6_table_is_not_an_empty_one(tmp_path, monkeypatch):
    """The same pattern in the table read: a /proc/net/tcp6 that cannot be read must not read as "no IPv6 listeners"
    (a dual-stack listener shows up there); a kernel without IPv6 (no such file) is fine."""
    (tmp_path / "net").mkdir()
    (tmp_path / "net" / "tcp").write_text(PROC_TCP_ENDPOINTS, encoding="utf-8")
    tcp6 = tmp_path / "net" / "tcp6"
    real_open = open

    def refuse(path, *args, **kwargs):
        if Path(path) == tcp6:
            raise PermissionError(13, "denied")
        return real_open(path, *args, **kwargs)

    import builtins

    assert guard._owners_linux("127.0.0.1", 4000, proc=str(tmp_path)) == set()  # no tcp6 file at all: no IPv6 here
    tcp6.write_text(PROC_TCP6_ENDPOINTS, encoding="utf-8")
    monkeypatch.setattr(builtins, "open", refuse)

    assert guard._owners_linux("127.0.0.1", 4000, proc=str(tmp_path)) is None


# -- round 16: a listener the pytest process already held before the test is not that test's leftover -----------------

IN_PROCESS_TESTS = (
    WAIT_HELPERS
    + """
import socket

KEPT = []


def _listen():
    sock = socket.socket()
    sock.bind(("127.0.0.1", int(os.environ["P1_LISTEN_PORT"])))
    sock.listen(1)
    return sock


def test_guarded_a():
    pass                                                         # a guarded warm-up


def test_opens_a_listener():                                     # NOT guarded: kept for a later test of the run
    KEPT.append(_listen())


def test_guarded_b():
    mode = os.environ["P1_MODE"]
    if mode == "replace":                                        # closes the earlier listener, opens a new one
        KEPT.pop().close()
        KEPT.append(_listen())


def test_closes_it():                                            # NOT guarded
    while KEPT:
        KEPT.pop().close()
"""
)


def _run_in_process(project, monkeypatch, *, evidence, workers, mode):
    port = _free_port()
    monkeypatch.setenv("P1_LISTEN_PORT", str(port))
    monkeypatch.setenv("P1_ADDRESSES", json.dumps([["127.0.0.1", port]]))
    monkeypatch.setenv("P1_REAL_PROBE", "1")
    monkeypatch.setenv("P1_MODE", mode)
    project.makepyfile(test_inproc=IN_PROCESS_TESTS)
    # one file under `loadfile`: every test runs in the same worker, so the listener is in the guarded test's process
    if evidence:
        result, journals = run_recorded(project, monkeypatch, workers=workers, dist="loadfile")
        return result, journals, port
    return run_inner(project, monkeypatch, workers=workers, dist="loadfile"), None, port


@pytest.mark.parametrize("evidence", [False, True], ids=["ordinary", "evidence"])
@pytest.mark.parametrize("workers", [0, 2], ids=["serial", "parallel"])
def test_a_listener_the_pytest_process_held_before_the_test_is_not_that_tests_leftover(
    project, monkeypatch, workers, evidence
):
    """Zoolok's Codex reader (9735040a): an UNGUARDED test keeps a loopback listener open inside the pytest process; the
    next, clean guarded test did nothing with it and was failed for leaving the port open (master passes). The listener
    existed at the test's start and is still the same socket afterwards: not the test's."""
    result, journals, _port = _run_in_process(project, monkeypatch, evidence=evidence, workers=workers, mode="clean")

    output = result.stdout.str()
    result.assert_outcomes(passed=4)
    assert result.ret == 0, output
    assert not any(" left: " in line for line in _guard_lines(output)), output
    if evidence:
        assert journals["controller" if workers else "serial"] == [([0], [0])], journals


@pytest.mark.parametrize("evidence", [False, True], ids=["ordinary", "evidence"])
def test_a_new_listener_on_the_same_port_after_the_old_one_was_closed_is_still_the_tests_leftover(
    project, monkeypatch, evidence
):
    """The earlier listener is closed by the guarded test and a NEW one is opened on the same port and kept: a different
    socket object, born during this test. The design tells them apart (it keeps the objects, not the port)."""
    result, journals, port = _run_in_process(project, monkeypatch, evidence=evidence, workers=0, mode="replace")

    output = result.stdout.str()
    assert result.ret == 1, output
    left = [line for line in _guard_lines(output) if " left: " in line]
    assert len(left) == 1 and "test_inproc.py::test_guarded_b" in left[0] and f"127.0.0.1:{port}" in left[0], left
    if evidence:
        assert journals["serial"] == [([1], [1])], journals


@pytest.mark.parametrize("evidence", [False, True], ids=["ordinary", "evidence"])
def test_a_failed_start_inventory_of_the_processs_own_sockets_is_undecided(project, monkeypatch, evidence):
    """If the sockets the process already held could not be listed when the test began, a listener in this process
    cannot be told from the test's own: undecided, never the test's and never another process's (named in an ordinary
    run, refused in an evidence run)."""
    monkeypatch.setenv("P1_INVENTORY_FAILS", "1")
    result, journals, _port = _run_in_process(project, monkeypatch, evidence=evidence, workers=0, mode="replace")

    output = result.stdout.str()
    lines = _guard_lines(output)
    if evidence:
        assert result.ret == 1, output
        assert any(" left: " in line and "could not tell whose" in line for line in lines), lines
        assert journals["serial"] == [([1], [1])], journals
    else:
        result.assert_outcomes(passed=4)
        assert result.ret == 0, output
        assert any("could not tell whose listener" in line for line in lines), lines
        assert not any(" left: " in line for line in lines), lines


def test_the_starting_inventory_tells_a_socket_object_apart_from_a_new_one_on_the_same_port():
    with socket.socket() as old:
        old.bind(("127.0.0.1", 0))
        old.listen(1)
        port = old.getsockname()[1]
        before = guard._listening_sockets()
        assert guard._own_verdict("127.0.0.1", port, before) == guard.OTHER  # the same object, still listening
        old.close()
        with socket.socket() as new:
            new.bind(("127.0.0.1", port))
            new.listen(1)
            assert guard._own_verdict("127.0.0.1", port, before) == guard.MINE  # a different object on the port
            assert guard._own_verdict("127.0.0.1", port, None) == guard.UNDECIDED  # no inventory: no answer
    assert guard._own_verdict("127.0.0.1", port, before) == guard.UNDECIDED  # the system may name us, we see nothing


# -- round 17: a partial inventory is not proof; an older listener must not hide a new one -----------------------------

BESIDE_TESTS = (
    WAIT_HELPERS
    + """
import socket

KEPT = []
RAW = []


def _listen():
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_REUSEPORT", socket.SO_REUSEADDR), 1)  # two may share it
    sock.bind(("127.0.0.1", int(os.environ["P1_LISTEN_PORT"])))
    sock.listen(1)
    return sock


def _note(sock):                                                 # the descriptor an injected read failure is about
    with open(os.environ["P1_FAIL_FILE"], "w", encoding="utf-8") as handle:
        handle.write(str(sock.fileno()))


def test_guarded_a():
    pass                                                         # a guarded warm-up


def test_opens_a_listener():                                     # NOT guarded: kept for a later test of the run
    old = _listen()
    KEPT.append(old)
    if os.environ["P1_MODE"] == "start":
        _note(old)                                               # the start inventory cannot read this one
    if os.environ["P1_MODE"] in ("alias", "alias-alone"):
        KEPT.append(old.dup())                                   # a second OBJECT for the same kernel socket


def test_guarded_b():
    mode = os.environ["P1_MODE"]
    if mode in ("start", "alias-alone"):
        return                                                   # a clean test: it touches nothing
    new = _listen()                                              # a NEW listener beside the old one
    if mode in ("raw", "alias"):
        RAW.append(new.detach())                                 # ... that is no socket object any more
        return
    KEPT.append(new)
    if mode == "end":
        _note(new)                                               # the end scan cannot read this one


def test_closes_it():                                            # NOT guarded
    while KEPT:
        KEPT.pop().close()
    while RAW:
        socket.socket(fileno=RAW.pop()).close()
"""
)


def _run_beside(project, monkeypatch, *, evidence, workers, mode):
    port = _free_port()
    monkeypatch.setenv("P1_LISTEN_PORT", str(port))
    monkeypatch.setenv("P1_ADDRESSES", json.dumps([["127.0.0.1", port]]))
    monkeypatch.setenv("P1_REAL_PROBE", "1")
    monkeypatch.setenv("P1_MODE", mode)
    monkeypatch.setenv("P1_FAIL_FILE", str(project.log / "failing-descriptor"))
    project.makepyfile(test_beside=BESIDE_TESTS)
    if evidence:
        result, journals = run_recorded(project, monkeypatch, workers=workers, dist="loadfile")
        return result, journals, port
    return run_inner(project, monkeypatch, workers=workers, dist="loadfile"), None, port


def _assert_undecided(result, journals, workers, evidence, passed=4):
    """Named in an ordinary run (exit 0, nothing blamed); refused in an evidence run (exit 1, journal 1)."""
    output = result.stdout.str()
    lines = _guard_lines(output)
    if evidence:
        assert result.ret == 1, output
        assert any(" left: " in line and "could not tell whose" in line for line in lines), lines
        assert journals["controller" if workers else "serial"] == [([1], [1])], journals
    else:
        result.assert_outcomes(passed=passed)
        assert result.ret == 0, output
        assert any("could not tell whose listener" in line for line in lines), lines
        assert not any(" left: " in line for line in lines), lines


@pytest.mark.parametrize("evidence", [False, True], ids=["ordinary", "evidence"])
@pytest.mark.parametrize("workers", [0, 2], ids=["serial", "parallel"])
@pytest.mark.parametrize("where", ["start", "end"])
def test_a_live_socket_that_cannot_be_read_is_undecided_never_a_complete_list(
    project, monkeypatch, where, workers, evidence
):
    """codex-arch R41-01. One live socket whose state cannot be read was dropped from the list, and what was left was
    taken as everything. At the START: the older listener is missing from the list, so a clean test was blamed for it.
    At the END: the new listener is missing beside an older one, so the older one alone read as "not the test's" and an
    evidence run recorded a false 0. A failed read is "not known": undecided, never the test's and never another's."""
    monkeypatch.setenv("P1_READ_FAILS", where)
    result, journals, _port = _run_beside(project, monkeypatch, evidence=evidence, workers=workers, mode=where)

    _assert_undecided(result, journals, workers, evidence)


@pytest.mark.parametrize("evidence", [False, True], ids=["ordinary", "evidence"])
@pytest.mark.parametrize("workers", [0, 2], ids=["serial", "parallel"])
def test_a_new_listener_detached_to_a_raw_descriptor_beside_an_older_one_is_undecided(
    project, monkeypatch, workers, evidence
):
    """codex-arch R41-02. An older listener (a socket object, visible) and a NEW listener on the same endpoint whose
    socket object is gone (the descriptor was detached): the visible objects all existed before, but the system counts
    one listening socket more than they account for. Undecided, never "another's"."""
    result, journals, _port = _run_beside(project, monkeypatch, evidence=evidence, workers=workers, mode="raw")

    _assert_undecided(result, journals, workers, evidence)


@pytest.mark.parametrize("evidence", [False, True], ids=["ordinary", "evidence"])
def test_a_new_visible_listener_beside_an_older_one_is_still_the_tests_leftover(project, monkeypatch, evidence):
    """The control: the new listener is a socket object the guard sees, so it is the test's (exit 1, journal 1)."""
    result, journals, port = _run_beside(project, monkeypatch, evidence=evidence, workers=0, mode="beside")

    output = result.stdout.str()
    assert result.ret == 1, output
    left = [line for line in _guard_lines(output) if " left: " in line]
    assert len(left) == 1 and "test_beside.py::test_guarded_b" in left[0] and f"127.0.0.1:{port}" in left[0], left
    if evidence:
        assert journals["serial"] == [([1], [1])], journals


def _listening(port=0):
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_REUSEPORT", socket.SO_REUSEADDR), 1)
    sock.bind(("127.0.0.1", port))
    sock.listen(1)
    return sock


def test_a_live_socket_that_cannot_be_read_makes_the_scan_and_the_inventory_unknown(monkeypatch):
    with _listening() as sock:
        port = sock.getsockname()[1]
        before = guard._listening_sockets()
        real = guard._listens

        def unreadable(candidate):
            if candidate is sock:
                raise OSError("this live socket cannot be read")
            return real(candidate)

        monkeypatch.setattr(guard, "_listens", unreadable)

        assert guard._own_listening_sockets("127.0.0.1", port) is None
        assert guard._listening_sockets() is None
        assert guard._own_verdict("127.0.0.1", port, before) == guard.UNDECIDED


def test_an_object_that_is_positively_not_a_listener_is_not_a_failed_read(monkeypatch):
    """A closed or detached socket, a datagram socket and (POSIX) a Unix socket are "no", not "not known": the scan and
    the inventory still complete, even though reading any of them with `_listens` would raise."""
    closed = socket.socket()
    closed.close()
    detached = socket.socket()
    detached.detach()
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    extra = [closed, detached, udp]
    if hasattr(socket, "AF_UNIX"):
        extra.append(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
    real = guard._listens

    def refuses_these(candidate):
        if any(candidate is one for one in extra):
            raise OSError("not something to read")
        return real(candidate)

    monkeypatch.setattr(guard, "_listens", refuses_these)
    try:
        assert guard._listening_sockets() is not None
        assert guard._own_listening_sockets("127.0.0.1", _free_port()) == []
    finally:
        for one in extra:
            one.close()


@pytest.mark.parametrize(
    ("claimed", "expected"),
    [(1, "OTHER"), (0, "OTHER"), (2, "UNDECIDED"), (None, "UNDECIDED")],
    ids=["one", "fewer", "more", "unreadable"],
)
def test_an_older_listener_is_another_s_only_if_the_system_counts_no_more_than_the_visible_objects(
    monkeypatch, claimed, expected
):
    with _listening() as old:
        port = old.getsockname()[1]
        before = guard._listening_sockets()
        monkeypatch.setattr(guard, "_own_claimed_count", lambda host, number: claimed)

        assert guard._own_verdict("127.0.0.1", port, before) == getattr(guard, expected)


def test_the_system_count_sees_a_listener_that_is_no_longer_a_socket_object():
    with _listening() as old:
        port = old.getsockname()[1]
        assert guard._own_claimed_count("127.0.0.1", port) == 1
        second = _listening(port)
        raw = second.detach()
        try:
            assert guard._own_claimed_count("127.0.0.1", port) == 2  # the system still counts the detached one
            assert len(guard._own_listening_sockets("127.0.0.1", port)) == 1  # the scan sees only the object
            assert guard._own_verdict("127.0.0.1", port, guard._listening_sockets()) == guard.UNDECIDED
        finally:
            socket.socket(fileno=raw).close()
    assert guard._own_claimed_count("127.0.0.1", port) == 0


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="a Linux descriptor table")
def test_a_copy_of_a_descriptor_is_the_same_listening_socket_for_the_system_count():
    with _listening() as old:
        port = old.getsockname()[1]
        copy = os.dup(old.fileno())
        try:
            assert guard._own_claimed_count("127.0.0.1", port) == 1
            assert guard._own_verdict("127.0.0.1", port, guard._listening_sockets()) == guard.OTHER
        finally:
            os.close(copy)


@pytest.mark.skipif(os.name != "posix", reason="dup")
def test_a_dup_copy_of_an_older_listener_in_a_new_socket_object_is_the_tests_policy():
    """Recorded policy (not a finding): the rule follows socket OBJECTS. A copy of an older listener wrapped in a new
    socket object counts as a new listener, so it is blamed on the test that made it."""
    with _listening() as old:
        port = old.getsockname()[1]
        before = guard._listening_sockets()
        with socket.socket(fileno=os.dup(old.fileno())):
            assert guard._own_verdict("127.0.0.1", port, before) == guard.MINE


@pytest.mark.skipif(os.name != "posix" or sys.byteorder != "little", reason="a fake /proc with links")
def test_the_linux_count_counts_sockets_not_descriptors_and_reads_nothing_it_cannot(tmp_path):
    net = tmp_path / "net"
    net.mkdir()
    row = "   {n}: 0100007F:0FA0 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000 0 {inode} 1 0 100 0"
    header = "  sl  local_address rem_address   st\n"
    (net / "tcp").write_text(header + row.format(n=0, inode=111) + "\n" + row.format(n=1, inode=222) + "\n", "utf-8")
    (net / "tcp6").write_text("  sl  local_address rem_address   st\n", encoding="utf-8")
    fd_dir = tmp_path / str(os.getpid()) / "fd"
    fd_dir.mkdir(parents=True)
    for name, target in (("3", "socket:[111]"), ("4", "socket:[111]"), ("5", "socket:[222]"), ("6", "pipe:[9]")):
        (fd_dir / name).symlink_to(target)

    assert guard._own_claimed_linux("127.0.0.1", 4000, proc=str(tmp_path)) == 2  # two sockets, three descriptors
    assert guard._own_claimed_linux("127.0.0.1", 4001, proc=str(tmp_path)) == 0
    (net / "tcp").unlink()
    assert guard._own_claimed_linux("127.0.0.1", 4000, proc=str(tmp_path)) is None  # no table is not an empty table
    (net / "tcp").write_text("  sl  local_address rem_address   st\n", encoding="utf-8")
    (fd_dir / "6").unlink()
    fd_dir.rename(fd_dir.with_name("gone"))
    assert guard._own_claimed_linux("127.0.0.1", 4000, proc=str(tmp_path)) is None  # own descriptors unreadable


def test_the_lsof_and_windows_rows_count_one_row_per_listening_socket(monkeypatch):
    text = "p10\nf3\ntIPv4\nn127.0.0.1:4000\nf4\ntIPv4\nn*:4000\nf5\ntIPv4\nn127.0.0.1:4001\n"
    text += "p11\nf3\ntIPv4\nn127.0.0.1:4000\n"
    assert guard._lsof_rows(text, "127.0.0.1", 4000) == [10, 10, 11]
    assert guard._lsof_owners(text, "127.0.0.1", 4000) == {10, 11}
    me = os.getpid()
    monkeypatch.setattr(sys, "platform", "darwin")  # the macOS lookup, forced
    monkeypatch.setattr(guard, "_lsof_listener_rows", lambda host, port: [me, me, me + 1])
    assert guard._own_claimed_count("127.0.0.1", 4000) == 2
    monkeypatch.setattr(guard, "_lsof_listener_rows", lambda host, port: None)
    assert guard._own_claimed_count("127.0.0.1", 4000) is None
    monkeypatch.setattr(sys, "platform", "win32")  # the Windows lookup, forced
    monkeypatch.setattr(guard, "_windows_rows", lambda host, port: [me, me + 1, me])
    assert guard._own_claimed_count("127.0.0.1", 4000) == 2
    assert guard._own_claimed_count("::1", 4000) == 0  # only the IPv4 table is read: nothing is claimed for IPv6
    monkeypatch.setattr(guard, "_windows_rows", lambda host, port: None)
    assert guard._own_claimed_count("127.0.0.1", 4000) is None


# -- round 18: count like with like; a failing lsof is not a zero count ------------------------------------------------


@pytest.mark.parametrize("evidence", [False, True], ids=["ordinary", "evidence"])
@pytest.mark.parametrize("workers", [0, 2], ids=["serial", "parallel"])
def test_two_objects_for_one_older_socket_do_not_hide_a_new_detached_listener(project, monkeypatch, workers, evidence):
    """codex-arch R42-01. An earlier test keeps A and A.dup(): two objects, ONE kernel socket. The guarded test leaves a
    separate new listener B on the same endpoint with no object (detached). The system counts 2 sockets and the scan
    sees 2 objects, so comparing the two numbers cleared B. The unit is the kernel socket: undecided, never
    another's."""
    result, journals, _port = _run_beside(project, monkeypatch, evidence=evidence, workers=workers, mode="alias")

    _assert_undecided(result, journals, workers, evidence)


@pytest.mark.parametrize("evidence", [False, True], ids=["ordinary", "evidence"])
def test_two_objects_for_one_older_socket_alone_are_cleared_only_where_they_can_be_told_apart(
    project, monkeypatch, evidence
):
    """The control: A and A.dup() from an earlier test, nothing new. Linux reads the socket each object stands for (one
    inode), so it is cleared: another's, nothing reported. Windows and macOS cannot tell two objects for one socket from
    two sockets, so there it is undecided (ordinary exit 0 with the name; evidence exit 1): conservative, and the price
    of the rule that keeps a new detached listener from hiding behind a copy."""
    result, journals, _port = _run_beside(project, monkeypatch, evidence=evidence, workers=0, mode="alias-alone")

    if sys.platform.startswith("linux"):
        output = result.stdout.str()
        lines = _guard_lines(output)
        result.assert_outcomes(passed=4)
        assert result.ret == 0, output
        assert not any(" left: " in line or "could not tell" in line for line in lines), lines
        assert any("a socket this process already held before the test began" in line for line in lines), lines
        if evidence:
            assert journals["serial"] == [([0], [0])], journals
    else:
        _assert_undecided(result, journals, 0, evidence)


def test_distinct_kernel_sockets_are_counted_where_they_can_be_told_apart(monkeypatch):
    with _listening() as first:
        port = first.getsockname()[1]
        copy = first.dup()
        second = _listening(port)
        try:
            assert guard._distinct_visible([first]) == 1
            if sys.platform.startswith("linux"):
                assert guard._distinct_visible([first, copy]) == 1  # one inode behind two objects
                assert guard._distinct_visible([first, copy, second]) == 2
            else:
                assert guard._distinct_visible([first, copy]) is None  # not told apart: not known
            for platform in ("win32", "darwin"):  # forced: the platforms that cannot tell
                monkeypatch.setattr(sys, "platform", platform)
                assert guard._distinct_visible([first]) == 1
                assert guard._distinct_visible([first, copy]) is None
                assert guard._distinct_visible([]) == 0
            monkeypatch.undo()
            if sys.platform.startswith("linux"):
                copy.close()
                assert guard._distinct_visible([first, copy]) is None  # a closed object has no socket to read
        finally:
            copy.close()
            second.close()


@pytest.mark.parametrize(
    ("claimed", "expected"), [(1, "OTHER"), (2, "UNDECIDED")], ids=["covered", "one-more-than-the-sockets-seen"]
)
def test_a_copy_of_an_older_listener_counts_as_one_socket_where_that_is_known(monkeypatch, claimed, expected):
    with _listening() as old:
        port = old.getsockname()[1]
        with old.dup():  # made BEFORE the inventory, as an earlier test would have
            before = guard._listening_sockets()
            monkeypatch.setattr(guard, "_own_claimed_count", lambda host, number: claimed)
            answer = guard._own_verdict("127.0.0.1", port, before)
            if sys.platform.startswith("linux"):
                assert answer == getattr(guard, expected)  # two objects, one socket: the count of 2 is one too many
            else:
                assert answer == guard.UNDECIDED  # the two objects cannot be told from two sockets


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr", "finished"),
    [
        (0, "p10\nf3\ntIPv4\nn127.0.0.1:4000\n", "", True),
        (0, "", "", True),
        (1, "", "", True),  # silent: lsof's "nothing located"
        (1, "", "lsof: WARNING: can't read the process table\n", False),
        (1, "p10\nf3\ntIPv4\nn127.0.0.1:4000\n", "lsof: WARNING: can't stat something\n", False),
        (1, "p10\nf3\n", "", False),  # something located yet "an error": not a clean answer
        (2, "", "", False),
        (127, "", "", False),
    ],
    ids=[
        "found",
        "empty-ok",
        "nothing-found",
        "error-message",
        "partial-with-error",
        "output-with-1",
        "status-2",
        "other",
    ],
)
def test_an_lsof_run_is_a_finished_answer_only_if_it_is_clean(returncode, stdout, stderr, finished):
    """codex-arch R42-02. lsof's manual: status 1 means an error was detected, and a search that located nothing counts
    as one. So status 1 is an empty answer only when nothing at all was said; with a diagnostic it is a failed read."""
    assert guard._lsof_completed(returncode, stdout, stderr) is finished


@pytest.mark.parametrize("platform", ["darwin"])
def test_a_failing_lsof_is_an_unreadable_count_and_a_clean_empty_one_is_zero(monkeypatch, platform):
    me = os.getpid()
    answer = {}

    def fake_run(argv, **kwargs):
        return SimpleNamespace(returncode=answer["code"], stdout=answer["out"], stderr=answer["err"])

    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(guard.shutil, "which", lambda name: "/usr/sbin/lsof")
    monkeypatch.setattr(guard.subprocess, "run", fake_run)

    answer.update(code=1, out="", err="lsof: WARNING: can't read the process table\n")
    assert guard._lsof_listener_rows("127.0.0.1", 4000) is None
    assert guard._own_claimed_count("127.0.0.1", 4000) is None  # unreadable: undecided, never zero
    assert guard._owners_lsof("127.0.0.1", 4000) is None  # the owner lookup reads it the same way

    answer.update(code=1, out="", err="")  # the clean "nothing found"
    assert guard._lsof_listener_rows("127.0.0.1", 4000) == []
    assert guard._own_claimed_count("127.0.0.1", 4000) == 0
    assert guard._owners_lsof("127.0.0.1", 4000) == set()

    answer.update(code=0, out=f"p{me}\nf3\ntIPv4\nn127.0.0.1:4000\n", err="")
    assert guard._own_claimed_count("127.0.0.1", 4000) == 1


# -- round 19: a system claim for this process that the scan cannot confirm stays undecided ------------------------

WILDCARD_TESTS = (
    WAIT_HELPERS
    + """
import socket

import gateway_port_guard as guard

KEPT = []


def test_guarded_a():
    pass                                                         # a guarded warm-up


def test_guarded_b():
    wide = socket.socket()
    wide.bind((guard.ANY_ADDRESS, int(os.environ["P1_LISTEN_PORT"])))   # an ordinary wildcard listener, kept open
    wide.listen(1)
    other = socket.socket()                                      # and a second ordinary socket, which cannot be read
    KEPT.extend([wide, other])
    with open(os.environ["P1_FAIL_FILE"], "w", encoding="utf-8") as handle:
        handle.write(str(other.fileno()))
"""
)


@pytest.mark.parametrize("evidence", [False, True], ids=["ordinary", "evidence"])
@pytest.mark.parametrize("workers", [0, 2], ids=["serial", "parallel"])
def test_a_wildcard_listener_is_undecided_when_another_sockets_read_fails(project, monkeypatch, workers, evidence):
    """The maintainers' final read of #667 (P2). The Windows bind probe calls a wildcard listener on the same port free,
    and the table's claim for this process was dropped when the scan failed, so nothing was left to decide: OTHER, and
    an evidence run recorded 0 with the socket still open. The claim is kept to the final decision: undecided, named
    in an ordinary run and refused in an evidence run. Ordinary sockets only."""
    port = _free_port()
    monkeypatch.setenv("P1_LISTEN_PORT", str(port))
    monkeypatch.setenv("P1_ADDRESSES", json.dumps([["127.0.0.1", port]]))
    monkeypatch.setenv("P1_REAL_PROBE", "1")
    monkeypatch.setenv("P1_FAIL_FILE", str(project.log / "failing-descriptor"))
    monkeypatch.setenv("P1_READ_FAILS", "end")
    project.makepyfile(test_wide=WILDCARD_TESTS)
    if evidence:
        result, journals = run_recorded(project, monkeypatch, workers=workers)
    else:
        result, journals = run_inner(project, monkeypatch, workers=workers), None

    _assert_undecided(result, journals, workers, evidence, passed=2)


def test_a_windows_table_claim_for_this_process_is_kept_beside_an_outsider(monkeypatch):
    """GitHub's reviewer on #667, line 765: the table names this process and another one, and this process's listener
    cannot be confirmed (the scan failed, or sees nothing). The claim is not subtracted, so the outsider does not decide
    the case alone."""
    me = os.getpid()
    monkeypatch.setattr(sys, "platform", "win32")  # the Windows lookup, forced
    monkeypatch.setattr(guard, "_owners_windows", lambda host, port: {me, 424242})
    for scan in (None, []):  # the scan failed / the scan sees no Python socket (a raw descriptor)
        monkeypatch.setattr(guard, "_own_listening_sockets", lambda host, port, scan=scan: scan)
        assert guard.listener_owners("127.0.0.1", 4000) == {me, 424242}
    monkeypatch.setattr(guard, "_owners_windows", lambda host, port: {me})
    assert guard.listener_owners("127.0.0.1", 4000) == {me}  # nor does it vanish when it is the only claim


@pytest.mark.parametrize("free", [False, True], ids=["busy", "free-by-the-probe"])
def test_an_unconfirmed_claim_for_this_process_beside_a_provable_outsider_is_undecided(monkeypatch, free):
    me = os.getpid()
    monkeypatch.setattr(guard, "_own_listening_sockets", lambda host, port: None)  # the scan cannot be completed
    monkeypatch.setattr(guard, "_classify", lambda pid, evidence: guard.OTHER)  # the outsider is provably another's
    plugin = guard.GuardPlugin(
        frozenset(),
        {},
        [("127.0.0.1", 4000)],
        lambda host, port: True,
        lambda seconds: None,
        owners=lambda host, port: {me, 424242},
    )

    answer, pids, who = plugin._judge(("127.0.0.1", 4000), set(), guard.Evidence("none", sockets_before=[]), free=free)

    assert answer == guard.UNDECIDED and pids == set() and str(me) in who


def test_a_confirmed_claim_for_this_process_beside_a_provable_outsider_is_still_another_s(monkeypatch):
    """The control: when the scan confirms the older listener (the same object, the count covered), it is OTHER."""
    me = os.getpid()
    monkeypatch.setattr(guard, "_own_verdict", lambda host, port, before: guard.OTHER)
    monkeypatch.setattr(guard, "_classify", lambda pid, evidence: guard.OTHER)
    plugin = guard.GuardPlugin(
        frozenset(),
        {},
        [("127.0.0.1", 4000)],
        lambda host, port: True,
        lambda seconds: None,
        owners=lambda host, port: {me, 424242},
    )

    answer, _pids, _who = plugin._judge(("127.0.0.1", 4000), set(), guard.Evidence("none", sockets_before=[]))

    assert answer == guard.OTHER


# -- round 20: the controller's probe is valid only for local workers ---------------------------------------------


class _XdistConfig:
    """Just enough of a pytest config for `configure` and `apply`: the options xdist sets, and a plugin manager."""

    def __init__(self, tx=None, numprocesses=None, strict=False) -> None:
        self.option = SimpleNamespace(numprocesses=numprocesses, tx=tx)
        self.pluginmanager = pytest.PytestPluginManager()
        self._strict = strict

    def getoption(self, name, default=None):
        return self._strict or default


def _configure(config, **kwargs):
    return guard.configure(
        config,
        frozenset({"test_guarded_a"}),
        environ={guard.OPT_IN_VAR: "1", **kwargs.pop("environ", {})},
        addresses=[("127.0.0.1", 1)],
        probe=lambda host, port: False,
        sleep=lambda seconds: None,
    )


@pytest.mark.parametrize(
    ("specs", "remote"),
    [
        (None, []),
        ([], []),
        (["popen"], []),
        (["2*popen"], []),
        (["popen", "popen"], []),
        (["popen//python=python3"], []),
        (["4*popen//chdir=work//id=gw"], []),
        (["socket=localhost:8888"], ["socket=localhost:8888"]),
        (["ssh=build-host"], ["ssh=build-host"]),
        (["popen", "ssh=build-host"], ["ssh=build-host"]),
        (["vagrant_ssh=default"], ["vagrant_ssh=default"]),
        (["something-unknown"], ["something-unknown"]),
        ([""], [""]),
        (["popenish"], ["popenish"]),
        (["2*socket=localhost:8888"], ["2*socket=localhost:8888"]),
    ],
)
def test_only_local_popen_transports_count_as_local_workers(specs, remote):
    assert guard.remote_transports(_XdistConfig(tx=specs)) == remote


def test_a_transport_option_that_cannot_be_read_counts_as_remote():
    assert guard.remote_transports(_XdistConfig(tx=5)) == ["5"]


@pytest.mark.parametrize(
    ("tx", "numprocesses", "refused"),
    [
        (None, 2, False),  # -n 2
        (["2*popen"], None, False),  # --tx 2*popen
        (["popen//python=python3"], None, False),
        (None, None, False),  # no xdist at all
        (["socket=localhost:8888"], None, True),
        (["ssh=build-host"], None, True),
        (["popen", "ssh=build-host"], None, True),
        (["something-unknown"], None, True),
    ],
    ids=["n2", "tx-2-popen", "tx-popen-python", "no-xdist", "socket", "ssh", "mixed", "unknown"],
)
def test_the_controllers_answer_is_refused_for_remote_transports_and_unchanged_otherwise(tx, numprocesses, refused):
    """GitHub's reviewer on #667 (P1): with a remote `--tx` the controller probed its own 127.0.0.1 and told workers on
    other hosts the port was free, and its opt-in bypassed theirs. The guarded tests are refused, naming the transport;
    local workers and no xdist behave as before."""
    plugin = _configure(_XdistConfig(tx=tx, numprocesses=numprocesses))

    reason = None if plugin.decision is None else plugin.decision["reason"]
    if not refused:
        assert reason is None
        return
    assert reason is not None and reason.startswith("refused: xdist transport ")
    assert tx[-1] in reason and "not a local worker" in reason
    # the guarded tests of the run are skipped with that reason, and nothing else is changed
    item = SimpleNamespace(originalname="test_guarded_a", name="test_guarded_a", added=[])
    item.add_marker = item.added.append
    guard.apply(
        [item],
        frozenset({"test_guarded_a"}),
        {guard.OPT_IN_VAR: "1"},
        config=SimpleNamespace(pluginmanager=SimpleNamespace(get_plugin=lambda name: plugin)),
    )
    assert len(item.added) == 1 and "refused: xdist transport" in item.added[0].kwargs["reason"]


def test_a_remote_transport_leaves_the_other_reasons_alone():
    """Opted out: the opt-in message stays (nothing to refuse). Nested: the nested message stays."""
    off = _configure(_XdistConfig(tx=["ssh=build-host"]), environ={guard.OPT_IN_VAR: "0"})
    assert off.decision["reason"] is not None and "opt-in" in off.decision["reason"]
    nested = _configure(_XdistConfig(tx=["ssh=build-host"]), environ={"PYTEST_CURRENT_TEST": "x"})
    assert nested.decision["reason"] is not None and "nested" in nested.decision["reason"]


def test_a_remote_transport_stops_an_evidence_run():
    config = _XdistConfig(tx=["socket=localhost:8888"], strict=True)

    with pytest.raises(pytest.exit.Exception) as stopped:
        _configure(config)

    assert "socket=localhost:8888" in str(stopped.value) and "not a local worker" in str(stopped.value)
