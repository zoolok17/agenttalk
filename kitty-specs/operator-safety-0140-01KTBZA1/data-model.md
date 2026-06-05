# Data Model: 0.14.0 Operator Safety

Phase 1 output. All structures are additive to the v0.13.0 store layout;
nothing existing changes meaning (NFR-001).

## 1. Rescind message (#12)

An ordinary message file under `.agenttalk/messages/`, kind `rescind`.

| Field | Value / rule |
|-------|--------------|
| `kind` | `"rescind"` — added to KNOWN_KINDS, NOT to CONTROL_KINDS |
| `sender` | must equal the thread's requester for the rescind to supersede (validated at derivation; also checked at send time for early feedback) |
| `recipient` | the thread's responder (resolved from the opener) |
| `meta.request_id` | **required** — the thread being rescinded |
| `meta.target_msg_id` | optional — pin a specific opener/message; default: the thread opener |
| body | optional human reason; carried verbatim into transcripts |

**Validation (send time, exit 2 on failure):** request_id must exist among
the sender's outbound thread openers; sender must be the requester.
**Semantics (derivation time):** see thread-state transitions below.
**Idempotency:** subsequent valid rescinds for the same rid do not change
state (first one decided it); they remain in the transcript.

## 2. Thread state additions (#12, #18)

`derive_threads` remains a pure function of (valid messages, agent,
cursor, now, closed_rids). New derived values:

### `closed-superseded` (terminal)

```
open / reply-waiting / owed-inbound / open-outbound
        │
        │  valid rescind r with r.sender == requester
        │  and r.id > opener.id (or > meta.target_msg_id when pinned)
        ▼
closed-superseded   (terminal; re-ask requires a fresh request_id,
                     same rule as manual ack closure)
```

Precedence (amended in WP01 review round 2): `closed` (manual ack,
existing) and `closed-superseded` are both terminal. A per-agent manual
ack always keeps its `closed` label — supersession overrides *derived*
states only, never an explicit ack; existing closure paths are
untouched. The non-acking party still derives `closed-superseded`, and
the `check` gate computes supersession from the log, unaffected by view
labels. (Derivation has no ack-timing information — `closed_rids` is a
set — so "earlier decider wins" is unimplementable without a signature
change; ack-always-keeps-its-label is the deterministic rule.)

### Escalation pending/answered/closed (derived, no new threadstate)

An escalation is an ordinary tracked question thread where the opener
carries `meta.needs_operator="true"`. `operator_state` is three-valued
(amended in WP01 review round 2):

```
pending    (opener sent; no qualifying reply yet)
   │
   │  liaison sends any non-control reply with the same request_id
   │  to the requester               (optionally meta.operator_answer)
   ▼
answered   (ordinary question closure — reuses existing rule; FR-014)

   │  alternative exits from pending: manual ack or supersession —
   │  terminal WITHOUT a liaison answer
   ▼
closed     (leaves the pending bucket; never fabricates an answer that
            did not happen)
```

Display only: the liaison's views bucket `operator_state == "pending"`
threads separately; the requester sees its escalation as open-outbound
until terminal. No new fields in `threadstate.json`.

## 3. Roster: operator_facing designation (#18)

`config.json` (additive key):

```json
{
  "agents": ["lead", "worker-a", "worker-b"],
  "roles": {"lead": "lead"},
  "groups": {"workers": ["worker-a", "worker-b"]},
  "operator_facing": "lead"
}
```

| Rule | Behavior |
|------|----------|
| Representation | single string (or absent) — **multiple liaisons are unrepresentable by construction** (refinement over warn-on-multiple; flag in WP review) |
| Set / clear | `roster set-operator-facing <agent>` / `--clear` |
| Validation | the named agent must be in the roster at set time; load_config treats an unknown/absent name as "not configured" (fail-open to absent, warn in diagnostics — consistent with roles/groups null-tolerance from 0.11.1) |
| Trust | advisory routing metadata only (C-007); config.json remains untrusted per SECURITY.md — the designation never affects message validity |

## 4. Escalation message (#18)

Ordinary `question` message sent by `agenttalk escalate`:

| Field | Value / rule |
|-------|--------------|
| `kind` | `"question"` (no new kind) |
| `recipient` | resolved `operator_facing` agent; `--to <agent>` overrides explicitly |
| `meta.request_id` | minted `esc-` + 12 hex (matches existing `rq-`/`q-`/`pp-`/`b-` convention) or `--meta request_id=` passthrough |
| `meta.needs_operator` | `"true"` — the bucket discriminator |
| body | the operator question; `--file -` supported (C-008) |

Refusal (exit 2): no `operator_facing` configured, designated agent not
in roster, or sender == liaison (it already owns the operator channel) —
each with a remediation hint.

## 5. Reply-in-flight marker (#14, conditional)

Observational state file `.agenttalk/state/<agent>.composing.json`
(same lifecycle discipline as `.heartbeat` / `.waiting`):

```json
{
  "agent": "codex",
  "threads": {
    "rq-1a2b3c4d5e6f": {"peer": "claude", "at": "2026-06-05T13:40:00Z"}
  }
}
```

| Rule | Behavior |
|------|----------|
| Written | by `composing --to-request RID` (alongside the composing message itself) |
| Read | by `threads`/`sync`/`status` to display "reply in flight" and suppress OPEN_OUTBOUND_STALE for that thread |
| Staleness | entry ignored after the same freshness window as composing extension (entry `at` older than the composing-extend cap ⇒ stale); heartbeat staleness also invalidates |
| Load-bearing | **never** — corrupt/missing file degrades to current behavior (C-004) |

## 6. Resolved root (#13)

Not persisted — a resolution rule:

```
--root flag  >  AGENTTALK_ROOT env  >  upward walk from CWD  (unchanged walk)
```

`init` preflight: resolve from the target's parent; if an existing store
is found up-tree, refuse (exit 2, naming it) unless `--force`.
`whoami`/`doctor` print `root: <resolved>` as their first line; `doctor`
additionally lists every `.agenttalk/` found between CWD and the
filesystem root when more than one exists.

## 7. Exit-code map (C-005 compliant)

| Code | Meaning | Used by |
|------|---------|---------|
| 0 | ok / current | all (unchanged); `check` = current |
| 1 | wait timeout (unchanged, exclusive) | `wait` |
| 2 | usage / refusal (unchanged) | all; new refusals: nested `init`, ambiguous `escalate`, invalid `rescind` |
| 3 | superseded | `check`; `wait --to-request` rescinded wake |
| 4 | unknown request id | `check` |
| 130 | SIGINT (unchanged) | all |

## 8. JSON output additions (all additive)

- `threads --json`: `state` may be `"closed-superseded"`; escalation rows
  carry `"needs_operator": true` and `"operator_state": "pending"|"answered"|"closed"`;
  reply-in-flight rows carry `"reply_in_flight": true`.
- `sync --json`: new `escalations` array for the liaison; rescinded
  threads flagged in the existing actionable/terminal sections.
- `status --json`: per-agent `operator_facing: true` where applicable;
  warnings list gains liaison/multi-store/stale-escalation entries.
- `whoami --json`: `operator_facing` boolean; `root` already present,
  ordering change only in human output.
