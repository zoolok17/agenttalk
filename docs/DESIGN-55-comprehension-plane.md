# DESIGN #55 — Static comprehension inventory contract (rev 2)

Status: rev 2, ready for adversarial re-review. Date: 2026-08-26.

Audience: agenttalk contributors implementing the first slice of task #55 and
the migration-program UI in task #208. Their goal is to agree on the static
artifact and storage contract that gives a migration fleet a shared inventory
of one legacy-repository snapshot.

This is an explanation/design document. None of the CLI examples below are
implemented or runnable yet.

This document is only the static, local, single-run inventory half of task #55.
`docs/DESIGN-55-priorart-and-requirements.md` owns the prior-art disposition and
the tiered target requirements. Runtime observation, persisted-state joins,
multi-run review analysis, fleet capability, and work-split synthesis are later
slices. The requirements reconciliation below names their destination rather
than implying that this design produces them. The reconciled companion baseline
is commit `8b064acd0419813b1f28df34159e468a18f9fd4f` on
`docs/55-priorart-requirements`.

The original JAWS Plateau 1 retrospective is not present in this repository or
its reachable history. This design therefore does not claim JAWS application,
language, component, or business-flow coverage. It uses checked-in task and
agenttalk design evidence plus the companion requirements document. It also
makes no Graphify compatibility or dependency decision; that belongs to the
companion document and a separately reviewed spike.

## Decision summary

Slice 1 adds a local, immutable scan plane under
`.agenttalk/comprehension/`. Each scan publishes versioned JSON artifacts for
repository/file units, direct static dependencies, features and entry points,
and evidence-backed preparation signals. The CLI and task #208 read those
artifacts through one validated projection.

The plane is advisory. It does not decide that a feature has parity, a unit is
safe to rewrite, a migration stage is complete, or a release may proceed. It
supplies bounded static facts and explicit unknowns to agents, onboarding, work,
gates, and the migration-program UI. Only an explicit onboarding record can
advance an onboarding status or set `ready-for-work`.

The privacy boundary is hard: scanning, storage, reporting, context-pack
construction, and UI projection are local-only. The implementation contains no
network path, telemetry, hosted parser, registry lookup, remote embedding, or
LLM summarization. Artifacts and context packs contain pointers and derived
metadata, never copied source text. A scan refuses before writing unless the
private store is ignored by the repository VCS or an attended operator accepts
the exact unignored-path risk for that run.

## Goal

For a bounded legacy-migration scope and one worktree capture, slice 1 must
answer four questions:

1. Which migration units exist, and which source files make up each unit?
2. Which units directly depend on which internal or external targets?
3. Which user-visible features and executable entry points map to those units?
4. Which evidence is present, missing, or blocking before migration work starts
   on each unit?

A lead should be able to scan once, assign work by stable unit or feature ID,
and give every wrapped agent the same evidence snapshot. Slice 1 detects whether
that snapshot still matches the current whole scope; comparing the facts in two
runs is an S4 capability.

## Non-goals

The comprehension plane is not:

- an IDE index, language server, symbol search engine, or arbitrary graph-query
  service;
- a complete semantic call graph or a proof of runtime behavior;
- a runtime-observation, persisted-state, state-machine, or fleet-capability
  plane;
- a multi-run graph-diff or counterexample-analysis engine in slice 1;
- a vulnerability scanner, generic code-health score, complexity leaderboard,
  or dependency-upgrade recommender;
- a source-code mirror, full-text index, embedding store, or source excerpt
  cache;
- an automatic architecture, feature, or migration-plan author;
- a rewrite/transpilation engine or a system that edits client code;
- a replacement for onboarding claims, characterization tests, independent
  review, gates, closes, or human decisions; or
- a hosted service, remote dashboard, or multi-project intelligence warehouse.

Fixed single-run migration reports are in scope. An open-ended query language is
not. If a future request does not help bound, plan, execute, or verify a legacy
migration, it belongs outside task #55.

## System boundary

The plane sits between the repository and existing agenttalk workflows:

```text
legacy worktree
      |
      v
VCS privacy preflight
      |
      v
sanitized bundled worker -----> immutable scan run
                                    |       |
                                    |       +----> migration report CLI
                                    |
                 +------------------+------------------+
                 |                                     |
                 v                                     v
        bounded context-pack builder          validated #208 projection
                 |                                     |
                 v                                     v
        wrapped-agent brief                    local program UI

onboarding/config declarations ----> evidence links, never hidden authority

                    no component above may use the network
```

The CLI starts one bundled scanner worker with a sanitized environment. Adapters
run in-process inside that worker and receive only an allowlisted input object.
The worker reads the current worktree, local manifests, and explicitly linked
local agenttalk evidence. It does not fetch Git refs, resolve packages against a
registry, install parsers, call a model, or execute repository code. The worker
is agenttalk code, not a third-party analyzer process; admitting an external
analyzer requires a later security design because “installed locally” does not
prove “cannot use the network.”

## Core invariants

1. **Local-only:** every scanned input resolves inside the selected project
   root, and every plane output is written under `.agenttalk/`. No scanner,
   reader, pack builder, or projector code path opens a socket.
2. **Private by default:** generated structure is client data. The scanner
   verifies or explicitly records the VCS-ignore disposition before writing;
   no v1 command exports artifacts or serves them off-machine.
3. **Pointer-shaped evidence:** artifacts may store relative paths, symbol and
   interface names, normalized dependency identifiers, line locations, and
   hashes. They must not store source excerpts, comments, string-literal bodies,
   environment values, command output, prompts, or bus-message bodies.
4. **Immutable generations:** a published scan run never changes. Readers bind
   to a scan ID and manifest digest; a new scan creates a new generation.
5. **Unknown is first-class:** unsupported syntax, excluded paths, unresolved
   edges, conflicting detectors, missing evidence, and resource limits remain
   visible. They never collapse to “ready.”
6. **Evidence is not authority:** a scan signal cannot set a gate, close a work
   item, or advance a migration stage. Those workflows may cite it as evidence.
7. **Deterministic content:** identical scoped inputs, platform identity,
   configuration, and adapter versions produce the same canonical content
   digest. Run IDs, timestamps, exact-byte artifact digests, and manifest digests
   may differ and are excluded from this equivalence claim.
8. **Bounded consumption:** scan artifacts, CLI reports, UI responses, and agent
   briefs have declared byte/record ceilings. They truncate only bounded
   projections with explicit omitted counts; they refuse an oversized durable
   artifact instead of silently publishing a partial inventory.

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
latest fully published scan, and a bounded list of run summaries. Every index
also records the digest of its predecessor. Updating it is a compare-and-set:
the scanner replaces the index only if the current bytes still match the digest
captured when the writer lock was acquired. A mismatch is a concurrent/manual
writer conflict, leaves the prior index current, and returns a command error.

`scan.lock` permits one writer per initialized project. The scanner creates it
exclusively before enumerating source and records a random owner token, PID,
process-start identity, host identity, acquisition time, and predecessor-index
digest. A second scanner refuses while that owner is live. A lock is reclaimed
automatically only when the recorded local process identity is definitely dead;
an unverifiable or remote-looking owner requires the explicit attended
`--recover-stale-lock` action. PID reuse cannot prove death because the
process-start identity must also match.

Each directory under `runs/` is immutable. A scan writes to a uniquely named
directory under `.staging/`; `owner.json` there repeats the lock token. The
writer closes every artifact handle, validates all schemas, cross-references,
counts, and digests, and flushes the files before publication. Publication uses
this sequence:

1. Rename the staging directory to a new, never-before-existing
   `runs/<scan-id>/` path on the same volume. It never replaces a run directory.
2. Write the complete successor index to a unique sibling temporary file,
   flush and close it, recheck the predecessor digest, then replace
   `index.json`.
3. Release the lock only after the index replacement or a reported failure.

On Windows, all scanner handles are closed before either rename. A sharing
violation receives bounded exponential retries for at most two seconds. If it
persists, the operation fails and the old index remains current. Readers read
and close all of `index.json` before opening a run, so a reader that already
loaded the old catalog keeps a complete old generation. A crash after step 1
but before step 2 leaves a valid unindexed run, never a half-current run; v1 does
not silently adopt it.

At lock acquisition, the scanner reclaims only unpublished staging directories
whose contained `owner.json` has the expected schema, whose resolved path stays
under `.staging/`, and whose owner is definitely dead. Anything ambiguous is
reported and retained. `comprehension prune --staging` performs the same check
as an attended v1 maintenance action. This cleanup is not deletion of published
project memory.

Context packs are immutable local projections bound to one run digest and one
brief scope. They live separately because they can be generated after the scan
and have a different retention lifecycle.

Comprehension data is project memory, like onboarding and knowledge data. The
existing `agenttalk reset` must preserve published runs and packs. V1 never
automatically deletes anything under `runs/` or `packs/`; published-retention UX
remains a later, separately reviewed decision.

### Common JSON envelope

Every JSON document uses UTF-8 without a byte-order mark, stable key ordering,
stable record ordering, RFC 3339 UTC timestamps, and this common identity:

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
version. The strict JSON loader rejects duplicate object keys at every nesting
level before schema validation. A breaking field or semantic change increments
the version. A reader may explicitly migrate an older version in memory, but it
never rewrites an immutable run.

`scan.json` lists every artifact's relative path, exact-byte SHA-256 digest,
canonical content digest, record count, and schema version. All documents repeat
the scan ID. A mismatched ID, digest, count, or cross-reference invalidates the
run instead of producing a partial truth from mixed generations.

The canonical content digest is the SHA-256 of a specified canonical JSON
projection. That projection removes `scan_id`, `generated_at`, capture times,
lock/owner tokens, and any other generation identity; it retains schema
versions, source-scope identity, platform identity, adapter/configuration
versions, record IDs, record content, problem codes, and ordering. `scan.json`
computes its run-level `content_digest` from the ordered tuple of artifact type,
schema version, record count, and artifact content digest. Comparing this one
field answers whether two generations are content-equivalent. Manifest and
exact-byte digests still authenticate the concrete generation and normally
differ between rescans.

All persisted paths use project-relative POSIX spelling. Absolute paths,
`..` segments, NULs, URL-like values, and paths resolving outside the project
root are rejected. Tracked files use Git's canonical spelling when available;
untracked paths retain their on-disk spelling. Case-fold collisions are a scan
problem rather than two silently merged units.

### Scan manifest and problems

`scan.json` records:

- scan ID, status (`complete` or `degraded`), start and completion times;
- generator version and every bundled adapter name/version;
- source state: a local non-plaintext resolved-root binding, local VCS kind and exact
  revision when available, dirty flag, and dirty-worktree content fingerprint;
- platform identity: OS family, architecture, path-normalization version,
  case-sensitivity result, and filesystem Unicode-normalization policy;
- included submodule/dependency roots with local revision or content digest, and
  excluded roots with an explicit boundary reason;
- effective include/exclude rules, enabled adapters, and their configuration
  digest;
- a whole-scope fingerprint over the sorted path/type/content-digest tuples for
  every non-hard-excluded entry under the selected roots, plus boundary markers
  and the preceding platform/configuration identity;
- artifact paths, digests, schema versions, and record counts; and
- bounded summary counts for units, edges, features, entry points, readiness
  states, exclusions, unsupported files, generated/vendor handling, resource
  omissions, conflicts, and problems.

`scan.json`'s `unsupported_relations`, `unsupported_invoke_shapes`, and
`unsupported_entry_point_shapes` are a STATIC CAPABILITY DECLARATION - the
named, enumerated set of recognized-but-unmodeled shapes this producer VERSION
carries, published unconditionally on every run regardless of whether that run
actually contains a matching shape. A per-run INSTANCE of any of these gaps is
what `problems.json`'s own reason-coded records already surface, one per
affected file or unit - these three fields answer "what can this version not
model," never "did this run hit one."

The whole-scope fingerprint is wider than the set of files selected into a pack.
A new file therefore changes it even when no old selected-path digest changes.
Hard-excluded secret/VCS/cache content is represented by exclusion category and
count, not read or copied into the fingerprint.

`root_binding` is SHA-256 over a domain-separation prefix plus the canonical
resolved project-root spelling under the recorded path policy. It binds a run to
the exact local root without persisting the absolute path in artifacts. It is a
privacy minimization, not a cryptographic secrecy claim.

`problems.json` contains bounded, machine-readable problem records. Each record
has a stable ID and reason code, severity, producers, optional relative path and
line, and a generated message. It never copies parser output wholesale.
Examples are `unsupported_language`, `parse_failed`, `path_excluded`,
`dependency_unresolved`, `adapter_conflict`, `feature_conflict`,
`resource_limit`, and `case_collision`. `artifact_limit` is a command-result
reason because that failure publishes no `problems.json`.

A run is `degraded` if an enabled adapter fails, an input budget truncates the
scope, or part of the selected source is unsupported *and code-bearing* (see
"Unsupported-language degradation is tiered, not binary" below). Ordinary
unresolved dependencies and readiness blockers are domain findings and do not
by themselves make the scan command fail. A fatal configuration, confinement,
or publication error publishes no run. Exceeding the durable-artifact ceiling
also publishes no run; it does not truncate the inventory and call the result
valid.

#### Unsupported-language degradation is tiered, not binary

An amendment (task #55 slice-1 PR-B, fix rounds 16-17): the plain reading above
- *any* unsupported file degrades the run - was measured against real
repositories and does not hold: an ordinary Spring Boot repository scanned
`degraded` over its own Maven wrapper, its CI configuration, and its
`LICENSE` file. An unsupported extension resolves through THREE tiers, not
two:

1. **Adapter-handled** - a bundled adapter recognizes the language and parses
   it normally. Not part of this section.
2. **Recognized code-bearing, unsupported** - a CLOSED, PROVISIONAL list of
   extensions this producer has no adapter for yet. Membership is decided by
   ONE criterion: the extension names NEVER-INCIDENTAL application or
   database estate - a file with this extension is ALWAYS real,
   migration-relevant source in an ordinary Java repository, never a routine
   helper/tooling/asset script that merely happens to share the same
   language (a repository-wide `scripts/release.py` helper or a webapp's own
   static `app.js` asset are both routine and common, and FAILED this
   criterion on measurement - removed after initially being added on the
   weaker "any real programming language" reading). Every file matching this
   tier is recorded in `problems.json` (`unsupported_language`) AND degrades
   the run - the same standard a fresh migration reader applies ("would a
   reader say the inventory missed something they needed?" - the reader
   test a reviewer's own delta first established, reused unchanged for this
   tier). The current membership is a source-code fact, not a design fact -
   see `worker.py`'s own `_DEGRADING_CODE_EXTENSIONS` constant (and its
   neighboring criterion comment) for the list actually enforced; this
   section describes the RULE that constant must satisfy, not an inline copy
   of its contents, which would drift out of sync with the code the moment
   either changes independently.
3. **Everything else non-benign** - any extension neither adapter-handled,
   on a closed BENIGN allowlist (documentation, plain text, lockfiles,
   images), nor on the tier-2 list above. Still recorded in `problems.json`
   (never silently dropped - a file's mere absence from the closed
   recognized-code-bearing list is not evidence that it is tooling, only
   that this producer has not yet been taught to recognize it as
   application code) but does NOT degrade the run - a build/tooling/
   infrastructure/configuration file (`Dockerfile`, a Maven wrapper script,
   CI YAML, `.properties`/`.yml` application configuration) is not "missed
   application code" the same way an unrecognized JVM-language source file
   is, even though this producer cannot parse either one.

The tier-2 list is explicitly PROVISIONAL and expected to GROW (and
occasionally SHRINK, on the identical criterion, per measurement) as this
producer is measured against more real, polyglot repositories - an absent
entry under-claims degradation for a real application language this list has
not yet caught up to, but (per tier 3's own guarantee) never silently
vanishes the file from `problems.json` entirely. Changing the list is a
narrow, low-risk change (one frozenset entry plus a regression test); judged
not to require a design amendment of its own each time a reviewer ratifies a
change.

### Fact provenance and canonical merge

Every unit, edge, feature, entry point, readiness signal, and problem carries a
sorted `producers` list. Each producer entry names the bundled adapter and
version, extraction-rule version, grammar/indexer version when applicable,
effective configuration or policy digest, source content digest, and capture
time. A fact without one source path uses the scan-manifest basis instead of a
fabricated source digest. Capture time is generation metadata and is excluded
from canonical content digests; every other producer identity field participates.

Adapter scheduling never affects output. The normalizer sorts claims by
`(adapter_id, adapter_version, rule_version, canonical_fact_key)` and applies one
merge rule:

1. Byte-equivalent normalized claims coalesce; their producer and evidence lists
   are deduplicated and sorted.
2. Compatible partial claims merge only for fields whose schema defines set
   union. A missing scalar never silently wins over a present scalar.
3. Incompatible claims remain distinct and receive the same stable
   `conflict_id`. That ID is the SHA-256 of the conflict kind, normalized anchor,
   and sorted canonical claim digests, with generation identity removed.
4. `problems.json` records that `conflict_id`, every claimant, and the disputed
   fields. No adapter is chosen as authoritative by execution order. Dependent
   readiness stays `unknown` until an explicit declaration resolves the conflict.

This rule applies when two adapters assign different kinds or qualified names to
one path: the candidate units remain visibly grouped by `conflict_id`, rather
than appearing as unrelated duplicates.

## Artifact 1: module inventory

`modules.json` contains migration units. Every non-excluded file remains an
addressable `file` unit. A bundled adapter may additionally identify a package,
module, component, or service and contain those file units. Unknown languages
therefore retain file identity without pretending that their internals were
understood.

Each unit record contains:

| Field | Meaning |
| --- | --- |
| `unit_id` | Deterministic SHA-256 ID over unit kind, normalized path, and qualified name. |
| `kind` | Closed adapter vocabulary such as `service`, `package`, `module`, `component`, or `file`. |
| `display_name` | Bounded derived or declared label; never a source excerpt. |
| `language` | Detected language or `unknown`. |
| `paths` | Sorted relative source paths that make up the unit. |
| `source_digests` | Per-path hashes used as direct staleness evidence; they do not replace the whole-scope fingerprint. |
| `classification` | Set such as `production`, `test`, `generated`, `vendor`, or `infrastructure`. |
| `container_unit_id` | Optional parent unit for hierarchy, with no cyclic containment. |
| `producers` | Canonical producer identities and whether each claim was extracted, inferred, or declared. |
| `conflict_id` | Optional stable link shared by incompatible claims. |
| `evidence` | Bounded local evidence pointers. |

Renaming a unit changes its path-derived ID. Matching a rename by content hash
may appear in a future S4 two-run projection as a candidate, but it must not
silently rewrite identity. This keeps references auditable and avoids false
rename certainty.

## Artifact 2: dependency edges

`dependencies.json` stores only direct static extracted, inferred, ambiguous, or
declared edges. Transitive closure, cycles, hotspots, and impact summaries are
derived by report/UI code so they cannot drift from the edge set.

Each edge contains:

- a deterministic `edge_id` and `from_unit_id`;
- a target union: internal `unit_id`, normalized external package/system name,
  or an explicit unresolved identifier;
- a closed relation from the coarse slice-1 vocabulary: `import`, `include`,
  `inherit`, `invoke`, `route`, `data`, `configuration`, `build`, or `test`;
- phase (`runtime`, `build`, `test`, or `migration`) and optionality;
- an `evidence_class` of `extracted`, `declared`, `inferred`, or `ambiguous`;
- resolution state and optional confidence (`high`, `medium`, or `low`), which
  never changes the evidence class;
- canonical producers and an optional conflict ID; and
- one or more local evidence pointers.

An adapter may emit a relation only when its versioned extraction rule names a
producer for that relation. Unsupported relation types remain coverage gaps;
they are never coerced into `data` or another healthy-looking generic edge.
The enriched static relations `contains`, `defines`, `calls`, `implements`,
distinct `reads`/`writes`, `generates`, `configures`, `dispatches_to`,
`tested_by`, and `documented_by` require named S2 producers. Conditions and
conditional reachability also land in S2. Slice 1 reports those capabilities as
unsupported or unknown rather than emitting empty-looking facts. `persists`,
`retries`, `cleans_up`, and `observed_at_runtime` require the S3 runtime/state
producer and are invalid in a slice-1 artifact. No static adapter may mint
`runtime_observed` evidence.

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
owning unit, path/symbol pointer, linked feature IDs, evidence class, canonical
producers, optional conflict ID, confidence, and evidence.

A feature groups one or more entry points and units around a migration-relevant
behavior. Feature records have an ID, bounded label, state (`candidate` or
`confirmed`), origin (`detected` or `declared`), unit and entry-point links,
canonical producers, optional conflict ID, confidence, and evidence. A detector
may create only a candidate. Confirmation requires an explicit local declaration
in `config.json` or a supported pointer to confirmed onboarding evidence;
confidence alone never promotes a feature.

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
| `stored_status` | Scan-time `satisfied`, `unsatisfied`, `unknown`, or `not_applicable`. |
| `severity` | `blocker`, `warning`, or `information`. |
| `basis` | `detected`, `declared`, or `verified_external_evidence`. |
| `confidence` | `high`, `medium`, or `low`; never a substitute for status. |
| `reason_code` | Stable machine-readable explanation. |
| `evidence` | Source or local agenttalk evidence pointers. |
| `policy` | Readiness-policy ID/version/digest that required the check. |
| `producers` | Canonical scanner/policy producer identities. |

The default policy should cover only migration preparation that the available
evidence can support:

- source is included and understood by an adapter;
- direct internal dependencies are resolved or explicitly marked dynamic;
- externally visible entry points are mapped;
- the unit is linked to a confirmed feature or explicitly classified shared;
- build/test or characterization evidence is located, otherwise unknown;
- statically visible external integration, configuration, and data boundaries
  are identified, otherwise unknown.

The immutable artifact stores `stored_assessment_state`: `assessed`,
`needs_evidence`, `blocked`, or `not_applicable`. Any required scan-time blocker
that is unsatisfied yields `blocked`; any required scan-time unknown yields
`needs_evidence`. `assessed` means only that the static preparation evidence was
present at capture time. It does not mean implemented, parity-verified, accepted,
cutover-ready, or safe to decommission.

The default policy does not contain “target-stack disposition exists” or any
other check that changes with the migration program's current plan. Those facts
belong to onboarding/work records and may be joined by a later projection, but
they do not make immutable scan content a function of mutable program state.

## Evidence pointers and trust

All four artifacts use one evidence-reference union:

- source pointer: relative path, optional exact range/symbol, and source digest;
- comprehension pointer: scan ID, artifact type, and record ID;
- onboarding pointer: run ID, item kind, and item key;
- work/gate/close pointer: local record ID plus frozen revision when available;
  or
- declaration pointer: configuration digest and declaration key.

Pointers are data, not instructions. Consumers revalidate the target before
acting. A source pointer whose digest no longer matches is stale. External
onboarding/work/gate/close records can also change or disappear after a scan, so
stored and revalidated values have separate names and precedence:

- `readiness.json` and its manifest digest cover only `stored_status` and
  `stored_assessment_state` at scan time.
- `validate` reports artifact integrity from those stored values. It reports
  external-pointer revalidation in a separate result and never calls an intact
  immutable artifact corrupt merely because a referenced record changed.
- `report --json`, context packs, and `/api/comprehension` emit
  `stored_status`, `revalidated_status`, `revalidated_at`, and
  `revalidation_reason`. A missing, malformed, changed, or unverifiable external
  record yields `revalidated_status: unknown`.
- The projection's unprefixed `assessment_state` is always derived from
  revalidated statuses. It is the value used for report grouping, pack admission,
  and #208 rendering. `stored_assessment_state` remains visible for audit and
  never wins a conflict with a more conservative revalidated result.

Revalidation does not mutate or redigest the run. A later projection therefore
can become more conservative while the stored artifact remains reproducible.

The plane inherits the local store's trust model. It validates structure and
provenance links but does not claim that a same-OS-user-authored declaration is
cryptographically true.

## Scan behavior

One scan follows this fixed pipeline:

1. Resolve the initialized project root and load/validate local configuration.
2. Prove the private-store VCS disposition or obtain the attended per-run
   acknowledgement described below. This happens before staging or lock files
   are created.
3. Acquire the single-project writer lock and reclaim only definitely abandoned
   staging directories.
4. Enumerate allowed files without following links outside the root. Exclude
   `.git/`, `.agenttalk/`, binaries, known secret files, dependency caches, and
   generated/vendor trees by default; record every category and count. Compute
   the whole-scope fingerprint before adapter relevance filtering.
5. Start the sanitized bundled worker, hash included files, and run enabled
   adapters in-process there. Adapters receive bytes and relative paths, not
   ambient credentials or environment dumps.
6. Normalize records, resolve only evidenced edges, merge declarations, and
   retain conflicts and unknowns.
7. Evaluate the versioned static-readiness policy from the normalized artifacts.
8. Enforce artifact ceilings and perform full publish validation: strict JSON,
   schemas, confinement, referential integrity, deterministic ordering, counts,
   exact-byte digests, and canonical content digests.
9. Publish with the lock, Windows retry, and predecessor compare-and-set sequence
   defined above.

Symlinks are recorded as boundaries and not followed by default. An eventual
opt-in may follow a link only after resolving it and proving the target remains
inside the project root. Submodules are external boundaries unless explicitly
included and already present locally. The scanner never initializes or updates
them.

Resource caps apply to file count, individual file bytes, total bytes, nesting,
and adapter work. The initial protective values are all **PROVISIONAL**:

- 100,000 filesystem entries per scope — **PROVISIONAL**;
- 64 MiB per file — **PROVISIONAL**;
- 2 GiB of hashed source bytes per scope — **PROVISIONAL**; and
- 30 seconds for one read-time freshness pass — **PROVISIONAL**.

The slice-1 planning gate must confirm or revise all four values from an executed
measurement against a representative legacy corpus. That evidence pins the
exact corpus revision/content fingerprint, platform and path policy, file-count
and size distribution, cold and warm scan/freshness timings, peak memory, and
cap-hit outcome. The implementation plan cannot present these unmeasured values
as a proven JAWS-scale budget.

Until that measurement is accepted, treat the provisional values as fail-closed
guards. If a scan reaches an entry, per-file, or hashed-byte limit, narrow the
repeated `--scope PATH` selection to a smaller coherent repository region and
rescan until enumeration completes. If a freshness pass reaches 30 seconds,
narrow the same scope and rebuild the scan and pack until freshness is
`current`. Do not raise a cap ad hoc, call an incomplete fingerprint current, or
use headless dispatch to bypass the resulting `unknown`; only the attended
work-item waiver described below can admit that state.

Hitting a scan cap yields a degraded scan and explicit problem; the scanner does
not silently sample. Hitting a freshness-pass cap yields `unknown`. An incomplete
enumeration marks the whole-scope fingerprint incomplete, so no pack from that
run can later claim `current`. Scanner output is deterministic even when adapter
work is parallelized: merge and serialization order are canonical.

## Proposed CLI surface

The v1 surface stays migration-shaped and read-mostly:

| Command | Contract |
| --- | --- |
| `agenttalk comprehension scan` | Create and publish one immutable run for the current worktree. Supports repeated `--scope PATH`, `--exclude GLOB`, `--config PATH`, `--acknowledge-unignored-private-store`, `--work-id ID`, `--recover-stale-lock`, and `--json`. An unignored-store acknowledgement requires both an existing work ID and an attended terminal; both safety overrides require attendance. |
| `agenttalk comprehension status` | Show the latest run, completeness, source revision/fingerprint, freshness, adapter coverage, and problem counts. Supports `--run ID` and `--json`. |
| `agenttalk comprehension report` | Answer fixed single-run migration questions. Filters include `--unit ID`, `--feature ID`, `--readiness STATE`, and `--dependencies`; `--json` emits the validated projection. |
| `agenttalk comprehension pack` | Build an immutable brief-time context pack. Requires at least one `--unit`, `--feature`, or `--path`; optionally binds `--work-id`, `--run`, and `--max-bytes`. |
| `agenttalk comprehension validate` | Perform full-run integrity validation and separately revalidate external evidence pointers for one run or the latest run. Supports `--json`. |
| `agenttalk comprehension prune --staging` | Reclaim only definitely abandoned, unpublished staging directories. It never deletes runs or packs. |

Examples of the proposed syntax, not runnable in rev 2:

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
graph query, multi-run `diff`, contract-inventory query, background watcher,
auto-rescan daemon, managed export command, or published-data delete command.

### Validation tiers and size ceilings

Publish validation and explicit `validate` are full-run operations. They read
every artifact and verify schemas, all exact-byte and canonical digests, counts,
confinement, and cross-references. This cost is paid once on publication or when
an operator explicitly requests the deep check.

Ordinary reads are bounded and demand-driven. `status` verifies the index and
`scan.json`. `report`, pack construction, and `/api/comprehension` then verify
the exact-byte digest and schema of each artifact they actually load; they do
not rescan unrelated artifacts on every response. Cross-references from a loaded
record are checked before that record is emitted. External evidence revalidation
is a separate projection step and follows the stored/revalidated precedence
above.

The v1 single-document format has hard publish ceilings: 16 MiB and 100,000
records per artifact, and 64 MiB and 250,000 records for all durable artifacts in
one run. The lower limit wins. `scan.json` declares the measured byte and record
counts. Exceeding a ceiling publishes no run and reports `artifact_limit` with a
request to narrow scope; it never allocates or parses an unbounded document on a
normal read path. A later format revision may add deterministic sharding, but it
must preserve the same content digest before these ceilings can be raised.

## Brief-time context packs for wrapped agents

A pack is a deterministic, bounded JSON projection, not a model-generated
summary. It contains:

- pack schema version, ID, creation time, selected work ID, source scan ID,
  scan-manifest digest, and run content digest;
- a structured query descriptor: selector kind/IDs, fixed filters,
  configuration digest, and byte limit;
- the exact sorted sets of returned unit, edge, feature, entry-point, readiness,
  problem, and conflict IDs;
- the scan's whole-scope fingerprint, selected-path digests, and a three-state
  freshness result with reason and evaluation time;
- selected features, entry points, and units;
- one-hop incoming and outgoing dependencies, with unresolved edges first;
- revalidated blockers and unknown readiness signals before satisfied
  informational rows, with stored values retained for audit;
- coverage gaps, conflicts, unknowns, and local evidence pointers;
- byte count, truncation flag, omitted counts by section, and a continuation
  descriptor for a narrower follow-up report; and
- a structured reproduction descriptor containing run ID, manifest/configuration
  digests, selectors, filters, and cap.

The reproduction descriptor is data. It contains no shell, executable, command
string, working directory, or repository-derived argument text. The CLI may
validate those typed fields and construct its own local argument vector; a brief
never injects a “direct re-run” command assembled from scanned content.

The selection order is fixed: blockers/unknowns, entry points, selected units,
direct dependencies, then lower-priority context. Transitive closure is never
included by default. The hard pack cap is 64 KiB; the proposed default is
24 KiB. Truncation always preserves the identity/freshness header and omitted
counts.

At dispatch, a migration work item carries comprehension selectors and either a
required scan ID or `latest`. The wrapper resolves `latest` once and records the
exact scan ID before building the pack. It validates the loaded artifacts and
revalidates pointers before assembling the brief. It injects a small rendered
summary plus the local pack path and digest. The bus record stores the pack ID
and digest, not the whole graph.

The default is refuse, not warn-and-continue. An invalid pack or a freshness
result other than `current` cannot satisfy dispatch admission. The wrapper never
silently rescans or substitutes a newer generation for a work item bound to an
exact scan.

An attended operator may waive freshness for one work item and one exact pack.
The waiver is a typed record on that work item containing work ID, scan ID,
manifest digest, pack ID/digest, observed `stale` or `unknown` state and reason,
bounded operator rationale, operator identity, policy version, and timestamp.
The action requires an interactive terminal and explicit confirmation;
non-interactive dispatch cannot create it. A log warning or brief label is not a
waiver. The waiver permits admission but does not relabel the pack `current`,
change `assessment_state`, or authorize migration/release decisions.

The brief labels the pack advisory and shows its freshness or waiver record. It
tells the agent to verify source pointers locally and treat names, labels, and
declarations as untrusted data, never as instructions.

The pack contains no source excerpts, raw literals, prompts, command output, or
secrets. The existing wrapped-agent provider boundary remains unchanged: task
#55 neither uploads files nor introduces a second service that receives client
code. Agents use ordinary local file tools to inspect only the pointers needed
for their assigned work.

After a delivery changes a selected file, the old pack is provably `stale`. A
new or changed unselected scope path makes relevance undecidable and therefore
`unknown`. A new scan and pack are the ordinary way to restore `current` for the
next agent or review round. Findings that deserve durable human interpretation
still belong in onboarding or knowledge records, with pointers back to the scan.

## Contract for the migration-program UI (#208)

The browser must not parse run files directly. A server-side comprehension
projector validates one run and emits a bounded, versioned
`GET /api/comprehension` response. The same projector backs
`comprehension report --json`, preventing CLI/UI semantic drift.

The endpoint follows the existing `/api/onboarding` precedent in
`src/agenttalk/web.py`: bounded summaries plus local evidence pointers, never raw
record bodies. It also inherits both existing loopback controls: server bind
validation and the per-request peer check. The contract is always GET-only and
has no action endpoint; `POST`, `PUT`, `PATCH`, and `DELETE` return HTTP 405 even
when the console starts with `--enable-actions`.

In a multi-root console, `?root=` selects exactly one configured root through the
same Team Console root resolver. With no parameter, the endpoint uses
`roots[0]`; an unknown or ambiguous root is a client error and never falls back
silently. Every projection names the selected root by its local non-plaintext
root binding. The UI uses packaged assets only.

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

The plane can substantiate evidence displayed during #208's assessment stage,
but it cannot set `assessment-complete`. Any status advance requires an explicit
onboarding record at a frozen revision, following R-22 in the companion
requirements. The later stages (`plan-approved`,
`reimplementation-complete`, `parity-verified`, `acceptance-ready`,
`cutover-ready`, and `legacy-decommissioned`) likewise come from work, test,
gate, close, onboarding, and operator records. The UI never infers or advances a
stage from a scan, freshness result, policy, or `assessed` unit.

Slice 1 projects exactly one run per response. Multi-run trends and graph deltas
belong to S4; the slice-1 API does not combine counts from generations.

## Privacy and offline enforcement

Generated graph structure is client data even when it contains no source text.
Before creating `.agenttalk/comprehension/`, `scan` performs a VCS privacy
preflight:

1. For Git, reject if any path under `.agenttalk/comprehension/` is already
   tracked, then use Git's ignore matcher on a synthetic child path to prove the
   directory is ignored. Other supported VCSs require an equivalent native
   ignore query; guessing from file text is not proof.
2. Record `vcs_privacy` as `ignored`, `acknowledged_unignored`, or
   `no_vcs_acknowledged`, including VCS kind, matched rule when available, and
   the bound work ID for either acknowledgement.
3. If ignore status is unprovable or false, refuse before any plane output is
   written. The only override is the attended
   `--acknowledge-unignored-private-store` action, which displays the resolved
   local target and `git add -A` risk, requires confirmation, and applies to one
   run bound to an existing work item. The work item records the root identity,
   risk, operator, time, and eventual scan ID. Scripts and wrappers cannot supply
   the acknowledgement.

V1 defines **export** as deliberately moving any raw artifact, report, pack,
query descriptor, path/symbol list, or graph-derived file outside the private
store; staging it in VCS and serving it beyond loopback are also export. V1 has
no managed export command and never performs those actions. A future managed
export must have a separate threat review, an explicit destination, operator-
reviewed redaction preview, confirmation, and a durable audit record. Agenttalk
cannot prevent a same-OS-user from copying a file manually, but that copy is
outside the plane's guarantee and is never described as safe by default.

“Local-only” is an acceptance criterion, not a deployment preference:

- production scanner/projector modules may not import or call network clients,
  sockets, URL openers, package managers, `git fetch`, hosted models, or remote
  language services;
- the CLI launches the bundled worker without provider credentials, proxy
  variables, remote endpoints, or inherited configuration that names a network
  service; sanitizing the child environment does not mutate the parent agenttalk
  process;
- v1 adapters are bundled and in-process within that worker, read-only,
  deterministic, and receive an allowlisted input object rather than the ambient
  environment;
- a dependency/import allowlist rejects network-capable modules from the
  production scanner package, while the external CI denial harness records and
  rejects any attempted connection;
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

Slice 1 does not claim that ordinary production execution is an OS sandbox.
Production enforcement is the closed bundled code path, sanitized child
environment, strict configuration, and audited dependency boundary. Release
evidence must additionally run the
end-to-end scanner with networking denied outside the process: a network
namespace or `--network=none` container on Linux, and a dedicated Windows runner
with an outbound firewall rule scoped to the worker executable (or Windows
Sandbox with networking disabled). The test places provider keys and proxy
variables in the parent environment and asserts both zero connection attempts
and their absence in the worker.

Additional tests use unique secret/comment/literal canaries that must not appear
in any artifact, report, pack, or API projection; malicious path and symlink
fixtures; and offline failure-path cases. An external Graphify or other prior-art
component is not part of slice 1. Admitting one later requires its own pinned,
network-denied design and cannot weaken this boundary.

## Freshness, admission, and failure behavior

Freshness is a read-time projection with exactly three values. It is computed
against the scan's whole-scope fingerprint, never only the paths returned in a
pack:

| Value | Exact condition |
| --- | --- |
| `current` | The run is valid; the current enumeration completed under the same root, platform/path policy, scope/configuration, and adapter identities; its whole-scope fingerprint equals the stored fingerprint; and every selected source pointer still matches. |
| `stale` | At least one selected input path is proven changed or deleted, or the caller requested an exact VCS revision that is proven different. Direct proof of staleness wins even if the whole-scope fingerprint also differs. |
| `unknown` | Re-enumeration is incomplete or errors; root/platform/configuration/adapter identity differs; the scan's scope fingerprint was incomplete; or the whole-scope fingerprint differs without direct selected-path proof. A newly added or changed unselected path therefore yields `unknown` because its relevance cannot be decided from the old pack. |

No scope-level mismatch can produce `current`. A status of `unknown` is not a
weaker spelling of fresh. The result, reason code, current fingerprint when
available, and evaluation time appear together; the immutable pack is not
rewritten. Dispatch requires `current` unless the attended work-item waiver
defined above exists.

Other failures follow these rules:

- A missing plane yields `not_scanned`, never an empty or healthy assessment.
- An unsupported newer schema or duplicate JSON key yields
  `unsupported_schema`/`malformed`, no projection, and no pack.
- A malformed artifact or manifest mismatch invalidates the generation. Readers
  may fall back only when the caller explicitly requests an older run.
- A parser failure yields a bounded problem and unknown signals for affected
  units. It does not erase units detected by other valid adapters.
- A live writer lock refuses a second scan. A stale lock whose death cannot be
  proven also refuses until an attended recovery.
- A compare-and-set conflict or exhausted Windows sharing-violation retry leaves
  the prior index current and returns a command error.
- A concurrent scan cannot disturb readers of the prior published generation.
- An input resource-limit hit yields a degraded run with exact omitted counts
  where knowable and an incomplete fingerprint; no automatic sampling is called
  complete or current.
- An output artifact-limit hit publishes no run.
- An empty selected scope is a command error, not a valid zero-unit scan.

## Delivery slices and targeted evidence

The companion requirements use these delivery tiers. Only S1 is normative for
this design revision:

| Slice | Contract | Producer status |
| --- | --- | --- |
| **S1 — static inventory** | Local, single-run repository inventory, coarse direct static edges, feature/entry-point map, static readiness projection, generic bounded pack, and immutable publication. | This document defines the producer and storage/read contracts. |
| **S2 — enriched static contracts** | Symbol/contract inventory, build/configuration entities, conditional static reachability, and richer negative-space accounting. | Later target; no producer is designed. S1 reports the capability as unsupported or unknown. |
| **S3 — runtime, state, and fleet evidence** | Runtime observations, persisted-state/lifecycle facts, fleet/config snapshots, expiry, and joins to static facts. | Later target; no producer, retention model, or threat model is designed. |
| **S4 — role-specific projections** | Lead/reviewer projections, base-to-head fact delta, counterexample evaluation, and human-reviewed boundary candidates. | Later target depending on S2/S3; S1's generic pack cannot claim it. |

S1 itself can be implemented in reviewable increments without changing its
acceptance contract:

1. Strict schemas/readers, fact provenance, canonical content digests, writer
   lock, staging recovery, and cross-platform publication.
2. The sanitized worker, bundled local adapters, `scan`, `status`, `report`, and
   `validate`.
3. Whole-scope freshness, generic pack selection, default-refuse admission, and
   attended work-item waivers.
4. The bounded GET-only projector consumed by task #208.

Each increment needs targeted evidence: duplicate-key/future-schema failures;
two byte-identical source scans with different IDs but equal content digests;
adapter-order and conflict-ID determinism; injected crashes at every publish
step; stale-lock, staging-reclaim, predecessor-CAS, and Windows sharing-violation
fixtures; old-generation concurrent readers; VCS ignored/unignored/no-VCS
admission; full-scope new-file `unknown`, selected-file `stale`, exact-match
`current`, and waiver records; stored-versus-revalidated readiness; artifact
ceilings; representative-corpus measurement of all four provisional
scan/freshness limits; symlink/root escape; secret/comment/literal canary
non-disclosure; Linux and Windows network-deny jobs; bounded pack truncation;
and CLI/API projection parity. Windows crash safety is a first-class test, not
subsumed by a generic “atomic rename” test.

## Requirements and review-item reconciliation

This matrix is the contract between this design and the tiered requirements at
companion commit `8b064acd0419813b1f28df34159e468a18f9fd4f`. An S1 row below
names a producer in this document. A later row is deliberately non-normative for
S1 and names the destination and present residual. No “pending rev 2”
disposition remains.

| Requirement / review item | Tier | Disposition in this design |
| --- | --- | --- |
| R-01 / X-1 | S1 | The sanitized bundled worker, prohibited network code paths, audited dependency boundary, and Linux/Windows CI denial harness provide the honestly bounded offline mechanism. No portable production OS sandbox is claimed. |
| R-02 / X-2 | S1 | VCS-native ignore proof or attended work-bound acknowledgement is required before writing; v1 has no export/non-loopback surface and defines manual copy/staging as out-of-band export. |
| R-03 | S1 | All S1 extraction and projection is deterministic and model-free. |
| R-04 | S1 | The worker neither executes repository code nor starts external analyzers/build tools. |
| R-05 / X-3 | S1 | The manifest binds root identity, VCS revision when present, dirty/full-scope content state, path scope, submodule/dependency roots, platform/path semantics, configuration, and capture time. |
| R-06 / X-4 | S1 | Every emitted fact carries producer/rule/grammar/configuration/source basis and capture time; run-only provenance is insufficient. |
| R-07 / X-5 | S1 | Strict duplicate-key decoding, exact schemas, digests, and all-or-nothing publication fail closed. |
| R-08 | S1 | The single-writer lock, provable stale recovery, staging reclamation, predecessor compare-and-set, and Windows-aware publish sequence are the producer. |
| R-09 / X-6 | S1 | Freshness is whole-scope `current`/`stale`/`unknown`; default admission refuses stale/unknown and only a typed attended work-item waiver bypasses it. |
| R-10a / X-7 | S1 | `scan.json`, file-backed units, features, entry points, and dependency targets produce the static entity subset. Every included file remains addressable. |
| R-10b / X-7 | S2/S3 | Symbol/type/callable, configuration, build, test, documentation, persisted-record, process-instance, state, and event entities are deferred to S2/S3. S1 has no producer and reports unsupported/unknown. |
| R-11a / X-8 | S1 | `dependencies.json` produces the closed coarse direct set: `import`, `include`, `inherit`, `invoke`, `route`, `data`, `configuration`, `build`, and `test`. |
| R-11b / X-8 | S2 | Enriched static relations need named S2 producers; S1 does not mint them from coarse edges. |
| R-11c / X-8 | S3 | Persistence/retry/cleanup/runtime-observed relations need the S3 producer; they are invalid S1 facts. |
| R-12a / X-9 | S1 | Every static edge separates evidence class from confidence and exposes canonical conflicts. |
| R-12b / X-9 | S3 | `runtime_observed` belongs only to S3 with capture identity and expiry. |
| R-13a / X-10 | S2 | Conditional static reachability is deferred to S2; S1 returns the missing capability as unknown. |
| R-13b / X-10 | S3 | Active-path proof needs versioned configuration/runtime evidence from S3; S1 never claims active. |
| R-14 / X-11 | S2 | Indexed contract inventory is deferred; no S1 CLI query or completeness claim exists. |
| R-15a | S1 | The manifest, `problems.json`, report, and pack expose indexed/excluded scope, unsupported inputs, errors, unresolved dependencies, generated/vendor handling, caps, conflicts, truncation, and knowable counts. |
| R-15b | S2/S3 | Symbol/build/config gaps and unobserved/expired runtime branches are later-layer coverage, not S1 claims. |
| R-16 / X-12 | S4 | A role-specific lead projection and S3 fleet input are deferred. S4 may offer boundary candidates, but the human lead authors any work split; automatic planning remains a non-goal. |
| R-17a | S1 | The generic pack produces exact snapshot, bounded paths/units/direct edges/features/readiness, pointers, coverage, conflicts, and omissions. |
| R-17b | S4 | Joined symbol/data/persistence/config/test/doc projections depend on S2/S3 and are not in the S1 pack. |
| R-18 / X-13 | S4 | Base-to-head fact delta and the reviewer projection are deferred; slice 1 is explicitly single-run. |
| R-19 / X-14 | S3 | State/event/lifecycle queries require a separate S3 producer. |
| R-20 / X-15 | S4 | Counterexample evaluation over captured configuration/fleet samples is deferred with no S1 producer. |
| R-21 | S1 | Full artifacts/packs stay in the private content-addressed store; bus/onboarding records contain bounded IDs, digests, and pointers. |
| R-22 | S1 | Stored/revalidated scan evidence is advisory; only explicit onboarding/run-state records change workflow status. |
| R-23a / X-16 | S1 | The schema rejects runtime-observed, persisted-state, and fleet facts from static adapters. |
| R-23b / X-16 | S3 | A separate runtime/fleet producer, identity, expiry, and threat model are deferred to S3. |
| R-24 / X-17 | S1 | Packs carry selectors, generation/config digests, exact returned IDs, pointers, coverage/conflicts/unknowns, omissions, and a structured reproduction descriptor with no command string. S4 adds role-specific views later. |

The same tiering applies to the companion acceptance scenarios. S1 is the first
passing slice for scenarios 1–5 and 11–13. Scenario 6 and contract scenario 8
first pass in S2; runtime scenario 7 first passes in S3; state-plus-delta scenario
9 and role/counterexample scenarios 10 and 14 first pass in S4. None is silently
used as an S1 release gate.

## Open questions for adversarial review

These choices can narrow adapters or UX, but cannot relax the S1 invariants,
freshness semantics, boundedness requirement, or tier boundary:

1. **Adapter scope:** Which bundled languages/frameworks form the smallest useful
   S1? The answer cannot introduce an external analyzer or a relation without a
   named versioned producer.
2. **Feature confirmation:** Is a versioned `config.json` declaration sufficient,
   or should #208 display `confirmed` only after a typed onboarding record and
   checker? Either choice uses stored/revalidated precedence.
3. **Readiness policy:** Which static preparation checks are mandatory by unit
   kind? Target-stack disposition and current program-plan state are explicitly
   excluded.
4. **Provider minimization:** Should a wrapped brief inline the bounded structural
   summary or only the local pack identity/path? Neither choice may add source
   text or weaken admission.
5. **#208 code ownership:** Should the pure canonical projector live with #55 or
   be imported by #208? The GET-only schema, root selection, projection caps,
   and semantic parity are already fixed here.
6. **Published retention:** What explicit, recoverable UX should eventually prune
   immutable runs and packs? V1 reclaims unpublished staging only.
7. **Optional prior art:** Does a pinned Graphify or other extractor pass the
   companion spike? It is not an S1 dependency and cannot be admitted without a
   separate security and license review.

## Rev 2 acceptance boundary

Rev 2 is ready for implementation planning only after the adversarial reviewer
accepts this S1 artifact/storage contract together with the companion's matching
tiered requirements. Acceptance covers stored-versus-revalidated readiness,
whole-scope freshness and dispatch admission, deterministic content identity,
VCS/export controls, lock/publication/recovery semantics, bounded read cost, the
GET-only console posture, and every X-1 through X-17 disposition above.

No implementation may treat S2–S4 target architecture as shipped or silently
manufacture its missing producers. The CLI spelling and record sketches remain
proposed until implementation lands with the targeted evidence listed here.
