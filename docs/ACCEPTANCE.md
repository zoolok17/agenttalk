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
evidence. The schema-3 execution bundle otherwise has the schema-2 reproduction
shape, with its version changed to `3`. Attachment adds one reproducer lens per
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
unapproved change. The previous attempts and their failures remain in the store.
