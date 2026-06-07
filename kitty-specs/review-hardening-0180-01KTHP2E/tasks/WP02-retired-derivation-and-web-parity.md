---
work_package_id: WP02
title: Retired-aware derivation + dashboard parity
dependencies:
- WP01
requirement_refs:
- FR-004
- FR-006
planning_base_branch: master
merge_target_branch: master
branch_strategy: Single serial lane from master; squash-merge back at mission end.
subtasks:
- T006
- T007
- T008
history:
- '2026-06-07: created from approved plan rev2 (fffcb78, Codex pre-code approved)'
authoritative_surface: src/agenttalk/threads.py
execution_mode: code_change
owned_files:
- src/agenttalk/threads.py
- src/agenttalk/web.py
- tests/test_threads.py
- tests/test_web.py
tags: []
---

# WP02 — Retired-aware derivation + dashboard parity

## Objective

Stop derivation and the dashboard message routes from mishandling retired
identities: a tombstone is never an owed obligation, and a retired identity's
history renders. `threads.py` + `web.py` + their tests ONLY.

## Context

- spec FR-004 (web half), FR-006; research D6, D7; data-model §3.
- `threads._derive_broadcast` builds `pending` from the frozen `audience`
  without consulting the current retired set; `_derive_next` projects
  `await-reply`/`next_owner` from `pending`.
- `web._all_messages` (web.py) validates against `cfg.get("agents")` (active
  only); the fix is to use the known roster (active ∪ retired), matching
  `store._validated_messages` / `_validated_for_state`. Find the store's
  known-roster accessor (`store._known_roster(cfg)` — used by
  `_validated_for_state` in web.py already for `/api/state`).

**Hard boundaries**: only the 4 owned files. Stdlib only. Don't touch the
frozen `audience` (immutable history, C-003). Full suite green at HEAD.

## T006 — Broadcast pending excludes retired + `audience_retired` (FR-006)

In `threads._derive_broadcast`:
- compute the retired set for this store. `derive_threads` is pure over a
  message list — it does not currently take a roster of retired names. Thread
  the retired set in the cleanest available way: the derivation already
  receives validated messages; pass the retired names into `derive_threads`
  (and through to `_derive_broadcast`) as an additive optional param
  (`retired: set[str] | None = None`, default empty), supplied by the callers
  (CLI/web already have the config). Keep it OPTIONAL and default-empty so no
  existing caller breaks.
- `pending = [a for a in audience if a not in responded and a not in retired]`.
- collect `audience_retired = [a for a in audience if a in retired]`.
- `audience` itself is unchanged.
- In `Thread.to_dict()`, emit `audience_retired` ONLY when non-empty
  (additive, absent otherwise — same pattern as `pending`/`responded`).
- `_derive_next`: a broadcast `await-reply` `next_owner` must be the pending
  list (already retired-free after the above). No retired name can appear.

NOTE on the optional-param threading: if passing `retired` through
`derive_threads` is too invasive for the pure function's signature, the
acceptable alternative is to compute `audience_retired`/filtered `pending`
where the caller has the roster — but the spec wants it in derivation so the
CLI `threads` and the dashboard agree. Prefer the additive-param route; if you
deviate, document why in the PR for Codex.

## T007 — `web._all_messages` → known roster (FR-004 web)

In `web._all_messages`, replace `roster = cfg.get("agents", []) or []` with
the known roster (active ∪ retired) — reuse `store._known_roster(cfg)` exactly
as `_validated_for_state` does elsewhere in web.py. Nothing else in
`_all_messages` changes. This makes `/api/messages`, `/api/messages/<id>`,
`/messages/<id>`, and the index render retired identities' history, matching
the thread panel.

## T008 — Tests

`tests/test_threads.py`:
- `test_broadcast_pending_excludes_retired`: broadcast to [lead,a,b]; a
  replies; b retired → derived thread `pending` excludes b, `audience_retired
  == ["b"]`, `audience` still lists b, `next_owner` (await-reply) does not
  name b.
- `test_audience_retired_absent_when_none`: a clean broadcast → no
  `audience_retired` key (additivity).

`tests/test_web.py`:
- `test_retired_history_renders`: store with [lead,b], b has prior messages,
  retire b → `web._all_messages` returns b's messages (not empty);
  `/api/messages` includes them; `list_invalid_messages` does NOT flag them.
  Pin that the active-roster bug is gone.
- keep all existing web tests green (the additivity gates incl. any
  `_OPEN_THREAD_KEYS` set may need `audience_retired` added — update the gate
  test if it trips, since test_web/test_threads are WP02-owned; if the gate
  lives in test_coordination.py it is WP03-owned — coordinate by leaving that
  to WP03 and noting it).

## Definition of Done

- [ ] `pytest tests/test_threads.py tests/test_web.py -q` green; FULL suite green.
- [ ] Only the 4 owned files changed; frozen `audience` never mutated.
- [ ] `audience_retired` absent when empty.
- [ ] `pip install -e .` before testing.

## Reviewer guidance (Codex)

Focus: the retired-set threading keeps `derive_threads` pure and the optional
param truly default-safe; `pending`/`next_owner` never name a tombstone;
`audience_retired` additivity; web known-roster switch matches
`_validated_for_state` and doesn't widen beyond active∪retired.
