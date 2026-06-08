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
- ``GET  /dashboard``           — obligation dashboard HTML (all roots; 0.17.0)
- ``GET  /static/dashboard.js`` — the dashboard's polling script (0.17.0)
- ``GET  /api/state``           — multi-root obligation aggregate, schema v1 (0.17.0)

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
allows (self-hosted) script + fetch — it renders no message-derived HTML
server-side and its client builds DOM via ``textContent`` only. Routes
that render hostile message bodies (``/messages/<id>``) keep the stricter
no-script policy byte-identical.
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
# /dashboard only: allow the self-hosted polling script and same-origin
# fetch. Still no inline script, no eval, no remote anything.
_DASHBOARD_CSP = ("default-src 'none'; script-src 'self'; "
                  "connect-src 'self'; style-src 'unsafe-inline'; "
                  "img-src 'none'; frame-ancestors 'none'")

# Thread states that owe nothing (mirror threads._derive_next's gate).
_TERMINAL_STATES = ("closed", "closed-superseded")

# /api/state per-root conversation-edge cap (0.19.0, FR-002/003). When more
# than this many distinct (from,to) pairs exist, the list is capped and the
# root carries an additive truncation signal.
_EDGE_LIMIT = 50


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
    """The obligation dashboard shell (0.17.0).

    Deliberately near-empty: ALL dynamic content is rendered client-side
    from ``/api/state`` by ``/static/dashboard.js`` — one renderer, not
    two. Only operator-supplied labels land here, escaped anyway.
    """
    sections = "".join(
        "<section class=\"root\" "
        f"data-root-label=\"{html.escape(d.label, quote=True)}\">"
        f"<h2>{html.escape(d.label)}</h2>"
        "<p class=\"muted\">loading…</p>"
        "</section>"
        for d in roots
    )
    body = (
        "<h1>agenttalk :: obligation dashboard</h1>"
        "<p><a href=\"/\">message log</a></p>"
        # Refresh controls (0.19.0). No inline handlers — dashboard.js wires
        # them with addEventListener, keeping the script-src 'self' CSP intact.
        "<div id=\"controls\">"
        "<button id=\"refresh-btn\" type=\"button\">Refresh</button> "
        "<label><input type=\"checkbox\" id=\"autorefresh\" checked> "
        "auto-refresh</label>"
        "</div>"
        "<noscript><p>This view needs JavaScript (it polls "
        "<code>/api/state</code> every 2 seconds). Poll "
        "<code>GET /api/state</code> directly instead.</p></noscript>"
        f"<div id=\"roots\">{sections}</div>"
        "<script src=\"/static/dashboard.js\"></script>"
    )
    return _html_page("agenttalk :: obligation dashboard", body)


# The dashboard's polling renderer. Served from /static/dashboard.js (a
# server-owned constant — no file on disk, no path interpolation). DOM is
# built exclusively via createElement/textContent: subjects and agent
# names are bus data and must never reach innerHTML (research D11).
# Detail links are created for ROOT[0] rows only — the pre-0.17.0 message
# routes bind to the first root, so a cross-root href would point at the
# wrong store (Codex pre-code finding #1; FR-003).
_DASHBOARD_JS = r"""
'use strict';
(function () {
  var POLL_MS = 2000;
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }
  function fmtAge(s) {
    if (s === undefined || s === null) return '';
    if (s < 90) return Math.round(s) + 's';
    if (s < 5400) return Math.round(s / 60) + 'm';
    return (s / 3600).toFixed(1) + 'h';
  }
  function threadRow(t, rootIndex) {
    var tr = el('tr');
    var subjCell = el('td');
    if (rootIndex === 0 && t.last_msg_id) {
      var aEl = document.createElement('a');
      aEl.href = '/messages/' + encodeURIComponent(t.last_msg_id);
      aEl.textContent = t.subject || t.request_id;
      subjCell.appendChild(aEl);
    } else {
      // non-root[0]: id/subject as PLAIN TEXT — no cross-root links (v1)
      subjCell.appendChild(el('span', null, t.subject || t.request_id));
    }
    tr.appendChild(subjCell);
    tr.appendChild(el('td', null, t.opener_kind || ''));
    tr.appendChild(el('td', null, t.state || ''));
    var owner = t.next_owner;
    if (Object.prototype.toString.call(owner) === '[object Array]') {
      owner = owner.join(', ');
    }
    var next = (owner || '') + (t.next_action ? ' → ' + t.next_action : '');
    tr.appendChild(el('td', null, next));
    var tags = [];
    if (t.mission) tags.push(t.mission + (t.wp_id ? '/' + t.wp_id : ''));
    else if (t.wp_id) tags.push(t.wp_id);
    if (t.epoch_status && t.epoch_status !== 'current') tags.push(t.epoch_status);
    tr.appendChild(el('td', null, tags.join(' · ')));
    tr.appendChild(el('td', 'muted', fmtAge(t.age_seconds)));
    return tr;
  }
  // Role -> layout column (0.19.0, FR-004/005). Convention documented in
  // data-model.md; case-insensitive substring, FIRST match wins in order.
  function classify(a) {
    var role = (a.role || '').toLowerCase();
    if (a.operator_facing === true || role.indexOf('lead') >= 0) return 'top';
    if (role.indexOf('dev') >= 0 || role.indexOf('eng') >= 0 ||
        role.indexOf('impl') >= 0) return 'left';
    if (role.indexOf('review') >= 0 || role.indexOf('qa') >= 0 ||
        role.indexOf('audit') >= 0) return 'right';
    return 'center';
  }
  // owes = open threads in THIS root whose next_owner is this agent (a string,
  // or membership when next_owner is a broadcast pending list). Client-side
  // (the threads array is already on the wire) — no server field.
  function owesCount(root, name) {
    var n = 0, threads = root.threads || [];
    for (var i = 0; i < threads.length; i++) {
      var o = threads[i].next_owner;
      if (o === name) n++;
      else if (Object.prototype.toString.call(o) === '[object Array]' &&
               o.indexOf(name) >= 0) n++;
    }
    return n;
  }
  function agentCard(a, owes) {
    var card = el('div', 'agent-card');
    card.appendChild(el('div', 'agent-name', a.name));
    var meta = [];
    if (a.operator_facing) meta.push('liaison');
    if (a.role) meta.push(a.role);
    if (a.groups && a.groups.length) meta.push('[' + a.groups.join(', ') + ']');
    if (meta.length) card.appendChild(el('div', 'muted', meta.join(' · ')));
    var stats = [];
    if (a.last_seen_age_seconds !== undefined && a.last_seen_age_seconds !== null) {
      stats.push('seen ' + fmtAge(a.last_seen_age_seconds) + ' ago');
    } else {
      stats.push('never seen');
    }
    stats.push('sent ' + (a.sent || 0));
    stats.push('recv ' + (a.received || 0));
    // Always show owes (FR-006): "owes 0" is meaningful — distinct from a
    // card that doesn't expose the field.
    stats.push('owes ' + (owes || 0));
    if (a.unread) stats.push(a.unread + ' unread');
    card.appendChild(el('div', 'stats', stats.join('  ·  ')));
    if (a.composing && a.composing.length) {
      card.appendChild(el('div', 'badge', 'composing… (' + a.composing.length + ')'));
    }
    return card;
  }
  function renderRoot(section, root, rootIndex) {
    while (section.firstChild) section.removeChild(section.firstChild);
    section.appendChild(el('h2', null, root.label));
    section.appendChild(el('p', 'muted', root.path || ''));
    if (root.errors && root.errors.length) {
      section.appendChild(el('p', 'invalid', 'degraded: ' + root.errors.join('; ')));
      return;
    }
    // --- hierarchical roster: liaison/lead on top, role-grouped columns below
    var agents = root.agents || [];
    var top = el('div', 'roster-top');
    var colLeft = el('div', 'col col-left');
    var colCenter = el('div', 'col col-center');
    var colRight = el('div', 'col col-right');
    colLeft.appendChild(el('div', 'col-head', 'developers'));
    colCenter.appendChild(el('div', 'col-head', 'team'));
    colRight.appendChild(el('div', 'col-head', 'reviewers'));
    for (var i = 0; i < agents.length; i++) {
      var a = agents[i];
      var card = agentCard(a, owesCount(root, a.name));
      var col = classify(a);
      if (col === 'top') top.appendChild(card);
      else if (col === 'left') colLeft.appendChild(card);
      else if (col === 'right') colRight.appendChild(card);
      else colCenter.appendChild(card);
    }
    if (top.firstChild) section.appendChild(top);
    var cols = el('div', 'roster-cols');
    cols.appendChild(colLeft);
    cols.appendChild(colCenter);
    cols.appendChild(colRight);
    section.appendChild(cols);
    if (root.retired && root.retired.length) {
      section.appendChild(el('p', 'muted', 'retired: ' + root.retired.join(', ')));
    }
    // --- conversation panel: who talks to whom (edges)
    var edges = root.edges || [];
    section.appendChild(el('h3', null, 'conversations'));
    if (!edges.length) {
      section.appendChild(el('p', 'muted', 'no traffic yet'));
    } else {
      var eul = el('ul', 'edges');
      for (var e = 0; e < edges.length; e++) {
        eul.appendChild(el('li', null,
          edges[e].from + ' → ' + edges[e].to + '  (' + edges[e].count + ')'));
      }
      section.appendChild(eul);
      if (root.edges_truncated) {
        section.appendChild(el('p', 'muted',
          'showing top ' + (root.edge_limit || edges.length) +
          ' pairs (more exist)'));
      }
    }
    // --- open threads (the obligation table, unchanged)
    var threads = root.threads || [];
    section.appendChild(el('h3', null, 'open threads'));
    if (!threads.length) {
      section.appendChild(el('p', 'muted', 'no open threads'));
    } else {
      var table = el('table');
      var head = el('tr');
      var hcols = ['subject', 'kind', 'state', 'next', 'tags', 'age'];
      for (var c = 0; c < hcols.length; c++) head.appendChild(el('th', null, hcols[c]));
      table.appendChild(head);
      for (var r = 0; r < threads.length; r++) {
        table.appendChild(threadRow(threads[r], rootIndex));
      }
      section.appendChild(table);
    }
    var bcs = root.broadcasts || [];
    if (bcs.length) {
      section.appendChild(el('h3', null, 'broadcasts'));
      var bul = el('ul');
      for (var b = 0; b < bcs.length; b++) {
        var bc = bcs[b];
        bul.appendChild(el('li', null,
          (bc.subject || bc.request_id) + ' — pending: ' +
          ((bc.pending || []).join(', ') || 'none')));
      }
      section.appendChild(bul);
    }
    if (root.spec_kitty) {
      section.appendChild(el('p', 'muted',
        'spec-kitty missions: ' + (root.spec_kitty.missions || []).join(', ')));
    }
  }
  function render(state) {
    var container = document.getElementById('roots');
    var sections = container.getElementsByTagName('section');
    var roots = state.roots || [];
    for (var i = 0; i < roots.length && i < sections.length; i++) {
      renderRoot(sections[i], roots[i], i);
    }
  }
  function poll() {
    fetch('/api/state').then(function (resp) { return resp.json(); })
      .then(render)
      .catch(function () { /* transient; retry next tick */ });
  }
  // Refresh controls (0.19.0, FR-008): a manual button forces one poll; the
  // auto-refresh toggle starts/clears the interval. Wired with
  // addEventListener ONLY (no inline handlers — keeps script-src 'self').
  // Toggling never reloads the page; renderRoot updates in place so scroll
  // position is preserved.
  var timer = null;
  function startAuto() { if (!timer) timer = setInterval(poll, POLL_MS); }
  function stopAuto() { if (timer) { clearInterval(timer); timer = null; } }
  var btn = document.getElementById('refresh-btn');
  if (btn) btn.addEventListener('click', poll);
  var chk = document.getElementById('autorefresh');
  if (chk) chk.addEventListener('change', function () {
    if (chk.checked) startAuto(); else stopAuto();
  });
  poll();
  if (!chk || chk.checked) startAuto();
})();
""".strip().encode("utf-8")


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
    for m in msgs_sorted:
        rid = (m.meta or {}).get("request_id")
        if isinstance(rid, str) and rid and rid not in openers:
            openers[rid] = m

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
        if "epoch_at_send" in ometa:
            # forwarded EXACTLY as stored: the 0.16.0 three-state
            # (absent / null / id) must survive serialization.
            d["epoch_at_send"] = ometa["epoch_at_send"]
        d["epoch_status"] = _epoch_status(ometa, current)
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


def _agent_entries(store: Store, cfg: dict, msgs: list[Message],
                   liaison: str | None) -> list[dict]:
    """Per-agent presence rows (data-model §3). Absent-not-null keys."""
    roles = cfg.get("roles", {}) or {}
    groups = cfg.get("groups", {}) or {}
    now = datetime.now(timezone.utc)
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
        out.append(e)
    return out


def _root_state(desc: RootDescriptor) -> dict:
    """One root's full snapshot — or its degraded errors-as-data form.

    A failure ANYWHERE in this root's collection yields
    ``{label, path, errors:[...]}`` with no partial data fields; it must
    never escape (one corrupt root would 500 the whole aggregate,
    violating FR-005).
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
        out["agents"] = _agent_entries(store, cfg, msgs, liaison)
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


def build_state(roots: list[RootDescriptor]) -> dict:
    """The /api/state aggregate (data-model.md, schema v1).

    ``generated_at`` is informational only — message ids remain the
    bus's sole ordering primitive.
    """
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "agenttalk_version": __version__,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "roots": [_root_state(d) for d in roots],
    }


# ------------------------------------------------------------ HTTP handler

def _make_handler(roots: list[RootDescriptor]) -> type[BaseHTTPRequestHandler]:
    """Build a request handler class closed over the watched roots.

    Returns a class (not an instance) because ``ThreadingHTTPServer``
    instantiates it per connection. ``roots[0]`` is the binding target
    for every pre-0.17.0 route (FR-009).
    """
    store = roots[0].store

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
                self._send_json(HTTPStatus.OK, build_state(roots))
                return
            if path == "/dashboard":
                self._send_html(HTTPStatus.OK, render_dashboard(roots),
                                csp=_DASHBOARD_CSP)
                return
            if path == "/static/dashboard.js":
                self._send(HTTPStatus.OK, _DASHBOARD_JS,
                           "application/javascript; charset=utf-8")
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
    "RootDescriptor",
    "build_state",
    "make_descriptors",
    "make_server",
    "render_dashboard",
    "render_index",
    "render_message",
    "serve",
    "serve_in_thread",
    "status_payload",
]
