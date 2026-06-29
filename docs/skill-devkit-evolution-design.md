# Skill devkit evolution design

Audience: AgentTalk maintainers, lead agents, and implementers building the devkit expansion.

Mode: explanation and design proposal. This document is the gated design artifact for the skill-devkit
evolution after v0.42.0. It should be accepted before implementation changes update the bundled skills,
doctor checks, or official `docs/DESIGN.md` ADR list.

Status: design for lead gate.

## Summary

Evolve the devkit in two stages:

1. Tier 0 foundation: refresh the existing stale skill contracts, add shared evidence and routing references,
   add a mechanical skill-currency check, and tighten `review-code`.
2. Tier 1 skills: add only the highest-value new capability skills after Tier 0 lands. Start with `fix-ci`,
   `refactor-code`, and `qa-strategy`; add `test-integration` as Tier 1b if the Tier 0 implementation stays
   small and the lead accepts the extra scope.

Do not add a new `team-lead` skill. The existing `agenttalk-lead` bus skill is the team-lead skill; this design
updates it with evidence, capacity, and fresh-review responsibilities instead of creating a duplicate contract.

The key review-layer change is dual review mode, not more review-lens files:

- Context-preserving reviewers are standing roster reviewers who carry project and change history.
- Fresh-context ad-hoc reviewers are one-shot evidence-only reviewers launched through the existing
  `request-launch` and supervisor flow when that opt-in supervisor feature is available.

Capacity remains advisory. Leads and routing guidance should account for rate-limit and context-window headroom,
but capacity never blocks protocol progress by itself.

## Problem

The current devkit has the right philosophy but is too easy to let drift:

- The v0.42.0 lead-loop and relay work changed important bus contracts. Some skill bodies still reflected older
  liveness, relay, and evidence assumptions.
- The existing doctor skill checks catch install freshness, not source-content staleness. If the bundled source is
  wrong, installed copies can be byte-identical and still wrong.
- The proposed 24-skill taxonomy would add useful concepts but also a large routing and maintenance surface.
- More review lenses can manufacture false consensus: many shallow green replies look stronger than one integrated
  review with unique evidence.
- The operator wants token and usage limits treated as a first-class planning input.
- The operator also wants both continuity reviews and independent fresh-context reviews to be explicit modes.

## Constraints

- The devkit source is single-source: `src/agenttalk/skills/devkit/<name>/...` installs byte-identically to both
  Claude and Codex Agent-Skills directories.
- The bus skills remain format-specific under `src/agenttalk/skills/claude/` and `src/agenttalk/skills/codex/`.
- `install_skills._devkit_pairs` copies every file under `skills/devkit`. Shared reference files must still install
  inside a skill directory, not as a top-level non-skill directory at the Agent-Skills root.
- `agenttalk capacity` is privacy-safe and advisory. It reports derived rate-limit and context-window metadata.
- Ephemeral reviewers already exist but are disabled by default and supervisor-gated. `agenttalk request-launch`
  queues a launch marker; supervisor validates opt-in enablement, authority, caps, profiles, skills, groups, roles,
  full SHA, prompt bytes, and timeout; `wrap --loop --one-shot` drives exactly one request; completion requires a
  typed `review-result`.
- Ephemeral review approval is evidence-only and must not count as a close signoff.
- The design must not create a second transport, second task database, or second lead role contract.

## Inputs Folded

This design folds:

- The proposal in `docs/skill_proposals.md`.
- Codex architect input on trimming MVP-13, dropping `team-lead`, and adding a currency discipline.
- Dev-2 implementer input: devkit file count is not the main cost; content currency is. Add deterministic
  CLI-token lint and a version-stamp ratchet before expanding skills. Use shared references for long-form evidence
  rules, but keep a tiny in-skill evidence stub because a skill loader may not automatically read sibling reference
  files.
- Reviewer-1 assurance input: do not ship standalone `review-architecture`, `review-security`, or
  `review-performance` yet; make `review-code` verdicts machine-visible; keep finding types small; make evidence
  exact about reviewed ref and scope; do not count fresh-review approvals as signoffs.
- Reviewer-1 follow-up consult: default `REQUEST-CHANGES` to `release_blocker=unknown` outside release/close gates,
  require exact reviewed refs/scopes for review evidence, keep fresh-review triggers risk-based, and treat duplicated
  green evidence as corroboration rather than additive coverage.
- Dev-2 follow-up consult: `cli.build_parser()` makes command/flag introspection buildable; the scanner should be
  conservative; `reviewed-against` should warn on minor-version lag; the currency check should land warn-only or
  atomically with the stale refresh; and Tier 0 should stay tightly scoped.

## Non-goals

- Do not add `team-lead`.
- Do not add job-title skills in this tier (`business-analyst`, `solution-architect`). Later tiers can add
  action-named versions such as `define-requirements` or `design-solution`.
- Do not add standalone `review-architecture`, `review-security`, `review-performance`, `test-e2e`,
  `test-security`, `test-performance`, or `docs-qa` in Tier 1.
- Do not build a semantic prose verifier for skills. Tier 0 checks are mechanical and deterministic.
- Do not make capacity a protocol gate.
- Do not let evidence-only fresh reviewers satisfy close signoff sets.

## Tier 0 Foundation

Tier 0 is the enabler. It should land before any new capability skill.

Keep Tier 0 narrow. Include the currency check, the stale refresh it exposes, the shared evidence reference plus local
evidence stubs, a minimal routing index, and the `review-code` tightening. Exclude new capability skills, semantic
prose validation, and an exhaustive routing manual.

### 1. Refresh Existing Skills for v0.42.0

Refresh bundled skill bodies against the v0.42.0 contracts. Prefer a check-driven refresh: the initial currency check
should identify command/flag and metadata drift, and the human review should cover the semantic v0.42.0 contract drift
that token lint cannot prove.

- `agenttalk-lead` on both Claude and Codex:
  - State capacity-aware dispatch as a lead responsibility.
  - Explain managed lead-loop ownership of team mailboxes.
  - Explain relay flow: `relay operator-answer` and `relay operator-command`, reserved metadata, and why relays are
    typed wrappers over existing send/reply plumbing.
  - Explain when and how to request fresh-context evidence-only reviewers through `request-launch`.
  - Before requesting fresh-context review, check or record availability: supervisor configured/running,
    `ephemeral_reviewers.enabled=true`, allowed profile/skill, authorized requester, full revision, and caps. If
    unavailable, record the reason and continue with standing reviewers.
  - State that lead close decisions depend on unique evidence, not repeated green prose.
- `agenttalk-listen` on both Claude and Codex:
  - Ensure loop-exit language matches v0.42.0: idle means keep listening; only typed, authorized `release`/`end`
    controls exit.
  - Ensure review-request handling requires currentness checks and typed evidence.
  - Ensure operator-command and operator-answer notes are treated as ordinary bus messages with reserved metadata,
    not as new kinds.
- Existing devkit skills:
  - Replace repeated evidence boilerplate with a small local evidence stub plus a link to
    `../_shared/references/evidence.md`.
  - Link routing-sensitive skills to `../_shared/references/routing.md`.
  - Keep review and QA skills close-compatible.
  - Keep production and planning skills from emitting fake approval semantics.
- `review-docs`:
  - Add a procedural docs-QA mode: run documented commands/examples when they are meant to be runnable, and record
    command/result evidence. Do not add a new `docs-qa` skill yet.
- `review-code`:
  - Add machine-visible verdict guidance and finding-type taxonomy as specified below.

### 2. Canonical Evidence Schema

Add `src/agenttalk/skills/devkit/_shared/references/evidence.md`.

`_shared` is a reference-holder skill directory, not a direct-use capability. It has a tiny `SKILL.md` with
`category=reference` and instructions saying not to invoke it directly. This mirrors the proven
`review-code/references/security.md` layout: references are nested inside a skill folder, so installing the devkit does
not create a top-level `skills/references/` directory that might confuse Claude or Codex skill discovery.

This reference is the single place that defines evidence output profiles in full. Other skills should include only a
small required-field stub and link to this file. The skill-currency check should verify that in-skill stubs match the
reference and, where the bus has a real validator, the real gate/close schema.

This is deliberately not link-only. A model may receive only `SKILL.md` text and never open a sibling reference file,
so every skill needs enough local evidence shape to avoid fake evidence.

Each profile must mark fields as either:

- Bus-validated: enforced by AgentTalk code today. Name the exact key and allowed values.
- Skill-policy-only: required by the skill contract or lead process, but not enforced by the bus validator today.

Do not blur those categories. For example, `reviewed_ref` and `scope` are important policy fields for review quality,
but `gates.validate_review_result_evidence` does not currently enforce them.

Required profiles:

#### `planning-artifact`

For requirements, design, strategy, and routing plans.

Required fields:

- `artifact_type`
- `scope`
- `decision` or `proposal`
- `assumptions`
- `alternatives`
- `risks`
- `required_reviews`
- `open_questions`
- `evidence`

Rules:

- Do not emit `status=approved` or `status=rejected`.
- Do not claim `tests_executed` unless tests were actually run.
- If a close-compatible field is not applicable, either omit it or mark it `n/a` with `na_reason`.

#### `production-handoff`

For implementation, refactor, and CI-fix handoffs.

Required fields:

- `changed_files`
- `base_ref`
- `head_ref` or `dirty_artifact`
- `summary`
- `tests_referenced`
- `tests_executed`
- `residual_risk`
- `required_review_lenses`
- `evidence`

Rules:

- `tests_executed` is the exact command/result or a CI run id. If nothing was run, use `n/a` plus `na_reason`.
- Production handoffs do not self-approve. They hand evidence to reviewers or the lead.

#### `review-result`

For code/docs/assurance reviews that reply to a `review-request`.

Required fields:

- Bus-validated for `status=approved`: `risk_class`, `release_blocker`, `tests_referenced`, `tests_executed`,
  `residual_risk`, plus `evidence` or `artifacts`.
- Bus-validated allowed values: `risk_class` must be one of `gates.CORE_RISK_CLASSES` or a project extension like
  `project:name`; `release_blocker` must be `yes`, `no`, or `unknown`.
- Skill-policy-only: `status=approved|rejected|needs-info`, `verdict` in body when using richer review-code verdicts,
  `reviewed_ref`, `scope`, secondary risks considered, reviewed surfaces, and finding types.

Rules:

- Approved review results need exact `reviewed_ref` and `scope`; a green without ref/scope is weak evidence.
- The bus validator currently enforces typed evidence only for `status=approved`. Rejected and needs-info evidence
  requirements are skill policy, not bus validation.
- `risk_class` is reviewer input, not the close router of record. The lead-owned close risk inventory remains
  authoritative.
- For concrete findings, include `finding_type`, severity, and file/line or command/output evidence.
- Finding types are limited to `correctness`, `security`, `contract-drift`, `test-gap`, `docs`, `performance`,
  `architecture`, and `maintainability`.
- A rejected review should default to `release_blocker=unknown` unless the request is explicitly release/close gated
  or the reviewer can prove it blocks release. Use `release_blocker=yes` for release-readiness and close blockers.
- `needs-info` must state the exact missing fact and who can answer it.

#### `qa-result`

For QA/tester evidence.

Required fields:

- `status=approved|rejected|needs-info`
- `reviewed_ref`
- `scope`
- `risk_class`
- `release_blocker=yes|no|unknown`
- `tests_referenced`
- `tests_executed`
- `evidence` or `artifacts`
- `residual_risk`

Rules:

- Separate tests only inspected from tests actually executed.
- For rejected QA, include the failing command/output or the exact test gap.
- For environment failures, say whether the result is real defect, test bug, flaky, or environment.

#### `close-ack`

For lead-recorded close acknowledgements.

Required fields:

- `id` in CLI, stored as `close_id`
- `lens`
- `status=accept|counter|na`
- `from`
- `revision`
- `request_id` when the ack points at a review-result/request message
- `risk_class`, `release_blocker`, `tests_referenced`, `tests_executed`, `residual_risk`, `na_reason`, and repeated
  `evidence` entries when recording an accept through the CLI
- `counter_id` when `status=counter`
- `reason` when `status=na` or when recording an override/counter context

Rules:

- A close acknowledgement records and disposes evidence; it does not manufacture evidence.
- A counter uses `counter_id`. If the lead accepts that counter, the remediation item uses the real close schema:
  `remediation_id`, owner, severity, fix, verification, blocker flag, and optional named gate. Use the CLI names
  `--rem-owner`, `--rem-fix`, `--rem-verification`, `--blocker`, and `--gate`.
- A close ack is stored on the frozen close `revision`; stale acks hold if the close revision changes.

#### `na-result`

For not-applicable evidence.

Required fields:

- `status=approved`
- `risk_class=none`
- `release_blocker=no`
- `scope`
- `na_reason`

Rules:

- Every `n/a` value in a close-compatible field needs `na_reason`.
- NA is not a weak approval. It is a typed statement that the lens does not apply.

### 3. Skill Currency Check

Add a new doctor check, tentatively `skill_currency`, separate from install freshness.

Current checks:

- `_check_skills()` checks whether installed bus skills match bundled bus skill files.
- `_check_devkit()` checks whether installed devkit files match bundled devkit files.

New check:

- Validates bundled skill source files for mechanical currency.
- Reports deterministic warnings before stale source prose ships.
- Does not attempt semantic proof that every sentence is correct.

Scope:

- `src/agenttalk/skills/claude/*.md`
- `src/agenttalk/skills/codex/*/SKILL.md`
- `src/agenttalk/skills/devkit/*/SKILL.md`
- shared reference markdown under `src/agenttalk/skills/devkit/_shared/references/`

Frontmatter checks:

- `name` required for `SKILL.md` files.
- `description` required for skill files.
- `reviewed-against` required for bundled bus and devkit skills after Tier 0.
- `category` required for devkit skills: `coordination`, `production`, `assurance`, or `reference`.
- `evidence-profile` required for devkit skills unless `category=reference`.
- A short evidence stub is required in the visible `SKILL.md` for every non-reference devkit skill. It must match the
  shared profile in `_shared/references/evidence.md`.

Version ratchet:

- Parse `reviewed-against` as an AgentTalk semver string, with or without a leading `v`.
- Warn when the major/minor is older than the current package major/minor.
- Do not warn on patch-only lag by default.
- CI should fail if a changed skill has no stamp or has a malformed stamp.
- Release-gate policy may require all touched skill stamps to equal the release major/minor.

CLI-token lint:

- Build the command inventory from `agenttalk.cli.build_parser()`.
- Walk the argparse tree recursively through `_SubParsersAction.choices` and collect option strings from each parser
  node. Do not scrape `--help` output.
- Scan only fenced code blocks and inline-backtick spans containing `agenttalk` or `python -m agenttalk`; do not scan
  arbitrary prose.
- Recognize root invocations such as:
  - `agenttalk <subcommand> ...`
  - `python -m agenttalk <subcommand> ...`
- Validate subcommands and flags against the resolved argparse parser path.
- Treat global flags as valid before the subcommand.
- Stop flag validation after a literal `--` command separator, because wrapper examples pass through flags for the
  underlying agent CLI.
- Ignore placeholder tokens such as `<agent>`, obvious all-caps metavariables, and `REPLACE:`-prefixed wrapper
  placeholders.
- Allow an explicit ignore comment for rare false positives:
  - `<!-- agenttalk-skill-lint: ignore-next -->`
  - `<!-- agenttalk-skill-lint: ignore-line -->`
- Report file, line, token, and reason.

The lint proves only that referenced commands and flags still exist. It does not prove the surrounding prose explains
them correctly.

Tests:

- Unit tests for parser inventory extraction.
- Unit tests for CLI-token scan on good and bad snippets.
- Regression tests proving wrapper examples ignore flags after `--`.
- A source-tree test that all bundled skills pass the currency check.
- A package-data test proving new shared references are included in the wheel.
- A source-tree test that evidence stubs in skills match `_shared/references/evidence.md` and the real gate/close
  evidence schemas where applicable.

Doctor/CI behavior:

- `agenttalk doctor` should surface `skill_currency` as `warn` for user environments, not `error`, because stale skill
  prose is serious but should not make the bus unusable.
- The source-tree currency test, stamp-required CI rule, frontmatter migration, and stale skill refresh must land in
  the same change. No bundled skill currently carries all new fields, so a source-tree "all skills pass" test without
  the migration would red the matrix immediately.
- Doctor severity can remain warn for user environments even when CI treats bundled source regressions as failures.
- After the stale refresh is green, CI should fail on bundled source currency regressions.
- Release gate should include the new test, not rely on humans running `doctor`.
- A one-time packaging regression check should build a wheel and confirm `_shared/references/evidence.md` and
  `_shared/references/routing.md` are present. No packaging config change is expected because hatchling already ships
  package data under `packages = ["src/agenttalk"]`.

### 4. Routing Index

Add `src/agenttalk/skills/devkit/_shared/references/routing.md`.

The Tier 0 routing index should stay small: a task-to-skill table, negative triggers, and the minimum capacity and
dual-review rules needed to prevent false consensus. Rich precedence detail can grow after Tier 1 skills exist.

Core precedence:

1. Bus/team coordination uses `agenttalk-lead`, `agenttalk-listen`, `agenttalk-handoff`, `agenttalk-send`, or
   `agenttalk-sk-loop`. Do not create or use `team-lead`.
2. New production code uses `craft-code`.
3. Behavior-preserving cleanup uses `refactor-code`; if behavior changes, use `craft-code`.
4. Failing checks use `fix-ci` when the root task is diagnosing and correcting a check failure.
5. Low-level deterministic behavior tests use `test-coverage`.
6. Cross-module, CLI-plus-store, filesystem, config, migration, or boundary tests use `test-integration` once it
   exists; until then route through `qa-strategy` plus `test-coverage`.
7. Integrated diff review uses `review-code`.
8. Architecture/security/performance are sub-lenses under `review-code` unless routed as explicit specialist work with
   distinct evidence.
9. Release/milestone multi-lens closes use `system-review-protocol`.
10. Failure-path, contract-drift, release-readiness, and docs reviews use the existing specialist skills when their
    trigger is exact.

Required negative examples:

- Do not call `review-performance` without a workload, budget, dataset, complexity change, or measurement plan.
- Do not call `review-security` for cosmetic changes that do not touch auth, input, filesystem/process/env,
  dependency, sandbox, or secret surfaces.
- Do not call fresh-context review merely to accumulate approvals.
- Do not require fresh-context review when ephemeral reviewers are disabled or no supervisor is running; record
  unavailable and use standing reviewers.
- Do not use `docs-qa` as a separate skill in Tier 1; use `review-docs` with docs-QA mode.
- Do not use `qa-strategy` to avoid writing or running obvious tests.
- Do not let duplicated green evidence count as independent coverage. Two reviewers citing the same run and same scope
  are corroborating one evidence item, not producing two distinct proofs.

Capacity guidance:

- Classify optional lenses/checks as cheap, moderate, or expensive.
- Prefer cheap local evidence before launching fresh reviewers.
- Use fresh reviewers deliberately when risk justifies token cost.
- Capacity is advisory and never blocks a required safety review.

Lead GO checklist:

- Every risk class in the close inventory has an accountable owner.
- Every review/QA evidence item names exact ref and scope.
- Green evidence is unique enough to add coverage: different scope, risk, tool/run, or adversarial question.
- Rejections, needs-info results, and malformed fresh-review outputs are dispositioned.
- No approval rests only on another approval.

### 5. Tighten `review-code`

Update `review-code` to emit a machine-visible final verdict and small finding-type taxonomy.

Verdict mapping:

- `APPROVE`: `status=approved`, usually `release_blocker=no`.
- `APPROVE-WITH-NITS`: `status=approved`, `release_blocker=no`, nits listed in body and `residual_risk`.
- `REQUEST-CHANGES`: `status=rejected`, `release_blocker=unknown` by default. Use `yes` only when the request is
  release/close gated or the finding is proven release-blocking.

Rules:

- `APPROVE-WITH-NITS` is allowed only for truly non-blocking nits.
- Any mandatory follow-up, unresolved major, unverified required test, compatibility risk, or security concern is
  `REQUEST-CHANGES` or `needs-info`.
- `finding_type` is required only for concrete findings, not empty approvals.
- Approvals must list reviewed surfaces and risk classes considered.
- File/line references remain required for concrete code findings.

## Capacity Awareness Integration

Capacity awareness belongs in lead/routing behavior, not in protocol gates.

Update `agenttalk-lead`:

- Before dispatching long, parallel, or optional work, refresh the lead snapshot and inspect team snapshots:
  `agenttalk capacity refresh --for <lead>` and `agenttalk capacity`.
- Treat high primary/weekly usage, imminent reset, or high context-window fill as planning hints.
- Route heavy work away from near-capacity or near-compaction agents when another capable agent is available.
- If a required safety review has no low-capacity reviewer, run it anyway and record the capacity risk.
- Before launching fresh-context reviewers, record why the extra token cost is justified.

Update `qa-strategy`:

- Include a cost line for proposed checks: cheap, moderate, expensive.
- Recommend the lowest-cost test level that proves the behavior.
- Mark skipped optional checks with a reason, not silence.

Update `routing.md`:

- Fresh-context review is an expensive review mode.
- Use it for risk and context-diversity value, not as a default ritual.
- Capacity hints should shape sequencing and reviewer choice, not waive required evidence.

## Dual Review Model

AgentTalk should support two first-class review modes.

### Context-Preserving Review

Standing reviewers are rostered agents with accumulated project and change context.

Use for:

- Ordinary diff review.
- Contract drift and history-aware review.
- Continuity across iterative fixes.
- Verifying that a fix resolves a previously reported finding.
- Work where prior design context is useful rather than biasing.

Strength:

- Better continuity and memory.
- Better at catching drift from earlier decisions.
- Better at checking whether a remediation actually addresses a known blocker.

Risk:

- Context can become bias. A reviewer who helped shape the design may miss assumptions shared by the team.

### Fresh-Context Ad-Hoc Review

Fresh reviewers are one-shot, independent reviewers launched through the existing ephemeral-reviewer mechanism.

Availability preconditions:

- Supervisor must be configured and running.
- `supervisor.json` must opt in with `ephemeral_reviewers.enabled=true`.
- The requested profile, skill, role, and groups must be allowed by supervisor config.
- The requester must satisfy the existing authorized-lead rule: operator-facing requester, else sole active lead, with
  no zero-lead fallback.
- The request must use a resolved full revision and fit prompt/timeout/capacity caps.

If any precondition is unavailable, degrade gracefully:

- Record that fresh-context review was unavailable and why.
- Continue with context-preserving standing reviewers.
- Do not treat the missing fresh review as a missing signoff unless a project policy explicitly made fresh review
  required and available.
- Do not block GO solely because ephemeral reviewers are disabled by default.

Use for:

- High-risk final SHA verification.
- Changes touching gate, close, signoff, authority, lead-loop, relay, persistence, security, evidence schema,
  routing, install/currency generation, or role/authority behavior.
- Release-blocker fixes that change the reviewed surface after approval.
- Large final diffs where continuity may become bias.
- Work where standing reviewers participated heavily in design or implementation.
- Operator or lead request.

Make final-release-SHA fresh review conditional on risk and capacity unless the release touches gate, close, security,
persistence, or authority surfaces.

Invocation pattern:

```text
agenttalk request-launch --from <authorized-lead> --profile <profile> --skill <review-skill> \
  --revision <full-or-resolvable-ref> --path <path> --summary <summary> \
  --timeout-seconds <bounded-timeout> --file <prompt-file>
```

Prompt rules:

- Include frozen revision, base revision when relevant, paths, risk questions, and required evidence shape.
- Do not include prior team conclusions unless the review question requires checking a specific remediation.
- Ask for adversarial verification, not endorsement.
- Require exactly one typed `review-result`.

Evidence metadata for fresh reviewers:

- `status=approved|rejected|needs-info`
- `evidence_only=true`
- `signoff_eligible=false`
- `reviewed_ref=<sha>`
- `scope=<paths or summary>`
- `launch_id=<request-launch id>`
- normal review evidence fields from `_shared/references/evidence.md`

Semantics:

- Approved fresh review is supporting evidence only.
- Rejected fresh review is a counter/remediation signal that the lead must disposition.
- `needs-info`, malformed output, timeout, or no typed result keeps the launch on HOLD.
- Fresh reviewer approvals are never auto-converted into close signoffs.
- The lead GO checklist must include the fresh-review availability/disposition note when fresh review was considered.

The point is diversity of context, not more shallow lenses. Prefer one strong fresh review with a distinct question over
several duplicate green reviews.

## Tier 1 Skills

Tier 1 lands after Tier 0. Each new skill must link to `_shared/references/evidence.md` and
`_shared/references/routing.md`, carry
`reviewed-against`, and pass skill-currency checks.

### `fix-ci`

Use when the main task is diagnosing and correcting a failing local or CI check.

Contract:

- Identify the failing command/check and exact failure.
- Determine root cause: code defect, test defect, flaky test, environment, dependency, or CI configuration.
- Apply or propose the smallest fix.
- Run or reference verification.
- Emit a `production-handoff` evidence record.

Not for:

- General feature work.
- Broad cleanup after CI turns green.
- Guessing at failures without reading logs.

### `refactor-code`

Use when behavior must stay unchanged and the goal is structure, duplication, boundaries, or simplification.

Contract:

- State behavior-preservation scope before editing.
- No behavior change without explicit approval.
- Keep changes local and reviewable.
- Prove behavior preservation with tests or explain exact gaps.
- Emit a `production-handoff` evidence record.

Not for:

- Feature work.
- Bug fixes that alter behavior.
- Opportunistic broad cleanup mixed into product changes.

### `qa-strategy`

Use when deciding which tests/checks/lenses are necessary for a change.

Contract:

- Identify risk areas.
- Recommend test levels and review lenses.
- State which checks are not needed and why.
- Include cost notes: cheap, moderate, expensive.
- Identify required evidence for close.
- Emit a `planning-artifact` evidence record.

Not for:

- Writing tests directly.
- Replacing `test-coverage`.
- Avoiding obvious required tests.

### `test-integration` (Tier 1b)

Use when confidence depends on real boundaries: CLI plus store, filesystem behavior, config, migrations, multiple
modules, or process/supervisor boundaries.

Contract:

- Prefer real boundaries where deterministic and cheap.
- Use isolated temp roots.
- Avoid faking the boundary under test.
- Record exact command/result.
- Emit a `qa-result` or production test evidence record, depending on mode.

Not for:

- Pure function or low-level behavior tests where `test-coverage` is enough.
- Full user journeys that require unstable sleeps or external services.

## Folder and Frontmatter Conventions

Keep the current source layout:

```text
src/agenttalk/skills/
  claude/
  codex/
  devkit/
    _shared/
      SKILL.md
      references/
        evidence.md
        routing.md
    craft-code/
      SKILL.md
    ...
```

Do not nest skills by category. The install path is already flat and predictable.

Devkit frontmatter after Tier 0:

```yaml
---
name: refactor-code
description: >-
  Behavior-preserving code restructuring with explicit no-behavior-change evidence.
category: production
reviewed-against: "0.42"
evidence-profile:
  - production-handoff
---
```

Bus skill frontmatter after Tier 0:

```yaml
---
name: agenttalk-lead
description: Coordinate a named multi-agent team over agenttalk as a lead.
reviewed-against: "0.42"
---
```

Reference files do not need `SKILL.md` and must not be treated as invocable skills.

## Migration and Alias Policy

- Do not rename existing skills in Tier 0 or Tier 1.
- Do not add `team-lead`; route team leadership to existing `agenttalk-lead`.
- Expect a post-upgrade `_check_devkit` warning for users with previously installed devkit skills, because the
  frontmatter migration changes bundled source files. The migration note should tell users to inspect with
  `agenttalk install-skills --devkit-only --dry-run --force`, then reinstall with
  `agenttalk install-skills --devkit-only --force` if they accept overwriting local skill edits.
- Document deferred concepts as later action-named skills:
  - `business-analyst` concept becomes `define-requirements` or `shape-requirements`.
  - `solution-architect` concept becomes `design-solution` or `design-architecture`.
- If a later release renames a skill, keep the old name as a documented alias for at least one minor release when
  technically possible.
- Any new skill must have:
  - frontmatter,
  - evidence profile,
  - routing entry,
  - currency check coverage,
  - install/package coverage,
  - at least one focused test or source-lint assertion.

## Proposed ADR Entries for `docs/DESIGN.md`

These entries should be appended to `docs/DESIGN.md` after lead approval and implementation.

### D-16 Devkit skills are capabilities, not roster roles

Decision: AgentTalk roster roles describe who is accountable in a session; devkit skills describe reusable
capabilities applied to a task. The devkit stays flat and action-oriented. `agenttalk-lead` remains the team
coordination skill; no separate `team-lead` devkit skill is added.

Why: Duplicate lead contracts would diverge and confuse authority, routing, and evidence collection. Capability
skills compose across roster roles without creating a second role system.

Rejected: a job-title devkit taxonomy (`developer`, `tester`, `team-lead`) and nested category folders.

### D-17 Skill evidence is typed by output profile, not uniform

Decision: Skills use a shared evidence reference under `_shared/references/` plus small in-skill stubs. Planning and
production skills emit artifact or handoff evidence; review, QA, and close skills emit close-compatible verdict
evidence. Evidence fields are marked as bus-validated or skill-policy-only. `n/a` fields require reasons.

Why: Requiring every skill to fake review metadata creates boilerplate and false confidence. The assurance layer needs
honest evidence about what was decided, changed, reviewed, or executed.

Rejected: copying the full evidence schema into every skill, using a top-level non-skill `skills/references/`
directory, and requiring every skill to emit `status=approved`.

### D-18 Skill currency is mechanically checked

Decision: `doctor` and CI include a deterministic skill-currency check: frontmatter and version stamps, evidence
stub/profile presence, parity with the shared evidence reference and real gate/close schemas where applicable, and
CLI-token lint against the live argparse command/flag surface.

Why: Existing install freshness checks can prove bundled and installed files match but cannot prove bundled prose is
current. Mechanical lint catches renamed commands/flags and missing maintenance metadata without pretending to verify
semantics.

Rejected: semantic LLM review as a required freshness gate and relying only on byte-identity install checks.

### D-19 Review quality comes from context diversity plus unique evidence

Decision: AgentTalk review routing supports both context-preserving standing reviewers and, when the supervisor is
configured for it, fresh-context ad-hoc reviewers launched through the existing evidence-only ephemeral reviewer
mechanism. Fresh approvals support a close but do not count as signoffs; fresh rejections are counters that must be
dispositioned. If fresh review is unavailable, the lead records that fact and falls back to standing reviewers.

Why: Standing reviewers preserve history and contract continuity. Fresh reviewers catch shared-assumption failures and
false consensus. More shallow review-lens files are less valuable than deliberate diversity of context and unique
evidence.

Rejected: counting ephemeral approvals as signoffs and adding many standalone review lenses before they prove distinct
evidence value.

### D-20 Capacity-aware dispatch is advisory

Decision: Leads, routing guidance, and QA strategy consider `agenttalk capacity` snapshots when sequencing heavy work,
optional lenses, and fresh reviewers. Capacity never blocks a required protocol action by itself.

Why: Token and usage limits affect execution quality and availability, especially near compaction or rate limits. But
capacity signals are best-effort and privacy-preserving; they are planning hints, not safety gates.

Rejected: using capacity as an authorization or close gate.

## Risks and Mitigations

| Risk | Mitigation |
|---|---|
| Currency lint false positives | Limit scanning to lines with `agenttalk` invocations; stop after `--`; provide ignore comments with file/line reporting. |
| Currency lint over-promises | Document that it proves command/flag existence only, not prose correctness. |
| Shared reference link is not loaded at runtime | Keep a tiny evidence stub in each visible `SKILL.md`; enforce parity with the long-form reference. |
| `_shared` appears in skill discovery | Give `_shared/SKILL.md` `category=reference` and "do not invoke directly" wording; routing points users to real skills. |
| Tier 0 scope creep | Exclude semantic lint, new review skills, job-title skills, and performance/security standalone skills. |
| Shared references become stale | Put evidence and routing under the same currency check and version-stamp ratchet. |
| Fresh review becomes ritual | Routing requires risk/capacity justification; duplicated green evidence is not additive. |
| Fresh reviewers get counted as signoffs | Require `evidence_only=true` and `signoff_eligible=false`; close ack must still be explicit and authorized. |
| Capacity becomes a hidden blocker | State advisory-only behavior in lead skill, routing, and DESIGN ADR. |
| Review-code status mapping is too strong | Default `REQUEST-CHANGES` to `release_blocker=unknown` except release/close blockers. |
| Too many new skills after Tier 0 | Tier 1 is limited to three skills plus optional `test-integration`; later skills require usage evidence. |

## Delivery Plan

1. Implement Tier 0 in an isolated worktree.
2. Land the source-tree currency test, stamp-required CI rule, frontmatter migration, and stale skill refresh
   atomically.
3. Keep doctor `skill_currency` severity warn for user environments.
4. Add or update tests for skill-currency parsing, install/package data, evidence-stub parity, and review-code
   evidence wording.
5. Cross-review Tier 0 with standing reviewers.
6. Optionally request one fresh-context review if available and if the implementation changes doctor/currency
   validation, evidence schema, close semantics, or lead authority wording. If unavailable, record the reason and
   proceed with standing reviewers.
7. Lead-gate Tier 0.
8. Implement Tier 1 skills after Tier 0 is accepted.

Optional staging:

- Tier 0a: currency check, CLI lint, frontmatter stamps, and v0.42.0 stale refresh, landed atomically.
- Tier 0b: evidence reference, in-skill stubs, minimal routing index, and `review-code` taxonomy.

Use this split if the hand-edited v0.42.0 refresh makes a single Tier 0 diff too large.

## Evidence

Inputs read:

- `docs/skill_proposals.md`
- `docs/DESIGN.md`
- `src/agenttalk/install_skills.py`
- `src/agenttalk/doctor.py`
- `src/agenttalk/capacity.py`
- `src/agenttalk/ephemeral.py`
- `src/agenttalk/gates.py`
- `src/agenttalk/supervisor.py`
- Existing devkit and bus skill files
- Dev-2 implementer discussion reply `q-c90f4729c564`
- Reviewer-1 discussion reply `q-c0b6800544c4`
- Reviewer-1 consult reply `consult-r1-skill-design-1302`
- Dev-2 consult reply `consult-dev2-skill-design-1302`

No code or tests were run as verification for this design document beyond read-only repository inspection.
