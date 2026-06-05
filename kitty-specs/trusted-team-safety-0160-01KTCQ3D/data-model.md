# Data Model: Trusted-Team Safety 0.16.0

All shapes are **additive** over the 0.15.0 store. A 0.15.0 `config.json` and
0.15.0-shaped messages remain valid and unchanged. New keys are absent on
upgrade until a feature is used (with the one documented `epoch_at_send=null`
exception).

## 1. Identity registry (`config.json`)

### Existing keys (unchanged)
```jsonc
{
  "agents": ["claude", "codex"],   // ACTIVE roster (sendable identities)
  "created_at": "...",
  "session_id": "...",
  "groups": { ... },               // optional (0.11.0)
  "roles": { ... },                // optional (0.11.0)
  "operator_facing": "claude"      // optional (0.14.0)
}
```

### New key: `retired` (optional, absent ⇒ none)
```jsonc
{
  "retired": [
    {
      "name": "codex",            // a former active identity; now a tombstone
      "retired_at": "2026-06-05T20:30:00Z",
      "renamed_to": "codex-rev",  // null if retired (not renamed)
      "reason": "renamed for review role"  // null allowed
    }
  ]
}
```

**Validation (fail-closed, at `load_config`)**:
- `retired` is a list of objects; each `name` passes `validate_agent_name`.
- Retired `name`s are **disjoint** from active `agents` (an identity is active
  XOR retired, never both).
- Retired `name`s are unique within `retired` (no duplicate tombstones).
- Case-insensitive uniqueness spans active ∪ retired (the *known roster*) — a
  retired name can never collide with or be re-bound as an active one.
- `renamed_to`, when non-null, is a safe identifier (it points at a successor;
  it need not currently be active — the successor may itself later retire).

### Derived roster views (new `Store` helpers)
- `active_agents()` → `config["agents"]` (sendable). Used by `send()` guard and
  audience resolution.
- `retired_agents()` → `[e["name"] for e in config.get("retired", [])]`.
- `known_agents()` → `active_agents()` ∪ `retired_agents()` (order: active first,
  then retired). Used for **history validation** so retired senders stay valid.

## 2. Global barrier event (a message)

A barrier is one ordinary message:
```jsonc
{
  "id": "20260605-203500-123456-AB12",   // <-- THIS id is the global epoch id
  "from": "claude", "to": "claude",       // self-addressed; recipient irrelevant to epochs
  "kind": "message",                       // NOT a new kind (C-004)
  "subject": "epoch bump",
  "body": "voiding the previous run; re-confirm before acting",  // audit prose
  "meta": {
    "barrier": { "version": 1, "scope": "global", "type": "epoch-bump" }
  }
}
```

**`current_epoch()` (new `Store` method)**:
- Scans `valid_messages()` for messages where
  `meta.barrier.scope == "global"` (and `meta.barrier` is well-formed).
- Returns the **id of the latest such message by message-id order** (the bus's
  deterministic lexicographic id ordering), or `None` if none exist.
- Pure read; no writes. Old clients that don't understand `meta.barrier` simply
  see a normal note (graceful degradation).

**Validation of `meta.barrier`** (lenient, additive): a message with a
malformed `meta.barrier` is still a valid message (meta is free-form) but is
ignored by `current_epoch()` (must have `version`, `scope`, `type` with
`scope=="global"` to count). This avoids a malformed barrier crashing the scan.

## 3. `epoch_at_send` (opener meta — three-state)

Stamped automatically in `Store.send()` when `kind in OPENER_KINDS`:
```jsonc
// barrier exists at send time:
"meta": { "request_id": "...", "epoch_at_send": "20260605-203500-123456-AB12" }

// epoch-aware client, NO barrier yet:
"meta": { "request_id": "...", "epoch_at_send": null }

// pre-0.16.0 client: key ABSENT
"meta": { "request_id": "..." }
```
- Value = `current_epoch()` result at send time (the latest barrier id, or
  `None`→serialized as JSON `null`).
- Never overwritten if a caller already supplied `epoch_at_send` (explicit
  caller intent wins). This is load-bearing for B3 below.
- Non-opener kinds never get the key.

**B3 (Codex review) — one epoch snapshot per logical broadcast**: a broadcast
opener (e.g. `broadcast --kind question`) fans out to N recipient copies in a
loop. If `send()` re-evaluated `current_epoch()` per copy, a barrier landing
mid-loop would give copies of the SAME `broadcast_id` different `epoch_at_send`
values. Fix: the broadcast path snapshots `store.current_epoch()` ONCE before
fan-out and passes that explicit `epoch_at_send` into every copy's (frozen) meta;
`send()`'s "don't overwrite a supplied value" rule then leaves it intact, and
`--resume` reconstructs from the frozen copies with the original stamp. All
copies of one `broadcast_id` therefore share one epoch stamp. (Point-to-point
openers keep the simple per-`send()` stamp — there is only one message.)

## 3b. Retired forwarding meta (B4 — Codex review)

`roster forward <retired> --to <live> --to-request <rid> [--from <agent>]
[--reason]` forwards a SPECIFIC owed request, not a generic note:
```jsonc
{
  "from": "claude",            // explicit --from, or the operator_facing identity; NEVER the target by default
  "to": "claude",              // the live target (--to)
  "kind": "note",
  "body": "<reason / audit prose>",
  "meta": {
    "forwarded_from": "codex",                  // the retired identity
    "forwarded_request_id": "<rid>",            // the owed request being redirected
    "forward": { "hop": 1 }
  }
}
```
Validation: `<retired>` ∈ retired tombstones; `<live>` active; `<rid>` is a real
thread owed to/from `<retired>` (derive via threads); refuse if the source
request already carries `meta.forward`/`meta.forwarded_from` (second hop) or if
`<from>` is not active. Sender resolution: `--from` if given (must be active),
else `operator_facing()` if set, else refuse asking for `--from`.

## 4. `check --epoch` result

| condition | exit | human | json `epoch` field |
|---|---|---|---|
| no superseding rescind; `epoch_at_send == current_epoch()` | 0 | `current` | `"current"` |
| no superseding rescind; no barrier exists at all | 0 | `current` | `"current"` |
| `epoch_at_send` older than `current_epoch()` (incl. `null` when a barrier exists) | 3 | `previous-epoch` | `"previous-epoch"` with `current_epoch` id |
| `epoch_at_send` ABSENT AND a barrier exists (pre-epoch opener) | **3** | `do not act — opener predates epochs; re-ask under the current barrier for irreversible actions` | `"unknown-pre-epoch"` with `current_epoch` id |
| valid requester rescind supersedes (existing) | 3 | `superseded` | n/a (rescind block) |
| unknown thread | 4 | `unknown` | — |

**B1 (Codex review)**: absent `epoch_at_send` + barrier present is exit **3**,
NOT a passing exit 0. `check` is the last executable safety point before an
irreversible action and automation gates on the exit code, so a pre-epoch opener
must fail closed (do-not-act) once an epoch boundary exists. With NO barrier at
all, absent is genuinely current (exit 0).

JSON output for `--epoch` adds an `epoch` object alongside the existing
`request_id`/`state`/`rescind` keys — additive; non-`--epoch` `check` output is
unchanged.

## 5. `next_owner` / `next_action` (ThreadRow, threads.py)

Two optional fields on `ThreadRow`, included in `to_dict()` (hence
`threads --json` / `sync --json`) only when derivable:
```jsonc
{
  "request_id": "...", "state": "owed-inbound",
  "next_owner": "claude",
  "next_action": "reply"
}
```
- `next_action` ∈ `{"reply", "await-reply", "act-or-rescind", "answer-operator"}`
  or omitted.
- `next_owner` is an agent name (or, for an outstanding broadcast, the list of
  non-responders) or omitted.
- Derivation table: see `research.md` D6. Pure function of `state`,
  `needs_operator`, `operator_state`, and (for broadcasts) the responded set.
- Terminal threads (`closed`, `closed-superseded`) omit both.
- **Read-only invariant**: no `send`/CLI path accepts a `next_owner`/`next_action`
  input; they never influence delivery, unread counts, or thread closure
  (FR-015). Enforced by construction (derived in `to_dict`, no setter).

## State transitions (retirement lifecycle)

```
active ──roster retire──▶ retired (tombstone, permanent)
active ──roster rename──▶ retired{renamed_to:new} + new active identity
retired ──(any re-add / rename-to)──▶ REFUSED (non-rebindable, FR-002)
active ──roster remove (no force)──▶ REFUSED (hint: use retire)
active ──roster remove --force──▶ removed from agents (warns: history-read breakage)
```

There is no transition OUT of `retired`. Tombstones are permanent.
