# Design 87-A: Revision 2 split-disposition audit

**Status:** Complete author audit against
`20fa5f238826d7dafa334e6589b6e0392bbe37af`; design only.
This audit does not mark 87-A delivered or conforming. Revision 15 of the atomic
normative specification leaves Q4 **SPECIFIED; IMPLEMENTATION BLOCKED** on
#115, #146, #57, and `ExactIssuerIdentityAdapterV1`. Overall 87-A is
additionally blocked on the closure successor and the general merged-#120
adapter. Those additional 87-A dependencies do not widen Q4's four-dependency
disposition.

**Mode:** Reference.

**Audience:** Design 87 split-integrity reviewers.

**Purpose:** Prove that splitting Revision 2 did not silently lose an
obligation. The normative 87-A specification is the atomic set of its
[core](DESIGN-87A-supervisor-classifier-authority.md) and
[owned-childless module](DESIGN-87A-owned-childless-wrapper-authority.md).
The original 59-ID register remains intact; `N01` records the later
operator-directed requirement in the same audit.

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
| A03 | Define all 12 runtime states and strict verdicts. | **PRESENT.** Complete state semantics are inlined; idle/terminal always map to `CURRENT_STALE_RECOVERABLE`; positive healthy/present holds are identified as green states. |
| A04 | Bind current runtime to managed identity without importing physical presence. | **PRESENT.** Strict runtime/managed-identity binding excludes `WrapperPresenceResultV1`. |
| A05 | Classify snapshot-derived active-child ambiguity. | **PRESENT (replacement).** `ActiveChildObservationV1` is an explicit independent process projection and the only legal snapshot-to-runtime input. Raw child subreasons remain typed diagnostics; the banked semantic fingerprint hashes their collapsed runtime reason. |
| A06 | Preserve generation/sequence high-water and sticky regression across a torn/invalid read. | **PRESENT.** One pure continuity reducer preserves wrapper turn-generation and progress-sequence high-water plus latch, rejects lower-turn replay, resets confirmation counters, and clears only on a different bound wrapper or strictly higher turn. |
| A07 | Treat absent, unsupported, and invalid runtime as distinct fail-closed values. | **PRESENT.** Closed 16 KiB envelope and strict current-schema validation. |
| A08 | Use one process observer and one complete relevant-candidate universe for planner, teardown resolution, and barrier. | **PRESENT.** Immutable capture, closed failures, candidate grouping, sort/dedup, and shared entry points. |
| A09 | Make wrapper presence and kill targetability total over mixed candidates. | **PRESENT.** Banked precedence and `TargetabilityProofV1` bijection remain unchanged. |
| A10 | Classify heartbeat freshness, finite launch grace, and never-launched/missing heartbeat. | **PRESENT.** Closed bounded raw heartbeat capture, nonrenewable `first_managed_epoch`, real-launch deadline, exact threshold/skew boundaries, and post-loss convergence. |
| A11 | Make chronic snapshot failure eventually visible rather than a permanent fresh hold. | **PRESENT.** Unchanged first-managed anchor makes `CONTRACT_ABSENT × UNKNOWN` stale and escalation-required after finite grace. |
| A12 | Give all classifier mutation one checked observation delta. | **PRESENT (strengthened replacement).** The caller-supplied delta is removed. Task #115 remains the sole owner: begin fixes the prospective ordinal-zero `CaptureIdV1` before acquisition and creates one private atomic lineage owner; one acquisition-handle alias wins `UNUSED -> ACQUIRING`, and one opaque commit-custody alias pairing receipt to owner wins `RECEIPT -> COMMITTING`. Receipt alone is inert. The installed adapter atomically yields one commit-custody handle over a deeply immutable ordinary-observation receipt preserving that ID, and commit validates without restamping it. Candidate sequence is checked predecessor plus one; only successful commit makes it current. At maximum `uint64`, begin returns typed attended exhaustion before acquisition and never wraps. One #115 checked rollover is admitted only from trustworthy persisted top-level `IDLE`: it installs a fresh epoch at revision/sequence zero, resets capture-derived evidence, preserves managed/manual/quarantine state, and invalidates the old epoch. Any childless envelope or persisted non-childless execution state blocks rollover byte-identically, so debt, cycle, continuation, or retired-attempt state cannot be erased. A non-null configured prior-effect fence keeps every audit/source/effect byte and rebases only its freshness floor to `(new epoch, 0)`; an old-epoch barrier cannot clear it. A concurrent transient ephemeral action uses exact staged outcomes: rollover visible at the final provenance read is `VARIANT_PROVENANCE_STALE` plus `REJECTED_NO_EFFECT`; after that read invocation may resolve without a no-effect inference. Rollover-CAS-first makes a trustworthy same-agent different-epoch receipt return `STALE_CLASSIFIER_EPOCH_RETRY_REQUIRED` and close with no write/archive/new effect; receipt-commit-first applies/closes and makes the original rollover request stale; unknown commit poisons. The explicit residual is an indefinite attended hold when a persisted active obligation needs another ordinary capture. The owner derives the private observation mutation—freshness, continuity, child counters, poll identity, and absence evidence—inside the same checked RMW transaction. Callers cannot supply confirmation successors. A stale CAS loser cannot move, reseal, or restamp its completed acquisition into the successor lineage. Private non-childless authority and witness/permit-bound childless-effect variants own consumed IDs and execution fencing. Observation commits first; later authority/effect construction uses the successor revision. |
| A13 | Require complete targetability for automatic teardown. | **PRESENT.** Banked automatic equation and target bijection. |
| A14 | Preserve explicit manual authority without using the marker as identity. | **PRESENT.** Closed raw capture, strict marker schema, one fail-closed live configuration snapshot, target/absence candidate, protection gates, cooldown, reservation-bound execution recapture, and final barrier. |
| A15 | Distinguish absent, corrupt, and unreadable restart-marker inputs. | **PRESENT (strengthening).** `ManualMarkerCaptureV1` prevents the shipped `None` conflation; only locked path absence permits automatic fallthrough. |
| A16 | Preserve manual configuration/stand-down override, backoff bypass, readiness reset, protection authorization, cooldown, and safe-attribution gates. | **PRESENT (strengthening).** Exact order, equations, and state deltas are specified. Manual origin cannot override the earlier dry-run, kill-switch, supervisor, population, action-enable, or auto-restart gates. Unlike Revision 2's fresh-heartbeat-only rule, every selected protected live kill now requires acknowledgement; confirmed-absence no-kill still does not. This adds a safety hold and never widens kill authority. |
| A17 | Select one origin when manual and automatic teardown overlap. | **PRESENT.** Manual-wins selector and authority-ID binding; the banked overlap control remains. |
| A18 | Separate current absence from post-teardown proof. | **PRESENT.** Ordinary absence reducer and synchronous action-scoped conditional use separate entry points. |
| A19 | Require two compatible complete ordinary absence polls. | **PRESENT.** Typed sample/confirmation, begin-bound prospective capture ID preserved through one atomically admitted acquisition/receipt/commit, predecessor-plus-one candidate validation, exact coverage equality, durable poll identity, replay/gap/reset transitions, and stale-loser acquisition rejection. |
| A20 | Combine physical absence and launch timing without contradiction. | **PRESENT (replacement).** Monolithic `NOW_ABSENT_CONFIRMED` becomes independent physical proof and timing eligibility, the seam task #116 needs. |
| A21 | Make absence proof one-use and bind it to the final barrier. | **PRESENT (strengthening).** Atomic confirmation consumption precedes the barrier; failure requires two new polls. |
| A22 | Ensure failed post-teardown scans count as zero later absence polls. | **PRESENT.** Separate reducer path; next ordinary clear poll is only `OBSERVED_ONCE`. |
| A23 | Cross every runtime dominant with reachable `ABSENT`. | **PRESENT.** Banked 96-cell dominant projection plus temporal overlays. |
| A24 | Preserve exact automatic action/escalation formulas and distributions. | **PRESENT.** Independently recomputed 81/3/12 action and 43/53 escalation counts remain unchanged. |
| A25 | Make semantic incident-condition equivalence complete and stable. | **PRESENT (replacement).** Underspecified fingerprint becomes versioned canonical `RecoveryConditionFingerprintV1`, bounded candidates/tail digest, and fixed vectors. |
| A26 | Supply durable redacted condition evidence, including rootless, foreign-root, unreadable, PID/start, executable, command-shape, and parse failures. | **PRESENT.** Typed `RecoveryConditionV1`, action resolution, and separate bounded operator diagnostic summary export evidence to 87-B without changing authority or the banked fingerprint. |
| A27 | Hold every launch on fresh observer disagreement. | **PRESENT.** Shared final barrier after reservation; no survivor becomes a target. |
| A28 | Remove the shipped `snapshot_unavailable + no_prior_process` launch exception and disclose the trade. | **PRESENT.** The one-condition cold-start outage versus three-condition duplicate-launch race is explicit; activation requires 87-B projections. |
| A29 | Preserve no-new-daemon, persistence-plane, or runtime-dependency scope. | **PRESENT — DECIDED CONSTRAINT (operator, 2026-07-31; M5 Option A).** The promise is absolute: implementation is pure code over existing checked state plus transient caller-owned synchronization, `dependencies = []` remains hard, and there is no daemon, new persistence plane, durable helper or OS object, runtime dependency, or mechanism-specific/separately versioned exception. Merged #120 at `587e7c1` does not prove action-scoped creation closure, has no POSIX exact-token executor, supplies no trustworthy host/process-universe token, and leaves a raw supervisor owned-tree kill entry. Revision 15 specifies #146's closed dispatcher, one private atomic call-use owner per variant, one #115-minted action custody per configured/ephemeral checked transition, and a configured owner-loss HOLD plus checked attended disposal, but does not claim any is delivered. Without a fresh matching witness, no 87-A childless effect/mutation is constructible; independently, no supervisor owned-tree native termination may occur except through one independently authorized closed dispatcher native-entry winner after #146. The availability cost has four permanent V1 limitations: Linux/macOS closure-dependent named teardown remains unavailable pending a reviewed exact-token adapter; automatic state-loss-quarantine retirement remains unavailable; declared same-platform transfer/restore/rollback/migration activation refuses before active-store admission; and a process present at a configured issuer PID makes attended disposal `CAPABILITY_UNAVAILABLE(ExactIssuerIdentityAdapterV1)` because the generic start token cannot distinguish the original issuer from reuse. Owner loss at configured `RESERVED/PRE_BARRIER` holds automatically; only a fresh independent definitive PID-absent `GONE` result admits the checked attended disposal. It performs no kill or launch, abandons only the dead action, and installs the singular prior-effect fence. `ConfiguredPreBarrierRetrySuccessorV1` remains an optional future automatic exit, not a Q4 conformance blocker. Configured relaunch separately remains activation-blocked on #57. PID and start, hostname, `state_epoch`, `process_source_digest`, MachineGuid alone, and local absence are insufficient process-universe evidence. A future retirement or transfer successor may read only an existing OS token and may not add a file, registry value, helper, daemon, OS object, persistence plane, or dependency. An unprovable implementation case creates a task, not a mechanism. |
| A30 | Disclose that 87-A does not shorten the 180/2400-second heartbeat thresholds. | **PRESENT.** Task #116 is identified as the user-visible recovery change and is mechanically scheduled after #115, not #87. |
| A31 | Distinguish the live-wedge watchdog rationale from positive process absence. | **PRESENT.** Epistemic boundary is explicit; #116 may change timing only after independently captured compatible absence. |
| A32 | Preserve bounded healthy/unknown-child escalation nondeterminism without widening kill. | **PRESENT.** The text promises only the conditional table result and explicitly makes no recurrence/silent-forever claim. |
| A33 | Keep current attributable, rooted-unattributable, dead-wrapper, and no-root manual cases safe. | **PRESENT.** Total manual candidate/gates plus presence, targetability, absence, and barrier tests cover each class. |
| A34 | Make dry run and policy hold non-consuming, and make failed teardown/launch fail closed. | **PRESENT.** Reservation-bound global execution eligibility, absence/execution state transitions, and durable ambiguous-launch ownership for both origins make every 87-A no-action/failure path non-consuming or explicitly tombstoned. |
| A35 | Require executed table, reducer, overlap, failure, fingerprint, observer, and barrier evidence before conformance. | **PRESENT.** Mandatory conformance evidence covers the complete 87-A surface. |

## Post-Revision 2 operator addition

| ID | New requirement | Disposition in 87-A |
| --- | --- | --- |
| N01 | Grant automatic teardown authority over a positively owned wrapper only after two complete post-establishment CLI-child absence observations; target its complete nonce-anchored owned tree; and stop same-incumbent recovery after a hard attempt cap instead of fading into exponential backoff. | **PRESENT AS A NORMATIVE CONTRACT; IMPLEMENTATION BLOCKED.** #115 privately derives two-observation confirmation from sealed one-use receipts; #146 must deliver the closed dispatcher; and merged #120 supplies the strict 64-entry snapshot plus exact target-local Windows FILETIME behavior, but no POSIX executor, action-scoped closure, sealed effect entry, configured retry authority, or issuer-reuse comparator. Revision 15 keeps one action owner per configured/ephemeral transition, one private call-use owner per variant, closed native-entry/no-effect outcomes, and the attended rollover result. Owner loss at `NON_CHILDLESS/RESERVED/PRE_BARRIER` holds with no remint, `PRE_BARRIER_RELEASE`, release-and-reserve, kill, or launch. Only a fresh independent definitive PID-absent `GONE` result admits the checked attended disposition; a present PID, even with a different generic start token, remains `LIVE_OR_UNPROVEN` and disposal is unavailable pending `ExactIssuerIdentityAdapterV1`. The disposition preserves `PRIOR_EFFECT_UNKNOWN`, performs no kill or launch, and installs the source-bound global fence; only the matching single-consumer committed-fresh barrier custody may later clear it without effect. Configured `Start-Process` replay separately remains blocked on #57. The ephemeral rollover race retains its closed stale-epoch result without claiming native no-effect. Q4 remains incomplete, nonconforming, unsealed, and unenforced until #115, #146, #57, and `ExactIssuerIdentityAdapterV1` land and are reviewed. Overall 87-A additionally requires the merged-#120 adapter and closure successor. The four permanent V1 capability limitations, configured owner-loss HOLD/remedy, #57 residual, origin-neutral debt, three-attempt cap, retained closure fencing, and approved Q9 evidence remain unchanged; 87-B owns human delivery. |

## Deferred to named split documents

| ID | Revision 2 requirement | Disposition |
| --- | --- | --- |
| B01 | Persist a condition activation before any action/kill-switch branch. | **DEFERRED to 87-B.** It consumes `RecoveryConditionV1`; activation order and checked persistence before action and kill-switch handling are its contract. 87-A grants that observational path no recovery authority. |
| B02 | Define `DUE`, rate-limited, pending, unconfigured, delivered, and resolved condition states. | **DEFERRED to 87-B.** |
| B03 | Guarantee routine-surface persistence independently of routed human receipt. | **DEFERRED to 87-B.** |
| B04 | Freeze sender, recipient, kind, body, semantic metadata, and operation nonce; publish idempotently. | **DEFERRED to 87-B.** |
| B05 | Specify null routing, retention, rate limiting, resolution, and delivery retry. | **DEFERRED to 87-B.** |
| B06 | Preserve old-compatible event projection/dedup and crash-safe state/event transitions. | **DEFERRED to 87-B** for promise semantics; 87-C owns state/event schema migration. |
| B07 | Project status, doctor, attention, and web diagnostics. | **DEFERRED to 87-B.** Every unavailable-capability projection must join the source fingerprint, name the held agent, and state a concrete remedy; a bare enum or “operator attention required” without an exit is nonconforming. Operator-facing surfaces must distinguish four permanent V1 limitations: Linux/macOS closure-dependent named teardown is `CAPABILITY_UNAVAILABLE`/`POLICY_HELD`; automatic state-loss-quarantine retirement is unavailable on every platform with `STATE_PROVENANCE_LOST`/`POLICY_HELD`; declared same-platform transfer/restore/rollback/migration activation refuses before active-store admission; and configured `RESERVED/PRE_BARRIER` disposal is unavailable while the issuer PID is present, including suspected reuse, until `ExactIssuerIdentityAdapterV1` is delivered. The configured owner-loss summary distinguishes fresh independent OS `GONE` from `LIVE_OR_UNPROVEN`, names the matching reservation/checkpoint and canonical source hash, and directs the operator to create or preserve `supervisor.kill`, stop every project supervisor, refresh, then use the specified-only attended disposition when eligible. A generic start-token equality or mismatch never proves issuer reuse. The GONE-only disposition abandons only the dead action without kill or launch and persists a prior-effect-unknown fence; 87-B receives only bounded redacted summaries, never issuer PID/start/token, full targets, actor, acknowledgements, or reason. Fence clearance names the remove-kill-switch/start-one-current-supervisor/winning committed source-bound #120 barrier/attended surviving-target sequence. Stopping the wrapper alone is not a remedy. Configured relaunch activation remains blocked on #57, and `ConfiguredPreBarrierRetrySuccessorV1` is only an optional future automatic retry seam, not a Q4 blocker. |
| B08 | Provide wrapper/dead-letter safe-recovery-or-visible condition handling. | **DEFERRED to 87-B.** |
| B09 | Make blocked manual restart durably visible without treating its barrier observation as the incident. | **DEFERRED to 87-B.** 87-A exports the closed marker/action disposition. |
| B10 | Failure-inject event/state/publish/delivery transitions and all early-return roots. | **DEFERRED to 87-B.** 87-A retains classifier/action controls. |
| B11 | Define full dry-run and kill-switch incident persistence, projection, mutation ordering, and any observational exception. | **DEFERRED to 87-B.** 87-A defines only the executor-side zero-recovery-mutation contract; task #114 is prerequisite to observing past cold-start kill switch. |
| C01 | Negotiate planner/executor capability and supported schema; preserve exact legacy behavior under skew. | **DEFERRED to 87-C.** |
| C02 | Add compatible top-level state extension and old-writer preservation. | **DEFERRED to 87-C.** |
| C03 | Define dormant release parity, activation unit, controlled restart, and no partial 87-A-without-87-B activation. | **DEFERRED to 87-C.** Activation also requires task #114, matching-generation 87-B projections, and reviewed operator documentation naming all four permanent V1 limitations: POSIX closure-dependent named teardown, automatic state-loss-quarantine retirement, declared same-platform transfer/restore/rollback/migration activation, and configured owner-loss disposal while the issuer PID is present or suspected reused. That fourth limitation persists until `ExactIssuerIdentityAdapterV1` is delivered; generic start-token comparison cannot enable disposal. The owner-loss surface names the held agent, matching `RESERVED/PRE_BARRIER` checkpoint, canonical source hash, `GONE` or `LIVE_OR_UNPROVEN` status, three acknowledgement kinds, and specified-but-undelivered GONE-only attended command without exporting the issuer PID/start/token or supplied acknowledgements/reason. It requires create-or-preserve kill switch and stop every project supervisor, preserves the current null-prior-effect-fence/current-phase/current-source-hash gates, and guarantees zero kill or launch. After disposal it names the bounded prior-effect fence, global no-action gate, and remove-kill-switch/start-one-current-supervisor/winning committed source-bound barrier/attended-target clearance sequence. Configured relaunch remains blocked on #57; Q4 remains implementation-blocked on #115, #146, #57, and `ExactIssuerIdentityAdapterV1`. `ConfiguredPreBarrierRetrySuccessorV1` remains an optional future automatic retry seam. |
| C04 | Define controlled/emergency rollback and temporary projection limits. | **DEFERRED to 87-C.** V1 rollback/restore activation is unavailable: every conforming declared workflow refuses before imported state becomes active and constructs no 87-A effect. An out-of-band overwrite may be undetectable and is nonconforming, so 87-C must bind source-universe provenance within M5 Option A or keep imported state inert. |
| C05 | Define rolling replacement, pending compatibility count, and the universal flag-day runbook. | **DEFERRED to 87-C.** |
| C06 | Migrate current unversioned restart-marker producers to `ManualRestartMarkerV1`. | **DEFERRED to 87-C.** 87-A owns the target schema and authority semantics. |
| C07 | Detect and safely roll legacy pre-`--root` wrappers. | **DEFERRED to 87-C** for migration procedure; 87-A owns fail-closed detection. |
| C08 | Prove package/script/state/event skew and every allowed release boundary. | **DEFERRED to 87-C.** |

## Intentionally dropped or externalized

| ID | Revision 2 item | Disposition and reason |
| --- | --- | --- |
| D01 | Design around cold-start kill-switch exit before instance claim. | **DROPPED from Design 87; externalized to task #114.** This is a present-tense source bug and 87-B depends on its fix. |
| D02 | Rely on an “existing supervisor-state lock.” | **DROPPED as false premise; externalized to task #115.** No lock spans current read-modify-write. 87-A specifies pure equations and sealed receipt inputs; #115 remains the sole owner and privately derives/applies observation mutation inside its checked RMW. Implementation/activation wait for #115. |
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

All 59 Revision 2 IDs are present, named-deferred, or reasoned-dropped. `N01`
is separately assigned rather than hidden outside the audit. The independently
verified 87-A matrix, presence/targetability classifier, absence reducer, and
fingerprint mechanics remain unchanged. The later structural Q4 correction
withdraws Revision 9's whole-state byte-identity remedy and the earlier
automatic different-owner/`COMPLETE_GONE` quarantine-retirement carve-out. It
replaces entry-point enumeration with a childless fresh-witness construction
boundary. Revision 11 closed permit issuance, transitive private sealing, #115
owner-private observation reduction, and the #146 closed-dispatch contract while
explicitly recording that the runtime seal is not delivered. Revision 12 closes
call consumption as a separate boundary: one private atomic use owner per
variant, winning admission/plan-ownership/native-entry/receipt-consumption
transitions, zero-effect alias/replay at each, an explicit ephemeral final-action gate, and a
begin-bound prospective ordinary capture ID that commit cannot restamp. Revision
13 moves configured/ephemeral ownership to one #115-minted logical-action token,
proves #120's three-case exact-target behavior, assigns configured launch
singleton safety to #57, closes native-entry no-effect/poison direction, and
specifies one attended maximum-sequence rollover. Round 6 showed that target-
local safety was not authority to recreate custody after configured owner loss.
Revision 14 therefore made owner-lost `RESERVED/PRE_BARRIER` an automatic HOLD,
names `ConfiguredPreBarrierRetrySuccessorV1` for any future checked retry, and
specified checked attended disposal as the only present escape: an independent
operator acknowledges prior effect may be unknown and abandons the dead action
without kill or launch. Disposal installs a singular source-bound fence that
globally blocks configured/childless action. After kill-switch removal and one-
current-supervisor observation-only restart, only the winning committed capture
may publish the fence witness and only its matching single-consumer
`ConfiguredPriorEffectFenceBarrierReceiptCustodyV1(CLEAR)` may remove it without
effect. Revision 14 also made an ephemeral receipt racing rollover return the
closed `STALE_CLASSIFIER_EPOCH_RETRY_REQUIRED` result without claiming native
no-effect. Revision 15 narrows the attended escape: only a fresh independent
definitive PID-absent `GONE` result admits disposition. A present PID, including
suspected reuse from generic start-token mismatch, remains
`LIVE_OR_UNPROVEN`; disposal is
`CAPABILITY_UNAVAILABLE(ExactIssuerIdentityAdapterV1)` until that separately
reviewed adapter is delivered. It also reserves exact-identity language for
named identity types/adapters and uses source-equal, byte-identical, matching,
and closed for value-copy, serialization, predicates, and algebras. The audit
records all four permanent V1 capability limitations together: POSIX named
teardown, automatic-quarantine retirement, declared same-platform
transfer/restore/rollback/migration activation refusal with its out-of-band-copy
residual, and present-issuer-PID/reuse disposal unavailability, alongside the
configured owner-loss HOLD/remedy and #57 duplicate-wrapper residual. Neither withdrawal reopens the
original 59-ID split disposition. This audit is complete; Q4 is specified but
its implementation is blocked on #115, #146, #57, and
`ExactIssuerIdentityAdapterV1`. Overall 87-A delivery/conformance
remains blocked on those prerequisites and the merged-#120 adapter and closure
successor; it remains incomplete, nonconforming, unsealed, unenforced, and
activation-prohibited until the named dependencies land and are reviewed.
