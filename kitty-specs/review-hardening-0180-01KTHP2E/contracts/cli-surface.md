# Contract: CLI / behavior surface — Review Hardening (0.18.0)

All changes are additive or strictly-narrowing. No command is removed or
renamed; no existing JSON key changes shape.

## Commands

### `agenttalk wait` (FR-007)
`wait` is the **only** blocking-wait CLI command. (`listen` is a bundled skill
that calls `wait`; there is no `agenttalk listen` CLI command and none is
added.)
- NEW: on startup, if another **live** process already holds the agent's
  `.waiting` marker (different pid, alive), print ONE advisory stderr warning
  (data-model §7). Never blocks; **exit code unchanged**.
- Everything else identical. The listen skills inherit the warning by calling
  `wait`.

### `agenttalk broadcast --resume <bid>` (FR-005)
- Retired frozen recipients are SKIPPED and reported `dropped=[…]` (not sent).
- Exit codes:
  - active copies still failed → **5** (unchanged), dropped noted;
  - all active copies sent (only retired remained) → **0** (resolved);
  - nothing left to send but retired → **0**, dropped noted.
- Previously: a retired recipient caused a permanent exit 5. This NARROWS
  exit-5 emission (C-004); never broadens it.

### `agenttalk tail` (FR-004)
- Validates against the known roster (active ∪ retired): a retired identity's
  historical messages now PRINT instead of being flagged `TAIL INVALID`.
- No flag/output-format change.

### `agenttalk doctor` (FR-009)
- NEW advisory: where a `.waiting` marker exists, reports its pid + liveness
  (additive in `--json`, one line in plain). Framed as advisory, not complete
  duplicate detection. **Exit code unchanged.**

## HTTP (dashboard, FR-004)
- `/api/messages`, `/api/messages/<id>`, `/messages/<id>`, index now render
  retired identities' history (known-roster validation), matching the thread
  panel and `/api/state`. Response SHAPES unchanged; only the included message
  set widens. `/api/state` and all 0.17.0 routes unchanged.

## Invalid-message classification (FR-001, FR-003)
- A message file with a non-string `meta.signature` (signing enforced) or a
  non-canonical `id` is now classified INVALID: surfaced by
  `status`/`doctor`/`list_invalid_messages`, quarantinable via
  `prune --invalid`, never delivered. Previously the signature case crashed
  every read path; the id case delivered and poisoned cursors.

## Exit-code contract (unchanged globally)
0 ok · 1 wait-timeout · 2 usage/refusal · 3 superseded/stale · 4 unknown rid ·
5 partial fan-out (now narrowed by FR-005) · 130 SIGINT. The new warning and
the doctor advisory never change any exit code.

## Additive JSON fields
- broadcast thread dict: `audience_retired: [names]` (absent when empty).
- doctor: per-marker pid/liveness advisory (absent when no marker).
All absent-not-null when unused; pre-0.18.0 readers unaffected (NFR-001).
