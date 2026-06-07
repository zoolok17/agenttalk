---
work_package_id: WP02
title: Docs + version 0.19.0
dependencies:
- WP01
requirement_refs:
- NFR-003
planning_base_branch: master
merge_target_branch: master
branch_strategy: Single serial lane from master; squash-merge back at mission end.
subtasks:
- T008
- T009
history:
- '2026-06-07: created from approved plan (5dc70e4, Codex pre-code approved 08f126ef)'
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

# WP02 — Docs + version 0.19.0

## Objective

Document the polished dashboard and bump the version. Owned files only:
README.md, CHANGELOG.md, ROADMAP.md, pyproject.toml, src/agenttalk/__init__.py.

## Context

- The shipped behavior is whatever WP01 landed — verify claims against code,
  not the spec (docs-honesty is a review gate; overclaims get rejected).
- contracts/api-surface.md for the additive `/api/state` keys; spec for the
  client behavior.

## T008 — README dashboard section

In the existing "The obligation dashboard (0.17.0)" section (or adjacent),
add a short 0.19.0 note: the dashboard now shows a **hierarchical team view**
(liaison/lead on top, developers left, reviewers right), per-agent **stats**
(last-seen, sent, received, owes, composing), a **who-talks-to-whom**
conversation panel, and **manual refresh + auto-refresh toggle**. Mention the
additive `/api/state` keys for automation: per-agent `sent`/`received`, per-
root `edges` (`{from,to,count}`, top-50, with `edges_truncated`/`edge_limit`).
Reuse existing phrasing; keep the read-only / loopback-only framing intact.
Do NOT bump install pins unless they reference a version (this release adds no
new install-pin guidance; if `@v0.18.0` pins exist, bump to `@v0.19.0` — grep:
`git grep -n "v0.18.0" README.md`).

## T009 — CHANGELOG + ROADMAP + version + gate

1. `CHANGELOG.md`: `## [0.19.0] - <date>` — Added: per-agent `sent`/`received`
   + per-root `edges` (+truncation) on `/api/state` (additive, schema_version
   stays 1); hierarchical roster, agent cards, who-talks-to-whom conversation
   panel, manual refresh button + auto-refresh toggle on `/dashboard`.
   Unchanged: routes, CSP, read-only, exit codes. Note it's presentation
   polish, bus-native only (no spec-kitty stats).
2. `ROADMAP.md`: header → v0.19.0; record the dashboard-polish release +
   issue #22 closed.
3. `pyproject.toml` + `src/agenttalk/__init__.py` → `0.19.0`.
4. `pip install -e .`; `python -m agenttalk --version` → `agenttalk 0.19.0`;
   `python -m pytest -q` FULL suite green; `git grep -n "v0.18.0" README.md`
   → bump any install pins.

## Definition of Done

- [ ] Full suite green; `--version` → 0.19.0.
- [ ] Every doc claim traceable to shipped code/test (no overclaim).
- [ ] `git grep "v0.18.0" README.md` → no install-pin hits.
- [ ] Only the 5 owned files changed.

## Reviewer guidance (Codex)

Focus: doc-vs-code honesty; CHANGELOG matches the WP01 diff (additive keys +
client features); schema_version-stays-1 stated; version consistency across
pyproject/__init__/docs; no overclaim (bus-native only, no spec-kitty).
