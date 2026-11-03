---
name: definition-of-done
description: >-
  State what "done" means for a task, check it with executed evidence, and record it
  in one short done record, before you hand work over or call it finished. One small
  base for every task (scope, exact artifact, executed checks, limits, next owner),
  plus stricter profiles by kind of work and risk (review, QA and test, documentation,
  investigation, bug fix, refactor, CI fix, feature or greenfield slice, greenfield
  project or release, migration slice, whole migration). Use at assignment (to pin the profile) and at
  handoff (to write the record). Do NOT use it to accept a product (use the close
  flow), to decide a release (use review-release-readiness), or as an extra review
  round.
reviewed-against: "0.102"
category: assurance
evidence-profile:
  - production-handoff
  - review-result
  - qa-result
  - planning-artifact
---

# definition-of-done

In plain words: "done" means three different things, and teams lose time when one is
mistaken for another. This skill is about the first one only: **the assigned task is
finished**. It gives every task the same short list of things to show, and asks for a
little more when the work is bigger or riskier. It does not accept the product and it does
not allow a release; other rules do that.

## QUICK START
1. At assignment, the lead names the profile (table below) and pins the policy text in the
   task (PINNING), for example `done-profile: bug-fix, risk: normal, policy:
   definition-of-done@1 tree <id>`. The profile does not change while the task runs.
2. Work. Keep a list of the checks the profile asks for.
3. At handoff, write the done record (next section): five lines, plus one line for each
   exception. Put it in the evidence profile that fits the kind of task (WHERE THE RECORD GOES).
4. Hand it to the next owner named in the record (a seat, not only a kind of review). The record says what is finished; it
   never says "accepted" or "ready to release".
5. A check that could not run is not "not applicable". It stays open (EXCEPTIONS).

## The three things kept apart
| Question | Who answers it | Where it lives |
|---|---|---|
| Is the assigned task finished? | The person who did it, with evidence, checked by someone else where the profile says so | this skill |
| Is the product accepted? | The lead, from reviews and the close | the `close` flow and its definition-of-done dimensions (assurance, coverage, knowledge, acceptance) |
| May it be released? | The lead, on evidence for the actual revision | `review-release-readiness` |

A review-only or investigation task can be finished with a negative result. It never needs
product acceptance or release checks to be finished. This skill adds no new verdict, no
extra review round and no approval step.

## The base: the done record (every task)
Write these five lines at handoff. Say each in plain words first.
1. **Scope and outcome.** What was asked and what came out of it, in two sentences. Name
   what was left out.
2. **Artifact.** The exact thing delivered: the repository and full revision, or the file
   and its hash. Every check below says which artifact it ran on. For comparisons, give the
   role of each build: **original** (the starting point), **candidate** (what is
   delivered), **derived** (made from the candidate for a check, for example a test build).
   Evidence about a different build does not count.
3. **Checks.** One row per check the profile asks for: the check, the exact command or run
   id, the result. "Referenced" is not "executed": a test you only read is listed as
   referenced.
4. **Limits and open items.** Everything not done, not run or not known, each with a reason
   and an owner. An empty list is a claim: write "none" only if it is true.
5. **Next owner.** Who acts next and on what (review, acceptance, fix, release decision).

## WHERE THE RECORD GOES: one record, in the profile that fits
The done record is not a second report. Its five lines are carried by the existing evidence
profile for the kind of task, using that profile's field names (rules and which fields the
bus checks: `../_shared/references/evidence.md`). Never append a separate production record
to a task whose profile is another one.

| Task kind (profile) | Evidence profile | Scope and outcome | Artifact | Checks | Limits | Next owner |
|---|---|---|---|---|---|---|
| `documentation`, `bug-fix`, `refactor`, `ci-fix`, `feature`, `project-or-release`, `migration-slice`, `migration` (the doer's handoff) | `production-handoff` | `summary` | `changed_files`, `base_ref`, `head_ref` | `tests_referenced`, `tests_executed`, `evidence` | `residual_risk` | `required_review_lenses` (the lenses) and the `next-owner` entry in `evidence` (the seat) |
| `review-only` (and the reader of any other task) | `review-result` | `scope`, `status` | `reviewed_ref` | `tests_referenced`, `tests_executed`, `evidence` | `residual_risk` (and `risk_class`, `release_blocker`) | the `next-owner` entry in `evidence` |
| `qa-test` (QA or test evidence for a change; the tester-qa skill) | `qa-result` | `scope`, `status` | `reviewed_ref` | `tests_referenced`, `tests_executed`, `evidence` | `residual_risk` (and `risk_class`, `release_blocker`) | the `next-owner` entry in `evidence` |
| `investigation` | `planning-artifact` | `scope`, `decision` or `proposal` | `artifact_type`, and in `evidence` the exact revision or documents examined | `evidence` | `assumptions`, `risks`, `open_questions` | `required_reviews` (the reviews) and the `next-owner` entry in `evidence` (the seat) |

**The next owner is a seat and an action, never only a lens.** `required_review_lenses` and
`required_reviews` say which kinds of review are needed; they cannot say who acts. In every
profile, put one entry in `evidence` of the form `next-owner: <seat> - <action>` (for
example `next-owner: codex-reviewer - security read of the head`), and when the profile has
a lens list, write each lens as `<lens>: <seat>`. No new field names are needed.

**A task kind this table does not list** (a new kind, or a mixed one) is not skipped: use
the base five lines in the profile of the kind it is closest to, and say under limits which
kind you chose and why. Never leave the handoff without a done record.

**Security lines.** When the work has a security note, its lines (`security-note:` and
each `security S<n>:`; [secure coding rules](../_shared/references/secure-coding.md), step
3) go in `evidence`, the commands they name in `tests_executed`, and every `open` line in
`residual_risk`. No new field and no new profile row.

The in-skill stub below is the `production-handoff` profile, the one most tasks use. A
review-only task emits `review-result` (required fields: `risk_class`, `release_blocker`,
`tests_referenced`, `tests_executed`, `residual_risk`, `evidence`, `status`, `reviewed_ref`,
`scope`). A QA or test task emits `qa-result` (the same required fields as `review-result`).
An investigation emits `planning-artifact` (required fields: `artifact_type`,
`scope`, `decision` or `proposal`, `assumptions`, `alternatives`, `risks`,
`required_reviews`, `open_questions`, `evidence`). A repository test keeps this table,
the frontmatter and `evidence.md` in step.

## Profiles: pick by kind of work, raise by risk
Each profile is the base plus the extra rows shown. A profile never removes a base row. A
task made of several kinds of work meets the rows of each part: a feature that also ships a
manual page meets the documentation rows for that page.

| Profile | Use it for | Extra rows |
|---|---|---|
| `review-only` | A review or audit that reads, and maybe runs, someone else's work | The reviewed revision is named in full. What was read and what was run are listed apart. A negative verdict is a finished task. No release or acceptance rows. |
| `documentation` | A README, guide, manual, design or policy text | Every factual claim is checked against the code or marked unverified. Every command and example was run, or is marked not run with the reason. **A procedure (steps to follow in order, such as an install or upgrade guide) was followed in order, from its stated starting state, by a fresh run or by someone who did not write it; separate commands that each pass do not count.** Version pins and counts agree with the release. The audience is named. |
| `investigation` | A question to answer, with no change to deliver | The question, the method, what was ruled out, how sure the answer is and what would change it, with pointers to the evidence. A next step is recommended, not done. |
| `qa-test` | QA or test evidence for a change (tests designed, run and triaged) | What was executed and what was only read are listed apart. The tested revision is named in full. A failing check names the command and output, or the exact gap, and says whether it is a real defect, a test bug, a flaky test or the environment. A green claim needs a run id or command. |
| `bug-fix` | A defect to remove | The defect is shown on the original revision. A test fails without the fix and passes with it. The test uses the real behaviour, not a stand-in. Nearby cases are named. The affected suites ran. |
| `refactor` | Restructuring that keeps behaviour | The behaviour that must not change is named. The same tests ran before and after, with the same results, test by test. No test was weakened. Anything that does change is listed. |
| `ci-fix` | A red check to turn green | The failing command and its log are quoted. The root cause is classified (a defect, a test bug, a flaky test, the environment). The same command now passes, run on the pushed revision. Nothing was skipped or weakened to get the green. |
| `feature` | A feature or a slice of a new project | The acceptance criteria are listed. Each behaviour has a test, including the failure paths and anything that must survive an interruption or a restart. User-visible change is in the changelog. No new warnings from the project's checks. The independent reader is named as the next owner (see "Rows that need someone else's read"). |
| `project-or-release` | A whole new project, or a release candidate | Exit criteria agreed at the start. A clean checkout builds, and the built artifact installs and runs. Every place that names the version agrees. The summary states each guarantee exactly, with its exceptions. Known limits are listed. CI ran on this revision (run id). Rollback is stated. |
| `migration-slice` | One piece of a platform or data migration | See `references/migration.md`, part 1. |
| `migration` | A whole migration | See `references/migration.md`, part 2. |

**Rows that need someone else's read.** The builder's done record is the builder's handoff:
it is written when the build is finished and pushed, and it lists the independent read
(a different seat for a feature, a different vendor for `risk: high`, the independent review
row of a migration) as an open item with the reader as next owner. It is not a precondition of
the builder's `done`. The read belongs to the acceptance stage, after the build task closes
(`docs/ROLES-SKILLS-PROCEDURES.md`, the review cycle: build, then the lead creates the cold
read). The feature or slice is accepted only when that read is in, which is the lead's
decision in the close flow, not this skill's.

**Risk.** Add a risk word to the profile: `normal` or `high`. High means money, security,
data that cannot be recovered, many users, or an action that cannot be undone. High adds,
**for the work product**: a reader from a different vendor than the doer (arranged as the
next owner, read at the acceptance stage); the undo or
rollback path stated and tried where it can be; no exception without the exception
authority's written decision.

**The risk rule never creates a review of a review.** It applies to the task whose output
is the product (the change, the document, the migration). The completion of a `review-only`
or `investigation` task creates no further review obligation: its record is finished when
the read is finished, whatever its verdict, and the lead alone decides what follows.
Example, a security review that finishes with FIX: the reviewer's task is `review-only`
(`risk: high`, because the product is security-relevant). It is finished when the verdict
and findings are written with the reviewed revision and what was run. The reviewer needs no
second reader. The `review-result` says status rejected, lists the findings in
`residual_risk`, and names the author as next owner. The author's fix is a new `bug-fix`
task with `risk: high`, which does get a different-vendor reader; that reader's own read
is again `review-only` and ends the chain.

## EXCEPTIONS
- **Not applicable** is decided at assignment, in the task text, with the reason, and
  accepted by a named checker who is not the doer. Deciding it afterwards, because the
  check is inconvenient, is a scope reduction (below).
- **A required check that could not run stays open.** The record says "not run", why, and
  what that leaves unknown. It becomes closed only through a **recorded scope reduction**:
  the exception authority writes what is dropped, why, who accepts the risk and until when.
  A reason alone never waives a check.
- **Never waivable:** the exact artifact is named; secrets are referred to by location and
  never copied; the record is honest (nothing unfinished is called finished).
- **Who:** the project names a **maintainer** of this policy and an **exception authority**
  in its overlay. Until it does, the lead is both, and the operator decides when the lead
  is the doer.

## PINNING: which text, exactly
The label `definition-of-done@1` names a line of policy text; it does not by itself say
which bytes a seat reads. Installing the skills copies into one directory per seat and a
later install can replace the text, so the label alone would let two seats, or one seat on
two days, work to different rules. The pin is therefore a **source identity**, recorded in
the task when it is assigned:
- **The skill:** `policy: definition-of-done@<label> tree <id> evidence <blob> (agenttalk <release>)`,
  where `<id>` is the git tree id of the directory `src/agenttalk/skills/devkit/definition-of-done`
  at that release tag (`git rev-parse <tag>:src/agenttalk/skills/devkit/definition-of-done`).
  It covers `SKILL.md` and `references/`. It is retrievable from the public repository for
  as long as the tag exists: `git ls-tree -r <id>` lists each file's blob id and
  `git show <blob id>` prints it, so a task assigned under an older text can still be read
  against that text.
- **The shared evidence rules:** this skill makes `../_shared/references/evidence.md`
  normative for field names, so the pin includes it: `<blob>` is the blob id of
  `src/agenttalk/skills/devkit/_shared/references/evidence.md` at the same tag
  (`git rev-parse <tag>:src/agenttalk/skills/devkit/_shared/references/evidence.md`). If it
  changes while this directory does not, the pin changes and the label is reviewed.
  (`plain-language.md` is style only and is not pinned.)
- **The overlay:** `overlay: <repository> <path> blob <id>` or a commit id. The overlay file
  never states its own identity; the task does. A change to the overlay is a new identity.
- **Checking it:** the doer and the checker compare the identity in the task with the files
  they actually read. On a seat, `git hash-object --no-filters <installed file>` for each
  file must equal the blob ids in `git ls-tree -r <id>`, and for the shared evidence file the
  `evidence` blob in the pin. The installer does not do this
  for you.
- **If it cannot be resolved** (no such tree, a blob that does not match, an overlay not
  committed): say so under limits ("policy text not verified"). The task is not finished
  until the identity is resolved or the exception authority records a scope reduction.
  Never fall back silently to whatever text is installed.
- **Change.** Any edit of the skill directory changes its tree id. The maintainer bumps the
  label when the edit changes what is asked of a task (not for a typo). A new label
  applies to tasks assigned after it; a running task keeps its pin. An urgent safety change
  may be applied to a running task only by the maintainer, in writing, with the reason.
- **Lessons change it only after review.** A lesson never edits the policy by itself. The
  maintainer reads the proposed change, decides, and bumps the label. A proposal that adds
  a row to every task needs a reason that is not "it happened once".
- **Project overlays.** A project adds what is specific to it in a short overlay file it
  owns (`references/project-overlay.md` shows the shape): isolated builds, branch rules, a
  named mutation tool, hosts that must never be touched, vendor rules, a fixed number of
  review rounds. An overlay can add rows and name people. It cannot remove a base row or a
  profile row.

## Reuse
- Evidence fields and rules: `../_shared/references/evidence.md`. Do not invent new field
  names.
- Acceptance and close: the `close` command and its definition-of-done dimensions. This
  skill does not repeat them.
- Release: `review-release-readiness`.
- Plain writing: `../_shared/references/plain-language.md`.

## Technical detail
- The done record is text in the handoff or reply. Nothing in the bus enforces it today
  (SKILL-POLICY). The `close` flow is the enforced part.
- A check's "result" is the exit code and the counts, not "passed".
- The base record is written once; where two rows overlap, the stricter row wins.
- Replay evidence for this version, with one entry per counted finding, is in the public
  repository (it is not installed with the skill, so these are links, not local paths):
  <https://github.com/zoolok17/agenttalk/blob/master/docs/research/DOD-REPLAY-2026-10.md> and
  <https://github.com/zoolok17/agenttalk/blob/master/docs/research/DOD-REPLAY-2026-10-ledger.csv>.

## Evidence

Emit the `production-handoff` profile (full rules + bus-validated vs skill-policy: ../_shared/references/evidence.md).

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
