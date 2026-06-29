---
name: review-code
description: >-
  Review a code diff for code health in priority order, grounding every finding in
  the actual code and severity-tagging each one. Use when asked to review a
  PR/diff/change or to self-review code before merging. Do NOT use for writing new
  code (use craft-code), reviewing documentation (use review-docs), or general code
  questions unrelated to a specific diff.
reviewed-against: "0.42"
category: assurance
evidence-profile:
  - review-result
---

# review-code

Decide whether the change improves the codebase's health on net — not whether it is
perfect. Verify every claim against the real code; never review on "this looks right".

## VERDICT — the bar
- [ ] Conclude **APPROVE / APPROVE-WITH-NITS / REQUEST-CHANGES**. Approve if the change
      improves code health on net; do **not** withhold approval merely because the code
      isn't perfect, and don't self-loop on nits.

## WALK — findings in priority order
Spend effort top-down; a naming nit on broken data flow is wasted effort.
1. [ ] **Design / architecture** — does this change belong here? Right layer, right approach?
2. [ ] **Correctness / functionality** — does it do what it intends, including edge cases?
3. [ ] **Complexity / over-engineering** — simpler way? speculative generality to cut?
4. [ ] **Tests** — present for the new behavior, asserting it, and would they FAIL if it broke?
5. [ ] **Naming** → 6. **Comments (why, not what)** → 7. **Style/consistency** → 8. **Docs**.

## VERIFY — adversarially (the #1 AI-review fix)
- [ ] Read **every changed line and its surrounding context** — bugs hide in
      unchanged-but-affected code; never review hunks in isolation.
- [ ] For every correctness/security claim: **quote the exact lines**, confirm the
      referenced symbols/APIs **actually exist**, trace the data flow, and run the tests
      or a minimal repro. No finding may rest on "this is probably…". Don't trust
      fluent-looking code.
- [ ] **Security pass:** if the change touches auth, input handling, data access,
      secrets/crypto, file/path handling, deserialization, or dependencies, run the
      OWASP checklist in [`references/security.md`](references/security.md) and tag
      security findings high by default. AI-written code carries security bugs at a
      materially higher rate than human-written code — don't skip this because the
      functional behavior "works".
- [ ] **Targeted performance** on hot paths only: N+1 queries, missing batching,
      unbounded caches/queues, leaked handles/connections across success AND error
      paths, obviously wrong complexity. Don't flag speculative micro-optimizations.
- [ ] Confirm **docs/READMEs/comments** were updated when the change alters how users
      build, test, configure, or call the code (else it's a finding → review-docs).

## REPORT — constructively, severity-tagged
- [ ] Tag every finding `[blocker] / [major] / [minor] / [nit]` as a Conventional
      Comment (`label (decoration): subject` + one-line reason). Mark anything
      non-mandatory explicitly **Nit/Optional/non-blocking** so the author can triage.
- [ ] Review the **change, not the author**: comment on the code, give the reasoning,
      resolve disputes with technical facts / the style guide. Point out the problem
      and let the author choose the fix. Mention a genuine positive when it helps the
      author keep a good pattern — optional, don't force praise into every review.
- [ ] Keep scope sane: defect-detection collapses past ~400 LOC; review large diffs in
      focused chunks.
- [ ] End with a **prioritized summary**: blockers, then majors, then minors/nits — so
      the reader sees what gates merge vs. what is optional.
