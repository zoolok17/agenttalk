# Verification dossier — PR #132

**Status:** COMPLETE (post-merge). PR #132 merged 2026-09-07 at squash commit
`9b8b385d5e1d81e8c8905da9bbeb6171a9a34861`, head `ed3e45a6dce7e27674b46ffe400b41257bd008e6`.

**Purpose.** This is the artifact a third-party auditor of *product claims* reads: not
"did the process run" (that's `docs/DEVELOPMENT-METHODOLOGY.md`'s job), but "is each
specific claim this change makes about its own behavior actually true, and how would I
check that myself." Every row below is sourced from a resolvable PR/issue/commit
pointer a skeptical reader can open directly — never a paraphrase asked to be trusted
on its own.

**This is a map, not a copy.** The full round-by-round record lives on PR #132 itself
(body + 19 issue-level comments + 225 inline review comments) and in
[`docs/COMPREHENSION-LIMITATIONS.md`](../COMPREHENSION-LIMITATIONS.md) (the PR's own
close-out honesty artifact, committed at `5b3f7a33`). This dossier indexes and cites
those primary records rather than re-deriving their content; where a claim's full
enumeration already exists in `COMPREHENSION-LIMITATIONS.md`, this document points at
it instead of duplicating its tables.

**On precision of the counts below.** Several summary figures commonly used in
conversation about this PR (a round count, a cold-read count, an audit count, a total
defect count) do not correspond to a single stated number in the citable record, or
mean something different from their casual gloss. Per this dossier's own §2 rule
("referenced-only... must never be silently upgraded"), each such figure below is
reported as what is actually verifiable, with the citable basis shown, rather than
rounded or restated to match a tidier round number. Where a commonly-repeated framing
does not hold up against the record, that is noted explicitly rather than quietly
corrected without comment.

---

## 1. Claims made

| ID | Claim | Source |
|----|-------|--------|
| C-1 | The platform layer (privacy/offline enforcement, artifact envelopes, storage, CLI/lock/staging lifecycle) behaves correctly under the design's own invariants. | PR #132 body "Summary"; `docs/DESIGN-55-comprehension-plane.md` |
| C-2 | Every extraction-layer (Java/pom.xml/web.xml adapter) limitation is enumerated and machine-published, never silently absent — and every enumerated gap is triaged by the direction its error could take (a false positive/wrong fact vs. a safe miss vs. cosmetic). | `docs/COMPREHENSION-LIMITATIONS.md` (full register) |
| C-3 | Network isolation (the design's "Privacy and offline enforcement" requirement) is independently enforced and CI-verified on both Linux (network-namespace denial) and Windows (firewall-rule denial). | `.github/workflows/comprehension-network-deny.yml`; PR #132 body line 40 (first green run at `18692d0`) |
| C-4 | `whole_scope_fingerprint` deliberately excludes generated/vendor/binary content from its inputs this slice — a declared scope narrowing, not an oversight — and any freshness verdict built on it must not ship before PR-C either widens the fingerprint's inputs or formally ratifies the narrower scope. | PR #132 body, "Named decisions and residuals" (PR-C entry criterion); `docs/COMPREHENSION-LIMITATIONS.md` S3 |
| C-5 | No relation or entry-point shape the adapter cannot model is ever coerced into a healthy-looking generic result — each is a named, enumerated, closed-vocabulary "unsupported shape" instead. | `docs/COMPREHENSION-LIMITATIONS.md` §2 (U1–U3, ~20 named shapes) |
| C-6 | The 223-comment automated cross-vendor review sweep produced exactly 3 findings capable of publishing a wrong fact with confidence (bucket A); all 3 were fixed in this same close-out, each with a locking regression test. | `docs/COMPREHENSION-LIMITATIONS.md` §3a (A1–A3); fix commits `74a21f9`, `f9e4be6`, and MICRO-NOD 50b F8 (folded into head `ed3e45a6`) |
| C-7 | Every other sweep finding (28 of 31: 20 safe-direction, 8 polish) either cannot publish a false fact, or is cosmetic-only, and is registered rather than fixed, with its own disposition. | `docs/COMPREHENSION-LIMITATIONS.md` §3b–§3c |
| C-8 | The regex/`.find()`-based extractor's own value-extraction call sites were structurally audited (nesting-aware vs. flat) as a distinct pass from the comment sweep, producing its own blockers. | PR #132 comment [5537686689](https://github.com/zoolok17/agenttalk/pull/132#issuecomment-5537686689) (Round 49 "extractor structural audit") |

---

## 2. Verification method per claim

| Claim | Method | Executed or referenced-only? |
|-------|--------|-------------------------------|
| C-1 | ~50 major review rounds (initial review through Round 50 + MICRO-NOD 50b) each landing a green CI matrix at its ship SHA; unbriefed cold reads recur through the arc, with cited ordinals reaching at least the 44th (see §4 for exactly what is and isn't verifiable about that count). | Executed |
| C-2 | Register compiled from three sources per the doc's own §-intro: (1) in-code `NAMED LIMIT`/`*_CAVEAT` constants, (2) the adapter's closed-vocabulary tables, (3) the 223-comment sweep, triaged by direction-of-error. | Executed |
| C-3 | `comprehension-network-deny.yml`'s `linux` job (`unshare --net`) and `windows` job (firewall rule), both gated on an explicit authorization env var, both required checks. | Executed (CI-gated) |
| C-4 | Design-review ratification of the narrower scope as an explicit, named PR-C entry criterion — not yet independently re-tested (PR-C has not landed). | Referenced-only until PR-C |
| C-5 | Static enumeration in code (`UNSUPPORTED_RELATIONS`, `UNSUPPORTED_INVOKE_SHAPES`, `UNSUPPORTED_ENTRY_POINT_SHAPES`), each shape exercised by the adapter's own test suite. | Executed |
| C-6 | Each bucket-A finding got a purpose-built regression test proving the wrong-data path, mutation-verified (fix reverted, test confirmed to fail, restored). | Executed |
| C-7 | Sweep-classified by reviewer-3; independently re-probed by reviewer-3's own separate confirmation-delta audit (one row, B11, honestly marked as attempted-but-not-reproduced — see `docs/COMPREHENSION-LIMITATIONS.md` footnote at that row). | Executed, with one named exception (B11) |
| C-8 | Full audit posted as PR comment `5537686689`; produced blockers B1–B3 and finding 49-M6, all addressed in the Round-49 fix cycle. | Executed |

---

## 3. Evidence pointers

| Claim | Fixture / test path | Commit SHA | PR/issue reference |
|-------|----------------------|------------|----------------------|
| C-1 | `tests/test_comprehension_*.py` (full suite; see PR body "Test scope and count") | head `ed3e45a6`, squash-merge `9b8b385d` | PR [#132](https://github.com/zoolok17/agenttalk/pull/132) |
| C-2 | `tests/test_comprehension_readiness_artifact.py`, `tests/test_comprehension_discovery.py`, `tests/test_comprehension_features_artifact.py`, `tests/test_comprehension_modules_artifact.py`, `tests/test_comprehension_scan_pipeline.py`, `tests/test_comprehension_adapter_java.py` | `5b3f7a33` (register itself) | `docs/COMPREHENSION-LIMITATIONS.md` |
| C-3 | `.github/workflows/comprehension-network-deny.yml` (`linux`, `windows` jobs) | first green: `18692d0` | PR #132 body line 40 |
| C-6 (A1) | `tests/test_comprehension_scan_pipeline.py` (two new F6 tests) | `74a21f9` | `docs/COMPREHENSION-LIMITATIONS.md` A1 |
| C-6 (A2) | `tests/test_comprehension_adapter_java.py` (three new F7 tests) | `f9e4be6` | `docs/COMPREHENSION-LIMITATIONS.md` A2 |
| C-6 (A3) | `tests/test_comprehension_adapter_java.py` (three new F8 tests) + one end-to-end F8 test in `tests/test_comprehension_scan_pipeline.py` | MICRO-NOD 50b F8, folded into head `ed3e45a6` | `docs/COMPREHENSION-LIMITATIONS.md` A3; re-bucketed from B by reviewer-3's triage-audit, comment [3884350384](https://github.com/zoolok17/agenttalk/pull/132#discussion_r3884350384) |
| C-7, sweep summary | — | — | comment [5563287180](https://github.com/zoolok17/agenttalk/pull/132#issuecomment-5563287180) (summary) + 4 enumeration parts: [5563288266](https://github.com/zoolok17/agenttalk/pull/132#issuecomment-5563288266), [5563288816](https://github.com/zoolok17/agenttalk/pull/132#issuecomment-5563288816), [5563289384](https://github.com/zoolok17/agenttalk/pull/132#issuecomment-5563289384), [5563289905](https://github.com/zoolok17/agenttalk/pull/132#issuecomment-5563289905) |
| C-8 | `src/agenttalk/comprehension/adapters/java.py` | Round 49 fix cycle, ship `90ffa17` | comment [5537686689](https://github.com/zoolok17/agenttalk/pull/132#issuecomment-5537686689) |

---

## 4. Round history (condensed, with the counts corrected against the citable record)

The PR body itself carries the full round-by-round detail from the initial review
through "Micro-round 49" (`65ab95c` → `c582298`) before hitting GitHub's PR-body length
limit — stated explicitly in comment
[5537686689](https://github.com/zoolok17/agenttalk/pull/132#issuecomment-5537686689).
Every round after that point (Round 49 clusters 2–4, micro-rounds 49b/49c, Round 50
clusters 0–5, micro-round 50c, and MICRO-NOD 50b F1–F8) is recorded as PR comments
only. What follows is the condensed shape; the exact per-round findings text is on the
PR itself, not reproduced here.

| Phase | Verdict pattern | Ship SHA | Note |
|-------|------------------|----------|------|
| Initial review | REJECTED (3 blockers, 2 findings, process rulings) | `6656c93` | |
| Fix rounds 1–2 | APPROVED, no blocking findings | `21386a0` | |
| CI-round fix | — | `18692d0` | first green `comprehension-network-deny` run (C-3) |
| **First unbriefed cold read** (no #55 history) | REJECTED — 3 blockers, 11 major (body enumerated M1–M12; the "11" was a summary-line miscount, corrected in the same comment), 6 notes | vs. `18692d0` | fix round 3 → `740917c`, all 17 MUST-FIX + 4 SHOULD-FIX addressed |
| Cold reads 2–43 (fresh reviewer each time, no prior-round briefing) | Alternating REJECTED/APPROVED, each round's fixes re-verified by the next | rounds 4 through ~49 | condensed detail on PR body / comments, not reproduced here |
| **Round 49** — includes the extractor structural audit (C-8) | 3 blockers + 5 majors + 2 minors + 8 completeness + 6 polish = 24 items total (explicitly summed in comment [5540046690](https://github.com/zoolok17/agenttalk/pull/132#issuecomment-5540046690)) | `90ffa17` | **44th cold read** cited by ordinal in comment [5542777846](https://github.com/zoolok17/agenttalk/pull/132#issuecomment-5542777846): *"Reviewer-3's forty-fourth cold read found the worst finding…"* — the highest ordinal found anywhere in the record |
| Round 50, clusters 0–5 | — | `f48aa38` | |
| Micro-round 50c | — | `8ff538b9` | |
| **223-comment cross-vendor sweep** | 31 distinct findings (3 bucket A, 20 bucket B, 8 bucket C) — see `docs/COMPREHENSION-LIMITATIONS.md` for the full table | summary at `5b3f7a33`'s tip | comment [5563287180](https://github.com/zoolok17/agenttalk/pull/132#issuecomment-5563287180) + 4 parts |
| **MICRO-NOD 50b, F1–F8** | all 3 wrong-data-capable sweep findings fixed (F6/F7/F8); F1–F5 close remaining items | head `ed3e45a6` | commits `74a21f9` (F6), `f9e4be6` (F7), F8 folded into `ed3e45a6` |
| **Merge** | squash | `9b8b385d`, parent `adc5b34f` (unrelated PR #140, master tip at merge time) | |

**On "44 cold reads across two vendors (Windows + Linux)."** This framing does not
match the citable record and should not be repeated as if it were a verified fact.
What *is* verifiable: cold-read ordinals reach at least **44** (the highest cited,
in comment `5542777846`) — not necessarily exactly 44 total, since no comment states a
final count. **"Two vendors"** refers to two distinct *review mechanisms*, not two
operating systems: the automated Codex-bot reviewer (source of the 225 inline review
comments and the 223-comment sweep — `docs/COMPREHENSION-LIMITATIONS.md` calls this
explicitly "the cross-vendor review's 223-comment sweep") versus the unbriefed
cold-read/reviewer-3 process. Confirmed via `gh api repos/zoolok17/agenttalk/pulls/132/reviews`:
every formal PR review is posted by the Codex Review GitHub App. **Windows and Linux**
are real, but describe something else entirely — the dual-*platform* CI requirement
for the network-deny privacy gate (C-3), not the cold-read count.

**On "3 structural audits."** No single comment or section groups exactly three items
under that name. What exists: one comment explicitly titled a **structural audit** —
the Round 49 extractor audit (C-8, comment `5537686689`) — plus two of reviewer-3's own
named audit passes over the 223-comment sweep, the **triage-audit** (re-bucketed A3
from bucket B, comment `3884350384`) and the **confirmation-delta audit** (independently
re-probed sweep findings, surfacing the R1/R2 residuals and the B11 non-reproduction
note in `docs/COMPREHENSION-LIMITATIONS.md`). Three audit-shaped activities exist; they
are not a single named group of "3 structural audits" anywhere in the record, so they
are listed here individually rather than force-fit under that label.

**On "~300 verified-fixed defects, each mutation-verified."** No single stated total
exists. Concrete, citable per-round counts: the initial cold read alone was 17
MUST-FIX + 4 SHOULD-FIX (21); Round 49 alone was 24 (comment `5540046690`); the sweep
contributed 31 more distinct findings. Summed loosely across ~50 rounds, a total in the
low-to-mid hundreds is a plausible order of magnitude, but is not independently
verifiable from any single record and should not be cited as a precise figure.

---

## 5. Decision record

**Primary record: this document.** Unlike every other citation in this dossier, the
decision below has no upstream GitHub artifact to chase — it was made in the
operator's direct steering channel, not posted as a PR/issue comment. This entry
*is* the record, not a pointer to one. A reviewer who needs more than what is written
here has nowhere else to look; that is stated plainly rather than implied by a
missing citation.

| Field | Value |
|-------|-------|
| Date | 2026-09-06 |
| Decision | End the unbounded adversarial-read loop. Fix the external cross-vendor read's findings, plus one confirmation review. Merge with declared limits (the limitations register, §6 below) and the incremental extraction replacement committed as the next slice (issue [#141](https://github.com/zoolok17/agenttalk/issues/141)). |
| Alternatives considered | Continue the unbounded read loop indefinitely; replace the extraction layer before merging (block the merge on issue #141's work landing first). |
| Decided by | Project steering (recorded here by the lead; no individual names). |

This decision is the reason the round history in §4 has an end at all — the process
described in §7 and §8 of `docs/DEVELOPMENT-METHODOLOGY.md` (confirmation vs. gate;
direction-of-error triage) does not itself specify a stopping point for an unbounded
review loop. That stopping point is a judgment call, made once, here, by the party
that owns the risk — not a mechanical output of the review process itself.

---

## 6. Residuals and declared limitations

The full, current register lives at
[`docs/COMPREHENSION-LIMITATIONS.md`](../COMPREHENSION-LIMITATIONS.md) — this dossier
does not duplicate it. Summary: 8 pre-existing structural/provenance caveats (§1), 3
closed-vocabulary unsupported-shape tables (§2, ~20 named shapes), and 31 distinct
sweep findings (§3: 3 fixed, 20 safe-direction registered, 8 polish registered).

**The merge-with-declared-limits decision.** PR #132 merged with 28 of the 31 sweep
findings still open (registered, not fixed) plus the 8 pre-existing structural caveats
and 3 unsupported-shape tables, all declared rather than resolved — the shape commonly
described as "merge with declared limits and a committed replacement plan." That
replacement plan is issue [#141](https://github.com/zoolok17/agenttalk/issues/141): a
5-step incremental extraction-layer replacement (real XML parser → tree-sitter Java →
an explicit binding stage → a measured old-vs-new comparison), seeded by 7 regression
fixtures from MICRO-NOD 50b F1–F7, and explicitly citing "~50 rounds of adversarial
review plus a cross-vendor sweep of 223 review comments on PR #132."

**What this dossier could not verify.** No PR comment, issue, or review on GitHub
frames this as a choice between a named "option A" and "option B," or attributes the
decision to "the operator." The full PR comment history was searched (all 19 issue-level
comments, all 225 inline review comments' headers, all formal reviews) with no such
framing found; the gap between the last PR comment (2026-09-07T00:15:54Z) and the
merge event (2026-09-07T05:18:50Z) contains no additional GitHub-visible comment. If
this decision was made, it happened over a channel this dossier's citation standard
(PR/issue/commit references a third-party reviewer can chase) cannot resolve — recorded
here as a gap, not silently omitted or asserted anyway.

**Follow-on issues surfaced by this arc:**

| Issue | Title | Tracks |
|-------|-------|--------|
| [#141](https://github.com/zoolok17/agenttalk/issues/141) | comprehension extraction replacement - incremental | The committed replacement plan for every "Yes"/"Partially" row in `docs/COMPREHENSION-LIMITATIONS.md`'s "Retired by the parser replacement?" column. |
| [#139](https://github.com/zoolok17/agenttalk/issues/139) | `wrap --loop`: self-continuation for unfinished multi-turn work + supervisor owes-a-reply detection | A wrapper/process gap this arc's own length exposed: an agent that ends a turn declaring unfinished work idles silently without an inbound trigger. Field instance cited: a multi-cluster dispatch stalled 55 minutes mid-sequence. Not a PR #132 defect — a tooling residual the arc's own scale surfaced. |
| [#138](https://github.com/zoolok17/agenttalk/issues/138) | `privacy.run_privacy_preflight`: git subprocess `GIT_TIMEOUT_SECONDS=2.0` flakes under starved CI runner | CI flake hit during this arc (Round 42 tip `bde91d6`, `windows/3.11` leg) — root-caused to a timeout, not an infra-silent failure; verified via full pytest output showing every other leg/test green. |
| [#142](https://github.com/zoolok17/agenttalk/issues/142) | `tests/test_lanes.py` concurrent delivery flakes on Windows wheel leg: `Errno 13` sharing-violation race | A second, distinct CI flake (different root cause — a Windows file-sharing race, not a git timeout) found on an unrelated version-bump run; explicitly distinguished from #138 rather than conflated with it. |

---

## 7. Reproduction instructions

```text
git checkout 9b8b385d5e1d81e8c8905da9bbeb6171a9a34861   # squash-merge commit
python3.10 -I -m pip install -r dev-gate-requirements.txt
python3.14 -I -m pip install -r dev-gate-requirements.txt
agenttalk dev-gate --profile release --python 3.10=<path> --python 3.14=<path>
pytest tests/test_comprehension_*.py -v
```

For the network-deny privacy gate specifically (C-3), the two required CI jobs are
`.github/workflows/comprehension-network-deny.yml`'s `linux` and `windows` jobs — both
gated on `AGENTTALK_AUTHORIZE_NETWORK_DENY_TEST=1`; re-running them requires a CI
dispatch, not a local command (the isolation mechanism itself — a network namespace on
Linux, a dedicated firewall rule on Windows — is not meaningfully reproducible in an
arbitrary local shell).

For any claim verified by human/cold review rather than automation (§2's
"referenced-only" or "Executed" rows citing a reviewer), a re-reviewer needs: the
frozen head SHA (`ed3e45a6`), the claim text from §1, and nothing else — reproducing an
unbriefed review with a briefing defeats the point (see
`docs/DEVELOPMENT-METHODOLOGY.md` §5).
