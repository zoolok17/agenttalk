---
work_package_id: WP01
title: 'Server core: multi-root state aggregate + dashboard routes'
dependencies: []
requirement_refs:
- FR-001
- FR-002
- FR-003
- FR-005
- FR-007
- FR-008
- FR-009
- NFR-001
- NFR-002
- NFR-003
- NFR-005
planning_base_branch: master
merge_target_branch: master
branch_strategy: Planning artifacts for this feature were generated on master. During /spec-kitty.implement this WP may branch from a dependency-specific base, but completed changes must merge back into master unless the human explicitly redirects the landing branch.
base_branch: kitty/mission-obligation-dashboard-0170-01KTHADQ
base_commit: 9eea3ca31ff85abab8e4a10aac1e772047a485cf
created_at: '2026-06-07T15:40:46.643076+00:00'
subtasks:
- T001
- T002
- T003
- T004
- T005
- T006
- T007
shell_pid: '50824'
history:
- '2026-06-07: created from approved plan rev2 (8e81ace, Codex pre-code review approved)'
authoritative_surface: src/agenttalk/web.py
execution_mode: code_change
owned_files:
- src/agenttalk/web.py
- tests/test_web.py
tags: []
---

# WP01 — Server core: multi-root state aggregate + dashboard routes

## Objective

Everything server-side for the 0.17.0 obligation dashboard, inside
`src/agenttalk/web.py` and `tests/test_web.py` ONLY: multi-root support in
the existing server, the `/api/state` aggregate (schema_version 1), the
`/dashboard` HTML hierarchy view with 2 s JS polling, the
`/static/dashboard.js` route, per-route CSP, and the complete test suite
including the no-mutation hash proof.

## Context (read first)

- `kitty-specs/obligation-dashboard-0170-01KTHADQ/spec.md` — FR-001..003,
  FR-005, FR-007..009, NFR-001..003, NFR-005, C-001..007.
- `research.md` D1 (refresh/CSP), D2 (route placement), D4 (descriptors),
  D5 (thread dedup — THE subtle one), D6 (epoch_status), D7 (generated_at),
  D8 (perf), D9 (no-mutation test), D11 (security checklist).
- `data-model.md` — the exact `/api/state` schema. Treat it as a contract;
  Codex reviews against it.
- `contracts/cli-surface.md` — route table with per-route CSP.
- Current `src/agenttalk/web.py` — module docstring documents the threat
  model you are extending, NOT relaxing.

**Hard boundaries**:
- Do NOT touch `src/agenttalk/cli.py`, `store.py`, `threads.py`, or any
  other file. `cli.py` calls `_web.make_server(store, args.host, args.port,
  quiet=args.quiet)` — that exact call MUST keep working unchanged (WP02
  wires the new surface; until then the full suite must stay green at your
  HEAD).
- Stdlib only (C-001). History immutable — zero writes (C-003).
- The loopback wall: `LOOPBACK_HOSTS` allowlist in `make_server` stays; no
  new binding flag of any kind (C-007).

## Subtask T001 — Multi-root server plumbing

**Purpose**: let one server hold 1..N stores without changing the existing
single-store call shape.

**Steps**:
1. Add near the top of `web.py`:
   ```python
   @dataclass(frozen=True)
   class RootDescriptor:
       store: Store
       label: str
   ```
   (import `dataclass`; `Store` is already imported).
2. Add `def _dedup_labels(paths: list[Path]) -> list[str]` — basename of
   each path; on case-insensitive collision append `~2`, `~3`, … in input
   order (Windows-first: casefold comparison).
3. Extend `make_server(store, host, port, *, quiet=True)` with an additive
   keyword-only parameter `extra: list[RootDescriptor] | None = None` —
   when provided, the handler serves `[RootDescriptor(store, label0)] +
   extra`; when None, exactly today's single-root behavior. The first
   descriptor is always **root[0]** — all existing routes keep binding to
   it (FR-009).
4. `_make_handler(store)` becomes `_make_handler(roots: list[RootDescriptor])`
   internally; existing route code reads `roots[0].store` where it used
   `store`. Keep the rename mechanical and complete — no behavior change.
5. `serve()`, `serve_in_thread()` gain the same optional `extra` pass-through
   (`serve_in_thread` is what your tests use for multi-root).

**Validation**: existing `tests/test_web.py` passes UNMODIFIED before you
add new tests (proves back-compat); new test constructs a 2-root server via
`serve_in_thread`.

## Subtask T002 — `build_state()` aggregate

**Purpose**: the single pure entry point `/api/state` calls (FR-001/002/005).

**Steps**:
1. `def build_state(roots: list[RootDescriptor]) -> dict` returning the
   data-model.md §1 shape: `schema_version: 1` (literal), `agenttalk_version`,
   `generated_at` (`datetime.now(timezone.utc).isoformat()`), `roots: [...]`.
2. Per root, build inside `try/except (OSError, ValueError) as e:` —
   on failure the root object is `{"label", "path", "errors": [str(e)]}`
   and nothing else; on success `errors` is `[]` (ALWAYS present, FR-005).
   Never let one root's failure escape (it would 500 the aggregate).
3. Healthy-root fields (data-model §2): `label`, `path` (str of store.root),
   `project_id`, `errors: []`, `signing_enforced`, `epoch`
   (`store.current_epoch()` — null until a barrier exists), `counts`
   (messages=len(valid), invalid=len(store.list_invalid_messages()),
   open_threads/closed_threads from T003), `operator_facing`
   (`store.operator_facing()`, key ABSENT if None), `agents` (T002.4),
   `retired` (`store.retired_agents()`), `threads`+`broadcasts` (T003),
   `spec_kitty` (T002.5).
4. AgentEntry per active roster agent (data-model §3): `name`; `role` /
   `groups` ABSENT if unset (cfg `roles`/`groups` maps); `operator_facing:
   true` only on the liaison; heartbeat via `store.read_heartbeat(a)` →
   `last_seen` ISO + `last_seen_age_seconds` (both ABSENT when no
   heartbeat); `unread` = `len(store.unread_for(a))`; `composing` — read
   `store.read_composing_intent(a).get("threads", {})`, emit an ARRAY of
   `{"request_id", "peer", "age_seconds"}` sorted by request_id, key ABSENT
   when empty (Codex pre-code finding #2: the marker is a multi-entry map).
   `age_seconds` from parsing the entry's `"at"` ISO timestamp; skip
   entries whose `at` is unparseable.
5. spec-kitty link metadata (FR-008): if `(store.root / "kitty-specs")` is
   a directory, emit `{"kitty_specs_dir": str(...), "missions": [child dir
   names, sorted]}`; otherwise the key is ABSENT. Filesystem detection
   only — never import spec-kitty.
6. Validation-parity note: reuse the module's existing `_all_messages(store)`
   for the validated set so `/api/state` renders exactly what `recv`/`tail`
   would deliver (module invariant #5).

**Validation**: unit-test `build_state` directly (no HTTP) for one healthy
root: schema_version pinned to 1, `errors == []`, absent-not-null keys.

## Subtask T003 — Thread rows: D5 dedup + D6 epoch_status + broadcasts

**Purpose**: the obligation core — one row per open thread per root with an
absolute `next_owner` (FR-003).

**Steps**:
1. Import `derive_threads` from `agenttalk.threads` (top-level import is
   fine; no cycle — `threads.py` doesn't import `web`).
2. Per root: `msgs = sorted validated set` (reuse what T002 computed — ONE
   scan per root per request, D8); for each active roster agent `a`:
   `derive_threads(msgs, agent=a, cursor=store.cursor(a) or "",
   closed_rids={rid for rid, e in store.read_threadstate(a).items() if
   isinstance(e, dict) and e.get("closed") is True})`.
   (That closed-rids expression mirrors `cli._closed_rids` — do NOT import
   cli; replicate the 3-line set comprehension with a comment naming the
   parity.)
3. Collapse to one row per request_id (research D5 table):
   - prefer a perspective whose thread is non-terminal AND whose
     `next_owner == that agent` (the ball-holder's own view);
   - else any non-terminal view (requester's `open-outbound` — everyone
     waits on the peer);
   - if ALL views are terminal, the thread is closed → EXCLUDE from
     `threads` (count it in `counts.closed_threads`).
4. Row shape (data-model §4): start from `thread.to_dict()`, add
   `next_action`/`next_owner` when present (same conditional pattern as
   `cli._inject_next` — replicate, don't import), add `mission`/`wp_id`
   copied from the OPENER message's meta when present (find the opener =
   first message in the rid group; you already have the messages), add
   `epoch_at_send` forwarded EXACTLY as stored on the opener meta (absent /
   null / id — the 0.16.0 three-state), and `epoch_status` (step 5). NEVER
   include any `body` content.
5. `epoch_status` (research D6 — exact `check --epoch` vocabulary):
   compute `cur = store.current_epoch()` once per root;
   - `cur is None` → `"current"` (no barrier yet, nothing can be stale);
   - opener meta KEY ABSENT and `cur` exists → `"unknown-pre-epoch"`;
   - `epoch_at_send` is None (null) and `cur` exists → `"previous-epoch"`;
   - equal to `cur` → `"current"`; any other id → `"previous-epoch"`.
6. `broadcasts` (data-model §5): projection of the deduped rows where
   `is_broadcast` — `{request_id, subject, opener_kind, requester (opener
   sender), audience, responded, pending, age_seconds}`.
7. `counts.open_threads = len(threads rows)`;
   `counts.closed_threads` = number of rids excluded as terminal.

**Validation**: tests in T005/T006 — both directions of a pairwise thread
produce ONE row with absolute next_owner; broadcast pending lists survive.

## Subtask T004 — Routes: `/api/state`, `/dashboard`, `/static/dashboard.js`, per-route CSP

**Purpose**: surface T002/T003 (FR-001/007); split CSP per route (D1) without
touching existing routes' headers (FR-009/NFR-005).

**Steps**:
1. Make `_send` accept an optional `csp: str | None = None` parameter;
   `None` → the EXISTING policy string byte-identical. Only the two new
   HTML/JS responses pass a custom one. (`/api/state` JSON uses the default
   like the other JSON routes.)
2. Routes (extend `_route`'s allowlist — same dispatch style, no regex on
   new paths beyond exact-match):
   - `/api/state` → `self._send_json(HTTPStatus.OK, build_state(roots))`.
   - `/static/dashboard.js` → `_DASHBOARD_JS` module constant,
     `application/javascript; charset=utf-8`, dashboard CSP not required on
     JS (send default headers).
   - `/dashboard` → `render_dashboard(roots)` HTML shell with CSP
     `default-src 'none'; script-src 'self'; connect-src 'self'; style-src
     'unsafe-inline'; img-src 'none'; frame-ancestors 'none'`.
3. `render_dashboard(roots)`: a mostly-empty shell — `<h1>`, a per-root
   `<section data-root-label=...>` placeholder, `<noscript>` note ("enable
   JS or use /api/state"), and `<script src="/static/dashboard.js"></script>`.
   ALL dynamic content is rendered client-side from `/api/state` (one
   renderer, not two).
4. `_DASHBOARD_JS` (plain ES5-ish, no build step): `fetch('/api/state')`
   every 2000 ms (`setInterval` + immediate first call); render per root:
   header (label, path, error banner when `errors` non-empty), agents list
   (liaison first — `operator_facing` flag; then roster order) with
   last-seen age / unread / composing badges, threads table (subject, kind,
   state, `next_owner` → `next_action`, mission/wp_id tag, epoch_status
   when not "current", age), broadcasts panel when present. Build DOM via
   `document.createElement` + `textContent` ONLY — never innerHTML with
   data (D11). Detail links: `/messages/<last_msg_id>` for ROOT[0] rows
   only; other roots render the id as plain text (Codex pre-code finding
   #1; client knows root index from array position).
5. Index link (D2): in `render_index`, add one line near the top:
   `<p><a href="/dashboard">obligation dashboard</a></p>`. Nothing else on
   `/` changes.
6. Keep `__all__` updated (`build_state`, `render_dashboard`,
   `RootDescriptor` exported for cli/tests).

**Validation**: manual quickstart §1–2 against `serve_in_thread`; tests T007
pin headers.

## Subtask T005 — Tests: `/api/state` schema contract

In `tests/test_web.py` (follow the file's existing fixture style — stores
built in tmp_path, `serve_in_thread`, `urllib.request`):

1. `test_api_state_schema_v1`: one root with traffic (a review-request with
   `mission`/`wp_id` meta + a reply) → `schema_version == 1`, top-level key
   set pinned, root object key set pinned, thread row carries
   request_id/state/next_owner/next_action/mission/wp_id/epoch_status.
2. `test_api_state_absent_not_null`: agent without role/groups/heartbeat/
   composing → those keys ABSENT; `errors` present and `[]`; no `body` key
   anywhere in the JSON (recursive walk).
3. `test_api_state_composing_array`: write two composing intents for one
   agent (`store.write_composing_intent`) → `composing` is a list of 2,
   sorted by request_id, each `{request_id, peer, age_seconds}`.
4. `test_api_state_epoch_status`: pre-barrier → all `"current"` and root
   `epoch` null; after `barrier bump`-equivalent (send a self-addressed
   kind=message with `meta.barrier={"version":1,"scope":"global","type":
   "epoch-bump"}` via store.send) → old thread `"unknown-pre-epoch"` or
   `"previous-epoch"` per its stamp, new opener `"current"`.
5. `test_api_state_thread_dedup`: pairwise request — exactly ONE row for
   the rid; `next_owner` is the recipient's name (absolute), state
   `owed-inbound`; after the reply lands unread, still one row,
   `next_owner` = requester, `next_action` = `read-reply`.

## Subtask T006 — Tests: multi-root separation + degraded roots + link policy

1. `test_api_state_multi_root_separation`: two stores, distinct rosters and
   traffic → `roots` length 2, supplied order, each root's threads
   reference only its own agents; label dedup test (two dirs both named
   `proj` → `proj`, `proj~2`).
2. `test_api_state_corrupt_root_isolated`: root[1] config replaced with
   `"{not json"` → HTTP 200, root[1] has non-empty `errors` and no
   `agents`/`threads` keys, root[0] complete. Then RESTORE the config and
   re-poll → root[1] healthy again (no server restart — D4).
3. `test_dashboard_root0_only_links`: fetch `/static/dashboard.js` and
   assert the renderer's link policy textually (the JS must gate href
   creation on root index 0), AND/OR drive it indirectly: the JSON gives
   the client no cross-root message route — assert `/api/messages/<id>` of
   a root[1] message id returns 404 (it isn't in root[0]'s store), pinning
   why links must be root[0]-only.
4. `test_uninitialized_root_is_error_data`: `--store` path with no
   `.agenttalk/` → errors non-empty, 200, other root fine.

## Subtask T007 — Tests: security invariants + no-mutation + perf smoke

1. `test_message_detail_csp_unchanged`: byte-compare the FULL
   `Content-Security-Policy` header of `/messages/<id>` (and `/`) against
   the literal 0.16.0 policy string; also assert `/dashboard`'s CSP equals
   the new literal exactly (no drift in either direction).
2. `test_new_routes_loopback_and_405`: POST/PUT/DELETE/PATCH to
   `/api/state` and `/dashboard` → 405 with `Allow: GET, HEAD`;
   `make_server` still raises ValueError for `0.0.0.0` (existing test
   probably covers — extend to assert the new routes exist only on the
   same server object).
3. `test_no_mutation_full_tree_hash` (D9, NFR-001): two roots with rich
   state (messages, cursors via ack, threadstate closure, composing
   markers, heartbeat). Snapshot `{relpath: sha256(bytes)}` for EVERY file
   under both `.agenttalk/` trees. Issue ≥10 mixed requests: 3×
   `/api/state`, `/dashboard`, `/static/dashboard.js`, `/`,
   `/messages/<valid id>`, `/messages/zzz` (404), `/nope` (404), POST
   `/api/state` (405). Re-snapshot → dict equality. Content hashes, NOT
   mtimes (Windows).
4. `test_api_state_perf_smoke` (NFR-003): generate a store with 1,000
   validated messages (loop `store.send`; cheap bodies), 2 agents; time ONE
   `build_state()` call (direct, no HTTP) with `time.perf_counter()`;
   assert `< 2.0` seconds. Mark with a comment that CI hardware is the
   reference — do not tighten below the NFR.
5. Keep every existing test green and UNMODIFIED (their passing is the
   FR-009/NFR-005 evidence).

## Definition of Done

- [ ] All 7 subtasks implemented; `python -m pytest tests/test_web.py -q` green.
- [ ] FULL suite green (`python -m pytest -q`) — proves cli.py untouched-and-working.
- [ ] No changes outside `src/agenttalk/web.py` + `tests/test_web.py`.
- [ ] `/api/state` matches data-model.md §§1–5 exactly (Codex reviews against it).
- [ ] Existing routes' headers byte-identical (test-pinned).
- [ ] No `Date.now`-style wall-clock in derivations beyond `generated_at`/ages.
- [ ] `pip install -e .` run before testing (dev gotcha — tests import the installed package).

## Reviewer guidance (Codex)

Focus: D5 dedup correctness (both perspectives, broadcast pending),
epoch_status vocabulary parity with `check --epoch`, CSP split (hostile-body
routes unchanged), the no-mutation walk actually covering cursors/state
files, absent-vs-null discipline against data-model.md, and that
`make_server`'s legacy call shape is untouched for cli.py.
