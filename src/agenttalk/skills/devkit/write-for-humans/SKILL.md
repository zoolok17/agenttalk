---
name: write-for-humans
description: >-
  Write anything a person will read (release notes, changelog entries, pull request
  descriptions, issue and review comments, status reports) so that someone who does
  not program understands what changed, why it matters and what they will notice.
  Same facts, explained in plain words, with the technical detail moved to the end.
  Use whenever the reader may not be a developer. Do NOT use for code comments (use
  craft-code), for reference, tutorial or how-to documentation pages (use write-docs),
  or for reviewing existing documentation (use review-docs).
reviewed-against: "0.94"
category: production
evidence-profile:
  - production-handoff
---

# write-for-humans

Picture the reader as someone who has never written code but wants to understand
what the software does and what just changed. They are curious and careful, and
they have time to read. What stops them is not length but jargon, missing context
and a list of file names where an explanation should be.

This skill never asks you to drop a fact. It asks you to **explain** every fact,
and to move the details only developers need (file names, commit IDs, test names)
to a short section at the end. The one exception is anything that must not be
published, such as a password, a key or a customer's details: that is removed or
summarised safely, never moved to the end (see PRECISION WITHOUT CLUTTER).

## WHO: the reader
- [ ] They use the product, pay for it or decide about it. They do not read the code.
- [ ] They know what the product is for, but not how it works inside.
- [ ] They read to answer four questions: **What changed? Why should I care? What
      will I notice? Do I need to do anything?** Answer all four.
- [ ] Length is fine. A longer text in plain words beats a short one full of
      jargon. Do not compress to look efficient.

## ORDER: write in this order
1. **What changed, for the user.** One or two sentences, told from the user's side.
   "Reminders no longer arrive twice", not "Fixed duplicate dispatch in the
   notifier".
2. **Why it matters.** The problem it solves, the risk it removes, or who it helps.
3. **What you will notice.** Use the pattern "Before this, X happened. Now, Y
   happens." Be concrete: what appears, disappears, gets faster or stops failing.
4. **What you need to do.** An action, or say plainly "Nothing to do."
5. **Technical details** (last, for developers and reviewers).

## WORDS: plain and short
- [ ] Use simple words and short sentences, one idea per sentence. Say who does
      what: "the app now saves your list" rather than "saving is now performed".
- [ ] Avoid jargon. When a technical term cannot be avoided, explain it the first
      time, in plain words: "a cache (a saved copy kept so pages load faster)".
- [ ] Give examples from the reader's world: the shopping basket, the to-do list,
      tomorrow's forecast.
- [ ] Keep numbers exact, but say what they mean: "the page now loads in 1 second
      instead of 6", not only "p95 latency down 83%".
- [ ] Be honest about limits: "we have not tested this on phones yet" is better
      than silence.
- [ ] Never let one plain phrase stand for two different facts. "Released" means
      a new version can be installed; it does not mean anyone is using it yet.

Instead of this, say that:

| Instead of | Say |
|---|---|
| merged | added to the main version of the code, the one the next release is made from |
| regression | something that used to work broke again |
| CI, the pipeline | the automatic checks that run on every change |
| race condition | two things happened at the same moment and got in each other's way |
| refactor | reorganised the code without changing what it does |
| flaky test | an automatic check that sometimes fails for no real reason |
| dependency | a piece of someone else's software that this one relies on |
| release, publish | made a new version available to install (people get it when they upgrade) |
| deploy, roll out | switched a running system over to the new version, so people are now using it |
| revert | undid a change in the code |
| rollback | switched a running system back to the previous version |
| cache | a saved copy kept so things load faster |
| endpoint, API | the address another program uses to ask for data |
| timeout | gave up waiting after N seconds |
| memory leak | the program kept using more memory and never gave it back |
| lock | a rule that lets only one thing change something at a time |
| null, undefined | missing or empty |
| PR, pull request | a proposed change, waiting for review |
| commit ID, SHA, file path | (move to Technical details) |

## PRECISION WITHOUT CLUTTER
- [ ] **Keep every fact.** Translate it; do not delete it. A fact that only a
      developer needs moves to Technical details, but it stays.
- [ ] **Except anything sensitive, which this rule never covers.** Secrets,
      passwords, keys and tokens; private or customer data (names, email
      addresses, account numbers, anything copied from a real person's records or
      logs); and internal-only paths, hostnames or addresses are never kept, not
      even in Technical details. Moving them to the end does not make them safe.
      Remove them, or replace them with a safe summary that keeps the meaning: "a
      key was rotated", "one customer's order was affected". Check pasted command
      output and logs for them too. If you are not sure whether something is
      sensitive, leave it out and tell whoever asked for the text.
- [ ] The main text must make sense with the Technical details section removed.
      Test this by reading it without that section.
- [ ] Technical details holds a short list of file paths, commit IDs, function and
      test names, issue numbers, settings names and exact command output, all
      with sensitive data already removed.
- [ ] Say "fixed" only when it was checked, and say how in plain words: "we left it
      running for ten minutes and memory stayed flat".
- [ ] When a document has a required format (for example a changelog with fixed
      headings), keep the format and write the entries in this style.

## TEMPLATES

### Release notes and changelog entries
```markdown
**<What changed, in the user's words>**

<Why it matters: the problem it solves, in one to three sentences.>

What you will notice: before this, <old behaviour>. Now, <new behaviour>.

What you need to do: <action, or "nothing">.

Technical details: <paths, setting names, issue and change numbers>.
```
Example, for a weather tool: **"Tomorrow's forecast no longer shows yesterday's
temperature."** Before this, after midnight the app could show the previous day's
numbers for up to an hour. Now it fetches the new day's forecast right away.
Nothing to do.

### Pull request descriptions
```markdown
## What this changes
<One paragraph, from the user's side.>

## Why
<The problem, and who it affects.>

## What you will notice
<Before this ... Now ...>

## What reviewers and users need to do
<Anything to check, configure or decide, or "nothing".>

## How we checked it
<What was tried and what was seen, in plain words.>

## Technical details
<Files, functions, tests, commands and results, issue links.>
```

### Issue and review comments
A review finding is a small story: what is wrong, why it matters, what to do.
If your review process gives each finding a severity tag (P1, P2 and so on, or
any other scale), keep that tag exactly as the process requires, at the start,
so findings can still be sorted and counted. The plain-language "why it matters"
goes next to the tag. It explains the tag; it never replaces it.
```markdown
**<severity tag, unchanged> What I found:** <one sentence a non-programmer
understands>.
**Why it matters:** <who is affected, how badly, how often, in plain words: "a
customer could be charged twice">.
**What I suggest:** <the change, or the decision needed>.
**Technical details:** <file and line, how to reproduce it, test names>.
```
Example, for a shop: **P1. What I found:** if two people buy the last item at the
same moment, both orders go through. **Why it matters:** one of them pays for
something we cannot send. It is rare, but it happens on busy sale days. **What I
suggest:** check the stock again just before taking the payment.

### Status reports to a person
```markdown
**Where things stand:** <one sentence>.
**Done since last time:** <plain-language list>.
**In progress:** <what, and when you expect it>.
**Blocked or needs your decision:** <the question, the options, your recommendation>.
**Technical details:** <IDs, links, numbers for the record>.
```

## SELF-CHECK: before posting
- [ ] Would someone who has never programmed understand every sentence of the
      main text?
- [ ] Is every technical term either avoided or explained the first time?
- [ ] Is "what you will notice" there, with a before and a now?
- [ ] Is "what you need to do" there, even if it is "nothing"?
- [ ] Are paths, IDs and code names only in Technical details, and does the main
      text still make sense without them?
- [ ] Did every fact from the technical version survive?
- [ ] Is there no secret, private data or internal-only address anywhere,
      including Technical details and pasted output?
- [ ] Does every review finding still carry its severity tag?
- [ ] Is every claim honest about what was checked and what was not?

## Evidence

This skill changes how the text reads, never the evidence that goes with it.
When you use it inside another skill's work, emit THAT skill's evidence, unchanged:
a review written with review-code still emits review-code's `review-result` fields
(its status, severity tags and reviewed commit), and a test report still emits the
testing skill's `qa-result` fields.

Only when this skill is the whole job (a release note, a changelog entry, a pull
request description or a status report that no other skill is producing), emit the
`production-handoff` profile (full rules + bus-validated vs skill-policy: ../_shared/references/evidence.md).

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
