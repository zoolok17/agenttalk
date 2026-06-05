---
work_package_id: WP04
title: Doctor hygiene, docs honesty, version & release
dependencies:
- WP01
- WP03
requirement_refs:
- FR-007
- FR-016
planning_base_branch: master
merge_target_branch: master
branch_strategy: Planning artifacts for this feature were generated on master. During /spec-kitty.implement this WP may branch from a dependency-specific base, but completed changes must merge back into master unless the human explicitly redirects the landing branch.
created_at: '2026-06-05T20:35:00Z'
subtasks:
- T018
- T019
- T020
- T021
- T022
agent: "claude"
shell_pid: "44536"
history:
- date: '2026-06-05T20:35:00Z'
  event: created
  by: claude
authoritative_surface: src/agenttalk/
execution_mode: code_change
owned_files:
- src/agenttalk/doctor.py
- tests/test_doctor.py
- src/agenttalk/__init__.py
- pyproject.toml
- README.md
- SECURITY.md
- CHANGELOG.md
- ROADMAP.md
tags: []
---

# WP04 — Doctor hygiene, docs honesty, version & release

## Objective

Close the release: a doctor registry-hygiene check, the load-bearing **docs
honesty** (the whole point of Phase A — trusted-team safety, NOT authz), and the
version bump + release notes. You own `doctor.py`, `test_doctor.py`,
`__init__.py`, `pyproject.toml`, `README.md`, `SECURITY.md`, `CHANGELOG.md`,
`ROADMAP.md`.

Depends on **WP01** (registry helpers) and **WP03** (final command surfaces — so
docs describe what actually shipped).

Read first: `spec.md` FR-016, `research.md` (inherited constraints),
`contracts/cli-surface.md`, the RFC §"Recommended Phases" / §"Acceptance
Criteria", and the existing `doctor.py` (`_check_store_hygiene`, the to_dict /
`_render_doctor_human` root-first output), plus how 0.15.0 was released
(`CHANGELOG.md`, `ROADMAP.md` Phase 2b entry).

> CI gate: the matrix (py3.10–3.13 × 3 OSes) MUST be green before tagging. That
> is a release-ritual step, not a WP file edit — but do not bump the version in a
> way that implies "released" before the gate passes.

## Subtasks

### T018 — doctor registry hygiene check
**Steps**: Add a check (in the existing doctor check pipeline) that validates the
identity registry health:
- active `agents` and `retired` names are disjoint (should already be guaranteed
  by `load_config`, but doctor reports it as a friendly finding rather than a
  hard load error where possible);
- each `retired` entry is well-formed (`name` safe, `renamed_to` safe-or-null);
- a `renamed_to` that points at a name which is neither active nor a later
  tombstone is a WARNING (lineage dangling), not an error;
- surface counts ("N active, M retired") in the doctor output.
Follow the existing finding/severity idiom (note vs warn) and the root-first
output contract. Keep doctor's exit code semantics as they are (host-env
dependent — see the CI gate note; tests must pin via monkeypatch, not assert
unpinned rc).

### T019 — `tests/test_doctor.py`
Cover: a healthy registry → no registry findings; a dangling `renamed_to` →
WARNING; counts reported. **Pin** any rc assertions with the existing
monkeypatch pattern for skill dirs (do NOT assert rc==0 on an unpinned host — the
0.14.0 red-matrix lesson).

### T020 — README updates
Document the new operator workflow and commands (from `contracts/cli-surface.md`):
`roster retire`/`rename --drain-check`/`remove [--force]`/`forward`,
`barrier bump --scope global`, `check --epoch`, and the `threads/sync --json`
`next_owner`/`next_action` fields. Include the operator-facing "wake a listening
agent from another window" note if a natural spot exists. Keep examples in
PowerShell (Windows-first).

### T021 — SECURITY.md honesty (FR-016 — the point of Phase A)
State plainly:
- This release is **trusted-team safety, not authorization**. It assumes a
  cooperative, non-malicious roster. It does NOT defend against a local peer who
  forges sends, edits `config.json`, or deletes messages.
- `check --epoch` **fails open** against barrier suppression: a writer who
  deletes/withholds a barrier makes `check --epoch` read the latest *surviving*
  barrier and possibly pass. HMAC proves bytes, not presence. Real presence
  hardening is deferred to a later RFC phase (D).
- The identity registry lives in `config.json` and is "no more trustworthy than
  that roster" — trusted-team metadata, not an authenticated authority.
- The `epoch_at_send` three-state (absent = pre-epoch opener; `null` =
  epoch-aware, no barrier yet; `<id>` = stamped) and why `null` is meaningful.
- Retired identities are permanent tombstones (non-rebindable); `remove --force`
  is the sole, warned escape hatch that knowingly breaks historical readability.
Cross-reference the RFC. Do NOT overclaim any guarantee.

### T022 — version bump + release notes
- `src/agenttalk/__init__.py` and `pyproject.toml`: `0.15.0` → `0.16.0`.
- `CHANGELOG.md`: a `0.16.0` section summarizing the registry/retirement, barrier/
  epoch/`check --epoch`, and `next_owner`/`next_action` features, with the
  trusted-team boundary noted.
- `ROADMAP.md`: mark RFC Phase A delivered as 0.16.0 (mirror the Phase 2b /
  0.15.0 entry style); leave Phases B/C/D as future, noting B is the
  operator-gated stdlib-crypto fork.

## Branch Strategy

Planning branch: `master`. Final merge target: `master`. Worktree from
`lanes.json` during `/spec-kitty.implement`.

Implement command: `spec-kitty agent action implement WP04 --agent claude`

## Definition of Done

- T018–T022 complete; `pytest tests/test_doctor.py` green; full `pytest` green.
- Docs make NO claim beyond trusted-team safety; the fail-open and registry-trust
  caveats are explicit (FR-016).
- Version reports `0.16.0`; CHANGELOG + ROADMAP updated.
- No file outside `owned_files` modified.
- Codex cross-review (meta mission + wp_id WP04) approved.
- (Release ritual, after merge) fresh-eyes review + CI matrix GREEN before tag.

## Reviewer guidance (for Codex)

- You authored the RFC: confirm the docs honesty matches the RFC's acceptance
  criteria verbatim in spirit — especially "fails open against suppression",
  "no more trustworthy than the roster", and "`operator_facing` remains advisory".
- Confirm no overclaim: nothing in README/SECURITY implies malicious-peer authz,
  per-agent crypto, or deletion defense.
- Confirm the version bump is consistent across `__init__.py` + `pyproject.toml`.

## Activity Log

- 2026-06-05T21:43:52Z – claude – shell_pid=44536 – Started implementation via action command
- 2026-06-05T21:56:03Z – claude – shell_pid=44536 – doctor hygiene + docs honesty + version 0.16.0; 3 tests; suite 596 pass
