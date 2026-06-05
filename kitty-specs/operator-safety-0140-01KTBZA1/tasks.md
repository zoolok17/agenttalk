# Tasks: 0.14.0 Operator Safety

**Mission**: `operator-safety-0140-01KTBZA1` | **Branch**: `master` (planning base `master`, merge target `master`)
**Inputs**: spec.md (FR-001..017, NFR-001..005, C-001..010), plan.md, research.md (D1–D7), data-model.md, contracts/cli-surface.md, quickstart.md

## Decomposition rationale

agenttalk funnels all four features through four source files (`cli.py`,
`store.py`, `threads.py`, `doctor.py`), so work packages are layered by
**module ownership** (disjoint `owned_files`), not by feature. Dependencies
chain them into one serial lane — matching the project's deliberate
single-implementer model. Feature traceability is kept per-subtask (each
subtask names its issue #12/#13/#18/#14).

**#14 slip rule (C-010)**: if intent-to-reply threatens C-004 (no new
load-bearing state) or the release gets heavy, drop exactly: T003's marker
half, T014, and the #14 lines of T022–T024. Everything else stands alone.

## Subtask Index

| ID | Description | WP | Parallel |
|----|-------------|----|----------|
| T001 | store: `rescind` kind + send-validation helper (+ web.py kind gate) (#12) | WP01 | | [D] |
| T002 | store: `AGENTTALK_ROOT` precedence + upward multi-store scan helper (#13) | WP01 | | [D] |
| T003 | store: operator_facing accessors (#18) + reply-in-flight marker helpers (#14) | WP01 | | [D] |
| T004 | threads: `closed-superseded` derivation per D2 (#12) | WP01 | | [D] |
| T005 | threads: escalation row surfacing (needs_operator, operator_state) (#18) | WP01 | | [D] |
| T006 | tests: new tests/test_store.py + test_threads.py extensions | WP01 | | [D] |
| T007 | cli: `cmd_rescind` (#12) | WP02 | | [D] |
| T008 | cli: `cmd_check` with exit 0/3/4 (#12) | WP02 | | [D] |
| T009 | cli: scoped-wait rescind wake, exit 3 (#12) | WP02 | | [D] |
| T010 | cli: display layer — superseded rows, escalation bucket, reply-in-flight, status warnings (#12/#18/#14) | WP02 | | [D] |
| T011 | cli: `init` up-tree guard + AGENTTALK_ROOT wiring + whoami root-first (#13) | WP02 | | [D] |
| T012 | cli: `roster set-operator-facing` + roster/whoami display (#18) | WP02 | | [D] |
| T013 | cli: `cmd_escalate` + refusal matrix (#18) | WP02 | | [D] |
| T014 | cli: `composing --to-request` sugar (#14, slip-droppable) | WP02 | | [D] |
| T015 | tests: test_cli.py extensions for T007–T014 | WP02 | | [D] |
| T016 | doctor: multi-store detection + root-first output (#13) | WP03 | | [D] |
| T017 | doctor: liaison diagnostics (#18) | WP03 | | [D] |
| T018 | tests: test_doctor.py extensions | WP03 | | [D] |
| T019 | e2e: rescind race — send→rescind→wake/check→abort (#12) | WP04 | | [D] |
| T020 | e2e: liaison flow — escalate→bucket→answer→closed + refusals (#18) | WP04 | | [D] |
| T021 | e2e: backward-compat sweep (NFR-001, mixed-version store) | WP04 | | [D] |
| T022 | skills: Claude-side updates (7 files) | WP05 | [P] |
| T023 | skills: Codex-side updates (7 files) | WP05 | [P] |
| T024 | docs: README + SECURITY | WP05 | [P] |
| T025 | release prep: CHANGELOG 0.14.0 + version bumps | WP05 | |
| T026 | gate: full suite + quickstart smoke-walk + skill-lint | WP05 | |

## Work Packages

### WP01 — Engine: store + threads foundations

**Prompt**: [tasks/WP01-engine-store-threads.md](tasks/WP01-engine-store-threads.md) (~420 lines)
**Goal**: every pure-logic foundation the CLI surface will call: rescind kind/validation, supersession derivation, root-resolution precedence, multi-store scan, operator_facing accessors, escalation row surfacing, reply-in-flight marker IO.
**Priority**: P0 — everything depends on it. **Dependencies**: none.
**Independent test**: `pytest tests/test_store.py tests/test_threads.py` green; no CLI behavior changed yet (cli.py untouched).

- [x] T001 store: rescind kind + send-validation helper (+ web.py kind gate) (WP01)
- [x] T002 store: AGENTTALK_ROOT precedence + upward multi-store scan helper (WP01)
- [x] T003 store: operator_facing accessors + reply-in-flight marker helpers (WP01)
- [x] T004 threads: closed-superseded derivation per D2 (WP01)
- [x] T005 threads: escalation row surfacing (WP01)
- [x] T006 tests: test_store.py (new) + test_threads.py extensions (WP01)

**Risks**: D2 ordering subtleties (pinned target_msg_id, requester-only, precedence vs manual ack closure); regression risk in derive_threads replay — the existing test_threads suite is the guard rail.

### WP02 — CLI surface: rescind/check/wait, root, liaison, intent

**Prompt**: [tasks/WP02-cli-surface.md](tasks/WP02-cli-surface.md) (~620 lines)
**Goal**: the entire user-visible command surface per contracts/cli-surface.md.
**Priority**: P0. **Dependencies**: WP01.
**Independent test**: quickstart.md sections 1–5 pass end-to-end by hand; `pytest tests/test_cli.py` green.
**Size note**: 9 subtasks — at the upper bound deliberately: `cli.py` has single-WP ownership (disjointness constraint), and splitting would violate it. Mitigation: subtasks are strictly independent argparse/command units; reviewer reviews per-subtask.

- [x] T007 cli: cmd_rescind (WP02)
- [x] T008 cli: cmd_check exit 0/3/4 (WP02)
- [x] T009 cli: scoped-wait rescind wake exit 3 (WP02)
- [x] T010 cli: display layer — superseded/escalations/reply-in-flight/status warnings (WP02)
- [x] T011 cli: init up-tree guard + AGENTTALK_ROOT wiring + whoami root-first (WP02)
- [x] T012 cli: roster set-operator-facing + display (WP02)
- [x] T013 cli: cmd_escalate + refusal matrix (WP02)
- [x] T014 cli: composing --to-request sugar (WP02, #14 slip-droppable)
- [x] T015 tests: test_cli.py extensions for T007–T014 (WP02)

**Risks**: exit-code contract regressions (C-005); `wait` exit 1 must stay timeout-exclusive; request_id autogen/echo logic must not regress (v0.9.0 aliasing bug — see research.md baseline table).

### WP03 — Diagnostics: doctor

**Prompt**: [tasks/WP03-doctor-diagnostics.md](tasks/WP03-doctor-diagnostics.md) (~260 lines)
**Goal**: doctor grows multi-store detection, root-first output, and liaison diagnostics.
**Priority**: P1. **Dependencies**: WP02 (the engine's scan helper and operator_facing accessor arrive transitively).
**Independent test**: `pytest tests/test_doctor.py` green; quickstart section 3 doctor lines pass.

- [x] T016 doctor: multi-store detection + root-first output (WP03)
- [x] T017 doctor: liaison diagnostics (WP03)
- [x] T018 tests: test_doctor.py extensions (WP03)

**Risks**: doctor/cli boundary — `cmd_doctor` wiring lives in cli.py (WP02 ownership); WP03 changes must stay inside doctor.py's check/run functions.

### WP04 — End-to-end coordination tests

**Prompt**: [tasks/WP04-e2e-coordination.md](tasks/WP04-e2e-coordination.md) (~300 lines)
**Goal**: prove the two production incidents are dead: scripted rescind race and liaison flow, plus the NFR-001 compatibility sweep.
**Priority**: P1 — release gate. **Dependencies**: WP02, WP03.
**Independent test**: `pytest tests/test_coordination.py` green; success criteria 1–3 of spec.md demonstrably hold.

- [x] T019 e2e: rescind race (WP04)
- [x] T020 e2e: liaison flow + refusals (WP04)
- [x] T021 e2e: backward-compat sweep (WP04)

**Risks**: timing flakiness in wait-based tests — use the suite's existing short-poll patterns, never sleeps near the timeout boundary.

### WP05 — Skills, docs, release prep

**Prompt**: [tasks/WP05-skills-docs-release.md](tasks/WP05-skills-docs-release.md) (~340 lines)
**Goal**: both CLIs' skill contracts teach check-before-irreversible / rescind-over-prose / escalate-not-your-window; README/SECURITY/CHANGELOG/version ready to tag.
**Priority**: P1 — ships the release. **Dependencies**: WP04 (transitively everything — docs must describe what actually landed, incl. whether #14 made it).
**Independent test**: `pytest` (full suite incl. skill-lint) green; quickstart.md walks clean against the built CLI.

- [ ] T022 skills: Claude-side updates (WP05)
- [ ] T023 skills: Codex-side updates (WP05)
- [ ] T024 docs: README + SECURITY (WP05)
- [ ] T025 release prep: CHANGELOG + version bumps (WP05)
- [ ] T026 gate: full suite + quickstart smoke-walk + skill-lint (WP05)

**Risks**: skill drift between the two CLI flavors (test_skill_lint guards structure, not semantics — cross-review covers semantics); tag/push/Release-object steps are NOT in this WP: they require explicit operator authorization per house rules.

## Dependency graph

```
WP01 ──> WP02 ──> WP03 ──> WP04 ──> WP05
```

Single lane (chained dependencies + shared-surface reality). Parallelism
exists only inside WP05 ([P] on T022/T023/T024 — different file sets).

## Review protocol (C-009)

Every WP: implement → `agenttalk` review-request to Codex (meta carries
`mission=operator-safety-0140-01KTBZA1`, `wp_id=WPxx`) → blockers fixed →
approved → next WP. Fresh-eyes reviewer before tagging the release.
