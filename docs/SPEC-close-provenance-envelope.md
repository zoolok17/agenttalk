# SPEC — Close-provenance envelope (`close.py`) · #31

**Status:** design **r5** (folds second-laptop Tier-3 panel round 4: **HOLD / DO-NOT-RATIFY 0/3** — two P0s (signoff invisible-GO reopened; the one-list model cannot represent today's verdict), five P1s, two P2s, and rulings on all four r4 open questions). For panel **round 5** before build.
**Author:** `claude-agenttalk-lead` (primary laptop) · r1 2026-07-19 · r2 2026-07-20 · r3 2026-07-21 · r5 2026-07-21
**Blocks:** second-laptop Native Work & Evidence Spine **D4** (their Open-Q#7)
**Related:** GH #45, Drive `FROM-secondlaptop` chat r2/r4 verdicts; review-lesson memories (circular-validation; target-binding & fail-closed; premise-and-form; check-not-read).
**Sequenced dependencies (named, not silent):** §7 `CloseNotFound` **ships first, as a coordinated change with the spine's RFC row-17 reciprocal re-derivation**; the independent **close-ack provenance producer** grounding occurrence+sender for BOTH lens satisfaction AND signoff counting (a NEW dependency, **not** #48 — see §3.8) which **must precede any claim that an ack-backed release dimension can advance**; the spine's `policy_eval` release-policy surface (§5.2); the reciprocal close-resolver obligation (§7). **Scheduling hard-stop:** the reducer refactor MUST NOT START until the §3.1 outcome/dimension data model is corrected (panel's one scheduling ask, P0-2).

---

## 0. What changed in r5 (disposition of the round-4 verdict)

r3 adopted the `BranchEvaluation`-list producer (completeness by construction) and the panel confirmed that direction is right — plus two of the three cross-artifact refinements landed *better* than asked. But round 4 found the **model cannot yet carry that shape**: it drops the verdict's dimensionless HARD holds, and the signoff consumer reopens the invisible-GO route the lens fix closed. r5 corrects the data model and grounds both ack consumers.

| r4 finding | severity | r5 resolution |
|---|---|---|
| **P0-1** signoff positives reopen invisible-GO (same ungrounded `lens_acks` field advances via `signoff:<set_id>→executed`; executed countermodel `test_signoff_go_with_two_distinct_candidates`) | P0 | §3.2/§3.8 an ordinary counted `signoff:<set_id>` positive is **`executed_trusted_record` (NON-ADVANCING)** — same disposition as the lens ack, 3/3. ONE close-ack producer grounds occurrence+sender for BOTH consumers. Non-advancement is **hard-coded, never policy-flagged**. Residual tested-as-present on BOTH paths. `counted_ack_override` unchanged (already ESCAPE). |
| **P0-2** the one-list model cannot represent today's verdict (dimensionless HARD holds are dropped or wrongly demand dimensions; no outcome payload; §8 can't see a never-appended branch) | P0 | §3.1 **separate VERDICT RELEVANCE from DIMENSION ELIGIBILITY**: every verdict branch emits one typed **`BranchOutcome`** (satisfied + hold codes/detail); `compute_verdict` reduces ALL applicable outcomes; only outcomes carrying an optional `Dimension` enter envelope/manifest; completeness asserts over dimension-bearing outcomes; the reducer is the **sole hold writer** (structurally enforced). **DO NOT START THE REDUCER UNTIL THIS LANDS.** |
| **P1-a** `GO_PERMITTING` used once, defined nowhere; natural reading `ADVANCING∪ESCAPE` is wrong (omits `executed_trusted_record`) → flips a verdict | P1 | §3.4 `GO_PERMITTING = frozenset(Basis) − {unknown}` (8 of 9), named beside ADVANCING/ESCAPE, with the stated asymmetry: **the VERDICT axis permits escapes, the RELEASE axis does not.** §8 corpus MUST contain a release-blocking close with a required lens. |
| **P1-b** validator declares per-dimension basis domains but does not enforce them (`lens:x + executed` is globally well-typed and reaches ADVANCING) | P1 | §4.2 adds exact **dimension-family → Basis-domain** validation + cross-domain injection tests. `Authority.kind` becomes a **closed nominal enum** (was `str`); `source_binding` is validated against the publication tuple. |
| **P1-c** manifest commits the id SET but not `target`; swap a target with ids unchanged and the exact-set check passes (r2 had per-dimension targets; consolidation dropped them) | P1 | §3.3/§4 the manifest commits exact **`(dimension_id, target)` pairs**; validator checks the pairs; target-substitution test with the id set unchanged. |
| **P1-d** `non_release` has no producer (vocabulary named but no member; `close.py` accepts any nonempty scope) → the `non_release` arm is dead as written | P1 | §5.2 defines a **closed `NON_RELEASE_SCOPES`** set in `close.py`; `classify_release` returns `non_release` only for its members, `release` for `RELEASE_CLASS_SCOPES`, else `unknown`. Both arms constructible. |
| **P1-e** §5.2 is not a total callable contract (`classify_release` "RAISES CloseError (or returns unknown)") | P1 | §5.2 **resolved to the TOTAL RETURN**: `classify_release` NEVER raises for scope classification; unparseable/absent scope ⇒ `unknown` (⇒ release-blocking), matching the `releaze→unknown` test. (`CloseNotFound` is a *record*-absence type, §7, orthogonal.) |
| **P2-a** rollup precedence overlaps on the empty set (`all ESCAPE` is vacuously true for empty, shadowing `empty⇒indeterminate`) | P2 | §3.5 the **empty case moves first** (right after HOLD) so it can never be shadowed. |
| **P2-b** §6 names republish as THE remedy, but republish never clears a lens/ack-bearing close (`executed ∉ lens domain`) — a remedy that does not remedy | P2 | §6 clause: republish clears the migration HOLD **only for closes with no ack-backed required dimension**; lens/signoff-bearing closes remain held until the close-ack producer lands (the *same* dependency, not a new one). §3.8 states the producer must **widen the basis domain**, not relabel. |
| **OQ1** `BranchEvaluation` importable vs close-internal | ruling 2-1 | §3.1/§4.2/§9 **export the TYPE (frozen `Dimension` read model + `Basis`/`ADVANCING`/`GO_PERMITTING` policy contract) and a validated-provenance read model; keep the CONSTRUCTION/minting path close-internal.** Exporting the read model is not exporting the control flow. |
| **OQ2** close-ack producer disposition | 3/3 | §3.8 distinct from #48; **hard-coded non-advancing**; broadened to specialist signoffs (P0-1); roster/sender resolution via the **`signoff_eval` impure→pure bridge applied to acks** (so grounding does not collide with `_ack_authorized`'s self-stored-field purity, `close.py:391-394`). |
| **OQ3** Finding B (waiver-provenance single producer) | confirmed 3/3 | §5.5 stands unchanged: D4 sources the waiver fact solely from `gate:<name>` dimensions and retires its parallel row scan; its own `gate_check` remains only for work's own live gating. |
| **OQ4** what D4 cannot honor | — | §5.4 adds a **bounded stable D4 disposition mapping** (ProvenanceNotFound / CloseNotFound / mismatch / invalid ⇒ established ⇒ HOLD; ProvenanceUnreadable ⇒ unknown ⇒ retry). The concrete D4 problem-code names live in the spine's RFC (they carry it) but the stable dispositions are named here, before implementation. |

---

## 1. Problem

`compute_verdict` (`close.py:146`) answers **GO | HOLD** for a close. It does not say **on what basis** each GO-gating branch passed — executed evidence, or a recorded discretionary escape (a gate waiver, `ack.override`, a counted `ack.override`, `signoff_overrides`, a worktree waiver, or the `non_lane_isolation_not_asserted` bare-bool). A reader that treats every GO identically cannot tell a fully-executed release from one riding escapes. The envelope makes the basis **explicit, per-branch, machine-readable**, so a consumer applies its own policy ("a release-blocking GO must be executed-backed, else HOLD").

Two hazards drive the structure (both verified at source, r2): a close is a **mutable subject** (`replace_close` swaps `instance_id`; `reopen` preserves it and permits same-revision republish → §3.3/§5), and **historical laundering** (`compute_verdict` on a PUBLISHED record is HOLD, `close.py:175-179`, so re-deriving provenance from the *live* close is epistemically invalid → §5.6).

Two round-4 structural facts now shape r5:
- **Provenance is not a second inventory beside `compute_verdict`'s control flow** (the r2 drift argument) — one shared structure both consume. r3 established this.
- **But that structure is not the dimension list — it is the OUTCOME list.** `compute_verdict` decides GO/HOLD over *every* branch, including many HARD, escape-less HOLD branches that earn no dimension (`malformed_state`, `revision_dirty_or_unresolved`, `publish_not_allowed`, `accepted_counter_missing_remediation`, `invalid_signoff_policy`, `unmapped_required_risk`, `stale_signoff_route`; `close.py:68-85`, `:171-251`). If the shared structure is the *dimension* list, the reducer either drops those failures (verdict changes) or is forced to invent dimensions for them. So the shared structure must model **verdict relevance** for every branch and **dimension eligibility** as an optional facet of it (§3.1).

## 2. Goals / non-goals

**Goals**
- A single typed **`BranchOutcome`** list produced once per determination; `compute_verdict` reduces **all applicable outcomes**, `provenance_envelope` maps the **dimension-bearing subset**, the validator checks that subset against a bound manifest — correct-by-construction completeness with the verdict preserved for escape-less branches (§3).
- Persist the envelope + an expected-dimension manifest (committing `(id, target)` pairs) in an **immutable close-owned sidecar**; a **public typed validator + atomic current-reader**, plus an **exported validated-provenance read model** (§4).
- A **normative consumer contract** keyed on D4's total `release_blocking`, with a bounded stable disposition mapping (§5).
- `#39`/§7 `CloseNotFound` — **shipped first**, coordinated with the spine's RFC row-17.

**Non-goals**
- Changing any GO/HOLD outcome of `compute_verdict` (the reducer must be behavior-identical to today's control flow — a required equivalence test, §8; **necessary but not sufficient**, see §8).
- Re-litigating that overrides are allowed — they are; we record that one was used, as **audit-only authority** (§3.7).
- Independent cryptographic grounding of lens/signoff acks — deferred to the named close-ack producer (§3.8); until it lands, both ack bases are **non-advancing**.

## 3. Producer — the BranchOutcome list (`close.py`)

### 3.1 Verdict relevance vs dimension eligibility — one outcome per branch (closes P0-2, B1/B4/B6/B7)

Every GO/HOLD branch of `compute_verdict` emits exactly one typed outcome. **Dimension eligibility is an optional facet of an outcome, not a second list.**

```python
@dataclass(frozen=True)
class BranchOutcome:
    branch_id: str                 # stable id of the verdict branch (close.py:68-85, :171-251)
    applicable: bool               # does this branch apply to THIS record?
    satisfied: bool                # did the branch pass? (verdict relevance — for EVERY branch)
    holds: tuple[HoldCode, ...]    # 0..N typed holds this branch contributes when not satisfied
                                   #   (one required lens can emit unauthorized + stale + undecided-counter
                                   #    SIMULTANEOUSLY, close.py:209-229 — so holds is a LIST, not 1:1)
    dimension: Dimension | None    # present IFF this branch is dimension-eligible (escape-bearing, §3.2)

@dataclass(frozen=True)
class Dimension:
    dimension_id: str              # e.g. gate:<name>, worktree, lens:<id>, blocker:<id>, signoff:<set_id>
    target: str                    # target-binding: the exact object checked (committed in the manifest, §3.3)
    basis: Basis                   # the closed BasisClass value (§3.4) — decided by _basis_for (§3.6)
    authority: Authority | None    # typed, audit-only; null iff basis is executed-family (§3.7)
```

- **`compute_verdict` REDUCES ALL applicable outcomes** (not just dimension-bearing ones): the final verdict is HOLD iff any `applicable and not satisfied` outcome exists, and the emitted hold-code set is the union of those outcomes' `holds`. This replaces the freehand `if`/loop and **must be output-identical to today's verdict** (§8 equivalence test). Because escape-less HARD branches emit outcomes too, their failures are never dropped and they are never forced to carry a dimension.
- **`provenance_envelope` MAPS the dimension-bearing subset**: each `applicable` outcome with a non-null `dimension` becomes one envelope dimension `{dimension_id, target, basis, authority}`.
- **The validator (§4.2) checks** the envelope's dimensions against the **manifest** (the committed `(dimension_id, target)` pairs + hash) persisted at publish.

**The reducer is the SOLE hold writer (structural, replaces r2's tuple + r3's subset-comparison).** No verdict branch may emit a HOLD except by appending a `BranchOutcome`; `compute_verdict` derives GO/HOLD purely by reducing the outcome list. This is enforced structurally (§8: a source-shape guard that no `HOLD_*` is produced outside the reducer) rather than by an inventory comparison — because r3's "produced list vs its mapped subset" invariant is blind to a branch that never appended itself (the completeness check could not detect the omission it exists to prevent). Completeness of the *dimension* set is asserted only over dimension-bearing outcomes; completeness of the *verdict* is guaranteed by the sole-hold-writer property + the equivalence corpus + a **deleted-branch-emitter mutation test** (§8).

### 3.2 Which branches earn a dimension (B1 selection criterion + P0-1 signoff grounding)

A branch's outcome carries a `dimension` **iff it has more than one satisfying basis — i.e. it has an escape route** (claude-rev's criterion; an executed-or-fail branch needs no basis disambiguation and carries `dimension=None`). Escape-less HARD holds emit an outcome with `dimension=None` and are reduced for the verdict only.

| `dimension_id` | branch (anchor) | escape route | `basis` domain |
|---|---|---|---|
| `gate:<name>` — **per gate ROW**, release-relevant blocking/required rows only | `:186-188` | gate waiver | `executed` \| `waived` \| `unknown` |
| `worktree` — **applicable only when `classify_release==release`** | `:190-204` | `waived`, `not_asserted` | `executed` \| `waived` \| `not_asserted` \| `unknown` |
| `lens:<lens_id>` | `:209-230` | `ack.override` | `executed_trusted_record` \| `ack_override` \| `unknown` |
| `blocker:<remediation_id>` | `:245-247` | waived gate | `executed` \| `waived_gate` \| `unknown` |
| `signoff:<set_id>` | `:250-251`, counted route `:317/:346-348` | `signoff_overrides`, counted `ack.override` | **`executed_trusted_record`** \| `skipped_override` \| `counted_ack_override` \| `unknown` |

**P0-1 — the signoff domain drops `executed`.** An ordinary counted `signoff:<set_id>` positive reads the **same record-self-stored `lens_acks`** field as the lens dimension (`close.py:296` `acks = record.get("lens_acks", {})`; `:318-320` `_signoff_signers`; `:337-356` `ack.get("from")/.get("revision")/.get("status")/.get("override")`). Routing an ungrounded ack through `signoff:<set_id>` must NOT reach ADVANCING when the identical ack through `lens:<id>` cannot. So the counted-signoff positive basis is **`executed_trusted_record` (non-advancing)**, not `executed`. It advances only once the close-ack producer grounds occurrence+sender (§3.8) — the same producer that grounds the lens ack, ONE producer serving two consumers. Wrapping the result as a nominal `SignoffEvalResult` does **not** fix this on its own: a nominal type constrains who may *mint* the value, not where the *data* came from (origin, not signature) — so the grounding must happen at the producer, and the basis stays non-advancing until it does.

Two round-2 riders, retained:
- **(a) applicability is shared, not just labels.** `worktree` applies only for a release-class record; the manifest records the **applicable set for this record**, so the consumer/validator can't drift on which dimensions *should* exist.
- **(b) only release-relevant blocking/required gate rows** emit a dimension. A red **non-blocking warn/info** gate row must NOT emit `unknown` (that would make §5 HOLD it, contradicting the additive "never changes a verdict" goal). Emit one `gate:<name>` per gate the check evaluated **that is blocking/required**.

### 3.3 Publication identity (B2 + one-shot publish)

At **every** publish (initial and any republish after reopen), a `publication_id` is minted — a random 128-bit id (Q1: identity, single-purpose; if audit ordering is ever needed, a *separate* lock-minted `publication_seq`, never overload identity with order). It is minted **inside the sole close-owned one-shot publish mutation**, never accepted as a caller argument. The envelope binds the **full tuple** `{close_id, instance_id, revision, publication_id}` + final verdict. A reopen→republish preserving `instance_id` still mints a new `publication_id`, so a stored prior-publication envelope no longer matches. The persisted **manifest commits the `(dimension_id, target)` pairs** for this publication (P1-c), not merely the id set.

### 3.4 One closed `BasisClass` + the two named positive sets (closes the P0, P1-a)

Defined **once**, cited by producer, `rollup()`, validator, and D4 — never re-listed:

```python
class Basis(str, Enum):
    executed = "executed"                                 # merit-backed positive (the ONLY release-advancing basis)
    executed_trusted_record = "executed_trusted_record"   # self-stored lens OR signoff ack — NON-ADVANCING (§3.8)
    waived = "waived"; waived_gate = "waived_gate"
    ack_override = "ack_override"; counted_ack_override = "counted_ack_override"
    skipped_override = "skipped_override"; not_asserted = "not_asserted"
    unknown = "unknown"

ADVANCING     = frozenset({Basis.executed})                       # the RELEASE axis (D4's rule) keys on THIS
ESCAPE        = frozenset({Basis.waived, Basis.waived_gate, Basis.ack_override,
                           Basis.counted_ack_override, Basis.skipped_override, Basis.not_asserted})
GO_PERMITTING = frozenset(Basis) - {Basis.unknown}               # the VERDICT axis: 8 of 9 (= ADVANCING ∪ ESCAPE ∪ {executed_trusted_record})
```

**P1-a — the asymmetry is now two NAMED sets.** `GO_PERMITTING` is used by the reducer (§3.1) to decide whether a dimension-bearing branch contributes a verdict HOLD; `ADVANCING` is used by D4's release rule (§5.3). They differ by exactly `executed_trusted_record`: **the VERDICT axis permits escapes AND a trusted-record positive (only `unknown` holds the verdict); the RELEASE axis permits only `executed`.** That difference is the entire point of the envelope — a lens/signoff-bearing close is GO on the verdict axis today (so the reducer must not flip it) yet must not *advance a release* until grounded. The natural (wrong) reading `ADVANCING ∪ ESCAPE` omits `executed_trusted_record` and would flip every lens/signoff-bearing verdict to HOLD, violating §2.

The r2 `"verified"` positive is **collapsed to `executed`**. `authority` pairing (§3.7): `basis in (executed, executed_trusted_record)` ⇒ `authority is None`; `basis in ESCAPE` ⇒ typed `authority`; `unknown` ⇒ `authority is None`. The validator rejects any other pairing.

### 3.5 One shared `rollup()` (total, no overlap — P2-a empty-case first)

A single pure `rollup(dimensions, verdict) -> str` called by BOTH producer and validator (so a stored rollup can never disagree with a recomputed one — Q2). **Precedence, empty-case first so no case matches two labels:**
1. `verdict == HOLD` ⇒ `"hold"` (no GO-provenance to roll up; never advances).
2. **empty applicable dimension-bearing set ⇒ `"indeterminate"`** (never vacuously `"executed"` or `"discretionary"` — this MUST precede the all-ESCAPE / all-positive cases, which are vacuously true on the empty set; P2-a).
3. else any `unknown` ⇒ `"indeterminate"` (fail-closed dominates).
4. else any ESCAPE and any ADVANCING/trusted-record ⇒ `"mixed"`.
5. else all ESCAPE ⇒ `"discretionary"`.
6. else all `executed`/`executed_trusted_record` ⇒ `"executed"`.

The rollup is a DERIVED convenience; the **per-dimension list is authoritative** and D4 gates on `all(d.basis in ADVANCING)`, never a stored rollup.

### 3.6 Non-circularity is STRUCTURAL, by data ORIGIN (B7)

The basis is decided by

```python
def _basis_for(*, eval_result: EvalResult, escape: EscapeMarker | None) -> Basis
```

where `EvalResult` is a **nominal typed result constructible only by the owning evaluator** (the gate evaluator mints `GateEvalResult` from the `gates.check_gates` I/O; the signoff evaluator mints `SignoffEvalResult` from the CLI-resolved roster/policy). The one-shot publish path is the only site that constructs these; `provenance_envelope`/`compute_verdict` receive them already-built. A record self-asserted result field is not an `EvalResult` and cannot be passed — circular derivation is a **type error**, not a review catch. Escape markers are normalized (`EscapeMarker`) and only ever select the escape branch; they never upgrade a basis. Tested by a call-graph/source-shape test (no `record`/closure/ambient route reaches `_basis_for`).

**Note (P0-1 origin, not signature):** minting `SignoffEvalResult` from CLI-resolved policy/roster does not by itself make a counted-signoff positive *grounded* — the ack occurrence+sender still come from `record.lens_acks`. The nominal type protects the origin of the *policy* decision, not of the *ack*. Grounding the ack is the close-ack producer's job (§3.8); until then the signoff basis is `executed_trusted_record`.

### 3.7 Authority is typed and audit-only — and NOT authenticated (smaller item)

Escape dimensions carry a typed `Authority`; executed-family and `unknown` carry `None`. **`recorder` is audit attribution, NOT an authenticated writer.**

```python
class AuthorityKind(str, Enum):            # P1-b: closed nominal, not free `str`
    gate_waiver = "gate_waiver"; ack_override = "ack_override"
    counted_ack_override = "counted_ack_override"; signoff_override = "signoff_override"
    worktree_waiver = "worktree_waiver"; worktree_not_asserted = "worktree_not_asserted"

@dataclass(frozen=True)
class Authority:
    kind: AuthorityKind
    target: str
    recorder: str | None          # updated_by — the writer as RECORDED (audit only; NOT authenticated)
    claimed_operator: str | None   # waiver.operator — CALLER-ASSERTED, never authenticated
    reason: str | None; decided_at: str | None; expiry: str | None
    source_binding: dict           # {revision, instance_id, publication_id} — validated vs the publication tuple (P1-b)
```

**Threat-model ceiling stated:** this is a trusted-team local bus; the sidecar pointer **sha256 is integrity (tamper-evidence), not authenticity**. Authority never influences a basis — it is attached after `_basis_for` decides, only to escape bases.

The **`not_asserted` contradiction resolved one way:** the bare-bool `non_lane_isolation_not_asserted` gets a typed `Authority(kind=AuthorityKind.worktree_not_asserted, recorder=<record updated_by>, claimed_operator=None, reason="non_lane_isolation_not_asserted")` — so every ESCAPE basis carries a typed authority (no null-escape exception, no un-constructible envelope).

### 3.8 `executed_trusted_record` is NON-ADVANCING for BOTH lens and signoff (OQ2 + P0-1)

Both the lens `executed` basis and the counted-signoff positive rest **entirely on record-self-stored data** (`lens_acks`; the evaluators resolve policy/roster but not the ack's occurrence or sender), so honoring either for release advancement reopens the invisible-GO route the envelope exists to close (0 of N fields independently grounded). r5, adopting D4's ruling and its P0-1 extension:
- Both positives are labelled **`executed_trusted_record`** — honest in the envelope for audit.
- Neither is in ADVANCING; D4's release rule treats them as non-advancing. This is **HARD-CODED, never policy-flagged** — a toggle that accepts ungrounded acks turns evidence-origin integrity into a local waiver and recreates invisible GO.
- Their dependency is a **NEW named dependency: an independent close-ack provenance producer** that grounds **occurrence + sender for BOTH lens satisfaction AND signoff counting** — ONE producer, two consumers, not two dependencies. **Explicitly not #48** (`Thread.opener_message_id`, RFC :5205-5227, which does not authenticate a close ack). Filed distinctly.
- **The producer must WIDEN THE BASIS DOMAIN, not relabel (P2-b).** Because the limitation is domain-level (`executed` is not in the lens/signoff basis domain *by construction*), the producer adds a new *grounded advancing* basis for a validated ack; relabelling `executed_trusted_record` to `executed` without grounding would re-open the exact hole. Until the producer lands, `§5.3` HOLDs any release resting on an ack.
- **Roster/sender resolution shape (OQ2):** `_ack_authorized`'s purity is currently bought with self-stored fields (`close.py:391-394`), so grounding the ack collides with that contract. The natural shape is the **`signoff_eval` impure→pure bridge applied to acks**: resolve the ack's occurrence+sender at the impure boundary (bus lookup), mint a grounded typed ack result, and pass it into the pure basis decision — mirroring how `signoff_eval` already resolves policy/roster impurely and hands a pure result forward.
- The residual is **tested as present on BOTH paths** (§8), not hidden.

### 3.9 Fail-closed

`_basis_for` returning no determinable basis ⇒ `unknown` ⇒ `rollup` `indeterminate` ⇒ release-blocking consumer HOLDs (§5.3). `unknown` is never upgraded.

## 4. Persistence — immutable close-owned sidecar + manifest (B6, Q3 close-owned)

- The envelope **and an expected-dimension manifest** (the committed `(dimension_id, target)` pairs for this record + their sha256) are written to an **immutable, append-only, close-owned sidecar**, keyed by **`(close_id, instance_id, revision, publication_id)`**, **write-once = exclusive-create + reject-overwrite in the close API**. The record carries `provenance_ref = {publication_id, sha256}`.
- **Write order:** sidecar entry first, then atomically commit the record pointer. Orphan sidecar entry (no pointer) is harmless; a published record whose pointer resolves to no sidecar entry **fails closed** (§5.4). Survives `replace_close`/`reopen` (the sidecar is never rewritten).

### 4.1 Sidecar absence is TYPED (B6 + §7)
Concrete public types: **`ProvenanceNotFound`** (entry never written → *established* absence) vs **`ProvenanceUnreadable`** (present-but-corrupt/unreadable → *unknown*, retry may clear). `CloseNotFound` (§7) covers the record; these cover the sidecar.

### 4.2 Public typed validator + atomic current-reader + exported read model (B6 + B2 TOCTOU + OQ1)
Close-owned public API the consumer MUST use (no re-derivation):

```python
def read_current_provenance(store, close_id) -> CurrentProvenance   # ATOMIC: record + pointed sidecar under the close lock
def validate_provenance(cp: CurrentProvenance) -> ValidationResult   # any failure ⇒ invalid ⇒ HOLD

@dataclass(frozen=True)                       # OQ1: the EXPORTED read model — the type crosses the boundary, the minting path does not
class ProvenanceView:
    publication: dict                         # {close_id, instance_id, revision, publication_id}
    dimensions: tuple[Dimension, ...]         # validated per-dimension bases + targets (D4 reads basis/target per §5.5)
    all_required_advance: bool                # close-owned accessor: every required dimension basis in ADVANCING
    rollup: str
```

`read_current_provenance` returns the record **and** its pointed sidecar entry read together **under the close lock** — no consumer-side TOCTOU. `validate_provenance` enforces, any failure ⇒ **invalid ⇒ HOLD**:
1. `schema_version` understood.
2. dimension set **exact/unique/exhaustive** vs the **persisted manifest's committed `(id, target)` pairs** (NOT derived from the envelope under check, NOT re-read from current gates) — so a deleted `gate:<name>` dimension **or a swapped `target` with the id set unchanged** (P1-c) is caught.
3. every dimension's target matches its committed manifest pair.
4. **per-dimension basis domain (P1-b):** each dimension's `basis` is in the exact domain declared for its family in §3.2 (e.g. `lens:*` ∈ {executed_trusted_record, ack_override, unknown}; `signoff:*` ∈ {executed_trusted_record, skipped_override, counted_ack_override, unknown}); a family/basis cross-domain pairing ⇒ invalid.
5. **basis ↔ authority pairing** (§3.4/§3.7), `Authority.kind` is a valid `AuthorityKind`, and `source_binding` matches the publication tuple.
6. the publication tuple matches the record; the pointer digest matches the sidecar entry.
7. `rollup()` **recomputed** (§3.5) equals any stored value.

**OQ1 boundary:** `ProvenanceView`, `Dimension`, `Basis`, `ADVANCING`, `GO_PERMITTING` are **exported** (D4 reads basis/target and the advancing decision it needs, without re-deriving §5.1's forbidden re-derivation). The `BranchOutcome`/`EvalResult` **construction and minting path stays close-internal** (§3.6 origin constraint). Exporting the read model is not exporting the authority.

## 5. Consumer contract (normative — for D4 / any gate reader)

> The envelope binds **one publication** at determination time and is **true evidence about that publication forever**. It is **never provenance for a different publication.**

### 5.1 Read only via the atomic current-reader
A consumer obtains provenance **only** from `read_current_provenance` + `validate_provenance` → `ProvenanceView` (§4.2) — never a hand-loaded record/sidecar. The single consistency boundary that closes the TOCTOU.

### 5.2 The release predicate is D4's TOTAL determination (B3, tri-state total, + P1-d/P1-e + policy-surface)
"Is this item release-blocking?" = D4's total `release_blocking`, from **two axes**:
- **close-scope axis — TOTAL, never raises (P1-e):** `close.classify_release(record) -> "release" | "non_release" | "unknown"`. It returns `release` iff `scope ∈ RELEASE_CLASS_SCOPES` (`{release, milestone, feature, hotfix}`), `non_release` iff `scope ∈ NON_RELEASE_SCOPES` (a **new closed set defined in `close.py`** — its members enumerated at build from the existing non-release scopes; P1-d), and `unknown` otherwise (unparseable/absent/typo ⇒ `unknown ⇒ release-blocking`). Countermodel this closes: `scope="releaze"` is well-formed today (`_is_wellformed` accepts any nonempty scope) — the total tri-state maps it to `unknown`, not `False`. `classify_release` **never raises** for scope classification (a missing *record* is `CloseNotFound`, §7 — orthogonal). §5 consumes the tri-state and **never negates a bool**. Release-class is read from the **record**, never the envelope.
- **policy axis:** `policy_eval` is the spine's **path-matched RELEASE policy** (`release_scoped`/`required_review`), **explicitly NOT `close.py`'s** `validate_signoff_policy`/`load_signoff_policy` (`:501/:645`) — the valid-object-wrong-kind that type-checks and fails silent to permanent HOLD. The spine owns `policy_eval`; `close.py` does not supply it.

`release_blocking` = the fail-closed truth table over (classify_release, policy_eval); absence/unknown on either axis ⇒ release-blocking.

### 5.3 The normative rule (B4)
For a **release-blocking** item, a consumer may advance **iff** a valid envelope for the current publication tuple exists (§4.2) **and every required dimension basis is in `ADVANCING`** (`= {executed}`). **Everything else HOLDs**, explicitly: `executed_trusted_record` (lens AND signoff, §3.8), `mixed`, `discretionary`, `indeterminate`, **absence** (§5.4), **mismatch**, **invalid schema**. Non-release items: the envelope is **advisory** (§5.7).

### 5.4 Absence / mismatch (established ⇒ HOLD) + D4 disposition mapping (OQ4)
Release-blocking GO requires a valid current-publication envelope. Stated **forward** (r2 had it backwards): requiring the envelope makes deleting the sidecar flip **GO→HOLD**, the safe direction. The **bounded stable disposition mapping** D4 implements (concrete problem-code names carried in the spine's RFC; the dispositions are fixed here):

| condition | disposition | D4 action |
|---|---|---|
| `ProvenanceNotFound` (§4.1) | **established absence** | HOLD |
| `CloseNotFound` (§7) | **established absence** | HOLD |
| publication-tuple **mismatch** (incl. null `instance_id` — never matches) | **established** | HOLD (a retry can never clear a mismatch) |
| `validate_provenance` **invalid** (any check in §4.2) | **established** | HOLD |
| `ProvenanceUnreadable` / could-not-read | **unknown** | HOLD now, retry may clear |

### 5.5 The gate-waiver fact has ONE authoritative producer (Finding B, OQ3 confirmed 3/3)
For "was this gate green-on-merit vs waived?", the envelope's `gate:<name>` dimensions are **authoritative**; the D4/work consumer reads THEM and does **not** also keep an independent `gate_check` scan for that fact. The consumer's own `gate_check` remains only for its *own* live gating.

### 5.6 Republish, not re-provenance (B5 + single-snapshot)
The r2 live re-provenance path is **removed** (epistemically invalid). The only way to clear a mismatch/absence/indeterminate HOLD is to **republish**: a new publication determination, new `publication_id`, computed from **one locked `PublishSnapshot`** (the BranchOutcome bundle used ONCE for both verdict and envelope), appended to the sidecar. A historical publication is never cleared from current state. (See §6 for the migration limit on ack-bearing closes.)

### 5.7 Non-release items
Advisory; absence/mismatch is not a gate concern.

## 6. Migration — HOLD-until-republish, with the ack-bearing limit named (Q3, B5, P2-b)
Envelopes generate at publish for all closes going forward. Pre-existing release-class closes (`instance_id` may be `None`, `:414-416`) **HOLD until republished**. A one-time tool may inventory them or emit forensic `unknown`/`indeterminate` envelopes; it **may NOT invent `executed`** and **cannot clear a HOLD**.

**P2-b — republish clears the HOLD only for closes with no ack-backed required dimension.** Because `executed ∉` the lens/signoff basis domain *by construction* (§3.2), republishing a release-class close that has a required lens or a counted signoff produces a fresh envelope whose ack basis is `executed_trusted_record` — §5.3 HOLDs again. So: **republish clears the migration HOLD only for closes whose required dimensions are all non-ack (gate/worktree/blocker); lens/signoff-bearing closes remain held until the close-ack producer lands (§3.8) — the same dependency, not a second one.** A migration that republishes a population and finds the ack-bearing closes still holding now has this documented, not a remedy that silently does not remedy.

## 7. `#39` — close-only not-found split (SHIPS FIRST, coordinated) + reciprocal obligation (Finding E)
§5.4's determinate-absence rule **requires** this type, so §7 lands **before** the consumer contract can be built.
- Add `class CloseNotFound(CloseError)` raised **only** on definitive absence; unreadable/malformed keep raising `CloseError`. **Forbid `str(e).startswith(...)` parsing.** Scope = `close` only; the sidecar has its own `ProvenanceNotFound`/`ProvenanceUnreadable` split (§4.1).
- **RECIPROCAL OBLIGATION (Finding E), as a COORDINATED CHANGE:** the instant `CloseNotFound` lands, the spine's close-resolver disposition (their RFC **row 17**, which currently emits `unreadable` for a genuinely-absent close) **must be re-derived in the same coordinated change** — otherwise the spine reintroduces the established-absence→unknown inversion §7 exists to prevent. Until `CloseNotFound` lands, the spine's current disposition stands.

## 8. Test plan
- **Verdict equivalence (necessary, NOT sufficient):** the BranchOutcome reducer produces the **identical** GO/HOLD + hold-code set as today's `compute_verdict` across the existing verdict corpus. **This alone does not gate the refactor** (P0-2) — it must be paired with the structural guards below.
- **Data-model correctness (P0-2, gates the reducer):** every verdict branch — including the escape-less HARD holds (`malformed_state`, `revision_dirty_or_unresolved`, `publish_not_allowed`, `accepted_counter_missing_remediation`, `invalid_signoff_policy`, `unmapped_required_risk`, `stale_signoff_route`) — emits a `BranchOutcome` and is reduced; a required lens emitting **simultaneous** `unauthorized + stale + undecided-counter` holds is represented (multi-hold payload).
- **Sole-hold-writer (source-shape guard):** no `HOLD_*` is produced anywhere except by appending a `BranchOutcome` reduced by `compute_verdict`; a **freehand-hold mutant** (a hold written outside the reducer) is caught; a **deleted-branch-emitter mutant** (a branch that stops appending its outcome) is caught (this is the omission r3's subset-comparison could not see).
- **GO_PERMITTING / release asymmetry (P1-a):** the reducer uses `GO_PERMITTING`; a lens/signoff-bearing close is GO on the verdict axis but does NOT advance on the release axis; the corpus **MUST contain a release-blocking close with a required lens** (else the equivalence test cannot catch the `ADVANCING∪ESCAPE` mutant) **and a case with simultaneous holds on one lens**.
- **Signoff grounding (P0-1):** `test_signoff_go_with_two_distinct_candidates` — the counted-signoff dimension is `executed_trusted_record` and **does NOT advance**; an ungrounded ack routed through `signoff:<set_id>` reaches the same non-advancing disposition as through `lens:<id>`; residual **tested as present on BOTH paths** (§3.8).
- **Non-circularity (§3.6):** `_basis_for` accepts only `EvalResult`; a record self-asserted field is a type error; call-graph test shows no record/closure route.
- **BasisClass/rollup (§3.4/3.5):** `"verified"` is gone; an all-`executed` release advances; `executed_trusted_record` (lens+signoff) does NOT advance; rollup precedence has no double-match; **empty set ⇒ indeterminate (P2-a: proven not shadowed by the all-ESCAPE case)**.
- **Validator (§4.2):** deleted dimension vs manifest ⇒ invalid; **swapped target with id set unchanged ⇒ invalid (P1-c)**; **cross-domain basis injection (`lens:x + executed`) ⇒ invalid (P1-b)**; `Authority.kind` not in `AuthorityKind` ⇒ invalid; `source_binding` ≠ publication tuple ⇒ invalid; stored rollup ≠ recomputed ⇒ invalid; `not_asserted` envelope is CONSTRUCTIBLE and valid; pointer digest ≠ sidecar ⇒ invalid.
- **B2/TOCTOU:** publish→tuple T1; reopen+republish→new publication_id T2; stored T1 vs current T2 ⇒ mismatch/HOLD; the atomic reader returns record+sidecar consistently under a concurrent republish.
- **B3 tri-state (P1-d/P1-e):** `scope="releaze"` ⇒ `unknown` ⇒ release-blocking (no raise); a **named `NON_RELEASE_SCOPES` member ⇒ `non_release`** (arm is constructible); `classify_release` is total (property test: never raises over arbitrary well-formed scopes); wiring `load_signoff_policy` as `policy_eval` is rejected/typed.
- **Consumer + disposition mapping (OQ4):** advisory-scope + release_scoped policy ⇒ HOLD; each row of the §5.4 table maps to its stated disposition; republish (not re-provenance) clears a **non-ack** HOLD but an **ack-bearing** close stays held (P2-b); null instance never matches.
- **§7:** absent ⇒ `CloseNotFound`; unreadable ⇒ `CloseError`; no-startswith test; a note/test referencing the row-17 reciprocal re-derivation as coordinated.
- **Export boundary (OQ1):** `ProvenanceView`/`Dimension`/`Basis`/`ADVANCING`/`GO_PERMITTING` are importable; the `BranchOutcome`/minting path is not reachable from outside `close.py`.

## 9. Open questions / confirmations for panel round 5
1. **P0-2 data model:** does the `BranchOutcome` (verdict relevance) + optional `Dimension` (dimension eligibility) split, with the reducer as sole hold writer and completeness asserted only over dimension-bearing outcomes, match D4's read needs? (I believe it does: D4 consumes `ProvenanceView`, never `BranchOutcome`.)
2. **P1-d `NON_RELEASE_SCOPES`:** r5 defines a closed non-release set in `close.py` enumerated at build. Does D4 want that vocabulary frozen in this spec, or is "closed set, members enumerated at build + a test that the arm is constructible" sufficient?
3. **§3.8 close-ack producer:** confirm the ONE-producer-two-consumers grounding (lens + signoff) via the `signoff_eval` impure→pure bridge, filed distinct from #48, is the shape D4 expects — and that it **must precede** any advancing ack.
4. **OQ4 disposition mapping:** the §5.4 table fixes the dispositions; the concrete D4 problem-code names live in your RFC. Confirm you will carry that mapping and that the five conditions are complete.
5. Anything in §5 (esp. the total `classify_release` and the exported `ProvenanceView`) D4 still cannot honor.

**Build order (panel-ratified sequencing):** §7 `CloseNotFound` lands FIRST, coordinated with the RFC row-17 re-derivation. **The §3.1 outcome/dimension data model is corrected BEFORE the reducer refactor begins.** The verdict-equivalence test + the sole-hold-writer source-shape guard + the freehand-hold and deleted-branch-emitter mutation tests gate the reducer together. The independent close-ack producer precedes any claim that an ack-backed release dimension can advance. Nothing changes an existing `compute_verdict` outcome.
