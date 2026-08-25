# DESIGN #202 — Interruption-aware redelivery (rev 3, approved-for-implementation shape)

Status: rev 3 — rev 1 REJECTED (3 blockers, all resolved in rev 2); rev 2
re-verdict found 3 NEW defects in the rev-2 additions (NEW-1 blocker, NEW-2
major, NEW-3 minor), fixed here per the reviewer's prescriptions; reviewer
pre-cleared rev 3 as approve-implement with these deltas. Author:
claude-agenttalk-lead · 2026-08-25. Field basis: JAWS retro finding 2;
mechanism investigation 2026-08-25 (anchors verified at master a35297c).

## Verified mechanism (unchanged from rev 1, review-confirmed)

No code path delivers into a live turn. The field case is: the turn watchdog
(or supervisor stall recovery) kills the child's whole process tree mid-build
(abrupt TerminateProcess → partial log, no exit file); the kill re-stamps the
heartbeat so the wrapper stays "healthy"; the turn classifies
CLASS_AMBIGUOUS; the uncommitted head re-drives in **0.3 s** into the SAME
resumed session with a byte-identical prompt, the partial draft deleted and
nothing telling the child it was interrupted; CLASS_AMBIGUOUS only escalates
at k_escalate=20 ≈ 10 hours of kill/rebuild.

## Contract

**A self-inflicted interruption is a first-class fact with its own persisted
accounting, its own supervisor-safe backoff, a bounded budget that ends in a
self-clearing dead-letter with an actionable remedy, and a visible trace to
the child.**

Stated plainly (review finding 6): v1 converts a silent 10-hour livelock
into a fast, loud, resumable failure — three attempts (each able to profit
from resumed session state, e.g. a warm Maven repo), then a dead-letter the
operator can requeue after raising the budget. It does NOT make oversized
work complete under an unchanged watchdog budget; that is #206
(watchdog progress-awareness) and operator budget tuning.

### D1. The interruption fact — DriveOutcome and the attempt ledger

- `DriveOutcome` gains `interrupted: bool = False` and
  `interruption_kind: str | None = None` ("turn_watchdog"). make_drive sets
  them wherever `sig["watchdog"]` is true — at ALL FOUR failure-return sites
  (run.py:2662, 2692, 2700, 2765 — the resume-failure branches rewrite the
  summary, so summary-sniffing is forbidden by construction).
- `store.record_attempt_result` gains the two fields and a persisted
  `interrupted_consecutive` counter: +1 when interrupted, RESET TO 0 on any
  non-interrupted result — and the fields are ALWAYS written (overwritten to
  False/None on non-interrupted results, including the commit-gate CAS-miss
  results at loop.py:1319/1337), so a stale flag can never pollute the
  rejoin or the ceiling.
- `store.reconcile_crash_in_progress` marks its reclassified attempt
  interrupted with kind "crash_mid_turn" and increments the same counter.
- Failure CLASS stays CLASS_AMBIGUOUS. The counter, not the class, carries
  the new semantics (this is new disposition state, named as such).

### D2. Supervisor-safe interruption backoff

- Knob `interruption_redrive_seconds` (default 60.0) in **supervisor.json**,
  resolved per-agent → global → default exactly like
  resolve_dead_letter_caps (supervisor.py:821+); corrupt values refuse
  visibly through the launch config-blocked path (the
  work_heartbeat.interval_violation precedent, cli.py:11200-11214) — never
  silently clamped.
- LAUNCH VALIDATION (rev 3, per re-verdict NEW-1: the rev-2 formula refused
  every wrapped-claude seat at shipped defaults — 60×2² = 240 ≥ 180 — and
  validated a quantity the chunked stamp makes moot): validate the actually
  load-bearing invariant instead — `heartbeat_interval <
  resolve_stuck_after(agent)`; violation refuses at launch with both
  numbers (interval_violation precedent). No constraint on the backoff
  length itself: the per-chunk stamp keeps heartbeat age ≤
  heartbeat_interval regardless of total sleep.
- The backoff sleep is CHUNKED: sleep in `heartbeat_interval` slices,
  stamping the heartbeat each slice (blocked-but-alive, the same posture as
  the holds). A supervisor can therefore never STUCK_RECOVER a wrapper that
  is deliberately backing off. Control kinds are still delayed for the
  duration — documented cost, bounded by the launch validation above.
- WHERE: not only after the failed turn. The backoff is computed at the
  head-entry site from the PERSISTED record (`interrupted_consecutive` > 0
  and `now - last_finished_at < required_backoff` → chunked sleep for the
  remainder). This single site survives relaunch amnesia (fail_sleep is
  in-memory) and covers crash_mid_turn's immediate-redrive-at-relaunch path
  (loop.py:1182 → 1268) with the same code.
- Backoff: base × 2^(n-1), n = interrupted_consecutive, no separate cap
  needed (n ≤ k_interrupted by D3).

### D3. Interruption budget → self-clearing dead-letter (NOT a park)

Rev 1 parked via the config-blocked path; the review proved that park (a)
never re-fires under an unchanged class (the durable hold is the ENTRY CHECK
keyed on last_failure_class, loop.py:1184-1188), (b) never re-drives and has
no un-park path (#62's re-probe belongs to CLASS_GATEWAY_HELD), and (c)
head-blocks a healthy seat (#122) — trading a self-clearing 10 h failure for
a permanent wedge. Rev 2 therefore uses the machinery whose semantics
actually fit:

- SCOPE OF THE CEILING (rev 3, per re-verdict NEW-2): only
  `interruption_kind == "turn_watchdog"` attempts count toward
  `k_interrupted`. crash_mid_turn's cause is UNOBSERVED — the codified codex
  ruling in `reconcile_crash_in_progress`'s contract (store.py:3994-4002)
  forbids exactly a DL@3 for it, because three supervisor stale-kills during
  a host anomaly must not dispose healthy-but-slow work. crash_mid_turn
  attempts still get the D1 accounting, the D2 backoff, and the D4 rejoin
  (the mechanisms that make retries cheaper and saner), but their disposal
  ceiling stays k_escalate. The ruling stands; this design does not overturn
  it.
- When the turn_watchdog-only `interrupted_consecutive` reaches
  `k_interrupted` (default 3, same supervisor.json resolver), the loop
  DEAD-LETTERS the head via the existing `_dispose` path with failure_class
  CLASS_AMBIGUOUS and reason `interruption_budget_exhausted`, and escalates
  once with the remedy:
  "this message's turns were killed <k> times by <kind> after <budget>s
  each; raise turn_watchdog.turn_elapsed_seconds for <agent>, split the
  task, or requeue as-is: `agenttalk dead-letter requeue --agent <a> --id
  <id>`" (threading the REAL reason into `_escalate_once` — review finding
  9: today it hardcodes the class into the operator notification).
- Dead-letter is self-clearing (cursor advances, the seat keeps serving the
  queue), preserves the original bytes, and ALREADY has the recovery verb:
  `requeue` mints a fresh message with a fresh attempt ledger — the exact
  "operator raised the budget, try again" path, no new machinery invented.
- Check ordering: the k_interrupted ceiling is evaluated with the other
  post-failure ceilings, BEFORE k_poison/noninfra/k_escalate (it is strictly
  more specific); AND mirrored at the ENTRY site (rev 3, per re-verdict
  NEW-3): a head whose persisted counter is already at the ceiling on entry
  — the relaunch-reconcile path can take it there — is disposed WITHOUT
  burning another watchdog budget, joining the existing
  auto-dispose-on-entry block (loop.py:1189+). Note the entry case can only
  arise from turn_watchdog counts per the scope rule above.

### D4. Tell the child — rejoin from the attempt ledger

- `rejoin_for(record) -> str | None` is built NEXT TO make_drive in the cli
  wiring (NOT injected by the loop — review finding 7): it reads
  `store.attempt_record(agent, record["id"])` and, when the previous attempt
  was interrupted, returns the REJOIN CONTEXT block (kind, elapsed, attempt
  n of k_interrupted, preserved-draft path if any, "verify state before
  repeating side-effectful work; prefer resuming over redoing").
- Keyed per head id, so a rejoin can never leak across heads; the cadence
  synthetic turn builds its own prompt and is untouched (verified: exactly
  one assemble_turn_prompt caller). Gate path: verify dispatch_record
  preserves record["id"] (it copies the record) so keying holds.
- One-shot path: `_run_one_shot` has no attempt ledger; D4 there uses the
  in-memory DriveOutcome from the previous scoped attempt (single-head loop,
  so a local variable suffices); D2's persisted-entry backoff does not apply
  (bounded by max_wall anyway) — stated, not implied.

### D5. Preserve the interrupted draft

`_with_reply_draft`: when the attempt record shows the previous attempt
interrupted, RENAME the leftover draft to `<id>.interrupted.md` (single
suffix; unlink the target first — Windows rename-over-existing throws,
mirroring preserve_refused_draft) instead of deleting. Otherwise delete as
today. Delivery reads only the exact declared live path (verified: nothing
globs the drafts dir), so the preserved copy can never publish; the #201
stale-draft regression test stays green and gains an interrupted-rename
sibling.

## Out of v1 (filed)

- #206 watchdog tree-kill selectivity / progress-awareness (the fix that
  would let a live build FINISH).
- #57 second-wrapper freeform fence; #87 supervisor thresholds;
  durable_continuation generalization (#203 adjacency).

## Tests (fixture store + injected drive; force the bound, not the happy path)

1. DriveOutcome contract: all four make_drive failure-return sites set
   interrupted/kind under sig["watchdog"] (parameterized over the resume
   branches — the summary-rewrite sites).
2. Ledger: interrupted result → counter+1; non-interrupted → reset AND flags
   overwritten to False (the stale-flag bug); crash_mid_turn reconcile
   increments with its kind; counter survives relaunch (new Store instance).
3. Backoff: injected sleep records requested durations — chunked at
   heartbeat_interval with a stamp per chunk (assert heartbeat writes DURING
   the backoff); entry-site backoff honored after simulated relaunch;
   non-interrupted ambiguous failure keeps idle_interval backoff.
4. Launch validation (rev-3 invariant): stuck_after ≤ heartbeat_interval
   refuses at launch with both numbers; the claude 180 s shipped default is
   the VALID-launch direction. (Implementation note: two persisted counters
   — any-kind drives the backoff, watchdog-only drives the ceiling; a
   non-interrupted result resets both, crash_mid_turn touches only the
   any-kind counter.)
5. Ceiling: 3rd consecutive turn_watchdog interruption → dead-letter with
   reason interruption_budget_exhausted + escalation carries the remedy +
   cursor ADVANCES (seat unblocked, next message drives); a non-interrupted
   failure between resets the run; requeue after the ceiling yields a fresh
   ledger; crash_mid_turn interruptions NEVER trip this ceiling (3+ crash
   reconciles → still retrying under backoff, per the store.py ruling);
   entry-site: a head at the ceiling on entry (kill between result-write and
   dispose, then relaunch) disposes without another drive.
6. Rejoin: re-drive after interruption carries the block with kind/elapsed/
   count and the preserved-draft path; first attempts and clean redeliveries
   carry none; gate-path dispatch keeps head keying.
7. Draft: interrupted → renamed .interrupted.md (target pre-unlinked), live
   path clear, preserved copy never publishes; non-interrupted → deleted as
   today.
8. One-shot: previous scoped attempt interrupted → next drive carries the
   rejoin (in-memory path).

## Review disposition (rev 1 → rev 2)

F1/F2 (blockers): park abandoned for dead-letter+requeue; new persisted
counter named as disposition state. F3 (blocker): chunked stamped sleep +
launch validation + persisted entry-site backoff (covers relaunch amnesia
and crash_mid_turn). F4: DriveOutcome fields + four enumerated sites. F5:
always-write semantics + counter + reconcile increment. F6: honest scope
statement added. F7: rejoin built in cli wiring; one-shot handled
in-memory. F8: single suffix + pre-unlink. F9: real reason threaded into
escalation. F10: supervisor.json + named resolvers + refuse-visibly. F11:
control-kind delay documented; ceiling ordering pinned. F12: all five gap
tests included above.

Rev 2 → rev 3 (re-verdict NEW findings): NEW-1 (launch validation refused
wrapped-claude at shipped defaults; validated a moot quantity) → validate
heartbeat_interval < stuck_after instead, no constraint on backoff length.
NEW-2 (k_interrupted@3 silently overturned the codified crash_mid_turn
ruling, store.py:3994-4002) → ceiling counts turn_watchdog only;
crash_mid_turn keeps k_escalate disposal but gains backoff + rejoin +
accounting; ruling explicitly preserved. NEW-3 (counter can reach the
ceiling at entry via relaunch) → ceiling mirrored at the entry-site
auto-dispose block. Reviewer pre-cleared this rev as approve-implement
without a further full pass.
