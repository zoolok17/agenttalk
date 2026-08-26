# DESIGN #55 — Prior art and tiered requirements (rev 2)

- Status: rev-2 design input reconciled to the static-inventory slice; not an implementation decision
- Audience: the #55 design owner and reviewers of the migration-comprehension plane
- Mode: explanation — what the plane must know, why, and which prior art is reusable
- Research cut: 2026-08-26, agenttalk `abc1c6e`, Graphify
  `43d54acbfa9e731f7a592bb582c1f4b9d48ed73e`
- Companion design: `docs/DESIGN-55-comprehension-plane.md` at `6ad0b714e5fa2db0ccc4ea45c7c8e2f0c5e4978d`

## Decision summary

Graphify is validated as **useful prior art and a candidate optional extractor**, not
as the comprehension plane or its authority. Its local tree-sitter pass, typed graph,
source anchors, edge provenance, incremental fingerprints, and graph queries transfer
well. A tightly pinned `--code-only` spike is justified.

Graphify's generic mixed-corpus and hosted paths do not meet the hard boundary for
client work. The plane must never send client source, documentation, derived graph
data, symbol names, paths, or query text off the machine. Remote model backends,
hosted/enterprise indexing, non-loopback serving, and configuration that merely
*intends* to stay offline are disqualified. The local boundary must be enforced, not
inferred from absent API keys.

No surveyed tool supplies the whole target architecture. The lead's rev-2 scope
ruling makes slice 1 a static, local, single-run inventory plane. Runtime
observation, persisted-state joins, fleet capability, and role-specific planning
or review projections remain named later slices; they are not slice-1 acceptance
claims.

The target architecture remains a local, versioned comprehension artifact with
adapter inputs:

1. deterministic syntax and symbol facts from tree-sitter and/or SCIP indexers;
2. dependency and architecture facts from language-specific graph tools;
3. repository evidence that those tools do not know — configuration, tests, generated
   artifacts, docs, runtime observations, fleet capabilities, and onboarding records;
4. a query envelope that always reports snapshot identity, provenance, coverage gaps,
   ambiguity, and staleness.

The graph is evidence for dispatch and review. It does not decide that a migration is
safe, that a path is active at runtime, or that a reviewer has verified a claim.

## Evaluation boundary

The candidate was evaluated against these non-negotiable constraints.

| Constraint | Consequence |
| --- | --- |
| Client material never leaves the machine | All source and all derived structure are confidential. Slice 1 prohibits network-capable production code paths and proves zero attempted egress under a CI network-deny harness. It does not claim a portable production OS sandbox. |
| Existing onboarding is advisory and pointer-first | Large graph artifacts stay outside the bus/onboarding JSONL; onboarding records content-addressed pointers and bounded claims. |
| A migration is judged against exact inputs | Every result binds to a VCS revision when available plus the whole-scope content fingerprint, platform/path semantics, and dirty state, not a branch name or “latest” graph. |
| Unknown is not safe | Unsupported syntax, dynamic dispatch, stale data, partial extraction, and conflicting evidence remain named unknowns. |
| Static structure is not runtime reachability | Configuration and observed execution are separate evidence classes; neither is manufactured from a call graph. |
| A wrapped fleet needs role-specific answers | Lead, implementer, and reviewer query different projections of the same evidence. |

## Prior art: Graphify

### What it does

This review targets open-source Graphify at the `v8` head resolved on the research
cut,
[`43d54acbfa9e731f7a592bb582c1f4b9d48ed73e`](https://github.com/Graphify-Labs/graphify/tree/43d54acbfa9e731f7a592bb582c1f4b9d48ed73e),
not unrelated projects with the same name and not the hosted Graphify product.
The upstream files below were inspected as primary documentation; their behavioral
claims have not been independently executed and remain spike hypotheses.

Graphify detects files, extracts structure, builds a NetworkX graph, clusters it,
analyzes it, and exports local JSON/HTML/report artifacts. For code, tree-sitter
extractors emit files, classes, functions and relations such as calls, imports, and
inheritance. The project exposes `query`, `path`, and `explain` operations and can
incrementally rebuild changed files from content fingerprints. Its current code
extractors include the languages important to mixed migration work, including Java,
Python, and PowerShell.

The code-only path is materially different from the general path:

- `graphify extract <root> --code-only` skips docs/media and does not require an LLM
  key; the repository has a direct regression test for that property.
- The mixed-corpus path can send docs, PDFs, and images to a configured or
  auto-detected model backend. A local backend is possible, but remote providers are
  also first-class.
- An optional MCP server defaults to stdio or loopback HTTP, while shared HTTP and a
  hosted always-on product are also documented options.
- `graphify install` can modify assistant instruction/hook files. Extraction does not
  require adopting that policy surface.

The privacy distinction is therefore real, but it is a mode boundary that #55 would
have to pin and enforce. It is not safe to treat the product name “Graphify” as proof
that a particular invocation is offline. The inspected primary documents also use
“analysis” inconsistently: the `v8` security policy says graph analysis makes no
network calls, while the `v8` README and pipeline guide explicitly describe model
backends for docs/media. The executable mode plus a network-deny control must settle
the boundary; prose cannot.

### Data model

Graphify's extractor boundary returns nodes and directed edges, then stores them in
NetworkX node-link JSON. The documented core is:

- node: stable `id`, human `label`, `file_type`, `source_file`, and source location;
- edge: `source`, `target`, typed `relation`, `source_file`/location, and evidence label
  `EXTRACTED`, `INFERRED`, or `AMBIGUOUS` (with a score for inferred edges);
- optional hyperedge: a named relationship among three or more nodes;
- derived node community and graph analyses such as highly connected nodes and paths;
- output sidecars for per-file fingerprints, analysis, labels, and build freshness.

This is a good interchange *shape*, but not yet the #55 contract. The #55 artifact
also needs a schema version, immutable repository identity, generator/config hashes,
coverage accounting, conditions on reachability, and links to tests/runtime evidence.
Numerical confidence must never turn an inference into authorization.

### Concepts to transfer

1. **Local deterministic baseline.** Parse code without executing it or calling a
   model. A comprehension run must remain useful with networking disabled.
2. **Typed, directed relationships.** “A calls B” and “A is documented by D” are
   queryable facts rather than prose embedded in a report.
3. **Evidence class on every edge.** Extracted, inferred, ambiguous, and later
   runtime-confirmed facts must stay distinguishable.
4. **Exact source anchors.** Nodes and edges point back to repo-relative files and
   ranges so an agent or reviewer can verify them against the snapshot.
5. **Incremental fingerprints and freshness.** Content hashes avoid full rescans, but
   the result still declares which revision and files it represents.
6. **Small scoped queries.** Neighborhood, path, reverse-dependency, and explanation
   queries are better dispatch inputs than a whole-graph dump.
7. **Communities as navigation hints.** Clusters can propose segments or ownership
   boundaries, but must not create them authoritatively.
8. **First-class ambiguity.** Unresolved edges should be visible review work, not
   silently omitted or accepted.

### Concepts that do not transfer

- **Remote semantic extraction or hosted indexing.** Any provider or service that
  receives client material is outside the product boundary.
- **“No API key” as the egress control.** The runner must sanitize provider
  credentials and proxy variables and enforce network denial; configuration alone is
  not the security boundary.
- **Committing generated graphs by default.** A graph leaks architecture, names,
  paths, and relationships even without source text. It is local state unless an
  operator deliberately exports a reviewed, redacted artifact.
- **Assistant-wide hooks/instructions as correctness.** A nudge to query a graph does
  not prove that the graph is fresh or that an agent used it.
- **Inferred edges as truth.** They can nominate inspection work, never clear an
  unknown or satisfy a gate.
- **Community labels, “god nodes,” or token savings as migration evidence.** These
  are navigation/efficiency aids, not safety claims.
- **A shared HTTP or hosted control plane.** #55 needs local file/stdio access; any
  future remote sharing requires a separate threat model.
- **One raw `graph.json` as durable authority.** Parallel/incremental publication,
  schema drift, partial writes, and stale snapshots require an agenttalk-owned
  envelope and fail-closed reader.

### Candidate verdict

| Use | Verdict | Conditions |
| --- | --- | --- |
| Prior-art schema and query model | Adopt concepts | Preserve provenance, anchors, ambiguity, and incremental freshness. |
| Optional extractor adapter | Spike | Pin the exact commit; direct CLI/library use; `--code-only`; no install hooks; isolated child environment; CI network-deny harness; synthetic or operator-approved corpus first. Production admission needs a separate platform enforcement decision if code-path prohibition is insufficient. |
| Durable #55 artifact schema | Do not adopt raw | Wrap or transform into an agenttalk-owned, versioned, snapshot-bound envelope. |
| Docs/media semantic extraction | Reject for client material | Only a separately approved, demonstrably local model could reopen this decision. |
| Hosted/enterprise/shared service | Reject | Violates the local-only boundary. |
| Migration GO/HOLD authority | Reject | Graph evidence remains advisory until verified by code, tests, runtime evidence, review, and gates. |

Before taking a runtime dependency, the spike must measure extraction coverage and
false edges on representative Java/PowerShell/Python fixtures, inspect transitive
dependencies and licenses, and prove zero egress under failure as well as success.
The pinned tree's package metadata declares Apache-2.0 and the tree also contains
`LICENSE-MIT` and `NOTICE`. This is source inspection, not a completed license
determination: a dependency decision still needs a normal bundled-license,
provenance, and transitive-dependency review.

## Alternative prior art

### Tree-sitter

[Tree-sitter](https://tree-sitter.github.io/tree-sitter/) is the strongest extraction
primitive in this survey. It is an embeddable incremental parser that produces concrete
syntax trees, remains useful in the presence of syntax errors, and supports structural
queries. It can run wholly locally and is already the foundation of Graphify's code
pass.

Transfer: deterministic local parsing, exact byte/range anchors, incremental updates,
language adapters, parse-error visibility. Missing: cross-file symbol resolution,
runtime/configuration reachability, dependency semantics, docs/tests linkage,
provenance beyond syntax, and a fleet-facing query/result contract. Tree-sitter alone
is therefore necessary-quality substrate, not a comprehension plane.

### SCIP

The open [Semantic Code Intelligence Protocol](https://github.com/scip-code/scip/tree/a7b9c65a8aa148a79b67cc7f6dafea154dbc63d0)
is the strongest semantic interchange model surveyed. A local SCIP index contains
workspace metadata, documents, source occurrences, stable symbols, symbol roles,
documentation, diagnostics, and relationships such as implementations and type
definitions. Indexers exist for Java/Scala/Kotlin, TypeScript/JavaScript, Rust,
Python, C/C++, .NET, Ruby, Dart, and PHP.

Transfer: language-neutral symbol identity, precise occurrences/ranges, definition /
reference / implementation relationships, producer metadata, streaming-friendly
versioned protobuf, and golden snapshot tooling. Missing: PowerShell coverage,
business concepts, build/config/runtime branches, test reachability, docs drift,
fleet capability, and migration-stage evidence. Some indexers invoke real build tools,
which is a side-effect and dependency-download boundary the #55 runner must declare
and sandbox. SCIP should be considered an adapter format, not a requirement that every
language use one indexer or any hosted Sourcegraph service.

### Python import-graph tools (Grimp / Import Linter)

[Grimp](https://github.com/python-grimp/grimp/tree/f4d9ecfc9495bd1419623f15124c5b9a63de1048) builds a local queryable graph of Python
module imports. [Import Linter](https://import-linter.readthedocs.io/en/stable/)
checks that graph against architectural contracts, including indirect dependency and
layer rules, and exposes local graph exploration.

Transfer: cheap module-level dependency graphs, reverse dependencies, cycles,
architectural boundaries, and the useful distinction between observed dependencies
and declared policy. Missing: non-Python code, symbol/call/data-flow detail, runtime
conditions, test/doc relationships, and exact migration evidence. This family is a
valuable Python adapter and policy check, not the polyglot core.

### Comparison

| Candidate | Fully local core | Detail | Cross-language interchange | Best #55 role | Principal gap |
| --- | --- | --- | --- | --- | --- |
| Graphify code-only | Yes, if mode and egress are enforced | Symbols, calls/imports, graph paths, communities | Its own JSON across many grammars | Optional broad extractor/prototype | Inference quality and no runtime/config authority |
| Tree-sitter | Yes | Concrete syntax and ranges | Grammar API, not a shared semantic graph | Default parsing substrate | No cross-file semantics |
| SCIP | Yes; indexer behavior varies | Semantic symbols and occurrences | Strong versioned protocol | Preferred semantic adapter where supported | Coverage/build-tool variance; no fleet/runtime model |
| Grimp / Import Linter | Yes | Python modules/imports/contracts | No | Python dependency/policy adapter | Python-only and coarse-grained |

The architecture should accept multiple producers and normalize evidence. Choosing one
extractor as the only door would turn its language and semantics gaps into silent blind
spots.

## Evidence provenance and limits

The original `jaws-legacy/docs/dryrun/RETRO.md` is referenced but is absent from
this clone and its reachable history at the research cut. Rev 2 therefore separates
four evidence categories instead of describing all requirements as JAWS field facts.

| Provenance | What it supports | What it does not support |
| --- | --- | --- |
| Checked-in agenttalk code and policy at `abc1c6e` | Existing local-store, onboarding, wrapper, console, and reset contracts; product privacy and workflow constraints. | Claims about a client legacy repository's languages, components, or business flows. |
| [DESIGN #201](DESIGN-201-wrapper-owned-reply-delivery.md) and [DESIGN #202](DESIGN-202-interruption-aware-redelivery.md) | A checked-in account of agenttalk wrapper delivery/redelivery failures and the mechanisms proposed to address them. | Direct evidence that a static client-code extractor needs runtime, state, or fleet entities in slice 1. Requirements extrapolated from these designs are later-slice target architecture. |
| Pinned external prior art | Candidate schemas, adapters, and query shapes worth testing. | Evidence that an upstream tool satisfies agenttalk's privacy, correctness, scale, or platform contract. |
| Missing JAWS Plateau 1 retrospective | Nothing normative yet. | Application-specific entity/relation sufficiency, language coverage, business-flow coverage, or a claim that the target architecture reconstructs the JAWS domain map. |

The checked-in proxy evidence still motivates later work: the recorded failures crossed
configuration, runtime mode, persisted state, retry/cleanup, test reachability, and
fleet capability boundaries. That is an extrapolation from agenttalk's own runtime,
not a finding from static analysis of JAWS client source. Rev 2 preserves those needs
as slices 2–4 and does not use them to enlarge slice 1.

Until the original retrospective is obtained, the design MUST describe any JAWS-
specific language, framework, component, and business-flow coverage as unknown. The
later requirements below are hypotheses to validate against that record, not proof that
the named vocabulary is sufficient.

| Requirement provenance class | Requirement parts |
| --- | --- |
| Checked-in agenttalk product/workflow constraints | R-01 through R-09, R-21, R-22, and R-23a. |
| Pinned prior-art mechanisms reconciled to the companion S1 design | R-10a, R-11a, R-12a, R-15a, R-17a, and R-24. |
| Extrapolated from agenttalk's #201/#202 runtime failures; later target only | R-10b, R-11b/c, R-12b, R-13, R-14, R-15b, R-16, R-17b, R-18 through R-20, and R-23b. |
| Deferred until the missing JAWS retrospective is obtained | Any assertion that the entity/relation vocabulary, adapter language set, or coverage criteria are sufficient for the JAWS application/domain. |

## Delivery slices and normative meaning

Keywords **MUST**, **MUST NOT**, **SHOULD**, and **MAY** are normative only for the
slice named on the requirement. Later-slice requirements remain target architecture;
they are explicitly non-normative for slice 1 because no slice-1 producer exists.

| Slice | Contract | Status and residual |
| --- | --- | --- |
| **S1 — static inventory** | Local, single-run repository inventory, direct static edges, feature/entry-point map, readiness projection, bounded static pack, and immutable publication. | Normative for the first implementation. The companion artifact/storage design must name a producer for every S1 requirement. |
| **S2 — enriched static contracts** | Symbol-level and contract inventory, build/configuration entities, conditional static reachability, and richer static negative-space accounting. | Later target. No producer is designed yet; S1 reports these needs as unsupported/unknown rather than fabricating them. |
| **S3 — runtime, state, and fleet evidence** | Separately captured runtime observations, persisted-state/lifecycle facts, fleet/config snapshots, expiry, and joins to static facts. | Later target. No producer or retention/threat model is designed yet. Static adapters cannot mint this evidence. |
| **S4 — role-specific projections** | Lead/reviewer projections, base-to-head delta, counterexample evaluation, and human-reviewed work-boundary candidates. | Later target. It depends on S2/S3 producers and cannot be claimed by S1's generic static pack. |

### A. Confidentiality and trust boundary

**R-01 — Local-only execution [S1, product boundary].** Production scanner,
validator, pack, and projector code paths MUST NOT import or invoke network clients,
socket operations, remote endpoints, hosted models, registries, or package fetches.
Adapters receive an allowlisted input object, not ambient credentials. CI MUST exercise
the complete S1 flow under a network-deny harness with provider credentials, proxy
variables, and remote-looking configuration deliberately present, and MUST observe zero
connection attempts.

S1 does not claim a portable OS-enforced production sandbox: Windows has no named,
shipped equivalent in this design, and mutating the parent agenttalk process environment
would affect the wrapped agent. Production therefore relies on prohibited code paths,
in-process bundled adapters, strict configuration, and the CI harness. Any external
analyzer or stronger production isolation requires a separate platform design.

**R-02 — Derived data is client data [S1, product boundary].** Graphs, reports,
caches, identifiers, paths, selectors, and telemetry MUST receive the same local-only
treatment as source. Before writing `.agenttalk/comprehension/`, `scan` MUST prove that
the directory is ignored by the applicable VCS or require an attended acknowledgement
that records the root, risk, and work item. Unattended operation fails closed.

For this contract, **export** means intentionally moving a comprehension artifact out of
its private store: copying it, staging/committing it, attaching/uploading it, or serving
it to a non-loopback peer. S1 has no export command and no non-loopback API. A future
export requires an explicit operator-reviewed, redacted action. The plane cannot prevent
an OS user from copying a local file; that out-of-band action remains an export under
policy and is an honest residual risk.

**R-03 — No mandatory model [S1, product boundary].** The S1 plane MUST be
deterministic and LLM-free. Later local semantic enrichment MAY produce `inferred`
evidence, but it cannot replace extracted or runtime-observed evidence.

**R-04 — No source execution [S1, product boundary].** S1 parsers MUST NOT import
or execute target code and MUST NOT spawn external analyzers, compilers, or build tools.
A later adapter that invokes a tool must first define its command, cwd, isolated env,
network policy, timeout, outputs, and side-effect contract.

### B. Snapshot identity, provenance, and publication

**R-05 — Immutable input identity [S1, product boundary].** Every artifact MUST bind
to the canonical repository root, VCS kind and revision when available, dirty status,
whole-scope content fingerprint, effective included/excluded paths, present submodule or
dependency roots and their inclusion status, platform/path-semantics identifier, and
capture time. A dirty or non-VCS worktree is valid when the content fingerprint is
complete; a branch name is display metadata, never identity.

**R-06 — Producer identity [S1, prior-art mechanism].** Every emitted unit, edge,
feature, entry point, readiness signal, and problem MUST name its producer and version,
effective configuration/policy digest, applicable grammar/indexer version, extraction
time, and source-content digest or manifest basis. A run-level producer field alone is
insufficient.

**R-07 — Versioned closed schema [S1, product boundary].** The artifact and every
producer payload MUST have an explicit schema version. Duplicate JSON keys, unsupported
versions, malformed facts, and partial publication MUST fail closed to an unusable or
unknown artifact, never a best-effort graph.

**R-08 — Atomic, concurrency-safe publication [S1, product boundary].** A scan MUST
publish one complete immutable generation under a single-writer lock, recover stale
locks safely, reclaim unpublished staging directories, and compare-and-set the index
against the previous index digest. Interrupted or competing scans and missing sidecars
MUST NOT make an older or partial generation current.

**R-09 — Three-state freshness and dispatch [S1, product boundary].** Freshness MUST
be computed against the scan's whole-scope source fingerprint, not only the selected
pack paths. `current` requires a complete comparison under the same root, platform/path
policy, scope/configuration, and adapter identities, an exact whole-scope fingerprint
match, and matching selected pointers. `stale` requires direct proof that a selected
path changed or disappeared, or that a requested exact VCS revision differs. An
incomplete comparison or a whole-scope/identity mismatch without that direct proof is
`unknown`; a new or changed unselected path is therefore `unknown` because its relevance
to the old pack is undecidable. Stale or unknown data MUST NOT satisfy dispatch/review
admission unless an attended waiver is recorded on the work item. Warning-and-continue
is not an allowed default.

### C. Comprehension model

**R-10a — Static inventory entities [S1, prior-art mechanism].** S1 MUST represent
repository snapshot, file-backed migration unit, feature, process/entry point, and
internal or external dependency. Each source-backed record carries exact repo-relative
paths, available ranges/symbol pointers, and source digests.

**R-10b — Enriched entities [S2/S3 later, extrapolated].** S2 adds first-class
symbol/type/callable, configuration key, build target, test, and documentation/decision
entities. S3 adds persisted record, runtime process instance, state, and event entities.
There is no S1 producer for these types; S1 reports their absence as unsupported or
unknown. JAWS-specific sufficiency remains deferred until its retrospective is obtained.

**R-11a — Static direct relations [S1, companion-design producer].** S1 MUST retain
the companion design's closed direct relation set: `import`, `include`, `inherit`,
`invoke`, `route`, `data`, `configuration`, `build`, and `test`. Producers MAY add
versioned namespaced relations; unknown relations MUST NOT be coerced into a generic
safe edge.

**R-11b — Enriched static relations [S2 later, extrapolated].** S2 adds `contains`,
`defines`, `calls`, `implements`, distinct `reads` and `writes`, `generates`,
`configures`, `dispatches_to`, `tested_by`, and `documented_by`, backed by named static
producers. S1 does not claim these relations merely because a coarser edge looks similar.

**R-11c — Runtime/state relations [S3 later, extrapolated].** S3 adds `persists`,
`retries`, `cleans_up`, and `observed_at_runtime`, backed by the S3 runtime/state
producer. No surveyed static extractor produces these facts.

**R-12a — Static evidence class [S1, prior-art mechanism].** Every S1 edge MUST carry
exactly one closed evidence class: `extracted`, `declared`, `inferred`, or `ambiguous`,
plus local source anchors and a bounded explanation. Confidence may rank inspection
work but MUST NOT promote evidence class. Conflicts remain explicit records.

**R-12b — Runtime-observed evidence [S3 later, extrapolated].** `runtime_observed` is
a separate S3 evidence class with capture identity and expiry. It cannot be inferred
from confidence or minted by a static producer.

**R-13a — Conditional static reachability [S2 later, extrapolated].** S2 MAY attach
platform, feature flag, environment, configuration, gate-mode, and entry-point
conditions to static edges. It MUST distinguish possible under a declared condition
from currently active.

**R-13b — Active-path proof [S3 later, extrapolated].** `active` requires matching,
versioned configuration or runtime evidence from an S3 producer. Static possibility is
never active-path proof.

**R-14 — Indexed contract inventory [S2 later, extrapolated].** For a selected
contract, S2 MUST enumerate all *indexed* producers, consumers, readers, writers, call
sites, generated copies, test doubles, and sibling entry points. Unsupported, dynamic,
and unresolved sites are named unknowns; the result MUST NOT claim whole-program
completeness.

**R-15a — Static coverage and negative space [S1, companion-design producer].** Each
S1 snapshot and pack MUST report included/indexed paths, excluded/ignored categories,
unsupported languages/files, parse and adapter errors, unresolved dependencies,
generated/vendor handling, resource-limit omissions, conflicts, and truncation. The
producer states exact counts where knowable and `unknown` otherwise.

**R-15b — Enriched coverage [S2/S3 later, extrapolated].** S2 adds unresolved
symbols/calls and missing build/config metadata. S3 adds unobserved runtime branches
and expired observations. S1 does not imply either coverage layer.

### D. Queries required by each role

**R-16 — Lead dispatch projection [S4 later, extrapolated].** An S4 lead projection
MUST combine exact scope, cross-boundary edges, configuration/runtime evidence, impact
candidates, tests/build targets, unknowns, and fleet capability. It MAY present
candidate boundary sets with explicit crossing edges; the human lead remains the author
of any work split. The plane MUST NOT automatically assign ownership or publish a
migration plan.

**R-17a — Static implementer pack [S1, companion-design producer].** For an assigned
selector set, S1 MUST provide the exact snapshot, bounded units/paths, direct inbound and
outbound dependencies, features/entry points, readiness blockers and unknowns, source
pointers, coverage, conflicts, and omitted counts. It is a reading map, not permission
to edit outside the lane.

**R-17b — Joined implementer projection [S4 later, extrapolated].** After S2/S3
producers exist, S4 adds symbol/call/data-flow detail, persistence/cleanup,
configuration/platform branches, generated relationships, test reachability,
docs/decisions, downstream contracts, and runtime-characterization targets.

**R-18 — Reviewer verification projection [S4 later, extrapolated].** At an exact
reviewed base and head, S4 MUST provide a fact delta; affected indexed callers,
consumers, states, config modes, and sibling paths; test-to-path evidence; invariant
enforcement and counter-paths; untouched risk/ambiguity; provenance, gaps, and
freshness. No S1 delta producer exists.

**R-19 — Lifecycle/state-machine queries [S3 later, extrapolated].** For retrying or
persisted workflows, S3 MUST answer how state is entered, recorded, observed, retried,
cleaned, and exited; list cycles and terminal states; and identify states with no proven
escape. This target comes from agenttalk's #202 failure class, not static JAWS evidence.

**R-20 — Counterexample evaluation [S4 later, extrapolated].** S4 SHOULD evaluate
proposed conditions and invariants over checked-in defaults and captured S3
fleet/config samples. It surfaces predicates that reject every shipped configuration or
select no evidenced field path. S1 supplies neither the samples nor this evaluator.

### E. Integration with agenttalk evidence

**R-21 — Artifact, not bus payload [S1, product boundary].** The full graph lives in
local, content-addressed artifact storage. The onboarding ledger stores bounded
pointers and summaries, never source or whole subgraphs.

**R-22 — Onboarding remains workflow authority [S1, checked-in contract].** Static
extractors may propose segments, claims, drift, and unknowns, but only explicit
onboarding records advance their statuses and only an explicit run-state event can set
`ready-for-work`. A scan, rebuild, readiness value, or UI projection cannot silently
advance workflow state.

**R-23a — Static producer guard [S1, product boundary].** No S1 static extractor may
mint `runtime_observed`, fleet-capability, or persisted-state facts. Missing later-layer
evidence remains unknown.

**R-23b — Separate runtime/fleet source [S3 later, extrapolated].** Preflight,
wrapper outcomes, configuration snapshots, and runtime traces MAY enrich queries only
through a separately designed producer. Every fact retains producer, capture time,
snapshot/process identity, expiry, and conflict behavior.

**R-24 — Reproducible static evidence envelope [S1, companion-design producer].**
Every S1 pack MUST include its selectors, snapshot/generation, configuration digest,
returned record IDs, source refs, truncation/omission counts, coverage, conflicts, and
unknowns. It carries a structured reproduction descriptor containing the run ID,
configuration digest, and selector set. It MUST NOT persist a command string. The local
CLI may validate that descriptor and render a command for an operator; repository-
derived text is never executed as instructions.

## Tiered acceptance scenarios

The first-passing slice is part of each scenario's contract. A later-slice scenario is
not an S1 release gate.

| Scenario | First passing slice | Required evidence |
| --- | --- | --- |
| 1. Provider keys, proxy variables, and remote-looking configuration are present while an offline code scan runs under a network-deny harness. | S1 | The scan completes with zero connection attempts; production still carries R-01's no-OS-sandbox residual. |
| 2. Mixed code/docs input cannot silently select a remote backend. | S1 | S1 indexes only explicitly supported local inputs or refuses, and reports exclusions. |
| 3. A selected source changes/disappears, or a new/changed unselected scope path alters the whole-scope fingerprint. | S1 | Direct selected-path or exact-revision proof makes the old pack `stale`; an unselected-only whole-scope mismatch or incomplete comparison makes it `unknown`; neither can be `current`. |
| 4. A scanner is killed or competes with another scanner during publication. | S1 | The previous generation remains current; stale locks and unpublished staging are reclaimed; compare-and-set prevents an older writer becoming latest. |
| 5. Dynamic dispatch, unsupported grammar, parse failure, conflict, or resource truncation occurs. | S1 | The bounded scope and unknown/conflict appear in coverage and packs; omission is a test failure. |
| 6. Gate-active and gate-inactive static configurations share one extracted edge. | S2 | Conditional reachability differs without changing or promoting the extracted fact. |
| 7. One runtime observation confirms one conditional path. | S3 | Sibling static paths and inferred edges remain unpromoted. |
| 8. A contract has continuous and one-shot indexed callers plus readers/writers. | S2 | The inventory lists every indexed site and names unsupported/dynamic scope; deleting an indexed fixture site fails the test. |
| 9. A retry-state fixture has no exit, then gains a verified exit. | S4 | S3 reports the cycle/no-escape state; S4 shows the explicit base-to-head fact delta. |
| 10. An invariant is false for every checked-in default configuration. | S4 | Counterexamples are returned rather than a safe summary. |
| 11. An artifact has a duplicate key, malformed fact, or future schema version. | S1 | Strict decoding refuses it as unknown/unusable and no pack is produced. |
| 12. A valid accepted/ready-for-work onboarding record exists when the graph rebuilds. | S1 | The workflow record is unchanged; extraction only proposes new evidence. |
| 13. The three real egress vectors are exercised. | S1 | `scan` refuses an unignored VCS path without an attended recorded acknowledgement; non-loopback serving remains refused; the product exposes no copy/export operation and policy identifies an operator's plain file copy as an explicit out-of-band export requiring review/redaction. |
| 14. Lead, implementer, and reviewer projections cite the same snapshot. | S4 | Each exposes its distinct R-16/R-17/R-18 fields without changing snapshot identity. |

## Cross-document reconciliation

Only S1 rows below are normative for the companion rev-2 artifact/storage design.
The reconciled companion contract is frozen at `6ad0b714e5fa2db0ccc4ea45c7c8e2f0c5e4978d`.
Later rows state the exact residual and destination instead of implying that S1
produces them.

| Review item | Rev-2 disposition | Companion-design producer or residual |
| --- | --- | --- |
| X-1 / R-01 | S1, honestly downgraded | Privacy/offline enforcement prohibits network code paths and requires the CI denial harness; no portable production OS sandbox is claimed. |
| X-2 / R-02 | S1, resolved | VCS-ignore/attended-ack preflight and the explicit export policy are part of scan admission. |
| X-3 / R-05 | S1, resolved | Scan identity includes revision when available, whole-scope fingerprint, path scope, dependency roots, and platform semantics. |
| X-4 / R-06 | S1, resolved by required schema addition | Every emitted fact carries producer/version/config/source basis, not only the run. |
| X-5 / R-07 | S1, resolved | Strict decoding rejects duplicate keys and unsupported or malformed artifacts. |
| X-6 / R-09 | S1, resolved | Whole-scope `current`/`stale`/`unknown` and refuse-or-attended-waiver replace warn-and-continue. |
| X-7 / R-10 | Split | S1 produces R-10a. R-10b lands in S2/S3; no producer exists yet. |
| X-8 / R-11 | Split | S1 produces R-11a. Enriched static relations land in S2; runtime/state relations land in S3. |
| X-9 / R-12 | Split | S1 produces explicit static evidence classes. `runtime_observed` lands in S3 only. |
| X-10 / R-13 | Deferred | Conditional static reachability lands in S2; active-path proof lands in S3. |
| X-11 / R-14 | Deferred | Indexed contract inventory lands in S2. |
| X-12 / R-16 | Deferred and narrowed | S4 presents candidate boundaries; a human lead authors the work split. Fleet/config evidence comes from S3. |
| X-13 / R-18 | Deferred | Base-to-head delta and reviewer projection land in S4. |
| X-14 / R-19 | Deferred | A separate S3 state/event producer supplies lifecycle facts. |
| X-15 / R-20 | Deferred | S4 evaluates counterexamples over S3 config/fleet samples. |
| X-16 / R-23 | Split | S1 forbids static minting of runtime evidence; the actual runtime/fleet producer lands in S3. |
| X-17 / R-24 | S1 static envelope; role enrichment later | S1 packs carry record IDs, coverage/conflicts, and a structured descriptor. S4 adds role-specific projections; no command text is stored. |

## Explicit non-requirements for the first slice

- Whole-program proof or complete resolution of dynamic languages.
- Automatic GO, ownership assignment, lane creation, or migration-stage completion.
- Symbol-level contract inventory, conditional/active reachability, graph delta,
  lifecycle analysis, runtime observation, fleet capability, counterexample evaluation,
  or distinct lead/reviewer projections. These are S2–S4 targets, not hidden S1 work.
- A hosted graph database, remote portal, multi-tenant service, or network listener.
- Model-generated summaries as a prerequisite for indexing or querying.
- Copying client source, raw prompts, transcripts, or full graph payloads into the
  agenttalk store or dashboard API.
- A new universal parser when a proven local adapter can be normalized.
- Replacing direct source reading, characterization tests, runtime tracing, independent
  review, or gates.

## Residual risks and open questions

1. **Original field record:** obtain the JAWS Plateau 1 retrospective before claiming
   application-specific requirements. The S2–S4 extrapolations may change or be removed
   when that evidence is available.
2. **Production network isolation:** S1 has a test-time network-deny harness and a
   production code-path prohibition, not a portable OS-enforced production sandbox.
   External analyzers remain inadmissible until a platform-specific design closes this.
3. **Out-of-band file copy:** the plane can fail closed on VCS-ignore status and omit an
   export/API path, but it cannot stop the same OS user copying local artifacts. Policy,
   review, redaction, and ordinary endpoint controls own that residual.
4. **Unbuilt target layers:** S2, S3, and S4 have no producer designs. They are retained
   to prevent S1 from becoming an accidental dead end, but no S1 release note or gate may
   claim their behavior.
5. **Initial language set:** choose the bundled S1 adapters against the actual migration
   corpus. Upstream claims of language support are candidates for fixtures, not evidence
   of agenttalk coverage.
6. **Retention and scale:** the companion design must keep published data bounded,
   reclaim unpublished staging, and state measurable read-path limits. A later retention
   policy must not silently delete published evidence.
7. **Graphify spike:** the prior-art tree is pinned, but its behavioral, privacy,
   performance, dependency, and license claims have not been independently exercised.
   Test the exact commit under synthetic fixtures and the denial harness before selecting
   any dependency.

## Primary sources

Repository evidence:

- [agenttalk onboarding model](../src/agenttalk/onboarding.py)
- [agenttalk roadmap: onboarding and comprehension](ROADMAP.md#project-onboarding-and-codebase-comprehension)
- [DESIGN #201 — wrapper-owned reply delivery](DESIGN-201-wrapper-owned-reply-delivery.md)
- [DESIGN #202 — interruption-aware redelivery](DESIGN-202-interruption-aware-redelivery.md)

External prior art (accessed 2026-08-26):

- [Graphify pinned README](https://github.com/Graphify-Labs/graphify/blob/43d54acbfa9e731f7a592bb582c1f4b9d48ed73e/README.md)
- [Graphify pinned architecture and extraction schema](https://github.com/Graphify-Labs/graphify/blob/43d54acbfa9e731f7a592bb582c1f4b9d48ed73e/ARCHITECTURE.md)
- [Graphify pinned pipeline and graph format](https://github.com/Graphify-Labs/graphify/blob/43d54acbfa9e731f7a592bb582c1f4b9d48ed73e/docs/how-it-works.md)
- [Graphify pinned security model](https://github.com/Graphify-Labs/graphify/blob/43d54acbfa9e731f7a592bb582c1f4b9d48ed73e/SECURITY.md)
- [Graphify pinned package and license metadata](https://github.com/Graphify-Labs/graphify/blob/43d54acbfa9e731f7a592bb582c1f4b9d48ed73e/pyproject.toml)
- [Graphify pinned code-only regression controls](https://github.com/Graphify-Labs/graphify/blob/43d54acbfa9e731f7a592bb582c1f4b9d48ed73e/tests/test_extract_code_only_cli.py)
- [Tree-sitter introduction](https://tree-sitter.github.io/tree-sitter/)
- [SCIP canonical repository and protocol overview](https://github.com/scip-code/scip/tree/a7b9c65a8aa148a79b67cc7f6dafea154dbc63d0)
- [SCIP pinned reference schema](https://github.com/scip-code/scip/blob/a7b9c65a8aa148a79b67cc7f6dafea154dbc63d0/docs/scip.md)
- [SCIP Java pinned indexer build behavior](https://github.com/scip-code/scip-java/blob/e13aab51c9c11e9b803f7bcd7b13e62ffe04dc1f/docs/getting-started.md)
- [Grimp canonical pinned import graph](https://github.com/python-grimp/grimp/tree/f4d9ecfc9495bd1419623f15124c5b9a63de1048)
- [Import Linter graph and contract model](https://import-linter.readthedocs.io/en/stable/)
