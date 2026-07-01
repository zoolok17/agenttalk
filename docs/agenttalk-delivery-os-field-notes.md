# agenttalk: from a message bus to a delivery OS
### Field notes + prioritized design recommendations from heavy real-world use

**Source:** an agenttalk-coordinated team of Claude + Codex agents running a real production Android app (Orbit Launcher) — multi-agent implementation, cross-vendor review, on-device testing, and live releases. This is a trench report from an intense multi-day stretch: the failure modes we actually hit, and what they imply for where agenttalk should go.

**Discipline note (please hold us to it):** every *operational* claim below is independently verifiable — git history, `health.json`, session transcripts. Treat it as evidence, not opinion. Where something is a judgment call, it's marked as one.

---

## Executive summary (for a product lead)

- **agenttalk's real product is a delivery OS, not a chat channel** — it turns many non-deterministic workers into one controlled delivery system while conserving human attention.
- **The scarcest resource is human attention, and it doesn't scale.** Every process routes decisions to one human; double the agents and you double the demands on that person. Architect to *conserve* it: one ranked "here's what needs you" queue with recommendations, not N agents pinging. **Optimize for features shipped per unit of human attention spent** — that's the North-Star metric, and the honest benchmark is "vs. one strong dev driving AI by hand."
- **Build order matters and is not flat.** Phase 1: **liveness** (pull-board + never-idle + typed blockers) and the **human-attention queue**. Phase 2: quality machinery (DoD-verifies-behavior, reproduction-based review, assumption log). The quality gates are worthless if the agents silently stop.
- **The #1 concrete bug:** the supervisor — the auto-restart that cures agent "parking," the biggest failure mode we hit — is effectively unusable on 0.54.0 (it launches agents with `-s workspace-write`, stripping the env so they can't reach the bus). Fix that and the largest source of operator babysitting disappears.
- **One discipline throughout:** *verify, don't trust the narrator* — including an agent's reports about itself.

---

## 0. The thesis: what agenttalk actually is

agenttalk began as "let Claude and Codex talk to each other." The real object is much bigger:

> **A coordination layer that turns many non-deterministic workers into one controlled delivery system — while conserving human attention.**

The reasoning: software delivery has *always* meant managing non-deterministic contributors (people) through process — requirements, communication, review, testing, escalation, accountability. AI agents fit that exact pattern. But they arrive **without** the implicit judgment, memory, culture, drive, and responsibility humans carry. **agenttalk's job is to externalize the missing pieces** — to make the implicit operating system of a healthy software team *explicit* enough that agents can operate inside it.

The reframe that matters: we are not building "AI pair programming." We are building **a managed delivery environment where AI agents behave like a software team under process discipline.** Agents don't need to become developers; they need to become *reliable participants in a delivery process.*

---

## 1. The single most important principle: **conserve human attention**

Every process below — readiness gates, blockers, reviews, verification, escalation — generates decisions and checks that route to **one human**. Double the agents, and you double the demands on that human. So:

> **The scarcest resource in an agent org is not compute, and not the agents. It is human attention. The system must be architected to spend it frugally.**

Concretely, this means agents must **not** ping the human directly and individually. Everything that needs a person must be **batched, ranked, and deduped into one prioritized "here's what actually needs you" queue**, each item carrying a recommendation. In practice we ran this as a human-facing "Number 1" agent that sat between the operator and the swarm and compressed it. **It should be a first-class primitive, not a role someone has to play.**

**Attention-queue item format** (this is the shape that worked, in daily use):
```
DECISION NEEDED: <one line>
WHY IT MATTERS:  <business/technical stakes>
OPTIONS:         A / B / C
RECOMMENDATION:  <agent's pick + one-line reason>
RISK IF IGNORED: <low/med/high + what happens>
CONFIDENCE:      <how sure the agent is>
PRIORITY / BY:   <when it's needed>
AFFECTED TASK:   <link>
```

**North-Star metric:** the benchmark for the whole product is not "is agentic development possible" (it clearly is). It is **"does the coordination layer pay for itself vs. one strong dev driving AI by hand — counting the human's own attention as a cost?"** If agenttalk ships 2× the features at 3× the operator's decisions, it lost. So the KPI to optimize is:

> **Features shipped per unit of human attention spent.**

Every other metric (agent idle time, human interruptions per feature, unresolved blockers, reopened tasks, bugs-after-"done", cycle time, diff size) is a leading indicator of that one.

---

## 2. Field report: the failure modes we actually hit

**A. Parking / liveness collapse (highest-impact bug).** Repeatedly, agents finished their assigned work and simply **stopped** — turn ended, agent idle, nothing pulled the next item. The whole team went `idle_waiting` with real work outstanding, and it was **invisible** until the operator manually swept git/health. Root cause: an agent's turn concludes and `wrap --loop` does not reliably re-trigger a *finished* agent; there is no "when you're done, pull the next thing or declare idle" protocol. Humans do this from ambient drive; agents have no ambient. *(Checkable: multiple stretches with zero new commits while all agents showed fresh heartbeats + `idle_waiting`.)*

**B. Verifying the wrong thing.** An agent verified that a build *launched* and shipped it — but the change was to a live wallpaper's behavior during a gesture, which it never exercised. Result: a shipped regression (frozen animation). "It builds / it launches" is not "it works." *(Checkable: transcript + the follow-up revert.)*

**C. More agents made it worse, not better.** Adding agents increased coordination cost, waiting, parking, and message noise. The useful unit was **a few agents wearing role-hats**, not a large per-role org chart. An 8-agent "team" per feature is enterprise cosplay for the org itself.

**D. Confirmation/ack spam drowned signal.** Agents acknowledged every dispatch/routing ("routed X to Y", "confirmed"), which flooded the operator and buried the milestones that actually mattered. We had to manually instruct "signal-only: ping me on milestones/decisions, not acks." This should be a built-in discipline.

**E. Review theatre risk.** The reviews that caught real defects were the ones with **independent reproduction** ("I ran the concurrency tests", "I pixel-diffed the frames to prove motion"). Reviews that were only *dialogue* ("looks good", "I see a risk") added little. Agents can object *performatively* and then approve, or hallucinate objections.

**F. Silent assumptions.** The bugs that slipped were rarely coding errors — they were invisible assumptions nobody surfaced or challenged.

---

## 3. Prioritized recommendations (sequencing matters — the list is not flat)

### Phase 1 — Liveness + attention (build these first; nothing else pays off until agents reliably *move* and the operator isn't the bottleneck)

**1. A pull-based work board with liveness rules.** States:
`Backlog → Ready → In Progress → Review → Human-Needed → Blocked → Done`
Protocol, enforced by the loop (not the operator):
- No agent may go **silently** idle.
- Done? **Pull the next eligible task.**
- Blocked? **Declare a typed blocker** (below).
- Waiting on a human? **Create a human-attention item** (§1).
- Task too large? **Split it before starting** (below).
This single change fixes the parking. The board *is* the "headset" made durable.

**2. The human-attention queue** (§1). The biggest product feature. One ranked surface, recommendation attached, deduped — not N agents pinging.

**3. Typed blockers with routing.** `BLOCKED` as a first-class state, not a message someone must notice. Types + routing logic (not all go to the human): requirement-unclear, ambiguous-acceptance, missing-env/secret, insufficient-context, conflicting-patterns, failing-unrelated-tests, unclear-domain-rule, security-sensitive, too-large-diff, architectural-uncertainty, external-API-uncertainty, migration-risk, **visual/behavioral-verification-needed** (routes to the human oracle).

### Phase 2 — Quality machinery (only valuable once Phase 1 holds)

**4. Definition of Done that verifies the *change*, not the compile.** Strict: build-passing and tests-passing are **not** done. *The changed behavior must be verified against its acceptance criterion.* Backend → automated test; UI/visual/mobile → screenshot diff or **human-oracle** confirmation. (This is directly our frozen-animation lesson.)

**5. Reproduction-based review (give reviewers teeth).** A review must be an *independent reproduction*, not an argument. Required review output is evidence: "I ran command X" / "I reproduced Y" / "I verified criterion Z" / "I could not verify B because <reason> → route to human." Verdict: merge / reject / human-review.

**6. Assumption log per task** (cheap, high-value). Format:
`Assumption · why I believe it · risk if wrong · how to verify · needs-human-confirmation? y/n`
Another agent or the human can then *challenge* it before it becomes a bug. Generalize the rule: **no silent reasoning** — agents externalize assumptions and uncertainty *as they go*, so they're challengeable.

**7. Definition of Ready gate.** No agent writes code against a task missing: goal, business value, acceptance criteria, non-goals, affected areas, constraints, known risks, test expectations, open questions. Missing → raise a blocker, don't improvise. Bad tickets create bad code — for agents *and* people.

**8. Small-task / small-diff bias.** Resist giant diffs; decompose (discovery → acceptance criteria → design note → test scaffold → impl slice 1 → impl slice 2 → cleanup → review). Keeps output reviewable and prevents context-window collapse.

**9. Decision records (lightweight ADRs) + requirement→test traceability.** Per meaningful decision: problem / options / choice / why / rejected alternatives / risks / follow-up. And maintain the chain `requirement → acceptance-criterion → code → test → review-note`. That chain is the literal, auditable **proof of "software, not code"** — the artifact you hand a skeptic.

**10. Onboarding / context-load step + quality memory.** Before starting, an agent confirms it has: project structure, coding conventions, domain terms, test command, recent decisions, what-not-to-touch, enough in-repo examples. Plus a project-specific memory of rules and past mistakes ("dates are UTC", "money uses BigDecimal", "module Z tests are flaky — rerun once", "don't touch generated files"). This is the agent equivalent of institutional knowledge, which agents otherwise reset to zero on every session.

**11. Handover notes at session end.** what was done / changed / remains / known risks / commands run / tests passing / open questions. Prevents "context death" between sessions.

### Cross-cutting principles (apply to all of the above)

- **Roles are hats, not headcount.** A small number of agents that switch mode (implementer / reviewer / tester / analyst / red-team), with structural adversariality *only where it pays* (impl vs. reviewer, always; a standalone "maintainer agent", almost never).
- **Keep the artifact, drop the meeting.** Agents don't need calendar rituals; they need the *outputs* of rituals (decisions, blockers, assumptions, handovers, test evidence). Don't simulate planning/design-review/retro as ceremonies — that re-imports the waste you were escaping. The artifact *is* the alignment, collapsed.
- **Red-team as a feature.** Turn skepticism into a mode: "assume this is wrong — find hallucinated APIs, hidden assumptions, missing tests, security holes, over-engineering, business-rule misreads." The correct response to any AI-failure example is *"add it to the red-team checklist,"* not denial.
- **Anti-over-engineering guard.** Agents generate architecture eagerly. Require a check: "is this proportional to the problem?" — flag unnecessary abstraction, invented frameworks, premature extensibility, cleverness, style deviation. (Note: this applies to the *org design* too — see "roles are hats.")

---

## 4. Concrete engineering gaps in the current release (0.54.0) — actionable for maintainers

1. **The supervisor — the exact cure for parking (§2A) — is currently unusable.** `agenttalk supervise --init` scaffolds a watchdog, but the launch path hardcodes `-s workspace-write`, which on Windows strips the environment/PATH so the spawned agent cannot reach the bus (`python -m agenttalk` not found), plus a preflight that reports green when it isn't. **This is the single highest-value fix**: a working health-driven auto-restart/re-prompt watchdog would eliminate the #1 failure mode without operator babysitting.
2. **Windows spawn is a landmine.** Launching a wrapped agent with bare `codex` fails — it's an npm `.cmd` shim, unspawnable by a Python subprocess (WinError 2), and that spawn failure is **misclassified as a retryable outage**. You must use the full `codex.exe` path, and `-s danger-full-access` (workspace-write strips env → the agent can't reach the bus). Asks: resolve the real executable; classify spawn-failure distinctly from outage; document/handle the sandbox env-strip.
3. **`wrap --loop` doesn't re-trigger a *finished* agent** (root of parking). The "done → pull next / declare blocked / declare idle-with-reason" protocol should live in the loop itself.
4. **No first-class BLOCKED / idle-with-reason surfacing.** `health.json` has `idle_waiting`, but nothing routes or surfaces it — parking is invisible without operator polling. Add a "who's idle/blocked and why" view.
5. **Dead-letter re-notification with no dispose.** A dead-lettered message keeps re-pinging; the subcommands are only `list/show/requeue`. Add `dispose`/`resolve`.
6. **Ack/confirmation spam has no built-in throttle.** The bus should distinguish milestone/decision traffic from routine acks (supports §1: conserve attention).
7. **`wait` cursor semantics are easy to misuse.** Bare `wait` advances a global cursor and can chew backlog; concurrent waiters split the stream; background waits can be culled. Liveness detection shouldn't depend on operator-run `wait` loops — see (4).
8. **Minor:** `broadcast --kind` rejects `end` (must use `agenttalk end`); kinds are inconsistent across commands.

---

## 5. A note on trust — including agents' reports *about themselves*

A design principle for anyone building on agent output, and for agenttalk's own telemetry:

> **Agent introspection is a source of hypotheses, not truth.**

Apply the same reproduction discipline you'd apply to an agent's *code* to an agent's *claims about itself*. "I felt the team was stuck" is a hypothesis; it's only worth acting on because the commits are absent and `health.json` says idle — **the evidence is the git state, not the narration.** Keep the evidence, discard the story. Practically, this means agenttalk should prefer **evidence-backed signals** (commit deltas, test results, health timestamps, reproduction logs) over agents' prose self-reports when deciding state, routing, and trust. The narrator is itself a non-deterministic contributor.

---

## TL;DR

You started building a channel for two AIs to talk. What the usage reveals is a **software-delivery operating system for agent teams**, whose scarcest resource is **human attention** and whose central discipline is **verify, don't trust the narrator** — applied even to the narrator. Build **liveness (pull-board + never-idle + blocked)** and the **human-attention queue** first; make the **supervisor auto-restart actually work**; then layer the quality machinery. Measure the one number that matters: **features shipped per unit of human attention spent.**
