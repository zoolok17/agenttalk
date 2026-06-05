# Research: 0.15.0 Team Scope

No open clarifications. Decisions with rationale; code anchors are the
post-0.14.0 tree (v0.14.0 tag).

## D1 — Role audience resolution: a sibling resolver, not an overload
- **Decision**: new `Store.resolve_role_audience(role, *, exclude)` →
  members whose `roles[agent] == role`, ValueError on unknown role or
  empty-after-exclude. `cmd_broadcast` adds `--to-role` to the existing
  mutually-exclusive target group (`--to-group` | `--all` | `--to-role`).
- **Rationale**: roles and groups are distinct config maps with distinct
  semantics; overloading `resolve_audience(target)` would create the
  role/group name-collision ambiguity the spec's edge case forbids.
- **Alternatives**: `--to-group` fallback-to-role (rejected: implicit
  resolution ambiguity); auto-groups mirroring roles (rejected: two
  sources of truth).

## D2 — Audience freezing: already structural; meta is audit + display
- **Decision**: fan-out copies gain `audience_kind` (role|group|all),
  `audience_role` (when kind=role), `audience_resolved` (comma-joined
  members), `batch_total` (stringified N). Derivation stays UNCHANGED in
  its obligation source: `_derive_broadcast` already computes the
  audience from the opener COPIES (`{m.recipient for m in openers}`),
  i.e. from messages, never from live config — C-004 holds structurally
  today. The new meta exists for display, audit, and the
  incomplete-batch warning (copies-present vs batch_total).
- **Rationale**: zero-risk way to honor the freeze guarantee; the
  message log remains the single source of derived truth.

## D3 — reply --na: flag + meta, no new kind (C-003)
- **Decision**: `reply --na` forces kind=message, meta
  `response=not-applicable`, default body "n/a" when none given (NA is
  the one reply whose body may be empty-ish by design). Refusal
  (exit 2, FR-006) when the anchor thread's opener kind is
  review-request or proposal — those contracts need typed responses.
  Display: broadcast rows get `responded_na` (subset of `responded`,
  additive); pairwise rows get `na_response: true` when the closing
  reply was NA; human views render "(n/a)".
- **Rationale**: any non-control reply already closes a question — NA
  is labeling, exactly like operator_state in 0.14.0.
- **Alternatives**: first-class kind (rejected for v1 — C-003, KNOWN_KINDS
  bloat); reuse `ack` (rejected: ack is view-local, the broadcaster
  would still see pending).

## D4 — Fan-out accounting: batch_total + exit 5, no rollback (C-006)
- **Decision**: preflight (resolve audience + roster validation —
  already done before the loop) then write copies in a try/except; on
  any copy failing: print delivered=[...] missed=[...] (+ `--json`
  shape), exit **5** (new documented code; 0/1/2/3/4/130 untouched).
  Each copy carries `batch_total`; `_thread_warnings` flags any
  broadcast whose visible copy-count < batch_total ("incomplete
  fan-out — re-send to the missed members or rescind"), suppressed when
  the thread is closed-superseded (rescind = resolution per FR-009).
- **Rationale**: the id system already gives us the batch key (the
  shared broadcast_id); counting copies against a frozen total is a
  pure-derivation incompleteness signal — no manifest FILE needed, the
  copies are the manifest (and a sidecar manifest file would be the
  kind of load-bearing state C-004/0.14.0 banned).
- **Alternatives**: sidecar manifest file (rejected: new load-bearing
  state, restart fragility); true rollback (impossible — C-006).

## D5 — Quarantine: move-only, same gates, collision-safe (#17)
- **Decision**: `Store.list_invalid_message_paths()` (same gate walk as
  `list_invalid_messages`, returning paths+reasons) +
  `Store.quarantine_invalid(dry_run)` moving files to
  `.agenttalk/quarantine/` via the archive-collision pattern (existing
  `_archive_session` precedent: never overwrite — suffix on collision).
  CLI: `agenttalk prune --invalid [--dry-run] [--json]`; refuses without
  the explicit `--invalid` selector (future selectors reserved).
  `status` adds `quarantined` count (additive); `doctor` reports
  invalid+quarantined.
- **Rationale**: safety was already verified by construction in the
  0.14.0 cycle (pure-function derivation, id-string cursors,
  per-message HMAC); the only new risk is selection drift, killed by
  sharing the exact gate code path (FR-011).
- **Alternatives**: delete (rejected: C-002, retroactive-validity);
  archive-to-sessions (rejected: semantic mismatch with transcripts).

## D6 — Test strategy + the CI lesson
- Unit per module (WP01-03), e2e gates in test_coordination (WP04):
  role-routing 100%, NA lifecycle, fault-injected partial fan-out
  (monkeypatch Store.send to fail at position k), prune byte-identity
  sweep. Strict-additivity set-equality gates extended for the new keys.
  **No test may assert exit codes that depend on host-environment
  health without pinning the environment** (the 0.14.0 red-matrix
  lesson); CI matrix green is a release gate (NFR-005).
