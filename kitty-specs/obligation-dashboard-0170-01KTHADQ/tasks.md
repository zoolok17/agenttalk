# Tasks: Obligation Dashboard (agenttalk 0.17.0)

**Mission**: `obligation-dashboard-0170-01KTHADQ` | **Branch contract**: plan on `master`, merge into `master`
**Input**: spec.md (FR-001..010, NFR-001..005, C-001..007), plan.md, research.md (D1–D11), data-model.md, contracts/cli-surface.md, quickstart.md
**Pre-code design review**: Codex approved rev2 (8e81ace) — root[0]-only detail links, composing array, NFR-002 per-surface.

## Subtask Index

| ID | Description | WP | Parallel |
|----|-------------|----|----------|
| T001 | Multi-root server plumbing: descriptors, label dedup, backward-compatible `make_server` | WP01 | — |
| T002 | `build_state()` aggregate: per-root isolated collection (roster, presence, composing, epoch, counts, spec-kitty detection) | WP01 | — |
| T003 | Thread rows: per-agent derivation, D5 ball-holder dedup, D6 epoch_status, mission/wp_id, broadcasts projection | WP01 | — |
| T004 | New routes: `/api/state`, `/dashboard` HTML, `/static/dashboard.js`, per-route CSP, additive index link | WP01 | — |
| T005 | Tests: `/api/state` schema contract (version pin, absent-not-null, composing array, no body keys) | WP01 | — |
| T006 | Tests: multi-root separation, corrupt-root isolation, root[0]-only hrefs | WP01 | — |
| T007 | Tests: security invariants (CSP pinning, peer gate, 405s, loopback) + no-mutation hash + perf smoke | WP01 | — |
| T008 | `dashboard` subparser + `--store` plumbing + shared dispatch | WP02 | — |
| T009 | Bind-failure handling (`OSError` → exit 2 actionable) + startup messages per spelling | WP02 | — |
| T010 | CLI tests: alias surface, `--host` unknown option, `--store` plumbing, bind-failure exit 2, `serve` unchanged | WP02 | — |
| T011 | README: dashboard/serve docs, CLI reference rows, install pin bumps | WP03 | — |
| T012 | SECURITY.md dashboard section + CHANGELOG 0.17.0 + ROADMAP refresh | WP03 | — |
| T013 | Version bump to 0.17.0 (pyproject.toml, `__init__.py`) + full-suite gate | WP03 | — |

## Work Packages

### WP01 — Server core: multi-root state aggregate + dashboard routes

**Prompt**: [tasks/WP01-web-state-aggregate.md](tasks/WP01-web-state-aggregate.md)
**Priority**: P1 (foundation — everything else builds on this)
**Goal**: All new server-side behavior in `src/agenttalk/web.py` plus its complete test coverage in `tests/test_web.py`.
**Independent test**: `python -m pytest tests/test_web.py -q` green; quickstart §§1–5 pass manually via `web.serve_in_thread` equivalents.
**Estimated prompt size**: ~640 lines (7 subtasks — at the upper bound deliberately: web.py and test_web.py are single-owner files, so splitting would create artificial cross-WP file ownership).

- [ ] T001 Multi-root server plumbing: descriptors, label dedup, backward-compatible `make_server` (WP01)
- [ ] T002 `build_state()` aggregate: per-root isolated collection (WP01)
- [ ] T003 Thread rows: per-agent derivation, D5 dedup, D6 epoch_status, broadcasts (WP01)
- [ ] T004 New routes `/api/state`, `/dashboard`, `/static/dashboard.js`, per-route CSP (WP01)
- [ ] T005 Tests: `/api/state` schema contract (WP01)
- [ ] T006 Tests: multi-root separation, corrupt-root isolation, root[0]-only hrefs (WP01)
- [ ] T007 Tests: security invariants + no-mutation hash + perf smoke (WP01)

**Implementation sketch**: extend `web.py` only. `make_server` keeps its
existing `(store, host, port, *, quiet)` call working byte-for-byte (cli.py
is NOT owned by this WP and must stay green); multi-root arrives via an
additive optional parameter. `build_state()` is the single pure entry point
for `/api/state`. CSP becomes per-route; existing routes' headers stay
byte-identical (pinned by test).
**Risks**: CSP leakage to hostile-body pages; dedup perspective bugs;
accidental `cli.py` breakage via signature change (mitigated: additive
signature + full suite run).
**Dependencies**: none.

### WP02 — CLI wiring: `dashboard` alias + bind-failure handling

**Prompt**: [tasks/WP02-cli-dashboard-alias.md](tasks/WP02-cli-dashboard-alias.md)
**Priority**: P1
**Goal**: The `dashboard` subcommand, `--store` plumbing, and FR-010 bind-error handling in `src/agenttalk/cli.py` + `tests/test_cli.py`.
**Independent test**: `python -m pytest tests/test_cli.py -q` green; quickstart §6 passes.
**Estimated prompt size**: ~330 lines (3 subtasks).

- [ ] T008 `dashboard` subparser + `--store` plumbing + shared dispatch (WP02)
- [ ] T009 Bind-failure handling (`OSError` → exit 2) + startup messages (WP02)
- [ ] T010 CLI tests: alias surface, `--host` rejection, bind failure, `serve` unchanged (WP02)

**Dependencies**: Depends on WP01 (consumes the multi-root `make_server`
surface and `/dashboard` landing route).

### WP03 — Release: docs, security honesty, version 0.17.0

**Prompt**: [tasks/WP03-docs-release.md](tasks/WP03-docs-release.md)
**Priority**: P2 (final WP)
**Goal**: README/CHANGELOG/ROADMAP/SECURITY.md updates and the 0.17.0 version bump.
**Independent test**: `python -m agenttalk --version` → 0.17.0; doc claims match shipped behavior; full suite green.
**Estimated prompt size**: ~300 lines (3 subtasks).

- [ ] T011 README: dashboard/serve docs, CLI reference rows, install pin bumps (WP03)
- [ ] T012 SECURITY.md dashboard section + CHANGELOG 0.17.0 + ROADMAP refresh (WP03)
- [ ] T013 Version bump to 0.17.0 + full-suite gate (WP03)

**Dependencies**: Depends on WP02 (documents the final CLI surface).

## Execution notes

- Single serial lane (WP01 → WP02 → WP03): every WP touches behavior the next
  one documents or wires, and `web.py`/`cli.py` are single-owner files.
- Per-WP Codex cross-review over agenttalk (C-006) before each move to
  approved; fresh-eyes adversarial review before any tag; CI matrix green
  before tag (NFR-004).
- Dev gotcha (recorded from 0.16.0): tests run against the installed
  package — `pip install -e .` inside the active environment after entering
  a worktree, and re-point when switching back.
- MVP scope: WP01 alone is demoable (`web.serve_in_thread` + browser);
  the feature is operator-usable after WP02.
