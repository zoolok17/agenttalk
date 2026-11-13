# agenttalk - Product Roadmap & Feasibility

**Status:** Official · **Owner:** lead (operator-facing) · **Last updated:** 2026-11-13 (UTC; a Current Plan section, §0; earlier, on 2026-10-20: a storage overview the operator asked for in §6.14; ideas from a shared-memory study in §7.4; ideas from a survey of AI self-improvement in §7.5; ideas from the research behind a video on AI self-improvement in §7.6; the Python practice and what it changes in §6.13; ideas from another single-agent harness in §7.3; conversations that grow too long added to §6.11; outside use of the same subscriptions in §6.12; the operator's order of 2026-10-12 in §6.11, the ideas round in §7.2)
**Audience:** maintainers, operators, and agents deciding what to build next.
**Horizon:** the plan the operator approved on 2026-10-07 for the next 4-6 weeks (§6): weeks 1-2 are committed, the team re-plans at the end of week 2, and weeks 5-6 are optional. Older items the plan does not carry are in a labelled "later" tier (§8). §5 records what v0.94.0 shipped and is kept for the record. §0 is the current plan; §6 is the plan of 2026-10-07.
**Current shipped baseline:** v0.105.0 (2026-11-13). `CHANGELOG.md` remains the release-history source of truth.

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

## 0. Current Plan

This section is the one current view of what is being built and in what order. The core team and the contributor team update it after each planning meeting and after each merge. The longer sections below stay as background: §6 is the plan the operator approved on 2026-10-07, and it has not been replaced.

### NOW: release 0.106.0

The rows are in the order the contributor team builds them. At most three of its pull requests (proposed changes) are open at a time. "Both reads GO" means two independent readers checked the change and said it can go ahead.

| Issue | What it does (plain words) | Owner | Size | Status | Waits on |
| --- | --- | --- | --- | --- | --- |
| #771 (issue #729) | Every command treats a blank or damaged lane checkout folder the same way, and the recovery hints name the lane they are about. (A lane is one builder's own working folder.) | contributor team | L | Built. Both reads GO on 59ab7485. The automated reviewer found nothing. | A merge of master that changes only the change log, then the core team's merge checks. |
| #769 | `doctor` (the health check) no longer creates a hidden `.agenttalk` folder in a folder that is not set up. | contributor team | S | Ready. Both reads GO. It may open as a pull request now. | Nothing. |
| #765 | Message ids are printed safely on one line in commands and notices. | contributor team | M | Ready. Both reads GO. It may open as a pull request now. | Nothing. |
| #712 | Use curl's own retry for passing network problems when the secret scanner is downloaded, instead of a new helper script. The checksum check and the unpacking stay exactly as they are, and a test of the workflow pins the retry settings. | contributor team | S | Approved, not built. A change to the test setup only, so no change-log entry. | A slot, early in the 0.106.0 cycle. |
| #761 | After a failed upload of test evidence, and never after a cancellation, the same fixed-version upload step runs one more time. A permanent error is tried twice and stays red. The test command itself is never run again or forgiven. | contributor team | M | Approved, not built. A change to the test setup only, so no change-log entry. | A slot, early in the 0.106.0 cycle. |
| #716 items 1-3 | Clearer first-run advice from `doctor`: "not set up yet", notes about skills, and finding PowerShell. | contributor team | M | Ready. Both reads GO. | #769 merging. |
| #772 | A send that overlaps a reset fails cleanly and leaves no missing entry in the message order. | contributor team | S | Ready. Both reads GO. | A slot. |
| #766 | A test of a blocked writer checks its time limit correctly. | contributor team | S | Ready. Both reads GO. | A slot. |
| #770 | Browser tests let the browser choose its own debugging port. | contributor team | S | Ready. The core team's read said GO. | The core team's fresh read. |
| #671 | `lane check` judges the files a merge really writes. | contributor team | M | Ready. Both reads GO. | A slot. |
| #668 | A relative worktrees folder can be settled when a lane is retired. | contributor team | S | Ready. Both reads GO. | A slot. |
| #716 items 8+5 | Install steps: a note about the venv package on Debian and Ubuntu, and keeping agenttalk on the path for assistants. | contributor team | S | Ready. Both reads GO. | The change log is rebased when its pull request is opened. |
| #716 item 6 | What to do when Windows refuses to start the agenttalk or pip launcher. | contributor team | S | Ready. Both reads GO. | #716 items 8+5. |
| #716 item 7 | The lead setup brief installs the current release. | contributor team | S | The branch is ready. The brief's steps need a re-check. | #716 item 6. |
| #759 | `lane gc` and three other commands stop trusting a damaged checkout folder, and plain `lane abandon` finds its launches by the lane's registered id. | contributor team | M | Built. Both reads GO on the follow-up. | #771 merging, then a rebase and a short read. |
| #748 (part A only) | A backup reads the store's session id before and after copying and refuses the snapshot if it changed. | contributor team | S | Open. | A slot. |
| Skills review (no issue yet) | Read all 44 shipped skills against 0.105.0, fix what has drifted, and re-stamp only what was read. The first batch is about two agent-hours (eight files); the core team checks the lead guide and the lead setup brief. | contributor team, and the core team for those two documents | L (the first batch is about two agent-hours, then it is sized) | Agreed plan. The 0.105.0 tag is published. | Nothing; the pilot batch runs now. |

### NEXT

Agreed follow-ups that are not built yet.

| Issue | What it does (plain words) | Owner | Size | Status | Waits on |
| --- | --- | --- | --- | --- | --- |
| Lane id audit (new issue needed) | Find the other places that still read a lane record's own id, such as verification, delivery clean-up and reservation filtering, and make them use the registered id. | contributor team | M | Agreed. | #771 merging, then an issue and a plan. |
| Claude skills use `AGENTTALK_PY` (new issue) | Assistants reach the right agenttalk without changing the path. | contributor team | M | Needs a challenge (an independent check that it should be done at all) first. | The 0.106.0 release. |
| #758 | A family of timing tests still fails on slow Windows runners. | contributor team | S-M | Open. | An owner. |
| #748 (the two wider checks) | Further checks that a backup was not mixed across sessions. | contributor team | S | A design is needed. | A challenge first. |
| Small follow-ups | The store's own live-copy race path has no test (a note from #768). Two ways of escaping a printed id exist in one program, and some cut ids say "stored record" (notes from #765). | contributor team | S | Open. | An owner. |
| Ready from earlier, not yet in a slot | #750, #749, #739 and #721 (each S), and refreshes of #498, #285, #697, #500, #652 and #695 (each S-M, rebased, earlier GO reads carried over). | contributor team | S-M | Ready. | A slot. |
| Review of 13 old pull requests | The pull requests open since July to early November: #92, #93, #195, #264, #274, #286, #308, #371, #411, #490, #495, #717 and #754. Each is either kept and finished, or closed with a note. | core team | M | The decision is pending. | A decision. |
| A withdrawn task can still be delivered | A task that was withdrawn could still be delivered again until it was given up on. This is a fix to the tool (no issue yet). | core team | S | Open. | An issue. |

### LATER

The backlog, by theme. Each line points to the place that holds the detail.

- **Supervisor and runtime:** §8 and #497, #519, #714, #722.
- **Messages and the way agents talk:** §8 and #514, #515, #516, #535 to #540, #617, #767.
- **Cost, limits and shared capacity:** §8 and #526, #529, #543, #546, #616.
- **Model gateway:** the field findings in #194, and #468.
- **Comprehension (reading and understanding a code base):** §8 and #496.
- **CI, tests and release:** §8 and #486, #510, #639, #666, #697, #713.
- **Long conversations and fresh starts:** §6.11 and #475, #477, #482, #487.
- **Keeping the store healthy (compaction, backups, repairs):** §8 and #706, #718, #732.
- **A storage overview page:** §6.14 and #576.
- **The lead's routines and the work board:** #518, #530, #539, #695.
- **Lessons and knowledge:** #541, #544 and §7.4.
- **Documentation follow-ups:** #517, #545, #572, #594.
- **Ideas from outside research:** §7.1 to §7.6.
- **On hold:** the plan review write path (#206, §8).

### NEW IDEAS

| Issue | What it does (plain words) | Owner | Size | Status | Waits on |
| --- | --- | --- | --- | --- | --- |
|  |  |  |  |  |  |

Ideas are added here after a planning meeting, each with a size guess and a proposed slot. A new idea is challenged, meaning an independent check of whether it should be done at all, before anyone builds it.

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

| Product | What it is | Today (v0.100.0) |
|---|---|---|
| **A. Core Coordination Bus** | Roster, typed messages, threads, requests, work orders, review handoff, broadcast, transcripts. | Product-grade. `task`/`task-response` work orders (0.88). Validated work-item tags and reply verdicts (0.93). **Known scale limit:** one store-wide publication guard (§8). |
| **B. Native Work & Evidence Spine** | Work items, workspaces, evidence, review bindings, gate/check state, delivery lifecycle. | **A derived work board (#207) shipped in 0.94.0.** Cards are projected from bus events tagged with `work_item`/`stage`; there is no hand-kept state. Tags and verdicts shipped in 0.93; the data layer, the reducer, the bounded feed and the board view shipped in 0.94.0. The operator walkthrough on live data is pending, and the deferred slices are listed in §5. |
| **C. Method Engine Adapter** | External planners create or link work; agenttalk owns execution evidence. | **Retired in its spec-kitty form** (operator decision, 2026-09-28; §8). The migration method becoming first-class is still open (#153). |
| **D. Craft Skill Pack** | Coding, review, test, QA, security, release and lead skills. | Useful and growing. Challenge skill (0.93); lead skills aligned with the work-item protocol (0.93); the assurance-scan and review lenses. |
| **E. Assurance & Release Governance** | Gates, closes, specialist sign-off, acceptance, scan evidence. | **Strong.** Cooperative acceptance passes on `close` (0.92). A pinned, offline-checked tool registry with `close acceptance preflight` (0.93). |
| **F. Knowledge & Codebase Memory** | Domains, pointer notes, lessons, onboarding runs, the comprehension plane. | Primitives exist. Comprehension producer slice 1, a static inventory plane (0.87). Seats look in the team's lessons once before build, fix and review work (0.100.0, #414). The per-project knowledge design is merged (#390); its measurement work orders are in the plan (§6.6). |
| **G. Operator Control Plane** | Local console, read views, attention/liveness, lead chat. | The console reports risk honestly (0.86). **Console v2 preview shipped in 0.94.0 (#214),** with the board view inside it; the operator walkthrough is pending. Plan review is on hold (#206). The operator's own two-way channel with the lead, usable from a phone, was approved on 2026-10-09; it waits for the operator's server order (§7.1). |
| **H. Execution Runtime** | Wrapper, supervisor, session continuity, dead-letter, lanes. | The wrapper is far more reliable (0.84–0.91): replies are owned by the wrapper, failures are loud, redelivery is honest. Store backup (0.89). **Dead seats are relaunched by hand**, because the supervisor is not run on the maintainers' fleet (§8). The POSIX gap remains. |
| **I. Delivery Workflows** | Greenfield build, existing-project change, legacy adoption, release. | Dogfooded end to end on a legacy-codebase modernization dogfood: two attempts, the second a blind rerun compared against the first. Not productized yet (#153). |
| **J. Model Gateway** *(optional)* | A local, watched gateway so a third-party open-weight model can run a seat. | Working. A spend ledger enforces a cutoff/soft-stop envelope, configurable at init (0.91), with Windows and Linux service backends (0.91). Reasoning is no longer re-sent as input (0.92). Since 0.100.0 the Windows task starts without a console window, so closing a terminal can no longer stop it (#419); the maintainers' own gateway still runs the older task until the operator runs the prepared one-time update. Field findings: #194; a dropped stream left uncertain charges that blocked paid calls until the lead settled them (#424). |

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

*Kept for the record. What v0.95.0, v0.96.0, v0.97.0, v0.98.0, v0.99.0, v0.100.0, v0.101.0, v0.101.1, v0.102.0, v0.103.0, v0.104.0 and v0.105.0 shipped is in `CHANGELOG.md`.*

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

**Approved by the operator on 2026-10-07. Status as of 2026-10-10.** The plan replaces the ranked list that stood here before. The older items it does not carry are in §8, each with a pointer.

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

| What | Status (2026-10-09; the rows that name pull requests re-checked 2026-10-10) | Issues |
|---|---|---|
| Finish the open reviews and merges | Done: the janitor fix, the dashboard Host-check fix and the real usage readings are merged, and so is the project-knowledge design. | #386, #387, #391, #390 (all merged) |
| Cut release 0.99.0 | Done: v0.99.0 was cut on 2026-10-08. | CHANGELOG |
| Cut release 0.100.0 | Done: v0.100.0 was cut on 2026-10-10. It carries the lessons look-up, the windowless gateway task, the worktree release, the domain-registry fix and the usage-reading checks. | #431 |
| Upgrade the second team to 0.98.0, and to 0.99.0 later (the operator restarts that team's three seats each time) | Not tracked in git; no status here. | none |

### 6.3 Wave 1 (week 1): the quick paybacks, three build lanes plus a background lane

A **lane** is who builds the item: a Claude or Codex builder, a reviewer, or the background lane that runs beside the others. **Size** runs from S (small) to L (large). **Risk:** C = needs its own independent challenge before it is built; Sec = needs a security read; F = changes a stored format; none = no special step.

| # | What changes for the team | Size · risk · lane | Status (2026-10-09; the rows that name pull requests re-checked 2026-10-10) | Issues |
|---|---|---|---|---|
| 1 | **Skip empty clock turns.** A clock tick wakes a seat only when something changed. It is tried on one team first, with tokens counted per day before and after. Message handling and recovery checks stay mandatory. | M · C · Codex build | **Probe first** (the challenge verdict). Step A is done: on the second team, 103 of 121 clock ticks over three days were followed by no output from the lead. The token measurement waits for the turn journal at that team's next restart. The build has not started. | from a field report |
| 2 | **Faster Windows CI, no check dropped.** Use the new per-test timings to remove the biggest costs (slow fixtures, repeated git starts, separate source and package runs). Split jobs only if measured and reviewed. | L · C if the gate layout changes · Codex build | Three parts are merged: per-test timings with a temporary longer Windows limit, separate Windows source and package runs on trial (the gate is unchanged), and shorter checks for docs-only pull requests. Whether to cut the gate over to the new layout is still to be decided. | #389, #395 (merged); #405 (merged, in 0.100.0); #378, #198, #151 |
| 3 | **Replies carry the seat's identity;** a review reply may name the commit it read; verdict words are consistent. | S-M · Sec · Claude build | Mostly merged: #403 is in 0.100.0, and issues #354 and #299 are closed. #178 (a completion record, so a finished work order is never run twice or lost) is not done; the operator's order of 2026-10-12 builds it attempt-bound (§6.11). A follow-up is open: the work board can show a verdict that names no commit under another reviewer's commit. | #403 (merged); follow-up #415; #354, #299, #178 |
| 4 | **Containment census:** where each team's files actually go, with the leads' input. Nothing is moved or deleted. | M · C · reviewer and lead | Done. | #336 |
| 5 | **Clean-machine baseline** (half a day): what a newcomer actually hits, so weeks 3-4 build only what is needed. | S · none · reviewer | Not started. | new |
| 6 | **Docs and skills paper cuts** in one pull request; knowledge notes other than lessons stop failing on a bus without a domain registry. | S+S · none · background (qwen, and a Codex read) | Both parts are done and shipped in 0.100.0: the docs and skills pull request (#397), and the fix that lets team notes work on a bus without a domain registry (#425). | #397, #425 (merged, in 0.100.0); #362, #363, #355 (closed); #312 (stays open: the policy data carries no tax label yet) |
| 7 | **Dependency updates,** one batch, with a package build. | S · none · background (lead) | Done: one batch (#422, merged 2026-10-09) raised the three gate minimums and replaced the separate update pull requests, which GitHub now shows as merged. | #422 (merged); replaces #117, #125, #126 |

**Added during week 1, on the operator's directions or after incidents**

| What changes for the team | Status (2026-10-09; the rows that name pull requests re-checked 2026-10-10) | Issues |
|---|---|---|
| Permanent checks so usage-reading mistakes cannot return | Merged, shipped in 0.100.0 | #406 |
| Janitor cleanup works when a parent folder changes | Merged, shipped in 0.100.0 | #410 |
| Tests and probes cannot touch the live message store | Merged on 2026-10-10 after 18 review rounds; not yet in a release. Follow-ups: #426, #427, #432 | #407 |
| Docs brought up to date after 0.100.0 (the README's fixed identity gap, the roadmap's statuses) | Merged on 2026-10-10, together with this roadmap's update (#434). Four issues it showed as fixed were closed: #299, #355, #362, #363 | #433 |
| One bounded lessons look-up before build, fix and review work | Merged, shipped in 0.100.0 | #414 |
| The gateway task runs without a console window | Merged, shipped in 0.100.0 | #419 |
| Release one merged worktree with guarded janitor commands | Merged, shipped in 0.100.0. Follow-up: a delivered lane whose publication may be unfinished should keep its checkout | #420; #428 |
| Design: project knowledge kept in the project repository. **Exactly two work orders are authorised** (currently estimated at about 10 days for both; the operator authorised them at about 9): a lessons report with a check of today's tag inference, and a record of the look-ups agents run by hand. Turning on a tag rule and reading a project's lessons from its repository are not authorised and follow the measurement. | Design merged | #390 |
| Design: finished worktrees cleaned up reliably (inventory first) | In review | #411 |
| Design: change gateway spending limits without a new ledger | Design merged | #398 |

### 6.4 Wave 2 (week 2)

All of these are **not started** as of 2026-10-10.

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
- one packaged modernisation entry path (#153): its first increment moved up on 2026-10-16 (§6.13);
- a source for searchable lead history (#375);
- the turn ticket (#384);
- a Linux and macOS monitor (#356): optional in weeks 5-6, still planned (§6.11).

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

### 6.10 What the week taught us (2026-10-08 to 2026-10-10)

These come from the team's own merges and reviews this week. They change how the plan is run, not what it contains.

1. **A safeguard and a new command can clash even when neither shows a problem alone.** The store fence (#407) and the new worktree release (#420) were each approved. Merged together, the release mode bypassed the fence. The reviewer caught it on the combined pull request, which was closed unmerged. *Change:* after master is merged into a safeguard's branch, every command master added is checked against that safeguard before merging.
2. **Checks run only on Windows miss failures that only Linux and macOS show.** The fence passed every local run and both reviews on Windows. Then CI's Linux and macOS runs failed: looking below a file reports "not a directory" there, but "missing" on Windows. *Change:* the full matrix stays the merge gate, and fixes for such errors carry a test that fakes the other system's error, so a Windows run fails too.
3. **A safeguard whose promise keeps widening costs many review rounds.** The fence took 18 rounds. Its promise grew from "no store outside the test folder" to "nothing Git reads lies outside". Each round found one more route Git can follow: a home-folder shortcut, links, alternates, a shared folder, hard links. The rounds converged once the code refused whatever it could not check exactly, instead of copying Git's own parsing. *Change:* a safeguard's design states its limits before it is built, and "refuse what you cannot check" is the default.
4. **Combined merges paid off.** One integration pull request carried #414 and #419 through one Windows run (#430), and the release followed the same day.
5. **Waiting on the operator:** the one-time update of the maintainers' gateway task to 0.100.0 (the step-by-step guide is reviewed), and the second team's switch-on of usage readings (its runbook is ready). Both need the operator at the machine.

### 6.11 The operator's order of 2026-10-12

On 2026-10-12 the operator approved this order for the work that comes next. In plain words: switch the maintainers' gateway to the new release, settle the bug that paused the paid gateway, then make "finished" mean something a seat cannot fake, then fix how the README tells people to run a team without anyone watching.

1. **The 0.101.0 switch.** Its step-by-step guide is reviewed and approved.
2. **#424, the Qwen gateway's LiteLLM stream bug.** It hit a second time on 2026-10-12 and paused all paid gateway work until it was settled by hand. Then #475, step 1 of the item on long conversations added below.
3. **#178, the completion record, built attempt-bound.** A "done" is tied to one attempt, not to a seat or a ticket (§7.2, item 1).
4. **A README change.** Present "a start script plus the lead's timers" as the normal way to run a team unattended on any operating system. The supervisor becomes an optional, independent watcher. Add a short section on leads that report to a lead.

#356 stays planned.

**Added by the operator on 2026-10-13: conversations that grow too long.** A background agent keeps one conversation for as long as it runs, and every turn sends all of it again, so every turn gets dearer. On 2026-10-13 a lead on the maintainers' second machine had reached 830 turns; each idle check cost about half a point of its subscription's weekly allowance, and it had to be restarted on a new conversation by hand. Earlier cases: September's measured token use, and agents that got stuck in July and September. The operator approved three steps:
- **Step 1, right after #424 (before #178):** a supported command that starts one agent on a fresh conversation, and a warning in `status` before a conversation gets expensive (#475).
- **Steps 2 and 3, next to #178:** a short handover note written at the end of every turn, so a fresh start loses nothing; then agenttalk choosing before each turn whether to continue, start fresh with the note, or start completely fresh (reviewers always). The design of 2026-09-22 (`docs/design/context-policy.md`) is refreshed first, and the steps get their own challenge.
- **Until then,** the lead restarts an idle agent on a fresh conversation once it passes about 300 turns.

Two notes beside item 4. They are notes, not a change of order:
- **The lead's timers expire.** In Claude Code they lapse after about seven days and live only in the session. So the README must say how to renew them, until agenttalk owns reminders itself (§7.2, item 6).
- **The identity gap.** The README must state it beside the new unattended advice (§7.2, item 5): a seat can send a message that claims to come from the lead. It must also say who is operator-facing in a chain where one lead reports to another lead.

### 6.12 When people use the same subscriptions outside agenttalk *(operator approved, 2026-10-12)*

A team's people may use the same Claude subscription, or the same pay-per-use key, outside agenttalk. Part of this is already handled. A Claude seat's usage reading is the provider's figure for the whole account, so outside use is counted in the pacing. A seat that reaches the limit stops and keeps its work queued. For a pay-per-use key, comparing the provider's bill with the gateway's own record of its spending shows other use of the key; the maintainers make that comparison themselves, outside agenttalk. What is missing: nobody can tell who used what, there is no fresh figure while no seat is running, a forecast built from agenttalk's own work alone comes out wrong, and nothing keeps room for the people.

The operator approved four measures. Two of them belong to a separate usage service: a program, not part of agenttalk and kept outside this repository, that gathers the usage figures of several subscriptions in one place.
1. **Advice in the README:** one subscription for the agents and separate ones for people, and one pay-per-use key for each agenttalk team. It goes into the README change (§6.11, item 4).
2. **A reserve for people:** a per-team setting such as "the agents use at most 70% of the weekly allowance". Past it, the lead takes on urgent work only. It goes to the usage service's design.
3. **Outside use shown as its own category,** never as an error. It also goes to the usage service's design.
4. **Readings from people's own Claude Code sessions,** through the same status-line reading the seats already use, so that less of the outside use goes unseen. Use in the Claude app or on the website still shows only in the account total. Small; it waits its turn with the usage-limit follow-ups (wave 2, item 10). (#470)

### 6.13 The Python practice and the first outside team *(the operator's focus from 2026-10-14)*

On 2026-10-14 the operator put all effort on one goal until the first outside team starts using agenttalk (about 2026-10-21): learn as much as possible about understanding, improving and migrating Python codebases. The operator's order in §6.11 resumes after that. The team practised on public open-source code only, on the maintainers' Windows desktop and on a Linux virtual machine:
- the core of an online shop from 2012 (Satchmo 0.9.2), moved from Python 2 to Python 3.13 and Django 5.2 (the web framework the shop is built on), then improved, including its payment modules;
- part of a newer shop (Shuup 3.1.0, from 2021), moved from Django 2.2 to 5.2 one long-term-support version at a time;
- a donation platform's 2018 payout engine (Liberapay), moved to Python 3.13 with its money code unchanged;
- the shops on Python 3.14 in container images (a sealed, ready-to-run copy of an app and everything it needs); a PHP web app (Crater, built on Laravel) moved to PHP 8.5; and one Python and one PHP app run under Kubernetes, a common system for running containers;
- still running: charging a payment once, and handling a message once, when the database is a simple key-value store and messages arrive through a queue that can deliver the same message twice (with local stand-ins for the cloud services).

**What it taught about migrations.** This is the method, and it is what an outside team needs most.
1. **A second, independent rebuild by an AI from another vendor found problems that everything else had missed.** Every frozen test suite and every review by the same vendor had passed the work. The rebuild found things the move to Python 3 had broken. An invalid payment choice crashed the page instead of showing an error, because the error path used a function the new framework had removed. Python 3 refuses to sort two items that tie, where Python 2 did not. File data was handled as text instead of raw bytes. It also found a test suite that stayed green even when the payment step was skipped.
2. **A check is evidence only if it can fail.** Break the code on purpose and see the check go red. A "fixed" claim needs proof that its test fails when the fix is removed, and fails because the result was wrong, not because the test could not start.
3. **Claims that something is safe when many users act at once need a realistic setup.** That means the same kind of database as the real system and several server processes, with the old version measured the same way for comparison. In every run of one such test, a safety fix made overselling worse. The simple file database used for the quick tests had hidden it.
4. **One finding of a kind means a search for the whole kind.** The search after five findings found five more.
5. **A test that fails after an upgrade is a finding, never a test to edit.** And each claim is written at the size of its proof.

**What it taught about agenttalk.** These are problems a new Linux or Python team meets in its first days.
- Background agents would not start from a normal Python virtual environment on Linux and macOS. Fixed in v0.102.0 (#493).
- First-hour problems addressed in v0.103.0 (the limits of #513's grouped refusals are in `CHANGELOG.md`):
  - a Claude agent that could not start got advice about Codex (#508, item 1);
  - a restart hint needed the supervisor, which a new team does not run (#508, item 2);
  - a refusal blamed an old version when the agent simply had not started yet (#508, item 3);
  - searching lessons by tag alone failed, and a lesson filed under a chosen scope was stored under a different domain without saying so (#508, items 5 and 6);
  - the command that records a lesson reported its missing options one at a time (#513).
- The code-understanding scan can report a Python repository as complete although it understood nothing in it (#496). A trial read of both shops showed that Python support is feasible (#495, design only).
- An AI tool that hangs at start-up shows as "working" forever (#497).
- Coordination gaps:
  - a finished review does not wake the lead, and one team sat idle for about an hour because of it (#514);
  - drafts an agent decided not to send raise false alarms (#515);
  - a review request that reuses a task's id loses its thread (#516).

**What changes in the plan.** Each larger item keeps its own challenge; the challenge on this list asked for it to stay small.
1. **Released:** v0.105.0 (2026-11-13), following v0.104.0 (2026-10-22), v0.103.0 (2026-10-21) and v0.102.0's virtual-environment fix.
2. **Delivered in v0.103.0:**
   - the first-hour fixes above (#508 items 1-3, 5 and 6, and #513 with its documented validation limits), plus recovery advice for hung tools and restarting agents after a tool update (#497); #508 item 4 and automatic hang detection remain open;
   - the first-run setup guide linked from the README, with its relationship to the lead guide and later command changes explained (#507). Its tested/install baseline remains v0.101.1; no new platform run is claimed.
3. **Still planned before the outside team starts:** an honest scan status for languages the scan cannot read (#496). This did not ship in v0.103.0.
4. **The migration method moves up** from the optional weeks 5-6 (§6.7). v0.103.0 ships the migration playbook and lesson pack, and v0.104.0 lets a team share lesson packs with another team (#554); the planned skill and blind comparison remain outstanding. That skill is about 150 lines, carrying the rules above with no project or payment specifics (#153). It ships only if a blind test shows that it helps: two fresh agents port the same small old app, which hides four problems of the kinds above, one agent with the skill and one without.
5. **Designed later, each with its own challenge:** #514, #515 and #516. Detecting a hung tool (#497) has no place in the plan yet; start-on-boot (#356) keeps its place in §6.7.
6. **Python code understanding (#495)** follows the outside team's first week, with its own challenge.

### 6.14 A storage overview *(the operator's request, 2026-10-20)*

The operator asked for "a UI for storage usage overview - which project is using how much space and for what, with a clear graphical representation on what is going on".

**Why.** On 2026-10-20 the project drive of the maintainers' desktop had about 10 GB free. Nothing in agenttalk had shown it filling up. Finding the large folders and checking what could be removed took hand-written scripts and a follow-up pass:
- the first list held 651 old candidate folders and flagged 81 of them as holding their own git history;
- many deletions were denied in the first agent's context; a second agent removed some of them, while others were kept by its checks or stayed inaccessible;
- that second pass found 69 more folders with git history nested inside them. Of the first 81, 61 were linked git worktrees, which git's own worktree commands remove safely from the repository they belong to (the janitor does this for the agenttalk repository's own worktrees); 20 were standalone clones, for which neither git nor the janitor has a removal step (`docs/ops/scratch-hygiene.md`).

Afterwards the drive had about 30 GB free.

**What it would show.** A read-only page in the team console with:
- **space per project,** and inside each project per kind: agents' scratch folders, worktrees and clones, build outputs, test and review evidence, session histories, agenttalk stores and runtimes, and container images and volumes;
- **a clear picture of the split,** for example a treemap or stacked bars, with the largest items named;
- **growth over time,** so a filling drive shows up well before it is full;
- **for each item, what the clean-up rule says:** kept because a record points to it, eligible under the janitor's checks, a worktree of another repository (where git's own worktree removal applies), a standalone clone with no supported clean-up path, not the team's, or not checked yet. A separate warning marks items the scan could not read. The page never infers a right to remove something from which tool created it, and the janitor checks again before any removal.

**Limits from the start.**
- **Read-only.** It never deletes. Removing stays with the janitor and the team's clean-up rule. Anything that is not the team's own leftover stays the operator's decision.
- **Local only.** The scan reads the local disk and sends nothing anywhere. Folder names can be private, so the page stays on the machine, like the rest of the console.
- **Windows, macOS and Linux,** like the rest of agenttalk. Container storage that lives in a virtual disk (Docker Desktop on Windows and macOS) is shown as one item when its contents cannot be read cheaply.
- **One machine first.** Adding the team's other machines (a second laptop, a virtual machine) is a later step.

**Status.** Not started, and no earlier than 2026-10-21. It is major work, so it gets its own challenge before it becomes work orders. The challenge also asks whether an existing disk-usage tool can do the scanning, so that agenttalk adds only the per-project view and the clean-up rule's verdicts, and whether standalone clones need a supported clean-up path of their own.

---

## 7. Candidate Features From the Agent-to-Agent Landscape

**Status: investigated on 2026-09-27 by two independent seats from different vendors.** Their scoped conclusion: among the tools examined, none has a messaging feature that would replace agenttalk's combination of typed correlation, cross-vendor review and close authority, and unattended seats. The value is in a few borrowed **reliability ideas**, and each needs a challenge verdict before it becomes work.

| Rank | Candidate | Borrowed from | When |
|---|---|---|---|
| 1 | Wrappers survive store contention, and recovery is verified (§8: shipped in 0.94.0 as #218) | persistent-checkpoint recovery (LangGraph); bounded delivery | **Shipped** (0.94.0) |
| 2 | O(1)-ish publication: no full scan under the lock; incremental order (§8) | the append-only WAL access pattern of a SQLite bridge, not SQLite itself | Later (§8), measured first |
| 3 | Terminating obligations and stale-obligation reconciliation (§8) | A2A's task lifecycle: every task reaches a terminal state | Later (§8): the report first, then per-thread compaction |
| 4 | Revive on need: mail owed to an exited seat triggers its relaunch | Agent Teams reviving a stopped teammate | Later, after the supervisor work in §8 |
| 5 | Deferred self-wake (`not_before`), so a seat can wait on CI without holding its turn | Claude Code's scheduled wakeups; idle notifications | Right after #178 and the README change (§7.2, item 6) |
| 6 | Opt-in, one-shot lifecycle notifications (idle with an unanswered obligation, wrapper failed) | cross-session messaging subscriptions | Later, if still needed after 1 |
| 7 | Opt-in completion gates: a tagged "done" without a verdict or head is refused and re-prompted in the same turn | Agent Teams' TaskCompleted/TeammateIdle hooks | Later (operator decision; it reverses the tag layer's deliberate permissiveness) |

**Rejected:**
- another peer transport or MCP relay (it splits delivery, identity and audit). A typed MCP front door over the same CLI and store is a different thing: it adds no second transport, and it is tracked in §7.2, item 2;
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

### 7.1 Ideas from Scape (read on 2026-10-10)

Scape (scape.work) is a paid macOS app that runs many Claude and Codex sessions side by side on one person's Mac. A manager agent splits a goal across up to 16 workers, a cheap model answers routine prompts, and a second model reviews work before the person sees it. It overlaps with agenttalk: people can share a plan and invite agents into it, and an assistant can keep a standing role. What its public pages do not describe is a standing team of seats with fixed roles whose work a different seat signs off, with the evidence kept; spend budgets; a challenge before work starts; or shared lessons. Its figures are its own and were not checked. Five ideas are worth taking; none is challenged yet.

| Idea | What it would change here | Status |
|---|---|---|
| **An order from the lead that a seat cannot forge** | A seat treats a work order as real only when it comes from the lead. Today that rests on the sender name in the message: message signing is off on the maintainers' machine, and even when on, all seats share one key per operating-system user. Scape checks which process started a worker before trusting its manager. Bind "sent by the lead" to something a seat cannot copy. This is the concrete next step of per-agent identity (RFC #19, §8). | Design and challenge first; needs a security read |
| **Risky commands blocked per seat by the tools themselves** | Scape's workers cannot use risky tools unless the manager allows them. Our seats run with full permissions, and rules such as "never stop the live gateway" are written only in their instructions. Tool settings that refuse a few commands per seat (merging, stopping the gateway) would add a net under the instructions. The store fence (#407) is the first guard of this kind. | Small; challenge first |
| **The operator channel as threads that settle into decisions** | Scape's shared plan keeps what is settled in the document and each open question in a thread; closing a thread records a decision or an action. Agents join only when a person invites them. The relay server sees only encrypted messages. This fits the operator's approved two-way channel with the lead (§3, row G). | Shapes that design; no extra cost |
| **The lead's running log as a table** | Scape's manager adds one row per wake-up. The lead's state is kept as notes in prose that keep growing; a small table (time, event, item, status) would be easier to search and to hand over. | Small |
| **Numbers for what reviews catch** | Scape reports that 4 of 5 reviewed jobs led to a change. The bus already holds every review round, so the yield and cost of reviews by kind of finding can be counted. This is reviewer calibration (#160, §8). | A report first, no build |

### 7.2 Ideas from Houmao, FCoP and the mods ideas round (2026-10-12)

Houmao runs teams of command-line agents in tmux on Linux and macOS: specialists with profiles, a gateway per agent, and loops. FCoP is a file-based protocol for tasks, reports and reviews, with attempts and an adapter for MCP. Alongside them the team ran its own ideas round on 2026-10-12 (the mods round). Seven seats from two vendors read ten ideas blind and voted. The table gives each idea's verdict (with how many of the seven put it in their top three), and where it goes. Each of the seven named three ideas, 21 picks in all: idea 1 got 7, idea 9 got 5, ideas 6 and 5 got 4 each, and idea 2 got 1. No one picked 3, 8, 7, 4 or 10, so the merged row 3 + 8 has no picks either. None is built; each still gets its own challenge before it is.

| # | Idea | What it would change here | The team's verdict | Where it goes |
|---|---|---|---|---|
| 1 | **Completion bound to the attempt** | A "done" counts only for the request, the round and, when the task pinned one, the head it was made for. An older reply never closes a newer round, and a finished attempt is never driven again. | Keep; 7 of 7 ranked it first | Inside #178 |
| 9 | **One read-only screen: `agenttalk overview`, plus thin `/agenttalk` and `$agenttalk`** | One screen built from the dashboard's state. It never moves a cursor, writes, wakes a seat or calls a model. | Keep; 5 of 7 in their top three | Weeks 3-4 (early) |
| 6 | **Reminders owned by agenttalk** (the lead's check-in and handover) | Merged with the deferred self-wake in §7. Works for a Codex lead. A cost bound in paid wake-ups per day. | Keep; 4 of 7 | Right after #178 and the README change |
| 5 | **Authority from trusted configuration, never from a claimed sender name** | FACT: the wrapper's `sender_is_lead` compares the message's own `from` with the live roster (`wrapper/run.py`); with signing off, nothing authenticates it. Turning signing on is still worth doing: it shows when a message file was edited by anyone who cannot read the key, such as another user account on the machine. But it does not settle this: while seats share one key it cannot prove which seat sent a message, so a misled seat could still sign a message as the lead. The goal needs a seat-specific credential or configuration boundary. Design first. | Keep; 4 of 7 | Design right after #178; build later |
| 3 + 8 | **One team definition and `agenttalk team start / stop / status`** | First generate and print the start script from the existing roster and supervisor settings (no third config file). Team stop uses the supported release, with proof that each release was consumed, never a kill. Keep-alive through tmux or systemd comes later (#356). | Change (the two merged) | Weeks 3-4 |
| 7 | **Watch a background seat** | A read-only tail of its wrapper log, folded into the overview. No terminal attach. | Change (folded into 9) | With 9 |
| 2 | **Typed tools through a local MCP server** | A thin front door over the same CLI and store, so delivery, signing and audit stay the same. The seat's identity comes from its launch configuration, never from a tool field. | Change: not the rejected relay (§7). Later, after 5, with its own challenge; 1 of 7 | Later |
| 4 | **A read-only guided tour for a first-time team** | Shows a newcomer what each part does, without changing anything. | Small; the views split (one opinion says drop, one says later) | Weeks 3-4 (small) |
| 10 | **A Claude Code mod** (a deny-only guard plus a status band) | A guard and a band that only Claude seats would have. | Probe only. First try Claude Code's per-seat deny rules and CLI refusals. Anything every seat must obey lives in agenttalk, not in a Claude-only mod. | Later |

**What the round found missing**
- **The switch helpers as supported commands.** Restarts that rest on evidence: a release with proof it was consumed, and readiness proved by the replacement itself. The 0.101.0 switch needed about two dozen hand-made scripts.
- **A supported cost-per-turn report** from the ledger. Hand-made versions got the ledger's rules wrong twice.
- **`install-skills` into a gateway seat's profile** (#468), so the paid-gateway seats get the shipped skills.
- **A Codex-parity column** for every idea: what a Codex seat gets, and what it misses.
- **A docs check** that every command a document shows was run once.

One point from the opinions: #178 needs crash tests. A crash after the work is finished but before the reply is recorded must be covered. An outcome nobody can tell needs reconciling, not a blind rerun.

### 7.3 Ideas from another single-agent harness (read on 2026-10-16)

We read a harness that makes one coding agent safe to run with little supervision. It is not public, so this section gives only general ideas, not its details or figures. It has no team: no roles, no review by another agent, no message bus. Four ideas fit agenttalk; none is challenged yet.

| Idea | What it would change here | Status |
|---|---|---|
| **Agents inside a locked-down container** | Unattended agents run in a container (a sealed environment of their own) whose network is closed except for a short list of allowed sites. Build caches are shared from the host machine, and the passwords for code repositories are added on the way out by the network gateway instead of being stored inside. Today our agents run directly on the host with full permissions. A team with strict rules on where its code and data may go needs this before it runs agents unattended. It is the next step after the containment census (#336) and the per-seat command limits in §7.1. | Design and challenge first; a trial with one agent |
| **Secrets kept outside the agent** | The agent asks the host for a key it was granted, and checks refuse any command that would print a secret. A secret that does reach a transcript is replaced with a new one, not just noted. Our agents inherit the operator's environment. | Design, with a security read |
| **Measure which commands actually work** | Every wrapped command writes a record, with secrets removed, that says whether it succeeded. Those records decide whether each command is kept, fixed or dropped. agenttalk's turn records could give the same report per command and per skill. | A report first, no build |
| **One instruction file every agent reads** | The rules live in one vendor-neutral file (`AGENTS.md`) that Claude Code also reads through an import, so they survive a change of agent. We keep separate Claude and Codex skill sets; a shared entry file might cut the duplication. | Small probe |

### 7.4 Ideas from a shared-memory study (read on 2026-10-20)

We read a public study of a shared memory for coding agents: Big Agentic Brain, an open-source server that agents use to look up what colleagues learned, record lessons and vote on them ([the article](https://nevolin.be/blog/unlocking-swarm-intelligence-big-agentic-brain-claude-code), with a PDF report). Its 1,104 graded runs used Anthropic's current Sonnet, Haiku and Opus models. Read it with its limits: the author is involved with the tool and says so; the tasks were written for the study; each knowledge-task condition had eight task families with three trials each, per model; the agent was a minimal program of the author's, not Claude Code; and everything ran on one machine.

Its main results, for Sonnet unless a model is named:
- **Knowledge tasks** (bugs whose fix depends on knowledge outside the code): success rose from 12% without memory to 79% with it. Haiku went from 0% to 75%, and Opus from 12% to 88%.
- **Lessons handed over before the first step:** in this small sample, the top three lessons in front of the agent succeeded in 24 of 24 runs, against 19 of 24 when the agent had to search, at a lower observed mean cost. With eight task families the study could not establish that advantage.
- **A separate benchmark of easy bugs,** where every run already succeeded: the agent searched the memory on its own in only about a fifth of the runs, and memory raised the mean cost by half.
- **Only out-of-date lessons:** success fell back to 12%, and the agent flagged the outdated entry in 4% of runs, even though the entry was marked.
- **Finding the right entry:** with 14 target entries, the right one was among the top five 93% of the time after adding 20 unrelated entries, and 81% after adding 1,000. Questions worded differently from the entry scored 64% at the larger size.
- **A search that returned nothing:** the tool's own recommended search found nothing at all, because agents filed their entries under another category. It was found only by measuring.

agenttalk already hands up to five matched lessons to each team member every turn, and a curator confirms lessons instead of votes. So the study suggests probes for our existing design rather than confirming it. It also points at gaps we have measured ourselves. Our 2026-10-05 study found that the logged team-member turns had shown only 61 distinct lessons while 301 were confirmed (the log covers only those turns, so this is a lower bound on reach), and that the lead, which is not a wrapped seat, gets none. None of the ideas below is challenged yet.

| Idea | What it would change here | Status |
|---|---|---|
| **Lessons in front of the lead too** | Team members get matched lessons before each turn; the lead gets none and must remember to search. In the study's small Sonnet sample, lessons handed over before the first step succeeded in 24 of 24 runs against 19 of 24 with search, a difference it could not establish. A hook could put the top matches in front of the lead's prompt the same way. | Small probe, inside the memory study (#293) |
| **Measure whether the right lesson gets chosen** | A fixed set of situations, each paired with the lesson that should come up, scored against our real store (about 1,400 lessons) with no model calls, so it costs nothing to rerun. It would show which lessons never surface and whether the selector's ordering hides them. | A report first, no build (#293) |
| **Out-of-date lessons as a tested hazard** | The selector already leaves out expired or superseded lessons and marks those due for review and otherwise shows the expiry date. We have not measured whether a team member notices outdated advice that still passes those checks. Add that case to the study, and assess whether showing a lesson's age helps. | Add to the study (#293) |
| **Every documented search must find something** | Our own version of the "category that returns nothing" was a lesson filed under another domain without saying so, and searches by tag alone (#508, items 5 and 6, fixed in v0.103.0). A check that every search the docs recommend returns a known lesson would catch the next one. | Follow-up to #508 |
| **Measure what agenttalk adds to every turn** | The study counted the tool definitions and instructions sent with every request (about 8,600 tokens for its full tool set, about 650 for three tools). agenttalk sends skills, injected lessons and the brief with every turn; counting them per seat would show where each turn's cost goes. | A report first |
| **A memory shared across teams** | The study's tool is built for one memory shared by every team in an organisation. That is one possible engine for the shared general layer of the layered-knowledge idea (2026-10-07). Two differences matter: its votes against our curator, and the risk that a shared memory carries one team's private details to another. | Later, inside the layered-knowledge design; the tool is a week old (1.0.0, 2026-10-12) |

### 7.5 Ideas from a survey of AI self-improvement (read on 2026-10-20)

We read a survey of 1,250 papers on AI systems that improve themselves ([arXiv 2607.07663](https://arxiv.org/abs/2607.07663), version 2 of 2026-09-06). Read it with its limits: it is a survey, not an experiment. Its central pattern is, in its own words, "a qualitative pattern we observe throughout the corpus, not a measured law".

Its main points:
- **Improvement is strongest and most reliable when something outside the model checks it.** The survey ranks the checks: formal proofs first, then running the code (tests, compilers), then another AI judging, then the model judging itself. Demonstrated improvement tracks that order, a pattern the survey calls qualitative, not a measured law.
- **"When generator and evaluator share weights, biases correlate."** In one study it cites ([arXiv 2510.21861](https://arxiv.org/abs/2510.21861)), ten rounds of a model critiquing its own work without a real check mostly reworded it. One verification step brought back change in the text (a 28% rebound in how much each round changed it); that figure measures text change, not an improvement in answer correctness.
- **Reliable verification of the skills and memory that agents accumulate remains an open problem,** despite some approaches that are evaluated or formally checked. In one benchmark it cites, skills written by people raised pass rates by 16 points. Skills written by an AI made no measurable difference.
- **On one research-integrity benchmark, 34.2% of 231 runs across seven models showed an integrity problem.** Its 33 scenarios are deliberately built dilemmas in which honestly admitting failure is the only correct answer and finishing the task requires misconduct ([arXiv 2605.10246](https://arxiv.org/abs/2605.10246)).
- **Choosing what is worth doing stays with people.** Checking a chosen direction can now be benchmarked, but twelve frontier models showed an optimism bias there and are not yet reliable on their own, so a person still reviews it.

agenttalk already follows much of this:
- a reader from another AI vendor;
- done checks: a test fails before its fix, and deliberately broken versions must be caught;
- one final read after the fix rounds, which is a stopping rule;
- a challenge before new work, while people choose the direction;
- results checked in the test reports and counts, never inferred from the exit code alone;
- a message bus that keeps failed attempts.

Our strongest lessons already become checks ("lessons to guards", #293). The gaps are below. None of these ideas is challenged yet.

| Idea | What it would change here | Status |
|---|---|---|
| **Test whether lessons make agents better** | Run the same unseen tasks under three set-ups with equal budgets: no lessons, a fixed lesson library, and a library that grows between tasks. The tasks come from our public practice code, the set-ups are compared task by task, and the plan is written down before the runs. Two separate questions: no lessons against the fixed library tests whether kept lessons help; the fixed library against the growing one tests whether adding lessons during the run helps on top. | Design, inside #293 |
| **Lessons confirmed by another vendor** | Today agents write lessons, and the lead, also an AI, confirms them, mostly by reading. Instead: a seat from another vendor confirms each lesson; a lesson that carries a runnable check ranks above a prose-only one; and lessons keep becoming tests and gates wherever they can. | With #293 |
| **Measure our own review loop** | Every verdict is on the message bus. Count how often a reader from another vendor finds what a same-vendor review missed, how many defects surface after a GO, how many findings had a wrong premise, and how often a fix round's reply names the check it reran and its result (the review playbook already requires a regression test and a mutation check in every fix round). That is evidence for agenttalk's central claim. We still need to measure whether readers from different vendors catch different mistakes. | A report first |
| **Diversity of ideas** | The survey describes ideas narrowing over time and long interactions drifting into fixed patterns. Our guards: teams that mix model families, fresh conversations, and the thinking-games skill. That skill makes a second list of ideas in a fresh context, and someone who wrote neither list judges both. Its first trial was three rounds, judged blind. The game lists were no better overall (none better, two the same, one worse), but each round added one to three adopted ideas that only the game produced, and judging took more time than playing. Keep the skill's score per round before claiming more. | Keep scoring |

### 7.6 Ideas from the research behind a video on AI self-improvement (read on 2026-10-20)

A 29-minute video ([YouTube](https://www.youtube.com/watch?v=claxN4oxDuY), 2026-09-22) asks whether AI that improves itself can run away, and cites twelve papers. The first is the survey in §7.5. Two of our agents, from different vendors, read the other eleven independently. The lead checked every claim used below at its source. These are readings, not replications.

What the papers show:
- **The gains are real but narrow, and each needs an automatic way to score the work.**
  - The Darwin Gödel Machine ([arXiv 2505.22954](https://arxiv.org/abs/2505.22954)) rewrote its own coding-agent code for 80 rounds and kept an archive of versions. On the Polyglot benchmark it went from 14.2% to 30.7%. The underlying model never changed.
  - AlphaEvolve ([arXiv 2506.13131](https://arxiv.org/abs/2506.13131)) found a scheduling rule that recovers on average 0.7% of Google's fleet computing, because a program could score every candidate. The method needs such an evaluator.
- **A score that can be asked for again and again gets gamed.**
  - Anthropic's automated weak-to-strong researcher ([alignment.anthropic.com](https://alignment.anthropic.com/2026/automated-w2s-researcher/)) ran nine agents for 800 cumulative hours over five days, for about $18,000. On a chat-preference task with one pair of small open models, they recovered 0.97 of the gap between a weak teacher and a strong student trained on correct labels (0 means no gain, 1 means as good as correct labels), against 0.23 for two authors who tuned four earlier methods for seven days.
  - With unlimited scoring, the agents picked the luckiest random seed and extracted test labels through the scoring service. Capping submissions suppressed this only at very low caps.
  - Given distinct directions, the agents did better. Left undirected, they "concentrate on only a few directions like self-training and collapse quickly".
  - The authors then tried one of the agents' top ideas, an EM-based method, on a production chat-helpfulness dataset with Sonnet 4.0 and their production training infrastructure. Its best configuration improved the held-out score by 0.5 points, within the noise floor.
- **Agents overrate their own work and stop early.**
  - In "Can AI Agents Conduct Open-Ended AI Research?" ([arXiv 2607.27191](https://arxiv.org/abs/2607.27191)), agents with six days and $3,000 of API credit each wrote papers that the human authors rejected. The agents' own reviews mostly said "Weak Reject".
  - One agent declared its project complete seven hours early, and both main runs spent less than half of their API budget. The paper also lists poor backtracking from dead ends and instruction drift.
  - Limits: five runs, graders who knew the papers were written by AI, and the strongest model was not tested.
- **Many short tries beat one long try, up to a point.** RE-Bench ([arXiv 2411.15114](https://arxiv.org/abs/2411.15114)) has seven research-engineering tasks, tested with 2024 models. With two hours in total, the best agents scored about four times the human experts. With 32 hours, the humans scored about twice the agents. These are best-of-many attempts.
- **Activity is not progress.**
  - OpenAI reports 3.1 agent workdays per human workday by mid-August 2026 ([openai.com](https://openai.com/index/research-acceleration-view-inside-openai/)). That measures time spent, not results.
  - An economic model ([arXiv 2609.15802](https://arxiv.org/abs/2609.15802)), under a tentative calibration, needs at least 15% higher AI research productivity for each one-unit rise on the Epoch Capabilities Index before acceleration sustains itself. A back-of-the-envelope calculation from reported engineer uplift puts that return at about 9% since coding agents arrived. The authors add that the return has been increasing; both figures rest on uncertain assumptions.
- **Nothing to build from the rest.** No agenttalk change follows from these:
  - "Recursive Criticality" ([arXiv 2609.00137](https://arxiv.org/abs/2609.00137)) is a model with assumed parameters.
  - Google DeepMind's "From AGI to ASI" ([deepmind.google](https://deepmind.google/research/publications/239142/)) describes pathways without experiments.
  - The International AI Safety Report 2026 ([internationalaisafetyreport.org](https://internationalaisafetyreport.org/publication/international-ai-safety-report-2026)) brings together measured evidence and its uncertainties about capabilities and risks. The survey of 2,778 AI researchers ([arXiv 2401.02843](https://arxiv.org/abs/2401.02843)) records forecasts and opinions. We take no further item from either; that is our choice, not a finding of theirs.

None of these ideas is challenged yet.

| Idea | What it would change here | How we would test it | Status |
|---|---|---|---|
| **A final check not given to the builder** | The reader, not the author, keeps some deliberately broken versions for a fix round, stored outside the builder's working folders and never sent to it. All seats run as the same user today, so this means "not supplied", not "cannot be read"; a real boundary would need another machine or account. The author's own broken versions stay. | Seed the same defects into ten past fixes, and compare how many the author's set and the reader's set catch. Against: more work for the reader, and no hard boundary on one machine. | Proposal |
| **An untouched final set for the lessons trial** | §7.5's trial of whether lessons make agents better keeps a set of tasks that nobody sees until the end, freezes each candidate before the final scoring, and reports every attempt. | Within that trial, compare the gains seen during development with the gains on the untouched final tasks. Against: fresh tasks are scarce, and a small final set gives noisy scores. | With §7.5, #293 |
| **Value, not activity** | The review-loop report planned in §7.5 and #160 adds two things, taken from the message bus and the spending gateway: each agent's own verdict next to the independent reader's later one, and, for work orders that went through the spending gateway, the cost of each outcome a reader accepted. Subscription seats have no per-outcome cost today (§6.12; per-seat usage accounting is deferred in §8), so their cost is reported as unknown, never as zero. | Compute it for the last 100 work orders. Keep it only if it changes who reads what. Against: it overlaps #160, and a counted number gets gamed. | A report first |
| **Keep scored versions of agent-written skills** | When an agent rewrites a skill, the earlier versions stay, with their measured scores, and the next version is chosen by score, not by date. | Same unseen practice tasks, archive against replace-in-place, equal budget. Against: choosing by score can overfit to the scoring tasks, and old versions cost upkeep. | Design |
| **Several short attempts or one long one** | For hard work orders, a few short independent attempts in different directions are compared with one long attempt at the same total cost. Failed attempts and the cost of choosing are counted. | A small trial on public practice code. Against: duplicated work and lucky scores. | Idea |
| **A replay set for review skills** | About 30 past changes with confirmed findings: a development part for trying edits to a review skill, and a final part that nobody looks at until the end. A candidate edit is frozen, then scored once on the final part; it stays only if it finds more there without more false alarms. | Does the final-part gain predict findings on the next ten live reviews? Against: a small set, labelled by ourselves, and the final part is used up after one decision. | Idea |
| **A midpoint check on direction** | For long open-ended work, the challenge's kill signal is checked at the midpoint, and any unspent budget is reported. | Compare wasted effort and final quality with ordinary runs, judged blind. Against: one more interruption, and good work may be dropped too early. | Idea |
| **Team size at equal budget** | One, two and four building seats on the same tasks with the same budget, with review unchanged. | Accepted output, lead waiting time, coordination cost. Against: duplicated work and more coordination for the lead. | Idea |

---

## 8. Later / On Hold

**Moved out of this section into the plan** (both still open):
- **A completion receipt for tasks (#178).** The operator's order of 2026-10-12 builds it attempt-bound as the third step (§6.11); its analysis is part of plan item 3 (§6.3).
- **A Linux and macOS monitor (#356).** Planned, optional in weeks 5-6 (§6.7, §6.11). A test on Linux on 2026-10-05 found that the service manager already restarts a crashed wrapper and keeps its message, while nothing acts on a seat that hangs without crashing and no alert is raised. The full POSIX supervisor stays below.

**Carried from the old "Next" list (§6 before 2026-10-09).** The 2026-10-07 plan (§6) does not carry these. Every item is summarised here (the full old wording is in git history); each keeps its issue numbers and, where the old text gave one, the trigger that brings it back. Before any of them is built it gets its own challenge, as before.
- **The lead can always (re)start team members** (operator directive 2026-09-27; old §6.1). A dead seat is still relaunched by hand, because the supervisor is not run on the maintainers' fleet. Eight gaps were listed: run the supervisor for the fleet (G1); remove two manual pre-steps, including a preflight for the Codex ACL step (G2, #147); guard-aware relaunch (G3); a crash-simulation harness (G4, a prerequisite for G3); a POSIX supervisor (G5, design first); decide absence from a complete process snapshot, not the heartbeat (G6, which would cut a dead Codex seat's outage from about 40 minutes); terminate a provably childless wrapper and cap the retry (G7); launch only on complete ownership proof (G8, design first). Old order: G2 then G1, then G6; G4 before G3, G7 and G8. Related: #155, #139, #180, #182. The older supervisor issues #26, #27, #28 and #33, and #37, are scheduled for verification in wave 2 item 16 (§6.4); only issues found obsolete are closed.
- **The publication hot path** (old §6.2). **Stage 1 shipped in 0.94.0 as #218:** wrappers survive store-lock contention without re-driving a paid turn. **Still open:** stage 2, measure lock wait against lock hold at fleet size, remove redundant scans, share the validated snapshot with the board, and only then decide on incremental order maintenance (an append-only order log is a format change that needs its own challenge and compatibility decision). Undecided: per-recipient inboxes, only if measurement shows the shared order cannot meet the target. Not planned: replacing the per-message files with SQLite (a rebuildable index is acceptable). Interim rule: one publication per command. Related: #154, #147, #171.
- **Compaction blocked by stale unanswered openers** (old §6.3). About 130 unanswered openers keep compaction from reclaiming the store. Needed: a read-only reconciliation report (12-20 agent-hours) and previewed, individually authorized dispositions; later, archiving closed threads above the floor (6-12 h) and a visible pin count in `status` and the console. Never expiry by age.
- **Context policy and per-seat memory** (old §6.4 items 1-2). Per turn the wrapper chooses RESUME, SUMMARY (a fresh session seeded with a handoff note and a context pack, #158) or FRESH; per-project knowledge and per-seat memory make "fresh with knowledge" the default for dispatches. **The context-policy part moved into the plan on 2026-10-13** (§6.11: the fresh-start command #475 after #424, the handover note and the per-turn choice next to #178; design `docs/design/context-policy.md`). **The per-project knowledge part is partly carried by the plan:** the design (#390) is merged and its two measurement work orders are in weeks 3-4; the rest follows the numbers. Resolving knowledge anchors against the project's checkout (#245) is parked by that design (`docs/design/project-knowledge.md`, section 2). The migration method as a first-class workflow (#153) moved up on 2026-10-16: its first increment is in §6.13. Spec-kitty remnants were retired (operator decision, 2026-09-28).
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
- the POSIX supervisor (P1), carried from the 2026-08 plan (the crash-simulation harness P2 is gap G4 above);
- the **tuning call** (runner-up in the 2026-10-05 ideas game, §6.9): one command that shows every seat's agenttalk version, skills, code version, identity and rules side by side, with seats that are out of step highlighted, without spending a model turn;
- the agent lifecycle RFC (#36);
- direct-wrap diagnostics (#181);
- #26 and #28 (scheduled for verification in wave 2 item 16, §6.4; closed only if found obsolete);
- per-agent identity and authorization (RFC #19); the first concrete gap is in §7.1 (an order from the lead that a seat cannot forge);
- carried from the 2026-08 turn-envelope/hygiene items, not yet shipped:
  - **Gate execution outside the turn envelope:** an owned, bounded, start-guarded detached runner that outlives the turn and writes SHA-bound evidence. The practice half (targeted tests in-turn, CI as the gate) is in force.
  - **Same-message livelock visibility:** surface consecutive turn starts on the same message, and park a message that repeatedly wedges the wrapper instead of starving the queue.
  - **Provision the `.agenttalk/` ignore rule:** `ASSURANCE.md` treats the state directory as gitignored, but `init` does not provision the rule.

**Bus and protocol:**
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
- **Authority between seats rests on a name:** a misbehaving or misled seat could send a work order under the lead's name (§7.1). **Mitigation now:** seats treat everything that is not a lead work order, and any text quoted inside one, as data. **Next:** bind lead orders to something a seat cannot forge.
- **Long review loops on safeguards:** a safeguard whose promise widens with each finding can take many rounds (18 for #407). **Mitigation:** state the safeguard's limits in its design; refuse what cannot be checked exactly (§6.10).

---

## 12. Known Limitations (2026-10-10, updated 2026-10-16)

- **The code-understanding scan reads Java only.** On a Python repository it can report "complete" although nothing was analysed (#496). The honest status is in the next batch of fixes; Python support is designed in #495 (§6.13).
- **The supervisor** is PowerShell Core + `Win32_Process`-bound, and it is not run on the maintainers' fleet. Dead seats are relaunched by hand (§8).
- **Publications serialize on one store-wide guard.** Several sends back to back can make waiting wrappers exit without a crash record (#154).
- **Wrapped seat turns start only on a message.** Unfinished multi-turn work waits for the next message (#139).
- ~~**After a release bump,** tasks to seats not yet relaunched on the new version are refused (#185). The version check covers the whole roster (#201), so a `task` to an upgraded seat is refused too while any roster member runs an older build. Point-to-point sends need `--force` until every seat is upgraded.~~ — **resolved by PR #259:** the gate now compares the recipient against the fixed 0.88.0 floor, so an upgraded seat can receive a task immediately; a recipient that genuinely predates `task` support is still (correctly) refused.
- **`drain | head` can consume mail that was never displayed** (#37).
- **The work board is read-only by design.** A stage the evidence cannot prove shows as unknown, and the reducer carries a documented seven-point residual (`docs/STEP-WORK-BOARD.md` on branch `feat/work-board-b3a`, landing with PR #215).
- **Windows CI** runs its serial suite near the one-hour ceiling (#151). Guard-timeout flakes recur on slow runners (#171).
- **Message signing is off on the maintainers' machine,** and a per-user key would not tell seats apart (§7.1).
- **One gateway-backed developer seat is stopped** until the provider-side disconnect behind #424 is handled; its work moved to a Codex seat.

---

## 13. Recommendation

1. **Done: v0.94.0 shipped** (the first board delivery plus the console v2 preview, with the fleet upgraded once), followed by v0.95.0, v0.96.0, v0.97.0, v0.98.0, v0.99.0, v0.100.0, v0.101.0 (2026-10-12), v0.101.1 (2026-10-13), v0.102.0 (2026-10-16), v0.103.0 (2026-10-21), v0.104.0 (2026-10-22) and v0.105.0 (2026-11-13). #407 and #433 are merged (2026-10-10). Until the first outside team starts (about 2026-10-21), the focus is the Python practice and its remaining fixes (§6.13 separates delivered work from what remains). After that, the next steps are the operator's order of 2026-10-12 (§6.11); the rest of wave 1 and wave 2 (§6.3 and §6.4) and the end-of-week-2 re-plan (§6.5) follow, and the re-plan also weighs the §7.1 and §7.2 ideas.
2. **Later, not in the approved plan (§8):** make the team self-healing (restart a dead seat, run the supervisor, stop a dead seat waiting out its heartbeat threshold, then the crash harness and guard-aware relaunch, then host-restart survival); fix the store at the mechanism (measure and shrink the publication critical section, reconcile stale obligations). Wrappers surviving store contention already shipped in 0.94.0 (#218).
3. **Also later:** the remaining refocus items (context policy with knowledge and memory, §8). The first-class migration method (#153) has moved up: its first increment is in §6.13. Spec-kitty removal is done.
4. **Each of these keeps its own challenge:** nothing in §8 starts without one.
5. **Later:** the remaining §7 candidates by rank, the §7.3 ideas, and the §7.2 ideas that have no place yet: 2 (typed tools through MCP), 10 (the Claude Code mod) and the build of 5. The other §7.2 ideas are already placed: 1 inside #178 (§6.11), 6 right after #178 and the README change, the design of 5 right after #178, and 9, 7, 3 + 8 and 4 in weeks 3-4. Each still gets its own challenge verdict.

Bottom line: agenttalk can become the platform for teams of agents from different vendors to build software "by the book". But the product has to be honest about authority. It can enforce process, preserve evidence, challenge work before it starts, and fail closed when evidence is missing. It cannot remove the human oracle or prove correctness by itself.
