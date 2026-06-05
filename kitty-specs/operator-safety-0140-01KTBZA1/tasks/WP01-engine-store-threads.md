---
work_package_id: WP01
title: 'Engine: store + threads foundations'
dependencies: []
requirement_refs:
- FR-002
- FR-008
planning_base_branch: master
merge_target_branch: master
branch_strategy: Planning artifacts for this feature were generated on master. During /spec-kitty.implement this WP may branch from a dependency-specific base, but completed changes must merge back into master unless the human explicitly redirects the landing branch.
base_branch: kitty/mission-operator-safety-0140-01KTBZA1
base_commit: 76784bbf99d5e1ae9933d40c644deede0d152419
created_at: '2026-06-05T13:47:42.064473+00:00'
subtasks:
- T001
- T002
- T003
- T004
- T005
- T006
shell_pid: '31044'
history:
- date: '2026-06-05T13:34:21Z'
  event: created
  by: claude
authoritative_surface: src/agenttalk/
execution_mode: code_change
owned_files:
- src/agenttalk/store.py
- src/agenttalk/threads.py
- src/agenttalk/web.py
- tests/test_store.py
- tests/test_threads.py
tags: []
---

# WP01 — Engine: store + threads foundations

## Objective

Build every pure-logic foundation the 0.14.0 CLI surface (WP02) will call:
the `rescind` kind and its validation, the `closed-superseded` thread
derivation, `AGENTTALK_ROOT` resolution precedence, an upward multi-store
scanner, the `operator_facing` config accessor, escalation row surfacing,
and the reply-in-flight marker helpers. **No cli.py changes in this WP** —
if a piece of logic seems to need cli.py, expose it as a store/threads
function that WP02 will wire up.

## Context

Read first: `kitty-specs/operator-safety-0140-01KTBZA1/research.md`
(decisions D1–D6 + verified-baseline table), `data-model.md` (exact field
shapes), `spec.md` (FR/C tables). Line refs below are v0.13.0 anchors —
verify against current code, symbols are the stable handles.

Standing constraints that bite here: C-002 (history immutable), C-003
(rescind is NOT a control kind), C-004 (no new load-bearing state — markers
observational only), NFR-001 (existing tests pass unmodified).

Branch strategy: planning base `master`, merge target `master`; your
working copy is the lane worktree allocated from `lanes.json` (single lane
for this mission). Implement command: `spec-kitty agent action implement WP01 --agent claude`.

## Subtasks

### T001 — store: `rescind` kind + send-validation helper (+ web.py gate) (#12)

**Purpose**: make `rescind` a first-class, transcript-visible kind with
early send-time validation (D1).

**Steps**:
1. Add `"rescind"` to `KNOWN_KINDS` (store.py:39-61). Do NOT touch
   `CONTROL_KINDS` (store.py:67) — C-003.
2. Add a module-level helper in store.py:
   `validate_rescind(store, sender, request_id, target_msg_id=None) -> Message`
   returning the resolved thread **opener** or raising `ValueError` with an
   actionable message when:
   - no valid message with `meta.request_id == request_id` exists where
     `sender` is the requester (the opener's sender) — i.e. you can only
     rescind threads you opened. Search via `valid_messages()` (not
     `messages_for`) so visibility matches thread derivation.
   - `target_msg_id` is given but no valid message with that id exists in
     the thread.
   Resolution rule for the recipient: the opener's recipient (WP02 uses
   this to address the rescind message).
3. web.py kind gate: the transcript viewer filters/styles by kind
   (web.py ~:258-272) — add `rescind` so it renders, visually distinct if
   the existing pattern supports per-kind styling cheaply; do not build new
   styling machinery.

**Validation**: unit tests in T006 — happy resolution, non-requester
rejected, unknown rid rejected, unknown target_msg_id rejected, broadcast
opener (requester = broadcaster) resolves.

**Edge cases**: multiple openers sharing a rid (broadcast fan-out copies):
requester check is against the opener *sender* (same for all copies);
recipient = the copy's recipient — for broadcast threads return the list
or the opener set; keep the helper's contract explicit either way and
document it in the docstring (WP02's rescind command sends one rescind per
distinct recipient for broadcast threads).

### T002 — store: `AGENTTALK_ROOT` + upward multi-store scan (#13)

**Purpose**: root precedence flag > env > walk; reusable scanner for
init/doctor (D4, FR-008).

**Steps**:
1. Extend `find_root(start=None)` (store.py:1180-1189): before the upward
   walk, consult `os.environ.get("AGENTTALK_ROOT")`. If set: resolve it and
   return it **whether or not a store exists there** — `_get_store`'s
   existing must-exist check then produces the loud exit-2 path, exactly
   like an invalid `--root` (contract: env never silently falls back to
   the walk). Explicit `--root` flag handling stays in cli.py and already
   wins by short-circuiting `find_root` (cli.py:38-39) — no cli change.
2. New helper `find_stores_upward(start) -> list[Path]`: every ancestor
   (start inclusive → filesystem root) containing `.agenttalk/`, in
   walk order. Used by WP02's init guard and WP03's doctor scan.
3. Docstrings state the precedence contract verbatim:
   `--root flag > AGENTTALK_ROOT > upward walk from CWD`.

**Validation**: T006 — env set + store exists (used); env set + no store
(returned anyway — caller errors); env unset (walk unchanged); scanner
finds 0/1/2 stores in a temp dir tree.

**Edge cases**: env var set to a relative path (resolve it); env var with
trailing slash/quotes typical of Windows shells (Path() normalizes; test
one). Never read the env var anywhere except `find_root` — single source.

### T003 — store: operator_facing accessors + reply-in-flight markers (#18, #14)

**Purpose**: the config slot for the liaison and the observational
composing marker (data-model.md §3, §5).

**Steps**:
1. Config: support optional top-level key `operator_facing: "<agent>"` in
   config.json. Follow the roles/groups null-tolerance precedent
   (store.py:488-497): absent/null/non-string ⇒ treated as not configured
   (never crash). Accessors:
   - `operator_facing() -> str | None` — returns the name only if it is
     currently in the roster; else None (callers that want the raw value
     for diagnostics use `operator_facing_raw()` which returns whatever
     the config holds — WP03's doctor needs to say "configured but not in
     roster").
   - `set_operator_facing(name: str | None)` — validates name in roster
     when not None (ValueError otherwise); None clears; persists via the
     existing `_write_config` path.
2. **(#14 — slip-droppable)** Reply-in-flight marker, mirroring the
   `.waiting` trio (store.py:983-1035):
   - `write_composing_intent(agent, request_id, peer)` — upserts into
     `state/<agent>.composing.json` per data-model §5 (map keyed by rid,
     ISO `at`).
   - `read_composing_intent(agent) -> dict` — `{}` on missing/corrupt.
   - `clear_composing_intent(agent, request_id=None)` — one rid or all;
     swallow FileNotFoundError/OSError.
   - Staleness is the READER's job (WP02 display): expose
     `COMPOSING_INTENT_STALE_SECONDS` constant = the composing-extend cap
     (1800.0) so cli imports one truth.
   All best-effort: any IO error degrades to "no marker" (C-004).

**Validation**: T006 — set/get/clear roundtrip; not-in-roster set rejected;
null-config tolerance; corrupt marker file returns `{}`; clear of missing
file silent.

**Edge cases**: roster removal of the designated liaison afterwards —
`operator_facing()` returns None (and raw returns the stale name);
concurrent marker writes (two threads of one agent) — last-write-wins is
acceptable, note it.

### T004 — threads: `closed-superseded` derivation (#12, D2)

**Purpose**: the supersession rule, decided at derivation time, as a pure
function (FR-002).

**Steps**:
1. In `derive_threads` (threads.py:247-391): after grouping a rid's
   messages, detect rescinds: valid messages of kind `rescind` with that
   `meta.request_id`, whose **sender == the thread requester**. The thread
   is superseded when any such rescind's id > the opener's id (or >
   `meta.target_msg_id`'s id when the rescind pins one). First qualifying
   rescind is the decider; store its id/timestamp/body for display.
2. Apply as TERMINAL state `"closed-superseded"` with precedence parallel
   to the manual-closure override (threads.py:360-361): supersession beats
   derived live states; an earlier manual `closed` (ack) stays `closed` —
   state remains terminal either way, `closed_reason` reflects whichever
   decided first (compare decider message id vs ack `closed_at`; document
   the comparison in a comment).
3. The multi-round re-ask reopen path (threads.py:336-343) must NOT
   resurrect a superseded thread — same hard-override approach as
   closed_rids.
4. Rescind messages are *events about* a thread, not conversation: exclude
   them from `last`/ball-passing classification in `_classify_event`
   (threads.py:52-90) — handle before classification, like the broadcast
   discriminator is handled (threads.py:288-291).
5. Extend `Thread` dataclass/to_dict/counts: new state value (additive in
   JSON — existing states unchanged), optional `rescind` sub-object
   (`{id, at, by, reason}`) on superseded rows.
6. Broadcast threads: a broadcaster's rescind supersedes the whole
   broadcast thread (all pending obligations clear to closed-superseded in
   both the broadcaster view and each recipient view —
   threads.py:148-244).

**Validation**: T006 matrix (below). **Edge cases** are the matrix.

### T005 — threads: escalation row surfacing (#18)

**Purpose**: pending/answered visibility derived from existing closure
logic (FR-014/015, data-model §2).

**Steps**:
1. A thread whose opener carries `meta.needs_operator == "true"` gets two
   additive row fields: `needs_operator: true` and
   `operator_state: "pending" | "answered"` — answered exactly when the
   existing question-closure already marks it closed (any non-control
   correlated reply from the liaison to the requester; threads.py:82-88).
   No new closure rule — this is labeling, not mechanics (C-007).
2. Both perspectives: liaison sees pending escalations among owed-inbound
   (WP02 buckets them); requester sees its escalation open-outbound with
   the same labels.

**Validation**: T006 — pending on send; answered on liaison reply;
`operator_answer=true` meta NOT required for closure (display nicety
only); a non-liaison third party reply does not answer it (existing
direction guards already ensure this — test it anyway).

### T006 — tests: test_store.py (new) + test_threads.py extensions

**Purpose**: every engine behavior locked before the CLI exists (NFR-004).

**Steps** — create `tests/test_store.py` (store-level, no CLI invocation;
follow existing fixture patterns from test_threads.py for store setup):
1. T001 coverage: validate_rescind happy/non-requester/unknown-rid/unknown-target;
   KNOWN_KINDS contains rescind; CONTROL_KINDS unchanged (exactly
   `{"composing"}` — literal assertion, this is C-003's regression guard).
2. T002 coverage: AGENTTALK_ROOT used/ignored/absent (monkeypatch env);
   find_stores_upward 0/1/2-store trees (tmp_path).
3. T003 coverage: operator_facing roundtrip + tolerance + marker IO.

Extend `tests/test_threads.py` with the **supersession matrix**:
- rescind after opener ⇒ closed-superseded (both perspectives)
- rescind by non-requester ⇒ state unchanged (and message visible in raw log)
- rescind with target_msg_id pinning an older message vs a newer reply
- rescind then reply arrives ⇒ stays closed-superseded (terminal)
- re-ask on superseded rid ⇒ does NOT reopen
- manual ack then rescind / rescind then ack ⇒ terminal; closed_reason = first decider
- duplicate rescinds ⇒ idempotent
- broadcast: broadcaster rescinds ⇒ every recipient's owed-inbound clears to closed-superseded
- escalation labels: pending → answered; third-party reply doesn't answer
- **NFR-001 guard**: run the whole pre-existing test_threads suite — zero modifications to existing test bodies.

**Validation**: `pip install -e .` first (dev-install gotcha!), then
`pytest tests/test_store.py tests/test_threads.py -q` fully green.

## Definition of Done

- [ ] All six subtasks complete; `pytest tests/test_store.py tests/test_threads.py` green
- [ ] Full pre-existing suite still green (`pytest -q`) with zero edits to existing test bodies
- [ ] No cli.py/doctor.py changes; no new third-party imports (C-001)
- [ ] CONTROL_KINDS untouched; no new load-bearing state (markers best-effort)
- [ ] Docstrings state the D2 ordering rule and the root-precedence contract verbatim
- [ ] Cross-review requested from Codex (`mission=operator-safety-0140-01KTBZA1`, `wp_id=WP01`) and approved

## Reviewer guidance (Codex)

Attack surface: D2 ordering correctness (especially target_msg_id pinning
and ack-vs-rescind precedence), requester-only enforcement against
broadcast openers, NFR-001 (diff the test files — existing bodies must be
unmodified), C-003/C-004 regression (CONTROL_KINDS literal, marker
corruption degradation), env-var single-source rule (grep for stray
AGENTTALK_ROOT reads outside find_root).
