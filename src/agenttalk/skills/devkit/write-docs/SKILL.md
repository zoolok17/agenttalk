---
name: write-docs
description: >-
  Write or update documentation as a first-class artifact — one Diataxis mode per
  page, audience named, examples runnable, zero drift from the code. Use when writing
  or updating a README, guide, reference, or any user-facing doc, or when a code
  change alters how users build/configure/call the software. Do NOT use for inline
  code comments (use craft-code) or for reviewing existing docs (use review-docs).
reviewed-against: "0.43"
category: production
evidence-profile:
  - production-handoff
---

# write-docs

Documentation is part of the behavioral surface: a change is incomplete while its
docs still teach the old behavior. Write for one reader, in one mode, with examples
that actually run.

## CLASSIFY — before writing
- [ ] Pick exactly **one Diataxis mode** for the page and don't mix them — split and
      cross-link instead:
      - **Tutorial** — learning by doing, hand-held, guaranteed to succeed.
      - **How-to guide** — a recipe to achieve one goal for a competent user.
      - **Reference** — neutral, exhaustive facts that mirror the code's structure.
      - **Explanation** — the why: concepts, trade-offs, background.
- [ ] Name the **audience** (user / integrator / contributor / operator) and their
      single goal at the top. Include only what serves that reader.

## WRITE — prose and examples
- [ ] For tutorials, how-tos, quickstarts, and any example meant to be executed: lead
      with a **minimal runnable** snippet — copy-paste runnable, versions pinned where
      output depends on them, expected output shown. Reference/explanation pages may
      lead with concepts or neutral facts; partial snippets (signatures, config
      fragments, diffs) are fine but must be clearly labeled non-runnable and accurate.
- [ ] Google prose conventions: second person, active voice, present tense,
      sentence-case headings, **conditions before instructions** ("To X, do Y"),
      numbered lists for ordered steps, bullets for sets, code font for code/paths/flags,
      descriptive link text (never "click here"), alt text on images.
- [ ] Optimize for **scanning**: 2–3 sentence paragraphs, one idea per sentence,
      front-load the key info, headings that form a usable table of contents. Reference
      pages mirror the code's structure.
- [ ] **README contract** (for a README): one-line problem statement → minimal example
      → install/quickstart → usage → link to full docs → contributing → support →
      license. Don't make an FAQ the primary documentation.
- [ ] Be precise: no vague claims or unsupported guarantees; state limits and
      prerequisites honestly.

## GATE — accuracy and CI
- [ ] Update docs in the **same change as the code** they describe. Never document
      unshipped behavior. When behavior changes, also update the changelog and any
      API reference / navigation that points at it.
- [ ] Run / confirm the examples produce the documented output.
- [ ] Pass the local docs-as-code gates if present (markdownlint, a prose/terminology
      linter like Vale, a link checker) before declaring done.

## Evidence

Emit the `production-handoff` profile (full rules + bus-validated vs skill-policy: ../_shared/references/evidence.md).

Required fields:

- `changed_files`
- `base_ref`
- `head_ref`
- `summary`
- `tests_referenced`
- `tests_executed`
- `residual_risk`
- `required_review_lenses`
- `evidence`
