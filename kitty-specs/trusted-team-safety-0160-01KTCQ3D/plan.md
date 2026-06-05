# Implementation Plan: Trusted-Team Safety 0.16.0

**Branch**: `master` | **Date**: 2026-06-05 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `kitty-specs/trusted-team-safety-0160-01KTCQ3D/spec.md`
**RFC**: `docs/rfc-identity-authz.md` (Phase A — already reviewed & committed)

## Summary

Implement Phase A of the identity/authz RFC: a trusted-team identity registry
with non-rebindable retired tombstones, safe rename/retire, a meta-marked global
barrier primitive whose epoch id is the barrier message id, `check --epoch`
currentness, and read-only `next_owner`/`next_action` thread hints. The work is
**purely additive** over the 0.15.0 store/threads/cli/doctor modules — no new
message kind, no history mutation, stdlib-only. The technical approach extends
existing seams: the `config.json` roster (new `retired` list + lineage), the
`Message`/`send` path (retired-send refusal + `epoch_at_send` stamping), thread
derivation in `threads.py` (`next_owner`/`next_action`), and the `check` command
(`--epoch`). A new `barrier` command sends an ordinary `kind=message` carrying
`meta.barrier`.

## Technical Context

**Language/Version**: Python 3.10+ (`pyproject.toml` `target-version = py310`; CI matrix 3.10–3.13)
**Primary Dependencies**: Python standard library ONLY (stdlib-only runtime is a hard constraint — NFR-001/C constraints). Existing internal modules: `store`, `threads`, `cli`, `doctor`, `signing`.
**Storage**: File-backed bus under `.agenttalk/` — one JSON file per message (immutable), plus `config.json` (the mutable roster/registry). No database.
**Testing**: pytest. Tests run against the installed package — `pip install -e .` after editing `src/` or you test the stale site-packages copy (known dev gotcha).
**Target Platform**: Windows-first (PowerShell), plus Linux/macOS — all three in CI.
**Project Type**: Single project (CLI tool + library).
**Performance Goals**: Not latency-bound; operations scan the message log (existing `valid_messages()` cost profile). Epoch lookup is a single scan for the latest `meta.barrier` message — acceptable at the team's message volume.
**Constraints**: Additive/backward-compatible JSON (absent-not-null, with the documented `epoch_at_send=null` exception); immutable history (rename/retire touch ONLY `config.json`); exit-code contract preserved (0/1/2/3/4/5/130) — `check --epoch` reuses 0/3/4; no new message kind for barriers.
**Scale/Scope**: Small trusted roster (2–6 agents) + one operator. ~16 FRs across 4 module surfaces.

## Charter Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

No charter file exists (`.kittify/charter/charter.md` absent — same as the
0.14.0/0.15.0 missions). Charter Check is **skipped**. The project's standing
constraints (stdlib-only, immutable history, additive migration,
governance-free transport, trusted-team-not-authz boundary) are carried as
explicit spec constraints (C-001..C-006, NFR-001..003) and enforced in review.

## Project Structure

### Documentation (this feature)

```
kitty-specs/trusted-team-safety-0160-01KTCQ3D/
├── plan.md              # This file
├── research.md          # Phase 0 — design decisions & resolved ambiguities
├── data-model.md        # Phase 1 — config registry, meta.barrier, epoch_at_send, next_owner/action
├── quickstart.md        # Phase 1 — the 7 primary-flow validation scenarios
├── contracts/           # Phase 1 — CLI command surfaces (cli-surface.md)
└── tasks.md             # Phase 2 — created by /spec-kitty.tasks (NOT here)
```

### Source Code (repository root)

```
src/agenttalk/
├── store.py        # registry (retired list + lineage), retire/rename/remove ops,
│                   #   retired-aware validation roster, current_epoch(), epoch_at_send
│                   #   stamping in send(), retired-send refusal, single-hop forwarding
├── threads.py      # next_owner / next_action derivation on ThreadRow (read-only)
├── cli.py          # roster retire|rename|remove subcommands, barrier bump,
│                   #   check --epoch, threads/sync --json next_* surfacing
├── doctor.py       # registry hygiene (retired tombstone sanity) — light touch
└── __init__.py     # version bump 0.15.0 -> 0.16.0

tests/
├── test_store.py        # registry, retire/rename/remove, epoch, validation roster
├── test_threads.py      # next_owner / next_action derivation
├── test_cli.py          # roster/barrier/check --epoch command behavior + exit codes
├── test_coordination.py # end-to-end barrier→epoch_at_send→check --epoch flow
└── test_doctor.py       # registry hygiene check

docs/
├── README.md       # new commands + operator workflow (retire/rename, barrier, check --epoch)
└── SECURITY.md     # trusted-team boundary, fail-open-vs-suppression honesty
```

**Structure Decision**: Single project, module-layered. WP ownership splits by
**file** (not feature) so `owned_files` never overlap — the finalizer hard-fails
on overlap and the 0.14.0/0.15.0 missions proved file-split is the only stable
partition for a single serial lane. Layering: store.py is the foundation
(registry + epoch + validation); threads.py and cli.py build on it; docs/tests
ride with their owning WP.

## Implementation Phasing (high level — WPs come from /spec-kitty.tasks)

1. **Foundation — registry & retirement (store.py)**: `retired` registry shape,
   `retire_agent`, `rename_agent` (+ drain-check helper), `remove_agent` refusal
   semantics + force, retired-aware validation roster, retired-send refusal,
   optional single-hop forwarding. This is the dependency root.
2. **Epoch primitive (store.py + cli.py)**: `current_epoch()`, `epoch_at_send`
   stamping in `send()` for OPENER_KINDS, `barrier bump` command, `check --epoch`.
3. **Next-owner/action (threads.py + cli.py)**: derive read-only
   `next_owner`/`next_action`, surface in `threads --json` / `sync --json`.
4. **Docs, doctor, version & release polish**: README/SECURITY honesty updates,
   doctor registry hygiene, version bump, CHANGELOG/ROADMAP.

## Complexity Tracking

No charter violations. The one design subtlety (how a barrier yields exactly one
stable epoch id; the three-state `epoch_at_send`) is resolved in `research.md`,
not deferred.
