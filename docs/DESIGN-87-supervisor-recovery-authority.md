# Design 87: Separate supervisor teardown, replacement, and escalation authority

**Status:** Proposed, Revision 2 after Tier-3 review; design only. No behavior
described here is shipped yet.

**Audience:** Contributors who maintain the supervisor, wrapper runtime contract,
generated supervisor script, and recovery tests.

This revision stays at the original path because Revision 1 was never committed
or implemented. Keeping one authoritative proposal is safer than leaving two
documents with different safety contracts.

## Revision 2 dispositions

| Panel finding | Disposition | Revision |
| --- | --- | --- |
| F1: observation independence | Folded | Runtime binding uses only the runtime record and supervisor-owned managed identity; snapshot evidence maps only to `WrapperPresence`. Idle and terminal classification no longer consults heartbeat freshness. |
| F2: totality and state ownership | Folded | Runtime uncertainty has a canonical multi-reason order, authority implications are exact, and a pure reducer plus one checked state owner replaces hidden mutation. |
| F3: reachability and absence confirmation | Folded | Current-runtime plus `ABSENT` is normal crash residue. No-kill launch requires two-poll confirmed absence rather than one empty snapshot. |
| F4: temporal replacement proof | Folded | Replacement uses `LaunchProof(NOW_ABSENT_CONFIRMED \| CONDITIONAL_POST_TEARDOWN)`; the conditional value is resolved only after teardown and a fresh barrier. |
| F5: escalation delivery | Folded | Persistence runs after the intentional dry-run exit but before the kill-switch gate, freezes the complete publication identity, uses one canonical state writer, projects incidents into routine operator surfaces, and does not claim human receipt without a validated route. |
| F6: migration, rollback, stageability | Folded | A planner/executor capability handshake, controlled supervisor restart, old-writer-compatible state/event shapes, skew tests, and explicit release boundaries are required. |
| F7: first-launch outage | Folded | A never-launched agent gets a persisted first-managed grace anchor; missing heartbeat becomes stale after that finite grace and chronic snapshot failure must escalate. |
| F8: required corrections | Folded | Rejected designs are narrowed, the rollout flag-day cost and runbook are explicit, ambiguity payloads carry bounded candidate detail, and the gate citation is corrected. |

## Decision

The supervisor must derive three independent authorities:

1. **teardown authority**: permission to stop a process that may contain work; and
2. **replacement authority**: permission to start a wrapper without creating a
   second consumer; and
3. **escalation authority**: an obligation to persist and project an
   unsafe-to-act condition without granting either destructive authority.

Replacement authority is temporal. It is represented by
`LaunchProof(NOW_ABSENT_CONFIRMED | CONDITIONAL_POST_TEARDOWN)`, not a
pre-action boolean. Teardown authority contains a separate
`TargetabilityProof`; permission to stop work never supplies process identity.

An unreadable, absent, unsupported, or unbound wrapper-runtime observation
forfeits **automatic teardown authority**. It does not veto replacement when an
independent process observation positively proves that the wrapper is absent.

This is not another branch before the current `runtime_valid` return. The
planner will first classify all observations, then derive all three authorities
with total functions, and finally combine them in one place. Automatic policy
gates may only narrow an automatic recovery candidate. They cannot suppress a
mandatory escalation. Authorized manual restart keeps its current, explicitly
different override semantics, described below.

The guarantee has two levels and must not conflate them:

- Once the Revision 2 capability is active, every stale ambiguous condition is
  either safely recovered or durably recorded and projected through `status`,
  `doctor`, `attention`, and the web console. It cannot disappear into a silent
  planner `NONE`.
- Human delivery is guaranteed only when `notify_sender` and `notify_to` form a
  validated route. With the package defaults, including the current live
  six-agent configuration where both are null, no design can honestly claim
  that a human receives a push notification. The incident remains an active
  `DELIVERY_UNCONFIGURED` operator item instead.

A deliberate qualification is required for manual restarts. A fully authorized
restart marker remains a separate, explicit teardown authority, as it is today.
It may stop a safely attributed process without a current runtime record.
That does not widen existing authority: the current authorization, protected
agent acknowledgements, start-time target guards, and final launch barrier all
remain mandatory. A marker is permission, not process identity, and cannot turn
an unknown process into a kill target.

## Scope and safety constraints

This design:

- does not widen automatic or manual kill authority;
- uses only the supervisor state, wrapper-runtime file, heartbeat/report, process
  snapshot, and restart marker already available;
- adds no daemon, persistence plane, or runtime dependency
  (`pyproject.toml:13` remains `dependencies = []`);
- preserves launch grace, protection, configuration and lead-loop holds,
  exponential backoff, `consecutive_fails`, readiness give-up, restart
  cooldown, and the one-live-wrapper launch barrier; and
- when the Revision 2 planner, executor, persistence reader, and operator
  projections are capability-active together, guarantees that a stale
  ambiguous agent is either safely recovered or durably visible on routine
  operator surfaces;
- guarantees routed human delivery only when the configured sender and
  recipient pass preflight validation;
- treats `-DryRun` and a legacy executor that did not advertise the Revision 2
  capability as outside that visibility guarantee; and
- accepts a recoverable hold when the alternative is either destroying
  in-flight work or starting a duplicate wrapper.

## Why the current planner loses recovery and escalation authority

The current observation path makes wrapper liveness depend on child-runtime
validity:

- `_wrapped_liveness` returns for an absent or invalid runtime record before it
  examines the process snapshot (`src/agenttalk/supervisor.py:3349-3351`);
- only a valid, bound record can currently yield `wrapper_state=dead` or
  `wrapper_state=alive` (`src/agenttalk/supervisor.py:3379-3408`); and
- `_plan_one` returns `CLI_CHILD_UNKNOWN` for `not runtime_valid`
  (`src/agenttalk/supervisor.py:4496-4509`) before wrapper absence and stale
  heartbeat can enter recovery (`src/agenttalk/supervisor.py:4519-4535`,
  `4807-4935`).

That is one member of a five-member control-flow class. `hb_stale` is available
before classification (`src/agenttalk/supervisor.py:4188-4192`), but every row
below returns before the generic recovery and warning table at `4812`.
`_result` defaults `notify=false` (`4378-4390`), and none of the returns
overrides it:

| Uncertainty class | Current return | Authority correctly refused | Authority accidentally lost |
| --- | --- | --- | --- |
| Runtime contract absent or invalid | `4501-4509` | kill | independent wrapper relaunch and escalation |
| Same-turn sequence regressed | `4510-4518` | kill | independent wrapper relaunch and escalation |
| Wrapper binding neither alive nor dead | `4536-4544` | kill and relaunch | escalation |
| `starting` beyond spawn grace | `4584-4588` | kill and relaunch while the wrapper is present | escalation |
| `active` child state unknown | `4622-4630` | kill and relaunch while the wrapper is present | escalation |

A sixth adjacent epistemic hold has the same observability defect even though
its public state is `CLI_CHILD_STALLED`, not `CLI_CHILD_UNKNOWN`: stale active
progress whose heartbeat/watchdog guards are not authoritative returns
`NONE/notify=false` at `4756-4776`.

The ordering makes “the child lifecycle cannot be verified” erase both the
independent fact “the wrapper is no longer in the process snapshot” and the
obligation to tell an operator why no safe action exists. It also contradicts
the existing terminal-phase rule that a stale wrapper heartbeat is the recovery
signal and a previous turn result must not suppress wrapper recovery
(`src/agenttalk/supervisor.py:4618-4620`).

The failure is mechanically reproducible on `7338d4e`:

```text
runtime absent + authoritative empty snapshot + heartbeat age 230s:
  action=none
  state=CLI_CHILD_UNKNOWN
  kill_first=false
  kill_targets=[]

same process and heartbeat facts + a valid idle runtime record:
  action=stuck_recover
  state=STUCK_OR_DEAD
  kill_first=false
  kill_targets=[]
```

The runtime record changes whether an already absent wrapper may be relaunched,
even though no process needs to be killed in either case. Mechanical stale
reproductions of all five uncertainty roots produce the same silent result:

```text
action=none
state=CLI_CHILD_UNKNOWN
notify=false
kill_first=false
kill_targets=[]
```

## Observation model

Observation and authorization are separate stages. Each observation has a
closed state rather than a nullable collection of fields.

### Runtime observation

`RuntimeObservation` is an immutable closed value over the raw contract and
current lifecycle classifier. It contains a `dominant` table state and a
canonical tuple of every matching runtime reason. It is derived without
consulting heartbeat freshness or the process snapshot.

Runtime-to-wrapper binding compares only the strict runtime record with
supervisor-owned managed identity: agent/root identity, wrapper generation,
launcher nonce, and the guarded PID/start identity already persisted by the
supervisor. A live snapshot does not participate in that comparison. Physical
presence, PID reuse, command-line readability, and target attribution belong
only to the independent `WrapperPresence` result. The two values are crossed
later. This avoids circular heartbeat or process “proof” states and ensures none
of the five early returns can hide inside a generic “no proof” value:

| State | Meaning | May prove child health or teardown? |
| --- | --- | --- |
| `CURRENT_PROGRESS_HEALTHY` | The complete current schema and binding are valid, and positive current evidence such as advancing adapter progress or bounded spawn grace determines the strict verdict without relying on heartbeat staleness. | Health only where the existing current-contract table says so; no teardown. |
| `CURRENT_STALE_RECOVERABLE` | The complete current schema and binding are valid, and the phase/configuration facts make an authoritative stale heartbeat the existing recovery signal. This includes idle, terminal, and a confirmed active stall whose static guards permit heartbeat recovery. The state means “eligible if stale,” not “the heartbeat is stale.” | No teardown by itself; stale heartbeat may complete the proof. |
| `CURRENT_TEARDOWN_PROOF` | The complete current schema and binding are valid, and a heartbeat-independent existing predicate, such as confirmed child death or an authoritative watchdog deadline, proves recovery is due. | Yes, subject to independent targetability. |
| `CURRENT_UNKNOWN_STARTING_OVERRUN` | `starting` remains after bounded spawn grace. | No. |
| `CURRENT_UNKNOWN_ACTIVE_CHILD` | `active` cannot bind a live or dead CLI child. | No. |
| `CURRENT_UNKNOWN_SEQUENCE_REGRESSION` | The same wrapper/turn generation publishes a lower progress sequence or retains the sticky regression latch. | No. |
| `CURRENT_UNKNOWN_BINDING` | A valid current runtime record cannot be bound to the managed wrapper identity. Physical wrapper presence is still classified independently. | No. |
| `CURRENT_BLOCKED_STALL` | Progress is stalled, but heartbeat/watchdog recovery guards are not authoritative. | No. |
| `CURRENT_UNKNOWN_OTHER` | A defensive current-schema tuple is unclassified or internally incoherent. | No. |
| `CONTRACT_ABSENT` | No runtime artifact exists. This is expected for a wrapper started before the contract shipped, but absence alone does not prove why it is missing. | No. |
| `UNSUPPORTED_CONTRACT` | A bounded JSON envelope identifies a schema version this supervisor does not implement. | No. |
| `INVALID_CONTRACT` | The record is malformed, torn, internally incoherent, or fails strict current-schema validation. | No. |

The three positive current states do not create new proof. They partition the
existing strict predicates, including confirmation polls, progress coalescing
allowance, activity-hook and watchdog rules, low `stuck_after_seconds`
protections, and the explicit idle/terminal stale-heartbeat recovery path.
Idle and terminal always contribute `CURRENT_STALE_RECOVERABLE` for both
heartbeat values. Freshness decides whether that eligibility is due; it never
changes the runtime classification.

Overlapping reasons are retained and fingerprinted, not discarded by branch
order. The first matching item in this total order is `dominant`:

1. `CURRENT_UNKNOWN_SEQUENCE_REGRESSION`;
2. `CURRENT_UNKNOWN_BINDING`;
3. `CURRENT_UNKNOWN_STARTING_OVERRUN`;
4. `CURRENT_UNKNOWN_ACTIVE_CHILD`;
5. `CURRENT_BLOCKED_STALL`;
6. `INVALID_CONTRACT`;
7. `UNSUPPORTED_CONTRACT`;
8. `CONTRACT_ABSENT`;
9. `CURRENT_UNKNOWN_OTHER`, which is the last-resort uncertainty state;
10. `CURRENT_TEARDOWN_PROOF`;
11. `CURRENT_STALE_RECOVERABLE`; and
12. `CURRENT_PROGRESS_HEALTHY`.

Specific uncertainty dominates positive recovery proof, while teardown proof
still dominates stale eligibility and positive progress. The semantic
fingerprint hashes the complete ordered reason tuple, not only `dominant`.
Therefore an invalid current read plus a preserved sticky regression has one
stable canonical representation, and adding or removing a secondary reason
changes the fingerprint exactly once.

The current `4536-4544` return must be split by its actual evidence:

- snapshot unavailable (`3379-3381`) yields `WrapperPresence.UNKNOWN`;
- runtime record versus stored managed-identity mismatch (`3385-3390`) yields
  `CURRENT_UNKNOWN_BINDING`;
- PID/start identity ambiguity (`3396-3398`) yields
  `WrapperPresence.UNKNOWN`; and
- a visible wrapper that fails attribution (`3399-3407`) yields
  `WrapperPresence.PRESENT_UNTARGETABLE`.

Only the second cause changes `RuntimeObservation`. A binding reason may still
coexist with any physical-presence value when the independent observer has
other evidence.

### Wrapper liveness

`WrapperPresence` has four states. Presence and safe targetability are distinct
because a process may be visible enough to block a duplicate launch without
being attributable enough to kill:

| State | Required evidence |
| --- | --- |
| `PRESENT_TARGETABLE` | An available process snapshot positively identifies a live wrapper and supplies complete, start-time-guarded kill targets under the existing attribution rules. |
| `PRESENT_UNTARGETABLE` | A live wrapper is positively visible, but ownership evidence is insufficient to kill it safely. A stored-launcher PID without its expected nonce, including a pre-root launch with a guarded recorded identity, is the current example. |
| `ABSENT` | An available process snapshot with complete observation coverage finds no recognized or ambiguous wrapper/wait candidate. When recorded launch identity exists, that guarded identity must also be absent. |
| `UNKNOWN` | The snapshot is unavailable, the recorded identity is ambiguous or reused, command lines cannot be read, or the launch shape cannot be recognized completely. |

“No recognized row” is not sufficient for `ABSENT`. Older launch arguments may
omit `--root`; the current parsers require a root match
(`src/agenttalk/supervisor.py:2067-2068`, `2246-2258`). Such a live wrapper can
be invisible both to kill-target discovery and to the current launch barrier.
The shared observer therefore also performs a root-agnostic ambiguity scan: an
agenttalk `wrap`/`wait` candidate for the same agent with no usable root, or an
unreadable candidate command line, prevents `ABSENT` even though it supplies no
kill target. This may conservatively hold on a rootless same-named wrapper from
another project; it is safer than stacking a second consumer. The executor
normalizes only future launches
(`src/agenttalk/supervisor.py:6088-6128`, `6361-6364`); refreshing a generated
script does not change an already-running command line.

The observer also returns bounded diagnostic candidates. Each candidate may
carry PID, guarded start identity, executable basename, a redacted and
length-bounded structural command-line fragment, and a parse/read failure code.
It must not persist credentials or arbitrary arguments. Escalation payloads
include these candidates, including foreign-root and pre-`--root` collisions,
so a common root-agnostic hold identifies what the operator must inspect.

Therefore a legacy launch shape with no trustworthy recorded PID/start identity
is `UNKNOWN`, not `ABSENT`. The process observer and the final launch barrier
must use the same recognition rules and recorded-identity veto. They must not
maintain two definitions of “wrapper exists.”

### Heartbeat freshness

The authority table has two heartbeat states:

- `FRESH`: within the configured threshold or still inside a finite launch
  grace; and
- `STALE`: stale or missing according to the existing authoritative heartbeat
  rules, after launch grace.

Every configured agent has a persisted `first_managed_epoch` in the Revision 2
top-level state extension. For an agent that has never launched and has no
heartbeat, grace is anchored to that epoch. Missing heartbeat becomes `STALE`
when `first_managed_epoch + launch_grace` expires; it can never remain fresh
forever merely because no launch event exists. A later real launch uses its
normal launch-grace anchor. Losing state may restart this bounded grace, but
after the last loss chronic snapshot failure converges to stale uncertainty and
mandatory escalation.

Staleness is temporal evidence. It never identifies a process and, by itself,
never authorizes a kill or a replacement. It does authorize escalation when
the lifecycle or wrapper observation is uncertain, because reporting “unsafe
to act” does not threaten in-flight work.

## Authority derivation

The implementation should use small immutable values, for example enums and
dataclasses from the standard library:

```text
raw = capture(runtime_file, snapshot, heartbeat, restart_marker)

observations, observation_delta =
  classify(raw, previous_state)

targetability      = derive_targetability(observations)
automatic_teardown = derive_automatic_teardown(observations, targetability)
manual_teardown    = derive_manual_teardown(
  observations, targetability, restart_marker
)
launch_proof       = derive_launch_proof(
  observations, observation_delta, automatic_teardown, manual_teardown
)
escalation         = derive_escalation(observations, previous_state)

intent = combine(
  automatic_teardown,
  manual_teardown,
  launch_proof,
  escalation,
)
plan = apply_existing_policy_gates(intent)

poll_delta = reduce_poll_state(
  previous_state,
  observation_delta,
  intent,
  plan,
)
canonical_state, revision = commit_poll_delta(poll_delta)
```

Observation functions do not return planner actions. Authority functions do not
mutate supervisor state. The escalation derivation reads only the prior
canonical incident state. The combiner is the only function that can construct
the independent recovery and escalation outputs.

`observation_delta` is the sole owner of classifier state transitions:
confirmation-counter resets/increments, preserved runtime high-water, sticky
regression, first-managed grace, and absence confirmation. The pure
`reduce_poll_state` function applies that delta before authority/policy outcome
deltas and returns a field-level `SupervisorStateDelta`; it never performs I/O.

One checked Python state owner applies every poll and action-result delta under
the existing supervisor-state lock and returns the canonical state plus a
monotonic revision. The generated executor may not save a cached whole `$state`,
`$p.next_state`, or an earlier revision after that call. Launch reservation,
record-launch, incident activation/delivery, barrier veto, marker clearing, and
final poll bookkeeping all submit deltas to the same owner. A revision mismatch
reloads and re-reduces or fails the poll closed; it never overwrites a newer
checked mutation. This uses the existing state file and lock, not a new
persistence plane.

### Automatic teardown authority

Automatic teardown is allowed only when all of these are true:

1. the runtime observation is current and bound;
2. an existing strict recovery predicate has produced teardown proof;
3. every target is positively attributed; and
4. every target carries the existing PID start-time guard.

The mechanically testable authority equation is:

```text
strict_recovery_due =
  (
    runtime.dominant is CURRENT_TEARDOWN_PROOF
    or (
      runtime.dominant is CURRENT_STALE_RECOVERABLE
      and heartbeat is STALE
    )
  )
  and runtime.reasons contains no uncertainty reason

automatic_teardown.allowed =
  strict_recovery_due
  and targetability is COMPLETE
  and WrapperPresence is PRESENT_TARGETABLE
  and targets is nonempty
  and every target has positive ownership and PID/start guard
```

Absent, unsupported, invalid, unbound, or otherwise uncertain runtime
observations therefore always produce
`automatic_teardown.allowed=false`. Their observation delta resets child
death/stall confirmation counters while preserving the last complete wrapper
generation, turn generation, progress-sequence high-water, and sticky
regression. One torn read cannot launder a later same-turn sequence regression.
This retains the safety behavior at `src/agenttalk/supervisor.py:4260-4306`,
`4332-4333` without hiding mutation inside authority derivation.

### Manual teardown authority

An authorized restart marker may supply explicit teardown intent, but only
under the existing authorization policy:

- the request and authority fields must validate;
- protected-agent force and live-kill acknowledgement rules still apply;
- target attribution and start-time guards remain mandatory; and
- no process is killed when the target set is empty.

The marker never changes `WrapperPresence`, supplies a missing root identity, or
converts a barrier survivor into a kill target.

The exact implication is:

```text
manual_teardown.allowed =
  restart_marker is valid and authorized
  and protected-agent force/live-kill acknowledgements are satisfied
  and targetability is COMPLETE
  and targets is nonempty
  and every target has positive ownership and PID/start guard
```

A valid marker with an absent process may affect manual launch timing, but it
does not manufacture a teardown authority or target.

### Replacement authority

Replacement authority is a typed temporal capability:

```text
LaunchProof =
  NONE
  | NOW_ABSENT_CONFIRMED
  | CONDITIONAL_POST_TEARDOWN(teardown_authority_id)
```

There are two ways to gain it:

1. **without a kill:** `NOW_ABSENT_CONFIRMED` requires
   `WrapperPresence=ABSENT`, heartbeat `STALE`, expired launch grace, and two
   consecutive available, complete snapshots from distinct polls. Both scans
   must have the same observer-coverage signature, find no recognized or
   ambiguous wrapper/wait candidate, and find any guarded recorded identity
   absent. The first empty scan yields `HOLD / ABSENCE_CONFIRMING`. Present,
   unknown, or changed-coverage observations reset confirmation.
2. **after a kill:** an allowed automatic or manual teardown creates only
   `CONDITIONAL_POST_TEARDOWN(authority_id)`. It does not assert current absence.
   After guarded termination, the executor captures a new snapshot and calls
   `resolve_post_teardown_launch_proof(teardown_result, post_kill_observation)`.
   Only a clear shared-observer barrier converts the conditional capability into
   launch permission.

The guarded termination result plus the fresh post-kill barrier is stronger than
an unrelated single empty poll, so it does not require a second polling-cycle
confirmation. A failed kill, surviving candidate, unavailable snapshot, or
changed observation coverage resolves to `NONE` and no launch.

An authorized manual restart may bypass automatic heartbeat timing and
automatic recovery backoff exactly as current policy permits; restart cooldown
remains in force. It still requires either positive absence or a completed safe
teardown. `WrapperPresence=UNKNOWN` never allows a replacement launch.

The no-kill implication is exact:

```text
launch_proof is NOW_ABSENT_CONFIRMED =>
  WrapperPresence is ABSENT
  and absence_confirmation is CONFIRMED
  and targets is empty
  and kill_first is false
  and kill_orphans is false
  and (
    heartbeat is STALE after grace
    or authorized manual timing bypass applies
  )
```

No-prior-state is not proof of first launch: lost supervisor state is
indistinguishable from a never-launched agent. The current barrier permits
`snapshot_unavailable + no_prior_process`
(`src/agenttalk/supervisor.py:2886-2895`), but this design removes that
exception. Initial launch and replacement both wait for an available snapshot
whose shared wrapper observer reaches confirmed `ABSENT`. A true first launch
can earn confirmed absence without recorded identity only when two complete
scans contain neither a recognized wrapper nor a rootless/unreadable ambiguous
candidate. A transient snapshot failure therefore delays startup instead of
risking a duplicate wrapper after state loss.

Current-runtime plus `ABSENT` is normal, reachable crash residue: a complete
persisted runtime record may still bind to supervisor-owned identity after the
wrapper exits, while the process observer correctly finds no live process.
Every current-runtime x `ABSENT` classifier mapping is required production
coverage, not a defensive incoherence case.

Removing `snapshot_unavailable + no_prior_process` makes an explicit asymmetric
trade. It removes a rare three-condition duplicate-launch race (state loss,
snapshot loss, and an existing wrapper) by accepting a one-condition cold-start
outage whenever snapshot capture is chronically broken. No agent can launch on
that host until observation recovers. The design accepts that availability
loss only because missing heartbeat for a never-launched agent becomes stale
after finite first-managed grace and the resulting
`CONTRACT_ABSENT x UNKNOWN x STALE` condition is a mandatory routine-surface
incident. If those projections are not active, the Revision 2 capability may
not activate; otherwise the outage would again be silent.

A relaunch without a kill has empty `kill_targets`, `kill_first=false`, and
`kill_orphans=false`. Runtime uncertainty cannot be smuggled into a
best-effort kill flag.

### Escalation authority

Escalation is neither a fallback kill nor a recovery action. Its boolean
predicate is explicit:

```text
recovery_blocked =
  (
    CURRENT_TEARDOWN_PROOF
    or (CURRENT_STALE_RECOVERABLE and heartbeat is STALE)
  )
  and WrapperPresence in {PRESENT_UNTARGETABLE, UNKNOWN}

stale_uncertainty =
  heartbeat is STALE and (
    runtime.reasons contains CONTRACT_ABSENT, UNSUPPORTED_CONTRACT,
      INVALID_CONTRACT, CURRENT_UNKNOWN_*, or CURRENT_BLOCKED_STALL
    or WrapperPresence is UNKNOWN
  )

escalation_required = recovery_blocked or stale_uncertainty
```

This equation is both necessary and sufficient for mandatory escalation. It
includes heartbeat-independent recovery blocked by
`PRESENT_UNTARGETABLE`/`UNKNOWN` even when heartbeat is fresh, including the
`CURRENT_TEARDOWN_PROOF` rows. “Degraded runtime” means exactly
`CONTRACT_ABSENT`, `UNSUPPORTED_CONTRACT`, `INVALID_CONTRACT`, every
`CURRENT_UNKNOWN_*` reason, or `CURRENT_BLOCKED_STALL`; it is not an open prose
category.

The exhaustive table below is normative for defensive combinations. A
positive-progress current tuple does not escalate merely because its heartbeat
is stale when another current, authoritative observation still supplies the
strict verdict. A stale-recoverable tuple escalates only when the recovery is
blocked; a safe recovery need not also notify. Wrapper-presence ambiguity still
requires escalation.

The first observation of a semantic escalation fingerprint is due immediately
after grace. Repeated identical conditions use the existing warning interval.
A different reason fingerprint is due immediately rather than being suppressed
by an unrelated prior warning. Rate limiting suppresses duplicate delivery, not
the durable active condition.

The fingerprint is canonical over agent identity, the ordered complete runtime
reason tuple, wrapper-presence reason, bounded ambiguity-candidate evidence,
freshness, and recovery-blocked disposition. It does not depend on incidental
branch order or free-form prose.

Escalation is independent of action. `RELAUNCH_ONLY` may therefore coexist with
an escalation about a degraded runtime contract, and `HOLD` may coexist with a
pending bus delivery. Recovery-policy gates cannot erase escalation authority.
Its predicate domain is deliberately broader than either destructive authority:
it is safe to report uncertainty in states where neither teardown nor
replacement is safe.

### Single combiner

The combiner emits a recovery intent and an escalation intent:

| Authorities | Recovery intent | Escalation intent |
| --- | --- | --- |
| No safe teardown and no safe replacement; no escalation predicate | `HOLD` | `NONE` |
| No safe teardown/replacement; escalation new or interval elapsed | `HOLD` | `DUE` |
| No safe teardown/replacement; same escalation inside interval | `HOLD` | `RATE_LIMITED` or `DELIVERY_PENDING` |
| `NOW_ABSENT_CONFIRMED` permits replacement | `RELAUNCH_ONLY` | independently `NONE`, `DUE`, or pending |
| Teardown is authorized with `CONDITIONAL_POST_TEARDOWN` | `KILL_THEN_RELAUNCH` (launch remains conditional on the post-kill barrier) | independently `NONE`, `DUE`, or pending |

Existing public action and state names may remain unchanged. The important
change is that they are emitted from an intent, not selected by the position of
an early return. `KILL_THEN_RELAUNCH` is an execution protocol, not a claim that
replacement is already proven: teardown may complete while the post-kill
barrier still refuses launch.

The following invariants should be asserted in code and tested:

```text
runtime.reasons intersects {
  CONTRACT_ABSENT,
  UNSUPPORTED_CONTRACT,
  INVALID_CONTRACT,
  CURRENT_UNKNOWN_*,
  CURRENT_BLOCKED_STALL
} => automatic_teardown.allowed is false

automatic_teardown.allowed =>
  strict_recovery_due
  and targetability is COMPLETE
  and WrapperPresence is PRESENT_TARGETABLE
  and nonempty start-guarded targets

manual_teardown.allowed =>
  valid authorized restart marker
  and required protection acknowledgements
  and targetability is COMPLETE
  and nonempty start-guarded targets

WrapperPresence in {
  PRESENT_TARGETABLE,
  PRESENT_UNTARGETABLE,
  UNKNOWN
} => launch_proof is not NOW_ABSENT_CONFIRMED

launch_proof is NOW_ABSENT_CONFIRMED =>
  WrapperPresence is ABSENT
  and absence_confirmation is CONFIRMED
  and no kill flags or targets

launch_proof is CONDITIONAL_POST_TEARDOWN =>
  automatic_teardown.allowed or manual_teardown.allowed

kill_first =>
  (automatic_teardown.allowed or manual_teardown.allowed)
  and nonempty start-guarded targets

RELAUNCH_ONLY =>
  launch_proof is NOW_ABSENT_CONFIRMED
  and no kill flags or targets

every actual launch =>
  launch_proof resolved to current absence
  and a fresh shared-observer barrier reports ABSENT

escalation_required =>
  escalation is DUE, RATE_LIMITED, DELIVERY_PENDING,
    DELIVERY_UNCONFIGURED, or DELIVERED

recovery policy may change an action to HOLD, never HOLD to an action
recovery policy cannot change mandatory escalation to NONE
```

Adding a new runtime phase or classifier branch cannot omit the derived
`launch_proof` or escalation values: it must extend a closed
classifier and the exhaustive matrix before it can produce a plan. Later policy
may still hold recovery execution under the existing caps, but the authority
and escalation dispositions remain explicit and observable. Table-driven tests
fail when a new enum member has no disposition.

### Gate precedence

Intent origin is data, not branch position. `RecoveryIntent` carries
`origin=AUTOMATIC` or `origin=MANUAL_AUTHORIZED`, and a total policy function
applies the existing precedence deliberately:

| Gate | Automatic intent | Authorized manual intent |
| --- | --- | --- |
| Configuration-blocked hold | Hold | Override, as today |
| Lead-loop stood-down/live-owner hold | Hold | Override/re-arm, as today |
| Automatic recovery backoff | Hold until due | Bypass, as today |
| Readiness give-up | Hold | Reset `readiness_fails` and retry, as today |
| Protected-agent authorization | Hold/warn | Require existing force and, when applicable, live-kill acknowledgement |
| Restart cooldown | n/a | Hold until due |
| Safe target attribution or positive absence | Required | Required |
| Fresh shared-observer launch barrier | Required | Required |

This preserves the deliberate current precedence at
`src/agenttalk/supervisor.py:4422-4479`, including the configuration/lead-loop
override. The configuration hold executes at
`src/agenttalk/supervisor.py:4817-4820`; lead-loop decisions execute at
`4837-4849`. Manual authority may override an operational hold; it may not
override evidence needed to avoid a false kill or duplicate launch.
This gate table governs only the recovery intent. Escalation still runs through
its independent persistence, rate-limit, and delivery path whether recovery is
allowed, held, or manually authorized.

## Exhaustive automatic decision table

This table covers all 96
`RuntimeObservation x WrapperPresence x heartbeat freshness` tuples. Action and
escalation are separate outputs. `REQUIRED` means a new fingerprint is emitted
immediately, an identical fingerprint is rate-limited, and any undelivered bus
notice remains pending. It does not mean “send on every poll.”

All current-runtime x `ABSENT` tuples are reachable. The runtime file is crash
residue and may remain complete and bound after its writer exits; an independent
complete snapshot then correctly reports no wrapper. Tests cover those
classifier-to-combiner mappings as production paths. Other genuinely
incoherent defensive tuples remain specified so a torn or future classifier
output fails closed.

The table records base recovery candidates. Every stale `ABSENT` cell that says
`RELAUNCH_ONLY` reaches execution only when `LaunchProof` is
`NOW_ABSENT_CONFIRMED`; after the first empty snapshot it is
`HOLD / ABSENCE_CONFIRMING`. Every `KILL_THEN_RELAUNCH` cell carries
`CONDITIONAL_POST_TEARDOWN` and may terminate after a barrier hold without
launching. These temporal overlays do not add kill authority or change the 96
runtime/presence/freshness dispositions.

Actions are candidates before existing automatic policy gates. A configuration
hold, lead-loop stand-down, protected-agent rule, backoff window, readiness cap,
or final launch barrier may turn an automatic recovery candidate into `HOLD`;
none may create one or turn `REQUIRED` escalation into `NONE`. The
manual-origin policy table above is intentionally different.

`HOLD (strict verdict)` means the current-contract health table still chooses
the non-recovery state for that runtime class and freshness, including
`HEALTHY_IDLE`, `HEALTHY_WORKING`, starting, terminal, or suspect as
applicable. Idle and terminal classify as `CURRENT_STALE_RECOVERABLE` for both
freshness values; fresh holds, while stale completes the recovery predicate.

| Runtime observation | Wrapper presence | Fresh action | Fresh escalation | Stale action | Stale escalation |
| --- | --- | --- | --- | --- | --- |
| `CURRENT_PROGRESS_HEALTHY` | `PRESENT_TARGETABLE` | `HOLD (strict verdict)` | `NONE` | `HOLD (strict verdict)` | `NONE` |
| `CURRENT_PROGRESS_HEALTHY` | `PRESENT_UNTARGETABLE` | `HOLD (strict verdict)` | `NONE` | `HOLD (strict verdict)` | `NONE` |
| `CURRENT_PROGRESS_HEALTHY` | `ABSENT` | `HOLD / WRAPPER_MISSING` | `NONE` | `RELAUNCH_ONLY` | `NONE` |
| `CURRENT_PROGRESS_HEALTHY` | `UNKNOWN` | `HOLD / WRAPPER_UNKNOWN` | `NONE` | `HOLD / WRAPPER_UNKNOWN` | `REQUIRED` |
| `CURRENT_STALE_RECOVERABLE` | `PRESENT_TARGETABLE` | `HOLD (strict verdict)` | `NONE` | `KILL_THEN_RELAUNCH` | `NONE` |
| `CURRENT_STALE_RECOVERABLE` | `PRESENT_UNTARGETABLE` | `HOLD (strict verdict)` | `NONE` | `HOLD / RECOVERY_BLOCKED` | `REQUIRED` |
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

Four consequences deserve emphasis:

- A present legacy wrapper with a stale heartbeat remains a non-green hold,
  even when the process is targetable. The supervisor lacks automatic kill
  authority, but escalation is required.
- An absent legacy wrapper with a stale heartbeat is relaunched without a kill
  and independently escalated because its runtime contract remains degraded.
- A stale `starting` overrun, active-child ambiguity, sticky sequence
  regression, or unknown wrapper binding can no longer return silently.
- A heartbeat-independent teardown proof that cannot be safely targeted
  escalates even with a fresh heartbeat; the operator is the only safe
  resolver.

The five current early returns map directly into this table: runtime
absent/invalid uses the three contract-degradation rows; sequence regression and
the two phase-specific ambiguities have dedicated runtime rows; and an unknown
runtime-to-wrapper binding has its own row crossed with all four physical
presence states. Consequently every one of those classes with a stale
heartbeat has `Stale escalation=REQUIRED`. The adjacent non-authoritative stall
uses `CURRENT_BLOCKED_STALL` and receives the same guarantee.

Fresh heartbeat plus positive absence deliberately waits. It can be a
process-table/heartbeat propagation race. Staleness is not treated as equivalent
to process confirmation: a no-kill launch additionally requires two consecutive
complete absence observations. This prevents a transient snapshot glitch that
happens to coincide with independent heartbeat staleness from authorizing a
duplicate wrapper.

## Escalation delivery and retention

Adding `notify=true` to the old early returns is insufficient. The generated
executor sends a bus note only for selected warning actions
(`src/agenttalk/supervisor.py:6612-6617`); `action=none` falls through the
default state-write branch at `6619`. A relaunch action can also `continue`
before any warning-only notification path.

Escalation must therefore be consumed independently of recovery action and
before recovery policy can return:

1. The planner emits a stable redacted condition code, complete canonical
   fingerprint, bounded ambiguity candidates, and one of `NONE`, `DUE`,
   `RATE_LIMITED`, `DELIVERY_PENDING`, `DELIVERY_UNCONFIGURED`, or `DELIVERED`.
2. `-DryRun` exits intentionally without persistence and is outside the durable
   visibility guarantee. In a normal poll, immediately after that exit and
   **before** the per-action `Assert-ActionsEnabled` gate at current line 6501,
   the executor submits the incident delta to the checked state owner.
3. The state owner persists the activation ID, occurrence counter, fingerprint,
   redacted payload, warning state, resolution state, and delivery state in the
   existing supervisor-state file and returns canonical state/revision. A
   subsequent action branch may submit another delta but may not save the stale
   pre-mutation object.
4. A checked event API records the transition, then the executor prints the
   rate-limited warning. Only a successful checked append permits the warning
   latch to advance. The current `append_supervisor_events` contract, which
   swallows every exception (`src/agenttalk/supervisor.py:1163-1190`), is not
   used for this transition.
5. If a route validates, the state owner atomically freezes a complete
   publication envelope and 32-hex nonce: sender, recipient, kind, body,
   subject if any, complete semantic metadata, payload digest, and operation
   nonce. Every retry reuses those exact fields. Store intent paths are
   sender-specific, so persisting only recipient/payload/nonce is forbidden.
6. Delivery uses a checked idempotent `supervisor_escalation` operation over the
   existing nonce substrate (`src/agenttalk/store.py:3338-3419`), not the plain
   `send` at `src/agenttalk/supervisor.py:6614-6615`. Success or
   already-published reconciliation returns the canonical message ID. A checked
   state delta records that ID and delivery time.
7. Only after steps 2-6 have either completed or left an explicit retryable
   state may the executor evaluate `supervisor.kill`, configuration, backoff, or
   the recovery action switch.
8. Recovery appends a checked resolved transition and marks the activation
   resolved. It never deletes an undelivered incident, its frozen publication
   envelope, or a delivered message reference. A configured resolution notice
   is a separate nonce-bound operation.

### Kill switch and early-exit contract

`supervisor.kill` deliberately suspends kills, launches, marker mutation,
seeding, and other recovery behavior. It does **not** suspend incident
persistence, checked compatible event append, routine operator projection, or
idempotent delivery for an already-derived mandatory escalation. The CLI must
therefore expose a narrow observational exemption for the Revision 2 checked
incident operations while continuing to reject every process/configuration
mutation. The exemption accepts only the claimed supervisor instance identity,
the planner capability/schema, and bounded typed incident fields; it is not a
general kill-switch bypass and is not web-reachable.

No normal-path `continue`, action value, policy hold, or gate may occur between
the dry-run exit and durable incident activation. Capability/schema mismatch
fails the whole Revision 2 path closed before any new action is executed. Tests
must cover the active kill-switch path in addition to `HOLD`,
`RELAUNCH_ONLY`, and `KILL_THEN_RELAUNCH`.

### Existing-plane state and event compatibility

Revision 2 state lives under one versioned **top-level** extension in the
existing `supervisor-state.json`, for example:

```text
recovery_authority_v2 = {
  schema_version,
  revision,
  first_managed_epoch_by_agent,
  absence_confirmation_by_agent,
  incidents_by_agent,
}
```

It must not live inside the closed per-agent `next_state` map. The current
planner reconstructs that map (`src/agenttalk/supervisor.py:4308-4334`) and an
old writer would drop unknown per-agent fields. Existing Python state
validation preserves unknown top-level fields, and the PowerShell load/save
round trip preserves the loaded top-level object. Upgrade/downgrade tests must
prove every old state writer preserves this extension byte-semantically.

The event ring must remain readable and preservable by the old sanitizer.
Revision 2 therefore encodes incident transitions using the existing
`agent_decision` event schema rather than introducing an event kind that an old
append would discard. Reserved action/state tokens identify
activation/resolution, and the bounded `reason_code` contains the transition
plus activation ID. The checked API deduplicates explicitly on
`(agent, activation_id, transition)` under the existing lock. The authoritative
condition and delivery state remain in the top-level extension; the bounded
ring remains an audit projection. A future event schema may replace this bridge
only after the old-writer skew is no longer supported.

### Crash and same-poll behavior

The ordering permits duplicates but never a lost or suppressed incident:

- crash after activation but before event append: the next poll sees the
  missing transition and appends it;
- crash after event append but before warning-latch persistence: the warning may
  repeat, but it is not lost;
- crash after bus publication but before delivery-state persistence: retrying
  the frozen sender and envelope with the same nonce resolves to the same
  canonical message;
- failure of state/event persistence: no warning or delivery latch advances;
  and
- checked mutation followed by recovery action: all later deltas apply to the
  returned canonical revision, so the final poll cannot overwrite the incident
  with stale `$p.next_state`.

This adds no persistence plane. The state extension, compatible event rows, and
operation intents all use existing state/store files and locks.

### Human-route and operator-surface contract

Package defaults are `notify_sender=null` and `notify_to=null`
(`src/agenttalk/supervisor.py:5521-5522`), and the current six-agent project has
that configuration. Supervisor stdout is not a human-delivery surface. Under
that configuration the only honest state is `DELIVERY_UNCONFIGURED`; no bus
message and no claim of human receipt exist.

Routine projection is therefore mandatory before the Revision 2 capability can
activate:

- `status` reads the active incident summary directly from the state extension
  even when it requests `event_limit=0`, including active, resolved-undelivered,
  route state, age, and affected-agent count;
- `doctor` reports a failing or warning diagnostic for active
  `DELIVERY_UNCONFIGURED`/failed delivery and names the configuration repair;
- `attention` derives a stable high-priority item for active and
  resolved-undelivered incidents; and
- the web console mirrors that attention source and its delivery state.

Resolved-but-undelivered incidents remain on all four surfaces until delivery
is proven or an explicit operator retention/disposition policy is recorded.
Configuration preflight validates both route identities before minting a
publication envelope. If no route exists, a later valid configuration may
freeze the first envelope and deliver the retained activation; it must not
silently rewrite an already-frozen sender or recipient.

The manual-restart blocked signal illustrates what not to copy. Its detailed
observation requires two matching blocked polls plus launch grace
(`src/agenttalk/coordination_stall.py:353-412`). At a configured 300-second poll
cadence that is roughly five to ten minutes, depending on phase alignment; the
package default poll interval is 15 seconds
(`src/agenttalk/supervisor.py:70`). A later clear barrier unlinks the observation
(`src/agenttalk/supervisor.py:1391-1406`) before preflight or spawn, while the
barrier event ring's `notify=true` bit has no bus-delivery consumer. The derived
stall is still useful through attention, status, doctor, and web, but this
design does not use that self-erasing file as the escalation record.

## Contract with wrapper failure and dead-letter retry

The wrapper intentionally clears any streaming heartbeat after a failed drive
(`src/agenttalk/wrapper/run.py:2367-2373`). At the retry/escalation backstop it
leaves the message uncommitted and does not stamp again, with the documented
assumption that staleness makes the supervisor restart the wrapper
(`src/agenttalk/wrapper/loop.py:1297-1309`).

That unconditional restart promise cannot be restored for every uncertainty
class without violating this design's safety constraints. A present wrapper in
`starting` overrun, active-child ambiguity, sequence regression, invalid
contract, or unknown binding cannot be killed safely, and a second wrapper
cannot be launched beside it.

The cross-subsystem guarantee is therefore:

```text
after grace, a deliberately stale wrapped agent:
  recovers automatically when teardown or absence proof permits;
  otherwise remains untouched and is durably recorded on routine operator surfaces;
  publishes a human-routed notice only when a validated route exists;
  never disappears into a silent NONE decision.
```

The normal returned-failure path remains automatically recoverable. A returned
failure publishes terminal state and clears the heartbeat; terminal plus stale
already falls through to wrapper recovery
(`src/agenttalk/supervisor.py:4589-4620`). Existing protection, backoff,
readiness, and barrier gates still apply.

Hard wedges that never return from spawn or the active turn restore
**visibility**, not an unsafe automatic restart. During implementation, the
comment and tests at `wrapper/loop.py:1307-1309` must describe the total
“safe recovery or durable operator visibility” contract. If the wrapper
subsystem later requires
an unconditional restart, it must self-exit or publish stronger authenticated
teardown proof; stale heartbeat alone cannot grant supervisor kill authority.
Thus this design restores the dead-letter carve-out as a bounded
recovery-or-visibility backstop, but explicitly qualifies its current
unconditional-restart claim: staleness always reaches safe recovery or durable
operator visibility once Revision 2 is active, not necessarily a new wrapper or
human-routed message.

The wrapper's existing attempt ledger, `_escalate_once`, routed AgentTalk
notice, and doctor check for unrouted escalation remain its durable
message-level backstop. Supervisor escalation complements them with
process-level evidence; it does not replace or clear them.

## Rollout and unknown runtime contracts

Falling back to heartbeat staleness is correct only for the temporal half of an
absence-based replacement decision. It is not a fallback health contract and
does not authorize teardown:

```text
old/unknown runtime + present wrapper + fresh heartbeat => non-green hold
old/unknown runtime + present wrapper + stale heartbeat => hold + escalation
old/unknown runtime + unknown wrapper + fresh heartbeat => hold
old/unknown runtime + unknown wrapper + stale heartbeat => hold + escalation
old/unknown runtime + absent wrapper + fresh heartbeat => hold
old/unknown runtime + absent wrapper + stale heartbeat => relaunch only + escalation
```

The current reader exposes only `valid`, `absent`, and `invalid`
(`src/agenttalk/wrapper_runtime.py:60-62`). It validates the exact closed key set
before rejecting an unsupported schema and then collapses all such errors to
`invalid/malformed` (`src/agenttalk/wrapper_runtime.py:156-176`, `285-329`).
The implementation should make `UNSUPPORTED_CONTRACT` observable by reading
only a bounded, duplicate-key-safe envelope and its `schema_version`.

No identity, lifecycle, health, target, or authority field from an unsupported
record may be salvaged. The bounded envelope read of `schema_version` is the
only exception and can select only `UNSUPPORTED_CONTRACT`; it cannot grant
health, teardown, or launch authority. Unknown keys under the current schema
remain invalid. Existing size, BOM, duplicate-key, encoding, time, and identity
checks remain fail-closed.

Wrapper rollout behavior is:

1. Every already-running pre-contract wrapper becomes
   `COMPATIBILITY_DEGRADED`, never green, on every supervisor upgrade until that
   wrapper restarts or naturally checkpoints the current contract. This is a
   universal flag-day compatibility cost for a long-running fleet, not an
   occasional residual.
2. `status`, `doctor`, `attention`, and web report “N agents pending
   compatibility restart” and list which wrappers are safely attributable.
3. Operators perform a controlled rolling restart only where attribution and
   protection authorization support it. Refreshing generated scripts alone
   upgrades neither a running wrapper nor the running PowerShell executor.
4. If an old wrapper exits or crashes, confirmed absence plus stale heartbeat
   permits replacement without a kill.
5. The replacement must publish a valid current `idle` observation before it
   can earn `HEALTHY_IDLE`.
6. If it publishes an unsupported contract or never reaches readiness, existing
   launch grace, backoff, `consecutive_fails`, and readiness give-up stop churn.
   A stale degraded observation retains an active incident on operator surfaces
   and, when configured, pending bus delivery.

### Planner/executor capability activation

`supervise --refresh-scripts` writes artifacts but a running PowerShell process
does not reload them. The new planner therefore remains behaviorally dormant
unless the caller advertises `recovery-authority-v2` and the generated executor
supplies the matching plan schema/generation. An old executor does not pass that
capability, so a new package returns the legacy plan and cannot silently enable
new recovery without its escalation consumer.

The supported activation runbook is:

1. stop the Scheduled Task (or foreground host) and wait until the old
   supervisor process has exited;
2. install the package containing the complete capability unit;
3. run `agenttalk supervise --refresh-scripts` and validate the generated
   artifact generation;
4. start the Scheduled Task/foreground host;
5. verify that report/plan diagnostics show matching planner and executor
   capability plus all four operator projections; and
6. perform the wrapper rolling-restart runbook while monitoring the pending
   compatibility count.

A parked agent self-heals on the first fully upgraded polls only after stale
heartbeat and `NOW_ABSENT_CONFIRMED` (or after authorized teardown plus a clear
post-kill barrier). `PRESENT_UNTARGETABLE` and `UNKNOWN` remain held and visible
for operator action.

Supported package/executor skews are explicit:

| Running executor | Installed planner | Result |
| --- | --- | --- |
| old | old | Legacy behavior. |
| old | new | New planner emits legacy schema; Revision 2 remains dormant. |
| new | new, matching capability | Revision 2 may activate after state/event/operator preflight. |
| new | old or mismatched generation | Poll fails closed before action; this is not a supported steady state. |

### State migration and rollback

New readers initialize an absent `recovery_authority_v2` extension
conservatively. Missing delivery proof is `PENDING` or
`DELIVERY_UNCONFIGURED`, never `DELIVERED`; missing first-managed and absence
confirmation fields create bounded grace/confirmation state rather than
authority.

Old readers ignore and preserve the top-level extension. Old event appenders
preserve the compatibility `agent_decision` projection because it uses the
existing schema. The build gate must execute every old state writer and event
append against new state, then prove the extension and compatibility rows
survive. It must also execute new readers against old/missing state.

Rollback is controlled, not a claim that an old executor implements Revision 2:

1. stop the new supervisor and wait for exit;
2. while the new package is still present, run a downgrade preflight that
   validates the top-level extension, emits/repairs old-compatible incident
   projections, and reports every pending/undelivered activation;
3. install the old package, refresh old artifacts, and restart the old
   executor; and
4. accept that old behavior does not project or deliver Revision 2 incidents,
   although it must preserve them for a later re-upgrade.

An emergency rollback that skips step 2 still must not erase the extension or
compatible event rows. It temporarily loses the Revision 2 visibility
guarantee and must be reported as such. A package-only downgrade while the new
executor remains running is forbidden.

The wrapper-observability contract also needs migration treatment. A running
wrapper launched before explicit `--root` normalization cannot be proven absent
merely because the current parser finds no row. Its state is `UNKNOWN` until a
guarded recorded identity proves presence/absence or an operator verifies and
stops it. The first new launch establishes both current runtime and
parser-visible process contracts.

## What `request-restart` actually covers

The current manual branch precedes runtime validation
(`src/agenttalk/supervisor.py:4422-4479`), so runtime validity and idle state do
not determine whether it works. Safe process attribution does.

| Condition | Current outcome | Design disposition |
| --- | --- | --- |
| Unprotected wrapper is positively absent | Works: manual `RELAUNCH` has no targets and a clear barrier permits launch. | Preserve. |
| Live wrapper has trusted PID/start/nonce attribution | Works if guarded termination succeeds; the barrier catches a survivor. | Preserve as explicit manual teardown authority. |
| A different live same-root, same-agent wrapper is command-line visible | Works: generic `own_wrapper` discovery can target it (`src/agenttalk/supervisor.py:2676-2691`). | Preserve. |
| Live stored-launcher PID lacks trusted nonce/ownership but its rooted command line is visible | Does not complete: the planner has no target, while the barrier returns `same_agent_wrapper_survived`; the marker is restored and retried. | Return a visible `MANUAL_RESTART_BLOCKED`; do not invent a kill target. |
| Live legacy wrapper lacks explicit `--root` | Unsafe today: neither targeting nor the barrier associates it with this root, so a second wrapper can be launched. | Use `PRESENT_UNTARGETABLE` when guarded recorded identity sees it; otherwise use `UNKNOWN`. Both block launch. |
| Snapshot is unavailable | With prior wrapper state, the barrier holds; without prior state, it currently permits `no_prior_process`. | Always hold until a snapshot is available; missing state is not proof of first launch. |
| Protected agent | The bare command is insufficient; force authorization is required, and a fresh heartbeat also requires the live-kill acknowledgement (`src/agenttalk/supervisor.py:4432-4458`). | Preserve. |
| `auto_restart=false`, agent absent from the report, supervisor stopped, or actions disabled | The marker does not produce a launch; planning covers configured report agents with `auto_restart` (`src/agenttalk/supervisor.py:5138-5169`). | Document; do not advertise an unconditional remedy. |

The rooted-but-untrusted loop is issue 78
(`docs/ISSUES.md:240-258`). It is not caused by idleness. A normal attributable
idle wrapper can be restarted; a live wrapper the planner cannot safely target
cannot.

The existing `MANUAL_RESTART_BLOCKED` projection remains useful corroboration,
but its confirmation delay and self-erasing detailed observation make it
unsuitable as the sole warning or audit record. The independent escalation path
above must record the first stale blocked condition immediately after grace,
regardless of whether the manual marker later succeeds.

The no-root case is more severe. Launcher confirmation requires nonce-backed
identity (`src/agenttalk/supervisor.py:2318-2369`), generic discovery skips the
stored launcher PID (`src/agenttalk/supervisor.py:2676-2691`), and the barrier
currently recognizes only parser-visible same-root invocations
(`src/agenttalk/supervisor.py:2870-2929`). A direct reproduction with a live
legacy no-root wrapper produced a manual `relaunch` with no targets and a
`clear` barrier. The design must close that false-absence path before the
remediation can be called general.

The current text

```text
agenttalk supervise --refresh-scripts
agenttalk request-restart --for <name>
```

is therefore not a generally correct remedy:

- it is correct for a positively absent unprotected wrapper;
- it is correct as an explicit destructive bounce for a safely attributed live
  wrapper, subject to protection rules;
- it cannot resolve a rooted but unattributed survivor by itself; and
- it can double-launch over a live no-root legacy wrapper.

Diagnostics should instead describe the observed class:

- `ABSENT + STALE`: automatic relaunch is pending or policy-gated; no manual
  kill is required;
- `PRESENT_* + CONTRACT_ABSENT/UNSUPPORTED`: schedule a controlled rolling
  restart, and offer `request-restart` only when the report says the live
  wrapper is safely attributable;
- `UNKNOWN`: inspect and stop the exact verified wrapper process, then retry;
  do not promise that `request-restart` alone is sufficient; and
- `UNSUPPORTED_CONTRACT`: align supervisor and wrapper versions; refreshing
  scripts does not change a running process.

## Final launch barrier

The launch barrier remains a mandatory last check, not the source of kill
authority. It runs on a fresh post-decision snapshot after any guarded kills and
before `Start-Process` (`src/agenttalk/supervisor.py:6523-6543`).

The barrier must consume the same `WrapperPresence` observer as the planner.
It blocks on:

- a recognized same-root, same-agent wrapper or wait process;
- a still-live guarded recorded launcher identity, even if its old command line
  lacks a root or nonce;
- incomplete observation coverage for a launch shape that may still exist; or
- an unavailable snapshot, with or without prior state.

It never turns a survivor into a target. A barrier hold restores the pre-action
state and reports a visible reason; automatic backoff remains bounded, and a
manual marker remains pending for operator repair.

## Rejected designs and failure modes

| Proposal | Rejection |
| --- | --- |
| Add a wrapper-dead branch before `runtime_valid`. | It preserves the positional failure class. The next early return can dominate it again. |
| Add `notify=true` to each early `NONE` return. | `action=none` has no bus-notify consumer, and the next early return can omit it again. Escalation must be a total independent output. |
| Treat a fresh legacy heartbeat as green. | Recreates the false-green child-health defect Design 72 closed. |
| Automatically kill a present legacy/unsupported wrapper when its heartbeat is stale. | A stale heartbeat is not child identity or proof that no work is in flight. False kill is worse than a recoverable hold. Explicit manual authority remains separately constrained. |
| Widen kill authority so the wrapper loop's “supervisor restarts us” comment is always true. | Converts ambiguous spawn/active/state-loss observations into destructive authority. The safe contract is recovery when proven, escalation otherwise. |
| Relaunch whenever heartbeat is stale. | Can create two consumers when the wrapper is alive but unobserved. |
| Treat an unavailable or recognition-incomplete snapshot as absence. | Snapshot failure, PID ambiguity, and incomplete parser coverage become duplicate-wrapper authority. An available empty snapshot with complete coverage remains valid absence evidence. |
| Best-effort salvage identity, lifecycle, health, target, or authority fields from a future runtime schema. | A future writer can accidentally widen health or kill authority in an older supervisor. Reading only a bounded duplicate-safe envelope and `schema_version` to select `UNSUPPORTED_CONTRACT` remains allowed. |
| Trust a stale runtime file as process liveness. | Crash residue and PID reuse can make a dead writer describe an unrelated live process. |
| Turn a launch-barrier survivor into a kill target. | The barrier has deny evidence, not ownership evidence. |
| Remove or weaken the launch barrier to make manual restart complete. | Converts a visible, recoverable hold into duplicate delivery and cursor races. |
| Automatically relaunch an absent wrapper while its heartbeat is fresh. | Races normal process-exit and heartbeat propagation. An authorized manual restart may bypass automatic heartbeat timing but still needs current launch proof and the final barrier. |
| Clear runtime high-water on one invalid read. | A torn read can launder a lower same-turn progress sequence on the next valid poll. |
| Use the manual-restart barrier observation as the escalation record. | It is delayed, pull-only, and unlinked on a later clear result, so successful recovery erases the detailed incident evidence. |
| Treat event-ring `notify=true` as successful delivery. | The bit has no bus consumer. It is audit metadata; configured AgentTalk delivery must be checked separately. |
| Use the current plain `send`, then save “delivered.” | A crash after publication but before the state save republishes the same notice on retry. A persisted operation nonce must make publication idempotent. |
| Advance the rate-limit timestamp before durable event persistence or checked bus delivery. | A transient write/send failure can suppress the only retry and recreate a silent outage. |
| Add a migration daemon, compatibility database, or dependency. | The existing schema field, state, snapshot, heartbeat, and wrapper launch are sufficient. |

The accepted residual failure is explicit and fleet-wide. On every supervisor
upgrade, every already-running wrapped agent remains
`COMPATIBILITY_DEGRADED`/non-green until individually restarted or naturally
re-checkpointed. A fleet upgraded more often than wrappers cycle therefore
experiences this as a guaranteed universal flag day, not an occasional edge
case. The rollout requires the controlled rolling-restart runbook and an
operator summary of “N agents pending compatibility restart.”

A present or unobservable old wrapper with a stale heartbeat may remain held
until an operator can verify and stop it. That costs availability, but it
neither destroys in-flight work nor creates a duplicate consumer. Once Revision
2 is capability-active it remains durably visible on routine surfaces; routed
human receipt remains conditional on valid notification configuration.

## Implementation and verification obligations

Implementation has two release phases and no external persistence-plane change.

The **dormant substrate phase** may be split across releases only while the
Revision 2 capability cannot be advertised:

1. add the closed observation/authority types, canonical reason ordering, pure
   reducer, and legacy-equivalence tests behind a disabled capability;
2. add the versioned top-level state extension, preserving readers/writers, and
   old-compatible checked event projection, with no caller;
3. extend operation-nonce publication to freeze and deduplicate the complete
   sender/recipient/kind/body/meta identity, with no supervisor caller;
4. add `status`, `doctor`, `attention`, and web readers for the extension; and
5. add plan-schema/executor-capability negotiation while an absent capability
   still returns the exact legacy plan.

Each of those is an allowed release boundary only if executed parity proves no
recovery action, state mutation, notification, or public health result changes.

The **activation phase** is one capability-gated release unit, even if developed
as several commits:

6. enable independent runtime/presence classification, exact authority
   derivation, absence confirmation, first-managed freshness, and the temporal
   launch proof;
7. enable the single checked state owner plus pre-kill-switch incident
   activation/delivery path;
8. route automatic and manual execution through targetability proof, the shared
   observer, conditional post-teardown resolution, and the final barrier; and
9. enable incident retention, compatible events, operator projections,
   configuration diagnostics, and the wrapper compatibility-restart summary.

There is no supported release boundary between steps 6-9. The planner advertises
Revision 2 actions only when the running executor supplies the matching
capability and schema. Old executors receive the legacy plan; mismatched new
executors fail before action. The release gate tests every supported package,
script, state, and event skew.

Required regression evidence:

- table-driven coverage of all 96 automatic action/escalation cells plus the
  unconfirmed/confirmed absence and conditional-post-teardown overlays;
- every current-runtime state crossed with `ABSENT` as a reachable
  classifier-to-combiner production path, in addition to missing, unsupported,
  invalid, and every uncertainty state crossed with all four presence states;
- snapshot unavailable, record/managed-identity mismatch, PID/start ambiguity,
  and visible attribution failure select exactly the independent runtime and
  presence values specified above;
- heartbeat permutation cannot change `RuntimeObservation`; idle and terminal
  map to `CURRENT_STALE_RECOVERABLE` for fresh and stale inputs, while confirmed
  child death/watchdog proof maps to `CURRENT_TEARDOWN_PROOF`;
- overlap tests cover invalid current read plus sticky regression and sequence
  regression crossed with binding, starting, active-child, and blocked-stall
  uncertainty; reason tuples and fingerprints remain canonical under predicate
  evaluation-order permutation;
- truth-table assertions enforce the exact implications for automatic teardown,
  manual teardown, `NOW_ABSENT_CONFIRMED`,
  `CONDITIONAL_POST_TEARDOWN`, relaunch-only, kill-first, and mandatory
  escalation;
- reducer tests prove confirmation resets, preserved high-water, sticky
  regression, first-managed grace, and absence confirmation have one pure
  transition owner and authority functions mutate nothing;
- a checked incident mutation followed by every action branch and final poll
  bookkeeping cannot be overwritten by cached `$state` or `$p.next_state`;
- the production trigger: legacy runtime, confirmed absent wrapper, stale
  heartbeat yields relaunch with no kill flags or targets;
- the first complete empty snapshot yields `HOLD / ABSENCE_CONFIRMING`; a second
  compatible empty snapshot may yield `NOW_ABSENT_CONFIRMED`; present, unknown,
  and coverage-change observations reset confirmation;
- a transient empty snapshot coinciding with heartbeat staleness cannot launch;
  the two-poll confirmation is independent of the stale threshold;
- `CONDITIONAL_POST_TEARDOWN` cannot launch before guarded teardown and a fresh
  clear barrier; failed kill, survivor, or unavailable barrier yields no launch;
- positive controls: current idle + present + fresh remains
  `HEALTHY_IDLE`; a legitimate current teardown proof still recovers;
- a present degraded wrapper, fresh or stale, is never killed automatically;
- every mandatory escalation produces `DUE`, `RATE_LIMITED`,
  `DELIVERY_PENDING`, `DELIVERY_UNCONFIGURED`, or `DELIVERED`, never silent
  `NONE`;
- stale `CURRENT_UNKNOWN_BINDING` escalates for every physical-presence class,
  while confirmed absence may independently authorize relaunch;
- a stale uncertain confirmed-`ABSENT` row may relaunch without kill and retain
  an incident in the same plan;
- a new complete fingerprint is immediate, an identical fingerprint is
  rate-limited, and adding/removing a secondary canonical reason is not
  suppressed by a prior reason;
- `HOLD`, `RELAUNCH_ONLY`, and `KILL_THEN_RELAUNCH` all exercise the same
  incident path after the dry-run exit and before the action gate;
- an active `supervisor.kill` blocks recovery but does not bypass mandatory
  incident persistence, compatible event append, operator projection, or
  idempotent delivery;
- changing sender, recipient, kind, body, or semantic metadata on retry is
  rejected; retrying the exact frozen envelope and nonce returns the canonical
  message;
- package-default null routing and the current null-routed configuration show
  `DELIVERY_UNCONFIGURED` in status/doctor/attention/web without claiming a bus
  message; resolved-undelivered incidents remain visible;
- checked events deduplicate on `(agent, activation_id, transition)`, and every
  old event append preserves their old-compatible `agent_decision` projection;
- event/state failure cannot advance warning or delivery latches; crashes after
  activation, event append, warning, bus publication, and delivery-state save
  produce at most duplicates resolved by the same activation/nonce, never a
  lost or suppressed incident;
- every old state writer preserves the top-level Revision 2 extension; new
  readers map missing delivery fields to pending/unconfigured, never delivered;
- old-executor/new-planner returns exact legacy behavior, matching new pairs
  activate Revision 2, and mismatched new-executor/old-planner fails before
  action;
- controlled supervisor restart activates new executor bytes; parked
  `ABSENT`+stale agents self-heal after confirmation, while
  `PRESENT_UNTARGETABLE`/`UNKNOWN` remain visible holds;
- controlled and emergency downgrade preserve the extension and compatible
  events, with the documented temporary loss of Revision 2 projection under old
  code;
- ambiguity incidents contain bounded PID/start/executable/redacted command
  fragments and parse failure codes for rootless, foreign-root, and unreadable
  candidates;
- fresh heartbeat plus absent wrapper waits;
- snapshot unavailable, PID reuse, and unreadable command lines yield `UNKNOWN`;
  a pre-root launch yields `PRESENT_UNTARGETABLE` when guarded identity sees it
  and otherwise `UNKNOWN`, never `ABSENT`;
- every automatic relaunch-only plan reaches existing backoff, protection,
  readiness, absence-confirmation, and launch-barrier gates;
- protected, configuration-held, and stood-down agents remain held under
  automatic recovery;
- authorized manual restart preserves its configuration/stand-down override,
  backoff bypass, readiness reset, protection authorization, restart cooldown,
  attribution, temporal launch proof, and barrier precedence;
- initial launch and replacement hold when the process snapshot is unavailable,
  even when supervisor state has no prior process fields;
- a never-launched agent with missing heartbeat becomes stale after persisted
  first-managed grace, and `CONTRACT_ABSENT x UNKNOWN x STALE` creates an active
  routine-surface incident rather than a permanent silent hold;
- first launch requires two compatible complete empty snapshots; a rootless
  same-agent candidate yields `UNKNOWN` and resets confirmation;
- barrier veto preserves the canonical state revision and manual marker;
- manual-restart detailed observation may still clear under its existing
  lifecycle, but it is never the sole incident/audit record;
- current attributable manual restart, rooted-unattributed manual restart,
  dead-wrapper manual restart, and no-root legacy manual restart;
- invalid runtime resets confirmation polls but preserves the last complete
  generation/sequence high-water and sticky regression;
- rollout reports the exact pending compatibility-restart count until every old
  wrapper checkpoints the current contract;
- the first valid current `idle` record after replacement exits compatibility
  degradation and establishes a new comparison baseline;
- returned wrapper failure publishes terminal, clears heartbeat, and still
  enters safe stale recovery; and
- all five early-return roots plus the non-authoritative stalled-progress path
  exercise the “safe recovery or durable routine visibility” cross-subsystem
  contract, with routed human delivery tested only when configured.

No release should rely on this note alone. The behavior remains unchanged until
the authority matrix and failure-path tests are implemented and reviewed.
