# agenttalk - Product Roadmap & Feasibility

**Status:** Official · **Owner:** lead (operator-facing) · **Last updated:** 2026-10-09 (UTC)
**Audience:** maintainers, operators, and agents deciding what to build next.
**Horizon:** the plan the operator approved on 2026-10-07 for the next 4-6 weeks (§6): weeks 1-2 are committed, the team re-plans at the end of week 2, and weeks 5-6 are optional. Older items the plan does not carry are in a labelled "later" tier (§8). §5 records what v0.94.0 shipped and is kept for the record.
**Current shipped baseline:** v0.99.0 (2026-10-08). `CHANGELOG.md` remains the release-history source of truth.

**Platform requirement:** agenttalk must run on **Windows, macOS, and Linux**. The Python core (bus, store, CLI, wrapper) is cross-platform and CI-tested on all three (Windows/macOS/Ubuntu × Python 3.10–3.13). The model gateway has a Windows scheduled-task backend and, since v0.91.0, a Linux systemd `--user` backend. **The supervisor is still the open platform gap:** it needs PowerShell Core 7+ and the Windows-only `Win32_Process`. A POSIX supervisor path is unbuilt (§8).

Companion docs:
- `docs/DESIGN.md` (why / architecture);
- `docs/ASSURANCE.md` (per-release attestation);
- `docs/ISSUES.md` (living tracker and known limitations);
- the work-board design (#207): `docs/DESIGN-work-board.md` on branch `design/work-board`, and the reducer's residual in `docs/STEP-WORK-BOARD.md` on branch `feat/work-board-b3a`. Both land on master with the board PRs;
- `docs/TEST-COVERAGE-REPORT.md`;
- `docs/DASHBOARD-CONTROL-PLANE-DESIGN-HISTORY.md`;
- `docs/ROADMAP-ARCHIVE-2026-06.md` (**archived**; do not plan from it);
- `CHANGELOG.md`.

**This file is the only current roadmap.** The per-release plans for v0.80.0–v0.83.0 that this file carried until 2026-08-03 are in git history.

---

## 1. Verdict

**Feasible if scoped as a local-first software delivery platform for trusted workspaces.**

The product should not promise that vague requirements become correct software without human judgment. The feasible claim is:

> agenttalk turns requirements into a disciplined, evidence-backed delivery workflow run by agent teams from more than one vendor: plan, challenge, isolate, build, test, review, accept, and release, with a human oracle for intent and waivers.

The human remains responsible for business intent, public contracts, migration risk, legal/security tolerance, and final release judgment. agenttalk is the workflow contract, the evidence spine, the local runtime and the operator control plane.

---

## 2. North Star

Build an **agentic software delivery platform** that a team can pick up for a greenfield or an existing project: provide requirements, and agents build the product by a professional software-development process. The primary use case since 2026-08 is **modernizing an existing codebase**. The platform should make the correct path the normal path:

1. Requirements become explicit specs, acceptance criteria, risks and non-goals.
2. Major work is **challenged before it starts** by an independent agent that may say "don't".
3. Work is split into bounded items with owners, paths, domains and isolated workspaces.
4. Agents produce code only inside recorded workspaces.
5. Quality checks and pinned third-party tools produce write-once, content-addressed evidence.
6. Independent reviewers, preferably from another vendor, read the real diff at the exact revision.
7. Gates, closes and acceptance compute HOLD/GO from typed evidence, not prose.
8. Operators can see what is moving, what is blocked, why, and what evidence is missing, without reading code.

**Positioning:** safer AI-assisted software delivery, not autonomous correctness. "Agents that tell you when not to build" is part of the product, not an add-on.

---

## 3. Product Shape

A thin substrate plus pluggable products with hard boundaries.

| Product | What it is | Today (v0.93.0 + master) |
|---|---|---|
| **A. Core Coordination Bus** | Roster, typed messages, threads, requests, work orders, review handoff, broadcast, transcripts. | Product-grade. `task`/`task-response` work orders (0.88). Validated work-item tags and reply verdicts (0.93). **Known scale limit:** one store-wide publication guard (§8). |
| **B. Native Work & Evidence Spine** | Work items, workspaces, evidence, review bindings, gate/check state, delivery lifecycle. | **In build as a derived work board (#207).** Cards are projected from bus events tagged with `work_item`/`stage`; there is no hand-kept state. Tags and verdicts shipped in 0.93; vendor records and a shared snapshot are on master; the reducer and feed are in review. |
| **C. Method Engine Adapter** | External planners create or link work; agenttalk owns execution evidence. | **Retired in its spec-kitty form** (operator decision, 2026-09-28; §8). The migration method becoming first-class is still open (#153). |
| **D. Craft Skill Pack** | Coding, review, test, QA, security, release and lead skills. | Useful and growing. Challenge skill (0.93); lead skills aligned with the work-item protocol (0.93); the assurance-scan and review lenses. |
| **E. Assurance & Release Governance** | Gates, closes, specialist sign-off, acceptance, scan evidence. | **Strong.** Cooperative acceptance passes on `close` (0.92). A pinned, offline-checked tool registry with `close acceptance preflight` (0.93). |
| **F. Knowledge & Codebase Memory** | Domains, pointer notes, lessons, onboarding runs, the comprehension plane. | Primitives exist. Comprehension producer slice 1, a static inventory plane (0.87). The per-project knowledge and per-seat memory design is drafted (§8); its measurement work orders are in the plan (§6.6). |
| **G. Operator Control Plane** | Local console, read views, attention/liveness, lead chat. | The console reports risk honestly (0.86). **Console v2 preview in review (#214).** The work board UI is next (§5). Plan review is on hold (#206). |
| **H. Execution Runtime** | Wrapper, supervisor, session continuity, dead-letter, lanes. | The wrapper is far more reliable (0.84–0.91): replies are owned by the wrapper, failures are loud, redelivery is honest. Store backup (0.89). **Dead seats are relaunched by hand**, because the supervisor is not run on the maintainers' fleet (§8). The POSIX gap remains. |
| **I. Delivery Workflows** | Greenfield build, existing-project change, legacy adoption, release. | Dogfooded end to end on a legacy-codebase modernization dogfood: two attempts, the second a blind rerun compared against the first. Not productized yet (#153). |
| **J. Model Gateway** *(optional)* | A local, watched gateway so a third-party open-weight model can run a seat. | Working. A spend ledger enforces a cutoff/soft-stop envelope, configurable at init (0.91), with Windows and Linux service backends (0.91). Reasoning is no longer re-sent as input (0.92). Field findings: #194. |

Key boundary, unchanged: **method engines plan; work items execute; evidence records facts; gates decide from evidence; humans own intent and waivers.**

---

## 4. Shipped Since v0.81.0

By theme. Versions are in brackets; details are in `CHANGELOG.md`.

- **A seat fails loudly and recovers honestly.**
  - An agent that cannot start says so, with a bounded remedy [0.82].
  - Auto-archive can no longer retire a live agent without proving it owns it [0.82].
  - A testable supervisor launch seam [0.83].
  - Wrapper-owned reply delivery [0.84].
  - Interruption-aware redelivery, never-started promotion and loud wrap refusals [0.85].
  - Refused replies leave a trace [0.88].
  - Interim progress notes that do not end a turn [0.88].
  - Wraps survive rate-limit spawn failures [0.88].
  - Task replies are no longer lost through the draft channel [0.91].
- **Fail closed on ambiguous input** [0.83]: the console Host check, signing that no longer fails open, and strict gate-state parsing.
- **Work orders the lead can trust** [0.88]: the `task`/`task-response` kind pair; a per-seat scratch root and janitor.
- **Store safety:** `agenttalk backup`, #156 increment 1 [0.89].
- **Comprehension:** the plane design frozen [0.86]; producer slice 1, a privacy-scoped static inventory [0.87]; a confidentiality sweep of tracked content [0.87].
- **The console reports risk honestly** [0.86]: gate and evidence wall, risk register, ownership map.
- **Model gateway:** a watched trial [0.88–0.90], a Linux backend and configurable spend envelope [0.91], envelope binding fixes [0.91.1], and reasoning not re-sent [0.92].
- **Acceptance of major work:** cooperative acceptance passes on `close` [0.92]; the pinned, offline-checked tool registry, preflight and schema-4 closes [0.93]; fewer git spawns in the acceptance suite [0.93].
- **Challenge before building** [0.93]: the `agenttalk.challenge` skill. It is mandatory for major work. A malformed, contaminated or late verdict counts as unassessed, never as proceed, and only the operator overrides replace/defer/stop.
- **Work board foundations** [0.93]: validated work-item tags, normalized reply verdicts, and supersession.
- **The first work board delivery** [0.94]: the reducer, the bounded feed and gate isolation, the shared needs-you projection, and the board view at `/v2#board`, inside the console v2 preview.
- **Seats survive store-lock contention** [0.94] (#218): phase-specific in-place retry that never re-drives a completed paid turn.

---

## 5. Shipped: the v0.94.0 Line (2026-09-28)

*Kept for the record. What v0.95.0, v0.96.0, v0.97.0, v0.98.0 and v0.99.0 shipped is in `CHANGELOG.md`.*

**Theme: a work board that moves itself, and seats that survive a busy store.** The first delivery of #207 and the console v2 preview it renders in both shipped in v0.94.0. The acceptance test is the operator's own walkthrough on live data (the script is in `docs/STEP-WORK-BOARD.md`); there is no scripted demo.

| Item | What | State (2026-09-29) |
|---|---|---|
| Board data layer | Vendor records and escalation links (B2b); a shared per-root envelope snapshot that includes compacted archives (B4a+B4s). | Shipped in 0.94.0 |
| Pure causal reducer (B3a+B3b) | Derives each card's stage from bus evidence, with a read-side audit. A stage the data cannot prove shows as unknown. | Shipped in 0.94.0 |
| Bounded feed + gate isolation (B6a) | Active cards plus 7 days of Done, capped and with overflow visible; board gates never block an ordinary merge check. | Shipped in 0.94.0 |
| Console v2 preview + board UI | `/v2` (#214), the shared needs-you projection (B7) and the board view (B8): GET only, text-only rendering, and stale or expired data dimmed as "last known". | Shipped in 0.94.0; the operator walkthrough is pending |
| Seats survive store-lock contention (#218) | Phase-specific in-place retry; no re-driven paid turn; corruption and ownership loss stay fatal. | Shipped in 0.94.0 |

**Fleet upgrade:** done on 2026-09-28. The seven Claude and Codex seats run the v0.94.0 runtime. The two gateway-backed seats stay on v0.91.1 for now: they use their own launcher, and the gateway upgrade is a separate decision.

**After the release, on master:** spec-kitty support removed entirely (§8), and the lead skill's assignment example now uses tagged `task` dispatches.

**Deferred board slices** (designed, not scheduled for 0.94.0):
- lead-authorized rescind (B2c);
- a disposable archive index and indexed hydration (B4b/B4c);
- explicit board-gate cleanup (B6b);
- a restricted local Git observer for integration evidence (B5);
- the read-only half of plan review, after the first delivery: the pinned document with copy-with-reference (D1), "changed since you looked" (D2), and finding marks once finding ids are stable (D3).

---

## 6. Next: the approved plan for weeks 1-6

**Approved by the operator on 2026-10-07. Status as of 2026-10-09.** The plan replaces the ranked list that stood here before. The older items it does not carry are in §8, each with a pointer.

### 6.1 In plain words

Three things slow the team down today:
- **Windows test runs take 3-5 hours.** Every code merge waits for them (a merge that changes only documents does not, since #405).
- **Seats spend turns on nothing, or on friction.** The second team's clock wakes each seat 48 times a day, mostly for nothing, and each wake-up is a whole model turn that re-reads the seat's conversation. Replies get refused over identity details, and every Codex review needs the lead to set up a workspace by hand.
- **Usage limits arrive without warning.** The weekly Claude allowance stopped a team twice in one week.

The operator also wants teams outside this project to adopt agenttalk, one repository per product. So the plan has three aims: stop paying for empty turns, speed up CI without dropping any check and make limits visible; remove the daily frictions; and make a second team's setup work, measured first on a clean machine and again at the end.

A few words used below: a **challenge** is an independent agent's verdict on whether major work should be done at all; a **probe** is a small measurement made before deciding to build; a **clock tick** is the timer that wakes a seat to look for work; a **gate** is the set of checks a change must pass before it is merged.

**Weeks 1-2 are committed.** The team re-plans at the end of week 2 from what was actually delivered. **Weeks 3-4 are re-forecast then, and weeks 5-6 are optional.** Weeks count from the team's return to full capacity. The plan sets priorities only: every item marked as major work still gets its own independent challenge before it is built.

**What we would see if it works**
1. **Protect scarce time.** Fewer paid turns spent on nothing (measured in tokens before and after); faster Windows CI with every existing check kept; no team meets a weekly limit without a warning.
2. **Fewer frictions.** Correctly wrapped seats no longer get false identity refusals (replies with an absent, malformed or unknown identity are still refused); Codex reviews without hand-made workspaces; the lead closes finished work without waking a seat; cancelled queued work is refused before a paid turn starts (work already running is not undone).
3. **A second team can adopt it.** A newcomer on a clean machine sets up a separate product repository, does one task with a cross-vendor review, survives a restart and keeps the project's knowledge. Measured in week 1 as a baseline and again at the end.
4. **Advice that reaches the seat.** Manual look-ups (where most useful lessons come from) become measurable, so that a later decision on showing only relevant, tagged lessons rests on numbers.

### 6.2 Wave 0: finish what was in flight

| What | Status (2026-10-09) | Issues |
|---|---|---|
| Finish the open reviews and merges | Done: the janitor fix, the dashboard Host-check fix and the real usage readings are merged, and so is the project-knowledge design. | #386, #387, #391, #390 (all merged) |
| Cut release 0.99.0 | Done: v0.99.0 was cut on 2026-10-08. | CHANGELOG |
| Upgrade the second team to 0.98.0, and to 0.99.0 later (the operator restarts that team's three seats each time) | Not tracked in git; no status here. | none |

### 6.3 Wave 1 (week 1): the quick paybacks, three build lanes plus a background lane

A **lane** is who builds the item: a Claude or Codex builder, a reviewer, or the background lane that runs beside the others. **Size** runs from S (small) to L (large). **Risk:** C = needs its own independent challenge before it is built; Sec = needs a security read; F = changes a stored format; none = no special step.

| # | What changes for the team | Size · risk · lane | Status (2026-10-09) | Issues |
|---|---|---|---|---|
| 1 | **Skip empty clock turns.** A clock tick wakes a seat only when something changed. It is tried on one team first, with tokens counted per day before and after. Message handling and recovery checks stay mandatory. | M · C · Codex build | **Probe first** (the challenge verdict). Step A is done: on the second team, 103 of 121 clock ticks over three days were followed by no output from the lead. The token measurement waits for the turn journal at that team's next restart. The build has not started. | from a field report |
| 2 | **Faster Windows CI, no check dropped.** Use the new per-test timings to remove the biggest costs (slow fixtures, repeated git starts, separate source and package runs). Split jobs only if measured and reviewed. | L · C if the gate layout changes · Codex build | Three parts are merged: per-test timings with a temporary longer Windows limit, separate Windows source and package runs on trial (the gate is unchanged), and shorter checks for docs-only pull requests. Whether to cut the gate over to the new layout is still to be decided. | #389, #395, #405 (merged); #378, #198, #151 |
| 3 | **Replies carry the seat's identity;** a review reply may name the commit it read; verdict words are consistent. | S-M · Sec · Claude build | Mostly merged: #403 is in, and issue #354 is closed. #299 is addressed by #403 but its issue is still open, and #178 (a completion receipt) is not done. A follow-up is open: the work board can show a verdict that names no commit under another reviewer's commit. | #403 (merged); follow-up #415; #354, #299, #178 |
| 4 | **Containment census:** where each team's files actually go, with the leads' input. Nothing is moved or deleted. | M · C · reviewer and lead | Done. | #336 |
| 5 | **Clean-machine baseline** (half a day): what a newcomer actually hits, so weeks 3-4 build only what is needed. | S · none · reviewer | Not started. | new |
| 6 | **Docs and skills paper cuts** in one pull request; knowledge notes other than lessons stop failing on a bus without a domain registry. | S+S · none · background (qwen, and a Codex read) | The docs and skills pull request is merged. The domain-registry bug is not started. | #397 (merged); #362, #363, #355, #312; domain-registry bug (new) |
| 7 | **Dependency updates,** one batch, with a package build. | S · none · background (lead) | Not started. | #117, #125, #126 |

**Added during week 1, on the operator's directions or after incidents**

| What changes for the team | Status (2026-10-09) | Issues |
|---|---|---|
| Permanent checks so usage-reading mistakes cannot return | Merged | #406 |
| Janitor cleanup works when a parent folder changes | Merged | #410 |
| Tests and probes cannot touch the live message store | In review | #407 |
| One bounded lessons look-up before build, fix and review work | In review | #414 |
| The gateway task runs without a console window | In review | #419 |
| Release one merged worktree with guarded janitor commands | In review | #420 |
| Design: project knowledge kept in the project repository. **Exactly two work orders are authorised** (currently estimated at about 10 days for both; the operator authorised them at about 9): a lessons report with a check of today's tag inference, and a record of the look-ups agents run by hand. Turning on a tag rule and reading a project's lessons from its repository are not authorised and follow the measurement. | Design merged | #390 |
| Design: finished worktrees cleaned up reliably (inventory first) | In review | #411 |
| Design: change gateway spending limits without a new ledger | Design merged | #398 |

### 6.4 Wave 2 (week 2)

All of these are **not started** as of 2026-10-09.

| # | What changes for the team | Size · risk · lane | Status | Issues |
|---|---|---|---|---|
| 8 | **Flaky tests wait for conditions instead of fixed timers,** one pull request per class of flake. | M · none · Codex build | Not started | #388, #367, #315 and similar |
| 9 | **A review-workspace helper,** so a Codex read no longer needs the lead to prepare a workspace (about 90 times so far on one team). | M · none · Claude build | Not started | new, from a field report |
| 10 | **Usage-limit follow-ups** after the real readings shipped. | M · F · Codex build | Not started | #351, #368, #332, #340 (after #301) |
| 11 | **A near-limit warning** in status, attention and the dashboard. It starts only after a field reading on the upgraded second team of what Claude's ordinary turns report: they often carry only "allowed", with no percentage. If so, the warning uses Claude's own warning status. | M · C · Claude build and frontend | Not started | from #301 |
| 12 | **The lead closes finished work quietly,** with an audit record; cancelled queued work never reaches a paid turn. | M+M · C, Sec · Codex build | Not started | #303, #252 |
| 13 | **Each pull request carries its own release-note fragment,** so merges stop conflicting on one file. | M · F · Claude build | Not started | #285 |
| 14 | **Trim the fixed handling block on every turn,** after measuring how a turn's tokens actually split. | S-M · C · Codex build | Not started | new |
| 15 | **Console defects:** phone layout, a false "Degraded", refresh during a request. | M · none · frontend, background | Not started | #380, #359, #370 (fold in #371) |
| 16 | **Old issues triaged (no build):** the lead verifies each of the July supervisor issues and closes only the ones found obsolete; checks whether mail can still be lost when `drain` output is cut short (if so, it becomes a small fix); and closes the console's Planned-lane issues unless the operator says that lane is still wanted. | S · none · lead (no build) | Not started | #26, #27, #28, #33, #37; #286, #279 |

### 6.5 The end-of-week-2 re-plan, and when to shrink the plan

At the end of week 2 the team counts the merged items, the review rounds, the Windows CI time, the Claude allowance used, the Codex credits and the paid-model euros. **If fewer than 6 of the wave 1-2 items are merged, weeks 3-4 are halved.** Weeks 3-6 are not promised before then. The plan says "merged". Some items (a probe, a census, a clean-machine baseline) can finish without a merge, and an item can be part done; whether such outcomes count is for the operator to say at the re-plan.

### 6.6 Weeks 3-4: adoption (re-forecast at the end of week 2)

- **One project, one repository for everything the team reads** (project binding): the bus, lanes and scans all find the same repository, separate from the installed runtime. (Resolving knowledge anchors against the checkout is not part of this: the accepted design parks it, see §8.) Large; needs a challenge, a security read and a format change. (#245, #144)
- **Containment, first slice:** from the census, child processes' temporary and cache folders stay inside the team folder; whatever is not yet contained is reported. Large; challenge, security read, format change. (#336)
- **Measure which lessons reach the seat (project-knowledge design, work orders 1 and 2):** a report of which accepted lessons carry no tags and what a tag rule would do to each (it changes nothing), and a record of the look-ups agents run by hand, with a usage report. Agents still see the same five lessons as today. Currently estimated at about 10 days for both (originally authorised at about 9), including their correctness and compatibility prerequisites (domain-qualified supersession, typed copyable event ids in lesson lines, and the reader-first two-release transition), as set out in `docs/design/project-knowledge.md`, sections 2 and 6; authorised by the operator on 2026-10-07. (#293, part; #390)
- **Not authorised yet, and held until the numbers exist:** showing only tagged, matching lessons (a setting that is off by default, about 1.5 days) and reading a project's lessons from its own repository, read-only (about 5 days). Each needs a separate decision after the measurement. No shared layers. (#390)
- **The clean-machine exercise again,** to show what improved. Small.
- **A maturity page** that labels every feature Stable, Beta, Preview or Experimental, and **an inventory of every path** by which knowledge can leave a machine. Small and medium. (#383, #385)
- **Scoped close fix,** only if a modernisation programme that uses close runs in this window. (#377)

### 6.7 Weeks 5-6: optional

These go ahead only with spare allowance:
- the review handover built in, if the running trial shows benefit (#306);
- one packaged modernisation entry path (#153);
- a source for searchable lead history (#375);
- the turn ticket (#384);
- a Linux and macOS monitor, only if the teams need it (#356).

### 6.8 How the team works through it

- **At most three build lanes at once,** with one Claude and one Codex reviewer kept free. About 30% of capacity is held back for review fixes and reruns.
- **Codex seats take most large builds;** Claude seats design, review and take the security reads. This protects the shared Claude allowance.
- **Budgets per week:** the Claude allowance share (set once real readings exist; until then judged by limit stops), Codex credits, and paid-model euros (about EUR 59 is left under the EUR 100 ceiling, and this plan needs almost none of it).
- **Combined merges** go through one tested integration branch per batch, so one Windows run covers several pull requests.
- **Releases:** one at the end of each wave that changes behaviour.
- **Windows time limit:** the temporary longer limit expires on 6 November; it is revisited with week 1's measurements.
- **How far to trust the plan:** the two seats that challenged it (both said "reshape") had each worked on items in it, so under the challenge rules their verdicts are not independent. The plan is not treated as independently approved. It sets priorities only; every item marked C still gets its own challenge.

### 6.9 Running trials from the 2026-10-05 ideas game *(operator approved, 2026-10-05)*

Three seats played two of the operator's design games blind: the lead and the frontend developer (both Claude) and a developer on Codex. In the first game, a random string forces unrelated things together into a feature. In the second, the usual rules of tools for agent teams are broken on purpose to find a version that still works. Of the 36 ideas, the three below won a blind vote (3, 2 and 1 points per voter). The games, the vote and the incidents named below come from the team's own records, not from this repository. Each starts as a small trial whose result is counted, and the trials run alongside the plan rather than after it. Any change to code or to a shipped skill still goes through the usual challenge, build and review first.

1. **A careful handover** (every voter's first choice). Whoever receives a job first says back, in two lines, what it will build, how it will check its work and what it will not touch. A job that changes hands carries a short notebook: decisions, unfinished work, open questions and the owner of each next step. At the end, the result is compared with what was asked.
   - *Why:* per the team's records, on 2026-10-04 a usage limit forced five seats into a fresh session. Each kept its files but lost the conversation it had been working in, including what it was about to do next. A misread work order is also a common reason for work to come back for another round.
   - *Trial (no code):* the next five handovers. Count the misunderstandings caught and the time the handover costs.
   - *Then:* the wrapper carries the notebook across sessions automatically. This is the "short handoff note" of the context policy (§8, #158).
2. **Lessons earn trust by being used.** Each lesson shows how often seats cited it as used, and when it was last checked. Lessons that keep helping are promoted; lessons that are shown but never used go quiet.
   - *Why:* the lead approves every lesson by hand, and the 2026-10-05 memory study found that today's records cannot show which lessons actually reach a seat or help it.
   - *First step (display only):* replies already record the lessons they used (`lessons_used=`). Show "cited as used N times, last checked on <date>" beside each lesson, with no change in behaviour yet. This pairs with logging manual lesson look-ups (#293, weeks 3-4 above).
3. **Last Word.** On a decision that matters, the lead collects every seat's independent view before it says what it prefers itself.
   - *Why:* reviewers who are told the lead's view tend to follow it; reviews that are not told the expected answer find more.
   - *Trial (no code):* the next three such decisions. Record whether the independent views changed the outcome. If they did, the lead skill adopts the rule.

The runner-up, the **tuning call**, is in §8.

---

## 7. Candidate Features From the Agent-to-Agent Landscape

**Status: investigated on 2026-09-27 by two independent seats from different vendors.** Their scoped conclusion: among the tools examined, none has a messaging feature that would replace agenttalk's combination of typed correlation, cross-vendor review and close authority, and unattended seats. The value is in a few borrowed **reliability ideas**, and each needs a challenge verdict before it becomes work.

| Rank | Candidate | Borrowed from | When |
|---|---|---|---|
| 1 | Wrappers survive store contention, and recovery is verified (§8: shipped in 0.94.0 as #218) | persistent-checkpoint recovery (LangGraph); bounded delivery | **Shipped** (0.94.0) |
| 2 | O(1)-ish publication: no full scan under the lock; incremental order (§8) | the append-only WAL access pattern of a SQLite bridge, not SQLite itself | Later (§8), measured first |
| 3 | Terminating obligations and stale-obligation reconciliation (§8) | A2A's task lifecycle: every task reaches a terminal state | Later (§8): the report first, then per-thread compaction |
| 4 | Revive on need: mail owed to an exited seat triggers its relaunch | Agent Teams reviving a stopped teammate | Later, after the supervisor work in §8 |
| 5 | Deferred self-wake (`not_before`), so a seat can wait on CI without holding its turn | Claude Code's scheduled wakeups; idle notifications | Later |
| 6 | Opt-in, one-shot lifecycle notifications (idle with an unanswered obligation, wrapper failed) | cross-session messaging subscriptions | Later, if still needed after 1 |
| 7 | Opt-in completion gates: a tagged "done" without a verdict or head is refused and re-prompted in the same turn | Agent Teams' TaskCompleted/TeammateIdle hooks | Later (operator decision; it reverses the tag layer's deliberate permissiveness) |

**Rejected:**
- another peer transport or MCP relay (it splits delivery, identity and audit);
- a wholesale SQLite store (it loses per-file signing and archives; an index is fine);
- Agent Teams as the runtime (Claude-only, interactive-only, no resume);
- a shared self-claim task list (it breaks deliberate cross-vendor routing and the read-only board);
- an LLM speaker selector;
- heartbeat-driven model turns on idle seats;
- A2A endpoints and Agent Cards now (no native CLI support, large new scope; revisit when a named remote integration needs them);
- replacing gates with vendor hooks;
- TTL auto-success.

**Corrections to the original research:**
- A2A v1.0 was announced on 2026-03-12, not in January.
- Agent Card signing is optional.
- A2A does have typed tasks and correlation. agenttalk's real differentiator is cross-vendor cold reads and close authority.
- Claude sessions can also message each other outside Teams.

The research (2026-09-27) found four families of direct agent-to-agent communication:

1. **In-program frameworks** (AutoGen, CrewAI, LangGraph, OpenAI Agents SDK): the agents are LLM calls inside one program, coordinated by group chat, task outputs, a typed-state graph or handoffs. They are not separate CLIs.
2. **Claude Code Agent Teams** (experimental): per-agent JSON mailboxes validated on read, a file-locked shared task list, and hooks as quality gates. Claude-only, one team per session, interactive sessions only.
3. **The A2A protocol** (Linux Foundation, v1.0 on 2026-03-12): HTTP or gRPC bindings, typed tasks, and optionally signed Agent Cards. Neither major coding CLI supports it natively; only MCP bridges exist.
4. **Cross-vendor CLI bridges** (for example a WAL-mode SQLite bridge, JSONL mailboxes, MCP relays): the closest to agenttalk. Some have threads and replies (an MCP mail server, for one). None of those examined has review, gate, close or authority semantics.

agenttalk's differences:
- typed request/reply with threads;
- lead and liaison authority;
- durable wrappers that run seats unattended;
- cross-vendor cold reads, gates, closes and acceptance;
- spend ledgers;
- the console.

---

## 8. Later / On Hold

**Carried from the old "Next" list (§6 before 2026-10-09).** The 2026-10-07 plan (§6) does not carry these. Every item is summarised here (the full old wording is in git history); each keeps its issue numbers and, where the old text gave one, the trigger that brings it back. Before any of them is built it gets its own challenge, as before.
- **The lead can always (re)start team members** (operator directive 2026-09-27; old §6.1). A dead seat is still relaunched by hand, because the supervisor is not run on the maintainers' fleet. Eight gaps were listed: run the supervisor for the fleet (G1); remove two manual pre-steps, including a preflight for the Codex ACL step (G2, #147); guard-aware relaunch (G3); a crash-simulation harness (G4, a prerequisite for G3); a POSIX supervisor (G5, design first); decide absence from a complete process snapshot, not the heartbeat (G6, which would cut a dead Codex seat's outage from about 40 minutes); terminate a provably childless wrapper and cap the retry (G7); launch only on complete ownership proof (G8, design first). Old order: G2 then G1, then G6; G4 before G3, G7 and G8. Related: #155, #139, #180, #182. The older supervisor issues #26, #27, #28 and #33, and #37, are scheduled for verification in wave 2 item 16 (§6.4); only issues found obsolete are closed.
- **The publication hot path** (old §6.2). **Stage 1 shipped in 0.94.0 as #218:** wrappers survive store-lock contention without re-driving a paid turn. **Still open:** stage 2, measure lock wait against lock hold at fleet size, remove redundant scans, share the validated snapshot with the board, and only then decide on incremental order maintenance (an append-only order log is a format change that needs its own challenge and compatibility decision). Undecided: per-recipient inboxes, only if measurement shows the shared order cannot meet the target. Not planned: replacing the per-message files with SQLite (a rebuildable index is acceptable). Interim rule: one publication per command. Related: #154, #147, #171.
- **Compaction blocked by stale unanswered openers** (old §6.3). About 130 unanswered openers keep compaction from reclaiming the store. Needed: a read-only reconciliation report (12-20 agent-hours) and previewed, individually authorized dispositions; later, archiving closed threads above the floor (6-12 h) and a visible pin count in `status` and the console. Never expiry by age.
- **Context policy and per-seat memory** (old §6.4 items 1-2). Per turn the wrapper chooses RESUME, SUMMARY (a fresh session seeded with a handoff note and a context pack, #158) or FRESH; per-project knowledge and per-seat memory make "fresh with knowledge" the default for dispatches. **The per-project knowledge part is partly carried by the plan:** the design (#390) is merged and its two measurement work orders are in weeks 3-4; the rest follows the numbers. Resolving knowledge anchors against the project's checkout (#245) is parked by that design (`docs/design/project-knowledge.md`, section 2). The migration method as a first-class workflow (#153) is in the plan's optional weeks 5-6. Spec-kitty remnants were retired (operator decision, 2026-09-28).
- **Store and host survival** (old §6.5): #156 slice 2, a durable mutation journal with the commit point before the local transition; #155, logon relaunch of registered seats through the existing planner and singleton lease, with one deduplicated boot note.
- **The ideas-game trials** (old §6.6) are still running; they are now in §6.9.

**From the 2026-10-07 plan: not now.**
- **To close:** #264 and #274 (superseded) and #195 (its facts live in #194).
- **Kept parked:** #308 (holding back paid turns was dropped) and #371 (folded into the console defects, wave 2 item 15).
- **Planned lane:** #286 and #279 are scheduled for closure in wave 2 item 16 (§6.4), unless the operator says the console's Planned lane is still wanted.
- **#92 and #93 (July):** re-triage, carry anything still needed into the reply-identity work or the project binding, then close.
- **A shorter Windows test set on pull-request runs:** not now. Speedups that keep every check come first. It returns as its own challenged proposal only if those fall short.

**On hold:**
- **Plan review write path (#206).** Annotate a plan and send it back. It is resumed only if the operator's trial of a markup tool shows that marking up beats chat. The read half is D1–D3 (§5).

**Supervisor and runtime:**
- the POSIX supervisor (P1), carried from the 2026-08 plan (the crash-simulation harness P2 is gap G4 above). A test on Linux on 2026-10-05 found that the service manager already restarts a crashed wrapper and keeps its message, while nothing acts on a seat that hangs without crashing and no alert is raised (#356);
- the **tuning call** (runner-up in the 2026-10-05 ideas game, §6.9): one command that shows every seat's agenttalk version, skills, code version, identity and rules side by side, with seats that are out of step highlighted, without spending a model turn;
- the agent lifecycle RFC (#36);
- direct-wrap diagnostics (#181);
- #26 and #28 (scheduled for verification in wave 2 item 16, §6.4; closed only if found obsolete);
- per-agent identity and authorization (RFC #19);
- carried from the 2026-08 turn-envelope/hygiene items, not yet shipped:
  - **Gate execution outside the turn envelope:** an owned, bounded, start-guarded detached runner that outlives the turn and writes SHA-bound evidence. The practice half (targeted tests in-turn, CI as the gate) is in force.
  - **Same-message livelock visibility:** surface consecutive turn starts on the same message, and park a message that repeatedly wedges the wrapper instead of starving the queue.
  - **Provision the `.agenttalk/` ignore rule:** `ASSURANCE.md` treats the state directory as gitignored, but `init` does not provision the rule.

**Bus and protocol:**
- a completion receipt for tasks (#178; its analysis is part of plan item 3, §6.3);
- ~~the task version gate refusing tasks to seats not yet relaunched after a release bump (#185), and the whole-roster sender-version check (#201)~~ — **fixed by PR #259** (the gate now compares the recipient against the fixed 0.88.0 floor, not the sender's version; other seats' versions are irrelevant);
- drain losing mail under truncated output (#37; a check is scheduled in wave 2 item 16, §6.4, with a small fix if it is still real);
- not-found subclasses (#39);
- a CLI command-registration seam (#38).

**Governance and cost:**
- per-seat usage accounting (#157);
- context packs (#158, also the context policy above);
- a risk assessment record in close policy (#159);
- reviewer calibration statistics (#160);
- composite lead-loop CLI verbs and a writable terminal console (#161).

**Comprehension:**
- the incremental extraction replacement (#141);
- scan-root selection (#144; now part of the project binding, §6.6);
- a codebase visualizer on the comprehension plane (#183).

**CI, test and release:**
- the Windows serial suite near its ceiling (#151; now plan item 2, §6.3);
- guard-timeout flakes (#171);
- the acceptance suite's git spawns (#198; partly addressed in 0.93; now plan item 2, §6.3);
- release provenance for the tested wheel (#179).
- Also carried from 2026-08: property-based and mutation testing (P3), and an honest per-OS CI matrix (P4).

**Model gateway:** the field findings in #194.

**Also labelled later by the 2026-10-07 plan:** shared or injected cross-team knowledge, a console framework migration, and widening remote access.

**Explicitly not scheduled:**
- a hosted multi-tenant SaaS;
- enterprise auth or cryptographic human identity;
- remote cloud runners;
- a semantic/vector index over large codebases;
- architecture inference that claims completeness;
- automatic large refactors without curated characterization;
- multi-repo programme management;
- a skill/method marketplace;
- broad merge/release automation before gate semantics are proven;
- native A2A support, unless §7 ranks it.

---

## 9. Native Work & Evidence Spine: Direction

The 2026-08 plan specified a native `agenttalk work` record store. The work board takes a smaller first step: **work is a projection over the bus.** Dispatches carry `work_item`/`stage` tags, replies carry typed verdicts, and a pure reducer derives the card. No second source of truth exists to drift from the bus, and "unknown" is a legal state. Anything a projection cannot prove, such as integration evidence, comes from a restricted observer that records evidence (B5), not from a second authoritative work registry.

These invariants from the original spine still hold:
- **Evidence tiers are never collapsed into one green dot:** `referenced` < `local_agent` < `local_operator` < `automation_ci` < `external_attested`. Release-blocking gates require `automation_ci`, `external_attested`, or an explicit operator waiver.
- **Evidence is write-once** and bound to the exact head, base, policy hash and producer.
- **Policy boundary:** core validates schemas, ids, references, freshness, hashes and transitions. It hardcodes no language or scanner. Project policy owns required checks, lenses, accepted tiers, waivers, named tools, timeouts and network policy. Third-party tools are declared checks, pinned and checked offline (0.93).

Workflows on top keep the 2026-08 shape, restated here so this file stays self-contained:
- **Onboarding and comprehension:** an onboarding run records the segments inspected, the claims proposed or confirmed, docs/code drift, and blocking unknowns. Large projects fan out by segment, with cross-check records before results become domains, work items, knowledge notes or characterization targets.
- **Greenfield:** requirements intake, then spec, bootstrap, slice delivery, and a release close over the exact revision.
- **Existing-project change:** onboard, record claims and unknowns before editing, map ownership and path scope, add characterization tests before risky changes, deliver small gated items, and preserve discoveries.
- **Legacy adoption:** map, preserve, build the safety net, then change in small gated steps.

The modernization dogfood showed that the migration phases are a lead-run method; #153 turns them into a first-class workflow.

---

## 10. Hard HOLDs

Do not ship broad workflow claims if any of these are true:

- Work items, lanes, gates, closes and review threads can disagree with no single projection explaining the conflict.
- **The board shows a card as Ready or Done that the bus evidence does not prove,** or shows stale state as live.
- **A board gate can block an ordinary merge check,** or any existing non-board gate changes behaviour.
- Artifact records are mutable in place.
- A gate can pass from chat prose, or from evidence that is referenced but was never executed.
- Evidence is not bound to the exact head SHA, base SHA, diff/policy hash and producer.
- Local agent evidence satisfies release-blocking gates by default.
- **An acceptance close reaches GO with a failing, missing or unassessed plan row.**
- **A malformed, contaminated or late challenge verdict is treated as proceed.**
- Project policy changed inside the same worktree silently changes the gate.
- A runner accepts shell strings by default or inherits an uncontrolled environment or secrets.
- A timeout, malformed output, parser failure or adapter failure can normalize to pass.
- Worktree cleanup can delete dirty, unmerged, unmanaged or user-created files.
- The console offers an enabled control the browser cannot perform, or collapses evidence tiers into one misleading green status.
- Cross-platform support is claimed while the supervisor runs, or is tested, on only one OS.

---

## 11. Top Risks

- **False trust:** operators read green as correctness. **Mitigation:** the language and UI say "evidence current and policy satisfied", never "code correct"; unknown is shown as unknown.
- **Store scale:** the publication guard's hold time grows with the store, and it has killed waiting wrappers (#154). **Mitigation:** one publication per command now; wrappers that survive contention, then a smaller critical section (§8); compaction unblocked (§8).
- **Seat availability:** a dead seat stalls its work until someone relaunches it. **Mitigation:** the supervisor work in §8.
- **State-machine drift:** a work layer duplicates lane/gate/close truth. **Mitigation:** the board is a pure projection (§9).
- **Harness drift:** vendor CLIs change underneath the wrapper. Seen in the field:
  - an approval reviewer rejected a bus reply as egress;
  - task-kind reply drafts were dropped;
  - long foreground commands were auto-backgrounded and then died with the turn.
  **Mitigation:** field defects go in with evidence; briefs keep commands short and chunked; cross-vendor seats are exercised on every release.
- **Token cost:** multi-agent work costs a multiple of one session, and most of it is input tokens. **Mitigation:** model routing by task size, usage accounting (#157) and context policy (§8).
- **Command-runner risk, policy drift, evidence rot:** unchanged from 2026-08, with the same mitigations (structured argv, policy-hash binding, stale-at-head detection).
- **Platform portability:** the Windows-bound supervisor. **Mitigation:** the POSIX path and a per-OS CI matrix (§8).

---

## 12. Known Limitations (2026-09-29)

- **The supervisor** is PowerShell Core + `Win32_Process`-bound, and it is not run on the maintainers' fleet. Dead seats are relaunched by hand (§8).
- **Publications serialize on one store-wide guard.** Several sends back to back can make waiting wrappers exit without a crash record (#154).
- **Wrapped seat turns start only on a message.** Unfinished multi-turn work waits for the next message (#139).
- ~~**After a release bump,** tasks to seats not yet relaunched on the new version are refused (#185). The version check covers the whole roster (#201), so a `task` to an upgraded seat is refused too while any roster member runs an older build. Point-to-point sends need `--force` until every seat is upgraded.~~ — **resolved by PR #259:** the gate now compares the recipient against the fixed 0.88.0 floor, so an upgraded seat can receive a task immediately; a recipient that genuinely predates `task` support is still (correctly) refused.
- **`drain | head` can consume mail that was never displayed** (#37).
- **The work board is read-only by design.** A stage the evidence cannot prove shows as unknown, and the reducer carries a documented seven-point residual (`docs/STEP-WORK-BOARD.md` on branch `feat/work-board-b3a`, landing with PR #215).
- **Windows CI** runs its serial suite near the one-hour ceiling (#151). Guard-timeout flakes recur on slow runners (#171).

---

## 13. Recommendation

1. **Done: v0.94.0 shipped** (the first board delivery plus the console v2 preview, with the fleet upgraded once), followed by v0.95.0, v0.96.0, v0.97.0, v0.98.0 and v0.99.0. The next steps are the operator-approved plan in §6.
2. **Later, not in the approved plan (§8):** make the team self-healing (restart a dead seat, run the supervisor, stop a dead seat waiting out its heartbeat threshold, then the crash harness and guard-aware relaunch, then host-restart survival); fix the store at the mechanism (measure and shrink the publication critical section, reconcile stale obligations). Wrappers surviving store contention already shipped in 0.94.0 (#218).
3. **Also later:** the remaining refocus items (context policy with knowledge and memory, §8) and a first-class migration method (#153, optional weeks 5-6 in §6.7). Spec-kitty removal is done.
4. **Each of these keeps its own challenge:** nothing in §8 starts without one.
5. **Later:** the remaining §7 candidates by rank, each with its own challenge verdict.

Bottom line: agenttalk can become the platform for teams of agents from different vendors to build software "by the book". But the product has to be honest about authority. It can enforce process, preserve evidence, challenge work before it starts, and fail closed when evidence is missing. It cannot remove the human oracle or prove correctness by itself.
