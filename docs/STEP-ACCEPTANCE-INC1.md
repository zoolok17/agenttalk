# Acceptance increment 1 implementation record

**Audience:** maintainers and cold reviewers checking the staged implementation against [acceptance design v3](DESIGN-acceptance-pass.md). This reference records the shipped subset, reader assumptions, implementation decisions and executed evidence per commit.

## 1a — schema, project binding and retained evidence

Base: `d24e72f`. The first implementation commit copies the accepted design from `4ec0600`, so the design and feature land together without adding a fourth commit to the requested three-part sequence.

**Boundary:** this slice can produce artifact-derived HOLD findings, but never acceptance GO. `acceptance_trust_unresolved` remains until reproduction and final cold-review enforcement are implemented in 1b/1c. Ordinary closes without a frozen acceptance route or live acceptance requirement retain their behavior. No acceptance gate is created or changed.

### CLI and storage contract

The following are command signatures, not copy-paste examples; uppercase operands are placeholders for private workspace inputs.

```text
agenttalk close open --id ID --from ACTOR --scope milestone --revision SHA --acceptance-plan PLAN --project-repo PROJECT --non-lane-isolation-not-asserted
agenttalk close acceptance attach --id ID --file BUNDLE --from ACTOR
agenttalk close check --id ID --json
```

Opening resolves the commit in PROJECT, requires that checkout's HEAD to match, checks cleanliness, and records its root commit set and tree. There is no unverified full-SHA fallback. The existing lane requirement still applies; use the explicit non-lane declaration when not asserting lane delivery. PROJECT may differ from the bus repository. Signoff derivation on an acceptance close requires explicit `--changed-path` arguments in 1a, so it cannot accidentally derive project changes from the bus repository.

When PROJECT is also the bus root, ignore `.agenttalk/` in the project before opening an acceptance close. Runtime files otherwise make the checkout dirty. Refusals display up to twenty porcelain entries, with a count of omitted entries. `open --json` and `show` emit the private runtime record, including its checkout locator; their output is not a sanitized public report.

The plan's `registry_ref` is relative to the plan file. Bundle artifact paths are relative to the bundle file. Parent traversal, absolute paths, drive syntax, symlinks and Windows reparse points are rejected at those boundaries. Reads require regular files, so special streams cannot block an evidence read. Git runs with a timeout and without ambient Git redirection variables. No comparator or command supplied in an artifact is executed.

Plan, registry, bundle and raw results are retained by exact-byte SHA-256 under `.agenttalk/acceptance/sha256/<DIGEST>`. Imported files may subsequently be removed: evaluation reads the retained copies. Files are written to unique temporary names in the same directory, flushed and fsynced, then exclusively hard-linked to their digest names and verified before reference. A filesystem without hard-link support refuses retention. An existing digest path is never overwritten; mismatched bytes cause a refusal naming the path to quarantine before retrying. Normal failure cleanup removes the temporary name; process termination can leave an unreferenced temporary file. Retained bytes are not automatically published or pruned.

Open checks lens compatibility before creating the close, then persists `acceptance_route: {pending: true}` with close schema version 2 and freezes the route and generated lenses in `close_transaction`. Interruption before the second write leaves a HOLD-only close. The transaction rechecks project identity and lens compatibility. Attachment validates the bundle's instance/attempt/revision/policy bindings, copies required artifacts and commits the bundle hash, attribution and event under the same existing close lock and optimistic tokens. `--from` resolves the actor using the same advisory authority check as publish/reopen: an unrecognized actor is warned about but recorded, not denied. This is not authentication. A second attachment is refused; this slice also refuses acceptance `--force` and reopen until immutable successors exist.

### Concrete 1a schema

This is the deliberately bounded first slice of the design, not the entire final schema. Objects reject unknown/missing fields and duplicate keys/IDs; JSON is strict UTF-8 with finite numbers. Each file is limited to 1 MiB, lists to 256 entries, and imported bundle bytes to 16 MiB. Custom comparators, future trust profiles, carry, scope reductions and final-review records cannot silently pass as supported features.

| Object | Fields and invariants |
| --- | --- |
| Plan | `schema_version:1`, `plan_id`, `project_id`, `scope`, `authors:string[]`, `partitions`, `rows`, `registry_ref`, `registry_digest`, `trust_profile:cooperative`. Scope must match the close. At least one row and partition; every partition owns a row. |
| Partition | `id`, nonempty unique `agents:string[]`. Generates one required `acceptance-run-<id>` lens with the same actor allow-list, or reuses a matching explicit lens/allow assignment. Conflicting assignments and duplicate lens IDs are rejected. |
| Row definition | `id`, `partition`, `policy:gating/informational`, `comparator`, `expected`, `artifact`, `field`. Partition and artifact references must resolve. |
| Registry | `schema_version:1`, `entries`. An entry has `id`, `kind:checker/toolchain/service`, `version`, `sha256`. Empty entries are valid for built-in assertion fixtures; offline acquisition/preflight is a later increment. |
| Frozen route | `schema_version:1`, `attempt_id`, `instance_id`, `project_id`, `revision`, `project`, `plan_hash`, `registry_hash`, nullable `bundle_hash`, `frozen_by`, `frozen_at`, nullable `attached_by` and `attached_at`. Attribution must accompany a bundle. Long-lived binding uses instance/attempt, not changing generation. The enclosing close uses schema version 2. |
| Project identity | `locator` (private runtime path), verified `revision`, `tree`, sorted `roots`. The locator is recorded only in private runtime state; examples use placeholders. |
| Bundle | `schema_version:1`, `close_id`, `instance_id`, `attempt_id`, `project_id`, `revision`, `plan_hash`, `registry_hash`, `runs`, `rows`, `artifacts`. Every identity/hash must match the frozen route. |
| Run | `id`, `partition`, `actor`, `revision`, `head_before`, `head_after`, `status_before`, `status_after`. Actor belongs to partition; HEADs match the revision; statuses are empty. These are submitted facts, not authenticated execution provenance. |
| Row observation | `id`, `run_id`. Exactly one per planned row; referenced run belongs to its partition. |
| Artifact | `id`, `path`, `sha256`. Manifest IDs equal the planned artifact set; source bytes are checked before retention. |
| Raw result | `schema_version:1`, `run_id`, `revision`, `values:object`. Run/revision must match the consuming row; `field` selects a direct key of values, not an executable expression. |

Built-ins are `exit-code` (integer expected/observed values at `exit_code`, never booleans), `exact-value` (canonical JSON equality distinguishing booleans from numbers), and `exact-failure-set` (unique named IDs, order independent, both added and missing failures count). An observed failure list longer than the 256-entry expected-set limit fails the comparison rather than reporting invalid policy. Missing data is not pass. An unreadable, malformed or unbound row has `passed:null` and an error code/detail; gating rows add direct holds, while informational rows remain visibly unmeasured without blocking. A failed informational measurement remains visible without a gating row failure. Each row is evaluated independently; shared policy/bundle identity errors still hold the whole attempt. The retained bundle and raw bytes, not an ack's evidence pointer or claimed verdict, determine outcomes.

### Existing reader inventory

Inventory baseline: `d24e72f`. Searches covered all Python sources for close persistence, the named fields below and their projections. There were no existing semantic readers of the new `acceptance_route` or sidecar fields. The following enumerates existing readers whose inputs or interpretation change, plus whole-record readers that must preserve the extension. Function names are used rather than unstable line numbers.

| Field or object | Existing reader(s) | Assumption and disposition in 1a |
| --- | --- | --- |
| New `acceptance_route` | No field-specific reader at baseline. `close._is_wellformed`, `load_close`, `_write_close` and CLI `cmd_close show/check` handle whole records. | Preserve unknown extension fields. New acceptance validation handles strict route shape; mere presence, even malformed/pending, forces evaluation. |
| Frozen route through persistence | `close.create_close`, `_upgrade_legacy_locked`, `upgrade_legacy_close`, `CloseTransaction.__init__/commit`, `close_transaction`, `save_close`, `replace_close` | Existing generation/instance checks remain authoritative. The route is installed transactionally after a fail-closed pending marker. Replacement of an acceptance attempt is now refused. |
| `instance_id`, `generation` referenced by the route | `close.close_instance_id`, `close_generation`, `_validate_expected_tokens`, `_require_current_tokens` | Token types/meaning unchanged. Bundle identity never binds to an ack-dependent generation. A replaced instance cannot consume the old bundle. |
| `required_lenses` receives partition lens entries | `close._is_wellformed`, `compute_verdict`, `validate_lens_spec`; CLI `_close_lens_specs`, `cmd_close open` | Still a list of existing lens records with unique generated IDs and required actor assignments. Opening reports the extended lens count. |
| `required_lenses` through specialist routing | `close.apply_signoffs`; CLI `_signoff_set_for_lens`, `_cmd_close_signoffs` | Partition lenses have no `signoff_set_id`, so specialist re-derivation preserves them and specialist candidacy checks do not misclassify them. |
| `lens_acks` gets separate partition keys | `close.apply_ack`, `compute_verdict`, `_ack_authorized`, `_evaluate_signoffs`, `_signoff_signers` | One ack per lens remains unchanged; each runner owns a distinct slot. Specialist counting consumes only its generated IDs. Attempt-bound re-ack rules arrive with successors in 1b. |
| `revision` now refers to verified PROJECT for acceptance; `revision_kind` remains `sha` | CLI `_resolve_revision`, `cmd_close open/reopen/show`; `close._is_wellformed`, `compute_verdict`, `reopen` | Legacy resolution unchanged; acceptance open bypasses the bus-root fallback. Accepted revision remains a full 40-character SHA. Acceptance reopen is refused in 1a. |
| `revision_clean`, `dirty_artifact` | CLI `_worktree_clean`, `cmd_close open`; `close.compute_verdict`, `reopen` | Ordinary closes keep existing dirty overrides. Acceptance requires verified clean project HEAD, forbids dirty overrides, and rechecks the project during evaluation. |
| `revision` in ack/signoff evidence | `close.apply_ack`, `compute_verdict`, `_evaluate_signoffs`, `_signoff_signers`, `apply_signoffs` | Equality binding remains valid for the verified project SHA; no silent restamping or schema change. |
| `revision` used to derive affected paths | CLI `_signoff_risk_inventory`, `_close_derive_signoffs`, `_cmd_close_signoffs`; downstream `_changed_paths_of`, `_build_signoff_eval` | Prior default diff uses `store.root`. Acceptance requires explicit changed paths; downstream inventory/domain routing still consumes those recorded paths. |
| `revision` versus lane delivery | CLI `_close_worktree_eval`; `close.compute_verdict` | Existing lane artifact/head checks remain additive and store-based. Acceptance does not assert foreign-project lane isolation; explicit non-lane declaration is documented. |
| `revision` in existing DoD evidence | CLI `_resolve_dod_knowledge`; `close._evaluate_dod_assurance`, `_evaluate_dod_coverage` | Existing knowledge/gate evidence must match the same frozen project SHA. Existing evidence-source, scope and freshness checks are not loosened. |
| `revision` in barrier identity | CLI `_new_close_barrier_binding`, `_validated_close_barrier_binding`, `_ensure_close_release_barrier` | Treat SHA as opaque identity with close instance/generation. 1a cannot publish acceptance GO or create its GO barrier. |
| Published snapshot projection | CLI `cmd_close list/show`, `_published_close_holds`; `attention.close_hold_items` | Existing final verdict/reason/revision projection remains unchanged and does not independently validate acceptance. Only direct check/publish evaluate evidence. |
| DoD `scopes.<scope>.acceptance` and supported dimension set | `close.load_dod_policy`, `validate_dod_policy`, `derive_required_dod`; CLI `_build_dod_eval` | New spec accepts exactly `{required:true}`. Unknown/malformed policy still holds. Frozen route forces the dimension even after live policy deletion. |
| `dod_eval.required_dimensions`, new `dod_eval.acceptance` | `close.evaluate_dod`, `compute_verdict`; CLI `cmd_close check/publish` | Resolver loads the acceptance snapshot; pure fold adds holds. Direct pure callers omitting the snapshot cannot bypass a frozen route. Existing dimensions and gate holds remain additive. |
| Whole final close transitions | `close.set_draft`, `decide_counter`, `record_publish`; CLI `cmd_close draft/counter/publish` | They preserve new route fields and retain existing counter/remediation rules. Acks or green gate labels cannot remove direct acceptance holds. |

No other production module reads the new fields. `gates.py` neither consumes acceptance sidecars nor needs a schema change. The inventory includes projections because a HOLD-only staged implementation must not accidentally surface as a published GO elsewhere.

### Decisions beyond the design's field sketches

- Keep exactly three implementation commits: copy the v3 design into 1a rather than make a separate design-only commit.
- Fail closed throughout 1a with a permanent stage hold. Implementing partial evidence validation must not imply full production GO.
- Use a bounded initial schema and built-in structured raw-result envelope. Reject unsupported future fields/features; document their later introduction instead of silently accepting uninterpreted policy.
- Preserve exact JSON source bytes by digest; store runtime bodies in a private shared content-addressed area. Plan chooses its registry via a relative reference plus digest.
- Use pending-marker creation followed by a checked freeze transaction, rather than widening the general close creation API. Partial creation remains visible and non-GO; no automatic rollback deletes audit state.
- Bind attachment only once per attempt. Refuse force/reopen until successor history exists. Require explicit acceptance signoff changed paths until project-aware routing is implemented.
- SHA-1 Git commit IDs are the supported project object format in this slice, matching existing close validation. Other formats fail closed.

### Verification

Executed commands use `PYTHONPATH=<CLONE>/src` and an isolated `<SCRATCH>` outside the repository; placeholders below intentionally contain no machine-specific paths.

```text
python -m pytest tests/test_acceptance.py tests/test_close.py tests/test_close_signoffs.py tests/test_gates.py -q --basetemp <SCRATCH>/pytest-final -p no:cacheprovider
python -m ruff check --no-cache src/agenttalk/acceptance.py src/agenttalk/close.py src/agenttalk/cli.py tests/test_acceptance.py
git diff --check
```

Executed results (Python 3.14, source import):

- Initial acceptance-only run: **46 passed**.
- Combined command above: **389 passed, 1 skipped, 1 failed**. The failure was a new test fixture missing its signoff policy; without that policy the existing CLI correctly returned before deriving paths. All **335 existing close/signoff/gate cases passed**.
- After adding the missing fixture policy, rerunning `tests/test_acceptance.py` with a fresh scratch basetemp: **55 passed, 1 skipped**.
- Final explicit-lens compatibility change: `tests/test_acceptance.py -k 'explicit_partition or conflicting_partition or two_partitions or acks_without_bundle'`, fresh scratch basetemp: **4 passed, 54 deselected**, including two added cases and two neighboring regressions.
- Final regular-file guard: `tests/test_acceptance.py -k 'nonregular_input or copies_evidence_before_source_cleanup'`, fresh scratch basetemp: **2 passed, 57 deselected**, including one new case and the retained-evidence regression.
- Final distinct coverage across these runs: **58 acceptance cases passed, 1 skipped; 335 existing cases passed**. The sole skip is real symlink creation, which this host does not permit. No system settings were changed to enable it.
- Ruff, Python syntax parsing, diff whitespace checks, step-record links and qualified reader symbols: passed. Privacy sweep detects eight positive controls and no protected strings or machine-local paths in new/added content.

The tests use real isolated Git repositories, real close CLI/store transactions and retained files; write-failure/interruption tests inject errors at owned boundaries. Named 1b/1c tests in the design remain future criteria, not executed evidence. No full suite, network scan or release gate is claimed. Scratch fixtures are retained under the private `acceptance-inc1a` task scratch directory for the cold reader; they are not committed.

Residual risk: same-user cooperative operation, including mutable local state and submitted execution facts; no cryptographic provenance. Reproduction, operator scope reduction, immutable successors, final-review enforcement and complete GO/check-publish integration remain gated on 1b/1c. Path checks do not claim protection against a hostile same-user process racing filesystem replacements. Unreferenced retained blobs after interrupted writes are kept for inspection, not auto-pruned.

## 1a correction — cold review dispositions (2026-09-24)

Base: `b59afa8`. Scope is correction of 1a; no 1b/1c GO functionality is introduced. All tests below are in `tests/test_acceptance.py`; names omit the common `test_acceptance_` prefix.

| Finding | Disposition | Regression or evidence |
| --- | --- | --- |
| F1 mixed-version GO | FIXED: acceptance closes, including pending routes, use enclosing schema 2; ordinary closes remain schema 1. This engine rejects mismatched route/version pairs. | `old_engine_schema_guard_rejects_route`; extracted full pre-change wellformedness rule; reconstructed `probe_1a_c` against installed 0.91.0: check HOLD/3 and GO publish refused/2, record not published. |
| F2 partial digest publication | FIXED: same-directory temporary file, fsync, exclusive hard link, verification; wrong existing blob names its recovery path. | `truncated_existing_blob_names_recovery_path`, `interrupted_retention_never_installs_partial_digest`. |
| F3 unattributed attach | FIXED: `--from`, existing advisory authority helper, transactional `acceptance:attach` event and strict `attached_by/attached_at` fields. | `attach_attributes_event_and_advisory_authority`; existing attachment-failure and immutable-attachment tests still pass. |
| F4 M1 | FIXED coverage; production check already present. | `runner_dirty_after_run_rejected`; M1 killed. |
| F4 M3 | FIXED coverage of F2 verification. | `truncated_existing_blob_names_recovery_path`; M3 killed. |
| F4 M4 | FIXED coverage; production check already present. | `observed_boolean_is_not_exit_code`; M4 killed. |
| F4 M10 | FIXED coverage; production check already present. | `raw_other_run_same_partition_holds`; M10 killed. |
| F5 lost outcomes | FIXED: retain every row outcome; informational errors are unmeasured with details and do not conceal gating failures. | `malformed_informational_keeps_gating_outcome` (bad value, bad JSON, missing bytes, mismatched digest). |
| F6 burned id | FIXED: validate explicit/generated lens compatibility before persistence, repeat before freeze; stable acceptance error/3. | `incompatible_lens_does_not_burn_id`, `conflicting_partition_assignment_refused`. |
| F7 opaque dirty refusal | FIXED: bounded offending-path listing; document ignore requirement for shared project/bus root. | `single_repo_names_dirty_runtime_paths` refuses dirty runtime and succeeds after ignoring it. |
| F8 checkout lifetime | DEFERRED to 1b: distinguish retained historical identity from a live candidate during immutable-successor design. Current check still requires matching clean live HEAD; moving it can mask prior row results. No archival independence claim. | Existing `project_cleanliness_is_project_not_store`; future historical-attempt read test required with successors. |
| F9 publish lock duration | DEFERRED to 1c: changing lock placement now risks stale evidence at publication. Bound individual Git calls today; address snapshot/revalidation and measure lock contention with check/publish parity. | Future bounded-contention and changed-byte publish tests; no performance result claimed. |
| F10 observed failure overflow | FIXED: oversized observed failure list is a failed comparison, not an invalid policy. | `observed_failure_overflow_is_row_failure`. |
| NIT freeze time | FIXED: use freeze-phase time instead of opened_at. | `freeze_timestamp_is_actual_freeze`. |
| NIT storage path | DATED ERRATUM 2026-09-24: the shipped store path is `.agenttalk/acceptance/sha256/<DIGEST>`, superseding the design's `acceptance/evidence/sha256` spelling. Keep the accepted design copy byte-identical. | Retained-copy tests and exact design-blob comparison. |
| NIT authors/project_id | DEFERRED to 1b as directed: author independence and mapping a declared project ID to verified identity need successor/reproduction semantics. | Future 1b tests; present fields are declarations, not identity proofs. |
| NIT output/README | FIXED documentation: private locator appears in runtime JSON; command table links this contract. | CLI help and README/step link checks. |

### Additional and changed readers

The original seventeen reader groups above remain applicable. These additions cover the widened version and audit/output contracts:

| Field or surface | Existing readers and assumptions | Correction |
| --- | --- | --- |
| Enclosing `schema_version`; engines other than this commit | Tag-pinned 0.91.0 and original 1a `_is_wellformed` require version 1, then ignore unknown extension fields. Their `compute_verdict` and transaction loaders rely on that rule. | Version 2 closes reject as malformed, even without live DoD policy. Probe confirms check HOLD and publish refusal. No automatic migration of original 1a version-1 acceptance records; retain them as audit material and open a fresh ID with this engine. Ordinary version-1 closes remain supported. |
| Current schema consumer chain | `close._is_wellformed`, `compute_verdict`, `_write_close`, `create_close`, `replace_close`, `_upgrade_legacy_locked`, `CloseTransaction.__init__/commit`, `save_close`, `reopen`; CLI open/check/publish and record mutations depend on these validators. | Schema 2 requires the acceptance route key, including pending/malformed routes that then HOLD through acceptance evaluation. Removing the route does not convert schema 2 to an ordinary close. DoD policy and sidecar schema versions stay 1. |
| Route attribution and freeze time | Existing `acceptance._route`, `_policy`, `attach`, `resolve`, plus whole-record persistence and CLI show/open JSON. | Strict route shape adds nullable actor/time pair; nonempty pair required after attachment. Freeze time no longer copies open time. |
| Append-only `events` | `close._event` appends arbitrary event kind/fields; `empty_close` initializes; persistence preserves; CLI show prints the whole record. No production fold dispatches on close event kinds. | Add `acceptance:attach` with `by`, `at`, `bundle_hash`; event and route committed together. |
| Row outcomes | `acceptance.evaluate` consumes `policy` and truth of `passed`; CLI `_build_dod_eval` retains the resolver snapshot, check/publish fold it. | `passed:null` means unmeasured, with `error`; gating errors still HOLD. Informational errors cannot erase sibling outcomes. |
| Operator CLI and stdout | `cmd_close`/parser, `_resolve_self`, `_check_close_authority`, `_close_lead_set`; README section 5 and step signatures. | Attach actor resolves and warns exactly like publish/reopen. Runtime output may expose the private locator; no sanitized-public-output promise. |

### Executed correction evidence

- Failing-first targeted command: `python -m pytest tests/test_acceptance.py -q -k "old_engine or truncated_existing or interrupted_retention or attach_attributes or runner_dirty_after or observed_boolean or raw_other_run or malformed_informational or incompatible_lens or single_repo or observed_failure_overflow or freeze_timestamp" --basetemp <SCRATCH>/pytest-correction-red -p no:cacheprovider`: **12 failed, 3 passed, 59 deselected**. The three passing cases exercise previously implemented checks whose mutation coverage was missing.
- After corrections, `python -m pytest tests/test_acceptance.py tests/test_close.py tests/test_close_signoffs.py tests/test_gates.py -q --basetemp <SCRATCH>/pytest-correction-green -p no:cacheprovider`: **408 passed, 1 skipped**. Source `PYTHONPATH=<CLONE>/src`, foreground; the skip remains host-restricted symlink creation.
- Reviewer scripts were absent at the supplied location, including hidden-file inspection. Reconstructed probes from the full findings in private task scratch; did not claim execution of unavailable originals. The mixed-version probe executes the complete `_is_wellformed` function extracted from `d24e72f` and confirms the installed runtime module path before invoking it. Old check returns **3, HOLD/malformed_state**; old GO publish returns **2, malformed record**, with no publication. Initial probe assumed publish would also return 3; corrected that probe expectation and reran successfully.
- Four isolated source copies, one mutation/test each: M1, M3, M4, M10 each produced **1 failed** with its named test. Mutations remove post-run cleanliness, retained-byte comparison, strict observed-integer typing, and raw run binding respectively. Repository sources were never mutated for this probe.
- Targeted Ruff, CLI attach help, step links and diff whitespace checks passed. Privacy sweep detected eight positive controls and no protected strings or machine-local paths in added content; the drive-path matcher requires a letter boundary so a diagnostic `dirty:` followed by a newline escape is not misidentified as a drive. Scratch retained in the private `acceptance-inc1a` task directory for reread, including reconstructed probes, mutation logs and isolated stores. No original reviewer files were modified.

Residuals: authority remains advisory, same-user filesystem races remain outside the trust claim, and historical checkout/lock-duration issues are explicitly deferred above. No acceptance GO or release readiness is claimed.

## 1b — awaiting lead acknowledgment and cold verdict

Not started. Record its reader inventory, decisions and executed tests here after 1a is accepted.

## 1c — awaiting lead acknowledgment and cold verdict

Not started. Record final independence, publish parity and the complete integration fixture here after 1b is accepted.
