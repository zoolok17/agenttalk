# Implementation Plan: agenttalk 0.24.0 — Coordination Polish

**Branch**: `master` | **Date**: 2026-06-08 | **Spec**: [spec.md](./spec.md)
**Branch contract**: current `master` → planning base `master` → merge target `master` (matches target: yes)

## Summary

Three agenttalk-side coordination fixes from production feedback, design-reviewed with
the peer agent: (1) `escalate` falls back to the team **lead** when no liaison is set,
backed by an *at-most-one-lead* roster invariant and a `doctor` nudge; (2) `wake`
messages carry a `wk-` correlation id without becoming thread openers; (3) a soft,
best-effort warning before sending traffic to a peer you owe an open decision-request.
Stdlib-only, additive, history-immutable, exit-code contract preserved.

## Technical Context

**Language/Version**: Python ≥3.10, standard library only (no new runtime deps)
**Primary Dependencies**: none (stdlib); pytest for tests only
**Storage**: existing `.agenttalk/` file-backed store; new behavior adds only message
*meta* (`wk-` id) and roster/config *state* (lead role moves) — no new files
**Testing**: pytest; CI matrix Python 3.10–3.13 × {Linux, macOS, Windows}
**Target Platform**: cross-platform CLI, Windows-first
**Project Type**: single project (`src/agenttalk/`)
**Performance Goals**: no regression; per-command work stays O(roster)/O(open threads)
**Constraints**: additive/backward-compatible; message history immutable; `escalate`
still exits 2 when there is genuinely no target; no removed flags/kinds/keys/exit codes
**Scale/Scope**: 3 source files (`cli.py`, `store.py`, `doctor.py`) + tests; ~14 FRs

## Charter Check

*Skipped — no charter configured (`.kittify/charter/charter.md` absent).*

## Project Structure

### Documentation (this feature)

```
kitty-specs/coordination-polish-0240-01KTMG5Z/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/
│   └── cli-behavior.md  # Phase 1 output — CLI behavioral contracts (test gates)
└── tasks.md             # Phase 2 (/spec-kitty.tasks — NOT created here)
```

### Source Code (touched files, repository root)

```
src/agenttalk/
├── cli.py        # escalate lead-fallback resolution; roster set-role demote+promote
│                 #   notice; wk- mint for wake; owed-inbound pre-send warning
├── store.py      # at-most-one-lead invariant in set_role(); sole_lead() helper
└── doctor.py     # new Check: no-human-facing-target warning (multi-agent only)

tests/
├── test_store.py    # at-most-one-lead invariant, sole_lead semantics
├── test_cli.py      # escalate fallback, set-role notice, wk- mint, pre-send warning
├── test_doctor.py   # no-target warning present/absent cases
└── test_threads.py  # assert wake is NOT a thread opener
```

**Structure Decision**: single existing package `src/agenttalk/`. No new modules; all
changes land in the three named files plus tests. `threads.py` is reused read-only
(`derive_threads`) with no signature change.

## Design Decisions (locked — operator + peer review)

1. **Liaison and lead stay distinct.** `operator_facing` is the primary escalation
   target; `role=lead` is only the fallback. Not merged.
2. **At-most-one lead, never exactly-one.** Uniqueness enforced on the `set_role`
   write path; zero leads is valid; no bootstrap/auto-promote.
3. **`sole_lead()` returns None when ambiguous.** A legacy config with two `lead` rows
   yields None so `escalate` falls through to remediation rather than guessing.
4. **`wk-` mints but does not open a thread.** Add `wake` to
   `cli._AUTOGEN_REQUEST_ID_PREFIX`; leave `store.OPENER_KINDS` unchanged (asserted by
   a test) so `threads` never creates an owed/open row for a wake.
5. **Owed-inbound warning is soft + best-effort.** Never blocks/fails a send; any
   thread-derivation error is swallowed. The single cuttable item (C-004).

## Anchors (verified against current source)

- `cli._AUTOGEN_REQUEST_ID_PREFIX` (cli.py:92) — add `"wake": "wk-"`; minting via
  `_maybe_autogen_request_id` (cli.py:106) already runs in the send path.
- `store.OPENER_KINDS` (store.py:94) — unchanged; this is what makes wake non-opening.
- `escalate` else-branch (cli.py:1098–1115) — insert lead-fallback before the exit-2
  error; keep `--to` override (cli.py:1090) and self-target guard (cli.py:1116).
- `store.set_role` (store.py:938) — enforce invariant inside its `_config_lock()`.
- `store.operator_facing` / `operator_facing_raw` (store.py:986/976) — reused as the
  first link of the resolution chain; add a `sole_lead()` sibling.
- `doctor.py` — append a conditional Check (absent/ok when a liaison or lead exists, or
  when the roster is solo).
- `cmd_send` send path — add the owed-inbound warning via `threads.derive_threads`.

## Phase 0 — Research

See [research.md](./research.md). No open unknowns; records the three small API
decisions and rejected alternatives.

## Phase 1 — Design & Contracts

- [data-model.md](./data-model.md) — lead-role invariant, liaison relationship, `wk-`
  id, owed-decision-request derivation.
- [contracts/cli-behavior.md](./contracts/cli-behavior.md) — exact input→output/exit
  contracts for `escalate`, `roster set-role`, `doctor`, wake send, pre-send warning.
- [quickstart.md](./quickstart.md) — manual validation per scenario.

## Implementation Sequencing (preview for /spec-kitty.tasks)

- **WP-A** — Escalation lead-fallback + at-most-one-lead invariant + doctor nudge
  (FR-001..009). Committed core; the bulk of the work.
- **WP-B** — Wake correlation id (FR-010..011). Tiny.
- **WP-C** — Owed-inbound pre-send warning (FR-012..014). Cuttable per C-004.
- **WP-D** — Docs + version bump to 0.24.0 (README, CHANGELOG, ROADMAP, pyproject,
  `__init__`). Final WP.

Exact WP boundaries, owned_files, and ordering finalized by `/spec-kitty.tasks`.

## Complexity Tracking

No charter violations. No added projects, patterns, or dependencies.
