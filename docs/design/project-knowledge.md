# Project knowledge: keeping what a team learned inside the project

Status: agreed design, answered by the operator on 2026-10-07; only work orders 1-2 are authorised so far. Nothing in this document is built yet. Where it describes how agenttalk works today, that part matches the code at release 0.98.0. Every example of proposed behaviour is marked "proposed, not runnable yet".

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
- 264 of 354 lessons carry no tags. Today an untagged lesson still has to pass the scope check, but every untagged `process` lesson passes it for every task, and `process` lessons rank first. A handful of them can therefore fill the five slots whatever the job is.
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
- Lessons without tags stop being injected. Existing teams get a report first and a release of warning before anything changes (section 4).
- A retired note stays retired, even when someone imports from an old clone. The one exception is a lesson retired because a skill now carries its advice: it stays active on a machine until that machine has the skill (sections 6 and 11.3).
- Searches people run by hand start to appear in the exposure log (section 8).

## 4. Which lessons an agent is shown (the selector)

Today an agent is shown at most five lessons per turn. A lesson is eligible if its scope is `process` or the task's scope, and if it has no tags or one of its tags matches the task. `process` lessons rank first.

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
| `test`, no tags | a `docs` task | no (scope differs) | no |
| `process`, tags `release` | task tags `release`, `docs` | yes | yes |
| `process`, tags `release` | task tags `ci` | no | no |
| `test`, tags `ci` | a `docs` task with tags `ci` | no (scope differs) | no (scope differs) |
| tracked, tags `ci`, injection off | a `test` task with tags `ci` | not applicable | no, lookup-only |
| tracked, tags `ci`, injection on, all five slots wanted by others | a `test` task with tags `ci` | not applicable | no, no free slot |

### Superseded lessons are qualified by domain (work order 1)

Today `lesson_superseded_keys` in `knowledge.py` collects the `supersedes` entries of every accepted lesson into one set of bare keys. If domains A and B both hold a lesson with key `x`, an accepted lesson in A that supersedes `x` also makes B's unrelated `x` stale. The fix, part of work order 1: the function returns (domain, key) pairs, and a bare key in a lesson's `supersedes` list means a key in the same domain as the lesson that holds it. Supersession across domains is not allowed. The stored events and the file format keep bare keys, so nothing existing is rewritten, and the selector, `pull` and the lessons report all read the pairs.

### Where "came from tracked files" is known (rule 4)

Rule 4 needs to know that a lesson was imported from a project's files. That fact is kept in the import log (section 9.3), which does not exist until work order 4. Work order 1 builds the rule with an origin check that reads the log, and until the log exists no lesson counts as tracked, so rule 4 changes nothing yet.

### Rollout for existing teams

1. **Report first.** One release adds `knowledge lessons-report`, which lists every accepted lesson that would become lookup-only, with a suggested tag taken from its scope and words. Nothing changes in behaviour.
2. **Curators decide, by publishing a changed version.** Tags are part of a lesson's fixed content, and the store refuses a curation whose content differs from the note it approves. So a tag change is two steps: the curator publishes the lesson again with the new tags (copying everything else, including both dates), then curates that exact publication. Until the second step, the previous accepted version, with its old tags, stays the active one and nothing in agents' turns changes. The earlier events are never edited. A curator may instead retire the lesson, or leave it lookup-only.
3. **Switch.** The following release makes rule 3 the default. A store setting lets a team keep the old behaviour for one more release. Each turn that skips an untagged lesson counts it in the report, so the effect is visible.

## 5. How tracked notes look and travel (overview)

Notes are advice, not rules. They stay separate from skills and from the vendors' instruction files (section 10), and an agent reads them as context.

- **One file per note**, in a folder at the top of the project (proposed name `.agenttalk-knowledge/`). Two people adding different notes never touch the same file.
- **Each file is a complete record**: the text, when it applies, tags, expiry, what it replaces, where it came from, and its state (accepted or retired). Section 9 gives the exact format.
- **Files are claims, not approvals.** A file in a repository does not make a note accepted on a machine. A curator on that machine imports it, and the import records who accepted it and from which commit (section 9).
- **A project is bound to a bus on purpose.** A curator tells the receiving bus which repository, which main branch and which checkout folder belong to the project, once (section 9.5).
- **Retirement is also a file change** (section 6).
- **Overrides are narrow.** A project can switch off a lesson it does not want. It cannot change the text of a shipped skill (section 7).

The travel path is:

1. A curator exports accepted and retired notes from the bus to the folder. This reuses the export and import proposed in issue #293 part 2, with its provenance and confidentiality check. No second store of notes and no second selector.
2. The folder change is a pull request, and the diff is the review.
3. On another machine, a curator runs the import against the latest commit of the project's main branch. Duplicates, conflicts and retirements follow section 9.

## 6. Retirement: retired stays retired

A note retires through a reviewed change: its file gets the state `retired`, a reason and a retirement identifier. The file keeps the note's full text and stays in the folder. Deleting a file never retires a note, and never revives one.

- **Retired beats accepted.** If a machine has seen a retirement, no import may turn that note back on, whatever the order of the commits.
- **Coming back is deliberate.** A retired note returns only by a new reviewed file that names the retirement it undoes, imported by a curator with `--reinstate`. After that, an old copy of the retirement is ignored.
- **A machine that never saw the retirement** cannot learn about it from an old clone. So the import asks the project's real remote for its latest commit and refuses if the source is behind. Offline, the design guarantees only what section 9.6 says.
- **The one exception** is a lesson retired because a skill now carries its advice. On a machine whose installed skill does not have that advice yet, the retirement waits and the lesson stays active, so the advice is never lost (section 11.3).

## 7. Overrides: what a project can and cannot switch off

- **Can:** switch off one lesson that the selector controls, by naming it as (domain, key) in an override record, with a reason. The override is reviewed like any note and imported by a curator. It hides that lesson from this project's selections and from nothing else.
- **Cannot:** remove or change the text of a shipped skill. A skill is loaded by the vendor tool on its own, and no note can edit it, so the loader gets no skill-rewriting mechanism.
- **An advisory exception to a skill** (for example "in this project we do not follow the shipped advice on X") goes in the vendor's own project instruction file (section 10). That shows the exception to the agent; it does not suppress the skill.
- **Precedence**, highest first: a retirement; a project override; the lesson's normal eligibility under section 4.

## 8. Logging manual searches

Today the exposure log only accepts records made by a wrapped turn. A manual `knowledge search`, `pull` or `onboard` is invisible to it. Section 11.1 gives the schema change and its two-release transition.

In plain words: each manual lookup writes a small record of which notes it showed (by key and fingerprint, never by text) and who ran it. The text someone typed into a search is never stored.

At this head the wrapper is the only production writer of exposure records. `sync` selects lessons but does not record them.

## 9. Contract: the tracked note file, trust and import

### 9.1 File record (format 1, proposed)

- One note per file. UTF-8 without a byte-order mark, LF line endings, JSON with sorted keys, two-space indent and a final newline.
- Location: `.agenttalk-knowledge/notes/<domain_id>/<key>.json`. Each path component is encoded so it can be created on every system, and the encoding is reversible: `:` is written `%3A`; a trailing `.` is written `%2E`; and a component whose name before its first `.` is a Windows device name (`CON`, `PRN`, `AUX`, `NUL`, `COM1` to `COM9`, `LPT1` to `LPT9`, in any letter case) has its first letter written as `%` and two hex digits, so a key `CON` is stored as `%43ON.json`. The importer decodes a path before comparing it with the identity inside the file. Keys that differ only in letter case are duplicates and are refused, because a case-insensitive disk cannot hold both.
- Identity is the pair (`domain_id`, `key`), as in the store. The file repeats both; a file whose name and contents disagree is refused.
- `format` is required. A reader that does not know the number refuses that file, names it and the number, and loads nothing from it.
- Overrides are separate files under `.agenttalk-knowledge/overrides/` (section 9.7). The folder also holds `project.json` (section 9.5).

Fields, and the three variants of the `state` block:

| Field | Meaning |
|---|---|
| `format` | `1` |
| `domain_id`, `key`, `type`, `body` | as in the store; `type` is any note type |
| `lesson` | present for lessons only: scope, trigger, evidence reference, owner, tags, supersedes (bare keys, read as keys in the same domain), review date, expiry (both required, as today), and the optional nested `anchor`, which the store already accepts and binds into a lesson's content |
| `anchor` | present for code notes (seam, gotcha, decision, pointer), as today |
| `verified_against_sha` | for code notes, and for lessons with a path or symbol anchor: the full commit of the project's code at which the note was last verified, copied by the export from the source note. The staleness check marks every path- or symbol-anchored note without it as stale, so import keeps it and a record that lacks it is imported as proposed only (section 9.4). It is not part of `content_id` |
| `supersedes_key` | optional key of a note this one replaces |
| `content_id` | the portable content hash (section 9.2) |
| `state` | `accepted` or `retired` |
| `reviewed` | who accepted it and when. For lessons this corresponds to the lesson's curator; for code notes it is the only review record, since they have no lesson block |
| `retirement` | `null` while accepted. When retired: `retirement_id`, `retired_at`, `retired_by`, `reason`, and optionally `promoted_to` (a skill name and the `content_id` of the promoted text, section 11.3) |
| `reinstates` | a list, empty unless the note has been brought back. It holds the `retirement_id` of **every** retirement occurrence this note has ever had undone, carried forward in each later file, so a current file after two retire-and-reinstate cycles lists both |
| `source_provenance` | original author, creation date and evidence reference; kept for people, never compared |

`retirement_id` names one retirement **occurrence**, not the advice. It is a random identifier created once, when the retirement is reviewed, and written into the retired file. Copies and replays carry the same value, so a later file can name it, and it does not depend on `content_id`. Retiring the same text a second time creates a new identifier.

A retired file also carries `retirement_rev`, the hash of the behaviour-affecting part of its retirement block (today only `promoted_to`). If a reviewed retirement is later corrected, its `retirement_id` and `content_id` stay the same but `retirement_rev` changes, which tells the importer to re-evaluate it (section 9.4).

A retired file keeps the full content, because a receiving store that has never seen the note must be able to publish it before retiring it (section 9.4).

Example (proposed, not runnable yet):

```json
{
  "format": 1,
  "domain_id": "process",
  "key": "example-lesson-key",
  "type": "lesson",
  "body": "The insight, in behaviour terms.",
  "lesson": {
    "scope": "test",
    "trigger": "when this applies",
    "evidence_ref": "a neutral reference",
    "owner": "an-agent-name",
    "applies_to": ["ci"],
    "supersedes": [],
    "review_after": "2027-01-01T00:00:00Z",
    "expires_at": "2027-06-01T00:00:00Z",
    "anchor": null
  },
  "content_id": "<64 hex characters>",
  "state": "accepted",
  "reviewed": {"by": "a-curator-name", "at": "2026-10-07T00:00:00Z"},
  "retirement": null,
  "reinstates": [],
  "source_provenance": {
    "author": "an-agent-name",
    "created_at": "2026-09-01T00:00:00Z"
  }
}
```

### 9.2 Two hashes, and what each is for

The existing hash in `knowledge.py` covers the note's text, anchor and lesson content, **and** its author, creation time and the id of the event it replaces. Those three differ on every machine, because each import creates a new local publication. So a file can never be compared to a local note with that hash.

- **`content_id` (portable, in the file).** Hash of: type, domain, key, text, anchor, `supersedes_key`, and the lesson content (scope, trigger, evidence reference, owner, tags, supersedes, review date, expiry, and the nested lesson anchor). It leaves out author, creation time, event ids, state, curator, `verified_against_sha` and provenance. Two lessons that differ only by their nested anchor therefore have different `content_id`s. Two records with the same `content_id` are the same note.
- **Local `payload_hash` (existing).** Used only for the store's own publish-then-curate chain, never written into a file.

So a change of tags, trigger, expiry or supersedes changes `content_id` and is a changed note. A change that only touches `reviewed` or `source_provenance` leaves `content_id` alone and is a metadata update: nothing is republished, and the importer only appends the missing approval step if one is needed. A change of `state` is **not** a metadata update: accepted to retired appends a retraction, and retired to accepted is a reinstatement. Both follow the table in section 9.4.

### 9.3 Trust

- **Who may accept.** Only a curator for the note's domain on the receiving bus, or the lead through the existing override. The author or reviewer named in a file gives no authority. The importing curator appends the events; the file's original author is kept in the import log, not in the event.
- **Mapping into events.** The local publication takes author = importing curator, creation time = import time, and no replaced-event id (`supersedes_key` is kept). The lesson owner comes from the file. A lesson's curator is filled by the approval step, as today.
- **Trusted source.** Import reads files from git at one named commit, not from the working folder. A working folder with uncommitted edits to the notes folder is refused. The commit must be the project's latest main-branch commit as section 9.5 defines it. A different commit is allowed only with `--as-proposed`, which imports everything as proposed.
- **Domains.** The `process` domain exists everywhere. Any other domain must be listed in the project binding (section 9.5) and exist in the receiving bus's registry. A record for any other domain is refused and listed, never dropped. On a clean machine the registry is empty, and today's `domain` command is read-only (`list`, `show`, `check-path`, `validate`), so there is no command that creates a project domain. How domains get created is an open decision (see "Open before work order 4"); until it is closed, the fresh-checkout exercise is limited to the `process` domain.
- **Anchors and the checkout root.** Today anchor checks resolve against the bus root. Tracked code notes therefore need a separate change that resolves anchors against the bound checkout root (work order 6). Until it ships, tracked code notes import as proposed only, and this design does not claim to close issue #245.
- **The import log.** The existing event format has no room for provenance, and it is not changed. Instead the import keeps `imports.jsonl`, next to `notes.jsonl`: source commit, path, `content_id`, `retirement_rev`, the local event ids written, the steps done, and the retirement identifiers reinstated. It is append-only and kept by reset, like the notes. It holds decisions about notes, not notes: the store of notes stays `notes.jsonl`, and the state of a note is always read from its events.
- **Crash-consistent writes.** The events and the log are two files and no lock makes two files atomic, so the order is fixed. Under the store's shared lock the importer (1) generates the event ids it will use and appends an **intent** line to the log naming them and the steps planned; (2) appends the events; (3) appends a **completion** line. A record counts as complete only when its completion line exists. A rerun that finds an intent without a completion checks each planned event id: it appends only the events that are missing, then writes the completion line. It never reads an intent as success, and it never reads events alone as complete, so the origin and reinstatement facts cannot be lost between steps. A change made by another curator between the intent and the rerun is detected by the state check and stops the rerun for that key.
- **History is never rewritten.** Import only appends events and log lines.

### 9.4 Append sequences (what an import does in each state)

The import reads the note's state on the receiving bus from its events, compares `content_id`, and appends only the steps that are missing. It does nothing only when the target state is already complete.

| Receiving bus holds | File says accepted | File says retired |
|---|---|---|
| nothing | publish, then approve (curate) | publish the file's content, then retract it |
| a publication that was never approved, same `content_id` | approve it only (this is the retry after an interrupted import) | retract it |
| a publication that was never approved, different `content_id` | conflict (below) | publish the file's content, then retract it |
| accepted, same `content_id` | nothing | retract it |
| accepted, different `content_id` | conflict (below) | publish the file's content, then retract it |
| retired, and the file's `retirement_id` matches the retirement here | refused: stays retired, unless the file's `reinstates` list names that `retirement_id` and a curator passes `--reinstate`; then publish and approve, and log every identifier in the list as reinstated | nothing, unless `retirement_rev` differs from the logged one, in which case the retirement is re-evaluated (below) |
| accepted after a reinstatement here, and the file's `retirement_id` is in the log's set of reinstated identifiers (the union of every `reinstates` list imported so far) | as the accepted rows above | ignored as an old retirement; reported |
| accepted after a reinstatement here, and the file's `retirement_id` is **not** recorded as reinstated (a second retirement, even of identical text) | as the accepted rows above | retract it: this is a new retirement and it applies |

**Retirements that depend on a skill, and corrected retirements.** For a retirement with `promoted_to`, the row above applies only if the local accepted lesson has the same `content_id` as the one named in `promoted_to` and the skill check in section 11.3 passes. If the local lesson has a different `content_id`, the case is a conflict, not a retraction: the skill carries different advice from the one this machine holds, so retracting it would leave the machine's own version nowhere. If `retirement_rev` of a file differs from the logged one for the same `retirement_id`, the importer re-runs the checks with the new metadata: a retirement that gained `promoted_to` is applied or deferred under 11.3, and a retirement that lost it becomes an ordinary retirement. The new `retirement_rev` is logged either way.

**Code notes without a baseline.** A tracked code note, or an anchored lesson, whose record lacks `verified_against_sha` is imported as proposed only, because the staleness check would otherwise mark it stale and hide it from search. A record that has it keeps it as its baseline.

Why a retired record is first published: the store refuses a retraction that has no earlier event for the same note, so retiring into a fresh store needs the publication first. It is the file's own content, appended as an unapproved publication and retracted at once; it is never shown to agents.

**Conflict.** The store has one current view per (domain, key), so there is no "keep both". A curator chooses per key:

- **take tracked:** publish the file's content, then approve it. The earlier accepted version stays active until that approval is appended;
- **keep local:** append nothing and record "kept local" in the import log;
- **decide later:** append nothing; the key is listed again on every import until decided.

**Records that arrive as proposed** (imported with `--as-proposed`, for an unknown commit, or code notes before work order 6) are appended as unapproved publications. They never reach agents' turns, are visible with `--include-uncurated`, and are listed in every import report with the reason. A later import from the trusted commit finds the same `content_id` and appends only the approval. A curator can retire a proposed record with a reason.

### 9.5 Binding a project to a bus, and freshness

A file cannot name its own trust anchor, so the binding is created on the receiving bus by a curator, once, and stored in the bus configuration, not in the repository:

`knowledge bind --source <repository address> --main-ref <branch reference> --checkout-root <folder> --domains <list>` (proposed, not runnable yet).

- The repository's `project.json` holds only a random `project_id`, created once. The bind command records it with the address, the main reference, the checkout folder and the allowed domains.
- Import refuses a source whose `project_id` is not the bound one, and a file whose domain is not in the bound list. This is also how the confidentiality policy (11.2) knows what "same project" means.
- **How the current tip is obtained.** At import time the importer asks the bound address for the current value of the bound main reference (the equivalent of `git ls-remote`). The source commit must be that commit, or an ancestor of it that the clone also contains with nothing newer touching the notes folder. If the clone lacks the remote's tip, the import says "fetch first" and stops.
- If the remote cannot be reached, the default is to refuse.

### 9.6 What the design guarantees offline

With `--offline`, the importer skips the remote check and prints the source commit's date and the sentence "retirements made after this date are not known". The guarantees are only these:

- a retirement the receiving bus already recorded is never undone;
- the import is consistent with the clone as it stands;
- nothing newer than the clone can be known.

A stale clone on a machine that has never seen a retirement can therefore import the old accepted note. That is a stated limit, not a defect we can remove without the network. It is why the default refuses.

### 9.7 Overrides

An override file has its own random `override_id`, names its target as (`domain_id`, `key`) and gives a reason. It is imported by a curator and recorded in the import log; the selector reads the active overrides from there. An override of a key that does not exist yet is kept and applies when the key appears. An override has no effect on shipped skill text (section 7). Withdrawing an override is a new override file with `state` set to `withdrawn` and a `withdraws` list naming the `override_id` of every override occurrence it withdraws (carried forward like `reinstates`). The import appends a withdrawal line to the log, and the old line is never deleted. An override whose `override_id` is in the log's withdrawn set is ignored whenever it arrives, even on a bus that sees the withdrawal first. A later, new override of the same target has a new `override_id` and applies normally; a withdrawal never acts as a tombstone for the target.

## 10. Contract: what agenttalk manages and what the vendors cover

Checked on 2026-10-07 against the vendors' public pages (Claude Code memory and skills pages; Codex AGENTS.md and skills pages).

- **Instruction files are context, not enforcement.** Claude Code treats `CLAUDE.md` and rules as context and says so; a real restriction needs permissions or a hook. A project that needs an enforced rule uses those, not a note or an instruction file.
- **Claude Code skills.** Project skills live in `.claude/skills/<name>/SKILL.md`. A personal skill of the same name wins over a project skill (enterprise wins over both).
- **Claude Code rules.** `.claude/rules/*.md`, optionally limited to matching paths. Loaded as context.
- **Claude Code and `AGENTS.md`.** By default Claude reads `AGENTS.md` only when there is no `CLAUDE.md`, `.claude/CLAUDE.md` or `CLAUDE.local.md` in the working folder or above it. Reading both needs the "Project instructions" setting `claude-md-and-agents-md`, or an import from `CLAUDE.md`. Reading `AGENTS.md` directly needs Claude Code 2.1.277 or later, and some sessions cannot read it. A project must not assume both files load.
- **Codex instructions.** It reads `AGENTS.md` from the Git root down to the working folder, with a combined default limit of 32 KiB.
- **Codex project skills.** Codex scans `.agents/skills` in every folder from the working folder up to the repository root, then `$HOME/.agents/skills` and `/etc/codex/skills`. Skills with the same name are not merged.
- **The folder `install-skills` uses for Codex.** It writes to `~/.codex/skills`. The current guide lists `$HOME/.agents/skills`, and the vendor's December 2025 changelog documents the older `.codex/skills` location. In the lead's session with Codex 0.160.0 the skills under `~/.codex/skills` were loaded, and the cross-vendor reviewer corroborated this (the installed package reports 0.160.0 and the skill files there were readable). I have not run it myself. This shows 0.160.0 works with the current install folder; it does not prove every version or platform. **Decision: no migration and no second install folder.** A version-specific verification task (work order 7) re-checks the folder whenever the Codex version changes, and before any promotion relies on it.

The split:

- **Vendors' mechanisms** own project instructions, rules and project-specific skills. agenttalk writes none of them in a project.
- **agenttalk** owns notes (advice with provenance and review) and the shipped skills it installs.

## 11. Contract: logging, confidentiality and promotion

### 11.1 Manual lookup logging (proposed)

- **Schema version 2** is used for the new records. The exposure reader today requires `schema_version` 1 and the surface `wrapper_turn`. The new reader accepts versions 1 and 2. Existing `wrapper_turn` events stay version 1 and valid, unchanged.
- New `surface` values: `manual_search`, `manual_pull`, `manual_onboard`, in the same file.
- **Oldest supported reader.** Release N ships a reader that accepts version 2 and skips what it does not know. Release N+1 turns the writers on. A reader older than N meets version-2 lines as malformed lines, which the reader reports in its problem list rather than failing; it does not read them, and that is the accepted limit for readers older than N.
- **Actor and recipient.** Manual `search`, `pull` and `onboard` gain `--from <agent>` for the actor, defaulting to `AGENTTALK_SELF`. If none resolves, nothing is logged and the command prints a one-line notice. `onboard --for <agent>` stays a label for whom the digest is for, today only a label; it is logged as a separate `recipient` field and is never taken as the actor. The turn identity and prompt-hash fields stay required for `wrapper_turn` only.
- **Result identity.** Each shown note is recorded as its key, domain, type and fingerprint. Code notes are included with their type. No body, no query text and no path is stored; only `has_query` and the kinds of filter used.
- **Size.** One lookup can show many notes, so it writes events of at most 50 items sharing a random `lookup_id` and carrying `part` and `parts`. An empty result writes one event with zero items.

### 11.2 Confidentiality

Two separate policies:

- **Same-project sync** (the bound repository and its bus). Applies only when the source's `project_id` is the bound one. The complete record is kept, including provenance. A forbidden-strings check still runs over the whole record, to catch another project's names pasted by mistake.
- **Outward sharing** (a public skill-promotion PR, the digest, a cross-project export). Only `process` lessons may leave, as in issue #293. The check covers the complete outgoing record: text, key, trigger, evidence reference, owner, curator, tags and retirement reason. Outward provenance is sanitised (neutral evidence label, role instead of agent name). The private provenance stays in the local store and is never exported.
- **Human clearance.** Outward output has two stages. First a **staging file** is written to a local staging folder; it is marked "NOT CLEARED" in its first line, cannot be used by any command that publishes, and is what the person reads. The checks run when it is staged. A named person reads every line of the staging file and records "cleared" together with the hash of that exact file. Only then does the tool write the **final output** to the output folder, byte for byte from the staging file, and only if the hash still matches; a staging file edited after clearance is refused. The tool never pushes. The pull request cites the clearance. A scope label or a forbidden-strings list does not count as clearance.

### 11.3 Promotion into skills, and how adoption is known

`install-skills` keeps a file that differs from the shipped one unless forced, and it cannot tell an old shipped copy from a personal edit. A machine can also receive a retirement without ever running `install-skills`, or skip a release. So adoption is checked on the machine, at the moment the retirement would be applied, not assumed from a release number.

- **The marker.** Each shipped skill file that carries promoted advice lists the lessons it covers in a short line of its own (`covers:` followed by `key@content_id` entries, using the first 16 characters of the `content_id`). A retirement made because of a promotion names the skill and the full `content_id` of the promoted text in `promoted_to`. The check compares both the key and the `content_id`, so a skill that carries other advice under the same key does not count.
- **Check at import.** When an import would retire a lesson that has `promoted_to`, it looks for the marker in the installed skill file for each agent tool installed on the machine. If every installed copy lists the key and the matching `content_id`, the retirement is applied, and the dependency (the skill names and `content_id`) is written to the import log. If any does not, the retirement waits: the lesson stays active, the import log says "retirement deferred: advice not installed", and `knowledge pull` prints "N lessons awaiting skill adoption: run install-skills". This is the single, logged exception to "retired beats accepted".
- **After the retirement is applied.** The skill can later be removed, replaced by an older release or edited. So the check is not once only: `knowledge pull`, `knowledge onboard` and `install-skills` re-run the cheap marker check for every retirement whose dependency is in the log. For each lesson whose marker has gone, they print a visible warning naming the lesson and the skill, and how to bring the lesson back (`import --reinstate`, a deliberate curator action). Nothing is reactivated automatically. Whether it should be automatic is an open decision (see "Open before work order 7"). Until it is closed, the lesson is still findable with `knowledge search --include-stale`.
- **A skipped release or a never-run installer** lands in the same place: no marker, so the lesson stays active and the message appears. No release number is consulted.
- **A kept local edit.** `install-skills` keeps a differing file by default, as today. If the kept file still lists the key and `content_id`, it counts as installed, but the check only proves the line is there; whether the advice itself survived the edit is **unverified**, and the report says so. If the kept file lacks the key, the lesson stays active. A curator who deliberately keeps a local skill may pass `--accept-local-skill` to apply the retirement anyway.
- **The fallback and how to find retired advice.** Ordinary search hides retired notes. A retired lesson is found with `knowledge search --include-stale` (and `knowledge pull --include-stale`). The skill text is the primary route; this flag is the manual route.
- **Upgrade policy.** A shipped manifest listing the hash of every released skill file lets `install-skills` overwrite a file that matches an older release (an untouched copy) and keep one that matches none. That changes today's default, so it is an operator decision (question 8). Until it is adopted, `install-skills` keeps the default of retaining every differing file and the warning below still applies.
- **Warning.** `install-skills` prints, for every kept file lacking a marker, the path and the keys it lacks, wording it as "not installed here", not as proof about edited content.

### 11.4 Promotion inputs and review

Inputs: `process` lessons ranked by the lesson-use report in 11.5 (how often agents cited them as used), how often they were shown, and how long they have held. The lead proposes a short list at release time and the operator approves it. The pull request edits the skill text, adds the `covers:` line and, in a later change, marks the lesson retired with `promoted_to`. The reviewer checks that the skill gets shorter or clearer. A human clears the text under 11.2 before the pull request is opened.

### 11.5 The lesson-use report (work order 2)

Both the promotion ranking and the adoption measure need to know how often a lesson was cited as used. Today an agent's reply can carry a `lessons_used` entry in its typed metadata, but nothing reads or counts it: the exposure log records only what was shown. Work order 2 therefore adds a read-only command, `knowledge usage-report`, with this contract:

- **Source of use.** The `lessons_used` metadata on the bus's stored replies. The command counts, per lesson key, the replies that cite it, and the replies that cite none.
- **Source of exposure.** The exposure log, including the manual lookups from 11.1, as the count of times each lesson was shown or looked up.
- **Output.** Per lesson: cited as used, shown, looked up by hand, and the date of its last citation. A summary line gives the share of citations that were for tracked lessons (from the import log, once it exists). It prints the date range the stored replies cover, because history that was compacted or pruned is not counted.
- **Limits.** The count is what agents report, not proof that a lesson helped; a missing citation is not proof of non-use. The command never writes and never logs a query. The report is the only producer of the adoption number.

## 12. Evidence gates, work orders and open questions

### Exercises (done before wider use)

Each guard is removed on its own, and the exercise that covers that guard must be the one that turns red.

| # | Exercise | Expected |
|---|---|---|
| 1 | Fresh checkout: clone clean, bind, import | accepted notes load without copying `.agenttalk/` |
| 2 | Two contributors edit the same note on different branches | conflict is listed and not silently settled; "decide later" repeats on the next import |
| 3 | Import from a clone older than a retirement, with the remote reachable | refused: source is behind the remote's tip |
| 4 | Same clone, `--offline` | imports the old note and prints the staleness sentence; a retirement the bus already knew stays retired |
| 5 | Reviewer-only update (who reviewed, when; no change of `state` or content) | no republish; only a missing approval is appended; resulting active text unchanged |
| 6 | Tag or expiry change on an existing accepted lesson | publish then approve; old tags stay active until the approval line; the resulting active payload has the new tags; earlier events are unchanged |
| 7 | Stop an import between publish and approval, then rerun | the rerun appends only the approval; no second publication; a rerun after completion appends nothing |
| 8 | Retired-only import into an empty store | publication plus retraction, never shown; a later non-retired old file is refused |
| 9 | Reinstatement, then replay of the old retirement | the note stays active; the old retirement is ignored and reported |
| 10 | Unapproved file: uncommitted edit, or a commit not on the main branch | refused, or imported as proposed with `--as-proposed` |
| 11 | Wrong project: a source with another `project_id`, an unlisted domain, or a non-curator | refused with the reason |
| 12 | Stale local reference: a clone whose own main branch lacks the remote's newer commit | "fetch first"; nothing imported |
| 13 | Old readers: an older exposure reader meets version-2 lines; an older loader meets an unknown file `format` | lines reported as malformed, not read; file refused by name |
| 14 | Manual lookups: an empty result, a result of more than 50 notes, code notes, a typed query | zero-item event, chunked events with one `lookup_id`, code notes recorded by type, no query text or body in the log |
| 15 | Selector: setting on and off, five slots full of other eligible lessons, tracked injection on | untagged lessons skipped and counted; tracked lessons take only free slots; no displacement |
| 16 | Confidentiality and clearance: a record whose key, trigger or retirement reason contains a forbidden string; then a clean record | the forbidden one is refused at staging; the clean one is staged as "NOT CLEARED", readable; the final output appears only after clearance of that file's hash; editing the staging file after clearance is refused |
| 17 | Old skill, new retirement: a kept edited skill with no marker, then one with the marker | lesson stays active with the "run install-skills" message; with the marker it retires and the report says "unverified"; `--include-stale` finds the retired lesson |
| 18 | Skipped release and no installer: a machine goes from an old release straight past the promotion release | lesson stays active until the installed skill carries the marker |
| 19 | Override: switch off one lesson, then withdraw it | hidden here only; target named as (domain, key); no shipped skill text changes; the withdrawal is a new log line with its own identifier, the old line is not deleted, and replaying the old override does not bring it back |
| 20 | Retire, reinstate, retire again, with identical text | the first retirement (R1) is replayed and ignored after the reinstatement; the second (R2, new identifier) applies and the note ends retired |
| 21 | Lifecycle change (accepted to retired, retired to reinstated) | the retraction or the new publication and approval is appended; it is never treated as a reviewer-only update |
| 22 | Crash at every durable append: after the intent, after each event, after the last event but before the completion line | a rerun appends only the missing events and then the completion; an intent is never read as success; events alone are never read as complete; a concurrent curator change is seen and stops the rerun for that key |
| 23 | Fresh store, a source retired because of a promotion, no installed skill | the import reports "not loaded here: the advice is in a skill that is not installed; run install-skills, then import again" and appends nothing; it never says "retained" for advice that was never loaded |
| 24 | Windows-reserved names: keys `CON`, `aux.v2`, a key ending in `.`, a domain `nul` | each is written to a path that can be created on Windows, decodes back to the same identity, and a checkout does not fail |
| 25 | Lessons that differ only by a nested anchor | different `content_id`s; export and import keep the anchor; the whole-record check covers it |
| 26 | A path-anchored code note with and without `verified_against_sha` | with it, the imported note is not stale; without it, it is imported as proposed and listed |
| 27 | Supersession across domains: a lesson in domain A supersedes key `x`, domain B has its own `x` | B's `x` stays active; only A's `x` is superseded |
| 28 | Two retire-and-reinstate cycles, then a replay of the first retirement's file on a fresh bus | the current file lists both identifiers; the replay is ignored |
| 29 | A fresh bus meets an override withdrawal first, then the older override; then a new override of the same target | the old override stays withdrawn; the new one applies |
| 30 | A promotion retirement where the local lesson has a different `content_id` from the one the skill carries; and a retirement later corrected to add `promoted_to` | the first is a conflict, not a retraction; the second re-runs the check and is applied or deferred under the new metadata |
| 31 | The installed skill is removed or downgraded after the retirement was applied | `pull`, `onboard` and `install-skills` print a warning naming the lesson and the skill; nothing is reactivated automatically; `--include-stale` still finds the lesson |
| 32 | The lesson-use report over replies that cite lessons, cite none, and a pruned range | per-lesson counts are right, the covered date range is printed, nothing is written, no query text appears |

### Kill signals

We stop or reshape if, over 2 to 4 weeks, any of these happens: a project's policy is overwritten; retired advice returns; generic advice crowds out needed local guidance; raw project material leaves its repository; merge conflicts or duplicate keys keep recurring.

### Two different measures

- **Adoption:** tracked or imported notes make up at least about 1 in 10 of the lessons agents report using. This shows the notes are used, not that they help.
- **Prevented-mistake indicators:** the pilot's displaced-needed-advice count stays at zero, and a review finding in a class a tracked note covers does not recur. These are **indicators, not proof**. For each, we record the **opportunities** (how many tasks could have hit that mistake) and whether the note was shown or looked up on them, beside the failures. Zero recurrences with no opportunities shows nothing. The operator picks which measure gates the work (question 4).

### Work orders, in order

Sizes are engineer-days for one builder plus one review round, assuming the existing store and event code are reused and no new dependency is added. They are rough, plus or minus half. Work orders 4 and 7 were re-estimated after the decisions in section 9 and 11.3. This is a rough order of magnitude, not a delivery forecast; it should be re-estimated after the operator's answers, and the operator chooses the stage and spend to authorise.

| # | Work | Days | Authorisation |
|---|------|------|---------------|
| 1 | Lessons report, the selector rule and setting, domain-qualified supersession, and a re-publish-then-approve command for tag changes (section 4) | 5 | authorised (operator, 2026-10-07) |
| 2 | Exposure schema version 2: reader first (accepts versions 1 and 2), then manual-lookup writers one release later, with `--from` and the separate onboarding recipient (11.1), and the read-only lesson-use report (11.5) | 7 | authorised (operator, 2026-10-07) |
| 3 | Note file format with path encoding, `content_id`, export of accepted and retired notes, staged outward output and whole-record confidentiality check (9.1, 9.2, 11.2) | 6 | to be re-estimated and authorised later |
| 4 | Import, in three parts: (a) read files, derive state, dry-run report, 5 days; (b) append sequences, intent-and-completion log, retirement, reinstatement lineage, 8 days; (c) bind command, remote freshness check, wrong-project checks, 4 days (9.3 to 9.6); the domain-creation decision may add work | 17 | to be re-estimated and authorised later |
| 5 | Override records (9.7) | 2 | to be re-estimated and authorised later |
| 6 | Resolve anchors against the bound checkout root (issue #245) | 4 | to be re-estimated and authorised later |
| 7 | Skill `covers:` markers bound to `content_id`, adoption check at import and re-check on `pull`, `onboard` and `install-skills`, deferred retirement, install warning, shipped manifest; Codex folder check per version (10, 11.3) | 7 | to be re-estimated and authorised later |
| 8 | The thirty-two exercises, with guard-removal runs, written up | 9 | to be re-estimated and authorised later |
| 9 | Promotion run, by hand, per release (11.4) | 1 per release | to be re-estimated and authorised later |
| 10 | Digest template and clearance checklist (11.2) | 1 | to be re-estimated and authorised later |

Items 1 to 8 total 57 days, which is the cost **before** the first promotion. Including the first promotion run (item 9) the total is 58 days, and with the digest (item 10) 59. The figure rose from 47 after the round 4 review, because lifecycle lineage, crash-safe import, staged clearance, domain-qualified supersession and the lesson-use report are now specified rather than assumed. Elapsed time is longer: several release boundaries and the 2 to 4 week observation period sit on top. Items 1 and 2 stand alone and can ship first. Items 3 and 4 are the core of B.

**Build acceptance gates** (they do not change the design; each must pass before its work order is accepted):

- **Work order 4(b), crash recovery.** The event file and the import log are two files, and no lock makes two files atomic. Before 4(b) is accepted, a recoverable intent-then-completion sequence is chosen and exercised under the store's shared lock, with an interruption after each durable append (exercise 22), concurrent curator changes, and reruns that reconstruct a missing completion without treating an intent as success. The existing event format does not change.
- **Work order 7, fresh store.** A fresh store, a source retired because of a promotion and no installed skill must end in the honest "not loaded here, manual recovery" result (exercise 23). The alternative, an authorised fallback publication and approval of the lesson, is not adopted: it would load advice the machine's skill does not carry.
- **Work order 3, canonical form.** The exact JSON canonicalisation and validation of format 1 are fixed before format 1 ships.

### Open before work order N

These decisions are real but not settled by this document. **A work order cannot start until its open items are closed.** Work orders 1 and 2 have none.

**Open before work order 4: how a project's domains are created on a clean machine.** Today's `domain` command is read-only, and the binding stores only allowed domain ids, not the definitions or the curator lists. Options: (a) the repository tracks each domain's definition in `.agenttalk-knowledge/domains/<domain_id>.json`, and a curator imports it under the same trust rules as notes, mapping curator names to local agents at import; (b) add a `domain add` command and have the curator type the definition once per machine; (c) leave it to hand-editing `.agenttalk/domains.json`. *Recommend (a): the fresh-checkout promise (exercise 1) holds only if the definitions travel with the notes, and it needs no new command. It adds roughly 2 days to work order 4.*

**Open before work order 7: what happens when a marker disappears after a promotion retirement.** Options: (a) warn only, and a curator brings the lesson back deliberately; (b) bring the lesson back automatically at the next `pull` or `onboard`; (c) check again before every selection. *Recommend (a): an automatic change of what agents are shown, made from a file check, is the kind of silent behaviour this design avoids, and (c) adds a file read to every turn. The warning in 11.3 makes the loss visible, and the retired lesson stays findable.*

### Questions for the operator

Each has a recommendation. **Answered on 2026-10-07: the operator accepted all nine recommendations as written** ("Accept all nine"), including the qualifications attached to questions 4, 7 and 8.

1. Folder name and place: is `.agenttalk-knowledge/` at the project top the right home? *Recommend yes.*
2. Should a project ever turn on injection of its tracked notes, and is "only into free slots" acceptable? *Recommend yes, default off.*
3. Who curates for a project with several contributors: one named person, or any maintainer through a pull request? *Recommend one named curator per project, with maintainers reviewing the pull request.*
4. Which measure gates the work: adoption, prevented-mistake indicators, or both? *Recommend adoption as the stop signal, with the prevented-mistake indicators as supporting evidence only, not a second numeric gate. The safety kill signals above stay unconditional whichever you choose. You are also choosing the stage and spend you authorise, not just a measure.*
5. Is a store setting for one more release of the old selector behaviour acceptable, or should it switch at once? *Recommend the setting.*
6. Is the binding stored in the bus configuration, with the repository holding only a project id, acceptable? *Recommend yes; a repository cannot vouch for itself.*
7. Is the single exception to "retired beats accepted" (a promotion retirement waits for the installed skill) acceptable? *Recommend yes, limited to retirements marked `promoted_to`. One limit to accept knowingly: the marker can survive in an edited skill that has lost the advice, so the check proves the line is present, not that the advice is. The alternative is to require a curator's deliberate acceptance (`--accept-local-skill`) for any edited copy.*
8. Should `install-skills` overwrite untouched copies of older shipped skills (identified by the manifest) while keeping personal edits? *Recommend yes; without it, promotion depends on people running `--force` or updating the files by hand.*
9. Is a refusal when the remote cannot be reached the right default for import, with `--offline` as the explicit way out? *Recommend yes.*

**Authorised scope.** The operator chose "Work orders 1-2 now": the lessons report and selector rule (work order 1) and exposure schema version 2 with manual-lookup logging (work order 2), about 9 days. Work orders 3 to 10 are to be re-estimated and authorised later; nothing in them is approved to start.

**Re-estimate of the authorised scope.** The operator authorised work orders 1 and 2 at about 9 days. After the round 4 review they stand at 12 days (5 and 7): domain-qualified supersession was added to work order 1, and the lesson-use report to work order 2, because both are needed for the selector and the adoption measure to be correct. This is a rise of about 3 days over what was authorised, and the operator should confirm it before work order 1 or 2 starts.

## 13. Technical notes (for builders)

- Today's code: notes are events in `.agenttalk/knowledge/notes.jsonl` (`knowledge.py`), folded to a current view per (domain id, key). Lessons carry a scope, tags, status, review and expiry dates (both required), and `supersedes`. The curation-bound content is everything but the status and the curator, plus author, creation time and the replaced-event id; the causal check (`_curation_causal_problem`) requires a curation to reference the current prior same-key event and carry its payload hash. That is why a tag change is a new publication and why a retraction needs a prior event.
- A retract is terminal until a fresh publish and curation reopen the key (`resolve_views_with_problems`), which is why import must check retractions itself.
- Curation authority is enforced in `cmd_knowledge` in `cli.py`. A missing domain registry reads as empty (`domains.py`), and anchor checks resolve against the store root. The existing `--include-stale` flag on `knowledge search` and `pull` is what shows retracted notes.
- The selector is `select_lessons` and `rank_lessons` in `lesson_context.py`. The clause to change is the empty-tag allowance. `exposure_event_problem` accepts only schema version 1 and `surface == "wrapper_turn"` with turn identity, a prompt-block hash and one to five lessons; the reader returns a problem list rather than raising.
- `install_skills.py` compares file bytes (`filecmp`) and skips a differing file without `--force`. It already has a warning-only check for retired skills, which is the place for the missing-marker warning.
- `lesson_superseded_keys` builds one set of bare keys across all domains; work order 1 changes it to (domain, key) pairs. `compute_staleness` marks a path- or symbol-anchored note with no `verified_against_sha` as hard-stale (`missing_verified_baseline`), which is why the file carries the baseline. The reply metadata `lessons_used` exists, but no code reads or counts it; work order 2 adds the report.
- The importer reuses `knowledge.event_problem` validation and the event builders, and adds `content_id` beside the existing payload hash rather than replacing it.
