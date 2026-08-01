# Design 87-A module: owned-childless wrapper authority

**Design status:** Proposed, Revision 12; candidate for
**NORMATIVE-SPECIFICATION COMPLETE**. This file and
[`DESIGN-87A-supervisor-classifier-authority.md`](DESIGN-87A-supervisor-classifier-authority.md)
at the same commit form one specification. Neither is conforming alone.

**Delivery status:** **IMPLEMENTATION BLOCKED.** Task #146 must replace the
merged raw supervisor owned-tree kill entry with the closed dispatcher specified
below and migrate every current caller. Task #115 and the closure successor also
remain open dependencies. Revision 12 specifies those boundaries; it does not
deliver them.

**Conformance status:** **UNAVAILABLE.** Neither Q4 nor 87-A is complete,
conforming, sealed, or enforced in merged code. If this revision passes design
review, only the normative specification is complete. Runtime conformance still
requires the named implementations and their executed controls.

**Activation status:** **PROHIBITED** until delivery and conformance close.

**Mode:** Reference.

**Audience:** Contributors implementing tasks #78, #115, #120, and #146, the
closure successor defined below, and reviewers checking teardown authority.

## Boundary and placement decision

This module adds one authority case:
`PROVABLY_CHILDLESS_OWNED_WRAPPER`. The authority rule belongs in 87-A because
87-A owns the recovery combiner. The Windows tree-observation mechanism and
post-kill launch barrier do not: task #120 owns those surfaces. The
action-scoped child-creation closure and its effect-linearized
acquire/reconcile/release adapters also do not belong to 87-A, but task #120
does not implement them. This document assigns that missing mechanism to an
explicit **closure successor**: a separately reviewed extension to #120 or a
successor task whose final task ID is assigned outside this design. 87-A
defines the exact typed contracts and adapter joins both dependencies must
satisfy.

This is a normative seam, not hand-waving:

```text
87-A observes and classifies
  -> #120 publishes a bounded owned-tree snapshot or a closed refusal
  -> that snapshot, every parsed target, and every reloaded state remain inert evidence
  -> #146 places the supervisor owned-tree native kill body behind one closed dispatcher
  -> only that dispatcher's private capability factory may mint an exact-executor witness
  -> only an exact witness-plus-binding match may construct an operation-scoped permit
  -> only a permit may construct a reservation, effect-owned mutation, or typed external call
  -> adapters accept typed calls and return matching typed receipts, never raw IDs or state
  -> the dispatcher alone projects a private immutable native plan into the sole native body
  -> #120's post-kill barrier may block, but never authorize, a launch
```

This is a construction boundary, not a requirement to remember a check at every
state entry. An eighteenth, nineteenth, or future childless-origin state may
deserialize inert evidence, but it cannot manufacture the object needed to act.
Preserving existing non-87-A behavior rather than preserving the raw kill entry
point is the compatibility principle. A caller-settable discriminator or a
typed wrapper around `kill_targets` is not authorization.

The core's independently approved operand convention, 96-cell dominant
projection, presence/targetability classifier, whole-wrapper absence reducer,
and condition fingerprint codec/vectors are imported byte-for-byte. This
module does not alter their inputs, equations, tables, serialization, or
counts.

The
[`DESIGN-87A-delta-panel-disposition.md`](DESIGN-87A-delta-panel-disposition.md)
register maps every finding from the panel over `f42570d..44b3787` to its
normative location. It is audit evidence, not a third 87-A specification.

## Safety decision

**SPECIFIED, NOT DELIVERED; requires tasks #115 and #146 plus the closure
successor (#120 input delivered at `587e7c1`):** An owned wrapper whose CLI child is
positively absent in two independent complete observations may be torn down
only after the same-turn nonrenewable `ChildEstablishmentGuardV1` is
`CLOSED`. Once closed, such a wrapper has no brain and no progressing CLI turn
to interrupt. This is positive evidence of nonexistence after the
child-establishment window, not an inference from silence, heartbeat
staleness, or two fast pre-handoff captures.

`CURRENT_UNKNOWN_ACTIVE_CHILD` is the refused neighboring state. It includes
an incomplete/ambiguous child observation, any complete zero-child capture
while `ChildEstablishmentGuardV1` is `OPEN`, and the first post-close complete
`ABSENT` sample. The snapshot-quality/establishment bit is load-bearing:
pre-close absence is never `ABSENT`, cannot seed either counter, and cannot be
carried across guard closure. This authority requires two adjacent post-close
complete `ABSENT` samples for the same guarded owner and guard object. Changing
completeness, establishment state, owner, or confirmation count changes
authority deterministically from the named case to none.

**SPECIFIED, NOT DELIVERED by the future 87-A adapter over merged, reviewed task
#120 (input delivered at `587e7c1`):** Ownership is never inferred from a process name,
executable basename, image substring, or command-line pattern. The wrapper
must match the persisted PID, exact start guard, and launch nonce. Every tree
target carries that same owner nonce through a complete #120 ownership proof.
Missing, malformed, unreadable, or mismatched identity refuses authority.

**STATED threat boundary:** This design protects against incomplete
observation, PID reuse, accidental cross-agent selection, process creation
races, partial teardown, crash/reload, and retry fade-out. It does not defend
against a malicious same-user process that can alter supervisor state or
directly terminate arbitrary processes.

## Published #120 snapshot and closure-successor contracts

**DELIVERED #120 integration status (2026-08-01):** task #120 shipped on master
as squash commit `587e7c1`. This specification maps the merged content, not the
pre-review candidate SHA.

Merged #120 persists strict-schema `owned_process_tree_v2` in existing
supervisor state. It records at most 64 parent-first entries with PID, start,
`start_filetime`, role, parent PID, discovery time, wrapper generation, and one
top-level launch nonce. Complete/absent Windows records require a positive
decimal exact creation FILETIME for every ISO-start entry; nullable FILETIME is
retained only in truncated/invalid HOLD diagnostics. The 87-A adapter maps it
as follows:

| #120 value | 87-A mapping |
| --- | --- |
| `status=complete`, `limit=64`, internally consistent counts, no omission/truncation, and valid generation/nonce | Candidate input to `OwnedWrapperTreeObservationV1.COMPLETE`; the adapter still performs the positive owner join and validates every live target against the same fresh complete raw process capture. |
| `status=absent` | No-kill absence/barrier evidence only; never an initial owned-tree teardown authority. |
| `status=truncated|invalid`, a non-64 limit, inconsistent counts, omitted entries, or an unreadable binding | `INCOMPLETE`; no teardown authority. |
| Live Windows `entries[].pid/start/start_filetime` | `OwnedTreeTargetV1.pid/start_guard` after exact live validation. The destructive guard is the positive decimal `start_filetime`; the rounded ISO `start` is capture/ordering corroboration and never substitutes for a missing FILETIME. The adapter derives and validates `parent_start_guard` and `depth` from the accepted live parent chain and projects the validated top-level owner nonce onto each target; it may not privately default any missing fact. |
| Non-Windows platform, including a live Linux `entries[].start` token | #120 recognizes Linux `linux:<boot_id>:<start_ticks>` as observation/barrier input; it declares no macOS exact-token mapping. The merged supervisor owned-tree native body has no non-Windows exact-token execution branch, so fresh authority construction returns pre-reservation `CAPABILITY_UNAVAILABLE(EXACT_TARGET_EXECUTOR_UNAVAILABLE)`, short-circuits before the effect envelope or reservation exists, and constructs no executable target, permit, attempt, debt, or external call. Reloaded evidence may be parsed and ordinary observation bookkeeping may advance, but no matching permit or effect-owned mutation is constructible. Input acceptance is not effect capability. |
| Non-live virtual ancestry bridge | Admissible only when copied exactly from a prior complete record with unchanged wrapper generation, launch nonce, and parent chain. It is validation provenance only: exclude it from `root_first_targets`, every target digest, and `Stop-Tree`. A live child whose immediate owned parent is such a bridge uses the module's existing positively proven orphan form: null owned-parent fields and depth one. |

`role` and `discovered_at` are validated bounded metadata but are excluded from
owner identity, target tuples, and target digests. The unique live wrapper
entry becomes depth zero with null internal parent fields; its external
supervisor/console parent is never an owned edge. The reserved
`detached_gate_runner` role never grants ownership by itself.
The owner retains the accepted wrapper row's reported `start` separately as
`wrapper_start_token` and its destructive exact identity as
`wrapper_start_guard`; neither field may substitute for the other.

#120 also strengthens target-local effect execution and implements a post-kill
launch barrier. For an openable planned Windows owned-tree target whose exact
creation FILETIME matches, `Stop-Tree` verifies identity and terminates through
one native handle. Every successful termination receives a wait attempt within
the remaining shared tree-wide budget before the fresh snapshot. Thus PID reuse
cannot occur between identity check and kill on a terminated target, and a
target that signals within the budget is not sampled as live. Open failure,
identity mismatch, termination failure, wait timeout, or depleted budget is not
completion and is decided only by the fresh snapshot and barrier; a target still
present remains survivor evidence.

Those target-local semantics do not make merged `Stop-Tree($targets)` a
conforming 87-A executor. At `587e7c1`, that function accepts a caller-supplied
raw array and current planners call it directly. The unsealed entry can reach
the native termination body without an 87-A permit. Consequently merged #120
cannot mint `CurrentExactTargetExecutorWitnessV1`, even on Windows. Task #146
must preserve the described non-87-A behavior while removing the raw entry,
migrating every current caller through an independently authorized opaque call,
and making the closed dispatcher the only route to the native body.

The barrier rechecks recorded exact identities and fresh descendant edges.
For an independently authorized Windows attempt, planning and `Stop-Tree`
remain separated by process scheduling, so a recorded parent may create a
descendant after the plan; that unplanned descendant may
survive because it was never a kill target, and the barrier catches it only to
block launch. When a recorded PID has been recycled, an exact child start equal
to or newer than the replacement is excluded from the retired-parent ownership
edge; a child provably older than the replacement, or exact evidence that is
missing/incomparable, remains conservative old-side survivor evidence. This
split does not suppress independent barrier evidence: the replacement-side
process still blocks if, for example, its command line parses as this agent's
wrapper or wait process. The barrier never adds a kill target, proves teardown
effect, or supplies closure evidence. The attended reset supplies an
exact-identity-bound human escape, and the request-bound attended archive
supplies retention; neither grants automatic teardown authority, closes child
creation, or proves an automatic effect complete.

**STATED merged behavioral delta:** Once #115, #146, and a future conforming
closure provider have independently authorized teardown, a planned target whose
termination succeeds and which signals within #120's remaining wait budget no
longer appears in the immediate post-kill snapshot, and an exact
replacement-side child of a recycled PID no longer creates a sticky barrier
HOLD solely through the retired-parent edge. When no independent barrier reason
applies, such an already-authorized attempt may therefore reach `COMPLETE_GONE`
where the pre-merge candidate would have held. This is
strictly downstream of the capability gate: it does not make any currently
`CAPABILITY_UNAVAILABLE` closure-dependent teardown start.

The closure successor owns `OwnedTreeClosureV1`,
`OwnedTreeClosureReconciliationV1`, action-scoped creation freeze, and the
synchronous acquire/reconcile/release and checked-continuation adapters below.
Merged #120 partially strengthens Windows effect execution at one target-local
seam; it does not implement those attempt-bound contracts or prevent an owned parent
from creating a new child between plan and effect. Until a conforming successor
is implemented, independently reviewed, and available inside the absolute
dependency-plane constraint below, a static
pre-reservation capability gate returns
`ClosureCapabilityV1.CAPABILITY_UNAVAILABLE` without creating a reservation,
consuming an attempt, or making an external call. No closure-dependent named
teardown proceeds, and dependent recovery remains `POLICY_HELD` pending a
human. Only `ClosureCapabilityV1.AVAILABLE` may reach acquisition. Once a
conforming provider advertises availability, a synchronous transient acquire
failure follows the ordinary closure-veto/attempt rules; structural inability
never does. Merged #120 narrows snapshot, target-identity, and post-effect
ambiguity; it does not narrow this closure-capability refusal, and no formerly
held closure-dependent teardown now proceeds. This structural refusal is scoped
to the named closure-dependent path. Task #146 preserves merged #120's
snapshot, exact-target behavior, barrier, attended reset, and attended archive
for independently authorized non-87-A callers; it does not preserve the raw
call signature or treat a planner array as authority.

Capability claims are proved at the line where the effect happens, not where an
input is accepted. Merged #120 accepts Linux `linux:<boot_id>:<start_ticks>` as
an exact observation token (grammar at
`src/agenttalk/supervisor.py:2187-2204`; exactness/record validation at
`2087-2115`, `2438-2470` in `587e7c1`), but its current raw executor enters the destructive branch only when
`start_filetime` is present (`8900-8928`) and explicitly skips an
`owned_process_tree` target without it (`8930-8932`). Therefore that accepted
token is not a POSIX kill adapter. Linux and macOS are structurally unavailable
for this named teardown until a separately reviewed exact-token executor is
delivered; Revision 12 does not dependency-track or assume one. Windows is also
unavailable to 87-A until #146 seals the dispatcher, even though merged #120's
target-local FILETIME mechanics exist. The Windows FILETIME requirement is
immutable and is never weakened to make another platform appear available.

```text
MAX_OWNED_TREE_TARGETS_V1 = 64
AUTOMATIC_CHILDLESS_ATTEMPT_CAP_V1 = 3

ProcStartGuardV1 =
  nonempty NFC UTF-8 process-start representation token of at most 256 bytes

OwnedExactStartGuardV1 =
  Windows ISO-start row: positive decimal exact creation FILETIME

OwnedLaunchNonceProvenanceV1 {
  checked_managed_launch_nonce: ASCII [A-Za-z0-9_-]{16,128}
  parsed_observed_root_launch_nonce: ASCII [A-Za-z0-9_-]{16,128}
  observed_parser_schema: "supervisor-launch-nonce/v1"
}

OwnedWrapperIdentityV1 {
  agent_key: NFC canonical agent/root string
  state_epoch: lowercase hyphenated UUID
  managed_generation: NFC UTF-8 string of at most 128 bytes
  runtime_wrapper_generation: NFC UTF-8 string of at most 128 bytes
  wrapper_pid: integer 1..4294967295
  wrapper_start_token: ProcStartGuardV1
  wrapper_start_guard: OwnedExactStartGuardV1
  launch_nonce: ASCII [A-Za-z0-9_-]{16,128}
  nonce_provenance: OwnedLaunchNonceProvenanceV1
}

OwnedPhysicalWrapperIdentityV1 =
  exact projection of OwnedWrapperIdentityV1 excluding state_epoch

QuarantineRetirementCapabilityV1 =
  CAPABILITY_UNAVAILABLE(PROCESS_UNIVERSE_IDENTITY_UNAVAILABLE)

StateLossQuarantineV1 =
  NONE
  | UNRESOLVED {
      quarantine_id: lowercase hyphenated UUID
      reason: MISSING | CORRUPT | TORN | ROLLBACK_UNPROVEN
      prior_physical_owner: OwnedPhysicalWrapperIdentityV1 | UNKNOWN
      attempt_provenance: UNKNOWN
      debt_provenance: UNKNOWN
      retirement_capability:
        QuarantineRetirementCapabilityV1.CAPABILITY_UNAVAILABLE
    }

OwnedTreeTargetV1 {
  pid: integer 1..4294967295
  start_guard: OwnedExactStartGuardV1
  parent_pid: integer 1..4294967295 | null
  parent_start_guard: OwnedExactStartGuardV1 | null
  depth: uint32
  owner_launch_nonce: ASCII [A-Za-z0-9_-]{16,128}
}

OwnedTreeCoverageV1 {
  observer_version: nonempty NFC UTF-8 string of at most 128 bytes
  process_source_digest: Hex64
  ownership_rule_version: "owned-tree/v2"
}

OwnedWrapperTreeObservationV1 =
  INCOMPLETE(
    ordered deduplicated nonempty tuple[
      CAPABILITY_UNAVAILABLE
      | SNAPSHOT_UNAVAILABLE
      | SNAPSHOT_TRUNCATED
      | COVERAGE_UNREADABLE
      | CAPTURE_ID_MISMATCH
      | MANAGED_IDENTITY_INCOMPLETE
      | WRAPPER_NOT_FOUND
      | WRAPPER_PID_START_MISMATCH
      | LAUNCH_NONCE_UNREADABLE
      | LAUNCH_NONCE_INVALID
      | LAUNCH_NONCE_MISMATCH
      | TREE_MEMBER_IDENTITY_UNREADABLE
      | TREE_MEMBERSHIP_AMBIGUOUS
      | TREE_TARGET_LIMIT_EXCEEDED
    ] in displayed order
  )
  | COMPLETE {
      capture_id: CaptureIdV1
      ordinary_poll_sequence: uint64
      coverage: OwnedTreeCoverageV1
      owner: OwnedWrapperIdentityV1
      owner_identity_id: Hex64
      root_first_targets:
        tuple[OwnedTreeTargetV1] of length 1..64
      target_digest: Hex64
    }

OwnedDebtResidualObservationV1 =
  INCOMPLETE(
    ordered deduplicated nonempty tuple[
      CAPABILITY_UNAVAILABLE
      | SNAPSHOT_UNAVAILABLE
      | SNAPSHOT_TRUNCATED
      | COVERAGE_UNREADABLE
      | CAPTURE_ID_MISMATCH
      | DEBT_BINDING_MISMATCH
      | RESIDUAL_IDENTITY_UNREADABLE
      | RESIDUAL_MEMBERSHIP_UNPROVEN
      | RESIDUAL_NOT_AUTHORIZED_SUBSET
      | NEW_OWNER_MEMBER_OBSERVED
      | TREE_TARGET_LIMIT_EXCEEDED
    ] in displayed order
  )
  | COMPLETE_GONE {
      capture_id: CaptureIdV1
      ordinary_poll_sequence: uint64
      coverage: OwnedTreeCoverageV1
      owner_identity_id: Hex64
      debt_id: Hex64
      debt_generation: strict positive integer
    }
  | COMPLETE_RESIDUAL {
      capture_id: CaptureIdV1
      ordinary_poll_sequence: uint64
      coverage: OwnedTreeCoverageV1
      recorded_owner: OwnedWrapperIdentityV1
      owner_identity_id: Hex64
      debt_id: Hex64
      debt_generation: strict positive integer
      ordered_targets:
        tuple[OwnedTreeTargetV1] of length 1..64
      target_digest: Hex64
    }

ClosureProviderVersionV1 =
  nonempty NFC UTF-8 string of at most 128 bytes

ExactTargetExecutorBindingV1 {
  platform: WINDOWS
  executor_contract: "stop-tree/windows-filetime/v1"
  owner_identity_id: Hex64
  authorized_target_digest: Hex64
}

CurrentExactTargetExecutorWitnessV1 =
  fresh private, nonserializable, noncopyable, deeply immutable current-process
  witness minted before permit/call construction only by #146's unexported
  dispatcher-capability factory after proving this is a conforming Windows
  dispatcher installation whose private native branch implements
  "stop-tree/windows-filetime/v1"; it carries an unexported live
  dispatcher-instance identity but grants no target authority by itself

CurrentExactTargetExecutorWitnessConstructionV1 =
  AVAILABLE(CurrentExactTargetExecutorWitnessV1)
  | CAPABILITY_UNAVAILABLE(
      EXACT_TARGET_EXECUTOR_UNAVAILABLE
      | DISPATCHER_SEAL_UNDELIVERED)

LiveEffectGuardLineageV1 =
  one private, nonserializable, noncopyable in-process lineage minted exactly
  once for one successful acquisition of the exact per-agent effect-guard
  handle, carrying fresh lineage_id and acquisition_generation

LiveEffectGuardLineageOwnerV1 =
  the one unexported atomic owner cell created with that acquisition and
  destroyed with guard release; it alone stores LiveEffectGuardLineageStateV1,
  exposes no clone/mint API, and is never a field reachable from a sealed effect
  value

LiveEffectGuardLineageStateV1 =
  AVAILABLE(LiveEffectGuardCustodyV1)
  | OUTSTANDING {
      lineage_id: lowercase hyphenated UUID
      issuance_id: lowercase hyphenated UUID
      holder: PERMIT | OWNER_COMMIT | CALL | DISPATCHING | PLAN_OWNED
              | INVOKING | RECEIPT
    }
  | POISONED {
      lineage_id: lowercase hyphenated UUID
      issuance_id: lowercase hyphenated UUID
      cause: ADAPTER_EFFECT_UNCERTAIN | OWNER_COMMIT_UNCERTAIN
               | RECEIPT_HANDOFF_UNCERTAIN | CUSTODY_PROTOCOL_BROKEN
    }
  | CLOSED

LiveEffectGuardCustodyV1 =
  the sole private, nonserializable, noncopyable, deeply immutable linear proof
  for one LiveEffectGuardLineageV1; it contains no mutable owner-cell reference,
  is minted initially for AVAILABLE, and on each private holder transition is
  consumed, never copied, into exactly one successor proof for the same lineage
  and issuance. The first transition is the atomic
  AVAILABLE -> OUTSTANDING move in LiveEffectGuardLineageOwnerV1.

ExactTargetExecutorOperationV1 =
  RESERVE | PRE_BARRIER_RELEASE | TAKEOVER
  | CLOSURE_ACQUIRE | CLOSURE_RECONCILE | CLOSURE_RELEASE
  | RETIRED_ATTEMPT_RECONCILE | STOP_TREE | POST_ACTION_CAPTURE
  | EFFECT_FINALIZE | SPAWN | SPAWN_RESULT_COMMIT
  | SPAWN_IDENTITY_COMMIT | OWNER_TRANSITION

ExactTargetExecutorPermitUseV1 =
  STATE_MUTATION | EXTERNAL_CALL | RECEIPT_MUTATION

ExactTargetExecutorPermitV1 =
  fresh private, nonserializable, noncopyable, deeply immutable, single-use
  permit constructed only from:
    CurrentExactTargetExecutorWitnessV1
    + exact ExactTargetExecutorBindingV1
    + the operation's exact binding proof: normally the recomputed exact
      owner/authorized-target tuple and, when applicable, an exact authorized
      residual subset; only the closed targetless old-side rebind and cleanup
      scopes below may use an exact tombstone/envelope binding plus their typed
      subject and prospective proof
    + current checked-state revision
    + one ExactTargetExecutorOperationV1
    + one ExactTargetExecutorPermitUseV1
    + that operation's exact live scope defined below
    + for a guard-required STATE_MUTATION or EXTERNAL_CALL, the one
      AVAILABLE LiveEffectGuardCustodyV1 moved atomically into this permit's
      fresh issuance_id; no second issuance from the acquisition is possible
      while its lineage is OUTSTANDING, POISONED, or CLOSED
    + for RECEIPT_MUTATION, the exact allowed predecessor receipt, consumed as
      the sole OUTSTANDING holder while preserving its lineage_id and
      issuance_id; this continuation neither requires AVAILABLE custody nor
      creates a fresh issuance

ExactTargetExecutorPermitConstructionV1 =
  PERMITTED(ExactTargetExecutorPermitV1)
  | CAPABILITY_UNAVAILABLE(EXACT_TARGET_EXECUTOR_UNAVAILABLE)
  | REJECTED(
      STALE_CHECKED_REVISION
      | OPERAND_MISMATCH
      | INVALID_OPERATION_SCOPE
      | LINEAGE_NOT_AVAILABLE_OR_POISONED
      | COPIED_FORGED_REPLAYED_OR_CONSUMED)

ClosureCapabilityV1 =
  AVAILABLE {
    reservation_permit:
      ExactTargetExecutorPermitV1(operation = RESERVE, use = STATE_MUTATION)
    closure_provider_version: ClosureProviderVersionV1
  }
  | CAPABILITY_UNAVAILABLE(
      ordered deduplicated nonempty tuple[
        EXACT_TARGET_EXECUTOR_UNAVAILABLE
        | DISPATCHER_SEAL_UNDELIVERED
        | SUCCESSOR_MISSING
        | SUCCESSOR_UNREVIEWED
        | PROVIDER_INCOMPATIBLE
        | CONTRACT_UNPROVABLE
        | FORBIDDEN_MECHANISM_REQUIRED
      ] in displayed order
    )

OwnedTreeClosureV1 =
  BLOCKED(
    ordered deduplicated nonempty tuple[
      OWNER_CHANGED
      | CHILD_OBSERVATION_CHANGED
      | DEBT_BINDING_CHANGED
      | CLOSURE_ACQUIRE_FAILED
      | CLOSURE_LOST
      | TREE_OBSERVATION_INCOMPLETE
      | TARGET_DIGEST_CHANGED
    ] in displayed order
  )
  | HELD {
      closure_provider_version: ClosureProviderVersionV1
      closure_id: lowercase hyphenated UUID
      acquisition_id: lowercase hyphenated UUID
      mode: INITIAL | DEBT_COMPLETION
      owner_identity_id: Hex64
      debt_id: Hex64 | null
      debt_generation: strict positive integer | null
      capture_id: CaptureIdV1
      raw_process_observation: ProcessObservationV1
      coverage: OwnedTreeCoverageV1
      ordered_targets:
        tuple[OwnedTreeTargetV1] of length 1..64
      target_digest: Hex64
    }

OwnedTreeClosureReconciliationV1 =
  NEVER_ACQUIRED {
    closure_provider_version: ClosureProviderVersionV1
    acquisition_id: lowercase hyphenated UUID
  }
  | RELEASED {
      closure_provider_version: ClosureProviderVersionV1
      acquisition_id: lowercase hyphenated UUID
      closure_id: lowercase hyphenated UUID
    }
  | HELD(OwnedTreeClosureV1.HELD)
  | UNKNOWN(
      ordered deduplicated nonempty tuple[
        CAPABILITY_UNAVAILABLE
        | ACQUISITION_NOT_QUERYABLE
        | CLOSURE_STATE_UNREADABLE
        | RELEASE_UNPROVED
      ] in displayed order
    )

ChildlessContinuationSubjectV1 =
  ACTIVE_ATTEMPT {
    reservation_id: lowercase hyphenated UUID
    attempt_id: lowercase hyphenated UUID
    attempt_revision: uint64
    closure_provider_version: ClosureProviderVersionV1
  }
  | RETIRED_ATTEMPT {
      attempt_id: lowercase hyphenated UUID
      attempt_revision: uint64
      closure_provider_version: ClosureProviderVersionV1
    }
  | SPAWN_RESERVATION {
      reservation_id: lowercase hyphenated UUID
    }

ChildlessContinuationOperationV1 =
  CLOSURE_ACQUIRE | CLOSURE_RECONCILE | CLOSURE_RELEASE
  | RETIRED_ATTEMPT_RECONCILE | STOP_TREE | POST_ACTION_CAPTURE
  | SPAWN

TakeoverOriginV1 =
  NONE
  | FROM {
      predecessor_continuation_id: lowercase hyphenated UUID
      predecessor_operation: ChildlessContinuationOperationV1
      predecessor_effect_stage: ARMED | CALL_RETURNED
    }

ChildlessContinuationOwnerV1 =
  NONE
  | OWNED {
      supervisor_instance_token_digest: Hex64
      supervisor_pid: integer 1..4294967295
      supervisor_start_guard: ProcStartGuardV1
      action_latch_epoch: uint64 | null
      continuation_id: lowercase hyphenated UUID
      role: ISSUER | RECONCILER
      subject: ChildlessContinuationSubjectV1
      operation: ChildlessContinuationOperationV1
      effect_stage: ARMED | CALL_RETURNED | TAKEOVER_CHECKPOINT
      takeover_origin: TakeoverOriginV1
      armed_state_revision: uint64
    }

BoundRetiredChildlessAttemptV1 {
  attempt_id: lowercase hyphenated UUID
  attempt_revision: uint64
  executor_binding: ExactTargetExecutorBindingV1
  closure_provider_version: ClosureProviderVersionV1
  cleanup_state:
    TERMINAL(NEVER_ACQUIRED | RELEASED)
    | RELEASE_PENDING(closure_id: lowercase hyphenated UUID)
}

ChildlessEffectEnvelopeV1 {
  executor_binding: ExactTargetExecutorBindingV1
  execution: core ChildlessRecoveryExecutionV1
  teardown_debt: TeardownDebtV1
  automatic_cycle: AutomaticChildlessCycleV1
  continuation_owner: ChildlessContinuationOwnerV1
  retired_attempts:
    bounded ordered tuple[BoundRetiredChildlessAttemptV1] of length 0..128
}

ExecutableOwnedTargetSetV1 =
  private, nonserializable, noncopyable, deeply immutable, alias-free exact
  root-first target tuple and recomputed target digest
  bound to one ExactTargetExecutorPermitV1(
    operation = STOP_TREE, use = EXTERNAL_CALL)

PermitBoundChildlessMutationV1 =
  private, nonserializable, noncopyable, deeply immutable, alias-free
  checked-state delta containing the exact expected revision,
  one current ExactTargetExecutorPermitV1 whose use is STATE_MUTATION or
  RECEIPT_MUTATION, the exact current envelope or
  NO_CHILDLESS_ENVELOPE for an initial RESERVE, and the complete next
  ChildlessEffectEnvelopeV1 or terminal envelope removal allowed by that
  permit's operation, plus the exact closed core ChildlessOuterStateDeltaV1

ChildlessExternalEffectCallV1 =
  private, nonserializable, noncopyable, deeply immutable, alias-free,
  single-use typed call in
    CLOSURE_ACQUIRE | CLOSURE_RECONCILE | CLOSURE_RELEASE
    | RETIRED_ATTEMPT_RECONCILE | STOP_TREE | POST_ACTION_CAPTURE
    | SPAWN
  carrying one call_id, an exact permit whose use is EXTERNAL_CALL, its exact
  binding/continuation, only that operation's typed arguments, and ownership of
  the permit's exact immutable LiveEffectGuardCustodyV1 proof and issuance_id;
  its separate private lineage owner cell is atomically in holder CALL

ChildlessExternalEffectReceiptV1 =
  private, nonserializable, noncopyable, deeply immutable, alias-free,
  single-use typed result carrying the exact call_id, operation,
  consumed call-permit identity/binding, call-time checked revision,
  operation-specific result, and
  the same still-live immutable custody proof moved by the synchronous adapter;
  its separate private lineage owner cell is atomically in holder RECEIPT

ConfiguredAgentOwnedTreeCallV1 =
  private, nonserializable, noncopyable, deeply immutable, alias-free,
  single-use call constructible only from the checked configured-agent
  relaunch/stuck-recovery planner provenance, the exact persisted barrier_state
  authorizing that plan, a privately sealed exact target tuple, one fresh
  call_id, and one immutable SupervisorOwnedTreeDispatchUseProofV1

EphemeralTerminalFinalActionGateV1 =
  private, nonserializable, noncopyable, deeply immutable, alias-free snapshot {
    dry_run: false
    kill_switch: CLEAR
    action_latch_state: ENABLED
    action_latch_epoch: uint64
  }

EphemeralTerminalOwnedTreeCallV1 =
  private, nonserializable, noncopyable, deeply immutable, alias-free,
  single-use call constructible only from exact request/action provenance for
  COMPLETE | TIMEOUT | FAILED, the exact persisted next_entry, one exact
  EphemeralTerminalFinalActionGateV1, a privately sealed exact target tuple,
  one fresh call_id, and one immutable SupervisorOwnedTreeDispatchUseProofV1

SupervisorOwnedTreeDispatchUseProofV1 =
  private, nonserializable, noncopyable, deeply immutable, alias-free proof
  carrying one fresh use_id, exact call_id, exact dispatch variant, and the
  unexported live dispatcher-instance identity; it grants no native-plan
  authority without the matching atomic owner in CALL

SupervisorOwnedTreeDispatchUseOwnerV1 =
  one private, nonserializable, noncopyable atomic owner cell minted exactly
  once alongside each CONFIGURED_AGENT_RELAUNCH or EPHEMERAL_TERMINAL call by
  that variant's private constructor; it exposes no clone/reset/rearm API and
  stores SupervisorOwnedTreeDispatchUseStateV1 outside every sealed call graph

SupervisorOwnedTreeDispatchPreEffectRejectionV1 =
  PRIVATE_SEAL_OR_OWNER_MISMATCH | CALL_ALREADY_DISPATCHING_OR_CONSUMED
  | VARIANT_PROVENANCE_STALE | TARGET_OR_BINDING_MISMATCH
  | FINAL_ACTION_GATE_CHANGED | DISPATCHER_INSTANCE_MISMATCH
  | ADMISSION_OR_PLAN_HANDOFF_FAILED_NO_EFFECT

SupervisorOwnedTreeDispatchPoisonCauseV1 =
  NATIVE_EFFECT_UNCERTAIN | RECEIPT_CONSTRUCTION_UNCERTAIN
  | RECEIPT_HANDOFF_UNCERTAIN | PLANNER_COMMIT_UNCERTAIN
  | DISPATCH_PROTOCOL_BROKEN

SupervisorOwnedTreeDispatchUseStateV1 =
  CALL {
    use_id: lowercase hyphenated UUID
    call_id: lowercase hyphenated UUID
    variant: CONFIGURED_AGENT_RELAUNCH | EPHEMERAL_TERMINAL
  }
  | DISPATCHING { same use_id; same call_id; same variant }
  | PLAN_OWNED { same use_id; same call_id; same variant }
  | INVOKING { same use_id; same call_id; same variant }
  | RECEIPT { same use_id; same call_id; same variant }
  | CONSUMING_RECEIPT { same use_id; same call_id; same variant }
  | REJECTED_NO_EFFECT {
      same use_id; same call_id; same variant
      reason: SupervisorOwnedTreeDispatchPreEffectRejectionV1
    }
  | POISONED {
      same use_id; same call_id; same variant
      cause: SupervisorOwnedTreeDispatchPoisonCauseV1
    }
  | CLOSED

SupervisorOwnedTreeDispatchCallV1 =
  private, nonserializable, noncopyable, deeply immutable, alias-free,
  single-use closed sum of exactly:
    CHILDLESS(
      ChildlessExternalEffectCallV1(operation = STOP_TREE))
    | CONFIGURED_AGENT_RELAUNCH(ConfiguredAgentOwnedTreeCallV1)
    | EPHEMERAL_TERMINAL(EphemeralTerminalOwnedTreeCallV1)

SupervisorOwnedTreeDispatchSubmissionV1 =
  private, nonserializable, noncopyable opaque submission handle emitted exactly
  once alongside one SupervisorOwnedTreeDispatchCallV1 by its private variant
  constructor; it carries that sealed call plus an unexported reference to the
  exact atomic owner: the call's LiveEffectGuardLineageOwnerV1 for CHILDLESS,
  otherwise its SupervisorOwnedTreeDispatchUseOwnerV1. It is not a sealed value
  graph or an authorization record. Retaining or racing aliases of the same
  handle is explicitly permitted by the threat model and does not duplicate the
  owner state.

SupervisorOwnedTreeDispatchAdmissionV1 =
  private, nonserializable, noncopyable opaque admission handle emitted as part
  of the #146 dispatcher's one exact atomic CALL -> DISPATCHING compare-and-swap
  for one valid SupervisorOwnedTreeDispatchSubmissionV1. It carries a deeply
  immutable admission proof binding the call seal, call_id, variant, target
  tuple, use/lineage identity, and live dispatcher instance plus an unexported
  reference to that exact atomic owner. It is outside every sealed value graph;
  retaining or racing aliases is explicitly permitted by the threat model and
  cannot duplicate owner state.

PrivateSupervisorOwnedTreeNativePlanV1 =
  dispatcher-local deeply immutable leaves-first target tuple plus the exact
  execution mode selected only after one admission alias wins the exact atomic
  owner transition DISPATCHING -> PLAN_OWNED; this value is neither an API type
  nor constructible outside the private dispatcher.

SupervisorOwnedTreeNativeInvocationV1 =
  private, nonserializable, noncopyable opaque invocation handle emitted exactly
  once alongside PrivateSupervisorOwnedTreeNativePlanV1 by the winning
  DISPATCHING -> PLAN_OWNED transition; it pairs that deeply immutable plan with
  an unexported reference to the exact atomic owner. The private native body
  accepts only this handle and must win PLAN_OWNED -> INVOKING before lexical
  raw-array materialization or effect. Retained aliases and replay name the same
  owner and cannot duplicate invocation.

SupervisorOwnedTreeNativeKnownNoEffectV1 =
  private, nonserializable, noncopyable, deeply immutable, alias-free closed
  result:
    ACTIONS_DISABLED_NO_EFFECT {
      exact call_id, use/lineage identity, dispatch variant,
      and live dispatcher-instance identity copied from the winning invocation
    }

SupervisorOwnedTreeDispatchReceiptV1 =
  private, nonserializable, noncopyable, deeply immutable, alias-free result
  bound to the exact consumed native invocation, call, variant, target tuple,
  use/lineage identity, and native outcomes; only CHILDLESS projects into
  ChildlessExternalEffectReceiptV1

SupervisorOwnedTreeDispatchReceiptCustodyV1 =
  private, nonserializable, noncopyable opaque consumption handle emitted
  exactly once alongside one SupervisorOwnedTreeDispatchReceiptV1; it carries
  that sealed receipt plus an unexported reference to the exact atomic owner.
  It is outside every sealed value graph. Retaining or racing aliases is
  explicitly permitted, and all aliases name the same RECEIPT owner state.

ChildlessSafetyReconciliationGateV1 =
  RETAIN_INVALID_FENCE
  | RETAIN_LIVE_CONTINUATION
  | RETAIN_EFFECT_GUARD_UNAVAILABLE
  | MAY_RELEASE_PRE_BARRIER(
      ExactTargetExecutorPermitV1(
        operation = PRE_BARRIER_RELEASE, use = STATE_MUTATION))
  | MAY_TAKEOVER(
      ExactTargetExecutorPermitV1(operation = TAKEOVER, use = STATE_MUTATION))
  | MAY_RECONCILE(
      ExactTargetExecutorPermitV1(
        operation = CLOSURE_RECONCILE | RETIRED_ATTEMPT_RECONCILE,
        use = STATE_MUTATION))
```

Every private transient value above that is described as deeply immutable
carries an unexported construction seal over its complete transitive value. That
seal is object provenance, not a persisted field, not a new digest domain, and
not an authority substitute. Private constructors deep-copy and canonicalize
caller material into primitives, enums, immutable tuples, and final sealed
records before validation. No trusted effect object may retain or expose a
reachable list, dictionary, set, byte-array, mutable record, caller-owned proxy,
writable property, or mutable subclass. A frozen outer record with a mutable
nested value is nonconforming. Consumers revalidate the private seal, exact
lineage/call/use identity, complete canonical content, binding, and target digest
before mutation or effect.

The atomic owner cells together with `SupervisorOwnedTreeDispatchSubmissionV1`,
`SupervisorOwnedTreeDispatchAdmissionV1`,
`SupervisorOwnedTreeNativeInvocationV1`, and
`SupervisorOwnedTreeDispatchReceiptCustodyV1` are the private synchronization
boundary outside every sealed value graph. They are never serialized, hashed,
exposed through a public field, or accepted as evidence. Sealed permits, calls,
plans, and receipts carry only their immutable opaque proof. Each private variant constructor atomically emits
exactly one sealed call and one matching submission handle over exactly one atomic owner;
there is no clone, lookup, reset, or reseal constructor. An external registry,
caller-provided mutex, call-ID set, or dispatcher-global unstated lock cannot
substitute for that per-call owner.

The #146 dispatcher accepts only `SupervisorOwnedTreeDispatchSubmissionV1`.
After validating the handle's private provenance and owner binding, it must win
one atomic admission before native-plan construction: CHILDLESS compares and
moves the exact `LiveEffectGuardLineageOwnerV1` holder `CALL -> DISPATCHING`;
CONFIGURED_AGENT_RELAUNCH and EPHEMERAL_TERMINAL compare and move their exact
`SupervisorOwnedTreeDispatchUseOwnerV1` state `CALL -> DISPATCHING`. Only the
winner receives `SupervisorOwnedTreeDispatchAdmissionV1`. The private
`try_admit` primitive fully constructs a dormant admission candidate first; the
successful compare-and-swap atomically activates and yields that exact candidate
as its linearization result, with no fallible allocation between state change
and result. If lexical result handoff nevertheless fails, positive no-effect
cleanup resolves `DISPATCHING` exactly as the first table row below, or an
uncertain owner transition poisons the exact owner with
`CUSTODY_PROTOCOL_BROKEN` for CHILDLESS and `DISPATCH_PROTOCOL_BROKEN` otherwise.
A concurrent alias,
sequential replay, or stale receipt sees a non-`CALL` state and returns typed
`CALL_ALREADY_DISPATCHING_OR_CONSUMED` with zero native-plan construction and
zero effect. “Noncopyable” does not prevent aliasing the same object; the atomic
transition is the safety boundary.

The winner then revalidates the complete sealed call and variant-specific
provenance while the owner remains `DISPATCHING`. For `EPHEMERAL_TERMINAL` it
acquires the action-latch read guard, requires `ENABLED` with the exact epoch in
`EphemeralTerminalFinalActionGateV1`, and retains that guard through native
issuance. Separately, it freshly requires the kill switch to remain clear before
native-plan construction and preserves the private native body's equivalent
final `Assert-ActionsEnabled`/kill-switch check. The latch guard does not freeze
the kill-switch file, and the two checks are not conflated. There is no wait
between the last checks and issuance. A kill-switch or latch flip after call
construction but before dispatch therefore yields
`FINAL_ACTION_GATE_CHANGED` before any native plan or effect; persisted
`next_entry` remains unchanged. After validation and these outer gates, aliases
of the one admission must race the same exact owner transition
`DISPATCHING -> PLAN_OWNED`. Only its compare-and-swap winner may construct one
`PrivateSupervisorOwnedTreeNativePlanV1` and its matching
`SupervisorOwnedTreeNativeInvocationV1`; every losing or replayed admission
constructs no plan and produces zero effect.

The preserved private-native-body check runs before lexical raw-array
materialization. The private body first races every alias of the invocation
handle through `PLAN_OWNED -> INVOKING`; only that winner may run the check or
materialize the plan. If the kill switch changes after the outer fresh check or
after native-plan construction but before that inner check, the body returns the
private typed `SupervisorOwnedTreeNativeKnownNoEffectV1` outcome
`ACTIONS_DISABLED_NO_EFFECT` bound to the exact invocation.
It materializes no raw array and attempts no native effect. The dispatcher uses
that positive no-effect result to move CHILDLESS custody from `INVOKING` to
one new `AVAILABLE` proof, or to move either non-childless owner to
`REJECTED_NO_EFFECT(FINAL_ACTION_GATE_CHANGED)`, exactly once. For
EPHEMERAL_TERMINAL, persisted `next_entry` remains unchanged. No branch treats
an empty/implicit return as a normal effect result.

Dispatch resolution is total:

| Dispatch exit | Exact atomic-owner disposition |
| --- | --- |
| Admission handoff failure, full validation rejection, or final-gate rejection before `DISPATCHING -> PLAN_OWNED` | Consume the admitted call. CHILDLESS moves `DISPATCHING` to one new `AVAILABLE` lineage custody proof only with positive no-effect proof; either non-childless owner moves to terminal `REJECTED_NO_EFFECT(reason)`. Return a typed rejection and no receipt. An uncertain owner transition maps to childless `CUSTODY_PROTOCOL_BROKEN` or non-childless `DISPATCH_PROTOCOL_BROKEN`. |
| Admission alias loses `DISPATCHING -> PLAN_OWNED`, or invocation alias loses `PLAN_OWNED -> INVOKING` | Reject with no owner change, plan/raw-array construction, mutation, or effect. Sequential replay has the same result. |
| Plan/invocation-handle construction or handoff fails after `PLAN_OWNED` but before native entry | With positive no-effect proof, move CHILDLESS to one new `AVAILABLE` proof or either non-childless owner to `REJECTED_NO_EFFECT(ADMISSION_OR_PLAN_HANDOFF_FAILED_NO_EFFECT)`. If exact owner resolution is uncertain, poison as childless `CUSTODY_PROTOCOL_BROKEN` or non-childless `DISPATCH_PROTOCOL_BROKEN`. |
| Private native-body final kill-switch rejection after `PLAN_OWNED -> INVOKING` but before lexical raw-array materialization | Return invocation-bound typed `ACTIONS_DISABLED_NO_EFFECT`. Move CHILDLESS custody to one new `AVAILABLE` proof or either non-childless owner to `REJECTED_NO_EFFECT(FINAL_ACTION_GATE_CHANGED)` exactly once. Produce no receipt, raw array, or native effect; retain ephemeral `next_entry`. |
| Native invocation returns a typed outcome | Fully construct a dormant sealed matching receipt plus `SupervisorOwnedTreeDispatchReceiptCustodyV1`, then atomically move the same owner `INVOKING -> RECEIPT` and yield that exact custody handle. No fallible allocation occurs between the transition and yield. The sealed receipt alone is inert and is not accepted by a consumer. |
| Native effect or native-return status becomes uncertain | Move CHILDLESS to `POISONED(ADAPTER_EFFECT_UNCERTAIN)` or either non-childless owner to `POISONED(NATIVE_EFFECT_UNCERTAIN)` exactly once. CHILDLESS retains its persisted effect fence; non-childless recovery follows its existing conservative planner state. No replay or rearm is permitted. |
| Receipt construction or handoff becomes uncertain after native return | Move CHILDLESS to `POISONED(RECEIPT_HANDOFF_UNCERTAIN)`; move either non-childless owner to `POISONED(RECEIPT_CONSTRUCTION_UNCERTAIN)` or `POISONED(RECEIPT_HANDOFF_UNCERTAIN)` at the exact failing boundary. Never return a second receipt or infer no effect. |
| Matching CHILDLESS receipt is consumed normally | The existing receipt-mutation constructor accepts only the matching `SupervisorOwnedTreeDispatchReceiptCustodyV1`, validates its sealed childless projection and owner binding, and atomically moves the exact lineage owner `RECEIPT -> PERMIT`; only its winner proceeds. Concurrent handle aliases and sequential replay produce zero mutation and zero effect. A synchronous failure after that CAS but before mutation poisons with `CUSTODY_PROTOCOL_BROKEN`; an uncertain checked commit poisons with `OWNER_COMMIT_UNCERTAIN`. Neither returns to `RECEIPT`, and the persisted effect fence remains. |
| Matching configured-agent or ephemeral receipt is consumed | The private planner continuation accepts only the matching `SupervisorOwnedTreeDispatchReceiptCustodyV1` and validates receipt/use/call/variant/owner equality. Before any existing planner behavior, exactly one consumer atomically moves its owner `RECEIPT -> CONSUMING_RECEIPT`. Only that winner applies the existing behavior, then moves `CONSUMING_RECEIPT -> CLOSED`. A synchronous failure before planner behavior moves to `POISONED(DISPATCH_PROTOCOL_BROKEN)`; an uncertain behavior or commit moves to `POISONED(PLANNER_COMMIT_UNCERTAIN)`. Neither returns to `RECEIPT`. Concurrent handle aliases and sequential replay produce zero mutation, launch, or effect. |
| Any duplicate cleanup or transition from a nonmatching state | Reject idempotently with no state change, native-plan construction, mutation, or effect. |

Only the `DISPATCHING -> PLAN_OWNED` winner over one
`SupervisorOwnedTreeDispatchAdmissionV1` may construct
`PrivateSupervisorOwnedTreeNativePlanV1` and its opaque invocation handle. Only
the invocation handle's `PLAN_OWNED -> INVOKING` winner may enter the sole
supervisor owned-tree native body, where the immutable plan tuple may be
materialized as an unaliased lexical raw array for the final native call. A caller-settable
discriminant, a wrapper around `kill_targets`, a
planner-produced array, or a field-equivalent copied record is inert evidence,
not authorization. The childless variant requires the permit-bound typed call.
The other two variants preserve independently authorized non-87-A behavior and
cannot grant, imply, reuse, or satisfy childless authority. The phrase “sole
native body” is scoped to the supervisor owned-tree executor in
`supervisor.py`; the separate `turn_watchdog.py` kill facility has its own
policy and is outside 87-A and #146.

`ExactTargetExecutorBindingV1` is inert persisted evidence. It names the only
executor contract that may act on the exact owner/target tuple, but it grants
no authority and cannot mint a witness or permit. The existing seven digest
domains remain unchanged: the binding references their recomputed identifiers
rather than adding fields to any banked payload.
`executor_contract="stop-tree/windows-filetime/v1"` identifies the exact
FILETIME effect semantics; it is not a public function signature and does not
authorize raw `Stop-Tree` invocation.

`CurrentExactTargetExecutorWitnessV1` exists only in the current process after
#146's private capability factory has established the current conforming
dispatcher installation and before any permit or call is constructed. This is
not circular: the capability factory creates the witness; the permit constructor
consumes its provenance plus the exact inert binding; the childless call carries
that permit; and the dispatcher accepts only the private submission enclosing
that resulting closed variant, and only when the permit's unexported
dispatcher-instance identity matches its own. Parser,
snapshot, target constructor, persisted platform text, deserialized state, the
merged raw `Stop-Tree` function, and either non-childless dispatcher variant
cannot produce a witness. Merged #120's Windows exact-FILETIME branch at
`src/agenttalk/supervisor.py:8900-8928` supplies the required target-local native
semantics but cannot back the V1 witness while the raw entry remains. The
non-Windows skip at `8930-8932` supplies neither semantics nor witness. Until
#146 lands and its direction controls pass, witness construction returns
`CAPABILITY_UNAVAILABLE(DISPATCHER_SEAL_UNDELIVERED)`. A future executor requires
a separately reviewed versioned contract.

The permit constructor compares every binding field. Except for the closed
targetless cleanup scopes below, it also recomputes the exact owner and
authorized-target digest from the displayed tuple and proves any
operation-specific residual tuple is an exact ordered subset of that immutable
authorization. A targetless scope instead proves its complete typed subject
against the exact historical tombstone or current envelope binding. Every
permit binds the current checked revision and is scoped to one operation and
one use. One successful effect-guard acquisition creates exactly one
`LiveEffectGuardLineageV1` in `AVAILABLE`. A guard-required permit constructor
atomically consumes its sole custody token and moves that lineage to
`OUTSTANDING(PERMIT, issuance_id)`. While the lineage is `OUTSTANDING`,
`POISONED`, or `CLOSED`, a second issuance, permit, call, or receipt lineage cannot
be minted from that acquisition. Every later stage moves the same
`(lineage_id, issuance_id)` custody; no stage observes the handle and creates a
replacement token.

Custody resolution is total:

| Exit | Exact lineage disposition |
| --- | --- |
| Mutation permit commits with a known result | Move custody through `OWNER_COMMIT`, consume the permit, and return exactly one successor custody token in `AVAILABLE`. |
| Mutation commit outcome is uncertain | Atomically enter `POISONED(OWNER_COMMIT_UNCERTAIN)` exactly once. |
| External call is constructed | Move custody `PERMIT -> CALL`; exactly one matching dispatcher submission exists, and call construction alone cannot create a native plan. |
| Synchronous adapter admits the external call | Exactly one atomic `CALL -> DISPATCHING` compare-and-swap wins and atomically yields one admission before any external-effect plan. For `STOP_TREE`, #146 performs this through the private submission; every other childless adapter performs the equivalent private transition. Every concurrent alias or sequential replay loses with zero effect. |
| Admitted call is prepared for effect | Exactly one admission alias moves `DISPATCHING -> PLAN_OWNED` before any plan exists; exactly one invocation alias moves `PLAN_OWNED -> INVOKING` before native entry. Losing/replayed aliases construct no plan or effect. Non-`STOP_TREE` adapters use equivalent private owner stages even when they do not publish the dispatcher-specific type names. |
| Invoked call validates and returns normally | Move custody `INVOKING -> RECEIPT`; only the matching sealed receipt owns it. |
| Admitted or invoked synchronous adapter rejects before the effect is positively proved to have begun | From `DISPATCHING`, `PLAN_OWNED`, or `INVOKING`, consume the call and return exactly one successor custody token in `AVAILABLE`; the old call/admission/invocation proofs and every alias remain consumed. |
| Adapter throws or loses receipt handoff after the effect may have begun | Atomically enter `POISONED` from the exact current holder using only a `LiveEffectGuardLineageStateV1` cause: `ADAPTER_EFFECT_UNCERTAIN`, `RECEIPT_HANDOFF_UNCERTAIN`, or `CUSTODY_PROTOCOL_BROKEN`. Never return custody or infer no effect. |
| Receipt-mutation permit commits with a known result | Move custody `RECEIPT -> PERMIT -> OWNER_COMMIT`, consume the receipt and permit, and return exactly one successor token in `AVAILABLE`. |
| Receipt commit is uncertain or its checked envelope changed incompatibly | Atomically enter `POISONED` and retain the persisted effect fence. |

Every exit returns or poisons custody exactly once, never both and never
neither. Repeating an exception cleanup or consuming a stale holder is rejected
idempotently. A poisoned lineage cannot mint, return, or clear authority; the
guard must unwind and release, a later acquisition creates a new lineage, and
persisted `ARMED`/`CALL_RETURNED` recovery remains conservative and never
reissues the uncertain effect. Call, admission, and invocation aliasing are not
lineage copying: all aliases name the same atomic owner, so only one can leave
each effect-bearing holder state.

No unconsumed permit crosses a checked commit. Every external call therefore
uses three sequential, distinct permits under one lineage: a `STATE_MUTATION`
permit arms the exact checked continuation; only after that permit returns
custody may an `EXTERNAL_CALL` permit at the successor revision construct and
invoke the call; only by consuming the matching receipt-held custody may a
distinct `RECEIPT_MUTATION` permit at the then-current revision apply it while
preserving the call issuance. An ordinary
observation CAS may intervene only when the effect envelope and continuation
remain exact; the receipt-mutation constructor rechecks both and binds the new
revision. The already-invoked call/receipt lineage may span only those
observation revisions. The live-scope operand is a closed discriminated value:

- `RESERVE` permits only `STATE_MUTATION` and has two closed current-state
  scopes. `INITIAL` requires top-level execution `IDLE` and creates the first
  envelope/binding. `CONTINUE` requires an existing envelope whose execution is
  `IDLE`, with no current attempt, closure, pending disposition, continuation,
  or `RELEASE_PENDING` tombstone. For initial-mode retry with debt `NONE`, it
  may atomically replace the envelope's current binding with the prospective
  fresh tree binding only when the prospective owner identity exactly equals
  the old binding owner. It preserves the same-owner cycle and terminal
  tombstones under their own historical bindings. A physically different owner
  must first use `OWNER_TRANSITION` and then `RESERVE/INITIAL`; `CONTINUE` cannot
  bypass that boundary. For `DEBT_COMPLETION`, the prospective
  binding must equal the immutable envelope/debt binding and its targets must be
  the exact authorized residual subset. Both scopes install `PRE_BARRIER`; no
  other operation may create or renew a reservation.
- `PRE_BARRIER_RELEASE` requires a valid `PRE_BARRIER` envelope with null
  attempt, closure, continuation, spawn guard, and deadline. It is a state-only
  `STATE_MUTATION`; it does not imply an external effect.
- `TAKEOVER`, every closure operation, `RETIRED_ATTEMPT_RECONCILE`,
  `STOP_TREE`, `POST_ACTION_CAPTURE`, `EFFECT_FINALIZE`, `SPAWN`,
  `SPAWN_RESULT_COMMIT`, and `SPAWN_IDENTITY_COMMIT` require the current caller
  to hold the nonserializable effect guard. Their permits move the same unique
  lineage custody; the spawn call moves that custody into its typed call and
  receipt exactly as every other external call does. First
  `CLOSURE_ACQUIRE` binds a proposed issuer continuation to the exact
  reservation, guard owner, and expected successor revision; its permit-bound
  mutation must install that exact continuation. Later operations require the
  exact current checked continuation, tombstone, or receipt stated below, or a
  proposed replacement continuation derived from it by the closed transition.
  `TAKEOVER` additionally requires positive predecessor-unwind or
  PID/start-death proof and authorizes only its no-call `STATE_MUTATION`. That
  mutation preserves the predecessor operation as evidence but changes its
  stage to inert `TAKEOVER_CHECKPOINT`; it never writes `ARMED` for any later
  operation.
- A call-bearing `STATE_MUTATION` installs exactly one `OWNED/ARMED`
  continuation. Its subject is `ACTIVE_ATTEMPT` for closure, `Stop-Tree`, and
  post-action capture; `RETIRED_ATTEMPT` for retired reconcile/release; or
  `SPAWN_RESERVATION` for spawn. The post-CAS `EXTERNAL_CALL` permit requires
  that exact subject, operation, `ARMED` stage, `armed_state_revision`, and live
  guard. No raw ID, tombstone, reservation, or receipt can construct it. An
  `OWNED/ARMED` continuation for operation `O` is constructible only by an
  `O/STATE_MUTATION` permit. Applying operation `P`'s receipt may write only
  `P/CALL_RETURNED` or the closed receipt-successor state allowed by the table
  below; it may not arm a different operation. Chaining to a later call
  therefore requires a new checked commit with that later operation's own
  `STATE_MUTATION` permit.
- `EFFECT_FINALIZE` has one additional debt-only scope: the exact current
  envelope is childless `IDLE`, has no closure, pending disposition,
  continuation, or debt current attempt, and the current ordinary residual is
  matching `COMPLETE_GONE`. The caller still holds the transient effect guard;
  the permit authorizes only the atomic debt/cycle clear plus same-poll terminal
  and constructs no external call or launch.
- `SPAWN` requires an exact transient live-action scope plus the pre-spawn
  barrier result. Its `STATE_MUTATION` enters `SPAWN_IN_FLIGHT` and installs an
  exact `SPAWN_RESERVATION/SPAWN/ARMED` continuation; only a fresh post-CAS
  `EXTERNAL_CALL` permit can construct `Start-Process`. A synchronous
  `SPAWN_RESULT_COMMIT` or `SPAWN_IDENTITY_COMMIT` requires the matching typed
  spawn receipt and a fresh receipt-derived `RECEIPT_MUTATION` permit that
  preserves the call issuance.
  The only receipt-free result scope is `CRASHED_DURING_SPAWN`, which requires
  a fresh witness, the exact inert `SPAWN_IN_FLIGHT` envelope, the reacquired
  effect guard, and positive proof that the recorded issuing process/start
  cannot resume. Its persisted continuation must be either the exact dead-
  issuer `SPAWN_RESERVATION/SPAWN/ARMED` predecessor or the exact table-
  authorized `SPAWN_RESERVATION/SPAWN/TAKEOVER_CHECKPOINT/RECONCILER` derived
  from that predecessor. It is a `STATE_MUTATION`, not a receipt mutation.
  `SPAWN_IDENTITY_COMMIT` also
  requires the exact guarded identity checkpoint returned for that spawn.
  Two other no-receipt state-only scopes are closed: a later strict checkpoint
  may construct `SPAWN_IDENTITY_COMMIT/STATE_MUTATION` only when it exactly
  matches the guard retained in `AMBIGUOUS_LAUNCH`; and two compatible ordinary
  absence captures strictly after the ambiguity boundary may construct
  `SPAWN_RESULT_COMMIT/STATE_MUTATION` only to resolve that exact envelope to
  `IDLE` without launch. None may be built from a persisted
  `SPAWN_IN_FLIGHT`/`AMBIGUOUS_LAUNCH` value alone.
- `OWNER_TRANSITION` is state-only. It requires an exact fresh guarded identity
  checkpoint for a physically different owner, quarantine `NONE`, debt `NONE`,
  childless execution `IDLE`, no continuation or retired cleanup obligation,
  and a witness matching the old envelope binding. It may clear only that old
  owner's cycle/envelope while committing the new guarded owner; it cannot kill
  or launch.

The only targetless old-side permit scopes are: `RESERVE/CONTINUE` changed-tree
rebind over the exact old envelope binding plus same owner, no debt/current
obligation/continuation/`RELEASE_PENDING`, and the complete fresh prospective
owner/target tuple; takeover/reconcile/release/finalization of a typed retired-
attempt subject whose tombstone carries the exact historical binding; and
`OWNER_TRANSITION` over the exact old envelope binding. These
scopes still require a matching current-host witness, checked revision, effect
guard when applicable, and their complete typed subject/receipt/checkpoint.
They cannot construct `ExecutableOwnedTargetSetV1`, rewrite authorization or
debt, or act on a raw digest. Every other operation recomputes the complete
owner/authorized-target tuple and residual subset when applicable.

There is no generic null-scope fallback. Witness preflight runs before permit or
provider construction. Absence of a fresh current-host witness returns the exact
`CurrentExactTargetExecutorWitnessConstructionV1` reason:
`CAPABILITY_UNAVAILABLE(EXACT_TARGET_EXECUTOR_UNAVAILABLE)` when no conforming
exact executor exists for the host/binding, or
`CAPABILITY_UNAVAILABLE(DISPATCHER_SEAL_UNDELIVERED)` when the required #146
dispatcher seal is not delivered. A stale checked revision returns
`REJECTED(STALE_CHECKED_REVISION)` and must reload/re-reduce.
A copied, forged, replayed, consumed, wrong-use, mismatched, or unsupported live
scope returns the corresponding `REJECTED` value; malformed checked evidence is
handled by the earlier invalid-fence row. Every non-permitted result constructs
no permit, mutation, or call. Only static witness/executor inability is rendered
as operator-facing capability unavailability. A permit is neither serializable
nor reusable after its operation/use,
checked revision, target/residual tuple, effect-guard ownership, continuation,
receipt, or identity checkpoint changes. `ClosureCapabilityV1.AVAILABLE`
carries only the `RESERVE` permit; it is consumed by the selected initial or
continuing reservation mutation and is never persisted or reused for
acquisition.

The `ClosureCapabilityV1` constructor first consumes witness preflight. A
preflight failure returns the corresponding exact one-element reason tuple—
`(EXACT_TARGET_EXECUTOR_UNAVAILABLE)` or
`(DISPATCHER_SEAL_UNDELIVERED)`—before permit or closure-provider predicates and
does not inspect downstream successor reasons because that boundary is
unreachable. Only an `AVAILABLE` witness may construct the fresh unconsumed
`RESERVE/STATE_MUTATION` permit; the capability constructor then takes ownership
of and embeds that permit before any closure-provider predicate, and only its
checked reservation mutation consumes it. A permit-construction `REJECTED`
result is not a capability result and follows its reload/reject rule. Only the
witness-and-permit-admitted constructor collects successor reasons or produces
`AVAILABLE`. Under merged #120, every non-Windows named-teardown path fails
preflight with `EXACT_TARGET_EXECUTOR_UNAVAILABLE`, while a Windows path fails
with `DISPATCHER_SEAL_UNDELIVERED` until #146 lands; Linux's admitted observation
token does not change that fact.

The receipt-predecessor relation is closed:

| Receipt operation | Permitted receipt mutation |
| --- | --- |
| `CLOSURE_ACQUIRE` | `CLOSURE_ACQUIRE` to bind/persist the exact result, or `EFFECT_FINALIZE` only for the event table's matching terminal `NEVER_ACQUIRED`/`RELEASED` case. |
| `CLOSURE_RECONCILE` | `CLOSURE_RECONCILE` to persist the exact same-operation `CALL_RETURNED` checkpoint, or `EFFECT_FINALIZE` only for an event-table terminal result. That terminal scope includes exact matching `RELEASED` after persisted `STOP_TREE/CALL_RETURNED`: it finalizes conservatively as `EFFECT_UNPROVEN`, retains debt, and performs no residual-capture call. A later operation's own state-mutation permit performs any next arm. |
| `CLOSURE_RELEASE` | `CLOSURE_RELEASE` to persist the same-operation `CALL_RETURNED` checkpoint for matching `HELD`/`UNKNOWN`, or `EFFECT_FINALIZE` for exact `RELEASED` on the active disposition or retired cleanup subject. A later retry begins with its own `CLOSURE_RECONCILE/STATE_MUTATION` arm. |
| `RETIRED_ATTEMPT_RECONCILE` | `RETIRED_ATTEMPT_RECONCILE` to restore terminal state or persist `RELEASE_PENDING` plus the reconcile `CALL_RETURNED` checkpoint. Exact release is armed only by a later `CLOSURE_RELEASE/STATE_MUTATION` permit. |
| `STOP_TREE` | `STOP_TREE` to record `CALL_RETURNED`; it cannot finalize the effect. |
| `POST_ACTION_CAPTURE` | `POST_ACTION_CAPTURE` to map the typed observation and persist its `CALL_RETURNED` release-required checkpoint. Exact closure release is armed only by a later `CLOSURE_RELEASE/STATE_MUTATION` permit. |
| `SPAWN` | Exactly one of `SPAWN_RESULT_COMMIT` or `SPAWN_IDENTITY_COMMIT`, according to the typed result and guarded checkpoint. |

Every other operation pair, a receipt whose call permit/binding/call ID does
not match, or a receipt applied after the envelope/continuation changed is
rejected before mutation.

`ChildlessEffectEnvelopeV1` is the only persisted home for childless-owned
effect state. The core represents execution as `IDLE`, a non-childless value,
or `CHILDLESS(ChildlessEffectEnvelopeV1)`; it never persists a childless
reservation or phase standalone. `ChildlessRecoveryExecutionV1` includes its
own `IDLE`, every named reservation phase through `SPAWN_IN_FLIGHT`, and
`AMBIGUOUS_LAUNCH`. The envelope remains present with childless execution
`IDLE` while debt, an automatic cycle, a continuation, or a retired attempt
remains. Every target tuple, attempt, closure, debt, continuation, pending
disposition, and active cycle attempt inside it must equal its current binding.
A terminal retired attempt retains the historical binding under which it was
issued and may differ after a safe initial-mode retry rebind; its cleanup uses
only that tombstone binding. A `RELEASE_PENDING` tombstone forbids
reservation/rebind. Any other mismatch is a malformed envelope and grants no
permit.

The checked-state owner accepts no raw write to this envelope. It accepts only
a `PermitBoundChildlessMutationV1` whose expected revision and permit still
match, except for the two explicitly non-effect mutation classes below:
the core's owner-private ordinary-observation mutation, derived only by #115
from one sealed receipt and unable to address the envelope,
and creation of an unresolved state-loss quarantine. Consequently a future
childless phase automatically inherits the same construction boundary. It may
deserialize evidence inside the envelope, but without a matching fresh witness
no **87-A childless** executor-dependent external effect and no childless
authority-enabling or effect-owned mutation is constructible. In particular, it
cannot construct an executable target, reservation, permit-bound mutation,
call, receipt, launch, or identity commit. Independently, no supervisor
owned-tree native termination is reachable except from one exact
`SupervisorOwnedTreeNativeInvocationV1` that won admission, plan ownership, and
native entry over a private submission/owner pair. A non-childless variant carries
its own checked provenance and neither grants nor satisfies childless authority.
Revision 12 specifies both rules, but merged code does not enforce the second:
raw `Stop-Tree($targets)` and its two direct planner calls remain until #146.

`OwnedTreeTargetV1`, `ChildlessTeardownAuthorityV1`,
`ChildlessReservationEvidenceV1`, closure values, observations, and every
persisted envelope member are inert evidence. `ExecutableOwnedTargetSetV1` is
accepted only by the private constructor for the dispatcher's `CHILDLESS`
variant; raw `Stop-Tree` accepts no conforming 87-A input.
`ChildlessExternalEffectCallV1` is the only input accepted by every childless
closure, teardown, post-action capture, and spawn adapter. The owned-tree
dispatcher separately accepts only the private submission enclosing its closed
three-variant sum. Those adapters return a matching
`ChildlessExternalEffectReceiptV1`; raw IDs, raw target tuples, persisted
state, and untyped provider results are rejected at the API boundary. A
receipt changes checked state only through a matching
`PermitBoundChildlessMutationV1` in the same guarded operation.
Only the invoked adapter may construct its receipt, and only as the synchronous
result of a valid typed call; a decoder or ordinary reducer cannot construct a
receipt. Module-private constructors enforce these boundaries. Public parsing
and serialization APIs expose inert evidence constructors only. Every consumer
also rejects a missing/private-seal mismatch, a mutable reachable member, a
source-alias substitution, and an altered or field-equivalent copied graph
before owner mutation or native effect.

For ordinary input, `capture_id` is the core #115 begin-bound prospective
ordinal-zero ID. Before commit, this module validates it against the sealed
receipt inside `OrdinaryObservationCommitCustodyV1`, its exact owner binding,
the checked predecessor, and candidate
`PrivateClassifierObservationMutationV1`; after commit, the owner may validate
the same ID against the committed successor. This module may not construct,
replace, reseal, or restamp it.

Every `COMPLETE`, `COMPLETE_GONE`, or `COMPLETE_RESIDUAL` ordinary tree/debt
observation is well formed only when all of these hold:

```text
capture_id == receipt.prospective_capture_id
capture_id == candidate PrivateClassifierObservationMutationV1.capture_id
capture_id.state_epoch == checked predecessor ClassifierStateV1.state_epoch
capture_id.agent_key == checked predecessor ClassifierStateV1.agent_key
capture_id.ordinary_poll_sequence == displayed ordinary_poll_sequence
displayed ordinary_poll_sequence ==
  candidate PrivateClassifierObservationMutationV1.ordinary_poll_sequence
candidate ordinary_poll_sequence ==
  checked predecessor ordinary_poll_sequence + 1 using checked uint64 addition
capture_id.capture_ordinal == 0
after successful commit,
  successor ordinary_poll_sequence == capture_id.ordinary_poll_sequence
```

Any mismatch returns `INCOMPLETE(CAPTURE_ID_MISMATCH)` and cannot confirm child
absence, construct initial/residual authority, or clear debt. A copied prior
capture ID paired with a rewritten current separate sequence is therefore
incomplete, not fresh evidence.

`owner_identity_id` is SHA-256 over
`agenttalk.supervisor.owned-wrapper-identity.v1\0` plus `CanonicalJsonV1` of
the complete owner object. Every `target_digest` in this module is an
`OwnedTargetDigestV1`: SHA-256 over
`agenttalk.supervisor.owned-targets.v1\0` plus `CanonicalJsonV1` of exactly
`{"owner_identity_id": <Hex64>, "schema": "owned-targets/v1",
"targets": <the displayed ordered tuple>}`. The initial complete tree,
residual observation, held closure, authority, reservation, and debt each
recompute that formula over their own displayed tuple. Equality is equality
of both the tuple and its recomputed digest; a copied digest never stands in
for the tuple. A residual digest therefore differs whenever its exact live
subset differs from the immutable authorized tuple.
`process_source_digest` is SHA-256 over
`agenttalk.supervisor.owned-tree-coverage-source.v1\0` plus the exact
`CanonicalJsonV1` bytes of the core's `ObserverCoverageSignatureV1`; it
identifies coverage semantics, not changing process contents.
The 87-A adapter owns `observer_version`. For the admitted Windows mapping
pinned above it is
exactly `win-tree/v2`, which binds merged task #120 at `587e7c1`, including its
`schema_version`, `attribution_model`, exact-FILETIME kill projection,
same-handle exact-check/termination plus conditional bounded wait attempt,
recycle-aware enumeration/ownership algorithm, and pinned implementation
revision. This replaces the pre-review `win-tree/v1`
mapping; the chained module vectors below are renewed accordingly. Any later
change to those inputs requires a different version value and renewed
vectors/review. Revision 9 first narrowed platform admission before target
construction, and later revisions preserve that narrowing without changing the
admitted Windows mapping or its fixed vector. 87-A fixes
`ownership_rule_version` to `owned-tree/v2` and the
core coverage's `pid_start_guard_schema` to `2` for the exact-FILETIME mapping;
none of those values may be omitted or privately defaulted.

`parent_pid`/`parent_start_guard` record only an owned-parent edge at initial
authorization, never the wrapper's external supervisor/console parent. In an
`INITIAL` tuple, the wrapper is the unique depth-zero target with null parent
fields regardless of its live external OS parent. A non-root member whose
guarded live owned parent is in the set records that parent and has parent
depth plus one. A positively proven owned orphan whose owned parent exited
before the initial capture has null parent fields and depth one. Any other
non-root live OS parent outside the owned set, a cycle, conflicting parent
rows, a duplicate guarded identity, or more than 64 targets is incomplete.
The initial tuple sorts by depth, then PID, then ordinal start guard. Image and
command-line text do not participate.

**SPECIFIED positive owner join; adapter not delivered:** `OwnedWrapperIdentityV1` is constructed only
when all of these independently captured values are present and satisfy the
displayed relation:

```text
checked managed agent/root == strict runtime agent/root == requested agent/root
checked managed wrapper PID == strict runtime wrapper PID == observed root PID
start_anchor := observed root reported start token
StartRepresentationMatchV1(checked managed wrapper start token, start_anchor)
StartRepresentationMatchV1(strict runtime wrapper start token, start_anchor)
Windows observed root exact start guard == fresh #120 exact start_filetime
checked managed wrapper generation
  == strict runtime wrapper generation == runtime_wrapper_generation
checked managed launch nonce == parsed observed-root launch nonce
```

`StartRepresentationMatchV1(a, anchor)` uses exact byte equality first; ISO
tokens may represent the anchor's same instant within the shipped
one-millisecond Windows representation tolerance; non-ISO tokens still require
byte equality. Every representation is checked independently against the one
named observed-root anchor—no chained or transitive inference is permitted. It
joins the checked/runtime/CIM representations but grants no destructive
authority. `wrapper_start_token` retains that anchor. On Windows, the retained
`wrapper_start_guard` and every target/parent start guard are the positive
decimal exact FILETIME from the complete #120 row and fresh exact probe. A
rounded ISO token never substitutes for that guard.

The checked managed identity also supplies `state_epoch`,
`managed_generation`, and the expected launch nonce; the strict runtime record
supplies the independently validated agent/PID/start-representation/generation
binding. The shipped strict runtime record has no launch-nonce field and is not an unstated
nonce operand. The observed root is the exact guarded row in the same complete
raw capture used for tree membership. The root target's PID, exact start guard, and
owner nonce equal those joined values. `nonce_provenance` retains both actual
nonce sources and their fixed parser schema; each retained token equals the
top-level `launch_nonce`. Every other target is bound by task #120 to that
exact root and carries the same nonce.

Only a Windows observation with a positive exact FILETIME for every live row
may enter this positive owner/target join. A well-formed Linux exact token
remains valid #120 observation input; other non-Windows platforms have no
admitted exact-token mapping. In either case the static capability gate returns
`CAPABILITY_UNAVAILABLE(EXACT_TARGET_EXECUTOR_UNAVAILABLE)` before this join.
The raw observation remains available to no-kill/barrier consumers; the named
destructive constructor is not invoked.

The observed-root nonce parser reuses the shipped strict
`--supervisor-launch-nonce` grammar: exactly one top-level option in either
`--supervisor-launch-nonce TOKEN` or
`--supervisor-launch-nonce=TOKEN` form, where `TOKEN` is ASCII
`[A-Za-z0-9_-]{16,128}`. Absence, duplication, malformed text, an option
after the wrapper subcommand/tail boundary, unreadable command line, or any
equality failure returns the applicable `INCOMPLETE` reason.
A process-name, executable, or free-form `CommandLine -match` result is never
an input to this join.

The nonce equality is independent from the three-way runtime identity join,
not a fabricated third nonce source. Removing either actual nonce source,
changing either token, changing the parser schema, or failing any
agent/PID/start-representation/exact-guard/generation operand returns
`INCOMPLETE`; an implementation may
not collapse provenance to the already-normalized top-level value.

Every target's `owner_launch_nonce` equals the positively guarded wrapper's
nonce. This does not claim that each descendant repeats the nonce in its
command line; task #120's complete ownership mechanism binds the descendant to
that owner.
For a Windows owned-tree target, the `Stop-Tree` projection is exactly
`{pid: target.pid, start: <fresh validated #120 row.start>, start_filetime:
target.start_guard, reason: "owned_process_tree", source:
"owned_process_tree"}` in root-first order. The rounded `start` is retained
only because the shipped executor's closed target shape requires it;
`start_filetime` is the destructive identity. The existing primitive reverses
the list, so leaves are attempted before the wrapper; every owned Windows target
missing its exact FILETIME is refused rather than falling back to the legacy
rounded `Proc-Start`/`Stop-Process` path. No non-Windows 87-A target projection
exists: at merged `587e7c1`, `Stop-Tree` executes the exact check/termination
only at `src/agenttalk/supervisor.py:8900-8928` and skips an
`owned_process_tree` target with no FILETIME at `8930-8932`.

`COMPLETE_RESIDUAL` is not an initial tree with its root omitted. The 87-A
residual adapter over #120's retained snapshot and one fresh complete process
capture must positively prove that every still-live owner member is an exact
order-preserving subset of the debt's immutable authorized tuple, that every
omitted authorized PID/exact-start-guard is gone, and that no new owner member
exists. Its
recorded owner and nonce come from the checked debt; every live target retains
that nonce and its complete authorization-time target object. Residual
parent/depth fields are never recomputed: a surviving depth-three target whose
authorized parent and grandparent exited keeps its original guarded parent
fields and depth even though those ancestor targets are omitted. The initial
orphan/depth and outside-parent rules therefore do not rewrite a residual
tuple. The wrapper may be absent. `COMPLETE_GONE` proves the same universe
empty. If the adapter cannot retain or reconstruct that ownership fact after
the root exits, it returns `INCOMPLETE`.

### Meaning of COMPLETE and HELD

**SPECIFIED; #120 input delivered and the 87-A adapter not delivered:**
`COMPLETE` means the capture accounts for every
process the guarded wrapper owns under one explicit coverage signature. It is
not “all rows that happened to be readable.” An implementation that cannot
prove its universe complete returns `INCOMPLETE`.

**SPECIFIED; closure successor not delivered:** `HELD` is an action-scoped, non-destructive
closure, idempotently acquired/reconciled by its caller-supplied
`acquisition_id`, with one linearization point before its capture. `INITIAL`
has null debt fields and the complete wrapper-rooted tuple.
`DEBT_COMPLETION` has non-null debt fields and the exact complete residual
tuple, whose root may already be gone. From the linearization point until
release:

- no member of this owner can create or admit a new owned process;
- every already-admitted member is included in the returned complete tuple;
- a target may exit naturally, but no PID/start identity may be silently
  replaced or added;
- loss, crash, timeout, or unverifiable release yields `BLOCKED` and no launch;
- acquiring, holding, or releasing the closure never terminates an authority
  target.

The closure-successor seam exposes one process-version-stable
`ClosureCapabilityV1` before reservation, then acquire, reconcile, and release
keyed by `acquisition_id`; release additionally requires the exact
`closure_id`. `AVAILABLE` exposes one exact `ClosureProviderVersionV1` and a
live one-shot `RESERVE` permit. It is well formed only for an installed,
independently reviewed implementation that can prove this contract inside the
absolute dependency-plane constraint for every case it accepts **and** for a
current witness from #146's private dispatcher-capability factory whose native body
actually acts on the exact target
token. A valid observation token with no conforming platform/binding executor
returns `EXACT_TARGET_EXECUTOR_UNAVAILABLE`; a Windows token whose target-local
semantics exist but whose #146 dispatcher seal is absent returns
`DISPATCHER_SEAL_UNDELIVERED`. Parser or snapshot acceptance is insufficient.
`ClosureCapabilityV1.CAPABILITY_UNAVAILABLE` is structural and
may appear only before reservation. After reservation, failure to construct a
fresh permit is a current-host fact: the envelope remains inert and exact, but
ordinary observation bookkeeping may still advance.
The caller persists an available provider version in the continuation's typed
`ACTIVE_ATTEMPT` subject before acquisition;
every `HELD`, `NEVER_ACQUIRED`, `RELEASED`, reconcile result, release request,
and held refresh must match it byte-for-byte. A provider contract change
requires a new version. Missing or mismatched versions normalize to `UNKNOWN`,
retain every fence, and grant no authority. A provider with no available
version is rejected by the static pre-reservation capability gate.
An acquisition result that claims `CAPABILITY_UNAVAILABLE` is malformed rather
than a transient closure veto: retain every persisted fence, emit continuous
`CAPABILITY_UNAVAILABLE`, and perform no teardown, retry, or exhaustion
transition. `UNKNOWN(CAPABILITY_UNAVAILABLE)` during reconciliation likewise
describes cleanup uncertainty, not permission to reclassify the issued attempt
as an ordinary failure.
Repeated acquisition returns the same closure or a closed refusal. A
reconciliation result always requires its acquisition ID to equal the
persisted attempt ID. While `TREE_CLOSURE_ACQUIRING` has no persisted closure
ID, the first well-formed matching `HELD` or `RELEASED` may bind its returned
non-null closure ID in the same checked transition; on reload that binding is
release-only and never authorizes termination. `NEVER_ACQUIRED` is valid only
in that null-ID acquiring state. Once a closure ID has been bound, every
`HELD` or `RELEASED` result and release request must exactly equal the
persisted pair. Missing, null, changed, or conflicting IDs normalize to
`UNKNOWN`, retain the reservation and any debt/current attempt, and forbid
kill and launch. `NEVER_ACQUIRED` is terminal for that acquisition ID: the
same checked transaction appends its bound attempt ID/revision, binding,
provider version, and `TERMINAL(NEVER_ACQUIRED)` tombstone to
`retired_attempts`, and the closure successor must not later acquire it. An
unexpected late `HELD` for a retired ID is never authority. A cleanup poll must
first arm a typed `RETIRED_ATTEMPT` continuation, then use a distinct fresh
post-CAS permit to construct the reconcile call. A matching receipt may only
restore terminal state or persist `RELEASE_PENDING(closure_id)` with the
reconcile continuation at `CALL_RETURNED`. A later
`CLOSURE_RELEASE/STATE_MUTATION` permit must replace that checkpoint with
`CLOSURE_RELEASE/ARMED`; release then uses its own fresh post-CAS call permit
and matching receipt mutation. The successor has no unsolicited result
channel. A crash leaves the
typed continuation/tombstone for guarded takeover; a host without a matching
witness can deserialize both but can build neither mutation nor call.

After crash/reload, only matching `NEVER_ACQUIRED` or `RELEASED` permits 87-A
to finish a release-dependent transition. Matching `HELD` must be released
with the exact pair and then reconciled to matching `RELEASED`; `UNKNOWN`
retains state. `HELD.raw_process_observation` is the immutable raw capture
returned under that closure, and its `capture_id` exactly equals
`HELD.capture_id`. An action-ready acquisition or held-refresh capture has the
reservation's `state_epoch`, `agent_key`, and current
`ordinary_poll_sequence`, has nonzero `capture_ordinal`, and differs from the
ordinary `source_capture_id`. It is captured strictly after the closure
linearization point. A replayed pre-closure capture, ID without its raw object,
target tuple, or digest cannot stand in for it. Reconciliation `HELD` by itself
never authorizes a new termination; a caller needing current evidence obtains
a fresh held-refresh result under the same exact closure.

The closure successor may choose a Windows mechanism only if it preserves
those semantics and the core's absolute dependency-plane constraint. Its
separate design must name the process-universe mechanism, synchronous
action-scoped linearization primitive, crash/release behavior, compatibility
evidence, and failure injection. It may use only existing checked supervisor
state and transient caller-owned synchronization that leaves no durable helper
or OS object. It may not add a daemon/service, durable helper, new durable
file/database/registry/journal outside that checked state, durable named OS
object, package, or runtime dependency.

The operator resolved delta-panel item M5 as Option A on 2026-07-31: this
constraint is absolute, with no mechanism-specific or separately versioned
exception. If a platform cannot prove the contract inside the boundary, 87-A
requires `CAPABILITY_UNAVAILABLE`, no closure-dependent named teardown
proceeds, and dependent recovery remains `POLICY_HELD` pending a human. An
implementation that discovers an unprovable case opens or updates a task; it
must not introduce a mechanism as an implementation detail.
This deliberately accepts that some recoveries may never become automatic;
the operator preferred that availability loss to stale-authority risk.

### Operator-visible capability reductions

Revision 12 has three permanent V1 capability limitations that operators must
see together. The first two are active recovery holds; the third refuses
activation before imported state becomes an active checked store:

- On Linux and macOS, every closure-dependent named teardown returns
  `CAPABILITY_UNAVAILABLE(EXACT_TARGET_EXECUTOR_UNAVAILABLE)` and recovery
  remains `POLICY_HELD` pending a human. This persists until a separately
  reviewed exact-token kill adapter exists; #120's accepted Linux observation
  token is not that adapter.
- On every platform, automatic retirement of
  `StateLossQuarantineV1.UNRESOLVED` is unavailable in V1. Recovery remains
  `POLICY_HELD` with `CHILDLESS_STATE_PROVENANCE_LOST` pending attended
  handling because merged #120 supplies no trustworthy host/process-universe
  token. A local replacement and local absence cannot prove that the prior
  wrapper and erased-debt members are extinct in their source universe.
- Same-platform state-file/workspace transfer, restore (including backup
  restore), rollback, and migration activation are unavailable in V1. When a
  conforming activation path is told,
  or otherwise knows, that checked state came from one of those operations, it
  must refuse before admitting those bytes as the active checked store. The
  refusal constructs no `CurrentExactTargetExecutorWitnessV1`, permit,
  authority/effect mutation, external call, or launch and directs the operator
  to attended handling and 87-C. This is an activation refusal, not
  `CAPABILITY_UNAVAILABLE`/`POLICY_HELD` inside an active agent, because no
  conforming V1 activation occurred.

None of the following is sufficient, alone or in combination, to establish
that missing process-universe identity: **PID and start, hostname,
`state_epoch`, `process_source_digest`, MachineGuid alone, local absence**.
A future successor may enable automatic retirement only through a reviewed
read-only producer over an **existing OS token** that binds the prior owner and
extinction coverage to the same source host/process universe. It may introduce
no new file, registry value, helper, daemon, OS object, persistence plane, or
runtime dependency. Until that successor is delivered, the unavailable result
is the design, not a temporary implementation fallback.

V1 persists no trustworthy source-host/process-universe token, so 87-A cannot
recognize an out-of-band copy or overwrite presented as an ordinary in-place
restart. Such a bypass is nonconforming and may be treated as local checked
state; 87-A makes no safety or recovery guarantee for it. If the existing outer-
state checks detect the replacement as rollback-unproven, the only admitted
non-dry-run response is fail-closed `StateLossQuarantineV1.UNRESOLVED`; lack of
such detection is not evidence of safe provenance. Future 87-C must bind the
source universe within M5 Option A or keep imported state inert.

## Two independent complete child-absence captures

This module does not modify the core's banked `CHILD_DEAD`
`ConsecutiveEvidenceV1`, its basis, or its runtime mapping. It maintains a
separate checked overlay:

```text
OwnedChildlessConfirmationV1 {
  count: integer 0..2
  basis_digest: Hex64 | null
  runtime_child_dead_basis_digest: Hex64 | null
  active_child_config_digest: Hex64 | null
  child_establishment_guard: ChildEstablishmentGuardV1.CLOSED | null
  owner_identity_id: Hex64 | null
  coverage: OwnedTreeCoverageV1 | null
  first_capture_id: CaptureIdV1 | null
  last_capture_id: CaptureIdV1 | null
  last_ordinary_poll_sequence: uint64 | null
}
```

All nine nullable evidence fields are null exactly when `count=0`. At count
one, every field is non-null and first and last capture IDs are equal. At
count two every field is non-null, capture IDs are distinct, and the last
ordinary sequence is exactly one greater than the first capture's sequence.
At both nonzero counts, first and last capture IDs have
`state_epoch == ClassifierStateV1.state_epoch`,
`agent_key == ClassifierStateV1.agent_key`, and `capture_ordinal == 0`;
`last_ordinary_poll_sequence` exactly equals
`last_capture_id.ordinary_poll_sequence`. Any other shape is invalid checked
state and holds recovery.

One qualifying ordinary poll requires, inside one #115 checked transaction:

```text
raw ProcessObservationV1.availability == COMPLETE
and raw ProcessObservationV1.active_child_availability == COMPLETE
and raw ProcessObservationV1.capture_id.state_epoch ==
    checked predecessor ClassifierStateV1.state_epoch
and raw ProcessObservationV1.capture_id.agent_key ==
    checked predecessor ClassifierStateV1.agent_key
and raw ProcessObservationV1.capture_id ==
    receipt.prospective_capture_id
and raw ProcessObservationV1.capture_id ==
    candidate PrivateClassifierObservationMutationV1.capture_id
and raw ProcessObservationV1.capture_id.ordinary_poll_sequence ==
    candidate PrivateClassifierObservationMutationV1.ordinary_poll_sequence
and candidate ordinary_poll_sequence ==
    checked predecessor ordinary_poll_sequence + 1 using checked uint64 addition
and raw ProcessObservationV1.capture_id.capture_ordinal == 0
and candidate ChildEstablishmentGuardV1 is CLOSED for the exact strict turn
and ActiveChildObservationV1 == ABSENT
and core CHILD_DEAD sample basis is valid
and active_child_config_digest is the exact config digest inside that basis
and OwnedWrapperTreeObservationV1 is COMPLETE
and tree.capture_id == raw ProcessObservationV1.capture_id
and tree.ordinary_poll_sequence == candidate ordinary_poll_sequence
```

Only the winning commit makes that candidate the current successor. A stale
CAS loser cannot reinterpret its candidate against the winner's successor. If
the predecessor sequence is maximum `uint64`, core #115 returns
`CAPTURE_SEQUENCE_EXHAUSTED` before acquisition, so no qualifying poll or module
reduction exists.

The module basis digest is SHA-256 over
`agenttalk.supervisor.owned-childless-confirmation-basis.v1\0` plus
`CanonicalJsonV1` of exactly:

```text
{
  "active_child_config_digest": <Hex64>,
  "child_establishment_guard": <ChildEstablishmentGuardV1.CLOSED>,
  "coverage": <OwnedTreeCoverageV1>,
  "owner_identity_id": <Hex64>,
  "runtime_child_dead_basis_digest":
    <core ConsecutiveEvidenceV1 CHILD_DEAD basis Hex64>,
  "schema": "owned-childless-confirmation-basis/v1"
}
```

A qualifying sample with no compatible prior sample becomes count one. An
`OPEN` guard is nonqualifying, maps the complete zero-child observation to
`UNKNOWN(CHILD_ESTABLISHMENT_OPEN)`, and resets both this overlay and the
banked child-death counter. Its capture cannot seed the closed window. From
count one, a distinct capture advances to count two only when its ordinary
sequence is adjacent and the complete basis, runtime basis, active-child
config, complete closed establishment guard, owner, and coverage are equal.
From count two, another distinct compatible capture adjacent to the stored
last capture keeps count two and slides the window: prior `last_capture_id`
becomes `first_capture_id`, the current capture becomes `last_capture_id`, and
the current ordinary sequence becomes `last_ordinary_poll_sequence`. Replay
leaves the `OwnedChildlessConfirmationV1` value byte-identical. A gap or any
changed equality input restarts
at the current qualifying sample; nonqualifying evidence resets to empty. Tree
membership may change between samples; the action-time closure freezes and
revalidates the final target set. The overlay is committed atomically beside,
but never feeds or rewrites, the banked core counter.

```text
child_death_sourced_dominant =
  runtime.dominant == CURRENT_TEARDOWN_PROOF
  and child_dead_confirmation.count == 2
  and child_dead_confirmation.basis_digest is non-null
  and ActiveChildObservationV1 derived from the current ordinary raw capture
      == ABSENT

childless_source =
  child_death_sourced_dominant
  and owned_childless_confirmation.count == 2
  and owned_childless_confirmation.runtime_child_dead_basis_digest ==
      child_dead_confirmation.basis_digest
  and owned_childless_confirmation.active_child_config_digest ==
      current active_child_config_digest
  and owned_childless_confirmation.child_establishment_guard ==
      current ChildEstablishmentGuardV1.CLOSED
  and owned_childless_confirmation.owner_identity_id ==
      current_tree.owner_identity_id
  and owned_childless_confirmation.coverage ==
      current_tree.coverage
  and owned_childless_confirmation.last_capture_id ==
      current_tree.capture_id
  and raw ProcessObservationV1.capture_id == current_tree.capture_id
  and ActiveChildObservationV1 derived from that raw capture == ABSENT
```

Two captures are required. One complete capture plus positive ownership is
not sufficient because the extra poll detects observer/transient mistakes at
low cost and matches the system's existing positive-absence standard.

## Named authority constructor

```text
ChildlessTeardownAuthorityV1 =
  NONE
  | BLOCKED(
      OWNER_OR_CHILD_INCOMPLETE
      | TREE_OBSERVATION_INCOMPLETE
      | TEARDOWN_DEBT_INCOMPLETE
    )
  | PROVABLY_CHILDLESS_OWNED_WRAPPER {
      mode: INITIAL | DEBT_COMPLETION
      authority_id: Hex64
      owner: OwnedWrapperIdentityV1
      owner_identity_id: Hex64
      basis_id: Hex64
      source_committed_revision: uint64
      source_condition_fingerprint: RecoveryConditionFingerprintV1
      source_capture_id: CaptureIdV1
      source_coverage: OwnedTreeCoverageV1
      owned_childless_basis_digest: Hex64 | null
      runtime_child_dead_basis_digest: Hex64 | null
      active_child_config_digest: Hex64 | null
      child_establishment_guard: ChildEstablishmentGuardV1.CLOSED | null
      targets: tuple[OwnedTreeTargetV1] of length 1..64
      target_digest: Hex64
      debt_id: Hex64 | null
      debt_generation: strict positive integer | null
    }

ChildlessReservationEvidenceV1 {
  mode: INITIAL | DEBT_COMPLETION
  authority_id: Hex64
  owner: OwnedWrapperIdentityV1
  owner_identity_id: Hex64
  basis_id: Hex64
  source_committed_revision: uint64
  source_condition_fingerprint: RecoveryConditionFingerprintV1
  source_capture_id: CaptureIdV1
  source_coverage: OwnedTreeCoverageV1
  owned_childless_basis_digest: Hex64 | null
  runtime_child_dead_basis_digest: Hex64 | null
  active_child_config_digest: Hex64 | null
  child_establishment_guard: ChildEstablishmentGuardV1.CLOSED | null
  targets: tuple[OwnedTreeTargetV1] of length 1..64
  target_digest: Hex64
  debt_id: Hex64 | null
  debt_generation: strict positive integer | null
}

ChildlessClosureEvidenceV1 {
  mode: INITIAL | DEBT_COMPLETION
  authority_id: Hex64
  basis_id: Hex64
  acquisition_id: lowercase hyphenated UUID
  closure_provider_version: ClosureProviderVersionV1
  closure_id: lowercase hyphenated UUID
  closure_capture_id: CaptureIdV1
  owner_identity_id: Hex64
  source_coverage: OwnedTreeCoverageV1
  active_child_capture_id: CaptureIdV1 | null
  active_child: ActiveChildObservationV1 | null
  current_owned_childless_basis_digest: Hex64 | null
  current_runtime_child_dead_basis_digest: Hex64 | null
  current_active_child_config_digest: Hex64 | null
  current_child_establishment_guard:
    ChildEstablishmentGuardV1.CLOSED | null
  targets: tuple[OwnedTreeTargetV1] of length 1..64
  target_digest: Hex64
  debt_id: Hex64 | null
  debt_generation: strict positive integer | null
}
```

With no teardown debt, `INITIAL` exists if and only if `childless_source` is
true, presence is `PRESENT_TARGETABLE`, targetability is `COMPLETE` for the
same wrapper PID, and the targetability candidate's generic start token
independently satisfies
`StartRepresentationMatchV1(candidate.start_guard,
owner.wrapper_start_token)` from the same complete raw capture. It is never
compared directly with `owner.wrapper_start_guard`, which is the separate exact
destructive identity. The current tree must be `COMPLETE`, and every
state/owner/capture binding above must match. A second relevant wrapper,
incomplete tree, unguarded target, or targetability mismatch is `BLOCKED`,
never generic teardown fallback. `INITIAL` copies the current tree owner,
capture, coverage, root-first targets, recomputed target digest, committed
successor revision containing those observations, and exact
`RecoveryConditionFingerprintV1` computed from that committed observation.
Its owned confirmation basis, runtime child-death basis, active-child config
digest, and complete closed child-establishment guard are non-null and both
debt fields are null.

With outstanding debt and no current attempt, `DEBT_COMPLETION` exists if and
only if the current `OwnedDebtResidualObservationV1` is
`COMPLETE_RESIDUAL` with the exact debt ID/generation/owner, its target tuple is
the exact live order-preserving subset of the immutable authorized tuple, and
every binding is current. It copies that residual capture/coverage/tuple.
It also copies the committed successor revision containing that residual and
its exact same-revision condition fingerprint. Its four
childless/runtime/config/establishment basis fields are null and both debt
fields are non-null. A
`COMPLETE_GONE` residual observation is inert input; only a fresh matching
`EFFECT_FINALIZE` permit may construct the checked mutation that clears
debt/cycle and writes the same-poll terminal. It constructs no authority or
launch. The #115 owner-private observation mutation cannot perform that clear.
`INCOMPLETE` retains debt and constructs `BLOCKED(TEARDOWN_DEBT_INCOMPLETE)`.
Closure is never a precondition of either authority constructor.

`basis_id` hashes
`agenttalk.supervisor.childless-teardown-basis.v1\0` plus `CanonicalJsonV1` of
exactly:

```text
{
  "active_child_config_digest": <Hex64 | null>,
  "child_establishment_guard":
    <ChildEstablishmentGuardV1.CLOSED | null>,
  "debt_generation": <positive integer | null>,
  "debt_id": <Hex64 | null>,
  "mode": "INITIAL" | "DEBT_COMPLETION",
  "owned_childless_basis_digest": <Hex64 | null>,
  "owner_identity_id": <Hex64>,
  "runtime_child_dead_basis_digest": <Hex64 | null>,
  "source_capture_id": <CaptureIdV1>,
  "source_committed_revision": <uint64>,
  "source_condition_fingerprint": <RecoveryConditionFingerprintV1>,
  "source_coverage": <OwnedTreeCoverageV1>,
  "state_epoch": <lowercase hyphenated UUID>,
  "target_digest": <Hex64>
}
```

`source_committed_revision` is exactly the checked state successor revision
that atomically persisted `source_capture_id` and all source observations.
`source_condition_fingerprint` is exactly the core fingerprint computed from
those same committed observations. Both are copied into authority and
reservation evidence and included verbatim in `basis_id`; a caller can
reconstruct the identifier without an unstated current-state choice.

`authority_id` hashes
`agenttalk.supervisor.provably-childless-owned-wrapper-authority.v1\0` plus
canonical `{"basis_id": ..., "mode": "INITIAL" | "DEBT_COMPLETION",
"schema": "provably-childless-owned-wrapper-authority/v1",
"target_digest": ...}`.

Every displayed proof object is inert. The fresh reservation constructor must
consume it together with a current witness, recompute its tuple/digest, create
the exact `ExactTargetExecutorBindingV1`, and emit one
`PermitBoundChildlessMutationV1` whose next state is
`CHILDLESS(ChildlessEffectEnvelopeV1)`. No caller may copy the fields directly
into checked state. Neither an authority hash, the generic targetability
digest, nor a command-line match is decoded or substituted for the target
tuple.

`ChildlessClosureEvidenceV1` is the 87-A join over the closure-successor result
and current checked state; the successor does not decide authority. It is valid only when the
closure acquisition ID equals the persisted attempt ID; its mode,
owner/coverage/targets/digest/debt fields exactly equal both the reservation
and `OwnedTreeClosureV1.HELD`; its closure-provider version equals the
persisted continuation owner; authority/basis IDs equal the reservation; and
every target digest equals a fresh `OwnedTargetDigestV1` recomputation.

For `INITIAL`, all debt fields are null and all three childless/runtime/config
basis digests plus the closed establishment guard are non-null. The joined
evidence additionally requires:

```text
HELD.raw_process_observation.capture_id == HELD.capture_id
  == closure_capture_id == active_child_capture_id
HELD.raw_process_observation.availability == COMPLETE
HELD.raw_process_observation.active_child_availability == COMPLETE
closure_capture_id has the current state_epoch, agent_key,
  and ordinary_poll_sequence, has capture_ordinal > 0,
  and closure_capture_id != source_capture_id
active_child == ActiveChildObservationV1(
  current strict runtime record,
  current checked managed identity,
  current active-child matcher/launcher-self configuration,
  current ChildEstablishmentGuardV1,
  HELD.raw_process_observation
) == ABSENT
current_child_establishment_guard ==
  current checked ChildEstablishmentGuardV1.CLOSED ==
  reserved child_establishment_guard
current_active_child_config_digest ==
  reserved active_child_config_digest
current_runtime_child_dead_basis_digest ==
  a live reconstruction of the core's exact CHILD_DEAD basis from
  the current strict runtime record, checked managed identity,
  and current active-child configuration
current_runtime_child_dead_basis_digest ==
  reserved runtime_child_dead_basis_digest
current checked child_dead_confirmation.count == 2
current checked child_dead_confirmation.basis_digest ==
  reserved runtime_child_dead_basis_digest
current checked OwnedChildlessConfirmationV1.count == 2
current checked OwnedChildlessConfirmationV1.runtime_child_dead_basis_digest ==
  current checked child_dead_confirmation.basis_digest
current checked OwnedChildlessConfirmationV1.active_child_config_digest ==
  reserved active_child_config_digest
current checked OwnedChildlessConfirmationV1.child_establishment_guard ==
  reserved child_establishment_guard
current_owned_childless_basis_digest ==
  a live reconstruction of the module basis from HELD coverage/owner,
  current_runtime_child_dead_basis_digest,
  current_active_child_config_digest,
  and current_child_establishment_guard
current_owned_childless_basis_digest ==
  reserved owned_childless_basis_digest ==
  current checked OwnedChildlessConfirmationV1.basis_digest
```

The live reconstructions do not read a cached counter digest as a substitute
for their named current inputs, and equal digests do not substitute for either
current counter being at count two. Thus a changed turn, phase, progress
sequence, wrapper/managed generation, PID/start, launch nonce, matcher,
launcher-self policy, row/start-guard schema, ancestry algorithm, coverage, or
confirmation count vetoes before teardown even if an older two-poll basis
digest remains present.

For `DEBT_COMPLETION`, active child, active-child capture ID, and all four
childless/runtime/config/establishment basis fields are null; current checked
debt still has the reserved ID/generation/owner/immutable authorized tuple.
Every other
well-formed but unequal current observation is an action-time veto. A
structurally invalid persisted state, malformed #120 snapshot, or malformed
closure-successor value is
`POLICY_HELD`; neither class grants authority.

## Action-time closure and closed supervisor owned-tree dispatch

**SPECIFIED, NOT DELIVERED; requires tasks #115 and #146 plus the closure
successor:**
State CAS alone does not own an external call. Every childless active or
retired closure-successor call, `Stop-Tree` call, action capture, and spawn
therefore runs under one exclusive transient per-agent effect
guard and one checked `ChildlessContinuationOwnerV1`. Lock order is fixed:
configuration snapshot, action latch, effect guard, then the short #115
checked-state transaction. The effect guard is caller-owned synchronization,
not a daemon or persistence plane: it exists only while a live caller holds an
in-process or kernel handle, is automatically released when that process
exits, has no detached owner or durable payload, and is never evidence after
release. No timeout, heartbeat, lease renewal, or second poller may steal it.
One successful acquisition creates exactly one private lineage in `AVAILABLE`;
the acquisition exposes no API capable of minting a second lineage or custody
token.

The checked owner identifies the exact supervisor process/start, applicable
action-latch epoch, continuation role, typed subject, operation, stage, and
state revision. The invocation
acquires the effect guard, recaptures every gate and basis, commits
`effect_stage=ARMED` with a state-mutation permit, constructs a distinct fresh
post-CAS call permit, rechecks that it still owns both the guard and exact
checked owner, privately constructs the sealed call, and wins its atomic
`CALL -> DISPATCHING` adapter admission before any external-effect plan. For
`STOP_TREE`, the private constructor also emits the one dispatcher submission
and #146 returns the admission proof before native-plan construction. The caller
then performs the one synchronous external operation and constructs a third fresh
receipt-derived mutation permit preserving the call issuance before committing
`CALL_RETURNED` or the
operation-specific terminal result. An adapter accepts a retired attempt only
through its exact typed retired subject and rejects any caller whose persisted
continuation owner no longer matches. Thus committing a phase never, by itself,
licenses a later stale continuation.

For every syntactically valid checked state, Revision 12 specifies two separate
structural rules. The childless rule is narrower and stronger than Revision 9's
path-enumeration claim:

```text
without a fresh CurrentExactTargetExecutorWitnessV1 that exactly matches the
persisted ExactTargetExecutorBindingV1, no 87-A childless executor-dependent
external effect and no childless authority-enabling or effect-owned mutation
is constructible

no supervisor owned-tree native termination is reachable except from one
SupervisorOwnedTreeNativeInvocationV1 created by the sole winning atomic
CALL -> DISPATCHING -> PLAN_OWNED -> INVOKING transitions over one privately
paired submission/owner
```

The second rule treats same-object aliasing and sequential replay as hostile and
preserves authorized non-childless behavior through its own
opaque variants without granting childless authority. It is a normative #146
contract, not a current-code fact: merged `Stop-Tree($targets)` and its two raw
planner calls remain reachable until #146 lands and is reviewed.

The witness is an operand of construction, not a Boolean checked at remembered
sites. Persisted Windows authority, a call-returned marker, a target tuple, a
closure result, or a deserialized envelope cannot substitute for it. Failure
because the current host cannot supply a matching witness leaves the complete
`ChildlessEffectEnvelopeV1` byte-identical, makes no external call, and emits
`CAPABILITY_UNAVAILABLE` plus `POLICY_HELD`; a `REJECTED` construction instead
performs zero effect and follows its reload/reject rule.

For the sole first `RESERVE/INITIAL` constructor, where no persisted binding yet
exists, “matching the persisted binding” means matching the prospective exact
binding carried by the one-shot permit and atomically persisting that same value
as part of the envelope-creation mutation. `RESERVE/CONTINUE` matches the exact
current envelope binding and, for a permitted initial-mode rebind, also carries
the prospective fresh binding committed by that same mutation. No other
operation receives a prospective-binding exception.

Here **authority-enabling mutation** means a checked change that creates,
consumes, releases, or rebinds a childless reservation; changes childless
execution, attempt, closure, debt, cycle, continuation, retired-attempt,
nonordinary-capture, spawn, or guarded-identity ownership; or clears one of
those fences. It does not mean a pure ordinary-observation field whose value is
inert evidence and whose constructor returns no reservation, permit, executable
target, call, or receipt. Such evidence may make a later pure predicate true,
but it cannot act until a separate permitted mutation succeeds.

The sole outer-state exception is
`StateLossQuarantineCreationDeltaV1`, used only when no trustworthy current
state/revision exists to mutate. It replaces unreadable or unproven bytes with
a new quarantined genesis that exposes less authority and no childless effect
object; it is not a constructor over a valid envelope. No other permit-free
exception exists.

Revision 10 explicitly withdrew Revision 9's broader promise that the entire
checked state remains byte-identical on such a poll. A valid owner-private
ordinary-observation mutation may increment
`ordinary_poll_sequence`, reset
`next_capture_ordinal`, clear the prior same-poll terminal, and update its
observation-only projection. Its type cannot address the childless effect
envelope. Quarantine creation is the only other permit-free childless-related
mutation, and it strictly removes authority. No #115 owner-private observation
mutation may clear or
rewrite a reservation, closure, debt, cycle, continuation, pending
disposition, retired attempt, spawn state, or ambiguity state.

A non-`NONE` owner is well formed only when its token digest and PID/start
exactly equal the arm-time `ExecutionGateCaptureV1` current supervisor; its
typed subject exactly equals the active reservation/attempt, one retained
tombstone, or the spawn reservation required by its operation; and
`armed_state_revision` is the successor revision that wrote that owner.
`CLOSURE_ACQUIRE`, callable `STOP_TREE/ARMED`, and `SPAWN/ARMED` require
`role=ISSUER` and non-null
`action_latch_epoch` exactly equal to a fresh enabled action latch.
Non-destructive `CLOSURE_RECONCILE` and
`CLOSURE_RELEASE`, `RETIRED_ATTEMPT_RECONCILE`, and
`POST_ACTION_CAPTURE` retain the current epoch when the latch is enabled and
use null when cleanup is permitted under a disabled latch.
Here `supervisor_start_guard` and the execution-gate supervisor start are both
generic `ProcStartGuardV1` representation tokens. Their exact arm-time equality
binds the live continuation; neither is compared with an owned-tree
`OwnedExactStartGuardV1`.
`STOP_TREE/CALL_RETURNED` retains the arm-time epoch as checked evidence that
the destructive call already returned; a post-action capture under that state
is non-destructive and may proceed through the narrow cleanup gate even if the
latch later disables. The pre-call recheck requires the operation-specific
values still equal a fresh gate capture. Any mismatch is a veto before a new
external call, not permission to continue under the older owner.

`role=ISSUER` requires `takeover_origin=NONE`. `role=RECONCILER` is first
introduced only by a takeover CAS and requires `takeover_origin=FROM` carrying
the exact immediate predecessor continuation ID, operation, and stage. The
takeover itself keeps the predecessor operation, writes inert
`TAKEOVER_CHECKPOINT`, and never writes any operation at `ARMED`; at that
checkpoint `operation == takeover_origin.predecessor_operation`. No adapter
accepts the checkpoint. Only after reload may a distinct operation-specific
`STATE_MUTATION` permit replace it with the closed table's next arm. That arm
and its `CALL_RETURNED` checkpoint retain the same `takeover_origin` so their
phase/operation provenance remains structurally checkable. A later distinct
post-CAS call permit is still mandatory. Another takeover replaces the origin
with its exact immediate predecessor; it cannot erase or invent a stage.

A different poller cannot reconcile while the persisted continuation may
resume: it returns `RETAIN_LIVE_CONTINUATION`. Takeover is permitted only
after acquiring the released effect guard and positively proving either
same-process structured unwind/cancellation or that the guarded predecessor
PID/start no longer exists. Mere age, missing heartbeat, a timeout, or
different supervisor token is not proof. The takeover writes the exact
`role=RECONCILER/TAKEOVER_CHECKPOINT/takeover_origin=FROM` owner before any
reconciliation operation.
Its closed no-call mapping is behavioral definition, not the universal proof:

| Predecessor obligation | Takeover checkpoint state | Only next arm/result mutation |
| --- | --- | --- |
| acquiring closure | retain acquiring and the predecessor operation | `CLOSURE_RECONCILE` |
| held closure | retain held and the predecessor operation; teardown stays forbidden | `CLOSURE_RECONCILE` |
| releasing closure, including post-action capture | retain disposition, debt, and predecessor operation | `CLOSURE_RECONCILE` |
| `STOP_TREE/ARMED` | map to releasing/`EFFECT_UNPROVEN`, retain debt, preserve predecessor `STOP_TREE` | `CLOSURE_RELEASE` |
| `STOP_TREE/CALL_RETURNED` | retain returned-effect fact and debt, preserve predecessor `STOP_TREE` | `CLOSURE_RECONCILE`; then `POST_ACTION_CAPTURE` only after matching `HELD`, or receipt-bound `EFFECT_FINALIZE/EFFECT_UNPROVEN` after matching `RELEASED` |
| retired-attempt cleanup | retain childless `IDLE`, tombstone, and predecessor operation | `RETIRED_ATTEMPT_RECONCILE` |
| `SPAWN/ARMED` | retain `SPAWN_IN_FLIGHT` and predecessor `SPAWN` | crash-only `SPAWN_RESULT_COMMIT` with positive issuer-death proof |

Any predecessor not admitted by this closed table is invalid and remains inert.
A matching
`NEVER_ACQUIRED` is a stable terminal result for that acquisition attempt and
retires the attempt in the same transaction; a later unexpected `HELD` is
release-only and can never restore authority. Any result that cannot be
classified exactly is `UNKNOWN` and retains the fence.

After the checked reservation, an invocation under that guard uses a fresh
`CLOSURE_ACQUIRE/STATE_MUTATION` permit to persist a fresh attempt ID, an exact
`ACTIVE_ATTEMPT/CLOSURE_ACQUIRE/ARMED/ISSUER` continuation, and
`TREE_CLOSURE_ACQUIRING` inside the same envelope. Automatic origin also
creates or increments its cycle in that mutation. At the successor revision,
only a distinct fresh `CLOSURE_ACQUIRE/EXTERNAL_CALL` permit may construct the
typed call. The closure adapter accepts no raw attempt ID and returns a receipt
bound to that call permit, binding, continuation, and checked revision. The
invocation retains the effect guard while a third, fresh receipt-derived
`CLOSURE_ACQUIRE/RECEIPT_MUTATION` permit preserves the call issuance and
applies the result. Acquisition and
crash reconciliation remain idempotently keyed by the attempt ID. 87-A must then construct
`ChildlessClosureEvidenceV1`; the closure object alone never supplies
authority. A well-formed current equality mismatch is a closure veto and
follows exact release. A well-formed envelope plus malformed closure-successor
output is `POLICY_HELD`; if its exact closure pair is known, only permit-bound
non-destructive reconciliation/release may proceed. Structurally invalid
checked state instead selects `RETAIN_INVALID_FENCE` before witness/permit
construction and makes no mutation or call. Neither case kills or launches.

Only after that equality check may a receipt-derived mutation persist the
closure ID while retaining its acquire/reconcile owner at `CALL_RETURNED`.
Before termination, one fresh `STOP_TREE/STATE_MUTATION` permit constructs a mutation that atomically
persists teardown debt, enters `TEARDOWN_IN_FLIGHT`, and records
an exact `ACTIVE_ATTEMPT/STOP_TREE/ARMED/ISSUER` continuation. At the successor
revision, and only after arm custody returns, a second fresh
`STOP_TREE/EXTERNAL_CALL` permit constructs `ExecutableOwnedTargetSetV1` and the
sealed typed childless call. #146's private factory atomically emits that call
as `SupervisorOwnedTreeDispatchCallV1.CHILDLESS` plus its one opaque submission;
the dispatcher admits only the `CALL -> DISPATCHING` winner and accepts neither
a raw tuple, planner array, caller tag, persisted state, nor call without its
paired atomic owner. On
synchronous return, only the matching typed receipt plus a third, fresh
receipt-derived `STOP_TREE/RECEIPT_MUTATION` permit preserving the call
issuance can construct the `CALL_RETURNED`
mutation; only that stage may arm a three-stage permitted post-action capture.
If the owner dies or unwind is proved while still
`ARMED`, recovery never reissues `Stop-Tree` or assumes it did not run: a
permitted reconcile/release mutation records `EFFECT_UNPROVEN`, retains debt,
releases any exact closure, and forbids launch. No copied
`Stop-Process`, `TerminateJobObject`, name/pattern kill, or second target-kill
path is conforming.

The closure remains held through `Stop-Tree` and exactly one fresh typed
post-action observation:

```text
ChildlessPostTeardownObservationV1 =
  INCOMPLETE(
    ordered deduplicated nonempty tuple[
      CAPABILITY_UNAVAILABLE
      | SNAPSHOT_UNAVAILABLE
      | SNAPSHOT_TRUNCATED
      | COVERAGE_UNREADABLE
      | CLOSURE_MEMBERSHIP_UNREADABLE
      | AUTHORIZED_IDENTITY_UNREADABLE
      | NEW_OR_UNOWNED_CLOSURE_MEMBER
      | RESIDUAL_NOT_AUTHORIZED_SUBSET
      | POST_KILL_LAUNCH_BARRIER_BLOCKED
    ] in displayed order
  )
  | COMPLETE_GONE {
      capture_id: CaptureIdV1
      closure_id: lowercase hyphenated UUID
      coverage: OwnedTreeCoverageV1
      owner_identity_id: Hex64
      authorized_target_digest: Hex64
    }
  | COMPLETE_RESIDUAL {
      capture_id: CaptureIdV1
      closure_id: lowercase hyphenated UUID
      coverage: OwnedTreeCoverageV1
      owner_identity_id: Hex64
      authorized_target_digest: Hex64
      live_targets:
        tuple[OwnedTreeTargetV1] of length 1..64
      residual_target_digest: Hex64
    }
```

A complete result requires the exact held closure/owner/authorized digest,
complete process and closure-membership coverage, an unblocked fresh #120
post-kill launch-barrier result over the same captured process rows, and a
capture with the same
state epoch, agent, and ordinary poll sequence whose ordinal is strictly
greater than the action-ready closure capture. `COMPLETE_GONE` means every
authorized PID/start is positively absent, closure membership is empty, and
the #120 barrier is clear: it finds no recorded-identity survivor,
conservative old-side descendant edge, same-agent wrapper/wait process, or
other blocking reason under its exact recycled-parent split.
`COMPLETE_RESIDUAL.live_targets` is the exact order-preserving live subset of
the immutable authorized tuple and its digest is a recomputed
`OwnedTargetDigestV1`; any omitted target is positively absent. Any other fact
is `INCOMPLETE`. Merged #120 issues a same-handle wait attempt after each
successful planned Windows-target termination, using the remaining shared
tree-wide budget before this fresh capture; presence after that budget remains
residual evidence and never counts as completion. An unavailable or ambiguous
barrier is incomplete.
A barrier blocked only by an unplanned late descendant that was absent from the
planned target set
maps to `INCOMPLETE(POST_KILL_LAUNCH_BARRIER_BLOCKED)` and therefore
`EFFECT_UNPROVEN`; that edge blocks launch but never becomes a target, proves
`COMPLETE_GONE`, or clears debt. An unblocked barrier alone also cannot prove
`COMPLETE_GONE`; the typed closure/identity absence proof remains mandatory.

The failure mapping is total: a residual containing the authorized tuple's
depth-zero wrapper target is `SAME_OWNER_SURVIVED`; a nonempty residual without
that root is `MEMBER_SURVIVED`; `INCOMPLETE` is `EFFECT_UNPROVEN`.
`COMPLETE_GONE` may clear debt and proceed to the core's fresh final barrier
only through a permit-bound mutation after a matching typed release receipt.
The same envelope and binding then carry through `PRE_BARRIER`,
`SPAWN_IN_FLIGHT`, and any `AMBIGUOUS_LAUNCH`; launch and identity commit are
typed permitted operations, not consequences of deserializing those phases.
Every failure retains debt and forbids launch until release handling finishes.
Crash or reload never recreates the same-call continuation or receipt.

## Durable teardown debt

```text
TeardownDebtV1 =
  NONE
  | OUTSTANDING {
      debt_id: Hex64
      owner: OwnedWrapperIdentityV1
      owner_identity_id: Hex64
      authorized_targets:
        tuple[OwnedTreeTargetV1] of length 1..64
      authorized_target_digest: Hex64
      generation: strict positive integer
      current_attempt_id: lowercase hyphenated UUID | null
      current_attempt_revision: uint64 | null
      last_outcome:
        ISSUED | SAME_OWNER_SURVIVED | MEMBER_SURVIVED | EFFECT_UNPROVEN
    }
```

The initial action transaction creates generation one before `Stop-Tree`.
Each later residual action increments it by exactly one and preserves the
owner and authorized tuple. `current_attempt_id` and revision are either both
non-null with `last_outcome=ISSUED`, or both null with a failure outcome.
The pair is non-null if and only if core execution is
`TEARDOWN_IN_FLIGHT` or non-veto `TREE_CLOSURE_RELEASING`, with the exact same
attempt pair. `CLOSURE_VETOED` release has both debt attempt fields null.
Mismatch is invalid state and `POLICY_HELD`.

`TeardownDebtV1` is inert persisted evidence and is well formed only inside a
`ChildlessEffectEnvelopeV1` whose binding matches its owner and recomputed
authorized target digest. Neither an ordinary observation nor a raw debt value
can clear or rewrite it. Every debt mutation is carried by a
`PermitBoundChildlessMutationV1`; the payload and its banked `debt_id` formula
remain unchanged.

At the initial arm, `debt_id` is SHA-256 over
`agenttalk.supervisor.childless-teardown-debt.v1\0` plus canonical
`{"initial_attempt_id": ..., "initial_authority_id": ...,
"owner_identity_id": ..., "state_epoch": ...,
"target_digest": ...}`. It never changes during residual completion.

### Chained digest conformance vector

**FROZEN CONFORMANCE EVIDENCE:** The following Revision 8 fixture is retained
unchanged through Revision 12. It fixes all seven module digest domains and renews the
authority-dependent chain for merged #120's
`win-tree/v2` adapter and the explicit representation-token/exact-guard split.
The two banked core condition fingerprints are outside this chain and remain
untouched.
Each payload is the exact one-line ASCII/UTF-8 `CanonicalJsonV1` byte sequence
shown, with no trailing LF. The hash input is the displayed ASCII domain,
one NUL byte, then the payload bytes. Later payloads use earlier expected
digests. The displayed bytes, byte counts, and digests are fixed
interoperability and change-detection anchors; they are not independent proof
that an implementation selected the correct typed field set or implemented
`CanonicalJsonV1` correctly. That proof comes from the independent typed-object
construction required by conformance item 20. The upstream byte-flip control
then verifies change propagation only.

| Value | Domain | Payload bytes | Expected lowercase SHA-256 |
| --- | --- | ---: | --- |
| `owner_identity_id` | `agenttalk.supervisor.owned-wrapper-identity.v1` | 475 | `dc2ec1dfec8ffc8cc405ec1713bc82392ecd09a07d9a069f7f11c125ef64b2c7` |
| `target_digest` | `agenttalk.supervisor.owned-targets.v1` | 432 | `d6812412d8e4e97ca2ce99ff7e502de5ec82c8a74fff5f46c650960f5da8b459` |
| `process_source_digest` | `agenttalk.supervisor.owned-tree-coverage-source.v1` | 296 | `a0761b7c59c6ccb30b50a3a76c0364bca6155e9c62d20e9fe4fe942711b53413` |
| `owned_childless_basis_digest` | `agenttalk.supervisor.owned-childless-confirmation-basis.v1` | 988 | `2c5422c20ab4982928c678947c69162d94d996e5d41c8179a62a781abb107789` |
| `basis_id` | `agenttalk.supervisor.childless-teardown-basis.v1` | 1,531 | `e03f4ea1f99aaf85411dfac6ca805c8ee8e5dbd0f95071ed6ed5d3d0b629e1a9` |
| `authority_id` | `agenttalk.supervisor.provably-childless-owned-wrapper-authority.v1` | 236 | `42fe64c496d362d278bcd1d99ae2f441b7272441aff359e4233d2cf93de2c433` |
| `debt_id` | `agenttalk.supervisor.childless-teardown-debt.v1` | 374 | `79aeba9dd6c25c15513890268c83a426bc20b68520c42eb900952cdc1640a4ed` |

`owner_identity_id` payload:

```json
{"agent_key":"agent-4","launch_nonce":"Nonce_0123456789AB","managed_generation":"mg-7","nonce_provenance":{"checked_managed_launch_nonce":"Nonce_0123456789AB","observed_parser_schema":"supervisor-launch-nonce/v1","parsed_observed_root_launch_nonce":"Nonce_0123456789AB"},"runtime_wrapper_generation":"wg-9","state_epoch":"11111111-2222-3333-4444-555555555555","wrapper_pid":4242,"wrapper_start_guard":"638920000000000000","wrapper_start_token":"3625-08-28T17:46:40.0000000Z"}
```

`target_digest` payload:

```json
{"owner_identity_id":"dc2ec1dfec8ffc8cc405ec1713bc82392ecd09a07d9a069f7f11c125ef64b2c7","schema":"owned-targets/v1","targets":[{"depth":0,"owner_launch_nonce":"Nonce_0123456789AB","parent_pid":null,"parent_start_guard":null,"pid":4242,"start_guard":"638920000000000000"},{"depth":1,"owner_launch_nonce":"Nonce_0123456789AB","parent_pid":4242,"parent_start_guard":"638920000000000000","pid":5151,"start_guard":"638920000000000111"}]}
```

`process_source_digest` payload:

```json
{"ambiguity_scan_schema":1,"command_line_coverage":"complete","pid_start_guard_schema":2,"platform":"WINDOWS","process_row_schema":1,"process_source":"WIN32_PROCESS_CIM","recorded_identity_coverage":"complete","schema":"wrapper-observer-coverage/v1","wait_parser_schema":1,"wrap_parser_schema":1}
```

`owned_childless_basis_digest` payload:

```json
{"active_child_config_digest":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","child_establishment_guard":{"active_age_grace_through_epoch_ms":2030000,"active_record_updated_at_epoch_ms":2000000,"close_evidence_epoch_ms":2120000,"generation_launch_grace_until_epoch_ms":2120000,"key":{"cli_launcher_pid":6001,"cli_launcher_start_guard":"638920000000000222","managed_generation":"mg-7","runtime_wrapper_generation":"wg-9","state_epoch":"11111111-2222-3333-4444-555555555555","turn_generation":12},"result":"NONRENEWABLE_GRACE_EXPIRED","variant":"CLOSED"},"coverage":{"observer_version":"win-tree/v2","ownership_rule_version":"owned-tree/v2","process_source_digest":"a0761b7c59c6ccb30b50a3a76c0364bca6155e9c62d20e9fe4fe942711b53413"},"owner_identity_id":"dc2ec1dfec8ffc8cc405ec1713bc82392ecd09a07d9a069f7f11c125ef64b2c7","runtime_child_dead_basis_digest":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","schema":"owned-childless-confirmation-basis/v1"}
```

`basis_id` payload:

```json
{"active_child_config_digest":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","child_establishment_guard":{"active_age_grace_through_epoch_ms":2030000,"active_record_updated_at_epoch_ms":2000000,"close_evidence_epoch_ms":2120000,"generation_launch_grace_until_epoch_ms":2120000,"key":{"cli_launcher_pid":6001,"cli_launcher_start_guard":"638920000000000222","managed_generation":"mg-7","runtime_wrapper_generation":"wg-9","state_epoch":"11111111-2222-3333-4444-555555555555","turn_generation":12},"result":"NONRENEWABLE_GRACE_EXPIRED","variant":"CLOSED"},"debt_generation":null,"debt_id":null,"mode":"INITIAL","owned_childless_basis_digest":"2c5422c20ab4982928c678947c69162d94d996e5d41c8179a62a781abb107789","owner_identity_id":"dc2ec1dfec8ffc8cc405ec1713bc82392ecd09a07d9a069f7f11c125ef64b2c7","runtime_child_dead_basis_digest":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","source_capture_id":{"agent_key":"agent-4","capture_ordinal":0,"ordinary_poll_sequence":41,"state_epoch":"11111111-2222-3333-4444-555555555555"},"source_committed_revision":77,"source_condition_fingerprint":"recovery-condition-v1:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff","source_coverage":{"observer_version":"win-tree/v2","ownership_rule_version":"owned-tree/v2","process_source_digest":"a0761b7c59c6ccb30b50a3a76c0364bca6155e9c62d20e9fe4fe942711b53413"},"state_epoch":"11111111-2222-3333-4444-555555555555","target_digest":"d6812412d8e4e97ca2ce99ff7e502de5ec82c8a74fff5f46c650960f5da8b459"}
```

`authority_id` payload:

```json
{"basis_id":"e03f4ea1f99aaf85411dfac6ca805c8ee8e5dbd0f95071ed6ed5d3d0b629e1a9","mode":"INITIAL","schema":"provably-childless-owned-wrapper-authority/v1","target_digest":"d6812412d8e4e97ca2ce99ff7e502de5ec82c8a74fff5f46c650960f5da8b459"}
```

`debt_id` payload:

```json
{"initial_attempt_id":"99999999-aaaa-bbbb-cccc-dddddddddddd","initial_authority_id":"42fe64c496d362d278bcd1d99ae2f441b7272441aff359e4233d2cf93de2c433","owner_identity_id":"dc2ec1dfec8ffc8cc405ec1713bc82392ecd09a07d9a069f7f11c125ef64b2c7","state_epoch":"11111111-2222-3333-4444-555555555555","target_digest":"d6812412d8e4e97ca2ce99ff7e502de5ec82c8a74fff5f46c650960f5da8b459"}
```

Outstanding debt has precedence over every new owner, whole-wrapper absence,
manual marker, and relaunch-only proof. A complete observation may:

- clear debt without an OS action only for `COMPLETE_GONE` and only through a
  fresh matching `EFFECT_FINALIZE` permit-bound mutation;
- construct `DEBT_COMPLETION` authority only for an exact live residual subset
  from `COMPLETE_RESIDUAL`, then require a new post-reservation `HELD` closure;
  or
- retain debt and continuous attention for `INCOMPLETE`/changed membership.

`DEBT_COMPLETION` targets only the immutable live residual. This keeps an
orphaned conhost/shell/tool reachable after the wrapper root exits. Debt alone
never authorizes a new identity.

Every finalized childless reservation/attempt outcome or permit-bound debt
reconciliation from ordinary residual evidence writes
`recovery_poll_terminal_sequence=ordinary_poll_sequence`. Pure refusal,
retained closure uncertainty, prior-poll exhaustion, and a no-op
`NOT_ATTEMPTED` row that leaves debt/cycle/execution unchanged do not. The
`NOT_ATTEMPTED` emitted after successful permit-bound ordinary-evidence or
reload cleanup is
a finalized reconciliation and does write the terminal. The reservation
predicate rejects equality. The next ordinary-poll increment clears the
terminal. Thus one poll cannot consume two automatic attempts or turn
finalized reconciliation into immediate launch.

## Fail-closed state-loss quarantine

**SPECIFIED; implementation blocked on task #115:** A missing, corrupt, torn, or rollback-unproven
checked state is not clean genesis. Recovery creates a new `state_epoch` only
with `StateLossQuarantineV1.UNRESOLVED`; the physical owner projection
deliberately excludes that epoch and is diagnostic only. Quarantine creation is
an explicit task #115 transaction and the sole permit-free mutation that may
replace unknown childless effect state: it creates the new epoch and
`UNRESOLVED` together, grants no authority, and makes no external call. The
quarantine denies the named teardown,
every other kill, every launch/relaunch, closure acquisition, attempt
increment/reset, debt clear, marker consumption, managed-owner commit, and
grace-based recovery. It emits continuous
`CHILDLESS_STATE_PROVENANCE_LOST` attention. Manual acknowledgement or force
cannot override it.

This revision intentionally defines no exact-restoration constructor. The
shipped store may accept a structurally valid backup that is one committed
generation behind; schema validity therefore cannot prove that an apparent
restoration contains the lost latest attempt count or teardown debt. Such a
backup remains `ROLLBACK_UNPROVEN` and quarantined. Adding an independently
retained last-commit revision/digest would change the persistence contract and
requires a separately reviewed versioned design.

Revision 12 defines no automatic V1 quarantine-clear transaction. The previous
`ProvablyDifferentPhysicalOwnerV1`/local `COMPLETE_GONE` carve-out is withdrawn:
`OwnedPhysicalWrapperIdentityV1` has no host/process-universe operand, and a
complete local absence on host B cannot prove that the copied prior wrapper or
erased-debt members are extinct on host A. The type is host-local evidence, not
a globally unique owner identity. `retirement_capability` therefore remains
`CAPABILITY_UNAVAILABLE(PROCESS_UNIVERSE_IDENTITY_UNAVAILABLE)` on every
platform, even when a distinct local replacement exists.

A future version may add retirement only if it persists a trustworthy source
host/process-universe binding before loss and obtains extinction coverage for
that exact same universe from a reviewed read-only producer over an existing OS
token. Unknown, unreadable, transferred, or mismatched universe identity must
remain non-retirable. PID and start, hostname, `state_epoch`,
`process_source_digest`, MachineGuid alone, local absence, or any combination
of those facts is insufficient. The producer must satisfy the absolute M5
boundary: no new file, registry value, helper, daemon, OS object, persistence
plane, or runtime dependency.

Until such a successor is delivered, `UNRESOLVED` remains sticky pending
attended handling. Ordinary local observations may continue for diagnostics,
but they cannot construct a retirement mutation, clear unknown debt/cycle or
effect state, commit a replacement owner, kill, or launch.

State loss after issued attempt one, two, or three therefore cannot recreate a
fresh three-attempt budget. State loss after debt arm or any partial target
attempt cannot erase debt precedence or become locally clearable. Task #115
must distinguish clean initial creation from loss of an expected state object;
implementations that cannot make that distinction fail closed in the permanent
V1 quarantine.

## Bounded automatic cycle

```text
AutomaticChildlessCycleV1 =
  NONE
  | CYCLE {
      status: ACTIVE | EXHAUSTED
      owner: OwnedWrapperIdentityV1
      owner_identity_id: Hex64
      issued_attempts: integer 1..3
      last_attempt_id: lowercase hyphenated UUID
      last_attempt_revision: uint64
      last_outcome:
        ISSUED | CLOSURE_VETOED | SAME_OWNER_SURVIVED
        | MEMBER_SURVIVED | EFFECT_UNPROVEN
}
```

Within an existing cycle, `EXHAUSTED` exists if and only if
`issued_attempts=3` with a typed failure. `ACTIVE` with a typed failure permits
only one or two issued attempts; `ACTIVE/ISSUED` permits one through three.
`ISSUED` exists if and only if an automatic named childless execution phase
owns the exact same owner, last attempt ID, and revision. The next attempt
transition is only `NONE -> 1` or same-owner
`ACTIVE/failure(n) -> ACTIVE/ISSUED(n+1)`. Every other shape is invalid and
`POLICY_HELD`.

An automatic attempt is consumed by the checked transition that records
`TREE_CLOSURE_ACQUIRING` with its attempt ID immediately before invoking the
closure successor after exact `ClosureCapabilityV1.AVAILABLE`. Planning,
policy holds, and structural `CAPABILITY_UNAVAILABLE` consume zero. A
post-reservation claim of structural unavailability is malformed and retains
the issued fence/cycle without becoming `CLOSURE_VETOED`, a retryable outcome,
or exhaustion. Only the displayed transient closure failures enter the bounded
cycle, so they cannot loop outside the cap. This cycle does not read or mutate
generic `consecutive_fails`, recovery-backoff deadline, or backoff exponent
fields. After a typed transient failure on attempts one or two, the next
attempt may be reserved only by the next eligible ordinary poll after the
same-poll terminal clears; all ordinary gates are reevaluated. It is neither
scheduled by nor hidden behind exponential backoff. Attempt three plus any
displayed transient failure becomes sticky `EXHAUSTED`: it schedules no fourth
automatic reservation.

`EXHAUSTED` emits `AUTOMATIC_CHILDLESS_RETRY_EXHAUSTED` and mandatory action
attention on every poll until a successful childless cleanup, a
freshly committed different guarded owner while quarantine and debt are both
`NONE`, or a successful authorized manual cleanup clears the cycle. The
different-owner clear is an `OWNER_TRANSITION` permit-bound mutation over the
old envelope plus the exact new guarded checkpoint; it is not an observation
delta or a generic identity write. A mere new
`state_epoch` or apparently new guarded owner after state loss cannot clear
it. It suppresses only a further automatic named childless teardown for that
owner;
it does not suppress the banked independently confirmed whole-wrapper-absence
relaunch path. A new manual request may wrap the same module proof after
exhaustion; manual failure does not increment or rewrite the automatic cycle.
This bounds destructive work while preventing the signal from fading as
retries become rarer.

An `ACTIVE` cycle whose last outcome is a typed failure emits
`AUTOMATIC_CHILDLESS_RETRY_ACTIVE` on every intervening poll. This makes failed
attempts one and two continuously visible rather than visible only when the
next attempt happens to be eligible.

87-B owns durable human routing and receipt. 87-A ENFORCES the continuous typed
signal; it does not falsely claim that a human received it.

## Core combiner overlay

The core applies these rules before generic candidate selection:

```text
if state_loss_quarantine is UNRESOLVED:
  every teardown and relaunch proof = NONE

else if teardown_debt is OUTSTANDING:
  every different-owner and relaunch-only proof = NONE
  selected teardown =
    DEBT_COMPLETION only if the exact residual authority is constructed
    else NONE

else if child_death_sourced_dominant:
  selected teardown =
    PROVABLY_CHILDLESS_OWNED_WRAPPER
      only if childless_source
      and the named authority is constructed
    else NONE

if automatic cycle is EXHAUSTED:
  automatic named childless teardown = NONE
  independently confirmed whole-wrapper-absence relaunch is unchanged
```

There is no generic fallback for the entire child-death-sourced subset, not
merely for the subset whose module overlay has already reached count two. For
example, if poll one has complete child absence but an incomplete owned-tree
capture, and poll two makes the banked child-death counter reach two while the
module overlay reaches only one, `child_death_sourced_dominant` is true,
`childless_source` is false, and the result is `HOLD`; generic
`STRICT_RUNTIME_TEARDOWN` cannot bypass the nonce/tree proof. A blocked proof
likewise resolves `HOLD`. Manual-wins changes origin and reservation ID, not
the module proof, closure, target tuple, debt, or safety checks.

With debt `NONE`, a confirmed `PhysicalAbsenceProofV1` is a no-kill
`RELAUNCH_ONLY` candidate before the live-wrapper child-death named-authority
gate. Thus an absent wrapper with confirmed absence is not suppressed merely
because the last strict runtime evidence was child-death-sourced. Automatic
and manual origins make the same no-kill choice; the named case constrains only
the branch that would actually terminate a present wrapper. Outstanding debt
still precedes and suppresses that relaunch, preventing debt laundering.

The module exports:

```text
ChildlessActionResultV1 =
  NOT_ATTEMPTED
  | POLICY_HELD
  | BARRIER_VETOED
  | TEARDOWN_FAILED
  | AUTOMATIC_RETRY_EXHAUSTED

ChildlessAttentionCodeV1 =
  CHILDLESS_STATE_PROVENANCE_LOST
  | CAPABILITY_UNAVAILABLE
  | CHILDLESS_OWNER_CHILD_TREE_OR_CLOSURE_INCOMPLETE
  | CHILDLESS_TEARDOWN_DEBT
  | AUTOMATIC_CHILDLESS_RETRY_ACTIVE
  | AUTOMATIC_CHILDLESS_RETRY_EXHAUSTED

TeardownDebtSummaryV1 =
  NONE
  | OUTSTANDING {
      debt_id: Hex64
      owner_identity_id: Hex64
      authorized_target_count: integer 1..64
      generation: strict positive integer
      last_outcome:
        ISSUED | SAME_OWNER_SURVIVED | MEMBER_SURVIVED | EFFECT_UNPROVEN
    }

AutomaticChildlessCycleSummaryV1 =
  NONE
  | CYCLE {
      status: ACTIVE | EXHAUSTED
      owner_identity_id: Hex64
      issued_attempts: integer 1..3
      last_outcome:
        ISSUED | CLOSURE_VETOED | SAME_OWNER_SURVIVED
        | MEMBER_SURVIVED | EFFECT_UNPROVEN
    }
```

Attention codes appear in displayed order for every true predicate:

```text
CHILDLESS_STATE_PROVENANCE_LOST iff
  StateLossQuarantineV1 is UNRESOLVED

CAPABILITY_UNAVAILABLE iff
  ClosureCapabilityV1 is CAPABILITY_UNAVAILABLE
  or fresh reservation permit construction returns CAPABILITY_UNAVAILABLE
  or ChildlessEffectEnvelopeV1 exists and next-permit construction returns
     CAPABILITY_UNAVAILABLE for its binding/current-host witness
  or StateLossQuarantineV1 is UNRESOLVED and its retirement_capability is
     CAPABILITY_UNAVAILABLE
  or a post-reservation closure-successor value illegally claims structural
     unavailability
  or applicable reconciliation is UNKNOWN(CAPABILITY_UNAVAILABLE)

CHILDLESS_OWNER_CHILD_TREE_OR_CLOSURE_INCOMPLETE iff
  the relevant owner/child/tree/debt-residual constructor is BLOCKED/INCOMPLETE
  or a named childless acquisition reconciliation is UNKNOWN
  or a release reconciliation is HELD or UNKNOWN
  or an existing named childless phase is retained because safety
     reconciliation is not eligible
  or checked childless state, #120 snapshot, or closure-successor output is structurally invalid

CHILDLESS_TEARDOWN_DEBT iff teardown_debt is OUTSTANDING

AUTOMATIC_CHILDLESS_RETRY_ACTIVE iff
  automatic_childless_cycle.status == ACTIVE
  and automatic_childless_cycle.last_outcome != ISSUED

AUTOMATIC_CHILDLESS_RETRY_EXHAUSTED iff
  automatic_childless_cycle.status == EXHAUSTED
```

No code is emitted merely because a synchronous valid closure is briefly
held. A retained acquiring/releasing uncertainty, debt, or failed/exhausted
cycle is emitted on every poll, including polls where other policy gates hold.

87-B must join an action resolution to the exact matching fingerprint and take
the held agent name from `RecoveryConditionV1.canonical_condition.agent_key`.
Every routine or incident rendering of `CAPABILITY_UNAVAILABLE` must name that
agent and state explicitly that operator action is required. Across 87-B
projections, 87-C activation surfaces, and the required operator material, the
three permanent V1 capability limitations must remain distinct: POSIX named
teardown lacks an exact-token executor; automatic quarantine retirement lacks a
trustworthy process-universe identity; and declared same-platform state-
file/workspace transfer, restore, rollback, or migration must refuse before
active-store admission. A bare enum, an unnamed agent for either active hold, a
message that collapses either hold into a transient retry, or a silent
activation refusal is nonconforming. The activation refusal identifies the
rejected operation/store; it does not fabricate a held agent.

In the original surviving live invocation, a successful gone proof after
verified release has no terminal module result: the core's final barrier/spawn
determines `BARRIER_VETOED`, `SPAWN_FAILED`,
`IDENTITY_COMMIT_AMBIGUOUS`, or `LAUNCH_COMMITTED`. Reload or reconciliation
without that live continuation instead emits the table's terminal-writing
`NOT_ATTEMPTED` cleanup result and never launches.
The summaries are deterministic projections of checked state. They omit the
launch nonce, PID/start target tuple, and acquisition IDs; those authority
fields never cross the 87-B export boundary.

## Exact state transitions

The safety property is established by a closed construction pipeline, not by
enumerating every state that might enter it:

| Construction boundary | Only conforming operation |
| --- | --- |
| Deserialize | Produce inert `ChildlessEffectEnvelopeV1` evidence. Deserialization never yields a witness, permit, executable target, mutation, call, or receipt. |
| Ordinary observation | The installed observer adapter atomically yields one `OrdinaryObservationCommitCustodyV1` pairing a sealed `OrdinaryClassifierObservationReceiptV1` with its exact lineage owner. #115 accepts only that custody handle; inside the checked RMW the owner alone derives and commits the private observation mutation, which cannot address the effect envelope. |
| Validate | Validate the complete envelope and exact binding before consulting host capability. A malformed envelope deterministically yields `POLICY_HELD`; malformed state precedes capability-unavailable reporting. |
| Witness/preflight | Only #146's unexported dispatcher-capability factory may mint `CurrentExactTargetExecutorWitnessV1`, before any permit or call exists; merged raw `Stop-Tree`, non-childless variants, and every other layer may not. Static inability to serve the binding or an undelivered dispatcher seal yields capability hold before guard acquisition. |
| Guard and permit | Acquire the effect guard when required, create its one unique lineage, and atomically move sole custody `AVAILABLE -> OUTSTANDING` while matching witness, binding proof, checked revision, guard/continuation, and one operation/use. A second issuance from the acquisition is unconstructible. `REJECTED` yields zero effect plus reload/reject, not capability hold. |
| Childless-envelope mutation | Task #115 accepts only `PermitBoundChildlessMutationV1`; the owner algebra separately admits pure observation, private non-childless authority, and fail-closed quarantine-creation variants, none of which may address a current childless envelope. |
| External effect | A childless adapter accepts only a deeply sealed `ChildlessExternalEffectCallV1` and returns only its matching sealed receipt. Owned-tree termination additionally requires the exact private submission/owner pair and winning `CALL -> DISPATCHING`, `DISPATCHING -> PLAN_OWNED`, and `PLAN_OWNED -> INVOKING` transitions for the closed `CHILDLESS` variant; only the resulting invocation may enter the native body. The other two dispatch variants use the same atomic stages with their own private owners. |
| Receipt commit | A matching sealed permit and receipt may construct the next checked mutation. CHILDLESS first wins `RECEIPT -> PERMIT`; either non-childless consumer first wins `RECEIPT -> CONSUMING_RECEIPT`. Every normal or exceptional exit returns, closes, or poisons the exact owner once; concurrent same-reference aliases and sequential replay cannot advance any holder state twice, and mismatch, altered nested value, or a changed revision cannot act. |

Full-poll order is state-loss detection; dry-run discard or, for a non-dry-run
outer-state failure, exclusive fail-closed quarantine creation; current-
supervisor handling; #115 sealed-receipt ordinary observation commit; complete envelope validation;
fresh witness/static preflight; required effect-guard acquisition; then permit
and guarded mutation or call construction.
This resolves the former malformed-versus-unavailable precedence conflict,
preserves dry-run zero persistence, and makes the observation projection
explicit.

`ChildlessSafetyReconciliationGateV1` is total without manufacturing
capability. A malformed envelope produces the pure inert
`RETAIN_INVALID_FENCE` first. A valid envelope plus a live foreign
continuation or unavailable effect guard produces the corresponding pure inert
`RETAIN_*` value without a permit, mutation, or call. Only a `MAY_*` variant
requires and carries the displayed fresh operation/use-specific permit; no
`MAY_*` result exists when that permit is absent. `MAY_TAKEOVER` authorizes only a permit-bound no-call
mutation; after its CAS, the winner must obtain a new operation-scoped permit
before reconcile or release. Those adapters accept typed calls, not persisted
attempt or closure IDs. Kill switch, action latch, report membership, and
auto-restart may still block new recovery while permitted non-destructive fence
cleanup proceeds. Unknown evidence retains the envelope.

This rule covers `PRE_BARRIER`, closure phases, teardown, debt-only `IDLE`,
retired attempts, `SPAWN_IN_FLIGHT`, `AMBIGUOUS_LAUNCH`, live resume, reload,
takeover, and future childless-origin variants within one supported V1 store
activation without making that list the proof. Every one lives inside the
envelope. A state not recognized by a permit/call constructor is inert by
construction.

This is not a same-platform host-transfer proof. Same-platform state-
file/workspace transfer, restore, rollback, and migration activation are
unavailable in V1; 87-C owns those contracts. Because the persisted executor
binding intentionally has no trustworthy host/process-universe operand, mere
equality of platform, executor-contract text, PID/start, or binding digests
cannot make copied state a conforming migration. When a conforming activation
path is told, or otherwise knows, that checked state came from one of those
operations, it must refuse before admitting or decoding those bytes as the
active checked store. The refusal constructs no witness, permit, authority/
effect mutation, external call, or launch. It directs attended handling and is
not an active-agent `CAPABILITY_UNAVAILABLE`/`POLICY_HELD` result.

An out-of-band copy or overwrite that bypasses that activation boundary may be
indistinguishable from an ordinary same-store reload and may therefore be
treated as local checked state. That deployment is nonconforming and receives
no 87-A safety or recovery guarantee. If existing outer-state checks classify
the replacement as rollback-unproven, they may create only the fail-closed
quarantine defined above; 87-A does not promise that every copied store is
detectable. A future 87-C design must either bind the source universe with a
reviewed existing-OS-token mechanism inside M5 Option A or keep transferred
state inert.

Task #115 adds the core's
`TREE_CLOSURE_RELEASING` phase and persisted
`childless_pending_disposition`. Beginning acquisition consumes an automatic
attempt and records `ACTIVE/ISSUED`. Once a closure exists, every veto or
post-action observation is first persisted as releasing with the exact
attempt/closure pair and one pending disposition. Release is then requested
with that pair. While reconciliation is `HELD` or `UNKNOWN`, execution remains
releasing, debt/current-attempt and automatic `ISSUED` remain unchanged, the
reservation remains owned, and neither termination nor launch may occur.

Only exact `RELEASED` may finalize the pending disposition:

- `CLOSURE_VETOED` releases the reservation without changing debt. Automatic
  origin records that cycle outcome and exhausts exactly when the issued
  attempt is three; manual origin leaves the cycle byte-identical.
- `SAME_OWNER_SURVIVED`, `MEMBER_SURVIVED`, or `EFFECT_UNPROVEN` retains debt,
  clears its current-attempt pair, records that exact debt outcome, releases
  the reservation, and for automatic origin records the same cycle outcome and
  exhausts exactly on attempt three. Manual origin leaves the cycle
  byte-identical.
- `COMPLETE_GONE` clears debt and the old-owner cycle only through the matching
  `EFFECT_FINALIZE` permit-bound mutation. Only the original live invocation
  may normalize the reservation to `PRE_BARRIER` and continue to the fresh
  final barrier; reload finalization enters `IDLE` with no launch.

Each finalized branch writes the same-poll terminal. Failure attempts one and
two schedule no generic backoff; a later retry begins only on a subsequent
eligible ordinary poll.

An ordinary residual observation is pure captured input, not a debt/cycle
mutation by the owner-private ordinary-observation projection. Its
`COMPLETE_GONE` clear is a
separate `PermitBoundChildlessMutationV1` and may commit only when
`ExecutionEligibilityV1 == ELIGIBLE`, a fresh permit for
`EFFECT_FINALIZE` exists, childless execution is `IDLE`, and there is no
closure, pending disposition, or debt current attempt. The observation commits
before permit construction and may update only ordinary observation fields.
If no permit exists, the envelope including debt/cycle/execution remains exact
while ordinary sequence/ordinal/terminal bookkeeping may change. `DRY_RUN` may
simulate but persists neither delta. A persisted childless phase remains inside
the envelope and cannot be erased by an ordinary observation.

The event constructor evaluates the following rows top to bottom and returns
the first applicable row; each later row therefore includes the negation of
all earlier predicates. After the explicit permit-failure rows, every row that
changes the effect envelope or invokes an adapter implicitly requires the
operation-specific permit, and every returned-result row requires the matching
typed receipt. The resulting event/result mapping is disjoint and exhaustive:

| Event at this poll | State/debt/cycle effect | Module result | Required visibility |
| --- | --- | --- | --- |
| `DRY_RUN` with any outer-state or envelope condition | Compute only the simulated fail-closed result; persist no observation, quarantine, envelope, terminal, or other delta and make no external call. | `POLICY_HELD` when state is lost/invalid/unavailable; otherwise the pure simulated result. | Simulated codes only; no persisted visibility claim. |
| Non-dry-run expected checked state is missing, corrupt, torn, or rollback-unproven and quarantine has not yet been created | In one fail-closed task #115 transaction create a new epoch plus `StateLossQuarantineV1.UNRESOLVED`; construct no permit, effect mutation, external call, replacement owner, or launch. | `POLICY_HELD` | `CHILDLESS_STATE_PROVENANCE_LOST` continuously. |
| `StateLossQuarantineV1` is `UNRESOLVED` | Retain quarantine indefinitely in V1; ordinary diagnostic observation may advance, but automatic retirement, every kill/launch, closure acquisition, attempt/debt mutation, identity commit, and manual override are unavailable. | `POLICY_HELD` | `CHILDLESS_STATE_PROVENANCE_LOST` and `CAPABILITY_UNAVAILABLE` continuously; any recoverable debt/cycle codes remain visible. |
| `ChildlessEffectEnvelopeV1` is structurally invalid, or #120/closure output is malformed, including a post-reservation structural-unavailability claim | Malformed wins before witness/permit construction. Retain the exact effect envelope; ordinary observation-only fields may already have advanced. Construct no call or effect-owned mutation and do not retry/exhaust. | `POLICY_HELD` | Incomplete code, plus capability/debt/cycle codes by predicate; pending a human. |
| Well-formed owner/child/tree/debt proof is incomplete before reservation | No reservation and no attempt consumed. | `POLICY_HELD` | Incomplete code; plus debt/cycle codes by predicate. |
| `ClosureCapabilityV1` is `CAPABILITY_UNAVAILABLE` before reservation | Static capability refusal: create no reservation or continuation, consume no attempt, and make no external call. | `POLICY_HELD` | `CAPABILITY_UNAVAILABLE` continuously pending a human; plus debt/cycle codes by predicate. |
| A valid `ChildlessEffectEnvelopeV1` exists and next-permit construction returns `CAPABILITY_UNAVAILABLE` | Retain the complete effect envelope byte-identically. Ordinary observation-only fields may advance. Construct no takeover, reconcile/release, capture reservation, finalization, `Stop-Tree`, spawn, or identity commit. | `POLICY_HELD` | `CAPABILITY_UNAVAILABLE` continuously pending a human; plus debt/cycle/incomplete codes by predicate. |
| A fresh named proof's reservation-permit construction returns `CAPABILITY_UNAVAILABLE` | Create no effect envelope, reservation, continuation, attempt, cycle, debt, executable target, or external call. | `POLICY_HELD` | `CAPABILITY_UNAVAILABLE` continuously pending a human. |
| Permit construction returns `REJECTED` | Construct no mutation or call. A stale revision reloads/re-reduces; invalid or replayed private operands are rejected and cannot become a capability hold. | `NOT_ATTEMPTED` unless same-invocation re-reduction reaches another row. | Existing codes only; no `CAPABILITY_UNAVAILABLE` solely from rejection. |
| A permitted envelope has childless execution `IDLE`, no closure/pending disposition/current attempt, and ordinary residual evidence is `COMPLETE_GONE` | A permit-bound mutation clears debt and old-owner cycle, remains childless `IDLE`, and writes the terminal; construct no launch. | `NOT_ATTEMPTED` | No childless code after the atomic clear. |
| No in-flight childless phase and a global/policy gate holds or no named candidate exists; cycle is `NONE` | No module mutation or terminal write. | `NOT_ATTEMPTED` | Debt/incomplete code only when its predicate independently applies. |
| No in-flight childless phase and a global/policy gate holds or no named candidate exists; cycle is `ACTIVE` with a prior typed failure | Preserve the cycle; no attempt, backoff, or terminal mutation. | `NOT_ATTEMPTED` | Active code; debt/incomplete code by predicate. |
| No in-flight childless phase and a global/policy gate holds or no otherwise-eligible automatic named proof exists; cycle is `EXHAUSTED` | Preserve the cycle; no attempt, backoff, or terminal mutation. | `NOT_ATTEMPTED` | Exhausted code; debt/incomplete code by predicate. |
| Automatic named proof is otherwise eligible, no eligible manual origin wins, and its cycle was already `EXHAUSTED` before this poll | No reservation, attempt, backoff, or terminal mutation. | `AUTOMATIC_RETRY_EXHAUSTED` | Exhausted code; debt code if applicable. |
| A valid envelope plus permit has gate `MAY_TAKEOVER` | Apply the exact no-call takeover as a permit-bound mutation, then reload and mint a new operation-scoped permit. A CAS loser reloads without mutation. | No terminal module result yet. | Existing debt/cycle/incomplete codes by predicate. |
| A valid permitted envelope has any other retain-only safety gate | Retain the effect envelope exactly and perform no external call; ordinary observation-only fields may already have advanced. | `POLICY_HELD` | Incomplete code; plus debt/cycle codes by predicate. |
| A childless `PRE_BARRIER` reservation is vetoed or reloaded before closure acquisition | A permit-bound mutation releases that reservation, consumes no automatic attempt, preserves debt/cycle and marker semantics, and writes the terminal. | `BARRIER_VETOED` | Debt/cycle/incomplete code by predicate. |
| Live acquisition returns a well-formed `HELD` and the full joined evidence is valid | Persist `TREE_CLOSURE_HELD`; no outcome or terminal is finalized. | No terminal module result yet. | Codes from preexisting debt/cycle only. |
| Any well-formed matching `HELD` returned by acquisition/reconciliation is not action-ready, or recaptured evidence/gates make a persisted `TREE_CLOSURE_HELD` non-action-ready | Bind/retain its closure ID, persist `TREE_CLOSURE_RELEASING/CLOSURE_VETOED`, and request release; no outcome is finalized yet. The earlier live-valid row alone may continue toward teardown. | No terminal module result yet. | Incomplete code while release remains held; debt code if applicable. |
| `TEARDOWN_IN_FLIGHT` has `STOP_TREE/CALL_RETURNED` and the same checked continuation chain owns the effect guard | Reserve the next nonzero capture ordinal and arm the typed post-action observation. Its receipt mutation persists the mapped releasing disposition with `POST_ACTION_CAPTURE/CALL_RETURNED`; a later `CLOSURE_RELEASE/STATE_MUTATION` permit must replace that checkpoint with exact release. No outcome is finalized yet. | No terminal module result yet. | Debt code; incomplete also when the observation is incomplete. |
| `TREE_CLOSURE_RELEASING` reconciliation remains matching `HELD`, or any applicable acquisition/reconciliation is `UNKNOWN` | Retain phase, reservation, pending disposition, debt/current attempt, and automatic `ISSUED`; no terminal write. | `POLICY_HELD` | Incomplete code; debt code if applicable. |
| Acquisition returns no closure and matching reconciliation proves terminal `NEVER_ACQUIRED`; acquiring reconciliation proves matching `RELEASED` and binds its returned closure ID; held-phase reconciliation proves matching `RELEASED` after atomically recording pending `CLOSURE_VETOED`; or any persisted `TREE_CLOSURE_RELEASING/CLOSURE_VETOED` reaches exact `RELEASED` | Finalize `CLOSURE_VETOED`; retire the attempt in the same transaction; debt unchanged. A later unexpected `HELD` for that retired attempt is release-only. Manual origin always leaves the cycle byte-identical; automatic issued-attempt count 1–2 becomes `ACTIVE/CLOSURE_VETOED`; count 3 becomes `EXHAUSTED/CLOSURE_VETOED`. | Manual origin: `BARRIER_VETOED`; automatic count 1–2: `BARRIER_VETOED`; automatic count 3: `AUTOMATIC_RETRY_EXHAUSTED`. | Cycle code by resulting predicate; debt code if applicable. |
| Reload/takeover retained `STOP_TREE/CALL_RETURNED`, and its exact `CLOSURE_RECONCILE` receipt proves matching `RELEASED` before any post-action capture | Consume that receipt only through `EFFECT_FINALIZE`; conservatively finalize `EFFECT_UNPROVEN`, retire the attempt, retain debt, clear its current-attempt pair, enter childless `IDLE`, and perform no residual-capture call or launch. Manual origin leaves the cycle byte-identical; automatic issued-attempt count 1–2 becomes `ACTIVE/EFFECT_UNPROVEN`, count 3 becomes `EXHAUSTED/EFFECT_UNPROVEN`. A later ordinary poll may clear debt only through the existing debt-only `EFFECT_FINALIZE` scope and matching `COMPLETE_GONE`. | Manual origin: `TEARDOWN_FAILED`; automatic count 1–2: `TEARDOWN_FAILED`; automatic count 3: `AUTOMATIC_RETRY_EXHAUSTED`. | Incomplete, debt, and cycle code by predicate. |
| Post-action `COMPLETE_RESIDUAL` contains the wrapper root and exact release is proved | Finalize `SAME_OWNER_SURVIVED`. Manual origin leaves any cycle byte-identical; automatic issued-attempt count 1–2 becomes `ACTIVE`, count 3 `EXHAUSTED`. | Manual origin: `TEARDOWN_FAILED`; automatic count 1–2: `TEARDOWN_FAILED`; automatic count 3: `AUTOMATIC_RETRY_EXHAUSTED`. | Debt plus cycle code by predicate. |
| Post-action `COMPLETE_RESIDUAL` omits the wrapper root and exact release is proved | Apply the preceding row with `MEMBER_SURVIVED`. | Same origin/attempt-sensitive result as the preceding row. | Debt plus cycle code by predicate. |
| Post-action `INCOMPLETE` and exact release is proved | Apply the preceding row with `EFFECT_UNPROVEN`. | Same origin/attempt-sensitive result as the preceding row. | Incomplete, debt, and cycle code by predicate. |
| Live post-action `COMPLETE_GONE`, exact release, and the same checked continuation ID remains live and effect-guard-owning through finalization | Clear debt/cycle and continue only to the core final barrier. | No terminal module result; the core result owns the outcome. | Codes recomputed from cleared state. |
| Reload or reconciliation finalizes `COMPLETE_GONE` without that exact live checked continuation chain | Clear debt/cycle, enter `IDLE`, write the terminal, and do not launch. | `NOT_ATTEMPTED` | Codes recomputed from cleared state. |

A well-formed current-evidence change is the action-time veto row, not invalid
state. An ID/revision/owner/target/origin mismatch inside persisted checked
state is the invalid-state row, not a barrier veto. The automatic-attempt-three
row takes precedence over the generic veto/failure result only after exact
release finalizes that failure. Manual origin never consults the automatic
attempt ordinal: its veto is always `BARRIER_VETOED`, its teardown failure is
always `TEARDOWN_FAILED`, and its cycle is byte-identical. Reload finalization
uses the persisted origin and the same mapping.
`CLOSURE_VETOED` never means structural `CAPABILITY_UNAVAILABLE`: the latter is
either the zero-attempt pre-reservation row, a valid persisted envelope that the
current host statically cannot serve, or a malformed/unknown retained fence
whose applicable attention includes capability loss. It never reaches
automatic retry/exhaustion. A stale, copied, replayed, consumed, or mismatched
permit is instead `REJECTED` and cannot be relabelled as capability loss.
Rows marked “no terminal module result yet” are internal same-invocation
transitions and do not emit an action-resolution record at that point. The
invocation either reaches a later final row or returns with retained checked
state; the next poll then matches the applicable retained
`POLICY_HELD`/`NOT_ATTEMPTED` row. Every emitted module result is one closed
`ChildlessActionResultV1` value.

Normal execution is:

| Transition | Checked effect |
| --- | --- |
| Reserve named proof | With no same-poll terminal/current obligation, consume a fresh witness plus inert authority and one `RESERVE` permit. `INITIAL` creates the first envelope/binding. `CONTINUE` transforms a qualifying childless-`IDLE` envelope: initial-mode retry may rebind while preserving cycle/terminal historical tombstones; debt completion retains the immutable binding and exact residual subset. Both install `PRE_BARRIER` in one checked mutation. |
| Begin closure acquisition | Under the effect guard, a `STATE_MUTATION` permit live-recomputes the binding and persists acquiring plus an exact `ACTIVE_ATTEMPT/CLOSURE_ACQUIRE/ARMED` continuation. At the successor revision, a distinct fresh `EXTERNAL_CALL` permit constructs/invokes acquisition. Automatic origin creates/increments `ACTIVE/ISSUED`; manual origin does not change the cycle. |
| Acquire valid closure | The typed receipt plus a distinct fresh receipt-derived `RECEIPT_MUTATION` permit preserving the call issuance, and full joined equality, persist `TREE_CLOSURE_HELD`, closure ID, and `CALL_RETURNED`. No raw provider result or consumed call permit may do so. |
| Veto after `HELD` | A `STATE_MUTATION` permit arms exact release; after that CAS a fresh `EXTERNAL_CALL` permit constructs/invokes it, and only a fresh receipt-derived permit preserving the call issuance may finalize `RELEASED`. Failure at any construction point retains the envelope. |
| Arm teardown | A `STATE_MUTATION` permit atomically enters `TEARDOWN_IN_FLIGHT`, creates/updates debt, and writes `STOP_TREE/ARMED`; after its lineage custody returns, a fresh successor-revision `EXTERNAL_CALL` permit constructs the sealed target set and childless call, which #146's private CHILDLESS-variant constructor wraps for the closed dispatcher. Only a fresh matching receipt-derived permit over the receipt-held lineage and same call issuance writes `CALL_RETURNED`. |
| Observe action effect | A `POST_ACTION_CAPTURE/STATE_MUTATION` permit at `STOP_TREE/CALL_RETURNED` allocates the next nonzero ordinal and arms the typed capture; a fresh post-CAS call permit obtains its receipt, and a fresh receipt-derived mutation permit preserving that call issuance maps the observation into releasing state with `POST_ACTION_CAPTURE/CALL_RETURNED`, without clearing debt/current attempt or cycle `ISSUED`. A later `CLOSURE_RELEASE/STATE_MUTATION` permit alone may arm release. |
| Finalize release | A matching release receipt and finalize permit apply the disposition, bind the retired-attempt tombstone, and only then clear the continuation/release the guard. |
| Spawn and identity commit | The same envelope/binding persists through `PRE_BARRIER`, `SPAWN_IN_FLIGHT`, and `AMBIGUOUS_LAUNCH`. A state-mutation permit first installs `SPAWN_RESERVATION/SPAWN/ARMED`; only a distinct fresh post-CAS call permit invokes spawn, and only its receipt plus a fresh receipt-derived mutation permit preserving that call issuance commits identity/ambiguity. The sole receipt-free conversion is the closed crash-only `SPAWN_RESULT_COMMIT` scope over the persisted issuer subject and positive issuer-death proof. |
| New guarded owner commits | Permitted only with quarantine `NONE`, debt `NONE`, and no childless effect envelope requiring cleanup. A quarantined state cannot commit a replacement owner automatically in V1. |

Crash/reload is equally closed. Deserialization yields only the inert envelope;
the full-poll precedence above handles state loss, observation, quarantine, and
malformed state without constructing an effect object. A valid envelope can
advance only after the current caller obtains a fresh witness, passes static
executor preflight, acquires the effect guard when the operation requires it,
and constructs the exact operation permit. `CAPABILITY_UNAVAILABLE` at preflight
selects the retained-envelope row; `REJECTED` after guard acquisition performs
zero effect, releases the guard, and follows its reload/reject rule. This order
also applies to a phase inherited from another platform.
`MAY_TAKEOVER` itself carries a `TAKEOVER` permit, first owns the effect guard,
proves a persisted predecessor unable to resume, and commits only the no-call
takeover mapping. The winner must reload and construct a new operation permit;
only then may these phase-specific operations run:

- `TREE_CLOSURE_ACQUIRING`: write `CLOSURE_RECONCILE/ARMED`, reconcile by
  attempt ID, and retain the guard through the result CAS. Matching
  `NEVER_ACQUIRED` terminally retires the attempt and finalizes
  `CLOSURE_VETOED`; a later unexpected `HELD` is release-only. Matching
  `RELEASED` binds its returned closure ID, retires the attempt, and finalizes
  the same veto. Matching `HELD` is persisted as releasing/vetoed and released
  without termination. `UNKNOWN` retains state and the retired set is
  unchanged.
- `TREE_CLOSURE_HELD`: do not terminate. Replace the takeover checkpoint only
  with `CLOSURE_RECONCILE/ARMED`. A matching `RELEASED` may finalize the veto;
  matching `HELD` requires a later, distinct `CLOSURE_RELEASE/STATE_MUTATION`
  arm and exact idempotent release; `UNKNOWN` retains the held state.
- `TREE_CLOSURE_RELEASING`: replace the takeover checkpoint only with
  `CLOSURE_RECONCILE/ARMED`. Finalize the already persisted disposition on
  matching `RELEASED`; matching `HELD` may proceed only through a later,
  distinct `CLOSURE_RELEASE/STATE_MUTATION` arm, and `UNKNOWN` retains state.
- `TEARDOWN_IN_FLIGHT` with `STOP_TREE/ARMED`: never reissue `Stop-Tree`, never
  infer that it did or did not run, and never infer action completion. After
  predecessor-death proof, persist `EFFECT_UNPROVEN`, retain debt, and release
  any exact closure only after the takeover checkpoint is replaced by a distinct
  `CLOSURE_RELEASE/STATE_MUTATION` arm. No branch launches.
- `TEARDOWN_IN_FLIGHT` with `STOP_TREE/CALL_RETURNED`: takeover writes the inert
  `STOP_TREE/TAKEOVER_CHECKPOINT/RECONCILER` owner while preserving the returned
  effect fact. After a distinct `CLOSURE_RECONCILE/STATE_MUTATION` arm and
  matching reconciliation, it may obtain a fresh
  post-action observation under matching `HELD`, then atomically persist the
  mapped releasing disposition with `POST_ACTION_CAPTURE/CALL_RETURNED`. A
  later `CLOSURE_RELEASE/STATE_MUTATION` permit alone may replace it with
  `CLOSURE_RELEASE/ARMED`. If reconciliation instead proves matching
  `RELEASED`, consume that exact reconcile receipt through a fresh
  `EFFECT_FINALIZE` permit and conservatively finalize `EFFECT_UNPROVEN`: enter
  childless `IDLE`, retire the attempt, retain debt, clear its current-attempt
  pair, record the origin-sensitive failure/exhaustion result, and make no
  residual-capture call or launch. The next ordinary poll may clear that debt
  only through the existing debt-only `EFFECT_FINALIZE` scope and a matching
  `OwnedDebtResidualObservationV1.COMPLETE_GONE`. `UNKNOWN`
  retains reservation, debt/current attempt, and cycle `ISSUED`;
  `NEVER_ACQUIRED` is invalid after debt and retains the fence.

Every reload post-action capture atomically reserves the current
core `next_capture_ordinal` under the same checked continuation, then
increments it. Its `CaptureIdV1` has the current state epoch, agent, ordinary
poll sequence, and that exact nonzero ordinal; it is greater than any prior
capture for the attempt. The ordinary-observation `capture_ordinal == 0`
well-formedness rule does not apply to this explicitly nonordinary capture.
Exhaustion of the nonzero ordinal range is `INCOMPLETE` and retains the fence.

Task #115 must make every displayed state change one checked compare-and-swap
transaction; no executor branch may save a cached whole state.

## Mandatory conformance evidence

1. Build two adjacent distinct complete child-absence captures for one exact
   PID/start/nonce owner and construct the named authority. Replay, gap, one
   capture, owner/state/generation/PID/start/nonce change, `UNKNOWN`, unreadable
   child evidence, or incomplete tree must refuse. Feed third and fourth
   adjacent compatible captures and prove the saturated two-sample window
   slides each poll and keeps current authority deterministic. Pair a cached
   prior `capture_id` with a rewritten current separate sequence, change
   epoch/agent/ordinal independently, and corrupt the stored last-sequence
   binding; every case must refuse or hold as specified.
2. Permute process rows and add name/command-line false positives. Only exact
   guarded ownership and complete coverage may affect the result. Recycled PID,
    duplicate/malformed nonce, missing descendant, live foreign parent, cycle,
    truncation, and more than 64 targets refuse. Prove the depth-zero
    wrapper's live external supervisor/console parent is excluded from owned
    parent fields and does not refuse an otherwise complete initial tree.
3. Execute the closure successor's separate mechanism suite. Prove closure linearization,
   complete membership, no post-closure creation, no target termination by the
   closure, attempt-keyed acquire/reconcile/release, crash safety, and
   activated-platform compatibility. Until all pass, the static capability
   gate refuses before reservation with zero attempt and zero external call.
   Prove `CurrentExactTargetExecutorWitnessV1` can be minted only by #146's
   unexported dispatcher-capability factory, after that factory proves a
   conforming Windows dispatcher installation and before any permit or call
   exists. Input acceptance, parsing, deserialization, platform text, the
   checked store, a submitted call, and either non-childless variant must not
   expose or substitute for that factory. Require
   `ClosureCapabilityV1.AVAILABLE.reservation_permit.operation == RESERVE`,
   consume that permit exactly once when an initial or continuing reservation
   commits, and prove it cannot be persisted or reused for acquisition. Exercise
   initial creation, same-binding retry, changed-tree initial-mode rebind, and
   immutable-binding debt completion. Rebind must preserve terminal tombstones
   under their historical bindings and refuse any `RELEASE_PENDING` tombstone.
4. Revalidate after reservation. Owner, coverage, and target digest must match;
   initial mode additionally requires a fresh post-linearization nonordinary
   raw capture, same-capture complete child absence, exact current matcher
    config, and live reconstruction of both runtime and module basis digests.
    Change turn/phase/progress/config after reservation and substitute a stale
    pre-closure capture; each must veto before debt or `Stop-Tree`. Debt mode
    requires the exact current debt ID/generation. Reserve at both banked and
    module count two, then force a same-basis qualifying gap that restarts
    either counter at count one before closure; equal basis digests must not
    substitute for count two, and the action must veto before debt or
    `Stop-Tree`.
5. Prove the admitted Windows initial root-first tuple and inherited-order
   residual tuple can construct only the sealed
   `SupervisorOwnedTreeDispatchCallV1.CHILDLESS` variant plus its one privately
   paired submission/owner. Only the winning admission may compete for plan
   ownership, and only the resulting invocation's native-entry winner may
   project that private immutable plan into the unchanged
   leaves-first native termination semantics; no raw target-array entry remains.
   No process name/pattern, non-Windows token projection, or second supervisor
   owned-tree kill path may reach an authority target.
6. Failure-inject before and after the debt commit and after every target
   attempt. Partial kill retains debt and blocks every launch. A later ordinary
   `COMPLETE_RESIDUAL` observation—not a preexisting closure—constructs exact
    residual authority. Kill the wrapper first and prove an authorized orphan
    tool remains reachable under a new post-reservation closure. In a
    wrapper-to-shell-to-tool chain, remove both ancestors and prove the live
    tool retains its complete original parent/depth target object as an exact
    residual subset; recomputing it to depth one must refuse.
7. Cross automatic attempts 0/1/2/3, closure veto before `HELD`, survivor,
   incomplete effect, crash reconciliation, manual retry after exhaustion,
   complete gone, new owner, and no fourth automatic reservation/backoff.
    Attempts one/two retry only on a later eligible ordinary poll and do not
    mutate generic backoff. Cross `ACTIVE`/`EXHAUSTED` with no current candidate
    and every global gate; require `NOT_ATTEMPTED` with unchanged continuous
    attention. Manual veto/failure always uses manual result precedence and
    leaves either cycle byte-identical. Attention remains true on every
    incomplete, debt, active-failure, and exhaustion poll.
    For `CAPABILITY_UNAVAILABLE`, join the exact fingerprint to the canonical
    agent key and require every 87-B rendering to name the held agent and say
    operator action is required; a bare code must fail conformance.
    Seed a future generic recovery-backoff deadline and nonzero exponent with
    cycle `NONE`, `ACTIVE/failure`, and `EXHAUSTED`. An otherwise eligible
    named attempt must bypass that deadline for `NONE` and `ACTIVE`; exhaustion
    must still refuse a fourth attempt. Every case leaves generic backoff
    byte-identical. The same deadline must still hold a non-childless automatic
    candidate.
    Independently cross every `ClosureCapabilityV1` reason before reservation:
    require zero reservation, attempt, continuation, external call, teardown,
    retry, and exhaustion; emit continuous `CAPABILITY_UNAVAILABLE` and remain
    `POLICY_HELD` pending a human. Inject an illegal post-reservation structural
    unavailability claim and reconciliation
    `UNKNOWN(CAPABILITY_UNAVAILABLE)`; both must retain their exact fences,
    avoid `CLOSURE_VETOED`/retry/exhaustion, and require task visibility.
    Independently remove the current-host witness with a persisted reservation/
    phase, debt-only state, and retired-attempt cleanup. Static inability must
    emit exact capability attention, preserve the complete childless effect
    envelope exactly, and construct no permit-bound mutation or call. Then
    supply a fresh but mismatching witness for each shape: construction must be
    `REJECTED`, preserve the envelope, make no mutation/call, reload or reject,
    and must not manufacture operator-facing capability attention. Two adjacent
    polls may still advance only the closed ordinary-observation projection.
8. After every finalized childless reservation/attempt outcome and
   permit-bound debt reconciliation from ordinary residual evidence, attempt a
   second reservation in the
   same ordinary poll; the terminal must refuse it. Prove pure refusal,
   retained uncertainty, prior-poll exhaustion, and no-op `NOT_ATTEMPTED` do
   not write the terminal; successful cleanup `NOT_ATTEMPTED` does. Cross
   ordinary `COMPLETE_GONE` debt reconciliation with every
   `ExecutionEligibilityV1`: only `ELIGIBLE` together with a fresh matching
   `EFFECT_FINALIZE` permit clears and writes the terminal,
   `DRY_RUN` discards, and every other gate retains debt/cycle/attention. The
   clear must also require `IDLE` with no reservation, closure, pending
   disposition, or current attempt. Inject the same observation into every
   named persisted phase and prove its exact phase-specific reconciliation
   wins and no fence/debt is discarded. Replay a stale
   `OwnedDebtResidualObservationV1.COMPLETE_GONE` with a rewritten current
   separate sequence and prove `CAPTURE_ID_MISMATCH` retains debt. The next
   poll may derive fresh authority.
9. Cross manual/automatic overlap. Exactly one manual-origin action may wrap
   the same module proof; marker authority cannot replace owner/child/tree/
   closure proof.
10. Recompute the banked matrix counts and both fingerprint vectors
     byte-for-byte unchanged after integration.
11. Make the banked child-death counter reach two while the module overlay is
    only one by using an incomplete tree on the first poll. Both automatic and
    manual generic teardown must be suppressed; only `HOLD` is conforming.
12. Failure-inject after `HELD`, after post-action observation, before release,
    and after crash in every closure phase. Null/mismatched release IDs and
    `HELD`/`UNKNOWN` reconciliation retain the reservation, debt/current
    attempt, pending disposition, and automatic `ISSUED`. Only exact
    `RELEASED` finalizes; the first acquiring reconciliation crosses
    `NEVER_ACQUIRED`, `HELD`, `RELEASED`, and `UNKNOWN`, with matching
    `RELEASED` binding its closure ID and finalizing the veto. Reload
    complete-gone cleanup emits `NOT_ATTEMPTED`, writes the terminal, and never
    launches. Cross every persisted named phase with every execution gate:
    dry run and a non-current supervisor make no closure-successor call or
    effect-owned mutation. A missing/mismatching fresh witness preserves the
    complete childless effect envelope exactly while ordinary observation may
    advance its separate projection. A current supervisor with a valid
    operation permit under kill switch, disabled action latch, missing report
    membership, or disabled auto-restart may only
    reconcile/release/finalize the old fence and never acquire, kill, launch,
    consume a marker, increment an attempt, or change backoff/readiness.
13. Generate every debt-current-attempt/cycle/execution cross-product. Accept
    only the stated biconditionals, attempt transitions, same-owner equality,
    and origin rules. In particular reject `IDLE + debt ISSUED`,
    `IDLE + cycle ISSUED`, `ACTIVE/failure` at count three, `EXHAUSTED` below
    count three, and any skipped/repeated/fourth attempt as `POLICY_HELD`.
14. Hold one strict turn before child handoff and feed two adjacent complete
    zero-child captures inside its nonrenewable establishment window. Both must be
    `UNKNOWN(CHILD_ESTABLISHMENT_OPEN)`/`CURRENT_UNKNOWN_ACTIVE_CHILD`, both
    counters must remain empty, and neither origin may construct authority.
    At exactly active age 30 seconds the guard must remain open. With a longer
    checked generation launch fence, feed two more captures after 30 seconds
    but before that fence; both must still remain open. At the exclusive launch
    deadline, after the inclusive active-age grace has ended, the first absence
    is count one only. Separately close by current-turn adapter progress. Prove
    heartbeat/polls/rewritten same-key `updated_at` or changed same-key launch
    fence cannot renew or shorten either anchor. Phase-flip the same
    wrapper/turn to non-active and replay `ACTIVE`; the persisted keyed guard
    and anchors must remain byte-identical, while a same-turn changed key or
    missing retained guard stays `UNKNOWN`. Independently change every
    closed-guard field between reservation and action; each change vetoes before
    debt or `Stop-Tree`.
15. Remove and mismatch independently the checked managed nonce, parsed
    observed-root nonce, and fixed parser schema; each must refuse. Prove the
    strict runtime record supplies no nonce operand while its independent
    agent/PID/start/generation mismatches still refuse. A normalized top-level
    nonce without both retained provenance fields is malformed, never a
    substitute.
16. Run two pollers at both commit/effect gaps. Pause P1 after
    `CLOSURE_ACQUIRE/ARMED` and before the closure successor; P2 must retain while P1 can resume.
    Then prove P1 unable to resume, reconcile under P2, and cross
    `NEVER_ACQUIRED`, `HELD`, `RELEASED`, and `UNKNOWN`; `NEVER_ACQUIRED` must
    retire the ID and a forced late `HELD` must be release-only. Repeat with P1
    after `STOP_TREE/ARMED` before call, after the call before
    `CALL_RETURNED`, and after `CALL_RETURNED` before capture. Require no
    second `Stop-Tree`, no completion inference from `ARMED`, no release while
    a live foreign continuation can run, and no destructive call after
    authority ownership is released. From retained `STOP_TREE/CALL_RETURNED`,
    make reconcile return matching `RELEASED`; require receipt-bound
    `EFFECT_FINALIZE` to record `EFFECT_UNPROVEN`, retain debt, enter `IDLE`, and
    make no residual-capture call or launch. With a fresh matching
    `PRE_BARRIER_RELEASE` permit, reload `PRE_BARRIER` through its state-only
    release without an attempt owner; without that permit retain the envelope
    exactly. For every takeover phase,
    assert the exact no-call mapping and then only the closed table's exact next
    operation (`CLOSURE_RECONCILE`, `CLOSURE_RELEASE`, retired reconcile, or the
    crash-only spawn result as applicable); reject
    `STOP_TREE/ARMED/RECONCILER`. Repeat every paused state after
    inheriting it on Linux and macOS; current-host executor unavailability must
    win before takeover and make both pollers zero-call and zero-mutation.
17. Lose checked state after automatic attempt one, two, and three, after debt
    arm, and after a partial leaves-first target attempt. A new epoch and the
    same physical PID/start/nonce/generation must remain
    `STATE_PROVENANCE_LOST`, with no fresh budget, debt clear, kill, or launch.
    Prove V1 exposes no automatic retirement constructor at all: a distinct
    local owner, a structurally valid one-generation-old backup, or local
    absence of the old owner and every visible residual must each leave
    quarantine indefinitely. Attempt to supply PID and start, hostname,
    `state_epoch`, `process_source_digest`, MachineGuid alone, local absence,
    and combinations of those values; none may construct a retirement delta.
    Only attended handling outside automatic recovery may resolve the state.
18. Enter `SPAWN_IN_FLIGHT` only with `spawned_guard=null`. Inject a valid
    returned guard and require an atomic identity commit directly to `IDLE`;
    inject ambiguous output and require `AMBIGUOUS_LAUNCH`. Reload must never
    accept a standalone valid-guard `SPAWN_IN_FLIGHT`.
19. Cross manual and automatic origin with strict child-death evidence,
    physical wrapper absence `CONFIRMED`, and debt `NONE`: both must select
    no-kill `RELAUNCH_ONLY`. With outstanding debt, both must hold or select
    exact `DEBT_COMPLETION`; with a present childless wrapper, both must require
    the named `INITIAL` authority before any kill.
20. Construct each of the seven typed payload objects through its typed
    constructor using the scalar fixture values shown in the displayed JSON.
    Parsing may extract those scalar values, but the expected typed object and
    its field set must come from the independent typed constructor, not from
    the parsed JSON object.
    Encode each object through the implementation's production
    `CanonicalJsonV1`; require exact displayed byte and byte-count equality,
    then compute `SHA-256(domain || NUL || produced_bytes)` and require the
    displayed digest. Each downstream object embeds the upstream digest just
    recomputed by the implementation, never the displayed upstream digest.
    As a secondary change-detection control, change one upstream byte at each
    stage and prove every dependent identifier changes; that mutation control
    is not independent codec or field-set correctness evidence.
21. Race two reload reconcilers reserving nonordinary capture ordinals.
    Require distinct CAS-allocated values greater than zero at the current
    ordinary sequence, exact attempt binding, and no reuse/wrap. Pair an
    ordinal-zero ordinary residual with a nonzero reload post-action capture and
    prove each is accepted only in its stated context. For matching `RELEASED`
    after retained `STOP_TREE/CALL_RETURNED`, require no capture allocation or
    external capture call: finalize `EFFECT_UNPROVEN` and leave residual
    discovery to the next ordinary poll.
22. Inspect the 87-A adapter and closure successor's implementation, package,
    state, and activated-platform diffs. Any new daemon/service, persistence
    plane, durable helper, durable file/database/registry/journal, durable named
    OS object, nonempty runtime dependency, or mechanism-specific exception
    introduced by those mechanisms is nonconforming and makes the capability
    unavailable. Existing request-bound attended archive/store surfaces may
    remain for their shipped human workflows, but those surfaces may not carry
    closure, authority, debt, or cap state or supply closure proof. Prove that an
    unprovable closure returns pre-reservation `CAPABILITY_UNAVAILABLE`, makes
    zero external calls, performs no named teardown, remains `POLICY_HELD`
    pending a human, and creates a task rather than an implementation-detail
    mechanism.
23. Adapt merged task #120 at `587e7c1` byte-for-byte. Accept only
    `owned_process_tree_v2`, schema 2, status `complete|absent|truncated|invalid`,
    exact limit 64, consistent counts, generation/nonce, and the stated field
    projection. Freshly validate every live PID/start/filetime; for every
    complete/absent Windows ISO-start row, require a positive decimal exact
    FILETIME and use it—not the rounded ISO value—as the destructive guard.
    Require `observer_version=win-tree/v2`,
    `ownership_rule_version=owned-tree/v2`, and
    `pid_start_guard_schema=2`; the superseded v1 values are incompatible.
    Feed a valid non-Windows `linux:<boot_id>:<start_ticks>` token through the
    accepted #120 input path and require pre-reservation
    `CAPABILITY_UNAVAILABLE(EXACT_TARGET_EXECUTOR_UNAVAILABLE)`: construct no
    owner/target tuple or authority, consume no attempt, create no debt, and
    make no `Stop-Tree` call. Prove the implementation neither falls back to
    the legacy rounded-start executor nor weakens the Windows FILETIME guard.
    Admit a
    non-live ancestry bridge only from an exact prior complete
    generation/nonce/parent chain, exclude it from target tuples/digests and
    `Stop-Tree`, and normalize a live child behind it to the stated orphan
    form. Validate but exclude role/discovery metadata from authority. Prove an
    openable exact-matching planned Windows target reaches #120's unchanged
    native semantics only through one sealed dispatcher variant, is verified
    and terminated through one native handle, and every successful termination receives a wait
    attempt within the remaining shared tree-wide budget. Check-to-kill PID
    reuse must be impossible. Inject open failure, exact-identity mismatch,
    termination failure, wait timeout, and depleted budget; each must defer
    completion to the fresh snapshot/barrier, and a still-present target must
    remain residual/HOLD. Race planning against a recorded
    parent that creates a late descendant and exits: the unplanned descendant
    must miss the kill set, survive that `Stop-Tree`, be found only by #120's
    fresh barrier, block launch, map the post-action result to
    `EFFECT_UNPROVEN`, and never become a kill target, prove `COMPLETE_GONE`, or
    clear debt. Recycle the parent PID and prove an exact equal/newer child is
    excluded from the retired-parent ownership edge, while a pre-recycle or
    incomparable child stays conservative survivor evidence. Independently
    classify the replacement-side child as this agent's wrapper/wait and prove
    that ordinary barrier evidence still blocks. A clear barrier without the
    typed closure/absence proof must also refuse debt clear. Attended reset/archive
    evidence must never substitute for automatic closure or completion.
24. Prove the construction seal, not a state-entry inventory. Through every
    public decoder and reducer, deserialize a valid current childless envelope;
    it may yield inert evidence only. Feed an unknown schema-valid extension
    through the compatibility boundary and require either closed rejection or
    inert opaque evidence, never an action object. Attempt to pass raw targets,
    IDs, bindings, persisted state,
    forged provider results, and copied/stale permits to the checked owner and
    every external adapter; each must be rejected before a state mutation or
    call. Inspect the supervisor owned-tree surface and require exactly one
    private native body, zero public raw-array termination entry, zero direct
    `Stop-Tree $p.kill_targets` call, and zero route from planner targets to the
    body except one `SupervisorOwnedTreeNativeInvocationV1` produced by the
    winning admission, plan-ownership, and native-entry transitions over a
    private submission/owner pair. Reject raw
    arrays, caller-settable variant tags, wrappers around `kill_targets`, and
    field-equivalent fake variants. Exercise the configured-agent and ephemeral
    variants through their independent private constructors and prove parity for
    their existing authorization, persistence/barrier ordering, exact target
    behavior, and dry-run semantics. For the ephemeral variant, prove its narrow
    final action gate separately: after constructing a legitimate call, first
    change/disable the action-latch epoch and then, in an independent case,
    activate the kill switch before dispatch. Each case must permit exactly one
    winning admission, then consume the call as `REJECTED_NO_EFFECT`, preserve
    persisted `next_entry`, and construct no native plan, lexical raw array, or
    effect; replay and any second admission stay rejected.
    With both gates unchanged, dispatch retains the latch read guard through
    issuance and the private native body repeats the kill-switch check. Add a
    third race: pass the outer checks, pause after native-plan construction but
    before that inner check, and activate the kill switch. Require the exact
    invocation-bound `ACTIONS_DISABLED_NO_EFFECT` result, one transition to
    `REJECTED_NO_EFFECT(FINAL_ACTION_GATE_CHANGED)`, retained `next_entry`, no
    lexical raw-array materialization or native effect, and zero-effect replay.
    Exercise
    the childless variant only
    from the matching permit-bound typed call. Scope this static direction scan
    to the supervisor owned-tree executor and explicitly exclude the separate
    turn-watchdog facility. Prove the private native plan retains an immutable
    tuple and that its final lexical raw-array materialization has no caller or
    post-return alias.

    Prove witness construction is acyclic and private: a conforming Windows
    installation's unexported dispatcher-capability factory may mint a witness
    before any permit or call exists; no public caller, raw native-body entry,
    planner, persisted value, or non-childless variant may invoke that factory.
    A childless call made from its witness-derived permit is accepted only by
    the same live dispatcher instance. Reject a copied witness, a witness from a
    different dispatcher instance, and any attempt to mint a witness by first
    constructing or submitting a call.

    Exercise exact preflight reason direction. In a staged Windows path with
    valid exact-FILETIME semantics but no #146 dispatcher seal, require
    `CurrentExactTargetExecutorWitnessConstructionV1` and fresh
    `ClosureCapabilityV1` to return the exact singleton
    `DISPATCHER_SEAL_UNDELIVERED` before permit/provider evaluation. Repeat with
    a retained Windows envelope: require the same exact internal preflight
    reason, generic `CAPABILITY_UNAVAILABLE` attention, exact envelope retention
    except the separately allowed #115 observation projection, and no permit,
    mutation, call, or effect. With executor and seal both absent on Linux or
    macOS, require `EXACT_TARGET_EXECUTOR_UNAVAILABLE` to take precedence. With
    a staged conforming Windows seal present but the closure successor absent,
    require witness `AVAILABLE` followed by the exact singleton
    `SUCCESSOR_MISSING`, never `DISPATCHER_SEAL_UNDELIVERED`. These are proposed
    conformance results, not behaviors present at merged `587e7c1`.

    For every `ExactTargetExecutorOperationV1` and permit use, prove the
    private permit constructor requires a fresh witness, exact binding, current
    revision, and the operation's closed live scope. Normally it also requires
    the recomputed authorized tuple/residual subset; the closed targetless old-
    side rebind, retired-cleanup, and owner-transition scopes instead require
    the exact tombstone/envelope binding plus the complete fresh prospective
    proof or typed subject/checkpoint specified above.
    Independently mismatch each applicable operand, reuse the one-shot permit, and
    change the revision before consuming the permit. No case may yield an
    executable target, permit-bound mutation, or typed call. Separately invoke
    a valid typed call, commit an observation-only revision while it runs, and
    require a fresh receipt permit to accept the result only when envelope and
    continuation remain exact; any effect-state change rejects it. For every call-
    bearing operation, require three distinct permits at the arm revision,
    post-CAS call revision, and receipt-commit revision. Prove a consumed arm
    permit cannot construct a call and a call permit cannot apply a receipt;
    enforce the closed receipt-predecessor table.

    From one successful guard acquisition, race two permit issuers and also
    attempt sequential B1/P1 then B2/P2 before P1 resolves. Exactly one atomic
    `AVAILABLE -> OUTSTANDING` move may succeed; P2 must reject with zero
    mutation/call. After P1 returns custody exactly once, one successor issuance
    may succeed and P1 replay must fail. Assert the same lineage_id and
    issuance_id through permit, call, dispatch admission, plan ownership, native
    invocation, receipt, and receipt permit; an unrelated
    token is rejected.

    For each of CHILDLESS, CONFIGURED_AGENT_RELAUNCH, and EPHEMERAL_TERMINAL,
    retain two references to the same legitimate submission and race dispatch.
    Exactly one atomic `CALL -> DISPATCHING` compare-and-swap may win and yield
    one admission; the loser produces zero effect. Retain two references to that
    same admission and race `DISPATCHING -> PLAN_OWNED`: exactly one may construct
    one immutable plan and one invocation handle. Retain two references to that
    same invocation and race private native entry: exactly one
    `PLAN_OWNED -> INVOKING` winner may reach the final check, lexical raw-array
    materialization, or effect. Pause each winner after its transition and prove
    a concurrent alias still loses. Sequentially replay the submission,
    admission, and invocation after normal receipt, positive pre-effect
    rejection, and uncertain exception; every replay must construct no new
    capability or plan and produce zero effect. For CHILDLESS, returning
    successor custody must never revive the consumed call issuance. For either
    non-childless variant, no terminal owner state may transition back to
    `CALL`, `DISPATCHING`, `PLAN_OWNED`, or `INVOKING`.

    Retain two references to each legitimate
    `SupervisorOwnedTreeDispatchReceiptCustodyV1` and race consumption. Prove
    the sealed receipt alone cannot locate or mutate an owner. For
    CHILDLESS, exactly one `RECEIPT -> PERMIT` receipt-mutation admission may
    win. For CONFIGURED_AGENT_RELAUNCH and EPHEMERAL_TERMINAL, exactly one
    `RECEIPT -> CONSUMING_RECEIPT` compare-and-swap may win before any existing
    planner behavior; success closes once and uncertain planner commit poisons
    once. Inject a synchronous failure after the receipt-consumption CAS but
    before mutation/planner behavior: CHILDLESS must poison with
    `CUSTODY_PROTOCOL_BROKEN`, either non-childless variant with
    `DISPATCH_PROTOCOL_BROKEN`; neither may return to `RECEIPT`. Inject a
    possibly-started childless commit and require `OWNER_COMMIT_UNCERTAIN`, and
    a possibly-started non-childless planner behavior/commit and require
    `PLANNER_COMMIT_UNCERTAIN`. Every concurrent loser and sequential custody-handle replay must produce zero
    mutation, launch, or effect.

    Inject result-handoff failure after the atomic admission, plan/invocation-
    handle construction or handoff failure after `PLAN_OWNED`, and synchronous
    exceptions during validation, native entry, invocation, after native return
    before receipt construction, during receipt handoff, receipt consumption,
    and owner/planner commit. Each path must produce exactly one
    terminal custody disposition: return only with positive proof that no effect
    began, otherwise `POISONED`; never both and never neither. Repeat cleanup and
    require idempotent rejection. A poisoned lineage cannot issue again until
    guard release/reacquisition and cannot reissue an uncertain external effect.

    Reproduce the reviewer's mutation probe: construct a legitimate sealed call
    whose nested target PID is 101, use the controlled tamper hook to rewrite it
    to 202, and require consumer rejection before native effect. Ordinary public
    mutation must fail; mutating the caller's original source alias after
    construction must leave the call unchanged. Repeat the transitive tamper
    control for the target set, permit proof/residual, next envelope/outer delta,
    call arguments, receipt result, configured-agent provenance, and ephemeral
    provenance. A frozen outer record, digest-only check, or public reseal API is
    insufficient. Inspect each sealed graph and prove neither the atomic lineage
    owner nor a non-childless dispatch-use owner cell is reachable; only the
    immutable custody/use proof is present. Prove the opaque submission,
    admission, invocation, and receipt-custody handles are the only private
    associations between their exact immutable values and that owner, and that each holder transition
    changes the separate cell exactly once. A caller
    mutex, external call-ID registry, or unstated dispatcher lock must fail the
    construction-direction control.

    Round-trip every serializable state shape and prove witness, permit,
    executable target, mutation, call, receipt, and live effect guard are absent
    after decoding. Prove only the actual adapter can return a receipt and only
    for its matching typed call. On Linux and macOS, exercise at least a fresh
    proof, a deserialized closure/debt envelope, childless `SPAWN_IN_FLIGHT`,
    `AMBIGUOUS_LAUNCH`, and a retired-attempt tombstone as direction controls;
    the effect envelope must remain exact and no adapter may be called, while
    two adjacent polls may advance only the owner-private ordinary-observation
    projection derived from distinct sealed receipts.
    Apply a private non-childless delta to each current childless envelope and
    reject it before field application. Reject composite mutation tags. Submit
    the former public observation-record shape with forged
    `child_dead_confirmation.count=2`,
    `owned_childless_confirmation.count=2`, and
    `absence_confirmation=CONFIRMED`; #115 must reject it before any write.
    Reject public/direct/fake receipt-factory access, an attempt to exchange raw
    evidence for a receipt outside the installed observer adapter, and a receipt
    minted by a substituted adapter. Prove only the #115-created lineage passed
    to the installed adapter can mint one sealed receipt from one completed
    acquisition. Retain two references to that same private acquisition handle
    and race the installed adapter: exactly one `UNUSED -> ACQUIRING` transition
    may win, exactly one real observation and receipt may result, and the loser
    plus sequential replay perform zero acquisition. Inject receipt/custody
    construction and atomic-yield handoff failure and require one `POISONED`
    owner with no returned custody handle. Retain two references to the
    resulting `OrdinaryObservationCommitCustodyV1` and race #115 commit; prove
    the sealed receipt alone cannot locate its owner. Exactly one
    `RECEIPT -> COMMITTING` transition may win before reducer derivation or field
    application; the loser and sequential replay perform zero mutation. One
    legitimate sealed ordinary receipt from empty/count-zero state may advance
    at most to count one/`OBSERVED_ONCE`; confirmation requires a second distinct
    acquisition and receipt committed against the successor revision. Reject
    copied/replayed/serialized receipts, a second receipt from one lineage,
    wrong agent/epoch/revision, forged sequence/ordinal, nonordinary receipts,
    derived successor fields, and mutated nested evidence. Race two valid
    same-revision receipt lineages: exactly one CAS commits and the loser is
    poisoned and cannot be reused. Assert that each #115 begin lineage contains
    the exact prospective ordinal-zero `CaptureIdV1` before the observer's first
    acquisition step. Prove `ProcessObservationV1.capture_id`,
    `receipt.prospective_capture_id`,
    `PrivateClassifierObservationMutationV1.capture_id`, and every non-null
    successor capture-ID field that records the current sample equal that
    begin-bound ID byte-for-byte. Empty/reset successors retain null IDs; no
    successor may synthesize a different ID. Starting from predecessor sequence
    `n`, require the receipt, raw/tree displayed sequence, and candidate mutation
    to use checked `n + 1`; after the winning commit require current sequence
    `n + 1`, never `n` during candidate validation. At maximum `uint64`, require
    exactly `CAPTURE_SEQUENCE_EXHAUSTED`, no acquisition-handle construction,
    observation, receipt, mutation, or 87-A action, and no wrap to zero. After the race,
    attempt to pass the stale loser's completed acquisition to a successor
    lineage, reseal it, or restamp it with the successor capture ID; every case
    must reject with zero state change and zero confirmation advance. Only a new
    begin plus a distinct real acquisition after successor reload may mint the
    next receipt. Commit an ordinary observation first,
    reload, and mint any later permit only at the successor revision. Prove
    initial non-dry-run state-loss quarantine
    creation is the exclusive fail-closed permit-free mutation, dry-run loss
    persists nothing, and V1 has no automatic retirement constructor.
    Exercise every declared state-file/workspace transfer, restore, rollback,
    and migration activation entry, including same-platform inputs. Each must
    refuse before imported bytes are admitted as the active checked store and
    produce no witness, permit, authority/effect mutation, external call, or
    launch. Prove the refusal identifies the rejected operation/store and
    directs attended handling without fabricating an active-agent
    `POLICY_HELD`. Separately document and test as a negative boundary that a
    structurally valid out-of-band replacement presented as an in-place restart
    need not be distinguishable from local checked state: it is nonconforming,
    may proceed outside 87-A's guarantees, and must not be advertised as a
    fail-closed V1 migration path. When existing outer-state checks do detect
    rollback-unproven state, require the sole admitted non-dry-run mutation to
    be `StateLossQuarantineCreationDeltaV1`.
    A future POSIX executor contract must not make a persisted Windows FILETIME
    binding compatible without a reviewed version/migration rule.

## Dependencies and release order

| Property | Classification | Owner |
| --- | --- | --- |
| Two same-owner post-establishment complete child absences | #120 INPUT DELIVERED; NORMATIVE CONTRACT SPECIFIED; implementation requires #115 and the 87-A adapter | #115 owner-private reducer over sealed receipts plus merged #120 snapshot |
| PID/start/nonce ownership; never pattern ownership | #120 INPUT DELIVERED; NORMATIVE CONTRACT SPECIFIED; adapter not implemented | Adapter over merged `owned_process_tree_v2`, including exact Windows FILETIME |
| Complete 64-entry tree observation | DELIVERED by merged #120; 87-A adapter validation remains required | #120 snapshot plus 87-A adapter |
| POSIX exact-target execution for named teardown | CURRENTLY UNAVAILABLE | #120 accepts Linux exact observation tokens but declares no macOS token and has no non-Windows executor branch. Fresh proofs cannot construct a reservation permit; deserialized reservations, phases, debt, spawn ambiguity, and retired tombstones are inert because they cannot construct a fresh matching permit, typed call, or effect-owned mutation. The effect envelope remains exact while ordinary observation may advance its separate projection. Every unresolved named case remains `POLICY_HELD` pending a human. |
| Action-scoped creation closure | CURRENTLY UNAVAILABLE; NORMATIVE CONTRACT SPECIFIED pending a conforming closure successor | Merged #120 does not freeze creation or expose attempt-keyed acquire/reconcile/release; otherwise `CAPABILITY_UNAVAILABLE` and `POLICY_HELD` pending a human |
| Atomic reservation/debt/cycle/terminal state | NORMATIVE CONTRACT SPECIFIED; implementation blocked on #115 | Task #115 checked state owner |
| External-call continuation/effect linearization | PARTIAL Windows target-local primitive DELIVERED by #120; full normative contract specified; implementation blocked on #115, #146, and the closure successor | #120 same-handle exact FILETIME check/terminate plus conditional bounded wait attempt, #146 closed dispatch, checked continuation state, unique guard lineage, and successor-owned attempt-bound synchronous adapters |
| Fail-closed state-loss quarantine | NORMATIVE CONTRACT SPECIFIED; implementation blocked on #115 | Task #115 checked state owner |
| Automatic state-loss-quarantine retirement | UNAVAILABLE IN V1 ON EVERY PLATFORM | No trustworthy host/process-universe token exists in merged #120; quarantine stays `STATE_PROVENANCE_LOST`/`POLICY_HELD` pending attended handling. A future read-only existing-OS-token successor must satisfy M5 Option A. |
| Same-platform state-file/workspace transfer, restore, rollback, and migration activation | UNAVAILABLE IN V1; DECLARED ACTIVATION REFUSES | A conforming activation path refuses before active-store admission with zero 87-A witness, mutation, effect, or launch and directs attended handling. An out-of-band replacement may be undetectable, is nonconforming, and has no 87-A guarantee. Future 87-C must bind the source universe within M5 Option A or keep imported state inert. |
| No daemon, persistence plane, durable helper or OS object, or runtime dependency | DECIDED ABSOLUTE by operator on 2026-07-31 (M5 Option A) | Project/package boundary; no mechanism-specific exception |
| Supervisor owned-tree termination dispatch | NORMATIVE CONTRACT SPECIFIED; IMPLEMENTATION BLOCKED ON #146 | Merged #120 supplies the exact FILETIME native-body semantics, but merged code still exposes a raw-array entry. #146 installs the closed opaque-sum dispatcher, migrates both raw callers, and proves no direct target call survives. This is scoped to the supervisor owned-tree executor; POSIX remains unavailable. |
| Three-attempt automatic cap and continuous typed attention | NORMATIVE CONTRACT SPECIFIED; implementation blocked on #115 | 87-A state/output |
| Durable human delivery and receipt | STATED out of scope | Future 87-B; every `CAPABILITY_UNAVAILABLE` rendering names the held agent and required operator action |
| Operator explanation of permanent capability limitations | REQUIRED before 87-A implementation close and activation | 87-B/follow-up operator manual and tutorial evidence must state all three limitations together: both indefinite `POLICY_HELD` paths and the declared transfer/restore/rollback/migration activation refusal, including the out-of-band-copy residual and attended action. |

Task #78 consumes the named authority only after #115, #146, the adapter over
merged #120, and the closure successor. Task #116 remains blocked only on #115 and
independently stageable: an already-absent wrapper needs neither a target tree
nor the closure successor. This preserves the
task #94 ordering and #107's single contained supervisor owned-tree kill site
after #146.
