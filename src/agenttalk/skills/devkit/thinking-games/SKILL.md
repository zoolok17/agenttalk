---
name: thinking-games
description: >-
  Play one short thinking game, optionally with a roleplay card, at a few named moments
  to get a second, independent list of candidate ideas, then merge it with a plain
  list. Use at the start of a new design or initiative (before its challenge), after
  two failed attempts on one assumption outside a review-ordered fix round (the reality
  probe only), or as a side round on product ideas. Do NOT use on routine work,
  review-ordered fixes, releases or every task, to decide scope, or for a premortem or
  "should we do this at all" (use the challenge skill, agenttalk.challenge or
  agenttalk-challenge).
reviewed-against: "0.105"
category: production
evidence-profile:
  - planning-artifact
---

# thinking-games

A thinking game is a few minutes of structured thinking that ends in a short list of
ideas you can test. Its job is to make a second list that differs from the first, not
to be clever. There are two axes: **game cards** (a way to think) and **roleplay cards**
(a viewpoint to think from; not a roster role). The deck is a menu, not a checklist: one
game card per list, optionally combined with one roleplay card, is enough.

## QUICK START
1. Play only at a named moment (WHEN, below); never on routine work. After two failed
   attempts outside a review-ordered fix round: only the reality probe, one list.
2. Write the frame: the decision in two or three sentences, plus the plan as it stands.
3. Make two lists from the same frame: a plain one, and one with a card. The second runs
   in a fresh context (another agent, or a fresh subagent that never saw the first list);
   if that is not possible, label the round "not independent".
4. Each list has 3-5 items, each in four parts: mechanism | expected benefit | main
   failure mode | cheap test.
5. Merge the lists by mechanism. Someone who wrote neither list marks each item adopt,
   reject or already in the plan, with one line why.
6. Write the one-line score (KEEP SCORE). "Nothing useful" is an accepted result, and a
   game never decides scope. Checks run only on copies or test environments; never
   record a round in a challenge brief. The rest of this page is reference.

## WHEN to play
- **A design start:** a new design, way of working or initiative that will get a
  challenge. One card (optionally with one roleplay card), at most 10 minutes, before
  the challenge; in a team round, one per pair or group. It is meant to be named in the
  work order, with the time box, so nobody has to remember.
- **Two failed attempts on the same assumption, outside a review-ordered fix round:**
  only the reality probe (card 3). One list is enough here: the output is the probe and
  its result, with no plain pass and no merge.
- **Product ideas:** first ask plain experts (agents given the problem and an expert's
  brief, with no card), then play one card as a side round. In an earlier trial, ideas
  from plain expert prompts scored higher on worth and fit; game cards scored higher
  only on "unusual".

**Never** on routine work, a fix ordered by a review (including its fix rounds), a
release or a rollback, or every task. If you are unsure whether the moment counts, it
does not.

## EVERY ITEM: four parts
Each item a card, a roleplay card or the plain pass produces states, in a line or two:
- **mechanism** - what we would do or change;
- **expected benefit** - what gets better, for whom;
- **main failure mode** - how it most likely goes wrong or misleads us;
- **cheap test** - the smallest check, question or run that would tell.

*Example, in that order:* batch the per-row lookups into one query | page time stops
growing with the row count | one huge query on the largest account | time the page on a
copy of the largest account, before and after.

## METHOD: two independent lists, then merge
1. **Frame it.** Both passes get the same frame. Choose one game card, optionally with
   one roleplay card; rotate cards between rounds.
2. **Plain pass:** "List the 3-5 most important ideas for this plan", in four parts,
   with no card.
3. **Card pass:** the same frame plus the chosen card.
4. **Fresh context for the second pass.** Run it in another agent, or in a fresh subagent
   given only the frame and the card. An agent cannot un-read its own context, so a
   second pass in the same conversation is not independent; if no fresh context is
   possible, play anyway and label the round "not independent" in the score.
5. **Merge:** take the union and group items by their underlying mechanism, so
   paraphrases collapse into one (keep the clearer wording). Note each item's source:
   plain, card or both.
6. **Mark:** someone who wrote neither list (another agent, the lead or the plan's owner)
   asks four questions of each item, each on its own and never as one blended score: is
   it useful, is it new compared with the plan, is it feasible, can it be tested? Then
   marks it **adopt**, **reject** or **already in the plan**, with one line why. At most
   one uncertain item per round may be marked **wildcard** instead: it is neither adopted
   nor rejected, only its cheap test is run, and it is counted on its own. Hide the
   sources when that is cheap; keep the marking to one line per item.
7. **Hand over.** End with the smallest experiment that checks the adopted items (often
   their cheap tests). The plan's owner puts them into the plan. Record the round in the
   design or plan, never in a challenge brief, which must stay blind to the planner's
   preferred solution. A game never decides scope, never approves or blocks work, and
   never replaces the challenge or a review.

## WHERE CHECKS RUN
Any check, test or probe that a game card or roleplay card produces runs only on copies
or test environments the seat is already allowed to use (a copy of the data, a test
environment, its own worktree). Never against a live system, real accounts or anyone
else's systems. The attacker card and the red team never attack a real system.

## TEAM ROUND: pairs or small groups at one moment
For a large design start or a product-ideas side round, team members can play together:
1. **The lead names the moment and the target:** the frame from step 1 of the method.
2. **Each pair or small group chooses one game card** (optionally with one roleplay
   card) **or one roleplay format.** No two groups pick the same card or format in one
   round, so two or more different games run at the same time; never the whole deck at
   once. One group, or one extra agent, writes the plain list with no card.
3. **Play privately and independently,** with no talk between groups until every list
   is written. Seeing the first plausible idea anchors everything after it.
4. **Exchange and transform.** Each group takes items from another group's list and
   reworks them with its own card. Keep the originals too.
5. **Merge and mark** as in steps 5 and 6 of the method, by someone who wrote none of
   the lists.
6. **End with the smallest experiment** that can check the chosen ideas.

## THE GAME CARDS
Pick by the situation: the goal may be wrong (1, 8); everyone already knows the answer
(2, 7, 11, 12); a key assumption is untested (3); the problem feels new (4, 9); a good
idea was dismissed (10); size or lifetime matters (6); only the builder's view so far (5,
or a roleplay card). Card 13 needs a team.
1. **Change the question.** Restate the goal for the user, the operator and the system;
   which version changes what we build? *Example:* "faster export" becomes "the report
   before my morning meeting", which a scheduled export solves.
2. **Forbid the favourite.** Ban the mechanism everyone reaches for first, plan without
   it, then lift the ban and keep what the detour found. *Example:* without "add a
   cache", the page's one query per row shows up; batching it may be enough.
3. **Reality probe.** Name the smallest real run that settles the key assumption today
   (clean checkout, fresh database, another operating system, production-like data),
   then run it where checks may run (above). The only card after two failed attempts.
4. **Borrow a mechanism.** How does another field or tool solve this shape of problem?
   Test: explain it without the source's words, or it is decoration. *Example:* a circuit
   breaker is "after five failures in a row, pause calls for a minute, then try one".
5. **The absent person.** Concrete circumstances, not personas: a connection that keeps
   dropping, distrust of the service, never opening the tool unless made to, an auditor
   to answer to. Pull out one improvement that helps everyone.
6. **Extremes.** Ten times the size or a tenth, zero users or ten thousand, an hour or a
   year? *Example:* "in a year" asks who deletes the old logs.
7. **Pull one assumption.** List the assumptions everyone makes; each player pulls one
   out and asks what would have to exist for the plan to work without it. *Example:*
   without "the nightly job ends by morning", the page must show the date of its data.
8. **Change the representation.** Describe the problem as a queue, a market, a control
   loop, a negotiation, an information bottleneck or a coordination problem; each view
   must yield a different intervention (as a queue, slow reviews say: limit open work).
9. **Evidence from the future.** Write a short release note, support ticket, incident
   report or complaint ("I love it, but...") from a future where an unusual idea works,
   then work back to the mechanism that made it true.
10. **Rescue the rejected idea.** Name the circumstances in which a dismissed idea is the
    best option, adapt it, and name a cheap test. *Example:* "just use a spreadsheet"
    wins while one person makes a few edits a week; ship it and count the edits.
11. **The constraint lottery.** Draw two constraints and one advantage at random ("must
    work offline", "no new storage", "unlimited computing power"), solve under them,
    then keep only what also improves the real, unconstrained case.
12. **Deliberately terrible ideas.** Each player writes the worst plausible solution, then
    pulls one useful mechanism out of someone else's. *Example:* "email every error to
    everyone" yields "a person sees each new kind of error once".
13. **Mutation relay (team game).** One agent proposes a basic idea; each next agent
    transforms the latest version: invert, subtract a part, transfer, scale, combine or
    shift in time. Keep every version; the best may sit halfway.

## ROLEPLAY: the second axis
A roleplay card is a viewpoint to think from, not a roster role: playing one never
changes an agent's role on the team. A player may combine one roleplay card with one
game card ("the attacker plays Pull one assumption") or play it on its own.

**Roleplay cards.** Each gives circumstances, a goal and a constraint; never a stereotype
and never a real person's name.
- **The attacker:** an ordinary account and plenty of patience; wants data or money out
  without being noticed.
- **The auditor or regulator:** reviews it months later and must sign it off; accepts
  only what is written down and can be traced.
- **The on-call engineer woken at 3 a.m. by it:** no author to ask; needs it safe in
  minutes.
- **The new team member on day one:** must make a small change with only the docs and
  the code.
- **The maintainer two years from now:** must change it after its authors have left.
- **The person who has to undo it:** rolls it back under pressure, with new data already
  written.
- **A generic competitor (never a named company) explaining why they lost:** sees only
  what is visible from outside; names what they could not copy.
- **The budget owner:** must justify the running cost every quarter, within a fixed
  budget.
- **The support person answering the angry ticket:** must find out what happened without
  a developer.
- **The user who never reads instructions:** clicks the first plausible thing and must
  not lose work.
- **A domain expert:** knows the subject the code models (an accountant for money logic);
  spots the rules the code breaks.
- **The decision-maker:** the person who will decide (the operator, a budget owner, a
  sceptical executive at a demo). A real person is built only from their documented
  priorities and quoted statements, never from invented opinions; a generic one only
  from the circumstances on the card. Uses: (1) before asking the real person, predict
  their follow-up questions, so the question arrives complete; (2) read a report or plan
  as them (on a phone, short on time, not a programmer) and flag what they would not
  understand; (3) rehearse a demo for a decision-maker who doubts AI, and list the
  objections to prepare for. **Hard rules:**
  - the output is a prediction in the third person ("they would likely ask..."), labelled
    "simulated", never written in their voice and never a decision;
  - it never replaces asking the real person; anything that needs their decision still
    goes to them;
  - never drop, soften or hold back an option or a fact because the simulation disliked
    it: the real decision-maker sees every option, with the simulated objections attached
    and labelled;
  - it is never sent through a channel that carries their identity or authority (such as
    a message that claims to come from the operator or the lead);
  - play it in a fresh context, ideally on a different vendor, given only the quoted
    statements, so it does not just mirror the author's own assumptions; pass a real
    person's words to another vendor only when they are not confidential.

**Roleplay formats for a pair or group.** Each fits the team round, so different pairs can
play different formats at the same time. Each produces candidate items in four parts for
the marking step, never a verdict or an agreed design.
- **Steelman pair:** one argues for, one against, then they swap sides; the output is the
  strongest version of each side.
- **Courtroom:** prosecution, defence and a judge on one proposal; the judge lists the
  strongest point on each side and the evidence each would need, not a verdict.
- **Interview:** one plays a user in concrete circumstances; the other asks questions and
  may NOT propose solutions during the interview. Afterwards both list what surprised them.
- **Stakeholder council:** each player speaks for one stakeholder with a stated interest;
  together they propose a design and record its trade-offs. It is simulated, never the
  real stakeholders' agreement.
- **Red team / blue team:** red attacks the plan (misuse, failure, abuse) on paper or in
  a test environment, blue defends and patches; the output is the attacks that survived.

**Rules for roleplay.**
- A roleplay card produces hypotheses, not facts. Anything it claims about users,
  attackers or regulators is marked as an assumption and gets a cheap test before anyone
  acts on it.
- Items keep the four parts. Roleplay never decides scope.
- Never impersonate a real person or organisation. A labelled third-person prediction
  built from someone's own quoted words, as in the decision-maker card, is not
  impersonation.
- Give different roleplay cards to different vendors (say a Claude seat and a Codex
  seat) when you can: it is a cheap way to get views that really differ.

## NOT HERE: the challenger, the premortem and "do nothing"
The standing challenger viewpoint belongs to the challenge skill (agenttalk.challenge in
Claude Code, agenttalk-challenge in Codex): its "Case against" is the premortem, and its
"Alternatives, including doing nothing" covers subtracting the work. Do not rebuild them
here. A game feeds the plan before the challenge; it never answers whether the work
should be done at all.

## KEEP SCORE
After each round, write one line per list (the plain list counts as card `plain`) where
the team keeps notes for that decision, even when nothing was adopted:

```text
thinking-games | <date> | <decision, a few words> | card: <name> | roleplay: <card or format, or none> | independent: yes/no | items: <n> | adopted: <n> | adopted only from this list: <n> | wildcards: <n> | minutes: play <n>, mark <n>
```

Count the plays of each game card, roleplay card and format. Judge one only after at
least 5 plays: if none of them produced an adopted item that only it found, rework or
drop it, and say so in your report rather than quietly stop playing it. Below 5 plays,
report "not enough plays"; an unplayed card is not judged.

## WHAT THE EVIDENCE SAYS
Games like these are sometimes said to use "different neural pathways". That is a
metaphor nobody can check; the practical target is candidate solutions that really
differ. The evidence is small: one blind trial of three runs, using three of the game
cards (change the question, forbid the favourite, the absent person). The card list was
never judged better than the plain list, and once worse. The gain came from merging two
independent lists: in every run each list added adopted items the other missed. Marking
cost more agent time than playing. The roleplay cards, the formats, the team round and
the other ten game cards are untried. That a card makes the second list differ more
than a second plain list would is a hope to be measured with the score above, not a
finding.

## Evidence

A round feeds a plan and never approves anything. When it is part of other work (a
design, a plan, a work order), record it there and emit that work's evidence; never put
it in a challenge brief. Only when the round is the whole job, emit the
`planning-artifact` profile (full rules + bus-validated vs skill-policy:
../_shared/references/evidence.md). The marked items go in `proposal`, which evidence.md
accepts in place of `decision`: a round proposes, it never decides.

Required fields:

- `artifact_type`
- `scope`
- `decision` - or `proposal`; a round always fills `proposal` with the marks
- `assumptions`
- `alternatives`
- `risks`
- `required_reviews`
- `open_questions`
- `evidence`
