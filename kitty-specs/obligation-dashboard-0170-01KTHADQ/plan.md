# Implementation Plan: Obligation Dashboard (agenttalk 0.17.0)

**Branch**: `master` (plan and merge target; lanes branch from master and squash back)
**Date**: 2026-06-07 | **Spec**: [spec.md](spec.md) | **Issue**: zoolok17/agenttalk#20

## Summary

Extend the existing read-only local web dashboard (`agenttalk serve`,
`src/agenttalk/web.py`) into a multi-root *obligation* dashboard: a new
`GET /api/state` versioned JSON aggregate, a hierarchical auto-refreshing
HTML view (who is doing what, per root), an `agenttalk dashboard` CLI alias
with a repeatable per-invocation store option, per-root error isolation, and
actionable bind-failure errors. All security properties of the existing
server (loopback-only with no override, GET/HEAD-only, route allowlist,
escaping + CSP, validation parity with `recv`) are preserved and extended to
the new routes. Design agreed in issue #20 (Claude proposal → Codex counter →
accepted).

## Technical Context

**Language/Version**: Python 3.10–3.13 (CI matrix), stdlib only
**Primary Dependencies**: none (stdlib `http.server`, `json`, `html`, `hashlib`)
**Storage**: existing `.agenttalk/` file store(s); the dashboard never writes
**Testing**: pytest; extends `tests/test_web.py` (ephemeral-port threaded server)
**Target Platform**: Windows-first, plus Linux/macOS (CI matrix)
**Project Type**: single project (`src/agenttalk/`)
**Performance Goals**: `/api/state` < 2 s at 1,000 validated messages (NFR-003)
**Constraints**: loopback-only absolute (C-007); zero store writes (C-003);
existing routes byte-compatible (FR-009/NFR-005); additive CLI (C-002)
**Scale/Scope**: ~2–5 roots × ~2–8 agents each; single human viewer

## Charter Check

Skipped — no charter exists at `.kittify/charter/charter.md` (consistent with
all prior missions in this repo).

## Planning Decisions (confirmed)

All planning questions were resolved against the agreed issue #20 design; the
two spec-deferred decisions are settled in `research.md`:

1. **Refresh mechanism** (D1): client-side JS polling of `/api/state` every
   2 s, with the script served from a new allowlisted static route — no
   inline JS; per-route CSP extension only on the dashboard page.
2. **HTML route placement** (D2): new `/dashboard` route; `/` (message log)
   unchanged except an additive link to the dashboard.

## Project Structure

### Documentation (this feature)

```
kitty-specs/obligation-dashboard-0170-01KTHADQ/
├── plan.md              # This file
├── research.md          # Phase 0 — decision records D1–D11
├── data-model.md        # Phase 1 — /api/state schema, root/thread row shapes
├── quickstart.md        # Phase 1 — validation walkthrough
├── contracts/
│   └── cli-surface.md   # Phase 1 — CLI + HTTP surface contract
└── tasks.md             # Phase 2 (/spec-kitty.tasks — not this command)
```

### Source Code (repository root)

```
src/agenttalk/
├── web.py        # PRIMARY: state aggregation, /api/state, /dashboard HTML,
│                 #   /static/dashboard.js, multi-root descriptors, per-route CSP
├── cli.py        # dashboard alias subparser, --store plumbing, serve OSError
│                 #   handling (exit 2 actionable message)
├── threads.py    # UNCHANGED (pure derivation reused as-is)
├── store.py      # UNCHANGED (read-only surfaces reused as-is)
└── __init__.py   # version bump 0.17.0

tests/
├── test_web.py   # /api/state schema, multi-root, error isolation,
│                 #   no-mutation hash test, CSP/route security, perf smoke
└── test_cli.py   # dashboard alias parsing, serve/dashboard bind-failure exit 2

README.md / CHANGELOG.md / ROADMAP.md / SECURITY.md / pyproject.toml  # release docs
```

**Structure Decision**: single-project layout (existing). All new server
logic lives in `src/agenttalk/web.py` (single-owner file, like `cli.py` in
the 0.16.0 mission); `cli.py` gets only argparse wiring + error handling.
`store.py`/`threads.py` are deliberately untouched — the dashboard composes
their existing pure read surfaces.

## Architecture Sketch

```
agenttalk dashboard --store D:\proj\a --store D:\proj\b --port 8765
        │
        ▼
cli.cmd_serve(args)  ── builds [RootDescriptor(path,label), ...]
        │                      (no --store → the normally-resolved single root)
        ▼
web.make_server(roots, host=127.0.0.1, port)   # same loopback wall, refactored
        │                                       # to accept 1..N descriptors
        ├── GET /                → existing index (root[0]) + link → /dashboard
        ├── GET /messages/<id>   → existing detail (root[0])        [unchanged]
        ├── GET /api/status      → existing payload (root[0])       [unchanged]
        ├── GET /api/messages    → existing payload (root[0])       [unchanged]
        ├── GET /dashboard       → hierarchy HTML (all roots), CSP+script-src 'self'
        ├── GET /static/dashboard.js → embedded-string JS, 2 s fetch loop
        └── GET /api/state       → build_state(roots):
                                     per root (isolated try/except → errors[]):
                                       Store(path) → load_config / roster views
                                       valid_messages() once
                                       derive_threads() per roster agent
                                       dedup by request_id (ball-holder rule, D5)
                                       composing intents, cursors/unread,
                                       last_seen, current_epoch + epoch_status
```

Single-root invocations of plain `serve` behave exactly as today (root[0] ==
the resolved root); existing routes always bind to root[0] so their behavior
and shapes are unchanged even under multi-root (FR-009/NFR-005).

## Phase Outline

- **Phase 0** (`research.md`): decision records D1–D11 (refresh, route
  placement, CLI shape, multi-root descriptors, thread dedup rule,
  epoch_status parity, timestamps, perf budget, no-mutation test design,
  bind-error handling, security posture extension).
- **Phase 1** (`data-model.md`, `contracts/cli-surface.md`, `quickstart.md`):
  exact `/api/state` schema (schema_version 1), root object and thread row
  field lists, CLI flags, HTTP route table with per-route CSP, exit codes,
  validation walkthrough.
- **Phase 2** (`/spec-kitty.tasks`): expected ~3 WPs, single serial lane,
  file-level non-overlapping ownership — WP01 `web.py`+`test_web.py` (server
  core), WP02 `cli.py`+`test_cli.py` (wiring + bind errors), WP03 release
  docs + version. Per-WP Codex cross-review over agenttalk (C-006).

## Risks

- **CSP regression**: loosening CSP on one route must not leak to message
  detail pages (which render hostile bodies). Mitigation: per-route header
  selection + explicit test that `/messages/<id>` CSP is byte-identical.
- **Perspective confusion in dedup** (D5): a thread derived from two agents'
  perspectives must collapse to one row with an absolute `next_owner`.
  Mitigation: dedup rule recorded in research.md with a table; tests cover
  both directions + broadcast pending case.
- **Multi-root path traversal**: `--store` paths come from the operator (not
  the network) and the route allowlist has no path interpolation; still,
  root labels are escaped and `/api/state` never echoes query input.
- **Windows file URLs / drive letters in labels**: labels derive from
  directory names, deduped with numeric suffixes; full paths only in JSON
  values (escaped in HTML).

## Complexity Tracking

No charter gates to violate; no exceptions requested. The only structural
change is `make_server` accepting multiple root descriptors — kept
backward-compatible for `serve`.
