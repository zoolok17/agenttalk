# Feature Specification: Trusted-Team Safety 0.16.0

**Mission**: `trusted-team-safety-0160-01KTCQ3D`
**Created**: 2026-06-05
**Source**: Phase A of the #19 identity/authz RFC (`docs/rfc-identity-authz.md`)
**Target branch**: `master`

## Overview

agenttalk is a file-backed message bus shared by a small team of trusted AI
agents and one human operator. Today the roster is an advisory list: any agent
can rename itself, a removed agent's identity can be silently reused, and there
is no team-wide way to say "everything before this point is a previous run —
re-confirm before acting on it." Production bands have hit all three: stale
openers acted on after a context reset, identity confusion after a rename, and
no clean "void the old run" signal.

This release delivers the **trusted-team safety** slice of the identity/authz
RFC. It makes identity a first-class, append-only registry; makes rename and
removal safe by turning them into *retirement* (the old name becomes a permanent
tombstone, never re-bindable) rather than rewriting history; and gives the team
a lightweight, transcript-visible **global barrier** primitive plus a
**`check --epoch`** pre-action currentness gate so an operator can mark a clean
epoch boundary and agents can cheaply ask "is the thing I'm about to act on from
the current epoch?" It also surfaces a read-only **next-owner / next-action**
hint on open threads so a tool can see who owes the next move.

Everything here is **trusted-team safety, not authorization**. It assumes every
roster member is cooperative and non-malicious. It does NOT defend against a
local peer that forges sends, edits `config.json`, or deletes barrier messages.
That honest boundary is stated in the docs and security notes, and the harder
guarantees are explicitly deferred to later RFC phases (B/C/D).

## User Scenarios & Testing

### Actors

- **Operator** — the human (or liaison agent) who manages the team, marks epoch
  boundaries, and retires/renames agents.
- **Team agent** — a cooperative AI agent (e.g. `claude`, `codex`) that sends
  and receives messages and is expected to call `check --epoch` before acting on
  a tracked request.
- **Tooling** — skills and scripts that read `threads --json` / `sync --json` to
  decide what to do next.

### Primary flows

1. **Mark a clean epoch boundary (void the previous run).**
   An operator finishes a chaotic run and wants the team to treat everything
   prior as superseded. They fire a global barrier. It is recorded as an ordinary
   transcript-visible message carrying barrier meta; its message id becomes the
   new global epoch id. Every tracked opener sent after it automatically records
   `epoch_at_send` equal to that barrier id.

2. **Cheaply check currentness before acting.**
   An agent is about to act on a tracked request opened earlier. It runs
   `check --to-request <rid> --epoch`. If the request's `epoch_at_send` matches
   the current global epoch, it is current; if a newer global barrier exists, the
   request is reported as from a previous epoch (stale), and the agent re-confirms
   with the operator instead of blindly acting.

3. **Safely rename an agent.**
   An operator renames `codex` to `codex-rev`. The system checks for in-flight
   work owed to/from the old name (`--drain-check`), then *retires* `codex`
   (permanent tombstone) and registers `codex-rev` as a new active identity.
   History referencing `codex` stays valid and readable; `codex` can never be
   re-bound to a new agent.

4. **Retire an agent that is leaving.**
   `roster retire codex` makes `codex` a tombstone: it can no longer send, its
   name cannot be reused, but every historical message it sent remains valid and
   attributable.

5. **Attempt to remove a retired/active agent.**
   `roster remove codex` is refused with a clear hint pointing the operator at
   `roster retire` (which preserves historical readability). An operator who
   knowingly accepts that historical-read breakage can force it with an explicit
   override flag.

6. **A retired identity has one last thing to forward.**
   Optionally, and only as an explicit single hop, a retired identity's
   outstanding obligation can be forwarded to a live agent, with the forwarding
   recorded in transcript-visible meta so the redirect is auditable.

7. **Tooling sees who owes the next move.**
   A skill reads `threads --json` and finds, on each open thread, a read-only
   `next_owner` / `next_action` hint derived from thread state, so it can route
   work without guessing.

### Acceptance scenarios

- Firing a barrier produces a normal message (no new kind) that appears in
  `recv`/transcript and whose own message id is the epoch id.
- A tracked opener minted by an epoch-aware client after a barrier carries
  `epoch_at_send` == that barrier id; one minted when no barrier has fired yet
  carries `epoch_at_send` == `null`; a pre-0.16.0 opener omits the key entirely.
- `check --to-request <rid> --epoch` exits with the documented currentness code
  and prints current vs. previous-epoch status; with no barrier in history it
  reports current.
- `roster retire <name>` then `send --from <name> ...` is refused with exit 2 and
  a tombstone message; the retired name cannot be re-registered as active.
- `roster rename <old> <new> --drain-check` refuses when work is owed to/from
  `<old>` and otherwise retires `<old>` and activates `<new>`; messages from
  `<old>` remain valid.
- `roster remove <name>` without force is refused with a retire hint; with the
  explicit force override it proceeds and warns about historical-read breakage.
- `threads --json` and `sync --json` include `next_owner` / `next_action` on open
  rows and omit them where not derivable; pre-existing JSON keys are unchanged.
- A store with no registry, no barriers, and no retirements behaves exactly as
  0.15.0 did (full backward compatibility).

### Edge cases

- Barrier fired, then a message with an older lexicographic id is observed (clock
  skew / out-of-order arrival): epoch ordering is by message id, and the RFC's
  wall-clock-not-real-time caveat is documented; the system does not claim
  real-time ordering.
- Barrier message deleted by a local writer: `check --epoch` **fails open**
  (treats the surviving log as authoritative and may report "current" when a
  suppressed barrier would have said otherwise). This is documented as a
  trusted-team limitation, not a defended attack.
- Renaming to a name that is already an active identity, or to a retired
  tombstone: refused with a clear error. Likewise `roster add <retired-name>`
  is refused (non-rebindable applies to add, not only rename).
- `check --epoch` on a pre-0.16.0 opener (`epoch_at_send` absent) when a barrier
  exists: reported do-not-act (exit 3, `unknown-pre-epoch`), because the opener
  predates epochs and must be re-asked for irreversible actions. With NO barrier,
  absent is fine (exit 0).
- A broadcast opener fanned out across N recipient copies: all copies of one
  `broadcast_id` carry the SAME `epoch_at_send` (snapshotted once before fan-out),
  even if a barrier lands mid-fan-out; `--resume` preserves that stamp.
- Retired forwarding without `--to-request`, or for a request not owed to/from
  the retired identity: refused.
- `--drain-check` with outstanding obligations: refused, listing what is owed.
- Forwarding attempted more than one hop, or from an active (non-retired)
  identity: refused (single-hop, retired-only).
- Operator on an old client (no registry support) reading a store that has a
  registry: registry data is additive and ignored safely by old readers.

## Requirements

### Functional Requirements

| ID | Requirement | Status |
|----|-------------|--------|
| FR-001 | The system SHALL maintain an identity registry in `config.json` recording, for each identity, at minimum its name and lifecycle state (active or retired). | Draft |
| FR-002 | Retiring an identity SHALL record a permanent tombstone; a retired name SHALL NOT be re-bindable to a new active identity by ANY registry operation — explicitly including `roster add` / `add_agent` and `roster rename <_> <retired>`, not only rename. (A `remove --force` name leaves no tombstone and therefore remains re-addable; that is the documented distinction from retire.) | Draft |
| FR-003 | The system SHALL provide `roster retire <name>`, which transitions an active identity to retired and reports success/failure with a clear message. | Draft |
| FR-004 | A retired identity SHALL be refused as the `--from` of a `send` (and any send-equivalent), with the documented usage exit code and a tombstone explanation. | Draft |
| FR-005 | The system SHALL provide `roster rename <old> <new> --drain-check`, which refuses when work is owed to or from `<old>`, and otherwise retires `<old>` and registers `<new>` as a new active identity. | Draft |
| FR-006 | After a rename or retirement, all historical messages authored by the old/retired identity SHALL remain valid and attributable (history is never rewritten). | Draft |
| FR-007 | `roster remove <name>` SHALL be refused by default with a hint directing the operator to `roster retire`; an explicit force override SHALL allow removal while warning that historical readability for that identity may break. | Draft |
| FR-008 | The system SHALL support optional, explicit, single-hop forwarding of a SPECIFIC outstanding request (identified by `--to-request <rid>`) owed to/from a retired identity, by emitting an ordinary message to a live agent carrying transcript-visible meta (`forwarded_from`, `forwarded_request_id`). The sender SHALL be an explicit `--from` or the operator-facing identity — never the target by default. Forwarding a request not owed to/from the retired identity, forwarding from an active (non-retired) identity, or a second hop SHALL be refused. | Draft |
| FR-009 | The system SHALL allow any active roster member to fire a global barrier, recorded as an ordinary message (no new message kind) carrying barrier meta. | Draft |
| FR-010 | The global epoch id SHALL be the message id of the latest validated global barrier event; there SHALL be no separate epoch counter. | Draft |
| FR-011 | Tracked request openers minted by an epoch-aware client SHALL automatically carry `epoch_at_send`: the current global epoch id when a barrier exists, or `null` when no barrier has fired yet. The key SHALL be absent only on openers written by pre-0.16.0 clients. (Deliberate, documented exception to the general absent-not-null convention — `null` is a meaningful state, "epoch-aware sender, no barrier yet", distinct from absent, which means "pre-epoch opener" and is checkable only for thread-local rescind.) | Draft |
| FR-012 | The system SHALL provide `check --to-request <rid> --epoch`, reporting whether the request's `epoch_at_send` matches the current global epoch (current, exit 0) or is older / indeterminate (exit 3, do-not-act). "Older" includes `null` once a barrier exists; "indeterminate" includes an ABSENT `epoch_at_send` when a barrier exists (a pre-epoch opener that must be re-asked for irreversible actions) — this maps to exit 3, NOT a passing exit 0, because automation gates on the exit code. With no barrier in history, the epoch dimension is current (exit 0). | Draft |
| FR-013 | With no barrier in history, `check --epoch` SHALL report the request as current. | Draft |
| FR-014 | `threads --json` and `sync --json` SHALL include read-only `next_owner` and `next_action` fields on open threads where derivable, and SHALL omit them where not derivable. | Draft |
| FR-015 | `next_owner` / `next_action` SHALL be derived from thread state only; they SHALL NOT be settable by senders and SHALL NOT alter delivery, unread, or thread-closure behavior. | Draft |
| FR-016 | Documentation and security notes SHALL state that this release is trusted-team safety only — not malicious-peer authorization, not deletion-suppression defense — and SHALL document that `check --epoch` fails open against barrier suppression until a later phase. | Draft |

### Non-Functional Requirements

| ID | Requirement | Threshold | Status |
|----|-------------|-----------|--------|
| NFR-001 | Runtime dependencies SHALL remain Python standard library only. | Zero non-stdlib runtime imports added. | Draft |
| NFR-002 | All new message/JSON fields SHALL be strictly additive: absent (not null) when the feature is unused, and pre-existing keys unchanged. The sole, documented exception is `epoch_at_send`, where `null` is a meaningful stamped value (see FR-011). | A 0.15.0 store and 0.15.0-shaped messages validate and render unchanged under 0.16.0. | Draft |
| NFR-003 | Message history SHALL remain immutable: no operation in this release edits, rewrites, or deletes existing message files. | No code path mutates an existing message file; rename/retire touch only `config.json`. | Draft |
| NFR-004 | The exit-code contract SHALL be preserved and extended only additively; existing codes keep their meaning. | New currentness/refusal outcomes map onto the documented code contract without changing existing codes. | Draft |
| NFR-005 | The full CI matrix (py3.10–3.13 × 3 OSes) SHALL be green before any release tag. | `gh run watch` shows all matrix jobs passing before tagging. | Draft |
| NFR-006 | Behaviour SHALL be correct on Windows first (PowerShell), and on Linux/macOS. | pytest suite passes on all three OSes in CI. | Draft |
| NFR-007 | Each work package SHALL pass Codex cross-review over agenttalk before being considered done. | A `review-result` with status=approved exists per WP. | Draft |

### Constraints

| ID | Constraint | Status |
|----|------------|--------|
| C-001 | DO NOT implement per-agent cryptographic identity in this release. | Active |
| C-002 | DO NOT implement policy permissions beyond trusted-team barrier/retirement rules. | Active |
| C-003 | DO NOT implement hash-chain replay defense. | Active |
| C-004 | DO NOT introduce a new message kind for barriers; barriers are meta-marked ordinary messages. | Active |
| C-005 | The identity registry lives in `config.json` and is "no more trustworthy than that roster" — it is trusted-team metadata, not an authenticated authority. | Active |
| C-006 | Governance rituals (when to fire a barrier, how the team interprets epochs) remain conventions layered on top, NOT transport-enforced behavior. | Active |

## Success Criteria

- **SC-001**: An operator can void a previous run with a single barrier command and every subsequently opened tracked request is automatically epoch-stamped — no manual tagging.
- **SC-002**: An agent can determine, in one `check --epoch` call, whether a request it is about to act on belongs to the current epoch, and the team's skills can gate on that result.
- **SC-003**: An operator can rename or retire an agent without ever invalidating or rewriting historical messages, and a retired name can never be silently reused.
- **SC-004**: Tooling can read who owes the next move on any open thread directly from `threads --json` / `sync --json` without inferring it heuristically.
- **SC-005**: A team upgrading from 0.15.0 sees zero behavior change until they opt into the new commands — old stores, messages, and scripts keep working unchanged.
- **SC-006**: The docs leave no reader with a false sense of security: the trusted-team boundary and the fail-open barrier limitation are stated plainly.

## Key Entities

- **Identity registry**: append-only record in `config.json` mapping each agent
  name to a lifecycle state (active / retired-tombstone), plus any metadata
  needed for rename lineage. Retired entries are permanent.
- **Global barrier event**: an ordinary message carrying barrier meta
  (`meta.barrier = {version, scope: "global", type: "epoch-bump"}`). Its message
  id is the global epoch id.
- **Epoch stamp (`epoch_at_send`)**: the global epoch id recorded automatically
  on a tracked opener at send time by an epoch-aware client; `null` when no
  barrier has fired yet; absent only on pre-0.16.0 openers (three-state).
- **Next-owner / next-action hint**: read-only, state-derived fields on open
  threads in `threads --json` / `sync --json` indicating who owes the next move
  and what kind of move it is.

## Assumptions

- The roster is small and fully trusted; all members are cooperative and
  non-malicious. (This is the explicit boundary of Phase A.)
- The exact `config.json` registry shape, the precise `next_owner` / `next_action`
  vocabulary, the exact barrier meta field names, and the currentness exit codes
  will be pinned in `/spec-kitty.plan` (`data-model.md` / `contracts/`), grounded
  in the RFC's specified shapes. Where the RFC already names a shape (epoch id =
  barrier message id; `meta.barrier` with version/scope/type; absent-not-null
  additivity), that shape is authoritative.
- "Tracked openers" are the existing opener kinds the bus already correlates by
  `request_id` (review-request, question, escalate, etc.); `epoch_at_send` is
  attached to those, matching the RFC.
- The exit-code contract from 0.14.0/0.15.0 (0 ok, 1 wait-timeout, 2
  usage/refusal, 3 superseded/stale, 4 unknown rid, 5 partial fan-out, 130
  SIGINT) is the baseline; `check --epoch` currentness reuses the
  current/superseded/unknown semantics rather than inventing new codes.

## Out of Scope (this release)

- Per-agent cryptographic identity or signed identity-bound messages (RFC Phase
  B/C).
- Any authorization/permission policy beyond trusted-team barrier/retirement
  rules (RFC Phase B/C).
- Hash-chain replay/deletion defense and external checkpointing (RFC Phase D).
- A dedicated barrier message kind or a barrier counter.
- Making `operator_facing` enforceable (it remains advisory until real authz).
- The Phase B stdlib-crypto fork decision (stay / external-signer / relax) — that
  is an operator decision gated after this release.
