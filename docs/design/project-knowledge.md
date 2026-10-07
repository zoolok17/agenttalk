# Project knowledge: keeping what a team learned inside the project

Status: proposed. Nothing in this document is built yet. Where it describes how agenttalk works today, that part matches the code at release 0.98.0. Every example of proposed behaviour is marked "proposed, not runnable yet".

## In plain words

Agents on the bus learn things while they work: a trap in a test setup, a review mistake that keeps coming back, a way of working that saved time. Today those notes live in one hidden folder on one machine. A fresh checkout, a second machine or a new contributor starts with none of them.

This document proposes to keep the notes a project has agreed on inside the project's own repository, as small reviewed files, and to bring them onto a machine through a deliberate, authorised import. It also proposes to fold the best general lessons into the skills agenttalk ships, and to allow a hand-made, human-checked summary.

It decides the note format, who may accept an imported note, how retirement is kept safe, which lessons may be shown to agents, and how we will know the whole thing works. It leaves open the questions listed in section 12.

How to read it: sections 1 to 8 are the overview, in plain words. Sections 9 to 11 are the detailed contracts for the people who will build it, and are marked "Contract". Section 13 holds technical notes.

Audience: the operator and the team who will review and build this. Mode: explanation (a design).

## 1. The problem, with the numbers we have

These come from the pilot reported on issue #293. They are a small pilot and a second-team count, not a full census.

- The store sits in `.agenttalk/`, which git ignores. One machine holds the only copy of the curated notes.
- Across 19 decisions the pilot found no useful correction from injected lessons and six cases where a lesson crowded out advice the agent needed.
- 16 of the 18 lessons agents reported using came from a search they ran at the start of the work, not from the five lessons injected for them.
- 264 of 354 lessons carry no tags. Today an untagged lesson counts as relevant to every task, so a handful of them can fill the five slots whatever the job is.
- Manual searches leave no record in the exposure log, so the team cannot see which notes people actually read.

## 2. Three parts, in the order we build them

**B, first: project notes tracked in the project.** Notes the project has curated are written as files in the project's repository and reviewed in ordinary pull requests. They stay apart from bus history and from usage statistics.

**A, as skills: the universal layer.** General lessons that keep proving useful are promoted into the skills agenttalk already ships. There is no separate "universal lesson pack" competing for the five slots.

**C, manual only: a digest people prepare by hand.** A summary written on one machine and read by a person before it leaves. A program never scans a client's repository for it, and no raw project material leaves the repository it came from.

B comes first because A and C both depend on notes being easy to find, review and retire.

## 3. What a person will notice

- A project that adopts this has a folder of note files in its repository. Changing, adding or retiring a note is a pull request that people review.
- A new machine or a new contributor runs one import and gets the project's accepted notes. Nobody copies `.agenttalk/`.
- Tracked notes are looked up on request. They do not fill the five injected slots unless the project turns that on, and then only in slots left empty (section 4).
- Lessons without tags stop being injected into every turn. Existing teams get a report first and a release of warning before anything changes (section 4).
- A retired note stays retired, even when someone imports from an old clone (section 6).
- Searches people run by hand start to appear in the exposure log (section 8).

## 4. Which lessons an agent is shown (the selector)

Today an agent is shown at most five lessons per turn. A lesson is eligible if its scope is `process` or matches the task, and if it has no tags or one of its tags matches the task. Every lesson has a scope, so a lesson with no tags is always eligible, and `process` lessons rank first. That is how a few untagged lessons fill the slots.

### The one rule (proposed)

A lesson is shown to an agent in a turn only if all of these hold:

1. It is active: accepted, not retired, not expired, not stale.
2. Its scope is `process` or the scope of the task.
3. It has at least one tag, and at least one of its tags matches a tag of the task.
4. If it came from a project's tracked files, the project has switched on injection of tracked notes (default off), and it only takes a slot that no other eligible lesson wants. In other words, tracked lessons rank after every non-tracked eligible lesson.

A lesson that fails rule 3 is **lookup-only**: `knowledge search`, `knowledge pull` and `knowledge onboard` still show it. Nothing is deleted or rewritten.

### Examples (proposed, not runnable yet)

| Lesson | Task | Shown today | Shown under the rule |
|---|---|---|---|
| `process`, no tags | any task | yes, ranked first | no, lookup-only |
| `test`, no tags | a `test` task | yes | no, lookup-only |
| `process`, tags `release` | task tags `release`, `docs` | yes | yes |
| `process`, tags `release` | task tags `ci` | no | no |
| `test`, tags `ci` | a `docs` task with tags `ci` | no (scope differs) | no (scope differs) |
| tracked, tags `ci`, injection off | a `test` task with tags `ci` | not applicable | no, lookup-only |
| tracked, tags `ci`, injection on, all five slots wanted by others | a `test` task with tags `ci` | not applicable | no, no free slot |

### Rollout for existing teams

1. **Report first.** One release adds `knowledge lessons-report`, which lists every accepted lesson that would become lookup-only, with a suggested tag taken from its scope and words. Nothing changes in behaviour.
2. **Curators decide.** For each listed lesson a curator tags it (a new curation event; the old event stays in the history), retires it, or leaves it lookup-only. Accepted lessons are never rewritten silently.
3. **Switch.** The following release makes rule 3 the default. A store setting lets a team keep the old behaviour for one more release. Each turn that skips an untagged lesson counts it in the report, so the effect is visible.

## 5. How tracked notes look and travel (overview)

Notes are advice, not rules. They stay separate from skills and from the vendors' instruction files (section 10), and an agent reads them as context.

- **One file per note**, in a folder at the top of the project (proposed name `.agenttalk-knowledge/`). Two people adding different notes never touch the same file.
- **Each file is a complete record**: the text, when it applies, tags, expiry, what it replaces, where it came from, and its review state. Section 9 gives the exact format.
- **Files are claims, not approvals.** A file in a repository does not make a note accepted on a machine. A curator on that machine imports it, and the import records who accepted it and from which commit (section 9, "Trust").
- **Retirement is also a file change** (section 6).
- **Overrides are narrow.** A project can switch off a lesson it does not want. It cannot change the text of a shipped skill (section 7).

The travel path is:

1. A curator exports accepted and retired notes from the bus to the folder. This reuses the export and import already proposed in issue #293 part 2, with its provenance and confidentiality check. No second store and no second selector.
2. The folder change is a pull request, and the diff is the review.
3. On another machine, a curator runs the import against a trusted commit. Duplicates, conflicts and retirements follow the rules in sections 6 and 9.

## 6. Retirement: retired stays retired

A note retires through a reviewed change: its file gets the status `retired` and a short reason. The file stays in the folder. Deleting a file never retires a note, and never revives one.

Rules (detailed in section 9):

- **Retired beats accepted.** If a machine has seen a retirement, no import may turn that note back on, whatever the order of the commits.
- **Coming back is deliberate.** A retired note returns only by a new reviewed file that names the retirement it replaces, imported with an explicit `--reinstate <key>` by a curator.
- **A machine that never saw the retirement** cannot know about it from an old clone. So the import by default requires the source to be up to date with the project's main branch. An offline import is allowed only with `--offline`, and it prints in plain words that retirements after the source's date are unknown.

## 7. Overrides: what a project can and cannot switch off

- **Can:** switch off one lesson that the selector controls, by naming its key in an override record, with a reason. The override is reviewed like any note. It hides that lesson from this project's selections and from nothing else.
- **Cannot:** remove or change the text of a shipped skill. A skill is loaded by the vendor tool on its own, and no note can edit it, so the loader gets no skill-rewriting mechanism.
- **An advisory exception to a skill** (for example "in this project we do not follow the shipped advice on X") goes in the vendor's own project instruction file (section 10). That shows the exception to the agent; it does not suppress the skill, and the document says so wherever it is described.
- **Precedence**, highest first: a retirement; a project override; the lesson's normal eligibility under section 4.

## 8. Logging manual searches

Today the exposure log only accepts records made by a wrapped turn. A manual `knowledge search`, `pull` or `onboard` is invisible to it. Section 11 gives the schema change and its two-release transition.

In plain words: each manual lookup writes a small record of which notes it showed (by key and fingerprint, never by text) and who ran it. The text someone typed into a search is never stored.

At this head the wrapper is the only production writer of exposure records. `sync` selects lessons but does not record them.

## 9. Contract: the tracked note file, trust and import

### 9.1 File record (version 1, proposed)

- One note per file. UTF-8 without a byte-order mark, LF line endings, JSON with sorted keys, two-space indent and a final newline. JSON is chosen so the loader reuses the existing validators and needs no new parser.
- Location: `.agenttalk-knowledge/<domain_id>/<key>.json`. A `:` in a key is written as `%3A`, because it is not allowed in Windows file names. Keys that differ only in letter case are treated as duplicates.
- Identity is the pair (`domain_id`, `key`), as in the store. The file must repeat both; a file whose name and contents disagree is refused.
- `format` is required. A reader that does not know the number refuses the whole file with a message naming the file and the number, and loads nothing from it.

Example (proposed, not runnable yet):

```json
{
  "format": 1,
  "domain_id": "process",
  "key": "example-lesson-key",
  "type": "lesson",
  "body": "The insight, in behaviour terms.",
  "anchor": null,
  "lesson": {
    "scope": "test",
    "trigger": "when this applies",
    "evidence_ref": "a neutral reference",
    "owner": "an-agent-name",
    "status": "accepted",
    "applies_to": ["ci"],
    "supersedes": [],
    "review_after": "2027-01-01T00:00:00Z",
    "expires_at": "2027-06-01T00:00:00Z"
  },
  "retirement": null,
  "reinstates": null,
  "override": null,
  "provenance": {
    "curated_by": "an-agent-name",
    "curated_at": "2026-10-07T00:00:00Z"
  }
}
```

### 9.2 What "the same note" means

Two records are the same only if their **complete comparison payload** is equal: type, domain, key, body, anchor, and every lesson field (scope, trigger, evidence reference, owner, tags, supersedes, review date, expiry). The existing payload hash in `knowledge.py` already covers these and is reused. Review state (accepted or retired) is compared separately.

So a change of tags, trigger, expiry or supersedes is a changed note, not a no-op. A change that only touches provenance or review state is a metadata update: it is applied as a new curation event and does not republish the text.

### 9.3 Trust

- **Who may accept.** Only an actor who is a curator for the note's domain on the receiving bus, or the lead through the existing override. The author recorded in a file gives no authority. Imported records are appended by the importing curator, with the source recorded as provenance (repository, commit, path).
- **Trusted source.** Import reads files at a named commit that is reachable from the project's main branch, read from git, not from the working folder. A working folder with uncommitted edits is refused. An unreviewed branch is refused unless a curator passes `--as-proposed`, which imports every record as `proposed`.
- **Domains.** The `process` domain exists everywhere. Any other domain must exist in the receiving bus's registry. A record for an unknown domain is refused and listed, never dropped. On a clean machine the registry is empty, so project domains are created first, by the existing domain commands.
- **Anchors and the checkout root.** Today anchor checks resolve against the bus root. Tracked code notes therefore need a separate change that resolves anchors against an explicit checkout root (work order 6). Until it ships, tracked code notes import as `proposed` only, and this design does not claim to close issue #245.
- **Proposed versus accepted.** Files with status `accepted` import as accepted only under the rules above. Everything else imports as `proposed`.
- **Causal and repeatable.** Each record is appended as a publish event and then, if accepted, a curate event, in that order. An import that stops half way leaves a `proposed` note, never an accepted one without its publish. Running it again finds the same payload and source commit and does nothing.
- **Local proposals.** If the bus already holds a different proposed or accepted version of the same key, the import stops for that key and lists both. A curator chooses per key: keep local, take tracked, or leave both. Nothing is overwritten without that choice.
- **History is never rewritten.** Import and upgrade only append events. Existing events stay as they are.

### 9.4 Retirement rules for import

- A retired file imports as a retract event. Retired beats accepted for the same (`domain_id`, `key`), whatever the commit order.
- If the bus already holds a retraction for a key, any record that is not itself retired and does not carry a matching `reinstates` is refused. Today's store would otherwise allow a fresh publish and curation to reopen the key, so the importer must check this itself.
- Reinstatement is a reviewed file with `reinstates` naming the retirement it undoes, imported with `--reinstate <key>` by a curator.
- Import by default checks that the source commit contains the main branch's current tip, and refuses if not. `--offline` skips the check and prints the date of the source commit and the sentence "retirements made after this date are not known".

## 10. Contract: what agenttalk manages and what the vendors cover

Checked on 2026-10-07 against the vendors' public pages (Claude Code memory and skills pages; Codex AGENTS.md and skills pages).

- **Instruction files are context, not enforcement.** Claude Code treats `CLAUDE.md` and rules as context and says so; a real restriction needs permissions or a hook. A project that needs an enforced rule uses permissions or hooks, not a note or an instruction file.
- **Claude Code skills.** Project skills live in `.claude/skills/<name>/SKILL.md`. A personal skill of the same name wins over a project skill (enterprise wins over both).
- **Claude Code rules.** `.claude/rules/*.md`, optionally limited to matching paths. Loaded as context.
- **Claude Code and `AGENTS.md`.** By default Claude reads `AGENTS.md` only when there is no `CLAUDE.md`, `.claude/CLAUDE.md` or `CLAUDE.local.md` in the working folder or above it. Reading both needs the "Project instructions" setting `claude-md-and-agents-md`, or an import from `CLAUDE.md`. Reading `AGENTS.md` directly needs Claude Code 2.1.277 or later, and some sessions cannot read it. A project must not assume both files load.
- **Codex instructions.** It reads `AGENTS.md` from the Git root down to the working folder, with a combined default limit of 32 KiB.
- **Codex project skills.** Codex scans `.agents/skills` in every folder from the working folder up to the repository root, then `$HOME/.agents/skills` and `/etc/codex/skills`. Skills with the same name are not merged.
- **A gap to check.** `install-skills` copies skills for Codex into `~/.codex/skills`. The Codex page I read lists `$HOME/.agents/skills`. I did not verify whether Codex also still reads the older folder; work order 7 settles it before any promotion relies on it.

The split:

- **Vendors' mechanisms** own project instructions, rules and project-specific skills. agenttalk writes none of them in a project.
- **agenttalk** owns notes (advice with provenance and review) and the shipped skills it installs.

## 11. Contract: logging, confidentiality and promotion

### 11.1 Manual lookup logging (proposed)

- New `surface` values: `manual_search`, `manual_pull`, `manual_onboard`, in the same file and event family. The reader's validator is changed to accept them.
- **Actor.** The resolved sender (`--from` or `AGENTTALK_SELF`). If none resolves, nothing is logged and the command prints a one-line notice. The turn-identity and prompt-hash fields stay required for `wrapper_turn` only.
- **Result identity.** Each shown note is recorded as its key, domain, type and fingerprint. Code notes are included with their type. No body, no query text and no path is stored; only `has_query` and the kinds of filter used.
- **Size.** One lookup can show many notes, so it writes events of at most 50 items sharing a random `lookup_id` and carrying `part` and `parts`. An empty result writes one event with zero items.
- **Two-release transition.** Release N ships a reader that accepts the new surfaces and skips what it does not know. Release N+1 turns the writers on. Older readers are the reason for the gap; the exercise in section 12 tests an old reader against a log written by the new writer.
- Old `wrapper_turn` events stay valid and unchanged.

### 11.2 Confidentiality

Two separate policies:

- **Same-project sync** (the project's own repository and bus). The complete record is kept, including provenance. A forbidden-strings check still runs over the whole record, to catch another project's names pasted by mistake.
- **Outward sharing** (a public skill-promotion PR, the digest, a cross-project export). Only `process` lessons may leave, as in issue #293. The check covers the complete outgoing record: text, key, trigger, evidence reference, owner, curator, tags and retirement reason. Outward provenance is sanitised (neutral evidence label, role instead of agent name). The private provenance stays in the local store and is never exported.
- **Human clearance.** The tool writes outward material only to a local output folder and never pushes. A named person reads every line and records "cleared" in the pull request before it is opened. A scope label or a forbidden-strings list does not count as clearance.

### 11.3 Promotion into skills, and adoption order

`install-skills` keeps a file that differs from the shipped one unless forced, and it cannot tell an old shipped copy from a personal edit. Retiring a lesson in the same release as the skill change would therefore leave some machines with neither.

- **Release N** ships the new skill text. The lesson stays active.
- **A shipped manifest** lists the hash of every released skill file. `install-skills` then overwrites a file that matches an older release (untouched) and keeps one that matches none (a personal edit).
- **Visible warning.** For every kept file that lacks promoted advice, `install-skills` prints the path and the lesson keys it is missing.
- **Release N+1 or later** retires the lesson, marked "promoted to skill X in release N". Retired lessons stay searchable, so a machine with a kept local edit can still find the advice.
- **Local edits** stay the owner's decision. The warning is the only nudge.

### 11.4 Promotion inputs and review

Inputs: `process` lessons ranked by `lessons_used` counts, how often they were shown, and how long they have held. The lead proposes a short list at release time and the operator approves it. The pull request edits the skill text and later retires the lesson; the reviewer checks that the skill gets shorter or clearer. A human clears the text under 11.2 before the pull request is opened.

## 12. Evidence gates, work orders and open questions

### Exercises (done before wider use)

1. **Fresh checkout.** Clone to a clean folder, run the import against the trusted commit, confirm the accepted notes load without copying `.agenttalk/`.
2. **Conflicts.** Two contributors edit the same note on different branches; confirm the conflict is visible and not silently settled.
3. **Retired stays retired, new machine.** Import from a clone made before a retirement: confirm the import refuses without `--offline`, and with `--offline` prints the staleness sentence.
4. **Retired stays retired, known retirement.** On a machine that saw the retirement, import an older source: confirm the note stays retired.
5. **Metadata-only update.** Change only tags or expiry: confirm it is reported as a change, not a no-op.
6. **Unapproved file.** A file that is uncommitted, or only on an unreviewed branch: confirm it is refused or imported as `proposed`.
7. **Interrupted import.** Stop the import between publish and curate: confirm a rerun completes with no duplicate and no accepted note lacking its publish.
8. **Old readers.** An older reader meets a version-2 exposure log, and an older loader meets an unknown file `format`: confirm both fail clearly or skip, not corrupt.
9. **Old skill, new retirement.** Keep an edited skill, install the release that retires the lesson: confirm the warning names the missing advice and the lesson is still found by search.
10. **Override.** Switch off one lesson: confirm it is hidden here only, and that no shipped skill text changes.

### Kill signals

We stop or reshape if, over 2 to 4 weeks, any of these happens: a project's policy is overwritten; retired advice returns; generic advice crowds out needed local guidance; raw project material leaves its repository; merge conflicts or duplicate keys keep recurring.

### Two different measures

- **Adoption:** tracked or imported notes make up at least about 1 in 10 of the lessons agents report using. This shows the notes are used, not that they help.
- **Prevented mistakes:** the pilot's displaced-needed-advice count stays at zero, and a review finding in a class covered by a tracked note does not recur. This shows the notes help. The operator picks which one gates the work (question 4).

### Work orders, in order

Sizes are engineer-days for one builder plus one review round, assuming the existing store and event code are reused and no new dependency is added. They are rough, plus or minus half, and are to be re-estimated after questions 1 to 3 are answered.

| # | Work | Days |
|---|------|------|
| 1 | Lessons report, then the selector rule and the setting (section 4) | 3 |
| 2 | Exposure reader accepts new surfaces; then manual-lookup writers one release later (11.1) | 5 |
| 3 | Note file format, export of accepted and retired notes, confidentiality check on whole records (9.1, 9.2, 11.2) | 5 |
| 4 | Import: trust, domains, causal and repeatable append, reconciliation, retirement and reinstatement (9.3, 9.4) | 9 |
| 5 | Override records for lessons (7) | 2 |
| 6 | Resolve anchors against an explicit checkout root (issue #245) | 4 |
| 7 | Shipped skill manifest, install-time warning, Codex skills folder check (11.3, section 10) | 4 |
| 8 | The ten exercises, written up | 4 |
| 9 | Promotion run, by hand, per release (11.4) | 1 per release |
| 10 | Digest template and clearance checklist (11.2) | 1 |

About 37 days before the first promotion. Items 1 and 2 stand alone and can ship first. Items 3 and 4 are the core of B.

### Questions for the operator

1. Folder name and place: is `.agenttalk-knowledge/` at the project top the right home?
2. Should a project ever turn on injection of its tracked notes, and is "only into free slots" acceptable?
3. Who curates for a project with several contributors: one named person, or any maintainer through a pull request?
4. Which measure gates the work: adoption, prevented mistakes, or both?
5. Is a store setting for one more release of the old behaviour acceptable in the selector rollout, or should it switch at once?
6. If Codex no longer reads the folder `install-skills` writes to, is fixing that a blocker for promotion?

## 13. Technical notes (for builders)

- Today's code: notes are events in `.agenttalk/knowledge/notes.jsonl` (`knowledge.py`), folded to a current view per (domain id, key). Lessons carry a scope, tags, status, review and expiry dates (both required), and `supersedes`. A retract is terminal until a fresh publish and curation reopen the key (`resolve_views_with_problems`), which is why import must check retractions itself.
- Curation authority is enforced in `cmd_knowledge` in `cli.py`. A missing domain registry reads as empty (`domains.py`), and anchor checks resolve against the store root.
- The selector is `select_lessons` and `rank_lessons` in `lesson_context.py`. The clause to change is the empty-tag allowance. `exposure_event_problem` accepts only `surface == "wrapper_turn"` with turn identity, a prompt-block hash and one to five lessons.
- `install_skills.py` compares file bytes (`filecmp`) and skips a differing file without `--force`. It already has a warning-only check for retired skills, which is the place for the missing-advice warning.
- The loader reuses `knowledge.event_problem` and the payload hash rather than adding a second reader.
