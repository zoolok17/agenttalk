# Phase 0 Research: agenttalk 0.24.0 — Coordination Polish

No open unknowns — the design was settled in the operator + peer (Codex) review before
this mission. This records the three small API decisions and the rejected alternatives,
so implementers don't re-litigate them.

## D1 — Escalation fallback target order

- **Decision**: `escalate` resolves `--to` (explicit) → `operator_facing` (liaison) →
  `sole_lead()` (the single `role=lead` agent) → exit 2 with remediation.
- **Rationale**: the liaison is the purpose-built human channel and must stay primary;
  the lead is a sane secondary because it is the team's coordinating, operator-adjacent
  role. Falling back kills the "escalation lands nowhere" failure without conflating
  the two roles.
- **Alternatives considered**:
  - *Merge liaison and lead into one concept* — rejected (C-003): they are genuinely
    different (one is "who the human talks to", the other is "who coordinates the
    team"); merging would be a breaking semantic change.
  - *Fall back to any role, or to the first agent* — rejected: ambiguous and surprising;
    only the lead has a coordinating contract.

## D2 — At-most-one vs exactly-one lead

- **Decision**: enforce **at-most-one** `lead` on the `set_role` write path; zero leads
  is valid. Switching the lead is one atomic demote+promote.
- **Rationale**: makes "the lead" unambiguous (what the fallback needs) without forcing
  ceremony on solo/pair runs, without a bootstrap question, without auto-promotion on
  retire, and without a costly global invariant on a file-backed multi-process store.
- **Alternatives considered**:
  - *Exactly-one, always* — rejected (Out of Scope): breaks solo/pair, no clean
    bootstrap, breaks on retire, expensive to maintain across processes.
  - *Reject a second lead and require a manual demote first* — rejected: reproduces the
    `--force` two-step friction the feedback (§2.2) complained about; the advertised
    path should just work.

## D3 — `sole_lead()` semantics under legacy ambiguity

- **Decision**: `sole_lead()` returns the single `lead` agent, or **None** if zero or
  (legacy) more than one exist.
- **Rationale**: the invariant prevents >1 going forward, but a hand-edited or
  pre-0.24.0 config could hold two. Returning None on ambiguity makes `escalate` fall
  through to the remediation error instead of silently picking one — consistent with
  "an escalation that lands nowhere is the failure we kill; a wrong-target escalation is
  worse."

## D4 — Where the `wk-` id is minted, and why it doesn't open a thread

- **Decision**: add `"wake": "wk-"` to `cli._AUTOGEN_REQUEST_ID_PREFIX`; leave
  `store.OPENER_KINDS` unchanged.
- **Rationale**: `_maybe_autogen_request_id` already mints from that prefix map in the
  send path, while thread derivation keys off `OPENER_KINDS`. The two are separate
  constants, so wake can get a correlation id while remaining FYI-class for `threads`.
  A test asserts `OPENER_KINDS` excludes `wake` to lock the separation.
- **Alternatives considered**:
  - *Make wake a tracked opener* — rejected (FR-011): wakes are nudges, not requests;
    tracking them would create phantom owed/open rows and noise in `threads`.

## D5 — Owed-inbound pre-send warning scope

- **Decision**: warn (soft, stderr, non-blocking) before a send to a peer when the
  sender owes that **same peer** an open decision-request (`proposal` or operator
  escalation, i.e. a `needs_operator` thread). Suppress when the outgoing message is a
  reply on the same `request_id`. Best-effort: swallow any derivation error.
- **Rationale**: targets the real "crossed decision-requests" incident without becoming
  a general nag. Same-peer + decision-kind keeps the signal-to-noise high.
- **Alternatives considered**:
  - *Warn on any owed thread (incl. questions/reviews)* — rejected: too noisy; reviews
    and questions already have `threads`/`sync` surfacing.
  - *Block the send until resolved* — rejected (FR-014): advisory only; blocking a
    legitimate send is worse than the crossed message it prevents.
  - *Cuttable*: if implementation balloons, drop FR-012..014 from 0.24.0 (C-004).
