"""Tests for the read-only web dashboard (`agenttalk serve`).

The dashboard is the v0.7.0 deliverable; it has a security-sensitive
posture (loopback-only, no auth, render-arbitrary-bodies) so the
tests cover both happy-path rendering AND the refusal semantics.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import http.client
import inspect
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from agenttalk import intents, signing, web
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


def _session(base: str) -> dict:
    with _get(f"{base}/api/session") as resp:
        assert resp.status == 200
        return json.loads(resp.read())


def _post_intent(base: str, token: str, payload: dict,
                 *, origin: str | None = None, method: str = "POST"):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(  # noqa: S310  # nosemgrep
        f"{base}/api/intent", method=method, data=data,
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
             "img-src 'none'; frame-ancestors 'none'")


def _make_two_stores(tmp_path: Path) -> tuple[Store, Store]:
    a = Store(tmp_path / "proj-a")
    (tmp_path / "proj-a").mkdir()
    a.init(["alpha", "beta"])
    b = Store(tmp_path / "proj-b")
    (tmp_path / "proj-b").mkdir()
    b.init(["lead", "dev"])
    return a, b


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


def test_dedup_labels_case_insensitive() -> None:
    labels = web._dedup_labels([
        Path("C:/x/proj"), Path("C:/y/PROJ"), Path("C:/z/other"),
    ])
    assert labels == ["proj", "PROJ~2", "other"]


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
        for bad in ("nope.js", "..%2F..%2Fweb.py", "console.css.bak", ""):
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
                     "/api/attention", "/api/thread/rid-c"):
            with _get(f"{base}{path}") as resp:
                assert resp.headers["Content-Security-Policy"] == _LEGACY_CSP, path
        for path in ("/", "/dashboard"):
            with _get(f"{base}{path}") as resp:
                assert resp.headers["Content-Security-Policy"] == _DASH_CSP
        # the served assets are not documents; they carry the default policy too
        for path in ("/static/console.js", "/static/console.css"):
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
        for path in ("/api/state", "/dashboard", "/api/attention",
                     "/api/thread/rid-w", "/static/console.js",
                     "/static/console.css"):
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
                     "/", f"/messages/{mid}", "/api/attention",
                     "/api/thread/r1"):
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
    assert "innerHTML" not in js
    assert "location.reload" not in js
    assert "eval(" not in js
    assert "csrf_token" not in js.split("localStorage", 1)[0]
    assert "sessionStorage" not in js


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
                  cli: str | None = None, mode: str | None = None) -> None:
    ts = _now_iso()
    store.write_health(agent, _hm.build_snapshot(
        agent=agent, cli=cli, mode=mode, state=state,
        updated_at=ts, since=ts, last_progress_at=ts, reason_code=state))


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


# ---------------------------------------------------------- /api/attention

def _attention(base: str) -> dict:
    with _get(f"{base}/api/attention") as resp:
        assert resp.status == 200
        assert resp.headers["Content-Type"].startswith("application/json")
        return json.loads(resp.read())


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
        assert set(payload) == {"root", "items", "count"}
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
    s.send(sender="alpha", recipient="beta", kind="question",
           subject="operator input needed", body="body",
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
    finally:
        srv.shutdown()
        srv.server_close()

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
    proposal-response verdict is emitted only for an allowlisted status."""
    s = _make_store(tmp_path)
    # review-results: latest-wins (approved then changes_requested).
    s.send(sender="alpha", recipient="beta", kind="review-request",
           subject="r", body="please review", meta={"request_id": "rid-v"})
    s.send(sender="beta", recipient="alpha", kind="review-result",
           subject="v1", body="ok", meta={"request_id": "rid-v", "status": "approved"})
    s.send(sender="beta", recipient="alpha", kind="review-result",
           subject="v2", body="wait", meta={"request_id": "rid-v",
                                            "status": "changes_requested"})
    # proposal-response: allowlisted status accepted -> verdict; unknown -> omit.
    s.send(sender="alpha", recipient="beta", kind="proposal",
           subject="p", body="do X", meta={"request_id": "rid-p"})
    s.send(sender="beta", recipient="alpha", kind="proposal-response",
           subject="pr", body="sure", meta={"request_id": "rid-p", "status": "accepted"})
    s.send(sender="alpha", recipient="beta", kind="proposal",
           subject="p2", body="do Y", meta={"request_id": "rid-q"})
    s.send(sender="beta", recipient="alpha", kind="proposal-response",
           subject="pr2", body="hmm", meta={"request_id": "rid-q", "status": "maybe"})
    srv, _t, base = _serve(s)
    try:
        (root,) = _state(base)["roots"]
        by_rid = {t["request_id"]: t for t in root["threads"]}
        assert by_rid["rid-v"]["verdict"] == "changes_requested"
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
