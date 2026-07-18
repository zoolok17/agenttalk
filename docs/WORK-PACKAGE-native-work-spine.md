# Work Package — Native Work & Evidence Spine (independent team)

**Owner team:** the second laptop (Windows; Claude + Codex agents), operating an **independent** agenttalk instance.
**Architect (this doc):** `claude-agenttalk-lead` on the primary laptop.
**Status:** assignment brief — stand up the team from §8, deliver §4 in order.
**Why this feature:** roadmap `docs/ROADMAP.md` §4 + §6 Q1 items 3–7 name this the **#1 priority** and "the missing product layer." It is a brand-new module with its own storage namespace, so it can be built fully in parallel with the primary team's work without collision.

---

## 1. How the two teams stay independent (read first)

The two laptops **do not share the file-backed bus** — agenttalk is local, so there is no cross-machine roster/messaging. That is intentional; it forces clean independence. Consequences:

- **Integration point is git/GitHub only.** The team works on a dedicated branch and integrates via **PRs**. No shared runtime state.
- **Coordination is async** — the operator relays status between teams, or a periodic (e.g., daily) git pull + a short written sync. Do not assume real-time coordination.
- **Ownership boundary is a hard rule** (see §2). Stay inside your namespace and you will never conflict with the primary team.

---

## 2. Ownership boundary — what you own vs must NOT touch

**You OWN (create/modify freely):**
- New module(s): `src/agenttalk/work.py` (+ submodules as needed, e.g. `work_store.py`, `evidence.py`, `work_check.py`).
- New CLI surface: `agenttalk work ...` (wire it into `cli.py` **only** via a single dispatch hook — see §6 for the low-collision way to do this).
- New storage namespace: `.agenttalk/work/`, `.agenttalk/artifacts/` (new dirs; nothing else writes there).
- New tests: `tests/test_work*.py`, `tests/test_evidence*.py`.
- New design doc: `docs/RFC-native-work-spine.md`.

**You must NOT modify** (the primary team is actively changing these — edits will merge-conflict and cross wires):
- `src/agenttalk/wrapper/*` (enforcement engine + turn loop), `src/agenttalk/supervisor.py`, `src/agenttalk/store.py` internals, `src/agenttalk/web.py`.
- Use `store` only through its **public API** (read roster/messages/threads/gates/closes/lanes/domains). If you need a new store primitive, do NOT add it to `store.py` — put a thin helper in your own module, and file a request (via the operator) for the primary team to add it. This keeps `store.py` single-owner.
- The dashboard Work view is **deferred** (it touches `web.py`) — out of scope for now to avoid collision.

If a task seems to require touching a forbidden file, stop and route it through the operator to the primary team. Boundary discipline is what makes parallel work safe.

---

## 3. What this feature is (the contract)

`agenttalk work` binds existing primitives into one durable delivery record. Per roadmap §4:

- **Domain** — who owns this area / which paths are in scope.
- **Lane/workspace** — the isolated local copy and what may change.
- **Work item** — what is being built, by whom, from which base, against which target.
- **Quality evidence** — which checks ran, at which revision, with what result.
- **Review evidence** — who reviewed the real diff and what they found.
- **Gate state** — what is missing, stale, failed, waived, or satisfied.
- **Close state** — is this exact revision ready to merge/release under current policy.

**Status is a projection over typed records, never a place to hide contradictory truth.**

Minimal lifecycle (start narrow — do NOT build a rich PM state machine):
```
draft -> open -> active -> review -> blocked -> ready -> delivered -> closed
                                 \                        \
                                  -> changes_requested     -> abandoned
```

---

## 4. Deliverables — sequenced (each design→review→gate→HOLD)

**D1 — Native Work RFC** (`docs/RFC-native-work-spine.md`). Specify: work-item schema, event model, artifact schema, lifecycle state machine, source-of-truth boundaries with lanes/gates/close (work **links and projects**; it never duplicates their truth), evidence tiers, reset semantics, and safety invariants (§5). **Get it reviewed by your team's reviewers before building** (adversarial, cross-family Claude+Codex). Relay it to the primary architect (via the operator) for one cross-team read.

**D2 — Work item MVP.** `work create|list|show|status|assign|start|deliver|abandon`. One record per item at `.agenttalk/work/items/<work_id>.json` + append-only events at `.agenttalk/work/events/<work_id>.jsonl`. Links to existing lanes, domains, request threads, gates, closes, onboarding runs, artifacts. `status` is **derived** (a projection), not stored authoritatively.

**D3 — Evidence registry MVP.** Write-once artifacts at `.agenttalk/artifacts/<artifact_id>.json` (+ `.log`). Hash validation; bounded logs; redaction status; **exact input binding** (work id, base SHA, head SHA, diff hash, policy hash, command/tool, cwd, exit code, timestamps, producer, trust tier); stale-at-head detection. Artifacts are **immutable** — corrections create new artifacts.

**D4 — Pure `work check`.** Computes `GO` / `HOLD` / `UNKNOWN` with **stable HOLD codes** over work state, lane/worktree state, artifact freshness, review state, and gate/close inputs. Pure function over typed records — no side effects.

**D5 (stretch) — Assurance integration.** Make `assurance.py` artifacts consumable by `work check` **without** turning assurance into authority (read-only ingestion).

Deliver D1→D4 in that order. Each in small committed increments, HELD (no merge to the shared main branch until reviewed — see §7).

---

## 5. Evidence tiers & safety invariants (non-negotiable — from roadmap §4/§7)

Evidence tiers (do NOT collapse all green into one dot):

| Tier | Meaning | Default gate role |
|---|---|---|
| `referenced` | prose/link only | Never satisfies required gates |
| `local_agent` | local command by an agent | Pre-review evidence |
| `local_operator` | operator-run/confirmed | Strong local, still not CI |
| `automation_ci` | configured CI on exact commit/policy | Default release-authoritative |
| `external_attested` | signed third-party | Strongest, later |

**Hard invariants (a design that violates any of these is a HOLD):**
- One corrupt work item must NOT brick all work (per-item records, failsafe reads).
- Artifacts are write-once; corrections create new artifacts (never mutate in place).
- Evidence binds to exact inputs (work id, base/head SHA, diff/policy hash, producer, trust tier).
- Gates consume only evidence matching the **current** work revision and policy.
- `local_agent` evidence never satisfies release-blocking gates by default.
- Timeout / malformed output / parser failure / adapter failure must NEVER normalize to pass (fail closed).
- No projection may hide a contradiction — if records disagree, surface it, don't pick a side.

**Policy boundary:** core validates schemas/IDs/refs/freshness/hashes/transitions/HOLD-codes. It must **not** hardcode any language/framework/scanner. Project policy owns required checks, review lenses, accepted tiers, waiver rules, named tool commands.

---

## 6. Low-collision CLI wiring

`agenttalk work ...` needs one entry in `cli.py` (the forbidden-to-edit file). Handle it with a **single minimal dispatch hook**: add exactly one subparser + one line `from agenttalk import work; return work.main(args)` and put ALL logic in your own module. Coordinate that one-line insertion with the primary architect (via the operator) so it lands cleanly — it is the only `cli.py` touch you should need.

**Tier note (per `docs/ASSURANCE.md`):** the `agenttalk work` CLI surface and the `.agenttalk/work/` + `.agenttalk/artifacts/` JSON schemas are **public contracts** — a public-contract change is **Tier 2 minimum** on its own (`ASSURANCE.md:79-82`). The feature as a whole is **Tier 3** (see §7). Treat the tier as settled here, not re-litigated per deliverable.

---

## 7. Process & quality bar (same standard as the primary team)

- **Tier: this feature is Tier 3** (per `docs/ASSURANCE.md:72-78`) — it is simultaneously a gate/authority surface, a provenance + persistent-state contract, and its core design problem is fail-open/fail-closed semantics. `ASSURANCE.md:63` ("uncertainty rounds UP") points the same way.
- **Reviewer floor = a HARD FLOOR OF 3** independent reviewers (`ASSURANCE.md:93-96`): ≥2 model families, distinct predeclared lenses, **no designer/builder/lead counted**, and ephemeral evidence-only reviews may add counters but do NOT satisfy the minimum. This floor applies to **the D1 RFC design panel** (D1 *is* the Tier-3 design review), not only to the final code diff. (Corrects an earlier "≥2" statement in this doc — GitHub #29.)
- **Design-first:** D1 RFC reviewed (adversarial, cross-family, ≥3 per above) before any build.
- **Small committed increments:** one logical unit per turn, failing-first evidence, commit, post SHA + which acceptance rows are green, STOP. (This is what keeps agent sessions from context-bloating and crashing — do NOT build a whole deliverable in one turn.)
- **Dual-version gate:** ruff + bandit + pytest on **Python 3.10 AND 3.14**, direct interpreter (see §8 gotchas), before calling anything done.
- **Adversarial review before merge:** the Tier-3 floor of 3 cross-family reviewers on the final SHA + your lead gate + CI. HOLD until then.
- **Failing-first is mandatory:** every new acceptance test must fail against unimplemented/broken code, or it's tautological.

---

## 8. Standing up the team on the new laptop

1. **Clone the repo** and check out a dedicated branch: `git checkout -b feature/native-work-spine origin/master`. All work lands here; integrate to `master` via **PR** (not direct push).
2. **Install agenttalk** into a pinned interpreter (recommend Python 3.10) so `<py> -m agenttalk --version` runs. Do NOT rely on a global shim that may shadow the source.
3. **Init a local bus:** `agenttalk init --agents <lead>,<dev>,<reviewer>` in a working root. This bus is **independent** of the primary laptop.
4. **Roster:** a lead (operator-facing), 1–2 Codex/Claude devs, 2 cross-family reviewers (so you can run adversarial review locally).
5. **Wrapper launch — CRITICAL env (hard-won lesson):** when launching a wrapped agent, the process MUST carry the launch env the supervisor bakes in, or the agent runs turns but its bus commands silently break (it looks "idle/stalled" but is actually mis-wired). Set before launch: `AGENTTALK_PY=<the pinned python that runs -m agenttalk>` (verify `& $AGENTTALK_PY -m agenttalk --version`), `AGENTTALK_ROOT=<bus root>`, `PYTHONPATH=<repo>/src`. Launch wrappers with stdout/stderr redirected to a log so a bad launch is visible, not hidden.
6. **Gate discipline gotcha:** if a stale global `agenttalk` install shadows the source, naive `pytest` tests the WRONG code. Always gate via `PYTHONPATH=<worktree>/src` + a direct interpreter (`py -3.10`, `py -3.14`), short `--basetemp`.
7. **Read the foundations:** `docs/ROADMAP.md` §4 (this feature's design north star), `docs/DESIGN.md` (architecture + boundaries), `docs/ASSURANCE.md` (the GOOD/ROBUST/SECURE bar + evidence tiers), `docs/ISSUES.md`. Install the bundled devkit skills (`agenttalk install-skills`) so agents inherit the write/test/review/gate discipline.

---

## 9. First actions for the team

1. Lead: create the branch, stand up the roster, confirm the wrapper-env recipe (§8.5) works end-to-end (an agent posts a bus reply successfully).
2. Dev: draft **D1 — the Native Work RFC** from roadmap §4 + §5's invariants. Small, reviewable.
3. Reviewers: adversarially review the RFC (schema completeness, source-of-truth boundary vs lanes/gates/close, fail-open/silent-contradiction hunt).
4. Lead: relay the reviewed RFC to the primary architect (via the operator) for one cross-team read before D2 starts.
5. Then D2→D4 in small increments, PR when a deliverable is review-clean.

**Integration cadence:** open a draft PR early; keep it rebased on `master`; the primary team will not touch your namespace, so rebases should be clean. Surface any needed `store.py`/`cli.py` change as a request, never a direct edit.

Welcome aboard — build the spine that makes the whole delivery workflow credible.
