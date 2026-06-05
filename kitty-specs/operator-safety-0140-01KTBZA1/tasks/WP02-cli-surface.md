---
work_package_id: WP02
title: 'CLI surface: rescind/check/wait, root, liaison, intent'
dependencies:
- WP01
requirement_refs:
- FR-001
- FR-003
- FR-004
- FR-005
- FR-006
- FR-009
- FR-010
- FR-012
- FR-013
- FR-014
- FR-015
- FR-016
- FR-017
planning_base_branch: master
merge_target_branch: master
branch_strategy: Planning artifacts for this feature were generated on master. During /spec-kitty.implement this WP may branch from a dependency-specific base, but completed changes must merge back into master unless the human explicitly redirects the landing branch.
subtasks:
- T007
- T008
- T009
- T010
- T011
- T012
- T013
- T014
- T015
agent: "claude"
shell_pid: "32384"
history:
- date: '2026-06-05T13:34:21Z'
  event: created
  by: claude
authoritative_surface: src/agenttalk/
execution_mode: code_change
owned_files:
- src/agenttalk/cli.py
- tests/test_cli.py
tags: []
---

# WP02 — CLI surface: rescind/check/wait, root, liaison, intent

## Objective

Implement the entire user-visible 0.14.0 command surface on top of WP01's
engine, exactly as specified in
`kitty-specs/operator-safety-0140-01KTBZA1/contracts/cli-surface.md`.
That contract file is normative for flags, output shapes, and exit codes —
on any divergence between this prompt and the contract, the contract wins.

**Size note**: 9 subtasks — deliberately at the upper bound because
`cli.py` has single-WP ownership. Each subtask is an independent
argparse/command unit; implement and self-verify them one at a time, in
order.

## Context

Read first: contracts/cli-surface.md (normative), data-model.md §7 (exit
codes) and §8 (JSON additions), research.md D1–D6 + baseline table.
WP01 must be merged in your lane before starting (`spec-kitty agent action
implement WP02 --agent claude` handles lane sequencing).

Hard constraints: C-005 — exit 0/1/2/130 keep exact current meanings;
new outcomes use 3 (superseded/rescinded) and 4 (unknown rid). `wait`
exit 1 stays timeout-exclusive. NFR-004 — every new error path: non-zero
exit + actionable stderr. C-008 — every body-bearing command supports
`--file -`. Known regression trap from v0.9.0: reply's request_id
auto-echo gating (`_maybe_autogen_request_id`, cli.py:84-114) — your
changes must not touch that logic path.

## Subtasks

### T007 — `cmd_rescind` (#12, FR-001)

**Steps**:
1. Subparser: `rescind --from <A> --to-request <RID> [--to-id <MSG>]
   [-m <reason> | --file <path|->] [--quiet]`. Reason body is OPTIONAL
   (unlike send) — an empty body is allowed; no empty-body check.
2. Resolve sender via `_resolve_self`. Call WP01's
   `store.validate_rescind(...)` → ValueError ⇒ print actionable stderr,
   exit 2.
3. Send `kind="rescind"` with `meta.request_id=RID` (+
   `meta.target_msg_id` when `--to-id`), recipient(s) from the helper's
   resolution — for broadcast threads, one rescind per distinct opener
   recipient (same rid; this mirrors fan-out, cli.py:690-704, but is NOT
   a broadcast — no broadcast_id).
4. Print the standard SENT render unless `--quiet`; exit 0. Rescinding an
   already-superseded thread: allowed, writes the message (audit), state
   unchanged — print an informational note to stderr.

**Validation hooks for T015**: happy path; exit-2 matrix (unknown rid,
non-requester, bad --to-id); `--file -` reason; broadcast fan-rescind
writes N messages sharing the rid.

### T008 — `cmd_check` (#12, FR-005)

**Steps**:
1. Subparser: `check --for <A> --to-request <RID> [--json]`. Read-only:
   no cursor, no heartbeat, no threadstate writes — verify by diffing
   state/ dir in tests.
2. Compute over `store.valid_messages()` (visibility = derivation):
   - rid unknown among A-visible messages ⇒ print `unknown`, exit 4
   - superseded per D2 (reuse the WP01 derivation — call derive_threads
     or the extracted rule helper; do NOT re-implement the ordering rule)
     ⇒ print `superseded` + rescind id/by/at/reason, exit 3
   - else ⇒ `current`, exit 0
3. `--json`: `{"request_id":..., "state":"current|superseded|unknown",
   "rescind":{...}|null}` exactly per data-model §8.
4. Performance: single valid_messages() pass (NFR-003); no per-message
   re-verification beyond what valid_messages already gates.

### T009 — scoped-wait rescind wake (#12, FR-003)

**Steps**:
1. In `_scoped_wait` (cli.py:885-988): (a) at entry, run the T008 rule —
   if already superseded, print the rescind render under a `RESCINDED`
   banner and return exit 3 immediately; (b) in the poll loop, treat an
   arriving valid rescind for this rid (requester-only rule!) as a wake:
   same banner, exit 3.
2. The rescind wake does NOT advance the global cursor (scoped waits never
   do) and does NOT mark threadstate seen beyond the existing per-thread
   pointer behavior for delivered messages.
3. `.waiting` marker cleanup stays in the existing finally. Plain
   (unscoped) `wait` is OUT of scope — it has no rid to check (note this
   in the code comment; skills teach scoped waits for known threads).
4. Exit-code doc: update the module-level exit-code comment + `wait`
   subparser epilog: 0 reply, 1 timeout, 3 rescinded.

### T010 — display layer: superseded, escalations, reply-in-flight, warnings (#12/#18/#14)

**Steps**:
1. `threads` human + `--json`: render `closed-superseded` rows (with
   rescind by/at/reason inline) and the WP01 `needs_operator`/
   `operator_state` fields; counts include the new state. JSON: additive
   keys only (data-model §8).
2. `sync`: rescinded threads listed under terminal decisions with a
   deterministic hint (`do not act on <rid> — rescinded by <A> at <ts>`);
   for the liaison (`store.operator_facing() == agent`), an
   `escalations` section: pending rows with age + hint
   (`reply --to-request <rid> after consulting the operator`); JSON gains
   `escalations` array.
3. `status`: warnings via `_status_warnings` (cli.py:336-356 pattern):
   stale pending escalation (pending > OPEN_OUTBOUND_STALE_SECONDS);
   liaison-unset-but-escalations-exist. Per-agent line marks the liaison
   (`[operator-facing]`).
4. **(#14, slip-droppable)** reply-in-flight: where `threads`/`sync`
   build rows, read the counterparty's composing-intent marker
   (`store.read_composing_intent`); fresh entry for this rid (younger
   than `COMPOSING_INTENT_STALE_SECONDS`, heartbeat not stale) ⇒
   annotate row `reply-in-flight` and SUPPRESS the OPEN_OUTBOUND_STALE
   warning for that thread (cli.py:396-412). Corrupt/missing marker ⇒
   exactly today's behavior.

### T011 — `init` up-tree guard + AGENTTALK_ROOT wiring + whoami root-first (#13)

**Steps**:
1. `cmd_init`: before creating, call WP01's
   `find_stores_upward(target.parent)`; non-empty and no `--force` ⇒
   stderr naming the found store(s) + remediation (`use --root <found>`
   to join, or `--force` for a deliberate nested store), exit 2. Add
   `--force` flag. A store already at the target itself keeps the current
   re-init behavior unchanged.
2. AGENTTALK_ROOT needs no `_get_store` change (WP01 put it in
   `find_root`) — but update the `--root` help text to state the full
   precedence and mention the env var.
3. `cmd_whoami`: resolved root becomes the FIRST line (`root: <path>`);
   add `operator-facing: yes|no (liaison: <name|none>)` line and
   `operator_facing` in `--json`.

### T012 — `roster set-operator-facing` (#18, FR-010/011-display)

**Steps**:
1. Roster subcommand: `roster set-operator-facing <agent>` and
   `roster set-operator-facing --clear` (mutually exclusive positional/
   flag). Calls WP01 `set_operator_facing`; ValueError ⇒ exit 2.
2. `roster` listing appends `[operator-facing]` to the designated agent's
   line; `status --json` per-agent gains `operator_facing: true`.

### T013 — `cmd_escalate` (#18, FR-012/013)

**Steps**:
1. Subparser: `escalate --from <A> [-m | --file <path|->] [--to <agent>]
   [--meta k=v ...] [--quiet]`. Body REQUIRED (it's a question).
2. Target: `--to` if given, else `store.operator_facing()`. Refusals (all
   exit 2 + remediation hint, per contract):
   - no liaison configured and no `--to` ("run roster set-operator-facing,
     or pass --to")
   - configured liaison not in roster (raw value shown)
   - sender == resolved target ("you are the liaison — ask your operator
     directly")
3. Mint `meta.request_id = "esc-" + uuid4().hex[:12]` unless supplied;
   force `meta.needs_operator="true"` (set, not setdefault). Send as
   `kind="question"` through the normal send path (HMAC etc. untouched).
4. Print the SENT render + a final machine-parseable line
   `request_id=<rid>` (callers wait on it); `--quiet` prints only that
   line.

### T014 — `composing --to-request` sugar (#14, FR-016 — SLIP-DROPPABLE)

**Steps**:
1. Add `--to-request <RID>` to the composing subparser: validates RID is
   an open inbound thread for the sender (exists in valid messages,
   sender is a participant; exit 2 otherwise), sets `meta.request_id=RID`
   (explicit `--meta request_id` conflicting ⇒ exit 2), and calls
   `store.write_composing_intent(sender, RID, peer)`.
2. The composing message itself is unchanged (still CONTROL kind, still
   extends scoped waits via the existing comp_rid gate, cli.py:939-942 —
   touch nothing there).
3. Fix the stale help text while here: composing subparser says "240s
   default timeout" but wait's default is 120 (cli.py:1828 vs 1926) —
   align the prose to reality.
4. **Slip rule**: if implementing this forces any new load-bearing state
   or threadstate change, STOP, drop T014 (and T010 step 4), record the
   slip in the WP review request, and proceed — C-010.

### T015 — tests: test_cli.py extensions

**Steps** — extend tests/test_cli.py following its existing in-process
invocation patterns (no real subprocesses; monkeypatch env where needed):
1. Per-command coverage as listed in each subtask's validation hooks.
2. Exit-code contract sweep: rescind 0/2, check 0/3/4, wait-scoped 0/1/3
   (1 still reachable ONLY by timeout — explicit test), escalate 0/2,
   init 0/2, composing sugar 0/2.
3. check is read-only: snapshot `.agenttalk/state/` before/after, assert
   identical.
4. JSON shape assertions for threads/sync/status/whoami additions
   (additive: assert pre-existing keys unchanged on a store with no new
   features used — NFR-001 angle).
5. Refusal stderr messages contain their remediation hints (grep
   substrings, not exact match).

**Validation**: `pip install -e .` then `pytest tests/test_cli.py -q`
green; full suite green.

## Definition of Done

- [ ] All nine subtasks (or eight + recorded #14 slip) complete; tests green; full suite green
- [ ] Exit codes verified against data-model §7; `wait` exit 1 timeout-exclusive test present
- [ ] Every new error path: non-zero exit + hint (NFR-004 sweep in T015)
- [ ] `--file -` works on rescind and escalate (C-008)
- [ ] No changes to store.py/threads.py/doctor.py (ownership) — needed engine changes go back to WP01 via review, not inline
- [ ] Cross-review by Codex approved (`wp_id=WP02`)

## Reviewer guidance (Codex)

Attack: exit-code contract (especially wait 1-vs-3 separation and any
path that could return 0 on a superseded thread), check's read-only
guarantee, escalate refusal matrix completeness, request_id echo/autogen
regression (v0.9.0 trap), JSON additivity (diff a 0.13.0-shaped store's
outputs), composing sugar not touching the comp_rid wait gate.

## Activity Log

- 2026-06-05T14:24:21Z – claude – shell_pid=32384 – Started implementation via action command
