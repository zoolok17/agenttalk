# Specification: agenttalk 0.24.0 — Coordination Polish

**Mission**: coordination-polish-0240-01KTMG5Z
**Created**: 2026-06-08
**Target branch**: master
**Status**: Draft

## Summary

A four-agent production run (`launcher-redesign-core-01KTK6H6`) surfaced a set of
multi-agent coordination friction points in `agenttalk`. After comparing the
feedback against the current code (v0.23.1) and a joint design review with the
peer reviewer, three items are confirmed as genuinely **agenttalk-side, still
open, and worth fixing now**. The rest of the feedback was either already shipped
(`--file`/stdin body, `wait --heartbeat-interval`) or belongs to the separate
spec-kitty work/review lifecycle (claim-as-lock, `reassign-review`,
`release-claim`, rejection-without-`--force`, feedback-file durability) and is
explicitly out of scope here.

This release delivers exactly three changes:

1. **Escalation never silently strands a reviewer.** When a reviewer needs a human
   decision but no operator-facing liaison is configured, `escalate` falls back to
   the team **lead** instead of hard-failing — backed by a new *at-most-one-lead*
   roster invariant so "the lead" is always unambiguous, and a `doctor` warning
   when a team has no human-facing target at all.
2. **Wake messages carry their own correlation id.** A `wake` gets a dedicated
   `wk-` id so a reply can echo it, ending the fragile habit of reusing the
   message id as a correlation handle.
3. **You're warned before talking over an open decision you owe.** When you send
   unrelated traffic to a peer you currently owe an open decision-request
   (proposal or operator-escalation), a soft warning surfaces that debt first.

## Actors

- **Reviewer / developer agent** — a non-lead roster member that may need to
  surface a decision to the human operator.
- **Lead agent** — the roster member designated `role=lead`; the fallback
  human-facing target and the team coordinator.
- **Operator (human)** — reached through the liaison (`operator_facing`) or, by
  fallback, the lead.
- **Roster admin** — whoever runs `roster set-role` / `set-operator-facing`
  (a local, deliberate admin op).

## User Scenarios & Testing

### Scenario A — Reviewer escalates with a lead but no liaison (§3.1)

1. A team has a `lead` on the roster but no `operator_facing` liaison configured.
2. A reviewer hits a gate-bypass that needs a human ruling and runs `escalate`.
3. **Today**: `escalate` exits 2 ("no operator-facing agent configured") and the
   reviewer is stranded, forced to fall back to a plain note.
4. **Desired**: `escalate` routes the operator-question to the lead, prints that it
   fell back to the lead, and mints the usual `esc-` request id for the follow-up
   `wait --to-request`.

### Scenario B — Roster cannot hold two leads (§3.1)

1. A roster already has agent `claude` as `role=lead`.
2. An admin runs `roster set-role codex lead`.
3. **Desired**: the operation **moves** the lead in one step — demotes `claude`
   (to no role) and promotes `codex` — and prints `demoted claude, promoted codex
   to lead`. It does NOT require a `--force` flag or a manual two-step demote.
4. At no point can two agents simultaneously hold `role=lead`.

### Scenario C — Team with no human-facing target is warned (§3.1)

1. A multi-agent roster has neither an `operator_facing` liaison nor a `role=lead`.
2. An agent runs `doctor`.
3. **Desired**: `doctor` reports a warning — "escalation has nowhere to go: run
   `roster set-operator-facing <agent>` or `roster set-role <agent> lead`" — so
   the gap is visible before an escalation is ever needed.

### Scenario D — Solo / symmetric pair is NOT forced to have a lead (§3.1)

1. The canonical two-agent pair runs with both members at `role=-` (no lead).
2. **Desired**: nothing forces a lead to exist. No ceremony, no warning solely for
   being a pair, no auto-promotion. The at-most-one invariant permits zero leads.

### Scenario E — Wake reply can be correlated (§3.3)

1. The lead sends a `wake` to a developer.
2. **Desired**: the wake carries a `wk-` correlation id in its meta. The developer
   can reply echoing that id, and neither side has to improvise by reusing the raw
   message id.

### Scenario F — Owed decision surfaced before unrelated send (§3.2)

1. `codex` has sent `claude` a `proposal` (option a/b) that `claude` has not yet
   answered.
2. `claude` goes to `send` `codex` an unrelated `note`.
3. **Desired**: a soft warning surfaces — "you owe codex an open proposal
   (pp-…); answer or rescind it before unrelated traffic" — but the send still
   proceeds (advisory, non-blocking).
4. The warning does NOT fire when `claude` is replying on that same request id, nor
   for non-decision traffic (plain note/message/FYI).

### Edge cases

- `escalate` with neither liaison, nor a lead, nor `--to`: still exits 2 (contract
  preserved) but with the improved remediation message.
- `escalate` with exactly one lead and no liaison: routes to the lead.
- `set-role <agent> lead` when that same agent is already the lead: no-op success
  (idempotent), no spurious "demoted" line.
- `set-role <agent> lead` when the agent is not yet on the roster: existing
  roster-membership rules apply unchanged.
- Demoting the current lead via `set-role <lead> -` (or equivalent) leaves the team
  with zero leads — allowed.
- `wake` with an explicitly supplied `request_id`: the supplied id is honored; no
  second id is minted.
- Owed-inbound warning when multiple decision-requests are owed to the same peer:
  surface them (count + ids), still non-blocking.
- Owed-inbound check must not itself fail the send if thread derivation errors —
  the warning is best-effort.

## Requirements

### Functional Requirements

| ID | Requirement | Status |
|----|-------------|--------|
| FR-001 | `escalate` MUST resolve its target in this order: configured `operator_facing` liaison → the single `role=lead` agent → fail. An explicit `--to` MUST override the entire chain. | Approved |
| FR-002 | When `escalate` falls back to the lead, it MUST emit a human-visible notice that it routed to the lead (not the liaison), and MUST still mint and print the `esc-` request id. | Approved |
| FR-003 | When `escalate` has no liaison, no lead, and no `--to`, it MUST still exit 2, with a remediation message naming both `roster set-operator-facing` and `roster set-role … lead` as fixes. | Approved |
| FR-004 | The roster MUST enforce an at-most-one-`lead` invariant: setting `role=lead` on an agent when another agent already holds `lead` MUST atomically demote the prior lead and promote the new one. | Approved |
| FR-005 | The lead reassignment in FR-004 MUST succeed without a `--force` flag and MUST print which agent was demoted and which was promoted. | Approved |
| FR-006 | Setting `role=lead` on the agent that is already the lead MUST be an idempotent no-op success (no demote/promote churn). | Approved |
| FR-007 | The at-most-one-`lead` invariant MUST treat the role name case-insensitively (e.g. `Lead`/`LEAD` resolve to the same role). | Approved |
| FR-008 | Zero leads MUST remain a valid roster state; nothing may force a lead to exist or auto-promote one. | Approved |
| FR-009 | `doctor` MUST emit a warning-level check when a multi-agent roster has neither an `operator_facing` liaison nor a `role=lead`, naming both remediation commands. The check MUST be absent/ok when a liaison or lead exists, and MUST NOT warn a solo (single-agent) roster. | Approved |
| FR-010 | A `wake` message MUST carry a dedicated `wk-` correlation id in its meta when none is supplied, so replies can echo it. An explicitly supplied `request_id` MUST be honored verbatim (no second id minted). | Approved |
| FR-011 | The `wk-` correlation id MUST NOT make `wake` a tracked thread opener (wake remains FYI-class for thread derivation; no new owed/open thread row is created by a wake). | Approved |
| FR-012 | Before sending traffic to a peer, the send path MUST surface a soft (non-blocking) warning if the sender currently owes that same peer an open decision-request (a `proposal` or an operator-escalation), naming the owed correlation id(s). | Approved |
| FR-013 | The owed-inbound warning MUST be suppressed when the outgoing message is a reply on the same owed request id, and MUST NOT fire for non-decision traffic. | Approved |
| FR-014 | The owed-inbound warning MUST be best-effort: any failure to derive thread state MUST NOT block or fail the send itself. | Approved |

### Non-Functional Requirements

| ID | Requirement | Threshold | Status |
|----|-------------|-----------|--------|
| NFR-001 | Runtime stays dependency-free. | Python standard library only; zero third-party runtime imports. | Approved |
| NFR-002 | All changes are backward-compatible and additive. | Existing commands, flags, exit codes, message schema, and on-disk state remain valid; no field removals. | Approved |
| NFR-003 | Message history remains immutable. | No change rewrites, deletes, or mutates any existing message file; new behavior only adds meta or roster/config state. | Approved |
| NFR-004 | Exit-code contract preserved. | `escalate` still exits 2 when there is genuinely no target; no exit code changes its meaning. | Approved |
| NFR-005 | Cross-platform parity. | All behavior and tests pass on the CI matrix: Python 3.10–3.13 × {Linux, macOS, Windows}. | Approved |
| NFR-006 | Test coverage for every FR. | Each FR has at least one pytest assertion; full suite green before tag. | Approved |

### Constraints

| ID | Constraint | Status |
|----|------------|--------|
| C-001 | Per-WP cross-review by the peer agent over agenttalk before merge; fresh-eyes adversarial review before tag. | Approved |
| C-002 | CI matrix green before any tag; release (tag + GitHub Release) only on explicit operator authorization. | Approved |
| C-003 | The roster `lead` role and the `operator_facing` liaison remain distinct concepts; this release does NOT merge them. The liaison stays the primary escalation target; the lead is only the fallback. | Approved |
| C-004 | FR-012/FR-013/FR-014 (owed-inbound warning) MAY be cut from this release if its implementation balloons in complexity; FR-001..FR-011 are the committed core. | Approved |

## Success Criteria

- SC-001: A reviewer on a team that has a lead but no liaison can escalate to the
  human via the lead in a single command, with zero manual fallback steps.
- SC-002: It is impossible to end up with two simultaneous leads through the
  documented roster commands; switching the lead takes exactly one command.
- SC-003: A team that has left itself with no human-facing escalation target learns
  this from `doctor` before an escalation is attempted, not after it fails.
- SC-004: A solo run and a symmetric two-agent pair operate exactly as before — no
  new required role, no new warning solely for lacking a lead.
- SC-005: Every wake reply can be correlated to its wake without reusing the raw
  message id.
- SC-006: An agent is told, before sending, when it is about to talk over an open
  decision it owes a peer — without that warning ever blocking a legitimate send.

## Key Entities

- **Roster role `lead`** — at most one per roster; the escalation fallback target
  and team coordinator. Distinct from the liaison.
- **`operator_facing` (liaison)** — the existing single-slot designation of the
  agent the human talks to directly; the primary escalation target. Unchanged by
  this release except as the first link in the escalation resolution chain.
- **`wk-` correlation id** — a new correlation-id prefix for wake messages
  (alongside existing `esc-`, `pp-`, `rq-`, `q-`, `b-`), carried in meta, not a
  thread opener.
- **Owed decision-request** — an open `proposal` or operator-escalation thread the
  sender owes a specific peer, derived from existing thread state.

## Assumptions

- The peer reviewer is available over agenttalk for per-WP cross-review (the
  established two-agent ritual).
- The existing `operator_facing` single-slot representation is retained as-is; this
  release adds a fallback, not a replacement.
- "Multi-agent roster" for FR-009 means two or more registered agents; a
  single-agent roster is treated as solo and not warned.
- Decision-request kinds for FR-012 are the existing `proposal` and
  operator-escalation (`needs_operator`) threads; ordinary `note`/`message`/
  `question`/FYI traffic is not a decision-request.

## Out of Scope

- The bus-visible **working/claim marker** (feedback §1.2/§4.3): deferred pending a
  spec-kitty claim-lock design that would give the marker a defined writer,
  lifetime, and enforcement meaning. Building it now would ship an advisory signal
  with no enforcer.
- All **spec-kitty-domain** items: claim-as-lock (TL;DR), unique claim identity
  (§1.1), do-not-touch lock (§1.3), `reassign-review`/`release-claim` (§2.1),
  rejection-without-`--force` (§2.2), review-ownership discovery (§2.3), and
  review-feedback-file durability (§4.1). These live in the separate spec-kitty
  project, not agenttalk.
- Already-shipped items: `--file`/stdin message body (§4.2) and
  `wait --heartbeat-interval`/`--grace` (§4.4) are present in v0.23.1 and need no
  work.
- Forcing **exactly one** lead (the always-one invariant): explicitly rejected — it
  breaks solo/pair runs, has no clean bootstrap, breaks on retire, and is costly to
  maintain on a file-backed multi-process store. At-most-one + the `doctor` nudge
  delivers the same guarantee without the rigidity.
