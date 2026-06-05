# Tasks: Trusted-Team Safety 0.16.0

**Mission**: `trusted-team-safety-0160-01KTCQ3D`
**Spec**: [spec.md](spec.md) · **Plan**: [plan.md](plan.md) · **RFC**: `docs/rfc-identity-authz.md` (Phase A)
**Branch**: `master` (planning + merge target)

Single serial lane. WPs are partitioned by **file** so `owned_files` never
overlap (the finalizer hard-fails on overlap). `cli.py` is a single file and
therefore has a single owner (WP03) — all CLI wiring for the release lives in
WP03, which depends on the library layers (WP01 store, WP02 threads). Each WP is
cross-reviewed by Codex over agenttalk before it is considered done.

## Ownership map (no overlaps)

| WP | owned_files |
|----|-------------|
| WP01 | `src/agenttalk/store.py`, `tests/test_store.py` |
| WP02 | `src/agenttalk/threads.py`, `tests/test_threads.py` |
| WP03 | `src/agenttalk/cli.py`, `tests/test_cli.py`, `tests/test_coordination.py` |
| WP04 | `src/agenttalk/doctor.py`, `tests/test_doctor.py`, `src/agenttalk/__init__.py`, `pyproject.toml`, `README.md`, `SECURITY.md`, `CHANGELOG.md`, `ROADMAP.md` |

## Dependency graph

```
WP01 (store foundation) ──┐
WP02 (threads next-owner) ─┼─▶ WP03 (cli integration) ──▶ WP04 (doctor+docs+release)
                           ┘
```
WP01 and WP02 are independent of each other (run in serial order anyway). WP03
depends on both. WP04 depends on WP01 + WP03.

## Subtask Index

| ID | Description | WP | Parallel |
|----|-------------|----|----------|
| T001 | `retired` registry shape + fail-closed validation in `load_config` | WP01 | | [D] |
| T002 | `active_agents`/`retired_agents`/`known_agents`; history validation uses known roster | WP01 | | [D] |
| T003 | `retire_agent`, `rename_agent` (+ `_drain_check` helper), `remove_agent` refusal/force | WP01 | | [D] |
| T004 | retired-send refusal in `send()` (active-roster guard) | WP01 | | [D] |
| T005 | single-hop retired forwarding library support | WP01 | | [D] |
| T006 | `current_epoch()` + `epoch_at_send` stamping in `send()` for `OPENER_KINDS` | WP01 | | [D] |
| T007 | tests in `test_store.py` | WP01 | | [D] |
| T008 | `next_owner`/`next_action` fields on `ThreadRow` + `to_dict` | WP02 | [D] |
| T009 | derivation function (state → action/owner) per research D6 | WP02 | [D] |
| T010 | wire derivation into `derive_threads` | WP02 | [D] |
| T011 | tests in `test_threads.py` | WP02 | [D] |
| T012 | `roster retire`/`rename`/`remove`/`forward` subcommands + argparse | WP03 | |
| T013 | `barrier bump` command | WP03 | |
| T014 | `check --epoch` extension | WP03 | |
| T015 | `threads`/`sync --json` `next_owner`/`next_action` surfacing | WP03 | |
| T016 | `test_cli.py` — command behavior + exit codes | WP03 | |
| T017 | `test_coordination.py` — barrier→epoch_at_send→check e2e | WP03 | |
| T018 | doctor registry hygiene check | WP04 | |
| T019 | `test_doctor.py` | WP04 | |
| T020 | README updates (new commands + operator workflow) | WP04 | |
| T021 | SECURITY.md honesty (trusted-team boundary, fail-open, epoch three-state) | WP04 | |
| T022 | version bump 0.15.0→0.16.0 + CHANGELOG + ROADMAP Phase A delivered | WP04 | |

---

## WP01 — Identity registry, retirement & epoch store layer

**Goal**: Deliver the entire store-layer foundation: the `retired` registry,
safe retire/rename/remove/forward, retired-aware validation, retired-send
refusal, and the epoch primitive (`current_epoch()` + `epoch_at_send` stamping).
**Priority**: P0 (dependency root). **Prompt**: [WP01-store-foundation.md](tasks/WP01-store-foundation.md)
**Independent test**: `pytest tests/test_store.py` covers registry validation,
retire/rename/remove/force, retired-send refusal, known-vs-active roster,
forwarding, `current_epoch()`, and `epoch_at_send` three-state.

- [x] T001 `retired` registry shape + fail-closed validation in `load_config` (WP01)
- [x] T002 `active_agents`/`retired_agents`/`known_agents`; history validation uses known roster (WP01)
- [x] T003 `retire_agent`, `rename_agent` (+ `_drain_check`), `remove_agent` refusal/force (WP01)
- [x] T004 retired-send refusal in `send()` (active-roster guard) (WP01)
- [x] T005 single-hop retired forwarding library support (WP01)
- [x] T006 `current_epoch()` + `epoch_at_send` stamping in `send()` for `OPENER_KINDS` (WP01)
- [x] T007 tests in `test_store.py` (WP01)

**Dependencies**: none. **Risks**: T002 touches `Message.validate`/history
validation — the highest-risk refactor; a mistake invalidates history. Heavy
test coverage + Codex scrutiny.

## WP02 — Next-owner / next-action derivation

**Goal**: Add read-only `next_owner`/`next_action` to `ThreadRow`, derived purely
from thread state, surfaced via `to_dict()` (so `threads --json`/`sync --json`
later pick them up). Library-only; CLI wiring is WP03/T015.
**Priority**: P1. **Prompt**: [WP02-threads-next-owner.md](tasks/WP02-threads-next-owner.md)
**Independent test**: `pytest tests/test_threads.py` covers the state→action/owner
table including terminal-omission and broadcast non-responders.

- [x] T008 `next_owner`/`next_action` fields on `ThreadRow` + `to_dict` (WP02)
- [x] T009 derivation function (state → action/owner) per research D6 (WP02)
- [x] T010 wire derivation into `derive_threads` (WP02)
- [x] T011 tests in `test_threads.py` (WP02)

**Dependencies**: none. **Risks**: must not alter existing `to_dict` keys or
thread ordering (additive only).

## WP03 — CLI integration: roster, barrier, check --epoch, json next_*

**Goal**: Wire every new command surface in the single `cli.py` file: the
`roster` subcommands (retire/rename/remove/forward), `barrier bump`,
`check --epoch`, and `threads`/`sync --json` `next_*` surfacing. Plus the e2e
coordination test.
**Priority**: P1. **Prompt**: [WP03-cli-integration.md](tasks/WP03-cli-integration.md)
**Independent test**: `pytest tests/test_cli.py tests/test_coordination.py` covers
each subcommand's behavior and exit codes, plus the barrier→epoch_at_send→
`check --epoch` end-to-end flow.

- [ ] T012 `roster retire`/`rename`/`remove`/`forward` subcommands + argparse (WP03)
- [ ] T013 `barrier bump` command (WP03)
- [ ] T014 `check --epoch` extension (WP03)
- [ ] T015 `threads`/`sync --json` `next_owner`/`next_action` surfacing (WP03)
- [ ] T016 `test_cli.py` — command behavior + exit codes (WP03)
- [ ] T017 `test_coordination.py` — barrier→epoch_at_send→check e2e (WP03)

**Dependencies**: WP01, WP02. **Risks**: largest WP (single cli.py owner). Keep
each subtask thin by delegating mechanics to the WP01/WP02 library methods;
`cli.py` only parses args, calls library, and formats output + exit codes.

## WP04 — Doctor hygiene, docs honesty, version & release

**Goal**: Doctor registry-hygiene check; the load-bearing docs honesty
(trusted-team boundary, fail-open-vs-suppression, the `epoch_at_send`
three-state); version bump and release notes.
**Priority**: P2 (polish/release). **Prompt**: [WP04-doctor-docs-release.md](tasks/WP04-doctor-docs-release.md)
**Independent test**: `pytest tests/test_doctor.py`; docs reviewed for the
required honesty statements; `agenttalk --version` reports 0.16.0.

- [ ] T018 doctor registry hygiene check (WP04)
- [ ] T019 `test_doctor.py` (WP04)
- [ ] T020 README updates (new commands + operator workflow) (WP04)
- [ ] T021 SECURITY.md honesty (trusted-team boundary, fail-open, epoch three-state) (WP04)
- [ ] T022 version bump 0.15.0→0.16.0 + CHANGELOG + ROADMAP Phase A delivered (WP04)

**Dependencies**: WP01, WP03. **Risks**: docs must not overclaim — the
trusted-team/not-authz boundary (FR-016) is the whole point of Phase A honesty.

## Release gate (after all WPs merge)

- Full `pytest` green locally.
- Fresh-eyes adversarial review before tag.
- **CI matrix (py3.10–3.13 × 3 OSes) GREEN before tagging** (NFR-005) —
  `gh run watch`. This is the gate that caught the 0.14.0 red matrix.
