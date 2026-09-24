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

The roster is the available-vendor snapshot at assignment, not a list trimmed to
the preferred participants. Include every author, allowed runner, verifier,
reproducer and reviewer. When another vendor is available, use it for reproduction
and final cold review. With one vendor, provide a nonempty absence disclosure;
distinct actors and access remain required. The current rule conservatively
requires the final reviewer's vendor to differ from every author/actual-runner
vendor. Mixed-vendor author teams may therefore need another available vendor.

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

## Reconcile and attest

After reveal, the same reviewer submits JSON with `schema_version:1`, exact
`commit_hash` and `bundle_hash`, `revealed:true`, and `findings`. Each finding is
`{id,disposition,evidence}`, with disposition `open` or `resolved`; every initial
observation must appear exactly once. Unresolved blocking observations HOLD.

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
