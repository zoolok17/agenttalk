# Phase 1 Data Model: agenttalk 0.24.0 — Coordination Polish

This release adds no new persisted files. It adds one invariant over existing roster
state, one new message-meta value, and one derived (non-persisted) concept.

## Entity: Roster role `lead` (config.json `roles`)

- **Storage**: existing `roles` map in `.agenttalk/config.json` (`{agent: role}`).
- **New invariant**: at most one agent may map to a role whose value `casefold()`s to
  `"lead"`. Enforced in `store.set_role()` inside its `_config_lock()`.
- **Transitions**:
  - set `lead` on agent X, no current lead → X becomes lead.
  - set `lead` on agent X, current lead is Y (≠X) → **atomic**: Y demoted (role removed
    / set to none), X promoted. One config write. Returns enough info for the CLI to
    print `demoted Y, promoted X`.
  - set `lead` on agent X, X is already lead → idempotent no-op success.
  - set a non-lead role on the current lead, or remove its role → zero leads (valid).
- **Validation**: still passes existing `validate_roles`. Role value stored as given;
  uniqueness compared case-insensitively.

## Helper: `store.sole_lead() -> str | None`

- Returns the single agent whose role `casefold()`s to `"lead"`, else `None`
  (None for zero leads AND for the legacy >1 case — ambiguity reads as "no unambiguous
  lead").
- Read-only; used by `escalate` resolution and (optionally) `doctor`.

## Relationship: liaison (`operator_facing`) vs lead

- `operator_facing` (existing single slot) and `lead` (this invariant) remain **distinct**.
- Escalation resolution order: `--to` → `operator_facing()` → `sole_lead()` → none.
- A team is "human-reachable" for escalation iff it has a resolvable liaison OR a sole
  lead.

## Value: `wk-` correlation id (message meta)

- **Storage**: `meta.request_id` on a `wake` message, value prefixed `wk-`.
- **Minted**: by `cli._maybe_autogen_request_id` when a `wake` is sent without an
  explicit `request_id`. An explicit id is honored verbatim.
- **Not an opener**: `store.OPENER_KINDS` excludes `wake`; thread derivation creates no
  owed/open row for a wake. The id exists only so a reply can echo it.

## Derived concept: owed decision-request (not persisted)

- Computed on demand from existing thread state via `threads.derive_threads`.
- For a prospective send from S to R: an *owed decision-request to R* is an open thread
  where S owes R a response and the opener kind is `proposal` or the thread is an
  operator escalation (`needs_operator`).
- Used only to emit the soft pre-send warning; never written anywhere.
