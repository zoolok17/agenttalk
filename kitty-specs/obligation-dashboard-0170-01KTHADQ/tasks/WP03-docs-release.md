---
work_package_id: WP03
title: 'Release: docs, security honesty, version 0.17.0'
dependencies:
- WP02
requirement_refs:
- NFR-004
planning_base_branch: master
merge_target_branch: master
branch_strategy: Planning artifacts for this feature were generated on master. During /spec-kitty.implement this WP may branch from a dependency-specific base, but completed changes must merge back into master unless the human explicitly redirects the landing branch.
subtasks:
- T011
- T012
- T013
agent: "claude"
shell_pid: "79472"
history:
- '2026-06-07: created from approved plan rev2 (8e81ace, Codex pre-code review approved)'
authoritative_surface: README.md
execution_mode: code_change
owned_files:
- README.md
- CHANGELOG.md
- ROADMAP.md
- SECURITY.md
- pyproject.toml
- src/agenttalk/__init__.py
tags: []
---

# WP03 — Release: docs, security honesty, version 0.17.0

## Objective

Document what WP01+WP02 actually shipped — no more, no less — and bump the
version. Owned files only: README.md, CHANGELOG.md, ROADMAP.md, SECURITY.md,
pyproject.toml, src/agenttalk/__init__.py.

## Context

- The shipped behavior is whatever is on the lane HEAD after WP02 — verify
  claims against code, not against the spec (the 0.16.0 lesson: docs honesty
  is a review gate; Codex rejects overclaims).
- `contracts/cli-surface.md` for the surface being documented.
- issue #20 for the motivation paragraph.

## Subtask T011 — README

1. CLI reference: add a `dashboard` row (multi-root obligation dashboard,
   `--store` repeatable, loopback-only, read-only) and refresh the `serve`
   row to mention it now also serves `/dashboard` + `/api/state` and the
   improved bind-failure message.
2. A short "Obligation dashboard" subsection near the `serve` docs:
   one-paragraph what/why, the two-store quickstart invocation, the
   `/api/state` one-liner for automation (`schema_version 1`), and the
   loopback/SSH-tunnel note (reuse the existing phrasing — do not invent a
   new security story).
3. Install pins: bump every `@v0.16.0` install reference to `@v0.17.0`
   (the 0.16.0 WP04 rejection was exactly a stale-pin miss — grep, don't
   skim: `git grep -n "v0\.16\.0" README.md`).

## Subtask T012 — SECURITY.md + CHANGELOG + ROADMAP

1. SECURITY.md: a "Multi-root obligation dashboard (0.17.0)" subsection
   under the web-dashboard section:
   - read-only stays true by construction AND by regression (full-tree
     hash test); name the test.
   - per-route CSP split: `/dashboard` allows self-hosted script + fetch;
     the hostile-body routes (`/messages/<id>`) keep the stricter policy
     byte-identical, test-pinned.
   - multi-root widens the BLAST RADIUS of the loopback wall (N projects
     readable from one port) — restate: no auth, loopback-only, no
     override, SSH-tunnel for remote. Honest claim discipline: do NOT
     claim the dashboard "cannot" leak across roots — say no cross-root
     merging is performed and each root's data is namespaced.
   - `/api/state` exposes subjects/meta but never bodies; bodies remain
     reachable only via root[0]'s detail routes.
2. CHANGELOG: `## [0.17.0] - <date>` — Added: `dashboard` command,
   `/api/state` (schema v1), `/dashboard` + `/static/dashboard.js`,
   multi-root `--store`, per-root error isolation; Changed: bind failures
   exit 2 with actionable message (was: raw traceback path), `/` gains a
   dashboard link; Security: per-route CSP, no-mutation regression.
3. ROADMAP: header to v0.17.0; mark the dashboard/visibility item
   delivered; reference issue #20 closed.

## Subtask T013 — Version bump + full-suite gate

1. `pyproject.toml` version = "0.17.0"; `src/agenttalk/__init__.py`
   `__version__ = "0.17.0"`.
2. `pip install -e .` then `python -m agenttalk --version` → `agenttalk
   0.17.0`; `python -m pytest -q` FULL suite green (the version string
   appears in `/api/state` and server headers — web tests must agree).
3. Cross-check: no doc claims a flag/route that does not exist at HEAD
   (run each documented command's `--help`).

## Definition of Done

- [ ] Full suite green at HEAD; `--version` says 0.17.0.
- [ ] `git grep -n "v0\.16\.0" README.md` → no install-pin hits.
- [ ] Every SECURITY.md claim traceable to a shipped test or code path.
- [ ] No changes outside the six owned files.

## Reviewer guidance (Codex)

Focus: doc-vs-code honesty (overclaim hunt), stale pins, CHANGELOG
completeness vs the actual diff, SECURITY.md blast-radius framing for
multi-root, version consistency across pyproject/__init__/docs.

## Activity Log

- 2026-06-07T16:18:05Z – claude – shell_pid=79472 – Started implementation via action command
