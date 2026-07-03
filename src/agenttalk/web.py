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
- ``GET  /``                    — message-log HTML (status + recent messages)
- ``GET  /messages/<id>``       — message detail HTML
- ``GET  /api/status``          — JSON status (mirrors ``agenttalk status --json``-ish)
- ``GET  /api/messages``        — JSON list of all validated messages
- ``GET  /api/messages/<id>``   — single message JSON
- ``GET  /favicon.ico``         — 204 (no icon shipped; suppress browser noise)
- ``GET  /dashboard``           — the Team Console HTML shell (all roots; 0.58.0)
- ``GET  /static/<name>``       — allowlisted console assets (console.css/js; 0.58.0)
- ``GET  /api/state``           — multi-root obligation aggregate, schema v1 (0.17.0)
- ``GET  /api/attention``       — ranked "needs a human" queue for root[0] (0.58.0)
- ``GET  /api/thread/<rid>``    — one thread's full transcript, CARRIES bodies (0.58.0)

Multi-root (0.17.0)
===================
One server can watch several stores (``agenttalk dashboard --store A
--store B``). The first root is **root[0]**: every pre-0.17.0 route keeps
binding to it unchanged, so single-root ``serve`` behavior is preserved
byte-for-byte. ``/api/state`` and ``/dashboard`` render all roots, each
namespaced under its own entry — never merged. A corrupt or uninitialized
root degrades to an ``errors`` entry in the payload; it cannot 5xx the
aggregate or affect sibling roots.

CSP note: ``/dashboard`` is the ONLY route whose Content-Security-Policy
allows (self-hosted) script + stylesheet + fetch — it renders no
message-derived HTML server-side and its client builds DOM via
``textContent`` only. The console CSS/JS ship as served files, so the console
CSP drops ``'unsafe-inline'`` entirely (``script-src 'self'; style-src
'self'``). Routes that render hostile message bodies (``/messages/<id>``) and
every JSON feed (``/api/state``, ``/api/attention``, ``/api/thread/<rid>``)
keep the stricter no-script policy byte-identical.
"""

from __future__ import annotations

import html
import ipaddress
import json
import re
import socket
import threading
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from agenttalk import __version__
from agenttalk import attention as _attention
from agenttalk import capacity as _capacity
from agenttalk import domains as _domains
from agenttalk import signing as _signing
from agenttalk.store import COMPOSING_INTENT_STALE_SECONDS, Message, Store
from agenttalk.threads import Thread, derive_threads


# The only host strings accepted by ``make_server``. No opt-in to
# extend this list — if you need remote access, SSH-tunnel.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _is_loopback_addr(peer: str) -> bool:
    """True iff ``peer`` is a loopback address (defense-in-depth peer check).

    Address-aware, not string-prefix: parse with ``ipaddress`` and accept
    only genuinely-loopback addresses. This rejects non-loopback peers whose
    text merely *starts with* ``::1`` (e.g. ``::1a2b:...``) — the trap in the
    old ``startswith('::1')`` matcher — while still accepting the IPv4-mapped
    form ``::ffff:127.0.0.1`` (what a dual-stack socket reports for an IPv4
    loopback client) via ``ipv4_mapped``. A non-IP literal is loopback only
    when it is exactly ``localhost``.
    """
    if not peer:
        return False
    try:
        addr = ipaddress.ip_address(peer)
    except ValueError:
        return peer == "localhost"
    if addr.is_loopback:
        return True
    mapped = getattr(addr, "ipv4_mapped", None)  # ::ffff:127.0.0.1 -> 127.0.0.1
    return mapped is not None and mapped.is_loopback

_MESSAGE_ID_RE = re.compile(r"\A[A-Za-z0-9_.\-]{1,128}\Z")

# /api/state contract version. Bumped ONLY on a breaking change to the
# aggregate's shape; additive keys do not bump it.
STATE_SCHEMA_VERSION = 1

# The pre-0.17.0 policy, byte-identical. Every route uses it unless it
# explicitly opts into a different one (only /dashboard does — see the
# module docstring's CSP note).
_DEFAULT_CSP = ("default-src 'none'; style-src 'unsafe-inline'; "
                "img-src 'none'; frame-ancestors 'none'")
# The console document routes (/dashboard) only: allow the self-hosted
# script + stylesheet and same-origin fetch. Still no inline script, no inline
# style, no eval, no remote anything. CHANGED (0.58.0): the CSS moved to a
# served file (/static/console.css), so 'unsafe-inline' for style is dropped in
# favor of 'self' — the console carries zero inline <style> and zero style= attrs.
_DASHBOARD_CSP = ("default-src 'none'; script-src 'self'; "
                  "connect-src 'self'; style-src 'self'; "
                  "img-src 'none'; frame-ancestors 'none'")

# Thread states that owe nothing (mirror threads._derive_next's gate).
_TERMINAL_STATES = ("closed", "closed-superseded")

# /api/state per-root conversation-edge cap (0.19.0, FR-002/003). When more
# than this many distinct (from,to) pairs exist, the list is capped and the
# root carries an additive truncation signal.
_EDGE_LIMIT = 50

# /api/state per-root recent-activity feed cap (0.58.0). Envelope-only rows
# (never body) for the live "what's happening" feed on the dashboard.
_RECENT_LIMIT = 25

# Health-timeline ring (0.58.0, §5): a per-(root,agent) window of recent
# health-state samples, kept IN-MEMORY on the server instance only (never a
# file — the read-only invariant). Samples older than this window are pruned;
# contiguous same-state samples collapse into {state, seconds} segments.
_HEALTH_TIMELINE_WINDOW_SECONDS = 30 * 60  # ~last 30 minutes

# Meta keys safe to surface in a thread transcript's `meta_line` (§4b). A
# strict whitelist — arbitrary meta may carry body-ish sender text, which must
# never leak; only these envelope-level decision markers are shown.
_META_LINE_WHITELIST = ("status", "head", "base")


@dataclass(frozen=True)
class RootDescriptor:
    """One store the server watches, plus its display label."""
    store: Store
    label: str


def _dedup_labels(paths: list[Path]) -> list[str]:
    """Directory basenames, deduplicated case-insensitively with ~2/~3…

    Windows-first: two paths differing only in case still collide, so
    the comparison key is casefolded while the emitted label keeps the
    original spelling.
    """
    labels: list[str] = []
    seen: dict[str, int] = {}
    for p in paths:
        base = p.name or str(p)
        key = base.casefold()
        n = seen.get(key, 0) + 1
        seen[key] = n
        labels.append(base if n == 1 else f"{base}~{n}")
    return labels


def make_descriptors(paths: list[Path]) -> list[RootDescriptor]:
    """Build descriptors for explicit store paths (the --store flag).

    Each path IS the project root — no upward walk. A path without a
    live store still gets a descriptor: ``build_state`` reports it as a
    degraded root (errors-as-data, never a startup refusal).
    """
    labels = _dedup_labels(paths)
    return [RootDescriptor(store=Store(p), label=lab)
            for p, lab in zip(paths, labels, strict=True)]


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
/* 0.19.0 dashboard polish: controls + hierarchical roster + cards */
#controls { margin: 0.5em 0 1em; }
#controls button { padding: 4px 12px; }
.roster-top { display: flex; flex-wrap: wrap; gap: 10px; justify-content: center;
              margin: 0.5em 0; }
.roster-cols { display: flex; flex-wrap: wrap; gap: 14px; align-items: flex-start; }
.col { flex: 1 1 30%; min-width: 220px; }
.col-head { font-weight: 600; color: #555; text-transform: uppercase;
            font-size: 0.75em; margin-bottom: 4px; }
.agent-card { border: 1px solid #ddd; border-radius: 6px; padding: 8px 10px;
              margin-bottom: 8px; background: #fafafa; }
.col-left .agent-card { border-left: 3px solid #2d6cdf; }
.col-right .agent-card { border-left: 3px solid #cf5b2d; }
.roster-top .agent-card { border-left: 3px solid #2da44e; background: #f2fbf4;
                          min-width: 240px; }
.agent-name { font-weight: 600; }
.agent-card .stats { font-size: 0.85em; color: #444; margin-top: 2px; }
.agent-card .badge { display: inline-block; margin-top: 4px; padding: 1px 7px;
                     border-radius: 3px; background: #fdf3e2; font-size: 0.8em; }
ul.edges { columns: 2; font-size: 0.9em; }
ul.edges li { break-inside: avoid; }
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
        "<p><a href=\"/dashboard\">obligation dashboard</a></p>"
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


def render_dashboard(roots: list[RootDescriptor]) -> bytes:
    """The Team Console shell (0.58.0) — a fixed 3-region skeleton.

    Deliberately near-empty: ALL dynamic content is rendered client-side from
    ``/api/state`` / ``/api/attention`` / ``/api/thread/<rid>`` by the served
    ``/static/console.js`` — one renderer. The document carries **zero** inline
    ``<style>`` and **zero** inline event handlers (``onclick=`` / ``style=``);
    styling is a linked ``/static/console.css`` and behavior a linked
    ``/static/console.js``, so the console CSP can drop ``'unsafe-inline'``
    entirely (``script-src 'self'; style-src 'self'``).

    Only operator-supplied labels (root labels) land here, escaped anyway; the
    client picks the active root from ``/api/state.roots`` (a multi-root
    switcher is JS-hydrated). Everything else is client-rendered.
    """
    body = (
        "<div id=\"app\">"
        "<header id=\"topbar\"></header>"
        "<div id=\"body\">"
        "<nav id=\"sidebar\"></nav>"
        "<main id=\"main\"></main>"
        "</div>"
        "</div>"
        "<noscript><p>This console needs JavaScript (it polls "
        "<code>/api/state</code> every 2 seconds). Poll "
        "<code>GET /api/state</code> directly instead.</p></noscript>"
        "<link rel=\"stylesheet\" href=\"/static/console.css\">"
        "<script src=\"/static/console.js\"></script>"
    )
    return _console_page("agenttalk :: team console", body)


def _console_page(title: str, body: str) -> bytes:
    """A minimal HTML document for the console shell.

    Unlike :func:`_html_page`, this carries NO inline ``<style>`` block — the
    console's CSS is served from ``/static/console.css`` so the document can run
    under a ``style-src 'self'`` policy with no ``'unsafe-inline'``. All markup
    here is server-authored (only the escaped ``<title>``), so nothing bus-
    derived reaches the document.
    """
    return (
        "<!doctype html>\n"
        "<html lang=\"en\"><head>"
        "<meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{html.escape(title)}</title>"
        "</head><body>"
        f"{body}"
        "</body></html>\n"
    ).encode("utf-8")


# ---------------------------------------------------------- static assets
#
# The console's CSS and JS ship as real, editable, lintable files under the
# package's ``web_static/`` dir (NOT a giant inline string constant). They are
# served via an ALLOWLISTED-FILENAME route — an exact dict lookup, never a
# ``pathlib`` join on request input — so the traversal guarantee is preserved
# byte-for-byte (a request path can only ever match a literal key here).
#
# Bytes are loaded once at import from the package dir. Loading is TOLERANT: a
# missing asset (e.g. during a partial build, or while the CSS/JS are still
# being authored) simply omits that key — import never errors and the route
# 404s the missing name instead of crashing the server.
_WEB_STATIC_DIR = Path(__file__).parent / "web_static"

# Allowlisted static assets: filename -> (content-type, bytes). Exact-match
# lookup only. A name not in this dict 404s (see the /static/<name> route).
_STATIC_ASSETS: dict[str, tuple[str, bytes]] = {}


def _load_static_assets() -> dict[str, tuple[str, bytes]]:
    """Load the allowlisted console assets from the package ``web_static`` dir.

    Tolerant by design: a missing/unreadable file is skipped (its route then
    404s) so importing this module never fails on a partial checkout or a
    mid-authoring asset. The filename allowlist is fixed here — request input
    is never joined onto a path.
    """
    assets: dict[str, tuple[str, bytes]] = {}
    for name, ctype in (
        ("console.css", "text/css; charset=utf-8"),
        ("console.js", "application/javascript; charset=utf-8"),
    ):
        try:
            assets[name] = (ctype, (_WEB_STATIC_DIR / name).read_bytes())
        except OSError:
            continue  # not present yet — route 404s until the file lands
    return assets


_STATIC_ASSETS = _load_static_assets()


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
    # 0.18.0 (FR-004): validate against the KNOWN roster (active ∪ retired),
    # matching valid_messages / _validated_for_state — otherwise a retired
    # identity's historical messages vanish from /api/messages, /messages/<id>,
    # and the index while the thread panel still shows them. The two surfaces
    # must agree.
    roster = store._known_roster(cfg)  # noqa: SLF001 — D3 parity
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
    now = datetime.now(timezone.utc).timestamp()
    agents = cfg.get("agents", []) or []
    agent_health = {}
    for a in agents:
        if not isinstance(a, str):
            continue
        hb = store.read_heartbeat(a)
        agent_health[a] = store.read_health(a, now_epoch=now, heartbeat=hb)
    return {
        "agenttalk_version": __version__,
        "project_root": str(store.root),
        "project_id": store.project_id(),
        "agents": agents,
        "agent_health": agent_health,
        "signing_enforced": store.signing_enforced(),
        "hmac_key": health.to_dict(),
        "invalid_messages": [
            {"id": mid, "reason": reason} for mid, reason in invalid
        ],
    }


# ----------------------------------------------- /api/state (schema v1)
#
# Everything below is READ-ONLY composition over existing pure surfaces
# (store reads + threads.derive_threads). No function in this section may
# call anything that writes: that is the dashboard's core safety claim,
# regression-proven by the full-tree hash test in tests/test_web.py.

def _parse_iso(value: Any) -> datetime | None:
    """Tolerant ISO-8601 parse for marker timestamps (None on garbage)."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _closed_rids_for(store: Store, agent: str) -> set[str]:
    # Parity with cli._closed_rids (web must not import the CLI layer).
    return {
        rid for rid, e in store.read_threadstate(agent).items()
        if isinstance(e, dict) and e.get("closed") is True
    }


def _validated_for_state(store: Store, cfg: dict) -> tuple[list[Message], int]:
    """One disk walk per root per request (research D8 / NFR-003).

    Parity with ``Store._validated_messages()`` — the KNOWN roster
    (active ∪ retired, the 0.16.0 D3 rule, so retired identities' open
    threads still derive) plus HMAC when enforced — but built from a
    single ``_scan_messages()`` pass. Calling the store's stacked
    surfaces here (``valid_messages`` + ``current_epoch`` +
    ``list_invalid_messages`` + ``unread_for``×N) would re-walk the
    message dir five-plus times per poll; at 1k messages that blew the
    2 s NFR by 5× in testing.

    Returns ``(validated messages sorted by id, invalid_count)`` where
    the count matches ``list_invalid_messages()``'s gate set (parse/
    schema failures + roster/signature rejects).
    """
    valid_scan, invalid_scan = store._scan_messages()  # noqa: SLF001 — same call doctor uses
    roster = store._known_roster(cfg)  # noqa: SLF001 — D3 parity with valid_messages()
    require_sig = store.signing_enforced()
    key: bytes | None = None
    project_id: str | None = None
    if require_sig:
        project_id = store.project_id()
        try:
            key = _signing.load_key(project_id)
        except (FileNotFoundError, OSError, ValueError):
            key = None  # enforcement on, key unreadable → refuse all (CLI parity)
    msgs: list[Message] = []
    rejects = 0
    for m in valid_scan:
        try:
            m.validate(roster)
        except ValueError:
            rejects += 1
            continue
        if require_sig:
            if key is None:
                rejects += 1
                continue
            try:
                _signing.verify_message(m.to_dict(), key,
                                        expected_key_id=project_id)
            except ValueError:
                rejects += 1
                continue
        msgs.append(m)
    msgs.sort(key=lambda m: m.id)
    return msgs, len(invalid_scan) + rejects


def _epoch_from(msgs: list[Message]) -> str | None:
    # Parity with Store.current_epoch() over the SAME validated set —
    # avoids that method's own whole-store re-scan (research D8).
    latest: str | None = None
    for m in msgs:
        b = (m.meta or {}).get("barrier")
        if (isinstance(b, dict) and b.get("scope") == "global"
                and "version" in b and "type" in b):
            if latest is None or m.id > latest:
                latest = m.id
    return latest


def _unread_count(msgs: list[Message], agent: str, cursor: str) -> int:
    # Parity with messages_for(..., since_id=cursor): recipient filter +
    # strictly-newer-than-cursor, over the same validated set.
    since = cursor or ""
    return sum(1 for m in msgs
               if m.recipient == agent and (not since or m.id > since))


def _epoch_status(opener_meta: dict, current: str | None) -> str:
    """The exact `check --epoch` three-state vocabulary (research D6).

    - no barrier yet → nothing can be stale → ``current``
    - opener meta lacks the key entirely (pre-0.16 sender) → cannot be
      ordered against a live barrier → ``unknown-pre-epoch``
    - stamped null (epoch-aware, sent before any barrier) or with an
      older id → ``previous-epoch``
    """
    if current is None:
        return "current"
    if "epoch_at_send" not in opener_meta:
        return "unknown-pre-epoch"
    return "current" if opener_meta.get("epoch_at_send") == current else "previous-epoch"


def _inject_next(d: dict, t: Thread) -> None:
    # Parity with cli._inject_next: surfaced conditionally so terminal
    # threads carry neither key (absent-not-null).
    if getattr(t, "next_action", None) is not None:
        d["next_action"] = t.next_action
    if getattr(t, "next_owner", None) is not None:
        d["next_owner"] = t.next_owner


def _thread_verdict(m: Message) -> str | None:
    """The decision a single message carries, or None (§3b). SAFE: reads only
    the envelope kind + ``meta.status`` — never the body. A ``review-result``
    forwards its status verbatim; a ``proposal-response`` only when the status
    is one of the known proposal outcomes. Anything else carries no verdict.

    Gate verdicts are NOT thread messages: "gate" is not a bus message kind, so
    gate HOLDs never reach here. They surface via /api/attention (file-based),
    not thread transcripts."""
    kind = m.kind
    status = (m.meta or {}).get("status")
    status = status if isinstance(status, str) and status else None
    if kind == "review-result":
        return status  # e.g. "approved" / "changes_requested" — verbatim envelope
    if kind == "proposal-response":
        return status if status in ("accepted", "rejected", "countered") else None
    return None


def _derive_root_threads(
    store: Store, msgs: list[Message], roster: list[str],
    current: str | None,
) -> tuple[list[dict], list[dict], int]:
    """(open thread rows, broadcast summaries, closed-thread count).

    Derives per roster agent with the existing pure derivation, then
    collapses to ONE row per request_id using the ball-holder rule
    (research D5):

    1. prefer a non-terminal view whose ``next_owner`` is the viewing
       agent itself — the ball-holder's own perspective;
    2. else the requester's non-terminal view (everyone is waiting on a
       peer / a broadcast pending set);
    3. all views terminal → the thread is closed: excluded from rows,
       counted instead.

    ``next_owner``/``next_action`` from the derivation are already
    absolute agent names (or a pending list), so the collapsed row needs
    no relabeling.
    """
    msgs_sorted = sorted(msgs, key=lambda m: m.id)
    openers: dict[str, Message] = {}
    verdicts: dict[str, str] = {}  # rid -> latest decision (0.58.0, §3b)
    for m in msgs_sorted:
        rid = (m.meta or {}).get("request_id")
        if not (isinstance(rid, str) and rid):
            continue
        if rid not in openers:
            openers[rid] = m
        v = _thread_verdict(m)
        if v is not None:
            verdicts[rid] = v  # msgs_sorted is ascending → last write is newest

    retired = set(store.retired_agents())  # 0.18.0: tombstones aren't owed moves
    views: dict[str, list[tuple[str, Thread]]] = {}
    for a in roster:
        derived = derive_threads(
            msgs_sorted, agent=a, cursor=store.cursor(a) or "",
            closed_rids=_closed_rids_for(store, a), retired=retired,
        )
        for t in derived:
            views.setdefault(t.request_id, []).append((a, t))

    rows: list[dict] = []
    broadcasts: list[dict] = []
    closed = 0
    for rid, pairs in views.items():
        open_pairs = [(a, t) for a, t in pairs
                      if t.state not in _TERMINAL_STATES]
        if not open_pairs:
            closed += 1
            continue
        chosen: tuple[str, Thread] | None = None
        for a, t in open_pairs:  # rule 1: the ball-holder's own view
            if getattr(t, "next_owner", None) == a:
                chosen = (a, t)
                break
        if chosen is None:  # rule 2: the requester's view
            opener = openers.get(rid)
            requester = opener.sender if opener is not None else None
            for a, t in open_pairs:
                if a == requester:
                    chosen = (a, t)
                    break
        if chosen is None:  # roster order is the deterministic fallback
            chosen = open_pairs[0]
        _, t = chosen
        d = t.to_dict()
        # Defensive (fresh-eyes 0.17.0 note): to_dict() adds a `rescind`
        # block only on closed-superseded threads, and those are terminal
        # → excluded above, so this is dead code TODAY. But its `reason`
        # field carries sender-supplied body text, which must never enter
        # /api/state (FR-003) — don't let that hang on an implicit
        # invariant two functions away.
        d.pop("rescind", None)
        _inject_next(d, t)
        opener = openers.get(rid)
        ometa = (opener.meta or {}) if opener is not None else {}
        if isinstance(ometa.get("mission"), str):
            d["mission"] = ometa["mission"]
        if isinstance(ometa.get("wp_id"), str):
            d["wp_id"] = ometa["wp_id"]
        # 0.58.x (P1): the thread's two fixed endpoints, perspective-independent
        # (the client renders "a ⇄ b" + the active-review graph edge from them).
        # Both are envelope-safe agent-name strings, absent-not-null.
        if opener is not None:
            d["opener"] = opener.sender
            peer = opener.recipient
            if isinstance(peer, str) and peer and peer != opener.sender:
                d["opener_peer"] = peer
        if "epoch_at_send" in ometa:
            # forwarded EXACTLY as stored: the 0.16.0 three-state
            # (absent / null / id) must survive serialization.
            d["epoch_at_send"] = ometa["epoch_at_send"]
        d["epoch_status"] = _epoch_status(ometa, current)
        # 0.58.0 (§3b): the thread's latest decision, if any (absent-not-null);
        # meta.status only — never body. Omitted when the thread has no verdict.
        if rid in verdicts:
            d["verdict"] = verdicts[rid]
        # active_review: emit ONLY when true (absent-not-null) — a non-terminal
        # review-request/proposal thread drives the dashed/animated graph edge.
        if d.get("opener_kind") in ("review-request", "proposal"):
            d["active_review"] = True
        rows.append(d)
        if d.get("is_broadcast"):
            broadcasts.append({
                "request_id": d["request_id"],
                "subject": d.get("subject"),
                "opener_kind": d.get("opener_kind"),
                "requester": opener.sender if opener is not None else None,
                "audience": d.get("audience"),
                "responded": d.get("responded"),
                "pending": d.get("pending"),
                "age_seconds": d.get("age_seconds"),
            })
    rows.sort(key=lambda r: r.get("last_msg_id") or "", reverse=True)
    broadcasts.sort(key=lambda b: b.get("request_id") or "")
    return rows, broadcasts, closed


# ------------------------------------------------- 0.58.0 per-agent enrichers
#
# All are absent-not-null: an undeterminable field is OMITTED entirely (§3a).
# None reads the message dir — they compose the already-loaded `cfg`/`msgs` plus
# cheap per-agent state reads, so the single-scan perf discipline holds. The
# owned-domains map is built ONCE per root (not per agent).

_CLI_VALUES = ("claude", "codex")


def _infer_cli(agent: str, health: dict, capacity_snap: dict | None) -> str | None:
    """Best-effort ``"claude" | "codex"`` for an agent (§3a). Order: a fresh
    health ``cli`` field, else the capacity snapshot ``source`` prefix, else the
    agent-name prefix. Omit (None) if nothing is determinable."""
    hc = health.get("cli") if isinstance(health, dict) else None
    if hc in _CLI_VALUES:
        return hc
    src = capacity_snap.get("source") if isinstance(capacity_snap, dict) else None
    if isinstance(src, str):
        # capacity `source` is e.g. "claude_statusline" / "codex_rollout".
        for cli in _CLI_VALUES:
            if src.startswith(cli):
                return cli
    prefix = agent.split("-", 1)[0]
    if prefix in _CLI_VALUES:
        return prefix
    return None


def _capacity_entry(snap: dict | None, *, now: datetime) -> dict | None:
    """The `capacity` object (§3a) or None when no snapshot exists. `null`
    percents are allowed INSIDE this object (a snapshot may carry only one
    signal); the absent-not-null rule is about the `capacity` KEY itself."""
    if not isinstance(snap, dict):
        return None
    rate = snap.get("primary_used_percent")
    ctx = snap.get("context_used_percent")
    return {
        "rate_used_pct": rate if isinstance(rate, (int, float)) else None,
        "context_used_pct": ctx if isinstance(ctx, (int, float)) else None,
        "confidence": _map_confidence(_capacity.effective_confidence(snap, now=now)),
    }


def _map_confidence(eff: str) -> str:
    """Map capacity's reader-confidence vocabulary (observed|stale|unknown) onto
    the wire enum the console expects (fresh|stale|unknown)."""
    if eff == "observed":
        return "fresh"
    if eff == "stale":
        return "stale"
    return "unknown"


def _is_wrapped(health: dict) -> bool | None:
    """True when the agent is determinably wrapped/supervised (§3a): the health
    snapshot's ``mode`` names a wrapped loop. Omit (None) when unknown — an
    `unknown` health view carries no mode we can trust.

    Wrapped modes: anything starting with ``wrapper`` (``wrapper-loop`` /
    ``wrapper-one-shot``) or the exact ``lead-loop``. ``manual``/``listen`` are
    explicitly unwrapped."""
    if not isinstance(health, dict):
        return None
    mode = health.get("mode")
    if not isinstance(mode, str) or not mode:
        return None
    m = mode.lower()
    if m.startswith("wrapper") or m == "lead-loop":
        return True
    if "manual" in m or "listen" in m:
        return False
    return None


def _owned_domains_map(store: Store, cfg: dict) -> dict[str, list[dict]]:
    """Invert the domain registry into ``{agent: [{name, globs}, ...]}`` ONCE
    per root (§3a). Reuses the same resolution the CLI's `_roster_expertise`
    uses (``domains.resolve_refset`` over each domain's owners). A missing or
    malformed registry degrades to an empty map (never raises up)."""
    out: dict[str, list[dict]] = {}
    try:
        reg = _domains.load_registry(store.dir / _domains.FILENAME, cfg)
    except _domains.DomainError:
        return out  # malformed registry is advisory here — no owned_domains
    for did, dentry in (reg.data.get("domains") or {}).items():
        if not isinstance(dentry, dict):
            continue
        globs = dentry.get("owned_globs") or []
        name = dentry.get("title") or did
        for owner in _domains.resolve_refset(dentry.get("owners") or {}, cfg):
            out.setdefault(owner, []).append({"name": name, "globs": list(globs)})
    return out


def _agent_task(agent: str, threads_rows: list[dict], composing: list[dict]) -> str | None:
    """A synthesized "current work" line (§3a), envelope-derived (subjects/meta
    already on the wire — no body). First match wins:
      (a) the subject of the agent's newest non-terminal open thread where it is
          ``next_owner``;
      (b) else ``mission · <m> · <wp>`` from such a thread;
      (c) else, if composing, ``composing a reply to <peer>``;
      (d) else omit.
    ``threads_rows`` is already newest-first (sorted by last_msg_id desc)."""
    for row in threads_rows:
        owner = row.get("next_owner")
        owns = owner == agent or (isinstance(owner, list) and agent in owner)
        if not owns:
            continue
        subj = row.get("subject")
        if isinstance(subj, str) and subj.strip():
            return subj
        mission, wp = row.get("mission"), row.get("wp_id")
        if isinstance(mission, str) and mission:
            return f"mission · {mission}" + (f" · {wp}" if isinstance(wp, str) and wp else "")
        break  # newest owned thread had no usable label — fall through to composing
    if composing:
        peer = composing[0].get("peer")
        if isinstance(peer, str) and peer:
            return f"composing a reply to {peer}"
        return "composing a reply"
    return None


class HealthTimelineRing:
    """Server-instance-owned, IN-MEMORY health-state history (§5).

    Never a file (the read-only invariant) and never a module global (avoids
    cross-test leakage — one instance per server, created in ``_make_handler``).
    Records ``(now, state)`` per (root-label, agent) into a bounded deque,
    prunes entries older than the ~30-min window, and collapses contiguous
    same-state samples into ``{state, seconds}`` segments oldest→newest.

    Best-effort: sampling and rendering both swallow errors so a timeline glitch
    can never affect the /api/state payload's core fields.
    """

    def __init__(self, *, window_seconds: float = _HEALTH_TIMELINE_WINDOW_SECONDS) -> None:
        self._window = float(window_seconds)
        self._samples: dict[tuple[str, str], list[tuple[float, str]]] = {}
        self._lock = threading.Lock()

    def record(self, root_label: str, agent: str, state: str, *, now: float) -> None:
        try:
            key = (root_label, agent)
            with self._lock:
                seq = self._samples.setdefault(key, [])
                seq.append((now, state))
                cutoff = now - self._window
                # prune from the front (oldest first); keep at most a bounded tail
                while seq and seq[0][0] < cutoff:
                    seq.pop(0)
        except Exception:  # noqa: BLE001, S110 — a ring glitch must never affect the payload
            pass

    def segments(self, root_label: str, agent: str, *, now: float) -> list[dict]:
        """Contiguous ``{state, seconds}`` segments over the window, oldest→newest.
        Returns [] when no samples (client shows a "building history…" placeholder)."""
        try:
            with self._lock:
                seq = list(self._samples.get((root_label, agent), ()))
            if not seq:
                return []
            segs: list[dict] = []
            for i, (ts, state) in enumerate(seq):
                end = seq[i + 1][0] if i + 1 < len(seq) else now
                dur = max(0.0, end - ts)
                if segs and segs[-1]["state"] == state:
                    segs[-1]["seconds"] = round(segs[-1]["seconds"] + dur, 3)
                else:
                    segs.append({"state": state, "seconds": round(dur, 3)})
            return segs
        except Exception:  # noqa: BLE001
            return []


def _agent_entries(store: Store, cfg: dict, msgs: list[Message],
                   liaison: str | None, *,
                   threads_rows: list[dict] | None = None,
                   owned_domains: dict[str, list[dict]] | None = None,
                   history: "HealthTimelineRing | None" = None,
                   root_label: str | None = None) -> list[dict]:
    """Per-agent presence rows (data-model §3). Absent-not-null keys.

    0.58.0 additive fields (all OMITTED when not determinable): ``cli``,
    ``capacity``, ``wrapped``, ``restartable``, ``owned_domains``, ``task``,
    and (best-effort) ``health_timeline``. ``threads_rows`` / ``owned_domains``
    are precomputed ONCE per root by the caller so no per-agent re-scan happens.
    """
    roles = cfg.get("roles", {}) or {}
    groups = cfg.get("groups", {}) or {}
    threads_rows = threads_rows or []
    owned_domains = owned_domains or {}
    now = datetime.now(timezone.utc)
    now_epoch = now.timestamp()
    # 0.19.0 (FR-001): per-agent message counts from the SAME validated `msgs`
    # already passed in — no extra scan. Always-present integers (0 is data).
    sent_counts: Counter = Counter(m.sender for m in msgs)
    recv_counts: Counter = Counter(m.recipient for m in msgs)
    out: list[dict] = []
    for a in cfg.get("agents", []) or []:
        e: dict[str, Any] = {"name": a}
        if roles.get(a):
            e["role"] = roles[a]
        in_groups = sorted(
            g for g, members in groups.items()
            if isinstance(members, list) and a in members
        )
        if in_groups:
            e["groups"] = in_groups
        if a == liaison:
            e["operator_facing"] = True
        hb = store.read_heartbeat(a)
        if hb is not None:
            e["last_seen"] = hb.isoformat()
            e["last_seen_age_seconds"] = round((now - hb).total_seconds(), 3)
        health = store.read_health(a, now_epoch=now_epoch, heartbeat=hb)
        e["health"] = health
        e["unread"] = _unread_count(msgs, a, store.cursor(a))
        e["sent"] = sent_counts.get(a, 0)
        e["received"] = recv_counts.get(a, 0)
        threads_map = store.read_composing_intent(a).get("threads", {})
        composing: list[dict] = []
        if isinstance(threads_map, dict):
            for rid in sorted(threads_map):
                ent = threads_map[rid]
                if not isinstance(ent, dict):
                    continue
                at = _parse_iso(ent.get("at"))
                if at is None:
                    continue  # unparseable marker entry — skip, don't guess
                age = round((now - at).total_seconds(), 3)
                if age < 0 or age > COMPOSING_INTENT_STALE_SECONDS:
                    # Mirror the CLI's active-window rule (cli.py _composing
                    # freshness): a crashed/abandoned writer — or a clock-skewed
                    # future marker (negative age) — is NOT actively composing.
                    # Without this the dashboard shows it as composing forever
                    # (review C2/M1).
                    continue
                composing.append({
                    "request_id": rid,
                    "peer": ent.get("peer"),
                    "age_seconds": age,
                })
        if composing:
            e["composing"] = composing

        # --- 0.58.0 additive fields (absent-not-null) ---
        snap = store.read_capacity(a)
        cli = _infer_cli(a, health, snap)
        if cli is not None:
            e["cli"] = cli
        cap = _capacity_entry(snap, now=now)
        if cap is not None:
            e["capacity"] = cap
        wrapped = _is_wrapped(health)
        if wrapped is not None:
            e["wrapped"] = wrapped
            # restartable mirrors wrapped for v1 (only wrapped agents restart).
            e["restartable"] = wrapped
        owned = owned_domains.get(a)
        if owned:
            e["owned_domains"] = owned
        task = _agent_task(a, threads_rows, composing)
        if task is not None:
            e["task"] = task
        # health_timeline: best-effort in-memory ring (§5). Record this tick,
        # then emit the collapsed segments — omitted entirely when history is
        # None (build_state stays pure) or no meaningful samples have
        # accumulated. A bare `unknown` (no/stale health snapshot at all) is NOT
        # recorded: the timeline plots OBSERVED health states, and recording
        # `unknown` would make the field appear for every agent on the first
        # poll (defeating the client's "building history…" placeholder).
        if history is not None and root_label is not None:
            state = health.get("state") if isinstance(health, dict) else None
            if isinstance(state, str) and state != "unknown":
                history.record(root_label, a, state, now=now_epoch)
            segs = history.segments(root_label, a, now=now_epoch)
            if segs:
                e["health_timeline"] = segs
        out.append(e)
    return out


def _root_state(desc: RootDescriptor,
                history: "HealthTimelineRing | None" = None) -> dict:
    """One root's full snapshot — or its degraded errors-as-data form.

    A failure ANYWHERE in this root's collection yields
    ``{label, path, errors:[...]}`` with no partial data fields; it must
    never escape (one corrupt root would 500 the whole aggregate,
    violating FR-005).

    ``history`` (0.58.0, §5): the server's in-memory health-timeline ring,
    passed only from the /api/state route handler. None (the default, and every
    direct ``build_state`` unit-test call) omits ``health_timeline`` and keeps
    the build pure.
    """
    label, path = desc.label, str(desc.store.root)
    try:
        store = desc.store
        cfg = store.load_config()
        roster = cfg.get("agents", []) or []
        # ONE disk walk per root per request (D8) — see _validated_for_state.
        msgs, invalid_count = _validated_for_state(store, cfg)
        current = _epoch_from(msgs)
        threads_rows, broadcasts, closed_count = _derive_root_threads(
            store, msgs, roster, current)
        # Domain-owner map: built ONCE per root (not per agent) so the
        # single-scan discipline holds (§3a).
        owned_domains = _owned_domains_map(store, cfg)
        out: dict[str, Any] = {
            "label": label,
            "path": path,
            "project_id": store.project_id(),
            "errors": [],
            "signing_enforced": store.signing_enforced(),
            "epoch": current,
            "counts": {
                "messages": len(msgs),
                "invalid": invalid_count,
                "open_threads": len(threads_rows),
                "closed_threads": closed_count,
            },
        }
        liaison = store.operator_facing()
        if liaison:
            out["operator_facing"] = liaison
        out["agents"] = _agent_entries(
            store, cfg, msgs, liaison,
            threads_rows=threads_rows, owned_domains=owned_domains,
            history=history, root_label=label)
        out["retired"] = store.retired_agents()
        out["threads"] = threads_rows
        out["broadcasts"] = broadcasts
        # 0.19.0 (FR-002/003): who-talks-to-whom traffic edges from the SAME
        # validated `msgs`. One Counter over (from,to) pairs, EXCLUDING
        # self-addressed messages (e.g. barriers) and INCLUDING broadcast
        # fan-out copies (this is traffic volume, not unique-thread semantics).
        # Sorted count desc with a deterministic (from,to) tiebreak so the
        # list is stable across polls; capped to the top 50 with an additive
        # truncation signal. HEALTHY-root only — the degraded branch below
        # keeps the errors-as-data shape.
        pairs: Counter = Counter(
            (m.sender, m.recipient) for m in msgs if m.sender != m.recipient)
        out["edges"] = [
            {"from": f, "to": t, "count": c}
            for (f, t), c in sorted(pairs.items(),
                                    key=lambda kv: (-kv[1], kv[0]))[:_EDGE_LIMIT]
        ]
        if len(pairs) > _EDGE_LIMIT:
            out["edges_truncated"] = True
            out["edge_limit"] = _EDGE_LIMIT
        # Recent-activity feed (0.58.0, additive): the last messages ENVELOPE-ONLY
        # (id/ts/from/to/kind/subject — NEVER body; /api/state carries no bus body
        # text, test-enforced). `msgs` is sorted ascending by id, so the tail is the
        # most recent; emit most-recent-first, capped at _RECENT_LIMIT.
        out["recent"] = [
            {"id": m.id, "ts": m.ts, "from": m.sender, "to": m.recipient,
             "kind": m.kind, "subject": m.subject or ""}
            for m in reversed(msgs[-_RECENT_LIMIT:])
        ]
        kdir = store.root / "kitty-specs"
        if kdir.is_dir():
            # Filesystem detection ONLY (FR-008): never import spec-kitty.
            out["spec_kitty"] = {
                "kitty_specs_dir": str(kdir),
                "missions": sorted(
                    p.name for p in kdir.iterdir() if p.is_dir()),
            }
        return out
    except Exception as e:  # noqa: BLE001
        # Degrade to errors-as-data for ANY failure, not just OSError/ValueError:
        # this function's whole contract (FR-005) is that one corrupt root must
        # never escape and 500 the entire /api/state aggregate. A broad catch is
        # strictly safer than propagating an unanticipated exception type
        # (review). The errors-as-data shape is the documented degraded form.
        return {"label": label, "path": path, "errors": [str(e)]}


def build_state(roots: list[RootDescriptor],
                *, history: "HealthTimelineRing | None" = None) -> dict:
    """The /api/state aggregate (data-model.md, schema v1).

    ``generated_at`` is informational only — message ids remain the
    bus's sole ordering primitive.

    PURE by default: ``history`` is None unless the /api/state route handler
    passes the server's in-memory ring, so every unit/perf test that calls
    ``build_state(roots)`` directly gets a deterministic payload with no
    ``health_timeline`` and no ring side effects (§5).
    """
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "agenttalk_version": __version__,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "roots": [_root_state(d, history) for d in roots],
    }


# --------------------------------------------------- /api/attention (§4a)
#
# The ranked "needs a human" queue for root[0]. Composed from the PURE
# attention.build_queue (escalations / gate HOLD / dead-letter / lead-unarmed /
# capacity / config-blocked / close HOLD) PLUS a derived STUCK item per agent
# whose advisory health.state == "stuck_suspected" (stuck is NOT a build_queue
# source). READ-ONLY: it lists items; it never disposes them (no writes in v1).
# web.py does NOT import the CLI layer, so the source collection mirrors
# cli._collect_attention_items here, each source INDEPENDENTLY fail-safe.

# Internal attention source -> the design's coarse wire source + label + severity.
_ATTENTION_SOURCE_MAP: dict[str, tuple[str, str, str]] = {
    _attention.SOURCE_NEEDS_OPERATOR: ("escalation", "ESCALATION", "high"),
    _attention.SOURCE_CONFIG_BLOCKED: ("gate", "GATE HOLD", "high"),
    _attention.SOURCE_GATE_HOLD: ("gate", "GATE HOLD", "high"),
    _attention.SOURCE_CLOSE_HOLD: ("gate", "GATE HOLD", "high"),
    _attention.SOURCE_DEAD_LETTER: ("deadletter", "DEAD LETTER", "med"),
    _attention.SOURCE_LEAD_UNARMED: ("supervisor", "SUPERVISOR", "low"),
    _attention.SOURCE_CAPACITY: ("supervisor", "SUPERVISOR", "low"),
    _attention.SOURCE_ERROR: ("supervisor", "SUPERVISOR", "low"),
}


def _attention_agent(item: dict) -> str | None:
    """Best-effort agent the item concerns, from its envelope source_refs
    (never body). Returns None when the source is not agent-scoped."""
    for ref in item.get("source_refs") or []:
        if isinstance(ref, dict) and isinstance(ref.get("agent"), str):
            return ref["agent"]
    return None


def _collect_web_attention_items(store: Store, roster: list[str],
                                 for_agent: str | None) -> list[dict]:
    """Mirror of cli._collect_attention_items, in-process (web must not import
    the CLI). Each source read is INDEPENDENTLY fail-safe — one bad source
    yields a bounded source_error item, never blanks the queue. needs_operator
    is skipped (not an error) when no liaison/sole-lead resolved (read-only view)."""
    A = _attention
    items: list[dict] = []
    if for_agent:
        try:
            items += A.needs_operator_items(_web_needs_operator(store, for_agent))
        except Exception as e:  # noqa: BLE001
            items.append(A.source_error_item("needs_operator", str(e)))
    try:
        holds = [h for a in roster if (h := store.read_config_blocked_hold(a))]
        items += A.config_blocked_items(holds)
    except Exception as e:  # noqa: BLE001
        items.append(A.source_error_item("config_blocked", str(e)))
    try:
        items += A.dead_letter_items(
            [{"agent": d.get("agent"), "message_id": d.get("message_id")}
             for d in store.list_dead_letters()])
    except Exception as e:  # noqa: BLE001
        items.append(A.source_error_item("dead_letter", str(e)))
    try:
        from agenttalk import gates as _gates
        items += A.gate_hold_items(_gates.check_gates(store.root).get("blockers", []))
    except Exception as e:  # noqa: BLE001
        items.append(A.source_error_item("gate_hold", str(e)))
    try:
        signals = []
        for a in store.managed_lead_loop_agents():
            st = store.lead_loop_state(a)
            if st.get("managed") and not st.get("armed"):
                signals.append({"agent": a, "reason": st.get("reason") or "lead-loop unarmed"})
        items += A.lead_unarmed_items(signals)
    except Exception as e:  # noqa: BLE001
        items.append(A.source_error_item("lead_unarmed", str(e)))
    return items


def _web_needs_operator(store: Store, for_agent: str) -> list[dict]:
    """Pending needs_operator escalations from ``for_agent``'s thread view, each
    as {request_id, subject, sender, age_seconds, meta}. Parity with the CLI's
    ``_needs_operator_items``: an escalation is an opener carrying
    ``meta.needs_operator == "true"`` whose derived thread is still
    ``operator_state == "pending"``. Envelope-only — the opener meta (incl.
    meta.attention) feeds the fail-safe parser; the body is never read."""
    now = datetime.now(timezone.utc)
    cfg = store.load_config()
    msgs, _ = _validated_for_state(store, cfg)
    msgs_sorted = sorted(msgs, key=lambda m: m.id)
    opener_meta: dict[str, dict] = {}
    opener_sender: dict[str, str] = {}
    for m in msgs_sorted:
        rid = (m.meta or {}).get("request_id")
        if rid and (m.meta or {}).get("needs_operator") == "true" and rid not in opener_meta:
            opener_meta[rid] = m.meta or {}
            opener_sender[rid] = m.sender
    retired = set(store.retired_agents())
    rows = derive_threads(msgs_sorted, agent=for_agent,
                          cursor=store.cursor(for_agent) or "", now=now,
                          closed_rids=_closed_rids_for(store, for_agent),
                          retired=retired)
    return [{"request_id": t.request_id, "subject": t.subject,
             "sender": opener_sender.get(t.request_id, ""),
             "age_seconds": t.age_seconds, "meta": opener_meta.get(t.request_id, {})}
            for t in rows if t.needs_operator and t.operator_state == "pending"]


def _derive_stuck_items(agents: list[dict], *, now: datetime) -> list[dict]:
    """One STUCK attention item per agent whose advisory health.state ==
    ``stuck_suspected`` (§4a — NOT a build_queue source). Envelope-derived; the
    detail line names the agent, never any body."""
    stuck: list[dict] = []
    for a in agents:
        health = a.get("health") or {}
        if health.get("state") != "stuck_suspected":
            continue
        name = a.get("name")
        age = a.get("last_seen_age_seconds")
        stuck.append({
            "id": f"stuck:{name}",
            "source": "stuck",
            "source_label": "STUCK",
            "severity": "med",
            "title": f"{name} looks stuck",
            "agent": name,
            "detail": "no forward progress on this agent's turn (advisory health)",
            "age_seconds": float(age) if isinstance(age, (int, float)) else 0.0,
            "human_can_unblock_now": True,
        })
    return stuck


def build_attention(desc: RootDescriptor,
                    agents: list[dict] | None = None) -> dict:
    """The /api/attention payload for one root (§4a). Envelope-only: every
    string here is envelope-derived — a raw message body NEVER lands in a
    field. ``agents`` (root[0]'s /api/state agent rows) supplies the derived
    STUCK items; when omitted they are computed from a fresh scan."""
    store = desc.store
    now = datetime.now(timezone.utc)
    cfg = _safe_load_config(store)
    roster = cfg.get("agents", []) or []
    for_agent = store.operator_facing() or store.sole_lead()
    items = _collect_web_attention_items(store, roster, for_agent)
    disps, _problems = _attention.read_dispositions(store)
    queue = _attention.build_queue(items, disps,
                                   now_iso=now.isoformat().replace("+00:00", "Z"))
    wire: list[dict] = []
    for it in queue.get("items", []):
        src = it.get("source", "")
        mapped = _ATTENTION_SOURCE_MAP.get(src)
        if mapped is None:
            continue  # unknown internal source — skip rather than mislabel
        wire_source, source_label, severity = mapped
        title = it.get("title") or "attention needed"
        detail = it.get("why_it_matters") or ""
        wire.append({
            "id": it.get("item_id", ""),
            "source": wire_source,
            "source_label": source_label,
            "severity": severity,
            "title": _envelope_str(title),
            "agent": _attention_agent(it),
            "detail": _envelope_str(detail),
            "age_seconds": float(it.get("age_seconds") or 0),
            "human_can_unblock_now": bool(it.get("human_can_unblock_now")),
        })
    if agents is None:
        try:
            agents = _agent_entries(store, cfg, _validated_for_state(store, cfg)[0],
                                    for_agent)
        except Exception:  # noqa: BLE001 — stuck items are best-effort
            agents = []
    wire.extend(_derive_stuck_items(agents, now=now))
    return {"root": desc.label, "items": wire, "count": len(wire)}


# NEEDS_OPERATOR titles/details are typed single-line fields the attention layer
# already caps; but as defense-in-depth for §4a's "NEVER raw message body"
# contract we bound every surfaced string to one line and a hard length so no
# multi-paragraph prose can ride a field. Envelope summaries are short by design.
_ENVELOPE_MAX = 300


def _envelope_str(value: Any) -> str:
    """Coerce an envelope-derived value to a short, single-line summary string.
    Strips newlines and truncates — belt-and-braces so no body-ish prose leaks
    through a field that is contractually envelope-only (§4a)."""
    s = "" if value is None else str(value)
    s = s.replace("\r", " ").replace("\n", " ").strip()
    if len(s) > _ENVELOPE_MAX:
        s = s[: _ENVELOPE_MAX - 1].rstrip() + "…"
    return s


# ------------------------------------------------ /api/thread/<rid> (§4b)

def _cli_from_prefix(name: str) -> str | None:
    """Infer ``"claude" | "codex"`` from an agent-name prefix, else None (§4b)."""
    prefix = (name or "").split("-", 1)[0]
    return prefix if prefix in _CLI_VALUES else None


def _thread_meta_line(meta: dict) -> str:
    """A SAFE, pre-formatted meta summary from a strict whitelist (§4b). Only
    ``status``/``head``/``base`` are surfaced — never arbitrary meta, which may
    carry body-ish sender text. Empty string when none are present."""
    parts: list[str] = []
    for key in _META_LINE_WHITELIST:
        v = meta.get(key)
        if isinstance(v, str) and v.strip():
            # Belt-and-braces (§4a): coerce to a single line + hard cap so no
            # multi-line/multi-paragraph value can ride a whitelisted field.
            sv = _envelope_str(v)
            parts.append(f"{key}={sv}" if key == "status" else f"{key} {sv}")
        elif isinstance(v, (int, float, bool)):
            parts.append(f"{key}={_envelope_str(v)}")
    return " · ".join(parts)


def build_thread(store: Store, rid: str) -> dict | None:
    """One thread's full transcript, CARRYING RAW BODIES (§4b). Returns None
    when no validated message has ``meta.request_id == rid`` (the route 404s).

    Messages are the validated set (roster + kind + HMAC when enforced) — the
    same surface /api/state derives from — ordered by id ascending. Bodies are
    RAW (JSON transport); the client MUST render them via ``textContent``. The
    caller validates ``rid`` against ``_MESSAGE_ID_RE`` BEFORE this touches disk.
    """
    now = datetime.now(timezone.utc)
    msgs = [m for m in _all_messages(store)
            if (m.meta or {}).get("request_id") == rid]
    if not msgs:
        return None
    msgs.sort(key=lambda m: m.id)
    opener = msgs[0]
    participants: list[str] = []
    for m in msgs:
        for who in (m.sender, m.recipient):
            if who and who not in participants:
                participants.append(who)
    out_msgs: list[dict] = []
    for m in msgs:
        age = _age_seconds_of(m.ts, now=now)
        out_msgs.append({
            "id": m.id,
            "from": m.sender,
            "to": m.recipient,
            "kind": m.kind,
            "ts": m.ts,
            "age_seconds": age,
            "cli": _cli_from_prefix(m.sender),
            "body": m.body or "",  # RAW — client renders via textContent
            "meta_line": _thread_meta_line(m.meta or {}),
        })
    return {
        "request_id": rid,
        "subject": opener.subject or "",
        "participants": participants,
        "kind": opener.kind,
        "messages": out_msgs,
    }


def _age_seconds_of(ts: Any, *, now: datetime) -> float | None:
    """Seconds since a message ``ts`` (ISO-8601), or None if unparseable."""
    dt = _parse_iso(ts)
    if dt is None:
        return None
    return round((now - dt).total_seconds(), 3)


# ------------------------------------------------------------ HTTP handler

def _make_handler(roots: list[RootDescriptor]) -> type[BaseHTTPRequestHandler]:
    """Build a request handler class closed over the watched roots.

    Returns a class (not an instance) because ``ThreadingHTTPServer``
    instantiates it per connection. ``roots[0]`` is the binding target
    for every pre-0.17.0 route (FR-009).
    """
    store = roots[0].store
    # Health-timeline ring (§5): one instance per SERVER (closed over here, not
    # a module global) so parallel test servers never share history. In-memory
    # only — never a file (the read-only invariant). Handler instances are
    # created per connection, so the ring lives on this closure, shared across
    # them for the lifetime of the server.
    health_history = HealthTimelineRing()

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
            return _is_loopback_addr(self.client_address[0] or "")

        # ---- response helpers
        def _send(self, status: int, body: bytes, content_type: str,
                  csp: str | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            # Per-route CSP (0.17.0): None means the pre-0.17.0 policy,
            # byte-identical. Only /dashboard opts into a different one.
            self.send_header("Content-Security-Policy",
                             csp if csp is not None else _DEFAULT_CSP)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _send_html(self, status: int, body: bytes,
                       csp: str | None = None) -> None:
            self._send(status, body, "text/html; charset=utf-8", csp)

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
            if path == "/api/state":
                # Pass the server's in-memory ring so /api/state records a
                # health sample this tick and emits health_timeline (§5). JSON
                # feed keeps the strict _DEFAULT_CSP.
                self._send_json(HTTPStatus.OK,
                                build_state(roots, history=health_history))
                return
            if path == "/api/attention":
                # Ranked "needs a human" queue for root[0] (§4a). Envelope-only,
                # read-only, strict _DEFAULT_CSP (JSON is not executed).
                self._send_json(HTTPStatus.OK, build_attention(roots[0]))
                return
            if path == "/dashboard":
                self._send_html(HTTPStatus.OK, render_dashboard(roots),
                                csp=_DASHBOARD_CSP)
                return
            if path.startswith("/static/"):
                # Allowlisted-filename static assets (§2): EXACT dict lookup,
                # NEVER a path join on request input — the name can only ever
                # match a literal key, so traversal is impossible. Unknown /
                # not-yet-present name -> 404.
                name = path[len("/static/"):]
                asset = _STATIC_ASSETS.get(name)
                if asset is None:
                    self._error_html(HTTPStatus.NOT_FOUND, "not found")
                    return
                ctype, data = asset
                self._send(HTTPStatus.OK, data, ctype)
                return
            if path.startswith("/api/thread/"):
                # One thread's full transcript — the ONLY route that carries
                # message bodies (§4b). Validate the rid BEFORE any disk touch
                # (traversal parity with /messages/<id>). JSON feed → strict CSP.
                rid = path[len("/api/thread/"):]
                if not _MESSAGE_ID_RE.match(rid):
                    self._send_json(HTTPStatus.NOT_FOUND,
                                    {"error": "thread not found"})
                    return
                thread = build_thread(store, rid)
                if thread is None:
                    self._send_json(HTTPStatus.NOT_FOUND,
                                    {"error": "thread not found"})
                    return
                self._send_json(HTTPStatus.OK, thread)
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
                *, quiet: bool = True,
                extra: list[RootDescriptor] | None = None,
                ) -> ThreadingHTTPServer:
    """Build (but do not start) a dashboard HTTP server.

    Refuses to bind to anything but a loopback host
    (:data:`LOOPBACK_HOSTS`). There is no opt-in to bind elsewhere:
    the dashboard has no auth, renders un-sandboxed message bodies,
    and is only safe for the local user. Use an SSH tunnel if you
    need to view it from another machine.

    ``extra`` (0.17.0, additive): further roots to watch alongside
    ``store``. ``store`` is always root[0] — the pre-0.17.0 routes bind
    to it; ``/api/state`` and ``/dashboard`` cover all roots. Omitted →
    exactly the historical single-root behavior.

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
    roots = [RootDescriptor(store=store, label=store.root.name or str(store.root))]
    roots.extend(extra or [])
    handler_cls = _make_handler(roots)
    if not quiet:
        handler_cls._quiet = False  # noqa: SLF001 — class attr by design
    # Bind a loopback LITERAL — never delegate 'localhost' to the OS resolver.
    # A hosts-file/DNS override could map 'localhost' off-loopback while the
    # validation above believed it was loopback, so 'localhost' is purely a
    # CLI alias for 127.0.0.1; users who need IPv6 ask for '::1'.
    bind_host = "127.0.0.1" if host == "localhost" else host
    family = socket.AF_INET6 if ":" in bind_host else socket.AF_INET

    # Per-call subclass instead of mutating the process-global
    # ThreadingHTTPServer.address_family — that had an interleaving race and
    # leaked the family into any other in-process use of the base class.
    class _LoopbackServer(ThreadingHTTPServer):
        address_family = family

    return _LoopbackServer((bind_host, port), handler_cls)


def serve(store: Store, *, host: str = "127.0.0.1", port: int = 8765,
          quiet: bool = True,
          extra: list[RootDescriptor] | None = None,
          on_ready: Callable[[str], None] | None = None) -> None:
    """Start the dashboard and block until interrupted.

    ``port=0`` asks the OS for an ephemeral port. The actual bound
    port is announced via ``on_ready(url)`` and is also available as
    ``server.server_address[1]`` if the caller wraps this manually.
    """
    srv = make_server(store, host, port, quiet=quiet, extra=extra)
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


def serve_in_thread(store: Store, *, host: str = "127.0.0.1", port: int = 0,
                    extra: list[RootDescriptor] | None = None,
                    ) -> tuple[ThreadingHTTPServer, threading.Thread, str]:
    """Start the dashboard on a daemon thread (used by tests).

    Returns ``(server, thread, base_url)``. Caller is responsible for
    ``server.shutdown()`` then ``server.server_close()``. ``base_url``
    has no trailing slash so callers can append ``/messages/<id>``
    etc. directly.
    """
    srv = make_server(store, host, port, extra=extra)
    t = threading.Thread(target=srv.serve_forever, daemon=True,
                         name="agenttalk-web")
    t.start()
    base = _format_url(host, srv.server_address[1]).rstrip("/")
    return srv, t, base


__all__ = [
    "LOOPBACK_HOSTS",
    "STATE_SCHEMA_VERSION",
    "HealthTimelineRing",
    "RootDescriptor",
    "build_attention",
    "build_state",
    "build_thread",
    "make_descriptors",
    "make_server",
    "render_dashboard",
    "render_index",
    "render_message",
    "serve",
    "serve_in_thread",
    "status_payload",
]
