# Specification Quality Checklist: Review Hardening (agenttalk 0.18.0)

**Purpose**: Validate specification completeness and quality before planning
**Created**: 2026-06-07
**Feature**: [spec.md](../spec.md)

## Content Quality

- [x] No implementation details (languages, frameworks, APIs) — code symbols named are the subjects of the bugs, not tech choices; HOW is deferred to plan
- [x] Focused on user value (a bus that doesn't crash, history that doesn't vanish, a guardrail against a known footgun)
- [x] Written for stakeholders — each FR maps to a named user scenario
- [x] All mandatory sections completed

## Requirement Completeness

- [x] No [NEEDS CLARIFICATION] markers remain
- [x] Requirements are testable and unambiguous
- [x] Requirement types separated (FR / NFR / C)
- [x] IDs unique across FR-### / NFR-### / C-###
- [x] All requirement rows have a Status
- [x] Non-functional requirements have measurable thresholds (CI matrix enumerated; probe ≤1 read + ≤1 O(1) syscall; no shape change)
- [x] Success criteria measurable (7, each tied to a verifiable repro)
- [x] Success criteria technology-agnostic
- [x] All acceptance scenarios defined (6 scenarios, 1 per finding/feature)
- [x] Edge cases identified (poison file, zzzz id, all-retired resume, dead/stale marker, pre-0.18 marker format, clock-skew exclusion)
- [x] Scope bounded (Out of Scope: locking, clock-skew fix, history mutation, render rewrite, complete registry)
- [x] Dependencies and assumptions identified

## Feature Readiness

- [x] All FRs have clear acceptance criteria
- [x] User scenarios cover the primary flows
- [x] Feature meets the measurable Success Criteria
- [x] No implementation detail leaks beyond naming the defective code paths

## Notes

- Two scope-honesty constraints are load-bearing and explicit: C-008 (id
  validation does NOT fix clock skew) and C-006 (warn, don't enforce). Both
  were Codex-accepted in the design proposal; the spec must not let a later
  artifact overclaim either.
- All six items have a confirmed reproduction (4 dynamically verified by
  Claude, the id-shape attack verified, the signature DoS verified); the
  regressions are therefore non-speculative.
