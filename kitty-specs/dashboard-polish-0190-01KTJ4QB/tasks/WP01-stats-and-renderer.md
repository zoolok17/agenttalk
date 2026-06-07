---
work_package_id: WP01
title: Server stats + dashboard renderer
dependencies: []
requirement_refs:
- FR-001
- FR-002
- FR-003
- FR-004
- FR-005
- FR-006
- FR-007
- FR-008
- NFR-001
- NFR-002
planning_base_branch: master
merge_target_branch: master
branch_strategy: Planning artifacts for this feature were generated on master. During /spec-kitty.implement this WP may branch from a dependency-specific base, but completed changes must merge back into master unless the human explicitly redirects the landing branch.
base_branch: kitty/mission-dashboard-polish-0190-01KTJ4QB
base_commit: 86e99f18e8f74e11c66635d4e0af15f8ba7aa313
created_at: '2026-06-07T23:05:07.329984+00:00'
subtasks:
- T001
- T002
- T003
- T004
- T005
- T006
- T007
shell_pid: "80276"
agent: "claude"
history:
- '2026-06-07: created from approved plan (5dc70e4, Codex pre-code approved 08f126ef)'
authoritative_surface: src/agenttalk/web.py
execution_mode: code_change
owned_files:
- src/agenttalk/web.py
- tests/test_web.py
tags: []
---

# WP01 — Server stats + dashboard renderer

## Objective

The whole operator-facing feature, inside `src/agenttalk/web.py` and
`tests/test_web.py` ONLY: additive `/api/state` stats (`sent`/`received` per
agent, `edges` per root), and the richer `/dashboard` client (hierarchical
roster, agent cards, conversation panel, refresh controls) in the embedded
`_DASHBOARD_JS` constant + `render_dashboard` shell.

## Context (read first)

- spec FR-001..008, NFR-001/002, C-001..008; research D1–D7; data-model
  (the additive shapes + the layout convention table); contracts/api-surface.md.
- **Verified code anchors:**
  - `_agent_entries(store, cfg, msgs, liaison)` at `web.py:763` already
    receives the validated `msgs` list — `sent`/`received` are one pass over
    it. Emit next to `unread`.
  - `_root_state` at `web.py:808` has `msgs` and `threads_rows` — `edges` is
    one Counter there. Degraded roots (the `except` branch) keep
    `{label, path, errors}` with NO partial fields (Codex note: stats/edges
    are HEALTHY-root only).
  - `render_dashboard` at `web.py:304` is the near-empty shell; `_DASHBOARD_JS`
    at `web.py:338` is the renderer (helpers `el()`, `renderRoot`, `poll`,
    `setInterval`). `/dashboard` CSP `_DASHBOARD_CSP` (`web.py:112`) already
    allows `script-src 'self'; connect-src 'self'`.
- **Hard boundaries:** only the 2 owned files. Stdlib only (C-001 — renderer
  stays the embedded constant, NO standalone static file). Read-only — zero
  writes (C-003). Per-route CSP byte-identical (C-004). No `innerHTML` with bus
  data (C-005). `schema_version` stays 1 (NFR-001). Full suite green at HEAD.

## T001 — Additive `sent`/`received` (FR-001, D1)

In `_agent_entries`, before the agent loop, build two counters over `msgs`
(use `collections.Counter`; import if not present): `sent[m.sender]`,
`received[m.recipient]`. In the loop emit `e["sent"] = sent.get(a, 0)` and
`e["received"] = received.get(a, 0)` — **always present integers** (0 is data).
No new scan (msgs is already passed in).

## T002 — Additive `edges` + truncation (FR-002/003, D2)

In `_root_state`'s HEALTHY path (after `msgs` is computed), build a Counter
over `(m.sender, m.recipient)` for every validated message where
`m.sender != m.recipient` (exclude self; INCLUDE broadcast fan-out copies).
Sort items by `count` desc then `(from, to)` for determinism; take top 50:
```python
out["edges"] = [{"from": f, "to": t, "count": c}
                for (f, t), c in sorted(pairs.items(),
                    key=lambda kv: (-kv[1], kv[0]))[:50]]
if len(pairs) > 50:
    out["edges_truncated"] = True
    out["edge_limit"] = 50
```
`edges` always present (may be `[]`); the truncation keys absent unless capped.
Do NOT add edges to the degraded `except` branch (errors-as-data stays).

## T003 — `render_dashboard` shell: control bar + containers (FR-008, D5)

In `render_dashboard`, add a control bar BEFORE `<div id="roots">`:
```html
<div id="controls">
  <button id="refresh-btn" type="button">Refresh</button>
  <label><input type="checkbox" id="autorefresh" checked> auto-refresh</label>
</div>
```
No inline `onclick`/`onchange` (C-004 — wired in JS). Keep the existing
`<noscript>` and the per-root `<section>` placeholders. Optionally add a
roster/conversation container per section if the JS needs stable mount points;
keep it minimal and label-escaped as today.

## T004 — `_DASHBOARD_JS`: hierarchical roster + cards + conversation (FR-004/005/006/007, D3/D4/D6)

Extend the renderer (keep `'use strict'`, the `el()` helper, createElement/
textContent ONLY — no innerHTML with data):
- **Role classifier** `classify(agent)` → `'top'|'left'|'right'|'center'`
  (data-model §3 / D4): `operator_facing===true` OR role contains `lead` →
  top; role contains `dev`/`eng`/`impl` → left; `review`/`qa`/`audit` → right;
  else center. Case-insensitive substring; first match wins in that order.
- **Hierarchical roster** per root: a top band (centered cards for `top`), then
  a 3-column row (left / center / right). Each agent → a **card** showing
  name, role + groups, last-seen age (from `last_seen_age_seconds`), `sent`,
  `received`, **owes** (count this root's `threads` where `next_owner === name`,
  or membership when `next_owner` is an array), and a composing badge when
  `composing` present.
- **Conversation panel** below the roster: render `edges` as `from → to
  (count)` rows; when `edges_truncated`, label "showing top {edge_limit} of
  more". Keep the existing open-threads table.
- Degraded roots: keep showing the error banner (existing behavior), no cards.

## T005 — `_DASHBOARD_JS`: refresh controls (FR-008, D5)

Replace the bare `poll(); setInterval(poll, POLL_MS)` tail with:
- one `poll()` (unchanged fetch→render).
- `var timer = null;` `function startAuto(){ if(!timer) timer =
  setInterval(poll, POLL_MS); }` `function stopAuto(){ if(timer){
  clearInterval(timer); timer = null; } }`.
- wire via `addEventListener` (NO inline handlers): the `#refresh-btn` click →
  `poll()`; the `#autorefresh` change → checked ? `startAuto()` : `stopAuto()`.
- initial: `poll()`; if the checkbox is checked, `startAuto()`.
- Toggling/clicking must NOT reload the page or reset scroll (no
  `location.reload`, no full re-mount that loses scroll — update in place as
  the renderer already does).

## T006 — Tests: `/api/state` additive keys + healthy/degraded (FR-001/002/003, NFR-001)

In `tests/test_web.py` (extend, don't rewrite the existing exact-key tests):
- `sent`/`received` present + correct for a small known traffic pattern.
- `edges` present, correct counts, self-excluded, fan-out-included, sorted
  desc, capped at 50 with `edges_truncated`/`edge_limit` when >50 distinct
  pairs (generate >50 pairs); absent truncation keys otherwise.
- `schema_version == 1` unchanged; assert NO existing agent/root key removed or
  renamed (compare against the known key set); the recursive no-`body` walk
  still passes.
- degraded/corrupt root still yields `{label, path, errors}` with NO
  `sent`/`received`/`edges` (extend `test_api_state_corrupt_root_isolated`).

## T007 — Tests: CSP / no-mutation / textContent / classifier (C-003/004/005, FR-005)

- CSP byte-identical: `/dashboard` == `_DASHBOARD_CSP`; `/`, `/messages/<id>`,
  `/api/*` == the legacy policy (extend the existing CSP-pin test).
- read-only full-tree-hash regression still passes after hitting `/dashboard`,
  `/static/dashboard.js`, `/api/state` (the existing no-mutation test — add the
  new routes' traffic if not already covered).
- `/static/dashboard.js` body assertions: contains `addEventListener`, the
  control ids; does NOT contain `innerHTML` or ` on` inline-handler patterns
  (e.g. assert no `onclick=`/`onchange=` in the served HTML shell either).
- role classifier convention pinned: assert the served JS contains the
  documented term lists (lead/dev|eng|impl/review|qa|audit) so the convention
  can't silently drift; if feasible, a data-driven check that representative
  roles map to the intended column.

## Definition of Done

- [ ] `pytest tests/test_web.py -q` green; FULL suite green.
- [ ] Only `web.py` + `test_web.py` changed.
- [ ] `schema_version` still 1; no existing `/api/state` key removed/renamed.
- [ ] degraded roots unchanged (errors-as-data, no stats/edges).
- [ ] no inline handlers, no `innerHTML` with bus data; per-route CSP byte-identical.
- [ ] `pip install -e .` before testing.

## Reviewer guidance (Codex)

Focus: edges correctness (self-excl, fan-out-incl, sort/cap/truncation,
determinism); sent/received from the existing scan (no new walk); healthy-vs-
degraded shape; additivity (extend not rewrite, schema_version 1, no body);
refresh controls addEventListener-only + no reload; textContent-only renderer;
the role-classifier convention pinned.

## Activity Log

- 2026-06-07T23:05:09Z – claude – shell_pid=80276 – Assigned agent via action command
- 2026-06-07T23:14:42Z – claude – shell_pid=80276 – WP01 done; suite 649; ruff clean
- 2026-06-07T23:27:06Z – claude – shell_pid=80276 – Codex approved rev2 (20260607-232650)
