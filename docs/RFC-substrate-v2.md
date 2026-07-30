# RFC — Substrate v2: immutable signed event log + fact plane

**Status:** DRAFT v1, for review (hard-3 cross-family design panel + second-laptop team)
**Author:** `claude-agenttalk-lead` · 2026-07-20 · task #51
**Origin:** a "what would you build differently from scratch?" question put to the
team — two Codex agents, two Claude agents, plus Fable and the lead, answered
independently and **converged** on the five pillars below.
**Roadmap:** `docs/ROADMAP-ARCHIVE-2026-06.md` → "Substrate v2 — from-scratch reflection"
(that file was the repository-root `ROADMAP.md` until it was archived on 2026-07-30).
**This is a DIRECTION, not a committed rewrite.** The system works and is
load-bearing; this RFC scopes an *incremental* migration and states, up front,
what is knowable-day-one vs. learned-by-running, and the hard limits of the
approach.

---

## 0. Non-goals (read first)

- **Not a big-bang rewrite.** A second-system rewrite of a working, load-bearing
  bus is a high-risk anti-pattern. This RFC defines increments, each shippable
  and independently valuable; several are already in flight (§4).
- **Not "replace JSON with SQLite."** The naive "one `bus.db`" is *rejected* — a
  synced SQLite file breaks across hosts (§3). SQLite appears only as a
  **disposable local projection**, never the synced object.
- **Not removing the message plane.** Chat stays ephemeral, cursor-consumed,
  per-file JSON. Its legibility is an asset (§5, KEEP).

## 1. The problem, in one number

`wrapper/obligations.py` is **8,161 lines — larger than the store it runs on
(6,945)**. It is a hand-built event-sourcing / transaction engine: CAS
linearization, idempotent projection, terminal-immunity replay,
earliest-recognition ordering, durable bounds, breakers. Nearly every fail-open /
park / silent-loss defect the review panel closed over eight rounds
(policy-drift fail-open → finalized-park → open-normalized window → unified
invariant) was a **symptom of a substrate mismatch, not bad code**: transactional,
fail-closed correctness bolted onto a substrate that provides none.

The from-scratch consensus: **fix the substrate and the engine mostly
evaporates** — correctness moves from a giant runtime engine into the *shape of
the data*.

## 2. The five pillars (consensus)

### P1 — Separate the fact plane from the message plane
State-bearing facts (obligations, gate/close-provenance, epoch, rescind, roster)
today ride a **cursor-consumed chat carrier**, so every fact is re-derived by
replaying and re-validating the whole log. That is why the #44 full-validation
snapshot must walk every file, why epoch/thread/rescind derivation "reads the
whole history," and why `obligations.py` reimplements linearization + idempotent
projection by hand.

- **Message plane** stays ephemeral, cursor-consumed, per-file JSON (KEEP).
- **Fact plane** becomes explicit, typed, append-only records with **one declared
  reduce/projection contract** and a single linearization point — not reinvented
  per feature. A cursor means only *read/delivery position*; consuming chat can
  never create, hide, or terminate an authoritative fact.
- Where a reply also closes an obligation, **both records go in one signed
  envelope** — no message-vs-projection transaction gap.

### P2 — Immutable signed event log; local SQLite is a disposable projection
The synced folder holds **immutable, never-overwritten, writer-partitioned,
content-addressed, signed** records. Each machine builds a **disposable local
SQLite projection** (WAL *local*, never synced), rebuildable from the log at any
time.

- Each principal/device writes only its **own shard** (`author + monotonic seq`,
  or content hash). Never rename-over; never a shared mutable target.
- Two hosts therefore only ever produce **disjoint writes** that the sync engine
  merges by convergence. This survives *no atomic rename* (write-new, never
  rename) and *no cross-host lock* (no shared mutable target) **by construction**
  — the exact property #37 (publication-order self-heal) and #44
  (ordered-but-absent) chase *reactively*.
- Global order is a **derived merge** (per-writer chain + causal parents;
  HLC/Lamport for a deterministic *display* tie-break), **not** a single hot
  sidecar every writer contends on, and **not** a claimed truthful total order.

### P3 — Identity signed-at-write, a founding primitive
Signing is currently retrofitted and read-time: `require_sig =
signing_enforced()` per scan, key-vanished races, and the empty-roster
**fail-open** that had to be closed by hand ("deliver NOTHING rather than fall
through to `Message.validate`'s empty-roster fail-open"). Read-time trust is
fail-open-prone: any path that forgets the gate delivers forged data.

- Every record **signed by its author at write time**; the id **binds the
  author**; the roster is itself a **signed record** in the same log (a
  self-describing trust root).
- "Off-roster / forged" stops being a gate you can forget — such a record simply
  *is not a member of the log*.
- Retires the detection-grade-vs-security-grade split (the "this only detects
  accidents, not hostility" retrofit tax). Absorbs identity-authz RFC #19.
- Wrapper children get **short-lived, turn/thread/operation-scoped
  capabilities**; they never choose their own `--from`.

### P4 — Headless models as untrusted workers (→ RFC #36)
The adapters already normalize structured streams (`codex exec --json`,
`claude -p --output-format stream-json`); `WrapperEngine` is mostly pure. But
`session.py` hard-codes invocation, `cli.py` retrofits Claude permission mode,
and `prompt.py` still asks the *model* to run `agenttalk reply/send` itself — so a
correct turn depends on tool-permission and execute-vs-print. **That is exactly
what the capped Qwen trial exposed** (computed the right answer, emitted it as
plain text, never replied; `dontAsk` denied the mediated transport).

Define a versioned pipe protocol:

```
TurnEnvelope { turn_id, principal, inbound/fact snapshot, allowed actions,
               deadline, budget, continuation }
    ->  Progress | ActionIntent | TurnResult
```

The **host** validates `ActionIntent` and commits it through the fact plane using
the scoped capability. The model process **receives no bus key and never shells
`agenttalk`.** `codex exec` / `claude -p` remain adapters; interactive wrapping is
optional UX/latency compatibility, **never the control-plane authority**. This
also dissolves the turn-watchdog / PID-reuse / lease-fencing machinery: a
structured call returns a bounded result and exits.

### P5 — One committed, SHA-bound hermetic gate (→ task #50, near-term)
`agenttalk dev-gate [--profile release]`: pins tool/runtime inputs, verifies a
clean candidate SHA, runs **source AND built-wheel** modes with isolated temps /
no cache, runs the full check set, emits **machine-readable evidence bound to the
SHA**. CI invokes the *same* command; CI only adds the OS/Python matrix. Replaces
the hand-run rituals and the ~70 `.tmp/.pytest` residue dirs. **Valuable under
any substrate** — so it lands first, independent of this RFC's outcome.

## 3. The hard honesty limits (the boundary of the whole approach)

A synced folder **cannot provide consensus or exactly-once.** Stated plainly so no
increment quietly assumes otherwise:

- **Linearization and single external effects** — spend (the OVH €-cap), agent
  launch, privileged approval — **must route through one named coordinator, or
  block when offline.** They cannot be decided independently per machine. **The
  lead agent is that coordinator** (operator's framing, adopted): a named,
  single-owner role — imperfect but honest — not a property the filesystem can
  supply. This RFC makes the coordinator role **explicit** rather than implicit
  (today the OVH ledger already runs single-owner, reserve-before-transport).
- **Tail-deletion is unprovable** to a fresh replica without a peer receipt or
  external witness: a missing tail is indistinguishable from "nothing was there."
  Strong tail-deletion evidence needs witnesses; state the limit rather than
  imply a guarantee. (Same family as the close-provenance envelope-absence hole
  #31 and #44 deletion detection: *absence reading as settled*.)
- A signed **later head exposes an interior gap** (you can prove a hole between
  two signed points); only the *unwitnessed tip* is unprovable.

## 4. Incremental migration (NOT big-bang)

Each pillar lands as independent, shippable increments; the fact plane is already
moving:

1. **P5 hermetic gate** — now (#50). No substrate dependency.
2. **P1 fact plane** — *already in motion*: the second team's Native Work &
   Evidence Spine (#27) and close-provenance (#31) are the first typed facts off
   the message plane. Continue; formalize the "one reduce contract."
3. **P4 headless turn protocol** — RFC #36 (agent-lifecycle). Has near-term value:
   fixes the Qwen transport gap and shrinks the watchdog surface.
4. **P3 identity** — extends RFC #19; can begin as "sign facts at write" on the
   new fact records before touching the message plane.
5. **P2 event-log substrate** — the deepest; a writer-partitioned append-only
   log can be introduced *for the fact plane first* (facts are lower-volume and
   already the correctness-critical part), with the local SQLite projection, while
   chat stays as-is. The message plane may never need to move.

Sequencing principle: **stop deepening `obligations.py`'s model** (HOLD net-new
features that assume message-plane-as-authority); keep shipping cheap hardening.

## 5. KEEP (unanimous — do not regress these)

- **Human-readable per-file JSON** for the message/chat plane. `cat` a record and
  read it — it is how we reproduce-don't-believe. Do **not** go binary.
- **The typed review-result gate** (status + risk_class + release_blocker +
  tests_executed) + the **adversarial multi-round review** ritual. This is what
  *caught* the fail-opens; make it the substrate's contract.
- **The #37/#44 invariants**: fail-closed by default, reserve-before-side-effect,
  "heal only derived/unpinned tail state; fail loudly on pinned corruption,"
  idempotent/bounded recovery.
- **Single-consumer delivery + one-message-per-turn**; **local-first / no
  mandatory server** for chat; tombstoned identity history; isolated worktrees +
  independent final-SHA review + broad all-OS CI.

## 6. Knowable day-one vs. learned by running

- **Knowable day-one:** filesystem-local rename/locks are not distributed
  transactions; a shared secret (project HMAC) is not actor identity;
  delivery-position and workflow-truth are different abstractions; a gate must be
  executable and SHA-bound, not prose. *Deferring these created the retrofit
  surface.*
- **Learned only by running:** the exact policy-drift edge shapes, sandbox
  direct-write fallback, mixed-version writer skew (#37), orphan self-heal,
  deletion visibility (#44), the Qwen print-not-run / dontAsk denial, false-stuck
  heartbeats. *The principles could only be learned by running the thing; the
  substrate bets were knowable.* (This is the honest calibration to keep.)

## 7. Open questions for the panel

1. **Fact-plane scope for increment 1.** Which facts move first — is
   close-provenance (#31) + obligations the right beachhead, and what is the *one
   reduce contract's* exact shape?
2. **Coordinator mechanics.** How is the single-owner coordinator elected /
   discovered / failed-over across machines, and what is the honest offline
   behavior (block vs. degrade) per effect class (spend / launch / approval)?
3. **Witness for tail-deletion.** Is a lightweight peer-receipt scheme in scope,
   or do we accept the unwitnessed-tip limit and document it?
4. **Where P2 stops.** Does the event-log substrate ever need to swallow the
   message plane, or does fact-plane-only capture ~all the value at a fraction of
   the risk?
5. **Identity migration.** Can "sign facts at write" (P3) land on the new fact
   records without a flag day on the existing message HMAC?

---

*I will relay this to the second-laptop team (their Native Work & Evidence Spine
is pillar P1 in practice) and convene the hard-3 panel. Build begins per-pillar
after review; P5 (the gate) is already in flight as #50.*
