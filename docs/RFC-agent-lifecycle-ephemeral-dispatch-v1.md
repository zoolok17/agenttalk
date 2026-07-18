# RFC — Agent Lifecycle: On-Demand Launch, Bounded Warmth, and Supervisor Dispatch (v1)

**Status:** DRAFT — for adversarial design review (design-first; no build until reviewed)
**Author:** `claude-agenttalk-lead` (primary laptop)
**Date:** 2026-07-19
**Supersedes/absorbs:** task #16 (per-agent fresh-session + rollout size bound), the "invert-the-listener" supervisor-dispatch consensus, and reframes #20 (warm-session) as one tier of a spectrum rather than a standalone feature.
**Relates to:** #24 (supervisor crash-harness), #25 (cross-platform supervisor), the detection-grade owed-action ledger (defines "task finished").

---

## 1. Problem — the stall taxonomy (from real incidents)

agenttalk agents run **wrapped**: a long-lived `wrap --loop` process sits waiting on the file bus and runs one CLI turn per inbound message, resuming its session each turn. Every stall we have actually hit traces to that long-lived-loop model:

| # | Failure (observed) | Root cause | Frequency |
|---|---|---|---|
| S1 | **Context-bloat crash** — codex wedged/crashed at ~177 turns | a persistent session accumulates context until the CLI host crashes | recurring; most damaging |
| S2 | **Idle-wedge** — wrapper alive (PID present) but not consuming its inbox | a `--loop` relaunched in a bad state (broken env, or no wake) sits "alive but deaf" | seen this session (PID 4740) |
| S3 | **Broken launch env** — wrapper runs turns but its bus commands silently fail | missing `AGENTTALK_PY`/`AGENTTALK_ROOT`/`PYTHONPATH` at launch | recurring after restarts |
| S4 | **No wake** — an idle loop never picks up a message that arrived before its wait began | the loop is the listener; nothing pokes it | recurring when supervisor down |
| S5 | **Fresh-kill looks alive** — supervisor can't distinguish a killed process from a silently-thinking one until heartbeat staleness (up to ~40 min for codex) | heartbeat-only liveness | recurring |

**Common thread:** the long-lived idle loop is simultaneously the *listener*, the *stateful session*, and the *thing that must be kept alive*. Bundling those three roles into one fragile process is the source of the stalls.

## 2. Goals / Non-goals

**Goals**
- Eliminate the context-bloat crash class (S1) structurally, not by tuning.
- Remove idle-wedge (S2) and the "kept-alive N loops" burden (S4/S5) as failure surfaces.
- Keep the single always-on component minimal, observable, and cross-platform.
- Preserve responsiveness for genuinely interactive multi-turn work.
- Make "safe to stop this agent" a *decidable* condition, not a guess.

**Non-goals**
- Not a scheduler/orchestrator rewrite. The file bus, worktrees, gates, closes, and lanes remain the source of truth.
- Not removing human/operator authority. Launch policy stays operator-owned.
- Not a distributed system — still local, single-host per bus.

## 3. Proposal — a hybrid, tiered lifecycle

Split the three bundled roles. The **bus + worktree + obligation ledger** hold durable state; the **supervisor** is the sole always-on listener/dispatcher; the **CLI session** becomes disposable and its persistence is a per-role choice.

### Tier E — Ephemeral (default for stateless-ish roles: reviewers, one-shot fixers)
Launch on demand for a specific unit of work (a review against a SHA, a scoped fix), run to obligation-satisfied, then **terminate**. No idle process, no bloat, no idle cost.
- *Already partially real:* the supervisor config has `ephemeral_reviewers`, and `request-launch` already does "spin up an on-demand agent against a revision, then it's gone." This tier generalizes a proven mechanism.

### Tier W — Bounded-warm (for a role in an active multi-turn exchange: a dev mid-build, the lead)
Stay warm across turns for responsiveness, **but with a hard context bound**: after *N* turns or *M* tokens (or on an explicit checkpoint milestone), **checkpoint → terminate → relaunch fresh with a curated brief**. This is "suspend then relaunch with context," applied only where warmth earns its cost. Directly implements #16; reframes #20 as *bounded* warmth.

### Tier D — Dispatcher (exactly one, always-on)
The supervisor is the only persistent listener. It watches the bus, and when there is work for an agent that is not currently warm, it **launches** the right tier. This is the "invert the listener" consensus. Consequence: we keep **one** thing alive instead of N — but that one thing must be bulletproof (hence #25 cross-platform + #24 crash-harness are prerequisites, not afterthoughts).

## 4. The two hard parts (where the real design work is)

### 4.1 Context continuity — curated checkpoint, NOT raw session-resume
"Relaunch with previous context" has a trap: CLI session-resume (`resume --last`) re-imports **exactly the bloat we tore down**. The valuable form is a **curated checkpoint**: a short, purpose-built brief that carries only the minimal state a role needs to continue.

Per-role carried state (initial cut):
- **Reviewer:** ~none. A review is (SHA, lens, prior findings-if-re-review). Nearly stateless → Tier E trivially.
- **Developer mid-build:** the **worktree on disk is the state** (code, branch, base) — already durable. Only the *reasoning/plan* is in-session. Checkpoint = a few lines: current sub-goal, what's done, what's next, gate status. Cheap.
- **Lead:** the durable control-plane (tasks, bus threads, memory) already externalizes most of it; the checkpoint discipline we use for compaction is the same primitive.

Design work: define a `checkpoint` artifact per role and a `--resume-brief` launch path that seeds it. This is the "checkpoint-after-milestone" idea made mechanical.

### 4.2 "Finished" must be decidable before we stop an agent
Suspending an agent with unfinished **owed work** silently drops it. We now have the mechanism to avoid that: the **detection-grade owed-action ledger** already defines obligation-satisfied. Rule: an agent is **safe to terminate** only when it holds no open obligation (or its obligations are durably transferred/finalized). The ledger becomes the gate for the E-tier teardown and the W-tier checkpoint boundary.

## 5. What we already have to build on
- `ephemeral_reviewers` config + `request-launch` (on-demand launch of a scoped agent).
- `request-restart` (force a relaunch) and the supervisor plan/decision table.
- Durable bus + worktrees + gates/closes/lanes (state survives process death by construction).
- The obligation ledger (a durable definition of "done").
- The compaction/checkpoint discipline (a working precedent for curated hand-off).

Roughly 60% of the primitives exist; the RFC is mostly *composition + two new artifacts* (per-role checkpoint, resume-brief launch), not green-field.

## 6. Safety invariants (a design violating any is a HOLD)
1. **No teardown with open owed work** — the obligation ledger gates every E-teardown and W-checkpoint. Fail closed: if obligation state is unreadable, do **not** terminate.
2. **Curated resume ≠ raw resume** — the default resume path must not re-import an unbounded prior session; bloat-shedding is the point.
3. **Single dispatcher, hardened** — exactly one always-on component; it must fail safe (heartbeat-staleness, not PID-tree guessing) and be cross-platform before this ships broadly.
4. **Launch env is verified at launch** — a launched agent proves it can post to the bus (S3) before it is considered live; a launch that can't post is a failed launch, surfaced, not hidden.
5. **Worktree is the dev's durable state** — a checkpoint may summarize reasoning but must never be the sole record of code changes; those live in the worktree/commits.
6. **Cost/latency honesty** — cold-start latency and per-relaunch re-billing are real; the tier choice is a documented cost lever, not hidden.

## 7. Trade-offs (adversarial, stated plainly)
- **Cold-start latency & re-cost** on chatty exchanges: Tier E teardown-per-message would make a multi-round review Q&A slow and re-billed. Mitigation: Tier W for genuinely interactive roles; Tier E only for bursty/stateless work.
- **Single point of failure**: concentrating "always-on" into the supervisor is better than N loops, but raises the blast radius of a supervisor bug. Mitigation: #24/#25 first; a dead supervisor must fail *safe* (agents simply aren't dispatched; nothing is corrupted).
- **Checkpoint fidelity**: a bad curated brief loses useful context. Mitigation: worktree carries the hard state; briefs are additive; start conservative (more context) and tighten.

## 8. Phasing
- **P1** — Formalize Tier E for reviewers (mostly wiring existing `ephemeral_reviewers`/`request-launch`); prove teardown-gated-by-obligation-ledger end to end.
- **P2** — Tier W bounded-warm for developers: context bound (#16) + per-role checkpoint artifact + `--resume-brief` launch.
- **P3** — Make the dispatcher the default wake path (invert-the-listener), contingent on #24 + #25.
- **P4** — Deprecate the always-on idle `--loop` as the default; keep it only as an explicit opt-in.

## 9. Open questions (for the panel)
- Is OS-level process *suspend/resume* ever worth it, or is *terminate + curated relaunch* always cleaner given our durable state? (Leaning: terminate + relaunch.)
- What is the right default bound for Tier W (turns vs tokens vs milestone), and is it per-role or global?
- Should the checkpoint artifact live on the bus (visible/auditable) or be a private per-agent file? (Leaning: bus — auditable, matches "no hidden state.")
- Does the dispatcher launch **per-message** or **per-task** (a task may span several messages)? Per-task avoids thrash but needs a task boundary the ledger can express.

## 10. Tier of this change (for its own review)
This is **Tier 3** (per `docs/ASSURANCE.md`): it governs agent authority/liveness, changes a persistent-state contract (checkpoints, launch policy), and its core problem is fail-safe/fail-closed teardown semantics. Reviewer floor = **hard floor of 3** independent, cross-family reviewers with distinct predeclared lenses (no designer/lead counted). This RFC is the design artifact that panel reviews before any build.
