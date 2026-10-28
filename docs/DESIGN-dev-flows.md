# Development flows: a spec-driven flow and a migration flow, enforced at agenttalk's own steps

Status: proposed. Written on 2026-10-27 (UTC) for work order tk-d00501b1ae5f, after the challenges ch-012b0a23 (the spec-driven core) and ch-11fc8afc (the wider family) both returned RESHAPE; the lead accepted both reshapes as binding. Fix round 1 (tk-e4d5d02f3288, 2026-10-28) answers the ten findings of the independent read tk-2eda8b4e6c31; a narrow fix round 2 (tk-be1996b22b8f, 2026-10-28) answers the final read tk-a5b0159dcf8b. Section 16 lists each finding and what changed. Nothing here is built. Where it describes agenttalk today, it matches master `544c6a1c` (after release 0.104.0). Owner: lead.

## In plain words: the design on one page

**What the flows are.** agenttalk will offer two built-in ways of working.
- **Spec-driven** is the default for new projects. Before a feature is built, someone writes a short spec: what and why, plus numbered acceptance criteria ("AC-1: ..."). Someone else reads it and approves it. Every task then names the requirements that apply to its kind of work: criteria where that kind of work requires them, none for, say, a documentation fix, a fix to old code with no spec, or the read that approves a spec. Every handover says, criterion by criterion, which check ran and whether it passed.
- **Migration** is for moving an existing system to new technology. Its spec also fixes the original version, the comparison cases to run on old and new, and the differences someone accepted.
- A project can turn its flow off with one command. The choice is recorded and visible. Projects that exist today keep working as now until someone switches a flow on.

**What agenttalk will refuse.** Only its own steps:
- sending a task that does not name its work and kind of work, that leaves out criteria its kind of work needs, or that cites an outdated spec or one nobody else has approved (the task asking for that approval is the exception);
- a handover that claims a criterion is covered without naming a check that ran, or that talks about a different version of the code;
- accepting work when a criterion has no passing check on the exact version being accepted, when the independent reader has not said GO on that version, when the evidence was produced for wording that has since changed in substance, or when a comparison shows a difference nobody accepted.

A builder can always hand over unfinished work, listing what is still open; only acceptance waits. A named person can excuse some refusals: each excuse is recorded and only moves the check to acceptance. A change to a criterion's wording makes its evidence stop counting, unless a recorded decision calls the change wording-only. Turning the flow off, losing its setting file, or agenttalk archiving old messages never erases what started work still owes.

**What it can never prove.** agenttalk cannot stop edits made with other tools, and cannot tell whether code really does what a criterion says: a test and a criterion with the same number are *linked*, not *proven*. That judgement stays with the tests and the independent reader, and every report says so.

**The trial.** Before anything is built, a half-day throwaway trial puts a small temporary check into a copy of agenttalk, at the points where tasks, handovers and acceptance are recorded. Two small features (one with a change of scope) and one made-up migration go through one correct path and about fifteen wrong ones, including a store whose old messages were archived. Each wrong step must be refused and leave nothing anyone could act on. The extra effort for the small feature must stay within four commands and fifteen minutes. If the first checks fail, the work stops.

**Questions for the owner** (section 17): when the default switches on; the effort limit; who may excuse which refusal; whether a wording-only change to a spec may keep existing evidence; and whether records from the existing acceptance pass count as evidence in the first build.

Audience: the owner and the lead, who decide; the Codex reader; the builders (sections 3 to 10 and 15). Mode: explanation (a design).

## Contents

1. [What and why: the spec for this feature](#1-what-and-why-the-spec-for-this-feature)
2. [What exists today, and what is reused](#2-what-exists-today-and-what-is-reused)
3. [The project's choice](#3-the-projects-choice)
4. [The spec record](#4-the-spec-record)
5. [Each work item carries its own requirements](#5-each-work-item-carries-its-own-requirements)
6. [Named refusals at existing steps](#6-named-refusals-at-existing-steps)
7. [Finished, accepted and released stay separate](#7-finished-accepted-and-released-stay-separate)
8. [The migration flow's own evidence](#8-the-migration-flows-own-evidence)
9. [Drift: what agenttalk can and cannot see](#9-drift-what-agenttalk-can-and-cannot-see)
10. [No second task-state machine](#10-no-second-task-state-machine)
11. [The trial: half a day, then thrown away](#11-the-trial-half-a-day-then-thrown-away)
12. [The rule for a third flow](#12-the-rule-for-a-third-flow)
13. [Build outline after the trial](#13-build-outline-after-the-trial)
14. [Kill signals](#14-kill-signals)
15. [Technical detail](#15-technical-detail)
16. [Read findings and what changed](#16-read-findings-and-what-changed)
17. [Open questions](#17-open-questions)

## 1. What and why: the spec for this feature

This section is written the way the flow asks every feature to be written; the rest of the document cites its numbers.

**What.** agenttalk ships exactly two development flows, `spec-driven` and `migration`. A new project starts with `spec-driven` unless its creator turns it off. Where a flow is used, every task names its kind of work and the requirements that apply to it, and agenttalk refuses its own transitions when they are not met.

**Why.** The owner asked for "an actual agenttalk default flow for new projects ... something that we can enforce that's it is used and there's no drift from it if it's used", and then for a migration flow. In the field, a team building a new service had no what-and-why spec, no acceptance criteria per work package and no trace from requirement to test, and a scope change went from an outside email straight into design edits. The definition-of-done skill says of its own record: "Nothing in the bus enforces it today."

**Limits.** agenttalk cannot control edits made outside it: agents edit files with their own tools. It cannot prove that code does what a criterion says: a test and a criterion that share a number are *linked*, not *proven*. It can refuse its own steps and report the drift it observes.

| ID | Acceptance criterion |
|---|---|
| AC-1 | **Default on, visible opt-out.** A project set up with this release records `spec-driven` unless its creator opts out. Setup names the flow and the command that turns it off; turning it off is recorded with who, when and why, and stays visible. |
| AC-2 | **Existing projects unchanged until they opt in.** Opting in is a recorded activation; work already running is listed as pre-flow, never silently bound or skipped. |
| AC-3 | **Exactly two flows.** The only values are `spec-driven`, `migration` and `none`. A third needs section 12. |
| AC-4 | **A versioned spec.** Each version is recorded once, never rewritten, with an exact-content fingerprint per criterion, case and accepted difference. Changed content under an unchanged number is refused without a recorded decision. |
| AC-5 | **Read before work.** Work citing a spec version cannot be sent until someone other than its author has recorded a GO read of it. The task that asks for that read is the one exemption. |
| AC-6 | **Requirements travel with the work item.** Every task names its work item and profile, and where needed the spec version and criterion numbers. These obligations hold through the done reply, replacements, a flow switch and a lost setting file, until a GO close accepts the work item or the exception authority reduces them. |
| AC-7 | **Named refusals on every path.** One check in the store's single write path covers `task`, `broadcast` (fresh and resumed), `reply` and the wrapper's draft replies; `close` evaluates acceptance. Each refusal has a stable code, a message saying what to do and a named authority, and a refused step leaves nothing anyone can act on. |
| AC-8 | **A change makes affected evidence stale.** Evidence for changed criteria, cases or accepted differences stops counting; evidence for unchanged ones still counts. Section 4 gives the recovery path. |
| AC-9 | **Switching never drops obligations.** Opting out or switching keeps each open work item's requirements until it is accepted or the exception authority releases them by a recorded decision. |
| AC-10 | **Unmapped is not an exit.** A project that was never activated keeps today's behaviour. A project that was activated never falls back to zero requirements: a missing setting file holds its steps, a close naming no work is held while a flow is on, bound work stays owed after a switch to `none`, and a task with no known profile is refused. |
| AC-11 | **Finished, accepted and released stay separate.** A builder can always hand over with open or failed items listed; the independent read is the next owner's work; acceptance needs it; release keeps its own rules. |
| AC-12 | **Migration evidence.** A migration spec fixes the original, declares comparison cases before any run and lists accepted differences; closes need results for the named candidate, and a whole migration needs combined-artifact and rollback evidence. No claim of equivalence. |
| AC-13 | **Drift is reported**, with a plain statement of what agenttalk cannot see. |
| AC-14 | **Restart-safe and honest.** Every refusal and report is recomputed from files, with the same answer after a restart. Nothing calls a linked criterion met, proven or correct. |
| AC-15 | **Trial first.** Section 11 passes before any later build order starts. |
| AC-16 | **No second task-state machine** (section 10). |
| AC-17 | **Only qualifying evidence counts.** For each criterion: a passing executed check on the exact revision the close accepts, for the exact criterion text, and a GO read of that revision by a qualifying reader. A failing check, or a FIX or HOLD read, can be handed over but holds acceptance. |
| AC-18 | **The policy text resolves.** The pinned definition-of-done text is kept with the pin; a pin that cannot be resolved refuses dispatch and holds acceptance, and never falls back to whatever is installed. |
| AC-19 | **Survives agenttalk's own compaction.** Activation, bound but unaccepted work, accepted decisions and the evidence they need are read from the live messages and the compaction archive together, so compacting the store changes no refusal, hold or report; missing history is reported, never guessed. |

Non-goals: a planner, a task registry, generated code or tests, claims that the method improves outcomes (that stays with #153's blind comparison), and any use of the external spec tool removed on 2026-09-28.

## 2. What exists today, and what is reused

- **The definition-of-done skill** (`src/agenttalk/skills/devkit/definition-of-done/`) gives every task a five-line done record and eleven profiles: `review-only`, `documentation`, `investigation`, `qa-test`, `bug-fix`, `refactor`, `ci-fix`, `feature`, `project-or-release`, `migration-slice`, `migration`. It adds a risk word, exception rules (not applicable only at assignment, scope reductions by an exception authority, never-waivable rows) and a policy pin by git tree id that must never fall back silently to installed text. It keeps finished, accepted and releasable apart, and puts the independent read after the build closes. *Reused:* profiles are the per-task binding (section 5), exceptions are the waiver rules (6), the split is section 7.
- **Its migration reference** defines levels A (a slice), B (the whole migration) and C (live, never claimed) and the roles original, candidate and derived; **its project overlay** names a maintainer and an exception authority, pinned by blob id. *Reused:* section 8, and who decides and who waives.
- **The single write path.** Every message is written by `Store.send` in `store.py`, after `work_tags.normalize`: `agenttalk task`, both a fresh `broadcast --kind task` and its `--resume` (which rebuilds the missing copies from the batch's frozen meta), `agenttalk reply`, and the wrapper's draft-file replies (through `reply_transport` and `Store.send_operation`). Generic `send` refuses kind `task`. *Reused:* the place for one shared check (6.0).
- **`lane`** is an advisory assignment to path prefixes; `lane deliver` answers HOLD (for example `out_of_bounds_path`) and writes no delivery record. A lane has no work-item link, only free-text notes. *Reused unchanged;* a task may name its lane.
- **`gate`** keeps named checks; a blocker turns green only from CI; `gate waive` records operator, reason, scope and expiry. *Reused:* permission to release (7).
- **`close`** is opened on an exact revision with a `--scope`; `.agenttalk/dod.json` maps scopes to the dimensions `assurance`, `coverage`, `knowledge` and `acceptance`. A missing file or an unmapped scope means zero requirements, on purpose (`derive_required_dod` in `close.py`). But a close that carries a frozen acceptance route is evaluated even after the live policy is removed (`evaluate_dod`, and `_build_dod_eval` in `cli.py`). *Reused:* one more dimension, `flow`, frozen the same way (6.3).
- **The acceptance pass** (`acceptance.py`; schemas 1-4 in `acceptance_schema.py`) has frozen plan rows, a final cold review, successors with an amendment reason, and obligations that "follow the change". *Reused:* the model for revisions and surviving obligations. It stays independently enforced; the flow is added beside it, never in place of it.
- **The work board** is a pure projection of messages tagged `work_item` and `stage` (`design`, `build`, `read`, `fix`, `delta`, `sweep`), with typed verdicts and no stored task state. *Reused:* it carries all work state (10).
- **`onboarding`** keeps an append-only ledger of segments, claims, drift and unknowns; **`comprehension`** builds an offline static inventory of a legacy repository. *Reused:* inputs to a migration spec, never comparison evidence.
- **#153** proposes a migration stage model (contract, move, improve, UI, infra), six programme documents, a cold-sweep protocol, and a lean migration skill that ships only if a blind comparison shows it helps; the migration playbook and lesson pack shipped in 0.103.0. *Reused:* section 8 maps #153 onto the flow; #153 keeps its method.

**The replay of the definition of done** (`docs/research/DOD-REPLAY-2026-10.md`) found 61 late findings in six finished jobs: 10 became unambiguous checks (7 were already rules in the review skills), 17 needed judgement, and 34 were not caught; the largest class, logic that depends on the order of events, needs an independent read. No time was measured. So the flows can move forced checks to the handoff and make missing or stale evidence visible; they cannot promise fewer defects, and their cost must be measured.

**Missing today:** a spec record (AC-4); a recorded project choice (AC-1, AC-2); any check of profile, criteria or pin when work moves, since `agenttalk task` checks only the sender's role, the body, the work tags and the recipient's version, and no code checks challenge meta (AC-6, AC-7); a rule for which evidence counts, and its staleness (AC-8, AC-17); the unmapped-scope boundary (AC-10); a checked record of original, candidate and cases (AC-12); a drift report (AC-13).

## 3. The project's choice

*Serves AC-1, AC-2, AC-3, AC-9, AC-10, AC-18.*

**Where.** In `.agenttalk/flow.json`, beside `dod.json` and `signoffs.json`, read the same way: a malformed file fails closed. Only `agenttalk flow set` changes it: `flow.json` is the one changeable record, the current setting. Everything that explains how it got there is written once (section 10). Each change also sends the team a **flow note**, an ordinary bus note carrying the new setting, the policy pin, who changed it and why; the file records the note's id.

**Never activated, or missing after activation.** The flow notes are the durable history of the setting. The live bus does not keep every message: compaction moves old ones, flow notes and finished tasks included, into the archive (`archived/compacted/`). So the flow reads its history from the live messages and the archive together (section 10.1):
- **never activated**: no flow note in that history and no `flow.json`. The project behaves exactly as today;
- **history missing**: `flow.json` names a flow note (`note_id`) that is in neither place. That is an unresolved policy too (`flow_policy_unresolved`), reported as missing history;
- **activated**: at least one flow note exists in that history. From then on a missing, malformed or disagreeing `flow.json` is an unresolved policy: sending tasks is refused and closes are held (`flow_policy_unresolved`), never treated as "no flow".

**The policy text is kept with the pin.** `flow set` copies the definition-of-done text it pins (the skill's `SKILL.md` and `references/`, the shared `evidence.md`, and the project overlay if there is one) into the store, once, under the pin's own identity, computed the way git computes it. Every later check resolves the pin against that copy and recomputes the identity. If the copy is missing or does not match, the pin is unresolved (`flow_policy_unresolved`). The recovery is to restore the copy from a store backup, or to record a new pin with `flow set --repin --reason "<why>"`. Work bound to the old pin keeps it, and is held at acceptance until its text resolves again or the exception authority records "policy text not verified" as a scope reduction. That last step is the definition-of-done skill's own rule.

**A new project.** `agenttalk init` gains `--flow`, default `spec-driven`, and says so (proposed, not runnable yet):

> Development flow: spec-driven (the default). To turn it off: `agenttalk flow set none --reason "<why>"`.

`agenttalk init --flow none --reason "<why>"` opts out at the start. `agenttalk flow show` prints the setting, its history, the pinned text and the open work bound to it; the work board shows the flow in its header.

**An existing project** has no flow note and no `flow.json`, and keeps today's behaviour, which `flow show` says in those words. The lead opts in with `agenttalk flow set spec-driven --reason "<why>"`. This **activation** records the work items open on the board at that moment as *pre-flow*; they finish under the old rules and are labelled so in every report. To bring one under the flow, the lead rescinds its task and sends a bound replacement for the same work item (the existing `supersedes` tag).

**Switching.** Obligations are derived from the work items' own bindings (section 5), not from the current setting. A switch to `migration` or `none`, or a lost file, therefore cannot remove them. `flow set` prints each open work item and what it still owes. New tasks follow the new setting.

**One flow, many kinds of work.** The flow sets the project's main kind of spec and default profile. A migration project still sends bug fixes and documentation tasks, each with its own profile.

## 4. The spec record

*Serves AC-4, AC-5, AC-8, AC-14.*

A spec is a short Markdown file in the project's repository: what and why, non-goals, numbered criteria. agenttalk reads only a few fixed parts. Example (proposed, not runnable yet):

```text
spec: export-csv
kind: feature
why: People want to open their to-do list in a spreadsheet.
non-goals: importing CSV files.
AC-1: `todo export --csv FILE` writes one row per item, after a header row.
AC-2: Text containing a comma or a quote reads back unchanged in a spreadsheet.
AC-3: If FILE already exists, the command stops with an error and leaves it unchanged.
```

**Versions and exact fingerprints.** `agenttalk spec record --file specs/export-csv.md` stores version 1 in the bus store, never rewritten. Each criterion is one line, and its fingerprint covers the exact bytes of its text, with no normalization of spaces, letters or Unicode. Two criteria that differ only inside a literal, such as `"a  b"` and `"a b"`, therefore differ. Tasks and evidence cite `export-csv@1`, and `agenttalk spec show export-csv@1` prints exactly that text, so builders read the recorded text, not whatever the file says today.

**Changed content under the same number is caught.** For each new version agenttalk compares fingerprints with the previous one. A changed text, or an added or withdrawn criterion, is a *change*. It is recorded only with `--decision <request id>`: an accepted proposal (the existing `proposal` / `proposal-response` pair) from the spec's decision owner that lists each change as `substantive` or `editorial`. If that list differs from the changes agenttalk found, it refuses with `spec_change_unrecorded`. A withdrawn number is never reused (`spec_id_reused`). Rewrapping a line or fixing a typo is a change too, and is recorded as `editorial`.

**Two kinds of read.** A **spec read** approves a spec version. It is a `read`-stage task with `read_of=spec` that cites the version, and its reply carries the verdict GO or FIX. A **work read** checks the delivered code: `read_of=work`, at an exact revision (section 6.3). A spec read never counts as evidence for a criterion. The spec itself is written in the work item's `design` stage.

**The first read.** The spec-read task is the one task exempt from "read before work" for the version it reads; it would otherwise be refused by its own prerequisite. Everything else about it is still checked: the version exists and is the latest, the task is bound to a work item, and its reader is not the version's author (`flow_spec_reader_is_author`).

**Staleness.** Evidence is tied to the fingerprint of the exact text it was produced for. After a revision, evidence for unchanged text still counts and evidence for a `substantive` change is stale. Evidence for an `editorial` change is carried forward and shown as "carried by decision `<id>`". Whether a change is editorial is a human judgement that agenttalk records but cannot check. Chains are followed one link at a time: evidence carried by an editorial version 2 is still made stale by a substantive version 3.

**Recovery.** (1) The decision owner accepts the proposal and the author records the new version with `--decision`. (2) `agenttalk flow status` lists the stale evidence per work item and the open tasks citing the old version; those tasks can still hand over. (3) The lead sends re-check tasks citing the new version, for changed and added criteria only, or the exception authority records a scope reduction, which shows as reduced scope, never as met. Old versions stay readable, so a task sent under version 1 can always be read against the text it was given.

## 5. Each work item carries its own requirements

*Serves AC-6, AC-9, AC-10, AC-11, AC-18.*

**The binding.** A task in a flow project names its **work item** and **stage** (both exist), its **profile** and **risk** (`normal` or `high`), and, where the profile needs them, the **spec version** and **criterion numbers**. For migration work it also names the **comparison cases**. It may name its **lane** and a **waiver**, and a read names `read_of`. agenttalk stamps the project's policy pin on every bound task, as it already stamps the epoch. A sent message is never edited, so later settings cannot change its binding.

**Durable obligations.** A work item is *bound* once any task for it carries a binding. Its obligations are the union of the bindings of all its tasks, including replacements. A replacement can add criteria or move to a newer spec version, but it cannot drop a criterion: only a recorded spec decision (a withdrawn criterion) or a scope reduction does that. The obligations hold through the done reply (finished is not accepted), a switch to `migration` or `none`, and a deleted or changed `flow.json`. They end only when a GO close names the work item and clears them (6.3), or when the exception authority reduces them. Compaction does not end them either: a finished task's messages may move to the archive, and the obligations are still derived from them there (section 10.1).

**What is checked by agenttalk and what by the reader.** agenttalk checks only what it can check from records. Every other row of the pinned profile is judged by the independent reader, who reads it from the pinned text (`flow show --policy <pin>`).

| Profile | Checked by agenttalk at acceptance | Judged by the reader (pinned text) |
|---|---|---|
| `feature`, `project-or-release` | each bound criterion: passing evidence and a GO work read, both at the close's revision (6.3) | every other profile row, for example failure paths and the changelog |
| `migration-slice` | as `feature`, plus a result for each bound case (8) | rows A2 to A11 of the migration reference |
| `migration` | as `migration-slice`, plus combined-artifact and rollback records (8) | rows B1 to B12 |
| `bug-fix`, `refactor`, `ci-fix`, `documentation` | a done delivery at the close's revision; a GO work read when risk is `high`; any criteria cited are checked as for `feature` | every profile row, for example a failing-first proof or the same tests before and after |
| `review-only`, `qa-test`, `investigation` | nothing: the finished read is the result | - |
| any other kind | the closest profile, with the reason (the skill's own rule); an unknown name is refused | - |

A requirement applies only where its evidence can exist: a documentation task is never refused for lacking criteria, and a fix to old code with no spec need not invent one.

## 6. Named refusals at existing steps

*Serves AC-7, AC-8, AC-10, AC-11, AC-12, AC-17.*

Every message reads `agenttalk <command>: refused (<code>): <message>`, with stable codes that reports and tests can count. A **waiver** is an accepted proposal from the named authority, naming the code, the task or close, a reason and an expiry. A waiver at dispatch only *defers* a check to acceptance; it never removes it. The project overlay names the **exception authority** and the **decision owner**; until it does, the lead is both and the operator decides when the lead is the doer. Identities are cooperative roster names, so reports count every waiver.

### 6.0 Where the checks run

- **One check, inside the write path.** Every task copy and every reply is written by `Store.send` (section 2). The flow check is one function called there, after `work_tags.normalize`: the dispatch rules (6.1) for kind `task`, and the delivery rules (6.2) for `task-response` with `status=done`. No caller can skip it, because it does not live in a caller.
- **No gap between check and write.** The check is a pure function of the message (its binding and its recipient) and the store's state. It runs again under the existing message-publication lock, just before the copy becomes visible. `flow set` and `spec record` make their changes visible through that same lock, with their notes, so a check cannot pass against a setting that changes in between.
- **Broadcast.** All copies of a batch carry the batch's frozen meta, so they share one binding by construction. Before writing the first copy, `broadcast` evaluates the check for every planned recipient. Any refusal writes no copy, and the message names the recipient when the rule depends on one (a spec read sent to the spec's author). What holds when the policy changes between copies is binding requirement F5 (section 16).
- **Resume.** `broadcast --resume` checks every missing copy against the current state before writing any. If the setting or the spec changed since the first copies, the resume is refused (for example with `flow_spec_not_current`), writes nothing, and the batch stays visibly partial. The lead rescinds the delivered copies and sends a new batch. Copies already delivered keep their binding, like any task sent before a change.
- **Close.** `close open`, `close check` and `close publish` evaluate the `flow` dimension (6.3). A refused reply is not written: the CLI prints the refusal, and a draft reply goes to the wrapper's existing refused-reply handling.

### 6.1 Dispatch (`agenttalk task`, `agenttalk broadcast --kind task`, and its `--resume`)

- **`flow_policy_unresolved`**: the project was activated and `flow.json` is missing, malformed or differs from the last flow note, or its pin does not resolve (section 3). "This project's flow setting or its pinned policy text cannot be resolved. Run `agenttalk flow show`." Never waived.
- **`flow_work_unbound`**: no work item, no profile, or an unknown profile. "This project uses the `<flow>` flow: add `--work-item <id> --meta profile=<kind of work>`." Never waived: an unbound task would leave no obligation anyone could find, and the fix costs two options.
- **`flow_spec_missing`**: the profile needs criteria and none are cited. "A `<profile>` task needs `--meta spec=<id>@<version> --meta criteria=<numbers>`." Waiver: the lead. The work item then owes its criteria at acceptance (`flow_obligation_open`).
- **`flow_spec_not_current`**: the cited version is not the latest. "`<id>@<n>` was replaced by version `<m>`. Cite `<id>@<m>`." Waiver: the decision owner. At acceptance the evidence is judged against the current text.
- **`flow_spec_not_read`**: no one but its author has a GO read of that version. This does not apply to the spec-read task itself. "Nobody but its author has approved `<id>@<n>`. Send a `read` task with `--meta read_of=spec` first." Waiver: the exception authority. At acceptance the work item is held by `flow_spec_unread` until the read exists.
- **`flow_spec_reader_is_author`**: a spec read is addressed to the version's author. "`<seat>` wrote `<id>@<n>` and cannot approve it. Send the read to another seat." Never waived.
- **`flow_criterion_unknown`**: a number is not in that version, or was withdrawn. "`<number>` is not a criterion of `<id>@<n>`. See `agenttalk spec show <id>@<n>`." Never waived.
- **`flow_original_undeclared`**: a migration task cites a spec with no fixed original. "The migration spec `<id>@<n>` names no original. Record a version that fixes it." Never waived.

### 6.2 Delivery (`agenttalk reply` and draft replies with `status=done`, on a bound task)

Delivery refuses only a handoff that contradicts itself. Anything unfinished, or failed, can always be handed over, so a builder is never blocked by someone else's work. Each bound criterion gets one evidence line: `criterion <number>: executed <check> -> pass <details>`, `... -> fail <details>`, or `criterion <number>: open - <reason>`. A failing line is a valid handoff; it never counts at acceptance.

- **`flow_criteria_unaccounted`**: a bound criterion has no line. "The handoff says nothing about `<number>`. Add `criterion <number>: executed <check> -> pass|fail <details>` or `criterion <number>: open - <reason>`." No waiver needed.
- **`flow_proof_missing`**: a line claims coverage without the executed form, for example only a test that was read. "`<number>` is reported covered but names no executed check. Name the command or run id and its outcome, or list it as open." No waiver needed.
- **`flow_artifact_mismatch`**: one of three things is wrong. A build or fix handoff names no full `head_ref`, or names a candidate other than that `head_ref`. Or its original is not the spec's original, or a derived build names no candidate. Or a read's `reviewed_ref` is not the revision its task asked it to read (`work_head`). "Evidence names `<found>` as the `<role>`; this handoff's `<role>` is `<expected>`. Evidence about another build does not count." Never waived: the exact artifact never is.

Only reported at delivery: a missing independent read (the next owner's work), and a spec that changed during the task ("The spec moved to version `<m>`; your evidence for `<numbers>` will not count until re-checked."). `lane deliver` keeps its own HOLDs.

### 6.3 Acceptance (`close open`, `close check`, `close publish`)

**The frozen flow route.** In a project that was ever activated, `close open --work-item <id>` (repeatable, proposed) freezes a *flow route* into the close record. The route holds the work items, the request ids of their bound tasks, their spec versions and their policy pins. `close check` and `close publish` evaluate the `flow` dimension whenever a record carries a flow route, even if `flow.json` is later deleted or set to `none`. This is the pattern a frozen acceptance route already follows. How a route opened before a later revision is reconciled is binding requirement F2 (section 16). `close publish` stores which evidence messages counted, so a GO can always be traced. The `flow` dimension is added beside the existing dimensions and any acceptance route; it never replaces them.

**Which evidence counts.** For a close accepting revision R, criterion X of a bound work item W counts as covered only when both of the following exist:
1. **A passing check.** A done reply on a bound `build` or `fix` task of W has `head_ref` equal to R exactly, and carries `criterion X: executed <check> -> pass <details>`. Its task's spec pin gives X the same fingerprint as the version frozen in the route, directly or through recorded editorial decisions.
2. **A GO work read.** A `read`, `delta` or `sweep` task of W with `read_of=work` has `work_head` equal to R, and its reply carries the verdict GO and `criterion X: checked <how>`. It comes from a seat that delivered no build or fix for W, and, at `risk: high`, from a vendor that differs from every builder's vendor in the roster. The latest work-read verdict at R must be GO. A FIX or HOLD at R holds acceptance until a later GO read at R, or a scope reduction that names its findings. A FIX at an earlier revision is answered by the fix round and a GO read at the new revision.

| Code | Held when | Message | Waiver |
|---|---|---|---|
| `flow_policy_unresolved` | the project was activated and its setting or a route pin does not resolve | "This close's flow policy cannot be resolved. Run `agenttalk flow show`." | the setting: never (restore it with `flow set`); a route pin whose text cannot be resolved: a "policy text not verified" reduction |
| `flow_scope_unmapped` | the current flow is `spec-driven` or `migration` and the close names no work item | "This close accepts nothing the flow can check. Name the work with `--work-item <id>`." | never |
| `flow_criterion_uncovered` | no passing check counts for X; the message says what was found instead (a failing check, another revision, none) | "`<number>` has no passing check on `<R>`: `<what was found>`." | reduction |
| `flow_evidence_stale` | the only passing check was produced for text that has changed | "Evidence for `<number>` predates its change in version `<n>`. Re-check it." | reduction |
| `flow_read_missing` | no qualifying GO work read names X at R | "No reader other than the builder has checked `<number>` on `<R>`." | reduction, never by the doer |
| `flow_read_not_go` | the latest work read at R returned FIX or HOLD | "The latest read of `<R>` returned `<verdict>`. Fix and re-read, or record a reduction for its findings." | reduction, never by the doer |
| `flow_spec_unread` | the work was sent under a waiver, and its spec version still has no GO spec read | "`<id>@<n>` was never approved by a reader." | reduction |
| `flow_obligation_open` | a bound work item owes something else: criteria waived at dispatch, a profile's checked items (section 5) or, for a `release` or `milestone` close, a bound work item whose delivered head is an ancestor of R and that no GO close has accepted | "`<work item>` still owes `<what>`." | reduction |
| `flow_decision_unrecorded` | a stored spec version changed without a decision (only possible by editing files outside agenttalk) | "Version `<n>` of `<id>` changed `<numbers>` without a recorded decision." | never |

A reduction is recorded by the exception authority, who is never the doer. It shows as "reduced scope: `<what>`", never as met. Section 8 adds the four migration codes. In a project that switched to `none`, a close may name no work item. But any bound work item it does not name stays "not accepted" in `flow status`, and a later `release` close that contains its delivery holds.

## 7. Finished, accepted and released stay separate

*Serves AC-11.*

| Question | Who answers | Where | What the flow adds |
|---|---|---|---|
| Is the task finished? | the builder | the done reply | refuses only self-contradicting handoffs (6.2) |
| Is the product accepted? | the lead, from reviews and evidence | `close check`, `close publish` | the `flow` dimension (6.3) |
| May it be released? | the lead, on evidence for the revision | `gate`, `review-release-readiness` | nothing |

The builder's handoff lists the independent read as open, with the reader as next owner; the lead then sends the read as a new task. That is today's review cycle, kept: no extra review round, no second report.

## 8. The migration flow's own evidence

*Serves AC-12, AC-8.*

A migration spec is the same record with three more parts. Each item is one line with an exact-content fingerprint, like a criterion, so changing it under the same number needs a recorded decision. A long case can name a file in the repository by path and blob id inside its line, so changing the file changes the fingerprint.
- **The original**: repository, full revision and artifact fingerprint, fixed for the whole migration. Changing it makes every comparison result stale.
- **Comparison cases** `C-1`, `C-2`, ...: what is sent, what is compared, and the clean-ups allowed first (timestamps, generated ids).
- **Accepted differences** `D-1`, `D-2`, ...: old behaviour, new behaviour, who accepted it and why.

Each result names its role: **original**, **candidate** (the handoff's `head_ref`) or **derived** (built from a named candidate). A result line reads `case C-3: original <rev> candidate <rev> -> same`, `-> differs (D-2)` or `-> differs (not accepted)`. A result is tied to the fingerprint of the case, and of the difference it cites, in its task's spec version. A substantive change to either makes the result stale (`flow_evidence_stale`); an editorial change is carried only by a recorded decision. "Declared before the run" is judged against the version in which that exact case content first appeared, not the first appearance of its number. The run time is the time the result line claims, bounded by the reply's publication time; agenttalk cannot check the claim itself. agenttalk checks presence, identity and order only, and holds a close with:

- **`flow_case_declared_late`**: a result is older than the version that introduced its case's content. "Case `<C-n>` as recorded in version `<v>` is newer than this result. Re-run it on `<candidate>`." Reduction: the exception authority may drop the case from scope; a late result never counts.
- **`flow_difference_unaccepted`**: a result reports a difference that no accepted `D-` number covers. "Case `<C-n>` differs from the original, and no accepted difference covers it. Fix the candidate, or ask the decision owner to accept a difference in a new spec version." Reduction: the case may be dropped from scope. Accepting the difference is a spec revision, not a waiver.
- **`flow_combined_evidence_missing`**: a whole-migration close has no fingerprint of the combined final artifact, or no results on it. "This close has no results on the combined artifact `<fingerprint>`. Run the suites and the declared cases on it." Reduction: named suites or cases may be dropped; the combined run itself and its fingerprint never.
- **`flow_rollback_evidence_missing`**: a whole-migration close has no rollback record (the boundary tested, the restore point, the measured recovery). "This close has no rollback record. Record the boundary you tested, the restore point and the measured recovery time." Reduction: the exception authority may narrow the stated boundary, naming who accepts the risk and until when.

The report says "`<n>` cases compared on candidate `<x>` against original `<y>`: `<k>` same, `<j>` accepted differences, `<u>` not accepted", never "equivalent". A `comprehension` inventory may suggest cases; it never counts as a result.

**How this consolidates #153.** #153 keeps its method; the flow checks its records.

| #153 element | Here |
|---|---|
| stage model | each `migration-slice` task names one stage, so stages cannot mix |
| GUARDRAILS | the project overlay |
| BASELINE register | accepted differences, and the spec's list of tests already failing on the original |
| DECISIONS register | accepted proposals on the bus, listed by `flow status` |
| SLICE plan, SLICE close | the slice task with its cases; the close with the `flow` dimension |
| stack assessment | `onboarding`, `comprehension` and an `investigation` task |
| cold sweep | the independent work read; the acceptance pass's cold review where used |
| the lean skill and its blind comparison | unchanged and owned by #153; no claim that the method helps |

## 9. Drift: what agenttalk can and cannot see

*Serves AC-13, AC-14.*

`agenttalk flow status` is a read-only report, recomputed from files each time. It reports:
- evidence that is stale against the current spec version;
- unlinked work, and every waiver and reduction used;
- a spec file that differs from its latest recorded version, per criterion, case and difference;
- for each open bound task that names a lane, the lane's verdict now. This is a fresh, read-only evaluation using the same Git reads `lane check` already makes. Past refused `lane deliver` attempts are not reported: `lane deliver` writes nothing on HOLD, and the flow keeps lanes unchanged. After a restart the report shows the present state, not history;
- spec or flow changes without a decision;
- uncovered criteria, open obligations, bound work items not yet accepted, pre-flow items and expiring waivers.

Its footer says what it cannot see: edits with other tools that never reach a delivery; whether code does what a criterion says; whether an `editorial` change is editorial; whether a test exercises the criterion it cites; whether two roster names are two people or vendors; a scope change agreed by email and never recorded; refused lane deliveries of the past; what was deleted from the archive by hand (it sees only that a named record is missing).

## 10. No second task-state machine

*Serves AC-16.*

| Concept | Carried by (exists today) |
|---|---|
| a work item's progress | `task` / `task-response` messages tagged `work_item` and `stage`, projected by the board |
| the binding and its obligations | the tasks' own meta, never edited |
| spec and work reads | `read`-stage tasks and their verdicts |
| delivery and evidence | the done reply's evidence lines |
| decisions, waivers, flow notes | `proposal` / `proposal-response` messages and notes |
| acceptance | the close record, with one more dimension and its frozen route |
| release | gates and `review-release-readiness` |

The new records are of two kinds. **The current setting**, `flow.json`, is changeable: `flow set` rewrites it, like `dod.json`. **The history** is written once and never changed: the flow notes on the bus, the stored spec versions and the retained policy text, like the acceptance pass's retained plans. None holds task state; everything else is computed from the bus and its archive, so neither a restart nor a compaction can change a result.

### 10.1 History that survives compaction

*Serves AC-6, AC-9, AC-10, AC-14, AC-19.*

agenttalk's own compaction (`agenttalk compact`, run by hand, or automatically when that is enabled) moves old messages out of the live message folder into `archived/compacted/`. It moves only messages that pass the full delivery check, and it never deletes them. A flow note has no open thread to keep it live. A task's messages can become eligible once its thread closes, which happens at the done reply, long before a close accepts the product. Read from the live folder alone, a compacted store would lose its activation, its bound but unaccepted work, its accepted decisions and the evidence they need.

**The choice: read the retained history, do not widen retention.** All flow derivation (activation, bindings and obligations, spec reads, decisions, waivers, evidence) reads the live messages and the compaction archive together. Both pass the same validation gate (`envelope_snapshot.validated_active`: configuration, roster and signing trust), and the result is the same union that `board verify-merges` already reduces (`work_board_facts`). The other route, protecting these messages in `_compute_keep_floor`, is rejected. Retention there is one cut-off point for the whole log, and everything older than it is archived. The relay rule can afford to hold that point while a hand-off is open, because hand-offs are short. The activation note is needed for as long as the flow is on, so protecting it would hold the point at activation and stop compaction for everything after it. Reading the archive needs no new store and no second task registry: it reads the bus's own records where compaction put them.

**Rules that make the read sound.**
- The live folder is read first, then the archive, and the two are joined by message id. A message that a compaction moves during the read is therefore seen in one place or the other, never missed.
- Completion and acceptance stay apart here too: compaction follows the finished task, obligations follow acceptance. A GO close records the ids of the evidence that counted, and those ids resolve in either place.
- Missing history is reported, never guessed. If `flow.json` names a flow note that neither place holds, or a close route names a task that neither place holds, the history is incomplete: dispatch is refused and the close is held with `flow_policy_unresolved` ("the flow history this step needs is in neither the live bus nor its archive").
- The cost grows with the archive, and B1 and B3 measure it. The re-check under the publication lock reads only what `flow set` and `spec record` can change, never the archive.

**Limits.** Deleting archived files by hand is an edit outside agenttalk: the flow cannot see what was deleted, only that a named record is missing. A store reset (`agenttalk reset`) deliberately deletes or moves the live history and leaves the archive in place. How the flow treats history from before a reset is decided in B1; until then a reset is outside this guarantee.

## 11. The trial: half a day, then thrown away

*Serves AC-15 and tests AC-4 to AC-14, AC-17 and AC-18.*

**What runs.** A throwaway patch in a scratch clone of agenttalk at the base revision. It is never pushed, and no product code changes. It adds:
- a minimal guard in `Store.send`, for `task` and done `task-response`;
- a minimal `flow` dimension in the close evaluation, with a frozen route set by `close open --work-item`;
- small scripts standing in for `flow set` and `spec record`.

The real commands (`agenttalk task`, `broadcast`, `broadcast --resume`, `reply`, `close open`, `close check`, `close publish`) run from that clone against a scratch store, never the live one. Every command is a fresh process. A switch in the throwaway guard can stop a broadcast after its first copy, to make a partial batch for the resume case.

**Each refusal is checked twice.** First, that the command exits with the expected code. Second, that nothing anyone could act on exists: no task message for that request in the scratch store, no copy of the batch, no new resume copy, no reply, or `close check` exit 3 and no GO record after `close publish`.

**Cases.** **F1**: the CSV export of section 4. **F2**: a JSON export whose AC-2 text changes under the same number midway, by decision, while AC-4 is added. **M1**: a synthetic migration, a 100-line command-line module moved from one standard-library interface to its successor, with an original tag, five cases and one accepted difference.

**The valid path, fixed beforehand.** A fresh project with no read and no waiver. `init`, then record F1. The spec-read task passes as the exemption, and its reader answers GO. Then a bound build task, and a done reply at H1 with one passing line per criterion. Then a work read at H1 answering GO with one line per criterion, and `close open --work-item`, `close check` and `close publish`, ending in GO.

| # | Invalid case | Expected answer, and nothing consumable left |
|---|---|---|
| 1 | F1 build sent before the spec read | `flow_spec_not_read`; no task |
| 2 | a work package with no work item, or no criteria | `flow_work_unbound` / `flow_spec_missing`; no task |
| 3 | F2 evidence for AC-2 from before the revision | `flow_evidence_stale`; no GO |
| 4 | a criterion claimed covered with a test that was only read | `flow_proof_missing`; no reply |
| 5 | a **failed check** (`-> fail`) | reply accepted as a handoff; `flow_criterion_uncovered`; no GO |
| 6 | a **negative read** (FIX at H1) | `flow_read_not_go`; no GO. After a fix round to H2, passing lines and a GO read at H2: GO |
| 7 | **H1 to H2**: evidence and read at H1, close at H2 | `flow_criterion_uncovered` ("only another revision"); no GO |
| 8 | M1 evidence naming another original, or a candidate that is not the head | `flow_artifact_mismatch`; no reply |
| 9 | F2's AC-2 text changed under the same number with no decision; also a change inside a literal (two spaces to one) and a Unicode-only change; then a **chained revision** (editorial v2, then substantive v3) | `spec_change_unrecorded` for each change without a decision; carried after v2, `flow_evidence_stale` after v3 |
| 10 | **unchanged-ID case and difference revisions**: C-3's content and D-1's text changed under the same numbers | old results `flow_evidence_stale`; no GO |
| 11 | **unresolved policy or scope**: an unreadable `flow.json`; a valid `flow.json` whose **pin cannot be resolved**; a close naming no work | `flow_policy_unresolved` at dispatch and close; `flow_scope_unmapped`; no task, no GO |
| 12 | the flow switched to `none` while F2's build is open | the done reply is still checked; the close still needs the `flow` dimension |
| 13 | **`flow.json` deleted, or opt-out, after the handoff and before the close** | close held (`flow_policy_unresolved`, or the frozen route still evaluated), never zero requirements; no GO |
| 14 | an invalid **broadcast** task; then a partial batch, a spec revision and `--resume` | refusal; zero copies; the resume refused with no new copy, and the batch shown partial |
| 15 | F1 built so that AC-3 overwrites the file, every number linked | never reported met. A reader's FIX on AC-3 holds acceptance (`flow_read_not_go`). A reader's GO despite the defect would pass: that is the stated limit, and the trial records which happened |
| 16 | a `release` close containing an F2 delivery no GO close accepted | `flow_obligation_open`; no GO |
| 17 | **compaction**: a bound build is done but not accepted. Make its messages and the activation note eligible (advance the scratch seats' cursors, lower the scratch store's keep count and age), run `agenttalk compact`, then restart | the activation note and the task's messages are in `archived/compacted/`; a new bound task is still accepted; `flow status` and `close check` show the same unaccepted obligation as before the compaction; after the archived activation note is deleted by hand, `flow_policy_unresolved` |

**Pass criteria:**
1. Every invalid case gets its expected answer, and leaves nothing consumable.
2. The valid path reaches GO with no duplicate report and no extra review round.
3. F2's recovery path clears acceptance after the re-check.
4. A fresh process re-running `close check` on every close, and `flow status`, gives byte-identical output, also after the compaction in case 17.
5. The opt-out is visible.
6. No output calls a link proof.
7. F1 stays within four added commands and fifteen added minutes against today's skill-plus-gate approach (the done record in the brief, a `dod.json` close). The count includes bootstrap, author and lead work, and the creation of every record, including the scripts' inputs. F2 and M1 effort is recorded too.

**The four-hour cap, honestly.** This is more than a classifier, and it may not all fit. The work runs in this order:
1. the write-path guard with cases 1 to 4 and 14;
2. the close boundary with cases 3, 5 to 7, 11 to 13, 16 and 17;
3. the migration cases 8 and 10;
4. case 9 and the measurement.

If step 1 or 2 does not fit, the trial stops and reports, and no build order starts. The assumption that then stays unproven is that one check in `Store.send`, and the frozen close route, refuse every path before anything consumable exists, and that a compaction changes no result. Even a complete trial does not test concurrency. The re-check under the publication lock against a simultaneous `flow set` runs one process at a time here; it becomes a test in B3.

**What it cannot establish:** that the flows improve outcomes (#153's blind comparison stays); the burden on a real team over weeks; review quality; the product code itself (the trial's guard is thrown away); other platforms; who really acts behind a roster name.

## 12. The rule for a third flow

A third flow is added only when a written proposal shows four things:
1. an evidence requirement that no profile of the two flows has, or a refusal at an existing step that they lack;
2. why a profile inside `spec-driven` cannot carry it;
3. a challenge that did not answer stop;
4. a trial case for it.

Today's candidates fail the first test. Changes to an existing product are `spec-driven` with `feature`, `bug-fix` or `refactor` after an `onboarding` run. Releases are `project-or-release` with the release gates. Documentation is `documentation`.

## 13. Build outline after the trial

One subject per pull request; each goes challenge, build, cross-vendor read, one fix round, final read.

| Order | Builds | Its acceptance criteria | Serves |
|---|---|---|---|
| B0 | the trial | section 11's pass criteria; results kept | AC-15 |
| B1 | `init --flow`; `flow show`; `flow set` (activation, switch, opt-out, `--repin`); fail-closed `flow.json`; flow notes; retained policy text; pin resolution; board header | a never-activated store is unchanged; an activated store with a missing file, missing history or unresolved pin refuses; activation is still found after a compaction; a switch lists open obligations | AC-1, 2, 3, 9, 10, 18, 19 |
| B2 | `spec record`, `spec show`, `spec diff`; exact fingerprints for criteria, cases and differences; decision check; spec-read exemption | a change under the same number, including inside a literal, is refused without a matching decision; chained revisions tested | AC-4, 5, 8 |
| B3 | the shared check in `Store.send`; broadcast pre-check; resume re-check; the re-check under the publication lock; 6.1 codes; waivers | each code has a failing-first test on every path (task, broadcast, resume, reply, draft); a refused step leaves no message; a concurrent `flow set` test; a compaction running during a check misses no record | AC-6, 7, 19 |
| B4 | 6.2 delivery checks | each code tested; a handoff listing every criterion as open or failed is accepted | AC-7, 11 |
| B5 | the frozen flow route; the 6.3 evidence rules and codes; durable obligations; the release-close ancestry check | the close cases of section 11 as tests, including compaction (case 17); the same answer after a restart and after a compaction | AC-6, 7, 8, 9, 10, 14, 17, 19 |
| B6 | `flow status` | every section 9 item from a fixture; the footer present | AC-13, 14 |
| B7 | migration evidence | the four section 8 codes and case/difference staleness tested; never "equivalent" | AC-12, 8 |
| B8 | the definition-of-done skill's "nothing enforces it" line, the lead skill's dispatch example, a manual page, the changelog | a review-docs read; no text calls a link proof | AC-1, 14 |

The default in `init` turns to `spec-driven` only when the checks it advertises have shipped (question 1).

## 14. Kill signals

From ch-012b0a23: "Stop and rescope if this needs a second task-state machine, treats matching identifiers as proof of behavior, silently accepts stale approvals, or becomes so burdensome that the small-feature trial bypasses it."

From ch-11fc8afc: "Stop expansion if either flow needs a second task-state machine, if matching IDs count as proof, or if missing profile mappings become an implicit opt-out. Existing close code intentionally gives unmapped scopes zero requirements; enabled-flow enforcement must address that boundary. Stop claims of preventing external file edits or proving parity from a comprehension inventory. Agenttalk can refuse its own transitions and report observed drift, not control other editing tools."

Where this design answers them:
- **A second state machine:** section 10.
- **Identifiers as proof:** AC-14, AC-17, `flow_read_not_go` and trial case 15.
- **Stale approvals:** section 4, `flow_evidence_stale` and trial cases 3, 9 and 10.
- **Burden:** the section 11 measurement and question 2.
- **Mappings as an implicit opt-out:** section 3 (activation), section 5 (durable obligations), `flow_scope_unmapped`, `flow_work_unbound`, `flow_policy_unresolved`, section 10.1 and trial cases 11, 13 and 17.
- **Outside edits and parity:** the section 1 limits, section 8 and the section 9 footer.

## 15. Technical detail

All proposed.
- **`flow.json`**: `schema_version`, `flow`, `policy` (the pin `definition-of-done@<label> tree <id> evidence <blob>`, optional `overlay`), `decision_owner`, `exception_authority`, `changed_by`, `changed_at`, `reason`, `note_id`, `pre_flow`. It is validated like `load_dod_policy` in `close.py`: size bound, duplicate and unknown keys rejected, fail closed.
- **Flow notes**: kind `note` to the team, with meta `flow_setting`, `flow_policy`, `reason`. "Activated" means at least one flow note is in the history (live or archived).
- **History read**: flow derivation calls `envelope_snapshot.validated_active` on `messages/` and then on `archived/compacted/`, as `work_board_facts._candidates` does, joined by message id (section 10.1).
- **Retained policy text**: `.agenttalk/flow/policy/<tree id>/`, written once. The tree id and blob ids are computed as git computes them, so they compare with `git rev-parse <tag>:<path>`. The key must cover the whole pin, not only the skill tree: see binding requirement F3 (section 16).
- **Spec versions**: `.agenttalk/specs/<spec id>/<version>.json`, created exclusively. Each holds:
  - the source path and git blob id, and the file hash;
  - for each criterion, case and difference: `id`, `text` and `sha256`. The fingerprint is SHA-256 over the exact UTF-8 bytes after `<ID>: `, up to the line ending, with no normalization;
  - the withdrawn numbers, and for migration the `original`;
  - `recorded_by`, `recorded_at`, `decision` and the previous version's hash.
- **Task meta**: `profile`, `risk`, `spec` (`<id>@<version>`), `criteria`, `cases`, `migration_stage`, `lane`, `waiver`, `read_of` (`spec` / `work`), and the stamped `flow_policy`. `work_item`, `stage` and `work_head` are reused unchanged.
- **Evidence lines**: these use the existing `evidence` field of the `production-handoff` and `review-result` profiles, so there are no new field names: `criterion <id>: executed <check> -> pass|fail <details>`, `criterion <id>: open - <reason>`, `criterion <id>: checked <how>`, `case <id>: original <rev> candidate <rev> -> <outcome>` and `derived <fingerprint> from candidate <rev>`. `head_ref` is skill policy today; a bound delivery makes it mechanical.
- **Hooks**:
  - the flow check is called in `Store.send` after `work_tags.normalize`, and called again under `_message_publication_lock`;
  - `cmd_broadcast` pre-checks the resolved audience before the first copy, and checks the remaining copies before a resume writes any;
  - `close open` freezes `flow_route`, and `_build_dod_eval` adds the `flow` dimension whenever `flow_route` is in the record, as it does for `acceptance_route`;
  - `close publish` stores the ids of the evidence messages that counted.
  - Extra work under the publication lock lengthens the one store-wide guard, a known scale limit. B3 measures it.
- **Decisions and waivers**: these are `proposal` messages with meta `waives=<code>`, `for=<id>` and `until=<date>`, or `spec=<id>`, `spec_version=<n>` and `changes=<id>:<kind>,...`. They are answered by `proposal-response` `status=accepted` from the authority named in `flow.json`: a roster name, not an authenticated person.

## 16. Read findings and what changed

The independent read tk-2eda8b4e6c31 (on `f8c2b3f7`) returned FIX. Every finding is fixed in this file; none asks for a change elsewhere.

| # | Finding | Fixed in |
|---|---|---|
| P1-1 | broadcast and resume skip a check placed in `cmd_task` | 6.0 (one check in `Store.send`, broadcast pre-check, resume re-check, publication lock); trial case 14; B3 |
| P1-2 | no rule for which test and read results qualify | 6.3 (passing check at R, latest work read at R is GO, FIX/HOLD dispositions); 6.2 (failed checks hand over); AC-17; cases 5-7 |
| P1-3 | a lost policy file turns acceptance back into zero requirements | 3 (activation from flow notes); 5 (durable obligations); 6.3 (frozen flow route, release ancestry); AC-6, AC-10; cases 12, 13, 16 |
| P1-4 | the trial classifies instead of refusing at the real boundaries | 11 (throwaway guard at `Store.send` and close; absence of consumable results; broadcast, resume, fresh process; the cap and the unproven assumption) |
| P2-5 | the first spec read is refused by its own rule | 4 (`read_of`, the exemption); 6.1 (`flow_spec_reader_is_author`); the valid path starts fresh |
| P2-6 | unresolved pins and profile obligations have no check | 3 (retained policy text, recovery); 5 (checked versus judged table); AC-18; case 11 |
| P2-7 | a changed case keeps its old result | 8 (case and difference fingerprints, staleness, declaration by exact content); case 10 |
| P2-8 | the fingerprint erases changes inside literals | 4 and 15 (exact bytes, no normalization); case 9 |
| P2-9 | refused lane deliveries leave no record | 9 (narrowed to a fresh evaluation now; history not reported) |
| P3-10 | migration codes lack messages | 8 (message and permitted reduction per code) |
| Q1-Q5 | the reader's decisions on the open questions | 17 (adopted; question 3 goes one step further) |

**The final read** tk-a5b0159dcf8b (on `1374d173`) returned FIX on one new P1, now fixed, and set the remaining findings as binding requirements for the trial and the build.

| # | Finding | Fixed in |
|---|---|---|
| F1 (P1) | compaction can remove the history that makes obligations durable | 10.1 (history read from the live bus and its archive; the keep-floor route rejected, with the reason; missing history reported); 3 (the false sentence that the bus never forgets a message is corrected); 5; AC-19; trial case 17; B1, B3, B5 |
| F6 (P2) | the summary overstated which tasks need criteria and which wording changes invalidate evidence | the one-page summary at the top |
| F7 (P3) | `flow.json` was called both changeable and written once | 3 and 10 (the changeable current setting versus the written-once history) |

### Binding requirements from the final read

These are not redesigned here. Each binds the step the reader assigned:
- **F2, a close opened before a later revision (B0, B5).** Test the order: open a close on W at spec v1, record a substantive v2 that changes AC-2 and adds AC-4, then check and publish the old close. At check and at publish, compare the frozen route with the current decisions and work bindings, and hold or amend the route before accepting. Keep the original pins as provenance.
- **F3, the retained-policy key (B1).** Key a retained policy bundle by the full composite pin (skill tree, shared evidence blob, overlay), or store each part under its own content identity. Test two pins with the same skill tree and a different evidence file or overlay: both stay resolvable after `--repin`.
- **F4, valid draft replies (B3, B4).** Define how a body-only draft reply supplies `head_ref`, `reviewed_ref`, the evidence lines and the verdict: a bounded structured envelope, or one shared body-to-evidence parser, including conflicts with CLI meta. Add a valid build draft and a valid work-read draft to the checks, beside the refusal cases.
- **F5, broadcast all-or-nothing (B3).** Pause after the first copy, change the policy or spec, then attempt the second. Decide whether policy changes are held back for the whole fan-out, or the result is explicitly partial, with surviving bindings and a recovery. Do not claim both per-copy locking and all-or-nothing delivery; the single-process trial is not concurrency evidence.
- **N1, a passing control for compaction (B0, B5)**, from the narrow check tk-b9ad2fd04529. Case 17 keeps its unaccepted work as the negative control. Beside it, prepare a close that returns GO and depends on an accepted decision (for example an editorial carry-forward), a spec approval, passing handoff evidence and a GO work read. Make those records eligible, confirm they actually moved to the archive, compact, restart, and require the same GO with the same contributing evidence ids.
- **F6 (B8) and F7 (B1, B8)** are fixed in this file; B8 keeps the manual's summary and wording to the same scope.

## 17. Open questions

These remain questions for the owner. Each recommended answer is the independent reader's position, adopted.

1. **When does the `init` default change?** Recommended: only when every check the default advertises has shipped. Either ship B1 to B8 together, or switch `spec-driven` on with B1 to B6 and B8 while `migration` is refused as "not available in this release" until B7 ships.
2. **How much burden is too much?** Recommended: for F1, at most four added commands and fifteen added minutes against today's approach. The count includes bootstrap, author and lead work, and the creation of every record. F2 and M1 effort is recorded too, without a ceiling. Above the F1 ceiling, simplify before any build order.
3. **Who may waive which refusal?** Recommended: the per-code table in section 6, not a blanket waiver for the lead. An unknown criterion and the exact artifact are never waived. A dispatch waiver only defers its check to acceptance, and reductions are reported as reductions. Going one step further than the reader, `flow_work_unbound` is never waived: a waived unbound task would leave no identifiable obligation, and binding it costs two options.
4. **May an `editorial` revision carry evidence forward?** Recommended: yes, only through a recorded decision naming each affected criterion, case or difference. The substantive-or-editorial call stays a human judgement, always shown as "carried by decision", and chained revisions are tested.
5. **Do acceptance-pass rows count as criterion evidence in the first build?** Recommended: no. Existing acceptance-route obligations stay independently enforced, and opting into a flow never replaces them.
