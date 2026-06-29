---
reviewed-against: "0.43"
---

# Canonical evidence profiles

Every devkit skill ends in TYPED EVIDENCE, not vibes - because the dominant multi-agent
failure mode is false consensus, and two agents agreeing is worthless without executed
evidence. This file is the single source for the evidence output profiles. Each skill carries
a small in-skill stub of its profile's required fields (a loader may show only the skill body,
not this file) and links here for the full rules; the skill-currency check enforces that the
stub fields match this file (and, where the bus validates, the real gate/close schema).

Each field is marked:

- BUS-VALIDATED: enforced by agenttalk code today (named key + allowed values).
- SKILL-POLICY: required by the skill contract / lead process, not enforced by the bus today.

Do not blur the two. For example `reviewed_ref` and `scope` are important review-quality
fields, but `gates.validate_review_result_evidence` does not enforce them.

## planning-artifact

For requirements, design, strategy, and routing plans. Emits a plan, never an approval.

Required fields:

- `artifact_type` (SKILL-POLICY)
- `scope` (SKILL-POLICY)
- `decision` (SKILL-POLICY) - or `proposal`
- `assumptions` (SKILL-POLICY)
- `alternatives` (SKILL-POLICY)
- `risks` (SKILL-POLICY)
- `required_reviews` (SKILL-POLICY)
- `open_questions` (SKILL-POLICY)
- `evidence` (SKILL-POLICY)

Rules: do NOT emit `status=approved`/`status=rejected`; do NOT claim `tests_executed` unless
tests actually ran; a not-applicable close-compatible field is omitted or marked `n/a` with a
`na_reason`.

## production-handoff

For implementation, refactor, and CI-fix handoffs. Hands evidence to reviewers/lead; never
self-approves.

Required fields:

- `changed_files` (SKILL-POLICY)
- `base_ref` (SKILL-POLICY)
- `head_ref` (SKILL-POLICY) - or `dirty_artifact`
- `summary` (SKILL-POLICY)
- `tests_referenced` (SKILL-POLICY)
- `tests_executed` (SKILL-POLICY)
- `residual_risk` (SKILL-POLICY)
- `required_review_lenses` (SKILL-POLICY)
- `evidence` (SKILL-POLICY)

Rules: `tests_executed` is the exact command/result or a CI run id; if nothing ran, use `n/a`
+ `na_reason`.

## review-result

For code/docs/assurance reviews replying to a review-request. The ONLY profile the bus
validates today, and only for `status=approved` (`gates.validate_review_result_evidence`).

Required fields:

- `risk_class` (BUS-VALIDATED) - one of gates.CORE_RISK_CLASSES (none, unknown, release,
  device, accessibility, security, performance, persistence, docs-contract, quality) or a
  `project:name` extension.
- `release_blocker` (BUS-VALIDATED) - `yes`, `no`, or `unknown`.
- `tests_referenced` (BUS-VALIDATED)
- `tests_executed` (BUS-VALIDATED)
- `residual_risk` (BUS-VALIDATED)
- `evidence` (BUS-VALIDATED) - `evidence` OR `artifacts` must be present.
- `status` (SKILL-POLICY) - `approved`|`rejected`|`needs-info`.
- `reviewed_ref` (SKILL-POLICY) - the exact reviewed SHA/ref; a green without it is weak.
- `scope` (SKILL-POLICY) - the exact paths/surface reviewed.

Rules: the bus enforces typed evidence only for `status=approved`; rejected/needs-info
evidence is skill policy. For concrete findings include `finding_type` (correctness,
security, contract-drift, test-gap, docs, performance, architecture, maintainability),
severity, and file/line or command/output. A rejected review defaults to
`release_blocker=unknown` unless the request is release/close gated or the finding is proven
release-blocking. `needs-info` states the exact missing fact and who can answer it. An `n/a`
in any bus field needs a `na_reason`. `risk_class` is reviewer input, not the close router of
record (the lead-owned close inventory is authoritative).

## qa-result

For QA/tester evidence. When sent as an approved review-result the BUS-VALIDATED fields below
are enforced by the same validator.

Required fields:

- `status` (SKILL-POLICY) - `approved`|`rejected`|`needs-info`.
- `reviewed_ref` (SKILL-POLICY)
- `scope` (SKILL-POLICY)
- `risk_class` (BUS-VALIDATED)
- `release_blocker` (BUS-VALIDATED)
- `tests_referenced` (BUS-VALIDATED)
- `tests_executed` (BUS-VALIDATED)
- `evidence` (BUS-VALIDATED) - `evidence` OR `artifacts`.
- `residual_risk` (BUS-VALIDATED)

Rules: separate tests only inspected from tests actually executed; for a rejected QA include
the failing command/output or the exact gap; for an environment failure say whether it is a
real defect, a test bug, flaky, or environment.

## close-ack

For lead-recorded close acknowledgements (`agenttalk close ...`). Records and disposes
evidence; never manufactures it.

Required fields:

- `from` (BUS-VALIDATED)
- `status` (BUS-VALIDATED) - `accept`|`counter`|`na`.
- `revision` (BUS-VALIDATED) - the frozen close revision; a stale ack holds if it changes.
- `counter_id` (BUS-VALIDATED) - when `status=counter`.
- `remediation_id` (BUS-VALIDATED) - when accepting a counter (with owner, fix, verification,
  blocker flag, optional gate via `--rem-owner`/`--rem-fix`/`--rem-verification`/`--blocker`/`--gate`).
- `reason` (BUS-VALIDATED when `status=na`) - `close.py apply_ack` REQUIRES a non-empty reason
  for an NA ack (and for a counter decision); skill-policy otherwise.

Rules: an accept recorded through the CLI also carries the review evidence fields
(`risk_class`, `release_blocker`, `tests_referenced`, `tests_executed`, `residual_risk`,
`na_reason`, repeated `evidence`).

## na-result

For not-applicable evidence. A typed statement that the lens does not apply, NOT a weak
approval.

Required fields:

- `status` (BUS-VALIDATED) - `approved`.
- `risk_class` (BUS-VALIDATED) - `none`.
- `release_blocker` (BUS-VALIDATED) - `no`.
- `tests_referenced` (BUS-VALIDATED) - `n/a` (the bus validator requires it on ANY approved
  review-result; cover it with `na_reason`).
- `tests_executed` (BUS-VALIDATED) - `n/a` + `na_reason`.
- `residual_risk` (BUS-VALIDATED) - `n/a` + `na_reason`.
- `evidence` (BUS-VALIDATED) - `n/a` + `na_reason` (or `artifacts`).
- `scope` (SKILL-POLICY)
- `na_reason` (SKILL-POLICY)

Rules: na-result IS sent as an approved review-result, so it must carry the same bus-required
fields as review-result - as `n/a` covered by a single `na_reason`. Every `n/a` value in a
close-compatible field needs that `na_reason`.
