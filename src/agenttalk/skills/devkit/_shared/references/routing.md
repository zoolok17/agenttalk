---
reviewed-against: "0.45"
---

# Skill routing index

Roster ROLE = who an agent is in a session; SKILL = a capability it applies to a task. One
agent can apply several skills in one task. This index picks the right capability and guards
against false consensus (more shallow reviews are weaker than one integrated review with
unique evidence).

## Task to skill

| Task | Skill |
|---|---|
| Coordinate a team / route / collect evidence / close | the bus skill agenttalk-lead (do NOT create team-lead) |
| Listen for and handle bus messages | the bus skill agenttalk-listen |
| New production code | craft-code |
| Behavior-preserving cleanup (no behavior change) | refactor-code (Tier 1); until then craft-code with explicit no-behavior-change tests |
| Diagnose + fix a failing local/CI check | fix-ci |
| Decide which tests/lenses a change needs | qa-strategy |
| Low-level / unit / regression / behavior tests | test-coverage |
| Cross-module / CLI-plus-store / filesystem / config / migration tests | test-integration (Tier 1b); until then test-coverage + tester-qa scoped by risk |
| Integrated diff review | review-code |
| Documentation | write-docs |
| Documentation accuracy review (+ run documented commands) | review-docs (docs-QA mode) |
| Failure-path / contract-drift / release-readiness assurance | review-failure-injection / review-contract-drift / review-release-readiness |
| Release / milestone multi-lens close | system-review-protocol |

## Core precedence

1. Bus/team coordination uses agenttalk-lead/-listen/-handoff/-send/-sk-loop. Never team-lead.
2. New production code uses craft-code; behavior-preserving cleanup uses refactor-code
   (Tier 1; until it exists, craft-code with explicit no-behavior-change tests); if behavior
   changes it is craft-code.
3. Architecture / security / performance are SUB-LENSES under review-code unless routed as
   explicit specialist work with distinct evidence (not a separate skill in Tier 0/1).
4. Failure-path, contract-drift, release-readiness, and docs reviews use the existing
   specialist skills when their trigger is exact.
5. When the main task is deciding QA/review coverage, use qa-strategy before invoking
   test-coverage, test-integration, review-code, or specialist review lenses; qa-strategy
   emits a plan only - it does not write/run tests and does not replace obvious required tests.
6. When a concrete command/check/job is ALREADY failing, use fix-ci (read the full
   log/output first, classify the root cause, smallest fix); use craft-code for new
   production behavior when there is no failing check yet; use qa-strategy when deciding
   which checks/lenses are needed (plan-only - it does not diagnose logs or edit code).

## Negative triggers (do NOT)

- Do NOT call a performance review without a workload, budget, dataset, complexity change, or
  measurement plan.
- Do NOT call a security review for cosmetic changes that do not touch auth, input,
  filesystem/process/env, dependency, sandbox, or secret surfaces.
- Do NOT request fresh-context review merely to accumulate approvals.
- Do NOT require fresh-context review when ephemeral reviewers are disabled or no supervisor
  is running; record unavailable and use standing reviewers.
- Do NOT use a separate docs-QA skill; use review-docs in docs-QA mode.
- Do NOT use qa-strategy to avoid writing or running obvious tests.
- Do NOT use fix-ci to guess at a failure without reading the failing log/output.
- Do NOT let duplicated green evidence count as independent coverage: two reviewers citing the
  same run + scope corroborate ONE evidence item, they are not two proofs.

## Capacity guidance (advisory, never a gate)

- Classify optional lenses/checks as cheap, moderate, or expensive.
- Prefer cheap local evidence before launching fresh reviewers.
- Use fresh reviewers deliberately when risk justifies the token cost.
- Capacity is advisory and NEVER blocks a required safety review (run it, record the risk).

## Dual review modes

- Context-preserving (standing roster) reviewers: ordinary diff review, contract drift,
  continuity, verifying a fix resolves a prior finding. Strength: history/memory. Risk:
  context becomes bias.
- Fresh-context ad-hoc reviewers (one-shot, evidence-only, via request-launch): high-risk
  final-SHA verification; changes touching gate/close/signoff/authority/lead-loop/relay/
  persistence/security/evidence-schema/routing/install behavior; release-blocker fixes that
  change the reviewed surface; large final diffs where continuity may bias; operator/lead
  request. Availability preconditions: supervisor running, ephemeral_reviewers.enabled=true,
  allowed profile/skill/role/groups, authorized requester, full revision + caps. If
  unavailable: record it and use standing reviewers; do NOT block GO. A fresh approval is
  EVIDENCE ONLY (evidence_only=true, signoff_eligible=false), never a close signoff; a fresh
  rejection is a counter to disposition. Prefer one strong fresh review with a distinct
  question over several duplicate green reviews.

## Lead GO checklist

- Every risk class in the close inventory has an accountable owner.
- Every review/QA evidence item names an exact ref + scope.
- Green evidence is unique enough to add coverage (different scope, risk, tool/run, or
  adversarial question) - not a repeat of another green.
- Rejections, needs-info results, and malformed fresh-review outputs are dispositioned.
- No approval rests only on another approval.
