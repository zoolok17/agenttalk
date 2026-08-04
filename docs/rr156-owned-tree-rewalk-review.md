Adversarial review, dev-6's Design #156 (bus message `20260802-180208-966119-VgxO`), read in full.
Cross-checked every load-bearing claim against the actual `src/agenttalk/supervisor.py` code
(`_owned_process_tree`, `_recorded_process_identity_state`, `_unverified_owned_process_tree`,
`_has_exact_start_identity`, `_filetime_of`, and the round-12 BFS precedent), not the design's prose,
per the request's explicit instruction on item 4. Also read dev-4's `#81` invariant note
(`1a15e95`, `docs/DESIGN-81-recovery-actually-recovers-invariant.md`) for the collision check.

===========================================================================
1. IS RECONCILIATION MONOTONIC?
===========================================================================

HOLDS, by construction. Reconciliation's only two possible actions per entry are "no action" (the
two safe buckets) or `mark_rejected(...)` (the third bucket) — there is no code path, in the design
as specified, where reconciliation itself constructs a new `owned` entry, grants `prior_authority`,
or otherwise increases what the live-edge walk would produce on its own. `prior_authority` stays
gated on `status == "complete"` and reconciliation never touches that gate directly — it can only
ever prevent `complete` from being reached (by forcing `rejected_count > 0`), never grant it by
itself; `complete` still requires the pre-existing, unchanged bar (clean fresh-walk admission) as a
precondition. I did not find a path where reconciliation is *more* permissive than a plain no-prior
walk. (Finding 4 below is a different failure mode — not reconciliation granting new authority, but
reconciliation's own "is this the same identity" check having a documented weak input.)

===========================================================================
2. IS THE ROUND-12 SHAPE REALLY ABSENT?
===========================================================================

HOLDS. I read the actual round-12 hazard in the code (`supervisor.py:4648-4663`, the BFS discovery
loop building `targets_by_pid` for absence-confirmation kill-target reconstruction) to get the real
shape, not dev-6's paraphrase of it: round 12's bug was using a *synthetic row* — reconstructed from
a prior record's own `(pid, start)` with no live re-verification — as a `_strict_child_edge` **parent**
for further BFS discovery of a *different*, independent pid's children. The comment there is explicit
about why that's dangerous: "If this pid has been RECYCLED... the synthetic row's stale start time is
almost always EARLIER than any of the replacement's own children — the ordering check that exists to
REJECT an unrelated descendant would instead PASS every one of them, misattributing a recycled
process's own children as ours." That is a two-hop shape: identity A (unverified) vouches for
discovering B, and B is checked against A's stale claim rather than independently.

`_recorded_process_identity_state`, which reconciliation would reuse, takes exactly one prior entry's
`(expected_start, expected_filetime)` and looks up that *same* pid directly in the current snapshot
index — there is no BFS, no child discovery, and no entry's classification is used as input to
classifying a different entry. Every reconciliation check is a flat, independent, one-hop lookup.
This is a structurally different shape from round 12's chained-parent hazard, and dev-6's claim holds
against the actual code precedent, not just against its own restated description of it.

===========================================================================
3. THE "NOT STUCK" TIER — CONFIRMED NOT MISREAD DOWNSTREAM
===========================================================================

HOLDS. Every occurrence of `refreshed_at` in `supervisor.py` (lines 214, 2582-2584, 2747, 3010, 3049,
3058, 3098, 3729, 3763) is either a write or a schema-shape validation (string, length, valid
timestamp) — none of them is read anywhere as a *decision input*. Every actual decision in this file
branches on `status`, `reason_code`, `rejected_count`, or `omitted_count`, never on how recent
`refreshed_at` is. I also checked `cli.py` and `dashboard.py` for any owned-process-tree-specific
rendering that might show a fresh timestamp in a way that reads as "healthy" without equally
prominent `status` — found none (no dedicated rendering exists yet for this record at all). I did not
find a place where a refreshed-but-still-invalid record could be mistaken for something more
trustworthy than a frozen one.

===========================================================================
4. PID REUSE ACROSS THE FROZEN WINDOW — THE LOAD-BEARING CLAIM DOES NOT UNIFORMLY HOLD
===========================================================================

**This is a real, code-confirmed gap, not a prose nitpick.** The design's claim — "the existing
exact-start/FILETIME comparison catches it, unchanged" — is true only when the prior entry being
reconciled actually *has* a `start_filetime`. Traced `_recorded_process_identity_state`
(`supervisor.py:2923-2950`) precisely:

```python
if expected_filetime is not None:
    if observed_filetime is None:
        return "ambiguous"
    if observed_filetime == expected_filetime:
        return "same"
    return "different"
if _start_tokens_match(observed_start, expected_start):
    return "same"
```

When `expected_filetime` (i.e. `entry.get("start_filetime")` for that prior entry) is `None`, the
function silently falls through to `_start_tokens_match`, which compares the **rounded ISO timestamp
string** (Win32_Process's `CreationDate`, documented elsewhere in this file as rounded, not the exact
kernel FILETIME). A rounded-timestamp match between two *different* processes — the old one and a new
one that has reused the same PID — is exactly the scenario the exact-FILETIME comparison exists to
rule out, and this fallback path does not rule it out; it can return `"same"` (falsely) on nothing
more than coincidental rounding.

This is not a hypothetical edge case: **entries lacking `start_filetime` are proven to exist in the
live fleet right now.** `_has_exact_start_identity` (`supervisor.py:2238-2246`) returns `False`
precisely when a Windows-ISO-shaped row has no FILETIME companion, and `add_node`
(`supervisor.py:3228-3246`) calls `mark_rejected("exact_start_filetime_unavailable", pid)` for such a
row — but does **not** exclude it from `owned` (there is no `return False` on that branch before the
node is still added to `owned[pid]`). So a persisted tree's `entries` list can and does contain rows
with no `start_filetime`, and dev-3's live incident is reported with exactly that `reason_code`. If
dev-6's reconciliation is applied to a tree like dev-3's, the specific entry that caused the
invalidity is exactly the one whose identity check would fall back to the weaker comparison.

**Mitigating factor, stated precisely so this isn't overstated:** if the underlying cause of the
missing FILETIME is a *persistent* provider/environment condition (the common case — e.g. a
permission or WOW64 quirk that recurs every poll), the fresh walk's own `add_node` would independently
re-reject the same pid this poll too, keeping `rejected_count > 0` regardless of what reconciliation
concludes — so the tree still couldn't reach `"complete"` in that case. The gap only bites when the
condition was transient for the *new* occupant of the reused PID (fresh poll gets a clean FILETIME for
the new process) while the *old*, frozen prior entry never had one to compare against — a real but
narrower window than "every FILETIME-less entry is exploitable."

**What I want confirmed/fixed before this proceeds:** either (a) reconciliation must treat a prior
entry with no `start_filetime` as *ineligible for the "re-admitted" bucket via identity-state
comparison alone* — i.e., such an entry can only resolve to "confirmed absent" (pid missing from the
current index entirely) or "rejected," never "same" via the token fallback — or (b) the design must
explicitly document this residual and accept it, the way `#81`'s design accepts and names other
residuals rather than silently inheriting `_recorded_process_identity_state`'s general-purpose
leniency into a new, narrower, safety-relevant use. Right now the design states this comparison is
"unchanged" and therefore implicitly fine everywhere reconciliation uses it; the code shows it is not
uniformly fine.

===========================================================================
LIVE FIELD DATA — DOES THE DESIGN HANDLE ALL THREE CAUSES, OR A NARROWER CLASS?
===========================================================================

**A narrower class — by dev-6's own admission in section 6 of the design, not a gap I'm inferring.**

- **dev-3** (`exact_start_filetime_unavailable`): this IS the shape the design targets (invalid tree,
  presumably healthy wrapper) — but see finding 4 above for the concrete weakness in exactly this
  case.
- **dev-7 and dev-8** (`post_kill_owned_descendant_edge_survived`, **wrappers gone**): dev-6's own
  design text says plainly, in section 6: "A wrapper that is actually gone (dev-8's shape): untouched
  by this design. That path is `_current_proof_failed`/`_unverified_owned_process_tree`, already
  fixed in principle by the walk_complete inversion, still cold-starts for pre-existing records that
  predate `rejected_count`/`walk_complete` entirely... This design doesn't touch that gap and doesn't
  need to." The design's own "not stuck" tier requires the wrapper's identity to independently verify
  via `_wrapped_liveness` before `_owned_process_tree` is even reached — with the wrapper gone, that
  precondition fails, so this design's re-walk path never engages at all for dev-7/dev-8's shape.

**Direct answer to the question that matters most:** two of this hour's three live causes are
explicitly out of scope for this design, and the design says so itself — this is honestly disclosed,
not hidden. But it means a release note claiming "invalid owned-process trees now self-heal" would
overclaim relative to what's actually fixed: it would need to say "self-heal only while the wrapper
remains healthy; a tree invalidated after the wrapper is gone still requires the existing absence
path and/or attended reset." Given dev-7 and dev-8 are two-thirds of this hour's incidents, that
qualifier is not a footnote — it's the majority case in the field data cited to justify the release.

===========================================================================
DEV-4'S #81 COLLISION CHECK
===========================================================================

**No collision found.** Read `docs/DESIGN-81-recovery-actually-recovers-invariant.md` in full. Its
`#156` treatment (the "two-stage" capability-progress witness, and the retroactive-allowlist table
row) treats dev-6's mechanism as an *input* — "the fresh tree is the effect witness" — and adds an
independent, separate verification (a live/stuck owned descendant whose scoped teardown must consume
the *repaired* tree digest before a queued stub turn completes) that doesn't require dev-6's design to
change anything. Section 5 of dev-6's own design states almost exactly the same discriminator
independently ("construct a prior with an entry that is neither re-admitted nor confirmed-absent...
and assert the result CANNOT be 'complete'") that `#81`'s "intended-effect witness" for owned-tree
repair restates ("each is re-admitted or independently proved absent/different... any leftover
identity keeps the result incomplete"). The two documents describe the same underlying set-cover
property from two different angles (design-internal correctness vs. external release-gate
falsifiability) and neither contradicts or requires the other to weaken. dev-4's "no collision" claim
holds. One thing worth surfacing to both authors: `#81`'s own witness (unmodified) inherits finding 4
above — if reconciliation can falsely mark a FILETIME-less identity "re-admitted," a gate built to
trust `walk_complete`'s resulting "complete" status without recomputing the set-cover from raw
evidence (which `#81` says it never does: "the external verifier recomputes this set relation from the
captured OS snapshot and persisted rows; it never trusts `walk_complete=true` by itself") should
independently catch the same gap `#81`'s own external recomputation is designed to catch. That's a
reason to think `#81`'s gate would eventually surface finding 4 in CI even if it shipped unfixed — not
a reason to defer fixing it now.

===========================================================================
VERDICT
===========================================================================

**REJECTED — one concrete, actionable defect, not a wholesale rejection of the approach.** The
reconciliation *mechanism*'s structure is sound: it is genuinely monotonic (finding 1), it does not
reproduce round 12's chaining hazard (finding 2), and it does not create a downstream trust misread at
the "not stuck" tier (finding 3). But its central safety claim about PID reuse does not uniformly hold
against the actual code (finding 4), and that gap is not hypothetical — the exact class of record
missing the precision this comparison depends on is present in the live fleet today. Separately, and
regardless of finding 4's resolution, the release note must not claim this design fixes the
wrapper-gone shape that two of this hour's three field incidents actually are — dev-6's own design
says so. Fix or explicitly accept-and-document finding 4, scope the release claim to "healthy wrapper,
invalid tree" only, and this is ready to re-review.

===========================================================================
RESOLUTION — FINDING 4, SETTLED 2026-08-03 (SECTION 6 RESIDUAL)
===========================================================================

**Finding 4 is NOT a live defect.** Building the round-2-authorized exclusion's regression test
surfaced that the loop it protects (`_owned_process_tree`'s live_prior admission loop) has no live
caller today. It only runs when `prior_authority is not None`, and `prior_authority = prior if
(prior or {}).get("status") == "complete" else None` (unchanged since #120/587e7c1) — an invalid
prior never reaches it, carve-out (`process_tree_invalid_generation_adoption_pending`) or not, since
the carve-out only bypasses the early `prior_tree_hold` return, not this gate.

A filetime-less ISO entry — the shape finding 4 depends on — cannot survive into a validated
`"complete"` record, for two independent, currently-live reasons:

1. **Producer gate** (`supervisor.py`, `add_node`): a filetime-less ISO row calls
   `mark_rejected("exact_start_filetime_unavailable", pid)`, which sets `invalid_reason`, which
   forces `status="invalid"` the same round. Pinned by
   `test_owned_process_tree_holds_when_windows_filetime_is_unavailable`.
2. **Schema gate** (`supervisor.py`, `_valid_owned_process_tree`, ~line 2746, predates #150/#156):
   returns `None` for any persisted record claiming `complete`/`absent` that contains a filetime-less
   ISO entry. Pinned by `test_valid_owned_process_tree_requires_filetime_for_windows_authority`.

Both tests now carry an explicit cross-reference back to this finding, so a future change that
weakens either one will be told, at the point of failure, that dev-6's exclusion loop just became
load-bearing.

**The exclusion itself (top of the live_prior admission loop, before the "different" check) is
kept** — it is correct, costs nothing, and is commented as conditional on these two gates rather than
as a fix for an observed incident.

**Conditional, not closed:** finding 4 becomes exploitable only if a future change widens
`prior_authority` to admit invalid priors (the un-specified "owned-tree rewalk" extension this design
thread was reasoning about, which does not exist in mainline and has no spec in this repo), or
relaxes either gate above. If that widening is ever built, finding 4 returns with it, and the two
pinned tests above are the intended tripwire. Until then, this is a defense-in-depth guard against a
currently-unreachable path, not a fix to a shipped vulnerability — do not describe it in a release
note as closing a live gap, because none was open.
