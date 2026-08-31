# Verification dossier — PR #132

**Status:** SKELETON. This PR is still moving through its review cadence; the round
history below is illustrative structure only (one worked example round), not the real
history. Fill the remaining rounds from the PR's actual review record once it closes,
following the worked example's shape exactly — do not shorten the format under time
pressure, since the format *is* the audit trail.

**Purpose.** This is the artifact a third-party auditor of *product claims* reads: not
"did the process run" (that's `docs/DEVELOPMENT-METHODOLOGY.md`'s job), but "is each
specific claim this change makes about its own behavior actually true, and how would I
check that myself." Every row below should let a skeptical reader go verify the claim
independently, without having to trust the summary.

**Genericization note.** This PR's real review history references a specific client
codebase used as a validation corpus. Any content copied into this dossier that names
that client, its codebase, or fragments of its source is a hard-constraint violation
(see the repo-wide sweep note in the increment-1 report). The worked example round
below is therefore a **synthetic, illustrative round** — invented to show the required
shape — not a transcription of any real round from this PR's actual history. When the
real history is filled in post-merge, every claim/evidence/finding text must be
re-checked against that same constraint before it is committed, not assumed clean
because the skeleton was.

---

## 1. Claims made

One row per distinct, checkable claim the change asserts about its own behavior.
"Distinct" means: if this claim turned out false, that would be a separate bug from
every other row failing. Group truly interdependent claims under one row rather than
inflating the count with restatements of the same guarantee.

| ID | Claim | Source (spec/design ref) |
|----|-------|---------------------------|
| C-1 | *(e.g., "a malformed input of class X is rejected with a named error, never silently coerced")* | *(design doc section / issue #)* |
| C-2 | *(e.g., "output for a given input is byte-identical across repeated runs")* | |
| C-3 | *(...)* | |

---

## 2. Verification method per claim

For each claim above, the method that actually checked it — named specifically enough
that a reader could tell the difference between "we ran an automated test" and "a
human read the code and it looked right," because those are very different strengths
of evidence and the dossier must not blur them.

| Claim | Method | Executed or referenced-only? |
|-------|--------|-------------------------------|
| C-1 | *(e.g., unit test `test_x_rejects_malformed_class` + mutation-verified: fix reverted, test confirmed to fail, restored)* | Executed |
| C-2 | *(e.g., property-style test running the pipeline twice and diffing output)* | Executed |
| C-3 | *(e.g., cold-read reviewer manually reproduced the claimed behavior against the frozen SHA)* | Executed |

"Referenced-only" is a legitimate value (e.g., a claim inspected by review but not
independently re-run) — it must never be silently upgraded to "executed" in a summary
line. If a claim has no verification method at all, that is itself a finding: an
unverified claim, not a verified one with thin evidence.

---

## 3. Evidence pointers

Concrete, resolvable pointers — not prose descriptions of evidence. Anything that
cannot be resolved (a fixture that was deleted, a CI run that expired) is a defect in
the dossier, flagged and fixed, not left dangling.

| Claim | Fixture / test path | Commit SHA | CI run ID | Reviewer verdict |
|-------|----------------------|------------|-----------|-------------------|
| C-1 | `tests/...::test_x_rejects_malformed_class` | `<sha>` | `<run-id>` | `<reviewer>` — approved, round N |
| C-2 | `tests/...::test_output_is_deterministic` | `<sha>` | `<run-id>` | `<reviewer>` — approved, round N |

---

## 4. Round history

One row per review round. This is the ratification trail: what was found, what was
fixed, what was independently re-confirmed. Severity vocabulary: **blocker** (merge
cannot proceed), **major** (must be fixed or explicitly accepted as a residual before
merge), **note** (recorded, non-blocking).

### Worked example (synthetic — not this PR's real history)

| Round | Reviewer type | Verdict | Findings | Positively re-verified from prior round | Ship SHA |
|-------|---------------|---------|----------|-------------------------------------------|----------|
| 1 | Briefed, first review | REJECTED | 2 blocker, 1 major | n/a (first round) | `aaaa111` |
| 2 | Same reviewer, fix round | APPROVED | 0 blocking | n/a | `bbbb222` |
| 3 | **Unbriefed cold read**, no prior history | REJECTED | 1 blocker (wrong-data class), 3 major, 2 note | n/a (first cold read) | `bbbb222` |
| 4 | Fix round for cold-read findings | — | all 6 items addressed | — | `cccc333` |
| 5 | **Second unbriefed cold read**, different reviewer, no prior history | REJECTED | 1 major, 4 note | Positively confirmed 3 of round 4's fixes hold (named: the blocker fix, and 2 of the 3 major fixes) | `cccc333` |
| 6 | Fix round + **ratification pass** | APPROVED, no blocking findings | round 5's items addressed; ratifier independently re-probed and confirmed all 6 total findings across rounds 3+5 remain fixed | Ratified rounds 3 and 5 as a set | `dddd444` |

Each row's "positively re-verified" column is not optional decoration — a cold read or
ratification pass that reports zero re-verification of prior work is a weaker pass than
one that names what it checked, even if both report the same number of new findings.

---

## 5. Residuals and declared limitations

Anything accepted rather than fixed, with the reasoning and who accepted it. Every row
here should also appear in (or be linkable to) the project's carry ledger — this
section is the PR-scoped view; the ledger is the durable one.

| ID | Residual | Why accepted | Re-measurement plan |
|----|----------|---------------|------------------------|
| R-1 | *(e.g., "performance under load class X is not yet measured")* | *(e.g., "out of scope for this slice; tracked as a follow-up")* | *(what would have to be measured, and when, before this is claimed fixed)* |

---

## 6. Reproduction instructions

Enough for an independent party to redo the verification from nothing but this
document and a checkout at the ship SHA — not "trust us, it works," but "here is
exactly how to find out for yourself."

```text
git checkout <ship-sha>
<install/setup steps, if any beyond the project's standard setup>
<exact command(s) to re-run the automated verification for each claim in §2>
```

For any claim verified by human review rather than automation, name what a
re-reviewer would need: the frozen SHA, the claim text, and nothing else — reproducing
an unbriefed review with a briefing defeats the point (see
`docs/DEVELOPMENT-METHODOLOGY.md` §5).
