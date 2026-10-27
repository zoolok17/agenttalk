# Development flows: a spec-driven flow and a migration flow, enforced at agenttalk's own steps

Status: proposed. Written on 2026-10-27 (UTC) for work order tk-d00501b1ae5f, after the challenges ch-012b0a23 (the spec-driven core) and ch-11fc8afc (the wider family) both returned RESHAPE; the lead accepted both reshapes as binding. Nothing here is built. Where it describes agenttalk today, it matches master `544c6a1c` (after release 0.104.0). Owner: lead.

**In plain words:** today a team writes down what "done" means for a task in a skill text, and nothing in agenttalk checks it. This design proposes two built-in ways of working. The **spec-driven flow**, on by default for new projects, asks for a short spec with numbered acceptance criteria, an independent read of it, and work that names the criteria it serves. The **migration flow** adds what moving an existing system needs: a fixed original, the delivered candidate, comparison cases declared before any run, and the differences someone accepted. Where a flow is used, agenttalk refuses its *own* steps (sending a task, handing work over, accepting it) when records are missing or out of date, and reports the drift it can see; it cannot stop edits made with other tools or prove that code does what a criterion says. The document decides the records, the refusals, a half-day trial that comes first and the build after it; section 16 lists what it leaves open.

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
16. [Open questions](#16-open-questions)

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
| AC-4 | **A versioned spec.** Each version is recorded once, never rewritten, with a fingerprint per criterion. Changed text under an unchanged number is refused without a recorded decision. |
| AC-5 | **Read before work.** Work citing a spec version cannot be sent until someone other than its author has recorded a read of it. |
| AC-6 | **Requirements travel with the task.** Every task names its work item and profile, and where needed the spec version and criterion numbers, fixed when it is sent. |
| AC-7 | **Named refusals at four existing steps.** Dispatch, delivery, acceptance and close refuse the cases in section 6, each with a stable code, a message saying what to do, and a waiver path recorded on the bus unless never waivable. |
| AC-8 | **A spec change makes affected evidence stale.** Evidence for changed criteria stops counting; for unchanged criteria it still counts. Section 4 gives the recovery path. |
| AC-9 | **Switching never drops obligations.** Opting out or switching keeps each open work item's requirements until it closes or the exception authority releases them by a recorded decision. |
| AC-10 | **Unmapped is not an exit.** In a flow project no close counts as "zero requirements", a close naming no work is held, and a task with no known profile is refused. |
| AC-11 | **Finished, accepted and released stay separate.** A builder can always hand over with open items listed; the independent read is the next owner's work; acceptance needs it; release keeps its own rules. |
| AC-12 | **Migration evidence.** A migration spec fixes the original, declares comparison cases before any run and lists accepted differences; closes need evidence for the named candidate, and a whole migration needs combined-artifact and rollback evidence. No claim of equivalence. |
| AC-13 | **Drift is reported**, with a plain statement of what agenttalk cannot see. |
| AC-14 | **Restart-safe and honest.** Every refusal and report is recomputed from files, with the same answer after a restart. Nothing calls a linked criterion met, proven or correct. |
| AC-15 | **Trial first.** Section 11 passes before any later build order starts. |
| AC-16 | **No second task-state machine** (section 10). |

Non-goals: a planner, a task registry, generated code or tests, claims that the method improves outcomes (that stays with #153's blind comparison), and any use of the external spec tool removed on 2026-09-28.

## 2. What exists today, and what is reused

- **The definition-of-done skill** (`src/agenttalk/skills/devkit/definition-of-done/`) gives every task a five-line done record and eleven profiles: `review-only`, `documentation`, `investigation`, `qa-test`, `bug-fix`, `refactor`, `ci-fix`, `feature`, `project-or-release`, `migration-slice`, `migration`. It adds a risk word, exception rules (not applicable only at assignment, scope reductions by an exception authority, never-waivable rows) and a policy pin by git tree id. It keeps finished, accepted and releasable apart, and puts the independent read after the build closes. *Reused:* profiles are the per-task binding (section 5), exceptions are the waiver rules (6), the split is section 7.
- **Its migration reference** defines levels A (a slice), B (the whole migration) and C (live, never claimed) and the roles original, candidate and derived; **its project overlay** names a maintainer and an exception authority, pinned by blob id. *Reused:* section 8, and who decides and who waives.
- **`lane`** is an advisory assignment to path prefixes; `lane deliver` answers HOLD (for example `out_of_bounds_path`) and writes no delivery record. A lane has no work-item link, only free-text notes. *Reused unchanged;* a task may name its lane.
- **`gate`** keeps named checks; a blocker turns green only from CI; `gate waive` records operator, reason, scope and expiry. *Reused:* permission to release (7).
- **`close`** is opened on an exact revision with a `--scope`; `.agenttalk/dod.json` maps scopes to the dimensions `assurance`, `coverage`, `knowledge` and `acceptance`. A missing file or an unmapped scope means zero requirements, on purpose (`derive_required_dod` in `close.py`). *Reused:* one more dimension, `flow` (6.3), which closes that boundary for flow projects.
- **The acceptance pass** (`acceptance.py`; schemas 1-4 in `acceptance_schema.py`) has frozen plan rows, a final cold review, successors with an amendment reason, and obligations that "follow the change". *Reused:* the model for revisions and surviving obligations.
- **The work board** is a pure projection of messages tagged `work_item` and `stage` (`design`, `build`, `read`, `fix`, `delta`, `sweep`), with typed verdicts and no stored task state. *Reused:* it carries all work state (10).
- **`onboarding`** keeps an append-only ledger of segments, claims, drift and unknowns; **`comprehension`** builds an offline static inventory of a legacy repository. *Reused:* inputs to a migration spec, never comparison evidence.
- **#153** proposes a migration stage model (contract, move, improve, UI, infra), six programme documents, a cold-sweep protocol, and a lean migration skill that ships only if a blind comparison shows it helps; the migration playbook and lesson pack shipped in 0.103.0. *Reused:* section 8 maps #153 onto the flow; #153 keeps its method.

**The replay of the definition of done** (`docs/research/DOD-REPLAY-2026-10.md`) found 61 late findings in six finished jobs: 10 became unambiguous checks (7 were already rules in the review skills), 17 needed judgement, and 34 were not caught; the largest class, logic that depends on the order of events, needs an independent read. No time was measured. So the flows can move forced checks to the handoff and make missing or stale evidence visible; they cannot promise fewer defects, and their cost must be measured.

**Missing today:** a spec record (AC-4); a recorded project choice (AC-1, AC-2); any check of profile, criteria or pin when work moves, since `agenttalk task` checks only the sender's role, the body, the work tags and the recipient's version, and no code checks challenge meta (AC-6, AC-7); links from criteria to evidence, with staleness (AC-8); the unmapped-scope boundary (AC-10); a checked record of original, candidate and cases (AC-12); a drift report (AC-13).

## 3. The project's choice

*Serves AC-1, AC-2, AC-3, AC-9.*

**Where.** In `.agenttalk/flow.json`, beside `dod.json` and `signoffs.json`, read the same way: a malformed file fails closed. Only `agenttalk flow set` changes it, and it also sends the team a note with the new setting, who changed it and why; the file records the note's id. If the file is missing after a flow was chosen, or differs from the last note, agenttalk refuses to send tasks (`flow_policy_unresolved`) rather than act as if no flow were chosen.

**A new project.** `agenttalk init` gains `--flow`, default `spec-driven`, and says so (proposed, not runnable yet):

> Development flow: spec-driven (the default). To turn it off: `agenttalk flow set none --reason "<why>"`.

`agenttalk init --flow none --reason "<why>"` opts out at the start. `agenttalk flow show` prints the setting, its history and the open work bound to it; the work board shows the flow in its header.

**An existing project** has no `flow.json` and keeps today's behaviour, which `flow show` says in those words. The lead opts in with `agenttalk flow set spec-driven --reason "<why>"`. This **activation** records the work items open on the board at that moment as *pre-flow*; they finish under the old rules and are labelled so in every report. To bring one under the flow, the lead rescinds its task and sends a bound replacement for the same work item (the existing `supersedes` tag).

**Switching.** A task's requirements are fixed in the task itself (section 5), so a switch to `migration` or `none` cannot remove them. `flow set` prints each open work item and what it still owes; those items stay checked at their own delivery and acceptance until they close, unless the exception authority records a scope reduction. New tasks follow the new setting.

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

**Versions.** `agenttalk spec record --file specs/export-csv.md` stores version 1 in the bus store, never rewritten, with a fingerprint of each criterion's text. Tasks and evidence cite `export-csv@1`, and `agenttalk spec show export-csv@1` prints exactly that text, so builders read the recorded text, not whatever the file says today.

**Changed text under the same number is caught.** For each new version agenttalk compares fingerprints with the previous one. A changed text or an added or withdrawn criterion is a *change*, recorded only with `--decision <request id>`: an accepted proposal (the existing `proposal` / `proposal-response` pair) from the spec's decision owner that lists each change as `substantive` or `editorial`. If that list differs from the changes agenttalk found, it refuses with `spec_change_unrecorded`. A withdrawn number is never reused (`spec_id_reused`).

**The read.** Before work cites a version, someone other than its author records a read of it: a `read`-stage task whose reply has the verdict GO and cites `export-csv@1`. The spec itself is written in the work item's `design` stage.

**Staleness.** Evidence is tied to the fingerprint of the criterion text it was produced for. After a revision, evidence for unchanged text still counts, evidence for a `substantive` change is stale, and evidence for an `editorial` change is carried forward and shown as "carried by decision `<id>`", a claim agenttalk records but cannot check.

**Recovery.** (1) The decision owner accepts the proposal and the author records the new version with `--decision`. (2) `agenttalk flow status` lists the stale evidence per work item and the open tasks citing the old version; those tasks can still hand over. (3) The lead sends re-check tasks citing the new version, for changed and added criteria only, or the exception authority records a scope reduction, which shows as reduced scope, never as met. Old versions stay readable, so a task sent under version 1 can always be read against the text it was given.

## 5. Each work item carries its own requirements

*Serves AC-6, AC-9, AC-11.*

A task in a flow project names its **work item** and **stage** (both exist), its **profile** and **risk** (`normal` or `high`), where needed the **spec version** and **criterion numbers**, for migration work the **comparison cases**, and optionally its **lane** and a **waiver**. agenttalk adds the project's pinned definition-of-done identity, as it already stamps the epoch on every task. A sent message is never edited, so later settings cannot change any of this.

| Profile | Criteria at dispatch | Extra |
|---|---|---|
| `feature`, `project-or-release` | required | - |
| `migration-slice`, `migration` | required, from a migration spec | cases; one #153 stage per slice |
| `bug-fix` | the criterion the defect breaks, if any | the defect shown on the original |
| `documentation`, `refactor`, `ci-fix` | optional | - |
| `review-only`, `qa-test`, `investigation` | none; a read of a spec cites the version read | - |
| any other kind | the closest profile, with the reason (the skill's own rule); an unknown name is refused | - |

A requirement applies only where its evidence can exist: a documentation task is never refused for lacking criteria, and a fix to old code with no spec need not invent one.

## 6. Named refusals at existing steps

*Serves AC-7, AC-8, AC-10, AC-11, AC-12.*

Every message reads `agenttalk <command>: refused (<code>): <message>`, with stable codes that reports and tests can count. A **waiver** is an accepted proposal from the named authority, naming the code, the task or close, a reason and an expiry. The project overlay names the **exception authority** and the **decision owner**; until it does, the lead is both and the operator decides when the lead is the doer. Identities are cooperative roster names, so reports count every waiver.

### 6.1 Dispatch (`agenttalk task`)

- **`flow_policy_unresolved`**: `flow.json` is malformed, missing after a flow was chosen, or differs from the last flow note. "This project's flow setting cannot be read or does not match its last recorded change. Run `agenttalk flow show`." Never waived; fix it with `flow set`.
- **`flow_work_unbound`**: no work item, no profile, or an unknown profile. "This project uses the `<flow>` flow: add `--work-item <id> --meta profile=<kind of work>`." Waiver: the lead, recorded.
- **`flow_spec_missing`**: the profile needs criteria and none are cited. "A `<profile>` task needs `--meta spec=<id>@<version> --meta criteria=<numbers>`." Waiver: the lead, recorded.
- **`flow_spec_not_current`**: the cited version is not the latest. "`<id>@<n>` was replaced by version `<m>`. Cite `<id>@<m>`." Waiver: the decision owner.
- **`flow_spec_not_read`**: only its author has read that version. "Nobody but its author has read `<id>@<n>`. Send a `read` task first." Waiver: the exception authority.
- **`flow_criterion_unknown`**: a number is not in that version, or was withdrawn. "`<number>` is not a criterion of `<id>@<n>`. See `agenttalk spec show <id>@<n>`." Never waived.
- **`flow_original_undeclared`**: a migration task cites a spec with no fixed original. "The migration spec `<id>@<n>` names no original. Record a version that fixes it." Never waived.

### 6.2 Delivery (`agenttalk reply` with `status=done`, on a task bound to criteria or cases)

Delivery refuses only a handoff that contradicts itself. Anything unfinished can always be handed over as an open item, so no waiver is needed and a builder is never blocked by someone else's work.

- **`flow_criteria_unaccounted`**: a bound criterion is not mentioned. "The handoff says nothing about `<number>`. Add `criterion <number>: executed <check> -> <result>` or `criterion <number>: open - <reason>`."
- **`flow_proof_missing`**: a criterion is reported covered with no executed check, only a test that was read. "`<number>` is reported covered but names no executed check. Name the command or run id and its result, or list it as open."
- **`flow_artifact_mismatch`**: the evidence's candidate is not the handoff's head, its original is not the spec's, or a derived build names no candidate. "Evidence names `<found>` as the `<role>`; this handoff's `<role>` is `<expected>`. Evidence about another build does not count." Never waived: the exact artifact never is.

Only reported at delivery: a missing independent read (the next owner's work), and a spec that changed during the task ("The spec moved to version `<m>`; your evidence for `<numbers>` will not count until re-checked."). `lane deliver` keeps its own HOLDs.

### 6.3 Acceptance (`close check`) and close (`close publish`)

In a flow project every close carries the `flow` dimension and names the work it accepts (`close open --work-item`, proposed); `close publish` publishes GO only when these are clear. Waivers here are scope reductions by the exception authority, who is never the doer, and show as "reduced scope: `<number>`", never as met.

- **`flow_scope_unmapped`**: the close names no work item and no spec. "This close accepts nothing the flow can check. Name the work with `--work-item <id>`." Never waived.
- **`flow_criterion_uncovered`**: a criterion in scope has no current executed evidence. "`<number>` of `<id>@<n>` has no executed evidence."
- **`flow_evidence_stale`**: evidence was produced for criterion text that has changed. "Evidence for `<number>` predates its change in version `<n>`. Re-check it."
- **`flow_read_missing`**: no reader other than the builder names the criterion (for `risk: high`, none from another vendor). "No reader other than the builder has checked `<number>`."
- **`flow_obligation_open`**: a work item still owes something from its binding, also after a switch. "`<work item>` still owes `<what>`."
- **`flow_decision_unrecorded`**: a stored spec version changed without a decision, which only editing files outside agenttalk can cause. "Version `<n>` of `<id>` changed `<numbers>` without a recorded decision." Never waived.

Section 8 adds four migration codes.

## 7. Finished, accepted and released stay separate

*Serves AC-11.*

| Question | Who answers | Where | What the flow adds |
|---|---|---|---|
| Is the task finished? | the builder | the done reply | refuses only self-contradicting handoffs (6.2) |
| Is the product accepted? | the lead, from reviews and evidence | `close check`, `close publish` | the `flow` dimension (6.3) |
| May it be released? | the lead, on evidence for the revision | `gate`, `review-release-readiness` | nothing |

The builder's handoff lists the independent read as open, with the reader as next owner; the lead then sends the read as a new task. That is today's review cycle, kept: no extra review round, no second report.

## 8. The migration flow's own evidence

*Serves AC-12.*

A migration spec is the same record with three more parts, each changeable only by a recorded revision:
- **the original**: repository, full revision and artifact fingerprint, fixed for the whole migration; changing it makes every comparison result stale;
- **comparison cases** `C-1`, `C-2`, ...: what is sent, what is compared, and the clean-ups allowed first (timestamps, generated ids);
- **accepted differences** `D-1`, `D-2`, ...: old behaviour, new behaviour, who accepted it and why.

Each result names its role: **original**, **candidate** (the handoff's head) or **derived** (built from a named candidate). A result line reads `case C-3: original <rev> candidate <rev> -> same`, `-> differs (D-2)` or `-> differs (not accepted)`. agenttalk checks presence, identity and order only, and holds a close with:
- **`flow_case_declared_late`**: a result cites a case first recorded after the run;
- **`flow_difference_unaccepted`**: a difference that no accepted `D-` number covers;
- **`flow_combined_evidence_missing`**: a whole-migration close without the combined final artifact's fingerprint, or without results on it;
- **`flow_rollback_evidence_missing`**: a whole-migration close without a rollback record (boundary tested, restore point, measured recovery).

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
| cold sweep | the independent read; the acceptance pass's cold review where used |
| the lean skill and its blind comparison | unchanged and owned by #153; no claim that the method helps |

## 9. Drift: what agenttalk can and cannot see

*Serves AC-13, AC-14.*

`agenttalk flow status` is a read-only report, recomputed from files each time. It reports:
- evidence stale against the current spec version (fingerprints against the latest version);
- unlinked work and every waiver used (task meta, waiver proposals);
- a spec file that differs from its latest recorded version, per criterion;
- deliveries outside lane bounds, for tasks that name a lane (lane HOLDs);
- spec or flow changes without a decision (the version chain, the flow notes);
- uncovered criteria, open obligations, pre-flow items and expiring waivers (the board projection).

Its footer says what it cannot see: edits with other tools that never reach a delivery; whether code does what a criterion says; whether an `editorial` change is editorial; whether a test exercises the criterion it cites; whether two roster names are two people or vendors; a scope change agreed by email and never recorded.

## 10. No second task-state machine

*Serves AC-16.*

| Concept | Carried by (exists today) |
|---|---|
| a work item's progress | `task` / `task-response` messages tagged `work_item` and `stage`, projected by the board |
| the binding | the task's own meta, never edited |
| the spec read | a `read`-stage task and its GO verdict |
| delivery and evidence | the done reply's evidence lines; lane records |
| decisions and waivers | `proposal` / `proposal-response` messages |
| acceptance | the close record, with one more `dod.json` dimension |
| release | gates and `review-release-readiness` |

The only new records are `flow.json`, a policy file like `dod.json`, and stored spec versions, written once like the acceptance pass's retained plans. Neither holds task state; everything else is computed from the bus, so a restart cannot change a result.

## 11. The trial: half a day, then thrown away

*Serves AC-15 and tests the rules behind AC-4 to AC-14.*

**What runs.** A throwaway checker script, not product code, over a scratch bus store and git repository. Real `agenttalk task` and `agenttalk reply` commands carry the proposed meta; the checker prints the refusal it would give at each step. Cap: four hours; afterwards only the results file is kept.

**Cases.** **F1**, a small, well-understood feature: the CSV export of section 4. **F2**, a feature with a scope change: a JSON export whose AC-2 text changes under the same number midway, by decision, while AC-4 is added. **M1**, a synthetic migration: a 100-line command-line module moved from one standard-library interface to its successor, with an original tag, five cases and one accepted difference.

**The valid handoff, fixed beforehand:** F1 with its spec recorded and read, a bound build task, a done reply with one executed check per criterion, an independent read, and a close that clears the `flow` dimension.

| # | Invalid case | Expected |
|---|---|---|
| 1 | F1 build sent before anyone read the spec | `flow_spec_not_read` |
| 2 | a work package with no work item, or no criteria | `flow_work_unbound`, `flow_spec_missing` |
| 3 | F2 evidence for AC-2 from before the revision | `flow_evidence_stale` |
| 4 | a criterion reported covered with a test only read | `flow_proof_missing` |
| 5 | M1 evidence naming another original, or a candidate that is not the head | `flow_artifact_mismatch` |
| 6 | F2's AC-2 text edited, same number, no decision | `spec_change_unrecorded`; `flow_decision_unrecorded` if the stored version is edited by hand |
| 7 | an unreadable flow setting; a close naming no work | `flow_policy_unresolved`; `flow_scope_unmapped` |
| 8 | the flow switched to `none` while F2's build is open | the task still accounts for its criteria; its close still needs the `flow` dimension |
| 9 | F1 built so AC-3 overwrites the file, every number linked | never reported met; acceptance needs a reader naming AC-3; the trial records whether the read caught it |

**Pass criteria:** (1) every invalid case gets its expected answer at its step; (2) the valid handoff reaches each next stage with no duplicate report or extra review round; (3) F2's recovery path clears acceptance after the re-check; (4) re-running the checker from the files after a stop gives byte-identical output; (5) the opt-out is visible, and a deleted `flow.json` is caught; (6) no output calls a link proof. **Stop rule:** if case 1, 2 or 3 fails, stop and report; no build order starts.

**Measurement.** F1 runs twice, with today's skill-plus-gate approach (the done record in the brief, a `dod.json` close) and with the flow. Count added commands, added meta fields, author and lead minutes, and which invalid cases today's approach catches.

**What it cannot establish:** that the flows improve outcomes (#153's blind comparison stays); the burden on a real team over weeks; review quality; the real hooks in `task`, `reply` and `close`; other platforms; who really acts behind a roster name.

## 12. The rule for a third flow

A third flow is added only when a written proposal shows (1) an evidence requirement no profile of the two flows has, or a refusal at an existing step they lack; (2) why a profile inside `spec-driven` cannot carry it; (3) a challenge that did not answer stop; and (4) a trial case for it. Today's candidates fail (1): changes to an existing product are `spec-driven` with `feature`, `bug-fix` or `refactor` after an `onboarding` run; releases are `project-or-release` with the release gates; documentation is `documentation`.

## 13. Build outline after the trial

One subject per pull request; each goes challenge, build, cross-vendor read, one fix round, final read.

| Order | Builds | Its acceptance criteria | Serves |
|---|---|---|---|
| B0 | the trial | section 11's pass criteria; results kept | AC-15 |
| B1 | `init --flow`; `flow show`; `flow set` (activation, switch, opt-out); fail-closed `flow.json`; the team note; board header | a new store defaults to `spec-driven`; an old store is unchanged; a switch lists open obligations; a deleted file is caught | AC-1, 2, 3, 9 |
| B2 | `spec record`, `spec show`, `spec diff`; fingerprints; decision check; read lookup | a changed text under the same number is refused without a matching decision; old versions stay readable | AC-4, 5, 8 |
| B3 | dispatch binding, 6.1 refusals, pin stamp, waivers | each code has a failing-first test; a waiver works once, for its task, until expiry | AC-6, 7 |
| B4 | 6.2 delivery checks | each code tested; a handoff listing every criterion open is accepted | AC-7, 11 |
| B5 | the `flow` dimension, 6.3 codes, obligations after a switch | no scope derives zero dimensions; stale evidence never clears; same answer after a restart | AC-7, 8, 9, 10, 14 |
| B6 | `flow status` | every section 9 item from a fixture; the footer present | AC-13, 14 |
| B7 | migration evidence | the four section 8 codes tested; never "equivalent" | AC-12 |
| B8 | the definition-of-done skill's "nothing enforces it" line, the lead skill's dispatch example, a manual page, the changelog | a review-docs read; no text calls a link proof | AC-1, 14 |

## 14. Kill signals

From ch-012b0a23: "Stop and rescope if this needs a second task-state machine, treats matching identifiers as proof of behavior, silently accepts stale approvals, or becomes so burdensome that the small-feature trial bypasses it."

From ch-11fc8afc: "Stop expansion if either flow needs a second task-state machine, if matching IDs count as proof, or if missing profile mappings become an implicit opt-out. Existing close code intentionally gives unmapped scopes zero requirements; enabled-flow enforcement must address that boundary. Stop claims of preventing external file edits or proving parity from a comprehension inventory. Agenttalk can refuse its own transitions and report observed drift, not control other editing tools."

Where this design answers them: a second state machine, section 10; identifiers as proof, AC-14, `flow_read_missing` and trial case 9; stale approvals, section 4, `flow_evidence_stale` and trial case 3; burden, the section 11 measurement and question 2; mappings as an implicit opt-out, `flow_scope_unmapped`, `flow_work_unbound`, `flow_policy_unresolved` and AC-10; outside edits and parity, the section 1 limits, section 8 and the section 9 footer.

## 15. Technical detail

All proposed.
- **`flow.json`**: `schema_version`, `flow`, `policy` (the pin `definition-of-done@<label> tree <id> evidence <blob>`, optional `overlay`), `decision_owner`, `exception_authority`, `changed_by`, `changed_at`, `reason`, `note_id`, `pre_flow`. Validated like `load_dod_policy` in `close.py`: size bound, duplicate and unknown keys rejected, fail closed.
- **Spec versions**: `.agenttalk/specs/<spec id>/<version>.json`, created exclusively, holding the source path and git blob id, the file hash, each criterion's `id`, `text` and `sha256`, the withdrawn numbers, for migration `original`, `cases` and `differences`, then `recorded_by`, `recorded_at`, `decision` and the previous version's hash. A criterion fingerprint is SHA-256 over its text after Unicode NFC, trimming and collapsing whitespace: reflowing a line changes nothing, changing a word does.
- **Task meta**: `profile`, `risk`, `spec` (`<id>@<version>`), `criteria`, `cases`, `migration_stage`, `lane`, `waiver`, and the stamped `flow_policy` hash. **Evidence lines** use the existing `evidence` field of the `production-handoff` and `review-result` profiles (no new field names): `criterion <id>: executed <check> -> <result>`, `criterion <id>: open - <reason>`, `criterion <id>: checked <how>`, `case <id>: original <rev> candidate <rev> -> <outcome>`, `derived <fingerprint> from candidate <rev>`.
- **Hooks**: dispatch in `cmd_task` (`cli.py`) after the sender-role and work-tag checks; delivery beside the reply refusals of `work_tags.py`, with refused replies following the existing refused-reply handling; acceptance as a `flow` entry in `_DOD_SUPPORTED_DIMENSIONS` (`close.py`) that `derive_required_dod` adds to every scope of a flow project, mapped or not, while a project without `flow.json` keeps today's zero-requirement default byte for byte.
- **Decisions and waivers**: `proposal` messages with meta `waives=<code>`, `for=<id>`, `until=<date>`, or `spec=<id>`, `spec_version=<n>`, `changes=<id>:<kind>,...`, answered by `proposal-response` `status=accepted` from the authority named in `flow.json`: a roster name, not an authenticated person.

## 16. Open questions

1. **When does the `init` default change?** Recommended: only in the release that ships B1 to B5 together; a default with half the refusals would teach a flow that does not hold.
2. **How much burden is too much?** Recommended: for F1, at most four added commands and fifteen added minutes against today's approach; above that, simplify before any build order.
3. **Who may waive a dispatch refusal?** Recommended: the lead, who sends the task, with every waiver counted in `flow status`; acceptance waivers stay with an exception authority who is not the doer.
4. **May an `editorial` revision carry evidence forward?** Recommended: yes, only by an accepted decision naming each number, always shown as "carried by decision".
5. **Do acceptance-pass rows count as criterion evidence in the first build?** Recommended: no; add it through a new acceptance schema version after one project has used both.
