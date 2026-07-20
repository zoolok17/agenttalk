# SPEC — Close-provenance envelope (`close.py`) · #31

**Status:** design **r3** (folds second-laptop Tier-3 panel round 2: 3/3 release-blocking REVISE — B1/B3/B4/B6/B7 not-closed + a fold-introduced P0 + the BranchEvaluation-list unifying fix + OQ2 ruling; plus the three cross-artifact enum-sweep refinements Findings B/E/policy-surface). For panel round 3 before build.
**Author:** `claude-agenttalk-lead` (primary laptop) · r1 2026-07-19 · r2 2026-07-20 · r3 2026-07-21
**Blocks:** second-laptop Native Work & Evidence Spine **D4** (their Open-Q#7)
**Related:** GH #45, Drive `FROM-secondlaptop-13/-14/-15`, chat r2 verdict; review-lesson memories (circular-validation; target-binding & fail-closed; premise-and-form; check-not-read).
**Sequenced dependencies (named, not silent):** §7 `CloseNotFound` **ships first**; the independent **close-ack provenance producer** for `executed_trusted_record` (a NEW dependency, **not** #48 — see §3.8); the spine's `policy_eval` release-policy surface (§5.2); the reciprocal close-resolver obligation (§7).

---

## 0. What changed in r3 (disposition of the round-2 verdict)

r2 rebuilt persistence/replay/laundering (B2/B5 closed) but bolted provenance onto `compute_verdict`'s freehand `if`/loop control flow beside a static dimension inventory — so B1/B4/B6/B7 stayed open and r2 introduced a **fail-closed P0** (the `"verified"` basis was in neither the advancing nor the escape set → D4 would HOLD every release forever). r3 adopts the panel's convergent fix.

| Item | r3 resolution |
|---|---|
| **Unifying fix** (closes B1/B4/B6/B7) | §3.1 a single typed **`BranchEvaluation`** list is the PRODUCER OF TRUTH: the branch evaluators append it, `compute_verdict` **reduces** it to GO/HOLD, `provenance_envelope` **maps** the same objects, the validator checks against the **same list persisted at publish**. No control-flow-beside-inventory. |
| **P0 (`"verified"` inert)** | §3.4 ONE closed `BasisClass` defined in ONE place; the merit-backed positive is `executed` everywhere (drop `"verified"`); ONE shared `rollup()` with total precedence (§3.5). |
| **B1** dimension completeness | §3.2 dimension vocabulary IS the BranchEvaluation list (dynamic per-row/per-lens), selection criterion = "a branch earns a dimension iff it has >1 satisfying basis"; the **applicable set** is shared, not just labels; only release-relevant blocking/required gate rows. |
| **B3** release predicate | §5.2 tri-state `close.classify_release(record) -> release\|non_release\|unknown` (unknown/unparseable ⇒ release-blocking); consumer predicate = D4's TOTAL `release_blocking`, never a bool. |
| **B4** mixed/discretionary unmapped | §5.3 normative rule: advance iff every required dimension basis is `executed`; all else HOLD. |
| **B6** validator unbuildable + not_asserted contradiction + sidecar-absence type | §4 persisted **expected-dimension manifest** (from the BranchEvaluation list) validated against a **bound commitment**, not the envelope-under-check; typed `unattributed_not_asserted` authority; concrete `ProvenanceNotFound`/`ProvenanceUnreadable` types. |
| **B7** non-circularity is signature-only | §3.6 the basis helper consumes a **nominal typed eval result** constructible ONLY by the owning evaluator (minted in the one-shot publish path); record self-asserted fields are unreachable. |
| **B2** TOCTOU rider | §4.2 one close-owned **atomic current-reader**; publication_id minted INSIDE the one-shot publish (§3.3). |
| **B5** single-snapshot rider | §5.6 republish from ONE locked `PublishSnapshot` used once for both verdict and envelope. |
| **OQ2** `executed_trusted_record` | §3.8 **NON-ADVANCING** for the release rule (their D4 ruling, 3/3); honestly labelled for audit; its dependency is a NEW independent close-ack producer, **not** #48. |
| authority not authenticated (smaller) | §3.7 `recorder` is **audit attribution, not authenticated**; pointer SHA is integrity not authenticity; local-writer threat ceiling stated. |
| **Finding B** (cross-artifact) | §5.5 the envelope's `gate:<name>` dimensions are AUTHORITATIVE for the waiver-backed-gate fact; the consumer does NOT keep a parallel gate_check scan for it. |
| **Finding E** (cross-artifact) | §7 reciprocal obligation: when `CloseNotFound` lands, the spine's close-resolver disposition (their RFC row 17) must be re-derived. |
| **policy-surface footgun** | §5.2 `policy_eval` is the spine's path-matched RELEASE policy (release_scoped/required_review), explicitly NOT `close.load_signoff_policy()`. |

---

## 1. Problem

`compute_verdict` (`close.py:146`) answers **GO | HOLD** for a close. It does not say **on what basis** each GO-gating branch passed — executed evidence, or a recorded discretionary escape (a gate waiver, `ack.override`, a counted `ack.override`, `signoff_overrides`, a worktree waiver, or the `non_lane_isolation_not_asserted` bare-bool). A reader that treats every GO identically cannot tell a fully-executed release from one riding escapes. The envelope makes the basis **explicit, per-branch, machine-readable**, so a consumer applies its own policy ("a release-blocking GO must be executed-backed, else HOLD").

Two hazards drive the structure (both verified at source, r2): a close is a **mutable subject** (`replace_close` swaps `instance_id`; `reopen` preserves it and permits same-revision republish → §3.3/§5), and **historical laundering** (`compute_verdict` on a PUBLISHED record is HOLD, `close.py:175-179`, so re-deriving provenance from the *live* close is epistemically invalid → §5.6).

The round-2 finding that reshapes r3: **provenance cannot be a second inventory sitting beside `compute_verdict`'s control flow** — the two drift (r2's static 5-row table vs the ~16 hold-producing branches at `close.py:69-85`; a new `if` with no registry entry passes both the tuple-equality check and the walk-every-branch test yet is unmapped). The fix is one shared structure both consume.

## 2. Goals / non-goals

**Goals**
- A single typed **`BranchEvaluation`** list produced once per determination; `compute_verdict` reduces it, `provenance_envelope` maps it, the validator checks it — correct-by-construction completeness (§3).
- Persist the envelope + an expected-dimension manifest in an **immutable close-owned sidecar**; a **public typed validator + atomic current-reader** (§4).
- A **normative consumer contract** keyed on D4's total `release_blocking` (§5).
- `#39`/§7 `CloseNotFound` — **shipped first**.

**Non-goals**
- Changing any GO/HOLD outcome of `compute_verdict` (the reducer must be behavior-identical to today's control flow — a required equivalence test, §8).
- Re-litigating that overrides are allowed — they are; we record that one was used, as **audit-only authority** (§3.7).
- Independent cryptographic grounding of lens acks — deferred to a named close-ack producer (§3.8).

## 3. Producer — the BranchEvaluation list (`close.py`)

### 3.1 One list, three consumers (closes B1/B4/B6/B7 by construction)

Introduce a typed record emitted by the branch evaluators inside `compute_verdict`:

```python
@dataclass(frozen=True)
class BranchEvaluation:
    branch_id: str            # stable id of the GO-gating branch (1:1 with a HOLD_* family, close.py:69-85)
    applicable: bool          # does this branch apply to THIS record? (e.g. worktree only for release-class)
    go_relevant: bool         # does a non-satisfied outcome contribute a HOLD? (escape-bearing branches only)
    dimension_id: str | None  # the envelope dimension this branch emits (None when not go_relevant)
    target: str               # target-binding: the exact object checked (gate name, lens@rev, set_id, delivered_head@rev)
    basis: Basis              # the closed BasisClass value (§3.4) — decided by _basis_for (§3.6)
    authority: Authority | None  # typed, audit-only; null iff basis is executed-family (§3.7)
```

- **`compute_verdict` REDUCES the list**: for each `applicable and go_relevant` evaluation whose `basis` is not GO-permitting, it emits the branch's `HOLD_*` code. This reducer replaces the freehand `if`/loop and **must be output-identical to today's verdict** (§8 equivalence test).
- **`provenance_envelope` MAPS the list**: each `applicable and go_relevant` evaluation with a `dimension_id` becomes one envelope dimension `{dimension, target, basis, authority}`.
- **The validator (§4.2) checks** the envelope's dimensions against the **manifest** (the committed BranchEvaluation `dimension_id` set + hash) persisted at publish.

Because all three consume the *same* list, a new GO-gating branch that appends a `BranchEvaluation` is automatically reduced, mapped, and validated; one that does not is a **producer bug** caught by the completeness invariant below — there is no static inventory to drift.

**Completeness invariant (correct-by-construction, replaces r2's tuple):** `provenance_envelope` asserts that the set of `applicable and go_relevant` BranchEvaluations equals the set of dimensions emitted — exactly one dimension per go-relevant applicable branch, no orphan dimension, no unmapped branch. One representative test walks a record exercising every branch id and asserts the emitted set equals the applicable-go-relevant set (§8). The **structure is the completeness argument**; there is no lattice to enumerate (premise-and-form).

### 3.2 Which branches earn a dimension (B1 selection criterion + riders)

A branch earns a dimension **iff it has more than one satisfying basis — i.e. it has an escape route** (claude-rev's criterion; a branch that can only be executed-or-fail needs no basis disambiguation). The go-relevant, dimension-bearing branches:

| `dimension_id` | branch (anchor) | escape route | `basis` domain |
|---|---|---|---|
| `gate:<name>` — **per gate ROW**, release-relevant blocking/required rows only | `:186-188` | gate waiver | `executed` \| `waived` \| `unknown` |
| `worktree` — **applicable only when `classify_release==release`** | `:190-204` | `waived`, `not_asserted` | `executed` \| `waived` \| `not_asserted` \| `unknown` |
| `lens:<lens_id>` | `:209-230` | `ack.override` | `executed_trusted_record` \| `ack_override` \| `unknown` |
| `blocker:<remediation_id>` | `:245-247` | waived gate | `executed` \| `waived_gate` \| `unknown` |
| `signoff:<set_id>` | `:250-251`, counted route `:317/:346-348` | `signoff_overrides`, counted `ack.override` | `executed` \| `skipped_override` \| `counted_ack_override` \| `unknown` |

Two round-2 riders, folded:
- **(a) applicability is shared, not just labels.** `worktree` applies only for a release-class record; the manifest records the **applicable set for this record**, so the consumer/validator can't drift on which dimensions *should* exist (a labels-only share would let the applicability predicate drift while the id-assertion still passes).
- **(b) only release-relevant blocking/required gate rows** emit a dimension. A red **non-blocking warn/info** gate row must NOT emit `unknown` (that would make §5 HOLD it, contradicting the additive "never changes a verdict" goal). Emit one `gate:<name>` per gate the check evaluated **that is blocking/required**.

### 3.3 Publication identity (B2 + one-shot publish)

At **every** publish (initial and any republish after reopen), a `publication_id` is minted — a random 128-bit id (Q1: identity, single-purpose; if audit ordering is ever needed, a *separate* lock-minted `publication_seq`, never overload identity with order). It is minted **inside the sole close-owned one-shot publish mutation**, never accepted as a caller argument (rider 2). The envelope binds the **full tuple** `{close_id, instance_id, revision, publication_id}` + final verdict. A reopen→republish preserving `instance_id` still mints a new `publication_id`, so a stored prior-publication envelope no longer matches.

### 3.4 One closed `BasisClass` (closes the P0)

Defined **once**, cited by producer, `rollup()`, validator, and D4 — never re-listed:

```python
class Basis(str, Enum):
    executed = "executed"                         # merit-backed positive (the ONLY advancing basis)
    executed_trusted_record = "executed_trusted_record"  # self-stored lens ack — NON-ADVANCING (§3.8)
    waived = "waived"; waived_gate = "waived_gate"
    ack_override = "ack_override"; counted_ack_override = "counted_ack_override"
    skipped_override = "skipped_override"; not_asserted = "not_asserted"
    unknown = "unknown"

ADVANCING = frozenset({Basis.executed})                       # D4's release rule keys on THIS
ESCAPE = frozenset({Basis.waived, Basis.waived_gate, Basis.ack_override,
                    Basis.counted_ack_override, Basis.skipped_override, Basis.not_asserted})
```

The r2 `"verified"` positive is **collapsed to `executed`** (it bought nothing and left the P0). `executed_trusted_record` is a positive but **not in ADVANCING** (§3.8). `authority` pairing (§3.7): `basis in (executed, executed_trusted_record)` ⇒ `authority is None`; `basis in ESCAPE` ⇒ typed `authority`; `unknown` ⇒ `authority is None`. The validator rejects any other pairing.

### 3.5 One shared `rollup()` (total, no overlap)

A single pure `rollup(dimensions, verdict) -> str` called by BOTH producer and validator (so a stored rollup can never disagree with a recomputed one — Q2). **Precedence, stated so no case matches two labels:**
1. `verdict == HOLD` ⇒ `"hold"` (no GO-provenance to roll up; never advances).
2. else any `unknown` ⇒ `"indeterminate"` (fail-closed dominates).
3. else any ESCAPE and any ADVANCING/trusted-record ⇒ `"mixed"`.
4. else all ESCAPE ⇒ `"discretionary"`.
5. else all `executed`/`executed_trusted_record` ⇒ `"executed"`.
6. empty applicable-go-relevant set ⇒ `"indeterminate"` (never vacuously `"executed"`).

The rollup is a DERIVED convenience; the **per-dimension list is authoritative** and D4 gates on `all(d.basis in ADVANCING)`, never a stored rollup.

### 3.6 Non-circularity is STRUCTURAL, by data ORIGIN (B7)

r2 removed `record` from the helper *signature*, which the panel correctly showed does not constrain data *origin* (a helper taking `independent_result` accepted `record['self_asserted_result']`). r3: the basis is decided by

```python
def _basis_for(*, eval_result: EvalResult, escape: EscapeMarker | None) -> Basis
```

where `EvalResult` is a **nominal typed result constructible only by the owning evaluator** (the gate evaluator mints `GateEvalResult` from the `gates.check_gates` I/O; the signoff evaluator mints `SignoffEvalResult` from the CLI-resolved roster/policy). The one-shot publish path is the only site that constructs these; `provenance_envelope`/`compute_verdict` receive them already-built. A record self-asserted result field is not an `EvalResult` and cannot be passed — circular derivation is a **type error**, not a review catch. Escape markers are normalized (`EscapeMarker`) and only ever select the escape branch; they never upgrade a basis. Tested by a call-graph/source-shape test (no `record`/closure/ambient route reaches `_basis_for`).

### 3.7 Authority is typed and audit-only — and NOT authenticated (smaller item)

Escape dimensions carry a typed `Authority`; executed-family and `unknown` carry `None`. **`recorder` is audit attribution, NOT an authenticated writer** — close authority is advisory and records unauthorized actors, and `ASSURANCE.md` states a gate-waiver `operator` is unauthenticated free text. So:

```python
@dataclass(frozen=True)
class Authority:
    kind: str                 # gate_waiver | ack_override | counted_ack_override | signoff_override | worktree_waiver | worktree_not_asserted
    target: str
    recorder: str | None      # updated_by — the writer as RECORDED (audit only; NOT authenticated)
    claimed_operator: str | None  # waiver.operator — CALLER-ASSERTED, never authenticated
    reason: str | None; decided_at: str | None; expiry: str | None
    source_binding: dict      # {revision, instance_id, publication_id}
```

**Threat-model ceiling stated:** this is a trusted-team local bus; the sidecar pointer **sha256 is integrity (tamper-evidence), not authenticity** (it does not prove who wrote it). Authority never influences a basis — it is attached after `_basis_for` decides, only to escape bases.

The **`not_asserted` contradiction resolved one way:** the bare-bool `non_lane_isolation_not_asserted` gets a typed `Authority(kind="worktree_not_asserted", recorder=<record updated_by>, claimed_operator=None, reason="non_lane_isolation_not_asserted")` — so every ESCAPE basis carries a typed authority (no null-escape exception, no un-constructible envelope). Upstream recording a real operator authority for it remains a named future improvement, but the envelope is constructible today.

### 3.8 `executed_trusted_record` is NON-ADVANCING (OQ2 ruling)

The lens `executed` basis rests **entirely on record-self-stored data** (`lens_acks`; `signoff_eval` resolves policy/roster but not the ack's occurrence or sender), so honoring it for release advancement reopens the invisible-GO route the envelope exists to close (0 of N fields independently grounded — not a disclosed-narrow residual like #15 row-27's same-3-fields alias). r3, adopting D4's ruling:
- The lens positive basis is labelled **`executed_trusted_record`** — honest in the envelope for audit.
- It is **NOT in ADVANCING**; D4's release rule treats it as non-advancing **by policy, with NO schema change** (the distinction survives into the data; a future stronger consumer could accept it).
- Its dependency is a **NEW named dependency: an independent close-ack provenance producer** (a validated bus-sender + review-binding for the ack). This is **explicitly not #48** — #48 is `Thread.opener_message_id` surfacing (RFC :5205-5227), which does not authenticate a close ack. Filed distinctly.
- The residual is **tested as present** (§8), not hidden. The path to advancement is a visible, named, closable producer — not a silent permanent HOLD.

### 3.9 Fail-closed

`_basis_for` returning no determinable basis ⇒ `unknown` ⇒ `rollup` `indeterminate` ⇒ release-blocking consumer HOLDs (§5.3). `unknown` is never upgraded.

## 4. Persistence — immutable close-owned sidecar + manifest (B6, Q3 close-owned)

- The envelope **and an expected-dimension manifest** (the committed BranchEvaluation `dimension_id` set for this record + its sha256) are written to an **immutable, append-only, close-owned sidecar**, keyed by **`(close_id, instance_id, revision, publication_id)`**, **write-once = exclusive-create + reject-overwrite in the close API** (defined operationally, not aspirationally). The record carries `provenance_ref = {publication_id, sha256}`.
- **Write order:** sidecar entry first, then atomically commit the record pointer. Orphan sidecar entry (no pointer) is harmless; a published record whose pointer resolves to no sidecar entry **fails closed** (§5.4). Survives `replace_close`/`reopen` (the sidecar is never rewritten).

### 4.1 Sidecar absence is TYPED (B6 + §7)
Concrete public types, not a comment: **`ProvenanceNotFound`** (entry never written → *established* absence) vs **`ProvenanceUnreadable`** (present-but-corrupt/unreadable → *unknown*, retry may clear). `CloseNotFound` (§7) covers the record; these cover the sidecar.

### 4.2 Public typed validator + atomic current-reader (B6 + B2 TOCTOU)
Close-owned public API the consumer MUST use (no re-derivation):

```python
def read_current_provenance(store, close_id) -> CurrentProvenance   # ATOMIC: record + pointed sidecar under the close lock
def validate_provenance(cp: CurrentProvenance) -> ValidationResult
```

`read_current_provenance` returns the record **and** its pointed sidecar entry read together **under the close lock**, so there is no consumer-side TOCTOU (the r2 `live_record` param — "not live by construction" — is gone; D4 consumes this atomic result, never a separately-loaded dict). `validate_provenance` enforces, any failure ⇒ **invalid ⇒ HOLD**:
1. `schema_version` understood.
2. dimension set **exact/unique/exhaustive** vs the **persisted manifest's committed set** (NOT derived from the envelope under check, NOT re-read from current gates) — so a deleted `gate:<name>` dimension is caught by comparison to the bound commitment.
3. every dimension has a valid target binding.
4. **basis ↔ authority pairing** (§3.4/§3.7).
5. the publication tuple matches the record; the pointer digest matches the sidecar entry.
6. `rollup()` **recomputed** (§3.5) equals any stored value.

## 5. Consumer contract (normative — for D4 / any gate reader)

> The envelope binds **one publication** at determination time and is **true evidence about that publication forever** (audit survives). It is **never provenance for a different publication.**

### 5.1 Read only via the atomic current-reader
A consumer obtains provenance **only** from `read_current_provenance` + `validate_provenance` (§4.2) — never a hand-loaded record/sidecar. This is the single consistency boundary that closes the TOCTOU.

### 5.2 The release predicate is D4's TOTAL determination (B3, tri-state, + policy-surface)
"Is this item release-blocking?" = D4's total `release_blocking`, from **two axes**:
- **close-scope axis:** `close.classify_release(record) -> "release" | "non_release" | "unknown"`. It **RAISES a typed `CloseError` on an unknown/unparseable scope** (or returns `unknown`); the known non-release vocabulary is **NAMED** (not "anything not in the release set"). Countermodel this closes: `scope="releaze"` (a typo) is well-formed today (`_is_wellformed` accepts any nonempty scope), and a bool accessor returns `False` → wrongly exempts it; the tri-state maps `unknown ⇒ release-blocking`. §5 consumes the tri-state result and **never negates a bool**. Release-class is read from the **record**, never the envelope (the absence rule §5.4 must be determinable when the envelope is gone).
- **policy axis:** `policy_eval` is the spine's **path-matched RELEASE policy** (carrying `release_scoped` / `required_review`). **It is explicitly NOT `close.py`'s signoff policy** (`validate_signoff_policy`/`load_signoff_policy` at `close.py:501/645`) — those are a valid, importable, type-checking object of the **wrong kind**; wiring them in makes `release_scoped` never-present → fail-closed-to-release-blocking → everything HOLDs forever with no error. The spine owns `policy_eval`; `close.py` does not supply it.

`release_blocking` = the fail-closed truth table over (classify_release, policy_eval); absence/unknown on either axis ⇒ release-blocking.

### 5.3 The normative rule (B4)
For a **release-blocking** item, a consumer may advance **iff** a valid envelope for the current publication tuple exists (§4.2) **and every required dimension basis is in `ADVANCING`** (`= {executed}`). **Everything else HOLDs**, explicitly: `executed_trusted_record` (§3.8), `mixed`, `discretionary`, `indeterminate`, **absence** (§5.4), **mismatch**, **invalid schema**. Non-release items: the envelope is **advisory** (§5.7).

### 5.4 Absence / mismatch (established ⇒ HOLD)
Release-blocking GO requires a valid current-publication envelope. Determinate absence (`ProvenanceNotFound` §4.1 / `CloseNotFound` §7) ⇒ *established* ⇒ HOLD. Publication-tuple mismatch ⇒ *established* ⇒ HOLD (a retry can never clear a mismatch; **null `instance_id` never matches** — treated as absence). `ProvenanceUnreadable` / could-not-read ⇒ *unknown* ⇒ retry may clear. Stated **forward** (r2 had it backwards): if absence were *permissive*, `rm sidecar` would flip HOLD→GO; requiring the envelope makes deletion flip **GO→HOLD**, the safe direction.

### 5.5 The gate-waiver fact has ONE authoritative producer (Finding B)
For the "was this gate green-on-merit vs waived?" determination, the envelope's `gate:<name>` dimensions are **authoritative**; the D4/work consumer reads THEM and does **not** also keep an independent `gate_check` scan for that fact. Two shipped producers of one waiver fact is the expensive, drift-prone version the enum sweep flagged (RFC :3143 names the close-side provenance envelope as exactly this capability). The consumer's own `gate_check` remains for its *own* gating; the *waiver-provenance* fact comes from the envelope.

### 5.6 Republish, not re-provenance (B5 + single-snapshot)
The r2 live re-provenance path is **removed** (epistemically invalid: `compute_verdict` on a published record is HOLD). The only way to clear a mismatch/absence/indeterminate HOLD is to **republish**: a new publication determination, new `publication_id`, computed from **one locked `PublishSnapshot`** (the BranchEvaluation bundle used ONCE for both the verdict and the envelope, so verdict and provenance can never diverge across two eval snapshots), appended to the sidecar. A historical publication is never cleared from current state.

### 5.7 Non-release items
Advisory; absence/mismatch is not a gate concern.

## 6. Migration — HOLD-until-republish (Q3, B5)
Envelopes generate at publish for all closes going forward. Pre-existing release-class closes (`instance_id` may be `None`, `:414-416`) **HOLD until republished**. A one-time tool may inventory them or emit forensic `unknown`/`indeterminate` envelopes; it **may NOT invent `executed`** from current state and **cannot clear a HOLD** (current code retains no publish-time eval snapshot to certify from). Chosen explicitly; the population refills the day after ship.

## 7. `#39` — close-only not-found split (SHIPS FIRST) + reciprocal obligation (Finding E)
§5.4's determinate-absence rule **requires** this type, so §7 lands **before** the consumer contract can be built (a D4 built against today's `load_close` collapses established absence into `unknown` and inverts the fail-closed direction).
- Add `class CloseNotFound(CloseError)` raised **only** on definitive absence; unreadable/malformed keep raising `CloseError`. **Forbid `str(e).startswith(...)` parsing.** Scope = `close` only; the sidecar has its own `ProvenanceNotFound`/`ProvenanceUnreadable` split (§4.1) — `CloseNotFound` does not cover it.
- **RECIPROCAL OBLIGATION (Finding E, cross-artifact):** the instant `CloseNotFound` lands, the spine's close-resolver disposition (their RFC **row 17**, which currently emits `unreadable` for a genuinely-absent close) **must be re-derived** — otherwise the spine reintroduces from its side the exact established-absence→unknown inversion §7 exists to prevent. §7 carries this note so neither document assumes the other tracks it; until `CloseNotFound` lands, the spine's current disposition stands.

## 8. Test plan
- **Verdict equivalence (non-negotiable):** the BranchEvaluation reducer produces the **identical** GO/HOLD + hold-code set as today's `compute_verdict` across the existing verdict corpus (the envelope must never change a verdict).
- **Completeness invariant (§3.1):** a record exercising every branch id → emitted dimensions == applicable-go-relevant set; a synthetic new go-relevant branch that appends no BranchEvaluation trips the invariant; a warn/info gate row emits NO dimension (rider b).
- **Non-circularity (§3.6):** `_basis_for` accepts only `EvalResult`; a record self-asserted field is a type error; call-graph test shows no record/closure route.
- **BasisClass/rollup (§3.4/3.5):** `"verified"` is gone; an all-`executed` release advances (the P0 regression); `executed_trusted_record` does NOT advance; rollup precedence has no double-match; empty set ⇒ indeterminate.
- **Validator (§4.2):** deleted dimension vs manifest ⇒ invalid; stored rollup ≠ recomputed ⇒ invalid; executed+authority ⇒ invalid; escape+null-authority ⇒ invalid; `not_asserted` envelope is CONSTRUCTIBLE and valid (§3.7); pointer digest ≠ sidecar ⇒ invalid.
- **B2/TOCTOU:** publish→tuple T1; reopen+republish→new publication_id T2; stored T1 vs current T2 ⇒ mismatch/HOLD; the atomic reader returns record+sidecar consistently under a concurrent republish.
- **B3 tri-state:** `scope="releaze"` ⇒ unknown ⇒ release-blocking; named non_release scope ⇒ non_release; policy-surface — wiring `load_signoff_policy` is rejected/typed so it cannot masquerade as `policy_eval`.
- **Consumer:** advisory-scope + release_scoped policy ⇒ HOLD; absence/mismatch ⇒ established/HOLD; unreadable ⇒ unknown; republish (not re-provenance) clears; null instance never matches.
- **§7:** absent ⇒ `CloseNotFound`; unreadable ⇒ `CloseError`; no-startswith test; a note/test referencing the row-17 reciprocal re-derivation.
- **executed_trusted_record residual TESTED AS PRESENT** (§3.8).

## 9. Open questions for panel round 4
1. `BranchEvaluation` lives in `close.py` as the shared reducer input — does D4 want it importable as a typed contract, or only the resulting validated envelope? (I lean: only the validated envelope crosses the boundary; BranchEvaluation is close-internal.)
2. The close-ack provenance producer (§3.8) — is filing it as a distinct dependency (not #48) the right call, and does D4 want `executed_trusted_record` non-advancing hard-coded or policy-flagged?
3. Finding B (§5.5): confirm D4 will source the waiver-provenance fact from the envelope and retire any parallel scan — or name why it must keep both.
4. Anything in §5 (esp. the tri-state predicate + the atomic current-reader) D4 still cannot honor.

Build begins after round 4 converges. §7 lands first; the verdict-equivalence test gates the reducer refactor; nothing changes an existing `compute_verdict` outcome.
