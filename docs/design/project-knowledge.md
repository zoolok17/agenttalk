# Project knowledge: measure first, share later

Status: reshaped design, accepted by the operator on 2026-10-08. Only the measurement work (work orders 1 and 2, authorised at about 9 days; this design now sizes it at about 10 days, section 6) is authorised. The read-only overlay in section 8 is not authorised yet and follows the measurement. The full import and promotion design is parked in Appendix A. Nothing in this document is built yet. Where it describes how agenttalk works today, that part matches the code at master `7e36cfb7` (release 0.98.0 plus later fixes). Every example of proposed behaviour is marked "proposed, not runnable yet".

## In plain words

Agents on the bus learn things while they work: a trap in a test setup, a review mistake that keeps coming back, a way of working that saved time. Today those notes live in one hidden folder on one machine, and nobody can say how often an agent actually uses one.

The earlier plan was to keep a project's agreed notes in its repository and import them onto each machine with a full lifecycle (retirement, abort, promotion into skills). It grew from about 47 to about 65 days. The operator accepted a smaller plan instead, in three steps:

1. **Measure now (about 10 days; 9 were authorised).** Count how often lessons are shown to agents, found by hand, and cited as used, so the question "are lessons used at all?" has an answer. Report which untagged lessons would stop filling the five injected slots under a tag rule (switching the rule on is held until the numbers are in), and fix a bug where one lesson can wrongly mark another project area's lesson as replaced.
2. **Read lesson files straight from a project's repository, read-only (later, not authorised).** A lookup would show a project's own lesson files, marked "from this project, not reviewed here", without ever copying them into the bus store. About 5 days, after the measurement.
3. **Hold everything else.** Import, retirement, overrides, promotion into skills and the manual digest are parked in Appendix A. Revisit them only if the numbers show that lessons are used and that people need to share them.

This document decides what the measurement counts and how, what the overlay would and would not do, and when to stop. It does not build anything.

Audience: the operator and the team who will review and build this. Mode: explanation (a design).

## The challenge record

| Item | Record |
|---|---|
| Challenge | `ch-project-knowledge-v3-b-20261008` |
| Verdict | RESHAPE (medium confidence, reasoned, from an unexposed challenger) |
| Disposition | **Accepted** by the operator on 2026-10-08: "Accept the reshape (Recommended)" |
| Operator's words | "Do only the measurement work (lessons report + lesson-use counting) within the ~9 days you approved. Instead of the 20-day import machinery, let agenttalk read lesson files straight from a project's repo (read-only). Hold the rest until the numbers show lessons are actually used and shared." |
| Why | The estimate had grown from about 47 to 65 to 67 days (work orders 1 and 2 from about 9 to 15), most of it import machinery built before any evidence that anyone wants it. |
| Kill signal | After the report runs, if fewer than a handful of lessons per project are cited as used within a month, stop investing in sharing and work on lookup and curation instead. If the first teams to try the overlay never add a project lesson file, stop. |
| What would change the verdict | The measurement shows lessons are used and two teams ask for the same lessons on two machines; or a prototype of the overlay is too slow or too hard to rank; or a threat review finds the overlay less safe than an import with quarantine. |

A second reply to the same challenge, from a reviewer who had seen earlier discussion, did not count for the verdict; it advised the same reshape.

## 1. The problem, with the numbers we have

These come from the pilot reported on issue #293. They are a small pilot and a second-team count, not a full census.

- The store sits in `.agenttalk/`, which git ignores. One machine holds the only copy of the curated notes.
- Across 19 decisions the pilot found no useful correction from injected lessons and six cases where a lesson crowded out advice the agent needed.
- 16 of the 18 lessons agents reported using came from a search they ran at the start of the work, not from the five lessons injected for them.
- 264 of 354 lessons carry no tags. Today an untagged lesson still has to pass the scope check, but every untagged `process` lesson passes it for every task, and `process` lessons rank first. A handful of them can therefore fill the five slots whatever the job is.
- Manual searches leave no record in the exposure log, so today's counts undercount what agents read. Nothing reads or counts the `lessons_used` metadata that replies already carry.

The first two work orders answer the last point and the one before it. The sharing question is held until they have produced numbers.

## 2. The plan, in order

| Step | What | Size | Status |
|---|---|---|---|
| Work order 1 | Lessons report; domain-qualified supersession; naming and testing today's task-tag inference | 3 days | authorised at about 9 days for both work orders (operator, 2026-10-07, reconfirmed by the reshape on 2026-10-08) |
| Work order 2 | Exposure logging for manual lookups; a typed, copyable event id in lesson lines; the lesson-use report with exact typed citations | 7 days | as above |
| Later, after the numbers | The tag rule behind a setting, off by default (section 4) | 1.5 days | **not authorised** |
| Overlay | Read a project's lesson files from its checkout, read-only (section 8) | about 5 days | **not authorised**; follows the measurement |
| Everything else | Import, retirement, overrides, anchors against the checkout, promotion into skills, the manual digest | 50 or more days | **parked** (Appendix A) |

## 3. What a person will notice

- A new report, `knowledge lessons-report`, shows which accepted lessons carry no tags and what a tag rule would do to each. It changes nothing.
- A new report, `knowledge usage-report`, shows per lesson how often it was shown, looked up by hand and cited as used, and says honestly how much of that it could not attribute.
- Searches people run by hand, with `knowledge search`, `pull` and `onboard`, start to appear in the exposure log. What was typed is never stored.
- Lesson lines shown to agents gain a typed, copyable event id (for example `@id:kn-3f9a01c2b7d4`), so a reply can name exactly which version of a lesson it used. Only a reply that cites that exact form is counted as use.
- Agents see the same five lessons as today. Switching on a tag rule is a later, separate decision made with the numbers.
- Nothing is imported, copied or retired, and no file format is added to any repository.

## 4. Which lessons an agent is shown (the selector)

Today an agent is shown at most five lessons per turn. A lesson is eligible if its scope is `process` or the task's scope, and if it has no tags or one of its tags matches the task. `process` lessons rank first.

### The rule (proposed; work order 1 only evaluates it in the report; switching it on is a later work order)

A lesson is shown to an agent in a turn only if all of these hold:

1. It is active: accepted, not retired, not expired, not stale.
2. Its scope is `process` or the scope of the task.
3. It has at least one tag, and at least one of its tags matches a tag of the task.

A lesson that fails rule 3 is **lookup-only**: `knowledge search`, `knowledge pull` and `knowledge onboard` still show it. Nothing is deleted or rewritten. When the rule is later switched on, it will be a setting `lesson_injection` with the values `all` (today's behaviour, the default) and `tagged_only`; building that setting is a later work order (about 1.5 days), not part of the measurement.

**What "the task's tags" are today.** Tags are not authored on a task. `record_lesson_context` in `lesson_context.py` derives them from the record's kind, its subject, its correlation identifiers (`request_id`, `broadcast_id`, `correlation_id`) and a fixed list of metadata keys (`assignment`, `artifact_type`, `domain`, `lane_id`, `risk`, `risk_class`, `review_ref`, `review_type`, `reviewed_ref`, `scope`, `status`, `work_id`, `wp_id`). It does not read the message body and has no task-tags field, so a task whose body mentions CI but whose subject is generic gets no `ci` tag. This design keeps that inference for the measurement, documents it, and tests it on a real wrapped record. Every report that applies the rule says it shows **the effect of this selector, not how relevant a lesson is**.

### Examples (proposed, not runnable yet)

| Lesson | Task (tags as derived above) | Shown today | Shown with `tagged_only` |
|---|---|---|---|
| `process`, no tags | any task | yes, ranked first | no, lookup-only |
| `test`, no tags | a `test` task | yes | no, lookup-only |
| `test`, no tags | a `docs` task | no (scope differs) | no |
| `process`, tags `release` | task tags `release`, `docs` | yes | yes |
| `process`, tags `release` | task tags `ci` | no | no |
| `test`, tags `ci` | a `docs` task with tags `ci` | no (scope differs) | no (scope differs) |

### Superseded lessons are qualified by domain (work order 1; a correctness fix, kept)

Today `lesson_superseded_keys` in `knowledge.py` collects the `supersedes` entries of every accepted lesson into one set of bare keys. If domains A and B both hold a lesson with key `x`, an accepted lesson in A that supersedes `x` also makes B's unrelated `x` stale. The fix: the function returns (domain, key) pairs, and a bare key in a lesson's `supersedes` list means a key in the same domain as the lesson that holds it. Supersession across domains is not allowed. The stored events keep bare keys, so nothing existing is rewritten, and the selector, `pull` and the reports read the pairs.

### What changes for existing teams

1. **The report comes first.** `knowledge lessons-report` lists every accepted lesson that the rule would make lookup-only, and, from the exposure log, how many past lesson shows were of such lessons. Nothing changes in behaviour.
2. **Tagging a lesson stays a manual two-step.** Tags are part of a lesson's fixed content, and the store refuses a curation whose content differs from the note it approves. So a curator publishes the lesson again with the new tags (copying everything else, including both dates) and then curates that exact publication. Until the second step the previous accepted version stays active and nothing in agents' turns changes. A dedicated command for this is deferred (section 6).
3. **Switching the rule on is a later decision**, made with the numbers from the two reports. The setting that would do it is not built in this work.

## 5. Measuring use

### 5.1 Manual lookup logging (work order 2)

Today the exposure log only accepts records made by a wrapped turn (`wrapper/run.py` is the only caller of `record_exposure`), and the reader requires schema version 1 and the surface `wrapper_turn`. A manual `knowledge search`, `pull` or `onboard` is invisible to it. `sync` selects lessons but records nothing.

- **Schema version 2** is used for the new records. The new reader accepts versions 1 and 2. Existing `wrapper_turn` events stay version 1 and valid, unchanged.
- New `surface` values: `manual_search`, `manual_pull`, `manual_onboard`, in the same file.
- **Two releases.** Release N ships a reader that accepts version 2 and skips what it does not know. Release N+1 turns the writers on. A reader older than N meets version-2 lines as malformed lines, which it reports in its problem list rather than failing; it does not read them, and that is the accepted limit.
- **Actor and recipient.** Manual `search`, `pull` and `onboard` gain `--from <agent>` for the actor, defaulting to `AGENTTALK_SELF`. If none resolves, nothing is logged and the command prints a one-line notice. `onboard --for <agent>` stays a label for whom the digest is for; it is logged as a separate `recipient` field and never taken as the actor.
- **Result identity.** Each shown note is recorded as its key, domain, type and fingerprint. No body, no query text and no path is stored; only `has_query` and the kinds of filter used.
- **Size.** One lookup can show many notes, so it writes events of at most 50 items sharing a random `lookup_id` and carrying `part` and `parts`. An empty result writes one event with zero items.
- **Complete lookups only.** A reader counts a lookup only when every declared part is present, valid, unique and consistent (the same `lookup_id`, actor and `parts`). A group with a missing, duplicated or conflicting part is shown separately as incomplete and is not counted. A reader that runs between two appends, or a writer interrupted before its last part, therefore never produces a half-counted lookup. The existing `lookup_id`, `part` and `parts` are enough; no separate transaction mechanism is needed.
- **What the totals cover.** Wrapper turns and manual lookups. Sync displays are not measured, and every report says so. A lesson with no recorded exposure is not evidence that no agent saw it.

### 5.2 The lesson-use report (work order 2)

Today an agent's reply can carry a `lessons_used` entry in its typed metadata, but nothing in the code reads or counts it. Work order 2 adds a read-only command, `knowledge usage-report`:

- **Source of use and the reply states.** The `lessons_used` metadata on the bus's stored replies, which is a comma-separated list. Each reply falls into exactly **one** state, decided by this precedence table (the first row that applies wins). Token problems are counted separately and never change the state of an otherwise valid reply.

  | Order | State | Rule | Credit | Usable declaration? |
  |---|---|---|---|---|
  | 1 | **conflicting** | `@none` together with any other token (a typed id, a bare word, a malformed token) | none | no |
  | 2 | **cited** | at least one `@id:` token that resolves uniquely to a valid lesson event | yes, for each resolved lesson | **yes** |
  | 3 | **none** | the sole valid `@none` token and nothing else | none | **yes** |
  | 4 | **legacy-only** or **unresolved-only** | no resolved typed id and no `@none`, but at least one bare word (legacy token), or at least one well-formed `@id:` that did not resolve uniquely to a lesson event | none | no |
  | 5 | **malformed** | only malformed tokens | none | no |
  | 6 | **empty** | the metadata is present but blank | none | no |
  | 7 | **absent** | no such metadata | none | no |

  A usable declaration is therefore a *cited* reply (at least one uniquely resolved valid lesson event) or the sole valid `@none`. Replies in rows 1 and 4 to 7 count in the coverage denominator and in no numerator. The **token counts** are kept apart from the state: well-formed `@id:` tokens that resolved, that did not resolve, malformed tokens, and legacy tokens by class. So `@id:<resolved>,@id:` is a *cited* reply (row 2) with one malformed token, and not a malformed reply; `@none,@id:<resolved>` is *conflicting* (row 1) and gets no credit. Absent is not "used none".
- **Typed citation forms (new replies).** Two typed forms exist, and both start with `@`, a character no lesson key can start with (keys start with a letter or a digit): `@id:<event id>` cites one exact lesson event, and `@none` states that no lesson was used. The event id may be any id the store accepts: one to 64 characters from letters, digits, `_`, `.` and `-`, starting with a letter or digit (the store checks this in `validate_note_id`). Generated ids look like `kn-3f9a01c2b7d4`, but a stored id may be shorter or differently shaped (the tests use `kn-lesson`), so the form carries the whole id after `id:`. The separate `@none` token means that even an event whose id is literally `none` is written `@id:none`. This is needed because bare words collide with keys: `none` and `kn-deadbeef1234` are both valid lesson keys, so a bare token cannot be told from a key.
- **Credit.** A lesson is credited with a use **only** for an exact `@id:` citation that resolves to a single, valid lesson event in the store. The event gives (domain, key, version), where the version is the lesson's content, so the id of the publication and the id of its approval name the same version. Not credited: an id that is not found, found twice, or belongs to an event that is not a lesson; a bare key; a legacy id; the version that was live when the reply was written; or any exposure of the same actor. (An agent can read version A for one task, see version B in an unrelated lookup, and later reply to the first task citing the key; any rule that credits the "latest" or "unique" exposure would credit B. Exact ids cannot be wrong that way.) **Resolve first, then count:** every cited id is resolved to (domain, key, content version) before anything is counted, and each distinct (domain, key, content version) counts once per reply, so the publication id and the approval id of one version, or the same id cited twice, give one credit. The prompt-record check below still looks at the raw cited ids.
- **Legacy and other bare tokens (anything without the `@`): reported, never credited.** Each is classified for the report only, without redefining any existing key: a **legacy id** (equals an existing event id, and no key has that spelling), a **legacy key** (equals a key in exactly one domain), **ambiguous** (equals a key in several domains, or is both an id and a key, or is `none` while a lesson with the key `none` exists), or **unknown** (matches nothing). A bare `none` as the only token is reported as a **legacy none** (or as ambiguous when a lesson has that key); it is not a usable declaration. The counts per class are printed beside the credited ones. Existing replies are all legacy, so the credited count starts at zero when typed citations begin.
- **Conflicting and malformed input.** The precedence table above decides the state; this is how the pieces behave. `@none` with anything else is *conflicting*: no credit, and not an explicit none. Malformed typed tokens (`@id:` with nothing after it, an id that breaks the grammar, the old `@kn-...` form, `@none2`, a lone `@`) are counted as malformed tokens and give no credit, while valid typed ids in the same reply keep theirs. Blank metadata is *empty*. The report reads old and new replies alike and rejects nothing; no write to the bus is refused.
- **Prompt-record evidence, not proof of source.** Knowing which version was cited does not show where the agent learned it (an id can be copied from a sync display, which is not measured, from a lookup or from another message), and a message is not one execution (a wrapped attempt can fail and a manual reply can follow). So the report keeps two separate statements per credited citation. The **discovery source is always "unknown"** in this plan: nothing here evidences it. The report adds only **prompt-record evidence**, labelled "recorded in a prompt for the replied-to message before the reply", and gives it only when the exposure log has a wrapper-turn record that (a) is by the same actor as the reply, (b) has a `message_id` equal to the reply's `meta.in_reply_to`, (c) lists the cited id exactly as written, the raw id and not an alias that only has the same content, among its lessons (`note_id`), and (d) was made **before** the reply. An exposure made after the reply is ignored. A reply whose `in_reply_to` names a later message (a follow-up arrived during the work, and the reply command chose the latest message) simply has no match, and the answer stays "no record", never a fallback to some other exposure of the same request. The label claims neither that the reply ran in that turn nor that the prompt was where the agent learned the lesson. **Prompt-record coverage** (credited citations that have the evidence, over all credited) is printed beside it. A missing record is never read as "not used" or as a preferred channel.
- **Every display must show the citable id.** The typed form has to be copyable wherever an agent can see a lesson. There are three code paths and four places: `format_lesson_line` in `lesson_context.py` builds the line in the wrapper prompt and, through `_kn_format_lesson_line`, the lesson text that `sync` prints; `_kn_print_lesson` in `cli.py` prints the plain text of `knowledge search`, `pull` and `onboard`; and `lesson_dict` in `lesson_context.py` builds the structured (JSON) lesson used by the lookup commands and by `sync --json`-style output, and gains a field holding the typed form. The `sync` change is display only: logging what `sync` shows stays out of scope, and the totals keep saying so. Work order 2 changes all of them, and the golden captures of each change with them.
- **Output.** Per (domain, key, version): credited citations, shown to an agent in a turn, looked up by hand, and the date of the last citation. A summary gives the replies in each state (cited, explicit none, absent, empty, conflicting, malformed); the credited citations and the legacy classes; the prompt-record coverage; and the observation range (the first and last reply dates and the number of days with at least one reply), because history that was compacted or pruned is not counted.
- **Limits.** The count is what agents report, not proof that a lesson helped; a missing citation is not proof of non-use. The command never writes and never logs a query.

### 5.3 What the numbers decide

- **Used at all (the stop signal), settled now.**
  - *Trial plan.* Written before the trial starts, and printed at the top of the report: the **explicit start date** (a UTC date), the **participating seats**, for each seat whether it replies by command (a *declaring seat*) or through the draft file (a *draft seat*), and the frozen thresholds. The plan also asks that **nobody compacts the store** during the observation period.
  - *Unit.* The number of distinct **(domain, key)** among credited citations, counted once however many versions or repeats. Five revisions of one lesson count as one.
  - *Scope.* **One known project's bus**: the report prints which store it read and does not combine stores.
  - *Window.* Half-open UTC windows `[start, start + 30 days)`, then the next 30 days, and so on, each evaluated alone, so early citations cannot hide an empty later window. The first window is the trial. The start is never derived from the data (not from the first typed reply, which compaction could remove). Before a window has ended the output is "in progress" and there is no decision.
  - *Retention evidence.* The report says whether the input it read covers the whole window. It can show that only when the oldest retained message is dated before the window's start and the archive holds no message dated inside the window. If either check cannot be made or fails (for example because compaction moved part of the window out), retention is **unknown** and no decision is made. Silence is never read as missing history: a quiet weekend or a slow day is normal and is not a gap.
  - *Eligible replies.* Replies of kind `task-response` or `review-result`, sent inside the window by a participating seat. The result is labelled **use reported in task and review replies**; consultation, challenge and question replies (sent as ordinary messages) are not part of it.
  - *Which replies can carry a declaration, in plain words.* A reply written through the draft file cannot carry a `lessons_used` field, and the draft transport leaves no mark that tells such a reply from a command reply that forgot the field (its code keeps the two byte-identical on purpose). So the first trial decides its population **by seat, in advance**: *declaring seats* reply with the command (`agenttalk reply`) and are the only seats in the ratios below; *draft seats* are counted and printed on their own line, **"channel cannot carry a declaration"**, and are outside both ratios. A declaring seat that sometimes uses the draft file shows up as missing declarations and lowers coverage, and the report prints coverage per seat so this is visible. This is a limit of the trial: the numbers describe seats that reply by command, not the whole team. Letting the draft file carry a declaration is a possible later work order; this plan does not change the draft format and does not estimate that work.
  - *Counting rules.* Times are UTC instants; a reply with a missing or invalid time is a problem row and is not usable, never read in local time. Each stored reply is counted once by message id. The participating seats are pinned for the window: roster changes during it do not change who counts, and a reply from any other seat is listed under "outside population". A later rescind cancels the work, not the fact that the agent reported its use, so it does not remove the observation; a correction is a later reply, counted on its own. A record that fails the store's integrity check is not usable input. Zero eligible replies prints **"no observations"** and no decision.
  - *Coverage, with the denominator being all eligible replies from declaring seats.* **Usable-declaration coverage** is the replies in a usable state (*cited* or *none*, section 5.2) over all eligible replies. Replies that are conflicting, legacy-only, unresolved-only, malformed, empty or absent are all in the denominator and in no numerator. So one `@none` reply among 99 without the metadata is 1 percent, and a window full of well-formed ids that resolve to nothing scores zero. As a data-quality guard the report also prints the **resolution share**: well-formed `@id:` tokens that resolved uniquely to a lesson event over all well-formed `@id:` tokens (not applicable when there are no such tokens).
  - *Minimums (frozen for the first evaluation).* Usable-declaration coverage of at least **80 percent**, and a resolution share of at least **60 percent** (or not applicable). A later change is a new policy, evaluated as a new trial, not an edit of this one.
  - *Decision.* If the window has ended, retention is shown, there is at least one eligible reply and both minimums are met: fewer than 5 distinct (domain, key) means "stop investing in sharing and work on lookup and curation instead"; 5 or more means "continue". Otherwise the report prints the reason (**in progress**, **no observations**, **retention unknown** or **insufficient coverage**) and makes **no decision**. A genuine zero may be printed as a descriptive count, labelled as not a decision.
  - *What it is.* A self-reported-use proxy. It is not evidence of cross-team sharing, of usefulness or of anyone using repository files. The threshold of 5 is provisional; the operator can change it before the trial starts.
- **Where use comes from:** the discovery source is not measured, so the report does not split use by channel. It shows prompt-record coverage (section 5.2). A low coverage means that question cannot be answered yet, and nothing is inferred from it.
- **Safety signals stay unconditional.** A project's policy being overwritten, retired advice returning and raw project material leaving its repository concern the later steps; work orders 1 and 2 write no project files. Generic advice crowding out needed local guidance can be observed during the measurement, because today's selector can already do it; the lessons report is where it shows.
- **Not measured:** whether a lesson prevented a mistake. That needs opportunities counted beside failures and is left for later; the pilot's displaced-needed-advice count (six of 19) is the baseline to compare against if the tag rule is ever switched on.

## 6. Work orders

Sizes are engineer-days for one builder plus one review round, with the existing store and event code reused and no new dependency. They are rough: the reviewer's range for this pair is about 6 to 14 days around the central figure. Calendar time is longer: two releases (readers first, writers one release later) and then at least a month of observation before the stop signal can be applied.

| # | Work | Days | Status |
|---|------|------|--------|
| 1 | **Lessons report** (read-only; evaluates the tag rule over today's lessons and the exposure log): 1.5. **Domain-qualified supersession**: 1. **Naming and testing today's task-tag inference** on a real wrapped record: 0.5. | 3 | authorised at about 9 days for 1 and 2 together |
| 2 | **Exposure schema version 2** with manual-lookup logging, `--from`, the `recipient` field, chunking, complete-lookup counting and the two-release transition: 3. **Typed, copyable event id in all four lesson displays** (wrapper prompt, lookup text, structured output, `sync` display), reply guidance and the golden captures: 1. **Lesson-use report**: typed credit for any accepted event id, the reply-state precedence table, prompt-record evidence, the distinct-lesson unit, the explicit trial start with retention evidence, the pinned seats with their per-seat lines, and the coverage rules: 3. | 7 | as above |
| later | **Tag rule behind the `lesson_injection` setting** (default `all`) | 1.5 | **not authorised**; after the numbers |
| 3 | Read-only overlay (section 8) | about 5 | **not authorised**; follows the measurement |
| 4 and later | Parked (Appendix A) | | **not authorised** |

Work orders 1 and 2 total **10 days**. With the tag-rule setting as well it would be 11.5. The setting is held back because the measurement does not need it (the report already shows what the rule would do) and because it changes what agents see.

### What changed from the previous version (9 days, round 7), and why

- **The tag-rule setting moved out (minus 1.5):** it is a behaviour change, not a measurement.
- **Naming and testing the task-tag inference (plus 0.5):** the reviewer found that tags come from the kind, subject, identifiers and a short list of metadata keys, not from the body or a task-tags field.
- **Complete-lookup counting (plus 0.5):** a half-written lookup must not be counted.
- **A typed, copyable id in the lesson line, with reply guidance (plus 0.5):** it was a short id before; it is now a typed form that cannot collide with a key.
- **The trial window, retention evidence, pinned seats and the reply-state rules (plus 0.5, round 10):** an explicit start instead of a derived one, a check that the retained input covers the window, seats declared in the trial plan with a line per seat, and the full precedence table.
- **The use report (plus 0.5 net, from 2 to 2.5, before round 10):** typed credit, legacy classes, source proof, coverage and the distinct-lesson unit were added; the reply-time and exposure-evidence resolution of bare keys was dropped, because it could credit the wrong version.

Net: 9 − 1.5 + 0.5 + 0.5 + 0.5 + 0.5 + 0.5 = **10 days**. Round 9 changed the contracts but not the number: accepting any valid event id is the same parser work as the narrower form; the conflict classes replace a branch; the window, the eligible replies and the coverage rules are arithmetic inside the report's 2.5 days; and the three extra lesson displays are one-line changes inside the 1 day already set for the typed line and its golden captures. This is one day above the authorised "about 9" and should be reconfirmed; the paragraph below says what could be deferred.

### What was not cut

Domain-qualified supersession; exact typed citations with legacy and key-only tokens reported but never credited; explicit `none` kept apart from absent metadata; no query text or body in the log; the two-release reader-first transition; complete-lookup counting; the coverage and observation labels; the `--from` actor rule. None of these was bought down by guessing attribution.

### What this loses, said plainly

- **No baseline from history.** Every existing reply is a legacy citation, so the credited count starts at zero when typed citations begin, and the first month's number depends on agents copying the typed form from the lesson line. The report still prints the legacy counts, so there is a rough picture from the first day, but they are not credited and not counted for the stop signal.
- **Only seats that reply by command are measured.** The draft file cannot carry a declaration and the two reply paths cannot be told apart afterwards, so the first trial predeclares which seats count. The result describes those seats, and it says so. Extending the draft file is a possible later work order, not estimated here.
- **No discovery source at all.** Nothing in this plan evidences where an agent learned a lesson. The report shows only prompt-record evidence (a lesson was recorded in a prompt for the replied-to message before the reply), which says it was available, not that it was used or where it was found. A later, optional `--request <id>` on manual lookups (about 0.5 day) would let a lookup be tied to a piece of work; it is not part of this work. The stop signal does not need a source.

### Does 9 days hold a correct measurement?

Not exactly: 10, with the setting already left out. The earlier claim that the smallest correct version is 6 days is withdrawn. With the attribution choices settled, **the smallest correct version is work order 2 (7 days) plus domain-qualified supersession (1 day): 8 days**, and it would lose the lessons report the operator asked for. Landing on exactly 9 would take deferring the naming-and-testing item (0.5 day) and half a day of the trial rules, which I do not recommend; deferring the naming item alone leaves 9.5. It is the less harmful of the two to defer, but the report would then misdescribe what "the task's tags" are. If time runs over, the first things to defer are the `recipient` field on `onboard` (a few hours) and that item. The two things that cannot be deferred are the typed credit rule and the complete-lookup counting.

## 7. Evidence gates and kill signals for the measurement

Each guard is removed on its own, and the exercise that covers that guard must be the one that turns red.

| # | Exercise | Expected |
|---|---|---|
| M1 | Today's selector on a real wrapped record, and the report's evaluation of the tag rule: untagged `process`, untagged task scope, matching tags, non-matching tags, five slots full; a record whose subject is generic but whose body mentions CI | the selector is unchanged (the existing golden captures do not change); the report names the tags it derived (from kind, subject, identifiers and the listed metadata keys) and shows no `ci` tag for the body-only case |
| M2 | Lessons report over a store with untagged and tagged lessons and an exposure log | lists the lessons the rule would skip and how many past shows were of them; writes nothing |
| M3 | Supersession across domains: a lesson in domain A supersedes key `x`, domain B has its own `x` | B's `x` stays active; only A's `x` is superseded |
| M4 | An older exposure reader meets version-2 lines; the new reader reads version 1 and 2 | older reader reports them as malformed lines and does not fail; new reader reads both |
| M5 | Manual lookups: an empty result, a result of more than 50 notes, code notes, a typed query, no resolvable actor, `onboard --for`; and multi-part lookups: a reader between two appends, a writer interrupted before its last part, a duplicated part, parts that disagree on `parts` | zero-item event, chunked events with one `lookup_id`, code notes recorded by type, no query text or body in the log, a notice and no log when there is no actor, the recipient recorded apart from the actor; an incomplete or conflicting group is shown separately and never counted |
| M6 | Use report over replies that cite: a typed `@id:` of a generated id; `@id:kn-lesson` (a stored id that is not generated-shaped); an `@id:` of a non-lesson event, of an unknown id and of an id found twice; the publication id and the approval id of one version in the same reply; two `@id:` of the same lesson; `@id:<resolved>,@id:`; a bare key (one domain, two domains); a bare `kn-...` that is an event id; lessons keyed `none` and `kn-deadbeef1234`; `@none` alone; `@none,@id:<resolved>`; `@none,plain-word`; a reply of only legacy tokens; a reply of only well-formed ids that resolve to nothing; the old `@kn-3f9a01c2b7d4` form; blank metadata; no metadata; plus a pruned range | the state follows the precedence table (conflicting, then cited, then none, then legacy-only or unresolved-only, then malformed, then empty, then absent); only a uniquely resolved lesson event is credited, once per (domain, key, content version) per reply; `@id:<resolved>,@id:` is cited with one malformed token; legacy-only and unresolved-only replies get no credit and are unusable; the lessons keyed `none` and `kn-deadbeef1234` are never lost or credited to another record; only `@none` alone is an explicit none; the observation range and the "sync is unmeasured" label are printed; nothing is written |
| M7 | A lesson read as version A in the turn for task X, version B accepted and seen in an unrelated lookup, then a reply to X citing the bare key; and the same reply citing the typed id of A | the bare key is unattributed (not credited to A or B); the typed id is credited to A |
| M8 | Every lesson display, for a generated id and for a stored id such as `kn-lesson`: the wrapper prompt, `knowledge search` / `pull` / `onboard` text, the structured (JSON) lesson output, and the lessons shown by `sync` | each shows the typed form `@id:<id>`, copyable as printed; the golden captures change in exactly those four places and nowhere else in the prompt; `sync` still records no exposure |
| M9 | Prompt-record evidence for a credited citation: (a) the same actor has a wrapper-turn exposure for the replied-to message that lists the id, made before the reply; (b) the same exposure made after the reply; (c) a wrapped attempt that failed, then a manual reply to the same message citing the id; (d) a follow-up arrived during the work and the reply's `in_reply_to` names the later message; (e) an exposure by another actor; (f) no exposure | (a) "recorded in a prompt for the replied-to message before the reply"; (b) none; (c) the same label as (a), and the report does not say the reply ran in that turn; (d), (e), (f) "no record", with no fallback to another exposure of the same request; in every case the discovery source stays "unknown", the credit is unchanged, and prompt-record coverage is printed |
| M10 | The stop signal, with an explicit start: (a) one `@none` reply and 99 without the metadata; (b) five early credited lessons and an empty later window; (c) five revisions of one lesson; (d) the original first typed reply compacted away; (e) a complete month with no messages on weekends; (f) a window not yet over; (g) replies from two stores; (h) a full window with both minimums met and 4, then 5, distinct (domain, key); (i) a window where every reply holds well-formed ids that resolve to nothing; (j) a window where every reply is conflicting; (k) a window with no eligible replies | (a) coverage 1 percent: "insufficient coverage", no decision; (b) the later window is evaluated alone and shows its own result; (c) one distinct (domain, key); (d) the start does not move; retention is "unknown" because part of the window was moved out; no decision; (e) retention is shown and the decision is allowed (silence is not a gap); (f) "in progress"; (g) the report refuses to combine stores; (h) 4 gives the stop signal, 5 gives "continue"; (i) coverage and resolution share are zero: "insufficient coverage"; (j) coverage zero: "insufficient coverage"; (k) "no observations" and no decision; a zero outside (h) is a descriptive count, not a decision |
| M11 | The trial population: (a) a draft seat with eligible replies; (b) a declaring seat that sometimes replies through the draft file; (c) a roster change during the window; (d) a reply from a seat not in the plan; (e) a later rescind of a reply that declared a lesson; (f) a stored reply whose file fails the integrity check; (g) the same message id seen twice; (h) a reply with a missing or invalid time | (a) counted on its own line, "channel cannot carry a declaration", in neither ratio; (b) its missing declarations lower coverage and the per-seat line shows it; (c) membership stays as planned; (d) listed under "outside population"; (e) the observation stays; (f) not usable; (g) counted once; (h) a problem row, not usable, no local-time guess |

**Kill signals.** The stop signal is in section 5.3 (fewer than 5 distinct (domain, key) credited as used in the first window from the trial's explicit start, with retention shown and the frozen coverage minimums met; 5 is a provisional figure). For the overlay: if the first teams to try it never add a project lesson file, stop. The safety signals in section 5.3 are unconditional.

## 8. The read-only overlay (not authorised; follows the measurement)

**What it is.** `knowledge onboard` and `knowledge search` (and no other command at first) would also read lesson files from the project's own checkout, on the fly. It would not copy them into the bus store, would not write anywhere, and would not make them eligible for the five injected prompt slots. Whether the prompt lookup should ever include them is left open; the recommendation is no, so the overlay stays something an agent asks for.

**The contract (proposed).**

- **Where.** A folder in the project's checkout (the git top folder of the working folder) named `.agenttalk-knowledge/lessons/`, one JSON file per lesson. The exact file form is decided when the overlay is authorised; it would be the same fields that `knowledge publish` takes for a lesson today (key, text, scope, trigger, tags, evidence reference, owner, review date, expiry), and nothing from the parked format.
- **Committed files only.** Only files that git tracks and has not modified are read. That is locally committed text, not necessarily reviewed text: a local commit or a commit on an unreviewed branch passes the check. Calling it reviewed needs an approved revision source (for example, only files reachable from the project's protected main branch), which must be decided before the overlay is authorised. Untracked or edited files are skipped and counted.
- **Marked.** Every overlay lesson is shown after the lessons from the store, with the label "from this project, not reviewed here", and never merged into the store's ranking.
- **Never written.** Nothing about an overlay lesson is written to the bus store. Counting its use needs an identity that the report's (domain, key, version) does not have: an overlay lesson has no domain or event id, and a content hash alone does not fit. A synthetic namespace (for example a project namespace plus the file path) and its typed citation form are decided before authorisation; until then overlay lessons are not counted in the use report.
- **Limits.** At most 200 files, 64 KB per file, 500 bytes of lesson text (a deliberate overlay cap, smaller than the store's note-body limit of 2,000 bytes; 500 bytes is the store's limit for the trigger and evidence fields) and 1 MB in total; anything beyond is skipped and counted.
- **Confidentiality refusal.** A file whose text contains a configured forbidden string is not shown and is counted. This needs a setting that does not exist yet (it is part of the export check proposed in issue #293), so the overlay would add the smallest version of it. A forbidden-strings list is a guard against pasted names, not proof that a file is safe.
- **Removal.** Removing or editing a file removes the lesson from the next lookup; there is no retirement, abort or reinstatement protocol.
- **Same key as a stored lesson.** The stored lesson wins; the overlay entry is hidden and counted.
- **No domain, anchor or freshness checks** beyond the lesson's own review and expiry dates.

**The risks, plainly.**

- **Untrusted text becomes advice an agent reads.** Anyone who can merge a change to the project can put text in front of every agent that looks. The mitigations are the label, the reviewed-commits-only rule, the size limits, the placement after reviewed lessons and keeping it out of the injected prompt. They reduce the risk; they do not remove it, and a threat review is part of the authorisation.
- **Private material in a repository.** Lesson files can name a client or a person. The refusal list helps; the real control is that the folder is reviewed like any code.
- **Ranking.** The overlay cannot be ranked next to stored lessons with the same evidence; showing it last is deliberate and may hide a useful lesson.
- **A lasting format.** Even a small file form must stay readable across releases once a project commits files in it. That cost is the reason the overlay waits for evidence.

**Size.** About 5 days: the reader and the git check 2, labelling, limits and the minimal refusal setting 1.5, logging 1, tests 0.5. Report integration waits for the identity decision above and is sized then. It is **not authorised**. It starts only after the measurement reports exist and the operator reconfirms.

## 9. Technical notes (checked at master `7e36cfb7`)

- Notes are events in `.agenttalk/knowledge/notes.jsonl` (`knowledge.py`), folded to a current view per (domain id, key). Lessons carry a scope, tags, status, review and expiry dates (both required), and `supersedes`. Tags are part of the curation-bound content, which is why a tag change is a new publication.
- `lesson_superseded_keys` builds one set of bare keys across all domains (`knowledge.py` 947-957); work order 1 changes it to (domain, key) pairs.
- The selector is `select_lessons` and `rank_lessons` in `lesson_context.py`; the clause to change is the empty-tag allowance, and the setting gates it. The default limit is 5 (`DEFAULT_LESSON_LIMIT`).
- `exposure_event_problem` accepts only schema version 1 and `surface == "wrapper_turn"` with turn identity, a prompt-block hash and one to five lessons; the reader returns a problem list rather than raising. `record_exposure` has one production caller, `wrapper/run.py`.
- The lesson line is built by `format_lesson_line` as `key [scope] trigger - body (evidence; marker)` for the wrapper prompt; plain lookup text comes from `_kn_print_lesson` in `cli.py`; the structured form is `lesson_dict` in `lesson_context.py`. `sync` prints through `_kn_format_lesson_line` (text) and `lesson_dict` (structured). None carries the event id today. Work order 2 adds the typed `@id:` form to all of them.
- The reply metadata `lessons_used` exists, but no code under `src` reads or counts it. `docs/ROADMAP.md` already lists a display-only usage report as a first step.
- Task tags are inferred by `record_lesson_context` from the kind, subject, correlation identifiers and the metadata keys `assignment`, `artifact_type`, `domain`, `lane_id`, `risk`, `risk_class`, `review_ref`, `review_type`, `reviewed_ref`, `scope`, `status`, `work_id`, `wp_id`; the body and `work_item` are not read.
- The draft reply channel (`reply_transport.py`) builds the stored metadata itself (status, correlation, an operation nonce and digest) and carries no typed `--meta`; the worker prompt names the draft file as the preferred channel (`wrapper/prompt.py`). Its code states byte-parity with a command reply, so a stored draft reply has no mark that separates it from a command reply without `lessons_used`. Compaction (`store.py`) moves a contiguous prefix of messages into `archived/compacted/`, which no live reader reads back.
- The key pattern is `[A-Za-z0-9][A-Za-z0-9_.-]{0,63}` (`_NOTE_ID_RE`; generated ids are `kn-` plus 12 hex characters, but any matching id is stored and valid). The key pattern is `[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}`, so `none` and `kn-deadbeef1234` are valid keys and no key can start with `@`. `BODY_MAX_BYTES` is 2000 (note body); `LESSON_TEXT_MAX_BYTES` is 500 (trigger and evidence fields).
- The exposure event carries `agent`, `turn_id`, `message_id`, `request_id`, `exposed_at` and, for each lesson shown, `note_id` (the event id of the displayed note) and a fingerprint. A reply carries `meta.in_reply_to` (the message it answers) and `request_id`, and no turn id. A wrapped attempt creates a fresh turn for each drive, and a manual reply uses the same transport but runs outside any wrapped turn; `reply --to-request` answers the latest non-control message of the request, so a follow-up that arrives during the work can make `in_reply_to` name the later message. The same-message match in section 5.2 is therefore prompt-record evidence only.
- No forbidden-strings check exists in the code today; it is proposed in issue #293.
- `knowledge.py`, `lesson_context.py` and `install_skills.py` are unchanged between `c180f807` and `7e36cfb7`, so the claims the earlier rounds checked still hold.

---

## Appendix A: Parked: the full import and promotion design

**Parked: the full import and promotion design, kept for reference; not authorised; revisit only if the measurement shows lessons are used and a sharing need exists (kill signal: few lessons cited as used within a month).** It is kept as it stood after round 6 of the review and is not polished further. It is not part of the plan above.

How to read it: headings are prefixed with `A.`, and the old numbering follows (so the old "section 9.3" is "A.9.3"). Cross-references inside the appendix to the old sections 4, 8, 11.1 and 11.5 now point to sections 4 and 5 of the main document (the selector, manual lookup logging and the lesson-use report), where the current versions live. Sizes, exercise counts and authorisation labels in the appendix are those of the parked design and are no longer current; the two work orders it called 1 and 2 are replaced by section 6 above.

### A.4 Selector parts that depended on tracked lessons

The one rule had a fourth clause: *4. If it came from a project's tracked files, the project has switched on injection of tracked notes (default off), and it only takes a slot that no other eligible lesson wants, so tracked lessons rank after every non-tracked eligible lesson.* Rule 4 needed the import log (A.9.3) to know that a lesson was imported, so it changed nothing until the import existed.

##### Where "came from tracked files" is known (rule 4)

Rule 4 needs to know that a lesson was imported from a project's files. That fact is kept in the import log (section 9.3), which does not exist until work order 4. Work order 1 builds the rule with an origin check that reads the log, and until the log exists no lesson counts as tracked, so rule 4 changes nothing yet.


### A.5 How tracked notes look and travel (overview)

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

### A.6 Retirement: retired stays retired

A note retires through a reviewed change: its file gets the state `retired`, a reason and a retirement identifier. The file keeps the note's full text and stays in the folder. Deleting a file never retires a note, and never revives one.

- **Retired beats accepted.** If a machine has seen a retirement, no import may turn that note back on, whatever the order of the commits.
- **Coming back is deliberate.** A retired note returns only by a new reviewed file that names the retirement it undoes, imported by a curator with `--reinstate`. After that, an old copy of the retirement is ignored.
- **A machine that never saw the retirement** cannot learn about it from an old clone. So the import asks the project's real remote for its latest commit and refuses if the source is behind. Offline, the design guarantees only what section 9.6 says.
- **The one exception** is a lesson retired because a skill now carries its advice. On a machine whose installed skill does not have that advice yet, the retirement waits and the lesson stays active, so at the moment the retirement is applied the advice is available in one place or the other (section 11.3). A later loss of the skill is a separate case: it is met with a warning, not a guarantee.

### A.7 Overrides: what a project can and cannot switch off

- **Can:** switch off one lesson that the selector controls, by naming it as (domain, key) in an override record, with a reason. The override is reviewed like any note and imported by a curator. It hides that lesson from this project's selections and from nothing else.
- **Cannot:** remove or change the text of a shipped skill. A skill is loaded by the vendor tool on its own, and no note can edit it, so the loader gets no skill-rewriting mechanism.
- **An advisory exception to a skill** (for example "in this project we do not follow the shipped advice on X") goes in the vendor's own project instruction file (section 10). That shows the exception to the agent; it does not suppress the skill.
- **Precedence**, highest first: a retirement; a project override; the lesson's normal eligibility under section 4.


### A.9 Contract: the tracked note file, trust and import

#### A.9.1 File record (format 1, proposed)

- One note per file. UTF-8 without a byte-order mark, LF line endings, JSON with sorted keys, two-space indent and a final newline.
- Location: `.agenttalk-knowledge/notes/<domain_id>/<key>.json`. Each path component is encoded so it can be created on every system, and the encoding is reversible: `:` is written `%3A`; a trailing `.` is written `%2E`; and a component whose name before its first `.` is a Windows device name (`CON`, `PRN`, `AUX`, `NUL`, `COM1` to `COM9`, `LPT1` to `LPT9`, in any letter case) has its first letter written as `%` and two hex digits, so a key `CON` is stored as `%43ON.json`. The importer decodes a path before comparing it with the identity inside the file. An encoded component longer than 100 characters is replaced by its first 60 characters, a `~`, and the first 16 hex characters of the SHA-256 of the unencoded component, because a valid 128-character key made mostly of colons would otherwise expand beyond the usual Windows limit. The name is then only a label for people and git: the identity is read from the file contents and never decoded from a shortened name. The shortened form is 60 + 1 + 16 = 77 characters before the `.json` suffix. Two different identities whose derived names collide are refused, never overwritten. The limit on a whole path is a separate matter from the limit on one component, and the format gate reports it separately. Keys that differ only in letter case are duplicates and are refused, because a case-insensitive disk cannot hold both.
- Identity is the pair (`domain_id`, `key`), as in the store. The file repeats both; a file whose name is not the name derived from its contents by these rules is refused.
- `format` is required. A reader that does not know the number refuses that file, names it and the number, and loads nothing from it.
- Overrides are separate files under `.agenttalk-knowledge/overrides/` (section 9.7). The folder also holds `project.json` (section 9.5).

Fields, and the three variants of the `state` block:

| Field | Meaning |
|---|---|
| `format` | `1` |
| `domain_id`, `key`, `type`, `body` | as in the store; `type` is any note type |
| `lesson` | present for lessons only: scope, trigger, evidence reference, owner, tags, supersedes (bare keys, read as keys in the same domain), review date, expiry (both required, as today), and the optional nested `anchor`, which the store already accepts and binds into a lesson's content |
| `anchor` | present for code notes (seam, gotcha, decision, pointer), as today |
| `verified_against_sha` | for code notes only (seam, gotcha, decision, pointer): the full commit of the project's code at which the note was last verified, copied by the export from the source note. The staleness check marks a path- or symbol-anchored code note without it as stale, so import keeps it, and an **accepted** code-note record that lacks it is imported as proposed only (a retired record is never held back by this rule, section 9.4). Lessons never need it: their nested anchor is provenance, not freshness authority (section 9.4). It is not part of `content_id` |
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
  "body": "The insight, in behaviour terms.",
  "content_id": "<64 hex characters>",
  "domain_id": "process",
  "format": 1,
  "key": "example-lesson-key",
  "lesson": {
    "anchor": null,
    "applies_to": ["ci"],
    "evidence_ref": "a neutral reference",
    "expires_at": "2027-06-01T00:00:00Z",
    "owner": "an-agent-name",
    "review_after": "2027-01-01T00:00:00Z",
    "scope": "test",
    "supersedes": [],
    "trigger": "when this applies"
  },
  "reinstates": [],
  "retirement": null,
  "reviewed": {"at": "2026-10-07T00:00:00Z", "by": "a-curator-name"},
  "source_provenance": {
    "author": "an-agent-name",
    "created_at": "2026-09-01T00:00:00Z"
  },
  "state": "accepted",
  "type": "lesson"
}
```

#### A.9.2 Two hashes, and what each is for

The existing hash in `knowledge.py` covers the note's text, anchor and lesson content, **and** its author, creation time and the id of the event it replaces. Those three differ on every machine, because each import creates a new local publication. So a file can never be compared to a local note with that hash.

- **`content_id` (portable, in the file).** Hash of: type, domain, key, text, anchor, `supersedes_key`, and the lesson content (scope, trigger, evidence reference, owner, tags, supersedes, review date, expiry, and the nested lesson anchor). It leaves out author, creation time, event ids, state, curator, `verified_against_sha` and provenance. Two lessons that differ only by their nested anchor therefore have different `content_id`s. Two records with the same `content_id` are the same note.
- **Local `payload_hash` (existing).** Used only for the store's own publish-then-curate chain, never written into a file.

So a change of tags, trigger, expiry or supersedes changes `content_id` and is a changed note. A change that only touches `reviewed` or `source_provenance` leaves `content_id` alone and is a metadata update: nothing is republished, and the importer only appends the missing approval step if one is needed. A change of `state` is **not** a metadata update: accepted to retired appends a retraction, and retired to accepted is a reinstatement. Both follow the table in section 9.4.

#### A.9.3 Trust

- **Who may accept.** Only a curator for the note's domain on the receiving bus, or the lead through the existing override. The author or reviewer named in a file gives no authority. The importing curator appends the events; the file's original author is kept in the import log, not in the event.
- **Mapping into events.** The local publication takes author = importing curator, creation time = import time, and no replaced-event id (`supersedes_key` is kept). The lesson owner comes from the file. A lesson's curator is filled by the approval step, as today.
- **Trusted source.** Import reads files from git at one named commit, not from the working folder. A working folder with uncommitted edits to the notes folder is refused. The commit must be the project's latest main-branch commit as section 9.5 defines it. A different commit is allowed only with `--as-proposed`, which imports everything as proposed.
- **Domains.** The `process` domain exists everywhere. Any other domain must be listed in the project binding (section 9.5) and exist in the receiving bus's registry. A record for any other domain is refused and listed, never dropped. On a clean machine the registry is empty, and today's `domain` command is read-only (`list`, `show`, `check-path`, `validate`), so there is no command that creates a project domain. How domains get created is an open decision (see "Open before work order 4"); until it is closed, the fresh-checkout exercise is limited to the `process` domain.
- **Anchors and the checkout root.** Today anchor checks resolve against the bus root. Tracked code notes therefore need a separate change that resolves anchors against the bound checkout root (work order 6). Until it ships, accepted tracked code notes import as proposed only (a trusted retirement of a code note is still carried out), and this design does not claim to close issue #245.
- **The import log.** The existing event format has no room for provenance, and it is not changed. Instead the import keeps `imports.jsonl`, next to `notes.jsonl`: source commit, path, `content_id`, `retirement_rev`, the local event ids written, the steps done, and the retirement identifiers reinstated. It is append-only and kept by reset, like the notes. It holds decisions about notes, not notes: the store of notes stays `notes.jsonl`, and the state of a note is always read from its events.
- **Crash-consistent writes.** The events and the log are two files and no lock makes two files atomic, so the order is fixed. Under the store's shared lock the importer (1) generates the event ids it will use and appends an **intent** line to the log naming them and the steps planned; (2) appends the events; (3) appends a **completion** line. A record counts as complete only when its completion line exists. A rerun that finds an intent without a completion checks each planned event id: it appends only the events that are missing, then writes the completion line. It never reads an intent as success, and it never reads events alone as complete, so the origin and reinstatement facts cannot be lost between steps. A change made by another curator between the intent and the rerun is detected by the state check and stops the rerun for that key.
- **What readers see meanwhile.** Events named by an intent that has no completion line are **quarantined**: every ordinary consumer (the selector, `pull`, `search`, `onboard`, the lessons report and the use report) ignores them, because the one shared reader that folds the note events also reads the import log and drops those event ids before folding. So after the intent line nothing has changed; after a publish event the note is not visible, even with `--include-uncurated`; after an approval event the previous accepted version is still the active one; and after the last event but before the completion line the new version is still not visible. The completion line releases all of a record's events at once, so a reader never sees a half-imported note, and the tracked-origin and injection rules apply from the first moment a note is visible.
- **A consistent view of two files.** The writer's lock does not by itself give a reader a consistent view of two files. The reader therefore reads the log, then the notes, then the log again; if the log changed in between (a new intent, a completion or an abort), it starts over. An approval appended after the first log read is never shown without its intent. This is a build gate for work order 4(b), with a test that crosses that boundary.
- **Effective history, for readers and writers.** The result of that shared reader is the *effective history*: all events minus the ids named by a pending or aborted intent. Every consumer that folds notes, **including the ordinary curation commands** (`publish`, `curate verify`, `curate retract`), uses it, so a hidden event can never be chosen as a parent. In addition, a curation command aimed at a key with a pending intent is refused with "an import is pending for this key: complete or abort it", so no curator is surprised by an import that finishes after their change.
- **Abort.** Suppose another curator's publication and approval, Q, lands on the key after the intent. Appending the planned approval of the old publication P would then fail the store's current-prior-event check, and retracting P or the key would retire Q, a valid result that belongs to someone else. So the curator runs `import --resolve <key> --abort`, and the importer appends an **abort line** to the log naming the intent. The intent's planned ids then stay out of the effective history for good (they are never released and never rewritten), Q stays the active version, and the key is free for a fresh intent against the current state if the curator then chooses "take tracked" (section 9.4). A rerun that finds the key unchanged since the intent may instead complete it. Quarantine and abort are read-time rules over the log; nothing in `notes.jsonl` is changed.
- **History is never rewritten.** Import only appends events and log lines.

#### A.9.4 Append sequences (what an import does in each state)

The import reads the note's state on the receiving bus from its events, compares `content_id`, and appends only the steps that are missing. It does nothing only when the target state is already complete.

| Receiving bus holds | File says accepted | File says retired |
|---|---|---|
| nothing | publish, then approve (curate) | publish the file's content, then retract it |
| a publication that was never approved, same `content_id` | approve it only, if the source file still says accepted (this is the retry after an interrupted import, and the source's current state decides) | retract it, whether the publication came from this import or earlier as a proposal |
| a publication that was never approved, different `content_id` | conflict (below) | publish the file's content, then retract it |
| accepted, same `content_id` | nothing | retract it |
| accepted, same `content_id`, a code note whose file `verified_against_sha` differs from the local baseline | verification update (proposed importer behaviour: today's curation path copies the old baseline and has no baseline-update step, so the importer builds this verify event itself, with the current effective prior event as its parent): a verify event carrying the new baseline is appended on the current publication; nothing is republished, because the baseline is not part of the content. Only if the new commit is reachable in the bound checkout, and freshness is then computed as usual: a baseline never overrides a changed anchor | retract it |
| accepted, different `content_id` | conflict (below) | publish the file's content, then retract it |
| retired here by retirement R | an accepted file carries no retirement of its own, so it is matched against the local retirement through its `reinstates` list: if the list names R and a curator passes `--reinstate`, publish and approve, and log every identifier in the list as reinstated; otherwise refused and the note stays retired | a retired file with the same `retirement_id` R: nothing, unless `retirement_rev` differs from the logged one, in which case the retirement is re-evaluated (below). A retired file with a different `retirement_id`: nothing appended; the newer identifier is logged and reported |
| accepted after a reinstatement here, and the file's `retirement_id` is in the log's set of reinstated identifiers (the union of every `reinstates` list imported so far) | as the accepted rows above | ignored as an old retirement; reported |
| accepted after a reinstatement here, and the file's `retirement_id` is **not** recorded as reinstated (a second retirement, even of identical text) | as the accepted rows above | retract it: this is a new retirement and it applies |

**Retirements that depend on a skill, and corrected retirements.**

- *The local lesson exists.* For a retirement with `promoted_to`, the retirement row applies only if the local accepted lesson has the same `content_id` as the one named in `promoted_to` and the skill check in section 11.3 passes. If the local lesson has a different `content_id`, the case is a conflict, not a retraction: the skill carries different advice from the one this machine holds, so retracting it would leave the machine's own version nowhere.
- *No local lesson (a fresh store).* The marker check does not need a local lesson. If every installed skill copy lists the key and the `content_id` in `promoted_to`, the importer publishes the file's content and retracts it, so the tombstone stops an old accepted file from reviving the lesson. If not, it appends nothing and reports "not loaded here". Running the import again after `install-skills` takes the first branch.
- *A corrected retirement.* If `retirement_rev` of a file differs from the logged one for the same `retirement_id`, the importer re-runs the checks with the new metadata and logs the new revision. If the retirement has **not** been applied here yet, it is applied or deferred under 11.3. If it **has** been applied (the retraction is terminal), the retraction stands, and the importer never silently reopens it. A correction that adds `promoted_to` whose skill check fails puts the lesson in a visible state, "retired, awaiting its skill": the import report, `pull` and `onboard` name it, `search --include-stale` finds its text, and the manual recovery is the ordinary reinstatement (a new reviewed file that names the retirement in `reinstates`, imported with `--reinstate`). A correction that removes `promoted_to` makes it an ordinary retirement and clears that state.

**Code-note baselines, and why lessons are different.** A tracked code note with a path or symbol anchor is marked stale by `compute_staleness` when it has no `verified_against_sha`, so an accepted code-note record that lacks one is imported as proposed only, and one that has it keeps it as its baseline. Re-verifying a code note at a newer commit does not change its `content_id`, so it is the verification-update row above, not a republish. Lessons follow different rules today: `compute_lesson_state` treats a lesson's nested anchor as provenance and never as freshness authority, and lesson freshness depends on dates, status and key only. So the import never applies the code-note rule to a lesson, a lesson needs no baseline, and an anchored lesson imports as accepted like any other. **Readiness governs activation only.** A baseline, or an anchor that resolves, decides whether an accepted record may be activated; it never blocks an authorised retirement. A trusted retired file is carried out whether or not the note has a baseline (publish the file's content, then retract it, as in the table), and the importer either honours a retirement or rejects it with a stated reason; it never turns it into a proposal and reports it as retired.

Why a retired record is first published: the store refuses a retraction that has no earlier event for the same note, so retiring into a fresh store needs the publication first. It is the file's own content, appended as an unapproved publication and retracted at once; it is never shown to agents.

**Conflict.** The store has one current view per (domain, key), so there is no "keep both". A curator chooses per key:

- **take tracked:** publish the file's content, then approve it. The earlier accepted version stays active until that approval is appended;
- **keep local:** append nothing and record "kept local" in the import log;
- **decide later:** append nothing; the key is listed again on every import until decided.

**Records that arrive as proposed** (imported with `--as-proposed`, for an unknown commit, or accepted code notes before work order 6) are appended as unapproved publications. They never reach agents' turns, are visible with `--include-uncurated`, and are listed in every import report with the reason. A later import from the trusted commit finds the same `content_id` and **applies the source's own lifecycle state**: approval if the file says accepted, and retraction if it says retired. A record that arrived as proposed from a retired file is therefore never approved by a later trusted import. A curator can retire a proposed record with a reason.

#### A.9.5 Binding a project to a bus, and freshness

A file cannot name its own trust anchor, so the binding is created on the receiving bus by a curator, once, and stored in the bus configuration, not in the repository:

`knowledge bind --source <repository address> --main-ref <branch reference> --checkout-root <folder> --domains <list>` (proposed, not runnable yet).

- The repository's `project.json` holds only a random `project_id`, created once. The bind command records it with the address, the main reference, the checkout folder and the allowed domains.
- Import refuses a source whose `project_id` is not the bound one, and a file whose domain is not in the bound list. This is also how the confidentiality policy (11.2) knows what "same project" means.
- **How the current tip is obtained.** At import time the importer asks the bound address for the current value of the bound main reference (the equivalent of `git ls-remote`). The source commit must be that commit, or an ancestor of it that the clone also contains with nothing newer touching the notes folder. If the clone lacks the remote's tip, the import says "fetch first" and stops.
- If the remote cannot be reached, the default is to refuse.

#### A.9.6 What the design guarantees offline

With `--offline`, the importer skips the remote check and prints the source commit's date and the sentence "retirements made after this date are not known". The guarantees are only these:

- a retirement the receiving bus already recorded is never undone;
- the import is consistent with the clone as it stands;
- nothing newer than the clone can be known.

A stale clone on a machine that has never seen a retirement can therefore import the old accepted note. That is a stated limit, not a defect we can remove without the network. It is why the default refuses.

#### A.9.7 Overrides

An override file has its own random `override_id`, names its target as (`domain_id`, `key`) and gives a reason. It is imported by a curator and recorded in the import log; the selector reads the active overrides from there. An override of a key that does not exist yet is kept and applies when the key appears. An override has no effect on shipped skill text (section 7). Withdrawing an override is a new override file with `state` set to `withdrawn` and a `withdraws` list naming the `override_id` of every override occurrence it withdraws (carried forward like `reinstates`). The import appends a withdrawal line to the log, and the old line is never deleted. An override whose `override_id` is in the log's withdrawn set is ignored whenever it arrives, even on a bus that sees the withdrawal first. A later, new override of the same target has a new `override_id` and applies normally; a withdrawal never acts as a tombstone for the target.

### A.10 Contract: what agenttalk manages and what the vendors cover

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


### A.11 Contract: confidentiality and promotion (the parts parked; logging and the use report are in sections 5.1 and 5.2)

#### A.11.2 Confidentiality

Two separate policies:

- **Same-project sync** (the bound repository and its bus). Applies only when the source's `project_id` is the bound one. The complete record is kept, including provenance. A forbidden-strings check still runs over the whole record, to catch another project's names pasted by mistake.
- **Outward sharing** (a public skill-promotion PR, the digest, a cross-project export). Only `process` lessons may leave, as in issue #293. The check covers every serialised field of the outgoing record, including nested ones such as a lesson's anchor; the fields that come to mind (text, key, trigger, evidence reference, owner, curator, tags, retirement reason) are examples, and the list is not exhaustive. Outward provenance is sanitised (neutral evidence label, role instead of agent name). The private provenance stays in the local store and is never exported.
- **Human clearance.** Outward output has two stages, and the clearance status never lives inside the publishable bytes. First the tool writes a **staging payload** to a local staging folder, with a **sidecar file** beside it holding the status "not cleared", the SHA-256 of the payload and, later, who cleared it and when. The payload itself carries no marker, so what the person reads is exactly what will be published, and a structured export stays valid. The checks run when it is staged. A named person reads every line of the payload and records "cleared" in the sidecar together with the payload's hash. Only then does the tool write the **final output** to the output folder: the payload bytes, unchanged, written only if the sidecar says cleared and the hash still matches. A payload edited after clearance is refused. No publishing command accepts a staging folder; the sidecar stays in staging. The tool never pushes. The pull request cites the clearance. A scope label or a forbidden-strings list does not count as clearance.

#### A.11.3 Promotion into skills, and how adoption is known

`install-skills` keeps a file that differs from the shipped one unless forced, and it cannot tell an old shipped copy from a personal edit. A machine can also receive a retirement without ever running `install-skills`, or skip a release. So adoption is checked on the machine, at the moment the retirement would be applied, not assumed from a release number.

- **The marker.** Each shipped skill file that carries promoted advice lists the lessons it covers in a short line of its own (`covers:` followed by `key@content_id` entries, using the first 16 characters of the `content_id`). A retirement made because of a promotion names the skill and the full `content_id` of the promoted text in `promoted_to`. The check compares both the key and the `content_id`, so a skill that carries other advice under the same key does not count.
- **Check at import.** When an import would retire a lesson that has `promoted_to`, it looks for the marker in the installed skill file for each agent tool installed on the machine. If every installed copy lists the key and the matching `content_id`, the retirement is applied, and the dependency (the skill names and `content_id`) is written to the import log. If any does not, the retirement waits: the lesson stays active, the import log says "retirement deferred: advice not installed", and `knowledge pull` prints "N lessons awaiting skill adoption: run install-skills". This is the single, logged exception to "retired beats accepted".
- **After the retirement is applied.** The skill can later be removed, replaced by an older release or edited. So the check is not once only: `knowledge pull`, `knowledge onboard` and `install-skills` re-run the cheap marker check for every retirement whose dependency is in the log. For each lesson whose marker has gone, they print a visible warning naming the lesson and the skill, and how to bring the lesson back (`import --reinstate`, a deliberate curator action). Nothing is reactivated automatically. Whether it should be automatic is an open decision (see "Open before work order 7"). Until it is closed, the lesson is still findable with `knowledge search --include-stale`.
- **A skipped release or a never-run installer** lands in the same place: no marker, so the lesson stays active and the message appears. No release number is consulted.
- **A kept local edit.** `install-skills` keeps a differing file by default, as today. If the kept file still lists the key and `content_id`, it counts as installed, but the check only proves the line is there; whether the advice itself survived the edit is **unverified**, and the report says so. If the kept file lacks the key, the lesson stays active. A curator who deliberately keeps a local skill may pass `--accept-local-skill` to apply the retirement anyway.
- **The fallback and how to find retired advice.** Ordinary search hides retired notes. A retired lesson is found with `knowledge search --include-stale` (and `knowledge pull --include-stale`). The skill text is the primary route; this flag is the manual route.
- **Upgrade policy.** A shipped manifest listing the hash of every released skill file lets `install-skills` overwrite a file that matches an older release (an untouched copy) and keep one that matches none. That changes today's default, so it is an operator decision (question 8). Until it is adopted, `install-skills` keeps the default of retaining every differing file and the warning below still applies.
- **Warning.** `install-skills` prints, for every kept file lacking a marker, the path and the keys it lacks, wording it as "not installed here", not as proof about edited content.

#### A.11.4 Promotion inputs and review

Inputs: `process` lessons ranked by the lesson-use report in 11.5 (how often agents cited them as used), how often they were shown, and how long they have held. The lead proposes a short list at release time and the operator approves it. The pull request edits the skill text, adds the `covers:` line and, in a later change, marks the lesson retired with `promoted_to`. The reviewer checks that the skill gets shorter or clearer. A human clears the text under 11.2 before the pull request is opened.


### A.12 Evidence gates, work orders and open questions of the parked design

#### Exercises (done before wider use)

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
| 16 | Confidentiality and clearance: a record whose key, trigger or retirement reason contains a forbidden string; then a clean record | the forbidden one is refused at staging; the clean one is staged as a payload with no marker, with a sidecar saying "not cleared"; after clearance the real final artifact is read back: it is the exact payload, parses as its format, contains no "NOT CLEARED" text and has the cleared hash; editing the payload after clearance is refused |
| 17 | Old skill, new retirement: a kept edited skill with no marker, then one with the marker | lesson stays active with the "run install-skills" message; with the marker it retires and the report says "unverified"; `--include-stale` finds the retired lesson |
| 18 | Skipped release and no installer: a machine goes from an old release straight past the promotion release | lesson stays active until the installed skill carries the marker |
| 19 | Override: switch off one lesson, then withdraw it | hidden here only; target named as (domain, key); no shipped skill text changes; the withdrawal is a new log line with its own identifier, the old line is not deleted, and replaying the old override does not bring it back |
| 20 | Retire, reinstate, retire again, with identical text | the first retirement (R1) is replayed and ignored after the reinstatement; the second (R2, new identifier) applies and the note ends retired |
| 21 | Lifecycle change (accepted to retired, retired to reinstated) | the retraction or the new publication and approval is appended; it is never treated as a reviewer-only update. An accepted file whose `reinstates` list names the local retirement is applied with `--reinstate` and refused without it; a retired file with a newer identifier over an already retired note appends nothing and is logged |
| 22 | Crash at every durable append: after the intent, after each event, after the last event but before the completion line | a rerun appends only the missing events and then the completion; an intent is never read as success; events alone are never read as complete; a concurrent curator change is seen and stops the rerun for that key |
| 23 | Fresh store, a source retired because of a promotion, no installed skill | the import reports "not loaded here: the advice is in a skill that is not installed; run install-skills, then import again" and appends nothing; it never says "retained" for advice that was never loaded. After the skill is installed, a rerun publishes the file's content and retracts it; with the skill still absent it again appends nothing |
| 24 | Windows-reserved names: keys `CON`, `aux.v2`, a key ending in `.`, a domain `nul` | each is written to a path that can be created on Windows, decodes back to the same identity, and a checkout does not fail |
| 25 | Lessons that differ only by a nested anchor | different `content_id`s; export and import keep the anchor; the whole-record check covers it |
| 26 | A path-anchored code note and a path-anchored lesson, with and without `verified_against_sha`, and a code note re-verified at a newer commit | a code note with a baseline is not stale unless its anchor changed; without one an accepted record is imported as proposed and listed, but a retired record is still carried out; the lesson with the same anchor and no baseline imports as accepted and stays active; the re-verified code note gets a verify event with the new baseline and no republish; a baseline never overrides a changed anchor |
| 27 | Supersession across domains: a lesson in domain A supersedes key `x`, domain B has its own `x` | B's `x` stays active; only A's `x` is superseded |
| 28 | Two retire-and-reinstate cycles, then a replay of the first retirement's file on a fresh bus | the current file lists both identifiers; the replay is ignored |
| 29 | A fresh bus meets an override withdrawal first, then the older override; then a new override of the same target | the old override stays withdrawn; the new one applies |
| 30 | A promotion retirement where the local lesson has a different `content_id` from the one the skill carries; and a retirement later corrected to add `promoted_to` | the first is a conflict, not a retraction; the second re-runs the check and is applied or deferred under the new metadata. A retirement already applied without a dependency, then corrected to add `promoted_to` with the skill absent: the retraction stands, the state "retired, awaiting its skill" is shown, recovery is a reinstatement file, and `--include-stale` finds the text |
| 31 | The installed skill is removed or downgraded after the retirement was applied | `pull`, `onboard` and `install-skills` print a warning naming the lesson and the skill; nothing is reactivated automatically; `--include-stale` still finds the lesson |
| 32 | The lesson-use report over replies that cite by event id, cite by key (one domain, two domains, a key later replaced), cite `none`, and carry no metadata, plus a pruned range, and the read-A, update-B, reply-by-key case | event-id and unambiguous key citations are credited to the right version; the ambiguous and undeterminable ones are unattributed and shown separately; explicit none and absent are different counts; adoption uses only attributed citations; the date range and the "sync is unmeasured" label are printed; a lesson read as version A, then accepted as version B, then cited by key only is credited to A when the actor's exposure shows A, and is unattributed when there is no exposure evidence; nothing is written and no query text appears |
| 33 | Readers and writers during a pending import: read after the intent, after each event and before the completion line; a reader whose two reads straddle a new intent; a concurrent curator publishing and approving Q on the same key; an ordinary curation command aimed at a key with a pending intent | ordinary readers see the previous accepted version throughout and the new one only after the completion line; the straddling reader starts over and never shows an approval without its intent; the curation command is refused while the intent is pending; after `import --resolve --abort` the old intent's ids appear in no reader or writer view, Q is still the active version, and a fresh intent against the current state can complete |
| 34 | A 128-character key made mostly of colons, and a long domain id | the file name is within 100 characters per component in the shortened form, the identity is read from the file, and a name that is not the derived one is refused; two different identities whose derived names collide are refused rather than overwritten; the whole-path length limit is reported separately from the component rule |
| 35 | A retired file imported as proposed (`--as-proposed`), then imported again from the trusted commit | the second import retracts it; the note never becomes active |
| 36 | A trusted retired file for a code note that has no baseline, over an accepted local version | the local accepted version is retracted; the import does not downgrade it to a proposal, and the report does not call a proposal a retirement |

#### Kill signals

We stop or reshape if, over 2 to 4 weeks, any of these happens: a project's policy is overwritten; retired advice returns; generic advice crowds out needed local guidance; raw project material leaves its repository; merge conflicts or duplicate keys keep recurring.

#### Two different measures

- **Adoption:** tracked or imported notes make up at least about 1 in 10 of the lessons agents report using. This shows the notes are used, not that they help.
- **Prevented-mistake indicators:** the pilot's displaced-needed-advice count stays at zero, and a review finding in a class a tracked note covers does not recur. These are **indicators, not proof**. For each, we record the **opportunities** (how many tasks could have hit that mistake) and whether the note was shown or looked up on them, beside the failures. Zero recurrences with no opportunities shows nothing. The operator picks which measure gates the work (question 4).

#### Work orders, in order

Sizes are engineer-days for one builder plus one review round, assuming the existing store and event code are reused and no new dependency is added. They are rough, plus or minus half. Work orders 4 and 7 were re-estimated after the decisions in section 9 and 11.3. This is a rough order of magnitude, not a delivery forecast; it should be re-estimated after the operator's answers, and the operator chooses the stage and spend to authorise.

| # | Work | Days | Authorisation |
|---|------|------|---------------|
| 1 | Lessons report, the selector rule and setting, domain-qualified supersession, and a re-publish-then-approve command for tag changes (section 4) | 5 | authorised at about 9 days for both (operator, 2026-10-07); pending reconfirmation of expanded scope |
| 2 | Exposure schema version 2: reader first (accepts versions 1 and 2), then manual-lookup writers one release later, with `--from` and the separate onboarding recipient (11.1), and the read-only lesson-use report with version-bound citation lines and exposure-evidence resolution (11.5) | 10 | authorised at about 9 days for both (operator, 2026-10-07); pending reconfirmation of expanded scope |
| 3 | Note file format with path encoding and bounded names, `content_id`, export of accepted and retired notes, staged outward output and whole-record confidentiality check (9.1, 9.2, 11.2) | 6 | to be re-estimated and authorised later |
| 4 | Import, in three parts: (a) read files, derive state, dry-run report, 5 days; (b) append sequences, intent-and-completion log with the quarantining reader filter, abort and the writer rules, a consistent reader snapshot, retirement, reinstatement lineage, 11 days; (c) bind command, remote freshness check, wrong-project checks, 4 days (9.3 to 9.6); the domain-creation decision may add work | 20 | to be re-estimated and authorised later |
| 5 | Override records (9.7) | 2 | to be re-estimated and authorised later |
| 6 | Resolve anchors against the bound checkout root (issue #245) | 4 | to be re-estimated and authorised later |
| 7 | Skill `covers:` markers bound to `content_id`, adoption check at import and re-check on `pull`, `onboard` and `install-skills`, deferred retirement, install warning, shipped manifest; Codex folder check per version (10, 11.3) | 7 | to be re-estimated and authorised later |
| 8 | The thirty-six exercises, with guard-removal runs, written up | 11 | to be re-estimated and authorised later |
| 9 | Promotion run, by hand, per release (11.4) | 1 per release | to be re-estimated and authorised later |
| 10 | Digest template and clearance checklist (11.2) | 1 | to be re-estimated and authorised later |

Items 1 to 8 total 65 days, which is the cost **before** the first promotion. Including the first promotion run (item 9) the total is 66 days, and with the digest (item 10) 67. If the recommended domain-definition option (about 2 days, open before work order 4) is adopted, add 2: 67 before the first promotion. The figure rose from 61 after the round 6 review: work order 4(b) grew by 2 days (abort, the writer rules, the reader snapshot), work order 2 by 1 day (version-bound citation lines and exposure-evidence resolution), and the exercises by 1 day (two more exercises and wider ones). Elapsed time is longer: several release boundaries and the 2 to 4 week observation period sit on top. Items 1 and 2 stand alone and can ship first. Items 3 and 4 are the core of B.

**Build acceptance gates** (they do not change the design; each must pass before its work order is accepted):

- **Work order 4(b), crash recovery.** The event file and the import log are two files, and no lock makes two files atomic. Before 4(b) is accepted, a recoverable intent-then-completion sequence is chosen and exercised under the store's shared lock, with an interruption after each durable append (exercise 22), concurrent curator changes, and reruns that reconstruct a missing completion without treating an intent as success. The existing event format does not change.
- **Work order 7, fresh store.** A fresh store, a source retired because of a promotion and no installed skill must end in the honest "not loaded here, manual recovery" result (exercise 23). The alternative, an authorised fallback publication and approval of the lesson, is not adopted: it would load advice the machine's skill does not carry.
- **Work order 3, canonical form.** The exact JSON canonicalisation and validation of format 1 are fixed before format 1 ships.

#### Open before work order N

These decisions are real but not settled by this document. **A work order cannot start until its open items are closed.** Work orders 1 and 2 have none.

**Open before work order 4: how a project's domains are created on a clean machine.** Today's `domain` command is read-only, and the binding stores only allowed domain ids, not the definitions or the curator lists. Options: (a) the repository tracks each domain's definition in `.agenttalk-knowledge/domains/<domain_id>.json`, and a curator imports it under the same trust rules as notes, mapping curator names to local agents at import; (b) add a `domain add` command and have the curator type the definition once per machine; (c) leave it to hand-editing `.agenttalk/domains.json`. *Recommend (a): the fresh-checkout promise (exercise 1) holds only if the definitions travel with the notes, and it needs no new command. It adds roughly 2 days to work order 4.*

**Open before work order 7: what happens when a marker disappears after a promotion retirement.** Options: (a) warn only, and a curator brings the lesson back deliberately; (b) bring the lesson back automatically at the next `pull` or `onboard`; (c) check again before every selection. *Recommend (a): an automatic change of what agents are shown, made from a file check, is the kind of silent behaviour this design avoids, and (c) adds a file read to every turn. The warning in 11.3 makes the loss visible, and the retired lesson stays findable.*

#### Questions for the operator

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

**Re-estimate of the authorised scope.** The operator authorised work orders 1 and 2 at about 9 days. After the round 6 review they stand at 15 days (5 and 10): domain-qualified supersession was added to work order 1, and the lesson-use report to work order 2, which after round 5 must also resolve each citation to a lesson version; all of it is needed for the selector and the adoption measure to be correct. This is about 6 days over what was authorised. The table labels both work orders "pending reconfirmation of expanded scope", and **neither starts until the operator confirms it.**


### A.13 Technical notes (for builders)

- Today's code: notes are events in `.agenttalk/knowledge/notes.jsonl` (`knowledge.py`), folded to a current view per (domain id, key). Lessons carry a scope, tags, status, review and expiry dates (both required), and `supersedes`. The curation-bound content is everything but the status and the curator, plus author, creation time and the replaced-event id; the causal check (`_curation_causal_problem`) requires a curation to reference the current prior same-key event and carry its payload hash. That is why a tag change is a new publication and why a retraction needs a prior event.
- A retract is terminal until a fresh publish and curation reopen the key (`resolve_views_with_problems`), which is why import must check retractions itself.
- Curation authority is enforced in `cmd_knowledge` in `cli.py`. A missing domain registry reads as empty (`domains.py`), and anchor checks resolve against the store root. The existing `--include-stale` flag on `knowledge search` and `pull` is what shows retracted notes.
- The selector is `select_lessons` and `rank_lessons` in `lesson_context.py`. The clause to change is the empty-tag allowance. `exposure_event_problem` accepts only schema version 1 and `surface == "wrapper_turn"` with turn identity, a prompt-block hash and one to five lessons; the reader returns a problem list rather than raising.
- `install_skills.py` compares file bytes (`filecmp`) and skips a differing file without `--force`. It already has a warning-only check for retired skills, which is the place for the missing-marker warning.
- `lesson_superseded_keys` builds one set of bare keys across all domains; work order 1 changes it to (domain, key) pairs. `compute_staleness` marks a path- or symbol-anchored code note with no `verified_against_sha` as hard-stale (`missing_verified_baseline`), which is why a code-note file carries the baseline; `compute_lesson_state` treats a lesson's anchor as provenance only, so lessons never need one. A code note re-verified at a new commit is a verify (curation) event carrying the new `verified_against_sha`, which the payload hash leaves out. Today's `new_curate_event` copies the base note's baseline (`knowledge.py` 523) and the CLI verification path has no baseline-update step, so the baseline update is new importer behaviour, not something the existing CLI already does. The store's causal check (`_curation_causal_problem`) requires a curation to name the current prior same-key event, which is why a stale import intent must be aborted and never completed over another curator's later result. The reply metadata `lessons_used` exists, but no code reads or counts it; work order 2 adds the report.
- The importer reuses `knowledge.event_problem` validation and the event builders, and adds `content_id` beside the existing payload hash rather than replacing it.
