# DESIGN — #81 recovery actually recovers

**Status:** design candidate; no production mechanism is claimed by this note

**Mode:** reference

**Audience:** recovery implementers, integration-test authors, and release reviewers

**Goal:** give CI a falsifiable answer to “did recovery restore the capability it
claimed to restore without losing committed work?”

**Base:** `2f95def62f67bcba39a481eb116143435219b6a8`

## Decision

Recovery worked only when an externally observed, pre-bound challenge for the
failed capability completes through the production path after the recovery
action, and every commitment made before that action remains semantically
present exactly once.

That is a conjunction, not a choice:

```text
RECOVERED =
    intended_effect_observed
    AND capability_challenge_committed_after_effect
    AND committed_work_preserved
    AND no_duplicate_effect_or_completion
```

The challenge must have been impossible for the proved failed baseline to
complete. It is normally a new deterministic stub-agent turn for wrapper and
CLI-child recovery. It is deliberately capability-specific: for owned-tree
recovery, a healthy incumbent can complete an ordinary turn before and after the
purported fix, so the challenge must exercise the recovered owned-tree authority.

None of these facts is recovery proof by itself:

- a warning disappeared;
- a health or readiness field became green;
- the recovery function returned success;
- a process was launched;
- a state record says `terminal` or `recovered`; or
- an unaffected incumbent completed an ordinary turn.

The system under test may emit evidence, but it does not award itself the gate
verdict. An external harness derives the verdict from real process observations,
validated store records, and the pre-bound scenario contract.

## Scope: what creates a recovery episode

This design is intentionally narrower than “anything went wrong.” A
`RecoveryEpisodeV1` exists only when all of the following are true:

1. The subject previously reached a proved usable generation. First launch and
   installation are not recovery.
2. A typed observation proves that one declared recoverable capability is
   unavailable or that an already-accepted recovery intent must be resolved.
3. AgentTalk owns an automatic actuator or an existing accepted control-plane
   intent for that capability. Observing a provider outage is not ownership of
   provider recovery.
4. A durable baseline binds the subject, generation, fault fingerprint,
   preservation fence, challenge contract, and finite budget before the first
   recovery action.

The subject is `(project, agent, capability, generation)`, not just an agent
name. Capabilities include wrapper execution, CLI-child execution, owned-tree
authority, supervisor adoption, and exact inbound reconciliation. This prevents
an unrelated healthy capability from satisfying a failed one.

An automatic detector and an already-accepted `request-restart` may both open an
episode. This does not add an operator ritual: it only makes the outcome of the
existing intent bounded and testable. A planned stop, a kill switch, and a
deliberately stood-down agent do not open one.

### Baseline and challenge binding

The baseline must contain:

```text
RecoveryBaselineV1 {
  episode_id
  subject                    # project + agent + capability + generation
  trigger_code
  trigger_evidence_ref       # real observation, not a copied boolean
  fault_fingerprint
  challenge_contract {
    predicate_id, predicate_version
    semantic_key_binding =
      CONCRETE { semantic_key, raw_input_manifest_ref }
      | DEFERRED {
          derivation_id, derivation_version
          semantic_key_namespace, raw_input_schema_id
        }
    instantiation_phase      # BEFORE_FAULT | BEFORE_ACTION | AFTER_EFFECT
  }
  publication_fence_ref
  attempt_limit
  deadline
  expected_terminal          # MUST_RECOVER | EXPECT_VISIBLE_BLOCK
  capability_matrix_ref      # authoritative required platforms/adapters
}
```

The challenge contract, rather than necessarily the concrete work id, is bound
before the first action. Its predicate and semantic-key derivation come from
closed registries. A `CONCRETE` binding fixes its key and raw inputs immediately.
A `DEFERRED` binding is admitted only when the scenario cannot know the concrete
input until a registered earlier effect exists; it fixes the derivation,
namespace, schema, and phase in advance, and the external harness—not the
implementation—later supplies the raw manifest and derives the key. The
external verifier recomputes both predicates from those raw inputs. A fixture
may bind a pending inbound before the fault (#112), enqueue it after the fault
but before action (#116/#150), or instantiate a deterministic key after an
earlier effect (#156). The scenario registry fixes that ordering; the
implementation may not choose it after seeing the result.

The predicate answers the counterfactual question: why could the failed
baseline not have produced this challenge result? A free-form explanation or a
later success without that binding is merely correlation.

### Publication fence and action reservation

A read followed by an actuator has a TOCTOU gap: a response can commit between
the read and a kill. Therefore every destructive or retrying action consumes a
`RecoveryActionReservationV1` created under the same serialization boundary as
terminal publication:

```text
RecoveryActionReservationV1 {
  episode_id, action_sequence
  publication_cut
  protected_commitment_manifest_ref
  semantic_effect_key
  state = RESERVED | CONSUMED | RETIRED
}
```

At reservation, the manifest contains every authoritative committed-work entry
at or before `publication_cut`. While any reservation for the episode is active,
the publication writer atomically adds every later terminal commitment for the
subject to that protected manifest. Those entries are pinned against compaction
until the terminal gate evidence is committed. The actuator cannot run until
the reservation is durable.

Both `RESERVED` and `CONSUMED` are active fence states: `CONSUMED` means the
actuator claimed the reservation, not that publication protection ended. Only
the transaction which durably commits the product terminal disposition and its
final protected manifest may move the reservation to `RETIRED`. The external
evaluation then verifies that committed cut; an actuator return cannot retire
its own fence early.

This is the no-gap rule: if a commitment becomes durable before or during
recovery, the same publication transaction protects it; if it never becomes
durable, it is not “work already done.” A response which lands during detection
is therefore covered, which is required for #73.

## Terminality invariant

Every episode must reach exactly one terminal disposition within both its
persisted attempt limit and its persisted deadline:

```text
RECOVERED {
  effect_witness_ref
  challenge_receipt_ref
  preservation_witness_ref
}

BLOCKED {
  blocker_code
  blocked_stage
  owning_component_or_dependency
  evidence_ref
  attempts_used
  automatic_retry = NONE
}

FAILED {
  failure_code
  failed_stage
  owning_component_or_dependency
  evidence_ref
  attempts_used
  automatic_retry = NONE
}
```

An action still scheduled for automatic retry is not terminal `BLOCKED`.
Retries remain in the same episode and consume the same budget; a controller
restart cannot reset either bound. Coalesced triggers for the same
subject/fingerprint join the episode rather than starting an unbounded chain.
After a terminal blocker changes, a new episode may be linked to the old one.
The old terminal record is never reopened.

`BLOCKED` is a valid fail-visible disposition, not successful recovery. It may
pass a fixture whose purpose is to prove safe visible refusal. It fails every
`MUST_RECOVER` fixture and cannot support a release claim that the agent returned
to work. `FAILED` always fails the recovery gate.

The gate mapping is closed:

| Scenario expectation | Passing observed terminal |
|---|---|
| `MUST_RECOVER` | `RECOVERED` with all three witnesses |
| `EXPECT_VISIBLE_BLOCK` | `BLOCKED` with the complete visible-blocker contract and no forbidden effect |

Every other pairing fails. In particular, always emitting a well-worded blocker
cannot turn an unavailable recovery implementation green.

`RecoveryEvaluationV1` is the external, immutable, exactly-once evaluation for
one `(candidate SHA, platform, scenario, episode)`. Its closed outcomes are
`RECOVERED`, `EXPECTED_BLOCK`, `FAILED`, `OVERDUE`, and `INVALID_EVIDENCE`.
Only the first two can match the passing rows above. This evaluation—not a
SUT-authored status field—owns CI terminality.

If the recovery owner dies, silence is not a terminal result. An independent
reconciler—not the action owner—must, at its first eligible startup or poll after
the persisted deadline, atomically commit
`ACTIVE -> FAILED{failure_code=RECOVERY_OVERDUE}`. The compare-and-swap makes
that transition exactly once. Until it is materialized, read-only status may
derive `RECOVERY_OVERDUE`, but that projection does not discharge terminality.

In CI the parent harness independently commits a terminal
`RecoveryEvaluationV1` of `OVERDUE` at its hard bound and fails the scenario; it
does not wait forever for the SUT to describe its own death. At runtime the
reconciler's committed failure feeds structured status, doctor, and dashboard
surfaces. The component that may be wedged is never the sole author of its own
health.

### Visible non-success contract

A `BLOCKED` or `FAILED` projection is conforming only when a reader can obtain
all of these positive facts from one structured record:

- episode and subject identity;
- stable blocker/failure code and failed stage;
- the component or named dependency that prevents automatic progress;
- the observation that justified refusal;
- attempts used and when the episode became blocked; and
- `automatic_retry=NONE`.

Console prose may render that record but is not the authority. “Operator
attention required,” warning cessation, an empty event list, or a dead
supervisor does not satisfy the contract. A remedy may be shown when a real one
exists, but this design introduces no required operator command. In a
`MUST_RECOVER` scenario, either non-success terminal is a gate failure.

## The three recovery witnesses

### 1. Intended-effect witness

The witness is registered per capability and binds to the episode subject. It
proves the actuator changed the failed resource, not merely that an action was
requested.

Examples:

- wrapper recovery: the old exact process identity is gone, one replacement
  generation owns the wrapper, and no same-agent survivor remains;
- supervisor adoption: a successor supervisor has consumed the persisted
  ownership input through the real adoption path and can use it;
- owned-tree repair: a fresh production walk, based on a post-trigger process
  snapshot, proves set coverage over the prior `(pid, start)` identities—each is
  re-admitted or independently proved absent/different—and emits a complete
  valid tree for the same wrapper generation. Any leftover identity keeps the
  result incomplete and names the blocker. The external verifier recomputes
  this set relation from the captured OS snapshot and persisted rows; it never
  trusts `walk_complete=true` by itself;
- inbound reconciliation: validated replay proves the exact terminal response
  for the exact inbound and the guarded commit advances past it.

A PID, warning, launch call, or hand-built “valid” record is not an effect
witness.

### 2. Capability-progress witness

The harness instantiates the unique challenge through the public production path
at the phase fixed by the pre-bound challenge contract. Success is a validated,
exactly-once receipt for that semantic challenge key which is causally downstream
of, or carries a checked binding to, the intended-effect witness. A merely
post-trigger receipt that raced ahead of the recovery effect does not pass.

For wrapper, child, relaunch, and resume scenarios the challenge is a no-model
stub turn which must land a bus response with its dispatched operation nonce and
advance the exact inbound. This is stronger than `HEALTHY`: it proves the queue,
wrapper, real child-spawn path, adapter, reply transport, and commit path all
worked together.

For a capability an incumbent can exercise while the failed recovery authority
remains broken, an ordinary turn is not discriminating. The scenario must add a
capability-specific challenge. The #156 case therefore has two stages:

1. require the real re-walk to replace the invalid tree; then
2. in the isolated fleet, keep the wrapper present and create a deterministic
   live/stuck owned descendant for which scoped tree teardown is the sole
   admitted actuator. The pre-action planner observation must deny the old
   invalid digest and admit the repaired digest. The checked action receipt must
   bind to and consume that repaired tree digest and wrapper generation before
   the queued stub turn completes.

The fresh tree is the effect witness. The second-stage turn proves the repaired
authority is usable. Complete absence is forbidden in this fixture because the
#150 absence path could relaunch without exercising the repaired tree. The
external verifier must reject any alternate actuator route. Either the tree or
turn fact alone would recreate the false green this gate exists to prevent.

### 3. Preservation witness

“Work already done” means work that crossed an existing durable semantic commit
boundary. It does not mean uncommitted model prose or a half-written external
side effect.

`RecoveryPreservationRegistryV1` is a closed capability-to-projection registry.
For the capabilities in this note its authoritative families are:

- validated terminal bus messages, keyed by immutable message identity plus
  opener/request binding, terminal kind/status, and payload digest;
- pending inbound, owed-action, resolution, and dead-letter dispositions;
- agent cursor and thread-seen watermarks;
- wrapper session-continuity records when the scenario claims resume; and
- recovery/action reservations and accepted control intents.

Each registry entry fixes a comparator id/version and its required fields. A
new or unreadable record family, an unknown comparator version, or a family
which cannot be projected returns `UNKNOWN` and fails the gate. A scenario may
select the registered capability projection; it may not omit a family or invent
a weaker comparator. Active publication-fence entries are pinned, so the first
implementation does not depend on the current store's unread cold-compaction
layout. A future live-plus-cold validated reader may replace pinning only through
a versioned registry change.

For every destructive or retry action, let `Cprotected` be the reservation's
validated manifest: the entries at its atomic publication cut plus every later
terminal commitment added while the reservation is active. Let `Cpost` be the
registered projection after recovery. The preservation requirement is:

```text
Cprotected is a semantic subset of Cpost
AND every pre-existing pending inbound is still pending or has one terminal result
AND the recovery challenge has one terminal result
AND no semantic effect key is applied more than once
```

Semantic subset compares immutable receipt identity, terminal kind/status, and
payload/outcome digest. It is not byte-for-byte store equality: compaction and
index rebuilding may change layout without deleting work. Cursors and
thread-seen watermarks may advance but may not regress or skip a pending inbound.

The exactly-once effect key is derived by the external registry from
`(episode_id, subject generation, actuator kind, target/challenge semantic key)`.
It is not an implementation-minted launch or action id. Two equivalent launches
with different fresh ids are two applications of the same semantic key and fail,
even if only one wrapper survives in the final snapshot. The verifier checks
cardinality across every externally captured actuator input and OS effect, not
only final state.

If no trustworthy preservation fence can be read, recovery must fail visibly;
it must not manufacture a clean baseline or claim `RECOVERED`.

Preservation is unconditional. `BLOCKED` and `FAILED` do not authorize loss or
duplicate effects merely because they are non-success terminals. A negative
fixture passes only when its expected refusal is visible **and** its preservation
and no-forbidden-effect checks hold.

## Mechanical gate

### Test substrate

Extend task #34's deterministic stub-agent canary, but do not treat its current
wrapper-only test as sufficient. The existing branch at `3772e05` establishes
the useful core:

- a dependency-free stub emits real Claude adapter event shapes;
- `run.make_drive` uses its default real subprocess spawner;
- the stub invokes the real AgentTalk reply CLI against the same store; and
- the test asserts that an operation-nonce-bearing response really landed,
  rather than trusting model text.

The recovery gate wraps that core in an outer process harness:

1. Create an isolated project with the production `init`/configuration writers.
2. Start the generated supervisor, real wrapper, and stub CLI as separate
   processes from the checkout under test.
3. Complete a baseline stub turn and capture the semantic preservation fence.
4. Inject the scenario fault by acting on real process state or by
   corrupting/removing a record the production writer first created. The harness
   must not synthesize a replacement “good” record or call the planner with a
   hand-built state dictionary.
5. Establish the typed failed baseline and pre-bind the challenge.
6. Let production polling, persistence, planning, and execution perform recovery.
7. Derive the terminal verdict from raw process observations, validated store
   records, status projection, and the challenge/preservation comparators.
8. Capture a final survivor snapshot and prove there is one owner, one
   completion, and no lost prior commitment.

The harness must set `AGENTTALK_ROOT` to the isolated root, bind the launched
package to the candidate checkout, strip provider credentials, enforce network
isolation where the runner supports it, and prove zero model cost. The stub is a
deterministic child, not a mock recovery record.

### Bounds and clocks

Each scenario declares a maximum number of recovery polls/actions derived from
the policy it tests and a hard wall-clock guard for harness failure. Poll count
is the primary deterministic bound; the wall clock prevents a wedged child from
hanging CI. Timeouts, missing evidence, malformed evidence, and skipped required
platforms are failures, never passes.

Long production thresholds are replaced only through supported configuration
or an explicit test fault seam. For example, the watchdog scenario uses low
test thresholds with the existing explicit low-threshold opt-in and a real hung
child; it does not sleep for 1,800 seconds and does not mock the kill result.

### CI placement and cost

| Tier | Runs | Purpose | Cost |
|---|---|---|---|
| Contract | every PR | outcome algebra, monotonic budgets, overdue derivation, preservation comparator, manifest validation | cheap, pure Python |
| Canonical native smoke | every PR | one real Windows supervisor + wrapper + stub recovery and preservation challenge; required artifact, never a green skip | moderate, no model spend; outer cost not yet measured |
| Touched scenario | additive when a recovery/store/wrapper/supervisor capability is selected | one or more additional real-subprocess allowlist fixtures | moderate, no model spend |
| Release matrix | release candidate | the full allowlist on every platform named by each scenario | highest; still deterministic and no model spend |
| Runtime projection | continuously | surface active, blocked, failed, and overdue episodes | observability only; never proof of `RECOVERED` |

A focused run of the existing #34 wrapper/stub core executed six scenarios in
20.79 seconds with no model spend or network dependency. That is evidence for
the inner challenge cost, not for the proposed outer supervisor harness. The
first live supervisor smoke and the full matrix must be measured before their
CI budgets are frozen; an estimate is not gate evidence.

Touched-path selection is only an additive optimization. It cannot suppress the
canonical native smoke: shared helpers, packaging, launch environment, and
generated-script changes are capable of breaking recovery without matching a
perfect path classifier.

A required Windows generated-supervisor fixture that skips on a POSIX runner is
not cross-platform evidence. The release matrix must execute it on Windows.
When a POSIX implementation is claimed, its applicable rows must execute there;
`not implemented` is an honest release HOLD, not a green skip.

### Gate artifact

The external harness emits one immutable artifact per scenario, bound to the
candidate SHA and OS:

```text
RecoveryGateEvidenceV1 {
  candidate_sha, scenario_id, issue_ids
  environment {
    os, os_version, architecture
    python_runtime, executor_adapter_id, executor_adapter_version
    duration_ms
  }
  isolated_root_id, episode_id, fault_fingerprint
  baseline_generation, trigger_evidence
  challenge_contract, challenge_raw_input_manifest_ref
  pre_action_process_snapshot_ref, post_action_process_snapshot_ref
  actuator_input_manifest_ref, os_effect_manifest_ref
  diagnostic_event_ref?       # never recovery authority
  challenge_id, challenge_receipt, operation_nonce, cursor_result
  publication_cut, action_sequence
  preservation_before_manifest_ref, preservation_before_digest, before_count
  protected_commitment_manifest_ref
  preservation_after_manifest_ref, preservation_after_digest, after_count
  preservation_comparator_id, preservation_comparator_version
  semantic_effect_keys, exactly_once_keys
  attempts_used, deadline, observed_terminal
  stub_executable_sha256, stub_argv
  provider_credentials_present = false
  network_policy { mode, evidence_ref? }
  model_spend_evidence
  verifier_id, verifier_version, verdict, failure_reasons
}
```

Manifest digests bind replayable entry lists; two opaque before/after digests are
not a subset proof. Existing best-effort supervisor event files are diagnostic
only. The actuator inputs and OS effects must be captured by the external
harness, and the verifier recomputes effect, challenge, subset, and cardinality
predicates from the referenced manifests. A SUT-authored status projection may
prove that `BLOCKED`/`FAILED` is operator-visible; it cannot contribute to a
`RECOVERED` verdict.

Zero paid spend is derived from the pinned stub digest/argv, absence of provider
credentials in the child environment, and the stub's captured result. Network
is reported as the actually enforced CI policy (`DENIED`, `ISOLATED`, or
`UNENFORCED`), with evidence where the runner supplies it; the artifact may not
claim `network_used=false` from a self-authored boolean. `UNENFORCED` is
acceptable only if the pinned stub and launched production path have no provider
credential and no model executable; the scenario still has zero model spend.

The release gate fails closed when an allowlist row has no matching executed
artifact for its required platform, the artifact is stale at the candidate SHA,
any manifest cannot be replayed, or any required witness is absent.

## Retroactive allowlist

The allowlist is a gate manifest, not a narrative list. Every row names its
scenario, required challenge, expected terminal, platforms, and issue. Platform
requirements are validated against `RecoveryCapabilityMatrixV1`, the
release-supported component/OS/runtime matrix; a scenario author may add a
platform but may not narrow that authoritative set. Marking a bug fixed does not
remove the row; it changes the expected result to an executed pass. A recovery
change must either select all affected rows or add a new row.

The verdicts below answer whether the gate defined in this note—not a generic
unit test—would have failed on the historical defect.

| Bug | Scenario and discriminating challenge | Challenge phase | Expected terminal | Current required execution | Would this gate have failed? |
|---|---|---|---|---|---|
| #156 — invalid owned tree is never re-walked while its wrapper stays healthy | Start from a production-written valid tree, make it invalid, retain a healthy incumbent, require an externally recomputed fresh set-cover walk, then create a live/stuck descendant whose checked teardown must consume the repaired tree digest before one queued stub turn completes. | `AFTER_EFFECT`; deferred key derivation pre-bound, concrete key/interruption later | `MUST_RECOVER` | Windows generated supervisor + source-layout stub; expands with the supervisor support matrix | **Yes, with this two-stage fixture.** A plain next-turn check would **not** catch it because the incumbent can keep doing turns. |
| #158 — a successor supervisor cannot use a persisted owned-tree record to adopt a surviving wrapper | Start supervisor A and a wrapper, persist the real tree, hard-stop A only, start B on the same root, then inject a challenge whose checked action binds B's adopted tree before the next stub turn can complete. | `BEFORE_ACTION` | `MUST_RECOVER` | Windows generated supervisor + real process restart + source-layout stub | **Yes, with the restart/adoption fixture.** An in-process reload or ordinary turn from the survivor would false-pass. |
| #116 — a twice-confirmed-absent wrapper waits for heartbeat staleness instead of relaunching | Prove complete process absence, queue a unique stub turn, and require replacement plus completion within the absence policy's poll budget rather than the much longer heartbeat threshold. | `BEFORE_ACTION` | `MUST_RECOVER` | Windows generated supervisor + source-layout stub | **Yes.** The old path misses the bounded terminal and progress witnesses even if it eventually relaunches much later. |
| #129 — a refused restart request remains latched and retries every poll | Submit the existing restart intent into a deterministic permanent safe-refusal condition; require one visible `BLOCKED` record with the exact blocker, retirement of the marker, and no later action. | accepted intent exists `BEFORE_ACTION` | `EXPECT_VISIBLE_BLOCK` | Windows generated supervisor; protected/refusal path | **Yes, on terminality/visibility.** This fixture does not claim the agent returned to work; it proves a refusal cannot remain a permanent intermediate retry. |
| #112 — the per-turn watchdog's kill wedges the wrapper it protects | Bind a pending inbound, run a real stub child with a hung descendant which inherits the output writer, let the real watchdog kill under short supported thresholds, and require one terminal result followed by a fresh stub turn. | `BEFORE_FAULT` | `MUST_RECOVER` | Windows source-layout wrapped-watchdog path for the observed inherited-writer defect | **Yes, with the inherited-writer fixture.** A generic hung child whose pipe closes normally would not reproduce the shipped defect. |
| #150 — invalid-tree HOLD pre-empts relaunch after the wrapper is confirmed absent | Corrupt a production-emitted tree, prove the entire agent tree absent, queue a turn, and let the real supervisor poll. | `BEFORE_ACTION` | `MUST_RECOVER` | Windows generated supervisor + source-layout stub | **Yes.** The old self-sealing HOLD produces neither a replacement nor the challenge receipt and times out. |
| #73 — a terminal bus response lands, then the failed turn is parked and redriven | Make the stub publish the exact response and then fail. The active publication reservation must protect that late response; require guarded cursor/thread commit and complete the next distinct challenge exactly once without re-driving the old inbound. | inbound `BEFORE_FAULT`; response may land during recovery | `MUST_RECOVER` | Windows, Linux, and macOS source-layout wrapper/stub | **Yes.** The old path fails preservation or redrives the exact inbound; new progress alone could hide the duplicate. |

Two adjacent controls explain why the witnesses are conjunctive:

- #72 would fail the stub-turn challenge when the wrapper reports healthy but
  its CLI child is dead. Health is therefore not the oracle.
- task #34's missing-resume scenario is the positive template: a later exact
  reply, cursor advance, no dead letter, and a reminted session together prove
  recovery more strongly than a self-heal status field.

## Scenario registry requirements

Each manifest row must declare:

- `scenario_id` and bug/task ids;
- failed capability and typed trigger;
- production setup and permitted fault injection;
- closed effect/challenge predicate ids and versions, raw input manifests, and
  why the failed baseline cannot satisfy them;
- challenge semantic key and permitted instantiation phase;
- checked action-input binding, including consumed tree/state digest where the
  capability depends on persisted authority;
- registered preservation projection/comparator and externally derived semantic
  exactly-once key;
- expected terminal (`MUST_RECOVER` or `EXPECT_VISIBLE_BLOCK`);
- poll/action and explicit hard-wall bounds;
- capability-matrix-derived OS/runtime and executor-adapter requirements; and
- owning test module.

The registry validator rejects an unknown trigger, witness, comparator, or
terminal; a missing required platform; duplicate scenario id; unbounded retry;
or a row which can pass from a SUT-authored `recovered=true` field.

## Deliberately out of scope

- **First launch, install, and configuration bootstrap.** There is no prior
  usable generation or preservation baseline; these need a bootstrap gate.
- **Provider availability, model output quality, and semantic correctness.**
  AgentTalk does not own provider recovery, and a paid/nondeterministic model is
  the wrong release oracle. The stub gates the orchestration path at zero spend.
- **Intentional pause, kill switch, release/end, or stood-down policy.** Those
  remove authority by design and must not be “recovered” against operator intent.
- **Host power loss or absence of any scheduler/observer.** Software cannot make
  progress while nothing executes. Once an evaluator runs, an overdue persisted
  episode must fail visibly; silence still cannot pass.
- **Irrecoverable corruption with no trustworthy commit fence.** The only safe
  result is a named visible blocker. Recovery may not invent history.
- **Uncommitted model reasoning, partial tool output, and external side effects
  without an idempotency receipt.** They have not crossed a durable AgentTalk
  commit boundary. Preserving or compensating them is a separate contract.
- **Crash prevention and root-cause elimination.** This gate asks whether the
  declared recovery path works after the injected failure, not whether the
  original failure can happen.
- **Performance tuning beyond the declared recovery budget.** Missing the
  scenario's bound fails; choosing product latency targets is separate policy.
- **Manual disaster-recovery procedures.** A manual procedure may remain useful
  operationally, but it cannot satisfy a `MUST_RECOVER` fixture or this release
  theme.

## Rejected alternatives

### Warning absence

A dead supervisor emits no warning. This is proof of neither terminality nor
progress and cannot distinguish recovery from disappearance.

### Health or readiness

Health is a sampled classification. It can observe the wrong process or only the
wrapper, as #72 demonstrated. It remains useful evidence, never the success
oracle.

### Any later completed turn

This is strong for wrapper/child recovery but false-passes #156 and #158: an
unaffected incumbent can complete work while the authority needed for the next
recovery is still unusable. The turn must be a challenge for the failed
capability.

### Recovery function success or runtime assertion alone

An action-return field can prove only that the action path believes it ran. A
runtime assertion inside the component that wedges cannot report its own death.
Both are inputs to the external gate, not verdicts.

## Review and close evidence

This design requires these independent review lenses before implementation:

1. **Failure injection:** crash at every boundary between baseline persistence,
   action, effect observation, challenge commit, and terminal write; prove a
   restart cannot reset the budget or duplicate the effect.
2. **Contract drift:** ensure store, status, doctor, dashboard, harness artifact,
   and scenario registry use the same outcome and blocker algebra.
3. **Release readiness:** prove every allowlist row executed at the exact
   candidate SHA on every applicable platform; referenced or skipped rows HOLD.
4. **False-green audit:** try to pass each scenario with a dead supervisor, a
   healthy wrong wrapper, a duplicate wrapper, a replayed receipt, a hand-built
   good record, a regressed cursor, and a missing platform leg.

Minimum close evidence is the candidate-bound `RecoveryGateEvidenceV1` bundle,
the executed allowlist projection, per-platform logs, zero-spend and declared
network-policy proof, and the reviewer dispositions above. Unit tests or a prose
assertion that the invariant holds are not close evidence.

## Evidence and dependencies

- `docs/ROADMAP.md` names v0.83.0 “recovery actually recovers” and puts this
  umbrella before its implementation slices.
- `docs/TASKS.md` records #73, #81, #112, #116, and #129; the #156 and #158
  scenario descriptions are supplied by the design-round brief and must become
  registry rows when their implementation branches exist.
- `docs/logbook/2026-08-02.md` records the #150 fleet-wide invalid-tree HOLD and
  the canary which remained absent for six polls instead of relaunching.
- `CHANGELOG.md` records the #156 residual measured on the maintainer fleet:
  nine agents retained 32-hour-old invalid trees while eight remained healthy
  and working. This is the counterexample to a generic turn-completion oracle.
- `docs/logbook/2026-07-30.md` records #112's watchdog kill leaving the wrapper
  frozen with no live CLI child.
- `docs/DESIGN-73-wrapper-landed-work-reconciliation.md` defines the exact
  durable-response boundary used by the preservation witness.
- `docs/TEST-COVERAGE-REPORT.md` records that most restart/relaunch tests are
  in-process simulations and that real supervisor/process crash coverage is a
  primary gap.
- Task #34 branch commit `3772e05` supplies the deterministic real-child stub
  and wrapper-path canary to extend. The outer supervisor/restart harness in this
  note is a dependency, not something that commit already provides.
- #158 is evidenced here by the design-round incident description, not by a
  locally established internal cause. Its real-restart scenario catches the
  externally described adoption failure; implementation review must still bind
  the eventual cause and fix to that scenario.

## Open implementation questions

These do not change the invariant but must close before code lands:

1. Will the publication writer maintain the normative protected-commitment
   manifest directly, or will a new versioned validated historical reader back
   it? The initial implementation must pin entries unless that reader lands.
2. Which process hosts the independent overdue reconciler on supervisor startup
   and poll, and how is its compare-and-swap shared across supported platforms?
3. The #156 design supplies the fresh-walk/set-cover effect witness. Which
   isolated second-stage interruption should exercise that repaired authority
   while keeping the wrapper present and every alternate relaunch path denied?
4. Which supported short-threshold/fault seams keep the real-process watchdog
   and restart fixtures deterministic without weakening production defaults?
5. What is the first measured wall budget for the canonical native smoke and
   each release scenario? No estimate becomes a passing threshold.
