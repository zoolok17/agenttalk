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


# ===================================================== 0.17.0 /api/state
#
# Obligation-dashboard coverage (mission obligation-dashboard-0170).
# Contract under test: kitty-specs/obligation-dashboard-0170-01KTHADQ/
# data-model.md (schema v1) + research.md D5/D6/D9.

import hashlib
import time

# The pre-0.17.0 CSP, byte-for-byte. The split-policy tests pin BOTH
# literals so neither can drift silently (FR-009 / research D1).
_LEGACY_CSP = ("default-src 'none'; style-src 'unsafe-inline'; "
               "img-src 'none'; frame-ancestors 'none'")
_DASH_CSP = ("default-src 'none'; script-src 'self'; "
             "connect-src 'self'; style-src 'unsafe-inline'; "
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
        assert "agents" not in roots[1] and "threads" not in roots[1]
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


def test_dashboard_html_and_root0_only_link_policy(tmp_path: Path) -> None:
    a, b = _make_two_stores(tmp_path)
    b.send(sender="lead", recipient="dev", kind="question",
           subject="b-side", body="y", meta={"request_id": "rid-b"})
    srv, _t, base = _serve_multi(a, b)
    try:
        with _get(f"{base}/dashboard") as resp:
            assert resp.status == 200
            page = resp.read().decode("utf-8")
            assert resp.headers["Content-Security-Policy"] == _DASH_CSP
        assert "/static/dashboard.js" in page
        assert "proj-a" in page and "proj-b" in page
        with _get(f"{base}/static/dashboard.js") as resp:
            js = resp.read().decode("utf-8")
            assert resp.headers["Content-Type"].startswith(
                "application/javascript")
        # the renderer must gate detail hrefs on root index 0 (FR-003):
        assert "rootIndex === 0" in js
        assert "textContent" in js and "innerHTML" not in js
        # and the server gives a cross-root client nothing to link to:
        # root[1]'s message ids are NOT resolvable via root[0]'s routes.
        mid_b = b._scan_messages()[0][0].id
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(  # noqa: S310  # nosemgrep
                f"{base}/api/messages/{mid_b}", timeout=5)
        assert exc.value.code == 404
        # index keeps working and now links to the dashboard (additive)
        with _get(f"{base}/") as resp:
            assert "/dashboard" in resp.read().decode("utf-8")
    finally:
        srv.shutdown()
        srv.server_close()


def test_csp_split_per_route(tmp_path: Path) -> None:
    """The hostile-body routes keep the pre-0.17.0 CSP byte-identical;
    only /dashboard gets the script-capable policy (research D1)."""
    s = _make_store(tmp_path)
    s.send(sender="alpha", recipient="beta", body="<script>x</script>")
    mid = s._scan_messages()[0][0].id
    srv, _t, base = _serve(s)
    try:
        for path in ("/", f"/messages/{mid}", "/api/status", "/api/state"):
            with _get(f"{base}{path}") as resp:
                assert resp.headers["Content-Security-Policy"] == _LEGACY_CSP, path
        with _get(f"{base}/dashboard") as resp:
            assert resp.headers["Content-Security-Policy"] == _DASH_CSP
    finally:
        srv.shutdown()
        srv.server_close()


def test_new_routes_reject_write_methods(tmp_path: Path) -> None:
    s = _make_store(tmp_path)
    srv, _t, base = _serve(s)
    try:
        for path in ("/api/state", "/dashboard", "/static/dashboard.js"):
            req = urllib.request.Request(  # noqa: S310  # nosemgrep
                f"{base}{path}", method="POST", data=b"x")
            with pytest.raises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(req, timeout=5)  # noqa: S310  # nosemgrep
            assert exc.value.code == 405
            assert exc.value.headers.get("Allow") == "GET, HEAD"
    finally:
        srv.shutdown()
        srv.server_close()


def _tree_hashes(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    base = root / ".agenttalk"
    for p in sorted(base.rglob("*")):
        if p.is_file():
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
        for _ in range(3):
            _state(base)
        for path in ("/dashboard", "/static/dashboard.js", "/",
                     f"/messages/{mid}"):
            with _get(f"{base}{path}") as resp:
                resp.read()
        for bad in (f"{base}/messages/zzz-does-not-exist", f"{base}/nope"):
            with pytest.raises(urllib.error.HTTPError):
                urllib.request.urlopen(bad, timeout=5)  # noqa: S310  # nosemgrep
        req = urllib.request.Request(  # noqa: S310  # nosemgrep
            f"{base}/api/state", method="POST", data=b"x")
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(req, timeout=5)  # noqa: S310  # nosemgrep
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
