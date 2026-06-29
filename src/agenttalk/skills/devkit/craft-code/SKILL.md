---
name: craft-code
description: >-
  Write the smallest, simplest, most readable correct change that matches the
  surrounding codebase. Use when implementing a feature, fixing a bug, or
  refactoring production code. Do NOT use for writing tests (use test-coverage),
  reviewing an existing diff (use review-code), or documentation (use write-docs).
reviewed-against: "0.42"
category: production
evidence-profile:
  - production-handoff
---

# craft-code

Produce code that is **simple** (a maintainer grasps it fast — not merely short),
**efficient** (no avoidable algorithmic cost, no premature micro-optimization),
and **readable**, by making the smallest correct change that fits the codebase.
Work the checklist top to bottom; don't skip the AFTER gate.

## BEFORE — understand and scope
- [ ] Restate the task as the **smallest concrete outcome**. List what is explicitly
      OUT of scope: no speculative features, flags, config knobs, or abstractions
      (YAGNI — every guess has build/delay/carry/repair cost).
- [ ] Read the target file **plus 1–2 neighbors**. Note the naming style,
      error-handling pattern, formatter/linter config, and test layout. Match them —
      inconsistency is the top agent failure mode.
- [ ] Search for code that already does part of the job and **reuse/extend it**
      rather than adding a parallel implementation.
- [ ] Find the project's build/test/lint commands in `AGENTS.md` / `CLAUDE.md`
      (or the README). Don't hardcode or guess them.

## WHILE — write it
- [ ] Make the **minimal correct change**; keep the diff small and reviewable.
      Don't fold unrelated refactors into a fix — land them separately.
- [ ] Prefer **deep modules**: a narrow interface hiding real complexity, over many
      shallow tiny functions. Complexity = dependencies + obscurity; resist
      fragmenting code into noise.
- [ ] **Rule of three**: don't introduce an abstraction until the third real
      duplication. Tolerate small duplication until the variation axes are known.
- [ ] **Don't optimize without measuring.** Write the clear version; if perf matters,
      profile, optimize only the proven hot path, and leave a comment citing the
      measurement.
- [ ] Name for **intent**; scale name length to scope (1-char names only for tiny
      loop indices). If a name needs a comment to explain it, rename it.
- [ ] Comment **WHY**, not what: rationale, trade-offs, gotchas, non-obvious
      constraints. Delete comments that restate the code. No unexplained magic numbers.
- [ ] Handle errors **by expectedness**: fail fast on programmer bugs; exceptions for
      truly exceptional cases; explicit return/Result for expected domain failures.
      Never silently swallow an error; attach context. Design errors out of existence
      where you can.
- [ ] Keep functions to one clear job; treat high cyclomatic complexity as a smell.
      If a function balloons, extract a **genuinely deeper** helper, not shallow shards.
- [ ] **Design it twice**: for any non-trivial implementation, sketch one alternative
      and pick deliberately. Build in small increments and sanity-check each.

## AFTER — verify (mandatory gate, not optional)
- [ ] Re-read your own diff **as a reviewer**: is every line necessary? Delete dead
      code, unused params, leftover scaffolding, debug prints.
- [ ] Run the project **formatter, linter, type-checker, and the relevant tests/build**
      (commands from `AGENTS.md`/`CLAUDE.md`). Confirm it actually compiles and behaves.
- [ ] Do not declare done on unverified or "probably works" reasoning. If you changed
      behavior, hand off to **test-coverage** (tests) and **write-docs** (docs) — a
      behavior change is incomplete while its tests or docs still describe the old one.

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
