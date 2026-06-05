# Implementation Plan: 0.14.0 Operator Safety

**Branch**: `master` | **Date**: 2026-06-05 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `kitty-specs/operator-safety-0140-01KTBZA1/spec.md`

Planning questions: none open — all design decisions were settled in the
cross-reviewed consults (agenttalk threads 535a091f, 2293cabd; review
52476b64) and grounded against the v0.13.0 source by a 7-reader
verification fleet (file:line evidence recorded in research.md).

## Summary

Close the three operator-safety gaps from the 2026-06-05 production retro:
a **rescind/supersede primitive with an executable pre-action currentness
check** (#12), **root hardening** so two windows can never silently address
different stores (#13), and an **operator-liaison workflow** with a
refuse-on-ambiguity escalation path (#18); plus, capacity permitting,
**reply-in-flight visibility** (#14, sole slip candidate). All additive on
the existing file-backed store, derivation-pure thread model, and
observational state-marker patterns.

## Technical Context

**Language/Version**: Python 3.10+ (matches pyproject), stdlib only (C-001)
**Primary Dependencies**: none at runtime; `pytest` + `ruff` for dev; `hatchling` build backend
**Storage**: existing file-backed store — one JSON file per message under `.agenttalk/messages/`, state files under `.agenttalk/state/` (`<agent>.cursor`, `.heartbeat`, `.waiting`, `.threadstate.json`), config in `.agenttalk/config.json`
**Testing**: pytest, invoking the CLI via the same in-process patterns as `tests/test_cli.py` / `tests/test_threads.py` / `tests/test_coordination.py`; dev-install gotcha applies (`pip install -e .` before testing)
**Target Platform**: Windows-first (production runs there), POSIX-portable
**Project Type**: single project — `src/agenttalk/` package + `tests/`
**Performance Goals**: rescind wake ≤ 2 s on local store (NFR-002); `check` ≤ 1 s at 10k messages (NFR-003) — both trivially satisfiable by the existing poll loop and one `valid_messages()` pass; no new indexing
**Constraints**: C-001..C-010 from spec.md — notably: history immutable, no new control kinds, no new load-bearing state, exit-code contract preserved, governance-free transport, Windows-safe flags
**Scale/Scope**: rosters ≤ ~8 agents, stores ≤ ~10k messages (observed production scale ~600); 4 issues, ~5 WPs expected

## Charter Check

*Skipped — no charter exists at `.kittify/charter/charter.md`.* The standing
constraint set (C-001..C-010) plus SECURITY.md's trust model serve as the
de-facto gates; all four features were checked against them during the
design consults. No violations to justify; Complexity Tracking is empty.

## Project Structure

### Documentation (this feature)

```
kitty-specs/operator-safety-0140-01KTBZA1/
├── spec.md              # done
├── plan.md              # this file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/
│   └── cli-surface.md   # Phase 1 output — new/changed CLI surface + exit codes
└── tasks/               # Phase 2 (/spec-kitty.tasks — NOT this command)
```

### Source Code (repository root)

```
src/agenttalk/
├── cli.py        # all four features: new subcommands (rescind, check, escalate),
│                 #   wait/scoped-wait rescind path, status/sync/threads surfacing,
│                 #   whoami/doctor/init changes, composing --to-request
├── store.py      # KNOWN_KINDS + rescind validation, find_root/AGENTTALK_ROOT,
│                 #   init up-tree guard, operator_facing roster accessor,
│                 #   composing-intent observational marker helpers
├── threads.py    # closed-superseded derivation, escalation pending/answered
│                 #   derivation, reply-in-flight surfacing hooks
├── doctor.py     # multi-store detection, operator_facing diagnostics
└── skills/       # claude/ + codex/ skill updates (lead = single voice,
                  #   workers escalate, rescind/check contracts)

tests/
├── test_cli.py           # extend: rescind/check/escalate CLI behavior, init guard
├── test_threads.py       # extend: closed-superseded + escalation derivation
├── test_coordination.py  # extend: end-to-end rescind race + liaison flow
├── test_doctor.py        # extend: multi-store + liaison diagnostics
└── test_skill_lint.py    # picks up skill doc changes automatically
```

**Structure Decision**: single-project layout unchanged — every feature is
an additive extension of the four existing modules plus their existing test
files. No new modules anticipated except possibly extracting escalation
helpers if `cli.py` growth becomes unwieldy (implementer's call per-WP).

## Phase ordering (mirrors C-010 priority)

1. **WP: rescind core** — kind + validation + thread derivation (#12 part 1)
2. **WP: rescind surfacing** — scoped-wait wake, `check`, sync/threads/status (#12 part 2)
3. **WP: root hardening** — init guard, doctor, AGENTTALK_ROOT, whoami/doctor root-first (#13)
4. **WP: operator liaison** — roster bit, escalate, buckets, diagnostics (#18)
5. **WP: skills + docs + release prep** — skill contracts both CLIs, README, CHANGELOG (cross-cutting)
6. **WP (conditional): intent-to-reply** — composing --to-request + visibility (#14; slips whole if C-004 is threatened)

Exact WP boundaries are decided by `/spec-kitty.tasks`; this ordering is the
dependency truth: 2 depends on 1; 4 reuses 1+2's thread-derivation plumbing
for the escalation bucket; 5 depends on all shipped features; 6 is
independent of everything except 5's doc pass.

## Process

Claude implements (Python + tests) per WP; every WP gets a
`kind=review-request` cross-review by Codex over agenttalk before
acceptance (C-009); a fresh-eyes reviewer runs before release. Release
follows the documented ritual in ROADMAP.md (version bump, CHANGELOG,
tag, Release object, README pin).
