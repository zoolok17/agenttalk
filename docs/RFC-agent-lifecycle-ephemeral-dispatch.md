# RFC — Agent Lifecycle: On-Demand Launch, Bounded Warmth, and Supervisor Dispatch

**Status:** DRAFT v2 — for the Tier-3 adversarial design panel (design-first; no build until the panel passes)
**Author:** `claude-agenttalk-lead` (primary laptop)
**Version:** v2 (2026-07-19) — supersedes v1 (`41bf8ee`); see §12 changelog. v2 corrects the "existing machinery" claims after a cross-family docs-test (codex-2, `rq-572edc9e9cef`).
**Absorbs / relates to:** #16 (fresh-session + rollout bound), #20 (warm-session, reframed as one tier), the "invert-the-listener" supervisor-dispatch consensus; #24 (crash-harness), #25 (cross-platform supervisor); the detection-grade owed-action ledger (a **held candidate**, not on master — see §4.2).

> **Accuracy note (read first).** Every claim in this RFC about *what exists today* is pinned to a ref. "master" = `41bf8ee`. "enforcement candidate" = PR #32 / `7d6fb98` on `reintegrate-enforcement-v311`, which is **held, not merged**. Where a mechanism is proposed but not built, it is marked **[NEW]**. The point of the v1→v2 revision was to stop conflating held/candidate work with shipped code.

---

## 1. Problem — the stall taxonomy (from real incidents)

agenttalk agents run **wrapped**: a long-lived `wrap --loop` process waits on the file bus and runs one CLI turn per inbound message, resuming its session each turn. The stalls we have hit:

| # | Failure (observed) | Root cause (corrected) | 
|---|---|---|
| S1 | **Context-bloat crash** — codex wedged/crashed ~177 turns | a persistent session accumulates context until the CLI host crashes |
| S2 | **Idle-wedge** — wrapper alive but not consuming its inbox | a `--loop` relaunched in a bad state (broken env) sits alive-but-deaf |
| S3 | **Broken launch env** — wrapper runs turns but its bus commands silently fail | missing `AGENTTALK_PY`/`AGENTTALK_ROOT`/`PYTHONPATH` at launch |
| S4 | **Dispatch outage** — a message sits unconsumed | **corrected in v2:** the wrapped loop *does* poll `next_record` every cycle and drives messages that arrived before it started (`wrapper/loop.py:427-460`; regression `tests/test_wrapper_loop.py:312-326`). The real incident was **no wrapper process at all because the supervisor was down** — a dispatch outage, not a pre-wait missed-wake. |
| S5 | **Fresh-kill looks alive** — killed process still "healthy" until heartbeat staleness (codex ~2400s / 40 min) | heartbeat-only liveness (`supervisor.py:3092-3102,362-377`) |

**Common thread:** the long-lived idle loop bundles three roles — *listener*, *stateful session*, *thing-kept-alive*. S1 is the bundling of session+longevity; S5 is longevity+liveness. S4 is not the loop's fault at all — it is the *absence* of the always-on dispatcher. That distinction matters: this proposal **centralizes** the dispatcher, which helps S1/S2/S5 but makes the dispatcher's own availability load-bearing (see §7, §10).

## 2. Goals / Non-goals

**Goals:** eliminate S1 structurally; remove S2 and the "keep N loops alive" burden; keep the always-on surface minimal, observable, cross-platform; preserve responsiveness for genuinely interactive work; make "safe to stop this agent" a *decidable, fail-closed* predicate.

**Non-goals (narrowed in v2):** This is **not** a general job scheduler. But it **does** introduce a bounded new coordination surface — a work-reservation/dedup step and a supervisor↔warm-worker turn channel (§6.4, both **[NEW]**). v2 no longer claims "just composition"; it inventories the new primitives honestly. The file bus, worktrees, gates, closes, and lanes remain the source of truth. Not distributed — still local, single-host per bus.

## 3. Proposal — a hybrid, tiered lifecycle

Split the three bundled roles. Durable state lives in the **bus + worktree + (candidate) obligation ledger**; the **supervisor** becomes the sole always-on listener/dispatcher; the **CLI session** becomes disposable and its persistence is a per-role choice.

### Tier E — Ephemeral (default for stateless-ish roles: reviewers, one-shot fixers)
Launch on demand for one unit of work, run to a **defined terminal** (§4.3), then terminate. No idle process, no bloat, no idle cost.
- *Partly real, review-specific:* `ephemeral_reviewers` + `request-launch` already queue a frozen-revision evidence-**review** request that an enabled supervisor validates and launches, with authority checks, caps, and typed `review-result` completion (`ephemeral.py:74-89,135-184,208-273,358-405`). This tier **generalizes** that review-specific machinery to other roles — the honest verb (the code is explicitly evidence-review-only, `ephemeral.py:1-6`).

### Tier W — Bounded-warm (for a role in an active multi-turn exchange: a dev mid-build, the lead)
Stay warm across turns for responsiveness, **but with a hard context bound**: after *N* turns / *M* tokens / an explicit checkpoint milestone → checkpoint → terminate → relaunch fresh with a curated brief (§4.1). Implements #16; reframes #20 as *bounded* warmth. Requires the **[NEW]** supervisor↔warm-worker channel (§6.4).

### Tier D — Dispatcher (exactly one, always-on)
The supervisor is the only persistent listener; when there is work for an agent not currently warm, it launches the right tier ("invert the listener"). We keep **one** thing alive instead of N — but that one thing is now load-bearing, so its availability and crash-safety are in-scope (§10), and #25 (cross-platform) + #24 (crash-harness) are **prerequisites, not afterthoughts**.

## 4. The hard parts (where the real design work is)

### 4.1 Context continuity — curated checkpoint, NOT raw session-resume **[NEW artifact]**
Raw `resume --last` re-imports the bloat we tore down. The valuable form is a **curated checkpoint**: a short brief carrying only the minimal state a role needs.
- **Reviewer:** ~none — (SHA, lens, prior findings). Nearly stateless → Tier E.
- **Developer mid-build:** the **worktree on disk is the state**; only reasoning/plan is in-session. Checkpoint = sub-goal, done, next, gate status. Cheap. (Invariant: the checkpoint may summarize reasoning but must **never** be the sole record of code changes — those live in commits.)
- **Lead:** the durable control-plane already externalizes most of it; same primitive as compaction checkpoints.
Design work **[NEW]**: a `checkpoint` artifact schema per role + a `--resume-brief` launch path. Open question (§9): bus-visible vs private.

### 4.2 "Finished" needs a real, and currently *incomplete*, definition of done
v1 wrongly implied master already has an owed-action ledger. **It does not** — at `41bf8ee` there is no `obligations.py`; the shipped loop commits every successful drive directly (`wrapper/loop.py:527-534`), and the thread view is pure replay with no persisted state (`threads.py:25-29`). The ledger exists **only** as the **held enforcement candidate** (`7d6fb98`).

Worse, even the candidate does not yet define "done" for **Tier E's first target (reviewers)**: at `7d6fb98`, `_eligibility` classes every non-`question` message NOT_OWED (`obligations.py:2052-2086`) and `status()` counts only open rows (`:7982-8025`), so a pending `review-request` coexists with `open_obligations=0` — it would report a reviewer as safe-to-stop while a review is outstanding. Meanwhile the shipped **thread replay does** model `review-request`/`review-result`/`needs-info` (`threads.py:44-60`).

**Consequence (design requirement, not a footnote):** the "safe to stop" predicate for P1 must be a **fail-closed union of two authorities** — the (candidate) obligation ledger for question/answer obligations *and* the thread-replay view for review obligations — with exact terminal rules per obligation kind, or enforcement must be extended to cover review obligations first. This is now a **P1 gating dependency**, not an assumed primitive.

### 4.3 "Safe to terminate" must be an atomic, testable predicate **[NEW]**
Today `retire_agent` takes retirement/config locks but performs **no** obligation check (`store.py:2161-2190`), and any drain snapshot is unlocked. A naive read-zero-then-kill races new work. Requirement: a **single reservation/CAS boundary** that *fences new assignments through retirement* before teardown — not "read zero, then terminate." Acceptance must include crash/race cases at: message arrival, broadcast fan-out, transfer, and terminal finalization landing *between* "zero open" and the kill; **unreadable obligation state must refuse to terminate** (fail closed).

## 5. What actually exists to build on (corrected)
- `ephemeral_reviewers` + `request-launch`: **queue** a frozen-revision evidence-**review** marker; an enabled, running supervisor later **claims and executes** the `Launch-Spec` (`cli.py:8928-8992`; `supervisor.py:4432-4551,5786-5831`). Real, but review-specific and Windows-only launch spec (`ephemeral.py:315-352`).
- `request-restart`: **queues an authorized manual-relaunch marker** at a supervisor poll; ordinary backoff is bypassed but authorization/protection/cooldown/2nd-ack and launch barriers/preflight **still apply** (`cli.py:8871-8924`; `supervisor.py:3927-3984`). Not an unconditional forced relaunch.
- Durable bus + worktrees + gates/closes/lanes (state survives process death).
- The obligation ledger: **held candidate only** (`7d6fb98`), and incomplete for reviewers (§4.2).
- Compaction/checkpoint discipline (a working precedent for curated hand-off).

**Honest scope:** v2 drops the "~60% already built" figure — there is no denominator or test that makes it verifiable. The reusable, shipped pieces are the review-specific ephemeral path, the durable stores, and heartbeat-staleness liveness. The genuinely new work is inventoried in §6.4.

## 6. Design detail

### 6.1 Liveness vs teardown are different signals (corrected)
- **Liveness** = heartbeat-staleness, not process discovery (`supervisor.py:3092-3102`). Correct and kept.
- **Teardown scope** = provenance: a start-time-guarded, *owned* process-tree kill set (`supervisor.py:3040-3088`) plus a post-kill **survivor/snapshot barrier** (`:5686-5707`) that prevents collateral kill and double-launch. This is load-bearing and must be preserved by any new teardown path. State the split explicitly: **heartbeat decides liveness; start-time/ownership provenance scopes the kill; the survivor barrier gates relaunch.**

### 6.2 Tier E lifecycle
Queue (reserve) → supervisor validates + launches → agent runs to a defined terminal (§4.3 union predicate) → supervisor tears down under §4.3's fenced boundary → identity retired. Note the current lost-state janitor only retires an `adversary-*` identity and has **no** process-snapshot/kill (`supervisor.py:4299-4308`); **orphan-process reaping and per-platform (POSIX) parity are acceptance requirements [NEW]**, not assumed.

### 6.3 Tier W lifecycle
Warm until a bound trips → checkpoint (§4.1) with the teardown predicate satisfied (§4.3) → relaunch with `--resume-brief`. The bound (turns/tokens/milestone; per-role vs global) is an open question (§9).

### 6.4 New primitives this actually requires (inventory) **[NEW]**
1. A **work-reservation / dedup protocol** so the dispatcher can claim "this unit of work is being handled" exactly once (prevents double-launch across polls).
2. A **supervisor↔warm-worker turn channel** to hand a warm session its next unit and to transfer cursor/lease ownership cleanly.
3. The **checkpoint artifact** schema + `--resume-brief` launch path (§4.1).
4. The **fenced teardown boundary** (§4.3) and its crash matrix.
5. **Orphan-reap + POSIX parity** for ephemeral teardown (§6.2).

## 7. Safety invariants (a design violating any is a HOLD)
1. **No teardown with open owed work**, enforced as an *atomic fenced predicate* (§4.3), over the *union* of ledger + thread-replay authorities (§4.2); unreadable state ⇒ do not terminate.
2. **Curated resume ≠ raw resume** — default resume must not re-import an unbounded prior session.
3. **Single dispatcher, hardened & crash-safe** — launch/checkpoint/teardown are **reserve-before-side-effect** with **idempotent recovery**; a dead or crashed supervisor must leave a recoverable state, not a half-applied one (§10).
4. **Launch env is proven at launch (S3)** by a *correlated agent-side bus post*, not a wrapper heartbeat and not `--version` (§11).
5. **Worktree is the dev's durable state** — checkpoints are additive, never the sole record of code.
6. **Liveness/teardown split** is respected (§6.1): heartbeat ≠ authority to kill.
7. **Cost/latency honesty** — tier choice is a documented cost lever (cold-start + re-bill), not hidden.

## 8. Trade-offs (adversarial)
- **Cold-start latency & re-cost** on chatty exchanges → Tier E only for bursty/stateless work; Tier W for interactive roles.
- **Single point of failure**: concentrating always-on into the supervisor beats N loops but raises blast radius → §10 crash-point matrix + #24/#25 are prerequisites; a dead supervisor must fail *safe and recoverable*, which today it does **not** provably do (launch prep is a sequence of separate durable side effects, `supervisor.py:4432-4551`, not one transaction).
- **Checkpoint fidelity**: worktree carries hard state; briefs additive; start conservative.

## 9. Open questions (for the panel)
- OS-level suspend/resume vs terminate + curated relaunch? (Leaning: terminate + relaunch, given durable state.)
- Tier W bound: turns vs tokens vs milestone; per-role or global?
- Checkpoint artifact on the bus (auditable) or private? (Leaning: bus.)
- Dispatch per-message or per-task? Per-task avoids thrash but needs a task boundary the union predicate can express.
- Does the ledger get extended to review obligations, or does the union predicate (§4.2) stay permanent?

## 10. Supervisor crash-safety (promoted from a footnote) **[NEW]**
v1's "a dead supervisor corrupts nothing" was an overclaim. Launch preparation is a chain of separate durable side effects — claim marker, add roster identity, send request, update marker, record state (`supervisor.py:4432-4551`). Required before build: an explicit **invariant + crash-point matrix** covering every launch, checkpoint, teardown, and projection boundary; each step must be **reserve-before-side-effect** with **idempotent recovery** on restart. #24/#25 are the vehicles, not the proof.

## 11. The S3 "proves it can post" acceptance, fully specified **[NEW]**
Current preflight runs `AGENTTALK_PY -m agenttalk --version` (`supervisor.py:5422-5478`) and readiness is the first fresh heartbeat (`:3838-3844`) — **neither proves the model-side bus command path**. Define: the **proof artifact** (a correlated bus post the agent must emit), its **producer** (the wrapped model turn, not the wrapper), **correlation/generation** id, **timeout/retry**, **cleanup**, and the explicit assertion that **a wrapper heartbeat cannot substitute** for a model-side post. A launch that cannot produce it is a failed launch, surfaced, not hidden.

## 12. Changelog v1 → v2 (traceability to the docs-test)
- **F1/S4:** corrected — loop *does* poll; incident was a dispatch outage (supervisor down), not a missed-wake.
- **F2:** ledger reframed as **held candidate (`7d6fb98`), not on master**; removed "already/now."
- **F3:** added §4.2 — candidate enforcement does **not** define "done" for reviewers; P1 requires a fail-closed **union** of ledger + thread-replay (or extend enforcement first).
- **F4:** `request-launch` reworded to "queues a marker an enabled supervisor validates and launches."
- **F5:** ephemeral teardown qualified to the normal Windows one-shot path; **orphan-reap + POSIX parity = acceptance requirements**.
- **F6:** `request-restart` reworded to "queues an authorized manual relaunch; backoff bypassed, other gates remain."
- **F7:** removed "~60%"; narrowed "not a scheduler"; §6.4 inventories the **new** primitives (reservation/dedup, supervisor↔warm channel).
- **F8:** §4.3 — teardown is now an atomic fenced predicate with a crash/race acceptance set.
- **F9:** §6.1 — explicit liveness (heartbeat) vs teardown (provenance/start-time) vs relaunch (survivor barrier) split.
- **F10:** §10 — supervisor crash-safety promoted; "nothing corrupted" replaced by a required invariant + crash-point matrix.
- **F11:** §11 — S3 proof artifact fully specified.
- **Verified (kept):** ASSURANCE Tier-3 + hard floor of 3 (≥2 families, distinct lenses); heartbeat-staleness liveness; `ephemeral_reviewers` reality.

## 13. Tier of this change (for its own review)
**Tier 3** (`docs/ASSURANCE.md:72-78`): governs agent authority/liveness, changes a persistent-state contract (checkpoints, launch policy), core problem is fail-safe/fail-closed teardown. Reviewer floor = **hard floor of 3** independent, cross-family reviewers, distinct predeclared lenses, with **no designer / builder / lead counted** (`ASSURANCE.md:93-102`). This RFC is the artifact that panel reviews before any build.
