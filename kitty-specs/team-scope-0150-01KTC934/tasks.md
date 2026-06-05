# Tasks: 0.15.0 Team Scope

**Mission**: `team-scope-0150-01KTC934` | **Branch**: `master` (planning base `master`, merge target `master`)
**Inputs**: spec.md (FR-001..013, NFR-001..005, C-001..009), plan.md, research.md (D1-D6), data-model.md, contracts/cli-surface.md, quickstart.md

## Decomposition rationale

Module-layered WPs with disjoint `owned_files` (the finalizer hard-fails
on overlap), chained into one serial lane - the proven 0.14.0 shape.
Feature traceability per-subtask (#15/#16/#17).

## Subtask Index

| ID | Description | WP | Parallel |
|----|-------------|----|----------|
| T001 | store: resolve_role_audience (#15) | WP01 | | [D] |
| T002 | store: quarantine machinery - shared-gate paths + move w/ collision suffix (#17) | WP01 | | [D] |
| T003 | threads: responded_na + na_response labels; batch/audience passthrough fields (#15/#16) | WP01 | | [D] |
| T004 | tests: store role resolution + quarantine matrix | WP01 | | [D] |
| T005 | tests: threads NA labels + freeze independence + batch fields | WP01 | | [D] |
| T006 | cli: broadcast --to-role + frozen-audience meta (#15) | WP02 | | [D] |
| T007 | cli: broadcast partial-failure manifest + exit 5 (#16) | WP02 | | [D] |
| T008 | cli: reply --na + FR-006 refusal (#15) | WP02 | | [D] |
| T009 | cli: prune command (#17) | WP02 | | [D] |
| T010 | cli: displays + warnings (n/a markers, quarantined count, incomplete-batch) | WP02 | | [D] |
| T011 | tests: test_cli for T006-T010 incl. fault injection | WP02 | | [D] |
| T012 | doctor: store-hygiene check (invalid + quarantined) (#17) | WP03 | | [D] |
| T013 | tests: test_doctor (environment-pinned - the CI lesson) | WP03 | | [D] |
| T014 | e2e: role routing + post-send freeze (SC 1) | WP04 | | [D] |
| T015 | e2e: NA lifecycle both perspectives (SC 2) | WP04 | | [D] |
| T016 | e2e: fault-injected partial fan-out at k=first/mid/last + warning lifecycle (SC 3) | WP04 | | [D] |
| T017 | e2e: prune byte-identity sweep + strict-additivity gates (SC 4 / NFR-001) | WP04 | | [D] |
| T018 | skills: both flavors - NA-not-placeholder-ack, --to-role preference, exit-5 handling, prune discipline | WP05 | [P] |
| T019 | docs: README rows/sections + SECURITY notes | WP05 | [P] |
| T020 | release prep: CHANGELOG 0.15.0 + version bumps | WP05 | |
| T021 | gate: full suite + quickstart walk + CI-gate notes | WP05 | |

## Work Packages

### WP01 - Engine: store + threads

**Prompt**: [tasks/WP01-engine.md](tasks/WP01-engine.md) (~330 lines)
**Goal**: role resolution, quarantine machinery, NA/batch derivation labels - all pure logic, no cli.py.
**Priority**: P0. **Dependencies**: none.
**Independent test**: `pytest tests/test_store.py tests/test_threads.py tests/test_teams.py` green; cli.py untouched.

- [x] T001 store: resolve_role_audience (WP01)
- [x] T002 store: quarantine machinery (WP01)
- [x] T003 threads: NA labels + batch/audience passthrough (WP01)
- [x] T004 tests: store matrix (WP01)
- [x] T005 tests: threads labels/freeze/batch (WP01)

**Risks**: quarantine selection drift (killed by sharing the literal gate walk); NA labeling must not alter closure mechanics.

### WP02 - CLI surface

**Prompt**: [tasks/WP02-cli.md](tasks/WP02-cli.md) (~430 lines)
**Goal**: the user-visible surface per contracts/cli-surface.md (normative).
**Priority**: P0. **Dependencies**: WP01.
**Independent test**: quickstart sections 1-4 by hand; `pytest tests/test_cli.py` green.

- [x] T006 cli: broadcast --to-role + frozen meta (WP02)
- [x] T007 cli: partial-failure manifest + exit 5 (WP02)
- [x] T008 cli: reply --na + refusal (WP02)
- [x] T009 cli: prune command (WP02)
- [x] T010 cli: displays + warnings (WP02)
- [x] T011 tests: test_cli extensions (WP02)

**Risks**: exit-code contract (5 is new; 0/1/2/3/4/130 untouched); strict additivity of every JSON addition.

### WP03 - Doctor

**Prompt**: [tasks/WP03-doctor.md](tasks/WP03-doctor.md) (~180 lines)
**Goal**: store-hygiene visibility (invalid + quarantined).
**Priority**: P1. **Dependencies**: WP02.
**Independent test**: `pytest tests/test_doctor.py` green on an UNPINNED-free design (env pinned in tests).

- [x] T012 doctor: store-hygiene check (WP03)
- [x] T013 tests: test_doctor env-pinned (WP03)

**Risks**: the 0.14.0 CI lesson - never assert doctor exit codes on an unpinned host.

### WP04 - E2e gates

**Prompt**: [tasks/WP04-e2e.md](tasks/WP04-e2e.md) (~300 lines)
**Goal**: success criteria 1-4 demonstrably hold; release gate.
**Priority**: P1. **Dependencies**: WP02, WP03.
**Independent test**: `pytest tests/test_coordination.py` green.

- [x] T014 e2e: role routing + freeze (WP04)
- [x] T015 e2e: NA lifecycle (WP04)
- [x] T016 e2e: partial fan-out injection (WP04)
- [x] T017 e2e: prune byte-identity + additivity gates (WP04)

**Risks**: fault injection must target Store.send deterministically (monkeypatch at position k), no timing dependence.

### WP05 - Skills, docs, release prep

**Prompt**: [tasks/WP05-skills-docs.md](tasks/WP05-skills-docs.md) (~280 lines)
**Goal**: both CLI flavors taught; docs + version staged; release-ready pending CI gate + authorization.
**Priority**: P1. **Dependencies**: WP04.
**Independent test**: full suite incl. skill-lint green; quickstart walks clean.

- [ ] T018 skills: both flavors (WP05)
- [ ] T019 docs: README + SECURITY (WP05)
- [ ] T020 release prep: CHANGELOG + versions (WP05)
- [ ] T021 gate: suite + quickstart + CI notes (WP05)

**Risks**: skill flavor parity; no overpromising (quarantine is manual+recoverable, fan-out has no rollback).

## Dependency graph

```
WP01 -> WP02 -> WP03 -> WP04 -> WP05
```

Single lane. Review protocol: per-WP Codex review (meta mission/wp_id),
fresh-eyes + CI matrix green before tagging (NFR-005).
