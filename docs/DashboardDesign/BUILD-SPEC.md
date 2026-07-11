# Team Console — Build Spec & Frozen Wire Contract (v0.58.0)

This is the implementation contract for recreating the **Team Console** design
(`design_handoff_team_console/`) in agenttalk. It freezes the JSON wire shapes so
the backend (`web.py`), the CSS (`console.css`), and the JS (`console.js`) can be
built in parallel and integrate cleanly.

**Scope of v0.58.0: the full 5-view console, READ-ONLY.** No write-actions
(Restart / defer / dismiss / requeue) ship in this release — those come in v0.59.0
behind `--enable-actions` + a CSRF token. In v0.58.0 the action buttons render
**disabled** with a `title="run via the agenttalk CLI"` hint. **Inspect** (client-side
navigation to Agent Detail) DOES work — it is not a write.

**Fonts: system stack.** No Google Fonts, no remote assets, no `@font-face` url().
Use: headings/wordmark → `"Segoe UI", system-ui, sans-serif`; mono/IDs → `ui-monospace,
"Cascadia Code", Consolas, monospace`. This keeps `default-src 'none'` intact (no
`font-src` needed) and matches the design's typographic *hierarchy* if not the exact
letterforms.

> **Superseding addendum — shipped in v0.74.0 on 2026-07-11.** This file remains the
> frozen v0.58.0 contract. The following current contract supersedes only its
> root-routing and client root-state statements (especially §3c and §7):
>
> - Every watched root has a stable path-derived `project_id`. Display labels
>   are not canonical or write-routing keys; their only routing use is the
>   unique-match GET compatibility below. Duplicate basenames receive stable
>   project-id suffixes independent of root-list order.
> - Selected-root GET endpoints accept `?root=<project_id>`, map omission to
>   `root[0]`, and may retain a unique display label as a legacy best-effort
>   selector. Blank, repeated, unknown, or ambiguous GET selectors return HTTP
>   400 `bad_root`. Multi-root POST `/api/intent` and `/api/lead-chat`
>   require exactly one explicit full project id; labels and omission are
>   forbidden, while single-root POST may omit it. Unknown, blank, repeated,
>   ambiguous, or non-full write selectors return HTTP 400 `bad_root` before
>   mutation.
> - Every selected-root response carries
>   `root_info: {project_id, label, path}` (or the equivalent target project id
>   in the action envelope), so clients can validate routing before applying a
>   response.
> - Frontend state uses `selectedRootId`, not an array index. The top bar keeps
>   project and path context visible, including in single-root mode. CSS may
>   ellipsize the path visually; the full value remains available through the
>   text, title, and accessibility surfaces. The document title includes
>   project and view.
> - A root switch clears root-bound drill-ins, transcript/learning/onboarding
>   caches, queued answers, archived state, and action-session state. Each
>   asynchronous request is bound to the selected project plus a local root
>   generation; late or mismatched responses are discarded.
>
> `project_id` is a routing and stale-response invariant, not authentication or
> cross-root isolation. All original v0.58.0 wire statements below remain
> historical evidence of that release.

---

## 0. Non-negotiable invariants (any change that breaks these is wrong)

These are enforced by `tests/test_web.py`. Preserve them; where a change is required,
it is called out explicitly below.

1. **Loopback-only bind + per-request peer 403** — unchanged. `make_server` refuses
   non-loopback hosts; every method 403s a non-loopback peer first.
2. **Read-only by regression** — only `do_GET`/`do_HEAD` route; every write method →
   405 `Allow: GET, HEAD`. `test_no_mutation_full_tree_hash` proves the `.agenttalk/`
   tree is byte-identical after a full route sweep. **No new code path may write to
   disk.** (The health-timeline ring is IN-MEMORY only — never a file.)
3. **`/api/state` carries NO message `body`, anywhere** — `_assert_no_body_keys` walks
   the whole payload. Bodies live ONLY on the dedicated `/api/thread/<rid>` route.
4. **CSP split by route** — hostile-body / legacy routes keep `_LEGACY_CSP`
   (`default-src 'none'`, no script). The console + its JSON feeds get the
   script-capable policy. **CHANGE (this release): the console CSP drops
   `style-src 'unsafe-inline'` and gains `style-src 'self'`** because CSS moves to a
   served file. New constant value below; update the test constants to match.
5. **Absent-not-null** — an agent/thread field that is unknown is OMITTED, never
   `null`. (`test_api_state_absent_not_null`.)
6. **Additive schema** — `schema_version` stays `1`; no existing key removed/renamed;
   new keys additive. (`test_api_state_additive_keys_no_removal`.)
7. **Degraded root = errors-as-data with NO partial fields** — a root whose collection
   throws yields `{label, path, errors:[...]}` only. All new fields go INSIDE the
   existing `try` in `_root_state`. (`test_api_state_corrupt_root_isolated`.)
8. **DOM built via `createElement`/`textContent` — NEVER `innerHTML`.** Message bodies
   and all bus-derived strings (subjects, names, tasks) are untrusted. `textContent`
   under `script-src 'self'` is safe (no parse, no execution). `console.js` must
   contain `textContent` and must NOT contain `innerHTML` or `location.reload`.
   (Re-anchored `test_console_renderer_safety`.)
9. **Perf: one disk scan per root per `/api/state`** — reuse `_validated_for_state`'s
   single `_scan_messages()` pass; do NOT add per-agent re-scans. (`test_api_state_perf_smoke`, <2s @ 1k msgs.)

---

## 1. CSP constants (web.py) — new values

```
_DEFAULT_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src 'none'; frame-ancestors 'none'"   # UNCHANGED — legacy/hostile-body routes
_DASHBOARD_CSP = "default-src 'none'; script-src 'self'; connect-src 'self'; style-src 'self'; img-src 'none'; frame-ancestors 'none'"   # CHANGED: 'unsafe-inline' -> 'self'
```

Routes and their CSP:
- `_DEFAULT_CSP`: `/`, `/messages/<id>`, `/api/status`, `/api/messages`,
  `/api/messages/<id>`, `/api/state`, `/api/attention`, `/api/thread/<rid>`
  (JSON is not executed as a document; keep it on the strict policy).
- `_DASHBOARD_CSP`: `/dashboard` and `/console` ONLY (the HTML documents that load
  `script-src 'self'` + `style-src 'self'` assets).

The console HTML shell must have **zero** inline `<style>` and **zero** inline event
handlers (`onclick=`, `onchange=`, `style=`). All styling via `<link rel="stylesheet"
href="/static/console.css">`; all behavior via `<script src="/static/console.js">`.

---

## 2. Static asset serving (web.py)

Add a package-data dir `src/agenttalk/web_static/` containing `console.css` and
`console.js`. Serve via an **allowlisted-filename** route — NEVER a `pathlib` join on
request input (preserve the traversal guarantee):

```
_STATIC_ASSETS = {
    "console.css": ("text/css; charset=utf-8", <bytes>),
    "console.js":  ("application/javascript; charset=utf-8", <bytes>),
}
```

Load the bytes at import/startup from the package dir (e.g.
`(Path(__file__).parent / "web_static" / name).read_bytes()`). Route
`/static/<name>` → exact dict lookup; unknown name → 404. Keep the existing
`/static/dashboard.js` behavior only if `/dashboard` is retained (see §6).

Ship these as real files (editable, lintable) — do NOT inline a 2000-line JS string in
`web.py`.

---

## 3. `/api/state` — additive fields (schema_version stays 1)

All additions are OMITTED when not determinable (absent-not-null). All go inside the
existing `try` in `_root_state`. No body text anywhere.

### 3a. Per-agent (`roots[].agents[]`) — extend `_agent_entries`
- `cli`: `"claude" | "codex"` — from `health.cli` if fresh, else infer from capacity
  snapshot `source`, else from the agent-name prefix (`name.split('-')[0]`). Omit if
  none determinable.
- `capacity`: object, present only if a capacity snapshot exists:
  `{ "rate_used_pct": number|null, "context_used_pct": number|null,
     "confidence": "fresh"|"stale"|"unknown" }`. Read via `store.read_capacity(agent)`
  + `capacity.effective_confidence`. `null` percents are allowed INSIDE this object
  (a snapshot may carry only one of the two signals) — the absent-not-null rule is
  about the `capacity` key itself.
- `wrapped`: bool — present when determinable (health `mode == "wrapped·loop"`, or the
  agent is supervisor-managed / in a lead-loop). Omit if unknown.
- `restartable`: bool — mirrors `wrapped` for v1 (only wrapped agents are restartable).
  Omit if `wrapped` is unknown.
- `owned_domains`: array, present only if the agent owns ≥1 domain:
  `[{ "name": string, "globs": [string, ...] }]`. Invert `domains.load_registry` via
  `resolve_refset(owners)` per domain, grouped by agent. Reuse the resolution logic
  from `cli._roster_expertise`. This is a per-ROOT read done once (load registry once,
  build an agent→domains map), NOT a per-agent registry reload — keep the single-scan
  discipline.
- `task`: string — a synthesized "current work" line (the design's card row 3 / Agent
  Detail current-work). Derivation, first match wins: (a) the subject of the agent's
  newest non-terminal open thread where it is `next_owner`; else (b) `mission`+`wp_id`
  from such a thread ("mission · <m> · <wp>"); else (c) if composing, "composing a
  reply to <peer>"; else omit. This is envelope-derived (subjects already ship in
  `threads[]`), rendered via `textContent`.
- `health_timeline`: array (BEST-EFFORT, in-memory ring — see §5), present only when
  the server has accumulated ≥1 sample for this agent:
  `[{ "state": <health-state-enum>, "seconds": number }]` — contiguous segments over
  roughly the last 30 minutes, oldest→newest. Omit entirely if no samples (client
  shows a "building history…" placeholder). If implementing the ring risks ANY core
  invariant, OMIT this field for v1 and leave the client placeholder — it is the one
  explicitly-optional piece.

### 3b. Per-thread (`roots[].threads[]`) — extend `_derive_root_threads`
- `verdict`: string — the latest decision on the thread, from the newest
  `review-result` (`meta.status`, e.g. `"approved"`) or `proposal-response`
  (`meta.status` ∈ `accepted|rejected|countered`) or a gate marker. Map to the design's
  chips client-side (approved/GO→ok, HOLD/rejected→danger, countered→violet). Read only
  `meta.status` (safe — never body). Omit if the thread has no decision yet.
- `active_review`: bool — `true` when `opener_kind ∈ {review-request, proposal}` AND the
  thread is non-terminal. Drives the dashed/animated graph edge. Omit when false (or
  emit only when true — absent-not-null).

### 3c. Root-level
- No new required root keys. `spec_kitty.missions` stays as-is (mission pill shows the
  mission name; the "x/y" fraction is intentionally NOT shown — no faithful source).

---

## 4. New read-only endpoints

### 4a. `GET /api/attention` — the ranked "needs a human" queue
Single-team: covers `roots[0]`. (If you want multi-root, accept `?root=<label>` and
default to root[0]; v1 may be root[0]-only.) CSP `_DEFAULT_CSP`. GET/HEAD only; POST→405.

Payload:
```
{
  "root": "<label>",
  "items": [
    {
      "id": string,
      "source": "escalation"|"gate"|"stuck"|"deadletter"|"supervisor",
      "source_label": "ESCALATION"|"GATE HOLD"|"STUCK"|"DEAD LETTER"|"SUPERVISOR",
      "severity": "high"|"med"|"low",
      "title": string,           // envelope-derived; NEVER raw message body
      "agent": string|null,      // the agent the item concerns
      "detail": string,          // short human line; envelope-derived
      "age_seconds": number,
      "human_can_unblock_now": bool
    }, ...
  ],
  "count": number
}
```
Build from `attention.build_queue` (escalations / gate HOLD / dead-letter / lead-unarmed)
PLUS a derived **STUCK** item per agent whose `health.state == "stuck_suspected"` (stuck
is NOT one of build_queue's sources). Severity map: escalation=high, gate=high,
stuck=med, deadletter=med, supervisor=low. **Verify `build_queue` output carries no raw
message-body prose**; if any field could, strip/truncate to an envelope summary. This
endpoint is READ-ONLY — it lists items; it does not dispose them (no writes in v1).

### 4b. `GET /api/thread/<rid>` — one thread's full transcript (CARRIES BODIES)
CSP `_DEFAULT_CSP` (JSON, not executed). GET/HEAD only; POST→405. Validate `<rid>`
against `_MESSAGE_ID_RE` (`[A-Za-z0-9_.\-]{1,128}`) BEFORE any disk touch (traversal
parity). Unknown rid / no messages → 404 JSON `{"error": "thread not found"}`.

Messages are the validated set (`_validated_for_state` surface: roster + kind + HMAC
when enforced) whose `meta.request_id == <rid>`, ordered by id ascending.

Payload:
```
{
  "request_id": string,
  "subject": string,             // opener subject (envelope)
  "participants": [string, ...], // distinct senders+recipients, in first-seen order
  "kind": string,                // opener kind
  "messages": [
    {
      "id": string,
      "from": string,
      "to": string,
      "kind": string,
      "ts": string,
      "age_seconds": number,
      "cli": "claude"|"codex"|null,   // inferred from `from` prefix
      "body": string,            // RAW body — NOT html-escaped. Client renders via textContent.
      "meta_line": string        // a SAFE, pre-formatted meta summary, e.g.
                                 // "status=approved · head 7b2d9c1" — derive from a
                                 // whitelist of meta keys (status, head, base). Do NOT
                                 // dump arbitrary meta (may contain body-ish text).
    }, ...
  ]
}
```
Ship `body` RAW (JSON transport). The client MUST render it with `textContent` (never
`innerHTML`, never a markdown parser — pre-wrap text only). Do NOT html-escape the body
in the JSON (that would double-escape under `textContent`).

---

## 5. Health-timeline ring (server-side, in-memory, best-effort)

Owned by the server instance (created in `_make_handler`), NOT a module global (avoids
cross-test leakage) and NOT a file (read-only invariant). On each `/api/state` build,
record `(now, agent, health.state)` per agent into a per-(root,agent) deque, prune
entries older than ~30 min, and collapse contiguous same-state samples into
`{state, seconds}` segments for the payload. `build_state(roots)` stays PURE by default
(the perf test and many unit tests call it directly and assert exact key sets) — pass
the ring in only from the `/api/state` route handler (e.g.
`build_state(roots, history=self.server._health_history)`), and when `history is None`
emit no `health_timeline`. If this entangles any invariant, DROP the field for v1.

---

## 6. `/dashboard` vs `/console`

The Team Console REPLACES the basic dashboard. Serve the console at **`/dashboard`**
(natural home; the index page already links there) with the new 3-region shell + the
served `console.css`/`console.js`. Remove the old `_DASHBOARD_JS` string constant and
the old obligation-table renderer; remove the `/static/dashboard.js` route and its
0.58.0-WIP additions to `_PAGE_CSS`. Keep `/`, `/messages/<id>`, `/api/messages`,
`/api/status`, `/api/state` exactly as they are (minus the additive `/api/state`
fields above). Migrate the tests that referenced `/static/dashboard.js` and
`_DASHBOARD_JS` to the new asset (see §8).

The shell HTML (`render_dashboard`) becomes a fixed skeleton:
```
<div id="app">
  <header id="topbar">…wordmark, mission pill, live clock, operator chip (all built/hydrated by JS)…</header>
  <div id="body">
    <nav id="sidebar">…VIEWS nav + status legend (JS-hydrated)…</nav>
    <main id="main"><!-- active view rendered here by console.js --></main>
  </div>
</div>
<noscript>…poll /api/state directly…</noscript>
<link rel="stylesheet" href="/static/console.css">
<script src="/static/console.js"></script>
```
Only operator-supplied labels (root labels) may be interpolated server-side, escaped.
Everything dynamic is client-rendered. No inline styles/handlers.

Multi-root reconciliation: the console renders ONE root at a time. Default root[0]. If
`state.roots.length > 1`, JS shows a project switcher in the top bar that sets a
`selectedRoot` client-state index; all views render the selected root.

---

## 7. Frontend behavioral contract (console.js + console.css)

Follow `design_handoff_team_console/README.md` and the extracted contract in the team's
notes for pixel/behavior fidelity. Key points:

- **Client state:** `{ view: 'overview'|'flow'|'attention'|'sessions'|'agent',
  selectedAgent: name|null, sessionRid: string|null, filter:
  'all'|'working'|'idle'|'attention', selectedRoot: int, now: ms }`. Prefs (persist in
  `localStorage`): `theme` (light|dark), `accent` (5 options), `density`
  (comfortable|compact).
- **Theme/accent/density** via CSS variables + a `data-theme` / `data-accent` /
  `data-density` attribute on `#app` (or `<html>`). Accent options as predefined
  var sets in CSS (`[data-accent="blue"] { --accent: #4457E6 } …`). Setting attributes
  from JS is fine under `style-src 'self'` (no inline `<style>`, no `style=` attr).
- **Poll loop:** `fetch('/api/state')` every ~2s (`POLL_MS`); re-render the active view
  in place (preserve scroll). Recompute all relative ages client-side each tick from
  timestamps so counters feel live even between polls (a 1s clock tick + a 2s data poll).
  Sessions view additionally fetches `/api/thread/<rid>` when a thread is opened;
  Attention view fetches `/api/attention`.
- **Status vocabulary** (health-state → label/color), reuse the existing `stateInfo`
  mapping and the design's `_sm` table: working_turn→"Working"(ok, pulsing dot),
  working_silent→"Working · quiet"(info), idle_waiting→"Idle · waiting"(warn),
  stuck_suspected→"Stuck?"(attn), rate_limited_or_outage→"Rate-limited"(danger),
  degraded_output→"Degraded"(danger), crashed_or_exited→"Exited"(neutral),
  errored_*→(danger/attn), unknown→"Unknown"(gray, "no hb").
- **Kind chips** (`_km`): review-request→accent, review-result→ok, proposal/-response→
  violet, question→info, note/message/reply/end→neutral, wake→teal, escalate→danger,
  broadcast→info, gate→danger.
- **5 views**: Team overview (stat tiles + filter chips + agent grid + live-activity
  rail from `roots[].recent`), Conversations (640×480 SVG circle graph from
  `roots[].edges`, center 320,240 r178, −90°+i·36°, edge width `w*1.4+0.6`, dashed
  animated edge where a thread `active_review` connects the pair; + Active-threads list
  from `threads[]`), Attention (ranked cards from `/api/attention`; action buttons
  DISABLED with CLI hint; Inspect works), Sessions (thread list + transcript bubbles
  from `/api/thread/<rid>`, bodies via `textContent` pre-wrap, system events wake/end as
  separators), Agent Detail (header + current-work tags + health timeline + recent
  messages + capacity meters + supervisor card + owned domains).
- **Meters** (rate/ctx): threshold color `<60 ok / 60–84 warn / ≥85 danger`, 2% minimum
  fill, `.5s` width transition.
- **Animations:** `liveDot` (opacity 1→.32→1, 1.8s), `dashmove` (dashoffset→−24, 1s),
  `fadeInUp` (translateY 7px + fade, .45s) on fresh feed items.
- **Empty/degraded states:** Attention empty → "All clear". Sessions with no transcript
  → a real "no transcript" empty state (do NOT fall back to another thread — that was a
  prototype shortcut). Degraded root (errors) → show the root's error line, not a crash.
- **CSP-safe:** all DOM via `createElement`/`createElementNS` (for SVG) + `textContent`.
  No `innerHTML`, no `eval`, no inline handlers, no `location.reload`.

---

## 8. Test migration (test_web.py)

- Update `_DASH_CSP` constant to the new value (§1). Update `test_csp_split_per_route`
  to assert the new console CSP on `/dashboard` (+`/console` if added) and `_LEGACY_CSP`
  on `/api/attention` and `/api/thread/<rid>`.
- `test_dashboard_html_and_root0_only_link_policy` / `test_dashboard_shell_no_inline_handlers`:
  re-anchor to the new shell + `/static/console.js`; assert no inline `<style>`/handlers,
  `console.css`+`console.js` linked.
- Replace `test_dashboard_renderer_controls_and_safety` (which reads `web._DASHBOARD_JS`)
  with `test_console_renderer_safety` reading the served `/static/console.js`: assert
  `textContent` present, `innerHTML`/`location.reload`/`eval(` ABSENT, `addEventListener`
  present.
- `test_new_routes_reject_write_methods`: add `/api/attention`, `/api/thread/<rid>`,
  `/static/console.js`, `/static/console.css` → POST 405.
- `test_no_mutation_full_tree_hash`: swap `/static/dashboard.js`→`/static/console.js`,
  add GETs to `/api/attention` and a valid `/api/thread/<rid>`; assert tree still
  byte-identical (proves the attention/thread/timeline paths never write).
- NEW positive tests: `/api/state` per-agent `capacity`/`cli`/`wrapped`/`owned_domains`/
  `task` present when data exists & absent otherwise; per-thread `verdict`/`active_review`;
  `/api/attention` shape incl. a derived STUCK item; `/api/thread/<rid>` returns raw
  bodies + a safe `meta_line`, 404s an unknown rid, and REJECTS a traversal `<rid>`;
  `_assert_no_body_keys(state)` still green after the additive fields.
- Keep ALL existing invariant tests green (loopback, 403-every-method, escape, dedup,
  epoch, edges, perf, corrupt-root, absent-not-null, additive-no-removal).

---

## 9. Build ownership (parallel)

- **Owner A — backend:** `src/agenttalk/web.py` + `tests/test_web.py`. Implements §1–§6
  (CSP, static route, /api/state additions, /api/attention, /api/thread, ring) + §8
  test migration. Creates `src/agenttalk/web_static/` (may leave the files to owners
  B/C, but must wire the serving route + package-data inclusion in `pyproject.toml`).
- **Owner B — CSS:** `src/agenttalk/web_static/console.css`. Token system (light+dark +
  5 accents + density), full-viewport shell, all component styles, animations. System
  font stack. No `@font-face` remote.
- **Owner C — JS:** `src/agenttalk/web_static/console.js`. The 5-view app per §7.
  CSP-safe DOM only. Codes to THIS wire contract (§3/§4) — field names are frozen here.

Integration (lead): assemble on the `team-console` branch, run `ruff`, `bandit`,
`python -m pytest tests/test_web.py`, `node --check console.js`, and a live smoke
(`serve_in_thread` + curl the routes), then adversarial review → team cross-review →
gate → ship v0.58.0.
