# Design 87-A: delta-panel disposition

**Status:** Revision 14 author fold against the panel over `f42570d..44b3787`,
the scoped Q4/Q8/Q9 correction over `49318ff`, the structural Q4 corrections
after the re-panels over `5fe41e0`, and the four-boundary-hole result over
`d67cef6`, the call-consumption result over `776578b`, and the action-custody,
abrupt-death, native-entry, and capture-exhaustion result over `95aed68`, plus
the configured-checkpoint replay and ephemeral-rollover result over `59d8938`.
Q4 is **SPECIFIED; IMPLEMENTATION BLOCKED**. Q9 remains approved; Q4 and 87-A
are not complete, conforming, sealed, or enforced in merged code. Task #146,
task #115, task #57, and `ConfiguredPreBarrierRetrySuccessorV1` are Q4's exact
implementation blockers. Overall 87-A additionally requires the closure
successor and general merged-#120 adapter.

**Mode:** Reference.

**Audience:** 87-A Tier-3 reviewers and implementers of tasks #57, #78, #115,
#120, and #146, plus `ConfiguredPreBarrierRetrySuccessorV1` and the closure
successor.

**Purpose:** Account for every blocker, major, and minor in the delta-panel
result. The normative specification remains the atomic set of the
[core](DESIGN-87A-supervisor-classifier-authority.md) and
[owned-childless module](DESIGN-87A-owned-childless-wrapper-authority.md).
This register is audit evidence, not a third normative specification.

**Merged-evidence correction (2026-08-01):** task #120 shipped on master as
`587e7c1`. It supplies the strict 64-entry `owned_process_tree_v2` snapshot,
exact Windows FILETIME target identity, same-handle identity-check/termination
for an openable exact-FILETIME Windows target, a bounded wait attempt after
successful termination, a recycle-aware deny-only launch barrier, exact attended-reset
evidence, and a request-bound attended archive.
Those Windows mechanisms strengthen target identity and post-effect
observation. They
do not freeze child creation,
own an attempt-bound continuation, or implement the module's
acquire/reconcile/release closure contract. The explicit closure/effect
successor therefore remains absent: every closure-dependent named teardown is
still `CAPABILITY_UNAVAILABLE` and `POLICY_HELD` pending a human. No path held
for missing action-scoped closure becomes available merely because #120 merged.
Once #146, a future conforming closure, and #115 have otherwise authorized an attempt,
a successful termination that signals within the remaining wait budget, plus
the replacement-side recycle split, may avoid a false post-action HOLD when no
independent barrier reason applies. The split excludes only the retired-parent
ownership edge; a same-agent wrapper/wait classification still blocks. That
downstream outcome change is not capability availability.
Merged #120 accepts Linux `linux:<boot_id>:<start_ticks>` as observation input
but declares no macOS exact-token mapping, and its current supervisor owned-tree
body acts only through the Windows FILETIME branch and skips a new
`owned_process_tree` target without `start_filetime`
(`src/agenttalk/supervisor.py:8900-8932` at `587e7c1`). Revision 9 therefore
removes the invalid POSIX target projection and returns pre-reservation
`CAPABILITY_UNAVAILABLE(EXACT_TARGET_EXECUTOR_UNAVAILABLE)` without an owner or
target tuple, authority, reservation, attempt, debt, or external call. It does
not weaken the Windows FILETIME guard or dependency-track an imaginary adapter.
Revision 10 withdrew Revision 9's attempt to prove inherited-state safety by
enumerating entry points and promising whole-state byte identity. Persisted
effect evidence now carries only an inert exact-executor binding. A fresh,
non-serializable witness matching that binding is required to construct the
permit consumed by every effect-bearing target, reservation, call, receipt, or
authority/effect mutation. Deserialization can recover evidence but cannot
manufacture the object needed to act. Owner-private ordinary observation
mutation and initial fail-closed quarantine creation remain explicit non-effect
mutations.

The re-panel over `d67cef6` proved that Revision 10's intended construction
boundary was not closed. Merged `Stop-Tree($targets)` still accepts a raw array
and has two direct planner callers; one guard acquisition could issue two
independent permits; nested typed values remained mutable; and callers could
forge a plain observation-delta record carrying confirmation successors. Revision 11
therefore specifies four structural corrections: #146's closed supervisor
owned-tree dispatcher over three independently authorized opaque variants; one
atomic lineage/custody token per guard acquisition with total return-or-poison
exception handling; transitively immutable alias-free private effect graphs;
and #115 owner-private observation reduction from a sealed one-use receipt.
These are normative contracts, not merged implementation claims.

The Q4 round-4 re-panel over `776578b` held the raw-entry, deep-sealing, and
owner-private observation corrections, but found that Revision 11 closed permit
issuance without closing call consumption. Two aliases of one legitimate call
could both pass an immutable seal while its custody remained `CALL`; the two
non-childless variants had no use owner at all. Revision 12 adds one private
atomic use owner to every dispatcher variant and permits native-plan
construction only after the one winning `CALL -> DISPATCHING` admission and
`DISPATCHING -> PLAN_OWNED` transition; native entry separately requires the
one `PLAN_OWNED -> INVOKING` winner, and receipt consumption requires the
opaque receipt-custody handle over that same owner. A
concurrent alias or sequential replay can retain or alias the call but
cannot manufacture the winning native-entry invocation needed to act. It also makes the ephemeral
final action latch explicit, adds exact `DISPATCHER_SEAL_UNDELIVERED` direction
controls, and binds the prospective ordinary capture ID at #115 begin without
commit-time restamping.

The Q4 round-5 re-panel over `95aed68` held those per-object alias/CAS seams but
found that the two non-childless constructors could each mint multiple owners
from one replayable logical action; transient owner death could not cover a
durable replay checkpoint; native-entry exceptions lacked an exact positive-
no-effect oracle; and maximum ordinary capture sequence had no implementable
attended transition. Revision 13 therefore (1) makes #115 mint one action owner
in `READY` at the checked configured or ephemeral transition and makes the call
constructor consume `READY -> CALL`; (2) proves exact-identity restart retry for
the #120 kill subphase while assigning durable configured-launch singleton
safety to #57; (3) adds invocation-bound `NATIVE_ENTRY_FAILED_NO_EFFECT` plus
exact return-or-poison dispositions; and (4) adds typed exhaustion attention and
one quiescent checked epoch rollover that Revision 13 described as refusing
every effect-bearing state. Round 6 narrowed that prior universal to persisted
non-childless execution and childless envelopes; Revision 14 specifies the
transient ephemeral race separately.
The first contract remains blocked on #115, the dispatcher on #146, and the
configured launch residual on #57. The document does not call those seams
delivered.

The Q4 round-6 scoped review over `59d8938` held all five Revision 13 exit
semantics, including the merged-#120 exact-FILETIME trace, but found that two of
them did not compose. A crash can destroy the transient configured action owner
while leaving `NON_CHILDLESS/RESERVED/PRE_BARRIER` durable; the closed
transition set neither permits a new reservation nor mints retry custody from
persisted provenance. Revision 14 therefore does not convert #120 target-local
idempotence into authority. It requires automatic reload to derive
`ConfiguredPreBarrierOwnerLossHoldV1` and
`POLICY_HELD(CONFIGURED_PRE_BARRIER_OWNER_LOST)` with zero remint,
release-and-reserve, kill, or launch until
`ConfiguredPreBarrierRetrySuccessorV1` supplies a checked retry transition.
Independently, one checked attended disposal is the only specified exit from
that HOLD: the operator first creates or preserves `supervisor.kill`, stops all
project supervisors, and the future
`agenttalk supervise --dispose-configured-pre-barrier`
surface requires an operator other than the dead owner to select and exact-
identity verify the checkpoint, acknowledge that the prior effect is unknown
and no automatic retry is claimed, and abandon only the dead action's ability
to resume. `AttendedConfiguredPreBarrierDispositionResultV1.DISPOSED_PRIOR_EFFECT_UNKNOWN`
never kills or launches and does not assert whether the prior kill occurred;
after it commits, persisted `ConfiguredPriorEffectUnknownFenceV1` records the
request/audit binding, exact source targets, and an epoch-aware freshness floor.
That fence pairs only with top-level `IDLE` and globally blocks every configured
or childless reservation/effect/archive/launch, so a second disposition cannot
replace it and childless origin cannot bypass it. The operator must remove
`supervisor.kill` and start exactly one current supervisor under the still-
global fence before observation is eligible. Only a winning committed ordinary
capture publishes `CommittedOrdinaryFenceCaptureV1`; a same-predecessor loser
publishes none. #115's narrow unexported reducer over that witness's sealed
merged-#120 operands yields the `ConfiguredPriorEffectFenceBarrierReceiptCustodyV1`
whose sole `RECEIPT -> COMMITTING` winner at the exact committed revision may
remove the fence through no-effect `PRIOR_EFFECT_FENCE_CLEAR`. Rollover preserves
its source/effect bytes and rebases only the floor. Normal selection resumes after clearance. The
operator-facing HOLD must name that specified-but-undelivered remedy. #57
separately remains necessary before any configured relaunch can be safe from
duplicate-wrapper replay.

The same review found a conservative epoch race between an attended capture
rollover and an in-flight ephemeral terminal action. Revision 14 intentionally
permits the rollover but makes the old-epoch result exact. A pre-frontier epoch
mismatch visible at the final provenance read before plan ownership is
`VARIANT_PROVENANCE_STALE` plus `REJECTED_NO_EFFECT`. If rollover wins after
that read, including during post-read `DISPATCHING` before plan ownership, no
branch infers no effect; any matching receipt against trustworthy
same-agent official state with a different epoch returns
`EphemeralTerminalReceiptApplyResultV1.STALE_CLASSIFIER_EPOCH_RETRY_REQUIRED`,
consumes and closes the transient owner, retains `next_entry`, performs no
old-epoch receipt-driven mutation, archive, or new effect, and makes no claim
that the native effect did not occur. Receipt-commit-first instead applies and
closes, makes the original rollover request stale, and requires a fresh request;
unknown commit ordering poisons `PLANNER_COMMIT_UNCERTAIN`.

The same correction removes automatic state-loss-quarantine retirement from
V1. Merged #120 supplies no trustworthy host/process-universe token, so local
absence cannot prove extinction of a process tree that may remain live on
another host. Quarantine therefore remains `STATE_PROVENANCE_LOST` and
`POLICY_HELD` pending attended handling. A future automatic-retirement
successor may consume only a read-only producer over an existing OS token; it
may not create a file, registry value, helper, daemon, OS object, persistence
plane, or runtime dependency.

Revision 10 also records a third permanent V1 capability limitation rather than
leaving it as residual scope. Same-platform state-file/workspace transfer,
restore, rollback, and migration activation are unavailable. A conforming
activation path that knows the state came from one of those operations refuses
before active-store admission and constructs no 87-A witness, mutation, effect,
or launch. This is not an active-agent `POLICY_HELD` result. An out-of-band copy
may be indistinguishable from an ordinary in-place restart and may proceed
outside that boundary; it is nonconforming and receives no 87-A guarantee. If
outer-state checks detect rollback-unproven state, only fail-closed quarantine
is admitted. Future 87-C must bind the source universe within M5 Option A or
keep imported state inert.

## Method and status vocabulary

The author checked premise-level findings B1, B3, and M7 against shipped source
and the `44b3787` equations before revising either normative document. Every
row uses exactly one permitted disposition:

- **PRESENT** — folded into the normative design with a mandatory control;
- **DEFERRED** — assigned to named later work;
- **DROPPED** — intentionally rejected with a reason;
- **PREMISE_REFUTED** — not folded because producer evidence disproved it; or
- **WITHDRAWN** — an earlier author remedy or guarantee is explicitly retracted
  and replaced rather than silently rewritten.

Lifecycle status is orthogonal to row disposition:

- **NORMATIVE-SPECIFICATION COMPLETE** means a panel accepted a closed,
  implementable contract and its mandatory evidence; it does not mean code
  enforcement exists.
- **IMPLEMENTATION BLOCKED** means named executable prerequisites are absent.
  Revision 14 is blocked on #115, #146, #57,
  `ConfiguredPreBarrierRetrySuccessorV1`, and the closure successor. Q4's
  construction/dispatch/replay subset is specifically blocked on #115, #146,
  #57, and `ConfiguredPreBarrierRetrySuccessorV1`.

No item is unassigned. All eleven panel premises hold and all eleven are
**PRESENT**; there are no deferred, dropped, or premise-refuted rows. The later
Q4-R1 remedy is separately `WITHDRAWN` and replaced below; that withdrawal does
not remove its underlying obligation.

## Producer checks

### B1: child-establishment grace

**Premise holds.** The shipped wrapper publishes `STARTING`, launches the CLI,
and invokes `runtime_writer.active` immediately from the spawn callback
([`wrapper/run.py`](../src/agenttalk/wrapper/run.py): `1502-1523`,
`1983-1987`, `2086-2091`). The shipped supervisor separately treats a
zero-child `ACTIVE` record as starting while either the checked generation
launch fence is open or runtime age is at most 30 seconds, and only after
handoff or both grace predicates close advances the two-poll child-dead reducer
([`supervisor.py`](../src/agenttalk/supervisor.py): `73`, `389-390`,
`4213-4223`, `4553-4569`, `4631-4658`). The `44b3787` module had adjacency but
no equivalent establishment predicate.

### B3: nonce operands

**Premise holds.** `wrapper_runtime.py` has a closed `RECORD_KEYS` set with no
nonce field and rejects unknown/missing keys
([`wrapper_runtime.py`](../src/agenttalk/wrapper_runtime.py): `64-80`,
`165-176`). The actual independent nonce sources are the checked managed
launch nonce and the strict parser result from the observed root command line
([`supervisor.py`](../src/agenttalk/supervisor.py): `2185-2218`,
`2334-2368`). The runtime record still independently joins
agent/PID/start/generation; it is not a third nonce source.

### M7: manual confirmed-absence suppression

**Premise holds.** At `44b3787`,
`childless_teardown_required = child_death_sourced_dominant or debt` preceded
the confirmed-absence arm. With a dead wrapper, debt `NONE`, and retained
child-death evidence, the named `INITIAL` case could not exist because it
requires `PRESENT_TARGETABLE`; the next arm therefore selected `SAFETY_HELD`
before reaching `RELAUNCH_ONLY`. The banked automatic
`CURRENT_TEARDOWN_PROOF × ABSENT × STALE` cell remained `RELAUNCH_ONLY`, so the
manual path was indeed weaker.

## Disposition register

| ID | Panel obligation | Disposition and exact normative location |
| --- | --- | --- |
| B1 | Prevent two pre-handoff absences from authorizing teardown. | **PRESENT.** The core's [classifier continuity state](DESIGN-87A-supervisor-classifier-authority.md#classifier-continuity-state) defines `ChildEstablishmentGuardV1` and its same-turn, nonrenewable constructor; [explicit active-child input](DESIGN-87A-supervisor-classifier-authority.md#explicit-active-child-input) maps complete pre-close zero-child evidence to `UNKNOWN(CHILD_ESTABLISHMENT_OPEN)` and preserves both shipped no-handoff predicates: inclusive active-record age through 30 seconds and the exclusive applicable generation launch fence. The module's [safety decision](DESIGN-87A-owned-childless-wrapper-authority.md#safety-decision), [two-capture reducer](DESIGN-87A-owned-childless-wrapper-authority.md#two-independent-complete-child-absence-captures), [named constructor](DESIGN-87A-owned-childless-wrapper-authority.md#named-authority-constructor), and conformance item 14 carry the complete closed guard through confirmation, reservation, and action equality. |
| B2 | Linearize external calls armed by checked state and close both two-poller gaps. | **PRESENT AS A NORMATIVE CONTRACT; IMPLEMENTATION BLOCKED.** `ChildlessContinuationOwnerV1`, provider-version equality, and terminal retired-attempt IDs are defined in the module's closure-successor contract. [Action-time closure and closed dispatch](DESIGN-87A-owned-childless-wrapper-authority.md#action-time-closure-and-closed-supervisor-owned-tree-dispatch) requires one unique guard lineage per acquisition, one #115-minted action custody per non-childless checked transition, separate winning constructor/admission/plan-ownership/native-entry transitions, atomically admitted receipt consumption, invocation-bound no-effect results, total return-or-poison handling, `ARMED`/`CALL_RETURNED`, predecessor-death proof, and release-only late `HELD`. The closed dispatcher prevents raw entry plus call, admission, invocation, and receipt replay after #146. Exact transitions forbid reconciliation while a foreign continuation can resume and keep `STOP_TREE/ARMED` from proving or reissuing the effect. Merged #120 contributes target-local same-handle FILETIME execution/wait but no authority to remint configured retry custody from a durable checkpoint. Owner loss at configured `RESERVED/PRE_BARRIER` automatically holds until `ConfiguredPreBarrierRetrySuccessorV1`; the only separately specified exit is exact checked attended disposal with no kill or launch. Full effect linearization remains blocked on #115, #146, #57 for configured relaunch, `ConfiguredPreBarrierRetrySuccessorV1`, and the closure successor. |
| B3 | Remove the undefined strict-runtime nonce operand and retain real provenance. | **PRESENT.** `OwnedLaunchNonceProvenanceV1`, `OwnedWrapperIdentityV1`, the [#120 snapshot adapter](DESIGN-87A-owned-childless-wrapper-authority.md#published-120-snapshot-and-closure-successor-contracts), and the corrected positive join retain the actual sources. Nonce equality is checked-managed versus parsed-observed-root; runtime remains an independent agent/PID/start/generation source. Conformance items 15 and 23 independently remove/mismatch every actual source and verify the strict 64-entry v2 mapping, including mandatory exact Windows FILETIME identity for complete/absent records. |
| B4 | Prevent state loss from resetting the hard cap or erasing teardown debt. | **PRESENT, WITH A V1 CAPABILITY REDUCTION.** [Fail-closed state-loss quarantine](DESIGN-87A-owned-childless-wrapper-authority.md#fail-closed-state-loss-quarantine) denies all teardown and launch after missing, corrupt, torn, or rollback-unproven checked state. Revision 10 removes the prior automatic different-owner/extinction retirement: merged #120 supplies no trustworthy host/process-universe token, so V1 cannot prove that a locally absent prior owner is extinct everywhere. Automatic quarantine retirement is therefore unavailable on every platform and the state remains `STATE_PROVENANCE_LOST`/`POLICY_HELD` pending attended handling. This prevents state loss after attempts 1/2/3 or partially acted debt from recreating a budget or laundering debt. PID and start, hostname, state_epoch, process_source_digest, MachineGuid alone, local absence are explicitly insufficient. |
| M5 | Decide whether the unqualified no-daemon/no-persistence-plane/no-runtime-dependency invariant remains final or receives a mechanism exception. | **PRESENT — RESOLVED by operator on 2026-07-31 (Option A).** The invariant is absolute: no daemon, new persistence plane, durable helper or OS object, runtime dependency, or mechanism-specific/separately versioned exception. The module uses existing checked state plus transient caller-owned synchronization. Without a fresh matching witness, no 87-A childless executor effect/mutation is constructible; independently, after #146 no supervisor owned-tree native termination is reachable except through one independently authorized closed dispatcher variant. The availability cost remains three permanent V1 limitations: POSIX closure-dependent named teardown `CAPABILITY_UNAVAILABLE`/`POLICY_HELD`; automatic quarantine retirement unavailable; and declared same-platform transfer/restore/rollback/migration activation refusal. The same operator surface separately names #57's configured-relaunch duplicate-wrapper residual and the configured owner-loss HOLD pending `ConfiguredPreBarrierRetrySuccessorV1`, together with the exact attended remedy. #120 target-local identity is not retry authority, and 87-A does not invent a second durability mechanism. A future retirement or transfer producer may read only an existing OS process-universe token and may not add a file, registry value, helper, daemon, OS object, persistence plane, or dependency. An unprovable implementation case creates a task, not a mechanism. |
| M6 | Eliminate the unproduced valid-guard `SPAWN_IN_FLIGHT` shape. | **PRESENT.** The core's [classifier continuity state](DESIGN-87A-supervisor-classifier-authority.md#classifier-continuity-state) requires `SPAWN_IN_FLIGHT.spawned_guard == null`; a valid result commits identity directly and ambiguity enters `AMBIGUOUS_LAUNCH`. Core conformance item 34 and module item 18 cover reload. |
| M7 | Preserve manual no-kill relaunch for a confirmed absent wrapper. | **PRESENT.** The core's [manual marker disposition and overlap](DESIGN-87A-supervisor-classifier-authority.md#manual-marker-disposition-and-overlap) evaluates outstanding debt first, then confirmed whole-wrapper absence, then the live-wrapper named kill gate. The module's [core combiner overlay](DESIGN-87A-owned-childless-wrapper-authority.md#core-combiner-overlay) states manual/automatic no-kill parity; conformance item 19 covers absence, debt, and present-wrapper cases. |
| M8 | Bank deterministic vectors for all seven new digest domains. | **PRESENT.** The module's [chained digest conformance vector](DESIGN-87A-owned-childless-wrapper-authority.md#chained-digest-conformance-vector) fixes exact payload bytes, byte counts, domains, and expected SHA-256 for `owner_identity_id`, `OwnedTargetDigestV1`, `process_source_digest`, owned-childless basis, `basis_id`, `authority_id`, and `debt_id`. Revision 10 retains the verified Windows `win-tree/v2` fixture unchanged. Conformance item 20 independently constructs typed objects, production-encodes them, requires exact byte/count equality, then hashes each produced payload while carrying forward the digest it just recomputed. Displayed digests and byte-flip propagation are interoperability/change-detection anchors, not independent schema/codec correctness proof. |
| m9 | Correct the stale single-condition reservation restatement. | **PRESENT.** The core's [manual marker disposition and overlap](DESIGN-87A-supervisor-classifier-authority.md#manual-marker-disposition-and-overlap) now states both required conjuncts: execution is `IDLE` and the same-poll terminal sequence differs from the ordinary poll sequence. |
| m10 | Qualify the two leading safety labels by their implementation dependencies. | **PRESENT.** The module's safety decision records merged #120 as a delivered ownership-input/target-local-effect dependency, not configured retry authority. Actual teardown/relaunch remains implementation-blocked on #115, #146, #57 where configured launch is involved, `ConfiguredPreBarrierRetrySuccessorV1`, and the closure successor. Both normative files state specification, delivery, conformance, and activation status separately. |
| m11 | Define deterministic ordinals for reload residual captures. | **PRESENT BY REMOVING THE UNTYPED PATH.** Revision 10 makes matching `RELEASED` after retained `STOP_TREE/CALL_RETURNED` finalize conservatively as `EFFECT_UNPROVEN`, retain debt, and perform no external residual capture. Residual discovery occurs only on the next ordinary ordinal-zero poll. The core's [capture identity and coverage equality](DESIGN-87A-supervisor-classifier-authority.md#capture-identity-and-coverage-equality) still defines one CAS allocator for every actual nonordinary post-action/reconciliation capture; conformance item 21 races those allocators and proves the ordinary/nonordinary context split. |

## Post-`49318ff` scoped panel corrections

The panel held Q1, Q2, and Q3 under attack; Revision 14 does not reopen those
arguments or the operator-decided M5 constraint. Q9 was approved after focused
re-panel and remains frozen below. The current re-panel scope is Q4 only. The
two Q8 residual obligations remain follow-up boundaries, not new authority
logic.

| ID | Panel result | Revision 14 disposition |
| --- | --- | --- |
| Q4 | The design projected a valid Linux exact token to `Stop-Tree`, but merged #120 only accepts that token as input and skips the corresponding target at execution. | **SPECIFIED; IMPLEMENTATION BLOCKED ON #115, #146, #57, AND `ConfiguredPreBarrierRetrySuccessorV1`.** The module deletes the non-Windows destructive mapping/type/join/projection and adds `EXACT_TARGET_EXECUTOR_UNAVAILABLE` to the static capability reasons. A valid Linux-token snapshot remains observation/barrier input; macOS has no admitted exact-token mapping. Without an admitted executor witness, no named POSIX childless effect graph is constructible. The mandatory control cites the actual Windows execution branch and non-Windows skip at `src/agenttalk/supervisor.py:8900-8932` rather than the input-acceptance lines. The Windows exact-FILETIME requirement remains absolute. #115 must mint action-scoped custody and own the narrow post-commit fence witness/barrier reducer, #146 must seal/migrate the dispatcher, #57 must close configured-launch replay, and `ConfiguredPreBarrierRetrySuccessorV1` must supply any future automatic configured retry transition. Until then, owner loss at `RESERVED/PRE_BARRIER` holds automatically and only the specified checked attended disposal can clear that checkpoint without kill or launch. This is a specification disposition, not a claim that merged code is conforming or enforced. |
| Q4-R1 | The fresh refusal did not cover a Windows-created closure-bearing state resumed, reloaded, or inherited on a non-Windows host; reconcile/release or finalization could call externally or mutate debt there. | **WITHDRAWN AND REPLACED.** Revision 9 tried to close this finding by enumerating 17 entry points and claimed that executor unavailability preserved every checked field byte-identically. Revision 10 expressly withdrew that whole-state claim and the enumeration as its proof; Revision 11 retains the withdrawal. #115 owner-private ordinary observation commits and initial `StateLossQuarantineV1.UNRESOLVED` creation may legitimately mutate observation or fail-closed state. The replacement universal is narrower and structural: without a fresh non-serializable witness matching the persisted exact-executor binding, no executor-dependent childless external effect and no childless authority-enabling or effect-owned mutation is constructible. |
| Q4-R2 | Childless-origin `SPAWN_IN_FLIGHT` followed by `AMBIGUOUS_LAUNCH` was an eighteenth persisted entry point omitted by the Revision 9 inventory. | **WITHDRAWN AND REPLACED AGAIN AFTER THE REVISION 10 BOUNDARY PROVED INCOMPLETE.** Revision 10 correctly made persisted state inert but failed to seal the raw supervisor executor, permit-adjacent lineage, nested object graph, and observation successor. Revision 11 retains the evidence-versus-capability split and adds the closed dispatcher, unique atomic custody lineage, transitive private sealing, and #115 owner-private observation reduction specified in Q4-R6 through Q4-R9. Therefore an eighteenth, nineteenth, or future state may deserialize evidence but cannot manufacture the object needed to act once those named implementation prerequisites land. `SPAWN_IN_FLIGHT` and `AMBIGUOUS_LAUNCH` need no path-specific safety exception. |
| Q4-R3 | Malformed-state precedence conflicted with executor-unavailable precedence, while ordinary observation legitimately changed poll, ordinal, and terminal fields before the unavailable decision. | **PRESENT — CLAIM NARROWED AND MUTATIONS PARTITIONED.** The checked owner distinguishes #115 owner-private observation, private non-childless authority, initial fail-closed quarantine creation, attended maximum-sequence rollover, attended configured-checkpoint disposal, and permit-bound childless-effect mutations. Observation and non-childless authority cannot address a current childless envelope; quarantine creates a less-authoritative genesis only from untrusted outer state; configured disposal can remove only the exact owner-lost `RESERVED/PRE_BARRIER` checkpoint and never kills, launches, or certifies prior effect; childless effect mutation is rejected without the fresh matching permit. Rollover is admitted only from trustworthy persisted top-level `IDLE` with no childless envelope or non-childless execution. A concurrent transient ephemeral action is handled by the exact old-epoch receipt outcome in Q4-R19 rather than by claiming every transient effect is serialized. Ordinary observation may advance its documented projection, so the design no longer promises whole-state byte identity. Malformed-versus-unavailable output precedence remains deterministic but is not relied on as the safety boundary. |
| Q4-R4 | The complete-extinction quarantine carve-out was not bound to a host/process universe, so local absence could clear a transferred quarantine while the prior tree remained live elsewhere. | **PRESENT — V1 AUTOMATIC RETIREMENT REMOVED.** Merged #120 supplies no trustworthy host/process-universe token. V1 therefore has no automatic quarantine-retirement constructor on any platform; a transferred or local quarantine remains `STATE_PROVENANCE_LOST`/`POLICY_HELD` pending attended handling. PID and start, hostname, state_epoch, process_source_digest, MachineGuid alone, local absence are insufficient. A future successor must use a read-only producer over an existing OS token and must preserve the absolute prohibition on a new file, registry value, helper, daemon, OS object, persistence plane, or dependency. |
| Q4-R5 | Initial state-loss quarantine creation was omitted from the zero-mutation entry inventory. | **PRESENT — EXPLICIT FAIL-CLOSED MUTATION.** Missing, corrupt, torn, or rollback-unproven expected state may create a new epoch only together with `StateLossQuarantineV1.UNRESOLVED`. That mutation creates no authority, effect object, attempt budget, debt clear, identity commit, kill, or launch. It is deliberately outside both the withdrawn byte-identity claim and the permit-bound effect-mutation class. |
| Q4-R6 | Merged `Stop-Tree($targets)` and its two planner callers bypassed the supposedly permit-only termination boundary; preserving that raw entry contradicted the typed-only claim. | **PRESENT AS A NORMATIVE CONTRACT; IMPLEMENTATION BLOCKED ON #146.** Revision 11 preserves non-87-A behavior rather than the raw signature. It specifies one closed supervisor owned-tree dispatcher over opaque CHILDLESS, CONFIGURED_AGENT_RELAUNCH, and EPHEMERAL_TERMINAL variants; the raw target array exists only inside the private native body. A caller-settable tag or wrapper around `kill_targets` is not authorization. #146 must migrate both current raw callers, bind each non-childless variant to its independent checked provenance, and prove no direct target call remains. The sole-body claim is scoped to the supervisor owned-tree executor and excludes the turn watchdog's separate facility. Until #146 lands and is reviewed, Q4 and 87-A are not complete, conforming, sealed, or enforced in merged code. |
| Q4-R7 | One guard acquisition could mint two independent borrow/permit pairs before either was consumed, allowing two single-use typed calls. | **PRESENT AS A NORMATIVE CONTRACT; IMPLEMENTATION BLOCKED.** One acquisition creates exactly one private lineage token and one custody state. Issuance atomically moves `AVAILABLE` to `OUTSTANDING`; a second issuance from the acquisition is unconstructible until the same custody is returned. This closes issuance, not call consumption: Revision 12 separately requires the exact call's private owner to win `CALL -> DISPATCHING` before any plan can exist. A synchronous adapter exception returns exactly one live custody only with positive no-effect proof or otherwise poisons it exactly once, never both and never neither. Success, terminal failure, replay, and exception controls cover the complete state machine. |
| Q4-R8 | A legitimate typed call remained shallowly mutable; a caller could change a nested PID after construction and the consumer observed the changed value. | **PRESENT AS A NORMATIVE CONTRACT; IMPLEMENTATION BLOCKED.** Every effect-bearing graph and nested target is privately constructed, transitively immutable, alias-free, and integrity-bound before dispatch. The consumer revalidates the private seal. Mandatory negative controls use a controlled unsafe hook to change a legitimate sealed call's nested PID from 101 to 202 and require consumer rejection; separately, mutation of a retained source alias must leave the sealed call unchanged. They also cover mutable-container substitution. Merely declaring a top-level type frozen is insufficient. |
| Q4-R9 | A caller-constructible classifier observation delta could forge confirmations and reduce the two-observation interlock to one. | **PRESENT AS A NORMATIVE CONTRACT; IMPLEMENTATION BLOCKED ON #115.** Revision 11 removes caller construction of confirmation-bearing deltas. The existing #115 owner begins the observation and passes its private lineage only to the installed observer adapter; that adapter's unexported factory seals one real acquisition into the receipt. The owner validates the already begin-bound ordinal-zero identity, then privately derives adjacency, distinct-capture, and confirmation successors inside the same checked RMW transaction and commits once without restamping. External callers cannot construct a receipt, mutation, or successor field. This cross-references #115 rather than creating a second owner. |
| Q4-R10 | Two aliases of one legitimate sealed call could dispatch concurrently because immutable/noncopyable calls had no atomic consumption admission; configured-agent and ephemeral calls could also replay sequentially because they had no lineage owner. | **PRESENT AS A NORMATIVE CONTRACT; IMPLEMENTATION BLOCKED ON #115 AND #146.** Every closed dispatch variant has exactly one private atomic use owner outside its deeply immutable call graph and one opaque submission associating that call with the owner. CHILDLESS reuses its guard lineage; each non-childless call consumes the action owner that #115 minted at its checked logical-action transition, as strengthened by Q4-R14. Submission aliases race `CALL -> DISPATCHING`; admission aliases race `DISPATCHING -> PLAN_OWNED`; invocation aliases race `PLAN_OWNED -> INVOKING`; receipt aliases race before any receipt-driven behavior. Concurrent aliases and sequential replay at every boundary produce zero additional plan, mutation, launch, or effect. Normal receipt, bound no-effect result, uncertain native/receipt/planner outcome, receipt consumption, poison, and cleanup all have exact owner dispositions. A registry, caller mutex, call-ID set, or unstated dispatcher lock is not a construction proof. |
| Q4-R11 | The ephemeral terminal variant carried request/action provenance and `next_entry` but did not explicitly repeat its final action-latch gate at dispatch. | **PRESENT AS A NORMATIVE CONTRACT; IMPLEMENTATION BLOCKED ON #146.** Its constructor captures a narrow final-action gate: dry-run false, kill switch clear, and enabled action-latch epoch. After the winning admission but before plan construction, the dispatcher requires that exact latch epoch under the read guard and separately rereads the kill switch. It retains the latch guard through issuance and preserves the native body's final kill-switch check. Independent post-construction latch and kill-switch races consume the call as `REJECTED_NO_EFFECT`, preserve `next_entry`, and create no plan, raw array, or effect. A third race flips the kill switch after outer validation/plan construction but before the inner check; the body returns typed invocation-bound `ACTIONS_DISABLED_NO_EFFECT` before raw-array materialization, the owner becomes `REJECTED_NO_EFFECT(FINAL_ACTION_GATE_CHANGED)`, and replay remains inert. |
| Q4-R12 | `DISPATCHER_SEAL_UNDELIVERED` had specified semantics but no exact direction control, so an implementation could return another unavailability reason while generic no-effect controls remained green. | **PRESENT.** Mandatory controls stage valid Windows exact-FILETIME semantics with #146 absent and require exactly `DISPATCHER_SEAL_UNDELIVERED` before permit/provider evaluation, including retained-envelope retention and generic public attention. Linux/macOS require `EXACT_TARGET_EXECUTOR_UNAVAILABLE` precedence; a staged sealed Windows dispatcher with only the closure successor absent requires `SUCCESSOR_MISSING`. The controls state explicitly that these are prospective conformance results, not runtime facts at merged `587e7c1`. |
| Q4-R13 | The prospective ordinary `CaptureIdV1` was not explicitly bound into the #115 begin lineage, leaving commit-time restamping or stale-loser acquisition reuse underspecified. | **PRESENT AS A NORMATIVE CONTRACT; IMPLEMENTATION BLOCKED ON #115.** Begin fixes the prospective ordinal-zero capture ID before acquisition and creates one atomic lineage owner. Only one acquisition-handle alias may win `UNUSED -> ACQUIRING`; normal observation atomically yields one opaque commit-custody handle pairing the sealed receipt with that owner, and only one custody alias may win `RECEIPT -> COMMITTING`. Receipt alone is inert. The installed observer, sealed receipt, and private successor mutation preserve the ID byte-for-byte, and commit validates rather than restamps it. Candidate validation compares receipt/raw/tree/mutation sequence to checked predecessor plus one; only the successful commit makes that candidate current. In a same-predecessor race, the losing lineage, receipt, and acquisition are poisoned; the completed acquisition cannot be moved into a successor lineage, resealed, or given the successor ID. Only a new begin and distinct post-reload acquisition may mint the next receipt. Maximum `uint64` returns typed attended exhaustion before acquisition and never wraps; Q4-R17 specifies its attended transition. |
| Q4-R14 | The non-childless owner was single-use per constructed call, not per authorized logical action; invoking a private constructor twice over unchanged barrier/next-entry provenance minted two independent owners. | **PRESENT AS A NORMATIVE CONTRACT; IMPLEMENTATION BLOCKED ON #115.** The configured reservation/barrier transition and exact ephemeral terminal transition each mint exactly one private action owner in `READY` as the atomic side-result of the winning #115 checked commit. The call constructor does not mint an owner: it must win `READY -> CALL` on that pre-existing custody before emitting one call/submission. Persisted provenance, IDs, or digests cannot reconstruct or look up custody. Concurrent and sequential duplicate constructors and duplicate checked-transition mints have exact zero-effect controls. CHILDLESS keeps its stronger one-outstanding-loan guard lineage; the per-variant KEEP/SKIP table makes every family property explicit. |
| Q4-R15 | Abrupt process death or hard cancellation destroyed the transient owner while leaving replayable persisted non-childless checkpoints, contradicting a blanket no-rearm claim. | **PRESENT AS A VARIANT-SPLIT CONTRACT; IMPLEMENTATION BLOCKED ON #57 AND `ConfiguredPreBarrierRetrySuccessorV1`.** The documents no longer claim transient custody survives crash. For a new checked EPHEMERAL_TERMINAL action, merged #120 proves only exact-target behavior: a gone PID is a no-op (`8908-8911`), a recycled PID/different FILETIME is refused (`8912-8913`), and the same PID/FILETIME is the same process terminated through the validated handle (`8912-8921`); the fresh barrier still controls archive. That target-local trace is not authority to recreate configured custody from durable state. If configured custody loss leaves `NON_CHILDLESS/RESERVED/PRE_BARRIER`, automatic reload holds with no remint, `PRE_BARRIER_RELEASE`, release-and-reserve, kill, or launch until `ConfiguredPreBarrierRetrySuccessorV1` supplies a checked retry transition. The hold renders before death is proved and records `LIVE_OR_UNPROVEN` or `PROVED_GONE`; only the proved form admits exact attended disposal. The actor acknowledges prior effect may be unknown, abandons only the dead action's ability to resume, and performs no kill or launch. Its global fence must remain through kill-switch removal and one-current-supervisor observation-only restart; only a winning committed source-bound #120 barrier receipt custody may clear it before any later configured or childless action. Task #57 separately remains the durable project-level per-agent launch-singleton dependency because `Start-Process` replay can duplicate a wrapper. |
| Q4-R16 | An exception after `PLAN_OWNED -> INVOKING` but before raw-array/native effect had no exact typed no-effect result or owner disposition. | **PRESENT — DOCS-CLOSED.** `NATIVE_ENTRY_FAILED_NO_EFFECT` is a private invocation-bound result/reason available only when the wrapper positively proves the native-effect frontier was not reached. Its one consumer returns exactly one CHILDLESS custody or terminally rejects either non-childless owner. At/after/unknown-frontier failure poisons with `ADAPTER_EFFECT_UNCERTAIN`/`NATIVE_EFFECT_UNCERTAIN`; uncertain result construction/handoff/owner resolution poisons with the protocol-specific cause. Direction controls inject each boundary and forbid exception class, elapsed time, missing receipt/result, caller flags, or null raw-array observation as a no-effect oracle. |
| Q4-R17 | Maximum ordinary capture sequence safely refused replay but exposed no typed attended result or checked rollover semantics, especially with active envelope/debt/cycle state. | **PRESENT — DOCS-CLOSED; IMPLEMENTATION BLOCKED ON #115.** Begin returns typed `CaptureSequenceExhaustionV1` with agent, old epoch/revision, exact `READY`/blocker disposition, and attended action. One #115 checked rollover from persisted top-level `IDLE` installs a fresh epoch at revision/sequence zero, resets capture-derived evidence, preserves managed/manual/quarantine state, and invalidates every old-epoch receipt/proof. Any persisted non-childless execution or childless envelope—including debt, cycle, continuation, or retired attempt—blocks byte-identically and cannot reset budget. A non-null prior-effect fence is not erased: all audit/source/effect fields stay byte-identical and only its freshness floor rebases to `(new epoch, 0)`, so an old-epoch barrier fails and only the first winning committed fresh successor capture's matching custody may clear, with complete #120 coverage. A transient in-flight ephemeral action is not summarized as a persisted blocker; its staged provenance and receipt-CAS races have the exact Q4-R19 outcomes. The explicit residual is an indefinite attended hold when exhaustion coincides with an obligation that requires another ordinary capture. |
| Q4-R18 | Revision 13 simultaneously forbade custody minting from replayable provenance and required configured kill retry after owner death, but its closed transition set exposed no legal path from persisted `NON_CHILDLESS/RESERVED/PRE_BARRIER`. | **PRESENT AS OPTION (b) PLUS A CHECKED ATTENDED ESCAPE; IMPLEMENTATION BLOCKED ON #115 AND `ConfiguredPreBarrierRetrySuccessorV1`.** Custody unavailability derives `ConfiguredPreBarrierOwnerLossHoldV1` and `POLICY_HELD(CONFIGURED_PRE_BARRIER_OWNER_LOST)` in both `LIVE_OR_UNPROVEN` and `PROVED_GONE` forms, naming the exact agent, reservation/checkpoint, canonical source hash, missing retry successor, and attended remedy. The hash covers only the exact persisted checkpoint projection, so volatile extinction-observation IDs cannot make it unreproducible; the attended commit rechecks extinction independently. No automatic transition may remint custody, call `PRE_BARRIER_RELEASE`, silently release and reserve again, kill, or launch. Only the proved form admits the future `agenttalk supervise --dispose-configured-pre-barrier` surface and #115's exact checked delta. A liaison or sole lead creates or preserves `supervisor.kill`, stops all project supervisors, selects the current checkpoint/hash, and supplies all three acknowledgements; independent OS evidence, not the operator or dead issuer, proves extinction. `DISPOSED_PRIOR_EFFECT_UNKNOWN` never kills or launches and persists its complete request, exact source targets, and epoch-aware floor in `ConfiguredPriorEffectUnknownFenceV1`. While that singular fence exists, top-level state is `IDLE` but every configured/childless reservation, effect, archive, and launch is globally held; hence no second disposition can replace it. After the operator removes `supervisor.kill` and starts exactly one current supervisor, only a winning committed ordinary capture publishes `CommittedOrdinaryFenceCaptureV1`; a same-predecessor loser publishes none. #115's narrow reducer consumes that witness, and only the committed-successor `ConfiguredPriorEffectFenceBarrierReceiptCustodyV1(CLEAR)` sole `RECEIPT -> COMMITTING` winner may apply no-effect `PRIOR_EFFECT_FENCE_CLEAR`. Deterministic admitted stale, blocked, ambiguous, unavailable, and wrong-source outcomes preserve the fence with their closed custody dispositions; replay losers read no state and claim neither preservation nor clearance. A state-CAS outcome-unknown path likewise claims neither and must reconcile the checked store as fence-current, fence-cleared, or untrustworthy. Mandatory controls cover hash preimage, response-loss lookup while current, kill-switch-ineligible then observation-only re-enable, all bypass origins, winning-commit/custody admission, rollover rebase, and stale/live/circular/replay refusal. Merged #120 remains target-local identity evidence, not retry authorization. The command and checked delta are specified, not delivered. |
| Q4-R19 | An attended capture rollover could change the classifier epoch while an ephemeral terminal native invocation remained in flight. | **PRESENT — EXACT STALE-EPOCH OUTCOME; IMPLEMENTATION BLOCKED ON #115 AND #146.** Rollover visible at the final provenance read before plan ownership maps to `VARIANT_PROVENANCE_STALE` plus `REJECTED_NO_EFFECT`. If rollover wins after that read—including during post-read `DISPATCHING` before plan ownership—invocation may resolve and no branch infers no effect. At receipt, trustworthy schema-valid same-agent official state with a different epoch admits the no-write stale result without requiring an unstored UUID lineage. Rollover-CAS-first yields exactly one `STALE_CLASSIFIER_EPOCH_RETRY_REQUIRED`, `CONSUMING_RECEIPT -> CLOSED`, retained `next_entry`, and zero state/archive/launch/new effect. Receipt-commit-first yields `APPLIED/CLOSED`, makes the original rollover request stale, and requires a fresh exhaustion result/request. Unknown commit poisons `PLANNER_COMMIT_UNCERTAIN`; missing/untrusted/wrong-agent state cannot select the stale result. No branch claims the native effect absent. |
| Q8-R1 | A bare `CAPABILITY_UNAVAILABLE` code does not tell an operator whom to act on. | **PRESENT.** The core/module 87-B boundary requires an exact-fingerprint join, the held agent from `canonical_condition.agent_key`, and actionable text. Bounded redacted `ConfiguredPreBarrierOwnerLossSummaryV1` names the exact reservation/checkpoint/source hash, proved-or-unproven extinction, missing retry successor, prior-effect-unknown posture, and specified-but-undelivered disposition remedy. Bounded redacted `ConfiguredPriorEffectUnknownFenceSummaryV1` separately exports the source target count/digest and freshness floor plus the exact remove-kill-switch/start-one-current-supervisor/committed-fresh-barrier/attended-target clearance remedy. The complete internal hold/fence, issuer PID/start/token, full targets, actor, acknowledgements, and free-text reason never cross 87-B. Bare-code, agentless, checkpointless, remedy-free, or leaking rendering is nonconforming. |
| Q8-R2 | Operators need advance documentation that fail-closed capability refusal can strand recovery indefinitely. | **PRESENT, WITH ALL THREE PERMANENT V1 LIMITATIONS AND BOTH CONFIGURED RESIDUALS NAMED TOGETHER.** 87-A implementation close and 87-C activation require reviewed 87-B/follow-up operator-manual and tutorial evidence that: (1) every Linux/macOS closure-dependent named teardown remains `CAPABILITY_UNAVAILABLE`/`POLICY_HELD` pending a human until a real exact-token adapter exists; (2) automatic state-loss-quarantine retirement is unavailable on every platform in V1, so provenance loss remains `STATE_PROVENANCE_LOST`/`POLICY_HELD` pending attended handling; (3) declared same-platform state-file/workspace transfer, restore, rollback, or migration activation refuses before active-store admission; (4) configured relaunch cannot activate before #57 because replay can duplicate a wrapper; and (5) unavailable configured custody holds until `ConfiguredPreBarrierRetrySuccessorV1`, with `agenttalk supervise --dispose-configured-pre-barrier` as the only specified escape before that successor lands. Held-agent projections apply to items 1, 2, and 5; item 3 identifies the rejected operation/store; item 4 identifies the missing singleton and duplicate-wrapper consequence; item 5 identifies the checkpoint/source hash, proved-or-unproven issuer status, prior-effect unknown, the three required acknowledgement kinds, and specified-but-undelivered disposition. Its first step is to create or preserve `supervisor.kill` and stop every project supervisor before refreshing the current hold; stopping the wrapper alone is not a remedy. After disposal it projects the global fence and tells the operator to remove `supervisor.kill`, start exactly one current supervisor under the fence, obtain a winning committed source-bound barrier, and handle a surviving exact source target before retrying clearance. Merged #120 target-local identity does not authorize automatic retry. |
| Q8-R3 | Same-platform state transfer, restore, rollback, and migration were recorded only as residual 87-C scope, leaving an operator unable to tell whether V1 refuses or proceeds on unvouched state. | **PRESENT — THIRD CAPABILITY LIMITATION MADE EXPLICIT.** A conforming activation path that is told, or otherwise knows, state came from one of those operations refuses before active-store admission and constructs no witness, permit, authority/effect mutation, external call, or launch. Because V1 persists no trustworthy source-host/process-universe token, an out-of-band overwrite may be indistinguishable from same-store reload and may proceed outside that boundary; the bypass is nonconforming and receives no 87-A safety or recovery guarantee. If existing outer-state checks detect rollback-unproven state, only fail-closed quarantine is admitted. 87-C must bind the source universe within M5 Option A or keep imported state inert. |
| Q9 | The displayed expected digest moved with the artifact and was circular as correctness assurance. | **PRESENT.** The fixture remains byte-for-byte verified, but is labeled an interoperability/change-detection anchor. Conformance item 20 constructs each typed object through its independent typed constructor from scalar fixture values shown in the displayed JSON; parsing may extract scalars but may not define the expected typed object or field set. It then encodes through production `CanonicalJsonV1`, byte-compares before hashing, and carries forward only the digest it just recomputed. The byte-flip chain is explicitly secondary change-detection evidence. |

## Frozen evidence boundary

This fold does not alter the 48-row/96-cell dominant matrix, either banked
`RecoveryConditionFingerprintV1` payload/vector, or their counts. The
independently verified size figures remain the frozen `44b3787` delta-panel
input; the core labels them as historical evidence rather than recomputing
them. The new seven-domain module vector is additive conformance evidence for
M8 and does not rewrite either banked core vector.

## Conclusion

All four original blockers, all four majors, and all three minors are
**PRESENT** at named normative locations with mandatory controls. M5 remains
the absolute Option A constraint. Q9 is approved and its seven-domain evidence
is unchanged. Revision 10 withdrew the Revision 9 whole-state guarantee,
removed unsafe automatic quarantine retirement from V1, and stated all three
permanent capability limitations together. Revision 11 replaced the incomplete
construction boundary with a closed dispatcher contract, one atomic issuance
lineage, transitive private sealing, and #115 owner-private observation
reduction. Revision 12 closes the remaining call-consumption boundary with one
private atomic use owner and winning admission, plan-ownership, native-entry,
and receipt-consumption transitions per call, repeats
the ephemeral final-action gates, fixes exact seal-unavailability controls, and
binds the prospective ordinary capture ID at begin. Revision 13 moves
non-childless ownership to one #115-minted logical-action token, proves #120's
three-case exact-target behavior without treating it as authority, assigns
configured launch singleton safety to #57, closes native-entry no-effect/poison
direction, and specifies attended maximum-sequence rollover. Revision 14 resolves
the Round 6 composition defect with automatic configured checkpoint HOLD plus
the sole checked attended escape, while naming
`ConfiguredPreBarrierRetrySuccessorV1` as the dependency for any future checked
retry. It also specifies the exact stale-epoch result when rollover races an
ephemeral receipt. Q4 is **SPECIFIED; IMPLEMENTATION BLOCKED ON #115, #146,
#57, AND `ConfiguredPreBarrierRetrySuccessorV1`**. Merged raw
`Stop-Tree($targets)` remains an actual bypass, and the custody, configured
retry, launch singleton, and rollover contracts remain specified mechanisms
rather than runtime-enforced facts. Q4 is not complete, conforming, sealed, or
enforced until every named dependency lands and is reviewed; activation remains
prohibited. Overall 87-A additionally requires the merged-#120 adapter and
closure successor and remains incomplete, nonconforming, unsealed, unenforced,
and activation-prohibited until every named dependency lands and passes review.
The seventh and final docs re-panel is scoped only to the Revision 14 configured
HOLD/attended escape and ephemeral-rollover outcome, not a fresh open sweep. It
may accept the specified contract and blocking decomposition; it cannot approve
merged-code enforcement. This author disposition is not an approval.
