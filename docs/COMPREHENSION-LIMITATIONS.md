# Comprehension producer — known limitations register

Status: PR #132 close-out artifact. Date: 2026-09-07.

Audience: anyone deciding whether to merge PR #132, and anyone picking up the
accepted incremental extraction-layer replacement afterward (real XML parser
for pom.xml/web.xml, then tree-sitter Java behind the adapter interface, then
an explicit binding stage, then a measured old-vs-new comparison — see the
companion migration issue).

This document is this PR's own honesty artifact: the platform (privacy,
storage, artifact envelopes, the CLI/lock/staging lifecycle) is proven by
~50 rounds of adversarial review; the extraction layer (the single-file,
regex-based Java/pom.xml/web.xml adapter, and the discovery/publish
filesystem layer around it) has known, enumerated edges. Every row below is
sourced from one of three places and verified against the CURRENT code (not
carried forward from stale prose): (1) existing in-code caveat constants and
`NAMED LIMIT` comments, (2) the adapter's own closed-vocabulary "unsupported
shape" audit tables, (3) the cross-vendor review's 223-comment sweep on this
PR, triaged by direction-of-error (see the sweep's own PR comments for the
full per-comment enumeration and bucket reasoning).

Two wrong-data-capable findings from source (3) were fixed during this same
close-out (MICRO-NOD 50b F6, F7) rather than merely registered — they are
listed here as CLOSED, with their commit, for completeness of the audit
trail.

## 1. Structural/provenance caveats (source: existing in-code constants)

These are pre-existing, already-declared gaps this PR did not introduce -
each is published machine-readably in the run's own artifacts (never left to
be independently rediscovered by a consumer), and each is exercised by that
module's own test suite.

| # | Limit | Honest behavior when hit | Locking test | Retired by the parser replacement? |
|---|---|---|---|---|
| S1 | `assessment_state` is a constant (`needs_evidence`) for every unit this slice - no producer yet for `boundaries_identified`, and `blocked` is unreachable (`source_understood` never returns unsatisfied). | Declared: `ASSESSMENT_STATE_CAVEAT` (readiness_artifact.py), published in scan.json. | tests/test_comprehension_readiness_artifact.py | No - a readiness-model gap, orthogonal to which parser extracts Java facts. |
| S2 | Producer/evidence provenance (config/policy digest, capture time), scan.json's VCS revision/dirty-state binding, problems.json's own missing producers field, `--acknowledge-unignored-private-store`'s `work_id` is caller-supplied and unverified, `revalidated_*` published only at summary (not per-signal) granularity, and unsegmented `entry_points_by_kind`/`dependency_summary` counts (test vs production) - six distinct declared gaps. | Declared: `PROVENANCE_CAVEAT` (readiness_artifact.py), published in scan.json. | tests/test_comprehension_readiness_artifact.py, tests/test_comprehension_scan_pipeline.py | No - a provenance/publishing-layer gap, not an extraction-correctness one. |
| S3 | `whole_scope_fingerprint` is entry-level, not content-level, for content entirely inside an already-excluded generated/vendor or dependency-cache region - a content change there does not change the fingerprint unless it also flips externality-poisoning. | Declared: `FINGERPRINT_CAVEAT` (discovery.py). | tests/test_comprehension_discovery.py | No - a discovery/fingerprint-layer gap. |
| S4 | `unmapped_entry_points` is always empty and every feature's own state is `candidate` this slice (both structural consequences of config.json parsing not being implemented yet, not a producer defect). | Declared: `FEATURES_STRUCTURAL_CAVEAT` (features_artifact.py). | tests/test_comprehension_features_artifact.py | No - config.json parsing is a separate, not-yet-built plane. |
| S5 | A `complete` run status does not warrant that every non-adapter-handled XML file was positively confirmed benign - an encoding-undecodable XML file records a real `encoding_undecodable` problem and publishes no classification, without degrading the run. | Declared: `CLASSIFICATION_CAVEAT` (modules_artifact.py). | tests/test_comprehension_modules_artifact.py | Partially - a real XML parser reports encoding failures more precisely, but "complete ≠ every file confirmed" remains an honest, permanent distinction. |
| S6 | `root_binding` is verified once, at scan (write) time, by the writer lock - no read command (status/report/validate) re-checks it against the root a read is actually invoked against; a moved/copied run directory still reports the stale value. | Declared: `ROOT_BINDING_VERIFICATION_CAVEAT` (scan_pipeline.py), published via `root_binding_verification_caveat`. | tests/test_comprehension_scan_pipeline.py | No - a storage/integrity-layer gap. |
| S7 | The secret-file exclusion list (`_SECRET_FILE_PATTERNS`) is a closed, provisional basename/extension set - a genuinely secret-shaped path absent from it is published as an ordinary discovered file (path + digest, never content); the reverse direction (a real code-bearing file whose name collides with a secret pattern) is declared via a degrading `secret_pattern_matched_code_bearing_file` problem. | Declared: `SECRET_PATTERNS_CAVEAT` (discovery.py). | tests/test_comprehension_discovery.py | No - a discovery-layer exclusion-policy gap. |

## 2. Enumerated "unsupported shape" audit tables (source: existing closed-vocabulary constants)

These are the adapter's own explicit, static capability declarations - each
name in each tuple is a REAL, recognized shape the adapter chooses not to
model, never a silent absence.

| # | Table | What it declares unsupported | Honest behavior when hit | Retired by the parser replacement? |
|---|---|---|---|---|
| U1 | `UNSUPPORTED_RELATIONS` (adapters/java.py) | `data`, `configuration`, `include` relations (a pom's own `<parent>` coordinate; reactor `<modules>` aggregator entries as their own edges) - would need call/type resolution beyond declaration. | Reported as explicit, enumerated coverage gaps, never coerced into a healthy-looking generic edge. | Partially - tree-sitter Java plus an explicit binding stage make `data`/`configuration` resolution tractable; `include` (a build-tool concept) stays out of scope regardless of parser. |
| U2 | `UNSUPPORTED_INVOKE_SHAPES` (adapters/java.py) | `constructor_call` (`new Foo(...)` mints no invoke edge), `instance_qualified_call` (a lowercase-led qualifier, e.g. a field-injected collaborator's own method call). | No invoke edge published; declared, not guessed. | Yes - a real grammar-aware parser plus a binding stage can resolve constructor targets and instance-qualified receivers that this adapter's own qualifier-must-look-like-a-type heuristic cannot. |
| U3 | `UNSUPPORTED_ENTRY_POINT_SHAPES` (adapters/java.py, ~15 named shapes) | Recognized-but-unmodeled entry-point mechanisms: JAX-WS `@WebMethod`, JAX-RS verb-only methods/sub-resource locators, `@Scheduled`, message-listener annotations, `@MessageDriven`/`@Remote`/EJB, WebSocket `@ServerEndpoint`, `<listener>`, servlet-name-scoped filters, startup-only servlets, `.jsp` implicit servlets, and the registrability-matrix pair (a route/servlet declared on a class the container would never actually instantiate). | Each publishes a bounded, declared `unsupported_entry_point_shape` problem, attributed to its own owning class when determinable - never a fabricated entry point, never a silent absence either. | Mixed - the registrability-matrix pair and JAX-RS shapes benefit from real type resolution (tree-sitter + binding stage); the framework-specific runtime mechanisms (`@Scheduled`, message listeners, EJB, WebSocket) are a modeling-scope decision independent of which parser reads the source, and stay declared regardless. |

## 3. The 31 distinct findings from the PR's own 223-comment cross-vendor sweep

Full per-comment enumeration, evidence, and the direction-of-error reasoning
for every row below are posted as PR comments on this PR (the comment-sweep
deliverable); this table is the register's own condensed form, one row per
DISTINCT underlying defect (most were posted 2-6 times across review rounds
against drifting line numbers - see the sweep summary for the comment-ID
groupings this table collapses).

### 3a. Wrong-data-capable (bucket A) - both fixed during this close-out

| # | Limit (now fixed) | What was wrong | Locking test | Status |
|---|---|---|---|---|
| A1 | Same-package-sibling resolution derived "package" by rsplitting a possibly-nested edge's own qualified name. | A type nested 3+ deep, referencing a bare name genuinely undeclared in its file, could resolve with confidence to an UNRELATED class in a different file sharing a duplicated top-level name. | tests/test_comprehension_scan_pipeline.py (the two new F6 tests) | **Fixed - MICRO-NOD 50b F6, commit 74a21f9.** |
| A2 | Every import/inherit/invoke edge hardcoded `phase="runtime"` regardless of the declaring file's own source root. | A class under `src/test` published every one of its own edges as a production/runtime dependency - a literal false fact in the published dependency graph. | tests/test_comprehension_adapter_java.py (the three new F7 tests) | **Fixed - MICRO-NOD 50b F7, commit f9e4be6.** |

### 3b. Safe-direction (bucket B) - register rows, migration-issue fodder

| # | Limit | Honest behavior when hit | Comment IDs | Retired by the parser replacement? |
|---|---|---|---|---|
| B1 | Local classes (declared inside a method body) sharing a simple name are not disambiguated by lexical/method position - verified this can only miss or report `ambiguous`, never resolve wrong (an edge FROM one of the duplicates falls back to file-level attribution, bucket-C item C1 below; a bare reference TO the ambiguous name correctly reports `resolution_state: "ambiguous"`). | Ambiguous/ file-level fallback, never a silent wrong pick. | 3886401210 | Yes - tree-sitter Java's real scope tracking distinguishes local classes by their own lexical position natively. |
| B2 | Worker/discovery byte-digest reconciliation - the worker re-reads every file's bytes independently in its own subprocess; nothing compares that re-read against discovery's earlier hash (a per-file worker-side digest existed once and was removed for "zero consumers" before this gap was closed). | Source_digest recorded reflects discovery's read, not necessarily the exact bytes the worker parsed if the file changed mid-scan. | 3877067360, 3878450466, 3882332944, 3884350314, 3885562388, 3886178997 | No - an inter-process TOCTOU concern, orthogonal to which parser reads the bytes. |
| B3 | No regular-file/FIFO check before `read_bytes()` in discovery's walk. | Worst case: the scan hangs (availability), never a false published fact. | 3877067415, 3880599235, 3883990578, 3885171827, 3886179013 | No - a filesystem-walk hardening gap. |
| B4 | `MAX_FILESYSTEM_ENTRIES` counts only regular files, never directories or boundary entries. | The nominal cap is weaker than its own documented intent for a tree dominated by non-file entries. | 3877067418, 3878450473 | No. |
| B5 | `os_family` stores raw `os.name` ("posix" for both Linux and macOS). | A true but coarse value is published, never a false one. | 3877067433, 3885562401 | No. |
| B6 | Per-file byte cap checked once via `stat()` before the read, never rechecked against the actual bytes read. | A file that grows between stat and read is not caught by a post-read recheck (only the separate whole-scan aggregate cap runs after). | 3877277267, 3882596320, 3886679626 | No. |
| B7 | Worker subprocess `cwd` is not isolated - `python -m` still inserts the inherited working directory ahead of the sanitized `PYTHONPATH`. | Requires an adversarial local package to matter (not realistic ordinary input); a supply-chain hardening gap, different axis from extraction correctness. | 3877845740, 3882333029, 3885037653, 3886179001 | No. |
| B8 | Windows publish retry loop wraps both the rename and the `owner.json` cleanup in the same `except PermissionError`. | A cleanup-only `PermissionError` after a successful rename can raise an uncaught `FileNotFoundError` on retry - a reliability/crash risk, never a false published fact. | 3881776972, 3885037650, 3886015689 | No. |
| B9 | Artifact byte ceilings (`ceilings.py`) are enforced only at publish/write time, never re-checked on the status/report/validate read path. | Resource-exhaustion-on-read risk, not a false fact. | 3882332980, 3884552131, 3886132456 | No. |
| B10 | Discovery symlink/directory-swap TOCTOU - `_boundary_kind`'s `lstat` and the later `is_dir()`/descent are separate syscalls with a race window. | Requires an active attacker mutating the filesystem mid-scan; flagged for hardening given this producer's own Cluster-0 symlink-confinement precedent, but not realistic ordinary input. | 3880599240, 3884552143 | No. |
| B11 | Empty-scope refusal (`if not discovery_result.files: raise ScanRefused`) doesn't check `discovery_result.problems` first. | A scope where every file was excluded by a resource-limit problem is refused identically to a genuinely empty root - same outcome, coarser reason. | 3881776981, 3886015685 | No. |
| B12 | Submodule classification runs after generic directory exclusion - a submodule literally named `vendor`/`build`/`target` is excluded generically, never reaching submodule-specific handling. | The directory is excluded either way; only the recorded reason differs. | 3882332971 | No. |
| B13 | `cli.py`'s `prune --staging` action has no attended-confirmation gate, unlike scan/lock-recovery/privacy-acknowledge. | A process-safety gap, not a data-correctness one. | 3882333018 | No. |
| B14 | Predecessor index digest is captured before the scan lock is acquired. | A narrow race on an already-informational value; the published index itself is not falsified. | 3883990593 | No. |
| B15 | Dependency-edge coalescing on an `edge_id` collision doesn't compare/surface conflicting scalar fields like `optional`. | Keeps the first-seen record's own true value verbatim; drops information about the other instance's claim rather than asserting a wrong one. | 3884350384 | Yes - an explicit binding stage can detect and surface genuine multi-declaration conflicts instead of picking first-seen. |
| B16 | `scan_pipeline.py` record converters catch only `(KeyError, TypeError)`, not `ValueError`. | A malformed list-shaped value can raise an unhandled `ValueError` instead of a typed validation failure. | 3885037660, 3886401209 | No. |
| B17 | Case-collision detection (`find_case_fold_collisions`) considers only `discovery_result.files`, never boundary paths. | Under-detects a different problem class (case collisions among boundaries); never publishes a false fact about the paths it does check. | 3885171819 | No. |
| B18 | Platform-detection probe directory has no symlink confinement check before probes write through it. | A hardening gap on an internal, one-time probe - not a fact this producer publishes about the scanned repo. | 3885562400 | No. |
| B19 | `.gitmodules` symlink is not rejected before the (already-hardened, quoting-aware) git-config pre-read. | The contents get parsed correctly either way; a hardening gap on the admission check itself. | 3886015680, 3886582641 | No. |
| B20 | `owner.json` is stripped after the publish rename, not before. | A crash between the two steps leaves an internal ownership marker behind; doesn't falsify any published artifact. | 3886679632 | No. |
| B21 | Artifact writes are not `fsync`'d before the index publish (unlike `publish.py`'s own index-replacement path). | A crash could lose durability, never publish wrong data. | 3877845752 | No. |

### 3c. Polish / declared-limit-adjacent (bucket C)

| # | Limit | Disposition | Comment IDs | Retired by the parser replacement? |
|---|---|---|---|---|
| C1 | Duplicate-qualified-name edges fall back to the file-level unit rather than the specific per-type component. | Register footnote - the same shape as `EntryPointRecord.owning_unit_id`'s own existing `NAMED LIMIT` (PR-B round 44, N1) for duplicate-type entry points; each edge still resolves its own correct target (verified under B1 above), only the owner attribution is coarsened. | 3883990582, 3886582633 | Yes - an explicit binding stage can attribute to the exact declaring component even under a duplicate qualified name. |
| C2 | Valueless Spring/route mapping (no class prefix, no method value) publishes a synthetic id instead of the real root path. | Cosmetic - the route's own existence is still recorded; only its display name is a placeholder instead of `/`. | 3883990598, 3885562391, 3886179016 | Yes - real parsing removes the need for this fallback entirely. |
| C3 | No cap on the number of extracted worker claims/edges (only an 8 MiB input-byte cap exists). | Resource/DoS-adjacent, not a data-correctness gap. | 3883990609, 3886179008 | No. |
| C4 | Network-deny CI workflow exercises only narrow worker/adapter tests, never a full `run_scan` under the network-denial boundary. | A documented, deliberate CI-scope decision (approved PR-B plan item 10); already declared-limit-adjacent. | 3884552127 | No. |
| C5 | Hard-exclude/VCS directory names (`.git`, `.agenttalk`) and other closed name sets are not case-folded on case-insensitive filesystems. | RECONCILED to declared-limit: FIX ROUND 37's own F9 LOW carry already names this exact asymmetry as accepted (the case-insensitive fix was applied to secret-pattern matching only, by explicit choice) - not carried as a separately open item. | 3883020373, 3885171823 | No. |
| C6 | Octal escape decoding (JLS 3.10.6, 1-3 digit `\0`-style escapes) is not implemented in the Java string-escape decoder. | Same family as every other accepted "spelling variant not yet parsed" residual elsewhere in this adapter; returns undecodable/absent, never a wrong decoded value. | 3885877714 | Yes - a real Java grammar handles the full escape set natively. |
| C7 | Directory entries are fully sorted before the per-entry cap check runs. | Pure cost ordering, no correctness effect. | 3885171806 | No. |
| C8 | `root_binding` is not threaded into projector.py's own report payload (though `get_status` already publishes it correctly elsewhere). | An omission, not a wrong value - the true value exists and is available via the other command. | 3878450543 | No. |

## Summary

- 7 pre-existing structural/provenance caveats (already declared, unaffected by this close-out).
- 3 pre-existing closed-vocabulary "unsupported shape" audit tables (~20 named shapes total).
- 31 distinct findings from this PR's own 223-comment cross-vendor sweep: 2 wrong-data-capable (both fixed, F6/F7), 21 safe-direction (registered above), 8 polish/declared-limit-adjacent (registered above).

Extraction-layer limits (adapter parsing, identifier/scope resolution, XML
decoding) are the ones the accepted migration plan (real XML parser → tree-
sitter Java → explicit binding stage → measured comparison) is expected to
retire, in whole or in part - marked above. Platform/discovery/publish-layer
limits (filesystem walking, locking, artifact envelopes, provenance) are a
separate architectural layer this migration does not touch; they remain
this producer's own standing, honestly-declared scope.
