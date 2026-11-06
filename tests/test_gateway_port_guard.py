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
import signal
import socket
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


def run_recorded(project, monkeypatch, *args, workers=0, conftest=None):
    project.makeconftest(conftest or RECORDER_CONFTEST)
    folder = project.log / "journals"
    result = run_inner(project, monkeypatch, f"--experiment-record={folder}", *args, workers=workers)
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
    [("other", "pid 1"), ("unknown", "could not be established")],
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
        "or by an owner that cannot be found out",
        "stopped before its body ran",
        "A test whose body never ran is not probed",
        "an xfail whose body did run keeps the full check",
        "An owner counts only for the exact endpoint the guard probed, not for the port number alone",
        "an IPv6 `::` listener owns it only when it is dual-stack",
        "so a real leftover listener can be missed when the system lookup fails too",
        "which can include a real leftover",
        'so "not held" is not proof that nothing listens',
        "the owner lookup is also asked about the guarded endpoints the probe called free",
        "never used the other way",
        "a launcher that spawns the provider and ends",
        "a real leftover can pass on those platforms",
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
            sock.bind(("::", 0))
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
    family = socket.AF_INET6 if kind.startswith("v6") else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        if family == socket.AF_INET6:
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
        assert f"127.0.0.1:{port}" in left[0] and "ended" in left[0], left
        assert journals["controller" if workers else "serial"] == [([1], [1])], journals
        assert not _port_is_listened_on(port), "the listener was left running"
    finally:
        _stop_listener(pid_file)
