# DESIGN #55 — Local comprehension plane for legacy migrations (rev 1)

Status: rev 1, ready for adversarial review. Date: 2026-08-26.

Audience: agenttalk contributors implementing task #55 and the migration-program
UI in task #208. Their goal is to agree on a small, local contract that gives a
migration fleet a shared picture of a legacy repository without turning
agenttalk into a general code-intelligence product.

This is an explanation/design document. None of the CLI examples below are
implemented or runnable yet.

Field basis: the JAWS migration dry run demonstrated that agenttalk's governance
layer can coordinate a migration, but the fleet lacked a shared, queryable
inventory, dependency graph, and feature map. The existing onboarding ledger
captures inspected evidence and disagreements, but deliberately does not analyze
the repository. Task #55 fills that one gap. Graphify prior-art validation and
the squadmate's additional JAWS-retro requirements are pending at rev 1; this
document makes no Graphify-specific compatibility claim.

## Decision summary

Task #55 adds a local, immutable scan plane under `.agenttalk/comprehension/`.
Each scan publishes versioned JSON artifacts for migration units, direct
dependencies, features and entry points, and evidence-backed readiness signals.
The CLI and task #208 read those artifacts through one validated projection.

The plane is advisory. It does not decide that a feature has parity, a unit is
safe to rewrite, a migration stage is complete, or a release may proceed. It
supplies bounded facts and explicit unknowns to agents, onboarding, work, gates,
and the migration-program UI.

The privacy boundary is hard: scanning, storage, reporting, context-pack
construction, and UI projection are local-only. The implementation must contain
no network path, telemetry, hosted parser, registry lookup, remote embedding, or
LLM summarization. Artifacts and context packs contain pointers and derived
metadata, never copied source text.

## Goal

For a bounded legacy-migration scope, the plane must answer four questions:

1. Which migration units exist, and which source files make up each unit?
2. Which units directly depend on which internal or external targets?
3. Which user-visible features and executable entry points map to those units?
4. Which evidence is present, missing, or blocking before migration work starts
   on each unit?

A lead should be able to scan once, assign work by stable unit or feature ID,
and give every wrapped agent the same evidence snapshot. A later scan should
make repository drift visible instead of silently changing the fleet's shared
picture.

## Non-goals

The comprehension plane is not:

- an IDE index, language server, symbol search engine, or arbitrary graph-query
  service;
- a complete semantic call graph or a proof of runtime behavior;
- a vulnerability scanner, generic code-health score, complexity leaderboard,
  or dependency-upgrade recommender;
- a source-code mirror, full-text index, embedding store, or source excerpt
  cache;
- an automatic architecture, feature, or migration-plan author;
- a rewrite/transpilation engine or a system that edits client code;
- a replacement for onboarding claims, characterization tests, independent
  review, gates, closes, or human decisions; or
- a hosted service, remote dashboard, or multi-project intelligence warehouse.

Fixed migration reports are in scope. An open-ended query language is not. If a
future request does not help bound, plan, execute, or verify a legacy migration,
it belongs outside task #55.

## System boundary

The plane sits between the repository and existing agenttalk workflows:

```text
legacy worktree
      |
      v
bundled local adapters -----> immutable scan run
                                   |       |
                                   |       +----> migration report CLI
                                   |
                +------------------+------------------+
                |                                     |
                v                                     v
       bounded context-pack builder          validated #208 projection
                |                                     |
                v                                     v
       wrapped-agent brief                    local program UI/export

onboarding/config declarations ----> evidence links, never hidden authority

                    no component above may use the network
```

The scanner reads the current worktree, local manifests, and explicitly linked
local agenttalk evidence. It does not fetch Git refs, resolve packages against a
registry, install parsers, call a model, or execute repository code. The first
implementation accepts only bundled, in-process adapters; admitting external
analyzer processes requires a later security design because “installed locally”
does not prove “cannot use the network.”

## Core invariants

1. **Local-only:** every scanned input resolves inside the selected project
   root, and every plane output is written under `.agenttalk/`. No scanner or
   projector code path opens a socket.
2. **Pointer-shaped evidence:** artifacts may store relative paths, symbol and
   interface names, normalized dependency identifiers, line locations, and
   hashes. They must not store source excerpts, comments, string-literal bodies,
   environment values, command output, prompts, or bus-message bodies.
3. **Immutable generations:** a published scan run never changes. Readers bind
   to a scan ID and manifest digest; a new scan creates a new generation.
4. **Unknown is first-class:** unsupported syntax, excluded paths, unresolved
   edges, conflicting detectors, missing evidence, and resource limits remain
   visible. They never collapse to “ready.”
5. **Evidence is not authority:** a scan signal cannot set a gate, close a work
   item, or advance a migration stage. Those workflows may cite it as evidence.
6. **Deterministic projection:** identical inputs, configuration, and adapter
   versions produce identical record IDs, ordering, and artifact contents apart
   from run timestamps and IDs.
7. **Bounded consumption:** CLI reports, UI responses, and agent briefs declare
   truncation and omitted counts. They never silently stuff a repository-sized
   graph into a prompt or browser response.

## Local storage model

The proposed layout is:

```text
.agenttalk/comprehension/
  config.json
  index.json
  scan.lock
  runs/
    <scan-id>/
      scan.json
      modules.json
      dependencies.json
      features.json
      readiness.json
      problems.json
  packs/
    <pack-id>.json
  .staging/
    <scan-id>-<nonce>/
```

`config.json` is an optional, versioned local input. It defines scan scopes,
exclusions, enabled bundled adapters, explicit feature declarations, unit
grouping hints, and the readiness policy. A scan works with conservative
defaults when the file is absent. URLs, remote adapter declarations, commands,
and paths outside the project root are invalid configuration.

`index.json` is the only mutable catalog. It records `latest_scan_id`, the
latest fully complete scan, and a bounded list of run summaries. The scanner
atomically replaces it only after the new run validates. A reader that opened
the old index continues to see a complete old generation during a concurrent
scan.

Each directory under `runs/` is immutable. A scan writes to a uniquely named
directory under `.staging/`, validates all schemas, cross-references, counts,
and digests, renames it into `runs/`, and updates the index last. A crash before
publication therefore leaves either the previous generation or an ignored
staging directory, never a half-current run.

Context packs are immutable local projections bound to one run digest and one
brief scope. They live separately because they can be generated after the scan
and have a different retention lifecycle.

Comprehension data is project memory, like onboarding and knowledge data. The
existing `agenttalk reset` must preserve it. Rev 1 proposes no automatic
deletion; retention and an explicit prune command remain an open review item.

### Common JSON envelope

Every JSON document uses UTF-8, stable key ordering, stable record ordering,
RFC 3339 UTC timestamps, and this common identity:

```json
{
  "schema_version": 1,
  "artifact_type": "agenttalk.comprehension.modules",
  "scan_id": "20260826T091530Z-a1b2c3d4",
  "generated_at": "2026-08-26T09:15:30Z"
}
```

`schema_version` is an integer major version. Readers accept the exact version,
ignore unknown optional fields, and reject missing required fields or a higher
version. A breaking field or semantic change increments the version. A reader
may explicitly migrate an older version in memory, but it never rewrites an
immutable run.

`scan.json` lists every artifact's relative path, SHA-256 digest, record count,
and schema version. All documents repeat the scan ID. A mismatched ID, digest,
count, or cross-reference invalidates the run instead of producing a partial
truth from mixed generations.

All persisted paths use project-relative POSIX spelling. Absolute paths,
`..` segments, NULs, URL-like values, and paths resolving outside the project
root are rejected. Tracked files use Git's canonical spelling when available;
untracked paths retain their on-disk spelling. Case-fold collisions are a scan
problem rather than two silently merged units.

### Scan manifest and problems

`scan.json` records:

- scan ID, status (`complete` or `degraded`), start and completion times;
- generator version and every bundled adapter name/version;
- source state: local VCS kind and revision when available, dirty flag, and a
  SHA-256 fingerprint over the sorted included-file paths and content hashes;
- effective include/exclude rules and their configuration digest;
- artifact paths, digests, schema versions, and record counts; and
- bounded summary counts for units, edges, features, entry points, readiness
  states, exclusions, unsupported files, and problems.

`problems.json` contains bounded, machine-readable problem records. Each record
has a stable reason code, severity, adapter, optional relative path and line,
and a generated message. It never copies parser output wholesale. Examples are
`unsupported_language`, `parse_failed`, `path_excluded`,
`dependency_unresolved`, `feature_conflict`, `resource_limit`, and
`case_collision`.

A run is `degraded` if an enabled adapter fails, an input budget truncates the
scope, or part of the selected source is unsupported. Ordinary unresolved
dependencies and readiness blockers are domain findings and do not by
themselves make the scan command fail. A fatal configuration, confinement, or
publication error publishes no run.

## Artifact 1: module inventory

`modules.json` contains migration units. A unit is the smallest assignable
piece the active adapter can identify reliably: commonly a package, module,
component, service, or file. Unknown languages fall back to file units only
when the scanner can do so without pretending to understand their internals.

Each unit record contains:

| Field | Meaning |
| --- | --- |
| `unit_id` | Deterministic SHA-256 ID over unit kind, normalized path, and qualified name. |
| `kind` | Closed adapter vocabulary such as `service`, `package`, `module`, `component`, or `file`. |
| `display_name` | Bounded derived or declared label; never a source excerpt. |
| `language` | Detected language or `unknown`. |
| `paths` | Sorted relative source paths that make up the unit. |
| `source_digests` | Per-path hashes used for exact relevant-scope freshness checks. |
| `classification` | Set such as `production`, `test`, `generated`, `vendor`, or `infrastructure`. |
| `container_unit_id` | Optional parent unit for hierarchy, with no cyclic containment. |
| `adapter` | Detector name/version and whether the unit was detected or declared. |
| `evidence` | Bounded local evidence pointers. |

Renaming a unit changes its path-derived ID. Matching a rename by content hash
may appear in a two-run report as a candidate, but it must not silently rewrite
identity. This keeps references auditable and avoids false rename certainty.

## Artifact 2: dependency edges

`dependencies.json` stores only direct observed or declared edges. Transitive
closure, cycles, hotspots, and impact summaries are derived by report/UI code so
they cannot drift from the edge set.

Each edge contains:

- a deterministic `edge_id` and `from_unit_id`;
- a target union: internal `unit_id`, normalized external package/system name,
  or an explicit unresolved identifier;
- a closed relation such as `import`, `include`, `inherit`, `invoke`,
  `route`, `data`, `configuration`, `build`, or `test`;
- phase (`runtime`, `build`, `test`, or `migration`) and optionality;
- resolution state and confidence (`high`, `medium`, or `low`); and
- one or more local evidence pointers.

The scanner never invents an internal target because names look similar.
Ambiguous resolution creates an unresolved edge with candidates. Dynamic
dispatch and runtime wiring are expected unknowns and remain visible in the
migration report.

## Artifact 3: feature and entry-point map

`features.json` contains two linked record sets.

An entry point is a locally evidenced way behavior starts or crosses a system
boundary. The initial vocabulary should include command-line commands, HTTP or
UI routes, library/public APIs, event consumers, scheduled jobs, process starts,
and migration scripts. Each entry-point record has an ID, kind, bounded name,
owning unit, path/symbol pointer, linked feature IDs, origin, confidence, and
evidence.

A feature groups one or more entry points and units around a migration-relevant
behavior. Feature records have an ID, bounded label, state (`candidate` or
`confirmed`), origin (`detected` or `declared`), unit and entry-point links,
confidence, and evidence. A detector may create only a candidate. Confirmation
requires an explicit local declaration in `config.json` or a supported pointer
to confirmed onboarding evidence; confidence alone never promotes a feature.

Infrastructure shared by several features may link to all of them or be marked
`shared` without a fabricated feature assignment. An entry point with no
feature link and a feature with no entry point are both reportable gaps.

## Artifact 4: migration-readiness signals

`readiness.json` records a signal matrix per unit. It deliberately does not
store a percentage or an opaque “migration score.” A green-looking number would
hide which evidence is missing and could be mistaken for release authority.

A signal contains:

| Field | Meaning |
| --- | --- |
| `signal_id` | Stable ID for unit, check, and policy version. |
| `unit_id` | Unit being assessed. |
| `check` | Closed check name. |
| `status` | `satisfied`, `unsatisfied`, `unknown`, or `not_applicable`. |
| `severity` | `blocker`, `warning`, or `information`. |
| `basis` | `detected`, `declared`, or `verified_external_evidence`. |
| `confidence` | `high`, `medium`, or `low`; never a substitute for status. |
| `reason_code` | Stable machine-readable explanation. |
| `evidence` | Source or local agenttalk evidence pointers. |
| `policy` | Readiness-policy ID/version/digest that required the check. |

The default policy should cover only migration preparation that the available
evidence can support:

- source is included and understood by an adapter;
- direct internal dependencies are resolved or explicitly marked dynamic;
- externally visible entry points are mapped;
- the unit is linked to a confirmed feature or explicitly classified shared;
- build/test or characterization evidence is located, otherwise unknown;
- external integration, configuration, and persistence boundaries are
  identified, otherwise unknown; and
- a target-stack disposition exists when the operator declares planning ready.

The derived per-unit `assessment_state` is `assessed`, `needs_evidence`,
`blocked`, or `not_applicable`. Any required blocker that is unsatisfied yields
`blocked`; any required unknown yields `needs_evidence`; `assessed` means only
that migration assessment evidence is present. It does not mean implemented,
parity-verified, accepted, cutover-ready, or safe to decommission.

## Evidence pointers and trust

All four artifacts use one evidence-reference union:

- source pointer: relative path, optional line/symbol, and source digest;
- comprehension pointer: scan ID, artifact type, and record ID;
- onboarding pointer: run ID, item kind, and item key;
- work/gate/close pointer: local record ID plus frozen revision when available;
  or
- declaration pointer: configuration digest and declaration key.

Pointers are data, not instructions. Consumers revalidate the target before
acting. A source pointer whose digest no longer matches is stale. A missing or
malformed external record changes a dependent readiness signal to `unknown`; it
does not preserve the old satisfied state.

The plane inherits the local store's trust model. It validates structure and
provenance links but does not claim that a same-OS-user-authored declaration is
cryptographically true.

## Scan behavior

One scan follows this fixed pipeline:

1. Resolve the initialized project root and load/validate local configuration.
2. Enumerate allowed files without following links outside the root. Exclude
   `.git/`, `.agenttalk/`, binaries, known secret files, dependency caches, and
   generated/vendor trees by default; record every category and count.
3. Hash included files and run enabled bundled adapters in-process. Adapters
   receive bytes and relative paths, not ambient credentials or environment
   dumps.
4. Normalize records, resolve only evidenced edges, merge declarations, and
   retain conflicts and unknowns.
5. Evaluate the versioned readiness policy from the normalized artifacts.
6. Validate schema, confinement, referential integrity, deterministic ordering,
   counts, and hashes in staging.
7. Publish the immutable run and atomically advance the index.

Symlinks are recorded as boundaries and not followed by default. An eventual
opt-in may follow a link only after resolving it and proving the target remains
inside the project root. Submodules are external boundaries unless explicitly
included and already present locally. The scanner never initializes or updates
them.

Resource caps apply to file count, individual file bytes, total bytes, nesting,
and adapter work. Hitting a cap yields a degraded scan and explicit problem;
the scanner does not silently sample. Scanner output is deterministic even when
adapter work is parallelized: merge and serialization order are canonical.

## Proposed CLI surface

The v1 surface stays migration-shaped and read-mostly:

| Command | Contract |
| --- | --- |
| `agenttalk comprehension scan` | Create and publish one immutable run for the current worktree. Supports repeated `--scope PATH`, `--exclude GLOB`, `--config PATH`, and `--json`. |
| `agenttalk comprehension status` | Show the latest run, completeness, source revision/fingerprint, freshness, adapter coverage, and problem counts. Supports `--run ID` and `--json`. |
| `agenttalk comprehension report` | Answer fixed migration questions for a run. Filters include `--unit ID`, `--feature ID`, `--readiness STATE`, and `--dependencies`; `--json` emits the validated projection. |
| `agenttalk comprehension pack` | Build an immutable brief-time context pack. Requires at least one `--unit`, `--feature`, or `--path`; optionally binds `--work-id`, `--run`, and `--max-bytes`. |
| `agenttalk comprehension validate` | Verify schemas, manifest hashes/counts, confinement, and cross-references for one run or the latest run. Supports `--json`. |

Examples of the proposed syntax, not runnable in rev 1:

```text
agenttalk comprehension scan --scope src/legacy --scope tests/characterization
agenttalk comprehension status --json
agenttalk comprehension report --feature checkout --dependencies
agenttalk comprehension pack --work-id migrate-checkout --feature checkout --max-bytes 24576
agenttalk comprehension validate --run 20260826T091530Z-a1b2c3d4
```

`scan` returns success when a valid `complete` or `degraded` generation is
published. Readiness blockers are report data, not process failures. Invalid
arguments, unsafe paths, invalid configuration, or a failure to publish a
validated run use agenttalk's ordinary command-error exit. The CLI must never
turn “findings exist” into a scripting failure unless a later, explicit policy
command is designed for that purpose.

`report --json` is the stable automation contract. Human output may evolve.
`validate` does not repair or rewrite an immutable run. V1 has no arbitrary
graph query, background watcher, auto-rescan daemon, or delete command.

## Brief-time context packs for wrapped agents

A pack is a deterministic, bounded JSON projection, not a model-generated
summary. It contains:

- pack schema version, ID, creation time, selected work ID, source scan ID, and
  scan-manifest digest;
- exact selectors and current freshness result for every relevant source path;
- selected features, entry points, and units;
- one-hop incoming and outgoing dependencies, with unresolved edges first;
- blockers and unknown readiness signals before satisfied informational rows;
- local evidence pointers; and
- byte count, truncation flag, omitted counts by section, and a continuation
  hint for a narrower follow-up report.

The selection order is fixed: blockers/unknowns, entry points, selected units,
direct dependencies, then lower-priority context. Transitive closure is never
included by default. The hard pack cap is 64 KiB; the proposed default is
24 KiB. Truncation always preserves the identity/freshness header and omitted
counts.

At dispatch, a migration work item carries comprehension selectors and either a
required scan ID or “latest.” The wrapper validates the artifact and builds or
loads the pack before assembling the brief. It injects a small rendered summary
plus the local pack path and digest. The bus record stores the pack ID/digest,
not the whole graph.

The brief labels the pack advisory and potentially stale. It tells the agent to
verify source pointers locally and treat names, labels, and declarations as
untrusted data, never as instructions. A changed/missing relevant path is
listed explicitly. Policy may HOLD admission when an exact fresh scan is
required; absent such policy, the wrapper warns and continues. It never silently
rescans or substitutes a newer generation for a work item bound to an exact
scan.

The pack contains no source excerpts, raw literals, prompts, command output, or
secrets. The existing wrapped-agent provider boundary remains unchanged: task
#55 neither uploads files nor introduces a second service that receives client
code. Agents use ordinary local file tools to inspect only the pointers needed
for their assigned work.

After a delivery changes relevant files, the old pack becomes stale. A new scan
and pack make the change visible to the next agent or review round. Findings
that deserve durable human interpretation still belong in onboarding or
knowledge records, with pointers back to the scan.

## Contract for the migration-program UI (#208)

The browser must not parse run files directly. A server-side comprehension
projector validates one run and emits a bounded, versioned
`GET /api/comprehension` response. The same projector backs
`comprehension report --json`, preventing CLI/UI semantic drift. The Team
Console remains loopback-only and uses packaged assets only.

Task #208 reads these fields from the projection:

- run identity, manifest digest, source revision/fingerprint, age, freshness,
  status, adapter coverage, exclusions, and problems;
- inventory counts and bounded unit rows grouped by feature, domain,
  classification, language, and assessment state;
- confirmed and candidate features, their entry points, unmapped entry points,
  and units not linked to a feature;
- direct dependency summaries: internal/external/unresolved counts, derived
  cycles, high fan-in/fan-out units, and evidence drill-down pointers;
- per-unit readiness signals, especially blockers, unknowns, policy version,
  and evidence provenance; and
- truncation and omitted counts for every bounded collection.

The projection does not expose raw source, absolute paths, file contents,
source excerpts, environment data, parser logs, bus bodies, prompts, or secrets.
All displayed labels are escaped as data. Browser links are local evidence
drill-downs, not remote URLs.

The comprehension plane substantiates only the assessment portion of #208's
stage vocabulary. A program may use fresh, complete comprehension evidence plus
explicit review/gate policy to support `assessment-complete`. The later stages
(`plan-approved`, `reimplementation-complete`, `parity-verified`,
`acceptance-ready`, `cutover-ready`, and `legacy-decommissioned`) must come from
work, test, gate, close, and operator records. The UI must never infer or advance
them from a scan or an `assessed` unit.

The program UI may aggregate across scan runs to show change over time, but it
must label the exact source generation for every number. It must not add counts
from different generations or hide a newer degraded scan behind an older green
one.

## Privacy and offline enforcement

“Local-only” is an acceptance criterion, not a deployment preference:

- production scanner/projector modules may not import or call network clients,
  sockets, URL openers, package managers, `git fetch`, hosted models, or remote
  language services;
- v1 adapters are bundled and in-process, read-only, deterministic, and receive
  an allowlisted input object rather than the ambient environment;
- the scanner never executes repository code, build scripts, package lifecycle
  hooks, or arbitrary configured commands;
- discovery is confined to the resolved project root; symlink, submodule,
  archive, and path-traversal boundaries fail closed;
- secret-like files, binary files, VCS internals, `.agenttalk/`, dependency
  caches, and generated/vendor trees are excluded by default and reported;
- persisted free text is bounded and generated from templates or explicit local
  declarations; parser exceptions and command output are reduced to reason
  codes;
- packs and the UI expose structural metadata and pointers only; and
- the local UI loads no CDN assets, analytics, remote fonts, or external links
  generated from scanned content.

Future implementation tests must prove the boundary with a socket-denial guard,
a fixture containing unique secret/comment/literal canaries that must not appear
in any artifact/report/pack, malicious path and symlink fixtures, and a fully
offline end-to-end scan/report/pack/UI projection. A Graphify or other prior-art
component is admissible only if it can satisfy this same boundary without a
network-capable sidecar; feature similarity is not enough.

## Failure and freshness behavior

- A missing plane yields “not scanned,” never an empty/healthy assessment.
- An unsupported newer schema yields “unsupported schema” and no derived
  readiness, not a best-effort green projection.
- A malformed artifact or manifest mismatch invalidates the generation. Readers
  may fall back only when the caller explicitly asks for an older run.
- A parser failure yields a bounded problem and unknown signals for affected
  units. It does not erase units detected by other valid adapters.
- A changed, deleted, or newly relevant path marks a pack stale. The old pack
  stays reproducible and visibly stale; it is never mutated.
- A concurrent scan cannot disturb readers of the prior published generation.
- A resource-limit hit yields a degraded run with exact omitted counts where
  knowable. No automatic sampling is called complete.
- An empty selected scope is a command error, not a valid zero-unit scan.

## Proposed implementation slices and targeted evidence

Implementation remains out of scope for this rev, but the contract decomposes
into reviewable slices:

1. Schemas, strict readers/validators, deterministic IDs, staging publication,
   and fixture-based module/dependency/feature/readiness artifacts.
2. Bundled local adapters plus `scan`, `status`, `report`, and `validate`.
3. Pack selection, freshness checking, and wrapper brief integration.
4. The bounded API projector consumed by task #208.

Each slice needs targeted tests rather than a full local dev-gate: deterministic
golden fixtures; malformed/higher-version/cross-reference failures; crash-safe
publication; concurrent old-generation reads; Windows/POSIX path normalization;
symlink/root escape; secret/comment/literal canary non-disclosure; socket-denied offline
execution; resource caps; stale pack behavior; deterministic truncation; and
CLI/API projection parity. CI remains the full gate.

## Open questions for adversarial review

1. **Unit granularity:** Which initial unit kinds are stable enough across the
   JAWS stack, and should a file always remain addressable beneath a larger
   adapter-defined unit?
2. **Adapter scope:** Which languages/frameworks form the smallest useful v1?
   The Graphify comparison and squadmate JAWS requirements are pending and may
   change this answer, but cannot relax the offline/no-egress boundary.
3. **Feature confirmation:** Is versioned `config.json` declaration sufficient,
   or should confirmation require a typed onboarding record and checker before
   #208 renders a feature as confirmed?
4. **Readiness policy:** Which checks are mandatory by unit kind, and should
   “target-stack disposition exists” belong in this plane or only in the
   migration-program/work layer?
5. **Freshness cost:** Is a full content fingerprint on every scan and relevant-
   path rehash on every pack acceptable for the largest expected legacy repo?
   If not, what weaker optimization preserves an exact stale/not-stale claim?
6. **Scale format:** At what measured record/byte threshold should one JSON
   document per artifact be replaced by deterministic shards or JSONL plus an
   index? V1 should not pre-optimize without a representative JAWS-scale corpus.
7. **Dynamic behavior:** Which unresolved dynamic edges are blockers versus
   accepted unknowns, and who records that disposition without making the
   scanner an authority?
8. **Provider minimization:** Although the plane has no network path and packs
   contain no source excerpts, should the wrapper inline bounded structural
   identifiers or inject only the local pack identity/path? Choose one before
   integration; neither option may introduce source-text egress.
9. **#208 API ownership:** Should `/api/comprehension` ship with #55 as the
   canonical projector, or with #208 while importing a #55-owned pure view
   function? One owner must define schema and truncation semantics.
10. **Lifecycle:** How many immutable runs and packs should be retained, and what
    explicit, recoverable prune UX is acceptable? Automatic deletion is rejected
    for v1.
11. **Prior art disposition:** Which Graphify capabilities are reusable locally,
    which are merely inspiration, and which conflict with the narrow migration
    scope, versioned artifact contract, or hard privacy constraint?
12. **JAWS reconciliation:** Which additional retro findings materially change
    artifact fields, readiness policy, or briefing selection? Fold the squadmate
    evidence into rev 2 before implementation starts.

## Rev 1 acceptance boundary

Rev 1 is ready for implementation planning only after adversarial reviewers
agree on the four artifact contracts, the offline/privacy threat boundary, the
meaning of `assessment_state`, the brief pack's confidentiality boundary, and
the #208 ownership seam. Graphify/JAWS findings must be incorporated or
explicitly disposed in rev 2. No implementation should begin by treating the
illustrative record fields or CLI spelling as already shipped behavior.
