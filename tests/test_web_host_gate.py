"""#379: the dashboard answers only requests whose Host is a loopback name or address on its own port.

A web page can use DNS rebinding to make a browser send requests to 127.0.0.1 with a foreign Host header. The gate
refuses those before ANY route runs: pages, static assets, every /api route, the v2 console, options-only routes
and unknown paths.
"""
from __future__ import annotations

import http.client
import urllib.parse
from pathlib import Path

import pytest

from agenttalk import web
from agenttalk.store import Store


def _store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "bus")
    store.init(["alpha", "beta"])
    return store


@pytest.fixture
def dashboard(tmp_path: Path):
    """A real dashboard with actions and the budget feed on, so every optional route exists."""
    srv, _thread, base = web.serve_in_thread(_store(tmp_path), host="127.0.0.1", port=0, enable_actions=True,
                                             enable_budget=True)
    parsed = urllib.parse.urlsplit(base)
    try:
        yield parsed.hostname, parsed.port
    finally:
        srv.shutdown()
        srv.server_close()


def _request(address, method: str, path: str, *, host: str | None, headers: dict | None = None, body: bytes = b""):
    """One raw request with exactly the Host we choose (or none at all)."""
    conn = http.client.HTTPConnection(address[0], address[1], timeout=10)
    try:
        conn.putrequest(method, path, skip_host=True)
        if host is not None:
            conn.putheader("Host", host)
        for key, value in (headers or {}).items():
            conn.putheader(key, value)
        if body:
            conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body)
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def _static_asset() -> str:
    return "/static/" + next(iter(web._STATIC_ASSETS))


ROUTES = [
    "/", "/dashboard", "/v2", "/favicon.ico", "/api/status", "/api/budget", "/api/state", "/api/work-board",
    "/api/session", "/api/intents", "/api/preflight", "/api/attention", "/api/gates", "/api/risk-register",
    "/api/ownership", "/api/learning", "/api/onboarding", "/api/lead-chat", "/api/threads", "/api/messages",
    "/api/thread/x", "/api/messages/x", "/messages/x", "/no-such-page", "/static/no-such-asset.js",
]


@pytest.mark.parametrize("path", ROUTES)
def test_a_foreign_host_is_refused_on_every_route(dashboard, path):
    status, body = _request(dashboard, "GET", path, host=f"evil.example:{dashboard[1]}")
    assert status == 403, f"{path} answered a foreign Host with {status}"
    assert b"loopback" in body


def test_a_foreign_host_is_refused_on_a_real_static_asset(dashboard):
    status, _body = _request(dashboard, "GET", _static_asset(), host=f"evil.example:{dashboard[1]}")
    assert status == 403


def test_the_two_routes_named_in_the_issue_no_longer_answer_a_foreign_host(dashboard):
    for path in ("/api/status", "/api/budget"):
        status, _ = _request(dashboard, "GET", path, host=f"rebind.example:{dashboard[1]}")
        assert status != 200


@pytest.mark.parametrize("method", ["HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
def test_a_foreign_host_is_refused_for_every_method(dashboard, method):
    status, _ = _request(dashboard, method, "/api/status", host=f"evil.example:{dashboard[1]}")
    assert status == 403, f"{method} answered a foreign Host with {status}"


def test_a_foreign_host_on_an_action_is_refused_even_with_a_matching_origin(dashboard):
    host = f"evil.example:{dashboard[1]}"
    status, _ = _request(dashboard, "POST", "/api/intent", host=host,
                         headers={"Origin": f"http://{host}", "Content-Type": "application/json"}, body=b"{}")
    assert status == 403


@pytest.mark.parametrize("host", ["127.0.0.1:1", "localhost:1", "[::1]:1", "evil.example", "127.0.0.1", "localhost"])
def test_a_wrong_or_missing_port_is_refused(dashboard, host):
    status, _ = _request(dashboard, "GET", "/api/status", host=host)
    assert status == 403, f"Host {host!r} was accepted for a server on port {dashboard[1]}"


@pytest.mark.parametrize("host", ["", "   "])
def test_an_empty_host_is_refused(dashboard, host):
    status, _ = _request(dashboard, "GET", "/api/status", host=host)
    assert status in (400, 403)


def test_a_missing_host_header_is_refused_with_400(dashboard):
    status, body = _request(dashboard, "GET", "/api/status", host=None)
    assert status == 400 and b"Host" in body


@pytest.mark.parametrize("name", ["127.0.0.1", "localhost", "LOCALHOST", "[::1]", "[::ffff:127.0.0.1]"])
@pytest.mark.parametrize("path", ["/", "/api/status", "/api/budget", "/v2"])
def test_each_allowed_form_is_accepted(dashboard, name, path):
    status, _ = _request(dashboard, "GET", path, host=f"{name}:{dashboard[1]}")
    assert status == 200, f"Host {name} on {path} was refused"


def test_a_foreign_host_cannot_tell_a_real_route_from_a_missing_one(dashboard):
    real = _request(dashboard, "GET", "/api/status", host=f"evil.example:{dashboard[1]}")
    missing = _request(dashboard, "GET", "/no-such-page", host=f"evil.example:{dashboard[1]}")
    assert real == missing, "a rebinding page could learn which routes exist"


# --- round 1: one valid Host, absolute targets for every method ---------------------------------------------------


@pytest.fixture
def counted(tmp_path: Path, monkeypatch):
    """A dashboard whose budget reader counts its calls: a refused request must never reach it."""
    from agenttalk import budget

    calls: list[int] = []
    monkeypatch.setattr(budget.BudgetFeed, "get", lambda self: calls.append(1) or {"status": "ok", "synthetic": True})
    srv, _thread, base = web.serve_in_thread(_store(tmp_path), host="127.0.0.1", port=0, enable_actions=True,
                                             enable_budget=True)
    parsed = urllib.parse.urlsplit(base)
    try:
        yield (parsed.hostname, parsed.port), calls
    finally:
        srv.shutdown()
        srv.server_close()


def _multi_host(address, hosts: list[str], path: str = "/api/budget") -> int:
    conn = http.client.HTTPConnection(address[0], address[1], timeout=10)
    try:
        conn.putrequest("GET", path, skip_host=True)
        for value in hosts:
            conn.putheader("Host", value)
        conn.endheaders()
        response = conn.getresponse()
        response.read()
        return response.status
    finally:
        conn.close()


def test_two_host_fields_are_refused_without_reaching_the_reader(counted):
    address, calls = counted
    local, foreign = f"127.0.0.1:{address[1]}", f"evil.example:{address[1]}"
    for hosts in ([local, foreign], [foreign, local], [local, local]):
        assert _multi_host(address, hosts) == 400, hosts
    assert calls == [], "a request with two Host fields reached the protected reader"


def _bad_hosts(port: int) -> list[str]:
    return [f"127.0.0.1:{port}/evil", f"127.0.0.1:{port}?evil", f"127.0.0.1:{port}#evil", "[::1",
            f"[evil.example]:{port}", f"127.0.0.1:{port}:{port}", "127.0.0.1:", f"localhost.:{port}",
            f"user@127.0.0.1:{port}", "127.0.0.1:99999", f"127.0.0.1:{port} evil", f"127.0.0.1:{port}x", "[::1]x",
            f"[]:{port}"]


def test_a_host_that_is_not_exactly_host_and_port_is_refused_with_400(counted, capfd):
    address, calls = counted
    for value in _bad_hosts(address[1]):
        status = _multi_host(address, [value])
        assert status == 400, f"Host {value!r} answered {status}"
    assert calls == [], "an invalid Host reached the protected reader"
    assert _multi_host(address, [f"127.0.0.1:{address[1]}"]) == 200, "the server must keep serving"
    assert "Traceback" not in capfd.readouterr().err, "an invalid Host raised an uncaught error"


@pytest.mark.parametrize("method", ["GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
def test_a_foreign_or_wrong_port_absolute_target_is_refused_for_every_method(counted, method):
    address, calls = counted
    local = f"127.0.0.1:{address[1]}"
    for target in (f"http://evil.example:{address[1]}/api/budget", "http://127.0.0.1:1/api/budget",
                   "http://127.0.0.1/api/budget", f"http://localhost.:{address[1]}/api/budget"):
        status, _ = _request(address, method, target, host=local)
        assert status in (400, 403), f"{method} {target} answered {status}"
    assert calls == []


@pytest.mark.parametrize("target", ["http://evil.example@127.0.0.1:{p}/api/budget",
                                    "http://127.0.0.1:{p}@evil.example/api/budget",
                                    "ftp://127.0.0.1:{p}/api/budget", "evil.example:80", "http://[::1/api/budget"])
def test_a_malformed_absolute_target_is_refused_with_400(counted, target):
    address, calls = counted
    status, _ = _request(address, "GET", target.format(p=address[1]), host=f"127.0.0.1:{address[1]}")
    assert status == 400 and calls == []


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("name", ["127.0.0.1", "localhost", "[::1]"])
def test_a_local_absolute_target_still_works(counted, method, name):
    address, calls = counted
    status, _ = _request(address, method, f"http://{name}:{address[1]}/api/budget", host=f"127.0.0.1:{address[1]}")
    assert status == 200 and calls == [1]


def test_a_foreign_absolute_target_cannot_tell_a_real_route_from_a_missing_one(counted):
    address, _calls = counted
    local = f"127.0.0.1:{address[1]}"
    real = _request(address, "GET", f"http://evil.example:{address[1]}/api/status", host=local)
    missing = _request(address, "GET", f"http://evil.example:{address[1]}/no-such-page", host=local)
    assert real == missing
