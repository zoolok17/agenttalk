---
work_package_id: WP05
title: Skills docs release prep
dependencies:
- WP04
requirement_refs:
- NFR-004
planning_base_branch: master
merge_target_branch: master
branch_strategy: Planning artifacts for this feature were generated on master. During /spec-kitty.implement this WP may branch from a dependency-specific base, but completed changes must merge back into master unless the human explicitly redirects the landing branch.
subtasks:
- T018
- T019
- T020
- T021
agent: "claude"
shell_pid: "51800"
history:
- date: '2026-06-05T16:40:00Z'
  event: created
  by: claude
authoritative_surface: src/agenttalk/skills/
execution_mode: code_change
owned_files:
- src/agenttalk/skills/**
- README.md
- SECURITY.md
- CHANGELOG.md
- pyproject.toml
- src/agenttalk/__init__.py
tags: []
---

# WP05 - Skills, docs, release prep

## Objective
Teach both CLI flavors, document, stage the version. Ends release-ready;
tag/push/Release/issue-closes stay outside (operator authorization +
the CI gate, NFR-005).

## Context
Skill parity rule binds (claude/*.md vs codex/*/SKILL.md byte-parallel
semantics). Implement:
`spec-kitty agent action implement WP05 --agent claude --mission team-scope-0150-01KTC934`.

## Subtasks

### T018 - skills (both flavors)
Contract deltas, placed in matching sections (not appendices):
- listen/send: a broadcast question that does not concern your role ->
  `reply --to-request <bid> --na` (never placeholder-ack, never
  silence).
- lead: prefer `--to-role` when roles exist; on broadcast exit 5,
  re-send to the missed members with the same request_id or rescind
  the thread; check `agenttalk status` for incomplete-batch warnings
  after any broadcast.
- listen/lead: store hygiene - `prune --invalid --dry-run` to inspect,
  prune to quarantine (recoverable; never hand-delete).

### T019 - README + SECURITY
README: CLI rows (broadcast --to-role + exit 5, reply --na, prune);
sections: role audiences + NA (with the freeze guarantee in user
words), delivery accounting (exit 5 + the no-rollback honesty),
quarantine (recoverable, restore path); exit-code table gains 5.
SECURITY: quarantine moves are recoverable + selection equals the
validation gates (no new trust surface); frozen audience meta is
display/audit (obligations always derived from copies); batch_total is
untrusted meta like everything else.

### T020 - CHANGELOG + versions
0.15.0 entry (production-friction framing: placeholder acks, silent
partial broadcasts, 562 INVALID debris); pyproject + __init__ ->
0.15.0 lockstep.

### T021 - gate
pip install -e . refresh; full pytest; ruff; quickstart.md walk in a
scratch dir (record output); install-skills spot check; NOTE in the
review request: CI matrix green (gh run watch) is a RELEASE step, after
push - cannot be exercised pre-push.

## Definition of Done
- [ ] Skills parallel across flavors; docs complete; versions lockstep
- [ ] Full suite + ruff + quickstart walk green
- [ ] No tag/push/Release performed
- [ ] Codex review approved (wp_id=WP05) + fresh-eyes pre-release review requested

## Reviewer guidance (Codex)
Author-grade docs review: voice match; no overpromising (no rollback,
quarantine manual); exit-code table consistency; skill parity diff.

## Activity Log

- 2026-06-05T17:43:53Z – claude – shell_pid=51800 – Started implementation via action command
- 2026-06-05T17:53:21Z – claude – shell_pid=51800 – Moved to planned
- 2026-06-05T17:58:41Z – claude – shell_pid=51800 – Moved to approved
