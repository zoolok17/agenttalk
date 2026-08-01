# Design 87-A: delta-panel disposition

**Status:** Revision 8 author fold against the panel over `f42570d..44b3787`;
all eleven findings are present, including the operator-resolved M5 constraint
and the merged-#120 reconciliation; design only.

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
for an openable exact-matching target, a bounded wait attempt after successful
termination, a recycle-aware deny-only launch barrier, exact attended-reset
evidence, and a request-bound attended archive.
Those mechanisms strengthen target identity and post-effect observation. They
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

## Method and status vocabulary

The author checked premise-level findings B1, B3, and M7 against shipped source
and the `44b3787` equations before revising either normative document. Every
row uses exactly one permitted disposition:

- **PRESENT** — folded into the normative design with a mandatory control;
- **DEFERRED** — assigned to named later work;
- **DROPPED** — intentionally rejected with a reason; or
- **PREMISE_REFUTED** — not folded because producer evidence disproved it.

No item is unassigned. All eleven panel premises hold and all eleven are
**PRESENT**; there are no deferred, dropped, or premise-refuted rows.

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
| B2 | Linearize external calls armed by checked state and close both two-poller gaps. | **PRESENT.** `ChildlessContinuationOwnerV1`, provider-version equality, and terminal retired-attempt IDs are defined in the module's [closure-successor contract](DESIGN-87A-owned-childless-wrapper-authority.md#published-120-snapshot-and-closure-successor-contracts). [Action-time closure](DESIGN-87A-owned-childless-wrapper-authority.md#action-time-closure-and-sole-teardown-path) requires the exclusive effect guard, `ARMED`/`CALL_RETURNED`, synchronous calls, predecessor-death proof, and release-only late `HELD`. [Exact state transitions](DESIGN-87A-owned-childless-wrapper-authority.md#exact-state-transitions) give `PRE_BARRIER` a state-only release, make takeover a no-call CAS with a closed phase mapping before `MAY_RECONCILE`, forbid reconciliation while a foreign continuation can resume, and keep `STOP_TREE/ARMED` from proving or reissuing the effect. Conformance item 16 exercises both commit/effect gaps; the core transition table and conformance item 32 bind the same rules into the combiner. Merged #120 contributes exact same-handle identity-check/termination for each openable exact-matching target and a bounded wait attempt after successful termination, but no checked continuation owner or action-scoped creation closure. Full effect linearization remains a successor prerequisite, not a delivered #120 API. |
| B3 | Remove the undefined strict-runtime nonce operand and retain real provenance. | **PRESENT.** `OwnedLaunchNonceProvenanceV1`, `OwnedWrapperIdentityV1`, the [#120 snapshot adapter](DESIGN-87A-owned-childless-wrapper-authority.md#published-120-snapshot-and-closure-successor-contracts), and the corrected positive join retain the actual sources. Nonce equality is checked-managed versus parsed-observed-root; runtime remains an independent agent/PID/start/generation source. Conformance items 15 and 23 independently remove/mismatch every actual source and verify the strict 64-entry v2 mapping, including mandatory exact Windows FILETIME identity for complete/absent records. |
| B4 | Prevent state loss from resetting the hard cap or erasing teardown debt. | **PRESENT.** `StateLossQuarantineV1`, `OwnedPhysicalWrapperIdentityV1`, and `ProvablyDifferentPhysicalOwnerV1` are closed module types. [Fail-closed state-loss quarantine](DESIGN-87A-owned-childless-wrapper-authority.md#fail-closed-state-loss-quarantine) denies all teardown and launch until complete different-owner/extinction proof. This revision deliberately has no exact-restoration escape because a structurally valid backup may be one committed generation stale. Conformance item 17 covers loss after attempts 1/2/3, partially acted debt, and stale backup; the core's global eligibility, transition table, and conformance item 33 make `STATE_PROVENANCE_LOST` combiner-enforced. |
| M5 | Decide whether the unqualified no-daemon/no-persistence-plane/no-runtime-dependency invariant remains final or receives a mechanism exception. | **PRESENT — RESOLVED by operator on 2026-07-31 (Option A).** The invariant is absolute: no daemon, new persistence plane, durable helper or OS object, runtime dependency, or mechanism-specific/separately versioned exception. The core's [split, decision, and scope](DESIGN-87A-supervisor-classifier-authority.md#split-decision-and-scope) and [mechanism inventory](DESIGN-87A-supervisor-classifier-authority.md#mechanism-inventory) make this the design rather than a provisional baseline. The module's [meaning of COMPLETE and HELD](DESIGN-87A-owned-childless-wrapper-authority.md#meaning-of-complete-and-held), dependency table, and conformance item 22 require existing checked state plus transient caller-owned synchronization. Merged #120 does not implement the closure contract; until a conforming successor proves it, pre-reservation `CAPABILITY_UNAVAILABLE` creates no reservation/attempt/call, performs no closure-dependent named teardown, and keeps recovery `POLICY_HELD` pending a human. Structural unavailability never becomes `CLOSURE_VETOED`, retry, or exhaustion. An unprovable implementation case creates a task, not a mechanism. |
| M6 | Eliminate the unproduced valid-guard `SPAWN_IN_FLIGHT` shape. | **PRESENT.** The core's [classifier continuity state](DESIGN-87A-supervisor-classifier-authority.md#classifier-continuity-state) requires `SPAWN_IN_FLIGHT.spawned_guard == null`; a valid result commits identity directly and ambiguity enters `AMBIGUOUS_LAUNCH`. Core conformance item 34 and module item 18 cover reload. |
| M7 | Preserve manual no-kill relaunch for a confirmed absent wrapper. | **PRESENT.** The core's [manual marker disposition and overlap](DESIGN-87A-supervisor-classifier-authority.md#manual-marker-disposition-and-overlap) evaluates outstanding debt first, then confirmed whole-wrapper absence, then the live-wrapper named kill gate. The module's [core combiner overlay](DESIGN-87A-owned-childless-wrapper-authority.md#core-combiner-overlay) states manual/automatic no-kill parity; conformance item 19 covers absence, debt, and present-wrapper cases. |
| M8 | Bank deterministic vectors for all seven new digest domains. | **PRESENT.** The module's [chained digest conformance vector](DESIGN-87A-owned-childless-wrapper-authority.md#chained-digest-conformance-vector) fixes exact payload bytes, byte counts, domains, and expected SHA-256 for `owner_identity_id`, `OwnedTargetDigestV1`, `process_source_digest`, owned-childless basis, `basis_id`, `authority_id`, and `debt_id`. Revision 8 renews the chain for merged #120's `win-tree/v2` adapter and the explicit representation-token/exact-guard split without changing either banked core fingerprint. Conformance item 20 recomputes the chain and direction controls. |
| m9 | Correct the stale single-condition reservation restatement. | **PRESENT.** The core's [manual marker disposition and overlap](DESIGN-87A-supervisor-classifier-authority.md#manual-marker-disposition-and-overlap) now states both required conjuncts: execution is `IDLE` and the same-poll terminal sequence differs from the ordinary poll sequence. |
| m10 | Qualify the two leading safety labels by their implementation dependencies. | **PRESENT.** The module's [safety decision](DESIGN-87A-owned-childless-wrapper-authority.md#safety-decision) records merged #120 as a delivered ownership-input dependency and labels actual teardown only after #115 and the closure successor; the dependency table repeats that split. |
| m11 | Define deterministic ordinals for reload residual captures. | **PRESENT.** The core's [capture identity and coverage equality](DESIGN-87A-supervisor-classifier-authority.md#capture-identity-and-coverage-equality) defines one CAS allocator for every nonordinary capture. The module's [exact state transitions](DESIGN-87A-owned-childless-wrapper-authority.md#exact-state-transitions) requires a current-sequence nonzero ordinal for reload post-action/residual capture and preserves ordinal zero for ordinary input; conformance item 21 races two allocators and checks context separation. |

## Frozen evidence boundary

This fold does not alter the 48-row/96-cell dominant matrix, either banked
`RecoveryConditionFingerprintV1` payload/vector, or their counts. The
independently verified size figures remain the frozen `44b3787` delta-panel
input; the core labels them as historical evidence rather than recomputing
them. The new seven-domain module vector is additive conformance evidence for
M8 and does not rewrite either banked core vector.

## Conclusion

All four blockers, all four majors, and all three minors are **PRESENT** at
named normative locations with mandatory controls. M5 is resolved as the
absolute Option A constraint. A fresh Tier-3 panel remains required over this
fold; this author disposition is not an approval.
