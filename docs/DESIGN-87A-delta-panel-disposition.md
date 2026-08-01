# Design 87-A: delta-panel disposition

**Status:** Revision 10 author fold against the panel over `f42570d..44b3787`,
the scoped Q4/Q8/Q9 correction over `49318ff`, and the structural Q4 correction
after the re-panel over `5fe41e0`; all eleven original findings are present,
including the operator-resolved M5 constraint and the merged-#120
reconciliation. Q9 remains approved; design only.

**Mode:** Reference.

**Audience:** 87-A Tier-3 reviewers and implementers of tasks #78, #115, and
#120, plus the closure successor.

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
Once a future conforming closure and #115 have otherwise authorized an attempt,
a successful termination that signals within the remaining wait budget, plus
the replacement-side recycle split, may avoid a false post-action HOLD when no
independent barrier reason applies. The split excludes only the retired-parent
ownership edge; a same-agent wrapper/wait classification still blocks. That
downstream outcome change is not capability availability.
Merged #120 accepts Linux `linux:<boot_id>:<start_ticks>` as observation input
but declares no macOS exact-token mapping, and its sole kill primitive acts only
through the Windows FILETIME branch and skips a new
`owned_process_tree` target without `start_filetime`
(`src/agenttalk/supervisor.py:8900-8932` at `587e7c1`). Revision 9 therefore
removes the invalid POSIX target projection and returns pre-reservation
`CAPABILITY_UNAVAILABLE(EXACT_TARGET_EXECUTOR_UNAVAILABLE)` without an owner or
target tuple, authority, reservation, attempt, debt, or external call. It does
not weaken the Windows FILETIME guard or dependency-track an imaginary adapter.
Revision 10 withdraws Revision 9's attempt to prove inherited-state safety by
enumerating entry points and promising whole-state byte identity. Persisted
effect evidence now carries only an inert exact-executor binding. A fresh,
non-serializable witness matching that binding is required to construct the
permit consumed by every effect-bearing target, reservation, call, receipt, or
authority/effect mutation. Deserialization can recover evidence but cannot
manufacture the object needed to act. Ordinary observation deltas and initial
fail-closed quarantine creation remain explicit non-effect mutations.

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
| B2 | Linearize external calls armed by checked state and close both two-poller gaps. | **PRESENT.** `ChildlessContinuationOwnerV1`, provider-version equality, and terminal retired-attempt IDs are defined in the module's [closure-successor contract](DESIGN-87A-owned-childless-wrapper-authority.md#published-120-snapshot-and-closure-successor-contracts). [Action-time closure](DESIGN-87A-owned-childless-wrapper-authority.md#action-time-closure-and-sole-teardown-path) requires the exclusive effect guard, `ARMED`/`CALL_RETURNED`, synchronous calls, predecessor-death proof, and release-only late `HELD`. [Exact state transitions](DESIGN-87A-owned-childless-wrapper-authority.md#exact-state-transitions) give `PRE_BARRIER` a state-only release, make takeover a no-call CAS with a closed phase mapping before `MAY_RECONCILE`, forbid reconciliation while a foreign continuation can resume, and keep `STOP_TREE/ARMED` from proving or reissuing the effect. Conformance item 16 exercises both commit/effect gaps; the core transition table and conformance item 32 bind the same rules into the combiner. Merged #120 contributes exact same-handle identity-check/termination for each openable exact-FILETIME Windows target and a bounded wait attempt after successful termination, but no POSIX exact-token executor, checked continuation owner, or action-scoped creation closure. Full effect linearization remains a successor prerequisite, not a delivered #120 API. |
| B3 | Remove the undefined strict-runtime nonce operand and retain real provenance. | **PRESENT.** `OwnedLaunchNonceProvenanceV1`, `OwnedWrapperIdentityV1`, the [#120 snapshot adapter](DESIGN-87A-owned-childless-wrapper-authority.md#published-120-snapshot-and-closure-successor-contracts), and the corrected positive join retain the actual sources. Nonce equality is checked-managed versus parsed-observed-root; runtime remains an independent agent/PID/start/generation source. Conformance items 15 and 23 independently remove/mismatch every actual source and verify the strict 64-entry v2 mapping, including mandatory exact Windows FILETIME identity for complete/absent records. |
| B4 | Prevent state loss from resetting the hard cap or erasing teardown debt. | **PRESENT, WITH A V1 CAPABILITY REDUCTION.** [Fail-closed state-loss quarantine](DESIGN-87A-owned-childless-wrapper-authority.md#fail-closed-state-loss-quarantine) denies all teardown and launch after missing, corrupt, torn, or rollback-unproven checked state. Revision 10 removes the prior automatic different-owner/extinction retirement: merged #120 supplies no trustworthy host/process-universe token, so V1 cannot prove that a locally absent prior owner is extinct everywhere. Automatic quarantine retirement is therefore unavailable on every platform and the state remains `STATE_PROVENANCE_LOST`/`POLICY_HELD` pending attended handling. This prevents state loss after attempts 1/2/3 or partially acted debt from recreating a budget or laundering debt. PID and start, hostname, state_epoch, process_source_digest, MachineGuid alone, local absence are explicitly insufficient. |
| M5 | Decide whether the unqualified no-daemon/no-persistence-plane/no-runtime-dependency invariant remains final or receives a mechanism exception. | **PRESENT — RESOLVED by operator on 2026-07-31 (Option A).** The invariant is absolute: no daemon, new persistence plane, durable helper or OS object, runtime dependency, or mechanism-specific/separately versioned exception. The core's [split, decision, and scope](DESIGN-87A-supervisor-classifier-authority.md#split-decision-and-scope) and [mechanism inventory](DESIGN-87A-supervisor-classifier-authority.md#mechanism-inventory) make this the design rather than a provisional baseline. The module's dependency table and conformance rules require existing checked state plus transient caller-owned synchronization. Without a fresh non-serializable witness matching persisted exact-executor evidence, no executor-dependent external effect and no authority-enabling or effect-owned mutation is constructible. The availability cost is explicit in three permanent V1 limitations: POSIX closure-dependent named teardown remains `CAPABILITY_UNAVAILABLE`/`POLICY_HELD`; automatic state-loss-quarantine retirement is unavailable pending attended handling; and declared same-platform state-file/workspace transfer, restore, rollback, or migration activation refuses before active-store admission with zero 87-A effect. The third is an activation refusal, not an active-agent hold; an out-of-band copy may be undetectable, is nonconforming, and has no 87-A guarantee. A future quarantine-retirement or transfer producer may read only an existing OS process-universe token and may not add a file, registry value, helper, daemon, OS object, persistence plane, or dependency. An unprovable implementation case creates a task, not a mechanism. |
| M6 | Eliminate the unproduced valid-guard `SPAWN_IN_FLIGHT` shape. | **PRESENT.** The core's [classifier continuity state](DESIGN-87A-supervisor-classifier-authority.md#classifier-continuity-state) requires `SPAWN_IN_FLIGHT.spawned_guard == null`; a valid result commits identity directly and ambiguity enters `AMBIGUOUS_LAUNCH`. Core conformance item 34 and module item 18 cover reload. |
| M7 | Preserve manual no-kill relaunch for a confirmed absent wrapper. | **PRESENT.** The core's [manual marker disposition and overlap](DESIGN-87A-supervisor-classifier-authority.md#manual-marker-disposition-and-overlap) evaluates outstanding debt first, then confirmed whole-wrapper absence, then the live-wrapper named kill gate. The module's [core combiner overlay](DESIGN-87A-owned-childless-wrapper-authority.md#core-combiner-overlay) states manual/automatic no-kill parity; conformance item 19 covers absence, debt, and present-wrapper cases. |
| M8 | Bank deterministic vectors for all seven new digest domains. | **PRESENT.** The module's [chained digest conformance vector](DESIGN-87A-owned-childless-wrapper-authority.md#chained-digest-conformance-vector) fixes exact payload bytes, byte counts, domains, and expected SHA-256 for `owner_identity_id`, `OwnedTargetDigestV1`, `process_source_digest`, owned-childless basis, `basis_id`, `authority_id`, and `debt_id`. Revision 10 retains the verified Windows `win-tree/v2` fixture unchanged. Conformance item 20 independently constructs typed objects, production-encodes them, requires exact byte/count equality, then hashes each produced payload while carrying forward the digest it just recomputed. Displayed digests and byte-flip propagation are interoperability/change-detection anchors, not independent schema/codec correctness proof. |
| m9 | Correct the stale single-condition reservation restatement. | **PRESENT.** The core's [manual marker disposition and overlap](DESIGN-87A-supervisor-classifier-authority.md#manual-marker-disposition-and-overlap) now states both required conjuncts: execution is `IDLE` and the same-poll terminal sequence differs from the ordinary poll sequence. |
| m10 | Qualify the two leading safety labels by their implementation dependencies. | **PRESENT.** The module's [safety decision](DESIGN-87A-owned-childless-wrapper-authority.md#safety-decision) records merged #120 as a delivered ownership-input dependency and labels actual teardown only after #115 and the closure successor; the dependency table repeats that split. |
| m11 | Define deterministic ordinals for reload residual captures. | **PRESENT BY REMOVING THE UNTYPED PATH.** Revision 10 makes matching `RELEASED` after retained `STOP_TREE/CALL_RETURNED` finalize conservatively as `EFFECT_UNPROVEN`, retain debt, and perform no external residual capture. Residual discovery occurs only on the next ordinary ordinal-zero poll. The core's [capture identity and coverage equality](DESIGN-87A-supervisor-classifier-authority.md#capture-identity-and-coverage-equality) still defines one CAS allocator for every actual nonordinary post-action/reconciliation capture; conformance item 21 races those allocators and proves the ordinary/nonordinary context split. |

## Post-`49318ff` scoped panel corrections

The panel held Q1, Q2, and Q3 under attack; Revision 10 does not reopen those
arguments or the operator-decided M5 constraint. Q9 was approved after focused
re-panel and remains frozen below. The current re-panel scope is Q4 only. The
two Q8 residual obligations remain follow-up boundaries, not new authority
logic.

| ID | Panel result | Revision 10 disposition |
| --- | --- | --- |
| Q4 | The design projected a valid Linux exact token to `Stop-Tree`, but merged #120 only accepts that token as input and skips the corresponding target at execution. | **PRESENT — RELEASE BLOCKER RESOLVED.** The module deletes the non-Windows destructive mapping/type/join/projection and adds `EXACT_TARGET_EXECUTOR_UNAVAILABLE` to the static capability reasons. A valid Linux-token snapshot remains observation/barrier input; macOS has no admitted exact-token mapping. With quarantine `NONE`, every fresh non-Windows named path constructs no destructive owner/target tuple or authority and performs no reservation, attempt, debt mutation, or external call. The mandatory control cites the actual Windows execution branch and non-Windows skip at `src/agenttalk/supervisor.py:8900-8932` rather than the input-acceptance lines. The Windows exact-FILETIME requirement remains absolute. |
| Q4-R1 | The fresh refusal did not cover a Windows-created closure-bearing state resumed, reloaded, or inherited on a non-Windows host; reconcile/release or finalization could call externally or mutate debt there. | **WITHDRAWN AND REPLACED.** Revision 9 tried to close this finding by enumerating 17 entry points and claimed that executor unavailability preserved every checked field byte-identically. Revision 10 expressly withdraws that whole-state claim and the enumeration as its proof. Ordinary `ClassifierObservationDeltaV1` commits and initial `StateLossQuarantineV1.UNRESOLVED` creation may legitimately mutate observation or fail-closed state. The replacement universal is narrower and structural: without a fresh non-serializable witness matching the persisted exact-executor binding, no executor-dependent external effect and no authority-enabling or effect-owned mutation is constructible. |
| Q4-R2 | Childless-origin `SPAWN_IN_FLIGHT` followed by `AMBIGUOUS_LAUNCH` was an eighteenth persisted entry point omitted by the Revision 9 inventory. | **PRESENT — RELEASE BLOCKER REMEDIATED BY CONSTRUCTION.** `ChildlessEffectEnvelopeV1` contains inert evidence and one immutable `ExactTargetExecutorBindingV1`, never a replayable permit. `ExecutableOwnedTargetSetV1`, `PermitBoundChildlessMutationV1`, and `ChildlessExternalEffectCallV1` each require a fresh matching `CurrentExactTargetExecutorWitnessV1`-derived `ExactTargetExecutorPermitV1`; external adapters accept only the typed call and return only `ChildlessExternalEffectReceiptV1`. Therefore an eighteenth, nineteenth, or future state may deserialize evidence but cannot manufacture the object needed to act. `SPAWN_IN_FLIGHT` and `AMBIGUOUS_LAUNCH` need no special safety exception. |
| Q4-R3 | Malformed-state precedence conflicted with executor-unavailable precedence, while ordinary observation legitimately changed poll, ordinal, and terminal fields before the unavailable decision. | **PRESENT — CLAIM NARROWED AND MUTATIONS PARTITIONED.** The checked owner distinguishes observation-only deltas, initial fail-closed quarantine creation, and permit-bound authority/effect deltas. The first two cannot construct effect objects; the third is rejected without the fresh matching permit. Ordinary observation may advance its documented projection, so the design no longer promises whole-state byte identity. Malformed-versus-unavailable output precedence remains deterministic but is not relied on as the safety boundary. |
| Q4-R4 | The complete-extinction quarantine carve-out was not bound to a host/process universe, so local absence could clear a transferred quarantine while the prior tree remained live elsewhere. | **PRESENT — V1 AUTOMATIC RETIREMENT REMOVED.** Merged #120 supplies no trustworthy host/process-universe token. V1 therefore has no automatic quarantine-retirement constructor on any platform; a transferred or local quarantine remains `STATE_PROVENANCE_LOST`/`POLICY_HELD` pending attended handling. PID and start, hostname, state_epoch, process_source_digest, MachineGuid alone, local absence are insufficient. A future successor must use a read-only producer over an existing OS token and must preserve the absolute prohibition on a new file, registry value, helper, daemon, OS object, persistence plane, or dependency. |
| Q4-R5 | Initial state-loss quarantine creation was omitted from the zero-mutation entry inventory. | **PRESENT — EXPLICIT FAIL-CLOSED MUTATION.** Missing, corrupt, torn, or rollback-unproven expected state may create a new epoch only together with `StateLossQuarantineV1.UNRESOLVED`. That mutation creates no authority, effect object, attempt budget, debt clear, identity commit, kill, or launch. It is deliberately outside both the withdrawn byte-identity claim and the permit-bound effect-mutation class. |
| Q8-R1 | A bare `CAPABILITY_UNAVAILABLE` code does not tell an operator whom to act on. | **PRESENT.** The core/module 87-B boundary requires an exact-fingerprint join, the held agent from `canonical_condition.agent_key`, and explicit text that operator action is required. Bare-code or agentless rendering is nonconforming and covered by the mandatory controls. |
| Q8-R2 | Operators need advance documentation that fail-closed capability refusal can strand recovery indefinitely. | **PRESENT, WITH ALL THREE PERMANENT V1 LIMITATIONS NAMED TOGETHER.** 87-A implementation close and 87-C activation require reviewed 87-B/follow-up operator-manual and tutorial evidence that: (1) every Linux/macOS closure-dependent named teardown remains `CAPABILITY_UNAVAILABLE`/`POLICY_HELD` pending a human until a real exact-token adapter exists; (2) automatic state-loss-quarantine retirement is unavailable on every platform in V1, so provenance loss remains `STATE_PROVENANCE_LOST`/`POLICY_HELD` pending attended handling; and (3) declared same-platform state-file/workspace transfer, restore, rollback, or migration activation refuses before active-store admission. Held-agent projections apply to items 1 and 2; item 3 identifies the rejected operation/store and directs attended handling rather than fabricating an active agent. |
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
is unchanged. Revision 10 withdraws the Revision 9 whole-state guarantee,
replaces entry-point enumeration with a capability-by-construction boundary,
removes unsafe automatic quarantine retirement from V1, and states all three
permanent capability limitations together, including declared activation
refusal and the nonconforming out-of-band-copy residual. A scoped Q4-only
re-panel remains required; this author disposition is not an approval.
