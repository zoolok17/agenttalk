"""Tests for the read-only web dashboard (`agenttalk serve`).

The dashboard is the v0.7.0 deliverable; it has a security-sensitive
posture (loopback-only, no auth, render-arbitrary-bodies) so the
tests cover both happy-path rendering AND the refusal semantics.
"""
from __future__ import annotations

import concurrent.futures
import errno
import hashlib
import http.client
import inspect
import json
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

from agenttalk import avatars, intents, knowledge as kn, lesson_context as lc
from agenttalk import onboarding as ob
from agenttalk import signing, web
from agenttalk.store import Store


# --------------------------------------------------------------- helpers

def _make_store(tmp_path: Path) -> Store:
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    return s


def _serve(store: Store, *, host: str = "127.0.0.1", enable_actions: bool = False):
    """Start the server on an ephemeral port in a daemon thread.

    Returns (server, thread, base_url). Caller is responsible for
    ``server.shutdown(); server.server_close()`` in a finally block.
    """
    return web.serve_in_thread(store, host=host, port=0, enable_actions=enable_actions)


def _urlopen(target, *, timeout: float = 5.0, _attempts: int = 4, _backoff: float = 0.05):
    """``urllib.request.urlopen`` with a bounded retry on TRANSIENT connection errors.

    Windows CI intermittently aborts the connect/accept against the in-thread
    dashboard server (``ConnectionAbortedError`` / ``ConnectionResetError`` -
    WinError 10053/10054). Depending on where the abort lands, urllib surfaces it
    either as a bare ``ConnectionError`` (raised out of ``getresponse``) or wrapped
    in a ``URLError`` (raised out of ``do_open``), so retry BOTH of those forms.
    An ``HTTPError`` is a real HTTP status response - re-raise it IMMEDIATELY so the
    ``pytest.raises(HTTPError)`` 403/404/405 assertions are unaffected - and any
    OTHER ``URLError`` is a real failure, never retried. Off Windows the transient
    errors are not raised, so this is a transparent pass-through (no behavior change).
    On exhaustion the LAST transient error is re-raised, so a persistent/real failure
    still fails the test (no masking).
    """
    last_exc: BaseException | None = None
    for attempt in range(1, _attempts + 1):
        try:
            return urllib.request.urlopen(target, timeout=timeout)  # noqa: S310  # nosemgrep
        except urllib.error.HTTPError:
            raise  # a status response (403/404/405/...), never a transient abort
        except (ConnectionError, urllib.error.URLError) as exc:
            if isinstance(exc, urllib.error.URLError) and not isinstance(
                getattr(exc, "reason", None), ConnectionError
            ):
                raise  # a non-transient URLError - do not mask it
            last_exc = exc
            if attempt < _attempts:
                time.sleep(_backoff * attempt)
    assert last_exc is not None  # unreachable: the loop only exits here via an except
    raise last_exc


def _get(url: str, *, method: str = "GET", timeout: float = 5.0):
    req = urllib.request.Request(url, method=method)  # noqa: S310  # nosemgrep
    return _urlopen(req, timeout=timeout)


def _raw_abort(base: str, request: bytes) -> None:
    parsed = urllib.parse.urlsplit(base)
    assert parsed.hostname is not None and parsed.port is not None
    sock = socket.create_connection((parsed.hostname, parsed.port), timeout=5)
    try:
        sock.sendall(request)
    finally:
        sock.close()


def _read_stderr_until(
    capfd: pytest.CaptureFixture[str],
    needles: tuple[str, ...],
    *,
    timeout: float = 5.0,
    interval: float = 0.05,
) -> str:
    deadline = time.monotonic() + timeout
    buf = ""
    while True:
        buf += capfd.readouterr().err
        if all(needle in buf for needle in needles):
            return buf
        if time.monotonic() >= deadline:
            return buf
        time.sleep(interval)


def test_client_disconnect_mid_response_no_traceback_and_server_survives(
    tmp_path: Path, capfd: pytest.CaptureFixture[str],
) -> None:
    s = _make_store(tmp_path)
    srv, _t, base = _serve(s)
    try:
        _raw_abort(
            base,
            b"GET /static/console.js HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Connection: close\r\n\r\n",
        )
        with _get(f"{base}/api/status") as resp:
            assert resp.status == 200
    finally:
        srv.shutdown()
        srv.server_close()

    err = capfd.readouterr().err
    assert "Traceback" not in err
    assert "ConnectionAbortedError" not in err
    assert "ConnectionResetError" not in err
    assert "BrokenPipeError" not in err


def test_thread_route_real_error_returns_500_and_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    def boom(_store: Store, _rid: str) -> dict | None:
        raise RuntimeError("boom")

    monkeypatch.setattr(web, "build_thread", boom)
    s = _make_store(tmp_path)
    srv, _t, base = _serve(s)
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            _get(f"{base}/api/thread/thread-1")
        assert exc.value.code == 500
    finally:
        srv.shutdown()
        srv.server_close()

    err = _read_stderr_until(capfd, ("RuntimeError: boom",))
    assert "RuntimeError: boom" in err


def test_error_html_swallows_only_disconnect(tmp_path: Path) -> None:
    handler_cls = web._make_handler([web.RootDescriptor(_make_store(tmp_path), "root")])

    handler = object.__new__(handler_cls)
    handler.close_connection = False

    def disconnect(_status: int, _body: bytes, _csp: str | None = None) -> None:
        raise ConnectionAbortedError()

    handler._send_html = disconnect  # type: ignore[method-assign]
    handler._error_html(500, "internal server error")
    assert handler.close_connection is True

    handler = object.__new__(handler_cls)
    handler.close_connection = False

    def explode(_status: int, _body: bytes, _csp: str | None = None) -> None:
        raise ValueError("render failed")

    handler._send_html = explode  # type: ignore[method-assign]
    with pytest.raises(ValueError, match="render failed"):
        handler._error_html(500, "internal server error")
    assert handler.close_connection is False


def test_client_disconnect_classifier_is_type_and_errno_scoped() -> None:
    class WinDisconnect(OSError):
        pass

    win_abort = WinDisconnect("abort")
    win_abort.winerror = 10053  # type: ignore[attr-defined]

    assert web._is_client_disconnect(ConnectionAbortedError())
    assert web._is_client_disconnect(ConnectionResetError())
    assert web._is_client_disconnect(BrokenPipeError())
    assert web._is_client_disconnect(socket.timeout())
    assert web._is_client_disconnect(OSError(errno.EPIPE, "pipe"))
    assert web._is_client_disconnect(win_abort)

    assert not web._is_client_disconnect(Exception("boom"))
    assert not web._is_client_disconnect(RuntimeError("boom"))
    assert not web._is_client_disconnect(ValueError())
    assert not web._is_client_disconnect(OSError(errno.ENOENT, "missing"))


def test_handle_one_request_swallows_only_disconnects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler_cls = web._make_handler([web.RootDescriptor(_make_store(tmp_path), "root")])

    def disconnect(_self: BaseHTTPRequestHandler) -> None:
        raise ConnectionResetError()

    monkeypatch.setattr(BaseHTTPRequestHandler, "handle_one_request", disconnect)
    handler = object.__new__(handler_cls)
    handler.close_connection = False
    handler.handle_one_request()
    assert handler.close_connection is True

    def explode(_self: BaseHTTPRequestHandler) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(BaseHTTPRequestHandler, "handle_one_request", explode)
    handler = object.__new__(handler_cls)
    handler.close_connection = False
    with pytest.raises(RuntimeError, match="boom"):
        handler.handle_one_request()
    assert handler.close_connection is False


def _session(base: str, *, root: str | None = None) -> dict:
    suffix = f"?root={urllib.parse.quote(root)}" if root else ""
    with _get(f"{base}/api/session{suffix}") as resp:
        assert resp.status == 200
        return json.loads(resp.read())


def _post_intent(base: str, token: str, payload: dict,
                 *, origin: str | None = None, method: str = "POST",
                 root: str | None = None):
    data = json.dumps(payload).encode("utf-8")
    suffix = f"?root={urllib.parse.quote(root)}" if root else ""
    req = urllib.request.Request(  # noqa: S310  # nosemgrep
        f"{base}/api/intent{suffix}", method=method, data=data,
        headers={
            "Content-Type": "application/json",
            "X-CSRF-Token": token,
            "Origin": origin or base,
        })
    return _urlopen(req, timeout=5)


def _raw_post(base: str, target: str, *, host: str, origin: str,
              token: str, payload: dict) -> tuple[int, dict[str, str], bytes]:
    parsed = urllib.parse.urlsplit(base)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
    body = json.dumps(payload).encode("utf-8")
    try:
        conn.request(
            "POST",
            target,
            body=body,
            headers={
                "Host": host,
                "Origin": origin,
                "Content-Type": "application/json",
                "X-CSRF-Token": token,
                "Content-Length": str(len(body)),
            },
        )
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, dict(resp.getheaders()), data
    finally:
        conn.close()


# --------------------------------------------------------------- routing


def test_root_serves_team_console_shell(tmp_path: Path) -> None:
    s = _make_store(tmp_path)
    s.send(sender="alpha", recipient="beta", body="hello world")
    srv, _t, base = _serve(s)
    try:
        with _get(f"{base}/") as resp:
            assert resp.status == 200
            assert resp.headers["Content-Type"].startswith("text/html")
            assert resp.headers["Content-Security-Policy"] == _DASH_CSP
            body = resp.read().decode("utf-8")
        assert 'id="topbar"' in body
        assert "/static/console.css" in body
        assert "/static/console.js" in body
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_status_returns_json(tmp_path: Path) -> None:
    s = _make_store(tmp_path)
    srv, _t, base = _serve(s)
    try:
        with _get(f"{base}/api/status") as resp:
            assert resp.status == 200
            payload = json.loads(resp.read())
        assert payload["project_root"] == str(s.root)
        assert payload["project_id"] == s.project_id()
        assert payload["signing_enforced"] is False
        assert payload["agents"] == ["alpha", "beta"]
        assert payload["invalid_messages"] == []
        assert "agenttalk_version" in payload
        # 0.58.x (P2): per-agent health is keyed by the exact roster, and the
        # signing-key inspection is surfaced as hmac_key.
        assert "agent_health" in payload
        assert set(payload["agent_health"]) == set(payload["agents"])
        assert "hmac_key" in payload
    finally:
        srv.shutdown()
        srv.server_close()


def test_dashboard_rejects_non_loopback_host_on_bound_port(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    srv, _thread, base = _serve(store, enable_actions=True)
    parsed = urllib.parse.urlsplit(base)
    assert parsed.hostname is not None and parsed.port is not None
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
    try:
        conn.putrequest("GET", "/api/session", skip_host=True)
        conn.putheader("Host", f"evil.example:{parsed.port}")
        conn.putheader("Origin", f"http://evil.example:{parsed.port}")
        conn.endheaders()
        response = conn.getresponse()
        response.read()
        assert response.status == 403
    finally:
        conn.close()
        srv.shutdown()
        srv.server_close()


def test_dashboard_rejects_userinfo_disguised_as_loopback_host(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    srv, _thread, base = _serve(store, enable_actions=True)
    parsed = urllib.parse.urlsplit(base)
    assert parsed.hostname is not None and parsed.port is not None
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
    try:
        conn.putrequest("GET", "/api/session", skip_host=True)
        conn.putheader("Host", f"evil.example@localhost:{parsed.port}")
        conn.putheader("Origin", f"http://localhost:{parsed.port}")
        conn.endheaders()
        response = conn.getresponse()
        response.read()
        assert response.status == 403
    finally:
        conn.close()
        srv.shutdown()
        srv.server_close()


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "localhost", "LOCALHOST", "[::1]", "[::ffff:127.0.0.1]"],
)
def test_dashboard_accepts_supported_loopback_host_forms(
    tmp_path: Path, host: str,
) -> None:
    store = _make_store(tmp_path)
    srv, _thread, base = _serve(store, enable_actions=True)
    parsed = urllib.parse.urlsplit(base)
    assert parsed.hostname is not None and parsed.port is not None
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
    try:
        authority = f"{host}:{parsed.port}"
        conn.putrequest("GET", "/api/session", skip_host=True)
        conn.putheader("Host", authority)
        conn.putheader("Origin", f"http://{authority}")
        conn.endheaders()
        response = conn.getresponse()
        response.read()
        assert response.status == 200
    finally:
        conn.close()
        srv.shutdown()
        srv.server_close()


def test_api_messages_lists_all(tmp_path: Path) -> None:
    s = _make_store(tmp_path)
    s.send(sender="alpha", recipient="beta", body="one")
    s.send(sender="beta", recipient="alpha", body="two")
    srv, _t, base = _serve(s)
    try:
        with _get(f"{base}/api/messages") as resp:
            payload = json.loads(resp.read())
        msgs = payload["messages"]
        assert len(msgs) == 2
        bodies = {m["body"] for m in msgs}
        assert bodies == {"one", "two"}
        # Wire format mirrors store.Message.to_dict (from/to keys)
        assert all("from" in m and "to" in m for m in msgs)
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.mark.parametrize("config_state", ["corrupt", "empty"])
def test_dashboard_projections_fail_closed_for_unusable_roster(
    tmp_path: Path, config_state: str,
) -> None:
    s = _make_store(tmp_path)
    message = s.send(sender="alpha", recipient="beta", body="must-not-render")
    if config_state == "corrupt":
        s.config_path.write_text("{broken", encoding="utf-8")
    else:
        cfg = s.load_config()
        cfg["agents"] = []
        cfg["roles"] = {}
        cfg["groups"] = {}
        cfg["operator_facing"] = None
        s._write_config(cfg)

    srv, _t, base = _serve(s)
    try:
        with _get(f"{base}/api/messages") as resp:
            messages = json.loads(resp.read())
        with _get(f"{base}/api/status") as resp:
            status = json.loads(resp.read())
        with _get(f"{base}/api/state") as resp:
            state = json.loads(resp.read())

        assert messages["messages"] == []
        assert messages["errors"]
        assert status["agents"] == []
        assert status["errors"]
        root = state["roots"][0]
        assert root["errors"]
        assert "agents" not in root and "recent" not in root
        with pytest.raises(urllib.error.HTTPError) as exc:
            _get(f"{base}/api/messages/{message.id}")
        assert exc.value.code == 404
    finally:
        srv.shutdown()
        srv.server_close()


def test_message_detail_renders_body(tmp_path: Path) -> None:
    s = _make_store(tmp_path)
    s.send(sender="alpha", recipient="beta",
           subject="hi", body="UNIQUE_BODY_TOKEN_42")
    valid, _ = s._scan_messages()
    mid = valid[0].id
    srv, _t, base = _serve(s)
    try:
        with _get(f"{base}/messages/{mid}") as resp:
            assert resp.status == 200
            html_body = resp.read().decode("utf-8")
        assert "UNIQUE_BODY_TOKEN_42" in html_body
        assert mid in html_body
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_message_by_id(tmp_path: Path) -> None:
    s = _make_store(tmp_path)
    s.send(sender="alpha", recipient="beta", body="payload-x")
    mid = s._scan_messages()[0][0].id
    srv, _t, base = _serve(s)
    try:
        with _get(f"{base}/api/messages/{mid}") as resp:
            data = json.loads(resp.read())
        assert data["id"] == mid
        assert data["body"] == "payload-x"
    finally:
        srv.shutdown()
        srv.server_close()


# --------------------------------------------------------------- security


def test_post_returns_405(tmp_path: Path) -> None:
    s = _make_store(tmp_path)
    srv, _t, base = _serve(s)
    try:
        req = urllib.request.Request(f"{base}/", method="POST", data=b"x")  # noqa: S310  # nosemgrep
        with pytest.raises(urllib.error.HTTPError) as exc:
            _urlopen(req, timeout=5)
        assert exc.value.code == 405
        assert exc.value.headers.get("Allow") == "GET, HEAD"
    finally:
        srv.shutdown()
        srv.server_close()


def test_unknown_path_returns_404(tmp_path: Path) -> None:
    s = _make_store(tmp_path)
    srv, _t, base = _serve(s)
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            _urlopen(f"{base}/nope", timeout=5)
        assert exc.value.code == 404
    finally:
        srv.shutdown()
        srv.server_close()


def test_message_id_traversal_rejected(tmp_path: Path) -> None:
    """A path like /messages/..%2F..%2Fetc%2Fpasswd should 404 long
    before any filesystem touch — the route allowlist + the
    ``_MESSAGE_ID_RE`` regex both gate it."""
    s = _make_store(tmp_path)
    srv, _t, base = _serve(s)
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            _urlopen(
                f"{base}/messages/..%2F..%2Fetc%2Fpasswd", timeout=5,
            )
        assert exc.value.code == 404
    finally:
        srv.shutdown()
        srv.server_close()


def test_message_with_html_in_body_is_escaped(tmp_path: Path) -> None:
    """Defense against the LLM smuggling JS through a message body
    into the operator's browser."""
    s = _make_store(tmp_path)
    s.send(sender="alpha", recipient="beta",
           body="<script>alert('xss')</script>")
    mid = s._scan_messages()[0][0].id
    srv, _t, base = _serve(s)
    try:
        with _get(f"{base}/messages/{mid}") as resp:
            html_body = resp.read().decode("utf-8")
        assert "<script>alert" not in html_body
        assert "&lt;script&gt;alert" in html_body
        # CSP belt-and-braces
        csp = resp.headers["Content-Security-Policy"]
        assert "default-src 'none'" in csp
    finally:
        srv.shutdown()
        srv.server_close()


def _hand_write_message(store: Store, doc: dict) -> str:
    path = store.root / ".agenttalk" / "messages" / f"{doc['id']}.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return doc["id"]


def test_unknown_kind_message_not_rendered_but_surfaced_in_status(
    tmp_path: Path,
) -> None:
    """v0.7.0 iter-1 BLOCKER (Codex review): the dashboard rendered
    messages whose kind/roster validation would have failed in
    recv/tail, because `_all_messages` only applied HMAC verification.
    Iter-2 fix: `_all_messages` mirrors `messages_for`'s validation
    surface (Message.validate + HMAC). This test pins that fix."""
    s = _make_store(tmp_path)
    s.send(sender="alpha", recipient="beta", body="GOOD_MSG")
    bad_id = _hand_write_message(s, {
        "id": "20990101-000000-000000-BADK", "ts": "2099-01-01T00:00:00Z",
        "from": "alpha", "to": "beta", "kind": "execute-now",
        "subject": "", "body": "UNKNOWN_KIND_BODY_NOT_RENDERED", "meta": {},
    })
    srv, _t, base = _serve(s)
    try:
        with _get(f"{base}/api/messages") as resp:
            payload = json.loads(resp.read())
        bodies = {m["body"] for m in payload["messages"]}
        assert "GOOD_MSG" in bodies
        assert "UNKNOWN_KIND_BODY_NOT_RENDERED" not in bodies
        with _get(f"{base}/api/status") as resp:
            status = json.loads(resp.read())
        assert any(inv["id"] == bad_id for inv in status["invalid_messages"])
        with pytest.raises(urllib.error.HTTPError) as exc:
            _urlopen(
                f"{base}/messages/{bad_id}", timeout=5,
            )
        assert exc.value.code == 404
    finally:
        srv.shutdown()
        srv.server_close()


def test_out_of_roster_sender_or_recipient_not_rendered(tmp_path: Path) -> None:
    """v0.7.0 iter-1 BLOCKER (Codex review). A forged on-disk message
    naming a `from` or `to` outside the configured roster must NOT
    appear in /api/messages or /messages/<id>; only in
    /api/status.invalid_messages. Mirrors the CLI invariant."""
    s = _make_store(tmp_path)
    s.send(sender="alpha", recipient="beta", body="LEGIT")
    bad_from = _hand_write_message(s, {
        "id": "20990101-000000-000001-FRMx", "ts": "2099-01-01T00:00:00Z",
        "from": "mallory", "to": "beta", "kind": "message",
        "subject": "", "body": "FORGED_SENDER", "meta": {},
    })
    bad_to = _hand_write_message(s, {
        "id": "20990101-000000-000002-TOnn", "ts": "2099-01-01T00:00:00Z",
        "from": "alpha", "to": "outsider", "kind": "message",
        "subject": "", "body": "FORGED_RECIPIENT", "meta": {},
    })
    srv, _t, base = _serve(s)
    try:
        with _get(f"{base}/api/messages") as resp:
            payload = json.loads(resp.read())
        bodies = {m["body"] for m in payload["messages"]}
        assert "LEGIT" in bodies
        assert "FORGED_SENDER" not in bodies
        assert "FORGED_RECIPIENT" not in bodies
        with _get(f"{base}/api/status") as resp:
            status = json.loads(resp.read())
        invalid_ids = {inv["id"] for inv in status["invalid_messages"]}
        assert bad_from in invalid_ids
        assert bad_to in invalid_ids
    finally:
        srv.shutdown()
        srv.server_close()


def test_invalid_signature_body_not_rendered(tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    """If HMAC is enforced and a message has a bad signature, it must
    not appear in /api/messages or /messages/<id>. Mirrors the
    `tail` invariant from v0.6.0."""
    monkeypatch.setenv("AGENTTALK_HMAC_KEY_FILE", str(tmp_path / "k.key"))
    s = _make_store(tmp_path)
    signing.init_key(s.project_id())
    s.send(sender="alpha", recipient="beta", body="GOOD")
    # Forge an unsigned message via the same helper the iter-2
    # roster/kind tests use — keeps the semgrep
    # `agenttalk-no-direct-message-write` rule satisfied through
    # the existing `_hand_write_message` exemption.
    forged_id = _hand_write_message(s, {
        "id": "20990101-000000-000000-FORG", "ts": "2099-01-01T00:00:00Z",
        "from": "alpha", "to": "beta", "kind": "message",
        "subject": "", "body": "FORGED_NOT_RENDERED", "meta": {},
    })

    srv, _t, base = _serve(s)
    try:
        with _get(f"{base}/api/messages") as resp:
            payload = json.loads(resp.read())
        bodies = {m["body"] for m in payload["messages"]}
        assert "GOOD" in bodies
        assert "FORGED_NOT_RENDERED" not in bodies
        # status must surface the forgery the same way `doctor` does
        with _get(f"{base}/api/status") as resp:
            status = json.loads(resp.read())
        assert any(forged_id in inv["id"]
                   for inv in status["invalid_messages"])
        # detail route returns 404 for the forged id
        with pytest.raises(urllib.error.HTTPError) as exc:
            _urlopen(
                f"{base}/messages/{forged_id}", timeout=5,
            )
        assert exc.value.code == 404
    finally:
        srv.shutdown()
        srv.server_close()


def test_make_server_refuses_any_non_loopback_host(tmp_path: Path) -> None:
    """v0.7.0 iter-2: there is no opt-in to bind elsewhere. Strict
    loopback-only — every non-loopback host raises."""
    s = _make_store(tmp_path)
    for bad_host in ("0.0.0.0", "192.168.1.10", "example.com", "10.0.0.1"):  # noqa: S104
        with pytest.raises(ValueError) as exc:
            web.make_server(s, bad_host, 0)
        assert "loopback" in str(exc.value).lower()


def test_make_server_accepts_loopback_aliases(tmp_path: Path) -> None:
    s = _make_store(tmp_path)
    for host in ("127.0.0.1", "localhost", "::1"):
        srv = web.make_server(s, host, 0)
        srv.server_close()


def test_format_url_brackets_ipv6(tmp_path: Path) -> None:
    """v0.7.0 iter-2 fix: ``http://::1:8765`` is invalid (ambiguous
    port boundary); IPv6 hosts must be bracketed."""
    assert web._format_url("::1", 8765) == "http://[::1]:8765/"
    assert web._format_url("2001:db8::1", 80) == "http://[2001:db8::1]:80/"
    # IPv4 and hostnames stay unbracketed
    assert web._format_url("127.0.0.1", 8765) == "http://127.0.0.1:8765/"
    assert web._format_url("localhost", 80) == "http://localhost:80/"


def test_serve_in_thread_ipv6_produces_valid_url(tmp_path: Path) -> None:
    s = _make_store(tmp_path)
    try:
        srv, _t, base = web.serve_in_thread(s, host="::1", port=0)
    except OSError as e:
        pytest.skip(f"IPv6 loopback not available in this env: {e}")
    try:
        assert base.startswith("http://[::1]:")
        with _get(f"{base}/api/status") as resp:
            assert resp.status == 200
    finally:
        srv.shutdown()
        srv.server_close()


def test_non_loopback_peer_gets_403_for_every_method(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defense-in-depth: even if a misconfigured upstream proxy or
    a future bug let a non-loopback peer reach the handler, EVERY
    method must trip 403 — including POST/PUT/DELETE/PATCH that
    used to fall straight through to 405 (which leaked existence
    information to LAN probes). v0.7.0 iter-2 regression test."""
    s = _make_store(tmp_path)
    srv, _t, base = _serve(s)
    try:
        monkeypatch.setattr(
            srv.RequestHandlerClass, "_is_loopback_peer",
            lambda self: False,
        )
        for method in ("GET", "HEAD", "POST", "PUT", "DELETE", "PATCH"):
            req = urllib.request.Request(  # noqa: S310  # nosemgrep
                f"{base}/", method=method,
                data=b"x" if method in {"POST", "PUT", "PATCH"} else None,
            )
            with pytest.raises(urllib.error.HTTPError) as exc:
                _urlopen(req, timeout=5)
            assert exc.value.code == 403, f"{method} should 403, got {exc.value.code}"
    finally:
        srv.shutdown()
        srv.server_close()


def test_security_headers_present(tmp_path: Path) -> None:
    s = _make_store(tmp_path)
    srv, _t, base = _serve(s)
    try:
        with _get(f"{base}/") as resp:
            h = resp.headers
        assert h["X-Content-Type-Options"] == "nosniff"
        assert h["X-Frame-Options"] == "DENY"
        assert h["Referrer-Policy"] == "no-referrer"
        assert h["Cache-Control"] == "no-store"
        assert "Content-Security-Policy" in h
    finally:
        srv.shutdown()
        srv.server_close()


def test_favicon_returns_no_content(tmp_path: Path) -> None:
    s = _make_store(tmp_path)
    srv, _t, base = _serve(s)
    try:
        with _get(f"{base}/favicon.ico") as resp:
            assert resp.status == 204
            assert resp.read() == b""
    finally:
        srv.shutdown()
        srv.server_close()


def test_head_returns_headers_no_body(tmp_path: Path) -> None:
    s = _make_store(tmp_path)
    srv, _t, base = _serve(s)
    try:
        with _get(f"{base}/", method="HEAD") as resp:
            assert resp.status == 200
            assert resp.read() == b""
            assert resp.headers["Content-Type"].startswith("text/html")
    finally:
        srv.shutdown()
        srv.server_close()


# ===================================================== 0.17.0 /api/state
#
# Obligation-dashboard coverage (mission obligation-dashboard-0170).
# Contract under test: kitty-specs/obligation-dashboard-0170-01KTHADQ/
# data-model.md (schema v1) + research.md D5/D6/D9.

# The pre-0.17.0 CSP, byte-for-byte. The split-policy tests pin BOTH
# literals so neither can drift silently (FR-009 / research D1).
_LEGACY_CSP = ("default-src 'none'; style-src 'unsafe-inline'; "
               "img-src 'none'; frame-ancestors 'none'")
_DASH_CSP = ("default-src 'none'; script-src 'self'; "
             "connect-src 'self'; style-src 'self'; "
             "img-src 'self'; frame-ancestors 'none'")


def _make_two_stores(tmp_path: Path) -> tuple[Store, Store]:
    a = Store(tmp_path / "proj-a")
    (tmp_path / "proj-a").mkdir()
    a.init(["alpha", "beta"])
    b = Store(tmp_path / "proj-b")
    (tmp_path / "proj-b").mkdir()
    b.init(["lead", "dev"])
    return a, b


def _make_same_named_store_list(tmp_path: Path, count: int) -> list[Store]:
    roots = [tmp_path / f"parent-{index}" / "project" for index in range(count)]
    stores: list[Store] = []
    for root in roots:
        root.mkdir(parents=True)
        store = Store(root)
        store.init(["alpha", "beta"])
        stores.append(store)
    return stores


def _make_same_named_stores(tmp_path: Path) -> tuple[Store, Store]:
    stores = _make_same_named_store_list(tmp_path, 2)
    return stores[0], stores[1]


def _serve_multi(first: Store, *rest: Store):
    extra = [web.RootDescriptor(store=s, label=s.root.name) for s in rest]
    return web.serve_in_thread(first, extra=extra)


def _state(base: str) -> dict:
    with _get(f"{base}/api/state") as resp:
        assert resp.status == 200
        return json.loads(resp.read())


def _assert_no_body_keys(node) -> None:
    if isinstance(node, dict):
        assert "body" not in node, f"body key leaked into /api/state: {node}"
        for v in node.values():
            _assert_no_body_keys(v)
    elif isinstance(node, list):
        for v in node:
            _assert_no_body_keys(v)


def test_api_state_schema_v1(tmp_path: Path) -> None:
    s = _make_store(tmp_path)
    s.send(sender="alpha", recipient="beta", kind="review-request",
           subject="review X", body="please review",
           meta={"request_id": "q1", "mission": "demo", "wp_id": "WP01"})
    srv, _t, base = _serve(s)
    try:
        state = _state(base)
        assert state["schema_version"] == 1
        assert set(state) == {"schema_version", "agenttalk_version",
                              "generated_at", "roots"}
        (root,) = state["roots"]
        assert root["errors"] == []
        assert root["path"] == str(s.root)
        assert root["project_id"] == s.project_id()
        assert root["counts"]["messages"] == 1
        assert root["counts"]["open_threads"] == 1
        (row,) = root["threads"]
        assert row["request_id"] == "q1"
        assert row["state"] == "owed-inbound"
        assert row["next_owner"] == "beta"
        assert row["next_action"] == "reply"
        assert row["mission"] == "demo"
        assert row["wp_id"] == "WP01"
        # opener was auto-stamped null by 0.16.0 send(); no barrier yet
        assert "epoch_at_send" in row and row["epoch_at_send"] is None
        assert row["epoch_status"] == "current"
        assert root["epoch"] is None
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_state_absent_not_null(tmp_path: Path) -> None:
    s = _make_store(tmp_path)
    srv, _t, base = _serve(s)
    try:
        state = _state(base)
        (root,) = state["roots"]
        for agent in root["agents"]:
            for absent in ("role", "groups", "operator_facing",
                           "last_seen", "last_seen_age_seconds", "composing"):
                assert absent not in agent
        assert "operator_facing" not in root
        assert "spec_kitty" not in root
        _assert_no_body_keys(state)
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_state_unwrapped_fresh_heartbeat_stays_raw_unknown(tmp_path: Path) -> None:
    s = _make_store(tmp_path)
    s.write_heartbeat("alpha")
    srv, _t, base = _serve(s)
    try:
        (root,) = _state(base)["roots"]
        alpha = next(a for a in root["agents"] if a["name"] == "alpha")
        assert set(alpha) == {
            "name", "last_seen", "last_seen_age_seconds", "health",
            "unread", "sent", "received",
        }
        assert alpha["health"]["state"] == "unknown"
        assert 0 <= alpha["last_seen_age_seconds"] <= 120
        assert "wrapped" not in alpha
        assert "unwrapped_live" not in json.dumps(alpha)
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_state_unwrapped_stale_heartbeat_stays_raw_unknown(tmp_path: Path) -> None:
    from datetime import datetime, timedelta, timezone

    s = _make_store(tmp_path)
    old = datetime.now(timezone.utc) - timedelta(seconds=130)
    (s.state_dir / "alpha.heartbeat").write_text(old.isoformat(), encoding="utf-8")
    srv, _t, base = _serve(s)
    try:
        (root,) = _state(base)["roots"]
        alpha = next(a for a in root["agents"] if a["name"] == "alpha")
        assert set(alpha) == {
            "name", "last_seen", "last_seen_age_seconds", "health",
            "unread", "sent", "received",
        }
        assert alpha["health"]["state"] == "unknown"
        assert alpha["last_seen_age_seconds"] > 120
        assert "wrapped" not in alpha
        assert "unwrapped_live" not in json.dumps(alpha)
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_state_composing_array(tmp_path: Path) -> None:
    s = _make_store(tmp_path)
    s.write_composing_intent("alpha", "q-2", "beta")
    s.write_composing_intent("alpha", "q-1", "beta")
    srv, _t, base = _serve(s)
    try:
        (root,) = _state(base)["roots"]
        alpha = next(a for a in root["agents"] if a["name"] == "alpha")
        comp = alpha["composing"]
        assert [c["request_id"] for c in comp] == ["q-1", "q-2"]  # sorted
        for c in comp:
            assert c["peer"] == "beta"
            assert isinstance(c["age_seconds"], (int, float))
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_state_excludes_stale_composing(tmp_path: Path) -> None:
    """A crashed/abandoned composing marker (older than the CLI's active
    window) must NOT show on the dashboard — the CLI applies
    0 <= age <= COMPOSING_INTENT_STALE_SECONDS; /api/state must too,
    or it shows dead writers as 'composing' forever (review C2/M1)."""
    s = _make_store(tmp_path)
    s.write_composing_intent("alpha", "q-fresh", "beta")
    s.write_composing_intent("alpha", "q-stale", "beta")
    cf = s.state_dir / "alpha.composing.json"
    data = json.loads(cf.read_text(encoding="utf-8"))
    data["threads"]["q-stale"]["at"] = "2020-01-01T00:00:00+00:00"  # ancient
    cf.write_text(json.dumps(data), encoding="utf-8")
    srv, _t, base = _serve(s)
    try:
        (root,) = _state(base)["roots"]
        alpha = next(a for a in root["agents"] if a["name"] == "alpha")
        rids = [c["request_id"] for c in alpha.get("composing", [])]
        assert "q-fresh" in rids
        assert "q-stale" not in rids
    finally:
        srv.shutdown()
        srv.server_close()


def test_root_state_degrades_on_unexpected_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A root's collection must degrade to errors-as-data for ANY exception,
    not just OSError/ValueError — one corrupt root must never 500 the whole
    /api/state aggregate (FR-005, review)."""
    s = _make_store(tmp_path)

    def boom(*_a, **_k):
        raise RuntimeError("unexpected non-ValueError failure")

    monkeypatch.setattr(web, "_agent_entries", boom)
    srv, _t, base = _serve(s)
    try:
        state = _state(base)  # must be 200, not 500
        (root,) = state["roots"]
        assert root["errors"]  # degraded, not crashed
        assert "unexpected" in root["errors"][0]
    finally:
        srv.shutdown()
        srv.server_close()


def test_is_loopback_addr_is_address_aware() -> None:
    """The per-request loopback check is address-aware, not string-prefix:
    a non-loopback IPv6 peer that merely starts with '::1' is rejected, while
    the IPv4-mapped loopback form is accepted (review nit)."""
    assert web._is_loopback_addr("127.0.0.1")
    assert web._is_loopback_addr("::1")
    assert web._is_loopback_addr("::ffff:127.0.0.1")    # v4-mapped loopback
    assert web._is_loopback_addr("localhost")
    assert not web._is_loopback_addr("::1a2b:0:0:0:1")  # startswith '::1', NOT loopback
    assert not web._is_loopback_addr("10.0.0.5")
    assert not web._is_loopback_addr("8.8.8.8")
    assert not web._is_loopback_addr("evil.example.com")
    assert not web._is_loopback_addr("")


def test_make_server_localhost_binds_loopback_literal(tmp_path: Path) -> None:
    """`localhost` is bound as the 127.0.0.1 literal, not delegated to the OS
    resolver (a hosts override could otherwise point it off-loopback)."""
    s = _make_store(tmp_path)
    srv = web.make_server(s, "localhost", 0)
    try:
        assert srv.server_address[0] == "127.0.0.1"
    finally:
        srv.server_close()


def test_make_server_ipv6_uses_inet6_without_global_mutation(tmp_path: Path) -> None:
    """IPv6 loopback uses a per-server AF_INET6 subclass and never mutates the
    process-global ThreadingHTTPServer.address_family (review nit)."""
    import socket as _socket
    from http.server import ThreadingHTTPServer
    before = ThreadingHTTPServer.address_family
    s = _make_store(tmp_path)
    try:
        srv = web.make_server(s, "::1", 0)
    except OSError:
        pytest.skip("no IPv6 loopback available")
    try:
        assert srv.address_family == _socket.AF_INET6        # per-server family
        assert ThreadingHTTPServer.address_family == before  # base class untouched
    finally:
        srv.server_close()


def test_message_subject_and_meta_are_html_escaped(tmp_path: Path) -> None:
    """XSS defense isn't only the body: subject and meta keys/values are also
    attacker-influenced (a coding agent can write arbitrary message JSON) and
    rendered into the operator's browser, so they must be escaped too."""
    s = _make_store(tmp_path)
    s.send(sender="alpha", recipient="beta",
           subject="<script>alert('subj')</script>",
           body="ok",
           meta={"<script>k</script>": "<script>v</script>"})
    mid = s._scan_messages()[0][0].id
    srv, _t, base = _serve(s)
    try:
        with _get(f"{base}/messages/{mid}") as resp:
            html_body = resp.read().decode("utf-8")
        assert "<script>alert('subj')" not in html_body      # subject escaped
        assert "&lt;script&gt;alert(&#x27;subj&#x27;)" in html_body
        assert "<script>k</script>" not in html_body         # meta key escaped
        assert "<script>v</script>" not in html_body         # meta value escaped
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_state_epoch_status_three_state(tmp_path: Path) -> None:
    s = _make_store(tmp_path)
    # pre-0.16-style opener: hand-written WITHOUT the epoch_at_send key
    _hand_write_message(s, {
        "id": "20200101-000000-000000-OLDx", "ts": "2020-01-01T00:00:00Z",
        "from": "alpha", "to": "beta", "kind": "review-request",
        "subject": "ancient", "body": "old",
        "meta": {"request_id": "q-old"},
    })
    # epoch-aware opener, stamped null by send() (no barrier yet)
    s.send(sender="alpha", recipient="beta", kind="question",
           subject="pre-barrier", body="x", meta={"request_id": "q-null"})
    state_root = lambda b: _state(b)["roots"][0]  # noqa: E731
    srv, _t, base = _serve(s)
    try:
        root = state_root(base)
        assert root["epoch"] is None
        assert {r["request_id"]: r["epoch_status"] for r in root["threads"]} \
            == {"q-old": "current", "q-null": "current"}  # no barrier → nothing stale
        # fire a barrier (one self-addressed meta-marked message, D1/0.16.0)
        s.send(sender="alpha", recipient="alpha", kind="message",
               subject="epoch bump", body="barrier",
               meta={"barrier": {"version": 1, "scope": "global",
                                 "type": "epoch-bump"}})
        cur = s.current_epoch()
        assert cur is not None
        # post-barrier opener stamps the live epoch
        s.send(sender="alpha", recipient="beta", kind="question",
               subject="post-barrier", body="y", meta={"request_id": "q-new"})
        root = state_root(base)
        assert root["epoch"] == cur
        by_rid = {r["request_id"]: r for r in root["threads"]}
        assert by_rid["q-old"]["epoch_status"] == "unknown-pre-epoch"
        assert "epoch_at_send" not in by_rid["q-old"]  # forwarded as ABSENT
        assert by_rid["q-null"]["epoch_status"] == "previous-epoch"
        assert by_rid["q-null"]["epoch_at_send"] is None
        assert by_rid["q-new"]["epoch_status"] == "current"
        assert by_rid["q-new"]["epoch_at_send"] == cur
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_state_thread_dedup_ball_holder(tmp_path: Path) -> None:
    s = _make_store(tmp_path)
    s.send(sender="alpha", recipient="beta", kind="review-request",
           subject="review me", body="x", meta={"request_id": "rid-1"})
    srv, _t, base = _serve(s)
    try:
        (root,) = _state(base)["roots"]
        assert len(root["threads"]) == 1  # one row despite two perspectives
        row = root["threads"][0]
        assert row["next_owner"] == "beta"  # absolute name, ball-holder view
        assert row["state"] == "owed-inbound"
        # the reply flips the ball back to the requester (still ONE row)
        s.send(sender="beta", recipient="alpha", kind="review-result",
               subject="done", body="lgtm",
               meta={"request_id": "rid-1", "status": "approved"})
        (root,) = _state(base)["roots"]
        assert len(root["threads"]) == 1
        row = root["threads"][0]
        assert row["next_owner"] == "alpha"
        assert row["next_action"] == "read-reply"
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_state_rescinded_thread_excluded_and_reason_never_leaks(
    tmp_path: Path,
) -> None:
    """Fresh-eyes 0.17.0 hardening pin: a rescinded thread is terminal →
    not a row (counted in closed_threads), and the rescind block — whose
    `reason` carries sender-supplied BODY text — must never appear on
    any /api/state row even if the exclusion invariant ever drifts."""
    s = _make_store(tmp_path)
    s.send(sender="alpha", recipient="beta", kind="review-request",
           subject="doomed", body="x", meta={"request_id": "rid-resc"})
    s.send(sender="alpha", recipient="beta", kind="rescind",
           subject="rescinding", body="SECRET RESCIND REASON BODY",
           meta={"request_id": "rid-resc"})
    s.send(sender="alpha", recipient="beta", kind="question",
           subject="alive", body="y", meta={"request_id": "rid-live"})
    srv, _t, base = _serve(s)
    try:
        raw = json.dumps(_state(base))
        assert "SECRET RESCIND REASON BODY" not in raw
        (root,) = _state(base)["roots"]
        rids = [r["request_id"] for r in root["threads"]]
        assert "rid-resc" not in rids and "rid-live" in rids
        assert root["counts"]["closed_threads"] >= 1
        for r in root["threads"]:
            assert "rescind" not in r
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_state_broadcast_summary(tmp_path: Path) -> None:
    s = Store(tmp_path)
    s.init(["alpha", "beta", "gamma"])
    bid = "bc-1"
    for member in ("beta", "gamma"):
        s.send(sender="alpha", recipient=member, kind="question",
               subject="poll", body="q",
               meta={"request_id": bid, "broadcast_id": bid,
                     "audience": ["beta", "gamma"]})
    # any non-control reply closes a member's broadcast obligation
    s.send(sender="beta", recipient="alpha", kind="message",
           subject="re: poll", body="a", meta={"request_id": bid})
    srv, _t, base = _serve(s)
    try:
        (root,) = _state(base)["roots"]
        (bc,) = root["broadcasts"]
        assert bc["request_id"] == bid
        assert bc["requester"] == "alpha"
        assert bc["responded"] == ["beta"]
        assert bc["pending"] == ["gamma"]
        # broadcasts also appear in threads with the pending set as owner
        row = next(r for r in root["threads"] if r["request_id"] == bid)
        assert row["is_broadcast"] is True
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_state_multi_root_separation(tmp_path: Path) -> None:
    a, b = _make_two_stores(tmp_path)
    a.send(sender="alpha", recipient="beta", kind="question",
           subject="a-thread", body="x", meta={"request_id": "rid-a"})
    b.send(sender="lead", recipient="dev", kind="question",
           subject="b-thread", body="y", meta={"request_id": "rid-b"})
    srv, _t, base = _serve_multi(a, b)
    try:
        roots = _state(base)["roots"]
        assert [r["label"] for r in roots] == ["proj-a", "proj-b"]
        assert [r["path"] for r in roots] == [str(a.root), str(b.root)]
        names_a = {ag["name"] for ag in roots[0]["agents"]}
        names_b = {ag["name"] for ag in roots[1]["agents"]}
        assert names_a == {"alpha", "beta"} and names_b == {"lead", "dev"}
        assert [t["request_id"] for t in roots[0]["threads"]] == ["rid-a"]
        assert [t["request_id"] for t in roots[1]["threads"]] == ["rid-b"]
    finally:
        srv.shutdown()
        srv.server_close()


def test_same_name_root_labels_are_stable_distinct_and_order_independent(
    tmp_path: Path,
) -> None:
    a, b = _make_same_named_stores(tmp_path)
    forward = web.make_descriptors([a.root, b.root])
    reverse = web.make_descriptors([b.root, a.root])

    by_path = {str(desc.store.root): desc.label for desc in forward}
    reversed_by_path = {str(desc.store.root): desc.label for desc in reverse}
    assert by_path == reversed_by_path
    assert len(set(by_path.values())) == 2
    assert a.project_id()[:8] in by_path[str(a.root)]
    assert b.project_id()[:8] in by_path[str(b.root)]


def test_multi_root_explicit_project_id_routes_every_console_get(
    tmp_path: Path,
) -> None:
    a, b = _make_same_named_stores(tmp_path)
    for store, marker in ((a, "from-a"), (b, "from-b")):
        store.send(
            sender="alpha",
            recipient="beta",
            kind="question",
            subject=marker,
            body=marker,
            meta={"request_id": "rid-shared"},
        )
    b.write_intent("send", {"target": "beta", "body": "only-b"})

    srv, _t, base = web.serve_in_thread(
        a,
        extra=[web.RootDescriptor(store=b, label=b.root.name)],
        enable_actions=True,
    )
    try:
        state_roots = _state(base)["roots"]
        assert len({root["label"] for root in state_roots}) == 2
        assert all(root["project_id"][:8] in root["label"] for root in state_roots)
        project_id = urllib.parse.quote(b.project_id())
        paths = (
            "/api/session",
            "/api/intents",
            "/api/preflight",
            "/api/attention",
            "/api/learning",
            "/api/onboarding",
            "/api/lead-chat",
            "/api/threads?state=closed",
            "/api/thread/rid-shared",
        )
        for path in paths:
            separator = "&" if "?" in path else "?"
            with _get(f"{base}{path}{separator}root={project_id}") as resp:
                assert resp.status == 200, path
                payload = json.loads(resp.read())
            assert payload["root_info"]["project_id"] == b.project_id(), path
            assert payload["root_info"]["path"] == str(b.root), path
            assert payload["target_root_project_id"] == b.project_id(), path

        with _get(f"{base}/api/thread/rid-shared?root={project_id}") as resp:
            thread = json.loads(resp.read())
        assert [message["body"] for message in thread["messages"]] == ["from-b"]

        with _get(f"{base}/api/intents") as resp:
            primary = json.loads(resp.read())
        assert primary["root_info"]["project_id"] == a.project_id()
    finally:
        srv.shutdown()
        srv.server_close()


def test_multi_root_session_and_post_mutate_only_selected_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, b = _make_same_named_stores(tmp_path)
    srv, _t, base = web.serve_in_thread(
        a,
        extra=[web.RootDescriptor(store=b, label=b.root.name)],
        enable_actions=True,
    )
    try:
        session = _session(base, root=b.project_id())
        assert session["root_info"]["project_id"] == b.project_id()
        with _post_intent(
            base,
            session["csrf_token"],
            {
                "kind": "send",
                "payload": {
                    "target": "beta",
                    "body": "selected project only",
                    "message_kind": "message",
                },
            },
            root=b.project_id(),
        ) as resp:
            assert resp.status == 202
            accepted = json.loads(resp.read())
        assert accepted["root_info"]["project_id"] == b.project_id()
        assert accepted["target_root_project_id"] == b.project_id()
        assert a.list_intents() == []
        assert [record["intent_id"] for record in b.list_intents()] == [
            accepted["intent_id"]
        ]

        selected_chat_roots: list[Path] = []

        class _Sent:
            id = "lead-chat-selected-root"

        def fake_lead_chat(target_store: Store, *, body: str):
            selected_chat_roots.append(target_store.root)
            assert body == "selected lead chat"
            return _Sent(), {"request_id": "lc-selected"}, None

        monkeypatch.setattr(web, "_send_authenticated_lead_chat", fake_lead_chat)
        lead_body = json.dumps({"body": "selected lead chat"}).encode("utf-8")
        request = urllib.request.Request(  # noqa: S310  # nosemgrep
            f"{base}/api/lead-chat?root={b.project_id()}",
            method="POST",
            data=lead_body,
            headers={
                "Content-Type": "application/json",
                "X-CSRF-Token": session["csrf_token"],
                "Origin": base,
            },
        )
        with _urlopen(request, timeout=5) as resp:
            assert resp.status == 202
            lead_chat = json.loads(resp.read())
        assert selected_chat_roots == [b.root]
        assert lead_chat["root_info"]["project_id"] == b.project_id()
        assert lead_chat["target_root_project_id"] == b.project_id()
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.mark.parametrize(
    ("endpoint", "payload"),
    [
        (
            "/api/intent",
            {
                "kind": "send",
                "payload": {"target": "beta", "body": "must not route"},
            },
        ),
        ("/api/lead-chat", {"body": "must not route"}),
    ],
)
@pytest.mark.parametrize("selector", ["omitted", "label", "blank", "repeated"])
def test_multi_root_posts_require_one_full_project_id_without_mutation(
    tmp_path: Path, endpoint: str, payload: dict, selector: str,
) -> None:
    a, b = _make_same_named_stores(tmp_path)
    srv, _t, base = web.serve_in_thread(
        a,
        extra=[web.RootDescriptor(store=b, label=b.root.name)],
        enable_actions=True,
    )
    try:
        token = _session(base)["csrf_token"]
        roots = _state(base)["roots"]
        if selector == "omitted":
            suffix = ""
        elif selector == "label":
            suffix = "?root=" + urllib.parse.quote(roots[1]["label"])
        elif selector == "blank":
            suffix = "?root="
        else:
            suffix = f"?root={a.project_id()}&root={a.project_id()}"
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(  # noqa: S310  # nosemgrep
            f"{base}{endpoint}{suffix}",
            method="POST",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-CSRF-Token": token,
                "Origin": base,
            },
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            _urlopen(request, timeout=5)
        assert exc.value.code == 400
        assert json.loads(exc.value.read())["error"] == "bad_root"
    finally:
        srv.shutdown()
        srv.server_close()
    assert a.list_intents() == []
    assert b.list_intents() == []
    assert list(a.valid_messages()) == []
    assert list(b.valid_messages()) == []


def test_single_root_omitted_posts_remain_compatible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _make_store(tmp_path)
    selected_chat_roots: list[Path] = []

    class _Sent:
        id = "single-root-lead-chat"

    def fake_lead_chat(target_store: Store, *, body: str):
        selected_chat_roots.append(target_store.root)
        assert body == "single root"
        return _Sent(), {"request_id": "lc-single"}, None

    monkeypatch.setattr(web, "_send_authenticated_lead_chat", fake_lead_chat)
    srv, _t, base = _serve(store, enable_actions=True)
    try:
        token = _session(base)["csrf_token"]
        with _post_intent(
            base,
            token,
            {
                "kind": "send",
                "payload": {"target": "beta", "body": "single root"},
            },
        ) as resp:
            intent_result = json.loads(resp.read())
        assert intent_result["target_root_project_id"] == store.project_id()

        body = json.dumps({"body": "single root"}).encode("utf-8")
        request = urllib.request.Request(  # noqa: S310  # nosemgrep
            f"{base}/api/lead-chat",
            method="POST",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-CSRF-Token": token,
                "Origin": base,
            },
        )
        with _urlopen(request, timeout=5) as resp:
            lead_result = json.loads(resp.read())
        assert lead_result["target_root_project_id"] == store.project_id()
        assert selected_chat_roots == [store.root]
    finally:
        srv.shutdown()
        srv.server_close()


def test_multi_root_bad_csrf_is_rejected_for_each_selected_project(
    tmp_path: Path,
) -> None:
    a, b = _make_same_named_stores(tmp_path)
    srv, _t, base = web.serve_in_thread(
        a,
        extra=[web.RootDescriptor(store=b, label=b.root.name)],
        enable_actions=True,
    )
    try:
        token = _session(base)["csrf_token"]
        for selected in (a, b):
            with pytest.raises(urllib.error.HTTPError) as exc:
                _post_intent(
                    base,
                    token + "-wrong",
                    {
                        "kind": "send",
                        "payload": {"target": "beta", "body": "bad csrf"},
                    },
                    root=selected.project_id(),
                )
            assert exc.value.code == 403
            assert json.loads(exc.value.read())["error"] == "bad_csrf"
    finally:
        srv.shutdown()
        srv.server_close()
    assert a.list_intents() == []
    assert b.list_intents() == []


def test_three_same_name_roots_route_by_id_and_unique_legacy_label(
    tmp_path: Path,
) -> None:
    stores = _make_same_named_store_list(tmp_path, 3)
    for index, store in enumerate(stores):
        store.send(
            sender="alpha",
            recipient="beta",
            body=f"root-{index}",
            meta={"request_id": "rid-three"},
        )
    srv, _t, base = _serve_multi(stores[0], *stores[1:])
    try:
        roots = _state(base)["roots"]
        assert len({root["label"] for root in roots}) == 3
        for index, (store, root) in enumerate(zip(stores, roots, strict=True)):
            assert root["project_id"] == store.project_id()
            for selector in (store.project_id(), root["label"]):
                with _get(
                    f"{base}/api/thread/rid-three?root={urllib.parse.quote(selector)}"
                ) as resp:
                    payload = json.loads(resp.read())
                assert payload["target_root_project_id"] == store.project_id()
                assert [item["body"] for item in payload["messages"]] == [
                    f"root-{index}"
                ]
    finally:
        srv.shutdown()
        srv.server_close()


def test_duplicate_project_id_descriptors_are_rejected(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    roots = [
        web.RootDescriptor(store=store, label="first"),
        web.RootDescriptor(store=store, label="second"),
    ]
    with pytest.raises(ValueError, match="duplicate dashboard project_id"):
        web._make_handler(roots)


@pytest.mark.parametrize(
    "path",
    [
        "/api/intents",
        "/api/preflight",
        "/api/attention",
        "/api/learning",
        "/api/onboarding",
        "/api/lead-chat",
        "/api/threads?state=closed",
        "/api/thread/rid-shared",
    ],
)
def test_explicit_unknown_root_fails_closed_for_console_gets(
    tmp_path: Path, path: str,
) -> None:
    a, b = _make_same_named_stores(tmp_path)
    a.send(
        sender="alpha",
        recipient="beta",
        body="must not leak",
        meta={"request_id": "rid-shared"},
    )
    srv, _t, base = _serve_multi(a, b)
    try:
        separator = "&" if "?" in path else "?"
        with pytest.raises(urllib.error.HTTPError) as exc:
            _get(f"{base}{path}{separator}root=missing-project")
        assert exc.value.code == 400, path
        assert json.loads(exc.value.read())["error"] == "bad_root", path
    finally:
        srv.shutdown()
        srv.server_close()


def test_repeated_root_query_fails_closed_for_console_get(tmp_path: Path) -> None:
    a, b = _make_same_named_stores(tmp_path)
    srv, _t, base = _serve_multi(a, b)
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            _get(
                f"{base}/api/intents?root={a.project_id()}&root={a.project_id()}"
            )
        assert exc.value.code == 400
        assert json.loads(exc.value.read())["error"] == "bad_root"
    finally:
        srv.shutdown()
        srv.server_close()


def test_explicit_unknown_root_post_fails_without_mutation(tmp_path: Path) -> None:
    a, b = _make_same_named_stores(tmp_path)
    srv, _t, base = web.serve_in_thread(
        a,
        extra=[web.RootDescriptor(store=b, label=b.root.name)],
        enable_actions=True,
    )
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            _session(base, root="missing-project")
        assert exc.value.code == 400
        assert json.loads(exc.value.read())["error"] == "bad_root"

        token = _session(base)["csrf_token"]
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post_intent(
                base,
                token,
                {
                    "kind": "send",
                    "payload": {"target": "beta", "body": "wrong root"},
                },
                root="missing-project",
            )
        assert exc.value.code == 400
        assert json.loads(exc.value.read())["error"] == "bad_root"

        body = json.dumps({"body": "wrong root"}).encode("utf-8")
        request = urllib.request.Request(  # noqa: S310  # nosemgrep
            f"{base}/api/lead-chat?root=missing-project",
            method="POST",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-CSRF-Token": token,
                "Origin": base,
            },
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            _urlopen(request, timeout=5)
        assert exc.value.code == 400
        assert json.loads(exc.value.read())["error"] == "bad_root"
    finally:
        srv.shutdown()
        srv.server_close()
    assert a.list_intents() == []
    assert b.list_intents() == []


def test_api_state_corrupt_root_isolated(tmp_path: Path) -> None:
    a, b = _make_two_stores(tmp_path)
    cfg_path = b.dir / "config.json"
    original = cfg_path.read_text(encoding="utf-8")
    cfg_path.write_text("{not json", encoding="utf-8")
    srv, _t, base = _serve_multi(a, b)
    try:
        roots = _state(base)["roots"]  # still HTTP 200 (FR-005)
        assert roots[0]["errors"] == []
        assert roots[0]["agents"]  # healthy root complete
        assert roots[1]["errors"], "corrupt root must surface errors"
        # degraded roots keep errors-as-data: NO partial fields, incl the
        # 0.19.0 additive stats/edges (Codex pre-code note).
        assert "agents" not in roots[1] and "threads" not in roots[1]
        assert "edges" not in roots[1]
        # recovery without a server restart (research D4)
        cfg_path.write_text(original, encoding="utf-8")
        roots = _state(base)["roots"]
        assert roots[1]["errors"] == []
        assert {ag["name"] for ag in roots[1]["agents"]} == {"lead", "dev"}
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_state_does_not_project_far_future_heartbeat_as_fresh(tmp_path: Path) -> None:
    from datetime import datetime, timezone

    s = _make_store(tmp_path)
    future = datetime.fromtimestamp(time.time() + 60.0, timezone.utc)
    (s.state_dir / "alpha.heartbeat").write_text(
        future.isoformat().replace("+00:00", "Z"), encoding="utf-8",
    )

    root = web.build_state([web.RootDescriptor(store=s, label="root")])["roots"][0]
    alpha = next(agent for agent in root["agents"] if agent["name"] == "alpha")

    assert alpha["last_seen"]
    assert "last_seen_age_seconds" not in alpha


def test_api_state_uninitialized_root_is_error_data(tmp_path: Path) -> None:
    a = Store(tmp_path / "good")
    (tmp_path / "good").mkdir()
    a.init(["alpha", "beta"])
    empty = tmp_path / "nothing-here"
    empty.mkdir()
    srv, _t, base = _serve_multi(a, Store(empty))
    try:
        roots = _state(base)["roots"]
        assert roots[0]["errors"] == []
        assert roots[1]["errors"]
        assert roots[1]["path"] == str(empty.resolve())
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_state_spec_kitty_detection(tmp_path: Path) -> None:
    s = _make_store(tmp_path)
    (tmp_path / "kitty-specs" / "some-mission-01ABC").mkdir(parents=True)
    srv, _t, base = _serve(s)
    try:
        (root,) = _state(base)["roots"]
        assert root["spec_kitty"]["missions"] == ["some-mission-01ABC"]
    finally:
        srv.shutdown()
        srv.server_close()


def test_dashboard_shell_links_console_assets(tmp_path: Path) -> None:
    """0.58.0: /dashboard serves the 3-region console shell that LINKS the
    served console.css/console.js (no inline style/script), under the new
    console CSP. Cross-root message ids remain unresolvable via root[0]."""
    a, b = _make_two_stores(tmp_path)
    b.send(sender="lead", recipient="dev", kind="question",
           subject="b-side", body="y", meta={"request_id": "rid-b"})
    srv, _t, base = _serve_multi(a, b)
    try:
        with _get(f"{base}/dashboard") as resp:
            assert resp.status == 200
            page = resp.read().decode("utf-8")
            assert resp.headers["Content-Security-Policy"] == _DASH_CSP
        # the new shell links the served assets, not an inline blob
        assert "/static/console.css" in page
        assert "/static/console.js" in page
        assert "/static/dashboard.js" not in page  # old asset gone
        # the fixed 3-region skeleton is present, hydrated by console.js
        assert 'id="topbar"' in page
        assert 'id="sidebar"' in page
        assert 'id="main"' in page
        # zero inline style / handlers in the shell (CSP-safe)
        assert "<style" not in page
        assert "onclick=" not in page and "style=" not in page
        # the assets serve with the right content types
        with _get(f"{base}/static/console.js") as resp:
            assert resp.headers["Content-Type"].startswith("application/javascript")
        with _get(f"{base}/static/console.css") as resp:
            assert resp.headers["Content-Type"].startswith("text/css")
        with _get(f"{base}/static/avatars/claude-dev.png") as resp:
            assert resp.headers["Content-Type"].startswith("image/png")
            assert resp.read(8) == b"\x89PNG\r\n\x1a\n"
        with _get(f"{base}/static/avatars/operator.png") as resp:
            assert resp.headers["Content-Type"].startswith("image/png")
            assert resp.read(8) == b"\x89PNG\r\n\x1a\n"
        # the server gives a cross-root client nothing to link to: root[1]'s
        # message ids are NOT resolvable via root[0]'s routes (FR-003).
        mid_b = b._scan_messages()[0][0].id
        with pytest.raises(urllib.error.HTTPError) as exc:
            _urlopen(
                f"{base}/api/messages/{mid_b}", timeout=5)
        assert exc.value.code == 404
        # scope-add v0.59.0: index now serves the console too.
        with _get(f"{base}/") as resp:
            assert "/static/console.js" in resp.read().decode("utf-8")
    finally:
        srv.shutdown()
        srv.server_close()


def test_static_unknown_name_404_and_no_traversal(tmp_path: Path) -> None:
    """The /static/<name> route is an EXACT allowlist lookup — an unknown name
    404s and a traversal attempt cannot escape the two known assets."""
    s = _make_store(tmp_path)
    srv, _t, base = _serve(s)
    try:
        for bad in (
            "nope.js",
            "..%2F..%2Fweb.py",
            "console.css.bak",
            "avatars/nope.png",
            "avatars/..%2Fconsole.js",
            "avatars/operator.png%2F..%2Fconsole.js",
            "avatars/http:%2F%2Fexample.invalid%2Fx.png",
            "..%2Favatars%2Fclaude-dev.png",
            "",
        ):
            with pytest.raises(urllib.error.HTTPError) as exc:
                _urlopen(f"{base}/static/{bad}", timeout=5)
            assert exc.value.code == 404, bad
        # the old dashboard.js name is gone (404), not silently aliased
        with pytest.raises(urllib.error.HTTPError) as exc:
            _urlopen(f"{base}/static/dashboard.js", timeout=5)
        assert exc.value.code == 404
    finally:
        srv.shutdown()
        srv.server_close()


def test_shaped_avatar_assets_are_flat_allowlisted_and_served(tmp_path: Path) -> None:
    paths = avatars.avatar_static_paths()
    assert len(paths) == 71
    assert "avatars/hexagon-architect.png" in paths
    assert "avatars/triangle-security.png" in paths
    assert all(path.startswith("avatars/") for path in paths)
    assert all("/" not in path.removeprefix("avatars/") for path in paths)

    s = _make_store(tmp_path)
    srv, _t, base = _serve(s)
    try:
        with _get(f"{base}/static/avatars/hexagon-architect.png") as resp:
            assert resp.headers["Content-Type"].startswith("image/png")
            assert resp.read(8) == b"\x89PNG\r\n\x1a\n"
        for bad in (
            "avatars/hexagon/architect.png",
            "avatars/..%2Fhexagon-architect.png",
            "avatars/hexagon-architect.png%2F..%2Fconsole.js",
        ):
            with pytest.raises(urllib.error.HTTPError) as exc:
                _urlopen(f"{base}/static/{bad}", timeout=5)
            assert exc.value.code == 404, bad
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_state_avatar_records_and_operator_descriptor(tmp_path: Path) -> None:
    s = Store(tmp_path)
    s.init(["claude-dev", "codex-dev", "plain"])
    s.set_role("claude-dev", "developer")
    s.set_role("codex-dev", "developer")
    s.set_avatar("codex-dev", "claude-rev")
    s.set_avatar("plain", "codex-rev")
    srv, _t, base = _serve(s)
    try:
        (root,) = _state(base)["roots"]
    finally:
        srv.shutdown()
        srv.server_close()

    by_name = {a["name"]: a for a in root["agents"]}
    assert by_name["claude-dev"]["avatar"] == {
        "id": "claude-dev",
        "file": "claude-dev.png",
        "source": "role_default",
        "shape": "",
    }
    assert by_name["plain"]["avatar"] == {
        "id": "codex-rev",
        "file": "codex-rev.png",
        "source": "chosen",
        "shape": "",
    }
    assert by_name["codex-dev"]["avatar"] == {
        "id": "claude-rev",
        "file": "claude-rev.png",
        "source": "chosen",
        "shape": "",
    }
    assert root["operator"] == {
        "principal": avatars.OPERATOR_PRINCIPAL,
        "label": "you",
        "role_label": "operator",
        "avatar": {
            "id": "operator",
            "file": "operator.png",
            "source": "operator_default",
            "shape": "",
        },
    }
    for agent in root["agents"]:
        assert "name" in agent and "health" in agent and "unread" in agent


def test_shaped_avatar_choice_flows_shape_and_originals_stay_circular(
    tmp_path: Path,
) -> None:
    s = Store(tmp_path)
    s.init(["alpha", "claude-dev"])
    s.set_role("claude-dev", "developer")
    s.set_avatar("alpha", "hexagon-architect")
    srv, _t, base = _serve(s)
    try:
        (root,) = _state(base)["roots"]
    finally:
        srv.shutdown()
        srv.server_close()

    by_name = {a["name"]: a for a in root["agents"]}
    assert by_name["alpha"]["avatar"] == {
        "id": "hexagon-architect",
        "file": "hexagon-architect.png",
        "source": "chosen",
        "shape": "hexagon",
    }
    assert by_name["claude-dev"]["avatar"] == {
        "id": "claude-dev",
        "file": "claude-dev.png",
        "source": "role_default",
        "shape": "",
    }
    assert root["operator"]["avatar"]["id"] == "operator"
    assert root["operator"]["avatar"]["shape"] == ""

    original_ids = set(avatars.AVATAR_ASSETS) - set(avatars.AVATAR_SHAPES)
    assert {
        "claude-arch", "claude-dev", "claude-docs", "claude-lead",
        "claude-rev", "codex-dev", "codex-infra", "codex-rev",
        "codex-scout", "codex-test", "operator",
    } <= original_ids
    assert "operator" not in avatars.AVATAR_SHAPES
    assert "claude-dev" not in avatars.AVATAR_SHAPES


def test_shaped_avatar_cross_family_ids_are_distinct() -> None:
    ids = {
        "hexagon-architect",
        "oval-muted-architect",
        "rounded-square-architect",
        "oval-muted-security",
        "oval-vivid-security",
        "triangle-security",
    }
    assert ids <= set(avatars.AVATAR_ASSETS)
    files = {avatars.AVATAR_ASSETS[avatar_id] for avatar_id in ids}
    assert len(files) == len(ids)
    assert {avatars.AVATAR_SHAPES[avatar_id] for avatar_id in ids} == {
        "hexagon", "oval-muted", "rounded-square", "oval-vivid", "triangle",
    }


@pytest.mark.parametrize("bad_value", [
    "../console.js",
    "avatars/claude-dev.png",
    "hexagon/architect.png",
    "claude-dev.png",
    "http://example.invalid/avatar.png",
    "unknown",
])
def test_api_state_bad_stored_avatar_choice_degrades_without_broken_path(
    tmp_path: Path,
    bad_value: str,
) -> None:
    s = Store(tmp_path)
    s.init(["claude-dev", "plain"])
    s.set_role("claude-dev", "developer")
    cfg = s.load_config()
    cfg["avatars"] = {
        "claude-dev": bad_value,
        "plain": bad_value,
        avatars.OPERATOR_PRINCIPAL: bad_value,
    }
    s._write_config(cfg)

    srv, _t, base = _serve(s)
    try:
        (root,) = _state(base)["roots"]
    finally:
        srv.shutdown()
        srv.server_close()

    assert root["errors"] == []
    by_name = {a["name"]: a for a in root["agents"]}
    assert by_name["claude-dev"]["avatar"] == {
        "id": "claude-dev",
        "file": "claude-dev.png",
        "source": "role_default",
        "shape": "",
    }
    assert "avatar" not in by_name["plain"]
    assert root["operator"]["avatar"] == {
        "id": "operator",
        "file": "operator.png",
        "source": "operator_default",
        "shape": "",
    }


def test_csp_split_per_route(tmp_path: Path) -> None:
    """The hostile-body routes AND every JSON feed keep the pre-0.17.0 CSP
    byte-identical; only the console document (/dashboard) gets the
    script+style-capable policy (research D1 / §1). 0.58.0: the console CSP
    drops 'unsafe-inline' for style in favor of 'self'; /api/attention and
    /api/thread/<rid> stay on the strict legacy policy."""
    s = _make_store(tmp_path)
    s.send(sender="alpha", recipient="beta", body="<script>x</script>",
           meta={"request_id": "rid-c"})
    mid = s._scan_messages()[0][0].id
    srv, _t, base = _serve(s)
    try:
        for path in (f"/messages/{mid}", "/api/status", "/api/state",
                     "/api/attention", "/api/learning", "/api/onboarding",
                     "/api/thread/rid-c"):
            with _get(f"{base}{path}") as resp:
                assert resp.headers["Content-Security-Policy"] == _LEGACY_CSP, path
        for path in ("/", "/dashboard"):
            with _get(f"{base}{path}") as resp:
                assert resp.headers["Content-Security-Policy"] == _DASH_CSP
        # the served assets are not documents; they carry the default policy too
        for path in ("/static/console.js", "/static/console.css",
                     "/static/avatars/claude-dev.png"):
            with _get(f"{base}{path}") as resp:
                assert resp.headers["Content-Security-Policy"] == _LEGACY_CSP, path
    finally:
        srv.shutdown()
        srv.server_close()


def test_new_routes_reject_write_methods(tmp_path: Path) -> None:
    s = _make_store(tmp_path)
    s.send(sender="alpha", recipient="beta", body="x",
           meta={"request_id": "rid-w"})
    srv, _t, base = _serve(s)
    try:
        for path in ("/api/state", "/api/threads", "/dashboard", "/api/attention",
                     "/api/learning", "/api/onboarding", "/api/thread/rid-w", "/static/console.js",
                     "/static/console.css", "/static/avatars/claude-dev.png"):
            req = urllib.request.Request(  # noqa: S310  # nosemgrep
                f"{base}{path}", method="POST", data=b"x")
            with pytest.raises(urllib.error.HTTPError) as exc:
                _urlopen(req, timeout=5)
            assert exc.value.code == 405, path
            assert exc.value.headers.get("Allow") == "GET, HEAD"
    finally:
        srv.shutdown()
        srv.server_close()


def test_actions_off_has_no_session_and_post_stays_405(tmp_path: Path) -> None:
    s = _make_store(tmp_path)
    srv, _t, base = _serve(s)
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            _get(f"{base}/api/session")
        assert exc.value.code == 404
        req = urllib.request.Request(  # noqa: S310  # nosemgrep
            f"{base}/api/intent", method="POST", data=b'{"kind":"send"}',
            headers={"Content-Type": "application/json", "Origin": base,
                     "X-CSRF-Token": "looks-valid"})
        with pytest.raises(urllib.error.HTTPError) as exc:
            _urlopen(req, timeout=5)
        assert exc.value.code == 405
        assert exc.value.headers.get("Allow") == "GET, HEAD"
        assert not s.intents_active_dir.exists()
    finally:
        srv.shutdown()
        srv.server_close()


def test_valid_action_post_appends_exactly_one_intent_file(tmp_path: Path) -> None:
    s = _make_store(tmp_path)
    # Keep one-time persistent lock setup outside the endpoint mutation window.
    with s._config_lock():
        pass
    before = _tree_hashes(s.root)
    srv, _t, base = _serve(s, enable_actions=True)
    try:
        token = _session(base)["csrf_token"]
        with _post_intent(base, token, {
            "kind": "send",
            "payload": {"target": "beta", "body": "hello", "message_kind": "message"},
        }) as resp:
            assert resp.status == 202
            accepted = json.loads(resp.read())
        after = _tree_hashes(s.root)
        added = sorted(set(after) - set(before))
        assert len(added) == 1
        added_path = added[0].replace("\\", "/")
        assert added_path.startswith("state/intents/active/")
        assert added_path.endswith(".json")
        rec = s.read_intent(accepted["intent_id"])
        assert rec is not None and rec["state"] == Store.INTENT_QUEUED
    finally:
        srv.shutdown()
        srv.server_close()


def test_valid_answer_escalation_post_appends_payload_only(tmp_path: Path) -> None:
    s = _make_store(tmp_path)
    s.set_operator_facing("beta")
    s.send(sender="alpha", recipient="beta", kind="question",
           subject="operator input needed", body="body",
           meta={"request_id": "esc-help", "needs_operator": "true"})
    srv, _t, base = _serve(s, enable_actions=True)
    try:
        token = _session(base)["csrf_token"]
        with _post_intent(base, token, {
            "kind": "answer_escalation",
            "payload": {"to_request": "esc-help", "body": "use option A"},
        }) as resp:
            assert resp.status == 202
            accepted = json.loads(resp.read())
        rec = s.read_intent(accepted["intent_id"])
        assert rec["kind"] == "answer_escalation"
        assert rec["payload"] == {"to_request": "esc-help", "body": "use option A"}
    finally:
        srv.shutdown()
        srv.server_close()


def test_rejected_action_post_mutates_nothing(tmp_path: Path) -> None:
    s = _make_store(tmp_path)
    before = _tree_hashes(s.root)
    srv, _t, base = _serve(s, enable_actions=True)
    try:
        req = urllib.request.Request(  # noqa: S310  # nosemgrep
            f"{base}/api/intent", method="POST",
            data=json.dumps({"kind": "send"}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Origin": base})
        with pytest.raises(urllib.error.HTTPError) as exc:
            _urlopen(req, timeout=5)
        assert exc.value.code == 403
        problem = json.loads(exc.value.read())
        assert problem["error"] == "bad_csrf"
    finally:
        srv.shutdown()
        srv.server_close()
    assert _tree_hashes(s.root) == before


def test_wrong_present_csrf_token_returns_403_without_file(tmp_path: Path) -> None:
    s = _make_store(tmp_path)
    before = _tree_hashes(s.root)
    srv, _t, base = _serve(s, enable_actions=True)
    try:
        token = _session(base)["csrf_token"]
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post_intent(base, token + "-wrong", {
                "kind": "send",
                "payload": {"target": "beta", "body": "hello"},
            })
        assert exc.value.code == 403
        problem = json.loads(exc.value.read())
        assert problem["error"] == "bad_csrf"
        assert not s.intents_active_dir.exists()
    finally:
        srv.shutdown()
        srv.server_close()
    assert _tree_hashes(s.root) == before


def test_non_ascii_csrf_returns_403_without_rate_or_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(web, "_ACTION_RATE_BURST", 1)
    monkeypatch.setattr(web, "_ACTION_RATE_PER_MINUTE", 0)
    s = _make_store(tmp_path)
    before = _tree_hashes(s.root)
    srv, _t, base = _serve(s, enable_actions=True)
    try:
        token = _session(base)["csrf_token"]
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post_intent(base, "é", {
                "kind": "send",
                "payload": {"target": "beta", "body": "bad token"},
            })
        assert exc.value.code == 403
        problem = json.loads(exc.value.read())
        assert problem["error"] == "bad_csrf"
        assert not s.intents_active_dir.exists()
        assert _tree_hashes(s.root) == before

        with _post_intent(base, token, {
            "kind": "send",
            "payload": {"target": "beta", "body": "valid token"},
        }) as resp:
            assert resp.status == 202
    finally:
        srv.shutdown()
        srv.server_close()

    assert len(s.list_intents()) == 1


def test_action_rate_bucket_rmw_is_locked() -> None:
    src = inspect.getsource(web._make_handler)
    assert "rate_lock = threading.Lock()" in src
    assert "with rate_lock:" in src
    assert src.index("with rate_lock:") < src.index("rate[\"tokens\"] = float(rate[\"tokens\"]) - 1.0")


def test_concurrent_valid_posts_respect_rate_burst(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(web, "_ACTION_RATE_BURST", 2)
    monkeypatch.setattr(web, "_ACTION_RATE_PER_MINUTE", 0)
    s = _make_store(tmp_path)
    srv, _t, base = _serve(s, enable_actions=True)
    try:
        token = _session(base)["csrf_token"]
        barrier = threading.Barrier(8)

        def attempt(i: int) -> int:
            barrier.wait(timeout=5)
            try:
                with _post_intent(base, token, {
                    "kind": "send",
                    "payload": {"target": "beta", "body": f"hello {i}"},
                }) as resp:
                    return resp.status
            except urllib.error.HTTPError as exc:
                return exc.code

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            statuses = list(pool.map(attempt, range(8)))
    finally:
        srv.shutdown()
        srv.server_close()

    assert statuses.count(202) == 2
    assert statuses.count(429) == 6
    assert len(s.list_intents()) == 2


def test_origin_mismatch_returns_403_without_file(tmp_path: Path) -> None:
    s = _make_store(tmp_path)
    before = _tree_hashes(s.root)
    srv, _t, base = _serve(s, enable_actions=True)
    try:
        token = _session(base)["csrf_token"]
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post_intent(base, token, {
                "kind": "send",
                "payload": {"target": "beta", "body": "hello"},
            }, origin="http://127.0.0.1:1")
        assert exc.value.code == 403
        problem = json.loads(exc.value.read())
        assert problem["error"] == "bad_origin"
        assert not s.intents_active_dir.exists()
    finally:
        srv.shutdown()
        srv.server_close()
    assert _tree_hashes(s.root) == before


def test_action_post_rate_limited_without_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(web, "_ACTION_RATE_BURST", 0)
    monkeypatch.setattr(web, "_ACTION_RATE_PER_MINUTE", 0)
    s = _make_store(tmp_path)
    before = _tree_hashes(s.root)
    srv, _t, base = _serve(s, enable_actions=True)
    try:
        token = _session(base)["csrf_token"]
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post_intent(base, token, {
                "kind": "send",
                "payload": {"target": "beta", "body": "hello"},
            })
        assert exc.value.code == 429
        problem = json.loads(exc.value.read())
        assert problem["error"] == "rate_limited"
        assert not s.intents_active_dir.exists()
    finally:
        srv.shutdown()
        srv.server_close()
    assert _tree_hashes(s.root) == before


def test_action_post_body_too_large_without_file(tmp_path: Path) -> None:
    s = _make_store(tmp_path)
    before = _tree_hashes(s.root)
    srv, _t, base = _serve(s, enable_actions=True)
    try:
        token = _session(base)["csrf_token"]
        req = urllib.request.Request(  # noqa: S310  # nosemgrep
            f"{base}/api/intent", method="POST",
            data=b"x" * (web._ACTION_BODY_LIMIT + 1),  # noqa: SLF001
            headers={
                "Content-Type": "application/json",
                "Origin": base,
                "X-CSRF-Token": token,
            })
        with pytest.raises(urllib.error.HTTPError) as exc:
            _urlopen(req, timeout=5)
        assert exc.value.code == 413
        problem = json.loads(exc.value.read())
        assert problem["error"] == "body_too_large"
        assert not s.intents_active_dir.exists()
    finally:
        srv.shutdown()
        srv.server_close()
    assert _tree_hashes(s.root) == before


def test_action_post_intent_cap_without_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Store, "INTENT_MAX_ACTIVE", 0)
    s = _make_store(tmp_path)
    before = _tree_hashes(s.root)
    srv, _t, base = _serve(s, enable_actions=True)
    try:
        token = _session(base)["csrf_token"]
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post_intent(base, token, {
                "kind": "send",
                "payload": {"target": "beta", "body": "hello"},
            })
        assert exc.value.code == 429
        problem = json.loads(exc.value.read())
        assert problem["error"] == "intent_cap"
        assert not s.intents_active_dir.exists()
    finally:
        srv.shutdown()
        srv.server_close()
    assert _tree_hashes(s.root) == before


def test_action_post_ignores_top_level_from_and_drain_uses_web_actor(
    tmp_path: Path,
) -> None:
    s = _make_store(tmp_path)
    s.set_role("alpha", "lead")
    srv, _t, base = _serve(s, enable_actions=True)
    try:
        token = _session(base)["csrf_token"]
        with _post_intent(base, token, {
            "from": "beta",
            "kind": "send",
            "payload": {"target": "beta", "body": "hello"},
        }) as resp:
            assert resp.status == 202
            accepted = json.loads(resp.read())
    finally:
        srv.shutdown()
        srv.server_close()

    summary = intents.drain_intents(s, pid=123, max_per_tick=1)

    assert summary["applied"] == 1
    assert s.read_intent(accepted["intent_id"])["state"] == Store.INTENT_APPLIED
    messages = s.messages_for("beta")
    assert len(messages) == 1
    assert messages[0].sender == "alpha"


def test_reserved_action_payload_returns_400_without_file(tmp_path: Path) -> None:
    s = _make_store(tmp_path)
    before = _tree_hashes(s.root)
    srv, _t, base = _serve(s, enable_actions=True)
    try:
        token = _session(base)["csrf_token"]
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post_intent(base, token, {
                "kind": "send",
                "payload": {"target": "beta", "body": "hello", "meta": {"request_id": "q-x"}},
            })
        assert exc.value.code == 400
        problem = json.loads(exc.value.read())
        assert problem["error"] == "invalid_intent"
    finally:
        srv.shutdown()
        srv.server_close()
    assert _tree_hashes(s.root) == before


def test_kill_switch_blocks_valid_action_post_423(tmp_path: Path) -> None:
    s = _make_store(tmp_path)
    (s.dir / "supervisor.kill").write_text("stop", encoding="utf-8")
    srv, _t, base = _serve(s, enable_actions=True)
    try:
        token = _session(base)["csrf_token"]
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post_intent(base, token, {
                "kind": "send",
                "payload": {"target": "beta", "body": "hello"},
            })
        assert exc.value.code == 423
        assert not s.intents_active_dir.exists()
    finally:
        srv.shutdown()
        srv.server_close()


def test_kill_switch_unreadable_fails_closed_423(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _make_store(tmp_path)
    monkeypatch.setattr(s, "supervisor_kill_switch", lambda: None)
    srv, _t, base = _serve(s, enable_actions=True)
    try:
        token = _session(base)["csrf_token"]
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post_intent(base, token, {
                "kind": "send",
                "payload": {"target": "beta", "body": "hello"},
            })
        assert exc.value.code == 423
        problem = json.loads(exc.value.read())
        assert problem["error"] == "executor_state_unreadable"
        assert not s.intents_active_dir.exists()
    finally:
        srv.shutdown()
        srv.server_close()


def test_action_post_absolute_form_accepts_loopback_alias_same_port(tmp_path: Path) -> None:
    s = _make_store(tmp_path)
    srv, _t, base = _serve(s, enable_actions=True)
    try:
        token = _session(base)["csrf_token"]
        port = srv.server_address[1]
        host = f"localhost:{port}"
        status, _headers, _body = _raw_post(
            base,
            f"http://localhost:{port}/api/intent",
            host=host,
            origin=f"http://{host}",
            token=token,
            payload={
                "kind": "send",
                "payload": {"target": "beta", "body": "hello"},
            },
        )
        assert status == 202
        assert len(s.list_intents()) == 1
    finally:
        srv.shutdown()
        srv.server_close()


def test_action_post_absolute_form_rejects_wrong_port_without_file(tmp_path: Path) -> None:
    s = _make_store(tmp_path)
    srv, _t, base = _serve(s, enable_actions=True)
    try:
        token = _session(base)["csrf_token"]
        port = srv.server_address[1]
        wrong_port = port + 1 if port < 65000 else port - 1
        host = f"localhost:{port}"
        status, _headers, body = _raw_post(
            base,
            f"http://localhost:{wrong_port}/api/intent",
            host=host,
            origin=f"http://{host}",
            token=token,
            payload={
                "kind": "send",
                "payload": {"target": "beta", "body": "hello"},
            },
        )
        assert status == 403
        assert json.loads(body)["error"] == "bad_host"
        assert not s.intents_active_dir.exists()
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_intents_and_preflight_are_read_only_body_free(tmp_path: Path) -> None:
    s = _make_store(tmp_path)
    rec = s.write_intent("send", {"target": "beta", "body": "SECRET", "subject": "S"})
    s.mark_intent_terminal(
        rec["intent_id"], state=Store.INTENT_DENIED,
        code="plan_revalidation_failed",
        error="broadcast/reply semantics drifted; requeue")
    before = _tree_hashes(s.root)
    srv, _t, base = _serve(s, enable_actions=True)
    try:
        with _get(f"{base}/api/intents") as resp:
            payload = json.loads(resp.read())
        assert payload["target_root_index"] == 0
        assert payload["items"][0]["kind"] == "send"
        assert payload["items"][0]["code"] == "plan_revalidation_failed"
        assert "SECRET" not in json.dumps(payload)
        with _get(f"{base}/api/preflight") as resp:
            preflight = json.loads(resp.read())
        assert preflight["target_root_index"] == 0
        assert {c["key"] for c in preflight["checks"]} >= {
            "store_initialized", "operator_actor", "supervisor_scaffolded",
            "supervisor_running", "actions_enabled",
        }
    finally:
        srv.shutdown()
        srv.server_close()
    assert _tree_hashes(s.root) == before


def test_actions_on_intent_route_allows_only_post(tmp_path: Path) -> None:
    s = _make_store(tmp_path)
    srv, _t, base = _serve(s, enable_actions=True)
    try:
        req = urllib.request.Request(  # noqa: S310  # nosemgrep
            f"{base}/api/intent", method="PUT", data=b"{}")
        with pytest.raises(urllib.error.HTTPError) as exc:
            _urlopen(req, timeout=5)
        assert exc.value.code == 405
        assert exc.value.headers.get("Allow") == "POST"
    finally:
        srv.shutdown()
        srv.server_close()


def _tree_hashes(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    base = root / ".agenttalk"
    for p in sorted(base.rglob("*")):
        if p.is_file() and p.name != "config.lock":
            out[str(p.relative_to(base))] = hashlib.sha256(
                p.read_bytes()).hexdigest()
    return out


def test_no_mutation_full_tree_hash(tmp_path: Path) -> None:
    """NFR-001 / research D9: the dashboard is read-only BY REGRESSION,
    not just by code review. Content hashes, never mtimes (Windows)."""
    a, b = _make_two_stores(tmp_path)
    # rich state: messages, cursors, thread closure, composing, heartbeat
    a.send(sender="alpha", recipient="beta", kind="review-request",
           subject="s", body="b", meta={"request_id": "r1"})
    a.send(sender="beta", recipient="alpha", kind="review-result",
           subject="re", body="ok", meta={"request_id": "r1"})
    mid = a._scan_messages()[0][0].id
    a.set_cursor("alpha", mid)
    a.close_thread("alpha", "r1", reason="done")
    a.write_composing_intent("beta", "r2", "alpha")
    a.write_heartbeat("alpha")
    b.send(sender="lead", recipient="dev", body="hi")

    before_a, before_b = _tree_hashes(a.root), _tree_hashes(b.root)
    srv, _t, base = _serve_multi(a, b)
    try:
        # Poll several times so the in-memory health-timeline ring is exercised
        # (§5): it must record samples WITHOUT ever touching disk.
        for _ in range(3):
            _state(base)
        for path in ("/dashboard", "/static/console.js", "/static/console.css",
                     "/", f"/messages/{mid}", "/api/attention", "/api/learning",
                     "/api/onboarding", "/api/thread/r1"):
            with _get(f"{base}{path}") as resp:
                resp.read()
        for bad in (f"{base}/messages/zzz-does-not-exist", f"{base}/nope",
                    f"{base}/api/thread/no-such-thread"):
            with pytest.raises(urllib.error.HTTPError):
                _urlopen(bad, timeout=5)
        req = urllib.request.Request(  # noqa: S310  # nosemgrep
            f"{base}/api/state", method="POST", data=b"x")
        with pytest.raises(urllib.error.HTTPError):
            _urlopen(req, timeout=5)
    finally:
        srv.shutdown()
        srv.server_close()
    assert _tree_hashes(a.root) == before_a
    assert _tree_hashes(b.root) == before_b


def test_api_state_perf_smoke(tmp_path: Path) -> None:
    """NFR-003: one build_state() pass over 1,000 validated messages in
    under 2 s (CI hardware is the reference bound — don't tighten).

    The bound is dominated by the store's one-file-per-message scan
    (identical cost to `status`/`recv` at the same size — profiled:
    derivation is ~5 ms, the rest is file opens). An environment whose
    raw IO floor alone exceeds the budget (e.g. aggressive AV scanning
    tmp dirs at ~8 ms/open) cannot meet the reference assumption for
    ANY command, so the test calibrates and skips there rather than
    failing on machine weather. build_state itself is pinned to a
    single scan per root (research D8) — regression on THAT shows up
    on CI as a >2× jump over the floor."""
    s = _make_store(tmp_path)
    for i in range(500):
        s.send(sender="alpha", recipient="beta", body=f"m{i}",
               meta={"request_id": f"rid-{i % 50}"})
        s.send(sender="beta", recipient="alpha", body=f"r{i}",
               meta={"request_id": f"rid-{i % 50}"})
    # IO floor: one raw pass over the same files (what ANY scan costs)
    msg_dir = s.root / ".agenttalk" / "messages"
    start = time.perf_counter()
    for p in msg_dir.iterdir():
        if p.suffix == ".json":
            p.read_bytes()
    floor = time.perf_counter() - start

    roots = [web.RootDescriptor(store=s, label="perf")]
    start = time.perf_counter()
    state = web.build_state(roots)
    elapsed = time.perf_counter() - start
    assert state["roots"][0]["counts"]["messages"] == 1000
    if floor > 1.0:
        pytest.skip(
            f"raw 1k-file scan alone takes {floor:.2f}s here — this "
            f"environment cannot meet the NFR-003 reference bound for any "
            f"command (build_state measured {elapsed:.2f}s)")
    assert elapsed < 2.0, f"build_state took {elapsed:.2f}s at 1k messages"


# ===================================== 0.18.0 (WP02): retired history parity

def test_retired_history_renders_on_message_routes(tmp_path: Path) -> None:
    """FR-004: a retired identity's historical messages must render on the
    dashboard message routes (known roster), matching the thread panel —
    not vanish, and not be flagged invalid."""
    s = Store(tmp_path)
    s.init(["lead", "beta"])
    s.send(sender="beta", recipient="lead", body="HISTORY_FROM_BETA")
    s.send(sender="lead", recipient="beta", body="reply-to-beta")
    s.retire_agent("beta")
    # _all_messages (powers /api/messages, /messages/<id>, index) now sees them
    bodies = {m.body for m in web._all_messages(s)}
    assert "HISTORY_FROM_BETA" in bodies and "reply-to-beta" in bodies
    # and they are NOT reported invalid
    assert s.list_invalid_messages() == []
    # served over HTTP too
    srv, _t, base = _serve(s)
    try:
        with _get(f"{base}/api/messages") as resp:
            payload = json.loads(resp.read())
        served = {m["body"] for m in payload["messages"]}
        assert "HISTORY_FROM_BETA" in served
    finally:
        srv.shutdown()
        srv.server_close()


# ===================================== 0.19.0 (WP01): dashboard polish

def test_api_state_sent_received(tmp_path: Path) -> None:
    """FR-001: additive per-agent sent/received from the same scan."""
    s = Store(tmp_path)
    s.init(["lead", "dev"])
    s.send(sender="lead", recipient="dev", body="a")
    s.send(sender="lead", recipient="dev", body="b")
    s.send(sender="dev", recipient="lead", body="c")
    srv, _t, base = _serve(s)
    try:
        (root,) = _state(base)["roots"]
        by = {a["name"]: a for a in root["agents"]}
        assert by["lead"]["sent"] == 2 and by["lead"]["received"] == 1
        assert by["dev"]["sent"] == 1 and by["dev"]["received"] == 2
        # always-present integers (0 is data, not absent)
        (tmp_path / "x").mkdir()
        s2 = Store(tmp_path / "x")
        s2.init(["solo"])
        srv2, _t2, base2 = _serve(s2)
        try:
            (r2,) = _state(base2)["roots"]
            assert r2["agents"][0]["sent"] == 0
            assert r2["agents"][0]["received"] == 0
        finally:
            srv2.shutdown()
            srv2.server_close()
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_state_edges_basic(tmp_path: Path) -> None:
    """FR-002: edges are directed pair counts, self-excluded, sorted desc."""
    s = Store(tmp_path)
    s.init(["lead", "dev"])
    s.send(sender="lead", recipient="dev", body="a")
    s.send(sender="lead", recipient="dev", body="b")
    s.send(sender="dev", recipient="lead", body="c")
    # a self-addressed barrier message must NOT appear as an edge
    s.send(sender="lead", recipient="lead", kind="message", subject="bump",
           body="x", meta={"barrier": {"version": 1, "scope": "global",
                                       "type": "epoch-bump"}})
    srv, _t, base = _serve(s)
    try:
        (root,) = _state(base)["roots"]
        assert root["edges"] == [
            {"from": "lead", "to": "dev", "count": 2},
            {"from": "dev", "to": "lead", "count": 1},
        ]
        assert "edges_truncated" not in root  # only a few pairs
        # self-pair excluded
        assert all(not (e["from"] == e["to"]) for e in root["edges"])
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_state_edges_include_broadcast_fanout(tmp_path: Path) -> None:
    """FR-002: broadcast fan-out copies count as traffic (one edge per copy)."""
    s = Store(tmp_path)
    s.init(["lead", "a", "b"])
    bid = "bc-1"
    for member in ("a", "b"):
        s.send(sender="lead", recipient=member, kind="question", subject="q",
               body="q", meta={"request_id": bid, "broadcast_id": bid,
                               "audience": ["a", "b"]})
    srv, _t, base = _serve(s)
    try:
        (root,) = _state(base)["roots"]
        edges = {(e["from"], e["to"]): e["count"] for e in root["edges"]}
        assert edges[("lead", "a")] == 1
        assert edges[("lead", "b")] == 1
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_state_edges_truncation(tmp_path: Path) -> None:
    """FR-003: >50 distinct pairs -> capped + truncation signal."""
    s = Store(tmp_path)
    names = ["hub"] + [f"a{i:02d}" for i in range(60)]
    s.init(names)
    for i in range(60):
        s.send(sender="hub", recipient=f"a{i:02d}", body="x")  # 60 distinct pairs
    srv, _t, base = _serve(s)
    try:
        (root,) = _state(base)["roots"]
        assert len(root["edges"]) == 50
        assert root["edges_truncated"] is True
        assert root["edge_limit"] == 50
        # sorted desc then (from,to) — all counts equal here, so stable order
        assert root["edges"] == sorted(
            root["edges"], key=lambda e: (-e["count"], e["from"], e["to"]))
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_state_additive_keys_no_removal(tmp_path: Path) -> None:
    """NFR-001: schema_version stays 1; no existing key removed/renamed; the
    new keys are additive; no body anywhere."""
    s = _make_store(tmp_path)
    s.send(sender="alpha", recipient="beta", kind="review-request",
           subject="r", body="x", meta={"request_id": "q1"})
    srv, _t, base = _serve(s)
    try:
        state = _state(base)
        assert state["schema_version"] == 1
        (root,) = state["roots"]
        # every pre-0.19.0 root key still present
        for k in ("label", "path", "project_id", "errors", "signing_enforced",
                  "epoch", "counts", "agents", "retired", "threads",
                  "broadcasts", "recent"):
            assert k in root, k
        # additive
        assert "edges" in root
        for a in root["agents"]:
            assert "sent" in a and "received" in a
            assert "name" in a and "unread" in a  # prior keys intact
        _assert_no_body_keys(state)
    finally:
        srv.shutdown()
        srv.server_close()


def test_console_renderer_safety(tmp_path: Path) -> None:
    """0.58.0 / invariant §0.8: the served console.js builds DOM via
    createElement/textContent and NEVER via innerHTML — message bodies and all
    bus-derived strings are untrusted, and textContent under script-src 'self'
    is safe. Read the ASSET AS SERVED (not a code constant), so the security
    property is checked on the exact bytes the browser receives."""
    s = _make_store(tmp_path)
    srv, _t, base = _serve(s)
    try:
        with _get(f"{base}/static/console.js") as resp:
            assert resp.headers["Content-Type"].startswith("application/javascript")
            js = resp.read().decode("utf-8")
    finally:
        srv.shutdown()
        srv.server_close()
    assert "textContent" in js
    assert "addEventListener" in js
    assert "/api/session" in js
    assert "/api/intent" in js
    assert "X-CSRF-Token" in js
    assert "tc-avatar-shaped" in js
    assert "avatarShape(agent)" in js
    assert "file.indexOf('/') !== -1" in js
    assert "file.indexOf('\\\\') !== -1" in js
    assert "innerHTML" not in js
    assert "location.reload" not in js
    assert "eval(" not in js
    assert "csrf_token" not in js.split("localStorage", 1)[0]
    assert "sessionStorage" not in js


def test_console_agent_state_info_uses_fresh_unwrapped_heartbeat(tmp_path: Path) -> None:
    console_js = Path(web.__file__).with_name("web_static") / "console.js"
    src = console_js.read_text(encoding="utf-8")
    marker = "  // ------------------------------------------------------------ loops\n"
    assert marker in src
    src = src.replace(
        marker,
        "  globalThis.__agenttalkConsoleTestHooks = {\n"
        "    stateInfo: stateInfo,\n"
        "    agentStateInfo: agentStateInfo,\n"
        "    freshHeartbeat: freshHeartbeat\n"
        "  };\n\n" + marker,
        1,
    )
    instrumented = tmp_path / "console.instrumented.js"
    instrumented.write_text(src, encoding="utf-8")
    runner = tmp_path / "console-agent-state.js"
    runner.write_text(r"""
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync(process.argv[2], 'utf8');
const ctx = {
  console,
  document: { readyState: 'loading', addEventListener() {} },
  localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
  setInterval() {},
  clearInterval() {},
  fetch() { throw new Error('fetch should not run'); },
  __agenttalkConsoleTestHooks: null,
};
ctx.globalThis = ctx;
ctx.window = ctx;
ctx.__agenttalkConsoleTestHooks = {};
vm.createContext(ctx);
vm.runInContext(source, ctx, { filename: 'console.instrumented.js' });
const hooks = ctx.__agenttalkConsoleTestHooks;

function assert(cond, msg) {
  if (!cond) throw new Error(msg);
}
function check(agent, expected) {
  const got = hooks.agentStateInfo(agent);
  for (const [key, value] of Object.entries(expected)) {
    assert(got[key] === value, `${JSON.stringify(agent)} ${key}: ${got[key]} !== ${value}`);
  }
}

check({ health: { state: 'unknown' }, last_seen_age_seconds: 5 },
  { key: 'unwrapped_live', label: 'Active', color: 'teal', grp: 'work', heartbeatOnly: true });
check({ last_seen_age_seconds: 5 },
  { key: 'unwrapped_live', label: 'Active', color: 'teal', grp: 'work', heartbeatOnly: true });
check({ health: { state: 'unknown' }, last_seen_age_seconds: 5, wrapped: false },
  { key: 'unwrapped_live', label: 'Active', color: 'teal', grp: 'work', heartbeatOnly: true });
check({ health: { state: 'unknown' }, last_seen_age_seconds: 121 },
  { key: 'unknown', label: 'Unknown', color: 'gray', grp: 'unknown' });
check({ health: { state: 'unknown' } },
  { key: 'unknown', label: 'Unknown', color: 'gray', grp: 'unknown' });
check({ health: { state: 'unknown' }, last_seen_age_seconds: 5, wrapped: true },
  { key: 'unknown', label: 'Unknown', color: 'gray', grp: 'unknown' });
check({ health: { state: 'working_turn' }, last_seen_age_seconds: 5, wrapped: true },
  { key: 'working_turn', label: 'Working', color: 'ok', grp: 'work', pulse: true });
check({ health: { state: 'idle_waiting' }, last_seen_age_seconds: 5, wrapped: true },
  { key: 'idle_waiting', label: 'Idle \u00b7 waiting', color: 'warn', grp: 'idle' });
assert(hooks.freshHeartbeat({ last_seen_age_seconds: -1 }) === false, 'negative heartbeat fails');
""", encoding="utf-8")
    subprocess.run(["node", str(runner), str(instrumented)], check=True,
                   capture_output=True, text=True)


def test_console_all_dashboard_views_render_smoke_non_empty_main(tmp_path: Path) -> None:
    if shutil.which("node") is None:
        pytest.skip("node is required for console all-views render smoke test")

    console_js = Path(web.__file__).with_name("web_static") / "console.js"
    src = console_js.read_text(encoding="utf-8")
    marker = "  // ------------------------------------------------------------ loops\n"
    assert marker in src
    src = src.replace(
        marker,
        "  globalThis.__agenttalkConsoleTestHooks = {\n"
        "    state: state,\n"
        "    renderChrome: renderChrome,\n"
        "    renderActiveView: renderActiveView,\n"
        "    setPayloads: function (payloads) {\n"
        "      lastState = payloads.lastState;\n"
        "      attentionData = payloads.attentionData;\n"
        "      leadChatData = payloads.leadChatData;\n"
        "      learningData = payloads.learningData;\n"
        "      onboardingData = payloads.onboardingData;\n"
        "      intentsData = payloads.intentsData;\n"
        "      threadCache = payloads.threadCache;\n"
        "      actionSession.enabled = true;\n"
        "      actionSession.token = 'test-token';\n"
        "      actionSession.pending = false;\n"
        "      actionSession.error = '';\n"
        "    }\n"
        "  };\n\n" + marker,
        1,
    )
    instrumented = tmp_path / "console.instrumented.js"
    instrumented.write_text(src, encoding="utf-8")
    runner = tmp_path / "console-all-views.js"
    runner.write_text(r"""
const fs = require('node:fs');
const vm = require('node:vm');

function makeNode(tag) {
  const node = {
    tagName: String(tag).toUpperCase(),
    children: [],
    parentNode: null,
    firstChild: null,
    attributes: {},
    className: '',
    textContent: '',
    disabled: false,
    id: '',
    value: '',
    rows: 0,
    placeholder: '',
    selectedIndex: -1,
    options: [],
    scrollTop: 0,
    style: {
      setProperty(name, value) { this[name] = String(value); },
    },
    classList: {
      contains(cls) {
        return String(node.className || '').split(/\s+/).includes(cls);
      },
    },
    appendChild(child) {
      this.children.push(child);
      child.parentNode = this;
      this.firstChild = this.children[0] || null;
      if (this.tagName === 'SELECT' && child.tagName === 'OPTION') this.options = this.children;
      return child;
    },
    removeChild(child) {
      const idx = this.children.indexOf(child);
      if (idx !== -1) this.children.splice(idx, 1);
      child.parentNode = null;
      this.firstChild = this.children[0] || null;
      if (this.tagName === 'SELECT') this.options = this.children;
      return child;
    },
    replaceChild(next, prev) {
      const idx = this.children.indexOf(prev);
      if (idx === -1) return this.appendChild(next);
      this.children[idx] = next;
      next.parentNode = this;
      prev.parentNode = null;
      this.firstChild = this.children[0] || null;
      if (this.tagName === 'SELECT') this.options = this.children;
      return prev;
    },
    setAttribute(name, value) {
      this.attributes[name] = String(value);
      if (name === 'class') this.className = String(value);
      if (name === 'id') this.id = String(value);
    },
    getAttribute(name) {
      return Object.prototype.hasOwnProperty.call(this.attributes, name)
        ? this.attributes[name]
        : null;
    },
    addEventListener() {},
    querySelector(selector) {
      if (selector[0] === '.') return findByClass(this, selector.slice(1));
      if (selector[0] === '#') return findById(this, selector.slice(1));
      return null;
    },
    querySelectorAll(selector) {
      const found = [];
      collectMatches(this, selector, found);
      return found;
    },
    closest(selector) {
      let cur = this;
      const classes = selector.split(',').map((s) => s.trim()).filter((s) => s[0] === '.')
        .map((s) => s.slice(1));
      while (cur) {
        if (classes.some((cls) => hasClass(cur, cls))) return cur;
        cur = cur.parentNode;
      }
      return null;
    },
  };
  return node;
}

function hasClass(node, cls) {
  return String(node.className || '').split(/\s+/).includes(cls);
}

function findById(node, id) {
  if (node.id === id || node.attributes.id === id) return node;
  for (const child of node.children || []) {
    const got = findById(child, id);
    if (got) return got;
  }
  return null;
}

function findByClass(node, cls) {
  if (hasClass(node, cls)) return node;
  for (const child of node.children || []) {
    const got = findByClass(child, cls);
    if (got) return got;
  }
  return null;
}

function collectMatches(node, selector, found) {
  if (selector[0] === '.' && hasClass(node, selector.slice(1))) found.push(node);
  if (selector[0] === '#' && (node.id === selector.slice(1) || node.attributes.id === selector.slice(1))) {
    found.push(node);
  }
  for (const child of node.children || []) collectMatches(child, selector, found);
}

function collectText(node) {
  let out = node.textContent || '';
  for (const child of node.children || []) out += collectText(child);
  return out;
}

function assert(cond, msg) {
  if (!cond) throw new Error(msg);
}

const source = fs.readFileSync(process.argv[2], 'utf8');
const app = makeNode('div');
app.setAttribute('id', 'app');
const topbar = makeNode('header');
topbar.setAttribute('id', 'topbar');
const sidebar = makeNode('aside');
sidebar.setAttribute('id', 'sidebar');
const main = makeNode('main');
main.setAttribute('id', 'main');
const document = {
  body: makeNode('body'),
  title: '',
  activeElement: null,
  readyState: 'loading',
  createElement: makeNode,
  createElementNS(_ns, tag) { return makeNode(tag); },
  addEventListener() {},
  getElementById(id) { return findById(this.body, id); },
  querySelector(selector) {
    if (selector === '#topbar .tc-clock') {
      const bar = this.getElementById('topbar');
      return bar ? findByClass(bar, 'tc-clock') : null;
    }
    return this.body.querySelector(selector);
  },
  querySelectorAll(selector) { return this.body.querySelectorAll(selector); },
};
document.body.appendChild(app);
app.appendChild(topbar);
app.appendChild(sidebar);
app.appendChild(main);
const ctx = {
  console,
  document,
  localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
  setInterval() {},
  clearInterval() {},
  fetch() { throw new Error('fetch should not run'); },
  __agenttalkConsoleTestHooks: null,
};
ctx.globalThis = ctx;
ctx.window = ctx;
ctx.__agenttalkConsoleTestHooks = {};
vm.createContext(ctx);
vm.runInContext(source, ctx, { filename: 'console.instrumented.js' });
const hooks = ctx.__agenttalkConsoleTestHooks;

const now = Date.now();
const iso = new Date(now - 30_000).toISOString();
const root = {
  label: 'demo-root',
  path: 'D:\\work\\demo-root',
  project_id: 'project-demo-id',
  operator: { principal: 'operator', label: 'Operator', role_label: 'operator' },
  spec_kitty: { missions: ['smoke-mission'] },
  counts: { closed_threads: 1 },
  agents: [
    {
      name: 'codex-test',
      cli: 'codex',
      role: 'tester',
      groups: ['qa'],
      model: 'sonnet',
      reasoning_effort: 'high',
      task: 'Smoke every dashboard view',
      wrapped: true,
      restartable: true,
      health: { state: 'working_turn' },
      last_seen: iso,
      last_seen_age_seconds: 5,
      sent: 4,
      received: 6,
      rate_used_pct: 22,
      context_used_pct: 41,
      capacity: {
        confidence: 'fresh',
        primary: { label: '5h', used_pct: 22, reset_in_seconds: 1800 },
        secondary: { label: 'weekly', used_pct: 33, reset_in_seconds: 7200 },
        context: { used_pct: 41, tokens: 41000, window_size: 100000 },
      },
      health_timeline: [
        { state: 'working_turn', seconds: 900 },
        { state: 'idle_waiting', seconds: 300 },
      ],
      owned_domains: [{ name: 'dashboard', globs: ['src/agenttalk/web_static/*'] }],
    },
    {
      name: 'claude-lead',
      cli: 'claude',
      role: 'lead',
      task: 'Route QA work',
      wrapped: true,
      health: { state: 'idle_waiting' },
      last_seen: iso,
      last_seen_age_seconds: 12,
      sent: 7,
      received: 5,
      capacity: { confidence: 'unknown', reason: 'not reported' },
    },
    {
      name: 'stuck-agent',
      cli: 'claude',
      role: 'developer',
      task: 'Needs restart',
      wrapped: true,
      restartable: true,
      health: { state: 'stuck_suspected' },
      last_seen: iso,
      last_seen_age_seconds: 900,
      sent: 1,
      received: 2,
      capacity: { confidence: 'stale' },
    },
  ],
  edges: [
    { from: 'claude-lead', to: 'codex-test', count: 4 },
    { from: 'codex-test', to: 'stuck-agent', count: 2 },
  ],
  threads: [{
    request_id: 'rid-qa',
    opener: 'claude-lead',
    opener_peer: 'codex-test',
    opener_kind: 'question',
    subject: 'All views QA task',
    next_owner: 'codex-test',
    active_review: true,
    mission: 'smoke-mission',
    wp_id: 'WP-QA',
    ts: iso,
    age_seconds: 30,
    verdict: 'accepted',
  }],
  recent: [
    {
      id: 'm2', ts: iso, age_seconds: 20,
      from: 'codex-test', to: 'claude-lead', kind: 'message',
      subject: 'PASS evidence',
    },
    {
      id: 'm1', ts: iso, age_seconds: 40,
      from: 'claude-lead', to: 'codex-test', kind: 'question',
      subject: 'All views QA task',
    },
  ],
};

const payloads = {
  lastState: { roots: [root], _fetchedAt: now },
  attentionData: {
    count: 5,
    items: [
      {
        source: 'escalation',
        source_label: 'ESCALATION',
        severity: 'high',
        title: 'Operator decision needed',
        detail: 'Choose release path',
        prompt_excerpt: 'Can I publish v0.72.1 now?',
        agent: 'claude-lead',
        request_id: 'esc-1',
        answerable: true,
        ts: iso,
        age_seconds: 90,
      },
      {
        source: 'stuck',
        source_label: 'STUCK',
        severity: 'med',
        title: 'stuck-agent may need restart',
        detail: 'No progress heartbeat',
        agent: 'stuck-agent',
        ts: iso,
        age_seconds: 900,
      },
      {
        source: 'supervisor',
        source_label: 'SUPERVISOR HOLD',
        severity: 'high',
        title: 'supervisor process-tree HOLD: codex-test',
        detail: 'Automatic teardown is HOLD because the tree is truncated.',
        recommendation: 'no scripted remedy applies in this state.',
        configured_launch: {
          source: 'supervisor.json',
          mode: 'detached',
          argv: ['C:\\Python\\python.exe', '-m', 'agenttalk', 'wrap', '--for', 'codex-test'],
          cwd: 'D:\\work\\demo-root',
        },
        restart_request: {
          request_id: 'rr-progressing',
          state: 'applied_pending_readiness',
          pending_progress: true,
        },
        agent: 'codex-test',
        ts: iso,
        age_seconds: 5,
      },
      {
        source: 'supervisor',
        source_label: 'SUPERVISOR HOLD',
        severity: 'high',
        title: 'supervisor process-tree HOLD: beta',
        detail: 'Automatic teardown is HOLD because the tree is invalid.',
        recommendation: (
          'The identity-accounting warning fills the bounded recommendation. ' + 'x'.repeat(300)
        ).slice(0, 300),
        configured_launch_unavailable: 'the agent has no supervisor.json launch entry',
        restart_request: {
          request_id: null,
          state: 'blocked_by_process_tree_hold',
          pending_progress: false,
          unavailable: true,
        },
        agent: 'beta',
        ts: iso,
        age_seconds: 4,
      },
      {
        source: 'supervisor',
        source_label: 'SUPERVISOR HOLD',
        severity: 'high',
        title: 'supervisor process-tree HOLD: gamma',
        recommendation: (
          'A restart request is blocked by this refusal and is not pending progress.'
        ),
        restart_request: {
          request_id: 'rr-blocked',
          state: 'blocked_by_process_tree_hold',
          pending_progress: false,
        },
        agent: 'gamma',
        ts: iso,
        age_seconds: 3,
      },
    ],
  },
  leadChatData: {
    status: 'live',
    available: true,
    operator: 'operator',
    lead: 'claude-lead',
    request_id: 'lead-chat-rid',
    messages: [
      { from: 'operator', kind: 'message', body: 'please gate after QA', ts: iso, age_seconds: 55 },
      { from: 'claude-lead', kind: 'message', body: 'route received', ts: iso, age_seconds: 45 },
    ],
    pending_decisions: [{
      request_id: 'esc-1',
      sender: 'codex-test',
      decision: 'Need operator choice',
      recommendation: 'hold until QA evidence lands',
      options: ['hold', 'go'],
      priority: 'high',
      ts: iso,
      age_seconds: 75,
    }],
    liveness: { state: 'idle_waiting', reason: 'listening' },
  },
  learningData: {
    root: 'demo-root',
    counts: { total: 1, active: 1, proposed: 0, accepted: 1, retired: 0, review_due: 0, stale: 0, exposures: 2 },
    lessons: [{
      domain_id: 'process',
      key: 'review.final-sha',
      note_id: 'kn-lesson',
      scope: 'review',
      status: 'accepted',
      active: true,
      trigger: 'Review the final SHA',
      body: 'Always review the final candidate, not an earlier draft.',
      author: 'codex-test',
      owner: 'codex-test',
      curator: 'claude-lead',
      evidence_ref: 'rq-final-review',
      applies_to: ['review'],
      exposure: {
        count: 2,
        agents: [{ agent: 'codex-test', count: 2 }],
        last_request_id: 'rid-qa',
        last_context_scope: 'review',
      },
    }],
    recent_exposures: [{
      domain_id: 'process',
      key: 'review.final-sha',
      agent: 'codex-test',
      request_id: 'rid-qa',
      context_scope: 'review',
      evidence_ref: 'rq-final-review',
      exposed_at: iso,
    }],
    problems: { knowledge: [], exposures: [] },
    note: 'Exposure means surfaced to an agent turn, not proven application.',
  },
  onboardingData: {
    root: 'demo-root',
    counts: {
      total: 1,
      showing: 1,
      active: 1,
      blocked: 1,
      segments: 1,
      accepted_segments: 1,
      claims: 1,
      confirmed_claims: 1,
      conflicted_claims: 0,
      needs_human_claims: 0,
      open_drift: 1,
      open_unknowns: 1,
      blocking_unknowns: 1,
      blocking_records: 1,
      human_needed: 1,
      invalid_lines: 0,
      truncated: 0,
    },
    runs: [{
      id: 'ob-api',
      title: 'API onboarding',
      objective: 'Map the API before implementation starts.',
      base_ref: 'main',
      lead: 'claude-lead',
      state: 'scanning',
      active: true,
      blocked: true,
      updated_at: iso,
      counts: {
        segments: 1,
        accepted_segments: 1,
        claims: 1,
        confirmed_claims: 1,
        needs_human_claims: 0,
        open_drift: 1,
        open_unknowns: 1,
        blocking_unknowns: 1,
        blocking_records: 1,
        human_needed: 1,
      },
      records: {
        segment: [{
          kind: 'segment',
          key: 'cli',
          status: 'accepted',
          summary: 'CLI parser and README command reference mapped.',
          actor: 'codex-test',
          owner: 'codex-test',
          paths: ['src/agenttalk/cli.py', 'README.md'],
        }],
        claim: [{
          kind: 'claim',
          key: 'cli.parser.source',
          status: 'confirmed',
          summary: 'Parser is the command surface authority.',
          actor: 'codex-test',
          source: 'code',
          confidence: 'high',
        }],
        drift: [{
          kind: 'drift',
          key: 'docs.cli.reference',
          status: 'open',
          summary: 'README command table may lag parser help.',
          segment: 'cli',
          source: 'docs',
          confidence: 'medium',
        }],
        unknown: [{
          kind: 'unknown',
          key: 'release.owner',
          status: 'open',
          summary: 'Need operator confirmation of the release owner.',
          blocking: true,
        }],
      },
      problems: [],
    }],
    problems: [],
    note: 'Onboarding records pointer evidence.',
  },
  intentsData: {
    target_root_label: 'demo-root',
    items: [{ intent_id: 'intent-1', kind: 'send', state: 'queued', code: 'queued' }],
  },
  threadCache: {
    'project-demo-id|rid-qa': {
      request_id: 'rid-qa',
      subject: 'All views QA task',
      participants: ['claude-lead', 'codex-test'],
      messages: [
        { id: 't1', from: 'claude-lead', kind: 'question', body: 'smoke every view', ts: iso, age_seconds: 30 },
        { id: 't2', from: 'codex-test', kind: 'message', body: 'PASS evidence', ts: iso, age_seconds: 10 },
      ],
    },
  },
};

const cases = [
  { view: 'overview', expected: ['Who', 'Health attention', 'need a human', 'codex-test'] },
  { view: 'flow', expected: ['talking to whom', 'Active threads', 'All views QA task'] },
  {
    view: 'attention',
    expected: [
      'Human attention queue',
      'Operator decision needed',
      'Can I publish v0.72.1 now?',
      'stuck-agent',
      'no scripted remedy applies in this state.',
      'Configured detached launch',
      'C:\\\\Python\\\\python.exe',
      'D:\\work\\demo-root',
      'Configured detached launch unavailable: the agent has no supervisor.json launch entry',
      'A restart request is blocked by this refusal and is not pending progress.',
    ],
  },
  { view: 'lead-chat', expected: ['Lead chat', 'Direct channel', 'route received'] },
  {
    view: 'learning',
    expected: [
      'Learning',
      'Accepted lessons',
      'Always review the final candidate',
      'surfaced, not proven applied',
    ],
  },
  {
    view: 'onboarding',
    expected: [
      'Onboarding',
      'Analysis runs',
      'API onboarding',
      'README command table may lag parser help',
      'Human-needed blockers',
    ],
  },
  { view: 'sessions', sessionRid: 'rid-qa', expected: ['Sessions', 'All views QA task', 'smoke every view'] },
  { view: 'agent', selectedAgent: 'stuck-agent', expected: ['stuck-agent', 'Restart with context', 'Supervisor'] },
  {
    view: 'agent',
    selectedAgent: 'codex-test',
    expected: ['codex-test', 'Supervisor', 'Skill', 'tester', 'sonnet', 'high'],
  },
];

for (const tc of cases) {
  hooks.setPayloads(payloads);
  hooks.state.view = tc.view;
  hooks.state.selectedRootId = root.project_id;
  hooks.state.selectedAgent = tc.selectedAgent || null;
  hooks.state.sessionRid = tc.sessionRid || null;
  hooks.state.filter = 'all';
  main.children = [];
  main.firstChild = null;
  main.textContent = '';
  hooks.renderChrome();
  hooks.renderActiveView();
  const text = collectText(main).replace(/\s+/g, ' ').trim();
  const topbarText = collectText(topbar).replace(/\s+/g, ' ').trim();
  const pathNode = findByClass(topbar, 'tc-project-path');
  assert(main.children.length > 0, `${tc.view} did not append content`);
  assert(text.length > 0, `${tc.view} rendered an empty main pane`);
  assert(topbarText.includes('demo-root'), `${tc.view} missing project label`);
  assert(topbarText.includes('D:\\work\\demo-root'), `${tc.view} missing project path`);
  assert(pathNode && pathNode.getAttribute('title') === 'D:\\work\\demo-root',
    `${tc.view} missing full accessible project path`);
  assert(document.title.includes('demo-root'), `${tc.view} title missing project`);
  assert(document.title.toLowerCase().includes(tc.view === 'lead-chat' ? 'lead chat' : tc.view),
    `${tc.view} title missing view: ${document.title}`);
  for (const expected of tc.expected) {
    assert(text.includes(expected), `${tc.view} missing expected text: ${expected}\nrendered: ${text}`);
  }
  if (tc.view === 'attention') {
    const restartLines = main.querySelectorAll('.tc-attn-restart');
    assert(restartLines.length === 1,
      `attention: expected exactly one blocked restart line, got ${restartLines.length}`);
  }
  if (tc.view === 'overview') {
    // v0.75.1: the runtime-identity line renders ONLY for agents with a model.
    // codex-test has model 'sonnet' + effort 'high'; claude-lead / stuck-agent
    // have neither, so the row is OMITTED for them (absent-not-null, no crash).
    const runtimeLines = main.querySelectorAll('.tc-agent-runtime');
    assert(runtimeLines.length === 1,
      `overview: expected exactly 1 runtime line (only codex-test has a model), got ${runtimeLines.length}`);
    const rtText = collectText(runtimeLines[0]).replace(/\s+/g, ' ').trim();
    assert(rtText.includes('Codex') && rtText.includes('Sonnet'),
      `overview: runtime line missing prettified 'Codex ... Sonnet': ${rtText}`);
    assert(rtText.includes('high'),
      `overview: runtime line missing effort 'high': ${rtText}`);
  }
  if (tc.view === 'agent' && tc.selectedAgent === 'codex-test') {
    // v0.75.1: read-only Skill row (role) alongside the v0.75.0 Model row.
    // supRow builds spans only (no input controls) -> the card stays read-only.
    const keys = main.querySelectorAll('.tc-sup-key').map((n) => collectText(n).trim());
    assert(keys.includes('Skill'),
      `detail: Supervisor missing read-only Skill row (keys: ${keys.join(', ')})`);
    assert(keys.includes('Model'),
      `detail: Supervisor missing Model row (keys: ${keys.join(', ')})`);
  }
}

// v0.76.0 (codex P1, r4): an error-as-data /api/attention 200 (200 body with a non-empty
// errors[] and count=0/items=[]) is a COLLECTION FAILURE, not a confirmed-empty queue. The
// attention view must say so — never "All clear" — and the topbar health verdict must not
// paint a green "Healthy / nothing needs you".
hooks.setPayloads(Object.assign({}, payloads, {
  attentionData: { root: root.project_id, count: 0, items: [], errors: ['attention collection failed'] },
}));
hooks.state.view = 'attention';
hooks.state.selectedRootId = root.project_id;
hooks.state.selectedAgent = null;
hooks.state.sessionRid = null;
hooks.state.filter = 'all';
main.children = [];
main.firstChild = null;
main.textContent = '';
hooks.renderChrome();
hooks.renderActiveView();
{
  const atext = collectText(main).replace(/\s+/g, ' ').trim();
  assert(/Can.?t read the human queue/.test(atext),
    `attention-error: missing collection-failure message: ${atext}`);
  assert(!atext.includes('All clear'),
    `attention-error: must NOT claim "All clear" when the queue failed to build: ${atext}`);
  const tb = collectText(topbar).replace(/\s+/g, ' ').trim();
  assert(!tb.includes('Healthy'), `attention-error: topbar must not read "Healthy": ${tb}`);
  assert(!/nothing needs you/.test(tb), `attention-error: topbar must not read all-clear: ${tb}`);
}

// v0.76.0 (codex P1, r5): a stale (aged-out) zero-agent state must NOT paint a CONFIRMED
// "No agents running yet" in the OVERVIEW GRID — that empty state is a second render path
// the top-bar verdict fix didn't cover. Grid + topbar must both read "Connecting…".
{
  const zeroRoot = Object.assign({}, root, { agents: [], threads: [], recent: [] });
  hooks.setPayloads(Object.assign({}, payloads, {
    lastState: { roots: [zeroRoot], _fetchedAt: 0 },                 // aged-out => stale
    attentionData: { root: zeroRoot.project_id, count: 0, items: [], _fetchedAt: 600000 },  // fresh empty
  }));
  hooks.state.now = 600000;   // 600s >> STATE_STALE_MS (4*POLL_MS) => state feed is stale
  hooks.state.view = 'overview';
  hooks.state.selectedRootId = zeroRoot.project_id;
  hooks.state.selectedAgent = null;
  hooks.state.sessionRid = null;
  hooks.state.filter = 'all';
  main.children = [];
  main.firstChild = null;
  main.textContent = '';
  hooks.renderChrome();
  hooks.renderActiveView();
  const otext = collectText(main).replace(/\s+/g, ' ').trim();
  assert(!otext.includes('No agents running yet'),
    `stale-zero overview: grid must NOT claim "No agents running yet" when state is stale: ${otext}`);
  assert(/Connecting/.test(otext),
    `stale-zero overview: grid should show a connecting/unavailable state: ${otext}`);
  const tb2 = collectText(topbar).replace(/\s+/g, ' ').trim();
  assert(!tb2.includes('Healthy'), `stale-zero overview: topbar must not read "Healthy": ${tb2}`);
  assert(/Connecting/.test(tb2), `stale-zero overview: topbar should read Connecting: ${tb2}`);
}

// v0.76.0 (trust contract): a STALE-but-empty /api/attention payload (no errors, aged-out
// _fetchedAt) must NOT render the green "All clear" — the queue view must say it's out of
// date, mirroring the top-bar staleness gate.
{
  hooks.setPayloads(Object.assign({}, payloads, {
    attentionData: { root: root.project_id, count: 0, items: [], _fetchedAt: 0 },
  }));
  hooks.state.now = 600000;   // aged-out beyond ATTENTION_STALE_MS (4*POLL_MS)
  hooks.state.view = 'attention';
  hooks.state.selectedRootId = root.project_id;
  hooks.state.selectedAgent = null;
  hooks.state.sessionRid = null;
  hooks.state.filter = 'all';
  main.children = [];
  main.firstChild = null;
  main.textContent = '';
  hooks.renderChrome();
  hooks.renderActiveView();
  const stext = collectText(main).replace(/\s+/g, ' ').trim();
  assert(!stext.includes('All clear'),
    `stale-attention: must NOT claim "All clear" from a stale payload: ${stext}`);
  assert(/out of date|stale/i.test(stext),
    `stale-attention: should say the queue status is out of date: ${stext}`);
}

// v0.76.0 (codex r5b P1): a stale NON-EMPTY attention payload preserves its last-known cards
// but must QUALIFY them (banner + "N open · stale"), never present an unqualified current
// "N open" while the topbar says the feed is uncertain.
{
  hooks.setPayloads(Object.assign({}, payloads, {
    attentionData: Object.assign({}, payloads.attentionData, { _fetchedAt: 0 }),  // stale, 2 items, no errors
  }));
  hooks.state.now = 600000;   // aged-out beyond ATTENTION_STALE_MS
  hooks.state.view = 'attention';
  hooks.state.selectedRootId = root.project_id;
  hooks.state.selectedAgent = null;
  hooks.state.sessionRid = null;
  hooks.state.filter = 'all';
  main.children = [];
  main.firstChild = null;
  main.textContent = '';
  hooks.renderChrome();
  hooks.renderActiveView();
  const nstext = collectText(main).replace(/\s+/g, ' ').trim();
  assert(/last-known|out of date|Reconnecting/i.test(nstext),
    `stale-nonempty-attention: must show a stale/last-known banner: ${nstext}`);
  assert(nstext.includes('stale'),
    `stale-nonempty-attention: header must qualify the count as stale (not a bare "N open"): ${nstext}`);
  assert(nstext.includes('Operator decision needed'),
    `stale-nonempty-attention: last-known cards must still be preserved: ${nstext}`);
  const tb3 = collectText(topbar).replace(/\s+/g, ' ').trim();
  assert(!tb3.includes('Healthy') && !/nothing needs you/.test(tb3),
    `stale-nonempty-attention: topbar must not read all-clear: ${tb3}`);
}
""", encoding="utf-8")
    subprocess.run(["node", str(runner), str(instrumented)], check=True,
                   capture_output=True, text=True)


def test_console_project_switch_uses_ids_resets_context_and_drops_stale_data(
    tmp_path: Path,
) -> None:
    if shutil.which("node") is None:
        pytest.skip("node is required for console project-switch test")

    console_js = Path(web.__file__).with_name("web_static") / "console.js"
    src = console_js.read_text(encoding="utf-8")
    marker = "  // ------------------------------------------------------------ loops\n"
    assert marker in src
    src = src.replace(
        marker,
        "  var testQueuedCallbacks = 0;\n"
        "  globalThis.__agenttalkConsoleTestHooks = {\n"
        "    setState: function (payload, projectId) {\n"
        "      lastState = payload;\n"
        "      state.selectedRootId = projectId;\n"
        "    },\n"
        "    setDrillIns: function () {\n"
        "      state.view = 'agent';\n"
        "      state.selectedAgent = 'alpha';\n"
        "      state.sessionRid = 'rid-old';\n"
        "      threadCache = {'project-a|rid-old': {request_id: 'rid-old'}};\n"
        "      attentionData = {root_info: {project_id: 'project-a'}, count: 7};\n"
        "    },\n"
        "    setDrafts: function () {\n"
        "      composerState.mode = 'broadcast';\n"
        "      composerState.target = 'alpha';\n"
        "      composerState.audienceKind = 'group';\n"
        "      composerState.audienceValue = 'project-a-reviewers';\n"
        "      composerState.kind = 'question';\n"
        "      composerState.subject = 'project A subject';\n"
        "      composerState.body = 'project A body';\n"
        "      answerComposerState = {'rid-shared': 'project A answer'};\n"
        "      leadChatComposerState.body = 'project A lead chat';\n"
        "    },\n"
        "    applyProjectSelection: applyProjectSelection,\n"
        "    reconcileProjectSelection: reconcileProjectSelection,\n"
        "    fetchRootPayloads: fetchRootPayloads,\n"
        "    fetchAttention: fetchAttention,\n"
        "    enableActions: function () {\n"
        "      actionSession.enabled = true;\n"
        "      actionSession.token = 'test-token';\n"
        "      actionSession.pending = false;\n"
        "      actionSession.error = '';\n"
        "    },\n"
        "    postTestIntent: function () {\n"
        "      postIntent({kind: 'send', payload: {target: 'beta', body: 'captured A'}},\n"
        "        false, function () { testQueuedCallbacks += 1; });\n"
        "    },\n"
        "    rootUrl: rootUrl,\n"
        "    snapshot: function () {\n"
        "      return {\n"
        "        projectId: state.selectedRootId,\n"
        "        view: state.view,\n"
        "        selectedAgent: state.selectedAgent,\n"
        "        sessionRid: state.sessionRid,\n"
        "        threadKeys: Object.keys(threadCache),\n"
        "        attention: attentionData,\n"
        "        actionPending: actionSession.pending,\n"
        "        actionError: actionSession.error,\n"
        "        queuedCallbacks: testQueuedCallbacks,\n"
        "        composer: {\n"
        "          mode: composerState.mode,\n"
        "          target: composerState.target,\n"
        "          audienceKind: composerState.audienceKind,\n"
        "          audienceValue: composerState.audienceValue,\n"
        "          kind: composerState.kind,\n"
        "          subject: composerState.subject,\n"
        "          body: composerState.body,\n"
        "        },\n"
        "        answerKeys: Object.keys(answerComposerState).sort(),\n"
        "        sharedAnswer: answerComposerState['rid-shared'] || '',\n"
        "        leadChatBody: leadChatComposerState.body,\n"
        "      };\n"
        "    }\n"
        "  };\n\n" + marker,
        1,
    )
    instrumented = tmp_path / "console-project.instrumented.js"
    instrumented.write_text(src, encoding="utf-8")
    runner = tmp_path / "console-project-switch.js"
    runner.write_text(r"""
const fs = require('node:fs');
const vm = require('node:vm');

function assert(cond, msg) {
  if (!cond) throw new Error(msg);
}

const calls = [];
function deferredFetch(url) {
  return new Promise((resolve) => calls.push({ url: String(url), resolve }));
}
function response(ok, data) {
  return { ok, status: ok ? 200 : 404, json() { return Promise.resolve(data || {}); } };
}
async function flush() {
  for (let i = 0; i < 8; i += 1) await Promise.resolve();
}
function assertProjectADrafts(snapshot, context) {
  assert(JSON.stringify(snapshot.composer) === JSON.stringify({
    mode: 'broadcast',
    target: 'alpha',
    audienceKind: 'group',
    audienceValue: 'project-a-reviewers',
    kind: 'question',
    subject: 'project A subject',
    body: 'project A body',
  }), `${context} changed generic draft: ${JSON.stringify(snapshot.composer)}`);
  assert(JSON.stringify(snapshot.answerKeys) === JSON.stringify(['rid-shared']) &&
    snapshot.sharedAnswer === 'project A answer',
    `${context} changed answer draft: ${JSON.stringify(snapshot)}`);
  assert(snapshot.leadChatBody === 'project A lead chat',
    `${context} changed lead-chat draft: ${JSON.stringify(snapshot)}`);
}
function assertDraftsCleared(snapshot, context) {
  assert(JSON.stringify(snapshot.composer) === JSON.stringify({
    mode: 'send',
    target: '',
    audienceKind: 'all',
    audienceValue: '',
    kind: 'message',
    subject: '',
    body: '',
  }), `${context} retained generic draft: ${JSON.stringify(snapshot.composer)}`);
  assert(snapshot.answerKeys.length === 0 && snapshot.sharedAnswer === '',
    `${context} retained answer draft: ${JSON.stringify(snapshot)}`);
  assert(snapshot.leadChatBody === '',
    `${context} retained lead-chat draft: ${JSON.stringify(snapshot)}`);
}

const historyValues = [];
const document = {
  readyState: 'loading',
  title: '',
  addEventListener() {},
  getElementById() { return null; },
  querySelector() { return null; },
  querySelectorAll() { return []; },
  createElement() { return {}; },
  createElementNS() { return {}; },
};
const ctx = {
  console,
  document,
  location: { pathname: '/dashboard', search: '', hash: '' },
  history: { replaceState(_state, _title, value) { historyValues.push(String(value)); } },
  URL,
  URLSearchParams,
  localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
  setInterval() {},
  clearInterval() {},
  fetch: deferredFetch,
  __agenttalkConsoleTestHooks: null,
};
ctx.globalThis = ctx;
ctx.window = ctx;
ctx.__agenttalkConsoleTestHooks = {};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), ctx);
const hooks = ctx.__agenttalkConsoleTestHooks;

(async () => {
  const rootA = {
    label: 'project [aaaa1111]', path: 'D:\\one\\project',
    project_id: 'project-a', agents: [{name: 'alpha'}],
  };
  const rootB = {
    label: 'project [bbbb2222]', path: 'D:\\two\\project',
    project_id: 'project-b', agents: [{name: 'alpha'}],
  };
  hooks.setState({ roots: [rootA, rootB] }, rootA.project_id);
  hooks.setDrillIns();
  hooks.setDrafts();

  hooks.setState({ roots: [rootA, rootB], generated_at: 'same-project-poll' }, rootA.project_id);
  assert(!hooks.reconcileProjectSelection(), 'same-project poll changed the project');
  let snapshot = hooks.snapshot();
  assertProjectADrafts(snapshot, 'same-project poll');

  hooks.enableActions();
  hooks.postTestIntent();
  assert(calls.length === 1 && calls[0].url.startsWith('/api/intent') &&
    calls[0].url.includes('root=project-a'),
    `in-flight POST was not captured for project A: ${JSON.stringify(calls)}`);
  const inFlightPost = calls[0];
  assert(hooks.applyProjectSelection(rootB.project_id), 'POST-time project switch failed');
  snapshot = hooks.snapshot();
  assert(!snapshot.actionPending,
    `project switch retained project A pending state: ${JSON.stringify(snapshot)}`);
  assertDraftsCleared(snapshot, 'A-to-B switch');
  inFlightPost.resolve(response(true, {
    root_info: { project_id: rootA.project_id },
    target_root_project_id: rootA.project_id,
    intent_id: 'intent-a',
    state: 'queued',
  }));
  await flush();
  snapshot = hooks.snapshot();
  assert(snapshot.projectId === rootB.project_id, 'POST response changed the selected project');
  assert(snapshot.queuedCallbacks === 0 && snapshot.actionError === '' && !snapshot.actionPending,
    `stale project-A POST response affected project B: ${JSON.stringify(snapshot)}`);
  assertDraftsCleared(snapshot, 'stale project-A completion');
  assert(calls.length === 1, 'stale POST response triggered current-project refetches');

  assert(hooks.applyProjectSelection(rootA.project_id), 'switch back before attention failed');
  snapshot = hooks.snapshot();
  assertDraftsCleared(snapshot, 'B-to-A switch');
  hooks.fetchAttention();
  assert(calls.length === 2 && calls[1].url.includes('root=project-a'),
    `initial attention was not project scoped: ${JSON.stringify(calls)}`);
  const staleAttention = calls[1];

  assert(hooks.applyProjectSelection(rootB.project_id), 'project switch was rejected');
  snapshot = hooks.snapshot();
  assert(snapshot.projectId === rootB.project_id, 'selected project id did not change');
  assert(snapshot.view === 'overview', 'agent drill-in did not return to overview');
  assert(snapshot.selectedAgent === null && snapshot.sessionRid === null,
    'root-bound drill-ins were not cleared');
  assert(snapshot.threadKeys.length === 0 && snapshot.attention === null,
    'root-bound caches were not cleared');
  assert(historyValues.at(-1).includes('root=project-b'),
    `URL was not persisted by project id: ${historyValues.at(-1)}`);
  assert(document.title.includes(rootB.label) && document.title.toLowerCase().includes('overview'),
    `document title lacks project/view context: ${document.title}`);

  hooks.fetchRootPayloads();
  const selectedCalls = calls.slice(2);
  const expected = ['/api/session', '/api/intents', '/api/attention', '/api/lead-chat',
    '/api/learning', '/api/onboarding'];
  for (const endpoint of expected) {
    const call = selectedCalls.find((item) => item.url.startsWith(endpoint));
    assert(call, `project switch did not refetch ${endpoint}`);
    assert(call.url.includes('root=project-b'), `${endpoint} used the wrong root: ${call.url}`);
  }

  const currentAttention = selectedCalls.find((item) => item.url.startsWith('/api/attention'));
  currentAttention.resolve(response(true, {
    root_info: { project_id: rootB.project_id }, count: 2, items: [],
  }));
  for (const call of selectedCalls) {
    if (call !== currentAttention) call.resolve(response(false, {}));
  }
  await flush();
  snapshot = hooks.snapshot();
  assert(snapshot.attention && snapshot.attention.count === 2,
    `current project response did not commit: ${JSON.stringify(snapshot.attention)}`);

  assert(hooks.applyProjectSelection(rootA.project_id), 'switch back to project A failed');
  hooks.fetchAttention();
  const currentA = calls.at(-1);
  currentA.resolve(response(true, {
    root_info: { project_id: rootA.project_id }, count: 3, items: [],
  }));
  await flush();
  staleAttention.resolve(response(true, {
    root_info: { project_id: rootA.project_id }, count: 99, items: [],
  }));
  await flush();

  snapshot = hooks.snapshot();
  assert(snapshot.attention && snapshot.attention.count === 3,
    `pre-switch response became current after A-B-A: ${JSON.stringify(snapshot.attention)}`);

  hooks.setState({ roots: [rootB, rootA] }, rootA.project_id);
  assert(!hooks.reconcileProjectSelection(), 'root reorder changed the selected project');
  assert(hooks.snapshot().projectId === rootA.project_id,
    'root reorder changed project-id selection');

  hooks.setState({ roots: [rootB] }, rootA.project_id);
  assert(hooks.reconcileProjectSelection(), 'removed project did not select the fallback');
  assert(hooks.snapshot().projectId === rootB.project_id,
    'removed project did not fall back to root 0');
  assert(historyValues.at(-1).includes('root=project-b'),
    'fallback project id was not persisted to the URL');
})().catch((err) => {
  console.error(err && err.stack ? err.stack : err);
  process.exitCode = 1;
});
""", encoding="utf-8")
    subprocess.run(["node", str(runner), str(instrumented)], check=True,
                   capture_output=True, text=True)


def test_console_project_history_back_forward_restores_root_context(
    tmp_path: Path,
) -> None:
    if shutil.which("node") is None:
        pytest.skip("node is required for console project-history test")

    console_js = Path(web.__file__).with_name("web_static") / "console.js"
    src = console_js.read_text(encoding="utf-8")
    marker = "  // ------------------------------------------------------------ loops\n"
    assert marker in src
    src = src.replace(
        marker,
        "  globalThis.__agenttalkHistoryTestHooks = {\n"
        "    setState: function (payload, projectId) {\n"
        "      lastState = payload;\n"
        "      state.selectedRootId = projectId;\n"
        "    },\n"
        "    seedRootContext: function (prefix) {\n"
        "      state.view = 'agent';\n"
        "      state.selectedAgent = 'alpha';\n"
        "      state.sessionRid = 'rid-shared';\n"
        "      threadCache = {};\n"
        "      threadCache[currentRootId() + '|rid-shared'] = {request_id: 'rid-shared'};\n"
        "      attentionData = {root_info: {project_id: currentRootId()}, count: 7};\n"
        "      composerState.body = prefix + ' message';\n"
        "      answerComposerState = {'rid-shared': prefix + ' answer'};\n"
        "      leadChatComposerState.body = prefix + ' lead chat';\n"
        "    },\n"
        "    selectProject: selectProject,\n"
        "    reconcileProjectSelection: reconcileProjectSelection,\n"
        "    snapshot: function () {\n"
        "      return {\n"
        "        projectId: state.selectedRootId,\n"
        "        view: state.view,\n"
        "        selectedAgent: state.selectedAgent,\n"
        "        sessionRid: state.sessionRid,\n"
        "        threadKeys: Object.keys(threadCache),\n"
        "        attention: attentionData,\n"
        "        messageBody: composerState.body,\n"
        "        answerKeys: Object.keys(answerComposerState),\n"
        "        leadChatBody: leadChatComposerState.body,\n"
        "      };\n"
        "    }\n"
        "  };\n\n" + marker,
        1,
    )
    instrumented = tmp_path / "console-history.instrumented.js"
    instrumented.write_text(src, encoding="utf-8")
    runner = tmp_path / "console-history.js"
    runner.write_text(r"""
const fs = require('node:fs');
const vm = require('node:vm');

function assert(cond, msg) {
  if (!cond) throw new Error(msg);
}
function response(ok, data) {
  return { ok, status: ok ? 200 : 404, json() { return Promise.resolve(data || {}); } };
}
async function flush() {
  for (let i = 0; i < 8; i += 1) await Promise.resolve();
}

const calls = [];
function deferredFetch(url) {
  return new Promise((resolve) => calls.push({url: String(url), resolve}));
}
const listeners = {};
const historyWrites = [];
const locationState = {pathname: '/dashboard', search: '', hash: ''};
function applyUrl(value) {
  const parsed = new URL(String(value), 'http://agenttalk.local');
  locationState.pathname = parsed.pathname;
  locationState.search = parsed.search;
  locationState.hash = parsed.hash;
}
const history = {
  entries: [],
  states: [],
  index: -1,
  reset(value, state) {
    this.entries = [String(value)];
    this.states = [state || null];
    this.index = 0;
    applyUrl(value);
  },
  pushState(state, _title, value) {
    this.entries = this.entries.slice(0, this.index + 1);
    this.states = this.states.slice(0, this.index + 1);
    this.entries.push(String(value));
    this.states.push(state || null);
    this.index += 1;
    applyUrl(value);
    historyWrites.push({method: 'push', value: String(value)});
  },
  replaceState(state, _title, value) {
    if (this.index < 0) this.reset(value, state);
    else {
      this.entries[this.index] = String(value);
      this.states[this.index] = state || null;
      applyUrl(value);
    }
    historyWrites.push({method: 'replace', value: String(value)});
  },
  back() {
    if (this.index <= 0) return;
    this.index -= 1;
    applyUrl(this.entries[this.index]);
    if (listeners.popstate) listeners.popstate({state: this.states[this.index]});
  },
  forward() {
    if (this.index + 1 >= this.entries.length) return;
    this.index += 1;
    applyUrl(this.entries[this.index]);
    if (listeners.popstate) listeners.popstate({state: this.states[this.index]});
  },
};
history.reset('/dashboard?root=project-a', {projectId: 'project-a'});

const document = {
  readyState: 'loading',
  title: '',
  addEventListener() {},
  getElementById() { return null; },
  querySelector() { return null; },
  querySelectorAll() { return []; },
  createElement() { return {}; },
  createElementNS() { return {}; },
};
const ctx = {
  console,
  document,
  location: locationState,
  history,
  addEventListener(type, fn) { listeners[type] = fn; },
  URL,
  URLSearchParams,
  localStorage: {getItem() { return null; }, setItem() {}, removeItem() {}},
  setInterval() {},
  clearInterval() {},
  fetch: deferredFetch,
  __agenttalkHistoryTestHooks: null,
};
ctx.globalThis = ctx;
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), ctx);
const hooks = ctx.__agenttalkHistoryTestHooks;

(async () => {
  const rootA = {
    label: 'project [aaaa1111]', path: 'D:\\one\\project',
    project_id: 'project-a', agents: [{name: 'alpha'}],
  };
  const rootB = {
    label: 'project [bbbb2222]', path: 'D:\\two\\project',
    project_id: 'project-b', agents: [{name: 'alpha'}],
  };
  hooks.setState({roots: [rootA, rootB]}, rootA.project_id);
  assert(!hooks.reconcileProjectSelection(), 'initial deep link changed the project');
  assert(history.entries.length === 1 && historyWrites.at(-1).method === 'replace',
    `initial reconciliation did not replace: ${JSON.stringify(historyWrites)}`);

  hooks.selectProject(rootB.project_id);
  assert(history.entries.length === 2 && history.index === 1,
    `user selection did not add history: ${JSON.stringify(history)}`);
  assert(historyWrites.at(-1).method === 'push' &&
    history.entries[1].includes('root=project-b'),
    `user selection was not a project-B push: ${JSON.stringify(historyWrites)}`);
  assert(calls.length === 9 && calls.every((call) => call.url.includes('root=project-b')),
    `project-B selection did not refetch B: ${JSON.stringify(calls)}`);
  const staleB = calls.find((call) => call.url.startsWith('/api/attention'));
  hooks.seedRootContext('project B');

  const writesBeforeBack = historyWrites.length;
  history.back();
  let snapshot = hooks.snapshot();
  assert(snapshot.projectId === rootA.project_id,
    `Back did not restore A: ${JSON.stringify(snapshot)}`);
  assert(snapshot.view === 'overview' && snapshot.selectedAgent === null &&
    snapshot.sessionRid === null && snapshot.threadKeys.length === 0,
    `Back did not clear B drill-ins/cache: ${JSON.stringify(snapshot)}`);
  assert(snapshot.messageBody === '' && snapshot.answerKeys.length === 0 &&
    snapshot.leadChatBody === '',
    `Back did not clear B drafts: ${JSON.stringify(snapshot)}`);
  assert(history.entries.length === 2 && history.index === 0 &&
    historyWrites.length === writesBeforeBack,
    `Back wrote history: ${JSON.stringify(historyWrites)}`);
  const aCalls = calls.slice(9);
  assert(aCalls.length === 9 && aCalls.every((call) => call.url.includes('root=project-a')),
    `Back did not refetch A: ${JSON.stringify(aCalls)}`);

  const currentA = aCalls.find((call) => call.url.startsWith('/api/attention'));
  currentA.resolve(response(true, {
    root_info: {project_id: rootA.project_id}, count: 3, items: [],
  }));
  for (const call of aCalls) if (call !== currentA) call.resolve(response(false, {}));
  for (const call of calls.slice(0, 9)) {
    if (call !== staleB) call.resolve(response(false, {}));
  }
  await flush();
  staleB.resolve(response(true, {
    root_info: {project_id: rootB.project_id}, count: 99, items: [],
  }));
  await flush();
  snapshot = hooks.snapshot();
  assert(snapshot.attention && snapshot.attention.count === 3,
    `stale B data replaced A after Back: ${JSON.stringify(snapshot.attention)}`);

  hooks.seedRootContext('project A');
  const writesBeforeForward = historyWrites.length;
  history.forward();
  snapshot = hooks.snapshot();
  assert(snapshot.projectId === rootB.project_id,
    `Forward did not restore B: ${JSON.stringify(snapshot)}`);
  assert(snapshot.messageBody === '' && snapshot.answerKeys.length === 0 &&
    snapshot.leadChatBody === '' && snapshot.threadKeys.length === 0,
    `Forward did not clear A root context: ${JSON.stringify(snapshot)}`);
  assert(history.entries.length === 2 && history.index === 1 &&
    historyWrites.length === writesBeforeForward,
    `Forward wrote history: ${JSON.stringify(historyWrites)}`);
  const forwardCalls = calls.slice(18);
  assert(forwardCalls.length === 9 &&
    forwardCalls.every((call) => call.url.includes('root=project-b')),
    `Forward did not refetch B: ${JSON.stringify(forwardCalls)}`);
})().catch((err) => {
  console.error(err && err.stack ? err.stack : err);
  process.exitCode = 1;
});
""", encoding="utf-8")
    subprocess.run(["node", str(runner), str(instrumented)], check=True,
                   capture_output=True, text=True)


def _density_vars(css: str, density: str) -> dict[str, str]:
    m = re.search(rf'#app\[data-density="{density}"\]\s*\{{(?P<body>.*?)\}}',
                  css, flags=re.S)
    assert m is not None
    out: dict[str, str] = {}
    for name, value in re.findall(r"(--[A-Za-z0-9_-]+)\s*:\s*([^;]+);", m.group("body")):
        out[name] = value.strip()
    return out


def _px(value: str) -> float:
    assert value.endswith("px")
    return float(value[:-2])


def _grid_columns_at_1280(vars_: dict[str, str]) -> int:
    viewport = 1280.0
    main_pad_x = 60.0
    content_w = viewport - _px(vars_["--sidebar-w"]) - main_pad_x
    card_min = _px(vars_["--agent-card-min"])
    gap = _px(vars_["--card-gap"])
    return int((content_w + gap) // (card_min + gap))


def test_console_compact_density_has_material_size_delta_and_mobile_guards(tmp_path: Path) -> None:
    s = _make_store(tmp_path)
    srv, _t, base = _serve(s)
    try:
        with _get(f"{base}/static/console.css") as resp:
            css = resp.read().decode("utf-8")
    finally:
        srv.shutdown()
        srv.server_close()

    comfortable = _density_vars(css, "comfortable")
    compact = _density_vars(css, "compact")
    comfortable_task = _px(comfortable["--task-min-h"])
    compact_task = _px(compact["--task-min-h"])
    assert (comfortable_task - compact_task) / comfortable_task >= 0.15
    assert _grid_columns_at_1280(compact) >= _grid_columns_at_1280(comfortable) + 1
    assert _px(compact["--topbar-h"]) < _px(comfortable["--topbar-h"])
    assert _px(compact["--agent-card-min"]) < _px(comfortable["--agent-card-min"])
    assert "grid-template-columns: minmax(0, 1fr);" in css
    assert "overflow-wrap: anywhere;" in css
    assert ".tc-project-context" in css
    assert ".tc-project-path" in css
    assert "text-overflow: ellipsis;" in css
    assert "max-width: calc(100vw - 24px);" in css


def test_console_mobile_filters_and_lead_subtitle_have_fit_contracts(
    tmp_path: Path,
) -> None:
    s = _make_store(tmp_path)
    srv, _t, base = _serve(s)
    try:
        with _get(f"{base}/static/console.css") as resp:
            css = resp.read().decode("utf-8")
        with _get(f"{base}/static/console.js") as resp:
            js = resp.read().decode("utf-8")
    finally:
        srv.shutdown()
        srv.server_close()

    assert "el('div', 'tc-filters')" in js
    assert "el('p', 'tc-subtitle', leadChatSubtitle(data, root))" in js
    assert re.search(r"\.tc-filters\s*\{[^}]*flex-wrap:\s*wrap", css, re.S)
    assert re.search(r"\.tc-view-head\s*>\s*:first-child\s*\{[^}]*min-width:\s*0", css, re.S)
    assert re.search(r"\.tc-subtitle\s*\{[^}]*overflow-wrap:\s*anywhere", css, re.S)
    assert re.search(
        r"@media \(max-width: 560px\)[\s\S]*?\.tc-filter\s*\{[^}]*flex:\s*1 1",
        css,
    )


def test_shaped_avatar_css_is_scoped_and_preserves_originals(tmp_path: Path) -> None:
    s = _make_store(tmp_path)
    srv, _t, base = _serve(s)
    try:
        with _get(f"{base}/static/console.css") as resp:
            css = resp.read().decode("utf-8")
    finally:
        srv.shutdown()
        srv.server_close()

    assert ".tc-avatar img" in css
    assert "object-fit: cover;" in css
    assert ".tc-avatar-shaped" in css
    assert ".tc-avatar-shaped img" in css
    assert "object-fit: contain;" in css
    assert ".tc-agent-avatar.tc-avatar-shaped" in css
    assert "width: 24px;" in css and "height: 32px;" in css
    assert ".tc-node-avatar.tc-avatar-shaped" in css
    assert "width: 30px;" in css and "height: 40px;" in css
    assert ".tc-detail-avatar.tc-avatar-shaped" in css
    assert "width: 52px;" in css and "height: 68px;" in css
    assert ".tc-operator-avatar" in css
    assert "border-radius: 7px;" in css


def test_console_capacity_detail_renders_rich_provider_rows(tmp_path: Path) -> None:
    s = _make_store(tmp_path)
    srv, _t, base = _serve(s)
    try:
        with _get(f"{base}/static/console.js") as resp:
            js = resp.read().decode("utf-8")
    finally:
        srv.shutdown()
        srv.server_close()

    for expected in (
        "function capacitySummary",
        "function capacityWindowRow",
        "function resetText",
        "Weekly rate limit",
        "budget unknown",
        "capProviderBadge",
    ):
        assert expected in js


def test_console_js_thread_cache_key_single_source(tmp_path: Path) -> None:
    """Regression (reviewer-1, fold-3): the transcript cache READ (transcriptCard)
    and WRITE (fetchThread) must derive the key identically, or every Sessions
    transcript stays stuck on "Loading…". A NUL byte had crept into threadKey's
    delimiter — rendering the file binary AND mismatching fetchThread's space key,
    so the read always missed. Guard the served bytes: no NUL, and the key is
    derived in exactly one place (threadKey) with no ad-hoc `+ ' ' + rid` site."""
    s = _make_store(tmp_path)
    srv, _t, base = _serve(s)
    try:
        with _get(f"{base}/static/console.js") as resp:
            raw = resp.read()
    finally:
        srv.shutdown()
        srv.server_close()
    assert b"\x00" not in raw, "console.js must not contain a NUL byte"
    js = raw.decode("utf-8")
    assert "function threadKey(" in js
    assert "label + ' ' + rid" not in js, "cache key must go through threadKey(), not an ad-hoc build"


def test_console_marks_launch_environment_guidance_as_unverified() -> None:
    console_js = Path(web.__file__).with_name("web_static") / "console.js"
    source = console_js.read_text(encoding="utf-8")

    assert "Launch environment guidance (child value not verified):" in source


def test_dashboard_shell_no_inline_handlers(tmp_path: Path) -> None:
    """§1 / §6: the /dashboard console shell carries no inline <style>, no
    inline event handlers, and no style= attributes — all of which would
    require 'unsafe-inline' and weaken the CSP. Styling/behavior are linked
    assets only."""
    s = _make_store(tmp_path)
    srv, _t, base = _serve(s)
    try:
        with _get(f"{base}/dashboard") as resp:
            shell = resp.read().decode("utf-8")
            assert resp.headers["Content-Security-Policy"] == _DASH_CSP
        assert "<style" not in shell
        assert "onclick=" not in shell and "onchange=" not in shell
        assert "style=" not in shell
        assert "/static/console.css" in shell and "/static/console.js" in shell
    finally:
        srv.shutdown()
        srv.server_close()


# =============================================== 0.58.0 Team Console fields
#
# Contract under test: docs/DashboardDesign/BUILD-SPEC.md §3 (additive
# /api/state fields), §4a (/api/attention), §4b (/api/thread/<rid>).

import agenttalk.health as _hm  # noqa: E402


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _write_health(store: Store, agent: str, state: str, *,
                  cli: str | None = None, mode: str | None = None,
                  reason_code: str | None = None) -> None:
    ts = _now_iso()
    store.write_health(agent, _hm.build_snapshot(
        agent=agent, cli=cli, mode=mode, state=state,
        updated_at=ts, since=ts, last_progress_at=ts,
        reason_code=reason_code or state))


def test_api_state_capacity_cli_wrapped_present_when_data_exists(tmp_path: Path) -> None:
    """§3a: per-agent capacity/cli/wrapped/restartable are present when their
    source data exists, and the capacity object carries the frozen wire keys."""
    s = _make_store(tmp_path)
    s.write_heartbeat("alpha")
    _write_health(s, "alpha", _hm.STATE_WORKING_TURN, cli="claude", mode="wrapper-loop")
    observed = _now_iso()
    s.write_capacity("alpha", {
        "schema_version": 1, "source_agent": "alpha", "observed_at": observed,
        "source": "claude_statusline", "primary_used_percent": 42.0,
        "primary_resets_at": 1781005233, "primary_window_minutes": 300,
        "secondary_used_percent": 64.0, "secondary_resets_at": 1781137669,
        "secondary_window_minutes": 10080,
        "context_used_percent": 71.5, "context_tokens": 1234,
        "context_window_size": 4096, "confidence": "observed",
        "plan_type": "max", "limit_id": "claude",
    })
    srv, _t, base = _serve(s)
    try:
        (root,) = _state(base)["roots"]
        alpha = next(a for a in root["agents"] if a["name"] == "alpha")
        assert alpha["cli"] == "claude"
        cap = alpha["capacity"]
        assert {"rate_used_pct", "context_used_pct", "confidence"} <= set(cap)
        assert cap["rate_used_pct"] == 42.0
        assert cap["context_used_pct"] == 71.5
        assert cap["confidence"] == "fresh"
        assert cap["source"] == "claude_statusline"
        assert cap["observed_at"] == observed
        assert cap["plan_type"] == "max"
        assert cap["limit_id"] == "claude"
        assert cap["primary"] == {
            "label": "5h",
            "used_pct": 42.0,
            "resets_at": 1781005233,
            "reset_in_seconds": cap["primary"]["reset_in_seconds"],
            "window_minutes": 300,
        }
        assert cap["secondary"]["label"] == "weekly"
        assert cap["secondary"]["used_pct"] == 64.0
        assert cap["secondary"]["window_minutes"] == 10080
        assert cap["context"] == {"used_pct": 71.5, "tokens": 1234, "window_size": 4096}
        assert alpha["wrapped"] is True
        assert alpha["restartable"] is True
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_state_wrapped_from_managed_lead_loop_when_health_unknown(tmp_path: Path) -> None:
    """§3a (review P2-1): wrapped/restartable arm on the managed-lead-loop set even
    when health mode is unknown (stale/missing snapshot). Health mode alone would
    OMIT the field exactly when a wrapped agent is down — losing the restart signal
    when it matters most."""
    s = _make_store(tmp_path)
    # 'alpha' is in a managed lead-loop but has NO health snapshot -> mode unknown.
    s.set_managed_lead_loop("alpha")
    srv, _t, base = _serve(s)
    try:
        (root,) = _state(base)["roots"]
        alpha = next(a for a in root["agents"] if a["name"] == "alpha")
        # No health snapshot => _is_wrapped(health) is None; the managed set arms it.
        assert alpha.get("wrapped") is True
        assert alpha.get("restartable") is True
        # 'beta' is neither wrapped-by-health nor managed -> field OMITTED.
        beta = next(a for a in root["agents"] if a["name"] == "beta")
        assert "wrapped" not in beta
        assert "restartable" not in beta
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_state_capacity_null_percent_allowed_inside_object(tmp_path: Path) -> None:
    """§3a: a capacity snapshot with only one signal keeps the OTHER percent as
    null INSIDE the capacity object (the absent-not-null rule is about the
    `capacity` key itself, not the percents within)."""
    s = _make_store(tmp_path)
    s.write_capacity("alpha", {
        "schema_version": 1, "source_agent": "alpha", "observed_at": _now_iso(),
        "source": "codex_rollout", "primary_used_percent": 55.0,
        "context_used_percent": None, "confidence": "observed",
    })
    srv, _t, base = _serve(s)
    try:
        (root,) = _state(base)["roots"]
        alpha = next(a for a in root["agents"] if a["name"] == "alpha")
        assert "capacity" in alpha
        assert alpha["capacity"]["rate_used_pct"] == 55.0
        assert alpha["capacity"]["context_used_pct"] is None
        assert alpha["capacity"]["primary"]["used_pct"] == 55.0
        assert "secondary" not in alpha["capacity"]
        assert "context" not in alpha["capacity"]
        # cli inferred from the snapshot source when no fresh health.cli
        assert alpha["cli"] == "codex"
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_state_capacity_unknown_reason_is_additive(tmp_path: Path) -> None:
    s = _make_store(tmp_path)
    s.write_capacity("alpha", {
        "schema_version": 1, "source_agent": "alpha", "observed_at": _now_iso(),
        "source": "unknown", "confidence": "unknown", "reason": "codex_home_missing",
        "primary_used_percent": None, "context_used_percent": None,
    })
    srv, _t, base = _serve(s)
    try:
        (root,) = _state(base)["roots"]
        alpha = next(a for a in root["agents"] if a["name"] == "alpha")
        cap = alpha["capacity"]
        assert cap["confidence"] == "unknown"
        assert cap["reason"] == "codex_home_missing"
        assert cap["rate_used_pct"] is None
        assert "primary" not in cap
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_state_new_agent_fields_absent_when_no_data(tmp_path: Path) -> None:
    """§3a / invariant §0.5: with no capacity/health/domains, the new per-agent
    fields are OMITTED (never null)."""
    s = _make_store(tmp_path)
    srv, _t, base = _serve(s)
    try:
        (root,) = _state(base)["roots"]
        for agent in root["agents"]:
            for absent in ("capacity", "cli", "wrapped", "restartable",
                           "owned_domains", "task", "health_timeline"):
                assert absent not in agent, absent
        _assert_no_body_keys(_state(base))
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_state_owned_domains_grouped_per_agent(tmp_path: Path) -> None:
    """§3a: owned_domains inverts the domain registry via resolve_refset(owners),
    present only for an agent that owns >=1 domain, carrying name + globs."""
    s = _make_store(tmp_path)
    (s.dir / "domains.json").write_text(json.dumps({
        "schema_version": 1,
        "domains": {
            "web": {"title": "Web layer", "owners": {"agents": ["alpha"]},
                    "owned_globs": ["src/agenttalk/web.py", "src/agenttalk/web_static/*"]},
        },
        "shared_paths": [],
    }), encoding="utf-8")
    srv, _t, base = _serve(s)
    try:
        (root,) = _state(base)["roots"]
        by = {a["name"]: a for a in root["agents"]}
        assert by["alpha"]["owned_domains"] == [
            {"name": "Web layer",
             "globs": ["src/agenttalk/web.py", "src/agenttalk/web_static/*"]},
        ]
        # a non-owner has no owned_domains key (absent-not-null)
        assert "owned_domains" not in by["beta"]
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_state_task_from_owned_thread_subject(tmp_path: Path) -> None:
    """§3a: task is the subject of the agent's newest non-terminal open thread
    where it is next_owner (envelope-derived, textContent-rendered)."""
    s = _make_store(tmp_path)
    s.send(sender="alpha", recipient="beta", kind="review-request",
           subject="review the parser change", body="please",
           meta={"request_id": "q-task"})
    srv, _t, base = _serve(s)
    try:
        (root,) = _state(base)["roots"]
        beta = next(a for a in root["agents"] if a["name"] == "beta")
        # beta is next_owner of q-task -> its task is that thread's subject
        assert beta["task"] == "review the parser change"
        # alpha is not the ball-holder on any thread and isn't composing
        alpha = next(a for a in root["agents"] if a["name"] == "alpha")
        assert "task" not in alpha
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_state_suppresses_canonical_wrapper_notice_threads(tmp_path: Path) -> None:
    """Wrapper dead-letter notice twins should not become dashboard current work."""
    from agenttalk import cli as cli_mod
    from agenttalk.wrapper import recv_api

    s = Store(tmp_path)
    s.init(["beta", "claude"])
    s.set_operator_facing("claude")
    msg = s.send(sender="claude", recipient="beta", body="poison", kind="message", meta={})
    rec = recv_api.next_record(s, "beta")
    s.dead_letter("beta", rec, reason="deterministic",
                  failure_class="poison_eligible", at="2026-07-02T00:00:00Z")
    emit = cli_mod._dead_letter_notifier(s, "beta")
    assert emit({"msg_id": msg.id, "agent": "beta", "from": "claude", "kind": "message",
                 "attempts": 3, "failure_class": "poison_eligible"}, disposed=True) is True

    state = web.build_state([web.RootDescriptor(store=s, label="root")])
    root = state["roots"][0]
    assert not [r for r in root["threads"] if r.get("subject") == "dead-letter notice"]
    beta = next(a for a in root["agents"] if a["name"] == "beta")
    assert beta.get("task") != "dead-letter notice"


def test_api_state_suppresses_canonical_config_blocked_notice_threads(tmp_path: Path) -> None:
    from agenttalk import cli as cli_mod

    s = Store(tmp_path)
    s.init(["beta", "claude"])
    s.set_operator_facing("claude")
    s.write_config_blocked_hold("beta", summary="exec denied")
    emit = cli_mod._dead_letter_notifier(s, "beta")
    assert emit({"msg_id": "x", "agent": "beta", "from": "claude", "kind": "message",
                 "attempts": 1, "failure_class": "config_blocked", "summary": "exec denied"},
                disposed=False) is True

    state = web.build_state([web.RootDescriptor(store=s, label="root")])
    root = state["roots"][0]
    assert not [r for r in root["threads"] if r.get("subject") == "dead-letter notice"]
    beta = next(a for a in root["agents"] if a["name"] == "beta")
    assert beta.get("task") != "dead-letter notice"


def test_api_state_thread_verdict_and_active_review(tmp_path: Path) -> None:
    """§3b: per-thread verdict from the newest review-result meta.status, and
    active_review true for a non-terminal review-request thread (emit-when-true).
    verdict reads ONLY meta.status — never body."""
    s = _make_store(tmp_path)
    s.send(sender="alpha", recipient="beta", kind="review-request",
           subject="rev", body="x", meta={"request_id": "rid-v"})
    srv, _t, base = _serve(s)
    try:
        (root,) = _state(base)["roots"]
        row = next(r for r in root["threads"] if r["request_id"] == "rid-v")
        assert row["active_review"] is True   # open review-request
        assert "verdict" not in row           # no decision yet
        # the review-result lands -> verdict appears (still active until closed)
        s.send(sender="beta", recipient="alpha", kind="review-result",
               subject="done", body="lgtm",
               meta={"request_id": "rid-v", "status": "approved"})
        (root,) = _state(base)["roots"]
        row = next(r for r in root["threads"] if r["request_id"] == "rid-v")
        assert row["verdict"] == "approved"
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_state_active_review_absent_for_plain_thread(tmp_path: Path) -> None:
    """§3b: active_review is emitted only when true; a plain question thread has
    no active_review key."""
    s = _make_store(tmp_path)
    s.send(sender="alpha", recipient="beta", kind="question",
           subject="q", body="x", meta={"request_id": "rid-q"})
    srv, _t, base = _serve(s)
    try:
        (root,) = _state(base)["roots"]
        row = next(r for r in root["threads"] if r["request_id"] == "rid-q")
        assert "active_review" not in row
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_state_health_timeline_builds_over_polls(tmp_path: Path) -> None:
    """§5: the in-memory ring accumulates samples across /api/state polls and
    emits collapsed {state, seconds} segments. It is server-owned, never a file
    (proven by test_no_mutation_full_tree_hash)."""
    s = _make_store(tmp_path)
    s.write_heartbeat("alpha")
    _write_health(s, "alpha", _hm.STATE_WORKING_TURN, cli="claude", mode="wrapper-loop")
    srv, _t, base = _serve(s)
    try:
        seen = None
        for _ in range(3):
            (root,) = _state(base)["roots"]
            alpha = next(a for a in root["agents"] if a["name"] == "alpha")
            seen = alpha.get("health_timeline")
        assert seen  # after several polls the ring has >=1 segment
        assert all(set(seg) == {"state", "seconds"} for seg in seen)
        assert seen[0]["state"] == "working_turn"
    finally:
        srv.shutdown()
        srv.server_close()


def test_build_state_is_pure_without_history(tmp_path: Path) -> None:
    """§5: build_state(roots) with no history omits health_timeline entirely,
    so the perf/unit surface stays deterministic (no ring side effects)."""
    s = _make_store(tmp_path)
    s.write_heartbeat("alpha")
    _write_health(s, "alpha", _hm.STATE_WORKING_TURN, cli="claude", mode="wrapper-loop")
    roots = [web.RootDescriptor(store=s, label="pure")]
    state = web.build_state(roots)  # no history=
    (root,) = state["roots"]
    for a in root["agents"]:
        assert "health_timeline" not in a


def test_health_timeline_ring_window_expiry() -> None:
    """§5 (review P2-6): segments() applies the window cutoff at RENDER time — an
    agent that stopped reporting can't accrue a growing stale segment past the
    window, and a sample older than the window is dropped entirely."""
    ring = web.HealthTimelineRing(window_seconds=1)
    ring.record("root", "alpha", "working_turn", now=1000.0)
    # Within the window: the (open) segment is clamped to the window, never longer.
    segs = ring.segments("root", "alpha", now=1000.5)
    assert segs and segs[0]["state"] == "working_turn"
    assert sum(seg["seconds"] for seg in segs) <= 1.0 + 1e-6
    # >1s later with no new sample: the stale sample falls outside the window.
    assert ring.segments("root", "alpha", now=1002.0) == []


# ---------------------------------------------------------- /api/learning

def _write_verified_lesson(
        store: Store, *, note_id: str = "kn-review-final",
        key: str = "review.final-sha", scope: str = "review",
        trigger: str = "Review final candidate",
        body: str = "Always review the final candidate, not an earlier draft.",
        evidence_ref: str = "rq-final-review",
        applies_to: list[str] | None = None,
        anchor: dict | None = None) -> dict:
    at = "2026-07-08T00:00:00Z"
    lesson = {
        "scope": scope,
        "trigger": trigger,
        "evidence_ref": evidence_ref,
        "owner": "dev",
        "status": kn.LESSON_STATUS_PROPOSED,
        "review_after": "2026-07-01T00:00:00Z",
        "expires_at": "2027-07-01T00:00:00Z",
        "applies_to": applies_to or [scope],
    }
    pub = kn.new_publish_event(
        note_id=note_id,
        key=key,
        type=kn.TYPE_LESSON,
        domain_id=kn.PROCESS_DOMAIN,
        body=body,
        anchor=anchor,
        verified_against_sha=None,
        domain_registry_hash="registry-hash",
        domain_definition_hash=kn.VIRTUAL_PROCESS_DOMAIN_HASH,
        author="dev",
        resolved_from="active",
        at=at,
        lesson=lesson,
    )
    cur = kn.new_curate_event(
        base=pub,
        action="verify",
        curated_by="lead",
        resolved_from="lead",
        at="2026-07-08T00:01:00Z",
        reason=None,
    )
    with store._config_lock():
        kn.write_event_locked(store, pub)
        kn.write_event_locked(store, cur)
    return cur


def _write_proposed_lesson(store: Store, *, key: str = "review.final-sha") -> dict:
    lesson = {
        "scope": "review",
        "trigger": "Review proposed candidate update",
        "evidence_ref": "rq-proposed-review",
        "owner": "dev",
        "status": kn.LESSON_STATUS_PROPOSED,
        "review_after": "2027-07-01T00:00:00Z",
        "expires_at": "2028-07-01T00:00:00Z",
        "applies_to": ["review"],
    }
    pub = kn.new_publish_event(
        note_id="kn-review-update",
        key=key,
        type=kn.TYPE_LESSON,
        domain_id=kn.PROCESS_DOMAIN,
        body="Proposed update must not show as learned by default.",
        anchor=None,
        verified_against_sha=None,
        domain_registry_hash="registry-hash",
        domain_definition_hash=kn.VIRTUAL_PROCESS_DOMAIN_HASH,
        author="dev",
        resolved_from="active",
        at="2026-07-08T00:04:00Z",
        lesson=lesson,
    )
    with store._config_lock():
        kn.write_event_locked(store, pub)
    return pub


def test_api_learning_surfaces_lessons_and_exposure_without_message_body(
        tmp_path: Path) -> None:
    s = Store(tmp_path)
    s.init(["lead", "dev", "codex"])
    note = _write_verified_lesson(s)
    verdict = kn.compute_lesson_state(note, now="2026-07-08T00:02:00Z")
    selection = lc.LessonSelection(
        rows=[(note, verdict)],
        warnings=[],
        context_scope="review",
        tags={"review"},
    )
    exposure = lc.build_exposure_event(
        agent="codex",
        record={
            "id": "20260708-000200-msg",
            "request_id": "rq-final-review",
            "kind": "review-request",
            "subject": "contains-safe-envelope-only",
            "body": "SECRET RAW BODY MUST NOT LEAK",
        },
        selection=selection,
        turn_id="turn-final-review",
        at="2026-07-08T00:02:00Z",
    )
    assert exposure is not None
    lc.append_exposure_event(s, exposure)

    payload = web.build_learning(web.RootDescriptor(s, "root"))
    assert payload["counts"]["accepted"] == 1
    assert payload["counts"]["active"] == 1
    assert payload["counts"]["review_due"] == 1
    assert payload["counts"]["exposures"] == 1
    lesson = payload["lessons"][0]
    assert lesson["key"] == "review.final-sha"
    assert lesson["body"] == "Always review the final candidate, not an earlier draft."
    assert lesson["author"] == "dev"
    assert lesson["curator"] == "lead"
    assert lesson["exposure"]["count"] == 1
    assert lesson["exposure"]["agents"] == [{"agent": "codex", "count": 1}]
    assert payload["recent_exposures"][0]["request_id"] == "rq-final-review"
    assert payload["recent_exposures"][0]["prompt_block_sha256"]
    assert payload["recent_exposures"][0]["lesson_fingerprint"] == lesson["lesson_fingerprint"]
    assert "SECRET RAW BODY" not in json.dumps(payload)

    srv, _t, base = _serve(s)
    try:
        with _get(f"{base}/api/learning") as resp:
            wire = json.loads(resp.read().decode("utf-8"))
    finally:
        srv.shutdown()
        srv.server_close()
    assert wire["lessons"][0]["key"] == "review.final-sha"


@pytest.mark.parametrize(("field", "forged_value"), [
    ("created_at", "2099-12-31T23:59:59Z"),
    ("curator", "attacker"),
])
def test_api_learning_ignores_causal_curation_with_forged_display_provenance(
        tmp_path: Path, field: str, forged_value: str) -> None:
    store = Store(tmp_path)
    store.init(["lead", "dev"])
    current = _write_verified_lesson(store)
    forged = kn.new_curate_event(
        base=current,
        action="verify",
        curated_by="lead",
        resolved_from="lead",
        at="2026-07-08T00:02:00Z",
        reason=None,
    )
    if field == "curator":
        forged["lesson"] = {**forged["lesson"], "curator": forged_value}
    else:
        forged[field] = forged_value
    assert kn.event_problem(forged) is None
    kn.append_event(store, forged)

    payload = web.build_learning(web.RootDescriptor(store, "root"))

    lesson = payload["lessons"][0]
    assert lesson["created_at"] == "2026-07-08T00:00:00Z"
    assert lesson["curated_by"] == "lead"
    assert lesson["curator"] == "lead"
    assert forged_value not in {lesson["created_at"], lesson["curator"]}


def test_api_learning_anchor_evidence_is_pointer_allowlisted(tmp_path: Path) -> None:
    s = Store(tmp_path)
    s.init(["lead", "dev"])
    _write_verified_lesson(
        s,
        anchor={
            "kind": "request",
            "request_id": "rq-anchor",
            "anchor_evidence": {
                "body": "SECRET ANCHOR BODY MUST NOT LEAK",
                "prompt": "SECRET ANCHOR PROMPT MUST NOT LEAK",
                "prompt_block": "SECRET PROMPT BLOCK MUST NOT LEAK",
                "output": "SECRET OUTPUT MUST NOT LEAK",
                "trace_sha256": "a" * 64,
                "artifact_ref": "quality-run-123",
                "nested_hashes": {"body": "SECRET NESTED BODY MUST NOT LEAK"},
            },
        },
    )

    payload = web.build_learning(web.RootDescriptor(s, "root"))
    dumped = json.dumps(payload)
    assert "SECRET ANCHOR BODY" not in dumped
    assert "SECRET ANCHOR PROMPT" not in dumped
    assert "SECRET PROMPT BLOCK" not in dumped
    assert "SECRET OUTPUT" not in dumped
    assert "SECRET NESTED BODY" not in dumped
    anchor = payload["lessons"][0]["anchor"]
    assert anchor["kind"] == "request"
    assert anchor["request_id"] == "rq-anchor"
    assert anchor["anchor_evidence"] == {
        "trace_sha256": "a" * 64,
        "artifact_ref": "quality-run-123",
    }


def test_api_learning_default_keeps_proposed_updates_separate(tmp_path: Path) -> None:
    s = Store(tmp_path)
    s.init(["lead", "dev"])
    _write_verified_lesson(s)
    _write_proposed_lesson(s)

    active = web.build_learning(web.RootDescriptor(s, "root"))
    assert active["filters"]["status"] == "active"
    assert active["counts"]["active"] == 1
    assert active["counts"]["accepted"] == 1
    assert active["counts"]["proposed"] == 1
    assert [it["body"] for it in active["lessons"]] == [
        "Always review the final candidate, not an earlier draft."
    ]

    proposed = web.build_learning(web.RootDescriptor(s, "root"), status="proposed")
    assert proposed["filters"]["status"] == "proposed"
    assert [it["body"] for it in proposed["lessons"]] == [
        "Proposed update must not show as learned by default."
    ]


def test_api_learning_route_rejects_bad_filters(tmp_path: Path) -> None:
    s = Store(tmp_path)
    s.init(["lead", "dev"])
    srv, _t, base = _serve(s)
    try:
        for suffix, code in (
            ("?status=bogus", "bad_status"),
            ("?limit=nope", "bad_limit"),
            ("?limit=0", "bad_limit"),
            ("?root=no-such-root", "bad_root"),
        ):
            with pytest.raises(urllib.error.HTTPError) as exc:
                _get(f"{base}/api/learning{suffix}")
            assert exc.value.code == 400
            payload = json.loads(exc.value.read().decode("utf-8"))
            assert payload["error"] == code
            assert payload["lessons"] == []
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_learning_empty_state_shape(tmp_path: Path) -> None:
    s = Store(tmp_path)
    s.init(["lead", "dev"])
    payload = web.build_learning(web.RootDescriptor(s, "root"))
    assert payload["schema_version"] == 1
    assert payload["filters"]["status"] == "active"
    assert payload["counts"]["total"] == 0
    assert payload["counts"]["showing"] == 0
    assert payload["items"] == []
    assert payload["lessons"] == []
    assert payload["recent_exposures"] == []
    assert payload["problems"] == {"knowledge": [], "exposures": []}

    srv, _t, base = _serve(s)
    try:
        with _get(f"{base}/api/learning") as resp:
            wire = json.loads(resp.read().decode("utf-8"))
    finally:
        srv.shutdown()
        srv.server_close()
    assert wire["lessons"] == []


def test_api_learning_keeps_real_legacy_process_lessons_visible(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.init(["lead", "dev"])
    fixture = (
        Path(__file__).parent / "fixtures" / "knowledge"
        / "legacy-process-lessons.jsonl"
    )
    kn.knowledge_dir(store).mkdir(parents=True, exist_ok=True)
    kn.notes_path(store).write_bytes(fixture.read_bytes())

    payload = web.build_learning(web.RootDescriptor(store, "root"))
    assert {row["key"] for row in payload["lessons"]} == {
        "model-effort-selection", "roster-and-context-lifecycle"}
    assert payload["counts"]["active"] == 2
    assert all(
        kn.CAUTION_LEGACY_UNSCOPED in row["caution_flags"]
        for row in payload["lessons"]
    )


def test_api_learning_retired_filter_is_explicit(tmp_path: Path) -> None:
    s = Store(tmp_path)
    s.init(["lead", "dev"])
    note = _write_verified_lesson(s)
    retired = kn.new_curate_event(
        base=note,
        action="retract",
        curated_by="lead",
        resolved_from="lead",
        at="2026-07-08T00:05:00Z",
        reason="obsolete",
    )
    with s._config_lock():
        kn.write_event_locked(s, retired)

    active = web.build_learning(web.RootDescriptor(s, "root"))
    assert active["lessons"] == []
    assert active["counts"]["retired"] == 1

    payload = web.build_learning(web.RootDescriptor(s, "root"), status="retired")
    assert [it["key"] for it in payload["lessons"]] == ["review.final-sha"]
    assert payload["lessons"][0]["status"] == kn.LESSON_STATUS_RETIRED
    assert payload["lessons"][0]["stale_reasons"] == ["retracted"]


def test_api_learning_route_filters_scope_tag_and_root(tmp_path: Path) -> None:
    a, b = _make_two_stores(tmp_path)
    _write_verified_lesson(
        a,
        note_id="kn-review-final",
        key="review.final-sha",
        scope="review",
        applies_to=["review"],
        body="Review lesson from project A.",
    )
    _write_verified_lesson(
        b,
        note_id="kn-docs-final",
        key="docs.final-pass",
        scope="docs",
        applies_to=["docs", "manual"],
        body="Docs lesson from project B.",
    )
    srv, _t, base = _serve_multi(a, b)
    try:
        with _get(f"{base}/api/learning?root={b.root.name}&scope=docs&tag=manual") as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        assert payload["root"] == b.root.name
        assert payload["filters"]["scope"] == "docs"
        assert payload["filters"]["tags"] == ["manual"]
        assert [it["body"] for it in payload["lessons"]] == [
            "Docs lesson from project B."
        ]

        with _get(f"{base}/api/learning?root={b.root.name}&scope=review") as resp:
            empty = json.loads(resp.read().decode("utf-8"))
        assert empty["lessons"] == []
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_learning_degrades_on_corrupt_ledger_lines(tmp_path: Path) -> None:
    s = Store(tmp_path)
    s.init(["lead", "dev"])
    note = _write_verified_lesson(s)
    verdict = kn.compute_lesson_state(note, now="2026-07-08T00:03:00Z")
    exposure = lc.build_exposure_event(
        agent="dev",
        record={"id": "20260708-000300-msg", "request_id": "rq-learning"},
        selection=lc.LessonSelection(
            rows=[(note, verdict)],
            warnings=[],
            context_scope="review",
            tags=set(),
        ),
        turn_id="turn-ok",
        at="2026-07-08T00:03:00Z",
    )
    assert exposure is not None
    lc.append_exposure_event(s, exposure)
    kn.notes_path(s).write_text(
        kn.notes_path(s).read_text(encoding="utf-8") + "{not-json\n",
        encoding="utf-8",
    )
    lc.exposures_path(s).write_text(
        lc.exposures_path(s).read_text(encoding="utf-8") + "{bad\n",
        encoding="utf-8",
    )

    payload = web.build_learning(web.RootDescriptor(s, "root"))
    assert payload["counts"]["total"] == 1
    assert payload["counts"]["exposures"] == 1
    assert payload["problems"]["knowledge"]
    assert payload["problems"]["exposures"]


# ---------------------------------------------------------- /api/onboarding

def _write_onboarding_run(store: Store, *, run_id: str = "ob-existing") -> None:
    with store._config_lock():
        ob.write_event_locked(store, ob.new_create_event(
            run_id=run_id,
            title="Existing codebase analysis",
            objective="Map code/docs drift before implementation starts.",
            base_ref="main",
            lead="alpha",
            state="scanning",
            at="2026-07-09T11:00:00Z",
        ))
        ob.write_event_locked(store, ob.new_record_event(
            run_id=run_id,
            kind=ob.KIND_SEGMENT,
            key="cli",
            status="accepted",
            summary="CLI command surface mapped from parser and README.",
            actor="alpha",
            owner="alpha",
            checkers=["beta"],
            refs=["analysis:cli"],
            paths=["src/agenttalk/cli.py", "README.md"],
            at="2026-07-09T11:01:00Z",
        ))
        ob.write_event_locked(store, ob.new_record_event(
            run_id=run_id,
            kind=ob.KIND_DRIFT,
            key="docs.cli.reference",
            status="open",
            summary="README command reference may lag parser help.",
            actor="beta",
            segment="cli",
            source="docs",
            confidence="medium",
            at="2026-07-09T11:02:00Z",
        ))
        ob.write_event_locked(store, ob.new_record_event(
            run_id=run_id,
            kind=ob.KIND_UNKNOWN,
            key="release.owner",
            status="open",
            summary="Need operator confirmation for release owner.",
            actor="beta",
            blocking=True,
            at="2026-07-09T11:03:00Z",
        ))


def test_api_onboarding_surfaces_runs_without_message_body(tmp_path: Path) -> None:
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    s.send(sender="alpha", recipient="beta", body="SECRET BUS BODY MUST NOT LEAK")
    _write_onboarding_run(s)

    payload = web.build_onboarding(web.RootDescriptor(s, "root"))

    assert payload["counts"]["total"] == 1
    assert payload["counts"]["active"] == 1
    assert payload["counts"]["accepted_segments"] == 1
    assert payload["counts"]["open_drift"] == 1
    assert payload["counts"]["blocking_unknowns"] == 1
    run = payload["runs"][0]
    assert run["id"] == "ob-existing"
    assert run["records"]["segment"][0]["paths"] == [
        "src/agenttalk/cli.py", "README.md"
    ]
    assert "SECRET BUS BODY" not in json.dumps(payload)

    srv, _t, base = _serve(s)
    try:
        with _get(f"{base}/api/onboarding") as resp:
            wire = json.loads(resp.read().decode("utf-8"))
    finally:
        srv.shutdown()
        srv.server_close()
    assert wire["runs"][0]["id"] == "ob-existing"


def test_api_onboarding_empty_state_shape(tmp_path: Path) -> None:
    s = Store(tmp_path)
    s.init(["alpha", "beta"])

    payload = web.build_onboarding(web.RootDescriptor(s, "root"))

    assert payload["schema_version"] == 1
    assert payload["counts"]["total"] == 0
    assert payload["counts"]["showing"] == 0
    assert payload["runs"] == []
    assert payload["problems"] == []


def test_api_onboarding_needs_human_claim_blocks_run(tmp_path: Path) -> None:
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    with s._config_lock():
        ob.write_event_locked(s, ob.new_create_event(
            run_id="ob-human",
            title="Human-needed claim",
            objective=None,
            base_ref=None,
            lead="alpha",
            state="scanning",
            at="2026-07-09T12:00:00Z",
        ))
        ob.write_event_locked(s, ob.new_record_event(
            run_id="ob-human",
            kind=ob.KIND_CLAIM,
            key="release.owner",
            status="needs-human",
            summary="Need operator to confirm release owner.",
            actor="beta",
            source="human",
            confidence="medium",
            at="2026-07-09T12:01:00Z",
        ))

    payload = web.build_onboarding(web.RootDescriptor(s, "root"))

    assert payload["counts"]["blocked"] == 1
    assert payload["counts"]["needs_human_claims"] == 1
    assert payload["counts"]["human_needed"] == 1
    run = payload["runs"][0]
    assert run["blocked"] is True
    assert run["counts"]["needs_human_claims"] == 1
    assert run["records"]["claim"][0]["status"] == "needs-human"
    assert run["records"]["unknown"] == []


def test_api_onboarding_route_rejects_bad_filters(tmp_path: Path) -> None:
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    srv, _t, base = _serve(s)
    try:
        for suffix, code in (
            ("?limit=nope", "bad_limit"),
            ("?limit=0", "bad_limit"),
            ("?root=no-such-root", "bad_root"),
        ):
            with pytest.raises(urllib.error.HTTPError) as exc:
                _get(f"{base}/api/onboarding{suffix}")
            assert exc.value.code == 400
            payload = json.loads(exc.value.read().decode("utf-8"))
            assert payload["error"] == code
            assert payload["runs"] == []
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_onboarding_degrades_on_corrupt_ledger_lines(tmp_path: Path) -> None:
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    _write_onboarding_run(s)
    path = ob.events_path(s, "ob-existing")
    path.write_text(path.read_text(encoding="utf-8") + "{bad\n", encoding="utf-8")

    payload = web.build_onboarding(web.RootDescriptor(s, "root"))

    assert payload["counts"]["total"] == 1
    assert payload["counts"]["invalid_lines"] == 1
    assert payload["runs"][0]["problems"]


# ---------------------------------------------------------- /api/attention

def _attention(base: str) -> dict:
    with _get(f"{base}/api/attention") as resp:
        assert resp.status == 200
        assert resp.headers["Content-Type"].startswith("application/json")
        return json.loads(resp.read())


def test_api_attention_surfaces_process_tree_hold_without_liaison(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agenttalk import attention as attention_mod
    from agenttalk import supervisor as supervisor_mod

    s = _make_store(tmp_path)
    assert s.operator_facing() is None
    assert s.sole_lead() is None
    launch_args = [
        "-m", "agenttalk", "--root", "{ROOT}", "wrap", "--for", "alpha",
        "--loop", "--", r"C:\Program Files\Codex\codex.exe",
    ]
    (s.dir / "supervisor.json").write_text(
        json.dumps({
                "agents": {
                    "alpha": {
                        "wrapped": True,
                        "cli": "codex",
                        "cwd": str(tmp_path / "alpha cwd"),
                    "launch": {
                        "windows_file": r"C:\Python\python.exe",
                        "windows_args": launch_args,
                    },
                },
            },
        }),
        encoding="utf-8",
    )
    assert not (tmp_path / "alpha cwd").exists()
    entries = [
        {
            "pid": 100 + index,
            "start": f"linux:{'a' * 32}:{index + 1}",
            "start_filetime": None,
            "role": "wrapper" if index == 0 else "tool_descendant",
            "parent_pid": 0 if index == 0 else 99 + index,
            "discovered_at": "2026-06-01T00:00:00Z",
        }
        for index in range(64)
    ]
    supervisor_mod.save_supervisor_state(
        s.dir / "supervisor-state.json",
        {
            "agents": {
                "alpha": {
                    "launcher_pid": 100,
                    "launcher_start": f"linux:{'a' * 32}:1",
                    "launcher_nonce": "12345678-1234-4234-8234-123456789abc",
                    "runtime_wrapper_generation": "wrapper-1",
                    "owned_process_tree": {
                        "schema_version": 2,
                        "attribution_model": "owned_process_tree_v2",
                        "agent": "alpha",
                        "root_key": str(s.root),
                        "status": "truncated",
                        "reason_code": "process_tree_truncated",
                        "limit": 64,
                        "observed_count": 1_000_065,
                        "recorded_count": 64,
                        "omitted_count": 1_000_001,
                        "rejected_count": 1_000_001,
                        "truncated": True,
                        "refreshed_at": "2026-06-01T00:00:00Z",
                        "wrapper_generation": "wrapper-1",
                        "launch_nonce": "12345678-1234-4234-8234-123456789abc",
                        "entries": entries,
                    }
                },
                "beta": {
                    "owned_process_tree": {
                        "schema_version": 2,
                        "attribution_model": "owned_process_tree_v2",
                        "agent": "beta",
                        "root_key": supervisor_mod._root_key(  # noqa: SLF001
                            str(s.root.resolve())
                        ),
                        "status": "invalid",
                        "reason_code": (
                            "process_tree_invalid_wrapper_state_mismatch"
                        ),
                        "observed_count": 1,
                        "recorded_count": 0,
                        "omitted_count": 1,
                        "limit": 64,
                        "truncated": True,
                        "refreshed_at": "2026-06-01T00:00:00Z",
                        "wrapper_generation": None,
                        "launch_nonce": None,
                        "entries": [],
                    },
                },
            }
        },
    )
    (s.state_dir / "alpha.restart-request").write_text(
        '{"agent": "alpha"}',
        encoding="utf-8",
    )
    (s.state_dir / "beta.restart-request").write_text(
        '{"agent": "beta"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        supervisor_mod,
        "evaluate_process_tree_reset_admissions",
        lambda *_args, **_kwargs: {
            "evaluated": True,
            "admissions": {},
            "blocked_admissions": {
                "alpha": {
                    "mode": "configured_reset",
                    "agent": "alpha",
                    "missing_precondition": "supervisor_kill_switch_absent",
                },
            },
        },
    )

    collected = web._collect_web_attention_items(
        s,
        ["alpha", "beta"],
        None,
    )
    internal = next(
        item
        for item in collected
        if item["source"] == attention_mod.SOURCE_PROCESS_TREE_HOLD
    )

    srv, _t, base = _serve(s)
    try:
        payload = _attention(base)
    finally:
        srv.shutdown()
        srv.server_close()

    wire = next(
        item for item in payload["items"]
        if item["id"] == "process_tree_hold:alpha"
    )
    missing_accounting_wire = next(
        item for item in payload["items"]
        if item["id"] == "process_tree_hold:beta"
    )
    assert {
        item["id"]
        for item in payload["items"]
        if item["source_label"] == "SUPERVISOR HOLD"
    } == {"process_tree_hold:alpha", "process_tree_hold:beta"}
    assert wire == {
        "id": internal["item_id"],
        "source": "supervisor",
        "source_label": "SUPERVISOR HOLD",
        "severity": "high",
        "title": internal["title"],
        "agent": "alpha",
        "detail": internal["why_it_matters"],
        "recommendation": web._envelope_str(internal["recommendation"]),
        "configured_launch": internal["configured_launch"],
        "restart_request": internal["restart_request"],
        "age_seconds": 0.0,
        "human_can_unblock_now": True,
    }
    assert wire["restart_request"] == {
        "request_id": None,
        "state": "blocked_by_process_tree_hold",
        "pending_progress": False,
        "unavailable": True,
    }
    assert wire["configured_launch"] == {
        "source": "supervisor.json",
        "mode": "detached",
        "argv": [
            r"C:\Python\python.exe",
            *[
                str(tmp_path) if token == "{ROOT}" else token
                for token in launch_args
            ],
            ],
            "cwd": str(tmp_path / "alpha cwd"),
            "environment": {
                "AGENTTALK_ROOT": str(tmp_path),
                "AGENTTALK_PY": r"C:\Python\python.exe",
                "supervisor_json_env_keys": [],
            },
            "environment_note": (
                "No prepared binding exists to compare. Recreate values; recover null "
                "AGENTTALK_PY from the artifact; supervisor may add CODEX_HOME/log "
                "paths. Relative cwd is emitted unchanged: use the supervisor's base "
                "or the agent may start elsewhere. Absolute has no such base hazard; "
                "existence is unchecked."
            ),
    }
    assert "no scripted remedy applies in this state" in wire["recommendation"]
    assert "Operator must confirm" in wire["recommendation"]
    assert "omits >1,000,000 identities" in wire["recommendation"]
    assert "excludes >1,000,000 candidates" in wire["recommendation"]
    assert ".agenttalk/supervisor.kill" in wire["recommendation"]
    assert "Create it while the supervisor remains stopped" in (
        wire["recommendation"]
    )
    assert "UNKNOWN, not zero" in missing_accounting_wire["recommendation"]
    assert "Ownership record carries no rejected-candidate accounting" in (
        missing_accounting_wire["recommendation"]
    )
    assert "Operator must confirm" in missing_accounting_wire["recommendation"]
    assert "configured_launch" not in missing_accounting_wire
    assert missing_accounting_wire["configured_launch_unavailable"] == (
        "the agent has no supervisor.json launch entry"
    )
    assert missing_accounting_wire["restart_request"] == {
        "request_id": None,
        "state": "blocked_by_process_tree_hold",
        "pending_progress": False,
        "unavailable": True,
    }
    assert "operator_command" not in wire

    monkeypatch.setattr(
        supervisor_mod,
        "evaluate_process_tree_reset_admissions",
        lambda *_args, **_kwargs: {
            "evaluated": True,
            "admissions": {
                "alpha": {
                    "mode": "configured_reset",
                    "agent": "alpha",
                    "actor": "lead",
                    "verified_launch_nonce": (
                        "12345678-1234-4234-8234-123456789abc"
                    ),
                    "reason": "all recorded process identities verified stopped",
                },
            },
        },
    )
    admitted_payload = web.build_attention(web.RootDescriptor(s, "root"))
    admitted_wire = next(
        item
        for item in admitted_payload["items"]
        if item["id"] == "process_tree_hold:alpha"
    )
    assert admitted_wire["operator_argv"][:4] == [
        "agenttalk", "--root", str(tmp_path), "supervise",
    ]


def test_api_attention_hides_resolved_dead_letter_and_keeps_unresolved(
    tmp_path: Path,
) -> None:
    from agenttalk import cli as cli_mod
    from agenttalk.wrapper import recv_api

    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    s.set_operator_facing("alpha")

    def dead_letter(body: str) -> str:
        message = s.send(
            sender="alpha", recipient="beta", body=body, kind="message", meta={}
        )
        record = recv_api.next_record(s, "beta")
        assert record["id"] == message.id
        s.dead_letter(
            "beta",
            record,
            reason="turn failed deterministically",
            failure_class="poison_eligible",
            at="2026-07-12T00:00:00Z",
        )
        return message.id

    resolved_id = dead_letter("resolved poison")
    unresolved_id = dead_letter("unresolved poison")
    assert cli_mod.main([
        "--root", str(tmp_path),
        "dead-letter", "resolve",
        "--from", "alpha",
        "--agent", "beta",
        "--id", resolved_id,
        "--reason", "handled out of band",
    ]) == 0

    srv, _t, base = _serve(s)
    try:
        payload = _attention(base)
    finally:
        srv.shutdown()
        srv.server_close()

    dead_letters = {
        item["id"] for item in payload["items"] if item["source"] == "deadletter"
    }
    assert f"dead_letter:beta:{resolved_id}" not in dead_letters
    assert f"dead_letter:beta:{unresolved_id}" in dead_letters


def test_api_attention_shape_and_gate_hold(tmp_path: Path) -> None:
    """§4a: /api/attention returns the ranked envelope, and a gate HOLD surfaces
    with the frozen wire fields. Envelope-only — no raw body leaks."""
    from agenttalk import gates as gmod
    s = _make_store(tmp_path)
    # a required, red blocker gate => a HOLD in check_gates().blockers, which
    # attention.gate_hold_items turns into a gate-source queue item.
    gmod.set_gate(s.root, name="ci", status="red", severity="blocker",
                  scope="release", actor="alpha", evidence_source="automation_ci",
                  reason="pipeline red", required=True)
    srv, _t, base = _serve(s)
    try:
        payload = _attention(base)
        assert set(payload) == {
            "root",
            "root_path",
            "root_info",
            "target_root_project_id",
            "items",
            "count",
        }
        assert payload["target_root_project_id"] == s.project_id()
        assert payload["root_info"] == {
            "project_id": s.project_id(),
            "label": s.root.name,
            "path": str(s.root),
        }
        assert payload["count"] == len(payload["items"])
        assert payload["root"] == s.root.name
        for it in payload["items"]:
            assert set(it) >= {"id", "source", "source_label", "severity",
                               "title", "agent", "detail", "age_seconds",
                               "human_can_unblock_now"}
            assert it["source"] in ("escalation", "gate", "stuck",
                                    "deadletter", "supervisor")
            assert it["severity"] in ("high", "med", "low")
        gate_items = [it for it in payload["items"] if it["source"] == "gate"]
        assert gate_items, "the gate HOLD should surface"
        assert gate_items[0]["severity"] == "high"
        _assert_no_body_keys(payload)
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_attention_actions_off_escalation_shape_is_legacy_fixture(
    tmp_path: Path,
) -> None:
    s = _make_store(tmp_path)
    s.set_operator_facing("beta")
    s.send(sender="alpha", recipient="beta", kind="question",
           subject="operator input needed", body="body must not leak",
           meta={"request_id": "esc-help", "needs_operator": "true",
                 "attention": {"priority": "urgent",
                               "decision": "Choose release path",
                               "recommendation": "ship the narrow fix",
                               "options": ["ship", "hold"]}})
    srv, _t, base = _serve(s)
    try:
        with _get(f"{base}/api/attention") as resp:
            raw = resp.read()
        payload = json.loads(raw)
        item = next(it for it in payload["items"] if it["source"] == "escalation")
        assert set(item) == {
            "id", "source", "source_label", "severity", "title", "agent",
            "detail", "age_seconds", "human_can_unblock_now",
        }
        assert item["title"] == "Choose release path"
        assert item["source_label"] == "ESCALATION"
        assert "body must not leak" not in raw.decode("utf-8")
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_attention_actions_on_adds_answer_annotations_for_liaison_only(
    tmp_path: Path,
) -> None:
    s = _make_store(tmp_path)
    s.set_operator_facing("beta")
    body = "Can I publish v0.72.1 now?\nCI is green but the old sdist was polluted."
    s.send(sender="alpha", recipient="beta", kind="question",
           subject="operator input needed", body=body,
           meta={"request_id": "esc-help", "needs_operator": "true",
                 "attention": {"priority": "urgent",
                               "decision": "Choose release path",
                               "recommendation": "ship the narrow fix",
                               "options": ["ship", "hold"]}})
    srv, _t, base = _serve(s, enable_actions=True)
    try:
        payload = _attention(base)
        item = next(it for it in payload["items"] if it["source"] == "escalation")
        assert item["answerable"] is True
        assert item["answer_escalation"] == {
            "to_request": "esc-help",
            "requester": "alpha",
        }
        assert item["actions"]["answer_escalation"]["kind"] == "answer_escalation"
        assert item["available_actions"] == [item["actions"]["answer_escalation"]]
        assert item["priority"] == "urgent"
        assert item["recommendation"] == "ship the narrow fix"
        assert item["options"] == ["ship", "hold"]
        assert item["prompt_excerpt"] == body
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_attention_prompt_excerpt_is_action_only_bounded_context(
    tmp_path: Path,
) -> None:
    s = _make_store(tmp_path)
    s.set_operator_facing("beta")
    body = "Question text " + ("x" * 1300)
    s.send(sender="alpha", recipient="beta", kind="question",
           subject="operator input needed", body=body,
           meta={"request_id": "esc-help", "needs_operator": "true"})

    srv, _t, base = _serve(s)
    try:
        item = next(it for it in _attention(base)["items"] if it["source"] == "escalation")
        assert "prompt_excerpt" not in item
    finally:
        srv.shutdown()
        srv.server_close()

    srv2, _t2, base2 = _serve(s, enable_actions=True)
    try:
        item2 = next(it for it in _attention(base2)["items"] if it["source"] == "escalation")
        assert item2["answerable"] is True
        assert item2["prompt_excerpt"].startswith("Question text ")
        assert len(item2["prompt_excerpt"]) == web._ATTENTION_PROMPT_MAX
        assert item2["prompt_excerpt"] == web._attention_prompt_excerpt(body)
    finally:
        srv2.shutdown()
        srv2.server_close()

    s2 = _make_store(tmp_path / "requester-view")
    s2.set_operator_facing("alpha")
    s2.send(sender="alpha", recipient="beta", kind="question",
            subject="operator input needed", body="body",
            meta={"request_id": "esc-help", "needs_operator": "true"})
    srv2, _t2, base2 = _serve(s2, enable_actions=True)
    try:
        payload2 = _attention(base2)
        assert not [it for it in payload2["items"] if it.get("answerable")]
    finally:
        srv2.shutdown()
        srv2.server_close()


def test_api_attention_derived_stuck_item(tmp_path: Path) -> None:
    """§4a: a per-agent STUCK item is derived for any agent whose advisory
    health.state == stuck_suspected (stuck is NOT a build_queue source)."""
    s = _make_store(tmp_path)
    s.write_heartbeat("alpha")
    _write_health(s, "alpha", _hm.STATE_STUCK_SUSPECTED, cli="claude", mode="wrapper-loop")
    srv, _t, base = _serve(s)
    try:
        payload = _attention(base)
        stuck = [it for it in payload["items"] if it["source"] == "stuck"]
        assert len(stuck) == 1
        assert stuck[0]["source_label"] == "STUCK"
        assert stuck[0]["severity"] == "med"
        assert stuck[0]["agent"] == "alpha"
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_attention_derived_worktree_stall_item(tmp_path: Path) -> None:
    s = _make_store(tmp_path)
    s.write_heartbeat("alpha")
    _write_health(
        s,
        "alpha",
        _hm.STATE_ERRORED_AMBIGUOUS,
        cli="codex",
        mode="wrapper-loop",
        reason_code="worktree_branch_already_checked_out",
    )
    srv, _t, base = _serve(s)
    try:
        payload = _attention(base)
        stalled = [it for it in payload["items"] if it["source"] == "stuck"]
        assert len(stalled) == 1
        assert stalled[0]["source_label"] == "STALLED"
        assert stalled[0]["severity"] == "med"
        assert stalled[0]["agent"] == "alpha"
        assert "branch already checked out" in stalled[0]["detail"]
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_attention_empty_is_all_clear(tmp_path: Path) -> None:
    """§4a: a clean project yields an empty queue (the client shows 'All clear')."""
    s = _make_store(tmp_path)
    srv, _t, base = _serve(s)
    try:
        payload = _attention(base)
        assert payload["items"] == []
        assert payload["count"] == 0
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_attention_degrades_on_corrupt_root(tmp_path: Path) -> None:
    """§4a (review B1): a corrupt/uninitialized root must NOT 500 /api/attention.

    Parity with /api/state's errors-as-data (test_api_state_corrupt_root_isolated):
    the config reads that back the queue — chiefly ``operator_facing()``/
    ``sole_lead()``, which raise ``JSONDecodeError`` on this corrupt config — sit
    behind a fail-safe, so the route returns a well-formed 200 JSON envelope, never
    an HTTP 500 with an HTML traceback. The remaining per-source reads degrade to
    bounded ``source_error`` items (the pre-existing granular fail-safe), so the
    queue is either empty-with-errors or carries only supervisor-severity source
    errors — in every case a valid, body-free envelope."""
    s = _make_store(tmp_path)
    cfg_path = s.dir / "config.json"
    cfg_path.write_text("{not json", encoding="utf-8")
    srv, _t, base = _serve(s)
    try:
        # 200, not 500 (the fix: for_agent resolution can no longer escape).
        with _get(f"{base}/api/attention") as resp:
            assert resp.status == 200
            assert resp.headers["Content-Type"].startswith("application/json")
            payload = json.loads(resp.read())
        # Well-formed envelope; count matches; body-free.
        assert set(payload) >= {"root", "items", "count"}
        assert payload["count"] == len(payload["items"])
        _assert_no_body_keys(payload)
        # Degraded either to an empty queue + errors field OR to bounded
        # source_error items — never a partial/garbage item, never a 500.
        assert payload["items"] == [] or all(
            it["id"].startswith("source_error:") for it in payload["items"])
    finally:
        srv.shutdown()
        srv.server_close()


# -------------------------------------------------------- /api/threads closed

def test_api_threads_closed_terminal_only_paginated_envelope(
    tmp_path: Path,
) -> None:
    """v0.61.0: closed history is on-demand, terminal-only, newest-first,
    cursor-paginated, and envelope-only. Active rows remain in /api/state, not
    /api/threads?state=closed."""
    s = _make_store(tmp_path)
    closed_q = _hand_write_message(s, {
        "id": "20990101-000000-000001-CLSO",
        "ts": "2099-01-01T00:00:01Z",
        "from": "alpha",
        "to": "beta",
        "kind": "question",
        "subject": "closed question",
        "body": "CLOSED_OPENER_BODY_TOKEN",
        "meta": {"request_id": "rid-closed"},
    })
    _hand_write_message(s, {
        "id": "20990101-000000-000002-CLSA",
        "ts": "2099-01-01T00:00:02Z",
        "from": "beta",
        "to": "alpha",
        "kind": "message",
        "subject": "closed answer",
        "body": "CLOSED_ANSWER_BODY_TOKEN",
        "meta": {"request_id": "rid-closed"},
    })
    _hand_write_message(s, {
        "id": "20990101-000000-000003-SUPO",
        "ts": "2099-01-01T00:00:03Z",
        "from": "alpha",
        "to": "beta",
        "kind": "question",
        "subject": "superseded question",
        "body": "SUPERSEDED_OPENER_BODY_TOKEN",
        "meta": {"request_id": "rid-sup"},
    })
    superseded_last = _hand_write_message(s, {
        "id": "20990101-000000-000004-SUPR",
        "ts": "2099-01-01T00:00:04Z",
        "from": "alpha",
        "to": "beta",
        "kind": "rescind",
        "subject": "rescinded",
        "body": "SUPERSEDED_RESCIND_BODY_TOKEN",
        "meta": {
            "request_id": "rid-sup",
            "target_msg_id": "20990101-000000-000003-SUPO",
        },
    })
    # Mark the terminal examples consumed; append a newer active opener after
    # the cursor floor so it remains active and must not enter the history page.
    s.set_cursor("alpha", superseded_last)
    s.set_cursor("beta", superseded_last)
    _hand_write_message(s, {
        "id": "20990101-000000-000005-ACTV",
        "ts": "2099-01-01T00:00:05Z",
        "from": "alpha",
        "to": "beta",
        "kind": "question",
        "subject": "still active",
        "body": "ACTIVE_BODY_TOKEN",
        "meta": {"request_id": "rid-active"},
    })

    srv, _t, base = _serve(s)
    try:
        with _get(f"{base}/api/threads?state=closed&limit=1") as resp:
            assert resp.status == 200
            assert resp.headers["Content-Security-Policy"] == _LEGACY_CSP
            first = json.loads(resp.read())
        assert first["root"] == s.root.name
        assert first["state"] == "closed"
        assert first["limit"] == 1
        assert first["total_count"] == 2
        assert first["next_cursor"] == "20990101-000000-000004-SUPR"
        assert [item["request_id"] for item in first["items"]] == ["rid-sup"]
        sup = first["items"][0]
        assert sup["state"] == "closed-superseded"
        assert sup["opener"] == "alpha"
        assert sup["opener_peer"] == "beta"
        assert sup["last_msg_id"] == "20990101-000000-000004-SUPR"
        assert sup["last_ts"] == "2099-01-01T00:00:04Z"

        dumped_first = json.dumps(first)
        assert "body" not in dumped_first
        assert "rescind" not in dumped_first
        assert "SUPERSEDED_RESCIND_BODY_TOKEN" not in dumped_first
        assert "ACTIVE_BODY_TOKEN" not in dumped_first

        with _get(
            f"{base}/api/threads?state=closed&limit=1"
            f"&cursor={first['next_cursor']}"
        ) as resp:
            second = json.loads(resp.read())
        assert second["next_cursor"] is None
        assert [item["request_id"] for item in second["items"]] == ["rid-closed"]
        assert second["items"][0]["state"] == "closed"
        assert second["items"][0]["last_msg_id"] == "20990101-000000-000002-CLSA"
        assert closed_q not in {item["last_msg_id"] for item in second["items"]}

        (root,) = _state(base)["roots"]
        assert root["counts"]["closed_threads"] == 2
        assert [row["request_id"] for row in root["threads"]] == ["rid-active"]
        _assert_no_body_keys(root)

        with _get(f"{base}/api/thread/rid-closed") as resp:
            transcript = json.loads(resp.read())
        assert "CLOSED_ANSWER_BODY_TOKEN" in json.dumps(transcript)
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_threads_query_validation_limit_cap_and_root_scope(
    tmp_path: Path,
) -> None:
    a, b = _make_two_stores(tmp_path)
    label0, label1 = a.root.name, b.root.name
    _hand_write_message(b, {
        "id": "20990101-000000-000001-BOPN",
        "ts": "2099-01-01T00:00:01Z",
        "from": "lead",
        "to": "dev",
        "kind": "question",
        "subject": "root one closed",
        "body": "ROOT1_CLOSED_OPENER_BODY",
        "meta": {"request_id": "rid-root1"},
    })
    last = _hand_write_message(b, {
        "id": "20990101-000000-000002-BANS",
        "ts": "2099-01-01T00:00:02Z",
        "from": "dev",
        "to": "lead",
        "kind": "message",
        "subject": "root one answer",
        "body": "ROOT1_CLOSED_ANSWER_BODY",
        "meta": {"request_id": "rid-root1"},
    })
    b.set_cursor("lead", last)
    b.set_cursor("dev", last)
    srv, _t, base = _serve_multi(a, b)
    try:
        with _get(f"{base}/api/threads?root={label1}&state=closed&limit=1000") as resp:
            payload = json.loads(resp.read())
        assert payload["root"] == label1
        assert payload["limit"] == 100
        assert payload["total_count"] == 1
        assert payload["items"][0]["request_id"] == "rid-root1"
        assert "ROOT1_CLOSED" not in json.dumps(payload)

        with _get(f"{base}/api/threads?root={label0}&state=closed") as resp:
            root0 = json.loads(resp.read())
        assert root0["root"] == label0
        assert root0["items"] == []

        bad_queries = (
            ("bad_state", f"{base}/api/threads?state=open"),
            ("bad_limit", f"{base}/api/threads?state=closed&limit=zero"),
            ("bad_limit", f"{base}/api/threads?state=closed&limit=0"),
            ("bad_cursor", f"{base}/api/threads?state=closed&cursor=..%2Fbad"),
            ("bad_root", f"{base}/api/threads?state=closed&root=missing-root"),
        )
        for code, url in bad_queries:
            with pytest.raises(urllib.error.HTTPError) as exc:
                _urlopen(url, timeout=5)
            assert exc.value.code == 400
            body = json.loads(exc.value.read())
            assert body["error"] == code
            assert body["items"] == []
            assert body["state"] == "closed"
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_threads_failure_is_bounded_and_state_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _make_store(tmp_path)
    s.send(sender="alpha", recipient="beta", kind="question",
           subject="active", body="ACTIVE_BODY_TOKEN",
           meta={"request_id": "rid-active"})
    srv, _t, base = _serve(s)
    original = web._validated_for_state

    def fail_validation(*_args, **_kwargs):
        raise RuntimeError("boom " + ("x" * 200))

    try:
        monkeypatch.setattr(web, "_validated_for_state", fail_validation)
        with _get(f"{base}/api/threads?state=closed") as resp:
            assert resp.status == 200
            payload = json.loads(resp.read())
        assert payload["error"] == "threads_unavailable"
        assert payload["items"] == []
        assert payload["total_count"] == 0
        assert len(payload["detail"]) < 260
        assert "ACTIVE_BODY_TOKEN" not in json.dumps(payload)

        monkeypatch.setattr(web, "_validated_for_state", original)
        (root,) = _state(base)["roots"]
        assert [row["request_id"] for row in root["threads"]] == ["rid-active"]
        _assert_no_body_keys(root)
    finally:
        srv.shutdown()
        srv.server_close()


# --------------------------------------------------------- /api/thread/<rid>

def test_api_thread_returns_raw_bodies_and_safe_meta_line(tmp_path: Path) -> None:
    """§4b: /api/thread/<rid> carries RAW (un-escaped) bodies for textContent
    rendering, and a meta_line derived from a whitelist (status/head/base) only —
    never arbitrary meta."""
    s = _make_store(tmp_path)
    s.send(sender="alpha", recipient="beta", kind="review-request",
           subject="please review", body="## Goal\n<b>not escaped</b>",
           meta={"request_id": "rid-t"})
    s.send(sender="beta", recipient="alpha", kind="review-result",
           subject="done", body="looks good",
           meta={"request_id": "rid-t", "status": "approved",
                 "head": "7b2d9c1", "secret_prose": "SHOULD NOT LEAK"})
    srv, _t, base = _serve(s)
    try:
        with _get(f"{base}/api/thread/rid-t") as resp:
            assert resp.status == 200
            assert resp.headers["Content-Security-Policy"] == _LEGACY_CSP
            payload = json.loads(resp.read())
        assert payload["request_id"] == "rid-t"
        assert payload["subject"] == "please review"
        assert payload["kind"] == "review-request"
        assert payload["participants"] == ["alpha", "beta"]
        msgs = payload["messages"]
        assert [m["id"] for m in msgs] == sorted(m["id"] for m in msgs)  # id asc
        # body is RAW — NOT html-escaped (client renders via textContent)
        assert msgs[0]["body"] == "## Goal\n<b>not escaped</b>"
        # cli is inferred from the sender-name PREFIX only; 'alpha' has no
        # claude-/codex- prefix, so it is null (not guessed).
        assert msgs[0]["cli"] is None
        # meta_line: whitelist only; the non-whitelisted key must NOT appear
        ml = msgs[1]["meta_line"]
        assert "status=approved" in ml
        assert "7b2d9c1" in ml
        assert "SHOULD NOT LEAK" not in json.dumps(payload)
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_thread_unknown_rid_404(tmp_path: Path) -> None:
    """§4b: an unknown rid (no messages) 404s with the error envelope."""
    s = _make_store(tmp_path)
    srv, _t, base = _serve(s)
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            _urlopen(f"{base}/api/thread/does-not-exist", timeout=5)
        assert exc.value.code == 404
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_thread_rejects_traversal_rid(tmp_path: Path) -> None:
    """§4b: the rid is validated against _MESSAGE_ID_RE BEFORE any disk touch —
    a traversal attempt 404s (parity with /messages/<id>)."""
    s = _make_store(tmp_path)
    srv, _t, base = _serve(s)
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            _urlopen(f"{base}/api/thread/..%2F..%2Fetc%2Fpasswd", timeout=5)
        assert exc.value.code == 404
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_thread_excludes_forged_and_out_of_roster(tmp_path: Path) -> None:
    """§4b (P1): /api/thread applies the SAME validation surface as /api/state —
    roster + kind (+ HMAC when enforced). A forged out-of-roster message and an
    unknown-kind message that both carry the thread's request_id must NEVER enter
    the transcript, even though they share the rid."""
    s = _make_store(tmp_path)
    s.send(sender="alpha", recipient="beta", kind="review-request",
           subject="review X", body="LEGIT_THREAD_BODY_TOKEN",
           meta={"request_id": "rid-x"})
    # Forged out-of-roster sender AND unknown kind — both tagged with the rid.
    _hand_write_message(s, {
        "id": "20990101-000000-000001-FRGx", "ts": "2099-01-01T00:00:00Z",
        "from": "mallory", "to": "beta", "kind": "message",
        "subject": "", "body": "FORGED_ROSTER_BODY_TOKEN",
        "meta": {"request_id": "rid-x"},
    })
    _hand_write_message(s, {
        "id": "20990101-000000-000002-UNKx", "ts": "2099-01-01T00:00:00Z",
        "from": "alpha", "to": "beta", "kind": "execute-now",
        "subject": "", "body": "UNKNOWN_KIND_BODY_TOKEN",
        "meta": {"request_id": "rid-x"},
    })
    srv, _t, base = _serve(s)
    try:
        with _get(f"{base}/api/thread/rid-x") as resp:
            assert resp.status == 200
            payload = json.loads(resp.read())
        ids = [m["id"] for m in payload["messages"]]
        assert len(ids) == 1  # only the legit review-request
        assert "20990101-000000-000001-FRGx" not in ids
        assert "20990101-000000-000002-UNKx" not in ids
        dumped = json.dumps(payload)
        assert "LEGIT_THREAD_BODY_TOKEN" in dumped
        assert "FORGED_ROSTER_BODY_TOKEN" not in dumped
        assert "UNKNOWN_KIND_BODY_TOKEN" not in dumped
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_thread_root_scoped(tmp_path: Path) -> None:
    """§4b (review P2-5): /api/thread is root-scoped via ?root=<label>. Two roots
    carrying the SAME request_id but different bodies must NOT bleed across each
    other, and a rid that exists ONLY in root1 is not resolvable without the
    correct ?root (binding root[0] unconditionally would 404 it or leak root[0])."""
    a, b = _make_two_stores(tmp_path)
    label0, label1 = a.root.name, b.root.name
    # Same request_id "rid-shared" in BOTH roots, distinct bodies.
    a.send(sender="alpha", recipient="beta", kind="note",
           subject="root0 subject", body="ROOT0_BODY_TOKEN",
           meta={"request_id": "rid-shared"})
    b.send(sender="lead", recipient="dev", kind="note",
           subject="root1 subject", body="ROOT1_BODY_TOKEN",
           meta={"request_id": "rid-shared"})
    # A rid that exists ONLY in root1.
    b.send(sender="lead", recipient="dev", kind="note",
           subject="only root1", body="ONLY_ROOT1_BODY",
           meta={"request_id": "rid-only1"})
    srv, _t, base = _serve_multi(a, b)
    try:
        # ?root=<label1> resolves root1's copy of the shared rid.
        with _get(f"{base}/api/thread/rid-shared?root={label1}") as resp:
            assert resp.status == 200
            p1 = json.loads(resp.read())
        assert p1["subject"] == "root1 subject"
        assert "ROOT1_BODY_TOKEN" in json.dumps(p1)
        assert "ROOT0_BODY_TOKEN" not in json.dumps(p1)

        # ?root=<label0> (and the no-root default) resolves root0's copy — no bleed.
        with _get(f"{base}/api/thread/rid-shared?root={label0}") as resp:
            assert resp.status == 200
            p0 = json.loads(resp.read())
        assert p0["subject"] == "root0 subject"
        assert "ROOT0_BODY_TOKEN" in json.dumps(p0)
        assert "ROOT1_BODY_TOKEN" not in json.dumps(p0)
        with _get(f"{base}/api/thread/rid-shared") as resp:  # no ?root -> root[0]
            assert resp.status == 200
            pdef = json.loads(resp.read())
        assert pdef["subject"] == "root0 subject"

        # A root1-only rid is NOT resolvable against root[0] (404), but IS with
        # the correct ?root — proves the binding follows the selected root.
        with pytest.raises(urllib.error.HTTPError) as exc:
            _urlopen(f"{base}/api/thread/rid-only1", timeout=5)
        assert exc.value.code == 404
        with _get(f"{base}/api/thread/rid-only1?root={label1}") as resp:
            assert resp.status == 200
            ponly = json.loads(resp.read())
        assert ponly["subject"] == "only root1"
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_thread_excludes_unsigned_when_hmac_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§4b (P1, HMAC variant): with signing enforced, an unsigned message
    carrying the thread's rid must NOT enter the transcript. Mirrors
    test_invalid_signature_body_not_rendered on the /api/thread surface."""
    monkeypatch.setenv("AGENTTALK_HMAC_KEY_FILE", str(tmp_path / "k.key"))
    s = _make_store(tmp_path)
    signing.init_key(s.project_id())
    s.send(sender="alpha", recipient="beta", kind="review-request",
           subject="review X", body="SIGNED_THREAD_BODY_TOKEN",
           meta={"request_id": "rid-x"})
    _hand_write_message(s, {
        "id": "20990101-000000-000003-UNSx", "ts": "2099-01-01T00:00:00Z",
        "from": "alpha", "to": "beta", "kind": "message",
        "subject": "", "body": "UNSIGNED_THREAD_BODY_TOKEN",
        "meta": {"request_id": "rid-x"},
    })
    srv, _t, base = _serve(s)
    try:
        with _get(f"{base}/api/thread/rid-x") as resp:
            payload = json.loads(resp.read())
        ids = [m["id"] for m in payload["messages"]]
        assert "20990101-000000-000003-UNSx" not in ids
        dumped = json.dumps(payload)
        assert "SIGNED_THREAD_BODY_TOKEN" in dumped
        assert "UNSIGNED_THREAD_BODY_TOKEN" not in dumped
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_state_recent_feed(tmp_path: Path) -> None:
    """0.58.0 (P1): root.recent is the last _RECENT_LIMIT (25) messages,
    ENVELOPE-ONLY (no body), most-recent-first (ids descending)."""
    s = _make_store(tmp_path)
    for i in range(30):
        s.send(sender="alpha", recipient="beta",
               subject=f"subj-{i:02d}", body=f"RECENT_BODY_TOKEN_{i:02d}")
    srv, _t, base = _serve(s)
    try:
        (root,) = _state(base)["roots"]
        recent = root["recent"]
        assert len(recent) == 25
        ids = [r["id"] for r in recent]
        assert ids == sorted(ids, reverse=True)  # most-recent-first
        for r in recent:
            assert set(r) == {"id", "ts", "from", "to", "kind", "subject"}
            assert "body" not in r
        assert "RECENT_BODY_TOKEN" not in json.dumps(recent)
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_state_no_body_content_sentinel(tmp_path: Path) -> None:
    """0.58.0 (review P2-7): the 'no body on /api/state' contract is enforced by
    CONTENT, not just key name. A message whose BODY is a distinctive sentinel
    must not appear ANYWHERE in the serialized /api/state (subject/meta/etc.)."""
    sentinel = "ZZZ_BODY_SENTINEL_QWE"
    s = _make_store(tmp_path)
    s.send(sender="alpha", recipient="beta", kind="review-request",
           subject="ordinary subject", body=sentinel,
           meta={"request_id": "rid-s"})
    srv, _t, base = _serve(s)
    try:
        state = _state(base)
        assert sentinel not in json.dumps(state)
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_state_verdict_latest_wins(tmp_path: Path) -> None:
    """0.58.0 §3b: the thread verdict is the LATEST decision on the rid; a
    malformed legacy proposal-response does not emit a verdict."""
    s = _make_store(tmp_path)
    # Current review-result statuses are validated at Store.send: latest wins.
    s.send(sender="alpha", recipient="beta", kind="review-request",
           subject="r", body="please review", meta={"request_id": "rid-v"})
    s.send(sender="beta", recipient="alpha", kind="review-result",
           subject="v1", body="ok", meta={"request_id": "rid-v", "status": "approved"})
    s.send(sender="beta", recipient="alpha", kind="review-result",
           subject="v2", body="wait", meta={"request_id": "rid-v",
                                            "status": "rejected"})
    # A current accepted response emits a verdict. Seed malformed legacy history
    # directly because the strict Store.send path correctly refuses status=maybe.
    s.send(sender="alpha", recipient="beta", kind="proposal",
           subject="p", body="do X", meta={"request_id": "rid-p"})
    s.send(sender="beta", recipient="alpha", kind="proposal-response",
           subject="pr", body="sure", meta={"request_id": "rid-p", "status": "accepted"})
    s.send(sender="alpha", recipient="beta", kind="proposal",
           subject="p2", body="do Y", meta={"request_id": "rid-q"})
    _hand_write_message(s, {
        "id": "20990101-000000-000000-MAYB",
        "ts": "2099-01-01T00:00:00Z",
        "from": "beta",
        "to": "alpha",
        "kind": "proposal-response",
        "subject": "pr2",
        "body": "hmm",
        "meta": {"request_id": "rid-q", "status": "maybe"},
    })
    srv, _t, base = _serve(s)
    try:
        (root,) = _state(base)["roots"]
        by_rid = {t["request_id"]: t for t in root["threads"]}
        assert by_rid["rid-v"]["verdict"] == "rejected"
        assert by_rid["rid-p"]["verdict"] == "accepted"
        assert "verdict" not in by_rid["rid-q"]  # unknown status -> omitted
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_state_wrapper_one_shot_is_wrapped(tmp_path: Path) -> None:
    """0.58.x (P3): a 'wrapper-one-shot' health mode must count as wrapped
    (and therefore restartable), not fall through to omitted."""
    s = _make_store(tmp_path)
    s.write_heartbeat("alpha")
    _write_health(s, "alpha", _hm.STATE_WORKING_TURN,
                  cli="claude", mode="wrapper-one-shot")
    srv, _t, base = _serve(s)
    try:
        (root,) = _state(base)["roots"]
        alpha = next(a for a in root["agents"] if a["name"] == "alpha")
        assert alpha["wrapped"] is True
        assert alpha["restartable"] is True
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_state_thread_carries_opener(tmp_path: Path) -> None:
    """0.58.x (P1): a thread row carries its two fixed endpoints — opener and
    opener_peer — perspective-independent envelope-safe agent names."""
    s = _make_store(tmp_path)
    s.send(sender="alpha", recipient="beta", kind="review-request",
           subject="review X", body="please review", meta={"request_id": "rid-o"})
    srv, _t, base = _serve(s)
    try:
        (root,) = _state(base)["roots"]
        (row,) = root["threads"]
        assert row["opener"] == "alpha"
        assert row["opener_peer"] == "beta"
    finally:
        srv.shutdown()
        srv.server_close()


# ------------------------------------------------------- /api/gates (§4c)
#
# Gate & Evidence Wall, read side (docs/PROPOSAL-console-client-sellability.md
# #1). Quick-win selection: 20260825-215757-153513-ljQ3.

def _gates(base: str) -> dict:
    with _get(f"{base}/api/gates") as resp:
        assert resp.status == 200
        assert resp.headers["Content-Type"].startswith("application/json")
        return json.loads(resp.read())


def test_api_gates_shape_evidence_and_waiver(tmp_path: Path) -> None:
    """A green blocker gate carries its evidence; a waived gate carries its
    waiver reason/expiry; a missing required gate blocks with a reason. The
    envelope carries the full picture /api/attention only ever summarizes as
    one blocker line."""
    from agenttalk import gates as gmod

    s = _make_store(tmp_path)
    gmod.set_gate(s.root, name="tests", status="green", severity="blocker",
                  scope="release", actor="alpha", evidence_source="automation_ci",
                  evidence=["ci-run-42"], reason="all green", required=True)
    gmod.waive_gate(s.root, name="security-scan", operator="alpha",
                    reason="scanner unavailable", scope="release",
                    expires="2099-01-01T00:00:00Z")
    srv, _t, base = _serve(s)
    try:
        payload = _gates(base)
        assert set(payload) == {
            "root", "root_path", "root_info", "target_root_project_id",
            "verdict", "required_gates", "gates", "count",
        }
        assert payload["count"] == len(payload["gates"])
        assert payload["required_gates"] == ["tests"]
        by_name = {g["name"]: g for g in payload["gates"]}

        tests_gate = by_name["tests"]
        assert tests_gate["status"] == "green"
        assert tests_gate["severity"] == "blocker"
        assert tests_gate["scope"] == "release"
        assert tests_gate["blocks"] is False
        assert tests_gate["waiver"] is None
        (evidence_entry,) = tests_gate["evidence"]
        assert evidence_entry["source"] == "automation_ci"
        assert evidence_entry["refs"] == ["ci-run-42"]
        assert evidence_entry["by"] == "alpha"
        assert "at" in evidence_entry

        scan_gate = by_name["security-scan"]
        assert scan_gate["status"] == "waived"
        assert scan_gate["blocks"] is False
        assert scan_gate["waiver"] == {
            "operator": "alpha",
            "date": scan_gate["waiver"]["date"],
            "reason": "scanner unavailable",
            "scope": "release",
            "expires": "2099-01-01T00:00:00Z",
        }

        # every required blocker gate is green -> GO
        assert payload["verdict"] == "GO"
        _assert_no_body_keys(payload)
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_gates_missing_required_gate_blocks(tmp_path: Path) -> None:
    from agenttalk import gates as gmod

    s = _make_store(tmp_path)
    state = gmod.load_gate_state(s.root)
    state["required_gates"] = ["never-run"]
    gmod.write_gate_state(s.root, state)
    srv, _t, base = _serve(s)
    try:
        payload = _gates(base)
        assert payload["verdict"] == "HOLD"
        (gate,) = payload["gates"]
        assert gate["name"] == "never-run"
        assert gate["status"] == "unknown"
        assert gate["blocks"] is True
        assert gate["reason"]
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_gates_degrades_on_corrupt_root(tmp_path: Path) -> None:
    """Parity with /api/attention's errors-as-data (B1): a corrupt gates.json
    must NOT 500 the route. gates.check_gates already fails closed for a
    corrupt state file with a single synthetic ``__gate_state__`` blocker
    (mirrored by attention.gate_hold_items) - build_gates must pass that
    through, not crash trying to read a non-existent evidence list for it."""
    s = _make_store(tmp_path)
    (s.dir / "gates.json").write_text("{not json", encoding="utf-8")
    srv, _t, base = _serve(s)
    try:
        payload = _gates(base)
        assert set(payload) >= {"root", "gates", "count", "verdict"}
        assert payload["count"] == len(payload["gates"])
        assert payload["verdict"] == "HOLD"
        (gate,) = payload["gates"]
        assert gate["name"] == "__gate_state__"
        assert gate["blocks"] is True
        assert gate["evidence"] == []
        _assert_no_body_keys(payload)
    finally:
        srv.shutdown()
        srv.server_close()


# --------------------------------------------------- /api/risk-register (§4e)
#
# Risk Register relabel (docs/PROPOSAL-console-client-sellability.md #6).

def _risk_register(base: str) -> dict:
    with _get(f"{base}/api/risk-register") as resp:
        assert resp.status == 200
        assert resp.headers["Content-Type"].startswith("application/json")
        return json.loads(resp.read())


def test_api_risk_register_shape_and_sorted_by_severity_then_age(
    tmp_path: Path,
) -> None:
    from agenttalk import gates as gmod
    from agenttalk.wrapper import recv_api

    s = _make_store(tmp_path)
    # A high-severity gate-hold risk (blocker gate, red).
    gmod.set_gate(s.root, name="ci", status="red", severity="blocker",
                  scope="release", actor="alpha", evidence_source="automation_ci",
                  reason="pipeline red", required=True)
    # A dead letter -> med severity risk with an owning agent.
    message = s.send(sender="alpha", recipient="beta", body="poison",
                     kind="message", meta={})
    record = recv_api.next_record(s, "beta")
    assert record["id"] == message.id
    s.dead_letter("beta", record, reason="deterministic",
                  failure_class="poison_eligible", at="2026-07-02T00:00:00Z")
    srv, _t, base = _serve(s)
    try:
        payload = _risk_register(base)
        assert set(payload) == {
            "root", "root_path", "root_info", "target_root_project_id",
            "items", "count",
        }
        assert payload["count"] == len(payload["items"])
        for it in payload["items"]:
            assert set(it) == {
                "id", "category", "category_label", "severity", "title",
                "owner", "detail", "age_seconds", "human_can_unblock_now",
            }
            assert it["severity"] in ("high", "med", "low")
        # severity-desc, then age-desc within a severity band
        order = [("high", 0), ("med", 1), ("low", 2)]
        rank = dict(order)
        ranks = [rank[it["severity"]] for it in payload["items"]]
        assert ranks == sorted(ranks)
        gate_items = [it for it in payload["items"] if it["category"] == "gate"]
        assert gate_items and gate_items[0]["category_label"] == "Gate blocker"
        deadletter_items = [it for it in payload["items"] if it["category"] == "deadletter"]
        assert deadletter_items and deadletter_items[0]["category_label"] == "Delivery failure"
        assert deadletter_items[0]["owner"] == "beta"
        _assert_no_body_keys(payload)
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_risk_register_degrades_on_corrupt_root(tmp_path: Path) -> None:
    s = _make_store(tmp_path)
    cfg_path = s.dir / "config.json"
    cfg_path.write_text("{not json", encoding="utf-8")
    srv, _t, base = _serve(s)
    try:
        payload = _risk_register(base)
        assert set(payload) >= {"root", "items", "count"}
        assert payload["count"] == len(payload["items"])
        _assert_no_body_keys(payload)
        assert payload["items"] == [] or all(
            it["category"] in ("escalation", "supervisor", "gate", "deadletter",
                               "coordination_stall", "stuck")
            for it in payload["items"]
        )
    finally:
        srv.shutdown()
        srv.server_close()


# ----------------------------------------------------- /api/ownership (§4d)
#
# Ownership & Accountability Map (docs/PROPOSAL-console-client-sellability.md
# #7).

def _ownership(base: str) -> dict:
    with _get(f"{base}/api/ownership") as resp:
        assert resp.status == 200
        assert resp.headers["Content-Type"].startswith("application/json")
        return json.loads(resp.read())


def test_api_ownership_full_registry_shape(tmp_path: Path) -> None:
    """The full registry - domains with resolved owners/reviewers/curators plus
    shared_paths - not just the thin per-agent slice /api/state carries."""
    s = _make_store(tmp_path)
    (s.dir / "domains.json").write_text(json.dumps({
        "schema_version": 1,
        "domains": {
            "web": {
                "title": "Web layer",
                "owners": {"agents": ["alpha"]},
                "reviewers": {"agents": ["beta"]},
                "owned_globs": ["src/agenttalk/web.py"],
                "description": "the dashboard server",
            },
        },
        "shared_paths": [{
            "glob": "**/pyproject.toml",
            "category": "package-metadata",
            "requires": "lead-approval",
            "default_approvers": {"agents": ["alpha"]},
        }],
    }), encoding="utf-8")
    srv, _t, base = _serve(s)
    try:
        payload = _ownership(base)
        assert set(payload) == {
            "root", "root_path", "root_info", "target_root_project_id",
            "domains", "shared_paths", "count",
        }
        assert payload["count"] == len(payload["domains"])
        (domain,) = payload["domains"]
        assert domain["id"] == "web"
        assert domain["title"] == "Web layer"
        assert domain["owners"] == ["alpha"]
        assert domain["reviewers"] == ["beta"]
        assert domain["curators"] == []
        assert domain["owned_globs"] == ["src/agenttalk/web.py"]
        assert domain["description"] == "the dashboard server"
        (shared,) = payload["shared_paths"]
        assert shared["glob"] == "**/pyproject.toml"
        assert shared["category"] == "package-metadata"
        assert shared["requires"] == "lead-approval"
        assert shared["default_approvers"] == ["alpha"]
        assert shared["default_reviewers"] == []
        _assert_no_body_keys(payload)
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_ownership_missing_registry_is_empty_not_error(tmp_path: Path) -> None:
    s = _make_store(tmp_path)
    srv, _t, base = _serve(s)
    try:
        payload = _ownership(base)
        assert payload["domains"] == []
        assert payload["shared_paths"] == []
        assert payload["count"] == 0
        assert "errors" not in payload
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_ownership_degrades_on_malformed_registry(tmp_path: Path) -> None:
    s = _make_store(tmp_path)
    (s.dir / "domains.json").write_text("{not json", encoding="utf-8")
    srv, _t, base = _serve(s)
    try:
        payload = _ownership(base)
        assert payload["domains"] == []
        assert payload["count"] == 0
        assert payload["errors"]
        _assert_no_body_keys(payload)
    finally:
        srv.shutdown()
        srv.server_close()
