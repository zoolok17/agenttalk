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
  caller intent wins — but no normal path supplies it; this is just defensive).
- Non-opener kinds never get the key.

## 4. `check --epoch` result

| condition | exit | human | json `epoch` field |
|---|---|---|---|
| no superseding rescind; `epoch_at_send == current_epoch()` | 0 | `current` | `"current"` |
| no superseding rescind; no barrier exists at all | 0 | `current` | `"current"` |
| `epoch_at_send` older than `current_epoch()` (incl. `null` when a barrier exists) | 3 | `previous-epoch` | `"previous-epoch"` with `current_epoch` id |
| valid requester rescind supersedes (existing) | 3 | `superseded` | n/a (rescind block) |
| `epoch_at_send` absent AND a barrier exists | 0 | `current` + advisory note "opener predates epochs; re-ask for irreversible actions" | `"unknown-pre-epoch"` |
| unknown thread | 4 | `unknown` | — |

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
