# Research: 0.14.0 Operator Safety

Phase 0 output. There are no open clarifications: design decisions were
made in two cross-reviewed Claude/Codex consults (agenttalk threads
535a091f, 2293cabd; bundle review 52476b64) and grounded against the
v0.13.0 source by a 7-reader parallel verification fleet (2026-06-05).
This file consolidates those decisions with rationale, alternatives, and
the verified code baseline each WP builds on. Line numbers reference
v0.13.0 (`4a2064f`) and may drift; symbol names are the stable anchors.

## D1 — Rescind is a first-class kind, not a control kind

- **Decision**: add `rescind` to `KNOWN_KINDS` (store.py:39-61); do NOT
  add it to `CONTROL_KINDS` (store.py:67, currently `{composing}` only).
- **Rationale**: it affects thread state and must be transcript-visible /
  auditable (band: "transcript IS the provenance"). Control kinds are
  hidden from default recv — wrong for an event that changes what other
  messages mean.
- **Alternatives considered**: control kind (rejected: hidden audit
  trail); meta flag on a plain message (rejected: invisible to kind-based
  filters, weak validation hook); message deletion/edit (rejected
  outright: C-002 history immutability — breaks HMAC, cursors, replay).

## D2 — Supersession ordering rule

- **Decision**: a thread is superseded when any **valid** rescind whose
  `meta.request_id` matches the thread is **newer (by message id) than
  the thread opener** (or than `meta.target_msg_id` when pinned). Only a
  rescind whose sender is the thread's **requester** counts. First valid
  rescind decides; later duplicates are idempotent no-ops for state.
- **Rationale**: message ids are already the bus's total order
  (lexicographic, store `_new_id`); deciding at derivation time keeps
  `derive_threads` a pure function of valid messages (threads.py:19-23) —
  no new load-bearing state (C-004). Requester-only authority prevents a
  worker from cancelling its own obligations.
- **Alternatives**: latest-message-wins regardless of sender (rejected:
  lets the executor self-cancel); epoch counters at send time (deferred
  to RFC #19 — would be the bus's first cross-message ordering rule,
  qualitatively new); wall-clock ordering (rejected: ids already encode
  order; clocks lie).
- **Code baseline**: terminal branch slots into `_classify_event`
  (threads.py:52-90) parallel to the proposal-response terminal
  (threads.py:78-80); the manual-closure override precedent is
  closed_rids at threads.py:360-361.

## D3 — `check` is the barrier; rescind alone is notification

- **Decision**: ship `agenttalk check --for A --to-request RID` returning
  current/superseded/unknown with distinct exit codes (0/3/4; see
  contracts/cli-surface.md), plus the documented contract "run check
  immediately before any irreversible action".
- **Rationale** (Codex blocking objection, consult 2293cabd→535a091f):
  the executor-already-drained race cannot be closed by any inbox
  primitive — a wake-on-rescind only helps an agent still waiting. The
  executable gate is what makes this a barrier.
- **Alternatives**: only wake-on-rescind (rejected as incomplete); global
  send-time epoch validation (deferred to #19).
- **Code baseline**: scoped-wait loop `_scoped_wait` (cli.py:885-988) is
  where the rescind wake path goes; it already has per-thread match
  logic and the composing-extension branch to model from.

## D4 — Root hardening targets `init`/diagnostics/env, not send paths

- **Decision**: (a) `init` refuses when `find_root()` from the target's
  parent finds an existing store up-tree, unless `--force`; (b) `doctor`
  walks cwd→drive-root and names every store found; (c) new
  `AGENTTALK_ROOT` env var with precedence flag > env > walk; (d)
  `whoami`/`doctor` print the resolved root first.
- **Rationale**: the verified fork mechanism is **two `init`s + the
  upward walk** — NOT silent store creation. Verified: no command
  auto-creates (`_get_store` requires an existing store, exit 2;
  cli.py:38); `find_root` walks up (store.py:1180-1189); no
  `AGENTTALK_ROOT` exists anywhere (repo-wide grep, only SELF/PEER).
  Fixing at send time would penalize every command for an error made
  once at init time.
- **Alternatives**: per-send multi-store scan (rejected: cost on every
  call, noise); refusing nested stores entirely (rejected: a
  deliberately nested store is legitimate in tests/sandboxes — hence
  `--force`); config-file root pinning (rejected for v1: env var covers
  the per-window need; a file adds another stateful source of truth).

## D5 — Liaison: advisory bit + explicit helper that refuses ambiguity

- **Decision**: (a) config gains a single optional `operator_facing`
  designation set via `roster set-operator-facing <agent>` (single-slot
  by representation — see data-model.md; ambiguity is unrepresentable,
  improving on the consult's warn-on-multiple); (b) new
  `agenttalk escalate` resolves the liaison, mints/echoes a request_id,
  sends an ordinary tracked question carrying `needs_operator=true`
  meta, and **refuses (exit 2)** when no liaison is set (or `--to`
  overrides explicitly); (c) liaison-scoped "operator-input needed"
  bucket in `sync`/`threads`; (d) `doctor`/`sync` warn when unset, when
  the designated agent is not in the roster, or when its heartbeat is
  stale.
- **Rationale**: raw meta conventions get mistargeted/uncorrelated
  (Codex blocking objection); refusal on the send path prevents the
  invisible-failure mode the feature exists to kill, while diagnostics
  stay warn-only. No new kind: transport stays governance-free (C-006);
  existing closure logic already treats any non-control correlated reply
  as the answer.
- **Closure rule**: pending until the liaison sends any non-control
  correlated reply to the requester (optionally tagged
  `operator_answer=true` for display). Derivable purely from valid
  messages — verified against `_derive_broadcast`/question closure
  precedents (threads.py:82-88, 176-185).
- **Alternatives**: first-class `operator-escalation` kind (rejected for
  v1: KNOWN_KINDS bloat, workflow concept in transport; revisit if meta
  proves too weak); enforced liaison (impossible at bus level — honest
  scope, enforcement meaning → #19); boolean per-agent map for the bit
  (rejected: makes "multiple liaisons" representable for no benefit).

## D6 — Intent-to-reply reuses composing; slips as a unit

- **Decision**: `composing --to-request RID` sugar + an observational
  per-agent reply-in-flight record surfaced in `threads`/`sync` +
  stale-warning suppression. Ships only if it needs no new thread-state
  model (C-004); otherwise the whole feature slips to 0.14.x (C-010).
- **Rationale**: ~80% verified built: scoped wait already extends on a
  composing carrying `meta.request_id==RID` via the `comp_rid in (None,
  rid)` gate (cli.py:939-942) and `composing` already accepts `--meta`
  (cli.py:575-583). Missing pieces are ergonomics + visibility only.
  Marker follows the `.heartbeat`/`.waiting` observational pattern
  (store.py:983-1035) — never load-bearing.
- **Alternatives**: persisted intent in threadstate.json (rejected:
  makes it load-bearing); auto-composing emitted by the CLI on a timer
  (rejected: agents own their cadence; matches composing-design decision
  from v0.8.0).

## D7 — Test & compatibility strategy

- **Decision**: extend the four existing test files per the plan's
  structure map; every new error path asserts exit code + stderr
  (NFR-004); the pre-existing suite must pass unmodified (NFR-001);
  end-to-end rescind-race and liaison-flow tests go in
  tests/test_coordination.py mirroring its existing two-agent patterns.
- **Rationale / known traps**: dev-install gotcha — run
  `pip install -e .` before testing or tests run against stale
  site-packages; mixed-version bus compatibility is by additive-only
  JSON keys and new kinds being ignored by old readers (old `recv`
  prints unknown kinds — verify a pre-0.14 reader treats `rescind` as
  ordinary inbox content, which is acceptable: it is transcript-visible
  by design).
- **Exit-code note**: new outcomes use exit 3 (superseded/rescinded
  wake) and 4 (unknown rid) — chosen to avoid the reserved 0/1/2/130
  (C-005). `wait` keeps 1 strictly for timeout.

## Verified code baseline (carry into WPs)

| Fact | Anchor |
|------|--------|
| No command auto-creates a store; loud exit 2 | `_get_store`, cli.py:38 |
| Root resolution: flag, else upward walk; no env var today | `find_root`, store.py:1180-1189 |
| `CONTROL_KINDS == {"composing"}`; KNOWN_KINDS has 10 kinds | store.py:39-67 |
| Thread replay is pure: `derive_threads(messages, agent, cursor, now, closed_rids)` | threads.py:247-254 |
| Per-kind closure rules + question's any-non-control rule | `_classify_event`, threads.py:52-90 |
| Manual closure override precedent (applies after replay) | threads.py:360-361 |
| Broadcast obligation = every recipient; roles not plumbed in | threads.py:169,185,208 |
| Scoped wait honors request_id-tagged composing | cli.py:939-942 |
| Observational marker pattern (write/read/clear, staleness) | store.py:983-1035 |
| ack closes thread view only; cursor/delivery untouched | store.py:1102-1123, cli.py:1096-1120 |
| request_id autogen + reply-echo gating (do not regress) | `_maybe_autogen_request_id`, cli.py:84-114 |
