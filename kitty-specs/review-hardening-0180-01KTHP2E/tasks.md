# Tasks: Review Hardening (agenttalk 0.18.0)

**Mission**: `review-hardening-0180-01KTHP2E` | **Branch**: plan + merge on `master`
**Input**: spec.md (FR-001..009 / NFR-001..003 / C-001..008), plan.md, research.md (D1–D8), data-model.md, contracts/cli-surface.md, quickstart.md
**Pre-code review**: Codex approved rev2 (fffcb78) — `wait` is the only blocking-wait command; `foreign_wait_pid` takes freshness policy as params.

## Subtask Index

| ID | Description | WP | Parallel |
|----|-------------|----|----------|
| T001 | Signature type-guard in `verify_message` (FR-001) + graceful-degradation tests | WP01 | — | [D] |
| T002 | `_ID_RE` + id-shape validation in `Message.from_raw` (FR-003) | WP01 | — | [D] |
| T003 | `_process_alive(pid)` liveness primitive (POSIX + Windows ctypes, fail-quiet) | WP01 | — | [D] |
| T004 | `Store.foreign_wait_pid(agent, self_pid, now=, stale_after=)` detector | WP01 | — | [D] |
| T005 | Store-level tests: poison-signature degradation, malformed-id invalid, liveness/detector | WP01 | — | [D] |
| T006 | `threads` broadcast: exclude retired from `pending`/`next_owner`; additive `audience_retired` (FR-006) | WP02 | — | [D] |
| T007 | `web._all_messages` → known roster (FR-004 web) | WP02 | — | [D] |
| T008 | threads + web tests (retired pending/audience_retired; retired history renders) | WP02 | — | [D] |
| T009 | `tail` → known roster (FR-004 tail) | WP03 | — | [D] |
| T010 | `broadcast --resume` skip-retired + dropped report + exit-0-when-all-retired (FR-005) | WP03 | — | [D] |
| T011 | `wait` duplicate-activation warning via `foreign_wait_pid` (FR-007/008) | WP03 | — | [D] |
| T012 | CLI tests: tail-retired, resume-skip-retired exits, wait-warning live/dead | WP03 | — | [D] |
| T013 | `doctor` marker pid/liveness advisory (FR-009) + tests | WP04 | — | [D] |
| T014 | Docs: README + SECURITY (one-window-per-agent unsupported; clock-agreement) (C-006/008) | WP04 | — | [D] |
| T015 | CHANGELOG 0.18.0 + ROADMAP + version bump pyproject/`__init__` + full-suite gate | WP04 | — | [D] |

## Work Packages

### WP01 — Core: invalid-classification + liveness primitive

**Prompt**: [tasks/WP01-core-invalid-and-liveness.md](tasks/WP01-core-invalid-and-liveness.md)
**Priority**: P1 (foundation — WP02/03/04 consume the new classification + helpers)
**Goal**: `signing.py` + `store.py` + their tests.
**Independent test**: `pytest tests/test_signing.py tests/test_store.py -q` green; quickstart §§1–2 manually.
**Estimated**: ~480 lines (5 subtasks).

- [x] T001 Signature type-guard in `verify_message` (FR-001) (WP01)
- [x] T002 `_ID_RE` + id-shape validation in `Message.from_raw` (FR-003) (WP01)
- [x] T003 `_process_alive(pid)` liveness primitive (WP01)
- [x] T004 `Store.foreign_wait_pid` detector (WP01)
- [x] T005 Store-level tests (WP01)

**Dependencies**: none.

### WP02 — Retired-aware derivation + dashboard parity

**Prompt**: [tasks/WP02-retired-derivation-and-web-parity.md](tasks/WP02-retired-derivation-and-web-parity.md)
**Priority**: P1
**Goal**: `threads.py` + `web.py` + their tests.
**Independent test**: `pytest tests/test_threads.py tests/test_web.py -q` green; quickstart §§3,5.
**Estimated**: ~360 lines (3 subtasks).

- [x] T006 broadcast pending/next_owner excludes retired + `audience_retired` (FR-006) (WP02)
- [x] T007 `web._all_messages` → known roster (FR-004 web) (WP02)
- [x] T008 threads + web tests (WP02)

**Dependencies**: Depends on WP01 (consumes the new invalid-classification; `web._all_messages` parity aligns with WP01's scan behavior).

### WP03 — CLI wiring

**Prompt**: [tasks/WP03-cli-wiring.md](tasks/WP03-cli-wiring.md)
**Priority**: P1
**Goal**: `cli.py` + `tests/test_cli.py` + `tests/test_coordination.py`.
**Independent test**: `pytest tests/test_cli.py tests/test_coordination.py -q` green; quickstart §§4,6.
**Estimated**: ~420 lines (4 subtasks).

- [x] T009 `tail` → known roster (FR-004 tail) (WP03)
- [x] T010 `broadcast --resume` skip-retired + exit codes (FR-005) (WP03)
- [x] T011 `wait` duplicate-activation warning (FR-007/008) (WP03)
- [x] T012 CLI + coordination tests (WP03)

**Dependencies**: Depends on WP01 (the `foreign_wait_pid` detector) and WP02 (resume reads the same retired-set semantics; single-owner cli serializes after).

### WP04 — doctor + docs + release

**Prompt**: [tasks/WP04-doctor-docs-release.md](tasks/WP04-doctor-docs-release.md)
**Priority**: P2 (final WP)
**Goal**: `doctor.py` + `tests/test_doctor.py` + release docs + version.
**Independent test**: `python -m agenttalk --version` → 0.18.0; full suite green; quickstart §§6–8.
**Estimated**: ~300 lines (3 subtasks).

- [x] T013 `doctor` marker pid/liveness advisory (FR-009) (WP04)
- [x] T014 Docs: README + SECURITY honesty notes (C-006/008) (WP04)
- [x] T015 CHANGELOG + ROADMAP + version 0.18.0 + full-suite gate (WP04)

**Dependencies**: Depends on WP03 (documents the final CLI surface; the liveness primitive it reuses is already in the lane transitively).

## Execution notes

- Single serial lane WP01 → WP02 → WP03 → WP04 (cli.py single-owner; WP01
  helpers consumed downstream). Non-overlapping owned_files.
- Per-WP Codex cross-review (C-007); fresh-eyes before tag; CI matrix green
  before tag (NFR-002).
- Dev gotcha: `pip install -e .` inside the worktree before testing; re-point
  to main on merge.
- MVP: WP01 alone closes the BLOCKER + the cursor-poison MAJOR.
