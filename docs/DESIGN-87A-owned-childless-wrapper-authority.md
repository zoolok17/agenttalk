# Design 87-A module: owned-childless wrapper authority

**Status:** Proposed, Revision 5; normative authority module of 87-A. This file
and
[`DESIGN-87A-supervisor-classifier-authority.md`](DESIGN-87A-supervisor-classifier-authority.md)
at the same commit form one specification. Neither is conforming alone.

**Mode:** Reference.

**Audience:** Contributors implementing tasks #78, #115, and #120, and
reviewers checking teardown authority.

## Boundary and placement decision

This module adds one authority case:
`PROVABLY_CHILDLESS_OWNED_WRAPPER`. The authority rule belongs in 87-A because
87-A owns the recovery combiner. The Windows tree-observation and closure
mechanism does not: task #120 must receive a separate, independently reviewable
mechanism design before implementation. 87-A defines the exact typed contract
that design must satisfy.

This is a normative seam, not hand-waving:

```text
87-A observes and classifies
  -> #120 returns COMPLETE tree / HELD closure or a closed refusal
  -> 87-A constructs or refuses authority
  -> existing Stop-Tree is the sole target-kill primitive
```

The core's independently approved operand convention, 96-cell dominant
projection, presence/targetability classifier, whole-wrapper absence reducer,
and condition fingerprint codec/vectors are imported byte-for-byte. This
module does not alter their inputs, equations, tables, serialization, or
counts.

## Safety decision

**ENFORCED:** An owned wrapper whose CLI child is positively absent in two
independent complete observations may be torn down. Such a wrapper has no
brain and no progressing CLI turn to interrupt. This is positive evidence of
nonexistence, not an inference from silence or heartbeat staleness.

`CURRENT_UNKNOWN_ACTIVE_CHILD` is the refused neighboring state. It may have
an incomplete/ambiguous child observation or the first complete `ABSENT`
sample, which is not yet confirmed. This authority requires two adjacent
complete `ABSENT` samples for the same guarded owner. Changing completeness,
owner, or confirmation count changes authority deterministically from the
named case to none.

**ENFORCED:** Ownership is never inferred from a process name, executable
basename, image substring, or command-line pattern. The wrapper must match the
persisted PID, exact start guard, and launch nonce. Every tree target carries
that same owner nonce through a complete #120 ownership proof. Missing,
malformed, unreadable, or mismatched identity refuses authority.

**STATED threat boundary:** This design protects against incomplete
observation, PID reuse, accidental cross-agent selection, process creation
races, partial teardown, crash/reload, and retry fade-out. It does not defend
against a malicious same-user process that can alter supervisor state or
directly terminate arbitrary processes.

## Closed #120 contract

```text
MAX_OWNED_TREE_TARGETS_V1 = 256
AUTOMATIC_CHILDLESS_ATTEMPT_CAP_V1 = 3

ProcStartGuardV1 =
  exact ordinal string emitted by the shipped Proc-Start producer

OwnedWrapperIdentityV1 {
  agent_key: NFC canonical agent/root string
  state_epoch: lowercase hyphenated UUID
  managed_generation: NFC UTF-8 string of at most 128 bytes
  runtime_wrapper_generation: NFC UTF-8 string of at most 128 bytes
  wrapper_pid: integer 1..4294967295
  wrapper_start_guard: ProcStartGuardV1
  launch_nonce: ASCII [A-Za-z0-9_-]{16,128}
}

OwnedTreeTargetV1 {
  pid: integer 1..4294967295
  start_guard: ProcStartGuardV1
  parent_pid: integer 1..4294967295 | null
  parent_start_guard: ProcStartGuardV1 | null
  depth: uint32
  owner_launch_nonce: ASCII [A-Za-z0-9_-]{16,128}
}

OwnedTreeCoverageV1 {
  observer_version: nonempty NFC UTF-8 string of at most 128 bytes
  process_source_digest: Hex64
  ownership_rule_version: "owned-tree/v1"
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
      | TREE_CREATION_NOT_CLOSED
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
        tuple[OwnedTreeTargetV1] of length 1..256
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
      | TREE_CREATION_NOT_CLOSED
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
        tuple[OwnedTreeTargetV1] of length 1..256
      target_digest: Hex64
    }

OwnedTreeClosureV1 =
  BLOCKED(
    ordered deduplicated nonempty tuple[
      CAPABILITY_UNAVAILABLE
      | OWNER_CHANGED
      | CHILD_OBSERVATION_CHANGED
      | DEBT_BINDING_CHANGED
      | CLOSURE_ACQUIRE_FAILED
      | CLOSURE_LOST
      | TREE_OBSERVATION_INCOMPLETE
      | TARGET_DIGEST_CHANGED
    ] in displayed order
  )
  | HELD {
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
        tuple[OwnedTreeTargetV1] of length 1..256
      target_digest: Hex64
    }

OwnedTreeClosureReconciliationV1 =
  NEVER_ACQUIRED {
    acquisition_id: lowercase hyphenated UUID
  }
  | RELEASED {
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
```

Every `COMPLETE`, `COMPLETE_GONE`, or `COMPLETE_RESIDUAL` ordinary tree/debt
observation is well formed only when all of these hold:

```text
capture_id.state_epoch == current ClassifierStateV1.state_epoch
capture_id.agent_key == current ClassifierStateV1.agent_key
capture_id.ordinary_poll_sequence == displayed ordinary_poll_sequence
displayed ordinary_poll_sequence == current ordinary_poll_sequence
capture_id.capture_ordinal == 0
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
Task #120 owns `observer_version`: it changes whenever the platform tree
enumeration, ownership, or closure-membership semantics change and is persisted
and compared as the exact bounded string. 87-A fixes the
`ownership_rule_version`; neither value may be omitted or privately defaulted.

`parent_pid`/`parent_start_guard` record only an owned-parent edge at initial
authorization, never the wrapper's external supervisor/console parent. In an
`INITIAL` tuple, the wrapper is the unique depth-zero target with null parent
fields regardless of its live external OS parent. A non-root member whose
guarded live owned parent is in the set records that parent and has parent
depth plus one. A positively proven owned orphan whose owned parent exited
before the initial capture has null parent fields and depth one. Any other
non-root live OS parent outside the owned set, a cycle, conflicting parent
rows, a duplicate guarded identity, or more than 256 targets is incomplete.
The initial tuple sorts by depth, then PID, then ordinal start guard. Image and
command-line text do not participate.

**ENFORCED positive owner join:** `OwnedWrapperIdentityV1` is constructed only
when all of these independently captured values are present and exactly equal:

```text
checked managed agent/root == strict runtime agent/root == requested agent/root
checked managed wrapper PID == strict runtime wrapper PID == observed root PID
checked managed wrapper start guard
  == strict runtime wrapper start guard == observed root start guard
checked managed wrapper generation
  == strict runtime wrapper generation == runtime_wrapper_generation
checked managed launch nonce
  == strict runtime launch nonce == parsed observed-root launch nonce
```

The checked managed identity also supplies `state_epoch`,
`managed_generation`, and the expected launch nonce; the strict runtime record
supplies the independently validated current binding. The observed root is the
exact guarded row in the same complete raw capture used for tree membership.
The root target's PID, start guard, and owner nonce equal those joined values.
Every other target is bound by task #120 to that exact root and carries the
same nonce.

The observed-root nonce parser reuses the shipped strict
`--supervisor-launch-nonce` grammar: exactly one top-level flag, followed by
one ASCII `[A-Za-z0-9_-]{16,128}` token. Absence, duplication, malformed
text, a flag after the wrapper subcommand/tail boundary, unreadable command
line, or any equality failure returns the applicable `INCOMPLETE` reason.
A process-name, executable, or free-form `CommandLine -match` result is never
an input to this join.

Every target's `owner_launch_nonce` equals the positively guarded wrapper's
nonce. This does not claim that each descendant repeats the nonce in its
command line; task #120's complete ownership mechanism binds the descendant to
that owner.
The `Stop-Tree` projection is exactly `{pid: target.pid, start:
target.start_guard}` in root-first order. The existing primitive reverses that
list, so leaves are attempted before the wrapper and its live PID/start check
still skips reuse.

`COMPLETE_RESIDUAL` is not an initial tree with its root omitted. Task #120
must positively prove that every still-live owner member is an exact
order-preserving subset of the debt's immutable authorized tuple, that every
omitted authorized PID/start is gone, and that no new owner member exists. Its
recorded owner and nonce come from the checked debt; every live target retains
that nonce and its complete authorization-time target object. Residual
parent/depth fields are never recomputed: a surviving depth-three target whose
authorized parent and grandparent exited keeps its original guarded parent
fields and depth even though those ancestor targets are omitted. The initial
orphan/depth and outside-parent rules therefore do not rewrite a residual
tuple. The wrapper may be absent. `COMPLETE_GONE` proves the same universe
empty. If task #120 cannot retain or reconstruct that ownership fact after the
root exits, it returns `INCOMPLETE`.

### Meaning of COMPLETE and HELD

**ENFORCED after task #120:** `COMPLETE` means the capture accounts for every
process the guarded wrapper owns under one explicit coverage signature. It is
not “all rows that happened to be readable.” An implementation that cannot
prove its universe complete returns `INCOMPLETE`.

**ENFORCED after task #120:** `HELD` is an action-scoped, non-destructive
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

The #120 seam exposes acquire, reconcile, and release keyed by
`acquisition_id`; release additionally requires the exact `closure_id`.
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
kill and launch.

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

Task #120 may choose the Windows mechanism, but it may not weaken those
semantics. Its separate design must name the process-universe mechanism,
linearization primitive, crash/release behavior, compatibility evidence, and
failure injection. 87-A conformance requires those tests to pass on every
activated platform. Until then `CAPABILITY_UNAVAILABLE` is the only
conforming result.

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
  owner_identity_id: Hex64 | null
  coverage: OwnedTreeCoverageV1 | null
  first_capture_id: CaptureIdV1 | null
  last_capture_id: CaptureIdV1 | null
  last_ordinary_poll_sequence: uint64 | null
}
```

All eight nullable evidence fields are null exactly when `count=0`. At count
one, every field is non-null and first and last capture IDs are equal. At
count two every field is non-null, capture IDs are distinct, and the last
ordinary sequence is exactly one greater than the first capture's sequence.
At both nonzero counts, first and last capture IDs have
`state_epoch == ClassifierStateV1.state_epoch`,
`agent_key == ClassifierStateV1.agent_key`, and `capture_ordinal == 0`;
`last_ordinary_poll_sequence` exactly equals
`last_capture_id.ordinary_poll_sequence`. Any other shape is invalid checked
state and holds recovery.

One qualifying ordinary poll requires:

```text
raw ProcessObservationV1.availability == COMPLETE
and raw ProcessObservationV1.active_child_availability == COMPLETE
and raw ProcessObservationV1.capture_id.state_epoch ==
    current ClassifierStateV1.state_epoch
and raw ProcessObservationV1.capture_id.agent_key ==
    current ClassifierStateV1.agent_key
and raw ProcessObservationV1.capture_id.ordinary_poll_sequence ==
    current ordinary_poll_sequence
and raw ProcessObservationV1.capture_id.capture_ordinal == 0
and ActiveChildObservationV1 == ABSENT
and core CHILD_DEAD sample basis is valid
and active_child_config_digest is the exact config digest inside that basis
and OwnedWrapperTreeObservationV1 is COMPLETE
and tree.capture_id == raw ProcessObservationV1.capture_id
and tree.ordinary_poll_sequence == current ordinary_poll_sequence
```

The module basis digest is SHA-256 over
`agenttalk.supervisor.owned-childless-confirmation-basis.v1\0` plus
`CanonicalJsonV1` of exactly:

```text
{
  "active_child_config_digest": <Hex64>,
  "coverage": <OwnedTreeCoverageV1>,
  "owner_identity_id": <Hex64>,
  "runtime_child_dead_basis_digest":
    <core ConsecutiveEvidenceV1 CHILD_DEAD basis Hex64>,
  "schema": "owned-childless-confirmation-basis/v1"
}
```

A qualifying sample with no compatible prior sample becomes count one. From
count one, a distinct capture advances to count two only when its ordinary
sequence is adjacent and the complete basis, runtime basis, active-child
config, owner, and coverage are equal. From count two, another distinct
compatible capture adjacent to the stored last capture keeps count two and
slides the window: prior `last_capture_id` becomes `first_capture_id`, the
current capture becomes `last_capture_id`, and the current ordinary sequence
becomes `last_ordinary_poll_sequence`. Replay leaves the state byte-identical.
A gap or any changed equality input restarts at the current qualifying sample;
nonqualifying evidence resets to empty. Tree membership may change between
samples; the action-time closure freezes and revalidates the final target set.
The overlay is committed atomically beside, but never feeds or rewrites, the
banked core counter.

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
      targets: tuple[OwnedTreeTargetV1] of length 1..256
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
  targets: tuple[OwnedTreeTargetV1] of length 1..256
  target_digest: Hex64
  debt_id: Hex64 | null
  debt_generation: strict positive integer | null
}

ChildlessClosureEvidenceV1 {
  mode: INITIAL | DEBT_COMPLETION
  authority_id: Hex64
  basis_id: Hex64
  acquisition_id: lowercase hyphenated UUID
  closure_id: lowercase hyphenated UUID
  closure_capture_id: CaptureIdV1
  owner_identity_id: Hex64
  source_coverage: OwnedTreeCoverageV1
  active_child_capture_id: CaptureIdV1 | null
  active_child: ActiveChildObservationV1 | null
  current_owned_childless_basis_digest: Hex64 | null
  current_runtime_child_dead_basis_digest: Hex64 | null
  current_active_child_config_digest: Hex64 | null
  targets: tuple[OwnedTreeTargetV1] of length 1..256
  target_digest: Hex64
  debt_id: Hex64 | null
  debt_generation: strict positive integer | null
}
```

With no teardown debt, `INITIAL` exists if and only if `childless_source` is
true, presence is `PRESENT_TARGETABLE`, targetability is `COMPLETE` for the
same wrapper PID/start, the current tree is `COMPLETE`, and every
state/owner/capture binding above matches. A second relevant wrapper,
incomplete tree, unguarded target, or targetability mismatch is `BLOCKED`,
never generic teardown fallback. `INITIAL` copies the current tree owner,
capture, coverage, root-first targets, recomputed target digest, committed
successor revision containing those observations, and exact
`RecoveryConditionFingerprintV1` computed from that committed observation.
Its owned confirmation basis, runtime child-death basis, and active-child
config digest are non-null and both debt fields are null.

With outstanding debt and no current attempt, `DEBT_COMPLETION` exists if and
only if the current `OwnedDebtResidualObservationV1` is
`COMPLETE_RESIDUAL` with the exact debt ID/generation/owner, its target tuple is
the exact live order-preserving subset of the immutable authorized tuple, and
every binding is current. It copies that residual capture/coverage/tuple.
It also copies the committed successor revision containing that residual and
its exact same-revision condition fingerprint. Its three
childless/runtime/config basis fields are null and both debt fields are
non-null. A
`COMPLETE_GONE` residual observation clears debt/cycle in an observation-only
checked transition, writes the same-poll terminal, and constructs no authority.
`INCOMPLETE` retains debt and constructs `BLOCKED(TEARDOWN_DEBT_INCOMPLETE)`.
Closure is never a precondition of either authority constructor.

`basis_id` hashes
`agenttalk.supervisor.childless-teardown-basis.v1\0` plus `CanonicalJsonV1` of
exactly:

```text
{
  "active_child_config_digest": <Hex64 | null>,
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

The core copies every displayed proof field into its checked reservation.
Neither an authority hash, the generic targetability digest, nor a
command-line match is decoded or substituted for the target tuple.

`ChildlessClosureEvidenceV1` is the 87-A join over the #120 result and current
checked state; #120 does not decide authority. It is valid only when the
closure acquisition ID equals the persisted attempt ID; its mode,
owner/coverage/targets/digest/debt fields exactly equal both the reservation
and `OwnedTreeClosureV1.HELD`; authority/basis IDs equal the reservation; and
every target digest equals a fresh `OwnedTargetDigestV1` recomputation.

For `INITIAL`, all debt fields are null and all three childless/runtime/config
basis fields are non-null. The joined evidence additionally requires:

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
  HELD.raw_process_observation
) == ABSENT
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
current_owned_childless_basis_digest ==
  a live reconstruction of the module basis from HELD coverage/owner,
  current_runtime_child_dead_basis_digest,
  and current_active_child_config_digest
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

For `DEBT_COMPLETION`, active child, active-child capture ID, and all three
childless/runtime/config basis fields are null; current checked debt still has
the reserved ID/generation/owner/immutable authorized tuple. Every other
well-formed but unequal current observation is an action-time veto. A
structurally invalid persisted state or malformed #120 value is
`POLICY_HELD`; neither class grants authority.

## Action-time closure and sole teardown path

After the checked reservation, one task #115 transaction persists a fresh
attempt ID and moves the core to `TREE_CLOSURE_ACQUIRING`. Automatic origin
also creates or increments its cycle in that transaction. Only then may task
#120 be invoked with that attempt ID to acquire `OwnedTreeClosureV1.HELD`.
Acquisition and crash reconciliation are idempotently keyed by the attempt ID.
87-A must then construct `ChildlessClosureEvidenceV1`; the closure object alone
never supplies authority. A well-formed current equality mismatch is a closure
veto and follows the exact release-pending transition below. Malformed #120
output or structurally invalid checked state is `POLICY_HELD`; if an exact
closure pair is known, only its non-destructive release/reconciliation may
proceed. Neither case kills or launches.

Only after that equality check may one task #115 transaction persist the
closure ID. A second transaction must persist the teardown debt described
below before the executor passes the complete immutable target tuple to the
repository's existing `Stop-Tree`. No copied `Stop-Process`,
`TerminateJobObject`, name/pattern kill, or second target-kill path is
conforming.

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
        tuple[OwnedTreeTargetV1] of length 1..256
      residual_target_digest: Hex64
    }
```

A complete result requires the exact held closure/owner/authorized digest,
complete process and closure-membership coverage, and a capture with the same
state epoch, agent, and ordinary poll sequence whose ordinal is strictly
greater than the action-ready closure capture. `COMPLETE_GONE` means every
authorized PID/start is positively absent and closure membership is empty.
`COMPLETE_RESIDUAL.live_targets` is the exact order-preserving live subset of
the immutable authorized tuple and its digest is a recomputed
`OwnedTargetDigestV1`; any omitted target is positively absent. Any other fact
is `INCOMPLETE`.

The failure mapping is total: a residual containing the authorized tuple's
depth-zero wrapper target is `SAME_OWNER_SURVIVED`; a nonempty residual without
that root is `MEMBER_SURVIVED`; `INCOMPLETE` is `EFFECT_UNPROVEN`.
`COMPLETE_GONE` may clear debt and proceed to the core's fresh final barrier
only in the same checked action invocation after verified closure release.
Every failure retains debt and forbids launch until release handling finishes.
Crash or reload never recreates the same-call launch continuation; it
reconciles closure and debt and requires a later ordinary poll to reserve fresh
authority.

## Durable teardown debt

```text
TeardownDebtV1 =
  NONE
  | OUTSTANDING {
      debt_id: Hex64
      owner: OwnedWrapperIdentityV1
      owner_identity_id: Hex64
      authorized_targets:
        tuple[OwnedTreeTargetV1] of length 1..256
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

At the initial arm, `debt_id` is SHA-256 over
`agenttalk.supervisor.childless-teardown-debt.v1\0` plus canonical
`{"initial_attempt_id": ..., "initial_authority_id": ...,
"owner_identity_id": ..., "state_epoch": ...,
"target_digest": ...}`. It never changes during residual completion.

Outstanding debt has precedence over every new owner, whole-wrapper absence,
manual marker, and relaunch-only proof. A complete observation may:

- clear debt without an OS action only for `COMPLETE_GONE`;
- construct `DEBT_COMPLETION` authority only for an exact live residual subset
  from `COMPLETE_RESIDUAL`, then require a new post-reservation `HELD` closure;
  or
- retain debt and continuous attention for `INCOMPLETE`/changed membership.

`DEBT_COMPLETION` targets only the immutable live residual. This keeps an
orphaned conhost/shell/tool reachable after the wrapper root exits. Debt alone
never authorizes a new identity.

Every finalized childless reservation/attempt outcome or observation-only debt
reconciliation writes
`recovery_poll_terminal_sequence=ordinary_poll_sequence`. Pure refusal,
retained closure uncertainty, prior-poll exhaustion, and a no-op
`NOT_ATTEMPTED` row that leaves debt/cycle/execution unchanged do not. The
`NOT_ATTEMPTED` emitted after successful observation-only or reload cleanup is
a finalized reconciliation and does write the terminal. The reservation
predicate rejects equality. The next ordinary-poll increment clears the
terminal. Thus one poll cannot consume two automatic attempts or turn
finalized reconciliation into immediate launch.

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
`TREE_CLOSURE_ACQUIRING` with its attempt ID immediately before invoking task
#120. Planning, policy holds, and a statically blocked proof consume zero;
closure failure therefore cannot loop outside the cap. This bounded cycle does
not read or mutate generic `consecutive_fails`, recovery-backoff deadline, or
backoff exponent fields. After a typed failure on attempts one or two, the next
attempt may be reserved only by the next eligible ordinary poll after the
same-poll terminal clears; all ordinary gates are reevaluated. It is neither
scheduled by nor hidden behind exponential backoff. Attempt three plus any
failure becomes sticky `EXHAUSTED`: it schedules no fourth automatic
reservation.

`EXHAUSTED` emits `AUTOMATIC_CHILDLESS_RETRY_EXHAUSTED` and mandatory action
attention on every poll until a successful childless cleanup, a new guarded
owner commit, or a successful authorized manual cleanup clears the cycle. It
suppresses only a further automatic named childless teardown for that owner;
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
if teardown_debt is OUTSTANDING:
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

The module exports:

```text
ChildlessActionResultV1 =
  NOT_ATTEMPTED
  | POLICY_HELD
  | BARRIER_VETOED
  | TEARDOWN_FAILED
  | AUTOMATIC_RETRY_EXHAUSTED

ChildlessAttentionCodeV1 =
  CHILDLESS_OWNER_CHILD_TREE_OR_CLOSURE_INCOMPLETE
  | CHILDLESS_TEARDOWN_DEBT
  | AUTOMATIC_CHILDLESS_RETRY_ACTIVE
  | AUTOMATIC_CHILDLESS_RETRY_EXHAUSTED

TeardownDebtSummaryV1 =
  NONE
  | OUTSTANDING {
      debt_id: Hex64
      owner_identity_id: Hex64
      authorized_target_count: integer 1..256
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
CHILDLESS_OWNER_CHILD_TREE_OR_CLOSURE_INCOMPLETE iff
  the relevant owner/child/tree/debt-residual constructor is BLOCKED/INCOMPLETE
  or a named childless acquisition reconciliation is UNKNOWN
  or a release reconciliation is HELD or UNKNOWN
  or an existing named childless phase is retained because safety
     reconciliation is not eligible
  or checked childless state/#120 output is structurally invalid

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

An already-owned childless fence uses a narrower gate than new recovery:

```text
ChildlessSafetyReconciliationGateV1 =
  RETAIN_DRY_RUN
    if ExecutionGateCaptureV1.dry_run == true
  | RETAIN_NO_CURRENT_SUPERVISOR
    if ExecutionGateCaptureV1.supervisor_instance != CURRENT
  | MAY_RECONCILE
```

`RETAIN_DRY_RUN` performs no #120 call and no persistence.
`RETAIN_NO_CURRENT_SUPERVISOR` performs no #120 call and no state mutation
because the invocation does not own the executor claim. Both retain the exact
reservation, closure, pending disposition, debt/current attempt, cycle, and
terminal. `MAY_RECONCILE` authorizes only checked non-destructive cleanup of an
already-persisted named reservation/phase: release `PRE_BARRIER`, call #120
reconcile/release by its persisted attempt/closure IDs, capture post-action or
residual evidence after an already-issued teardown, and finalize the persisted
disposition. It may do so while kill switch, action latch, report membership,
or auto-restart blocks new recovery, because it cannot acquire a new closure,
reserve/increment an attempt, invoke `Stop-Tree`, launch, consume a marker, or
change generic backoff/readiness. This is fence cleanup, not teardown
authority. Unknown cleanup evidence retains every fence.

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
- `COMPLETE_GONE` clears debt and the old-owner cycle. Only the original live
  invocation may normalize the reservation to `PRE_BARRIER` and continue to
  the fresh final barrier; reload finalization enters `IDLE` with no launch.

Each finalized branch writes the same-poll terminal. Failure attempts one and
two schedule no generic backoff; a later retry begins only on a subsequent
eligible ordinary poll.

An ordinary residual observation is pure captured input, not a debt/cycle
mutation by `ClassifierObservationDeltaV1`. Its `COMPLETE_GONE` clear is a
separate recovery-authority delta and may commit only when
`ExecutionEligibilityV1 == ELIGIBLE`, `recovery_execution == IDLE`, and there
is no named reservation, closure ID, pending disposition, or debt current
attempt. `DRY_RUN` may simulate it but discards every delta. Every other
noneligible global gate retains debt, cycle, execution, terminal, and
continuous attention and falls through to the applicable no-op gate row
below. Any persisted named phase takes the later phase-specific
release/reconciliation rows and cannot be erased by an ordinary observation.

The event constructor evaluates the following rows top to bottom and returns
the first applicable row; each later row therefore includes the negation of
all earlier predicates. The resulting event/result mapping is disjoint and
exhaustive:

| Event at this poll | State/debt/cycle effect | Module result | Required visibility |
| --- | --- | --- | --- |
| Structurally invalid persisted state or malformed #120 value | No destructive mutation; retain every owned fence. If an exact known closure exists and the safety-reconciliation gate is `MAY_RECONCILE`, only its non-destructive release/reconciliation may proceed. | `POLICY_HELD` | Incomplete code; plus debt/cycle codes by predicate. |
| Well-formed owner/child/tree/debt proof is incomplete before reservation | No reservation and no attempt consumed. | `POLICY_HELD` | Incomplete code; plus debt/cycle codes by predicate. |
| `ExecutionEligibilityV1 == ELIGIBLE`, `recovery_execution == IDLE`, no named reservation/closure/pending disposition/current attempt exists, and ordinary observation-only debt reconciliation is `COMPLETE_GONE` | Clear debt and old-owner cycle, remain `IDLE`, write terminal; construct no authority and do not launch. | `NOT_ATTEMPTED` | No childless code after the atomic clear. |
| No in-flight childless phase and a global/policy gate holds or no named candidate exists; cycle is `NONE` | No module mutation or terminal write. | `NOT_ATTEMPTED` | Debt/incomplete code only when its predicate independently applies. |
| No in-flight childless phase and a global/policy gate holds or no named candidate exists; cycle is `ACTIVE` with a prior typed failure | Preserve the cycle; no attempt, backoff, or terminal mutation. | `NOT_ATTEMPTED` | Active code; debt/incomplete code by predicate. |
| No in-flight childless phase and a global/policy gate holds or no otherwise-eligible automatic named proof exists; cycle is `EXHAUSTED` | Preserve the cycle; no attempt, backoff, or terminal mutation. | `NOT_ATTEMPTED` | Exhausted code; debt/incomplete code by predicate. |
| Automatic named proof is otherwise eligible, no eligible manual origin wins, and its cycle was already `EXHAUSTED` before this poll | No reservation, attempt, backoff, or terminal mutation. | `AUTOMATIC_RETRY_EXHAUSTED` | Exhausted code; debt code if applicable. |
| An existing named reservation/phase requires reconciliation and the safety-reconciliation gate is not `MAY_RECONCILE` | Retain reservation/phase, closure, pending disposition, debt/current attempt, cycle, and terminal byte-identically; perform no #120 call. | `POLICY_HELD` | Incomplete code; plus debt/cycle codes by predicate. |
| A named `PRE_BARRIER` reservation is vetoed or reloaded before closure acquisition begins | Release that reservation, consume no automatic attempt, preserve debt/cycle and marker semantics, and write the terminal. | `BARRIER_VETOED` | Debt/cycle/incomplete code by predicate. |
| Live acquisition returns a well-formed `HELD` and the full joined evidence is valid | Persist `TREE_CLOSURE_HELD`; no outcome or terminal is finalized. | No terminal module result yet. | Codes from preexisting debt/cycle only. |
| Any well-formed matching `HELD` returned by acquisition/reconciliation is not action-ready, or recaptured evidence/gates make a persisted `TREE_CLOSURE_HELD` non-action-ready | Bind/retain its closure ID, persist `TREE_CLOSURE_RELEASING/CLOSURE_VETOED`, and request release; no outcome is finalized yet. The earlier live-valid row alone may continue toward teardown. | No terminal module result yet. | Incomplete code while release remains held; debt code if applicable. |
| `TEARDOWN_IN_FLIGHT` reconciliation obtains matching `HELD` | Take the fresh post-action observation under closure and persist its mapped releasing disposition; no outcome is finalized yet. | No terminal module result yet. | Debt code; incomplete also when the observation is incomplete. |
| `TREE_CLOSURE_RELEASING` reconciliation remains matching `HELD`, or any applicable acquisition/reconciliation is `UNKNOWN` | Retain phase, reservation, pending disposition, debt/current attempt, and automatic `ISSUED`; no terminal write. | `POLICY_HELD` | Incomplete code; debt code if applicable. |
| Acquisition returns no closure and matching reconciliation proves `NEVER_ACQUIRED`; acquiring reconciliation proves matching `RELEASED` and binds its returned closure ID; held-phase reconciliation proves matching `RELEASED` after atomically recording pending `CLOSURE_VETOED`; or any persisted `TREE_CLOSURE_RELEASING/CLOSURE_VETOED` reaches exact `RELEASED` | Finalize `CLOSURE_VETOED`; debt unchanged. Manual origin always leaves the cycle byte-identical; automatic issued-attempt count 1–2 becomes `ACTIVE/CLOSURE_VETOED`; count 3 becomes `EXHAUSTED/CLOSURE_VETOED`. | Manual origin: `BARRIER_VETOED`; automatic count 1–2: `BARRIER_VETOED`; automatic count 3: `AUTOMATIC_RETRY_EXHAUSTED`. | Cycle code by resulting predicate; debt code if applicable. |
| Post-action `COMPLETE_RESIDUAL` contains the wrapper root and exact release is proved | Finalize `SAME_OWNER_SURVIVED`. Manual origin leaves any cycle byte-identical; automatic issued-attempt count 1–2 becomes `ACTIVE`, count 3 `EXHAUSTED`. | Manual origin: `TEARDOWN_FAILED`; automatic count 1–2: `TEARDOWN_FAILED`; automatic count 3: `AUTOMATIC_RETRY_EXHAUSTED`. | Debt plus cycle code by predicate. |
| Post-action `COMPLETE_RESIDUAL` omits the wrapper root and exact release is proved | Apply the preceding row with `MEMBER_SURVIVED`. | Same origin/attempt-sensitive result as the preceding row. | Debt plus cycle code by predicate. |
| Post-action `INCOMPLETE` and exact release is proved | Apply the preceding row with `EFFECT_UNPROVEN`. | Same origin/attempt-sensitive result as the preceding row. | Incomplete, debt, and cycle code by predicate. |
| Live post-action `COMPLETE_GONE`, exact release, and original invocation survives | Clear debt/cycle and continue only to the core final barrier. | No terminal module result; the core result owns the outcome. | Codes recomputed from cleared state. |
| Reload or reconciliation finalizes `COMPLETE_GONE` without the original live continuation | Clear debt/cycle, enter `IDLE`, write the terminal, and do not launch. | `NOT_ATTEMPTED` | Codes recomputed from cleared state. |

A well-formed current-evidence change is the action-time veto row, not invalid
state. An ID/revision/owner/target/origin mismatch inside persisted checked
state is the invalid-state row, not a barrier veto. The automatic-attempt-three
row takes precedence over the generic veto/failure result only after exact
release finalizes that failure. Manual origin never consults the automatic
attempt ordinal: its veto is always `BARRIER_VETOED`, its teardown failure is
always `TEARDOWN_FAILED`, and its cycle is byte-identical. Reload finalization
uses the persisted origin and the same mapping.
Rows marked “no terminal module result yet” are internal same-invocation
transitions and do not emit an action-resolution record at that point. The
invocation either reaches a later final row or returns with retained checked
state; the next poll then matches the applicable retained
`POLICY_HELD`/`NOT_ATTEMPTED` row. Every emitted module result is one closed
`ChildlessActionResultV1` value.

Normal execution is:

| Transition | Checked effect |
| --- | --- |
| Reserve named proof | Require core execution `IDLE`, no same-poll terminal, and no outstanding current attempt. Store origin, proof fields, target tuple, and execution-gate snapshot. Manual-wins stores its manual authority ID separately. |
| Begin closure acquisition | Live-recompute the reserved initial-tree or debt-residual bindings, persist `TREE_CLOSURE_ACQUIRING` with a fresh attempt ID/revision, and only then invoke task #120. Automatic origin creates/increments `ACTIVE/ISSUED`; manual origin does not change the cycle. |
| Acquire valid closure | Require the full joined live-basis/capture/target equality, then persist `TREE_CLOSURE_HELD` and the exact closure ID. Do not increment the attempt again. |
| Veto after `HELD` | This includes a live join mismatch or any recaptured execution/manual/policy gate veto. Persist `TREE_CLOSURE_RELEASING/CLOSURE_VETOED`, request release by exact pair, and finalize only after matching `RELEASED`. |
| Arm teardown | Before `Stop-Tree`, atomically enter `TEARDOWN_IN_FLIGHT` and create/update origin-neutral debt with the same attempt and immutable authorized tuple. |
| Observe action effect | Map the closed post-action observation to one pending disposition, atomically enter `TREE_CLOSURE_RELEASING` without clearing debt/current attempt or cycle `ISSUED`, then request exact release. |
| Finalize release | Apply the applicable pending-disposition rule above in one checked transaction. |
| New guarded owner commits | Permitted only with debt `NONE`, no childless closure phase, and no pending disposition. Clear a debt-free old-owner cycle; leave no inherited authority. |

Crash/reload is equally closed after
`ChildlessSafetyReconciliationGateV1 == MAY_RECONCILE`; either retain variant
uses the explicit retained-fence event row above:

- `TREE_CLOSURE_ACQUIRING`: reconcile by attempt ID. Matching
  `NEVER_ACQUIRED` finalizes `CLOSURE_VETOED`; matching `RELEASED` binds its
  returned closure ID and finalizes the same veto; matching `HELD` is persisted
  as releasing/vetoed and released without termination; `UNKNOWN` retains
  state.
- `TREE_CLOSURE_HELD`: do not terminate. Persist
  releasing/`CLOSURE_VETOED`; matching `RELEASED` may then finalize directly,
  while matching `HELD` requires exact release and a later matching
  `RELEASED`. `UNKNOWN` retains the held state.
- `TREE_CLOSURE_RELEASING`: reconcile the exact pair and finalize the already
  persisted disposition only on matching `RELEASED`.
- `TEARDOWN_IN_FLIGHT`: never reissue `Stop-Tree`. Matching `HELD` obtains a
  fresh post-action observation under that closure, persists the mapped
  releasing disposition, and follows exact release. Matching `RELEASED`
  obtains a fresh `OwnedDebtResidualObservationV1` without assuming frozen
  membership: `COMPLETE_GONE` clears debt/cycle into `IDLE`;
  `COMPLETE_RESIDUAL` or `INCOMPLETE` finalizes the mapped failure into `IDLE`.
  No reload branch launches. `UNKNOWN` retains reservation, debt/current
  attempt, and cycle `ISSUED`; `NEVER_ACQUIRED` is invalid after debt and
  retains the fence.

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
    truncation, and more than 256 targets refuse. Prove the depth-zero
    wrapper's live external supervisor/console parent is excluded from owned
    parent fields and does not refuse an otherwise complete initial tree.
3. Execute task #120's separate mechanism suite. Prove closure linearization,
   complete membership, no post-closure creation, no target termination by the
   closure, attempt-keyed acquire/reconcile/release, crash safety, and
   activated-platform compatibility. Until all pass, capability is unavailable.
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
5. Prove the initial root-first tuple and inherited-order residual tuple reach
   only the existing leaves-first `Stop-Tree` adapter. No process name/pattern
   or second kill path may reach an authority target.
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
    Seed a future generic recovery-backoff deadline and nonzero exponent with
    cycle `NONE`, `ACTIVE/failure`, and `EXHAUSTED`. An otherwise eligible
    named attempt must bypass that deadline for `NONE` and `ACTIVE`; exhaustion
    must still refuse a fourth attempt. Every case leaves generic backoff
    byte-identical. The same deadline must still hold a non-childless automatic
    candidate.
8. After every finalized childless reservation/attempt outcome and
   observation-only debt reconciliation, attempt a second reservation in the
   same ordinary poll; the terminal must refuse it. Prove pure refusal,
   retained uncertainty, prior-poll exhaustion, and no-op `NOT_ATTEMPTED` do
   not write the terminal; successful cleanup `NOT_ATTEMPTED` does. Cross
   ordinary `COMPLETE_GONE` debt reconciliation with every
   `ExecutionEligibilityV1`: only `ELIGIBLE` clears and writes the terminal,
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
    dry run and a non-current supervisor retain state byte-identically and make
    no #120 call; a current supervisor under kill switch, disabled action
    latch, missing report membership, or disabled auto-restart may only
    reconcile/release/finalize the old fence and never acquire, kill, launch,
    consume a marker, increment an attempt, or change backoff/readiness.
13. Generate every debt-current-attempt/cycle/execution cross-product. Accept
    only the stated biconditionals, attempt transitions, same-owner equality,
    and origin rules. In particular reject `IDLE + debt ISSUED`,
    `IDLE + cycle ISSUED`, `ACTIVE/failure` at count three, `EXHAUSTED` below
    count three, and any skipped/repeated/fourth attempt as `POLICY_HELD`.

## Dependencies and release order

| Property | Classification | Owner |
| --- | --- | --- |
| Two same-owner complete child absences | ENFORCED | 87-A reducer |
| PID/start/nonce ownership; never pattern ownership | ENFORCED | 87-A/#120 contract |
| Complete tree and action-scoped creation closure | ENFORCED after #120 | Separate #120 mechanism design and implementation |
| Atomic reservation/debt/cycle/terminal state | ENFORCED after #115 | Task #115 checked state owner |
| Sole leaves-first guarded kill | ENFORCED | Existing `Stop-Tree` |
| Three-attempt automatic cap and continuous typed attention | ENFORCED after #115 | 87-A state/output |
| Durable human delivery and receipt | STATED out of scope | Future 87-B |

Task #78 consumes the named authority only after #115 and #120. Task #116
remains blocked only on #115 and independently stageable: an already-absent
wrapper needs neither a target tree nor task #120's closure. This preserves the
task #94 ordering and #107's single contained kill site.
