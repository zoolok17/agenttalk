# Implementation Plan: Dashboard Polish (agenttalk 0.19.0)

**Branch**: `master` (plan + merge target; lane branches from master, squash back)
**Date**: 2026-06-07 | **Spec**: [spec.md](spec.md) | **Issue**: zoolok17/agenttalk#22

## Summary

Presentation polish on the existing read-only dashboard: additive `/api/state`
stats (`sent`/`received` per agent, `edges` per root) computed from the scan
`build_state` already does, plus a richer `/dashboard` client (hierarchical
roster, agent cards, conversation panel, refresh controls) rendered entirely
in the embedded `_DASHBOARD_JS` constant. No new routes, no CSP change, no
mutation, no spec-kitty dependency. Design Codex-accepted (`pp-62742311`).

## Technical Context

**Language/Version**: Python 3.10–3.13 (CI matrix), stdlib only; vanilla browser JS (no build step)
**Primary Dependencies**: none new
**Storage**: existing `.agenttalk/` store(s); read-only
**Testing**: pytest, extending `tests/test_web.py`
**Target Platform**: Windows-first + Linux/macOS (CI matrix)
**Project Type**: single project (`src/agenttalk/`)
**Performance Goals**: `/api/state` < 2 s at 1,000 messages, no extra scan (NFR-002)
**Constraints**: schema_version stays 1 / additive (NFR-001); read-only (C-003); per-route CSP byte-identical (C-004); textContent-only (C-005); renderer stays the embedded constant (C-001); bus-native only (C-008)

## Verified code facts (drive the plan)

- `_agent_entries(store, cfg, msgs, liaison)` (`web.py:763`) **already receives
  the validated `msgs` list** → `sent`/`received` per agent are one pass over
  `msgs` (`from == a` / `to == a`), no new scan. Add additive keys next to
  `unread`.
- `_root_state` (`web.py:808`) also has `msgs` and `threads_rows` → `edges` is
  one `Counter` over `(from, to)` pairs (excluding self), top-50, with the
  truncation signal. No new server field for "owes": the client already
  receives `threads` (each with `next_owner`) and counts owes itself.
- The client is the `_DASHBOARD_JS` string constant + `render_dashboard` shell
  in `web.py` (NOT a standalone file — C-001). `/dashboard` CSP already allows
  `script-src 'self'; connect-src 'self'`; refresh controls use
  `addEventListener` (the existing JS is already inline-handler-free).
- The no-mutation, CSP-split, and additivity tests in `tests/test_web.py` are
  the regression floor; new additive keys must not trip the exact-key/no-body
  assertions (extend, don't rewrite).

## Charter Check

Skipped — no charter at `.kittify/charter/charter.md` (consistent with all prior missions).

## Project Structure

```
kitty-specs/dashboard-polish-0190-01KTJ4QB/
├── plan.md  research.md  data-model.md  quickstart.md
├── contracts/api-surface.md
└── tasks.md            # /spec-kitty.tasks — not this command

src/agenttalk/
├── web.py        # PRIMARY: additive sent/received/edges in build_state;
│                 #   the _DASHBOARD_JS renderer + render_dashboard shell
└── __init__.py   # version 0.19.0
tests/test_web.py  README.md CHANGELOG.md ROADMAP.md pyproject.toml
```

## Work Package shape (Phase 2 preview — single serial lane, non-overlapping files)

- **WP01 — server stats + dashboard renderer.** `src/agenttalk/web.py`,
  `tests/test_web.py`. Additive `sent`/`received` (agent entries) + `edges` +
  truncation (root), all from the existing scan; the hierarchical-roster /
  cards / conversation-panel / refresh-controls client in `_DASHBOARD_JS` +
  the `/dashboard` shell; the full test set. web.py is single-owner, so server
  and client land together (same pattern as the 0.17.0 WP01). ~7–8 subtasks.
- **WP02 — docs + version.** `README.md`, `CHANGELOG.md`, `ROADMAP.md`,
  `pyproject.toml`, `src/agenttalk/__init__.py`. Dashboard section refresh,
  CHANGELOG 0.19.0, ROADMAP header, version bump. Depends on WP01.

## Phase outline

- **Phase 0** (`research.md`): decision records D1–D7 (sent/received placement,
  edges shape + self/broadcast policy + cap, owes-on-client, layout convention
  client-side, refresh-controls + CSP, render-without-innerHTML, additivity
  gate updates).
- **Phase 1** (`data-model.md`, `contracts/api-surface.md`, `quickstart.md`):
  exact additive `/api/state` shapes, the role→column convention table, the
  client interaction contract, validation walkthrough.
- **Phase 2** (`/spec-kitty.tasks`): the 2 WPs; per-WP Codex review (C-007);
  fresh-eyes before tag; CI matrix green before tag (NFR-003).

## Risks

- **Additivity gate trip**: the existing `test_api_state_*` exact-key /
  no-`body` assertions. Mitigation: extend those tests for the additive keys;
  `edges`/`sent`/`received` are not `body` and absent-not-null keeps clean
  shapes stable.
- **CSP regression**: the refresh controls must not introduce inline handlers
  or `innerHTML`. Mitigation: `addEventListener` + `textContent`/`createElement`
  only; the CSP-byte-identical test stays green.
- **Layout convention drift**: client-side role matching could misclassify.
  Mitigation: a single documented convention table (FR-005), pinned by a test
  that drives the classifier with representative roles.
- **Edge count blow-up**: a huge store could produce many pairs. Mitigation:
  top-50 cap + truncation signal; the Counter is O(messages), same scan.

## Complexity Tracking

No charter gates. The only structural note: web.py carries both the server
aggregation and the client renderer (single-owner file), so WP01 is at the
upper subtask bound by design — splitting would create artificial cross-WP
ownership of one file, which the 0.17.0 mission already established as the
wrong trade.
