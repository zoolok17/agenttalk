# Design: rolling out the secure-coding standard

Status: accepted for the pilot. Written on 2026-10-28 for work order tk-6f367777d3cc, after the challenge ch-651fb888 returned RESHAPE and the lead accepted it. Revised in fix round tk-4f0f5ca4e611 after the read tk-1297b29787e4; in fix round tk-deae55f9bdd9 after the final read tk-d62bcfb4bff4 (requirements R1 to R5) and a read by the second team's reviewers (2026-10-29); and in fix round tk-9830ec5cc9c4 after the delta read tk-b1d90a6c8abc. The operator accepted the standard and this plan for the pilot on 2026-10-29. The pilot ran on 2026-10-29 and 2026-10-30, met its bar, and the operator accepted its cost and read its kill signal as not fired (2026-10-30); the wiring pull request of section 2 followed, with the decisions recorded in section 7. Where this page describes agenttalk today, it matches master `12418bb6`. Owner: the lead.

## In plain words

[The secure-coding standard](../SECURE-CODING.md) asks a builder to write a short security note before coding, choose protections while coding, and show at handoff which code and which executed checks carry each choice. This page plans how the standard would reach everyone who writes or reads code: which skills, briefs and contributor pages would point to it, and how someone working from a fork, without our tools, sees it. It also plans a small pilot that can fail: one real Python change, and one fresh task that nobody has seen, built both with and without the standard by the same model, so that a difference, gained or lost, gives a tentative sign of what the standard does. It decides the order of the work, the bar the pilot must pass and what happens when the pilot finds no gain; it leaves open the questions in section 7.

## Contents

1. [Order of work](#1-order-of-work)
2. [Where the standard would be loaded](#2-where-the-standard-would-be-loaded)
3. [What a contributor from a fork sees](#3-what-a-contributor-from-a-fork-sees)
4. [Evidence wording, shared with the development flows design](#4-evidence-wording-shared-with-the-development-flows-design)
5. [The pilot](#5-the-pilot)
6. [Alternatives considered](#6-alternatives-considered)
7. [Open questions](#7-open-questions)
8. [Technical details](#8-technical-details)

## 1. Order of work

1. **This branch** (`docs/secure-coding-standard`): the standard (`docs/SECURE-CODING.md`), the corrected section 1 of `docs/ASSURANCE.md`, and this plan. A reader from another AI vendor reads it.
2. **The operator's OK** on the standard and on this plan: given on 2026-10-29, for the pilot.
3. **The pilot** (section 5). Its authors get the standard from this branch, not from a skill, so the pilot needs no skill change.
4. **One pull request that wires the standard in** (section 2), only if the pilot meets its bar and no kill signal fired. It goes through the normal review cycle. If the pilot shows no difference, there is no such pull request: the team takes the smaller alternative in section 6 instead.
5. **Review** of the standard by 2027-04-28, or earlier on the triggers in its section 10.

The standard adds no approval at any step. The rule conflict that the challenge named is settled first, on this branch: `docs/ASSURANCE.md` section 1 now follows the definition of done and `CONTRIBUTING.md`, and marks the standard as proposed.

## 2. Where the standard would be loaded

**Every route that writes code loads the rules first.** The standard applies to every change to code, tests included, and to text that agents act on, such as skills and runbooks (its section 1). So every skill whose output is such a change points to the rules in its *first* step, before anything is written; a pointer only at handoff, or only in a lead's brief, is too late for a skill that is used on its own. Skills that only read or plan get no pointer. A skill that writes documentation gets a conditional one: when the text it writes directs what an agent does or may do (a skill, a runbook, an agent brief), its first step points to the rules, whoever started it; when the text only describes, nothing changes.

**One source, a general part any project can use, and agenttalk's own part.** The standard has two parts. Part 1, between two marker lines, holds the general rules: the three steps, the ten principles (S1 to S10), agent-written code and the examples. It names no project, path or tool of agenttalk's, so it follows the playbook rule that skills and shared texts stay universal (`docs/playbook/build-and-test.md`, B10). Part 2 holds what is agenttalk's own: its trust boundaries, its rules under the principles, where the lines go here, the existing checks and the framework map. The skills carry only part 1. agenttalk's builders get part 2 through their brief (the lead's row below), because a builder who sees only part 1 misses rules such as agent-name validation and the no-runtime-dependency rule.

**A pinned version.** A task names the version it works under (`secure-coding@1`), the way it already names its definition-of-done profile. A later version then never changes what work already started owes. The pilot is pinned to version 1 as merged: changes made before the standard's first merge stay version 1. This follows the team's lesson that obligations belong on the work's own dispatch record (`design.pin-obligations-on-the-immutable-work-record`).

| Entry point | File | Who uses it | Proposed change |
|---|---|---|---|
| A shipped copy of part 1 | new: `src/agenttalk/skills/devkit/_shared/references/secure-coding.md` | anyone who installs the skills with `agenttalk install-skills`, in any project | the text between the marker lines of `docs/SECURE-CODING.md`, under a version line of its own. Two repository tests: the body of the copy is identical to the marked text (one marker pair, in order, not empty), and the marked text contains none of a list of agenttalk-specific words (its name, its state folder, its source paths, its bus terms). The adoption change also checks that the installed copy can be read. |
| craft-code | `src/agenttalk/skills/devkit/craft-code/SKILL.md` | builders of production code | BEFORE: write the security note (step 1). WHILE: choose protections from S1 to S10 (step 2). AFTER: add the security lines to the handoff (step 3). Each item points at the shared copy. |
| refactor-code | `src/agenttalk/skills/devkit/refactor-code/SKILL.md` | builders of refactors | first step: the note, like any change. A refactor that moves, merges or simplifies code at a boundary (a path check, a permission check) names that boundary, and its handoff shows that the protection still holds, with the same check before and after. Only a refactor that touches no boundary writes `no trust boundary`. |
| fix-ci | `src/agenttalk/skills/devkit/fix-ci/SKILL.md` | builders of fixes for red checks | first step: the note, for every fix that changes code or tests, not only workflows. A fix that changes a workflow, a scanner setting or a dependency also checks S7 and S10. |
| test-coverage, tester-qa, test-security, test-integration, test-performance | each skill's `SKILL.md` | whoever writes tests, test harnesses or benchmarks | first step: the note. For most tests it is one line, `no trust boundary`; a test or fixture that starts processes, writes outside its own temporary folder, uses the network or handles a secret names that boundary. test-security also names the principle (`S<n>`) in its abuse-case evidence, so that handoff lines can cite those checks. |
| test-docs | `src/agenttalk/skills/devkit/test-docs/SKILL.md` | whoever checks documentation by running it | the same first-step pointer, used only when it adds check code to the repository. |
| write-docs | `src/agenttalk/skills/devkit/write-docs/SKILL.md` | whoever writes or changes a page, including skills, runbooks and briefs that agents follow | a conditional item in its first step (CLASSIFY): when the page directs what an agent does or may do, write the security note before writing the page (standard section 1). The pointer sits in the skill itself, so it works without a lead's brief. A page that only describes needs nothing. |
| definition-of-done | `src/agenttalk/skills/devkit/definition-of-done/SKILL.md` | everyone at handoff | one sentence under "Where the record goes": the security lines go in `evidence`, the commands in `tests_executed`, the open lines in `residual_risk`. No new field and no new profile row. |
| review-code | `src/agenttalk/skills/devkit/review-code/SKILL.md` and `references/security.md` | every reader | the security pass also checks the builder's lines (the list "What the reader checks" in step 3), and findings name the principle they break. The existing OWASP-based checklist stays as it is. |
| The lead's skill, and with it agenttalk's own part | `src/agenttalk/skills/claude/agenttalk.lead.md` and `src/agenttalk/skills/codex/agenttalk-lead/` | the lead, for every agenttalk builder | a build, fix or test brief names `secure-coding@1` next to the done profile, and tells the builder to read both parts before coding: part 1 for the rules, part 2 (`docs/SECURE-CODING.md`, sections 7 and 8) for agenttalk's own boundaries and rules. A read brief asks the reader to check the security lines against both parts. A brief to a seat that has no skills carries both texts. Before adoption, the routes listed under "Checked before adoption" below are checked. |
| The procedures | `docs/ROLES-SKILLS-PROCEDURES.md`, sections 4.1 and 4.2 | the team | one sentence each: the brief pins the version and points at both parts; the reader checks the lines. |
| The contributor guide | `CONTRIBUTING.md`, "Before you send a pull request" and "Proposing a change" | outside contributors | one bullet each: for a change to code or tests, read the standard's section "If you are sending one pull request" (linked by its web address), put the three-line note in the issue, and add the security lines under "How you checked it". |
| The pull request template | `.github/pull_request_template.md` | everyone who opens a pull request | widen the existing prompt "How you checked it" with "(for code: the security lines, see SECURE-CODING.md)". It stays four prompts. |

**Left out on purpose:** the review lenses other than `review-code` (`review-failure-injection` already covers what S7 and S8 ask), `qa-strategy` (it plans and writes no code; its risk list can feed a builder's note), `assurance-scan` (it runs the existing scanners), `write-for-humans` (release notes, pull request descriptions and status reports direct no agent), `review-docs` (it reads), and the README.

**Checked before adoption, route by route.** The adoption change shows, for each route, that the rules arrive before anything is written:

- **An installed skill used on its own:** it brings part 1; the brief must bring part 2.
- **A seat on the paid gateway:** its brief must bring both parts.
- **One standalone runbook edit:** an agent that uses `write-docs` alone, with no lead's brief, to change `docs/ops/scratch-hygiene.md` (a runbook that agents follow) reaches the security note in its first step.

**A known gap:** seats on the paid gateway do not see the shipped skills (issue #468). Until that is fixed they get both parts only through the text of their brief, as the lead's row says.

## 3. What a contributor from a fork sees

- **What they read:** the standard's short section "If you are sending one pull request", reached from `CONTRIBUTING.md` (by its web address) and the pull request template. It holds the two complete examples (with and without outside input) and says what they run. Both are public on GitHub. They need no bus, no agenttalk installation beyond the development install they already make, no private tool and no extra approval.
- **What they write:** the three-line note in the issue they open for a larger change (or in a draft pull request), and the security lines under "How you checked it". A change with no outside input needs one line.
- **What they run, and what CI runs:** they run the tests their lines name, with pytest, which the development install already provides. CI picks the checks for the pull request: the full development gate with the scanners, or the lighter documentation path, as `CONTRIBUTING.md` ("Which checks a change gets") explains; CodeQL runs on every pull request. The maintainers run the client-reference check that a fork cannot run.
- **Who checks it:** the maintainers' reader who reads every pull request. If the lines are missing, the reader asks for them, as for any missing "How you checked it"; it is not a new refusal.
- **What does not change for them:** the client-reference check and its fork route. The standard does not depend on the team's private knowledge store.

## 4. Evidence wording, shared with the development flows design

The development flows design (`docs/DESIGN-dev-flows.md` on the branch `docs/dev-flows-design`, not merged) proposes handoff lines of the form `criterion <number>: executed <check> -> pass <details>`, `criterion <number>: executed <check> -> fail <details>` and `criterion <number>: open - <reason>`, and reader lines of the form `criterion <number>: checked <how>`. The standard uses the same grammar with `security S<n>` in place of `criterion <number>`, so one handoff can carry both kinds of line, and a later parser can read both. This does not wait for that design to be built.

Four differences are on purpose:

- **"Does not apply" is the builder's claim here.** The standard has `security S<n>: does not apply - <reason>`, a claim the reader checks. In the development flows design, and in the definition of done, a check is made "not applicable" at dispatch instead. The two do not conflict: a principle that does not apply is the result of the builder's analysis, not a check waived.
- **"Checked by reading" names a promise no test can show.** The standard allows `security S<n>: checked by reading - <reason>` only for the promises its section 3 marks that way (constant-time comparison, strong randomness, a package's authenticity). The reader checks the reason.
- **A failing check is handed over as open.** The standard lists `open` and leaves out `-> fail`; a builder whose protection check fails hands it over as `open`, with the failing output.
- **No refusal in version 1.** A principle is not an acceptance criterion, and version 1 adds no refusal at handoff or at acceptance (the challenge kept enforcement out of the first delivery). If a later version adds one, it refuses only what a handoff itself contradicts, such as an `executed` line that names no command, or a line about a commit other than the one handed over (the team's lesson `design.handoff-refusals-refuse-only-self-contradiction`).

## 5. The pilot

**A pilot, not a trial.** Two pairs of runs and one real change are too few to measure an effect. They can show whether the standard is workable and whether it changes anything at all in a few cases, and that is all the result will claim: a sign, not proof.

**What agents cannot show.** Every run is made by an AI agent. The pilot cannot show what a person who sends a single pull request would do with the page; it shows only what agents do with it.

**Goal.** Before the standard is wired in, find out whether it changes what is built and checked, what it costs the author and the reader, and whether someone with only public text can follow it. The pilot can fail, and a result of "no difference" stops the rollout.

**Observed order, not claimed order.** In every run the note and the code are two separate dispatches. The first task gives the change and asks for the note only, with no code written; the lead records the reply and checks that the author's working copy has no change at that point. Only then does the lead send the second task, to build. The order is then shown by two dispatches the lead made, not by a time the author wrote down. A run whose order was not observed this way counts as "order not shown", and does not count towards the bar.

**T1, a real Python change in agenttalk.** The recommended candidate is issue #427: under the test-and-probe store fence, taking a backup without `AGENTTALK_RECOVERY_DIR` writes outside the fence, into the real per-user recovery folder. It has a clear trust boundary (the fence), a plausible misuse (a test leaves files outside its own temporary folder), and a safe behaviour a test can check (refuse plainly, or write only inside the fence). A fallback is issue #571 (`broadcast --resume` accepts an empty `-m` and `--file`), which is a plain input-validation case. Both were still open on 2026-10-29; the lead checks again before choosing. A maintainer builds it with the standard, a reader from another vendor reads it in the normal review cycle, and it merges through the normal gate if it passes. With no run without the standard to compare against, T1 cannot show that the standard made a difference, so it cannot meet the bar on its own.

**T2, a fresh task, built with and without the standard.**

- **A fresh task that neither side has seen.** The standard's own examples (a file read by name, a git call, a key comparison, a settings loader, a report query) are not used, because a run that copies them would only show copying. The lead chooses the task when the pilot starts and writes it only into the pilot's briefs, never into the standard or this plan. It touches at least three of the principles, is small enough for about thirty minutes of an agent's work, and is written in Java, so that the standard is also tried on a language other than Python. The same task serves both pairs. The lead names the JDK in the brief.
- **Common requirements.** Before any run, the lead writes the task's functional acceptance tests. Each run's result must pass them; a run that fails them does not count.
- **One model per pair, balanced across pairs.** Both runs of a pair use the same model, the same tool access and the same time budget, by two seats that have not seen the task. The two pairs use models from two different vendors when both are available. In one pair the run without the standard goes first; in the other, the run with the standard goes first.
- **Equal briefs, recorded.** The two briefs of a pair are the same text, with one difference: the run with the standard gets the public page `docs/SECURE-CODING.md` and a first step asking for its security note; the run without it gets the same first step asking for "a three-line plan of the change". Nothing else differs: no better wording, no extra hints. The lead records exactly what each author received.
- **The outsider's path.** Both authors get only what a fork contributor sees: the public text of `CONTRIBUTING.md` and the pull request template, plus, for the run with the standard, `docs/SECURE-CODING.md`. No skills, no bus commands beyond receiving and answering the two tasks, no knowledge store.
- **No author validates the standard's examples.** The standard's Java fragments are compiled and tested by a separate seat that makes no pilot run, after all pilot results are frozen. No author, in either arm, is given that work, so the run without the standard never sees the protections being measured.
- **A blind comparison of gains and losses.** The reader compares the code and the tests of the two runs of each pair, without the handoff text and without being told which run had the standard. For each run, the reader lists the protections built and the checks that fail without them. Then the reader lists every protection present in one run and missing from the other, in both directions: gains (only in the run with the standard) and losses (only in the run without it).

**What is measured, per run.** The team's lesson `process.agent-seat-timing-is-not-human-effort` applies: an agent's seconds are not a person's minutes, so the result reports agent-seat time and counts, and leaves the question of a person's time to the operator.

- **Author:** seconds spent on the note step, and seconds spent on the build step; the number of note lines, handoff lines, tests and commands.
- **Reader:** seconds spent on the blind comparison and on checking the handoff lines; how many lines were checked, how many held, how many failed, and any boundary the note missed.
- **Order:** whether the note step was observed before the build step, as above.
- **Effect:** the gains and the losses the blind comparison found, each confirmed by running the check on the run's final code. A confirmed gain is reported as a tentative sign that the standard helped in this pilot, never as proof that it causes the difference.

**The bar for going ahead.** All of these must hold:

1. Every counted T2 run passes the task's acceptance tests.
2. In each of the two T2 pairs, the blind comparison finds at least one gain: a protection, with a check that fails without it, in the run with the standard and missing from the run without it, which the reader confirms by running that check.
3. A gain never hides a loss. Every protection that the run with the standard lacks and the run without it has is reported next to the gains, and it is explained and resolved before that pair counts as a success.
4. Every counted run had its order observed (note step first, no code in the working copy at that point).
5. The T2 runs with the standard were finished with public text only.
6. In T1, the reader confirms on the exact head at least one concrete protection that the note or a principle led to, with an executed check, and checks every handoff line. A count of lines read is not enough on its own.
7. No text produced in the pilot claims compliance with a framework.
8. No approval was added: the reader was the normal reader.
9. The pilot stays within about four hours of seat time in total, plus the separate validation of the Java fragments; the lead stops at that limit and reports what was done. The measured cost goes to the operator, who decides whether it is acceptable.

**When the pilot finds no gain.** The rollout stops if either T2 pair shows no confirmed gain, or if a loss in the run with the standard is left unresolved: no wiring pull request, and the team takes the smaller alternative in section 6. Every gain and every loss is reported, including a loss that is later explained and resolved. The result describes what was seen, case by case, in plain words:

- **No gain and no loss in a pair:** "In this pair, the standard did not change which protections were built or tested."
- **A loss and no gain:** "In this pair, the standard changed the result for the worse: the run with it lacked <protection>, and it gained nothing."
- **Mixed, within a pair:** "In this pair, the run with the standard gained <protection> and lost <protection>."
- **A gain in one pair only:** "The standard gained <protection> in one pair and nothing in the other." This is a weak sign, and it stops the rollout like no gain.

**Kill signals** (the challenge's, with how each is detected). If one fires, the wiring pauses, the lead reports it, and the team falls back to the smaller alternative in section 6.

| Signal | How it shows |
|---|---|
| Only after-the-fact attestations | a run whose note step was not observed before its build step, or a line that claims an executed check (`executed ... -> pass`) when no such check ran on that commit. A `checked by reading` or `does not apply` line, the forms the standard allows, does not fire this signal when the reader confirms its reason. A late note counts against the pilot even when it says it is late |
| Private tooling needed by outsiders | a T2 run with the standard could not be finished with public text only |
| A claim of whole-framework compliance | any text in the standard, the pilot or the wiring that says "compliant" or "certified" |
| Approvals added without settling the rule conflict | any new sign-off, or the wiring proposed before `docs/ASSURANCE.md` section 1 is corrected |

**Output.** A one-page result in the pilot's scratch folder, and lessons in the knowledge store for what the pilot taught. The result states its limits: two pairs, one fresh task and one real change, all by agents, show an effect in this pilot, not a general one, and say nothing about a person sending one pull request. The T2 Java code is deleted once the lead accepts the result.

## 6. Alternatives considered

- **The smaller path the challenge named:** turn the review skill's security reference into prompts for builders, plus a small framework map, without the three steps. It costs less, but it has no before step, so it cannot tell planned protections from paperwork written afterwards. It is the fallback if the pilot shows no difference or a kill signal fires.
- **A security section in every pull request, without a before step:** rejected. It invites exactly the after-the-fact attestation the kill signal names.
- **New scanners or hosted services** (for example a second dependency scanner, or one for the development tools): out of the first delivery, as the challenge required. The existing checks and their gaps are listed in the standard's section 8; a new one comes only for a risk shown to be uncovered.
- **Adopting ASVS in full, or claiming SSDF compliance:** rejected. ASVS is written for web applications; agenttalk is mostly a command-line tool, and the standard selects only the chapters that apply.
- **The whole standard in the skills:** rejected. Part 2 is agenttalk's own, and the playbook keeps shared skills universal (B10). **The standard only in `docs/`:** rejected too, because people who install the skills have no `docs/` folder. Hence part 1 in the skills, checked against the page by a test, and part 2 through agenttalk's briefs.
- **A trial with the standard's own example as T2:** replaced by the fresh task in section 5, because a run that copies the page's example measures copying, not the standard.

## 7. Open questions

1. **The shipped copy:** keep the page as the source with part 1 copied into the skills and checked by tests (recommended), or make the skill reference the source and have the page include it by a link? **Decided on 2026-10-30 by the lead:** the page stays the source, part 1 is copied into the skills, and two repository tests check the copy (`tests/test_secure_coding_copy.py`).
2. **Tests-only changes:** should they need the note? The standard says yes; usually it is the one line "no trust boundary". **Decided on 2026-10-30 by the lead:** yes; the test skills point to the note in their first step.
3. **The review date:** decided on 2026-10-29: 28 April 2027, or when NIST finalises its new SSDF version, whichever comes first.
4. **The Cyber Resilience Act:** decided on 2026-10-29: the maintainer confirmed the facts behind the team's reading (no paid support, no money made from agenttalk through other services, no company backing it as a steward); see the standard's section 9.7.
5. **T1:** issue #427 (recommended) or #571?
6. **T2:** which fresh task, and which JDK. The lead chooses both when the pilot starts.
7. **Gateway seats:** wait for issue #468, or add both parts to their brief text meanwhile? **Decided on 2026-10-30 by the lead:** both parts go in their brief text until #468 is fixed (the lead's skill says so).

## 8. Technical details

- **Read at master `eebe3460`, rechecked at `12418bb6` for this revision:**
  - the documents: `docs/ASSURANCE.md`, `docs/DESIGN.md` (section 6), `CONTRIBUTING.md` (including #573's change to how tests are run), `SECURITY.md`, `docs/ROLES-SKILLS-PROCEDURES.md` (section 1 and sections 4.1 to 4.4), `docs/playbook/build-and-test.md` (B10);
  - the gate and its settings: `dev-gate.json`, `src/agenttalk/dev_gate.py` (`_runtime_dependency_snapshot`, `_pip_audit_check`, `isolated_tool_argv` and its callers), `pyproject.toml` (ruff settings, source package list);
  - the repository's GitHub files: `.github/workflows/tests.yml` and `security.yml`, `.github/pull_request_template.md`, `.github/dependabot.yml`, `.semgrep/agenttalk.yml`;
  - the skills: `craft-code`, `definition-of-done`, `review-code` (with `references/security.md`), `test-security` and `_shared/references/evidence.md`.
- **The development flows design** was read on the branch `origin/docs/dev-flows-design`, file `docs/DESIGN-dev-flows.md`, sections 6.2 and 6.3.
- **The inputs this revision answers:**
  - the challenge's verdict, in the lead's briefs folder (`challenge-secure-coding-standard-verdict.txt`, request ch-651fb888-3d04-48ed-bf23-d6b81a4faff3);
  - the reads tk-1297b29787e4 and tk-d62bcfb4bff4 (R1 to R5);
  - the second team's notes of 2026-10-29.
- **Framework pages** were read on 2026-10-28; the standard's section 9 lists each one with its address.
