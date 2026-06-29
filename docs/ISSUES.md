# agenttalk — issues & work tracker

The living record of **what we are doing and why**: work in flight, planned
fast-follows, accepted known limitations, and the backlog. Pairs with
`docs/DESIGN.md` (the why behind the architecture) and `CHANGELOG.md` (what
shipped). Keep this current — when an item ships, move it to "Recently shipped"
with its version; when a new finding lands, add it with a disposition.

**Conventions.** Status: `IN PROGRESS` · `PLANNED` · `KNOWN LIMITATION`
(shipped & accepted) · `BACKLOG` · `SHIPPED`. Severity: `P0` critical · `P1`
major · `P2` minor · `P3` nit. Each item: what, why, where, disposition.

---

## IN PROGRESS — lead-loop Slice 2 / WP3 (the CADENCE TICK, `lead-loop-wp3`)

The proactive sweep the controller drives when the bus is QUIET and the cadence
interval has elapsed - so a lead-loop controller does forward work (nudge stalled
outbound threads, surface dead-letter / unrouted escalations) instead of only
reacting to inbound messages. Built off master (WP2 merged); cross-review starts with
codex (reviewer-1 joins when restored; v0.42.0 ships WP1+WP2[+WP3/WP4] at strict 2/2).

- **Synthetic, dead-letter-isolated (P1; DESIGN.md D-14).** The tick is the
  *timeout/idle branch* of the SAME `_run_continuous` loop (no second consumer/thread):
  a message present -> the per-message path; no message + not due -> heartbeat + lease
  renew + sleep; no message + DUE -> a bounded read-only snapshot, and a model turn ONLY
  if the snapshot has actionable items. It NEVER calls `record_attempt_start`, NEVER
  advances the cursor, and NEVER enters the dead-letter path.
- **Cadence state (controller-owned single-writer).** `state/<agent>.lead-loop-cadence.json`
  holds `last_tick_epoch` (due-ness), `last_reminded` ((request_id -> last_msg_id) so an
  outbound nudge fires once per thread state), `escalation_dedup`, and the failure /
  backoff fields. Degrade-safe read; reset-cleared like the lease (the dead-letter SINK
  is elsewhere and survives reset).
- **Actionability (avoid premature reminders).** Unread / reply-waiting / owed-inbound
  are the message path's job (not cadence). Open-outbound reminders fire only past
  `reminder_after_seconds`, with no fresh peer composing marker, once per
  `(request_id, last_msg_id)`. Operator-blocked work is tracked context, not its own
  nudge. Dead-letter / unrouted-escalation items are due immediately but deduped.
- **Snapshot contract (P2).** A bounded point-in-time view (self identity, lease/timing
  [token-free], `derive_threads` summaries, operator-pending, lead-loop health, restart/
  launch state, dead-letter / unrouted escalation): ids + summaries, NOT transcripts;
  counts + body lengths capped; per-field degrade so one unreadable subsystem yields an
  empty field, not a failed snapshot. The lease TOKEN is never serialized into it. The
  synthetic prompt carries the same verb-guard as a message turn (no sync/threads/recv/
  wait/drain/ack; fresh data -> a typed question + a one-turn delay).
- **Cadence failure = controller-HEALTH, not poison (P1).** A failed sweep updates the
  failure/backoff state (exponential, capped), withholds the heartbeat so the supervisor
  notices, and after a threshold escalates ONCE to the operator (deduped via
  `health_escalated`). It leaves the cursor AND the attempt ledger untouched and never
  dead-letters.

*Where:* `store.py` (cadence-state persistence + consts), `lead_loop_cadence.py` (NEW:
snapshot + actionability + state transitions), `wrapper/loop.py` (`CadenceResult` +
the idle-branch `cadence` hook), `wrapper/run.py` (`make_cadence_drive`),
`wrapper/prompt.py` (`assemble_cadence_prompt`), `cli.py` (`_wrap_loop_mode` wiring +
`_cadence_health_notifier`), `docs/DESIGN.md` (D-14), `tests/test_lead_loop_wp3.py` (NEW).
*Disposition:* IN PROGRESS (branch `lead-loop-wp3`); self-gated, in cross-review. WP4
(relay) stays on HOLD.

---

## SHIPPED (merged 24c39f7) — lead-loop Slice 2 / WP1 (opening hardening foundation, `lead-loop-wp1`)

Slice 2 of the lead-loop adds the actual controller + mechanical relay; codex's
design splits it into 4 WPs, **WP1-first** (cross-reviewed + lead-gated before any
controller work layers on). WP1 is the pure-core HARDENING FOUNDATION the rest
builds on - it consolidates the Slice 1 liveness/expiry/steal/armed/guard logic into
ONE source of truth so the three views can never drift apart again (the bug class
that bit Slice 1 twice). MERGED at 24c39f7 (codex 2x approved + lead adversarial-verify
+ gate; reviewer-1 hung during WP1, covers WP2-4 + the v0.42.0 ship at strict 2/2).

- **Single `_lead_loop_authority` (P1).** ONE method computes
  {managed, present, owner_liveness, owner_alive, expired, heartbeat_stale,
  stealable, armed, guarded, reason}. `_lease_stealable`, `lead_loop_state`, AND
  `lead_loop_active_owner` ALL derive from it - no per-caller liveness branch. By
  construction, for a present managed lease `armed == not stealable` and
  `guarded == (liveness != CONFIRMED-DEAD)`, for EVERY case (alive/dead/unknown).
- **Read-boundary expiry normalization.** `read_lead_loop_lease` coerces
  `expires_at` to a finite float or None (NaN / +-inf / non-numeric / missing ->
  None); `_lease_expired` treats None as fail-safe NOT-expired. One sanitized shape
  for every consumer.
- **Confirmed-dead-only lock-break.** `_break_stale_lock` breaks ONLY a
  CONFIRMED-dead holder (tri-state `_process_liveness`); an ALIVE or UNKNOWN holder
  waits out the timeout (no fail-quiet breaking - the lock analogue of the
  no-false-steal rule).
- **Config-gated armed.** An UNMANAGED stray lease is inert: managed/guarded/armed
  all False, never auto-stolen (reason `not managed`).
- **Timing resolver (non-store module).** `agenttalk.lead_loop_runtime.resolve_timing`
  resolves {ttl_seconds, cadence_seconds, heartbeat_stale_after} so the controller's
  acquire/steal AND doctor/state use the SAME window; with a supervisor config it
  uses the supervisor's per-agent/per-CLI `resolve_stuck_after` (a duplicate never
  steals earlier than the supervisor would call the owner stuck). Keeps `store.py`
  free of supervisor imports.

- **Corrupt-config coercion class (robustness; see DESIGN.md D-13).** A per-agent
  `supervisor.json` entry that is a TRUTHY non-dict (operator typo, e.g.
  `{"agents": {"beta": "wrapped"}}`) is NOT rescued by a falsy-only `(x or {}).get(...)`
  - the string reaches `cfg_agent.get(...)` and raises `AttributeError`, crashing the
  reader. Every per-agent reader now coerces to `{}` only when `isinstance(..., dict)`:
  `resolve_timing`, `resolve_stuck_after`, `resolve_dead_letter_caps` (config /
  cfg_agent / nested `dead_letter`), `session_args`, and `cmd_wrap`'s extraction -
  including the PRE-EXISTING v0.41.0 dead-letter/wrap sites surfaced while closing the
  class. All fail safe to the defaults (`claude_permission_mode` already used the
  `isinstance` form). A bool `stuck_after_seconds` is also ignored (bool-is-int). A
  single per-agent typo must never crash `status` / `doctor` / `supervise --report` /
  `wrap --loop`. Surfaced by: lead verify P2 (resolve_timing), codex review (wrap/
  dead-letter sibling), dev-2 sweep (session_args). Pinned by regression tests.

*Where:* `store.py` (authority + `_lease_expired`/`_heartbeat_stale` + read
normalization + lock-break), `lead_loop_runtime.py` (NEW), `supervisor.py` +
`cli.py` (resolver wiring + corrupt-config coercion), `doctor.py` (resolver wiring),
`docs/DESIGN.md` (D-4 threat sentence, D-13 coercion class),
`tests/test_lead_loop_wp1.py` (NEW).
*Disposition:* SHIPPED (merged 24c39f7). WP2 (controller) GO off 24c39f7; WP3
(cadence) + WP4 (relay) stay on HOLD until WP2 is gated + merged.

**Accepted limitation (WP1): the triple-fault edge.** An owner whose liveness probe
is UNKNOWN *and* whose lease has a corrupt/None expiry *and* whose heartbeat is stale
is NOT stealable (normalized None = not-expired), so a dead-but-unprobeable owner
with a corrupt lease waits for a valid renewal it cannot make. This is the deliberate
fail-safe direction: NEVER a false steal of a maybe-live owner; delayed recovery only
under a triple fault. Pinned by a regression test.

---

## SHIPPED (merged 61e2ce0) — lead-loop enforcement, Slice 1 (`lead-loop-slice1`)

Origin: the **operator-raised "the lead stops leading"** failure (every lead, every
project): a chat-agent lead silently UN-ARMS its control loop — the harness default
("answer the human, then yield") drops the re-arm even though the discipline is
documented and in memory. A documented-but-reliably-dropped rule means willpower is
NOT the fix; we need MECHANICAL enforcement. The operator chose **split-identity
enforcement**: keep `claude` as the free-form operator-facing liaison, and add a
separately-supervised managed lead-loop identity that OWNS the team mailbox via a
wrapped continuous loop and cannot silently un-arm. codex designed (assignment
`lead-loop-controller`); dev-2 builds; codex + reviewer-1 cross-review; lead gates.

**Slice 1 (this build) = store-level mechanism + visibility + guard (NO controller
yet; that is Slice 2).** cli-AGNOSTIC by construction — keyed on the agent NAME +
its `managed_lead_loop` config, never a cli (a codex identity is managed exactly as
a claude one).

- **Lease (P1, correctness).** `state/<agent>.lead-loop-lease.json` is the ownership
  state (renewable; `acquire`/`renew`/`release`/`read`). `.waiting` only MIRRORS the
  live lease for status/UX. STEAL is gated on the CONFIGURED `managed_lead_loop`
  flag and a CONFIRMED-dead tri-state liveness (see D-12): a CONFIRMED-DEAD owner is
  stealable IMMEDIATELY regardless of expiry (a crashed controller recovers at
  once); an ALIVE *or* UNKNOWN owner is treated as probably-alive and stealable only
  when the lease is EXPIRED *and* its heartbeat is stale. A long HEALTHY turn (within
  TTL, or heartbeat fresh) is never stolen, an uncertain probe never displaces a live
  owner, and a manual chat identity is NEVER auto-stolen.
- **Single-consumer guard (P1).** A live managed lease REJECTS the cursor-CONSUMING
  verbs (`wait`/`recv`/`drain`/`ack`, exit 7) for a non-owner; read-only
  (`sync`/`threads`/`status`/`check`) stays allowed. The owner proves itself via the
  live `lease_id` (`AGENTTALK_LEAD_LOOP_LEASE`). This closes the cursor-loss hole that
  `--refuse-stacked-wait` alone misses (the model subprocess / a stray window would
  otherwise race the controller's in-process consumption).
- **`lead_unarmed` visibility (P2).** doctor + `status` + the supervisor report:
  a MANAGED identity that is NOT armed is an ERROR (the controller is down).
  `armed` = a present managed lease that is NOT confirmed-dead AND NOT (EXPIRED *and*
  heartbeat-stale) — the EXACT complement of the steal predicate, i.e. unarmed only on
  **no lease, a CONFIRMED-dead owner, or a lease that is EXPIRED *and* heartbeat-stale**.
  An UNKNOWN-liveness owner (uncertain probe) is NOT `owner_alive` but is treated as
  probably-alive → armed within TTL (never a false unarmed from a fail-quiet probe).
  Neither expiry NOR a lapsed heartbeat ALONE is an error: a long healthy turn (within
  TTL, heartbeat lapsed) and an expired-but-heartbeating lease are both still armed. A LEGACY
  lead/liaison is a NON-GATING WARN, only with open team work AND no fresh
  heartbeat/waiter (best-effort; depends on the heartbeat hook, so it is WARN never
  ERROR — a busy free-form liaison must not false-fire).

**Slice 1b — post-turn turn-end audit — DEFERRED (feasibility verdict).** The
idea of a post-turn / post-final-answer harness hook that verifies the lead
re-armed a background wait is **not buildable now**: the repo supports only soft
PostToolUse hooks (`agenttalk heartbeat --hook`); there is no reliable
post-final-answer hook that can inspect a backgrounded armed wait. So we **defer**
Slice 1b and rely on the managed `lead_unarmed` detector (above) as the substitute —
a wrapped lead-loop is mechanically armed every cycle, so the turn-end audit is moot
for it, and the liaison no longer owns the team loop. *Disposition:* DEFERRED;
revisit only if the host harness adds a post-turn hook.

*Where:* `store.py` (lease API + `managed_lead_loop` config + visibility),
`cli.py` (verb-guard + `managed-lead-loop` command + status), `doctor.py`
(`_check_lead_unarmed`), `supervisor.py` (report field), `tests/test_lead_loop.py`.
*Disposition:* built (branch `lead-loop-slice1`), in cross-review.

**Accepted limitations / Slice-2 notes** (from the dev-2 adversarial-verify):

- **PID-reuse in the liveness heuristic (P2, accepted).** `lead_loop_active_owner`
  (guard), `_lease_stealable` (steal), and `lead_loop_state` (armed) use the tri-state
  `_process_liveness(owner_pid)` (see D-12) - if the OS recycles a dead owner's pid, the
  owner can look alive. This matches the existing posture (`foreign_wait_pid` / the
  `.waiting` marker rely on the fail-quiet `_process_alive`). The `lease_id` (not the pid)
  is the real ownership token for the guard's owner-bypass, and a recycled pid running a
  different process will not heartbeat as this agent, so the stale-heartbeat path
  self-corrects (the lease becomes stealable). `owner_start` is in the lease for a future
  tighten. Accepted for Slice 1 (consistent with the single-writer model). NOTE: PID-reuse
  is the *conservative* direction - a recycled pid makes a dead owner look ALIVE, so the
  worst case is a delayed (expiry+heartbeat) recovery, never a false steal of a live owner.
- **Lease + .waiting mirror are not atomically coupled (P3, by design).** The lease write
  is atomic; the observational mirror is a best-effort second write. A crash between them
  leaves a valid lease without a mirror - fine, readers degrade. Docstring corrected.
- **Slice-2 note: heartbeat + renewal cadence.** `armed` is NOT confirmed-dead AND
  NOT (expired AND heartbeat-stale) — so a single lapsed heartbeat on a within-TTL
  lease no longer flips `armed`. With the defaults (heartbeat window 120s, renewal
  cadence 300s, TTL 900s) a controller that renews on cadence is never simultaneously
  expired, so it stays armed even across heartbeat gaps. The wrapped controller
  (Slice 2) should still stamp the heartbeat frequently (as `_run_continuous` already
  does each idle cycle) for fresh liveness, but a LIVE/UNKNOWN owner only ERRORs once
  a lease has BOTH lapsed past TTL AND gone heartbeat-stale (a genuinely down
  controller); a CONFIRMED-dead owner ERRORs immediately (see D-12).

**Review folds (cross-review on `lead-loop-slice1`):**

- **reviewer-1 round 1 (39589ac→8d923a8):** non-atomic acquire/steal → per-agent
  O_EXCL lease lock around acquire/renew/release; `lease_id` leak via the `.waiting`
  mirror → mirror no longer carries the token; `clear` left a live lease → clear
  force-releases + the guard requires `is_managed_lead_loop`. codex round 1 converged
  on the same atomic-acquire blocker (+ a doctor docstring nit, folded → 178c9b1).
- **lead adversarial-verify (folded this pass):** **(P1)** roster `remove`/`rename`/
  `retire` of a managed agent left a dangling `managed_lead_loop` key → `validate_…`
  raised → `load_config` exit 2 for EVERY command (un-recoverable in-tool). Fix:
  `_strip_identity`/`remove_agent` pop the managed key; `rename_agent` carries the
  spec onto the new name (parity with role/group/liaison); `load_config` SELF-HEALS a
  dangling key in-memory (prune + warn-once) so an already-bricked config recovers.
  **(P2)** managed `armed` false-ERRORed on a healthy long turn (hb-only rule was
  stricter than the guard) → armed dropped the raw-heartbeat gate (this round; later
  refined to the confirmed-dead tri-state — see the codex bullet below + D-12 for the
  final predicate). **(P2)** `ttl_seconds`/`cadence_seconds` accepted NaN/inf
  (`v<=0` is False for both) → reject `not math.isfinite(v)` at the single validate
  choke point. **(convergence)** `lead_loop_active_owner` is now config-gated on the
  managed flag (mirrors `_lease_stealable`) so a stray lease for a manual identity
  never guards its mailbox at the store layer, independent of the CLI guard.
- **reviewer-1 final-fold review (84babac, release-blocker):** a **dead owner within
  TTL** was unarmed (detector) AND unguarded (`lead_loop_active_owner` → None) yet
  **un-stealable** until TTL expiry — a down-but-unrecoverable limbo, and a hole in
  the "armed = exact complement of stealable" claim. First fix made `_lease_stealable`
  steal a dead owner immediately, regardless of expiry.
- **codex final-fold review (2142e84/7bd4a85, release-blocker) → lead D-12 ruling
  (Option A):** the first fix based immediate steal on `not _process_alive(pid)`, but
  `_process_alive` is **fail-quiet** — it returns False for *uncertain* probes
  (access-denied, ambiguous OpenProcess failures, any exception), not only for a
  confirmed-dead pid. So an uncertain probe could **immediate-steal a LIVE
  controller** (codex reproduced it by forcing the probe False for a live pid). The
  safety premise "a live owner can never look dead" was false for this codebase.
  **Fix (lead Option A):** a dedicated tri-state `_process_liveness` → ALIVE / DEAD /
  UNKNOWN, where **DEAD is only a DEFINITIVE OS not-running signal** (POSIX ESRCH;
  Windows `GetExitCodeProcess != STILL_ACTIVE` or `OpenProcess` →
  `ERROR_INVALID_PARAMETER`). Steal, the `armed` detector, AND the guard all use it:
  CONFIRMED-DEAD → immediate steal/unarmed/unguarded; ALIVE *or* UNKNOWN →
  probably-alive (armed, guarded, stealable only when EXPIRED *and* heartbeat-stale).
  A fail-quiet/uncertain probe can never immediate-steal a live owner; a genuinely-
  dead-but-unprobeable owner still recovers via expiry+heartbeat. This restores the
  exact complement for EVERY case (alive, dead, *and* unknown): `not stealable ==
  armed`. See DESIGN.md **D-12**. Regression tests pin: confirmed-dead steals
  immediately + recovers; UNKNOWN within TTL is armed/guarded/not-stolen (codex's
  forced-false-dead repro); UNKNOWN recovers via expiry+stale-heartbeat.

## SHIPPED v0.40.0 — hardening batch (`hardening-batch-040`)

Origin: the **2026-06-28 fresh-agent audit** (6 independent reviewers; the
operator asked every agent to spawn its own fresh reviewer). The audit found a
cluster of real, *shipped* false-GO / authority defects the normal cadence had
missed — validating the assurance posture. Branch `hardening-040`; designed by
codex, gated by lead, built by dev-2, cross-reviewed by codex + reviewer-1.

- **C1 · gates.py fail-closed cluster (P1).** Four distinct fail-opens, all
  letting a release report GO when it should HOLD:
  (1) `set/waive` silently overwrite a *corrupt* `gates.json` (dropped the
  corruption HOLD); (2) unlocked read-modify-write lost-update; (3) a required
  gate under a mismatched scope was dropped (absence=pass); (4) a `severity=blocker`
  gate set `status=skipped` returned GO (reproduced). *Fix:* refuse mutation on
  `load_error`; lock the full RMW; present-but-wrong-scope blocks; `skipped`≠green
  for blockers; **+ new `tests/test_gates.py`** (the module had zero dedicated
  tests). *Why it mattered:* the subsystem meant to prevent false-GO was the
  weakest link.
- **C2 · lane shared-approval authority (P1).** `_shared_approved` accepted a
  path *prefix* match, so a broad `schema/**` approval cleared a nested
  `schema/secret.sql` whose distinct approvers were never consulted; and the
  verdict never revalidated persisted approvals. *Fix:* drop the prefix arm;
  persist the matched entry glob (not raw path); revalidate at verdict time →
  emit the previously-dead `HOLD_SHARED_WRONG_APPROVAL`; and require approval from
  **every** matching shared entry — a touched path is cleared only when each
  matching entry has a fresh approval by an authorized approver (no winner-picking
  between overlapping globs; that ordering was twice unsound). Validation rejects
  duplicate normalized shared globs. 3 reviewers converged; the fix iterated
  prefix → most-specific → all-matching as review reproduced deeper bypasses (D-11).
- **C3 · wrapper one-shot + release authority (P2/P3).** One-shot reviewer could
  starve/hang behind an unrelated unread message; the loop left a stale `.waiting`
  marker on exit; `is_release_authorized` still carried a zero-lead fallback that
  diverged from the v0.39 fail-closed envelope. *Fix:* scoped receive + bounded
  timeout; `try/finally clear_waiting`; `is_release_authorized` delegates to the
  single `loop_exit_relay_authorized` resolver.
- **C6 · end-to-end regression test (operator request).** New
  `tests/test_e2e_lifecycle.py`: in-process `cli.main` over a real throwaway git
  repo, asserting exit codes + JSON verdicts + on-disk state across the full
  lifecycle, **including negative assertions that the C1/C2/C3 bugs stay fixed**
  and that `reset` preserves the durable set. Covers the previously-untested
  CLI↔core wiring, git adapter, and reset durability boundary.

Status: **SHIPPED as v0.40.0** (merge SHA `e0e8f7b`). All four clusters approved by
both reviewers and lead-gated (ruff/bandit/diff-check clean, 1213 passed on 3.10
AND 3.14). The C2 authority fix iterated through three reproduced bypasses before
review settled on all-matching (D-11).

## SHIPPED v0.40.1 — fast-follow (merge SHA `1962b92`)

Both approved + lead-gated (1227 passed on 3.10 AND 3.14). Review folded one real
C5a P1 (structural → semantic artifact readback). WP resolver deferred (banked).

- **C4 · knowledge gaps (0.38.0).** `roster --expertise` now uses the curated view;
  anchor staleness fails closed (`missing_verified_baseline` for a null path/symbol
  baseline, `unsupported_wp_anchor` for a pathless `wp`, exact `msg_id`, scan
  failure → unresolvable); `publish`+`curate` share one durable append helper
  (lock + flush + fsync, Windows-guarded); `knowledge onboard` is bounded
  (`--limit`, default 20, grouped domain→type).
- **C5 · TOCTOUs.** `lane deliver` reads back + shape/verdict-validates the delivery
  artifact before clearing the lane (a HOLD/wrong-schema artifact can no longer
  clear it); `write_restart_request` + `clear_restart_request` share the config
  lock so a stale clear cannot remove a newer marker.

## SHIPPED v0.41.0 — dead-letter / poison-message handling (`dead-letter-build`)

Origin: the 0.30.0 **poison-message head-of-line** known limitation + the
2026-06-28 backlog ("do all of them"). A message failing *deterministically* at the
head of a mailbox drove an unbounded backoff-restart loop. Designed by codex
(ultracode rubric gate), built by dev-2, cross-reviewed by codex + reviewer-1, and
lead-gated through **six** adversarial-verify passes — the 6th caught a real
classification bug that 2/2 reviewers + five passes had missed (folded before ship).

- **Durable attempt ledger + recoverable sink.** Per-agent ledger
  (`state/dead-letter-attempts/<agent>.json`), write-ahead before each drive so a
  crash mid-turn counts; on exhaustion the head message moves to a scan-invisible,
  recoverable sink (`dead-letter/<agent>/`) and the cursor advances **last** — never
  unless the bytes are recoverable; collision-safe; idempotent replay.
- **Three-way failure taxonomy.** poison-eligible (terminal turn-failed +
  crash-mid-turn) → low *consecutive* cap K=3; known-global-infra
  (spawn/auth/rate-limit/network/5xx + recognized retryable transport drop) →
  **never** auto-DL, escalate at K=20; ambiguous/unknown → escalate + auto-DL only
  at K=20. No class loops forever; a sustained outage never false-DLs at the low cap.
- **6th-verify P2 (folded, `cd39e12` → `b91e400`).** `_classify` checked the
  started/partial branch *before* the retryable→infra branch, so a retryable
  transport drop *after* the handshake (codex "Reconnecting…", claude rate-limit
  mid-stream) was misclassed ambiguous and could false-DL a healthy message at the
  ceiling during an outage. *Fix:* a recognized retryable signal classifies **infra**
  before the started branch (terminal-text precedence preserved) + regression tests.
- **Restore = requeue** (fresh-id message; no cursor rewind). **CLI:**
  `dead-letter list/show/requeue`; **doctor** loud on no/solo escalation target or
  unreadable sink; **scope:** supervised continuous loop only (manual `listen` +
  one-shot untouched — documented v1 boundary).

Status: **SHIPPED as v0.41.0**. 2/2 reviewer-approved on the frozen SHA + lead-gated
(ruff/bandit/diff-check clean, 1280 passed on 3.10 AND 3.14).

## BACKLOG

- **Dead-letter defense-in-depth (P3, fast-follow).** Banked from the dead-letter
  review/verify: (1) `ack` / `advance_cursor` accept an arbitrary id on write (no
  `_ID_RE` guard) — an operator cursor-skip vector; (2) the disposal path is not
  wrapped against a transient IO error mid-move (fail-closed today, but a retry +
  backoff would be more robust); (3) no idle/startup reconcile of a stuck
  `in_progress` / orphan-sidecar, and `doctor` does not warn on it.
- **Model-tiering / routing.** Route work to model tiers by task class.
- **Restart resilience.** Restart-notice, checkpoint-before-compact skill,
  richer `request-launch`.
- **Auto-provision per-agent git worktree** for supervised/parallel dev (the
  harness already has a worktree-isolation concept) — removes the manual
  isolated-worktree step from the cadence.

## KNOWN LIMITATIONS (shipped & accepted)

- **Poison-message head-of-line blocking** — fixed for the supervised wrapper loop
  in v0.41.0 (dead-letter). Manual `listen` and one-shot turns remain out of v1
  scope (documented boundary).
- **Wrapped-Codex conservative 900s stale threshold** — chosen for safety; may
  delay degraded detection for wrapped Codex.
- **No explicit visible idle signal** (minor UX).
- **`degraded.py` `window_seconds >= stuck_after` invariant** is false for
  wrapped Codex (900s) — latent, telemetry-only, no live mis-fire (P2, banked).
- **Defense-in-depth nits (P3, banked):** state helpers don't re-`validate_agent_name`
  (CLI validates upstream); `_process_alive` treats exit 259 (STILL_ACTIVE) as
  alive; `domains` normalize allows trailing dots / reserved device names (not a
  path escape); ephemeral `skill`/`profile` weak validation (not shell-exploitable;
  layered behind disabled-by-default + authorized-lead).

## Audit findings → disposition (2026-06-28)

Full point-in-time report (methodology, per-reviewer detail, what-held): `docs/audit-2026-06-28.md`.

| # | Finding | Sev | Reviewers | Disposition |
|---|---|---|---|---|
| 1 | gates corrupt-overwrite false-GO | P1 | security | C1 / 0.40.0 |
| 2 | gates unlocked RMW | P1 | security, reviewer-1, dev-2 | C1 / 0.40.0 |
| 3 | gates scoped-drop absence=pass | P1 | dev-2 | C1 / 0.40.0 |
| 4 | gates skipped-blocker→GO (reproduced) | P1 | reviewer-1 | C1 / 0.40.0 |
| 5 | gates: no `tests/test_gates.py` | P0(cov) | test-coverage | C1 / 0.40.0 |
| 6 | lane shared-approval over-grant + no revalidation | P1 | dev-2, test-coverage, reviewer-1 | C2 / 0.40.0 |
| 7 | lane overlapping-glob authority (all-matching-must-approve) (reproduced) | P1 | reviewer-1, codex | C2 / 0.40.0 |
| 8 | `kind=end` from any sender | P1 | dev-2 | **fixed in v0.39.0** |
| 9 | wrapper release-authority drift | P1→cleanup | codex/Kepler | C3 / 0.40.0 |
| 10 | wrapper one-shot starve/hang | P2 | dev-2, reviewer-1 | C3 / 0.40.0 |
| 11 | wrapper leaves `.waiting` on exit | P3 | codex/Kepler | C3 / 0.40.0 |
| 12 | e2e regression test (operator ask) | — | test-coverage | C6 / 0.40.0 |
| 13 | knowledge `roster --expertise` wrong view | P2 | correctness | C4 / 0.40.1 |
| 14 | knowledge wp/request/null-sha anchor fail-open | P2/P3 | reviewer-1, correctness, Kepler | C4 / 0.40.1 |
| 15 | knowledge dup writer / no fsync | P2 | Kepler | C4 / 0.40.1 |
| 16 | knowledge `onboard` doc drift | P3 | correctness | C4 / 0.40.1 |
| 17 | close publish unlocked TOCTOU | P2 | security | C5 / 0.40.1 |
| 18 | lane deliver direct-write no readback | P2 | dev-2 | C5 / 0.40.1 |
| 19 | clear_restart_request TOCTOU | P2 | dev-2 | C5 / 0.40.1 |
| 20 | P3 defense-in-depth (×3) | P3 | dev-2/Kepler | banked |
| — | ephemeral skill/profile validation | P3 | security | banked |

None were remotely exploitable or recall-worthy (they require pre-existing
corruption, specific mis-use, or are conservative/advisory).

## Recently shipped (rationale in CHANGELOG.md / docs/DESIGN.md)

- **v0.40.1** — fast-follow hardening: knowledge expertise curated-view, anchor
  staleness fail-closed, one durable writer, bounded `onboard`; lane delivery
  artifact verified-before-clear; restart-marker lock.
- **v0.40.0** — post-audit hardening: gates fail-closed, lane all-matching shared
  approval [D-11], wrapper one-shot + resolver unification, first e2e regression
  test. (Origin: the 2026-06-28 fresh audit — `docs/audit-2026-06-28.md`.)
- **v0.39.0** — stand-down authority (idle = always listening; human-origin
  loop-exit envelope). [D-7]
- **v0.38.0** — knowledge layer (append-only pointer memory; capture-open +
  curate-gated; anchor-relative staleness). [D-6, D-8]
- **v0.37.0** — lane deliver-gate (middle-tier Phase 1).
- **v0.36.0** — ephemeral adversarial reviewers (evidence-only). [D-9]
- **v0.31.0–v0.35.0** — domain registry + the assurance arc (gate, close,
  sign-offs, devkit skills).
- Full history: `CHANGELOG.md`.
