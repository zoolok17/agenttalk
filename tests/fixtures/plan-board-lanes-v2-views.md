# Plan: board lanes and the v2 team views

Plan id: board-lanes-and-the-v2-team-views
Plan revision: r2 (supersedes r1) · Status: **Phase 1 approved by the lead 2026-09-30 13:55Z** under the operator's decisions below; Phase 2 is gated (see r2 changes) · Owner: claude-agenttalk-lead

## r2 changes (2026-09-30, from two challenges, both RESHAPE)
- ch-00d9f8a9-1379-4ada-863d-3217fa86169b (reviewer-3): RESHAPE, medium, reasoned, exposed=no. The facts command has no trigger, so Done would silently go stale. The plan deviates from design section 4 (an in-server cached observer) more than it says, so it needs a design-doc amendment. The cheapest option: ship the lanes now and defer the v2 views until #239 is understood.
- ch-11185e34-19a7-4c62-bf2f-1362e6028e8b (dev-5): RESHAPE, medium, verified, exposed=yes, so formally unassessed; substance adopted.
  - A facts file cannot see a later target movement, so bind each observation to project, repo alias, candidate and target OID, with an expiry and a named refresh owner. Missing or expired evidence suppresses Done without erasing the known stage.
  - /api/messages and /api/thread scan everything and return whole bodies, so "existing APIs + client paging" is not bounded.
  - The plan import needs a portable, versioned contract.
- **Disposition (adopted):**
  1. wb-done-facts: every fact records project, repo alias, candidate OID, target ref, target OID and checked_at. A fact older than a configured freshness window (default 24 h) is STALE: the card keeps its derived column and shows "integration evidence stale". Refresh owners: the lead's merge ritual runs `verify-merges` right after every merge, and the lead's tick runs it every tick. The item also delivers the design-doc amendment for section 4 (in-server observer replaced by a lead-run command).
  2. wb-planned-lane: a versioned import contract (schema version, plan id, plan_rev, row identity = plan id + work_item). Re-import replaces a plan's rows atomically. Conflicts across plans are shown as Unknown. A dispatched item always wins over a plan row. Planned cards count inside the existing 100-card and 256 KiB bounds, and active work is never displaced by planned rows (planned rows are cut first).
  3. **Phase 2 is gated** on the #239 fix being merged (exp 5 REPRODUCED the growth with a real browser at about 600 MB/min) and on a fresh challenge of the Phase 2 rows.
  4. v2-history now explicitly includes a bounded backend: paged, root-scoped message and thread endpoints. Its acceptance thresholds (response bytes, scan work, retained memory over repeated navigation) are set BEFORE implementation.
Operator decisions behind it (2026-09-30):
- Done lane: option (a), a separate command that records a verified list of merges, checked against git, which the board reads;
- the Planned lane from plan files: yes;
- "I miss the team overview, captured learning, chat history of the team and the members, team members profile pages with names, work, avatars, etc".

> Progress is NEVER tracked here. It is derived from the bus (tagged dispatches, typed replies) and the work board.

## 1. Outcome
The board shows every lane:
- **Planned:** plan rows that have no dispatch yet;
- **Queued, Building, Review, Fix and Ready:** as today;
- **Done:** only for work whose reviewed head is PROVEN merged into the configured target branch.
The proof is made outside the web server, by a lead-run command, so the server never runs git.

The v2 console (/v2) gains four views next to the Conversation and the Board:
- a team overview;
- a profile page per member (name, avatar, role, CLI/model, health, current work, recent messages, lessons);
- the team's chat history, with a per-member filter and a thread reading view;
- the captured learning.

Everything stays read-only (GET only, CSP kept, bodies rendered as text) and runs on live data.

## 2. Scope
In:
- the out-of-server facts command (verify merges, import plan rows);
- the server reading the facts file;
- the Planned column in the reducer;
- the lanes in the board view;
- the four v2 views over the EXISTING snapshot APIs (/api/state, /api/messages, /api/threads, /api/thread/<id>, /api/learning, /api/work-board).
Out, for a later revision:
- squash/rebase merge proof (design section 4: unproved in v1);
- PR imports or any network call;
- actions or writes from the console;
- the budget meter, phone layout and two-machine views (design 10-BUILD-PLAN step 9);
- configuring the model-vendor map (a lead config step, not code);
- the #239 leak fix (investigation separate, tk-565b1591d5a8).

## 3. Approval envelope
- **Scope:** sections 2 and 5 as written. They implement the operator's 2026-09-30 decisions inside the already-approved work-board design (docs/DESIGN-work-board.md on design/work-board, sections 4-5) and the console v2 design (design/console-v2 on master).
- **Budget:**
  - effort 22-34 agent-hours;
  - paid model spend none (no Qwen seats);
  - Codex credits for about 8-12 reads plus one challenge;
  - wall time 4-6 days including CI.
- **Delegated to the lead:** sequencing, same-vendor-rule seat substitution, fix rounds within the stopping rule.
- **Needs a new approval:**
  - any server-side git or network call;
  - any console write or action;
  - scope beyond section 2.
- **Design deviation, recorded:** design/console-v2 03-SPEC-APP section 7 puts inherited surfaces into stream items and a drawer, "not as separate dashboards". The operator's 2026-09-30 ask for profile pages and views overrides this. They become NavRail views (the design system's NavRail component), in the v2 frame and themes.

## 4. Assumptions and unknowns
- **FACT:** the reducer already places Done from `integrated={(work_item, candidate_head): fact}` (work_board.py, around line 505). The feed never passes it, so Done is unreachable today.
- **FACT:** the board columns are needs_you, done, fix_round, building, independent_review, ready, queued and unknown. There is no planned column.
- **FACT:** the v1 console (/) has the overview, agent, learning, sessions and lead-chat views on the APIs above. v2 has only #conversation and #board. There are 30 avatar PNGs in web_static/avatars. The name-shortening rule is in design/console-v2.
- **FACT:** this repo merges with merge commits, so ancestry proves integration. Only dispatches since 2026-09-29 carry a full work_head, so older items have no candidate and stay Unknown. That is correct, not invented.
- **FACT:** draft PR #238 computed git facts INSIDE the server. dev-5 found 4 majors against design section 4. This plan supersedes #238, which will be closed with a pointer here.
- **ASSUMPTION:** the facts file can use the store's existing lock and atomic-write helpers.
- **UNKNOWN:** do other projects' plan files follow the template's section 5 table? Probe: the import refuses a file without that table, with a clear message. The trial imports this file.
- **UNKNOWN:** the per-member model/vendor source (dispatches show `assignee_model_vendors` 'unverified' for every seat). Probe: v2-team shows the CLI from the roster, and "model not configured" otherwise.
- **UNKNOWN:** #239, the serve memory growth seen on 2026-09-29. New views must NOT add pollers (they reuse the existing cadence and fetch detail on navigation). Probe: exp 5's result before v2-team merges. Until #239 is resolved, the lead runs `agenttalk serve` only while the operator is watching.

## 5. Work items
Dispatches carry `--meta work_item=<slug> --meta stage=<stage> --meta plan_rev=r2`. Implementation briefs cite the design doc and section.

| Phase | work_item | Owner (vendor) | Reviewer (other vendor) | Starts when | Completion evidence | Estimate (incl. review/rework) | Stopping rule |
|---|---|---|---|---|---|---|---|
| 1 | wb-done-facts | claude-agenttalk-developer-2 (claude, Opus) | codex-agenttalk-reviewer-1 (codex) | challenge disposed | `agenttalk board verify-merges`, lead-run, out of the server. It follows the design section 4 observer rules: the alias allowlist in config, a hardened git env, rev-parse, merge-base --is-ancestor and cat-file only, a 2 s timeout, and invalidation on branch movement. It writes verified (work_item, candidate, target ref, target OID, as-of) facts to a store facts file. The server loads the file (bounded, schema-checked; malformed or missing = Unknown) and passes integrated=. Evidence: a real merged item shows Done on a copy of the live store; hostile-ref, shallow and timeout tests; PR merged; #238 closed | 5-8 h | 2 FIX rounds, then recast |
| 1 | wb-planned-lane | claude-agenttalk-developer-6 (claude) | codex-agenttalk-developer-4 (codex) | wb-done-facts merged | `agenttalk board import-plan <file>` records section 5 rows (work_item, phase, owner, starts-when, plan name, plan_rev) in the same facts file. A plan row with no dispatch goes to a new planned column; a dispatched item keeps its derived column; a re-import that drops a row removes it; a superseded plan_rev is replaced. Tests; PR merged | 3-5 h | 2 FIX rounds |
| 1 | wb-lanes-ui | claude-agenttalk-frontend-dev (claude) | codex-agenttalk-developer-4 (codex) | wb-planned-lane merged | The board view shows Needs-you, Planned, Queued, Building, Review, Fix, Ready and Done. Done uses the design section 5 seven-day window, with the as-of time shown. Node fixtures from real envelopes; PR merged | 2-3 h | 2 rounds |
| 2 | v2-team | claude-agenttalk-frontend-dev (claude) | codex-agenttalk-reviewer-1 (codex) | wb-lanes-ui merged + the #239 fix merged + the Phase 2 challenge disposed | NavRail views. #team overview: an avatar grid with short name, role, CLI/model, health, current item and column, and last activity. #member=<name> profile page: the same plus the member's board cards, recent messages and threads, and their lessons. No new poller. Node fixtures; PR merged | 5-7 h | 2 FIX rounds, then recast |
| 2 | v2-history | claude-agenttalk-frontend-dev (claude) | codex-agenttalk-developer-4 (codex) | v2-team merged | #history: the team timeline, paged and newest first, filtered by member, kind or work item. #thread=<id> reading view. Bounded pages; bodies as text only. PR merged | 4-6 h | 2 FIX rounds |
| 2 | v2-learning | claude-agenttalk-developer-6 (claude) | codex-agenttalk-reviewer-1 (codex) | v2-team merged | #learning over /api/learning: lessons with author, date, tags and linked item, plus search and filter. Profile pages link to the member's lessons. PR merged | 2-4 h | 2 rounds |
| 3 | v2-walkthrough | claude-agenttalk-lead | the operator (acceptance) | all rows above merged | The operator walks through live data. The lead starts serve only while the operator watches, and stops it by PID afterwards. Findings filed as issues | 1 h | none |

Assignment rules as in the template. Owners are re-resolved against the live roster at dispatch time.

## 6. Merge order and shared files
- **Order:** wb-done-facts, then wb-planned-lane, then wb-lanes-ui, then v2-team, then v2-history and v2-learning (parallel).
- **The facts file** (format and loader) is created by wb-done-facts. wb-planned-lane only adds its own section.
- **console2.js router/nav:** v2-team creates the NavRail and the router. v2-history and v2-learning each add their own view module plus one router entry. The second of them resolves a small router conflict.
- **CHANGELOG:** under [Unreleased]. If ### Added is missing, create it directly under ## [Unreleased]. Insert at the START of the list, and never edit a released section. At the merge gate, run changelog_released_guard.py on every head.
- **CI:** about 2 h of Windows CI per merge (3 h ceiling). Budget one merge per half-day.

## 7. Challenge
See the r2 changes at the top: two challenges (reviewer-3 exposed=no; dev-5 exposed=yes), both RESHAPE, substance adopted. Dispatches carry challenge=ch-00d9f8a9-1379-4ada-863d-3217fa86169b and plan_rev=r2. Phase 2 gets a fresh challenge before dispatch.

## 8. Replan rule
As in the template.

## 9. Recovery checklist
As in the template. The Planned lane is the dogfood: once wb-planned-lane merges, import this file, and the board must show every undispatched row as Planned.
