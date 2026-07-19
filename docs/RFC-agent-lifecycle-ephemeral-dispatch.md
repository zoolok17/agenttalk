# RFC — Agent Lifecycle: On-Demand Launch, Bounded Warmth, and Supervisor Dispatch

**Status:** DRAFT v3 — for the Tier-3 adversarial design panel (design-first; no build until the panel passes)
**Author:** `claude-agenttalk-lead` (primary laptop)
**Version:** v3 (2026-07-19) — supersedes v2 (`8f2ac45`) / v1 (`41bf8ee`); see §12. v2 folded a first cross-family docs-test (codex-2); v3 folds the second (`rq-6075627b312e`): a broader safe-stop authority, a completed primitive inventory, a corrected survivor-barrier scope, honest "requirements-not-spec" wording, a restored dependency table, and citation repairs.
**Absorbs / relates to:** #16 (fresh-session + rollout bound), #20 (warm-session, reframed as one tier), the "invert-the-listener" supervisor-dispatch consensus; #24 (crash-harness), #25 (cross-platform supervisor); the detection-grade owed-action ledger (a **held candidate**, not shipped — see §4.2).

> **Accuracy note (read first).** Claims about *what exists today* are pinned to refs. **`41bf8ee` is the shipped-code baseline; `8f2ac45` is the document ref** (source is unchanged between them). "enforcement candidate" = PR #32 lineage `7d6fb98` — now **merged to master `12c7d21`** as of 2026-07-19, but the RFC keeps citing the candidate ref for provenance. Mechanisms proposed but not built are marked **[NEW]**. The v1→v2→v3 revisions exist to stop conflating held/candidate/unbuilt work with shipped code.

---

## 1. Problem — the stall taxonomy (from real incidents)

agenttalk agents run **wrapped**: a long-lived `wrap --loop` process waits on the file bus and runs one CLI turn per inbound message, resuming its session each turn.

| # | Failure (observed) | Root cause (corrected) |
|---|---|---|
| S1 | **Context-bloat crash** — codex wedged/crashed ~177 turns | a persistent session accumulates context until the CLI host crashes |
| S2 | **Idle-wedge** — wrapper alive but not consuming its inbox | a `--loop` relaunched in a bad state (broken env) sits alive-but-deaf |
| S3 | **Broken launch env** — wrapper runs turns but its bus commands silently fail | missing `AGENTTALK_PY`/`AGENTTALK_ROOT`/`PYTHONPATH` at launch |
| S4 | **Dispatch outage** — a message sits unconsumed | the wrapped loop *does* poll `next_record` every cycle and drives pre-queued records (`wrapper/loop.py:427-460`; `tests/test_wrapper_loop.py:312-326`). Source disproves a missed-wake; the specific incident (no wrapper because the supervisor was down) is **operator-observed**, not provable from source. |
| S5 | **Fresh-kill looks alive** — killed process still "healthy" until heartbeat staleness (codex ~2400s / 40 min) | heartbeat-only liveness (`supervisor.py:3092-3102,362-377`) |

**Common thread:** the long-lived idle loop bundles three roles — *listener*, *stateful session*, *thing-kept-alive*. S4 is the *absence* of an always-on dispatcher, not a loop defect. This proposal **centralizes** the dispatcher, which helps S1/S2/S5 but makes the dispatcher's own availability load-bearing (§7, §10).

## 2. Goals / Non-goals

**Goals:** eliminate S1 structurally; remove S2 and the "keep N loops alive" burden; keep the always-on surface minimal, observable, cross-platform; preserve responsiveness for interactive work; make "safe to stop this agent" a *decidable, fail-closed* predicate.

**Non-goals (narrowed):** not a general job scheduler. But it **does** add a bounded new coordination surface — a work-reservation/dedup step and a supervisor↔warm-worker channel (§6.4, **[NEW]**). v2 dropped the "just composition" framing; §6.4 inventories the new primitives, and §8.5 gives the dependency order. The bus, worktrees, gates, closes, and lanes remain the source of truth. Local, single-host per bus.

## 3. Proposal — a hybrid, tiered lifecycle

Split the three bundled roles. Durable state lives in the **bus + worktree + (candidate) obligation ledger**; the **supervisor** becomes the sole always-on listener/dispatcher; the **CLI session** becomes disposable, its persistence a per-role choice.

### Tier E — Ephemeral (default for stateless-ish roles: reviewers, one-shot fixers)
Launch on demand for one unit of work, run to a **terminal** (terminal rules TBD under §4.2, projected through §4.3's fence), then terminate. No idle process, no bloat, no idle cost.
- *Partly real, review-specific:* `ephemeral_reviewers` + `request-launch` already queue a frozen-revision evidence-**review** request that an enabled supervisor validates and launches, with authority checks, caps, and typed `review-result` completion (`ephemeral.py:74-89,135-184,208-273,358-405`). Generalizing this review-specific machinery to other roles is **[NEW, unbuilt]** — the code is explicitly evidence-review-only (`ephemeral.py:1-6`).

### Tier W — Bounded-warm (dev mid-build, the lead)
Stay warm across turns for responsiveness, **but with a hard context bound**: after *N* turns / *M* tokens / an explicit checkpoint milestone → checkpoint → terminate → relaunch fresh with a curated brief (§4.1). Implements #16; reframes #20 as *bounded* warmth. Requires the **[NEW]** supervisor↔warm-worker channel + durable bound accounting (§6.4). Today continuous `wrap` has `max_turns=None` (`cli.py:9617-9621`) — no bound exists.

### Tier D — Dispatcher (exactly one, always-on) **[NEW, unbuilt]**
The supervisor becomes the only persistent listener; when there is work for an agent not currently warm, it launches the right tier ("invert the listener"). We keep **one** thing alive instead of N — but that one thing is load-bearing, so its availability and crash-safety are in-scope (§10), and #25 (cross-platform) + #24 (crash-harness) are **prerequisites, not afterthoughts**. Today `build_report`/`plan_actions` handle configured liveness and explicit launch markers, **not** general inbox dispatch (`supervisor.py:3179-3344,4313-4351`).

## 4. The hard parts (where the real design work is)

### 4.1 Context continuity — curated checkpoint, NOT raw session-resume **[NEW artifact]**
Raw `resume --last` re-imports the bloat we tore down. The valuable form is a **curated checkpoint**: a short brief carrying only the minimal state a role needs.
- **Reviewer:** ~none — (SHA, lens, prior findings). Nearly stateless → Tier E.
- **Developer mid-build:** the **worktree on disk is the state**; only reasoning/plan is in-session. Checkpoint = sub-goal, done, next, gate status. (Invariant: the checkpoint may summarize reasoning but must **never** be the sole record of code changes — those live in commits.)
- **Lead:** the durable control-plane already externalizes most of it; same primitive as compaction checkpoints.
Design work **[NEW]**: a `checkpoint` artifact schema per role + a `--resume-brief` launch path. Open question (§9): bus-visible vs private.

### 4.2 "Finished" needs a real, currently *incomplete*, definition of done
v1 wrongly implied master already had an owed-action ledger. At `41bf8ee` there is no `obligations.py`; the shipped loop commits every successful drive directly (`wrapper/loop.py:527-534`), and the thread view is pure replay with no persisted state (`threads.py:25-29`). The ledger exists as the **held enforcement candidate** (`7d6fb98`, now merged to `12c7d21`).

Even the candidate does not define "done" for **Tier E's first target (reviewers)**: `_eligibility` classes every non-`question` message NOT_OWED (`7d6fb98:obligations.py:2079-2086`) and `status()` counts only open rows (`:8055-8058`), so a pending `review-request` coexists with `open_obligations=0`. The shipped thread replay *does* model `review-request`/`proposal`/`question` transitions (`threads.py:76-90`; kinds at `store.py:482`), and the real wrapper *also* delivers ordinary unread `message`/`note` records (`wrapper/recv_api.py:78-90`).

**Consequence — the "safe to stop" predicate must read a canonical WORK ENVELOPE, not two authorities.** A ledger + review-thread union can still read zero while a dispatchable message or an active reservation/lease exists. The fenced predicate (§4.3) must be computed over **all four in-flight authorities**:
1. candidate obligation ledger (question/answer obligations),
2. thread-replay obligations (review-request / proposal / needs-info),
3. unread dispatchable `message`/`note` records (`recv_api`),
4. active **work reservations + warm-worker leases** (the §6.4 [NEW] in-flight states) — atomically projected into the authority *before* launch.
Fail-closed: unreadable state ⇒ do not terminate. Either enforcement is extended to a single canonical tracked-work envelope, or the predicate reads all four with exact terminal rules per kind. This is a **P1 gating dependency**, not an assumed primitive.

### 4.3 "Safe to terminate" must be an atomic, testable predicate **[NEW]**
Today `retire_agent` takes retirement/config locks but performs **no** owed-work check (`store.py:2161-2190`); any drain snapshot is unlocked. A naive read-zero-then-kill races new work. Requirement: a **single reservation/CAS boundary** that *fences new assignments (all four §4.2 sources) through retirement* before teardown — not "read zero, then terminate." Crash/race acceptance at: message arrival, broadcast fan-out, transfer, reservation/lease grant, and terminal finalization landing *between* "zero" and the kill; unreadable state refuses to terminate.

## 5. What actually exists to build on (corrected)
- `ephemeral_reviewers` + `request-launch`: **queue** a frozen-revision evidence-**review** marker; an enabled, running supervisor later **claims and executes** the `Launch-Spec` (`cli.py:8928-8992`; `supervisor.py:4432-4551,5786-5831`). Real, but review-specific and Windows-only launch spec (`ephemeral.py:315-352`).
- `request-restart`: **queues an authorized manual-relaunch marker**; ordinary backoff is bypassed but authorization/protection/cooldown/2nd-ack and launch barriers/preflight **still apply** (`cli.py:8871-8924`; `supervisor.py:3927-3984,5686-5728`).
- Durable bus + worktrees + gates/closes/lanes.
- The obligation ledger: **candidate** (`7d6fb98`), incomplete for reviewers (§4.2).
- Compaction/checkpoint discipline (a precedent for curated hand-off).

**Honest scope:** no "% already built" figure — no denominator makes it verifiable. Reusable shipped pieces: the review-specific ephemeral path, the durable stores, heartbeat-staleness liveness. New work is inventoried in §6.4; ordered in §8.5.

## 6. Design detail

### 6.1 Liveness vs teardown are different signals (corrected)
- **Liveness** = heartbeat-staleness, not process discovery (`supervisor.py:3092-3102`). Kept.
- **Teardown scope** = provenance: a start-time-guarded, *owned* process-tree kill set (`supervisor.py:3040-3088`).
- **Relaunch gate** = the post-kill survivor/snapshot barrier (`supervisor.py:5686-5707`) — but it recognizes **only same-agent `agenttalk wrap`/`wait` command lines** (`:2766-2825`, esp. `:2793-2803`). Direct probe: a live legacy-direct `claude.exe --resume …` → `allow_launch=true, survivor_count=0`; a wrapped invocation → blocked, `survivor_count=1`. So the survivor barrier gates **wrapped** relaunches; **legacy-direct double-launch suppression depends on durable reserve-before-spawn state, not the barrier.** Split: heartbeat decides liveness; provenance/start-time scopes the kill; the survivor barrier gates wrapped relaunch; reserve-before-spawn covers legacy-direct.

### 6.2 Tier E lifecycle
Reserve → supervisor validates + launches → agent runs to a terminal (rules TBD §4.2, projected through §4.3's fence) → supervisor tears down under §4.3's fenced boundary → identity retired. The current lost-state janitor only *plans* retirement of an `adversary-*` identity (`supervisor.py:4299-4308`), and its retire-only mutation (`:4665-4674`) has **no** process-snapshot/kill; **guaranteed orphan-reaping + POSIX parity are acceptance requirements [NEW]**.

### 6.3 Tier W lifecycle
Warm until a bound trips → checkpoint (§4.1) with the §4.3 predicate satisfied → relaunch with `--resume-brief`. The bound (turns/tokens/milestone; per-role vs global) is an open question (§9); durable bound accounting is [NEW] (§6.4).

### 6.4 New primitives this requires — inventory (non-exhaustive) **[NEW]**
1. **Work-reservation / dedup protocol** — claim "this unit is being handled" exactly once (prevents double-launch across polls); projected into §4.2's envelope.
2. **Supervisor↔warm-worker turn channel** — hand a warm session its next unit; transfer cursor/lease ownership cleanly.
3. **Checkpoint artifact** schema + `--resume-brief` launch path (§4.1).
4. **Fenced teardown boundary** (§4.3) and its crash matrix — *and* the same reserve-before-side-effect + idempotent-recovery journaling for **launch, checkpoint, and projection** (not teardown alone; §10).
5. **Guaranteed orphan-reap + POSIX parity** for ephemeral teardown (§6.2).
6. **Supervisor inbox discovery / eligibility / tier-routing** — general mailbox dispatch, which `plan_actions` does not do today.
7. **Operator-owned lifecycle policy** — which agents are Tier E vs W, bounds, who may launch/checkpoint.
8. **Durable Tier-W bound accounting + trip + checkpoint orchestration.**
9. **The S3 launch-proof protocol** (§11) — still a requirement, not a spec.

This list is **non-exhaustive**; the Tier-D dispatcher and the generalized Tier-E transition are themselves **[NEW, unbuilt]** (§3).

## 7. Safety invariants (a design violating any is a HOLD)
1. **No teardown with open owed work**, as an *atomic fenced predicate* (§4.3) over the **canonical work envelope** (§4.2: ledger + thread-replay + unread dispatchable records + active reservations/leases); unreadable state ⇒ do not terminate.
2. **Curated resume ≠ raw resume.**
3. **Single dispatcher, hardened & crash-safe** — launch/checkpoint/teardown/projection are **reserve-before-side-effect** with **idempotent recovery**; a crashed supervisor leaves a recoverable, not half-applied, state (§10).
4. **Launch env proven at launch (S3)** by a *correlated agent-side bus post* with a real provenance mechanism (§11) — not a wrapper heartbeat, not `--version`.
5. **Worktree is the dev's durable state** — checkpoints additive, never the sole code record.
6. **Liveness/teardown split** respected (§6.1): heartbeat ≠ authority to kill; the survivor barrier only gates *wrapped* relaunch.
7. **Cost/latency honesty** — tier choice is a documented cost lever.

## 8. Trade-offs & phasing

### 8.1 Trade-offs (adversarial)
- **Cold-start latency & re-cost** → Tier E for bursty/stateless; Tier W for interactive.
- **Single point of failure**: concentrating always-on into the supervisor beats N loops but raises blast radius → §10 + #24/#25 prerequisites; a dead supervisor must fail *safe and recoverable*, which today it does **not** provably do (launch prep is separate side effects, `supervisor.py:4432-4551`).
- **Checkpoint fidelity**: worktree carries hard state; briefs additive; start conservative.

### 8.5 Dependency / phase table **[restored]**
| Phase | Deliverable | Hard dependency |
|---|---|---|
| **P1a** | Canonical work-envelope authority (§4.2, all four sources) + fenced teardown predicate (§4.3) | candidate ledger merged; `recv_api`/threads read paths |
| **P1b** | Work-reservation/dedup protocol (§6.4-1) projected into the envelope | P1a |
| **P1c** | Crash-safety: reserve-before-side-effect + idempotent recovery for launch/checkpoint/teardown/projection (§10) | **#24 crash-harness** to prove it |
| **P2** | Tier E generalized beyond reviewers; guaranteed orphan-reap; **POSIX parity** | P1a-c; **#25 cross-platform supervisor** |
| **P3** | Tier W bound accounting + checkpoint artifact + `--resume-brief` + supervisor↔warm channel | P1a-c |
| **P4** | Tier D as the default wake path (invert-the-listener); deprecate always-on idle `--loop` | P2 + P3; **#24 + #25 both green** |
| **cross** | S3 launch-proof protocol (§11) | before any tier trusts a fresh launch |

**#24 gates P1c and P4** (crash-safety proof); **#25 gates P2 and P4** (POSIX parity). No tier ships before its row's dependency is green.

## 9. Open questions (for the panel)
- OS-level suspend/resume vs terminate + curated relaunch? (Leaning: terminate + relaunch.)
- Tier W bound: turns vs tokens vs milestone; per-role or global?
- Checkpoint artifact on the bus (auditable) or private? (Leaning: bus.)
- Dispatch per-message or per-task? Per-task avoids thrash but needs a task boundary the envelope can express.
- Extend the ledger to a single canonical tracked-work envelope, or keep a multi-source predicate (§4.2) permanently?

## 10. Supervisor crash-safety **[NEW]**
v1's "a dead supervisor corrupts nothing" was an overclaim. Launch preparation is a chain of separate durable side effects — claim marker, add roster identity, send request, update marker, record state (`supervisor.py:4432-4551`). Required before build: an explicit **invariant + crash-point matrix** covering every launch, checkpoint, teardown, and projection boundary; each step **reserve-before-side-effect** with **idempotent recovery** on restart. #24/#25 are the vehicles, not the proof.

## 11. The S3 launch-proof — requirements still to specify **[NEW]**
Current preflight runs `AGENTTALK_PY -m agenttalk --version` (`supervisor.py:5422-5478`) and readiness is the first fresh heartbeat (`:3838-3844`) — **neither proves the model-side bus command path** (the S3 failure). This section states *requirements*, not a spec; the protocol is unbuilt (§6.4-9). Still to define: the proof-artifact **message kind/schema**; **issuer/verifier**; **nonce issuance + one-time consumption**; **ordering boundary**; concrete **timeout/retry** values; **restart/replay/duplicate** semantics; **cleanup** transition; and an **executable oracle**. **Provenance caveat:** a bus post alone cannot prove "model, not wrapper" — roster identity is selected by CLI flag/environment and `Store.send` validates/signs the *asserted* identity (`cli.py:195-220`; `store.py:2483-2555`). So the proof needs a provenance mechanism distinguishing a model-driven post from a wrapper-driven one; that mechanism is itself part of the unspecified work.

## 12. Changelog
**v1 → v2** (first docs-test, 11 findings): S4 corrected to a dispatch outage; ledger reframed as held candidate not on master; §4.2 reviewer-"done" gap added; `request-launch`/`request-restart` reworded to "queue a marker the supervisor executes"; removed "~60%"; §4.3 fenced teardown; §6.1 liveness/teardown split; §10 crash-safety promoted; §11 S3 requirement.

**v2 → v3** (second docs-test, `rq-6075627b312e`, 8 findings):
- **[gap]** §4.2 safe-stop authority broadened from a 2-way union to a **4-source canonical work envelope** (ledger + thread-replay + unread dispatchable records + active reservations/leases), atomically projected before launch; §4.3/§7 updated.
- **[gap]** §6.4 inventory completed (inbox discovery/routing, operator lifecycle policy, Tier-W bound accounting, launch/checkpoint/projection journaling, S3 protocol) and labeled **non-exhaustive**; Tier-D + generalized Tier-E marked **[NEW, unbuilt]**.
- **[overclaim]** §11 retitled "requirements still to specify"; added the model-vs-wrapper **provenance caveat** (identity is CLI/env-asserted and signed as such).
- **[factual-error]** §6.1 survivor barrier narrowed: it gates **wrapped** relaunch only (recognizes same-agent `wrap`/`wait`); legacy-direct suppression depends on reserve-before-spawn (direct probe cited).
- **[contradiction]** Tier E "defined terminal" → "terminal rules TBD §4.2, projected through §4.3's fence."
- **[overclaim]** teardown wording → "best-effort provenance-scoped kill on the normal Windows path; guaranteed orphan reaping is NEW."
- **[gap]** restored a **dependency/phase table** (§8.5) naming which boundary #24/#25 gate; defined P1a–P4.
- **[citation]** fixed `status()` (`:8055-8058`), thread transitions (`threads.py:76-90`), barrier/preflight (`:5686-5728`), janitor-plan vs retire-mutation (`:4299-4308` / `:4665-4674`), and header "shipped baseline `41bf8ee` vs document ref `8f2ac45`."
- **Verified (kept):** ASSURANCE Tier-3 + hard floor of 3 (`:72-78`, `:93-102`); heartbeat-staleness; `ephemeral_reviewers` reality.

## 13. Tier of this change (for its own review)
**Tier 3** (`docs/ASSURANCE.md:72-78`): governs agent authority/liveness, changes a persistent-state contract (checkpoints, launch policy), core problem is fail-safe/fail-closed teardown. Reviewer floor = **hard floor of 3** independent, cross-family reviewers, distinct predeclared lenses, with **no designer / builder / lead counted** (`ASSURANCE.md:93-102`). This RFC is the artifact that panel reviews before any build.
