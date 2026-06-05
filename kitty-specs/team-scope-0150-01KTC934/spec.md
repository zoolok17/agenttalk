# Feature Specification: 0.15.0 Team Scope

**Mission**: `team-scope-0150-01KTC934`
**Created**: 2026-06-05
**Status**: Draft
**Source**: GitHub issues #15, #16, #17; ROADMAP Phase 2b; the 2026-06-05 production retro (band wishlist #2) + joint Claude/Codex design consults (threads 535a091f, and the #11-deferral notes).

## Overview

The 0.14.0 operator-safety release closed the band's top two asks. This
release closes the remaining production friction cluster around **teams
at scale**: reviewer-only questions obligating non-reviewers (placeholder
acks as a workaround), broadcast fan-out failing silently partway, and
562 permanently-stuck INVALID messages cluttering the store. Three
features: role-scoped audiences with honest not-applicable replies,
broadcast delivery accounting, and recoverable quarantine of invalid
messages.

## User Scenarios & Testing

### Primary actors
**Broadcaster** (often the lead) fanning a question out; **role members**
(e.g. reviewers) who owe answers; **non-members** who today get wrongly
obligated; the **operator** maintaining store hygiene.

### Scenario 1 — role-scoped broadcast (#15)

1. The roster assigns roles (`reviewer`, `implementer`); no hand-curated
   group needed.
2. The lead broadcasts a question `--to-role reviewer`; only agents whose
   role is `reviewer` receive copies and owe answers.
3. The roster's roles change later; historical obligations do NOT change
   (the audience was frozen into each copy at send time).

**Acceptance:** non-reviewers never see the thread nor owe on it; an
unknown/empty role refuses loudly; `threads` shows responded/pending over
exactly the frozen audience; a post-send role change alters nothing
historical.

### Scenario 2 — not-applicable reply (#15)

1. An agent receives a broadcast question that doesn't concern its role.
2. It replies `--na`: a structured not-applicable response that closes
   its obligation.
3. The broadcaster's view distinguishes "answered" from "responded (n/a)"
   instead of pretending everyone answered substantively.

**Acceptance:** the NA reply closes the obligation under existing closure
rules; broadcaster's `threads` shows the member under a distinct n/a
marking; requester-side and JSON output mark it distinctly and
additively.

### Scenario 3 — broadcast delivery accounting (#16)

1. A broadcast fans out to N recipients; delivery fails partway (e.g.
   disk error at copy k).
2. The sender sees exactly who received the message and who did not, plus
   a shared batch id on every copy.
3. `status` surfaces the incomplete fan-out instead of pretending the
   broadcast either fully happened or didn't.

**Acceptance:** every fan-out copy carries the same batch id; on partial
failure the command exits non-zero, names delivered AND missed recipients
in machine-readable form, and the incomplete batch is visible in status
warnings until resolved (re-send or rescind).

### Scenario 4 — quarantine of invalid messages (#17)

1. A store accumulates INVALID messages (early flag mistakes, unknown
   kinds, roster drift).
2. The operator runs `prune --invalid`: the files MOVE to a quarantine
   area — recoverable, never deleted; valid history untouched.
3. `status`/`doctor` report invalid counts before and quarantined counts
   after; restoring a quarantined file by hand is possible and documented.

**Acceptance:** "what gets pruned" is byte-identical to "what status
reports invalid"; after pruning, status reports zero invalid and the
quarantine dir holds exactly the moved files; thread state, cursors, and
HMAC verification over valid messages are bit-for-bit unaffected; a
`--dry-run` lists without moving.

### Edge cases

- `--to-role` with a role nobody holds → refuse (exit 2), like an unknown
  group; the sender is excluded from the audience as with groups.
- Role vs group name collision: explicit flags (`--to-role` vs
  `--to-group`) disambiguate; no implicit fallback from one to the other.
- NA reply on a point-to-point (non-broadcast) question: allowed — same
  closure, same distinct display.
- NA reply on review-request/proposal threads: refused — those contracts
  require their typed responses (review-result / proposal-response).
- Partial-failure mid-loop where the failure is the LAST recipient;
  where it is the FIRST (zero delivered).
- Prune on a store with zero invalid messages → no-op, exit 0.
- Quarantined file name collision on repeated prunes → never overwrite.
- Prune while signing is enforced: files invalid only-for-signature move
  like any other invalid file (recoverable if the key situation changes).
- Mixed-version bus: pre-0.15.0 readers ignore batch/audience/NA metadata
  (additive meta keys on existing kinds) and never see the quarantine dir.

## Requirements

### Functional Requirements

| ID | Requirement | Issue | Status |
|----|-------------|-------|--------|
| FR-001 | Broadcast supports targeting all roster members holding a given role, resolved at send time from the roles map. | #15 | Proposed |
| FR-002 | Each fan-out copy freezes its audience facts into message metadata at send time (kind of audience, the role/group label, and the resolved member list); thread derivation of historical obligations never consults the live roster config. | #15 | Proposed |
| FR-003 | An unknown role, or a role with no members besides the sender, refuses with a non-zero exit and an actionable message. | #15 | Proposed |
| FR-004 | A reply can be marked not-applicable; it closes the replier's obligation under the existing closure rules for questions (point-to-point and broadcast alike). | #15 | Proposed |
| FR-005 | Not-applicable responses are displayed distinctly from substantive answers in thread listings and structured output, for both broadcaster and replier perspectives. | #15 | Proposed |
| FR-006 | A not-applicable reply on a review-request or proposal thread is refused with guidance (those contracts require typed responses). | #15 | Proposed |
| FR-007 | Every broadcast fan-out stamps one shared batch identifier on all copies and preflights the recipient list before the first write. | #16 | Proposed |
| FR-008 | On partial fan-out failure, the command exits non-zero and reports delivered and missed recipients in both human and machine-readable form. | #16 | Proposed |
| FR-009 | An incomplete fan-out batch is surfaced in status warnings until every planned recipient has a copy (or the thread is rescinded). | #16 | Proposed |
| FR-010 | `prune --invalid` moves every message file failing validation into a quarantine area inside the store dir; `--dry-run` lists without moving. | #17 | Proposed |
| FR-011 | The prune selection is computed by the same validation gates that status/doctor use to report invalid messages. | #17 | Proposed |
| FR-012 | Quarantined files are recoverable: never overwritten, never deleted by the tool, and the restore path is documented. | #17 | Proposed |
| FR-013 | `status` and `doctor` report invalid counts and quarantine counts. | #17 | Proposed |

### Non-Functional Requirements

| ID | Requirement | Threshold | Status |
|----|-------------|-----------|--------|
| NFR-001 | Backward compatibility: pre-0.15.0 stores/readers unaffected; all new JSON/meta strictly additive (absent when unused, never null). | Full pre-existing suite passes unmodified; strict-shape gate tests | Proposed |
| NFR-002 | Prune of 10,000 messages completes in bounded time with no quadratic behavior. | ≤ 10 s on a local store | Proposed |
| NFR-003 | Every new failure mode fails loudly: non-zero exit + actionable stderr. | A test per error path asserting code and message | Proposed |
| NFR-004 | Docs/skills coverage for every new command/flag in both CLI flavors. | Skill-lint green; README rows + sections | Proposed |
| NFR-005 | CI gate: the full GitHub matrix (3.10–3.13 × 3 OSes) is green before the release is tagged. | `gh run watch --exit-status` success | Proposed |

### Constraints

| ID | Constraint | Status |
|----|-----------|--------|
| C-001 | Stdlib-only runtime. | Standing |
| C-002 | History immutable; quarantine MOVES invalid files (recoverable), never edits or deletes; valid files are never touched by prune. | Standing |
| C-003 | No new message kinds: NA replies are ordinary messages with structured meta (`response=not-applicable`); a first-class kind only if production proves meta too weak. | Agreed (consult) |
| C-004 | Audience freezing at send time — never plumb live roles/groups into thread derivation; historical obligations must not drift. | Agreed (consult) |
| C-005 | Exit-code contract preserved (0/1/2/130 + 3/4 from 0.14.0); new refusals use 2; partial fan-out failure uses a documented non-zero code that is none of the reserved ones. | Standing |
| C-006 | No true rollback for fan-out (local FS has no multi-file atomicity): preflight + manifest + explicit partial-failure surfacing instead of pretending atomicity. | Agreed (ROADMAP) |
| C-007 | Windows-first ergonomics; `--file -` everywhere bodies exist. | Standing |
| C-008 | Per-WP Codex cross-review; fresh-eyes review before release; CI gate before tag. | Process |
| C-009 | Scope priority: #15 → #16 → #17; #16 ships with #15 (role audiences expand fan-out usage). | Agreed |

## Success Criteria

1. A scripted 4-agent roster with reviewer/implementer roles routes a
   `--to-role reviewer` question to exactly the reviewers in 100% of
   runs; a post-send role change alters zero historical obligations.
2. The placeholder-ack workaround is dead: an NA reply closes the
   obligation and is displayed as n/a, never as a substantive answer.
3. An injected fan-out failure at any position k of N leaves a complete,
   accurate delivered/missed report and a visible status warning; zero
   silent partial broadcasts remain possible.
4. On a store seeded with hundreds of invalid messages, one prune
   empties the invalid report, the quarantine holds exactly those files,
   and every valid-message behavior (threads, cursors, signatures) is
   byte-identical before/after.
5. Full pre-existing suite passes unmodified; the GitHub CI matrix is
   green before tagging (NFR-005 — the 0.14.0 red-matrix lesson).

## Key Entities

- **Frozen audience meta**: per-copy facts — audience kind (role/group/
  all), label, resolved member list, shared correlation id.
- **NA response**: ordinary message + `response=not-applicable` meta;
  closes like any non-control reply; displayed distinctly.
- **Fan-out batch**: shared batch id across copies + a preflighted plan;
  partial state reportable at any time.
- **Quarantine area**: a directory inside the store dir holding moved
  invalid message files, collision-safe, recoverable by hand.

## Assumptions

- Roles remain single-valued per agent (the existing roles map);
  multi-role agents are out of scope until production asks.
- The quarantine dir lives under `.agenttalk/` (exact name decided in
  planning) and is excluded from message scanning by construction.
- Reply-all (#11) stays deferred; this release may dissolve its need.

## Out of Scope

- Reply-all (#11 — revisit with production evidence after #15).
- Identity/authz, epochs, retired identities (#19 RFC, running in
  parallel).
- True transactional fan-out (impossible on local FS — C-006).
- Automatic prune scheduling (manual operator command only).
