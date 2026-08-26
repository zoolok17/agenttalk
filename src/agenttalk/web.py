"""Loopback Team Console and action-gated dashboard control surface.

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
3. **Actions off by default.** With ``enable_actions=False`` only
   ``GET``/``HEAD`` are dispatched and POST returns 405 without touching
   disk. When actions are enabled, ordinary dashboard writes append typed
   intents for the drain executor; those intent requests never call
   ``store.send`` directly. ``/api/lead-chat`` is the single authenticated
   in-process direct operator-send path, guarded by reserved-principal checks.
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
- ``GET  /static/<name>``       — allowlisted console assets (css/js/png; 0.58.0/0.61.0)
- ``GET  /api/state``           — multi-root obligation aggregate, schema v1 (0.17.0)
- ``GET  /api/attention``       — ranked "needs a human" queue for a selected root
- ``GET  /api/gates``           — gate & evidence wall: every gate's status/
  severity/evidence/waiver for a selected root
- ``GET  /api/risk-register``   — client-legible relabel of the attention queue,
  sorted by severity/age
- ``GET  /api/ownership``       — full domain-ownership registry (owners/
  reviewers/curators/shared-paths) for a selected root
- ``GET  /api/learning``        — lesson ledger + pointer-only exposure telemetry
- ``GET  /api/onboarding``      — project/codebase onboarding runs + evidence pointers
- ``GET  /api/thread/<rid>``    — one thread's full transcript, CARRIES bodies (0.58.0)
- ``GET  /api/threads``         — paged closed-thread stubs, envelope-only (0.61.0)
- ``GET  /api/session``         — dashboard session metadata (token only when
  actions are enabled)
- ``GET  /api/intents``         — recent dashboard intent state (no bodies)
- ``GET  /api/lead-chat``       — operator<->lead chat view
- ``POST /api/intent``          — append a typed intent when actions are enabled
- ``POST /api/lead-chat``       — authenticated operator lead-chat send/answer

Multi-root (0.17.0)
===================
One server can watch several stores (``agenttalk dashboard --store A
--store B``). Legacy status/message routes remain bound to **root[0]**.
Team Console feeds accept an optional path-derived project ID in ``?root=``;
omission preserves root[0] compatibility, and a unique display label remains a
read-only legacy selector. With multiple roots, actions require exactly one
explicit full project ID and never resolve display labels; single-root actions
may omit ``root`` for compatibility. Unknown, blank, repeated, or ambiguous
selectors fail closed. ``/api/state`` and ``/dashboard`` render all roots, each
namespaced under its own entry — never merged. A corrupt or uninitialized root
degrades to an ``errors`` entry in the payload; it cannot 5xx the aggregate or
affect sibling roots.

CSP note: ``/dashboard`` is the ONLY route whose Content-Security-Policy
allows (self-hosted) script + stylesheet + fetch — it renders no
message-derived HTML server-side and its client builds DOM via
``textContent`` only. The console CSS/JS ship as served files, so the console
CSP drops ``'unsafe-inline'`` entirely (``script-src 'self'; style-src
'self'``). Routes that render hostile message bodies (``/messages/<id>``) and
every JSON feed (``/api/state``, ``/api/attention``, ``/api/gates``,
``/api/risk-register``, ``/api/ownership``, ``/api/learning``,
``/api/onboarding``,
``/api/thread/<rid>``) keep the stricter no-script policy byte-identical.
"""

from __future__ import annotations

import errno
import html
import hmac
import ipaddress
import json
import re
import secrets
import socket
import threading
import time
import urllib.parse
from collections import Counter, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from agenttalk import __version__
from agenttalk import attention as _attention
from agenttalk import avatars as _avatars
from agenttalk import capacity as _capacity
from agenttalk import domains as _domains
from agenttalk import health as _health
from agenttalk import knowledge as _knowledge
from agenttalk import lesson_context as _lesson_context
from agenttalk import onboarding as _onboarding
from agenttalk import signing as _signing
from agenttalk import threads as th
from agenttalk.store import COMPOSING_INTENT_STALE_SECONDS, Message, Store
from agenttalk.threads import Thread, derive_threads


# The only host strings accepted by ``make_server``. No opt-in to
# extend this list — if you need remote access, SSH-tunnel.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

_CLIENT_DISCONNECT_ERRNOS = frozenset(
    code for code in (
        getattr(errno, "EPIPE", None),
        getattr(errno, "ECONNRESET", None),
        getattr(errno, "ECONNABORTED", None),
        getattr(errno, "ESHUTDOWN", None),
    )
    if code is not None
)
_CLIENT_DISCONNECT_WINERRORS = frozenset({10053, 10054})


def _is_client_disconnect(exc: BaseException) -> bool:
    if isinstance(exc, (
        BrokenPipeError,
        ConnectionAbortedError,
        ConnectionResetError,
        socket.timeout,
    )):
        return True
    if not isinstance(exc, OSError):
        return False
    return (
        getattr(exc, "errno", None) in _CLIENT_DISCONNECT_ERRNOS
        or getattr(exc, "winerror", None) in _CLIENT_DISCONNECT_WINERRORS
    )


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
                  "img-src 'self'; frame-ancestors 'none'")
_ACTION_BODY_LIMIT = 64 * 1024
_ACTION_RATE_PER_MINUTE = 60
_ACTION_RATE_BURST = 20
_INTENT_STALE_SECONDS = 120.0
_CLAIM_STALE_SECONDS = 900.0

# Thread states that owe nothing (mirror threads._derive_next's gate).
_TERMINAL_STATES = ("closed", "closed-superseded")

# /api/state per-root conversation-edge cap (0.19.0, FR-002/003). When more
# than this many distinct (from,to) pairs exist, the list is capped and the
# root carries an additive truncation signal.
_EDGE_LIMIT = 50

# /api/state per-root recent-activity feed cap (0.58.0). Envelope-only rows
# (never body) for the live "what's happening" feed on the dashboard.
_RECENT_LIMIT = 25
_THREADS_DEFAULT_LIMIT = 50
_THREADS_MAX_LIMIT = 100
_LEAD_CHAT_LIMIT = 100
_LEARNING_DEFAULT_LIMIT = 100
_LEARNING_MAX_LIMIT = 200
_LEARNING_RECENT_EXPOSURE_LIMIT = 25
_ONBOARDING_DEFAULT_LIMIT = 50
_ONBOARDING_MAX_LIMIT = 100
_ONBOARDING_RECORD_LIMIT = 50
_LEARNING_STATUSES = frozenset({
    "active", "proposed", "review_due", "stale", "retired", "all",
})
_LEARNING_ANCHOR_KEYS = frozenset({
    "kind", "path", "symbol", "request_id", "msg_id", "mission", "wp_id", "sha",
})
_LEARNING_ANCHOR_EVIDENCE_KEYS = frozenset({
    "id", "ids", "ref", "refs", "sha", "sha256", "hash", "hashes",
    "digest", "digests", "line", "lines", "range", "ranges", "path", "symbol",
})
_LEARNING_ANCHOR_EVIDENCE_SUFFIXES = (
    "_id", "_ids", "_ref", "_refs", "_sha", "_sha256", "_hash", "_hashes",
    "_digest", "_digests", "_line", "_lines", "_range", "_ranges",
)
_LEARNING_ANCHOR_EVIDENCE_FORBIDDEN = (
    "body", "prompt", "prompt_block", "output", "stdout", "stderr",
    "transcript", "content", "text",
)

_AVATAR_ASSETS = _avatars.avatar_static_paths()

# Health-timeline ring (0.58.0, §5): a per-(root,agent) window of recent
# health-state samples, kept IN-MEMORY on the server instance only (never a
# file — the read-only invariant). Samples older than this window are pruned;
# contiguous same-state samples collapse into {state, seconds} segments.
_HEALTH_TIMELINE_WINDOW_SECONDS = 30 * 60  # ~last 30 minutes
# Hard per-(root,agent) sample cap (P2-6): a maxlen deque so the stored sequence
# can never grow unboundedly regardless of poll rate. At the ~2s /api/state
# cadence a 30-min window is ~900 samples; this bounds a pathological rate.
_HEALTH_TIMELINE_MAX_SAMPLES = 4096

# Meta keys safe to surface in a thread transcript's `meta_line` (§4b). A
# strict whitelist — arbitrary meta may carry body-ish sender text, which must
# never leak; only these envelope-level decision markers are shown.
_META_LINE_WHITELIST = ("status", "head", "base")


@dataclass(frozen=True)
class RootDescriptor:
    """One store the server watches, plus its display label."""
    store: Store
    label: str


@dataclass
class _RootThreadRows:
    """Shared thread derivation for active /api/state rows and closed history."""
    active: list[dict]
    broadcasts: list[dict]
    terminal: list[dict]


def _stable_root_labels(labels: list[str], project_ids: list[str]) -> list[str]:
    """Make duplicate display labels stable under root-list reordering."""
    out = list(labels)
    groups: dict[str, list[int]] = {}
    for index, label in enumerate(labels):
        groups.setdefault(label.casefold(), []).append(index)
    for indexes in groups.values():
        if len(indexes) == 1:
            continue
        width = 8
        while (
            width < 64
            and len({project_ids[index][:width] for index in indexes}) < len(indexes)
        ):
            width += 1
        for index in indexes:
            out[index] = f"{labels[index]} [{project_ids[index][:width]}]"
    return out


def _dedup_labels(paths: list[Path]) -> list[str]:
    """Directory basenames with stable path-derived duplicate suffixes."""
    stores = [Store(path) for path in paths]
    labels = [store.root.name or str(store.root) for store in stores]
    return _stable_root_labels(labels, [store.project_id() for store in stores])


def _normalize_descriptors(roots: list[RootDescriptor]) -> list[RootDescriptor]:
    """Normalize caller labels and reject duplicate project identities."""
    labels: list[str] = []
    project_ids: list[str] = []
    for desc in roots:
        project_id = desc.store.project_id()
        label = desc.label.strip() or desc.store.root.name or str(desc.store.root)
        match = re.search(r" \[([0-9a-f]{8,64})\]$", label)
        if match and project_id.startswith(match.group(1)):
            label = label[:match.start()]
        labels.append(label)
        project_ids.append(project_id)
    if len(set(project_ids)) != len(project_ids):
        raise ValueError("duplicate dashboard project_id descriptors are unsupported")
    stable = _stable_root_labels(labels, project_ids)
    return [
        RootDescriptor(store=desc.store, label=label)
        for desc, label in zip(roots, stable, strict=True)
    ]


def _root_info(desc: RootDescriptor) -> dict[str, str]:
    return {
        "project_id": desc.store.project_id(),
        "label": desc.label,
        "path": str(desc.store.root),
    }


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


def _server_host_port(handler: BaseHTTPRequestHandler) -> tuple[str, int]:
    addr = handler.server.server_address
    return str(addr[0]), int(addr[1])


def _normalized_host_port(value: str) -> tuple[str, int | None] | None:
    if not value or value.endswith(".") or "@" in value:
        return None
    parsed = urllib.parse.urlsplit("//" + value)
    try:
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if not host or not _is_loopback_addr(host):
        return None
    if ":" in host and not value.startswith("["):
        return None
    return host, port


def _same_host_port(a: tuple[str, int | None] | None,
                    b: tuple[str, int | None] | None,
                    *, default_port: int = 80) -> bool:
    if a is None or b is None:
        return False
    ah, ap = a
    bh, bp = b
    if (ap if ap is not None else default_port) != (bp if bp is not None else default_port):
        return False
    return ah.casefold() == bh.casefold()


def _intent_public_record(rec: dict, *, now: datetime) -> dict:
    created = _parse_iso(rec.get("created_at"))
    claim = rec.get("claim") if isinstance(rec.get("claim"), dict) else {}
    claimed = _parse_iso(claim.get("at"))
    state = str(rec.get("state") or "unknown")
    out = {
        "intent_id": rec.get("intent_id"),
        "kind": rec.get("kind"),
        "state": state,
        "created_at": rec.get("created_at"),
        "claimed_at": claim.get("at"),
        "terminal_at": rec.get("terminal_at"),
        "code": rec.get("code"),
        "error": rec.get("error"),
        "deliveries": [
            {
                "delivery_index": d.get("delivery_index"),
                "state": d.get("state"),
                "recipient": d.get("recipient"),
                "bus_kind": d.get("bus_kind"),
                "message_id": d.get("message_id"),
            }
            for d in rec.get("deliveries", [])
            if isinstance(d, dict)
        ],
    }
    if created is not None and state == Store.INTENT_QUEUED:
        out["queued_age_seconds"] = round((now - created).total_seconds(), 3)
        out["queued_stale"] = out["queued_age_seconds"] > _INTENT_STALE_SECONDS
    if claimed is not None and state == Store.INTENT_CLAIMED:
        out["claimed_age_seconds"] = round((now - claimed).total_seconds(), 3)
        out["claimed_stale"] = out["claimed_age_seconds"] > _CLAIM_STALE_SECONDS
    return out


def build_intents(roots: list[RootDescriptor], *, limit: int = 100,
                  root_index: int = 0) -> dict:
    """Read-only intent status feed. Message bodies are deliberately omitted."""
    now = datetime.now(timezone.utc)
    root = roots[root_index]
    return {
        "root_info": _root_info(root),
        "target_root_index": root_index,
        "target_root_project_id": root.store.project_id(),
        "target_root_label": root.label,
        "target_root_path": str(root.store.root),
        "items": [
            _intent_public_record(rec, now=now)
            for rec in root.store.list_intents(limit=limit)
        ],
    }


def build_preflight(root: RootDescriptor, *, actions_enabled: bool,
                    root_index: int = 0) -> dict:
    """Read-only dashboard bootstrap/preflight checks. No store writes."""
    store = root.store
    initialized = store.initialized()
    checks = []

    def add(key: str, ok: bool, detail: str) -> None:
        checks.append({"key": key, "ok": bool(ok), "detail": detail})

    add("store_initialized", initialized,
        "store exists" if initialized else "run agenttalk start --init-if-absent with --agents")
    if initialized:
        try:
            actor = None
            from agenttalk import intents as intent_mod
            actor = intent_mod.resolve_web_actor(store)
        except Exception:  # noqa: BLE001 - preflight reports degraded data, never 500s
            actor = None
        add("operator_actor", actor is not None,
            f"browser actions will run as {actor}" if actor else
            "set an operator-facing liaison or exactly one role=lead")
    else:
        add("operator_actor", False, "store not initialized")
    sup_ps1 = store.dir / "supervisor.ps1"
    sup_cfg = store.dir / "supervisor.json"
    add("supervisor_scaffolded", sup_ps1.exists() and sup_cfg.exists(),
        "supervisor scaffold found" if sup_ps1.exists() and sup_cfg.exists()
        else "run agenttalk supervise --init, then fill supervisor.json")
    inst = store.read_supervisor_instance()
    add("supervisor_running", inst is not None,
        f"instance pid={inst.get('pid')}" if inst else "no live supervisor instance lock")
    add("actions_enabled", actions_enabled,
        "browser intent enqueueing is enabled" if actions_enabled
        else "restart dashboard with --enable-actions to enqueue intents")
    return {
        "root_info": _root_info(root),
        "target_root_index": root_index,
        "target_root_project_id": store.project_id(),
        "target_root_label": root.label,
        "target_root_path": str(store.root),
        "checks": checks,
        "ok": all(c["ok"] for c in checks),
    }


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
        *((name, "image/png") for name in _AVATAR_ASSETS),
    ):
        try:
            assets[name] = (ctype, (_WEB_STATIC_DIR / name).read_bytes())
        except OSError:
            continue  # not present yet — route 404s until the file lands
    return assets


_STATIC_ASSETS = _load_static_assets()


# ------------------------------------------------------------ data shaping

def _heartbeat_age_seconds(
    heartbeat: datetime | None, *, now_epoch: float,
) -> float | None:
    if heartbeat is None:
        return None
    timestamp = heartbeat.timestamp()
    if timestamp > now_epoch + _health.DEFAULT_HEARTBEAT_SKEW_SECONDS:
        return None
    return max(0.0, now_epoch - timestamp)

def _safe_load_config(store: Store) -> dict:
    try:
        return store.load_config()
    except (OSError, ValueError, FileNotFoundError):
        return {}


def _projection_config(store: Store) -> tuple[dict | None, str | None]:
    """Config boundary for roster-authorized dashboard projections."""
    try:
        cfg = store.load_config()
    except (OSError, ValueError, FileNotFoundError) as exc:
        return None, f"project config is unavailable: {type(exc).__name__}"
    roster = cfg.get("agents")
    if not isinstance(roster, list) or not roster:
        return None, "project config roster is empty"
    return cfg, None


def _all_messages(store: Store, *, cfg: dict | None = None) -> list[Message]:
    """Return every renderable message in the store, most recent first.

    Mirrors the validation surface ``Store.messages_for()`` uses so
    the dashboard's render set matches what ``recv``/``tail`` would
    deliver: schema-passing, roster-valid, known-kind, and (when
    ``signing_enforced()``) HMAC-valid. Anything that fails goes
    through ``store.list_invalid_messages()`` and is surfaced in
    ``/api/status.invalid_messages`` instead of being rendered.
    """
    if cfg is None:
        cfg, config_error = _projection_config(store)
        if config_error is not None or cfg is None:
            return []
    # 0.18.0 (FR-004): validate against the KNOWN roster (active ∪ retired),
    # matching valid_messages / _validated_for_state — otherwise a retired
    # identity's historical messages vanish from /api/messages, /messages/<id>,
    # and the index while the thread panel still shows them. The two surfaces
    # must agree.
    roster = store._known_roster(cfg)  # noqa: SLF001 — D3 parity
    valid, _ = store._scan_messages()  # noqa: SLF001 — same call doctor uses
    if not roster:
        return []
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


def messages_payload(store: Store) -> dict:
    cfg, config_error = _projection_config(store)
    if config_error is not None or cfg is None:
        return {"messages": [], "errors": [config_error or "project config unavailable"]}
    return {
        "messages": [m.to_dict() for m in _all_messages(store, cfg=cfg)],
        "errors": [],
    }


def status_payload(store: Store) -> dict:
    cfg, config_error = _projection_config(store)
    cfg = cfg or {}
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
        "errors": [config_error] if config_error is not None else [],
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
    if not roster:
        raise ValueError("project config roster is empty")
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


def _choose_terminal_thread(
    pairs: list[tuple[str, Thread]], opener: Message | None,
) -> tuple[str, Thread]:
    """Pick one deterministic terminal perspective for an envelope-only stub."""
    if opener is not None:
        for a, t in pairs:
            if a == opener.sender:
                return a, t
    return pairs[0]


def _terminal_thread_row(t: Thread, opener: Message | None,
                         last: Message | None) -> dict:
    peer = None
    opener_name = t.opener_sender
    if opener is not None:
        opener_name = opener.sender
        if isinstance(opener.recipient, str) and opener.recipient and opener.recipient != opener.sender:
            peer = opener.recipient
    elif t.opener_recipient and t.opener_recipient != t.opener_sender:
        peer = t.opener_recipient
    out: dict[str, Any] = {
        "request_id": t.request_id,
        "subject": t.subject,
        "opener_kind": t.opener_kind,
        "opener": opener_name,
        "state": t.state,
        "age_seconds": (round(t.age_seconds, 3)
                        if t.age_seconds is not None else None),
        "last_msg_id": t.last_msg_id,
    }
    if peer:
        out["opener_peer"] = peer
    if last is not None and last.ts:
        out["last_ts"] = last.ts
    if t.needs_operator:
        out["needs_operator"] = True
    if t.operator_state:
        out["operator_state"] = t.operator_state
    return out


def _derive_root_thread_sets(
    store: Store, msgs: list[Message], roster: list[str],
    current: str | None,
) -> _RootThreadRows:
    """Active thread rows, broadcast summaries, and terminal stubs.

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
    last_msgs: dict[str, Message] = {}
    verdicts: dict[str, str] = {}  # rid -> latest decision (0.58.0, §3b)
    for m in msgs_sorted:
        rid = (m.meta or {}).get("request_id")
        if not (isinstance(rid, str) and rid):
            continue
        if rid not in openers:
            openers[rid] = m
        last_msgs[rid] = m
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
    terminal_rows: list[dict] = []
    broadcasts: list[dict] = []
    for rid, pairs in views.items():
        open_pairs = [(a, t) for a, t in pairs
                      if t.state not in _TERMINAL_STATES]
        if not open_pairs:
            _, terminal = _choose_terminal_thread(pairs, openers.get(rid))
            terminal_rows.append(_terminal_thread_row(
                terminal, openers.get(rid), last_msgs.get(rid)))
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
        if opener is not None and th.wrapper_notice_has_canonical_row(
            store, ometa, opener.sender
        ):
            continue
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
    terminal_rows.sort(key=lambda r: r.get("last_msg_id") or "", reverse=True)
    return _RootThreadRows(active=rows, broadcasts=broadcasts,
                           terminal=terminal_rows)


def _derive_root_threads(
    store: Store, msgs: list[Message], roster: list[str],
    current: str | None,
) -> tuple[list[dict], list[dict], int]:
    rows = _derive_root_thread_sets(store, msgs, roster, current)
    return rows.active, rows.broadcasts, len(rows.terminal)


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


def _capacity_number(value: object) -> int | float | None:
    if isinstance(value, bool):
        return None
    return value if isinstance(value, (int, float)) else None


def _capacity_int(value: object) -> int | None:
    num = _capacity_number(value)
    return int(num) if num is not None else None


def _capacity_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _capacity_window(snap: dict, prefix: str, *, label: str,
                     now: datetime) -> dict | None:
    used = _capacity_number(snap.get(f"{prefix}_used_percent"))
    resets_at = _capacity_int(snap.get(f"{prefix}_resets_at"))
    window_minutes = _capacity_int(snap.get(f"{prefix}_window_minutes"))
    if used is None and resets_at is None and window_minutes is None:
        return None
    reset_in = None
    if resets_at is not None:
        reset_in = max(0, int(resets_at - now.timestamp()))
    return {
        "label": label,
        "used_pct": used,
        "resets_at": resets_at,
        "reset_in_seconds": reset_in,
        "window_minutes": window_minutes,
    }


def _capacity_context(snap: dict) -> dict | None:
    used = _capacity_number(snap.get("context_used_percent"))
    tokens = _capacity_int(snap.get("context_tokens"))
    window_size = _capacity_int(snap.get("context_window_size"))
    if used is None and tokens is None and window_size is None:
        return None
    return {"used_pct": used, "tokens": tokens, "window_size": window_size}


def _capacity_entry(snap: dict | None, *, now: datetime) -> dict | None:
    """The `capacity` object (§3a) or None when no snapshot exists. `null`
    percents are allowed INSIDE this object (a snapshot may carry only one
    signal); the absent-not-null rule is about the `capacity` KEY itself."""
    if not isinstance(snap, dict):
        return None
    rate = _capacity_number(snap.get("primary_used_percent"))
    ctx = _capacity_number(snap.get("context_used_percent"))
    out = {
        "rate_used_pct": rate,
        "context_used_pct": ctx,
        "confidence": _map_confidence(_capacity.effective_confidence(snap, now=now)),
    }
    for key in (
        "source", "observed_at", "plan_type", "limit_id",
        "rate_limit_reached_type", "reason",
    ):
        val = _capacity_string(snap.get(key))
        if val is not None:
            out[key] = val
    primary = _capacity_window(snap, "primary", label="5h", now=now)
    if primary is not None:
        out["primary"] = primary
    secondary = _capacity_window(snap, "secondary", label="weekly", now=now)
    if secondary is not None:
        out["secondary"] = secondary
    context = _capacity_context(snap)
    if context is not None:
        out["context"] = context
    return out


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
        self._samples: dict[tuple[str, str], deque[tuple[float, str]]] = {}
        self._lock = threading.Lock()

    def record(self, root_label: str, agent: str, state: str, *, now: float) -> None:
        try:
            key = (root_label, agent)
            with self._lock:
                seq = self._samples.get(key)
                if seq is None:
                    # Hard length bound (P2-6): a maxlen deque can never grow
                    # unboundedly even under a pathological poll rate; the window
                    # cutoff below is the primary prune, this is the safety cap.
                    seq = deque(maxlen=_HEALTH_TIMELINE_MAX_SAMPLES)
                    self._samples[key] = seq
                seq.append((now, state))
                cutoff = now - self._window
                # prune from the front (oldest first) — window cutoff.
                while seq and seq[0][0] < cutoff:
                    seq.popleft()
        except Exception:  # noqa: BLE001, S110 — a ring glitch must never affect the payload  # nosec B110
            pass

    def segments(self, root_label: str, agent: str, *, now: float) -> list[dict]:
        """Contiguous ``{state, seconds}`` segments over the window, oldest→newest.
        Returns [] when no samples (client shows a "building history…" placeholder).

        Applies the window cutoff HERE too (P2-6): a caller may render long after
        the last ``record``, so drop samples older than ``now - window`` before
        building, and clamp the final (open) segment so it never stretches a
        stale sample beyond the window. Total span is capped at the window."""
        try:
            cutoff = now - self._window
            with self._lock:
                seq = [(ts, st) for (ts, st) in self._samples.get((root_label, agent), ())
                       if ts >= cutoff]
            if not seq:
                return []
            segs: list[dict] = []
            for i, (ts, state) in enumerate(seq):
                # Clamp the OPEN (last) segment's end to the window edge so an
                # agent that stopped reporting can't accrue a growing stale span.
                end = seq[i + 1][0] if i + 1 < len(seq) else now
                dur = max(0.0, end - max(ts, cutoff))
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
                   avatar_prefs: dict[str, str] | None = None,
                   history: "HealthTimelineRing | None" = None,
                   root_label: str | None = None,
                   managed_loop: set[str] | None = None) -> list[dict]:
    """Per-agent presence rows (data-model §3). Absent-not-null keys.

    0.58.0 additive fields (all OMITTED when not determinable): ``cli``,
    ``capacity``, ``wrapped``, ``restartable``, ``owned_domains``, ``task``,
    and (best-effort) ``health_timeline``. ``threads_rows`` / ``owned_domains``
    are precomputed ONCE per root by the caller so no per-agent re-scan happens.

    ``managed_loop`` (review P2-1): the set of agents the store reports as
    supervisor-managed / in a lead-loop, computed ONCE per root by the caller.
    An agent in this set is wrapped/restartable even when its health mode is
    unknown (stale/missing snapshot) — that is exactly when the restart signal
    matters most, so health mode must not be the sole arm.
    """
    roles = cfg.get("roles", {}) or {}
    groups = cfg.get("groups", {}) or {}
    threads_rows = threads_rows or []
    owned_domains = owned_domains or {}
    avatar_prefs = avatar_prefs or {}
    managed_loop = managed_loop or set()
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
            heartbeat_age = _heartbeat_age_seconds(hb, now_epoch=now_epoch)
            if heartbeat_age is not None:
                e["last_seen_age_seconds"] = round(heartbeat_age, 3)
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
        avatar = _avatars.resolve_avatar(avatar_prefs, a, roles.get(a), cli)
        if avatar.get("source") != "none":
            e["avatar"] = avatar
        cap = _capacity_entry(snap, now=now)
        if cap is not None:
            e["capacity"] = cap
        # wrapped/restartable arm on EITHER the health mode OR the managed
        # lead-loop set (review P2-1): health mode alone omits the field exactly
        # when a wrapped agent's health goes stale/missing (mode unknown),
        # losing the restart signal when the agent is down. Keep absent-not-null
        # only when NEITHER signal is determinable.
        wrapped = _is_wrapped(health)
        if a in managed_loop:
            wrapped = True
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
        # --- v0.75.0 wrapped-agent runtime (additive, absent-not-null) ---
        # A fail-safe allow-list projection: model / reasoning_effort and a nested
        # `runtime` = {state, reset_reason?}. NO raw session/thread ids (dropped at
        # the store boundary), NO fingerprint. Named `runtime` (NOT `session`) to
        # avoid colliding with /api/session, the Sessions thread view, or the launch
        # epoch. STATE_SCHEMA_VERSION unchanged (purely additive).
        rt = store.read_wrapper_runtime(a)
        if rt is not None:
            if rt.get("model"):
                e["model"] = rt["model"]
            if rt.get("reasoning_effort"):
                e["reasoning_effort"] = rt["reasoning_effort"]
            runtime: dict[str, Any] = {"state": rt.get("session_state", "fresh")}
            if rt.get("reset_reason"):
                runtime["reset_reason"] = rt["reset_reason"]
            e["runtime"] = runtime
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
    project_id = desc.store.project_id()
    try:
        store = desc.store
        cfg = store.load_config()
        roster = cfg.get("agents", []) or []
        if not roster:
            raise ValueError("project config roster is empty")
        avatar_prefs, _avatar_warnings = _avatars.sanitize_avatar_preferences(
            cfg.get("avatars"), roster)
        # ONE disk walk per root per request (D8) — see _validated_for_state.
        msgs, invalid_count = _validated_for_state(store, cfg)
        current = _epoch_from(msgs)
        threads_rows, broadcasts, closed_count = _derive_root_threads(
            store, msgs, roster, current)
        # Domain-owner map: built ONCE per root (not per agent) so the
        # single-scan discipline holds (§3a).
        owned_domains = _owned_domains_map(store, cfg)
        # Managed lead-loop set: computed ONCE per root (P2-1), same source
        # _collect_web_attention_items reads. Feeds the health-independent
        # wrapped/restartable arm. Best-effort — a bad read never blanks a root.
        managed_loop: set[str] = set()
        try:
            for a in store.managed_lead_loop_agents():
                if store.lead_loop_state(a).get("managed"):
                    managed_loop.add(a)
        except Exception:  # noqa: BLE001 — advisory arm; never fail the root
            managed_loop = set()
        out: dict[str, Any] = {
            "label": label,
            "path": path,
            "project_id": project_id,
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
        operator: dict[str, Any] = {
            "principal": _avatars.OPERATOR_PRINCIPAL,
            "label": "you",
            "role_label": "operator",
        }
        operator_avatar = _avatars.resolve_avatar(
            avatar_prefs, _avatars.OPERATOR_PRINCIPAL)
        if operator_avatar.get("source") != "none":
            operator["avatar"] = operator_avatar
        out["operator"] = operator
        out["agents"] = _agent_entries(
            store, cfg, msgs, liaison,
            threads_rows=threads_rows, owned_domains=owned_domains,
            avatar_prefs=avatar_prefs, history=history, root_label=label,
            managed_loop=managed_loop)
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
        return {
            "label": label,
            "path": path,
            "project_id": project_id,
            "errors": [str(e)],
        }


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


def build_threads_index(desc: RootDescriptor, *, state: str = "closed",
                        limit: int = _THREADS_DEFAULT_LIMIT,
                        cursor: str | None = None) -> dict:
    limit = max(1, min(_THREADS_MAX_LIMIT, limit))
    payload: dict[str, Any] = {
        "root": desc.label,
        "root_path": str(desc.store.root),
        "root_info": _root_info(desc),
        "target_root_project_id": desc.store.project_id(),
        "state": state,
        "limit": limit,
        "total_count": 0,
        "next_cursor": None,
        "items": [],
    }
    if state != "closed":
        payload["error"] = "unsupported_state"
        return payload
    try:
        store = desc.store
        cfg = store.load_config()
        roster = cfg.get("agents", []) or []
        msgs, _invalid_count = _validated_for_state(store, cfg)
        rows = _derive_root_thread_sets(store, msgs, roster,
                                        _epoch_from(msgs)).terminal
        payload["total_count"] = len(rows)
        if cursor:
            rows = [r for r in rows if (r.get("last_msg_id") or "") < cursor]
        page = rows[:limit]
        payload["items"] = page
        if len(rows) > limit and page:
            payload["next_cursor"] = page[-1].get("last_msg_id")
        return payload
    except Exception as e:  # noqa: BLE001
        payload["error"] = "threads_unavailable"
        payload["detail"] = _envelope_str(e)
        return payload


# --------------------------------------------------- /api/learning

def _learning_text(value: Any, *, limit: int = 600) -> str:
    """Bound knowledge text for dashboard display.

    Knowledge note bodies are first-class project memory, not bus message
    bodies, so the Learning view may show them. They remain untrusted text and
    the client renders them via textContent.
    """
    s = "" if value is None else str(value).replace("\r", "\n").strip()
    if len(s) > limit:
        s = s[: limit - 3].rstrip() + "..."
    return s


def _learning_short(value: Any, *, limit: int = 160) -> str:
    s = "" if value is None else str(value)
    s = s.replace("\r", " ").replace("\n", " ").strip()
    if len(s) > limit:
        s = s[: limit - 3].rstrip() + "..."
    return s


def _learning_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _lesson_ref(domain_id: str, key: str) -> str:
    return f"{domain_id}/{key}" if domain_id else key


def _learning_anchor_evidence_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, list):
        out = []
        for item in value[:16]:
            if item is None or isinstance(item, (bool, int, float, str)):
                out.append(_learning_anchor_evidence_value(item))
        return out
    if isinstance(value, str):
        return _learning_short(value, limit=160)
    return ""


def _learning_anchor_evidence(raw: Any) -> dict:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for k, v in list(raw.items())[:12]:
        key = _learning_short(k, limit=64)
        folded = key.casefold()
        if not key:
            continue
        if any(token in folded for token in _LEARNING_ANCHOR_EVIDENCE_FORBIDDEN):
            continue
        if (
            folded not in _LEARNING_ANCHOR_EVIDENCE_KEYS
            and not folded.endswith(_LEARNING_ANCHOR_EVIDENCE_SUFFIXES)
        ):
            continue
        value = _learning_anchor_evidence_value(v)
        if value != "":
            out[key] = value
    return out


def _learning_anchor(raw: Any) -> dict:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for key in _LEARNING_ANCHOR_KEYS:
        if raw.get(key) is not None:
            out[key] = _learning_short(raw.get(key), limit=160)
    evidence = _learning_anchor_evidence(raw.get("anchor_evidence"))
    if evidence:
        out["anchor_evidence"] = evidence
    return out


def _exposure_index(events: list[dict]) -> tuple[dict[tuple[str, str, str], dict],
                                                list[dict]]:
    by_lesson: dict[tuple[str, str, str], dict] = {}
    recent: list[dict] = []
    for evt in events:
        event_row = {
            "id": _learning_short(evt.get("id"), limit=96),
            "surface": _learning_short(evt.get("surface"), limit=64),
            "agent": _learning_short(evt.get("agent"), limit=96),
            "message_id": _learning_short(evt.get("message_id"), limit=128),
            "request_id": _learning_short(evt.get("request_id"), limit=128),
            "broadcast_id": _learning_short(evt.get("broadcast_id"), limit=128),
            "correlation_id": _learning_short(evt.get("correlation_id"), limit=128),
            "turn_id": _learning_short(evt.get("turn_id"), limit=96),
            "context_scope": _learning_short(evt.get("context_scope"), limit=32),
            "tags": [_learning_short(t, limit=64) for t in evt.get("tags") or []
                     if isinstance(t, str)],
            "prompt_block_sha256": _learning_short(
                evt.get("prompt_block_sha256"), limit=64),
            "exposed_at": _learning_short(evt.get("exposed_at"), limit=64),
        }
        for lesson in evt.get("lessons") or []:
            domain_id = _learning_short(lesson.get("domain_id"), limit=128)
            key = _learning_short(lesson.get("key"), limit=128)
            fingerprint = _learning_short(
                lesson.get("lesson_fingerprint"), limit=64)
            if not key:
                continue
            agg = by_lesson.setdefault((domain_id, key, fingerprint), {
                "count": 0,
                "agents": {},
                "last_exposed_at": "",
                "last_agent": "",
                "last_request_id": "",
                "last_message_id": "",
                "last_context_scope": "",
            })
            agg["count"] += 1
            agent = event_row["agent"]
            if agent:
                agg["agents"][agent] = agg["agents"].get(agent, 0) + 1
            if event_row["exposed_at"] >= str(agg.get("last_exposed_at") or ""):
                agg["last_exposed_at"] = event_row["exposed_at"]
                agg["last_agent"] = agent
                agg["last_request_id"] = event_row["request_id"]
                agg["last_message_id"] = event_row["message_id"]
                agg["last_context_scope"] = event_row["context_scope"]
            recent_row = dict(event_row)
            recent_row.update({
                "domain_id": domain_id,
                "key": key,
                "note_id": _learning_short(lesson.get("note_id"), limit=96),
                "marker": _learning_short(lesson.get("marker"), limit=160),
                "evidence_ref": _learning_short(lesson.get("evidence_ref"), limit=160),
                "lesson_fingerprint": fingerprint,
            })
            recent.append(recent_row)
    for agg in by_lesson.values():
        agents = agg.pop("agents")
        agg["agents"] = [
            {"agent": name, "count": count}
            for name, count in sorted(agents.items(), key=lambda kv: (-kv[1], kv[0]))
        ]
    recent.sort(key=lambda r: r.get("exposed_at") or "", reverse=True)
    return by_lesson, recent[:_LEARNING_RECENT_EXPOSURE_LIMIT]


def _lesson_learning_item(note: dict, verdict: dict,
                          exposure: dict | None) -> dict:
    lesson = note.get("lesson") or {}
    authority = note.get("authority") if isinstance(note.get("authority"), dict) else {}
    exposure = exposure or {
        "count": 0,
        "agents": [],
        "last_exposed_at": "",
        "last_agent": "",
        "last_request_id": "",
        "last_message_id": "",
        "last_context_scope": "",
    }
    domain_id = _learning_short(note.get("domain_id"), limit=128)
    key = _learning_short(note.get("key"), limit=128)
    return {
        "ref": _lesson_ref(domain_id, key),
        "domain_id": domain_id,
        "key": key,
        "note_id": _learning_short(note.get("id"), limit=96),
        "scope": _learning_short(lesson.get("scope"), limit=32),
        "status": _learning_short(lesson.get("status"), limit=32),
        "lesson_status": _learning_short(lesson.get("status"), limit=32),
        "active": bool(verdict.get("active")),
        "marker": _lesson_context.lesson_marker(verdict),
        "trigger": _learning_short(lesson.get("trigger"), limit=240),
        "body": _learning_text(note.get("body")),
        "lesson_fingerprint": _lesson_context.lesson_fingerprint(note),
        "author": _learning_short(note.get("author"), limit=96),
        "owner": _learning_short(lesson.get("owner"), limit=96),
        "curated_by": _learning_short(note.get("curated_by"), limit=96),
        "curator": _learning_short(lesson.get("curator") or note.get("curated_by"), limit=96),
        "authority_state": _learning_short(authority.get("state"), limit=64),
        "authority_from": _learning_short(authority.get("resolved_from"), limit=64),
        "verified_against_sha": _learning_short(note.get("verified_against_sha"), limit=40),
        "domain_registry_hash": _learning_short(note.get("domain_registry_hash"), limit=96),
        "domain_definition_hash": _learning_short(
            note.get("domain_definition_hash"), limit=96),
        "evidence_ref": _learning_short(lesson.get("evidence_ref"), limit=240),
        "anchor": _learning_anchor(lesson.get("anchor") or note.get("anchor")),
        "supersedes": [_learning_short(t, limit=128) for t in lesson.get("supersedes") or []
                       if isinstance(t, str)],
        "supersedes_key": _learning_short(note.get("supersedes_key"), limit=128),
        "supersedes_id": _learning_short(note.get("supersedes_id"), limit=96),
        "applies_to": [_learning_short(t, limit=64) for t in lesson.get("applies_to") or []
                       if isinstance(t, str)],
        "created_at": _learning_short(note.get("created_at"), limit=64),
        "updated_at": _learning_short(note.get("updated_at"), limit=64),
        "curated_at": _learning_short(note.get("curated_at"), limit=64),
        "review_after": _learning_short(verdict.get("review_after"), limit=64),
        "expires_at": _learning_short(verdict.get("expires_at"), limit=64),
        "review_due": bool(verdict.get("review_due")),
        "hard_stale": bool(verdict.get("hard_stale")),
        "caution_flags": list(verdict.get("caution_flags") or []),
        "stale_reasons": list(verdict.get("stale_reasons") or []),
        "exposure": exposure,
    }


def _learning_rows(events: list[dict], *, scope: str | None,
                   tags: list[str], now: str | None,
                   domains: dict[str, Any] | None = None,
                   registry_hash: str | None = None) -> list[tuple[dict, dict]]:
    rows: dict[str, tuple[dict, dict]] = {}
    for group in (
        _lesson_context.lesson_rows(
            events, scope=scope, tags=tags, now=now, active_only=True,
            domains=domains, registry_hash=registry_hash),
        _lesson_context.lesson_rows(
            events, scope=scope, tags=tags, now=now,
            include_uncurated=True, include_stale=True,
            domains=domains, registry_hash=registry_hash),
        _lesson_context.lesson_rows(
            events, scope=scope, tags=tags, now=now,
            include_stale=True, domains=domains, registry_hash=registry_hash),
    ):
        for note, verdict in group:
            note_id = str(note.get("id") or f"{note.get('domain_id')}:{note.get('key')}")
            rows[note_id] = (note, verdict)
    return list(rows.values())


def _learning_exposure_for_item(item: dict,
                                exposure_by_lesson: dict[tuple[str, str, str], dict]) -> dict | None:
    domain_id = str(item.get("domain_id") or "")
    key = str(item.get("key") or "")
    fingerprint = str(item.get("lesson_fingerprint") or "")
    return (
        exposure_by_lesson.get((domain_id, key, fingerprint))
        or exposure_by_lesson.get((domain_id, key, ""))
    )


def _learning_item_matches_status(item: dict, status: str) -> bool:
    if status == "all":
        return True
    if status == "active":
        return bool(item.get("active")) and item.get("status") == _knowledge.LESSON_STATUS_ACCEPTED
    if status == "review_due":
        return bool(item.get("active")) and bool(item.get("review_due"))
    if status == "proposed":
        return item.get("status") == _knowledge.LESSON_STATUS_PROPOSED
    if status == "retired":
        return item.get("status") == _knowledge.LESSON_STATUS_RETIRED
    if status == "stale":
        return bool(item.get("hard_stale")) or (
            item.get("status") == _knowledge.LESSON_STATUS_ACCEPTED
            and not item.get("active")
        )
    return False


def _learning_sort_key(item: dict) -> tuple:
    status_order = {
        _knowledge.LESSON_STATUS_ACCEPTED: 0,
        _knowledge.LESSON_STATUS_PROPOSED: 1,
        _knowledge.LESSON_STATUS_RETIRED: 2,
    }.get(item.get("status"), 3)
    return (
        0 if item.get("active") else 1,
        0 if item.get("review_due") else 1,
        status_order,
        item.get("scope") or "",
        item.get("key") or "",
    )


def build_learning(desc: RootDescriptor, *, status: str = "active",
                   scope: str | None = None, tags: list[str] | None = None,
                   limit: int = _LEARNING_DEFAULT_LIMIT,
                   now: str | None = None) -> dict:
    """Dashboard learning ledger: lessons plus pointer-only exposure telemetry.

    Exposure telemetry proves the wrapper surfaced a lesson in a turn. It does
    not prove the model read, understood, or applied the lesson.
    """
    status = status if status in _LEARNING_STATUSES else "active"
    tags = list(tags or [])
    limit = max(1, min(_LEARNING_MAX_LIMIT, int(limit)))
    payload: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": _learning_now(),
        "root": desc.label,
        "root_path": str(desc.store.root),
        "root_info": _root_info(desc),
        "target_root_project_id": desc.store.project_id(),
        "filters": {
            "status": status,
            "scope": scope or "",
            "tags": tags,
            "limit": limit,
        },
        "items": [],
        "lessons": [],
        "recent_exposures": [],
        "counts": {
            "total": 0,
            "showing": 0,
            "active": 0,
            "proposed": 0,
            "accepted": 0,
            "retired": 0,
            "review_due": 0,
            "stale": 0,
            "expired": 0,
            "superseded": 0,
            "exposures": 0,
            "invalid_notes": 0,
            "invalid_exposures": 0,
            "truncated": 0,
        },
        "problems": {
            "knowledge": [],
            "exposures": [],
        },
        "note": "Exposure means surfaced to an agent turn, not proven application.",
    }
    try:
        events, knowledge_problems = _knowledge.read_events(desc.store)
        _views, semantic_problems = _knowledge.resolve_views_with_problems(events)
        knowledge_problems = [*knowledge_problems, *semantic_problems]
        registry = _domains.load_registry(
            desc.store.dir / _domains.FILENAME, desc.store.load_config())
        exposures, exposure_problems = _lesson_context.read_exposure_events(desc.store)
        exposure_by_lesson, recent = _exposure_index(exposures)
        rows = _learning_rows(
            events, scope=scope, tags=tags, now=now,
            domains=registry.data.get("domains") or {},
            registry_hash=registry.registry_hash)
        items = []
        for note, verdict in rows:
            item = _lesson_learning_item(note, verdict, None)
            item["exposure"] = _learning_exposure_for_item(item, exposure_by_lesson) \
                or item["exposure"]
            items.append(item)
        items.sort(key=_learning_sort_key)
        counts = payload["counts"]
        counts["total"] = len(items)
        counts["active"] = sum(1 for it in items if it.get("active"))
        counts["review_due"] = sum(1 for it in items if it.get("review_due"))
        counts["stale"] = sum(1 for it in items if it.get("hard_stale"))
        counts["expired"] = sum(1 for it in items
                                if "expired" in (it.get("stale_reasons") or []))
        counts["superseded"] = sum(1 for it in items
                                   if "superseded" in (it.get("stale_reasons") or []))
        for it in items:
            status = it.get("status")
            if status in ("proposed", "accepted", "retired"):
                counts[status] += 1
        counts["exposures"] = len(exposures)
        counts["invalid_notes"] = len(knowledge_problems)
        counts["invalid_exposures"] = len(exposure_problems)
        page = [it for it in items if _learning_item_matches_status(it, payload["filters"]["status"])]
        counts["showing"] = min(len(page), limit)
        counts["truncated"] = max(0, len(page) - limit)
        page = page[:limit]
        payload["items"] = page
        payload["lessons"] = page
        payload["recent_exposures"] = recent
        payload["problems"] = {
            "knowledge": knowledge_problems[:20],
            "exposures": exposure_problems[:20],
        }
        return payload
    except Exception as e:  # noqa: BLE001
        payload["error"] = "learning_unavailable"
        payload["detail"] = _envelope_str(e)
        return payload


# --------------------------------------------------- /api/onboarding

def _onboarding_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _onboarding_short(value: Any, *, limit: int = 180) -> str:
    s = "" if value is None else str(value)
    s = s.replace("\r", " ").replace("\n", " ").strip()
    if len(s) > limit:
        s = s[: limit - 3].rstrip() + "..."
    return s


def _onboarding_record(row: dict) -> dict:
    return {
        "kind": _onboarding_short(row.get("kind"), limit=32),
        "key": _onboarding_short(row.get("key"), limit=128),
        "status": _onboarding_short(row.get("status"), limit=64),
        "summary": _onboarding_short(row.get("summary"), limit=600),
        "segment": _onboarding_short(row.get("segment"), limit=128),
        "actor": _onboarding_short(row.get("actor"), limit=96),
        "owner": _onboarding_short(row.get("owner"), limit=96),
        "checkers": [
            _onboarding_short(v, limit=96)
            for v in (row.get("checkers") or [])[:16]
            if isinstance(v, str)
        ],
        "refs": [
            _onboarding_short(v, limit=200)
            for v in (row.get("refs") or [])[:16]
            if isinstance(v, str)
        ],
        "paths": [
            _onboarding_short(v, limit=200)
            for v in (row.get("paths") or [])[:32]
            if isinstance(v, str)
        ],
        "source": _onboarding_short(row.get("source"), limit=32),
        "confidence": _onboarding_short(row.get("confidence"), limit=32),
        "blocking": bool(row.get("blocking")),
        "updated_at": _onboarding_short(row.get("updated_at"), limit=64),
    }


def _onboarding_records(records: dict) -> tuple[dict, dict]:
    out: dict[str, list[dict]] = {}
    trunc: dict[str, int] = {}
    for kind in sorted(_onboarding.ITEM_KINDS):
        rows = list(records.get(kind) or [])
        out[kind] = [_onboarding_record(r) for r in rows[:_ONBOARDING_RECORD_LIMIT]]
        trunc[kind] = max(0, len(rows) - _ONBOARDING_RECORD_LIMIT)
    return out, trunc


def _onboarding_run_item(run: dict) -> dict:
    records, truncated = _onboarding_records(run.get("records") or {})
    return {
        "id": _onboarding_short(run.get("id"), limit=96),
        "run_id": _onboarding_short(run.get("run_id") or run.get("id"), limit=96),
        "title": _onboarding_short(run.get("title"), limit=240),
        "objective": _onboarding_short(run.get("objective"), limit=800),
        "base_ref": _onboarding_short(run.get("base_ref"), limit=200),
        "lead": _onboarding_short(run.get("lead"), limit=96),
        "state": _onboarding_short(run.get("state"), limit=64),
        "state_summary": _onboarding_short(run.get("state_summary"), limit=600),
        "created_at": _onboarding_short(run.get("created_at"), limit=64),
        "updated_at": _onboarding_short(run.get("updated_at"), limit=64),
        "active": bool(run.get("active")),
        "blocked": bool(run.get("blocked")),
        "counts": dict(run.get("counts") or {}),
        "records": records,
        "record_truncated": truncated,
        "problems": [
            {
                "line": p.get("line"),
                "error": _onboarding_short(p.get("error"), limit=240),
            }
            for p in (run.get("problems") or [])[:20]
            if isinstance(p, dict)
        ],
    }


def build_onboarding(desc: RootDescriptor, *,
                     limit: int = _ONBOARDING_DEFAULT_LIMIT) -> dict:
    """Dashboard onboarding ledger: project-analysis runs and evidence pointers."""
    limit = max(1, min(_ONBOARDING_MAX_LIMIT, int(limit)))
    payload: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": _onboarding_now(),
        "root": desc.label,
        "root_path": str(desc.store.root),
        "root_info": _root_info(desc),
        "target_root_project_id": desc.store.project_id(),
        "filters": {"limit": limit},
        "runs": [],
        "counts": {
            "total": 0,
            "showing": 0,
            "active": 0,
            "blocked": 0,
            "segments": 0,
            "accepted_segments": 0,
            "claims": 0,
            "confirmed_claims": 0,
            "conflicted_claims": 0,
            "needs_human_claims": 0,
            "open_drift": 0,
            "open_unknowns": 0,
            "blocking_unknowns": 0,
            "blocking_records": 0,
            "human_needed": 0,
            "invalid_lines": 0,
            "truncated": 0,
        },
        "problems": [],
        "note": "Onboarding records pointer evidence for codebase understanding; it is not an enforcement boundary.",
    }
    try:
        listed = _onboarding.list_runs(desc.store, limit=limit)
        runs = [_onboarding_run_item(r) for r in listed.get("runs") or []]
        counts = payload["counts"]
        counts["total"] = int(listed.get("total") or len(runs))
        counts["showing"] = len(runs)
        counts["active"] = sum(1 for r in runs if r.get("active"))
        counts["blocked"] = sum(1 for r in runs if r.get("blocked"))
        counts["truncated"] = int(listed.get("truncated") or 0)
        for run in runs:
            c = run.get("counts") or {}
            for key in (
                "segments", "accepted_segments", "claims", "confirmed_claims",
                "conflicted_claims", "needs_human_claims", "open_drift",
                "open_unknowns", "blocking_unknowns", "blocking_records",
                "human_needed",
            ):
                counts[key] += int(c.get(key) or 0)
            counts["invalid_lines"] += len(run.get("problems") or [])
        payload["runs"] = runs
        payload["problems"] = [
            {
                "run_id": _onboarding_short(p.get("run_id"), limit=96),
                "problems": [
                    {
                        "line": row.get("line"),
                        "error": _onboarding_short(row.get("error"), limit=240),
                    }
                    for row in (p.get("problems") or [])[:10]
                    if isinstance(row, dict)
                ],
            }
            for p in (listed.get("problems") or [])[:20]
            if isinstance(p, dict)
        ]
        return payload
    except Exception as e:  # noqa: BLE001
        payload["error"] = "onboarding_unavailable"
        payload["detail"] = _envelope_str(e)
        return payload


# --------------------------------------------------- /api/attention (§4a)
#
# The ranked "needs a human" queue for one selected root. Composed from the PURE
# attention.build_queue (escalations / gate HOLD / dead-letter / lead-unarmed /
# capacity / config-blocked / close HOLD) PLUS a derived STUCK item per agent
# whose advisory health.state == "stuck_suspected" (stuck is NOT a build_queue
# source). READ-ONLY: it lists items; it never disposes them (no writes in v1).
# web.py does NOT import the CLI layer, so the source collection mirrors
# cli._collect_attention_items here, each source INDEPENDENTLY fail-safe.

# Internal attention source -> the design's coarse wire source + label + severity.
_ATTENTION_SOURCE_MAP: dict[str, tuple[str, str, str]] = {
    _attention.SOURCE_NEEDS_OPERATOR: ("escalation", "ESCALATION", "high"),
    _attention.SOURCE_PROCESS_TREE_HOLD: ("supervisor", "SUPERVISOR HOLD", "high"),
    _attention.SOURCE_CONFIG_BLOCKED: ("gate", "GATE HOLD", "high"),
    _attention.SOURCE_GATE_HOLD: ("gate", "GATE HOLD", "high"),
    _attention.SOURCE_CLOSE_HOLD: ("gate", "GATE HOLD", "high"),
    _attention.SOURCE_DEAD_LETTER: ("deadletter", "DEAD LETTER", "med"),
    _attention.SOURCE_COORDINATION_STALL: (
        "coordination_stall", "TEAM STALL", "high"),
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
        from agenttalk import supervisor as _supervisor

        state = _supervisor.load_supervisor_state(
            store.dir / "supervisor-state.json"
        )
        try:
            supervisor_config = _supervisor.load_supervisor_config(
                store.dir / "supervisor.json"
            )
        except Exception:  # noqa: BLE001 - the HOLD must survive bad config
            supervisor_config = None
        try:
            store_config = store.load_config()
        except Exception:  # noqa: BLE001 - fail closed without blanking the HOLD
            store_config = None
        restart_requests: dict[str, dict] = {}
        for name in A.configured_process_tree_hold_agents(state):
            try:
                marker = store.read_restart_request(name)
            except Exception:  # noqa: BLE001 - optional context, not the signal
                marker = None
            if isinstance(marker, dict):
                restart_requests[name] = marker
        reset_admissions = _supervisor.evaluate_process_tree_reset_admissions(
            store,
            state,
            actor=for_agent,
        )
        launch_requests = _supervisor.active_ephemeral_launch_markers(
            store,
            state,
        )
        launch_deliveries = _supervisor.active_ephemeral_one_shot_deliveries(
            store,
            state,
            launch_requests,
        )
        lane_workspaces = _supervisor.active_ephemeral_lane_workspaces(store)
        items += A.process_tree_hold_items(
            state,
            supervisor_config=supervisor_config,
            store_config=store_config,
            root=store.root,
            restart_requests=restart_requests,
            launch_requests=launch_requests,
            launch_deliveries=launch_deliveries,
            lane_workspaces=lane_workspaces,
            reset_admissions=reset_admissions,
            now_epoch=time.time(),
        )
    except Exception as e:  # noqa: BLE001
        items.append(A.source_error_item("process_tree_hold", str(e)))
    try:
        items += A.dead_letter_items(store.list_dead_letters())
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
    try:
        from agenttalk import coordination_stall as _coordination_stall

        snapshot = _coordination_stall.build_snapshot(store)
        items += A.coordination_stall_items(snapshot.get("items") or [])
    except Exception as e:  # noqa: BLE001
        items.append(A.source_error_item("coordination_stall", str(e)))
    return items


def _web_needs_operator(store: Store, for_agent: str) -> list[dict]:
    """Pending needs_operator escalations from ``for_agent``'s thread view, each
    as {request_id, subject, sender, age_seconds, meta, prompt_excerpt}. Parity
    with the CLI's ``_needs_operator_items``: an escalation is an opener carrying
    ``meta.needs_operator == "true"`` whose derived thread is still
    ``operator_state == "pending"``. Envelope-first: opener meta feeds the
    fail-safe parser, while ``prompt_excerpt`` is a bounded body excerpt used
    only by action-enabled Attention cards."""
    now = datetime.now(timezone.utc)
    cfg = store.load_config()
    msgs, _ = _validated_for_state(store, cfg)
    msgs_sorted = sorted(msgs, key=lambda m: m.id)
    opener_meta: dict[str, dict] = {}
    opener_sender: dict[str, str] = {}
    opener_prompt: dict[str, str] = {}
    for m in msgs_sorted:
        rid = (m.meta or {}).get("request_id")
        if rid and (m.meta or {}).get("needs_operator") == "true" and rid not in opener_meta:
            opener_meta[rid] = m.meta or {}
            opener_sender[rid] = m.sender
            opener_prompt[rid] = _attention_prompt_excerpt(m.body or "")
    retired = set(store.retired_agents())
    rows = derive_threads(msgs_sorted, agent=for_agent,
                          cursor=store.cursor(for_agent) or "", now=now,
                          closed_rids=_closed_rids_for(store, for_agent),
                          retired=retired)
    pending = [{"request_id": t.request_id, "subject": t.subject,
                "sender": opener_sender.get(t.request_id, ""),
                "age_seconds": t.age_seconds, "meta": opener_meta.get(t.request_id, {}),
                "prompt_excerpt": opener_prompt.get(t.request_id, "")}
               for t in rows
               if t.needs_operator and t.operator_state == "pending"
               and t.opener_recipient == for_agent and t.opener_sender != for_agent]
    return [p for p in pending
            if not th.wrapper_notice_has_canonical_row(store, p["meta"], p["sender"])]


_HEALTH_REASON_DETAILS = {
    "worktree_branch_already_checked_out": (
        "STALLED",
        "looks stalled",
        "branch already checked out in another worktree; use the existing worktree, "
        "remove the stale one, or assign a unique branch",
    ),
}


def _derive_stuck_items(agents: list[dict], *, now: datetime) -> list[dict]:
    """One STUCK attention item per agent whose advisory health.state ==
    ``stuck_suspected`` (§4a — NOT a build_queue source). Envelope-derived; the
    detail line names the agent, never any body."""
    stuck: list[dict] = []
    for a in agents:
        health = a.get("health") or {}
        reason = str(health.get("reason_code") or "")
        descriptor = _HEALTH_REASON_DETAILS.get(reason)
        if health.get("state") == "stuck_suspected":
            source_label = "STUCK"
            title_suffix = "looks stuck"
            detail = "no forward progress on this agent's turn (advisory health)"
        elif descriptor is not None:
            source_label, title_suffix, detail = descriptor
        else:
            continue
        name = a.get("name")
        age = a.get("last_seen_age_seconds")
        stuck.append({
            "id": f"stuck:{name}",
            "source": "stuck",
            "source_label": source_label,
            "severity": "med",
            "title": f"{name} {title_suffix}",
            "agent": name,
            "detail": detail,
            "age_seconds": float(age) if isinstance(age, (int, float)) else 0.0,
            "human_can_unblock_now": True,
        })
    return stuck


def _answer_action_for_item(item: dict, for_agent: str | None) -> dict | None:
    if not for_agent or item.get("source") != _attention.SOURCE_NEEDS_OPERATOR:
        return None
    rid = ""
    for ref in item.get("source_refs") or []:
        if isinstance(ref, dict) and ref.get("kind") == "message":
            maybe_rid = ref.get("request_id")
            if isinstance(maybe_rid, str):
                rid = maybe_rid
                break
    if not rid:
        iid = str(item.get("item_id") or "")
        prefix = f"{_attention.SOURCE_NEEDS_OPERATOR}:"
        if iid.startswith(prefix):
            rid = iid[len(prefix):]
    if not rid:
        return None
    return {
        "kind": "answer_escalation",
        "to_request": rid,
        "requester": _envelope_str(item.get("requester") or ""),
    }


def build_attention(desc: RootDescriptor,
                    agents: list[dict] | None = None,
                    *,
                    actions_enabled: bool = False) -> dict:
    """The /api/attention payload for one root (§4a). Envelope-only: every
    string here is envelope-derived — a raw message body NEVER lands in a
    field. ``agents`` (the selected root's /api/state rows) supplies the derived
    STUCK items; when omitted they are computed from a fresh scan.

    FAIL-SAFE (parity with /api/state's errors-as-data, review B1): a corrupt or
    uninitialized root must NOT 500 this JSON route. ANY exception raised while
    building the payload — including the ``operator_facing()``/``sole_lead()``
    config reads that back the needs_operator source — degrades to a 200 body
    ``{"root", "items": [], "count": 0, "errors": ["<str(e)>"]}``.
    """
    store = desc.store
    now = datetime.now(timezone.utc)
    try:
        cfg = _safe_load_config(store)
        roster = cfg.get("agents", []) or []
        try:
            for_agent = store.operator_facing() or store.sole_lead()
        except Exception:  # noqa: BLE001 — a corrupt root can't resolve a liaison
            for_agent = None  # needs_operator source skips cleanly when None
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
            entry = {
                "id": it.get("item_id", ""),
                "source": wire_source,
                "source_label": source_label,
                "severity": severity,
                "title": _envelope_str(title),
                "agent": _attention_agent(it),
                "detail": _envelope_str(detail),
                "age_seconds": float(it.get("age_seconds") or 0),
                "human_can_unblock_now": bool(it.get("human_can_unblock_now")),
            }
            if (
                src == _attention.SOURCE_PROCESS_TREE_HOLD
                and it.get("recommendation")
            ):
                # Unlike answerable escalations, this HOLD has no mutation
                # action. The remediation must therefore travel on the ordinary
                # card even when dashboard actions are disabled.
                entry["recommendation"] = _envelope_str(it.get("recommendation"))
                if it.get("operator_command"):
                    entry["operator_command"] = _operator_command_str(
                        it.get("operator_command")
                    )
                operator_argv = it.get("operator_argv")
                if (
                    isinstance(operator_argv, list)
                    and len(operator_argv) <= 32
                    and all(
                        isinstance(token, str) and len(token) <= 500
                        for token in operator_argv
                    )
                ):
                    entry["operator_argv"] = list(operator_argv)
                launch = it.get("configured_launch")
                if isinstance(launch, dict):
                    argv = launch.get("argv")
                    cwd = launch.get("cwd")
                    if (
                        isinstance(argv, list)
                        and all(isinstance(token, str) for token in argv)
                        and isinstance(cwd, str)
                    ):
                        entry["configured_launch"] = {
                            "source": "supervisor.json",
                            "mode": "detached",
                            "argv": list(argv),
                            "cwd": cwd,
                        }
                        environment = launch.get("environment")
                        if isinstance(environment, dict):
                            entry["configured_launch"]["environment"] = {
                                str(key): value
                                for key, value in environment.items()
                                if isinstance(key, str)
                                and (
                                    value is None
                                    or isinstance(value, str)
                                    or (
                                        isinstance(value, list)
                                        and all(isinstance(v, str) for v in value)
                                    )
                                )
                            }
                        note = launch.get("environment_note")
                        if isinstance(note, str):
                            entry["configured_launch"]["environment_note"] = (
                                _envelope_str(note)
                            )
                else:
                    launch_problem = it.get("configured_launch_unavailable")
                    if isinstance(launch_problem, str):
                        launch_problem = _envelope_str(launch_problem)
                        if launch_problem:
                            entry["configured_launch_unavailable"] = launch_problem
                restart_request = it.get("restart_request")
                if isinstance(restart_request, dict):
                    entry["restart_request"] = dict(restart_request)
            if actions_enabled:
                action = _answer_action_for_item(it, for_agent)
                if action is not None:
                    entry["answerable"] = True
                    entry["answer_escalation"] = {
                        "to_request": action["to_request"],
                        "requester": action["requester"],
                    }
                    entry["actions"] = {"answer_escalation": dict(action)}
                    entry["available_actions"] = [dict(action)]
                    if it.get("priority") not in (None, "unknown"):
                        entry["priority"] = _envelope_str(it.get("priority"))
                    if it.get("risk_severity") not in (None, "unknown"):
                        entry["risk_severity"] = _envelope_str(it.get("risk_severity"))
                    if it.get("recommendation"):
                        entry["recommendation"] = _envelope_str(it.get("recommendation"))
                    if isinstance(it.get("options"), list):
                        entry["options"] = [
                            _envelope_str(o) for o in it["options"] if isinstance(o, str)
                        ]
                    prompt = _attention_prompt_excerpt(it.get("prompt_excerpt"))
                    if prompt:
                        entry["prompt_excerpt"] = prompt
            wire.append(entry)
        if agents is None:
            try:
                agents = _agent_entries(store, cfg,
                                        _validated_for_state(store, cfg)[0],
                                        for_agent)
            except Exception:  # noqa: BLE001 — stuck items are best-effort
                agents = []
        wire.extend(_derive_stuck_items(agents, now=now))
        return {
            "root": desc.label,
            "root_path": str(store.root),
            "root_info": _root_info(desc),
            "target_root_project_id": store.project_id(),
            "items": wire,
            "count": len(wire),
        }
    except Exception as e:  # noqa: BLE001 — errors-as-data, never a 500 (B1)
        return {
            "root": desc.label,
            "root_path": str(store.root),
            "root_info": _root_info(desc),
            "target_root_project_id": store.project_id(),
            "items": [],
            "count": 0,
            "errors": [str(e)],
        }


# NEEDS_OPERATOR titles/details are typed single-line fields the attention layer
# already caps; but as defense-in-depth for §4a's "NEVER raw message body"
# contract we bound every surfaced string to one line and a hard length so no
# multi-paragraph prose can ride a field. Envelope summaries are short by design.
_ENVELOPE_MAX = 300
_ATTENTION_PROMPT_MAX = 1200
_OPERATOR_COMMAND_MAX = 1000


def _envelope_str(value: Any) -> str:
    """Coerce an envelope-derived value to a short, single-line summary string.
    Strips newlines and truncates — belt-and-braces so no body-ish prose leaks
    through a field that is contractually envelope-only (§4a)."""
    s = "" if value is None else str(value)
    s = s.replace("\r", " ").replace("\n", " ").strip()
    if len(s) > _ENVELOPE_MAX:
        s = s[: _ENVELOPE_MAX - 1].rstrip() + "…"
    return s


def _operator_command_str(value: Any) -> str:
    """Return one complete, bounded operator command without body-like text."""
    if not isinstance(value, str):
        return ""
    command = "".join(
        char if ord(char) >= 32 else " "
        for char in value.replace("\r", " ").replace("\n", " ")
    ).strip()
    if len(command) > _OPERATOR_COMMAND_MAX:
        return ""
    return command


def _attention_prompt_excerpt(value: Any) -> str:
    """Bound an escalation body for the action-enabled Attention composer.

    This is not the generic attention envelope. It exists only so a local
    operator can see the question they are about to answer. The frontend renders
    it with textContent; the server still strips control characters and caps it.
    """
    if not isinstance(value, str):
        return ""
    s = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not s:
        return ""
    s = "".join(ch if ch == "\n" or ord(ch) >= 32 else " " for ch in s)
    if len(s) > _ATTENTION_PROMPT_MAX:
        s = s[: _ATTENTION_PROMPT_MAX - 3].rstrip() + "..."
    return s


# --------------------------------------------------- /api/gates (§4c)
#
# Gate & Evidence Wall, read side (docs/PROPOSAL-console-client-sellability.md
# #1 - the "nothing is green without proof" screen). Every gate by scope as a
# red/green/waived card, with the evidence behind a green and the reason/
# expiry behind a waiver. READ-ONLY: wraps gates.check_gates's already-
# computed status/severity/blocks/reason and merges each gate's raw evidence
# list; today web.py only ever reads ``.get("blockers", [])`` for the
# attention queue (§4a's ``_collect_web_attention_items``), discarding every
# green/waived gate and the evidence/waiver detail entirely. No new data
# capture, no writes - export (proposal #5) is explicitly out of scope here.

_GATE_EVIDENCE_MAX_ENTRIES = 50
_GATE_EVIDENCE_REFS_MAX = 20
_GATE_EVIDENCE_EXTRA_KEYS_MAX = 20
_GATE_EVIDENCE_KEY_MAX = 64


def _gate_evidence_entry(entry: Any) -> dict | None:
    """Bound one raw gates.json evidence entry to safe fields (§4c). Evidence
    entries are operator/CI-authored (``gates.set_gate``), not raw message
    bodies, but every string is still capped like every other envelope field
    - belt-and-braces against an oversized/binary value riding a JSON field
    that was never meant to carry prose."""
    if not isinstance(entry, dict):
        return None
    out: dict[str, Any] = {}
    source = entry.get("source")
    if isinstance(source, str):
        out["source"] = _envelope_str(source)
    refs = entry.get("refs")
    if isinstance(refs, list):
        out["refs"] = [
            _envelope_str(r) for r in refs[:_GATE_EVIDENCE_REFS_MAX] if isinstance(r, str)
        ]
    at = entry.get("at")
    if isinstance(at, str):
        out["at"] = _envelope_str(at)
    by = entry.get("by")
    if isinstance(by, str):
        out["by"] = _envelope_str(by)
    # F-2 (review rq-093f956dd595): every OTHER key from gates.json's
    # operator-defined evidence_details passes through too - gates.py leaves
    # that schema open by design (coverage_percent, pr_url, ...), so a closed
    # field list isn't viable here. Bound the KEY the same way every value is
    # already bounded, and cap how many extra keys ride one entry (entries
    # themselves are already capped at _GATE_EVIDENCE_MAX_ENTRIES).
    #
    # R-1 (review rq-e05589aa3c80, follow-up on F-2): the reserved-name and
    # duplicate check MUST run on the bounded key, not the raw one - a raw
    # key like "source " (trailing space) or two long keys sharing the same
    # 64-char prefix are DISTINCT raw keys but collide once bounded, and
    # checking the raw key let a collision silently overwrite an
    # already-populated field (the canonical "source" in the first case).
    # Skip on collision (first write wins) rather than overwrite.
    extra_keys = 0
    for key, value in entry.items():
        if not isinstance(key, str) or extra_keys >= _GATE_EVIDENCE_EXTRA_KEYS_MAX:
            continue
        bounded_key = _envelope_str(key)[:_GATE_EVIDENCE_KEY_MAX]
        if not bounded_key or bounded_key in ("source", "refs", "at", "by") or bounded_key in out:
            continue
        if isinstance(value, str):
            out[bounded_key] = _envelope_str(value)
        elif isinstance(value, (int, float, bool)) or value is None:
            out[bounded_key] = value
        else:
            continue
        extra_keys += 1
    return out


def _gate_waiver_entry(waiver: Any) -> dict | None:
    if not isinstance(waiver, dict):
        return None
    return {
        "operator": _envelope_str(waiver.get("operator")),
        "date": _envelope_str(waiver.get("date")),
        "reason": _envelope_str(waiver.get("reason")),
        "scope": _envelope_str(waiver.get("scope")),
        "expires": _envelope_str(waiver.get("expires")),
    }


def build_gates(desc: RootDescriptor) -> dict:
    """The /api/gates payload for one root (§4c). Envelope-only, GET-only,
    read-only. Errors-as-data (parity with build_attention/build_state): a
    corrupt or uninitialized root must NOT 500 this JSON route - it degrades
    to a 200 body ``{"gates": [], "count": 0, "errors": ["<str(e)>"]}``.
    """
    store = desc.store
    try:
        from agenttalk import gates as _gates

        checked = _gates.check_gates(store.root)
        raw_state = _gates.load_gate_state(store.root)
        raw_gates = raw_state.get("gates")
        if not isinstance(raw_gates, dict):
            raw_gates = {}
        wire: list[dict] = []
        for item in checked.get("gates", []):
            name = item.get("name", "")
            raw = raw_gates.get(name) if isinstance(name, str) else None
            evidence_raw = raw.get("evidence") if isinstance(raw, dict) else None
            evidence: list[dict] = []
            if isinstance(evidence_raw, list):
                for entry in evidence_raw[:_GATE_EVIDENCE_MAX_ENTRIES]:
                    parsed = _gate_evidence_entry(entry)
                    if parsed is not None:
                        evidence.append(parsed)
            wire.append({
                "name": _envelope_str(name),
                "status": _envelope_str(item.get("status") or "unknown"),
                "severity": _envelope_str(item.get("severity") or "blocker"),
                "scope": _envelope_str(item.get("scope") or "global"),
                "blocks": bool(item.get("blocks")),
                "reason": _envelope_str(item.get("reason") or ""),
                "updated_at": _envelope_str(item.get("updated_at") or ""),
                "updated_by": _envelope_str(item.get("updated_by") or ""),
                "evidence": evidence,
                "waiver": _gate_waiver_entry(item.get("waiver")),
            })
        return {
            "root": desc.label,
            "root_path": str(store.root),
            "root_info": _root_info(desc),
            "target_root_project_id": store.project_id(),
            "verdict": checked.get("verdict", "HOLD"),
            "required_gates": [
                _envelope_str(n) for n in (checked.get("required_gates") or [])
            ],
            "gates": wire,
            "count": len(wire),
        }
    except Exception as e:  # noqa: BLE001 — errors-as-data, never a 500
        return {
            "root": desc.label,
            "root_path": str(store.root),
            "root_info": _root_info(desc),
            "target_root_project_id": store.project_id(),
            "verdict": "HOLD",
            "required_gates": [],
            "gates": [],
            "count": 0,
            "errors": [str(e)],
        }


# --------------------------------------------------- /api/risk-register (§4e)
#
# Risk Register relabel (docs/PROPOSAL-console-client-sellability.md #6): the
# SAME ranked queue /api/attention already computes (build_attention, §4a),
# resorted by severity/age and relabeled with client-legible categories and an
# owner column instead of the operator triage source tags, PLUS open
# onboarding drift/unknown findings (the proposal's #6 body explicitly lists
# "open onboarding drift/unknown" alongside stalled agent/dead letter/gate
# blocker - review rq-a7038d8175f2 finding 3). READ-ONLY, no new data, no
# writes.

_RISK_CATEGORY_LABELS: dict[str, str] = {
    "escalation": "Decision needed",
    "supervisor": "Process health",
    "gate": "Gate blocker",
    "deadletter": "Delivery failure",
    "coordination_stall": "Coordination risk",
    "stuck": "Process health",
    "onboarding_drift": "Doc/code drift",
    "onboarding_unknown": "Open unknown",
}
# severity stays the SAME "high"/"med"/"low" vocabulary /api/attention already
# uses (not remapped to "medium") so the frontend can reuse the existing
# SEV_COLOR/SEV_LABEL/.sev-<key> convention verbatim - this is a relabel of
# category/sort, not a new severity vocabulary.
_RISK_SEVERITY_ORDER = {"high": 0, "med": 1, "low": 2}
# attention.py's typed meta.attention risk_severity vocabulary is "low" /
# "medium" / "high" (RISK_LEVELS) - distinct spelling from the wire's "med".
# "unknown" (the _mk_item default when a source never set it) intentionally
# has NO entry here, so an untyped item falls through to the coarse
# per-source severity below rather than silently becoming "low".
_RISK_SEVERITY_FROM_TYPED = {"high": "high", "medium": "med", "low": "low"}
# F-1 (review rq-093f956dd595): every neighboring surface in this diff bounds
# itself (_ONBOARDING_DEFAULT_LIMIT, _GATE_EVIDENCE_MAX_ENTRIES, ...) - the
# register itself was left unbounded after removing the 50-run cap. A high
# cap + an explicit `truncated` count keeps the "an old item must not
# silently vanish" intent (the point was never "no bound", only "no SILENT
# bound").
_RISK_REGISTER_ITEM_CAP = 500
# B-1/B-2 (review rq-093f956dd595): cap how many degraded-source notes ride
# the payload, so a pathological number of corrupt onboarding runs can't
# blow up the response - the COUNT of open items lost is what matters to a
# human, not an unbounded list of which ones.
_RISK_DEGRADED_MAX = 50


def build_risk_register(desc: RootDescriptor) -> dict:
    """The /api/risk-register payload for one root (§4e). Envelope-only,
    GET-only, read-only. Errors-as-data (parity with build_attention): a
    corrupt/uninitialized root degrades to a 200 body with ``items: []`` and
    an ``errors`` list, never a 500.

    B-1 (review rq-093f956dd595): a register whose CONTRACT is "each open
    item" must never let an inner collection failure render as a confident,
    error-free "0 risks" - that is indistinguishable from a genuine all-clear
    and is the worst failure mode for a screen whose purpose is letting a
    client conclude nothing is outstanding. Every inner best-effort fallback
    below (liaison resolution, stuck-agent derivation, onboarding) now
    records what it lost into ``degraded``, surfaced as ``partial`` +
    ``degraded_sources`` even on an otherwise-200 response - never silently
    swallowed.
    """
    store = desc.store
    now = datetime.now(timezone.utc)
    degraded: list[str] = []
    try:
        cfg = _safe_load_config(store)
        roster = cfg.get("agents", []) or []
        try:
            for_agent = store.operator_facing() or store.sole_lead()
        except Exception as e:  # noqa: BLE001 — a corrupt root can't resolve a liaison
            for_agent = None
            degraded.append(_envelope_str(f"liaison_resolution: {e}"))
        items = _collect_web_attention_items(store, roster, for_agent)
        disps, _problems = _attention.read_dispositions(store)
        queue = _attention.build_queue(items, disps,
                                       now_iso=now.isoformat().replace("+00:00", "Z"))
        risks: list[dict] = []
        for it in queue.get("items", []):
            src = it.get("source", "")
            mapped = _ATTENTION_SOURCE_MAP.get(src)
            if mapped is None:
                continue  # unknown internal source — skip rather than mislabel
            wire_source, _label, coarse_severity = mapped
            # Prefer the item's OWN typed risk assessment (an operator-authored
            # escalation's meta.attention.risk_severity) over the coarse
            # per-source default - a low-risk escalation must not be forced to
            # "high" just because escalations are high-severity on average
            # (review rq-a7038d8175f2 finding 1).
            severity = _RISK_SEVERITY_FROM_TYPED.get(
                it.get("risk_severity"), coarse_severity)
            risks.append({
                "id": it.get("item_id", ""),
                "category": wire_source,
                "category_label": _RISK_CATEGORY_LABELS.get(wire_source, "Other"),
                "severity": severity,
                "title": _envelope_str(it.get("title") or "attention needed"),
                "owner": _attention_agent(it),
                "detail": _envelope_str(it.get("why_it_matters") or ""),
                "age_seconds": float(it.get("age_seconds") or 0),
                "human_can_unblock_now": bool(it.get("human_can_unblock_now")),
            })
        try:
            agents = _agent_entries(store, cfg, _validated_for_state(store, cfg)[0],
                                    for_agent)
        except Exception as e:  # noqa: BLE001
            agents = []
            degraded.append(_envelope_str(f"stuck_agents: {e}"))
        for stuck in _derive_stuck_items(agents, now=now):
            risks.append({
                "id": stuck["id"],
                "category": "stuck",
                "category_label": _RISK_CATEGORY_LABELS["stuck"],
                "severity": stuck.get("severity") or "low",
                "title": _envelope_str(stuck.get("title") or ""),
                "owner": stuck.get("agent"),
                "detail": _envelope_str(stuck.get("detail") or ""),
                "age_seconds": float(stuck.get("age_seconds") or 0),
                "human_can_unblock_now": bool(stuck.get("human_can_unblock_now")),
            })
        try:
            # limit=None, NOT build_onboarding(desc) - that helper caps at
            # _ONBOARDING_DEFAULT_LIMIT (50) newest-first runs for the
            # Onboarding VIEW's presentation payload. A risk register must
            # source "each open item" (proposal §3 #6) without that cap: an
            # older unresolved drift/unknown must not silently vanish once
            # 50 newer runs exist (review rq-4ecf94c4f814 finding 1).
            onboarding_result = _onboarding.list_runs(store, limit=None)
            all_runs = onboarding_result.get("runs") or []
            # B-2: a run whose ledger fails to parse is SKIPPED from `runs`
            # and only ever named in `problems` - discarding that channel
            # drops the run's open findings with no signal at all, no
            # exception required. Surface which runs were unreadable.
            for p in (onboarding_result.get("problems") or [])[:_RISK_DEGRADED_MAX]:
                degraded.append(_envelope_str(
                    f"onboarding_run:{_onboarding_short(p.get('run_id'), limit=96)}"))
            for run in all_runs:
                run_records = run.get("records") or {}
                run_id = _onboarding_short(run.get("run_id") or run.get("id"), limit=96)
                run_title = _onboarding_short(run.get("title"), limit=240)
                for kind, category in (
                    (_onboarding.KIND_DRIFT, "onboarding_drift"),
                    (_onboarding.KIND_UNKNOWN, "onboarding_unknown"),
                ):
                    for rec in run_records.get(kind) or []:
                        if not isinstance(rec, dict) or rec.get("status") != "open":
                            continue
                        age = _age_seconds_of(rec.get("updated_at"), now=now)
                        blocking = bool(rec.get("blocking"))
                        owner = rec.get("owner") or rec.get("actor") or None
                        risks.append({
                            "id": f"onboarding:{run_id}:{kind}:"
                                  f"{_onboarding_short(rec.get('key'), limit=128)}",
                            "category": category,
                            "category_label": _RISK_CATEGORY_LABELS[category],
                            "severity": "high" if blocking else "med",
                            "title": _envelope_str(
                                rec.get("summary") or f"{kind}: {rec.get('key')}"),
                            "owner": _onboarding_short(owner, limit=96) if owner else None,
                            "detail": run_title,
                            # L-2: an unparseable updated_at must not read as
                            # "0 seconds old" (misleadingly fresh) AND must
                            # not silently sort to the bottom of its severity
                            # band (deprioritized) - "unknown" is flagged
                            # explicitly and sorted as the MOST urgent within
                            # its band via the internal-only _sort_age below.
                            "age_seconds": age if age is not None else 0.0,
                            "age_unknown": age is None,
                            # L-1: "blocking" (an onboarding workflow-progress
                            # claim) is a DIFFERENT claim from "a human can
                            # clear this right now" (a triage-affordance
                            # claim the attention pipeline computes
                            # separately for every other source). Nothing
                            # external gates a human from acting on an
                            # onboarding finding, so this is unconditionally
                            # true here - not copied from `blocking`.
                            "human_can_unblock_now": True,
                            "_sort_age": age if age is not None else float("inf"),
                        })
        except Exception as e:  # noqa: BLE001
            degraded.append(_envelope_str(f"onboarding: {e}"))
        risks.sort(key=lambda r: (_RISK_SEVERITY_ORDER.get(r["severity"], 3),
                                  -r.get("_sort_age", r["age_seconds"])))
        truncated = max(0, len(risks) - _RISK_REGISTER_ITEM_CAP)
        risks = risks[:_RISK_REGISTER_ITEM_CAP]
        for r in risks:
            r.pop("_sort_age", None)
        degraded = degraded[:_RISK_DEGRADED_MAX]
        return {
            "root": desc.label,
            "root_path": str(store.root),
            "root_info": _root_info(desc),
            "target_root_project_id": store.project_id(),
            "items": risks,
            "count": len(risks),
            "truncated": truncated,
            "partial": bool(degraded),
            "degraded_sources": degraded,
        }
    except Exception as e:  # noqa: BLE001 — errors-as-data, never a 500
        return {
            "root": desc.label,
            "root_path": str(store.root),
            "root_info": _root_info(desc),
            "target_root_project_id": store.project_id(),
            "items": [],
            "count": 0,
            "truncated": 0,
            "partial": True,
            "degraded_sources": degraded[:_RISK_DEGRADED_MAX],
            "errors": [str(e)],
        }


# --------------------------------------------------- /api/ownership (§4d)
#
# Ownership & Accountability Map (docs/PROPOSAL-console-client-sellability.md
# #7): the full domain-ownership registry - which agent/team owns which part
# of the codebase, which paths require shared sign-off - as its own view,
# instead of the thin per-agent ``owned_domains`` slice /api/state already
# carries (§3a's ``_owned_domains_map``, only names + globs, only visible on
# a single agent's detail card). READ-ONLY: a straight read of domains.json
# via the existing ``domains.load_registry``, no new data, no writes.

def build_ownership(desc: RootDescriptor) -> dict:
    """The /api/ownership payload for one root (§4d). Envelope-only,
    GET-only, read-only. A missing registry is a valid empty state
    (``domains.load_registry`` already returns ``empty_registry()``); a
    MALFORMED registry degrades to a 200 body with ``domains: []`` and an
    ``errors`` list, matching build_gates/build_attention's errors-as-data
    contract rather than a 500."""
    store = desc.store
    try:
        cfg = _safe_load_config(store)
        try:
            reg = _domains.load_registry(store.dir / _domains.FILENAME, cfg)
        except _domains.DomainError as e:
            return {
                "root": desc.label,
                "root_path": str(store.root),
                "root_info": _root_info(desc),
                "target_root_project_id": store.project_id(),
                "domains": [],
                "shared_paths": [],
                "count": 0,
                "errors": [str(e)],
            }
        data = reg.data
        domains: list[dict] = []
        for did, dentry in sorted((data.get("domains") or {}).items()):
            if not isinstance(dentry, dict):
                continue
            domains.append({
                "id": _envelope_str(did),
                "title": _envelope_str(dentry.get("title") or did),
                "owners": _domains.resolve_refset(dentry.get("owners") or {}, cfg),
                "reviewers": _domains.resolve_refset(dentry.get("reviewers") or {}, cfg),
                "curators": _domains.resolve_refset(dentry.get("curators") or {}, cfg),
                "owned_globs": [
                    _envelope_str(g) for g in (dentry.get("owned_globs") or [])
                ],
                "description": _envelope_str(dentry.get("description") or ""),
            })
        shared_paths: list[dict] = []
        for entry in data.get("shared_paths") or []:
            if not isinstance(entry, dict):
                continue
            shared_paths.append({
                "glob": _envelope_str(entry.get("glob") or ""),
                "category": _envelope_str(entry.get("category") or ""),
                "requires": _envelope_str(entry.get("requires") or ""),
                "default_reviewers": _domains.resolve_refset(
                    entry.get("default_reviewers") or {}, cfg),
                "default_approvers": _domains.resolve_refset(
                    entry.get("default_approvers") or {}, cfg),
                "description": _envelope_str(entry.get("description") or ""),
            })
        return {
            "root": desc.label,
            "root_path": str(store.root),
            "root_info": _root_info(desc),
            "target_root_project_id": store.project_id(),
            "domains": domains,
            "shared_paths": shared_paths,
            "count": len(domains),
        }
    except Exception as e:  # noqa: BLE001 — errors-as-data, never a 500
        return {
            "root": desc.label,
            "root_path": str(store.root),
            "root_info": _root_info(desc),
            "target_root_project_id": store.project_id(),
            "domains": [],
            "shared_paths": [],
            "count": 0,
            "errors": [str(e)],
        }


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


# ------------------------------------------------------------ /api/lead-chat

def _lead_chat_messages(
    store: Store, *, request_id: str, operator: str, lead: str, limit: int
) -> list[dict]:
    now = datetime.now(timezone.utc)
    allowed = {operator, lead}
    msgs = [
        m for m in _all_messages(store)
        if m.sender in allowed and m.recipient in allowed
        and (
            (m.meta or {}).get("request_id") == request_id
            or (
                isinstance((m.meta or {}).get("request_id"), str)
                and str((m.meta or {}).get("request_id")).startswith("esc-")
                and (
                    (m.meta or {}).get("needs_operator") == "true"
                    or (m.meta or {}).get("operator_answer") == "true"
                )
            )
        )
    ]
    msgs.sort(key=lambda m: m.id)
    out: list[dict] = []
    for m in msgs[-max(1, limit):]:
        out.append({
            "id": m.id,
            "from": m.sender,
            "to": m.recipient,
            "kind": m.kind,
            "subject": m.subject or "",
            "body": m.body or "",
            "ts": m.ts,
            "age_seconds": _age_seconds_of(m.ts, now=now),
        })
    return out


def _lead_chat_pending_decisions(store: Store, operator: str, lead: str) -> list[dict]:
    pending = _web_needs_operator(store, operator)
    out: list[dict] = []
    for item in pending:
        if _envelope_str(item.get("sender")) != lead:
            continue
        meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
        att = meta.get("attention") if isinstance(meta.get("attention"), dict) else {}
        options = att.get("options") if isinstance(att.get("options"), list) else []
        out.append({
            "request_id": _envelope_str(item.get("request_id")),
            "sender": _envelope_str(item.get("sender")),
            "subject": _envelope_str(item.get("subject") or "operator input needed"),
            "decision": _envelope_str(att.get("decision") or item.get("subject")
                                      or "operator input needed"),
            "recommendation": _envelope_str(att.get("recommendation") or ""),
            "priority": _envelope_str(att.get("priority") or ""),
            "risk_severity": _envelope_str(att.get("risk_severity") or ""),
            "options": [_envelope_str(o) for o in options if isinstance(o, str)],
            "age_seconds": item.get("age_seconds"),
            "answerable": True,
        })
    return out


def _send_authenticated_lead_chat(
    store: Store, *, body: str
) -> tuple[Message | None, dict | None, dict | None]:
    """Send operator->lead from the authenticated web request boundary only.

    Honest ceiling: this is an auditable same-machine bus assertion gated by
    loopback, CSRF, and the in-memory dashboard session. It prevents public
    intent-queue spoofing, but it is not a cryptographic boundary against a
    fully privileged local process that can write raw message files or inspect
    process memory.
    """
    try:
        operator, lead = store.lead_chat_identities()
        request_id = store.lead_chat_request_id(operator=operator, lead=lead)
    except ValueError as e:
        return None, None, {
            "status": HTTPStatus.CONFLICT,
            "error": "lead_chat_identity_denied",
            "detail": str(e),
        }
    liveness = store.lead_chat_liveness(lead=lead)
    if not liveness.get("available"):
        return None, None, {
            "status": HTTPStatus.CONFLICT,
            "error": "lead_unavailable",
            "detail": liveness.get("reason") or liveness.get("detail") or "",
            "liveness": liveness,
        }
    from agenttalk import intents as intent_mod
    meta = intent_mod.lead_chat_stable_meta(
        store, operator=operator, lead=lead)
    try:
        msg = store.send(
            sender=operator,
            recipient=lead,
            body=body,
            kind="message",
            subject="lead chat",
            meta=meta,
            _allow_reserved_sender=True,
        )
    except ValueError as e:
        return None, None, {
            "status": HTTPStatus.CONFLICT,
            "error": "lead_chat_send_rejected",
            "detail": str(e),
        }
    return msg, {
        "operator": operator,
        "lead": lead,
        "request_id": request_id,
    }, None


def _send_authenticated_lead_chat_answer(
    store: Store, *, request_id: str, body: str
) -> tuple[Message | None, dict | None, dict | None]:
    try:
        operator, lead = store.lead_chat_identities()
        lead_chat_request_id = store.lead_chat_request_id(
            operator=operator, lead=lead)
    except ValueError as e:
        return None, None, {
            "status": HTTPStatus.CONFLICT,
            "error": "lead_chat_identity_denied",
            "detail": str(e),
        }
    liveness = store.lead_chat_liveness(lead=lead)
    if not liveness.get("available"):
        return None, None, {
            "status": HTTPStatus.CONFLICT,
            "error": "lead_unavailable",
            "detail": liveness.get("reason") or liveness.get("detail") or "",
            "liveness": liveness,
        }
    result = store.send_operator_answer_atomic(
        actor=operator,
        request_id=request_id,
        body=body,
        subject=f"operator answer ({request_id})",
        expected_recipient=lead,
    )
    if not result.ok:
        status = HTTPStatus.CONFLICT
        if result.failed:
            status = HTTPStatus.INTERNAL_SERVER_ERROR
        return None, None, {
            "status": status,
            "error": result.denial_code or "operator_answer_denied",
            "detail": result.detail,
        }
    return result.message, {
        "request_id": request_id,
        "lead_chat_request_id": lead_chat_request_id,
    }, None


def build_lead_chat(desc: RootDescriptor, *, limit: int = _LEAD_CHAT_LIMIT) -> dict:
    """Lead-chat read model: bounded transcript plus fail-closed availability."""
    store = desc.store
    payload: dict[str, Any] = {
        "root": desc.label,
        "root_path": str(store.root),
        "root_info": _root_info(desc),
        "target_root_project_id": store.project_id(),
        "available": False,
        "status": "unavailable",
        "messages": [],
        "pending_decisions": [],
        "limit": max(1, min(_LEAD_CHAT_LIMIT, int(limit))),
    }
    try:
        operator, lead = store.lead_chat_identities()
        request_id = store.lead_chat_request_id(operator=operator, lead=lead)
        liveness = store.lead_chat_liveness(lead=lead)
        payload.update({
            "operator": operator,
            "lead": lead,
            "request_id": request_id,
            "available": bool(liveness.get("available")),
            "status": liveness.get("status") or "unavailable",
            "liveness": liveness,
            "messages": _lead_chat_messages(
                store, request_id=request_id, operator=operator,
                lead=lead, limit=payload["limit"]),
            "pending_decisions": _lead_chat_pending_decisions(store, operator, lead),
        })
        if not payload["available"]:
            payload["error"] = "lead_unavailable"
            payload["detail"] = liveness.get("reason") or liveness.get("detail") or ""
        return payload
    except Exception as e:  # noqa: BLE001 - fail-safe JSON, never a broken endpoint
        payload["error"] = "lead_chat_unavailable"
        payload["detail"] = _envelope_str(e)
        return payload


def _age_seconds_of(ts: Any, *, now: datetime) -> float | None:
    """Seconds since a message ``ts`` (ISO-8601), or None if unparseable."""
    dt = _parse_iso(ts)
    if dt is None:
        return None
    return round((now - dt).total_seconds(), 3)


# ------------------------------------------------------------ HTTP handler

def _make_handler(roots: list[RootDescriptor], *, enable_actions: bool = False) -> type[BaseHTTPRequestHandler]:
    """Build a request handler class closed over the watched roots.

    Returns a class (not an instance) because ``ThreadingHTTPServer``
    instantiates it per connection. Legacy routes stay bound to ``roots[0]``;
    Team Console routes resolve their optional ``?root=`` selection here.
    """
    roots = _normalize_descriptors(roots)
    store = roots[0].store
    # Health-timeline ring (§5): one instance per SERVER (closed over here, not
    # a module global) so parallel test servers never share history. In-memory
    # only — never a file (the read-only invariant). Handler instances are
    # created per connection, so the ring lives on this closure, shared across
    # them for the lifetime of the server.
    health_history = HealthTimelineRing()
    session_id = secrets.token_hex(8)
    csrf_token = secrets.token_urlsafe(32) if enable_actions else ""
    rate = {"tokens": float(_ACTION_RATE_BURST), "updated": time.monotonic()}
    rate_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        server_version = f"agenttalk/{__version__}"
        # Silence default stderr access log — we route everything
        # through ``log_message`` so the caller can opt in / out.
        _quiet = True

        def handle_one_request(self) -> None:
            try:
                super().handle_one_request()
            except Exception as exc:
                if _is_client_disconnect(exc):
                    self.close_connection = True
                    return
                raise

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            if not self._quiet:
                super().log_message(format, *args)

        # ---- defense-in-depth: per-request loopback check
        def _is_loopback_peer(self) -> bool:
            return _is_loopback_addr(self.client_address[0] or "")

        # ---- response helpers
        def _send(self, status: int, body: bytes, content_type: str,
                  csp: str | None = None,
                  extra_headers: dict[str, str] | None = None) -> None:
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
            for k, v in (extra_headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _send_html(self, status: int, body: bytes,
                       csp: str | None = None) -> None:
            self._send(status, body, "text/html; charset=utf-8", csp)

        def _send_json(self, status: int, payload: Any) -> None:
            data = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
            self._send(status, data, "application/json; charset=utf-8")

        def _json_problem(self, status: int, code: str, detail: str,
                          *, close: bool = False) -> None:
            if close:
                self.close_connection = True
            self._send_json(status, {"error": code, "detail": detail})

        def _error_html(self, status: int, message: str) -> None:
            body = _html_page(
                f"agenttalk :: {status}",
                f"<h1>{status}</h1><p>{html.escape(message)}</p>"
                "<p><a href=\"/\">&larr; back to dashboard</a></p>",
            )
            try:
                self._send_html(status, body)
            except Exception as exc:
                if _is_client_disconnect(exc):
                    self.close_connection = True
                    return
                raise

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
            except Exception as exc:  # noqa: BLE001 — never leak a traceback to the browser
                if _is_client_disconnect(exc):
                    self.close_connection = True
                    return
                self._error_html(HTTPStatus.INTERNAL_SERVER_ERROR,
                                 "internal server error")
                raise  # surface in stderr for the operator

        def do_POST(self) -> None:  # noqa: N802
            if not self._check_peer_or_403():
                return
            if not enable_actions:
                self._method_not_allowed()
                return
            path = self._request_path(validate_absolute=True)
            if not path:
                self._json_problem(HTTPStatus.FORBIDDEN, "bad_host",
                                   "absolute-form request target must match this loopback server",
                                   close=True)
                return
            if path not in ("/api/intent", "/api/lead-chat"):
                self._method_not_allowed()
                return
            if path == "/api/lead-chat":
                self._handle_lead_chat_post()
            else:
                self._handle_intent_post()

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
            allow = (
                "POST"
                if enable_actions and self._request_path() in ("/api/intent", "/api/lead-chat")
                else "GET, HEAD"
            )
            self._send(HTTPStatus.METHOD_NOT_ALLOWED, b"", "text/plain; charset=utf-8",
                       extra_headers={"Allow": allow})

        def _request_path(self, *, validate_absolute: bool = False) -> str:
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.scheme or parsed.netloc:
                if validate_absolute and not self._absolute_target_allowed(parsed):
                    return ""
                return parsed.path or "/"
            return self.path.split("?", 1)[0].split("#", 1)[0] or "/"

        def _absolute_target_allowed(self, parsed: urllib.parse.SplitResult) -> bool:
            _bound_host, bound_port = _server_host_port(self)
            target = _normalized_host_port(parsed.netloc)
            if target is None:
                return False
            _target_host, target_port = target
            return (target_port if target_port is not None else 80) == bound_port

        def _host_allowed(self) -> bool:
            _host, port = _server_host_port(self)
            hp = _normalized_host_port(self.headers.get("Host", ""))
            return (
                hp is not None
                and _is_loopback_addr(hp[0])
                and (hp[1] if hp[1] is not None else 80) == port
            )

        def _origin_allowed(self, value: str | None) -> bool:
            if not value:
                return False
            parsed = urllib.parse.urlsplit(value)
            if parsed.scheme != "http" or not parsed.netloc:
                return False
            host = _normalized_host_port(self.headers.get("Host", ""))
            origin = _normalized_host_port(parsed.netloc)
            return _same_host_port(origin, host)

        def _session_origin_headers_ok(self) -> bool:
            if not self._host_allowed():
                return False
            origin = self.headers.get("Origin")
            if origin and not self._origin_allowed(origin):
                return False
            ref = self.headers.get("Referer")
            if ref:
                parsed = urllib.parse.urlsplit(ref)
                if parsed.scheme != "http" or not self._origin_allowed(f"{parsed.scheme}://{parsed.netloc}"):
                    return False
            fetch_site = self.headers.get("Sec-Fetch-Site")
            return fetch_site in (None, "", "same-origin", "same-site", "none")

        def _content_type_ok(self) -> bool:
            ctype = self.headers.get("Content-Type", "")
            return ctype.split(";", 1)[0].strip().casefold() == "application/json"

        def _rate_allowed(self) -> bool:
            with rate_lock:
                now = time.monotonic()
                elapsed = max(0.0, now - float(rate["updated"]))
                rate["tokens"] = min(
                    float(_ACTION_RATE_BURST),
                    float(rate["tokens"]) + elapsed * (_ACTION_RATE_PER_MINUTE / 60.0))
                rate["updated"] = now
                if float(rate["tokens"]) < 1.0:
                    return False
                rate["tokens"] = float(rate["tokens"]) - 1.0
                return True

        def _active_intent_capacity_ok(
            self, target_store: Store,
        ) -> tuple[bool, str, str]:
            count, size = target_store._intent_active_usage()  # noqa: SLF001 - web maps the store cap to HTTP before parse
            if count >= Store.INTENT_MAX_ACTIVE:
                return False, "intent_cap", "too many active intents"
            if size >= Store.INTENT_MAX_ACTIVE_BYTES:
                return False, "intent_bytes", "active intent byte cap reached"
            return True, "", ""

        def _read_limited_body(self) -> bytes | None:
            raw_len = self.headers.get("Content-Length")
            if raw_len is not None:
                try:
                    n = int(raw_len)
                except ValueError:
                    self._json_problem(HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                                       "body_too_large", "invalid Content-Length",
                                       close=True)
                    return None
                if n > _ACTION_BODY_LIMIT:
                    self._json_problem(HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                                       "body_too_large", "request body exceeds 64 KiB",
                                       close=True)
                    return None
                return self.rfile.read(max(0, n))
            data = self.rfile.read(_ACTION_BODY_LIMIT + 1)
            if len(data) > _ACTION_BODY_LIMIT:
                self._json_problem(HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                                   "body_too_large", "request body exceeds 64 KiB",
                                   close=True)
                return None
            return data

        def _handle_session_get(self) -> None:
            if not enable_actions:
                self._error_html(HTTPStatus.NOT_FOUND, "not found")
                return
            if not self._session_origin_headers_ok():
                self._json_problem(HTTPStatus.FORBIDDEN, "bad_origin",
                                   "session endpoint requires same-origin loopback headers")
                return
            selected = self._root_selection(self._request_params())
            if selected is None:
                self._bad_root()
                return
            root_index, root = selected
            self._send_json(HTTPStatus.OK, {
                "session_id": session_id,
                "csrf_token": csrf_token,
                "root_info": _root_info(root),
                "target_root_index": root_index,
                "target_root_project_id": root.store.project_id(),
                "target_root_label": root.label,
                "target_root_path": str(root.store.root),
            })

        def _handle_intent_post(self) -> None:
            if not self._host_allowed():
                self._json_problem(HTTPStatus.FORBIDDEN, "bad_host",
                                   "Host must be loopback for this server",
                                   close=True)
                return
            if not self._origin_allowed(self.headers.get("Origin")):
                self._json_problem(HTTPStatus.FORBIDDEN, "bad_origin",
                                   "Origin must match this dashboard",
                                   close=True)
                return
            if not self._content_type_ok():
                self._json_problem(HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                                   "bad_content_type",
                                   "Content-Type must be application/json",
                                   close=True)
                return
            supplied = self.headers.get("X-CSRF-Token", "")
            try:
                supplied_b = supplied.encode("ascii")
                token_b = csrf_token.encode("ascii")
            except UnicodeEncodeError:
                self._json_problem(HTTPStatus.FORBIDDEN, "bad_csrf",
                                   "CSRF token is missing or expired",
                                   close=True)
                return
            if not hmac.compare_digest(supplied_b, token_b):
                self._json_problem(HTTPStatus.FORBIDDEN, "bad_csrf",
                                   "CSRF token is missing or expired",
                                   close=True)
                return
            selected = self._write_root_selection(self._request_params())
            if selected is None:
                self._bad_root()
                return
            root_index, root = selected
            target_store = root.store
            kill = target_store.supervisor_kill_switch()
            if kill is not False:
                code = "executor_disabled" if kill else "executor_state_unreadable"
                self._json_problem(HTTPStatus.LOCKED, code,
                                   "supervisor kill-switch blocks dashboard writes",
                                   close=True)
                return
            body = self._read_limited_body()
            if body is None:
                return
            if not self._rate_allowed():
                self._json_problem(HTTPStatus.TOO_MANY_REQUESTS, "rate_limited",
                                   "too many dashboard write attempts")
                return
            ok, cap_code, cap_detail = self._active_intent_capacity_ok(target_store)
            if not ok:
                self._json_problem(HTTPStatus.TOO_MANY_REQUESTS, cap_code, cap_detail)
                return
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                self._json_problem(HTTPStatus.BAD_REQUEST, "invalid_json",
                                   "request body must be a JSON object")
                return
            if not isinstance(payload, dict):
                self._json_problem(HTTPStatus.BAD_REQUEST, "invalid_json",
                                   "request body must be a JSON object")
                return
            kind = payload.get("kind")
            intent_payload = payload.get("payload")
            from agenttalk import intents as intent_mod
            errors = intent_mod.validate_intent(kind, intent_payload)
            if errors:
                self._send_json(HTTPStatus.BAD_REQUEST,
                                {"error": "invalid_intent", "details": errors})
                return
            try:
                rec = target_store.write_intent(
                    kind, intent_payload,
                    origin={"source": "web-console", "session_id": session_id,
                            "host": self.headers.get("Host", ""),
                            "origin": self.headers.get("Origin", "")})
            except Store.IntentCapacityError as e:
                status = HTTPStatus.INSUFFICIENT_STORAGE if e.code == "max_bytes" else HTTPStatus.TOO_MANY_REQUESTS
                self._json_problem(status, e.code, str(e))
                return
            except ValueError as e:
                self._json_problem(HTTPStatus.BAD_REQUEST, "invalid_intent", str(e))
                return
            self._send_json(HTTPStatus.ACCEPTED,
                            {"intent_id": rec["intent_id"], "state": rec["state"],
                             "root_info": _root_info(root),
                             "target_root_index": root_index,
                             "target_root_project_id": target_store.project_id(),
                             "target_root_label": root.label})

        def _handle_lead_chat_post(self) -> None:
            if not self._host_allowed():
                self._json_problem(HTTPStatus.FORBIDDEN, "bad_host",
                                   "Host must be loopback for this server",
                                   close=True)
                return
            if not self._origin_allowed(self.headers.get("Origin")):
                self._json_problem(HTTPStatus.FORBIDDEN, "bad_origin",
                                   "Origin must match this dashboard",
                                   close=True)
                return
            if not self._content_type_ok():
                self._json_problem(HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                                   "bad_content_type",
                                   "Content-Type must be application/json",
                                   close=True)
                return
            supplied = self.headers.get("X-CSRF-Token", "")
            try:
                supplied_b = supplied.encode("ascii")
                token_b = csrf_token.encode("ascii")
            except UnicodeEncodeError:
                self._json_problem(HTTPStatus.FORBIDDEN, "bad_csrf",
                                   "CSRF token is missing or expired",
                                   close=True)
                return
            if not hmac.compare_digest(supplied_b, token_b):
                self._json_problem(HTTPStatus.FORBIDDEN, "bad_csrf",
                                   "CSRF token is missing or expired",
                                   close=True)
                return
            selected = self._write_root_selection(self._request_params())
            if selected is None:
                self._bad_root()
                return
            root_index, root = selected
            target_store = root.store
            kill = target_store.supervisor_kill_switch()
            if kill is not False:
                code = "executor_disabled" if kill else "executor_state_unreadable"
                self._json_problem(HTTPStatus.LOCKED, code,
                                   "supervisor kill-switch blocks dashboard writes",
                                   close=True)
                return
            body = self._read_limited_body()
            if body is None:
                return
            if not self._rate_allowed():
                self._json_problem(HTTPStatus.TOO_MANY_REQUESTS, "rate_limited",
                                   "too many dashboard write attempts")
                return
            ok, cap_code, cap_detail = self._active_intent_capacity_ok(target_store)
            if not ok:
                self._json_problem(HTTPStatus.TOO_MANY_REQUESTS, cap_code, cap_detail)
                return
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                self._json_problem(HTTPStatus.BAD_REQUEST, "invalid_json",
                                   "request body must be a JSON object")
                return
            if not isinstance(payload, dict):
                self._json_problem(HTTPStatus.BAD_REQUEST, "invalid_json",
                                   "request body must be a JSON object")
                return
            keys = set(payload)
            if keys == {"body"}:
                kind = "lead_chat_send"
                intent_payload = {"body": payload.get("body")}
                from agenttalk import intents as intent_mod
                errors = intent_mod.validate_intent(kind, intent_payload)
                if errors:
                    self._send_json(HTTPStatus.BAD_REQUEST,
                                    {"error": "invalid_intent",
                                     "details": errors})
                    return
                msg, chat_ids, problem = _send_authenticated_lead_chat(
                    target_store, body=intent_payload["body"])
                if problem is not None:
                    status = problem.pop("status")
                    self._send_json(status, problem)
                    return
                self._send_json(HTTPStatus.ACCEPTED, {
                    "message_id": msg.id if msg else "",
                    "state": "sent",
                    "kind": kind,
                    "request_id": (chat_ids or {}).get("request_id", ""),
                    "root_info": _root_info(root),
                    "target_root_index": root_index,
                    "target_root_project_id": target_store.project_id(),
                    "target_root_label": root.label,
                })
                return
            elif keys == {"to_request", "body"}:
                kind = "answer_escalation"
                intent_payload = {
                    "to_request": payload.get("to_request"),
                    "body": payload.get("body"),
                }
                from agenttalk import intents as intent_mod
                errors = intent_mod.validate_intent(kind, intent_payload)
                if errors:
                    self._send_json(HTTPStatus.BAD_REQUEST,
                                    {"error": "invalid_intent",
                                     "details": errors})
                    return
                chat = build_lead_chat(root)
                if not chat.get("available"):
                    self._send_json(HTTPStatus.CONFLICT, {
                        "error": "lead_unavailable",
                        "detail": chat.get("detail") or "",
                        "liveness": chat.get("liveness") or {},
                    })
                    return
                pending_ids = {
                    item.get("request_id")
                    for item in chat.get("pending_decisions") or []
                    if isinstance(item, dict)
                }
                if payload.get("to_request") not in pending_ids:
                    self._send_json(HTTPStatus.CONFLICT, {
                        "error": "decision_not_pending",
                        "detail": "lead-chat answers must target a pending "
                                  "lead escalation addressed to the operator",
                    })
                    return
                msg, answer_ids, problem = _send_authenticated_lead_chat_answer(
                    target_store,
                    request_id=intent_payload["to_request"],
                    body=intent_payload["body"],
                )
                if problem is not None:
                    status = problem.pop("status")
                    self._send_json(status, problem)
                    return
                self._send_json(HTTPStatus.ACCEPTED, {
                    "message_id": msg.id if msg else "",
                    "state": "sent",
                    "kind": kind,
                    "request_id": (answer_ids or {}).get("request_id", ""),
                    "lead_chat_request_id": (
                        (answer_ids or {}).get("lead_chat_request_id", "")
                    ),
                    "root_info": _root_info(root),
                    "target_root_index": root_index,
                    "target_root_project_id": target_store.project_id(),
                    "target_root_label": root.label,
                })
                return
            else:
                self._send_json(HTTPStatus.BAD_REQUEST, {
                    "error": "invalid_lead_chat",
                    "details": [
                        "lead chat send requires exactly {body}; "
                        "decision answer requires exactly {to_request, body}"
                    ],
                })
                return

        def _request_params(self) -> dict[str, list[str]]:
            parsed = urllib.parse.urlsplit(self.path)
            return urllib.parse.parse_qs(parsed.query, keep_blank_values=True)

        def _root_selection(
            self, params: dict[str, list[str]],
        ) -> tuple[int, RootDescriptor] | None:
            if "root" not in params:
                return 0, roots[0]
            values = params.get("root") or []
            if len(values) != 1 or not values[0]:
                return None
            wanted = values[0]
            for index, desc in enumerate(roots):
                if desc.store.project_id() == wanted:
                    return index, desc
            matches = [
                (index, desc)
                for index, desc in enumerate(roots)
                if desc.label == wanted
            ]
            if len(matches) == 1:
                return matches[0]
            return None

        def _write_root_selection(
            self, params: dict[str, list[str]],
        ) -> tuple[int, RootDescriptor] | None:
            if "root" not in params:
                return (0, roots[0]) if len(roots) == 1 else None
            values = params.get("root") or []
            if len(values) != 1 or not values[0]:
                return None
            wanted = values[0]
            matches = [
                (index, desc)
                for index, desc in enumerate(roots)
                if desc.store.project_id() == wanted
            ]
            return matches[0] if len(matches) == 1 else None

        def _root_descriptor_for_threads(
            self, params: dict[str, list[str]],
        ) -> RootDescriptor | None:
            selected = self._root_selection(params)
            return selected[1] if selected is not None else None

        def _bad_root(self) -> None:
            self._json_problem(HTTPStatus.BAD_REQUEST, "bad_root", "unknown root")

        def _threads_bad_request(self, code: str, detail: str) -> None:
            self._send_json(HTTPStatus.BAD_REQUEST, {
                "error": code,
                "detail": detail,
                "state": "closed",
                "limit": _THREADS_DEFAULT_LIMIT,
                "total_count": 0,
                "next_cursor": None,
                "items": [],
            })

        def _learning_bad_request(self, code: str, detail: str) -> None:
            self._send_json(HTTPStatus.BAD_REQUEST, {
                "schema_version": 1,
                "error": code,
                "detail": detail,
                "items": [],
                "lessons": [],
                "recent_exposures": [],
                "counts": {
                    "total": 0,
                    "showing": 0,
                    "active": 0,
                    "proposed": 0,
                    "accepted": 0,
                    "retired": 0,
                    "review_due": 0,
                    "stale": 0,
                    "expired": 0,
                    "superseded": 0,
                    "exposures": 0,
                    "invalid_notes": 0,
                    "invalid_exposures": 0,
                    "truncated": 0,
                },
                "problems": {"knowledge": [], "exposures": []},
            })

        def _onboarding_bad_request(self, code: str, detail: str) -> None:
            self._send_json(HTTPStatus.BAD_REQUEST, {
                "schema_version": 1,
                "error": code,
                "detail": detail,
                "runs": [],
                "counts": {
                    "total": 0,
                    "showing": 0,
                    "active": 0,
                    "blocked": 0,
                    "segments": 0,
                    "accepted_segments": 0,
                    "claims": 0,
                    "confirmed_claims": 0,
                    "conflicted_claims": 0,
                    "needs_human_claims": 0,
                    "open_drift": 0,
                    "open_unknowns": 0,
                    "blocking_unknowns": 0,
                    "blocking_records": 0,
                    "human_needed": 0,
                    "invalid_lines": 0,
                    "truncated": 0,
                },
                "problems": [],
            })

        def _handle_threads_get(self) -> None:
            params = self._request_params()
            state = (params.get("state") or ["closed"])[0] or "closed"
            if state != "closed":
                self._threads_bad_request("bad_state", "state must be closed")
                return
            root = self._root_descriptor_for_threads(params)
            if root is None:
                self._threads_bad_request("bad_root", "unknown root")
                return
            raw_limit = (params.get("limit") or [str(_THREADS_DEFAULT_LIMIT)])[0]
            try:
                limit = int(raw_limit)
            except ValueError:
                self._threads_bad_request("bad_limit", "limit must be an integer")
                return
            if limit <= 0:
                self._threads_bad_request("bad_limit", "limit must be positive")
                return
            limit = min(_THREADS_MAX_LIMIT, limit)
            cursor = (params.get("cursor") or [None])[0]
            if cursor and not _MESSAGE_ID_RE.match(cursor):
                self._threads_bad_request("bad_cursor", "invalid cursor")
                return
            self._send_json(HTTPStatus.OK, build_threads_index(
                root, state=state, limit=limit, cursor=cursor))

        def _handle_learning_get(self) -> None:
            params = self._request_params()
            root = self._root_descriptor_for_threads(params)
            if root is None:
                self._learning_bad_request("bad_root", "unknown root")
                return
            status = (params.get("status") or ["active"])[0] or "active"
            if status not in _LEARNING_STATUSES:
                self._learning_bad_request(
                    "bad_status",
                    "status must be one of active, proposed, review_due, stale, retired, all")
                return
            raw_limit = (params.get("limit") or [str(_LEARNING_DEFAULT_LIMIT)])[0]
            try:
                limit = int(raw_limit)
            except ValueError:
                self._learning_bad_request("bad_limit", "limit must be an integer")
                return
            if limit <= 0:
                self._learning_bad_request("bad_limit", "limit must be positive")
                return
            scope = (params.get("scope") or [""])[0].strip() or None
            tags = [
                str(tag).strip()
                for tag in params.get("tag", [])
                if str(tag).strip()
            ]
            self._send_json(HTTPStatus.OK, build_learning(
                root, status=status, scope=scope, tags=tags, limit=limit))

        def _handle_onboarding_get(self) -> None:
            params = self._request_params()
            root = self._root_descriptor_for_threads(params)
            if root is None:
                self._onboarding_bad_request("bad_root", "unknown root")
                return
            raw_limit = (params.get("limit") or [str(_ONBOARDING_DEFAULT_LIMIT)])[0]
            try:
                limit = int(raw_limit)
            except ValueError:
                self._onboarding_bad_request("bad_limit", "limit must be an integer")
                return
            if limit <= 0:
                self._onboarding_bad_request("bad_limit", "limit must be positive")
                return
            self._send_json(HTTPStatus.OK, build_onboarding(root, limit=limit))

        # ---- routing
        def _route(self) -> None:
            path = self._request_path()
            if path == "/":
                self._send_html(HTTPStatus.OK, render_dashboard(roots),
                                csp=_DASHBOARD_CSP)
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
            if path == "/api/session":
                self._handle_session_get()
                return
            if path == "/api/intents":
                selected = self._root_selection(self._request_params())
                if selected is None:
                    self._bad_root()
                    return
                root_index, _root = selected
                self._send_json(
                    HTTPStatus.OK,
                    build_intents(roots, root_index=root_index),
                )
                return
            if path == "/api/preflight":
                selected = self._root_selection(self._request_params())
                if selected is None:
                    self._bad_root()
                    return
                root_index, root = selected
                self._send_json(
                    HTTPStatus.OK,
                    build_preflight(
                        root,
                        actions_enabled=enable_actions,
                        root_index=root_index,
                    ),
                )
                return
            if path == "/api/attention":
                selected = self._root_selection(self._request_params())
                if selected is None:
                    self._bad_root()
                    return
                _root_index, root = selected
                # Ranked "needs a human" queue for the selected root. Envelope-only,
                # read-only, strict _DEFAULT_CSP (JSON is not executed).
                self._send_json(HTTPStatus.OK,
                                build_attention(root,
                                                actions_enabled=enable_actions))
                return
            if path == "/api/gates":
                selected = self._root_selection(self._request_params())
                if selected is None:
                    self._bad_root()
                    return
                _root_index, root = selected
                # Gate & Evidence Wall, read side (§4c). Envelope-only,
                # read-only, strict _DEFAULT_CSP (JSON is not executed).
                self._send_json(HTTPStatus.OK, build_gates(root))
                return
            if path == "/api/risk-register":
                selected = self._root_selection(self._request_params())
                if selected is None:
                    self._bad_root()
                    return
                _root_index, root = selected
                # Client-legible relabel of the /api/attention queue (§4e).
                self._send_json(HTTPStatus.OK, build_risk_register(root))
                return
            if path == "/api/ownership":
                selected = self._root_selection(self._request_params())
                if selected is None:
                    self._bad_root()
                    return
                _root_index, root = selected
                # Ownership & Accountability Map (§4d).
                self._send_json(HTTPStatus.OK, build_ownership(root))
                return
            if path == "/api/learning":
                # Lessons + wrapper exposure telemetry for the selected root.
                # Carries knowledge-note text, never raw bus message bodies.
                self._handle_learning_get()
                return
            if path == "/api/onboarding":
                # Project/codebase onboarding runs: bounded summaries plus
                # evidence pointers, never raw bus message bodies.
                self._handle_onboarding_get()
                return
            if path == "/api/lead-chat":
                selected = self._root_selection(self._request_params())
                if selected is None:
                    self._bad_root()
                    return
                _root_index, root = selected
                # Dedicated operator<->lead transcript. Carries bodies, but only
                # from the stable lc-* thread between the configured principals.
                self._send_json(HTTPStatus.OK, build_lead_chat(root))
                return
            if path == "/api/threads":
                self._handle_threads_get()
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
                selected = self._root_selection(self._request_params())
                if selected is None:
                    self._bad_root()
                    return
                _root_index, root = selected
                thread = build_thread(root.store, rid)
                if thread is None:
                    self._send_json(HTTPStatus.NOT_FOUND,
                                    {"error": "thread not found"})
                    return
                thread["root"] = root.label
                thread["root_info"] = _root_info(root)
                thread["target_root_project_id"] = root.store.project_id()
                self._send_json(HTTPStatus.OK, thread)
                return
            if path == "/api/messages":
                self._send_json(HTTPStatus.OK, messages_payload(store))
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
                enable_actions: bool = False,
                ) -> ThreadingHTTPServer:
    """Build (but do not start) a dashboard HTTP server.

    Refuses to bind to anything but a loopback host
    (:data:`LOOPBACK_HOSTS`). There is no opt-in to bind elsewhere:
    the dashboard has no auth, renders un-sandboxed message bodies,
    and is only safe for the local user. Use an SSH tunnel if you
    need to view it from another machine.

    ``extra`` (0.17.0, additive): further roots to watch alongside
    ``store``. ``store`` is always root[0] for legacy routes; Team Console
    routes may select any root by project ID. ``/api/state`` and
    ``/dashboard`` cover all roots. Omitted → exactly the historical
    single-root behavior. Duplicate project IDs are rejected because no
    selector can distinguish two descriptors for the same store.

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
    handler_cls = _make_handler(roots, enable_actions=enable_actions)
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
          enable_actions: bool = False,
          on_ready: Callable[[str], None] | None = None) -> None:
    """Start the dashboard and block until interrupted.

    ``port=0`` asks the OS for an ephemeral port. The actual bound
    port is announced via ``on_ready(url)`` and is also available as
    ``server.server_address[1]`` if the caller wraps this manually.
    """
    srv = make_server(store, host, port, quiet=quiet, extra=extra,
                      enable_actions=enable_actions)
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
                    enable_actions: bool = False,
                    ) -> tuple[ThreadingHTTPServer, threading.Thread, str]:
    """Start the dashboard on a daemon thread (used by tests).

    Returns ``(server, thread, base_url)``. Caller is responsible for
    ``server.shutdown()`` then ``server.server_close()``. ``base_url``
    has no trailing slash so callers can append ``/messages/<id>``
    etc. directly.
    """
    srv = make_server(store, host, port, extra=extra,
                      enable_actions=enable_actions)
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
    "build_intents",
    "build_learning",
    "build_lead_chat",
    "build_onboarding",
    "build_preflight",
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
