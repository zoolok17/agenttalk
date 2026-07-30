# Design 87-A: Revision 2 split-disposition audit

**Status:** Complete author audit against
`20fa5f238826d7dafa334e6589b6e0392bbe37af`; design only.

**Mode:** Reference.

**Audience:** Design 87 split-integrity reviewers.

**Purpose:** Prove that splitting Revision 2 did not silently lose an
obligation. The normative 87-A specification remains
[`DESIGN-87A-supervisor-classifier-authority.md`](DESIGN-87A-supervisor-classifier-authority.md).

## Method and status vocabulary

The author diffed the complete predecessor
`20fa5f2:docs/DESIGN-87-supervisor-recovery-authority.md` against 87-A,
including its decision, safety constraints, state machine, delivery promise,
migration plan, rejected failure modes, and regression list. Repeated prose and
tests are grouped by one semantic obligation below.

Every row uses exactly one permitted disposition:

- **PRESENT** — specified in 87-A, including an explicit safer replacement;
- **DEFERRED** — assigned to named 87-B or 87-C work; or
- **DROPPED** — intentionally removed from Design 87, with the replacement or
  external task stated.

There is no unassigned category.

## Present in 87-A

| ID | Revision 2 requirement | Disposition in 87-A |
| --- | --- | --- |
| A01 | Derive runtime, presence, teardown, replacement, and escalation independently; policy cannot erase required escalation. | **PRESENT.** Closed constructors, authority equations, and combiner. |
| A02 | Rank overlapping runtime reasons deterministically. | **PRESENT (replacement).** Mixed dominant/membership operands are replaced by the independently verified dominant-only convention; reasons remain diagnostics/fingerprint input. |
| A03 | Define all 12 runtime states and strict verdicts. | **PRESENT.** Exact state semantics are inlined; idle/terminal always map to `CURRENT_STALE_RECOVERABLE`; positive healthy/present holds are identified as green states. |
| A04 | Bind current runtime to managed identity without importing physical presence. | **PRESENT.** Strict runtime/managed-identity binding excludes `WrapperPresenceResultV1`. |
| A05 | Classify snapshot-derived active-child ambiguity. | **PRESENT (replacement).** `ActiveChildObservationV1` is an explicit independent process projection and the only legal snapshot-to-runtime input. Raw child subreasons remain typed diagnostics; the banked semantic fingerprint hashes their collapsed runtime reason. |
| A06 | Preserve generation/sequence high-water and sticky regression across a torn/invalid read. | **PRESENT.** One pure continuity reducer preserves wrapper turn-generation and progress-sequence high-water plus latch, rejects lower-turn replay, resets confirmation counters, and clears only on a different bound wrapper or strictly higher turn. |
| A07 | Treat absent, unsupported, and invalid runtime as distinct fail-closed values. | **PRESENT.** Closed 16 KiB envelope and strict current-schema validation. |
| A08 | Use one process observer and one complete relevant-candidate universe for planner, teardown resolution, and barrier. | **PRESENT.** Immutable capture, closed failures, candidate grouping, sort/dedup, and shared entry points. |
| A09 | Make wrapper presence and kill targetability total over mixed candidates. | **PRESENT.** Banked precedence and `TargetabilityProofV1` bijection remain unchanged. |
| A10 | Classify heartbeat freshness, finite launch grace, and never-launched/missing heartbeat. | **PRESENT.** Closed bounded raw heartbeat capture, nonrenewable `first_managed_epoch`, real-launch deadline, exact threshold/skew boundaries, and post-loss convergence. |
| A11 | Make chronic snapshot failure eventually visible rather than a permanent fresh hold. | **PRESENT.** Unchanged first-managed anchor makes `CONTRACT_ABSENT × UNKNOWN` stale and escalation-required after finite grace. |
| A12 | Give all classifier mutation one checked observation delta. | **PRESENT.** `ClassifierStateV1` and `ClassifierObservationDeltaV1` own freshness, continuity, child counters, poll identity, absence, consumed manual IDs, and shared automatic/manual execution fencing. |
| A13 | Require complete targetability for automatic teardown. | **PRESENT.** Banked automatic equation and target bijection. |
| A14 | Preserve explicit manual authority without using the marker as identity. | **PRESENT.** Closed raw capture, strict marker schema, one fail-closed live configuration snapshot, target/absence candidate, protection gates, cooldown, reservation-bound execution recapture, and final barrier. |
| A15 | Distinguish absent, corrupt, and unreadable restart-marker inputs. | **PRESENT (strengthening).** `ManualMarkerCaptureV1` prevents the shipped `None` conflation; only locked path absence permits automatic fallthrough. |
| A16 | Preserve manual configuration/stand-down override, backoff bypass, readiness reset, protection authorization, cooldown, and safe-attribution gates. | **PRESENT (strengthening).** Exact order, equations, and state deltas are specified. Manual origin cannot override the earlier dry-run, kill-switch, supervisor, population, action-enable, or auto-restart gates. Unlike Revision 2's fresh-heartbeat-only rule, every selected protected live kill now requires acknowledgement; confirmed-absence no-kill still does not. This adds a safety hold and never widens kill authority. |
| A17 | Select one origin when manual and automatic teardown overlap. | **PRESENT.** Manual-wins selector and authority-ID binding; the banked overlap control remains. |
| A18 | Separate current absence from post-teardown proof. | **PRESENT.** Ordinary absence reducer and synchronous action-scoped conditional use separate entry points. |
| A19 | Require two compatible complete ordinary absence polls. | **PRESENT.** Typed sample/confirmation, exact coverage equality, durable poll identity, replay/gap/reset transitions. |
| A20 | Combine physical absence and launch timing without contradiction. | **PRESENT (replacement).** Monolithic `NOW_ABSENT_CONFIRMED` becomes independent physical proof and timing eligibility, the seam task #116 needs. |
| A21 | Make absence proof one-use and bind it to the final barrier. | **PRESENT (strengthening).** Atomic confirmation consumption precedes the barrier; failure requires two new polls. |
| A22 | Ensure failed post-teardown scans count as zero later absence polls. | **PRESENT.** Separate reducer path; next ordinary clear poll is only `OBSERVED_ONCE`. |
| A23 | Cross every runtime dominant with reachable `ABSENT`. | **PRESENT.** Banked 96-cell dominant projection plus temporal overlays. |
| A24 | Preserve exact automatic action/escalation formulas and distributions. | **PRESENT.** Independently recomputed 81/3/12 action and 43/53 escalation counts remain unchanged. |
| A25 | Make semantic incident-condition equivalence complete and stable. | **PRESENT (replacement).** Underspecified fingerprint becomes versioned canonical `RecoveryConditionFingerprintV1`, bounded candidates/tail digest, and fixed vectors. |
| A26 | Supply durable redacted condition evidence, including rootless, foreign-root, unreadable, PID/start, executable, command-shape, and parse failures. | **PRESENT.** Typed `RecoveryConditionV1`, action resolution, and separate bounded operator diagnostic summary export evidence to 87-B without changing authority or the banked fingerprint. |
| A27 | Hold every launch on fresh observer disagreement. | **PRESENT.** Shared final barrier after reservation; no survivor becomes a target. |
| A28 | Remove the shipped `snapshot_unavailable + no_prior_process` launch exception and disclose the trade. | **PRESENT.** The one-condition cold-start outage versus three-condition duplicate-launch race is explicit; activation requires 87-B projections. |
| A29 | Preserve no-new-daemon, persistence-plane, or runtime-dependency scope. | **PRESENT.** `dependencies = []` remains a stated constraint. |
| A30 | Disclose that 87-A does not shorten the 180/2400-second heartbeat thresholds. | **PRESENT.** Task #116 is identified as the user-visible recovery change and is mechanically scheduled after #115, not #87. |
| A31 | Distinguish the live-wedge watchdog rationale from positive process absence. | **PRESENT.** Epistemic boundary is explicit; #116 may change timing only after independently captured compatible absence. |
| A32 | Preserve bounded healthy/unknown-child escalation nondeterminism without widening kill. | **PRESENT.** The text promises only the conditional table result and explicitly makes no recurrence/silent-forever claim. |
| A33 | Keep current attributable, rooted-unattributable, dead-wrapper, and no-root manual cases safe. | **PRESENT.** Total manual candidate/gates plus presence, targetability, absence, and barrier tests cover each class. |
| A34 | Make dry run and policy hold non-consuming, and make failed teardown/launch fail closed. | **PRESENT.** Reservation-bound global execution eligibility, absence/execution state transitions, and durable ambiguous-launch ownership for both origins make every 87-A no-action/failure path non-consuming or explicitly tombstoned. |
| A35 | Require executed table, reducer, overlap, failure, fingerprint, observer, and barrier evidence before conformance. | **PRESENT.** Mandatory conformance evidence covers the complete 87-A surface. |

## Deferred to named split documents

| ID | Revision 2 requirement | Disposition |
| --- | --- | --- |
| B01 | Persist a condition activation before any action/kill-switch branch. | **DEFERRED to 87-B.** It consumes `RecoveryConditionV1`; activation order and checked persistence before action and kill-switch handling are its contract. 87-A grants that observational path no recovery authority. |
| B02 | Define `DUE`, rate-limited, pending, unconfigured, delivered, and resolved condition states. | **DEFERRED to 87-B.** |
| B03 | Guarantee routine-surface persistence independently of routed human receipt. | **DEFERRED to 87-B.** |
| B04 | Freeze sender, recipient, kind, body, semantic metadata, and operation nonce; publish idempotently. | **DEFERRED to 87-B.** |
| B05 | Specify null routing, retention, rate limiting, resolution, and delivery retry. | **DEFERRED to 87-B.** |
| B06 | Preserve old-compatible event projection/dedup and crash-safe state/event transitions. | **DEFERRED to 87-B** for promise semantics; 87-C owns state/event schema migration. |
| B07 | Project status, doctor, attention, and web diagnostics. | **DEFERRED to 87-B.** |
| B08 | Provide wrapper/dead-letter safe-recovery-or-visible condition handling. | **DEFERRED to 87-B.** |
| B09 | Make blocked manual restart durably visible without treating its barrier observation as the incident. | **DEFERRED to 87-B.** 87-A exports the closed marker/action disposition. |
| B10 | Failure-inject event/state/publish/delivery transitions and all early-return roots. | **DEFERRED to 87-B.** 87-A retains classifier/action controls. |
| B11 | Define full dry-run and kill-switch incident persistence, projection, mutation ordering, and any observational exception. | **DEFERRED to 87-B.** 87-A defines only the executor-side zero-recovery-mutation contract; task #114 is prerequisite to observing past cold-start kill switch. |
| C01 | Negotiate planner/executor capability and supported schema; preserve exact legacy behavior under skew. | **DEFERRED to 87-C.** |
| C02 | Add compatible top-level state extension and old-writer preservation. | **DEFERRED to 87-C.** |
| C03 | Define dormant release parity, activation unit, controlled restart, and no partial 87-A-without-87-B activation. | **DEFERRED to 87-C.** Activation also requires task #114 and matching-generation 87-B projections; task #115 precedes durable 87-A state. |
| C04 | Define controlled/emergency rollback and temporary projection limits. | **DEFERRED to 87-C.** |
| C05 | Define rolling replacement, pending compatibility count, and the universal flag-day runbook. | **DEFERRED to 87-C.** |
| C06 | Migrate current unversioned restart-marker producers to `ManualRestartMarkerV1`. | **DEFERRED to 87-C.** 87-A owns the target schema and authority semantics. |
| C07 | Detect and safely roll legacy pre-`--root` wrappers. | **DEFERRED to 87-C** for migration procedure; 87-A owns fail-closed detection. |
| C08 | Prove package/script/state/event skew and every allowed release boundary. | **DEFERRED to 87-C.** |

## Intentionally dropped or externalized

| ID | Revision 2 item | Disposition and reason |
| --- | --- | --- |
| D01 | Design around cold-start kill-switch exit before instance claim. | **DROPPED from Design 87; externalized to task #114.** This is a present-tense source bug and 87-B depends on its fix. |
| D02 | Rely on an “existing supervisor-state lock.” | **DROPPED as false premise; externalized to task #115.** No lock spans current read-modify-write. 87-A specifies pure deltas; implementation/activation wait for #115. |
| D03 | Reuse a confirmed absence proof by restoring all pre-action state after barrier veto. | **DROPPED.** Typed one-use proof is consumed at reservation. Fresher barrier disagreement invalidates reuse; the manual marker remains pending and two ordinary polls must rebuild proof. |
| D04 | Classify every guarded pre-`--root` wrapper as `PRESENT_UNTARGETABLE`. | **DROPPED as a permissive special case.** Banked total precedence classifies a root-unreadable relevant candidate `UNKNOWN`; both deny kill/launch, while `UNKNOWN` conservatively preserves stale escalation. |
| D05 | Keep one monolithic classifier/delivery/migration specification. | **DROPPED.** The growing cross-lens free-dimension count motivated the explicit 87-A/87-B/87-C split. |

## Preserved rejected-failure constraints

Revision 2's rejected proposals remain accounted for:

| Rejected proposal family | Disposition |
| --- | --- |
| Positional wrapper-dead branch | **PRESENT.** 87-A derives one total independent escalation output outside branch order. |
| `notify=true` on scattered early returns | **DEFERRED to 87-B.** It owns one persistence/delivery path rather than per-branch hints. |
| Fresh legacy heartbeat as green; stale heartbeat as identity; unconditional stale relaunch | **PRESENT.** Runtime, presence, targetability, and absence remain independent. |
| Automatic kill of legacy/unsupported/ambiguous wrappers or widened kill authority | **PRESENT.** Complete targetability is mandatory; uncertainty holds/escalates. |
| Unavailable/incomplete snapshot as absence | **PRESENT.** Total presence and final barrier fail closed. |
| Salvage authority fields from a future runtime schema | **PRESENT.** Only bounded schema version is exposed. |
| Trust stale runtime as process liveness | **PRESENT.** Process presence remains independent. |
| Turn a barrier survivor into a target or weaken the barrier | **PRESENT.** Barrier supplies deny evidence only. |
| Automatically relaunch fresh absence | **PRESENT with task #116 seam.** 87-A requires timing eligibility; #116 may shorten only after independently confirmed absence. |
| Clear high-water on one invalid read | **PRESENT.** Explicitly forbidden by the sticky continuity reducer. |
| Use manual-restart detail as incident record | **DEFERRED to 87-B.** 87-A exports typed manual/action evidence. |
| Treat event `notify=true` as delivery; plain send-then-save; advance rate-limit before persistence | **DEFERRED to 87-B.** Its promise must define checked persistence and idempotent delivery. |
| Add a daemon/database/dependency | **PRESENT.** 87-A retains the no-new-plane/no-dependency constraint. |
| Invent a separate migration plane | **DEFERRED to 87-C.** Its migration must use existing state/schema mechanisms. |

## Audit conclusion

All Revision 2 obligation families are present, named-deferred, or
reasoned-dropped. The independently verified 87-A matrix, presence/targetability
classifier, absence reducer, and fingerprint mechanics were not changed by
this audit.
