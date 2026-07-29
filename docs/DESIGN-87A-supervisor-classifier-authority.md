# Design 87-A: Supervisor classifier and recovery-authority totality

**Status:** Proposed, Revision 3 after the consolidated Tier-3 fold; design only.
Nothing in this document is implemented yet.

**Mode:** Reference.

**Audience:** Contributors and reviewers implementing the supervisor classifier,
recovery-authority combiner, shared process observer, and their tests.

**Goal:** Given the same captured evidence, two conforming implementations must
choose the same presence state, teardown origin, replacement proof, escalation
predicate, and final action.

Revision 2 (`20fa5f238826d7dafa334e6589b6e0392bbe37af`) attempted
to specify classifier totality, incident delivery, and migration in one 1,393-line
document. Revision 3 replaces that monolith with three documents. This document
is 87-A only. 87-B and 87-C do not yet exist.

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
| 87-A, this document | Classifiers, targetability, authority equations, origin selection, physical-absence confirmation, semantic condition fingerprint, and final-barrier invariants. | Incident persistence/delivery and rollout. |
| 87-B, future | Incident, promise, projection, and delivery contract. It consumes `escalation_required` and `RecoveryConditionFingerprintV1` from 87-A. | Classifier authority and migration. |
| 87-C, future | Activation, compatibility, migration, rollback, and flag-day procedure after 87-A and 87-B are closed. | Classifier and incident semantics. |

**STATED dependencies and ordering:**

- Task #114 owns the current cold-start kill-switch exit that precedes instance
  claim. 87-B depends on that task instead of redesigning it.
- Task #115 owns the missing linearizable supervisor-state read-modify-write
  lock/API. 87-A specifies a pure absence reducer, but durable poll identity and
  one-use consumption cannot ship until task #115 supplies that mechanism.
- Task #116 owns earlier recovery from twice-confirmed physical absence. It must
  remain independently stageable and must not wait for the rest of Design 87.
- The task #81 recovery umbrella preceding Design 87 is consistent with the
  task #94 umbrella-first release policy. 87-A does not challenge that order.

**STATED non-goals:** 87-A does not specify notification routes, human receipt,
incident retention, state-extension compatibility, executor capability
activation, migration, rollback, wrapper-writer generations, or a rollout
runbook. Those concerns must not be reintroduced here.

## Closed inputs that remain closed

**ENFORCED by independent constructors and the dominant-projection matrix:**
`RuntimeObservation` is derived without heartbeat or process-snapshot evidence.
`WrapperPresenceResult` is derived from one process observation without runtime
health or heartbeat. The two values are crossed only after both exist. This
preserves the closed F1 independence result.

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

The states retain Revision 2's meanings:

| State group | Members | Meaning |
| --- | --- | --- |
| Positive current contract | `CURRENT_PROGRESS_HEALTHY`, `CURRENT_STALE_RECOVERABLE`, `CURRENT_TEARDOWN_PROOF` | Strict current-schema predicates supply health, stale eligibility, or heartbeat-independent teardown proof. |
| Current uncertainty | `CURRENT_UNKNOWN_SEQUENCE_REGRESSION`, `CURRENT_UNKNOWN_BINDING`, `CURRENT_UNKNOWN_STARTING_OVERRUN`, `CURRENT_UNKNOWN_ACTIVE_CHILD`, `CURRENT_BLOCKED_STALL`, `CURRENT_UNKNOWN_OTHER` | Current evidence cannot safely prove teardown. |
| Contract degradation | `CONTRACT_ABSENT`, `UNSUPPORTED_CONTRACT`, `INVALID_CONTRACT` | No current contract may supply health, identity, or teardown authority. |

Runtime-to-managed-wrapper binding uses only the strict runtime record and
supervisor-owned managed identity: agent/root, wrapper generation, launcher
nonce, and guarded recorded PID/start identity. Snapshot evidence never
participates in this constructor.

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
}
```

`ObserverFailureV1` is the closed ordered enum
`SNAPSHOT_UNAVAILABLE`, `COVERAGE_INCOMPLETE`,
`OBSERVATION_INCONSISTENT`. `coverage` is non-null only when both required
coverage channels are complete. Failure reasons are deduplicated in that order.

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

### Manual marker disposition and overlap

**ENFORCED by one pre-combiner marker classifier:**

```text
ManualMarkerDispositionV1 =
  ABSENT
  | PENDING_AUTHORIZED
  | INVALID_OR_UNAUTHORIZED
  | PROTECTION_ACK_REQUIRED
  | COOLDOWN_HELD
  | CONSUMED
```

The classifier evaluates this exact precedence:

1. no marker file is `ABSENT`;
2. a present but malformed/incomplete marker, invalid request ID, or missing,
   mismatched, or expired requester/authorizer evidence is
   `INVALID_OR_UNAUTHORIZED`;
3. missing or expired protected-agent force authorization is
   `PROTECTION_ACK_REQUIRED`;
4. an otherwise valid request ID already in the consumed set is `CONSUMED`;
5. missing live-protected-kill acknowledgement is
   `PROTECTION_ACK_REQUIRED`;
6. an unexpired restart cooldown is `COOLDOWN_HELD`; and
7. the remaining valid marker is `PENDING_AUTHORIZED`.

A pending manual marker is highest authority priority, preserving the current
explicit-request precedence at `src/agenttalk/supervisor.py:4422-4479`:

| Marker disposition | Authority/origin rule |
| --- | --- |
| `PENDING_AUTHORIZED` and manual targetability/protection requirements pass | Derive manual teardown or manual no-kill timing; select `MANUAL_AUTHORIZED`. |
| `PENDING_AUTHORIZED` but a manual-specific safety or policy gate fails | `HOLD` under manual origin; do not fall through to automatic teardown in the same poll. |
| `INVALID_OR_UNAUTHORIZED`, `PROTECTION_ACK_REQUIRED`, or `COOLDOWN_HELD` | Visible refusal/hold; do not fall through to automatic teardown in the same poll. |
| `ABSENT` | Automatic authority may be selected. |
| `CONSUMED` | No manual bypass remains; ordinary automatic classification may proceed, matching current fall-through behavior. |

Manual teardown still requires `TargetabilityProofV1.COMPLETE`, nonempty exact
targets, existing request/authority validation, protected-agent force
authorization, and live-kill acknowledgement where applicable. A marker never
changes presence or targetability.

If both manual and automatic teardown candidates are allowed:

```text
selected_teardown = manual_teardown
origin = MANUAL_AUTHORIZED
```

Exactly one teardown is emitted. `CONDITIONAL_POST_TEARDOWN` binds to that
manual authority ID; automatic evidence remains diagnostic only. Derivation or
iteration order cannot change origin.

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

Escalation is an independent boolean output. 87-B decides how to persist and
deliver it. Recovery policy may narrow an action to `HOLD`; it cannot create
teardown/replacement authority or change mandatory escalation to false.

**ENFORCED manual-origin gates:** Manual origin retains current
configuration/stand-down override, automatic-backoff bypass, readiness reset,
protected-agent authorization, and restart cooldown. Both origins still require
targetability or confirmed absence and the fresh final barrier. Automatic
origin retains configuration, lead-loop, backoff, readiness, and protection
holds.

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

## Final launch barrier

**ENFORCED by execution order:** Every actual launch follows:

1. select authority and apply policy gates;
2. atomically reserve the launch and consume any
   `AbsenceConfirmationV1`;
3. run one fresh capture through the shared observer;
4. require that capture to classify `ABSENT`; and
5. only then call `Start-Process`.

For `CONDITIONAL_POST_TEARDOWN`, the fresh post-teardown capture is also the
final barrier. A survivor, unavailable/incomplete capture, or ambiguous
candidate resolves the conditional to `NONE`.

The barrier never turns a survivor into a target. A veto preserves a pending
manual marker, leaves any no-kill confirmation consumed, and requires a new
two-poll confirmation before another no-kill launch attempt.

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
physical absence or targetability.

## Accepted residual

**STATED residual, not fixed by 87-A:** Raw active-child discovery may still
flap. With presence `PRESENT_TARGETABLE`, a stale poll whose dominant runtime
state is `CURRENT_PROGRESS_HEALTHY` does not escalate; a stale poll at the same
presence that lands in `CURRENT_UNKNOWN_ACTIVE_CHILD` does. (`UNKNOWN` presence
already escalates in both rows.) A later ambiguous poll converges to mandatory
escalation, so this is nondeterministic latency in which poll escalates and
continued display churn, not widened kill authority or a silent-forever
terminal state. 87-A supplies no wall-clock or poll-count bound for when raw
discovery will next observe the ambiguity.

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
4. Prove heartbeat and process-snapshot permutations cannot change
   `RuntimeObservationV1`; prove runtime permutations cannot change
   `WrapperPresenceResultV1`.
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
9. Assert invalid, unauthorized, protection-incomplete, and cooldown-held
   manual markers do not fall through to automatic teardown in the same poll;
   assert a consumed marker does.
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
    Candidate permutation and exact duplicates must be invariant; child reason,
    secondary runtime reason, presence reason, and overflow-tail changes must
    change the fingerprint.
15. Prove the planner, post-teardown resolver, and final barrier use the same
    observer recognition and coverage-signature implementation.
16. Prove `CONDITIONAL_POST_TEARDOWN` is never persisted and every resolution
    path returns synchronously to cleared launch permission or `NONE`.
17. Assert unsupported runtime schemas expose only a bounded duplicate-safe
    schema envelope; no future identity, health, target, or authority field is
    salvaged.
18. Cover the accepted stale healthy/unknown-child escalation-latency residual
    without converting either cell into kill authority.

## Mechanism inventory

| Property | Classification | Mechanism |
| --- | --- | --- |
| Runtime/presence independence | ENFORCED | Separate private constructors and permutation tests. |
| One result for overlapping runtime reasons | ENFORCED | Ranked tuple plus dominant-only authority operands. |
| Total mixed-candidate presence | ENFORCED | Ordered aggregation and closed reason codes. |
| No partial-target kill | ENFORCED | `TargetabilityProofV1.COMPLETE` bijection invariant. |
| One origin for simultaneous manual/automatic authority | ENFORCED | Manual-priority marker classifier and selector. |
| Physical proof independent from timing | ENFORCED | Separate closed values and reducers. |
| Two independent compatible absence polls | ENFORCED after task #115 | Durable capture IDs, exact coverage equality, and adjacent poll sequence. |
| Absence proof is one use | ENFORCED after task #115 | Atomic `CONFIRMED -> CONSUMED` launch reservation. |
| Failed post-teardown scan contributes no counter | ENFORCED | Separate action-scoped reducer path. |
| Stable semantic condition equivalence | ENFORCED | Versioned canonical fingerprint and fixed vectors. |
| No launch on stale proof or observer disagreement | ENFORCED | Shared final barrier after reservation. |
| Earlier fresh-but-confirmed-absent recovery | STATED out of scope | Task #116. |
| Durable incident visibility/delivery | STATED out of scope | Future 87-B, dependent on tasks #114/#115. |
| Migration and rollback | STATED out of scope | Future 87-C after 87-A/87-B. |
| Raw discovery stops flapping | STATED not promised | Process-discovery behavior is unchanged. |

This document is sufficient to implement and review 87-A's pure classifier and
authority substrate. It is not permission to activate the behavior, and it
makes no delivery or migration promise.
