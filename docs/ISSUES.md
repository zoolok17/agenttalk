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

## SHIPPED v0.42.0 — split-identity lead-loop enforcement (Slice 1 + WP1–4)

Origin: the **operator-raised "the lead stops leading"** failure (every lead, every
project): a chat-agent lead silently UN-ARMS its control loop — the harness default
("answer the human, then yield") drops the re-arm even though the discipline is
documented and in memory. A documented-but-reliably-dropped rule means willpower is
NOT the fix; we need MECHANICAL enforcement. The operator chose **split-identity
enforcement** (cli-AGNOSTIC — keyed on the agent NAME + its `managed_lead_loop`
config, never a cli, so a codex identity is managed exactly as a claude one): keep the
free-form `<name>` as the operator-facing liaison (manual, never auto-killed), and add
a separately-supervised managed `<name>-lead-loop` identity that OWNS the team mailbox
via a renewable lease and cannot silently un-arm. codex designed it as Slice 1 + four
WPs; dev-2 built each in an isolated worktree; codex + reviewer-1 cross-reviewed; lead
gated. Rationale + decision log: `docs/DESIGN.md` D-12..D-15.

- **Slice 1 (61e2ce0) — store-level lease + single-consumer guard + visibility.**
  `state/<agent>.lead-loop-lease.json` is the ownership truth (acquire/renew/release/
  read; `.waiting` only mirrors it for UX); a verb-guard rejects a second mailbox
  consumer (`wait`/`recv`/`drain`/`ack`, exit 7) while read-only verbs stay allowed,
  the owner proving itself via the live `lease_id` (`AGENTTALK_LEAD_LOOP_LEASE`);
  `lead_unarmed` is an ERROR in `status`/`doctor`/supervisor for a managed identity
  that is down. Steal is gated on the configured `managed_lead_loop` flag + a
  CONFIRMED-dead tri-state liveness probe (D-12): a confirmed-dead owner is stolen
  immediately (a crashed controller recovers at once), a live/uncertain one only when
  EXPIRED *and* heartbeat-stale (an uncertain probe never displaces a live owner; a
  manual chat identity is never auto-stolen).
- **WP1 (24c39f7) — single authority + timing resolver + corrupt-config class.**
  One `_lead_loop_authority` computes {managed, present, liveness, expired,
  heartbeat-stale, stealable, armed, guarded, reason}; `_lease_stealable`,
  `lead_loop_state`, and `lead_loop_active_owner` ALL derive from it, so the three
  views can never drift (for a present managed lease `armed == not stealable` in
  every case). `read_lead_loop_lease` normalizes `expires_at` to a finite float or
  None (fail-safe NOT-expired). A timing resolver (`lead_loop_runtime.resolve_timing`)
  gives the steal path and the visibility paths one shared `heartbeat_stale_after`
  (a duplicate never steals earlier than the supervisor would call the owner stuck),
  keeping `store.py` free of supervisor imports. A truthy non-dict supervisor config
  entry (operator typo) coerces to default via `isinstance` instead of crashing
  status/doctor/supervise/wrap — the corrupt-config coercion class swept across every
  per-agent reader (D-13).
- **WP2 (152636e) — the lead-loop CONTROLLER (`wrap --loop --lead-loop`).** A
  long-running supervised process that owns the mailbox for its whole lifetime:
  acquire-before-loop, a combined renew+heartbeat on every idle stamp and streaming
  event, an ownership gate at every cursor-advance boundary (a lost lease stops
  consumption at once), and three exit states the supervisor reads from an exit
  marker — blocked-acquire (HOLD, no relaunch), valid human release/end (deliberate
  stand-down, no relaunch), crash/lost-lease (relaunch + re-acquire). The lease token
  is never leaked to the model child.
- **WP3 (fe6ed6b) — the proactive CADENCE TICK.** The idle/timeout branch of the
  SAME loop (no second consumer/thread) drives a SYNTHETIC sweep over a bounded
  read-only snapshot (ids + summaries, never transcripts, never the lease token) when
  the bus is quiet and the cadence interval elapses — nudging stalled outbound threads
  (once per `(request_id, last_msg_id)`, past `reminder_after_seconds`, no fresh-peer
  composing marker) and surfacing dead-letter / unrouted escalations (deduped),
  spending a model turn only when something is actionable. It NEVER records an attempt,
  advances the cursor, or enters the dead-letter path; a failed sweep is
  controller-HEALTH (backoff + heartbeat withheld + a single deduped escalation
  retried until it routes), never message poison (D-14).
- **WP4 (de2873a) — the mechanical liaison RELAY (`agenttalk relay`).** Carries the
  operator's words across the human<->bus boundary with an audit stamp and NO new
  message kind: `relay operator-answer --to-request <rid>` validates a *pending*
  `needs_operator` escalation addressed to the liaison and routes the answer back to
  the asking lead-loop (flipping the thread to `operator_state=answered`); `relay
  operator-command` relays a spontaneous operator instruction to a managed lead-loop,
  inferring `--to` only when exactly one exists and FAILING CLOSED unless the sender is
  the operator-facing liaison (an audited `--override --reason` is the only exception).
  Both handlers are authoritative for the reserved audit meta — a caller `--meta` can
  never forge an audit marker or graft routing onto a relayed message (D-15). The
  lead-loop → operator direction stays the existing `escalate`; no new kinds.
- **Cadence-snapshot threshold-skew fix (8b8e79e) — the v0.42.0 release blocker.**
  reviewer-1's consolidated strict-2/2 review of the merged arc caught that
  `build_cadence_snapshot` resolved the supervisor heartbeat window into
  `snap["timing"]` but built `snap["lead_loop_health"]` via `lead_loop_state(agent)`
  WITHOUT it, so the cadence health view fell back to the 120s default while
  steal/guard/supervisor used the resolved window (e.g. 900s) — the *same*
  threshold-skew class WP1 set out to kill, resurfacing in the visibility snapshot,
  where it could hand the model a FALSE controller-down state while the controller
  still owned the lease. *Fix:* thread `now=now_epoch` + the resolved
  `heartbeat_stale_after` into the health call (None → safe default), inside the
  existing degrade-guard; pinned by a WP3 regression
  (`test_snapshot_health_uses_resolved_window_not_default`) that fails unfixed /
  passes fixed.

*Where:* `store.py` (lease API + `_lead_loop_authority` + `_process_liveness`
tri-state + read normalization + confirmed-dead-only lock-break + cadence-state),
`lead_loop_runtime.py` (NEW, timing resolver), `lead_loop_cadence.py` (NEW, snapshot +
actionability + cadence state + health), `wrapper/loop.py` + `wrapper/run.py` +
`wrapper/prompt.py` (controller + per-cursor ownership gate + token-strip + combined
heartbeat + the idle-branch cadence hook), `cli.py` (`wrap --lead-loop`,
`managed-lead-loop` set/clear/list, the verb-guard, `cmd_relay` +
`_RELAY_RESERVED_META`), `supervisor.py` (`_plan_one` no-relaunch rules + report
field), `doctor.py` (`_check_lead_unarmed`), `docs/DESIGN.md` (D-12..D-15),
`tests/test_lead_loop*.py` + `tests/test_relay_wp4.py` + `tests/test_supervisor.py`.

Process: each WP merged slice-internal on codex review + lead adversarial-verify +
lead full-suite gate (the independent-verify-different-focus pattern caught a real bug
at nearly every WP — config-brick, the stale-blocked-marker recovery defeat, audit-meta
forgeability — complementary to codex's resolver-skew / lost-lease / escalation-latch /
correlation-id catches). The v0.42.0 release added reviewer-1's consolidated strict-2/2
on the exact head, which caught the cadence-snapshot threshold-skew blocker; dev-2
folded it (8b8e79e); BOTH reviewers re-approved the fix on the final SHA.

Status: **SHIPPED as v0.42.0** (merge SHA `8b8e79e`). Strict 2/2 on the final SHA
(codex + reviewer-1, no findings) + lead-gated (ruff/bandit/diff-check clean, **1395
passed on Python 3.10 AND 3.14**).

**Accepted limitation (WP1): the triple-fault edge.** An owner whose liveness probe
is UNKNOWN *and* whose lease has a corrupt/None expiry *and* whose heartbeat is stale
is NOT stealable (normalized None = not-expired), so a dead-but-unprobeable owner
with a corrupt lease waits for a valid renewal it cannot make. This is the deliberate
fail-safe direction: NEVER a false steal of a maybe-live owner; delayed recovery only
under a triple fault. Pinned by a regression test.

**Accepted limitations (Slice 1).** *PID-reuse in the liveness heuristic (P2):*
steal / guard / armed use the tri-state `_process_liveness(owner_pid)` (D-12); a
recycled pid can make a dead owner look ALIVE, but the `lease_id` (not the pid) is the
real ownership token and a recycled pid won't heartbeat as this agent, so the worst
case is delayed (expiry+heartbeat) recovery, NEVER a false steal of a live owner.
*Lease + `.waiting` mirror are not atomically coupled (P3, by design):* the lease
write is atomic, the observational mirror is best-effort; a crash between them leaves a
valid lease without a mirror and readers degrade.

**Slice 1b (post-turn turn-end audit) — DEFERRED.** A post-final-answer harness hook
to verify the lead re-armed a background wait is not buildable on the current host
(only soft PostToolUse hooks exist, e.g. `agenttalk heartbeat --hook`); the managed
`lead_unarmed` detector is the substitute (a wrapped lead-loop is mechanically armed
every cycle, and the liaison no longer owns the team loop). Revisit only if the host
harness adds a reliable post-turn hook.

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

- **v0.42.0** — split-identity lead-loop enforcement: managed lease + single-consumer
  guard, single authority + timing resolver, the supervised controller, the proactive
  cadence tick, the mechanical liaison relay; fixes "the lead stops leading"
  mechanically. [D-12–D-15]
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
