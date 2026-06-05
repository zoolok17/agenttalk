# Data Model: 0.15.0 Team Scope

All additive on the 0.14.0 model.

## 1. Frozen audience meta (broadcast copies, #15/#16)

| Key | Value | Notes |
|-----|-------|-------|
| `audience_kind` | `"role"` \| `"group"` \| `"all"` | new; absent pre-0.15 |
| `audience` | label (existing) | unchanged (backcompat display) |
| `audience_role` | role name | only when kind=role |
| `audience_resolved` | `"a,b,c"` | frozen member list, comma-joined |
| `batch_total` | `"3"` | stringified copy count |
| `request_id`/`broadcast_id` | shared bid | unchanged |

Derivation source of obligations stays the opener copies (already
config-independent). New meta = display/audit/incompleteness only.

## 2. NA response (#15)

Ordinary `message` with `meta.response="not-applicable"` echoing the
thread's request_id. Closure mechanics unchanged. Labels (additive):
- broadcast rows: `responded_na: [members]` (subset of `responded`)
- pairwise rows: `na_response: true` when the terminal reply was NA
- human render: `(n/a)` markers.
Refusal rule: NA on review-request/proposal threads → exit 2.

## 3. Fan-out batch (#16)

Batch key = the existing shared broadcast id; total = `batch_total`.
Incomplete batch (derived): visible copies < batch_total → status
warning naming missed members (= audience_resolved minus copy
recipients), suppressed when superseded. Partial-failure at send time:
exit **5**, stdout manifest `delivered=[...] missed=[...]` (+ --json).

## 4. Quarantine (#17)

`.agenttalk/quarantine/<original-name>[.<ts>]` — move-only, collision
suffix, never overwritten/deleted by the tool. Selection = exactly the
`list_invalid_messages` gate walk. Restore = move the file back into
`messages/` by hand (documented). `status --json` adds `quarantined`
(int, count of files; additive). Scanning never reads the dir (it is
not `messages/`).

## 5. Exit codes

| Code | Meaning |
|------|---------|
| 0/1/2/3/4/130 | unchanged (0.14.0 contract) |
| **5** | partial fan-out: some copies written, some failed (new) |

## 6. JSON additions (all strictly additive — absent when unused)

- `threads --json` rows: `audience_kind`, `responded_na`, `na_response`
- `status --json`: `quarantined`; warnings gain incomplete-batch entries
- `prune --json`: `{"selected": [...], "moved": [...], "dry_run": bool}`
