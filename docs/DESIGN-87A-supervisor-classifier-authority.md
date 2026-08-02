# Design 87-A: Supervisor classifier and recovery-authority totality

**Design status:** Proposed, Revision 16; candidate for
**NORMATIVE-SPECIFICATION COMPLETE** with the operator-directed owned-childless
wrapper authority. This core and its
[normative owned-childless module](DESIGN-87A-owned-childless-wrapper-authority.md)
at the same commit constitute 87-A; neither is conforming alone.

**Delivery status:** **IMPLEMENTATION BLOCKED** on task #115, task #146, task
#57, `ExactIssuerIdentityAdapterV1`, and the closure successor. Revision 15
specifies the definitive-`GONE` attended owner-death escape and names, but does
not define or deliver, `ExactIssuerIdentityAdapterV1`, which is required to classify a
reused issuer PID.

**Conformance status:** **UNAVAILABLE.** Neither Q4 nor 87-A is complete,
conforming, sealed, or enforced in merged code. A successful design panel may
complete only the normative specification.

**Activation status:** **PROHIBITED** until delivery and conformance close.

**Mode:** Reference.

**Audience:** Contributors and reviewers implementing the supervisor classifier,
recovery-authority combiner, shared process observer, and their tests.

**Goal:** Given the same captured evidence, two conforming implementations must
choose the same presence state, teardown origin, replacement proof, escalation
predicate, and final action.

Revision 2 (`20fa5f238826d7dafa334e6589b6e0392bbe37af`) attempted
to specify classifier totality, incident delivery, and migration in one
1,393-line document. Revision 3 replaces that monolith with three
specifications: 87-A, 87-B, and 87-C. This core and its same-commit normative
module are the atomic two-file 87-A specification. 87-B and 87-C do not yet
exist.

## Requirement labels

Every asserted property in this document has one of these labels:

- **ENFORCED** means this document defines a closed type, constructor, equation,
  invariant, or mandatory conformance test that mechanically decides the
  property. It means “implementable and test-gated,” not “currently shipped.”
- **STATED** means a verified current fact, scope boundary, dependency, ordering
  decision, or accepted residual. 87-A supplies no mechanism that enforces it.
- **NORMATIVE-SPECIFICATION COMPLETE** means a panel has accepted a closed,
  implementable contract and its mandatory evidence. It does not assert that
  executable enforcement exists.
- **IMPLEMENTATION BLOCKED** means at least one named prerequisite required for
  conformance is absent from merged code. Revision 16 is independently blocked
  on #115, #146, #57, `ExactIssuerIdentityAdapterV1`, and the closure successor.

An implementation may claim conformance to 87-A only when every ENFORCED rule
and required test below exists in executable code. Until then, all behavior
described here remains proposed.

### Identity and equality vocabulary

Across this atomic four-document set, the **exact** stem is reserved for named
identity types/adapters or direct references to those identity contracts, and
ordinary cardinality/multiplicity (`exactly one` / `exactly once`).
`OwnedExactStartGuardV1` is such a type; `ProcStartGuardV1` and
`ExecutionGateCaptureV1.guarded_start` are not. A field described as
**source-equal** carries the same typed value as the named source, but that
equality grants no process-identity comparator. **Byte-identical** means equal
canonical serialized bytes or byte-for-byte preservation. **Matching** names
an ordinary predicate without granting identity authority, and **closed** means
that the displayed algebra has no other variants. No other use of the exact
stem is admitted in these four documents.

## Split, decision, and scope

**ENFORCED by the closed types and combiner below:** The supervisor derives
teardown, replacement, and escalation authority independently. Refusing a kill
does not erase positive absence, and refusing both destructive authorities does
not erase mandatory escalation.

**STATED split:**

| Document | Owns | Does not own |
| --- | --- | --- |
| 87-A, this core plus its normative module | Runtime, active-child, presence, targetability, heartbeat-freshness, and manual-marker classifiers; classifier continuity state; authority equations; origin selection; physical-absence and owned-childless proofs; retry exhaustion; semantic condition record/fingerprint; and final-barrier invariants. | Incident persistence/delivery and rollout. |
| 87-B, future | Incident, promise, projection, and delivery contract. It consumes `RecoveryConditionV1` and action/result resolution inputs from 87-A. | Classifier authority and migration. |
| 87-C, future | Activation, compatibility, migration, rollback, and flag-day procedure after 87-A and 87-B are closed. | Classifier and incident semantics. |

**STATED dependencies and ordering:**

- Task #114 owns the current cold-start kill-switch exit that precedes instance
  claim. 87-B depends on that task instead of redesigning it.
- Task #115 owns the missing linearizable supervisor-state read-modify-write
  lock/API. 87-A specifies pure equations plus sealed-receipt contracts, while
  #115 owns the sole begin/checked-reduce/commit API and derives ordinary
  observation mutation inside that checked transaction. Durable state epoch/revision,
  freshness anchors, runtime high-water/latch, confirmation counters, poll
  identity, consumed manual IDs, absence consumption, and guarded-identity
  commit cannot ship until task #115 supplies that mechanism. Revision 15 also
  assigns #115 the checked attended disposition of a source-equal configured
  `RESERVED/PRE_BARRIER` checkpoint after its issuer is independently proved
  dead; that operation never kills or launches and cannot mint retry custody.
  For Q4's resulting fence only, #115's ordinary-observation commit also owns
  the private post-commit witness and unexported deny-only barrier reducer over
  the sealed merged-#120 observation operands. This narrow receipt producer is
  part of #115's Q4 delivery, not the separately undelivered general 87-A
  adapter and not new external authority.
- Task #116 owns earlier recovery from twice-confirmed physical absence. It is
  mechanically blocked on task #115, explicitly not blocked on Design 87, and
  is scheduled immediately after #115 and before 87-A implementation. It
  remains independently stageable.
- Task #120 shipped on master as squash commit `587e7c1`. It owns a bounded
  64-entry nonce-anchored tree snapshot, Windows FILETIME target identity,
  same-handle identity-check/termination for an openable target whose live
  creation FILETIME matches its `OwnedExactStartGuardV1`, a bounded wait attempt after successful termination,
  a recycle-aware deny-only post-kill launch barrier, attended-reset
  evidence, and a request-bound attended archive. It does not
  implement action-scoped
  child-creation closure, a checked continuation owner, or attempt-keyed
  acquire/reconcile/release. It also does not implement a named POSIX
  exact-target-identity kill adapter: its current supervisor owned-tree native body acts on Windows
  FILETIME and skips new
  `owned_process_tree` targets without that field.
- Task #146 owns the supervisor owned-tree effect-entry migration. Merged code
  still exposes raw `Stop-Tree($targets)` at
  `src/agenttalk/supervisor.py:8889-8939`; the configured-agent and ephemeral
  planners call it with `.kill_targets` at `9392` and `9591`. Revision 15
  specifies one closed dispatcher over opaque childless, configured-agent, and
  ephemeral-terminal variants, with one private atomic use owner and winning
  admission, plan ownership, native entry, and receipt-consumption transitions
  per call. It does not claim that runtime seal exists until
  #146 migrates every caller, removes the raw entry, and passes the direction
  controls. “Sole native body” is scoped to the supervisor owned-tree executor;
  `src/agenttalk/wrapper/turn_watchdog.py` has a separate kill facility outside
  87-A and #146.
- Task #57 owns the missing durable project-level singleton per wrapped agent
  (launch lock). The configured-agent kill subphase is target-idempotent under
  #120's FILETIME identity rules, but a replayed `Start-Process` is not
  idempotent. A transient 87-A custody owner cannot survive the crash that
  leaves the configured relaunch checkpoint replayable. Therefore automatic
  configured relaunch remains implementation- and activation-blocked until #57
  lands and is reviewed; 87-A does not respecify that task's lock.
- `ExactIssuerIdentityAdapterV1` is a named, separately reviewed implementation
  dependency for deciding that a present issuer PID belongs to a different
  process instance. Revision 15 deliberately does not define its platform
  identity types, canonical serialization, provider binding, comparator, or
  ambiguity rules. Until that adapter is delivered, a fresh independent OS
  result can prove issuer extinction only by definitive PID absence
  (`GONE`). A present PID, including one whose generic captured start token
  differs and suggests reuse, remains `LIVE_OR_UNPROVEN`; reuse-based disposal is
  `CAPABILITY_UNAVAILABLE(ExactIssuerIdentityAdapterV1)`, and no `RECYCLED`
  result is admitted. The checkpoint's
  token digest, PID, and `ProcStartGuardV1` are source-equal audit fields from
  the live issuer's `ExecutionGateCaptureV1`, not evidence admitted by
  `ExactIssuerIdentityAdapterV1` and not disposal authority.
- `ConfiguredPreBarrierRetrySuccessorV1` is the named, separately reviewed
  optional implementation seam for any future **automatic** retry after the
  configured action issuer dies while durable state remains
  `NON_CHILDLESS/RESERVED/PRE_BARRIER`. The current closed transition set has no
  replay, resume, release-and-reserve, or custody-remint transition. Until this
  successor is delivered and reviewed, reload derives the source-bound
  `ConfiguredPreBarrierOwnerLossHoldV1`, remains `POLICY_HELD`, and names the
  checked attended disposition below as its only V1 escape. That disposition
  installs `ConfiguredPriorEffectUnknownFenceV1`; it does not claim the prior
  native effect is known. This optional successor is not a Q4 blocker. It may
  not infer a new owner from persisted reservation, target, barrier, or call
  provenance and is not supplied by #120's target-local retry safety.
- The **closure successor**—a separately reviewed extension to #120 or a
  successor task—owns that missing closure and the synchronous typed adapters
  needed to linearize its external effects with #115 checked state. Merged
  #120 is a Windows-only partial effect-side primitive, not that successor.
  Raw persisted state, target tuples, bindings, and IDs are never adapter
  inputs: adapters accept only live permit-bound calls and return only matching
  typed receipts. Until a conforming successor exists, a static
  pre-reservation `ClosureCapabilityV1` returns `CAPABILITY_UNAVAILABLE`
  without an attempt or external call, and the dependent recovery remains
  `POLICY_HELD` pending a human. Task #78 consumes the constructor/cap only
  after #115, #146's sealed dispatcher migration, the adapter over merged #120,
  and that successor.
  Task #116 remains independent because an absent wrapper needs no teardown
  target.
- Revision 15 preserves existing non-87-A owned-tree behavior, not the raw
  `Stop-Tree` entry point. Only #146's closed dispatcher may reach the private
  supervisor owned-tree native body after migration.
- The task #81 recovery umbrella preceding Design 87 is consistent with the
  task #94 umbrella-first release policy. 87-A does not challenge that order.

**STATED placement/size decision:** This addition stays in 87-A because this
combiner consumes it, but its new proof/execution mechanics live in one
same-revision normative module. That is a review-surface split, not a
dependency edge: core classifiers feed the module; its closed result feeds the
core combiner/output. Authority initiation overlays the child-death-sourced
runtime subset; subsequent module debt and cycle are global safety constraints
over later different-owner, relaunch-only, automatic-selection, result, and
attention decisions. Banked mechanics do not move. The split bounds the newly
reviewed delta and keeps task #120's platform mechanism and the closure
successor out of 87-A. The #146 dispatcher migration, adapter over merged #120,
and closure-successor mechanism are prerequisites, not additional 87-A
conformance files.

**STATED frozen delta-panel input at `44b3787` (whitespace-token count, UTF-8
bytes):** core
`2547` lines / `16090` words / `135979` bytes; normative module
`1245` / `7777` / `64669`; atomic normative 87-A
`3792` / `23867` / `200648`; disposition audit
`140` / `2327` / `17448`. Against `f42570d`'s
`2223` / `13431` / `113215` core, the atomic specification grows by
`1569` lines / `10436` words / `87433` bytes
(`77.70%` words). This cost is accepted for the fresh authority,
closure, debt, and cap contract; splitting task #120's snapshot/barrier and
the closure-successor mechanism separately keeps platform implementation
detail out without separating 87-A's authority.
These independently reverified figures remain frozen historical panel
evidence; later revisions do not recompute or replace
them.

**STATED non-goals:** 87-A does not specify notification routes, human receipt,
incident retention, state-extension compatibility, executor capability
activation, migration, rollback, wrapper-writer generations, or a rollout
runbook. Those concerns must not be reintroduced here, except that 87-A defines
the fail-closed V1 boundary: a conforming activation path refuses any declared
transfer, restore, rollback, or migration before imported state becomes the
active checked store. 87-C owns any future supported workflow.

**DECIDED dependency-plane constraint (operator, 2026-07-31; M5 Option A):**
Implementation adds no daemon, new persistence plane, durable helper or
durable OS object, or runtime dependency; `pyproject.toml:13` remains
`dependencies = []`. It may add only pure code and fields to the existing
checked supervisor state after task #115 plus transient caller-owned
synchronization that leaves no durable helper or OS object. Neither task #120
nor the closure successor relaxes this absolute promise. There is no
mechanism-specific or separately versioned exception.

If a platform cannot construct `CurrentExactTargetExecutorWitnessV1` or prove
synchronous action-scoped closure inside that boundary, `ClosureCapabilityV1` is
`CAPABILITY_UNAVAILABLE`. Merged #120 alone does not satisfy that conjunction,
so the static pre-reservation refusal remains mandatory: create no childless
effect envelope or reservation, consume no attempt, make no external call,
perform no closure-dependent named teardown, and keep the dependent recovery
`POLICY_HELD` with `CAPABILITY_UNAVAILABLE` pending a human. Structural
unavailability is never an ordinary closure veto, retry, or exhaustion.

Revision 10 replaced Revision 9's path-enumerated whole-state
byte-identical-preservation claim. That claim is withdrawn: an ordinary observation may legitimately
advance poll identity, reset the ordinary capture ordinal, clear a prior-poll
terminal, and update continuity or confirmation state while an executor is
unavailable. The narrower universal is structural and stronger: without a
fresh non-serializable executor witness that matches the persisted inert
  binding, no 87-A childless executor-dependent external effect and no
  childless authority-enabling or effect-owned mutation is constructible.
  Separately, no supervisor owned-tree native termination is reachable except
  through one `SupervisorOwnedTreeNativeInvocationV1` created by the winning
  admission, plan-ownership, and native-entry transitions for a closed
  dispatcher variant; that second rule is specified but remains undelivered
  until #146. Deserializing a reservation, phase,
debt, retired ID, or future childless state yields evidence only; it cannot
manufacture the permit, executable call, receipt, or checked delta needed to
act.

In that universal, authority-enabling mutation is the module's closed term for
a change to childless reservation/execution/attempt/closure/debt/cycle/
continuation/retired-attempt/nonordinary-capture/spawn/guarded-identity
ownership or for clearing such a fence. Pure ordinary-observation evidence is
not an authority object: it may change only when #115 consumes one opaque
commit-custody handle over a sealed ordinary-observation receipt and derives
the private mutation inside its checked
RMW transaction. It cannot reserve, consume, or execute the predicate it later
helps satisfy.

Automatic state-loss-quarantine retirement is also unavailable in V1. Merged
#120 supplies no trustworthy process-universe token, so local absence or a
locally different owner cannot clear lost attempt/debt provenance. The
quarantine remains `POLICY_HELD` pending attended handling. A future successor
may use only a read-only producer over an existing OS token; it may not add a
file, registry value, helper, daemon, OS object, persistence plane, or runtime
dependency.

Same-platform state-file/workspace transfer, restore, rollback, and migration
activation are likewise unavailable in V1. When a conforming activation path
is told, or otherwise knows, that checked state came from one of those
operations, it must refuse before admitting or decoding those bytes as the
active checked store. The refusal constructs no
`CurrentExactTargetExecutorWitnessV1`, permit, authority/effect mutation,
external call, or launch and directs attended handling and 87-C. It is an
activation refusal, not an active-agent `CAPABILITY_UNAVAILABLE`/`POLICY_HELD`
result. An out-of-band copy or overwrite presented as an ordinary same-store
restart may be indistinguishable from local checked state and may proceed; that
bypass is nonconforming and has no 87-A safety or recovery guarantee. If the
existing outer-state checks detect rollback-unproven state, only fail-closed
`StateLossQuarantineV1.UNRESOLVED` is admitted. Future 87-C must bind the source
universe within M5 Option A or keep imported state inert.
Finding an unprovable case during implementation creates a task; it never
authorizes an implementation-detail mechanism.
This is an intentional availability cost: some recoveries may never become
automatic. The operator accepted that outcome rather than permit stale
authority.

The companion
[`DESIGN-87A-revision-2-disposition.md`](DESIGN-87A-revision-2-disposition.md)
accounts for every normative Revision 2 requirement as present here, deferred
to named 87-B/87-C work, or explicitly dropped/replaced. It is a split-integrity
audit, not another normative specification.
The
[`DESIGN-87A-delta-panel-disposition.md`](DESIGN-87A-delta-panel-disposition.md)
register separately accounts for every finding in the panel over
`f42570d..44b3787`; it is likewise audit evidence, not a conformance file.

## Closed inputs that remain closed

**ENFORCED by independent constructors and the dominant-projection matrix:**
`RuntimeObservation` is derived without heartbeat or
`WrapperPresenceResultV1`. Its active-child input is a separate projection from
process evidence, defined below. `WrapperPresenceResult` is derived from one
process observation without runtime health or heartbeat. Shared raw snapshot
rows may feed both private projections, but neither projection may read the
other's result. Runtime dominant, presence, and freshness are crossed only
after all three exist. This preserves the closed F1 independence result while
making the active-child evidence flow explicit.

**ENFORCED by an action-scoped, non-persisted conditional value:**
`CONDITIONAL_POST_TEARDOWN` resolves synchronously from one guarded teardown
result and one fresh shared-observer capture. It is never stored between polls.
A failure resolves to `NONE`; no pending value can wait forever. This preserves
the closed F4 result.

**STATED source correction:** `brain_ancestry_ambiguous` occurs only after the
wrapper PID/start identity and launcher attribution have succeeded and
`wrapper_state="alive"` has been established
(`src/agenttalk/supervisor.py:3390-3408` and
`src/agenttalk/supervisor.py:3478-3484`). It therefore contributes
`ActiveChildObservationV1.UNKNOWN`, and from there
`CURRENT_UNKNOWN_ACTIVE_CHILD`, not a `WrapperPresence` state. 87-A does not
revive the rejected visible-wrapper-attribution framing.

## Runtime observation

### Closed value and precedence

**ENFORCED by the private constructor:** `RuntimeObservationV1` is:

```text
RuntimeObservationV1 {
  dominant: RuntimeDominantV1
  reasons: nonempty tuple[RuntimeDominantV1]
}
```

The constructor evaluates every predicate, substitutes
`CURRENT_UNKNOWN_OTHER` if none matches, deduplicates matches, sorts them by
this displayed rank, and sets `dominant = reasons[0]`:

1. `CURRENT_UNKNOWN_SEQUENCE_REGRESSION`
2. `CURRENT_UNKNOWN_BINDING`
3. `CURRENT_UNKNOWN_STARTING_OVERRUN`
4. `CURRENT_UNKNOWN_ACTIVE_CHILD`
5. `CURRENT_BLOCKED_STALL`
6. `INVALID_CONTRACT`
7. `UNSUPPORTED_CONTRACT`
8. `CONTRACT_ABSENT`
9. `CURRENT_UNKNOWN_OTHER`
10. `CURRENT_TEARDOWN_PROOF`
11. `CURRENT_STALE_RECOVERABLE`
12. `CURRENT_PROGRESS_HEALTHY`

Supplying an inconsistent `(dominant, reasons)` pair is invalid. Authority code
cannot construct this type directly.

**ENFORCED state semantics:** Each state is the strict verdict of the runtime
classifier. “Strict” means that neither heartbeat nor wrapper-presence policy
may rewrite it.

| State | Normative meaning |
| --- | --- |
| `CURRENT_PROGRESS_HEALTHY` | The complete current schema and binding are valid, and positive current evidence such as advancing adapter progress or bounded spawn grace determines health without heartbeat staleness. |
| `CURRENT_STALE_RECOVERABLE` | The complete current schema and binding are valid, and the phase/configuration permits authoritative stale heartbeat to complete recovery proof. This includes idle, terminal, and a confirmed active stall whose static guards permit heartbeat recovery. It means “eligible if stale,” not “heartbeat is stale.” Idle and terminal map here for both freshness values. |
| `CURRENT_TEARDOWN_PROOF` | The complete current schema and binding are valid, and a heartbeat-independent predicate such as confirmed child death or an authoritative watchdog deadline proves recovery is due. |
| `CURRENT_UNKNOWN_SEQUENCE_REGRESSION` | The same wrapper/turn generation publishes a sequence below persisted high-water or the sticky regression latch is already set. |
| `CURRENT_UNKNOWN_BINDING` | A strict current record cannot bind to the supervisor-managed wrapper identity. Physical wrapper presence remains independent. |
| `CURRENT_UNKNOWN_STARTING_OVERRUN` | Phase remains `starting` after bounded spawn grace. |
| `CURRENT_UNKNOWN_ACTIVE_CHILD` | Active phase cannot bind a uniquely guarded live or positively absent CLI child. |
| `CURRENT_BLOCKED_STALL` | Progress is stalled, but heartbeat/watchdog recovery guards are not authoritative. |
| `CURRENT_UNKNOWN_OTHER` | A defensive current-schema tuple is unclassified or internally incoherent. |
| `CONTRACT_ABSENT` | No runtime artifact exists. This is expected for a pre-contract wrapper, but absence alone does not prove why it is missing. |
| `UNSUPPORTED_CONTRACT` | A bounded valid envelope identifies a schema version this supervisor does not implement. |
| `INVALID_CONTRACT` | The record is malformed, torn, internally incoherent, or fails strict current-schema validation. |

The positive `CURRENT_PROGRESS_HEALTHY × PRESENT_*` `HOLD` cells below are
green health states, not operator-visible recovery holds. More generally,
`HOLD (strict verdict)` means the current-contract health classifier chose its
non-recovery state—healthy, working, starting, terminal, suspect, or another
strict class as defined above. Matrix annotations after `/` are the
operator-visible recovery dispositions.

Runtime-to-managed-wrapper binding uses only the strict runtime record and
supervisor-owned managed identity: agent/root, wrapper generation, launcher
nonce, and guarded recorded PID/start identity. `WrapperPresenceResultV1`
never participates in this constructor.

### Explicit active-child input

**ENFORCED by a separate private projection:** Process evidence may enter the
runtime classifier only as:

```text
ActiveChildObservationV1 =
  NOT_EVALUATED(NO_STRICT_RECORD | BINDING_UNPROVEN)
  | NOT_APPLICABLE
  | LIVE_GUARDED(pid, start_guard, discovery_kind)
  | ABSENT
  | UNKNOWN(reason_codes)

ActiveChildRowV1 {
  pid: integer 1..4294967295 | null
  parent_pid: integer 1..4294967295 | null
  start_guard: nonempty NFC UTF-8 string of at most 128 bytes | null
  image_stem: lowercase NFC UTF-8 string of at most 128 bytes | null
  row_failures: ordered tuple[
    PID_INVALID | PARENT_INVALID | START_UNREADABLE | IMAGE_UNREADABLE
  ]
}
```

Its inputs are the strict current runtime record, supervisor-managed identity,
configured brain matcher/launcher-self policy, and the immutable raw
`ProcessObservationV1`, plus the checked `ChildEstablishmentGuardV1` derived
only from that same strict turn. Its forbidden inputs are heartbeat,
`WrapperPresenceResultV1`, `TargetabilityProofV1`, and every presence
reason/action.

- `NOT_EVALUATED` exists when no strict current record or no proven
  runtime/managed binding exists. It contributes no child reason, allowing the
  absent/invalid/unsupported/binding runtime predicate to decide.
- `NOT_APPLICABLE` exists only for a strict bound non-active runtime phase.
- `LIVE_GUARDED` requires exactly one live matching child, positively bound to
  the runtime-declared launcher/turn lineage with a matching PID/start guard,
  and no additional matching unreadable or ambiguous candidate. PID is an
  integer in `1..4294967295`, start guard is a nonempty NFC UTF-8 string of at
  most 128 bytes, and `discovery_kind` is `MATCHED_DESCENDANT` or
  `LAUNCHER_SELF`.
- `ABSENT` requires a complete available snapshot, no matching child, and no
  ambiguous matching candidate.
- Every other result is `UNKNOWN`, with a deduplicated closed reason tuple in
  this order: `SNAPSHOT_UNAVAILABLE`, `OBSERVATION_INCONSISTENT`,
  `MATCHER_CONFIG_INVALID`, `LAUNCHER_IDENTITY_INVALID`,
  `LAUNCHER_IDENTITY_MISMATCH`, `CHILD_PID_START_AMBIGUOUS`,
  `MULTIPLE_MATCHES`, `BRAIN_ANCESTRY_AMBIGUOUS`,
  `CHILD_ESTABLISHMENT_OPEN`, `OTHER`.

**ENFORCED constructor, evaluated top to bottom:**

1. No strict record or no strict managed binding returns `NOT_EVALUATED`.
   A strict bound non-active phase returns `NOT_APPLICABLE`.
2. Active phase requires a positive integer `turn_generation`, positive
   `cli_launcher_pid`, nonempty bounded `cli_launcher_start`, and a normalized
   nonempty `brain_pattern` of at most 128 UTF-8 bytes. Invalid launcher fields
   or matcher config return the corresponding `UNKNOWN`.
3. `ProcessObservationV1` carries every raw snapshot row through
   `active_child_rows`. Normalized rows with matching field values collapse. Conflicting
   rows with the same positive PID, or two rows that make parentage cyclic,
   make `active_child_availability=INCOMPLETE` and return
   `UNKNOWN(OBSERVATION_INCONSISTENT)`. Active-child capture
   `UNAVAILABLE/INCOMPLETE` returns `UNKNOWN(SNAPSHOT_UNAVAILABLE or
   OBSERVATION_INCONSISTENT)`; it never returns absence. Presence-only candidate
   inconsistency cannot change this projection.
4. When a row with the launcher PID exists, its start guard must match the
   runtime launcher guard by a literal token match or the existing ISO representation
   tolerance of one millisecond. A mismatch is
   `UNKNOWN(LAUNCHER_IDENTITY_MISMATCH)`. The row may be absent for a forking
   launcher; direct parent references to its recorded PID remain usable.
5. A row is a positive descendant candidate only when its known image stem
   contains the case-folded pattern; it has positive PID and start guard; it is
   not the launcher; its unique parent chain reaches the recorded launcher PID;
   and its start compares in the same supported scheme no earlier than the
   launcher start (one-millisecond ISO tolerance). A launcher row is a positive
   self candidate only when `allow_launcher_self=true`, its guard matches, and
   its image matches.
6. A matching-image row that fails PID/start/ancestry proof is ambiguous. An
   image-unreadable row whose otherwise valid ancestry reaches the launcher is
   also ambiguous. Rows with a known nonmatching image and a parent chain that
   conclusively does not reach the launcher are unrelated.
7. More than one positive candidate emits `UNKNOWN(MULTIPLE_MATCHES)`. Any
   ambiguous candidate emits the applicable PID/start reason plus
   `BRAIN_ANCESTRY_AMBIGUOUS`. Exactly one positive candidate and no ambiguous
   candidate emits `LIVE_GUARDED`. Zero positive/ambiguous matching candidates
   under complete capture emits `UNKNOWN(CHILD_ESTABLISHMENT_OPEN)` while the
    same-turn `ChildEstablishmentGuardV1` is `OPEN`, and emits `ABSENT`
   only after that guard is `CLOSED`. The `OTHER` defensive branch catches any
   representation not matched above.

All matching and ambiguity predicates are evaluated so `UNKNOWN.reason_codes`
contains every applicable code in the displayed order. The constructor never
uses wrapper-presence relevance, ownership, or targetability. These subreasons
are bounded operator diagnostics; all normalize to the single semantic runtime
reason `CURRENT_UNKNOWN_ACTIVE_CHILD` and do not independently alter the
banked `RecoveryConditionFingerprintV1` payload.

The runtime mapping is closed:

- `UNKNOWN` contributes `CURRENT_UNKNOWN_ACTIVE_CHILD`;
- `LIVE_GUARDED` feeds the existing progress/stall predicates;
- `ABSENT` feeds same-wrapper/turn child-death confirmation—the first
  qualifying poll contributes `CURRENT_UNKNOWN_ACTIVE_CHILD`, while the second
  consecutive qualifying poll contributes `CURRENT_TEARDOWN_PROOF`; and
- `NOT_EVALUATED` and `NOT_APPLICABLE` contribute no active-child reason.

The presence and active-child projections may consume the same immutable raw
capture but may not consume each other's result. Process-row order, field-matching
duplicates, and candidates unrelated to the runtime launcher lineage and brain
matcher cannot change `ActiveChildObservationV1`; relevant lineage evidence
may change it. This is the permitted snapshot-to-runtime flow used by
`_wrapped_liveness` (`src/agenttalk/supervisor.py:3379-3484`).

### Classifier continuity state

**SPECIFIED as pure equations whose constructor and execution are private to
task #115's checked RMW transaction:**

```text
RuntimeContinuityStateV1 =
  NO_BASELINE
  | BASELINE {
      wrapper_generation: bounded NFC string
      turn_generation_high_water: nonnegative integer
      phase: closed runtime phase
      progress_sequence_high_water: nonnegative integer
      progress_seen_epoch: finite nonnegative Unix seconds
      regression_latched: bool
    }

ConsecutiveEvidenceV1 {
  count: integer 0..2
  basis_digest: Hex64 | null
  last_capture_id: CaptureIdV1 | null
}

ChildEstablishmentKeyV1 {
  state_epoch: lowercase hyphenated UUID
  managed_generation: bounded NFC string
  runtime_wrapper_generation: bounded NFC string
  turn_generation: strict positive integer
  cli_launcher_pid: integer 1..4294967295
  cli_launcher_start_guard: nonempty NFC UTF-8 string of at most 128 bytes
}

ChildEstablishmentGuardV1 =
  NOT_APPLICABLE
  | OPEN {
      key: ChildEstablishmentKeyV1
      active_record_updated_at_epoch_ms: uint64 Unix milliseconds
      active_age_grace_through_epoch_ms: uint64 Unix milliseconds
      generation_launch_grace_until_epoch_ms: uint64 Unix milliseconds | null
    }
  | CLOSED {
      key: ChildEstablishmentKeyV1
      result: ADAPTER_PROGRESS | NONRENEWABLE_GRACE_EXPIRED
      active_record_updated_at_epoch_ms: uint64 Unix milliseconds | null
      active_age_grace_through_epoch_ms: uint64 Unix milliseconds | null
      generation_launch_grace_until_epoch_ms: uint64 Unix milliseconds | null
      close_evidence_epoch_ms: uint64 Unix milliseconds
}
```

Guard `NOT_APPLICABLE` means that no strict active establishment key has yet
been accepted in the current runtime-wrapper/high-water-turn binding. It is
not written merely because a later record for that same binding is non-active.

When embedded in a canonical hash payload, the closed variant serializes as the
following `CanonicalJsonV1` object:

```text
{
  "active_age_grace_through_epoch_ms": <uint64 | null>,
  "active_record_updated_at_epoch_ms": <uint64 | null>,
  "close_evidence_epoch_ms": <uint64>,
  "generation_launch_grace_until_epoch_ms": <uint64 | null>,
  "key": <ChildEstablishmentKeyV1>,
  "result": "ADAPTER_PROGRESS" | "NONRENEWABLE_GRACE_EXPIRED",
  "variant": "CLOSED"
}
```

The open/not-applicable variants never enter an owned-childless hash payload.

```text
RecoveryReservationV1 {
  reservation_id: lowercase hyphenated UUID
  origin: AUTOMATIC | MANUAL_AUTHORIZED
  request_id: RequestId | null
  marker_revision_sha256: Hex64 | null
  authority_id: Hex64
  authorization_snapshot_id: Hex64 | null
  execution_gate_snapshot_id: Hex64
  candidate: KILL_THEN_RELAUNCH | RELAUNCH_ONLY
  authority_case:
    STRICT_RUNTIME_TEARDOWN | PROVABLY_CHILDLESS_OWNED_WRAPPER
    | CONFIRMED_ABSENCE | MANUAL_TARGETS
  evidence_id: Hex64
  prior_guarded_identity_digest: Hex64 | null
}

ConfiguredActionIssuerCheckpointV1 {
  checkpoint_id: lowercase hyphenated UUID
  agent_key: source-equal to checked ClassifierStateV1.agent_key
  state_epoch: source-equal to checked ClassifierStateV1.state_epoch
  committed_revision: source-equal to the successor revision that persists PRE_BARRIER
  configured_action: KILL_THEN_RELAUNCH | STUCK_RECOVERY
  reservation_id: source-equal to RecoveryReservationV1.reservation_id
  authority_id: source-equal to RecoveryReservationV1.authority_id
  checked_reservation_transition_id: lowercase hyphenated UUID
  barrier_state_identity:
    source-equal to the persisted configured planner barrier-state identity
  source_targets:
    deeply immutable ordered module-typed #120 configured target tuple of length 1..64
    source-equal to that persisted barrier state's target tuple
  target_digest: module OwnedTargetDigestV1 computed over source_targets
  issuer_supervisor_instance_token_digest:
    source-equal to the ExecutionGateCaptureV1 token digest
  issuer_supervisor_pid:
    source-equal to the ExecutionGateCaptureV1 guarded PID
  issuer_supervisor_start_guard:
    source-equal to the generic ExecutionGateCaptureV1 guarded start
  action_latch_epoch:
    source-equal to the enabled ExecutionGateCaptureV1 action epoch
  invariant:
    issuer_supervisor_start_guard is a ProcStartGuardV1-shaped audit value,
    not OwnedExactStartGuardV1 and not evidence produced by
    ExactIssuerIdentityAdapterV1
}

ExactIssuerIdentityAdapterV1 =
  named undelivered implementation dependency for a separately reviewed
  issuer-identity decision when issuer_supervisor_pid is presently occupied;
  this revision defines no platform identity schema, provider binding,
  serialization, comparator, or RECYCLED constructor for that adapter

ConfiguredPreBarrierRetrySuccessorV1 =
  named undelivered implementation seam that may later define one separately
  reviewed checked retry transition from a definitively owner-dead configured
  PRE_BARRIER checkpoint; no V1 decoder, reducer, constructor, or adapter in
  this revision implements or may emulate that transition

ConfiguredPreBarrierOwnerLossHoldV1 {
  reason: CONFIGURED_PRE_BARRIER_OWNER_LOST
  agent_key: source-equal to current ClassifierStateV1.agent_key
  state_epoch: source-equal to current ClassifierStateV1.state_epoch
  current_revision: source-equal to current ClassifierStateV1.revision
  reservation_id: source-equal to the current RESERVED reservation ID
  issuer_checkpoint: source-equal to current ConfiguredActionIssuerCheckpointV1
  issuer_extinction:
    PROVED_GONE {
      observation_id: lowercase hyphenated UUID
      queried_pid: source-equal to issuer_supervisor_pid
      result: GONE
    }
    | LIVE_OR_UNPROVEN {
        reasons: ordered nonempty tuple[
          ISSUER_PID_PRESENT_IDENTITY_UNRESOLVED | OBSERVATION_UNAVAILABLE
        ] in displayed order
      }
  present_pid_reuse_disposition:
    CAPABILITY_UNAVAILABLE(ExactIssuerIdentityAdapterV1)
  effect_disposition: PRIOR_EFFECT_UNKNOWN
  source_hash: sha256(
    UTF8("configured-pre-barrier-owner-loss-hold/v1\0")
    || CanonicalJsonV1({
         "agent_key": agent_key,
         "current_revision": current_revision,
         "effect_disposition": "PRIOR_EFFECT_UNKNOWN",
         "issuer_checkpoint": issuer_checkpoint,
         "reason": "CONFIGURED_PRE_BARRIER_OWNER_LOST",
         "reservation_id": reservation_id,
         "state_epoch": state_epoch
       })
  )
  invariant: source_hash excludes issuer_extinction, present_pid_reuse_disposition,
             attended_action, and source_hash itself; LIVE_OR_UNPROVEN and
             PROVED_GONE projections of one byte-identical checkpoint therefore
             have matching source_hash values
  invariant: PROVED_GONE requires a fresh independent OS result that the PID is
             absent; a present PID always remains LIVE_OR_UNPROVEN in this
             revision, even when generic token/start values differ
  automatic_exit: UNAVAILABLE(ConfiguredPreBarrierRetrySuccessorV1)
  attended_action:
    READY(ATTENDED_CONFIGURED_PRE_BARRIER_DISPOSITION)
    | BLOCKED_ISSUER_LIVE_OR_UNPROVEN
}

ConfiguredPriorEffectUnknownFenceV1 {
  disposition_id: lowercase hyphenated UUID
  disposition_request:
    source-equal to the complete AttendedConfiguredPreBarrierDispositionRequestV1 input, including
    request ID, expected hold source hash, actor, acknowledgements, and reason
  source_checkpoint:
    source-equal to the disposed ConfiguredActionIssuerCheckpointV1
  source_targets:
    source-equal to module-typed source_checkpoint.source_targets; its digest is
    source-equal to source_checkpoint.target_digest
  disposed_state_epoch: source-equal to checked ClassifierStateV1.state_epoch
  disposed_revision: source-equal to the successor revision that installs this fence
  disposed_ordinary_poll_sequence:
    source-equal to the ordinary poll sequence at disposition
  freshness_floor_state_epoch: lowercase hyphenated UUID
  freshness_floor_after_ordinary_poll_sequence: uint64
  effect_disposition: PRIOR_EFFECT_UNKNOWN
  launch_requirement:
    FRESH_120_POST_KILL_BARRIER_THEN_TASK_57_BEFORE_CONFIGURED_LAUNCH
}

CommittedOrdinaryFenceCaptureUseOwnerV1 =
  one private, nonserializable, noncopyable atomic owner cell created by #115
  for exactly one successful ordinary-observation commit while a prior-effect
  fence is current; it is unreachable from every immutable witness/receipt graph
  and has closed states READY | ADAPTING | RECEIPT | COMMITTING | CLOSED
  | POISONED

CommittedOrdinaryFenceCaptureV1 =
  private, nonserializable, noncopyable, deeply immutable, alias-free,
  single-use post-commit witness published only after one winning #115 ordinary
  observation commit durably installs its successor; its opaque handle pairs
  the immutable value with an unexported reference to the bound use owner {
    fence_disposition_id: source-equal to committed successor fence_disposition_id
    agent_key: source-equal to committed successor agent_key
    state_epoch: source-equal to committed successor state_epoch
    committed_revision: source-equal to committed successor revision
    capture_id: source-equal to the begin-bound CaptureIdV1 that won that commit
    source_checkpoint_id: source-equal to committed successor fence checkpoint ID
    source_target_digest: source-equal to committed successor fence target digest
    source_targets: source-equal to committed successor fence source_targets
    sealed_capture:
      immutable raw and owned-tree observation operands source-equal to those in the
      winning OrdinaryClassifierObservationReceiptV1 for #120 deny-only checking
    invariant: a stale/CAS-losing observation lineage publishes no witness;
               a prospective capture ID without the winning committed revision
               cannot construct or substitute this value
  }

ConfiguredPriorEffectFenceBarrierReceiptV1 =
  private, nonserializable, noncopyable, deeply immutable, alias-free receipt
  minted only by #115's unexported fence-barrier reducer after the bound
  CommittedOrdinaryFenceCaptureV1 handle wins READY -> ADAPTING and the reducer
  matches its complete binding to the current ConfiguredPriorEffectUnknownFenceV1 {
    fence_disposition_id: source-equal to witness fence_disposition_id
    agent_key: source-equal to witness agent_key
    state_epoch: source-equal to witness state_epoch
    expected_revision: source-equal to witness committed_revision
    capture_id: source-equal to witness capture_id
    source_checkpoint_id: source-equal to witness source_checkpoint_id
    source_target_digest: source-equal to witness source_target_digest
    source_targets: source-equal to witness source_targets
    barrier_result: CLEAR | BLOCKED | AMBIGUOUS | UNAVAILABLE
    invariant: CLEAR proves every module-typed source target and descendant absent or
               recycled under #120's deny-only identity/coverage rules
  }

ConfiguredPriorEffectFenceBarrierReceiptCustodyV1 =
  private, nonserializable, noncopyable opaque handle yielded with exactly one
  ConfiguredPriorEffectFenceBarrierReceiptV1 after the reducer fully constructs
  both and atomically moves the witness use owner ADAPTING -> RECEIPT; it carries
  that sealed receipt plus the unexported owner reference. Aliases may exist, but
  only one RECEIPT -> COMMITTING compare-and-swap may win before any clearance
  result or mutation is derived

NonChildlessRecoveryExecutionV1 =
  RESERVED {
    reservation: RecoveryReservationV1
    phase: PRE_BARRIER | SPAWN_IN_FLIGHT
    configured_action_issuer_checkpoint:
      ConfiguredActionIssuerCheckpointV1 | null
    spawned_guard: SpawnGuardV1 | null
    pending_attempt_deadline_epoch: finite nonnegative Unix seconds | null
  }
  | AMBIGUOUS_LAUNCH {
      reservation: the complete non-childless RESERVED value normalized with
                   phase = SPAWN_IN_FLIGHT and
                   pending_attempt_deadline_epoch = null
      ambiguity_boundary_poll_sequence: uint64
      evidence: AmbiguousLaunchEvidenceV1
    }

ChildlessRecoveryExecutionV1 =
  IDLE
  | RESERVED {
      reservation: RecoveryReservationV1
      childless_evidence: module ChildlessReservationEvidenceV1
      phase:
        PRE_BARRIER | TREE_CLOSURE_ACQUIRING | TREE_CLOSURE_HELD
        | TREE_CLOSURE_RELEASING | TEARDOWN_IN_FLIGHT | SPAWN_IN_FLIGHT
      childless_attempt_id: lowercase hyphenated UUID | null
      childless_attempt_revision: uint64 | null
      childless_closure_id: lowercase hyphenated UUID | null
      childless_pending_disposition:
        CLOSURE_VETOED | COMPLETE_GONE | SAME_OWNER_SURVIVED
        | MEMBER_SURVIVED | EFFECT_UNPROVEN | null
      spawned_guard: SpawnGuardV1 | null
      pending_attempt_deadline_epoch: finite nonnegative Unix seconds | null
    }
  | AMBIGUOUS_LAUNCH {
      reservation: the complete childless RESERVED value normalized with
                   phase = SPAWN_IN_FLIGHT and
                   childless_attempt_id = null and
                   childless_attempt_revision = null and
                   childless_closure_id = null and
                   childless_pending_disposition = null and
                   pending_attempt_deadline_epoch = null
      ambiguity_boundary_poll_sequence: uint64
      evidence: AmbiguousLaunchEvidenceV1
    }

RecoveryExecutionStateV1 =
  IDLE
  | NON_CHILDLESS(NonChildlessRecoveryExecutionV1)
  | CHILDLESS(module ChildlessEffectEnvelopeV1)

ManualReadinessStateV1 =
  NONE
  | APPLIED_PENDING_READINESS {
      request_id: RequestId
      marker_revision_sha256: Hex64
      committed_managed_generation: bounded NFC string
    }

SpawnGuardV1 {
  pid: integer 1..4294967295
  start_guard: nonempty NFC UTF-8 string of at most 128 bytes
  launch_reservation_id: lowercase hyphenated UUID
}

GuardedLaunchCommitV1 =
  nonserializable inert strict-checkpoint input {
    spawn_guard: SpawnGuardV1
    managed_generation: bounded NFC string
    launch_grace_until: finite nonnegative Unix seconds
  }

AmbiguousLaunchEvidenceV1 {
  code: START_RETURNED_WITHOUT_GUARD | IDENTITY_COMMIT_FAILED
        | CRASHED_DURING_SPAWN
  observed_guard: SpawnGuardV1 | null
  first_seen_epoch: finite nonnegative Unix seconds
}

ClassifierStateV1 {
  state_epoch: lowercase hyphenated UUID
  revision: uint64
  agent_key: NFC canonical agent/root string
  managed_generation: bounded NFC string | null
  first_managed_epoch: finite nonnegative Unix seconds
  launch_grace_until: finite nonnegative Unix seconds | null
  launching: bool
  readiness_seen: bool
  ordinary_poll_sequence: uint64
  next_capture_ordinal: uint32 in 1..65536
  recovery_poll_terminal_sequence: uint64 | null
  runtime_continuity: RuntimeContinuityStateV1
  child_establishment_guard: ChildEstablishmentGuardV1
  child_dead_confirmation: ConsecutiveEvidenceV1
  child_stall_confirmation: ConsecutiveEvidenceV1
  owned_childless_confirmation: module OwnedChildlessConfirmationV1
  absence_confirmation: AbsenceConfirmationStateV1
  consumed_manual_request_ids: bounded ordered set
  recovery_execution: RecoveryExecutionStateV1
  configured_prior_effect_unknown_fence:
    ConfiguredPriorEffectUnknownFenceV1 | null
  state_loss_quarantine: module StateLossQuarantineV1
  manual_readiness: ManualReadinessStateV1
}
```

Null `managed_generation` requires `launch_grace_until=null`,
`launching=false`, and `readiness_seen=false`. `launching=true` requires a
non-null managed generation, non-null launch-grace deadline, and
`readiness_seen=false`. `readiness_seen=true` requires a non-null managed
generation and `launching=false`; `false/false` remains valid before a launch,
after a positively proven no-spawn failure, or while readiness for the current
generation has not been observed. Only the named launch and
matching-generation readiness transitions below may change these fields.

`recovery_poll_terminal_sequence` is null or at most
`ordinary_poll_sequence`. Every finalized childless reservation/attempt
outcome and every permit-bound debt reconciliation from ordinary residual
evidence writes the current
`ordinary_poll_sequence` there in the same checked transaction. Pure refusal,
retained closure uncertainty, prior-poll exhaustion, and no-op
   `NOT_ATTEMPTED` results that leave childless debt/cycle/execution byte-identical do
not. Successful permit-bound ordinary-evidence or reload cleanup also exports
`NOT_ATTEMPTED`, but it is a finalized reconciliation and does write the
terminal. It is
logically clear when the next ordinary poll increments
`ordinary_poll_sequence`; the increment transaction sets it to null. This is a
persisted same-poll terminal, not a timer or a cross-poll hold.

For `origin=AUTOMATIC`, `request_id`, `marker_revision_sha256`, and
`authorization_snapshot_id` are null. For `origin=MANUAL_AUTHORIZED`, all
three are non-null. Any other pairing is invalid state and holds recovery.
Automatic origin forbids `MANUAL_TARGETS`; manual origin uses
`PROVABLY_CHILDLESS_OWNED_WRAPPER` for that overlap, `CONFIRMED_ABSENCE` for
no-kill, and `MANUAL_TARGETS` otherwise.

`NON_CHILDLESS` forbids
`authority_case=PROVABLY_CHILDLESS_OWNED_WRAPPER`. Its execution value owns no
module binding, debt, cycle, continuation, or retired attempt. `CHILDLESS`
requires `ChildlessEffectEnvelopeV1` and the envelope's execution is either
`IDLE`, a `RESERVED` value whose authority case is
`PROVABLY_CHILDLESS_OWNED_WRAPPER`, or the matching `AMBIGUOUS_LAUNCH` value.
Its required `childless_evidence` satisfies the module's mode/nullability rules
and its `authority_id` is source-equal to `evidence_id`. For automatic origin
`authority_id` is also source-equal to that module ID; for manual origin it remains the
distinct manual authority ID. No childless execution, debt, cycle,
continuation, or retired-attempt value exists outside that one envelope.

A kill-bearing configured `NON_CHILDLESS/RESERVED/PRE_BARRIER` value requires
one non-null `configured_action_issuer_checkpoint` minted and persisted by the
same #115 checked transition as the reservation. It source-binds that
reservation, complete source target tuple/digest, successor revision, and current supervisor
identity/action epoch. A no-kill `PRE_BARRIER`, every `SPAWN_IN_FLIGHT`, and the
nested normalized reservation inside `AMBIGUOUS_LAUNCH` require the field null.
Normal receipt/barrier completion clears the checkpoint in the same checked
transition that leaves `PRE_BARRIER`; no caller may clear or replace it alone.

The checkpoint is evidence, not custody. While its source-bound live transient action
owner remains available, that owner alone may advance the configured action.
After reload, hard cancellation, or any failure to validate that custody,
persisted fields cannot reconstruct the owner and
`ConfiguredPreBarrierOwnerLossHoldV1` is the unique derived action hold. Its
`issuer_extinction` is `PROVED_GONE` only from a fresh independent OS result
that the source-equal issuer PID is absent. A present PID always remains
`LIVE_OR_UNPROVEN`: generic token/start equality or mismatch cannot decide
whether that process is the issuer or a recycled occupant. Reuse classification
is `CAPABILITY_UNAVAILABLE(ExactIssuerIdentityAdapterV1)` and produces no
`RECYCLED` disposal result.
Both forms render the specified remedy, but only definitive `PROVED_GONE`
makes the attended disposition `READY`. Neither
case permits automatic `PRE_BARRIER_RELEASE`, a second `RESERVE`, or a custody
remint. The derived hold is not persisted state and is not the later
`ConfiguredPriorEffectUnknownFenceV1`: the checked attended disposition consumes
the proved form, clears the abandoned execution, and installs that durable
fence.

Every trustworthy initial classifier genesis initializes
`configured_prior_effect_unknown_fence=null`. The attended disposition requires
that field null and initializes the fence's freshness floor to the disposition
epoch/current ordinary sequence. While the fence is non-null, every configured
and childless reservation, kill, spawn arm, `Start-Process`, archive, identity
commit, and authority-enabling/effect-owned mutation is blocked. Therefore a
second owner-loss disposition cannot replace or collapse the first obligation,
and no childless origin can bypass it. Ordinary observation and every mutation
other than capture rollover and `PRIOR_EFFECT_FENCE_CLEAR` preserve the complete
fence byte-identically.
State validity requires a non-null fence to pair only with top-level
`recovery_execution=IDLE`; any persisted pairing with `NON_CHILDLESS` or
`CHILDLESS` is an invalid fence and permits no clearance, reservation, or effect.

After the attended disposition commits, the operator removes `supervisor.kill`
and starts exactly one current supervisor. The still-non-null fence makes that
restart observation-only: every reservation, mutation other than ordinary
observation/fence clearance, effect, archive, identity commit, and launch remains
blocked. This explicit re-enable step may establish the narrower
`PriorEffectFenceClearanceEligibilityV1.ELIGIBLE`; clearance cannot run while
the kill switch is active or the current supervisor cannot be proved. It does
not assert global `ExecutionEligibilityV1.ELIGIBLE`.

Only the winning #115 ordinary-observation commit may publish one
`CommittedOrdinaryFenceCaptureV1`, and only when its durably installed successor
still carries the complete current fence. The witness capture ID must be in the
source-equal freshness-floor epoch with a sequence strictly greater than the floor.
A same-predecessor CAS loser publishes no witness even if its prospective
capture ID equals the winner's; knowing that ID or retaining its sealed capture
is inert. #115's unexported fence-barrier reducer consumes the witness once and
applies merged #120's deny-only identity/coverage rules over its sealed capture
and the module-typed source targets. It yields one
`ConfiguredPriorEffectFenceBarrierReceiptCustodyV1` whose receipt binds the
winning successor revision.

`PRIOR_EFFECT_FENCE_CLEAR` is a private #115 checked transition from top-level
`IDLE`; it accepts only that custody handle and evaluates one closed admission
order while holding the lifecycle and checked-store locks. After validating the
private receipt seal, it first races the owner `RECEIPT -> COMMITTING`. Only the
winner may read state or derive any clearance result; an alias or replay whose
owner is already `COMMITTING`, `CLOSED`, or `POISONED` returns
`REJECTED_REPLAY_ZERO_EFFECT` with `NOT_READ_BY_LOSER`. The winner next validates
the official checked store. Missing, corrupt, quarantined, or otherwise
untrustworthy state cannot supply a current fence: it closes the owner and
returns `POLICY_HELD_STATE_PROVENANCE_LOST`. With a trustworthy store, each
missing-fence, stale epoch/revision, wrong disposition/source, or below-floor
capture predicate selects exactly one
`PriorEffectFenceClearanceStateRejectionV1`; the owner closes and the result is
`REJECTED_STALE_OR_MISMATCHED`. It next evaluates
`PriorEffectFenceClearanceEligibilityV1`. `DRY_RUN`, `KILL_SWITCH_ACTIVE`, or
`SUPERVISOR_STOPPED` closes the owner and returns the corresponding
`POLICY_HELD_GATE` variant. It then closes a `BLOCKED`, `AMBIGUOUS`, or
`UNAVAILABLE` receipt and returns its corresponding `POLICY_HELD_BARRIER` variant. Every
deterministic rejection therefore consumes custody once and requires a fresh
committed capture; no external registry or caller mutex supplies single use.

Only a matching `CLEAR` winner may derive the checked clear mutation. It holds the
kill-switch/current-supervisor read guard through the state compare-and-swap;
normal commit closes the owner and returns `CLEARED`. A failure positively
proved to precede the state CAS closes the owner and returns
`FAILED_PROVED_NO_COMMIT` with the byte-identical current fence. Once that state CAS may
have run, an exception, crash, or lost response returns or reconciles as
`CLEAR_COMMIT_OUTCOME_UNKNOWN`; it must not claim that the fence was preserved.
Custody is `POISONED` if uncertainty is observed while its owner remains
reachable, `CLOSED_RESPONSE_LOST` if normal close preceded response loss, or
`OWNER_LOST_WITH_PROCESS` after a process crash. A subsequent trustworthy
checked read derives exactly one
`ConfiguredPriorEffectFenceClearanceReconciliationV1`: `FENCE_STILL_CURRENT`
requires a fresh committed capture, `FENCE_CLEARED` permits normal selection
only through full `ExecutionEligibilityV1`, and untrustworthy state requires
attended provenance handling. A gate flip after the successful state CAS does
not undo clearance, but it still blocks later action through the full gate.
Every non-effect result constructs no action custody, kill, launch, archive, or
other effect. The
operator-facing remedy says to remove the kill switch, start one current
supervisor, obtain the winning committed source-bound barrier-receipt custody,
and, if a source-bound #120 target remains, handle that target attended while the
global fence stays present before retrying clearance.
Stopping a wrapper before disposing its dead issuer is not presented as a
remedy. Once clearance commits, normal selection may start from new evidence;
task #57's durable singleton remains independently mandatory before any
configured launch.

Capture rollover never erases the obligation. A null fence remains null. A
non-null fence preserves its disposition/audit, source checkpoint/targets,
effect disposition, and launch requirement byte-identically while atomically
rebasing only `freshness_floor_state_epoch` to the fresh rollover epoch and
`freshness_floor_after_ordinary_poll_sequence` to zero. Thus the first new
winning committed ordinary capture may satisfy freshness without comparing
sequence numbers from unrelated epochs; an old-epoch barrier can never clear
the rebased fence.

Within the envelope, `childless_attempt_id` and
`childless_attempt_revision` are either both null or both non-null.
`PRE_BARRIER` requires that pair, `childless_closure_id`, and
`childless_pending_disposition` null, plus null spawned guard and deadline.
`TREE_CLOSURE_ACQUIRING` requires the attempt pair non-null, the closure ID and
pending disposition null, and null spawned guard/deadline.
`TREE_CLOSURE_HELD` requires the attempt pair and closure ID non-null, pending
disposition null, and a previously valid joined module closure value with the
same acquisition/closure IDs. `TEARDOWN_IN_FLIGHT` has the same ID/null
shape. `TREE_CLOSURE_RELEASING` requires the attempt pair, closure ID, and
pending disposition non-null. All four phases require null spawned
guard/deadline.

The envelope's `continuation_owner` is non-`NONE` while a typed external call is
armed or its receipt is being applied, including after the original transient
effect-guard holder dies. A live continuation must own that guard; a detached
persisted owner is an inert tombstone, not an executable capability. Its typed
subject closes the phase pairing:

The module-defined `TAKEOVER_CHECKPOINT` is the sole intermediate pairing.
It requires `role=RECONCILER`, `takeover_origin=FROM` with the source-equal immediate
predecessor continuation ID/operation/stage, the current operation source-equal
to that predecessor operation, and a byte-identical no-call takeover mapping.
No external adapter accepts it. A later operation-specific
`STATE_MUTATION` permit must replace it with the module table's specified next arm
before any call, and that arm/`CALL_RETURNED` checkpoint retains the origin.
`role=ISSUER` instead requires `takeover_origin=NONE`; every noncheckpoint
`RECONCILER` pairing requires the retained origin. The phase-specific pairings
below apply otherwise.

- `ACTIVE_ATTEMPT` is source-equal to the reservation/attempt/provider tuple and is
  the only subject for closure acquire/reconcile/release, `STOP_TREE`,
  and `POST_ACTION_CAPTURE`.
- `TREE_CLOSURE_ACQUIRING` admits issuer `CLOSURE_ACQUIRE` or table-authorized
  reconciler `CLOSURE_RECONCILE`, each only at `ARMED`/`CALL_RETURNED`.
  `TREE_CLOSURE_HELD` admits the matching acquire/reconcile `CALL_RETURNED`
  checkpoint and its permit-bound next arm. A live issuer may arm `STOP_TREE`
  or release; a takeover reconciler may reconcile and, after matching `HELD`,
  may arm release but never teardown.
- `TREE_CLOSURE_RELEASING` admits the matching acquire/reconcile/post-capture
  `CALL_RETURNED` checkpoint, table-authorized reconciler
  `CLOSURE_RECONCILE/ARMED`, or `CLOSURE_RELEASE` at
  `ARMED`/`CALL_RETURNED`; no other continuation is valid.
- `TEARDOWN_IN_FLIGHT` admits issuer `STOP_TREE` at
  `ARMED`/`CALL_RETURNED`. Matching `STOP_TREE/CALL_RETURNED` may be replaced by
  `POST_ACTION_CAPTURE/ARMED`; its receipt moves to releasing. After takeover
  of that returned call, only reconciler `CLOSURE_RECONCILE` may be
  `ARMED`/`CALL_RETURNED`; matching `HELD` may next arm
  `POST_ACTION_CAPTURE`. Matching `RELEASED` instead finalizes conservatively as
  `EFFECT_UNPROVEN` without another external capture. Those capture arms and
  returned checkpoints require
  the matching permit/receipt lineage and cannot arise from `STOP_TREE/ARMED`.
- `RETIRED_ATTEMPT` is source-equal to one retained tombstone. It is permitted while
  envelope execution is `IDLE` only for `RETIRED_ATTEMPT_RECONCILE`, its
  `CALL_RETURNED` checkpoint, or matching `CLOSURE_RELEASE` of that tombstone's
  `RELEASE_PENDING` closure.
- `SPAWN_RESERVATION` is source-equal to the childless reservation and is required
  for `SPAWN/ARMED` while execution is `SPAWN_IN_FLIGHT`. A typed spawn receipt
  or the closed positive-dead-issuer transition clears/replaces it before
  `AMBIGUOUS_LAUNCH`; that phase requires continuation `NONE`.

`PRE_BARRIER` requires continuation `NONE`. Childless `IDLE` requires `NONE`
except for the matching retired-attempt cleanup above, and every non-childless
state requires `NONE`. Every other phase/subject/operation/stage/origin pairing
is invalid and `POLICY_HELD`.

The attempt revision is the post-commit state revision immediately before
invoking closure-successor acquisition. Automatic origin in any childless
closure/teardown phase requires the envelope's matching `ACTIVE/ISSUED` cycle
with the same attempt ID/revision until verified release finalizes the outcome.
`TREE_CLOSURE_ACQUIRING` and `TREE_CLOSURE_HELD` require no debt current attempt
for this revision. `TEARDOWN_IN_FLIGHT` requires envelope debt with
`current_attempt_id=childless_attempt_id`,
`current_attempt_revision=childless_attempt_revision`, and
`last_outcome=ISSUED`. A releasing disposition other than
`CLOSURE_VETOED` requires that same debt/current-attempt equality;
`CLOSURE_VETOED` requires both debt current-attempt fields null.

The converses are also enforced inside the envelope. Debt current-attempt
fields are non-null if and only if execution is `TEARDOWN_IN_FLIGHT` or
non-veto `TREE_CLOSURE_RELEASING`, and their pair is source-equal to execution. A
cycle `last_outcome=ISSUED` exists if and only if execution is an automatic
named childless phase in `{TREE_CLOSURE_ACQUIRING, TREE_CLOSURE_HELD,
TREE_CLOSURE_RELEASING, TEARDOWN_IN_FLIGHT}`; its owner and last attempt pair
source-equal execution. An existing `EXHAUSTED` cycle has exactly three issued
attempts and a typed failure outcome. An `ACTIVE` cycle with a typed failure
has only one or two issued attempts; `ACTIVE/ISSUED` may have one, two, or
three. Beginning the next automatic attempt permits only `NONE -> attempt 1`
or same-owner `ACTIVE/failure(n) -> ACTIVE/ISSUED(n+1)`. Every other envelope
shape is invalid and `POLICY_HELD`.

The envelope's `retired_attempts` receives a
`BoundRetiredChildlessAttemptV1` in the same checked transaction that finally
releases its reservation. No external adapter accepts a raw retired ID.
Its attempt revision and provider version are source-equal to those in its binding and
are retained with typed cleanup state. Reconcile requires a `STATE_MUTATION` permit that installs
a `RETIRED_ATTEMPT/RETIRED_ATTEMPT_RECONCILE/ARMED` continuation bound to that retired attempt, a
fresh post-CAS `EXTERNAL_CALL` permit and typed call, then a fresh matching
  receipt-derived `RECEIPT_MUTATION` permit whose call-issuance binding is
  source-equal to the corresponding typed receipt.
  `NEVER_ACQUIRED`/`RELEASED` restores terminal state
  and clears the continuation; unexpected `HELD` persists
  `RELEASE_PENDING(closure_id)` with the reconcile continuation at
  `CALL_RETURNED`. A later `CLOSURE_RELEASE/STATE_MUTATION` permit replaces that
  checkpoint with typed `CLOSURE_RELEASE/ARMED` bound to that retired attempt; its release follows the
  same three-stage sequence. A crash leaves the typed
continuation and cleanup state inert for guarded takeover/reconciliation.
Without each fresh permit, the envelope is retained byte-identically and no call
occurs; ordinary observation bookkeeping outside the envelope may still
advance. Eviction follows checked commit-revision order and is permitted only
by the same permit-bound finalization transaction that inserts a new terminal
attempt, when the evicted tombstone is `TERMINAL` and owns no continuation.
There is no background or maintenance eviction; otherwise the set is full and
named recovery holds.

Every normal or reload result first requires the returned acquisition ID to be
source-equal to the persisted attempt ID. In `TREE_CLOSURE_ACQUIRING`, the first
well-formed `HELD`/`RELEASED` may bind its non-null closure ID in the same
checked transition; reload binding is release-only. After that binding, every
`HELD`/`RELEASED` must be source-equal to the persisted pair. Null, mismatch,
conflict, or unreadable reconciliation is `UNKNOWN`, keeps the phase,
reservation, debt, cycle, and current-attempt fields byte-identical, and holds every action.

`SPAWN_IN_FLIGHT` requires a non-null deadline and a null spawned guard. In a
childless envelope it also requires null childless attempt
ID/revision/closure ID/pending disposition. No transition may persist a
returned guard in this phase. A returned guard either commits identity or
moves into `AMBIGUOUS_LAUNCH`, whose nested `reservation.spawned_guard`
is source-equal to `evidence.observed_guard` while its deadline is null.
`IDENTITY_COMMIT_FAILED` requires that shared guard non-null;
`START_RETURNED_WITHOUT_GUARD` and `CRASHED_DURING_SPAWN` require both copies
null. Any other combination holds. A childless `SPAWN_IN_FLIGHT` or
`AMBIGUOUS_LAUNCH` remains inside the same envelope and retains its
`ExactTargetExecutorBindingV1` byte-identically. Manual origin never increments
or failure-updates the
automatic cycle; an origin-neutral successful debt-clear may clear it. The
module validates envelope execution, debt, cycle, continuation, and retired
attempts as one checked value; an invalid pairing selects no action and
resolves `POLICY_HELD`.
Thus automatic and manual launches share the same durable reservation,
in-flight, and ambiguity fence. Manual readiness bookkeeping is orthogonal to
that ownership fence and cannot authorize or block a later recovery.

Task #115 owns the sole observation begin/checked-reduce/commit API. It accepts
one `OrdinaryObservationCommitCustodyV1` over a sealed ordinary-observation
receipt, not a standalone receipt, caller-constructed successor, or arbitrary
field map:

```text
OrdinaryObservationLineageV1 =
  one private, nonserializable, noncopyable, single-use lineage issued by the
  #115 owner at begin, carrying:
    lineage_id
    expected_agent_key
    expected_state_epoch
    expected_revision
    prospective_capture_id: CaptureIdV1 fixed as {
      state_epoch = expected_state_epoch
      agent_key = expected_agent_key
      ordinary_poll_sequence =
        checked predecessor.ordinary_poll_sequence + 1
      capture_ordinal = 0
    }
  invariant: prospective_capture_id is fixed before observer acquisition and
             immutable within this lineage; this lineage cannot replace,
             rebind, or restamp it

OrdinaryObservationLineageOwnerV1 =
  one private, nonserializable, noncopyable atomic owner cell minted exactly
  once with OrdinaryObservationLineageV1 and never reachable from the lineage
  or receipt graph; only the acquisition and commit-custody handles below carry
  its unexported reference;
  its closed states are UNUSED | ACQUIRING | RECEIPT | COMMITTING | CLOSED
  | POISONED

OrdinaryObservationAcquisitionV1 =
  private, nonserializable, noncopyable opaque handle pairing the immutable
  lineage proof with an unexported reference to its bound owner cell; aliases
  may exist, but only one UNUSED -> ACQUIRING compare-and-swap may win

CaptureSequenceRolloverBlockerV1 =
  NON_CHILDLESS_EXECUTION | CHILDLESS_ENVELOPE | CHILDLESS_EXECUTION
  | CHILDLESS_TEARDOWN_DEBT | CHILDLESS_AUTOMATIC_CYCLE
  | CHILDLESS_CONTINUATION | CHILDLESS_RETIRED_ATTEMPT

CaptureSequenceExhaustionV1 {
  code: CAPTURE_SEQUENCE_EXHAUSTED
  agent_key: source-equal to checked ClassifierStateV1.agent_key
  exhausted_state_epoch: source-equal to checked ClassifierStateV1.state_epoch
  exhausted_revision: source-equal to checked ClassifierStateV1.revision
  ordinary_poll_sequence: 18446744073709551615
  rollover_disposition:
    READY
    | BLOCKED(ordered nonempty tuple[CaptureSequenceRolloverBlockerV1])
  required_action: ATTENDED_STATE_EPOCH_ROLLOVER
}

OrdinaryObservationBeginResultV1 =
  READY(OrdinaryObservationAcquisitionV1)
  | ATTENDED_REQUIRED(CaptureSequenceExhaustionV1)

OrdinaryClassifierObservationReceiptV1 =
  private, nonserializable, noncopyable, deeply immutable, alias-free,
  single-use receipt minted only by the actual ordinary observer from one real
  acquisition and one OrdinaryObservationLineageV1, carrying:
    lineage_id
    expected_agent_key
    expected_state_epoch
    expected_revision
    prospective_capture_id: source-equal to lineage.prospective_capture_id
    sealed raw ordinary capture and independent classifier operands,
      with raw capture.capture_id source-equal to prospective_capture_id
  invariant: ordinary_poll_sequence and capture_ordinal occur only inside the
             owner-bound prospective_capture_id and matching raw capture ID;
             neither is caller-settable or restampable
  invariant: contains no caller-settable continuity/guard successor,
             confirmation count, confirmation basis, last/first capture
             successor, CONFIRMED state, or other persisted successor field

OrdinaryObservationCommitCustodyV1 =
  private, nonserializable, noncopyable opaque handle emitted exactly once
  alongside one OrdinaryClassifierObservationReceiptV1; it carries that sealed
  receipt plus an unexported reference to the bound lineage owner. It is outside
  every sealed graph. Aliases may exist, but all name the same RECEIPT state and
  only one RECEIPT -> COMMITTING compare-and-swap may win

PrivateClassifierObservationMutationV1 =
  private, nonserializable, noncopyable, deeply immutable, single-commit result
  derived and applied only inside #115's checked RMW transaction {
  expected_state_epoch: lowercase hyphenated UUID
  expected_revision: uint64
  capture_id: source-equal to receipt.prospective_capture_id
  ordinary_poll_sequence:
    source-equal to capture_id.ordinary_poll_sequence and equal to
    checked predecessor.ordinary_poll_sequence + 1 using checked uint64 addition
  next_capture_ordinal: 1
  recovery_poll_terminal_sequence: null
  runtime_continuity: RuntimeContinuityStateV1
  child_establishment_guard: ChildEstablishmentGuardV1
  child_dead_confirmation: ConsecutiveEvidenceV1
  child_stall_confirmation: ConsecutiveEvidenceV1
  owned_childless_confirmation: module OwnedChildlessConfirmationV1
  absence_confirmation: AbsenceConfirmationStateV1
  invariant: configured_prior_effect_unknown_fence is preserved byte-identically
}

NonChildlessAuthorityTransitionV1 =
  RESERVE | PRE_BARRIER_RELEASE | SPAWN_ARM | NO_SPAWN_COMMIT
  | AMBIGUITY_COMMIT | AMBIGUITY_RESOLVE | IDENTITY_COMMIT
  | OWNER_TRANSITION | READINESS_COMMIT | PRIOR_EFFECT_FENCE_CLEAR

NonChildlessAuthorityDeltaV1 =
  private, nonserializable, noncopyable, deeply immutable, alias-free reducer result {
  expected_state_epoch: lowercase hyphenated UUID
  expected_revision: uint64
  precondition: current recovery_execution is IDLE or NON_CHILDLESS;
                current CHILDLESS is rejected before field application
  precondition: state_loss_quarantine is NONE
  precondition: PRIOR_EFFECT_FENCE_CLEAR requires
                PriorEffectFenceClearanceEligibilityV1 = ELIGIBLE;
                every other transition requires ExecutionEligibilityV1 = ELIGIBLE
  precondition: PRIOR_EFFECT_FENCE_CLEAR requires top-level IDLE, one matching
                non-null configured fence, and its matching sealed CLEAR receipt
                inside ConfiguredPriorEffectFenceBarrierReceiptCustodyV1 after
                the sole RECEIPT -> COMMITTING winner; receipt state_epoch and
                expected_revision are source-equal to the checked current values;
                every other transition requires that fence null
  transition: NonChildlessAuthorityTransitionV1
  allowed_updates: closed field map limited to
    absence_confirmation, consumed_manual_request_ids, recovery_execution,
    recovery_poll_terminal_sequence, manual_readiness, managed_generation,
    first_managed_epoch, launch_grace_until, launching, readiness_seen,
    runtime_continuity, child_establishment_guard, child_dead_confirmation,
    child_stall_confirmation, owned_childless_confirmation, and
    configured_prior_effect_unknown_fence
  invariant: the transition's private constructor supplies the closed allowed
             field subset and values from the normative transition table;
             callers cannot construct a map or choose arbitrary values
  invariant: next recovery_execution is IDLE or NON_CHILDLESS
  invariant: state_loss_quarantine is preserved byte-identically
  invariant: configured_prior_effect_unknown_fence is preserved byte-identically except that
             PRIOR_EFFECT_FENCE_CLEAR may clear one matching non-null fence only
             with the matching committed-successor barrier custody specified
             below
}

ConfiguredOwnedTreeActionCommitV1 =
  private #115 checked-transition result {
    mutation: private NonChildlessAuthorityDeltaV1(transition = RESERVE)
    precondition: selected configured action is kill-bearing
                  KILL_THEN_RELAUNCH or STUCK_RECOVERY
    action_binding:
      module-typed SupervisorOwnedTreeDispatchActionBindingV1.
        CONFIGURED_AGENT_RELAUNCH over the committed reservation/barrier state,
        successor revision, sealed target digest, and agent
    issuer_checkpoint:
      source-equal ConfiguredActionIssuerCheckpointV1 persisted inside that committed
      NON_CHILDLESS/RESERVED/PRE_BARRIER value
    action_custody:
      exactly one module SupervisorOwnedTreeDispatchActionCustodyV1 whose owner
      is READY for that binding
  }

EphemeralTerminalActionCommitV1 =
  private #115 checked-transition result {
    expected checked-store revision and source-equal request entry
    action: COMPLETE | TIMEOUT | FAILED
    next_entry: source-equal persisted successor written by this transition
    action_binding:
      module-typed SupervisorOwnedTreeDispatchActionBindingV1.
        EPHEMERAL_TERMINAL over request, agent, action, successor revision,
        sealed next_entry, target digest, and action-latch epoch
    action_custody:
      exactly one module SupervisorOwnedTreeDispatchActionCustodyV1 whose owner
      is READY for that binding
  }

AttendedConfiguredPreBarrierDispositionRequestV1 {
  request_id: RequestId
  expected_agent_key: NFC canonical agent/root string
  expected_state_epoch: lowercase hyphenated UUID
  expected_revision: uint64
  expected_reservation_id: lowercase hyphenated UUID
  expected_checkpoint_id: lowercase hyphenated UUID
  expected_hold_source_hash: Hex64 from the current operator-visible hold
  acknowledged_by: current operator-facing liaison or sole lead
  acknowledgements: ordered tuple, in this order, [
    ACKNOWLEDGE_NO_LIVE_ISSUER,
    ACKNOWLEDGE_PRIOR_STOP_TREE_EFFECT_UNKNOWN,
    ACKNOWLEDGE_FRESH_OBSERVATION_REQUIRED
  ]
  reason: nonempty single-line NFC string of at most 500 characters
}

AttendedConfiguredPreBarrierDispositionDeltaV1 =
  private, nonserializable, noncopyable, deeply immutable, alias-free #115
  checked mutation {
    request: complete AttendedConfiguredPreBarrierDispositionRequestV1
    hold: current ConfiguredPreBarrierOwnerLossHoldV1
    precondition: request agent/epoch/revision/reservation/checkpoint are
                  source-equal to the current hold and request.expected_hold_source_hash
                  is source-equal to hold.source_hash
    precondition: current recovery_execution is source-equal to the hold's
                   NON_CHILDLESS/RESERVED/PRE_BARRIER value
    precondition: state_loss_quarantine is NONE
    precondition: configured_prior_effect_unknown_fence is null
    precondition: hold.issuer_extinction is PROVED_GONE(result = GONE)
    precondition: the supervisor kill switch is present, the strict singleton
                  marker is absent, and a fresh independent OS observation
                  reports the source-equal issuer PID definitively absent;
                  a present PID is ineligible even when generic token/start
                  values differ
    precondition: acknowledged_by resolves under the current configuration lock
                  to the operator-facing liaison or sole lead
    allowed_updates (closed list):
      recovery_execution = IDLE;
      recovery_poll_terminal_sequence = current ordinary_poll_sequence;
      child_dead_confirmation = {count: 0, basis_digest: null,
                                 last_capture_id: null};
      child_stall_confirmation = {count: 0, basis_digest: null,
                                  last_capture_id: null};
      owned_childless_confirmation = module all-null count-zero value;
      absence_confirmation = EMPTY;
      configured_prior_effect_unknown_fence =
        new ConfiguredPriorEffectUnknownFenceV1 over the complete request/audit,
        checkpoint and its complete persisted #120 source target tuple,
        successor revision,
        current epoch/current ordinary poll sequence, and freshness-floor fields
        source-equal to that current epoch/sequence;
      revision = checked predecessor revision + 1
    invariant: every other ClassifierStateV1 field is preserved byte-identically
    invariant: the consumed reservation/absence proof is not restored and no
               manual request is consumed or satisfied
    invariant: constructs no witness, custody, permit, target, call, receipt,
               kill, barrier, launch, identity commit, retry, debt, or cycle
  }

AttendedConfiguredPreBarrierDispositionRejectionV1 =
  UNAUTHORIZED {
    reason: ACTOR_UNAUTHORIZED
    current_resolution: REDACTED_UNAUTHORIZED
    next_required_step: USE_CURRENT_AUTHORIZED_LIAISON_OR_SOLE_LEAD
  }
  | STORE_NOT_SELECTED {
      reason: WRONG_STORE | WRONG_AGENT
      current_resolution: NO_CURRENT_MATCHING_HOLD
      next_required_step: REFRESH_CURRENT_HOLD_AND_RETRY
    }
  | INVALID_OFFICIAL_STATE {
      reason:
        INVALID_CHECKED_STATE | WRONG_BARRIER_OR_TARGET_BINDING
      current_resolution: NO_CURRENT_MATCHING_HOLD
      next_required_step: ATTENDED_STATE_REPAIR_REQUIRED
    }
  | NO_CURRENT_OWNER_LOSS_HOLD {
      reason: WRONG_PHASE
      current_resolution: NO_CURRENT_MATCHING_HOLD
      next_required_step: REFRESH_CURRENT_HOLD_AND_RETRY
    }
  | STALE_CURRENT_HOLD {
      reason:
        STALE_SOURCE_HASH | WRONG_STATE_EPOCH | WRONG_REVISION
        | WRONG_RESERVATION | WRONG_CHECKPOINT
      current_resolution: CURRENT_HOLD(ConfiguredPreBarrierOwnerLossHoldV1)
      next_required_step: REFRESH_CURRENT_HOLD_AND_RETRY
    }
  | KILL_SWITCH_ABSENT {
      reason: KILL_SWITCH_ABSENT
      current_resolution: CURRENT_HOLD(ConfiguredPreBarrierOwnerLossHoldV1)
      next_required_step:
        CREATE_KILL_SWITCH_STOP_ALL_PROJECT_SUPERVISORS_THEN_REFRESH
    }
  | SINGLETON_PRESENT_OR_STALE {
      reason: SINGLETON_PRESENT_OR_STALE
      current_resolution: CURRENT_HOLD(ConfiguredPreBarrierOwnerLossHoldV1)
      next_required_step:
        REPAIR_CURRENT_SUPERVISOR_SINGLETON_THEN_REFRESH
    }
  | MALFORMED_ATTESTATION {
      reason: MALFORMED_REQUEST | MALFORMED_ACKNOWLEDGEMENTS
      current_resolution: CURRENT_HOLD(ConfiguredPreBarrierOwnerLossHoldV1)
      next_required_step: REFRESH_CURRENT_HOLD_AND_RETRY
    }

AttendedConfiguredPreBarrierDispositionResultV1 =
  DISPOSED_PRIOR_EFFECT_UNKNOWN {
    request_id, disposition_id, agent_key, state_epoch,
    old_revision, new_revision,
    reservation_id, source_checkpoint_id,
    prior_stop_tree_effect: UNKNOWN,
    automatic_retry: UNAVAILABLE(ConfiguredPreBarrierRetrySuccessorV1),
    next_required_step:
      REMOVE_KILL_SWITCH_START_ONE_CURRENT_SUPERVISOR_THEN_FRESH_OBSERVATION
  }
  | BLOCKED_ISSUER_LIVE_OR_UNPROVEN(ConfiguredPreBarrierOwnerLossHoldV1)
  | REJECTED_STALE_UNAUTHORIZED_OR_INVALID(
      AttendedConfiguredPreBarrierDispositionRejectionV1)

PriorEffectFenceClearanceGateHoldV1 =
  DRY_RUN {
    next_required_step: REINVOKE_WITHOUT_DRY_RUN_THEN_RETRY_CLEARANCE
  }
  | KILL_SWITCH_ACTIVE {
      next_required_step:
        REMOVE_KILL_SWITCH_CONFIRM_ONE_CURRENT_SUPERVISOR_THEN_RETRY_CLEARANCE
    }
  | SUPERVISOR_STOPPED {
      next_required_step:
        START_EXACTLY_ONE_CURRENT_SUPERVISOR_THEN_RETRY_CLEARANCE
    }

PriorEffectFenceBarrierHoldV1 =
  BLOCKED {
    next_required_step:
      HANDLE_AUTHORIZED_CURRENT_SOURCE_TARGET_SET_ATTENDED_THEN_RECAPTURE
  }
  | AMBIGUOUS {
      next_required_step:
        RESOLVE_SOURCE_TARGET_OBSERVATION_AMBIGUITY_THEN_RECAPTURE
    }
  | UNAVAILABLE {
      next_required_step:
        RESTORE_SOURCE_TARGET_OBSERVER_AVAILABILITY_THEN_RECAPTURE
    }

PriorEffectFenceClearanceStateRejectionV1 =
  NO_CURRENT_FENCE {
    current_resolution: NO_CURRENT_FENCE
    next_required_step: REFRESH_CURRENT_FENCE_STATE
  }
  | STALE_STATE_EPOCH_OR_REVISION {
      current_resolution: CURRENT_FENCE(ConfiguredPriorEffectUnknownFenceV1)
      next_required_step: OBTAIN_FRESH_COMMITTED_CAPTURE
    }
  | WRONG_FENCE_DISPOSITION_OR_SOURCE {
      current_resolution: CURRENT_FENCE(ConfiguredPriorEffectUnknownFenceV1)
      next_required_step: REFRESH_CURRENT_FENCE_AND_SOURCE_BINDING
    }
  | CAPTURE_NOT_ABOVE_CURRENT_FRESHNESS_FLOOR {
      current_resolution: CURRENT_FENCE(ConfiguredPriorEffectUnknownFenceV1)
      next_required_step: OBTAIN_FRESH_COMMITTED_CAPTURE
    }

ConfiguredPriorEffectFenceClearanceReconciliationV1 =
  FENCE_STILL_CURRENT {
    current_fence: source-equal checked ConfiguredPriorEffectUnknownFenceV1
    next_required_step: OBTAIN_FRESH_COMMITTED_CAPTURE
  }
  | FENCE_CLEARED {
      state_epoch: source-equal trustworthy checked current epoch
      current_revision: source-equal trustworthy checked current revision
      next_required_step: RESUME_NORMAL_SELECTION_SUBJECT_TO_FULL_EXECUTION_GATE
    }
  | STATE_OR_FENCE_UNTRUSTWORTHY {
      next_required_step: COMPLETE_ATTENDED_STATE_PROVENANCE_HANDLING
    }

ConfiguredPriorEffectFenceClearanceResultV1 =
  CLEARED {
    disposition_id: source-equal to cleared fence disposition_id
    source_checkpoint_id: source-equal to cleared source checkpoint ID
    state_epoch: source-equal to checked current epoch
    predecessor_revision: source-equal to barrier-receipt expected_revision
    successor_revision: predecessor_revision + 1
    capture_id: source-equal to barrier-receipt capture ID
    custody_disposition: CLOSED
  }
  | POLICY_HELD_GATE {
      fence: source-equal to current ConfiguredPriorEffectUnknownFenceV1
      gate: PriorEffectFenceClearanceGateHoldV1
      custody_disposition: CLOSED_FRESH_CAPTURE_REQUIRED
    }
  | POLICY_HELD_STATE_PROVENANCE_LOST {
      reason:
        MISSING | CORRUPT | TORN | ROLLBACK_UNPROVEN
        | QUARANTINED | INVALID_CHECKED_STATE
      current_resolution: NO_CURRENT_TRUSTWORTHY_FENCE
      custody_disposition: CLOSED_FRESH_CAPTURE_AFTER_ATTENDED_STATE_REPAIR
      next_required_step: COMPLETE_ATTENDED_STATE_PROVENANCE_HANDLING
    }
  | POLICY_HELD_BARRIER {
      fence: source-equal to current ConfiguredPriorEffectUnknownFenceV1
      barrier: PriorEffectFenceBarrierHoldV1
      custody_disposition: CLOSED_FRESH_CAPTURE_REQUIRED
    }
  | REJECTED_STALE_OR_MISMATCHED {
      rejection: PriorEffectFenceClearanceStateRejectionV1
      custody_disposition: CLOSED_FRESH_CAPTURE_REQUIRED
    }
  | FAILED_PROVED_NO_COMMIT {
      current_fence: source-equal to current ConfiguredPriorEffectUnknownFenceV1
      reason: CHECKED_MUTATION_REJECTED | PRE_COMMIT_EXCEPTION_PROVED_NO_WRITE
      custody_disposition: CLOSED_FRESH_CAPTURE_REQUIRED
      next_required_step: OBTAIN_FRESH_COMMITTED_CAPTURE
    }
  | CLEAR_COMMIT_OUTCOME_UNKNOWN {
      attempted_disposition_id: source-equal receipt fence disposition ID
      attempted_source_checkpoint_id: source-equal receipt source checkpoint ID
      attempted_state_epoch: source-equal receipt state epoch
      attempted_predecessor_revision: source-equal receipt expected revision
      attempted_capture_id: source-equal receipt capture ID
      custody_disposition:
        POISONED | CLOSED_RESPONSE_LOST | OWNER_LOST_WITH_PROCESS
      next_required_step:
        REREAD_OFFICIAL_CHECKED_STATE_AND_DERIVE_CONFIGURED_PRIOR_EFFECT_FENCE_CLEARANCE_RECONCILIATION_V1
    }
  | REJECTED_REPLAY_ZERO_EFFECT {
      custody_state: COMMITTING | CLOSED | POISONED
      current_resolution: NOT_READ_BY_LOSER
      next_required_step: OBSERVE_WINNER_OR_RELOAD_THEN_RECAPTURE_IF_HELD
    }

EphemeralTerminalReceiptApplyResultV1 =
  APPLIED
  | STALE_CLASSIFIER_EPOCH_RETRY_REQUIRED {
      request_id: source-equal to receipt request ID
      agent_key: source-equal to receipt agent
      action: source-equal to receipt COMPLETE | TIMEOUT | FAILED action
      bound_state_epoch: source-equal to receipt action-binding epoch
      current_state_epoch: source-equal to checked current classifier epoch
      retained_next_entry: source-equal sealed persisted next_entry identity
    }

ChildlessOuterStateDeltaV1 =
  private nonserializable operation projection {
    operation: module ExactTargetExecutorOperationV1
    allowed_updates: closed field map limited to
      managed_generation, first_managed_epoch, launch_grace_until, launching,
      readiness_seen, next_capture_ordinal, recovery_poll_terminal_sequence,
      runtime_continuity, child_establishment_guard, child_dead_confirmation,
      child_stall_confirmation, owned_childless_confirmation,
      absence_confirmation, consumed_manual_request_ids, and manual_readiness
    invariant: operation is source-equal to the enclosing permit operation
    invariant: the operation/event-table constructor supplies the closed allowed
               field subset and values; callers cannot construct a map or
               choose arbitrary values
    invariant: state_epoch, agent_key, ordinary_poll_sequence, and
               state_loss_quarantine are source-equal to their predecessor fields; the
               configured_prior_effect_unknown_fence is preserved byte-identically
  }

StateLossQuarantineCreationDeltaV1 =
  private, nonserializable, noncopyable, deeply immutable, alias-free
  owner-constructed fail-closed result {
  precondition: dry_run == false
  expected_outer_state: MISSING | CORRUPT | TORN | ROLLBACK_UNPROVEN
  new_state_epoch: lowercase hyphenated UUID
  quarantine_id: lowercase hyphenated UUID distinct from new_state_epoch
  decision_now_epoch: finite nonnegative Unix seconds
  replacement_state: complete ClassifierStateV1 with only:
    state_epoch = new_state_epoch; revision = 0; agent_key = current target;
    managed_generation = null; first_managed_epoch = decision_now_epoch;
    launch_grace_until = null; launching = false; readiness_seen = false;
    ordinary_poll_sequence = 0; next_capture_ordinal = 1;
    recovery_poll_terminal_sequence = null;
    runtime_continuity = NO_BASELINE;
    child_establishment_guard = NOT_APPLICABLE;
    child_dead_confirmation = {count: 0, basis_digest: null,
                               last_capture_id: null};
    child_stall_confirmation = {count: 0, basis_digest: null,
                                last_capture_id: null};
    owned_childless_confirmation = the module's all-null count-zero value;
    absence_confirmation = EMPTY; consumed_manual_request_ids = empty;
    recovery_execution = IDLE;
    configured_prior_effect_unknown_fence = null;
    state_loss_quarantine = module StateLossQuarantineV1.UNRESOLVED {
      quarantine_id = quarantine_id;
      reason = expected_outer_state;
      prior_physical_owner = UNKNOWN;
      attempt_provenance = UNKNOWN;
      debt_provenance = UNKNOWN;
      retirement_capability =
        CAPABILITY_UNAVAILABLE(PROCESS_UNIVERSE_IDENTITY_UNAVAILABLE)
    };
    manual_readiness = NONE
  invariant: creates no childless envelope, usable debt/cycle, attempt budget,
             managed identity, consumed manual ID, or launch authority
}

AttendedCaptureSequenceRolloverRequestV1 {
  request_id: RequestId
  expected_agent_key: NFC canonical agent/root string
  expected_state_epoch: lowercase hyphenated UUID
  expected_revision: uint64
  expected_ordinary_poll_sequence: 18446744073709551615
  acknowledgement: ATTENDED_STATE_EPOCH_ROLLOVER
}

CaptureSequenceRolloverDeltaV1 =
  private, nonserializable, noncopyable, deeply immutable, alias-free #115
  checked-replacement result {
    request: source-equal to the validated AttendedCaptureSequenceRolloverRequestV1 input
    precondition: checked agent/epoch/revision/maximum-sequence values are
                  source-equal to the request's expected values
    precondition: persisted recovery_execution == IDLE; a transient
                  EPHEMERAL_TERMINAL action owner is not persisted execution
    new_state_epoch: fresh lowercase hyphenated UUID distinct from old epoch
    replacement_state: complete ClassifierStateV1 with only:
      state_epoch = new_state_epoch; revision = 0;
      agent_key, managed_generation, first_managed_epoch, launch_grace_until,
        launching, readiness_seen, consumed_manual_request_ids,
        state_loss_quarantine, and manual_readiness preserved byte-identically;
      configured_prior_effect_unknown_fence =
        null when the predecessor field is null, otherwise copy the predecessor
        fence, preserving every other field byte-identically, with only
        freshness_floor_state_epoch = new_state_epoch and
        freshness_floor_after_ordinary_poll_sequence = 0;
      ordinary_poll_sequence = 0; next_capture_ordinal = 1;
      recovery_poll_terminal_sequence = null;
      runtime_continuity = NO_BASELINE;
      child_establishment_guard = NOT_APPLICABLE;
      child_dead_confirmation = {count: 0, basis_digest: null,
                                 last_capture_id: null};
      child_stall_confirmation = {count: 0, basis_digest: null,
                                  last_capture_id: null};
      owned_childless_confirmation = the module's all-null count-zero value;
      absence_confirmation = EMPTY;
      recovery_execution = IDLE
    invariant: constructs no quarantine, witness, permit, authority, effect
               mutation, call, kill, launch, attempt, debt, or cycle
    invariant: a non-null fence's disposition/audit, source checkpoint/targets,
               effect disposition, and launch requirement are preserved
               byte-identically; rollover neither clears nor duplicates it
  }

AttendedCaptureSequenceRolloverResultV1 =
  ROLLED_OVER {
    request_id, agent_key, old_state_epoch, old_revision,
    new_state_epoch, new_revision: 0
  }
  | BLOCKED(CaptureSequenceExhaustionV1)
  | REJECTED_STALE_OR_INVALID

PrivateClassifierStateMutationV1 =
  private, nonserializable, noncopyable, deeply immutable, alias-free closed sum:
    OBSERVATION(PrivateClassifierObservationMutationV1)
    | NON_CHILDLESS_AUTHORITY(NonChildlessAuthorityDeltaV1)
    | ATTENDED_CONFIGURED_PRE_BARRIER_DISPOSITION(
        AttendedConfiguredPreBarrierDispositionDeltaV1)
    | CHILDLESS_EFFECT(module PermitBoundChildlessMutationV1)
    | STATE_LOSS_QUARANTINE(StateLossQuarantineCreationDeltaV1)
    | CAPTURE_SEQUENCE_ROLLOVER(CaptureSequenceRolloverDeltaV1)
```

At begin, the #115 owner reads the checked predecessor. When its
`ordinary_poll_sequence` is less than the maximum `uint64`, begin fixes the
prospective ordinal-zero `CaptureIdV1` and atomically creates the lineage owner
before the installed observer performs any acquisition. At the maximum value,
checked addition is impossible: begin returns
`ATTENDED_REQUIRED(CaptureSequenceExhaustionV1)`, constructs no lineage,
acquisition handle, capture, receipt, or mutation, performs no 87-A action, and
never wraps to zero. The result's blocker tuple is derived from the current checked
state in displayed enum order: `NON_CHILDLESS_EXECUTION` for any non-childless
execution; `CHILDLESS_ENVELOPE` for every childless envelope, plus each
applicable non-idle execution, non-`NONE` debt/cycle/continuation, or nonempty
retired-attempt reason. `READY` exists only for persisted top-level `IDLE`; an
in-process `EPHEMERAL_TERMINAL` owner is intentionally not a persisted blocker.
Begin does not commit or reserve persisted state. The
owner invokes the installed ordinary-observer adapter only with the resulting
`OrdinaryObservationAcquisitionV1`. That adapter must win the specified atomic
`UNUSED -> ACQUIRING` transition before its first observation step; a concurrent
alias or sequential replay loses without observation or receipt construction.
The winner writes the source-equal prospective ID into one new immutable
`ProcessObservationV1`; its unexported receipt factory deep-copies and seals
that completed acquisition, fully constructs a dormant
`OrdinaryObservationCommitCustodyV1`, then atomically moves the owner
`ACQUIRING -> RECEIPT` and yields that bound custody handle. No fallible
allocation occurs between transition and yield. Any uncertain acquisition,
receipt/custody construction, or handoff poisons the owner instead of retrying.
No factory surface accepts an acquisition completed for a different lineage or
replaces its capture ID. Receiving raw operands, knowing a lineage ID, or
implementing the public owner API does not expose that factory. This is one #115
begin/observe/checked-reduce/commit boundary, not a second observation owner.

The only V1 recovery from the typed exhaustion result is the attended #115
operation
`rollover_capture_sequence(AttendedCaptureSequenceRolloverRequestV1)`. It
rechecks the source-equal agent, old epoch, revision, maximum sequence, and top-level
`IDLE`, fully constructs the displayed replacement, and atomically installs it
with one checked replacement compare-and-swap. Two requests race that same old
epoch/revision; exactly one can win. The first ordinary begin after success mints
`CaptureIdV1(new_state_epoch, same agent_key, 1, 0)`. Every old-epoch lineage,
receipt, custody handle, capture ID, confirmation, proof, or CAS loser is stale
and must reject or poison with zero mutation; rollover needs no registry of
transient observation handles.

An ephemeral receipt application and rollover race through #115's checked-state
compare-and-swap boundary. If rollover is visible at the dispatcher's final
classifier-provenance read before plan ownership, the old binding is
`REJECTED_NO_EFFECT(VARIANT_PROVENANCE_STALE)` and preserves `next_entry`
byte-identically. Once
that read has passed, rollover may win during the post-read portion of
`DISPATCHING`, or while the call is `PLAN_OWNED` or `INVOKING`; the invocation
may proceed and no branch claims no effect merely because the eventual receipt
epoch is stale. Normal no-effect-result or poison rules still apply before a
receipt exists.

At receipt application, any trustworthy schema-valid official checked state for
the same agent whose current epoch differs from the receipt binding is sufficient
for the no-write stale branch; an old/new UUID pair need not prove an unstored
rollover lineage. A missing, untrusted, corrupt, or wrong-agent state cannot use
that branch and follows the existing rejection/poison rule. The CAS orders are
specified below. If rollover's checked-state CAS wins first, exactly one receipt owner wins
`RECEIPT -> CONSUMING_RECEIPT`, returns
`EphemeralTerminalReceiptApplyResultV1.STALE_CLASSIFIER_EPOCH_RETRY_REQUIRED`,
moves `CONSUMING_RECEIPT -> CLOSED`, preserves the persisted `next_entry` byte-identically,
and performs no old/new-epoch state write, archive, launch, or other receipt-
driven behavior. If receipt application wins first, it returns `APPLIED`, closes
the owner, and the original revision-bound rollover request returns
`REJECTED_STALE_OR_INVALID`; a fresh exhaustion result and attended request
against the successor revision are required. An unknown receipt-state commit
outcome moves the owner to `POISONED(PLANNER_COMMIT_UNCERTAIN)`. A read-only
barrier captured before a losing commit is discarded. Only a fresh #115 checked
terminal transition against trustworthy current state may authorize another
attempt; quarantine and every current hold still gate it.

Any persisted non-childless execution or any childless envelope blocks rollover and leaves
the complete state byte-identical. In particular, rollover never rebinds,
retires, or clears active childless execution, teardown debt, an
`ACTIVE`/`EXHAUSTED` automatic cycle, continuation, or retired attempt into a
new epoch and never resets attempt budget. If maximum sequence is reached while
such an obligation requires a later ordinary capture, V1 may remain
`POLICY_HELD` indefinitely pending attended resolution of that obligation. This
is an explicit residual, not authority to erase the fence or create a
state-loss quarantine from trustworthy state.

The ordinary-observation equations remain pure and independently testable, but
their constructor and execution are private to #115's checked transaction. At
commit, #115 accepts only `OrdinaryObservationCommitCustodyV1`, validates its
sealed receipt and bound owner binding, and uses that handle's unexported owner
reference; the sealed receipt alone is inert, and no receipt-to-owner registry
or caller mutex is permitted. It must win `RECEIPT -> COMMITTING` before
derivation or field application. A same-reference custody-handle alias or
sequential replay loses without mutation. The winner validates the intact
receipt seal, lineage, expected predecessor, prospective capture ID, and raw
capture ID against the still-current predecessor. It derives one candidate
observation successor whose `CaptureIdV1` is source-equal to that begin-bound ID
and whose ordinary poll sequence is the predecessor sequence plus one, sets the
`PrivateClassifierObservationMutationV1.capture_id` source-equal to that
successor ID, and derives continuity,
establishment guard, and every
confirmation successor from the checked predecessor plus the receipt's raw
operands. Commit never mints, replaces, reseals, or restamps a completed
acquisition's `CaptureIdV1`. The owner applies the mutation before leaving that
same transaction. A caller cannot submit that mutation or choose any derived
field. The committed module overlay is reduced from the same receipt and never
feeds or rewrites either banked child counter. It may advance only the
displayed fields while a childless executor is unavailable, but cannot alter
`ChildlessEffectEnvelopeV1`, consumed manual IDs, `manual_readiness`,
launch/backoff/readiness, marker/configuration state, allocate a nonordinary
effect capture, or write an effect terminal.

When the checked predecessor carries a non-null configured prior-effect fence,
#115 also fully constructs one dormant `CommittedOrdinaryFenceCaptureV1` and
its separate use owner from that sealed receipt, prospective successor revision,
and byte-identically preserved fence before the state compare-and-swap. Only the
winning commit activates and publishes the witness after the successor is
durably installed; a CAS loser activates no witness and returns none. Thus two lineages may
share a prospective `CaptureIdV1`, but only the capture belonging to the
actually committed successor can reach the fence-barrier reducer. Failure to
publish after the state commit poisons that dormant use owner and leaves the
fence intact; a later fresh ordinary observation may try again.

One receipt commits or is poisoned exactly once. A successful checked commit
moves its owner `COMMITTING -> CLOSED`; after the one commit-admission winner,
a stale predecessor, checked-state compare-and-swap loss, or uncertain commit
moves it to `POISONED`, never back to `RECEIPT`. A losing receipt-consumption
alias does not change the winner's owner state. Copy, replay, serialization,
wrong agent/epoch/revision, a nonordinary receipt, mutable or altered nested
evidence, or any receipt carrying a derived successor field is rejected before
field application. Two valid lineages based on one predecessor revision may
carry the same prospective capture ID, but the checked CAS admits at most one.
The stale loser's lineage, receipt, and completed acquisition are poisoned.
After reload, that acquisition cannot be wrapped, resealed, or restamped with
the successor prospective ID; only a new begin and a new real acquisition may
produce the next receipt. One legitimate receipt can advance an empty/count-zero confirmation
only to count one/`OBSERVED_ONCE`; a second confirmation requires a distinct
later acquisition and receipt committed against the successor revision.

A non-childless authority delta may consume/invalidate absence proof and
advance only its displayed field mask after execution eligibility. The checked
owner rejects that variant when current execution is `CHILDLESS`; it cannot
replace or remove an envelope by selecting `IDLE` or `NON_CHILDLESS`. Only a
`CHILDLESS_EFFECT` delta may address, replace, or remove a current childless
envelope. Its private transition constructor fixes both the updated subset and
values. A childless mutation is not constructible from checked state or a persisted binding: its
private constructor requires a fresh non-serializable
`ExactTargetExecutorPermitV1` whose binding, operation scope, and expected
revision match the current envelope, or whose `RESERVE` scope matches the
absence of one. The permit-bound delta may change that envelope and only the
`ChildlessOuterStateDeltaV1` projection named by the module's closed
transition.
`StateLossQuarantineCreationDeltaV1` is the exclusive fail-closed state-loss
constructor; it cannot compose with an observation, authority, or effect delta.

For a kill-bearing configured reservation, #115 fully constructs one dormant
`ConfiguredOwnedTreeActionCommitV1` before its checked compare-and-swap. The
winning checked commit both installs the configured reservation/barrier successor and
its source-equal `ConfiguredActionIssuerCheckpointV1` and atomically activates/yields
its single action custody in `READY`; a CAS loser activates nothing. The closed ephemeral terminal transition applies the same
rule to `EphemeralTerminalActionCommitV1`: it persists that `next_entry` and
yields one `READY` custody for that request/action. Neither constructor may mint
custody after the fact from the persisted successor. A crash between commit and
caller receipt may lose the action, but reload sees only inert provenance and
cannot reproduce the token. For ephemeral action, only a new checked #115
terminal transition may authorize a new attempt. For configured PRE_BARRIER,
the persisted reservation rejects such a transition: reload instead derives
`ConfiguredPreBarrierOwnerLossHoldV1` with `PROVED_GONE` or
`LIVE_OR_UNPROVEN` extinction status and waits for the eligible attended
disposition or the undelivered `ConfiguredPreBarrierRetrySuccessorV1`.

The corresponding module call constructor fully prepares a dormant call and
submission, then atomically consumes that owner `READY -> CALL`. Two
constructor invocations over one custody—concurrent or sequential—have one
winner. A second checked mint for one committed transition, a constructor over
copied barrier/next-entry data, and a lookup by action ID or digest are
unconstructible. This action-scoped issuance contract is normative but remains
**BLOCKED ON #115**; #146 begins only at the later `CALL -> DISPATCHING` seam.

This owner-loss rule leaves the live same-invocation veto path intact. While the
captured configured issuer and its `READY` custody remain live, a final barrier or
policy veto may atomically close that custody and apply the ordinary private
`PRE_BARRIER_RELEASE`, positively proving no native call was issued. Once
custody has left `READY`, issuer liveness is unproven, or reload handles the
checkpoint, that release is forbidden. The complete reservation/checkpoint
remains, prior Stop-Tree effect is `UNKNOWN`, and the current matching owner-loss hold is
returned with no automatic remint, generic release, or release-and-reserve.

Revision 15 specifies one new attended operation,
`dispose_configured_pre_barrier(AttendedConfiguredPreBarrierDispositionRequestV1)`.
It is modeled only on the attended, no-effect, state-clearing posture of merged
`agenttalk supervise --reset-process-tree-ownership`; that shipped command's
conservative generic start-token fallback is neither evidence from
`ExactIssuerIdentityAdapterV1` nor disposal authority. The shipped command is not this new operation and does
not already implement it. Before invoking it,
the operator creates or preserves `supervisor.kill`, stops every project
supervisor, and confirms the current singleton marker is absent. The operation
runs only against the official checked store under supervisor lifecycle,
configuration, and #115 state locks, with that kill switch still present and
the marker still absent. It rechecks source-equal agent, epoch, revision,
reservation, checkpoint, barrier-state identity, target digest, and hold source
hash. A fresh OS observation made outside the dead issuer must report the
checkpoint issuer PID definitively absent. A present PID cannot be classified
as recycled from the generic checkpoint token/start fields; it stays
`LIVE_OR_UNPROVEN` with
`CAPABILITY_UNAVAILABLE(ExactIssuerIdentityAdapterV1)`. Operator assertion, the
checkpoint itself, and the process being disposed cannot prove extinction.

The operator attests only that the abandoned action is being disposed with its
native kill outcome unknown, that no automatic retry is claimed, and that a
fresh ordinary observation is required. The operator does not attest that a
kill did or did not happen, that any target is gone, or that launch is safe. One
winning checked disposition clears the abandoned execution to `IDLE`, writes
the same-poll terminal, resets only the displayed selection confirmations, and
installs `ConfiguredPriorEffectUnknownFenceV1`; it never kills or launches.
Afterward the system may assume only that the definitively absent issuer owns
no reservation or action custody. The persistent fence records the complete
request/audit binding, a checkpoint source-equal to the disposed checkpoint, module-typed source targets,
immutable prior-effect-unknown disposition, and
current-epoch freshness floor. It globally blocks every configured or childless
reservation/effect/launch; it cannot be replaced by another disposition. The
operator must then remove `supervisor.kill` and start exactly one current
supervisor. The persistent global fence makes that supervisor observation-only
until clearance, while removing the kill switch restores the
`PriorEffectFenceClearanceEligibilityV1.ELIGIBLE` precondition when dry run is
off, state provenance is intact, and the current instance matches. The only automatic next step is
a read-only fresh ordinary observation. Only its winning committed successor
publishes `CommittedOrdinaryFenceCaptureV1`; #115's private deny-only reducer
consumes that witness and yields the matching barrier-receipt custody. Private
`PRIOR_EFFECT_FENCE_CLEAR` may remove the fence only from top-level `IDLE`
when narrow fence-clearance eligibility is `ELIGIBLE`, current revision still
matches the receipt's committed successor revision, and that receipt is clear for every
source target/descendant and strictly newer than the current freshness floor.
It never kills or launches. A blocked/ambiguous/unavailable receipt keeps
`POLICY_HELD` and names attended target handling. Until the automatic retry
successor ships, the attended disposition remains the only V1 exit from the
owner-loss hold; fence clearance is a later no-effect reconciliation, not a
retry mint.

While the byte-identical fence remains current, replay of its
`disposition_request.request_id` with the same complete request returns the same
disposition result without another
mutation. A different request is stale. After clearance, even the original
request is stale; this core claims no hidden audit lookup beyond the persisted
fence and future 87-B delivery.

Every disposition refusal is typed and is selected by one closed order. Actor
authorization is evaluated first and dominates every other failure; an
unresolvable or unauthorized actor returns only `UNAUTHORIZED` with
`REDACTED_UNAUTHORIZED`, without reading out current hold or store details.
For an authorized actor, official-store selection and checked-state/binding
validity follow: `WRONG_STORE` precedes `INVALID_CHECKED_STATE`, which precedes
`WRONG_BARRIER_OR_TARGET_BINDING`. A byte-identical request against its
current successor fence is then the idempotent `DISPOSED` replay; this branch
precedes owner-loss-hold derivation, so the already-disposed `IDLE` state is not
misreported as `WRONG_PHASE`. Otherwise no current owner-loss hold yields
`WRONG_PHASE`. A current hold is compared in this order: `WRONG_AGENT` returns
no hold; for the same agent, `WRONG_STATE_EPOCH`, `WRONG_REVISION`,
`WRONG_RESERVATION`, `WRONG_CHECKPOINT`, then `STALE_SOURCE_HASH` return the
current matching hold. Malformed request/acknowledgements follow, then
`KILL_SWITCH_ABSENT`, then `SINGLETON_PRESENT_OR_STALE`, and finally issuer
extinction. The same order is rechecked under the commit locks. Thus every
observable failure has exactly one reason, resolution, and next step even when
several predicates fail. `KILL_SWITCH_ABSENT` directs the operator to create the kill
switch, stop every project supervisor, and refresh rather than pretending an
absent switch can be preserved. `BLOCKED_ISSUER_LIVE_OR_UNPROVEN` remains the
distinct post-validation result carrying the current unproved hold. No rejected
result mutates state or performs an effect.

The checked owner applies exactly one private tagged mutation per transaction.
Its public observation API is
`commit_ordinary_observation(OrdinaryObservationCommitCustodyV1)`, not
`commit_delta(OBSERVATION(...))`. A non-dry-run poll first consumes that custody
handle and its sealed receipt,
derives and commits the private observation mutation, reloads the successor
revision, and only then may privately construct a non-childless or permit-bound
childless delta. It may never infer a permit from the observation or upgrade the
committed projection into an effect mutation. Authority and policy functions
remain mutation-free. `decision_now_epoch` is the poll's one captured finite
nonnegative UTC Unix-seconds value. The task #115 owner compares
`(state_epoch, revision)`, commits with `revision + 1`, and makes a stale writer
recapture/reduce or fail closed. State-loss creation instead compares the observed
unavailable outer-state condition and atomically installs the complete displayed
replacement at revision zero; no field from the unavailable state participates.
The owner exposes no raw whole-state write and never lets a cached whole-state
save roll back a newer field. It returns only the committed read-only observation
projection and successor revision, never a resubmittable mutation. A dry-run
may preview the same private equations and discard the output, but it consumes
or poisons its receipt and exposes no commit-shaped value. A recovery plan binds
to the committed successor revision and cannot execute unless that
transaction committed; it must reload/recapture rather than combine an old
runtime/freshness result with newer absence or manual state.

The module's ordinary residual observation is captured input only. Clearing
envelope debt/cycle after `COMPLETE_GONE` is a
`PermitBoundChildlessMutationV1`, never a private ordinary-observation
mutation, and
may commit only under `ExecutionEligibilityV1.ELIGIBLE`, a matching fresh
permit, and childless execution `IDLE` with no named reservation, closure ID,
pending disposition, or debt current attempt. Without that permit, no residual
effect capture or clearing mutation is constructible. A same-poll ordinary
observation may still advance only its independent projection as specified
above.

**ENFORCED runtime-continuity transitions, evaluated top to bottom:** Current
schema `turn_generation` and `progress_sequence` retain the strict runtime
validator's arbitrary-precision nonnegative-integer domain; active phase
separately requires `turn_generation > 0`.

1. With `NO_BASELINE`, or with a strict valid bound record whose
   `wrapper_generation` differs from
   `RuntimeContinuityStateV1.BASELINE.wrapper_generation`,
   establish a new baseline, set both generation/sequence high-water values,
   set `progress_seen_epoch=decision_now_epoch`, clear the regression latch,
   reset child confirmation, and reset the establishment guard to
   `NOT_APPLICABLE`.
2. For the same wrapper generation, a turn generation below
   `turn_generation_high_water` preserves the complete baseline, sets the
   latch, resets child confirmation, and emits
   `CURRENT_UNKNOWN_SEQUENCE_REGRESSION`. It never becomes a new baseline.
3. For the same wrapper generation, a turn generation above high-water is the
   only new-turn transition: advance turn-generation high-water, set sequence
   high-water to the new record's sequence, persist its phase, set
   `progress_seen_epoch=decision_now_epoch`, clear the regression latch, and
   reset child confirmation and the establishment guard to `NOT_APPLICABLE`.
4. Within the same wrapper and high-water turn, sequence below high-water
   preserves high-water, phase, and timestamp; sets the latch; resets child
   confirmation; and emits `CURRENT_UNKNOWN_SEQUENCE_REGRESSION`.
5. Within the same wrapper and high-water turn, sequence above high-water advances high-water,
   persists the current phase, sets
   `progress_seen_epoch=decision_now_epoch`, resets child confirmation, and
   preserves an already-set latch.
6. The same wrapper/turn with equal sequence but a changed phase preserves
   high-water and latch, persists the new phase, sets
   `progress_seen_epoch=decision_now_epoch`, resets child confirmation, and
   preserves an existing keyed establishment guard byte-identically.
7. The same wrapper/turn with equal sequence and equal phase preserves
   high-water, timestamp, latch, and eligible confirmation progress.
8. An absent, invalid, unsupported, unbound, or otherwise unusable current
   record preserves the complete baseline, latch, and keyed establishment
   guard but resets child-death and stall counters. If latched, reasons contain
   both sequence regression and the current degradation; the banked rank keeps
   regression dominant.
9. Only a strict valid bound different wrapper generation or a strictly higher
   turn generation clears the latch. Lower-turn replay, torn reads, higher
   same-turn sequence, heartbeat or snapshot changes, and policy actions cannot
   clear it. A genuinely new `state_epoch` after irrecoverable state loss has
   no prior baseline; cross-loss regression detection is not promised.

**ENFORCED nonrenewable child-establishment guard after task #115:** A strict
bound `ACTIVE` record constructs one `ChildEstablishmentKeyV1`. The guard's
time codec is integer-only: validated UTC timestamps normalize to Unix
milliseconds by rounding an evidence timestamp upward and
`decision_now_epoch_ms` downward; overflow is invalid. When no current-turn
adapter progress exists, the normalized `updated_at` is the wrapper-published
`ACTIVE` transition time and constructs `OPEN` with
`active_age_grace_through_epoch_ms =
active_record_updated_at_epoch_ms + 30000` using checked addition. The same
transaction captures the applicable checked generation launch fence:
`generation_launch_grace_until_epoch_ms` is source-equal to the normalized
`launch_grace_until` only when the managed generation is the guard key's
generation and checked state says `launching=true` and `readiness_seen=false`;
otherwise it is null. A missing, malformed, generation-mismatched, or changing
same-key launch-fence input cannot construct or close the guard: the active
child remains `UNKNOWN`, both child-death counters reset, and no authority is
created. The checked owner persists both anchors on first observation of the
key. Heartbeat writes, ordinary polls, absence, snapshot changes, and repeated
runtime reads cannot move them. A same-key `ACTIVE` record that changes
`updated_at` without positive current-turn adapter progress is likewise
invalid for child-death qualification and resets both counters; it never
renews either grace.

The guard becomes `CLOSED(ADAPTER_PROGRESS)` on a validated current-turn
`last_progress_at`, using its normalized integer timestamp as
`close_evidence_epoch_ms`; its three anchor fields are null when their original
values are no longer recoverable. It becomes
`CLOSED(NONRENEWABLE_GRACE_EXPIRED)` only when both shipped no-handoff
protections have closed:

```text
decision_now_epoch_ms > active_age_grace_through_epoch_ms
and (
  generation_launch_grace_until_epoch_ms is null
  or decision_now_epoch_ms >= generation_launch_grace_until_epoch_ms
)
```

The active-record grace is therefore inclusive through the 30-second boundary,
while the generation launch fence is exclusive at its stored deadline, matching
the shipped predicates. The closed variant retains both opening anchors byte-identically and
sets `close_evidence_epoch_ms` to the earliest integer millisecond satisfying
that conjunction:
`max(active_age_grace_through_epoch_ms + 1,
generation_launch_grace_until_epoch_ms or 0)`, using checked addition. A first
observation that already has current-turn adapter progress constructs the
`ADAPTER_PROGRESS` closed variant directly. A key may replace the persisted
guard only after runtime continuity has accepted a different wrapper
generation or a strictly higher turn, or after the common guarded-launch
commit has installed a different managed generation; those transitions first
write `NOT_APPLICABLE`. A changed key within the same accepted wrapper and
high-water turn is `UNKNOWN`, preserves the old guard, and resets both
child-death counters. A non-active phase makes
`ActiveChildObservationV1` return `NOT_APPLICABLE` but preserves the keyed
guard byte-identically. A later same-key `ACTIVE` record must resume that
retained `OPEN` or `CLOSED` guard; missing or unreadable retained state is
`UNKNOWN` and cannot reconstruct anchors from a newer `updated_at`.

While the guard is `OPEN`, a complete zero-child capture is
`UNKNOWN(CHILD_ESTABLISHMENT_OPEN)`, contributes
`CURRENT_UNKNOWN_ACTIVE_CHILD`, and resets both the banked `CHILD_DEAD`
counter and the module overlay. At the 30-second boundary it remains open. If a
longer generation launch fence applies, two complete absences after 30 seconds
but before that fence also remain open. The first capture at or after the
exclusive launch-fence deadline is the first `ABSENT` sample only when the
inclusive active-age grace has also ended. No pre-close capture carries into a
closed confirmation. The module confirmation basis, reservation, and
action-time guard fields are source-equal to the complete closed guard object; a key,
result, either anchor, or close-evidence change vetoes the named action.

Child-death confirmation increments only on qualifying consecutive
same-baseline observations. Stall confirmation increments only on qualifying
consecutive same-baseline/same-sequence observations. Uncertainty, identity
change, sequence advance, or an incompatible observation resets the applicable
counter; each saturates at 2. One invalid read therefore cannot launder a later
same-turn sequence regression; clearing high-water on such a read is
nonconforming.

For either counter, `basis_digest` is SHA-256 over
`agenttalk.supervisor.consecutive-evidence-basis.v1\0` plus
`CanonicalJsonV1` of the following closed object:

```text
{
  "schema": "consecutive-evidence-basis/v1",
  "kind": "CHILD_DEAD" | "CHILD_STALL",
  "state_epoch": <state_epoch>,
  "managed_generation": <managed_generation>,
  "wrapper_generation": <wrapper_generation>,
  "turn_generation": <nonnegative integer turn-generation high-water>,
  "phase": <phase>,
  "progress_sequence": <nonnegative integer>,
  "active_child_config_digest": <Hex64>
}
```

The config digest covers the canonical brain matcher, launcher-self policy,
row schema, ancestry algorithm version, and start-guard schema. A qualifying
capture has `capture_ordinal=0`. Replay leaves `last_capture_id` byte-identical. A
distinct capture with the same basis advances only when its committed ordinary
poll sequence equals the predecessor sequence plus one; a gap or changed basis
restarts at count
1. Nonqualifying evidence resets to `(0, null, null)`. Count 2 is the only
confirmed value. Cached capture replay and stale re-reduction therefore cannot
manufacture teardown proof.

### One operand convention

**ENFORCED by every authority equation in 87-A:** Authority and escalation use
`runtime.dominant` only. `runtime.reasons` affects diagnostics and
`RecoveryConditionFingerprintV1`, never teardown, replacement, escalation, or
policy gates.

This concrete counterexample is normative:

```text
runtime.reasons = (
  CURRENT_UNKNOWN_SEQUENCE_REGRESSION,
  CURRENT_TEARDOWN_PROOF,
)
runtime.dominant = CURRENT_UNKNOWN_SEQUENCE_REGRESSION
presence = PRESENT_UNTARGETABLE
freshness = FRESH

automatic_teardown = false
escalation_required = false
```

Changing only freshness to `STALE` makes escalation required because the
dominant state is stale uncertainty. A membership reading of the secondary
teardown reason is nonconforming.

**STATED cardinality:** The 96 cells below are the cardinality of the automatic
authority's dominant projection:

```text
12 runtime dominants x 4 presence states x 2 freshness states = 96
```

They are not the cardinality of the full observation space.
`RuntimeObservationV1` includes an ordered reason tuple with overlaps, and the
valid predicate combinations are derived rather than enumerated. Manual-marker,
targetability, absence-confirmation, launch-timing, and post-teardown values add
further dimensions. Every full observation still maps to exactly one dominant
projection cell.

### Unsupported runtime envelope

**ENFORCED by a fail-closed envelope reader:** Runtime input is read under the
existing 16 KiB byte ceiling, UTF-8/no-BOM requirement, duplicate-key
rejection, and top-level-object requirement. The envelope may inspect only
`schema_version`, which must be an integer (not Boolean) in `0..4294967295`
(`src/agenttalk/wrapper_runtime.py:28` and
`src/agenttalk/wrapper_runtime.py:302-321`).

- Current `schema_version == 1` enters the full closed-record validator. Unknown or
  missing keys there remain `INVALID_CONTRACT`.
- A well-formed noncurrent version produces `UNSUPPORTED_CONTRACT` and exposes
  only that numeric version as bounded diagnostic evidence.
- A missing, mistyped, out-of-range, duplicate, oversized, malformed, or
  otherwise unreadable envelope produces `INVALID_CONTRACT`.

No agent, identity, lifecycle, health, target, timing, or authority field is
salvaged from an unsupported record. An unsupported version can never grant
health, teardown, or replacement authority.

## Heartbeat freshness

**ENFORCED by a closed input and pure freshness reducer:**

```text
HeartbeatRawCaptureV1 =
  ABSENT
  | PRESENT_TIMESTAMP(timestamp_epoch: finite Unix seconds)
  | INVALID(reason: HeartbeatInvalidReasonV1)

HeartbeatInvalidReasonV1 =
  UNREADABLE | SIZE | BOM_OR_NUL | UTF8 | EMPTY | TIMESTAMP | TIMEZONE
  | NONFINITE | FUTURE_SKEW | ARITHMETIC

HeartbeatEvidenceV1 =
  OBSERVED(authoritative_age_seconds: finite non-Boolean number >= 0)
  | MISSING
  | INVALID_OR_FUTURE_SKEW(reason: HeartbeatInvalidReasonV1)

HeartbeatFreshnessV1 = FRESH | STALE

FreshnessStateV1 {
  first_managed_epoch: finite nonnegative Unix seconds
  launch_grace_until: finite nonnegative Unix seconds | null
}
```

**ENFORCED raw constructor:** One bounded file capture distinguishes path
absence alone as `ABSENT`. File-kind/path/I/O failure is
`INVALID(UNREADABLE)`. The reader accepts at most 128 bytes, UTF-8 without BOM
or NUL, trims surrounding Unicode whitespace, and requires a nonempty
timezone-aware ISO-8601 timestamp accepted by the current
`datetime.fromisoformat` grammar after terminal `Z` is normalized to `+00:00`.
The displayed invalid-reason order is precedence when multiple failures are
observable. Conversion to Unix seconds must be finite; range/overflow is
`NONFINITE` or `ARITHMETIC`.

`decision_now_epoch` is captured once for the poll. The existing
`resolve_stuck_after` precedence supplies `resolved_stuck_after_seconds`, but
each configured candidate must be a finite non-Boolean numeric value at least
zero; an invalid per-agent/global candidate is skipped and the existing
wrapped-CLI or 120-second built-in default applies.
The existing per-agent/global health-timing precedence similarly supplies
`resolved_heartbeat_skew_seconds`; a configured candidate is accepted only
when it is finite, non-Boolean, and nonnegative, otherwise resolution continues
to the next candidate and finally the existing 30-second default. An observed
heartbeat timestamp later than
`decision_now_epoch + resolved_heartbeat_skew_seconds` is
`INVALID_OR_FUTURE_SKEW`; one inside that tolerance has
`authoritative_age_seconds = max(0, decision_now_epoch - timestamp_epoch)`.
`OBSERVED` requires that resulting age to be finite and nonnegative.
`ABSENT` maps only to `MISSING`; every `INVALID` raw capture, excessive future
skew, nonfinite subtraction, and arithmetic failure maps to
`INVALID_OR_FUTURE_SKEW` with its corresponding typed reason. No invalid input can become
`MISSING` or `OBSERVED`.
`resolved_launch_grace_seconds` is the configured finite non-Boolean numeric
value in `0..86400`, otherwise the existing 120-second default. The checked
state owner initializes `first_managed_epoch` exactly once on the first
committed classifier poll for every configured managed `(state_epoch,
agent_key)`, even when runtime, heartbeat, report, and snapshot are missing.
It does not wait for first launch. Missing heartbeat, runtime failure, snapshot
failure, ordinary polling, and process absence cannot rewrite it.

```text
grace_deadline =
  launch_grace_until
    if a real launch was atomically committed for the current managed generation
  else first_managed_epoch + resolved_launch_grace_seconds

within_grace = decision_now_epoch < grace_deadline

heartbeat_within_threshold =
  evidence is OBSERVED
  and authoritative_age_seconds <= resolved_stuck_after_seconds

freshness =
  FRESH if within_grace or heartbeat_within_threshold
  else STALE
```

Age equal to the threshold is fresh; grace expires at its
deadline. `MISSING`, malformed, or excessive-future-skew heartbeat has no
age-based freshness and is fresh only inside applicable grace. A committed
real launch atomically writes its generation-specific `launch_grace_until`.

Irrecoverable state loss may create a new `state_epoch` and one new
`first_managed_epoch` only through the exclusive
`StateLossQuarantineCreationDeltaV1` and together with module
`StateLossQuarantineV1.UNRESOLVED`. That constructor creates no childless
effect envelope, permit, attempt budget, usable debt/cycle, or launch authority.
No later poll in that epoch may renew the freshness anchor, and no
freshness/grace result may bypass quarantine. V1 has no automatic quarantine
retirement constructor. The guarantee is observational convergence after the
last state loss, not destructive recovery during lost provenance. Thus
repeated missing heartbeat and unavailable snapshots eventually yield:

```text
runtime.dominant = CONTRACT_ABSENT
presence = UNKNOWN
decision_now_epoch >= first_managed_epoch + resolved_launch_grace_seconds
=> freshness = STALE
=> stale_uncertainty = true
=> escalation_required = true
```

No additional chronic-failure counter is required: the nonrenewable anchor is
the finite visibility bound, while `STATE_PROVENANCE_LOST` still denies every
kill and launch. Repeated loss recreates or retains quarantine; it never
refreshes the childless three-attempt budget or erases possible debt. 87-B owns
durable projection of that required escalation, and 87-C may not activate this
classifier until that projection is capability-active.

## Shared process observation

### Capture and candidate universe

**ENFORCED by one immutable observer result:** The planner, post-teardown
resolver, and final launch barrier consume the same observer implementation and
the same closed recognition rules. They may capture at different times, but may
not maintain different definitions of “wrapper may exist.”

Each capture has:

```text
ProcessObservationV1 {
  capture_id: CaptureIdV1
  availability: COMPLETE | INCOMPLETE | UNAVAILABLE
  observer_reasons: tuple[ObserverFailureV1]
  coverage: ObserverCoverageSignatureV1 | null
  recorded_identity: ABSENT | PRESENT_MATCH | AMBIGUOUS_OR_REUSED | UNKNOWN
  candidates: full tuple[RelevantCandidateV1]
  active_child_availability: COMPLETE | INCOMPLETE | UNAVAILABLE
  active_child_rows: full tuple[ActiveChildRowV1]
  active_child_failures: tuple[
    SNAPSHOT_UNAVAILABLE | OBSERVATION_INCONSISTENT
  ]
}
```

`ObserverFailureV1` is the closed ordered enum
`SNAPSHOT_UNAVAILABLE`, `COVERAGE_INCOMPLETE`,
`OBSERVATION_INCONSISTENT`. `coverage` is non-null only when both required
coverage channels are complete. Failure reasons are deduplicated in that order.
`active_child_availability`, `active_child_rows`, and
`active_child_failures` form an independently derived closed projection
consumed only by `ActiveChildObservationV1`. Its allowed combinations mirror
the three availability forms above: complete has the full normalized row tuple
and no failure; unavailable has an empty row tuple and only
`SNAPSHOT_UNAVAILABLE`; incomplete retains all safely representable rows and
has only `OBSERVATION_INCONSISTENT`. Both availability projections carry a
failure source-equal to a global raw snapshot acquisition failure. A presence-only candidate parse,
recognition, ownership, or deduplication defect cannot alter the active-child
projection; an active-child-only lineage/matcher defect cannot alter
`availability`, `observer_reasons`, `candidates`, wrapper presence, or
targetability.

The private observer constructor permits only:

- `COMPLETE` with non-null coverage and no observer failure;
- `UNAVAILABLE` with null coverage and failures
  `(SNAPSHOT_UNAVAILABLE, COVERAGE_INCOMPLETE)`; or
- `INCOMPLETE` with null coverage, `COVERAGE_INCOMPLETE`, and optional
  `OBSERVATION_INCONSISTENT`.

Any attempted mixed representation normalizes to the third form with
`OBSERVATION_INCONSISTENT`.

`RelevantCandidateV1` includes every recognized same-agent/same-root
`wrap`/`wait` process and every process that cannot be excluded from that set
because its root, command line, launch shape, or recorded identity is
unreadable or ambiguous. Known foreign evidence may be retained diagnostically
outside `ProcessObservationV1.candidates`; it is neither authority evidence nor
input to `RecoveryConditionFingerprintV1`.

Before classification, the observer groups candidate rows by PID. Full-field-matching
duplicates, including the same PID/start guard and every derived field, collapse
to one candidate. Two rows for the same PID that disagree on start guard,
agent/root match, shape, ownership, or failure-code tuple make the observation
`INCOMPLETE / OBSERVATION_INCONSISTENT`. Rows with null or ambiguous PID/start
identity never collapse merely because their remaining fields match. The
surviving candidates sort by numeric PID with null last, then NFC UTF-8
start-guard bytes with null last, then their full `CanonicalJsonV1` bytes. Thus
duplicate enumeration cannot create an extra target, while conflicting
enumeration cannot be killed.

The observer constructor rejects internal contradictions before authority
classification. In particular, a `PRESENT_MATCH` recorded identity must map to
exactly one relevant candidate with the same PID/start guard, and a candidate
with that guarded identity cannot coexist with recorded-identity `ABSENT`.
Rejected observations remain representable as `availability=INCOMPLETE` with
`observer_reasons` containing `OBSERVATION_INCONSISTENT`; they never become
authority.

The authority classifier uses the full relevant-candidate set. Diagnostic
truncation is permitted only after classification and only under the
fingerprint rules below.

### Total `WrapperPresence`

**ENFORCED by this precedence, evaluated top to bottom:**

1. `UNAVAILABLE` or `INCOMPLETE` capture, `UNKNOWN` or ambiguous/reused
   recorded identity, any relevant unreadable/ambiguous candidate, or any
   observer inconsistency yields `UNKNOWN`.
2. Otherwise, a nonempty relevant set containing any definitely live
   same-agent candidate that lacks positive ownership or a PID/start guard
   yields `PRESENT_UNTARGETABLE`.
3. Otherwise, a nonempty relevant set in which every candidate is positively
   owned and start guarded yields `PRESENT_TARGETABLE`.
4. Otherwise, complete coverage, an empty relevant set, and positive absence
   of the guarded recorded identity yields `ABSENT`.
5. Any input not matched above yields `UNKNOWN / OBSERVATION_INCONSISTENT`.

No capture matches more than one state after this precedence. In particular:

- one targetable wrapper plus one unreadable candidate is `UNKNOWN`;
- one targetable wrapper plus one readable but unowned same-agent candidate is
  `PRESENT_UNTARGETABLE`;
- only a nonempty set of fully owned, guarded candidates is
  `PRESENT_TARGETABLE`; and
- an empty but incomplete capture is `UNKNOWN`, never `ABSENT`.

`WrapperPresenceResultV1` also carries a deduplicated tuple of closed reason
codes in the following prescribed order:

1. `SNAPSHOT_UNAVAILABLE`
2. `COVERAGE_INCOMPLETE`
3. `OBSERVATION_INCONSISTENT`
4. `COMMAND_UNREADABLE`
5. `ROOT_UNREADABLE`
6. `LAUNCH_SHAPE_AMBIGUOUS`
7. `PID_START_AMBIGUOUS`
8. `IDENTITY_REUSED`
9. `RECORDED_IDENTITY_UNKNOWN`
10. `VISIBLE_UNOWNED`
11. `ALL_TARGETABLE`
12. `COMPLETE_EMPTY`

The constructor includes every matching reason, deduplicated in that order:

- `SNAPSHOT_UNAVAILABLE` iff availability is `UNAVAILABLE`;
- `COVERAGE_INCOMPLETE` iff availability is not `COMPLETE` or coverage is null;
- `OBSERVATION_INCONSISTENT` iff the observer emitted that failure, a candidate
  has that failure code, or the classifier reaches its defensive final branch;
- each of `COMMAND_UNREADABLE`, `ROOT_UNREADABLE`,
  `LAUNCH_SHAPE_AMBIGUOUS`, and `PID_START_AMBIGUOUS` iff at least one relevant
  candidate has that failure code;
- `IDENTITY_REUSED` iff the recorded identity is `AMBIGUOUS_OR_REUSED` or a
  candidate has that failure code;
- `RECORDED_IDENTITY_UNKNOWN` iff recorded identity is `UNKNOWN`;
- `VISIBLE_UNOWNED` iff the final presence is `PRESENT_UNTARGETABLE`;
- `ALL_TARGETABLE` iff the final presence is `PRESENT_TARGETABLE`; and
- `COMPLETE_EMPTY` iff the final presence is `ABSENT`.

No conforming implementation may emit only a highest-ranked reason. Adding or
changing a reason code or predicate requires a schema version change.

### Closed `TargetabilityProof`

**ENFORCED by the presence constructor:**

```text
TargetabilityProofV1 =
  COMPLETE(capture_id, candidate_digest, nonempty canonical targets)
  | INCOMPLETE(reason_codes)
  | NO_TARGETS(capture_id)
```

Each canonical target is the following closed object:
`{"pid": <integer 1..4294967295>, "start_guard": <NFC UTF-8 string of at most
128 bytes>}` and appears in the same canonical order as its relevant candidate.

`COMPLETE` exists if and only if:

- presence is `PRESENT_TARGETABLE`;
- the capture is complete;
- the relevant-candidate set is nonempty;
- every relevant candidate has positive ownership and a nonempty PID/start
  guard;
- there is a bijection from every relevant candidate to exactly one canonical
  target; and
- there are no extra or duplicate targets.

`ABSENT` produces `NO_TARGETS`. `PRESENT_UNTARGETABLE` and `UNKNOWN` produce
`INCOMPLETE`. Partial targets are retained only as bounded diagnostics; they
never cross the authority boundary. The executor rechecks each start guard
immediately before termination (`src/agenttalk/supervisor.py:6073-6085`).

`candidate_digest` is SHA-256 over the domain
`agenttalk.supervisor.targetability-candidates.v1\0` followed by the complete,
byte-identically deduplicated candidate sequence (not the first-eight diagnostic
truncation) after the per-field bounds defined under
`RecoveryConditionFingerprintV1`, each canonical object encoded as a four-byte
big-endian length plus bytes.

### Complete owned-wrapper tree

**ENFORCED by the
[normative module](DESIGN-87A-owned-childless-wrapper-authority.md):** The
87-A adapter maps a merged task #120 Windows `owned_process_tree_v2` snapshot
into at most 64 `OwnedTreeTargetV1` values, each carrying an
`OwnedExactStartGuardV1` and nonce ownership, requires
`OwnedExactStartGuardV1` for every live row, and rejects every incomplete or
incompatible snapshot. A valid Linux `linux:<boot_id>:<start_ticks>`
process-identity token remains observation/barrier input; no macOS
process-identity-token mapping is declared. No non-Windows named path constructs a destructive
owner/target tuple or authority; with quarantine `NONE`, each fresh path returns
`CAPABILITY_UNAVAILABLE(EXACT_TARGET_EXECUTOR_UNAVAILABLE)` before a fresh
reservation. The adapter may persist only an inert
`ExactTargetExecutorBindingV1` inside `ChildlessEffectEnvelopeV1`. A
reservation/closure/spawn phase, debt-only state, ambiguous launch, or retired
attempt inherited onto a host that cannot serve its bound binding is not
reconstructed as fresh authority: checked state cannot produce the non-serializable
`CurrentExactTargetExecutorWitnessV1`, and only a fresh matching witness can
construct `ExactTargetExecutorPermitV1`. Without that permit, no
`ExecutableOwnedTargetSetV1`, `PermitBoundChildlessMutationV1`,
`ChildlessExternalEffectCallV1`, or effect receipt can exist. Ordinary
observation remains independent. Unresolved quarantine follows its separate
fail-closed path and has no automatic V1 retirement transition.
The closure successor separately supplies the action-scoped
non-destructive creation closure and effect-linearized adapters. Missing or
unverifiable observation produces `OwnedWrapperTreeObservationV1.INCOMPLETE`;
an unavailable target-executor witness or successor produces `CAPABILITY_UNAVAILABLE`;
a name or pattern never supplies ownership.

## Physical absence and launch timing

### Separate values

**ENFORCED by separate constructors:**

```text
PhysicalAbsenceProofV1 =
  UNCONFIRMED
  | CONFIRMED(AbsenceConfirmationV1)

LaunchTimingEligibilityV1 =
  NOT_ELIGIBLE
  | AUTOMATIC_HEARTBEAT_DUE
  | MANUAL_AUTHORIZED_BYPASS
```

Only `AbsenceConfirmationStateV1.CONFIRMED` constructs
`PhysicalAbsenceProofV1.CONFIRMED`. `EMPTY`, `OBSERVED_ONCE`, and `CONSUMED`
construct `UNCONFIRMED`; the complete confirmation retained inside `CONSUMED`
is audit/binding evidence and can never restore replacement authority.

Physical absence reads only ordinary process observations and managed identity.
Heartbeat, launch grace, restart marker, cooldown, and recovery backoff cannot
change it. Conversely, timing eligibility cannot manufacture physical absence.

Under 87-A's current timing policy:

- two qualifying `ABSENT` polls may confirm physical absence while heartbeat is
  `FRESH`;
- `FRESH + CONFIRMED + no authorized manual bypass` remains automatic `HOLD`;
- `FRESH + CONFIRMED + MANUAL_AUTHORIZED_BYPASS` permits manual no-kill
  relaunch; and
- `STALE after grace + CONFIRMED` permits automatic no-kill relaunch.

This removes Revision 2's contradiction: `NOW_ABSENT_CONFIRMED` no longer both
requires stale heartbeat and permits a fresh manual bypass. Confirmation is
physical; eligibility is temporal/policy evidence.

### Canonical state serialization

**ENFORCED by one shared codec:** Every “canonical JSON” or “canonical
serialized bytes” reference in 87-A means `CanonicalJsonV1`: values are limited
to objects, arrays, strings, integers, Booleans, and null; strings are Unicode
NFC; object keys sort by their NFC UTF-8 bytes; arrays emit elements in specified
order; integers use shortest base-10 with no leading zero or plus sign; and
`true`, `false`, and `null` are lowercase. There is no whitespace.

String encoding follows these byte-level rules: printable ASCII is literal except `"` and `\`, which
use `\"` and `\\`; `/` is never escaped; backspace, tab, LF, form feed, and CR
use `\b`, `\t`, `\n`, `\f`, and `\r`; other controls use lowercase
`\u00xx`; and every non-ASCII code point uses lowercase four-hex-digit UTF-16
`\u` escapes, with a high/low surrogate pair for a non-BMP code point. Floats,
negative zero, and NaN/Infinity are forbidden. The resulting ASCII text is
encoded as UTF-8. No caller may substitute a private serializer.

### Capture identity and coverage equality

**SPECIFIED for derivation inside the checked state owner required from task
#115:**

```text
CaptureIdV1 {
  state_epoch: lowercase hyphenated UUID string
  agent_key: NFC canonical agent/root string
  ordinary_poll_sequence: uint64
  capture_ordinal: uint16
}
```

For an ordinary observation, #115 computes the prospective `CaptureIdV1` at
begin from the checked predecessor and binds it into the private lineage before
acquisition. The installed observer writes that source-equal ID into
`ProcessObservationV1`. Persisted `ordinary_poll_sequence` advances only if the
matching receipt wins commit; commit validates and preserves the ID rather than
restamping the completed capture. A stale acquisition cannot be rebound to a
later poll identity.

The canonical state owner increments `ordinary_poll_sequence` once for each
committed ordinary observation of that agent. The planner's ordinary capture
has `capture_ordinal=0`. Reusing a cached capture preserves its ID and cannot
advance confirmation. Post-teardown and final-barrier captures have nonzero
ordinals and never count as ordinary absence polls.

The checked owner resets `next_capture_ordinal=1` in the same transaction that
increments `ordinary_poll_sequence`. Before every nonordinary closure,
post-action, reload-reconciliation, or final-barrier capture, it atomically
reserves the current value as that capture's ordinal and increments the stored
value. The capture's ID must be source-equal to that reserved `CaptureIdV1`; a caller may not
invent or reuse an ordinal. `next_capture_ordinal=65536` is a typed incomplete
hold until the next ordinary poll, never wraparound. Thus concurrent callers
cannot assign the same nonzero ID, and every reload post-action or reconciliation
capture has a deterministic identity even though it occurs mid-poll. The
structural reload path that observes matching `RELEASED` performs no residual
capture; it finalizes conservatively and leaves residual discovery to the next
ordinary ordinal-zero poll.

Only a coverage signature with both command-line and recorded-identity channels
complete can qualify:

```text
ObserverCoverageSignatureV1 {
  schema: "wrapper-observer-coverage/v1"
  platform: "WINDOWS"
  process_source: "WIN32_PROCESS_CIM"
  process_row_schema: uint16
  wrap_parser_schema: uint16
  wait_parser_schema: uint16
  ambiguity_scan_schema: uint16
  pid_start_guard_schema: uint16
  command_line_coverage: "complete"
  recorded_identity_coverage: "complete"
}
```

The initial value of every `*_schema` field is `1`; a semantic parser,
recognition, row-shape, or PID/start-guard change increments its owning field.
A new platform or process source requires `ObserverCoverageSignatureV2`.
Equality requires byte-identical `CanonicalJsonV1` bytes. A version or capability
change is unequal even when both captures happen to be empty.

### Typed one-use confirmation

**SPECIFIED by this persisted closed state and #115's owner-private reducer over
one commit-custody-bound sealed ordinary-observation receipt:**

```text
AbsenceConfirmationStateV1 =
  EMPTY
  | OBSERVED_ONCE(sample)
  | CONFIRMED(confirmation)
  | CONSUMED(confirmation, launch_reservation_id)

AbsenceSampleV1 {
  agent_key: NFC canonical agent/root string
  state_epoch: lowercase hyphenated UUID string
  managed_generation: NFC UTF-8 string of at most 128 bytes | null
  guarded_launcher_identity_digest: 64 lowercase hex characters | null
  recognition_config_digest: 64 lowercase hex characters
  coverage: ObserverCoverageSignatureV1
  capture_id: CaptureIdV1
}

AbsenceConfirmationV1 {
  schema: "absence-confirmation/v1"
  confirmation_id: 64 lowercase hex characters
  agent_key: NFC canonical agent/root string
  state_epoch: lowercase hyphenated UUID string
  managed_generation: NFC UTF-8 string of at most 128 bytes | null
  guarded_launcher_identity_digest: 64 lowercase hex characters | null
  recognition_config_digest: 64 lowercase hex characters
  coverage: ObserverCoverageSignatureV1
  first_capture_id: CaptureIdV1
  second_capture_id: CaptureIdV1
  latest_compatible_capture_id: CaptureIdV1
}
```

`launch_reservation_id` is a lowercase hyphenated UUID minted by the checked
state owner. The optional guarded-launcher digest is SHA-256 over
`agenttalk.supervisor.guarded-launcher-identity.v1\0` plus
`CanonicalJsonV1` bytes for the following closed object:

```text
{
  "launcher_pid": <integer 1..4294967295>,
  "launcher_start_guard": <NFC UTF-8 string of at most 128 bytes>
}
```

It is null only when no guarded managed launcher identity exists. The
recognition-config digest is SHA-256 over
`agenttalk.supervisor.wrapper-recognition-config.v1\0` plus
`CanonicalJsonV1` bytes for the following closed object:

```text
{
  "schema": "wrapper-recognition-config/v1",
  "agent_key": <NFC canonical agent/root string>,
  "recognized_shapes": ["WAIT", "WRAP"],
  "recognition_rules_schema": 1
}
```

Any semantic recognition-rule or effective recognition-config change requires
a new `recognition_rules_schema` value before capture.

`confirmation_id` is SHA-256 over the domain
`agenttalk.supervisor.absence-confirmation.v1\0` plus `CanonicalJsonV1` bytes
for the following closed object:

```text
{
  "schema": "absence-confirmation/v1",
  "agent_key": <agent_key>,
  "state_epoch": <state_epoch>,
  "managed_generation": <managed_generation>,
  "guarded_launcher_identity_digest": <digest or null>,
  "recognition_config_digest": <digest>,
  "coverage": <ObserverCoverageSignatureV1>,
  "first_capture_id": <CaptureIdV1>,
  "second_capture_id": <CaptureIdV1>
}
```

`latest_compatible_capture_id` is deliberately excluded so extension does not
change the one-use proof identity.

The binding tuple comprises, in order, `(agent_key, state_epoch, managed_generation,
guarded_launcher_identity_digest, recognition_config_digest, coverage)`.
“Compatible” means field-for-field equality of that tuple.

**ENFORCED transitions, evaluated top to bottom:**

| Prior state and input | Next state |
| --- | --- |
| Any state + changed agent key, state epoch, managed generation, guarded launcher identity, recognition config, or coverage | Discard the prior state, then reduce the current input from `EMPTY`: a qualifying ordinary `ABSENT` becomes `OBSERVED_ONCE(input)`; every other input becomes `EMPTY`. |
| `CONFIRMED` + atomic launch reservation | `CONSUMED(confirmation, reservation_id)`; retain the complete binding for later comparisons. |
| Any state other than `CONFIRMED` + launch-reservation attempt | Refuse the reservation and leave state byte-identical. |
| `CONSUMED` + successful launch whose new guarded managed identity is not yet atomically committed | Remain `CONSUMED`; success alone cannot revive the proof. |
| `CONSUMED` + atomic commit of the new guarded managed identity | The binding-change rule above yields `EMPTY`. |
| Any unconsumed state + `PRESENT_TARGETABLE`, `PRESENT_UNTARGETABLE`, `UNKNOWN`, unavailable/incomplete observation, or any other nonqualifying ordinary observation | `EMPTY`. |
| `CONSUMED` + a nonqualifying observation under the same binding | Remain `CONSUMED`. |
| `EMPTY` + qualifying ordinary `ABSENT` | `OBSERVED_ONCE(input)` |
| `OBSERVED_ONCE` + replay of the same capture ID | State remains byte-identical; never confirmed. |
| `OBSERVED_ONCE` + a qualifying `ABSENT` from the next committed ordinary poll, distinct capture ID, and matching binding/coverage | `CONFIRMED(first, second)` |
| `OBSERVED_ONCE` + any other qualifying ordinary `ABSENT` | New `OBSERVED_ONCE(input)`; changed compatibility or a sequence gap cannot complete the old sample. |
| `CONFIRMED` + replay of its first, second, or latest-compatible capture ID, or an older capture ID | State remains byte-identical; replay cannot extend freshness. |
| `CONFIRMED` + the next consecutive compatible ordinary `ABSENT` after `latest_compatible_capture_id` | Retain `confirmation_id`; advance `latest_compatible_capture_id`. |
| `CONFIRMED` + any other qualifying ordinary `ABSENT` | New `OBSERVED_ONCE(input)`; changed compatibility or a sequence gap starts a new proof. |
| `CONSUMED` + any qualifying ordinary `ABSENT` under the same binding | New `OBSERVED_ONCE(input)`; it is the first of two new polls. |
| Any state + an input not covered above | Fail closed to `EMPTY`, except `CONSUMED` remains `CONSUMED`. |

“Consecutive” means adjacent committed `ordinary_poll_sequence` values for the
same `agent_key` and `state_epoch`. Heartbeat, runtime-reason, and restart-marker
changes do not reset physical evidence. A qualifying capture has
the source-equal #115 begin-bound ordinal-zero `CaptureIdV1` validated unchanged at
commit, complete coverage, presence `ABSENT`, and the reducer's agent key and
state epoch. Dry run and recovery-policy `HOLD` do not request a
launch reservation and therefore do not consume.

The launch reservation consumes confirmation atomically before the final
barrier. A barrier veto, spawn failure, or ambiguous launch result leaves it
consumed. A new confirmed proof requires two new ordinary captures. Successful
launch clears the consumed tombstone only after a new guarded managed identity
is committed.

### Post-teardown observations

**ENFORCED by separate reducer entry points:** Post-teardown observations never
call the ordinary absence reducer.

- A successful guarded teardown plus a fresh complete clear capture resolves
  its action-scoped conditional proof for that authority ID.
- Failed teardown, any survivor, unavailable/incomplete capture, or changed
  coverage resolves the conditional proof to `NONE`.
- No post-teardown capture, including a clear scan following a failed teardown
  result, counts as either poll for later no-kill confirmation.
- After failure, the next qualifying ordinary `ABSENT` poll starts at
  `OBSERVED_ONCE`.

This is the normative answer to Lens A's failed-post-teardown-scan question.

## Authority derivation

### Global execution eligibility

**ENFORCED after the pure classifier observation delta and before
automatic/manual selection or any recovery-authority delta:**

```text
ExecutionEligibilityV1 =
  DRY_RUN
  | STATE_PROVENANCE_LOST
  | KILL_SWITCH_ACTIVE
  | SUPERVISOR_STOPPED
  | ACTIONS_DISABLED
  | AGENT_NOT_REPORTED
  | AUTO_RESTART_DISABLED
  | ELIGIBLE

PriorEffectFenceClearanceEligibilityV1 =
  DRY_RUN
  | STATE_PROVENANCE_LOST
  | KILL_SWITCH_ACTIVE
  | SUPERVISOR_STOPPED
  | ELIGIBLE

ExecutionGateCaptureV1 {
  dry_run: bool
  state_loss_quarantine_id: lowercase hyphenated UUID | null
  kill_switch: CLEAR | ACTIVE_OR_UNREADABLE
  supervisor_instance:
    CURRENT(token_digest, guarded_pid, guarded_start) | STOPPED_OR_UNREADABLE
  action_latch: ENABLED(action_epoch: uint64) | DISABLED
  report_membership: PRESENT | ABSENT | UNREADABLE
  auto_restart: ENABLED | DISABLED_OR_UNREADABLE
  snapshot_id: Hex64
}
```

`PriorEffectFenceClearanceEligibilityV1` is the deliberately narrower gate for
the no-effect `PRIOR_EFFECT_FENCE_CLEAR` reconciliation only. It requires
`dry_run=false`, no state-loss quarantine, a positively clear kill switch, and
one current supervisor instance. It deliberately ignores `action_latch`, report
membership, and `auto_restart`: clearing the prior-effect fence grants no action
and normal selection still evaluates the full `ExecutionEligibilityV1`
afterward. Every other authority/effect transition uses the full gate.

**ENFORCED capture sources:** `dry_run` is the immutable invocation flag.
`kill_switch` is `CLEAR` only when the supervisor kill-switch path is
positively absent; presence, file-kind ambiguity, or path/I/O failure is
`ACTIVE_OR_UNREADABLE`. `supervisor_instance` is `CURRENT` only when the
executor is in its in-memory `RUNNING` phase and the freshly read instance
record is source-equal to its claim token plus guarded PID/start; shutdown,
release, mismatch, or read/validation failure is `STOPPED_OR_UNREADABLE`.

The executor owns an atomic `action_latch`. It becomes enabled with a fresh
monotonic `action_epoch` only after instance claim and action-subsystem
initialization. It is set disabled before shutdown/claim release and after a
fatal executor-state failure. The action issuer takes its shared read guard;
shutdown/disabling takes the exclusive write guard. `report_membership` comes
from one freshly validated report image and is `UNREADABLE` on read/schema
failure. `auto_restart` comes from the same configuration-lock image used for
manual authorization and is enabled only for the Boolean value `true`; a
read/schema failure is disabled.

`snapshot_id` hashes
`agenttalk.supervisor.execution-gate-snapshot.v1\0` plus `CanonicalJsonV1` of
the following closed object:

```text
{
  "schema": "execution-gate-snapshot/v1",
  "dry_run": <bool>,
  "state_loss_quarantine_id": <lowercase hyphenated UUID | null>,
  "kill_switch": "CLEAR" | "ACTIVE_OR_UNREADABLE",
  "supervisor_instance": {
    "state": "CURRENT" | "STOPPED_OR_UNREADABLE",
    "token_digest": <Hex64 | null>,
    "pid": <integer 1..4294967295 | null>,
    "start_guard": <bounded nonempty NFC string | null>
  },
  "action_latch": {
    "state": "ENABLED" | "DISABLED",
    "epoch": <uint64 | null>
  },
  "report_membership": "PRESENT" | "ABSENT" | "UNREADABLE",
  "auto_restart": "ENABLED" | "DISABLED_OR_UNREADABLE"
}
```

`STOPPED_OR_UNREADABLE` requires null token digest, PID, and start guard;
`DISABLED` requires null epoch. The claim token itself is represented only by
its SHA-256 digest. The payload excludes timestamps and unrelated report/config
fields, so equivalent recaptures match.

Checked-state processing precedes this constructor. In dry run, missing,
corrupt, torn, or rollback-unproven outer state produces only a simulated
`STATE_PROVENANCE_LOST`/`POLICY_HELD` result and no delta. In a non-dry-run
invocation the same outer-state failure admits only the exclusive
`StateLossQuarantineCreationDeltaV1`; no observation or effect delta is
composed with it. For a syntactically valid state, an existing `UNRESOLVED` quarantine
precedes semantic childless-envelope validation. A malformed envelope or
execution/debt/cycle/continuation pairing selects `RETAIN_INVALID_FENCE` before
any witness or permit construction. Current-host executor unavailability
cannot relabel malformed state. Only a valid envelope may be matched against a
fresh witness.

The eligibility constructor then evaluates the displayed variant precedence:
`DRY_RUN` from `dry_run`; `STATE_PROVENANCE_LOST` when module
`StateLossQuarantineV1` is `UNRESOLVED`; `KILL_SWITCH_ACTIVE` unless kill switch
is clear; `SUPERVISOR_STOPPED` unless the instance is current;
`ACTIONS_DISABLED` when the action latch is disabled or report/config capture
is unreadable; `AGENT_NOT_REPORTED` for report absence;
`AUTO_RESTART_DISABLED` unless the value is Boolean true; otherwise `ELIGIBLE`. Only
`ELIGIBLE` may reserve/consume authority, mutate a restart marker, teardown,
launch, seed managed identity, or update launch/backoff/readiness state.

`DRY_RUN` may compute and display a pure simulated decision but discards every
owner-private observation mutation and consumes or poisons its observation
commit custody;
it performs no state/event/marker/config persistence. For every other value,
the checked owner may first consume one
`OrdinaryObservationCommitCustodyV1` and derive/commit the observation-only
projection, including the nonrenewable freshness anchor, continuity, and
confirmation resets, inside the same #115 RMW transaction. That observation
commit cannot reserve or consume an absence/manual proof or alter
manual execution, launch, backoff, readiness, marker, or configuration state.
This ordering lets `AGENT_NOT_REPORTED` converge to stale uncertainty instead
of recreating the F7 first-launch outage.

`KILL_SWITCH_ACTIVE` then forbids every new recovery and
marker/config/seeding mutation. The future 87-B narrow observational path may
persist and project a mandatory condition under the kill switch; 87-A grants
that path no recovery authority. The other noneligible values are visible
no-action holds and perform no new recovery-authority or marker mutation.

`STATE_PROVENANCE_LOST` is stronger than every recovery policy. It denies
automatic and manual teardown, launch, closure acquisition, attempt
increment/reset, debt clear, marker consumption, managed-identity commit, and
grace-based recovery. V1 permits only pure observation, quarantine retention,
and mandatory attention after initial quarantine creation. It defines no
automatic different-owner/extinction retirement. Manual
force/acknowledgement cannot override it; attended handling is required.

Cleanup of an already-persisted childless envelope is governed by the same
constructor boundary as fresh action. A valid inert
`ExactTargetExecutorBindingV1` plus a freshly captured
`CurrentExactTargetExecutorWitnessV1` first passes static executor preflight.
After acquiring the effect guard when the operation requires it, the module may
mint exactly one private lineage in `AVAILABLE`. Permit issuance atomically
moves its sole custody to `OUTSTANDING` in the one unexported atomic owner cell;
a second permit from that acquisition cannot exist until the first stage returns
custody. The owner cell is not reachable from an effect value. The permit, binding,
targets, mutation, call, receipt, and all nested members are transitively
immutable, alias-free, and privately sealed; they carry only an immutable opaque
custody proof. A synchronous adapter atomically moves the separate owner cell
through `CALL -> DISPATCHING -> PLAN_OWNED -> INVOKING -> RECEIPT` while
carrying that proof; only the `DISPATCHING -> PLAN_OWNED` winner may construct a
plan, and only the `PLAN_OWNED -> INVOKING` winner may enter the native body.
For owned-tree `STOP_TREE`, this is the module's private submission/admission/
invocation boundary.
The configured-agent and ephemeral dispatch variants each use the one
action-scoped owner minted in `READY` by their source-bound #115 checked transition;
their private constructors must first win `READY -> CALL` and cannot mint an
owner from persisted provenance. Concurrent or sequential duplicate constructors
therefore emit at most one call/submission, and aliases/replay at every later
call/admission/invocation/receipt-custody handoff lose with zero effect. A
sealed dispatcher receipt alone is inert; its private custody handle is the
only association to the bound owner and the only input accepted for receipt
consumption. Every synchronous native exit returns a matching receipt, returns
one invocation-bound `ACTIONS_DISABLED_NO_EFFECT` or
`NATIVE_ENTRY_FAILED_NO_EFFECT` result, or poisons custody exactly once. A
pre-frontier exception returns authority only through that bound positive
no-effect result; an unknown/possibly crossed native-effect frontier poisons the
owner and never licenses reissue. A receipt-mutation permit consumes the bound receipt-held
proof and atomically moves the same OUTSTANDING call issuance from RECEIPT to
PERMIT; it does not require AVAILABLE custody or create a new issuance.
Releasing, losing, transferring, replacing, or poisoning the lineage
invalidates every corresponding holder. No state-only
pre-barrier release, takeover, debt-only effect finalization, retired-attempt
cleanup, nonordinary capture allocation, finalization, `Stop-Tree`, or launch
accepts raw persisted state or IDs. Static witness unavailability makes all of
those objects unconstructible and emits `CAPABILITY_UNAVAILABLE`/`POLICY_HELD`;
a rejected stale/replayed/mismatched private operand performs zero effect and
follows the module's reload/reject rule instead.

The owner-private ordinary-observation transaction remains independent of this
boundary and may advance only its closed projection before the hold is returned. Cleanup may
still run under later kill-switch, action-latch, report-membership, or
auto-restart holds only when its permit-bound call/mutation and the module's
exclusive continuation/effect guard are both valid. That exception is
non-destructive fence cleanup, not new recovery authority.

These are population/executor gates and manual origin cannot override them.
They differ from the later per-agent configuration-blocked and lead-loop
stand-down holds, which an eligible authorized manual request may override.
Task #114 owns moving the shipped kill-switch exit behind instance claim so the
87-B observational exception can exist.

**ENFORCED action-time fence:** Reservation stores the eligible
`snapshot_id`. Under the configuration lock and action-latch read guard, the
executor recaptures eligibility and requires `ELIGIBLE` with the same semantic
snapshot ID immediately before issuing any guarded termination and again
immediately before `Start-Process`; manual origin also repeats live
authorization. At both manual fences, raw capture must still be
`PRESENT_VALID` with request ID and `revision_sha256` source-equal to the
reservation, and authorization must still be `AVAILABLE` with
`snapshot_id == reservation.authorization_snapshot_id`. Deletion, replacement,
unreadability, or any semantically different authorization snapshot is a
veto—even when the new requester would independently be authorized.

The executor reruns the origin-applicable policy gates and performs no
intervening wait between the final check and OS action issuance. It retains the
configuration lock and action-latch read guard through issuance, then releases
them before any process-completion wait. A mismatch or noneligible result
aborts the next action and records a typed veto. A non-childless action releases
A no-kill non-childless reservation may release through its private
`NonChildlessAuthorityDeltaV1` transition. A kill-bearing configured action may
do so only in the original live invocation while the original source-bound custody remains
`READY`, by atomically closing that custody as positive no-effect in the same
checked commit. Custody unavailability, any later owner stage, or reload instead
derives `ConfiguredPreBarrierOwnerLossHoldV1` and cannot release.
A named childless pre-closure veto instead requires a matching
`PRE_BARRIER_RELEASE/STATE_MUTATION` permit to remove its effect envelope,
release its reservation, write its same-poll terminal, and consume no automatic
attempt. Static executor unavailability preserves the complete envelope
byte-identically; a stale
revision reloads/re-reduces, and any other rejected private operand performs no
mutation. Both branches leave any still-matching manual marker pending and any
one-use absence proof consumed.

`EPHEMERAL_TERMINAL` has no 87-A reservation and therefore does not borrow the
reservation `snapshot_id` rule. It uses the module's narrow
`EphemeralTerminalFinalActionGateV1`: after winning its private atomic dispatch
admission but before native-plan construction, the dispatcher requires the
same enabled action-latch epoch under the latch read guard and separately
requires the kill switch to remain clear. It retains the latch guard through
issuance and preserves the private native body's final kill-switch check. A
post-construction latch or kill-switch change consumes the call as a typed
pre-effect rejection, preserves persisted `next_entry` byte-identically, and produces no plan,
raw array, or effect. The disabled-latch exception above is non-destructive
fence cleanup and never authorizes this destructive ephemeral variant.
If the kill switch changes after the outer check or plan construction but before
the native body's final check, that body returns the invocation-bound typed
`ACTIONS_DISABLED_NO_EFFECT` result before lexical raw-array materialization.
The dispatcher consumes the call as
`REJECTED_NO_EFFECT(FINAL_ACTION_GATE_CHANGED)`, preserves `next_entry`
byte-identically, and
produces no receipt, raw array, or effect.
An exception while evaluating that gate or entering the private body maps to
`NATIVE_ENTRY_FAILED_NO_EFFECT` only when the private wrapper positively proves
the native-effect frontier was not reached. The configured and ephemeral owner
then becomes `REJECTED_NO_EFFECT(NATIVE_ENTRY_FAILED_NO_EFFECT)`; the childless
lineage instead receives one successor custody. An exception at/after the
frontier or at an unknown locus poisons with the module's closed variant-specific
effect-uncertain cause. A missing result or exception class is never a no-effect
oracle.

After a childless closure is held but before
`Stop-Tree`, the executor
instead commits `TREE_CLOSURE_RELEASING/CLOSURE_VETOED`, requests release using
the source-equal persisted attempt/closure pair, and may release the reservation only
after matching `RELEASED` reconciliation. `HELD` or `UNKNOWN` retains the
reservation and continuous attention. If teardown already completed and
closure/debt were safely cleared, the fresh pre-spawn fence still refuses
spawn; later recovery must rebuild ordinary absence proof.

**SPECIFIED abrupt-death split for non-childless dispatch:** Transient action
custody is not serialized and cannot itself prevent a restart retry. For
`EPHEMERAL_TERMINAL`, retry after a new #115 checked transition is intentionally
safe only for the Windows FILETIME-guarded kill subphase proved by merged #120: null
`OpenProcess` for a gone PID is a no-op (`587e7c1:8908-8911`), a different
creation FILETIME is refused (`8912-8913`), and the same FILETIME is the same
process instance terminated through that validated handle (`8912-8921`). The
fresh teardown barrier still decides whether archive may proceed
(`9592-9631`).

`CONFIGURED_AGENT_RELAUNCH` has the same target-local #120 kill semantics but no
authority to retry merely from its durable checkpoint. Loss of its transient
owner at `RESERVED/PRE_BARRIER` derives the source-bound owner-loss hold; there is no
reload remint, release-and-reserve, kill, or launch. The GONE-only attended
disposition is the only specified exit before
`ConfiguredPreBarrierRetrySuccessorV1` lands, and it installs the global prior-
effect fence rather than asserting the kill outcome. Only after the fresh
source-bound #120 barrier clears that fence may normal selection resume.

Launch replay remains independently unsafe. Merged code persists the configured
checkpoint before kill (`9377-9392`) and later persists launch state, calls
`Start-Process`, and only then records the PID (`9445-9477`). A crash in that
window can leave a wrapper live while replayable state remains. 87-A does not
claim an in-process token closes that durable gap. Automatic configured relaunch
is implementation- and activation-blocked on task #57's project-level singleton
per wrapped agent (launch lock). #120 is target-local identity evidence, not
retry authority or launch idempotence.

### Dominant-only automatic predicates

**ENFORCED by these equations:**

```text
UNCERTAIN_DOMINANTS = {
  CURRENT_UNKNOWN_SEQUENCE_REGRESSION,
  CURRENT_UNKNOWN_BINDING,
  CURRENT_UNKNOWN_STARTING_OVERRUN,
  CURRENT_UNKNOWN_ACTIVE_CHILD,
  CURRENT_BLOCKED_STALL,
  INVALID_CONTRACT,
  UNSUPPORTED_CONTRACT,
  CONTRACT_ABSENT,
  CURRENT_UNKNOWN_OTHER,
}

strict_recovery_due =
  runtime.dominant == CURRENT_TEARDOWN_PROOF
  or (
    runtime.dominant == CURRENT_STALE_RECOVERABLE
    and freshness == STALE
  )

automatic_teardown.allowed =
  strict_recovery_due
  and presence == PRESENT_TARGETABLE
  and targetability is COMPLETE

recovery_blocked =
  strict_recovery_due
  and presence in {PRESENT_UNTARGETABLE, UNKNOWN}

stale_uncertainty =
  freshness == STALE
  and (
    runtime.dominant in UNCERTAIN_DOMINANTS
    or presence == UNKNOWN
  )

escalation_required = recovery_blocked or stale_uncertainty
```

`COMPLETE` already guarantees a nonempty `OwnedTreeTargetV1` tuple whose start
guards are `OwnedExactStartGuardV1` values;
the automatic equation must not reconstruct that invariant.

### Provably-childless owned-wrapper authority

**ENFORCED module import:** The
[owned-childless module](DESIGN-87A-owned-childless-wrapper-authority.md)
constructs `PROVABLY_CHILDLESS_OWNED_WRAPPER` only from two complete
same-owner, same-basis child-absence captures whose nonrenewable
child-establishment guard is `CLOSED`, complete nonce-anchored tree
observation, and the current targetability proof, or from an outstanding
debt's source-equal immutable residual subset. It also owns the authority-facing
tree-closure contract, origin-neutral teardown debt, childless-only
three-attempt automatic cycle, and closed typed result/attention constructors.

This core consumes the module value without reconstruction. In the
entire module-defined `child_death_sourced_dominant` subset, generic strict
teardown is suppressed before selection. The named case is selectable only
when the separate module confirmation and full proof are complete; otherwise
the result is `HOLD`. This includes the reachable case where the banked
child-death counter is two but an earlier incomplete tree leaves the module
overlay at one. `BLOCKED` is likewise `HOLD`; generic strict teardown is not a
fallback. Manual-wins may change origin only by wrapping the same proof.
This is positive nonexistence evidence, not silence or staleness, and therefore
does not weaken #72.

### Manual marker disposition and overlap

The existing `Store.read_restart_request` collapses absent, malformed,
non-object, and `OSError` inputs to `None`
(`src/agenttalk/store.py:5724-5733`). It is not a conforming reader for 87-A.

**ENFORCED raw capture:**

```text
ManualMarkerCaptureV1 =
  ABSENT
  | PRESENT_VALID(marker: ManualRestartMarkerV1, revision_sha256: Hex64)
  | PRESENT_INVALID(reason: ManualMarkerInvalidReasonV1)
  | UNREADABLE(reason: ManualMarkerReadErrorV1)
```

`ManualMarkerInvalidReasonV1` is the closed ordered enum `SIZE`, `BOM`,
`UTF8`, `JSON`, `DUPLICATE_KEY`, `TOP_LEVEL`, `SCHEMA`, `FIELD`,
`REQUEST_ID`, `TIMESTAMP`. `ManualMarkerReadErrorV1` is `LOCK_FAILURE`,
`UNSAFE_FILE_KIND`, `IO_ERROR`, `PATH_RACE`, in that order. If more than one
failure is observable, the first in the applicable displayed order is emitted.

The reader acquires the existing configuration lock shared by marker write and
compare-clear, returns `ABSENT` only when the path is absent under that lock,
rejects a directory, symlink/reparse point, or other non-regular file, and
reads at most 16 KiB. Empty/oversized content is
`PRESENT_INVALID(SIZE)`. It requires UTF-8 without BOM, a single JSON top-level
object, and duplicate-key rejection. I/O or path-generation races are
`UNREADABLE`, never `ABSENT`. An accepted object carries SHA-256 of the captured
source bytes as `revision_sha256`; reservation and compare-clear match both request ID
and revision.

Implementation has one private `capture_manual_marker_locked` primitive whose
precondition is “configuration lock held” and one public wrapper that acquires
the lock. Write, reservation, and compare-clear call the private primitive;
they never recursively acquire the non-reentrant configuration lock.

**ENFORCED closed marker schema:** Unknown or missing keys are invalid.

```text
ManualRestartMarkerV1 {
  "schema_version": 1,
  "agent": AgentName,
  "request_id": RequestId,
  "source": "manual",
  "requested_by": AgentName,
  "authorized_by": AgentName,
  "authority_result": "authorized",
  "authority_reason": "operator_facing" | "sole_lead",
  "issued_at_epoch_ms": uint64,
  "expires_at_epoch_ms": uint64,
  "force_protected": bool,
  "force_protected_authorized": bool,
  "force_protected_authorized_by": AgentName | null,
  "acknowledge_live_protected_kill": bool,
  "acknowledge_live_protected_kill_authorized": bool,
  "acknowledge_live_protected_kill_by": AgentName | null,
  "reason": string
}
```

`AgentName` matches `\A[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\z`; marker `agent` is
source-equal to the configured target. `RequestId` matches `\Arr-[0-9a-f]{12}\z`,
matching the current producer shape. `requested_by == authorized_by`;
`authority_result == "authorized"`. `reason` is at most 1,024 UTF-8 bytes and
contains no NUL. For each protection/acknowledgement triple, a false requested
Boolean requires false authorization and null `*_by`; a true requested Boolean
requires true authorization and `*_by == requested_by`.

The version and integer millisecond timestamps replace the current unversioned,
mixed timestamp marker. 87-C owns producer migration and may not activate this
reader until every active producer writes this schema.

**ENFORCED clock and expiry:** The poll captures one UTC wall-clock integer
`decision_now_epoch_ms`.

```text
MANUAL_MARKER_TTL_MS_V1 = 300000
MAX_FUTURE_SKEW_MS_V1 = 30000

expires_at_epoch_ms == issued_at_epoch_ms + MANUAL_MARKER_TTL_MS_V1
issued_at_epoch_ms <= decision_now_epoch_ms + MAX_FUTURE_SKEW_MS_V1
not_expired = decision_now_epoch_ms < expires_at_epoch_ms
```

Both timestamps must be JSON integer values (not Boolean), addition must not
overflow `uint64`, and both displayed relations must hold. Overflow, a
mismatched expiry, or issue time beyond allowed future skew is
`PRESENT_INVALID(TIMESTAMP)` and therefore `INVALID_HELD`; it never reaches the
gate list as a valid marker. A structurally valid marker with
`decision_now_epoch_ms >= expires_at_epoch_ms` is `EXPIRED_HELD`.

The marker expires when decision time reaches the stored deadline. Five minutes is five times the
maximum 60-second restart cooldown: it permits a bounded retry window without
preserving destructive intent indefinitely. An expired marker remains a
visible manual hold until removed or replaced; it cannot fall through to
automatic teardown.

**ENFORCED live authorization revalidation:** Planning and immediate
pre-reservation execution independently capture:

```text
ManualAuthorizationSnapshotV1 =
  UNAVAILABLE(CONFIG_LOCK_FAILURE | CONFIG_UNREADABLE | CONFIG_INVALID)
  | AVAILABLE {
      agent: AgentName
      operator_facing: AgentName | null
      sole_lead: AgentName | null
      protected: bool
      snapshot_id: Hex64
    }
```

The constructor acquires the configuration lock and loads one configuration
image. Lock, read, or schema/roster/role failure returns `UNAVAILABLE` with the
displayed reason; unavailable authorization is always false and holds recovery.
For an available snapshot:

- `operator_facing` is the configured value only when it names a member of the
  validated roster, otherwise null;
- `sole_lead` is the roster member whose role case-folds to `lead` only when
  exactly one such member exists, otherwise null; and
- `protected` is true iff the target is `operator_facing` or any
  roster member whose role case-folds to `lead`. Multiple leads therefore all
  remain protected even though `sole_lead` is null.

These values come from the same configuration image as `auto_restart` and
cannot come from a cached report Boolean. `snapshot_id` hashes the domain
`agenttalk.supervisor.manual-authorization-snapshot.v1\0` plus
`CanonicalJsonV1` of the following closed object:

```text
{
  "agent": <AgentName>,
  "operator_facing": <AgentName | null>,
  "protected": <bool>,
  "sole_lead": <AgentName | null>
}
```

Planning and action-time revalidation both use this constructor.
For manual origin, each execution-gate/manual-authorization pair is derived
from one configuration-lock acquisition and one configuration image.

```text
base_authorized =
  snapshot is AVAILABLE
  and (
  if operator_facing is a valid roster member:
    requested_by == operator_facing
  else:
    sole_lead != null and requested_by == sole_lead
  )

basis =
  "operator_facing" when the first branch applies
  else "sole_lead"

base_revalidated =
  base_authorized
  and basis == marker.authority_reason

force_authorized =
  base_revalidated
  and marker.force_protected
  and marker.force_protected_authorized
  and marker.force_protected_authorized_by == requested_by

live_kill_ack_authorized =
  base_revalidated
  and requested_by == operator_facing
  and marker.acknowledge_live_protected_kill
  and marker.acknowledge_live_protected_kill_authorized
  and marker.acknowledge_live_protected_kill_by == requested_by
```

The second snapshot must be available and have the same `snapshot_id` as
planning or the candidate is discarded and reclassified. Stored authorization
is audit evidence, never current authority. This preserves the current base
authorization source (`src/agenttalk/supervisor.py:590-638` and `:965-985`).

Every transition spanning marker/config and supervisor state uses one lock
order: configuration lock, action-latch read guard when an execution snapshot
is involved, then task #115's checked state transaction; release in reverse.
Reservation rereads marker bytes/revision and current authorization under the
held configuration lock before committing state. No 87-A caller may acquire
configuration state or the action guard while already holding the checked
state lock. Shutdown takes the action-latch write guard without holding either
other lock. This makes marker replacement, authority/gate change, and state
reservation one fail-closed comparison without a lock cycle.

**ENFORCED candidate before acknowledgement applicability:**

```text
manual_candidate =
  KILL_THEN_RELAUNCH using the module DEBT_COMPLETION target witness
    if teardown_debt is OUTSTANDING
    and ChildlessTeardownAuthorityV1 is
        PROVABLY_CHILDLESS_OWNED_WRAPPER(mode=DEBT_COMPLETION)
  else SAFETY_HELD
    if teardown_debt is OUTSTANDING
  else RELAUNCH_ONLY
    if PhysicalAbsenceProofV1 is CONFIRMED
  else KILL_THEN_RELAUNCH using the module INITIAL target witness
    if child_death_sourced_dominant
    and ChildlessTeardownAuthorityV1 is
        PROVABLY_CHILDLESS_OWNED_WRAPPER(mode=INITIAL)
  else SAFETY_HELD
    if child_death_sourced_dominant
  else KILL_THEN_RELAUNCH
    if presence == PRESENT_TARGETABLE and targetability is COMPLETE
  else SAFETY_HELD

force_required =
  protected
  and manual_candidate in {KILL_THEN_RELAUNCH, RELAUNCH_ONLY}

live_kill_ack_required =
  protected
  and manual_candidate == KILL_THEN_RELAUNCH
  and selected_targets is nonempty
```

A protected confirmed-absence no-kill restart needs force authorization, but
not acknowledgement of a kill that will not occur. Freshness alone never makes
the live-kill acknowledgement applicable. Manual-wins origin selection does
not weaken childless execution evidence: an overlapping authorized marker
wraps the same initial-tree or debt-residual proof and the required action-time
recapture. Outstanding debt is evaluated first and blocks every relaunch-only
proof. With debt `NONE`, confirmed whole-wrapper absence is evaluated before
the live-wrapper child-death kill gate, so manual and automatic origins retain
the same no-kill `RELAUNCH_ONLY` recovery.

**STATED policy strengthening:** Shipped behavior requires the protected
live-kill acknowledgement only while heartbeat is fresh
(`src/agenttalk/supervisor.py:4452-4458`). 87-A deliberately requires it for
every selected protected `KILL_THEN_RELAUNCH`, fresh or stale. Once a
destructive action is selected, heartbeat age does not make killing a live
protected process less destructive. This does not widen authority; it adds a
hold. The confirmed-absence `RELAUNCH_ONLY` case remains exempt because it
kills nothing.

```text
ManualMarkerDispositionV1 =
  ABSENT
  | INVALID_HELD(ManualMarkerInvalidReasonV1)
  | UNREADABLE_HELD(ManualMarkerReadErrorV1)
  | CONSUMED
  | EXPIRED_HELD
  | UNAUTHORIZED_HELD
  | SAFETY_HELD
  | FORCE_REQUIRED_HELD
  | LIVE_KILL_ACK_REQUIRED_HELD
  | COOLDOWN_HELD
  | PENDING_AUTHORIZED(KILL_THEN_RELAUNCH | RELAUNCH_ONLY)
```

Its byte-identical `CanonicalJsonV1` form is
`{"candidate": <action or null>, "reason": <reason enum or null>,
"state": <variant name>}`. `reason` is non-null only for `INVALID_HELD` and
`UNREADABLE_HELD`; `candidate` is non-null only for `PENDING_AUTHORIZED`.

**ENFORCED total marker/gate precedence:**

1. `ABSENT`: automatic authority may proceed.
2. `PRESENT_INVALID` or `UNREADABLE`: `INVALID_HELD` or `UNREADABLE_HELD`;
   visible manual-origin `HOLD`; retain the path; no automatic fallthrough.
3. A valid request ID already in the committed consumed set: `CONSUMED`; no
   manual bypass; automatic authority may proceed.
4. Expired marker or failed planning revalidation: `EXPIRED_HELD` or
   `UNAUTHORIZED_HELD`; visible `HOLD`; no fallthrough.
5. `SAFETY_HELD` because presence is unknown/untargetable or proof incomplete:
   visible `HOLD`; no fallthrough.
6. Missing required protected force: `FORCE_REQUIRED_HELD`; visible `HOLD`; no
   fallthrough.
7. Missing applicable live-kill acknowledgement:
   `LIVE_KILL_ACK_REQUIRED_HELD`; visible `HOLD`; no fallthrough.
8. Let `raw_cooldown` be `config.restart_cooldown_seconds` when it is a finite
   numeric non-Boolean, otherwise `45`; restart cooldown is
   `min(60, max(30, float(raw_cooldown)))`. Thus configured zero becomes 30,
   matching shipped normalization. A missing or null `last_launch_epoch` means
   there is no prior-launch cooldown. A Boolean, negative, nonfinite, or
   future-valued `last_launch_epoch` is invalid classifier state and produces
   `SAFETY_HELD`. Otherwise its finite nonnegative value holds while
   `decision_now_epoch - last_launch_epoch < cooldown`; retain marker and do
   not fall through as `COOLDOWN_HELD`.
9. A surviving candidate is `PENDING_AUTHORIZED(candidate)` and selects
   `MANUAL_AUTHORIZED`.
10. Manual origin overrides configuration hold and lead-loop stand-down for
    this attempt, bypasses automatic backoff, and resets readiness give-up. It
    cannot override targetability, start guards, protection authorization,
    cooldown, or the final barrier.

If manual and automatic teardown are both allowed, the selector emits exactly
one manual teardown and one manual authority ID:

```text
selected_teardown = manual_teardown
origin = MANUAL_AUTHORIZED
```

The authority ID is SHA-256 over
`agenttalk.supervisor.manual-authority.v1\0` plus `CanonicalJsonV1` of the following closed object:

```text
{
  "schema": "manual-authority/v1",
  "request_id": <RequestId>,
  "marker_revision_sha256": <Hex64>,
  "authority_case": "PROVABLY_CHILDLESS_OWNED_WRAPPER"
                    | "CONFIRMED_ABSENCE" | "MANUAL_TARGETS",
  "candidate": "KILL_THEN_RELAUNCH" | "RELAUNCH_ONLY",
  "evidence_id": <childless authority ID for childless kill,
                  targetability candidate_digest for other kill,
                  absence confirmation_id for no-kill>,
  "authorization_snapshot_id": <Hex64>
}
```

Automatic origin outside the named owned-childless case uses
`AutomaticAuthorityIdV1`, SHA-256 over
`agenttalk.supervisor.automatic-authority.v1\0` plus `CanonicalJsonV1` of
the following closed object:

```text
{
  "schema": "automatic-authority/v1",
  "state_epoch": <ClassifierStateV1.state_epoch>,
  "committed_revision": <revision containing the selected observations>,
  "condition_fingerprint": <RecoveryConditionFingerprintV1>,
  "candidate": "KILL_THEN_RELAUNCH" | "RELAUNCH_ONLY",
  "evidence_id": <targetability candidate_digest for strict-runtime kill,
                  absence confirmation_id for no-kill>
}
```

Automatic `PROVABLY_CHILDLESS_OWNED_WRAPPER` sets both selected authority ID
and evidence ID source-equal to the module's separately domained proof `authority_id`.
Manual-wins instead stores the manual authority ID as selected authority and
the source-equal module proof ID as evidence. Either origin sets the reservation's
complete `ChildlessReservationEvidenceV1` source-equal to the module evidence; hashes are not
decoded to recover its owner, mode, basis, debt, or target tuple/digest. Their
constructors and fields are disjoint; no implementation
may compare the manual wrapper ID to the module proof ID or substitute the
generic targetability digest for the owned-tree or debt-residual digest.

`CONDITIONAL_POST_TEARDOWN` binds to the selected origin's ID. When manual wins
an overlap, the losing automatic candidate remains diagnostic only. Evaluation
order cannot change origin.

**ENFORCED state deltas after task #115:** Both origins enter
`RecoveryExecutionStateV1`; the checked owner applies only the closed delta
union above and no executor branch may save a cached whole state. Every
authority-enabling or effect-owned childless row below consumes the fresh
operation/use-specific permit or permits matching the complete persisted
envelope. Each external adapter accepts only
`ChildlessExternalEffectCallV1` and returns only the matching
`ChildlessExternalEffectReceiptV1`; raw IDs, targets, bindings, or persisted
state cannot reach an effect.

Reservation has the following precondition:
`recovery_poll_terminal_sequence != ordinary_poll_sequence` and either
top-level `recovery_execution == IDLE` or
`recovery_execution == CHILDLESS(envelope)` with envelope execution `IDLE`
and every `RESERVE/CONTINUE` predicate from the module true. It also requires
`configured_prior_effect_unknown_fence == null` for every origin. A non-null
fence permits ordinary observation and its private source-bound
`PRIOR_EFFECT_FENCE_CLEAR` only; it blocks all reservation/effect/launch paths.
`RESERVED/PRE_BARRIER`, `RESERVED/TREE_CLOSURE_HELD`,
`RESERVED/TREE_CLOSURE_ACQUIRING`,
`RESERVED/TREE_CLOSURE_RELEASING`, `RESERVED/TEARDOWN_IN_FLIGHT`,
`RESERVED/SPAWN_IN_FLIGHT`, and
`AMBIGUOUS_LAUNCH` reject every new automatic or manual reservation without
mutation, even after a CAS loser reloads and re-reduces. `manual_readiness` is
orthogonal bookkeeping: `NONE` and `APPLIED_PENDING_READINESS` both permit a
new reservation only under that same precondition; replacing that
bookkeeping during a later launch cannot make its consumed request ID reusable.

| Transition | Normative delta |
| --- | --- |
| Refused/held | Preserve marker/revision byte-identically. Do not reserve, consume, kill, launch, reset readiness, or mutate automatic backoff. |
| Irrecoverable checked-state loss | Dry run returns only a simulated `STATE_PROVENANCE_LOST`/`POLICY_HELD` result. Otherwise apply only `StateLossQuarantineCreationDeltaV1`: create a new epoch with `StateLossQuarantineV1.UNRESOLVED`, select `STATE_PROVENANCE_LOST`, emit mandatory attention, and deny every kill, launch, closure, authority-enabling/effect-owned mutation, marker consumption, identity commit, and grace recovery. Construct no childless envelope, permit, usable debt/cycle, or attempt budget. V1 has no automatic retirement constructor; a valid backup or local extinction is not restoration proof. |
| Maximum ordinary capture sequence | Ordinary begin returns the typed `ATTENDED_REQUIRED(CaptureSequenceExhaustionV1)` before acquisition and performs no mutation or action. An attended request may apply `CAPTURE_SEQUENCE_ROLLOVER` only through #115's checked compare-and-swap from a matching maximum-sequence top-level `IDLE` predecessor, installing the displayed fresh-epoch replacement. Any stale request, non-childless execution, or childless envelope/debt/cycle/continuation/retired attempt returns the typed rejection/blocker and leaves the complete checked state byte-identical. |
| Childless capability or target-executor witness unavailable | A missing `ClosureCapabilityV1` or permit construction result `CAPABILITY_UNAVAILABLE` cannot construct `ChildlessEffectEnvelopeV1` for a fresh reservation, `ExactTargetExecutorPermitV1` for persisted state, any permit-bound mutation, executable target/call, or receipt-consuming transition. Preserve an existing envelope byte-identically, allow only the separate #115 owner-private observation mutation, emit continuous `CAPABILITY_UNAVAILABLE`, and remain `POLICY_HELD`. This never becomes `CLOSURE_VETOED`, retry, or exhaustion. |
| Childless permit rejected | A stale checked revision reloads/re-reduces. A copied, replayed, consumed, wrong-use, mismatched, or invalid-scope private operand is rejected with zero mutation/call and never becomes operator-facing `CAPABILITY_UNAVAILABLE`. Malformed checked state follows the invalid-fence row instead. |
| Prior-effect fence reconciliation | From top-level `IDLE` with a non-null fence, the winning ordinary-observation commit may publish one `CommittedOrdinaryFenceCaptureV1`; a same-predecessor CAS loser publishes none. #115's unexported deny-only reducer consumes that witness and applies merged #120 rules over its sealed capture and module-typed fence targets. Every clearance custody first requires the sole `RECEIPT -> COMMITTING` winner; losers read no state. Only a matching `CLEAR` winner whose committed revision is source-equal to the current revision, whose capture epoch is at the freshness-floor epoch, and whose sequence is strictly above its floor may construct private `NonChildlessAuthorityDeltaV1(PRIOR_EFFECT_FENCE_CLEAR)` and atomically clear. Blocked/ambiguous/unavailable receipts return their corresponding closed `POLICY_HELD_BARRIER` variant; dry-run/kill-switch/supervisor gates return `POLICY_HELD_GATE`; untrustworthy state, stale/mismatched input, proved no-commit failure, replay, and state-CAS outcome uncertainty use their separate closed result and custody disposition. Deterministic no-commit outcomes over a trustworthy current fence preserve it byte-identically; provenance loss, replay losers, and an outcome-unknown state CAS claim neither preservation nor clearance until checked reread reconciliation. This row constructs no action custody, reservation, permit, kill, spawn, archive, identity commit, or launch. |
| Reserve selected authority | Only with `configured_prior_effect_unknown_fence=null`, a non-childless selection from top-level `IDLE` records `NON_CHILDLESS/RESERVED/PRE_BARRIER`. For a kill-bearing configured relaunch/stuck-recovery action, that same winning #115 checked transition atomically yields exactly one `ConfiguredOwnedTreeActionCommitV1` with its owner in `READY`; a no-kill selection yields no dispatch custody. A childless selection requires `ClosureCapabilityV1.AVAILABLE` and consumes its one-shot `RESERVE` permit. `INITIAL` creates the complete envelope/binding. `CONTINUE` starts from childless `IDLE`: initial-mode retry may atomically rebind only the fresh target tree for the same source-bound owner while preserving cycle and terminal historical tombstones byte-identically; a physically different owner must first use `OWNER_TRANSITION` and a later `INITIAL`. Debt completion keeps the immutable envelope/debt binding source-equal to its predecessor and retains the residual subset whose members are source-equal to the corresponding authorized tuple members. Both enter `RESERVED/PRE_BARRIER` with null spawn guard/deadline/attempt/revision/closure/pending disposition; neither is allowed with a continuation or `RELEASE_PENDING` tombstone. The live witness, permit, and action custody are not persisted. Consume the selected proof according to its mode; preserve the separate module confirmation byte-identically for a live match. Do not add a manual request ID to the consumed set. |
| Begin childless closure acquisition | Acquire the exclusive effect guard, live-recompute the reservation/binding, and consume a `CLOSURE_ACQUIRE/STATE_MUTATION` permit to commit `TREE_CLOSURE_ACQUIRING` plus the `ACTIVE_ATTEMPT/CLOSURE_ACQUIRE/ARMED` continuation bound to that active attempt. At the successor revision construct a distinct fresh `CLOSURE_ACQUIRE/EXTERNAL_CALL` permit, then invoke the typed closure call while retaining the guard. Apply its receipt only through a third fresh receipt-derived `RECEIPT_MUTATION` permit whose call-issuance binding is source-equal to that receipt. Automatic origin creates/increments `ACTIVE/ISSUED`; manual origin leaves the cycle byte-identical. Preserve existing debt byte-identically. |
| Childless closure transiently absent/blocked | Only after the current-host executor gate passes, under the same guard, a conforming transient refusal plus terminal matching `NEVER_ACQUIRED`, or matching `RELEASED` while still acquiring, retires the attempt and finalizes `CLOSURE_VETOED`. A reload-held closure, live joined-evidence mismatch, or late execution/manual/policy veto commits `TREE_CLOSURE_RELEASING/CLOSURE_VETOED`; matching release/reconcile calls each use the effect guard and finalize only after matching `RELEASED`. `HELD`, `UNKNOWN`, a live foreign continuation, or current-host executor unavailability preserves every fence byte-identically. A post-reservation structural-unavailability claim from the successor remains malformed; the independently reconstructed current-host unavailable fact uses the prior row. No kill or launch occurs. |
| Childless closure held | Only a matching permit plus the fresh raw-capture/live-basis/target-matching join may construct the mutation to `TREE_CLOSURE_HELD`, bind its closure ID, and apply the matching acquire/reconcile receipt at `CALL_RETURNED`. A later operation-specific `STATE_MUTATION` permit replaces that checkpoint atomically with `STOP_TREE/ARMED` or `CLOSURE_RELEASE/ARMED`; the receipt permit cannot arm the next call. Preserve existing debt and cycle count byte-identically. |
| Childless teardown action-ready | Under the effect guard, consume a `STOP_TREE/STATE_MUTATION` permit to enter `TEARDOWN_IN_FLIGHT`, create/update origin-neutral debt, and arm the matching continuation. At the successor revision, only a distinct fresh `STOP_TREE/EXTERNAL_CALL` permit can construct `ExecutableOwnedTargetSetV1` and invoke the typed call. A third fresh receipt-derived mutation permit whose call-issuance binding is source-equal to that receipt applies it to enter `CALL_RETURNED`; a stale/nonowner caller, consumed permit, or raw persisted value cannot invoke/apply the adapter. The invocation-bound typed `NATIVE_ENTRY_FAILED_NO_EFFECT` result returns one successor custody and preserves `ARMED` byte-identically; an uncertain/maybe-crossed native-effect frontier poisons the lineage and preserves the fence byte-identically. |
| Childless post-action observation | A `POST_ACTION_CAPTURE/STATE_MUTATION` permit and matching `STOP_TREE/CALL_RETURNED` fact reserve the next nonordinary ordinal and arm the typed capture. A distinct fresh post-CAS call permit obtains the observation receipt; a third fresh receipt-derived mutation permit whose call-issuance binding is source-equal to that receipt maps it and enters `TREE_CLOSURE_RELEASING` with `POST_ACTION_CAPTURE/CALL_RETURNED` while preserving debt/current attempt and automatic `ISSUED` byte-identically. Only a later `CLOSURE_RELEASE/STATE_MUTATION` permit arms typed release, which then repeats the three-stage pattern. |
| Childless matching-release finalization | A matching permit, effect guard, and typed `RELEASED` receipt are required to apply the module event table, clear current-attempt fields, record failure/exhaustion, retire the attempt, release the reservation, clear the continuation, or clear debt/cycle. For reload/takeover retained `STOP_TREE/CALL_RETURNED`, a matching `CLOSURE_RECONCILE` receipt that proves `RELEASED` finalizes conservatively as `EFFECT_UNPROVEN`, enters childless `IDLE`, keeps debt outstanding, and makes no residual-capture call; the next ordinary poll may clear debt only through the module's debt-only finalize scope. Live `COMPLETE_GONE` after a typed post-action capture may normalize within the same envelope to `PRE_BARRIER`; other reload cleanup enters envelope execution `IDLE` without launch. Every finalized branch writes the same-poll terminal through a permit-bound mutation. |
| Ephemeral terminal action becomes committed | The winning #115 checked COMPLETE/TIMEOUT/FAILED transition persists the source-equal `next_entry` and atomically yields one `EphemeralTerminalActionCommitV1` with its action owner in `READY`. A stale CAS loser and every replay of the same transition yield no custody. Only the private call constructor's `READY -> CALL` winner may submit the kill; persisted `next_entry` and request/action provenance alone remain inert. |
| Non-childless no-kill veto | A reservation that minted no action custody may release through its private `PRE_BARRIER_RELEASE` delta. Preserve any marker and launch/readiness/backoff fields byte-identically. A reserved no-kill absence proof remains consumed. |
| Kill-bearing configured live-invocation veto | Only while the captured issuer is positively live and its action custody remains `READY`, atomically close that custody as typed no-effect and apply private `PRE_BARRIER_RELEASE` in the same #115 commit. Both happen or neither; no call, plan, or effect is constructed. |
| Kill-bearing configured issuer unavailable/reload | Derive `ConfiguredPreBarrierOwnerLossHoldV1` with current extinction status. Preserve reservation/checkpoint/barrier/targets and prior-effect-unknown fields byte-identically. No generic release, custody remint, second reserve, kill, or launch exists. Only the GONE-only attended disposition or optional future `ConfiguredPreBarrierRetrySuccessorV1` can leave this hold. |
| Childless final-barrier veto at `PRE_BARRIER` | Consume a fresh matching `PRE_BARRIER_RELEASE/STATE_MUTATION` permit to release/remove the childless envelope and write the terminal. Static executor unavailability preserves the complete envelope byte-identically and remains `POLICY_HELD`; a stale/replayed/mismatched constructor is `REJECTED` and reloads/re-reduces with the envelope byte-identical. No generic/direct release path exists. |
| Barrier passed, immediately before spawn | Require `configured_prior_effect_unknown_fence=null` for every origin. When #120 owned-tree state or post-kill provenance applies, require its fresh deny-only launch barrier to be unblocked and unambiguous. A blocked/ambiguous result preserves the hold byte-identically and cannot clear debt. For childless origin, consume `SPAWN/STATE_MUTATION` to update the module-named `ChildlessOuterStateDeltaV1` projection, enter envelope `SPAWN_IN_FLIGHT`, and install `SPAWN_RESERVATION/SPAWN/ARMED`; at the successor revision a distinct fresh `SPAWN/EXTERNAL_CALL` permit is the only constructor for `Start-Process`. Non-childless origin uses its private typed delta/call. The childless envelope and inert binding remain byte-identical through the call and any ambiguity. |
| Proven no-spawn failure | Only an OS/API result that positively proves no child was created may set `launching=false`, release reservation, preserve any marker, attempt/backoff bookkeeping, and prior guarded identity byte-identically, clear the pending deadline, and record the typed failure result. For childless origin, the matching typed receipt and a fresh receipt-derived `SPAWN_RESULT_COMMIT/RECEIPT_MUTATION` permit whose call-issuance binding is source-equal to that receipt are mandatory. Timeout, exception, lost return, or any uncertain post-issuance effect enters `AMBIGUOUS_LAUNCH` instead. |
| Spawn returned but guarded identity is ambiguous | For childless origin, consume a fresh receipt-derived `SPAWN_RESULT_COMMIT/RECEIPT_MUTATION` permit whose call-issuance binding is source-equal to the matching typed spawn receipt and persist `AMBIGUOUS_LAUNCH` with continuation `NONE`, the complete envelope reservation, null pending deadline, and `ambiguity_boundary_poll_sequence=ordinary_poll_sequence`; reset `absence_confirmation` to `EMPTY`. For `IDENTITY_COMMIT_FAILED`, set `reservation.spawned_guard` and `evidence.observed_guard` source-equal to the returned non-null `SpawnGuardV1`; for `START_RETURNED_WITHOUT_GUARD`, keep both null. The receipt-free crash conversion instead requires the module's persisted SPAWN issuer subject plus positive dead-issuer scope. Non-childless origin uses its private typed transition. Do not release authority ownership or permit another launch. |
| New guarded identity commits | In one checked transaction replace the managed identity, reset the establishment guard, and update launch/readiness state. `GuardedLaunchCommitV1` is inert checkpoint input only. For childless spawn origin, only the matching typed spawn receipt + checkpoint + fresh receipt-derived `SPAWN_IDENTITY_COMMIT/RECEIPT_MUTATION` permit whose call-issuance binding is source-equal to that receipt may construct the mutation and remove the envelope, after debt is `NONE`, no closure remains, and all retired-attempt obligations are terminal; the spawn continuation is consumed by that same commit. A physically different guarded owner observed outside that spawn may clear an old-owner `IDLE` envelope/cycle only through the module's state-only `OWNER_TRANSITION` permit with the same no-debt/no-obligation predicates. Non-childless origin returns directly to top-level `IDLE`. Manual spawn origin also records its consumed request and pending readiness. |
| Readiness observed | Only guarded readiness whose managed generation is source-equal to `committed_managed_generation` sets `readiness_seen=true` and `launching=false`, and it alone satisfies a pending manual-readiness value. Compare-clear that marker using request ID plus revision and set `manual_readiness=NONE`; a replaced marker is untouched. Readiness for any other generation cannot change launch state, clear the marker, or satisfy the request. |

The consumed set retains the latest 128 IDs in checked commit-revision order
and evicts the oldest; the five-minute TTL prevents an evicted ancient marker
from regaining authority. Every failure leaves the marker pending. A consumed
no-kill absence proof must be rebuilt from two ordinary polls. Consumed-set
mutation and guarded-identity commit are both task #115-dependent.

**SPECIFIED crash/reload and ambiguity rules; effect enforcement requires
#115, #146, and the closure successor:**

Every crash, reload, resume, takeover, CAS re-reduction, and future childless
entry variant within one supported V1 store activation can deserialize only
`ChildlessEffectEnvelopeV1` evidence. None can deserialize or manufacture
`CurrentExactTargetExecutorWitnessV1`,
`ExactTargetExecutorPermitV1`, `PermitBoundChildlessMutationV1`, an executable
call, receipt, live guard lineage, or private construction seal. A fresh witness
must match the complete inert binding and pass static preflight; an operation
that requires the effect guard acquires it, creates one unique lineage, and
atomically moves its sole custody before constructing its revision-bound permit.
Only then is any release,
takeover, nonordinary capture, reconciliation, finalization, `Stop-Tree`, or
launch object constructible. A native owned-tree termination additionally
requires the one winning private admission, plan-ownership, and native-entry
transition for one closed dispatcher variant. This is a construction rule,
not an inventory of entry paths, but that dispatcher rule remains undelivered
until #146.

This construction seal is not a same-platform host-transfer guarantee. Same-
platform state-file/workspace transfer, restore, rollback, and migration
activation are unavailable in V1; those are 87-C scope. The inert binding
contains no trustworthy host/process-universe operand, so platform/contract
equality, PID/start, or its digests cannot prove a copied state belongs to the
destination universe. A conforming activation path that is told, or otherwise
knows, that state came from one of those operations must refuse before
admitting or decoding the bytes as the active checked store. It constructs no
witness, permit, authority/effect mutation, call, or launch. This is an
activation refusal before a conforming active agent exists, not
`CAPABILITY_UNAVAILABLE`/`POLICY_HELD` within one.

An out-of-band copy or overwrite can bypass that boundary and may be
indistinguishable from an ordinary same-store reload. It may therefore be
treated as local checked state, but that deployment is nonconforming and 87-A
makes no safety or recovery guarantee for it. If outer-state validation detects
rollback-unproven state, the sole admitted non-dry-run transition is fail-closed
quarantine; V1 does not promise universal detection. A future 87-C design must
add reviewed source-universe semantics within M5 Option A or keep copied state
inert. The non-Windows inherited-state rule below remains independently
structural because no matching V1 action-site executor witness exists there.

Without a `PERMITTED` construction, the envelope is retained byte-identically and no
adapter is callable. Static witness/executor inability emits
`CAPABILITY_UNAVAILABLE`/`POLICY_HELD`; stale, copied, replayed, consumed, or
mismatched construction is `REJECTED` and reloads/re-reduces without that
operator-facing code. Ordinary observation may still advance its separate
projection. A malformed envelope selects
`RETAIN_INVALID_FENCE` before witness construction. No V1 cross-platform
resume is actionable, and a future executor cannot treat a persisted Windows
FILETIME binding as compatible without a reviewed version and migration rule.
The following phase semantics apply only after the matching permit exists; they
do not form the proof of universal coverage.
Whenever a bullet invokes reconcile, release, capture, or another adapter,
“matching permit” means the module's three distinct arm-mutation, fresh post-
CAS external-call, and fresh receipt-derived mutation permits (the last carrying
a call-issuance binding source-equal to its receipt); no bullet authorizes a
raw call or reuse across revisions.

- Reload of childless `RESERVED/PRE_BARRIER` may use only its fresh permit-bound
  `MAY_RELEASE_PRE_BARRIER`; no attempt pair, continuation owner, effect guard,
  or spawn exists. It records `BARRIER_VETOED`, consumes no automatic attempt,
  preserves any consumed absence proof, and writes the same-poll terminal. A
  reloaded non-childless no-kill reservation may use its private no-custody
  release with the same consumed-proof rule. A kill-bearing configured
  reservation with an issuer checkpoint never uses either release: custody
  unavailability derives `ConfiguredPreBarrierOwnerLossHoldV1`, preserves the
  complete checkpoint/barrier/targets byte-identically, and permits only the GONE-only attended
  disposition or optional future `ConfiguredPreBarrierRetrySuccessorV1`.
- Reload of `RESERVED/TREE_CLOSURE_ACQUIRING` invokes only the closure successor's
  attempt-keyed `OwnedTreeClosureReconciliationV1`. Matching
  `NEVER_ACQUIRED` finalizes `CLOSURE_VETOED`; matching `RELEASED` binds its
  returned closure ID and finalizes the same veto; matching `HELD` is persisted
  as `TREE_CLOSURE_RELEASING/CLOSURE_VETOED` and released without termination.
  `UNKNOWN` preserves the reservation and automatic `ISSUED` byte-identically.
- Reload of `RESERVED/TREE_CLOSURE_HELD` never terminates. After any required
  no-call takeover checkpoint, a distinct `CLOSURE_RECONCILE` arm reconciles
  the source-equal persisted pair. It persists
  `TREE_CLOSURE_RELEASING/CLOSURE_VETOED`; matching `RELEASED` may then
  finalize directly, while matching `HELD` requires a later distinct source-bound
  release arm and a later matching `RELEASED`. `UNKNOWN` preserves the held state
  byte-identically.
- Reload of `RESERVED/TREE_CLOSURE_RELEASING` likewise reconciles the source-equal
  persisted pair before any release arm. `HELD` or `UNKNOWN` preserves the
  reservation, pending disposition, debt/current attempt, and automatic
  `ISSUED` byte-identically. Only matching `RELEASED` applies the module's pending-disposition
  finalizer; matching `HELD` requires a later distinct release arm.
- Reload of childless `RESERVED/TEARDOWN_IN_FLIGHT` never reissues
  `Stop-Tree`. Matching `HELD` takes a fresh typed post-action observation
  under that closure, persists its releasing disposition, and follows matching
  release. Matching `RELEASED` consumes the matching reconcile receipt only
  through a fresh `EFFECT_FINALIZE` permit, conservatively records
  `EFFECT_UNPROVEN`, enters childless `IDLE`, keeps debt outstanding, clears its current
  attempt, records the origin-sensitive failure/exhaustion result, and makes no
  residual-capture call or launch. The next ordinary poll may clear that debt
  only through the existing debt-only finalize scope and matching
  `OwnedDebtResidualObservationV1.COMPLETE_GONE`.
  `UNKNOWN` preserves the reservation, debt/current attempt, and automatic
  `ISSUED` byte-identically; `NEVER_ACQUIRED` after debt is invalid and preserves
  the fence byte-identically.
- Before invoking `Start-Process`, the checked state must already say
  `SPAWN_IN_FLIGHT` with the typed `SPAWN/ARMED` issuer continuation bound to that spawn reservation. For
  childless origin, the arm mutation and launch call require distinct fresh
  permits at their respective revisions. Reload of childless
  `SPAWN_IN_FLIGHT` becomes `AMBIGUOUS_LAUNCH(CRASHED_DURING_SPAWN)` only
  through a fresh state-mutation permit bound to that persisted issuer plus
  positive proof that its recorded supervisor process/start cannot resume;
  without that permit it remains inert inside the envelope while
  ordinary observations may advance. A permitted conversion records the
  current `ordinary_poll_sequence`, resets absence confirmation, and sets
  `launching=false`. Non-childless reload retains its existing transition.
- The launch reservation ID is passed to the wrapper and returned in the
  guarded managed-identity checkpoint. While `AMBIGUOUS_LAUNCH`, every manual
  and automatic teardown/replacement attempt is `HOLD`; marker deletion does
  not clear the hold.
- A later strict checkpoint whose PID/start guard and launch reservation ID
  are source-equal to `SpawnGuardV1` is adopted through the common guarded-launch
  commit below. Childless adoption requires a fresh matching
  `SPAWN_IDENTITY_COMMIT/STATE_MUTATION` permit over the matching envelope and
  checkpoint; any mismatch or missing permit remains ambiguous.
- Otherwise only a new `PhysicalAbsenceProofV1.CONFIRMED`, built from two
  compatible ordinary captures whose poll sequences are both strictly greater
  than `ambiguity_boundary_poll_sequence`, resolves it to `IDLE`, leaves a
  manual marker pending when present, and sets
  `launching=false`. That new confirmation remains available for one new
  reservation. Present, unknown, replayed, pre-ambiguity, or incomplete
  evidence cannot resolve the tombstone. For childless origin the observations
  may form while unavailable, but only a matching
  `SPAWN_RESULT_COMMIT/STATE_MUTATION` permit-bound mutation may remove the
  envelope and resolve the tombstone to top-level `IDLE`.
- `manual_readiness=APPLIED_PENDING_READINESS` survives reload without blocking
  recovery. Matching-generation readiness clears it only after the marker
  compare-clear result is durably recorded; a replaced marker is never removed.
  A later committed manual launch may replace the bookkeeping only after the
  older request ID is already durable in the consumed set. A different
  automatic generation supersedes and clears only the bookkeeping as specified
  above, never the marker.

**ENFORCED common origin-independent checkpoint after task #115:** Automatic
and manual launches construct one inert `GuardedLaunchCommitV1` checkpoint.
It is input, not an independently applicable state delta. For non-childless
origin the private `IDENTITY_COMMIT` transition applies it. For childless
origin only a matching typed spawn receipt plus a fresh receipt-derived
`SPAWN_IDENTITY_COMMIT/RECEIPT_MUTATION` permit whose call-issuance binding is
source-equal to that receipt may apply it through
`PermitBoundChildlessMutationV1`. Only after the final barrier, spawn, and a
strict PID/start/reservation checkpoint does that checked transaction replace managed identity, establish the real
`launch_grace_until`, reset child confirmation, and retain `launching=true`
with `readiness_seen=false` until matching-generation guarded readiness.
Manual origin additionally commits its consumed request ID and
sets `manual_readiness=APPLIED_PENDING_READINESS`. No pre-spawn, failed, or
ambiguous attempt can renew heartbeat freshness.

### Replacement proof and combiner

**ENFORCED by the closed launch proof:**

```text
LaunchProofV1 =
  NONE
  | CURRENT_ABSENCE_ELIGIBLE(
      confirmation_id,
      AUTOMATIC_HEARTBEAT_DUE | MANUAL_AUTHORIZED_BYPASS,
    )
  | CONDITIONAL_POST_TEARDOWN(selected_teardown_authority_id)
```

`CURRENT_ABSENCE_ELIGIBLE` requires both
`PhysicalAbsenceProofV1.CONFIRMED` and non-`NOT_ELIGIBLE` timing. It always has
empty targets and all kill flags false.

An allowed selected teardown creates only
`CONDITIONAL_POST_TEARDOWN`. The executor performs guarded termination, captures
a fresh shared-observer snapshot, and resolves the condition synchronously.
The conditional is never persisted across polls.

The combiner emits:

| Selected proof/authority | Recovery intent |
| --- | --- |
| No selected teardown and no eligible absence | `HOLD` |
| `CURRENT_ABSENCE_ELIGIBLE` | `RELAUNCH_ONLY` |
| Selected teardown plus `CONDITIONAL_POST_TEARDOWN` | `KILL_THEN_RELAUNCH`; launch remains conditional |

Module `TeardownDebtV1.OUTSTANDING` forces every different-owner and
relaunch-only proof to `NONE`, including otherwise eligible whole-wrapper
 absence. It permits only the module's same-debt completion authority; launch
 stays `NONE` until a synchronous typed `COMPLETE_GONE` plus a fresh matching
`EFFECT_FINALIZE` permit atomically clears that debt, after which the ordinary
nonpersisted conditional may proceed. A
non-null debt current-attempt ID permits reconciliation only and denies a new
reservation. For a
child-death-sourced teardown subset, “selected teardown” means only
`PROVABLY_CHILDLESS_OWNED_WRAPPER`; a blocked owner/tree/closure/debt proof
produces `HOLD`, never generic fallback. An `EXHAUSTED` module cycle likewise
narrows only automatic named childless teardown to `HOLD`, while fresh manual
authority may wrap and retry the same module proof. It does not suppress an
independently confirmed whole-wrapper-absence relaunch. These overlays change
neither banked matrix nor fingerprint.

Escalation and action attention are independent boolean outputs. 87-B decides
how to persist and deliver them. Recovery policy may narrow an action to
`HOLD`; it cannot create teardown/replacement authority or change either
mandatory output to false.
When `CAPABILITY_UNAVAILABLE` is present, 87-B must join the action resolution
to the matching fingerprint, name the held agent from
`canonical_condition.agent_key`, and state that operator action is required. A
bare enum is not a conforming operator projection.

**ENFORCED manual-origin gates:** Manual origin retains current
configuration/stand-down override, automatic-backoff bypass, readiness reset,
protected-agent authorization, and restart cooldown. Both origins still
require the selected case's complete proof and the fresh final barrier:
banked targetability for a non-childless kill, the module's `INITIAL` tree or
`DEBT_COMPLETION` residual witness for the named case, or confirmed absence
for no-kill. Automatic origin retains configuration, lead-loop, readiness,
and protection holds.
Generic automatic recovery backoff remains a hold for every non-childless
candidate. `PROVABLY_CHILDLESS_OWNED_WRAPPER`, in both `INITIAL` and
`DEBT_COMPLETION` mode, bypasses any pre-existing generic recovery-backoff
deadline/exponent; its checked childless cycle and cap exclusively govern
automatic retry timing, and it neither reads nor mutates those generic
backoff fields.

## Dominant-projection automatic matrix

**ENFORCED by generated exhaustive tests:** This table has exactly 48
runtime/presence rows and 96 freshness cells. It is the automatic base
projection before absence-confirmation, manual-origin, and final-barrier
overlays.

| Runtime dominant | Presence | Fresh action | Fresh escalation | Stale action | Stale escalation |
| --- | --- | --- | --- | --- | --- |
| `CURRENT_PROGRESS_HEALTHY` | `PRESENT_TARGETABLE` | `HOLD` | `NONE` | `HOLD` | `NONE` |
| `CURRENT_PROGRESS_HEALTHY` | `PRESENT_UNTARGETABLE` | `HOLD` | `NONE` | `HOLD` | `NONE` |
| `CURRENT_PROGRESS_HEALTHY` | `ABSENT` | `HOLD / WRAPPER_MISSING` | `NONE` | `RELAUNCH_ONLY` | `NONE` |
| `CURRENT_PROGRESS_HEALTHY` | `UNKNOWN` | `HOLD / WRAPPER_UNKNOWN` | `NONE` | `HOLD / WRAPPER_UNKNOWN` | `REQUIRED` |
| `CURRENT_STALE_RECOVERABLE` | `PRESENT_TARGETABLE` | `HOLD` | `NONE` | `KILL_THEN_RELAUNCH` | `NONE` |
| `CURRENT_STALE_RECOVERABLE` | `PRESENT_UNTARGETABLE` | `HOLD` | `NONE` | `HOLD / RECOVERY_BLOCKED` | `REQUIRED` |
| `CURRENT_STALE_RECOVERABLE` | `ABSENT` | `HOLD / WRAPPER_MISSING` | `NONE` | `RELAUNCH_ONLY` | `NONE` |
| `CURRENT_STALE_RECOVERABLE` | `UNKNOWN` | `HOLD / WRAPPER_UNKNOWN` | `NONE` | `HOLD / RECOVERY_BLOCKED` | `REQUIRED` |
| `CURRENT_TEARDOWN_PROOF` | `PRESENT_TARGETABLE` | `KILL_THEN_RELAUNCH` | `NONE` | `KILL_THEN_RELAUNCH` | `NONE` |
| `CURRENT_TEARDOWN_PROOF` | `PRESENT_UNTARGETABLE` | `HOLD / RECOVERY_BLOCKED` | `REQUIRED` | `HOLD / RECOVERY_BLOCKED` | `REQUIRED` |
| `CURRENT_TEARDOWN_PROOF` | `ABSENT` | `HOLD / WRAPPER_MISSING` | `NONE` | `RELAUNCH_ONLY` | `NONE` |
| `CURRENT_TEARDOWN_PROOF` | `UNKNOWN` | `HOLD / RECOVERY_BLOCKED` | `REQUIRED` | `HOLD / RECOVERY_BLOCKED` | `REQUIRED` |
| `CURRENT_UNKNOWN_STARTING_OVERRUN` | `PRESENT_TARGETABLE` | `HOLD / CLI_CHILD_UNKNOWN` | `NONE` | `HOLD / CLI_CHILD_UNKNOWN` | `REQUIRED` |
| `CURRENT_UNKNOWN_STARTING_OVERRUN` | `PRESENT_UNTARGETABLE` | `HOLD / CLI_CHILD_UNKNOWN` | `NONE` | `HOLD / CLI_CHILD_UNKNOWN` | `REQUIRED` |
| `CURRENT_UNKNOWN_STARTING_OVERRUN` | `ABSENT` | `HOLD / WRAPPER_MISSING` | `NONE` | `RELAUNCH_ONLY` | `REQUIRED` |
| `CURRENT_UNKNOWN_STARTING_OVERRUN` | `UNKNOWN` | `HOLD / CLI_CHILD_UNKNOWN` | `NONE` | `HOLD / CLI_CHILD_UNKNOWN` | `REQUIRED` |
| `CURRENT_UNKNOWN_ACTIVE_CHILD` | `PRESENT_TARGETABLE` | `HOLD / CLI_CHILD_UNKNOWN` | `NONE` | `HOLD / CLI_CHILD_UNKNOWN` | `REQUIRED` |
| `CURRENT_UNKNOWN_ACTIVE_CHILD` | `PRESENT_UNTARGETABLE` | `HOLD / CLI_CHILD_UNKNOWN` | `NONE` | `HOLD / CLI_CHILD_UNKNOWN` | `REQUIRED` |
| `CURRENT_UNKNOWN_ACTIVE_CHILD` | `ABSENT` | `HOLD / WRAPPER_MISSING` | `NONE` | `RELAUNCH_ONLY` | `REQUIRED` |
| `CURRENT_UNKNOWN_ACTIVE_CHILD` | `UNKNOWN` | `HOLD / CLI_CHILD_UNKNOWN` | `NONE` | `HOLD / CLI_CHILD_UNKNOWN` | `REQUIRED` |
| `CURRENT_UNKNOWN_SEQUENCE_REGRESSION` | `PRESENT_TARGETABLE` | `HOLD / CLI_CHILD_UNKNOWN` | `NONE` | `HOLD / CLI_CHILD_UNKNOWN` | `REQUIRED` |
| `CURRENT_UNKNOWN_SEQUENCE_REGRESSION` | `PRESENT_UNTARGETABLE` | `HOLD / CLI_CHILD_UNKNOWN` | `NONE` | `HOLD / CLI_CHILD_UNKNOWN` | `REQUIRED` |
| `CURRENT_UNKNOWN_SEQUENCE_REGRESSION` | `ABSENT` | `HOLD / WRAPPER_MISSING` | `NONE` | `RELAUNCH_ONLY` | `REQUIRED` |
| `CURRENT_UNKNOWN_SEQUENCE_REGRESSION` | `UNKNOWN` | `HOLD / CLI_CHILD_UNKNOWN` | `NONE` | `HOLD / CLI_CHILD_UNKNOWN` | `REQUIRED` |
| `CURRENT_UNKNOWN_BINDING` | `PRESENT_TARGETABLE` | `HOLD / CLI_CHILD_UNKNOWN` | `NONE` | `HOLD / CLI_CHILD_UNKNOWN` | `REQUIRED` |
| `CURRENT_UNKNOWN_BINDING` | `PRESENT_UNTARGETABLE` | `HOLD / CLI_CHILD_UNKNOWN` | `NONE` | `HOLD / CLI_CHILD_UNKNOWN` | `REQUIRED` |
| `CURRENT_UNKNOWN_BINDING` | `ABSENT` | `HOLD / WRAPPER_MISSING` | `NONE` | `RELAUNCH_ONLY` | `REQUIRED` |
| `CURRENT_UNKNOWN_BINDING` | `UNKNOWN` | `HOLD / CLI_CHILD_UNKNOWN` | `NONE` | `HOLD / CLI_CHILD_UNKNOWN` | `REQUIRED` |
| `CURRENT_BLOCKED_STALL` | `PRESENT_TARGETABLE` | `HOLD / CLI_CHILD_STALLED` | `NONE` | `HOLD / CLI_CHILD_STALLED` | `REQUIRED` |
| `CURRENT_BLOCKED_STALL` | `PRESENT_UNTARGETABLE` | `HOLD / CLI_CHILD_STALLED` | `NONE` | `HOLD / CLI_CHILD_STALLED` | `REQUIRED` |
| `CURRENT_BLOCKED_STALL` | `ABSENT` | `HOLD / WRAPPER_MISSING` | `NONE` | `RELAUNCH_ONLY` | `REQUIRED` |
| `CURRENT_BLOCKED_STALL` | `UNKNOWN` | `HOLD / CLI_CHILD_STALLED` | `NONE` | `HOLD / CLI_CHILD_STALLED` | `REQUIRED` |
| `CURRENT_UNKNOWN_OTHER` | `PRESENT_TARGETABLE` | `HOLD / CLI_CHILD_UNKNOWN` | `NONE` | `HOLD / CLI_CHILD_UNKNOWN` | `REQUIRED` |
| `CURRENT_UNKNOWN_OTHER` | `PRESENT_UNTARGETABLE` | `HOLD / CLI_CHILD_UNKNOWN` | `NONE` | `HOLD / CLI_CHILD_UNKNOWN` | `REQUIRED` |
| `CURRENT_UNKNOWN_OTHER` | `ABSENT` | `HOLD / WRAPPER_MISSING` | `NONE` | `RELAUNCH_ONLY` | `REQUIRED` |
| `CURRENT_UNKNOWN_OTHER` | `UNKNOWN` | `HOLD / CLI_CHILD_UNKNOWN` | `NONE` | `HOLD / CLI_CHILD_UNKNOWN` | `REQUIRED` |
| `CONTRACT_ABSENT` | `PRESENT_TARGETABLE` | `HOLD / COMPATIBILITY_DEGRADED` | `NONE` | `HOLD / COMPATIBILITY_DEGRADED` | `REQUIRED` |
| `CONTRACT_ABSENT` | `PRESENT_UNTARGETABLE` | `HOLD / COMPATIBILITY_DEGRADED` | `NONE` | `HOLD / COMPATIBILITY_DEGRADED` | `REQUIRED` |
| `CONTRACT_ABSENT` | `ABSENT` | `HOLD / WRAPPER_MISSING` | `NONE` | `RELAUNCH_ONLY` | `REQUIRED` |
| `CONTRACT_ABSENT` | `UNKNOWN` | `HOLD / WRAPPER_UNKNOWN` | `NONE` | `HOLD / WRAPPER_UNKNOWN` | `REQUIRED` |
| `UNSUPPORTED_CONTRACT` | `PRESENT_TARGETABLE` | `HOLD / CONTRACT_UNSUPPORTED` | `NONE` | `HOLD / CONTRACT_UNSUPPORTED` | `REQUIRED` |
| `UNSUPPORTED_CONTRACT` | `PRESENT_UNTARGETABLE` | `HOLD / CONTRACT_UNSUPPORTED` | `NONE` | `HOLD / CONTRACT_UNSUPPORTED` | `REQUIRED` |
| `UNSUPPORTED_CONTRACT` | `ABSENT` | `HOLD / WRAPPER_MISSING` | `NONE` | `RELAUNCH_ONLY` | `REQUIRED` |
| `UNSUPPORTED_CONTRACT` | `UNKNOWN` | `HOLD / WRAPPER_UNKNOWN` | `NONE` | `HOLD / WRAPPER_UNKNOWN` | `REQUIRED` |
| `INVALID_CONTRACT` | `PRESENT_TARGETABLE` | `HOLD / CLI_CHILD_UNKNOWN` | `NONE` | `HOLD / CLI_CHILD_UNKNOWN` | `REQUIRED` |
| `INVALID_CONTRACT` | `PRESENT_UNTARGETABLE` | `HOLD / CLI_CHILD_UNKNOWN` | `NONE` | `HOLD / CLI_CHILD_UNKNOWN` | `REQUIRED` |
| `INVALID_CONTRACT` | `ABSENT` | `HOLD / WRAPPER_MISSING` | `NONE` | `RELAUNCH_ONLY` | `REQUIRED` |
| `INVALID_CONTRACT` | `UNKNOWN` | `HOLD / CLI_CHILD_UNKNOWN` | `NONE` | `HOLD / CLI_CHILD_UNKNOWN` | `REQUIRED` |

The generated test must rederive these validated distributions:

- 81 `HOLD`
- 3 `KILL_THEN_RELAUNCH`
- 12 `RELAUNCH_ONLY`
- 43 escalation `REQUIRED`
- 53 escalation `NONE`
- zero duplicates, missing cells, extra cells, or formula mismatches

**ENFORCED temporal overlays:**

- An unconfirmed stale-`ABSENT` base candidate becomes
  `HOLD / ABSENCE_CONFIRMING`.
- A confirmed but timing-ineligible fresh-`ABSENT` observation remains `HOLD`.
- Every kill candidate carries `CONDITIONAL_POST_TEARDOWN` and may finish as
  `HOLD` after termination if its fresh barrier does not clear.
- Manual-origin selection is evaluated outside this automatic matrix.

## `RecoveryConditionFingerprintV1`

**STATED source fact:** The live `_decision_fingerprint`
(`src/agenttalk/supervisor.py:1034-1043`) is an unhashed pipe-joined identity
over public decision fields. It omits child reason, complete runtime reasons,
presence, candidates, freshness, and recovery-blocked disposition. Two
`CLI_CHILD_UNKNOWN` decisions that differ only in `child_reason` therefore
collide. 87-A does not rename or silently change that existing concept.
`ActiveChildObservationV1` now makes the boundary explicit: raw child
subreasons remain diagnostic and may intentionally share one semantic
condition; the typed 87-B export carries them separately.

**ENFORCED distinct contract:**

```text
RecoveryConditionFingerprintV1 =
  "recovery-condition-v1:" + lowercase_hex_sha256(
    b"agenttalk.supervisor.recovery-condition.v1\0"
    + canonical_payload_bytes
  )
```

The payload has the following closed schema:

```text
{
  "schema": "recovery-condition/v1",
  "agent_key": <NFC canonical agent/root key>,
  "runtime": {
    "dominant": <enum>,
    "reasons": [<complete ranked enum tuple>]
  },
  "presence": {
    "state": <enum>,
    "reasons": [<closed ranked reason tuple>],
    "coverage": <ObserverCoverageSignatureV1> | null
  },
  "freshness": "FRESH" | "STALE",
  "recovery_blocked": true | false,
  "stale_uncertainty": true | false,
  "candidates": <CandidateSummaryV1>
}
```

`canonical_payload_bytes` are byte-identical to the `CanonicalJsonV1` encoding of that
payload. Presence coverage is null if and only if
`ProcessObservationV1.coverage` is null.

**ENFORCED candidate normalization:**

1. Convert each authority-normalized relevant candidate, and no known-foreign
   diagnostic evidence, into the following closed object:

   ```text
   {
     "pid": integer 1..4294967295 | null,
     "start_guard": NFC UTF-8 string of at most 128 bytes | null,
     "executable_basename": NFC string,
     "shape": "WRAP" | "WAIT" | "UNKNOWN",
     "agent_match": "MATCH" | "MISMATCH" | "UNKNOWN",
     "root_match": "MATCH" | "MISMATCH" | "UNKNOWN",
     "ownership": "OWNED" | "UNOWNED" | "UNKNOWN",
     "failure_codes": [
       "COMMAND_UNREADABLE"
       | "ROOT_UNREADABLE"
       | "LAUNCH_SHAPE_AMBIGUOUS"
       | "PID_START_AMBIGUOUS"
       | "IDENTITY_REUSED"
       | "OWNERSHIP_UNPROVEN"
       | "OBSERVATION_INCONSISTENT"
     ]
   }
   ```

   The observer includes every matching failure code, deduplicated in the order
   shown. A new key, code, or code predicate requires
   `RecoveryConditionFingerprintV2`.
2. Normalize strings to NFC. Lowercase the executable basename and bound it to
   128 UTF-8 bytes, cutting only at a code-point boundary. A missing, invalid,
   or overlong raw start guard becomes null and adds
   `PID_START_AMBIGUOUS`; an identity guard is never truncated. Command
   arguments and credentials never enter the object or its hash; the closed
   `shape` and match/failure fields are their only semantic projection.
3. Canonically serialize each object, deduplicate byte-identical byte strings, and sort
   bytewise before truncation.
4. Define `total_count` as the number after byte-identical deduplication. Persist the
   first eight objects. `CandidateSummaryV1` also contains `total_count`,
   `omitted_count = max(total_count - 8, 0)`, and `omitted_sha256`.
5. When candidates are omitted, compute `omitted_sha256` over the domain
   `agenttalk.supervisor.recovery-condition.candidates.v1\0` followed by each
   omitted canonical object as a four-byte big-endian length plus bytes.
   Store the digest as 64 lowercase hexadecimal characters. Otherwise it is
   null.

`CandidateSummaryV1` has this byte-identical serialization:

```text
{
  "items": [<first eight canonical candidate objects>],
  "total_count": <post-dedup integer>,
  "omitted_count": <integer>,
  "omitted_sha256": <64 lowercase hexadecimal characters> | null
}
```

The full candidate set determines authority before this diagnostic bound.
Candidate enumeration order and duplicates cannot change the fingerprint.
Changing any omitted candidate changes `omitted_sha256` except for the accepted
SHA-256 collision risk.

**ENFORCED fixed vector:** For NFC agent key `agenté/root`, no relevant
candidates, freshness `FRESH`, unavailable/unknown presence with reasons
`SNAPSHOT_UNAVAILABLE`, `COVERAGE_INCOMPLETE`, and
`RECORDED_IDENTITY_UNKNOWN`, and runtime `CURRENT_UNKNOWN_OTHER`, the canonical
payload is byte-identical to these 433 ASCII bytes:

```text
{"agent_key":"agent\u00e9/root","candidates":{"items":[],"omitted_count":0,"omitted_sha256":null,"total_count":0},"freshness":"FRESH","presence":{"coverage":null,"reasons":["SNAPSHOT_UNAVAILABLE","COVERAGE_INCOMPLETE","RECORDED_IDENTITY_UNKNOWN"],"state":"UNKNOWN"},"recovery_blocked":false,"runtime":{"dominant":"CURRENT_UNKNOWN_OTHER","reasons":["CURRENT_UNKNOWN_OTHER"]},"schema":"recovery-condition/v1","stale_uncertainty":false}
```

The resulting `RecoveryConditionFingerprintV1` suffix is
`e5dd312b003e55327548f1dae152a28de60bb99b61d3aa3aeae8ea263f729f94`.
Every implementation must reproduce both the bytes and digest.

A candidate-bearing vector uses agent key `a/root`, one unowned PID 42
`python.exe` wrapper with the shown guarded start and ownership failure,
complete initial V1 coverage, `PRESENT_UNTARGETABLE`, fresh
`CURRENT_TEARDOWN_PROOF`, `recovery_blocked=true`, and this byte-identical 875-byte
payload:

```text
{"agent_key":"a/root","candidates":{"items":[{"agent_match":"MATCH","executable_basename":"python.exe","failure_codes":["OWNERSHIP_UNPROVEN"],"ownership":"UNOWNED","pid":42,"root_match":"MATCH","shape":"WRAP","start_guard":"2026-01-01T00:00:00Z"}],"omitted_count":0,"omitted_sha256":null,"total_count":1},"freshness":"FRESH","presence":{"coverage":{"ambiguity_scan_schema":1,"command_line_coverage":"complete","pid_start_guard_schema":1,"platform":"WINDOWS","process_row_schema":1,"process_source":"WIN32_PROCESS_CIM","recorded_identity_coverage":"complete","schema":"wrapper-observer-coverage/v1","wait_parser_schema":1,"wrap_parser_schema":1},"reasons":["VISIBLE_UNOWNED"],"state":"PRESENT_UNTARGETABLE"},"recovery_blocked":true,"runtime":{"dominant":"CURRENT_TEARDOWN_PROOF","reasons":["CURRENT_TEARDOWN_PROOF"]},"schema":"recovery-condition/v1","stale_uncertainty":false}
```

Its fingerprint suffix is
`f2b7ea5888d903e2a257462691150dbbc6f6c13da7451d1498e50bb4c06dfa13`.

The fingerprint controls incident-condition equivalence and duplicate
rate-limiting only. It never supplies teardown or replacement authority.

### Typed export to 87-B

**ENFORCED by a closed boundary record:** A fingerprint string alone cannot
carry the durable redacted condition evidence that 87-B must project.
87-A exports:

```text
RecoveryConditionV1 {
  schema: "recovery-condition-export/v1"
  fingerprint: RecoveryConditionFingerprintV1
  canonical_condition: source-equal to the fingerprint payload object above
  escalation_required: bool
  condition_codes: ordered tuple of length 0..2[
    "RECOVERY_BLOCKED" | "STALE_UNCERTAINTY"
  ]
  active_child_reason_codes: ActiveChild UNKNOWN reason tuple | null
  operator_candidates: OperatorDiagnosticCandidateSummaryV1
}

ConfiguredPreBarrierOwnerLossSummaryV1 {
  reason: CONFIGURED_PRE_BARRIER_OWNER_LOST
  agent_key: NFC canonical agent/root string
  state_epoch: lowercase hyphenated UUID
  current_revision: uint64
  reservation_id: lowercase hyphenated UUID
  source_checkpoint_id: lowercase hyphenated UUID
  source_hash: Hex64
  issuer_extinction: PROVED_GONE | LIVE_OR_UNPROVEN
  present_pid_reuse_disposition:
    CAPABILITY_UNAVAILABLE(ExactIssuerIdentityAdapterV1)
  effect_disposition: PRIOR_EFFECT_UNKNOWN
  automatic_exit: UNAVAILABLE(ConfiguredPreBarrierRetrySuccessorV1)
  attended_action:
    READY(ATTENDED_CONFIGURED_PRE_BARRIER_DISPOSITION)
    | BLOCKED_ISSUER_LIVE_OR_UNPROVEN
  remedy:
    CREATE_OR_PRESERVE_KILL_SWITCH_STOP_ALL_PROJECT_SUPERVISORS_REFRESH_CURRENT_HOLD_THEN_DISPOSE_AS_LIAISON_OR_SOLE_LEAD
}

ConfiguredPriorEffectUnknownFenceSummaryV1 {
  reason: CONFIGURED_PRIOR_EFFECT_UNKNOWN
  agent_key: NFC canonical agent/root string
  disposition_id: lowercase hyphenated UUID
  disposition_request_id: RequestId
  source_checkpoint_id: lowercase hyphenated UUID
  source_target_count: integer 1..64
  source_target_digest: Hex64
  disposed_state_epoch: lowercase hyphenated UUID
  disposed_revision: uint64
  freshness_floor_state_epoch: lowercase hyphenated UUID
  freshness_floor_after_ordinary_poll_sequence: uint64
  effect_disposition: PRIOR_EFFECT_UNKNOWN
  launch_requirement:
    FRESH_120_POST_KILL_BARRIER_THEN_TASK_57_BEFORE_CONFIGURED_LAUNCH
  remedy:
    REMOVE_KILL_SWITCH_START_ONE_CURRENT_SUPERVISOR_OBSERVATION_ONLY_THEN_OBTAIN_COMMITTED_SOURCE_BARRIER_AND_HANDLE_SURVIVORS_ATTENDED
}

RecoveryActionResolutionV1 {
  schema: "recovery-action-resolution/v1"
  fingerprint: RecoveryConditionFingerprintV1
  origin: "AUTOMATIC" | "MANUAL_AUTHORIZED" | "NONE"
  selected_authority_id: Hex64 | null
  authority_case:
    "STRICT_RUNTIME_TEARDOWN" | "PROVABLY_CHILDLESS_OWNED_WRAPPER"
    | "CONFIRMED_ABSENCE" | "MANUAL_TARGETS" | "NONE"
  intent: "HOLD" | "KILL_THEN_RELAUNCH" | "RELAUNCH_ONLY"
  result: "NOT_ATTEMPTED" | "POLICY_HELD" | "TEARDOWN_FAILED"
          | "BARRIER_VETOED" | "SPAWN_FAILED"
          | "IDENTITY_COMMIT_AMBIGUOUS" | "AUTOMATIC_RETRY_EXHAUSTED"
          | "LAUNCH_COMMITTED"
  teardown_debt: module TeardownDebtSummaryV1
  automatic_childless_cycle: module AutomaticChildlessCycleSummaryV1
  capture_sequence_exhaustion: CaptureSequenceExhaustionV1 | null
  configured_pre_barrier_owner_loss:
    ConfiguredPreBarrierOwnerLossSummaryV1 | null
  configured_prior_effect_unknown_fence:
    ConfiguredPriorEffectUnknownFenceSummaryV1 | null
  action_attention_required: bool
  action_attention_codes: ordered tuple of length 0..9[
    "CHILDLESS_STATE_PROVENANCE_LOST"
    | "CAPABILITY_UNAVAILABLE"
    | "CHILDLESS_OWNER_CHILD_TREE_OR_CLOSURE_INCOMPLETE"
    | "CHILDLESS_TEARDOWN_DEBT"
    | "AUTOMATIC_CHILDLESS_RETRY_ACTIVE"
    | "AUTOMATIC_CHILDLESS_RETRY_EXHAUSTED"
    | "CAPTURE_SEQUENCE_EXHAUSTED"
    | "CONFIGURED_PRE_BARRIER_OWNER_LOST"
    | "CONFIGURED_PRIOR_EFFECT_UNKNOWN"
  ]
  manual_marker_disposition: ManualMarkerDispositionV1 | null
}
```

`condition_codes` includes each true predicate in the displayed order.
`canonical_condition` must reproduce the fingerprint bytes byte-identically; 87-B may
persist it but cannot mutate and rehash it. The action record binds later
policy/execution resolution without pretending that result supplied authority.
The normative module gives the result/code precedence.
`action_attention_required == (action_attention_codes is nonempty)`. Neither
field enters the banked condition fingerprint.
At maximum ordinary sequence, `capture_sequence_exhaustion` is source-equal to the typed
begin result, intent is `HOLD`, result is `POLICY_HELD`, and
`CAPTURE_SEQUENCE_EXHAUSTED` is present. Otherwise the field is null and that
code is absent. 87-B must name the agent, exhausted epoch/revision, `READY` or
the source-equal blocker tuple, and the required attended rollover action; a bare enum
is nonconforming.
When configured issuer custody is unavailable,
`configured_pre_barrier_owner_loss` is the bounded redacted projection
`ConfiguredPreBarrierOwnerLossSummaryV1`, intent is `HOLD`, result is
`POLICY_HELD`, and `CONFIGURED_PRE_BARRIER_OWNER_LOST` is present. Otherwise the
field is null and that code is absent. 87-B must name the agent,
reservation/checkpoint, source hash, `PRIOR_EFFECT_UNKNOWN`, unavailable
`ConfiguredPreBarrierRetrySuccessorV1`, the
`CAPABILITY_UNAVAILABLE(ExactIssuerIdentityAdapterV1)` present-PID/reuse limitation,
and the specified-but-undelivered attended disposition. It states whether a
fresh independent OS result made extinction definitive `PROVED_GONE` and the
action ready or whether the PID is present/observation unavailable and the
action remains `LIVE_OR_UNPROVEN`. A present PID cannot be classified as
recycled from the redacted or internal generic start token; the hold persists
until the named adapter is delivered and reviewed. Its remedy text tells the
operator to create or preserve `supervisor.kill`, stop every project supervisor,
refresh the current hold/source hash, and invoke the prospective attended
operation as the liaison or sole lead; it explicitly says that stopping the
wrapper is not a remedy. A bare code, omitted summary, or generic “operator
attention required” text is nonconforming.
While `configured_prior_effect_unknown_fence` is non-null, that exported field
is the bounded redacted projection
`ConfiguredPriorEffectUnknownFenceSummaryV1`, intent is `HOLD`, result is
`POLICY_HELD`, and `CONFIGURED_PRIOR_EFFECT_UNKNOWN` is present. Otherwise the
field is null and that code is absent. 87-B names the source checkpoint,
bounded target count/digest, disposition request ID, freshness floor, and the
committed-fresh-barrier/attended-target remedy. It never states that the prior
effect is known or that a new reservation or any launch may proceed before
`PRIOR_EFFECT_FENCE_CLEAR` commits. Its specified sequence says: remove
`supervisor.kill`, start one current supervisor under the still-active global
fence, obtain the winning committed source-bound barrier-receipt custody, handle
the authorized current source target set attended if necessary, and retry
clearance.

The complete internal hold and fence never cross this boundary. Their issuer
instance token digest, PID/start proof, action-latch epoch, full source-target
tuple, authority/transition identifiers, disposition actor,
acknowledgements, and free-text reason are absent from both summary types. The
summary constructor is a closed projection over the checked current object; 87-B
cannot accept a raw hold/fence or add an optional internal-details branch. This
redaction is independent of the attended command's separate authorization
check: even an authorized 87-B renderer receives only the summary, while an
unauthorized disposition request receives the still narrower
`REDACTED_UNAUTHORIZED` result.
For `CAPABILITY_UNAVAILABLE`, 87-B joins this resolution to the matching
`RecoveryConditionV1.fingerprint`, renders
`canonical_condition.agent_key`, and says explicitly that operator action is
required. A projection that emits only the code or omits the agent is
nonconforming.

**ENFORCED diagnostic projection, not authority:** The shared observer also
projects relevant and known-foreign rows into
`OperatorDiagnosticCandidateV1`:

```text
{
  "classification": "RELEVANT" | "KNOWN_FOREIGN",
  "pid": integer 1..4294967295 | null,
  "start_guard": bounded NFC string | null,
  "executable_basename": bounded lowercase NFC string,
  "command_shape_fragment": bounded NFC string,
  "failure_codes": [
    "COMMAND_UNREADABLE" | "ROOT_MISSING" | "ROOT_FOREIGN"
    | "ROOT_UNREADABLE" | "LAUNCH_SHAPE_AMBIGUOUS"
    | "PID_START_AMBIGUOUS" | "IDENTITY_REUSED"
    | "OWNERSHIP_UNPROVEN" | "OBSERVATION_INCONSISTENT"
  ]
}
```

The command fragment contains no raw argument values. It has the following closed form:
`"<exe> <shape> --for=<MATCH|MISMATCH|UNKNOWN>
--root=<MATCH|MISMATCH|MISSING|UNKNOWN>"`, where `<exe>` and `<shape>` are the
already-normalized basename and `WRAP|WAIT|UNKNOWN`. It is capped at 256 UTF-8
bytes at a code-point boundary. Rows are canonicalized, byte-identically deduplicated,
byte-sorted, capped at eight, and carry total/omitted counts plus a SHA-256 tail
digest using domain
`agenttalk.supervisor.operator-diagnostic-candidates.v1\0` and the same
length-prefix construction as `CandidateSummaryV1`.

```text
OperatorDiagnosticCandidateSummaryV1 {
  items: tuple[OperatorDiagnosticCandidateV1] of length 0..8
  total_count: uint32
  omitted_count: uint32
  omitted_sha256: Hex64 | null
}
```

`omitted_count = total_count - len(items)` and the digest is null iff
that value is zero.

This diagnostic summary restores Revision 2's bounded rootless, foreign-root,
unreadable, PID/start, executable, structural-command, and parse-failure
evidence without changing the independently verified semantic fingerprint or
granting authority from known-foreign rows.

## Final launch barrier

**ENFORCED by execution order:** Every actual launch follows:

1. select authority, capture eligible execution gates, and apply policy gates;
2. atomically reserve the launch with that gate snapshot and consume any
   `AbsenceConfirmationV1`;
3. for a kill candidate, recapture execution/manual/policy authority
   immediately before guarded termination and issue termination only if it
   still matches;
4. run one fresh post-teardown/no-kill capture through the shared observer;
5. require that capture to classify `ABSENT`;
6. when #120 owned-tree state or post-kill provenance applies, run its fresh
   deny-only launch barrier and require an unblocked, unambiguous result;
7. recapture execution/manual/policy authority under the action-time fence; and
8. only then invoke the origin-specific typed `Start-Process` call.

For `PROVABLY_CHILDLESS_OWNED_WRAPPER`, the normative module structurally owns
steps 2, 3, and 8. Step 2 consumes the one-shot `RESERVE/STATE_MUTATION` permit
to create the complete effect envelope. Step 3 uses the closure successor's
action-time tree closure, matching reserved-target digest check, checked
debt/attempt commit, #146 closed dispatcher's childless variant, and
complete-gone proof.
Step 8 first consumes `SPAWN/STATE_MUTATION` to arm the persisted issuer and
then requires a distinct fresh post-CAS `SPAWN/EXTERNAL_CALL` permit; the
generic launch function cannot accept childless raw reservation state. The
path rejoins step 4 only after origin-neutral debt is cleared. Module debt
forces every unrelated launch proof to `NONE` while permitting only its
debt-bound residual cleanup, so neither manual nor automatic absence can
bypass a partial kill.

#120's barrier is not the closure successor. At merged `587e7c1`, an openable
Windows owned-tree target whose `OwnedExactStartGuardV1` creation FILETIME matches is verified and
terminated through one native handle. Each successful termination receives a
wait attempt within the remaining shared tree-wide budget before the fresh
snapshot. That removes the target-local check-to-kill PID-reuse gap; when the
process signals within the budget it also avoids an immediate live sample.
Open failure, identity mismatch, termination failure, wait timeout, or depleted
budget is not completion and is decided only by the fresh snapshot and barrier;
a target still present remains a survivor. #120 does not close process creation
between planning and effect: a
recorded parent may create an unplanned descendant after planning and then
exit, leaving a process outside the target set that may survive `Stop-Tree`.
The recycle-aware barrier catches the old-side descendant only to block launch;
a child of a replacement PID whose `OwnedExactStartGuardV1` creation FILETIME is
at or after the replacement PID's is excluded from that
retired-parent ownership edge, while missing/incomparable FILETIME identity evidence
remains conservative. The split does not suppress independent barrier evidence:
the replacement-side process still blocks if, for example, its command line
parses as this agent's wrapper or wait process. The barrier
never adds a kill target, proves `COMPLETE_GONE`, clears debt, or substitutes
for the successor's pre-effect creation closure. Attended reset is an
human escape bound to a named process-identity adapter, and the request-bound archive is retention;
neither is automatic closure evidence. For the named childless path, the module
consumes the first barrier result in its typed post-action observation; step 6 performs
the fresh final recheck immediately before spawn. No closure-dependent named
teardown previously held by `CAPABILITY_UNAVAILABLE` becomes executable solely
because #120 merged.

That effect claim is grounded at its execution site:
`src/agenttalk/supervisor.py:8900-8928` enters the FILETIME-guarded destructive branch,
while `8930-8932` skips an `owned_process_tree` target without
`start_filetime`. The separate Linux-token acceptance paths are observation
input, not a kill adapter. Accordingly a valid Linux-token snapshot receives
pre-reservation
`CAPABILITY_UNAVAILABLE(EXACT_TARGET_EXECUTOR_UNAVAILABLE)` and no destructive
tuple, authority, reservation, attempt, debt, or `Stop-Tree` call. The legacy
rounded-start path and any weakening of the Windows FILETIME requirement remain
forbidden.

**OPERATOR-VISIBLE CAPABILITY CONSEQUENCES:** This revision deliberately leaves
four permanent V1 capability limitations. The first, second, and fourth can
remain indefinitely held; the third refuses activation before imported state
becomes active. The same operator surface must also disclose two
configured-action residuals:

1. On Linux and macOS with state-loss quarantine `NONE`, every fresh
   closure-dependent named teardown returns
   `CAPABILITY_UNAVAILABLE(EXACT_TARGET_EXECUTOR_UNAVAILABLE)` before
   reservation or action. A childless envelope inherited from Windows remains
   inert because the host cannot construct a matching permit. If Windows
   already returned from `Stop-Tree`, the non-Windows host neither repeats the
   call nor clears debt. Ordinary observation may advance, but recovery remains
   `POLICY_HELD` until a reviewed named POSIX exact-target-identity adapter exists
   under M5 Option A.
2. On every platform, automatic `StateLossQuarantineV1.UNRESOLVED` retirement
   is unavailable in V1. Merged #120 supplies no trustworthy process-universe
   token, so local evidence cannot prove that the lost owner and every residual
   are extinct in the source universe. The quarantine remains
   `STATE_PROVENANCE_LOST` and `POLICY_HELD` pending attended handling.
3. Same-platform state-file/workspace transfer, restore (including backup
   restore), rollback, and migration activation are unavailable in V1. A
   conforming activation path that knows
   checked state came from one of those operations refuses before active-store
   admission, constructs no witness, permit, authority/effect mutation, call,
   or launch, identifies the rejected operation/store, and directs attended
   handling and 87-C. This is not an active-agent `POLICY_HELD` result. An out-
   of-band copy may be indistinguishable from same-store reload and may proceed
   outside that boundary; it is nonconforming and has no 87-A guarantee. If
   existing validation detects it as rollback-unproven, only fail-closed state-
   loss quarantine is admitted.
4. When a configured PRE_BARRIER issuer PID is present, V1 cannot distinguish
   the original live issuer from PID reuse. The checkpoint's generic
   `ProcStartGuardV1`-shaped value is audit evidence, not
   `OwnedExactStartGuardV1` and not a reuse comparator. Attended disposal is
   therefore `CAPABILITY_UNAVAILABLE(ExactIssuerIdentityAdapterV1)` and the
   owner-loss hold persists until that separately reviewed adapter is
   delivered. The operator cannot attest around this limit, and a generic
   token/start mismatch cannot upgrade the hold to `PROVED_GONE`.
5. **Configured PRE_BARRIER owner-loss residual:** when configured action custody
   is unavailable, automatic recovery returns the complete
   `ConfiguredPreBarrierOwnerLossHoldV1` and
   `POLICY_HELD(CONFIGURED_PRE_BARRIER_OWNER_LOST)`. It names whether issuer
   extinction is proved, the checkpoint/source hash, prior-effect-unknown fact,
   absent `ConfiguredPreBarrierRetrySuccessorV1`, the
   `ExactIssuerIdentityAdapterV1` limitation, and the complete prospective attended remedy. Only a
   fresh independent definitive PID-absent `GONE` result admits disposition; a
   present PID remains blocked under item 4. The disposition never kills or
   launches; it installs
   the global `ConfiguredPriorEffectUnknownFenceV1`. That fence keeps every
   configured and childless effect/launch held. The prescribed next steps are: remove
   `supervisor.kill`, start exactly one current supervisor under the still-
   global fence, obtain a winning committed source-bound #120 barrier, handle
   any surviving #120 identity-bound source target attended while the fence remains, then
   retry clearance. Clearance is ineligible while the kill switch is present;
   the restarted supervisor is observation-only until it succeeds. Stopping the
   wrapper without first disposing the dead issuer is not a remedy. #120 target-
   local identity supplies neither remint nor disposition authority.
6. **Configured-relaunch implementation residual:** until task #57's durable
   project-level singleton per wrapped agent is delivered and reviewed,
   automatic configured relaunch is unavailable for activation. A crash or hard
   cancellation after `Start-Process` but before launch-result persistence can
   leave a live wrapper while the checkpoint remains replayable, so retry may
   create a duplicate wrapper. #120 makes the preceding
   `OwnedExactStartGuardV1`-guarded kill target-safe to retry; it does not make
   launch idempotent. This is an
   implementation/activation blocker rather than an active-agent held result.

The following are explicitly insufficient as a process-universe proof: PID and
start, hostname, `state_epoch`, `process_source_digest`, MachineGuid alone,
local absence. A future successor may consume a read-only producer over an
existing OS token only; it may add no file, registry value, helper, daemon, OS
object, persistence plane, or runtime dependency. Accepting an identity token
as parser or snapshot input does not make it executable or authoritative.
87-B projections, 87-C activation surfaces, and the required operator manual
and tutorial must state all four permanent limitations and both configured
residuals. Held-agent projections apply to items 1, 2, 4, and 5; item 3
identifies the rejected operation/store rather than fabricating an active
agent; item 5 names its complete attended disposition and subsequent
fence-clearance remedy; item 6 identifies the missing dependency and
duplicate-wrapper consequence.

For `CONDITIONAL_POST_TEARDOWN`, the fresh post-teardown capture is also the
final barrier. A survivor, unavailable/incomplete capture, or ambiguous
candidate resolves the conditional to `NONE`.

The barrier never turns a survivor into a target. A veto preserves a pending
manual marker, leaves any no-kill confirmation consumed, and requires a new
two-poll confirmation before another no-kill launch attempt.

**STATED shipped-behavior change:** Today, snapshot unavailability plus no
prior process state can produce `allow_launch=true, reason=no_prior_process`
through `_prior_wrapper_may_be_alive` and `evaluate_launch_barrier`
(`src/agenttalk/supervisor.py:2859-2895`). 87-A deliberately removes that
exception. Every unavailable or incomplete final-barrier capture vetoes every
launch, including cold start and post-state-loss launch.

This accepts a possible one-condition cold-start outage—snapshot capture
failure—to eliminate a three-condition duplicate-launch race: supervisor state
loss, snapshot loss, and an already-live wrapper. It is a safety change, not a
restatement of shipped behavior.

**STATED activation constraint binding 87-C:** The strict barrier cannot
activate unless 87-B durable incident persistence and routine operator
projections are capability-active for the same supervisor generation,
including first-managed grace and chronic snapshot-failure projection.
Otherwise 87-C must retain shipped behavior; a silent cold-start strand is not
an acceptable partial activation.
87-A implementation close and 87-C activation additionally require reviewed
87-B/follow-up operator-manual and tutorial evidence explaining all three
limitations above: child-death recovery can remain indefinitely `POLICY_HELD`
when capability is unavailable; provenance loss remains held because automatic
quarantine retirement is unavailable;
and declared transfer/restore/rollback/migration activation refuses before
active-store admission while an out-of-band bypass is nonconforming and
unvouched. The evidence names the held agent for the first two, identifies the
rejected operation/store for the third, and directs the operator to act.

## Heartbeat boundary and task #116

**STATED scope boundary:** 87-A does not shorten the existing per-CLI heartbeat
threshold. A fresh-but-absent wrapper remains governed by 180 seconds for
wrapped Claude and 2,400 seconds for wrapped Codex under 87-A's automatic
timing function (`src/agenttalk/supervisor.py:360-375` and
`src/agenttalk/supervisor.py:4521-4535`). Therefore 87-A alone does not fix the
measured roughly 38-minute absent-Codex outage. Task #116 owns that recovery
change.

**STATED epistemic distinction:** The v0.46.0 timing invariant says stale
recovery for a live Codex wrapper must remain above the 1,800-second turn
watchdog plus margin, or the supervisor can relaunch into the same live wedge
(`src/agenttalk/supervisor.py:5590`). A synthetic timer heartbeat infers
progress from silence and can mark a silently wedged turn healthy forever.
Two independently captured complete absence observations are positive evidence
that no wrapper process exists. There is no live wedge to relaunch into, so the
live-wedge timing rationale does not transfer. Within this model, task #116
changes the timing-eligibility decision; its independently stageable
implementation need not wait for these proposed type names and must not weaken
two compatible independently captured absence polls, atomic one-use
reservation/consumption, targetability, or the fresh final barrier.

## Accepted residual

**STATED residual, not fixed by 87-A:** Raw active-child discovery may still
flap. With presence `PRESENT_TARGETABLE`, a stale poll whose dominant runtime
state is `CURRENT_PROGRESS_HEALTHY` does not escalate; a stale poll at the same
presence that lands in `CURRENT_UNKNOWN_ACTIVE_CHILD` does. (`UNKNOWN` presence
already escalates in both rows.) If a later poll produces the unknown-child
observation, that poll reaches mandatory escalation. 87-A supplies no fairness,
recurrence, wall-clock, or poll-count guarantee that a later ambiguous
observation will occur. The bounded residual is nondeterminism in which
observation escalates and continued display churn; kill authority is not
widened. A silent-forever claim is deliberately not made.

## Mandatory conformance evidence

**ENFORCED release barrier:** No implementation may claim 87-A conformance
without all of this executed evidence:

1. Generate the 96 dominant-projection cells and assert the specified distributions,
   formula parity, and zero missing/duplicate/extra cells.
2. Generate canonical overlapping reason sets. Secondary-reason permutation
   cannot change authority, action, or escalation; adding/removing a secondary
   reason must change `RecoveryConditionFingerprintV1`.
3. Execute Lens C's concrete
   `{CURRENT_UNKNOWN_SEQUENCE_REGRESSION, CURRENT_TEARDOWN_PROOF}` fresh/stale
   counterexample.
4. Prove heartbeat permutations cannot change `RuntimeObservationV1`; runtime
   contract permutations cannot change `WrapperPresenceResultV1`; changing
   unrelated wrapper-presence candidates while holding active-child input fixed
   cannot change runtime; and relevant child-lineage evidence changes runtime
   only through the closed `ActiveChildObservationV1` mapping. Inject a
   presence-only conflicting candidate and an active-child-only lineage
   conflict in both orders: each marks only its own availability projection
   incomplete. Global snapshot failure marks both unavailable.
5. Cover targetable plus unreadable, targetable plus definite unowned,
   all-owned guarded, duplicate, rootless, PID-reused, unreadable-only,
   snapshot-unavailable, incomplete-empty, and complete-empty candidate sets.
   A full-field-matching duplicate owned/guarded row must collapse to one target and remain
   `PRESENT_TARGETABLE`; a conflicting same-PID duplicate must be `UNKNOWN`
   with incomplete targetability.
6. Assert `TargetabilityProofV1.COMPLETE` has a candidate-to-target
   bijection. Every other proof variant denies both automatic and manual
   teardown.
7. Cross every current runtime dominant with reachable `ABSENT`, plus all
   contract-degraded and uncertain states with all presence states.
8. Make automatic and fully authorized manual teardown true together. Assert
   one `MANUAL_AUTHORIZED` teardown, one authority ID, manual gate semantics,
   and evaluation-order independence.
9. Cross absent path, corrupt/non-object/duplicate/oversized marker, unreadable
   path, raced replacement, expiry, failed live reauthorization, force/kill-ack
   requirements under both fresh and stale protected live-kill candidates,
   cooldown, consumed ID, targetability hold, and confirmed-absence no-kill
   candidate with simultaneous automatic authority. Only `ABSENT` and
   `CONSUMED` may fall through; no-kill requires force but not kill
   acknowledgement.
10. Confirm physical absence from two adjacent compatible ordinary captures
    while heartbeat is fresh. Assert automatic `HOLD`, then separately assert
    fresh manual bypass and stale automatic eligibility.
11. Exercise same-capture replay, nonconsecutive polls, changed coverage,
    managed-identity change, `PRESENT_*`, `UNKNOWN`, snapshot failure, and state
    loss against every absence-state transition.
12. Atomically consume a confirmed proof once. Dry run and policy hold must not
    consume; a second reservation, barrier veto, spawn failure, or ambiguous
    launch cannot reuse it.
13. Inject failed teardown, survivor, unavailable post-teardown capture,
    changed coverage, and a clear scan after failed teardown. Each contributes
    zero polls; the next ordinary clear scan is only `OBSERVED_ONCE`.
14. Execute fixed fingerprint vectors across every supported implementation.
    Candidate permutation and full-field-matching duplicates must be invariant; changing the
    semantic `RuntimeObservationV1.reasons` tuple, a presence reason, or an
    overflow-tail item must change the fingerprint. Active-child diagnostic
    subreason changes alone must not, because they normalize to one banked
    runtime reason.
15. Prove the planner, post-teardown resolver, and final barrier use the same
    observer recognition and coverage-signature implementation.
16. Prove `CONDITIONAL_POST_TEARDOWN` is never persisted and every resolution
    path returns synchronously to cleared launch permission or `NONE`.
17. Assert unsupported runtime schemas expose only a bounded duplicate-safe
    schema envelope; no future identity, health, target, or authority field is
    salvaged.
18. Cover the accepted stale healthy/unknown-child escalation-latency residual
    without converting either cell into kill authority.
19. Cover first-managed and real-launch grace just before and at
    expiry; heartbeat at and just over the threshold; missing,
    malformed/future-skew/overflow heartbeat; repeated snapshot failure;
    accepted future skew at and rejected skew just over the configured bound;
    restart persistence; and one observation-only bounded grace after state
    loss while `STATE_PROVENANCE_LOST` still denies action. Cover
    configured launch grace and heartbeat skew at zero and with negative,
    nonfinite, and Boolean inputs. Failed state commit cannot re-anchor
    freshness or authorize action.
20. Exercise baseline sequence 5, torn invalid read, same-turn sequence 4,
    then sequence 6. High-water remains monotonic, the latch becomes and
    remains true, degradation reasons overlap deterministically, and only a
    strict bound higher turn/new wrapper clears it. Also exercise same-wrapper
    turn 5 to turn 4 to turn 5; turn-generation high-water and latch never
    clear on either replay. Accept strict nonnegative integers above `uint64`
    for both generation and progress sequence. Dead/stall counters cannot
    cross invalid read, identity change, or progress. Replaying one capture ID
    or skipping an ordinary poll cannot increment either counter.
21. Race two deltas from one state revision and prove one checked commit wins;
    the stale writer reloads/re-reduces or fails closed. Persist/reload retains
    freshness anchor, continuity baseline/high-water/latch, child counters,
    absence state, and consumed marker IDs. A cached whole-state save cannot
    roll any field back.
22. Cover guarded live child, ancestry-ambiguous child, first/confirmed
    complete child absence, unrelated rootless wrapper, row shuffle/full-field-matching
    duplicate, and relevant lineage change. Assert the independent presence
    and active-child projections and their permitted runtime effects.
23. Inject `snapshot unavailable + no prior process state` at the final
    barrier and require veto. Prove 87-C activation refuses the strict barrier
    when matching-generation 87-B incident projections are not active.
24. Reconstruct `RecoveryConditionV1` and
    `RecoveryActionResolutionV1`; assert the canonical condition byte-identically
    reproduces the banked fingerprint and that bounded relevant, rootless,
    foreign-root, unreadable, structural-command, and overflow-tail diagnostic
    evidence reaches the typed 87-B boundary without changing authority.
25. Assert idle and terminal strict records map to
    `CURRENT_STALE_RECOVERABLE` under both freshness values; the first strict
    idle record after degraded replacement establishes a new baseline; and a
    returned-wrapper terminal record with cleared heartbeat still enters safe
    stale recovery rather than green health.
26. Route every automatic relaunch-only candidate through backoff, protection,
    readiness, configuration/lead-loop, absence, and final-barrier gates.
    Protected, configuration-held, and stood-down agents remain held
    automatically, while a fully authorized manual candidate has only the
    explicitly specified overrides.
27. Failure-inject each recovery execution transition under both origins:
    reservation compare race, teardown failure, barrier veto, spawn failure,
    crash during spawn, ambiguous identity, guarded-identity commit, and
    post-ambiguity absence. Assert one durable ownership fence prevents a
    second automatic or manual launch and only two captures strictly after the
    stored ambiguity boundary resolve it. For manual origin also cover readiness
    compare-clear for matching generation, different-generation automatic
    supersession, consumed-ID eviction, and marker replacement/deletion or
    authorization-snapshot change after reservation at both action fences;
    neither stale marker bytes, changed authorization, nor a stale state
    revision can execute. Concurrent marker replacement/reservation obeys
    config-before-state lock order without deadlock. Cover missing, null,
    future, negative, Boolean, nonfinite, and valid `last_launch_epoch` plus
    zero/nonfinite cooldown configuration.
28. Cross automatic and manual candidates with every
    `ExecutionEligibilityV1` value. Assert no manual request overrides dry run,
    kill switch, stopped supervisor, disabled actions, absent report entry, or
    `auto_restart != true`; every noneligible value performs zero
    recovery-authority/marker/config/launch mutation. Except for dry run,
    commit observation-only freshness/continuity/reset deltas and prove an
    absent report entry still reaches finite stale escalation. Separately
    assert that the future 87-B kill-switch observation exception can persist
    only condition evidence after task #114 and cannot reserve or execute
    recovery authority. Toggle each gate after reservation, immediately before
    kill, and after teardown immediately before spawn; inject unreadable
    instance/report/config/kill-switch captures and action-latch epoch change.
    Every mismatch vetoes the next OS action and preserves the corresponding
    origin-specific state delta.
    Independently construct a legitimate `EPHEMERAL_TERMINAL` call, then race
    an action-latch epoch change after construction but before dispatch; repeat
    with a kill-switch activation while the latch remains unchanged. Each race
    must permit exactly one winning dispatch admission and then terminate as a
    typed pre-effect rejection, preserve the persisted `next_entry` byte-identically, and
    create no native plan, raw array, or effect; no second admission may exist.
    With both gates unchanged, retain the
    latch read guard through issuance and preserve the private native body's
    separate final kill-switch check. Add a third race after the outer checks
    and native-plan construction but before that inner check: the typed result
    must be `ACTIONS_DISABLED_NO_EFFECT`, the call must become
    `REJECTED_NO_EFFECT(FINAL_ACTION_GATE_CHANGED)`, `next_entry` must remain
    byte-identical, and no lexical raw array or native effect may exist.
29. Execute every mandatory case in the normative owned-childless module,
     including initial versus rootless debt-residual observation, joined
     action-time evidence, tree-closure races, action-time target-rebinding
     veto, crash reconciliation, manual/automatic teardown debt, result
     precedence, retained closure release, every execution/debt/cycle
     cross-product, active/exhausted no-candidate polls, three-attempt
     exhaustion, and no fourth automatic reservation.
30. Recompute all banked matrix/fingerprint evidence after module integration;
    prove initiation narrows only the child-death-sourced subset, while outstanding debt
    globally suppresses relaunch/different-owner proofs, exhaustion suppresses
    only automatic named childless teardown, manual-wins wraps the same proof,
    and module/core version skew fails conformance.
31. Produce two complete zero-child captures before the same strict turn's
    nonrenewable child-establishment closure. Both map to
    `UNKNOWN(CHILD_ESTABLISHMENT_OPEN)` and neither counter advances. Repeat at
    at active age 30 seconds and after 30 seconds but before a longer
    generation launch fence; all remain open. Prove the exclusive launch-fence
    deadline is the first eligible count-one capture after the inclusive
    active-age grace ends, all closed-guard fields participate in
    reservation/action equality, and no observation can renew or shorten
    either anchor. Phase-flip the same wrapper/turn from `ACTIVE` to non-active
    and replay `ACTIVE` with equal sequence and a newer `updated_at`; the keyed
    guard and both original anchors must remain byte-identical. A same-turn
    changed key or missing retained guard is `UNKNOWN`, never a new grace.
    The guarded-launch identity commit must retain
    `launching=true/readiness_seen=false`; only matching-generation guarded
    readiness clears that generation fence.
32. With fresh operation-scoped permits, pause two pollers at the
    closure-acquire commit/effect gap and at the teardown-arm/`Stop-Tree` gap.
    A live foreign continuation retains every
    fence. After positive predecessor-death proof, only matching idempotent
    reconciliation may proceed; `ARMED` teardown never proves completion or
    permits reissue, and `CALL_RETURNED` alone permits post-action capture.
    From retained `STOP_TREE/CALL_RETURNED`, make reconciliation return matching
    `RELEASED`; require receipt-bound finalization as `EFFECT_UNPROVEN`, retained
    debt, childless `IDLE`, and zero residual-capture calls or launch.
    Exercise the state-only `PRE_BARRIER` release and every prescribed no-call
    takeover mapping with its distinct permit before only the closed table's
    table-prescribed next reconcile, release, retired-cleanup, or crash-result operation.
    For acquire, reconcile, release, retired reconcile, `Stop-Tree`,
    post-action capture, and spawn, require three distinct permits: arm-state
    mutation, post-CAS external call, and receipt mutation. A consumed arm
    permit cannot construct the call; a call permit cannot apply its receipt.
    Independently replay, cross-operation/use substitute, and race each permit
    against a revision change; none may construct a mutation or call.
33. Recreate checked state after loss at childless attempt one, two, and three,
    and after partially acted debt. Require `STATE_PROVENANCE_LOST` to deny
    every kill/launch/mutation despite a new epoch or same physical owner.
    Prove no automatic quarantine-retirement constructor exists in V1. A
    structurally valid stale backup, a locally different owner, and complete
    local absence must remain quarantined pending attended handling. Reject
    each explicitly insufficient process-universe input named by the module.
34. Persist `SPAWN_IN_FLIGHT` with null `spawned_guard` only. A valid returned
    guard atomically commits identity without that intermediate state; an
    ambiguous return enters `AMBIGUOUS_LAUNCH`, where nested and evidence guard
    copies are source-equal and match the ambiguity code's nullability.
    Reload of a valid-guard standalone `SPAWN_IN_FLIGHT` is invalid and holds.
35. Cross manual/automatic origin with confirmed whole-wrapper absence and
    child-death-sourced residue. With debt `NONE`, both select no-kill
    `RELAUNCH_ONLY`; outstanding debt suppresses both. Independently construct,
    production-encode, require byte-identical payloads, and hash the module's chained seven-domain
    vector as required by its conformance item 20; treat its byte-flip chain as
    change detection rather than independent codec correctness. Race
    nonordinary capture-ordinal allocation as required by its conformance
    section.
36. Integrate the module's merged-#120 FILETIME-identity adapter and post-kill barrier
    control through the appropriate sealed #146 dispatcher variant. Prove an
    openable planned Windows target guarded by `OwnedExactStartGuardV1` uses one
    native handle for FILETIME verification and termination, and that every
    successful termination receives a wait attempt within the remaining shared
    tree-wide budget. Inject open failure, `OwnedExactStartGuardV1` mismatch, termination
    failure, wait timeout, and depleted budget; each must defer completion to
    the fresh snapshot and barrier. Race a
    recorded parent that creates a descendant after planning and exits during
    `Stop-Tree`; require the unplanned descendant to miss the target set,
    survive, and be detected only by the fresh deny-only barrier before
    `SPAWN_IN_FLIGHT`. Recycle that parent PID: a replacement child with the
    same-or-newer Windows creation FILETIME must be excluded from the
    retired-parent ownership edge, while a
    pre-recycle or incomparable child remains conservative survivor evidence.
    Independently classify the replacement-side child as this agent's
    wrapper/wait and require the ordinary barrier evidence still to block. The
    barrier must hold launch
    without retargeting the descendant, proving `COMPLETE_GONE`, clearing debt,
    or substituting for the closure successor. An unavailable/ambiguous barrier
    also holds, and an unblocked barrier without the typed post-action proof
    cannot clear debt or launch.
    Feed a valid non-Windows `linux:<boot_id>:<start_ticks>` snapshot and require
    pre-reservation
    `CAPABILITY_UNAVAILABLE(EXACT_TARGET_EXECUTOR_UNAVAILABLE)`, no destructive
    tuple/authority, no reservation/attempt/debt, and no `Stop-Tree` call. The
    legacy rounded-start branch and any relaxation of the Windows FILETIME
    requirement are forbidden. Derive availability from the action site, not
    the accepted snapshot/token path.
37. Integrate every `ClosureCapabilityV1` reason before reservation and require
    zero reservation, attempt, continuation, external call, teardown, retry,
    and exhaustion with continuous `CAPABILITY_UNAVAILABLE`/`POLICY_HELD`
    pending a human. Inject a post-reservation structural-unavailability claim
    and `UNKNOWN(CAPABILITY_UNAVAILABLE)` reconciliation; retain every fence
    byte-identically and forbid `CLOSURE_VETOED`, retry, exhaustion, kill, and launch.
    Export all applicable module attention codes in their prescribed order,
    including `CHILDLESS_STATE_PROVENANCE_LOST` and
    `CAPABILITY_UNAVAILABLE`, and require `action_attention_required=true`.
    Join the action resolution to the matching `canonical_condition.fingerprint`; every 87-B
    rendering must name `canonical_condition.agent_key` and say operator action is required.
    Exercise the required preflight direction, not only generic zero-effect behavior.
    On a staged Windows path with valid `OwnedExactStartGuardV1` semantics and no #146
    dispatcher seal, require witness construction and fresh capability to return
    `DISPATCHER_SEAL_UNDELIVERED` before permit or closure-provider
    evaluation. Repeat from a retained Windows effect envelope: preserve the
    envelope byte-identically except the separately permitted #115 observation
    projection, export generic `CAPABILITY_UNAVAILABLE` attention, and construct
    no permit, mutation, call, or effect. On Linux and macOS, where the named
    target executor and seal are both absent, require
    `EXACT_TARGET_EXECUTOR_UNAVAILABLE` to take precedence. With a staged
    conforming Windows seal present but the closure successor absent, require an
    available witness followed by `SUCCESSOR_MISSING`, never
    `DISPATCHER_SEAL_UNDELIVERED`. These are proposed conformance outcomes, not
    runtime behavior at merged `587e7c1`.
38. Integrate the module's construction-seal suite. Prove the checked owner has
    exactly six internal mutation variants, exposes the custody-bound
    sealed-receipt ordinary
    observation API rather than a caller-supplied observation mutation, and has
    no raw whole-state or childless-envelope write. Exercise every public decoder/reducer and every childless external
    adapter with raw IDs, target tuples, bindings, persisted envelopes, forged
    receipts, copied/stale permits, wrong-operation permits, mismatched
    revisions, and lost effect guards; none may construct or apply a childless
    effect delta or external call. Round-trip persisted state and prove no
    witness, permit, executable target, typed call, receipt, private seal, or live guard lineage
    survives serialization.

    Inspect the supervisor owned-tree effect surface and require exactly one
    private native body, zero public raw-array termination entry, zero direct
    `Stop-Tree $p.kill_targets` call, and zero route from planner targets to the
    body except one `SupervisorOwnedTreeNativeInvocationV1` produced by the
    winning atomic admission and plan-ownership transitions over the call's private
    submission/use-owner pair, whose call binding is source-equal to the call. Reject raw
    arrays, caller-settable tags, wrappers around `kill_targets`, and
    field-equivalent fake variants. Prove configured-agent and ephemeral-terminal
    authorization/persistence/barrier parity through their independent private
    constructors; the childless variant accepts only the matching permit-bound
    call. Scope this scan to the supervisor owned-tree executor and exclude the
    separate turn-watchdog facility. Prove the native plan retains an immutable
    tuple and materializes its lexical raw array only for the final private-body
    invocation, with no caller or post-return alias.

    For CONFIGURED_AGENT_RELAUNCH and EPHEMERAL_TERMINAL, race two applications
    of the same source-bound checked #115 action transition. Exactly one may commit and
    activate one `READY` owner/custody. Race and sequentially replay two call
    constructors over that custody: exactly one `READY -> CALL` winner emits one
    call/submission, and every loser produces zero plan, effect, receipt,
    mutation, planner behavior, or launch. Serialize/reload the barrier_state or
    next_entry byte-identically and attempt to reconstruct, look up, or reseal
    custody from copied provenance, IDs, or digests; every case must fail. A new
    post-crash custody requires a new checked transition. Verify CHILDLESS keeps
    its stronger guard-lineage issuer and rejects a second sibling-style token,
    and verify every KEEP/SKIP cell for every dispatch variant.

    For each of CHILDLESS, CONFIGURED_AGENT_RELAUNCH, and EPHEMERAL_TERMINAL,
    retain two references to the same legitimate private submission and race
    dispatch. Exactly one atomic `CALL -> DISPATCHING` compare-and-swap may win;
    only that winner may receive one admission. Retain two references to that
    admission and race `DISPATCHING -> PLAN_OWNED`; exactly one winner may
    construct one native plan and one invocation handle. Retain two references
    to that invocation and race native entry; exactly one
    `PLAN_OWNED -> INVOKING` winner may reach the final check, lexical raw array,
    or native effect. Pause after each transition and prove the concurrent alias
    loses with zero effect. Sequentially replay the submission, admission, and
    invocation after normal receipt, positive pre-effect rejection, and uncertain
    exception; every replay must create no new capability or plan and produce
    zero effect.
    Noncopyable must not be treated as protection against aliasing the same
    object. Prove the childless constructor moves its existing lineage and each
    non-childless constructor consumes its pre-existing action owner rather than
    minting one. Admission/invocation handles retain the same owner outside the
    deeply sealed value graphs; a
    caller mutex, external call-ID registry, or unstated dispatcher lock is not
    a conforming substitute.

    Retain two references to every legitimate
    `SupervisorOwnedTreeDispatchReceiptCustodyV1` and race consumption. Prove
    the sealed receipt alone cannot locate or mutate an owner. For
    CHILDLESS exactly one `RECEIPT -> PERMIT` receipt-mutation admission may win.
    For CONFIGURED_AGENT_RELAUNCH and EPHEMERAL_TERMINAL exactly one
    `RECEIPT -> CONSUMING_RECEIPT` compare-and-swap may win before existing
    planner behavior. Normal success must end `CLOSED`; a possibly-started or
    uncertain planner behavior/commit must end
    `POISONED(PLANNER_COMMIT_UNCERTAIN)`. Inject synchronous failure
    after receipt admission but before mutation/planner behavior and require
    childless `CUSTODY_PROTOCOL_BROKEN` or non-childless
    `DISPATCH_PROTOCOL_BROKEN`, never a return to `RECEIPT`; distinguish these
    from childless `OWNER_COMMIT_UNCERTAIN` and non-childless
    `PLANNER_COMMIT_UNCERTAIN` after work may have begun. Concurrent losers and
    sequential custody-handle replay must produce zero mutation, launch, or effect.

    For EPHEMERAL_TERMINAL, separately change the action-latch epoch and activate
    the kill switch after legitimate call construction but before dispatch.
    Each case must permit exactly one winning admission, consume the call as
    `REJECTED_NO_EFFECT`, preserve `next_entry` byte-identically, and create no native plan,
    raw array, or effect; a second admission or replay remains rejected.
    With both unchanged, require the same enabled latch epoch under its read
    guard immediately before plan construction, a separately fresh clear
    kill-switch check, retention of the latch guard through issuance, and the
    native body's final kill-switch check. Pause after the outer checks and plan
    construction but before that inner check, activate the kill switch, and
    require the invocation-bound typed `ACTIONS_DISABLED_NO_EFFECT` result, one
    `REJECTED_NO_EFFECT(FINAL_ACTION_GATE_CHANGED)` transition, byte-identical
    `next_entry`, no lexical raw-array materialization or native effect, and
    zero-effect replay.

    At each post-construction non-childless owner stage—`CALL`, `DISPATCHING`, `PLAN_OWNED`,
    `INVOKING`, `RECEIPT`, and `CONSUMING_RECEIPT`—terminate the supervisor
    process or abandon its worker before receipt/poison publication and reload
    the persisted checkpoint. For EPHEMERAL_TERMINAL, a new #115 checked action may retry only
    the source-equal #120 target tuple: gone PID is a no-op, recycled PID/different
    FILETIME is refused, and the same live PID/same FILETIME remains the intended
    same-handle target; only the fresh teardown barrier may permit archive.
    For CONFIGURED_AGENT_RELAUNCH, require the source-equal persisted issuer checkpoint
    at every stage. As soon as custody cannot be validated, reload must derive
    `ConfiguredPreBarrierOwnerLossHoldV1` with `LIVE_OR_UNPROVEN`. A fresh
    independent OS result that the source-equal issuer PID is absent refreshes
    it to `PROVED_GONE(result=GONE)`. Hold that PID present twice, once with the
    same generic start token and once with a different generic start token; both
    cases must remain `LIVE_OR_UNPROVEN`, expose the bounded
    `CAPABILITY_UNAVAILABLE(ExactIssuerIdentityAdapterV1)` dependency, and
    refuse disposition. A literal-token comparison and a tolerant fallback
    comparison must both be incapable of producing `PROVED_GONE`. Make the OS
    observation unavailable and require the same blocked posture. Both hold
    forms render the remedy, but only definitive PID absence admits
    disposition. Recompute the domain-separated source hash from the canonical
    persisted-field projection displayed in the type;
    require matching hashes before and after the extinction refresh, and reject
    inclusion of volatile extinction/action fields, hash-including/fixed-point,
    or partial alternatives. Neither hold may remint custody,
    invoke `PRE_BARRIER_RELEASE`, silently release and reserve again, kill, or
    launch. Exercise the checked attended disposition under the official-store
    locks and require its one checked winner to install
    `ConfiguredPriorEffectUnknownFenceV1`, return `IDLE`, and perform zero kill
    or launch while explicitly retaining `PRIOR_EFFECT_UNKNOWN`. Exercise every
    rejection predicate alone and in combination and require the displayed
    precedence plus the closed typed reason/resolution/next-step variant. An
    unauthorized actor must always receive `UNAUTHORIZED` and
    `REDACTED_UNAUTHORIZED`, even when a current hold exists and every other
    field is valid; no PID/start/token, checkpoint target, source hash, or hold
    detail may escape. Wrong store/state, stale source hash, wrong
    agent/epoch/revision/reservation/checkpoint, malformed request or any missing
    acknowledgement, live/unproven issuer, circular issuer-supplied death
    evidence, absent kill switch, or present singleton must produce its closed
    mapping and zero mutation/effect. In particular,
    `KILL_SWITCH_ABSENT` directs create-kill-switch, stop-all-supervisors, then
    refresh; it may not say to preserve a nonexistent switch. Map the
    prospective CLI's three flags field-for-field to the request tuple. Race and replay two valid requests and require
    one state change; while its fence remains, recover the same result from the
    persisted request/actor/acknowledgement/reason binding, and after clearance
    reject the old request as stale. With
    `ConfiguredPreBarrierRetrySuccessorV1` absent, direction-control every other
    transition and prove attended disposition is the sole exit from owner loss.
    Independently set a non-null prior-effect fence, change the current
    `RESERVED/PRE_BARRIER` predecessor, and stale
    `expected_hold_source_hash`; each must reject with zero mutation, kill, or
    launch. These three replay gates remain mandatory under every issuer
    observation outcome.
    While its resulting fence is non-null, direction-control configured and
    childless reservation/effect/archive/spawn/launch entries: all hold with zero
    action. Keep `supervisor.kill` present after disposition and prove
    `PRIOR_EFFECT_FENCE_CLEAR` remains ineligible. Then remove the kill switch,
    start one current supervisor, prove the fence still blocks every action while
    ordinary observation proceeds. Independently and in combination set the
    action latch disabled, report membership absent/unreadable, and auto-restart
    disabled; narrow fence clearance must remain eligible while every subsequent
    action stays blocked by full `ExecutionEligibilityV1`. Dry run, a non-clear
    kill switch, or a noncurrent supervisor must return the corresponding
    `POLICY_HELD_GATE` reason after the sole `RECEIPT -> COMMITTING` winner,
    close that custody, and require a fresh committed capture after repair.
    Missing, corrupt, quarantined, or otherwise untrustworthy state must instead
    return `POLICY_HELD_STATE_PROVENANCE_LOST`, expose no current fence, close
    the old custody, and require attended repair plus a fresh capture. Race two ordinary lineages begun from the
    same predecessor; although their prospective capture IDs are equal, only the
    winning state commit may publish `CommittedOrdinaryFenceCaptureV1`, and the
    CAS loser cannot mint or substitute a barrier receipt. Retain two aliases of
    the winning witness and require exactly one `READY -> ADAPTING` winner.
    Require that its matching fresh source-bound #120 CLEAR receipt custody alone
    may commit `PRIOR_EFFECT_FENCE_CLEAR`; advance the checked revision after
    receipt creation and require the closed stale/mismatched result, closed
    custody, and intact fence. Race same-custody aliases for every outcome; only
    the CAS winner may read the store or return a state-bearing result, while
    losers return `NOT_READ_BY_LOSER`. Map `BLOCKED`, `AMBIGUOUS`, and `UNAVAILABLE` to
    their separate remedies, close the admitted custody, and require a fresh
    committed capture. Inject a positively pre-CAS no-write failure and require
    `FAILED_PROVED_NO_COMMIT`, the current fence, and closed custody. Inject
    failure immediately before the state CAS, immediately after a successful
    state CAS but before owner close, and after owner close but before response.
    Any point at which the state CAS may have run must return or reconcile as
    `CLEAR_COMMIT_OUTCOME_UNKNOWN`, never assert fence preservation; require
    `POISONED`, `CLOSED_RESPONSE_LOST`, or `OWNER_LOST_WITH_PROCESS` according
    to the injected failure point. A checked reload must derive whichever of
    `FENCE_STILL_CURRENT`, `FENCE_CLEARED`, or
    `STATE_OR_FENCE_UNTRUSTWORTHY`. Every wrong-source, alias, or replay receipt
    produces no additional transition. Verify
    the action export includes the complete bounded hold/fence summaries and
    their concrete remedies rather than bare codes, while excluding the issuer
    token/PID/start, action epoch, full target tuple, `authority_id`,
    `checked_reservation_transition_id`, actor, acknowledgements, and free-text
    reason.
    With #57 absent require activation refusal before `Start-Process`; after #57
    is delivered, its own review must prove a crash or abandoned worker cannot
    create two live wrappers for one agent. No test may treat target-local #120
    kill identity as launch idempotence.

    Prove the private witness path is acyclic: the conforming Windows
    dispatcher's unexported capability factory mints the current witness before
    any permit or call exists, and the later childless call is accepted only by
    that same live dispatcher instance. Reject public/fake factory access,
    copied or cross-instance witnesses, witness creation from a submitted call,
    and witness creation by either non-childless variant.

    Reject any composite/multi-tag mutation. In a non-dry-run poll, consume one
    `OrdinaryObservationCommitCustodyV1`, derive/commit only the private
    `OBSERVATION` mutation, reload its successor revision, and then construct at most one
    later authority/effect delta against that revision. Prove every childless
    outer-field update is present in the private
    `ChildlessOuterStateDeltaV1` projection for its permit operation, and reject
    omitted, extra, or caller-selected fields/values.

    Apply `NonChildlessAuthorityDeltaV1` to every current `CHILDLESS` envelope
    shape, including `IDLE`, `SPAWN_IN_FLIGHT`, and `AMBIGUOUS_LAUNCH`; the
    checked owner must reject it before field application. Composition with an
    owner-private observation mutation must not change that result. Only a matching
    `CHILDLESS_EFFECT` delta may replace or remove the envelope.

    Construct `StateLossQuarantineCreationDeltaV1` from every outer-state loss
    and require the complete replacement state to be byte-identical to the displayed
    quarantined genesis; reject a partial/default-from-lost-state object.

    For every `ExactTargetExecutorOperationV1` and permit use, construct the
    object only from a fresh action-site witness plus an inert binding source-equal
    to the current envelope's binding,
    current revision, and closed operation scope. Normally require the
    authorized tuple or residual subset; for the closed targetless old-side
    rebind, retired-cleanup, and owner-transition scopes require a binding source-equal
    to the selected historical tombstone/envelope binding plus the complete prospective proof
    or typed subject/checkpoint instead. Consume it
    once; replay and cross-operation use must fail. Feed an unknown future
    childless execution variant through the compatibility boundary and require
    closed rejection or inert evidence, never an action object. Use fresh
    selection, an inherited external-effect envelope, childless
    `SPAWN_IN_FLIGHT`, `AMBIGUOUS_LAUNCH`, and a retired tombstone as direction
    controls on Linux and macOS: the effect envelope remains byte-identical, no adapter
    runs, and only the separately typed observation projection may advance.
    From one guard acquisition, race two issuers and attempt B1/P1 then B2/P2
    before P1 resolves. Exactly one `AVAILABLE -> OUTSTANDING` issuance wins;
    P2 rejects with zero mutation/call. Assert one lineage_id/issuance_id moves
    through permit, call, winning dispatch admission, plan ownership, native
    invocation, receipt, and receipt permit. After custody returns,
    exactly one successor issuance may proceed and P1 replay fails. Inject
    admission-result handoff failure and plan/invocation-handle construction or
    handoff failure. For every variant, throw deterministically during final-gate
    evaluation and on private-body entry after `INVOKING` but before the native-
    effect frontier; require the invocation-bound typed
    `NATIVE_ENTRY_FAILED_NO_EFFECT` and the displayed childless/non-childless
    owner transition. Retain and replay two result aliases; only one transition
    or custody return may occur. Throw at/after the frontier and at an unknown
    locus; require the applicable `ADAPTER_EFFECT_UNCERTAIN` or
    `NATIVE_EFFECT_UNCERTAIN` poison. Fail construction/handoff/owner resolution
    of a positively pre-frontier result and require the applicable
    `CUSTODY_PROTOCOL_BROKEN` or `DISPATCH_PROTOCOL_BROKEN`. Keep
    `ACTIONS_DISABLED_NO_EFFECT` as a separate false-gate oracle. Continue
    failures after native return, during receipt handoff, receipt consumption,
    and owner/planner commit. Each resolves exactly once—never both or neither.
    Exception class, elapsed time, absent result/receipt, caller flags, or a null
    raw-array observation never proves no effect. A poisoned lineage cannot
    issue again before guard release/reacquisition and never reissues an
    uncertain effect.

    Reproduce the nested mutation probe: construct a legitimate sealed call
    whose nested target PID is 101, tamper it to 202 through the controlled
    unsafe hook, and require consumer rejection before native effect. Public
    mutation must fail, and mutating the original caller alias after construction
    must leave every sealed field of the constructed call unchanged. Repeat for
    permit proof/residual, target set,
    mutation next state, call arguments, receipt result, and both non-childless
    provenance variants. A frozen outer record or digest-only check is
    insufficient. Inspect every sealed graph and prove the atomic lineage owner
    and non-childless dispatch-use owner cells are unreachable; only immutable
    custody/use proofs are present. Prove the private submission, admission, and
    invocation and receipt-custody handles are the only associations between
    their source-bound immutable values and owner, and each holder transition changes the separate cell
    exactly once.

    Submit the former public observation-record shape with forged child-dead
    count two, owned-childless count two, and confirmed absence; the #115 public
    API must reject it before any field write. Reject public/direct/fake access
    to the receipt factory, an attempt to exchange raw evidence for a receipt
    outside the installed observer adapter, and a receipt minted by a substituted
    adapter. Prove only the #115-created lineage passed to the installed adapter
    can mint one sealed receipt from one completed acquisition. Retain two
    references to the same private acquisition handle and race the installed
    adapter: exactly one `UNUSED -> ACQUIRING` compare-and-swap may win before
    observation; the loser and sequential replay perform zero acquisition and
    mint no receipt. Inject receipt/custody construction and atomic-yield
    handoff failure and require one `POISONED` owner with no returned custody
    handle. Retain two references to the resulting
    `OrdinaryObservationCommitCustodyV1` and race #115 commit; prove the sealed
    receipt alone cannot locate its owner. Exactly one
    `RECEIPT -> COMMITTING` transition may win before
    reducer derivation or field application; the loser and sequential replay
    perform zero mutation. Require that
    lineage to contain the source-equal prospective ordinal-zero `CaptureIdV1` before
    acquisition begins. Prove `ProcessObservationV1.capture_id`,
    `receipt.prospective_capture_id`,
    `PrivateClassifierObservationMutationV1.capture_id`, and every non-null
    successor capture-ID field that records the current sample to be source-equal
    to that begin-bound ID. Empty/reset successors retain null IDs; no
    successor may synthesize a different ID or restamp at commit. From a checked
    predecessor sequence `n`, require the begin-bound ID, raw/tree displayed
    sequence, and candidate mutation sequence to equal checked `n + 1`; only
    after the winning commit may current equal `n + 1`. At maximum `uint64`,
    require the typed `ATTENDED_REQUIRED(CaptureSequenceExhaustionV1)` with the
    agent/epoch/revision, maximum value, `READY` or ordered blockers, and attended
    action; require no acquisition handle, observation, receipt, mutation, 87-A
    action, or wrap. From top-level `IDLE`, race two byte-identical attended rollover
    requests: exactly one checked replacement wins. Require the displayed
    reset field set to be source-equal to its specified values and the preserved
    field set to remain byte-identical to its predecessor values.
    With a non-null prior-effect fence at the
    maximum sequence, require every disposition/audit/source/effect field to be
    byte-identical and only its freshness floor to become `(new epoch, 0)`;
    a null fence remains null. Require the first new ordinary ID to be
    `(new epoch, same agent, 1, 0)`, and reject every old-epoch lineage, receipt,
    custody, ID, confirmation, and proof. Retain an in-flight old-epoch
    observation handle across rollover and require its later commit to reject or
    poison with zero mutation. Independently test non-childless execution and
    every childless envelope/execution/debt/cycle/continuation/retired-attempt
    blocker, alone and combined; require byte-identical state, no new epoch or
    quarantine, and no reset attempt budget. Verify the typed 87-B projection
    names the agent, old epoch/revision, complete typed disposition/blockers, and attended
    operation; a bare enum fails. One legitimate receipt from
    count zero may reach at most count one/`OBSERVED_ONCE`; a second distinct
    post-successor acquisition is required to confirm. Reject copy, replay,
    serialization, two receipts from one lineage, wrong agent/epoch/revision,
    forged sequence/ordinal, nonordinary receipt, derived successor fields, and
    mutated nested evidence. Race two valid same-revision receipt lineages:
    exactly one CAS commits and the loser is poisoned and cannot be reused.
    Attempt to pass the stale loser's completed acquisition into a successor
    lineage, reseal it, or restamp it with the successor capture ID; each must
    reject without state change or confirmation advance. Only a new begin plus
    a distinct acquisition after successor reload may mint the next receipt.
    Separately prove state-loss quarantine creation is the sole permit-free
    fail-closed state-loss mutation in a non-dry-run invocation; the attended
    capture-sequence rollover is a distinct non-effect replacement admitted only
    from trustworthy maximum-sequence persisted top-level `IDLE` state and
    blocked by any persisted non-childless execution or childless envelope.
    Reject an old-epoch barrier against the rebased fence; allow only the first
    new winning committed capture's matching barrier custody to clear, and only
    if every source target/descendant is clear. A same-predecessor loser emits no
    witness. Repeat rollover to prove the floor rebases again without erasure.

    Race rollover against an in-flight `EPHEMERAL_TERMINAL` owner at `READY`,
    `CALL`, `DISPATCHING`, `PLAN_OWNED`, `INVOKING`, `RECEIPT`, and
    `CONSUMING_RECEIPT`. Rollover visible at the final provenance read before
    plan ownership must yield one
    `REJECTED_NO_EFFECT(VARIANT_PROVENANCE_STALE)` and preserve `next_entry`
    byte-identically. If
    rollover wins after that read, including during post-read `DISPATCHING`
    before plan ownership, allow invocation to resolve normally and do not infer
    no effect. At receipt, make each CAS order deterministic: rollover
    state-CAS first yields exactly one
    `STALE_CLASSIFIER_EPOCH_RETRY_REQUIRED` and
    `CONSUMING_RECEIPT -> CLOSED`, with no state/archive/launch/new-effect
    behavior; receipt-state commit first yields `APPLIED/CLOSED`, makes the
    original rollover request `REJECTED_STALE_OR_INVALID`, and requires a fresh
    exhaustion result/request; unknown receipt commit yields
    `POISONED(PLANNER_COMMIT_UNCERTAIN)`. Accept the stale result for any
    trustworthy schema-valid same-agent official state with a different epoch;
    reject missing, untrusted, corrupt, or wrong-agent state. No case claims the
    original native effect absent. Repeat every outer-state
    loss under dry run and require only a simulated hold with zero persistence.
    Prove that no automatic V1 retirement constructor can be reached with local
    or transferred evidence. Exercise every declared same-platform state-
    file/workspace transfer, restore, rollback, and migration activation entry;
    each must refuse before active-store admission with no witness, permit,
    authority/effect mutation, external call, or launch. Require the refusal to
    identify the rejected operation/store and direct attended handling without
    fabricating an active-agent hold. As a negative boundary, show that a
    structurally valid out-of-band replacement presented as an in-place restart
    need not be distinguishable from local state: it is nonconforming, may
    proceed outside 87-A's guarantees, and cannot be claimed as a fail-closed V1
    migration path. When outer-state checks detect rollback-unproven state,
    require only `StateLossQuarantineCreationDeltaV1`. A future POSIX contract
    may not make an inherited Windows FILETIME binding actionable without
    reviewed version/migration semantics.

## Mechanism inventory

| Property | Classification | Mechanism |
| --- | --- | --- |
| Global executor gates cannot be bypassed by manual origin | ENFORCED | Closed capture/precedence after observation commit, reservation-bound semantic ID, and guarded pre-kill/pre-spawn recapture; noneligible paths perform zero recovery mutation. |
| Dry run is persistence-free and kill switch has no 87-A mutation exception | ENFORCED | Pure simulated decision/discarded delta and explicit zero-mutation gates; task #114 enables only 87-B observation. |
| Runtime/presence independence with explicit child evidence | ENFORCED | Separate runtime, active-child, and presence constructors plus narrowed permutation tests. |
| One result for overlapping runtime reasons | ENFORCED | Ranked tuple plus dominant-only authority operands. |
| Runtime high-water and sticky regression survive torn reads | ENFORCED after task #115 | Pure continuity reducer plus checked state revision; only new bound generation clears latch. |
| Missing heartbeat becomes stale after finite grace | ENFORCED after task #115 | Nonrenewable `first_managed_epoch`, guarded launch deadline, and specified freshness formula. |
| Child confirmation cannot cross uncertainty or replay | NORMATIVE CONTRACT SPECIFIED; implementation blocked on task #115 | #115 owner-private reducer over sealed one-use observation receipts derives basis-bound counters, durable capture ID, and adjacent poll sequence; none is caller input. |
| Total mixed-candidate presence | ENFORCED | Ordered aggregation and closed reason codes. |
| No partial-target kill | ENFORCED | `TargetabilityProofV1.COMPLETE` bijection invariant. |
| Absent, invalid, and unreadable manual paths differ | ENFORCED | Locked bounded raw capture and closed codec; only true path absence is `ABSENT`. |
| One origin for simultaneous manual/automatic authority | ENFORCED | Total manual gates, live revalidation, candidate-scoped acknowledgement, and manual-priority selector. |
| Reserved manual authority cannot drift before action | ENFORCED after task #115 | Pre-kill/pre-spawn marker revision plus authorization snapshot equality under the fixed lock order. |
| Consumed marker cannot replay | ENFORCED after task #115 | Revision-bound reservation, bounded committed ID set, and compare-clear. |
| Ambiguous spawn cannot release authority or duplicate-launch | ENFORCED after task #115 | Durable `AMBIGUOUS_LAUNCH` tombstone, guarded reconciliation, and no second reservation. |
| Physical proof independent from timing | ENFORCED | Separate closed values and reducers. |
| Two independent compatible absence polls | NORMATIVE CONTRACT SPECIFIED; implementation blocked on task #115 | Two distinct sealed receipt acquisitions, owner-derived capture IDs, matching coverage, adjacent poll sequence, and one winning CAS per predecessor revision. |
| Absence proof is one use | ENFORCED after task #115 | Atomic `CONFIRMED -> CONSUMED` launch reservation. |
| New guarded managed identity commits atomically | ENFORCED after task #115 | One checked transaction commits identity, manual consumption, launch grace, and reservation resolution. |
| Failed post-teardown scan contributes no counter | ENFORCED | Separate action-scoped reducer path. |
| Stable semantic condition equivalence | ENFORCED | Versioned canonical fingerprint and fixed vectors. |
| 87-B receives durable evidence rather than only a hash | ENFORCED | Typed canonical condition, action resolution, and bounded diagnostic candidate export. |
| No launch on stale proof or observer disagreement | ENFORCED | Shared final barrier after reservation. |
| Strict barrier is not silently partially activated | STATED for 87-C | Same-generation 87-B incident projection is an activation prerequisite. |
| No daemon, persistence plane, durable helper or OS object, or runtime dependency | DECIDED ABSOLUTE by operator on 2026-07-31 (M5 Option A) | Pure code, existing checked state, and transient caller-owned synchronization only; there is no mechanism-specific exception, and missing conforming action-scoped closure remains `CAPABILITY_UNAVAILABLE` when that boundary cannot prove its contract. |
| Earlier fresh-but-confirmed-absent recovery | STATED out of scope | Task #116, blocked on #115 but not #87, scheduled before 87-A implementation. |
| Durable incident visibility/delivery | STATED out of scope | Future 87-B, dependent on tasks #114/#115; every unavailable-capability projection names the held agent and required operator action. |
| Operator documentation of permanent capability limitations and configured-action residuals | REQUIRED before implementation close/activation | 87-B/follow-up manual and tutorial evidence states all four permanent limitations together plus both configured residuals: POSIX teardown unavailability, quarantine-retirement unavailability, the declared transfer/restore/rollback/migration activation refusal, present-issuer-PID/reuse disposal unavailability pending `ExactIssuerIdentityAdapterV1`, the GONE-only owner-loss HOLD remedy and subsequent fence-clearance sequence, and configured relaunch unavailable for activation until the durable per-agent singleton lands. |
| Same-platform state-file/workspace transfer, restore, rollback, and migration activation | UNAVAILABLE IN V1; DECLARED ACTIVATION REFUSES | A conforming activation path refuses before imported bytes become the active checked store and constructs no 87-A witness, mutation, effect, or launch. An out-of-band replacement may be undetectable, is nonconforming, and has no 87-A guarantee. Future 87-C must bind the source universe within M5 Option A or keep imported state inert. |
| Raw discovery stops flapping | STATED not promised | Process-discovery behavior is unchanged. |
| Owned-childless teardown requires a nonce-owned complete tree, a named target executor, and action-time closure | WINDOWS #120 INPUT/TARGET-LOCAL EFFECT DELIVERED; NORMATIVE CONTRACT SPECIFIED; TEARDOWN IMPLEMENTATION BLOCKED on #115, #146, and the closure successor; POSIX unavailable independently | Merged #120 supplies `OwnedExactStartGuardV1` Windows target/effect evidence but exposes a raw entry, supplies no POSIX process-identity target executor, and supplies no action-scoped creation closure. A fresh proof cannot create an envelope without a `RESERVE` permit; deserialized evidence cannot construct a witness, lineage, sealed call, receipt, or effect mutation. Unresolved named paths remain `CAPABILITY_UNAVAILABLE` and `POLICY_HELD` pending a human. |
| Supervisor owned-tree termination dispatch | NORMATIVE CONTRACT SPECIFIED; IMPLEMENTATION BLOCKED ON #115 AND #146 | One closed sum admits permit-bound childless, independently authorized configured-agent, and independently authorized ephemeral-terminal calls. #115 mints exactly one non-childless action owner in `READY` at the checked logical-action transition; only one constructor consumes it to `CALL`. Submission aliases then race for one admission, admission aliases for one plan, invocation aliases for one native entry, and receipt aliases before receipt-driven behavior. Replay or same-reference races at every boundary produce zero additional mutation, launch, or effect. The ephemeral variant repeats its narrow action-latch and kill-switch gates. Merged raw `Stop-Tree($targets)` and both raw callers make the seal absent until #146 migrates them and proves the private native body has no other entry. Scope excludes the turn watchdog. |
| Configured issuer PID reuse classification | CAPABILITY UNAVAILABLE; Q4 IMPLEMENTATION BLOCKED ON `ExactIssuerIdentityAdapterV1` | Only a fresh independent definitive PID-absent OS result may produce `PROVED_GONE`. Any process present at that PID, including suspected reuse from a generic start-token mismatch, remains `LIVE_OR_UNPROVEN`; attended disposal is unavailable and the hold persists. Revision 15 names but does not define `ExactIssuerIdentityAdapterV1`. |
| Configured PRE_BARRIER owner loss | NORMATIVE HOLD, GONE-ONLY ATTENDED ESCAPE, AND GLOBAL FENCE CLEARANCE SPECIFIED; IMPLEMENTATION BLOCKED ON #115 AND `ExactIssuerIdentityAdapterV1`; AUTOMATIC RETRY OPTIONAL/UNDELIVERED AS `ConfiguredPreBarrierRetrySuccessorV1` | Reload cannot reconstruct transient custody. It derives the complete operator-visible hold in proved or unproven extinction form; only definitive PID absence admits the future checked attended disposition. Disposal attests only `PRIOR_EFFECT_UNKNOWN`, never kills or launches, and persists request/audit/source targets plus an epoch-aware freshness floor. A present PID never admits disposal without the separately reviewed adapter. The singular fence pairs only with `IDLE` and globally blocks every configured/childless action, so it cannot be replaced or bypassed. After kill-switch removal and one-current-supervisor restart, #115's winning ordinary commit alone may publish the post-commit witness; #115's narrow unexported reducer over its sealed merged-#120 operands yields the source-bound barrier custody that alone may clear the fence through a no-effect checked transition. This narrow producer belongs to #115's Q4 delivery; the general adapter remains an overall 87-A dependency. |
| Configured-agent relaunch crash replay | NORMATIVE RISK SPLIT SPECIFIED; IMPLEMENTATION/ACTIVATION BLOCKED ON #57 | #120 makes the `OwnedExactStartGuardV1`-guarded kill subphase target-safe for an independently authorized retry, not authority to remint that retry and not `Start-Process` idempotence. The optional automatic retry successor remains absent. Task #57's project-level singleton per wrapped agent must close the durable duplicate-wrapper window; 87-A does not respecify it. |
| Maximum ordinary capture sequence | NORMATIVE ATTENDED PATH SPECIFIED; implementation blocked on #115 | Typed exhaustion attention and one checked persisted-top-level-`IDLE` epoch rollover reset capture-derived evidence while keeping the managed/manual/quarantine fields byte-identical. A non-null configured prior-effect fence preserves every audit/source/effect field byte-identically and rebases only its freshness floor to `(new epoch, 0)`. Non-childless execution and every childless envelope/debt/cycle/continuation/retired attempt block rollover and remain byte-identical; an in-flight ephemeral transient owner instead resolves through the closed stale-epoch receipt result with no new-epoch receipt behavior. No budget or fence is laundered. |
| Child-establishment grace cannot be sampled away | NORMATIVE CONTRACT SPECIFIED; implementation blocked on #115, #146, the merged-#120 adapter, and the closure successor for effect conformance | Nonrenewable same-turn closed guard in observation, confirmation, reservation, and action equality. |
| External childless calls cannot outlive their authority owner | PARTIAL WINDOWS #120 TARGET-LOCAL PRIMITIVE DELIVERED; full normative contract specified; implementation blocked on #115, #146, and the closure successor | For an openable Windows target guarded by `OwnedExactStartGuardV1`, merged #120 binds identity-check and termination to one handle and attempts the bounded same-handle wait. Unique lineage custody, deep seals, closed dispatch, checked continuation owner/stage, stable tombstones, and attempt-bound synchronous adapters remain absent. |
| POSIX named owned-childless teardown and inherited cleanup | CURRENTLY UNAVAILABLE | #120 accepts Linux `linux:<boot_id>:<start_ticks>` observation tokens but declares no macOS mapping, and the merged supervisor owned-tree body skips the Linux-token target because it has no FILETIME. No POSIX childless dispatcher witness exists, so neither fresh authority nor a deserialized `PRE_BARRIER`, external-effect phase, debt-only state, spawn ambiguity, or retired tombstone can construct the permit and typed object required to act. The Windows FILETIME guard is unchanged. |
| Partial owned-childless teardown cannot be laundered into launch | NORMATIVE CONTRACT SPECIFIED; implementation blocked on #115, #146, the merged-#120 adapter, and the closure successor | Origin-neutral durable teardown debt, closed dispatch, and debt-bound completion authority. |
| Automatic owned-childless retry stops at three without fading from attention | ENFORCED after tasks #78/#115 | Durable childless-only cycle, hard cap, and independent action-attention output. |
| State loss cannot reset a cap or erase teardown debt | ENFORCED after task #115, with automatic V1 retirement unavailable | Fail-closed quarantine has no automatic retirement constructor on any platform. It remains `STATE_PROVENANCE_LOST`/`POLICY_HELD` pending attended handling; local different-owner or extinction evidence and a structurally valid backup cannot clear it. |

Q4 is **SPECIFIED; IMPLEMENTATION BLOCKED ON #115, #146, #57, AND
`ExactIssuerIdentityAdapterV1`**. It is not
complete, conforming, sealed, enforced, or activatable in merged code until
those four dependencies land and their executed controls pass. Overall 87-A
additionally requires the merged-#120 adapter and closure successor and remains
incomplete, nonconforming, unsealed, unenforced, and activation-prohibited until
every named dependency lands and passes review.

This core and its same-commit normative module together are sufficient to
implement and review 87-A's pure classifier and authority substrate. Neither
file alone is conforming. They are not permission to activate the behavior and
make no delivery promise or supported migration promise beyond the explicit V1
activation refusal above.
