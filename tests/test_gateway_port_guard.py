"""#318: the tests that touch the paid gateway's real, fixed ports are opt-in, and skip
while another process holds a port. These tests check the guard itself and never
touch the real ports: they use ephemeral ports they open themselves, a fake probe, or
a child pytest run without the opt-in, in which the guarded test is skipped before it
can run and no port is probed."""

from __future__ import annotations

import ast
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

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


def pytest_configure(config):
    guard.configure(config, NAMES, environ=os.environ, addresses=_addresses(), probe=counting_probe,
                    sleep=lambda seconds: None, teardown_probe=teardown_probe)


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


def run_inner(project, monkeypatch, *args, workers=0, dist="loadgroup"):
    # The outer test sets PYTEST_CURRENT_TEST; an inner run must not look nested unless a test says so.
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    extra = ["-p", "xdist.plugin", "-n", str(workers), "--dist", dist] if workers else []
    return project.runpytest_subprocess(*extra, *args, timeout=180)


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
