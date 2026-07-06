# agenttalk — Product Roadmap & Feasibility

**Status:** Official · **Owner:** lead (operator-facing) · **Last updated:** 2026-07-06
**Horizon:** pragmatic next 2–3 quarters + a labeled "later" tier.

Companion docs: `docs/DESIGN.md` (why / architecture) · `docs/ASSURANCE.md` (per-release GOOD/ROBUST/SECURE attestation) · `docs/ISSUES.md` (living work tracker + known limitations) · `docs/DASHBOARD-CONTROL-PLANE-ROADMAP.md` (the detailed dashboard sub-roadmap — a component of Products F/H below) · `CHANGELOG.md`.

**Provenance:** synthesized from two independently-run team tracks — a builder roadmap and an adversarial feasibility critique — reconciled with the lead's view. The tracks *converged*, which is the basis for the confidence in the scoping below. Keep this document current: when scope or sequencing changes, update it (same anti-drift discipline as `DESIGN.md`).

---

## 1. The verdict, up front

**Feasible — if scoped.** Feasible as a **local-first product *suite*** for trusted single-operator workspaces. **Not** feasible as stated if "comprehensive AI-orchestration platform" implies enterprise auth, remote multi-tenant security, or *autonomous* legacy comprehension.

The builder track and the skeptic track were run independently and **converged** on this — an optimistic architect and a blunt critic landing in the same place is a high-confidence result, not a compromise.

**North star:** *a local AI engineering operating system for trusted workspaces* — not a magic autonomous engineer. **The claim to sell is "faster safe comprehension and safer change," not "full automation" or "proof of correctness."** The human remains the oracle for business intent, public-API contracts, data migration, and release.

---

## 2. Shape: separate products that work together

The healthy shape is **a thin substrate + pluggable products with hard seams**, held together by the load-bearing principles (*bus-is-files, bodies-are-data, fail-closed, advisory-not-authz*). Monolith creep is the #1 structural risk — every capability that folds into one CLI with shared invariants makes every file-lock and provider-quirk release-critical.

| Product | What it is | Independently adoptable? | Today |
|---|---|---|---|
| **A. Core Coordination Bus** | roster, threads, requests, review handoff, broadcast, transcripts | Yes — the minimal install | **Product-grade** |
| **B. Method Engine Adapter** | spec-kitty (and other) workflow engines drive *what next*; agenttalk carries wake/handoff/evidence | Yes | Integration exists; not packaged |
| **C. Craft Skill Pack** | coding/review/test/QA/security/release skills encoding standards + evidence | Mostly | Useful; needs templates |
| **D. Assurance & Release Governance** | gates, closes, specialist sign-off, release ledger, scan evidence | Yes | Strong primitive; needs turnkey close-flow |
| **E. Knowledge & Codebase Memory** | domains, durable pointer-notes, anchor-relative staleness, onboarding digests | Yes | Primitives exist; comprehension product not built |
| **F. Operator Control Plane** | local console, read views, action-gated intent queue, lead-chat, attention/capacity/liveness | Yes (local) | Useful local console; not remote admin |
| **G. Execution Runtime** | supervisor, wrapper, session continuity, dead-letter, managed lead-loop, isolated lane worktrees | Yes | Powerful but **high-maintenance** |
| **H. Legacy Adoption Suite** *(flagship)* | opinionated composition of E+D+G+F+A+skills: map → preserve → safety-net → gated change | Yes, if built as a flow | **Primitives + dogfooded practice, not yet a product** |

Key seam discipline: **assurance produces *evidence*, gates/close *decide* from typed evidence; runtime *executes*, never decides release truth; method owns *task state*, the bus owns *messaging*.** Don't merge those state machines.

---

## 3. The flagship — Legacy Adoption Suite (honestly framed)

The reframe that makes this real: the product is **not** "AI understands your legacy code." It is **"the system makes your uncertainty visible, preserves what gets discovered, and blocks risky changes without evidence."** Less sexy, actually true, and differentiated — nobody does the *trust + preservation* part well.

**The experience (phased):**
- **Phase 0 — point at a repo.** Detect stacks/tests/CI/docs/ownership seams; propose a mapping plan; ask the operator to confirm goals + risk tolerance. No authority claims yet.
- **Phase 1 — map by fan-out.** Scout agents across proposed domains produce *bounded artifacts* — subsystem map, build/test entry points, risky seams, missing docs, characterization-test targets — written back as normal `domains.json`, `knowledge/notes.jsonl`, and DESIGN docs (not private transcripts). Curators verify so staleness stays anchor-relative.
- **Phase 2 — install the safety net.** Configure assurance, baseline scans, gates, risk classes; identify golden/characterization tests *before* changing behavior; work through lanes with worktree isolation.
- **Phase 3 — small gated changes.** Each change starts from a mapped domain + knowledge digest; add characterization tests before invasive edits; `lane deliver` → `gate` → `close` → independent review + assurance evidence.

**Honest gap (primitives vs product):** we *have* durable notes + anchor staleness (`knowledge.py`), domain ownership (`domains.py`), worktree isolation (`lanes.py`), review/close (`gates.py`/`close.py`), scan evidence (`assurance.py`), local ops (`supervisor.py`/`web.py`/`intents.py`). We *don't have*: a first-class repo-map command, a fan-out scout orchestrator with stable artifact contracts, a legacy-onboarding dashboard flow, a characterization-first workflow, or a coverage/drift dashboard telling the operator what's unmapped/stale/unverified. **The MVP must be humble: map, summarize, gate.**

---

## 4. Roadmap

### Quarter 1 — harden & package what exists *(trust is the prerequisite for everything bigger)*
1. **Finish live reliability slices** — the wrapped-fleet follow-ups: launch containment (shipped), structured failure taxonomy + persisted dead-letter tails (shipped, v0.69.2), **identity-bound heartbeat hook (Bug 6, next)**. `supervisor.py`, `wrapper/*`, `redaction.py`.
2. **Assurance Slice B** — make `assurance.py` artifacts consumable by `gate`/`close` (still evidence-not-authority); dashboard + release-summary views for required/skipped scans, new findings, provenance.
3. **Productize first-run setup** — one command/dashboard path that checks install, skills, roster, operator-facing lead, supervisor script, activity hook, dashboard, codex config. Local, explicit, no daemon.
4. **Supervisor & attention observability** — surface *why* the system is waiting/backing-off/refusing/escalating in the dashboard.

### Quarter 2 — legacy-adoption **alpha**
5. **Repo-map MVP** — `agenttalk map init`: detect stacks/docs/tests/CI/likely-domains/unmapped areas → propose `domains.json` + map report + knowledge-note drafts + assurance entries. Reviewable, opt-in.
6. **Fan-out comprehension workflow** — assign scout questions per domain, collect structured summaries, curate into knowledge notes. Reuse typed metadata; avoid new bus kinds.
7. **Characterization-first change workflow** — playbooks/templates to extract behavior specs from tests/logs/outputs; gate changes touching unmapped/fragile domains unless characterization evidence is supplied or explicitly waived.
8. **Dashboard legacy-map view** — mapped domains, stale knowledge, missing tests, risky seams, active lanes, assurance status. Read-first.

### Quarter 3 — make the alpha repeatable
9. **Beta packaging** — a documented golden path (bootstrap → map → curate → configure safety → first safe change → release) with sample-repo walkthroughs + importable artifact schemas.
10. **Knowledge quality & drift management** — coverage metrics (domains w/o notes, stale anchors, high-change areas lacking characterization), *review queues* not automatic truth claims.
11. **Optional runtime polish** — digest-pinned Docker clean-install smoke + scanner image (off by default); runtime self-test.

### Later — explicitly *not* scheduled
Hosted multi-tenant SaaS · enterprise auth / cryptographic human identity · remote runners / cloud fleet · semantic/vector index over large codebases · automated architecture inference claiming completeness · multi-repo program management · skill/method marketplace.

---

## 5. Honest limits & top risks *(keep these load-bearing)*

**Ceilings (from the repo's own posture):** advisory-not-authz · identity = auditable assertion, not a crypto boundary · gates prove *evidence recorded*, not *behavior correct* · knowledge = curated pointers, not a codebase brain · capacity/context = advisory, can't eliminate context loss · execution = local process mgmt, not a cloud scheduler · **legacy intent needs a human oracle.**

**Risks → trigger:**
- **False trust** → operators read "gate green / dashboard clean" as *correctness*. (Mitigate: language + UI that says "evidence recorded," never "verified correct.")
- **Authority confusion** → multiple humans / untrusted agents / prompt-injected commands writing to the bus. (Mitigate: keep the identity model honest; don't over-promise authz.)
- **Monolith creep** → everything moves into one CLI with shared invariants. (Mitigate: hard seams; the product table above.)
- **Maintenance overload** → provider CLI JSON changes, Windows quirks, stale state. (This session's four-fold redaction hardening is the evidence.) (Mitigate: reliability stays ahead of features.)
- **Knowledge rot** → uncurated notes pile up, anchors go stale. (Mitigate: pointer-shaped notes + review queues; resist over-ingest.)
- **Adoption friction** → a legacy team must configure roster/env/skills/supervisor/domains/lanes/gates/CI before seeing *any* value.
- **Safety regression** → dashboard drifts from read-only observability to broad write control.

---

## 6. Team-capacity reality

Mostly AI agents + one operator → **narrow vertical slices, dogfooded, each shipped via the existing cadence.** Every feature is exercised by its own dev loop — that's the moat *and* the constraint. **Do not** attempt broad platform bets while runtime/assurance are still evolving. Explicit *not now*: remote SaaS/multi-user auth · IDE replacement · universal language understanding · automatic large refactors without human-curated characterization · per-ecosystem dependency policy · a new DB/server core · more authority surfaces before dashboard/supervisor/assurance are *boring*.

---

## 7. Lead's synthesis & recommendation

Three points on top of the two tracks:

1. **The convergence is the headline.** An optimistic builder and a deliberately adversarial skeptic, run independently, agreed on scope, on the human-oracle requirement, and on the "reduced-chaos-not-autonomy" positioning. Treat that as a settled foundation, not a debate to keep having.

2. **The real enemy is friction + trust, not feasibility.** Technically, the primitives are further along than most teams' — the risk that actually kills this is (a) *adoption friction* (config-before-value) and (b) *false trust* (green ≠ correct). So the highest-leverage Q1 investment isn't a flashy feature — it's **value-before-configuration** (deliver a read-only "here's what we found / here's what's unmapped and risky" map *before* asking anyone to wire up gates/lanes/CI) and **honest UI language** everywhere evidence is shown.

3. **Dogfood the flagship workflow *before* building the command.** We can run the Phase-1 fan-out map on a real legacy repo *today*, by hand, using existing primitives + a workflow. Do that first to pin down the *artifact contracts* (what a scout returns, what a map report looks like) — then Q2's `agenttalk map` command productizes a proven flow instead of a guessed one. Same "prove the vertical slice, then harden" discipline that's worked all along.

**Concrete next increment:** finish **Bug 6** (identity-bound heartbeat hook — v0.69.4) to close the reliability arc, then **Assurance Slice B** (artifact→gate/close consumption + dashboard evidence). Those make the *runtime and the evidence* trustworthy — the precondition for the legacy-adoption alpha. Hold the legacy-map command until the fan-out map has been dogfooded once by hand.

**Bottom line:** yes, this is achievable — as a suite of local-first products with a legacy-adoption flagship, built in narrow dogfooded slices over 2–3 quarters, sold as *safer, more understandable AI-assisted change on codebases you don't fully understand.* Not as an autonomous modernization engine. The scoping is what makes it real.
