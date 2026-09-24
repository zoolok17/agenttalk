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

## 1b — cooperative evidence and immutable successors

Base: `11939ff`; authorized after the cold delta read accepted 1a. The sections above describe the 1a contracts and their correction history; this section defines the additional version-2 sidecar contract. The enclosing close stays schema 2. The accepted design remains byte-identical.

**Boundary:** all original and reproduced gating comparisons are recomputed from retained raw results. A complete cooperative fixture now clears `acceptance_trust_unresolved`, but still HOLDs on `acceptance_cold_missing`: final cold eligibility and vendor requirements remain 1c. No gate label, waiver, NA or override clears these direct holds. Schema-1 sidecars retain the earlier HOLD-only behavior; they do not silently acquire 1b semantics.

### Versioned inputs and transitions

Version-2 plans keep the same field set as version 1, require nonempty `authors`, and use a derived `project_id`: `git-` plus the first sixty hex characters of SHA-256 over Python's sorted-key JSON encoding of `{object_format:sha1, roots:<sorted verified root commits>}`. `acceptance.project_id(acceptance.verify_project(PROJECT, SHA))` computes it. Forks sharing that root set intentionally share identity; this is neither a remote-URL identity nor proof of authorship. SHA-1 is the supported Git object format. Verification binds the actual commit/tree/root set; private locator, tree and revision remain in the frozen route. Declared authors cannot be verifier or reproducer. Final cold author exclusions remain 1c.

| Record | Version-2 additions / rules |
| --- | --- |
| Route | `schema_version:2`; nullable `parent_record_hash` and `amendment_hash`, both absent as null for a first attempt or both digests for a successor. Existing instance/attempt/plan/registry bindings stay strict. |
| Bundle | `schema_version:2`, `verifier_access:{id,evidence}`, `reproductions:[]`. Evidence is an artifact ID in the retained manifest, not an external pointer. |
| Original run | Adds nonempty `access_id`. Existing original actor, partition, SHA and before/after checks remain. |
| Reproduction | `id`, `source_run`, `actor`, `access_id`, `access_evidence`, `revision`, `head_before`, `head_after`, `status_before`, `status_after`, `rows:[{id,artifact}]`. Exactly one reproduction per run serving a gating row, with every gating assertion from that run present and recomputed. Raw envelopes remain schema 1 and bind the reproduction's own run ID and project SHA. |
| Access / verifier | Attacher must currently be the configured lead, operator-facing liaison or reserved operator, and must differ from all original runners and authors. Reproducer differs from all original runners, authors and attacher. Verifier and reproduction access IDs differ from all runner access IDs and each other; retained access evidence must be nonempty. These are cooperative declarations with evidence, not authenticated access or execution provenance. CLI attach still records advisory-authority warnings; evaluation enforces the additional HOLD. |
| Ack | Adds `acceptance_binding:{instance_id,attempt_id,revision,plan_hash,registry_hash}` when written for a version-2 route. Every partition requires a current allowed actor's ACCEPT without override. Copying an old ack, even at the same SHA, remains stale. |
| Published `final` | Adds the freshly resolved `acceptance_snapshot` and complete `close_result`. Parent record retention preserves final HOLD, comparisons, acks, counters, remediation and evidence references. |
| Amendment | Schema 1; parent close/attempt, successor close ID, old/new plan and registry hashes, actor/time/reason, source-change or policy-change cause, `observed_before` parent-record digest, nullable reduction and retained operator approval digest. |

Command signatures (uppercase operands are placeholders):

```text
agenttalk close acceptance successor --parent OLD --id NEW --acceptance-plan PLAN --project-repo PROJECT --revision SHA --from ACTOR --reason REASON [--scope-reduction REDUCTION]
agenttalk close reopen --id OLD --successor NEW --acceptance-plan PLAN --project-repo PROJECT --revision SHA --from ACTOR --reason REASON [--scope-reduction REDUCTION]
```

Both call the same successor helper. The parent must already be published and use version-2 sidecars. Its complete record and amendment are retained before exclusive child creation. The original remains terminal and unchanged. Child starts with a new instance/attempt, no acks, no draft/final and no signoff overrides; counters, remediation, non-acceptance lens requirements and specialist routes remain. Unresolved parent counters add direct holds even if a child partition later accepts. Child freeze uses the same pending marker/transaction as open. A failure after creation may leave a HOLD-only pending child; no automatic rollback or history deletion. `--force` remains refused.

Historical version-2 check/HOLD publication verifies commit/tree/roots from the Git object database without requiring live HEAD or cleanliness. The checkout must still exist with those objects. Open, attach and GO publish require matching clean live HEAD. A new revision can therefore be evaluated while the old attempt retains its real failing comparison. F9's publish-lock contention/revalidation work stays in 1c; no timing improvement is claimed here.

### Operator scope reduction

Reduction file fields: `rows`, `reason`, nonempty `alternatives`, `impact`, `owner`, timezone-aware future `expires_at`, `cause:unavailable-tool/measured-variance`, nonempty retained evidence digest list and `decision_ref`. The reference resolves an actual message in the existing store from `Store.operator_identity()`, the reserved operator principal, never the lead or a free-text operator name. The message body must equal the structured object returned by `acceptance_history.approval_payload(parent, NEW, new_plan_hash, reduction)`: schema 1, predecessor attempt, reserved successor close ID, new plan hash, and the complete reduction except its self-referential message ID. The approval message bytes are retained and hashed. The exclusive successor ID plus predecessor attempt and exact policy hash bind the approval; this is cooperative origin checking, not cryptographic authentication.

Only gating-to-informational changes preserving the entire original assertion qualify. Missing, substituted or expired approval holds; missing/corrupt parent evidence holds. An approval's evidence references retain the measured variance/unavailability evidence; the operator assesses the stated cause and alternatives. No automated statistical inference is claimed. Original failure and raw evidence remain in the retained parent. Reports explicitly say **reduced scope**; affected outcomes have `disposition:scope-narrowed`, `passed:null`, the original outcome and separately recomputed `comparison_passed`. They are never promoted to a satisfied tool row. Later successors retain that label and recheck ancestral approvals, including expiry. Ancestry is bounded at 32 links and each retained JSON artifact remains subject to the 1 MiB limit; deeper/larger histories HOLD.

At the same SHA, changing an existing assertion's comparator, expected value, field or artifact cannot erase its history: `acceptance_plan_stale` persists through subsequent same-SHA successors. Typed canonical JSON comparisons distinguish booleans from numbers. A new source revision requires fresh runs, reproduced results and acknowledgments. There is no carry optimization. Plan/registry changes always create a fresh attempt, even at identical SHA.

### Existing readers of added or widened fields

The 1a inventories still apply. This commit traced the following existing reader chains; the history module itself has no pre-existing readers.

| Field / surface | Existing readers and assumptions | 1b disposition |
| --- | --- | --- |
| Plan/route/bundle schema versions | `acceptance._version`, `validate_plan`, `_route`, `_policy`, `_bundle`, `prepare`, `freeze`, `attach`, `resolve`; CLI open/attach/check/publish; whole-record persistence | Explicit 1/2 dispatch with exact fields per version; registry and raw-result schemas stay 1. Older engines reject the enclosing schema or unsupported route, as before. |
| `authors`, `project_id`, project locator/roots/tree/revision | `validate_plan`, `prepare`, `verify_project`, `_policy`, `_bundle`, `resolve`; CLI `_build_dod_eval` | Version 2 derives project identity and checks verifier/reproducer author exclusions. Historical read uses object identity; live operations retain clean-HEAD checks. |
| Run and artifact maps | `_bundle`, `attach`, `resolve`, `_compare`, retained-file readers | Manifest includes reproduced raw results and access evidence, under existing byte/count/path bounds. Raw run binding and strict integer comparisons apply to reproduced measurements. |
| `lens_acks` | `close.apply_ack`, `compute_verdict`, `_ack_authorized`, `_evaluate_signoffs`, `_signoff_signers`; CLI ack/show | Existing ordinary ack semantics remain; version-2 writes add attempt/policy binding, acceptance resolution requires fresh allowed ACCEPT without override. Specialist readers preserve/ignore the extension. |
| `final` extension | `close.record_publish`, `reopen`; CLI publish/list/show, `_published_close_holds`; `attention.close_hold_items` | Existing verdict/revision projections remain; extra snapshots are audit data consumed by successor validation. Acceptance reopen routes to a new ID, ordinary reopen unchanged. |
| Route through transitions | `load_close`, `_is_wellformed`, `create_close`, `CloseTransaction`, `_write_close`, `save_close`, `replace_close`; CLI open/reopen | Parent never rewritten by successor helper; exclusive child and checked freeze reuse existing transaction semantics. No force replacement. |
| `counters`, `remediation_items`, specialist routes | `compute_verdict`, `decide_counter`, `_evaluate_signoffs`, `apply_signoffs` | Copy obligations, clear acks/overrides; add direct unresolved-parent-counter holds to prevent clearing a counter by resetting its ack. |
| Resolver snapshot/outcomes | `acceptance.evaluate`; CLI `_build_dod_eval`, check/publish | `trust_checked` means version-2 resolution ran, not that trust passed. Missing rows retain their diagnostic without false failed-recomputation text. Scope reduction uses explicit disposition, original outcome and report label; final cold HOLD remains. |
| Operator message/config | `Store.operator_identity`, `messages_dir`, published message envelope readers | Read-only lookup by validated ID, reserved sender and exact body binding, retained approval bytes; no bus cursor or new decision lifecycle. Existing producers need the exact approval body, not a new message kind. |
| Blob publication | `_retain`, `_retained`, prepare/attach/freeze/resolver | Non-FileExists hard-link errors fall back to complete atomic replacement; visible corrupted blobs still name their quarantine path. This covers retained evidence only: existing store locks have their own filesystem requirements. `.pending-*` crash remnants and unreferenced digests are never auto-pruned. |
| CLI/documentation | close parser, `cmd_close`, `_check_close_authority`, README command table | Add successor and reopen flags; authority helper stays advisory, evaluator applies cooperative verifier restrictions. Private JSON snapshots remain private output, not sanitized public reports. |

### Carried findings and named tests

All names below are in `tests/test_acceptance.py` with prefix `test_acceptance_`.

| Finding / requirement | Disposition | Test |
| --- | --- | --- |
| Every comparison / reproduction | Implemented; full production GO still waits for 1c cold sweep. | `cooperative_go_requires_every_comparison_and_run_reproduction` |
| Separate actors/access | Implemented. | `reproduction_same_actor_or_shared_access_holds`, `verifier_must_not_be_runner_or_author`, `unrecognized_verifier_cannot_satisfy_trust` |
| Operator reduction | Implemented with original failure/report preservation, exact typed approval binding and ancestral expiry. | `scope_reduction_lead_only_or_expired_approval_holds`, `operator_scope_reduction_preserves_failure_and_reports_reduced_scope`, `unapproved_scope_reduction_remains_hold`, `scope_approval_expiry_survives_successor_chain`, `scope_approval_exact_binding_required` |
| Immutable attempts / direct holds | Implemented, including copied counters and same-SHA typed movement. | `successor_preserves_hold`, `same_sha_policy_change_stales_acks`, `same_sha_typed_expected_change_holds`, `successor_keeps_parent_counter_obligation`, `gate_label_and_waiver_cannot_clear`, `na_and_override_cannot_satisfy_partition` |
| F8 / C2 historical identity | Implemented. | `historical_attempt_reads_objects_after_checkout_moves`, `new_revision_successor_preserves_historical_failure`, `attach_requires_live_clean_candidate`, `go_publish_requires_live_candidate` |
| Authors / project ID | Implemented as cooperative declarations plus verified root identity. | `verifier_must_not_be_runner_or_author`, `reproduction_same_actor_or_shared_access_holds`, `project_id_cannot_name_unrelated_repository` |
| C1 verifier outside runners | Implemented; configured lead/operator plus actor/access checks. | `verifier_must_not_be_runner_or_author`, `unrecognized_verifier_cannot_satisfy_trust` |
| C3 repeated policy retention | Implemented explicit regression. | `successor_reretains_identical_policy`, `reopen_creates_linked_successor` |
| N1 hard-link fallback | Implemented for evidence retention; existing store lock requirements unchanged. | `hard_link_unavailable_falls_back_atomically` |
| N2 unmeasured double report | Fixed; unreadable evidence diagnostics identify digest, not an OS path. | `missing_gating_bytes_is_unmeasured_not_failed`, `failed_trust_keeps_gating_comparison` |
| N3 attribution branch coverage | Added regression and mutation check. | `attribution_without_bundle_holds` |
| N4 crash remnants | Documented by name; no unrequested cleanup policy. | Existing interrupted-retention regression; no crash-pruning claim. |
| F9 publish lock duration | Remains deferred to 1c as directed. | Not measured; check/publish parity work remains. |

### Executed 1b evidence

All pytest commands ran foreground with `PYTHONPATH=<CLONE>/src`, `PYTHONDONTWRITEBYTECODE=1`, isolated `<SCRATCH>/pytest-1b-*` basetemps and `-p no:cacheprovider`.

- Failing-first `tests/test_acceptance.py -k "hard_link_unavailable or missing_gating_bytes or attribution_without"`: **2 failed, 1 passed, 74 deselected**. N1/N2 reproduced; N3's existing guard passed before its mutation check.
- First acceptance run: **96 passed, 1 skipped, 3 failed**. Fixture defects: hard-link injection also affected the store lock, author absent from roster, and missing required gate evidence. Fixed fixtures; no product behavior was weakened. Second acceptance run: **104 passed, 1 skipped**.
- Combined bar: `python -m pytest tests/test_acceptance.py tests/test_close.py tests/test_close_signoffs.py tests/test_gates.py -q --basetemp <SCRATCH>/pytest-1b-final -p no:cacheprovider`: **442 passed, 1 skipped**.
- Final typed-value review added five cases and strict JSON comparison checks. `tests/test_acceptance.py -k "typed_expected or scope_approval_exact or operator_scope_reduction or scope_approval_expiry"`, fresh basetemp: **7 passed, 106 deselected**.
- Final CLI/route review added three cases and guards. `tests/test_acceptance.py -k "go_publish_requires or malformed_route_ack or reopen_flags_require or reopen_creates or same_sha_policy_change"`, fresh basetemp: **5 passed, 111 deselected**.
- Distinct final coverage across those runs: **115 acceptance passes, 1 skipped; 335 existing close/signoff/gate passes** (**450 passes** total). This is aggregate executed coverage; the final eight added cases were targeted runs, not part of the earlier combined command. The skip remains host-restricted symlink creation.
- M18, deleting the attribution-without-bundle guard in an isolated source copy: **1 failed**, killed by `attribution_without_bundle_holds`. Mutation log and source copy retained; repository source was never mutated.
- Targeted Ruff covers acceptance, acceptance_history, close, CLI and the acceptance test module. Successor/reopen CLI help, whitespace checks, documentation links and design-byte equality were checked. Privacy sweep uses positive controls before push.

Scratch is retained in the private `acceptance-inc1a` task directory for the cold reader, with `1b`-named fixtures and M18 probe/log. No reviewer workspace was modified. F9, final cold/vendor enforcement and complete check/publish integration remain for 1c; no broad suite or release GO is claimed.

## 1b correction — policy amendments across revisions

Base: `58a5abd`. This section supersedes the earlier 1b descriptions where noted.
Audience: implementers and cold reviewers checking the successor contract.

Every change to an existing gating assertion (including its partition, comparator,
expected value, field or artifact) now requires reserved-operator approval at any
source revision. An unapproved change adds `acceptance_category_moved_unreviewed`,
independently of the staged cold-review hold; descendants retain this hold. A new
SHA never makes changed policy into evidence that the original assertion passed.
Same-SHA unapproved changes also retain `acceptance_plan_stale`.

The existing `--scope-reduction FILE` input accepts `cause:policy-amendment` with
the existing reason, alternatives, impact, owner, expiry, retained evidence and
decision reference, plus `changes:{ROW_ID:{old:OLD_ROW,new:NEW_ROW}}`. Both rows
are complete plan row objects. `rows` must exactly equal the changed gating IDs.
The operator message remains `approval_payload(parent, NEW, plan_hash, input)`;
canonical typed JSON equality binds that exact diff, predecessor attempt,
successor ID and entire new plan. A mismatched, absent, expired or lead-origin
approval cannot clear the policy hold. Policy changes and gating-to-informational
reductions must use separate successor attempts; one approval cannot blur them.

New retained amendments use schema 2 and add the computed `assertion_changes` map;
evaluation recomputes and checks it. Existing schema-1 amendments are read with
the same cross-revision policy checks, so the older bypass stays HOLD. Outcomes
for changed assertions use `disposition:policy-amended`, `passed:null`, retained
`original_outcome`, and separate `comparison_passed`, with report label **policy
amended**. A failed amended gating comparison still HOLDs. Descendants preserve
the original outcome and amendment label; ancestral approvals are revalidated.

Authors must be a superset of the predecessor's declarations; each existing
partition must retain its prior allowed runner set. Removing either adds a direct
`acceptance_lens_not_independent` hold, inherited by descendants. This deliberately
offers no operator override for erasing independence exclusions. Partition ack
bindings add `bundle_hash`: pre-attachment accepts and prior-format bindings are
stale and must be refreshed after attach.

Open-attempt `close check` and GO publish resolve acceptance once with live
candidate verification. HOLD publication retains historical evaluation, so a
moved checkout cannot erase a failing terminal record. A terminal `close check`
labels itself `historical; not GO-publication eligibility` in text and the JSON
`acceptance_evaluation` field. The live label is `live candidate`. Existing
published records still cannot be republished without a successor. F9's lock
duration and final integration work remain 1c; no stronger filesystem race claim.

Parent snapshot summaries now contain only `close_id`, `record_hash` and
`verdict`; the full final remains reachable through the retained record digest.
Inherited scope summaries likewise store only that digest, avoiding recursive
snapshot embedding. Existing parent blobs remain readable without rewriting.
Ancestry still caps at 32 links and retained JSON at 1 MiB. Do not deepen a shallow
checkout or replace it during an attempt: root-set identity is frozen; changes
HOLD. A full-history checkout avoids the shallow-boundary ambiguity.

### Correction reader inventory

| Changed surface | Existing readers and assumptions |
| --- | --- |
| Amendment schema/diff and approval input | `successor`, history `evaluate`, `_reduction`, `_approval`, `approval_payload`; CLI successor/reopen pass the same file. Exact fields dispatch by amendment version; typed diff comparison; operator producers bind the full new input. Older 1b history readers reject schema 2; 1a/0.91 engines retain their earlier rejection guards. |
| Bundle-bound ack | `close.apply_ack` writes through `acceptance.ack_binding`; `_ack_bindings` checks equality. `compute_verdict`, `_ack_authorized`, `_evaluate_signoffs`, `_signoff_signers`, show/list preserve or ignore the extension. No ordinary-close ack change. |
| Author/runner sets | Plan validation, partition-lens generation, `_bundle`, `_reproduce`, successor and history evaluation. Existing per-attempt checks remain; history adds monotonic exclusions. |
| Parent and inherited snapshot summaries | History `evaluate` was the only reader of nested snapshots; it reads full retained parent records. CLI publish stores the resolver result; show emits it. `record_publish`, `_published_close_holds`, attention and barrier validation read outer final fields, not nested parent summaries. |
| Outcome disposition and null pass | Acceptance `evaluate` holds only on explicit false and consumes direct history holds. History carries labels/original outcomes; CLI final storage/show preserve them. No consumer may interpret `comparison_passed` as the original row's pass. |
| Live resolver option / check label | `_build_dod_eval`, CLI check/publish, acceptance `resolve`. Default helper callers retain historical semantics; open check and GO publish select live once. Existing `compute_verdict` and ordinary-close output retain their contracts; extra label applies only to acceptance check output. |

### Finding dispositions

Test names below are in `tests/test_acceptance.py`, prefixed `test_acceptance_`.

| Finding | Disposition | Test / evidence |
| --- | --- | --- |
| M1 policy bypass across SHAs | FIXED; exact operator amendment binding, original failure preserved, direct hold independent of cold. | `gating_amendment_requires_operator_at_any_revision`, `exact_operator_policy_amendment_preserves_failure`, existing same-SHA/history tests |
| M2 shrinking independence lists | FIXED; monotonic authors and runner sets, inherited direct hold. | `successor_cannot_shrink_independence_lists` |
| M3 pre-attach accept | FIXED; bundle digest in ack binding. | `pre_attachment_accepts_are_stale` |
| M4 reproducer self-attestation | DEFER to 1c's independence lenses; add an attempt/bundle-bound reproducer acknowledgment before removing the cold hold. Current cooperative declarations remain insufficient for production GO. | No self-attestation claim in this correction. |
| M5 successor forks | DEFER to 1c; choose fork discovery/reporting or an exclusive successor reservation with crash recovery alongside final close integration. Current API permits siblings; no single-successor guarantee. | Reviewer probe accepted as limitation; no changed behavior. |
| M6 Mb/Mc/Md/Mn | ADDED missing regression tests. | `duplicate_reproduction_holds`, `reproduction_dirty_after_holds`, `allowed_runner_override_holds`, `reproduction_cannot_reuse_original_run_id` |
| M6 Me/Mg/Mi | ADDED depth, embedded approval identity and resolution identity tests. | `ancestry_depth_cap_holds`, `operator_message_embedded_id_must_match`, `resolver_rechecks_derived_project_id` |
| M7 inherited reduction approval | DEFER renewal semantics to 1c before production GO: current descendants inherit the approved narrowed scope with original label and ancestral expiry. Decide renewal per revision/attempt with the lead; this correction does not claim per-attempt renewal. | Existing `scope_approval_expiry_survives_successor_chain`; staged cold hold remains. |
| N1 recursive final growth | FIXED with digest summaries; full final retained separately in parent record. | `parent_snapshot_is_digest_reference`, history tests resolve the digest |
| N2 check/publish mismatch | FIXED for open candidates; terminal history explicitly labeled; one resolve per invocation. | `live_check_matches_go_publish`, `historical_attempt_reads_objects_after_checkout_moves` |
| N3 shallow history | DOCUMENTED limitation; do not deepen/replace checkout during attempt. | Existing identity mismatch holds; no shallow-clone support expansion claimed. |

### Correction evidence

All pytest runs used foreground execution, `PYTHONPATH=<CLONE>/src`,
`PYTHONDONTWRITEBYTECODE=1`, isolated task scratch and `-p no:cacheprovider`.

- Failing-first `python -m pytest tests/test_acceptance.py -q -k gating_amendment_requires_operator --basetemp <SCRATCH>/pytest-1b-correction-red -p no:cacheprovider`: **2 failed, 116 deselected**, specifically because `acceptance_category_moved_unreviewed` was absent at both unchanged and new source SHA.
- First acceptance run: **132 passed, 1 skipped, 2 failed**. Fixed an aliased tamper fixture; corrected the override probe to use `close.apply_ack(override=True)` for an allowed runner, since the CLI ignores that flag from non-leads. No authority check was relaxed.
- Focused follow-up with `-k "exact_operator_policy or allowed_runner_override or successor_preserves_hold or gating_amendment or scope_approval_expiry"`: **10 passed, 125 deselected**.
- Final bar: `python -m pytest tests/test_acceptance.py tests/test_close.py tests/test_close_signoffs.py tests/test_gates.py -q --basetemp <SCRATCH>/pytest-1b-correction-final -p no:cacheprovider`: **469 passed, 1 skipped** in 199.60 seconds. The skip remains host-restricted symlink creation.
- Isolated source copies, one named test per mutation: **Mb, Mc, Md, Mn, Me, Mg, Mi all KILLED**, each with one failing test. Repository source was not mutated. Logs and copies retained under private task scratch, `correction-mutants-88f96d7e`.
- Targeted Ruff on the three changed production files and acceptance tests passed. Successor CLI help, whitespace, the seven-file scope and byte-identical accepted design were checked. Privacy sweep detected **8/8 positive controls, zero matches** in added public content.

Scratch is retained in the private `acceptance-inc1a` task directory: correction
fixtures, failing-first stores and seven mutant logs/copies support the delta
read. No reviewer workspace was modified. No broad-suite or production-GO claim;
the final cold hold, M4/M5/M7 decisions and F9 remain for 1c.

## 1b correction 2 — gating history across informational attempts

Base: `fcaff36`; A1 is fixed before 1c. Audience: implementers and delta reviewers.
For every row gating in the new plan, successor creation and evaluation now find
the most recent gating definition in retained ancestry. Informational definitions
never replace that baseline, even if they change the assertion or span multiple
attempts. The existing 32-link ancestry bound and retained-byte verification apply
to this lookup. No live parent edit or new mutable index is introduced.

The exact operator-approved diff uses that gating definition as `old`, and the
new row as `new`. Missing approval still produces the direct
`acceptance_category_moved_unreviewed` hold independently of cold review. The
original failed outcome is retained through informational attempts. Restoring the
identical gating assertion needs no policy amendment; a row that has never gated
can enter gating without this amendment requirement. A change to a prior gating
assertion needs approval even if it tightens policy or reverts an approved
weakening. Same-SHA checks no longer mistake informational-only evolution for a
gating assertion change; re-entering gating still checks retained history.

Amendment schema and approval fields remain unchanged. Previously written diffs
that omit a re-gating change fail recomputation with the same direct amendment
hold. This correction does not lift the cold hold or relax approval expiry.
Policy-amendment approvals continue to expire and re-hold descendants when
rechecked; that conservative contract is retained explicitly. Permanent policy
approval semantics would require a lead decision and tests before changing it.

### Reader inventory and dispositions

| Surface / finding | Disposition and existing reader assumptions | Test / evidence |
| --- | --- | --- |
| A1 / computed `assertion_changes` | FIXED. `successor` writes and history `evaluate` recomputes the same ancestor-aware map. `assertion_changes` accepts an optional gating map; existing direct callers default to the immediate plan. Operator payload producers must supply the most recent gating row. CLI successor/reopen still pass the same file; `_approval` binds the entire exact diff. Older engines either compute a different diff and HOLD, or retain their unconditional cold hold. | `regating_compares_last_gating_ancestor`: direct and extra informational hop; changed unapproved HOLD, identical restoration, exact ancestor-bound approval. |
| Never-gating row / same-SHA movement | FIXED distinction. History evaluation limits the same-SHA assertion guard to prior gating rows; `acceptance.evaluate` and CLI still consume direct holds. | `never_gating_row_can_enter_without_amendment` |
| A2 Nl / Nk | Added regressions for failed approved amended rows and unchanged descendants. These holds must remain because history marks outcomes `passed:null`. | `exact_operator_policy_amendment_preserves_failure[operator-child-failure]` and `[operator-descendant-failure]` |
| A2 Nh | Added typed-body approval cases; `true` and `1.0` cannot substitute for integer `1` despite Python equality. | Same test, `[operator-boolean-body]` and `[operator-float-body]` |
| A2 Ni / Nb | Added retained-diff corruption and mismatched approved row-set cases. | Same test, `[operator-retained-diff]` and `[operator-rows]` |
| A2 Nc redundant combined-reduction guard | Retained; isolated mutation coverage deferred. The independent scope-reduction assertion-preservation check also rejects this combination. No GO or unique protection attributed to this redundant branch. | Existing reduction preservation tests; no mutation kill claimed for Nc. |
| Approval cause diagnostic | FIXED to name all three allowed causes, including policy-amendment. Strict shape errors still use the object validator. | Targeted lint and source inspection. |
| Tightening / reversion friction | DOCUMENTED: any changed gating assertion needs approval, in either direction. | Existing same-SHA and changed-assertion tests. |
| Policy amendment expiry | RETAIN conservative current behavior; permanent approval is a product decision. | Existing exact-operator amendment expiry check and ancestral approval validation. |

`latest_gating_rows` reads existing route parent hashes and retained plan rows;
it creates no record fields. `_policy` validates those reads. Existing final,
attention, barrier, ack and CLI output readers are unchanged. Original-outcome
projection unwraps an informational predecessor's preserved outcome; consumers
remain history `evaluate`, pure acceptance `evaluate`, and CLI final storage/show.

### Correction 2 evidence

Failing-first A1 run: **2 failed, 2 passed, 135 deselected**. Both changed re-gating
paths lacked `acceptance_category_moved_unreviewed`; both identical restorations
passed. Focused post-fix run: **13 passed, 130 deselected**.

All commands used foreground execution, `PYTHONPATH=<CLONE>/src`,
`PYTHONDONTWRITEBYTECODE=1` and isolated task scratch.

- Red: `python -m pytest tests/test_acceptance.py -q -k regating_compares_last --basetemp <SCRATCH>/pytest-1b-round2-red -p no:cacheprovider` (counts above).
- Focus: same module with `-k "regating_compares_last or exact_operator_policy"`, fresh `pytest-1b-round2-focus` basetemp (counts above).
- Final: `python -m pytest tests/test_acceptance.py tests/test_close.py tests/test_close_signoffs.py tests/test_gates.py -q --basetemp <SCRATCH>/pytest-1b-round2-final -p no:cacheprovider`: **482 passed, 1 skipped** in 249.01 seconds. The skip remains host-restricted symlink creation.
- Isolated source copies: A1 (discard ancestry at both diff call sites), Nl, Nk, Nh, Ni and Nb each produced **one failing named regression**, all six **KILLED**. Nc is explicitly excluded from this claim. Copies and logs are retained under `round2-mutants-918d5452`; repository source was not mutated.
- Targeted Ruff on `acceptance_history.py` and `test_acceptance.py`, whitespace, four-file scope and unchanged accepted design checks passed. Privacy sweep detected **8/8 positive controls, zero added-content matches** before push.

Scratch is retained in the private `acceptance-inc1a` task directory: `round2`
fixtures, red/green stores, mutation sources/logs and the verification scripts for
delta review. No reviewer workspace or unrelated untracked files were changed.

M4 reproducer acknowledgment, M5 fork visibility/reservation, M7 scope-renewal
semantics and F9 publish lock duration remain mandatory 1c exit conditions before
the unconditional cold hold can be lifted. No 1c implementation is included here.

## 1b correction 3 — protected-target coverage (pre-implementation plan)

Base: `6878381`. The following enumeration was recorded before changing code.
Audience: implementers and cold reviewers. It supersedes row-ID policy identity.

A protected target is `(partition, artifact, field)`. Comparator scope is the
predicate over that field, not part of target identity: changing a comparator
must not invent a fresh target. Row IDs are labels. All distinct ancestral gating
predicates contribute obligations for the target; their conjunction is the
strongest retained coverage. Incomparable predicates are retained, never ordered
by numeric expected value. A candidate covers a target when its gating conjunction
provably implies every ancestral predicate. Matching predicates are equivalent;
adding constraints strengthens coverage. Typed exact-value and integer exit-code
equality are equivalent for the same integer. Exact JSON string-list equality
implies exact-failure-set equality for the same set; the reverse is weaker.
Other comparator/value changes are incomparable and require approval.

| Reshape | Expected outcome without a new operator approval |
| --- | --- |
| Rename / new ID, same target and predicate | No amendment hold. |
| Rename / new ID, different equality value | `acceptance_category_moved_unreviewed` HOLD. |
| Split into duplicate equivalent predicates on the same target | No amendment hold. |
| Split by retaining the old predicate and adding a constraint | No amendment hold; measured contradictions still fail. |
| Split by replacing a target with different targets | HOLD for lost original coverage. |
| Merge equivalent predicates on one target | No amendment hold. |
| Merge distinct targets, or drop a distinct conjunct | HOLD for uncovered obligation. |
| Move partition, artifact or field | HOLD for lost original target, even if the new target passes. |
| Comparator change with proven equivalence | No amendment hold. |
| Comparator change to a provably narrower predicate | No amendment hold. |
| Comparator change to a wider/incomparable predicate | HOLD. |
| Informational hop, then rename and weaken | HOLD against ancestral target coverage. |
| Informational hop, then restore identical coverage | No amendment hold. |
| Delete target / leave only informational coverage | HOLD; exact operator approval may authorize the omission. |
| Introduce a genuinely new target | No amendment hold; normal execution and independence checks still apply. |

Approval binds the exact target obligations and new gating coverage, predecessor,
successor and plan hash using the existing reserved-operator message mechanism.
Absent coverage remains visible as a target-level audit entry with original
outcomes sourced from the ancestors that carried its gating predicates. Depth
checks must reject an over-limit successor before creating its close ID and must
raise explicitly on walk exhaustion. Table-driven tests will pin the amendment
code for coverage loss and permit identical/strengthened coverage.

### Implemented contract and decisions

`acceptance_coverage` owns the common target grouping, conservative implication,
retained-history walk and coverage diff used by successor creation and evaluation.
There is no row-ID fallback. The walk retains the conjunction of all historical
gating predicates, removing only predicates implied by another retained predicate.
The proof deliberately recognizes only equality and the exact-list to failure-set
implication described above; the list must satisfy the failure-set comparator's
size, string and uniqueness constraints. A numerically larger equality value is
not stronger. Unproven implications require approval. Contradictory predicates
remain obligations; the ordinary artifact comparisons prevent them producing GO.

Amendment schema **3** retains the field name `assertion_changes`, but its keys are
`target-<SHA256>` identifiers derived from canonical `(partition, artifact, field)`
objects. Each value contains `target`, `old` and `new` predicate lists; an empty
`new` list means removal of gating coverage. Approval `rows` now names exactly
these target identifiers, and `changes` must equal that complete diff. Both
scope reductions and policy amendments supply `changes`. Existing operator
origin, typed payload, predecessor/successor/plan binding, evidence and expiry
checks remain mandatory. A tool/variance reduction authorizes only missing
coverage; replacement predicates require cause `policy-amendment`.

This supersedes correction 2's tightening/reversion friction: equivalent or
provably stronger coverage, including restoration of the strongest historical
predicate, needs no new approval. Conversely, approval does not erase historical
obligations. **Each successor that still weakens coverage needs its own exact
approval**, even if its parent had approval. This is the conservative renewal
choice implied by comparing every candidate to strongest retained coverage;
it replaces inherited approval reuse. Expired approvals still re-hold ancestry.
M7's product decision remains visible for the lead's 1c review; changing this
renewal rule requires an explicit alternative with tests before GO is enabled.

Snapshots add `coverage_changes`, including missing targets with no current row,
approval status, report label and ancestor source projections. Each source names
its close/attempt/plan/row and original comparison outcome; full evidence remains
in the retained close. No nested final snapshot is copied. Current rows are
matched by target, so renamed rows cannot claim that the original assertion
passed. Failed amended comparisons still contribute a direct row hold before
their `passed` value becomes null. Original failure selection searches the
retained gating ancestors rather than indexing the immediate parent's rows.

History supports at most 32 parent links. Successor creation reserves one link
for the child and rejects overflow before close creation. Exhaustion raises
explicitly, with no implicit `None` or immediate-parent fallback.

Previously written schema-1/2 amendments are read and their coverage recomputed;
old row-based approvals do not authorize the new target diff and therefore HOLD.
No automatic migration or re-signing is performed. Older branch engines reject
schema-3 amendments; installed ordinary-close engines retain the schema-2 close
guard. The unconditional cold hold is unchanged in this correction.

### Reader inventory and finding dispositions

Earlier sections are chronological records; this section supersedes their
row-ID comparison, row-based approval and inherited-renewal contracts.

| Changed surface / finding | Readers and disposition | Regression |
| --- | --- | --- |
| B1: protected target and predicate comparison | `successor` and history `evaluate` now share `coverage.history/changes`; removed `latest_gating_rows/assertion_changes` helpers have no remaining code callers. FIXED structurally, including removal and comparator moves. | `target_coverage_reshape_table`, 19 cases matching the enumeration above. |
| Strongest historical coverage | Retained route/plan readers `_policy`, `_retained`, `decode` feed the walk. Approval never replaces older obligations. | `strongest_target_coverage_survives_approved_weakening`; `duplicate_list_is_not_stronger_failure_set_coverage`. |
| B2 and depth off-by-one | `successor` refuses over-limit children before `create_close`; recursive history `evaluate` and coverage walk use the same 32-link bound. FIXED now. | `target_history_depth_is_bounded_before_child_creation`. |
| Missing immediate-parent original outcome | History evaluation reads ancestor source projections and publishes target-level audit entries, including deleted targets. FIXED now. | `approved_target_deletion_and_renamed_return_keep_ancestor_failure`. |
| Amendment schema 3 / approval `rows` and `changes` | `_reduction`, `_approval`, `approval_payload`, `successor`, history `evaluate`; CLI successor and reopen forward the file unchanged. Test approval producers now emit exact target diffs. Prior row approvals fail closed; other engines reject schema 3. | Existing exact-operator tamper/expiry/origin tests, target deletion/re-entry and table cases. |
| `final.acceptance_snapshot.coverage_changes` and outcome annotations | `acceptance.resolve/evaluate`, CLI check/publish/show/list and `record_publish` carry the snapshot. `attention._published_close_holds`, barrier readers and ordinary close consumers use outer verdict/revision fields; no change to those fields or callers. History reads compact ancestor outcome fields only. | Existing close/signoff/gate regressions plus target audit regression. Attention suites were not rerun in this round. |
| A2 redundant Nc guard | Old row-specific combined-reduction branch removed in the structural rewrite. No standalone mutation claim for the retired branch. | Unified target approval shape and scope-only-removal check replace it. |
| Renewal and expiry | Conservative per-attempt target approval, as described above; no expiry relaxation. M4 reproducer attestation, M5 fork handling and F9 publish lock remain 1c exit criteria. | Informational-hop fixtures renew approval; unchanged weakened descendant without renewal HOLDs. |

No CLI flag, route, ack-binding or accepted design-note byte changed. The README
links this staged contract; CHANGELOG describes target-based amendments.

### Correction 3 evidence

Foreground commands used `PYTHONPATH=<CLONE>/src`, `PYTHONDONTWRITEBYTECODE=1`
and isolated task scratch, with `-p no:cacheprovider` throughout.

- Required red: `python -m pytest tests/test_acceptance.py -q -k target_coverage_reshape_table --basetemp <SCRATCH>/pytest-1b-round3-red -p no:cacheprovider`: **10 failed, 9 passed, 148 deselected**. This was before production changes.
- Initial focused post-fix run: table plus policy-amendment, re-gating and expiry selections: **35 passed, 2 failed, 130 deselected**. The 19-case table passed; two informational-hop fixtures needed explicit per-attempt renewal and were corrected without relaxing the coverage rule.
- Final bar: `python -m pytest tests/test_acceptance.py tests/test_close.py tests/test_close_signoffs.py tests/test_gates.py -q --basetemp <SCRATCH>/pytest-1b-round3-final -p no:cacheprovider`: **505 passed, 1 skipped** in 295.60 seconds. The skip remains host-restricted symlink creation.
- The depth test was subsequently reordered to check explicit overflow refusal before CLI creation. Its focused rerun: **1 passed, 170 deselected**. Production code did not change after the full bar.
- Six isolated source mutations were caught: discard ancestry, ignore missing targets, accept arbitrary implications, return `None` on depth exhaustion, discard stronger ancestral predicates and accept duplicate-list implication. Five initially failed their named assertion; depth initially failed through an unexpected exception. After the test reorder, the depth mutant fails specifically with **DID NOT RAISE AcceptanceError** (**1 failed, 170 deselected**). No invalid collection or setup run is counted as a kill.
- Targeted Ruff on `acceptance_coverage.py`, `acceptance_history.py` and `test_acceptance.py` passed. Whitespace, five-file scope, branch and unchanged accepted design checks passed. The privacy sweep detected **8/8 positive controls and zero added-content matches**, including the new coverage module.

Scratch is retained in the private task directory for delta review: red/green
stores, `round3-mutants-6dc66cb3` source copies/logs and verification helpers.
No reviewer workspace or unrelated untracked files were changed.

## 1c — awaiting lead acknowledgment and cold verdict

Not started. Record final independence, publish parity and the complete integration fixture here after 1b is accepted.
