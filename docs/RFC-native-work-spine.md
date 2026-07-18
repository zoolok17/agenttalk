# RFC: native work and evidence spine

Status: draft
Date: 2026-07-18
Related: `docs/ROADMAP.md` §4 / §6 Q1 3–7 / §7 / §8, `docs/ISSUES.md`
(P1 PLANNED 2026-07-08; C1, C2, C5), `docs/DESIGN.md` D-5, D-11, D-13, D-16,
`docs/WORK-PACKAGE-native-work-spine.md`

## Summary

agenttalk already owns every primitive a delivery record needs — domains,
lanes, gates, closes, review threads, knowledge, onboarding — and binds
none of them together. `agenttalk work` should be that binding and
nothing more. It is a **link-and-project** layer: it stores the
associations between existing records and derives a verdict over them.
It never stores a second copy of a truth another module owns.

Recommended D1–D4 scope:

1. Per-item work records at `.agenttalk/work/items/<work_id>.json` with an
   append-only event ledger at `.agenttalk/work/events/<work_id>.jsonl`.
   One corrupt item blocks that item and nothing else.
2. Write-once evidence artifacts at `.agenttalk/artifacts/<artifact_id>.json`
   (+ `.log`), each bound to the exact inputs that produced it. Corrections
   create a new artifact; nothing is ever mutated in place.
3. A derived `trust_tier` that is a **mapping over** the four evidence
   vocabularies this repo already has, not a fifth parallel one.
4. A pure `work.compute_verdict` returning `GO` / `HOLD` / `UNKNOWN` with
   stable string HOLD codes, matching the `close` / `lanes` verdict shape.
5. An optional `.agenttalk/code-policy.json` whose required checks are
   matched by glob with **all-matching** semantics (D-11), evaluated
   against the merge-base policy so a branch cannot rewrite the rules that
   judge it.

Recommended later scope:

1. Assurance ingestion as a read-only evidence source (D5).
2. CI adapters, a generic quality runner, and the dashboard Work view —
   each blocked until the schema and check semantics are proven.

Important constraint, stated up front: this design produces
**coordination evidence, not proof**. Everything under `.agenttalk/` is
writable by any process running as the same OS user, so a hostile local
writer can forge a work item, an event, or an artifact. Per DESIGN.md
principle 4, the spine makes the right thing easy and the wrong thing
visible; it is not an authorization boundary. Every claim below about
"binding" and "immutability" means *machine-checkable for a trusted
team*, and the Threat Model section says exactly where that stops.

## Current Model

What already exists, and who owns what. Getting this list right is the
whole point — every duplicated field here would be a future
contradiction.

- **`domains.py`** owns path/area ownership: one unified registry
  (`.agenttalk/domains.json`) shared by lanes and knowledge (D-5), with
  `registry_hash`, `normalize_repo_path`, `glob_matches`, and
  `check_paths`. It survives `reset`.
- **`lanes.py`** owns worktree isolation and delivery. Active
  coordination state is `.agenttalk/state/lanes.json` — **cleared by
  `reset`** — while the durable committed delivery artifact lives at
  `.agenttalk/lane-deliveries/` and survives. `compute_verdict` is pure
  and returns `{"verdict", "holds": [{code, detail}], "ok"}` with 15
  stable string HOLD codes. Delivery artifacts carry
  `isolation_status` ∈ `{verified, advisory_unisolated, unverified}` plus
  an HMAC integrity token.
- **`gates.py`** owns assurance gate state (`.agenttalk/gates.json`):
  `VALID_STATUSES` = `{red, green, unknown, skipped, waived}`,
  `VALID_SEVERITIES` = `{blocker, warn, info}`,
  `VALID_EVIDENCE_SOURCES` = `{automation_ci, local_command,
  manual_review, operator_waiver}`, and the subset
  `BLOCKER_GREEN_SOURCES` = `{automation_ci, operator_waiver}`.
  `check_gates` returns a **different** shape from close/lanes —
  `{schema_version, verdict, required_gates, blockers, gates}` — a fact
  the Work Check section handles explicitly rather than papering over.
- **`close.py`** owns milestone/release closes under `.agenttalk/closes/`:
  sign-off policy, lens acks, counters, and a pure `compute_verdict`
  with 16 stable HOLD codes. Its docstring records the pure-core
  discipline this RFC copies: the CLI resolves all I/O into a
  `signoff_eval` bundle and the pure function only counts.
- **`knowledge.py`** owns durable team memory and the staleness
  vocabulary this RFC reuses: 13 hard `STALE_*` reasons and 6
  `CAUTION_*` flags, folded by a pure `compute_staleness` into
  `{stale_reasons, caution_flags, hard_stale}`. Authority is
  `AUTH_*` ∈ `{uncurated, verified, lead_override, retracted}`.
- **`onboarding.py`** owns comprehension runs, with
  `CLAIM_SOURCES` = `{code, docs, test, command, human, ci, runtime}` and
  `CONFIDENCE_LEVELS` = `{low, medium, high}`.
- **`assurance.py`** owns scan execution and per-dimension attestation
  `{"GOOD": …, "ROBUST": …, "SECURE": …, "reasons": [...]}` where each
  dimension is `good` / `unknown` / `not_assessed`.

Two facts about the current code shape the design more than anything in
the roadmap. First, `_atomic.write_text` is temp+fsync+`os.replace` with
a **latched Windows sandbox-direct fallback that is not crash-atomic** —
so any file this feature writes needs a validate-on-read guard, not a
trust-the-write assumption. Second, `_jsonl.append_record` explicitly
does **not** serialize writers; its docstring says "the caller owns
inter-process serialization," and `onboarding.append_event` wraps it in
`store._config_lock()`. Work events follow that same pattern.

There is no `trust_tier` symbol anywhere in the repo today. That
vocabulary is genuinely new, which is exactly why it must be defined as a
projection over the four above rather than a fifth competing one.

## Goals

- Bind domain, lane, work item, quality evidence, review evidence, gate
  state, and close state into one durable record that can be inspected
  and checked as a unit.
- Make status a **projection** over typed records. A projection that
  cannot reconcile its sources reports the disagreement; it never picks a
  side and never renders blank-or-green.
- Bind every piece of evidence to the exact inputs that produced it, so
  "the tests passed" is always answerable with "at which revision, under
  which policy, run by whom, at what tier."
- Fail closed everywhere a verdict is computed: absent, corrupt,
  torn, mis-scoped, skipped, and unknown all block a blocking
  requirement. None of them read as pass.
- Keep the core domain-neutral. Core validates shape, IDs, references,
  freshness, hashes, transitions, and HOLD codes; the project owns which
  checks are required and which tools produce them.
- Stay additive. Existing stores must load unchanged, and a store that
  has never run `work` must behave exactly as it does today.

## Non-Goals

Quoted verbatim from `docs/ISSUES.md` (P1 PLANNED, 2026-07-08), whose
deferral list is this section:

> Defer arbitrary third-party command execution, CI adapters, merge
> automation, and broad dashboard actions until the schema and check
> semantics are proven.

Additionally, out of scope for D1–D5:

- Do not build a project-management state machine. The lifecycle in this
  RFC is deliberately narrow and adds no priorities, sprints, estimates,
  dependencies, or assignees beyond a single owner.
- Do not duplicate lane, gate, close, domain, or knowledge truth into the
  work record. Work stores references and derives; it does not mirror.
- Do not let `work` mutate any record another module owns. It reads
  lanes, gates, closes, domains, threads, and onboarding through their
  public read APIs and writes only under `.agenttalk/work/` and
  `.agenttalk/artifacts/`.
- Do not add a new message kind. Review binding rides existing
  `review-request` / `review-result` threads, which old clients already
  understand.
- Do not treat `assurance.py` as authority. It is a producer whose output
  is ingested read-only, and roadmap §6 item 7 forbids the alternative.
- Do not claim malicious-peer safety from any hash or binding described
  here.

## Source-Of-Truth Boundaries

For each primitive, exactly one of: work **links** it (stores a reference
and reads through the owner), work **projects** it (derives a display
value that is recomputed every read and never persisted as authority), or
work **owns** it (work is the only writer).

| Primitive | Relationship | What work persists | Who remains authoritative |
|---|---|---|---|
| Domain / path scope | links | `domain_id`, `registry_hash_at_bind` | `domains.json` |
| Lane / worktree | links | `lane_id`, `delivery_artifact_path` | `lanes.py` + the durable delivery artifact |
| Gate state | links | `gate_scope` | `gates.json` via `check_gates` |
| Close | links | `close_id` | `.agenttalk/closes/` |
| Review | links | `request_id` of the review thread | bus messages + `threads.derive_threads` |
| Onboarding run | links | `run_id` | `.agenttalk/onboarding/` |
| Knowledge notes | links | `note_id[]` | `knowledge/notes.jsonl` |
| Project policy | links | `policy_hash_at_open` (witness) | `.agenttalk/code-policy.json` at the merge base |
| Review lenses | links | nothing | `close.py` `required_lenses` / `lens_acks` |
| Requirement satisfaction | **owns** | satisfaction records (matched glob) | work — but as a cache, never authority |
| Work item identity, owner, base/head refs, links | **owns** | the item record | work |
| Work events | **owns** | the append-only ledger | work |
| Evidence artifacts | **owns** | write-once artifact records | work |
| Lifecycle status | **projects** | nothing | derived per read |
| Verdict (`GO`/`HOLD`/`UNKNOWN`) | **projects** | nothing | derived per read |
| Trust tier of an artifact | owns, derived-then-bound | `trust_tier` on the artifact | derived at write, revalidated at verdict |

Rules:

- A field that appears in this table as *links* is stored as an
  identifier only. Work never copies the linked record's contents,
  because a copy is a contradiction waiting to happen (roadmap §8,
  "state-machine drift").
- Work persists **three drift witnesses**, and each is disclosed here
  with the rule that reads it. A witness is a record of *which version of
  someone else's truth we bound against*, used only to detect drift; none
  is ever read as the current answer. An undisclosed witness is just a
  copy, and a witness with no drift rule is dead weight:

  | Witness | Witnesses | Drift rule |
  |---|---|---|
  | `registry_hash_at_bind` | `domains.json` | `work_domain_scope_drift` |
  | `lane_generation` | `lanes.json` | `work_lane_generation_mismatch` |
  | `policy_hash_at_open` | `code-policy.json` | `work_policy_changed_since_open` |

  `isolation_status` on an *artifact* is a fourth copy of lanes-owned
  truth, disclosed in Evidence Tiers: it is frozen at artifact-write time
  by design, because an artifact is an immutable record of the conditions
  under which a check ran.
- `work_policy_changed_since_open` is what anchors `policy_hash_at_open`.
  An earlier draft carried that field with nothing reading it, which
  invites a later phase to invent a meaning for it. It is distinct from
  `work_policy_hash_drift` (artifact-side: the policy moved since the
  *artifact* was produced) — this one says the rules moved since the item
  was opened, which an owner needs to know even when every artifact is
  current.
- Status and verdict are never persisted. `work show` recomputes both on
  every read. There is no `status` field in the item schema, and adding
  one later is a schema break, not an optimization.
- `trust_tier` is derived at artifact-write time, bound into the
  immutable artifact, and **revalidated against a re-resolved
  `producer_class`** when a verdict consumes it. The re-resolution is
  what makes revalidation meaningful: every other tier input
  (`evidence_source`, `exit_code`, binding completeness) is immutable on
  a write-once record, so recomputing over those alone is a tautology
  that returns the same value by construction and could only ever fire on
  tampering `content_hash` already catches. `producer_class` is the one
  input that genuinely moves — an agent retires, or its roster
  classification changes — and freezing it would make the real drift
  invisible. The re-resolved classification arrives in `roster_eval`, not
  from an ambient read.
- **Re-resolution may only WEAKEN. The bound tier CAPS the effective
  tier.** If the re-resolved tier is weaker, the artifact does not count
  and the verdict emits `work_artifact_tier_drift`; if it is stronger,
  the bound tier stands and the stronger value is discarded. A tier
  records the conditions under which a check *ran*, and those cannot
  improve retroactively. Leaving the strengthening direction unspecified
  would have let a **roster text edit** — reclassifying a producer from
  `agent` to `operator` — retroactively upgrade every artifact that
  producer ever wrote from `local_agent` to `local_operator`, with no
  re-execution. That is foreclosed by name elsewhere in this document:
  operator confirmation is not operator execution, and a roster edit is
  strictly easier than clicking confirm. It cannot reach `automation_ci`
  (that is `evidence_source`-driven and immutable), so it is not a
  release-gate bypass — but it would silently satisfy a
  `min_tier: "local_operator"` floor, including the one in this RFC's own
  policy example.
- **A retired producer is a caution, never a demotion.** `producer_class`
  is a closed set with no `retired` member, so re-resolving a departed
  agent matches no mapping row. Neither available answer is acceptable:
  falling to `referenced` would demote every artifact that producer ever
  made — retiring one departing agent would hold every item that depended
  on them, with no record having changed and no repair short of re-running
  every check — and leaving it unmapped would make the "pure total
  function" not total. So retirement raises the caution flag
  `producer_retired` on the artifact and changes no tier. The check still
  ran under the conditions it ran under; who has since left the team says
  nothing about that. This also matches how retired *owners* are already
  treated (a caution, not a silent reassignment) rather than giving
  producers the opposite treatment without saying so.
- This is the C2 lesson (`ISSUES.md`:874–884) only *because* of that
  re-resolution: C2's bypass was a persisted approval whose underlying
  inputs could change while the approval stood. A revalidation over
  frozen inputs would not have been that lesson at all, and citing C2 for
  it would have been borrowing authority the mechanism did not earn.
  What it additionally catches is version skew — a store written by a
  build whose derivation table differs from this one.
- Every source read in a projection is independently guarded, so a
  failing source becomes a bounded `source_error` row naming the source
  and the error rather than an empty list. That guarding *pattern* is
  D-16's (DESIGN.md:618-620, "a bounded `source_error` row, never a blank
  queue"); the additional rule that **a failed source can never yield
  green** is this RFC's own, not D-16's. D-16 governs the operator
  attention queue's completeness, not gate greenness, and citing it for
  the stronger proposition would have dressed a new rule in borrowed
  authority.

## Work Item Schema

`.agenttalk/work/items/<work_id>.json`, one file per item, written with
`_atomic.write_text` under a per-item lock:

```json
{
  "schema_version": 1,
  "work_id": "w-0007",
  "title": "Harden lane delivery readback",
  "created_at": "2026-07-18T09:14:02.113400Z",
  "created_by": "claude-lead",
  "owner": "codex-dev-2",
  "domain_id": "core-lanes",
  "registry_hash_at_bind": "9f2c1b7e4a",
  "scope_globs": ["src/agenttalk/lanes.py", "tests/test_lanes*.py"],
  "base_ref": "master",
  "base_sha": "e0e8f7b4c19a2d6f8b3e5107c9a4d2f6b8e1c3a7",
  "target_ref": "master",
  "lane_id": "lane-lanes-hardening",
  "lane_generation": 3,
  "delivery_artifact_path": null,
  "item_seq": 12,
  "pending_op": null,
  "ledger_head": {"seq": 12, "hash": "sha256:b41f7c09de2a8653"},
  "close_id": null,
  "gate_scope": "feature",
  "review_request_ids": ["q-de4534c8b3c4"],
  "onboarding_run_id": null,
  "note_ids": [],
  "artifact_ids": ["a-3f91c2", "a-77b0de"],
  "policy_hash_at_open": "3b1f0c9d22",
  "terminal": null
}
```

Rules:

- `work_id` is opaque, stable, and validated by the same character class
  as existing IDs. It is never reused, including after abandonment.
- `schema_version` is an integer and is checked **exact-match
  fail-closed** — see Schema Versioning. A record whose version this
  build does not recognize is not upgraded, not guessed at, and not
  skipped; it blocks that item.
- `owner` is a single agent name validated against the roster's known
  agents (active or retired), so an item owned by a retired identity
  still reads. Retired ownership surfaces as a caution, not a silent
  reassignment.
- `base_sha` is a full 40-character SHA, never an abbreviation and never
  a symbolic ref. `base_ref` and `target_ref` are advisory labels; the
  SHA is what evidence binds to.
- `scope_globs` are normalized through `domains.normalize_repo_path` /
  `normalize_glob`, which already reject NUL, absolute, drive-relative,
  UNC, and `..`-escape paths. Work adds no second path normalizer.
- **`scope_globs` must be a subset of the bound domain's paths, checked
  against `domains.check_paths` at bind time and revalidated at verdict
  time.** A mismatch raises `work_scope_outside_domain`. Without this the
  domain link is decorative: an item could bind `domain_id: "core-lanes"`
  with a perfectly fresh registry hash while its `scope_globs` covered
  `src/agenttalk/gates.py`, and no hold would fire —
  `work_domain_scope_drift` only detects that the *registry* moved, never
  that the item's scope ever agreed with it. That is work silently
  re-owning path scope while the boundary table calls it a link, and it
  bypasses the domain's approver model.
- There is no `head_sha` in the item. Head is resolved live from the
  linked lane or the repo at read time, because a persisted head is stale
  the moment it is written and would invite exactly the evidence-rot the
  roadmap warns about (§8).
- There is no `status`. Status is projected.
- `terminal` is `null` for a live item, or one of `delivered`,
  `closed`, `abandoned` with the event id that made it terminal. This is
  the only lifecycle fact stored on the item, and it is stored only
  because "this item is finished" must survive a corrupt event ledger.
  Every non-terminal state is derived.
- `item_seq` is a **record version**, not a ledger position. It counts
  durable *item writes* and serves only as the `expected_version` for
  optimistic concurrency: a mutation states the `item_seq` it read, and a
  write whose expected value no longer matches is refused as a version
  conflict. This closes the C1(2) lost-update analogue at the record
  level, not only at the lock level.
- **`item_seq` and the ledger's `seq` are not commensurate and must never
  be compared.** One mutation performs two item writes (set `pending_op`,
  then clear it) and appends one event, so a clean mutation advances
  `item_seq` by 2 and the ledger by 1. An earlier draft compared them
  directly, which meant every successful mutation looked divergent.
  Reconciliation keys on `pending_op` and `ledger_head` instead:

  > **A settled item is one where `pending_op` is `null` and
  > `ledger_head` equals the ledger's actual last `{seq, hash}`.**

  That relation holds after every completed mutation regardless of how
  many item writes it took, and it is the only relation the crash table
  classifies on.
- `pending_op` is `null` on a settled item, or `{op_id, intended_type}`
  while a mutation is in flight. It is written durably *before* the
  matching event is appended and cleared after, so an interrupted
  transaction is always visible on read. See Crash And Recovery Protocol.
- `lane_generation` pins the lane's immutable generation at bind time. A
  lane whose ID matches but whose generation does not is a *different*
  lane that reused the name, and it raises
  `work_lane_generation_mismatch` rather than reading as the bound one.
- `delivery_artifact_path` points at the validated immutable lane
  delivery artifact once one exists. It is what lets a terminal item stay
  readable after its lane is cleaned up, and the Source-Of-Truth table
  names it, so the schema carries it rather than leaving the table and
  the schema disagreeing about what work stores.
- `ledger_head` is `{seq, hash}` of the **current** last event, written
  in the same locked transaction as the event itself. `prev_hash` chains
  each record to its predecessor, which commits every event *except the
  last one* — the tail's `actor`, `ts`, `type`, and payload could be
  rewritten without breaking any chain link. Committing the head on the
  item closes that: the tail is covered by a record outside the ledger.
  **`ledger_head` is validated on every item read** — the same
  every-consumption rule the artifact pair carries — because a head
  checked once at recovery is a cache, and a cache is not authority.
  It does **not** reject a semantic replay: a replay appended by a
  legitimate writer advances `ledger_head` too, and only the three named
  per-type preconditions reject the replays that are decidable at all.
  The honest limit: this is unkeyed, so it detects rewriting, not a
  writer who updates both files coherently — the same trusted-team
  boundary as `content_hash`, and explicitly weaker than the HMAC token
  `lanes.py` uses for delivery artifacts.
- **There is no `release_blocking` field.** Whether an item is
  release-blocking is **derived** per The Release Baseline and never
  stored. An earlier draft carried it as a persisted boolean while the
  prose said "derived" — so an author writing `release_blocking: false`
  would have switched the entire Release Baseline off. That is the
  round-2 pathology exactly (update the prose, leave the schema), and it
  is a live fail-open, not a documentation slip.
- A legacy record carrying a stored `release_blocking` value has it
  **ignored, not honoured**. Ignoring is the only safe reading: an
  authority field that was never authoritative must not become one by
  surviving a migration.
- Reading an item is fail-safe per item. A malformed
  `items/<work_id>.json` yields a bounded error record for that
  `work_id`; `work list` still returns every other item. This is the
  roadmap's "one corrupt item must not brick all work," and it is
  asserted by a test that writes literal garbage into one item file.

### Corrupt Items Block Their Own Mutation

This is C1 (`ISSUES.md`:864–873) restated for work. In `gates.py`, `set`
and `waive` silently overwrote a corrupt `gates.json`, discarding the
corruption HOLD.

- A work item that fails to load blocks every mutation of **that item**.
  `work assign`, `work start`, `work deliver`, and `work abandon` all
  refuse with a non-zero exit and a repair hint.
- The corrupt file is never overwritten, never repaired implicitly, and
  never replaced by a fresh record under the same `work_id`. Recovery is
  an explicit operator action that moves the file aside.
- Every read-modify-write of a work item holds a per-item cross-process
  lock for the whole sequence — load, validate, decide, write the item,
  append the event, fsync — not just the write. An unlocked RMW was C1's
  second fail-open. The lock is backed by `item_seq` as an
  `expected_version`, so a writer that somehow proceeds without the lock
  still loses cleanly to a version conflict instead of silently
  overwriting.
- The artifact analogue holds too: an `artifact_id` whose existing JSON
  is **unreadable** is never repaired in place and never reused. A
  registration against a corrupt existing artifact refuses; it does not
  treat the unreadable file as absent and write over it. That was C1(1)
  in a different costume.
- The honest limit: the lock is a coordination lock, not a security
  boundary (DESIGN.md principle 4). It prevents concurrent honest writers
  from losing an update; it does not stop a writer who ignores it.

## Event Model

`.agenttalk/work/events/<work_id>.jsonl`, append-only, one JSON record
per line, appended through `_jsonl.append_record` **inside**
`store._config_lock()` — the `onboarding.append_event` pattern, because
`append_record` does not serialize writers on its own.

```json
{
  "schema_version": 1,
  "event_id": "we-20260718-091402-113400-KQ3x",
  "work_id": "w-0007",
  "seq": 12,
  "prev_hash": "sha256:7c1a0e93bb42f508",
  "op_id": "op-4a91c07e",
  "ts": "2026-07-18T09:14:02.113400Z",
  "actor": "codex-dev-2",
  "type": "artifact_attached",
  "artifact_id": "a-3f91c2",
  "head_sha": "b8e1c3a70f2d4916ac53b7e08d1f6a2c94e7d305",
  "note": "ruff + pytest on 3.10"
}
```

Rules:

- Every persisted event field is classified **bound** xor
  **curation-mutable**, and both sets are pinned by a construction test
  that fails when a new field is added to neither set. This is the
  provenance discipline from the v0.76.1 knowledge work
  (`ISSUES.md`:84–89): the integrity boundary is complete-by-construction,
  not a hand-maintained list that drifts.
- **Bound** (immutable, folded into the event's identity — and the
  canonical event hash covers **every** field in this list):
  `schema_version`, `event_id`, `work_id`, `seq`, `prev_hash`, `op_id`,
  `ts`, `actor`, `type`, `from_state`, `to_state`, **plus every per-type
  payload field**: `title`, `domain_id`, `owner`, `lane_id`,
  `lane_generation`, `artifact_id`, `head_sha`, `request_id`,
  `reviewed_head_sha`, `asserted_state`, `delivery_artifact_path`,
  `reason`, `supersedes_event_id`. A later event can never rewrite these
  on an earlier event.
  An earlier draft declared the bound set before the per-type schemas
  existed and then added those schemas without extending it, classifying
  only `artifact_id` and `head_sha`. The construction test could not then
  pin both the partition and the schemas, and an `owner`, `lane_id`, or
  `request_id` payload could have been changed without altering the event
  hash or `ledger_head` — provenance forgery inside a record the design
  calls immutable.
- **Curation-mutable** (a later authorized event may supersede the
  projected value): `note`, `labels`. Nothing that feeds a verdict is
  curation-mutable, which is the point — a forged curation event must not
  be able to move a gate.
- `actor` and `ts` are bound specifically because forging attribution and
  creation time was a reproduced attack in the knowledge work, where it
  had poisoned `roster --expertise` and the dashboard.
- Event types are a small closed vocabulary, and **each carries a
  required payload** — a type with a missing or extra payload field is a
  malformed line, not a tolerated variant:

  | `type` | Required payload | Who may drive |
  |---|---|---|
  | `created` | `title`, `domain_id` | any rostered agent |
  | `assigned` | `owner` | owner or lead |
  | `started` | — | owner |
  | `lane_bound` | `lane_id`, `lane_generation` | owner |
  | `artifact_attached` | `artifact_id`, `head_sha` | producer or owner |
  | `review_requested` | `request_id` | owner |
  | `review_recorded` | `request_id`, `reviewed_head_sha` | owner |
  | `state_asserted` | `asserted_state` | any rostered agent (advisory) |
  | `delivered` | `delivery_artifact_path` | owner or lead |
  | `abandoned` | `reason` | owner or lead |
  | `reopened` | `reason`, `supersedes_event_id` | lead only |

  An earlier draft named the types and classified their fields as bound
  xor curation-mutable, but never said what each type must *contain* —
  so `assigned` had no owner, `lane_bound` no lane, and `reopened` no
  authority rule. The classification test passed anyway, because it
  tested field classification rather than per-type completeness.
- `op_id` is **unique across the ledger**. A replayed event re-encoded
  with a fresh `op_id` still fails against `ledger_head`, and one
  re-encoded with the original `op_id` is rejected as a duplicate.
- The construction test is extended accordingly: it pins the bound xor
  curation-mutable partition **and** the per-type required payload above,
  failing when a new type or field is added to neither.
- **`seq` orders the ledger; timestamps never do.** `seq` is a
  gap-free monotonic integer per `work_id`, and `prev_hash` chains each
  record to its predecessor's canonical hash. `event_id` retains a
  timestamp prefix for human readability and is *never* a sort key.
  Anything in a projection that reaches for "the latest" means highest
  valid `seq`, not newest `ts`.
- A malformed physical line is **isolated**, surfaced, and skipped, and
  later valid records are retained — the existing JSONL reader contract
  (DESIGN.md principle 3). It is never silently dropped: the projection
  carries a `ledger_problems` count, and a non-zero count blocks a
  `GO` verdict rather than degrading quietly.
- Skipping a bad line preserves it for diagnostics but must **not** let
  later records take effect across the hole. A `seq` gap or a broken
  `prev_hash` link raises `work_event_chain_broken`, and transitions
  after the break are not applied. Reading past a torn transition to find
  a later `ready` is precisely the fail-open this rule exists to stop.
- **The per-ledger lock spans the whole append**, not just the write
  call. `_jsonl.append_record` is not one-record-atomic on its own and
  its docstring says the caller owns serialization: internally it
  `lseek`s to inspect the trailing byte, may issue a *separate*
  `_write_all(fd, b"\n")` to repair a missing delimiter, and then writes
  the payload through a `while remaining:` loop over `os.write`
  (`_jsonl.py:23-29`, `:66-74`). `O_APPEND` makes each individual
  `os.write` atomic; it does not make one logical record atomic. Two
  unserialized writers can therefore both observe the same unterminated
  tail, or interleave chunks of two payloads. Work holds
  `store._config_lock()` across tail inspection, `seq` allocation, the
  append, and the `fsync` — the `onboarding.append_event` pattern,
  applied deliberately rather than by imitation.
- Events are evidence of what happened, not authority over what is true.
  A `state_asserted` event records that an actor claimed a state; the
  projection still derives status from records. An event cannot assert an
  item into `ready`.

### Crash And Recovery Protocol

Two files cannot be updated atomically together, so the design names
which side is authoritative **per question** and makes the other
reconstructible. Guessing at recovery time is what produces split-brain.

- **The item is authoritative for identity and current state** — what
  this item is, who owns it, what it is bound to, whether it is terminal.
- **The ledger is authoritative for history** — how it got there, who
  acted, and in what order.
- **A `GO` requires both to agree.** The item answers "what," the ledger
  answers "how we got here," and a divergence between them is non-GO in
  either direction. The item being authoritative for state does *not*
  mean an unaudited state advances; it means the item wins the question
  of what state we are in, while the ledger's silence still blocks.

One mutation publishes **exactly three item images** and one ledger
append, in this order:

| # | Write | Item image |
|---|---|---|
| I0 | (pre-existing) | settled: `pending_op` null, `ledger_head` = current tail |
| I1 | item write 1 | `pending_op` = `{op_id, intended_type}`, `ledger_head` **unchanged** |
| — | ledger append | the event, under the per-ledger lock |
| I2 | item write 2 | `pending_op` null, `ledger_head` **advanced** to the new tail |

`ledger_head` advances only in I2, together with clearing `pending_op`,
so those two facts move as a unit and the settled relation is restored
atomically from a reader's perspective. Recovery is idempotent by
`op_id`: re-appending an event whose `op_id` already exists is a no-op.

Classification keys on **`pending_op` and `ledger_head`** — never on
`item_seq`, which is a record version and is not commensurate with ledger
`seq`:

| Crash point | Observed | Outcome |
|---|---|---|
| (a) after I1, before append | `pending_op` set; no matching `op_id` in ledger | Recovery re-appends the event from `pending_op` exactly once, then publishes I2. If the payload cannot be reconstructed, `work_ledger_gap`; the state is not audited and does not count for GO. |
| (b) after append, before I2 | `pending_op` set; matching `op_id` **present**; `ledger_head` stale | Recovery publishes I2 only — clears `pending_op`, advances `ledger_head`. No re-append. This is the distinct case an earlier draft muddled by calling it "case (a) with the event present," which contradicted (a)'s own definition of *no matching event*. |
| (c) ledger tail ahead of `ledger_head`, `pending_op` null | an event exists that no item image commits | `work_ledger_ahead`; HOLD. Not applied — replaying history onto the state-authoritative record is the silent rebinding this design forbids. |
| (d) `ledger_head` names a tail the ledger does not have | head/tail mismatch | `work_ledger_head_mismatch`; HOLD. Covers a rewritten or truncated tail whose internal `prev_hash` chain still verifies. |
| (e) orphan `op_id` with no item | event references an unknown `work_id` | `work_ledger_orphan`; never materializes an item. Unreachable through the normal path, so it means a hand-edited or externally-written ledger. |
| (f) neither published | settled relation holds | Clean no-op. Nothing happened. |

Rules:

- Every outcome above is deterministic and converges on repeated reads.
  Two agents recovering the same interrupted transaction reach the same
  state or the same named HOLD; neither invents a third.
- Rows (a) and (b) are the two halves of "the clearing write can itself
  crash," and they are distinguished by whether the `op_id` is already in
  the ledger — which is exactly the observation that makes recovery
  idempotent rather than looping.
- **`work check` run BEFORE recovery holds with `work_recovery_required`**
  (class `established`). Rows (a) and (b) are states a verdict can
  observe, not merely inputs to a repair command: `pending_op` is set,
  recovery has not run, and no other code covers it —
  `work_ledger_gap` requires an unreconstructible payload,
  `work_ledger_ahead` requires `pending_op` null, and
  `work_ledger_head_mismatch` requires a head naming an absent tail. Left
  uncoded, an implementation driven by the HOLD table would have no
  branch and return GO on an item mid-transaction.
- **`ledger_head` commits a tail; it does not reject a semantic replay,
  and neither does anything else in general.** A replay appended through
  a legitimate writer also advances `ledger_head`, and a hash commitment
  cannot distinguish it from an honest new event. Three named
  preconditions reject the replays that are *decidable against the
  specified schema*, and only those: a **duplicate `op_id`**, a
  `lane_bound` for an **already-bound lane**, and a `delivered` on an
  **already-terminal item**. Each is checkable from the current item plus
  the closed per-type payload.
- **Everything else is not rejectable and this document does not claim it
  is.** An earlier draft named "an `assigned` event whose `from` owner
  does not match the current item" — but the closed `assigned` payload is
  `owner` alone, with no `from_owner`, so that check cannot be written
  against this schema. More fundamentally, a genuine re-assignment back
  to a previous owner is *indistinguishable* from a replay of the
  original assignment; `ledger_head` commits both equally. Adding
  pre-state bindings to every event type would make it decidable, and
  that is deliberately not in D1–D5. Safety Invariant #22 states exactly
  this narrowed guarantee, and the two now specify the same thing.
- **Recovery never runs inside `work check`.** It is a separate explicit
  command. A verdict function that repaired state would make the answer
  depend on how many times it had been called (see Work Check purity).
- The honest limit: this converges for *crashes*. It does not defend
  against a writer who edits both files to agree on a false history,
  which is the same trusted-team boundary as everything else here.

## Artifact Schema

`.agenttalk/artifacts/<artifact_id>.json`, write-once, with an optional
bounded `.log` sibling:

```json
{
  "schema_version": 1,
  "artifact_id": "a-3f91c2",
  "work_id": "w-0007",
  "created_at": "2026-07-18T09:31:44.902113Z",
  "ingested_at": "2026-07-18T09:31:45.010882Z",
  "root_execution_id": "x-6b20f4de91c7",
  "derived_from": null,
  "supersedes": null,
  "producer": "codex-dev-2",
  "producer_class": "agent",
  "check_name": "pytest",
  "covered_globs": ["src/agenttalk/**", "tests/**"],
  "command": ["py", "-3.10", "-m", "pytest", "-q"],
  "cwd": "worktrees/lane-lanes-hardening",
  "exit_code": 0,
  "started_at": "2026-07-18T09:29:02.441077Z",
  "ended_at": "2026-07-18T09:31:44.180255Z",
  "base_sha": "e0e8f7b4c19a2d6f8b3e5107c9a4d2f6b8e1c3a7",
  "head_sha": "b8e1c3a70f2d4916ac53b7e08d1f6a2c94e7d305",
  "diff_hash": "sha256:41d9a0c6f38b2e57",
  "policy_hash": "3b1f0c9d22",
  "evidence_source": "local_command",
  "isolation_status": "verified",
  "trust_tier": "local_agent",
  "log_path": "a-3f91c2.log",
  "log_bytes": 65536,
  "log_hash": "sha256:0b7e14c2aa9f6d30",
  "log_truncated": true,
  "redaction_status": "not_scanned",
  "content_hash": "sha256:9c2f77b104ae3d81"
}
```

Rules:

- An artifact is written once and never modified. There is no update
  path, no status field to flip, and no in-place correction. A wrong
  artifact is superseded by a new artifact carrying `supersedes:
  "<old_id>"`; both remain readable, and the verdict considers only
  artifacts that bind to the current revision.
- Write-once is enforced with **create-if-absent** under the artifact
  lock. Re-registering an existing `artifact_id` is idempotent **only**
  when the incoming content is byte-identical; any difference is a
  collision that refuses and leaves the original bytes untouched. A
  correction that reuses an ID is refused, not merged — reusing an ID is
  how "write once" quietly becomes "write whenever," and every gate
  already holding that ID would silently observe new evidence.
- The honest limit: this is not filesystem immutability. A process with
  write access can overwrite the file, which is why `content_hash` exists
  and why validation is on-read.
- `content_hash` is computed over the canonical serialization of every
  field except itself, so a tampered or torn artifact fails validation on
  read. It detects accidental corruption and casual edits; it does not
  detect an attacker who recomputes it (no secret is involved). Lane
  delivery artifacts already use an HMAC integrity token for the stronger
  property; work artifacts deliberately do not claim it.
- The **exact input binding** is `work_id`, `base_sha`, `head_sha`,
  `diff_hash`, `policy_hash`, `command`, `cwd`, `exit_code`,
  `started_at`/`ended_at`, `producer`, and `trust_tier` — the roadmap §4
  list in full. An artifact missing any of these does not count as
  evidence for a blocking requirement; it is `referenced` at best.
- **A missing binding field is never backfilled from current state.**
  An importer that fills an absent `base_sha`, `head_sha`, `diff_hash`,
  or `policy_hash` from the item's present values manufactures the exact
  provenance the binding exists to prove, and turns a stale or foreign
  artifact into a current one. Absence raises
  `work_artifact_binding_missing`; mismatch raises `work_artifact_stale`.
  Neither is repaired.
- `ingested_at` is when work recorded the artifact and is distinct from
  the producer-reported `started_at`/`ended_at`. Only `ingested_at` is
  observed locally; the other two are producer claims.
- `covered_globs` declares **what the check actually exercised**,
  normalized through `domains.normalize_glob`. Without it there is no way
  to express "a `src/ui/**` artifact," and therefore no way to detect
  that a `src/payments/**` requirement was answered with unrelated
  evidence — `work_artifact_wrong_scope` would be unimplementable and
  its test untestable. A requirement is satisfied only when the
  requirement's glob is covered by the artifact's `covered_globs`.
  The honest limit: `covered_globs` is a producer claim like any other
  field. It makes a *mismatch* detectable; it cannot prove the check
  really exercised those paths.
- **The exact-key join is authoritative: an artifact whose
  `(base_sha, head_sha)` differs from the resolved current revision is
  NOT CURRENT**, and a not-current artifact never satisfies a
  requirement. Evidence binds to an exact commit (roadmap §4: "Evidence
  binds to exact inputs … base SHA, head SHA"; §7 makes unbound evidence
  a Hard HOLD), and `automation_ci` is "automation on exact
  commit/policy" — a CI run attested H1, and no local reasoning converts
  that into an attestation of H2.
- Not-current is not automatically *hard* stale. The
  `{stale_reasons, caution_flags, hard_stale}` shape carries the
  distinction: an ordinary head move is a `stale_reason`, while a
  **content-identical rebase** raises the named caution flag
  `rebased_identical_diff`. Neither satisfies a requirement; the flag
  exists so an operator can tell "this needs a re-run" from "this needs
  a rethink."
- `diff_hash` is over the normalized `base_sha..head_sha` diff, which
  makes it a **diagnostic, never a satisfier**. Equal patch content does
  not prove an equal base tree: a rebase onto a different base yields the
  same diff and a different resulting tree, along with different
  dependency resolution and build inputs. `diff_hash` equality tells an
  operator that a cheap revalidation is likely to pass; it does not
  perform that revalidation.
- The caution/blocking split is explicit. For a **release-blocking**
  requirement a caution is **insufficient**: re-execution at the new
  `(base_sha, head_sha)` pair is required, with no exception. For a
  **non-blocking** requirement, project policy may accept the caution.
  A test result is a property of base *plus* patch, not of the patch: if
  the new base moved a dependency pin, a default, or a signature, the
  identical diff can fail where it passed — ordinary merge skew. That is
  why the caution is a prompt to re-run rather than a substitute for it.
- Project policy **may** permit diff-equivalent reuse, and only in this
  narrow shape: a new artifact bound to H2 is produced by a verifier that
  positively establishes the equivalence, and its `trust_tier` is no
  stronger than that verifier's own tier. That is new evidence about H2,
  not old evidence stretched over it. Reuse is available for non-release
  claims only; a release-blocking `automation_ci` requirement stays stale
  until CI attests H2, because the whole content of that tier is *which
  commit the automation ran on*.
- An earlier draft justified diff-equivalent reuse by analogy to D-6's
  anchor-relative knowledge staleness. **The analogy fails on its own
  terms**, which is a cleaner refutation than the cost argument that
  first replaced it. D-6 rejects HEAD-relative staleness because it would
  empty the knowledge layer on every *unrelated* commit — a rationale
  that requires the anchor to be evaluated against a continuously
  advancing shared HEAD. Work evidence is not HEAD-relative in that
  sense: it binds one specific `base_sha..head_sha` pair for one item,
  and unrelated commits to master do not move it. The noise problem D-6
  solves does not arise here, so its conclusion cannot be imported.
- The underlying difference: a knowledge note's claim is scoped to an
  **anchor**, while a test artifact's claim is scoped to a **build**.
  Anchor-relative freshness is sound for the former and unsound for the
  latter. The cost asymmetry points the same way — a false-stale artifact
  costs one re-run, a false-fresh one costs a false GO — but the scope
  argument is the decisive one.
- `command` is a structured argv list, never a shell string. D1 defines
  the field; D1–D4 do not execute it. Recording what an agent says it ran
  is not the same as running it, and the runner is deliberately deferred
  (roadmap §7: "a runner accepts shell strings by default" is a Hard
  HOLD).
- `log_truncated` being `true` is not a failure, but a truncated log
  cannot be the sole basis for a blocking requirement being satisfied,
  because the truncation may have removed the failure.
- `redaction_status` ∈ `{not_scanned, scanned_clean, scanned_redacted}`.
  D3 ships `not_scanned` honestly rather than claiming a scan that does
  not exist.
- **Existence is never advancement.** C5 (`ISSUES.md`:914–917) records
  the *fix* — `lane deliver` "reads back + shape/verdict-validates the
  delivery artifact before clearing the lane (a HOLD/wrong-schema
  artifact can no longer clear it)". The pre-fix behaviour is implied by
  that text rather than stated in it, so this RFC asserts the rule on its
  own account and cites C5 as the precedent for the remedy. A work item advances only after the artifact is read back and
  shape-validated *and* verdict-validated: schema version exact-match,
  `content_hash` recomputed, required binding fields present, `work_id`
  matching, and `head_sha` matching the current revision. A file that
  exists but fails any of those blocks; it does not advance and it does
  not read as absent.

### Torn Reads And The JSON/Log Pair

`_atomic.write_text` falls back to a direct non-atomic write once the
Windows sandbox latch trips, and its own docstring says "a reader can
catch a half-write." An artifact is also *two* files, which are not
group-atomic. Both are first-class outcomes, not exceptions.

**Write order is log first, metadata last.** The `.log` is written and
hash-validated before the `.json` exists, and publishing the `.json` is
the commit marker that makes the artifact real. A crash therefore leaves
either nothing or an unreferenced log, never a metadata record pointing
at a log that was never finished.

| State | Outcome |
|---|---|
| (a) `.log` only | Orphan. Surfaced, never evidence. Cleanable. |
| (b) `.json` only, log declared | `work_artifact_log_missing` |
| (c) torn `.log` | `log_hash`/`log_bytes` mismatch → `work_artifact_log_hash` |
| (d) torn `.json` | `work_artifact_unreadable`. Never reconstructed by guessing from the log. |
| (e) both valid, no attach event | Valid orphan; not counted. An idempotent attach may adopt it. |
| (f) event references an artifact whose pair is not valid | `work_artifact_unreadable` — a broken reference is non-GO |
| (g) ID exists with different bytes | Immutable collision; refuse, never overwrite |

Rules:

- Both files are validated on **every** consumption, not once at
  registration. A hash checked only at attach time is a cache, and a
  cache is not authority.
- `UNKNOWN` never satisfies a blocking requirement and never crashes the
  projection. Both halves matter: crashing would make one bad artifact
  brick `work check`, and passing would be the fail-open C1 was about.

## Evidence Tiers And Trust Tier

The roadmap tier table, reproduced in full including its trailing default
rule:

| Tier | Meaning | Default gate role |
|---|---|---|
| `referenced` | prose claim or linked output only | Never satisfies required gates |
| `local_agent` | local command run by an agent | Useful pre-review evidence |
| `local_operator` | local command run/confirmed by operator | Strong local evidence, still not CI |
| `automation_ci` | configured automation on exact commit/policy | Default release-authoritative tier |
| `external_attested` | signed/attested third-party evidence | Later, strongest tier |

> Release-blocking gates should require `automation_ci`,
> `external_attested`, or an explicit operator waiver unless project
> policy intentionally allows otherwise.

`trust_tier` is **derived**, by a pure total function, from vocabularies
that already exist. It is not a new independent assertion a producer gets
to make about itself:

| Derived `trust_tier` | Derived from |
|---|---|
| `referenced` | no `evidence_source`, or `manual_review`, or any missing binding field, or `exit_code` absent |
| `local_agent` | `gates` `evidence_source == "local_command"` and `producer_class == "agent"` |
| `local_operator` | `evidence_source == "local_command"` and `producer_class == "operator"` — **unreachable in D1–D5, see below** |
| `automation_ci` | `evidence_source == "automation_ci"` |
| `external_attested` | `evidence_source == "external_attested"` (new; no producer emits it in D1–D5) |

Rules:

- The tier ladder maps onto `gates.BLOCKER_GREEN_SOURCES` rather than
  competing with it. That set is `{automation_ci, operator_waiver}`
  today, which is already the repo's answer to "what may green a
  blocker," and it agrees with the roadmap rule on two of three terms.
  `external_attested` is the third term and has no `gates.py`
  counterpart yet.
- **`local_operator` is unreachable in D1–D5**, on the same honest
  footing as `external_attested` and `min_independent_roots > 1`. The
  mapping row above is the eventual rule, not a D1–D5 capability. D3
  ships `work artifact attach|list|show` and no runtime execution
  record, so the only way `producer_class == "operator"` can arise is an
  operator **attaching** an agent-produced output — which is operator
  *confirmation*, the exact thing this document forbids from becoming
  operator *execution*. Removing the assurance-specific path was not
  enough; the generic attach path laundered it just as effectively.
  Reaching `local_operator` requires a runtime adapter that binds actor,
  command, and inputs at execution start. Until that exists, no producer
  emits the tier and any `min_tier: "local_operator"` floor is
  unsatisfiable — including the one in this document's own policy
  example, which is therefore an illustration of a future capability.
- Note the reproduced roadmap tier table says `local_operator` means
  "local command run/confirmed by operator". This document deliberately
  diverges from the "confirmed" half: confirmation is not execution. The
  quote is preserved verbatim because it is the roadmap's text, not
  because the "confirmed" reading survives.
- Because `gates.py` is outside this team's ownership boundary, changing
  `VALID_EVIDENCE_SOURCES` / `BLOCKER_GREEN_SOURCES` for
  `external_attested` is **deferred, not requested** — it becomes a
  request only once the D6 attestation-scope decision lands. Until then
  `external_attested` is a work-side tier that no producer emits, and
  `work check` treats it as unreachable rather than pretending it works.
- `producer_class` ∈ `{agent, operator, automation, external}` is
  resolved from the roster and the invoking context at write time, not
  from a self-declared field in the artifact. An artifact that claims
  `producer_class: "operator"` while being written by a rostered agent is
  a validation failure, not an upgrade. The honest limit: a local writer
  who bypasses the CLI can write any value, which is why the tier is
  revalidated at verdict time and why this is trusted-team correctness
  rather than an authority boundary.
- **Operator waiver is not a tier.** It is a separate gate-side decision
  that already exists (`gates.waive_gate`, `operator_waiver`). Work reads
  a waiver through `check_gates`; it does not implement a second waiver
  mechanism, because two waiver paths would be two authorities.
- `isolation_status` on the artifact records the linked lane's isolation
  at write time, using the **artifact-side** lane vocabulary
  `{verified, advisory_unisolated, unverified}` — the values `lanes.py`
  actually writes. Only `verified` counts toward a release-blocking
  requirement. This matches the existing hard reject in
  `lanes.validate_delivery_artifact(..., require_isolation=True)`, which
  raises on every non-`verified` status, so work is not inventing a
  stricter rule than the close path already enforces.
- **`producer`, `producer_class`, and `trust_tier` in an incoming
  artifact are untrusted input.** They are recomputed at registration
  from the invoking context and rejected on disagreement, never accepted
  as written. An artifact that arrives claiming
  `producer: "github-actions"` gets whatever tier its actual origin
  earns.
- **Registration caps an operator-context attach at `local_agent`.** The
  recompute above resolves `producer_class` from the invoking context, so
  an operator running `work artifact attach` would otherwise resolve
  `operator` and map straight through the `local_operator` row — which is
  the generic version of the laundering path removed from assurance
  ingestion, reachable by any operator attaching any agent's output.
  Until a runtime adapter binds actor, command, and inputs *at execution
  start*, a generic attach **cannot emit `local_operator` regardless of
  who invokes it**. Removing the assurance-specific route while leaving
  the generic one open would have withdrawn the claim without withdrawing
  the capability.
- A **platform claim** is evidence like any other: "this works on Linux"
  requires an executed artifact whose producer ran on Linux at the
  current revision. A skipped platform is `UNKNOWN`, never pass. Because
  `check_name` is an opaque string, platform-scoped requirements are
  expressed as distinct check names (`pytest-linux`, `pytest-windows`)
  rather than a platform axis in the schema — the roadmap's own
  cross-platform Hard HOLD is exactly this failure, where a silently
  skipped tranche read as support.

### Execution Lineage And Independent Corroboration

`content_hash` proves an artifact record is intact. It says nothing about
whether two records came from the *same run*, because the record includes
its own `artifact_id` and two registrations of one execution hash
differently. Independence therefore needs its own identity:

- `root_execution_id` is derived from the **execution's identity**:
  `command`, `cwd`, `base_sha`, `head_sha`, `policy_hash`, `producer`,
  and `started_at`. It is deliberately **not** derived from the captured
  output. Output was the obvious choice and it is wrong: a JUnit XML and
  a console log of one pytest process are different bytes, so an
  output-derived root would hand two representations of one run two
  different identities — exactly the corroboration the field exists to
  prevent. An adapter that truncates or reformats output must not thereby
  mint a new execution.
- Registering a second artifact with an existing `root_execution_id` and
  a **different** `trust_tier` or origin is refused as
  `work_artifact_execution_conflict`. Re-importing one local log under a
  new ID with `producer_class: "automation"` does not launder it into CI
  evidence; it collides with its own earlier registration.
- `derived_from` names the parent artifact when one record is a
  reformatting or aggregation of another. Its rules are normative, not
  descriptive:
  - a descendant **inherits its ancestor's `root_execution_id`
    verbatim** — it does not compute its own;
  - a `derived_from` whose parent's root disagrees with the child's
    declared root is rejected as `work_artifact_lineage_invalid`;
  - lineage is walked **transitively** to a single root, and a cycle or
    a missing parent is `work_artifact_lineage_invalid`, never a
    silently-accepted new root;
  - a derived artifact may support a *different* claim, but it can never
    corroborate its own ancestor or any sibling sharing that root.
- **Corroboration counts distinct transitive roots, not records.** A
  policy asking for two independent checks is not satisfied by two
  adapters over one process. `required_checks` entries may declare
  `min_independent_roots` (default 1); the independence count is the size
  of the set of distinct transitive `root_execution_id` values among
  satisfying artifacts, and it is computed, not asserted.
- **A claimed root is not an independent root, and must not satisfy
  authority-critical independence.** Every input to the derivation is a
  producer claim — `started_at` explicitly so — which means a second
  adapter over one run can simply register `derived_from: null` with a
  slightly different but plausible `started_at`, pass the timestamp
  plausibility check, and mint a fresh root. No canonicalization lets the
  registry recognise the alias, because there is nothing independent to
  compare against. So `min_independent_roots` counts **distinct claimed
  tuples**, which is a weaker property than distinct executions, and
  saying otherwise would be an overclaim.
- Therefore, in D1–D5, `min_independent_roots > 1` **cannot be satisfied
  by claimed roots alone**. It is satisfiable only by roots minted by a
  launch-time execution adapter — a nonce created once when the
  execution starts and propagated verbatim to every output of that run —
  and no such adapter exists yet. A policy demanding independent
  corroboration therefore holds with
  `work_independence_unverifiable` rather than being satisfied by two
  self-asserted tuples. The lineage rules above remain fully load-bearing
  for artifacts that *honestly declare* `derived_from`; they close the
  accidental and the careless path, not the adversarial one.
- The honest limit, stated plainly: this makes accidental double-counting
  and adapter-shaped laundering machine-detectable. It is not a defence
  against a writer constructing false executions, and the independence
  count must not be described as though it were.
- An **operator waiver never mutates evidence.** It never changes an
  artifact's tier, never turns a non-`pass` outcome green, and is not
  transferable to a second requirement that happens to match the same
  glob. The underlying artifact keeps its original tier, verbatim.
- **No waiver satisfies a release-blocking work requirement through a
  direct work-side gate in D1–D5, because no waiver record capable of
  doing so exists yet.** (The waiver-backed *close* route is a separate
  matter and is **not** closed — see The Release Baseline's honest limit
  and D6 item 7.)
  `gates.waive_gate` (`gates.py:143-181`) stores only
  `{operator, date, reason, scope, expires}` where `operator` is
  unauthenticated free text; it records no `work_id`, no check or glob
  identity, no head SHA, and no policy hash. `_gate_verdict`
  (`gates.py:311-317`) then makes *any* active waiver non-blocking.
  ASSURANCE.md:109-114 is explicit that this path "MUST NOT be treated as
  operator authority until it is hardened to consume + validate a typed
  operator-answer reference," and that "a lead may not self-waive an open
  REVISE/P0/P1." Work honours that prohibition rather than routing around
  it: a gates waiver is *displayed* on the requirement it names and never
  satisfies one.
- The waiver record work would need, specified here so the hardening has
  a target and D4 does not improvise one: an authenticated
  operator-answer reference (server-derived actor, never caller-asserted),
  the exact `work_id`, the requirement's `check_name` and matched glob,
  `head_sha`, both base and candidate `policy_hash`, a reason, an expiry,
  an explicit non-transitivity marker, and a no-self-waive rule against
  the finding's owner. Until such a record exists and is validated, the
  honest state is that release-blocking requirements have **no** waiver
  escape — which is stricter than the roadmap's tier table anticipated,
  and deliberately so, because the roadmap describes the intended end
  state while ASSURANCE.md describes what is trustworthy today.
- **Operator confirmation is not operator execution.** An operator
  reviewing an agent-produced artifact produces a separate typed
  acknowledgement referencing the immutable lower-tier artifact. It never
  rewrites `producer_class` to `operator`. `local_operator` requires
  evidence the operator *ran* the command under the bound inputs;
  clicking confirm on someone else's output is not that, and treating it
  as such would make the strongest local tier the easiest one to obtain.
- The honest limit: every one of these rules is enforced against records
  a local writer could fabricate wholesale. They stop accidental
  double-counting and casual laundering, which is the realistic failure;
  they do not stop a determined forger.

## Lifecycle State Machine

The roadmap lifecycle, unchanged and deliberately narrow:

```text
draft -> open -> active -> review -> blocked -> ready -> delivered -> closed
                                  \                         \
                                   -> changes_requested      -> abandoned
```

Every state except the terminal ones is **derived**. The projection
answers "what state is this item in" by evaluating records in a fixed
order, and the first matching rule wins:

| Derived state | Condition |
|---|---|
| `abandoned` | `terminal == "abandoned"` |
| `closed` | `terminal == "closed"`, or the linked close is published |
| `delivered` | `terminal == "delivered"`, or the linked lane has a committed delivery artifact |
| `changes_requested` | a review-result bound to the **current revision** is `rejected` |
| `review` | a linked review thread **for the current revision** is open and unanswered |
| `active` | a lane is bound, or any artifact exists |
| `open` | an owner is assigned |
| `draft` | otherwise |

Rules:

- **`record_state` is a pure function of records. It never reads the
  verdict.** An earlier draft put `blocked` and `ready` in this ladder,
  which made `record_state = f(verdict)` while the contradiction pass
  made `verdict = g(record_state)` — a circular definition with no fixed
  point, no evaluation order, and no convergence. It also put `blocked`
  above every record-shaped row, so once `work check` existed a fresh
  draft with no resolvable revision would derive `UNKNOWN` and render
  `blocked`, and `changes_requested` became unreachable. The two are
  **orthogonal axes**, and interleaving them was the single most serious
  defect in the first draft.
- Ordering is fixed and total, so two readers of the same records always
  derive the same state. It is written as a table, not as nested
  conditionals, so a reviewer can check exhaustiveness by reading it.
- `changes_requested` carries a **revision qualifier**. A rejection bound
  to H3 does not describe an item now at H4; it is history. Without that
  qualifier a stale rejection would poison every later revision, which is
  Contradiction case C reappearing in the state ladder — and in the first
  draft case C survived only because the `closed` row happened to match
  first, which is luck of table position rather than a mechanism.
- Who may drive each transition: the **owner** may assign, start, bind a
  lane, attach artifacts, and request review. Any rostered agent may
  attach an artifact naming itself as producer. Only the item owner or a
  lead may `deliver` or `abandon`. `closed` is driven by `close.py`, not
  by work — work reads it. No prose in any message body drives any
  transition (DESIGN.md principle 1).
- Terminal states are recorded on the item because they must survive a
  corrupt ledger; every other state is recomputed and nothing about it is
  persisted.
- "Current" and "latest" in this table never mean newest timestamp. For
  the ledger it means highest valid `seq`. **For bus records there is no
  ordering authority at all, so work never orders them.**
  `threads.derive_threads` sorts by message id (`threads.py:596`), and
  `store._new_id` is monotonic only *within one process* — its own
  docstring says "for any single writer" (`store.py:6085-6094`). Across
  two agents, id order is producer-clock order wearing a different name.
  Delegating "the current review" to thread derivation would be a
  clock-ordering bug committed while believing it had been avoided.
- Work therefore resolves review state by **revision binding, not
  recency**: a review-result counts only if it is bound to the current
  revision, and two conflicting bound results at the same revision are
  `work_contradiction` — surfaced, never ordered. This is the one place
  where "we cannot order these" is the honest answer, and picking the
  newest would be inventing an authority the bus does not have.
- **A lane may be claimed by at most one non-terminal work item.**
  Binding is serialized on `(lane_id, lane_generation)`, so a second
  concurrent `work start` against the same lane loses at mutation time.
  The projection independently detects cardinality greater than one — a
  state only reachable through corrupt or hand-written records — and
  holds *every* claimant with `work_lane_conflict` until reconciled,
  rather than picking the older one.
- A non-terminal item requires a live lane whose generation matches
  `lane_generation` and whose head matches the bound revision. A
  terminal item may instead reference a validated immutable delivery
  artifact, which is what lets a delivered item stay readable after its
  lane is cleaned up. A lane that vanished raises `work_lane_missing`; a
  lane whose ID was reused raises `work_lane_generation_mismatch`.
  Neither is inferred to mean the work completed.
- **Work never deletes a worktree.** `work abandon` and `work deliver`
  record intent and may *request* lane cleanup; `lanes.py` remains the
  sole authority on whether cleanup is safe, and it refuses on dirty,
  unmerged, unmanaged, or user-created content. Roadmap §7 makes
  "worktree cleanup can delete dirty, unmerged, unmanaged, or
  user-created files" a Hard HOLD, and the cheapest way to never violate
  it is to own no deletion path at all.
- The honest limit: "who may drive" is enforced against the roster, which
  is trusted-team routing metadata, not authenticated identity. Per the
  identity RFC, roles and groups remain routing metadata until a real
  per-agent signing model exists.

### Two Axes, Never Collapsed

`work status` reports a **pair**: the record state above, and the gate
verdict from Work Check. Neither is derived from the other, and neither
may hide the other.

| | `GO` | `HOLD` | `UNKNOWN` |
|---|---|---|---|
| `draft` | `draft · GO` | `draft · HOLD(n)` | `draft · UNKNOWN(n)` |
| `open` | `open · GO` | `open · HOLD(n)` | `open · UNKNOWN(n)` |
| `active` | **`ready`** | **`blocked`**`(n)` | `active · UNKNOWN(n)` |
| `review` | **`ready`** | **`blocked`**`(n)` | `review · UNKNOWN(n)` |
| `changes_requested` | **IMPOSSIBLE** — see below | `changes_requested · HOLD(n)` | `changes_requested · UNKNOWN(n)` |
| `delivered` | `delivered · GO` | `delivered · HOLD(n)` | `delivered · UNKNOWN(n)` |
| `closed` | `closed · GO` | `closed · HOLD(n)` | `closed · UNKNOWN(n)` |
| `abandoned` | `abandoned · GO` | `abandoned · HOLD(n)` | `abandoned · UNKNOWN(n)` |

Every cell is written out. An earlier draft left one cell as a literal
"…" and two terminal cells empty — and in the one table whose entire
purpose is that no rendering drops an axis, an ellipsis is a *mention*,
not a specification.

Rules:

- **Both axes resolve from ONE immutable snapshot.** The CLI resolves
  records once, hands the same snapshot to `record_state` and to
  `compute_verdict`, and the view carries the snapshot's content token.
  Resolving them from two reads admits a real interleaving: compute the
  verdict from an approved review (`GO`), a same-revision rejection
  lands, derive `record_state` from the newer message set
  (`changes_requested`), and emit one envelope whose two halves refute
  each other. Two correct answers to two different questions, composed
  into one wrong document.
- **`changes_requested · GO` is impossible by construction.**
  `changes_requested` derives from a rejected review bound to the current
  revision, and that same record raises `work_review_rejected`. If a
  reader ever sees that pair, the composition came from two snapshots and
  the correct response is to recompute, not to trust either half. The
  implementation asserts it.
- `ready` and `blocked` are **presentation compositions**, not rungs.
  They are the names for (`active`-or-`review`, `GO`) and
  (`active`-or-`review`, `HOLD`) respectively, kept because the roadmap's
  lifecycle vocabulary uses them. They are not states a record can be in.
- **No single-token rendering may drop either axis.** A UI that must show
  one token shows the record state and the verdict together
  (`active · HOLD(3)`), never a bare `blocked`. Collapsing to one token
  is how a fresh draft would read as blocked and how a rejected review
  would vanish behind a HOLD.
- A `draft` item with an unresolvable revision is `draft, UNKNOWN` — an
  honest "we have not started, so we cannot tell you it is deliverable."
  It is not `blocked`, because nothing is blocking; nothing has begun.
- Terminal items still report a verdict. A `delivered` item whose
  evidence has since gone stale reads `delivered · HOLD(1)`, which is
  exactly the contradiction an operator needs to see rather than a green
  `delivered`.

## Work Check

`work.compute_verdict` is pure. The CLI resolves every piece of I/O —
git, lanes, gates, closes, threads, artifacts, policy — into a resolved
bundle and passes it in. This is the `close.compute_verdict` /
`_build_signoff_eval` split, copied deliberately, and it is what makes
the verdict unit-testable against dicts with no live repo:

```python
def compute_verdict(item: dict, *, artifacts: list[dict], lane_eval: dict | None,
                    gate_check: dict, close_eval: dict | None, review_eval: dict,
                    policy_eval: dict, revision: dict, roster_eval: dict,
                    source_manifest: dict, source_errors: list[dict]) -> dict[str, Any]:
```

The return matches `close` and `lanes` exactly:

```json
{
  "verdict": "HOLD",
  "holds": [
    {"code": "work_artifact_stale", "class": "established",
     "detail": "a-3f91c2 binds head b8e1c3a7, current head is 4d92f10c"},
    {"code": "work_artifact_unreadable", "class": "unknown",
     "detail": "a-77b0de failed content_hash; cannot establish its result"}
  ],
  "ok": false,
  "has_unknown": true
}
```

Rules:

- `verdict` ∈ `{GO, HOLD, UNKNOWN}`; `ok` is `true` **only** for `GO`.
  `UNKNOWN` is not a third outcome between pass and fail — it is
  non-advancing exactly like `HOLD`, and it exists only so an operator
  can tell "we established a problem" from "we could not establish
  anything." Any consumer reading `ok` gets the safe answer without
  knowing about `UNKNOWN`.
- **Every hold carries an epistemic `class`** ∈ `{established, unknown}`,
  and `has_unknown` is true when any hold is unknown-class. This extends
  `close`/`lanes`' `{code, detail}` entry shape additively; the outer
  `{verdict, holds, ok}` keys are unchanged, so existing consumers keep
  working.
- `UNKNOWN` wins over `GO` and loses to `HOLD` **at the top level only**.
  If every hold is unknown-class the verdict is `UNKNOWN`; if any is
  established the verdict is `HOLD`. Without the per-entry `class`, a
  single established hold would erase the fact that a *different*
  question was never answered — safely non-GO, but misleading, because a
  consumer could not distinguish "the tree is dirty" from "we never
  managed to read the security policy." Both facts survive.
- Codes are therefore not intrinsically HOLD or UNKNOWN; the **class**
  carries that. `work_artifact_unreadable` is always `class: "unknown"`
  (we could not establish the artifact's result), while
  `work_revision_dirty` is always `class: "established"`. An earlier
  draft listed the former in the HOLD-code table and called it UNKNOWN in
  Failure Modes; the class field is what makes both statements true at
  once instead of contradictory.
- HOLD codes are **stable plain strings**, not enums, matching
  `close.py:68` ("STABLE hold codes (the public verdict contract - tests
  assert each one)"). Every code below gets a test that asserts the exact
  literal.
- `gate_check` is `check_gates`'s output, whose shape
  (`{schema_version, verdict, required_gates, blockers, gates}`) does
  **not** match the close/lanes verdict shape. The CLI adapts it: each
  entry in `blockers` becomes one `work_gate_hold` entry carrying the
  gate name and reason. Work does not attempt to unify the two shapes,
  and it does not read `gate_check["holds"]`, which does not exist.
- `source_errors` is a required parameter, not an optional one, so a
  caller cannot forget to pass failures and accidentally get a clean
  verdict. Each entry produces a hold; a projection with a failed source
  is never green (D-16).
- **Requirements are derived first, evidence is matched second.** The
  evaluator computes the required check set from the changed paths and
  the applicable policy *before* looking at any artifact, then asks
  whether each requirement is satisfied. Filtering artifacts first and
  concluding from an empty list is how `all([])` returns true and a
  wrong-scope artifact reads as no-requirement — the C1(3) shape. A
  requirement with no matching evidence stays unsatisfied; it never
  disappears.
- A **present but wrong-scope** artifact is `work_artifact_wrong_scope`
  *and* leaves its requirement unsatisfied. Two holds, not one, because
  "you ran the wrong thing" and "you did not run the right thing" are
  different repairs.
- Each required check resolves to exactly one outcome from a closed
  enum: `pass`, `fail`, `skipped`, `waived`, `unknown`, `timeout`,
  `malformed`, `adapter_error`, `unavailable`, `missing`. **Only `pass`
  satisfies.** Every other value is a distinct, stable, non-GO outcome
  with its own detail. There is no `else` branch and no "no findings
  means green" — an empty parse result is `malformed`, not `pass`.
  `waived` and `unknown` are in the enum because they are real current
  `gates.VALID_STATUSES` values (`{red, green, unknown, skipped,
  waived}`); an earlier draft claimed elsewhere that they were covered
  while omitting them here, which would have left an implementer with no
  branch for two statuses the gate layer actually emits.
- **Any value outside this enum normalizes to `malformed`**, never to
  `pass` and never to a silent skip. A closed enum without a stated
  normalization rule is only closed until the first unexpected input.
- **Facts are joined on an exact key**, never on `work_id` alone. An
  artifact, review, gate result, or close counts toward the current
  verdict only when it matches on `(work_id, base_sha, head_sha,
  diff_hash, policy_hash, lane_id + lane_generation)` as applicable to
  that source. A record that matches loosely is *historical*, and
  historical records are reported as history rather than silently
  applied to the current revision.
- **The revision is resolved at check time**, not read from a field. The
  CLI resolves the authoritative head from the bound lane or target ref,
  requires it to equal the artifact's bound `head_sha`, and requires that
  SHA to still be reachable. A force-push that leaves the bound commit
  unreachable is `work_revision_unreachable`, and rebinding to the new
  head is an explicit event, never automatic.
- **Nothing is inferred from an unresolvable revision.** A null or
  garbage `base_sha`, an unreachable object, a git failure, or a diff
  that cannot be computed yields `work_revision_unresolvable` — never
  "no changed paths, therefore no requirements." An empty requirement set
  from a failed diff is the same vacuous-truth bug as `all([])`, reached
  by a different road.
- **Timestamps never order anything and never establish freshness.**
  Ordering is by `seq` and the existing thread derivation. A producer
  timestamp beyond a bounded skew, or an interval where `ended_at`
  precedes `started_at`, raises `work_implausible_timestamp` and the
  artifact does not count. Only `ingested_at` is locally observed;
  producer times are claims.
- **The evaluator writes nothing.** `compute_verdict` performs no I/O,
  stamps no `last_checked`, refreshes no cache, and reads no ambient
  clock, git, or roster state — every such value arrives in a resolved
  parameter (`revision` for git facts, `roster_eval` for actor
  classification). An earlier draft required re-resolving
  `producer_class` at verdict time while providing no roster parameter
  and saying every value arrives via `revision` — so D4 would have had to
  either read the roster inside the pure function, breaking this clause
  and Safety Invariant #15, or skip re-resolution and leave the
  tautology unfixed. `roster_eval` is the carrier: CLI-resolved I/O like
  every other source, inside the snapshot SI #15 hashes. Calling
  it twice with the same snapshot returns the same verdict and leaves
  `.agenttalk/` byte-identical. Recovery from an interrupted transaction
  is a separate command, so no repeat invocation of `work check` can turn
  a HOLD into a GO.

### The Resolved Bundle And Source Manifest

The pure/impure split only fails closed if the *bundle* is trustworthy.
An earlier draft specified a gate adapter that walked `gate_check`'s
`blockers` list and made `source_errors` a required parameter, and called
that sufficient. It was not, in two distinct ways.

- **Emptiness is not agreement.** A bundle carrying
  `{"verdict": "HOLD", "required_gates": ["security"], "blockers": [],
  "gates": []}` produces no hold under a blockers-only adapter and lands
  in the GO branch. Both precedents this design claims to copy guard
  against exactly that: `lanes.py:420-422` and `close.py:186-188` each
  test `gate_check.get("verdict") != GO` rather than trusting the list.
  The draft cited their discipline while dropping their guard.
- **A required parameter is not an attestation.** Making
  `source_errors` mandatory forces a caller to pass a list; `[]` says
  "no errors were reported," never "every source was attempted."

Rules:

- **Every verdict-bearing source must return exact GO.** `gate_check`
  satisfies only when `gate_check["verdict"] == "GO"`; `close_eval` only
  when `final.verdict == "GO"`; `lane_eval` only when **both**
  `verdict == "GO"` **and** `ok is True`. A non-GO verdict raises its
  adapted hold *even when its detail list is empty*.
- **Any source whose own fields disagree is `work_bundle_invalid`.** A
  lane bundle of `{"verdict": "HOLD", "holds": [], "ok": true}` is
  well-shaped and internally contradictory; accepting it on `ok` alone
  would let a torn adapter satisfy the lane rule. `lanes.py:427-429`
  keeps the two consistent today — this is a contract against a future or
  damaged producer, not an accusation against the current one. The same
  applies to an empty detail list under a non-GO verdict: emptiness and
  non-GO disagree, so the bundle is invalid rather than merely held.
- The bundle is a **closed, validated schema**. Every parameter has a
  required shape; an unknown key, a missing key, or a value of the wrong
  type is `work_bundle_invalid`, not a tolerated variant. The evaluator
  validates the bundle before evaluating anything in it.
- The CLI supplies a **source manifest** naming every verdict-bearing
  source and its resolution status ∈ `{ok, failed, not_attempted}`:

  ```json
  {"gates": "ok", "close": "ok", "lanes": "ok", "review": "ok",
   "policy": "failed", "revision": "ok", "artifacts": "ok", "roster": "ok"}
  ```

  A source absent from the manifest, or marked `not_attempted`, raises
  `work_source_not_attempted`. A `failed` source raises
  `work_source_error` with `class: "unknown"`. **GO requires every
  verdict-bearing source to be present and `ok`** — completeness is
  attested positively rather than inferred from an empty error list.
- The manifest is part of the snapshot Safety Invariant #15 hashes, so
  the same snapshot always yields the same verdict, manifest included.

### The Release Baseline

"No waiver satisfies a release blocker" is not closed under **omitted**
requirements. Policy is optional, `close_id` is nullable,
`review_request_ids` may be empty, and a release tier floor only raises
the bar on requirements that *exist* — it never requires one to exist.
An item with no policy, no matching rule, no close, and an empty gates
set therefore has no release requirement to fail. Omission becomes the
silent waiver, which is the same fail-open as an unauthenticated waiver
wearing different clothes.

**The baseline applies UNLESS the item is provably non-release.** This
direction is load-bearing. An earlier draft triggered the baseline only
when release-blocking *derived true* — and derived it from the presence
of a close or a policy, which are exactly the records whose **absence**
is the omission being audited. A null `close_id` made the first disjunct
false, a missing policy made the second false, the baseline never ran,
and `work_close_missing` could fire only when a close *existed* but was
unpublished. The guard was disabled by the condition it existed to
catch; omission moved up one level instead of being closed.

So: an item is treated as release-blocking unless **every** applicable
classification record positively proves otherwise. Two signals classify
an item — the linked close's `scope`, and the `release_scoped` flag on
each policy rule matched to a changed path — and they are combined by a
**total truth table**, never by a disjunction:

| Close signal | Policy signal | Result |
|---|---|---|
| `scope` ∈ `RELEASE_CLASS_SCOPES` | anything | **release-blocking** |
| any | any matched rule `release_scoped: true` | **release-blocking** |
| `scope` release · policy all `false` | — | **release-blocking** (conflict) |
| `scope` non-release · any rule `release_scoped: true` | — | **release-blocking** (conflict) |
| `scope` non-release | every matched rule explicitly `release_scoped: false`, every changed path matched | **exempt** |
| absent / no `close_id` | anything | **release-blocking** |
| any | any matched rule with `release_scoped` **absent** | **release-blocking** |
| any | any changed path matched by no rule | **release-blocking** |
| `scope` missing or unparseable | anything | **release-blocking** |

Rules:

- **Any present release signal wins.** One release-class close makes the
  item release-blocking no matter what the policy says.
- **Disagreement is release-blocking, never exempt.** Both directions of
  conflict resolve the same way, because a contradiction between two
  classification records is not evidence of exemption — it is evidence
  that the classification is unreliable, and `work_release_class_conflict`
  reports it so an operator can repair the records rather than have work
  pick a side.
- **Absence is never proof.** An earlier draft exempted on
  *(non-release close)* **OR** *(no matched release-scoped rule)* while
  defaulting a missing `release_scoped` to `false` — so a release-class
  close plus one legacy rule `{glob: "src/**", checks: ["pytest"]}` with
  no `release_scoped` key made the second disjunct true and skipped the
  entire baseline, *despite the authoritative close being release-class*.
  An omitted field had been defaulted into proof. `release_scoped` is now
  three-valued for classification: `true`, `false`, or **absent =
  unknown**, and unknown never exempts.
- Exemption therefore requires all four: at least one applicable
  classification record exists, no release signal is present, every
  matched rule is *explicitly* `release_scoped: false`, and no
  classification is absent, unmatched, or unparseable.

| Requirement | Hold when absent |
|---|---|
| A policy exists at the base revision | `work_release_policy_missing` |
| Every changed path matches ≥1 release-scoped rule | `work_uncovered_release_path` |
| A linked close exists and is published | `work_close_missing` |
| A review-result bound to the current revision exists and is approved | `work_review_missing` |
| `gate_check.required_gates` is non-empty and every one is green | `work_release_gates_vacuous` |

Rules:

- The release classification reads the linked close record's **`scope`**
  field — the one `close.py:190` tests against `RELEASE_CLASS_SCOPES`.
  Explicitly **not** the close's `gate_scope`, and **not** the work
  item's own `gate_scope`. All three exist, two share a name
  (`close.py:419` lists `scope` and `gate_scope` as separate required
  fields, and the item schema's `gate_scope` example `"feature"` is
  itself a member of `RELEASE_CLASS_SCOPES`), so reading the wrong one
  silently misclassifies. The name collision is the trap; naming the
  exclusions is the guard.
- **Waiver-backed gates are rejected, not inherited.** `gates.py:311-317`
  removes an active waiver from `blockers` and reports GO. Work therefore
  scans `gate_check["gates"]` **row by row**, independently of
  `blockers`, and raises `work_waiver_backed_gate` for any required gate
  whose status is `waived`. Consuming another module's GO without
  inspecting what produced it is how a waiver becomes transitive.
- **`close_eval` is the STORED publication verdict, not a
  recomputation.** `close.compute_verdict` deliberately returns
  `publish_not_allowed` for an already-published record
  (`close.py:175-179`), so "recompute and require GO" is *unsatisfiable*
  for exactly the published closes the baseline requires. `close_eval`
  therefore carries `record["final"]` — its `verdict`, `gate_verdict`,
  `blockers`, `by`, `at`, and the close's `scope` and `revision` — and
  the baseline requires `final.verdict == "GO"` bound to the current
  revision.
- **HONEST LIMIT — work CANNOT currently detect a waiver-backed
  close.** `record["final"]` stores `gate_verdict` and blocker **names**
  only (`close.py:1083-1091`); it carries no gate rows, no waiver status,
  and no override provenance. Three further escapes are equally
  invisible: a blocker remediation resolved through a non-required waived
  gate (`close.py:232-247`, `:371-387`), an unauthorized lens ack cleared
  via `ack.override` (`close.py:390-396`, `:984-1010`), and a required
  specialist set skipped via `signoff_overrides` (`close.py:299-305`,
  `:1174-1187`). The last two are roster-lead escapes, not the
  authenticated operator answer ASSURANCE.md:104-114 requires. There is
  therefore **no `work_waiver_backed_close` code**, because a named code
  that cannot fire is the same defect as a persisted field the prose
  calls derived. Closing this needs a close-side provenance envelope,
  which is outside this team's ownership boundary — scheduled at D6.
- The honest limit on the whole baseline: it makes *omission* detectable,
  so a release cannot pass by having no requirements. It cannot make the
  requirements *correct* — a policy demanding only a lint check passes a
  baseline check and attests very little. Non-vacuous is a floor, not a
  guarantee.

### The Review Binding Contract

A `review-result` message today carries **no lens and no revision**.
`gates.validate_review_result_evidence` is the only validator, it fires
only for `status == "approved"` (`gates.py:270`), and the fields it
requires are `risk_class`, `release_blocker`, `tests_referenced`,
`tests_executed`, `residual_risk`, `evidence`-or-`artifacts`, and
`na_reason` (`gates.py:269-296`). There is no `head_sha`, no `base_sha`,
no `diff_hash`, and no `lens`. `reviewed_ref` exists only as skill
policy and is validated by nothing.

Two consequences the draft has to state rather than assume:

- **Staleness is not computable for an unbound review.** A message with
  no revision cannot be compared against the current one. Work does not
  pretend otherwise: an unbound review-result raises
  `work_review_unbound` and cannot satisfy a blocking review
  requirement. It remains visible as `referenced` evidence.
- **A new meta contract is unavoidable**, and specifying it here is the
  point — the alternative is D4 inventing one under time pressure. Work
  reads two additive meta keys on an existing `review-result`:
  `meta.work_id` and `meta.reviewed_head_sha`. No new message kind, so
  old clients are unaffected and the Non-Goal holds; but "no new kind"
  is not the same as "no new contract," and pretending otherwise would
  have hidden a real schema decision inside an implementation phase.

Rules:

- Work **validates** these two keys on ingestion and never writes them;
  the reviewing agent supplies them. A `reviewed_head_sha` that is not
  a full 40-character SHA is unbound, not coerced.
- `work_review_stale` applies only to a *bound* review whose
  `reviewed_head_sha` is no longer current. Unbound and stale are
  different failures with different repairs — one needs a re-review, the
  other needs the reviewer to say what they reviewed.
- Lens identity is **not** read from the message, because nothing on the
  message names a lens. It comes from close's `--lens` argument and
  lives in close's `lens_acks`. Work links that; it does not parse it.

### HOLD Codes

| Code | Raised when |
|---|---|
| `work_malformed_item` | the item record fails to load or fails schema validation |
| `work_unknown_schema_version` | `schema_version` is not exactly the supported version |
| `work_ledger_problem` | one or more event lines are malformed |
| `work_event_chain_broken` | a `seq` gap or `prev_hash` mismatch; later transitions are not applied |
| `work_ledger_gap` | an item mutation has no corresponding event and cannot be reconstructed |
| `work_ledger_ahead` | the ledger tail is ahead of `ledger_head` with `pending_op` null |
| `work_ledger_head_mismatch` | `ledger_head` names a tail the ledger does not have (rewritten or truncated tail whose internal `prev_hash` chain still verifies) |
| `work_bundle_invalid` | the resolved bundle fails its closed schema |
| `work_source_not_attempted` | a verdict-bearing source is absent from the source manifest or marked `not_attempted` |
| `work_release_policy_missing` | a release-blocking item has no policy at the base revision |
| `work_uncovered_release_path` | a changed path matches no release-scoped rule |
| `work_close_missing` | a release-blocking item has no published linked close |
| `work_release_gates_vacuous` | `required_gates` is empty for a release-blocking item |
| `work_waiver_backed_gate` | a required gate is green only via an active waiver |
| `work_recovery_required` | `pending_op` is set and recovery has not run (crash states (a) and (b)) |
| `work_release_class_conflict` | the close and policy release signals disagree (item is release-blocking) |
| `work_independence_unverifiable` | `min_independent_roots > 1` cannot be satisfied by claimed roots |
| `work_ledger_orphan` | an event references a `work_id` with no item record |
| `work_version_conflict` | a mutation's `expected_version` no longer matches `item_seq` |
| `work_artifact_unreadable` | an artifact is torn, unparseable, or fails `content_hash` |
| `work_artifact_binding_missing` | a required binding field is absent (never backfilled) |
| `work_artifact_stale` | an artifact binds a `head_sha`/`diff_hash` that is not the current revision |
| `work_artifact_wrong_scope` | an artifact is present but scoped to paths the requirement does not cover |
| `work_artifact_log_missing` | metadata declares a log that is absent |
| `work_artifact_log_hash` | the log's hash or size disagrees with the metadata |
| `work_artifact_tier_drift` | the re-resolved `trust_tier` is **weaker than** the bound one (a stronger re-resolution is discarded, never applied) |
| `work_artifact_execution_conflict` | a second artifact claims an existing `root_execution_id` at a different tier or origin |
| `work_artifact_lineage_invalid` | a `derived_from` chain has a root mismatch, a cycle, or a missing parent |
| `work_independence_insufficient` | fewer distinct transitive roots than the requirement's `min_independent_roots` |
| `work_requirement_expired` | a satisfying artifact is older than the requirement's `max_age` |
| `work_artifact_insufficient_tier` | the best artifact for a release-blocking requirement is below `automation_ci` (no waiver escape exists — see Evidence Tiers) |
| `work_waiver_not_authoritative` | a gates waiver is present on a required check but cannot satisfy it (unauthenticated `operator` field, no work/head/policy binding) |
| `work_implausible_timestamp` | a producer timestamp is beyond bounded skew or the interval is impossible |
| `work_required_check_missing` | a policy-required check has no artifact at all |
| `work_required_check_not_green` | a required check resolves to any closed-enum outcome other than `pass` (the outcome is named in the detail) |
| `work_revision_dirty` | the working tree or lane has uncommitted changes |
| `work_revision_unreachable` | the bound `head_sha` is no longer reachable (force-push, gc) |
| `work_revision_unresolvable` | base/head cannot be resolved, or the diff cannot be computed |
| `work_lane_isolation_unverified` | the linked lane's `isolation_status` is not `verified` |
| `work_lane_missing` | the item references a lane that no longer exists |
| `work_lane_generation_mismatch` | the lane ID matches but the generation does not |
| `work_lane_conflict` | more than one non-terminal item claims the same lane |
| `work_review_missing` | a review is required — by the release baseline, or by a `required_review: true` rule in the matched policy entry — and has no terminal review-result bound to the current revision |
| `work_review_unbound` | a review-result carries no `reviewed_head_sha`, so its currency cannot be established |
| `work_review_stale` | a *bound* review-result's `reviewed_head_sha` is no longer current |
| `work_review_conflict` | two bound review-results disagree at the same revision (never ordered by recency) |
| `work_review_rejected` | the current terminal review-result is `rejected` |
| `work_gate_hold` | `check_gates` reported a blocker for the item's scope |
| `work_gate_stale` | the gate result was recorded against a different revision or policy hash |
| `work_close_hold` | the linked close's stored `final.verdict` is not `GO` (a malformed or absent `final` is `work_bundle_invalid`, not a pass) |
| `work_close_stale` | the close was evaluated at a revision or policy that is no longer current |
| `work_policy_missing` | policy is required by the item but absent |
| `work_policy_invalid` | policy fails shape validation |
| `work_policy_changed_in_branch` | the branch relaxes `code-policy.json` (no authoritative waiver exists in D1–D5, so this does not clear) |
| `work_policy_hash_drift` | the policy hash now differs from the one artifacts were produced under |
| `work_policy_changed_since_open` | the policy moved since `policy_hash_at_open` |
| `work_domain_scope_drift` | `registry_hash_at_bind` no longer matches the current registry |
| `work_scope_outside_domain` | `scope_globs` is not a subset of the bound domain's paths |
| `work_out_of_scope_change` | the diff touches paths outside `scope_globs` |
| `work_source_error` | a source read failed and its result could not be established |
| `work_contradiction` | two records disagree irreconcilably (see below) |

### Contradictions Are Surfaced, Never Resolved

Roadmap §7 makes "records can disagree with no single projection
explaining the conflict" a Hard HOLD. The projection therefore has an
explicit contradiction pass:

- If the item's `terminal` says `delivered` but no committed lane
  delivery artifact exists, that is `work_contradiction`, not a silent
  preference for either record.
- If a close is published **at a revision the linked records do not
  support** — its `revision` does not match the resolved current
  revision, or the gate/review records it rests on are not current — that
  is `work_contradiction`. Stated as record-to-record, deliberately: an
  earlier draft phrased it as "a close is published but work check
  holds," which compared a record to the verdict nine lines above the
  rule forbidding exactly that, and would have fired for *any* closed
  item carrying *any* hold, duplicating `work_close_stale`.
- **`state_asserted` events are advisory and are never a contradiction
  source.** An actor's recorded belief about the state is evidence that
  they believed it, nothing more. An item whose records legitimately
  advance past its last assertion is *normal*, not contradictory — an
  earlier draft treated that disagreement as a hold, which combined with
  a verdict-derived state to make such an item a permanent HOLD by
  construction.
- Contradictions compare **records to records**, never a record to the
  verdict and never a record to the derived state. The evaluation order
  is fixed and one-directional: resolve sources → compute per-source
  holds → run the contradiction pass over records and those holds →
  return. Nothing downstream feeds back upstream, which is what makes
  the function total and its repeat-invocation determinism (Safety
  Invariant #15) actually true rather than merely claimed.
- A contradiction is always `HOLD`, never `UNKNOWN`. We established that
  the records conflict; that is knowledge, not absence of it.

Two tempting reducers are both wrong, and the design rejects both by
name. **"Most recent wins"** is wrong because clocks are producer-
controlled and a non-authoritative record must not override the owner of
a field — it is the same mistake as ordering the ledger by timestamp.
**"Most pessimistic wins"** is wrong because a stale historical failure,
or an expected earlier HOLD, would permanently poison every later valid
revision; a project whose gate went red at H1 could never go green at
H7. The correct reducer is exact-key applicability plus per-field source
ownership: a record applies to the current verdict only if it joins on
the key, and within that set each field is answered by the module that
owns it.

Three cases pin the semantics:

- **A — stale approvals under a dirty head.** Item says ready at H2, the
  lane is dirty at H2, and gate, close, and review are all green at H1.
  Render H2 as `HOLD` with `work_revision_dirty` plus
  `work_gate_stale` / `work_close_stale` / `work_review_stale`. The H1
  facts remain visible as history; they simply do not apply to H2.
- **B — an unbacked delivery claim.** Item claims delivered at H3, the
  lane has no validated delivery artifact and is dirty, gate is green at
  H3, close holds at H3. Render `work_contradiction` and non-GO. Do not
  silently demote the item to active, and do not bless the delivery
  because the gate happens to be green — either would be picking a side.
- **C — historical dissent must not poison a valid present.** Item and
  lane are closed at H4 under policy P2; the gate has an old blocker at
  H3/P1 and green at H4/P2; close and review are valid at H4/P2; and an
  H3 `changes_requested` message carries a *later* timestamp than
  everything at H4. Render closed and current at H4, with the H3 dissent
  shown as history. The future-dated H3 record does not apply to H4,
  because applicability is by key, not by clock. Note this is enforced by
  the exact-key join and the `changes_requested` revision qualifier — not
  by the `closed` row happening to match first, which is where an earlier
  draft's case C actually survived.

### The View Envelope

`compute_verdict` returns `{verdict, holds, ok}` with `close`/`lanes`'
outer keys preserved, extended additively by a per-hold `class` and a
top-level `has_unknown` (see Work Check). Both extensions appear in the
envelope's nested verdict and in the legacy shape below — a projection
that dropped them would hide the established/unknown distinction the
evaluator computes. That shape deliberately cannot carry history — and
the sections above promise history in several places: the H1 facts in
case A, the H3 dissent in case C, `ledger_problems`, bounded
`source_error` rows, a bounded error record per corrupt `work_id`, and
per-artifact tiers for Safety Invariant #11.

Those live in the **view**, not the verdict:

```json
{
  "schema": "work-view-v1",
  "work_id": "w-0007",
  "record_state": "active",
  "verdict": {"verdict": "HOLD", "ok": false, "has_unknown": false,
              "holds": [{"code": "work_artifact_stale", "class": "established", "detail": "…"}]},
  "revision": {"base_sha": "e0e8f7b4…", "head_sha": "b8e1c3a7…", "resolved_at_head": true},
  "artifacts": [{"artifact_id": "a-3f91c2", "check_name": "pytest", "trust_tier": "local_agent",
                 "current": false, "caution_flags": ["rebased_identical_diff"]}],
  "history": [{"kind": "review", "revision": "H3", "status": "rejected", "applies_to_current": false}],
  "source_errors": [{"source": "gates", "error": "gates.json unreadable"}],
  "ledger_problems": 0
}
```

Rules:

- The verdict object inside the envelope is byte-identical to what
  `compute_verdict` returns. The view wraps it; it never rewrites it, and
  no consumer needs to reconstruct one from the other.
- Every history row carries `applies_to_current`. A row that does not
  apply is shown as history and is **never** an input to the verdict —
  which is what makes "surface the disagreement, don't pick a side"
  implementable rather than merely asserted.
- `record_state` and `verdict` both appear, per Two Axes, Never
  Collapsed. There is no field that reduces them to one token.
- **The `--output-schema legacy` escape carries both axes or it does not
  exist.** A legacy rendering that emits `record_state` alone could show
  `delivered` while omitting a HOLD, which is the no-collapse rule
  defeated by the compatibility hatch. The legacy shape is therefore
  `{work_id, record_state, verdict, ok, has_unknown}` — flattened, but
  never single-axis and never dropping the epistemic flag. If a consumer
  needs less than that, it does not get a work view.
- The envelope carries the **snapshot content token** both axes were
  resolved from, so a reader can tell that the pair is internally
  consistent rather than composed from two reads.

## Policy Boundary

Optional `.agenttalk/code-policy.json`. Absent policy means no
project-required checks — it does not mean everything passes, because
gates, close, lane isolation, and review requirements still apply.

```json
{
  "schema_version": 1,
  "policy_id": "agenttalk-core",
  "required_checks": [
    {
      "glob": "src/agenttalk/**",
      "checks": ["ruff", "bandit", "pytest"],
      "min_tier": "local_agent",
      "release_min_tier": "automation_ci",
      "required_review": true,
      "release_scoped": true
    },
    {
      "glob": "src/agenttalk/supervisor.py",
      "checks": ["pytest", "supervisor-crash-matrix"],
      "min_tier": "local_operator",
      "release_min_tier": "automation_ci"
    }
  ],
  "require_close_lenses": true,
  "waiver": {"allowed_by": ["operator"], "requires_reason": true}
}
```

Rules:

- Matching is by glob through `domains.glob_matches`, and **every**
  matching entry must be satisfied independently. There is no
  first-match, no most-specific-wins, no longest-prefix, and no prefix
  arm. All four are the same bug wearing different hats: each picks one
  winner, and a permissive nested rule that wins erases a broad
  security requirement. This is D-11 verbatim — picking a winner
  "imposes a total order on a partial order and was twice proven
  unsound."
- Longest-prefix deserves its own refusal because it *looks* principled.
  It is not even well-defined here: globs are sets, and sets overlap
  without nesting. `src/**/secret/*.py` and `src/api/**` intersect while
  neither contains the other, so there is no longer one, and wildcards,
  ties, and case/path normalization each produce further incomparable
  pairs. Requirements **compose** — a path in two rules owes both — which
  removes the ordering question rather than answering it.
- Duplicate normalized globs are rejected at validation, matching the
  lane registry's rule, so "which of these two identical entries won" can
  never be asked.
- A change to `src/agenttalk/supervisor.py` therefore satisfies both
  example entries, including the stricter `local_operator` floor and the
  union of both check lists.
- The **matched glob** is persisted on the artifact's satisfaction
  record, never the raw path. C2 persisted the path and let a broad
  approval clear a nested one; persisting the glob is what makes
  revalidation meaningful.
- Persisted satisfaction is **revalidated at verdict time** across six
  dimensions, not two: the matched **rule identity** (glob), the current
  **policy** and **registry**, the artifact's **revision**, its **tier**
  floor, and the producing **actor**'s re-resolved classification.
  Requirement **expiry** is the sixth — a `required_checks` entry may
  declare `max_age`, after which a satisfying artifact is stale even at
  an unchanged revision, for checks whose result decays with the outside
  world (dependency audits, licence scans). A check satisfied under an
  older policy or beyond its declared age does not stay satisfied by
  inertia. Producer **retirement** is explicitly not in this list — it
  raises `producer_retired` as a caution and demotes nothing, per
  Source-Of-Truth Boundaries.
- `required_review: true` on a matched entry is what makes a review
  required work-side, and it is the predicate `work_review_missing`
  fires on outside the release baseline. It defaults to `false` when
  absent.
- `release_scoped` is **three-valued for classification**: `true`,
  `false`, or **absent = unknown**. It does *not* default to `false`. An
  absent flag cannot prove an item non-release, because a legacy policy
  written before the field existed would then silently exempt every item
  it matched. For every other purpose (which requirements apply) an
  absent flag behaves as not-release-scoped; only the *exemption* reading
  treats it as unknown, and only exemption needs proof.
- `min_tier` applies always; `release_min_tier` applies when the item is
  release-blocking per The Release Baseline. Absent `release_min_tier` defaults to
  `automation_ci`, per the roadmap's trailing rule — the default is the
  strict one, so forgetting to write it fails closed.
- `policy_hash` is computed over the canonical serialization and bound
  into every artifact. If policy changes, artifacts produced under the
  old hash stop counting for required checks and `work_policy_hash_drift`
  is raised.
- **Base-policy evaluation**: required checks are resolved from the
  policy at the item's `base_sha`, not from the working tree. A branch
  that edits `code-policy.json` cannot thereby change the rules that
  judge it (roadmap §8, "policy drift"). If the branch modifies the
  policy file in a **relaxing** direction, `work_policy_changed_in_branch`
  holds, and **no escape exists in D1–D5** — see the union rule below.
  The roadmap's named mitigation is "explicit waiver for policy changes,"
  not "detect and allow," but the waiver it names is the authenticated
  record specified in Evidence Tiers, which has not been built.
- The evaluated requirement set is the **union** of the base policy and
  the candidate policy. A branch may freely make itself *stricter* — an
  added requirement takes effect immediately, with no waiver, because
  raising your own bar needs no permission. Only **relaxation** (a
  removed requirement, a lowered tier floor, a widened waiver rule) needs
  an operator waiver binding **both** the base and candidate policy
  hashes so it cannot be reused after either side moves. That waiver is
  the same record specified in Evidence Tiers, and it does not exist yet
  — so in D1–D5 a policy relaxation simply holds. Fail-closed with no
  escape is the honest state; an escape routed through the current
  unauthenticated `gate waive` would be worse than none.
- Both hashes are recorded on the verdict. "The policy changed" and
  "which direction it changed" are different facts, and a reviewer needs
  the second one to judge the waiver.
- Core validates policy **shape** only. It does not know what `ruff`,
  `bandit`, or `supervisor-crash-matrix` are, and it never executes them
  in D1–D5. A check name is an opaque string that an artifact's
  `check_name` must match exactly.
- **Work does not own review lenses; `close.py` does.** An earlier draft
  put a bare `required_lenses` string list here, which would have been a
  second and weaker authority on a question close already answers with a
  full authorization model: `required_lenses` entries carry
  `{id, allowed_agents, allowed_roles, allowed_groups, required}`
  (`close.py:126`), acks land in `lens_acks` (`:130`), authorization is
  checked against agent/role/group refsets (`:390-403`), staleness
  compares `ack["revision"]` to the close revision (`:209-222`), and the
  verdict emits `missing_lens` / `unauthorized_lens_ack` /
  `stale_lens_ack` (`:72-74`). A bare string list would have reported
  lens coverage satisfied for an ack `close.compute_verdict` rejects as
  unauthorized — exactly the duplicated-truth failure Source-Of-Truth
  Boundaries forbids. `require_close_lenses` is therefore a boolean that
  defers to the linked close; `work check` reads close's verdict and
  never recomputes lens coverage.

### Satisfaction Records

When a requirement is satisfied, work persists a satisfaction record at
`.agenttalk/work/satisfaction/<work_id>.json` — a **separate file**, not
a key inside the item. Putting cache data in the identity-authoritative
record would share its lock and its `item_seq`, so a cache write would
bump the item's version and contend with real mutations. It is data,
never authority — the verdict recomputes matching every time (see
"revalidated at verdict time"):

```json
{
  "schema_version": 1,
  "requirement_glob": "src/agenttalk/**",
  "check_name": "pytest",
  "artifact_id": "a-3f91c2",
  "root_execution_id": "x-6b20f4de91c7",
  "policy_hash_base": "3b1f0c9d22",
  "policy_hash_candidate": "3b1f0c9d22",
  "head_sha": "b8e1c3a70f2d4916ac53b7e08d1f6a2c94e7d305",
  "recorded_at": "2026-07-18T09:31:45.112094Z"
}
```

Rules:

- `requirement_glob` is the **normalized glob**, never the raw path.
  Persisting the path was C2's bug: it let a broad approval clear a
  nested entry with different approvers.
- A satisfaction record is a cache of a past evaluation, not a licence.
  If the current policy produces a requirement this record does not
  match — or the artifact behind it fails revalidation — the requirement
  is unsatisfied regardless of what the record says.
- It exists for audit and diagnosis: "why did this pass last time" is a
  question an operator will ask, and reconstructing it from scratch is
  worse than recording it.
- The honest limit: base-policy evaluation reads the policy from git at
  `base_sha`, so it depends on git being available and the base being
  reachable. When it is not, the result is `UNKNOWN` with
  `work_source_error`, never a fallback to the working-tree policy.
  Falling back would silently reintroduce the exact bypass.

## Reset And Durability Semantics

`Store.reset` deletes `messages/` and `state/` and preserves everything
else. `.agenttalk/work/` and `.agenttalk/artifacts/` are **top-level**,
so both **survive reset** by construction.

Rules:

- This placement is deliberate. A work item is a durable delivery record;
  losing it on a session reset would destroy exactly the history the
  feature exists to keep. It sits with `domains.json`, `knowledge/`,
  `closes/`, `gates.json`, `lane-deliveries/`, and `control-audit/` on
  the preserved side of the load-bearing durability boundary
  (DESIGN.md §3).
- Because they survive, the durability-boundary assertion in the
  end-to-end regression test must be extended to name `work/` and
  `artifacts/` in the preserved set. The boundary is only load-bearing
  because a test pins it.
- `cli.py` already warns on `reset` that active lane coordination state
  is cleared while delivery artifacts under `.agenttalk/lane-deliveries/`
  are not touched. That warning needs extending: work items and evidence
  artifacts also survive, so an operator resetting a bus to "start clean"
  will still see the prior work records. Surviving is correct; surviving
  *silently* would be surprising in the same way lane artifacts were.
- **Name hazard, called out so nobody conflates them:**
  `.agenttalk/state/work-heartbeat/` already exists and is **entirely
  unrelated** to this feature. It holds per-agent wrapper liveness
  diagnostics written by `store.write_work_heartbeat_status`, it is
  explicitly "NOT a supervisor input in v1," and because it lives under
  `state/` it **is** cleared by reset. The two directories share four
  letters and nothing else. This RFC's storage is `work/` (durable
  delivery records) and `state/work-heartbeat/` is liveness telemetry;
  no code should read one expecting the other.
- A reset that clears `state/lanes.json` while work items survive leaves
  items referencing lanes that no longer exist. That is not corruption —
  it is exactly the contradiction the projection must surface, and it
  raises `work_lane_missing` rather than silently unbinding.

## Failure Modes And Fail-Closed Behavior

Every row is a case where an earlier version of this codebase, or an
obvious naive implementation, would have returned green.

| Condition | Result | Never |
|---|---|---|
| Item file corrupt | `HOLD` `work_malformed_item`; mutations refused | overwritten or recreated |
| Item file absent | command exits non-zero, "unknown work id" | treated as an empty valid item |
| Unknown `schema_version` | `HOLD` `work_unknown_schema_version` | upgraded, guessed, or skipped |
| Event line malformed | line isolated + surfaced; `HOLD` `work_ledger_problem` | silently dropped |
| Artifact torn / bad hash | `UNKNOWN` `work_artifact_unreadable` | crash, or treated as pass |
| Artifact absent for a required check | `HOLD` `work_required_check_missing` | absence read as pass |
| Artifact present, wrong `head_sha` | `HOLD` `work_artifact_stale` | accepted because it exists |
| Artifact present, wrong scope/glob | `HOLD` | dropped as if absent |
| Required check `skipped` / `waived` / `unknown` | `HOLD` `work_required_check_not_green` | counted as green |
| Blocking requirement, tier below floor | `HOLD` `work_artifact_insufficient_tier` | promoted because it is the best available |
| Gate source read fails | `HOLD` `work_source_error` | empty gate list read as no blockers |
| Policy unreadable at `base_sha` | `UNKNOWN` `work_source_error` | fallback to working-tree policy |
| Policy invalid | `HOLD` `work_policy_invalid` | ignored as if absent |
| Two records disagree | `HOLD` `work_contradiction` | one side silently preferred |
| Concurrent mutation | serialized under a per-item lock + `expected_version` | last-writer-wins |
| Crash between item and event | `HOLD` `work_ledger_gap` / `work_ledger_ahead`, or idempotent replay by `op_id` | the advanced state accepted unaudited |
| Event chain gap | `HOLD` `work_event_chain_broken`; later transitions not applied | reading past the hole to a later `ready` |
| Log present, metadata absent | orphan; surfaced | counted as evidence |
| Metadata present, log torn/absent | `HOLD` `work_artifact_log_hash` / `work_artifact_log_missing` | metadata trusted alone |
| Same run registered twice at a higher tier | `HOLD` `work_artifact_execution_conflict` | two independent corroborators |
| Future-dated or impossible timestamp | `HOLD` `work_implausible_timestamp` | fresh forever, or ordering the ledger |
| Bound commit unreachable after force-push | `HOLD` `work_revision_unreachable` | evidence silently rebound to the new head |
| Diff cannot be computed | `UNKNOWN` `work_revision_unresolvable` | "no changed paths" read as no requirements |
| Gates waiver on a required check | displayed; `work_waiver_not_authoritative` for a release blocker | treated as operator authority (ASSURANCE.md:109-114 forbids it) |

The unifying rule, stated once: **absence, corruption, staleness,
skipping, waiving without authority, and unknown are all non-green for a
blocking requirement.** C1's four fail-opens were four different ways of
violating that one sentence, and each row above is a test.

## Assurance Ingestion

D5 makes `assurance.py` artifacts readable by `work check` without making
assurance an authority. Roadmap §6 item 7 requires exactly this
separation.

Rules:

- Ingestion is **read-only**. Work never writes into
  `.agenttalk/assurance/`, never triggers a scan, and never modifies an
  assurance artifact.
- `assurance.py` writes its artifact with plain `Path.write_text`
  (`assurance.py:711`), **not** `_atomic.write_text`. A reader can
  therefore catch a torn file. Ingestion treats an unparseable assurance
  artifact as `UNKNOWN` with `work_source_error`. It never crashes, and
  absence is never pass.
- The attestation shape is `{"GOOD": …, "ROBUST": …, "SECURE": …,
  "reasons": [...]}` — keys uppercase, values lowercase. The value
  vocabulary is `good` / `unknown` / **`not_assessed`**; the third value
  is reachable for `ROBUST` and must be handled explicitly rather than
  falling into an `else` that means good.
- **`good` does not map to `satisfied`.** An assurance attestation is
  ingested as an artifact at tier `local_agent` **at most**, carrying the
  attestation and its `reasons` as detail. It is pre-review evidence.
  Mapping `good` → satisfied would silently turn a scan producer into a
  release authority, which is the thing the roadmap forbids.
- **There is no ingestion path to `local_operator`, and D1–D5 defines
  none.** An operator importing or confirming a scan an agent ran is not
  evidence that the operator ran it, and nothing in the assurance record
  distinguishes those two cases. `local_operator` requires a runtime
  adapter that binds actor, command, and exact inputs *when execution
  begins*; no such adapter exists here. An earlier draft allowed
  "`local_operator` when operator-run" on ingestion, which would have let
  an agent scan acquire the operator tier by passing through operator
  hands and satisfy a `local_operator` floor.
- **The assurance artifact does not carry work's bindings.** Its record
  has `schema_version`, `artifact_type`, `run_id`, `generated_at`,
  `profile`, `root`, `scanner`, `attestation`, `verdict_summary`, and
  `residual_risk`, plus `git_sha` / `git_dirty` / `changed_from` /
  `changed_to` on the scan result — and **no** `work_id`, `base_sha`,
  `diff_hash`, work `policy_hash`, verified producer, or trust tier.
  **Missing bindings stay missing.** Ingestion does not supply them from
  the ingesting context — an earlier draft said it did while also
  forbidding backfill, and both cannot hold; supplying a binding *is*
  backfilling it, whatever the source is called. An ingested assurance
  artifact is therefore `referenced` and raises
  `work_artifact_binding_missing` against any blocking requirement, and
  it becomes `local_agent` — never higher — only if the producing run
  itself recorded the work bindings at execution time.
- **Tier comes from the ingestion path, never from the artifact's
  content.** A `profile: "release"` field, a `GOOD` attestation, a file
  sitting under a CI-looking directory, a producer string containing
  "github", or a `CI=true` environment variable are all *claims*. None of
  them promotes an ingested scan to `automation_ci`. Origin tier is
  assigned only by a trusted execution or transport adapter, and no such
  adapter exists in D1–D5, so every ingested assurance artifact is
  `local_agent`. `local_operator` is unreachable through ingestion by
  construction — see the rule above.
- `unknown` and `not_assessed` are non-green for a blocking requirement,
  and their `reasons` strings are surfaced verbatim in the hold detail so
  an operator sees *why* — the reasons are already formatted as
  `"SECURE: missing executed security, deps evidence"` and carry more
  information than a boolean.
- The honest limit: assurance's own attestation is fail-honest per
  dimension, which is a strength, but it attests a **scan run**, not the
  correctness of the code. Work reports it as one evidence input among
  several and never as a verdict.

## Safety Invariants

Roadmap §7's Hard HOLDs, restated as assertions a test can make. Each is
a named test in D1–D4's acceptance set.

1. **No hidden contradiction.** Given records that disagree, `work check`
   returns `HOLD` with `work_contradiction` naming both sources.
   *Test:* construct an item with `terminal: "delivered"` and no lane
   delivery artifact; assert the code and that neither record is
   silently preferred.
2. **Artifacts are immutable.** Writing an artifact to an existing
   `artifact_id` fails. *Test:* attempt a second write; assert refusal
   and that the original bytes are unchanged.
3. **Prose never gates.** No message body content is an input to
   `compute_verdict`. *Test:* a review-result whose body says "approved"
   but whose `meta.status` is `rejected` yields
   `work_review_rejected`.
4. **Evidence binds exactly.** An artifact missing any of `work_id`,
   `base_sha`, `head_sha`, `diff_hash`, `policy_hash`, `producer`,
   `trust_tier` cannot satisfy a required check. *Test:* one omission per
   field, seven assertions.
5. **Local agent evidence never greens a release blocker.** *Test:* a
   `local_agent` artifact against a release-blocking item with default
   policy yields `work_artifact_insufficient_tier`.
6. **Branch policy cannot judge itself.** *Test:* a branch that relaxes
   `code-policy.json` still evaluates under the base policy and raises
   `work_policy_changed_in_branch`.
7. **Failure never normalizes to pass.** *Test:* torn artifact, absent
   artifact, non-zero exit, `skipped` status, and unreadable source each
   assert non-green.
8. **One corrupt item does not brick work.** *Test:* garbage in one item
   file; `work list` returns every other item and a bounded error row for
   the corrupt one.
9. **Corrupt state is never overwritten.** *Test:* mutate a corrupt item;
   assert refusal and byte-identical file contents afterward.
10. **All matching policy entries must be satisfied.** *Test:* a path
    matching two globs with different required checks holds until both
    are satisfied; satisfying only the broader one still holds.
11. **Tiers do not collapse.** *Test:* `work show --json` reports the
    per-artifact tier; no field reduces the set to a single boolean.
12. **Crash recovery converges.** *Test:* inject process death at each of
    the **six** kill points in the Crash And Recovery Protocol; assert
    every replay reaches the same state or the same named HOLD, and that
    re-running recovery is a no-op. Additionally assert `work check`
    **before** recovery holds with `work_recovery_required` for rows (a)
    and (b) — convergence-after is not the same property as safe-before.
13. **The ledger is one record per append under contention.** *Test:*
    monkeypatch `os.write` to return short writes, barrier two writers on
    one ledger, and assert two parseable records with distinct `seq` and
    an intact `prev_hash` chain — no interleaving, no duplicate `seq`.
14. **One execution corroborates once.** *Test:* register two artifacts
    derived from a single run; assert the independence count is 1 and
    that a policy requiring two independent checks still holds.
15. **`work check` is inert.** *Test:* hash `.agenttalk/` before and
    after; call the evaluator twice on the same snapshot; assert
    identical verdicts and byte-identical state.
16. **Requirements survive wrong-scope evidence.** *Test:* a change under
    `src/payments/**` with only an artifact whose `covered_globs` is
    `["src/ui/**"]` yields both `work_artifact_wrong_scope` and
    `work_required_check_missing` — never a vacuous pass from an empty
    filtered list.
17. **Bus records are never ordered by id.** *Test:* two conflicting
    bound review-results at one revision, authored so the *rejecting*
    one has the lower message id; assert `work_review_conflict` and that
    swapping the ids does not change the verdict.
18. **An unbound review cannot satisfy.** *Test:* an approved
    review-result with no `reviewed_head_sha` yields
    `work_review_unbound` and leaves the requirement unsatisfied.
19. **Record state never reads the verdict.** *Test:* a fresh `draft`
    item with no lane and an unresolvable revision renders
    `draft` + `UNKNOWN`, **not** `blocked`; and an item whose records
    advance past its last `state_asserted` event does not become a
    permanent HOLD. Asserts the two axes are independent and the
    projection is total.
20. **No waiver satisfies a release blocker through a direct work-side
    gate.** *Test:* `gate waive` an active waiver over a required check
    on a release-blocking item; assert `work_waiver_not_authoritative`
    and that the verdict stays non-GO. Scoped deliberately: the
    waiver-backed close route is undetectable and is not asserted here.
21. **Lineage cannot mint independence.** *Test:* two artifacts derived
    from one root — one reformatted, one truncated — yield independence
    count 1; a `derived_from` cycle and a root mismatch each yield
    `work_artifact_lineage_invalid`.
22. **The ledger tail is committed.** *Test:* rewrite the last event's
    `actor` in place; assert the chain still verifies internally but
    `ledger_head` mismatches and the verdict holds.
    **Narrowed deliberately:** replay rejection is claimed *only* for
    replays that violate a **named per-type precondition** (a duplicate
    `op_id`, a `lane_bound` for an already-bound lane, a `delivered` on a
    terminal item). A general fresh-`seq` replay is **not** rejectable
    with the specified schema, and the earlier unqualified claim was
    unimplementable: a re-assignment back to a previous owner is
    genuinely indistinguishable from a replay of the original
    assignment, and `ledger_head` commits both equally. Claiming a
    general property the schema cannot deliver would be exactly the
    overclaim this document keeps removing elsewhere.
23. **Imported assurance never reaches `local_operator`.** *Test:*
    ingest an agent-produced assurance artifact through an operator
    context; assert the tier is `referenced` (bindings absent) and that
    a `local_operator` floor stays unsatisfied.
24. **A non-GO source verdict holds even with an empty detail list.**
    *Test:* `gate_check = {"verdict": "HOLD", "required_gates":
    ["security"], "blockers": [], "gates": []}` with every other input
    passing; assert non-GO. Repeat for a close whose verdict is non-GO
    with no holds listed.
25. **Completeness is attested, not inferred.** *Test:* omit a
    verdict-bearing source from the manifest, and separately mark one
    `not_attempted`, each with `source_errors=[]`; assert
    `work_source_not_attempted` both times.
26. **The release baseline is non-vacuous.** *Test:* a release-blocking
    item with no policy, no matching rule, no close, no review, and an
    empty `required_gates` yields a hold for each missing element — not
    GO — **and is treated as release-blocking despite proving nothing**,
    since absence never exempts. Separately: a required gate green only
    via an active waiver yields `work_waiver_backed_gate`. There is no
    waiver-backed-close assertion, because work cannot see that fact —
    see the honest limit in The Release Baseline.
27. **Re-resolution never strengthens.** *Test:* reclassify a producer
    from `agent` to `operator` in the roster; assert every existing
    artifact keeps its bound `local_agent` tier and that a
    `min_tier: "local_operator"` floor stays unsatisfied.
28. **Retirement cautions, never demotes.** *Test:* retire a producer;
    assert their artifacts keep their tiers, carry `producer_retired`,
    and that no item holds solely because of the retirement.
29. **`external_attested` is unreachable in D1–D5.** *Test:* attempt to
    register an artifact claiming `external_attested`, ingest one via
    assurance, and set a gate with that source; assert none produces a
    satisfying result.
30. **The axes cannot disagree.** *Test:* assert
    `changes_requested · GO` is unreachable, and that both axes carry the
    same snapshot token.
31. **A generic operator attach cannot reach `local_operator`.** *Test:*
    an operator invokes `work artifact attach` on an agent-produced
    output; assert the registered tier is `local_agent`, that no path
    emits `local_operator` in D1–D5, and that a `local_operator` floor
    stays unsatisfied.
32. **Release classification is total and conflict fails closed.**
    *Test:* the full truth table — a release-class close with an
    all-`false` policy, a non-release close with a `release_scoped: true`
    rule (both yield release-blocking plus
    `work_release_class_conflict`), a legacy rule with `release_scoped`
    **absent** alongside a release-class close (release-blocking, not
    exempt), a changed path matched by no rule (release-blocking), and
    the only exempting case: a non-release close with every matched rule
    explicitly `release_scoped: false` and every path matched.

## Threat Model And Honest Limits

### What This Design Establishes

For a trusted team, on records written through the CLI: which revision
evidence was produced against, under which policy, by whom, at what
tier, and whether any of that has drifted from the current revision.
It makes stale, absent, mis-scoped, corrupt, and insufficient-tier
evidence machine-detectable rather than a matter of memory.

### What It Does Not Establish

- **Not a defense against a local writer.** Everything under
  `.agenttalk/` is writable by the same OS user. A hostile writer can
  author a work item, append events, and write artifacts with any
  content, including a recomputed `content_hash`. `content_hash` detects
  accidental corruption and casual edits, not a determined forger.
- **Not proof a command ran.** D1–D4 record a `command` that a producer
  claims to have executed. Nothing verifies execution. This is why
  `local_agent` cannot green a release blocker, and it is the honest
  reason the tier ladder exists at all.
- **Not proof the code is correct.** Every surface must say "evidence
  current and policy satisfied," never "code correct" — roadmap §8's
  named mitigation for the false-trust risk. A `GO` means the recorded
  evidence is current and sufficient under policy; it means nothing about
  whether the tests were good tests.
- **Not atomic with the action that follows.** Like `check --epoch` in
  the identity RFC, a `GO` can be invalidated by a commit landing
  immediately after the check. Consumers re-check immediately before an
  irreversible action and treat the residual race as they already do.
- **Not a same-user authorization boundary.** Per-item locks are
  coordination gates producing auditable evidence (DESIGN.md principle
  4). They prevent honest concurrent writers from losing updates.
- **Roster-based authority is routing metadata.** "Only the owner or a
  lead may deliver" is enforced against config that a local writer can
  edit. Real per-agent authority awaits the signing model the identity
  RFC scopes.

## Schema Versioning

This repo has no repo-wide versioning ADR, and its two existing
precedents disagree: `assurance.py:794` raises on
`schema_version != SCHEMA_VERSION` (exact-match fail-closed), while the
knowledge work shipped a versioned `knowledge-view-v1` envelope with an
`--output-schema legacy` escape.

**Decision: split by direction, because the two precedents are answers to
different questions.**

- **Persisted records we own — exact-match fail-closed.** Work items,
  events, and artifacts carry an integer `schema_version` that must equal
  the supported version exactly. An unrecognized version raises
  `work_unknown_schema_version` and blocks that record. These records are
  *verdict inputs*: a record this build cannot fully interpret must not
  be allowed to vote, and best-effort partial parsing of a safety input
  is how fail-opens are born. Following `assurance.py`.
- **Output projections — versioned envelope with a legacy escape.**
  `work show --json` and `work check --json` emit a `work-view-v1`
  envelope, with the same `--output-schema legacy` escape hatch the
  knowledge view established. These are *consumer surfaces*: a dashboard
  or script breaking on an additive field is a real cost, and a misread
  here causes display drift, not a false GO.

Rules:

- Forward compatibility is bought by making new **persisted** fields
  additive within a version, and bumping the version only when a field
  changes meaning. A field that changes meaning without a version bump is
  the failure this rule exists to prevent.
- An unknown version is never "upgraded in place." Migration, if ever
  needed, is an explicit operator command that writes new records and
  leaves the old ones readable.
- The honest cost of exact-match: a store written by a newer agenttalk
  blocks on an older one, rather than degrading. That is the intended
  trade for a verdict input, and it is stated here so nobody
  "fixes" it later by loosening the check.

## Migration And Compatibility

Migration is read-first and additive, matching the identity RFC's
discipline.

1. A store with no `.agenttalk/work/` or `.agenttalk/artifacts/`
   directory behaves exactly as today. Nothing is created until the first
   `work create`.
2. No existing file is modified by this feature. `gates.json`,
   `domains.json`, `closes/`, `lanes.json`, and the message store are
   read-only inputs.
3. `.agenttalk/code-policy.json` is optional. Its absence means no
   project-required checks, and every other requirement still applies.
4. No new message kind. Review binding uses existing `review-request` /
   `review-result` threads, so old clients neither break nor need
   upgrading.
5. New JSON fields are ignored by older readers as ordinary data. Work's
   own readers reject unknown *versions*, not unknown *fields*.
6. The single `cli.py` touch is one subparser plus one dispatch line, per
   the work package's low-collision rule. All logic lives in the new
   modules. That insertion is coordinated with the primary team rather
   than merged unilaterally.
7. One change outside this team's namespace is a **request, not an
   edit**: extending the reset warning in `cli.py`. It is filed through
   the operator.
8. **The `gates` enum change is deferred, not requested.** An earlier
   draft asked the primary team to add `external_attested` to
   `gates.VALID_EVIDENCE_SOURCES` / `BLOCKER_GREEN_SOURCES`. That would
   make the tier *reachable* through `gates.set_gate`, which trusts a
   selected allowed source plus evidence refs, **before** any attestation
   adapter or claim-scope model exists — turning a deferral into a
   producer path and letting a caller label a blocker green as externally
   attested. The enum change waits for the D6 attestation decision, and
   until then `external_attested` stays unreachable and non-satisfying
   everywhere, pinned by a test.

## Recommended Phases

### Phase D1: This RFC

Implement: nothing. D1 is the design gate.

Do NOT implement: any module, CLI surface, or storage directory before
this RFC is review-clean. Design-first is the work package's §7 rule and
the reason this document exists.

### Phase D2: Work Item MVP

Implement:

- `work.py` + `work_store.py`: per-item records, the append-only event
  ledger under `store._config_lock()`, per-item locking for every
  read-modify-write.
- The crash protocol in full: `item_seq` as `expected_version`,
  `pending_op` / `op_id`, ledger `seq` + `prev_hash`, `ledger_head`
  validated on **every item read**, and idempotent recovery as a separate
  command. This lands in D2 rather than later
  because retrofitting a chain onto an existing ledger means rewriting
  history, which the append-only rule forbids.
- `work create|list|show|status|assign|start|deliver|abandon`.
- The derived-state projection table, with terminal states on the item.
- Links to lanes, domains, threads, gates, closes, onboarding, notes.
- Fail-safe per-item reads, corrupt-item mutation refusal, and the
  one-corrupt-item-does-not-brick-work test.

Do NOT implement: artifacts, policy, tiers, or `work check`. `work
status` in D2 projects lifecycle state only and reports `UNKNOWN` for
anything requiring evidence, rather than a provisional verdict that would
have to be un-taught later.

### Phase D3: Evidence Registry MVP

Implement:

- `evidence.py`: write-once artifacts with create-if-absent,
  `content_hash`, exact input binding, bounded logs with `log_hash` /
  `log_truncated`, `redaction_status`.
- The log-first / metadata-last pair protocol and its seven-state
  validation on every consumption.
- `root_execution_id`, `derived_from`, and independence counting.
- Derived `trust_tier` with the mapping table, plus revalidation.
- Stale-at-head detection reusing the knowledge staleness **shape**
  (`{stale_reasons, caution_flags, hard_stale}`) — the data structure
  only, not D-6's anchor-relative rule. A head mismatch is a hard stale
  for evidence, where it would be a caution for a knowledge note.
- `work artifact attach|list|show`, and readback validation before any
  advancement (C5).

Do NOT implement: command execution. D3 records an argv that a producer
claims it ran; it never runs one. Also no redaction scanning — ship
`not_scanned` honestly. **And no `local_operator` emission from any
path**: the tier mapping table is the eventual rule, not a D3 capability,
so a generic attach caps at `local_agent` whoever invokes it. Emitting
`local_operator` requires the runtime adapter, which is not in D3.

### Phase D4: Pure Work Check

Implement:

- `work_check.py`: `compute_verdict` as a pure function over resolved
  bundles, with the CLI owning all I/O.
- The full HOLD-code table, one literal-asserting test per code.
- `GO` / `HOLD` / `UNKNOWN` with `ok` true only for `GO`.
- The contradiction pass and `source_errors`.
- Policy loading, glob matching with all-matching semantics, policy-hash
  binding, and base-policy evaluation.

Do NOT implement: assurance ingestion, CI adapters, merge automation, or
any dashboard surface.

### Phase D5: Assurance Ingestion (Stretch)

Implement: read-only ingestion of assurance artifacts as `referenced`
evidence — or `local_agent` only where the producing run itself recorded
the work bindings at execution time — plus torn-file tolerance yielding
`UNKNOWN` and verbatim `reasons` in hold details.

Do NOT implement: **any ingestion path to `local_operator`** (there is
none; an operator importing or confirming an agent's scan is not evidence
the operator ran it), any mapping from `good` to satisfied, any
backfilling of missing bindings from ingesting context, any write into
the assurance namespace, and any scan triggering.

### Phase D6 Gate: Deferred Decisions

These are deliberately **not** answered by this RFC. They are scheduled
here rather than left floating, and each is decided before the phase that
needs it — not during it.

1. **Does `external_attested` need a `gates.py` counterpart before D3
   ships, or can it stay work-side-only?** Decide with the primary team
   when the `gates.py` request is filed. Until then no producer emits it.
2. **Does `work check` need its own scope, or does it always inherit the
   item's `gate_scope`?** Decide at D4, when the first multi-scope item
   exists. Guessing now would likely produce the mis-scoped-gate class
   C1 already paid for.
3. **What is the retention policy for artifacts and logs?** Write-once
   plus survive-reset means unbounded growth. Decide at D3 alongside log
   caps; a `work gc` that deletes evidence is itself a safety surface and
   deserves its own review.
4. **Should `work` expose a merge-base helper, or require callers to
   supply `base_sha`?** Decide at D4 with the base-policy evaluation
   implementation, since both need the same git access.
5. **Does the dashboard Work view need a projection contract frozen in
   this RFC?** Deferred to Q2 by the work package (`web.py` is outside
   the ownership boundary). The `work-view-v1` envelope is designed to be
   that contract when the time comes.
6. **What exactly does an external attestation attest?** Signing or
   uploading an artifact proves custody of a blob, not that the check
   ran. A container's tier must not become transitive to the claims
   inside it, so `external_attested` needs an attestation-scope model
   naming which claims are verified — blob existence, execution, inputs,
   or result. Deferred because `external_attested` has **no producer in
   D1–D5**, so the surface does not exist yet; the untrusted-fields rule
   in Evidence Tiers covers the part that does. This decision is forced
   by whichever phase first introduces an attestation producer, and it
   blocks that phase.
7. **The close-provenance envelope — BLOCKS D4.** `record["final"]`
   stores blocker *names* only (`close.py:1083-1091`), so work cannot see
   that a close's GO rested on a waived gate, an `ack.override`, a
   `signoff_overrides` skip, or a remediation resolved through a
   non-required waived gate. `work check` lands in **D4**, so unless a
   bound envelope (pre-publish gate rows + override markers + exact
   scope/revision binding) exists first, D4 ships with that route
   admittedly invisible. This is a scheduled blocker, not a note: D4 may
   not ship until it is resolved or an operator explicitly accepts the
   residual. It needs `close.py` changes outside this team's boundary, so
   it is filed through the operator.
8. **What is the bounded clock skew, and against which clock?** The
   `work_implausible_timestamp` rule needs a concrete bound. Decided at
   D3 with the first real producer, because picking a number now without
   measuring agent-host clock spread would be arbitrary.

## Acceptance Criteria For The RFC

This RFC is ready to drive implementation when reviewers agree on the
contested points — the ones where the source documents left a genuine
choice and this document made one:

- **Storage placement.** `.agenttalk/work/` and `.agenttalk/artifacts/`
  are top-level and therefore survive `reset`; the durability-boundary
  test and the `reset` warning are extended accordingly; and
  `state/work-heartbeat/` is unrelated liveness telemetry that reset
  still clears.
- **Schema versioning is split by direction.** Exact-match fail-closed
  for persisted verdict inputs, versioned envelope with a legacy escape
  for output projections — rather than one rule everywhere.
- **`UNKNOWN` is non-advancing.** `ok` is true only for `GO`, and
  `UNKNOWN` loses to `HOLD` when both apply.
- **`trust_tier` is derived, not asserted**, mapping onto
  `gates.BLOCKER_GREEN_SOURCES` rather than competing with it, and
  revalidated at verdict time rather than trusted from persistence.
- **`external_attested` has no producer in D1–D5**, and its `gates.py`
  extension is **deferred** — it becomes a request to the primary team
  only once the D6 attestation-scope decision lands, because requesting
  it sooner would make the tier reachable through `gates.set_gate` before
  any adapter can verify it.
- **`local_operator` is likewise unreachable in D1–D5**, because the only
  path to it would be an operator attaching an agent's output, which is
  confirmation rather than execution.
- **Work check adapts each source's actual return shape** rather than
  assuming the verdict producers match. `close.compute_verdict` and
  `lanes.compute_verdict` share `{verdict, holds, ok}`, but work consumes
  a *published* close through its stored `record["final"]`
  (`verdict`, `gate_verdict`, `blockers`, `by`, `at`, plus the close's
  `scope` and `revision`) — a different shape again, and the only one
  reachable for a published close.
- **Lane isolation uses the artifact-side vocabulary**
  (`verified` / `advisory_unisolated` / `unverified`) and requires
  `verified`, matching the existing hard reject in
  `validate_delivery_artifact(..., require_isolation=True)`.
- **Policy is evaluated at the merge base** as the union of base and
  candidate; a branch may only make itself stricter, and a **relaxation
  holds with no escape in D1–D5** — detection alone is
  not the mitigation.
- **All matching policy globs must be satisfied** (D-11), with the
  matched glob persisted rather than the raw path.
- **Status and verdict are never persisted**, and contradictions are
  always `HOLD` with both sources named, never resolved by preference.
- **Terminal states are the one persisted lifecycle fact**, justified by
  needing to survive a corrupt ledger.
- **Every persisted event field is bound xor curation-mutable**, pinned
  by a construction test, with nothing verdict-bearing in the mutable
  set.
- **D1–D5 execute no commands.** `command` is recorded, never run, and
  the runner stays deferred behind roadmap §7's Hard HOLD.
- **The item is authoritative for state, the ledger for history, and a
  `GO` needs both** — with `pending_op` + `op_id` + `seq` + `prev_hash`
  making every one of the six crash points converge idempotently, with
  `work_recovery_required` covering a check run before recovery.
- **`seq` orders everything; timestamps order nothing**, and an
  implausible producer timestamp disqualifies the artifact rather than
  merely being noted.
- **Independence is counted by `root_execution_id`, not by record**, so
  two adapters over one process corroborate once — and a *claimed* root
  cannot satisfy `min_independent_roots > 1` at all in D1–D5.
- **Policy is the union of base and candidate**: a branch may make itself
  stricter immediately with no waiver, while a **relaxation holds with no
  escape in D1–D5**, because the authenticated waiver record it would
  require has not been built.
- **Work owns no deletion path.** Cleanup is requested from `lanes.py`,
  which remains the sole authority on whether it is safe.
- **Record state and gate verdict are orthogonal axes**, neither derived
  from the other, neither allowed to mask the other, resolved from **one
  snapshot**, with `changes_requested · GO` impossible by construction
  and `ready` / `blocked` presentation compositions rather than rungs.
- **Every verdict-bearing source must return exact GO**, and the source
  manifest attests that each was attempted — an empty `blockers` list or
  an empty `source_errors` proves nothing.
- **A release-blocking item needs a non-vacuous baseline** (policy, path
  coverage, published close, bound review, non-empty green gates), and
  waiver-backed **gates** are rejected rather than inherited — while a
  waiver-backed **close** is admittedly **undetectable** with the fields
  `record["final"]` carries, which is a scheduled D6 blocker on D4 rather
  than a closed route. Omission must not become the silent waiver.
- **Release classification is derived by a total truth table**, never
  stored and never disjunctive: any present release signal wins,
  disagreement is release-blocking, and an absent `release_scoped` is
  unknown rather than false.
- **Tier re-resolution may only weaken**; the bound tier caps the
  effective tier, and producer retirement is a caution, never a demotion.
- **`item_seq` is a record version, not a ledger position.** Crash
  classification keys on `pending_op` + `ledger_head`.
- **Claimed execution roots cannot satisfy independence**; that needs a
  launch-time nonce which does not exist in D1–D5.
- **No waiver satisfies a release-blocking requirement through a direct
  work-side gate in D1–D5**, because `gates.waive_gate` stores
  unauthenticated free text with no work/head/policy binding and
  ASSURANCE.md:109-114 forbids treating it as operator authority. The
  waiver record work *would* need is specified so the hardening has a
  target. The waiver-backed **close** route remains open and blocks D4.
- **Independence is counted by distinct transitive `root_execution_id`**,
  with descendants inheriting their ancestor's root, and roots derived
  from execution identity rather than captured output.
- **`ledger_head` on the item commits the ledger tail**, which
  `prev_hash` alone cannot.
- **Imported assurance is capped at `referenced`/`local_agent`.** There
  is no ingestion path to `local_operator`, and missing bindings stay
  missing rather than being supplied from ingesting context.
- **Work never orders bus records.** Review state resolves by revision
  binding; same-revision disagreement is `work_review_conflict`. Message
  ids are not a cross-process clock, so no projection may treat them as
  one.
- **An unbound review cannot satisfy a blocking requirement.** Work reads
  additive `meta.work_id` + `meta.reviewed_head_sha` on existing
  `review-result` messages — a new meta contract, not a new message kind,
  and named here rather than left for D4 to invent.
- **Review lenses stay in `close.py`.** `code-policy.json` carries a
  `require_close_lenses` boolean, not a competing lens list.
- **A head mismatch is a hard stale for evidence**, `diff_hash` is a
  diagnostic rather than a satisfier, and diff-equivalent reuse is
  available only as a new H2-bound artifact tiered no stronger than its
  verifier — never for a release-blocking `automation_ci` requirement.
  This is deliberately stricter than D-6's rule for knowledge notes.

Once ratified, the safest first build is D2 exactly as scoped, with the
corrupt-item and one-item-does-not-brick-work tests written **failing
first** against the unimplemented module — the work package's §7 rule,
and the only thing that makes an acceptance test non-tautological.

## Pre-Mortem Traceability (codex-dev, 2026-07-18)

Dispositions for all 34 findings in the independent adversarial
pre-mortem. Counts: **6 ADDRESSED · 27 FOLD · 1 DEFER · 0 REJECT.**

**Amended four times on 2026-07-18, every time by cross-family review.**
(4) Panel round 2 (all three REJECTED, bound to commit `4dc795c`) found
15 P0 in three classes. The design gaps: the resolved bundle admitted GO
from a malformed source (an empty `blockers` list with a `HOLD` verdict),
omission worked as a silent waiver for release-blocking items, the two
axes could be composed from different snapshots, claimed execution roots
are not independent, and the round-1 `producer_class` fix had no channel
to deliver a roster and no stated direction. Five of the P0s were
**stale text contradicting the document's own new rules** — the class
that matters most, because a phase directive contradicting a normative
rule looks fixed to a reader and reproduces the bug in the implementer.

**Amended three times before that, every time by cross-family review.**
(3) Panel round 1 (`claude-rev` REJECTED, `codex-sec` NEEDS-INFO,
`codex-rev` NEEDS-INFO on artifact drift) found 11 P0s. The two
structural ones are recorded against the rows they touch: the derived
state table was **circular** with the contradiction pass
(`record_state = f(verdict)` while `verdict = g(record_state)`), and
`blocked` sat above every record-shaped row so a fresh draft would have
rendered `blocked` once `work check` existed. Record state and gate
verdict are now orthogonal axes. Findings with no pre-mortem number —
the waiver authority gap, execution lineage, assurance ingestion tiers,
the review binding envelope, the unhashed ledger tail, and ten P1/P2
items — are folded into the body.

**Amended twice before that, both times by cross-family review.**
(1) Finding 20 was originally a partial decline; codex-dev contested it
with source citations, the decline was wrong, and the RFC was narrowed.
(2) claude-rev then found that the finding-18 fix was *itself* a
clock-ordering bug — it delegated bus ordering to thread derivation,
which is producer-clock ordering across processes. Both rows record the
reversal in place. A traceability matrix that quietly rewrites its own
history is worth less than one that shows where it was corrected, and
"my fix for the ordering bug contained the ordering bug" is precisely
the kind of thing a reviewer should be able to see.

Findings raised by claude-rev's review (`rq-6d0c9c2b3410`) that were not
in the codex-dev pre-mortem are folded into the body rather than
numbered here: the review binding contract, `covered_globs` on
artifacts, the satisfaction-record schema, and dropping
`required_lenses` in favour of close's model.

An `ADDRESSED` row means the cited sentence already carried the normative
force before the fold-in pass. Where the pre-mortem's phrasing was
sharper than the draft's, the row is `FOLD` even if the draft gestured at
the idea — a reviewer having to infer an invariant is the same as it not
being there.

| Finding | ID | Disposition | RFC section / rationale |
|---|---|---|---|
| 1 | HH-CONTRADICTION | FOLD | Work Check → "Facts are joined on an exact key"; Contradictions → per-field ownership + cases A/B/C. Added `work_gate_stale` / `work_close_stale`; the draft had one blanket contradiction code and no join key. |
| 2 | HH-ARTIFACT-MUTATION | FOLD | Artifact Schema → create-if-absent, "idempotent **only** when the incoming content is byte-identical", `supersedes` field. Draft said "refusing to write when the target path exists" with no idempotency or supersede link. |
| 3 | HH-UNTYPED-AUTHORITY | ADDRESSED | Safety Invariants #3 "Prose never gates" — "No message body content is an input to `compute_verdict`", with the test that a body saying "approved" under `meta.status: rejected` yields `work_review_rejected`. Plus Evidence Tiers: `referenced` never satisfies. |
| 4 | HH-INCOMPLETE-BINDING | FOLD | Artifact Schema → "**A missing binding field is never backfilled from current state**"; added `ingested_at` as the one locally-observed time. Draft required the fields but never forbade backfilling them. |
| 5 | HH-LOCAL-RELEASE | ADDRESSED | Policy Boundary — "Absent `release_min_tier` defaults to `automation_ci` … the default is the strict one, so forgetting to write it fails closed." Plus Safety Invariants #5. |
| 6 | HH-SELF-JUDGING-POLICY | FOLD | Policy Boundary → union of base and candidate; stricter candidate rules apply immediately with no waiver; **relaxation holds with no escape in D1–D5**, because the authenticated waiver it would require does not exist. Draft evaluated base-only and bound one hash. |
| 7 | HH-RUNNER-EXECUTION | ADDRESSED | Artifact Schema — "`command` is a structured argv list, never a shell string. D1 defines the field; D1–D4 do not execute it." The runner is deferred behind roadmap §7's Hard HOLD, so the attack surface does not exist in scope. |
| 8 | HH-FAILURE-NORMALIZATION | FOLD | Work Check → closed outcome enum (`pass`/`fail`/`skipped`/`timeout`/`malformed`/`adapter_error`/`unavailable`/`missing`), "**Only `pass` satisfies** … no `else` branch". Draft lumped these into one code. |
| 9 | HH-DESTRUCTIVE-CLEANUP | FOLD | Lifecycle → "**Work never deletes a worktree.**" The draft was silent on cleanup, which is worse than wrong — an implementer would have invented one. |
| 10 | HH-TIER-DISPLAY | ADDRESSED | Safety Invariants #11 "Tiers do not collapse" with its test; dashboard itself is out of scope (Q2, `web.py` outside the boundary). |
| 11 | HH-PLATFORM-CLAIM | FOLD | Evidence Tiers → platform claims need executed target-platform evidence; skipped stays UNKNOWN; expressed via distinct opaque `check_name`s. Draft had the mechanism but never stated the claim rule. |
| 12 | GATE-ANALOG-CORRUPT-OVERWRITE | FOLD | Corrupt Items Block Their Own Mutation (item side was already there) **+** new artifact-analogue clause: an unreadable existing artifact is "never repaired in place and never reused." |
| 13 | GATE-ANALOG-LOST-UPDATE | FOLD | Work Item Schema → `item_seq` as `expected_version`; Corrupt Items → lock spans the full protocol including the event append. Draft had the lock but no version, so an unlocked writer still won. |
| 14 | GATE-ANALOG-SCOPE-DROP | FOLD | Work Check → "**Requirements are derived first, evidence is matched second**", naming the `all([])` vacuous-truth trap; wrong-scope emits two holds. |
| 15 | GATE-ANALOG-SKIPPED-BLOCKER | FOLD (amended) | HOLD table `work_required_check_not_green` + Failure Modes row. *Amended after round 2:* `waived` and `unknown` were claimed covered here while being absent from the closed outcome enum, so an implementer had no branch for two statuses `gates.VALID_STATUSES` actually emits. Both are now enum members, with out-of-enum values normalizing to `malformed`. Was ADDRESSED; the claim was not true as written. |
| 16 | GLOB-WINNER-BYPASS | FOLD | Policy Boundary → longest-prefix now refused **by name**, with the reason globs are not totally orderable (`src/**/secret/*.py` vs `src/api/**` intersect without nesting) and duplicate-normalized-glob rejection. |
| 17 | GLOB-STALE-SATISFACTION | ADDRESSED | Policy Boundary — "Persisted satisfaction is **revalidated at verdict time** … does not stay satisfied by inertia", plus requirements-first derivation, so a newly-added nested rule surfaces as unsatisfied. |
| 18 | TIME-FUTURE | FOLD (amended twice) | Event Model → "**`seq` orders the ledger; timestamps never do**"; Work Check → `work_implausible_timestamp`. The original draft's lifecycle table said "latest", which was the bug. *The first fix was itself incomplete:* it delegated bus-record ordering to `threads.derive_threads`, which sorts by message id (`threads.py:596`) where `_new_id` is monotonic only within one process (`store.py:6085-6094`) — producer-clock ordering under another name. claude-rev caught it. Work now orders bus records **not at all**, resolving review state by revision binding and emitting `work_review_conflict` for same-revision disagreement. |
| 19 | ID-NULL-UNRESOLVABLE | FOLD | Work Check → `work_revision_unresolvable`; "An empty requirement set from a failed diff is the same vacuous-truth bug as `all([])`, reached by a different road." |
| 20 | ID-REBASE-FORCE-PUSH | FOLD (in full) | Artifact Schema → the **exact-key join is authoritative** (`(base_sha, head_sha)` mismatch = not current); ordinary head move is a `stale_reason`, content-identical rebase is the named caution `rebased_identical_diff`; caution is **insufficient for release-blocking** (re-execution required) and may be accepted for non-blocking; rebinding stays an explicit event. *Revision history — dispositioned three times:* first a partial decline on D-6 grounds; codex-dev contested and I conceded; then claude-rev and codex-sec independently overturned it again with a sharper refutation, which is the one now in the doc. **D-6's analogy fails on its own terms** — it rejects HEAD-relative staleness because unrelated commits would empty the knowledge layer, but work evidence binds one `base..head` pair and unrelated commits do not move it, so the noise problem never arises. Decisive: a test result is a property of base+patch, not of the patch. My cost-asymmetry argument was true but secondary. |
| 21 | ID-DUPLICATE-LANE-CLAIM | FOLD | Lifecycle → "**A lane may be claimed by at most one non-terminal work item**", serialized on `(lane_id, lane_generation)`, with `work_lane_conflict` holding *every* claimant rather than picking the older. |
| 22 | ID-LANE-DISAPPEARS/REUSED | FOLD | Work Item Schema → `lane_generation`; Lifecycle → live-lane-with-matching-generation for non-terminal items, delivery artifact for terminal ones; `work_lane_generation_mismatch`. |
| 23 | CRASH-ITEM/EVENT-TRANSACTION | FOLD | New **Crash And Recovery Protocol**: item authoritative for identity/state, ledger for history, `GO` requires both; write order with `pending_op`; all **six** kill points (a)–(f) given stated outcomes across three published item images; recovery idempotent by `op_id`; `work_recovery_required` covers a check run before recovery. |
| 24 | CRASH-TORN-ITEM/LEDGER | FOLD | Event Model → a `seq` gap or broken `prev_hash` raises `work_event_chain_broken` and "transitions after the break are not applied". Draft surfaced malformed lines but would have read past them to a later `ready`. |
| 25 | CRASH-CONCURRENT-JSONL | FOLD | Event Model → lock spans tail inspection, `seq` allocation, append, and fsync, citing `_jsonl.py:23-29` / `:66-74`. **Verified and strengthened:** `_write_all` really is a `while remaining:` loop over `os.write`, *and* `append_record` does a separate `lseek`+`read` tail probe with a possible standalone delimiter write — two race windows, not one. |
| 26 | CRASH-ARTIFACT-PAIR | FOLD | Artifact Schema → **Torn Reads And The JSON/Log Pair**: log written and hash-validated first, metadata published last as the commit marker; all seven states (a)–(g) tabulated; both files revalidated on every consumption. |
| 27 | CONTRADICTION-SEMANTICS | FOLD | Contradictions → cases A, B, C spelled out, with "most recent" and "most pessimistic" each rejected by name and reason (clock control; permanent poisoning of later valid revisions). |
| 28 | TIER-ASSURANCE-INGEST | FOLD | Assurance Ingestion → the artifact's actual field list, confirmed against `assurance.py:2164-2189`, carries no work bindings; tier comes from the ingestion path, never from `profile` / attestation / path / producer string / `CI=true`. |
| 29 | TIER-WAIVER-LAUNDERING | FOLD (partial — close route open) | Execution Tiers → a waiver never mutates evidence, and no waiver satisfies a release-blocking requirement **through a direct work-side gate**. The **close** route is NOT closed: `record["final"]` carries blocker names only, so a waiver-backed close is undetectable; there is no `work_waiver_backed_close` code, and D6 item 7 blocks D4 on a close-provenance envelope. *Amended after codex-sec:* the first fold asserted a typed waiver "scoped to one requirement at one head and policy hash" — a record that does not exist. `gates.waive_gate` (`gates.py:143-181`) stores `{operator, date, reason, scope, expires}` with `operator` as unauthenticated free text and no work/check/head/policy binding, `_gate_verdict` (`:311-317`) makes any active waiver non-blocking, and ASSURANCE.md:109-114 forbids treating that path as operator authority. The required record is now specified as a target for the hardening, and until it lands the close route remains open and blocks D4. |
| 30 | TIER-REREGISTRATION | FOLD (amended twice) | Execution Lineage → `root_execution_id` derived from **execution identity** (command, cwd, base, head, policy hash, producer, started_at), explicitly not captured output, with `work_artifact_execution_conflict` on a tier/origin change. `content_hash` alone cannot do this: the record hashes its own `artifact_id`, so two registrations of one run hash differently by construction. *Round 2:* codex-sec showed the replacement is still **self-asserted** — every input is a producer claim, so a sibling adapter can mint a fresh root with a plausible `started_at`. `min_independent_roots > 1` is therefore unsatisfiable by claimed roots in D1–D5 (`work_independence_unverifiable`); it needs a launch-time nonce that does not exist. |
| 31 | TIER-FAKE-CORROBORATION | FOLD | Execution Lineage → "**Corroboration counts distinct `root_execution_id` values, not records**"; `derived_from` lineage; a derived artifact "can never corroborate its own ancestor." |
| 32 | TIER-CONTAINER/SIGNER-SPOOF | DEFER | D6 gate item 6, forced by whichever phase first introduces an attestation producer. Partly covered now: Evidence Tiers makes `producer`/`producer_class`/`trust_tier` untrusted input recomputed at registration, and `external_attested` has **no producer in D1–D5**, so the transitive-container surface does not yet exist. The attestation-scope model is what remains. |
| 33 | TIER-OPERATOR-CONFIRMATION | FOLD | Execution Lineage → "**Operator confirmation is not operator execution**"; confirmation is a separate typed acknowledgement, never a `producer_class` rewrite. |
| 34 | PURE-CHECK | FOLD | Work Check → "**The evaluator writes nothing**" — no `last_checked`, no cache, no ambient clock or git read; recovery is a separate command "so no repeat invocation of `work check` can turn a HOLD into a GO." Safety Invariant #15 pins it with a before/after hash. |

The pre-mortem's closing note is accepted in full: `_atomic`'s latched
Windows direct path is explicitly non-crash-atomic and
`_jsonl.append_record` explicitly delegates serialization to callers, so
this RFC cannot cite either helper as satisfying findings 23–26. The
Crash And Recovery Protocol, the ledger lock span, and the artifact pair
ordering are the additional protocol those findings demanded.
