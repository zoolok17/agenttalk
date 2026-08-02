# Design 87-A module: owned-childless wrapper authority

**Design status:** Proposed, Revision 16; candidate for
**NORMATIVE-SPECIFICATION COMPLETE**. This file and
[`DESIGN-87A-supervisor-classifier-authority.md`](DESIGN-87A-supervisor-classifier-authority.md)
at the same commit form one specification. Neither is conforming alone.

**Delivery status:** **IMPLEMENTATION BLOCKED.** Task #146 must replace the
merged raw supervisor owned-tree kill entry with the closed dispatcher specified
below and migrate every current caller. Task #115 must supply the checked owner,
action-scoped custody mint, and the narrow Q4 post-commit fence witness/barrier
reducer over sealed merged-#120 observation operands. Task #57 must supply the durable per-wrapped-agent
launch singleton required by configured relaunch. A separately reviewed
`ExactIssuerIdentityAdapterV1` must supply any present-PID/reuse identity
decision; this revision admits attended disposal only after a definitive
PID-absent `GONE` result. `ConfiguredPreBarrierRetrySuccessorV1` remains an
optional undelivered seam for future automatic retry. The closure successor
also remains an open owned-childless dependency. Revision 15 specifies those
boundaries and the attended escape from the resulting hold; it does not deliver
them.

**Conformance status:** **UNAVAILABLE.** Neither Q4 nor 87-A is complete,
conforming, sealed, or enforced in merged code. If this revision passes design
review, only the normative specification is complete. Runtime conformance still
requires the named implementations and their executed controls.

**Activation status:** **PROHIBITED** until delivery and conformance close.

**Mode:** Reference.

**Audience:** Contributors implementing tasks #57, #78, #115, #120, and #146,
the `ExactIssuerIdentityAdapterV1`, optional
`ConfiguredPreBarrierRetrySuccessorV1`, the closure successor defined below,
operators resolving held configured actions, and reviewers checking teardown
authority.

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
defines the closed typed contracts and adapter joins both dependencies must
satisfy.

This is a normative seam, not hand-waving:

```text
87-A observes and classifies
  -> #120 publishes a bounded owned-tree snapshot or a closed refusal
  -> that snapshot, every parsed target, and every reloaded state remain inert evidence
  -> #146 places the supervisor owned-tree native kill body behind one closed dispatcher
  -> only that dispatcher's private capability factory may mint `CurrentExactTargetExecutorWitnessV1`
  -> only a source-bound witness-plus-binding match may construct an operation-scoped permit
  -> only a permit may construct a reservation, effect-owned mutation, or typed external call
  -> adapters accept typed calls and return matching typed receipts, never raw IDs or state
  -> the dispatcher alone projects a private immutable native plan into the sole native body
  -> #115 persists an inert configured issuer checkpoint beside each kill-bearing PRE_BARRIER reservation
  -> loss of that checkpoint's transient custody holds; persisted provenance cannot remint it
  -> only definitive independent PID absence admits checked attended disposition
  -> a present PID remains held until ExactIssuerIdentityAdapterV1 can decide reuse
  -> ConfiguredPreBarrierRetrySuccessorV1 remains an optional future automatic exit
  -> attended disposition installs a global fence; only #115's winning committed ordinary capture may feed its no-effect clearance
  -> #57 prevents a crash-replayed configured relaunch from starting a second wrapper
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
and condition fingerprint codec/vectors are imported without semantic drift. This
module does not alter their inputs, equations, tables, serialization, or
counts.

The
[`DESIGN-87A-delta-panel-disposition.md`](DESIGN-87A-delta-panel-disposition.md)
register maps every finding from the panel over `f42570d..44b3787` to its
normative location. It is audit evidence, not a third 87-A specification.

### Identity and equality vocabulary

This module imports the core's vocabulary unchanged. The **exact** stem appears
only in a declared identifier or a direct reference to a named identity contract,
type, or adapter with its comparator, such as **exact-FILETIME target**.
**Source-equal** means the same typed field value as the named source and grants
no identity authority. **Matching** applies a named predicate or relates a typed
result, receipt, or transition to its required operands. **Byte-identical**
applies to canonical serialized bytes and byte-for-byte preservation; **closed**
applies to a complete displayed algebra. “Exactly one” and “exactly once” remain
ordinary cardinality or multiplicity phrases. In particular,
`ExecutionGateCaptureV1.guarded_start` is generic `ProcStartGuardV1`-shaped
audit evidence, not `OwnedExactStartGuardV1` and not a PID-reuse comparator. A
recomputed, derived, calculated, or execution-produced value is never
source-equal to the operand or expected result; it must match under the
displayed predicate. A later field may be source-equal only when it carries
that already-produced value from a named source.

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
must match the persisted PID, `OwnedExactStartGuardV1`, and launch nonce. Every tree
target carries an owner nonce source-equal to that persisted launch nonce through
a complete #120 ownership proof.
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
| Live Windows `entries[].pid/start/start_filetime` | `OwnedTreeTargetV1.pid/start_guard` after live `OwnedExactStartGuardV1` validation. The destructive guard is the positive decimal `start_filetime`; the rounded ISO `start` is capture/ordering corroboration and never substitutes for a missing FILETIME. The adapter derives and validates `parent_start_guard` and `depth` from the accepted live parent chain and projects the validated top-level owner nonce onto each target; it may not privately default any missing fact. |
| Non-Windows platform, including a live Linux `entries[].start` token | #120 recognizes Linux `linux:<boot_id>:<start_ticks>` as observation/barrier input; it declares no macOS process-start identity mapping. The merged supervisor owned-tree native body has no non-Windows target-identity execution branch, so fresh authority construction returns pre-reservation `CAPABILITY_UNAVAILABLE(EXACT_TARGET_EXECUTOR_UNAVAILABLE)`, short-circuits before the effect envelope or reservation exists, and constructs no executable target, permit, attempt, debt, or external call. Reloaded evidence may be parsed and ordinary observation bookkeeping may advance, but no matching permit or effect-owned mutation is constructible. Input acceptance is not effect capability. |
| Non-live virtual ancestry bridge | Admissible only as a source-equal copy of one prior complete record; its wrapper generation, launch nonce, and parent chain are therefore source-equal to that record. It is validation provenance only: exclude it from `root_first_targets`, every target digest, and `Stop-Tree`. A live child whose immediate owned parent is such a bridge uses the module's existing positively proven orphan form: null owned-parent fields and depth one. |

`role` and `discovered_at` are validated bounded metadata but are excluded from
owner identity, target tuples, and target digests. The unique live wrapper
entry becomes depth zero with null internal parent fields; its external
supervisor/console parent is never an owned edge. The reserved
`detached_gate_runner` role never grants ownership by itself.
The owner's `wrapper_start_token` is source-equal to the accepted wrapper row's
reported `start`, and its `wrapper_start_guard` is source-equal to that row's
validated destructive `OwnedExactStartGuardV1`; neither field may substitute for the other.

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

### Abrupt-death replay split for the two non-childless variants

The non-childless custody owner is intentionally transient. A process death can
destroy it after the persisted planner checkpoint and before receipt/poison
publication. Revision 15 does not pretend that an in-process owner survives
that boundary. Instead it separates the two variants by their actual durable
effect.

For the kill-only `EPHEMERAL_TERMINAL` subphase, restart retry is intentionally
permitted only after a new #115 checked action transition mints new custody.
The safety argument is the merged #120 exact-FILETIME identity path at `587e7c1`, traced
from target construction to effect:

1. A complete Windows owned-tree entry must carry a positive decimal
   `start_filetime` (`src/agenttalk/supervisor.py:2444-2470`), and the planner
   emits that source-equal field on live complete-tree kill targets (`3285-3300`). A
   malformed owned-tree target without it is skipped (`8930-8932`); rounded
   start is not a fallback.
2. If the PID is gone, `Open-AgenttalkProcessHandle` returns null and
   `Stop-Tree` continues without effect (`8908-8911`).
3. If the PID was recycled, creation FILETIME read from the opened handle differs
   from the planned source-equal value and the body continues without termination
   (`8912-8913`).
4. If the PID and creation FILETIME still match, termination is attempted through
   that same already-validated handle (`8912-8921`; helper definitions
   `8780-8804`). This is the same process instance the original call targeted.
   A repeated termination is the intended exact-FILETIME target retry; after a prior
   successful exit the next attempt becomes the gone-PID case.

Thus all three restart cases are target-local identity-safe: gone is a no-op,
recycled is refused, and the same live process remains the intended target. This
does not prove arbitrary prior native work is gone or make every `Stop-Tree`
implementation globally idempotent. The persisted ephemeral `next_entry` at
`9579-9591` is followed by the fresh teardown barrier and retained-active hold at
`9592-9631`; only that fresh result permits archive.

`CONFIGURED_AGENT_RELAUNCH` keeps the same exact-FILETIME target-identity safety for its kill
subphase, whose `barrier_state` is persisted before `Stop-Tree` at `9377-9392`
and whose fresh survivor barrier runs at `9393-9419`. That proves the target
semantics of an independently authorized repeat; it does not authorize a repeat
from the persisted checkpoint. A crash can leave
`NON_CHILDLESS/RESERVED/PRE_BARRIER` while destroying the only transient action
custody. Revision 15 therefore requires
`ConfiguredPreBarrierOwnerLossHoldV1` with
`POLICY_HELD(CONFIGURED_PRE_BARRIER_OWNER_LOST)`: no reload path may remint from
`barrier_state`, silently apply generic `PRE_BARRIER_RELEASE`, or release and
reserve again. The only V1 exit is the checked attended disposition below.
Automatic retry is unavailable; `ConfiguredPreBarrierRetrySuccessorV1` names
an optional future mechanism rather than a Q4 blocker.

The later launch is different again: merged code persists `next_state`, calls
`Launch`, and only then records the returned PID (`9445-9477`). `Start-Process`
is not idempotent, and a crash or abandoned worker can destroy transient custody
while a prior wrapper is already live. #120 proves no launch singleton. Task
#57's open scope is the durable project-level singleton per wrapped agent
(launch lock); automatic configured relaunch remains implementation- and
activation-blocked until #57 lands and is reviewed. 87-A neither respecifies
that lock nor converts replayable planner state into launch authority.

The barrier rechecks recorded `OwnedExactStartGuardV1` identities and fresh descendant edges.
For an independently authorized Windows attempt, planning and `Stop-Tree`
remain separated by process scheduling, so a recorded parent may create a
descendant after the plan; that unplanned descendant may
survive because it was never a kill target, and the barrier catches it only to
block launch. When a recorded PID has been recycled, a child's exact-FILETIME start equal
to or newer than the replacement is excluded from the retired-parent ownership
edge; a child provably older than the replacement, or exact-FILETIME evidence that is
missing/incomparable, remains conservative old-side survivor evidence. This
split does not suppress independent barrier evidence: the replacement-side
process still blocks if, for example, its command line parses as this agent's
wrapper or wait process. The barrier never adds a kill target, proves teardown
effect, or supplies closure evidence. The attended reset supplies an
identity-bound human escape, and the request-bound attended archive
supplies retention; neither grants automatic teardown authority, closes child
creation, or proves an automatic effect complete.

**STATED merged behavioral delta:** Once #115, #146, and a future conforming
closure provider have independently authorized teardown, a planned target whose
termination succeeds and which signals within #120's remaining wait budget no
longer appears in the immediate post-kill snapshot, and a replacement-side
child classified by the exact-FILETIME comparator for a recycled PID no longer creates a sticky barrier
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
snapshot, exact-target identity behavior, barrier, attended reset, and attended archive
for independently authorized non-87-A callers; it does not preserve the raw
call signature or treat a planner array as authority.

Capability claims are proved at the line where the effect happens, not where an
input is accepted. Merged #120 accepts Linux `linux:<boot_id>:<start_ticks>` as
a Linux process-start identity token (grammar at
`src/agenttalk/supervisor.py:2187-2204`; token/record validation at
`2087-2115`, `2438-2470` in `587e7c1`), but its current raw executor enters the destructive branch only when
`start_filetime` is present (`8900-8928`) and explicitly skips an
`owned_process_tree` target without it (`8930-8932`). Therefore that accepted
token is not a POSIX kill adapter. Linux and macOS are structurally unavailable
for this named teardown until a separately reviewed process-start-identity target executor is
delivered; Revision 15 does not dependency-track or assume one. Windows is also
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
  projection whose included fields are source-equal to the corresponding
  OwnedWrapperIdentityV1 source fields, excluding state_epoch

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
  once for one successful acquisition of that same per-agent effect-guard
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
    + one module-typed ExactTargetExecutorBindingV1
    + the operation's source-bound binding proof: normally the recomputed
      owner/authorized-target tuple and, when applicable, an authorized residual
      subset whose members are source-equal to the corresponding tuple members;
      only the closed targetless old-side rebind and cleanup scopes below may use
      a binding source-equal to the selected tombstone/envelope binding plus their typed
      subject and prospective proof
    + current checked-state revision
    + one ExactTargetExecutorOperationV1
    + one ExactTargetExecutorPermitUseV1
    + that operation's specified live scope defined below
    + for a guard-required STATE_MUTATION or EXTERNAL_CALL, the one
      AVAILABLE LiveEffectGuardCustodyV1 moved atomically into this permit's
      fresh issuance_id; no second issuance from the acquisition is possible
      while its lineage is OUTSTANDING, POISONED, or CLOSED
    + for RECEIPT_MUTATION, the matching allowed predecessor receipt, consumed as
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
  private, nonserializable, noncopyable, deeply immutable, alias-free
  root-first target tuple source-equal to the permit's authorized source tuple,
  with a recomputed OwnedTargetDigestV1 over that tuple matching the permit's
  ExactTargetExecutorBindingV1.authorized_target_digest,
  bound to one ExactTargetExecutorPermitV1(
    operation = STOP_TREE, use = EXTERNAL_CALL)

PermitBoundChildlessMutationV1 =
  private, nonserializable, noncopyable, deeply immutable, alias-free
  checked-state delta containing the expected revision source-equal to the
  checked state,
  one current ExactTargetExecutorPermitV1 whose use is STATE_MUTATION or
  RECEIPT_MUTATION, an envelope source-equal to the checked current envelope or
  NO_CHILDLESS_ENVELOPE for an initial RESERVE, and the complete next
  ChildlessEffectEnvelopeV1 or terminal envelope removal allowed by that
  permit's operation, plus the closed core ChildlessOuterStateDeltaV1 whose
  operation is source-equal to the permit operation

ChildlessExternalEffectCallV1 =
  private, nonserializable, noncopyable, deeply immutable, alias-free,
  single-use typed call in
    CLOSURE_ACQUIRE | CLOSURE_RECONCILE | CLOSURE_RELEASE
    | RETIRED_ATTEMPT_RECONCILE | STOP_TREE | POST_ACTION_CAPTURE
    | SPAWN
  carrying one call_id, a matching permit whose use is EXTERNAL_CALL, binding and
  continuation fields source-equal to that permit's fields, only that operation's
  typed arguments, and ownership of that permit's immutable
  LiveEffectGuardCustodyV1 proof plus an issuance_id source-equal to that permit's
  issuance_id;
  its separate private lineage owner cell is atomically in holder CALL

ChildlessExternalEffectReceiptV1 =
  private, nonserializable, noncopyable, deeply immutable, alias-free,
  single-use typed result carrying call_id, operation, consumed call-permit
  identity/binding, and call-time checked revision, each source-equal to the
  corresponding field of the consumed call,
  operation-specific result, and
  the same still-live immutable custody proof moved by the synchronous adapter;
  its separate private lineage owner cell is atomically in holder RECEIPT

ConfiguredAgentOwnedTreeCallV1 =
  private, nonserializable, noncopyable, deeply immutable, alias-free,
  single-use call constructible only by atomically consuming one matching
  SupervisorOwnedTreeDispatchActionCustodyV1 whose binding was minted by #115's
  checked configured reservation/barrier transition, plus checked
  relaunch/stuck-recovery planner provenance matching that binding, persisted
  barrier_state, privately
  sealed target tuple, one fresh call_id, and one immutable
  SupervisorOwnedTreeDispatchUseProofV1

ConfiguredActionIssuerCheckpointV1 =
  core ConfiguredActionIssuerCheckpointV1 type alias imported without redefinition;
  the module adds, removes, renames, and defaults no field

ConfiguredPreBarrierOwnerLossHoldV1 =
  core ConfiguredPreBarrierOwnerLossHoldV1 type alias imported without redefinition;
  the module consumes its complete source hash and remedy projection

ConfiguredPreBarrierOwnerLossSummaryV1 =
  core ConfiguredPreBarrierOwnerLossSummaryV1 type alias imported without
  redefinition; it is the bounded redacted 87-B projection, not the hold

ConfiguredPriorEffectUnknownFenceV1 =
  core ConfiguredPriorEffectUnknownFenceV1 type alias imported without redefinition

ConfiguredPriorEffectUnknownFenceSummaryV1 =
  core ConfiguredPriorEffectUnknownFenceSummaryV1 type alias imported without
  redefinition; it is the bounded redacted 87-B projection, not the fence

CommittedOrdinaryFenceCaptureV1 =
  core CommittedOrdinaryFenceCaptureV1 type alias imported without redefinition;
  only the winning #115 ordinary-observation commit may publish it

ConfiguredPriorEffectFenceBarrierReceiptV1 =
  core ConfiguredPriorEffectFenceBarrierReceiptV1 type alias imported without
  redefinition

ConfiguredPriorEffectFenceBarrierReceiptCustodyV1 =
  core ConfiguredPriorEffectFenceBarrierReceiptCustodyV1 type alias imported without
  redefinition

AttendedConfiguredPreBarrierDispositionRequestV1 =
  core AttendedConfiguredPreBarrierDispositionRequestV1 type alias imported without
  redefinition

AttendedConfiguredPreBarrierDispositionDeltaV1 =
  core AttendedConfiguredPreBarrierDispositionDeltaV1 type alias imported without
  redefinition

AttendedConfiguredPreBarrierDispositionResultV1 =
  core AttendedConfiguredPreBarrierDispositionResultV1 type alias imported without
  redefinition

AttendedConfiguredPreBarrierDispositionRejectionV1 =
  core AttendedConfiguredPreBarrierDispositionRejectionV1 type alias imported
  without redefinition

PriorEffectFenceClearanceGateHoldV1 =
  core PriorEffectFenceClearanceGateHoldV1 type alias imported without redefinition

PriorEffectFenceBarrierHoldV1 =
  core PriorEffectFenceBarrierHoldV1 type alias imported without redefinition

PriorEffectFenceClearanceStateRejectionV1 =
  core PriorEffectFenceClearanceStateRejectionV1 type alias imported without
  redefinition

ConfiguredPriorEffectFenceClearanceReconciliationV1 =
  core ConfiguredPriorEffectFenceClearanceReconciliationV1 type alias imported
  without redefinition

ConfiguredPriorEffectFenceClearanceResultV1 =
  core ConfiguredPriorEffectFenceClearanceResultV1 type alias imported without
  redefinition

ExactIssuerIdentityAdapterV1 =
  core named undelivered dependency imported without redefinition; this module
  defines no process-identity schema, comparator, or RECYCLED constructor

ConfiguredPreBarrierRetrySuccessorV1 =
  named, separately reviewed optional future mechanism that alone may add an
  automatic checked retry transition from a matching owner-lost configured
  checkpoint. It must prove the persisted issuer dead independently, atomically consume
  that checkpoint into one fresh action-scoped custody, preserve
  PRIOR_EFFECT_UNKNOWN until a fresh #120 barrier, and expose no constructor
  from caller-supplied or merely deserialized provenance. It is not delivered
  or made constructible by Revision 15.

EphemeralTerminalFinalActionGateV1 =
  private, nonserializable, noncopyable, deeply immutable, alias-free snapshot {
    dry_run: false
    kill_switch: CLEAR
    action_latch_state: ENABLED
    action_latch_epoch: uint64
  }

EphemeralTerminalOwnedTreeCallV1 =
  private, nonserializable, noncopyable, deeply immutable, alias-free,
  single-use call constructible only by atomically consuming one matching
  SupervisorOwnedTreeDispatchActionCustodyV1 whose binding was minted by #115's
  matching checked COMPLETE | TIMEOUT | FAILED action transition, plus
  request/action provenance whose fields are source-equal to the corresponding
  fields of that transition, persisted next_entry, one bound
  EphemeralTerminalFinalActionGateV1, a privately sealed module-typed target tuple,
  one fresh call_id, and one immutable SupervisorOwnedTreeDispatchUseProofV1

SupervisorOwnedTreeDispatchActionBindingV1 =
  private, deeply immutable, alias-free closed sum:
    CONFIGURED_AGENT_RELAUNCH {
      agent_key, state_epoch, committed_revision,
      checked reservation/barrier transition identity,
      sealed barrier_state identity, and target digest, all source-equal to the
      corresponding fields of #115's checked configured transition
    }
    | EPHEMERAL_TERMINAL {
        request_id, agent_key, COMPLETE | TIMEOUT | FAILED action, state_epoch,
        committed_revision, checked action-transition identity, sealed next_entry
        identity, target digest, and action_latch_epoch, all source-equal to the
        corresponding fields of #115's checked terminal transition
      }

SupervisorOwnedTreeDispatchUseProofV1 =
  private, nonserializable, noncopyable, deeply immutable, alias-free proof
  carrying one fresh use_id, a call_id and dispatch variant source-equal to the
  corresponding fields of the bound call, and the
  unexported live dispatcher-instance identity; it grants no native-plan
  authority without the matching atomic owner in CALL

SupervisorOwnedTreeDispatchActionCustodyV1 =
  private, nonserializable, noncopyable opaque action-custody handle emitted as
  the linearization result of exactly one #115 checked transition. It pairs one
  immutable SupervisorOwnedTreeDispatchActionBindingV1 and use_id with an
  unexported reference to the matching atomic owner in READY. Aliases may exist,
  but every alias names that same owner. Persisted barrier_state, next_entry,
  planner output, IDs, digests, or a deserialized equivalent cannot construct,
  find, reset, or reseal this handle.

SupervisorOwnedTreeDispatchUseOwnerV1 =
  one private, nonserializable, noncopyable atomic owner cell minted exactly
  once by #115 as part of the checked transition that authorizes one configured
  or ephemeral logical action, never by a call constructor or from replayable
  provenance. It exposes no clone/reset/rearm API and stores
  SupervisorOwnedTreeDispatchUseStateV1 outside every sealed call graph.

SupervisorOwnedTreeDispatchPreEffectRejectionV1 =
  PRIVATE_SEAL_OR_OWNER_MISMATCH | CALL_ALREADY_DISPATCHING_OR_CONSUMED
  | VARIANT_PROVENANCE_STALE | TARGET_OR_BINDING_MISMATCH
  | FINAL_ACTION_GATE_CHANGED | DISPATCHER_INSTANCE_MISMATCH
  | ADMISSION_OR_PLAN_HANDOFF_FAILED_NO_EFFECT
  | NATIVE_ENTRY_FAILED_NO_EFFECT

SupervisorOwnedTreeDispatchPoisonCauseV1 =
  NATIVE_EFFECT_UNCERTAIN | RECEIPT_CONSTRUCTION_UNCERTAIN
  | RECEIPT_HANDOFF_UNCERTAIN | PLANNER_COMMIT_UNCERTAIN
  | DISPATCH_PROTOCOL_BROKEN

SupervisorOwnedTreeDispatchUseStateV1 =
  READY {
    use_id: lowercase hyphenated UUID
    variant: CONFIGURED_AGENT_RELAUNCH | EPHEMERAL_TERMINAL
    action_binding: SupervisorOwnedTreeDispatchActionBindingV1
  }
  | CALL {
    use_id: lowercase hyphenated UUID
    call_id: lowercase hyphenated UUID
    variant: CONFIGURED_AGENT_RELAUNCH | EPHEMERAL_TERMINAL
  }
  | DISPATCHING { use_id; call_id; variant; each source-equal to its corresponding CALL field }
  | PLAN_OWNED { use_id; call_id; variant; each source-equal to its corresponding CALL field }
  | INVOKING { use_id; call_id; variant; each source-equal to its corresponding CALL field }
  | RECEIPT { use_id; call_id; variant; each source-equal to its corresponding CALL field }
  | CONSUMING_RECEIPT { use_id; call_id; variant; each source-equal to its corresponding CALL field }
  | REJECTED_NO_EFFECT {
      use_id; call_id; variant; each source-equal to its corresponding CALL field
      reason: SupervisorOwnedTreeDispatchPreEffectRejectionV1
    }
  | POISONED {
      use_id; call_id; variant; each source-equal to its corresponding CALL field
      cause: SupervisorOwnedTreeDispatchPoisonCauseV1
    }
  | CLOSED

SupervisorOwnedTreeDispatchCallV1 =
  private, nonserializable, noncopyable, deeply immutable, alias-free,
  single-use closed sum with these three variants:
    CHILDLESS(
      ChildlessExternalEffectCallV1(operation = STOP_TREE))
    | CONFIGURED_AGENT_RELAUNCH(ConfiguredAgentOwnedTreeCallV1)
    | EPHEMERAL_TERMINAL(EphemeralTerminalOwnedTreeCallV1)

SupervisorOwnedTreeDispatchSubmissionV1 =
  private, nonserializable, noncopyable opaque submission handle emitted exactly
  once alongside one SupervisorOwnedTreeDispatchCallV1 by its private variant
  constructor; it carries that sealed call plus an unexported reference to the
  same atomic owner: the call's LiveEffectGuardLineageOwnerV1 for CHILDLESS,
  otherwise its SupervisorOwnedTreeDispatchUseOwnerV1. It is not a sealed value
  graph or an authorization record. Retaining or racing aliases of the same
  handle is explicitly permitted by the threat model and does not duplicate the
  owner state.

SupervisorOwnedTreeDispatchAdmissionV1 =
  private, nonserializable, noncopyable opaque admission handle emitted as part
  of the #146 dispatcher's one atomic CALL -> DISPATCHING compare-and-swap
  for one valid SupervisorOwnedTreeDispatchSubmissionV1. It carries a deeply
  immutable admission proof binding the call seal, call_id, variant, target
  tuple, use/lineage identity, and live dispatcher instance plus an unexported
  reference to that same atomic owner. It is outside every sealed value graph;
  retaining or racing aliases is explicitly permitted by the threat model and
  cannot duplicate owner state.

PrivateSupervisorOwnedTreeNativePlanV1 =
  dispatcher-local deeply immutable leaves-first target tuple plus the
  execution mode selected by the dispatcher only after one admission alias
  wins the named atomic
  owner transition DISPATCHING -> PLAN_OWNED; this value is neither an API type
  nor constructible outside the private dispatcher.

SupervisorOwnedTreeNativeInvocationV1 =
  private, nonserializable, noncopyable opaque invocation handle emitted exactly
  once alongside PrivateSupervisorOwnedTreeNativePlanV1 by the winning
  DISPATCHING -> PLAN_OWNED transition; it pairs that deeply immutable plan with
  an unexported reference to the same atomic owner. The private native body
  accepts only this handle and must win PLAN_OWNED -> INVOKING before lexical
  raw-array materialization or effect. Retained aliases and replay name the same
  owner and cannot duplicate invocation.

SupervisorOwnedTreeNativeKnownNoEffectV1 =
  private, nonserializable, noncopyable, deeply immutable, alias-free closed
  result:
    ACTIONS_DISABLED_NO_EFFECT {
      call_id, use/lineage identity, dispatch variant, and live
      dispatcher-instance identity source-equal to the winning invocation
    }
    | NATIVE_ENTRY_FAILED_NO_EFFECT {
        call_id, use/lineage identity, dispatch variant, and live
        dispatcher-instance identity source-equal to the winning invocation
      }

SupervisorOwnedTreeDispatchReceiptV1 =
  private, nonserializable, noncopyable, deeply immutable, alias-free result
  bound to the same consumed native invocation and call; its variant, target
  tuple, and use/lineage identity are source-equal to the corresponding fields
  carried by that invocation. Its native outcomes are produced by executing
  and remain bound to that invocation under the closed native-result algebra;
  only
  CHILDLESS projects into
  ChildlessExternalEffectReceiptV1

SupervisorOwnedTreeDispatchReceiptCustodyV1 =
  private, nonserializable, noncopyable opaque consumption handle emitted
  exactly once alongside one SupervisorOwnedTreeDispatchReceiptV1; it carries
  that sealed receipt plus an unexported reference to the same atomic owner.
  It is outside every sealed value graph. Retaining or racing aliases is
  explicitly permitted, and all aliases name the same RECEIPT owner state.

EphemeralTerminalReceiptApplyResultV1 =
  closed core EphemeralTerminalReceiptApplyResultV1 imported without redefinition

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
nested value is nonconforming. Consumers revalidate the private seal, matching
lineage/call/use identity, complete canonical content, binding, and target digest
before mutation or effect.

The atomic owner cells together with
`SupervisorOwnedTreeDispatchActionCustodyV1`,
`SupervisorOwnedTreeDispatchSubmissionV1`,
`SupervisorOwnedTreeDispatchAdmissionV1`,
`SupervisorOwnedTreeNativeInvocationV1`, and
`SupervisorOwnedTreeDispatchReceiptCustodyV1` are the private synchronization
boundary outside every sealed value graph. They are never serialized, hashed,
exposed through a public field, or accepted as evidence. Sealed permits, calls,
plans, and receipts carry only their immutable opaque proof. The CHILDLESS
constructor moves its existing guard lineage into `CALL`. For either
non-childless variant, #115 first fully constructs one dormant action-custody
candidate as part of the matching checked action transition; the successful checked
commit atomically activates and yields that candidate with its owner in `READY`.
No fallible allocation occurs between the checked-state transition and that
yield. A crash or handoff failure may lose the transient token and therefore lose
that action; it cannot reconstruct a token from the persisted transition. For a
kill-bearing configured transition, that same commit also persists the
`ConfiguredActionIssuerCheckpointV1`. The checkpoint records which live issuer
owned the one custody mint; its complete immutable #120 source target tuple and
digest are source-equal to the persisted barrier state's values. It is inert evidence: it is
neither custody nor a recipe for reminting custody.

The matching private call constructor fully constructs one dormant sealed call
and submission, then atomically consumes the pre-existing action owner
`READY -> CALL`. Only that compare-and-swap winner receives the call/submission
pair. Allocation failure before the compare-and-swap leaves `READY`; uncertain
handoff after it poisons the owner with `DISPATCH_PROTOCOL_BROKEN`. A concurrent
or sequential duplicate constructor, or a second mint attempt from one checked
transition, yields no call, submission, plan, receipt, mutation, planner
behavior, launch, or effect. There is no clone, lookup, reset, or reseal
constructor. An external registry, caller-provided mutex, call-ID set, or
dispatcher-global unstated lock cannot substitute for this action-scoped owner.
This contract is specified but conformance is **BLOCKED ON #115**, whose checked
transactions must mint and return the two non-childless custody variants.

This owner-loss rule does not remove the live same-invocation configured veto
path. While the captured issuer remains live and the configured action owner is
still `READY`, a final barrier or policy veto may apply the private
non-childless `PRE_BARRIER_RELEASE` only by atomically closing that same custody
as `REJECTED_NO_EFFECT` with its closed typed reason in the same #115 checked
transition. Both the custody close and state release commit, or neither does;
the path constructs no call or native plan. Once custody has left `READY`, the
issuer is not positively live, or reload is handling the persisted checkpoint,
that live-veto transition is unavailable and the owner-loss rule below applies.

When reload finds the matching configured checkpoint still paired with
`NON_CHILDLESS/RESERVED/PRE_BARRIER` but cannot validate the original live
custody owner, it returns typed `ConfiguredPreBarrierOwnerLossHoldV1` and
`POLICY_HELD(CONFIGURED_PRE_BARRIER_OWNER_LOST)`. The complete reservation,
checkpoint, barrier state, target evidence, and prior-effect-unknown fact remain
byte-identical. No automatic reducer may mint new custody, use the generic
non-childless `PRE_BARRIER_RELEASE`, synthesize a no-effect result, or clear and
re-enter through a later `RESERVE`. Exact-FILETIME retry safety answers *which
process a separately authorized repeat could affect*; it does not supply the
authority to repeat. The hold exists in both cases: `issuer_extinction` is
`PROVED_GONE` only from a fresh independent OS result whose queried PID is
source-equal to the checkpoint issuer PID and whose result is absent; otherwise
it is `LIVE_OR_UNPROVEN`. Any process present
at that PID remains unproven even when its generic token/start differs. Reuse
classification is `CAPABILITY_UNAVAILABLE(ExactIssuerIdentityAdapterV1)` and
produces no `RECYCLED` disposal result. Both render the remedy;
only definitive PID absence is eligible for the attended commit. Its source
hash is the domain-separated canonical hash of the core's canonical
persisted-field projection: agent, epoch/revision, reservation,
complete issuer checkpoint, reason, and prior-effect disposition. It excludes
the volatile extinction observation and attended-action projection, so one
byte-identical checkpoint produces a matching hash in `LIVE_OR_UNPROVEN` and `PROVED_GONE`
form; the commit independently rechecks extinction.

Revision 15 defines one attended escape from that automatic hold. The operator-
visible remedy is the future command surface
`agenttalk supervise --dispose-configured-pre-barrier --for AGENT
--hold-source-hash HASH --acknowledge-no-live-issuer
--acknowledge-prior-stop-tree-effect-unknown
--acknowledge-fresh-observation-required --from ACTOR --reason REASON`. These
three flags map in displayed order to the three named acknowledgements in
`AttendedConfiguredPreBarrierDispositionRequestV1`. This is a
distinct operation from merged `--reset-process-tree-ownership`: the merged
reset requires an assertion that owned processes stopped and may retire process-
tree ownership evidence, while this disposition permits the #120 identity-bound configured
target to remain live and preserves that evidence.

The hold rendering tells the operator to create or leave
`supervisor.kill` in place, stop every supervisor for the project, obtain the
refreshed hold source hash, and run
that complete prospective command as the liaison or sole lead. It labels the
command **specified, not delivered** until #115 implements the checked
transition; it must not imply that today's merged
`--reset-process-tree-ownership` accepts prior-effect-unknown disposition.

The command operates only on the official checked store while holding the
supervisor lifecycle, configuration, and #115 checked-state locks and while the
supervisor kill switch remains present. The current singleton marker must be
absent and rechecked before commit; if it is stale, the response names the
existing attended instance-marker repair prerequisite. The actor must be the
operator-facing liaison or sole lead. The current attention source hash, agent,
epoch, revision, reservation, checkpoint, barrier state, and target digest must
match field-for-field. A fresh OS observation independent of the dead issuer
must report the checkpoint issuer PID definitively absent. A present PID,
including suspected reuse from a generic token/start mismatch, remains held
pending `ExactIssuerIdentityAdapterV1`. An operator acknowledgement or the
persisted checkpoint cannot prove extinction.

The operator attests only that the abandoned configured action is being disposed
with its native kill outcome unknown and that no automatic retry is being
claimed. The operator does **not** attest that a kill happened, that no kill
happened, that the target tree is extinct, or that launch is safe. The checked
`PRE_BARRIER` checkpoint proves only that no `Start-Process` authority was
issued. Exactly one compare-and-swap may apply
`AttendedConfiguredPreBarrierDispositionDeltaV1`: it returns the outer execution
to `IDLE`, removes the issuer checkpoint, writes the same-poll terminal, resets
only action-selection-derived confirmations, and installs
`ConfiguredPriorEffectUnknownFenceV1`. It preserves managed identity,
continuity, establishment guard, `launching`, readiness, backoff, manual and
quarantine state byte-identically. Because this predecessor is top-level
non-childless state, no childless debt, automatic cycle, continuation, or
retired attempt may exist; the transition creates, clears, or resets none. It
preserves the complete #120 tree/barrier evidence byte-identically and records the prior-effect-
unknown fence alongside `IDLE`. It constructs no custody, kill, launch, or other
external effect.

After disposition the system may assume only that the dead issuer no longer
owns the reservation. The non-null `ConfiguredPriorEffectUnknownFenceV1` is a
global action gate: configured and childless reservation, kill, spawn, archive,
and launch remain unavailable. The operator next removes `supervisor.kill` and
starts exactly one current supervisor. The fence makes that supervisor
observation-only while removal of the kill switch re-establishes core
`PriorEffectFenceClearanceEligibilityV1.ELIGIBLE` when dry run is off, state
provenance is intact, and the current instance matches; clearance is ineligible
while the switch is present. Action-latch, report-membership, and auto-restart
gates do not block this no-effect clearance, but they still gate every later
action through full `ExecutionEligibilityV1`. Only a winning fresh ordinary-observation commit may publish one
`CommittedOrdinaryFenceCaptureV1`. #115's private deny-only reducer consumes it
and yields one source-bound #120 barrier-receipt custody, and only that custody
may clear the fence through the core's private `PRIOR_EFFECT_FENCE_CLEAR`
transition. A blocked or ambiguous barrier keeps the fence byte-identical and directs
attended handling of the #120 identity-bound surviving source targets; the clearance
transition itself never kills or launches. Thus normal checked selection resumes
only after clearance, no second owner-loss disposition can replace the first
fence, and childless origin cannot bypass it. Any later configured launch
additionally requires task #57's durable singleton.

The fence persists the disposition request ID, actor, acknowledgements, reason,
source checkpoint/targets, and result identity with each field source-equal to
its corresponding disposition or source-checkpoint value. While that fence remains
current, a response-loss replay of the same request returns the same audited
`DISPOSED` result without a second mutation. A different request cannot dispose
the successor state. After `PRIOR_EFFECT_FENCE_CLEAR` removes the fence, even the
original request is stale and no longer claims durable lookup beyond existing
87-B/audit scope.

Until `ConfiguredPreBarrierRetrySuccessorV1` is delivered and reviewed, this
attended delta is the **only** transition out of the owner-lost configured hold.
That future mechanism, not generic release logic, may define an automatic
checked retry mint. Neither the attended escape nor the future successor is
implemented by this document. The attended escape depends on #115's checked
owner; the optional automatic-retry successor is not a Q4 delivery blocker.

Every new member of this dispatcher family must state whether it keeps or skips
every family property; silence is nonconforming:

| Variant | Action-scoped issuance | Deep seal and dispatch ownership | Additional gate/dependency |
| --- | --- | --- | --- |
| `CHILDLESS` | **KEEP** the stronger unique live effect-guard lineage and one outstanding loan per acquisition; **SKIP** a second non-childless `READY` token because planner provenance is never its issuer. | **KEEP** every seal, admission, plan, invocation, receipt, and poison rule. | **KEEP** permit/witness/closure requirements; **SKIP** non-childless planner provenance. |
| `CONFIGURED_AGENT_RELAUNCH` | **KEEP** one #115-minted configured action owner in `READY`, bound to the checked reservation/barrier transition, plus one atomically persisted inert `ConfiguredActionIssuerCheckpointV1`. | **KEEP** every seal and owner transition; **KEEP** exact-FILETIME target semantics but **SKIP** automatic remint after issuer loss pending optional `ConfiguredPreBarrierRetrySuccessorV1`. | **KEEP** the owner-lost hold and definitive-`GONE` attended disposition; **KEEP** present-PID/reuse disposal unavailable pending `ExactIssuerIdentityAdapterV1`; **KEEP** #57's durable per-agent launch singleton; **SKIP** `EphemeralTerminalFinalActionGateV1` because its checked configured reservation/barrier gates are the independent authorization. |
| `EPHEMERAL_TERMINAL` | **KEEP** one #115-minted ephemeral action owner in `READY`, bound to the closed checked terminal transition. | **KEEP** every seal and owner transition; **KEEP** exact-FILETIME restart retry. | **KEEP** the terminal latch/kill-switch gate and stale-classifier-epoch receipt result; **SKIP** #57 because this variant never launches a wrapper and archives only after its fresh teardown barrier. |

The #146 dispatcher accepts only `SupervisorOwnedTreeDispatchSubmissionV1`.
After validating the handle's private provenance and owner binding, it must win
one atomic admission before native-plan construction: CHILDLESS compares and
moves the bound `LiveEffectGuardLineageOwnerV1` holder `CALL -> DISPATCHING`;
CONFIGURED_AGENT_RELAUNCH and EPHEMERAL_TERMINAL compare and move their bound
`SupervisorOwnedTreeDispatchUseOwnerV1` state `CALL -> DISPATCHING`. Only the
winner receives `SupervisorOwnedTreeDispatchAdmissionV1`. The private
`try_admit` primitive fully constructs a dormant admission candidate first; the
successful compare-and-swap atomically activates and yields that candidate
as its linearization result, with no fallible allocation between state change
and result. If lexical result handoff nevertheless fails, positive no-effect
cleanup resolves `DISPATCHING` according to the first table row below, or an
uncertain owner transition poisons the bound owner with
`CUSTODY_PROTOCOL_BROKEN` for CHILDLESS and `DISPATCH_PROTOCOL_BROKEN` otherwise.
A concurrent alias,
sequential replay, or stale receipt sees a non-`CALL` state and returns typed
`CALL_ALREADY_DISPATCHING_OR_CONSUMED` with zero native-plan construction and
zero effect. “Noncopyable” does not prevent aliasing the same object; the atomic
transition is the safety boundary.

The winner then revalidates the complete sealed call and variant-specific
provenance while the owner remains `DISPATCHING`. For `EPHEMERAL_TERMINAL` it
acquires the action-latch read guard and requires `ENABLED` with an epoch
matching `EphemeralTerminalFinalActionGateV1.action_latch_epoch`, then
retains that guard through native
issuance. Separately, it freshly requires the kill switch to remain clear before
native-plan construction and preserves the private native body's equivalent
final `Assert-ActionsEnabled`/kill-switch check. The latch guard does not freeze
the kill-switch file, and the two checks are not conflated. There is no wait
between the last checks and issuance. A kill-switch or latch flip after call
construction but before dispatch therefore yields
`FINAL_ACTION_GATE_CHANGED` before any native plan or effect; persisted
`next_entry` remains byte-identical. After validation and these outer gates, aliases
of the one admission must race the same bound owner transition
`DISPATCHING -> PLAN_OWNED`. Only its compare-and-swap winner may construct one
`PrivateSupervisorOwnedTreeNativePlanV1` and its matching
`SupervisorOwnedTreeNativeInvocationV1`; every losing or replayed admission
constructs no plan and produces zero effect.

The preserved private-native-body check runs before lexical raw-array
materialization. The private native-entry wrapper installs its exception
boundary before `PLAN_OWNED -> INVOKING` and fully constructs dormant
invocation-bound known-no-effect candidates before its first fallible post-CAS
operation. The private body first races every alias of the invocation handle
through `PLAN_OWNED -> INVOKING`; only that winner may run the check or
materialize the plan. Its native-effect frontier is the first instruction that
may materialize the lexical raw target array or begin target-handle/native
termination work.

If the kill switch changes after the outer fresh check or after native-plan
construction but before the inner check, the body returns the private typed
`ACTIONS_DISABLED_NO_EFFECT` result bound to the same invocation. If evaluating
that final gate or entering the private native body throws and the wrapper can
positively prove the frontier was not reached, it instead activates and returns
the matching dormant `NATIVE_ENTRY_FAILED_NO_EFFECT` result. Either result
materializes no raw array and attempts no native effect. Exactly one result
consumer may move CHILDLESS custody from `INVOKING` to one new `AVAILABLE`
proof, or move either non-childless owner to the matching terminal
`REJECTED_NO_EFFECT` reason. For EPHEMERAL_TERMINAL, persisted `next_entry`
remains byte-identical; configured `barrier_state` is likewise retained
byte-identically.

An exception at or after the frontier, an unknown program point, an escaped
exception, a missing result, or inability to validate the bound result is not
proof of no effect. No branch infers no effect from exception class, elapsed
time, a caller flag, a null/absent raw-array observation, or an empty/implicit
return.

An attended capture-sequence rollover may race an `EPHEMERAL_TERMINAL` transient
owner because that owner is not persisted `recovery_execution`. If rollover is
visible at the dispatcher's final classifier-provenance read before plan
ownership, it rejects the old epoch as `VARIANT_PROVENANCE_STALE` with positive
no effect. Once that read passes, rollover may win at `PLAN_OWNED` or `INVOKING`;
it may also win during the post-read portion of `DISPATCHING`. In all three
post-read cases the invocation may run, and epoch mismatch alone proves no
native outcome.

At receipt application, any trustworthy schema-valid official checked state for
the same agent whose current epoch differs from the receipt binding admits the
no-write stale branch; it does not require an unstored old-to-new UUID lineage.
If rollover's checked-state CAS won, exactly one receipt-custody consumer moves
`RECEIPT -> CONSUMING_RECEIPT`, returns
`EphemeralTerminalReceiptApplyResultV1.STALE_CLASSIFIER_EPOCH_RETRY_REQUIRED`,
and moves to `CLOSED`. It applies no old/new-epoch mutation, archive, launch, or
new native effect, does not infer the original native effect absent, and retains
persisted `next_entry`. If receipt application won first, it returns `APPLIED`
and closes; the original rollover request bound to that revision is stale and a fresh
exhaustion result/request is required. Unknown commit ordering poisons
with `PLANNER_COMMIT_UNCERTAIN`. Missing, untrusted, corrupt, or wrong-agent
current state cannot use the stale result. A new checked #115 action transition
against trustworthy current state is required before any exact-FILETIME identity retry
and fresh teardown barrier.

Dispatch resolution is total:

| Dispatch exit | Atomic-owner disposition |
| --- | --- |
| Admission handoff failure, full validation rejection, or final-gate rejection before `DISPATCHING -> PLAN_OWNED` | Consume the admitted call. CHILDLESS moves `DISPATCHING` to one new `AVAILABLE` lineage custody proof only with positive no-effect proof; either non-childless owner moves to terminal `REJECTED_NO_EFFECT(reason)`. Return a typed rejection and no receipt. An uncertain owner transition maps to childless `CUSTODY_PROTOCOL_BROKEN` or non-childless `DISPATCH_PROTOCOL_BROKEN`. |
| Admission alias loses `DISPATCHING -> PLAN_OWNED`, or invocation alias loses `PLAN_OWNED -> INVOKING` | Reject with no owner change, plan/raw-array construction, mutation, or effect. Sequential replay has the same result. |
| Plan/invocation-handle construction or handoff fails after `PLAN_OWNED` but before native entry | With positive no-effect proof, move CHILDLESS to one new `AVAILABLE` proof or either non-childless owner to `REJECTED_NO_EFFECT(ADMISSION_OR_PLAN_HANDOFF_FAILED_NO_EFFECT)`. If bound owner resolution is uncertain, poison as childless `CUSTODY_PROTOCOL_BROKEN` or non-childless `DISPATCH_PROTOCOL_BROKEN`. |
| Private native-body final kill-switch rejection after `PLAN_OWNED -> INVOKING` but before lexical raw-array materialization | Return invocation-bound typed `ACTIONS_DISABLED_NO_EFFECT`. Move CHILDLESS custody to one new `AVAILABLE` proof or either non-childless owner to `REJECTED_NO_EFFECT(FINAL_ACTION_GATE_CHANGED)` exactly once. Produce no receipt, raw array, or native effect; preserve ephemeral `next_entry` byte-identically. |
| Final-gate evaluation or private native-entry failure after `PLAN_OWNED -> INVOKING`, positively proved before the native-effect frontier | Return invocation-bound `NATIVE_ENTRY_FAILED_NO_EFFECT` and no receipt. Exactly one result consumer moves CHILDLESS `INVOKING -> AVAILABLE` and yields one successor lineage custody while preserving its checked `ARMED` effect envelope byte-identically; for either non-childless variant it moves `INVOKING -> REJECTED_NO_EFFECT(NATIVE_ENTRY_FAILED_NO_EFFECT)`, preserving configured `barrier_state` or ephemeral `next_entry` byte-identically. Result aliases/replay lose with no state change, custody, plan, raw array, planner behavior, or effect. |
| Native invocation returns a typed effect outcome | Fully construct a dormant sealed matching receipt plus `SupervisorOwnedTreeDispatchReceiptCustodyV1`, then atomically move the same owner `INVOKING -> RECEIPT` and yield that same custody handle. No fallible allocation occurs between the transition and yield. The sealed receipt alone is inert and is not accepted by a consumer. |
| Native-effect frontier may have been reached, or failure locus/native-return status is uncertain | Move CHILDLESS to `POISONED(ADAPTER_EFFECT_UNCERTAIN)` or either non-childless owner to `POISONED(NATIVE_EFFECT_UNCERTAIN)` exactly once. CHILDLESS preserves its persisted effect fence byte-identically; non-childless recovery follows its existing conservative planner state. No replay or rearm is permitted. |
| Pre-frontier no-effect is known, but invocation-bound result construction, handoff, validation, or owner resolution is uncertain | Move CHILDLESS to `POISONED(CUSTODY_PROTOCOL_BROKEN)` or either non-childless owner to `POISONED(DISPATCH_PROTOCOL_BROKEN)` exactly once. Never return custody or infer no effect from the absent result. |
| Receipt construction or handoff becomes uncertain after native return | Move CHILDLESS to `POISONED(RECEIPT_HANDOFF_UNCERTAIN)`; move either non-childless owner to `POISONED(RECEIPT_CONSTRUCTION_UNCERTAIN)` or `POISONED(RECEIPT_HANDOFF_UNCERTAIN)` at the identified failing boundary. Never return a second receipt or infer no effect. |
| Matching CHILDLESS receipt is consumed normally | The existing receipt-mutation constructor accepts only the matching `SupervisorOwnedTreeDispatchReceiptCustodyV1`, validates its sealed childless projection and owner binding, and atomically moves the bound lineage owner `RECEIPT -> PERMIT`; only its winner proceeds. Concurrent handle aliases and sequential replay produce zero mutation and zero effect. A synchronous failure after that CAS but before mutation poisons with `CUSTODY_PROTOCOL_BROKEN`; an uncertain checked commit poisons with `OWNER_COMMIT_UNCERTAIN`. Neither returns to `RECEIPT`, and the persisted effect fence remains byte-identical. |
| Matching current-epoch configured-agent or ephemeral receipt is consumed | The private planner continuation accepts only the matching `SupervisorOwnedTreeDispatchReceiptCustodyV1` and validates receipt/use/call/variant/owner equality. Before any existing planner behavior, exactly one consumer atomically moves its owner `RECEIPT -> CONSUMING_RECEIPT`. Only that winner applies the existing behavior, then moves `CONSUMING_RECEIPT -> CLOSED`. A synchronous failure before planner behavior moves to `POISONED(DISPATCH_PROTOCOL_BROKEN)`; an uncertain behavior or commit moves to `POISONED(PLANNER_COMMIT_UNCERTAIN)`. Neither returns to `RECEIPT`. Concurrent handle aliases and sequential replay produce zero mutation, launch, or effect. |
| Matching ephemeral receipt meets trustworthy same-agent official checked state with a different epoch | Before planner behavior, exactly one receipt-custody consumer wins `RECEIPT -> CONSUMING_RECEIPT`, returns `EphemeralTerminalReceiptApplyResultV1.STALE_CLASSIFIER_EPOCH_RETRY_REQUIRED`, and moves to `CLOSED`. Preserve `next_entry` byte-identically; perform zero classifier mutation, archive, launch, or new native effect, without claiming the original native effect absent. Receipt aliases and result replay are inert. Missing, untrusted, corrupt, or wrong-agent state cannot select this row. |
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
executor contract that may act on the bound owner and module-typed target tuple, but it grants
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
consumes its provenance plus the inert binding source-equal to the current checked
envelope's binding; the childless call carries
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
targetless cleanup scopes below, it also recomputes the bound owner and
authorized-target digest from the displayed tuple and proves any
operation-specific residual tuple is an order-preserving subset composed only
of members source-equal to the corresponding members of that immutable
authorization. A targetless scope instead proves its complete typed subject has
a binding source-equal to the selected historical tombstone or current envelope binding. Every
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

| Exit | Lineage disposition |
| --- | --- |
| Mutation permit commits with a known result | Move custody through `OWNER_COMMIT`, consume the permit, and return exactly one successor custody token in `AVAILABLE`. |
| Mutation commit outcome is uncertain | Atomically enter `POISONED(OWNER_COMMIT_UNCERTAIN)` exactly once. |
| External call is constructed | Move custody `PERMIT -> CALL`; exactly one matching dispatcher submission exists, and call construction alone cannot create a native plan. |
| Synchronous adapter admits the external call | Exactly one atomic `CALL -> DISPATCHING` compare-and-swap wins and atomically yields one admission before any external-effect plan. For `STOP_TREE`, #146 performs this through the private submission; every other childless adapter performs the equivalent private transition. Every concurrent alias or sequential replay loses with zero effect. |
| Admitted call is prepared for effect | Exactly one admission alias moves `DISPATCHING -> PLAN_OWNED` before any plan exists; exactly one invocation alias moves `PLAN_OWNED -> INVOKING` before native entry. Losing/replayed aliases construct no plan or effect. Non-`STOP_TREE` adapters use equivalent private owner stages even when they do not publish the dispatcher-specific type names. |
| Invoked call validates and returns normally | Move custody `INVOKING -> RECEIPT`; only the matching sealed receipt owns it. |
| Admitted synchronous adapter rejects before invocation with positive no-effect proof | From `DISPATCHING` or `PLAN_OWNED`, consume the call and return exactly one successor custody token in `AVAILABLE`; the old call/admission proofs and every alias remain consumed. |
| Invoked adapter yields a matching invocation-bound known-no-effect result | From `INVOKING`, validate and consume exactly one `ACTIONS_DISABLED_NO_EFFECT` or `NATIVE_ENTRY_FAILED_NO_EFFECT` result and return exactly one successor custody token in `AVAILABLE`; the old call/invocation/result proofs and every alias remain consumed. A missing or unbound result cannot return custody. |
| Adapter throws or loses receipt handoff after the effect may have begun | Atomically enter `POISONED` from the current holder named by the owner cell using only a `LiveEffectGuardLineageStateV1` cause: `ADAPTER_EFFECT_UNCERTAIN`, `RECEIPT_HANDOFF_UNCERTAIN`, or `CUSTODY_PROTOCOL_BROKEN`. Never return custody or infer no effect. |
| Receipt-mutation permit commits with a known result | Move custody `RECEIPT -> PERMIT -> OWNER_COMMIT`, consume the receipt and permit, and return exactly one successor token in `AVAILABLE`. |
| Receipt commit is uncertain or its checked envelope changed incompatibly | Atomically enter `POISONED` and keep the persisted effect fence byte-identical to its pre-commit value. |

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
permit arms a checked continuation whose fields are source-equal to that
permit's proposed continuation; only after that permit returns
custody may an `EXTERNAL_CALL` permit at the successor revision construct and
invoke the call; only by consuming the matching receipt-held custody may a
distinct `RECEIPT_MUTATION` permit at the then-current revision apply it with a
call-issuance binding source-equal to that receipt. An ordinary
observation CAS may intervene only when the effect envelope and continuation
remain byte-identical; the receipt-mutation constructor rechecks both and binds the new
revision. The already-invoked call/receipt lineage may span only those
observation revisions. The live-scope operand is a closed discriminated value:

- `RESERVE` permits only `STATE_MUTATION`, requires core
  `configured_prior_effect_unknown_fence == null`, and has two closed current-state
  scopes. `INITIAL` requires top-level execution `IDLE` and creates the first
  envelope/binding. `CONTINUE` requires an existing envelope whose execution is
  `IDLE`, with no current attempt, closure, pending disposition, continuation,
  or `RELEASE_PENDING` tombstone. For initial-mode retry with debt `NONE`, it
  may atomically replace the envelope's current binding with the prospective
  fresh tree binding only when the prospective owner identity matches the old
  binding owner. It keeps the same-owner cycle and terminal
  tombstones byte-identical under their own historical bindings. A physically different owner
  must first use `OWNER_TRANSITION` and then `RESERVE/INITIAL`; `CONTINUE` cannot
  bypass that boundary. For `DEBT_COMPLETION`, the prospective
  binding must be source-equal to the immutable envelope/debt binding and its targets must be
  an order-preserving subset composed only of members source-equal to the
  corresponding members of the authorized tuple. Both scopes install
  `PRE_BARRIER`; no
  other operation may create or renew a reservation.
- `PRE_BARRIER_RELEASE` requires a valid `PRE_BARRIER` envelope with null
  attempt, closure, continuation, spawn guard, and deadline. It is a state-only
  `STATE_MUTATION`; it does not imply an external effect.
- `TAKEOVER`, every closure operation, `RETIRED_ATTEMPT_RECONCILE`,
  `STOP_TREE`, `POST_ACTION_CAPTURE`, `EFFECT_FINALIZE`, `SPAWN`,
  `SPAWN_RESULT_COMMIT`, and `SPAWN_IDENTITY_COMMIT` require the current caller
  to hold the nonserializable effect guard. Their permits move the same unique
  lineage custody; the spawn call moves that custody into its typed call and
  receipt in the same way as every other external call does. First
  `CLOSURE_ACQUIRE` binds a proposed issuer continuation whose reservation,
  guard-owner, and expected-successor-revision fields are source-equal to the
  corresponding checked sources; its permit-bound mutation must install fields
  source-equal to that proposed continuation. Later operations require the
  matching current checked continuation, tombstone, or receipt stated below, or a
  proposed replacement continuation derived from it by the closed transition.
  `TAKEOVER` additionally requires positive predecessor-unwind or
  PID/start-death proof and authorizes only its no-call `STATE_MUTATION`. That
  mutation sets the operation source-equal to the predecessor operation as evidence but changes its
  stage to inert `TAKEOVER_CHECKPOINT`; it never writes `ARMED` for any later
  operation.
- `SPAWN` additionally requires core
  `configured_prior_effect_unknown_fence == null` at arm, call construction,
  and final native-entry validation. A non-null fence is a global no-launch
  gate; a childless permit, envelope, or closure cannot clear it or substitute
  for the core's winning-commit-bound
  `ConfiguredPriorEffectFenceBarrierReceiptCustodyV1`.
- A call-bearing `STATE_MUTATION` installs exactly one `OWNED/ARMED`
  continuation. Its subject is `ACTIVE_ATTEMPT` for closure, `Stop-Tree`, and
  post-action capture; `RETIRED_ATTEMPT` for retired reconcile/release; or
  `SPAWN_RESERVATION` for spawn. The post-CAS `EXTERNAL_CALL` permit requires
  that matching subject, operation, `ARMED` stage, `armed_state_revision`, and live
  guard. No raw ID, tombstone, reservation, or receipt can construct it. An
  `OWNED/ARMED` continuation for operation `O` is constructible only by an
  `O/STATE_MUTATION` permit. Applying operation `P`'s receipt may write only
  `P/CALL_RETURNED` or the closed receipt-successor state allowed by the table
  below; it may not arm a different operation. Chaining to a later call
  therefore requires a new checked commit with that later operation's own
  `STATE_MUTATION` permit.
- `EFFECT_FINALIZE` has one additional debt-only scope: the checked current
  envelope is childless `IDLE`, has no closure, pending disposition,
  continuation, or debt current attempt, and the current ordinary residual is
  matching `COMPLETE_GONE`. The caller still holds the transient effect guard;
  the permit authorizes only the atomic debt/cycle clear plus same-poll terminal
  and constructs no external call or launch.
- `SPAWN` requires a matching transient live-action scope plus the pre-spawn
  barrier result. Its `STATE_MUTATION` enters `SPAWN_IN_FLIGHT` and installs an
  matching `SPAWN_RESERVATION/SPAWN/ARMED` continuation; only a fresh post-CAS
  `EXTERNAL_CALL` permit can construct `Start-Process`. A synchronous
  `SPAWN_RESULT_COMMIT` or `SPAWN_IDENTITY_COMMIT` requires the matching typed
  spawn receipt and a fresh receipt-derived `RECEIPT_MUTATION` permit whose
  call-issuance binding is source-equal to that receipt.
  The only receipt-free result scope is `CRASHED_DURING_SPAWN`, which requires
  a fresh witness, an inert `SPAWN_IN_FLIGHT` envelope source-equal to the
  checked current envelope, the reacquired
  effect guard, and positive proof that the recorded issuing process/start
  cannot resume. Its persisted continuation must be either the existing dead-
  issuer `SPAWN_RESERVATION/SPAWN/ARMED` predecessor, with fields source-equal
  to the corresponding checked predecessor fields, or the specified table-
  authorized `SPAWN_RESERVATION/SPAWN/TAKEOVER_CHECKPOINT/RECONCILER` derived
  from that predecessor. It is a `STATE_MUTATION`, not a receipt mutation.
  `SPAWN_IDENTITY_COMMIT` also
  requires a guarded identity checkpoint matching the one returned for that
  spawn.
  Two other no-receipt state-only scopes are closed: a later strict checkpoint
  may construct `SPAWN_IDENTITY_COMMIT/STATE_MUTATION` only when it matches the
  guard retained in `AMBIGUOUS_LAUNCH`; and two compatible ordinary
  absence captures strictly after the ambiguity boundary may construct
  `SPAWN_RESULT_COMMIT/STATE_MUTATION` only to resolve that matching envelope to
  `IDLE` without launch. None may be built from a persisted
  `SPAWN_IN_FLIGHT`/`AMBIGUOUS_LAUNCH` value alone.
- `OWNER_TRANSITION` is state-only. It requires a fresh `GuardedLaunchCommitV1`
  identity checkpoint for a physically different owner, quarantine `NONE`, debt `NONE`,
  childless execution `IDLE`, no continuation or retired cleanup obligation,
  and a witness matching the old envelope binding. It may clear only that old
  owner's cycle/envelope while committing the new guarded owner; it cannot kill
  or launch.

The only targetless old-side permit scopes are: `RESERVE/CONTINUE` changed-tree
rebind over a binding source-equal to the old envelope binding plus same owner, no debt/current
obligation/continuation/`RELEASE_PENDING`, and the complete fresh prospective
owner/target tuple; takeover/reconcile/release/finalization of a typed retired-
attempt subject using a binding source-equal to that tombstone's historical binding; and
`OWNER_TRANSITION` over a binding source-equal to the old envelope binding. These
scopes still require a matching current-host witness, checked revision, effect
guard when applicable, and their complete typed subject/receipt/checkpoint.
They cannot construct `ExecutableOwnedTargetSetV1`, rewrite authorization or
debt, or act on a raw digest. Every other operation recomputes the complete
owner/authorized-target tuple and residual subset when applicable.

There is no generic null-scope fallback. Witness preflight runs before permit or
provider construction. Absence of a fresh current-host witness returns the corresponding typed
`CurrentExactTargetExecutorWitnessConstructionV1` reason:
`CAPABILITY_UNAVAILABLE(EXACT_TARGET_EXECUTOR_UNAVAILABLE)` when no conforming
target-identity executor exists for the host/binding, or
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
preflight failure returns the corresponding one-element reason tuple—
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
| `CLOSURE_ACQUIRE` | `CLOSURE_ACQUIRE` to bind/persist the matching result, or `EFFECT_FINALIZE` only for the event table's matching terminal `NEVER_ACQUIRED`/`RELEASED` case. |
| `CLOSURE_RECONCILE` | `CLOSURE_RECONCILE` to persist a `CALL_RETURNED` checkpoint whose operation is source-equal to the receipt operation, or `EFFECT_FINALIZE` only for an event-table terminal result. That terminal scope includes matching `RELEASED` after persisted `STOP_TREE/CALL_RETURNED`: it finalizes conservatively as `EFFECT_UNPROVEN`, keeps debt outstanding, and performs no residual-capture call. A later operation's own state-mutation permit performs any next arm. |
| `CLOSURE_RELEASE` | `CLOSURE_RELEASE` to persist a `CALL_RETURNED` checkpoint whose operation is source-equal to the receipt operation for matching `HELD`/`UNKNOWN`, or `EFFECT_FINALIZE` for matching `RELEASED` on the active disposition or retired cleanup subject. A later retry begins with its own `CLOSURE_RECONCILE/STATE_MUTATION` arm. |
| `RETIRED_ATTEMPT_RECONCILE` | `RETIRED_ATTEMPT_RECONCILE` to restore terminal state or persist `RELEASE_PENDING` plus the reconcile `CALL_RETURNED` checkpoint. Matching release is armed only by a later `CLOSURE_RELEASE/STATE_MUTATION` permit. |
| `STOP_TREE` | `STOP_TREE` to record `CALL_RETURNED`; it cannot finalize the effect. |
| `POST_ACTION_CAPTURE` | `POST_ACTION_CAPTURE` to map the typed observation and persist its `CALL_RETURNED` release-required checkpoint. Matching closure release is armed only by a later `CLOSURE_RELEASE/STATE_MUTATION` permit. |
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
remains. The binding fields of every target tuple, attempt, closure, debt,
continuation, pending disposition, and active cycle attempt inside it must be
source-equal to the envelope's current binding. A terminal retired attempt keeps
a binding source-equal to the historical binding under which it was
issued and may differ after a safe initial-mode retry rebind; its cleanup uses
only that tombstone binding. A `RELEASE_PENDING` tombstone forbids
reservation/rebind. Any other mismatch is a malformed envelope and grants no
permit.

The checked-state owner accepts no raw write to this envelope. It accepts only
a `PermitBoundChildlessMutationV1` whose expected revision and permit still
match, except for the four explicitly non-effect mutation classes below:
the core's owner-private ordinary-observation mutation, derived only by #115
from one sealed receipt and unable to address the envelope,
creation of an unresolved state-loss quarantine, and the attended maximum-
sequence epoch rollover that is constructible only when no childless envelope
or non-childless execution exists, plus
`AttendedConfiguredPreBarrierDispositionDeltaV1`, which is constructible only
over a predecessor whose fields are source-equal to the corresponding top-level
non-childless owner-lost values and cannot address
a childless envelope. Consequently a future
childless phase automatically inherits the same construction boundary. It may
deserialize evidence inside the envelope, but without a matching fresh witness
no **87-A childless** executor-dependent external effect and no childless
authority-enabling or effect-owned mutation is constructible. In particular, it
cannot construct an executable target, reservation, permit-bound mutation,
call, receipt, launch, or identity commit. Independently, no supervisor
owned-tree native termination is reachable except from one admitted
`SupervisorOwnedTreeNativeInvocationV1` that won admission, plan ownership, and
native entry over a private submission/owner pair. A non-childless variant carries
its own checked provenance and neither grants nor satisfies childless authority.
Revision 15 specifies both rules, but merged code does not enforce the second:
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
receipt inside `OrdinaryObservationCommitCustodyV1`, its bound owner binding,
the checked predecessor, and candidate
`PrivateClassifierObservationMutationV1`; after commit, the owner may validate
that the committed successor's ID is source-equal to that begin-bound ID. This module may not construct,
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
`agenttalk.supervisor.owned-targets.v1\0` plus `CanonicalJsonV1` of the following
closed object:
`{"owner_identity_id": <Hex64>, "schema": "owned-targets/v1",
"targets": <the displayed ordered tuple>}`. The initial complete tree,
residual observation, held closure, authority, reservation, and debt each
recompute that formula over their own displayed tuple. Equality is equality
of both the tuple and its recomputed digest; a copied digest never stands in
for the tuple. A residual digest therefore differs whenever its live subset,
whose members must be source-equal to corresponding authorized members, differs
from the immutable authorized tuple.
`process_source_digest` is SHA-256 over
`agenttalk.supervisor.owned-tree-coverage-source.v1\0` plus byte-identical
`CanonicalJsonV1` bytes of the core's `ObserverCoverageSignatureV1`; it
identifies coverage semantics, not changing process contents.
The 87-A adapter owns `observer_version`. For the admitted Windows mapping
pinned above it is
the literal value `win-tree/v2`, which binds merged task #120 at `587e7c1`, including its
`schema_version`, `attribution_model`, exact-FILETIME kill projection,
same-handle FILETIME identity check/termination plus conditional bounded wait attempt,
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
Windows observed root OwnedExactStartGuardV1
  == fresh #120 positive-decimal start_filetime
checked managed wrapper generation
  == strict runtime wrapper generation == runtime_wrapper_generation
checked managed launch nonce == parsed observed-root launch nonce
```

`StartRepresentationMatchV1(a, anchor)` first tests for byte-identical values;
otherwise, ISO tokens may represent the anchor's same instant within the shipped
one-millisecond Windows representation tolerance; non-ISO tokens still require
a byte-identical match. Every representation is checked independently against the one
named observed-root anchor—no chained or transitive inference is permitted. It
joins the checked/runtime/CIM representations but grants no destructive
authority. `wrapper_start_token` retains that anchor. On Windows, the retained
`wrapper_start_guard` and every target/parent start guard are the positive
decimal exact FILETIME from the complete #120 row and fresh same-handle FILETIME probe. A
rounded ISO token never substitutes for that guard.

The checked managed identity also supplies `state_epoch`,
`managed_generation`, and the expected launch nonce; the strict runtime record
supplies the independently validated agent/PID/start-representation/generation
binding. The shipped strict runtime record has no launch-nonce field and is not an unstated
nonce operand. The observed root's fields are source-equal to the guarded row's
fields in the same complete raw capture used for tree membership. The root target's PID,
`OwnedExactStartGuardV1`, and owner nonce are source-equal to those joined values.
`nonce_provenance` retains both actual nonce sources and their fixed parser
schema; each retained token is source-equal to the top-level `launch_nonce`.
Every other target is bound by task #120 to that guarded root and carries
an owner nonce source-equal to the root's.

Only a Windows observation with a positive exact FILETIME for every live row
may enter this positive owner/target join. A well-formed Linux process-start
identity token
remains valid #120 observation input; other non-Windows platforms have no
admitted process-start identity mapping. In either case the static capability gate returns
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
agent/PID/start-representation/`OwnedExactStartGuardV1`/generation operand returns
`INCOMPLETE`; an implementation may
not collapse provenance to the already-normalized top-level value.

Every target's `owner_launch_nonce` is source-equal to the positively guarded wrapper's
nonce. This does not claim that each descendant repeats the nonce in its
command line; task #120's complete ownership mechanism binds the descendant to
that owner.
For a Windows owned-tree target, the `Stop-Tree` projection is the following
closed object:
`{pid: target.pid, start: <fresh validated #120 row.start>, start_filetime:
target.start_guard, reason: "owned_process_tree", source:
"owned_process_tree"}` in root-first order. The rounded `start` is retained
only because the shipped executor's closed target shape requires it;
`start_filetime` is the destructive identity. The existing primitive reverses
the list, so leaves are attempted before the wrapper; every owned Windows target
missing its exact FILETIME is refused rather than falling back to the legacy
rounded `Proc-Start`/`Stop-Process` path. No non-Windows 87-A target projection
exists: at merged `587e7c1`, `Stop-Tree` executes the same-handle FILETIME identity check/termination
only at `src/agenttalk/supervisor.py:8900-8928` and skips an
`owned_process_tree` target with no FILETIME at `8930-8932`.

`COMPLETE_RESIDUAL` is not an initial tree with its root omitted. The 87-A
residual adapter over #120's retained snapshot and one fresh complete process
capture must positively prove that every still-live owner member belongs to an
order-preserving subset composed only of members source-equal to corresponding
members of the debt's immutable authorized tuple, that every omitted authorized
`(pid, OwnedExactStartGuardV1)` is gone, and that no new owner member
exists. Its recorded owner and nonce are source-equal to the checked debt's
owner and nonce; every live target's nonce and complete authorization-time
target object are source-equal to the corresponding checked-debt values. Residual
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
`DEBT_COMPLETION` has non-null debt fields and a complete residual tuple whose
members are source-equal to the current debt-residual observation's targets;
its root may already be gone. From the linearization point until
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
keyed by `acquisition_id`; release additionally requires a `closure_id`
source-equal to the held closure's `closure_id`. `AVAILABLE` exposes one
provider-bound `ClosureProviderVersionV1` and a
live one-shot `RESERVE` permit. It is well formed only for an installed,
independently reviewed implementation that can prove this contract inside the
absolute dependency-plane constraint for every case it accepts **and** for a
current witness from #146's private dispatcher-capability factory whose native body
actually acts on the target-identity
token. A valid observation token with no conforming platform/binding executor
returns `EXACT_TARGET_EXECUTOR_UNAVAILABLE`; a Windows token whose target-local
semantics exist but whose #146 dispatcher seal is absent returns
`DISPATCHER_SEAL_UNDELIVERED`. Parser or snapshot acceptance is insufficient.
`ClosureCapabilityV1.CAPABILITY_UNAVAILABLE` is structural and
may appear only before reservation. After reservation, failure to construct a
fresh permit is a current-host fact: the envelope remains inert and byte-identical, but
ordinary observation bookkeeping may still advance.
The caller persists an available provider version in the continuation's typed
`ACTIVE_ATTEMPT` subject before acquisition;
every `HELD`, `NEVER_ACQUIRED`, `RELEASED`, reconcile result, release request,
and held refresh must carry a provider-version field source-equal to the version
persisted in the current continuation. A provider contract change
requires a new version. Missing or mismatched versions normalize to `UNKNOWN`,
preserve every fence byte-identically, and grant no authority. A provider with no available
version is rejected by the static pre-reservation capability gate.
An acquisition result that claims `CAPABILITY_UNAVAILABLE` is malformed rather
than a transient closure veto: preserve every persisted fence byte-identically, emit continuous
`CAPABILITY_UNAVAILABLE`, and perform no teardown, retry, or exhaustion
transition. `UNKNOWN(CAPABILITY_UNAVAILABLE)` during reconciliation likewise
describes cleanup uncertainty, not permission to reclassify the issued attempt
as an ordinary failure.
Repeated acquisition returns the matching closure or a closed refusal. A
reconciliation result always requires its acquisition ID to be source-equal to the
persisted attempt ID. While `TREE_CLOSURE_ACQUIRING` has no persisted closure
ID, the first well-formed matching `HELD` or `RELEASED` may bind its returned
non-null closure ID in the same checked transition; on reload that binding is
release-only and never authorizes termination. `NEVER_ACQUIRED` is valid only
in that null-ID acquiring state. Once a closure ID has been bound, every
`HELD` or `RELEASED` result and release request must be source-equal to the
persisted pair. Missing, null, changed, or conflicting IDs normalize to
`UNKNOWN`, preserve the reservation and any debt/current attempt byte-identically, and forbid
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
with the matching pair and then reconciled to matching `RELEASED`; `UNKNOWN`
keeps state byte-identical. `HELD.raw_process_observation` is the immutable raw capture
returned under that closure, and its `capture_id` is source-equal to
`HELD.capture_id`. An action-ready acquisition or held-refresh capture has the
reservation's `state_epoch`, `agent_key`, and current
`ordinary_poll_sequence`, has nonzero `capture_ordinal`, and differs from the
ordinary `source_capture_id`. It is captured strictly after the closure
linearization point. A replayed pre-closure capture, ID without its raw object,
target tuple, or digest cannot stand in for it. Reconciliation `HELD` by itself
never authorizes a new termination; a caller needing current evidence obtains
a fresh held-refresh result under the same closure.

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

Revision 15 has four permanent V1 capability limitations that operators must
see together. The first, second, and fourth can remain active recovery holds;
the third refuses activation before imported state becomes an active checked
store. The same surface must also disclose both configured-action residuals
that prevent 87-A activation:

- On Linux and macOS, every closure-dependent named teardown returns
  `CAPABILITY_UNAVAILABLE(EXACT_TARGET_EXECUTOR_UNAVAILABLE)` and recovery
  remains `POLICY_HELD` pending a human. This persists until a separately
  reviewed process-start-identity kill adapter exists; #120's accepted Linux observation
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
- If any process is present at a configured PRE_BARRIER issuer PID, V1 cannot
  distinguish the original issuer from PID reuse. A live observation's generic
  start token may match the checkpoint token only as audit evidence;
  neither value is an exact-identity type or comparator. Attended disposal is
  `CAPABILITY_UNAVAILABLE(ExactIssuerIdentityAdapterV1)` and the hold persists
  until that separately reviewed adapter is delivered. A generic token/start
  mismatch and operator acknowledgement are both insufficient to upgrade the
  hold to `PROVED_GONE`.
- If a configured kill-bearing action loses its transient issuer while the
  durable state is `NON_CHILDLESS/RESERVED/PRE_BARRIER`, automatic recovery is
  unavailable and the agent remains
  typed `ConfiguredPreBarrierOwnerLossHoldV1` plus
  `POLICY_HELD(CONFIGURED_PRE_BARRIER_OWNER_LOST)`. The hold preserves the complete
  checkpoint byte-identically and says that the prior kill may or may not have happened. It names
  its prospective attended remedy: create or preserve `supervisor.kill`, stop
  every project supervisor, refresh the source hash, then run
  `agenttalk supervise --dispose-configured-pre-barrier ...` as the liaison or
  sole lead with the no-live-issuer, prior-Stop-Tree-effect-unknown, and fresh-
  observation-required acknowledgements. Only a fresh independent definitive
  PID-absent `GONE` result admits that checked operation; any present PID
  remains under the preceding capability limit. The operation never kills or
  launches and does not assert target extinction. Its persistent prior-effect
  fence globally blocks every new configured or childless effect/launch. After
  disposition the operator removes `supervisor.kill` and starts exactly one
  current supervisor; the fence makes it observation-only. A winning committed
  fresh observation may then yield the source-bound #120 barrier custody that
  clears the fence; a surviving source target remains held for attended
  handling. Clearance is ineligible while the kill switch remains present.
  Automatic exit remains unavailable; optional future
  `ConfiguredPreBarrierRetrySuccessorV1` is not a Q4 blocker, and no reload remint or silent
  release-and-reserve is allowed.
- Until task #57's durable project-level singleton per wrapped agent is delivered
  and reviewed, automatic configured relaunch is unavailable for activation.
  A crash or hard cancellation after `Start-Process` but before launch-result
  persistence can leave a live wrapper behind; replaying the persisted planner
  checkpoint could then start a duplicate wrapper. #120's exact-FILETIME rules
  make the preceding kill retry target-safe but do not make launch idempotent.
  This is a named implementation/activation blocker, not a claim that a running
  conforming agent returns a held capability result above.

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

All nine nullable evidence fields are null if and only if `count=0`. At count
one, every field is non-null and first and last capture IDs match. At
count two every field is non-null, capture IDs are distinct, and the last
ordinary sequence equals the first capture's sequence plus one.
At both nonzero counts, first and last capture IDs have
`state_epoch == ClassifierStateV1.state_epoch`,
`agent_key == ClassifierStateV1.agent_key`, and `capture_ordinal == 0`;
`last_ordinary_poll_sequence` is source-equal to
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
and candidate ChildEstablishmentGuardV1 is CLOSED for the matching current strict-turn binding
and ActiveChildObservationV1 == ABSENT
and core CHILD_DEAD sample basis is valid
and active_child_config_digest is source-equal to the config digest inside that basis
and OwnedWrapperTreeObservationV1 is COMPLETE
and tree.capture_id == raw ProcessObservationV1.capture_id
and tree.ordinary_poll_sequence == candidate ordinary_poll_sequence
```

Only the winning commit makes that candidate the current successor. A stale
CAS loser cannot reinterpret its candidate against the winner's successor. If
the predecessor sequence is maximum `uint64`, core #115 returns
`ATTENDED_REQUIRED(CaptureSequenceExhaustionV1)` before acquisition, so no
qualifying poll or module reduction exists. Its checked attended rollover is
admitted only from top-level `IDLE`; every childless envelope, including active
execution, debt, cycle, continuation, or retired-attempt state, blocks rollover
and remains byte-identical, as does every persisted non-childless execution.
An `EPHEMERAL_TERMINAL` transient dispatch owner is not persisted execution and
does not block rollover. Rollover visible at the final provenance read rejects
the old-epoch call before plan ownership; if rollover wins after that read the
invocation may complete without a no-effect inference, and its receipt closes
with the typed result
`EphemeralTerminalReceiptApplyResultV1.STALE_CLASSIFIER_EPOCH_RETRY_REQUIRED`
with zero new-epoch mutation, as specified above.

The module basis digest is SHA-256 over
`agenttalk.supervisor.owned-childless-confirmation-basis.v1\0` plus
`CanonicalJsonV1` of the following closed object:

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
config, complete closed establishment guard, owner, and coverage match.
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
compared directly with `owner.wrapper_start_guard`, which is the separate
`OwnedExactStartGuardV1` destructive identity. The current tree must be
`COMPLETE`, and every
state/owner/capture binding above must match. A second relevant wrapper,
incomplete tree, unguarded target, or targetability mismatch is `BLOCKED`,
never generic teardown fallback. `INITIAL` sets its tree owner and root-first
targets source-equal to the corresponding current-tree fields, and its capture
and coverage source-equal to the corresponding committed-observation fields;
recomputes the target digest from that tuple;
records the checked successor revision containing those observations; and sets
`source_condition_fingerprint` source-equal to the core
`RecoveryConditionFingerprintV1` for that committed observation.
Its owned confirmation basis, runtime child-death basis, active-child config
digest, and complete closed child-establishment guard are non-null and both
debt fields are null.

With outstanding debt and no current attempt, `DEBT_COMPLETION` exists if and
only if the current `OwnedDebtResidualObservationV1` is
`COMPLETE_RESIDUAL` with debt ID, generation, and owner fields source-equal to
the corresponding current-debt fields, its target
tuple is an order-preserving subset composed only of members source-equal to the
corresponding members of the immutable authorized tuple, and
every binding is current. It sets its residual capture, coverage, and tuple
source-equal to the corresponding current-residual fields; recomputes the
target digest from that tuple; records the checked successor revision containing
that residual; and sets `source_condition_fingerprint` source-equal to the core
same-revision fingerprint for that committed residual observation. Its four
childless/runtime/config/establishment basis fields are null and both debt
fields are non-null. A
`COMPLETE_GONE` residual observation is inert input; only a fresh matching
`EFFECT_FINALIZE` permit may construct the checked mutation that clears
debt/cycle and writes the same-poll terminal. It constructs no authority or
launch. The #115 owner-private observation mutation cannot perform that clear.
`INCOMPLETE` preserves debt byte-identically and constructs `BLOCKED(TEARDOWN_DEBT_INCOMPLETE)`.
Closure is never a precondition of either authority constructor.

`basis_id` hashes
`agenttalk.supervisor.childless-teardown-basis.v1\0` plus `CanonicalJsonV1` of
the following closed object:

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

`source_committed_revision` is source-equal to the checked state successor revision
that atomically persisted `source_capture_id` and all source observations.
`source_condition_fingerprint` is source-equal to the core fingerprint computed from
those same committed observations. The authority and reservation evidence fields
are respectively source-equal to those two source values, which are included byte-identically in
the canonical `basis_id` preimage; a caller can
reconstruct the identifier without an unstated current-state choice.

`authority_id` hashes
`agenttalk.supervisor.provably-childless-owned-wrapper-authority.v1\0` plus
canonical `{"basis_id": ..., "mode": "INITIAL" | "DEBT_COMPLETION",
"schema": "provably-childless-owned-wrapper-authority/v1",
"target_digest": ...}`.

Every displayed proof object is inert. The fresh reservation constructor must
consume it together with a current witness, recompute its tuple/digest, create
the corresponding `ExactTargetExecutorBindingV1`, and emit one
`PermitBoundChildlessMutationV1` whose next state is
`CHILDLESS(ChildlessEffectEnvelopeV1)`. No caller may copy the fields directly
into checked state. Neither an authority hash, the generic targetability
digest, nor a command-line match is decoded or substituted for the target
tuple.

`ChildlessClosureEvidenceV1` is the 87-A join over the closure-successor result
and current checked state; the successor does not decide authority. It is valid only when the
closure acquisition ID is source-equal to the persisted attempt ID; its mode,
owner/coverage/targets/digest/debt fields are source-equal to both the reservation
and `OwnedTreeClosureV1.HELD`; its closure-provider version is source-equal to the
persisted continuation owner's version; authority/basis IDs are source-equal to
the reservation's IDs; and every target digest matches a fresh
`OwnedTargetDigestV1` recomputation.

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
  a live reconstruction of the core's specified CHILD_DEAD basis from
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

The checked owner identifies the supervisor's named process/start identity, applicable
action-latch epoch, continuation role, typed subject, operation, stage, and
state revision. The invocation
acquires the effect guard, recaptures every gate and basis, commits
`effect_stage=ARMED` with a state-mutation permit, constructs a distinct fresh
post-CAS call permit, rechecks that it still owns both the guard and the same
checked owner, privately constructs the sealed call, and wins its atomic
`CALL -> DISPATCHING` adapter admission before any external-effect plan. For
`STOP_TREE`, the private constructor also emits the one dispatcher submission
and #146 returns the admission proof before native-plan construction. The
synchronous operation then yields exactly one matching receipt, yields exactly
one invocation-bound `SupervisorOwnedTreeNativeKnownNoEffectV1` with the
displayed return/reject transition, or poisons its owner with the displayed
cause. Only a matching receipt may construct a third fresh receipt-derived
mutation permit whose call-issuance binding is source-equal to that receipt
before committing `CALL_RETURNED`
or the operation-specific terminal result. An adapter accepts a retired attempt only
through its matching typed retired subject and rejects any caller whose persisted
continuation owner no longer matches. Thus committing a phase never, by itself,
licenses a later stale continuation.

For every syntactically valid checked state, Revision 15 specifies two separate
structural rules. The childless rule is narrower and stronger than Revision 9's
path-enumeration claim:

```text
without a fresh CurrentExactTargetExecutorWitnessV1 that is source-bound to the
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
exists, “matching the persisted binding” means matching the prospective binding
carried by the one-shot permit and atomically persisting binding fields
source-equal to that prospective value
as part of the envelope-creation mutation. `RESERVE/CONTINUE` matches the
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

Three exceptional non-effect operations outside ordinary observation and the
live non-childless transition table are explicit.
`StateLossQuarantineCreationDeltaV1` applies only when no trustworthy current
state/revision exists to mutate; it replaces unreadable or unproven bytes with
a new quarantined genesis that exposes less authority and no childless effect
object. `CaptureSequenceRolloverDeltaV1` applies only to trustworthy
maximum-sequence top-level `IDLE` state under an attended #115 checked
replacement; it is blocked by every childless envelope or persisted
non-childless execution and constructs no effect object.
`AttendedConfiguredPreBarrierDispositionDeltaV1` is a checked **mutation**, not a
state-loss or epoch replacement. It accepts only the matching trustworthy top-level
`NON_CHILDLESS/RESERVED/PRE_BARRIER` predecessor and dead-issuer proof described
above, reduces authority, and installs the prior-effect-unknown fence. None is a
constructor over a valid childless envelope; none may compose with another
variant, an observation mutation, or a childless effect delta; no additional
exceptional replacement/disposition variant exists.

Revision 10 explicitly withdrew Revision 9's broader promise that the entire
checked state remains byte-identical on such a poll. A valid owner-private
ordinary-observation mutation may increment
`ordinary_poll_sequence`, reset
`next_capture_ordinal`, clear the prior same-poll terminal, and update its
observation-only projection. Its type cannot address the childless effect
envelope. Quarantine creation, attended capture-sequence rollover, and attended
configured PRE_BARRIER disposition are the other permit-free non-effect
mutations: quarantine strictly removes authority, rollover is unavailable
whenever a childless envelope exists, and configured disposition is unavailable
for every childless or non-PRE_BARRIER predecessor. No #115 owner-private observation
mutation may clear or
rewrite a reservation, closure, debt, cycle, continuation, pending
disposition, retired attempt, spawn state, or ambiguity state.

A non-`NONE` owner is well formed only when its token digest and PID/start are
source-equal to the corresponding fields of the arm-time `ExecutionGateCaptureV1` current
supervisor; its typed subject is source-equal to the active
reservation/attempt, one retained
tombstone, or the spawn reservation required by its operation; and
`armed_state_revision` is the successor revision that wrote that owner.
`CLOSURE_ACQUIRE`, callable `STOP_TREE/ARMED`, and `SPAWN/ARMED` require
`role=ISSUER` and non-null
`action_latch_epoch` matching the epoch of a fresh enabled action latch.
Non-destructive `CLOSURE_RECONCILE` and
`CLOSURE_RELEASE`, `RETIRED_ATTEMPT_RECONCILE`, and
`POST_ACTION_CAPTURE` keep an action-latch epoch source-equal to the current
epoch when the latch is enabled and
use null when cleanup is permitted under a disabled latch.
Here `supervisor_start_guard` and the execution-gate supervisor start are both
generic `ProcStartGuardV1` representation tokens. Their arm-time fields are
source-equal to the corresponding `ExecutionGateCaptureV1` fields and bind the
live continuation; neither is compared with an owned-tree
`OwnedExactStartGuardV1`.
`STOP_TREE/CALL_RETURNED` retains the arm-time epoch as checked evidence that
the destructive call already returned; a post-action capture under that state
is non-destructive and may proceed through the narrow cleanup gate even if the
latch later disables. The pre-call recheck requires the operation-specific
values still match a fresh gate capture. Any mismatch is a veto before a new
external call, not permission to continue under the older owner.

`role=ISSUER` requires `takeover_origin=NONE`. `role=RECONCILER` is first
introduced only by a takeover CAS and requires `takeover_origin=FROM` carrying
continuation ID, operation, and stage fields source-equal to the corresponding
immediate-predecessor fields. The
takeover itself keeps the predecessor operation, writes inert
`TAKEOVER_CHECKPOINT`, and never writes any operation at `ARMED`; at that
checkpoint `operation == takeover_origin.predecessor_operation`. No adapter
accepts the checkpoint. Only after reload may a distinct operation-specific
`STATE_MUTATION` permit replace it with the closed table's next arm. That arm
and its `CALL_RETURNED` checkpoint carry `takeover_origin` fields source-equal
to the takeover checkpoint's field so their
phase/operation provenance remains structurally checkable. A later distinct
post-CAS call permit is still mandatory. Another takeover replaces the origin
with fields source-equal to its immediate predecessor's fields; it cannot erase
or invent a stage.

A different poller cannot reconcile while the persisted continuation may
resume: it returns `RETAIN_LIVE_CONTINUATION`. Takeover is permitted only
after acquiring the released effect guard and positively proving either
same-process structured unwind/cancellation or that the guarded predecessor
PID/start no longer exists. Mere age, missing heartbeat, a timeout, or
different supervisor token is not proof. The takeover writes the specified
`role=RECONCILER/TAKEOVER_CHECKPOINT/takeover_origin=FROM` owner before any
reconciliation operation.
Its closed no-call mapping is behavioral definition, not the universal proof:

| Predecessor obligation | Takeover checkpoint state | Only next arm/result mutation |
| --- | --- | --- |
| acquiring closure | acquiring state and operation source-equal to the predecessor values | `CLOSURE_RECONCILE` |
| held closure | held state and operation source-equal to the predecessor values; teardown stays forbidden | `CLOSURE_RECONCILE` |
| releasing closure, including post-action capture | disposition, debt, and operation source-equal to the predecessor values | `CLOSURE_RECONCILE` |
| `STOP_TREE/ARMED` | map to releasing/`EFFECT_UNPROVEN`, keep debt source-equal to predecessor debt, and set the operation source-equal to predecessor `STOP_TREE` | `CLOSURE_RELEASE` |
| `STOP_TREE/CALL_RETURNED` | keep the returned-effect fact and debt source-equal to the predecessor values, and set the operation source-equal to predecessor `STOP_TREE` | `CLOSURE_RECONCILE`; then `POST_ACTION_CAPTURE` only after matching `HELD`, or receipt-bound `EFFECT_FINALIZE/EFFECT_UNPROVEN` after matching `RELEASED` |
| retired-attempt cleanup | childless `IDLE`, tombstone, and operation source-equal to the predecessor values | `RETIRED_ATTEMPT_RECONCILE` |
| `SPAWN/ARMED` | `SPAWN_IN_FLIGHT` state and operation source-equal to the predecessor values | crash-only `SPAWN_RESULT_COMMIT` with positive issuer-death proof |

Any predecessor not admitted by this closed table is invalid and remains inert.
A matching
`NEVER_ACQUIRED` is a stable terminal result for that acquisition attempt and
retires the attempt in the same transaction; a later unexpected `HELD` is
release-only and can never restore authority. Any result that cannot be
classified unambiguously into a named result is `UNKNOWN` and keeps the fence byte-identical.

After the checked reservation, an invocation under that guard uses a fresh
`CLOSURE_ACQUIRE/STATE_MUTATION` permit to persist a fresh attempt ID, the specified
`ACTIVE_ATTEMPT/CLOSURE_ACQUIRE/ARMED/ISSUER` continuation, and
`TREE_CLOSURE_ACQUIRING` inside the same envelope. Automatic origin also
creates or increments its cycle in that mutation. At the successor revision,
only a distinct fresh `CLOSURE_ACQUIRE/EXTERNAL_CALL` permit may construct the
typed call. The closure adapter accepts no raw attempt ID and returns a receipt
bound to that call permit, binding, continuation, and checked revision. The
invocation retains the effect guard while a third, fresh receipt-derived
`CLOSURE_ACQUIRE/RECEIPT_MUTATION` permit whose call-issuance binding is
source-equal to the receipt applies the result. Acquisition and
crash reconciliation remain idempotently keyed by the attempt ID. 87-A must then construct
`ChildlessClosureEvidenceV1`; the closure object alone never supplies
authority. A well-formed current equality mismatch is a closure veto and
follows matching typed release. A well-formed envelope plus malformed closure-successor
output is `POLICY_HELD`; if its closure pair is source-equal to the corresponding
checked continuation fields, only permit-bound
non-destructive reconciliation/release may proceed. Structurally invalid
checked state instead selects `RETAIN_INVALID_FENCE` before witness/permit
construction and makes no mutation or call. Neither case kills or launches.

Only after that matching check may a receipt-derived mutation persist the
closure ID while keeping its acquire/reconcile owner fields source-equal to
their prior values at `CALL_RETURNED`.
Before termination, one fresh `STOP_TREE/STATE_MUTATION` permit constructs a mutation that atomically
persists teardown debt, enters `TEARDOWN_IN_FLIGHT`, and records
the specified `ACTIVE_ATTEMPT/STOP_TREE/ARMED/ISSUER` continuation. At the successor
revision, and only after arm custody returns, a second fresh
`STOP_TREE/EXTERNAL_CALL` permit constructs `ExecutableOwnedTargetSetV1` and the
sealed typed childless call. #146's private factory atomically emits that call
as `SupervisorOwnedTreeDispatchCallV1.CHILDLESS` plus its one opaque submission;
the dispatcher admits only the `CALL -> DISPATCHING` winner and accepts neither
a raw tuple, planner array, caller tag, persisted state, nor call without its
paired atomic owner. On
synchronous return, only the matching typed receipt plus a third, fresh
receipt-derived `STOP_TREE/RECEIPT_MUTATION` permit whose call-issuance binding
is source-equal to that receipt can construct the `CALL_RETURNED`
mutation; only that stage may arm a three-stage permitted post-action capture.
If the owner dies or unwind is proved while still
`ARMED`, recovery never reissues `Stop-Tree` or assumes it did not run: a
permitted reconcile/release mutation records `EFFECT_UNPROVEN`, keeps debt outstanding,
releases any matching closure, and forbids launch. No copied
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

A complete result requires `closure_id`, `owner_identity_id`, and
`authorized_target_digest` fields source-equal to the corresponding held-closure
and current-envelope fields,
complete process and closure-membership coverage, an unblocked fresh #120
post-kill launch-barrier result over the same captured process rows, and a
capture with the same
state epoch, agent, and ordinary poll sequence whose ordinal is strictly
greater than the action-ready closure capture. `COMPLETE_GONE` means every
authorized PID/start is positively absent, closure membership is empty, and
the #120 barrier is clear: it finds no recorded-identity survivor,
conservative old-side descendant edge, same-agent wrapper/wait process, or
other blocking reason under its specified recycled-parent split.
`COMPLETE_RESIDUAL.live_targets` is an order-preserving live subset composed
only of members source-equal to the corresponding members of the immutable
authorized tuple and its digest is a recomputed
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
Every failure keeps debt outstanding and forbids launch until release handling finishes.
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
Each later residual action increments it by one and keeps the owner and
authorized tuple source-equal to their predecessor values. `current_attempt_id` and revision are either both
non-null with `last_outcome=ISSUED`, or both null with a failure outcome.
The pair is non-null if and only if core execution is
`TEARDOWN_IN_FLIGHT` or non-veto `TREE_CLOSURE_RELEASING`, with the same
attempt pair. `CLOSURE_VETOED` release has both debt attempt fields null.
Mismatch is invalid state and `POLICY_HELD`.

`TeardownDebtV1` is inert persisted evidence and is well formed only inside a
`ChildlessEffectEnvelopeV1` whose binding matches its owner and recomputed
authorized target digest. Neither an ordinary observation nor a raw debt value
can clear or rewrite it. Every debt mutation is carried by a
`PermitBoundChildlessMutationV1`; the payload schema and its banked `debt_id`
formula remain fixed.

At the initial arm, `debt_id` is SHA-256 over
`agenttalk.supervisor.childless-teardown-debt.v1\0` plus canonical
`{"initial_attempt_id": ..., "initial_authority_id": ...,
"owner_identity_id": ..., "state_epoch": ...,
"target_digest": ...}`. It never changes during residual completion.

### Chained digest conformance vector

**FROZEN CONFORMANCE EVIDENCE:** The following Revision 8 fixture remains
byte-identical through Revision 16. It fixes all seven module digest domains and renews the
authority-dependent chain for merged #120's
`win-tree/v2` adapter and the explicit
representation-token/`OwnedExactStartGuardV1` split.
The two banked core condition fingerprints are outside this chain and remain
untouched.
Each payload is byte-identical to the one-line ASCII/UTF-8 `CanonicalJsonV1` byte sequence
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
- construct `DEBT_COMPLETION` authority only for an order-preserving live
  residual subset composed only of members source-equal to corresponding
  authorized members from `COMPLETE_RESIDUAL`, then require a new
  post-reservation `HELD` closure;
  or
- preserve debt byte-identically and retain continuous attention for `INCOMPLETE`/changed membership.

`DEBT_COMPLETION` targets only the immutable live residual. This keeps an
orphaned conhost/shell/tool reachable after the wrapper root exits. Debt alone
never authorizes a new identity.

Every finalized childless reservation/attempt outcome or permit-bound debt
reconciliation from ordinary residual evidence writes
`recovery_poll_terminal_sequence=ordinary_poll_sequence`. Pure refusal,
retained closure uncertainty, prior-poll exhaustion, and a no-op
`NOT_ATTEMPTED` row that leaves debt/cycle/execution byte-identical do not. The
`NOT_ATTEMPTED` emitted after successful permit-bound ordinary-evidence or
reload cleanup is
a finalized reconciliation and does write the terminal. The reservation
predicate rejects equality. The next ordinary-poll increment clears the
terminal. Thus one poll cannot consume two automatic attempts or turn
finalized reconciliation into immediate launch.

## Fail-closed state-loss quarantine

**SPECIFIED; implementation blocked on task #115:** A missing, corrupt, torn, or rollback-unproven
checked state is not clean genesis. When prior checked state is unavailable or
untrusted, recovery creates a new `state_epoch` only with
`StateLossQuarantineV1.UNRESOLVED`; the physical owner projection
deliberately excludes that epoch and is diagnostic only. Quarantine creation is
an explicit task #115 transaction and the sole permit-free mutation that may
replace unknown childless effect state: it creates the new epoch and
`UNRESOLVED` together, grants no authority, and makes no external call. The
separately permit-free attended capture-sequence rollover applies only to
trustworthy top-level `IDLE` state and cannot replace unknown or valid childless
effect state. The
quarantine denies the named teardown,
every other kill, every launch/relaunch, closure acquisition, attempt
increment/reset, debt clear, marker consumption, managed-owner commit, and
grace-based recovery. It emits continuous
`CHILDLESS_STATE_PROVENANCE_LOST` attention. Manual acknowledgement or force
cannot override it.

The core's separate attended capture-sequence rollover begins from trustworthy,
schema-valid maximum-sequence state and is not state-loss recovery. It may
replace only top-level `IDLE`; any childless envelope or persisted non-childless
execution blocks it. An ephemeral transient action owner instead becomes stale
by the specified receipt rule above. Rollover therefore cannot replace, rebind, or clear unknown or active
childless effect state and does not weaken this quarantine rule.

This revision intentionally defines no state-restoration constructor. The
shipped store may accept a structurally valid backup that is one committed
generation behind; schema validity therefore cannot prove that an apparent
restoration contains the lost latest attempt count or teardown debt. Such a
backup remains `ROLLBACK_UNPROVEN` and quarantined. Adding an independently
retained last-commit revision/digest would change the persistence contract and
requires a separately reviewed versioned design.

Revision 15 defines no automatic V1 quarantine-clear transaction. The previous
`ProvablyDifferentPhysicalOwnerV1`/local `COMPLETE_GONE` carve-out is withdrawn:
`OwnedPhysicalWrapperIdentityV1` has no host/process-universe operand, and a
complete local absence on host B cannot prove that the copied prior wrapper or
erased-debt members are extinct on host A. The type is host-local evidence, not
a globally unique owner identity. `retirement_capability` therefore remains
`CAPABILITY_UNAVAILABLE(PROCESS_UNIVERSE_IDENTITY_UNAVAILABLE)` on every
platform, even when a distinct local replacement exists.

A future version may add retirement only if it persists a trustworthy source
host/process-universe binding before loss and obtains extinction coverage for
that same identity-bound universe from a reviewed read-only producer over an existing OS
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
owns the same source-bound owner, last attempt ID, and revision. The next attempt
transition is only `NONE -> 1` or same-owner
`ACTIVE/failure(n) -> ACTIVE/ISSUED(n+1)`. Every other shape is invalid and
`POLICY_HELD`.

An automatic attempt is consumed by the checked transition that records
`TREE_CLOSURE_ACQUIRING` with its attempt ID immediately before invoking the
closure successor after matching typed `ClosureCapabilityV1.AVAILABLE`. Planning,
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
old envelope plus a matching new guarded checkpoint; it is not an observation
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
    DEBT_COMPLETION only if the matching residual authority is constructed
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

Core `CaptureSequenceExhaustionV1` is a separate typed attended condition, not a
childless retry code. When a childless envelope exists, its blocker projection
names that envelope and every applicable execution/debt/cycle/continuation/
retired-attempt reason; the attended rollover is refused and the envelope stays
byte-identical. 87-B names the agent and exhausted epoch/revision and directs
attended handling. It must not collapse this state into automatic retry,
quarantine creation, or retry-cap reset.

87-B must join an action resolution to the matching fingerprint and take
the held agent name from `RecoveryConditionV1.canonical_condition.agent_key`.
Every routine or incident rendering of `CAPABILITY_UNAVAILABLE` must name that
agent and state explicitly that operator action is required. Across 87-B
projections, 87-C activation surfaces, and the required operator material, the
four permanent V1 capability limitations must remain distinct: POSIX named
teardown lacks a process-start-identity target executor; automatic quarantine retirement lacks a
trustworthy process-universe identity; and declared same-platform state-
file/workspace transfer, restore, rollback, or migration must refuse before
active-store admission; additionally, a process present at the recorded
configured issuer PID makes attended disposal unavailable until
`ExactIssuerIdentityAdapterV1`, because generic start-token equality or
mismatch cannot classify PID reuse. The same surfaces must name both configured residuals:
the complete bounded `ConfiguredPreBarrierOwnerLossSummaryV1` plus
`CONFIGURED_PRE_BARRIER_OWNER_LOST`, its complete checkpoint/current source hash,
prior-effect-unknown posture, missing optional retry successor, GONE-only
attended eligibility, named exact-identity adapter limitation, and specified attended
remedy; after disposition, the complete bounded
`ConfiguredPriorEffectUnknownFenceSummaryV1`
plus `CONFIGURED_PRIOR_EFFECT_UNKNOWN`, its prescribed remove-kill-switch/start-one-
current-supervisor/committed-fresh-barrier/attended-target clearance remedy, and
the global no-action/no-launch gate; and #57's
configured-relaunch duplicate-wrapper residual, with activation
blocked until its durable per-agent singleton is delivered. A bare enum, an
unnamed agent for any active hold, a message that collapses a hold into a
transient retry, a remedy-free owner-loss/fence hold, a silent activation refusal, or
omission of either configured dependency/consequence is nonconforming. The
activation refusal identifies the rejected operation/store; it does not
fabricate a held agent.

In the original surviving live invocation, a successful gone proof after
verified release has no terminal module result: the core's final barrier/spawn
determines `BARRIER_VETOED`, `SPAWN_FAILED`,
`IDENTITY_COMMIT_AMBIGUOUS`, or `LAUNCH_COMMITTED`. Reload or reconciliation
without that live continuation instead emits the table's terminal-writing
`NOT_ATTEMPTED` cleanup result and never launches.
The summaries are deterministic projections of checked state. They omit the
issuer instance token/PID/start, action-latch epoch, full source-target tuple,
`authority_id`, `checked_reservation_transition_id`, disposition actor,
acknowledgements, and free-text reason; those internal authority/audit fields
never cross the 87-B export boundary.

## Closed state transitions

The safety property is established by a closed construction pipeline, not by
enumerating every state that might enter it:

| Construction boundary | Only conforming operation |
| --- | --- |
| Deserialize | Produce inert `ChildlessEffectEnvelopeV1` evidence. Deserialization never yields a witness, permit, executable target, mutation, call, or receipt. |
| Ordinary observation | The installed observer adapter atomically yields one `OrdinaryObservationCommitCustodyV1` pairing a sealed `OrdinaryClassifierObservationReceiptV1` with the lineage owner that produced it. #115 accepts only that custody handle; inside the checked RMW the owner alone derives and commits the private observation mutation, which cannot address the effect envelope. |
| Validate | Validate the complete envelope and bound binding before consulting host capability. A malformed envelope deterministically yields `POLICY_HELD`; malformed state precedes capability-unavailable reporting. |
| Witness/preflight | Only #146's unexported dispatcher-capability factory may mint `CurrentExactTargetExecutorWitnessV1`, before any permit or call exists; merged raw `Stop-Tree`, non-childless variants, and every other layer may not. Static inability to serve the binding or an undelivered dispatcher seal yields capability hold before guard acquisition. |
| Guard and permit | Acquire the effect guard when required, create its one unique lineage, and atomically move sole custody `AVAILABLE -> OUTSTANDING` while matching witness, binding proof, checked revision, guard/continuation, and one operation/use. A second issuance from the acquisition is unconstructible. `REJECTED` yields zero effect plus reload/reject, not capability hold. |
| Childless-envelope mutation | Task #115 accepts only `PermitBoundChildlessMutationV1`; the owner algebra separately admits pure observation, private non-childless authority, fail-closed quarantine creation, and attended capture-sequence rollover variants. None may address a current childless envelope, and rollover is blocked while one exists. |
| External effect | A childless adapter accepts only a deeply sealed `ChildlessExternalEffectCallV1` and returns only its matching sealed receipt. Owned-tree termination additionally requires the matching private submission/owner pair and winning `CALL -> DISPATCHING`, `DISPATCHING -> PLAN_OWNED`, and `PLAN_OWNED -> INVOKING` transitions for the closed `CHILDLESS` variant; only the resulting invocation may enter the native body. The other two dispatch variants use the same atomic stages with their own private owners. |
| Receipt commit | A matching sealed permit and receipt may construct the next checked mutation. CHILDLESS first wins `RECEIPT -> PERMIT`; either non-childless consumer first wins `RECEIPT -> CONSUMING_RECEIPT`. Every normal or exceptional exit returns, closes, or poisons the bound owner once; concurrent same-reference aliases and sequential replay cannot advance any holder state twice, and mismatch, altered nested value, or a changed revision cannot act. |

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
cleanup proceeds. Unknown evidence preserves the envelope byte-identically.

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
post-action observation is first persisted as releasing with attempt and closure
fields source-equal to the corresponding current attempt and closure fields,
plus one pending disposition. Release is then requested
with that pair. While reconciliation is `HELD` or `UNKNOWN`, execution remains
releasing; debt/current-attempt, automatic `ISSUED`, and the reservation remain
byte-identical, and neither termination nor launch may occur.

Only matching `RELEASED` may finalize the pending disposition:

- `CLOSURE_VETOED` releases the reservation while preserving debt byte-identically. Automatic
  origin records that cycle outcome and exhausts if and only if the issued
  attempt is three; manual origin leaves the cycle byte-identical.
- `SAME_OWNER_SURVIVED`, `MEMBER_SURVIVED`, or `EFFECT_UNPROVEN` keeps debt outstanding,
  clears its current-attempt pair, records that corresponding debt outcome, releases
  the reservation, and for automatic origin records the same cycle outcome and
  exhausts on attempt three. Manual origin leaves the cycle
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
If no permit exists, the envelope including debt/cycle/execution remains byte-identical
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
| `StateLossQuarantineV1` is `UNRESOLVED` | Preserve quarantine byte-identically indefinitely in V1; ordinary diagnostic observation may advance, but automatic retirement, every kill/launch, closure acquisition, attempt/debt mutation, identity commit, and manual override are unavailable. | `POLICY_HELD` | `CHILDLESS_STATE_PROVENANCE_LOST` and `CAPABILITY_UNAVAILABLE` continuously; any recoverable debt/cycle codes remain visible. |
| `ChildlessEffectEnvelopeV1` is structurally invalid, or #120/closure output is malformed, including a post-reservation structural-unavailability claim | Malformed wins before witness/permit construction. Retain the effect envelope byte-identically; ordinary observation-only fields may already have advanced. Construct no call or effect-owned mutation and do not retry/exhaust. | `POLICY_HELD` | Incomplete code, plus capability/debt/cycle codes by predicate; pending a human. |
| Well-formed owner/child/tree/debt proof is incomplete before reservation | No reservation and no attempt consumed. | `POLICY_HELD` | Incomplete code; plus debt/cycle codes by predicate. |
| `ClosureCapabilityV1` is `CAPABILITY_UNAVAILABLE` before reservation | Static capability refusal: create no reservation or continuation, consume no attempt, and make no external call. | `POLICY_HELD` | `CAPABILITY_UNAVAILABLE` continuously pending a human; plus debt/cycle codes by predicate. |
| A valid `ChildlessEffectEnvelopeV1` exists and next-permit construction returns `CAPABILITY_UNAVAILABLE` | Retain the complete effect envelope byte-identically. Ordinary observation-only fields may advance. Construct no takeover, reconcile/release, capture reservation, finalization, `Stop-Tree`, spawn, or identity commit. | `POLICY_HELD` | `CAPABILITY_UNAVAILABLE` continuously pending a human; plus debt/cycle/incomplete codes by predicate. |
| A fresh named proof's reservation-permit construction returns `CAPABILITY_UNAVAILABLE` | Create no effect envelope, reservation, continuation, attempt, cycle, debt, executable target, or external call. | `POLICY_HELD` | `CAPABILITY_UNAVAILABLE` continuously pending a human. |
| Permit construction returns `REJECTED` | Construct no mutation or call. A stale revision reloads/re-reduces; invalid or replayed private operands are rejected and cannot become a capability hold. | `NOT_ATTEMPTED` unless same-invocation re-reduction reaches another row. | Existing codes only; no `CAPABILITY_UNAVAILABLE` solely from rejection. |
| A permitted envelope has childless execution `IDLE`, no closure/pending disposition/current attempt, and ordinary residual evidence is `COMPLETE_GONE` | A permit-bound mutation clears debt and old-owner cycle, remains childless `IDLE`, and writes the terminal; construct no launch. | `NOT_ATTEMPTED` | No childless code after the atomic clear. |
| No in-flight childless phase and a global/policy gate holds or no named candidate exists; cycle is `NONE` | No module mutation or terminal write. | `NOT_ATTEMPTED` | Debt/incomplete code only when its predicate independently applies. |
| No in-flight childless phase and a global/policy gate holds or no named candidate exists; cycle is `ACTIVE` with a prior typed failure | Preserve the cycle byte-identically; no attempt, backoff, or terminal mutation. | `NOT_ATTEMPTED` | Active code; debt/incomplete code by predicate. |
| No in-flight childless phase and a global/policy gate holds or no otherwise-eligible automatic named proof exists; cycle is `EXHAUSTED` | Preserve the cycle byte-identically; no attempt, backoff, or terminal mutation. | `NOT_ATTEMPTED` | Exhausted code; debt/incomplete code by predicate. |
| Automatic named proof is otherwise eligible, no eligible manual origin wins, and its cycle was already `EXHAUSTED` before this poll | No reservation, attempt, backoff, or terminal mutation. | `AUTOMATIC_RETRY_EXHAUSTED` | Exhausted code; debt code if applicable. |
| A valid envelope plus permit has gate `MAY_TAKEOVER` | Apply the specified no-call takeover as a permit-bound mutation, then reload and mint a new operation-scoped permit. A CAS loser reloads without mutation. | No terminal module result yet. | Existing debt/cycle/incomplete codes by predicate. |
| A valid permitted envelope has any other retain-only safety gate | Retain the effect envelope byte-identically and perform no external call; ordinary observation-only fields may already have advanced. | `POLICY_HELD` | Incomplete code; plus debt/cycle codes by predicate. |
| A childless `PRE_BARRIER` reservation is vetoed or reloaded before closure acquisition | A permit-bound mutation releases that reservation, consumes no automatic attempt, preserves debt, cycle, and marker fields byte-identically, and writes the terminal. | `BARRIER_VETOED` | Debt/cycle/incomplete code by predicate. |
| Live acquisition returns a well-formed `HELD` and the full joined evidence is valid | Persist `TREE_CLOSURE_HELD`; no outcome or terminal is finalized. | No terminal module result yet. | Codes from preexisting debt/cycle only. |
| Any well-formed matching `HELD` returned by acquisition/reconciliation is not action-ready, or recaptured evidence/gates make a persisted `TREE_CLOSURE_HELD` non-action-ready | Persist a closure ID source-equal to the matching `HELD` result, persist `TREE_CLOSURE_RELEASING/CLOSURE_VETOED`, and request release; no outcome is finalized yet. The earlier live-valid row alone may continue toward teardown. | No terminal module result yet. | Incomplete code while release remains held; debt code if applicable. |
| `TEARDOWN_IN_FLIGHT` has `STOP_TREE/CALL_RETURNED` and the same checked continuation chain owns the effect guard | Reserve the next nonzero capture ordinal and arm the typed post-action observation. Its receipt mutation persists the mapped releasing disposition with `POST_ACTION_CAPTURE/CALL_RETURNED`; a later `CLOSURE_RELEASE/STATE_MUTATION` permit must replace that checkpoint with matching release. No outcome is finalized yet. | No terminal module result yet. | Debt code; incomplete also when the observation is incomplete. |
| `TREE_CLOSURE_RELEASING` reconciliation remains matching `HELD`, or any applicable acquisition/reconciliation is `UNKNOWN` | Preserve phase, reservation, pending disposition, debt/current attempt, and automatic `ISSUED` byte-identically; no terminal write. | `POLICY_HELD` | Incomplete code; debt code if applicable. |
| Acquisition returns no closure and matching reconciliation proves terminal `NEVER_ACQUIRED`; acquiring reconciliation proves matching `RELEASED` and binds its returned closure ID; held-phase reconciliation proves matching `RELEASED` after atomically recording pending `CLOSURE_VETOED`; or any persisted `TREE_CLOSURE_RELEASING/CLOSURE_VETOED` reaches matching `RELEASED` | Finalize `CLOSURE_VETOED`; retire the attempt in the same transaction; debt remains byte-identical. A later unexpected `HELD` for that retired attempt is release-only. Manual origin always leaves the cycle byte-identical; automatic issued-attempt count 1–2 becomes `ACTIVE/CLOSURE_VETOED`; count 3 becomes `EXHAUSTED/CLOSURE_VETOED`. | Manual origin: `BARRIER_VETOED`; automatic count 1–2: `BARRIER_VETOED`; automatic count 3: `AUTOMATIC_RETRY_EXHAUSTED`. | Cycle code by resulting predicate; debt code if applicable. |
| Reload/takeover retained `STOP_TREE/CALL_RETURNED`, and its matching `CLOSURE_RECONCILE` receipt proves matching `RELEASED` before any post-action capture | Consume that receipt only through `EFFECT_FINALIZE`; conservatively finalize `EFFECT_UNPROVEN`, retire the attempt, keep debt outstanding, clear its current-attempt pair, enter childless `IDLE`, and perform no residual-capture call or launch. Manual origin leaves the cycle byte-identical; automatic issued-attempt count 1–2 becomes `ACTIVE/EFFECT_UNPROVEN`, count 3 becomes `EXHAUSTED/EFFECT_UNPROVEN`. A later ordinary poll may clear debt only through the existing debt-only `EFFECT_FINALIZE` scope and matching `COMPLETE_GONE`. | Manual origin: `TEARDOWN_FAILED`; automatic count 1–2: `TEARDOWN_FAILED`; automatic count 3: `AUTOMATIC_RETRY_EXHAUSTED`. | Incomplete, debt, and cycle code by predicate. |
| Post-action `COMPLETE_RESIDUAL` contains the wrapper root and matching release is proved | Finalize `SAME_OWNER_SURVIVED`. Manual origin leaves any cycle byte-identical; automatic issued-attempt count 1–2 becomes `ACTIVE`, count 3 `EXHAUSTED`. | Manual origin: `TEARDOWN_FAILED`; automatic count 1–2: `TEARDOWN_FAILED`; automatic count 3: `AUTOMATIC_RETRY_EXHAUSTED`. | Debt plus cycle code by predicate. |
| Post-action `COMPLETE_RESIDUAL` omits the wrapper root and matching release is proved | Apply the preceding row with `MEMBER_SURVIVED`. | Same origin/attempt-sensitive result as the preceding row. | Debt plus cycle code by predicate. |
| Post-action `INCOMPLETE` and matching release is proved | Apply the preceding row with `EFFECT_UNPROVEN`. | Same origin/attempt-sensitive result as the preceding row. | Incomplete, debt, and cycle code by predicate. |
| Live post-action `COMPLETE_GONE`, matching release, and the same checked continuation ID remains live and effect-guard-owning through finalization | Clear debt/cycle and continue only to the core final barrier. | No terminal module result; the core result owns the outcome. | Codes recomputed from cleared state. |
| Reload or reconciliation finalizes `COMPLETE_GONE` without that same live checked continuation chain | Clear debt/cycle, enter `IDLE`, write the terminal, and do not launch. | `NOT_ATTEMPTED` | Codes recomputed from cleared state. |

A well-formed current-evidence change is the action-time veto row, not invalid
state. An ID/revision/owner/target/origin mismatch inside persisted checked
state is the invalid-state row, not a barrier veto. The automatic-attempt-three
row takes precedence over the generic veto/failure result only after matching
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
invocation either reaches a later final row or returns with checked state
byte-identical to its invocation-entry state; the next poll then matches the applicable retained
`POLICY_HELD`/`NOT_ATTEMPTED` row. Every emitted module result is one closed
`ChildlessActionResultV1` value.

Normal execution is:

| Transition | Checked effect |
| --- | --- |
| Reserve named proof | With no same-poll terminal/current obligation, consume a fresh witness plus inert authority and one `RESERVE` permit. `INITIAL` creates the first envelope/binding. `CONTINUE` transforms a qualifying childless-`IDLE` envelope: initial-mode retry may rebind while preserving cycle/terminal historical tombstones byte-identically; debt completion keeps the immutable binding source-equal to its predecessor and an order-preserving residual subset composed only of members source-equal to corresponding authorized members. Both install `PRE_BARRIER` in one checked mutation. |
| Begin closure acquisition | Under the effect guard, a `STATE_MUTATION` permit live-recomputes the binding and persists acquiring plus the specified `ACTIVE_ATTEMPT/CLOSURE_ACQUIRE/ARMED` continuation. At the successor revision, a distinct fresh `EXTERNAL_CALL` permit constructs/invokes acquisition. Automatic origin creates/increments `ACTIVE/ISSUED`; manual origin preserves the cycle byte-identically. |
| Acquire valid closure | The typed receipt plus a distinct fresh receipt-derived `RECEIPT_MUTATION` permit whose call-issuance binding is source-equal to that receipt, and full joined equality, persist `TREE_CLOSURE_HELD`, closure ID, and `CALL_RETURNED`. No raw provider result or consumed call permit may do so. |
| Veto after `HELD` | A `STATE_MUTATION` permit arms matching release; after that CAS a fresh `EXTERNAL_CALL` permit constructs/invokes it, and only a fresh receipt-derived permit whose call-issuance binding is source-equal to that receipt may finalize `RELEASED`. Failure at any construction point preserves the envelope byte-identically. |
| Arm teardown | A `STATE_MUTATION` permit atomically enters `TEARDOWN_IN_FLIGHT`, creates/updates debt, and writes `STOP_TREE/ARMED`; after its lineage custody returns, a fresh successor-revision `EXTERNAL_CALL` permit constructs the sealed target set and childless call, which #146's private CHILDLESS-variant constructor wraps for the closed dispatcher. Only a fresh matching receipt-derived permit over the receipt-held lineage with a call-issuance binding source-equal to that receipt writes `CALL_RETURNED`. |
| Observe action effect | A `POST_ACTION_CAPTURE/STATE_MUTATION` permit at `STOP_TREE/CALL_RETURNED` allocates the next nonzero ordinal and arms the typed capture; a fresh post-CAS call permit obtains its receipt, and a fresh receipt-derived mutation permit whose call-issuance binding is source-equal to that receipt maps the observation into releasing state with `POST_ACTION_CAPTURE/CALL_RETURNED`, without clearing debt/current attempt or cycle `ISSUED`. A later `CLOSURE_RELEASE/STATE_MUTATION` permit alone may arm release. |
| Finalize release | A matching release receipt and finalize permit apply the disposition, bind the retired-attempt tombstone, and only then clear the continuation/release the guard. |
| Spawn and identity commit | Each `PRE_BARRIER`, `SPAWN_IN_FLIGHT`, and `AMBIGUOUS_LAUNCH` envelope carries a binding source-equal to its predecessor. A state-mutation permit first installs `SPAWN_RESERVATION/SPAWN/ARMED`; only a distinct fresh post-CAS call permit invokes spawn, and only its receipt plus a fresh receipt-derived mutation permit whose call-issuance binding is source-equal to that receipt commits identity/ambiguity. The sole receipt-free conversion is the closed crash-only `SPAWN_RESULT_COMMIT` scope over the persisted issuer subject and positive issuer-death proof. |
| New guarded owner commits | Permitted only with quarantine `NONE`, debt `NONE`, and no childless effect envelope requiring cleanup. A quarantined state cannot commit a replacement owner automatically in V1. |

Crash/reload is equally closed. Deserialization yields only the inert envelope;
the full-poll precedence above handles state loss, observation, typed
capture-sequence exhaustion, quarantine, and malformed state without
constructing an effect object. A valid envelope can
advance only after the current caller obtains a fresh witness, passes static
executor preflight, acquires the effect guard when the operation requires it,
and constructs the bound operation permit. `CAPABILITY_UNAVAILABLE` at preflight
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
  without termination. `UNKNOWN` preserves state and the retired set
  byte-identically.
- `TREE_CLOSURE_HELD`: do not terminate. Replace the takeover checkpoint only
  with `CLOSURE_RECONCILE/ARMED`. A matching `RELEASED` may finalize the veto;
  matching `HELD` requires a later, distinct `CLOSURE_RELEASE/STATE_MUTATION`
  arm and matching idempotent release; `UNKNOWN` preserves the held state
  byte-identically.
- `TREE_CLOSURE_RELEASING`: replace the takeover checkpoint only with
  `CLOSURE_RECONCILE/ARMED`. Finalize the already persisted disposition on
  matching `RELEASED`; matching `HELD` may proceed only through a later,
  distinct `CLOSURE_RELEASE/STATE_MUTATION` arm, and `UNKNOWN` preserves state
  byte-identically.
- `TEARDOWN_IN_FLIGHT` with `STOP_TREE/ARMED`: never reissue `Stop-Tree`, never
  infer that it did or did not run, and never infer action completion. After
  predecessor-death proof, persist `EFFECT_UNPROVEN`, keep debt outstanding, and release
  any matching closure only after the takeover checkpoint is replaced by a distinct
  `CLOSURE_RELEASE/STATE_MUTATION` arm. No branch launches.
- `TEARDOWN_IN_FLIGHT` with `STOP_TREE/CALL_RETURNED`: takeover writes the inert
  `STOP_TREE/TAKEOVER_CHECKPOINT/RECONCILER` owner with a returned-effect fact
  source-equal to its predecessor. After a distinct `CLOSURE_RECONCILE/STATE_MUTATION` arm and
  matching reconciliation, it may obtain a fresh
  post-action observation under matching `HELD`, then atomically persist the
  mapped releasing disposition with `POST_ACTION_CAPTURE/CALL_RETURNED`. A
  later `CLOSURE_RELEASE/STATE_MUTATION` permit alone may replace it with
  `CLOSURE_RELEASE/ARMED`. If reconciliation instead proves matching
  `RELEASED`, consume that matching reconcile receipt through a fresh
  `EFFECT_FINALIZE` permit and conservatively finalize `EFFECT_UNPROVEN`: enter
  childless `IDLE`, retire the attempt, keep debt outstanding, clear its current-attempt
  pair, record the origin-sensitive failure/exhaustion result, and make no
  residual-capture call or launch. The next ordinary poll may clear that debt
  only through the existing debt-only `EFFECT_FINALIZE` scope and a matching
  `OwnedDebtResidualObservationV1.COMPLETE_GONE`. `UNKNOWN` preserves
  reservation, debt/current attempt, and cycle `ISSUED` byte-identically;
  `NEVER_ACQUIRED` is invalid after debt and preserves the fence byte-identically.

Every reload post-action capture atomically reserves the current
core `next_capture_ordinal` under the same checked continuation, then
increments it. Its `CaptureIdV1` has the current state epoch, agent, ordinary
poll sequence, and that allocated nonzero ordinal; it is greater than any prior
capture for the attempt. The ordinary-observation `capture_ordinal == 0`
well-formedness rule does not apply to this explicitly nonordinary capture.
Exhaustion of the nonzero ordinal range is `INCOMPLETE` and preserves the fence
byte-identically.

Task #115 must make every displayed state change one checked compare-and-swap
transaction; no executor branch may save a cached whole state.

## Mandatory conformance evidence

1. Build two adjacent distinct complete child-absence captures for one
   `(pid, OwnedExactStartGuardV1, launch_nonce)` owner identity and construct the
   named authority. Replay, gap, one
   capture, owner/state/generation/PID/start/nonce change, `UNKNOWN`, unreadable
   child evidence, or incomplete tree must refuse. Feed third and fourth
   adjacent compatible captures and prove the saturated two-sample window
   slides each poll and keeps current authority deterministic. Pair a cached
   prior `capture_id` with a rewritten current separate sequence, change
   epoch/agent/ordinal independently, and corrupt the stored last-sequence
   binding; every case must refuse or hold as specified.
2. Permute process rows and add name/command-line false positives. Only validated
   `OwnedExactStartGuardV1`-guarded ownership and complete coverage may affect
   the result. Recycled PID,
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
   immutable-binding debt completion. Rebind must keep terminal tombstones byte-identical
   to their pre-rebind values
   under their historical bindings and refuse any `RELEASE_PENDING` tombstone.
4. Revalidate after reservation. Owner, coverage, and target digest must match;
   initial mode additionally requires a fresh post-linearization nonordinary
   raw capture, same-capture complete child absence, current matcher config
   matching the reservation, and live reconstruction of both runtime and
   module basis digests.
    Change turn/phase/progress/config after reservation and substitute a stale
    pre-closure capture; each must veto before debt or `Stop-Tree`. Debt mode
    requires reservation debt ID/generation source-equal to the current debt values. Reserve at both banked and
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
   attempt. Partial kill preserves debt byte-identically and blocks every launch. A later ordinary
   `COMPLETE_RESIDUAL` observation—not a preexisting closure—constructs the
    source-bound residual authority. Kill the wrapper first and prove an authorized orphan
    tool remains reachable under a new post-reservation closure. In a
    wrapper-to-shell-to-tool chain, remove both ancestors and prove the live
    tool carries a complete parent/depth target object source-equal to its
    original object in an
    order-preserving residual subset composed only of members source-equal to
    corresponding authorized members; recomputing it to depth one must refuse.
7. Cross automatic attempts 0/1/2/3, closure veto before `HELD`, survivor,
   incomplete effect, crash reconciliation, manual retry after exhaustion,
   complete gone, new owner, and no fourth automatic reservation/backoff.
    Attempts one/two retry only on a later eligible ordinary poll and do not
    mutate generic backoff. Cross `ACTIVE`/`EXHAUSTED` with no current candidate
    and every global gate; require `NOT_ATTEMPTED` with matching continuous
    attention. Manual veto/failure always uses manual result precedence and
    leaves either cycle byte-identical. Attention remains true on every
    incomplete, debt, active-failure, and exhaustion poll.
    For `CAPABILITY_UNAVAILABLE`, join the action resolution to the matching
    canonical-condition fingerprint and require every 87-B rendering to name the
    canonical-condition agent key and say
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
    `UNKNOWN(CAPABILITY_UNAVAILABLE)`; both must retain their fences byte-identically,
    avoid `CLOSURE_VETOED`/retry/exhaustion, and require task visibility.
    Independently remove the current-host witness with a persisted reservation/
    phase, debt-only state, and retired-attempt cleanup. Static inability must
    emit the corresponding capability attention, preserve the complete
    childless effect envelope byte-identically, and construct no permit-bound
    mutation or call. Then
    supply a fresh but mismatching witness for each shape: construction must be
    `REJECTED`, preserve the envelope byte-identically, make no mutation/call, reload or reject,
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
    `DRY_RUN` discards, and every other gate preserves debt/cycle byte-identically
    and retains attention. The
   clear must also require `IDLE` with no reservation, closure, pending
   disposition, or current attempt. Inject the same observation into every
   named persisted phase and prove its specified phase-specific reconciliation
   wins and no fence/debt is discarded. Replay a stale
   `OwnedDebtResidualObservationV1.COMPLETE_GONE` with a rewritten current
    separate sequence and prove `CAPTURE_ID_MISMATCH` preserves debt byte-identically. The next
   poll may derive fresh authority.
9. Cross manual/automatic overlap. Exactly one manual-origin action may wrap
   the same module proof; marker authority cannot replace owner/child/tree/
   closure proof.
10. Recompute the banked matrix counts and require them to match the banked values; require both fingerprint-
    vector serializations to remain byte-identical after integration.
11. Make the banked child-death counter reach two while the module overlay is
    only one by using an incomplete tree on the first poll. Both automatic and
    manual generic teardown must be suppressed; only `HOLD` is conforming.
12. Failure-inject after `HELD`, after post-action observation, before release,
    and after crash in every closure phase. Null/mismatched release IDs and
    `HELD`/`UNKNOWN` reconciliation preserves the reservation, debt/current
    attempt, pending disposition, and automatic `ISSUED` byte-identically. Only matching
    `RELEASED` finalizes; the first acquiring reconciliation crosses
    `NEVER_ACQUIRED`, `HELD`, `RELEASED`, and `UNKNOWN`, with matching
    `RELEASED` binding its closure ID and finalizing the veto. Reload
    complete-gone cleanup emits `NOT_ATTEMPTED`, writes the terminal, and never
    launches. Cross every persisted named phase with every execution gate:
    dry run and a non-current supervisor make no closure-successor call or
    effect-owned mutation. A missing/mismatching fresh witness preserves the
    complete childless effect envelope byte-identically while ordinary observation may
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
    At active age 30 seconds the guard must remain open. With a longer
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
    `EFFECT_FINALIZE` to record `EFFECT_UNPROVEN`, keep debt outstanding, enter `IDLE`, and
    make no residual-capture call or launch. With a fresh matching
    `PRE_BARRIER_RELEASE` permit, reload `PRE_BARRIER` through its state-only
    release without an attempt owner; without that permit retain the envelope
    byte-identically. For every takeover phase,
    assert the specified no-call mapping and then only the closed table's specified next
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
    typed `DEBT_COMPLETION`; with a present childless wrapper, both must require
    the named `INITIAL` authority before any kill.
20. Construct each of the seven typed payload objects through its typed
    constructor using the scalar fixture values shown in the displayed JSON.
    Parsing may extract those scalar values, but the expected typed object and
    its field set must come from the independent typed constructor, not from
    the parsed JSON object.
    Encode each object through the implementation's production
    `CanonicalJsonV1`; require bytes byte-identical to the display and a matching
    byte count,
    then compute `SHA-256(domain || NUL || produced_bytes)` and require the
    displayed digest. Each downstream object embeds the upstream digest just
    recomputed by the implementation, never the displayed upstream digest.
    As a secondary change-detection control, change one upstream byte at each
    stage and prove every dependent identifier changes; that mutation control
    is not independent codec or field-set correctness evidence.
21. Race two reload reconcilers reserving nonordinary capture ordinals.
    Require distinct CAS-allocated values greater than zero at the current
    ordinary sequence, an attempt binding source-equal to the current attempt's
    binding, and no reuse/wrap. Pair an
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
23. Adapt merged task #120 at `587e7c1` without semantic drift. Accept only
    `owned_process_tree_v2`, schema 2, status `complete|absent|truncated|invalid`,
    literal `limit=64`, consistent counts, generation/nonce, and the stated field
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
    Admit a non-live ancestry bridge only as a source-equal copy of one prior
    complete record; separately require its generation, nonce, and parent chain
    to remain source-equal to that record. Exclude it from target tuples/digests and
    `Stop-Tree`, and normalize a live child behind it to the stated orphan
    form. Validate but exclude role/discovery metadata from authority. Prove an
    openable planned Windows target matching its `OwnedExactStartGuardV1`
    reaches #120's unchanged
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
    clear debt. Recycle the parent PID and prove a same-or-newer exact-FILETIME child is
    excluded from the retired-parent ownership edge, while a pre-recycle or
    incomparable child stays conservative survivor evidence. Independently
    classify the replacement-side child as this agent's wrapper/wait and prove
    that ordinary barrier evidence still blocks. A clear barrier without the
    typed closure/absence proof must also refuse debt clear. Attended reset/archive
    evidence must never substitute for automatic closure or completion.
    At every post-construction non-childless owner stage—`CALL`, `DISPATCHING`, `PLAN_OWNED`,
    `INVOKING`, `RECEIPT`, and `CONSUMING_RECEIPT`—terminate the supervising
    process or abandon its worker before receipt/poison publication, then reload
    the persisted checkpoint. For EPHEMERAL_TERMINAL, a new checked #115 transition
    may retry only the module-typed #120 target tuple: require gone PID to perform no kill,
    recycled PID/different FILETIME to be refused, and the same live PID/same
    FILETIME to remain the intended same-handle kill target; only the fresh
    teardown barrier may permit archive. For configured relaunch, require the
    atomically persisted `ConfiguredActionIssuerCheckpointV1` at every one of
    those stages and, after issuer loss, require typed
    `ConfiguredPreBarrierOwnerLossHoldV1` with the reservation and checkpoint
    byte-identical. Prove reload cannot remint custody, apply generic
    `PRE_BARRIER_RELEASE`, or release and reserve again. Separately exercise the
    live same-invocation veto while custody is `READY`: exactly one atomic
    custody close plus private release may commit, with zero call/effect, and it
    must be unavailable after owner loss.

    Derive the hold whenever source-bound custody cannot be validated. With the issuer
    still live or death observation unavailable, require
    `issuer_extinction=LIVE_OR_UNPROVEN`, render the same remedy, and reject the
    attended commit. Only a fresh independent OS result whose queried PID is
    source-equal to the checkpoint issuer PID and whose result is absent refreshes
    it to `PROVED_GONE(result=GONE)`. Hold that PID
    present with the same generic start token and again with a different generic
    start token; both cases must remain `LIVE_OR_UNPROVEN`, render
    `CAPABILITY_UNAVAILABLE(ExactIssuerIdentityAdapterV1)`, and reject
    disposition. Literal token comparison and a tolerant fallback must both be
    incapable of classifying `RECYCLED`. Make the observation unavailable and
    require the same blocked posture. In every case, the byte-identical persisted
    checkpoint produces a matching source hash. Recompute that hash from the core's
    canonical domain-separated persisted-field projection; inclusion of the volatile
    extinction observation or attended-action value, fixed-point, hash-including,
    alternate-domain, or partial projections must fail.

    Exercise `AttendedConfiguredPreBarrierDispositionRequestV1` against only the
    official store under the lifecycle/configuration/#115 locks after the
    operator creates or retains `supervisor.kill` and stops every project
    supervisor, with the singleton absent before and at commit.
    Require an authorized liaison/sole lead, current source hash and complete
    checkpoint/state equality, and an independent fresh OS result that the
    checkpoint issuer PID is definitively absent. A caller acknowledgement,
    checkpoint, absent target, or dead issuer's own record is not that proof.
    Keep the #120 identity-bound target live in one case and prove the attended operation still
    performs zero kill and zero launch. Require the operator acknowledgement to
    mean only prior native effect unknown; it must not prove kill, no-kill,
    target extinction, or launch safety. Map the prospective CLI's three
    flags to `ACKNOWLEDGE_NO_LIVE_ISSUER`,
    `ACKNOWLEDGE_PRIOR_STOP_TREE_EFFECT_UNKNOWN`, and
    `ACKNOWLEDGE_FRESH_OBSERVATION_REQUIRED` in displayed order; omit or alter
    any one and require zero mutation.

    Race two byte-identical attended requests and require one checked disposition. Compare
    the successor: top-level `IDLE`, same-poll terminal written, selection-derived
    confirmations reset, issuer checkpoint removed, and matching
    `ConfiguredPriorEffectUnknownFenceV1` installed; managed identity,
    continuity, establishment guard, launching/readiness, backoff,
    manual/quarantine state, and #120 evidence remain byte-identical, with no
    childless debt/cycle/continuation/retired attempt created or cleared. Replay
    the winning request while that fence remains current and recover its complete
    persisted request/actor/acknowledgement/reason/result binding without a
    second mutation; after fence clearance the same request is stale. Reject a
    stale hash, wrong actor/store/agent/epoch/revision/
    reservation/checkpoint/barrier/target, removed kill switch, present or stale
    singleton, live/unproven issuer, wrong phase, and malformed attestation with
    the closed typed rejection and zero mutation/effect. Exercise combined
    failures and require the core's closed precedence and one legal
    reason/resolution/next-step triple. When the actor is
    authorized and a current matching hold exists, return that complete hold and
    remedy; when none exists return `NO_CURRENT_MATCHING_HOLD`; for an
    unauthorized actor return `REDACTED_UNAUTHORIZED` without any current hold,
    source hash, checkpoint target, or issuer identity. Require the stale-
    singleton result to name its existing attended repair prerequisite and the
    kill-switch-absent result to say create the switch, stop all supervisors,
    then refresh. Verify the hold rendering names the create-or-preserve-kill-
    switch/stop-supervisors/prospective-command remedy and labels it not yet
    delivered. Absent reviewed
    `ConfiguredPreBarrierRetrySuccessorV1`, prove this attended transition is the
    only exit; with that successor absent no automatic remint or silent release-
    and-reserve exists.
    Independently set a non-null prior-effect fence, change the current
    `RESERVED/PRE_BARRIER` predecessor, and stale the expected hold source hash;
    each must reject with zero mutation, kill, or launch under every issuer
    observation outcome.

    While the fence is non-null, attempt configured no-kill, configured kill,
    childless reserve, every childless effect operation, archive, `SPAWN_ARM`,
    and both launch variants; each must remain `POLICY_HELD` with zero
    reservation, permit, custody, call, mutation, archive, or launch. This makes
    a second owner-loss disposition unconstructible instead of replacing the
    first fence. Keep `supervisor.kill` present after disposition and prove
    clearance is ineligible. Then remove it, start exactly one current
    supervisor, and prove ordinary observation proceeds while the fence still
    blocks every action. Disable the action latch, remove report membership, and
    disable auto-restart separately and together: the no-effect narrow clearance
    remains eligible, while the full gate still blocks every later action. Dry
    run, non-clear kill switch, or noncurrent supervisor must return its closed
    typed gate-held result after the sole `RECEIPT -> COMMITTING` winner, close custody,
    and require a fresh committed capture. Missing/corrupt/quarantined state
    must expose no current fence, close custody, and require attended repair plus
    a fresh capture. Race two lineages begun from the same predecessor and
    prospective `CaptureIdV1`; only the winning checked commit may publish
    `CommittedOrdinaryFenceCaptureV1`, and the stale loser cannot mint or
    substitute a barrier receipt. Race aliases of that witness and require one
    `READY -> ADAPTING` winner. #115's unexported reducer must use the witness's
    complete current source targets and sealed capture above the epoch-aware
    floor. `BLOCKED`, `AMBIGUOUS`, `UNAVAILABLE`, stale old-epoch, and wrong-source
    receipts preserve the fence byte-identically and return the core's corresponding closed
    result/remedy/custody disposition; alias/replay losers read no state and
    claim neither preservation nor clearance. Every admitted deterministic hold closes
    custody and requires a fresh capture; same-custody losers read no store.
    Exactly one matching `CLEAR`
    receipt-custody alias may win `RECEIPT -> COMMITTING` and commit
    `PRIOR_EFFECT_FENCE_CLEAR`; it removes only the fence and performs no kill or
    launch. Advance the checked revision after receipt creation and require
    stale-revision rejection with the fence intact. Inject proved pre-state-CAS
    failure, post-CAS/pre-close failure, and post-close/pre-response loss. Require
    `FAILED_PROVED_NO_COMMIT` only for proved no-write; otherwise require
    `CLEAR_COMMIT_OUTCOME_UNKNOWN` with the corresponding custody disposition and a
    checked reread yielding only `FENCE_STILL_CURRENT`, `FENCE_CLEARED`, or
    `STATE_OR_FENCE_UNTRUSTWORTHY`. After disposition at maximum
    sequence, roll over and prove the fence's audit/source/effect bytes remain
    byte-identical while only its floor becomes `(new epoch, 0)`; old-epoch clearance
    fails, the first winning committed ordinary capture `(new epoch, 1, 0)` may
    yield clearance custody only when #120 coverage is clear, and a repeated rollover rebases again without
    erasure. Without task #57 require activation refusal before
    `Start-Process`; with #57 delivered, its separate review must prove a crash
    or abandoned worker cannot create two live wrappers for one agent. No test
    may credit #120's kill identity with launch-singleton safety.
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
    their existing authorization, persistence/barrier ordering, exact-target identity
    behavior, and dry-run semantics. For the ephemeral variant, prove its narrow
    final action gate separately: after constructing a legitimate call, first
    change/disable the action-latch epoch and then, in an independent case,
    activate the kill switch before dispatch. Each case must permit exactly one
    winning admission, then consume the call as `REJECTED_NO_EFFECT`, preserve
    persisted `next_entry` byte-identically, and construct no native plan, lexical raw array, or
    effect; replay and any second admission stay rejected.
    With both gates unchanged, dispatch retains the latch read guard through
    issuance and the private native body repeats the kill-switch check. Add a
    third race: pass the outer checks, pause after native-plan construction but
    before that inner check, and activate the kill switch. Require the matching
    invocation-bound `ACTIONS_DISABLED_NO_EFFECT` result, one transition to
    `REJECTED_NO_EFFECT(FINAL_ACTION_GATE_CHANGED)`, byte-identical `next_entry`, no
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

    Exercise typed preflight-reason direction. In a staged Windows path with
    valid exact-FILETIME semantics but no #146 dispatcher seal, require
    `CurrentExactTargetExecutorWitnessConstructionV1` and fresh
    `ClosureCapabilityV1` to return the singleton
    `DISPATCHER_SEAL_UNDELIVERED` before permit/provider evaluation. Repeat with
    a retained Windows envelope: require the same internal preflight
    reason, generic `CAPABILITY_UNAVAILABLE` attention, byte-identical envelope retention
    except the separately allowed #115 observation projection, and no permit,
    mutation, call, or effect. With executor and seal both absent on Linux or
    macOS, require `EXACT_TARGET_EXECUTOR_UNAVAILABLE` to take precedence. With
    a staged conforming Windows seal present but the closure successor absent,
    require witness `AVAILABLE` followed by the singleton
    `SUCCESSOR_MISSING`, never `DISPATCHER_SEAL_UNDELIVERED`. These are proposed
    conformance results, not behaviors present at merged `587e7c1`.

    For every `ExactTargetExecutorOperationV1` and permit use, prove the
    private permit constructor requires a fresh witness, bound binding, current
    revision, and the operation's closed live scope. Normally it also requires
    the recomputed authorized tuple/residual subset; the closed targetless old-
    side rebind, retired-cleanup, and owner-transition scopes instead require
    a binding source-equal to the selected tombstone/envelope binding plus the complete fresh prospective
    proof or typed subject/checkpoint specified above.
    Independently mismatch each applicable operand, reuse the one-shot permit, and
    change the revision before consuming the permit. No case may yield an
    executable target, permit-bound mutation, or typed call. Separately invoke
    a valid typed call, commit an observation-only revision while it runs, and
    require a fresh receipt permit to accept the result only when envelope and
    continuation remain byte-identical; any effect-state change rejects it. For every call-
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

    For CONFIGURED_AGENT_RELAUNCH and EPHEMERAL_TERMINAL, race two applications
    of the same checked #115 action transition and require exactly one committed
    transition, one `READY` action owner, and one custody handle. Then race and
    sequentially replay two private call constructors over that same handle:
    exactly one `READY -> CALL` transition may emit one call/submission; every
    loser produces zero plan, effect, receipt, mutation, planner behavior, or
    launch. Repeat after serializing/reloading the barrier_state byte-identically or
    next_entry and attempt to mint/lookup custody by IDs, digests, and copied
    provenance; every attempt must fail. For EPHEMERAL_TERMINAL, a fresh post-
    crash action can arise only from a new checked #115 transition, never from
    decode. For CONFIGURED_AGENT_RELAUNCH, the same persisted PRE_BARRIER action
    cannot receive a fresh #115 transition at all: it remains in the matching owner-lost
    HOLD until the attended disposition clears it, or until the separately
    delivered `ConfiguredPreBarrierRetrySuccessorV1` wins its named checked
    transition. CHILDLESS keeps its
    existing one-outstanding-loan guard lineage and must reject a second
    non-childless-style issuer. Verify the per-variant KEEP/SKIP matrix and fail
    conformance if a new variant omits any family property without a reason.

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

    Inject result-handoff failure after the atomic admission and plan/invocation-
    handle construction or handoff failure after `PLAN_OWNED`. For each variant,
    install a legitimate invocation and deterministically throw (a) during
    final-gate evaluation and (b) on private-body entry, after the `INVOKING` CAS
    but before the native-effect frontier. Require the matching invocation-bound
    `NATIVE_ENTRY_FAILED_NO_EFFECT`; exactly one result alias may move CHILDLESS
    to one successor `AVAILABLE` custody or a non-childless owner to
    `REJECTED_NO_EFFECT(NATIVE_ENTRY_FAILED_NO_EFFECT)`. Sequential result replay
    is inert. Throw at the frontier, immediately after it, and from a hook whose
    locus is unknown; require the specified childless `ADAPTER_EFFECT_UNCERTAIN` or
    non-childless `NATIVE_EFFECT_UNCERTAIN` poison. Separately fail positive-
    result construction/handoff/owner resolution while pre-frontier no-effect is
    known; require childless `CUSTODY_PROTOCOL_BROKEN` or non-childless
    `DISPATCH_PROTOCOL_BROKEN`. Keep `ACTIONS_DISABLED_NO_EFFECT` as the separate
    false-gate oracle. Continue failure injection after native return, during
    receipt handoff, receipt consumption, and owner/planner commit. Every path
    resolves exactly once, never both and never neither; exception class,
    elapsed time, missing result/receipt, caller flags, and a null raw-array
    observation are not no-effect proof. Repeat cleanup and require idempotent
    rejection. A poisoned lineage cannot issue again until guard
    release/reacquisition and cannot reissue an uncertain external effect.

    Reproduce the reviewer's mutation probe: construct a legitimate sealed call
    whose nested target PID is 101, use the controlled tamper hook to rewrite it
    to 202, and require consumer rejection before native effect. Ordinary public
    mutation must fail; mutating the caller's original source alias after
    construction must leave every sealed field of the constructed call unchanged.
    Repeat the transitive tamper
    control for the target set, permit proof/residual, next envelope/outer delta,
    call arguments, receipt result, configured-agent provenance, and ephemeral
    provenance. A frozen outer record, digest-only check, or public reseal API is
    insufficient. Inspect each sealed graph and prove neither the atomic lineage
    owner nor a non-childless dispatch-use owner cell is reachable; only the
    immutable custody/use proof is present. Prove the opaque submission,
    admission, invocation, and receipt-custody handles are the only private
    associations between their byte-identical immutable values and that owner, and that each holder transition
    changes the separate cell exactly once. A caller
    mutex, external call-ID registry, or unstated dispatcher lock must fail the
    construction-direction control.

    Round-trip every serializable state shape and prove witness, permit,
    executable target, mutation, call, receipt, and live effect guard are absent
    after decoding. Prove only the actual adapter can return a receipt and only
    for its matching typed call. On Linux and macOS, exercise at least a fresh
    proof, a deserialized closure/debt envelope, childless `SPAWN_IN_FLIGHT`,
    `AMBIGUOUS_LAUNCH`, and a retired-attempt tombstone as direction controls;
    the effect envelope must remain byte-identical and no adapter may be called, while
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
    the source-bound prospective ordinal-zero `CaptureIdV1` before the observer's first
    acquisition step. Prove `ProcessObservationV1.capture_id`,
    `receipt.prospective_capture_id`,
    `PrivateClassifierObservationMutationV1.capture_id`, and every non-null
    successor capture-ID field that records the current sample to be source-equal
    to that begin-bound ID. Empty/reset successors retain null IDs; no
    successor may synthesize a different ID. Starting from predecessor sequence
    `n`, require the receipt, raw/tree displayed sequence, and candidate mutation
    to use checked `n + 1`; after the winning commit require current sequence
    `n + 1`, never `n` during candidate validation. At maximum `uint64`, require
    the typed result `ATTENDED_REQUIRED(CaptureSequenceExhaustionV1)`, including the
    agent/epoch/revision, fixed maximum sequence, `READY` or the complete ordered
    blocker tuple whose fields are source-equal to the corresponding checked
    blocker fields, and attended action; require no acquisition-handle
    construction, observation, receipt, mutation, or 87-A action, and no wrap to
    zero. From top-level `IDLE`, race one attended rollover request and a second
    whose fields are source-equal to the first:
    exactly one checked replacement may install a fresh epoch at revision and
    sequence zero. Require the reset capture-derived fields to match
    their specified reset values and the preserved managed/manual/quarantine
    fields to remain byte-identical to their predecessor values. With a non-null
    configured prior-effect fence, preserve its complete audit/source/effect
    fields byte-identically and rebase only its
    freshness floor to `(new epoch, 0)`; null remains null. Require the first new ordinary ID to
    use new sequence one and reject every old-epoch lineage, receipt, custody,
    capture, confirmation, and proof. Retain an in-flight old-epoch observation
    handle across rollover and prove its later commit rejects or poisons with
    zero mutation. Race the same rollover against an old-epoch
    EPHEMERAL_TERMINAL owner at `READY`, `CALL`, `DISPATCHING`, `PLAN_OWNED`,
    `INVOKING`, `RECEIPT`, and `CONSUMING_RECEIPT`. Rollover visible at the final
    provenance read before plan ownership requires the specified typed positive-no-effect
    `VARIANT_PROVENANCE_STALE`; after that read—including post-read
    `DISPATCHING` before plan ownership—invocation may resolve normally and must
    not infer no effect from epoch mismatch. At receipt, exercise both
    checked-state CAS orders. Rollover first yields exactly one
    `EphemeralTerminalReceiptApplyResultV1.STALE_CLASSIFIER_EPOCH_RETRY_REQUIRED`
    and `CONSUMING_RECEIPT -> CLOSED`, preserves `next_entry` byte-identically, and performs zero
    new-epoch mutation, archive, launch, or new native effect. Receipt commit
    first yields `APPLIED/CLOSED`, makes the original rollover request stale, and
    requires a fresh exhaustion result/request. Unknown commit poisons with
    `PLANNER_COMMIT_UNCERTAIN`. Accept the stale result for any trustworthy
    schema-valid same-agent official current state with a different epoch;
    reject missing, untrusted, corrupt, or wrong-agent state. Aliases and result
    replay are inert, and no branch claims the original native effect absent.
    Only a fresh checked action transition against trustworthy current state may
    retry and reach the teardown barrier.
    Independently test persisted non-childless execution and every
    childless envelope/debt/cycle/continuation/retired-attempt blocker, alone and
    combined; state must remain byte-identical with no new epoch, quarantine,
    or reset attempt budget. Require 87-B to project complete blockers
    source-equal to checked state and attended
    handling rather than a bare enum. After the race,
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
| POSIX target-identity execution for named teardown | CURRENTLY UNAVAILABLE | #120 accepts Linux process-start identity tokens but declares no macOS token and has no non-Windows executor branch. Fresh proofs cannot construct a reservation permit; deserialized reservations, phases, debt, spawn ambiguity, and retired tombstones are inert because they cannot construct a fresh matching permit, typed call, or effect-owned mutation. The effect envelope remains identity-bound while ordinary observation may advance its separate projection. Every unresolved named case remains `POLICY_HELD` pending a human. |
| Action-scoped creation closure | CURRENTLY UNAVAILABLE; NORMATIVE CONTRACT SPECIFIED pending a conforming closure successor | Merged #120 does not freeze creation or expose attempt-keyed acquire/reconcile/release; otherwise `CAPABILITY_UNAVAILABLE` and `POLICY_HELD` pending a human |
| Atomic reservation/debt/cycle/terminal state | NORMATIVE CONTRACT SPECIFIED; implementation blocked on #115 | Task #115 checked state owner |
| External-call continuation/effect linearization | PARTIAL Windows target-local primitive DELIVERED by #120; full normative contract specified; implementation blocked on #115, #146, and the closure successor | #120 same-handle exact FILETIME check/terminate plus conditional bounded wait attempt, #115 action-scoped non-childless custody and checked continuation state, #146 closed dispatch, unique guard lineage, and successor-owned attempt-bound synchronous adapters |
| Fail-closed state-loss quarantine | NORMATIVE CONTRACT SPECIFIED; implementation blocked on #115 | Task #115 checked state owner |
| Automatic state-loss-quarantine retirement | UNAVAILABLE IN V1 ON EVERY PLATFORM | No trustworthy host/process-universe token exists in merged #120; quarantine stays `STATE_PROVENANCE_LOST`/`POLICY_HELD` pending attended handling. A future read-only existing-OS-token successor must satisfy M5 Option A. |
| Same-platform state-file/workspace transfer, restore, rollback, and migration activation | UNAVAILABLE IN V1; DECLARED ACTIVATION REFUSES | A conforming activation path refuses before active-store admission with zero 87-A witness, mutation, effect, or launch and directs attended handling. An out-of-band replacement may be undetectable, is nonconforming, and has no 87-A guarantee. Future 87-C must bind the source universe within M5 Option A or keep imported state inert. |
| Configured issuer PID reuse classification | CAPABILITY UNAVAILABLE; Q4 IMPLEMENTATION BLOCKED ON `ExactIssuerIdentityAdapterV1` | Only a fresh independent definitive PID-absent OS result may produce `PROVED_GONE`. Any process present at that PID, including suspected reuse from a generic start-token mismatch, remains `LIVE_OR_UNPROVEN`; attended disposal is unavailable and the hold persists. Revision 15 names but does not define the exact-identity adapter. |
| Configured PRE_BARRIER owner loss | NORMATIVE HOLD, GONE-ONLY ATTENDED ESCAPE, AND GLOBAL FENCE CLEARANCE SPECIFIED; IMPLEMENTATION BLOCKED ON #115 AND `ExactIssuerIdentityAdapterV1`; AUTOMATIC RETRY OPTIONAL/UNDELIVERED AS `ConfiguredPreBarrierRetrySuccessorV1` | #115 must persist the inert issuer checkpoint, derive the typed hold, implement the official-store attended disposition, and own the narrow Q4 post-commit witness/barrier reducer over sealed merged-#120 observation operands. Only definitive PID absence admits disposition; a present PID remains held until the adapter lands. With the optional retry successor absent, attended disposition is the sole specified exit from the owner-lost checkpoint: no reload remint, generic release, or release-and-reserve. Disposition installs the singular prior-effect fence; after kill-switch removal and one-current-supervisor restart, only a winning committed fresh observation may yield the source-bound #120 `CLEAR` receipt custody that removes the fence without kill or launch. The general 87-A adapter remains a separate overall dependency; this narrow producer is included in #115's Q4 delivery. |
| Configured-agent relaunch after crash/hard cancellation | NORMATIVE RISK SPLIT SPECIFIED; IMPLEMENTATION/ACTIVATION BLOCKED ON #57 | #120 makes only the exact-FILETIME kill subphase target-safe for an independently authorized retry. The optional retry transition remains absent. `Start-Process` remains non-idempotent; task #57 must deliver and review the project-level singleton per wrapped agent before configured relaunch activates. |
| Maximum ordinary capture sequence | NORMATIVE ATTENDED PATH SPECIFIED; implementation blocked on #115 | Typed exhaustion attention plus one top-level-`IDLE` checked epoch rollover; every persisted non-childless execution or childless envelope/debt/cycle/continuation/retired attempt blocks rollover and remains byte-identical. A non-null prior-effect fence preserves every audit/source/effect field byte-identically and rebases only its freshness floor to `(new epoch, 0)`. An ephemeral transient owner instead follows the closed staged provenance and receipt-CAS outcomes with no old-epoch receipt-driven mutation. |
| No daemon, persistence plane, durable helper or OS object, or runtime dependency | DECIDED ABSOLUTE by operator on 2026-07-31 (M5 Option A) | Project/package boundary; no mechanism-specific exception |
| Supervisor owned-tree termination dispatch | NORMATIVE CONTRACT SPECIFIED; IMPLEMENTATION BLOCKED ON #115 AND #146 | #115 must mint the configured/ephemeral action custody consumed before submission. Merged #120 supplies the exact FILETIME native-body semantics, but merged code still exposes a raw-array entry. #146 installs the closed opaque-sum dispatcher, migrates both raw callers, and proves no direct target call survives. This is scoped to the supervisor owned-tree executor; POSIX remains unavailable. |
| Three-attempt automatic cap and continuous typed attention | NORMATIVE CONTRACT SPECIFIED; implementation blocked on #115 | 87-A state/output |
| Durable human delivery and receipt | STATED out of scope | Future 87-B; every `CAPABILITY_UNAVAILABLE` rendering names the held agent and required operator action |
| Operator explanation of permanent capability limitations and configured-action residuals | REQUIRED before 87-A implementation close and activation | 87-B/follow-up operator manual and tutorial evidence must state all four permanent limitations together, the configured owner-loss hold with its GONE-only attended remedy and subsequent global prior-effect-fence clearance, and #57's duplicate-wrapper residual. It must distinguish POSIX teardown unavailability, quarantine-retirement unavailability, the declared transfer/restore/rollback/migration activation refusal, present-issuer-PID/reuse disposal unavailability pending `ExactIssuerIdentityAdapterV1`, the attended-only PRE_BARRIER exit while automatic retry is absent, the required post-disposition sequence (remove `supervisor.kill`, start exactly one current supervisor under the still-global fence, obtain a winning committed source-bound #120 barrier, handle any surviving target attended, then clear), and configured relaunch unavailable until the durable singleton lands. |

Q4 is **SPECIFIED; IMPLEMENTATION BLOCKED ON #115, #146, #57, AND
`ExactIssuerIdentityAdapterV1`**. This is a
normative-specification exit, not delivery: Q4 remains incomplete,
nonconforming, unsealed, unenforced, and activation-prohibited until those four
dependencies land and their controls pass. Overall 87-A additionally
requires the merged-#120 adapter and closure successor and remains incomplete,
nonconforming, unsealed, unenforced, and activation-prohibited until every named
dependency lands and passes review.

Task #78 consumes the named authority only after #115, #146, #57,
`ExactIssuerIdentityAdapterV1`, the adapter over merged #120, and the
closure successor. Task #116 remains blocked only on #115 and
independently stageable: an already-absent wrapper needs neither a target tree
nor the closure successor. This preserves the
task #94 ordering and #107's single contained supervisor owned-tree kill site
after #146.
