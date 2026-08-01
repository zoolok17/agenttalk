# Panel disposition: 87-A classifier and wrapper authority

Design under review: four `docs/DESIGN-87A-*.md` documents at `49318ff`, reconciled against **merged**
`#120` at `587e7c1`. Three independent lenses, nine questions.
**Verdict: REVISE. Do not implement.** One release blocker.

| Q | Lens | Disposition | Class |
|---|---|---|---|
| 1 | A | **HOLDS** | closure-dependent teardown cannot proceed unproven; survived attack |
| 2 | A | **HOLDS** | authority survives the measured bistable classifier |
| 3 | A | **HOLDS** | effect primitive holds on wait-timeout, open-failure, exit-between-open-and-wait |
| 4 | B | **BREAKS** | **RELEASE BLOCKER** — POSIX targets can never be killed |
| 5 | B | (see B) | dependency pinning on #115 + closure successor |
| 6 | B | (see B) | late-descendant window, POSIX token form |
| 7 | C | **HOLDS** | four documents agree after the reconciliation pass |
| 8 | C | **HOLDS** (2 residuals) | operator-facing honesty about permanent POLICY_HELD |
| 9 | C | **BREAKS** | module digest item 20 is circular as written |

Lens A returned `needs-info`; lenses B and C returned `rejected`. Lens B set `release_blocker=true`.

## What survives, and it is the important part

**The authority core holds under attack.** Q1, Q2 and Q3 all held. Q2 matters most: I instructed lens A
to treat the field-measured bistability as the NORMAL case — the classifier flipped state on 16 of 17
polls while an agent worked normally — rather than as an edge case, and the design's authority still
held with two adjacent polls disagreeing at every decision point that consumes classification. That was
the central worry of task #87 and it is answered.

Lens B also independently **confirmed all six** of the merged-#120 claims dev-4 corrected, each with
file:line at `587e7c1`, and inspected the matching direction controls. The reconciliation was real.

## THE RELEASE BLOCKER (Q4) — POSIX targets are unkillable

`DESIGN-87A-owned-childless-wrapper-authority.md:530-539` states that a non-Windows owned target
carries `linux:<boot_id>:<start_ticks>` in `start`, omits `start_filetime`, and **reaches the existing
sole kill primitive**. Merged #120 does accept that identity form and can emit such targets
(`supervisor.py:2087-2115,2444-2470,3285-3300`).

But `Stop-Tree` explicitly **skips every `source=owned_process_tree` target whose `start_filetime` is
absent** (`supervisor.py:8900-8938`). Project a valid Linux-token target exactly as the design
specifies: `$exact` is empty, `source` is `owned_process_tree`, `Stop-Tree` executes `continue`, and
**no termination is ever attempted**. The post-action path can then only rediscover a residual and
HOLD.

So the document credits #120 with a sole-kill adapter that **does not exist for its own declared
non-Windows target form**. On Linux and macOS this design cannot kill anything, and it will hold
forever instead.

Two acceptable resolutions, and the choice must be explicit:
1. Declare POSIX `CAPABILITY_UNAVAILABLE` and delete the mapping — honest, and consistent with M5.
2. Dependency-track a real exact-token kill adapter and gate the mapping on it.

**Do not resolve this by loosening `Stop-Tree`'s FILETIME requirement.** That requirement is exactly
what #120's review round installed to stop a recycled PID being killed in place of the original.

### Why this is the third instance of one pattern

This document has now credited `#120` with capability it does not have **three times**: an
acquire/reconcile/release closure contract that never existed, a 256-target cap that never existed,
and now a POSIX kill adapter that is skipped in code. The first two traced to my task list wrongly
recording #120 as complete. This one survived a reconciliation pass done specifically to fix that
class. The lesson for the revision: **a claim that merged code does X must cite the line where X
happens, not the line where X is accepted as input.** #120 accepts the Linux token; it never acts on
it.

## Q9 BREAKS — the digest check is circular as written

Lens C recomputed all seven module digest vectors byte-exact, ran an independent `CanonicalJsonV1`
round-trip (7/7), verified 5/5 chain links, cross-checked FILETIME exactly, and diffed field sets
against the prose schemas (3/3). The **fixture itself is sound**; item 20 as written is circular — the
expected value is not independent of the artifact it validates, so an error in the source would move
both together and the check would still pass.

This is the same class already banked on this project from the #31 close-provenance work. Make item
20's expected value derive independently, or state plainly that it is a change-detector rather than a
correctness check. Do not leave it reading as integrity assurance.

## Q8 — honest, with two residuals

The operator-facing story about permanent `POLICY_HELD` under Option A is told where an operator would
read it. Two residuals recorded by lens C; fold them, they are not blockers.

## Carried residual risks (lens B)

POSIX late-descendant false-clear; equal-tick misattribution; the missing exact-token kill adapter
(the Q4 blocker). Lens B's full trace is at `D:\tmp\agenttalk-review-rq-82688195ff5d.md`.

## Required next steps

1. Fix Q4 by choosing resolution 1 or 2 explicitly. This is the release blocker.
2. Fix Q9's circularity, or downgrade its claim to change-detection.
3. Fold Q8's two residuals.
4. Do **not** re-litigate M5 (operator-decided, Option A, absolute) or Q1/Q2/Q3 (held under attack).
5. Re-panel scope: Q4 and Q9 only. The authority core does not need re-review.

Nothing here is on a release clock. #120 is merged; this design is not implemented and nothing
depends on it shipping today.
