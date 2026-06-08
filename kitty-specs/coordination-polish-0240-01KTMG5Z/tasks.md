# Tasks: agenttalk 0.24.0 — Coordination Polish

**Mission**: coordination-polish-0240-01KTMG5Z
**Branch contract**: master → master (matches target)
**Spec**: [spec.md](./spec.md) · **Plan**: [plan.md](./plan.md)

Split by **file surface** (cli.py must be single-owner). 3 work packages, serial:
WP01 (store + doctor) → WP02 (cli, depends WP01) → WP03 (docs/version, depends WP01+WP02).

## Subtask Index

| ID | Description | WP | Parallel |
|----|-------------|----|----------|
| T001 | `store.set_role` at-most-one-lead invariant (atomic demote+promote, idempotent, case-insensitive, zero-leads ok, returns demoted name) | WP01 | | [D] |
| T002 | `store.sole_lead() -> str\|None` (None for zero AND legacy >1) | WP01 | [D] |
| T003 | `doctor.py` no-human-facing-target Check (multi-agent only; neither liaison nor lead) | WP01 | [D] |
| T004 | tests: `test_store.py` — invariant, sole_lead, idempotent, case-insensitive, zero-leads | WP01 | | [D] |
| T005 | tests: `test_doctor.py` — warn / absent(liaison) / absent(lead) / solo | WP01 | | [D] |
| T006 | `escalate` lead-fallback resolution + remediation message + fallback notice | WP02 | |
| T007 | roster `set-role` handler prints `demoted X, promoted Y` (no --force) | WP02 | |
| T008 | wake `wk-` id: add to `_AUTOGEN_REQUEST_ID_PREFIX` (+ comment); honor explicit id | WP02 | [P] |
| T009 | owed-inbound pre-send warning in `cmd_send` (soft, same-peer decision-kinds, suppress same request_id, best-effort) — CUTTABLE (C-004) | WP02 | |
| T010 | tests: `test_cli.py` (escalate matrix, set-role notice, wk-, owed-inbound) + `test_threads.py` (OPENER_KINDS excludes wake) | WP02 | |
| T011 | Version bump 0.24.0 (`pyproject.toml`, `__init__.py`) + README install pins | WP03 | |
| T012 | CHANGELOG 0.24.0 section + ROADMAP header | WP03 | |

## WP01 — Store + doctor foundation

**Goal**: the at-most-one-lead roster invariant, the `sole_lead()` resolver helper, and
the `doctor` no-target nudge — everything below the CLI line. No dependencies; MVP core.
**Independent test**: `pytest tests/test_store.py tests/test_doctor.py -q` green; the
invariant, sole_lead semantics, and the doctor warn/absent/solo cases all pass.
**Requirements**: FR-004, FR-005 (store half), FR-006, FR-007, FR-008, FR-009.
**Prompt**: [tasks/WP01-store-doctor-foundation.md](./tasks/WP01-store-doctor-foundation.md)

- [x] T001 store.set_role at-most-one-lead invariant (WP01)
- [x] T002 store.sole_lead() helper (WP01)
- [x] T003 doctor no-human-facing-target Check (WP01)
- [x] T004 test_store.py coverage (WP01)
- [x] T005 test_doctor.py coverage (WP01)

**Risks**: changing `set_role` return shape could ripple to callers — keep it
backward-compatible (return cfg, communicate demoted name additively). Case-folding the
role must not corrupt stored role values.

## WP02 — CLI wiring (depends WP01)

**Goal**: wire the three CLI-facing behaviors — escalate lead-fallback, the set-role
notice, the wake `wk-` id, and the owed-inbound pre-send warning. Owns `cli.py`.
**Independent test**: `pytest tests/test_cli.py tests/test_threads.py -q` green; escalate
fallback matrix, set-role notice, wk- mint, owed-inbound warn/suppress/best-effort, and
the OPENER_KINDS-excludes-wake assertion all pass.
**Requirements**: FR-001, FR-002, FR-003, FR-005 (print half), FR-010, FR-011, FR-012,
FR-013, FR-014.
**Dependencies**: WP01 (needs `store.sole_lead()` and the `set_role` demoted-name return).
**Prompt**: [tasks/WP02-cli-wiring.md](./tasks/WP02-cli-wiring.md)

- [ ] T006 escalate lead-fallback (WP02)
- [ ] T007 roster set-role notice (WP02)
- [ ] T008 wake wk- correlation id (WP02)
- [ ] T009 owed-inbound pre-send warning — cuttable (WP02)
- [ ] T010 test_cli.py + test_threads.py coverage (WP02)

**Risks**: owed-inbound warning (T009) noise/complexity — keep it tight and cut per C-004
rather than stretch. The pre-send warning must never fail the send (best-effort).

## WP03 — Docs + version 0.24.0 (depends WP01, WP02)

**Goal**: release prep — version bump, README pins, CHANGELOG/ROADMAP. Final WP.
**Independent test**: `python -m agenttalk --version` → `agenttalk 0.24.0`;
`git grep -n "v0.23" README.md` returns no install-pin hits; ruff clean.
**Requirements**: none (docs/release).
**Dependencies**: WP01, WP02.
**Prompt**: [tasks/WP03-docs-release.md](./tasks/WP03-docs-release.md)

- [ ] T011 version bump 0.24.0 + README pins (WP03)
- [ ] T012 CHANGELOG 0.24.0 + ROADMAP header (WP03)

**Risks**: CHANGELOG must match what actually shipped (note if T009 was cut).

## MVP

WP01 + WP02 deliver the committed core (FR-001..011) plus the cuttable owed-inbound
warning (FR-012..014). WP03 is release packaging.
