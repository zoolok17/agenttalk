# Data Model — Obligation Dashboard (0.17.0)

The only new persisted-data surface is **none** — the dashboard writes
nothing. This document defines the `/api/state` response schema (the
feature's contract) and the in-process shapes that build it.

## 1. `/api/state` aggregate (schema_version 1)

```jsonc
{
  "schema_version": 1,            // integer; bumped only on breaking change
  "agenttalk_version": "0.17.0",
  "generated_at": "2026-06-07T15:30:00.123456+00:00",  // informational only (D7)
  "roots": [ RootObject, ... ]    // ordered as supplied via --store (or the single resolved root)
}
```

Field rules for THIS schema (it is new — additivity gates don't constrain its
interior, but we adopt the repo convention anyway): keys that carry no value
are **absent, not null**, with two documented exceptions that mirror the bus:
`epoch` is `null` until a barrier exists, and `epoch_at_send` is forwarded
exactly as stored (absent / null / id — the 0.16.0 three-state).

## 2. RootObject

```jsonc
{
  "label": "agenttalk",            // basename, deduped with ~2/~3 (D4)
  "path": "D:\\Projects\\claude\\agenttalk",
  "project_id": "abc123...",       // store identity; ABSENT if unreadable
  "errors": [],                    // ALWAYS present; strings; non-empty = degraded root (FR-005)
  "signing_enforced": false,
  "epoch": null,                   // current global epoch id, or null (no barrier yet)
  "counts": {
    "messages": 412,               // validated messages
    "invalid": 2,                  // quarantine candidates (count only)
    "open_threads": 5,
    "closed_threads": 44
  },
  "operator_facing": "claude",     // liaison name; ABSENT if none configured
  "agents": [ AgentEntry, ... ],   // active roster order from config
  "retired": [ "old-codex" ],      // tombstone names only
  "threads": [ ThreadRow, ... ],   // OPEN threads only, deduped per D5
  "broadcasts": [ BroadcastSummary, ... ],   // open broadcast threads' manifest view
  "spec_kitty": {                  // ABSENT unless kitty-specs/ exists at this root (FR-008)
    "kitty_specs_dir": "D:\\...\\kitty-specs",
    "missions": ["obligation-dashboard-0170-01KTHADQ"]   // directory names only
  }
}
```

A degraded root (corrupt config, missing store, unreadable files) keeps
`label`, `path`, `errors` (explaining the failure) and omits the data fields
it could not produce. HTTP status stays 200 (FR-005).

## 3. AgentEntry

```jsonc
{
  "name": "claude",
  "role": "lead",                  // ABSENT if unset
  "groups": ["devs"],              // ABSENT if none
  "operator_facing": true,         // ABSENT unless true
  "last_seen": "2026-06-07T15:29:58+00:00",  // ABSENT if never seen
  "last_seen_age_seconds": 2.1,    // ABSENT with last_seen absent
  "unread": 3,                     // messages past cursor
  "composing": {                   // ABSENT unless an intent marker is live
    "request_id": "abc-123",
    "peer": "codex",
    "age_seconds": 41.0
  }
}
```

## 4. ThreadRow (open threads only)

Reuses `Thread.to_dict()` field names verbatim, plus the CLI-layer additions
and the dashboard's mission context:

```jsonc
{
  "request_id": "6a3879ef-...",
  "opener_kind": "review-request",
  "subject": "review WP03 rev2: ...",
  "peer": "codex",                 // from the chosen perspective (D5)
  "state": "owed-inbound",         // the ball-holder's state
  "age_seconds": 512.3,
  "last_msg_id": "20260607-...",
  "unread": true,
  "next_action": "reply",          // closed vocabulary {reply, read-reply, await-reply, answer-operator}
  "next_owner": "codex",           // absolute agent name, or array (broadcast pending)
  "is_broadcast": true,            // broadcast-only trio, exactly as threads --json
  "audience": ["a", "b"],
  "responded": ["a"],
  "pending": ["b"],
  "needs_operator": true,          // escalation labels when applicable
  "operator_state": "pending",
  "mission": "trusted-team-safety-0160-01KTCQ3D",  // ABSENT unless in opener meta (FR-008)
  "wp_id": "WP03",                                  // ABSENT unless in opener meta
  "epoch_at_send": null,           // forwarded as stored: absent / null / "<id>"
  "epoch_status": "current"        // current | previous-epoch | unknown-pre-epoch (D6)
}
```

No `body` field exists in any `/api/state` shape (FR-003). Detail links are
client-derivable: `/messages/<last_msg_id>` (existing route, root[0] only in
v1 — multi-root message detail is out of scope; the HTML notes which root a
thread belongs to).

## 5. BroadcastSummary

```jsonc
{
  "request_id": "bc-...",
  "subject": "...",
  "opener_kind": "question",
  "requester": "claude",
  "audience": ["a", "b", "c"],
  "responded": ["a"],
  "pending": ["b", "c"],
  "age_seconds": 120.0
}
```

(Derived from the same deduped thread set — broadcasts also appear in
`threads`; this list is the convenience projection for the manifest panel.)

## 6. In-process shapes (web.py internal, not serialized)

- `RootDescriptor = (path: Path, label: str)` — built once at startup (D4).
- `build_state(descriptors) -> dict` — pure function over fresh per-request
  `Store` reads; the ONLY entry point `/api/state` calls. Per-root failures
  are caught inside and become `errors[]`.

## 7. Validation rules

- Schema version is a literal `1`; tests pin it.
- `roots[*].errors` is always a list (possibly empty) — consumers branch on
  it without existence checks.
- All timestamps are UTC ISO-8601.
- Thread rows appear at most once per `request_id` per root (D5 dedup test).
- `epoch_status` only on rows where the thread is open; values from the
  closed three-state vocabulary (D6).
