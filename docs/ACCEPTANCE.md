# Run a supported cooperative acceptance pass

This guide is for a lead and independent seats using the increment-1 built-in
comparators: `exit-code`, `exact-value` and `exact-failure-set`. It explains the
schema-3 workflow. Earlier acceptance schemas remain HOLD-only. The executable
synthetic example is `test_acceptance_increment_one_integration_hold_then_successor_go`
in [the acceptance tests](../tests/test_acceptance.py).

Agenttalk is a same-user cooperative system. Actor names, vendor availability,
fresh context and separate access are declared operating arrangements backed by
retained evidence and each participant's acknowledgment. They are not cryptographic
identity or proof of execution. Keep the reviewer's workspace and initial brief
free of the acceptance plan, expected counts, baselines and author claims.

## Prepare the assignment

Use a verified, clean project checkout, with runtime `.agenttalk/` files ignored
if the bus is in the same repository. Freeze a schema-3 plan with the ordinary
plan fields and this additional `cold_policy` object:

```json
{
  "reviewer": "cold-reviewer",
  "change_base": "0123456789abcdef0123456789abcdef01234567",
  "roster": [
    {"actor": "author", "vendor": "vendor-a"},
    {"actor": "runner", "vendor": "vendor-a"},
    {"actor": "lead", "vendor": "vendor-a"},
    {"actor": "reproducer", "vendor": "vendor-b"},
    {"actor": "cold-reviewer", "vendor": "vendor-b"}
  ],
  "absence_disclosure": ""
}
```

Replace the illustrative `change_base` with the full commit SHA immediately before
the complete change under review. Git must verify it as an ancestor of the candidate.
For a multi-commit change, pin the base before the first commit, not the final
commit's parent. The frozen plan hash binds this base. Missing/abbreviated bases,
unknown commits, unrelated bases, empty diffs and shallow histories are refused;
this workflow requires a nonempty change with an existing base commit. There is
no automatic last-commit fallback. Earlier development schema-3 plans lacking
`change_base` fail closed; this unreleased format has no automatic migration.

The roster is the available-vendor snapshot at assignment, not a list trimmed to
the preferred participants. Include every author, allowed runner, verifier,
reproducer and reviewer. When another vendor is available, use it for reproduction
and final cold review. With one vendor, provide a nonempty absence disclosure;
distinct actors and access remain required. The current rule conservatively
requires the final reviewer's vendor to differ from every author/actual-runner
vendor. Mixed-vendor author teams may therefore need another available vendor.

**Cooperative-profile residual:** the plan writer supplies availability and vendor
labels. The tool checks declared participant completeness, not the entire active
seat roster or authenticated vendor metadata. Labels can change between attempts.
Authenticated availability/vendor identity belongs to the future hardened profile;
the current checks do not establish that the declaration is truthful.

Gate attribution is also read from the bus: actors on gates in the close's scope
(including global gates), or gates named by remediation, count as participants
when their update/evidence predates the cold commitment. This uses recorded
`updated_by` and evidence attribution, including retained earlier evidence entries.
Gate state is mutable, not an immutable actor journal: overwritten updates without
retained evidence, removed gates, and activity elsewhere remain cooperative
disclosures. Two roster names for one person cannot be detected automatically.
Global gate participation is deliberately conservative: even a bot that only set
an unrelated global gate before the commitment is excluded while that attribution
remains recorded.

**Cooperative-profile boundary:** these checks catch accidental reuse for related
source history or the same whole-change content within the verified project
identity. Deliberately fabricating a replacement project identity, changing the
declared change boundary, or rewriting the diff so its content identity differs
is outside this profile. The tool cannot distinguish intent behind those actions.
The hardened-profile remedy is authenticated reviewer identity and signed change
identity, not additional Git heuristics. Keep an operator record of any history
repair or quarantine; removing a close is not proof of reviewer independence.

Open with `close open --acceptance-plan PLAN --project-repo PROJECT --revision SHA`.
Supply the usual ID, scope and actor, plus lane evidence or the existing explicit
non-lane declaration for milestone/release scopes. The plan adds partition lenses
and `acceptance-cold`; explicit lens assignments must agree with the plan.

## Commit cold observations before revealing the bundle

The assigned reviewer prepares strict JSON with these fields:

| Field | Content |
| --- | --- |
| `schema_version` | Integer `1`. |
| `binding` | Exact `instance_id`, `attempt_id`, `revision`, `project_id`, `plan_hash`, `registry_hash` from the route. Hashes identify withheld policy without revealing its content. |
| `reviewer`, `context_id` | Assigned actor and fresh session/context identity. |
| `claims_exposed`, `authored_in_scope`, `prior_exposure` | All `false`; disclose memory, earlier review or bus exposure instead of asserting false freshness. |
| `leakage_reviewed` | `true` after reviewing all delivered material for expectations leakage. This is an attestation, not an automatic token scanner. |
| `access_id`, `access_evidence` | Separate cold access identity and SHA-256 of an `access` resource in the delivery manifest. |
| `delivery_manifest` | Objects `{kind,path,sha256}`. Kinds: `source`, `safety`, `toolchain`, `access`. Require source and access evidence. Paths are relative to the report file. Plan/expectation/author-claim deliveries are refused. |
| `observations` | Objects `{id,blocking,evidence}`; blocking is a boolean. Empty is allowed for a clean sweep. |
| `blind_spots` | List of disclosed limitations; may be empty. |

The source delivery is a declared projection of the verified revision. The tool
retains and checks its exact bytes; the reviewer attests what was actually read.
Bounded resource retention is 1 MiB per input and 16 MiB total delivery. Large
projects must provide a bounded reviewed projection and disclose its blind spots;
this slice does not automatically package a whole checkout.

```text
agenttalk close acceptance cold --id PASS --phase commit --file INITIAL.json --from cold-reviewer
agenttalk close acceptance attach --id PASS --file BUNDLE.json --from lead
```

Commit must precede attachment. Only the assigned reviewer may submit a cold
phase. The initial report and all delivered bytes become immutable retained
evidence. Schema-3 bundles extend the schema-2 reproduction shape with required
execution hygiene and recovery approvals, described below. Attachment adds one reproducer lens per
declared reproduction, named `acceptance-repro-<reproduction-id>`.

Attachment before commitment is refused without changing the attempt; commit the
report and retry attachment on the same ID. Delivery hashes cannot match withheld
plan, registry, bundle, amendment, ancestor-record or result-artifact hashes under
another resource label. This exact-byte check does not detect excerpts, re-encoded
JSON or expectation leakage in arbitrary prose; leakage review remains a declared
cooperative control. The private content store contains both withheld policy and
delivered resources. It does not enforce filesystem read isolation: operators must
keep the reviewer's workspace/access separate from that shared store.

## Reconcile and attest

After reveal, the same reviewer submits JSON with `schema_version:1`, exact
`commit_hash` and `bundle_hash`, `revealed:true`, and `findings`. Each finding is
`{id,disposition,evidence}`, with disposition `open` or `resolved`; every initial
observation must appear exactly once. Unresolved blocking observations HOLD.
Reconciliation also requires the sealed `closeout` record described below.
In this bounded cooperative slice the reviewer classifies and reconciles its own
observations. This is not the full owner/lead-disposition residual ledger from the
design; ordinary close counters and remediation remain available for that workflow.

```text
agenttalk close acceptance cold --id PASS --phase reconcile --file RECONCILIATION.json --from cold-reviewer
```

Then obtain typed `close ack --status accept` acknowledgments from every partition,
every declared reproducer on its own lens, and the cold reviewer. Supply the usual
`--risk-class`, `--release-blocker`, `--tests-referenced`, `--tests-executed`,
`--residual-risk` and `--evidence` fields. These accepts bind the attempt, policy,
bundle and both cold-phase hashes. An earlier accept, `na`, override or another
actor's accept cannot substitute.

`close check --id PASS` recomputes the supported assertions and all gating-run
reproductions. `close publish --id PASS --verdict go --from lead` repeats live
verification under the close transaction and persists that same resolved snapshot.
Changed retained bytes after check prevent GO publication. Publishing HOLD keeps
the failing snapshot. Ordinary gates, counters, remediation and isolation holds
remain additive. Terminal checks explicitly report historical evaluation.

## Correct a failed attempt

Use `close acceptance successor` with a new ID, parent, plan, project, revision,
actor and amendment reason. It preserves the terminal parent and clears accepts
and cold phases. `close show --id PARENT` lists all linked alternatives, including
open successors. A reviewer already unblinded in this lineage may do a delta
review, but cannot supply the descendant's final cold sweep; assign a fresh actor.
Unreadable unrelated closes or child links appear in `acceptance_successors_error`
alongside the healthy parent and any discoverable children; they do not hide the
parent from the diagnostic view.

Eligibility derives from recorded actor provenance, not a short list of roles.
Opening/freezing the plan, attaching evidence, authorship, runner/reproducer
participation, amendment authorship, or any earlier recorded event excludes an
actor from the final cold sweep. Current review-phase events are not themselves
disqualifying; prior ancestor events are. Exposure is checked across root closes
as well: in the same verified project, Git checks whether either source revision
is an ancestor of the other. **Related source history identifies the same change
regardless of row, artifact, field or partition labels.** Overlapping protected
targets can additionally identify related work, but relabelling cannot erase
source ancestry. Git's stable patch ID of the **whole diff from the frozen,
verified `change_base`** also identifies the same change after rebasing,
squashing or cherry-picking that diff. Patch IDs are recomputed from Git objects,
not accepted from the plan writer. The successful cold snapshot records the base,
candidate revision and patch ID. A reveal before the current cold commitment
disqualifies that reviewer even with a new root or context name. This conservative
rule covers a new fix revision even with entirely renamed measurement targets.
Sibling branches with neither revision ancestral to the other, disjoint targets
and different whole-change patch IDs do not establish exposure to the same change.
Missing objects, Git/patch-ID errors or shallow
history cannot establish independence and HOLD; use the complete verified project
repository and restore missing commit objects before retrying.
The diff is limited to 16 MiB. Stable patch IDs ignore whitespace and line numbers,
so they can conservatively identify equivalent formatting variants as the same
change; they are not a cryptographic identity proof. See
[Git's patch-ID contract](https://git-scm.com/docs/git-patch-id).
The check depends on retained local close history; absent external history remains
a cooperative declaration. Records with a readable different-project identity are
skipped before their policy/ancestor blobs are traversed. An unreadable same-project
record, or one whose identity cannot be read, HOLDs eligibility. The refusal names
the close and path: restore that record and retained blobs from a trusted backup,
or preserve and quarantine the unreadable close outside `.agenttalk/closes` for
operator review. Quarantine removes its exposure evidence; it is not proof of an
independent reviewer. `close show` remains available to diagnose the store.

Coverage is tracked by `(partition, artifact, field)` across all retained ancestry.
Renaming, splitting or merging rows cannot erase the strongest historical gating
obligation. Weaker or absent coverage needs the exact reserved-operator approval
for each successor; approvals never carry automatically to the next attempt.
Use target-keyed `rows` and exact `changes` in `--scope-reduction`, as documented in
[the implementation contract](STEP-ACCEPTANCE-INC1.md). Tool/variance reductions
can remove gating coverage; replacing an assertion needs `policy-amendment`.
Reports preserve the original failure as reduced scope or policy amended.

**An unapproved coverage loss permanently HOLDs its lineage. Recovery requires a
new root close**, complete evidence and an eligible fresh reviewer. Restoring
coverage or adding approval in a later successor does not erase the earlier
unapproved change. A new root resets procedural lineage only: **protected
obligations follow the change**. Earlier related attempts are found using the
same verified project, ancestry, whole-change patch ID and additional target
overlap rules used for reviewer exposure. Their strongest gating assertions and
original outcomes remain in `related_obligations`, even when the recovery passes.
Deleting, renaming or weakening a target needs fresh exact operator approval;
changing the close ID or reviewer supplies no authorization. Earlier attempts
remain unchanged. As with lineage coverage, retaining all historical gating
predicates is conservative: contradictory historical assertions require an
explicit amendment, even if one assertion was introduced in an unapproved attempt.

Every schema-3 bundle contains `recovery_approvals` (an empty list if none).
Each entry is `{prior_attempt_id,reduction,approval_artifact}`. `reduction` uses
the existing exact target-keyed LD2 shape. The operator message body is the
existing `approval_payload(prior, new_close_id, frozen_plan_hash, reduction)`;
its `parent_attempt_id` identifies the prior obligation, not a procedural parent.
Put the actual reserved-operator message bytes in `approval_artifact`, an ordinary
`{id,path,sha256}` bundle artifact. Attachment checks those bytes against the bus
message at `decision_ref`. Evaluation rechecks origin, expiry, plan/attempt binding,
approved target set and full old/new predicates. An approval for one prior attempt
does not approve another; unused or duplicate references are refused. Approved
losses retain original outcomes and the `policy-amended`/`scope-narrowed` label.
Unchanged original assertions may pass without approval. No implicit approvals
carry to a successor or another recovery root.

The final reviewer and every reproducer must be **different actors**, even if
reproduction happens after the reviewer commits initial observations. This is the
lead's final LD3 ruling; it supersedes the earlier design sentence permitting a
cold reviewer to reproduce after commitment.

## Retain execution and close-out hygiene

Schema 3 requires this closed cooperative evidence contract. Missing, extra,
misbound or unreadable records HOLD; schemas 1/2 remain HOLD-only. Earlier
development schema-3 bundles without these fields are unsupported and must be
rebuilt. The verifier checks evidence, not the truth of a same-user execution
attestation. Offline tool acquisition and automatic harness execution remain later
increments. This bounded slice supports externally enforced egress denial;
recipe-only offline claims and retained scratch are unsupported and HOLD.

Each original run **and reproduction** adds `environment` and `offline_proof`,
artifact IDs in the bundle manifest. Their JSON records have `schema_version:1`,
`binding` containing the bundle's exact instance/attempt/project/revision/plan and
registry identities, and the exact `run_id`.

| Record | Other required fields |
| --- | --- |
| Environment | Nonempty `version_banners` string list; nonempty `scratch`, `cache_overlay`, `service_data` descriptions; `scratch_isolated:true`, `cache_overlay_fresh:true`, `service_data_fresh:true`, `outputs_outside_checkout:true`; `services` list. |
| Owned service | `{pid,ports,owned,stopped,ports_released,evidence}`. PID is positive, ports are valid integers, all three booleans are true, and evidence describes start/stop and socket checks. An empty service list declares no services. |
| Offline proof | `mode:"external-denial"`, `egress_denied:true`, `owned_loopback_only:true`, `positive_control:true`, `attempted_fetch:false`, and nonempty `evidence` retaining the enforcement/control log. |

The bundle adds `hygiene`, an artifact ID for a JSON execution close-out. That
record has `schema_version:1`, the same `binding`, `sealed_manifest`,
`bundle_digest`, `retained_readable:true`, `scratch_removed:true`,
`services_stopped:true`, `ports_released:true`, and `confidentiality`.
`confidentiality` is exactly `{positive_control:true,matches:[],evidence:TEXT}`;
retain the executed scan/control evidence, not just a pass label.

The execution `sealed_manifest` is the sorted unique SHA-256 list of every bundle
artifact except the hygiene result itself, plus the frozen plan and registry.
`bundle_digest` hashes canonical JSON of the bundle with only that hygiene artifact
entry removed. Canonical JSON uses sorted keys, UTF-8, `ensure_ascii=False` and
Python JSON's default separators (the same encoding as coverage approvals).
Excluding the result avoids a self-hash cycle while binding all run definitions,
observations, artifact references and recovery approvals.

Reconciliation adds `closeout:{sealed_manifest,report_digest,confidentiality}`.
Its manifest seals the current plan, registry, bundle, every execution artifact,
initial cold report/delivered resources and retained lineage records/artifacts.
Its `report_digest` hashes canonical `{report,dispositions}` JSON: `report` is the
reconciliation with `closeout` omitted; `dispositions` contains source-bound
inherited counters and their referenced remediation items on the current close.
The sweep covers revealed findings and reviewed resolutions without hashing its
own result. Changing a disposition after reconciliation makes the seal stale.
The implementation's `acceptance_hygiene.final_manifest` defines this evidence
projection. The later acknowledgment/publication envelope is derived metadata,
not an additional source artifact in this sealed set. All sealed retained bytes
are re-read at GO publication. `hygiene_checked` is required in the resolved
verdict snapshot; a cold pass alone is insufficient.

Comparison policy does not relax evidence integrity. An informational measurement
may fail its assertion without gating, but its required artifact must exist, match
its digest, parse as the closed raw-result schema and bind the right run/revision.

## Resolve obligations inherited by a recovery root

A new close ID provides a fresh procedural attempt. It retains substantive
obligations from prior attempts of the same change, including counters and cold
findings outside the measured assertions. Related means the same verified project
with related revisions, the same whole-change patch ID, or overlapping targets.
Different history/content with disjoint targets does not inherit obligations.

At freeze, schema 3 retains the complete prior close records and binds their
catalog through `acceptance_route.obligations_hash`. The catalog also follows
earlier recovery catalogs and linked parents (at most 256 source records).
Missing catalog/source bytes or required source artifacts HOLD. These bytes,
including original failed results, join the final sealed manifest and are read
again at publication. The original source commit/tree/root identity must still
verify in the current verified repository, even after rebase or cherry-pick. The
old checkout may be retired when its source objects remain available there.
Deleting an old close file does not delete its retained
obligations. Earlier development schema-3 routes without this field fail closed;
there is no migration that discards their history.

`close show` exposes inherited review counters with stable `ob-...` IDs and an
`obligation_source` identifying the originating attempt and finding. Blocking cold
observations without a resolved reconciliation become counters too. Decide them
using the ordinary lead workflow, before staging the fresh cold review:

```text
agenttalk close counter decide --id RECOVERY --counter OB_ID --from LEAD --decision reject --reason "Reviewed evidence disproves this finding"
agenttalk close counter decide --id RECOVERY --counter OB_ID --from LEAD --decision accept --reason "Confirmed defect" --rem-owner OWNER --rem-fix "Repair description" --rem-verification "Executed verification" --blocker --gate REPAIR_GATE
```

An accepted blocker still requires its named remediation gate to be green or
validly waived. A fresh accept acknowledgment does not decide a counter. Existing
non-acceptance review requirements and gate scope remain required; restore any
reported signoff/risk requirements and regenerate the ordinary signoff route.
Unknown saved substantive HOLD codes conservatively become review counters.
Recorded hygiene failures therefore require reviewed resolution, even when the
new execution's hygiene passes. Generic counter decisions cannot waive missing
source bytes or the separate exact LD2 authorization for weaker gating coverage.

This remains cooperative reviewed disposition, not authenticated proof that a
lead's explanation is true. Obligations belong to the change at GO publication,
regardless of attempt-open order. Source records are frozen as observed when the
new attempt opens. Evaluation rescans every related attempt, including later-created
roots and siblings; GO publication repeats that scan inside its close transaction.
If a related source is new or changed, the frozen catalog is stale and GO is
refused. Publish unfinished attempts as HOLD, then create a fresh attempt to
capture their findings and decisions and repeat evidence collection and cold
review. Evidence is never appended silently after its cold seal.
As with a linked successor, preserve a terminal prior
record: publish an unfinished prior attempt as HOLD before opening the recovery.
A later retained terminal version supersedes the unfinished version for this
prerequisite; both versions' evidence stays retained. A previously clean check
does not authorize publication against a now-incomplete source set. Already
published records remain immutable; this check governs each new GO decision.

All close writers in the local store share one writer lock, acquired before the
per-close lock. GO holds both from reload and source discovery through durable
publication. Opening another attempt, recording a counter, submitting a cold
report, or saving any other close mutation must wait or return a conflict while
publication owns the lock. If that writer completes first, publication sees its
obligations and refuses a stale candidate. If publication completes first, a
later writer cannot retroactively change its decision. This also serializes
ordinary close writes; it avoids a classification gap when acceptance is first
attached. Gate, knowledge and signoff stores retain their separate locking rules
(issues 66/31); external bus stores still require cooperative disclosure.

Audit enumeration is strict. Unreadable, missing or partly enumerated local
history produces `acceptance_audit_unavailable`, even when individual known files
remain readable. Restore access to the complete closes directory and retry;
replacing unavailable history with an empty directory discards evidence.
`close show` keeps displaying the requested record and reports the enumeration
failure in `acceptance_successors_error`. A genuinely empty readable directory
is distinct from unavailable history.
