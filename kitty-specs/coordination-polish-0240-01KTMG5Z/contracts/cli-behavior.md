# CLI Behavioral Contracts: agenttalk 0.24.0 — Coordination Polish

These are the acceptance gates. Each row is a test target (NFR-006: every FR covered).

## `escalate` (FR-001, FR-002, FR-003, NFR-004)

| Precondition | Invocation | Expected |
|---|---|---|
| liaison set | `escalate -m ...` | routes to liaison (unchanged); exit 0; prints `request_id=esc-…` |
| no liaison, exactly one lead | `escalate -m ...` | routes to lead; stderr/stdout notice it fell back to the lead; exit 0; prints `request_id=esc-…` |
| no liaison, no lead, no `--to` | `escalate -m ...` | exit 2; remediation names BOTH `roster set-operator-facing <agent>` AND `roster set-role <agent> lead` |
| any | `escalate --to X -m ...` | `--to` overrides everything; routes to X (existing behavior) |
| no liaison, two+ legacy leads | `escalate -m ...` | `sole_lead()`→None → exit 2 with remediation (does NOT guess) |
| sender IS the resolved target | `escalate -m ...` | exit 2, existing "you own the operator channel" guard |

## `roster set-role` (FR-004, FR-005, FR-006, FR-007, FR-008)

| Precondition | Invocation | Expected |
|---|---|---|
| no current lead | `roster set-role X lead` | X becomes lead; exit 0 |
| current lead is Y (≠X) | `roster set-role X lead` | atomic demote Y + promote X; prints `demoted Y, promoted X` (no `--force` needed); exit 0; exactly one lead after |
| X is already lead | `roster set-role X lead` | idempotent success; no "demoted" line; exit 0 |
| current lead is X | `roster set-role X -` (or other role) | zero leads now; allowed; exit 0 |
| role given as `Lead`/`LEAD` | `roster set-role X Lead` | treated as the `lead` role for uniqueness (case-insensitive) |
| non-lead roles | `roster set-role X reviewer` | unchanged; multiple reviewers allowed |

## `doctor` (FR-009)

| Precondition | Expected |
|---|---|
| ≥2 agents, no liaison AND no lead | a warning-level check: "escalation has nowhere to go" naming both remediation commands |
| ≥2 agents, liaison set | check absent/ok |
| ≥2 agents, sole lead set | check absent/ok |
| 1 agent (solo) | check absent/ok (never warns solo) |

## wake send (FR-010, FR-011)

| Precondition | Invocation | Expected |
|---|---|---|
| no explicit id | `send --kind wake ...` | meta gains `request_id=wk-…`; (non-quiet) prints the minted id |
| explicit `--meta request_id=Z` | `send --kind wake ...` | id is exactly `Z`; no `wk-` minted |
| any wake | (after send) `threads` | NO new owed/open thread row attributable to the wake; `OPENER_KINDS` excludes `wake` |

## pre-send owed-inbound warning (FR-012, FR-013, FR-014)

| Precondition | Invocation | Expected |
|---|---|---|
| S owes R an open `proposal` (pp-…) | `send --from S --to R --kind note ...` | soft stderr warning names the owed pp- id; send still succeeds (exit unchanged) |
| S owes R an operator escalation | `send --from S --to R ...` (unrelated) | soft warning names the owed esc-; send succeeds |
| S replying on the same owed id | `reply --to-request pp-… ...` | NO warning (suppressed on same request_id) |
| S owes R nothing of decision-kind | `send --from S --to R ...` | NO warning |
| S owes R only a question/review | `send --from S --to R ...` | NO warning (decision-kinds only) |
| thread derivation raises | `send ...` | send still succeeds; warning silently skipped (best-effort) |
