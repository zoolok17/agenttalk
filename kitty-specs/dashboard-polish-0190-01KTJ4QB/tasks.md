# Tasks: Dashboard Polish (agenttalk 0.19.0)

**Mission**: `dashboard-polish-0190-01KTJ4QB` | **Branch**: plan + merge on `master`
**Input**: spec.md (FR-001..008 / NFR-001..003 / C-001..008), plan.md, research.md (D1–D7), data-model.md, contracts/api-surface.md, quickstart.md
**Pre-code review**: Codex approved (08f126ef) — note: always-present sent/received/edges applies to HEALTHY roots only; degraded roots keep errors-as-data.

## Subtask Index

| ID | Description | WP | Parallel |
|----|-------------|----|----------|
| T001 | Additive `sent`/`received` per agent in `_agent_entries` (one pass over msgs) | WP01 | — | [D] |
| T002 | Additive `edges` + `edges_truncated`/`edge_limit` in `_root_state` (Counter, self-excl, fan-out-incl, top-50) | WP01 | — | [D] |
| T003 | `render_dashboard` shell: control bar (Refresh button + auto-refresh toggle) + roster/conversation containers | WP01 | — | [D] |
| T004 | `_DASHBOARD_JS` renderer: hierarchical roster (client-side role classification), agent cards (incl client-computed owes), conversation panel | WP01 | — | [D] |
| T005 | `_DASHBOARD_JS` refresh controls: addEventListener wiring, toggle starts/clears interval, button one-shot, no reload | WP01 | — | [D] |
| T006 | Tests: `/api/state` additive keys (sent/received/edges/truncation), schema_version still 1, no key removed, no body, healthy-vs-degraded | WP01 | — | [D] |
| T007 | Tests: CSP byte-identical (incl /dashboard), read-only no-mutation still passes, JS has no inline handlers/innerHTML, role-classifier convention | WP01 | — | [D] |
| T008 | README dashboard section refresh (stats/edges/controls) | WP02 | — | [D] |
| T009 | CHANGELOG 0.19.0 + ROADMAP header + version bump pyproject/`__init__` + full-suite gate | WP02 | — | [D] |

## Work Packages

### WP01 — Server stats + dashboard renderer

**Prompt**: [tasks/WP01-stats-and-renderer.md](tasks/WP01-stats-and-renderer.md)
**Priority**: P1 (the whole feature)
**Goal**: All `src/agenttalk/web.py` changes (additive `/api/state` stats + the `_DASHBOARD_JS` renderer + `render_dashboard` shell) and complete `tests/test_web.py` coverage.
**Independent test**: `python -m pytest tests/test_web.py -q` green; quickstart §§1–4 manually.
**Estimated**: ~620 lines (7 subtasks — at the upper bound by design: web.py is a single-owner file, server + client land together, the 0.17.0 pattern).

- [x] T001 Additive `sent`/`received` per agent (WP01)
- [x] T002 Additive `edges` + truncation per root (WP01)
- [x] T003 `render_dashboard` shell: control bar + containers (WP01)
- [x] T004 `_DASHBOARD_JS`: hierarchical roster + cards + conversation panel (WP01)
- [x] T005 `_DASHBOARD_JS`: refresh controls (WP01)
- [x] T006 Tests: additive `/api/state` keys + healthy/degraded (WP01)
- [x] T007 Tests: CSP/no-mutation/textContent/role-classifier (WP01)

**Implementation sketch**: server delta is small and additive (sent/received
from the msgs already passed to `_agent_entries`; edges from one Counter in
`_root_state`); the bulk is the `_DASHBOARD_JS` rewrite (hierarchical layout,
cards, conversation panel, refresh controls) — all DOM via createElement/
textContent, controls via addEventListener. Degraded roots keep errors-as-data
(Codex note) — sent/received/edges only on healthy roots.
**Risks**: additivity gate (extend exact-key tests, don't rewrite); CSP
regression (no inline handlers / innerHTML); layout misclassification (pin the
convention).
**Dependencies**: none.

### WP02 — Docs + version

**Prompt**: [tasks/WP02-docs-release.md](tasks/WP02-docs-release.md)
**Priority**: P2 (final WP)
**Goal**: README dashboard section, CHANGELOG 0.19.0, ROADMAP header, version 0.19.0.
**Independent test**: `python -m agenttalk --version` → 0.19.0; doc claims match shipped behavior; full suite green.
**Estimated**: ~200 lines (2 subtasks).

- [x] T008 README dashboard section refresh (WP02)
- [x] T009 CHANGELOG + ROADMAP + version 0.19.0 + full-suite gate (WP02)

**Dependencies**: Depends on WP01 (documents the shipped dashboard surface).

## Execution notes

- Single serial lane WP01 → WP02 (web.py is single-owner; WP02 documents it).
  Non-overlapping owned_files.
- Per-WP Codex cross-review (C-007); fresh-eyes before tag; CI matrix green
  before tag (NFR-003).
- Dev gotcha: `pip install -e .` inside the worktree before testing; re-point
  to main on merge.
- MVP: WP01 alone is the operator-facing feature.
