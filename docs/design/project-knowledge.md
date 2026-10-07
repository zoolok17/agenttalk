# Project knowledge: keeping what a team learned inside the project

Status: proposed. Nothing in this document is built yet. Where it describes how agenttalk works today, that part matches the code at release 0.98.0.

## In plain words

Agents on the bus learn things while they work: a trap in a test setup, a review mistake that keeps coming back, a way of working that saved time. Today those notes live in one hidden folder on one machine. A fresh checkout, a second machine or a new contributor starts with none of them.

This document proposes to keep the notes a project has agreed on inside the project's own repository, as small reviewed files. It also proposes to fold the best general lessons into the skills agenttalk ships, and to allow a hand-made, human-checked summary for people who want to share what they learned.

It decides the shape of the notes, how they move between the bus and the repository, and how we will know it works. It leaves open a few choices for the operator, listed at the end. Technical detail comes last, in the final sections.

Audience: the operator and the team who will review and build this. Mode: explanation (a design).

## 1. The problem, with the numbers we have

- The store sits in `.agenttalk/`, which git ignores. One machine holds the only copy of the curated notes.
- A pilot compared the lessons injected into agents with what they would have seen otherwise. Across 19 decisions it found no useful correction and six cases where a lesson crowded out advice the agent needed.
- 16 of the 18 lessons agents reported using came from a search they ran at the start of the work, not from the five lessons injected for them.
- 264 of 354 lessons carry no tags. An untagged lesson counts as relevant to every task, so a handful of them can fill the five slots whatever the job is.
- Manual searches leave no record in the exposure log, so the team cannot see which notes people actually read.

The design answers each of these: move notes into the repository (section 2), stop untagged lessons from filling the slots (section 5), and make manual searches visible (section 5).

## 2. Three parts, in the order we build them

**B, first: project notes tracked in the project.** Notes the project has curated are written as files in the project's repository and reviewed by ordinary pull requests. They stay apart from bus history and from usage statistics.

**A, as skills: the universal layer.** General lessons that keep proving useful are promoted into the skills agenttalk already ships. There is no separate "universal lesson pack" competing for the five slots.

**C, manual only: a digest people prepare by hand.** A summary written on one machine, read by a person before it leaves, never produced by scanning a client's repository automatically. No raw project material leaves the repository it came from.

We build B first because A and C both depend on notes being easy to find, review and retire.

## 3. B: how the tracked notes look

Notes are advice, not rules. They stay separate from skills and from the vendors' rules files (section 7), and an agent may read them as context but is never told they override anything.

### Layout

Each note is one file in a folder named `.agenttalk-knowledge/` at the top of the project, tracked by git. One file per note keeps merges clean: two people adding different notes never touch the same file.

Each file carries:

- a **key**: a stable name, the same one the bus uses;
- the **text** and the **trigger** (when it applies), as the bus holds them today;
- **tags and scope**, so the selector can match it (section 5);
- **provenance**: who wrote it, which request or PR is the evidence, and the date;
- **review state**: proposed, accepted or retired, who changed it and when, and a "review after" date;
- **retirement**: a retired note stays in the folder, marked retired, with a reason. Deleting a file does not retire a note, because an old copy on another machine would bring it back.
- **supersedes**: the keys it replaces.
- **project override**: an optional line saying "in this project, ignore the shipped advice named X, because Y". An override is written down and reviewed like any note, so a project's policy is never overwritten silently.

### Rules for conflicts

- Two files with the same key are an error that the loader reports by name. It never picks one.
- A retired key stays retired. If a bus note with a retired key arrives, it is shown as a conflict for a human to settle.
- The text of a note is capped as it is today (500 bytes for a lesson).

## 4. B: how notes travel

1. **Bus to repository.** A curator runs an export of accepted notes. Issue #293 part 2 already proposes the command and the checks: `knowledge export --curated` with a scope filter, and `knowledge import`, both keeping provenance and refusing text that contains configured forbidden strings (a confidentiality check). This design reuses that work. It adds a folder target and does not add a second store or a second selector.
2. **Review.** The export is a normal pull request. The diff is the review: a person reads each added, changed or retired note.
3. **Repository to agent.** When agenttalk starts in a checkout, it loads the tracked notes into the same note view the bus store already feeds. A fresh checkout or a second machine gets the notes by cloning; nobody copies `.agenttalk/`.
4. **Repository to bus.** Import brings tracked notes into a bus store as accepted notes, with the repository as the source. A retired note in the repository retires the bus copy.
5. **Duplicates and drift.** Matching is by key. Same key and same text is a no-op. Same key and different text is a conflict shown to the curator. Nothing is overwritten without a person choosing.

This also covers the gap in issue #245, where code notes go stale when the bus folder is not the code repository: tracked notes live where the code lives.

## 5. The selector: no new competition, and a fix for untagged lessons

Agents see at most five lessons per turn today (`DEFAULT_LESSON_LIMIT`). Tracked notes must not take slots from what is chosen now. Three changes, all proposed:

1. **Tracked notes are not injected by default.** They are found by search and onboarding, like other notes, and enter the five slots only through the same ranking as everything else, with no extra weight.
2. **A lesson needs a tag or a scope to be injected.** A lesson with no tags no longer matches every turn. It can still be searched and still counts for onboarding. Existing untagged lessons are listed for a curator to tag or retire; this is the cleanup for the 264 untagged lessons.
3. **Manual searches are logged.** `knowledge search` and `onboard` record what they showed in the existing exposure log, using a new `surface` value, as issue #293 recommends. The log keeps one schema.

## 6. A: promoting lessons into skills, once per release

- **Inputs.** Curated lessons in the `process` scope, ranked by how often agents report them as used (`lessons_used`), how often they were shown, and how long they have held up.
- **Who decides.** The lead proposes a short list at release time; the operator approves it.
- **How it is reviewed.** A promotion is a pull request that edits the skill text and retires the lesson with a pointer to the skill. The reviewer checks the skill gets shorter or clearer, not longer.
- **Local edits.** `install-skills` keeps a person's edited copy unless forced, so a promoted line reaches a machine only when its owner accepts the new file.

## 7. What agenttalk manages and what the vendors cover

Checked on 2026-10-07 against the vendors' public pages:

- **Claude Code skills.** Project skills live in `.claude/skills/<name>/SKILL.md` and are meant to be committed. A personal skill of the same name wins over a project skill. Verified in the skills page.
- **Claude Code rules.** `.claude/rules/*.md` holds project instructions, optionally limited to matching file paths; the files are loaded as context, not enforced. `CLAUDE.md` and an `AGENTS.md` are also read. Verified in the memory page.
- **Codex.** It reads `AGENTS.md` from the Git root down to the working folder, with a combined limit of 32 KiB by default. Verified. I did not find a project skills folder on the Codex page I read, so I make no claim about one.

The split:

- **Vendors' mechanisms** own rules and project-specific skills. A project that wants an enforced rule writes it there, not as a note.
- **agenttalk** owns the notes (advice with history and review) and the shipped skills. It does not write into `.claude/rules/` or `AGENTS.md`.

## 8. C: the manual digest

- **Contents.** A short document of accepted lessons and open questions, written on one machine from the curated notes.
- **Readers.** The operator and people they choose; never published automatically.
- **Confidentiality rule.** A person reads every line before it leaves. The export's forbidden-string check runs first. Nothing is scanned out of a client repository by a program, and no raw project text, file contents or names travel with it.

## 9. Evidence gates

**Fresh-checkout and second-contributor exercise** (done before any wider use):

1. Clone the repository to a clean folder; confirm the accepted notes load without copying `.agenttalk/`.
2. A second contributor adds a note, and two contributors edit the same note on different branches; confirm the conflict is visible, not silent.
3. Retire a note, then load from a stale clone; confirm it stays retired.
4. Confirm a project override is shown and the shipped advice it names is not.

**Kill signals.** We stop or reshape if, over 2 to 4 weeks, any of these happens:

- a project's own policy is overwritten;
- retired advice comes back;
- generic advice crowds out needed local guidance;
- raw project material leaves its repository;
- tracked or imported notes make up fewer than about 1 in 10 of the lessons agents report using;
- merge conflicts or duplicate keys keep recurring.

## 10. Work orders, in order

| # | Work | Size |
|---|------|------|
| 1 | Selector: require a tag or scope to inject; list untagged lessons for cleanup | small |
| 2 | Log manual search and onboard in the exposure log (`surface` value) | small |
| 3 | Export and import with provenance and the confidentiality check (issue #293 part 2), folder target | medium |
| 4 | Loader for `.agenttalk-knowledge/`: duplicate-key and retired-key checks, overrides | medium |
| 5 | Fresh-checkout and second-contributor exercise, written up | small |
| 6 | Per-release promotion into skills (first run by hand) | small |
| 7 | Manual digest template and checklist | small |

Items 1 and 2 stand alone and can ship first. Items 3 and 4 are the core of B.

## 11. Questions for the operator

1. Folder name and place: is `.agenttalk-knowledge/` at the project top the right home, or should it sit under an existing folder?
2. Should tracked notes ever be injected by default for one project, if its owner asks for it?
3. Who curates for a project with several contributors: one named person, or any maintainer through a pull request?
4. Is the 1-in-10 reuse line the right bar, and is 2 to 4 weeks long enough?
5. Should Codex-only projects get the promoted skill text some other way, since a project skills folder for Codex is not confirmed?

## Technical notes (for builders)

- Today's code: notes are events in `.agenttalk/knowledge/notes.jsonl` (`knowledge.py`), folded to a current view per domain and key. Lessons carry scope (`process`, `craft`, `review`, `test`, `release`, `ops`, `docs`, `security`), `applies_to` tags, status (proposed, accepted, retired), and `supersedes`.
- The selector is `select_lessons` in `lesson_context.py`. A lesson matches when its scope is `process` or the turn's scope and its tag list is empty or overlaps the turn's tags. Ranking puts `process` first, then the turn's scope, then review-due, then newest. The empty-tag clause is the one to change (work order 1).
- `knowledge search` and `onboard` go through `cmd_knowledge` in `cli.py` and call no exposure writer; `record_exposure` is only reached from the wrapper and sync paths.
- Shipped skills install with `install_skills.py`; the devkit skills copy to both `~/.claude/skills/` and `~/.codex/skills/`.
- The loader should reuse `knowledge.event_problem` validation by turning each file into a publish and curate event, rather than adding a second reader.
