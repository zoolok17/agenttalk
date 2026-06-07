---
work_package_id: WP04
title: doctor advisory + docs honesty + version 0.18.0
dependencies:
- WP03
requirement_refs:
- FR-009
- NFR-001
- NFR-002
- NFR-003
planning_base_branch: master
merge_target_branch: master
branch_strategy: Planning artifacts for this feature were generated on master. During /spec-kitty.implement this WP may branch from a dependency-specific base, but completed changes must merge back into master unless the human explicitly redirects the landing branch.
subtasks:
- T013
- T014
- T015
agent: "claude"
shell_pid: "36020"
history:
- '2026-06-07: created from approved plan rev2 (fffcb78, Codex pre-code approved)'
authoritative_surface: src/agenttalk/doctor.py
execution_mode: code_change
owned_files:
- src/agenttalk/doctor.py
- tests/test_doctor.py
- README.md
- SECURITY.md
- CHANGELOG.md
- ROADMAP.md
- pyproject.toml
- src/agenttalk/__init__.py
tags: []
---

# WP04 — doctor advisory + docs honesty + version 0.18.0

## Objective

The doctor advisory (FR-009), the two honest doc constraints (C-006/C-008),
and the 0.18.0 version bump. Owned files only.

## Context

- spec FR-009, NFR-001; C-006, C-008; research D8; data-model §6.
- WP01 provides `_process_alive` and `read_waiting`. doctor.py structure:
  `Check`/`Report` dataclasses, `run()` gates config-dependent checks on the
  init check status (the 0.16.0 fix).
- Verify shipped behavior against code, not the spec (docs-honesty is a review
  gate — overclaims get rejected).

**Hard boundaries**: only the 8 owned files. Stdlib only. Full suite green.

## T013 — `doctor` marker pid/liveness advisory (FR-009)

Add a check (or extend the existing per-agent diagnostics) that, for each
agent with a `.waiting` marker present, reports the marker `pid` and whether
`store._process_alive(pid)` is true — framed advisory:
"a process is currently waiting as `<agent>` (PID x, alive/▒dead)". 
- WARN status at most (never error); doctor's exit code is unchanged.
- Explicitly NOT a claim of complete duplicate detection (Codex framing): the
  text says "current waiter", not "all duplicates".
- Additive in `--json` (absent when no marker); one line in plain output.
- Gate it like the other config-dependent checks (skip cleanly when the store
  isn't initialized).
Tests in `tests/test_doctor.py`: marker present with a live pid → advisory
present; no marker → absent; doctor exit code unchanged; no crash when the
marker is malformed (read_waiting → None).

## T014 — Docs honesty (C-006, C-008)

README + SECURITY.md, two short notes:
1. **One window per agent / concurrency unsupported** (C-006): same-agent
   concurrent consumers are unsupported; `advance_cursor`/`mark_thread_seen`/
   `close_thread` are atomic writes, not process-safe read-modify-write;
   0.18.0 warns on a detected live duplicate (best-effort, `wait`) but does
   not enforce. No locking.
2. **Cross-machine clock agreement** (C-008): message ids are timestamp-
   prefixed and ordering is lexical; a store synced across machines with
   skewed clocks can misorder/hide messages; id-shape validation (0.18.0)
   rejects malformed ids but does NOT fix well-formed future-dated ids from
   skew. State it plainly — do not imply 0.18.0 closes skew.
Place them near the existing trust-model / exit-code sections; reuse existing
phrasing patterns, don't invent a new security story.

## T015 — CHANGELOG + ROADMAP + version + gate

1. `CHANGELOG.md`: `## [0.18.0] - <date>` — Fixed: signature-TypeError DoS
   (now quarantinable), malformed-id cursor poison, retired history hidden
   from tail + dashboard, broadcast --resume stuck on retired recipient,
   retired member shown as owed; Added: `agenttalk wait` duplicate-activation
   warning, doctor waiter pid/liveness advisory, broadcast `audience_retired`
   + resume `dropped`; Docs: one-window-per-agent + clock-agreement
   constraints. Note the FR-004 visible change (more messages render).
2. `ROADMAP.md`: header → v0.18.0; record the review-hardening release +
   issue #21 closed; note the two fresh-context reviews as the source.
3. `pyproject.toml` + `src/agenttalk/__init__.py` → `0.18.0`.
4. `pip install -e .`; `python -m agenttalk --version` → `agenttalk 0.18.0`;
   `python -m pytest -q` FULL suite green. `git grep -n "0\.17\.0" README.md`
   → no install-pin hits (bump any).

## Definition of Done

- [ ] Full suite green; `--version` → 0.18.0.
- [ ] Every doc claim traceable to shipped code/test (no overclaim of
  concurrency or skew being "fixed").
- [ ] `git grep "v0\.17\.0" README.md` → no install-pin hits.
- [ ] Only the 8 owned files changed.

## Reviewer guidance (Codex)

Focus: doctor advisory framed as best-effort (not complete detection) + exit
code unchanged; docs honesty (C-006/C-008 NOT overclaimed); CHANGELOG matches
the real 4-WP diff incl. the FR-004 render change; version consistency across
pyproject/__init__/docs.

## Activity Log

- 2026-06-07T21:07:32Z – claude – shell_pid=36020 – Started implementation via action command
- 2026-06-07T21:17:06Z – claude – shell_pid=36020 – WP04 done; 0.18.0; suite 640; ruff clean
- 2026-06-07T21:34:48Z – claude – shell_pid=36020 – Codex approved rev2 (20260607-213426)
