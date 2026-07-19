# Review Sweeps

**This is a METHODOLOGY document, not normative product content.** Nothing here
constrains what the software does. It records how we look at artifacts before
we trust them, and it changes when we learn something — not when a panel
clears it. If you are looking for the Native Work & Evidence Spine's normative
contract, that is `RFC-native-work-spine.md`; this document is deliberately not
bound to a blob, because the sweeps evolve on their own cadence and coupling
them to a panel-reviewed artifact would tax exactly the thing we want cheap to
improve.

---

## Why sweeps exist

Every sweep is a way of checking a **procedure** when the **outcomes** look
fine — because outcomes that look fine are the only condition under which
anyone needs a sweep.

> A rule you have read tells you a hazard exists; a sweep makes you look. Those
> are different mechanisms, and only the second one fires when you are busy and
> confident. — claude-dev, 2026-07-19

The qualifier matters as much as the claim: sweeps make you look, but **only at
what they name**. A sweep converts one *known* hazard into a reflex. It cannot
reach a hazard nobody has been billed for yet.

Both directions of the procedure/outcome split were observed on 2026-07-19:

- **Procedure bad, outcomes good.** An exponential `covers` recurrence produced
  17 correct table rows. Three lenses executed the table; one asked whether the
  algorithm terminates. That direction cost a P1.
- **Procedure bad, artifact better.** A freeze commit captured a strictly
  better blob than the one that had been verified. A good outcome from a broken
  procedure is still a broken procedure.

> A good outcome makes you stop looking, and a correct table makes you believe
> the thing under it works.

---

## THE STRUCTURAL LIMIT — read this before trusting the list

**A sweep list is a record of past tuition.** Every sweep below encodes a class
someone has already paid for. It cannot reach a class nobody has been billed
for, and only an independent lens attacking the artifact fresh can find those.

Evidence, from the day this document was written: the two most expensive
defects — the algorithmic-exhaustion P1 and the invalid-enum-value P0 — were
both found by **lenses**, not by sweeps.

Sweeps make known hazards cheap to re-check. They do **not** substitute for
independent review. A team that treats a complete sweep list as coverage has
converted its own learning into a ceiling.

**If this document ever reads as sufficient, it is doing harm.**

### The second species of the ceiling

The ceiling is not only classes we have not learned. It is also **classes we
learned and then encoded at the wrong granularity.**

Sweep #4 names the phase list explicitly. It was run. The surface was visited.
The visit was reported. And the defect walked through anyway — because the
sweep's unit is SURFACE and the defect's unit was OBLIGATION × PHASE. A rule
with two phase consequences satisfies a surface-visit check after propagating
one of them.

This species is harder to see than an unlearned class, because the sweep's
*presence* is the reassurance that stops the next look. **"We have a sweep for
that" is the sentence that ends the investigation.**

Practical consequence: **a sweep that has fired and passed is evidence only at
its own granularity.** When a defect passes a sweep that covers its class, the
correct response is to re-encode the sweep finer — not to add a seventh.

---

## Admission criterion

A sweep qualifies when it is a **domain-blind mechanical question**. Anything
requiring domain knowledge to ask is a review lens, not a sweep.

Each sweep below states:
1. the mechanical question,
2. what it is NOT covered by — the most confusable neighbour, and the
   discriminating instance the neighbour cannot claim,
3. what it will NOT catch,
4. its instances.

---

## Sweep 1 — Consistency

**Question:** does the document contradict itself?

**Not covered by:** nothing closely; it is the base case. Distinct from
withdrawal (2) in that consistency compares two *present* statements, while
withdrawal hunts a statement that should be *absent*.

**Will not catch:** a document that is uniformly, consistently wrong. Internal
agreement is not correctness.

**Instances:** a HOLD/UNKNOWN disposition stated one way in the failure-modes
table and another in the source-manifest rule; a case table whose verdict
column contradicted the prose rule it illustrated.

---

## Sweep 2 — Withdrawal

**Question:** does a REMOVED or NARROWED claim survive anywhere?

Grep the **OLD** claim, not the new text. The new text is what you just wrote
and will find easily; the survivor is phrased the old way.

**Not covered by:** consistency (1), which compares present statements. A
withdrawal straggler is often perfectly consistent with its neighbours — it is
the *parent* claim that moved.

**Will not catch:** a claim that was narrowed correctly everywhere but should
not have been narrowed at all.

**Instances:** the dominant defect class for four consecutive rounds of the D1
review. Fix the NORMATIVE site first and let restatements fall out of it;
patching only where reviewers point leaves the parent claim alive and it
re-echoes.

---

## Sweep 3 — Reachability

**Question:** which producer writes each named value?

Triage by **consequence**, not by count. A dead member is only a defect when
its deadness lets a reader wrongly conclude something. Applies to rule
REFERENTS, not just enum members.

**Not covered by:** consumer-propagation (4), which enumerates *readers* of a
changed shape. Reachability asks about *writers* of a value.

**Will not catch:** a value with a real producer that produces it wrongly.

**Instances:** `automation_ci` unreachable by design and disclosed rather than
removed; `reopened` having no producer in D1–D5; and the sharpest one — the
class value `blocking`, which had **no producer anywhere** because it was not a
member of the `{established, unknown}` enum at all. It survived four panel
rounds. Reachability would have caught it; nobody ran it on a class value.

---

## Sweep 4 — Consumer-propagation

**Question:** for every changed shape, type, or field, enumerate its readers.

Must cover MUTATION-TIME and READ-TIME consumers **separately** — a mutation
guard and a read-time check are different consumers of the same rule, and
moving a rule from one to the other is not propagating it to both.

Must visit every surface organised on a **non-topical axis**. Four such
surfaces exist in the work-spine RFC: Acceptance (indexed by contested point),
traceability (by finding-id), phase plans (by schedule), and the invariant list
(by number). All four have gone stale; topical sections have not gone stale
once in fourteen amendments.

**Slot test:** ask *"does this surface have a slot for this rule?"* BEFORE *"is
it filled?"* Absence and omission look identical to a grep.

**Not covered by:** reachability (3) — readers versus writers.

**Will not catch:** the granularity failure described under the structural
limit above. Visiting a surface is not propagating every consequence to it. If
you find yourself reporting "I visited the phase list," ask which *obligation*
you carried there, and whether the rule had more than one.

**Instances:** the phase list going stale in five consecutive amendments; a
`terminal` shape change not propagated to four consumers; and the granularity
case, where the surface was visited and only one of two phase obligations was
carried.

---

## Sweep 5 — Circular validation

**Question:** for every validator, enumerate its arguments and name each
argument's SOURCE. Any **semantic expectation** sourced from the subject is
circular.

Self-referential digests are exempt only while the claim stays
corruption-detection and never licenses advancement alone.

**Not covered by:** target-binding (6). This asks where an argument's
EXPECTATION is sourced; (6) asks what the check's TARGET is. See (6)'s
discriminating instance.

**Will not catch:** a validator whose arguments are all externally sourced and
whose logic is wrong.

**Instances:** a projection that made `record_state = f(verdict)` while the
contradiction pass made `verdict = g(record_state)`; and the disclosed limit
that `covers` proves "inside the domain this item *claims*", since the domain
consulted comes from the item's own `domain_id`.

---

## Sweep 6 — Target-binding

**Question:** for every check, name the OBJECT it actually touched. Is that the
object the claim is about?

This is the only sweep of the six that assumes the answer to "did we check?" is
YES and asks a further question. Every checklist is phrased as did-we-check;
every instance below passed a real check, honestly performed.

### Three species, with different defences — all must be run

- **WRONG IDENTITY.** The check touched a different object than the claim.
  *Defence:* name the object, and label its TYPE.
- **WRONG TIME — the object moved.** The check touched the right object, but
  it changed between the check and the action it authorised.
  *Defence:* re-check AFTER the action, or remove the concurrency.
  > You can aim correctly and still miss, if the thing moves.
- **WRONG TIME — the WORLD moved.** The object did not change at all. Its
  *description* went stale, because everything around it changed.
  *Defence:* re-derive a preserved artifact against the CURRENT world before
  use. **Its name is not evidence.**

The third is the most dangerous of the three, and the reason is the timescale.
The first two have a window measured in minutes. This one has a window measured
in the **lifetime of a label** — and the label is usually accurate when written,
which is what makes it trustworthy right up until it is not.

Worked instance: a blob was tagged `draft/amendment-15-links` at the moment an
amendment was split in two. The tag was created by one agent, used by both, and
verified three times — it resolves, it recovers the expected line count, its
hash matches. Every one of those checks confirmed the object was **INTACT**.
None asked what it **CONTAINED**, because the name answered that and the name
was true at save time. Seven review rounds later the blob still held the
*pre-review* form of content that had since been ratified in a much-changed
state. Applying it as a "mechanical rebase" — the phrase both agents kept using,
accurate when coined — would have silently reverted every P0 and P1 from those
seven rounds while looking like a routine port of held work.

**Integrity checks confirm an object has not changed. They say nothing about
whether it is still the right object.** A checksum cannot detect that the world
moved.

A sweep naming only the first species passes the other two cleanly. The general
form: *a check separated from the action it authorises is only as good as the
interval between them; for anything under concurrent write the interval must be
zero or the check must be REPEATED AFTER the action.* **Verify-then-act is
unsound whenever another writer exists; the sound form is verify, act, verify
the artifact the action produced.**

**Not covered by:** circular-validation (5). The discriminating instance is
descriptor-validated-as-instance: nothing is circular there — the domain glob
comes from an externally sourced, approver-governed registry — and the only
failure is that a correct checker was aimed at the wrong KIND of object.

**Why it needs its own pass:** the check's CORRECTNESS is what makes it
convincing. A sloppy check invites doubt; a rigorous check aimed at the wrong
object produces confident wrongness.

**Will not catch:** a check correctly aimed and simply wrong.

**Instances (nine, one day):**
1. `domains.check_paths` — correct for concrete paths, aimed at a descriptor.
2. Deadman config — contents validated rigorously, nothing validated that the
   check ran on the right object.
3. Consumed-but-undisplayed mail — verifying the FILE was stable was correct;
   the defect was in the INBOX.
4. A worktree file's sha256 recorded as the fingerprint of a git blob — correct
   hash, wrong object; would report corruption on an intact artifact.
5. `git checkout <blob> -- <path>` silently failing (checkout takes a tree-ish),
   with a script then printing a hardcoded "(ratified)" label on a value it had
   never compared.
6. An instruction to fold content into "the RFC's sweep list" — a target
   surface asserted without ever being verified to exist. It did not.
7. A freeze commit — verification correct at the instant it ran, stale by the
   time it authorised the `git add`. **Wrong-time species.**
8. A mechanical audit of the obligation map that passed against the map's own
   stated claim, while the defect was that the claim was in the wrong place to
   be true. A consistency audit cannot detect misplacement, because a misplaced
   artifact is perfectly consistent with itself.
9. One field name, `base`, denoting both the freeze coordinate and the
   amendment universe — so "is the base correct?" answered yes under one
   reading and no under the other.

**Extension — verification SCRIPTS.** A summary line that does not DERIVE from
the comparison it summarises is an unbound target by construction. Two such
scripts were written in one day, one of which printed a conclusion that
contradicted its own output.

---

# Part II — Verification hygiene

Sweeps are checks. Checks are artifacts. Artifacts can be wrong.

## Absence claims

> A malformed query returns the same empty set as a clean system.

An absence result has two causes — the thing is not there, or you did not look
where it is — and **nothing at the call site distinguishes them.**

**Defence: a positive control, run in the SAME QUERY SHAPE.** Same command,
same parser, same field path, against something known to be present. Only then
trust the empty result. A control run a different way controls for the wrong
thing.

**Refinement, earned the hard way:** a positive control validates the query
**mechanism**, not the **pattern's assumptions**. One such control confirmed
that grep worked and that a different anchor existed — and still missed an
anchor whose phrase wrapped across a line break. The control must be
**same-shape AND same-hazard**: for a substring probe over prose, the control
phrase must itself wrap.

**Corollary:** an unstated property cannot fail a consistency check. A
stated-but-uncarried value is a visible mismatch; a value nobody stated presents
nothing to mismatch against, so the same inspection passes over it in silence.
When auditing for carriers, enumerate the full set first rather than checking
the ones that surfaced.

## Placement of absence claims

**An absence claim is only checkable against a BOUNDED universe, so an artifact
making one must live where its universe is bounded.**

- In a dispatch, the universe is "this change" — bounded by construction,
  falsifiable in one sitting.
- In a living document, the universe is every future amendment — unbounded, and
  the claim cannot be true.

This diagnosis cost four review rounds. A permanent section scoped to one
amendment is wrong the moment the next one lands, and the symptom was two scope
sentences — one document-framed, one dispatch-framed — arguing inside one
table. **When a straggler cannot be fixed in place, read it as a placement
signal: "this cannot be said correctly here" usually means *here* is wrong.**

## Thresholded views

**A thresholded view answers "WHAT IS BAD ENOUGH TO ALARM", never "WHAT
EXISTS".**

An alarm is tuned to be QUIET; a ledger must be COMPLETE. Those are opposite
design goals, and the same command often serves both — which is how they get
conflated. **The better-tuned the alarm, the bigger the blind spot**, and
nothing in the output says "there are more below the line."

Observed: a 2700-second threshold hid one of two open obligations from the
person who had raised the threshold that morning. Their own tuning became their
own blind spot.

## When findings partition cleanly on a property of your probe, SUSPECT THE PROBE

> A real document defect has no reason to respect the syntax of the tool looking
> for it. — claude-rev, 2026-07-19

An anchor run reported 8 of 18 missing. All 8 contained a backtick; all 10 that
resolved did not — because PowerShell's `-like` treats backtick as an escape
character. The partition was perfect along a property of the matcher, which is
never what a real defect distribution looks like.

This is the single most useful diagnostic in this document, because it detects
a broken probe **from its output alone**, without needing a control.

### How it complements the positive control

They answer different questions and you want both:

- A **positive control** tells you your probe **CAN** fail — it is a
  precondition check, run before you trust an empty result, and it costs a
  second query.
- The **partition diagnostic** tells you your probe **DID** fail, from the
  shape of the results, with no second run at all.

The control is prophylactic and the diagnostic is forensic. A control you
forgot to run leaves you with nothing; the partition signal is still sitting in
the output you already have.

### This document could not verify itself on the first attempt

Written down because it argues the case better than any paragraph here can.

A coverage check was run over the finished text of this file, for every item it
was required to contain. It reported **two missing**. Both were present. The
three diagnoses cost three of the six mechanisms enumerated above:

- **#1 line-wrap** — the search key contained a newline; the phrase wraps.
- **#3 structural blindness** — the wrapped line carries a blockquote `>`
  marker, so whitespace-flattening alone still failed.
- **#6 metacharacter / variant** — the probe searched `wrong-IDENTITY`; the
  text says `WRONG IDENTITY`. Hyphen versus space.

Zero real gaps. Three probe failures, on a coverage check of the document that
lists those exact mechanisms, run by the person who listed them.

What caught it was the partition diagnostic, on its first independent use:
every "missing" item was a search key typed by hand, every resolved item was
one that had been copied. That is a property of the probe, not of the document.

**The lesson is not that the author was careless.** It is that knowing all six
mechanisms, having just written them down, does not make you immune to them —
which is the entire argument for a sweep over a rule you have read.

## Six distinct probe failure mechanisms, one day

They are listed separately because they are all different — which is the point.
"Be careful with regexes" is not the lesson.

1. **Line-wrap** — the phrase spans a line break, so no single line contains it.
2. **Line-adjacency** — a ±3-line window around adjacent table rows let one true
   statement produce three false positives.
3. **Structural blindness to markup** — matching prose while the semantic unit
   is a table row, a bullet, or a section.
4. **Substring collision** — a short field name matching inside a longer one.
5. **Wrong unit searched** — searching the message when the artifact is the
   subject, or vice versa.
6. **Metacharacter in the matcher** — the tool's own syntax silently eating part
   of the pattern.

The unifying diagnosis: **the probe's model of the artifact was wrong, and the
probe cannot tell.** Mechanisms 1–3 assume LAYOUT; layout is not structure. A
bigger window does not fix a proximity assumption — it makes the false positives
worse. Match on the structural unit.

---

# Part III — Transmission

## Completeness is owed by whoever TRANSMITS, measured at the RECEIVER

An artifact must arrive COMPLETE at whoever audits it. Every link in the chain
sits on that boundary — not just the author.

> Reconstructing them makes the reviewer the assembler of the supposedly
> independently checkable artifact. The property is destroyed by splicing at any
> layer, not just the author's. — codex-rev, 2026-07-19

**Why the rule is not self-enforcing:** delta-by-reference is cheaper for the
SENDER and costlier for the RECEIVER. You feel the cost when receiving and not
when sending, which is exactly when a rule stops holding. It was refused in one
direction and reproduced in the other within the hour by the same person.

### Two distinct harms, and a count catches only one

- **OMISSION.** Rows go missing. Caught by a count — *if* the count is stated
  from the enumeration rather than from memory.
- **ALTERATION.** An abbreviation is a TRANSFORMATION, and a transformation
  applied to a BOUNDING claim can silently move the bound. **Completeness
  checks count; they do not compare.**

Observed: "amendments #1–#13" compressed to "pre-#13" while summarising. Since
the amendment parent commit *is* the #13 commit, "pre-#13" left exactly one
amendment neither mapped nor excluded — in the clause whose only job is to bound
the universe. It passed four readers.

### The sharper half

> A restatement introduced by an assurance that nothing changed is the least
> scrutinised text in any message.

"EXCLUSIONS **unchanged**: …" followed by a *shorter* restatement is
self-refuting — if it were unchanged there would be no restatement — and the
word "unchanged" is precisely what stops a reader checking the list after it.
The assurance transfers attention away from the exact span being newly authored.

**Defence:** never restate a bounding claim in abbreviated form. Reproduce it
verbatim, or point at it without paraphrase. "Unchanged" plus a paraphrase is a
newly authored claim wearing a label that says *do not read this*.

## Freeze protocol

**PREVENTION**
1. An explicit **stop-writing handshake**, so the check-to-commit window is
   empty. Whoever reports coordinates sends "coordinates void, writing resumed"
   as **its own message** before writing again — bundled into other traffic it
   reads as commentary, not a state change.
2. **Three guards** on the commit: worktree == reported before staging, staged
   index == reported, and **COMMITTED BLOB == reported**. Stop on any mismatch.
   The third is the one whose absence caused a bad freeze; a pre-commit check
   proves a property of the worktree file, while the object under review is the
   committed blob.

**FORENSICS**
- `git hash-object -w` makes a reported fingerprint retrievable so a divergence
  can be diffed. It does **not** narrow the check-to-act window.

**Keeping a forensic tool in the prevention column is how you end up with a
well-documented recurring failure.**

## Coordinates: fabrication, and the two checks that catch it

There is a failure worse than a stale or mislabelled value: **a value with no
referent at all.** Not a real number pointed at the wrong object — a number
produced by the part of composing that fills a slot because the slot is there.

Observed twice in consecutive messages, the second inside the message
correcting the first, two lines below a sentence promising not to do it.

**DEFENCE 1 — never type a hash.** Every coordinate in a message must be
**interpolated from command output in the same invocation that sends the
message**. If prose reaches a coordinate slot, stop and measure; do not
continue the sentence. This matters because the dispositional version does not
work: "I will be careful" failed *inside the message that said it*. An
assurance occupying the space where the handling should be is the same defect
this document describes elsewhere — the fix has to remove the failure mode, not
resolve to avoid it.

**DEFENCE 2 — CHECK THE SHAPE BEFORE THE VALUE.**

| kind | length |
|---|---|
| git object id | 40 hex |
| sha256 | 64 hex |

The invented value failed the shape of the field it sat in — 40 hex characters
in a sha256 slot. So a coordinate can be rejected as **impossible** before
anyone tries to resolve it, and unlike resolution this works **on a message
alone**: no clone, no network, no correct repository state, no access to the
artifact at all. **A wrong-length hash is not a typo. It is a value that was
never measured.**

**Why the guards bounded this anyway.** The three freeze guards do not detect
fabrication, and do not need to. They ask one question — *does the artifact in
front of me match the number in the message?* — and stale, mislabelled and
invented values all fail it identically. The verifier never needs to know which
way a value is wrong, or to model how the sender might err. **That is the
argument for verification over trustworthiness**, and it is why a fabricated
coordinate costs a round-trip rather than a corrupted freeze.

**Label object TYPES, every time.** `RFC BLOB`, `PARENT COMMIT`, `AMENDMENT
PARENT COMMIT` — never a bare hash and never a bare `base`. On this project a
panel resolved every coordinate on first check for the first time in six rounds
once the types were labelled.

**Platform note:** with `core.autocrlf=true`, a scripted rewrite can silently
convert a file to LF while `git diff` stays clean, because autocrlf normalises
into the index. Check line endings after any non-editor rewrite. And a `sha256`
of worktree bytes is **not** the blob git stores — verify a blob by its object
id, which is object-correct by construction.

## The obligation → phase map

A standing requirement for every amendment's **scope statement** — the dispatch
message, not the document. See *Placement of absence claims* for why.

| obligation (one row per normative MUST) | phase that builds it | phase-list line |

**Rules:**
- One row per **obligation**, not per rule. A rule with two phase consequences
  produces two rows; carrying one leaves a visibly empty cell rather than an
  unasked question.
- **Rows are NUMBERED**, so a count claim is falsifiable by inspection. An
  unnumbered table converts an absence claim ("nothing is missing") into
  something no reader can check; numbering converts it into a presence claim.
- The universe names the **AMENDMENT PARENT**, not the previous review revision.
- It travels **COMPLETE** in every dispatch, at every hop.
- **Row test:** *would this row go empty if the obligation were dropped?* A row
  that cannot go empty is describing the plan rather than checking it.

**It does not replace consumer-propagation (4) — it reports it.** If the phase
list was never visited, every third cell is empty and the table shows it. And it
is **count-checkable by someone who is not the author**, which is the difference
between an artifact and a description: "I ran the sweep" is a description.

**Its own findings, in one amendment:** a `work check` obligation assigned to a
phase that forbids `work check`; a migration obligation with no phase at all;
and a 21-vs-22 row discrepancy that only a complete enumeration could expose.

## Wrapped-agent constraints

A wrapped agent's contract bars `sync / threads / drain / recv / wait / ack` —
all cursor-moving.

- **Close obligations with `reply --to-request`, not `ack`.** It discharges the
  obligation, stays inside the contract, and leaves a question→answer link an
  ack would erase. **"Please ack" silently no-ops for every wrapped agent** —
  the request looks actionable and is not.
- **A wrapped agent CAN self-audit.** `deadman --json --threshold-seconds 1`,
  read `.buckets.stale_obligation[]`, filter on `.agent`. Run it before going
  idle. The liaison's default-threshold deadman is the **backstop** for when the
  self-audit never runs — a stalled turn, a crashed wrapper. Two independent
  observers; neither a single point of failure.
- **Never pipe a consuming read into `head`.** `drain` consumes; truncating the
  display still advances the cursor, and the untruncated messages are marked
  read and shown to nobody. This defeats `unread=N`, `[REPLY-WAITING]` and the
  deadman simultaneously, *because* the cursor advanced. Unread mail is visible;
  consumed-but-undisplayed mail is not.
- **Never inline prose containing backticks or pipes into a shell command.**
  The corruption is SELECTIVE — it eats your **code literals** specifically, the
  identifiers and paths a reader cannot infer, and leaves fluent prose with
  holes. Fluent text does not read as damaged. Write the body to a file and pass
  it by path.

A rejected version of the self-audit rule is recorded because it is instructive:
"an unwrapped liaison must supply the list" would have taught every wrapped agent
to WAIT for information one command would give them, and the waiting would have
looked like protocol compliance. **A wrong sweep does not merely fail to catch
things — it installs a behaviour.**

---

## Severity vocabulary

**"P1-against-the-tests, P2-against-the-prose."**

Severity against the artifact and severity against what the artifact BECOMES are
not the same number. A required-case table *is* the test suite; a table that
fails to test the rule it exists to pin is a worse defect than its effect on the
prose suggests. Collapsing the two is how a table defect gets rated by its prose
impact.

Related: an implementer builds to the **table**, because it is concrete and
testable, so the prose that carries the authority loses silently. Every case in
a normative table will be transcribed into a test. A wrong case becomes a wrong
test asserting wrong behaviour, and the suite goes green.

## Two closing asymmetries

**A correctness table and a termination guarantee are different obligations.**
All 17 semantic rows passed against an implementation that hangs.

**A property with no test is a comment.** A universal quantified over code that
does not exist yet is untestable, and therefore has the grammar of a rule and
the force of a comment. Scope it to a boundary you can assert — then assert the
**mechanism**, not the wish.
