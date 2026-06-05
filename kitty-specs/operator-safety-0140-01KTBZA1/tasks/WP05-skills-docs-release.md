---
work_package_id: WP05
title: Skills, docs, release prep
dependencies:
- WP04
requirement_refs:
- NFR-005
planning_base_branch: master
merge_target_branch: master
branch_strategy: Planning artifacts for this feature were generated on master. During /spec-kitty.implement this WP may branch from a dependency-specific base, but completed changes must merge back into master unless the human explicitly redirects the landing branch.
subtasks:
- T022
- T023
- T024
- T025
- T026
agent: "claude"
shell_pid: "71908"
history:
- date: '2026-06-05T13:34:21Z'
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

# WP05 — Skills, docs, release prep

## Objective

Teach both agent CLIs the new operator-safety contracts, document the
release, and stage the version bump. After this WP the repo is one
authorized tag away from shipping 0.14.0.

## Context

Read: contracts/cli-surface.md "Skill-contract deltas" (normative for
T022/T023), the final shipped state of WP01–WP04 (especially whether #14
made the release — every #14 mention below is conditional on that),
ROADMAP.md "Release ritual".

The skills exist in two parallel flavors that must stay semantically
identical: `src/agenttalk/skills/claude/agenttalk.<name>.md` (7 files)
and `src/agenttalk/skills/codex/agenttalk-<name>/SKILL.md` (7 dirs).
Codex syntax is `$agenttalk-<name>`, Claude is `/agenttalk.<name>` —
never mix (standing house rule). `tests/test_skill_lint.py` and
`tests/test_install_skills.py` enforce structure; they are NOT owned
here and must pass unmodified.

**Hard boundary**: tag, push, and GitHub Release creation are NOT in this
WP — they require explicit per-instance operator authorization. This WP
ends with the working tree release-ready.

Implement: `spec-kitty agent action implement WP05 --agent claude`.

## Subtasks

### T022 — Claude-side skill updates [P with T023/T024]

**Steps** — apply the four contract deltas across the 7 Claude skill
files where each is relevant:
1. **check-before-irreversible** (listen, sk-loop, handoff, lead): before
   any irreversible action tied to a request (merge, release, fire-type
   actions), run `agenttalk check --for $SELF --to-request <RID>`; exit 3
   ⇒ hard stop + report back on the thread; exit 4 ⇒ treat as stale,
   re-confirm with the counterparty.
2. **rescind-over-prose** (send, lead, propose): to cancel a tracked
   request, use `agenttalk rescind --to-request` — a prose "ignore my
   last message" moves no thread state. Mention the waiter wakes with
   exit 3.
3. **escalate-not-your-window** (listen, sk-loop, lead): when the roster
   has an operator-facing agent and you are not it, operator questions go
   through `agenttalk escalate`, then `wait --to-request` on the printed
   rid; only if escalate refuses (exit 2) fall back to your own window's
   human. Lead skill gains the single-voice contract: surface pending
   escalations from `sync` to your human with context, relay the answer
   on the same rid, never forward raw noise.
4. **(#14)** intent-to-reply (listen, handoff, consult): when drafting a
   long reply on a known rid, `composing --to-request <RID>` instead of
   hand-built `--meta request_id=...`.
Keep each skill's existing structure/voice; additions go in the matching
procedure/constraint sections, not bolted-on appendices.

### T023 — Codex-side skill updates [P]

**Steps**: mirror T022 1:1 into the 7 `skills/codex/*/SKILL.md` files,
in Codex invocation syntax (`$agenttalk-listen` etc.), preserving each
file's existing shell dialect (Codex-side examples use the same
PowerShell-safe patterns the 0.13.0 pass established). Semantic parity
rule: a reviewer reading claude/X and codex/X side-by-side must find the
same rules with only syntax differing.

### T024 — README + SECURITY [P]

**Steps**:
1. README CLI table: rows for `rescind`, `check`, `escalate`,
   `roster set-operator-facing`; update rows for `wait` (exit 3 outcome),
   `init` (--force / up-tree guard), `whoami`/`doctor` (root-first),
   `composing` (#14 --to-request).
2. README sections: extend the global-cursor/threadstate mental-model
   section with supersession (the D2 rule in user words + check-gate
   contract); new short "Operator liaison" section (assign, escalate,
   answer, the honest enforcement limit); root-resolution paragraph
   gains AGENTTALK_ROOT + precedence; bump install pin only at tag time
   (note for the release step, not now).
3. SECURITY.md: rescind does not change the trust model (it is validated
   content like any message; a forged rescind is gated by the same
   roster/HMAC rules); `operator_facing` is advisory routing metadata —
   explicitly not an authorization boundary (mirror C-007); composing
   marker is observational with the same tamper profile as
   heartbeat/waiting.

### T025 — CHANGELOG + version bumps

**Steps**:
1. CHANGELOG.md: `## [0.14.0] - <date>` in the established voice
   (production-incident framing like 0.12.0's entry): Added (rescind,
   check, escalate, set-operator-facing, AGENTTALK_ROOT, init guard,
   doctor checks, #14 if shipped), Changed (wait exit 3, whoami/doctor
   root-first, skills), Security (trust-model note), and a Fixed line for
   the composing help-text drift (240s→actual) if WP02 fixed it.
2. `pyproject.toml` + `src/agenttalk/__init__.py` → `0.14.0` (keep the
   two in lockstep — the 0.9.0 drift lesson).
3. Issue hygiene note for the release request: closing #12/#13/#18 (+#14)
   happens at release time by the operator-authorized step, with comments
   linking the CHANGELOG section.

### T026 — release gate: full suite + quickstart walk + skill-lint

**Steps**:
1. `pip install -e .` (refresh editable install), then full `pytest -q` —
   green, including skill-lint and install-skills suites picking up the
   new skill content.
2. Execute quickstart.md top-to-bottom in a scratch dir against the
   built CLI; record any divergence as a blocker (either the code or the
   quickstart is wrong — resolve before review).
3. `agenttalk install-skills` locally and spot-check one Claude + one
   Codex skill file landed with the new content.
4. `ruff check` clean (matches repo conventions).

## Definition of Done

- [ ] 14 skill files updated, semantically parallel across flavors
- [ ] README/SECURITY/CHANGELOG complete; versions bumped in lockstep
- [ ] Full suite + ruff + quickstart walk green (record the walk output in the review request)
- [ ] No tag/push/Release performed — working tree is release-ready, awaiting operator authorization
- [ ] Cross-review by Codex approved (`wp_id=WP05`) — request the fresh-eyes pre-release review alongside it per the mission review protocol

## Reviewer guidance (Codex)

You own docs in this project — review T024/T025 as author-grade, not
checker-grade: voice match with existing README/CHANGELOG, no
overpromising (especially around enforcement language for the liaison),
exit-code table consistency with data-model §7, skill parity across
flavors (pick two files and diff semantics line by line), and the
honest-limit phrasing for turn-cost (skills must not promise turn-free
waits).

## Activity Log

- 2026-06-05T15:30:26Z – claude – shell_pid=71908 – Started implementation via action command
- 2026-06-05T15:37:21Z – claude – shell_pid=71908 – Release-ready; gate green; awaiting Codex + fresh-eyes reviews
