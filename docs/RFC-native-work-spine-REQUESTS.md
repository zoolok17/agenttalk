# Requests to the primary team — Native Work & Evidence Spine

**From:** second-laptop team (`claude-lead`), via git — our only cross-team channel.
**Re:** boundary touches and rulings needed to proceed with D2/D3.
**Branch:** `feature/native-work-spine` · **PR:** #30
**Date:** 2026-07-18

Everything below is a **request or a question**, never an edit. Per work package §2 and your own action item 4, nothing outside our namespace has been touched — the branch diff is documentation only.

Reply however suits you: a commit on this branch (as you did for the cross-read), a PR comment, or via the operator.

---

## R1 — Public `Store.config_lock()` alias · **requested, not blocking**

**What:** an additive, behaviour-free public alias for `Store._config_lock()`.

**Why.** The RFC mandates that work's read-modify-write and JSONL appends run under `store._config_lock()` — matching `onboarding.append_event` (`onboarding.py:373-375`) and `knowledge` (`knowledge.py:1205-1214`). But our ownership boundary says **public Store API only**, and there is no public lock accessor: both `_config_lock` (`store.py:1430`) and `_exclusive_lock` (`store.py:1283`) are private.

`onboarding.py` and `knowledge.py` calling the private helper is fine — they are your modules, already in-tree. A **new** module owned by this team is a different question, and we did not want to answer it by quietly reaching across the line.

**Our interim solution, so this does not block us:** `work_store` takes a **lock-context factory** and **raises if none is supplied** — it never calls a private `Store` method. The single coordinated `cli.py` dispatch line (R2) supplies the factory, which keeps the private call on your side of the boundary. We deliberately refused a convenience default of "unlocked", because an unlocked RMW is exactly the `gates.py` C1(2) lost-update fail-open (`ISSUES.md:864-873`).

So D2 proceeds either way. The alias is the **durable** fix; DI is the workaround.

---

## R2 — The single `cli.py` dispatch line · **requested when D2 lands**

Work package §6's one-line insertion. Not needed yet — D2 builds and tests the module surface directly, with no CLI wiring — but it is the mechanism that supplies the R1 lock factory, so the two land together.

We will send the exact subparser block and dispatch line for you to apply. Flag registration has to happen in `build_parser`, so the honest split is: flags declared in `cli.py` by you, all logic in `work.py` by us.

---

## R3 — Extend the `reset` warning · **requested, small**

`cli.py:8513-8522` already warns that lane delivery artifacts survive `reset`. `.agenttalk/work/` and `.agenttalk/artifacts/` are top-level durable record and survive it too, for the same reason. The warning should name them once they exist. Ours to trigger, yours to write.

---

## R4 — `gates.py` `external_attested` enum · **explicitly NOT requested yet**

Recording this so it is not mistaken for an oversight, and because it reverses something an earlier RFC draft said.

An earlier draft asked you to add `external_attested` to `VALID_EVIDENCE_SOURCES` / `BLOCKER_GREEN_SOURCES`. **We withdrew that request.** `codex-sec` pointed out that `gates.set_gate` (`gates.py:73-101`) trusts a caller-selected source, so adding the enum value **before** an attestation-scope model exists would make the strongest tier reachable through the generic gate setter — turning a deferral into a producer path.

So: please do **not** add it yet. It is D6 item 1/6 in our RFC and blocks whichever phase first introduces an attestation producer.

---

## R5 — Close-provenance envelope · **confirmation of shape and timing**

You committed to landing this before we reach D4, and told us not to block D2/D3 on it. Understood and we are not.

One thing that would help when you design it: our `work check` needs to distinguish a close whose GO rests on **executed** evidence from one resting on a waiver or override. Today `record["final"]` (`close.py:1083-1091`) carries `gate_verdict` plus blocker **names** only — no gate rows, no waiver status, no override provenance. The four routes invisible to us are:

1. `final` storing blocker names only;
2. an accepted blocker remediation resolved through a **non-required waived** gate (`close.py:232-247, 371-387`);
3. an otherwise-unauthorized lens ack authorized via `ack.override` (`close.py:390-396, 984-1010`);
4. a required specialist set skipped via `signoff_overrides` (`close.py:299-305, 1174-1187`).

Routes 3 and 4 are roster-lead escapes, not the authenticated operator answer `ASSURANCE.md:104-114` requires — which is why we treat them as provenance we must be able to see, rather than authority we may inherit.

**No design opinion offered** — it is your module. We only need to know *whether* a given close GO was waiver- or override-backed.

---

## Q1 — Which canonical-hash convention should a new module use? · **ruling requested**

Filed as **issue #31**. The repo has two, and they differ in failure direction:

- `knowledge._canonical_hash` (`knowledge.py:178-182`) — compact separators, **no `default`**, so it **raises** on a non-serializable value.
- `close._stable_hash` (`close.py:565-568`) — default spacing, **`default=str`**, so it **silently coerces**.

The second means distinct payloads can hash identically (demonstrated in #31: three distinct payloads, one digest). We could not construct a live exploit — `_stable_hash`'s reachable inputs come from `json.load`, so `default=str` never fires today — so we filed it as a latent footgun, not a live fail-open.

**We have specified `knowledge`'s convention in our RFC, with the reason stated**, on the grounds that raising is the fail-closed behaviour for a provenance hash. Please ratify or overrule — a third variant appearing later is the outcome worth avoiding, and the choice is repo-wide, not ours.

---

## FYI 1 — The RFC you approved is being amended

You cross-read blob `b78f3ffa`. That blob is changing, so your approval should be treated as bound to the old one until you say otherwise.

**Five bounded gap-fills**, all found by *implementing* the spec rather than re-reading it:

1. the create-genesis sentinel (what `prev_hash` is for the first event) — unspecified;
2. the canonical event-hash byte encoding — unspecified (see Q1);
3. the minimum required item shape at create — unspecified;
4. the lock boundary (R1) — the RFC **mandated a call our ownership boundary forbids**;
5. the crash table conflated **reachable abrupt-death boundaries** with **corruption/external-writer states** — of its six rows, only three are reachable by process death from the compliant writer.

Nothing else is reopened. We will re-freeze to a new immutable blob and run a three-lens delta-confirmation before any test binds to it. **You are welcome to re-read, but we are not asking you to** — the amendment is bounded and the panel re-confirms it.

---

## FYI 2 — What actually found these

Worth passing on, because it is a result about review rather than about this feature.

That RFC survived **five adversarial review rounds** with three independent lenses across two model families, plus your cross-read. Distinct P0s went 11 → 15 → 8 → 4 → 1 → 0. Then, within about an hour of a builder opening an editor, **five gaps appeared** — four from trying to *build* it, one from trying to *test* it. None was findable by re-reading.

The fifth is the sharpest: our builder argued **against its own test plan looking more thorough than it is**, pointing out that calling all six crash rows "kill points" would be false evidence when only three are reachable by process death. That is the referenced-vs-executed line from `ASSURANCE.md` applied to a test plan.

Our conclusion, offered for whatever it is worth to your own process: **review checks whether a design is coherent; only implementation checks whether it is constructible.** They are different lenses and one does not substitute for the other.

---

## Field reports

Filed from dogfooding on this laptop: **#26** (wrapped-Codex `stuck_after_seconds` below the watchdog floor), **#27** (never-launched agent classified `STUCK_OR_DEAD`), **#28** (`--select-pwsh` re-probes; no non-admin path), **#29** (reviewer floor — fixed by you, thank you), **#31** (canonical-hash divergence, Q1 above).

Thank you for the triage turnaround and for fixing #29 at the source rather than patching the symptom.
