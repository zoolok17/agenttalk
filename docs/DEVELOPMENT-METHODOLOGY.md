# Development methodology

**Audience.** A third-party auditor of *process* who has never seen this codebase and
needs to answer one question: can this team's claims about its own work be trusted
without re-deriving them from scratch? A new team member who needs to know what is
actually expected of them, beyond "write code and open a PR." Anyone deciding whether
a specific shipped change is safe to rely on.

**Scope.** This document describes *mechanisms and roles*, not tooling. It names no
internal command names, agent identities, or repository-specific paths — every
mechanism here is implementable with a human team, a fully automated pipeline, or any
mix of the two. Where this project's own `docs/` illustrate a mechanism concretely,
that is noted in an aside; the aside is not required reading to understand the rule.

**How to read this document.** Each section states a mechanism, then a one-sentence
**why** — the failure mode the mechanism exists to close. If you are auditing this
project, the *why* is the part to interrogate: ask what would go wrong without it, and
check whether the evidence trail actually demonstrates the mechanism ran, not just that
it is described here.

---

## 1. The shape of the pipeline

Every non-trivial change moves through the same six stages, in order, with no stage
empowered to skip the one before it:

1. **Design** — the contract is written down before anything is built.
2. **Design review** — reviewed at a rigor proportional to blast radius, not to the
   size of the diff.
3. **Implementation** — built in isolation from the mainline, with every defect fix
   proven against a regression test that is itself proven to detect the defect.
4. **Independent review of the shipped diff** — cold reads and ratification passes,
   described below, on the exact revision that will ship.
5. **Automated gate** — a machine-run, fail-closed check that is the *only* accepted
   authority for "this passes," never a person's assertion that they ran something
   locally.
6. **Release** — a deliberate, evidenced act, not a byproduct of the gate passing.

A finding raised at any stage is either folded and the change re-enters the stage that
produced it, or it is escalated to an explicit decision by whoever owns the risk. It is
never silently dropped, and it is never resolved by the person or process that produced
it re-asserting it was fine.

---

## 2. Design contract first

Before a non-trivial change is built, its designer writes the contract: what the change
does, what it explicitly does not do, what invariant it must preserve, and what would
prove it wrong. The contract is reviewed and provisionally agreed *before* the first
line of implementation, not reconstructed from the diff afterward.

**Why:** a contract written after the code exists is a description of what the code
happens to do, not a commitment to what it should do — it cannot catch the gap between
the two, because by then there is nothing left to compare it against.

---

## 3. Tiered adversarial design review

Not every change carries the same risk, and rigor is priced accordingly — but the
*tier is determined by what the change touches*, not by how the change's author
describes it, and not by how many lines it spans. A one-line change to a permission
check, a lock ordering, or a fail-open/fail-closed branch can be system-wide; a
thousand-line mechanical rename usually is not.

- **High-tier surfaces** — authority and permission, trust boundaries, provenance and
  integrity guarantees, concurrency and lock ordering, durability and recovery
  semantics, process lifecycle control, wire/schema contracts, anything moving money or
  equivalent value, or normative policy — get a design panel: multiple independent
  reviewers, deliberately diverse in background and assigned distinct angles of attack,
  who try to break the design before anyone builds it, and return a clear verdict:
  proceed, proceed with named conditions, or revise. A floor on panel size exists and
  scales up with novelty and blast radius; it never scales down for a design that looks
  simple.
- **Mid-tier surfaces** — ordinary product code, or anything changing a public
  contract (a flag, a schema, a config key, a packaging input) — get a documented
  design note and a standard review, not a full panel.
- **Low-tier changes** — non-normative prose, formatting, comments, and additive tests
  that touch no fixture, snapshot, contract, or CI configuration — skip the design gate
  entirely, on a narrow, explicit allowlist. Anything not clearly on that allowlist is
  treated as mid-tier or higher.

Classification is by what the change *does*, not by who is proposing it or what they
call it. The person proposing a tier is never the sole authority on it: an independent
party ratifies the tier, even when the proposer is the most senior person available,
and uncertainty about which tier applies always rounds up, never down. A change that
started narrow and grew scope mid-flight is re-tiered against its full effect, not
grandfathered in at its original, smaller classification.

**Why:** self-assessed risk is a conflict of interest — the person closest to a change
is the worst-positioned to see what it threatens, precisely because they have spent the
most time believing it is fine; and rigor spent uniformly on every change either
starves the changes that need it or taxes the ones that don't, so the system stops
being followed at all.

---

## 4. Implementation: mutation-verified fixes

A regression test is a claim: "this test would have caught the bug." That claim is
never taken on faith. For every defect fix, the mechanism that causes the fix to
matter is temporarily reverted (or, where reverting the whole mechanism isn't
practical, the specific behavior is disabled), the associated test is run and
confirmed to fail — for the reason the fix addresses, not some unrelated breakage —
and only then is the fix restored and the test re-confirmed green.

This applies recursively: a test added specifically because a reviewer proved a gap
by mutating a piece of shared state (see §6) is itself proven the same way before it
is trusted.

**Why:** an untested assumption that "this would have caught it" is exactly as
reliable as any other untested assumption — which is to say, not reliable at all —
and a test suite that only ever gets easier to pass, never proven to actually
discriminate correct from incorrect behavior, degrades into decoration.

*(A concrete instance of this project's own carry ledger — the running record of every
known limitation, when it was raised, and its current disposition — lives in a plain
tracked file with a fixed schema: what, why, where, current status. Auditors can read
it directly rather than reconstructing it from commit messages.)*

---

## 5. Unbriefed cold reads

Beyond the ordinary review that folds a change (where the reviewer knows the history
and the intent), a change that ships product-facing behavior additionally gets at
least one **unbriefed** review: a reviewer who has not seen the design discussion, the
prior review rounds, or the implementer's own account of what the change does. They
are handed the frozen, exact revision that would ship — nothing else — and required to:

- reproduce every blocker and major finding **empirically**, with a concrete
  repro (a command, an input, an observed output) — an assertion that something is
  wrong without a reproduction is not a finding, it is a suspicion;
- positively re-verify, not just assume, that prior fixes actually work — a cold
  reviewer who finds nothing new but never checked whether the last round's claimed
  fixes hold is not providing new evidence, just an absence of effort;
- report every finding it can positively verify, not only the ones that support a
  predetermined conclusion.

The bar for this pass is strict and asymmetric: **any confirmed instance of a system
reporting wrong data as if it were correct blocks the merge**, regardless of severity
elsewhere, because a wrong-but-confident answer is worse than a system that visibly
refuses to answer. A cold read that finds nothing is worth less than one that finds
something and is wrong about half of it — the value is in the empirical pressure, not
the verdict.

**Why:** briefed review inherits the blind spots of the brief — a reviewer who is told
what a change is *supposed* to do will, without intending to, verify that story rather
than interrogate the artifact; someone who starts from zero and has to reconstruct
what the thing claims to do from the thing itself cannot inherit a blind spot they were
never shown.

---

## 6. Ratification deltas

Round-over-round convergence is not assumed — it is checked. After a fix round lands,
a second reviewer (distinct from whoever raised the finding and from whoever fixed it)
independently re-probes the change end-to-end and, for each finding from the prior
round, either **ratifies** the fix (confirms, with fresh evidence, that it actually
resolves the issue) or **overturns** it (shows the fix is incomplete, wrong, or has
introduced a new problem). A ratifier can also mutate a piece of shared logic
themselves to prove a class of finding was systemic rather than a one-off — and when
they do, that mutation and its result become part of the record, not just the verdict.

Judgment calls — "this is an acceptable trade-off," "this is out of scope," "this
matches the design's stated intent" — are exactly the kind of claim this step exists to
test, because they are the ones least likely to be caught by an automated check.

**Why:** the person who just fixed something is the worst-positioned judge of whether
the fix is real, for the same reason a self-assessed risk tier is unreliable — and a
"looks fixed" that was never independently re-probed is a belief wearing the clothes of
a verified fact.

---

## 7. The carry ledger

Every known limitation — something the team knows is imperfect, deferred, or
explicitly out of scope for now — is named in one place, with what it is, why it
exists, where it lives in the system, and its current disposition (in progress,
planned, accepted as a known limitation, backlog). A limitation is never re-asserted
as "still true" without being **re-measured**: if a claim in the ledger depends on a
number, a threshold, or an observed behavior, that observation is repeated against the
current system before the entry is renewed, not copied forward from when it was first
written. A limitation is retired only when the re-measurement shows it resolved, with
the evidence attached — never by simply removing the line.

**Why:** a "known limitations" list that nobody re-checks decays into either a lie (a
limitation that was actually fixed months ago, still listed as open, teaching readers
to stop trusting the list) or a trap (a limitation that quietly got worse, still
described at its original, smaller severity).

---

## 8. Continuous integration as the gate

The single authority for "this change is safe to merge" is a machine-run, fail-closed
pipeline — never a person's local run, and never a person's assertion that they ran
something. The pipeline:

- runs the **full** test suite, not a subset chosen by the change's author, across
  every officially supported combination of operating system and language/runtime
  version the project claims to support (a multi-OS, multi-version matrix, not a
  single representative leg);
- runs static analysis, dependency/vulnerability scanning, and secret-scanning as
  voting checks, not advisory ones;
- has no local escape hatch: a check that can be skipped with a flag is not a gate,
  it is a suggestion;
- treats an individual matrix leg's pass as incomplete evidence on its own — only the
  full, matching set of legs against the exact same revision constitutes a pass;
- treats a flaky failure as a failure to be reproduced and understood, never as
  something to re-run into silence without a diagnosis.

Anything the pipeline cannot yet check mechanically (for example, whether documentation
matches current behavior) is either promoted into an automated check or explicitly
flagged as evidence still requiring independent human verification — it is never
quietly assumed to be fine because the automated part is green.

**Why:** a check that runs on one machine, one operating system, or one person's say-so
answers "did it work for me, once" — which is a different, much weaker question than
"is this safe for everyone who will run it," and the gap between those two questions is
exactly where regressions that "worked on my machine" live.

---

## 9. The release ritual

Shipping is a deliberate act with its own checklist, not something that happens because
the gate went green:

1. The version is bumped and the change log is updated *before* the release gate runs
   again — so the artifact that gets re-checked is the exact one that will be
   published, not an earlier one that happens to be close.
2. The full gate re-runs against that bumped, final revision. A release built from a
   revision that was only checked *before* the version bump is not evidenced — the
   bump itself is a change to what ships.
3. The release is tagged, published, and its CI run is watched to green before it is
   considered complete — a tag pushed while CI is still running is not yet a release.
4. A standing assurance record is appended (or updated) attesting what was checked,
   with pointers to the actual evidence (which reviewers, which gate run, which
   residual limitations carried forward) — not a prose summary asserting quality
   without a way to check it.

**Why:** the moment of shipping is the last point at which a mistake is cheap to catch
and the first point at which a mistake becomes everyone else's problem; treating it as
a ritual with its own re-verification, rather than a formality after the "real" work is
done, is what keeps that asymmetry from being exploited by rushing the last step.

---

## 10. What this methodology assumes, and what it costs

None of the above is free. A design panel costs reviewer-time before a single line of
code exists. Unbriefed cold reads cost the time of someone re-deriving context that
already exists elsewhere. Mutation-verifying every fix costs the discipline to revert
working code on purpose. The claim this methodology makes is not that any of this is
free, but that the alternative — trusting self-assessment, briefed review, and
un-re-measured claims — is not actually cheaper; it just defers the cost to whoever
discovers the gap later, under worse conditions, with less context than the person who
could have caught it here.

A third-party auditor should expect to find: a written design contract for anything
non-trivial; a recorded tier and ratifier for that contract; at least one independent,
unbriefed review on the exact shipped revision for product-facing change, with
positively-verified findings; a second reviewer's ratification of judgment calls; a
carry ledger with re-measured (not merely repeated) entries; a gate that is a machine
decision, not a person's word; and a release record that names its own evidence. Where
any of those is missing for a specific change, that is itself a finding, not a reason
to lower the bar for the next one.
