# Research & Decision Records — Obligation Dashboard (0.17.0)

All decisions resolve against the agreed design in issue #20 (Claude proposal
→ Codex counter → accepted) and the verified current code in
`src/agenttalk/web.py`, `cli.py`, `threads.py`, `store.py`.

## D1 — Refresh mechanism: JS polling with a static script route

**Decision**: The `/dashboard` page loads `/static/dashboard.js` (served from
an embedded string constant in `web.py` — no new files on disk, no path
interpolation). The script `fetch()`es `/api/state` every 2 s and re-renders
the hierarchy client-side. CSP for `/dashboard` only:
`default-src 'none'; script-src 'self'; connect-src 'self'; style-src
'unsafe-inline'; img-src 'none'; frame-ancestors 'none'`. Every other route —
including `/messages/<id>`, which renders hostile message bodies — keeps the
existing stricter CSP (`default-src 'none'; style-src 'unsafe-inline'; …`)
byte-identical.

**Rationale**: `<meta http-equiv=refresh>` (zero-JS) would satisfy the ≤3 s
requirement but reloads the whole page every 2 s — scroll position jumps and
the view flickers, which is hostile for a page meant to be stared at. A
fetch loop updates in place. `script-src 'self'` still forbids inline JS
(the protection the module docstring cares about — message bodies cannot
inject script), and the loosened policy applies only to the page that never
renders message bodies.

**Alternatives considered**: meta-refresh (rejected: UX); SSE (rejected in
the proposal round: connection lifecycle complexity for zero win at local
latency); inline `<script>` with nonce (rejected: easier to get wrong than a
static route, and the allowlist already handles routes well).

## D2 — HTML placement: new `/dashboard` route, `/` untouched except a link

**Decision**: The hierarchy view lives at `GET /dashboard`. The existing
index `/` keeps its message-log role and gains one additive link line. The
`agenttalk dashboard` alias prints the `/dashboard` URL on startup;
`agenttalk serve` keeps printing `/`.

**Rationale**: FR-009 requires existing routes keep behavior; replacing `/`
would change what `serve` users see. A separate route also lets CSP differ
per route (D1) without conditionals inside one renderer.

## D3 — CLI shape: `dashboard` alias + repeatable `--store`

**Decision**:
- `agenttalk dashboard [--port N] [--access-log] [--store PATH]...` — new
  subcommand dispatching to the same `cmd_serve` implementation with a
  landing-route flag. No `--host` (binds `127.0.0.1`; the loopback wall does
  not need a knob here).
- `--store PATH` (repeatable, `action="append"`, dest `stores` — deliberately
  distinct from the global `--root` destination) selects the roots. Each PATH
  is a project root containing `.agenttalk/`. No `--store` → the normally
  resolved single root, exactly like `serve`.
- `agenttalk serve` keeps its surface byte-identical (`--host/--port/--quiet/
  --access-log`) and single-root behavior. It serves the new routes too (same
  server), but its startup message is unchanged.

**Rationale**: Codex counter required the alias-not-fork and the separate
parser destination. Putting `--store` only on `dashboard` honors "serve keeps
existing flags and behavior unchanged" in the simplest auditable way.

**Alternatives**: `--root` repeatable on the subcommand (rejected: collides
conceptually with the global `--root`, the exact confusion the counter calls
out); comma-separated single flag (rejected: Windows paths contain commas
rarely but drive-letter colons make custom splitting fragile; argparse
append is idiomatic).

## D4 — Multi-root descriptors: resolve labels at startup, data at request time

**Decision**: At startup, `cmd_serve`/`dashboard` builds an ordered list of
`(path, label)` descriptors: label = directory basename, deduplicated with
`~2`, `~3` suffixes. Paths are checked for existence of `.agenttalk/` at
startup only to WARN (stderr), never to refuse — a root that becomes valid
later starts rendering on the next poll. All store reads happen per request
inside a per-root `try/except`; failures become that root's `errors[]`
(FR-005) and never a 5xx.

**Rationale**: a viewer should observe, not gate. Startup refusal would make
"start dashboard, then init the second project" needlessly order-dependent.

## D5 — Root-level thread rows: dedup by request_id, ball-holder perspective

**Decision**: Per root, `/api/state` derives threads once per roster agent
(`derive_threads(valid_messages, agent=a, cursor=cursor(a),
closed_rids=closed(a))` — the existing pure derivation, unchanged), then
collapses to ONE row per `request_id`:

| situation | chosen perspective | resulting `next_owner` |
|---|---|---|
| some agent's view is non-terminal with `next_owner == that agent` | the ball-holder's own view | that agent (absolute name) |
| only `open-outbound` views exist (everyone waits on a peer/pending set) | the requester's view | the peer / pending list (absolute) |
| all views terminal (`closed`/`closed-superseded`) | the requester's view | absent (terminal rows carry no next_*) |

`next_owner`/`next_action` from `_derive_next` are already absolute agent
names (or a pending list for broadcasts), so the collapsed row needs no
relabeling. Closed threads are EXCLUDED from `/api/state` by default (this
is a *current obligations* view); the row count of closed threads appears in
the root's `counts`.

**Rationale**: rendering both participants' perspectives doubles rows and
forces the client to dedup; the ball-holder rule is precisely the question
the dashboard answers. Reusing `derive_threads` per agent keeps WP scope out
of `threads.py` entirely.

**Alternatives**: a new "observer" derivation in `threads.py` (rejected:
touches the most subtle pure code in the repo for a presentation concern);
emitting per-agent thread lists (rejected: duplication, larger payloads,
client-side merging).

## D6 — `epoch_status` parity with `check --epoch`

**Decision**: Per root, compute `store.current_epoch()` once per request.
Each non-terminal thread row gets `epoch_status` derived from the opener's
`meta.epoch_at_send` with EXACTLY the `check --epoch` three-state semantics:
`current` (no barrier yet, or stamp == current epoch), `previous-epoch`
(stamped with an older id, including null-when-barrier-exists), and
`unknown-pre-epoch` (key absent while a barrier exists). The root object
carries `epoch` = current epoch id or null.

**Rationale**: two different staleness vocabularies on one bus would be a
trap; the CLI's exit-code mapping (0/3/3) stays CLI-only.

## D7 — `generated_at`

**Decision**: `datetime.now(timezone.utc).isoformat()` at aggregate build
time; documented as informational only (no ordering semantics — message ids
remain the only ordering primitive on the bus).

## D8 — Performance budget

**Decision**: one full store scan + one threads derivation per roster agent,
per root, per poll. At the NFR-003 bound (1,000 messages, ≤8 agents) this is
well under 2 s (the same scan every CLI command already does, ~8×). A perf
smoke test asserts the bound with a generated 1,000-message store. No
caching layer in v1 — correctness first; `Cache-Control: no-store` stays.

## D9 — No-mutation regression test design

**Decision**: build two stores with traffic (incl. composing markers and
ack state), snapshot `{relative_path: sha256(content)}` for EVERY file under
both `.agenttalk/` trees, issue ≥10 mixed requests (`/api/state`,
`/dashboard`, `/`, `/messages/<id>`, 404s, and a POST→405), re-snapshot,
assert dict equality. Explicitly NOT directory mtimes (unreliable on
Windows, Codex counter requirement). Cursors and thread/ack state files are
covered automatically by the full-tree walk.

## D10 — Bind-failure handling (FR-010, live repro 2026-06-07)

**Decision**: `cmd_serve` (both spellings) wraps server construction in
`except OSError as e:` → stderr message naming the failed `host:port`,
stating the likely cause ("another program is already listening on this
port"), and the remedies (`--port 0` for an OS-chosen port, or another
`--port`), then `return 2`. The existing `ValueError` (non-loopback host)
path is untouched. Test binds an ephemeral socket first, runs the command at
that port, asserts exit 2 + message content; works on all three OSes.

**Repro that motivated this**: an unrelated local app held `127.0.0.1:8765`;
Windows surfaced the conflict as `WinError 10013` and the raw exception
escaped to the generic top-level handler with no guidance.

## D11 — Security posture extension checklist

Carried over verbatim from `web.py`'s threat model, applied to new routes:

- `make_server` keeps the loopback-host allowlist with **no new flag**; the
  `dashboard` alias cannot select a host at all (D3).
- New routes are added to the same strict allowlist dispatch (`/dashboard`,
  `/static/dashboard.js`, `/api/state`); no path interpolation anywhere new.
- Every do_* method keeps the per-request loopback peer gate.
- `/api/state` emits subjects and derived fields only — never raw bodies
  (FR-003); subjects are JSON-encoded (API) or escaped (HTML).
- The JS is a server-owned constant; `script-src 'self'` (no inline, no
  eval); the dashboard page renders agent names/subjects via
  `textContent`, never `innerHTML` with interpolation.
- Existing routes' headers (incl. CSP) byte-identical — covered by test.
