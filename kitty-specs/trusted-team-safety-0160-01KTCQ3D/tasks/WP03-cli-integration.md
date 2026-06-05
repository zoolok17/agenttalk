---
work_package_id: WP03
title: 'CLI integration: roster, barrier, check --epoch, json next_*'
dependencies:
- WP01
- WP02
requirement_refs:
- FR-003
- FR-005
- FR-007
- FR-008
- FR-009
- FR-012
- FR-013
- FR-014
planning_base_branch: master
merge_target_branch: master
branch_strategy: Planning artifacts for this feature were generated on master. During /spec-kitty.implement this WP may branch from a dependency-specific base, but completed changes must merge back into master unless the human explicitly redirects the landing branch.
created_at: '2026-06-05T20:35:00Z'
subtasks:
- T012
- T013
- T014
- T015
- T016
- T017
history:
- date: '2026-06-05T20:35:00Z'
  event: created
  by: claude
authoritative_surface: src/agenttalk/
execution_mode: code_change
owned_files:
- src/agenttalk/cli.py
- tests/test_cli.py
- tests/test_coordination.py
tags: []
---

# WP03 — CLI integration: roster, barrier, check --epoch, json next_*

## Objective

Wire every new command surface into the single `cli.py` file. Because `cli.py`
cannot be co-owned, ALL CLI wiring for the release lives here. Keep each handler
thin: parse args → call a WP01 store method / read a WP02 thread field → format
output + exit code. You own `src/agenttalk/cli.py`, `tests/test_cli.py`,
`tests/test_coordination.py`.

Depends on **WP01** (store: `retire_agent`, `rename_agent`, `_drain_check`,
`remove_agent(force=)`, `forward_retired`, `current_epoch`, `epoch_at_send`
stamping, retired-send refusal) and **WP02** (`ThreadRow.next_owner/next_action`).

Read first: `contracts/cli-surface.md` (the authoritative command contract),
`data-model.md` §4/§5, `research.md` D5, and the existing `cmd_roster` (~1285),
`cmd_check` (~862), the `threads`/`sync` JSON paths, and the argparse setup.

Standing constraints: **stdlib only**; exit-code contract preserved (0/1/2/3/4/5/
130) — reuse, don't invent; additive JSON; Windows-first.

## Subtasks

### T012 — `roster` subcommands: retire / rename / remove / forward
**Reference**: `contracts/cli-surface.md` §roster*. Extend `cmd_roster` +
argparse with four actions (mirror the existing roster action dispatch):
1. `roster retire <name> [--reason]` → `store.retire_agent(...)`; print the
   tombstone confirmation; `--json` prints `{"retired":[...]}`. Map a
   not-active `ValueError` to **exit 2**.
2. `roster rename <old> <new> [--drain-check] [--reason]`:
   - if `--drain-check`, call `store._drain_check(old)`; if non-empty, print the
     owed threads and **exit 2** WITHOUT mutating;
   - else `store.rename_agent(old,new,...)`; print that `old`→tombstone, `new`
     active, and which role/group/liaison bits carried over. `new` already-known
     or `old` not-active → **exit 2**.
3. `roster remove <name> [--force]`:
   - call `store.remove_agent(name, force=args.force)`;
   - on the no-force refusal, print the retire hint to stderr + **exit 2**;
   - on force, print the history-breakage WARNING + succeed (exit 0).
4. `roster forward <retired> --to <live> --to-request <rid> [--from <agent>]
   [--reason]` (B4) → `store.forward_retired(retired, to_agent=live,
   request_id=rid, from_agent=args.from_agent, reason=...)`; print the auditable
   redirect (names the forwarded request + the resolved sender); invalid
   source/target, non-owed `<rid>`, missing sender, or second-hop → **exit 2**.

### T013 — `barrier bump`
**Reference**: contracts §"barrier bump". Add a new top-level `barrier` command
with a `bump` action:
```
agenttalk barrier bump --from <agent> --scope global -m "<reason>"
```
- Validate `--scope == "global"` (only value in 0.16.0; else stderr + exit 2).
- Send ONE message via `store.send(sender=<from>, recipient=<from>,
  kind="message", subject="epoch bump", body=<reason>,
  meta={"barrier":{"version":1,"scope":"global","type":"epoch-bump"}})`.
  (Self-addressed; recipient is irrelevant to epoch detection — see research D1.)
- `--from` must be active; the store send-guard already refuses retired → surfaces
  as exit 2.
- Print the new epoch id (= the returned message `id`). `--json` →
  `{"epoch":"<id>","scope":"global"}`.

**B3 (Codex review) — broadcast epoch snapshot**: in the existing `broadcast`
fan-out path (cli.py ~1223), when the broadcast kind is an opener
(`OPENER_KINDS`), compute `store.current_epoch()` ONCE before the fan-out loop
and pass that explicit `epoch_at_send` into every recipient copy's (frozen) meta,
so all copies of one `broadcast_id` share one stamp even if a barrier lands
mid-loop, and `--resume` preserves it. (`send()` won't overwrite a supplied
value.) Add a test that all copies of one `broadcast_id` carry the same
`epoch_at_send`. Non-opener broadcasts are unaffected (no stamp).

### T014 — `check --epoch`
**Reference**: contracts §"check --epoch", data-model §4, research D5. Extend
`cmd_check` + argparse with an `--epoch` flag. Without it: unchanged. With it,
ALSO evaluate the epoch dimension on the resolved thread row:
- Fetch the request's opener `epoch_at_send` (from the opener message meta) and
  `store.current_epoch()`.
- Decision (after the existing rescind check, which still returns 3 first):
  - no barrier exists at all → epoch `current`, exit 0;
  - `epoch_at_send == current_epoch` → `current`, exit 0;
  - `epoch_at_send` present but older than `current_epoch` (incl. `null` when a
    barrier exists) → `previous-epoch`, **exit 3**;
  - **B1 (Codex review)**: `epoch_at_send` ABSENT but a barrier exists →
    **exit 3** (do-not-act), human text "opener predates epochs; re-ask under the
    current barrier for irreversible actions", JSON `epoch:"unknown-pre-epoch"`
    with the `current_epoch` id. NOT a passing exit 0 — automation gates on the
    exit code, so a pre-epoch opener must fail closed once an epoch exists.
- JSON: add an `epoch` object alongside existing keys (additive); non-`--epoch`
  output unchanged. Stay read-only (no cursor/threadstate writes — like 0.14.0).

> "older than" = by message-id lexicographic order. `null`/None sorts before any
> real id (treat absent-epoch-at-send-value `None` as "older than any barrier").

### T015 — `threads` / `sync --json` next_owner / next_action surfacing
**Steps**: The WP02 fields are already in `ThreadRow.to_dict()`. Confirm the
`threads --json` and `sync --json` code paths serialize via `to_dict()` so the
new keys appear automatically; if either path hand-builds a dict, add the two
keys conditionally (only when present). Optionally add a compact `next:` hint to
the human (non-JSON) output WITHOUT changing existing columns. Do not add the
fields to any non-thread JSON.

### T016 — `tests/test_cli.py`
Cover each subcommand's behavior + exit codes:
- `roster retire`/`rename`/`remove`/`forward` happy paths and refusals (exit 2):
  rename `--drain-check` blocks on owed work; remove no-force hint vs `--force`
  warning; `forward` requires `--to-request` and refuses a non-owed rid / active
  source / missing sender (B4).
- `barrier bump`: emits the `meta.barrier` message, prints epoch id; bad `--scope`
  exit 2; retired `--from` exit 2.
- broadcast epoch snapshot (B3): all copies of one `broadcast_id` opener share one
  `epoch_at_send` even with a barrier fired mid-fan-out.
- `check --epoch`: current (0), previous-epoch (3), **unknown-pre-epoch with a
  barrier → exit 3** (B1), unknown-pre-epoch with NO barrier → 0, unknown rid (4),
  and still-superseded-by-rescind (3) all behave per contract.
- `threads --json` / `sync --json` include `next_owner`/`next_action` where
  derivable and omit them on terminal threads; non-JSON output unchanged columns.

### T017 — `tests/test_coordination.py`
End-to-end: fire a `barrier bump`; send a tracked opener; assert its
`epoch_at_send` equals the barrier id; `check --epoch` → current; fire a second
barrier; `check --epoch` on the first request → previous-epoch (exit 3). Include
the "no barrier ever" path (check --epoch → current).

## Branch Strategy

Planning branch: `master`. Final merge target: `master`. Worktree from
`lanes.json` during `/spec-kitty.implement`.

Implement command: `spec-kitty agent action implement WP03 --agent claude`
(branch from the WP01/WP02 result per the lane base.)

## Definition of Done

- T012–T017 complete; `pytest tests/test_cli.py tests/test_coordination.py` green;
  full `pytest` green.
- Every new/changed exit path matches `contracts/cli-surface.md` exactly.
- No file outside `owned_files` modified; handlers stay thin (no business logic
  that belongs in store/threads).
- Codex cross-review (meta mission + wp_id WP03) approved.

## Reviewer guidance (for Codex)

- Confirm exit codes reuse the contract (esp. epoch-stale folds into **3**, not a
  new code) and that non-`--epoch` `check` output is byte-unchanged.
- Confirm `barrier bump` produces exactly ONE message whose id is reported as the
  epoch, and that `--scope` rejects non-global.
- Confirm `rename --drain-check` does NOT mutate config when it refuses.
- Confirm the json `next_*` surfacing is additive and absent on terminal threads.
