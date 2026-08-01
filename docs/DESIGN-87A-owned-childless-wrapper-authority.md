# Design 87-A module: owned-childless wrapper authority

**Status:** Proposed, Revision 8; normative authority module of 87-A. This file
and
[`DESIGN-87A-supervisor-classifier-authority.md`](DESIGN-87A-supervisor-classifier-authority.md)
at the same commit form one specification. Neither is conforming alone.

**Mode:** Reference.

**Audience:** Contributors implementing tasks #78, #115, and #120, the
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
  -> the closure successor declares AVAILABLE or CAPABILITY_UNAVAILABLE
  -> only AVAILABLE may acquire HELD closure or a transient closed refusal
  -> 87-A constructs or refuses authority
  -> existing Stop-Tree is the sole target-kill primitive
  -> #120's post-kill barrier may block, but never authorize, a launch
```

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

**ENFORCED after task #115, the merged-#120 adapter, and the closure successor
(#120 input delivered at `587e7c1`):** An owned wrapper whose CLI child is
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

**ENFORCED by the 87-A adapter over merged, reviewed task #120 (input delivered
at `587e7c1`):** Ownership is never inferred from a process name,
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
| Live `entries[].pid/start/start_filetime` | `OwnedTreeTargetV1.pid/start_guard` after exact live validation. On Windows the destructive guard is the positive decimal `start_filetime`; the rounded ISO `start` is capture/ordering corroboration and never substitutes for a missing FILETIME. On a platform whose exact start identity is carried directly in `start`, that exact token is the guard. The adapter derives and validates `parent_start_guard` and `depth` from the accepted live parent chain and projects the validated top-level owner nonce onto each target; it may not privately default any missing fact. |
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

The barrier rechecks recorded exact identities and fresh descendant edges.
Planning and `Stop-Tree` remain separated by process scheduling, so a recorded
parent may create a descendant after the plan; that unplanned descendant may
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

**STATED merged behavioral delta:** Once #115 and a future conforming closure
provider have independently authorized teardown, a planned target whose
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
Merged #120 partially strengthens effect execution at one target-local seam;
it does not implement those attempt-bound contracts or prevent an owned parent
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
to the named closure-dependent path; it does not disable merged #120's
snapshot, existing authorized exact-target execution, barrier, attended reset,
or attended archive for their non-87-A callers.

```text
MAX_OWNED_TREE_TARGETS_V1 = 64
AUTOMATIC_CHILDLESS_ATTEMPT_CAP_V1 = 3

ProcStartGuardV1 =
  nonempty NFC UTF-8 process-start representation token of at most 256 bytes

OwnedExactStartGuardV1 =
  Windows ISO-start row: positive decimal exact creation FILETIME
  other exact-start row: exact ordinal entries[].start token

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

StateLossQuarantineV1 =
  NONE
  | UNRESOLVED {
      quarantine_id: lowercase hyphenated UUID
      reason: MISSING | CORRUPT | TORN | ROLLBACK_UNPROVEN
      prior_physical_owner: OwnedPhysicalWrapperIdentityV1 | UNKNOWN
      attempt_provenance: UNKNOWN
      debt_provenance: UNKNOWN
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

LostOwnerExtinctionObservationV1 =
  INCOMPLETE(
    ordered deduplicated nonempty tuple[
      CAPABILITY_UNAVAILABLE
      | SNAPSHOT_UNAVAILABLE
      | SNAPSHOT_TRUNCATED
      | COVERAGE_UNREADABLE
      | PRIOR_PHYSICAL_OWNER_UNKNOWN
      | PRIOR_OWNER_IDENTITY_UNREADABLE
      | PRIOR_OWNER_OR_RESIDUAL_MEMBERSHIP_UNPROVEN
    ] in displayed order
  )
  | COMPLETE_GONE {
      capture_id: CaptureIdV1
      coverage: OwnedTreeCoverageV1
      prior_physical_owner: OwnedPhysicalWrapperIdentityV1
      replacement_physical_owner: OwnedPhysicalWrapperIdentityV1
      replacement_root_guarded_identity_present: true
      prior_root_guarded_identity_absent: true
      every_prior_owner_residual_absent: true
    }

ProvablyDifferentPhysicalOwnerV1 {
  prior_physical_owner: OwnedPhysicalWrapperIdentityV1
  replacement_owner: OwnedWrapperIdentityV1
  extinction: LostOwnerExtinctionObservationV1.COMPLETE_GONE
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

ClosureCapabilityV1 =
  AVAILABLE {
    closure_provider_version: ClosureProviderVersionV1
  }
  | CAPABILITY_UNAVAILABLE(
      ordered deduplicated nonempty tuple[
        SUCCESSOR_MISSING
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

ChildlessContinuationOwnerV1 =
  NONE
  | OWNED {
      supervisor_instance_token_digest: Hex64
      supervisor_pid: integer 1..4294967295
      supervisor_start_guard: ProcStartGuardV1
      action_latch_epoch: uint64 | null
      continuation_id: lowercase hyphenated UUID
      role: ISSUER | RECONCILER
      closure_provider_version: ClosureProviderVersionV1
      attempt_id: lowercase hyphenated UUID
      attempt_revision: uint64
      operation:
        CLOSURE_ACQUIRE
        | CLOSURE_RECONCILE
        | CLOSURE_RELEASE
        | STOP_TREE
      effect_stage: ARMED | CALL_RETURNED
      armed_state_revision: uint64
    }
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
The 87-A adapter owns `observer_version`. For the mapping pinned above it is
exactly `win-tree/v2`, which binds merged task #120 at `587e7c1`, including its
`schema_version`, `attribution_model`, exact-FILETIME kill projection,
same-handle exact-check/termination plus conditional bounded wait attempt,
recycle-aware enumeration/ownership algorithm, and pinned implementation
revision. This replaces the pre-review `win-tree/v1`
mapping; the chained module vectors below are renewed accordingly. Any later
change to those inputs requires a different version value and renewed
vectors/review. 87-A fixes `ownership_rule_version` to `owned-tree/v2` and the
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

**ENFORCED positive owner join:** `OwnedWrapperIdentityV1` is constructed only
when all of these independently captured values are present and satisfy the
displayed relation:

```text
checked managed agent/root == strict runtime agent/root == requested agent/root
checked managed wrapper PID == strict runtime wrapper PID == observed root PID
start_anchor := observed root reported start token
StartRepresentationMatchV1(checked managed wrapper start token, start_anchor)
StartRepresentationMatchV1(strict runtime wrapper start token, start_anchor)
Windows observed root exact start guard == fresh #120 exact start_filetime
non-Windows observed root exact start guard == exact observed-root start token
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
`start_filetime` is the destructive identity. A non-Windows exact-start target
uses its exact `start_guard` as `start` and omits `start_filetime`. The existing
primitive reverses the list, so leaves are attempted before the wrapper; every
owned Windows target missing its exact FILETIME is refused rather than falling
back to the legacy rounded `Proc-Start`/`Stop-Process` path.

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

**ENFORCED after merged, reviewed task #120 and the 87-A adapter:**
`COMPLETE` means the capture accounts for every
process the guarded wrapper owns under one explicit coverage signature. It is
not “all rows that happened to be readable.” An implementation that cannot
prove its universe complete returns `INCOMPLETE`.

**ENFORCED after the closure successor:** `HELD` is an action-scoped, non-destructive
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
`closure_id`. `AVAILABLE` exposes one exact `ClosureProviderVersionV1` and is
well formed only for an installed, independently reviewed implementation that
can prove this contract inside the absolute dependency-plane constraint for
every case it accepts. `CAPABILITY_UNAVAILABLE` is structural and may appear
only before reservation. The caller persists an available version in
`ChildlessContinuationOwnerV1` before acquisition;
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
same checked transaction appends it to `retired_childless_attempt_ids`, and
the closure successor must not later acquire it. An unexpected late `HELD` for a retired
ID is never authority; it is accepted only to perform exact idempotent release
under the effect guard, after which the tombstone remains.

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
and current ChildEstablishmentGuardV1 is CLOSED for the exact strict turn
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
leaves the state byte-identical. A gap or any changed equality input restarts
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

The core copies every displayed proof field into its checked reservation.
Neither an authority hash, the generic targetability digest, nor a
command-line match is decoded or substituted for the target tuple.

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

## Action-time closure and sole teardown path

**ENFORCED effect linearization after task #115 and the closure successor:**
State CAS alone does not own an external call. Every childless
closure-successor call, `Stop-Tree` call, and
action capture therefore runs under one exclusive transient per-agent effect
guard and one checked `ChildlessContinuationOwnerV1`. Lock order is fixed:
configuration snapshot, action latch, effect guard, then the short #115
checked-state transaction. The effect guard is caller-owned synchronization,
not a daemon or persistence plane: it exists only while a live caller holds an
in-process or kernel handle, is automatically released when that process
exits, has no detached owner or durable payload, and is never evidence after
release. No timeout, heartbeat, lease renewal, or second poller may steal it.

The checked owner identifies the exact supervisor process/start, applicable
action-latch epoch, continuation role, attempt, operation, stage, and state
revision. The invocation
acquires the effect guard, recaptures every gate and basis, commits
`effect_stage=ARMED`, rechecks that it still owns both the guard and exact
checked owner, performs the one synchronous external operation, and commits
`CALL_RETURNED` or the operation-specific terminal result before releasing the
guard. An adapter refuses a retired attempt ID and any caller whose persisted
continuation owner no longer matches. Thus committing a phase never, by
itself, licenses a later stale continuation.

A non-`NONE` owner is well formed only when its token digest and PID/start
exactly equal the arm-time `ExecutionGateCaptureV1` current supervisor; its
attempt ID/revision equal the core reservation; and `armed_state_revision` is
the successor revision that wrote that owner. `CLOSURE_ACQUIRE` and callable
`STOP_TREE/ARMED` require `role=ISSUER` and non-null
`action_latch_epoch` exactly equal to a fresh enabled action latch.
Non-destructive `CLOSURE_RECONCILE` and
`CLOSURE_RELEASE` retain the current epoch when the latch is enabled and use
null when cleanup is permitted under a disabled latch.
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

`role=RECONCILER` can be written only by the takeover CAS below. It may invoke
only idempotent closure-successor reconcile/release operations. In particular,
an inherited `STOP_TREE/ARMED/ISSUER` tombstone is transformed atomically to
`TREE_CLOSURE_RELEASING/EFFECT_UNPROVEN` plus
`CLOSURE_RELEASE/ARMED/RECONCILER`; a
`STOP_TREE/ARMED/RECONCILER` state is invalid and can never call `Stop-Tree`.
`STOP_TREE/CALL_RETURNED/RECONCILER` may take the read-only post-action
capture, then release. These role constraints are checked again by each
adapter.

A different poller cannot reconcile while the persisted continuation may
resume: it returns `RETAIN_LIVE_CONTINUATION`. Takeover is permitted only
after acquiring the released effect guard and positively proving either
same-process structured unwind/cancellation or that the guarded predecessor
PID/start no longer exists. Mere age, missing heartbeat, a timeout, or
different supervisor token is not proof. The takeover writes a new checked
continuation owner with `role=RECONCILER` before any reconciliation operation.
A matching
`NEVER_ACQUIRED` is a stable terminal result for that acquisition attempt and
retires the attempt in the same transaction; a later unexpected `HELD` is
release-only and can never restore authority. Any result that cannot be
classified exactly is `UNKNOWN` and retains the fence.

After the checked reservation, an invocation under that guard persists a fresh
attempt ID,
`ChildlessContinuationOwnerV1(CLOSURE_ACQUIRE, ARMED, ISSUER)`, and
`TREE_CLOSURE_ACQUIRING` in one task #115 transaction. Automatic origin also
creates or increments its cycle in that transaction. Only that checked owner
may synchronously invoke the closure successor with the attempt ID. It retains the effect
guard through the returned-result CAS. Acquisition and crash reconciliation
are idempotently keyed by the attempt ID. 87-A must then construct
`ChildlessClosureEvidenceV1`; the closure object alone never supplies
authority. A well-formed current equality mismatch is a closure veto and
follows exact release. Malformed closure-successor output or structurally invalid checked
state is `POLICY_HELD`; if an exact closure pair is known, only its
non-destructive release/reconciliation may proceed. Neither case kills or
launches.

Only after that equality check may the guard-owning invocation persist the
closure ID while retaining its acquire/reconcile owner at `CALL_RETURNED`.
Before termination, it atomically
persists the teardown debt, enters `TEARDOWN_IN_FLIGHT`, and records
`ChildlessContinuationOwnerV1(STOP_TREE, ARMED, ISSUER)`. It rechecks exact ownership
and then passes the complete immutable target tuple to the repository's
existing `Stop-Tree`. On synchronous return it commits the same owner with
`CALL_RETURNED`; only that stage may reserve a post-action capture. If the
owner dies or unwind is proved while still `ARMED`, recovery never reissues
`Stop-Tree` or assumes it did not run: it records `EFFECT_UNPROVEN`, retains
debt, releases any exact closure, and forbids launch. No copied
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

At the initial arm, `debt_id` is SHA-256 over
`agenttalk.supervisor.childless-teardown-debt.v1\0` plus canonical
`{"initial_attempt_id": ..., "initial_authority_id": ...,
"owner_identity_id": ..., "state_epoch": ...,
"target_digest": ...}`. It never changes during residual completion.

### Chained digest conformance vector

**ENFORCED:** The following Revision 8 fixture fixes all seven module digest
domains and renews the authority-dependent chain for merged #120's
`win-tree/v2` adapter and the explicit representation-token/exact-guard split.
The two banked core condition fingerprints are outside this chain and remain
untouched.
Each payload is the exact one-line ASCII/UTF-8 `CanonicalJsonV1` byte sequence
shown, with no trailing LF. The hash input is the displayed ASCII domain,
one NUL byte, then the payload bytes. Later payloads use earlier expected
digests, so recomputing this chain also detects a private field set or
serialization in any upstream domain.

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

## Fail-closed state-loss quarantine

**ENFORCED after task #115:** A missing, corrupt, torn, or rollback-unproven
checked state is not clean genesis. Recovery creates a new `state_epoch` only
with `StateLossQuarantineV1.UNRESOLVED`; the physical owner projection
deliberately excludes that epoch. The quarantine denies the named teardown,
every other kill, every launch/relaunch, closure acquisition, attempt
increment/reset, debt clear, marker consumption, managed-owner commit, and
grace-based recovery. It emits continuous `STATE_PROVENANCE_LOST` attention.
Manual acknowledgement or force cannot override it.

This revision intentionally defines no exact-restoration constructor. The
shipped store may accept a structurally valid backup that is one committed
generation behind; schema validity therefore cannot prove that an apparent
restoration contains the lost latest attempt count or teardown debt. Such a
backup remains `ROLLBACK_UNPROVEN` and quarantined. Adding an independently
retained last-commit revision/digest would change the persistence contract and
requires a separately reviewed versioned design.

The only quarantine-clear transaction is therefore a complete
`ProvablyDifferentPhysicalOwnerV1` proving both a distinct physical replacement
and extinction of the prior wrapper plus every process that could belong to an
erased debt tuple.

A different `state_epoch`, wrapper generation, PID, start guard, nonce, or
merely valid backup by itself is insufficient. The replacement's exact
physical projection must differ from the prior projection in at least one
field; `replacement_owner` projected without its new epoch must equal
`extinction.replacement_physical_owner`; and
`prior_physical_owner == extinction.prior_physical_owner ==` the quarantine's
known prior owner. The current ordinary capture must have ordinal zero,
current epoch/agent/sequence, complete ownership coverage, the replacement
root positively present, and the prior root plus every possible nonce-owned
rootless residual positively absent. If the prior physical owner is `UNKNOWN`,
or the 87-A adapter over #120 cannot completely prove the old owner and all
possible rootless residuals
gone, the second transition is impossible and quarantine remains
indefinitely. This is intentional: lost provenance after a partial
`Stop-Tree` cannot be laundered into launch authority.

The clearing CAS requires `dry_run=false`, a freshly captured current
supervisor, an unchanged quarantine ID and state revision, and the same live
complete capture revalidated immediately before commit. It atomically installs
the already-present replacement owner, sets quarantine/debt/cycle/execution/
continuation/pending disposition to their clean values, writes the same-poll
terminal and a quarantine-retired audit fact, and performs no OS action. No
kill or launch is permitted in that poll; the next ordinary poll must rebuild
all evidence from the replacement owner.

State loss after issued attempt one, two, or three therefore cannot recreate a
fresh three-attempt budget. State loss after debt arm or any partial target
attempt cannot erase debt precedence. Task #115 must distinguish clean initial
creation from loss of an expected state object; implementations that cannot
make that distinction fail closed in quarantine.

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
`ProvablyDifferentPhysicalOwnerV1` transition outside quarantine, or a
successful authorized manual cleanup clears the cycle. A mere new
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
  | RETAIN_STATE_PROVENANCE_LOST
    if StateLossQuarantineV1 is UNRESOLVED
  | RETAIN_NO_CURRENT_SUPERVISOR
    if ExecutionGateCaptureV1.supervisor_instance != CURRENT
  | MAY_RELEASE_PRE_BARRIER
    if the persisted phase is PRE_BARRIER, its attempt pair and continuation
       owner are null, and one checked state-only release is required
  | RETAIN_LIVE_CONTINUATION
    if a different checked ChildlessContinuationOwnerV1 can still resume
  | RETAIN_EFFECT_GUARD_UNAVAILABLE
    if the invocation cannot acquire the exclusive effect guard, or a
       different persisted predecessor exists and cannot be proved unable
       to resume
  | MAY_TAKEOVER
    if this invocation owns the effect guard, the exact persisted predecessor
       is positively unable to resume, and the checked revision still matches
  | MAY_RECONCILE
    if this invocation owns the effect guard and exact checked continuation
  | RETAIN_INVALID_FENCE
    otherwise
```

`RETAIN_DRY_RUN` performs no closure-successor call and no persistence.
`RETAIN_STATE_PROVENANCE_LOST` performs no cleanup that depends on erased
attempt/debt provenance.
`RETAIN_NO_CURRENT_SUPERVISOR` performs no closure-successor call and no state mutation
because the invocation does not own the executor claim.
`MAY_RELEASE_PRE_BARRIER` needs no effect guard because no external operation
or attempt pair has been armed. It permits one state-only #115 CAS that releases
the reservation, writes `BARRIER_VETOED` and the same-poll terminal, and
performs no closure-successor call, teardown, launch, marker consumption, attempt/cycle
mutation, or debt clear.
`RETAIN_LIVE_CONTINUATION` and `RETAIN_EFFECT_GUARD_UNAVAILABLE` close both
commit/effect gaps without inferring that an external call did or did not
occur. `RETAIN_INVALID_FENCE` makes the constructor total for an unmatched or
malformed phase/owner pairing; the earlier invalid-state event remains
`POLICY_HELD`. Every retain variant preserves the exact reservation, closure,
pending disposition, debt/current attempt, cycle, continuation owner, and
terminal.
`MAY_TAKEOVER` authorizes no external call. It permits exactly one #115 CAS
that compares the full old continuation owner and state revision, replaces
its supervisor/continuation fields with this guard holder, sets
`role=RECONCILER`, preserves the exact attempt/closure/debt/cycle, and applies
exactly one phase mapping:

- acquiring, at either stage, stays acquiring and becomes
  `CLOSURE_RECONCILE/ARMED`;
- held becomes `TREE_CLOSURE_RELEASING/CLOSURE_VETOED` with
  `CLOSURE_RELEASE/ARMED`;
- releasing preserves its pending disposition and becomes
  `CLOSURE_RECONCILE/ARMED`;
- teardown at `STOP_TREE/ARMED` becomes
  `TREE_CLOSURE_RELEASING/EFFECT_UNPROVEN` with
  `CLOSURE_RELEASE/ARMED`; and
- teardown at `STOP_TREE/CALL_RETURNED` remains teardown with the returned
  effect fact and exact `STOP_TREE/CALL_RETURNED`.

Each successor writes the new `armed_state_revision`; a disabled action latch
uses null only on the new non-destructive reconcile/release operation. A CAS
loser reloads the gate. The winner must recompute as `MAY_RECONCILE`; it cannot
continue directly from `MAY_TAKEOVER`.
`MAY_RECONCILE` authorizes only checked non-destructive cleanup of an
already-persisted named external-effect phase under the effect guard: call
closure-successor reconcile/release by persisted attempt/closure IDs, capture post-action
or residual evidence after an already-issued teardown, and finalize the
persisted disposition. Before each closure-successor operation it commits its exact new
`ChildlessContinuationOwnerV1`; it retains the guard through the returned
result CAS. The `STOP_TREE/CALL_RETURNED` read-only capture instead retains
that exact owner until its atomic transition to releasing. It may do so while
kill switch, action latch, report membership, or auto-restart blocks new
recovery, because it cannot acquire a new closure, reserve/increment an
attempt, invoke `Stop-Tree`, launch, consume a marker, or change generic
backoff/readiness. This is fence cleanup, not teardown authority. Unknown
cleanup evidence retains every fence.

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
| Quarantine is `UNRESOLVED` and the exact checked `ProvablyDifferentPhysicalOwnerV1` clear predicate succeeds | Atomically retire quarantine, install the already-present replacement owner, clear unknowable old debt/cycle/execution fences only because the complete extinction proof makes them physically inactionable, and write the terminal. Perform no OS action and prohibit launch for this poll. | `NOT_ATTEMPTED` | Quarantine-retired audit fact; childless codes recompute on the next ordinary poll. |
| `StateLossQuarantineV1` is `UNRESOLVED` | Retain quarantine and every recoverable fence; deny every kill, launch, closure acquisition, attempt/debt mutation, identity commit, and manual override. | `POLICY_HELD` | `CHILDLESS_STATE_PROVENANCE_LOST` continuously; any recoverable debt/cycle codes also remain visible. |
| Structurally invalid persisted state, malformed #120 snapshot, or malformed closure-successor value, including a post-reservation claim of structural `CAPABILITY_UNAVAILABLE` | No destructive mutation; retain every owned fence. If an exact known closure exists and the safety-reconciliation gate is `MAY_RECONCILE`, only its non-destructive release/reconciliation may proceed. Do not finalize `CLOSURE_VETOED`, retry, or exhaust. | `POLICY_HELD` | `CAPABILITY_UNAVAILABLE` for the structural claim, incomplete code otherwise; plus debt/cycle codes by predicate; pending a human. |
| Well-formed owner/child/tree/debt proof is incomplete before reservation | No reservation and no attempt consumed. | `POLICY_HELD` | Incomplete code; plus debt/cycle codes by predicate. |
| `ClosureCapabilityV1` is `CAPABILITY_UNAVAILABLE` before reservation | Static capability refusal: create no reservation or continuation, consume no attempt, and make no external call. | `POLICY_HELD` | `CAPABILITY_UNAVAILABLE` continuously pending a human; plus debt/cycle codes by predicate. |
| `ExecutionEligibilityV1 == ELIGIBLE`, `recovery_execution == IDLE`, no named reservation/closure/pending disposition/current attempt exists, and ordinary observation-only debt reconciliation is `COMPLETE_GONE` | Clear debt and old-owner cycle, remain `IDLE`, write terminal; construct no authority and do not launch. | `NOT_ATTEMPTED` | No childless code after the atomic clear. |
| No in-flight childless phase and a global/policy gate holds or no named candidate exists; cycle is `NONE` | No module mutation or terminal write. | `NOT_ATTEMPTED` | Debt/incomplete code only when its predicate independently applies. |
| No in-flight childless phase and a global/policy gate holds or no named candidate exists; cycle is `ACTIVE` with a prior typed failure | Preserve the cycle; no attempt, backoff, or terminal mutation. | `NOT_ATTEMPTED` | Active code; debt/incomplete code by predicate. |
| No in-flight childless phase and a global/policy gate holds or no otherwise-eligible automatic named proof exists; cycle is `EXHAUSTED` | Preserve the cycle; no attempt, backoff, or terminal mutation. | `NOT_ATTEMPTED` | Exhausted code; debt/incomplete code by predicate. |
| Automatic named proof is otherwise eligible, no eligible manual origin wins, and its cycle was already `EXHAUSTED` before this poll | No reservation, attempt, backoff, or terminal mutation. | `AUTOMATIC_RETRY_EXHAUSTED` | Exhausted code; debt code if applicable. |
| An existing external-effect childless phase has gate `MAY_TAKEOVER` | Apply exactly the no-call phase mapping above in one CAS, then reload state and recompute the gate/table while retaining the effect guard. A CAS loser reloads without mutation. | No terminal module result yet. | Existing debt/cycle/incomplete codes by predicate. |
| An existing external-effect childless phase has any `RETAIN_*` safety-reconciliation gate | Retain reservation/phase, closure, pending disposition, debt/current attempt, cycle, continuation owner, and terminal byte-identically; perform no closure-successor call. | `POLICY_HELD` | Incomplete code; plus debt/cycle codes by predicate. |
| A named `PRE_BARRIER` reservation is vetoed or reloaded before closure acquisition begins and its gate is `MAY_RELEASE_PRE_BARRIER` | Release that reservation by the state-only CAS, consume no automatic attempt, preserve debt/cycle and marker semantics, and write the terminal. | `BARRIER_VETOED` | Debt/cycle/incomplete code by predicate. |
| Live acquisition returns a well-formed `HELD` and the full joined evidence is valid | Persist `TREE_CLOSURE_HELD`; no outcome or terminal is finalized. | No terminal module result yet. | Codes from preexisting debt/cycle only. |
| Any well-formed matching `HELD` returned by acquisition/reconciliation is not action-ready, or recaptured evidence/gates make a persisted `TREE_CLOSURE_HELD` non-action-ready | Bind/retain its closure ID, persist `TREE_CLOSURE_RELEASING/CLOSURE_VETOED`, and request release; no outcome is finalized yet. The earlier live-valid row alone may continue toward teardown. | No terminal module result yet. | Incomplete code while release remains held; debt code if applicable. |
| `TEARDOWN_IN_FLIGHT` has `STOP_TREE/CALL_RETURNED` and the same checked continuation chain owns the effect guard | Reserve the next nonzero capture ordinal, take the fresh post-action observation, persist its mapped releasing disposition, and update the same continuation ID to exact closure release; no outcome is finalized yet. | No terminal module result yet. | Debt code; incomplete also when the observation is incomplete. |
| `TREE_CLOSURE_RELEASING` reconciliation remains matching `HELD`, or any applicable acquisition/reconciliation is `UNKNOWN` | Retain phase, reservation, pending disposition, debt/current attempt, and automatic `ISSUED`; no terminal write. | `POLICY_HELD` | Incomplete code; debt code if applicable. |
| Acquisition returns no closure and matching reconciliation proves terminal `NEVER_ACQUIRED`; acquiring reconciliation proves matching `RELEASED` and binds its returned closure ID; held-phase reconciliation proves matching `RELEASED` after atomically recording pending `CLOSURE_VETOED`; or any persisted `TREE_CLOSURE_RELEASING/CLOSURE_VETOED` reaches exact `RELEASED` | Finalize `CLOSURE_VETOED`; retire the attempt in the same transaction; debt unchanged. A later unexpected `HELD` for that retired attempt is release-only. Manual origin always leaves the cycle byte-identical; automatic issued-attempt count 1–2 becomes `ACTIVE/CLOSURE_VETOED`; count 3 becomes `EXHAUSTED/CLOSURE_VETOED`. | Manual origin: `BARRIER_VETOED`; automatic count 1–2: `BARRIER_VETOED`; automatic count 3: `AUTOMATIC_RETRY_EXHAUSTED`. | Cycle code by resulting predicate; debt code if applicable. |
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
either the zero-attempt pre-reservation row or a malformed/unknown retained
fence pending a human, and it never reaches automatic retry/exhaustion.
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
| Begin closure acquisition | Acquire the effect guard; live-recompute the reserved bindings; persist `TREE_CLOSURE_ACQUIRING` with a fresh attempt ID/revision and exact `CLOSURE_ACQUIRE/ARMED` continuation; recheck ownership; synchronously invoke the closure successor; and retain the guard through the returned-result CAS. Automatic origin creates/increments `ACTIVE/ISSUED`; manual origin does not change the cycle. |
| Acquire valid closure | Require the full joined live-basis/capture/target equality, then persist `TREE_CLOSURE_HELD`, the exact closure ID, and the acquire/reconcile owner at `CALL_RETURNED` while retaining the effect guard. The same checked live chain must next replace it atomically with teardown arm or release arm. Do not increment the attempt again. |
| Veto after `HELD` | Under the effect guard, persist `TREE_CLOSURE_RELEASING/CLOSURE_VETOED` and exact `CLOSURE_RELEASE/ARMED`; request release by exact pair; persist `CALL_RETURNED`; and finalize only after matching `RELEASED`. |
| Arm teardown | Under the effect guard, atomically enter `TEARDOWN_IN_FLIGHT`, create/update origin-neutral debt, and persist exact `STOP_TREE/ARMED`. Recheck owner, call only `Stop-Tree`, and persist `CALL_RETURNED` before any observation. |
| Observe action effect | Require `STOP_TREE/CALL_RETURNED`, allocate the next checked nonzero capture ordinal, map the closed observation, and atomically enter `TREE_CLOSURE_RELEASING` with the same continuation ID updated to `CLOSURE_RELEASE/ARMED`, without clearing debt/current attempt or cycle `ISSUED`. |
| Finalize release | Retain the effect guard through matching release/result CAS, apply the pending-disposition rule, retire the attempt, and only then clear the continuation owner and release the guard. |
| New guarded owner commits | Permitted only with quarantine `NONE`, debt `NONE`, no childless closure phase/continuation owner/pending disposition, and either clean genesis or the complete provably-different-owner transition. Clear a debt-free old-owner cycle; leave no inherited authority. |

Crash/reload is equally closed. Every retain gate uses the explicit
retained-fence row above. `MAY_TAKEOVER` first owns the effect guard, proves a
persisted predecessor unable to resume, and commits the no-call takeover
mapping; only after recomputation as `MAY_RECONCILE` may these operations run:

- `TREE_CLOSURE_ACQUIRING`: write `CLOSURE_RECONCILE/ARMED`, reconcile by
  attempt ID, and retain the guard through the result CAS. Matching
  `NEVER_ACQUIRED` terminally retires the attempt and finalizes
  `CLOSURE_VETOED`; a later unexpected `HELD` is release-only. Matching
  `RELEASED` binds its returned closure ID, retires the attempt, and finalizes
  the same veto. Matching `HELD` is persisted as releasing/vetoed and released
  without termination. `UNKNOWN` retains state and the retired set is
  unchanged.
- `TREE_CLOSURE_HELD`: do not terminate. Under the guard, persist
  releasing/`CLOSURE_VETOED` with `CLOSURE_RELEASE/ARMED`; matching `RELEASED`
  may then finalize directly, while matching `HELD` requires exact idempotent
  release and a later matching `RELEASED`. `UNKNOWN` retains the held state.
- `TREE_CLOSURE_RELEASING`: use checked `CLOSURE_RECONCILE` or
  `CLOSURE_RELEASE` ownership for each exact synchronous call and finalize the
  already persisted disposition only on matching `RELEASED`.
- `TEARDOWN_IN_FLIGHT` with `STOP_TREE/ARMED`: never reissue `Stop-Tree`, never
  infer that it did or did not run, and never infer action completion. After
  predecessor-death proof, persist `EFFECT_UNPROVEN`, retain debt, and release
  any exact closure under a takeover continuation. No branch launches.
- `TEARDOWN_IN_FLIGHT` with `STOP_TREE/CALL_RETURNED`: a takeover may write
  a new `STOP_TREE/CALL_RETURNED/RECONCILER` owner while preserving the returned
  effect fact. After the gate recomputes `MAY_RECONCILE`, it may obtain a fresh
  post-action observation under matching `HELD`, then atomically persist the
  mapped releasing disposition with `CLOSURE_RELEASE/ARMED`. If
  reconciliation instead proves matching `RELEASED`, obtain a fresh
  `OwnedDebtResidualObservationV1` without assuming frozen membership:
  `COMPLETE_GONE` clears debt/cycle into `IDLE`; `COMPLETE_RESIDUAL` or
  `INCOMPLETE` finalizes the mapped failure into `IDLE`. No reload branch
  launches. `UNKNOWN` retains reservation, debt/current attempt, and cycle
  `ISSUED`; `NEVER_ACQUIRED` is invalid after debt and retains the fence.

Every reload post-action or residual capture atomically reserves the current
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
    Independently cross every `ClosureCapabilityV1` reason before reservation:
    require zero reservation, attempt, continuation, external call, teardown,
    retry, and exhaustion; emit continuous `CAPABILITY_UNAVAILABLE` and remain
    `POLICY_HELD` pending a human. Inject an illegal post-reservation structural
    unavailability claim and reconciliation
    `UNKNOWN(CAPABILITY_UNAVAILABLE)`; both must retain their exact fences,
    avoid `CLOSURE_VETOED`/retry/exhaustion, and require task visibility.
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
    no closure-successor call; a current supervisor under kill switch, disabled action
    latch, missing report membership, or disabled auto-restart may only
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
    authority ownership is released. Reload `PRE_BARRIER` through its
    state-only release without an attempt owner. For every takeover phase,
    assert the exact no-call mapping, recomputation to `MAY_RECONCILE`, and
    rejection of `STOP_TREE/ARMED/RECONCILER`.
17. Lose checked state after automatic attempt one, two, and three, after debt
    arm, and after a partial leaves-first target attempt. A new epoch and the
    same physical PID/start/nonce/generation must remain
    `STATE_PROVENANCE_LOST`, with no fresh budget, debt clear, kill, or launch.
    Clear only by a distinct physical owner plus complete
    old-owner/all-residual extinction. A structurally valid one-generation-old
    backup, unknown prior owner, or unprovable rootless residual must leave
    quarantine indefinitely.
18. Enter `SPAWN_IN_FLIGHT` only with `spawned_guard=null`. Inject a valid
    returned guard and require an atomic identity commit directly to `IDLE`;
    inject ambiguous output and require `AMBIGUOUS_LAUNCH`. Reload must never
    accept a standalone valid-guard `SPAWN_IN_FLIGHT`.
19. Cross manual and automatic origin with strict child-death evidence,
    physical wrapper absence `CONFIRMED`, and debt `NONE`: both must select
    no-kill `RELAUNCH_ONLY`. With outstanding debt, both must hold or select
    exact `DEBT_COMPLETION`; with a present childless wrapper, both must require
    the named `INITIAL` authority before any kill.
20. Recompute every row of the chained module digest vector from the exact
    displayed payload bytes and expected upstream digest. Change one upstream
    byte at each stage and prove every dependent identifier changes.
21. Race two reload reconcilers reserving nonordinary capture ordinals.
    Require distinct CAS-allocated values greater than zero at the current
    ordinary sequence, exact attempt binding, and no reuse/wrap. Pair an
    ordinal-zero ordinary residual with a nonzero reload residual and prove
    each is accepted only in its stated context.
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
    Admit a
    non-live ancestry bridge only from an exact prior complete
    generation/nonce/parent chain, exclude it from target tuples/digests and
    `Stop-Tree`, and normalize a live child behind it to the stated orphan
    form. Validate but exclude role/discovery metadata from authority. Prove an
    openable exact-matching planned Windows target is verified and terminated
    through one native handle, and every successful termination receives a wait
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

## Dependencies and release order

| Property | Classification | Owner |
| --- | --- | --- |
| Two same-owner post-establishment complete child absences | #120 INPUT DELIVERED; ENFORCED after #115 and the 87-A adapter | 87-A reducer plus merged #120 snapshot |
| PID/start/nonce ownership; never pattern ownership | #120 INPUT DELIVERED; ENFORCED by the 87-A adapter | Adapter over merged `owned_process_tree_v2`, including exact Windows FILETIME |
| Complete 64-entry tree observation | DELIVERED by merged #120; 87-A adapter validation remains required | #120 snapshot plus 87-A adapter |
| Action-scoped creation closure | CURRENTLY UNAVAILABLE; ENFORCED after a conforming closure successor | Merged #120 does not freeze creation or expose attempt-keyed acquire/reconcile/release; otherwise `CAPABILITY_UNAVAILABLE` and `POLICY_HELD` pending a human |
| Atomic reservation/debt/cycle/terminal state | ENFORCED after #115 | Task #115 checked state owner |
| External-call continuation/effect linearization | PARTIAL target-local primitive DELIVERED by #120; full contract ENFORCED after #115 and the closure successor | #120 same-handle exact check/terminate plus conditional bounded wait attempt, checked continuation state, and successor-owned attempt-bound synchronous adapters |
| Fail-closed state-loss quarantine | ENFORCED after #115 | Task #115 checked state owner |
| No daemon, persistence plane, durable helper or OS object, or runtime dependency | DECIDED ABSOLUTE by operator on 2026-07-31 (M5 Option A) | Project/package boundary; no mechanism-specific exception |
| Sole leaves-first guarded kill | ENFORCED; exact owned-target seam DELIVERED by #120 | Same-handle exact FILETIME check/terminate and conditional bounded wait attempt in existing `Stop-Tree` |
| Three-attempt automatic cap and continuous typed attention | ENFORCED after #115 | 87-A state/output |
| Durable human delivery and receipt | STATED out of scope | Future 87-B |

Task #78 consumes the named authority only after #115, the adapter over merged
#120, and the closure successor. Task #116 remains blocked only on #115 and
independently stageable: an already-absent wrapper needs neither a target tree
nor the closure successor. This preserves the
task #94 ordering and #107's single contained kill site.
