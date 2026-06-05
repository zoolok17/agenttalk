---
work_package_id: WP01
title: 'Engine: store + threads'
dependencies: []
requirement_refs:
- FR-001
- FR-002
- FR-011
- FR-012
planning_base_branch: master
merge_target_branch: master
branch_strategy: Planning artifacts for this feature were generated on master. During /spec-kitty.implement this WP may branch from a dependency-specific base, but completed changes must merge back into master unless the human explicitly redirects the landing branch.
base_branch: kitty/mission-team-scope-0150-01KTC934
base_commit: 1da22861f2ac8188841222473eaf6a4a9987a1e8
created_at: '2026-06-05T16:33:40.067173+00:00'
subtasks:
- T001
- T002
- T003
- T004
- T005
shell_pid: '71028'
history:
- date: '2026-06-05T16:40:00Z'
  event: created
  by: claude
authoritative_surface: src/agenttalk/
execution_mode: code_change
owned_files:
- src/agenttalk/store.py
- src/agenttalk/threads.py
- tests/test_store.py
- tests/test_threads.py
- tests/test_teams.py
tags: []
---

# WP01 - Engine: store + threads

## Objective
Pure-logic foundations for 0.15.0: role audience resolution (#15),
quarantine machinery (#17), NA/batch derivation labels (#15/#16). NO
cli.py changes - expose functions WP02 wires up.

## Context
Read research.md D1-D5 and data-model.md. Constraints: C-002 (move-only,
valid files untouched), C-003 (no new kinds), C-004 (derivation never
reads live config). Implement: `spec-kitty agent action implement WP01 --agent claude --mission team-scope-0150-01KTC934`.

## Subtasks

### T001 - store: resolve_role_audience (#15, D1)
`Store.resolve_role_audience(role, *, exclude=None) -> list[str]`:
members whose `roles()[agent] == role`, sender excluded, order =
roster order, de-duped. ValueError on: unknown role (name the known
roles), or empty-after-exclude. Sibling of `resolve_audience` - do NOT
overload it (role/group collision ambiguity is forbidden by spec edge
case). Validation: T004.

### T002 - store: quarantine machinery (#17, D5)
1. Refactor the gate walk: extract the loop body of
   `list_invalid_messages` so a sibling
   `list_invalid_message_paths() -> list[tuple[Path, str]]` shares the
   LITERAL same gates (FR-011) - one helper, two views; zero behavior
   change to the existing method (NFR-001).
2. `quarantine_dir` property = `self.dir / "quarantine"` (NOT under
   messages_dir - invisible to scanning by construction).
3. `Store.quarantine_invalid(*, dry_run=False) -> list[dict]` - each
   `{"id": stem, "reason": r, "from": str(src), "to": str(dst)}`;
   on move: mkdir parents, collision-suffix like `_archive_session`
   (never overwrite), `shutil.move`. dry_run computes dst without
   moving. Valid files NEVER touched (selection = the shared gates).
Validation: T004 (incl. byte-identity of valid files + repeated-prune
collision safety).

### T003 - threads: NA labels + batch/audience passthrough (#15/#16, D2/D3)
1. `_derive_broadcast`: track members whose closing response carries
   `meta.response == "not-applicable"` -> new Thread field
   `responded_na: list[str]` (subset of responded; additive in to_dict
   ONLY when non-empty).
2. Pairwise path: when the thread is replay-terminal and the terminal
   event message carried `response=not-applicable` -> Thread field
   `na_response: bool = False` (to_dict emits only when True).
   Closure mechanics UNCHANGED - labeling only, exactly like 0.14.0's
   operator_state.
3. Broadcast Thread rows pass through `batch_total` (int|None, parsed
   from opener meta; None when absent) and `audience_kind` (str|None) -
   additive in to_dict only when present. These power WP02's
   incomplete-batch warning and displays; derivation of obligations
   STAYS opener-copy-based (D2 - do not touch the audience computation).
Validation: T005.

### T004 - tests: store matrix
test_teams.py: role resolution (happy, unknown role names known roles,
empty-after-exclude, order/dedupe, sender exclusion).
test_store.py: quarantine - seed invalid files (unknown kind via raw
file write, out-of-roster sender), assert dry-run moves nothing +
lists exactly status's invalid set; real run moves exactly those, store
scanning no longer sees them, `quarantine/` holds them; collision on
repeated prune never overwrites; zero-invalid no-op; VALID files
byte-identical before/after (hash sweep); selection == 
list_invalid_messages ids (FR-011 lockstep).

### T005 - tests: threads labels/freeze/batch
test_threads.py: responded_na both perspectives; na_response pairwise
(emitted only when True); plain threads emit NONE of the new keys
(strict additivity); batch_total/audience_kind passthrough; freeze
independence - derivation result identical before/after a roles-map
change (structural C-004 guard); closure unchanged when the NA meta is
present (label only).

## Definition of Done
- [ ] pytest test_store/test_threads/test_teams green; full suite green; ruff clean
- [ ] No cli.py/doctor.py changes; no new kinds; CONTROL_KINDS untouched
- [ ] FR-011 lockstep test present (shared gates)
- [ ] Codex review approved (wp_id=WP01)

## Reviewer guidance (Codex)
Attack: gate-sharing refactor must be behavior-identical for
list_invalid_messages (diff the outputs on a seeded store); quarantine
must be unreachable by message scanning; NA labels must not alter any
closure outcome (run the 0.14.0 closure tests mentally against the
diff); to_dict additivity (absent-not-null).
