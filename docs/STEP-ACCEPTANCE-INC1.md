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

## 1c — final cold review and supported cooperative GO

Base: `93184c6`, accepted for 1c by the lead. This section supersedes earlier
HOLD-only staging statements for **schema 3**; schemas 1 and 2 still cannot GO.
The accepted design copy remains byte-identical. The user workflow and complete
field reference are in [ACCEPTANCE.md](ACCEPTANCE.md).

### Decisions and bounded implementation

Plans, routes and bundles use version 3 for the final-review workflow. The plan
adds `cold_policy` (reviewer, available-vendor roster snapshot, absence disclosure).
The route adds immutable `cold_commit_hash` and `cold_reconcile_hash`. The ordinary
close record remains schema 2, so old ordinary-close engines reject it. Earlier
acceptance engines reject route version 3; no migration can silently grant GO.

The reviewer alone can submit `close acceptance cold --phase commit` before
attachment, then `--phase reconcile` after attachment. Initial reports bind
instance/attempt/revision/project/policy hashes, reviewer/context, authorship and
exposure declarations, leakage review, separate-access evidence, delivered-resource
hashes, observations and blind spots. Reports and delivered bytes are copied into
retention and rehashed on evaluation. Delivery permits source, safety, toolchain
and access evidence; the plan, expected values and author claims remain withheld.
The delivery projection and actual access are cooperative attestations, not
cryptographic proof that a particular person read a complete checkout. This
increment does not implement automatic expectation-token scanning or whole-repo
packaging; the reviewer explicitly attests leakage review and discloses limitations.

Reconciliation binds both immutable report and bundle hashes, records reveal,
and dispositions every initial observation. Commit, attach and reconcile events
must occur once in that order with matching actor/hash attribution. A blocking
observation left open contributes `acceptance_residual_open`. The final reviewer
must differ from every declared author, allowed runner, verifier and reproducer,
with a different declared access identity. A known unblinded ancestor reviewer
cannot be made fresh by changing its context string; delta work is permitted but
does not satisfy the final sweep. A fresh actor is needed on the final successor.

The frozen available-vendor roster must include all participants. When another
vendor is available, reproducer vendor differs from its original runner and the
cold reviewer's vendor differs from all authors/actual runners. This is conservative
for mixed-vendor author teams, which may require another vendor. With one vendor,
absence disclosure is required and retained; actor/access separation still applies.
Availability is declared by the assignment, not inferred from runtime package names.

M4 is closed by required `acceptance-repro-<reproduction-id>` lenses installed
transactionally during attachment. Every declared reproducer attests its own
evidence. `acceptance-cold` is frozen from the plan. All schema-3 accepts bind both
cold hashes as well as attempt, revision, policy and bundle; pre-reconciliation
accepts are stale. Another actor, `na` or `--override` cannot substitute.

M5 uses the permitted **audit-view** option: `close show` adds
`acceptance_successors`, listing every fully linked child (open or published),
its status and verdict. The parent is never rewritten. A pending failed creation
has no frozen parent link and cannot GO; sibling enumeration is not an exclusive
reservation and does not prohibit alternatives. Unreadable linked records fail
the audit rather than silently hiding an alternative.

M7/N4 are **DECIDED by the lead**: retain exact per-attempt re-approval. An
unapproved coverage loss permanently holds all descendants, even after restoration.
**Recovery is a new root close**, with complete evidence and an eligible fresh
reviewer. Prior attempts remain in the store; no restoring-successor exception
was added. The user guide and README state this rule.

F9 disposition: retain the existing per-close publication critical section.
GO performs one live acceptance resolution and persists exactly that snapshot;
it does not reuse a prior check or run a second resolver. Moving Git/evidence
reads outside that lock would enlarge the stale-record window. Git subprocesses
retain their existing individual 10-second timeout; a slow checkout can still
hold the lock longer than another writer's default lock timeout. That unmeasured
latency optimization is deferred to the acquisition/performance work, not presented
as fixed. The existing cross-file evidence-writer race documented in CLI issue
comments also remains: the close lock is not a filesystem-wide evidence mutex.
Cooperative immutable retention and immediate revalidation are the bounded claim.

### Reader inventory

| Changed item | Every existing reader/producer class and its assumption |
| --- | --- |
| Plan schema 3 / `cold_policy` | `validate_plan`, `prepare`, `_policy`, `partition_lenses`, `freeze`; history successor/evaluation and coverage history read the validated policy. CLI open/signoff lens derivation must agree with frozen allowed actors. The new cold validator requires exact keys and bounded lists. |
| Route schema 3 / cold hashes | `_route`, `_policy`, `_bundle`, attach, resolve, history successor/evaluation, coverage ancestry, `ack_binding`; close `_is_wellformed` still requires close schema 2 for any acceptance route. CLI open/attach/check/publish/reopen/show carry it. Force/reopen remain refused; successors reset both phases and remove inherited dynamic reviewer/reproducer lenses. |
| Bundle version 3 / dynamic reproducer lenses | `_bundle`, attach, `_reproduce`, history retained-bundle checks. Bundle fields otherwise match version 2. `apply_ack`, `_ack_authorized`, `_evaluate_signoffs`, `_signoff_signers`, `compute_verdict` see ordinary required lenses. Signoff-derived sets do not own these lenses. |
| Extended `lens_acks[*].acceptance_binding` | `close.apply_ack` writes version 2 or 3 bindings; acceptance partition and cold/reproduction validators compare exactly. Ordinary ack/signoff/verdict/show readers ignore extra binding keys. Earlier schemas keep their old binding keys. |
| Cold retained report schemas and events | New `submit/validate_initial/validate_reconciliation/evaluate` produce/read reports; `_retain/_retained` enforce bytes. Existing `_event`, close transactions and show serialize events; ordinary verdict logic does not infer evidence from event labels. |
| `cold_checked`, `cold`, direct holds in resolved snapshot | `acceptance.evaluate` alone lifts the staged cold hold when verified; `close.evaluate_dod/compute_verdict` remain additive. `_build_dod_eval` has check/publish callers only. `record_publish` stores this same snapshot; show/list and history can read it. Attention `_published_close_holds` and barrier consumers still read outer final verdict/revision fields. |
| `acceptance_successors` in show output | CLI show adds a read-only projection via history `successors`; `load_close/list_close_ids` plus retained parent identity enumerate children. It is not persisted and is not a verdict input. Ordinary show output is unchanged. |
| Unknown comparator rejection | Coverage `predicate`, called by `group/history/changes`; both creation and evaluation fail closed. Future comparator additions must provide semantics and update tests. Future registry command/provenance references must also enter target identity; not implemented by this built-in-only slice. |
| Other engines / documentation | Older route validators reject version 3; schemas 1/2 retain cold HOLD. CLI help, README command table and the new guide describe cold phases. No accepted design bytes or ordinary gate/DoD policy fields changed. |

### Requirement-to-test mapping

Names below omit the `test_acceptance_` prefix.

| Requirement / review finding | Test |
| --- | --- |
| N1 reduction cannot approve replacement; added FIRST | `reduction_cannot_authorize_replacement_coverage[unavailable-tool/measured-variance]` |
| N3 explicit comparator support | `coverage_comparators_are_explicit` |
| N2 pruning and unrelated outcome annotation | `strongest_pruning_has_canonical_approval_shape`; `amendment_keeps_unrelated_outcome_passed` |
| Final cold authorship/claims/prior exposure | `final_cold_rejects_author_or_claims_exposure`; `final_cold_actor_must_be_disjoint` |
| Required vendor diversity / absence disclosure | `available_second_vendor_required_otherwise_absence_disclosed` |
| M4 reproducer self-attestation; fresh bindings | `final_attestations_are_required_and_bound` |
| Cold event order, digest, actor and binding integrity | `cold_commitment_tampering_holds`; `cold_phases_reject_wrong_actor_and_late_commit` |
| Separate access, withheld plan and retained delivery | `cold_delivery_access_and_retention` |
| Delta reviewer cannot supply final sweep after reveal | `unblinded_delta_reviewer_cannot_be_final_cold` |
| Cold residual and ordinary counter stay additive | `cold_blocking_residual_is_additive`; `complete_cold_cannot_clear_ordinary_counter` |
| Supported complete GO | `complete_supported_fixture_go` |
| Changed retained bytes block publish | `publish_rechecks_changed_bytes` |
| F9 single live resolution, exact saved snapshot | `publish_resolves_once_and_saves_that_snapshot` |
| Increment-1 integration: artifact HOLD, snapshot, complete successor GO | `increment_one_integration_hold_then_successor_go` |
| M5 all successor alternatives visible | `parent_audit_lists_all_successor_alternatives` |
| M7/N4 restoration stays held; new root recovers | `unapproved_lineage_requires_new_root`, plus existing per-attempt renewal/expiry regressions |

### 1c validation evidence

N1 was the first edit and first execution: **2 passed, 171 deselected**. An initial
focused integration run passed **16 tests, 173 deselected**. A subsequent full run
was intentionally interrupted when the cold delivery design review required
retained source/access resources and withheld plan contents; that partial run is
not counted as green evidence. The final full command uses a fresh scratch root.

Six isolated source mutations were caught: reduction cause guard, unknown comparator
fallback, strongest pruning, annotation of unrelated targets, reproducer's own
bound accept, and known ancestor exposure. Copies and per-test logs are retained
under the private task scratch `inc1c-mutants-1caec917`; repository source was never
mutated. This includes the previously surviving N1/Cj, N2/Cf and N2/Cp guards.

Final foreground command, with `PYTHONPATH=<CLONE>/src` and
`PYTHONDONTWRITEBYTECODE=1`:

```text
python -m pytest tests/test_acceptance.py tests/test_close.py tests/test_close_signoffs.py tests/test_gates.py -q --basetemp <SCRATCH>/pytest-1c-final -p no:cacheprovider
```

Result: **544 passed, 1 skipped** in 378.26 seconds (209 acceptance cases plus
335 existing close/signoff/gate cases passed). The skip remains host-restricted
symlink creation. No attention test execution is claimed for this implementation
round. Targeted Ruff on the six touched production modules and the acceptance
test module passed. `close acceptance cold --help` was executed from the source
checkout and agrees with the guide. Whitespace, eleven-file scope, branch and
unchanged design-copy checks passed. Privacy sweep: **8/8 positive controls,
zero added-content matches**, including both new files.

Scratch remains under the private acceptance task directory for review: the
initial/final fixture stores, isolated mutation copies/logs and verification
script. No reviewer workspace or unrelated untracked directory was modified.

The lead must commission the final whole-increment cold sweep by an uninvolved,
unexposed reviewer before any PR. This implementation handoff does not substitute
for that review or grant merge/release authority.

## 1c correction — actor provenance and change-wide freshness

Base: `6c82647`. Lead findings M1–M7 are dispositioned below. Earlier sections are
historical; this section supersedes their role-specific exclusion, lineage-only
exposure and fail-whole-show behavior. No serialized route/report schema changes.

### Structural rule and identity

`acceptance_audit.provenance` walks the attempt and retained lineage. It interprets
attribution keys (`by`, `from`, `actor`, `owner`, `*_by`, `authors`, `agents`) across
record lifecycle metadata, declared participants, execution/reproduction records,
amendments and the event ledger. The exclusion set is derived once from that
provenance; there is no independently maintained opener/freezer/attacher list.
New event names require no exclusion update. New schema attribution must use this
grammar or explicitly extend it alongside its reader inventory.

For the current attempt, events before the first cold commitment are participation
evidence. For ancestors, all events predate the child's sweep and are considered.
Recorded lifecycle actors and declared execution participants remain excluded.
Passive roster/lens assignments and post-commit current review/ack actions do not
make a reviewer its own forbidden predecessor. The 32-link bound remains enforced.

M4 change identity is conservative: same verified `project_id` and either the same
revision or intersecting `(partition, artifact, field)` targets. `check_prior_exposure`
scans stored closes and retained ancestors; a prior reveal by the assigned reviewer
before the current commitment HOLDs even across a new root. Later reveals do not
retroactively disqualify an earlier sweep merely because it was checked again.
Corrupt/unreadable history makes eligibility unverifiable and HOLDs; diagnostic show
separately returns healthy records plus errors. Retained local history is the scope
of this audit; external/unrecorded knowledge is still declared under LD1.

### Dispositions and regression coverage

Names omit `test_acceptance_`.

| Finding | Disposition | Test |
| --- | --- | --- |
| M1 structural actor exclusion | FIXED with shared provenance, including arbitrary event and amendment actors. Required pre-fix table produced **7 failed, 4 passed, 210 deselected**; all seven bypasses were observed before production edits. | `every_recorded_actor_is_excluded_from_final_cold`, 11 role cases; `plan_opener_and_distinct_attacher_cannot_review` covers the real configured-liaison scenario without vendor/authority masking. |
| M4 fresh reviewer across new roots | FIXED with project plus source/target identity and ordered reveal evidence. | `new_root_cannot_reuse_unblinded_reviewer[same-revision/same-targets-new-revision]`; existing fresh-actor recovery remains green. |
| M6 attachment ordering | FIXED: schema-3 attach refuses missing cold commitment before retaining/mutating bundle or lenses, leaving the attempt retryable. | `cold_phases_reject_wrong_actor_and_late_commit` verifies byte-identical record on refusal and successful commit/attach retry. |
| M5 diagnostic show on corruption | FIXED: skip failing entries, report their close ID/error in `acceptance_successors_error`, retain healthy parent/children output. | `show_reports_corruption_without_hiding_record[unrelated-close/child-parent]`. |
| M2 withheld content | FIXED minimum content check: reject delivered digests matching the attempt/ancestry's policy, registry, bundle, amendment, parent records or result artifacts, at submission and reevaluation. Exact-byte identity only. Re-encoding/excerpts/prose expectations and physical separation of the shared private content store remain bounded residuals documented in the guide. | `withheld_plan_digest_cannot_be_mislabelled[source/safety/toolchain/access]`. |
| M3 roster truth/completeness beyond participants | COOPERATIVE-PROFILE RESIDUAL per lead: availability and labels are plan-writer declarations, not authenticated store metadata; may be relabelled across attempts. Hardened-profile work, no stronger enforcement claimed. | Existing declared-roster diversity tests; guide explicitly distinguishes participant validation from active-seat roster truth. |
| K3 attached actor isolation | Covered without the earlier vendor masking. | Configured-liaison test above. |
| K20 pre-reconciliation accepts | Covered by actual early runner/reproducer accepts, later reconcile and cold accept, then fresh accepts. | `pre_reconciliation_accepts_are_stale`. |
| K19 source/access presence, K18 kind allowlist | Covered with otherwise-valid reports so each guard independently matters. | `delivery_guards_each_refuse_otherwise_valid_report[missing-source/mismatched-access/unknown-kind]`. |
| K8 reproduction vendor, K23 participant roster | Covered independently of cold-vendor and KeyError paths. | `reproduction_must_use_available_second_vendor`; `monoculture_roster_must_include_participants`. |
| K11 unique events, K22 dynamic lenses | Added tests despite redundant protections. | `duplicate_attach_commitment_event_holds`; `attach_installs_reproducer_lenses`. |
| Reviewer self-disposition of findings | Bounded cooperative residual now explicit in guide; full owner/lead ledger is not claimed by this slice. | Existing blocking-observation and ordinary-counter tests remain. |
| Cold command's lead-authority warning | Removed irrelevant advisory lead check for cold phases; assigned-reviewer enforcement in `submit` remains mandatory. | Existing wrong-actor rejection plus CLI cold tests. |

### Reader inventory

| Surface | Existing readers and assumptions |
| --- | --- |
| Actor attribution and pre-commit event prefix | New audit consumes `load_close`, `_policy`, retained parent/amendment/bundle bytes and their existing actor fields. Existing producers `new_close/freeze/attach/_event/apply_ack/set_draft/record_publish/successor` are unchanged in shape. Passive assignments are not actor actions. Cold `evaluate` alone consumes the derived exclusion set. |
| Change-wide exposure scan | `list_close_ids/load_close`, `_policy`, coverage `target_id` and retained ancestry are read-only inputs. Cold `evaluate` invokes it before allowing final review. Ordinary close readers do not gain this scan. No mutable cached exposure verdict or new persisted index is introduced. |
| Withheld digest audit | Cold `submit` and `evaluate` share the audit; `_retain/_retained` retain their limits/hash checks. Current/ancestral route hashes and bundle artifacts supply protected identities. |
| Attach sequencing | CLI acceptance attach, `acceptance.attach` and transactional close readers see a refusal before any route/lens write. Existing bundle validation and attachment-event schema are unchanged. |
| `successors` return shape | Only CLI show calls this helper; updated together to unpack children and errors. `acceptance_successors_error` is a non-persisted output key, not a verdict field. Existing show JSON readers must tolerate this additional diagnostic key. |
| Cold command authority dispatch | CLI retains `_resolve_self`; cold `submit` enforces assigned reviewer. Attach/successor still use the prior advisory lead helper. |
| Other engines and snapshot consumers | Route/report/bundle/ack schemas remain version 3/1/3/current. Older pre-1c engines still reject version 3. The pre-correction 1c engine lacks these new policy checks and must not be used for final eligibility; the cooperative system cannot force a same-user engine upgrade. `acceptance.evaluate`, pure DoD/verdict, final/attention/barrier readers retain their field contracts. |

### Correction evidence

Required red command: `python -m pytest tests/test_acceptance.py -q -k every_recorded_actor --basetemp <SCRATCH>/pytest-1c-correction-red -p no:cacheprovider`.
Result **7 failed, 4 passed, 210 deselected**. Focused post-fix actor/workflow run:
**15 passed, 206 deselected**. New guard selection: **16 passed, 221 deselected**.
All runs use foreground execution, `PYTHONPATH=<CLONE>/src` and private task scratch.

Final bar: `python -m pytest tests/test_acceptance.py tests/test_close.py tests/test_close_signoffs.py tests/test_gates.py -q --basetemp <SCRATCH>/pytest-1c-correction-final -p no:cacheprovider`
→ **573 passed, 1 skipped** in 444.51 seconds (238 acceptance + 335 ordinary close
checks; host-restricted symlink skip). Attention suites were not rerun in this
correction; their readers and persisted final field contracts are unchanged.

Nine isolated source-copy mutations were executed against the corresponding
regressions: **K3, K20, K19, K18, K8, K23, K11, K22 and M4 all KILLED**. Each run
executed tests and produced an asserted behavioral failure, not a collection error.
The runner and individual logs are retained under private task scratch
`inc1c-correction-mutants-3d27b2dc`; production sources were never mutated in place.

`python -m ruff check --no-cache src/agenttalk/acceptance.py src/agenttalk/acceptance_audit.py src/agenttalk/acceptance_cold.py src/agenttalk/acceptance_history.py src/agenttalk/cli.py tests/test_acceptance.py`
→ all checks passed. Privacy verification checks eight positive controls and all
added content, the exact nine-file scope, branch, accepted design bytes and
`git diff --check`. Basetemps and mutation evidence are retained for the delta
review. No PR or release is authorized by this correction's completion.

## 1c correction 2 — source ancestry, scoped audit and gate actors

Base: `d1110a2`. This section supersedes the prior correction's revision-equality
or target-overlap definition of related work. The lead's recovery rule remains:
unapproved coverage loss poisons its lineage, recovery requires a new root, and
operator approvals are per attempt. A new root does not erase prior exposure.

### N1 source identity and required red table

Within the same verified project identity, Git's `merge-base --is-ancestor` in
both directions determines whether revisions are related. Either direction is
enough. Target overlap may add exposure, never subtract ancestry. Comparisons use
the verified project locator and full commit identities; ambient Git redirection
is removed and replace objects disabled. Git errors/timeouts, missing objects or
shallow history cannot establish independence and fail closed with
`acceptance_cold_missing`. Neither a new SHA nor changing every measurement label
can clear exposure to ancestral work. Each Git subprocess has a ten-second bound.

`relabelled_recovery_uses_source_ancestry` ran before production edits:
**3 failed, 1 passed, 239 deselected** (15.87 seconds), with failures at the pinned
cold hold assertion, not collection/setup errors.

| Prior/current source relationship; all targets renamed | Expected |
| --- | --- |
| Current descends from previously revealed revision | HOLD `acceptance_cold_missing`; GO publish refused |
| Current is ancestor of previously revealed revision | Same HOLD and refusal |
| Sibling branches; neither revision ancestral to the other; targets disjoint | No exposure hold; complete fixture publishes GO |
| Git ancestry cannot be verified | HOLD `acceptance_cold_missing`; GO publish refused |

### Findings and test mapping

Names below omit `test_acceptance_`.

| Finding | Disposition and evidence |
| --- | --- |
| N1 ancestry instead of writer labels | FIXED, four-case table above exercises real project history and an injected Git error. |
| N2 corrupt history and project scope | FIXED. Read project identity before traversing a record's policy/ancestors. Readable different-project records are skipped; unknown/same-project corruption still HOLDs. Error names close ID/path and trusted-backup restoration or operator quarantine outside `.agenttalk/closes`, explicitly warning that quarantine removes exposure evidence. `exposure_corruption_scoped_with_remedy[unknown-project/same-project/different-project]`. |
| N3 external gate attribution | FIXED for reliable recorded data. `provenance` reads `gates.load_gate_state`, includes applicable scope/global gates and remediation-named gates, and interprets their update/evidence attribution with the same actor grammar. Pre-commit updates and retained evidence count; later updates do not retroactively taint a sweep. Unreadable gate state HOLDs. `gate_actor_provenance[milestone/global/remediation/unrelated]` uses metadata without evidence; `gate_retains_earlier_evidence_actor` isolates earlier evidence after another actor updates the gate. |
| N4 R8 ancestor bundle actors | `ancestor_bundle_actor_without_ack_is_excluded` isolates an actor recorded only in an ancestor reproduction, without its own acknowledgment. |
| N4 R10 result-artifact digests | `ancestor_raw_artifact_is_withheld` rejects a parent's raw result delivered as safety input. |
| N4 R19 evaluation-time delivery recheck | `evaluation_rechecks_withheld_delivery` installs a consistently rehashed report, reconciliation, events and accepts containing forbidden plan bytes; no stale-binding guard masks the delivery check. |
| N4 R2 owner grammar | `owner_provenance_excludes_reviewer` isolates a remediation owner without an attribution event. |
| N4 R14 unique cold commitment | `exposure_requires_one_commit_event` directly exercises the exposure reader's ambiguity rejection, independently of the later event-order check. |
| N4 R21 attacher in vendor participants | `monoculture_roster_includes_attacher` isolates an omitted attacher without diverse-vendor KeyError masking. |
| N4 R13 later reveal ordering | Added despite stricter-mutant direction: `later_reveal_does_not_taint_earlier_commit` keeps an earlier completed sweep eligible after another root reveals. |
| N4 R12 project identity scope | Project isolation is tested by N2's different-project case. The inner lineage project guard is retained defensively: removing it alone is redundant for valid same-project ancestry after the outer identity filter and successor project binding. No claim that this single redundant mutant is killed. |
| N5 canonical-JSON leakage check | Deferred as the reviewer-accepted exact-byte-only bound. Re-encoding, whitespace, excerpts and prose can evade content identity; this is explicitly documented, not represented as an automated expectation scan. |
| Interim binaries / identity aliases | No schema bump: released/pre-1c engines still reject supported schema 3; earlier development commits lack later policy checks and must not decide final eligibility. Distinct roster names for the same person remain a cooperative disclosure. |

Gate records are mutable current state, with retained evidence entries where
present; there is no complete immutable gate actor journal. Removed gates,
overwritten attribution without evidence, changed scope, and other unrecorded bus
activity remain cooperative-profile residuals beside declared roster truth.
No gate is written by this audit. Authenticating roster/vendor/person identity
or adding an immutable bus-wide activity ledger belongs to the hardened profile.

### Changed-reader inventory

Only `acceptance_audit.py` changes production behavior. Cold `submit` and cold
`evaluate` remain its callers. `related_revisions` reads the project's Git object
graph; it never executes supplied commands, updates refs, fetches or changes HEAD.
`check_prior_exposure` reads close JSON for project scoping before validated
`load_close`/retained-policy traversal. Diagnostic show retains the prior
nonfatal `acceptance_successors_error` behavior. Gate attribution consumes the
existing `load_gate_state` normalization/error contract, `updated_by/updated_at`
and evidence `by/at`, plus close `gate_scope` and remediation gate names.
No serialized route, report, bundle, acknowledgment, final, attention or barrier
field changes. No CLI flags or public mutation commands change.

### Executed correction evidence

All pytest runs are foreground with `PYTHONPATH=<CLONE>/src`, bytecode disabled,
and private assigned `<SCRATCH>` basetemps. Required red command:
`python -m pytest tests/test_acceptance.py -q -k relabelled_recovery --basetemp <SCRATCH>/pytest-1c-round2-red -p no:cacheprovider`
→ **3 failed, 1 passed, 239 deselected**. Focused new regressions after the fix:
**18 passed, 239 deselected** in 45.79 seconds.

After separating gate metadata/evidence cases, the final five-test gate selection
passed: **5 passed, 253 deselected** in 10.86 seconds. The four scope tests now have
no evidence entries, so `updated_by` is tested independently; the added fifth case
tests preserved evidence independently of the current updater.

Regression bar: `python -m pytest tests/test_acceptance.py tests/test_close.py tests/test_close_signoffs.py tests/test_gates.py -q --basetemp <SCRATCH>/pytest-1c-round2-final -p no:cacheprovider`
→ **591 passed, 1 skipped** in 488.62 seconds (256 acceptance + 335 ordinary close
checks). The skip is host-restricted symlink creation. This bar started before the
one additional gate-evidence test and the metadata-only refinement; those final
test changes are covered by the separate five-test selection above. Production
code was unchanged throughout both runs. Attention suites were not rerun; their
persisted final reader contracts are unchanged.

Isolated source-copy mutations: **12/12 KILLED**. R8, R10, R13, R19, R2, R14, R21,
N1 (remove ancestry), N2 (remove outer project filter), N3 (remove gate attribution)
are retained in `inc1c-round2-mutants-6025c027`. The two independent gate-update and
gate-evidence mutants are retained in `inc1c-round2-mutants-2f07d5b0`. Each executed
tests and failed a behavioral assertion; no collection-error result is counted.
R12's redundant inner lineage check is explicitly excluded from this claim.

`python -m ruff check --no-cache src/agenttalk/acceptance_audit.py tests/test_acceptance.py`
→ all checks passed. Private verification: eight positive controls, zero matches
in added content; five-file scope, branch, accepted design bytes and
`git diff --check` verified. Task gates returned GO. Scratch basetemps, source copies,
mutation logs and verifier are retained for the delta reviewer; no PR or release.

## 1c correction 3 — whole-change patch identity and settled cooperative boundary

Base: `79323ff`. This final correction adds content identity alongside source
ancestry, within verified project identity. The lead explicitly excludes deliberate
fabrication of project identity and changes that deliberately alter content identity
from the cooperative profile. This is a threat-model boundary, not a claim that
intent can be detected. Authenticated reviewer identity and signed change identity
are the hardened-profile remedy; neither is implemented in this slice.

### Content identity contract

Schema-3 `cold_policy` now requires `change_base`, a full commit SHA identifying
the boundary before the whole change. `prepare` verifies both base and candidate
as commits in the verified project repository and verifies base ancestry before
creating a close. There is no implicit last-commit fallback. Missing/abbreviated
bases, absent commit objects, unrelated bases, empty diffs and shallow histories
fail closed. A root-only candidate without an existing base is unsupported.

The identity is Git `patch-id --stable` of one aggregate base-to-candidate diff,
not a list of individual commit IDs. Diff options explicitly disable external
diffs, text conversion, rename heuristics and color, and request full binary/index
data with fixed prefixes/context and algorithm. Patch bytes are passed directly
to Git; no shell pipeline, executable supplied by a caller or generated temporary
patch file is used. The diff has the existing 16 MiB acceptance limit; each Git
subprocess retains a ten-second timeout. Ambient Git redirection is removed and
replace objects are disabled for both ancestry and content reads.

The frozen plan hash binds the declared base. Evaluation recomputes the current
whole-change identity even when no earlier exposure exists. For a prior reveal
by the same reviewer in the same project, either direction of Git ancestry,
overlapping protected targets, or equal aggregate patch IDs establishes the same
change. Unknown patch identity is never treated as independent. A successful cold
snapshot gains `change_identity: {base, revision, patch_id}` and publication retains
that resolved snapshot. Stable patch IDs deliberately ignore whitespace/line
numbers: equivalent formatting variants may be conservatively identified together;
this is not authenticated change identity. Contract reference:
[Git patch-id documentation](https://git-scm.com/docs/git-patch-id).

Rebase/cherry-pick copies must declare their new verified base before the copied
range; a squash declares the base before the same aggregate diff. The tool verifies
objects and ancestry, not the semantic honesty of a writer's declared boundary.
Fabricated project identity, a deliberately misleading boundary or changed diff
identity remain explicit cooperative residuals. Existing same-project scoping and
the operator-visible corrupt-record repair/quarantine policy are unchanged.

### Required table and other findings

`rewritten_change_identity_table` ran on the old implementation first:
**4 failed, 1 passed, 258 deselected** in 21.75 seconds. Failures were the pinned
exposure HOLD assertion after complete real CLI workflows, not setup errors.
The prior attempt contains two commits; every new plan relabels all row/artifact
IDs to isolate content identity from target matching.

| Table row | Expected outcome |
| --- | --- |
| Actual `git rebase --onto` of the two-commit change | HOLD `acceptance_cold_missing`; GO publication refused |
| Actual squash of the same aggregate diff | Same HOLD and refusal |
| Actual cherry-pick of both commits onto another branch base | Same HOLD and refusal |
| Genuinely different diff on an unrelated branch, disjoint targets | No exposure hold; complete workflow publishes GO |
| Git patch-ID computation fails | HOLD `acceptance_cold_missing`; GO publication refused |

| Finding | Disposition / test (omit `test_acceptance_`) |
| --- | --- |
| N1' content-equivalent rewritten history | FIXED by aggregate patch identity; table above. Source ancestry and target-overlap checks remain additive. |
| Deliberate identity/content evasion | OUT OF SCOPE per lead LD1 decision; user guide states limits and authenticated/signed identity remedy. No claim that fabricated new project identities or altered content IDs are detected. |
| Verified multi-commit base | `change_base_verified_before_creation[missing/abbreviated/unknown/candidate/unrelated]`: invalid or empty boundaries leave no close behind. |
| S1 shallow guard | `ancestry_preconditions_fail_closed[shallow]` isolates the guard before Git could independently refuse ancestry. |
| S4 replace-object guard | `ancestry_ignores_replace_graft` creates a real Git graft that hides the parent unless replacement lookup is disabled. |
| S5 full-SHA guard | `ancestry_preconditions_fail_closed[abbreviated]` uses a resolvable abbreviation, so Git itself would accept the missing guard. |
| S12 unreadable gate state | `unreadable_gate_attribution_holds` checks the cold snapshot directly, independently of ordinary gate holds. |
| S8/S9 pre-cutoff filters | `later_gate_metadata_and_evidence_do_not_taint_commit` covers both latest metadata and evidence after the cold commitment. Both stricter mutants are killed. |
| Global gate over-blocking | Conservative recorded global participation remains excluded, including bots. Stated in the guide; no claim that overwritten historical actors remain available. |
| Oversized/unreadable close audit | Retain the 1 MiB JSON limit and fail-closed refusal with existing close/path/repair remedy; bounded availability trade-off. |
| Git cost under check/publish | Unmeasured per-store scaling retained as a residual: patch comparison adds object/diff/hash reads to matching prior exposures. Per-command timeouts and diff byte cap are not an overall store-scan budget. No performance claim. |
| Quarantine | Operator must preserve the file and record the decision; removal loses exposure evidence and cannot prove freshness. Guide updated. |

### Reader and compatibility inventory

| Changed contract | Producers/readers |
| --- | --- |
| Required `cold_policy.change_base` | Written by the plan author; strict `acceptance_cold.policy` validates shape/SHA through plan validation and vendor-policy reading. `acceptance.prepare` verifies Git objects/ancestry/nonempty aggregate diff before creation; `_policy` rereads retained plan shape; successor calls the same prepare path. Freeze, report bindings, bundle bindings and accepts already pin the plan hash and therefore its base. |
| Git identity helpers | Audit `_git` is shared by ancestry and content; `check_prior_exposure` recomputes current and matching prior identities from immutable commit objects at the verified locator. Delivery/provenance callers otherwise retain their contracts. No ref, HEAD or checkout writes. |
| `cold.change_identity` snapshot member | Cold evaluation produces it; resolve, CLI check and publish carry it; terminal `final.acceptance_snapshot` retains it. Attention/barrier readers consume outer final fields and remain unchanged. |
| Earlier development engines | Strict previous cold-policy readers reject the new required field; this engine rejects prior schema-3 plans lacking it. Fail closed both ways, no automatic migration for this unreleased format. Schema-1/2 plans and routes retain their prior HOLD-only behavior; released/pre-1c engines still reject schema 3. No route/bundle/ack schema or CLI flag changes. |

### Executed evidence

Foreground runs use `PYTHONPATH=<CLONE>/src`, bytecode disabled, and assigned private
`<SCRATCH>` basetemps. Required red command:
`python -m pytest tests/test_acceptance.py -q -k rewritten_change_identity_table --basetemp <SCRATCH>/pytest-1c-round3-red -p no:cacheprovider`
→ **4 failed, 1 passed, 258 deselected**. Post-fix rewrite/ancestry/complete-GO
selection: **11 passed, 252 deselected** in 56.25 seconds. New guard/base selection:
**10 passed, 263 deselected** in 11.26 seconds.

Final command: `python -m pytest tests/test_acceptance.py tests/test_close.py tests/test_close_signoffs.py tests/test_gates.py -q --basetemp <SCRATCH>/pytest-1c-round3-final -p no:cacheprovider`
→ **607 passed, 1 skipped** in 574.32 seconds (272 acceptance + 335 ordinary close
checks). Skip: host-restricted symlink creation. Attention suites were not rerun;
their outer persisted-final contracts remain unchanged.

Nine isolated source-copy mutants were executed: **S1, S4, S5, S12, S8, S9,
patch-match, base-ancestry and whole-diff all KILLED**. The last mutant substitutes
the final commit's parent for the declared aggregate base; the squash case kills
it. Every result contains executed behavioral assertion failures, not collection
errors. Logs and copied sources remain in private task scratch
`inc1c-round3-mutants-453c7bf6`; the production checkout was never mutated in place.

`python -m ruff check --no-cache src/agenttalk/acceptance.py src/agenttalk/acceptance_audit.py src/agenttalk/acceptance_cold.py tests/test_acceptance.py`
→ all checks passed. Privacy verification: eight positive controls, zero matches
in added content; exact seven-file scope, branch, accepted design bytes and
`git diff --check` verified. Task gates returned GO. Basetemps, mutation runner,
logs/source copies and verifier are retained for the final reviewer. No PR or
release; the lead orders the independent final cold sweep after this handoff.

## Final independent sweep correction — obligations and hygiene

The independent sweep found a root-reset bypass and two missing enforcement
requirements. This entry supersedes the earlier statements that a new root clears
all obligations and that informational evidence errors never gate. The accepted
design changes only its contradictory reviewer/reproducer sentence to the lead's
final LD3 ruling: disjoint actors, including after commitment.

| Finding | Disposition / enforced behavior |
| --- | --- |
| Blocker: fresh root loses failed assertions | Fixed. Project history scanning and the same ancestry/whole-change patch-ID/target identity are shared by reviewer exposure and obligation recovery. Earlier related attempts contribute all strongest gating predicates and compact original outcomes. A new root clears inherited procedural holds only. |
| Same SHA, rebased or squashed change | Renaming every target does not remove obligations. A fresh reviewer and new root still HOLD `acceptance_category_moved_unreviewed` without the exact operator amendment. |
| Different history/content and disjoint targets | No inherited obligations; the complete supported fixture can publish GO. |
| Recovery authorization | Bundle `recovery_approvals` references each prior attempt and an actual operator-message artifact. It uses the existing LD2 exact payload, origin, expiry, target-set and predicate checks. Restoring original assertions and passing them needs no exception. Approved weakened rows retain original failure and `policy-amended`; approvals are per attempt. |
| Major: missing execution/offline/hygiene evidence | Fixed with required schema-3 run/reproduction environment and offline-proof artifacts, execution close-out and final reconciliation close-out. The closed shapes and sealed-manifest encoding are in ACCEPTANCE.md. |
| Major: informational artifact deletion/corruption | Fixed. Required artifact retention is checked for every manifest entry; raw-result shape/binding failures HOLD independently of assertion policy. Informational comparison failure remains non-gating. |
| Minor: cold reviewer reproduces after commit | Lead ruling retained: LD3 actors are disjoint for the entire pass. A specifically named ordered regression enforces it. The guide states the design-text override. |
| NIT: obsolete CLI help | Open/acceptance help now describes schema-3 cooperative GO and current operations. |

The obligation scan reads earlier attempts, including open attempts with frozen
plans, and excludes the current retained lineage already checked by the existing
amendment evaluator. Later-created attempts do not retroactively taint an earlier
pass. Source protections are conservative: every retained gating predicate is
preserved, including contradictory predicates introduced by an unapproved attempt;
restoring only one side then needs an exact amendment. Historical failures remain
visible even when current original assertions pass. The same existing project
corruption and unverifiable-identity refusals apply to both audits. Deliberately
removing history or fabricating a new project/change identity remains outside the
documented cooperative profile; it is not an authorized recovery procedure.

Minimal supported hygiene profile: externally enforced egress denial with an
owned-loopback restriction and a positive control; no attempted fetch; retained
version banners, isolated scratch/cache/service-data descriptions and per-service
PID/socket/teardown evidence; all scratch removed, owned services stopped and ports
released. Recipe-only offline evidence and approved scratch retention are not yet
supported, so they cannot GO. Evidence is bound to the attempt and each original
or reproduction run. The execution manifest excludes only its hygiene result.
The final evidence projection includes the initial delivery, revealed execution
set, retained lineage and reconciled findings, with the final close-out result
excluded to avoid a cycle. Acknowledgments/publication are subsequent derived
metadata, outside that source-evidence projection. Retention is rechecked at
publication. These records enforce evidence presence, shape and binding in LD1's
cooperative model; no authenticated sandbox or confidential-content detector is
claimed. Vendor availability, actual access separation and truthful execution
remain the previously documented cooperative residuals.

Readers changed together: `_bundle`/attach/resolve, the pure verdict fold, cold
reconciliation validation, test bundle/report producers and the user guide. New
schema-3 fields make older development engines reject the closed bundle/report
shapes. Schema-1/2 routes remain HOLD-only; ordinary closes retain their contract.
The coverage approval evaluator is shared by linked amendments and recovery roots.
No route, acknowledgment or ordinary final-envelope schema was relaxed.

Failing-first evidence: the new-root weakening and missing/corrupt informational
artifact regressions each failed on the unmodified engine: **3 failed, 273
deselected**. The observed hold sets were empty in all three cases. The first
post-fix selection (original regressions, complete GO and content-identity table)
passed **9 tests**, 267 deselected. Further results are recorded below after the
final foreground regression and privacy checks.

### Executed verification for the final-sweep correction

All pytest commands ran in the feature worktree with `PYTHONPATH=<WORKTREE>/src`,
bytecode disabled, foreground processes, private assigned `<SCRATCH>` basetemps
and `-p no:cacheprovider`.

| Command / selection | Observed result |
| --- | --- |
| `python -m pytest tests/test_acceptance.py -q -k 'recovery_root_preserves_failed_obligation or informational_required_artifact_integrity' --basetemp <SCRATCH>/final-red -p no:cacheprovider` before production edits | 3 failed, 273 deselected: all three unsafe hold sets were empty. |
| `python -m pytest tests/test_acceptance.py -q -k 'recovery_root_preserves_failed_obligation or informational_required_artifact_integrity or complete_supported_fixture_go or rewritten_change_identity_table' --basetemp <SCRATCH>/final-target1 -p no:cacheprovider` | 9 passed, 267 deselected. |
| `python -m pytest tests/test_acceptance.py -q -k 'recovery_obligations_follow_change or recovery_requires_original or ld3_reproducer or hygiene_evidence or final_sealed_closeout or unapproved_lineage_requires_new_root' --basetemp <SCRATCH>/final-target2 -p no:cacheprovider` | 26 passed, 275 deselected. |
| `python -m pytest tests/test_acceptance.py tests/test_close.py tests/test_close_signoffs.py tests/test_gates.py -q --basetemp <SCRATCH>/final-regression -p no:cacheprovider` | 636 passed, 1 skipped, 4 failed in 708.05 seconds. Three failures were superseded expectations that malformed/missing informational records have no direct hold. One was the test helper constructing a manifest before deliberately submitting a premature reconciliation. |
| `python -m pytest tests/test_acceptance.py -q -k 'malformed_informational or cold_phases_reject_wrong_actor or rewritten_change_identity_table or complete_supported_fixture_go or final_sealed_closeout' --basetemp <SCRATCH>/final-corrected -p no:cacheprovider` after correcting those test expectations/helper | 16 passed, 290 deselected in 63.72 seconds, including all four failures and adjacent GO/identity/manifest paths. |

The broad run and corrected selection together validate **640 distinct passing
cases and one host-restricted symlink skip**. This is not a claim that a second
full green command was run: no behavioral production correction was needed after
the broad run, so only the affected expectations/helper and adjacent paths were
rerun. Source changes afterward were indentation/help-line formatting only.
Attention suites and a full-repository run were not repeated.

Five isolated source-copy mutations all produced behavioral assertion failures:
removing the recovery obligation audit, offline positive control, scratch cleanup,
confidentiality positive control, and pure-fold hygiene guard. Runner
`mutate-final.py` and logs/source copies in `final-mutants-f836f7b3` remain under
private task scratch. No production source was mutated by that runner.

`python -m ruff check --no-cache src/agenttalk/acceptance.py src/agenttalk/acceptance_audit.py src/agenttalk/acceptance_history.py src/agenttalk/acceptance_hygiene.py src/agenttalk/acceptance_cold.py src/agenttalk/cli.py tests/test_acceptance.py`
passes. Two initial long help lines were wrapped. The privacy verifier checks all
added content with eight positive controls, exact twelve-file scope, branch,
whitespace, and design bytes permitting only the authorized LD3 sentence change.
All scratch from this correction is retained for the sweeper's recheck; no reviewer
scratch was modified. No PR, merge or release is part of this work order.

## Recovery obligations: structural correction (R1, R2)

Inventory recorded before implementation, from DESIGN acceptance rows, attempts,
residual ledger and GO conditions, and the ordinary/acceptance verdict folds:

| Obligation | Recovery resolution |
| --- | --- |
| Gating predicates, failed/unmeasured comparisons, removed or weakened coverage | Current passing evidence for retained coverage, or the exact existing LD2 approval; original outcomes remain visible. |
| Pending partition/final review counters | Existing reviewed counter disposition; accepting a blocker requires its remediation and green named gate. |
| Blocking cold findings, including an unreconciled blocking observation | Materialize a source-bound counter; use the same reviewed disposition/remediation path. |
| Accepted blocker remediation and named gates | Preserve counter/remediation and require the named gate, independently of fresh acceptance acknowledgments. |
| Required but absent/invalid/expired operator approval | Recompute the complete protected coverage and revalidate the exact per-attempt LD2 approval. A general counter disposition cannot waive it. |
| Missing/corrupt required retained records, artifacts and bindings | Re-read the retained source evidence closure. Restore the exact bytes; a disposition cannot waive integrity. Seal the source records and closure in final close-out. |
| Ordinary review lenses, signoff/risk routing and gate scope | Preserve requirements; obtain fresh eligible acknowledgments and current gate/signoff evaluation. |
| Execution, project, independence, offline and hygiene prerequisites | Fresh bound evidence is required by the normal evaluator; any recorded unresolved substantive hold not represented above becomes a reviewed counter, so unknown hold kinds cannot disappear. |
| DoD assurance/coverage/knowledge, isolation and other ordinary holds | Current ordinary checks remain additive; recorded unresolved holds require reviewed disposition when not otherwise represented. |
| Procedural cold freshness, stale acknowledgments and inherited lineage poison | Fresh procedural lineage and evidence are allowed; this never removes the substantive records above. |
| Complete prior attempt and verifiable source identity | Preserve the terminal prior record, as a normal successor requires; reverify its commit/tree/roots. Recapture updated history in a fresh attempt. |

Implementation boundary: one recovery inheritance fold uses retained complete
source records for both linked successors and new roots. It carries structured
obligations, rather than trusting a cached acceptance outcome alone. Existing
counter decisions are the reviewed-disposition mechanism; no new waiver API.
The table will cross every substantive representation with same SHA, rebase,
squash and different change. Related changes HOLD until their resolution;
unrelated history/content with disjoint targets adds no inherited obligation.

Implementation: `acceptance_obligations.inherit` is the common capture/evaluation
fold for schema-3 successors and recovery roots. A new mandatory
`obligations_hash` retains a catalog of complete source-close hashes. The catalog
is bounded to 256 source records including transitive parent/recovery sources;
overflow, missing bytes and malformed bindings fail closed. Source plans,
registries, bundles, all artifacts, cold reports/deliveries, amendment/approval
records and referenced reduction evidence join the final sealed manifest.
Execution hygiene is checked even for a source not yet published, so a recorded
cleanup/offline failure cannot evade inheritance by omitting the final snapshot.

Review counters are keyed by original attempt/kind/id, preventing close-local
counter-ID reuse from merging different defects. Cold findings normalize to this
same ordinary counter representation. Copied reviewed decisions remain evidence;
new decisions require matching counter events and are bound in the reconciliation
digest. Accepted blockers use the existing ordinary remediation-gate fold.
Unrecognized saved HOLD codes become counters by default; comparison/LD2,
integrity, cold freshness and current acknowledgment checks have explicit existing
resolvers. Conflicting inherited lens definitions or remediation IDs refuse
instead of choosing one silently. Scope/risk/signoff requirements cannot shrink.

Contract readers: freeze produces the catalog; `_route` requires its digest;
`ack_binding` and cold `binding` include it; the shared inheritance fold reads it;
cold provenance includes source actors and withheld bytes; final hygiene seals
the evidence closure and inherited dispositions. The CLI counter workflow is
unchanged. Ordinary close schema and pure verdict code are unchanged. Older
development schema-3 routes lacking the catalog fail closed, and older engines
reject the new exact route/report fields. Schemas 1/2 remain HOLD-only. Design
bytes are unchanged in this correction.

The single `recovery_obligations_follow_change_table` enumerates the following
representations. Each row runs at identical SHA, on a rebase, on a squash and on
different history/content with disjoint targets. Expected results are HOLD for
the first three and no inherited HOLD for the fourth; current ordinary gates
still apply independently.

| Table case | Concrete obligation exercised |
| --- | --- |
| coverage | Removed original gating target and its failed comparison. |
| approval | Changed gating predicate without exact operator authorization. |
| counter | Pending partition review counter (R1 real-CLI reproduction). |
| cold | Blocking committed cold observation reconciled open (R1 real-CLI reproduction). |
| final-review | Pending counter raised on the final cold review lens. |
| remediation | Accepted blocker with an unresolved named repair gate. |
| gate | Named prior blocker gate, independently of row predicates. |
| missing / corrupt | Prior failed result deleted/corrupted after sealing (R2). |
| hygiene | Prior execution close-out records incomplete scratch cleanup. |
| offline | Prior execution records failed egress enforcement. |
| review-requirement | Required extra review lens absent from the new request. |
| routing | Recorded specialist/risk requirement. |
| dod | Recorded missing assurance evidence. |
| isolation | Recorded worktree isolation failure. |
| unknown | Future substantive HOLD code, defaulting to reviewed disposition. |
| unfinished-source | Prior attempt has not preserved its terminal snapshot; recapture after publishing HOLD. |

The last four ordinary/future representations inject a saved verdict/requirement
record to test the consumer, independently of their existing producer suites.
The counter, cold finding, integrity, execution and gate cases use real CLI or
normal gate producers. Different-change gate tests remove the shared live gate
to isolate inheritance; they do not claim that different changes bypass live
gate requirements. Separate tests exercise ordinary counter resolution in both
root and linked recovery, exact LD2 approvals, retained-source deletion, pre-open
loss of prior raw evidence, post-check loss, decision events and disposition-seal
staleness.

Self-review added three failing-first edge probes in private scratch. An
unfinished prior attempt was accepted, loss of a cherry-picked source commit was
not rechecked, and an exact copy of the newly captured full close was not yet in
the cold withheld set. All three are now pinned in committed tests (unfinished
source is the seventeenth table row). The Git-object probe first hit Windows'
read-only file bit; after correcting that fixture it failed specifically on the
missing required HOLD. No reviewer scratch or production source was mutated by
these probes. Source Git verification and terminal-state checks use the latest
retained version per attempt, while sealing retains every source version.
An additional positive test recaptures completed source history and reaches GO.

Cooperative limits remain explicit: ordinary reviewed counter/remediation
evidence uses its existing text/event contract; arbitrary text references are
not fetched as files. Every schema-declared retained digest is verified and
sealed. Authenticated actors, signed change identities and semantic detection
of re-encoded expectation leakage remain the previously accepted hardened-profile
work. Generic reviewed disposition never waives digest/binding failure or LD2.

Final self-review also exercised recovery through a verified clone after the old
checkout retires. Its initial probe failed because source verification used the
old locator. Verification now uses the current verified repository and compares
every retained commit/tree/root identity; missing historical objects still HOLD.
The new positive test also publishes GO, rather than checking the helper alone.

### Executed verification for structural recovery

All commands ran foreground from the feature worktree with `PYTHONPATH=<W>/src`,
bytecode disabled and `-p no:cacheprovider`. Basetemps and logs are under the
assigned private `<S>` scratch root. No production store or reviewer scratch was
modified. Selections below refer to `tests/test_acceptance.py`.

| Command / selection | Result |
| --- | --- |
| `python -m pytest ... -q -k 'recovery_obligations_follow_change_table and same-sha' --basetemp <S>/structural-red` before production edits | 4 failed, 1 passed, 317 deselected: counter, cold, missing and corrupt paths lacked HOLD; existing coverage control passed. |
| Same selection, `<S>/structural-target1` | 5 passed, 317 deselected. |
| `-k 'recovery_obligations_follow_change_table or inherited_review_requires or recovery_seals or recovery_requires_original'`, `<S>/structural-target2` | 53 passed, 297 deselected. |
| `python -m pytest tests/test_acceptance.py tests/test_close.py tests/test_close_signoffs.py tests/test_gates.py -q --basetemp <S>/structural-regression` | 712 passed, 1 skipped, 1 failed in 1179.48 seconds. The old ancestor-actor fixture rewrote its bundle without rebinding its cold report; corrected to construct the valid bundle before attachment. All 335 ordinary close/signoff/gate tests passed. |
| `-k 'ancestor_bundle_actor_without_ack or captured_source_record or recovery_rechecks_prior_commit or unfinished-source or recovery_refuses_new_findings'`, `<S>/structural-edges-green` | 8 passed, 377 deselected, including the corrected ancestor fixture. |
| `python -m pytest tests/test_acceptance.py -q --basetemp <S>/structural-final-acceptance` | 384 passed, 1 skipped, 1 failed in 1196.21 seconds. All 68 obligation-table cases passed. The new recapture test expected exit 0 from a successfully published HOLD with exit 3; corrected to assert both exit 3 and the terminal record. |
| Private current-clone probe before locator fix | 1 failed, 3 deselected: valid current clone incorrectly held when the old checkout was retired. |
| `python <S>/mutate-structural.py` on final production source | 7/7 killed: counter inheritance, cold finding inheritance, source-evidence reread, unknown-HOLD inheritance, disposition seal, historical Git verification, terminal-source requirement. Isolated source copies only; retained in `<S>/structural-mutants-98b41b52`. |

The intermediate `<S>/structural-target3` run had 27 passes and one fixture
failure: source-file deletion preceded a helper that still needed the source.
Moving that deletion after fixture construction preserves the intended test.
The symlink skip is host-restricted creation, not an unexecuted GO assertion.
No full-repository or performance result is claimed.

Final-source follow-up:
`python -m pytest tests/test_acceptance.py -q -k 'recovery_can_recapture or recovery_uses_current or recovery_rechecks_prior_commit or recovery_requires_original or inherited_review_requires or recovery_seals or recovery_dispositions' --basetemp <S>/structural-final-targeted -p no:cacheprovider`
passed **19 tests, 368 deselected** in 117.73 seconds. This reruns the corrected
recapture assertion, adds the verified-clone positive, and exercises historical
object refusal, exact approvals, reviewed dispositions and source sealing after
the locator change. Across the full runs and corrected/new-case follow-ups,
**386 distinct acceptance tests and 335 ordinary tests passed; 1 skipped**.
This is aggregate evidence, not a claim that one final combined command ran green.
Ruff on all seven touched Python files passed. The eight-control privacy check,
ten-file scope check, unchanged-design comparison and `git diff --check` passed.
Scratch logs, failing-first probes and isolated mutation copies are retained under
`<S>` for the sweeper's re-check; no reviewer evidence was changed.

### R3 correction: obligations at publication, independent of opening order

The lead's publish-time rule supersedes all earlier statements here that later
attempts cannot invalidate a frozen source set. The eleven obligation families
and seventeen concrete representations above remain unchanged. Their discovery
has no opening-time exemption: every related attempt must appear in the frozen
catalog at GO evaluation. A new or changed record invalidates the seal/review;
the recovery is terminal HOLD records followed by a fresh capture and cold pass.
No source bytes or decisions are appended to an already sealed review.

The single obligation table now crosses 17 kinds x 4 identity relationships x
3 orderings, for 204 cases. Identity relationships remain same SHA, rebase,
squash and genuinely different history/content with disjoint targets.

| Ordering | Setup | Related source result | Different-change result |
| --- | --- | --- | --- |
| earlier | Source records its obligation before publisher opens. | Existing obligation-specific HOLD. | No inherited HOLD. |
| later | Publisher opens first; source opens and records its obligation afterward. | Stale catalog HOLD before GO. | No inherited HOLD. |
| sibling | Both roots exist before source records its obligation; publisher froze an incomplete source snapshot. | Stale catalog HOLD before GO. | No inherited HOLD. |

This ordering axis applies to coverage, approval, counter, cold, final-review,
remediation, gate, missing, corrupt, hygiene, offline, review-requirement, routing,
DoD, isolation, unknown future hold and unfinished-source representations. The
table's siblings are concurrent roots; an additional real-CLI test exercises
two linked successors of the same terminal parent as well as two recovery roots.
The sweeper's exact preopened recovery scenario is included in that test.

Additional tests pin equal/backdated open timestamps at capture and the rescan
inside the existing close publication lock after an earlier clean check. Failed
GO leaves the frozen route, cold seal and close record unchanged. Cold exposure
still uses commitment-time knowledge: a later reveal cannot rewrite what a
reviewer knew earlier, but its attempt must enter the obligation audit at publish.

Failing-first: the three read-only sweeper probes each failed specifically because
GO returned 0 instead of 3 (**3 failed, 4 deselected**, 23.65 seconds). The expanded
table's later-counter case failed on the missing stale hold; sibling and unrelated
controls passed (**1 failed, 3 passed, 519 deselected**, 25.96 seconds). All probe
stores were isolated under assigned scratch; reviewer evidence was unchanged.

The capture/publication regression selection also failed first: **5 failed, 523
deselected** in 28.65 seconds. After removing both filters, the focused ordering,
unrelated-change, exposure-order, linked-sibling and lock selection passed
**18 tests, 510 deselected** in 110.77 seconds. Re-running the sweeper's unchanged
`test_structural_probes.py` passed **7 tests** in 55.34 seconds, including all
three original unsafe-GO cases and its four preservation/resolution controls.

Two isolated source-copy mutations restore the capture-time and publication-time
filters independently. Both are killed by the equal-timestamp capture and later
counter table tests. Logs are retained in `<S>/r3-mutants-312f5bce`; the working
source was never mutated by this check.

Only the common inheritance fold changes production behavior. Freeze and
`acceptance.resolve` retain their callers; publication invokes resolution again
inside its existing per-close transaction. Route/report/bundle/ack shapes and
ordinary close behavior are unchanged. The lock test establishes that boundary;
it does not claim store-wide serialization of all gate/evidence writers (the
existing CLI publish comment tracks that separate boundary under issues 66/31).

Final combined command, foreground with `PYTHONPATH=<W>/src`, bytecode disabled
and isolated assigned scratch:

```text
python -m pytest tests/test_acceptance.py tests/test_close.py tests/test_close_signoffs.py tests/test_gates.py -q --basetemp <S>/r3-regression -p no:cacheprovider
```

Result: **862 passed, 1 skipped in 2070.03 seconds**. All 204 table cases passed.
The skip is host-restricted symlink creation. This is one completed combined run
on the final production/test source, not an aggregate count. No full-repository
or performance result is claimed. The detailed output is `<S>/r3-regression.log`.
Ruff on the two touched Python files passed. Privacy: 8/8 positive controls and
zero added-content matches; exact five-file scope, unchanged design and
`git diff --check` passed. Scratch test stores, logs and isolated mutation copies
are retained for the sweeper's re-check; reviewer scratch remains unchanged.
