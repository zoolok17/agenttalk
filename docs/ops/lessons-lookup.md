# Lessons lookup: the one search a seat makes before real work

Applies to every seat that does build, fix or review work over agenttalk. This page is the
frozen policy. The lessons-use trial plan (PR #390) points here so that it measures reported
use under one unchanged rule.

## In plain words

Before a seat starts real work (building something, fixing something, reviewing something),
it looks once in the team's lessons for what earlier work learned about the same subject. It
picks one concrete word from the task, such as `junction` or `banner`, and searches for it.

- The search is for exact pieces of words. It does not understand sentences, so a pasted
  task description finds nothing. One short word does.
- It is one search, with one retry using a different word if the first found nothing. It is
  not repeated on every turn and it is not a full catch-up on everything the team knows.
- Quick turns skip it: an acknowledgement, a status question.
- What it finds is advice. A seat names a lesson in its reply only when that lesson changed
  something it did or checked, and says in a few words what changed. It never names lessons
  to look careful.
- The reply keeps three situations apart, because they mean different things:
  1. **Nothing found.** The search worked and no lesson matched.
  2. **The lookup failed.** The command did not run or returned an error. The seat says so.
     It is never written down as "found nothing".
  3. **Read, not useful.** Lessons came back but none changed anything.
- A failed or empty lookup never stops the work and never adds a new step or approval.

## The policy, frozen

| Item | Value |
| --- | --- |
| Name | Lessons lookup before substantive work |
| Decided | 2026-10-08, by the operator ("Yes, add the step for searching lessons before starting."), after challenge `ch-lessons-search-step-20261008` reshaped it |
| Takes effect | The day this page merges to master. The lead writes that date on the trial plan; the policy does not change after it |
| The command | `agenttalk knowledge search <term> --type lesson --limit 5` |
| Where it is stated | The wrapped-turn instructions (`src/agenttalk/wrapper/prompt.py`), the listen skill (Claude and Codex copies), one line in the lead skill's brief guidance |
| Reported through | `--meta lessons_used=<lesson ids or none>` on a reply sent by command (see "Reporting" below) |
| Changes only by | A new, dated version of this page and a new trial |

## Seats that already searched by habit

Before this page, six seats had a standing instruction of their own to run
`agenttalk knowledge search` before starting work (read from the seat instruction files on the
operator's machine on 2026-10-09; whether each seat really did so is not recorded anywhere,
see below):

- claude-agenttalk-developer-2
- claude-agenttalk-developer-6
- claude-agenttalk-frontend-dev
- claude-agenttalk-reviewer-3
- qwen-agenttalk-dev-1
- qwen-agenttalk-reviewer-1

The other seats had no such instruction in those files. A trial reading should treat the six
as "already doing it" and the rest as "newly told", and should not mix them without saying so.

## Reporting

- A seat that replies by command (`agenttalk reply ...`) can carry `--meta lessons_used=...`.
  Today it names the lessons as ids (or keys), or `none`. When the exact-citation form
  from #390 (`@id:<event id>`, `@none`) lands, this page's wording switches to it and nothing
  else here changes.
- A seat that replies through the draft file cannot carry that field. It names the lessons
  in the body and says so. This keeps #390's split between declaring seats (reply by command)
  and draft seats (draft file); nothing here changes the draft format.

## What this does not measure

A manual search leaves no record in the store: only the lessons chosen by the wrapper are
logged as shown (lesson `manual-knowledge-search-is-invisible-to-the-exposure-log`). So the
number of lookups cannot be counted from the store. The only evidence is what seats report in
`lessons_used`. A reading of the trial must say "reported use", not "searches made".

## What would make this policy wrong

Forced citations, an inbox command inside a wrapped turn, repeated broad searches, work
blocked by a failed lookup, or a quiet change of who is in the trial or what the rule says.
Any of these is a reason to stop and reopen the challenge, not to adjust quietly.

## Technical detail

- The lookup is `knowledge search` with `--type lesson --limit 5`; the same flags exist at
  the base this was built on (`agenttalk knowledge search --help`: `--type`, `--scope`,
  `--tags`, `--limit`, `--json`, and the stale and uncurated switches). No other flag is used
  or taught.
- Search is substring matching over lesson fields, not ranked by relevance, and `--limit 5`
  shows the first five of all matches. A term that is a part of a longer word matches that
  word too (the word `room` matches `headroom`).
- It is read-only and is not an inbox command: it does not touch the cursor. The ban on
  `sync`, `threads`, `drain`, `recv`, `wait` and `ack` inside a wrapped turn is unchanged.
- The wrapper already adds up to five lessons per turn, chosen from the message's kind,
  subject and meta (never the body). This lookup adds the part the wrapper cannot do: a
  search on a term taken from the body of the real task.
- Tests: `tests/test_lessons_lookup_text.py` checks the prompt and both skill copies.
