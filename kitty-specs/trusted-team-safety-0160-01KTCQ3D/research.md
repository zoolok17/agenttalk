# Research & Design Decisions: Trusted-Team Safety 0.16.0

This is Phase A of an already-reviewed RFC (`docs/rfc-identity-authz.md`), so
most decisions are inherited. This file records (a) the inherited decisions that
constrain implementation and (b) the resolutions to the two genuine
implementation subtleties the RFC leaves to the implementer.

## D1 — Barrier representation: one message, one epoch id

**Decision**: A barrier is a single ordinary message (`kind=message`, NOT
fanned out to the roster) carrying
`meta.barrier={"version":1,"scope":"global","type":"epoch-bump"}`. Its own
message id is the global epoch id. Team-wide visibility does NOT depend on
per-recipient delivery: `current_epoch()` scans the *entire* validated message
log for the latest `meta.barrier` (global) event, regardless of recipient.

**Rationale**: The RFC states "epoch id = the barrier **message** id" (singular).
A broadcast/fan-out creates N message files with N distinct ids sharing a batch
id — that would make "the barrier message id" ambiguous. Modeling the barrier as
one message file sidesteps the ambiguity entirely and matches the RFC text
literally. Discovery-by-store-scan (not by inbox delivery) is what makes a single
message globally authoritative for the epoch.

**Recipient of the single barrier message**: `--to` defaults to the firer
(self-addressed) so it is a valid roster message with one id; the body is audit
prose. `current_epoch()` ignores recipient. (Operators who want the barrier to
*also* land in everyone's inbox can additionally broadcast a note — out of scope;
the barrier's job is to mark the epoch, not to notify.)

**Alternatives considered**: (a) fan-out barrier with epoch id = batch id —
rejected: invents a "batch id is epoch" rule the RFC didn't specify and couples
epochs to broadcast mechanics. (b) a counter in config.json — rejected
explicitly by the RFC (epoch id is a message id, no counter; counters in
attacker-writable config are not trustworthy and break the "ordered by message
id" property).

## D2 — `epoch_at_send` is a three-state field (absent / null / id)

**Decision**: An epoch-aware (0.16.0+) client minting a tracked opener ALWAYS
records `meta.epoch_at_send`:
- `<barrier-message-id>` when a global barrier exists at send time;
- `null` when no barrier has ever fired.
The key is **absent** only on openers written by pre-0.16.0 clients.

**Rationale (RFC lines 386–388, 407–409)**: The three states carry distinct
meaning for `check --epoch`:
- **absent** → pre-epoch opener; `check --epoch` cannot reason about epoch, only
  thread-local rescind; for irreversible actions, treat as "re-ask under the
  current barrier."
- **null** → epoch-aware opener sent before any barrier; once a barrier fires,
  `null` is *older than* the current epoch ⇒ correctly reported **stale /
  previous-epoch** (the opener predates the barrier).
- **`<id>`** → compare to `current_epoch()`: equal ⇒ current; older ⇒ stale.

This is a deliberate, documented exception to the project's general
absent-not-null additivity convention (NFR-002). `null` here is *not* "feature
unused" — it is a meaningful stamped value distinguishing an epoch-aware sender
from a legacy one. Documented in the spec (FR-011) and in SECURITY/README.

**Which kinds get stamped**: the existing `OPENER_KINDS`
(`review-request`, `question`, `proposal`) — the kinds the bus already
correlates by `request_id` and that thread derivation treats as openers.
Stamping happens centrally in `Store.send()` so every path (CLI, skills) gets it
uniformly.

## D3 — Retired identities stay in the validation roster, are refused as senders

**Decision**: Introduce a distinction between the **active roster** (`agents`)
and the **known roster** (`agents` ∪ retired names). Two consumers diverge:
- `Message.validate(roster)` and `load_config`-time history validation use the
  **known roster** so historical messages from a retired identity remain valid
  (FR-006, immutable history).
- The `send()` sender/recipient guard uses the **active roster** so a retired
  identity is refused as a new `--from` (FR-004) and you cannot send *to* a
  retired identity either (it cannot act on it).

**Rationale**: Retiring must never invalidate history (the never-rewrite rule).
But a tombstone must not be able to send. The only clean way to honor both is to
keep retired names "known for validation" while removing them from "active for
sending." Case-insensitive uniqueness (`validate_agent_roster`) must consider the
known roster so a retired name can never be re-bound (FR-002).

**Implementation note**: `Store` gains `active_agents()` and `known_agents()`
(active ∪ retired) helpers; existing call sites that validate *history* switch to
`known_agents()`; the `send()` guard and roster-audience resolution keep using
`active_agents()`. This is the highest-risk refactor (touches validation) and
gets the most test coverage + Codex scrutiny.

## D4 — `retired` registry shape in config.json

**Decision**: Add an additive `retired` key: a list of objects, each
`{"name": <str>, "retired_at": <iso>, "renamed_to": <str|null>, "reason":
<str|null>}`. Absent ⇒ no retirements (full 0.15.0 behavior). `renamed_to` links
a rename's tombstone to its successor (lineage / forwarding target hint).

**Rationale**: A list-of-objects (vs a bare list of names) captures retirement
time, rename lineage, and reason without a second config key, and stays additive.
`load_config` validates it fail-closed (each name a safe identifier; names
disjoint from active `agents`; no duplicate tombstones) exactly as it validates
`groups`/`roles`.

**Why config.json and not a message**: The RFC fixes the registry in
`config.json` and is explicit it is "no more trustworthy than that roster" —
trusted-team metadata, not an authenticated authority. Retirement is roster
admin, the same surface as `add_agent`/`set_role`.

## D5 — `check --epoch` exit codes reuse the existing contract

**Decision**: `check --to-request RID --epoch` returns:
- `0` current — no superseding rescind AND `epoch_at_send` equals
  `current_epoch()` (or no barrier exists at all);
- `3` superseded/stale — a valid requester rescind supersedes it (existing
  behavior) **OR** `epoch_at_send` is older than `current_epoch()` (new
  epoch-stale case); the human/JSON output distinguishes "rescinded" from
  "previous-epoch";
- `4` unknown — no such thread visible.

**Rationale**: The RFC says `check --epoch` verifies *both* the rescind condition
and the epoch condition. Folding epoch-stale into exit 3 keeps the exit-code
contract unchanged (NFR-004) — a caller that already treats 3 as "do not act"
behaves correctly without knowing about epochs. The distinction surfaces in the
message text / JSON `reason`, not a new code.

**Absent `epoch_at_send` under `--epoch` (REVISED per Codex B1)**: if NO barrier
exists, absent is current (exit 0). If a barrier EXISTS and the opener predates
epochs (absent), `check --epoch` returns **exit 3** (`epoch="unknown-pre-epoch"`,
do-not-act), NOT a passing exit 0. Rationale: `check` is the last executable
safety point before an irreversible action (RFC 401–405) and automation gates on
the exit code; "old requests must be re-asked under the current barrier for
irreversible actions" (RFC 407–409) is only enforceable if the exit code says
do-not-act. An advisory note at exit 0 would let an exit-code-gated caller act on
a pre-epoch request after an epoch boundary — exactly the crossing the barrier
exists to prevent. (My first draft made this exit-0-advisory; Codex correctly
rejected it.)

## D7 — Non-rebindable guard covers `add_agent`, not only rename (Codex B2)

The tombstone non-rebindability (FR-002) must be enforced at EVERY write path
that introduces an active name — `add_agent` / `roster add` as well as
`rename_agent`. The existing `add_agent` only validates the active roster, so
without an explicit guard `roster add <retired-name>` would write a config that
puts a name in both `agents` and `retired` (then `load_config` fail-closes on the
NEXT read — too late, and a poor UX). Fix: `add_agent` refuses a name present in
`known_agents()` (active ∪ retired) with a tombstone-aware error. A
`remove --force` name leaves no tombstone, so it remains re-addable — that is the
intended distinction and must be tested explicitly.

## D8 — Broadcast epoch snapshot, one per logical broadcast (Codex B3)

`send()` stamps `epoch_at_send = current_epoch()` per call for point-to-point
openers. But broadcast openers fan out to N per-recipient copies in a loop; a
barrier landing mid-loop would split one `broadcast_id` across two epochs. Fix:
the broadcast path computes `current_epoch()` ONCE before fan-out and passes that
explicit `epoch_at_send` into every copy's frozen meta. `send()`'s
don't-overwrite-a-supplied-value rule keeps it intact, and `--resume` rebuilds
from the frozen copies with the original stamp. Test: all copies of one
`broadcast_id` share one stamp even when a barrier is fired between copies.

## D9 — Retired forwarding forwards a SPECIFIC request (Codex B4)

The RFC's "forward a retired identity's outstanding obligation" implies there IS
an identified obligation. My first contract had `roster forward <retired>
--to <live>` with no request id — that can only mint a generic note, not forward
an owed thread. Fix (option a of Codex's two): require `--to-request <rid>`,
validate the thread is genuinely owed to/from `<retired>`, and emit an ordinary
message to `<live>` carrying `meta.forwarded_from` + `meta.forwarded_request_id`
(+ `meta.forward.hop=1`). The sender is an explicit `--from` (must be active) or
the `operator_facing` identity — NEVER the target by default (a forward should
not look like it came from the agent receiving it). Second hop refused (a source
request already carrying forward meta cannot be forwarded again); active-source
and non-owed-request refused. See data-model §3b.

## D6 — `next_owner` / `next_action`: derived, read-only (REVISED)

**Decision**: Add two optional fields to the `Thread` dataclass (threads.py),
derived purely from existing thread fields:
- `next_owner`: the agent who owes the next move (an agent name, or for an
  outstanding broadcast the list of non-responders); omitted for terminal
  (`closed`/`closed-superseded`).
- `next_action`: a closed vocabulary of the values actually produced:
  `"reply"` | `"read-reply"` | `"await-reply"` | `"answer-operator"`.

**Surfacing (revised — see WP02/WP03 split)**: `Thread.to_dict()` does NOT emit
these. They appear on EVERY open thread (not feature-gated like the 0.15.0
keys), so emitting from `to_dict` would change the baseline thread JSON shape
and trip the 0.15.0 additivity gates. WP02 derives the fields onto the `Thread`;
WP03's CLI layer (`threads`/`sync --json`) injects them into the row dict and
updates the additivity gates. Never settable by senders; never affects delivery,
unread, or closure (FR-015).

**Rationale**: Addresses the band's soft-deadlock pain (a tool can see who owes
the next move) without making the bus a workflow engine — a *projection* of
state already computed. Tiny, closed vocabulary.

**Mapping (REVISED to match the real state semantics)**. Note: in
`derive_threads`, `reply-waiting` means a reply addressed to `self` is sitting
UNREAD (the ball is back with self — NOT awaiting the peer), while `open-outbound`
means `self` is waiting on the peer. The first draft had these inverted.
| thread state (from `self`'s view) | next_action | next_owner |
|---|---|---|
| `owed-inbound` (you owe a reply) | `reply` | self |
| needs_operator + operator_state == pending | `answer-operator` | self |
| `reply-waiting` (a reply to you is unread) | `read-reply` | self |
| `open-outbound` pairwise (you await the peer) | `await-reply` | the peer |
| `open-outbound` broadcast (members still owe) | `await-reply` | non-responders |
| `closed` / `closed-superseded` | (omitted) | (omitted) |

`act-or-rescind` was dropped from the vocabulary — no current state produces it,
and the vocabulary stays closed to values actually emitted.

## Inherited constraints (from RFC / prior releases — not re-litigated)

- Stdlib-only runtime (no asymmetric crypto exists in stdlib — Phase A claims no
  per-agent crypto; that's Phase B/C).
- History immutable: retire/rename mutate ONLY `config.json`; never touch message
  files. `remove --force` is the sole path that knowingly breaks historical
  readability, and it warns.
- No new message kind for barriers (C-004) — `kind=message` + `meta.barrier`.
- `check` is not atomic with the following action; skill contracts must run it
  immediately before acting (documented, not engineered away).
- Barrier suppression (delete/withhold) makes `check --epoch` fail open — Phase A
  is trusted-team correctness, not a malicious-peer control. Stated in
  SECURITY.md (C-005, FR-016).
- Governance (when to fire a barrier) stays convention, not transport-enforced
  (C-006).
