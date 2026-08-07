# agenttalk - Product Roadmap & Feasibility

**Status:** Official · **Owner:** lead (operator-facing) · **Last updated:** 2026-08-03
**Audience:** maintainers, operators, and agents deciding what to build next.
**Horizon:** pragmatic next 2-3 quarters + a labeled "later" tier.
**Current shipped baseline:** v0.81.0; runtime hardening has continued since v0.74.1 (supervisor crash/durability fixes, PowerShell baseline enforcement, publication-order guards). `CHANGELOG.md` remains the release-history source of truth.

**Platform requirement:** agenttalk must run on **Windows, macOS, and Linux**. The Python core (bus/store/CLI/wrapper) is already cross-platform and CI-tested on all three (Windows/macOS/Ubuntu × Python 3.10–3.13). The **supervisor is the open platform gap** — it currently requires PowerShell Core 7+ and Windows-only `Win32_Process`; a POSIX supervisor path is an unbuilt follow-up (see §6 cross-cutting and §8).

Companion docs: `docs/DESIGN.md` (why / architecture) · `docs/ASSURANCE.md` (per-release GOOD/ROBUST/SECURE attestation) · `docs/ISSUES.md` (living work tracker + known limitations) · `docs/TEST-COVERAGE-REPORT.md` (test inventory, coverage, and tooling recommendations) · `docs/DASHBOARD-CONTROL-PLANE-DESIGN-HISTORY.md` (dashboard control-plane design history) · `docs/ROADMAP-ARCHIVE-2026-06.md` (**archived** early roadmap/working notes through v0.24.0 — do not plan from it) · `CHANGELOG.md`.

**This file is the only current roadmap.** Two other files carry "roadmap" in their history: the archive above, and the dashboard design history. Both were renamed on 2026-07-30 so the current one is unambiguous.

---

## 1. Verdict

**Feasible if scoped as a local-first software delivery platform for trusted workspaces.**

The product should not promise that vague requirements become correct software without human judgment. The feasible claim is:

> agenttalk turns requirements into a disciplined, evidence-backed delivery workflow run by agent teams: plan, isolate, build, test, review, gate, and release with a human oracle for intent and waivers.

The human remains responsible for business intent, public contracts, migration risk, legal/security tolerance, and final release judgment. agenttalk is the workflow contract, evidence spine, local runtime, and operator control plane.

---

## 2. North Star

Build an **agentic software delivery platform** that a team can pick up for greenfield or existing projects, provide requirements, and have agents build the product by a professional software-development process.

The platform should make the correct path the normal path:

1. Requirements become explicit specs, acceptance criteria, risks, and non-goals.
2. Work is split into bounded items with owners, paths, domains, and isolated workspaces.
3. Agents produce code only inside recorded workspaces.
4. Quality checks and third-party tools produce write-once, content-addressed evidence artifacts.
5. Independent reviewers inspect the real diff at the exact revision.
6. Gates and closes compute HOLD/GO from typed evidence, not prose.
7. Operators can see what is blocked, why, and what evidence is missing.

**Positioning:** safer AI-assisted software delivery, not autonomous correctness.

---

## 3. Product Shape

The healthy shape is still a thin substrate plus pluggable products with hard boundaries. The new center of gravity is the **Native Work & Evidence Spine**.

| Product | What it is | Today |
|---|---|---|
| **A. Core Coordination Bus** | roster, messages, threads, requests, review handoff, broadcast, transcripts | Product-grade |
| **B. Native Work & Evidence Spine** | work items, bounded workspaces, evidence artifacts, review bindings, gate/check status, delivery lifecycle | Missing product layer; next major slice |
| **C. Method Engine Adapter** | spec-kitty and future planners can create/link work items; method owns decomposition, agenttalk owns execution evidence | Integration exists; source-of-truth boundary must shift |
| **D. Craft Skill Pack** | coding/review/test/QA/security/release skills that encode standards and evidence expectations | Useful; needs templates aligned to native work |
| **E. Assurance & Release Governance** | gates, closes, specialist sign-off, release ledger, scan evidence | Strong primitives; needs work-item consumption |
| **F. Knowledge & Codebase Memory** | domains, durable pointer-notes, lessons, onboarding digests, onboarding evidence ledgers, anchor-relative staleness | Primitives exist; codebase-comprehension product just started |
| **G. Operator Control Plane** | local console, read views, action-gated intent queue, lead-chat, attention/capacity/liveness, onboarding progress | Useful local console; needs Work view |
| **H. Execution Runtime** | supervisor, wrapper, session continuity, dead-letter, managed lead-loop, isolated lane worktrees | Powerful but high-maintenance; runtime ergonomics remain active. **Cross-platform gap:** wrapper/core run on all 3 OSes; the supervisor is PowerShell-Core + Windows-`Win32_Process`-bound — POSIX path pending. |
| **I. Delivery Workflows** | greenfield product build, existing-project change, legacy adoption, release preparation | Mostly dogfooded practice, not yet productized |

Key boundary:

**method engines plan; work items execute; evidence records facts; gates decide from evidence; humans own intent and waivers.**

---

## 4. Native Work & Evidence Spine

This is the missing product layer. It should be agenttalk-native, domain-neutral, and optional for non-development uses.

### Core contract

`agenttalk work` should bind existing primitives into one durable delivery record:

- **Domain:** who owns this area and which paths are in scope?
- **Lane/workspace:** where is the isolated local copy and what may change?
- **Work item:** what is being built, by whom, from which base, against which target?
- **Quality evidence:** which checks ran, at which revision, with what result?
- **Review evidence:** who reviewed the real diff and what did they find?
- **Gate state:** what is missing, stale, failed, waived, or satisfied?
- **Close state:** is this exact revision ready to merge/release under current policy?

### Minimal native lifecycle

Start narrow. Avoid a rich project-management state machine.

```text
draft -> open -> active -> review -> blocked -> ready -> delivered -> closed
                                  \                         \
                                   -> changes_requested      -> abandoned
```

Status should be a projection over typed records, not a place to hide contradictory truth. A work item may link to one lane/worktree, many evidence artifacts, review-request threads, gates, closes, and knowledge records.

### Storage direction

Use small per-item records, append-only events, and write-once artifacts:

```text
.agenttalk/work/items/<work_id>.json
.agenttalk/work/events/<work_id>.jsonl
.agenttalk/artifacts/<artifact_id>.json
.agenttalk/artifacts/<artifact_id>.log
```

The exact layout can change in the RFC, but these invariants should not:

- One corrupt item must not brick all work.
- Artifacts are write-once; corrections create new artifacts.
- Evidence binds to exact inputs: work id, base SHA, head SHA, diff hash, policy hash, command/tool, cwd, exit code, timestamps, producer, and trust tier.
- Gates consume only evidence that matches the current work revision and policy.

### Evidence tiers

Do not collapse all green checks into one green dot.

| Tier | Meaning | Default gate role |
|---|---|---|
| `referenced` | prose claim or linked output only | Never satisfies required gates |
| `local_agent` | local command run by an agent | Useful pre-review evidence |
| `local_operator` | local command run/confirmed by operator | Strong local evidence, still not CI |
| `automation_ci` | configured automation on exact commit/policy | Default release-authoritative tier |
| `external_attested` | signed/attested third-party evidence | Later, strongest tier |

Release-blocking gates should require `automation_ci`, `external_attested`, or an explicit operator waiver unless project policy intentionally allows otherwise.

### Policy boundary

Core validates schemas, IDs, references, freshness, hashes, transitions, and HOLD codes. It should not hardcode Python, Rust, Android, web, or any specific scanner.

Project policy owns:

- required checks by risk/domain/path
- required review lenses
- accepted evidence tiers
- waiver rules
- named tool commands/adapters
- timeouts, environment assumptions, and network policy

Third-party tools are declared checks. agenttalk's job is to run or ingest them reproducibly, capture output safely, and require evidence at the right boundary.

---

## 5. Workflows On Top

### Project onboarding and codebase comprehension

Before greenfield bootstrap or existing-project changes, the lead should open
an onboarding run. The run records which code/docs segments the team inspected,
what claims were proposed or confirmed, where docs/code/runtime drift exists,
and which unknowns still block safe work. This is evidence tracking, not a
claim that agenttalk understands the whole project.

For large existing projects, fan out by segment so each agent has enough
context, then require cross-check records before the lead turns the result into
domains, work items, knowledge notes, or characterization-test targets. When
code and documentation disagree, record drift rather than silently treating one
side as truth.

### Greenfield product build

1. Requirements intake: goals, users, non-goals, constraints, risk tolerance.
2. Spec: acceptance criteria, architecture, test strategy, delivery plan.
3. Bootstrap: repo, stack, CI, checks, docs, code policy, work lanes.
4. Slice delivery: each feature becomes native work with artifacts and review.
5. Release: close over exact revision and required evidence.

### Existing-project change

1. Open an onboarding run and map stack, tests, docs, CI, domains, risky seams.
2. Record segments, claims, drift, and blocking unknowns before editing.
3. Map ownership and path scope from the reviewed onboarding evidence.
4. Add characterization tests before risky behavior changes.
5. Deliver small bounded work items through review/gate/close.
6. Preserve durable discoveries in knowledge notes and lessons.

### Legacy adoption

Legacy adoption remains a flagship workflow, but it is no longer the whole roadmap. It becomes an opinionated composition of native work, knowledge, assurance, runtime, and dashboard:

1. Map: repo-map MVP, domain proposals, build/test inventory.
2. Preserve: curated knowledge notes, lessons, risky seams, characterization targets.
3. Safety net: assurance baselines, gates, CI evidence, required reviews.
4. Change: small gated work items with exact evidence and independent review.

---

## 6. Roadmap

### 6.0 Near-term release plan

The quarterly view below sets direction. This subsection is the *executable* near-term plan, and it follows
the release discipline the team adopted: **one theme per release, at most four items, and an umbrella ships
before its instances.** Anything not listed here is deliberately unscheduled — the backlog tail is large and
most of it should not be planned yet.

Sequencing note added 2026-07-30 after a production day in which a single wrapper defect consumed most of the
fleet's implementation throughput: **the runtime must survive a long turn before anything else gets cheaper to
build.** Every item below marked *(runtime)* exists because it was measured failing in the field, not
predicted.

#### v0.80.0 — "a watchdog kill must fail the turn" *(SHIPPED 2026-07-31)*

| Item | What | State |
|---|---|---|
| Watchdog kill must fail the turn *(runtime)* | When the per-turn watchdog kills a hung tool tree the wrapper freezes at `phase=active` with no terminal outcome, so the turn is never failed, never reported and never released. The wrapper's *shutdown* path already writes the correct terminal record — the watchdog path simply skips it. Small fix, highest value on the board. | **shipped** |

Scoped down from four items to one during the release round. The three supervisor items originally declared
here were moved to v0.81.0 because each produced a *growing* count of confirmed independent-review findings
rather than a shrinking one — the same signal that split the recovery-authority design below. Every finding
the implementers checked was real, so the code was improving; it was simply not converging on a release
clock. Shipping the one measured-highest-value item beat shipping four half-converged ones.

#### v0.81.0 — "the supervisor knows what it owns, and says so when it cannot" *(SHIPPED 2026-08-02)*

The theme changed during the round. Two of the three items declared here converged and shipped; the other
two — kill-switch startup persistence and the supervisor-state lock — were deferred *again* rather than
shipped half-converged, for the same reason they were deferred out of v0.80.0. Their place is taken by two
items pulled forward off the recovery board, one of them found in production during the release round.

| Item | What | State |
|---|---|---|
| Wrapper stdout/stderr capture *(runtime)* | Wrappers launch with a hidden window and no output redirection, so a dying wrapper's traceback is destroyed by construction. Adds bounded, rotated per-agent capture plus factual lifecycle-event lines (never self-assessed health). | **shipped** (#117) |
| Owned process tree per agent, with exact process identity | Ownership traversal stopped at shell hosts, so the tool descendant that actually wedges a wrapper appeared in no durable record. Kill targets now carry the exact creation FILETIME rather than a rounded timestamp, so a recycled PID cannot be terminated in place of the original. Pulled forward from v0.83.0. | **shipped** (#120) |
| A confirmed-absent wrapper is launchable under a sticky invalid owned tree *(runtime)* | Found in production on the maintainers' own fleet: one transient malformed row in a single whole-host process snapshot invalidated the owned-tree check for every agent in that poll, and since an invalid record was never re-derived, a ~1-second glitch disabled automatic recovery fleet-wide indefinitely. The HOLD pre-empted the LAUNCH path, so the state was self-sealing. Unplanned; found and fixed during the round. | **shipped** (#150) |
| Kill-switch startup persistence | A kill switch present at *startup* currently produces no state, no projection and no operator-visible record: the generated supervisor exits before the instance claim. Adds a switch-present observational mode with a durable record projected through `status`/`status --json` independent of `event_limit`. | **deferred again** — PR 107, implementation in review |
| Supervisor-state lock + single checked owner | `load_supervisor_state`/`save_supervisor_state` hold no lock across the read-modify-write, so two writers lose an update. Introduces one data-only checked owner, an enforced lock order with state as the terminal leaf, and removes the second (PowerShell) writer entirely rather than trying to synchronise two implementations. | **deferred again** — complete, never gated; mechanically blocked on the item above |

**Known limitation shipped with this release, not fixed by it:** an owned process tree that goes `invalid`
stays invalid for as long as its wrapper keeps running, because a healthy wrapper never triggers a fresh
walk. The rule predates the release; upgrading makes it *visible*, since an invalid record already on disk
survives every restart and code upgrade. Clearing it is per-agent and fully attended. See `CHANGELOG.md`
§0.81.0 "Known limitations" and the self-healing follow-up on the recovery board.

#### v0.82.0 — "the supervisor can see and start the fleet, or says plainly that it cannot" *(runtime)*

**Re-scoped twice. Read this before planning anything in it.**

*2026-08-03:* declared theme was "a wrapped agent never dies silently". Replaced, because after a host
reboot the supervisor could neither start the fleet nor *see* the fleet an operator starts by hand — nine
agents on the maintainers' host, twelve on a second independent host (different reason code, all entry
points confirmed on 0.81.0, so not version skew), both recovered only by bypassing the supervisor. An
agent that never launches cannot die silently.

*2026-08-04:* the cold-start keystone was **removed from this release** after one attempt, one revert and
two independent refutations. It is not a small fix, and the reason is structural rather than an
estimating error:

- The attempt proved absence by ENUMERATING the identities an agent's own records name, then checking
  each against the live process list. `7de07e4` was merged, turned master red on every platform (one
  deterministic failure in the ephemeral-reviewer path — a caller nobody had mapped), and was reverted
  in `627d5ee`.
- An independent cold review then **reproduced a false absence**: a live same-agent process that the
  predicate cannot see, because a `TERMINAL`-phase record drops the CLI-launcher identity, and because
  only rows whose *current direct* parent is a named root are excluded — so a process reparented after
  its parent exited, or a grandchild, is invisible. The code raises the correct HOLD first and the new
  predicate then suppresses it.
- **The method is the defect.** You cannot prove nothing is running by ticking off what you know about;
  the failure mode is what is missing from the list. No length of list fixes it. A complete ownership
  proof is required, which is the recovery-authority work — see v0.83.0.
- Separately: **the actual spawn is not testable today.** It happens entirely inside the generated
  PowerShell executor; the stub-agent canary drives the *wrapper's* CLI-spawn loop and never the
  supervisor's launch step. So a fix to this path currently has no way to demonstrate that an agent
  actually starts. Extracting that spawn step to a testable seam is a prerequisite, not a nicety.

What ships here instead is the honest subset: three items that are converging, plus a replacement for the
keystone that improves availability **without** authorising any launch.

| Item | What |
|---|---|
| **Refuse loudly, with an honestly bounded remedy** *(keystone replacement)* | The worst property of the cold-start defect is not that automation declines to act — it is that the refusal is silent and the printed remedy does not run. Measured: 20+ polls producing `warn_only` and nothing else; an explicit `request-restart` accepted with "the supervisor will relaunch it" that relaunched nothing; a remedy naming `--reset-process-tree-ownership`, which for one of the nine self-reported that it would refuse, and which requires attesting there is no live supervisor while the supervisor is running. Fix the *communication*, not the authority: when the supervisor will not start an agent, say so once per agent as an operator-visible attention item and state which specific evidence is missing. Handler-backed reset/archive commands are printed only when their real admission predicate currently holds. Re-prepare after editing the configured profile. Recovery emits a named refusal instead of argv when the configured launch mapping or reconstructed launch-effective projection no longer matches; equivalent-looking edits are not guaranteed to remain valid. The child's environment is explicitly unverified and requires operator review. Where neither applies, say plainly that no scripted remedy applies. Turns an indefinite silent wait into one actionable step without authorising anything new. |
| Strict health reaches operator surfaces | The strict child verdict never reached `status`, `doctor` or the console, so a wrapper with a dead child reads green. A verdict must not be presented where an observation belongs. **Implemented in PR #100** with a shared two-poll freshness allowance across status/doctor/web; ships with this theme rather than anchoring a different one. |
| Wrapper capture on every launch path | The #117 bounded stdout/stderr capture is wired into the *generated PowerShell launcher*, not the wrapper, so a wrapper started any other way — including the only working recovery for the keystone above — produces no log at all. Confirmed on both hosts, for healthy and failing agents alike. Its log root is also a hashed path (`%LOCALAPPDATA%\agenttalk\wrapper-logs\<project-hash>\agent-<hash>\`) that no document names and no command prints. Move the capture into the wrapper's own startup and expose the resolved root. |
| Instance-marker honesty | Stale singleton markers are chronic, not incidental: five quarantines on one host since 2026-07-16. Ctrl-C on `agenttalk start` is not a stop and nothing says so; the orphan keeps the lock, the next `start` prints a success line for a pid that never existed, and the resulting process stays alive writing no state, snapshot or events because it never won the claim. Detect a marker whose holder is dead or parentless, name `--repair-instance-marker`, and never print a pid that was not verified to exist. |

#### v0.83.0 — "recovery actually recovers"

Umbrella first, per the release discipline. Carries the three items deferred from v0.82.0's original scope.

**OVER-SUBSCRIBED — needs a split before it is built.** Six live items against the four-item cap in the
release discipline. Recorded here rather than silently absorbed, because a release that quietly grows past
its cap is how v0.80.0 shrank from four items to one mid-round. The recommended split when this comes up:
keep the umbrella with "terminate an orphaned wrapper" and "absence is not staleness" (all three are the
same recovery-authority question), and move "gate execution outside the turn envelope", "same-message
livelock visibility" and the `.agenttalk/` ignore rule into a separate turn-envelope/hygiene release. That
call belongs to whoever scopes v0.83.0, not to this note.

| Item | What |
|---|---|
| Define + gate the recovery-actually-recovers invariant | The umbrella. Recovery currently *attempts* and reports success while nothing recovers. Make the invariant explicit and gated. Its first falsifiable entry is now measured rather than hypothetical: a supervisor that polls twenty times, reports `warn_only`, accepts an explicit restart request, and launches nothing, while its own record says the wrapper is absent. |
| **Complete ownership proof** *(moved here from v0.82.0 on 2026-08-04)* | The cold-start fix belongs to this release, not an earlier one, because it needs what this release is for. Enumerating known identities cannot establish absence — reproduced: a reparented live process, and a grandchild whose intermediate parent exited, are both invisible to an identity list, and a `TERMINAL`-phase record silently drops the launcher identity. Recovery may only launch when absence rests on a complete ownership account of the current process graph, not on a list of names the records happen to carry. Until then the supervisor must keep refusing — see v0.82.0's loud-refusal item for the interim operator experience. |
| **A testable spawn seam** *(prerequisite, found 2026-08-04)* | The supervisor's actual process launch lives entirely inside the generated PowerShell executor, and no existing harness reaches it: the stub-agent canary drives the *wrapper's* CLI-spawn loop instead. So no fix to the launch path can currently demonstrate that an agent starts. Extract the spawn step behind a seam that a test can drive and observe (exactly one replacement process, reaching readiness, exact identity, no duplicate, no kill, exact cleanup). Build this BEFORE the ownership proof, because otherwise its acceptance test cannot exist. Two changes in two days were shipped or nearly shipped on tests that could not reach the behaviour they claimed to cover; this closes that channel. |
| Gate execution outside the turn envelope | Running the project's own gate inside a wrapped turn *guarantees* a watchdog kill: the source pytest leg caps at 2700s while the watchdog tolerates a 600s tool descendant inside a 1800s turn. The practice half (targeted tests in-turn, CI as the gate) is already in force; this ships the durable half — an owned, bounded, start-guarded detached runner that outlives the turn and writes SHA-bound evidence. |
| Same-message livelock visibility | A message whose processing wedges the wrapper is retried forever, and every later instruction — including the one that would fix it — is starved behind it. The runtime record already carries `message_id`; nothing compares it across turns. Surface consecutive same-message turn starts, and park a repeatedly-wedging message rather than starving the queue. Deferred from v0.82.0: it cannot bite an agent that never launches. |
| Provision the `.agenttalk/` ignore rule | `ASSURANCE.md` asserts the state directory is gitignored as a *structural* property, but nothing provisions the rule on `init`. Deferred from v0.82.0. |
| ~~Owned process tree per agent~~ | **Shipped early in v0.81.0** (#120) — pulled forward because the wedge frequency made it the blocker. Left here for the trail; do not re-plan it. |
| Terminate an orphaned wrapper; bound the retry | Recovery is defined as "launch a replacement", which the launch barrier correctly refuses while the incumbent survives — so the cycle backs off exponentially and fades into silence. Something must own terminating a *provably childless* wrapper, and the retry cycle needs a hard cap that escalates instead of going quiet. |
| Absence is not staleness | A twice-confirmed-absent wrapper waits out the per-CLI heartbeat threshold (up to 2400s for Codex) before relaunch. The heartbeat answers "is this agent still working?"; a complete process snapshot answers "does this process exist?" — conflating them is the defect. Independently stageable, and must not wait for the rest of the recovery-authority design. |

#### v0.84.0 and beyond — recovery-authority design, implemented in slices

The supervisor recovery-authority design was split after two review rounds produced a *growing* finding count:
a single document was specifying a state machine, a persistence/delivery promise and a migration plan at once.
The slices ship separately — classifier/authority core first, then the incident/delivery contract (which
depends on the state-lock work in v0.81.0), then migration/rollback. Nothing merges without a fresh
independent panel.

#### Deliberately unscheduled

CI determinism and the security-stack pin; the wall-clock flake family; the earned-green invariant; the
assurance/DoD authentication and waiver-expiry items; bus/knowledge resolution and vocabulary items; and the
alternative-worker evaluations. These are tracked, not planned.

### Quarter 1 - prove the delivery spine

1. **Wrapped-agent runtime ergonomics.** First-class per-agent model / reasoning-effort config, restart-safe session fingerprinting when runtime config changes, and the planned no-visible-CLI/headless supervised mode with full dashboard status.
2. **Project onboarding MVP.** ✅ Shipped in v0.73.0. Native onboarding runs for segments, claims, drift, and blocking unknowns, plus a read-only dashboard projection. This is the first step for existing-codebase adoption and the input to later work routing.
3. **Native Work RFC.** Specify work item schema, event model, artifact schema, lifecycle, source-of-truth boundaries with lanes/gates/close, evidence tiers, reset semantics, and safety invariants.
4. **Work item MVP.** `work create|list|show|status|assign|start|deliver|abandon`, with one record per item and links to existing lanes, domains, request threads, gates, closes, onboarding runs, and artifacts.
5. **Evidence registry MVP.** Write-once artifacts, hash validation, bounded logs, redaction status, exact input binding, trust tiers, and stale-at-head detection.
6. **Pure work check.** `work check` computes `GO`, `HOLD`, or `UNKNOWN` with stable HOLD codes over work state, lane/worktree state, artifact freshness, review state, and gate/close inputs.
7. **Assurance Slice B integration.** Make `assurance.py` artifacts consumable by work checks, gates, and closes without turning assurance into authority.

### Quarter 2 - make it useful for real projects

8. **Project code-policy v1.** Optional `.agenttalk/code-policy.json` declaring required evidence by risk/domain/path, accepted tiers, review lenses, and named checks. No arbitrary shell runner by default.
9. **Generic quality runner / ingestion.** Start with structured argv, no shell by default, explicit cwd/env/network policy, timeouts, output caps, redaction, and fake-adapter failure tests. Add convenience adapters only after the generic contract is proven.
10. **Native review binding.** `review request --work` and review-result consumption tied to reviewed ref, artifact ids, tests executed, findings, residual risk, and release-blocker flags.
11. **Dashboard Work view.** Show work item, owner, worktree, head SHA, dirty/stale state, evidence tiers, review obligations, HOLD reasons, and next owner/action. Keep `/api/state` body-free and log-free.
12. **First-run delivery setup.** One command/dashboard path to check install, skills, roster, operator-facing lead, supervisor, dashboard, code policy, CI visibility, and tool availability.

### Quarter 3 - productize greenfield and existing-project workflows

13. **Greenfield workflow alpha.** Requirements intake -> spec -> onboarding/bootstrap -> work slices -> evidence/review/gate -> release close.
14. **Existing-project workflow alpha.** Onboarding run, repo detection, domain proposal, test/CI inventory, characterization targets, safe first change.
15. **Legacy adoption alpha.** Repo-map MVP, fan-out comprehension artifacts, knowledge curation, dashboard legacy-map view, unmapped/stale/risky coverage.
16. **CI adapters.** GitHub Actions first: ingest run ids, workflow refs, commit SHA, conclusion, logs/artifacts as evidence. Other providers later.
17. **Repeatable beta packaging.** Golden-path walkthroughs for one greenfield app and one existing-project change, with sample policies and artifact schemas.

### Cross-cutting - runtime, platform & test hardening

These are foundational, not tied to one quarter — the delivery spine above rides on a runtime that must be portable and provably robust. Prioritize the cross-platform supervisor and the crash-test harness near Q1 (they gate the "runs anywhere, self-heals" promise).

- **P0 (umbrella). Agent lifecycle model** — `docs/RFC-agent-lifecycle-ephemeral-dispatch.md` (RFC #36; Tier-3, in docs-test → design panel). The structural fix for the recurring stall classes (context-bloat crash, idle-wedge, dispatch outage): a hybrid tiered lifecycle — **Tier E** ephemeral on-demand (generalizes the review-specific `ephemeral_reviewers`/`request-launch` path), **Tier W** bounded-warm (context-bounded checkpoint→relaunch), **Tier D** single always-on supervisor dispatcher ("invert the listener"). **Absorbs #16** (fresh-session/rollout bound) and **reframes #20** (warm-session) as one tier. Its genuinely-new primitives (work-reservation/dedup, supervisor↔warm-worker channel, per-role checkpoint artifact + `--resume-brief`, atomic fenced teardown gated by a fail-closed union of the owed-action ledger + thread-replay, orphan-reap + POSIX parity) are the build scope. **Prerequisites: P1 + P2 below** (the dispatcher tier cannot ship until the supervisor is cross-platform and crash-proven). Design-first: the Tier-3 panel passes before any build.
- **P1. Cross-platform supervisor.** Close the platform gap so the supervisor runs on Windows, macOS, and Linux — either a POSIX supervisor with parity to `supervisor.ps1` (liveness/heartbeat-staleness, claim/marker, restart/backoff, reserve-before-spawn, config last-good, fail-closed), or make the generated script pwsh-Core-portable by abstracting the Windows-only `Win32_Process` snapshot (`ps`/`Get-Process` on POSIX). Design-first (choose the approach). This is what makes "runs on all three" true for the whole stack, not just the core.
- **P2. Supervisor crash-simulation test harness.** A configurable fake agent the real supervisor launches, failing every realistic way (crash-on-start, crash-after-N, hang, wedge = alive-heartbeat-but-no-work, crash-loop, bad/stale/future heartbeat, pid-reuse, record-launch-fail), driven through poll cycles asserting the invariants we have been fixing by hand (daemon never crashes, correct restart/backoff/reset, liveness classification, no double-launch, hot-add survival, config-corruption tolerance, fail-closed on permanent I/O). This session's three supervisor bugs were caught by review/production, not tests — this closes that class. Must run on each target OS (a Linux-only matrix false-greens the Windows/macOS supervisor).
- **P3. Test-quality tooling.** Adopt property-based testing (Hypothesis) for the bus protocol / resolver state machine / enforcement invariants, and mutation testing (`mutmut`/`cosmic-ray`) to measure whether tests actually catch bugs. Add `pytest-xdist` (parallel) and `pytest-randomly` (order-independence); pin the multi-version matrix via `tox`/`nox`. See `docs/TEST-COVERAGE-REPORT.md`.
- **P4. CI honesty for platform coverage.** Ensure the matrix runs the platform-specific supervisor/pwsh path on **each** OS (real pwsh tranche is skipped off-Windows today → false-green risk). Consider a fake-model-gateway container to exercise backend-failure modes (429/timeout/malformed) without paid model calls, ahead of the Qwen enforcement canary.

### Later - explicitly not scheduled

Hosted multi-tenant SaaS · enterprise auth / cryptographic human identity · remote cloud runners · semantic/vector index over large codebases · automatic architecture inference claiming completeness · automatic large refactors without human-curated characterization · multi-repo program management · skill/method marketplace · broad merge/release automation before gate semantics are proven.

---

## 7. Hard HOLDs

Do not ship broad workflow claims if any of these are true:

- Work items, lanes, gates, closes, and review threads can disagree with no single projection explaining the conflict.
- Artifact records are mutable in place.
- A gate can pass from chat prose or referenced-but-unexecuted evidence.
- Evidence is not bound to exact head SHA, base SHA, diff/policy hash, and producer.
- Local agent evidence satisfies release-blocking gates by default.
- Project policy changed inside the same worktree silently changes the gate.
- A runner accepts shell strings by default or inherits uncontrolled environment/secrets.
- Timeout, malformed output, parser failure, or adapter failure can normalize to pass.
- Worktree cleanup can delete dirty, unmerged, unmanaged, or user-created files.
- Dashboard collapses evidence tiers into one misleading green status.
- Cross-platform support is claimed while the supervisor runs (or is tested) on only one OS, or while the real-pwsh/supervisor test tranche is silently skipped on a target platform's CI.

---

## 8. Top Risks

- **False trust:** operators read green as correctness. Mitigation: language and UI say "evidence current and policy satisfied," not "code correct."
- **State-machine drift:** native work duplicates lane/gate/close truth. Mitigation: work links and projects existing primitives; pure checks derive state.
- **Command-runner risk:** project policy becomes arbitrary code execution. Mitigation: structured argv, opt-in execution, explicit env/network, timeouts, caps, redaction, and failure-injection tests.
- **Policy drift:** a branch changes the rules that judge itself. Mitigation: policy hash binding, base-policy evaluation, explicit waiver for policy changes.
- **Evidence rot:** artifacts point at old SHAs or missing logs. Mitigation: stale-at-head detection and content-addressed artifact references.
- **Maintenance overload:** provider CLI, CI, and scanner formats change. Mitigation: adapter boundaries and generic artifact schema first.
- **Platform portability:** the supervisor is Windows/PowerShell/`Win32_Process`-bound while the product must run on Windows, macOS, and Linux. Mitigation: a POSIX (or pwsh-Core-portable) supervisor path, and a CI matrix that exercises the supervisor on every target OS.
- **Runtime test coverage is behavioral, not line-measurable, and thin on failure paths:** the supervisor's logic is a PowerShell template `coverage.py` can't see, so line-% understates it; this session's supervisor bugs reached production because the crash/liveness matrix was under-tested. Mitigation: the crash-simulation harness (P2) + property/mutation testing (P3), measuring behavioral coverage rather than lines.

---

## 9. Recommendation

The next major roadmap change is to build **Native Work & Evidence Spine** before expanding legacy-adoption features. That spine is what makes both greenfield and existing-project delivery credible.

Immediate sequence:

1. Close the small wrapped-agent runtime ergonomics slice.
2. Write the Native Work RFC.
3. Ship work item records + lane/worktree binding + write-once evidence registry.
4. Add pure `work check` with stable HOLD codes.
5. Integrate assurance artifacts and review-result evidence.
6. Only then add runners, CI adapters, dashboard Work view, and greenfield/legacy workflow productization.

Bottom line: agenttalk can become the platform for teams of agents to build software "by the book," but the product has to be honest about authority. It can enforce process, preserve evidence, and fail closed when evidence is missing. It cannot remove the human oracle or prove correctness by itself.
