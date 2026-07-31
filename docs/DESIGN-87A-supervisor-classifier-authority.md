# Design 87-A: Supervisor classifier and recovery-authority totality

**Status:** Proposed, Revision 7 with the operator-directed owned-childless
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
- Task #120 candidate `28f663fce694fd72f311bda7590ced53abfab528`
  owns a bounded 64-entry nonce-anchored tree snapshot and a deny-only
  post-kill launch barrier. As of 2026-07-31 it is **UNMERGED**, has no
  completed review, exists only on
  `origin/codex/task120-owned-process-tree-final`, and may move under open PR
  102 review. It does not implement action-scoped child-creation closure or
  attempt-keyed acquire/reconcile/release.
- The **closure successor**—a separately reviewed extension to #120 or a
  successor task—owns that missing closure and the synchronous adapters needed
  to linearize its external effects with #115 checked state. No reviewed
  successor exists as of 2026-07-31. Until one exists, a static
  pre-reservation `ClosureCapabilityV1` returns `CAPABILITY_UNAVAILABLE`
  without an attempt or external call, and the dependent recovery remains
  `POLICY_HELD` pending a human. Task #78 consumes the
  constructor/cap only after #115, merged/reviewed #120, and that successor.
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
successor out of 87-A. A reviewed #120 mapping and closure-successor mechanism
are prerequisites, not additional 87-A conformance files.

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
evidence; Revision 7 does not recompute or replace them.

**STATED non-goals:** 87-A does not specify notification routes, human receipt,
incident retention, state-extension compatibility, executor capability
activation, migration, rollback, wrapper-writer generations, or a rollout
runbook. Those concerns must not be reintroduced here.

**DECIDED dependency-plane constraint (operator, 2026-07-31; M5 Option A):**
Implementation adds no daemon, new persistence plane, durable helper or
durable OS object, or runtime dependency; `pyproject.toml:13` remains
`dependencies = []`. It may add only pure code and fields to the existing
checked supervisor state after task #115 plus transient caller-owned
synchronization that leaves no durable helper or OS object. Neither task #120
nor the closure successor relaxes this absolute promise. There is no
mechanism-specific or separately versioned exception.

If a platform cannot prove synchronous action-scoped closure inside that
boundary, `ClosureCapabilityV1` is `CAPABILITY_UNAVAILABLE`. The successor does
not exist as of 2026-07-31, so that static pre-reservation refusal remains
mandatory: create no reservation, consume no attempt, make no external call,
perform no closure-dependent named teardown, and keep the dependent recovery
`POLICY_HELD` with `CAPABILITY_UNAVAILABLE` pending a human. Structural
unavailability is never an ordinary closure veto, retry, or exhaustion.
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
RecoveryExecutionStateV1 =
  IDLE
  | RESERVED {
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
      childless_evidence:
        module ChildlessReservationEvidenceV1 | null
      phase:
        PRE_BARRIER | TREE_CLOSURE_ACQUIRING | TREE_CLOSURE_HELD
        | TREE_CLOSURE_RELEASING | TEARDOWN_IN_FLIGHT | SPAWN_IN_FLIGHT
      childless_attempt_id: lowercase hyphenated UUID | null
      childless_attempt_revision: uint64 | null
      childless_closure_id: lowercase hyphenated UUID | null
      childless_pending_disposition:
        CLOSURE_VETOED | COMPLETE_GONE | SAME_OWNER_SURVIVED
        | MEMBER_SURVIVED | EFFECT_UNPROVEN | null
      prior_guarded_identity_digest: Hex64 | null
      spawned_guard: SpawnGuardV1 | null
      pending_attempt_deadline_epoch: finite nonnegative Unix seconds | null
    }
  | AMBIGUOUS_LAUNCH {
      reservation: the complete RESERVED value normalized with
                   phase = SPAWN_IN_FLIGHT and
                   childless_attempt_id = null and
                   childless_attempt_revision = null and
                   childless_closure_id = null and
                   childless_pending_disposition = null and
                   pending_attempt_deadline_epoch = null
      ambiguity_boundary_poll_sequence: uint64
      evidence: AmbiguousLaunchEvidenceV1
    }

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
  teardown_debt: module TeardownDebtV1
  automatic_childless_cycle: module AutomaticChildlessCycleV1
  childless_continuation_owner: module ChildlessContinuationOwnerV1
  retired_childless_attempt_ids: bounded ordered set of 128 UUIDs
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
outcome and every observation-only debt reconciliation writes the current
`ordinary_poll_sequence` there in the same checked transaction. Pure refusal,
retained closure uncertainty, prior-poll exhaustion, and no-op
`NOT_ATTEMPTED` results that leave childless debt/cycle/execution unchanged do
not. Successful observation-only or reload cleanup also exports
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
`childless_evidence` is non-null if and only if
`authority_case=PROVABLY_CHILDLESS_OWNED_WRAPPER`; it must satisfy the module's
mode/nullability rules and its `authority_id` must equal `evidence_id`.
For automatic origin `authority_id` also equals that module ID; for manual
origin it remains the distinct manual authority ID.
`childless_attempt_id` and `childless_attempt_revision` are either both null
or both non-null. `PRE_BARRIER` requires that pair,
`childless_closure_id`, and `childless_pending_disposition` null, plus null
spawned guard and deadline.
`TREE_CLOSURE_ACQUIRING` is valid only for the module's named case and requires
the attempt pair non-null, the closure ID and pending disposition null, and
null spawned guard/deadline. `TREE_CLOSURE_HELD` requires the attempt pair and
closure ID non-null, pending disposition null, and a previously valid joined
module closure value with the exact same acquisition/closure IDs.
`TEARDOWN_IN_FLIGHT` has the same ID/null shape.
`TREE_CLOSURE_RELEASING` requires the attempt pair, closure ID, and pending
disposition non-null. All four phases require null spawned guard/deadline.

`childless_continuation_owner` is non-`NONE` while an external
acquire/reconcile/release or `Stop-Tree` operation is armed/returned, including
after its original transient effect-guard holder dies. A live continuation
must own that guard; a detached persisted owner is a valid tombstone, not an
invalid state, until an effect-guard-owning takeover CAS replaces or clears it.
Its attempt ID/revision exactly equals the reservation.
`TEARDOWN_IN_FLIGHT` requires operation `STOP_TREE` with stage `ARMED` or
`CALL_RETURNED`; only `CALL_RETURNED` permits a normal or reload post-action
capture under the guard. `TREE_CLOSURE_ACQUIRING` permits only the
corresponding closure acquire/reconcile operation.
`TREE_CLOSURE_HELD` requires the same acquire/reconcile owner at
`CALL_RETURNED`; the guard-owning live chain must replace it atomically with
either `STOP_TREE/ARMED` or `CLOSURE_RELEASE/ARMED`. A releasing phase permits
only reconcile/release. `PRE_BARRIER` and all non-childless phases require
owner `NONE`. Every other phase/owner pairing is invalid and `POLICY_HELD`.

The attempt revision is the post-commit state revision immediately before
invoking closure-successor acquisition. Automatic origin in any childless
closure/teardown phase requires a matching `ACTIVE/ISSUED` childless cycle
with the same attempt ID/revision until verified release finalizes the outcome.
`TREE_CLOSURE_ACQUIRING` and `TREE_CLOSURE_HELD` require no debt current attempt
for this revision. `TEARDOWN_IN_FLIGHT` requires module debt with
`current_attempt_id=childless_attempt_id`,
`current_attempt_revision=childless_attempt_revision`, and
`last_outcome=ISSUED`. A releasing disposition other than
`CLOSURE_VETOED` requires that same debt/current-attempt equality;
`CLOSURE_VETOED` requires both debt current-attempt fields null.

The converses are also enforced. Debt current-attempt fields are non-null if
and only if execution is `TEARDOWN_IN_FLIGHT` or non-veto
`TREE_CLOSURE_RELEASING`, and their pair exactly equals execution. A cycle
`last_outcome=ISSUED` exists if and only if execution is an automatic named
childless phase in
`{TREE_CLOSURE_ACQUIRING, TREE_CLOSURE_HELD,
TREE_CLOSURE_RELEASING, TEARDOWN_IN_FLIGHT}`; its owner and last attempt pair
exactly equal execution. An existing `EXHAUSTED` cycle has exactly three
issued attempts and a typed failure outcome. An `ACTIVE` cycle with a typed
failure has only one or two issued attempts; `ACTIVE/ISSUED` may have one,
two, or three. Beginning the next automatic attempt permits only
`NONE -> attempt 1` or same-owner `ACTIVE/failure(n) -> ACTIVE/ISSUED(n+1)`.
Every other debt/cycle/execution shape is invalid and `POLICY_HELD`.

`retired_childless_attempt_ids` receives the attempt ID in the same checked
transaction that finally releases its reservation. No external childless
adapter may acquire, terminate, capture, or release using a retired ID except
the module's release-only handling of an unexpected late `HELD` result.
Eviction follows checked commit-revision order and is permitted only after
the closure successor proves the evicted ID terminally `NEVER_ACQUIRED` or `RELEASED`;
otherwise the set is full and named recovery holds.

Every normal or reload result first requires the returned acquisition ID to
equal the persisted attempt ID. In `TREE_CLOSURE_ACQUIRING`, the first
well-formed `HELD`/`RELEASED` may bind its non-null closure ID in the same
checked transition; reload binding is release-only. After that binding, every
`HELD`/`RELEASED` must exactly equal the persisted pair. Null, mismatch,
conflict, or unreadable reconciliation is `UNKNOWN`, retains the phase,
reservation, debt, cycle, and current-attempt fields, and holds every action.

`SPAWN_IN_FLIGHT` requires a non-null deadline and null childless attempt
ID/revision/closure ID/pending disposition, plus a null spawned guard. No
transition may persist a returned guard in this standalone phase. A returned
guard either commits identity or moves into
`AMBIGUOUS_LAUNCH`, whose nested `reservation.spawned_guard` exactly equals
`evidence.observed_guard` while its deadline is null.
`IDENTITY_COMMIT_FAILED` requires that shared guard non-null;
`START_RETURNED_WITHOUT_GUARD` and `CRASHED_DURING_SPAWN` require both copies
null. Any other combination holds. Manual origin never increments or failure-updates
the automatic cycle; an origin-neutral successful debt-clear may clear it.
The module validates recovery execution, debt, and cycle as one checked state;
an invalid pairing selects no action and resolves `POLICY_HELD`.
Thus automatic and manual launches share the same durable reservation,
in-flight, and ambiguity fence. Manual readiness bookkeeping is orthogonal to
that ownership fence and cannot authorize or block a later recovery.

`ClassifierObservationDeltaV1` is a field-level pure result limited to
freshness, runtime continuity, child confirmation, ordinary poll identity, and
absence-observation fields, plus the module-owned
`owned_childless_confirmation` overlay. That overlay is reduced from the same
ordinary raw capture and committed beside but never feeds or rewrites either
banked child counter. The delta cannot alter module debt/cycle,
`recovery_execution`, consumed manual IDs, `manual_readiness`,
launch/backoff/readiness, or
marker/configuration state. `RecoveryAuthorityDeltaV1` is a separate pure
result that may consume or invalidate absence proof for the named
reservation/action outcomes, advance execution/module-debt/module-cycle/launch
fields, and update manual-readiness bookkeeping only after execution
eligibility. Both are subtypes of `ClassifierStateDeltaV1`; the checked owner
may compose them into one transaction when allowed. Authority and policy
functions remain mutation-free. `decision_now_epoch` is the poll's one
captured finite nonnegative UTC Unix-seconds value. The task #115 owner compares
`(state_epoch, revision)`,
commits one delta with `revision + 1`, and makes a stale writer reload/re-reduce
or fail closed; a cached whole-state save may not roll back a newer field.
The reducer returns `(observations, delta, expected_revision)`. A recovery plan
binds to the committed successor revision and cannot execute if that exact
delta did not commit; it must reload/re-reduce rather than combine an old
runtime/freshness result with newer absence or manual state.

The module's ordinary residual observation is captured input only. Clearing
debt/cycle after `COMPLETE_GONE` is a `RecoveryAuthorityDeltaV1`, never a
`ClassifierObservationDeltaV1`, and may commit only under
`ExecutionEligibilityV1.ELIGIBLE` while `recovery_execution == IDLE` with no
named reservation, closure ID, pending disposition, or debt current attempt.

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
`first_managed_epoch` only together with module
`StateLossQuarantineV1.UNRESOLVED`; it never creates usable fresh recovery
authority. No later poll in that epoch may renew the freshness anchor, and no
freshness/grace result may bypass quarantine. The guarantee is observational
convergence after the last state loss, not destructive recovery during lost
provenance. Thus repeated missing heartbeat and unavailable snapshots
eventually yield:

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
87-A adapter maps task #120 candidate `owned_process_tree_v2` into at most 64
exact PID/start/nonce-owned targets and rejects every incomplete or incompatible
snapshot. The closure successor separately supplies the action-scoped
non-destructive creation closure and effect-linearized adapters. Missing or
unverifiable observation produces `OwnedWrapperTreeObservationV1.INCOMPLETE`;
an unavailable successor produces `CAPABILITY_UNAVAILABLE`; a name or pattern
never supplies ownership.

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
hold until the next ordinary poll, never wraparound. Thus concurrent callers cannot
assign the same nonzero ID, and every reload residual capture has a
deterministic identity even though it occurs mid-poll.

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

The eligibility constructor evaluates the displayed variant precedence:
`DRY_RUN` from `dry_run`; `STATE_PROVENANCE_LOST` when module
`StateLossQuarantineV1` is `UNRESOLVED`; `KILL_SWITCH_ACTIVE` unless kill switch is clear;
`SUPERVISOR_STOPPED` unless the instance is current; `ACTIONS_DISABLED` when
the action latch is disabled or report/config capture is unreadable;
`AGENT_NOT_REPORTED` for report absence; `AUTO_RESTART_DISABLED` unless exact
Boolean true; otherwise `ELIGIBLE`. Only `ELIGIBLE` may reserve/consume
authority, mutate a restart marker, teardown, launch, seed managed identity, or
update launch/backoff/readiness state.

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
grace-based recovery. Apart from the module's one checked
different-owner/extinction quarantine-retirement transaction, it permits only
pure observation and mandatory attention. That retirement performs no OS
action, cannot launch in the same poll, and is not an exact-restoration or
backup-rollback escape. Manual force/acknowledgement cannot override it.

There is one narrow module-owned cleanup exception for an already-persisted
named childless reservation/phase. Its
`ChildlessSafetyReconciliationGateV1` permits a state-only pre-barrier release,
a no-call takeover CAS, or `MAY_RECONCILE` only under the module's exact
conditions. External cleanup requires that this is not dry run, state-loss
quarantine is `NONE`, the freshly captured supervisor instance is `CURRENT`,
and the module's exclusive continuation/effect guard is held by this
invocation. It may then invoke only the closure successor's attempt-keyed
reconcile/release operations, capture the effect of an already-issued
teardown, and compare-and-swap the persisted disposition even when kill
switch, action latch, report membership, or auto-restart blocks new recovery.
It cannot acquire a new closure, reserve/increment an attempt, call
`Stop-Tree`, launch, consume a marker, or change generic backoff/readiness.
Dry run and a non-current/unreadable supervisor retain the complete fence
byte-identically and make no closure-successor call. This exception is non-destructive
fence cleanup, not recovery authority.

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
aborts the next action and records a typed veto. Before any childless closure
is held, and for non-childless actions, it releases the reservation directly,
leaves any still-matching manual marker pending, and leaves any one-use absence
proof consumed. A named childless pre-closure veto additionally writes its
same-poll terminal and consumes no automatic attempt. After a childless closure
is held but before `Stop-Tree`, it
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
`RecoveryExecutionStateV1`; the checked owner applies these transitions and no
executor branch may save a cached whole state.

Reservation has the exact precondition `recovery_execution == IDLE and
recovery_poll_terminal_sequence != ordinary_poll_sequence`.
`RESERVED/PRE_BARRIER`, `RESERVED/TREE_CLOSURE_HELD`,
`RESERVED/TREE_CLOSURE_ACQUIRING`,
`RESERVED/TREE_CLOSURE_RELEASING`, `RESERVED/TEARDOWN_IN_FLIGHT`,
`RESERVED/SPAWN_IN_FLIGHT`, and
`AMBIGUOUS_LAUNCH` reject every new automatic or manual reservation without
mutation, even after a CAS loser reloads and re-reduces. `manual_readiness` is
orthogonal bookkeeping: `NONE` and `APPLIED_PENDING_READINESS` both permit a
new reservation only when `recovery_execution == IDLE` and
`recovery_poll_terminal_sequence != ordinary_poll_sequence`; replacing that
bookkeeping during a later launch cannot make its consumed request ID reusable.

| Transition | Exact delta |
| --- | --- |
| Refused/held | Retain marker/revision. Do not reserve, consume, kill, launch, reset readiness, or mutate automatic backoff. |
| Irrecoverable checked-state loss | Create a new epoch only with `StateLossQuarantineV1.UNRESOLVED`; select `STATE_PROVENANCE_LOST`, emit mandatory attention, and deny every automatic/manual kill or launch, closure acquisition, attempt/debt mutation, marker consumption, identity commit, and grace recovery. Do not initialize usable debt/cycle values or a fresh childless attempt budget. The only exit is the module's checked complete different-owner/extinction retirement CAS, which performs no OS action or same-poll launch; a valid backup is not restoration proof. |
| Named childless closure capability unavailable | When `ClosureCapabilityV1` is `CAPABILITY_UNAVAILABLE`, retain marker and checked recovery state, create no reservation/continuation, consume no attempt, make no external call, and emit continuous `CAPABILITY_UNAVAILABLE`; remain `POLICY_HELD` pending a human. Structural unavailability never becomes `CLOSURE_VETOED`, retry, or exhaustion. |
| Reserve selected authority | Record the complete origin-specific `RESERVED/PRE_BARRIER` value, including current execution-gate snapshot ID and the complete module reservation evidence when applicable, with null spawn guard/deadline/childless attempt ID/revision/closure ID/pending disposition. Consume a selected whole-wrapper absence proof; retain the separate module confirmation for live action-time equality. Do not add a manual request ID to the committed consumed set. |
| Begin childless closure acquisition | Require exact `ClosureCapabilityV1.AVAILABLE`, acquire the module's exclusive effect guard, live-recompute the reservation, generate an attempt/continuation ID, and commit `TREE_CLOSURE_ACQUIRING` plus `ChildlessContinuationOwnerV1(CLOSURE_ACQUIRE, ARMED)` immediately before synchronously invoking the closure successor with that ID. Retain the guard through the call and result CAS. Automatic origin creates/increments `ACTIVE/ISSUED`; manual origin leaves the cycle byte-identical. Preserve existing debt. |
| Childless closure transiently absent/blocked | Under the same guard, a conforming transient refusal plus terminal matching `NEVER_ACQUIRED`, or matching `RELEASED` while still acquiring, retires the attempt and finalizes `CLOSURE_VETOED`. A reload-held closure, live joined-evidence mismatch, or late execution/manual/policy veto commits `TREE_CLOSURE_RELEASING/CLOSURE_VETOED`; exact release/reconcile calls each use the effect guard and finalize only after matching `RELEASED`. `HELD`, `UNKNOWN`, or a live foreign continuation retains every fence. A post-reservation structural-unavailability claim is malformed, remains `POLICY_HELD` with exact fences, and never reaches this row. No kill or launch occurs. |
| Childless closure held | Only the module's fresh raw-capture/live-basis/target-equality join may commit `TREE_CLOSURE_HELD`, its exact closure ID, and the acquire/reconcile continuation at `CALL_RETURNED` while the effect guard remains held. The same checked live chain must replace it atomically with `STOP_TREE/ARMED` or `CLOSURE_RELEASE/ARMED`. Preserve existing debt and automatic-cycle count. |
| Childless teardown action-ready | Acquire and retain the effect guard. In one transaction enter `TEARDOWN_IN_FLIGHT`, create/update origin-neutral debt with the immutable tuple, and persist `ChildlessContinuationOwnerV1(STOP_TREE, ARMED)`. Recheck that owner under the guard, invoke only existing `Stop-Tree`, then atomically change the same owner to `CALL_RETURNED`. A stale/nonowner continuation cannot call. |
| Childless post-action observation | Only `STOP_TREE/CALL_RETURNED` may reserve a fresh nonzero capture ordinal and observe the effect. Map it to `COMPLETE_GONE`, `SAME_OWNER_SURVIVED`, `MEMBER_SURVIVED`, or `EFFECT_UNPROVEN`; atomically enter `TREE_CLOSURE_RELEASING` with that disposition and update the same continuation ID to `CLOSURE_RELEASE/ARMED` while retaining debt/current attempt and automatic `ISSUED`. Request exact release under the retained effect guard and persist its returned stage. |
| Childless exact-release finalization | Apply the module's exhaustive event table while holding the effect guard. Only matching `RELEASED` may clear debt current-attempt fields, record failure/exhaustion, retire the attempt, release the reservation, clear the continuation owner, or clear debt/cycle. Live `COMPLETE_GONE` with the same checked continuation chain alone normalizes to `PRE_BARRIER` and returns `CONDITIONAL_POST_TEARDOWN`; reload cleanup enters `IDLE` without launch. Every finalized branch writes the same-poll terminal. |
| Non-childless teardown or final-barrier veto after no closure remains | Release the reservation directly. Retain any marker and leave launch/readiness/backoff fields unchanged. A reserved no-kill absence proof remains consumed. |
| Barrier passed, immediately before spawn | When #120 owned-tree state or post-kill provenance applies, first require its fresh deny-only launch barrier to be unblocked and unambiguous. A blocked/ambiguous result retains the launch hold, never retargets the survivor, never proves `COMPLETE_GONE`, and never clears debt. Then increment `consecutive_fails`; compute normal future automatic backoff while bypassing it for this attempt; clear `healthy_since`; set readiness fields to false/zero and `launching=true`; reset both banked child confirmations and the separate module overlay; commit `phase=SPAWN_IN_FLIGHT`, null childless attempt ID/revision/closure ID/pending disposition, null `spawned_guard`, and `pending_attempt_deadline_epoch=decision_now_epoch + resolved_launch_grace_seconds`. Preserve prior guarded identity. The pending deadline is never heartbeat freshness. Only after this commit may `Start-Process` run. |
| Proven no-spawn failure | Only an OS/API result that positively proves no child was created may set `launching=false`, release reservation, retain any marker and attempt/backoff bookkeeping, preserve prior guarded identity, clear the pending deadline, and record the typed failure result. Timeout, exception, lost return, or any uncertain post-issuance effect enters `AMBIGUOUS_LAUNCH` instead. |
| Spawn returned but guarded identity is ambiguous | Persist `AMBIGUOUS_LAUNCH` with the complete reservation, null pending deadline, and `ambiguity_boundary_poll_sequence=ordinary_poll_sequence`; reset `absence_confirmation` to `EMPTY`. For `IDENTITY_COMMIT_FAILED`, copy the returned non-null `SpawnGuardV1` identically into `reservation.spawned_guard` and `evidence.observed_guard`; for `START_RETURNED_WITHOUT_GUARD`, keep both null. Do not release authority ownership or permit another launch. |
| New guarded identity commits | In one checked transaction replace the managed identity, clear stale brain identity, reset the establishment guard to `NOT_APPLICABLE`, set `launching=true`, `readiness_seen=false`, `launch_grace_until=decision_now_epoch + resolved_launch_grace_seconds`, and `recovery_execution=IDLE`. Debt must already be `NONE` and no closure may remain. A debt-free childless cycle for a different old owner is cleared. Manual origin also adds the request ID to the consumed set and sets `manual_readiness=APPLIED_PENDING_READINESS` for this exact generation. If an automatic commit supersedes a different pending manual generation, it records `manual_readiness_superseded`, sets `manual_readiness=NONE`, and leaves that marker untouched; otherwise it preserves the bookkeeping. |
| Readiness observed | Only guarded readiness whose managed generation exactly equals `committed_managed_generation` sets `readiness_seen=true` and `launching=false`, and it alone satisfies a pending manual-readiness value. Compare-clear that marker using request ID plus revision and set `manual_readiness=NONE`; a replaced marker is untouched. Readiness for any other generation cannot change launch state, clear the marker, or satisfy the request. |

The consumed set retains the latest 128 IDs in checked commit-revision order
and evicts the oldest; the five-minute TTL prevents an evicted ancient marker
from regaining authority. Every failure leaves the marker pending. A consumed
no-kill absence proof must be rebuilt from two ordinary polls. Consumed-set
mutation and guarded-identity commit are both task #115-dependent.

**ENFORCED crash/reload and ambiguity rules:**

For the named childless phases, the following release/reconciliation bullets
run only after the module's `ChildlessSafetyReconciliationGateV1` has either
selected the state-only `MAY_RELEASE_PRE_BARRIER` path or converged through a
no-call `MAY_TAKEOVER` CAS to `MAY_RECONCILE`. Any `RETAIN_*` variant keeps the
complete fence byte-identical, makes no closure-successor call, emits module `POLICY_HELD`,
and returns. This condition does not change the preexisting non-childless
ambiguity rules.

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
- Reload of `RESERVED/TREE_CLOSURE_HELD` never terminates. It persists
  `TREE_CLOSURE_RELEASING/CLOSURE_VETOED`; matching `RELEASED` may then
  finalize directly, while matching `HELD` requires exact release by the
  persisted pair and a later matching `RELEASED`. `UNKNOWN` retains the held
  state.
- Reload of `RESERVED/TREE_CLOSURE_RELEASING` reconciles the exact persisted
  pair. `HELD` or `UNKNOWN` preserves the reservation, pending disposition,
  debt/current attempt, and automatic `ISSUED`. Only matching `RELEASED`
  applies the module's pending-disposition finalizer.
- Reload of childless `RESERVED/TEARDOWN_IN_FLIGHT` never reissues
  `Stop-Tree`. Matching `HELD` takes a fresh typed post-action observation
  under that closure, persists its releasing disposition, and follows exact
  release. Matching `RELEASED` takes a fresh
  `OwnedDebtResidualObservationV1` without assuming frozen membership:
  complete gone clears debt/cycle into `IDLE`; residual or incomplete records
  the mapped failure into `IDLE`. Reload complete-gone cleanup exports module
  `NOT_ATTEMPTED`, writes the same-poll terminal, and never launches.
  `UNKNOWN` preserves the reservation, debt/current attempt, and automatic
  `ISSUED`; `NEVER_ACQUIRED` after debt is invalid and preserves the fence.
- Before invoking `Start-Process`, the checked state must already say
  `SPAWN_IN_FLIGHT`. Reload of that phase becomes
  `AMBIGUOUS_LAUNCH(CRASHED_DURING_SPAWN)`, records the then-current
  `ordinary_poll_sequence` as its boundary, resets absence confirmation, and
  sets `launching=false`.
- The launch reservation ID is passed to the wrapper and returned in the
  guarded managed-identity checkpoint. While `AMBIGUOUS_LAUNCH`, every manual
  and automatic teardown/replacement attempt is `HOLD`; marker deletion does
  not clear the hold.
- A later strict checkpoint whose PID/start guard and launch reservation ID
  exactly match `SpawnGuardV1` is adopted through the common guarded-launch
  commit below. Any mismatch remains ambiguous.
- Otherwise only a new `PhysicalAbsenceProofV1.CONFIRMED`, built from two
  compatible ordinary captures whose poll sequences are both strictly greater
  than `ambiguity_boundary_poll_sequence`, resolves it to `IDLE`, leaves a
  manual marker pending when present, and sets
  `launching=false`. That new confirmation remains available for one new
  reservation. Present, unknown, replayed, pre-ambiguity, or incomplete
  evidence cannot resolve the tombstone.
- `manual_readiness=APPLIED_PENDING_READINESS` survives reload without blocking
  recovery. Matching-generation readiness clears it only after the marker
  compare-clear result is durably recorded; a replaced marker is never removed.
  A later committed manual launch may replace the bookkeeping only after the
  older request ID is already durable in the consumed set. A different
  automatic generation supersedes and clears only the bookkeeping as specified
  above, never the marker.

**ENFORCED common origin-independent commit after task #115:** Automatic and
manual launches use one `GuardedLaunchCommitV1`. Only after the final barrier,
spawn, and a strict PID/start/reservation checkpoint does one checked
transaction replace managed identity, establish the real
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
 stays `NONE` until a synchronous typed `COMPLETE_GONE` atomically clears that
debt, after which the ordinary nonpersisted conditional may proceed. A
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
8. only then call `Start-Process`.

For `PROVABLY_CHILDLESS_OWNED_WRAPPER`, the normative module replaces step 3
with the closure successor's action-time tree closure, exact reserved-target digest check,
checked debt/attempt commit, sole `Stop-Tree` adapter, and typed complete-gone
proof. It rejoins step 4 only after origin-neutral debt is cleared. Module debt
forces every unrelated launch proof to `NONE` while permitting only its
debt-bound residual cleanup, so neither manual nor automatic absence can
bypass a partial kill.

#120's barrier is not the closure successor. At candidate `28f663f`, planning
and `Stop-Tree` are separated by process scheduling: a recorded parent may
create a late descendant after planning and then exit, leaving a process that
was never in the planned target set and may survive `Stop-Tree`. The barrier
catches that descendant only to block launch. It never adds a kill target,
proves `COMPLETE_GONE`, clears debt, or substitutes for the successor's
pre-effect creation closure. For the named childless path, the module consumes
the first such result in its typed post-action observation; step 6 performs the
fresh final recheck immediately before spawn.

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
32. Pause two pollers at the closure-acquire commit/effect gap and at the
    teardown-arm/`Stop-Tree` gap. A live foreign continuation retains every
    fence. After positive predecessor-death proof, only exact idempotent
    reconciliation may proceed; `ARMED` teardown never proves completion or
    permits reissue, and `CALL_RETURNED` alone permits post-action capture.
    Exercise the state-only `PRE_BARRIER` release and every exact no-call
    takeover mapping before `MAY_RECONCILE`.
33. Recreate checked state after loss at childless attempt one, two, and three,
    and after partially acted debt. Require `STATE_PROVENANCE_LOST` to deny
    every kill/launch/mutation despite a new epoch or same physical owner.
    Clear only by the module's complete
    provably-different-owner/extinction transition. A structurally valid stale
    backup must remain quarantined.
34. Persist `SPAWN_IN_FLIGHT` with null `spawned_guard` only. A valid returned
    guard atomically commits identity without that intermediate state; an
    ambiguous return enters `AMBIGUOUS_LAUNCH`, where nested and evidence guard
    copies are exactly equal and match the ambiguity code's nullability.
    Reload of a valid-guard standalone `SPAWN_IN_FLIGHT` is invalid and holds.
35. Cross manual/automatic origin with confirmed whole-wrapper absence and
    child-death-sourced residue. With debt `NONE`, both select no-kill
    `RELAUNCH_ONLY`; outstanding debt suppresses both. Recompute the module's
    chained seven-domain vector and race nonordinary capture-ordinal
    allocation as required by its conformance section.
36. Integrate the module's exact #120 candidate adapter and post-kill barrier
    control. Race a recorded parent that creates a descendant after planning
    and exits during `Stop-Tree`; require the late descendant to miss the
    planned target set, survive, and be detected only by the fresh deny-only
    barrier before `SPAWN_IN_FLIGHT`. The barrier must hold launch without
    retargeting the descendant, proving `COMPLETE_GONE`, clearing debt, or
    substituting for the closure successor. An unavailable/ambiguous barrier
    also holds, and an unblocked barrier without the typed post-action proof
    cannot clear debt or launch.
37. Integrate every `ClosureCapabilityV1` reason before reservation and require
    zero reservation, attempt, continuation, external call, teardown, retry,
    and exhaustion with continuous `CAPABILITY_UNAVAILABLE`/`POLICY_HELD`
    pending a human. Inject a post-reservation structural-unavailability claim
    and `UNKNOWN(CAPABILITY_UNAVAILABLE)` reconciliation; retain every exact
    fence and forbid `CLOSURE_VETOED`, retry, exhaustion, kill, and launch.
    Export all applicable module attention codes in their exact order,
    including `CHILDLESS_STATE_PROVENANCE_LOST` and
    `CAPABILITY_UNAVAILABLE`, and require `action_attention_required=true`.

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
| No daemon, persistence plane, durable helper or OS object, or runtime dependency | DECIDED ABSOLUTE by operator on 2026-07-31 (M5 Option A) | Pure code, existing checked state, and transient caller-owned synchronization only; there is no mechanism-specific exception, and the nonexistent closure successor remains `CAPABILITY_UNAVAILABLE` when that boundary cannot prove its contract. |
| Earlier fresh-but-confirmed-absent recovery | STATED out of scope | Task #116, blocked on #115 but not #87, scheduled before 87-A implementation. |
| Durable incident visibility/delivery | STATED out of scope | Future 87-B, dependent on tasks #114/#115. |
| Migration and rollback | STATED out of scope | Future 87-C after 87-A/87-B. |
| Raw discovery stops flapping | STATED not promised | Process-discovery behavior is unchanged. |
| Owned-childless teardown requires a nonce-owned complete tree and action-time closure | CURRENTLY UNAVAILABLE; ENFORCED after #115, merged/reviewed #120, and a conforming closure successor | No reviewed closure successor exists as of 2026-07-31, so the named path is `CAPABILITY_UNAVAILABLE` and `POLICY_HELD` pending a human. |
| Child-establishment grace cannot be sampled away | ENFORCED after #115, merged/reviewed #120, and the closure successor | Nonrenewable same-turn closed guard in observation, confirmation, reservation, and action equality. |
| External childless calls cannot outlive their authority owner | ENFORCED after #115 and the closure successor | Exclusive effect guard, checked continuation owner/stage, stable attempt tombstones, and successor-owned synchronous adapters. |
| Partial owned-childless teardown cannot be laundered into launch | ENFORCED after #115, merged/reviewed #120, and the closure successor | Origin-neutral durable teardown debt and debt-bound completion authority. |
| Automatic owned-childless retry stops at three without fading from attention | ENFORCED after tasks #78/#115 | Durable childless-only cycle, hard cap, and independent action-attention output. |
| State loss cannot reset a cap or erase teardown debt | ENFORCED after task #115 | Fail-closed quarantine until complete different-owner/extinction proof; no exact-restoration or valid-backup escape exists in this revision. |

This core and its same-commit normative module together are sufficient to
implement and review 87-A's pure classifier and authority substrate. Neither
file alone is conforming. They are not permission to activate the behavior and
make no delivery or migration promise.
