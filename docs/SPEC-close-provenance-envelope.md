# SPEC — Close-provenance envelope (`close.py`) · #31

**Status:** design **r2** (folds second-laptop Tier-3 panel round 1: 3/3 release-blocking REVISE, blockers B1–B7 + smaller items + Q1–Q4). For panel round 2 before build.
**Author:** `claude-agenttalk-lead` (primary laptop) · r1 2026-07-19 · r2 2026-07-20
**Blocks:** second-laptop Native Work & Evidence Spine **D4** (their Open-Q#7)
**Related:** GH #45 (threaded review), Drive `FROM-architect-06/-07/-08`,
`FROM-secondlaptop-09/-10` (the ABA finding); review-lesson memories
(circular-validation; target-binding & fail-closed; premise-and-form).
**Depends on / sequences with:** §7 (`CloseNotFound`) **ships first** (B-small/§7);
independent-ack grounding sequences with **#48** (B7); consumer predicate consumes
D4's total `release_blocking` classifier (RFC :3013-3029) (B3).

---

## 0. What changed in r2 (disposition of the panel verdict)

The panel found the direction sound but the r1 spec did not close the routes it
exists to close. Every blocker is folded below; none deferred silently.

| Blocker | Where folded |
|---|---|
| **B1** dimension enumeration incomplete (worktree; counted ack-override; per-gate-row) | §3.2 dimension vocabulary is now **derived branch-for-branch** from `compute_verdict` with a **completeness assertion** (§3.3) |
| **B2** `instance_id` alone insufficient (reopen preserves it; same-revision republish) | §3.4 **publication identity**; §5 consumer compares the **full tuple** |
| **B3** wrong release predicate (`close.scope` vs D4 total `release_blocking`) | §5.1 predicate is **D4's total determination**; release-class read from the **record**; new `close.is_release_class(record)` accessor (§3.5) |
| **B4** mixed/discretionary/absence/mismatch/invalid unmapped | §5.2 **normative rule**: advance iff every required dimension `executed`; all else HOLD |
| **B5** historical laundering via live re-provenance + backfill | §5.6 **re-provenance removed**; republish = new publication from one locked snapshot; §6 backfill cannot invent `executed` |
| **B6** persistence contradicts audit-survival; no consumer validator | §4 **immutable close-owned sidecar** + §4.2 **public typed validator** with recomputed rollup |
| **B7** non-circularity is prose, not structure | §3.1 **separate named helper**; evals **required**; lens `executed` narrowed to *trusted-record provenance* pending #48 |
| authority not producer-complete | §3.6 typed authority; **audit-only, never influences `executed`** |
| rollup undefined for HOLD / empty set | §3.7 rollup defined over HOLD + empty; **derived/advisory, recomputed** (Q2) |
| null `instance_id` is not a binding | §5 null instance **never matches**, treated as absence |
| §7 ordering; sidecar needs its own absence split | §7 **ships first**; sidecar clean-absence vs unreadable split |
| numbering / §5.3 dangle / backwards destroy-evidence text | §5 renumbered; corrected |

**Panel Q-answers adopted verbatim:** Q1 immutable **sidecar** (§4); Q2 per-dimension
list **authoritative**, rollup derived/advisory (§3.7); Q3 migration =
**HOLD-until-republish**, no invented `executed` (§6); Q4 the corrected consumer
form (§5).

---

## 1. Problem

`compute_verdict` (`close.py:146`) answers **GO | HOLD** for a close. It does not
answer the next question a downstream gate needs: **on what basis did this GO
pass — executed evidence, or a discretionary escape?**

Multiple GO-relevant branches can each be satisfied *without* executed evidence,
via a recorded escape. Verified in current code (master `f81a7fc`):

| Branch | Executed basis | Escape basis | Code anchor |
|---|---|---|---|
| Gate (per gate row) | gate row GO on merits | gate **waiver** | `:186-188`, `_green_gate_names` |
| Worktree isolation (release-class) | `worktree_eval.status=="verified"` | `status=="waived"` **treated identically** (`:198`), or `non_lane_isolation_not_asserted` bare-bool `pass` (`:191-192`) | `:190-204` |
| Required lens | authorized, non-stale ack | `ack.override` | `_ack_authorized :390`, `ack.get("override")` |
| Open-blocker remediation | named gate genuinely green | green only via a **waived** gate | `:245-247` |
| Specialist signoff | enough distinct qualifying acks | `signoff_overrides[set]`; **and** a counted `ack.override` route under policy `override_counts:true` | `_evaluate_signoffs :255`, `:317/:346-348` |

A gate reader that treats every GO identically cannot tell a fully-executed
release from one that rode escapes. The envelope makes the basis **explicit,
per-dimension, machine-readable**, so a consumer applies its own policy (e.g.
"a release-blocking GO must be executed-backed, else HOLD").

**Two independent hazards drive the r2 structure:**

- **ABA / subject mutation** (second team `-09`/`-10`; **B2**). A close is a
  *mutable subject*: `replace_close` swaps `instance_id` under the same
  `close_id` (`:881`), and `reopen` (`:1096`) **preserves** `instance_id` while
  permitting a revision change *and* a same-revision republish after changed
  acks/overrides. So binding to `close_id` — **or even `instance_id` alone** — is
  not enough; a stored envelope must bind an **immutable publication identity**.
- **Historical laundering** (**B5**). `compute_verdict` on a PUBLISHED record
  returns `HOLD_PUBLISH_NOT_ALLOWED` (`:175-179`), and the envelope's `verdict`
  mirrors `compute_verdict`. So re-deriving provenance against the *live* close
  is epistemically invalid: gate state is mutable, and a publication that used a
  waiver could be relabelled "executed" after the gate later turns green. The
  envelope must be a **frozen determination from one publish-time snapshot**,
  never recomputed from current state.

## 2. Goals / non-goals

**Goals**
- A **pure** producer of a per-dimension provenance envelope, computed from the
  same snapshot as `compute_verdict`; the per-dimension basis is decided by a
  **separate helper that cannot see the record's self-asserted results** (§3.1).
- Persist it in an **immutable close-owned sidecar** keyed by the full
  publication identity; bind a pointer+digest in the record (§4).
- A **public typed validator/reader** that recomputes the rollup and enforces the
  dimension set, target bindings, basis↔authority pairing, and publication tuple
  (§4.2).
- A **normative consumer contract** keyed on **D4's total `release_blocking`
  predicate** (§5).
- The `#39`/§7 `CloseNotFound` split — **shipped first**.

**Non-goals**
- Changing any GO/HOLD outcome of `compute_verdict`. The envelope is *additive*.
- Re-litigating whether overrides are allowed — they are; we record that one was
  used, as **audit-only authority** that never grounds `executed`.
- Independent *cryptographic* grounding of lens acks — deferred to #48; until
  then lens `executed` is explicitly *trusted-record provenance* (§3.1).

## 3. The envelope (producer — `close.py`)

### 3.1 Non-circularity is STRUCTURAL, not prose (B7 — the row-35 form)

The r1 spec asserted non-circularity in prose while `provenance_envelope(record,
...)` took `record` as param 1 with `signoff_eval`/`worktree_eval` defaulting to
`None` — the exact pattern amendment #15 row 35 rejected ("weaker than (i);
enforced by review"). r2 makes circular derivation **inexpressible**:

```python
def provenance_envelope(record, *, gate_check, signoff_eval,
                        worktree_eval, publication_id) -> dict
```

- **All evals are REQUIRED** keyword args (no `None` default). A caller cannot
  silently omit the independent evidence.
- The per-dimension basis is decided by a **separate named helper** whose entire
  input surface is *(independent eval result, normalized escape marker)*:

  ```python
  def _basis_for_dimension(*, independent_result, escape_marker) -> str
  ```

  `_basis_for_dimension` **never receives `record`**. The record's self-asserted
  result fields (a gate result copied into the record, a self-reported
  `content_hash`) *cannot reach the basis decision* — so "`executed` derived from
  the thing it certifies" is a type-level impossibility, not a review catch.
- `provenance_envelope` extracts only the **normalized escape markers** from the
  record (`ack.override`, `signoff_overrides[set]`, gate-waiver presence,
  `worktree_eval.status`, `non_lane_isolation_not_asserted`) and passes them,
  plus the **independent** eval results, to `_basis_for_dimension`. Escape
  markers are *inputs to the escape branch only*; they never upgrade a basis.
- **Lens `executed` is narrowed.** The only ack input available is
  `record["lens_acks"]` (self-stored `from`/`groups`/`revision`/`override`);
  `signoff_eval` resolves policy/roster but not the ack's *occurrence or sender*.
  Until #48 supplies an independent origin-minted ack reference, a lens dimension
  whose basis would be `executed` is labelled **`executed_trusted_record`** and
  the schema documents it as *trusted-record provenance, not independently
  grounded*. A consumer that requires cryptographic grounding treats it per
  policy; D4's release rule (§5.2) accepts it, with the dependency named. This is
  the honest fail-closed choice: **surface the limit, don't fake the grounding.**

### 3.2 Dimension vocabulary — derived branch-for-branch (B1)

The dimension set is **not** a hand-authored table (r1's four rows missed the
fifth, worktree, and the counted ack-override route). It is derived **one
dimension per GO-relevant branch** in `compute_verdict`:

| Dimension key | Branch (anchor) | `basis` domain |
|---|---|---|
| `gate:<gate_name>` — **one row per gate**, not per scope | `:186-188` (+ per-row waiver) | `verified` \| `waived` \| `unknown` |
| `worktree` (release-class only) | `:190-204` | `verified` \| `waived` \| `not_asserted` \| `unknown` |
| `lens:<lens_id>` | `:209-230` | `executed_trusted_record` \| `override` \| `unknown` |
| `blocker:<remediation_id>` | `:245-247` | `executed` \| `waived_gate` \| `unknown` |
| `signoff:<set_id>` | `:250-251`, `:317/:346-348` | `executed` \| `counted_ack_override` \| `skipped_override` \| `unknown` |

Two B1 specifics folded:
- **Per gate ROW, not per scope.** A single gate scope can hold a green gate
  *and* a waived gate at once; one scope-level dimension cannot represent both.
  Emit one `gate:<name>` dimension per gate the check evaluated.
- **`worktree` is a first-class dimension.** `worktree_eval` is already an input
  to `compute_verdict` but had no output dimension in r1 (an input with no
  output). `waived` and `not_asserted` are **distinct escape bases** — they must
  not read as `verified`. `not_asserted` (the bare-bool self-assertion at
  `:191-192`) has **no authority producer** (no by/reason/at); per the #48
  pattern, either upstream records an authority or the schema discloses
  `authority: null` for it as a **named dependency**, not a field nothing fills.
- **`signoff` counted-override route.** `ack.override` counted under policy
  `override_counts:true` (`:346-348`) is a GO route distinct from
  `signoff_overrides` (the unroutable-lead escape); both get typed escape bases.

Each dimension carries a **target binding** (the exact object checked, per the
target-binding lesson): `gate:<name>` → the gate name; `worktree` →
`delivered_head@revision`; `lens:<id>` → `<lens_id>@<revision>`; `blocker:<id>`
→ the named gate; `signoff:<set_id>` → the set id.

### 3.3 Completeness assertion (B1 — correct-by-construction)

Enumeration cannot prove itself complete (the premise-and-form lesson: a
growing free-dimension count is unclosable by prose). So the producer carries a
**structural completeness invariant**, verified by one representative test:

- `compute_verdict` and `provenance_envelope` share a single internal
  enumeration of GO-relevant branches (a module-level tuple of branch ids). Each
  branch id maps to exactly one dimension-emitter.
- `provenance_envelope` asserts, before returning, that **every branch id that
  `compute_verdict` could satisfy for this record emitted exactly one bound
  dimension** — no branch unmapped, no dimension without a branch, no duplicate.
- The representative test walks a record that exercises **every** branch and
  asserts the emitted dimension set equals the branch set. The **structure is the
  completeness argument**; there is no lattice of cases left to enumerate.

### 3.4 Publication identity (B2)

At **every** publish (initial and any republish after reopen), mint an immutable
`publication_id` (a fresh random id, or a monotonic final-publication
generation). The envelope binds the **full tuple**:

```jsonc
{
  "schema_version": 2,
  "close_id": "<id>",
  "close_instance_id": "<32-hex>",   // null ONLY for pre-migration closes (§6); null never matches (§5)
  "revision": "<40-char sha>",
  "publication_id": "<immutable, minted at THIS publish>",
  "verdict": "GO" | "HOLD",          // mirrors compute_verdict at publish-snapshot; envelope is not authority for it
  "dimensions": [ /* §3.2, each {dimension, target, basis, authority} */ ],
  "rollup": "executed" | "mixed" | "discretionary" | "indeterminate" | "hold"  // §3.7 DERIVED/advisory
}
```

A reopen→republish that preserves `instance_id` still mints a **new
`publication_id`**, so a stored envelope for the prior publication no longer
matches (closes B2's same-revision-republish and reopen-preserves-instance
cases).

### 3.5 Release-class accessor (B3)

Add `close.is_release_class(record) -> bool` (reads `record["scope"]` against
`RELEASE_CLASS_SCOPES`, the module constant at `:87`). D4 and every consumer call
it instead of copying the scope set — so adding a scope later cannot drift the
consumer. **Note:** this accessor answers only the *close-scope* axis; the
consumer's release predicate is D4's **total** determination (§5.1), which also
folds `policy_eval`.

### 3.6 Authority is typed and audit-only (smaller item)

Escape dimensions carry a typed `authority`; executed dimensions carry
`authority: null`. Structural rule: **authority never influences a basis** — it
is attached *after* the basis is decided by `_basis_for_dimension` (§3.1), and
only to escape bases. Shape:

```jsonc
"authority": {
  "kind": "gate_waiver" | "ack_override" | "signoff_override" | "worktree_waiver" | "worktree_not_asserted",
  "target": "<the exact object waived>",
  "recorder": "<updated_by — the authenticated writer>",
  "claimed_operator": "<waiver.operator — CALLER-ASSERTED, NOT authenticated>",
  "reason": "<str>" | null,          // nullability explicit; ack --override reason may be None
  "decided_at": "<iso>" | null,
  "expiry": "<iso>" | null,
  "source_binding": { "revision": "...", "instance_id": "...", "publication_id": "..." }
} | null
```

Per `ASSURANCE.md`, `claimed_operator` is recorded **as claimed** and never
treated as authenticated authority; `recorder` is the authenticated writer. Both
are kept (a gate waiver carries both, and a single `{by,at}` would lose one).
`worktree_not_asserted` has no producer authority today → `authority: null` with
the named dependency (§3.2).

### 3.7 Rollup is derived, advisory, and total (Q2 + smaller)

The **per-dimension list is authoritative and normative.** The `rollup` is a
**derived convenience, recomputed on every read** by the validator (§4.2);
D4's gating primitive is **`all(d.basis is executed-family)`**, never a trusted
stored rollup. Defined over all cases so no enum member is dead:
- `executed` — every dimension basis is executed-family
  (`executed` / `executed_trusted_record`).
- `mixed` — at least one executed-family and at least one escape.
- `discretionary` — every dimension passed via an escape.
- `indeterminate` — at least one `unknown` (fail-closed, §3.8).
- `hold` — `verdict == HOLD`: the close did not GO, so there is no GO-provenance
  to roll up; the envelope exists for audit only and **never advances a
  consumer**.
- **Empty dimension set** (no GO-relevant branch applied): stated rule — an empty
  set is **`indeterminate`**, never vacuously `executed`.

A stored `rollup` that disagrees with the recomputed value is **invalid ⇒ HOLD**
(§4.2).

### 3.8 Fail-closed

If `_basis_for_dimension` cannot determine a basis from its independent inputs,
the basis is **`unknown`**, the recomputed rollup is **`indeterminate`**, and a
release-blocking consumer treats it as **HOLD** (§5.2). `unknown` is never
silently upgraded.

## 4. Persistence — immutable close-owned sidecar (B6, Q1)

r1 embedded one mutable `record["provenance_envelope"]`. That contradicts the
audit-survival promise: `replace_close` overwrites the record (`:881-907`) and
reopen/republish overwrite the slot — a single mutable slot cannot be both
"single source" and "historical survival." r2:

- The envelope is written to an **immutable, append-only, close-owned sidecar**,
  keyed by **`(close_id, instance_id, revision, publication_id)`**. Entries are
  **write-once**; a republish appends a new entry under the new
  `publication_id`, never overwriting.
- The record carries a **bound pointer + digest** to the current publication's
  sidecar entry: `record["provenance_ref"] = {publication_id, sha256}`.
- **Write order:** write the sidecar entry **first**, then atomically commit the
  record pointer. An orphaned sidecar entry (no pointer) is harmless; a published
  record whose pointer resolves to **no sidecar entry fails closed** (§5.2
  absence). Survives `replace_close`/`reopen` because the sidecar is never
  rewritten.
- **Sidecar absence split (B-small).** The sidecar read helper distinguishes
  **clean-absent** (entry never written → *established* absence) from
  **unreadable/malformed** (→ `unknown`, retry may clear) — the same typed split
  §7 gives `load_close`. `CloseNotFound` alone is insufficient for the sidecar.

### 4.2 Public typed validator / reader (B6)

A **public close-owned** helper — the consumer must not re-derive validation:

```python
def load_provenance_envelope(store, close_id, *, publication_id=None) -> Envelope   # typed absence split
def validate_provenance_envelope(env, *, live_record) -> ValidationResult
```

`validate_provenance_envelope` enforces, and **any failure ⇒ invalid ⇒ HOLD**:
1. `schema_version` is understood.
2. The dimension set is **exact, unique, exhaustive** vs the expected set for
   this record's satisfied branches (§3.3) — any missing / extra / duplicate
   dimension is invalid. (So "delete a dimension but keep `rollup:executed`" is
   caught.)
3. Every dimension has a valid **target binding**.
4. **basis ↔ authority pairing**: executed-family ⇒ `authority is null`; every
   escape basis ⇒ a typed `authority` (§3.6).
5. The **publication tuple** `(close_id, instance_id, revision, publication_id)`
   matches `live_record` (§5), and the pointer digest matches the sidecar entry.
6. The **rollup is recomputed** from the dimension list and must equal any stored
   value (§3.7).

The per-dimension list is authoritative; the validator trusts no stored rollup.

## 5. Consumer contract (normative — for D4 / any gate reader)

The half that lives outside `close.py`. This is `-10`'s synthesis made concrete
and corrected per the panel.

> The envelope binds **one publication** at determination time and is **true
> evidence about that publication forever** (audit survives). It is **never
> provenance for a different publication.**

### 5.1 The release predicate is D4's TOTAL determination (B3)

A consumer decides "is this item release-blocking?" using **D4's total
`release_blocking`** — `close_eval.scope` **combined with** `policy_eval` under
D4's fail-closed truth table (RFC :3013-3029) — **never `close.scope` alone.**
Countermodel this closes: `scope=advisory` + matched policy `release_scoped:true`
⇒ D4 says release-blocking, but r1's `close.scope` test said advisory ⇒ the
envelope was bypassed exactly where policy made it mandatory.

Two source-of-truth riders:
- Release-class / scope is read from the **record** via `is_release_class`
  (§3.5), **never from the envelope** — §5.4 branches on it in the case the
  envelope is *absent*, so envelope-sourcing it would make the absence rule
  undeterminable.
- `policy_eval` absence/unknown **fails closed** (treated release-blocking).

### 5.2 The normative rule (B4)

For a **release-blocking** item (per §5.1), a consumer may advance **iff**:

- an envelope for the **current publication tuple** exists and is **valid**
  (§4.2), **and**
- **every required dimension basis is executed-family**
  (`executed` / `executed_trusted_record`).

**Everything else HOLDs**, explicitly: `mixed`, `discretionary`,
`indeterminate`, **absence** (§5.4), **mismatch** (§5.3), **invalid schema**
(§4.2). There is no "advance on discretionary" for a release-blocking item.
Non-release items: the envelope is **advisory** (§5.5).

### 5.3 Mismatch ⇒ `established` HOLD

If the live record's publication tuple ≠ the envelope's tuple, the answer is *"no
valid provenance for this publication."* That is a **determination** (a different
publication was found) ⇒ `established` ⇒ **HOLD**. Deliberately **not**
`unknown`: a retry can never clear a mismatch (GH #40 correction). **Null
`instance_id` never matches** (`None == None` is not a binding) — a null-instance
envelope is treated as **absence** (§5.4), not a match.

### 5.4 Absence ⇒ `established` HOLD (release-blocking)

A release-blocking GO **requires** a valid envelope for the current publication.
Determinate absence (sidecar clean-absent per §4 / `CloseNotFound` per §7) ⇒
`established` ⇒ HOLD. Could-not-read ⇒ `unknown` ⇒ retry may clear.

This closes the destroy-evidence inversion, **stated forward** (r1 had it
backwards): if absence were *permissive*, then `rm envelope` would make a
*stricter* gate *pass* — deletion would flip HOLD→GO. Requiring the envelope
makes `rm` flip **GO→HOLD** — the safe direction — with no forgery available in
either direction.

### 5.5 Non-release items

The envelope is **advisory**; absence/mismatch is not a gate concern.

### 5.6 Republish, not re-provenance (B5 — laundering removed)

r1's §5.1 "clear the HOLD by re-running the determination against the live
close" is **removed**: it is epistemically invalid (`compute_verdict` on a
published record is HOLD, `:175-179`; gate state is mutable, so a waiver-backed
publication could be relabelled `executed` after the gate later goes green).

The **only** way to clear a mismatch/absence/indeterminate HOLD is to
**republish**: a *new* publication determination, minting a **new
`publication_id`** (§3.4), computed from **one locked publish-time snapshot** and
appended to the sidecar (§4). A stored historical publication is **never cleared
from current state**. "A fresh envelope exists" clears nothing; only a
structurally valid, bound, all-executed envelope **for the new publication**
advances the consumer.

## 6. Migration — HOLD-until-republish (Q3, B5)

- Envelopes are generated at publish for **all** closes going forward; absence
  never arises for new publications.
- **Pre-existing release-class closes have no envelope** (`instance_id` may be
  `None`, `:414-416`). They **HOLD until republished** (§5.6).
- A one-time tool **may inventory** such closes or emit **forensic
  `unknown`/`indeterminate`** envelopes for visibility. It **may NOT invent
  `executed`** provenance from current state, and **cannot clear a HOLD**. It
  could certify `executed` *only* from independently retained publish-time eval
  snapshots — **which current code does not keep** — so in practice migration is
  HOLD-until-republish. Chosen explicitly: emptiness is a fact about an instant;
  the population refills the day after ship.

## 7. `#39` — close-only not-found split (SHIPS FIRST)

§5.4's determinate-absence rule **requires** this type to exist, so §7 lands
**before** the consumer contract can be built (B-small ordering; a D4 built
against today's `load_close` collapses established absence into `unknown` and
**inverts** the fail-closed direction).

- Add `class CloseNotFound(CloseError)` raised **only** on definitive absence
  (record file absent / clean not-found).
- Unreadable / malformed keep raising `CloseError` (→ `unknown`, retry may clear).
- **The type carries the distinction. Forbid `str(e).startswith(...)` parsing.**
- Scope is **`close` only** (`knowledge`/`onboarding.read_events` already return
  `([], [])` for clean absence — second team `-11`). The **sidecar** gets its own
  clean-absent vs unreadable split (§4), which `CloseNotFound` does not cover.

## 8. Test plan

- **Non-circularity (structural):** `_basis_for_dimension` has no `record`
  parameter (a signature test); a record whose self-reported gate result says
  "pass" while independent `gate_check` says HOLD yields `unknown`/escape, never
  `executed`. `provenance_envelope` rejects a missing eval (required kwargs).
- **Completeness (§3.3):** a record exercising every GO branch → emitted
  dimension set equals the branch set (no unmapped branch, no orphan dimension,
  no duplicate). A synthetic new branch without an emitter trips the invariant.
- **Per-route bases:** one test per basis incl. the r1-missed ones —
  `worktree:waived`, `worktree:not_asserted`, `signoff:counted_ack_override`,
  per-gate-row mixed (one gate green + one waived in the same scope).
- **Publication identity (B2):** publish → tuple T1; `reopen`+republish
  (same revision) → new `publication_id`, tuple T2; a stored T1 envelope vs live
  T2 ⇒ mismatch/HOLD. `replace_close` → new instance ⇒ mismatch/HOLD.
- **Validator (§4.2):** delete a dimension but keep `rollup:executed` ⇒ invalid;
  stored rollup ≠ recomputed ⇒ invalid; executed basis carrying an authority ⇒
  invalid; escape basis with null authority ⇒ invalid; pointer digest ≠ sidecar
  ⇒ invalid.
- **Consumer contract:** predicate uses D4 total `release_blocking` (advisory
  scope + release_scoped policy ⇒ HOLD path exercised); mismatch⇒established/HOLD;
  release absence⇒established/HOLD; non-release absence⇒advisory; indeterminate,
  mixed, discretionary ⇒ HOLD; **null instance never matches**; republish (not
  re-provenance) clears.
- **Persistence:** sidecar write-once (republish appends, never overwrites);
  survives `replace_close`/`reopen`; sidecar clean-absent⇒established vs
  unreadable⇒unknown.
- **§7:** absent⇒`CloseNotFound`; unreadable/malformed⇒`CloseError`; a test
  asserting no caller uses `str(e).startswith`.

## 9. Open questions for panel round 2

1. **`publication_id` shape (§3.4):** immutable random id vs a monotonic
   final-publication generation counter. Either satisfies "new id per publish";
   does D4's projection prefer a comparable/orderable generation?
2. **Lens `executed_trusted_record` (§3.1/B7):** is narrowing lens `executed` to
   trusted-record provenance (with the independent-ack reference sequenced onto
   #48) acceptable for D4's release rule now, or does D4 need to treat
   `executed_trusted_record` as non-advancing until #48 lands?
3. **Sidecar storage model (§4):** does D4 want the sidecar close-owned (this
   spec) or does the spine prefer to own provenance storage keyed by the
   publication tuple, with `close.py` only producing+validating? The immutability
   and full-tuple key are invariant either way.
4. Anything in §5 (esp. the total-`release_blocking` predicate and the
   republish-only clearance) that D4's read-path still cannot honor.

Build begins after round 2 converges. §7 lands first; nothing here changes an
existing `compute_verdict` outcome.
