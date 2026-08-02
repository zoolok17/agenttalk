# DESIGN — #81 recovery CI conformance gate

Audience: maintainers implementing or reviewing recovery behavior, the release
owner, and CI owners deciding whether a recovery scenario may become a release
gate.

Status: revision 2 design; no production implementation is claimed.

This revision withdraws the earlier general claim that runtime recovery is
terminal, preserves all durable work, and restores capability. The earlier
definition had concrete false-green executions. This note specifies a smaller
claim that can be made mechanically: a finite, versioned set of real CI
scenarios may prove recovery for only the scenarios, platforms, durable
families, and semantic work keys they enumerate.

The registry currently has no enabled scenario. Its result is therefore
`UNAVAILABLE`, not `PASS`. `WATCHDOG_HELD_PIPE_RECOVERY_V1` (#112) is the first
blocked candidate. This is intentional non-vacuity, not a placeholder green.

The normative machine-readable registry for this revision is
`docs/recovery-gates/recovery-ci-registry-v1.json`. Its canonical digest is
published beside it. Prose and registry disagreement is a gate error.

## Decision

The observable that distinguishes recovery from an appearance of recovery is:

> In a registry-selected real-subprocess scenario whose negative control has
> first proved the broken baseline cannot satisfy its challenge, the exact
> recovered process generation must execute the named production actuator path
> and complete the challenge within the CI bound; an independent harness must
> attribute both facts to that execution; globally keyed terminal work and
> effects must occur exactly once across the whole run; and the explicitly
> enumerated baseline durable families must survive.

For enabled registry entry `s` and isolated run `r`:

```text
CI_RECOVERED_V1(s, r) :=
    REGISTRY_BOUND(s, r)
  ∧ FAILED_BASELINE_ORACLE(s, r)
  ∧ ATTRIBUTED_EFFECT_CARDINALITIES_HOLD(s, r)
  ∧ ATTRIBUTED_CHALLENGE_TERMINAL_COUNT(s, r) = 1
  ∧ GLOBAL_TERMINAL_MULTIPLICITY(s, r)
  ∧ GLOBAL_EFFECT_MULTIPLICITY(s, r)
  ∧ ENUMERATED_BASELINE_PRESERVED(s, r)
  ∧ CI_PARENT_REACHED_PASS_WITHIN_BOUNDS(s, r)
```

The suite is `PASS` only when all enabled entries are `CI_RECOVERED_V1` and at
least one entry is enabled. A failed conjunct is `FAIL`. An empty registry,
missing adapter, unsupported platform, digest drift, incomplete evidence, or
unavailable historical parent is `UNAVAILABLE`; none is converted to `PASS`.

This is a CI conformance statement. It is not a statement that every runtime
recovery episode terminates. Runtime terminality remains a named residual in
this revision.

## Why the scope is deliberately narrow

Warning absence is compatible with a dead supervisor. Health is compatible
with a dead CLI child. A later turn can be completed by a healthy incumbent
that never exercised the repaired dispatcher. A nonce correlates data but does
not identify the process that executed it. A production-written `complete`
field can repeat the bug being tested. None is sufficient alone.

The gate therefore admits only scenarios for which all of these are available:

1. a failed-baseline oracle that is executable on a historical broken parent;
2. independent process and effect attribution through an exhaustive native-
   effect boundary;
3. a real challenge through the named production path;
4. whole-run semantic multiplicity;
5. raw discovery and comparison for the enumerated durable families; and
6. hard CI parent bounds.

The registry does not infer coverage from an issue label or prose fixture. A
scenario that lacks one item remains a blocked candidate.

## Closed scenario registry

### Registry identity

`RecoveryCiScenarioRegistryV1` is a finite document, not an extensible name
map. An implementation must exhaustively dispatch every `enabled_entries[].id`
known to schema version 1. Unknown IDs, normalized-key collisions, missing
required fields, extra executable oracle names without a reviewed dispatch
case, or schema drift make the registry unavailable.

Every enabled entry must bind all fields listed by
`enabled_entry_required_fields`, including:

- supported platforms;
- failed-baseline, effect, and challenge oracles plus the closed effect-contract
  family;
- the digest of every executable oracle module;
- the protected-opener selector and opener-derived terminal-contract family;
- the terminal and protected-work resolvers, external execution-attribution
  adapter, actuator adapter, publication-attribution adapter, raw-event journal
  adapter, cursor-attribution adapter, preservation-verifier bundle, and
  immutable-SUT-bundle builder, each by closed ID and executable SHA-256;
- the participant resolver by closed ID and executable SHA-256, plus its
  exhaustive, nonempty participant set when cursor preservation is named;
- the deterministic fixture builder/input and projected-baseline equivalence
  comparator, each digest-bound;
- global terminal and effect key definitions;
- the exact preservation-family IDs;
- poll and wall bounds; and
- the immutable historical parent expected to fail.

The baseline manifest, every trace record, and the final evidence envelope must
carry the same registry digest and candidate commit. The verifier must resolve
through the closed dispatch and clean-checkout rehash every enabled-entry field
ending in `*_sha256`, plus every member of `oracle_module_digests`. This includes
the contract families, resolvers, attribution/publication/cursor adapters,
actuator adapter, native-effect fence, raw-event adapter, preservation bundle,
SUT-bundle and fixture builders, and baseline-equivalence comparator. A missing
component, unresolvable ID, or byte mismatch is `UNAVAILABLE`. A SUT-authored
digest is not accepted as evidence of those bytes.

### Canonical digest

`agenttalk-nfc-sorted-json-v1` means:

1. parse JSON while rejecting duplicate object keys;
2. NFC-normalize every string and object key, then reject any key collision
   introduced by normalization;
3. admit only objects, arrays, strings, integers, booleans, and null; floats are
   rejected;
4. order object keys by normalized Unicode scalar sequence;
5. encode UTF-8 JSON with `,` and `:` separators, no insignificant whitespace,
   no ASCII escaping, and no trailing newline; and
6. compute SHA-256 over those bytes.

The sidecar digest is a review aid. The clean-checkout rehash is the oracle.
For this revision the canonical registry SHA-256 is
`548f8f6a409521cc3e0995224d015707b27abc33419cbae9eb81c740817e8bd1`;
the embedded preservation-algebra SHA-256 is
`df371ee1a79ec2be155cbf64bebd9eb10cf26f7e41b9c2f56a9a1219188fd730`.

### Non-vacuity

`enabled_entries` is empty in this revision. Availability is derived by the
closed `enabled-entry-nonvacuity-v1` algorithm; it is not an independently
authored status that may drift. The registry explicitly specifies
`minimum_enabled_entries: 1` and `empty_result: UNAVAILABLE`. CI must not skip an
empty parameterization and report the job green. The job must emit a typed
`UNAVAILABLE(empty_registry)` result and remain incapable of satisfying a
release rule that requires this gate.

The blocked #112 row is not executed and cannot contribute to pass cardinality.

### Admission procedure

A blocked candidate becomes enabled only in a change that supplies and reviews:

1. the closed dispatch case and canonical registry update;
2. executable negative and positive oracles plus the effect-contract family;
3. external attribution, native-effect fence, and raw-journal adapters on every
   declared platform;
4. a digest-bound fixture pair whose pre-recovery projections compare equal;
5. a run on the immutable historical parent that fails for the intended
   conjunct;
6. a run on the candidate that passes;
7. mutation evidence for attribution, multiplicity, preservation, and bounds;
8. measured poll and wall limits; and
9. a release-gate review of the resulting evidence envelope.

An issue description, a hand-built record, a mocked process table, or a test
that exercises only the recovery function is not admission evidence.

## Failed-baseline oracle

Each scenario owns a typed negative-control oracle. It must establish the
specific broken precondition and then demonstrate, on the historical parent,
that the exact challenge predicate is unsatisfied for that cause.

The oracle cannot be only “no response arrived before timeout.” Death also
satisfies absence. It must combine the missing result with positive external
facts defined by the entry, such as the relevant process still existing in the
blocked wait, the exact native effect having occurred, or the repaired
dispatcher path remaining unexecuted.

The same oracle module and inputs run against historical parent and candidate.
Changing the predicate between runs changes its digest and invalidates the
comparison. The historical run must produce the entry's expected failing
conjunct; an unrelated crash or harness failure is `UNAVAILABLE`, not a useful
negative.

For `WATCHDOG_HELD_PIPE_RECOVERY_V1`, an admissible negative fixture still needs
to reproduce the real inherited-writer shape with real subprocesses and prove
the wrapper remains blocked after the named watchdog effect. Existing tests
that replace process creation, process probes, or kill behavior do not close
that oracle.

### Historical/candidate fixture equivalence

`DeterministicRecoveryFixtureV1` builds the historical-parent and candidate
roots from the same canonical fixture input. The enabled entry binds the
builder ID/SHA-256, input SHA-256, and a projected-baseline comparator ID/SHA-
256. A harness-owned start barrier prevents either SUT from beginning recovery
until its raw baseline and broken-state observations are sealed.

The evidence carries separate historical-parent and candidate baseline
manifests. `ScenarioBaselineProjectionV1` must compare them equal for every
challenge, protected opener, durable family, logical process role, held
resource, failure predicate, and policy input. Platform-assigned PID/FILETIME
values may differ only through an explicit one-to-one logical-role mapping;
they are retained in the role-specific manifests rather than discarded. SUT
revision and immutable bundle are the intended differing inputs.

The failed-baseline precondition must hold externally in both roles before the
barrier releases. If the historical fixture is broken but the candidate fixture
starts healthy, the comparator fails. One unqualified baseline digest is never
sufficient.

## External execution attribution

### `ExternalProcessExecutionTraceV1`

The independent harness, not the SUT, owns the attribution channel and records:

```text
ExternalProcessExecutionTraceV1 {
  test_run_id
  registry_digest
  run_role: HISTORICAL_PARENT | CANDIDATE
  sut_revision
  immutable_sut_bundle_digest
  import_path_manifest_digest
  harness_generation_token
  process_identity
  parent_process_identity
  executable_path
  executable_sha256
  argv_digest
  isolated_store_root_digest
  scenario_id
  challenge_id
  publication_receipt_id
  event_kind
  monotonic_sequence
}
```

On Windows, `process_identity` is PID plus positive decimal creation FILETIME
read from the same live process handle. Other platforms require a registry-
named exact adapter before an entry may declare them. Rounded ISO start tokens
are not process identity.

The harness first creates an immutable, isolated SUT bundle from the exact
historical-parent or candidate tree. `ImmutableSutBundleV1` hashes the canonical
source/package manifest, entry point, interpreter, environment import paths,
and every loadable AgentTalk module, then makes the bundle read-only for the
run. The launch trace binds `run_role` and `sut_revision` to that bundle. A
generic `python.exe` hash or a harness-declared commit string alone does not
prove which AgentTalk bytes ran.

The harness mints a non-refreshable generation token before spawn and binds it
to the exact OS process identity, executable and immutable-bundle digests, SUT
revision and role, parent identity, and isolated root. Possession of a payload
nonce is insufficient.
The attribution endpoint must independently recover and compare the peer's OS
identity; an inherited token presented by a different process fails.

The challenge terminal is counted only if the externally observed writer is
the process generation, SUT bundle, and executable required by the entry and
its ancestry shows the intended recovered path. A healthy incumbent, a helper
process, or a different checkout can publish a semantically valid reply and
still fail this conjunct.

### Writer-to-terminal join

`ExternalAttributedPublicationBrokerV1` must observe the actual production
publication boundary and atomically bind all of these in one harness-owned
receipt:

- exact writer PID and creation identity, generation, SUT bundle, and role;
- scenario, challenge, exact opener, and obligated responder;
- terminal message ID and canonical raw payload SHA-256;
- final store path and publication-order sequence/event ID;
- closed publication-path ID; and
- the immutable raw bytes retained by the harness.

The terminal resolver must join that receipt to byte-identical raw store or
raw-event-journal content and to `publication_receipt_id` in the process trace.
A trace from the recovered process plus a terminal written by an incumbent is
not a join and fails. If the production publication path cannot expose this
external atomic binding without substitution, the scenario remains
`UNAVAILABLE`.

### Exact effect and actuator path

`WatchdogActuatorBrokerV1` is the first proposed effect adapter. It remains a
dependency. It must sit at the native boundary reached by the actual production
watchdog path, verify the caller's externally bound identity, execute or observe
the OS effect, and append a harness-owned record containing:

- process generation and executable binding;
- closed actuator-path ID;
- semantic target identity;
- requested and observed native operation;
- native result; and
- monotonic event sequence.

An alternate test-only recovery controller is not equivalent. A production
receipt copied into the harness is not attribution. A nonce proves correlation,
not which generation, executable, or path caused the effect.

Every enabled entry binds a closed effect-contract family and executable
digest. It enumerates the allowed actuator kinds, externally resolved semantic
targets, and required cardinality for each resulting `RecoveryEffectKeyV1`.
For the #112 candidate, `WATCHDOG_STOP_EFFECT_FAMILY_V1` requires exactly one
watchdog stop for the pre-bound target; caller-selected actuator names or
cardinalities are not admitted.

Broker observation is insufficient if the SUT can bypass the broker.
`ExternalNativeEffectFenceV1` must make the named adapter the exhaustive native-
effect capability and independently reconcile every target state transition
with a broker receipt. The initially proposed Windows shape would use a harness-
owned restricted test token/job: the SUT lacks target termination handles and
permissions, the
broker alone retains the permitted handle, the harness owns console and input
signals, and the deterministic target cannot self-exit inside the bound. The
harness observes exact target identity, exit transition, code, and sequence.

One brokered stop plus any direct, unmatched, or unexplained target exit fails.
If the restricted boundary, process permissions, or independent exit observer
cannot prove exhaustiveness on the CI host, the entry is `UNAVAILABLE`.

Until the process trace, actuator adapter, native-effect fence, and negative
fixture all exist, #112 remains blocked and the registry remains empty.

`ExternalNativeEffectFenceV1` is the single unbounded dependency for the first
enabled entry. It is not currently feasible alongside an exact, unmodified
historical parent: `_ProcStream` creates the child with `subprocess.Popen` and
retains a termination-capable Windows process handle, existing paths call
`terminate()` or `kill()`, and the watchdog calls `os.kill(SIGTERM)`; a later
DACL or restricted-token step cannot revoke the retained handle. This note does
not select a resolution. The registry remains `UNAVAILABLE` until an operator-
scoped feasibility spike selects and proves a boundary compatible with the
historical-parent requirement.

## Global semantic multiplicity

Exactly-once is evaluated across the entire isolated store session and the
external raw-event journal, not separately per recovery episode.

### Terminal assignments

```text
TerminalAssignmentKeyV1 = (
  store_session_id,
  exact_opener_message_id,
  obligated_responder,
  opener_derived_obligation_kind
)
```

The terminal message ID, operation nonce, recovery episode ID, and wrapper
generation are not members. A duplicate terminal sent under a fresh message ID
therefore collides with the first terminal. A different recovery episode does
not mint a new obligation.

Each enabled entry binds a `protected_opener_selector` that runs over every
strictly validated raw message in the baseline and raw-event journal. It returns
`PROTECTED(opener_id, responder, obligation_kind)` or `NOT_PROTECTED`; unknown or
ambiguous input is a gate failure. The selected opener, not a later terminal,
determines `opener_derived_obligation_kind`.

The entry also binds a closed `terminal_contract_family` and terminal-resolver
module. That family enumerates every mutually exclusive terminal variant for
the obligation. Accepted, rejected, error, or any other allowed terminal
variant for one opener therefore resolves to the same assignment key. More
than one variant is cardinality greater than one, not two valid keys. A raw
opener-linked message that resembles a terminal but is outside the closed
family fails classification; it is not ignored.

The protected opener set is discovered from the raw baseline plus all raw
events in the run. It includes work already terminal at the baseline cut as
well as work pending at that cut or created later. For every protected opener,
the verifier resolves terminal messages back to the exact opener using the
stored origin binding and requires cardinality exactly one. Request IDs may be
checked for consistency but do not replace the opener identity.

### Recovery effects

```text
RecoveryEffectKeyV1 = (
  test_run_id,
  registry_digest,
  scenario_id,
  protected_work_key,
  actuator_kind,
  semantic_target
)
```

`episode_id` is deliberately absent. Two episodes that perform the same
semantic effect for the same protected work collide. Different required effect
kinds, such as one stop and one later launch, have distinct keys and distinct
entry-specified cardinalities.

`protected_work_key` is not SUT-authored. Each entry binds a closed
`protected_work_resolver` and its executable digest. It derives the key from the
strict raw opener/obligation and the entry's semantic target. Episode, attempt,
wrapper generation, output message ID, operation nonce, timestamp, and any
caller-selected recovery identifier are forbidden inputs. Unknown or ambiguous
derivation fails the gate. For the first candidate, the proposed protected work
is the opener-derived terminal assignment plus the watchdog's exact semantic
target.

Effect counts come from the external actuator journal. Terminal counts come
from strict raw store discovery plus the external raw-event journal. Final
state alone is insufficient because a duplicate could be created and later
deleted.

## Explicit preservation-family algebra

Revision 1 incorrectly spoke of all protected durable work. Revision 2 protects
only the two families named by `RecoveryPreservationFamilyV1` for an isolated,
quiescent CI store.

The preservation registry is itself a closed dispatch. Its machine record
enumerates the only admitted family, validator, and comparator IDs and the
required fields of each case. Each family record must be field-for-field equal
to its compiled dispatch case, including family-specific fields such as
`integrity_paths` and `session_binding`; the generic required-field list is only
a minimum. Unknown IDs, an incomplete or altered case, or an enabled entry
whose preservation-verifier bundle digest does not implement every named case
is `UNAVAILABLE`.

Each scenario dispatch case pins an exact, nonempty preservation-family set.
The enabled entry's `preservation_families` must be set-equal to that case; an
empty or selective list is `UNAVAILABLE`. The proposed #112 case pins both
`HOT_BUS_MESSAGE_V1` and `AGENT_CURSOR_V1`.

### `HOT_BUS_MESSAGE_V1`

Raw discovery reads every `messages/*.json` file, the publication-order sidecar
and anchor, and `config.session_id`. The verifier uses strict message schema,
signature, filename, session, and publication-chain validation; it does not
consume a filtered production `valid_messages()` view.

The closed comparator
`baseline-byte-identical-and-global-terminal-multiplicity-v1` requires every
valid baseline message to remain byte-for-byte unchanged and present.
Messages created during the run must form a valid append to the publication
chain. Semantic terminal multiplicity is checked independently, so preserving
two duplicate messages does not pass.

The baseline and final cuts are made while the harness has stopped new writers.
This avoids claiming that message publication and cursor updates form one
transaction when they do not.

### `AGENT_CURSOR_V1`

Raw discovery reads the exact `state/{registry_participant}.cursor` file for
every member of the enabled entry's registry-bound, exhaustive, nonempty
`registry_participants` set. A clean-checkout participant resolver independently
derives the mandatory union of every protected opener's sender and obligated
responder, every attributed challenge writer, and every scenario control actor.
The registry set must contain that union; the SUT cannot select or reduce it.
Naming `AGENT_CURSOR_V1` with a missing, empty, or incomplete participant set is
`UNAVAILABLE`, not vacuous preservation. The verifier accepts only empty
or a syntactically valid generated message ID. It does not inherit the
production reader's malformed-as-empty fallback.

A cursor may stay or advance, never regress. Every crossed addressed input must
have the entry-required terminal or disposition witness, and the destination
must exist in the validated message chain. Advancing a cursor merely because a
later healthy process wrote it is not preservation.

`ExternalAttributedCursorBrokerV1` observes the actual production cursor-write
boundary. It retains exact before/after bytes and atomically binds the write to
the peer's exact process/generation/SUT identity, participant, destination, and
closed cursor-path ID. A cursor mutation without a matching broker receipt
fails.

### Explicit exclusions

The following are not protected by version 1 and must not be described as
preserved by this gate:

- cold compacted messages;
- dead letters and dead-letter attempt ledgers;
- thread-state files;
- awaiting records;
- wrapper session records;
- operation markers;
- owed-action ledgers;
- supervisor ownership records; and
- any durable family added after this registry version.

Their paths are listed as `out_of_scope_paths` where a stable path is known.
Discovery of one does not make the gate fail; deletion of one is simply outside
the claim. Protecting another family requires a new versioned family case, raw
discovery rule, validator, comparator, mutation tests, and registry digest.

### Raw-event journal dependency

`ExternalRawEventJournalV1` remains required before any entry is enabled. On
Windows, the first implementable case is
`WindowsIsolatedStoreChangeJournalV1`, a harness-owned
`ReadDirectoryChangesW` watcher over the isolated root with an explicit
overflow/loss result. It monotonically records path, operation, rename pairing,
and sequence for creation, replacement, rename, and deletion, including
transient files between cuts. It is tamper/loss evidence, not actor attribution:
ordinary directory notifications do not identify the writer.

Exact actor identity and raw pre/post content come from the publication and
cursor brokers at their actual production write boundaries. Their immutable
receipts retain the raw bytes in a harness-owned blob store. The closed Windows
adapter reconciles every notification and final-manifest change one-to-one with
the expected broker receipt sequence. An unmatched write, unmatched receipt,
rename mismatch, sequence gap, or watcher overflow is `UNAVAILABLE`; it is not
ignored. A brokered create-then-delete terminal remains keyable from its retained
bytes. An unbrokered create-then-delete produces an unmatched notification and
fails without needing to trust its content.

The SUT cannot author or truncate the watcher log, broker receipts, or blob
store. Other platforms remain unavailable until they supply an equivalent
closed watcher-and-broker reconciliation adapter.

## CI terminality and the runtime residual

The CI parent owns a hard wall bound and a finite poll bound from the registry.
It kills the isolated process tree after collecting failure evidence if the
scenario does not pass. Thus the *test* reaches `PASS`, `FAIL`, or `UNAVAILABLE`
in bounded time.

This does not prove runtime recovery reaches a terminal outcome. If the runtime
reconciler dies while the rest of the host continues, no independently owned
component currently guarantees a terminal recovery record or a visible named
blocker.

```text
RUNTIME_RECOVERY_TERMINALITY_UNENFORCED
  blocked_on: independently owned durable reconciler
```

Runtime visibility and automatic resumption remain release residuals. This gate
does not introduce an operator ritual, and an operator action cannot satisfy a
CI scenario.

## #156 checked witness and inherited identity defect

#156 fits the conceptual class but is not an enabled version-1 scenario. Its
future verifier must independently reconstruct the set cover from raw manifests
and live OS observations. It must never trust production `status == complete`,
`walk_complete`, `rejected_count`, or a copied tree digest as the proof.

For prior entry set `P`, the verifier constructs disjoint sets:

```text
R = entries re-admitted by a fresh live-edge walk from the wrapper
A = entries independently confirmed absent or occupied by a different identity
X = every remaining entry, reported as a named rejection

P = R ⊎ A ⊎ X
successful_repair iff X = ∅
```

Re-admission requires an exact live process identity and fresh ancestry. A
prior row whose `start_filetime` is null is categorically ineligible for `R`.
Because no exact prior identity exists to compare, such a row may enter `A`
only when the OS definitively reports its PID absent. A present PID, access
failure, current exact identity, or any ambiguity enters `X`; observing the
current process cannot prove it differs from an unidentified prior process.
Falling back to rounded `_start_tokens_match` can call two different processes
“same” and is forbidden for the witness.

After set-cover repair, a second attributed challenge must exercise the checked
authority/dispatcher path that consumes the repaired tree. An incumbent turn,
raw OS disappearance, or a copied repaired digest cannot satisfy it.

Current field data also limits what #156 represents: six fleet trees are
invalid and three complete across four causes; two invalid cases have no live
wrapper and are outside #156's live-wrapper predicate. The live-wrapper case is
therefore not representative by volume. Wrapper-gone recovery belongs to the
#150 candidate or another explicitly admitted row.

## Retroactive issue ledger

“Would catch” below means the current revision-2 gate as actually available,
not the behavior of a hypothetical completed fixture. Because the registry has
zero enabled entries, no issue is claimed as caught today.

| Issue | Local establishment | Revision-2 gate today | Honest future disposition |
| --- | --- | --- | --- |
| #112 — watchdog kill wedges its wrapper | **ESTABLISHED as an injected-boundary regression, not as historical-parent evidence.** Reviewer-1 ran the three existing tests against the current `2f95def` workspace (3/3); they emulate the historical failure through injected process/effect boundaries. No historical-parent gate run has been executed. | **NO — UNAVAILABLE.** | First candidate. It may become `WATCHDOG_HELD_PIPE_RECOVERY_V1` only after real inherited-writer subprocess, external process/effect attribution, raw journal, bounds, and historical-parent evidence exist. The historical parent must fail the exact challenge conjunct. |
| #156 — invalid owned tree is not re-walked while a wrapper stays healthy | **ESTABLISHED** for the live-wrapper predicate. | **NO — UNAVAILABLE.** | A later row needs independent raw set-cover, the null-FILETIME exclusion, and an attributed downstream authority action. The production `complete` field is not evidence. |
| #158 — successor adoption of a surviving wrapper | **CANNOT-ESTABLISH.** No independent local task, test, logbook, or commit evidence establishes the reported incident or internal cause. | **NO.** | Do not create a registry entry from the current record. First obtain an independently reproducible incident and exact restart/adoption oracle. |
| #116 — confirmed-absent wrapper waits for heartbeat staleness | Locally described recovery candidate; no closed attributed relaunch fixture is bound here. | **NO — UNAVAILABLE.** | Requires a real absence proof, externally attributed replacement generation, attributed challenge, and measured absence-policy bound. |
| #129 — refused restart remains latched and retries | Locally described visible-block candidate. | **NO — OUT OF SCOPE.** | Version 1 proves successful recovery entries, not runtime visible-block terminality. Admission waits for the independently owned durable reconciler and a closed blocker oracle. |
| #150 — invalid-tree HOLD pre-empts relaunch after wrapper absence | Field shape exists, including current wrapper-gone invalid trees, but no attributed relaunch fixture is registered. | **NO — UNAVAILABLE.** | Requires independent whole-tree absence, attributed successor launch and challenge, and a preservation family for any ownership state the scenario promises to retain. |
| #73 — response lands before failed turn is parked/redriven | Locally described concurrency/preservation candidate. | **NO — UNAVAILABLE.** | Requires a complete external raw-event journal and global terminal index across the race. The two version-1 preservation families and quiescent cuts do not prove concurrent publication fencing. |

This ledger prevents retrospective theatre: #112 and #156 are established
instances, but the design does not claim a gate catches them before the gate can
actually execute.

## Mechanical check

### Substrate

Each enabled row runs in an isolated temporary store against real source-layout
entry points and real subprocesses. The stub agent (#34) supplies deterministic
turn behavior without model spend. Process creation, termination, filesystem,
CLI, and store boundaries remain real unless the registry explicitly declares
an external observing adapter. A hand-built Python dictionary cannot stand in
for a production-written store artifact.

The run has four phases:

1. **Bind:** clean-checkout hash, registry/oracle hashes, platform adapters,
   isolated root, raw baseline, and global key index.
2. **Negative control:** run the historical parent and require the typed failed-
   baseline oracle rather than an unrelated timeout.
3. **Candidate:** reproduce the same baseline, permit automatic recovery, and
   inject the exact challenge. No operator action is available.
4. **Verify:** stop writers, close journals, recompute attribution, multiplicity,
   and preservation independently, then emit one evidence envelope.

The verifier consumes raw files and harness-owned traces. Production summaries
may be compared for diagnostics but cannot discharge a conjunct.

### Evidence envelope

```text
RecoveryCiEvidenceV1 {
  schema_version
  registry_digest
  preservation_registry_digest
  scenario_id
  platform
  candidate_commit
  historical_parent_commit
  oracle_module_digests
  verifier_and_adapter_digests
  historical_parent_sut_bundle_digest
  candidate_sut_bundle_digest
  fixture_builder_digest
  fixture_input_sha256
  baseline_equivalence_comparator_digest
  historical_parent_baseline_manifest_digest
  candidate_baseline_manifest_digest
  projected_baseline_equivalence_result
  external_process_trace_digest
  external_actuator_journal_digest
  external_publication_journal_digest
  external_cursor_journal_digest
  external_raw_event_journal_digest
  terminal_key_index_digest
  effect_key_index_digest
  poll_count
  elapsed_monotonic
  conjunct_results
  result: PASS | FAIL | UNAVAILABLE
}
```

Evidence is invalid if a required digest or trace is missing, if the registry
cannot be rehashed, or if the evidence says `PASS` while the registry is empty.

### CI placement and cost

The first enabled #112 scenario belongs on Windows CI because its historical
failure shape is Windows-specific. It uses the no-model stub and local
subprocesses only. The negative parent plus candidate run is expected to cost
seconds to low minutes; admission requires measured bounds before it becomes a
required PR check. Platform-specific later rows run only where their exact
adapters exist.

No paid model, network service, browser, or operator is required.

## QA strategy and close evidence

The highest risks are a false-green attribution, a duplicate hidden by a fresh
ID or episode, an omitted durable family, and a test process that never
terminates. Required checks are:

### Registry and digest tests

- empty registry returns `UNAVAILABLE`;
- one fully valid enabled row is required for possible `PASS`;
- every enabled row's preservation-family set is nonempty and exactly matches
  its closed scenario case;
- duplicate or NFC-colliding keys, unknown scenario IDs, missing fields, floats,
  registry drift, and oracle-module drift fail closed;
- registry/sidecar disagreement is detected by clean-checkout rehash; prose-to-
  registry semantic parity remains an explicit documentation-review check; and
- every enabled ID has an exhaustive verifier dispatch case.

### Attribution mutation tests

- valid nonce from the wrong PID, creation FILETIME, executable, checkout,
  parent, generation, root, or actuator path fails;
- a helper or healthy incumbent cannot answer for the recovered generation;
- copied SUT receipts and copied repaired digests fail;
- an inherited generation token from another process fails peer-identity
  comparison; and
- a recovered-process trace joined to an incumbent-written terminal fails;
- a terminal whose raw bytes, publication sequence, or writer receipt differs
  at any join point fails;
- a cursor mutation written by the wrong generation or lacking a matching
  cursor-broker receipt fails;
- an unbrokered native effect, unexplained target exit, caller-selected effect
  kind, or cardinality outside the closed effect family fails;
- historical-parent bytes labelled as candidate, a mutable SUT bundle, or an
  alternate imported AgentTalk module fails; and
- historical-parent/candidate fixture-input drift, broken-state drift, or
  projected-baseline inequality fails before recovery is released;
- a brokered native effect plus any unmatched target exit fails; and
- missing exact identity adapter is `UNAVAILABLE`.

### Multiplicity mutation tests

- duplicate terminal under a fresh message ID fails;
- duplicate of work already terminal at the baseline cut fails;
- identical effects split across two episode IDs fail;
- create-then-delete terminal/effect evidence remains visible in the external
  journal and fails; and
- two legitimate different actuator kinds use separate declared keys.

### Preservation and #156 tests

- raw-message deletion, mutation, signature/filename/session mismatch, and
  publication-chain break fail;
- cursor regression, malformed-as-empty cursor, or advance without disposition
  fails;
- excluded durable families are demonstrated not to be part of the claim;
- a new protected family cannot enter without a registry-version change; and
- a #156 prior row without FILETIME can only be confirmed absent or rejected,
  never re-admitted.

### Integration and failure injection

- build both roles from the same digest-bound fixture, prove projected baseline
  equivalence and the broken precondition in both, then run the real historical
  parent negative control and candidate positive path;
- run real subprocess lineage, CLI/store boundary, and named actuator path;
- kill or wedge the SUT, attribution adapter, verifier, and reconciler at each
  phase; the CI parent must return `FAIL` or `UNAVAILABLE` within its hard bound;
  and
- preserve full raw artifacts for an adversarial reviewer.

Performance benchmarking, model-quality evaluation, UI/browser testing, and
network security scanning are not required for this local no-model gate.
Security review of the harness capability and peer-identity binding is required
because forgery would create a false green.

Release close requires: exact commands, platform, candidate and historical
commits, registry and module digests, measured bounds, pass/fail mutation
matrix, raw evidence artifact paths, and independent review of attribution,
failure injection, contract drift, integration behavior, and release readiness.

## Named dependencies and implementation order

The design is specified; the gate is not available. Implementation should
proceed in this order:

1. canonical registry parser, exhaustive dispatch, digest binder, and explicit
   `UNAVAILABLE` result;
2. `ImmutableSutBundleV1`, `DeterministicRecoveryFixtureV1`, and the projected-
   baseline comparator, plus `ExternalProcessExecutionTraceV1` for Windows exact
   identity and actual loaded source/package bytes;
3. `WatchdogActuatorBrokerV1`, `ExternalAttributedPublicationBrokerV1`,
   `ExternalAttributedCursorBrokerV1`, and
   `WindowsIsolatedStoreChangeJournalV1`, under
   `ExternalNativeEffectFenceV1`;
4. real inherited-writer #112 negative/positive fixture using the stub agent;
5. global terminal/effect index and the two preservation-family verifiers;
6. historical-parent, candidate, and mutation evidence; then
7. a reviewed registry change that moves #112 from blocked to enabled.

Other named residuals are:

- `RUNTIME_RECOVERY_TERMINALITY_UNENFORCED` — independently owned durable
  reconciler unavailable;
- exact identity adapters for every non-Windows platform claimed by a future
  row;
- #156 null-FILETIME reconciliation repair and external authority trace;
- attributed launch/adoption paths for #116, #150, and any established #158
  incident; and
- concurrent publication fencing/journaling for #73.

## Deliberately out of scope

- **Universal runtime recovery.** The current process topology has no durable,
  independent reconciler owner.
- **Runtime blocker visibility.** Important, but version 1 does not claim it can
  survive reconciler death.
- **All incidents and all recovery paths.** Only enabled registry rows count.
- **All durable state.** Only `HOT_BUS_MESSAGE_V1` and `AGENT_CURSOR_V1` count.
- **Concurrent store preservation.** Version 1 uses harness-owned quiescent cuts;
  #73 needs a stronger journal/fence.
- **Business/model correctness.** The stub proves capability execution, not the
  semantic quality of a paid model answer.
- **Platforms without an exact registered adapter.** They are unavailable, not
  approximated.
- **Manual repair.** No operator action can make a scenario pass.
- **Availability of blocked candidates.** A design name is not an implemented
  oracle.

These exclusions keep the first gate falsifiable. They are not claims that the
excluded behavior is safe.

## Rejected alternatives

- warning disappearance;
- health/readiness alone;
- any later completed turn without generation/path attribution;
- operation nonce as process identity;
- production `complete`, digest, receipt, or cursor summaries as their own
  verifier;
- per-episode exactly-once keys;
- final-state-only multiplicity;
- a preservation registry described as closed without raw discovery;
- hand-built records or mocked process boundaries for admission;
- timeout absence without a positive broken-state oracle; and
- unconditional runtime terminality without an independent reconciler owner.

Each can pass while the repaired capability remains broken or completed work is
duplicated or destroyed.

## Open questions

No open question changes the current `UNAVAILABLE` result. Before #112 can be
enabled, reviewers must settle:

1. the exact Windows peer-identity mechanism for the attribution channel;
2. how an immutable SUT bundle proves the AgentTalk module bytes actually
   available to the historical-parent and candidate processes;
3. how the production publication boundary atomically joins exact writer
   identity to terminal bytes and publication sequence;
4. how the native watchdog boundary exposes an externally verifiable closed
   actuator-path ID without substituting a test controller;
5. the feasibility-spike result that selects and proves an exhaustive native-
   effect boundary compatible with exact historical-parent execution;
6. the exact notification-to-broker reconciliation rules and buffer sizing for
   `WindowsIsolatedStoreChangeJournalV1`;
7. measured poll and wall bounds for the real inherited-writer fixture; and
8. the immutable historical parent commit used by the negative control.

Until those are answered in executable artifacts, the registry remains empty
and the release gate remains unavailable.
