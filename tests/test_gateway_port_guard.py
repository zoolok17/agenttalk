"""#318: the tests that touch the paid gateway's real, fixed ports are opt-in, and skip
while another process holds a port. These tests check the guard itself and never
touch the real ports: they use ephemeral ports they open themselves, a fake probe, or
a child pytest run without the opt-in, in which the guarded test is skipped before it
can run and no port is probed."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import gateway_port_guard as guard
import pytest

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
