"""Read-only local web dashboard for an agenttalk project.

Threat model
============
This server is designed for a *single human at their workstation*
browsing the bus's message log in a real browser. It is not a
multi-tenant API and not safe to expose beyond loopback. By design,
there is **no opt-in for remote binding** — the only accepted hosts
are loopback addresses (``127.0.0.1``, ``::1``, ``localhost``). If
you need to view the dashboard from another machine, SSH-tunnel
``localhost:8765`` from that machine; do not punch a hole in the
loopback wall.

Defenses in this module:

1. **Loopback-only bind.** ``make_server`` refuses any ``host`` not
   in :data:`LOOPBACK_HOSTS`. There is no override flag.
2. **Defense in depth on the handler.** Every request — including
   POST/PUT/DELETE/PATCH that go straight to 405 — first rechecks
   ``self.client_address[0]`` against a loopback prefix allowlist.
   A non-loopback peer is rejected with 403 *before* any other
   policy runs, so probes can't measure us via methods that skip
   later checks.
3. **No write methods.** Only ``GET``/``HEAD`` are dispatched;
   everything else returns 405 without touching disk.
4. **Strict path allowlist.** Only the routes documented below are
   served; anything else 404s before path interpolation.
5. **Roster/kind validation parity with ``recv``/``tail``.** The
   dashboard reuses the same validation surface (``Message.validate``
   + HMAC verify when ``signing_enforced()``) so messages with an
   unknown ``kind`` or out-of-roster sender/recipient are NOT
   rendered — they surface in ``/api/status.invalid_messages``
   instead, matching the existing CLI invariant.
6. **HTML escaping everywhere.** Message bodies are user-supplied
   and may contain ``<script>``-like text. Everything that lands
   in HTML goes through ``html.escape``. ``Content-Security-Policy``
   blocks inline JS as a second layer.
7. **Conservative response headers.** ``X-Content-Type-Options:
   nosniff``, ``X-Frame-Options: DENY``,
   ``Referrer-Policy: no-referrer``, ``Cache-Control: no-store``.

Routes
======
- ``GET  /``                    — dashboard HTML (status + recent messages)
- ``GET  /messages/<id>``       — message detail HTML
- ``GET  /api/status``          — JSON status (mirrors ``agenttalk status --json``-ish)
- ``GET  /api/messages``        — JSON list of all validated messages
- ``GET  /api/messages/<id>``   — single message JSON
- ``GET  /favicon.ico``         — 204 (no icon shipped; suppress browser noise)
"""

from __future__ import annotations

import html
import json
import re
import socket
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from agenttalk import __version__
from agenttalk import signing as _signing
from agenttalk.store import Message, Store


# The only host strings accepted by ``make_server``. No opt-in to
# extend this list — if you need remote access, SSH-tunnel.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
# Per-request peer-IP allowlist. The dual-stack mapped form
# ``::ffff:127.0.0.1`` is what an IPv6 socket reports when an IPv4
# loopback client connects; treat it as loopback.
LOOPBACK_PEER_PREFIXES = ("127.", "::1", "::ffff:127.")

_MESSAGE_ID_RE = re.compile(r"\A[A-Za-z0-9_.\-]{1,128}\Z")


def _format_url(host: str, port: int) -> str:
    """Return an RFC 3986 URL with IPv6 hosts bracketed.

    ``http://::1:8765`` is invalid (ambiguous port boundary); the
    bracketed form ``http://[::1]:8765/`` is correct. IPv4 literals
    and hostnames don't get brackets.
    """
    if ":" in host and not host.startswith("["):
        return f"http://[{host}]:{port}/"
    return f"http://{host}:{port}/"


# ----------------------------------------------------------- HTML rendering

_PAGE_CSS = """
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI",
       Roboto, sans-serif; max-width: 960px; margin: 2em auto;
       padding: 0 1em; color: #222; }
h1, h2, h3 { color: #111; }
table { border-collapse: collapse; width: 100%; margin: 1em 0; }
th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid #ddd;
         vertical-align: top; }
th { background: #f4f4f4; }
code, pre { font-family: ui-monospace, Menlo, Consolas, monospace; }
pre { background: #f7f7f7; padding: 1em; border: 1px solid #e0e0e0;
      border-radius: 4px; white-space: pre-wrap; word-break: break-word; }
.tag { display: inline-block; padding: 1px 8px; border-radius: 3px;
       background: #eee; font-size: 0.85em; margin-right: 4px; }
.tag-end { background: #fde2e2; }
.tag-review-request, .tag-review-result { background: #e2ecfd; }
.tag-proposal, .tag-proposal-response { background: #e6f7ea; }
.tag-question, .tag-wake { background: #fdf3e2; }
.tag-rescind { background: #f7d6d6; font-weight: 600; }
.invalid { color: #b00; }
.muted { color: #666; font-size: 0.9em; }
.footer { margin-top: 3em; color: #888; font-size: 0.85em; }
""".strip()


def _html_page(title: str, body: str) -> bytes:
    return (
        "<!doctype html>\n"
        "<html lang=\"en\"><head>"
        "<meta charset=\"utf-8\">"
        f"<title>{html.escape(title)}</title>"
        f"<style>{_PAGE_CSS}</style>"
        "</head><body>"
        f"{body}"
        f"<div class=\"footer\">agenttalk {html.escape(__version__)} "
        "&middot; read-only local dashboard</div>"
        "</body></html>\n"
    ).encode("utf-8")


def _kind_tag(kind: str) -> str:
    safe = html.escape(kind or "message")
    cls = f"tag tag-{html.escape(kind or 'message')}"
    return f"<span class=\"{cls}\">{safe}</span>"


def _fmt_message_row(m: Message, *, base: str = "") -> str:
    href = f"{base}/messages/{html.escape(m.id, quote=True)}"
    subj = html.escape(m.subject or "")
    return (
        "<tr>"
        f"<td><code><a href=\"{href}\">{html.escape(m.id)}</a></code></td>"
        f"<td>{html.escape(m.ts)}</td>"
        f"<td>{html.escape(m.sender)} &rarr; {html.escape(m.recipient)}</td>"
        f"<td>{_kind_tag(m.kind)}</td>"
        f"<td>{subj}</td>"
        "</tr>"
    )


def render_index(store: Store, messages: list[Message],
                 invalid: list[tuple[str, str]]) -> bytes:
    cfg = _safe_load_config(store)
    agents = cfg.get("agents", []) or []
    title = f"agenttalk :: {store.root.name}"
    sig_enforced = store.signing_enforced()
    sig_label = "enforced (HMAC key present)" if sig_enforced else "disabled (no HMAC key)"
    sig_class = "" if sig_enforced else "muted"

    rows = "".join(_fmt_message_row(m) for m in messages[:200])
    body_rows = rows or (
        "<tr><td colspan=\"5\" class=\"muted\">No messages yet.</td></tr>"
    )

    invalid_html = ""
    if invalid:
        items = "".join(
            f"<li><code>{html.escape(mid)}</code>: "
            f"<span class=\"invalid\">{html.escape(reason)}</span></li>"
            for mid, reason in invalid
        )
        invalid_html = (
            "<h2 class=\"invalid\">Invalid messages "
            f"({len(invalid)})</h2><ul>{items}</ul>"
        )

    body = (
        f"<h1>{html.escape(title)}</h1>"
        "<h2>Status</h2>"
        "<table>"
        f"<tr><th>Project root</th><td><code>{html.escape(str(store.root))}</code></td></tr>"
        f"<tr><th>Project ID</th><td><code>{html.escape(store.project_id())}</code></td></tr>"
        f"<tr><th>Agents</th><td>{html.escape(', '.join(agents)) or '<em>none</em>'}</td></tr>"
        f"<tr><th>Signing</th><td class=\"{sig_class}\">{html.escape(sig_label)}</td></tr>"
        f"<tr><th>Messages</th><td>{len(messages)}</td></tr>"
        "</table>"
        f"{invalid_html}"
        f"<h2>Messages (most recent first, up to 200)</h2>"
        "<table>"
        "<tr><th>ID</th><th>Timestamp</th><th>Route</th><th>Kind</th><th>Subject</th></tr>"
        f"{body_rows}"
        "</table>"
    )
    return _html_page(title, body)


def render_message(store: Store, m: Message) -> bytes:
    title = f"agenttalk :: {m.id}"
    meta_rows = "".join(
        f"<tr><th>{html.escape(str(k))}</th>"
        f"<td><code>{html.escape(str(v))}</code></td></tr>"
        for k, v in (m.meta or {}).items()
    )
    meta_html = f"<table>{meta_rows}</table>" if meta_rows else ""
    body = (
        f"<p><a href=\"/\">&larr; back to dashboard</a></p>"
        f"<h1>{_kind_tag(m.kind)} {html.escape(m.subject or m.id)}</h1>"
        "<table>"
        f"<tr><th>ID</th><td><code>{html.escape(m.id)}</code></td></tr>"
        f"<tr><th>Timestamp</th><td>{html.escape(m.ts)}</td></tr>"
        f"<tr><th>From</th><td>{html.escape(m.sender)}</td></tr>"
        f"<tr><th>To</th><td>{html.escape(m.recipient)}</td></tr>"
        f"<tr><th>Kind</th><td>{html.escape(m.kind)}</td></tr>"
        f"<tr><th>Subject</th><td>{html.escape(m.subject or '')}</td></tr>"
        "</table>"
        f"{('<h3>Meta</h3>' + meta_html) if meta_html else ''}"
        "<h3>Body</h3>"
        f"<pre>{html.escape(m.body or '')}</pre>"
    )
    return _html_page(title, body)


# ------------------------------------------------------------ data shaping

def _safe_load_config(store: Store) -> dict:
    try:
        return store.load_config()
    except (OSError, ValueError, FileNotFoundError):
        return {}


def _all_messages(store: Store) -> list[Message]:
    """Return every renderable message in the store, most recent first.

    Mirrors the validation surface ``Store.messages_for()`` uses so
    the dashboard's render set matches what ``recv``/``tail`` would
    deliver: schema-passing, roster-valid, known-kind, and (when
    ``signing_enforced()``) HMAC-valid. Anything that fails goes
    through ``store.list_invalid_messages()`` and is surfaced in
    ``/api/status.invalid_messages`` instead of being rendered.
    """
    cfg = _safe_load_config(store)
    roster = cfg.get("agents", []) or []
    valid, _ = store._scan_messages()  # noqa: SLF001 — same call doctor uses
    require_sig = store.signing_enforced()
    key: bytes | None = None
    project_id: str | None = None
    if require_sig:
        project_id = store.project_id()
        try:
            key = _signing.load_key(project_id)
        except (FileNotFoundError, OSError, ValueError):
            # Enforcement is on but the key file is unreadable. The
            # CLI's recv/tail path skips all messages in this state;
            # do the same here.
            return []
    out: list[Message] = []
    for m in valid:
        try:
            m.validate(roster)
        except ValueError:
            continue  # unknown kind / out-of-roster — surface in invalid_messages
        if require_sig:
            try:
                _signing.verify_message(
                    m.to_dict(), key, expected_key_id=project_id,
                )
            except ValueError:
                continue
        out.append(m)
    return sorted(out, key=lambda x: x.id, reverse=True)


def status_payload(store: Store) -> dict:
    cfg = _safe_load_config(store)
    invalid = store.list_invalid_messages()
    health = _signing.inspect_key(store.project_id(), store.root)
    return {
        "agenttalk_version": __version__,
        "project_root": str(store.root),
        "project_id": store.project_id(),
        "agents": cfg.get("agents", []) or [],
        "signing_enforced": store.signing_enforced(),
        "hmac_key": health.to_dict(),
        "invalid_messages": [
            {"id": mid, "reason": reason} for mid, reason in invalid
        ],
    }


# ------------------------------------------------------------ HTTP handler

def _make_handler(store: Store) -> type[BaseHTTPRequestHandler]:
    """Build a request handler class closed over ``store``.

    Returns a class (not an instance) because ``ThreadingHTTPServer``
    instantiates it per connection.
    """

    class Handler(BaseHTTPRequestHandler):
        server_version = f"agenttalk/{__version__}"
        # Silence default stderr access log — we route everything
        # through ``log_message`` so the caller can opt in / out.
        _quiet = True

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            if not self._quiet:
                super().log_message(format, *args)

        # ---- defense-in-depth: per-request loopback check
        def _is_loopback_peer(self) -> bool:
            peer = (self.client_address[0] or "")
            return any(peer == p.rstrip(".") or peer.startswith(p)
                       for p in LOOPBACK_PEER_PREFIXES)

        # ---- response helpers
        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; style-src 'unsafe-inline'; "
                "img-src 'none'; frame-ancestors 'none'",
            )
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _send_html(self, status: int, body: bytes) -> None:
            self._send(status, body, "text/html; charset=utf-8")

        def _send_json(self, status: int, payload: Any) -> None:
            data = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
            self._send(status, data, "application/json; charset=utf-8")

        def _error_html(self, status: int, message: str) -> None:
            body = _html_page(
                f"agenttalk :: {status}",
                f"<h1>{status}</h1><p>{html.escape(message)}</p>"
                "<p><a href=\"/\">&larr; back to dashboard</a></p>",
            )
            self._send_html(status, body)

        # ---- method dispatch
        # Every do_* method MUST first call ``_check_peer_or_403``.
        # The earlier design routed POST/PUT/DELETE/PATCH straight
        # to 405 without the peer check, which let a non-loopback
        # client distinguish "the server is here" (405) from "you
        # are blocked" (403). The unified gate below closes that.
        def _check_peer_or_403(self) -> bool:
            if self._is_loopback_peer():
                return True
            self._send(HTTPStatus.FORBIDDEN,
                       b"forbidden: dashboard is loopback-only\n",
                       "text/plain; charset=utf-8")
            return False

        def do_HEAD(self) -> None:  # noqa: N802 — stdlib API
            self.do_GET()

        def do_GET(self) -> None:  # noqa: N802 — stdlib API
            if not self._check_peer_or_403():
                return
            try:
                self._route()
            except Exception:  # noqa: BLE001 — never leak a traceback to the browser
                self._error_html(HTTPStatus.INTERNAL_SERVER_ERROR,
                                 "internal server error")
                raise  # surface in stderr for the operator

        def do_POST(self) -> None:  # noqa: N802
            if not self._check_peer_or_403():
                return
            self._method_not_allowed()

        def do_PUT(self) -> None:  # noqa: N802
            if not self._check_peer_or_403():
                return
            self._method_not_allowed()

        def do_DELETE(self) -> None:  # noqa: N802
            if not self._check_peer_or_403():
                return
            self._method_not_allowed()

        def do_PATCH(self) -> None:  # noqa: N802
            if not self._check_peer_or_403():
                return
            self._method_not_allowed()

        def _method_not_allowed(self) -> None:
            self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
            self.send_header("Allow", "GET, HEAD")
            self.send_header("Content-Length", "0")
            self.end_headers()

        # ---- routing
        def _route(self) -> None:
            path = self.path.split("?", 1)[0].split("#", 1)[0]
            if path == "/":
                self._serve_index()
                return
            if path == "/favicon.ico":
                self._send(HTTPStatus.NO_CONTENT, b"", "image/x-icon")
                return
            if path == "/api/status":
                self._send_json(HTTPStatus.OK, status_payload(store))
                return
            if path == "/api/messages":
                msgs = _all_messages(store)
                self._send_json(HTTPStatus.OK,
                                {"messages": [m.to_dict() for m in msgs]})
                return
            if path.startswith("/messages/"):
                mid = path[len("/messages/"):]
                if not _MESSAGE_ID_RE.match(mid):
                    self._error_html(HTTPStatus.NOT_FOUND, "message not found")
                    return
                m = _find_message(store, mid)
                if m is None:
                    self._error_html(HTTPStatus.NOT_FOUND, "message not found")
                    return
                self._send_html(HTTPStatus.OK, render_message(store, m))
                return
            if path.startswith("/api/messages/"):
                mid = path[len("/api/messages/"):]
                if not _MESSAGE_ID_RE.match(mid):
                    self._send_json(HTTPStatus.NOT_FOUND,
                                    {"error": "message not found"})
                    return
                m = _find_message(store, mid)
                if m is None:
                    self._send_json(HTTPStatus.NOT_FOUND,
                                    {"error": "message not found"})
                    return
                self._send_json(HTTPStatus.OK, m.to_dict())
                return
            self._error_html(HTTPStatus.NOT_FOUND, f"no route for {path}")

        def _serve_index(self) -> None:
            msgs = _all_messages(store)
            invalid = store.list_invalid_messages()
            self._send_html(HTTPStatus.OK, render_index(store, msgs, invalid))

    return Handler


def _find_message(store: Store, mid: str) -> Message | None:
    for m in _all_messages(store):
        if m.id == mid:
            return m
    return None


# ----------------------------------------------------- server entry points

def make_server(store: Store, host: str, port: int,
                *, quiet: bool = True) -> ThreadingHTTPServer:
    """Build (but do not start) a dashboard HTTP server.

    Refuses to bind to anything but a loopback host
    (:data:`LOOPBACK_HOSTS`). There is no opt-in to bind elsewhere:
    the dashboard has no auth, renders un-sandboxed message bodies,
    and is only safe for the local user. Use an SSH tunnel if you
    need to view it from another machine.

    The caller is expected to ``serve_forever`` (in this thread or
    another).
    """
    if host not in LOOPBACK_HOSTS:
        raise ValueError(
            f"refusing to bind to non-loopback host {host!r}. The "
            f"dashboard is loopback-only by design (no auth, renders "
            f"un-sandboxed message bodies). Use one of: "
            f"{', '.join(sorted(LOOPBACK_HOSTS))}. If you need remote "
            f"access, SSH-tunnel localhost:<port> from the remote box."
        )
    handler_cls = _make_handler(store)
    if not quiet:
        handler_cls._quiet = False  # noqa: SLF001 — class attr by design
    # ``::1`` is IPv6 loopback; everything else in the allowlist is
    # IPv4. ``localhost`` is left to the OS resolver (defaults to v4
    # on virtually every system; users that need v6 ask for ``::1``).
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    ThreadingHTTPServer.address_family = family
    try:
        return ThreadingHTTPServer((host, port), handler_cls)
    finally:
        # Restore default for any other use of the class in-process.
        ThreadingHTTPServer.address_family = socket.AF_INET


def serve(store: Store, *, host: str = "127.0.0.1", port: int = 8765,
          quiet: bool = True,
          on_ready: Callable[[str], None] | None = None) -> None:
    """Start the dashboard and block until interrupted.

    ``port=0`` asks the OS for an ephemeral port. The actual bound
    port is announced via ``on_ready(url)`` and is also available as
    ``server.server_address[1]`` if the caller wraps this manually.
    """
    srv = make_server(store, host, port, quiet=quiet)
    actual_port = srv.server_address[1]
    url = _format_url(host, actual_port)
    if on_ready is not None:
        on_ready(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


def serve_in_thread(store: Store, *, host: str = "127.0.0.1", port: int = 0
                    ) -> tuple[ThreadingHTTPServer, threading.Thread, str]:
    """Start the dashboard on a daemon thread (used by tests).

    Returns ``(server, thread, base_url)``. Caller is responsible for
    ``server.shutdown()`` then ``server.server_close()``. ``base_url``
    has no trailing slash so callers can append ``/messages/<id>``
    etc. directly.
    """
    srv = make_server(store, host, port)
    t = threading.Thread(target=srv.serve_forever, daemon=True,
                         name="agenttalk-web")
    t.start()
    base = _format_url(host, srv.server_address[1]).rstrip("/")
    return srv, t, base


__all__ = [
    "LOOPBACK_HOSTS",
    "make_server",
    "render_index",
    "render_message",
    "serve",
    "serve_in_thread",
    "status_payload",
]
