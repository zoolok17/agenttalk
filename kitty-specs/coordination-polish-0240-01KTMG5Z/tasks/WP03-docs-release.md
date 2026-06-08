---
work_package_id: WP03
title: Docs + version 0.24.0
dependencies:
- WP01
- WP02
requirement_refs:
- NFR-002
- NFR-004
planning_base_branch: master
merge_target_branch: master
branch_strategy: Planning artifacts for this feature were generated on master. During /spec-kitty.implement this WP may branch from a dependency-specific base, but completed changes must merge back into master unless the human explicitly redirects the landing branch.
subtasks:
- T011
- T012
history:
- Created by /spec-kitty.tasks for coordination-polish-0240-01KTMG5Z
authoritative_surface: README.md
execution_mode: code_change
owned_files:
- README.md
- CHANGELOG.md
- ROADMAP.md
- pyproject.toml
- src/agenttalk/__init__.py
tags: []
---

# WP03 — Docs + version 0.24.0

## Objective

Release packaging for 0.24.0: bump the version everywhere, refresh README install pins,
and write the CHANGELOG/ROADMAP entries. Final WP; depends on WP01 and WP02 so the
CHANGELOG reflects what actually shipped.

## Context

- Version lives in `pyproject.toml` (`version = "..."`) and `src/agenttalk/__init__.py`
  (`__version__ = "..."`). Both must read `0.24.0`.
- README has tag-pinned install lines (`@v0.23.1`) in two places plus a "Replace `v…`"
  example line — bump all to `v0.24.0`.
- CHANGELOG follows Keep-a-Changelog; newest section on top, under the header block.
- ROADMAP has a dated header line for the latest release.

## Subtasks

### T011 — Version bump + README pins

**Steps**:
1. `pyproject.toml`: `version = "0.24.0"`.
2. `src/agenttalk/__init__.py`: `__version__ = "0.24.0"`.
3. README: replace the `@v0.23.1` install pins (two occurrences) and the "Replace
   `v0.23.1`" example with `v0.24.0`.

**Validation**: `python -m agenttalk --version` → `agenttalk 0.24.0`;
`git grep -n "v0.23" README.md` shows no remaining install-pin hits.

### T012 — CHANGELOG + ROADMAP

**Steps**:
1. Add a `## [0.24.0] - 2026-06-08` section to CHANGELOG with:
   - **Added**: `escalate` falls back to the team lead when no liaison is configured;
     an at-most-one-`lead` roster invariant (switching the lead is one atomic
     demote+promote, no `--force`); a `doctor` warning when a multi-agent team has no
     human-facing escalation target; a `wk-` correlation id on `wake` messages; a soft
     owed-inbound pre-send warning when you'd talk over an open decision you owe a peer.
   - **Unchanged / honesty notes**: the `lead` role and the `operator_facing` liaison
     stay distinct concepts (liaison is still the primary escalation target, lead is only
     the fallback); the invariant is **at-most-one** lead, not exactly-one — zero leads
     remains valid and solo/pair runs are never forced to have a lead; `escalate` still
     exits 2 when there is genuinely no target; message history, message schema, and
     exit codes are unchanged.
   - **If T009 (owed-inbound warning) was cut** per C-004: remove its bullet from Added
     and note it as deferred.
2. Update the ROADMAP header line to reference v0.24.0 (coordination polish from the
   `agenttalk-improvements.md` production feedback: 3.1 escalate lead-fallback, 3.3 wake
   correlation id, 3.2 owed-inbound warning).

**Validation**: CHANGELOG section matches the merged WP01+WP02 behavior; version
consistent across pyproject/__init__/README.

## Definition of Done

- Version `0.24.0` in pyproject + `__init__`; README pins bumped; CHANGELOG + ROADMAP
  updated; `ruff check` clean. CHANGELOG truthfully reflects whether the owed-inbound
  warning shipped or was cut.
- No edits outside `owned_files`.

## Branch Strategy

Planning base **master**; final merge target **master**. Depends on WP01+WP02; branch
from their merged lane base. Execution worktree allocated from `lanes.json`.

## Reviewer Guidance

- Version consistent across all three surfaces.
- CHANGELOG claims match the actual diff (no overclaiming; cut items not listed as
  shipped).
- README has no stale `v0.23` install pins.

## Implementation command

`spec-kitty agent action implement WP03 --agent <name>`
