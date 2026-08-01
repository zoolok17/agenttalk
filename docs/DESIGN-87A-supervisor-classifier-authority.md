# Design 87-A: Supervisor classifier and recovery-authority totality

**Status:** Proposed, Revision 10 with the operator-directed owned-childless
wrapper authority; design only. This core and its
[normative owned-childless module](DESIGN-87A-owned-childless-wrapper-authority.md)
at the same commit constitute 87-A; neither is conforming alone.

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

An implementation may claim conformance to 87-A only when every ENFORCED rule
and required test below exists in executable code. Until then, all behavior
described here remains proposed.

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
  lock/API. 87-A specifies pure reducers, but durable state epoch/revision,
  freshness anchors, runtime high-water/latch, confirmation counters, poll
  identity, consumed manual IDs, absence consumption, and guarded-identity
  commit cannot ship until task #115 supplies that mechanism.
- Task #116 owns earlier recovery from twice-confirmed physical absence. It is
  mechanically blocked on task #115, explicitly not blocked on Design 87, and
  is scheduled immediately after #115 and before 87-A implementation. It
  remains independently stageable.
- Task #120 shipped on master as squash commit `587e7c1`. It owns a bounded
  64-entry nonce-anchored tree snapshot, exact Windows FILETIME target
  identity, same-handle identity-check/termination for an openable
  exact-matching target, a bounded wait attempt after successful termination,
  a recycle-aware deny-only post-kill launch barrier, exact attended-reset
  evidence, and a request-bound attended archive. It does not
  implement action-scoped
  child-creation closure, a checked continuation owner, or attempt-keyed
  acquire/reconcile/release. It also does not implement a POSIX exact-token
  kill adapter: its sole executor acts on Windows FILETIME and skips new
  `owned_process_tree` targets without that field.
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
  after #115, the adapter over merged #120, and that successor.
  Task #116 remains independent because an absent wrapper needs no teardown
  target.
- Existing guarded `Stop-Tree` is the sole kill site, preserving #107.
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
successor out of 87-A. The adapter over merged #120 and the closure-successor
mechanism are prerequisites, not additional 87-A conformance files.

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
evidence; later revisions, including Revision 10, do not recompute or replace
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

If a platform cannot prove an exact target executor or synchronous
action-scoped closure inside that boundary, `ClosureCapabilityV1` is
`CAPABILITY_UNAVAILABLE`. Merged #120 alone does not satisfy that conjunction,
so the static pre-reservation refusal remains mandatory: create no childless
effect envelope or reservation, consume no attempt, make no external call,
perform no closure-dependent named teardown, and keep the dependent recovery
`POLICY_HELD` with `CAPABILITY_UNAVAILABLE` pending a human. Structural
unavailability is never an ordinary closure veto, retry, or exhaustion.

Revision 10 replaces Revision 9's path-enumerated whole-state byte-identity
claim. That claim is withdrawn: an ordinary observation may legitimately
advance poll identity, reset the ordinary capture ordinal, clear a prior-poll
terminal, and update continuity or confirmation state while an executor is
unavailable. The narrower universal is structural and stronger: without a
fresh non-serializable executor witness that matches the persisted inert
binding, no executor-dependent external effect and no authority-enabling or
effect-owned mutation is constructible. Deserializing a reservation, phase,
debt, retired ID, or future childless state yields evidence only; it cannot
manufacture the permit, executable call, receipt, or checked delta needed to
act.

In that universal, authority-enabling mutation is the module's closed term for
a change to childless reservation/execution/attempt/closure/debt/cycle/
continuation/retired-attempt/nonordinary-capture/spawn/guarded-identity
ownership or for clearing such a fence. Pure ordinary-observation evidence is
not an authority object: it may change only through
`ClassifierObservationDeltaV1` and cannot reserve, consume, or execute the
predicate it later helps satisfy.

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
this exact rank, and sets `dominant = reasons[0]`:

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

| State | Exact meaning |
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
   `active_child_rows`. Exact duplicate normalized rows collapse. Conflicting
   rows with the same positive PID, or two rows that make parentage cyclic,
   make `active_child_availability=INCOMPLETE` and return
   `UNKNOWN(OBSERVATION_INCONSISTENT)`. Active-child capture
   `UNAVAILABLE/INCOMPLETE` returns `UNKNOWN(SNAPSHOT_UNAVAILABLE or
   OBSERVATION_INCONSISTENT)`; it never returns absence. Presence-only candidate
   inconsistency cannot change this projection.
4. When a row with the launcher PID exists, its start guard must match the
   runtime launcher guard by exact token or the existing ISO representation
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
   exact same-turn `ChildEstablishmentGuardV1` is `OPEN`, and emits `ABSENT`
   only after that guard is `CLOSED`. The `OTHER` defensive branch catches any
   representation not matched above.

All matching and ambiguity predicates are evaluated so `UNKNOWN.reason_codes`
contains every applicable code in the displayed order. The constructor never
uses wrapper-presence relevance, ownership, or targetability. These subreasons
are bounded operator diagnostics; all normalize to the single semantic runtime
reason `CURRENT_UNKNOWN_ACTIVE_CHILD` and do not independently alter the
banked `RecoveryConditionFingerprintV1` payload.

The runtime mapping is exact:

- `UNKNOWN` contributes `CURRENT_UNKNOWN_ACTIVE_CHILD`;
- `LIVE_GUARDED` feeds the existing progress/stall predicates;
- `ABSENT` feeds same-wrapper/turn child-death confirmation—the first
  qualifying poll contributes `CURRENT_UNKNOWN_ACTIVE_CHILD`, while the second
  consecutive qualifying poll contributes `CURRENT_TEARDOWN_PROOF`; and
- `NOT_EVALUATED` and `NOT_APPLICABLE` contribute no active-child reason.

The presence and active-child projections may consume the same immutable raw
capture but may not consume each other's result. Process-row order, exact
duplicates, and candidates unrelated to the runtime launcher lineage and brain
matcher cannot change `ActiveChildObservationV1`; relevant lineage evidence
may change it. This is the permitted snapshot-to-runtime flow used by
`_wrapped_liveness` (`src/agenttalk/supervisor.py:3379-3484`).

### Classifier continuity state

**ENFORCED by one pure observation reducer, durably enforceable after task
#115:**

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

When embedded in a canonical hash payload, the closed variant is exactly the
`CanonicalJsonV1` object:

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

NonChildlessRecoveryExecutionV1 =
  RESERVED {
    reservation: RecoveryReservationV1
    phase: PRE_BARRIER | SPAWN_IN_FLIGHT
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
generation has not been observed. Only the exact launch and
matching-generation readiness transitions below may change these fields.

`recovery_poll_terminal_sequence` is null or at most
`ordinary_poll_sequence`. Every finalized childless reservation/attempt
outcome and every permit-bound debt reconciliation from ordinary residual
evidence writes the current
`ordinary_poll_sequence` there in the same checked transaction. Pure refusal,
retained closure uncertainty, prior-poll exhaustion, and no-op
`NOT_ATTEMPTED` results that leave childless debt/cycle/execution unchanged do
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
`IDLE`, a `RESERVED` value whose authority case is exactly
`PROVABLY_CHILDLESS_OWNED_WRAPPER`, or the matching `AMBIGUOUS_LAUNCH` value.
Its required `childless_evidence` satisfies the module's mode/nullability rules
and its `authority_id` equals `evidence_id`. For automatic origin
`authority_id` also equals that module ID; for manual origin it remains the
distinct manual authority ID. No childless execution, debt, cycle,
continuation, or retired-attempt value exists outside that one envelope.

Within the envelope, `childless_attempt_id` and
`childless_attempt_revision` are either both null or both non-null.
`PRE_BARRIER` requires that pair, `childless_closure_id`, and
`childless_pending_disposition` null, plus null spawned guard and deadline.
`TREE_CLOSURE_ACQUIRING` requires the attempt pair non-null, the closure ID and
pending disposition null, and null spawned guard/deadline.
`TREE_CLOSURE_HELD` requires the attempt pair and closure ID non-null, pending
disposition null, and a previously valid joined module closure value with the
exact same acquisition/closure IDs. `TEARDOWN_IN_FLIGHT` has the same ID/null
shape. `TREE_CLOSURE_RELEASING` requires the attempt pair, closure ID, and
pending disposition non-null. All four phases require null spawned
guard/deadline.

The envelope's `continuation_owner` is non-`NONE` while a typed external call is
armed or its receipt is being applied, including after the original transient
effect-guard holder dies. A live continuation must own that guard; a detached
persisted owner is an inert tombstone, not an executable capability. Its typed
subject closes the phase pairing:

An exact module-defined `TAKEOVER_CHECKPOINT` is the sole intermediate pairing.
It requires `role=RECONCILER`, `takeover_origin=FROM` with the exact immediate
predecessor continuation ID/operation/stage, the current operation equal to
that predecessor operation, and the no-call takeover mapping to match byte-for-
byte. No external adapter accepts it. A later operation-specific
`STATE_MUTATION` permit must replace it with the module table's exact next arm
before any call, and that arm/`CALL_RETURNED` checkpoint retains the origin.
`role=ISSUER` instead requires `takeover_origin=NONE`; every noncheckpoint
`RECONCILER` pairing requires the retained origin. The phase-specific pairings
below apply otherwise.

- `ACTIVE_ATTEMPT` exactly equals the reservation/attempt/provider tuple and is
  the only subject for closure acquire/reconcile/release, `STOP_TREE`,
  and `POST_ACTION_CAPTURE`.
- `TREE_CLOSURE_ACQUIRING` admits issuer `CLOSURE_ACQUIRE` or table-authorized
  reconciler `CLOSURE_RECONCILE`, each only at `ARMED`/`CALL_RETURNED`.
  `TREE_CLOSURE_HELD` admits the exact acquire/reconcile `CALL_RETURNED`
  checkpoint and its permit-bound next arm. A live issuer may arm `STOP_TREE`
  or release; a takeover reconciler may reconcile and, after matching `HELD`,
  may arm release but never teardown.
- `TREE_CLOSURE_RELEASING` admits the exact acquire/reconcile/post-capture
  `CALL_RETURNED` checkpoint, table-authorized reconciler
  `CLOSURE_RECONCILE/ARMED`, or `CLOSURE_RELEASE` at
  `ARMED`/`CALL_RETURNED`; no other continuation is valid.
- `TEARDOWN_IN_FLIGHT` admits issuer `STOP_TREE` at
  `ARMED`/`CALL_RETURNED`. Exact `STOP_TREE/CALL_RETURNED` may be replaced by
  `POST_ACTION_CAPTURE/ARMED`; its receipt moves to releasing. After takeover
  of that returned call, only reconciler `CLOSURE_RECONCILE` may be
  `ARMED`/`CALL_RETURNED`; matching `HELD` may next arm
  `POST_ACTION_CAPTURE`. Matching `RELEASED` instead finalizes conservatively as
  `EFFECT_UNPROVEN` without another external capture. Those capture arms and
  returned checkpoints require
  the exact permit/receipt lineage and cannot arise from `STOP_TREE/ARMED`.
- `RETIRED_ATTEMPT` exactly equals one retained tombstone. It is permitted while
  envelope execution is `IDLE` only for `RETIRED_ATTEMPT_RECONCILE`, its
  `CALL_RETURNED` checkpoint, or exact `CLOSURE_RELEASE` of that tombstone's
  `RELEASE_PENDING` closure.
- `SPAWN_RESERVATION` exactly equals the childless reservation and is required
  for `SPAWN/ARMED` while execution is `SPAWN_IN_FLIGHT`. A typed spawn receipt
  or the closed positive-dead-issuer transition clears/replaces it before
  `AMBIGUOUS_LAUNCH`; that phase requires continuation `NONE`.

`PRE_BARRIER` requires continuation `NONE`. Childless `IDLE` requires `NONE`
except for the exact retired-attempt cleanup above, and every non-childless
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
non-veto `TREE_CLOSURE_RELEASING`, and their pair exactly equals execution. A
cycle `last_outcome=ISSUED` exists if and only if execution is an automatic
named childless phase in `{TREE_CLOSURE_ACQUIRING, TREE_CLOSURE_HELD,
TREE_CLOSURE_RELEASING, TEARDOWN_IN_FLIGHT}`; its owner and last attempt pair
exactly equal execution. An existing `EXHAUSTED` cycle has exactly three issued
attempts and a typed failure outcome. An `ACTIVE` cycle with a typed failure
has only one or two issued attempts; `ACTIVE/ISSUED` may have one, two, or
three. Beginning the next automatic attempt permits only `NONE -> attempt 1`
or same-owner `ACTIVE/failure(n) -> ACTIVE/ISSUED(n+1)`. Every other envelope
shape is invalid and `POLICY_HELD`.

The envelope's `retired_attempts` receives a
`BoundRetiredChildlessAttemptV1` in the same checked transaction that finally
releases its reservation. No external adapter accepts a raw retired ID.
Its attempt revision and provider version are retained with its binding and
typed cleanup state. Reconcile requires a `STATE_MUTATION` permit that installs
an exact `RETIRED_ATTEMPT/RETIRED_ATTEMPT_RECONCILE/ARMED` continuation, a
fresh post-CAS `EXTERNAL_CALL` permit and typed call, then a fresh matching
  `RECEIPT_MUTATION` permit. `NEVER_ACQUIRED`/`RELEASED` restores terminal state
  and clears the continuation; unexpected `HELD` persists
  `RELEASE_PENDING(closure_id)` with the reconcile continuation at
  `CALL_RETURNED`. A later `CLOSURE_RELEASE/STATE_MUTATION` permit replaces that
  checkpoint with exact typed `CLOSURE_RELEASE/ARMED`; its release follows the
  same three-stage sequence. A crash leaves the typed
continuation and cleanup state inert for guarded takeover/reconciliation.
Without each fresh permit, the envelope remains exactly retained and no call
occurs; ordinary observation bookkeeping outside the envelope may still
advance. Eviction follows checked commit-revision order and is permitted only
by the same permit-bound finalization transaction that inserts a new terminal
attempt, when the evicted tombstone is `TERMINAL` and owns no continuation.
There is no background or maintenance eviction; otherwise the set is full and
named recovery holds.

Every normal or reload result first requires the returned acquisition ID to
equal the persisted attempt ID. In `TREE_CLOSURE_ACQUIRING`, the first
well-formed `HELD`/`RELEASED` may bind its non-null closure ID in the same
checked transition; reload binding is release-only. After that binding, every
`HELD`/`RELEASED` must exactly equal the persisted pair. Null, mismatch,
conflict, or unreadable reconciliation is `UNKNOWN`, retains the phase,
reservation, debt, cycle, and current-attempt fields, and holds every action.

`SPAWN_IN_FLIGHT` requires a non-null deadline and a null spawned guard. In a
childless envelope it also requires null childless attempt
ID/revision/closure ID/pending disposition. No transition may persist a
returned guard in this phase. A returned guard either commits identity or
moves into `AMBIGUOUS_LAUNCH`, whose nested `reservation.spawned_guard`
exactly equals `evidence.observed_guard` while its deadline is null.
`IDENTITY_COMMIT_FAILED` requires that shared guard non-null;
`START_RETURNED_WITHOUT_GUARD` and `CRASHED_DURING_SPAWN` require both copies
null. Any other combination holds. A childless `SPAWN_IN_FLIGHT` or
`AMBIGUOUS_LAUNCH` remains inside the same envelope and retains its exact inert
executor binding. Manual origin never increments or failure-updates the
automatic cycle; an origin-neutral successful debt-clear may clear it. The
module validates envelope execution, debt, cycle, continuation, and retired
attempts as one checked value; an invalid pairing selects no action and
resolves `POLICY_HELD`.
Thus automatic and manual launches share the same durable reservation,
in-flight, and ambiguity fence. Manual readiness bookkeeping is orthogonal to
that ownership fence and cannot authorize or block a later recovery.

The checked owner accepts this closed delta algebra, not arbitrary field maps:

```text
ClassifierObservationDeltaV1 {
  expected_state_epoch: lowercase hyphenated UUID
  expected_revision: uint64
  ordinary_poll_sequence: exactly current + 1
  next_capture_ordinal: exactly 1
  recovery_poll_terminal_sequence: null
  runtime_continuity: RuntimeContinuityStateV1
  child_establishment_guard: ChildEstablishmentGuardV1
  child_dead_confirmation: ConsecutiveEvidenceV1
  child_stall_confirmation: ConsecutiveEvidenceV1
  owned_childless_confirmation: module OwnedChildlessConfirmationV1
  absence_confirmation: AbsenceConfirmationStateV1
}

NonChildlessAuthorityTransitionV1 =
  RESERVE | PRE_BARRIER_RELEASE | SPAWN_ARM | NO_SPAWN_COMMIT
  | AMBIGUITY_COMMIT | AMBIGUITY_RESOLVE | IDENTITY_COMMIT
  | OWNER_TRANSITION | READINESS_COMMIT

NonChildlessAuthorityDeltaV1 =
  private nonserializable reducer result {
  expected_state_epoch: lowercase hyphenated UUID
  expected_revision: uint64
  precondition: current recovery_execution is IDLE or NON_CHILDLESS;
                current CHILDLESS is rejected before field application
  precondition: state_loss_quarantine is NONE and execution eligibility is ELIGIBLE
  transition: NonChildlessAuthorityTransitionV1
  allowed_updates: closed field map limited to
    absence_confirmation, consumed_manual_request_ids, recovery_execution,
    recovery_poll_terminal_sequence, manual_readiness, managed_generation,
    first_managed_epoch, launch_grace_until, launching, readiness_seen,
    runtime_continuity, child_establishment_guard, child_dead_confirmation,
    child_stall_confirmation, and owned_childless_confirmation
  invariant: the transition's private constructor supplies the exact allowed
             field subset and values from the normative transition table;
             callers cannot construct a map or choose arbitrary values
  invariant: next recovery_execution is IDLE or NON_CHILDLESS
  invariant: state_loss_quarantine is unchanged
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
    invariant: operation exactly equals the enclosing permit operation
    invariant: the operation/event-table constructor supplies the exact allowed
               field subset and values; callers cannot construct a map or
               choose arbitrary values
    invariant: state_epoch, agent_key, ordinary_poll_sequence, and
               state_loss_quarantine are unchanged
  }

StateLossQuarantineCreationDeltaV1 {
  precondition: dry_run == false
  expected_outer_state: MISSING | CORRUPT | TORN | ROLLBACK_UNPROVEN
  new_state_epoch: lowercase hyphenated UUID
  quarantine_id: lowercase hyphenated UUID distinct from new_state_epoch
  decision_now_epoch: finite nonnegative Unix seconds
  replacement_state: complete ClassifierStateV1 with exactly:
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

ClassifierStateDeltaV1 =
  OBSERVATION(ClassifierObservationDeltaV1)
  | NON_CHILDLESS_AUTHORITY(NonChildlessAuthorityDeltaV1)
  | CHILDLESS_EFFECT(module PermitBoundChildlessMutationV1)
  | STATE_LOSS_QUARANTINE(StateLossQuarantineCreationDeltaV1)
```

`ClassifierObservationDeltaV1` is a field-level pure result. Its module overlay
is reduced from the same ordinary raw capture and committed beside but never
feeds or rewrites either banked child counter. An ordinary observation may
advance exactly the displayed fields while a childless executor is unavailable.
It cannot alter `ChildlessEffectEnvelopeV1`, consumed manual IDs,
`manual_readiness`, launch/backoff/readiness, marker/configuration state,
allocate a nonordinary effect capture, or write an effect terminal.

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
exact `ChildlessOuterStateDeltaV1` projection named by the module's closed
transition.
`StateLossQuarantineCreationDeltaV1` is the exclusive fail-closed state-loss
constructor; it cannot compose with an observation, authority, or effect delta.

The checked owner commits exactly one tagged delta per transaction. A non-dry-
run poll first commits `OBSERVATION`, reloads the successor revision, and only
then may privately construct a non-childless or permit-bound childless delta.
It may never infer a permit from the prior observation or upgrade that
already-committed observation into an effect mutation. Authority and policy functions
remain mutation-free. `decision_now_epoch` is the poll's one captured finite
nonnegative UTC Unix-seconds value. For the first three delta variants, the task
#115 owner compares `(state_epoch, revision)`, commits with `revision + 1`, and
makes a stale writer reload/re-reduce or fail closed. State-loss creation instead
compares the exact unavailable outer-state condition and atomically installs the
complete displayed replacement at revision zero; no field from the unavailable
state participates. The owner accepts only the displayed typed delta union,
exposes no raw whole-state write, and never lets a cached whole-state save roll
back a newer field.
The reducer returns `(observations, delta, expected_revision)`. A recovery plan
binds to the committed successor revision and cannot execute if that exact
delta did not commit; it must reload/re-reduce rather than combine an old
runtime/freshness result with newer absence or manual state.

The module's ordinary residual observation is captured input only. Clearing
envelope debt/cycle after `COMPLETE_GONE` is a
`PermitBoundChildlessMutationV1`, never a `ClassifierObservationDeltaV1`, and
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
`generation_launch_grace_until_epoch_ms` is the exact normalized
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

The active-record grace is therefore inclusive at exactly 30 seconds, while
the generation launch fence is exclusive at its exact deadline, matching the
shipped predicates. The closed variant retains both exact opening anchors and
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
counter and the module overlay. At exactly 30 seconds it remains open. If a
longer generation launch fence applies, two complete absences after 30 seconds
but before that fence also remain open. The first capture at or after the
exclusive launch-fence deadline is the first `ABSENT` sample only when the
inclusive active-age grace has also ended. No pre-close capture carries into a
closed confirmation. The complete closed guard object is copied into the
module confirmation basis, reservation, and action-time equality; a key,
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
`CanonicalJsonV1` of exactly:

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
capture has `capture_ordinal=0`. Replay of `last_capture_id` is unchanged. A
distinct capture with the same basis advances only when its committed ordinary
poll sequence is exactly one greater; a gap or changed basis restarts at count
1. Nonqualifying evidence resets to `(0, null, null)`. Count 2 is the only
confirmed value. Cached capture replay and stale re-reduction therefore cannot
manufacture teardown proof.

### One operand convention

**ENFORCED by every authority equation in 87-A:** Authority and escalation use
`runtime.dominant` only. `runtime.reasons` affects diagnostics and
`RecoveryConditionFingerprintV1`, never teardown, replacement, escalation, or
policy gates.

This exact counterexample is normative:

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

- Exact current version `1` enters the full closed-record validator. Unknown or
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
`INVALID_OR_FUTURE_SKEW` with its exact reason. No invalid input can become
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

Age exactly equal to the threshold is fresh; grace expires exactly at its
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
has only `OBSERVATION_INCONSISTENT`. A global raw snapshot acquisition failure
is copied into both availability projections. A presence-only candidate parse,
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

Before classification, the observer groups candidate rows by PID. Exact
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
codes in this exact order:

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

Each canonical target is exactly
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
exact-deduplicated candidate sequence (not the first-eight diagnostic
truncation) after the per-field bounds defined under
`RecoveryConditionFingerprintV1`, each canonical object encoded as a four-byte
big-endian length plus bytes.

### Complete owned-wrapper tree

**ENFORCED by the
[normative module](DESIGN-87A-owned-childless-wrapper-authority.md):** The
87-A adapter maps a merged task #120 Windows `owned_process_tree_v2` snapshot
into at most 64 exact PID/start/nonce-owned targets, requires exact FILETIME for
every live row, and rejects every incomplete or incompatible snapshot. A valid
Linux exact token remains observation/barrier input; no macOS exact-token
mapping is declared. No non-Windows named path constructs a destructive
owner/target tuple or authority; with quarantine `NONE`, each fresh path returns
`CAPABILITY_UNAVAILABLE(EXACT_TARGET_EXECUTOR_UNAVAILABLE)` before a fresh
reservation. The adapter may persist only an inert
`ExactTargetExecutorBindingV1` inside `ChildlessEffectEnvelopeV1`. A
reservation/closure/spawn phase, debt-only state, ambiguous launch, or retired
attempt inherited onto a host that cannot serve its exact binding is not
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
an unavailable exact executor or successor produces `CAPABILITY_UNAVAILABLE`;
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
NFC; object keys sort by their NFC UTF-8 bytes; arrays preserve specified
order; integers use shortest base-10 with no leading zero or plus sign; and
`true`, `false`, and `null` are lowercase. There is no whitespace.

String encoding is exact: printable ASCII is literal except `"` and `\`, which
use `\"` and `\\`; `/` is never escaped; backspace, tab, LF, form feed, and CR
use `\b`, `\t`, `\n`, `\f`, and `\r`; other controls use lowercase
`\u00xx`; and every non-ASCII code point uses lowercase four-hex-digit UTF-16
`\u` escapes, with a high/low surrogate pair for a non-BMP code point. Floats,
negative zero, and NaN/Infinity are forbidden. The resulting ASCII text is
encoded as UTF-8. No caller may substitute a private serializer.

### Capture identity and coverage equality

**ENFORCED by the checked state owner required from task #115:**

```text
CaptureIdV1 {
  state_epoch: lowercase hyphenated UUID string
  agent_key: NFC canonical agent/root string
  ordinary_poll_sequence: uint64
  capture_ordinal: uint16
}
```

The canonical state owner increments `ordinary_poll_sequence` once for each
committed ordinary observation of that agent. The planner's ordinary capture
has `capture_ordinal=0`. Reusing a cached capture preserves its ID and cannot
advance confirmation. Post-teardown and final-barrier captures have nonzero
ordinals and never count as ordinary absence polls.

The checked owner resets `next_capture_ordinal=1` in the same transaction that
increments `ordinary_poll_sequence`. Before every nonordinary closure,
post-action, reload-reconciliation, or final-barrier capture, it atomically
reserves the current value as that capture's ordinal and increments the stored
value. The capture must use that exact reserved `CaptureIdV1`; a caller may not
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
Equality is exact equality of `CanonicalJsonV1` bytes. A version or capability
change is unequal even when both captures happen to be empty.

### Typed one-use confirmation

**ENFORCED by this persisted closed state and pure reducer:**

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
`CanonicalJsonV1` bytes of exactly:

```text
{
  "launcher_pid": <integer 1..4294967295>,
  "launcher_start_guard": <NFC UTF-8 string of at most 128 bytes>
}
```

It is null only when no guarded managed launcher identity exists. The
recognition-config digest is SHA-256 over
`agenttalk.supervisor.wrapper-recognition-config.v1\0` plus
`CanonicalJsonV1` bytes of exactly:

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
of exactly:

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

The binding tuple is exactly `(agent_key, state_epoch, managed_generation,
guarded_launcher_identity_digest, recognition_config_digest, coverage)`.
“Compatible” means exact equality of that tuple.

**ENFORCED transitions, evaluated top to bottom:**

| Prior state and input | Next state |
| --- | --- |
| Any state + changed agent key, state epoch, managed generation, guarded launcher identity, recognition config, or coverage | Discard the prior state, then reduce the current input from `EMPTY`: a qualifying ordinary `ABSENT` becomes `OBSERVED_ONCE(input)`; every other input becomes `EMPTY`. |
| `CONFIRMED` + atomic launch reservation | `CONSUMED(confirmation, reservation_id)`; retain the complete binding for later comparisons. |
| Any state other than `CONFIRMED` + launch-reservation attempt | Refuse the reservation and leave state unchanged. |
| `CONSUMED` + successful launch whose new guarded managed identity is not yet atomically committed | Remain `CONSUMED`; success alone cannot revive the proof. |
| `CONSUMED` + atomic commit of the new guarded managed identity | The binding-change rule above yields `EMPTY`. |
| Any unconsumed state + `PRESENT_TARGETABLE`, `PRESENT_UNTARGETABLE`, `UNKNOWN`, unavailable/incomplete observation, or any other nonqualifying ordinary observation | `EMPTY`. |
| `CONSUMED` + a nonqualifying observation under the same binding | Remain `CONSUMED`. |
| `EMPTY` + qualifying ordinary `ABSENT` | `OBSERVED_ONCE(input)` |
| `OBSERVED_ONCE` + replay of the same capture ID | Unchanged; never confirmed. |
| `OBSERVED_ONCE` + a qualifying `ABSENT` from the next committed ordinary poll, distinct capture ID, and exact-equal binding/coverage | `CONFIRMED(first, second)` |
| `OBSERVED_ONCE` + any other qualifying ordinary `ABSENT` | New `OBSERVED_ONCE(input)`; changed compatibility or a sequence gap cannot complete the old sample. |
| `CONFIRMED` + replay of its first, second, or latest-compatible capture ID, or an older capture ID | Unchanged; replay cannot extend freshness. |
| `CONFIRMED` + the next consecutive compatible ordinary `ABSENT` after `latest_compatible_capture_id` | Retain `confirmation_id`; advance `latest_compatible_capture_id`. |
| `CONFIRMED` + any other qualifying ordinary `ABSENT` | New `OBSERVED_ONCE(input)`; changed compatibility or a sequence gap starts a new proof. |
| `CONSUMED` + any qualifying ordinary `ABSENT` under the same binding | New `OBSERVED_ONCE(input)`; it is the first of two new polls. |
| Any state + an input not covered above | Fail closed to `EMPTY`, except `CONSUMED` remains `CONSUMED`. |

“Consecutive” means adjacent committed `ordinary_poll_sequence` values for the
same `agent_key` and `state_epoch`. Heartbeat, runtime-reason, and restart-marker
changes do not reset physical evidence. A qualifying capture has
`capture_ordinal=0`, complete coverage, presence `ABSENT`, and the reducer's
agent key and state epoch. Dry run and recovery-policy `HOLD` do not request a
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

**ENFORCED capture sources:** `dry_run` is the immutable invocation flag.
`kill_switch` is `CLEAR` only when the supervisor kill-switch path is
positively absent; presence, file-kind ambiguity, or path/I/O failure is
`ACTIVE_OR_UNREADABLE`. `supervisor_instance` is `CURRENT` only when the
executor is in its in-memory `RUNNING` phase and the freshly read instance
record exactly matches its claim token plus guarded PID/start; shutdown,
release, mismatch, or read/validation failure is `STOPPED_OR_UNREADABLE`.

The executor owns an atomic `action_latch`. It becomes enabled with a fresh
monotonic `action_epoch` only after instance claim and action-subsystem
initialization. It is set disabled before shutdown/claim release and after a
fatal executor-state failure. The action issuer takes its shared read guard;
shutdown/disabling takes the exclusive write guard. `report_membership` comes
from one freshly validated report image and is `UNREADABLE` on read/schema
failure. `auto_restart` comes from the same configuration-lock image used for
manual authorization and is enabled only for exact Boolean `true`; a
read/schema failure is disabled.

`snapshot_id` hashes
`agenttalk.supervisor.execution-gate-snapshot.v1\0` plus `CanonicalJsonV1` of
exactly:

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
`AUTO_RESTART_DISABLED` unless exact Boolean true; otherwise `ELIGIBLE`. Only
`ELIGIBLE` may reserve/consume authority, mutate a restart marker, teardown,
launch, seed managed identity, or update launch/backoff/readiness state.

`DRY_RUN` may compute and display a pure simulated decision but discards every
`ClassifierObservationDeltaV1`; it performs no state/event/marker/config
persistence. For every other value, the checked owner may first commit the
observation-only fields of `ClassifierObservationDeltaV1`, including the
nonrenewable freshness anchor, continuity, and confirmation resets. That
observation commit cannot reserve or consume an absence/manual proof or alter
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
linearly borrow that exact live handle and construct one operation-scoped non-serializable
`ExactTargetExecutorPermitV1`, then a
`PermitBoundChildlessMutationV1` or `ChildlessExternalEffectCallV1`; an adapter
revalidates the borrow at entry and returns it only inside a matching
`ChildlessExternalEffectReceiptV1`. A receipt-mutation permit consumes that
returned borrow and revalidates it before applying the result. Releasing,
losing, transferring, or replacing the handle invalidates the corresponding
permit/call/receipt before effect. No state-only
pre-barrier release, takeover, debt-only effect finalization, retired-attempt
cleanup, nonordinary capture allocation, finalization, `Stop-Tree`, or launch
accepts raw persisted state or IDs. Static witness unavailability makes all of
those objects unconstructible and emits `CAPABILITY_UNAVAILABLE`/`POLICY_HELD`;
a rejected stale/replayed/mismatched private operand performs zero effect and
follows the module's reload/reject rule instead.

The ordinary observation delta remains independent of this boundary and may
advance only its closed projection before the hold is returned. Cleanup may
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
`PRESENT_VALID` with request ID and `revision_sha256` exactly equal to the
reservation, and authorization must still be `AVAILABLE` with
`snapshot_id == reservation.authorization_snapshot_id`. Deletion, replacement,
unreadability, or any semantically different authorization snapshot is a
veto—even when the new requester would independently be authorized.

The executor reruns the origin-applicable policy gates and performs no
intervening wait between the final check and OS action issuance. It retains the
configuration lock and action-latch read guard through issuance, then releases
them before any process-completion wait. A mismatch or noneligible result
aborts the next action and records a typed veto. A non-childless action releases
its reservation through the private `NonChildlessAuthorityDeltaV1` transition.
A named childless pre-closure veto instead requires the exact
`PRE_BARRIER_RELEASE/STATE_MUTATION` permit to remove its effect envelope,
release its reservation, write its same-poll terminal, and consume no automatic
attempt. Static executor unavailability retains the complete envelope; a stale
revision reloads/re-reduces, and any other rejected private operand performs no
mutation. Both branches leave any still-matching manual marker pending and any
one-use absence proof consumed. After a childless closure is held but before
`Stop-Tree`, the executor
instead commits `TREE_CLOSURE_RELEASING/CLOSURE_VETOED`, requests release using
the exact persisted attempt/closure pair, and may release the reservation only
after exact `RELEASED` reconciliation. `HELD` or `UNKNOWN` retains the
reservation and continuous attention. If teardown already completed and
closure/debt were safely cleared, the fresh pre-spawn fence still refuses
spawn; later recovery must rebuild ordinary absence proof.

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

`COMPLETE` already guarantees nonempty exact targets and positive start guards;
the automatic equation must not reconstruct that invariant.

### Provably-childless owned-wrapper authority

**ENFORCED module import:** The
[owned-childless module](DESIGN-87A-owned-childless-wrapper-authority.md)
constructs `PROVABLY_CHILDLESS_OWNED_WRAPPER` only from two complete
same-owner, same-basis child-absence captures whose nonrenewable
child-establishment guard is `CLOSED`, complete nonce-anchored tree
observation, and the current targetability proof, or from an outstanding
debt's exact immutable residual subset. It also owns the authority-facing
tree-closure contract, origin-neutral teardown debt, childless-only
three-attempt automatic cycle, and exact result/attention constructors.

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
`UNREADABLE`, never `ABSENT`. An accepted object carries SHA-256 of its exact
bytes as `revision_sha256`; reservation and compare-clear match both request ID
and revision.

Implementation has one private `capture_manual_marker_locked` primitive whose
precondition is “configuration lock held” and one public wrapper that acquires
the lock. Write, reservation, and compare-clear call the private primitive;
they never recursively acquire the non-reentrant configuration lock.

**ENFORCED exact marker schema:** Unknown or missing keys are invalid.

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

`AgentName` matches `\A[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\z`; marker `agent`
equals the configured target. `RequestId` matches `\Arr-[0-9a-f]{12}\z`,
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

Both timestamps must be exact JSON integers (not Boolean), addition must not
overflow `uint64`, and both displayed relations must hold. Overflow, a
mismatched expiry, or issue time beyond allowed future skew is
`PRESENT_INVALID(TIMESTAMP)` and therefore `INVALID_HELD`; it never reaches the
gate list as a valid marker. A structurally valid marker with
`decision_now_epoch_ms >= expires_at_epoch_ms` is `EXPIRED_HELD`.

The marker expires at the exact deadline. Five minutes is five times the
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
- `protected` is true exactly when the target is `operator_facing` or any
  roster member whose role case-folds to `lead`. Multiple leads therefore all
  remain protected even though `sole_lead` is null.

These values come from the same configuration image as `auto_restart` and
cannot come from a cached report Boolean. `snapshot_id` hashes the domain
`agenttalk.supervisor.manual-authorization-snapshot.v1\0` plus
`CanonicalJsonV1` of exactly:

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
wraps the same initial-tree or debt-residual proof and exact action-time
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

Its `CanonicalJsonV1` form is exactly
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
`agenttalk.supervisor.manual-authority.v1\0` plus `CanonicalJsonV1` of exactly:

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
exactly:

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

Automatic `PROVABLY_CHILDLESS_OWNED_WRAPPER` uses the module's separately
domained proof `authority_id` as both selected authority ID and evidence ID.
Manual-wins instead stores the manual authority ID as selected authority and
the exact module proof ID as evidence. Either origin also copies the complete
module `ChildlessReservationEvidenceV1` into the reservation; hashes are not
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

Reservation has the exact precondition
`recovery_poll_terminal_sequence != ordinary_poll_sequence` and either
top-level `recovery_execution == IDLE` or
`recovery_execution == CHILDLESS(envelope)` with envelope execution `IDLE`
and every `RESERVE/CONTINUE` predicate from the module true.
`RESERVED/PRE_BARRIER`, `RESERVED/TREE_CLOSURE_HELD`,
`RESERVED/TREE_CLOSURE_ACQUIRING`,
`RESERVED/TREE_CLOSURE_RELEASING`, `RESERVED/TEARDOWN_IN_FLIGHT`,
`RESERVED/SPAWN_IN_FLIGHT`, and
`AMBIGUOUS_LAUNCH` reject every new automatic or manual reservation without
mutation, even after a CAS loser reloads and re-reduces. `manual_readiness` is
orthogonal bookkeeping: `NONE` and `APPLIED_PENDING_READINESS` both permit a
new reservation only under that same exact precondition; replacing that
bookkeeping during a later launch cannot make its consumed request ID reusable.

| Transition | Exact delta |
| --- | --- |
| Refused/held | Retain marker/revision. Do not reserve, consume, kill, launch, reset readiness, or mutate automatic backoff. |
| Irrecoverable checked-state loss | Dry run returns only a simulated `STATE_PROVENANCE_LOST`/`POLICY_HELD` result. Otherwise apply only `StateLossQuarantineCreationDeltaV1`: create a new epoch with `StateLossQuarantineV1.UNRESOLVED`, select `STATE_PROVENANCE_LOST`, emit mandatory attention, and deny every kill, launch, closure, authority-enabling/effect-owned mutation, marker consumption, identity commit, and grace recovery. Construct no childless envelope, permit, usable debt/cycle, or attempt budget. V1 has no automatic retirement constructor; a valid backup or local extinction is not restoration proof. |
| Childless capability or exact executor unavailable | A missing `ClosureCapabilityV1` or permit construction result `CAPABILITY_UNAVAILABLE` cannot construct `ChildlessEffectEnvelopeV1` for a fresh reservation, `ExactTargetExecutorPermitV1` for persisted state, any permit-bound mutation, executable target/call, or receipt-consuming transition. Preserve an existing envelope exactly, allow only the separate ordinary observation delta, emit continuous `CAPABILITY_UNAVAILABLE`, and remain `POLICY_HELD`. This never becomes `CLOSURE_VETOED`, retry, or exhaustion. |
| Childless permit rejected | A stale checked revision reloads/re-reduces. A copied, replayed, consumed, wrong-use, mismatched, or invalid-scope private operand is rejected with zero mutation/call and never becomes operator-facing `CAPABILITY_UNAVAILABLE`. Malformed checked state follows the invalid-fence row instead. |
| Reserve selected authority | A non-childless selection from top-level `IDLE` records `NON_CHILDLESS/RESERVED/PRE_BARRIER`. A childless selection requires `ClosureCapabilityV1.AVAILABLE` and consumes its one-shot `RESERVE` permit. `INITIAL` creates the complete envelope/binding. `CONTINUE` starts from childless `IDLE`: initial-mode retry may atomically rebind only the fresh target tree for the exact same owner while preserving cycle and terminal historical tombstones; a physically different owner must first use `OWNER_TRANSITION` and a later `INITIAL`. Debt completion must retain the immutable envelope/debt binding and exact residual subset. Both enter `RESERVED/PRE_BARRIER` with null spawn guard/deadline/attempt/revision/closure/pending disposition; neither is allowed with a continuation or `RELEASE_PENDING` tombstone. The live witness and permit are not persisted. Consume the selected proof according to its mode; retain the separate module confirmation for live equality. Do not add a manual request ID to the consumed set. |
| Begin childless closure acquisition | Acquire the exclusive effect guard, live-recompute the reservation/binding, and consume a `CLOSURE_ACQUIRE/STATE_MUTATION` permit to commit `TREE_CLOSURE_ACQUIRING` plus the exact `ACTIVE_ATTEMPT/CLOSURE_ACQUIRE/ARMED` continuation. At the successor revision construct a distinct fresh `CLOSURE_ACQUIRE/EXTERNAL_CALL` permit, then invoke the typed closure call while retaining the guard. Apply its receipt only through a third fresh `RECEIPT_MUTATION` permit. Automatic origin creates/increments `ACTIVE/ISSUED`; manual origin leaves the cycle unchanged. Preserve existing debt. |
| Childless closure transiently absent/blocked | Only after the current-host executor gate passes, under the same guard, a conforming transient refusal plus terminal matching `NEVER_ACQUIRED`, or matching `RELEASED` while still acquiring, retires the attempt and finalizes `CLOSURE_VETOED`. A reload-held closure, live joined-evidence mismatch, or late execution/manual/policy veto commits `TREE_CLOSURE_RELEASING/CLOSURE_VETOED`; exact release/reconcile calls each use the effect guard and finalize only after matching `RELEASED`. `HELD`, `UNKNOWN`, a live foreign continuation, or current-host executor unavailability retains every fence. A post-reservation structural-unavailability claim from the successor remains malformed; the independently reconstructed current-host unavailable fact uses the prior row. No kill or launch occurs. |
| Childless closure held | Only a matching permit plus the fresh raw-capture/live-basis/target-equality join may construct the mutation to `TREE_CLOSURE_HELD`, bind its closure ID, and apply the matching acquire/reconcile receipt at `CALL_RETURNED`. A later operation-specific `STATE_MUTATION` permit replaces that checkpoint atomically with `STOP_TREE/ARMED` or `CLOSURE_RELEASE/ARMED`; the receipt permit cannot arm the next call. Preserve existing debt and cycle count. |
| Childless teardown action-ready | Under the effect guard, consume a `STOP_TREE/STATE_MUTATION` permit to enter `TEARDOWN_IN_FLIGHT`, create/update origin-neutral debt, and arm the exact continuation. At the successor revision, only a distinct fresh `STOP_TREE/EXTERNAL_CALL` permit can construct `ExecutableOwnedTargetSetV1` and invoke the typed call. A third fresh receipt-mutation permit applies its receipt to enter `CALL_RETURNED`; a stale/nonowner caller, consumed permit, or raw persisted value cannot invoke/apply the adapter. |
| Childless post-action observation | A `POST_ACTION_CAPTURE/STATE_MUTATION` permit and exact `STOP_TREE/CALL_RETURNED` fact reserve the next nonordinary ordinal and arm the typed capture. A distinct fresh post-CAS call permit obtains the observation receipt; a third fresh receipt-mutation permit maps it and enters `TREE_CLOSURE_RELEASING` with `POST_ACTION_CAPTURE/CALL_RETURNED` while retaining debt/current attempt and automatic `ISSUED`. Only a later `CLOSURE_RELEASE/STATE_MUTATION` permit arms typed release, which then repeats the three-stage pattern. |
| Childless exact-release finalization | A matching permit, effect guard, and exact `RELEASED` receipt are required to apply the module event table, clear current-attempt fields, record failure/exhaustion, retire the attempt, release the reservation, clear the continuation, or clear debt/cycle. For reload/takeover retained `STOP_TREE/CALL_RETURNED`, a matching `CLOSURE_RECONCILE` receipt that proves `RELEASED` finalizes conservatively as `EFFECT_UNPROVEN`, enters childless `IDLE`, retains debt, and makes no residual-capture call; the next ordinary poll may clear debt only through the module's debt-only finalize scope. Live `COMPLETE_GONE` after a typed post-action capture may normalize within the same envelope to `PRE_BARRIER`; other reload cleanup enters envelope execution `IDLE` without launch. Every finalized branch writes the same-poll terminal through a permit-bound mutation. |
| Non-childless teardown or final-barrier veto after no closure remains | Release a non-childless reservation through its private `PRE_BARRIER_RELEASE` delta. Retain any marker and leave launch/readiness/backoff fields unchanged. A reserved no-kill absence proof remains consumed. |
| Childless final-barrier veto at `PRE_BARRIER` | Consume a fresh matching `PRE_BARRIER_RELEASE/STATE_MUTATION` permit to release/remove the childless envelope and write the terminal. Static executor unavailability retains the complete envelope and remains `POLICY_HELD`; a stale/replayed/mismatched constructor is `REJECTED` and reloads/re-reduces with the envelope unchanged. No generic/direct release path exists. |
| Barrier passed, immediately before spawn | When #120 owned-tree state or post-kill provenance applies, first require its fresh deny-only launch barrier to be unblocked and unambiguous. A blocked/ambiguous result retains the hold and cannot clear debt. For childless origin, consume `SPAWN/STATE_MUTATION` to update the exact outer projection, enter envelope `SPAWN_IN_FLIGHT`, and install `SPAWN_RESERVATION/SPAWN/ARMED`; at the successor revision a distinct fresh `SPAWN/EXTERNAL_CALL` permit is the only constructor for `Start-Process`. Non-childless origin uses its private typed delta/call. The childless envelope and inert binding remain present through the call and any ambiguity. |
| Proven no-spawn failure | Only an OS/API result that positively proves no child was created may set `launching=false`, release reservation, retain any marker and attempt/backoff bookkeeping, preserve prior guarded identity, clear the pending deadline, and record the typed failure result. For childless origin, the matching typed receipt and a fresh `SPAWN_RESULT_COMMIT/RECEIPT_MUTATION` permit are mandatory. Timeout, exception, lost return, or any uncertain post-issuance effect enters `AMBIGUOUS_LAUNCH` instead. |
| Spawn returned but guarded identity is ambiguous | For childless origin, consume a fresh `SPAWN_RESULT_COMMIT/RECEIPT_MUTATION` permit bound to the matching typed spawn receipt and persist `AMBIGUOUS_LAUNCH` with continuation `NONE`, the complete envelope reservation, null pending deadline, and `ambiguity_boundary_poll_sequence=ordinary_poll_sequence`; reset `absence_confirmation` to `EMPTY`. For `IDENTITY_COMMIT_FAILED`, copy the returned non-null `SpawnGuardV1` identically into `reservation.spawned_guard` and `evidence.observed_guard`; for `START_RETURNED_WITHOUT_GUARD`, keep both null. The receipt-free crash conversion instead requires the module's persisted SPAWN issuer subject plus positive dead-issuer scope. Non-childless origin uses its private typed transition. Do not release authority ownership or permit another launch. |
| New guarded identity commits | In one checked transaction replace the managed identity, reset the establishment guard, and update launch/readiness state. `GuardedLaunchCommitV1` is inert checkpoint input only. For childless spawn origin, only the matching typed spawn receipt + checkpoint + fresh `SPAWN_IDENTITY_COMMIT/RECEIPT_MUTATION` permit may construct the mutation and remove the envelope, after debt is `NONE`, no closure remains, and all retired-attempt obligations are terminal; the spawn continuation is consumed by that same commit. A physically different guarded owner observed outside that spawn may clear an old-owner `IDLE` envelope/cycle only through the module's state-only `OWNER_TRANSITION` permit with the same no-debt/no-obligation predicates. Non-childless origin returns directly to top-level `IDLE`. Manual spawn origin also records its consumed request and pending readiness. |
| Readiness observed | Only guarded readiness whose managed generation exactly equals `committed_managed_generation` sets `readiness_seen=true` and `launching=false`, and it alone satisfies a pending manual-readiness value. Compare-clear that marker using request ID plus revision and set `manual_readiness=NONE`; a replaced marker is untouched. Readiness for any other generation cannot change launch state, clear the marker, or satisfy the request. |

The consumed set retains the latest 128 IDs in checked commit-revision order
and evicts the oldest; the five-minute TTL prevents an evicted ancient marker
from regaining authority. Every failure leaves the marker pending. A consumed
no-kill absence proof must be rebuilt from two ordinary polls. Consumed-set
mutation and guarded-identity commit are both task #115-dependent.

**ENFORCED crash/reload and ambiguity rules:**

Every crash, reload, resume, takeover, CAS re-reduction, and future childless
entry variant within one supported V1 store activation can deserialize only
`ChildlessEffectEnvelopeV1` evidence. None can deserialize or manufacture
`CurrentExactTargetExecutorWitnessV1`,
`ExactTargetExecutorPermitV1`, `PermitBoundChildlessMutationV1`, an executable
call, or a receipt. A fresh witness must match the complete inert binding and
pass static preflight; an operation that requires the effect guard acquires it
before constructing its revision-bound permit. Only then is any release,
takeover, nonordinary capture, reconciliation, finalization, `Stop-Tree`, or
launch object constructible. This is a construction rule, not an inventory of
entry paths.

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

Without a `PERMITTED` construction, the envelope is retained exactly and no
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
CAS external-call, and fresh receipt-mutation permits; no bullet authorizes a
raw call or reuse across revisions.

- Reload of `RESERVED/PRE_BARRIER` uses only
  `MAY_RELEASE_PRE_BARRIER` to release that reservation; no attempt pair,
  continuation owner, effect guard, or spawn exists. Any consumed absence proof
  stays consumed. For the named childless case it records `BARRIER_VETOED`,
  consumes no automatic attempt, and writes the same-poll terminal.
- Reload of `RESERVED/TREE_CLOSURE_ACQUIRING` invokes only the closure successor's
  attempt-keyed `OwnedTreeClosureReconciliationV1`. Matching
  `NEVER_ACQUIRED` finalizes `CLOSURE_VETOED`; matching `RELEASED` binds its
  returned closure ID and finalizes the same veto; matching `HELD` is persisted
  as `TREE_CLOSURE_RELEASING/CLOSURE_VETOED` and released without termination.
  `UNKNOWN` retains the reservation and automatic `ISSUED`.
- Reload of `RESERVED/TREE_CLOSURE_HELD` never terminates. After any required
  no-call takeover checkpoint, a distinct `CLOSURE_RECONCILE` arm reconciles
  the exact persisted pair. It persists
  `TREE_CLOSURE_RELEASING/CLOSURE_VETOED`; matching `RELEASED` may then
  finalize directly, while matching `HELD` requires a later distinct exact
  release arm and a later matching `RELEASED`. `UNKNOWN` retains the held state.
- Reload of `RESERVED/TREE_CLOSURE_RELEASING` likewise reconciles the exact
  persisted pair before any release arm. `HELD` or `UNKNOWN` preserves the
  reservation, pending disposition, debt/current attempt, and automatic
  `ISSUED`. Only matching `RELEASED` applies the module's pending-disposition
  finalizer; matching `HELD` requires a later distinct release arm.
- Reload of childless `RESERVED/TEARDOWN_IN_FLIGHT` never reissues
  `Stop-Tree`. Matching `HELD` takes a fresh typed post-action observation
  under that closure, persists its releasing disposition, and follows exact
  release. Matching `RELEASED` consumes the exact reconcile receipt only
  through a fresh `EFFECT_FINALIZE` permit, conservatively records
  `EFFECT_UNPROVEN`, enters childless `IDLE`, retains debt, clears its current
  attempt, records the origin-sensitive failure/exhaustion result, and makes no
  residual-capture call or launch. The next ordinary poll may clear that debt
  only through the existing debt-only finalize scope and matching
  `OwnedDebtResidualObservationV1.COMPLETE_GONE`.
  `UNKNOWN` preserves the reservation, debt/current attempt, and automatic
  `ISSUED`; `NEVER_ACQUIRED` after debt is invalid and preserves the fence.
- Before invoking `Start-Process`, the checked state must already say
  `SPAWN_IN_FLIGHT` with the exact typed `SPAWN/ARMED` issuer continuation. For
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
  exactly match `SpawnGuardV1` is adopted through the common guarded-launch
  commit below. Childless adoption requires a fresh matching
  `SPAWN_IDENTITY_COMMIT/STATE_MUTATION` permit over the exact envelope and
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
origin only a matching typed spawn receipt plus fresh
`SPAWN_IDENTITY_COMMIT/RECEIPT_MUTATION` permit may apply it through
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
to the exact matching fingerprint, name the held agent from
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

The payload has this exact schema:

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

`canonical_payload_bytes` are the `CanonicalJsonV1` encoding of that exact
payload. Presence coverage is null if and only if
`ProcessObservationV1.coverage` is null.

**ENFORCED candidate normalization:**

1. Convert each authority-normalized relevant candidate, and no known-foreign
   diagnostic evidence, into exactly this closed object:

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
3. Canonically serialize each object, exact-deduplicate those bytes, and sort
   bytewise before truncation.
4. Define `total_count` as the number after exact deduplication. Persist the
   first eight objects. `CandidateSummaryV1` also contains `total_count`,
   `omitted_count = max(total_count - 8, 0)`, and `omitted_sha256`.
5. When candidates are omitted, compute `omitted_sha256` over the domain
   `agenttalk.supervisor.recovery-condition.candidates.v1\0` followed by each
   omitted canonical object as a four-byte big-endian length plus bytes.
   Store the digest as 64 lowercase hexadecimal characters. Otherwise it is
   null.

`CandidateSummaryV1` serializes exactly as:

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
payload is exactly these 433 ASCII bytes:

```text
{"agent_key":"agent\u00e9/root","candidates":{"items":[],"omitted_count":0,"omitted_sha256":null,"total_count":0},"freshness":"FRESH","presence":{"coverage":null,"reasons":["SNAPSHOT_UNAVAILABLE","COVERAGE_INCOMPLETE","RECORDED_IDENTITY_UNKNOWN"],"state":"UNKNOWN"},"recovery_blocked":false,"runtime":{"dominant":"CURRENT_UNKNOWN_OTHER","reasons":["CURRENT_UNKNOWN_OTHER"]},"schema":"recovery-condition/v1","stale_uncertainty":false}
```

The resulting `RecoveryConditionFingerprintV1` suffix is
`e5dd312b003e55327548f1dae152a28de60bb99b61d3aa3aeae8ea263f729f94`.
Every implementation must reproduce both the bytes and digest.

A candidate-bearing vector uses agent key `a/root`, one unowned PID 42
`python.exe` wrapper with the shown guarded start and ownership failure,
complete initial V1 coverage, `PRESENT_UNTARGETABLE`, fresh
`CURRENT_TEARDOWN_PROOF`, `recovery_blocked=true`, and this exact 875-byte
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
  canonical_condition: the exact fingerprint payload object above
  escalation_required: bool
  condition_codes: ordered tuple of length 0..2[
    "RECOVERY_BLOCKED" | "STALE_UNCERTAINTY"
  ]
  active_child_reason_codes: ActiveChild UNKNOWN reason tuple | null
  operator_candidates: OperatorDiagnosticCandidateSummaryV1
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
  action_attention_required: bool
  action_attention_codes: ordered tuple of length 0..6[
    "CHILDLESS_STATE_PROVENANCE_LOST"
    | "CAPABILITY_UNAVAILABLE"
    | "CHILDLESS_OWNER_CHILD_TREE_OR_CLOSURE_INCOMPLETE"
    | "CHILDLESS_TEARDOWN_DEBT"
    | "AUTOMATIC_CHILDLESS_RETRY_ACTIVE"
    | "AUTOMATIC_CHILDLESS_RETRY_EXHAUSTED"
  ]
  manual_marker_disposition: ManualMarkerDispositionV1 | null
}
```

`condition_codes` includes each true predicate in the displayed order.
`canonical_condition` must reproduce the fingerprint bytes exactly; 87-B may
persist it but cannot mutate and rehash it. The action record binds later
policy/execution resolution without pretending that result supplied authority.
The normative module gives the result/code precedence.
`action_attention_required == (action_attention_codes is nonempty)`. Neither
field enters the banked condition fingerprint.
For `CAPABILITY_UNAVAILABLE`, 87-B joins this resolution to the exact matching
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

The command fragment contains no raw argument values. It is exactly
`"<exe> <shape> --for=<MATCH|MISMATCH|UNKNOWN>
--root=<MATCH|MISMATCH|MISSING|UNKNOWN>"`, where `<exe>` and `<shape>` are the
already-normalized basename and `WRAP|WAIT|UNKNOWN`. It is capped at 256 UTF-8
bytes at a code-point boundary. Rows are canonicalized, exact-deduplicated,
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

`omitted_count = total_count - len(items)` and the digest is null exactly when
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
action-time tree closure, exact reserved-target digest check, checked
debt/attempt commit, sole typed `Stop-Tree` adapter, and complete-gone proof.
Step 8 first consumes `SPAWN/STATE_MUTATION` to arm the persisted issuer and
then requires a distinct fresh post-CAS `SPAWN/EXTERNAL_CALL` permit; the
generic launch function cannot accept childless raw reservation state. The
path rejoins step 4 only after origin-neutral debt is cleared. Module debt
forces every unrelated launch proof to `NONE` while permitting only its
debt-bound residual cleanup, so neither manual nor automatic absence can
bypass a partial kill.

#120's barrier is not the closure successor. At merged `587e7c1`, an openable
Windows owned-tree target whose exact creation FILETIME matches is verified and
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
an exact equal/newer child of a replacement PID is excluded from that
retired-parent ownership edge, while missing/incomparable exact evidence
remains conservative. The split does not suppress independent barrier evidence:
the replacement-side process still blocks if, for example, its command line
parses as this agent's wrapper or wait process. The barrier
never adds a kill target, proves `COMPLETE_GONE`, clears debt, or substitutes
for the successor's pre-effect creation closure. Attended reset is an
exact-identity-bound human escape, and the request-bound archive is retention;
neither is automatic closure evidence. For the named childless path, the module
consumes the first barrier result in its typed post-action observation; step 6 performs
the fresh final recheck immediately before spawn. No closure-dependent named
teardown previously held by `CAPABILITY_UNAVAILABLE` becomes executable solely
because #120 merged.

That effect claim is grounded at its execution site:
`src/agenttalk/supervisor.py:8900-8928` enters the exact destructive branch,
while `8930-8932` skips an `owned_process_tree` target without
`start_filetime`. The separate Linux-token acceptance paths are observation
input, not a kill adapter. Accordingly a valid Linux-token snapshot receives
pre-reservation
`CAPABILITY_UNAVAILABLE(EXACT_TARGET_EXECUTOR_UNAVAILABLE)` and no destructive
tuple, authority, reservation, attempt, debt, or `Stop-Tree` call. The legacy
rounded-start path and any weakening of the Windows FILETIME requirement remain
forbidden.

**OPERATOR-VISIBLE CAPABILITY CONSEQUENCES:** This revision deliberately leaves
three permanent V1 capability limitations. The first two remain indefinitely
held pending a human; the third refuses activation before imported state becomes
active:

1. On Linux and macOS with state-loss quarantine `NONE`, every fresh
   closure-dependent named teardown returns
   `CAPABILITY_UNAVAILABLE(EXACT_TARGET_EXECUTOR_UNAVAILABLE)` before
   reservation or action. A childless envelope inherited from Windows remains
   inert because the host cannot construct a matching permit. If Windows
   already returned from `Stop-Tree`, the non-Windows host neither repeats the
   call nor clears debt. Ordinary observation may advance, but recovery remains
   `POLICY_HELD` until a reviewed exact-token adapter exists under M5 Option A.
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

The following are explicitly insufficient as a process-universe proof: PID and
start, hostname, `state_epoch`, `process_source_digest`, MachineGuid alone,
local absence. A future successor may consume a read-only producer over an
existing OS token only; it may add no file, registry value, helper, daemon, OS
object, persistence plane, or runtime dependency. Accepting an identity token
as parser or snapshot input does not make it executable or authoritative.
87-B projections, 87-C activation surfaces, and the required operator manual
and tutorial must state all three permanent limitations and the attended action
required. Held-agent projections apply to items 1 and 2; item 3 identifies the
rejected operation/store rather than fabricating an active agent.

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

1. Generate the 96 dominant-projection cells and assert the exact distributions,
   formula parity, and zero missing/duplicate/extra cells.
2. Generate canonical overlapping reason sets. Secondary-reason permutation
   cannot change authority, action, or escalation; adding/removing a secondary
   reason must change `RecoveryConditionFingerprintV1`.
3. Execute Lens C's exact
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
   An exact duplicate owned/guarded row must collapse to one target and remain
   `PRESENT_TARGETABLE`; a conflicting same-PID duplicate must be `UNKNOWN`
   with incomplete targetability.
6. Assert `TargetabilityProofV1.COMPLETE` has an exact candidate-to-target
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
    Candidate permutation and exact duplicates must be invariant; changing the
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
19. Cover first-managed and real-launch grace just before and exactly at
    expiry; heartbeat exactly at and just over threshold; missing,
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
    complete child absence, unrelated rootless wrapper, row shuffle/exact
    duplicate, and relevant lineage change. Assert the independent presence
    and active-child projections and their permitted runtime effects.
23. Inject `snapshot unavailable + no prior process state` at the final
    barrier and require veto. Prove 87-C activation refuses the strict barrier
    when matching-generation 87-B incident projections are not active.
24. Reconstruct `RecoveryConditionV1` and
    `RecoveryActionResolutionV1`; assert the canonical condition exactly
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
    Every mismatch vetoes the next OS action and preserves the exact
    origin-specific state delta.
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
    exactly active age 30 seconds and after 30 seconds but before a longer
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
    fence. After positive predecessor-death proof, only exact idempotent
    reconciliation may proceed; `ARMED` teardown never proves completion or
    permits reissue, and `CALL_RETURNED` alone permits post-action capture.
    From retained `STOP_TREE/CALL_RETURNED`, make reconciliation return matching
    `RELEASED`; require receipt-bound finalization as `EFFECT_UNPROVEN`, retained
    debt, childless `IDLE`, and zero residual-capture calls or launch.
    Exercise the state-only `PRE_BARRIER` release and every exact no-call
    takeover mapping with its distinct permit before only the closed table's
    exact next reconcile, release, retired-cleanup, or crash-result operation.
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
    copies are exactly equal and match the ambiguity code's nullability.
    Reload of a valid-guard standalone `SPAWN_IN_FLIGHT` is invalid and holds.
35. Cross manual/automatic origin with confirmed whole-wrapper absence and
    child-death-sourced residue. With debt `NONE`, both select no-kill
    `RELAUNCH_ONLY`; outstanding debt suppresses both. Independently construct,
    production-encode, byte-compare, and hash the module's chained seven-domain
    vector as required by its conformance item 20; treat its byte-flip chain as
    change detection rather than independent codec correctness. Race
    nonordinary capture-ordinal allocation as required by its conformance
    section.
36. Integrate the module's exact merged-#120 adapter and post-kill barrier
    control. Prove an openable exact-FILETIME planned Windows target uses one
    native handle for FILETIME verification and termination, and that every
    successful termination receives a wait attempt within the remaining shared
    tree-wide budget. Inject open failure, exact-identity mismatch, termination
    failure, wait timeout, and depleted budget; each must defer completion to
    the fresh snapshot and barrier. Race a
    recorded parent that creates a descendant after planning and exits during
    `Stop-Tree`; require the unplanned descendant to miss the target set,
    survive, and be detected only by the fresh deny-only barrier before
    `SPAWN_IN_FLIGHT`. Recycle that parent PID: an exact equal/newer replacement
    child must be excluded from the retired-parent ownership edge, while a
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
    and `UNKNOWN(CAPABILITY_UNAVAILABLE)` reconciliation; retain every exact
    fence and forbid `CLOSURE_VETOED`, retry, exhaustion, kill, and launch.
    Export all applicable module attention codes in their exact order,
    including `CHILDLESS_STATE_PROVENANCE_LOST` and
    `CAPABILITY_UNAVAILABLE`, and require `action_attention_required=true`.
    Join the exact fingerprint to `canonical_condition.agent_key`; every 87-B
    rendering must name that agent and say operator action is required.
38. Integrate the module's construction-seal suite. Prove the checked owner has
    exactly four delta variants and no raw whole-state or childless-envelope
    write. Exercise every public decoder/reducer and every childless external
    adapter with raw IDs, target tuples, bindings, persisted envelopes, forged
    receipts, copied/stale permits, wrong-operation permits, mismatched
    revisions, and lost effect guards; none may construct or apply a childless
    effect delta or external call. Round-trip persisted state and prove no
    witness, permit, executable target, typed call, receipt, or live guard
    survives serialization.

    Reject any composite/multi-tag delta. In a non-dry-run poll, commit only
    `OBSERVATION`, reload its successor revision, and then construct at most one
    later authority/effect delta against that revision. Prove every childless
    outer-field update is present in the exact private
    `ChildlessOuterStateDeltaV1` projection for its permit operation, and reject
    omitted, extra, or caller-selected fields/values.

    Apply `NonChildlessAuthorityDeltaV1` to every current `CHILDLESS` envelope
    shape, including `IDLE`, `SPAWN_IN_FLIGHT`, and `AMBIGUOUS_LAUNCH`; the
    checked owner must reject it before field application. Composition with an
    observation delta must not change that result. Only a matching
    `CHILDLESS_EFFECT` delta may replace or remove the envelope.

    Construct `StateLossQuarantineCreationDeltaV1` from every outer-state loss
    and byte-compare the complete replacement state against the displayed
    quarantined genesis; reject a partial/default-from-lost-state object.

    For every `ExactTargetExecutorOperationV1` and permit use, construct the
    object only from a fresh action-site witness plus exact inert binding,
    current revision, and closed operation scope. Normally require the
    authorized tuple or residual subset; for the closed targetless old-side
    rebind, retired-cleanup, and owner-transition scopes require the exact
    historical tombstone/envelope binding plus the complete prospective proof
    or typed subject/checkpoint instead. Consume it
    once; replay and cross-operation use must fail. Feed an unknown future
    childless execution variant through the compatibility boundary and require
    closed rejection or inert evidence, never an action object. Use fresh
    selection, an inherited external-effect envelope, childless
    `SPAWN_IN_FLIGHT`, `AMBIGUOUS_LAUNCH`, and a retired tombstone as direction
    controls on Linux and macOS: the effect envelope remains exact, no adapter
    runs, and only the separately typed observation projection may advance.
    For every guard-required operation, prove permit construction linearly
    borrows the exact live handle. Release, lose, transfer, or replace it before
    each of state-mutation consumption, adapter entry, and receipt commit; each
    case must reject with zero mutation/call. A successful synchronous adapter
    must return the same borrow in its typed receipt, and the receipt-mutation
    permit must consume and revalidate that returned borrow rather than acquire
    an unrelated second borrow.
    Separately prove state-loss quarantine creation is the sole permit-free
    fail-closed mutation in a non-dry-run invocation. Repeat every outer-state
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
| Missing heartbeat becomes stale after finite grace | ENFORCED after task #115 | Nonrenewable `first_managed_epoch`, guarded launch deadline, and exact freshness formula. |
| Child confirmation cannot cross uncertainty or replay | ENFORCED after task #115 | Basis-bound consecutive counters with durable capture ID and adjacent poll sequence in the sole classifier delta. |
| Total mixed-candidate presence | ENFORCED | Ordered aggregation and closed reason codes. |
| No partial-target kill | ENFORCED | `TargetabilityProofV1.COMPLETE` bijection invariant. |
| Absent, invalid, and unreadable manual paths differ | ENFORCED | Locked bounded raw capture and closed codec; only true path absence is `ABSENT`. |
| One origin for simultaneous manual/automatic authority | ENFORCED | Total manual gates, live revalidation, candidate-scoped acknowledgement, and manual-priority selector. |
| Reserved manual authority cannot drift before action | ENFORCED after task #115 | Pre-kill/pre-spawn marker revision plus authorization snapshot equality under the fixed lock order. |
| Consumed marker cannot replay | ENFORCED after task #115 | Revision-bound reservation, bounded committed ID set, and compare-clear. |
| Ambiguous spawn cannot release authority or duplicate-launch | ENFORCED after task #115 | Durable `AMBIGUOUS_LAUNCH` tombstone, guarded reconciliation, and no second reservation. |
| Physical proof independent from timing | ENFORCED | Separate closed values and reducers. |
| Two independent compatible absence polls | ENFORCED after task #115 | Durable capture IDs, exact coverage equality, and adjacent poll sequence. |
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
| Operator documentation of permanent capability limitations | REQUIRED before implementation close/activation | 87-B/follow-up manual and tutorial evidence states all three limitations together: the two permanent `POLICY_HELD` availability costs and the declared transfer/restore/rollback/migration activation refusal, including the out-of-band-copy residual and attended action. |
| Same-platform state-file/workspace transfer, restore, rollback, and migration activation | UNAVAILABLE IN V1; DECLARED ACTIVATION REFUSES | A conforming activation path refuses before imported bytes become the active checked store and constructs no 87-A witness, mutation, effect, or launch. An out-of-band replacement may be undetectable, is nonconforming, and has no 87-A guarantee. Future 87-C must bind the source universe within M5 Option A or keep imported state inert. |
| Raw discovery stops flapping | STATED not promised | Process-discovery behavior is unchanged. |
| Owned-childless teardown requires a nonce-owned complete tree, an exact target executor, and action-time closure | WINDOWS #120 INPUT/EFFECT DELIVERED; TEARDOWN CURRENTLY UNAVAILABLE until #115 and a conforming closure successor; POSIX executor unavailable independently | Merged #120 supplies exact Windows target/effect evidence but no POSIX exact-token executor or action-scoped creation closure. A fresh proof cannot create an envelope without a `RESERVE` permit; deserialized childless evidence cannot construct a fresh operation permit, typed call, receipt, or effect-owned mutation. The envelope remains exact while ordinary observation may advance its separate projection. Unresolved named paths remain `CAPABILITY_UNAVAILABLE` and `POLICY_HELD` pending a human. |
| Child-establishment grace cannot be sampled away | ENFORCED after #115, the merged-#120 adapter, and the closure successor | Nonrenewable same-turn closed guard in observation, confirmation, reservation, and action equality. |
| External childless calls cannot outlive their authority owner | PARTIAL WINDOWS #120 TARGET-LOCAL PRIMITIVE DELIVERED; full contract ENFORCED after #115 and the closure successor | For an openable exact-FILETIME Windows target, merged #120 binds identity-check and termination to one handle and issues a same-handle bounded wait attempt after successful termination; POSIX exact-token execution and the exclusive effect guard, checked continuation owner/stage, stable attempt tombstones, and attempt-bound synchronous adapters remain absent. |
| POSIX named owned-childless teardown and inherited cleanup | CURRENTLY UNAVAILABLE | #120 accepts Linux exact observation tokens but declares no macOS mapping, and its sole executor skips the Linux-token target because it has no FILETIME. No POSIX action-site witness exists, so neither fresh authority nor a deserialized `PRE_BARRIER`, external-effect phase, debt-only state, spawn ambiguity, or retired tombstone can construct the permit and typed object required to act. The Windows FILETIME guard is unchanged. |
| Partial owned-childless teardown cannot be laundered into launch | ENFORCED after #115, the merged-#120 adapter, and the closure successor | Origin-neutral durable teardown debt and debt-bound completion authority. |
| Automatic owned-childless retry stops at three without fading from attention | ENFORCED after tasks #78/#115 | Durable childless-only cycle, hard cap, and independent action-attention output. |
| State loss cannot reset a cap or erase teardown debt | ENFORCED after task #115, with automatic V1 retirement unavailable | Fail-closed quarantine has no automatic retirement constructor on any platform. It remains `STATE_PROVENANCE_LOST`/`POLICY_HELD` pending attended handling; local different-owner or extinction evidence and a structurally valid backup cannot clear it. |

This core and its same-commit normative module together are sufficient to
implement and review 87-A's pure classifier and authority substrate. Neither
file alone is conforming. They are not permission to activate the behavior and
make no delivery promise or supported migration promise beyond the explicit V1
activation refusal above.
