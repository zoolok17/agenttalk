# agenttalk - Product Roadmap & Feasibility

**Status:** Official · **Owner:** lead (operator-facing) · **Last updated:** 2026-07-09
**Audience:** maintainers, operators, and agents deciding what to build next.
**Horizon:** pragmatic next 2-3 quarters + a labeled "later" tier.
**Current shipped baseline:** v0.72.2 (2026-07-09). `CHANGELOG.md` remains the release-history source of truth.

Companion docs: `docs/DESIGN.md` (why / architecture) · `docs/ASSURANCE.md` (per-release GOOD/ROBUST/SECURE attestation) · `docs/ISSUES.md` (living work tracker + known limitations) · `docs/DASHBOARD-CONTROL-PLANE-ROADMAP.md` (dashboard control-plane design history) · `CHANGELOG.md`.

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
| **F. Knowledge & Codebase Memory** | domains, durable pointer-notes, lessons, onboarding digests, anchor-relative staleness | Primitives exist; codebase-comprehension product not built |
| **G. Operator Control Plane** | local console, read views, action-gated intent queue, lead-chat, attention/capacity/liveness | Useful local console; needs Work view |
| **H. Execution Runtime** | supervisor, wrapper, session continuity, dead-letter, managed lead-loop, isolated lane worktrees | Powerful but high-maintenance; runtime ergonomics remain active |
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

### Greenfield product build

1. Requirements intake: goals, users, non-goals, constraints, risk tolerance.
2. Spec: acceptance criteria, architecture, test strategy, delivery plan.
3. Bootstrap: repo, stack, CI, checks, docs, code policy, work lanes.
4. Slice delivery: each feature becomes native work with artifacts and review.
5. Release: close over exact revision and required evidence.

### Existing-project change

1. Detect stack, tests, docs, CI, domains, risky seams.
2. Map ownership and path scope before editing.
3. Add characterization tests before risky behavior changes.
4. Deliver small bounded work items through review/gate/close.
5. Preserve discoveries in knowledge notes and lessons.

### Legacy adoption

Legacy adoption remains a flagship workflow, but it is no longer the whole roadmap. It becomes an opinionated composition of native work, knowledge, assurance, runtime, and dashboard:

1. Map: repo-map MVP, domain proposals, build/test inventory.
2. Preserve: curated knowledge notes, lessons, risky seams, characterization targets.
3. Safety net: assurance baselines, gates, CI evidence, required reviews.
4. Change: small gated work items with exact evidence and independent review.

---

## 6. Roadmap

### Quarter 1 - prove the delivery spine

1. **Wrapped-agent runtime ergonomics.** First-class per-agent model / reasoning-effort config, restart-safe session fingerprinting when runtime config changes, and the planned no-visible-CLI/headless supervised mode with full dashboard status.
2. **Native Work RFC.** Specify work item schema, event model, artifact schema, lifecycle, source-of-truth boundaries with lanes/gates/close, evidence tiers, reset semantics, and safety invariants.
3. **Work item MVP.** `work create|list|show|status|assign|start|deliver|abandon`, with one record per item and links to existing lanes, domains, request threads, gates, closes, and artifacts.
4. **Evidence registry MVP.** Write-once artifacts, hash validation, bounded logs, redaction status, exact input binding, trust tiers, and stale-at-head detection.
5. **Pure work check.** `work check` computes `GO`, `HOLD`, or `UNKNOWN` with stable HOLD codes over work state, lane/worktree state, artifact freshness, review state, and gate/close inputs.
6. **Assurance Slice B integration.** Make `assurance.py` artifacts consumable by work checks, gates, and closes without turning assurance into authority.

### Quarter 2 - make it useful for real projects

7. **Project code-policy v1.** Optional `.agenttalk/code-policy.json` declaring required evidence by risk/domain/path, accepted tiers, review lenses, and named checks. No arbitrary shell runner by default.
8. **Generic quality runner / ingestion.** Start with structured argv, no shell by default, explicit cwd/env/network policy, timeouts, output caps, redaction, and fake-adapter failure tests. Add convenience adapters only after the generic contract is proven.
9. **Native review binding.** `review request --work` and review-result consumption tied to reviewed ref, artifact ids, tests executed, findings, residual risk, and release-blocker flags.
10. **Dashboard Work view.** Show work item, owner, worktree, head SHA, dirty/stale state, evidence tiers, review obligations, HOLD reasons, and next owner/action. Keep `/api/state` body-free and log-free.
11. **First-run delivery setup.** One command/dashboard path to check install, skills, roster, operator-facing lead, supervisor, dashboard, code policy, CI visibility, and tool availability.

### Quarter 3 - productize greenfield and existing-project workflows

12. **Greenfield workflow alpha.** Requirements intake -> spec -> stack/bootstrap -> work slices -> evidence/review/gate -> release close.
13. **Existing-project workflow alpha.** Repo detection, domain proposal, test/CI inventory, characterization targets, safe first change.
14. **Legacy adoption alpha.** Repo-map MVP, fan-out comprehension artifacts, knowledge curation, dashboard legacy-map view, unmapped/stale/risky coverage.
15. **CI adapters.** GitHub Actions first: ingest run ids, workflow refs, commit SHA, conclusion, logs/artifacts as evidence. Other providers later.
16. **Repeatable beta packaging.** Golden-path walkthroughs for one greenfield app and one existing-project change, with sample policies and artifact schemas.

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

---

## 8. Top Risks

- **False trust:** operators read green as correctness. Mitigation: language and UI say "evidence current and policy satisfied," not "code correct."
- **State-machine drift:** native work duplicates lane/gate/close truth. Mitigation: work links and projects existing primitives; pure checks derive state.
- **Command-runner risk:** project policy becomes arbitrary code execution. Mitigation: structured argv, opt-in execution, explicit env/network, timeouts, caps, redaction, and failure-injection tests.
- **Policy drift:** a branch changes the rules that judge itself. Mitigation: policy hash binding, base-policy evaluation, explicit waiver for policy changes.
- **Evidence rot:** artifacts point at old SHAs or missing logs. Mitigation: stale-at-head detection and content-addressed artifact references.
- **Maintenance overload:** provider CLI, CI, and scanner formats change. Mitigation: adapter boundaries and generic artifact schema first.

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
