# Design: authorize generated-supervisor observations and claims

**Status:** proposed for independent adversarial review; implementation is
blocked on the Windows process-provider spike; PR 107 is held

**Date:** 2026-07-31

**Base:** `e77cc3974974b4fea48d344a842bcc3876d4fd50`

**Audience:** supervisor, Windows lifecycle, and runtime-evidence maintainers

**Goal:** make a persisted `startup` or `mid_poll` kill-switch observation, and
the corresponding singleton executor claim, mean that the supported generated
`supervisor.ps1` launch path produced it. This is an explanation and authority
contract, not an implementation guide.

## Decision summary

The current checked writer proves the selected PowerShell host, PID/start,
native image, bounded ancestry, and generated artifacts. It does not prove that
the host is executing `.agenttalk/supervisor.ps1`. That distinction is
load-bearing because the status surface presents the record as an observation
made by the generated supervisor.

The replacement authority check must combine all of these facts in one locked
transaction:

1. the supplied PID/start identifies the live, selected PowerShell host;
2. provider-reported process metadata parses as the canonical
   `-File <expected .agenttalk/supervisor.ps1>` invocation;
3. the live child chain is exactly the process shape produced by the generated
   `agenttalk.cmd` shim;
4. the executing script reports the same deterministic artifact generation as
   the exact bundle validated under the lifecycle lock; and
5. selection, process identity, activity, artifact generation, and the raw
   kill-switch level are rechecked before publication or instance claim.

PID/start arguments, role labels, artifact hashes, and command-line strings are
evidence inputs. None authorizes a write alone. Missing, denied, ambiguous,
oversized, stale, or inconsistent provenance refuses the observation and the
claim.

The runtime observation schema must also change. A pre-hardening record is not
retroactively trusted merely because a new reader can parse its old fields.

## Finding that changes the premise

The security finding is not only missing coverage. The passing test
`test_checked_observer_accepts_identified_console_script_ancestry` creates an
arbitrary PowerShell harness, supplies the same hidden `--pid` and
`--pid-start` values used by `Supervisor-IdentityArgs`, and asserts that a
trusted `startup` observation is written. That test blesses the permissive
behavior.

The literal manual command without those two locator arguments is refused,
because it defaults to the Python PID. Adding the publicly discoverable
locator arguments succeeds. Invoking the generated `agenttalk.cmd` manually
also succeeds, so deleting only the empty ancestry tuple would leave the
fabrication channel open.

Implementation must visibly rewrite the existing blessing test into a refusal
test. It must not delete or silently replace the test. The diff must show that
the project knowingly reversed a previously accepted behavior.

The shared claim boundary has a second live blessing:
`test_real_core_accepts_direct_powershell_to_python_claim_chain`. It proves an
arbitrary direct-Python harness can claim and release the executor marker. The
new predicate must be shared by observation and claim, and this test must also
be visibly reversed rather than deleted.

## Threat model

### Caller capabilities this design defends against

Assume a local caller can:

- read the checkout, generated artifacts, selection record, and CLI help or
  source;
- know and supply hidden PID/start, phase, artifact-generation, and path values;
- start the selected PowerShell binary;
- set the inherited `AGENTTALK_PYTHON` shim interpreter selector, which is not
  launch authority;
- invoke `python -m agenttalk`, an installed `agenttalk.exe`, or the public
  generated `agenttalk.cmd` shim from an interactive shell or another script;
- create or remove `supervisor.kill`; and
- race supported CLI calls, artifact refresh, host selection, and process exit.

Those capabilities must not be enough to mint a record described as a
generated-supervisor startup or poll observation.

### Capabilities outside this design

The existing same-user trust boundary remains. Starting the selected host is in
scope; modifying or spoofing that host's command-line metadata, PEB, or address
space after launch is not. This design also does not defend against a process
that can directly rewrite `.agenttalk` evidence or generated artifacts, inject
into or rewrite any observed process (including one it started), spoof parent
process relationships at creation, falsify the Windows process provider, use
debug/administrator rights, or replace trusted OS components. Signer, ACL,
mapped-image, DLL-tree, and separate-service attestation remain out of scope.

> under the documented trusted-same-user model this proves the supported launch
> path, and must NOT be advertised as cryptographic authentication against the
> filesystem or process owner.

The resulting claim is deliberately narrow: the supported supervisor script
was launched and its checked shim descendant published the observation. This
is supported-path consistency, not an authenticated Windows creation audit.
It does not distinguish a Scheduled Task launch from a documented operator-run
launch, because both are supported and execute the same script.

## Supported launch grammar

The selected PowerShell process must have been started with the documented
profile-free file invocation. The normalized argument vector is:

```text
<selected-pwsh> -NoLogo -NoProfile -NonInteractive -File <supervisor.ps1> [script switches]
```

The accepted script-switch grammar is every subset of `-Once`, `-DryRun`, and
`-Quiet`, each appearing at most once and only in that canonical order. The
eight accepted suffixes are therefore: empty, `-Once`, `-DryRun`, `-Quiet`,
`-Once -DryRun`, `-Once -Quiet`, `-DryRun -Quiet`, and
`-Once -DryRun -Quiet`. The order used by the Scheduled Task, migration, and
existing functional paths remains accepted. Other orderings that PowerShell's
binder might tolerate are deliberately retired because they are outside the
closed raw-template contract. PowerShell option abbreviations, `-Command`,
`-EncodedCommand`, stop-parsing tokens, duplicate `-File` or script switches,
unknown host options, and unknown script arguments are ambiguous and must be
refused.

The Scheduled Task, documented foreground command, and `agenttalk start` all
emit the exact host-option prefix above. The task adds `-Quiet`; `agenttalk
start` uses an absolute script path and no script switch. Historical or ad hoc
launches that omit the canonical host options are not silently grandfathered.

The `supervisor.ps1` argument must be absolute and is compared to the canonical
expected path. Launch-time working directory is not available from the chosen
process provider, and a descendant's present working directory is not evidence
of the directory PowerShell used when it opened the script. Relative `-File`
arguments are therefore refused. The documented foreground examples must
first resolve `.agenttalk/supervisor.ps1` and pass that absolute path. Short
names, junctions, symlinks, or alternate paths are also refused even if native
file identity happens to match; adding any alias requires a new reviewed rule.

[`CommandLineToArgvW`](https://learn.microsoft.com/en-us/windows/win32/api/shellapi/nf-shellapi-commandlinetoargvw)
alone is not authority: its reconstruction is not documented as PowerShell
Core's parser contract. Acceptance is two-layered. First, the bounded raw
provider string must exactly match one of the closed command-line templates
emitted by the Scheduled Task, `agenttalk start`, or the newly documented
canonical foreground launcher, with only selected-host path, canonical script
path, and the closed switch combinations substituted through one reviewed
Windows quoting serializer. Arbitrary quote-equivalent spellings are refused.
Second, `CommandLineToArgvW` must reconstruct the expected normalized argument
vector above. A mismatch at either layer fails closed; no regular expression,
substring, or permissive token parse may establish the `-File` boundary.

The exact templates and serializer are not approved by prose alone. The
physical gate must capture the provider's raw string and PowerShell's actual
`$PSCommandPath`/bound script switches for every accepted emitter, then compare
both with the validator result. It must also exercise malformed quoting,
escaped quotes, stop-parsing tokens, duplicate switches, and prefix variants
that one parser could accept differently. If the supported emitters do not
produce a stable closed set across the host/architecture matrix, this design
returns to review rather than widening the parser.

## Provider-reported launch metadata

The candidate documented source is the Windows `Win32_Process` provider. Its
read-only `CommandLine` property is documented as the command line used to
start a process, and `CreationDate` provides provider-reported corroboration of
the native start observation:
[`Win32_Process`](https://learn.microsoft.com/en-us/windows/win32/cimwin32prov/win32-process).
Microsoft also recommends `Win32_Process` when a 32-bit caller cannot retrieve
64-bit process properties through `Get-Process`, which makes it the candidate
that must be proven across the supported cross-architecture matrix.

This candidate is not approved merely because the API is documented. A
same-user `Get-CimInstance Win32_Process` probe in the current constrained
development host returned `Access denied`. That environment may be stricter
than the production Scheduled Task, but the failure establishes that provider
availability cannot be assumed or treated as a harmless reader detail. The
query also introduces a PowerShell/COM subprocess. Both facts are build
blockers until the physical-host spike below measures them in every supported
launch context.

The query must:

- run profile-free and noninteractively through the already selected and
  native-identity-validated PowerShell binary;
- use a static encoded query whose only variable is the validated integer PID;
- replace caller-controlled module search paths with the selected host's
  built-in module directory and invoke the OS CIM cmdlet by its qualified
  module name; a caller-supplied `PSModulePath` must not select query code;
- request exactly `ProcessId`, `CreationDate`, and `CommandLine`; executable
  path remains native held-handle evidence because the provider property is
  privilege-sensitive and adds no authority;
- run before the lifecycle lock is acquired while retaining a native handle to
  the target, then compare the result with the locked selection and held native
  identity;
- launch in a private kill-on-close Job Object where the host permits it, with
  an outer wall-clock deadline covering process startup, module import, CIM,
  serialization, pipe I/O, and cleanup;
- concurrently drain stdout and stderr with independent byte caps, never parse
  a partial result, and on deadline terminate the owned job/process tree,
  wait/reap it, and close every handle before attempting the locked
  transaction; if bounded ownership cannot be established, fail closed before
  starting the helper;
- accept exactly one closed-shape JSON result;
- cross-check PID and creation time against the still-held native process
  observation; and
- treat null command lines, provider denial, timeout, extra rows, parse errors,
  or any mismatch as unavailable provenance.

The query helper is an informational observer, not an authentication primitive.
Authority within the stated trust model comes from agreement among the held
native process identity, current selection, strict launch grammar,
generated-shim process shape, and current artifact generation. A functional
provider spike demonstrates availability and compatibility; it cannot
demonstrate tamper resistance beyond that trust model.

Do not use `NtQueryInformationProcess(ProcessCommandLineInformation)` or direct
PEB reads as an implicit fallback. A local experiment showed that information
class 60 can retrieve a 32-bit PowerShell command line from 64-bit Python, but
that success is not a supported contract. Microsoft documents
[`NtQueryInformationProcess`](https://learn.microsoft.com/en-us/windows/win32/api/winternl/nf-winternl-ntqueryinformationprocess)
as internal and subject to change, and the documented information-class table
does not expose that command-line class as a supported contract. An
undocumented cross-bitness dependency would trade a fabrication defect for a
fleet-startup risk.

The provider gate is currently **RED / needs-info**. Before implementation is
approved, a read-only spike must demonstrate that the
public query returns the required fields for the Scheduled Task, documented
foreground, and `agenttalk start` contexts on physical x64, x86-on-WOW64, and
available ARM64/native-emulation combinations. The evidence must include
success/denial, latency and timeout behavior, exact returned fields, and the
querier/target bitness. It must also define and verify `CreationDate` precision,
UTC conversion, and comparison tolerance against native creation ticks.
Provider denial and a stuck query must be tested. If
that spike fails, this design returns to review for a different provenance
primitive, such as a deliberately designed inherited OS capability; it does
not fall back to caller-supplied command-line claims, an undocumented
information class, or PEB offsets.

## Generated-shim process shape

The generated `supervisor.ps1` always invokes
`.agenttalk/bin/agenttalk.cmd`. The batch file runs `-m agenttalk` with the
inherited `AGENTTALK_PYTHON` shim interpreter override when present, otherwise
with its baked interpreter fallback. The override is an existing supported
operator contract and is retained; choosing it does not grant write authority
without all other launch checks. Therefore the only accepted launcher-role
sequences between the selected PowerShell host and the running base Python are:

```text
cmd
cmd -> venv Python redirector
```

The `cmd.exe` image must remain the OS-owned interpreter appropriate to the
observed process architecture. The optional venv redirector and running base
Python must retain their existing exact-image and start-order checks.

The locked artifact check reads the shim's baked fallback. The held live chain
then classifies the actual interpreter as `baked` when native identity agrees
with that fallback, or `environment_override` when it differs. In the latter
case, the validator still holds and identity-checks the actual venv/base images
reported by the running interpreter, and the authority object records the
classification plus bounded native identity fingerprints; it never trusts or
persists the inherited environment string. Relative interpreter resolution is
acceptable only insofar as Windows has already resolved it to those held native
images. This preserves source, wheel, and venv overrides without pretending the
override is artifact-pinned.

Empty ancestry, console-only ancestry, venv-only ancestry, and
`cmd -> console` variants are not generated-shim shapes and must be refused at
the observer and claim boundaries. Requiring `cmd.exe` is necessary but not
sufficient: a manual caller can invoke the generated batch file. The parent
PowerShell provider metadata must also name the canonical supervisor
script.

The generated script must pass its baked 64-hex artifact generation on each
observer and claim call. The value is public and grants no authority by itself.
It closes the parsed-old-script window by requiring agreement with the exact
bundle validated while the lifecycle lock is held.

## Atomic authorization transaction

The existing global lock order remains:

```text
supervisor lifecycle -> PowerShell selection -> config
```

For both `checked_powershell_supervisor_observer` and
`claim_powershell_supervisor`, authorization has a bounded preflight followed by
one locked commit transaction:

1. Under the dedicated authorization-state lock, strictly read the allocator
   and every available trusted success floor, then durably allocate the next
   `(lineage, sequence)` tuple by the rules below. Release that lock.
   Allocation or consistency failure refuses the attempt before any authority
   work.
2. Strictly read the selection as a hint, open the locator PID, match its
   supplied start token and native image, and retain the process handle.
3. Run the owned, wall-clock-bounded process-provider helper outside all
   lifecycle/selection/config locks. Strictly parse its one result, require the
   absolute canonical supervisor-file grammar, and match PID/start to the held
   process. Close and reap the helper while retaining the target handle.
4. Acquire the supervisor lifecycle lock.
5. Validate the `supervisor` artifact boundary and retain its common generator
   generation. Strictly re-read the atomic authorization state, require the
   allocated lineage still current and `allocated_through` not below this
   attempt, and refuse a repaired/rotated or invalid allocator. An explicit
   repair holds the lifecycle lock while rotating lineage, so no repair can
   race this recheck and commit.
6. Acquire the PowerShell selection lock and strictly read the current
   selection. Match it to the preflight host and its still-live held handle;
   any selection change refuses the call rather than repeating preflight while
   locked.
7. Walk and hold the descendant chain. Require one of the two generated-shim
   role sequences, exact image identities, bounded depth, acyclic parents, and
   parent-before-child creation order.
8. Match the script-supplied artifact generation to the locked artifact
   bundle.
9. Re-read the selection and recheck every held process.
10. Acquire the config lock. Re-read the selection, revalidate the host image
    and all live handles, and read the raw kill-switch level as late as possible.
11. Branch on that locked level. The observation boundary may publish the
    active or inactive level. The claim boundary requires inactive; an active
    level refuses the instance claim and may only be published through the
    observation boundary.
12. Publish the field-owned observation or permitted instance claim with the
    attempt sequence before releasing any lock or retained target/ancestry handle.
13. Release the authority locks/handles, then complete the diagnostic attempt
    using the causal rule below.

The command-line snapshot precedes the lock, but it is not an unlocked
read-modify-write decision: PID/start, current selection, native image,
ancestry, artifact generation, and liveness must still agree at the locked
commit point. Immutability of the held host's command-line metadata is part of
the explicit trusted-same-user bound above. Moving the potentially wedged
provider call outside the lock prevents it from freezing every lifecycle
mutation.

No caller-provided string, WMI row, process name, or role label is unioned into
authority after a failed check. A failed or truncated transaction performs no
automatic teardown and publishes no trusted observation.

## Durable authorization ordering and refusal diagnostics

Refusing authority must not become silent merely because the generated task
runs with `-Quiet`. Observation/claim evidence and authorization diagnostics
therefore use separate files and separate claims.

A bounded, field-owned `supervisor-authorization-state.json` holds the attempt
allocator plus at most one current refusal episode. A failed preflight or
locked transaction updates it under a dedicated bounded authorization-state
lock. That lock is never nested with the lifecycle, selection, or config locks
during ordinary calls: the authority transaction first releases every lock and
handle, then records its bounded outcome. The state is closed-shape with:

- `schema`, an explicit `grants_authority: false`, a random bounded allocator
  lineage, and unsigned 64-bit `allocated_through` (exhaustion fails closed
  rather than wrapping);
- the current completion's exact lineage/sequence tuple and, for a
  failure, a random episode ID;
- a closed reason enum such as `provider_unavailable`, `provider_ambiguous`,
  `launch_shape_refused`, `artifact_generation_mismatch`,
  `selection_changed`, or `process_identity_mismatch`;
- the boundary (`observation` or `claim`) and, when applicable, the requested
  phase;
- bounded candidate PID/start and artifact-generation fields labelled as
  unverified inputs, never as selected-host evidence; and
- UTC first/last refusal times, a saturating count, and optional resolution
  time.

The lineage/counter orders attempts independently of wall-clock movement or
completion scheduling. On a normal allocation, the writer requires a valid
current authorization state and strictly reads every valid trusted runtime and
instance/released-tombstone success floor in that lineage. It durably assigns
`max(allocated_through, trusted success floors) + 1` before starting authority
work. A wholly fresh installation may bootstrap a random lineage at zero only
when the state file and every new-schema trusted authority record are absent.
A missing state alongside new-schema evidence, an invalid state, or exhaustion
fails closed with `authorization_state_repair_required`; it never silently
resets to one.

On completion, the writer requires the attempt's lineage to remain current and
strictly reads both the latest diagnostic completion and the greatest durable
successful sequence in the trusted runtime observation/instance authority
state (including its released tombstone). Their maximum is the completion
floor. It applies a new outcome only when its sequence is greater than that
floor. Equality is valid only as an exact idempotent replay of the same
outcome; a conflicting replay is rejected as corrupt. Thus a delayed older
failure cannot overwrite a newer success, and a delayed older success cannot
resolve a newer failure. A success resolves only failure sequences lower than
that success. Crashing after allocation leaves a harmless sequence gap.

The trusted observation/instance authority object carries the same sequence,
and each authority record retains the greatest successfully published sequence
rather than allowing a delayed lower-sequence success to roll it back. Status
treats a failure as current only when its lineage is current and its sequence
exceeds every durable successful sequence in that lineage that it can strictly
validate. A clean instance release
atomically replaces the live marker with a closed-shape released tombstone; it
does not delete the greatest-successful sequence. A future claim accepts that
inactive tombstone as non-owning state, preserves the sequence floor, and
atomically replaces it with its new live marker. Thus claim success remains a
durable causal fence after release.

Recovery is explicit and evidence-preserving. With the task/foreground
supervisor stopped, the raw switch absent, and no live instance owner, the
operator runs a new
`supervise --repair-authorization-state --quarantine
--acknowledge-no-live-supervisor` mutation. It acquires the lifecycle lock and
then the authorization-state lock (the only nested repair path), preserves any
old state bytes under a distinct quarantine name, writes a fresh random
lineage at sequence zero, and records that the new lineage supersedes all prior
lineages. New-schema runtime/instance records from another lineage become
legacy-untrusted until a new authorized call replaces them; invalid evidence
bytes remain preserved and warned, not treated as a floor. Any pre-repair
in-flight attempt carries the old lineage and is refused at the locked recheck.
The command refuses a live/unqueryable owner, a present raw switch, or inability
to preserve bytes. This is allocator recovery only; discovery, indexing,
retention, and reaping of quarantine files remain the separate follow-up.

The writer samples time at the refusal point, atomically replaces at most one
small record, bounds its own lock wait and every string/integer, and never
copies raw command lines, provider output, paths, or exception text. A later
fully authorized observation or claim attempts to mark the current episode
resolved; it does not rewrite trusted evidence from diagnostic data. If that
post-publication resolution write fails, the trusted write is not rolled back
or reported as failed. Its durable attempt sequence is the causal fence that
prevents status or a delayed writer from reviving an older warning; stderr and
the next status read surface the diagnostic-write failure separately.
Repeated identical failures update the count/last time without creating an
unbounded log, and status renders one stable warning rather than emitting retry
spam. An authorization-state allocation failure or a failure-record write on a
refused attempt remains a nonzero CLI error and may not turn refusal into
success.

Human status and `status --json` read this file through a fail-safe strict
reader. They say that an authorization attempt was refused and that the record
is untrusted diagnostic evidence; they must not say that the supervisor made
the attempt. Invalid diagnostic bytes produce a bounded
`supervisor_authorization_state_invalid` warning with the stop/remove-switch/
explicit-repair remediation instead of a traceback. A manual harness can
intentionally allocate or create a refusal, so the record grants no ownership
and cannot disable actions. This channel exists only to order authorized
attempts and make a quiet startup/provider failure durable and
operator-visible.

## Durable evidence schema

The observation schema must advance rather than treating schema-1 records as
newly authenticated. Each phase observation records a closed authority object
alongside its existing observer PID/start and time:

```json
{
  "authority": {
    "schema": "windows-supervisor-launch/v1",
    "selection_revision": 4,
    "selection_fingerprint": "<bounded hash>",
    "artifact_generation": "<64 hex>",
    "authorization_lineage": "<bounded random id>",
    "authorization_attempt_sequence": 42,
    "invocation": "supervisor-file",
    "interpreter_source": "baked",
    "interpreter_fingerprint": "<bounded native identity hash>",
    "launcher_roles": ["cmd", "venv"]
  }
}
```

The exact field names may change during implementation review, but these facts
and their closed validation may not. Do not persist the raw command line. The
authority object is the checked writer's durable attestation; it is not a
replayable credential.

A schema-1 record must not set `observed=true` after the authority change. The
reader surfaces a specific legacy-untrusted warning. When the raw switch is
active and a hardened observation succeeds, preserve the old bytes under a
distinct legacy/quarantine name before atomically publishing the new record.
When the switch is inactive, retain the old bytes for diagnosis and keep the
warning visible. Discovery, indexing, retention, and reaping of preserved files
remain a separate follow-up.

The singleton instance authority file must carry the same authorization schema,
artifact generation, lineage, attempt sequence, and monotonic
`greatest_successful_attempt_sequence` when a new claim succeeds. Its closed
state is either `active` (with the existing checked owner/token fields) or
`released` (with no live owner/token and a bounded release time). Clean release
by the existing PID/start-checked owner atomically writes the latter instead of
deleting the file. Stale-owner repair applies only to `active`; `released`
never blocks a claim and its sequence floor is preserved when the next active
marker replaces it. This makes a pre-hardening live claim distinguishable
during migration and keeps the trusted success fence after the owner exits.

## Launch compatibility matrix

The following launch paths must pass:

| Launch path | Required proof |
| --- | --- |
| Current-user Scheduled Task | Exact selected host; canonical task argument vector; absolute `supervisor.ps1`; `cmd` shim; current artifact generation |
| Operator-run foreground supervisor | Documented profile-free `-File`; canonical absolute script path; `cmd` shim |
| `agenttalk start` dashboard launch | Exact selected host and host-option prefix; absolute `supervisor.ps1`; `cmd` shim (its configured working directory is compatibility context, not authority evidence) |
| Source checkout | Same process proof; generated shim's checkout `PYTHONPATH` does not change authority; baked or held-image `AGENTTALK_PYTHON` override |
| Installed wheel, base Python | `PowerShell -> system cmd -> running Python`; baked or held-image override |
| Installed wheel in a venv | `PowerShell -> system cmd -> venv redirector -> base Python`; baked or held-image override |
| WOW64 or other supported cross-architecture host | Provider-reported host command line plus the guest-appropriate verified system `cmd`; native image reopening remains architecture-aware |

The following shapes must be refused:

| Shape | Refusal reason |
| --- | --- |
| Interactive PowerShell calling `python -m agenttalk` | No supervisor-file launch and empty ancestry |
| Arbitrary `.ps1` harness calling installed `agenttalk.exe` | Wrong `-File` target and console-only ancestry |
| Arbitrary `.ps1` harness calling the real generated `agenttalk.cmd` | Wrong `-File` target even though the `cmd` hop is real |
| Interactive PowerShell calling the generated shim | Missing `-File` supervisor provenance |
| Any launch using a relative `-File` path | Launch-time working directory is not independently bound |
| Correct image/PID with missing, denied, null, oversized, or unparsable launch data | Provenance unavailable or ambiguous |
| Duplicate/abbreviated/encoded command forms or an alternate script path | Unsupported launch grammar |
| Stale script generation, selection change, PID reuse, exited ancestor, or image mismatch | Transaction inputs disagree before publication |

## Migration and compatibility

This is a stop-and-restart authority migration, not a hot authentication
upgrade.

An already running supervisor is not killed automatically. Automatic teardown
would add a new destructive authority and is outside this change. Until that
process crosses a new checked observer boundary, it may still be running under
the old claim. Status must label a live marker without the new authorization
schema as `authorization_upgrade_required`; it must not describe it as a
hardened generated-supervisor claim.

The supported upgrade sequence is:

1. Stop the Scheduled Task or foreground supervisor and wait for the selected
   host process to exit.
2. Inspect the old instance marker strictly. If absent, continue. If invalid,
   run the existing explicit
   `--repair-instance-marker --quarantine --acknowledge-no-live-supervisor`
   path only after confirming no owner is live. If structurally valid with a
   dead owner, do **not** use that repair command (it correctly refuses valid
   bytes); the lifecycle-locked refresh in step 4 confirms the owner is gone
   and quarantines the stale marker automatically. A valid live or unqueryable
   owner remains a stop/wait blocker.
3. Remove `supervisor.kill` if present. Script refresh is a supervisor mutation
   and is deliberately refused while that raw switch exists.
4. Run `agenttalk supervise --refresh-scripts` so the script and shim share the
   new artifact generation.
5. With no supervisor running, create `supervisor.kill`, then run the canonical
   foreground launch with an absolute `supervisor.ps1` path. Require exit 3 and
   a new-schema active observation. This step atomically moves any schema-1
   bytes to the distinct preserved legacy/quarantine path before publishing the
   replacement; it is the deliberate migration operation, not deletion.
6. Remove `supervisor.kill`, then run the canonical absolute foreground
   `-Once -DryRun` launch. Require a new-schema inactive resolution and no
   legacy-untrusted warning on the primary runtime record.
7. Start the Scheduled Task or foreground supervisor normally.
8. Confirm status reports the new authorization schema, the released/live
   instance-state shape as applicable, and no primary legacy warning. Preserved
   diagnostic bytes remain filesystem-visible; their indexing/retention is the
   separate follow-up.

If status reports invalid/missing authorization state alongside any
new-schema authority record, perform the explicit lineage-rotating repair
before step 5; never delete or hand-edit the allocator to make migration
continue.

Old parsed script bytes omit or carry the wrong artifact generation and are
refused on their next observer or claim call. There is no fallback to direct,
console, or generic-shim ancestry. A site where the public process provider is
unavailable remains held with an operator-visible provenance warning; it does
not silently retain the old authority rule.

## Failure behavior and operator surface

Authorization failure returns exit 3 before observation publication or
instance claim. Startup remains a failed Scheduled Task attempt and therefore
retains retry behavior. A mid-poll raw switch transition whose provenance query
fails keeps actions disabled from the raw switch level, emits a bounded warning,
and writes no trusted phase.

Human status and `status --json` must distinguish at least:

- raw switch active but no trusted observation;
- legacy observation present but untrusted under the new schema;
- an untrusted authorization refusal with unavailable/ambiguous provider
  metadata or a refused launch shape;
- a stale diagnostic completion whose sequence is behind a durable trusted
  success fence;
- stale/mixed generated artifacts;
- a legacy live instance that requires restart; and
- a valid new-schema generated-supervisor observation.

Warnings must name the remediation: stop, wait, refresh generated artifacts,
and restart through the canonical `-File` path. Repeated provider failure must
remain operator-visible and rate-limited; it must not fade into silent retries.

## Alternatives rejected

### Delete only the empty ancestry tuple

Reject. The current console blessing test and manual generated-shim path still
mint records.

### Require any `cmd.exe` ancestor

Reject. Native image identity proves the interpreter, not which batch file or
PowerShell script caused it to run.

### Trust a nonce, generation, environment variable, or role label alone

Reject. These values are readable or copyable by the caller. They are useful
only as agreement fields inside the independently checked launch transaction.

### Trust Scheduled Task configuration

Reject as the sole proof. Operator-run foreground supervision is supported,
and a task action describes configuration rather than the live process that
made the write.

### Use an undocumented process-information class or PEB offsets

Reject. It is not a stable cross-architecture compatibility contract and would
make the supervisor unable to start on an unverified portion of the fleet.

### Introduce a service, separate OS principal, or signing system

Defer. Those mechanisms could provide stronger authentication, but they change
the deployment and trust model. This design is an anti-fabrication consistency
boundary inside the documented trusted-same-user model.

## Required implementation controls

Every direction control must fail on `e77cc39` for the intended reason.

1. Rename and rewrite
   `test_checked_observer_accepts_identified_console_script_ancestry` to assert
   exit 3 and no runtime observation. Keep the history-visible reversal in the
   diff.
2. Rename and rewrite
   `test_real_core_accepts_direct_powershell_to_python_claim_chain` to assert
   refusal and no executor marker. This is the claim-side blessing of the same
   authority defect.
3. Add a live manual generated-shim test that has a genuine `cmd.exe` hop but a
   non-supervisor parent script; assert refusal and no record.
4. Split the ancestry parameterization into accepted generated-shim roles and
   refused direct/console/venv-only roles.
5. Exercise canonical Scheduled Task, `agenttalk start`, and operator
   foreground absolute command lines plus explicit relative/alias refusals and
   all eight canonical script-switch suffixes; refuse every permutation,
   duplicate, or extra token outside that table. The current suite has no live
   Task Scheduler launch; a physical task-run artifact is required in addition
   to structural CI coverage.
6. Refuse null, denied, timed-out, oversized, duplicate-row, malformed, and
   creation-mismatched process-provider results deterministically.
7. Refuse duplicate `-File`, `-Command`, `-EncodedCommand`, abbreviations,
   alternate script paths, unsupported switches, and malformed quoting. For
   every case compare provider raw text, the closed-template acceptor,
   `CommandLineToArgvW`, and PowerShell's actual bound script/switches; a
   parser-differential case must be refused.
8. Prove source, base-wheel, wheel-in-venv, x64, WOW64, and available ARM64
   process shapes with both baked and `AGENTTALK_PYTHON`-override interpreters;
   assert the actual held images and persisted classification, not the
   environment string. Keep the POSIX portability registry updated for new
   Windows-only tests without weakening its guard.
9. Force selection, artifact generation, host exit, PID/start, and parent-chain
   races inside the transaction without sleeps; assert no write and no claim.
10. Feed a schema-1 record to the new reader; assert it is not trusted, its bytes
   are preserved, and a later valid active observation publishes new-schema
   evidence.
11. Exercise a live legacy instance marker and assert the restart-required
    warning, with no automatic kill.
12. Refuse a provider timeout in a `-Quiet` startup and assert the bounded
    untrusted diagnostic survives for human and JSON status; corrupt that
    authorization state and assert a repair-required warning rather than a
    traceback or silent reset. Prove repeated refusals remain one bounded
    episode and a successful authorized call marks it resolved.
13. Wedge the helper before stdout, after partial stdout, and with a living
    descendant. Assert the outer deadline drains caps, terminates/reaps the
    owned tree, closes handles, acquires no authority lock during the wait, and
    parses no partial result.
14. Complete ordered attempts out of order in both directions without sleeps:
    delayed older failure after newer success, and delayed older success after
    newer failure in the same lineage. Assert the higher attempt sequence
    controls status, clock rollback changes nothing, and a failed
    post-publication diagnostic update cannot roll back trusted evidence or
    revive an older warning.
15. Publish startup success at sequence 10, a refusal at 11, and claim success
    at 12; force the claim's diagnostic-resolution write to fail, then release
    the instance. Assert the released tombstone retains 12 and status does not
    revive refusal 11. Also assert a later refusal above 12 remains visible.
16. Delete and corrupt the allocator after a trusted success and assert the
    next call refuses rather than issuing sequence one. Exercise explicit
    evidence-preserving repair, assert it rotates lineage, and prove an
    already-allocated old-lineage attempt cannot publish afterward. With a
    valid state whose trusted floor is higher than `allocated_through`, assert
    allocation advances from the maximum floor.

Implementation documentation must add a superseding changelog entry and update
the operator hosting contract. It must not rewrite the historical release entry
that accurately records the earlier, now-rejected direct/cmd-hop rule.

Targeted local tests cover the touched lifecycle/runtime/CLI files. CI remains
the cross-platform and packaging gate. The public-provider spike is a design
gate and must produce physical-machine evidence rather than mocked green tests.

## Independent review asks

The adversarial review must answer these questions before build approval:

1. Can any caller shape satisfy both the canonical host launch grammar and the
   generated-shim ancestry without actually starting `supervisor.ps1`, under
   the stated threat model?
2. Does the public process-provider query remain available, bounded, and
   semantically stable in every supported host context across x64,
   x86/WOW64, and ARM64 combinations, or does fail-closed behavior create an
   unacceptable startup outage? The current constrained-host denial must have
   an explicit disposition.
3. Do canonical absolute-path comparison and native artifact identity refuse
   relative paths, short names, junction/symlink aliases, and rename races
   without excluding a supported absolute launch?
4. Can artifact refresh, a parsed-old script, PID reuse, or selection change
   make the persisted authority object disagree with what was checked?
5. Does schema migration ever present legacy bytes as trusted evidence or
   destroy diagnostic evidence?
6. Is the stop-and-restart migration sufficiently visible without granting
   automatic teardown authority?
7. Are the accepted raw templates stable, and do the template validator,
   `CommandLineToArgvW`, and actual PowerShell binding agree for every accepted
   and adversarial quoting case?
8. Does the retained `AGENTTALK_PYTHON` override remain image-bound without
   turning an inherited environment value into authority?
9. Can concurrent observer/claim attempts, diagnostic-write failure, or clean
   instance release erase the greatest-successful sequence or hide/revive a
   causally newer refusal, and does allocator repair invalidate every
   pre-repair lineage without discarding evidence?

No production implementation starts until these questions have independent
dispositions and the public-provider spike has a reviewed evidence artifact.
