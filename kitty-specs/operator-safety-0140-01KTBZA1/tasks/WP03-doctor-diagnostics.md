---
work_package_id: WP03
title: 'Diagnostics: doctor multi-store + liaison checks'
dependencies:
- WP02
requirement_refs:
- FR-007
- FR-009
- FR-011
planning_base_branch: master
merge_target_branch: master
branch_strategy: Planning artifacts for this feature were generated on master. During /spec-kitty.implement this WP may branch from a dependency-specific base, but completed changes must merge back into master unless the human explicitly redirects the landing branch.
subtasks:
- T016
- T017
- T018
agent: "claude"
shell_pid: "348"
history:
- date: '2026-06-05T13:34:21Z'
  event: created
  by: claude
authoritative_surface: src/agenttalk/
execution_mode: code_change
owned_files:
- src/agenttalk/doctor.py
- tests/test_doctor.py
tags: []
---

# WP03 — Diagnostics: doctor multi-store + liaison checks

## Objective

`doctor` becomes the place where the two silent misconfigurations of the
production retro are loud: multi-store split-brain layouts (#13) and
broken liaison designations (#18). Plus the root-first output rule.

## Context

Read: contracts/cli-surface.md (doctor section), research.md D4/D5,
data-model.md §3/§6. WP01's `find_stores_upward` and
`operator_facing_raw()/operator_facing()` are your inputs; WP02 already
ships `cmd_doctor`'s wiring as-is (cli.py:1309 passes root) — **all your
changes live inside doctor.py's check functions** (tests excepted). If a
wiring change in cli.py seems required, coordinate via review — do not
edit cli.py (WP02 ownership).

Branch strategy: planning base `master`, merge target `master`, lane
worktree from lanes.json. Implement:
`spec-kitty agent action implement WP03 --agent claude`.

Production context worth keeping in mind: the band lost 30 minutes to two
windows on two stores with zero diagnostics; the June-3 incident came
from liaison conventions decaying invisibly. Doctor's job after this WP:
one command that makes both visible.

## Subtasks

### T016 — multi-store detection + root-first output (#13, FR-007/009)

**Steps**:
1. Resolved root becomes doctor's FIRST output line: `root: <path>` (and
   first key in any structured output it produces). Today doctor receives
   the root (doctor.py:63-82 area) — surface it before any check output.
2. New check `check_multi_store(cwd)`: call `find_stores_upward(cwd)`;
   - 0 stores: INFO "no store found from <cwd> upward" (only when doctor
     is run rootless; with a working root this check still runs from cwd
     to catch *other* stores).
   - 1 store: OK line naming it.
   - ≥2 stores: WARN/FAIL line naming every store path in walk order +
     the split-brain explanation + remediation ("all windows must agree:
     pass --root <intended> or set AGENTTALK_ROOT; see also init --force
     provenance").
3. Also flag the case where the *resolved* root differs from the first
   store `find_stores_upward(cwd)` would yield (i.e., env/flag points
   somewhere the walk would not have gone — informational, not a
   failure: "root pinned by flag/env, walk would have chosen <other>").

**Validation**: T018 — 0/1/2-store trees; pinned-root-differs case;
first-line assertion.

### T017 — liaison diagnostics (#18, FR-011)

**Steps**:
1. New check `check_operator_facing(store)`:
   - not configured: INFO (teams without a liaison are legitimate) —
     but WARN instead when escalation traffic exists (any valid message
     with `meta.needs_operator` — cheap scan).
   - configured + in roster: OK, name it.
   - configured + NOT in roster (use `operator_facing_raw()` vs
     `operator_facing()` difference): FAIL with the raw value and
     remediation (`roster set-operator-facing <valid>` or `--clear`).
   - configured + in roster + heartbeat stale (reuse the status staleness
     rule): WARN "liaison <name> last seen <age> — escalations may sit
     unread".
2. Wording rule: diagnostics may warn loudly but must never claim
   enforcement (C-007) — phrase as routing/visibility facts.

**Validation**: T018 — all four states; the needs_operator-traffic
upgrade; stale-heartbeat WARN.

### T018 — tests: test_doctor.py extensions

**Steps**:
1. Multi-store: tmp trees with 0/1/2 stores; assert names + ordering +
   first-line root rule. Windows path quirks: build with pathlib only.
2. Liaison: four states above; assert FAIL only for
   configured-not-in-roster; assert no wording implies enforcement
   (negative grep for "enforce").
3. NFR-001: existing doctor tests untouched and green.

**Validation**: `pip install -e .`, `pytest tests/test_doctor.py -q`
green; full suite green.

## Definition of Done

- [ ] Three subtasks complete; doctor output starts with `root:`
- [ ] Multi-store layouts produce named, actionable warnings; single-store stays quiet
- [ ] Liaison states OK/INFO/WARN/FAIL exactly as specified; no enforcement language
- [ ] No edits outside doctor.py + tests/test_doctor.py
- [ ] Cross-review by Codex approved (`wp_id=WP03`)

## Reviewer guidance (Codex)

Attack: false-positive potential of multi-store WARN in legitimate nested
setups (the deliberate `init --force` case — is the message fair?), the
escalation-traffic scan cost on big stores (must be one pass), wording
that could read as enforcement, root-first ordering consistency between
doctor and whoami (whoami is WP02's — flag mismatches, don't fix here).

## Activity Log

- 2026-06-05T14:56:14Z – claude – shell_pid=348 – Started implementation via action command
- 2026-06-05T14:59:53Z – claude – shell_pid=348 – Doctor diagnostics complete; full suite green
- 2026-06-05T15:08:04Z – claude – shell_pid=348 – Moved to planned
- 2026-06-05T15:12:45Z – claude – shell_pid=348 – Review passed: root-first doctor contract fixed on human and JSON surfaces; targeted and full pytest suites passed.
