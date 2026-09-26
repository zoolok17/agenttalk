# Acceptance increment 2 plan

**Audience:** maintainers and cold reviewers deciding the registry/offline-preflight
implementation boundary. **Status:** M1/M1c readers and M2 read-only preflight
implemented for cold review; M3 close/GO integration has not started. The lead
approved the plan subject to D1–D6 below. Earlier milestone sections preserve
their historical scope; the M2 section records the current operator surface.

Base: `fc21190f414d3a926ba8fd3aa405ab3691cfe1b7` (0.92.0).
Branch: `feat/acceptance-inc2`.
Authority: [accepted design](DESIGN-acceptance-pass.md), limited for this plan to
LD1–LD3, the Registry entry/Environment rows and retention paragraphs, Offline
execution and close-out, and Buildable increments item 2. Existing binding was
checked against [the guide](ACCEPTANCE.md) and
[increment 1's record](STEP-ACCEPTANCE-INC1.md).

## Lead decisions

The lead approved the plan in `tk-1b066cd1a42c`; unlisted proposals are accepted.
These decisions override the corresponding original planning choices below.

- **D1:** distributions are pinned by exact digest and cache-relative location,
  never copied into retained evidence. Only bounded declarative inputs are
  retained: manifests, lockfiles, configs, proof logs, banner captures, provenance
  and verification records (including bounded advisory snapshots and declarative
  adapter descriptions). An oversized declarative input HOLDs. Current candidates
  verify live pins; unavailable historical cache entries display **artifact not
  retained, pinned by digest**, a non-green status rather than a new failure or
  fabricated pass of the original attempt.
- **D2:** plan rows reference registry entry IDs, with explicit empty lists for
  tool-free rows. Registry entries never reference rows. Reject dangling or
  unused references; entries used transitively through dependencies count as used.
- **D3:** M2 adds a read-only operator preflight command using the same evaluator,
  proposed as `close acceptance preflight --plan PLAN --cache-root ROOT [--json]`.
  It prints outcomes/codes, exits zero only when all required entries pass, and
  writes no store data or close record.
- **D4:** the private cache locator belongs only in private route/bundle records,
  never a sanitized/public summary, publishable hold detail or changelog. M1's
  input errors suppress OS path details; M2/M3 must test sanitized output too.
- **D5:** M3 adds an operator staging walkthrough with synthetic pinned JDK,
  checker jar and expiring snapshot, full registry/layout and success/failure
  output. No downloads or real staged-tool launches in tests.
- **D6:** stop after every milestone for a cold read. Final pre-merge review is a
  cross-vendor Opus sweep; keep milestone diffs well below about 1,500 changed
  lines excluding test tables. No PR before the final milestone's read.

M1b decisions from the M1 cold read (`tk-8039097e0029`):

- **F1:** each toolchain/service has a required exact `expected_banner`. Environment
  runtime/compiler/package_manager/services are lists of entry IDs (empty means
  absent); lists allow multiple runtimes such as Java and Node. Each toolchain or
  service appears exactly once across those roles. The first three roles accept
  toolchains only, services accepts services only. No free-text substitute and
  no unreferenced entry is allowed. Checker expected_banner is explicitly null.
- **F3:** large advisory content is a distribution pin. Its small retained snapshot
  manifest pin adds `distribution:{id,sha256}` and `expires_at`. The reference must
  name a distribution and match its digest. The retained registry binds this
  manifest metadata and the exact manifest bytes; the distribution is not copied.
  M2 must apply the manifest's expiry to **every** entry consuming that distribution,
  including through inputs, templates and transitive dependencies, even if that
  entry does not separately list the snapshot. Multiple manifests all constrain
  the distribution; none overrides another. M1 validates bindings, not freshness.
- **F7:** proof markers match whole lines exactly after stripping the trailing
  CR/LF terminator only. No substring matching, whitespace trimming or Unicode
  normalization. Containment between different markers is valid. M2 implements
  this interpretation; M1 validates distinct bounded literals only.
- **F2/F9:** M3's first commit must consolidate schema dispatch through one helper
  rejecting unknown schemas, with a test walking all comparison sites below.
  M2's operator refusal for a linked/unresolved staging root must tell the operator
  to pass the fully resolved path (including platform temporary aliases and cloud
  placeholders), without echoing the locator. Neither feature is enabled in M1b.

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
| Which rows require which tools | D2 overrides the original bidirectional proposal: plan rows alone contain `registry_entries`. Empty is explicit. Validate references and dependency closure; reject unused entries. Registry remains reusable across plans. |
| Cache root discovery and portable locations | Proposed `--acceptance-cache-root` at open names an explicitly approved private root; entries use only cache-relative regular-file paths. Store the private root locator in the route. No ambient PATH discovery, user cache guessing, URL opening, archive extraction or silent fallback. |
| Transitive dependency completeness | Validate the declared finite dependency graph, all references and staged hashes; reject duplicate/conflicting pins, dangling edges and cycles for this increment. Retain package manifests/lockfiles as bounded inputs. Do not invoke a package manager or pretend declared closure proves an undeclared runtime dependency cannot exist. |
| Large packages/directories and retention | D1 overrides full binary retention: distribution files are pinned and live-verified, never copied. Explicit bounded declarative files are retained; no glob/directory manifest language. Historical unavailable distributions have the visible non-green D1 status. |
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

## M1 implemented record contract

The preceding step-0 evidence is historical. D1–D6 now settle its open decisions.
M1 adds `acceptance_registry.py` with no callers in production CLI/verdict paths.
`policy(plan_bytes, registry_bytes)` strictly decodes both inputs, validates the
new policy and compares the registry's exact-byte hash to `registry_digest`.
Legacy `acceptance.validate_plan`/`validate_registry` still reject these versions.
No route schema has been enabled and M1 cannot create a new kind of GO.

All fields below are required; nullable cases are explicit. Dictionaries reject
extra fields. IDs follow existing acceptance ID rules; hashes are lowercase
64-character SHA-256. Schema versions require actual integers, not booleans or
floating-point equivalents. The byte decoder rejects duplicate keys at all
levels, invalid UTF-8, BOMs, unpaired surrogates and non-finite numbers.

| Record | Closed shape and interpretation |
| --- | --- |
| Registry v2 | `schema_version:2`, `entries:Entry[]`, `files:FilePin[]`. Empty lists are valid only together with explicitly tool-free plan rows and empty environment role lists. IDs are unique within each namespace. File paths are unique after NFC normalization then case folding, with file/directory prefix conflicts refused. |
| FilePin | `id, role, path, sha256, size, expires_at, version, provenance`. `size` is an integer byte count. `role` is distribution/manifest/lockfile/config/proof-log/banner/provenance/verification/snapshot/adapter. Distribution and snapshot require exact opaque `version` and a Provenance object; other roles require both null. Only snapshot has a UTC `expires_at`; others require null. Snapshot also requires `distribution:{id,sha256}`, binding one distribution's ID and exact digest; this extra field is forbidden on other roles. All pins must be referenced. Package distributions are file pins in an entry's inputs, with their own version/provenance. |
| Entry | `id, kind, version, artifact, dependencies, inputs, snapshots, provenance, command, offline, failure_policy, measurement, expected_banner`. Kind is toolchain/checker/service. Toolchain/service expected_banner is a required bounded exact literal; checker requires null. Artifact references a distribution pin whose version/provenance must match this entry. Dependencies reference other entry IDs; inputs reference pinned files, including declared transitive package files; snapshots reference only snapshot pins. No row IDs. |
| Provenance | `source, retrieved_at, checksum_source, independent_verification, record, verification`. Coordinates are inert nonempty strings. `record` references a provenance-role file. Independent verification is a strict boolean; true requires a verification-role file reference, false requires null. UTC retrieval time is mandatory. Same-source checksums remain provenance only; M1 does not authenticate claims. |
| Command template | `argv:string[], cwd, inputs:FileID[], outputs:string[]`. First argv item is exactly `{artifact}`; cwd is `{checkout}`. Allowed whole-token placeholders are artifact/checkout/scratch/cache_overlay. The latter three also allow a safe relative suffix. Other tokens are scalar arguments with no braces, slash, backslash or colon. Outputs are unique `{scratch}/relative` paths. No shell grammar, substitution, path search or execution. |
| Offline policy | `mode, positive_control, cache_hit, real_fetch`. Mode is external-denial/offline-recipe. Markers are distinct bounded literal strings, not regex/code; match whole lines after removing CR/LF terminators only. Offline-recipe requires cache_hit; external-denial requires it null. Policy describes expectations only; M2 interprets retained logs. |
| Failure policy | Exactly `{unavailable:not-run, mismatch:fail, expired:not-run, absent_proof:not-run, attempted_fetch:fail}`. No caller-defined fallback/acquisition or success policy. |
| Measurement pins | Checker requires `{comparator, parser, config, normalizer}` with adapter-role references for comparator/parser/normalizer and config-role for config. Other entry kinds require null. Adapters are bounded declarative descriptions in M1, never executable plugins. |
| Environment v1 | `schema_version:1, runtime, compiler, package_manager, services, os, locale, timezone, environment_digest, config_digest, scratch, cache_overlay, service_data, time_limit_seconds, memory_limit_bytes, row_overrides`. Runtime/compiler/package_manager/services are entry-ID lists, each bounded to 64. Every toolchain/service must be referenced exactly once across the roles (F1), including in observations. OS/locale/timezone are nonempty exact strings. Scratch/cache_overlay/service_data are respectively isolated/fresh-writable/fresh. Digests bind external environment/config inputs. |
| Row override | `{id, environment:EvidenceRef}`; id references a plan row. The external environment bytes are pinned separately, preventing a self-hash cycle. Nested overrides in loaded override environments will be refused using `validate_environment(..., overrides=False)`. Loading/comparing these bytes is M2, not a M1 pass. |
| Plan v4 | Existing schema-3 plan fields plus `environment:Environment`; every row adds `registry_entries:EntryID[]`. Validate the old plan rules on a copy projected to schema 3, preserving the original. Entry use includes reachable dependencies; reject any unused entry or dangling row/service/override reference. Do not guess undeclared tools. |
| EvidenceRef | `{path, sha256, size}` for bounded declarative bytes relative to their approved input root. Shape validation does not substitute for later retained-byte validation. |
| Observation v1 | `schema_version:1, registry_hash, observed_at, environment:Environment, entries:ObservedEntry[]`. Require exactly the registry entry-ID set; supplied data is separate from registry bytes to avoid circular hashes. M2 checks the hash and planned/observed equivalence; M3 wraps capture in attempt/run binding. |
| ObservedEntry | `{id, version, banner:EvidenceRef, offline:OfflineProof}`. A mismatch is still a validly shaped observation; only the evaluator decides HOLD. |
| OfflineProof | `{mode, egress_denied, positive_control, cache_hit, attempted_fetch, endpoints, log:EvidenceRef}`. Flags are strict booleans. Each endpoint is `{host,port,pid,owned}` with nonempty host, port 1–65535, positive PID and boolean owned. False controls, external hosts and attempted_fetch=true are valid observations, never a M1 success claim; their required HOLD semantics and evidence/log cross-checks belong to M2. |

UTC timestamps use exactly `YYYY-MM-DDTHH:MM:SSZ`, rejecting offsets, fractions,
invalid calendar dates and leap-second spelling. M1 checks representation only;
M2 will compare retrieval/observation/expiry against one injected decision clock.
Exact versions are opaque identifiers, not package constraints; no semantic
version solver or implicit normalization is introduced.

### Visible limits and path policy

| Limit | M1 value |
| --- | ---: |
| Each JSON or retained declarative input | 1 MiB |
| Aggregate registry declarative inputs / observation banner-and-log bytes | 16 MiB each |
| Entries / observed entries / service banners / endpoints per proof | 64 each |
| Files / plan rows / references per list / environment overrides | 256 each |
| Total entry-dependency edges | 1,024 |
| JSON nesting and entry dependency chain depth | 24 |
| JSON nodes (including object keys) | 8,192 |
| Argv items / output paths | 64 each |
| Portable relative path | 512 characters |
| Literal offline marker | 256 characters |
| Other nonempty text | 4,096 characters |
| Declared distribution byte count | 0 through signed 64-bit maximum (metadata only in M1) |
| Time limit / memory limit | 1–86,400 seconds / 1 byte–1 TiB |

JSON byte/depth/node limits are checked before allocating the parsed tree.
Graph, reference and declared file budgets are checked before staged reads.
`read_declarative_inputs` also limits actual reads per file and in aggregate,
regardless of dishonest declared sizes. It reads only non-distribution roles and
returns bytes; it performs no hashing verdict, retention write, acquisition,
process spawn or socket operation. M2/M3 must verify hashes before using/retaining
those bytes. Streaming live distribution hashing and its resource limits remain
M2 work; signed-64 metadata support is not a promise to scan an arbitrary size.

Paths use forward-slash relative components only. Reject empty/dot/parent
components, drive/UNC/absolute syntax, backslashes, control characters, wildcard
or stream syntax, Windows device names and trailing dots/spaces. Reject links
and reparse points along the approved root's ancestors and the relative path;
declarative reads require regular files. Public errors contain fixed descriptions,
not OS exception text or the private root. As accepted, supported store locks do
not protect against concurrent manual cache mutation; staging must be quiescent.

M1b extends portable device refusal to the `ntpath.isreserved` device rules,
plus COM0/LPT0, superscript 1/2/3 port digits and CONIN$/CONOUT$. The implementation
works on Python 3.10, where `ntpath.isreserved` is unavailable. Prefix conflicts
and NFC/casefold collisions are checked without changing the actual staged path.
Invalid IDs produce fixed bounded diagnostics, even for a 200,000-character input.
Fixed OS refusals are raised outside their exception handlers with `from None`,
so both the rendered chain and the stored exception context omit private OS text.

Declarative files are lstat-checked, opened once, and fstat-checked against the
earlier device/inode/mode before reading. The leaf is lstat-checked again to reject
a Windows link to the original inode. POSIX uses O_NOFOLLOW and O_NONBLOCK to
reject leaf links and avoid blocking on a substituted FIFO. Platforms without
these flags cannot provide the same kernel-level protection; comparisons use
the identity fields the platform reports (zero/missing identifiers cannot prove
identity). Parent-directory ABA changes and in-place writes remain within the
cooperative mutation boundary. No claim of an adversarial filesystem sandbox is
made; M2 still verifies the bytes against their fixed digest.

### Existing and future reader inventory

| Reader or writer | M1 disposition and M3 obligation |
| --- | --- |
| `acceptance.validate_plan`, `validate_registry`, `prepare`, `freeze`, `_route`, `_policy`, `_bundle`, `attach`, `resolve`, `ack_binding`, `evaluate` | Unchanged; old version gates reject v4/v2. M3 must dispatch explicitly, bind cache locator/preflight report, preserve legacy rules and require evaluated preflight in the pure fold. |
| CLI `cmd_close` acceptance/check/publish branches, `_build_dod_eval` and release-barrier paths | Unchanged. M2's operator command will call the same standalone evaluator without Store construction/writes; M3 open/check/publish use transactional capture and the existing shared writer lock. |
| `acceptance_history.successor`, `evaluate`, `apply_coverage`; `acceptance_coverage.history` and predicate/coverage comparisons | Unchanged. Carry row entry requirements and original preflight failures; route/schema changes cannot discard LD2 approval or protected coverage. |
| `acceptance_obligations` source discovery/capture/evaluation/evidence | Unchanged. New report/input hashes must enter retained source projections and inherited substantive obligations independent of attempt-open order. Distributions get D1's explicit historical status, not fabricated retained bytes. |
| `acceptance_audit.lineage`, `provenance`, `check_delivery`, exposure checks | Unchanged. Include new actor/evidence attribution and withheld hashes in history/cold checks; avoid delivering author preflight claims to a fresh cold reviewer. |
| `acceptance_cold.policy`, `binding`, submit/reveal/reconcile/evaluate | Unchanged. Schema-4 binding and seal invalidation remain mandatory; new input/report hashes cannot be appended silently after commitment. |
| `acceptance_hygiene.binding`, execution/final manifests, execution/evaluate | Unchanged. Manifest includes retained declarative inputs and reports, excludes binary content under D1 and never includes its own result hash. Run/reproduction environment/proof records remain bound. |
| `close.evaluate_dod`, `compute_verdict`, persistence/transactions, `record_publish`, signoff/ack handling | Unchanged. Additive direct HOLDs, generation/instance binding and total lock order remain authoritative. No bypass through green gate labels or cached projections. |
| CLI list/show/published holds, `attention.close_hold_items` and final/barrier consumers | Unchanged. Future sanitized outputs whitelist IDs/status/codes and omit cache locator. Raw private records remain explicitly private. |
| `acceptance_git` command/operation scope | Unchanged. New staged-file or proof results are never memoized as immutable Git metadata. |
| New `acceptance_registry` API | Called only by M1 tests at this milestone. Uses existing acceptance strict-object, ID/hash, decoder and path primitives. M1b bounds the shared ID refusal diagnostic; legacy validation outcomes and persistence behavior are unchanged. |

Planned M3 envelopes: schema-4 route extends schema 3 with private `cache_root`
and a retained `preflight_hash`; bundle carries its observation/report and input
manifest references. Reports bind instance/attempt/project/revision/plan/registry
without embedding their own hash. Exact envelope grammar and current-run binding
will be recorded with M3 before enabling those readers. They are unsupported now.

#### Concrete schema-dispatch inventory (F2)

Locations below refer to the M1b source, with unchanged surrounding logic. This
includes the review's narrow schema-3 sites and the additional version predicates
found by scanning every `acceptance*.py` module and `close.py`. They are not made
schema-4 aware in this round. M3 must address them in its **first commit**, using
one validated dispatch helper and a regression that walks the comparison sites;
simply broadening `== 3` to `>= 3` would accept unknown schemas and is forbidden.

| File:line | Predicate and meaning |
| --- | --- |
| acceptance.py:202 | Plan `==3`: include cold-policy fields in closed shape. |
| acceptance.py:206 | Plan `==3`: validate cold policy. |
| acceptance.py:215 | Plan `>=2`: require nonempty authors. |
| acceptance.py:330 | Plan `>=2`: verify project identity. |
| acceptance.py:332 | Plan `==3`: verify cold change base. |
| acceptance.py:346 | Plan `==3`: add the required final cold-review lens assignment. |
| acceptance.py:378 | Plan `>=2`: freeze parent/amendment fields. |
| acceptance.py:381 | Plan `==3`: freeze cold and obligation hashes. |
| acceptance.py:385 | Plan `==3`: capture inherited obligations. |
| acceptance.py:396 | Route `in (2,3)`: include parent/amendment in route shape. |
| acceptance.py:397 | Route `==3`: require cold/obligation route hashes. |
| acceptance.py:406 | Route `>=2`: validate paired parent/amendment references. |
| acceptance.py:438 | Plan/route version equality: frozen policy binding. |
| acceptance.py:444 | Route `>=2`: verifier/reproduction bundle and run fields. |
| acceptance.py:445 | Route `==3`: recovery approvals/hygiene/environment/offline fields. |
| acceptance.py:527 | Route `==3`: cold commitment must precede bundle attachment. |
| acceptance.py:534 | Route `==3`: reproduction lenses on attachment. |
| acceptance.py:550 | Route `==3`: bind recovery approvals to actual operator records. |
| acceptance.py:602 | Route `==1`: force live Git verification for legacy route. |
| acceptance.py:605 | Route `>=2`: recheck verified project identity. |
| acceptance.py:649 | Route `>=2`: reproduction, ack and history checks. |
| acceptance.py:655 | Route `==3`: inherited obligations, cold sweep and hygiene fold. |
| acceptance.py:691 | Route `==3`: cold/obligation hashes in ack binding. |
| acceptance_cold.py:91 | Route `!=3`: reject cold commitment on unsupported route. |
| acceptance_obligations.py:116 | Route `==3`: check source execution hygiene for inherited obligations. |
| acceptance_history.py:109 | Route `not in (2,3)`: successor eligibility. |
| acceptance_history.py:121 | Proposed plan version `<` parent route version: prohibit downgrade. |
| acceptance_history.py:175 | Amendment version `in (2,3)`: assertion_changes shape. |
| acceptance_history.py:209 | Route `<3`: legacy parent-counter inheritance versus modern obligation fold. |
| acceptance_history.py:216 | Amendment version `==3`: exact assertion-change comparison. |
| close.py:1740 | Route `in (2,3)`: attach acceptance binding to lens acknowledgement. |

Explicit version gates also remain in `acceptance._version` and callers:
plan/registry/route/bundle/raw/reproduced-raw, cold policy and reconciliation,
hygiene, operator approval/amendment, and the new registry/plan/environment/
observation readers. `acceptance_registry.validate_plan` deliberately projects
v4 onto a **copy** with v3 for old row rules; that is validation reuse, not route
dispatch. `close.py:462` validates the close envelope version; `:607` validates
signoff-policy version type and `:786-788` the DoD-policy version. Those separate
schema namespaces must not be confused with acceptance route versions. No
version comparison occurs in the remaining acceptance modules (audit, coverage,
Git or hygiene beyond its explicit version gate).

### M1 executed evidence

The required `test_preflight_path_escape_rejected` first failed with **1 failed**
(`DID NOT RAISE AcceptanceError`, 0.14 s), using the existing path helper through
the new staging API. It models a linked ancestor above the approved root without
requiring host symlink privileges. The new ancestor check makes it pass. A
separate real-symlink case is skipped when the host denies symlink creation.

Final foreground command, Python 3.10, `PYTHONPATH=<checkout>/src` and
`PYTHONDONTWRITEBYTECODE=1`:

```text
py -3.10 -m pytest tests/test_acceptance_registry.py tests/test_acceptance_git_reads.py tests/test_acceptance.py::test_acceptance_acks_without_bundle_hold tests/test_acceptance.py::test_acceptance_invalid_plan_refused_before_close_creation tests/test_acceptance.py::test_acceptance_schema3_cannot_attach_without_required_hygiene -q -p no:cacheprovider --basetemp <scratch>/final
```

**160 passed, 1 skipped in 10.63 seconds**. The M1 file contains 132 cases, with
131 passing and the host-restricted symlink skip. It uses synthetic files and no
Git repositories. The selected existing integration cases use the session Git
templates. No full suite, downloads, network requests or staged tools ran.

```text
py -3.10 -m ruff check --no-cache src/agenttalk/acceptance_registry.py tests/test_acceptance_registry.py
py -3.10 -m bandit -q src/agenttalk/acceptance_registry.py
py -3.10 -m bandit -q -s B101 tests/test_acceptance_registry.py
```

Ruff passed. Both Bandit invocations exited zero with no findings; the test scan
excludes only intentional assertions. No subprocess code was added to production.
Local links, required-test inventory, patch whitespace and the privacy sweep with
positive controls passed before publication. Scratch logs and isolated fixture
directories are retained for the cold read.

M1 is ready for the milestone cold read, not acceptance GO. No M2
evaluator/command, binary hashing, proof interpretation, sanitized summary,
retention capture or M3 route/bundle integration is claimed. The remaining five
named design tests must still be shown red before their M2 guards are built.

### M1b executed evidence and limits

Before production edits, the new contract/reader regressions ran with **16 failed,
132 deselected in 0.45 s**. Failures demonstrated missing banner/mapping and
snapshot-binding support, accepted reserved names/prefix/normalization collisions,
an unbounded ID diagnostic, unchecked opened-file identity and exposed OS context.
Subsequent tests cover all F4 guards, a real Windows junction above the staging
root, file replacement before open, device/inode/type mismatch, environment role
limits and whole-line marker containment. Existing tests were updated only for
the explicitly changed grammar and open API; refusal assertions remain in place.

Final targeted foreground command (Python 3.10, checkout `src` on PYTHONPATH,
bytecode disabled, isolated scratch):

```text
py -3.10 -m pytest tests/test_acceptance_registry.py tests/test_acceptance_git_reads.py tests/test_acceptance.py::test_acceptance_acks_without_bundle_hold tests/test_acceptance.py::test_acceptance_invalid_plan_refused_before_close_creation tests/test_acceptance.py::test_acceptance_schema3_cannot_attach_without_required_hygiene -q -p no:cacheprovider --basetemp <scratch>/final
```

**216 passed, 3 skipped in 10.72 seconds**. The registry file accounts for
187 passes: one real-symlink test is host-restricted; two POSIX FIFO/link-swap
tests are skipped on Windows. The Windows junction case passed. No full suite,
network, downloads or staged-tool launches ran. The junction fixture uses only
the fixed OS `mklink /J` helper with isolated temporary paths.

The supplied reviewer scripts were absent from the originally named location;
they were located in the reviewer's checkout and left unchanged. Original M1
source/tests were copied to isolated scratch before baseline mutation runs.
All mutants run one at a time in scratch, with source restored after each run.

| Mutation run | Killed | Survived | Text anchors not found |
| --- | ---: | ---: | ---: |
| Original `mutate_m1.py`, original M1 code/tests | 64 | 33 | 0 |
| Original runner, initial M1b code/tests | 84 | 7 | 6 |
| Same runner, final M1b, six updated anchors | 92 | 5 | 0 |

Updated anchors are M22/M94 (fixed refusal construction), M52 (NFC plus casefold),
M68/M72 (environment entry lists and role type), and M90 (lstat before open).
The original runner logic and mutation intent are preserved; skipped anchors are
not counted as killed. All **27** lead-listed gaps now kill their mutants:

```text
M20 M69 M55 M29 M25 M37 M38 M39 M05 M46 M47 M08 M09 M90
M12 M14 M43 M51 M66 M67 M70 M82 M85 M86 M87 M94 M97
```

Five remaining mutants are disclosed, not counted as covered: M16 removes an
explicit dot-component guard while trailing-dot refusal still rejects those
paths; M36 removes template-input lookup while the aggregate reference set still
refuses missing pins; M59/M61 remove one of the complementary graph cycle/depth
checks while the others still refuse; M91 changes the per-read remaining-budget
cap but the aggregate post-read check still refuses oversized totals. M91 can
read up to the per-file cap before refusal, so this is not a proof of identical
resource cost. No new bypass was demonstrated by these five mutations.

The unchanged `probe_m1.py` cases A/B/D/E/F/G/I were run on baseline and M1b;
both completed after supplying the required scratch parent. The initial runs
stopped at case I's missing scratch parent and are retained as superseded harness
logs. The final probes confirm path/device refusals, 3,000 JSON fuzz cases with
zero undercount bypasses, 43 hostile-scalar cases with zero unstructured refusals,
the 200,000-character ID producing a 21-character diagnostic, and bounded reads.
Old service-object syntax in one E probe is now deliberately invalid; positive
new-shape service and multi-runtime cases are repository tests. H's junction
behavior is covered by the repository test rather than the probe's shell cleanup.

```text
py -3.10 -m ruff check --no-cache src/agenttalk/acceptance_registry.py src/agenttalk/acceptance.py tests/test_acceptance_registry.py
py -3.10 -m bandit -q src/agenttalk/acceptance_registry.py src/agenttalk/acceptance.py
py -3.10 -m bandit -q -s B101 tests/test_acceptance_registry.py
```

All three checks exited zero, with no Ruff/Bandit findings. Bandit emits existing
suppression-comment warnings in acceptance.py; no suppression was added there.
Patch whitespace and a privacy scan with positive controls are required before
push. Scratch baseline/after copies, mutation scripts, fixture directories and
logs are retained under the M1b task scratch for the short delta read.

M1b remains an uncalled reader milestone. M2 evaluation, expiry propagation,
banner/log comparisons and operator command, and M3 schema dispatch/GO integration
remain pending their respective authorization and cold reads.

### M1c: delta-read fixes before M2

The lead authorized M2 in `tk-8d776f7f6aa2`, with this small prerequisite commit.
The non-regular-file test now patches `Path.lstat` directly, avoiding Python
3.14's changed relationship between `stat` and `lstat`. New tests cover snapshot
reference shape/role (N12/N13), cross-role duplicate and unknown entry IDs
(N21/N22), checker banner prohibition (N06), post-open identity (N29) and descriptor
closure on refusal (N35). Null/extra-field references and unknown IDs already
produce structured refusals in the unmutated reader; the reported crashes arise
when their guards are removed. Tests now distinguish those regressions.

Invalid ID refusals remain value-free, now naming a caller-supplied field/position
and the fixed ASCII alphanumerics plus `. _ -`, maximum-64-character rule, including
the initial-alphanumeric requirement. The new diagnostic test failed first
(1 failed, 197 deselected), then passed. Registry reference lists supply indices;
shared indexed records supply record positions. This adds no input-value echo.

Foreground `py -3.10 -m pytest tests/test_acceptance_registry.py -q -p no:cacheprovider`
and the same command under `py -3.14`, each with its own scratch basetemp, both
completed: **195 passed, 3 skipped** (0.46 s / 0.48 s). The skips are the existing
host-restricted symlink and two POSIX leaf-swap cases. N27 (Windows post-open
reparse substitution) lacks a deterministic real substitution witness; N33/N34
(POSIX no-follow/nonblocking flags) are platform-limited on this Windows host.

M2 mapping decision F6: import-time unsafe/racing policy files remain
`acceptance_policy_invalid`. A staged file changing while opening during
evaluation becomes `acceptance_preflight_unavailable`, with `not-run` and retry
allowed. M2 uses an explicit evaluation boundary for this mapping.

## M2: read-only evaluator and operator preflight

Authority: `tk-8d776f7f6aa2`. M1c is commit `9b6d5d4`; its seven targeted
reviewer mutations (N12/N13/N21/N22/N06/N29/N35) were all killed, with zero
survivors or skipped mutations. M2 adds no acceptance GO path. The schema-dispatch
helper/inventory walk remains M3's first commit, followed by route, retention and
open/check/publish integration. Stop here for the required cold read.

### Decisions and public contract

- **F4 banner:** an expected literal must equal any complete captured line after
  removing only its CR/LF terminator. No whitespace trimming, regex interpretation,
  Unicode line-separator splitting or first-line restriction. Offline markers use
  the same rule. Captures must decode as strict UTF-8.
- **F3 freshness:** requiring a freshness manifest remains author-declared under
  LD1. Every snapshot must bind a distribution's ID and digest, and at least one
  entry must both list that snapshot and consume the distribution (possibly
  transitively). Import refuses decorative/unconsumed bindings. Evaluation applies
  the manifest's expiry and evidence failures to every consumer of its distribution.
  Distributions without manifests remain valid, for example a pinned JDK.
- **F6 races:** evaluation maps an unsafe or changing staged file to
  `acceptance_preflight_unavailable` / `not-run`; retry is allowed. Policy import
  retains `acceptance_policy_invalid`. Both use the same single-open, identity-
  checked reader. Missing proof is non-green; no acquisition is attempted.
- **D4/F9 privacy:** reports contain IDs, hashes, statuses and fixed diagnostics,
  without cache locators, relative file paths, banners or proof-log content.
  Unsafe-root messages tell the operator to pass a fully resolved path without
  links or reparse ancestors, including aliased temporary roots and placeholders.
  M2b limits that advice to an actually detected link/reparse point; missing files
  and identity races receive ordinary unavailable diagnostics.
  Caller-labelled ID errors state the fixed rule without printing submitted values.

The operator stages files and captures observations separately, then runs:

```text
agenttalk close acceptance preflight --plan plan.json --cache-root staged --observation observation.json --json
```

The registry locator is relative to the plan's directory. Registry file pins and
planned environment overrides are relative to the cache root. Banner/proof files
and observed environment overrides are relative to the observation's directory.
Omitting `--observation` reports missing proof when entries require it. Omitting
`--json` prints readable entry statuses and hold codes. Exit 0 means every required
entry passed this preflight; any failure/refusal returns 3. This is a staging check,
not an acceptance verdict. The handler does not construct a Store, create a close,
write evidence, invoke Git, open sockets or launch a staged tool.

`acceptance_preflight.evaluate` accepts plan/registry bytes, a cache root, optional
observation bytes/proof root and an injected UTC whole-second decision clock. The
version-1 result contains plan/registry/observation hashes, decision time, aggregate
status, per-entry/per-row holds, and per-file expected digest/size/status/retention
classification. Distribution bytes are streamed and discarded, never copied into
retained evidence. Declarative bytes are bounded reads; durable retention and the
historical missing-cache display remain M3 work.
Each hold has a public `ref`: the registry pin ID for file failures, the entry ID
for its banner/offline proof, the row ID for overrides, or the fixed `preflight`
scope for failures before a specific pin is known. Text output prints `[ref]`.

| Condition | Code | Outcome |
| --- | --- | --- |
| Missing/unreadable/racing staged input or distribution read budget | `acceptance_preflight_unavailable` | not-run |
| Pin, version, banner, chronology or environment mismatch | `acceptance_preflight_mismatch` | fail |
| Snapshot expired at the decision instant | `acceptance_snapshot_expired` | not-run |
| Absent positive control, recipe cache-hit proof or observation | `acceptance_offline_unproven` | not-run |
| Attempted fetch, allowed external egress, external/unowned endpoint | `acceptance_offline_violation` | fail |
| Corrupt/malformed supplied proof, invalid override, oversized declarative input | `acceptance_record_missing` | fail |
| Observation bound to another registry | `acceptance_plan_stale` | fail |
| Invalid imported policy | `acceptance_policy_invalid` | refusal |

All registry pins, including distributions, are hashed at evaluation time within
the read budgets. Every invocation rereads current bytes and reads the clock once.
Expiry is inclusive (`decision >= expires_at`); provenance retrieval cannot follow
the observation, and observation time cannot follow the decision. Planned and
observed environment records compare exactly; referenced overrides must also have
valid bytes and shapes. Dependency proof failures propagate to consumers and rows.
The standalone operator command requires all required entries to pass even for an
informational row; M3 will apply the existing row-policy fold for close decisions.

External-denial and offline-recipe modes validate supplied evidence only. A positive
control requires both its observation flag and exact log line; recipes additionally
require the cache-hit flag and line. Only external-denial requires `egress_denied`;
offline-recipe relies on its own control/cache-hit evidence. A fetch flag or exact
fetch marker fails in both modes.
Endpoints require literal loopback IP addresses and declared ownership; unresolved
hostnames, scoped addresses, external addresses and unowned endpoints fail. These
are cooperative declarations, not a network sandbox or process-ownership attestation.
IPv4-mapped IPv6 addresses are classified using their mapped IPv4 address, so
supported Python versions agree on the same bytes.
Provenance and adapter/config files are validated and digest-bound data, never run.

Additional evaluator limits: 2 GiB per distribution, 8 GiB total distribution
bytes, streaming chunks at most 1 MiB. M2b reads a distribution only up to its
declared size plus one sentinel byte; excess bytes immediately yield a mismatch.
Each declarative file and each imported
plan/registry/observation record is at most 1 MiB. Registry declarative files,
planned override evidence, and observed override/banner/proof evidence each have
a separate 16 MiB aggregate budget. Exceeding a budget refuses green status;
it never fetches, truncates into a successful proof, or launches a verifier.

### Executed evidence

The five design tests were written against an optimistic evaluator stub first.
They produced **6 failed** assertions (changed-byte/missing-dependency is two
cases), without setup or collection failures, before evaluator implementation:

| Test | Red failure | Green / independent mutation |
| --- | --- | --- |
| `test_preflight_changed_byte_or_missing_dependency_fails` | missing refusal for changed/missing bytes | both pass; pin-check mutant killed |
| `test_preflight_expired_snapshot_holds` | missing expired hold | passes; expiry mutant killed |
| `test_preflight_offline_positive_control_required` | missing proof hold | passes; positive-control mutant killed |
| `test_preflight_loopback_allowed_egress_denied` | external endpoint accepted | passes; endpoint mutant killed |
| `test_preflight_toolchain_drift_holds` | changed banner accepted | passes; banner mutant killed |

All five independent in-memory mutations were killed, zero survivors. Two further
wrong-type plan cases first failed with `TypeError` and now give structured policy
refusals. Additional tests cover multi-line banner matches/near-misses, exact-line
markers, malformed/integrity evidence, dependency/expiry propagation, environment
overrides, limits, clock boundaries, safe opening, privacy and CLI read-only behavior.
The existing path-escape design test remains in the registry suite.

Final foreground commands, each with a separate task-scratch `--basetemp`, source
`PYTHONPATH` and `PYTHONDONTWRITEBYTECODE=1`:

```text
py -3.10 -m pytest tests/test_acceptance_preflight.py tests/test_acceptance_registry.py tests/test_acceptance_git_reads.py tests/test_acceptance.py::test_acceptance_acks_without_bundle_hold tests/test_acceptance.py::test_acceptance_invalid_plan_refused_before_close_creation tests/test_acceptance.py::test_acceptance_schema3_cannot_attach_without_required_hygiene -q -p no:cacheprovider
py -3.14 -m pytest tests/test_acceptance_preflight.py tests/test_acceptance_registry.py tests/test_acceptance_git_reads.py tests/test_acceptance.py::test_acceptance_acks_without_bundle_hold tests/test_acceptance.py::test_acceptance_invalid_plan_refused_before_close_creation tests/test_acceptance.py::test_acceptance_schema3_cannot_attach_without_required_hygiene -q -p no:cacheprovider
```

Results: **281 passed, 3 skipped** on each interpreter (13.10 s / 13.71 s).
Skips are the same host-restricted symlink and two POSIX-only cases described in
M1c. Windows junction and the corrected non-regular-file test pass on both.
No full-suite run, real download or staged-tool launch was performed.

```text
py -3.10 -m ruff check --no-cache src/agenttalk/acceptance.py src/agenttalk/acceptance_history.py src/agenttalk/acceptance_registry.py src/agenttalk/acceptance_preflight.py src/agenttalk/cli.py tests/test_acceptance_registry.py tests/test_acceptance_preflight.py
py -3.10 -m bandit -q src/agenttalk/acceptance.py src/agenttalk/acceptance_history.py src/agenttalk/acceptance_registry.py src/agenttalk/acceptance_preflight.py src/agenttalk/cli.py
py -3.10 -m bandit -q -s B101 tests/test_acceptance_registry.py tests/test_acceptance_preflight.py
```

All exit zero with no findings. Bandit emits existing suppression-comment warnings
in acceptance/CLI; test assertions are intentionally excluded from B101.
Task scratch is retained for review: red/green fixtures, M1c mutation copy/log,
the five M2 mutation scripts/logs and isolated test roots. No private locator is
included in public documentation or report output.

## M2b: cold-read corrections

Authority: `tk-046fd6cf218f`; base `6fbd1e9`. This delta implements F1–F5/F7
and records F6/F8 for M3. M3 remains unstarted pending the short delta read.

| Finding | Change and evidence |
| --- | --- |
| F1 interpreter-dependent mapped loopback | Normalize IPv4-mapped IPv6 before classification. The table retains all 21 reviewer spellings and adds octal/leading-zero/integer controls. The unchanged R2 probe gives identical outcomes on Python 3.10 and 3.14. |
| F2 offline modes | Require denial only for external-denial. Recipe positive-control/cache-hit evidence can pass with `egress_denied=false`; recorded fetch flags or whole-line markers fail both modes. |
| F3 CLI status | Import errors use the evaluator's status mapping; stale/integrity failures print `fail`, invalid policy prints `refusal`, unavailable inputs print `not-run`. JSON tests and unchanged R12 probes cover these outcomes. |
| F4 guard gaps | Tests cover status membership, evaluation race/link refusals, streaming peak memory, separate evidence roots including overrides and CLI, null checker banners, snapshot consumption/dependency/provenance closure, clock types, override errors, bounded CLI diagnostics, non-object plans, OS errors and legacy KeyError translation. |
| F5 diagnostic references | Every hold carries a public `ref`, preserved during deduplication and printed in text mode. Missing files no longer suggest resolving links. R9 confirms missing pin identification without locator, relative-path or banner leakage. |
| F7 bounded distribution reads | Stop at declared size plus one and classify excess as mismatch. A reader spy proves a two-byte pin causes at most a three-byte read; streaming memory stays bounded rather than accumulating distribution chunks. |

### Deferred lead decisions for M3

- **F6 authoring guidance:** the worked staging example must specify banner/log
  captures as strict UTF-8 without BOM. Warn that Windows PowerShell 5.1 `>` writes
  UTF-16. Show PowerShell 7 (`pwsh`) with `Out-File -Encoding utf8NoBOM`; do not
  present that encoding switch as available in Windows PowerShell 5.1. This is
  documentation work for the authorized M3 walkthrough, not a new M2 decoder change.
- **F8 publication cost:** hash staged distributions outside the store-wide
  acceptance lock; inside the lock re-check size and file identity before committing.
  Preserve the cooperative mutation boundary and reject changed inputs. Do not
  hold the store lock while hashing gigabytes. M3's first commit still introduces
  the schema-dispatch helper and comparison-site test before lifecycle integration.

### Red/green and mutation evidence

Before the fixes, the new regression run had **10 failed, 97 passed**: both mapped
loopback spellings on 3.10, valid recipe evidence, four CLI statuses, race advice,
the declared-size read bound, and missing public pin references. All were behavior
assertions, not setup/collection errors. The subsequent targeted tests pass on both
interpreters; no existing assertion was weakened.

The unchanged reviewer `mutate_m2.py` baseline on an isolated copy of `6fbd1e9`
was **51 killed, 32 survived, 0 skipped**. After the fixes and tests:

- First unchanged-script run: **61 killed, 9 survived, 13 exact-text skips**.
- A scratch-only adapter retained the same mutation intent at the 13 changed
  sites: **12 killed, 1 survived, 0 skipped**.
- Additional boundary tests killed seven remaining original mutants; the escaping
  registry-reference test killed C7. Aggregate final original-table evidence:
  **81 killed, 2 survived, 0 unresolved skips**, across these completed runs.
- D7's remaining catch deletion is equivalent: `AcceptanceError` derives from
  `CloseError(ValueError)`, so the unchanged `ValueError` catch still handles it.
  A supplementary mutant that actually re-raises acceptance errors was killed.
  V24 removes a redundant aggregate: import requires file use and per-entry input
  closure already propagates every required pin's holds. Neither survivor is claimed
  as a killed mutant or proof of additional coverage.

The adapter initially had a variable-name error; a boundary rerun initially lacked
its scratch parent. Those invalid runs are retained and excluded from all counts.
Corrected runs completed and restored their scratch sources. Reviewer scripts and
the working source were never mutated by these runners.

Final commands use the same targeted file/node list as the M2 section, source
`PYTHONPATH`, `PYTHONDONTWRITEBYTECODE=1`, and distinct task-scratch basetemps.
Python 3.10: **351 passed, 3 skipped in 15.39 s**. Python 3.14:
**351 passed, 3 skipped in 14.82 s**. The skips remain the host-restricted symlink
and two POSIX-only cases. Both include the complete registry and preflight files,
Git-read regression file and three legacy close smoke tests named above.
Ruff and Bandit cover `acceptance_preflight.py`, `cli.py` and
`test_acceptance_preflight.py` (test assertions excluded with `-s B101`). Negative
all-interface address data has an explicit S104/B104 annotation; it opens no socket.
All three final lint/security commands exit zero with no findings; Bandit emits
existing suppression-comment warnings. Patch whitespace and the added-line privacy
scan pass; the privacy scanner has eight positive controls.
No full suite, downloads or staged-tool launches are used. Task scratch is retained
for the delta reader: baseline/after source copies, mutation/probe logs and isolated
test roots.
