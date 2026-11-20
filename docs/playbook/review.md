# Playbook: the review rule

**In plain words:** nothing ships because the person who built it says it is fine. Someone who did not build it reads it cold, the builder fixes what that reader finds, and the fix is read again. This page holds the rules that make those reads worth having: how to set up the rounds, how to brief a reader without steering them, and what a reader should check. Each rule says what to do, why (with the mishap that taught it, told without names), and how to apply it.

The step list for the review cycle is in [Roles, skills and procedures, section 4.2](../ROLES-SKILLS-PROCEDURES.md#42-the-review-cycle). The shipped skills [review-code](../../src/agenttalk/skills/devkit/review-code/SKILL.md), [review-failure-injection](../../src/agenttalk/skills/devkit/review-failure-injection/SKILL.md) and [review-contract-drift](../../src/agenttalk/skills/devkit/review-contract-drift/SKILL.md) carry the checks themselves. This page does not repeat them; it adds the habits around them.

Contents: [the shape of a review](#the-shape-of-a-review) · [briefing a reader](#briefing-a-reader) · [what a reader checks](#what-a-reader-checks) · [hygiene for reviewers](#hygiene-for-reviewers)

## The shape of a review

### R1. The sequence: build, cold read, fix round, delta read, final read
- **The rule.** A change is built, then read by someone who did not build it (a cold read). Each finding goes to a fix round. The fix is read again (a delta read), and the last round is read once more as a whole.
- **Why.** A builder cannot see its own blind spots, and a fix can carry new defects of its own.
- **How to apply.** Follow [section 4.2](../ROLES-SKILLS-PROCEDURES.md#42-the-review-cycle). A verdict counts only for the exact commit it names. When no reader of another vendor is available, follow [the single-vendor rule](../ROLES-SKILLS-PROCEDURES.md#when-only-one-vendor-is-available).

### R2. A builder's own green test run is not the review
- **The rule.** Treat the builder's passing tests as the start of the review, not the end. A reader from another vendor, told nothing about the builder's reasoning, reads the work against its stated contract.
- **Why.** One builder's large green suite missed the wire format and the authorisation contract. A blind read from another vendor found both within one turn.
- **How to apply.** Choose a reader of a different vendor, and pin the read to the exact commit. When none is available, the [single-vendor rule](../ROLES-SKILLS-PROCEDURES.md#when-only-one-vendor-is-available) says what to do: wait, use a second team's read, or, last, a labelled same-vendor read that another vendor repeats later.

### R3. Match the bar of the last round to the risk of the change
- **The rule.** Keep a strict last-round bar ("a blocker found after the second fix round parks the work") for safety, money, data-loss and security code. For display-only or wording changes, accept a follow-up for what remains.
- **Why.** On low-risk work, strict bars parked changes that could have shipped; on high-risk work, loose bars let real defects through.
- **How to apply.** State the risk class when you set the bar, set the bar before the late read starts, and list every exception you have already accepted inside the bar itself. A bar that forgets an exception it once allowed catches that exception later, and changing a bar after a verdict looks like moving the goalposts.

### R4. A fresh final sweep finds what many narrow rounds miss
- **The rule.** Before a release or a merge of something large, run one fresh sweep over the whole change, with no history of the earlier rounds.
- **Why.** A final sweep found a blocker that several narrow delta rounds had stepped around, because each of those rounds only looked at what the last fix touched.
- **How to apply.** Send it as a `sweep` task. Say what the change is for, not what you expect to hear.

### R5. Close a residual in the same round
- **The rule.** When a fixer names a leftover problem inside the very mechanism the round is changing, close it in that round, or measure its reach before agreeing to carry it.
- **Why.** The next cold read attacks the most recently changed code hardest, so a leftover named there is the first thing it finds. Carrying it turned a cheap same-round fix into a full extra round of read, fix and re-read. A race or stuck state that a reviewer names may not be carried until a probe has measured it either way.
- **How to apply.** Ask, "is this residual inside what we just touched?" If yes, fix it now or write down the measurement that shows it is harmless.

### R6. Report weak findings, marked as weak
- **The rule.** If something looks suspicious but you cannot build the whole failure, report it anyway and say how sure you are.
- **Why.** A review told to drop unprovable findings dropped an area that a different reviewer later found a real defect in, and in another case the one weak finding that was dropped was the one that happened later in production. A human can dismiss a weak finding in seconds; a dropped one is invisible.
- **How to apply.** Write "low confidence" and the reason you could not finish the chain. For a pre-merge read, completeness matters more than precision.

### R7. Get two blind reads before the first fix round when fixes are expensive
- **The rule.** When fix rounds cost as much as the build itself, get two independent reads (a mid-tier and a top-tier reader, blind to each other) before the first fix round, and send one combined round.
- **Why.** A fix round costs as much as the build, so a defect found only after it costs a whole extra round. Two blind reads lower that risk and show which findings both readers share. Count only valid, unique findings when you claim that a second read improves coverage: in one trial the stronger reader's findings covered everything valid, and the cheaper reader's one unique finding was later withdrawn, so the second read added confidence, not findings.
- **How to apply.** Collect both reads, merge and de-duplicate, then send the builder one list.

### R8. Use the cheaper reader for deltas and the strongest for the final read
- **The rule.** Per-commit and delta reads can go to a mid-tier reader. The strongest reader reads the final pre-merge state of a feature, money and security paths, and design rounds.
- **Why.** Reading is the expensive part of the loop, and each read re-reads the whole diff.
- **How to apply.** Put the choice in the dispatch. Spend scarce second-vendor reads where a second vendor matters most: a cold read of the other vendor's code, a final delta read before merge, a design challenge. Keep fix rounds small.

### R9. A cheap-model review has breadth, not authority
- **The rule.** A review by a cheap model may add findings. It never gives the approval that counts for a gate.
- **Why.** Cheap-model reviews in trials invented confident but ungrounded claims or found no confirmed defect.
- **How to apply.** Use it as an extra pair of eyes, and check every claim it makes against the code.

### R10. Adversarial reviewers run the reproduction
- **The rule.** An adversarial reviewer executes its reproductions. Reasoning about a bypass is not evidence of one.
- **Why.** Reviews that only reasoned passed bypasses that executed reproductions caught.
- **How to apply.** Ask for the command that was run and its output. A reader who runs the documented commands (skill examples, real writer fixtures) also finds what reading alone cannot.

## Briefing a reader

### R11. Never call a review "confirmatory"
- **The rule.** Do not tell a reviewer the review will be a formality, and do not say which verdict you hope for.
- **Why.** A reader told "this will be confirmatory only" ran the check that had been described as already done and found a blocking defect, and its first line was that this was not confirmatory.
- **How to apply.** Scope a review by what it should look at ("only the new guard, do not reopen the rest"), never by what it should conclude.

### R12. Say your motive, and invite the reader to call it out
- **The rule.** When you catch yourself building a reason a finding should not block, put the reasoning in the dispatch, name what you want ("I am tired and I want this to ship"), and ask the reviewer to say so if you are rationalising.
- **Why.** A configuration described as "never valid anyway" would have been waved through; the reader pointed out that the optional preflight knew it was invalid while the live path admitted it. In the same session the same wording produced an honest "this one really is the endless tail".
- **How to apply.** Pair it with a real way out: "if it is the endless tail, say so and I will book it." Ask for the premise first: can the case happen, then the outcome, then the severity.

### R13. Do not inherit a suspicion from the brief
- **The rule.** When a brief compares the change to an earlier bug, check whether the same mechanism is present before agreeing there is a problem. If it is not, say so plainly.
- **Why.** A reviewer was primed with two earlier parsing bugs and asked whether a new design repeated them. The design compared exact strings against a closed list, so it had no parsing step to slip through. Reporting that was right, and softening it toward the brief would not have been.
- **How to apply.** Compare what made the old bug possible with what the new code does. Do not manufacture a finding to match the brief either.

### R14. At the merge gate, run one read with no expectation
- **The rule.** Run at least one read where you state no expected result and no list of places to look: only the question, and the two ways it can fail ("does anything promise more than the code enforces, or deny something it does?").
- **Why.** Unbriefed reads found real defects in code that thorough briefed reads had just approved. A brief focuses attention, and attention leaves a blind spot everywhere else. The briefed reader checks what changed; the cold reader checks the whole contract.
- **How to apply.** Ask for a sweep, not a checklist. Say a clean verdict will be taken as a real signal. If two reads agree but used the same method (for example both re-derived the same hash inputs), count them as one; ask the second to use a different method.

### R15. Do not send a verification read until the author says they are finished
- **The rule.** A push is not a declaration. Ask the author to reply "fold complete at <commit>, nothing further pending" and send the read for that commit.
- **Why.** A reviewer was sent to verify a commit that was replaced minutes later by the author, and spent its turn on a head that no longer existed.
- **How to apply.** If the head moves under a live read, tell the reviewer at once, ask it to keep its findings and mark which survive at the new head.

### R16. Write review briefs in plain engineering terms
- **The rule.** Describe findings by behaviour and impact ("a check that can be bypassed", "a folder that changes into a link while it is being removed"), not as attacker steps or payloads.
- **Why.** The model providers' safety filters rejected ordinary defensive review briefs written with attack vocabulary. One brief was retried many times and dead-lettered without the seat ever seeing it.
- **How to apply.** Keep literal exploit strings in tests and private notes, not in messages. If a seat's provider will not take the work, route it to another seat. If the only seat left is of the builder's vendor, its read is a same-vendor read under [the single-vendor rule](../ROLES-SKILLS-PROCEDURES.md#when-only-one-vendor-is-available): it carries the label, and the list of what a same-vendor GO is never enough for still applies.

## What a reader checks

### R17. Check the premise of a finding, not only its remedy
- **The rule.** For each finding, first ask whether the case it describes can happen at all.
- **Why.** A fix was built for a case that could not occur. It added surface and false confidence and was deleted later.
- **How to apply.** Trace the case to whatever would produce it. If every round closes one dimension and uncovers another, the obligation cannot be closed by listing cases: recast it as a structure that is correct by construction, and test one representative.
- **When the input is free-form text.** If a parser review finds a second new input of the same kind, stop patching the parser and change the input to a strict format.
- **After a parser fix.** A fix round that changes a parser must re-run the original real-input check, not only a new synthetic fixture, which can hide a regression on the real input.

### R18. A validator that takes its expectation from the thing it checks proves nothing
- **The rule.** Take the expected value from a source independent of the artifact being validated.
- **Why.** "Does this agree with itself?" is answered yes by every well-formed artifact, including a stale one. The check runs, passes, and manufactures evidence.
- **How to apply.** For each validator argument, ask where it came from. A test that rewrites the logic it claims to test checks the copy, so call the real entry point. When a change narrows a contract and also edits the tests that enforce it, run the same probe matrix on the base and the head and compare.

### R19. Name the object each check touched
- **The rule.** For each check, say which object it actually looked at, and whether that is the object the claim is about.
- **Why.** A correct checker aimed at the wrong kind of thing (a pattern given to a path checker, a file checked when the defect was in the mailbox) passes honestly and proves nothing.
- **How to apply.** When a check counts N things, say whether the N can be told apart. "Cannot verify" is never "may proceed": make it unknown or hold. Bound any check that runs on every read.

### R20. Audit a claim at its own scope
- **The rule.** When a document makes a global claim ("complete", "everywhere", "every X"), search everything the claim covers, not only the section that announced it.
- **Why.** A check limited to a section's own scope can only confirm what the author already believed, and counterexamples sat outside it.
- **How to apply.** Ask "what is the scope of the property?" separately from "where does the text say it applies?"

### R21. Two questions: applied correctly, and the right rule
- **The rule.** Check that a rule is applied correctly, and separately that it is the right rule for this place.
- **Why.** Wording can stay consistent while the requirement underneath weakens; one reviewer checked conformance and another checked the premise, and only the second found the drift.
- **How to apply.** When you review alone, run both passes on purpose.

### R22. Follow a type to its root
- **The rule.** When a specification calls a field "exact" or strong, follow every reference down to its definition.
- **Why.** The word "exact" meant a strong type in one place and "copied verbatim" in another; the field turned out to be a plain string.
- **How to apply.** Do it first for fields that gate something destructive or hard to undo.

### R23. When a field's meaning widens, list its existing readers
- **The rule.** A fix that widens a field, gives an identifier a second meaning, adds a discriminator, moves a computation to one source or mints a new reason code must list every existing reader and what each assumed.
- **Why.** Several rounds in a row regressed in old code that the round never touched. A membership test can also become wrong in meaning when a single value becomes a set.
- **How to apply.** Make the reader list a named deliverable. Check twin emitters too, such as the two spellings of the same thing.

### R24. For a "preserve behaviour" task, check the other direction
- **The rule.** For anything with two directions (write and read, encode and decode, request and response), name the direction the tests cover, then count the other one.
- **Why.** All the golden tests were write-side, so a port restored the write defaults and none of the read ones. No failing test was structural, not evidence.
- **How to apply.** Add the missing-direction check before declaring the port done.

### R25. A check that is not read is no check
- **The rule.** Read the output of every check to the assertion, and act on it.
- **Why.** A smoke check printed the very proof that the code was wrong, and the author read past it. After the last edit, re-run every check the edit could affect, not just the one you were fixing: fixing a scanner finding tripped a line-length rule.
- **How to apply.** If a check exists but nothing consumes its result, wire it up or delete it.

### R26. A search that returns nothing proves nothing yet
- **The rule.** Before you report "there is no X", show that the same search finds a known example.
- **Why.** A wrong file pattern and a wrong field name each returned an empty, plausible-looking answer, and one nearly let a pull request pass as reviewed on the strength of another one's approval.
- **How to apply.** Print the total before filtering. If the schema is in doubt, dump one known record's keys.

### R27. A quoted command is not evidence it ran as claimed
- **The rule.** Record an observed property of the run: the order that executed, the resolved path, the effective value.
- **Why.** A build file hard-coded a setting, so every "reverse order" flag on the command line was silently ignored; every "both orders" sign-off was the same run twice.
- **How to apply.** When you adopt a flag, prove once that it changes what you can observe, then keep quoting the observation.

### R28. "Pre-existing" needs the baseline's number
- **The rule.** A red result may be labelled pre-existing only with the baseline gate's result for the same case beside it.
- **Why.** A set of errors carried that label for days while the baseline run had none; a test-library upgrade had changed the meaning of a matcher.
- **How to apply.** If the baseline was green, treat the red as a defect of the change until a mechanism proves otherwise.

### R29. Compare content, not digests, for builds
- **The rule.** A packaged artifact's hash identifies one build event, not a source tree. To ask whether two builds are the same, compare a manifest of every entry (name plus a digest of its extracted bytes, leaving out the nondeterministic metadata such as timestamps), recursing into nested archives, or compare the extracted trees byte for byte. A list of names alone declares two packages identical when the same paths hold different compiled modules or resources.
- **Why.** The same tree built several times gave different digests, because of timestamps, and the archives were content-identical.
- **How to apply.** Make the bar the property the change protects, or a sorted manifest of entry names with content digests.

### R30. Defend the known classes, and bound what is left
- **The rule.** Where a check faces an effectively unbounded input space, defend the classes you know and say out loud what residual remains. Do not claim to be exhaustive.
- **Why.** A check for invisible text went many rounds, each finding one more blank character, until the overclaim was replaced by an honest bounded residual.
- **How to apply.** Tie the residual to the threat model: gaming your own quality gate is out of scope. The overclaim is the defect, not the residual.

### R31. When you tighten something, ask what it now refuses
- **The rule.** For every fix that makes something stricter, ask what it now rejects that it should not.
- **Why.** A guard added to a reviewed pull request, against the reviewer's advice to follow up later, refused every launch on one shell. It had been checked "structurally", but the defect was in how arguments are passed.
- **How to apply.** Cross-shell and cross-platform claims need execution on each target. When a reviewer says "fast-follow", ship the approved part and give the addition its own round.

### R32. Look at bytes and at the package
- **The rule.** Diff output hides control characters, so check served assets at the byte level. A diff that touches packaging configuration needs a real wheel build.
- **Why.** A control byte inside a string delimiter rendered as a blank in a diff and broke a feature. Several releases passed CI and could not be installed because editable installs skip wheel building.
- **How to apply.** One cheap byte check or one wheel build at the reviewed commit catches the class.

### R33. A removed enforcement takes its claims with it
- **The rule.** When you authorise a revert, say what must still be true afterwards and name the control that proves it. Any change that removes an enforcement must also remove or re-earn every claim that depended on it.
- **Why.** A revert removed an over-strict rule and the binding and its test as well, while the message to the user still promised the binding. That was a confidently wrong instruction.
- **How to apply.** Search for the claim, not only for the code.

## Hygiene for reviewers

### R34. Review a committed, clean commit, in its own worktree
- **The rule.** Run the tests from the worktree's directory, with its source first on the import path, and review only a committed clean commit.
- **Why.** An editable install made tests import another checkout's code, relative file reads came from the wrong tree, and a live dirty worktree gave false conclusions twice.
- **How to apply.** Check where the package resolves before trusting a run. Before pushing on someone's behalf, require an empty status. When your reproduction contradicts a careful agent's green, suspect your own setup first.

### R35. Outside review runs are for your own code
- **The rule.** A limited external review run is for your own code, never for a client's.
- **Why.** Client code must not reach services outside the programme's boundary.
- **How to apply.** Before using one, gather the retrospectives and the open issues, get a clean branch state, and have the operator start the billed run.
