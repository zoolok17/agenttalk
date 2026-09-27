# agenttalk - Product Roadmap & Feasibility

**Status:** Official · **Owner:** lead (operator-facing) · **Last updated:** 2026-09-27 (UTC)
**Audience:** maintainers, operators, and agents deciding what to build next.
**Horizon:** the next two releases in detail, then a ranked "next" tier and a labelled "later" tier.
**Current shipped baseline:** v0.93.0 (2026-09-27). `CHANGELOG.md` remains the release-history source of truth.

**Platform requirement:** agenttalk must run on **Windows, macOS, and Linux**. The Python core (bus, store, CLI, wrapper) is cross-platform and CI-tested on all three (Windows/macOS/Ubuntu × Python 3.10–3.13). The model gateway has a Windows scheduled-task backend and, since v0.91.0, a Linux systemd `--user` backend. **The supervisor is still the open platform gap:** it needs PowerShell Core 7+ and the Windows-only `Win32_Process`. A POSIX supervisor path is unbuilt (§7, §9).

Companion docs:
- `docs/DESIGN.md` (why / architecture);
- `docs/ASSURANCE.md` (per-release attestation);
- `docs/ISSUES.md` (living tracker and known limitations);
- `docs/DESIGN-work-board.md` (the work board, #207);
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
| **A. Core Coordination Bus** | Roster, typed messages, threads, requests, work orders, review handoff, broadcast, transcripts. | Product-grade. `task`/`task-response` work orders (0.88). Validated work-item tags and reply verdicts (0.93). **Known scale limit:** one store-wide publication guard (§6.2). |
| **B. Native Work & Evidence Spine** | Work items, workspaces, evidence, review bindings, gate/check state, delivery lifecycle. | **In build as a derived work board (#207).** Cards are projected from bus events tagged with `work_item`/`stage`; there is no hand-kept state. Tags and verdicts shipped in 0.93; vendor records and a shared snapshot are on master; the reducer and feed are in review. |
| **C. Method Engine Adapter** | External planners create or link work; agenttalk owns execution evidence. | **Being retired in its spec-kitty form.** The remnants are due for removal, and the migration method becomes first-class instead (§6.4). |
| **D. Craft Skill Pack** | Coding, review, test, QA, security, release and lead skills. | Useful and growing. Challenge skill (0.93); lead skills aligned with the work-item protocol (0.93); the assurance-scan and review lenses. |
| **E. Assurance & Release Governance** | Gates, closes, specialist sign-off, acceptance, scan evidence. | **Strong.** Cooperative acceptance passes on `close` (0.92). A pinned, offline-checked tool registry with `close acceptance preflight` (0.93). |
| **F. Knowledge & Codebase Memory** | Domains, pointer notes, lessons, onboarding runs, the comprehension plane. | Primitives exist. Comprehension producer slice 1, a static inventory plane (0.87). The per-project knowledge and per-seat memory design is drafted (§6.4). |
| **G. Operator Control Plane** | Local console, read views, attention/liveness, lead chat. | The console reports risk honestly (0.86). **Console v2 preview in review (#214).** The work board UI is next (§5). Plan review is on hold (#206). |
| **H. Execution Runtime** | Wrapper, supervisor, session continuity, dead-letter, lanes. | The wrapper is far more reliable (0.84–0.91): replies are owned by the wrapper, failures are loud, redelivery is honest. Store backup (0.89). **Dead seats are relaunched by hand**, because the supervisor is not run on the maintainers' fleet (§6.1). The POSIX gap remains. |
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

---

## 5. Now: the v0.94.0 Line

**Theme: a work board that moves itself.** The first delivery of #207, together with the console v2 preview it renders in. The acceptance test is the operator's own walkthrough on live data; there is no scripted demo.

| Item | What | State (2026-09-27) |
|---|---|---|
| Board data layer | Vendor records and escalation links (B2b, #212). A shared per-root envelope snapshot that includes compacted archives (B4a+B4s, #213). | **Merged to master** |
| Pure causal reducer (B3a+B3b) | Derives each card's stage from bus evidence, with a read-side audit. A stage the data cannot prove shows as unknown. It stops with a documented residual rather than chasing edge cases, unless a realistic false Ready/Done is found. | Reviewed GO; CI (PR #215) |
| Bounded feed + gate isolation (B6a) | Active cards plus 7 days of Done, capped by count and size with overflow visible; zero message-file reads on the HTTP thread. Board gates never block an ordinary merge check. | Cold read GO; PR next |
| Console v2 preview + board UI | The v2 preview at `/v2` (#214). Then the shared "needs you" projection (B7) and the console model, cards and walkthrough (B8). GET only, text-only rendering, and no stale state shown as live. | #214 reviewed, CI; B7/B8 after #214 and B6a |

**Fleet upgrade:** the maintainers' seats skip a 0.93.0 runtime and upgrade **once, to 0.94.0**: one runtime build, one pin change, one full-fleet relaunch.

**Deferred board slices** (designed, not scheduled for 0.94.0):
- lead-authorized rescind (B2c);
- a disposable archive index and indexed hydration (B4b/B4c);
- explicit board-gate cleanup (B6b);
- a restricted local Git observer for integration evidence (B5);
- the read-only half of plan review, after the first delivery: the pinned document with copy-with-reference (D1), "changed since you looked" (D2), and finding marks once finding ids are stable (D3).

---

## 6. Next

Ranked. Each is major work under the challenge rule: it gets an independent challenge verdict before it becomes work orders.

### 6.1 The lead can always (re)start team members *(operator directive 2026-09-27)*

The team should be as autonomous as possible, so the lead must always be able to restart a dead seat, either directly or through the supervisor. Today a dead seat stays dead until someone relaunches it by hand.

**Findings (investigation, 2026-09-27).** The supervisor's decision core already covers most of this:
- relaunch with backoff, and a readiness give-up that stops after three never-ready relaunches;
- a fail-closed barrier that proves no wrapper for the seat survives before it starts a replacement;
- an audited `request-restart` verb that only the lead or liaison may use;
- config rows for every current seat kind: Claude Code, Codex CLI, and gateway-backed seats declared as Claude seats.

The gaps are operational and platform-related, not missing logic.

| Gap | Size | Note |
|---|---|---|
| G1. Run the supervisor for the maintainers' fleet | ~1 agent-hour | Choose a quiet window; per-CLI staleness thresholds apply (180 s Claude, 2400 s Codex). |
| G2. Remove the two manual pre-steps | 2-4 h | A preflight for the Codex ACL step (#147); re-add the one seat the config marks unmanaged. |
| G3. Guard-aware relaunch | 4-8 h | Do not relaunch a wrapper killed by a publication-guard timeout straight into the same contention (§6.2). |
| G4. The supervisor crash-simulation harness | 1-2 agent-days | A fake agent the real supervisor launches, failing in every realistic way. A prerequisite for G3. |
| G5. A POSIX supervisor | design first | §8. |

**Related:** host-restart survival (#155), self-continuation of unfinished work (#139), and the supervisor correctness items #27, #33, #180 and #182.

### 6.2 Per-recipient delivery instead of the store-wide publication guard *(candidate)*

Every publication takes one store-wide guard with a 10 s deadline (#154). When the lead sent several messages back to back, waiting wrappers timed out on the guard and exited without a crash record. This happened seven times on 2026-09-12, at about 9k messages, and twice on 2026-09-27, at about 11.7k.

**Mechanism (code read, 2026-09-27).** Inside the lock, each send runs a full validated scan of the store: every file is parsed, the roster checked and the HMAC verified. It then rewrites the whole publication-order map and its hash chain. The final rename is O(1); the critical section is O(store). The wrapper does not treat a lock timeout on a core path as transient.

Staged plan:
1. **Wrappers survive lock contention** (2-4 agent-hours): bounded retry with backoff on the same message and cursor, a degraded health reason, and one durable diagnostic if contention persists. No format change. This is the first fix.
2. **Measure, then shrink the critical section** (6-10 h of measurement and design, then a bounded build):
   - measure lock wait against lock hold, scans and bytes at fleet size;
   - remove redundant scans and share the validated snapshot with the board;
   - only then decide on incremental order maintenance. An append-only order log with checkpointed chains is a format v2 plus migration. It keeps the fail-closed rule for reconstructed order, and it needs a challenge and a separate compatibility decision.
3. **Not planned:** replacing the canonical per-message files with SQLite. That would lose per-file signing, archives and backup semantics. A disposable, rebuildable index is acceptable.

- **Interim rule:** one publication per command.
- **Related:** #147 and #171 (the same 10 s guard flakes on slow CI).

### 6.3 Compaction blocked by stale unanswered openers

About 130 unanswered openers keep compaction from reclaiming the store, so the store keeps growing and feeds §6.2. The keep floor is one global watermark: one old obligation pins everything after it. Rescinding alone does not free space, because closed-superseded threads are protected too. Needed:
- **A read-only reconciliation report** (12-20 agent-hours including the cleanup flow). For each pin: the requester, the last evidence, the current authority and the reclaimable range. Then a previewed batch of individually authorized dispositions, distinguishing abandoned, completed and bookkeeping closures.
- **Later:** compaction that archives closed threads above the floor (6-12 h).
- **Never:** expiry by age. Age must never imply success, delivery, cancellation or approval.
- A visible pin count in `status` and the console.

### 6.4 The 2026-09-22 refocus items

1. **Context policy:** per turn, the wrapper chooses RESUME, SUMMARY (a fresh session seeded with a short handoff note and a context pack, #158) or FRESH.
2. **Per-project knowledge and per-seat memory,** curated at slice close. They make "fresh with knowledge" the default for dispatches.
3. **Retire the spec-kitty remnants,** and make the migration method first-class: the stage model, programme documents and per-stage evidence profiles (#153).

### 6.5 Store and host survival

- #156 slice 2: a durable mutation journal with the commit point before the local transition.
- #155: logon relaunch of registered seats through the existing planner and singleton lease, with one deduplicated boot note.

---

## 7. Candidate Features From the Agent-to-Agent Landscape

**Status: investigated on 2026-09-27 by two independent seats from different vendors.** They agree: no tool has a messaging feature that beats agenttalk's typed, correlated, cross-vendor, unattended model. The value is in a few borrowed **reliability ideas**, and each needs a challenge verdict before it becomes work.

| Rank | Candidate | Borrowed from | When |
|---|---|---|---|
| 1 | Wrappers survive store contention, and recovery is verified (§6.2 step 1, §6.1) | persistent-checkpoint recovery (LangGraph); bounded delivery | **Now** |
| 2 | O(1)-ish publication: no full scan under the lock; incremental order (§6.2 step 2) | the append-only WAL access pattern of a SQLite bridge, not SQLite itself | **Now**, measured first |
| 3 | Terminating obligations and stale-obligation reconciliation (§6.3) | A2A's task lifecycle: every task reaches a terminal state | **Now** (report); later (per-thread compaction) |
| 4 | Revive on need: mail owed to an exited seat triggers its relaunch | Agent Teams reviving a stopped teammate | Later, after §6.1 |
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
4. **Cross-vendor CLI bridges** (for example a WAL-mode SQLite bridge, JSONL mailboxes, MCP relays): the closest to agenttalk. They carry plain-text messages with no types, threads, reviews or gates.

agenttalk's differences:
- typed request/reply with threads;
- lead and liaison authority;
- durable wrappers that run seats unattended;
- cross-vendor cold reads, gates, closes and acceptance;
- spend ledgers;
- the console.

---

## 8. Later / On Hold

**On hold:**
- **Plan review write path (#206).** Annotate a plan and send it back. It is resumed only if the operator's trial of a markup tool shows that marking up beats chat. The read half is D1–D3 (§5).

**Supervisor and runtime:**
- the POSIX supervisor (P1) and a crash-simulation harness (P2), carried from the 2026-08 plan;
- the agent lifecycle RFC (#36);
- direct-wrap diagnostics (#181);
- #26 and #28;
- per-agent identity and authorization (RFC #19).

**Bus and protocol:**
- a completion receipt for tasks (#178);
- the task version gate refusing tasks to seats not yet relaunched after a release bump (#185), and the whole-roster sender-version check (#201);
- drain losing mail under truncated output (#37);
- not-found subclasses (#39);
- a CLI command-registration seam (#38).

**Governance and cost:**
- per-seat usage accounting (#157);
- context packs (#158, also §6.4);
- a risk assessment record in close policy (#159);
- reviewer calibration statistics (#160);
- composite lead-loop CLI verbs and a writable terminal console (#161).

**Comprehension:**
- the incremental extraction replacement (#141);
- scan-root selection (#144);
- a codebase visualizer on the comprehension plane (#183).

**CI, test and release:**
- the Windows serial suite near its ceiling (#151);
- guard-timeout flakes (#171);
- the acceptance suite's git spawns (#198; partly addressed in 0.93);
- release provenance for the tested wheel (#179).
- Also carried from 2026-08: property-based and mutation testing (P3), and an honest per-OS CI matrix (P4).

**Model gateway:** the field findings in #194.

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

The 2026-08 plan specified a native `agenttalk work` record store. The work board takes a smaller first step: **work is a projection over the bus.** Dispatches carry `work_item`/`stage` tags, replies carry typed verdicts, and a pure reducer derives the card. No second source of truth exists to drift from the bus, and "unknown" is a legal state. Native work records are added only where a projection cannot prove a fact, for example integration evidence (B5).

These invariants from the original spine still hold:
- **Evidence tiers are never collapsed into one green dot:** `referenced` < `local_agent` < `local_operator` < `automation_ci` < `external_attested`. Release-blocking gates require `automation_ci`, `external_attested`, or an explicit operator waiver.
- **Evidence is write-once** and bound to the exact head, base, policy hash and producer.
- **Policy boundary:** core validates schemas, ids, references, freshness, hashes and transitions. It hardcodes no language or scanner. Project policy owns required checks, lenses, accepted tiers, waivers, named tools, timeouts and network policy. Third-party tools are declared checks, pinned and checked offline (0.93).

Workflows on top (onboarding and comprehension, greenfield, existing-project change, legacy adoption) keep the 2026-08 shape. The modernization dogfood showed that the migration phases are a lead-run method; #153 turns them into a first-class workflow.

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
- **Store scale:** the publication guard's hold time grows with the store, and it has killed waiting wrappers (#154). **Mitigation:** one publication per command now; compaction unblocked (§6.3); per-recipient delivery (§6.2).
- **Seat availability:** a dead seat stalls its work until someone relaunches it. **Mitigation:** §6.1.
- **State-machine drift:** a work layer duplicates lane/gate/close truth. **Mitigation:** the board is a pure projection (§9).
- **Harness drift:** vendor CLIs change underneath the wrapper. Seen in the field:
  - an approval reviewer rejected a bus reply as egress;
  - task-kind reply drafts were dropped;
  - long foreground commands were auto-backgrounded and then died with the turn.
  **Mitigation:** field defects go in with evidence; briefs keep commands short and chunked; cross-vendor seats are exercised on every release.
- **Token cost:** multi-agent work costs a multiple of one session, and most of it is input tokens. **Mitigation:** model routing by task size, usage accounting (#157) and context policy (§6.4).
- **Command-runner risk, policy drift, evidence rot:** unchanged from 2026-08, with the same mitigations (structured argv, policy-hash binding, stale-at-head detection).
- **Platform portability:** the Windows-bound supervisor. **Mitigation:** the POSIX path and a per-OS CI matrix (§8).

---

## 12. Known Limitations (2026-09-27)

- **The supervisor** is PowerShell Core + `Win32_Process`-bound, and it is not run on the maintainers' fleet. Dead seats are relaunched by hand (§6.1).
- **Publications serialize on one store-wide guard.** Several sends back to back can make waiting wrappers exit without a crash record (#154).
- **Wrapped seat turns start only on a message.** Unfinished multi-turn work waits for the next message (#139).
- **After a release bump,** tasks to seats not yet relaunched on the new version are refused (#185).
- **`drain | head` can consume mail that was never displayed** (#37).
- **The work board is read-only by design.** A stage the evidence cannot prove shows as unknown, and the reducer carries a documented residual (see `docs/DESIGN-work-board.md`).
- **Windows CI** runs its serial suite near the one-hour ceiling (#151). Guard-timeout flakes recur on slow runners (#171).

---

## 13. Recommendation

1. **Ship v0.94.0**, the first board delivery plus the console v2 preview, and upgrade the fleet once.
2. **Make the team self-healing:**
   - wrappers survive store contention (§6.2 step 1);
   - close G2 and run the supervisor (§6.1 G1);
   - then the crash harness and guard-aware relaunch (G4, G3);
   - then host-restart survival (#155).
3. **Fix the store at the mechanism:** measure and shrink the publication critical section (§6.2 step 2), and reconcile stale obligations (§6.3).
4. **Then the refocus items:** context policy with knowledge and memory, spec-kitty removal, and a first-class migration method (§6.4).
5. **Later:** the remaining §7 candidates by rank, each with its own challenge verdict.

Bottom line: agenttalk can become the platform for teams of agents from different vendors to build software "by the book". But the product has to be honest about authority. It can enforce process, preserve evidence, challenge work before it starts, and fail closed when evidence is missing. It cannot remove the human oracle or prove correctness by itself.
