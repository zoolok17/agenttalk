# Acceptance increment 2 plan

**Audience:** maintainers and cold reviewers deciding the registry/offline-preflight
implementation boundary. **Status:** step 0 proposal only; implementation awaits
the lead's go. This is an explanation of the proposed work, not a shipped CLI
reference or a claim of implemented checks.

Base: `fc21190f414d3a926ba8fd3aa405ab3691cfe1b7` (0.92.0).
Branch: `feat/acceptance-inc2`.
Authority: [accepted design](DESIGN-acceptance-pass.md), limited for this plan to
LD1–LD3, the Registry entry/Environment rows and retention paragraphs, Offline
execution and close-out, and Buildable increments item 2. Existing binding was
checked against [the guide](ACCEPTANCE.md) and
[increment 1's record](STEP-ACCEPTANCE-INC1.md).

## Invariant and existing integration

Preflight reads staged files and retained evidence. It never downloads, resolves
packages online, launches a checker/compiler/service/adapter, runs a command
template, or opens a network socket. Operators stage inputs and obtain execution
proof outside preflight. Existing project Git verification remains the close
workflow's separate prerequisite; preflight itself introduces no subprocesses.

Today `registry_ref` is relative to the plan file. Its exact-byte SHA-256 must
equal `plan.registry_digest`; the frozen route stores the same `registry_hash`.
Policy loading revalidates retained plan/registry bytes and their binding. Run
and reproduction hygiene already reference retained environment/offline records.
New results must extend that chain, not become an independent green flag.

The cooperative trust boundary remains LD1. Supplied version banners, package
closure, provenance and isolation attestations are declarations with retained
evidence, not authenticated execution. LD2 alone authorizes a successor's scope
reduction; the lead cannot waive a failed prerequisite. LD3 actor separation and
the existing cold-review, history, integrity and hygiene holds remain additive.

## Milestones and review stops

| Milestone | Work and completion evidence | Stop |
| --- | --- | --- |
| M1: closed records and retained inputs | Define registry v2 and its staged-file, dependency, snapshot, provenance, template, environment and proof records; enforce strict decoding, reference closure, bounds and paths. Specify schema-4 binding extensions and compatibility dispatch without enabling a GO shortcut. Show `test_preflight_path_escape_rejected` red before implementation, then green; add malformed-record and limit tables. Record the complete reader inventory and final field grammar here. | Small commits, targeted Ruff/Bandit/tests; send head and results for cold read. |
| M2: read-only preflight evaluation | Hash staged artifacts and declared transitive inputs; compare exact pins/banners; check snapshot freshness, proof positive controls, real-fetch markers and owned-loopback declarations. Retain deterministic per-entry/row outcomes and inputs, never execute recipes. Show the other five named tests red before their implementing code, then green; test no subprocess/socket/acquisition calls, read failures and informational integrity. | Send head/results; await cold read before integration. |
| M3: close integration and documentation | Wire schema-4 open, attachment, check and publish through the same validator and pure verdict fold; revalidate under the existing publication lock; bind all required new evidence into hygiene and inherited obligations. Test mutation/deletion/expiry between open/check/publish, downgrade attempts and reduced-scope successors. Update guide, changelog and this execution record. | Send final head/results for cold read. Open a PR only after that read and lead authorization to proceed. |

These are proposed boundaries, not permission to implement before step-0 approval.
Each named design test must fail for its intended missing guard, not collection
or fixture errors. M2's required tests are:

- `test_preflight_changed_byte_or_missing_dependency_fails`
- `test_preflight_expired_snapshot_holds`
- `test_preflight_offline_positive_control_required`
- `test_preflight_loopback_allowed_egress_denied`
- `test_preflight_toolchain_drift_holds`

## Proposed close lifecycle

| Boundary | Proposed behavior |
| --- | --- |
| `close open` | Validate syntax, registry digest and safe locators before creating a usable route. Reserve an instance/attempt and freeze registry, environment expectations, private staged-root locator and the initial bound preflight report under the existing transaction. A syntactically valid but unavailable/failed prerequisite opens a HOLD attempt with explicit `not-run`/`fail` outcomes; malformed policy/path escapes refuse open. An interrupted freeze stays pending/HOLD. |
| Attachment | Capture supplied observations/proofs and re-read their bounded inputs. Bind them to instance, attempt, project, revision, plan hash and registry hash. Re-evaluate rather than trusting a submitted `passed` flag. Changed declared pins require a successor; evidence cannot silently amend policy. |
| `close check` | Re-read retained policy/evidence and current staged pins, recompute preflight, and evaluate freshness against an injected UTC decision time. Surface named holds and per-entry outcomes. No acquisition, tool execution, persistence of a new green report, or mutation of a cold seal. |
| `close publish` | Run the same evaluation under the existing store-wide acceptance lock through durable publication. Re-read file bytes and expiry; do not cache a previous preflight verdict. Newly required evidence absent from the sealed source set forces HOLD and a fresh attempt/seal. Preserve failure outcomes and inherited LD2 obligations. |
| Historical audit | Validate retained snapshots without requiring an old private cache to remain mounted. Recheck retained integrity and applicable expiry. A current candidate still validates its own live staged pins. Distinguish retained historical failure from the successor's current result. |

Proposed stable hold codes (subject to lead approval):

| Code | Meaning |
| --- | --- |
| `acceptance_preflight_unavailable` | Required staged file/dependency or observation unavailable; affected execution is `not-run`. |
| `acceptance_preflight_mismatch` | Artifact, toolchain/config/template/parser pin or planned-versus-observed banner differs; `fail`. |
| `acceptance_snapshot_expired` | Required snapshot is outside its declared freshness interval; `not-run` for the dependent execution. |
| `acceptance_offline_unproven` | Required positive control/cache-hit/enforcement evidence absent or invalid; `not-run`. |
| `acceptance_offline_violation` | Recorded attempted fetch, permitted external egress, or unowned loopback endpoint; `fail`. |
| Existing `acceptance_plan_stale`, `acceptance_record_missing`, `acceptance_row_unbound` | Policy/digest changes, missing/corrupt required retained bytes, or wrong evidence binding remain unconditional integrity holds. |
| Existing `acceptance_category_moved_unreviewed` and successor approval holds | Scope/policy reduction needs the exact existing LD2 authorization; preflight cannot waive it. |

Invalid JSON, unsupported schemas and unsafe paths use the existing strict-policy
refusal contract at import, and an integrity HOLD if encountered during retained
evaluation. Preflight `not-run` never creates a successful measured run or a
fabricated exit code. All integrity/binding/offline safety failures hold regardless
of informational comparison policy. Other prerequisite outcomes follow the frozen
failure policy and retain their visible failure/unmeasured status.

## Ambiguities and proposed resolutions for approval

The design specifies required concepts but leaves their detailed wire shapes and
several operational choices open. These resolutions are proposals; none is an
already accepted extension of the design.

| Ambiguity | Proposed resolution |
| --- | --- |
| Compatibility with shipped registry v1/schema-3 passes | Add registry v2 with plan/route/bundle schema 4 for increment 2. Preserve explicit legacy evaluation; never claim a legacy pass performed preflight. A required-preflight policy rejects legacy routes. Old engines reject schema 4. No silent in-place migration or reinterpretation of frozen v1 bytes. |
| Exact field names/nested records | M1 publishes a closed field/type table before implementing validators. Include every required Registry entry and Environment concept in the cited design rows. Every conditional absence is explicit, never an unknown-field escape hatch. Use exact versions, SHA-256 pins and typed references, not semver ranges or free-form executable policy. |
| Which rows require which tools | Registry entries declare covered stable row IDs and typed dependency references, frozen by `registry_digest`. Validate all references and require each schema-4 run/row to declare its entry set. A tool-free row needs an explicit empty set; do not infer a universal tool catalogue. Pin/config/reference changes use normal successor/LD2 review. |
| Cache root discovery and portable locations | Proposed `--acceptance-cache-root` at open names an explicitly approved private root; entries use only cache-relative regular-file paths. Store the private root locator in the route. No ambient PATH discovery, user cache guessing, URL opening, archive extraction or silent fallback. |
| Transitive dependency completeness | Validate the declared finite dependency graph, all references and staged hashes; reject duplicate/conflicting pins, dangling edges and cycles for this increment. Retain package manifests/lockfiles as bounded inputs. Do not invoke a package manager or pretend declared closure proves an undeclared runtime dependency cannot exist. |
| Large packages/directories and retention | Support explicitly enumerated regular files with per-file and aggregate byte limits, no glob/directory traversal as a manifest language. Retain required verified file bytes with their manifests, not only hashes; enforce limits before reads/copies. Oversized tool distributions are unsupported/HOLD until a bounded private evidence-store policy is agreed, never hash-only success. |
| Numeric limits | Start with existing strict-JSON and retained-byte limits; add explicit entry/file/dependency-edge/argv/proof-marker counts and depth limits in M1. Every limit is tested at the boundary. Do not raise global limits opportunistically for a large tool. Lead may adjust these bounds before implementation. |
| Provenance versus trust | Require source coordinates, retrieval UTC time, exact digest/version, checksum/signature-source coordinates and explicit independent-verification status plus evidence reference. Coordinates are inert strings. Same-origin checksums are labelled provenance only. Do not run signature tools; retained cooperative verification evidence is checked for shape/binding, not promoted to cryptographic attestation. |
| Freshness clock and expiry | Strict timezone-aware UTC timestamps, explicit snapshot `expires_at` and retrieval time; require retrieval <= observation <= decision time and decision time < expiry. Reject missing or inconsistent required freshness. Inject one decision clock per evaluation; re-evaluate at publish. No network time or implicit grace period. |
| Command templates and verifier inputs | Store argv arrays and a closed placeholder vocabulary with allowed cwd/input/output roles and resource bounds. Reject shell-command strings, unresolved placeholders and escaping paths. Validate syntax/pins only; never execute or infer arbitrary argv is offline-safe. Parser/comparator/normalizer/config adapters are pinned data; unsupported executable adapters HOLD. |
| Planned versus observed environment | Freeze planned runtime/compiler/package-manager/service banners, OS, locale/timezone, config/env digests, writable overlay/scratch policy and time/resource limits. Attach exact observed equivalents and hashed row overrides. Exact comparison by default; no unapproved normalization. Missing observation is `not-run`, drift is failure; reading a banner never launches a tool. |
| Two offline modes | Closed enum: external egress denial with owned loopback, or a retained tested offline-proof recipe. Both need positive-control evidence and explicit attempted-fetch observations. Recipe mode additionally binds expected cache-hit/real-fetch markers to bounded retained logs and pinned inputs; no arbitrary regex/executable parser. Mode-specific missing evidence holds. |
| Meaning of loopback/egress test without networking | Test validation of synthetic retained enforcement reports: owned loopback allowed, non-loopback or owner mismatch denied, absent enforcement/proof or recorded fetch holds. Do not create sockets, services or firewall rules in tests or preflight. The test proves evidence interpretation, not host network isolation. |
| Service preconditions and teardown | Registry pins service files and fresh-data/owned-endpoint requirements. Validate supplied PID/start/stop/port-release records against run binding; do not launch, signal or probe a PID/socket. Existing hygiene remains responsible for requiring close-out evidence. Declared ownership remains cooperative. |
| Failure policy and LD2 | Use a closed mapping of unavailable/expired/invalid-proof to `not-run` and detected mismatch/fetch to `fail`. Gating dependencies block their rows. Informational outcomes remain visible, but mandatory retained integrity, unsafe paths and offline violations cannot be suppressed. Only an exact operator-approved successor may narrow gating coverage; preserve original failure and reduced-scope label. |
| Evidence timing at open versus dispatch/run | Open may freeze a report with missing execution proof and HOLD. Operators can supply already staged preflight proof; later run/reproduction proofs are captured at attachment. Bind supplied records to frozen expectations and attempt identity; never accept a standalone historical success report as proof for the current run. |
| Seal, history and inheritance coverage | Extend the existing evidence projection/attribution grammar to new records, source artifacts and preflight obligations, including related roots. A new frozen requirement or proof set after cold commitment invalidates readiness; use the existing successor/recovery flow instead of silently extending a seal. Inventory all readers in M1, including cold delivery exclusions and retained-history validation. |
| File mutation during checks | Reuse approved-root checks, regular-file/reparse refusal, exact-byte hashing and verified retention. Re-read at publication; store locks serialize supported record writers but cannot freeze arbitrary external cache writers. Operator-staged roots must stay quiescent during an operation. Document this cooperative boundary and test changes between lifecycle boundaries; do not claim an OS sandbox or atomic tree snapshot. |
| Recovery after missing versus changed staged bytes | Restoring the exact frozen staged bytes may satisfy current prerequisites with fresh evidence; different pins/policy require a successor. Original captured failures remain retained. Required lost/corrupt historical evidence must be repaired exactly, never replaced with a new claim of success. |

## Validation and cost controls

Use synthetic staged files, synthetic logs and injected clocks. Reuse the existing
session repository templates for the limited close-integration cases; pure
preflight tables need no Git repository. Tests must deny subprocess/network/tool
launches at the preflight boundary and require that no acquisition fallback is
called on failures. Keep all existing acceptance assertions intact.

Each milestone runs targeted files only in the foreground with checkout `src`
on `PYTHONPATH`. Run Ruff and Bandit on every new/changed Python module; fixed
argv subprocess annotations follow the existing house style if a separate
integration helper requires them. No full suite, network, downloads or staged
tool launches. Record commands, counts, durations and red/green evidence here
after execution, then stop for that milestone's cold read.

## Step 0 evidence and next decision

This commit contains the plan and an explicitly planned changelog note only.
Implementation tests executed: **0**. Ruff/Bandit: not applicable to this
documentation-only step; no Python module changed. Local relative links, all six
required test names, patch whitespace and the privacy sweep with positive
controls passed. No performance or GO claim.

Await the lead's go or amendments to the proposal, especially schema-4/legacy
policy, bounded full-file retention and the evidence-only offline-proof contract.
