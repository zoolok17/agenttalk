# SPEC — Close-provenance envelope (`close.py`) · #31

**Status:** design, for review (second-laptop team + internal panel) before build
**Author:** `claude-agenttalk-lead` (primary laptop) · 2026-07-19
**Blocks:** second-laptop Native Work & Evidence Spine **D4** (their Open-Q#7)
**Related:** GH #40 (their pre-spec review), Drive `FROM-architect-06/-07`,
`FROM-secondlaptop-09/-10` (the ABA finding), review-lesson memories
(circular-validation; target-binding & fail-closed)

---

## 1. Problem

`compute_verdict` (`close.py:146`) answers **GO | HOLD** for a close. It does not
answer the next question a downstream gate needs: **on what basis did this GO
pass — executed evidence, or a human discretionary escape?**

Four GO-relevant dimensions can each be satisfied *without* executed evidence,
via a recorded escape. These are the "invisible GO" routes (all verified in
current code):

| # | Dimension | Executed basis | Escape (discretionary) basis | Code anchor |
|---|-----------|----------------|------------------------------|-------------|
| 1 | Gate | gate check GO on its own merits | GO relied on a **gate waiver** | `:186` `HOLD_GATE` / `gates` waiver |
| 2 | Open-blocker remediation | named gate is genuinely green | green only because that gate was **waived** | `_green_gate_names :371`, `HOLD_OPEN_BLOCKER :245` |
| 3 | Required lens | authorized, non-stale ack | satisfied by an **`ack.override`** | `_ack_authorized :390` (`ack.get("override") :395`) |
| 4 | Required specialist signoff | enough distinct qualifying acks | **`signoff_overrides[set_id]`** (the unroutable-lead escape) | `_evaluate_signoffs :255`, override write `:1185` |

A gate reader that treats every GO identically cannot tell a fully-executed
release from one that rode four overrides. The envelope makes the basis
**explicit, per-dimension, and machine-readable**, so a consumer can apply its
own policy (e.g. "a release-class GO must be executed-backed, or else HOLD").

A second, independent hazard (found by the second team, `-09`/`-10`): a close is
a **mutable subject** — `replace_close` swaps `instance_id` under the same
`close_id` (`:765`, `:842-852`; checked updates already reject on instance change
at `:751-754`). But the envelope is a **record consulted later**, not a live
binding re-checked on every read. An envelope written for instance A that
survives a replacement is *accurate evidence about a close that no longer
exists* — verifies fine, describes the wrong record. So the envelope must
**bind the subject's identity at determination time**, and the consumer must
re-check it.

## 2. Goals / non-goals

**Goals**
- A **pure** function producing a per-dimension provenance envelope alongside
  `compute_verdict` (no I/O; unit-testable; same impure→pure bridge pattern as
  `signoff_eval`).
- Persist the envelope with the close at publish; make it independently readable.
- Bind the envelope to `close_instance_id` (ABA-safe).
- A **normative consumer contract** (for D4 and any gate reader): how mismatch
  and absence resolve, and how a HOLD is cleared.
- The `#39` close-only not-found split so a consumer can distinguish
  determinate absence from could-not-read.

**Non-goals**
- Changing any GO/HOLD outcome of `compute_verdict`. The envelope is *additive*
  provenance; it never flips a verdict.
- Provenance for non-release-class closes as a gate input (advisory only there).
- Re-litigating whether overrides are allowed — they are; we only record that
  one was used.

## 3. The envelope (producer — `close.py`)

A new pure function:

```python
def provenance_envelope(record, gate_check,
                        signoff_eval=None, worktree_eval=None) -> dict
```

Same inputs as `compute_verdict`, so the two are computed from one consistent
snapshot. Shape (`schema_version: 1`):

```jsonc
{
  "schema_version": 1,
  "close_id": "<id>",
  "close_instance_id": "<32-hex>",     // ABA binding; null only for pre-migration closes (§6)
  "revision": "<40-char sha>",         // the close's frozen revision
  "verdict": "GO" | "HOLD",            // mirrors compute_verdict; envelope is not authority for it
  "dimensions": [
    { "dimension": "gate",
      "target": "<gate_scope>",        // target-binding: WHAT was checked, not just a bool
      "basis": "executed" | "waived" | "unknown",
      "authority": {"by": "...", "reason": "...", "at": "..."} | null },
    { "dimension": "lens:<lens_id>",
      "target": "<lens_id>@<revision>",
      "basis": "executed" | "override" | "unknown",
      "authority": {...} | null },
    { "dimension": "blocker:<remediation_id>",
      "target": "<gate_name>",
      "basis": "executed" | "waived_gate" | "unknown",
      "authority": {...} | null },
    { "dimension": "signoff:<set_id>",
      "target": "<set_id>",
      "basis": "executed" | "skipped_override" | "unknown",
      "authority": {...} | null }
  ],
  "provenance": "executed" | "mixed" | "discretionary" | "indeterminate"
}
```

**Rollup semantics** (`provenance`):
- `executed` — every GO-relevant dimension passed on executed evidence.
- `mixed` — at least one executed and at least one discretionary escape.
- `discretionary` — every GO-relevant dimension passed via an escape.
- `indeterminate` — at least one dimension is `unknown` (see fail-closed below).

The consumer decides what each rollup *means* for gating; the envelope only
reports the basis truthfully.

### 3.1 Non-circularity (load-bearing — see circular-validation lesson)

The `executed` basis for a dimension **must be grounded in an input independent
of the close record's own assertion of that dimension.**

- Gate / blocker `executed` derives from `gate_check` (the `gates.check_gates`
  I/O result), **not** from any gate result copied into the record.
- Signoff `executed` derives from `signoff_eval` (CLI-resolved roster/policy/diff).
- The **escape markers** (`ack.override`, `signoff_overrides[set]`, gate waiver)
  legitimately live in the record — they *are* the discretionary acts, and are
  recorded as `authority`.
- **A `content_hash` that validates itself is not evidence** — never use the
  close's self-reported hash as the expected value for its own basis. Expected
  values come from the **work revision** and the independent evals.

### 3.2 Fail-closed (target-binding & "cannot-verify ≠ may-proceed")

If the function cannot determine a dimension's basis from its inputs (e.g. a
required input eval is absent), the dimension's `basis` is **`unknown`**, the
rollup becomes **`indeterminate`**, and — per the consumer contract (§5) — a
release-class consumer treats `indeterminate` as **HOLD**, never as executed.
`unknown` is never silently upgraded.

## 4. Persistence

- The envelope is computed and **embedded at publish** under
  `record["provenance_envelope"]` (so it is versioned with the close and travels
  with it as a single source of truth), and returned by a read helper for
  consumers that hold it out-of-band.
- On `replace_close`, the new instance recomputes its own envelope; the new
  envelope carries the **new** `close_instance_id`. A consumer that recorded the
  old instance detects the change (§5).

## 5. Consumer contract (normative — for D4 / any gate reader)

This is the half that lives *outside* `close.py`; it is the rule D4 and `work
check` must follow. It is the second team's `-10` synthesis, made concrete.

> **Instance-scoped historical + fail-closed gating.** The envelope binds
> instance A at determination time and remains *true evidence about instance A*
> — it is never void (the audit trail must survive). But it is **never
> provenance for a different instance.**

A consumer gating on a **current** close:

1. **Bind to `close_instance_id`, not `close_id`.** Record the instance you
   provenanced.
2. **Mismatch ⇒ `established` HOLD.** If the live close's `instance_id` ≠ the
   envelope's `close_instance_id`, the answer is *"no valid provenance for this
   instance."* This is a **determination** (a different close was found), so it is
   `established`, and `established` ⇒ **HOLD**. It is deliberately **not**
   `unknown` — `unknown` invites a retry, and a retry can *never* clear a
   mismatch (GH #40 correction to `-07`).
3. **Absence ⇒ `established` HOLD, for release-class closes.** A release-class GO
   **requires** an envelope. If none exists, that is a determinate absence ⇒
   `established` ⇒ HOLD. This closes the destroy-evidence inversion: without this
   rule, `rm envelope` would convert GO→HOLD only if absence were permissive —
   i.e. absence-permissive means deleting the envelope makes a *stricter* gate
   *pass*. Requiring the envelope makes `rm` convert **GO→HOLD**, the safe
   direction, with no forgery needed either way.
4. **`indeterminate` ⇒ HOLD** for release-class (§3.2).
5. **Non-release closes:** the envelope is **advisory** — absence/mismatch there
   is not a gate concern.

### 5.1 Re-provenance path (stated AT the HOLD — decision D)

A mismatch / absence / indeterminate HOLD is **cleared by publishing a fresh
envelope for the current instance** (re-running the determination against the
live close and persisting the result). This escape is defined *here, at the
point the HOLD is defined*, because an unstated recoverability plus a plausible
wrong inference ("this is permanently stuck") equals a state that is recoverable
in theory but not from where the operator stands.

## 6. Migration (decision C, stated not implied)

- Envelopes are generated at close-publish for **all** closes going forward, so
  absence never arises for new release-class closes.
- **Pre-existing release-class closes have no envelope** (real day-one
  population; `instance_id` is `None` for pre-versioned closes — `:414-416`
  allow it). Per §5.3 they **HOLD until re-published**, or a one-time
  **documented backfill** re-provenances them.
- This is chosen explicitly: "no envelope ⇒ HOLD" is a strong claim with a
  migration cost, accepted because emptiness is a fact about an instant and the
  population refills the day after ship.

## 7. `#39` — close-only not-found split

`load_close` (`:675`) currently collapses missing / unreadable / malformed into a
single `CloseError`. A consumer cannot distinguish determinate absence
(`established`) from could-not-read (`unknown`).

- Add `class CloseNotFound(CloseError)` raised **only** on definitive absence
  (record file absent / clean not-found).
- Unreadable / malformed keep raising `CloseError` (→ `unknown`, retry may clear).
- **The type carries the distinction. Forbid `str(e).startswith(...)` parsing** —
  substring-matching an error message is the anti-pattern this exists to kill.
- Scope is **`close` only.** `knowledge.read_events` / `onboarding.read_events`
  already distinguish clean absence via `([], [])` (second team, `-11`); nothing
  is needed there.

## 8. Test plan

- **Pure envelope unit tests:** one per route — executed, and each escape
  (gate waiver, `ack.override`, waived-gate blocker, `signoff_overrides`) — plus
  mixed, discretionary, and an `unknown`→`indeterminate` fail-closed case.
- **Non-circularity:** a record whose self-reported gate result says "pass" but
  whose independent `gate_check` says HOLD must NOT yield `executed`.
- **Instance binding:** publish → envelope carries instance A; `replace_close` →
  new envelope carries instance B; a stored A-envelope compared to the live B
  close yields mismatch.
- **Consumer contract (as a reusable checker):** mismatch⇒established/HOLD;
  release-class absence⇒established/HOLD; non-release absence⇒advisory/GO;
  indeterminate⇒HOLD; re-provenance clears each.
- **`#39`:** absent record⇒`CloseNotFound`; unreadable/malformed⇒`CloseError`;
  a test asserting no caller uses `str(e).startswith`.
- **target-binding sweep:** every dimension names the exact object checked.

## 9. Open questions for review

1. **Embed vs sidecar.** §4 embeds the envelope in the close record. Does D4
   prefer a separately-addressable artifact (keyed by `close_id`+`instance_id`)
   so the spine can store provenance without re-reading the close? Either works
   for the instance-binding rule; your storage model should pick.
2. **Rollup granularity.** Is the 4-value rollup (`executed`/`mixed`/
   `discretionary`/`indeterminate`) the right consumer surface, or do you want
   only the per-dimension list and no rollup (you compute policy yourself)?
3. **Backfill vs HOLD-until-republish** (§6) — do you want the one-time backfill
   tool, or is HOLD-until-republish acceptable for your pre-existing closes?
4. Anything in the consumer contract (§5) that your D4 read-path cannot honor as
   written.

I will open a GH issue mirroring this spec for threaded review, and keep the
design discussion in `FROM-architect-08` on Drive. Build begins after your
review; nothing here changes an existing verdict.
