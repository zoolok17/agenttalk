"""Tests for the read-only web dashboard (`agenttalk serve`).

The dashboard is the v0.7.0 deliverable; it has a security-sensitive
posture (loopback-only, no auth, render-arbitrary-bodies) so the
tests cover both happy-path rendering AND the refusal semantics.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from agenttalk import signing, web
from agenttalk.store import Store


# --------------------------------------------------------------- helpers

def _make_store(tmp_path: Path) -> Store:
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    return s


def _serve(store: Store, *, host: str = "127.0.0.1"):
    """Start the server on an ephemeral port in a daemon thread.

    Returns (server, thread, base_url). Caller is responsible for
    ``server.shutdown(); server.server_close()`` in a finally block.
    """
    return web.serve_in_thread(store, host=host, port=0)


def _get(url: str, *, method: str = "GET", timeout: float = 5.0):
    req = urllib.request.Request(url, method=method)  # noqa: S310  # nosemgrep
    return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310  # nosemgrep


# --------------------------------------------------------------- routing


def test_index_renders_status_and_messages(tmp_path: Path) -> None:
    s = _make_store(tmp_path)
    s.send(sender="alpha", recipient="beta", body="hello world")
    srv, _t, base = _serve(s)
    try:
        with _get(f"{base}/") as resp:
            assert resp.status == 200
            assert resp.headers["Content-Type"].startswith("text/html")
            body = resp.read().decode("utf-8")
        assert "<!doctype html>" in body
        assert "Project root" in body
        assert "alpha" in body and "beta" in body
        assert "Signing" in body
        # The message row should be linkable
        assert "/messages/" in body
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
            urllib.request.urlopen(req, timeout=5)  # noqa: S310  # nosemgrep
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
            urllib.request.urlopen(f"{base}/nope", timeout=5)  # noqa: S310  # nosemgrep
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
            urllib.request.urlopen(  # noqa: S310  # nosemgrep
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
            urllib.request.urlopen(  # noqa: S310  # nosemgrep
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
            urllib.request.urlopen(  # noqa: S310  # nosemgrep
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
                urllib.request.urlopen(req, timeout=5)  # noqa: S310  # nosemgrep
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
