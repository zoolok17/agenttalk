# CLI Surface Contract: 0.15.0 Team Scope

Normative. Everything not listed is unchanged from 0.14.0.

## Changed: `agenttalk broadcast`

- New `--to-role <role>` in the required mutually-exclusive target
  group (`--to-group` | `--all` | `--to-role`). Resolves members whose
  roster role equals `<role>`, excluding the sender. Unknown role or
  empty audience → exit 2 + actionable message (names known roles).
- Every copy now carries `audience_kind`, `audience_resolved`,
  `batch_total` (+ `audience_role` for role targets) — additive meta.
- Partial-failure accounting: if any copy fails to write, print
  `delivered=[...]` / `missed=[...]` (machine-parseable lines; with
  `--json`, a structured object), exit **5**. Complete success stays
  exit 0 with the existing summary.

## Changed: `agenttalk reply`

- New `--na` flag: not-applicable response. Forces kind=message, sets
  `meta.response=not-applicable`, body optional (defaults to "n/a").
  Closes the obligation like any answer; displayed as `(n/a)`.
- Refusal (exit 2): anchor thread opened by review-request or proposal
  ("this thread needs a typed response: review-result /
  proposal-response"). Mutually exclusive with `--kind`.

## New: `agenttalk prune --invalid [--dry-run] [--json]`

- Moves every validation-failing message file to
  `.agenttalk/quarantine/` (collision-suffixed, never overwriting).
  `--dry-run` lists without moving. Zero invalid → exit 0, "nothing to
  prune". Selection is the SAME gate walk status/doctor report.
  Bare `prune` without `--invalid` → exit 2 (explicit selector
  required; future selectors reserved).

## Changed: `status` / `threads` / `doctor`

- `status`: `quarantined` count (additive); warnings gain
  `incomplete fan-out` entries (visible copies < batch_total, with the
  missed members; suppressed when the thread is superseded).
- `threads`: broadcast rows show `n/a` responders distinctly
  (`responded_na`); pairwise rows closed by an NA reply show `(n/a)`.
- `doctor`: invalid + quarantined counts in a store-hygiene check.

## Skill-contract deltas (WP05)

- listen/send: answer broadcast questions OR `reply --na` when the
  thread does not concern your role — never placeholder-ack.
- lead: prefer `--to-role` over hand-curated groups when roles exist;
  on exit 5, re-send to the missed members or rescind the thread.
- All: `prune --invalid --dry-run` before prune; quarantine is
  recoverable.
