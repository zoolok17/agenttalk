# DESIGN #55 — Prior art and field requirements (rev 1)

- Status: design input, not an implementation decision
- Audience: the #55 design owner and reviewers of the migration-comprehension plane
- Mode: explanation — what the plane must know, why, and which prior art is reusable
- Research cut: 2026-08-26, agenttalk `abc1c6e`

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

No surveyed tool supplies the whole product. The recommended shape is a local,
versioned comprehension artifact with adapter inputs:

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
| Client material never leaves the machine | All source and all derived structure are confidential. Network-denied local execution is required. |
| Existing onboarding is advisory and pointer-first | Large graph artifacts stay outside the bus/onboarding JSONL; onboarding records content-addressed pointers and bounded claims. |
| A migration is judged at an exact revision | Every result binds to an immutable commit plus dirty-worktree state, not a branch name or “latest” graph. |
| Unknown is not safe | Unsupported syntax, dynamic dispatch, stale data, partial extraction, and conflicting evidence remain named unknowns. |
| Static structure is not runtime reachability | Configuration and observed execution are separate evidence classes; neither is manufactured from a call graph. |
| A wrapped fleet needs role-specific answers | Lead, implementer, and reviewer query different projections of the same evidence. |

## Prior art: Graphify

### What it does

This review targets the open-source
[Graphify-Labs/graphify `v8` line](https://github.com/Graphify-Labs/graphify/tree/v8),
not unrelated projects with the same name and not the hosted Graphify product.

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
| Optional extractor adapter | Spike | Pin an exact version; direct CLI/library use; `--code-only`; no install hooks; sanitized environment; OS-enforced network denial; synthetic or operator-approved corpus first. |
| Durable #55 artifact schema | Do not adopt raw | Wrap or transform into an agenttalk-owned, versioned, snapshot-bound envelope. |
| Docs/media semantic extraction | Reject for client material | Only a separately approved, demonstrably local model could reopen this decision. |
| Hosted/enterprise/shared service | Reject | Violates the local-only boundary. |
| Migration GO/HOLD authority | Reject | Graph evidence remains advisory until verified by code, tests, runtime evidence, review, and gates. |

Before taking a runtime dependency, the spike must measure extraction coverage and
false edges on representative Java/PowerShell/Python fixtures, inspect transitive
dependencies and licenses, and prove zero egress under failure as well as success.
The inspected `v8` package declares Apache-2.0 and also ships MIT/NOTICE material; a
pinned dependency still needs a normal bundled-license, provenance, and transitive-
dependency review rather than an inferred answer.

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

The open [Semantic Code Intelligence Protocol](https://github.com/scip-code/scip)
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

[Grimp](https://github.com/python-grimp/grimp) builds a local queryable graph of Python
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

## Field evidence: what JAWS exposed

### Evidence boundary

The original `jaws-legacy/docs/dryrun/RETRO.md` is referenced but is not present in
this clone or its reachable history at the research cut. This section therefore uses
only the two checked-in designs that explicitly cite retrospective findings:

- [DESIGN #201](DESIGN-201-wrapper-owned-reply-delivery.md), based on JAWS finding 1;
- [DESIGN #202](DESIGN-202-interruption-aware-redelivery.md), based on JAWS finding 2
  plus a mechanism investigation at a named agenttalk revision.

This is enough to derive wrapped-fleet requirements, but not to reconstruct Plateau 1's
application/domain map. The final #55 design should obtain the original retrospective
before claiming JAWS component, language, or business-flow coverage.

### Observed facts and comprehension implications

| Field fact | What was missing | Requirement it creates |
| --- | --- | --- |
| The Claude-wrapped seat failed to deliver a reply in 5/5 work turns; the first Codex reply also failed because required environment was absent. | A dispatch-time view of seat capabilities, sandbox/write/command channels, required environment, and preflight evidence. | Fleet capability and runtime fingerprint must be queryable beside code ownership before assignment. |
| Failed answers became files indistinguishable on the bus from a dead agent, and one adapter could not report which tool ran. | End-to-end evidence linking intent, attempted mechanism, side effects, output artifact, and delivery disposition. | Comprehension must include observable boundary outcomes, not infer behavior from a wrapper's health or return code. |
| The first #201 design targeted the commit-gate path, but the JAWS ledger showed the gate was inactive for the whole run; every motivating failure used the freeform branch. | The *active* path under actual configuration, plus evidence that a test/design reaches it. | Every path claim carries conditions and configuration/runtime evidence; inactive paths cannot stand in for field behavior. |
| A watchdog kill produced partial output, no exit file, a healthy-looking heartbeat, `CLASS_AMBIGUOUS`, then byte-identical redelivery after 0.3 seconds into the same session with the partial draft removed. | A state-machine view across process, persistence, retry, and cleanup boundaries. | Model failure transitions, persisted state, retry/cleanup effects, and ambiguous outcomes as first-class edges/events. |
| The system could repeat that cycle for roughly ten hours before escalation. | Bounded-cycle and liveness analysis, not just a local call graph. | Queries must expose cycles, retry ceilings, backoff, terminal dispositions, and head-of-queue effects. |
| Review found that a proposed durable park had no honest unpark path and would permanently block the seat. | Reverse reachability from recovery states and negative-space review of every exit/unblock path. | Reviewer queries need “how can this state be entered, left, retried, or cleared?” and must name missing transitions. |
| A later design revision added a launch invariant that would reject every wrapped-Claude seat at shipped defaults. | Configuration-domain evaluation against real/default fleet values. | Proposed predicates must be evaluable over checked-in defaults and observed fleet snapshots, with counterexample generation. |
| Fixes required parity across CLI/wrapper producers and both continuous/one-shot loop paths. | Complete call-site and producer/consumer inventory, including sibling paths. | The plane must answer “all writers/readers/callers of this contract” and report unsupported/dynamic sites as unknown. |

The decisive lesson is that code comprehension cannot be only a source graph. JAWS
failed where source, configuration, runtime mode, persisted state, test reachability,
and fleet capability met. The plane must preserve those as separate evidence layers
and let a query join them without pretending a static edge is a runtime observation.

## Requirements from the field

Keywords **MUST**, **MUST NOT**, **SHOULD**, and **MAY** are normative for the #55
design.

### A. Confidentiality and trust boundary

**R-01 — Enforced local-only execution.** Indexing and querying MUST work with all
network access denied. The runner MUST strip provider credentials, proxy variables,
and remote endpoints or run inside an equivalent deny-by-default boundary. A tool
configuration flag is not sufficient evidence.

**R-02 — Derived data is client data.** Graphs, reports, caches, symbol/path lists,
query text, and telemetry MUST receive the same local-only treatment as source. They
MUST NOT be committed, uploaded, or served off-machine by default.

**R-03 — No mandatory model.** The minimum useful plane MUST be deterministic and
LLM-free. Local semantic enrichment MAY be an optional evidence producer, but its
output is `inferred` and can never replace extracted or runtime-confirmed evidence.

**R-04 — No source execution by default.** Parsers MUST NOT import or execute target
code. Adapters that invoke a compiler/build tool MUST declare the command, cwd, env,
network policy, timeout, outputs, and side-effect risk before they run.

### B. Snapshot identity, provenance, and publication

**R-05 — Immutable input identity.** Every artifact MUST bind to repository root,
exact commit SHA, dirty-worktree digest/status, included and excluded paths, submodule
or dependency roots, platform, and capture time. A branch name is display metadata,
not identity.

**R-06 — Producer identity.** Every fact MUST name its producer, producer version,
configuration/policy hash, language grammar/indexer version, extraction time, and
source content hash.

**R-07 — Versioned closed schema.** The artifact and every producer payload MUST have
an explicit schema version. Duplicate fields, unsupported versions, malformed facts,
and partial publication MUST fail closed to an unusable/unknown artifact, never a
best-effort graph.

**R-08 — Atomic, concurrency-safe publication.** Incremental updates MUST publish a
complete new generation atomically. Concurrent writers, interrupted scans, merge
conflicts, and missing sidecars MUST not produce a graph that claims the new revision.

**R-09 — Staleness is an outcome.** A query MUST return `current`, `stale`, or
`unknown` against the requested worktree/SHA. Stale or unknown data cannot satisfy a
dispatch/review prerequisite without an explicit attended waiver.

### C. Minimum comprehension model

**R-10 — Stable anchored entities.** At minimum, represent repository, file, module /
package, symbol/type/callable, configuration key, build target, test, documentation /
decision, persisted record, process/entrypoint, and external dependency. Each source
entity carries an exact repo-relative path and range plus content hash.

**R-11 — Typed directed relations.** At minimum, support `contains`, `defines`,
`imports`, `calls`, `inherits`, `implements`, `reads`, `writes`, `generates`,
`configures`, `dispatches_to`, `persists`, `retries`, `cleans_up`, `tested_by`,
`documented_by`, and `observed_at_runtime`. Producers MAY add namespaced relations;
unknown relation types MUST NOT be coerced into a generic safe edge.

**R-12 — Evidence class, not confidence soup.** Every relation is one of
`extracted`, `inferred`, `ambiguous`, or `runtime_observed`, with source anchors and an
explanation. Confidence may rank inspection work but MUST NOT promote evidence class.
Conflicts remain visible as conflicts.

**R-13 — Conditions and reachability.** A path/edge MAY carry platform, feature flag,
environment, configuration, gate mode, entrypoint, and lifecycle-state conditions.
“Active” MUST require matching configuration or runtime evidence. Static possibility
alone is never active-path proof.

**R-14 — Whole-contract inventory.** The plane MUST enumerate all known producers,
consumers, readers, writers, call sites, generated copies, test doubles, and sibling
entrypoints for a selected contract. Dynamic/unresolved sites are returned as named
unknowns, not omitted.

**R-15 — Coverage and negative space.** Each snapshot MUST report indexed files,
ignored files, unsupported languages, parse errors, unresolved symbols/calls, generated
code handling, missing build metadata, and unobserved runtime branches. Coverage is
part of every query response.

### D. Queries required by each role

**R-16 — Lead dispatch pack.** Before dispatch, the lead MUST be able to request:

- candidate domains/communities with exact path scope and cross-boundary edges;
- active entrypoints and configuration/runtime evidence for the target behavior;
- impacted files/symbols, owners/shared paths, build targets, tests, and risky seams;
- unresolved/ambiguous areas and characterization targets;
- fleet capability/runtime fingerprint needed for the work; and
- a proposed non-overlapping work split whose boundary edges are explicit.

The output must distinguish “not indexed,” “not reachable,” and “not observed.”

**R-17 — Implementer work pack.** For an assigned slice, a developer MUST receive:

- the exact snapshot and bounded source neighborhood;
- inbound/outbound calls and dependencies, data reads/writes, persistence and cleanup;
- configuration/platform branches and generated-source relationships;
- linked tests, fixtures, commands, docs/ADRs, known drift, and rationale;
- change-blast-radius candidates and downstream contracts; and
- named unknowns requiring direct reading or runtime characterization.

The pack is a reading map, not permission to edit paths outside the lane.

**R-18 — Reviewer verification pack.** At the reviewed SHA, a reviewer MUST receive:

- a graph delta from base to head, including added/removed/changed facts;
- affected callers, consumers, state transitions, config modes, and sibling paths;
- which tests exercise which changed path under which configuration;
- claimed invariants mapped to enforcement sites and counter-paths;
- untouched high-risk or ambiguous paths; and
- exact evidence provenance, extraction gaps, and staleness verdict.

The reviewer must be able to ask “what path makes this claim false?” and “what writer,
reader, exit, retry, or cleanup path did the change not touch?”

**R-19 — Lifecycle/state-machine queries.** For retrying or persisted workflows, the
plane MUST answer how a state is entered, durably recorded, observed, retried, cleaned,
and exited; list cycles and terminal states; and identify states with no proven escape.
This is required for the JAWS watchdog/redelivery class.

**R-20 — Counterexample evaluation.** Proposed conditions and invariants SHOULD be
evaluated over checked-in defaults and captured fleet/config samples. A predicate that
rejects all shipped configurations or selects no field path must be surfaced before
implementation.

### E. Integration with agenttalk evidence

**R-21 — Artifact, not bus payload.** The full graph lives in local,
content-addressed artifact storage. The onboarding ledger records bounded pointers:
artifact digest, snapshot/generation, segment, paths, producer, coverage/staleness,
summary, reviewer/checker, and open drift/unknowns. It MUST NOT copy source or whole
subgraphs into messages.

**R-22 — Existing onboarding vocabulary remains authoritative for workflow.** Static
extractors may propose segments/claims/drift/unknowns, but only explicit onboarding
records advance kind-specific statuses such as `proposed`, `confirmed`, `conflicted`,
or `accepted`, and only an explicit run-state event can set `ready-for-work`. A graph
rebuild cannot silently change workflow state.

**R-23 — Runtime/fleet evidence is separately sourced.** Preflight, wrapper outcomes,
configuration snapshots, and runtime traces MAY enrich queries, but they retain their
own producer, capture time, identity, and expiry. No static extractor may mint
`runtime_observed` facts.

**R-24 — Human-readable evidence envelope.** Every role pack MUST include the query,
snapshot/generation, returned node/edge IDs, source refs, truncation/omission counts,
coverage, conflicts, unknowns, and a direct re-run recipe. A prose answer without that
envelope is advisory commentary only.

## Failing-first acceptance scenarios for the eventual design

These scenarios define the product boundary; they are not a commitment to one storage
or query implementation.

1. With provider keys and proxy variables deliberately present, an offline scan under
   a network-deny harness completes for code and makes zero connection attempts.
2. Mixed code/docs input does not silently choose a remote backend; it refuses or
   indexes the explicitly allowed local subset and reports excluded files.
3. Changing one file makes the old snapshot stale; no query can report it as current
   for the new SHA.
4. Killing the extractor during publication leaves the previous generation current and
   the attempted generation unusable, never half-current.
5. A dynamic call, unsupported grammar, or parse error appears as an unknown with a
   source scope; removing it from results is a test failure.
6. The same static call graph under gate-active and gate-inactive configuration yields
   different conditional reachability, while the underlying extracted edge remains the
   same fact.
7. A runtime observation can confirm one conditional path without promoting sibling
   static paths or inferred edges.
8. A reviewer query over a contract lists both continuous and one-shot callers and
   every writer/reader; deleting one indexed call site causes the inventory test to fail.
9. A retry-state fixture with no exit is reported as a cycle/HOLD candidate; adding a
   verified exit changes the result with an explicit graph delta.
10. A proposed invariant that is false for every checked-in default configuration is
    rejected with counterexamples rather than summarized as safe.
11. A malformed/duplicate-field/future-version artifact is refused as unknown and
    cannot produce a role pack.
12. A valid ACCEPT/ready-for-work onboarding record remains unchanged after a graph
    rebuild; new extraction only proposes new evidence.
13. Export attempts show that graph and report artifacts are local/private by default
    and require an explicit operator-reviewed action.
14. Lead, developer, and reviewer packs all cite the same snapshot but expose the
    distinct data required in R-16 through R-18.

## Explicit non-requirements for the first slice

- Whole-program proof or complete resolution of dynamic languages.
- Automatic GO, ownership assignment, lane creation, or migration-stage completion.
- A hosted graph database, remote portal, multi-tenant service, or network listener.
- Model-generated summaries as a prerequisite for indexing or querying.
- Copying client source, raw prompts, transcripts, or full graph payloads into the
  agenttalk store or dashboard API.
- A new universal parser when a proven local adapter can be normalized.
- Replacing direct source reading, characterization tests, runtime tracing, independent
  review, or gates.

## Risks and open questions for the #55 design owner

1. **Original field record:** obtain the JAWS Plateau 1 retrospective before the final
   design claims application-specific requirements; this rev only proves fleet/runtime
   requirements from checked-in citations.
2. **Artifact home:** choose a local content-addressed location and retention/redaction
   policy. `.agenttalk/` is plausible, but large artifacts and cache lifecycle need a
   bounded design.
3. **Initial language set:** decide which adapters cover the first migration corpus.
   Graphify/tree-sitter can cover PowerShell; SCIP provides stronger semantics for many
   compiled languages but not PowerShell.
4. **Build-aware indexing:** decide whether any adapter may invoke Maven/Gradle or other
   build tools in slice 1. If yes, dependency/network/side-effect policy is a separate
   execution contract.
5. **Runtime evidence:** define the smallest safe observation input. Static graph and
   runtime trace must not become an unversioned mixed-confidence graph.
6. **Dirty worktrees:** decide whether a content-manifest identity is sufficient for
   local implementation queries and when review must refuse anything not at a commit.
7. **Graphify spike:** pin an exact commit/release and evaluate precision, incremental
   integrity, resource cost, license/provenance, and zero-egress under a synthetic
   JAWS-shaped fixture before selecting it as a dependency.

## Primary sources

Repository evidence:

- [agenttalk onboarding model](../src/agenttalk/onboarding.py)
- [agenttalk roadmap: onboarding and comprehension](ROADMAP.md#project-onboarding-and-codebase-comprehension)
- [DESIGN #201 — wrapper-owned reply delivery](DESIGN-201-wrapper-owned-reply-delivery.md)
- [DESIGN #202 — interruption-aware redelivery](DESIGN-202-interruption-aware-redelivery.md)

External prior art (accessed 2026-08-26):

- [Graphify `v8` README](https://github.com/Graphify-Labs/graphify/blob/v8/README.md)
- [Graphify architecture and extraction schema](https://github.com/Graphify-Labs/graphify/blob/v8/ARCHITECTURE.md)
- [Graphify pipeline and graph format](https://github.com/Graphify-Labs/graphify/blob/v8/docs/how-it-works.md)
- [Graphify security model](https://github.com/Graphify-Labs/graphify/blob/v8/SECURITY.md)
- [Graphify package and license metadata](https://github.com/Graphify-Labs/graphify/blob/v8/pyproject.toml)
- [Graphify code-only regression controls](https://github.com/Graphify-Labs/graphify/blob/v8/tests/test_extract_code_only_cli.py)
- [Tree-sitter introduction](https://tree-sitter.github.io/tree-sitter/)
- [SCIP repository and protocol overview](https://github.com/scip-code/scip)
- [SCIP reference schema](https://github.com/scip-code/scip/blob/main/docs/scip.md)
- [SCIP Java indexer build behavior](https://github.com/scip-code/scip-java/blob/main/docs/getting-started.md)
- [Grimp import graph](https://github.com/python-grimp/grimp)
- [Import Linter graph and contract model](https://import-linter.readthedocs.io/en/stable/)
