---
work_package_id: WP01
title: Store + doctor foundation
dependencies: []
requirement_refs:
- FR-004
- FR-005
- FR-006
- FR-007
- FR-008
- FR-009
planning_base_branch: master
merge_target_branch: master
branch_strategy: Planning artifacts for this feature were generated on master. During /spec-kitty.implement this WP may branch from a dependency-specific base, but completed changes must merge back into master unless the human explicitly redirects the landing branch.
base_branch: kitty/mission-coordination-polish-0240-01KTMG5Z
base_commit: 2533c504fd8d3ea930cacbb908df318019d0005e
created_at: '2026-06-08T21:07:17.643725+00:00'
subtasks:
- T001
- T002
- T003
- T004
- T005
shell_pid: '41124'
history:
- Created by /spec-kitty.tasks for coordination-polish-0240-01KTMG5Z
authoritative_surface: src/agenttalk/store.py
execution_mode: code_change
owned_files:
- src/agenttalk/store.py
- src/agenttalk/doctor.py
- tests/test_store.py
- tests/test_doctor.py
tags: []
---

# WP01 — Store + doctor foundation

## Objective

Implement the roster-state foundation for the escalation fallback: an **at-most-one-lead**
invariant on `store.set_role`, a `store.sole_lead()` resolver, and a `doctor` check that
warns when a multi-agent team has no human-facing escalation target. No CLI changes here
(that's WP02). This is the MVP core; it has no dependencies.

## Context

- agenttalk is a stdlib-only file-backed message bus. Roster roles live in
  `.agenttalk/config.json` under `roles` (`{agent: role}`); the liaison is the separate
  single-slot `operator_facing`.
- `store.set_role(name, role)` (store.py:938) currently writes `roles[name] = role`,
  calls `validate_roles`, and writes config — all inside `with self._config_lock():`.
- `store.operator_facing()` (store.py:986) returns the liaison or None (None if unset or
  not in roster). `operator_facing_raw()` (store.py:976) returns the configured value
  without a roster check (doctor uses it to distinguish "unset" from "stale").
- `doctor.py` runs a list of checks and returns a report; checks are appended
  conditionally (an absent check means "nothing to report"). Follow the existing Check
  shape in that file (status levels like ok/warn/error).
- The role `lead` is advisory routing metadata, exactly like the liaison — it never
  affects message validity, thread closure, or authorization.

## Subtasks

### T001 — `store.set_role`: at-most-one-lead invariant

**Purpose**: setting `role=lead` on an agent must guarantee at most one lead, moving the
designation atomically when one already exists.

**Steps** (all inside the existing `with self._config_lock():` block in `set_role`):
1. Keep the existing roster-membership check and `validate_roles` call.
2. Determine whether the requested `role` is the lead role: compare
   `role.casefold() == "lead"` (FR-007, case-insensitive). Store the role value
   **as given** (do not lowercase what you persist) — only the comparison is folded.
3. If it is the lead role:
   - Find any existing agent X (≠ name) whose current role casefolds to `"lead"`.
   - If X exists: remove X's lead role (demote — `roles.pop(X, None)` or set to the
     team's "no role" representation consistent with how roles are cleared elsewhere)
     and set `roles[name] = role` (promote). One config write (FR-004, atomic).
   - If `name` is already the lead (its current role casefolds to lead): idempotent
     no-op success — do not record a demotion of self (FR-006).
   - If no existing lead: just set `roles[name] = role`.
4. Communicate the demoted agent to the caller **without breaking the return contract**.
   `set_role` currently returns `cfg`. Keep returning `cfg`, but make the demoted name
   discoverable — e.g. stash it on a small attribute/result the CLI can read, OR (simpler
   and preferred) have `set_role` return `cfg` as before and add a sibling that the CLI
   calls. Choose the minimal approach; document it in the docstring. The CLI half of the
   notice is WP02 — here just make the demoted name available.
5. Zero leads remains valid: setting a non-lead role on the current lead, or clearing it,
   must not error and must leave the team with no lead (FR-008).

**Validation**: existing `validate_roles` still passes; never two leads after any
`set_role`. Role value stored verbatim; comparison case-insensitive.

### T002 — `store.sole_lead() -> str | None`

**Purpose**: resolve the single lead for escalation fallback (WP02 consumes it).

**Steps**:
1. Add a read-only method `sole_lead(self) -> str | None`.
2. Read `roles` from config; collect agents whose role casefolds to `"lead"` AND who are
   in the active roster.
3. Return the single such agent if exactly one; return `None` for zero **and** for the
   legacy >1 case (ambiguity reads as "no unambiguous lead" — defensive, per research D3).

**Validation**: returns the lead when exactly one; None for zero; None when two are
present (simulate a hand-built config with two lead rows).

### T003 — `doctor.py`: no-human-facing-target check

**Purpose**: warn a team that has left itself with no escalation destination, before an
escalation is ever attempted (FR-009).

**Steps**:
1. Add a check that fires only when the active roster has **≥2 agents** AND there is
   neither a resolvable liaison (`store.operator_facing()` is None) NOR a sole lead
   (`store.sole_lead()` is None).
2. On that condition, emit a **warning-level** Check whose message names BOTH remediation
   commands: `agenttalk roster set-operator-facing <agent>` and
   `agenttalk roster set-role <agent> lead`.
3. When a liaison OR a sole lead exists, the check is **absent or ok** (do not emit a
   warning). A **solo** roster (<2 agents) must never warn.
4. Match the conditional-append pattern already used for advisory checks in this file
   (return the Check or None; append only when present).

**Validation**: warns on the no-target multi-agent case; absent/ok when a liaison exists;
absent/ok when a sole lead exists; never warns a single-agent roster.

### T004 — tests: `tests/test_store.py`

Cover: (a) setting a second lead demotes the first and leaves exactly one (FR-004);
(b) demoted agent is reported to the caller (FR-005 store half); (c) setting lead on the
current lead is an idempotent no-op (FR-006); (d) `Lead`/`LEAD` are treated as the lead
role (FR-007); (e) clearing/replacing the lead's role yields zero leads without error
(FR-008); (f) `sole_lead()` returns the lead for one, None for zero, None for two.

### T005 — tests: `tests/test_doctor.py`

Cover the four FR-009 cases: warn (≥2 agents, no liaison, no lead); absent/ok (liaison
set); absent/ok (sole lead set); no warn (solo roster).

## Test Strategy

pytest, stdlib only. Build stores in a tmp dir; manipulate roster via the public store
API where possible, and via direct config for the legacy >1-lead case. Assert on the
returned report/Check objects and store return values — not on log text where avoidable.

## Definition of Done

- FR-004..009 implemented as described; `pytest tests/test_store.py tests/test_doctor.py -q`
  green; `ruff check src tests` clean.
- No two leads possible via `set_role`; zero leads valid; solo never warned.
- `set_role` return contract unchanged for existing callers (additive only).
- No edits outside `owned_files`.

## Branch Strategy

Planning base **master**; final merge target **master**. The execution worktree for this
WP's lane is allocated from `lanes.json` at finalize time.

## Reviewer Guidance

- Confirm the lead comparison is case-insensitive but storage is verbatim.
- Confirm atomicity: demote+promote is one config write inside the lock.
- Confirm `sole_lead()` returns None on the legacy two-lead config (not a guess).
- Confirm the doctor check is absent (not error/ok-noise) on healthy and solo rosters.
- Confirm no message file is touched (history immutable; config-only).

## Implementation command

`spec-kitty agent action implement WP01 --agent <name>`
