---
reviewed-against: "0.105"
---

# Migration profiles (neutral)

In plain words: a migration moves working software to a new platform, version or data layout
and promises it still behaves the same. These two profiles say when one **slice** of the
move is done, and when the **whole** move is done. They are written for any project. What
belongs to one project (its own offline rules, branch rules, named tools, hosts it must never
touch, vendor rules) goes in that project's overlay (`project-overlay.md`).

They sit on top of the base done record in `../SKILL.md` and use its exception rules. They
never claim the third level below.

## Three levels, kept apart
- **Level A, a slice is done.** One piece of migration work with one purpose is finished
  when every row of part 1 is met or is a recorded exception.
- **Level B, the agreed programme is delivered.** Every included slice is at level A and
  every row of part 2 is met or is a recorded exception. This is what a demonstration can show.
- **Level C, live in production.** Only whoever owns production can authorise and do this.
  This skill never claims it. Decisions that belong to that owner stay listed as blockers
  for going live.

Anything short of a level is **partly met** or **not met**. It is never "done". A decision
that explains a gap does not turn it into met.

## What every row records
The outcome (met, not met, not applicable), where the evidence is, who checked it, and any
exception decision. **The checker is never the builder.**

## Which build each result ran on
A comparison needs two builds, and some checks run on something built from the candidate.
So each result names the role of what it ran on:
- **original**: the starting point, kept the same across every comparison;
- **candidate**: the build being delivered;
- **derived**: made from the candidate for the check (a test build, or the changed
  bytecode a mutation tool produces), with the candidate it came from.

Every run records: the role, the source revision and tree, the artifact fingerprint (for a
derived build, the candidate it came from), the tool with its version and configuration,
the test data, the command and environment, the outcome and exit code. A source review
records the reviewed tree and the verdict; the runtime fields do not apply to it. The
checker confirms that all candidate evidence names the same candidate and that the
original is the same original everywhere. Evidence from another build does not count, and
old evidence is not reused for a new candidate.

## Applicability
A row may be marked not applicable only at assignment, in the slice's plan, with the
reason, and accepted by a named checker who is not the builder. **A test or tool that could
not run is not met, never "not applicable".** These rows can never be not applicable:
pinning (A1), safety (A8), the independent review (A10) and the honest record (A11).

## Part 1: a slice is done (level A) when
1. **Pinned.** The starting and the finished revisions are named in full, on a branch.
2. **Builds from scratch.** A clean checkout builds. The build records the list of libraries
   and the fingerprint of each artifact.
3. **The project's own tests give the same results, test by test.** Compare each test's
   outcome with the original, not only the totals: a test that newly fails is a failure
   even if another that used to fail now passes. Count discovery and start-up errors and
   skipped tests. Tests that already failed on the original are listed by name and accepted
   by the checker. No test is deleted, switched off or weakened without a written reason
   the checker accepted. **A suite named as the proof of a change must exercise the changed
   component.** Name which tests call it. A suite that stubs the component, or never calls
   it, cannot prove its upgrade: unchanged results of such a suite do not meet this row.
   Compare the component's real output, old against new (for example the rendered
   documents or exported files, or the actual response of the library), against criteria
   recorded before the run.
4. **Behaves the same from the outside, when the slice changes anything that runs.**
   Before the run, name the requests to send, the stored data and side effects to compare,
   the clean-ups allowed before comparing (timestamps, generated ids) and the expected
   differences, accepted by the checker. Keep the raw differences. Claim sameness only for
   what was compared. **A new component that answers requests** (for example a new root
   application, a gateway or a fallback page) is probed for everything it can now answer,
   not only the route it was added for: list the request surface it newly owns, write the
   expected answer for each class of request before the run (including the paths nobody
   claimed before, other methods, and requests that must be refused), and probe every class.
5. **The tests still catch planted bugs, where the stack supports mutation testing.** Run
   the same targets and settings on the original and the new code. Report killed,
   survived, no coverage and timed out, and what was excluded. Every planted bug the
   original tests caught is still caught (matched by class, method, kind of change and
   description), or the difference is explained. Where the tool cannot run: "not measured
   by mutation testing", the reason and the other evidence; the row is not met unless it
   was declared not applicable at assignment.
6. **Every fix is proven.** Each fix has a test that fails when the fix is removed and
   passes when it is back.
7. **No new warnings from the analysis tools and scans.** At least, each on what actually
   ships: a vulnerability scan of the packaged libraries; a secret scan of everything that
   ships; an image scan when an image ships; code analysis suited to the stack. Name each
   tool, its version and its configuration. Each run must complete: a skipped or failed
   scan is not met, never "no new warnings". Findings that already existed on the original
   go on the owner's findings list. **Check each new finding against the vendor's own
   advisory page**: the scanner's "fixed in" version can be one that was never released,
   its severity label can differ from the vendor's, and the vendor lists the conditions
   under which the problem applies. Then test those conditions in the shipped artifact
   and the owned source, and record the disposition (applies and is fixed, does not apply
   and why, or covered by an authorised decision).
8. **Safe (never waivable).** Secrets are referred to by location only, never copied or
   printed. No credential ends up inside an artifact. A credential that was already in
   the original stays an unmet safety row until an authorised decision resolves it; it is
   recorded, never waived. Every run (build, tests, comparison, mutation, runtime checks)
   happens inside the boundary the project's overlay names, with evidence of that boundary.
9. **Checked at runtime on every launch path that can be run.** List the launch paths
   (local start, container, rollback path). Confirm the runtime version and the libraries
   actually loaded. Paths that belong to someone else and cannot be run are listed as
   gaps; never visit someone else's system to tick this row.
10. **Independently reviewed.** A reviewer from a different vendor than the builder read it,
    with no expected outcome stated. Every finding that remains after the read has one of
    three dispositions, and only these: **fixed** (with the evidence); **shown not to block
    the agreed scope** (with the reason, checked by someone who is not the builder); or
    **covered by an authorised scope or risk decision** (the exception authority writes
    what is accepted, why, by whom and until when). An owner and a destination (the next
    slice, or the final follow-up list) are recorded for the second and third kind, but
    recording them does not finish a finding that still breaks the agreed scope or a
    safety row. No P1 is open. How many review rounds are allowed, and whether a P1 found
    late is logged and cut rather than chased, are fixed by the project's own plan and
    belong in its overlay; this row does not decide them. The builder's handoff lists this
    review as open with the reader as next owner; the slice is accepted when it is in (the
    build closes first, then the lead creates the read).
11. **Recorded honestly.** The acceptance record has the verdict, the numbers and the
    evidence paths. Nothing unfinished is described as done.

## Part 2: the whole migration (level B) when
1. **A manifest of what is included:** the slices, the requirements left out, and the
   fingerprint of the combined final artifact.
2. **Every acceptance row has a verdict with evidence.** Partly met and not met stay
   visible as such. Each gap and each deferred finding has an authorised decision on scope
   and risk, with an owner and a follow-up.
3. **Everything works together.** All the project's suites run on the final combined code,
   on the target versions, each test compared with the original as in A3. Failures that
   already existed on the original are listed by name: this is parity with the original,
   not a new "zero failures" rule.
4. **The test net is as strong as before.** Mutation testing on the final code against the
   original, per module, as in A5, or the same recorded reason as there.
5. **The final artifact works end to end.** It starts and stops cleanly on each launch path
   that can be run, and the comparison from the outside (A4) passes on the final artifact
   itself.
6. **The data survives.** Name the data set, the inventory of tables and objects and the
   rules the data must keep (sequences and permissions where they matter). Reconcile them,
   not only row counts. Label it a local rehearsal.
7. **Going back is proven up to a stated point.** State the boundary that was tested.
   Evidence that writers were stopped and fenced; the backup and restore point; the old
   application's checks against the restored data; a procedure for writes made after the
   switch (kept and reconciled, or their loss explicitly authorised on current evidence);
   the triggers for going back, the owner, and the measured recovery time and data loss
   against the agreed limits. Local restore evidence is kept apart from the behaviour of
   managed services that was not tested.
8. **A security read of the final artifact passed.** By a different vendor than the
   builders. It passes when no P1 is open and every finding has a disposition: fixed,
   accepted by a named owner, or listed for the owner of production.
9. **The people who run it can run it.** The run and upgrade instructions are written and
   were followed in order, from their stated starting state, by a fresh run or by someone who
   did not write them (separate commands that each pass do not count); they are labelled
   rehearsals where they are not proven in production.
10. **The evidence is complete,** and the production owner's decisions are listed as
    blockers for level C.
11. **A final cold read passed.** A vendor that did not write the acceptance record read
    it, with no expected outcome stated.
12. **The demonstration runs from the branches the manifest names.**

## Technical detail
- This text is the neutral form of a project's migration checklist (versions 1 to 3 and the
  two independent reads that shaped it). Where the project had a rule that does not travel
  (an offline-only build, local-only branches, a named mutation tool, a vendor that may
  never be the only reader, hosts that must not be touched), it lives in the project's
  overlay.
- Applicability is decided before the work; the three-level split mirrors the acceptance
  record's rule that partly met and not met are not done.
