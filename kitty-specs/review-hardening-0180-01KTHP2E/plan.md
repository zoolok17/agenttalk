# Implementation Plan: Review Hardening (agenttalk 0.18.0)

**Branch**: `master` (plan + merge target; lane branches from master, squash back)
**Date**: 2026-06-07 | **Spec**: [spec.md](spec.md) | **Issue**: zoolok17/agenttalk#21

## Summary

Five reviewer findings + one operator-requested guardrail, all small,
additive, and backward-compatible. The fixes land in the existing modules
that own each surface; no public command shape changes beyond additive fields
and one advisory warning. Design Codex-accepted (bus proposal `pp-47eae0ce`).

## Technical Context

**Language/Version**: Python 3.10–3.13 (CI matrix), stdlib only
**Primary Dependencies**: none new (`ctypes` for the Windows liveness probe; `os.kill` on POSIX)
**Storage**: existing `.agenttalk/` file store; no schema change (the marker field used by FR-007 already exists)
**Testing**: pytest, extending existing suites; every fix pins the exact reviewer repro
**Target Platform**: Windows-first + Linux/macOS (CI matrix)
**Project Type**: single project (`src/agenttalk/`)
**Constraints**: stdlib-only (C-001); additive/back-compat (C-002); immutable history (C-003); exit-code contract (C-004); Windows-first (C-005); warn-not-enforce (C-006); scope-honest id validation (C-008)

## Verified code facts (drive the plan)

- **id format** (`store.py:1887` `_new_id`): `"%Y%m%d-%H%M%S-%f"` + `"-"` + 4×`_ID_ALPHABET` where `_ID_ALPHABET = string.ascii_letters + string.digits`. → canonical regex `^\d{8}-\d{6}-\d{6}-[A-Za-z0-9]{4}$`, **built in code from the same constants**, not hand-copied (FR-003/C-008).
- **`.waiting` marker already carries `pid`** (`cli.py:1654` writes `{"agent","pid","since","cursor_at_start","deadline_epoch"}`). `read_waiting` (`store.py:1675`) returns the dict or None, never raises. → FR-007 needs NO heartbeat restructuring; it reads the existing marker.
- **signature gate** (`signing.py` `verify_message`, ~328): `compare_digest(claimed, actual)` with `claimed = meta["signature"]`; callers catch only `ValueError`. → FR-001 guards the type before the call.
- **validation surfaces**: `store._validated_messages` uses `_known_roster` (active∪retired, the D3 rule); `web._all_messages` and `cli tail` use `cfg["agents"]` (active only) → the FR-004 asymmetry.
- **broadcast**: resume reconstructs recipients from frozen `audience_resolved` meta and calls `store.send` (refuses retired). `threads._derive_broadcast` builds `pending` from the frozen `audience` without consulting the current retired set.

## Charter Check

Skipped — no charter at `.kittify/charter/charter.md` (consistent with all prior missions).

## Project Structure

```
kitty-specs/review-hardening-0180-01KTHP2E/
├── plan.md  research.md  data-model.md  quickstart.md
├── contracts/cli-surface.md
└── tasks.md            # /spec-kitty.tasks — not this command

src/agenttalk/
├── signing.py   # FR-001 signature type guard
├── store.py     # FR-003 id-shape validation (Message.from_raw); FR-007 liveness primitive + foreign-wait detector
├── threads.py   # FR-006 broadcast pending excludes retired + audience_retired
├── web.py       # FR-004 dashboard message routes → known roster
├── cli.py       # FR-004 tail → known roster; FR-005 resume skip-retired; FR-007 wait/listen warning
├── doctor.py    # FR-009 marker pid/liveness report
└── __init__.py  # 0.18.0
tests/  (per-module)   README.md SECURITY.md CHANGELOG.md ROADMAP.md pyproject.toml
```

## Work Package shape (Phase 2 preview — single serial lane, non-overlapping files)

- **WP01 — core: invalid-classification + liveness primitive.** `signing.py`,
  `store.py`, `tests/test_signing.py`, `tests/test_store.py`. FR-001, FR-003,
  FR-002 (store-level graceful degradation), and the FR-007 **primitives**
  (`_process_alive(pid)`, `foreign_wait_pid(agent, self_pid)`). Foundation —
  everything else consumes the new invalid-classification + helpers.
- **WP02 — retired-aware derivation + dashboard parity.** `threads.py`,
  `web.py`, `tests/test_threads.py`, `tests/test_web.py`. FR-006 (broadcast
  pending/next_owner excludes retired, additive `audience_retired`) + FR-004
  (web message routes → known roster). Two read-derivation surfaces, one
  theme: render retired identities correctly. Depends on WP01.
- **WP03 — CLI wiring.** `cli.py`, `tests/test_cli.py`,
  `tests/test_coordination.py`. FR-004 (tail → known roster), FR-005 (resume
  skip-retired + exit-0-when-all-retired), FR-007 (wait/listen duplicate
  warning using the WP01 detector). Single-owner cli. Depends on WP01, WP02.
- **WP04 — doctor + docs + release.** `doctor.py`, `tests/test_doctor.py`,
  `README.md`, `SECURITY.md`, `CHANGELOG.md`, `ROADMAP.md`, `pyproject.toml`,
  `src/agenttalk/__init__.py`. FR-009 (marker pid/liveness, advisory), the
  C-006/C-008 documentation, version 0.18.0. Depends on WP03.

## Phase outline

- **Phase 0** (`research.md`): decision records D1–D8 (id regex derivation,
  signature guard placement, liveness mechanism, duplicate-detection
  semantics, resume skip-retired exit codes, audience_retired shape, roster
  parity scope, scope-honesty wording).
- **Phase 1** (`data-model.md`, `contracts/cli-surface.md`, `quickstart.md`):
  exact additive shapes (`audience_retired`, doctor liveness field, the id
  regex), CLI/exit-code deltas, and the per-finding validation walkthrough.
- **Phase 2** (`/spec-kitty.tasks`): the 4 WPs above; per-WP Codex review
  (C-007); fresh-eyes before tag; CI matrix green before tag (NFR-002).

## Risks

- **id regex too strict** → reclassifies a legitimate message. Mitigation:
  derive the pattern from the same `_ID_ALPHABET`/strftime constants `_new_id`
  uses, and test it against a large batch of freshly-generated ids + the
  monotonic-bump path (`+1µs` can roll seconds — `%f` stays 6 digits, but
  verify around second/minute boundaries).
- **Windows liveness false "alive"** on PID reuse → a stale warning. Accepted
  (advisory, false-positive-tolerant, FR-008); never blocks.
- **roster-parity render change** (FR-004) shows more messages than before —
  intended, but a visible behavior change; CHANGELOG calls it out, existing
  shape tests must stay green (NFR-001).
- **liveness probe must never throw** (FR-008): wrap every syscall; any error
  → the detector returns None (fail-quiet, never crash a `wait` start).

## Complexity Tracking

No charter gates. The only cross-module coupling is WP01's two store helpers
consumed by WP03 (detector) and WP04 (doctor) — handled by the linear lane
dependency, no shared-file ownership.
