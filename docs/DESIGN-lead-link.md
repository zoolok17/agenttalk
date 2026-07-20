# Lead Link: direct lead↔lead messaging across machines

Status: DRAFT r2 — not normative, not scheduled. r1 (blob 01562533 @ bd6bf03)
was reviewed by the full local team + lead on 2026-07-20; r2 folds all findings
(dispositions on bus thread b-2efcd7b316bc). Written 2026-07-20.
Audience: the two team operators and the primary platform architect.
Scope: exactly two agenttalk stores on two computers, one lead each, coordinating
directly. Full bus federation is explicitly out of scope (ROADMAP parks
"remote cloud runners / hosted multi-tenant SaaS" under *Later — explicitly not
scheduled*; this design does not reopen that).

---

## 1. Problem

Two agenttalk teams run on two computers. Each bus is file-backed and
deliberately local: no cross-machine roster, no cross-machine messaging
(`docs/WORK-PACKAGE-native-work-spine.md`; team memory). The leads currently
talk over **agentchat**: a hand-rolled channel on a shared Google Drive folder
(`/g/My Drive/agenttalkdiscussion/chat/`) — one immutable JSON file per
message (`{from, to, seq, ts, reply_to, text}`, filename
`<from>__<seq6>__<rand8>.json`, append-only), each reader scanning the folder
against a per-reader seen-cursor keyed by sha256. It has real virtues, and its
operators value them. What it cannot be is fast or integrated:

- **Slow.** Delivery is Drive desktop-sync latency (seconds to minutes) plus
  reader polling; each hop costs a human-noticeable wait. That gap is the
  problem statement.
- **Unreliable, silently.** No delivery receipt, no gap alarm: a message stuck
  in sync looks identical to a peer who hasn't answered — the
  "idle vs. structurally unable to speak" ambiguity this team has already been
  burned by locally (memory `cli-build-must-match-agent-build`).
- **Outside the bus.** agentchat messages don't appear in `threads`, create no
  owed-inbound obligations, never trip `deadman`, and wake no armed wait.
  Every notification is a human noticing a file.

Requirement: the two leads talk **directly, without waiting**, and the
receiving lead is **automatically told** when a message arrives. A file-based
mechanism is acceptable. Teams stay local; only leads cross the machine
boundary. **Fail-safe over fast** (the architect's phrasing, adopted as a
requirement): a message you can't read beats one re-delivered badly.

### 1a. Invariants inherited from agentchat

agentchat's load-bearing properties are kept, not incidentally but as named
invariants — two of them encode bugs its operators already paid for:

1. **Immutable, append-only record.** Envelopes and receipts are write-once;
   nothing in this design edits or deletes a delivered artifact.
2. **Stable identities across processes and machines.** Every identity or
   dedup key in the protocol is an explicit sequence number, a minted message
   id, or a sha256 — never anything process-randomized (agentchat's original
   cursor used Python `hash()` and re-delivered all history on every restart).
3. **UTF-8 end-to-end.** Envelopes are UTF-8 JSON files; the courier's data
   plane is files and sockets — the console codepage never touches it
   (agentchat crashed on a cp1252 console before forcing UTF-8).
4. **Threading.** agentchat's `reply_to` maps onto the bus's
   `request_id`/`in_reply_to` meta on both ends (via the translation rules in
   §5).

## 2. The key observation: notification is already built

`agenttalk wait` is a filesystem poll over the local store
(`cli.py:cmd_wait`, `store.messages_for(agent, since_id=cursor)` every
`--interval`, adaptive backoff capped at `--max-poll-interval`, default 2.0 s).
The lead operating model (LEAD-GUIDE.md §4) is an interactive window that keeps
a **background `agenttalk wait --for <lead> --timeout 1800` armed at all
times** and re-arms after each wake.

Therefore: **anything that lands a message in the local store wakes the local
lead within ~2 seconds, with zero new notification machinery.** The entire
cross-machine problem reduces to one job:

> Move a message from store A to store B reliably and fast, and inject it into
> B **through `Store.send()`** so it is a first-class local message.

Injection through `send()` (never by copying message files) is load-bearing:

- ids are minted locally (`store.py:_new_id`) — UTC-time-prefixed, monotonic
  *per process*; delivery order is a lexicographic id compare. A foreign id
  minted by another machine's clock must never enter the store (clock skew
  would corrupt chronology; lesson `timestamps-are-not-ordering-keys`).
- this branch's publication-order ledger
  (`.agenttalk/state/message-publication-order.json` + its hash-chain anchor)
  requires every validated message to hold a contiguous sequence entry; once
  the sidecar exists, a message file without an entry makes `send()` and the
  obligations projection **fail closed** for everyone
  (`store.py:_reserve_message_publication_sequence`,
  `ValueError("validated message is missing durable publication order")`;
  observed live 2026-07-19, memory `cli-build-must-match-agent-build`).
- `send()` enforces roster principals, `KNOWN_KINDS`, epoch stamping, and the
  authority-sensitive validation path (`store.py:2641-2744`). A relay that
  bypasses it re-implements all of that badly.

## 3. Design overview

```
 machine A (site-a)                              machine B (site-b)
┌────────────────────────┐                     ┌────────────────────────┐
│ .agenttalk store A     │                     │ .agenttalk store B     │
│  roster: lead-a, devs… │                     │  roster: lead-b, devs… │
│        + lead-b  ◄─────┼── peer principal ──►│        + lead-a        │
│                        │                     │                        │
│  lead-a sends normally:│                     │  background wait armed │
│  send --to lead-b      │                     │  by lead-b (existing)  │
└─────────┬──────────────┘                     └───────────▲────────────┘
          │ courier tails store                            │ courier injects
          │ (read-only, own cursor)                        │ via Store.send()
┌─────────▼──────────────┐    transport        ┌───────────┴────────────┐
│ courier A              │  (direct push, or   │ courier B              │
│ outbox spool + receipts│◄── replicated ─────►│ outbox spool + receipts│
└────────────────────────┘      folder)        └────────────────────────┘
```

Four pieces, each small:

1. **Peer principal.** Each store adds the *remote* lead to its own roster
   (`agenttalk roster add lead-b --role peer-lead` on A, and vice versa).
   Names are already required to be safe identifiers and the two leads already
   have distinct names. Locally, sending to the remote lead is now just
   `agenttalk send --to lead-b …` — no new send-side UX, and threads/deadman
   track the exchange like any local conversation.
2. **Courier** (one small process per machine). Outbound: tail the local store
   *read-only* for messages whose recipient is the peer principal, keep a
   private cursor, and append each to a durable **outbox spool** (envelope
   files, atomic temp+rename, LF-normalized, sha256). Inbound: take envelopes
   from the peer, dedupe, validate, inject via `send(sender=<peer principal>,
   recipient=<local lead>, …)`, and write a **receipt**.
3. **Transport profile** — how spool entries cross the wire. Three profiles,
   one envelope format (§6):
   - `folder` (phase-0 bootstrap): a replicated directory (the existing
     Google Drive share; Syncthing also fits). Single writer per direction;
     receipts sync back the other way.
   - `direct` (target state; requires architect posture sign-off, §10):
     encrypted HTTP push between the two couriers (Tailscale or TLS).
     Sub-second, synchronous ack.
   - `git` (alternative): a dedicated private repo as the spool substrate;
     ~5–15 s, no new infrastructure accounts.
4. **Link state made visible on existing rails.** Delivery/ack state maps onto
   the peer principal's cursor; link liveness maps onto its heartbeat; failures
   raise operator `attention`/`escalate` items — so `status`, `threads`,
   `deadman`, and the dashboard report the link with no new views.

## 4. Identity, trust, and what a peer may say

- The envelope's sender must equal the configured peer principal. The courier
  **must only ever inject `sender == <peer principal>`** — stated as a policy,
  not a structural guarantee: `Store.send` accepts whatever active sender its
  caller names, so in phases 0–1 this is enforced by the courier's own code
  and config. Making it structural — a constrained peer-injection API — is an
  explicit phase-2 ask to the primary team. Reserved principals
  (`_avatars.RESERVED_PRINCIPALS`) are rejected as peer names outright, and
  the courier never passes `_allow_reserved_sender`.
- v1 recipients: the local lead only (configurable allowlist later), and the
  peer principal is reachable only by an explicit `--to <peer>` — never via
  role/group/`all` audience resolution (§7). Teams stay local by construction.
- **Two control axes, and the meta axis is strictly the stronger one.** The
  kind allowlist and the meta allowlist are not independent layers: a
  capability expressed as meta riding an allowlisted kind is structurally
  invisible to the kind list. The proving case is operator authority —
  `escalate` is not a kind at all; it sends an ordinary `question` carrying
  `meta.needs_operator` (`cli.py:5688-5697`), which the attention queue
  projects as a top-priority, always-blocking item that is *engineered never
  to be hidden* (`attention.py:54,66,80`). Only the meta rule below keeps a
  remote peer out of the local operator channel; no kind rule can.
- **Meta crosses by per-kind ALLOWLIST; everything else is quarantined.**
  Portable keys: `request_id` (namespaced on import, §5), message-id-valued
  keys (`in_reply_to` — translated through the id map, §5), and the
  proposal-correlation keys enumerated per admitted kind. **Every other key —
  including keys that do not exist yet — is moved under
  `meta.link_origin.foreign` or dropped.** Named examples of what therefore
  never crosses as itself: `attention`, `needs_operator`,
  `operator_answer`/`operator_command`/`operator_origin`, `release_authority`,
  `epoch_at_send`, `barrier`, `roster_revision`, `authorized_liaisons`,
  `origin_request_id`/`origin_inbound_id` (the authority-sensitive path in
  `Store.send`, store.py:2657-2741), and `broadcast_id`/`audience*` (v1 relays
  no fan-out; a lone message carrying `broadcast_id` derives as a different
  thread shape on the two stores, `threads.py:597-607`). A key not on the
  portable list does not cross as itself — closed by construction, not
  maintained by vigilance (the amendment #15 row-33 lesson).
- **`meta.link_origin` is the courier's own namespace.** It is written by the
  receiving courier and is never read from the incoming envelope's message
  meta. An envelope whose message already carries `link_origin` is
  dead-lettered, not treated as already-relayed — a forged `link_origin`
  would otherwise silently suppress delivery of a legitimate message, and
  "message never arrived" is the failure class this design exists to remove.
- **Kinds, v1: five in, seven out, default DENY.** IN: `message`, `note`,
  `question`, `proposal`, `proposal-response`. OUT, for three distinct
  reasons:
  - *Session control* — `end`, `release`, `composing`: precisely the kinds
    that change local control flow without an agent deciding anything
    (`LOOP_CONTROL_KINDS`, `wrapper/loop.py:147`; `CONTROL_KINDS`,
    `store.py:477`). The line is the code's own, not a judgment call.
  - *Redundant and hazardous* — `wake`: every injected message already wakes
    the armed wait, and a `wake` carrying a colliding `request_id` can
    register as the terminal answer to a tracked question
    (`threads.py:97-108`). No transport value, real semantic risk.
  - *Deferred pending contracts* — `review-request`/`review-result`: relaying
    them makes amendment #15's row-27 duplicate-opener residual reachable via
    routine recovery instead of deliberate action (§5); deferred until the
    #48 opener-identity work lands (§11). `rescind`: generic `send` refuses
    the kind (`cli.py:1487-1496`) because the dedicated command performs
    requester validation the link would bypass; it crosses only in v2 through
    a mapped, validated rescind path where the imported opener proves the
    peer is the requester.
  The allowlist is pinned to the `KNOWN_KINDS` frozenset (`store.py:431`) —
  never to CLI help text, which omits members — and is an IN-list: upstream
  kind #13 arrives **denied by default**, not relayed unreviewed.
- **Composing crosses as link state, not as a kind.** When receipts carry
  genuine drafting evidence from the far side, the receiving-side courier may
  re-emit a *local* `composing` as the peer principal, capped by local policy
  and the existing `_COMPOSING_MAX_EXTEND_SECONDS` ceiling. The remote peer
  never injects a control kind; the local courier owns the signal. Phase 0
  ships without this, which means cross-site scoped waits lose the long-draft
  extension — a documented cost, accepted for now, not an oversight.
- **Content trust is unchanged.** Locally, agenttalk's stance (SECURITY.md,
  `Message.validate` docstring, `docs/rfc-identity-authz.md`) is a
  trusted-team local bus: schema validation is not a defense against a writer
  of well-formed messages, and the roster is explicitly not a security
  boundary. The link keeps that stance honest: transport auth (§6) proves
  *which machine* wrote the envelope; it does not make the words true.
  Peer-lead messages remain prompt-injection surface and get the same
  skepticism as any bus message.
- **Envelope signing rides the existing signing model, with per-direction
  keys.** `signing.py` already does opt-in HMAC-SHA256 over a canonical
  payload (sorted-key compact JSON minus the signature field), with keys
  stored *outside* the attacker-writable store
  (`%LOCALAPPDATA%\agenttalk\keys\`, XDG on POSIX) — exactly the
  "anchor enforcement outside `.agenttalk/`" rule the identity RFC sets. The
  link reuses the pattern but a single shared key is **not** enough: one
  symmetric key proves possession by *either* endpoint, not which site or
  direction authored the bytes. So: **independent A→B and B→A signing keys**,
  plus a distinct transport credential, with `key_id` and rotation; the
  receiver pins `link_id` and its expected inbound direction and rejects
  anything else. Receipts are signed the same way (§5). Per-site asymmetric
  identity stays deferred with the RFC's future phases.

## 5. Delivery semantics

- **At-least-once transport + idempotent injection = exactly-once visible
  effect.** The dedupe key is the full tuple
  `(link_id, direction, origin_site, origin_message_id)` — never the origin
  id alone.
- **Per-direction sequence + hash chain.** Envelopes are numbered `seq = 1,2,…`
  per direction and carry `prev_sha256` of the previous envelope. The receiver
  detects gaps and requests replay from the spool; tampering or truncation is
  evident. This is the monotonic-causal-primitive pattern the codebase already
  prescribes over timestamps.
- **Fresh local identity at injection.** `send()` mints the local id/ts —
  cross-machine clock skew cannot corrupt local ordering. The origin id/ts
  live in `meta.link_origin` for display and correlation.
- **The delivered/origin↔local id map is correctness-critical state, not
  courier bookkeeping.** Amendment #15 ratified with one disclosed residual: a
  bound review link cannot distinguish a same-kind/sender/recipient message
  carrying the same `request_id` from its true opener (no authoritative
  opener id on the public surface — issue #48). Losing the delivered index
  and re-injecting manufactures exactly that duplicate through *routine
  recovery*. Therefore: the index gets the spool's durability treatment, and
  crash recovery in the window between `Store.send()` succeeding and the
  index write completing **scans the destination store for the signed
  `link_origin` tuple before retrying** — one injection survives. (This is
  also why review-* kinds stay out of v1, and why `link_origin.message_id`
  should ride to the architect together with #48: the link independently
  arrives at the same need — a stable origin-minted identity for a message
  whose local id was re-minted.)
- **`request_id` is namespaced on import.** Threads group globally by
  `request_id`, so a peer-chosen id passed raw could merge with — or
  terminally close — an unrelated local thread. Imported ids become
  `lnk-<link_id>-<origin id>`; the courier holds the reverse mapping durably
  and re-applies it on export, so each side sees one coherent thread and the
  wire sees the origin id. Collision is impossible by construction rather
  than improbable by luck.
- **Message-id-valued meta is translated, never passed raw.** `in_reply_to`,
  `target_msg_id`, and any future member of the reserved message-id meta list
  are rewritten through the id map at injection; the foreign originals are
  preserved under `link_origin`. An unresolvable reference **dead-letters**
  (fail-safe over fast) instead of injecting a dangling pointer that resolves
  to nothing — silently — on the local store.
- **Receipts are authenticated; delivery is a contiguous watermark.** A
  receipt is signed with the direction key and bound to `{link_id, direction,
  seq, origin id, envelope digest, injected local id, result}` — an HTTP 2xx
  is not delivery evidence, and a forged receipt is *worse* than a forged
  message (it advances the sender's delivery state and manufactures silent
  loss). The sender-side cursor (§7) advances only across the highest
  contiguous authenticated `result=delivered` prefix: a dead-letter at seq N
  pins the watermark below N even if N+1 injected, so `unread=0` can never
  overstate delivery.
- **Dead-letter, never silent drop — after authentication.** An envelope that
  fails validation (allowlist violation, malformed body, version skew) is
  written to `dead-letter/`, receipted as `result=dead-letter, reason=…`, and
  raised as an operator attention item **on both sides** — the sending lead
  is told their message did not deliver, the failure agentchat hides today.
  Two qualifiers: authentication runs **before** any durable dead-letter or
  attention work, so unauthenticated traffic cannot mount a disk/notification
  DoS; and courier-generated failure items are attributed to the **link
  identity**, never authored as if the remote lead said them.
- **Ordering scope.** Order is guaranteed per direction (seq). No global order
  across the two directions is claimed; replies correlate by thread, not by
  arrival order — same as the local bus.

## 6. Envelope and transport profiles

One envelope format for both profiles (`link_protocol: 1`):

```json
{
  "link_protocol": 1,
  "link_id": "sitea-siteb",
  "direction": "site-a->site-b",
  "seq": 42,
  "prev_sha256": "…",
  "origin": {"site": "site-a", "message_id": "20260720-…", "ts": "…"},
  "message": {"from": "lead-a", "to": "lead-b", "kind": "question",
               "subject": "…", "body": "…", "meta": {"request_id": "q-…"}},
  "sha256": "…"
}
```

Envelope files are written atomically (temp + rename), UTF-8, LF-only
(replicators and autocrlf must never be able to change bytes under a recorded
hash — memory `freeze-coordinates-crlf-hazard`), and named
`<seq 8-digit>-<origin_id>.json` so lexicographic listing is replay order.

**The v1 wire parser is strict, and unknown fields are REJECTED, not
ignored.** Exact protocol-v1 keys and types; bounded lengths, body size, and
nesting depth; duplicate JSON keys and non-finite numbers rejected; the HMAC
covers every semantic field except its own tag, over one canonical byte form,
and the hash chain runs over that same signed payload. Lenient parsing at a
brand-new trust boundary invites parser/version differentials — two builds
disagreeing about what a message *is*; schema evolution is handled by bumping
`link_protocol` (unknown version → dead-letter), not by silently carrying
fields one side doesn't understand. Signing applies to **both transport
profiles from phase 0** — the sha256 chain alone is integrity, not
authentication.

### Profile `direct` (target state; ships only with architect posture sign-off, §10)

- Each courier runs a stdlib `ThreadingHTTPServer` — the same stack the
  dashboard already uses (`web.py:111`), so no new dependency class. One
  **deliberate posture departure, called out loudly:** `web.py` hard-refuses
  any non-loopback bind (`LOOPBACK_HOSTS`, `web.py:132`), and today the
  codebase contains *zero* other network code, inbound or outbound. The link
  listener is the first non-loopback surface, so it is a **separate,
  link-only listener** — never an extension of the dashboard server — bound
  to a **private** address only (Tailscale tailnet IP or LAN IP; never
  `0.0.0.0`). agenttalk stays stdlib-only; Tailscale is operator
  infrastructure, not a package dependency.
- `POST /v1/link/<link_id>/messages` with the envelope; response is the
  signed receipt. Idempotent by the §5 dedupe tuple. A transport credential
  (distinct from the signing keys, stored in the keys dir outside
  `.agenttalk/`) is compared with `hmac.compare_digest` (the dashboard's
  CSRF-token precedent, `web.py:3215`).
- **Encrypted transport is mandatory; cleartext HTTP on a bare LAN is not a
  supported mode.** A private bind reduces exposure but is neither
  confidentiality nor peer authentication — on-path observers see the
  credential and the message bodies, and private address space is never an
  authorization boundary. Direct v1 requires an encrypted overlay (Tailscale)
  or TLS/mTLS with pinned peer identity.
- **Listener hardening (required, not advisory):** exact single-interface
  bind with a **runtime assertion** that refuses startup on any non-private
  resolved address (the `web.py` hard-refusal shape — a config field that
  *can* say `0.0.0.0` eventually does); source-IP allowlist for the peer;
  bounded Content-Length, header size, meta/body size, and concurrency
  (`ThreadingHTTPServer` is otherwise thread-unbounded); read/write timeouts
  and rate limits; exact method/path/content-type; no chunked or compression
  ambiguity; authentication and replay/gap checks **before** any filesystem
  or Store mutation; tokens and message bodies never logged. Outbound:
  destination pinned, redirects and ambient proxies disabled — credentials
  are never followable to a third host.
- Sender behavior: spool first (durable), then push immediately; on failure,
  retry with exponential backoff (1 s → 60 s cap) until acked. Push-on-send
  means **sub-second transport when both machines are up**; the spool is the
  queue when they aren't.
- Latency budget end-to-end: local capture ≤1 s (courier tail poll) + push
  ~10–100 ms + injection + receiver's `wait` poll ≤2 s ⇒ **lead-to-lead
  typically 1–3 s.**

### Profile `folder` (the phase-0 MVP; permanent fallback thereafter)

- A replicated directory with **strict single-writer-per-direction** layout:

  ```
  <share>/<link_id>/
    a-to-b/   messages/ 00000042-<id>.json …   ← only site A writes
              receipts/ 00000041.json …        ← only site B writes? NO —
    b-to-a/   messages/ …  receipts/ …         ← receipts live in the
                                                 direction the RECEIVER writes
  ```

  Concretely: site A writes `a-to-b/messages/` and `b-to-a/receipts/`; site B
  writes `b-to-a/messages/` and `a-to-b/receipts/`. Each file has exactly one
  writer ever, and files are write-once — the entire Drive conflict class
  (concurrent edits, conflict copies) is designed out rather than handled.
- Couriers poll the inbound direction every ~2 s (same adaptive-backoff shape
  as `cmd_wait`). With Syncthing (event-driven, LAN-direct) end-to-end is
  typically **3–15 s**; over the existing Google Drive share it degrades to
  Drive's sync latency — but becomes *reliable*: receipts, seq-gap detection,
  and deadman turn "lost in Drive" from silence into an alarm.

### Profile `git` (alternative; the no-new-accounts option)

- A **dedicated** private GitHub repo — never the code repo; chat must not
  enter the PR/issue integration point — holds the same per-direction spool
  layout, pushed as write-once files. Push lands in ~1–2 s; the receiver
  polls with conditional requests (304 responses don't count against API rate
  limits) every 5–10 s ⇒ **~5–15 s end-to-end**. Setup: one private repo,
  existing credentials. Trust: GitHub sees message content — the same trust
  the operators already extend it for code — and the git history doubles as
  an immutable, tamper-evident audit ledger. Same envelope, signing, receipt,
  and quiescence rules as `folder`.
- Write-once + rename-into-place keeps *local* half-writes out of replay
  order — but a remote replicator may expose a final-name file **before its
  content is stable**, so rename alone is not the guarantee it is on a local
  disk. Rule: a hash/signature mismatch on an inbound spool file is
  **pending — retry until quiescent**, and only a file that stays wrong after
  quiescence dead-letters. Drive conflict-copies are never consumed (they
  fail the filename contract).
- The spool and dead-letter directories are **plaintext message retention**:
  they carry lead-to-lead content and inherit only folder ACLs (on Drive,
  the share's ACLs). Retention policy in §11 applies to them as message
  stores, not as logs.

## 7. Link state on existing rails (no new dashboards)

- **Delivered = cursor — and it means exactly "delivered into the far
  store", never "read".** The peer principal's local cursor is advanced by
  the courier — `agenttalk ack --for <peer> --id <message-id>` — only along
  the highest contiguous authenticated-receipt watermark (§5). So on machine
  A, `agenttalk status` showing `lead-b` at `unread=0` means everything sent
  is durably in B's store with its ledger entry. It does **not** mean the far
  lead has read or handled anything; docs and UI keep that exact wording.
  (The courier is the single consumer of that mailbox — no human waiter or
  drain may share the peer principal, or the mapping lies. It never uses
  `drain`/`recv`: read-only listing plus explicit `ack`, so nothing is
  consumed-but-undisplayed; memory `never-truncate-a-consuming-read`.)
- **Four different facts, kept separate.** Transport reachability, remote
  *courier* liveness, remote *lead* listening, and thread closure — none
  proves another, and the design surfaces each from its own evidence:
  - The courier does **not** write the peer principal's
    `state/<peer>.heartbeat` in phases 0–1. `Store.write_heartbeat`
    semantically means "someone is actively listening here"
    (`store.py:3939-3946`), and `status`/consult read it as `last_seen`; a
    healthy link with a closed lead window would present false liveness. The
    peer principal legitimately shows `health=unknown` — truthful, since no
    local process speaks for it.
  - The **link** has its own identity and state: courier heartbeat, last
    push/ack age, outbox depth, dead-letter count — under `state/link/`,
    reported by `link status` and, in phase 2, `doctor`.
  - The far lead's *genuine* wait heartbeat age crosses inside receipts and
    is surfaced by `link status` with its source timestamp — never re-stamped
    fresher than the far side reported it.
- **Owed = threads/deadman, unchanged.** A `question` to the peer is a
  tracked `OPEN-OUTBOUND` thread exactly as locally; if the link is down long
  enough, `deadman` flags the stale obligation with no link-specific code.
  Note the SLO is already per-store config (`deadman.mail_age_slo_seconds` —
  this laptop's store runs 2700 s against a 900 s code default); a link
  install should set it to honest cross-site expectations.
- **The peer principal is excluded from audience resolution — and until that
  lands, the courier compensates.** `resolve_audience`/`resolve_role_audience`
  exclude only the *sender* (`cli.py:6590,6598`), so a routine
  `broadcast --to all` — or `release --to all` (`cli.py:9036`) — would sweep
  the peer principal in and send session-control traffic at the machine
  boundary as a matter of course. Since dead-letters raise **always-blocking**
  attention items (`attention.py:80`), that would train operators to skim the
  one queue that must stay trustworthy. Rules: (phase 2, primary-team
  surface) the peer principal is excluded from role/group/`all` resolution,
  supervisor scope, and signoff refsets — reachable only by explicit `--to`;
  (phases 0–1, courier policy) only explicitly-addressed point-to-point
  messages relay — fan-out copies (any `broadcast_id`/`audience` meta) are
  ack-skipped with a log line and **no** attention item. After the phase-2
  exclusion exists, an OUT-kind explicitly addressed to the peer is by
  definition deliberate, and *then* it earns the alarm.
- **Supervisor scope is a setup assertion, not a doc note.** The phase-0
  installer checks that the peer principal is absent from `supervisor.json`'s
  managed set and refuses to proceed otherwise — a supervisor that "restarts"
  a processless principal thrashes forever.
- **A dead courier with queued outbound raises an attention item** — the one
  courier-health condition that must interrupt an operator rather than wait
  in `link status`.
- **Optional human ping.** On injection the courier can fire a Windows toast
  (best-effort, off by default) — useful when the receiving lead's window is
  closed and only the human is present. Not load-bearing; the bus signal is.

## 8. What was rejected, and why

| Option | Why not |
| --- | --- |
| **Share one `.agenttalk/` store via Drive/Syncthing** | The store depends on OS file locks, a config lock, single-process-monotonic id minting, and the publication-order ledger. None of these hold across a replicator: locks don't replicate, two minting clocks break id chronology, and the ledger sees foreign files appear unordered — the exact "partial write that reports success relocates the failure onto other processes" trap, at scale. Also deliberately violates team isolation. |
| **Keep raw prose files on Drive (status quo, tidied)** | No receipts, no ordering, no obligations, no wake. Every improvement converges on re-inventing §5 — at which point the envelope/receipt spool *is* this design's `folder` profile. |
| **Git/GitHub via the CODE repo** | The pollution objection stands for the code repo: chat never enters the PR/issue integration point, and `.agenttalk/` is gitignored by policy. A *dedicated* private repo avoids both objections — promoted to the `git` profile in §6; the residual tradeoff is latency (~5–15 s) versus `direct`. |
| **Third-party broker (MQTT, ntfy.sh, Slack, …)** | Message bodies leave the two machines for a third party; new runtime dependency against the stdlib-only, local-first posture; another failure domain. The `direct` profile achieves push latency with stdlib + operator-owned networking. |
| **A second full agent window "impersonating" the remote lead** | An LLM re-typing messages is a lossy, expensive, unaudited courier. The courier must be mechanical — carry bytes, mint nothing, decide nothing (same philosophy as `agenttalk relay`'s mechanical human↔bus boundary). |

## 9. Failure modes, explicitly

| Failure | Behavior |
| --- | --- |
| Peer machine asleep/offline | Outbox spools; retries back off; peer heartbeat goes stale (visibly); deadman flags owed threads if it lasts. Delivery resumes on reconnect, in order, deduped. |
| Courier crashes | Spool + cursors are durable; restart resumes exactly (capture cursor, seq, delivered index all on disk, written atomically). One-live-courier guard follows the supervisor's singleton pattern: a `link/courier.instance.lock` holding `{root, pid, token, started_at, pid_start}` (`store.py:5819` precedent), plus a script-owned state file with a `.backup` sibling (`supervisor-state.json` precedent). |
| Envelope tampered / truncated in transit | Signature + hash chain fail ⇒ pending-until-quiescent, then dead-letter + attention on both sides. |
| Replicator syncs a half-written file | Temp names are outside replay order locally; a remotely-exposed final-name file with unstable content fails its hash and stays **pending** until quiescent — only persistent mismatch dead-letters. Conflict-copies violate the filename contract and are never consumed. |
| Courier crashes after `Store.send()` succeeds, before the delivered-index write | The one window where at-least-once could become twice-visible — and per amendment #15 row 27 a visible duplicate is a **correctness** event. Recovery scans the destination store for the signed `link_origin` tuple before any retry; exactly one injection survives. |
| Envelope arrives with `link_origin` already set in its message meta | Forgery of the courier's own namespace (would silently suppress relay) ⇒ dead-letter, never treated as already-relayed. |
| Peer-chosen `request_id` equals an unrelated local thread's id | Cannot merge: imported ids are namespaced `lnk-<link_id>-…` with a durable reverse map (§5). |
| Forged or replayed receipt | Receipts are signed per-direction and bound to seq/origin/digest/result; the cursor watermark only moves on a verified contiguous prefix — a forged 2xx or replay moves nothing. |
| Remote message carries `meta.needs_operator`/`attention` | Not on the portable meta allowlist ⇒ quarantined under `link_origin.foreign`; it cannot enter the local operator attention queue as an authority claim. |
| Version skew between machines (different KNOWN_KINDS) | Injection rejects ⇒ dead-letter with reason ⇒ sender sees it. The envelope carries the sender's agenttalk version for the error message. |
| Wrong build runs the courier | The courier startup asserts which `agenttalk.store` file it loaded (the `s.__file__` check from memory) and refuses a site-packages/branch mismatch with the agents' build — the ledger incident must not be reproducible here. |
| Both leads message simultaneously | Two independent directions; no shared writer, no conflict. Correlation by `request_id`. |
| Clock skew between sites | Irrelevant to ordering (local ids re-minted; per-direction seq). Skew shows only inside `link_origin.ts` display. |

## 10. Rollout

- **Phase 0 — the MVP *and* the proof vehicle, zero network, zero repo
  surface changes.** A standalone courier (`tools/agenttalk_link.py`, run per
  machine) + `folder` profile pointed at the *existing* Google Drive share.
  Phase 0 is not a stopgap: the folder profile exercises every
  correctness-critical mechanism — envelope format, strict parser, signing,
  seq/hash chain, receipts, dedupe/id map, injection, watermark cursor —
  and phase 1 changes *only the transport*. Proving the adversarial
  obligations (§10a) against real Drive behavior before any listener exists
  is the strongest argument for this sequencing.
  - **Injection calls the pinned public `Store.send()` API in-process** — one
    canonical JSON envelope in, nested typed meta passed as a dict. The CLI
    path cannot carry the contract: `--meta` values are flat strings only
    (`_parse_meta`, `cli.py:159-169`; executed-verified — typed values
    flatten), and generic `send` refuses `kind=rescind` by design
    (`cli.py:1487-1496`). Flattening provenance would be the wrong fix. The
    tool uses no private Store calls and writes nothing under `.agenttalk/`
    directly; at startup it asserts `sys.executable`, the resolved
    `agenttalk.store.__file__`, and the expected build identity, and refuses
    a branch/site-packages mismatch — the ledger incident stays
    unreproducible.
  - **Coexistence:** phase 0 runs *alongside* agentchat; agentchat retires
    only after both leads agree the link has run clean for an agreed window
    (proposal: 7 days with zero unexplained dead-letters), so there are never
    two authoritative channels — one live channel plus one being
    decommissioned, with a named cutover.
- **Phase 1 — `direct` push, routed to the architect as a security-posture
  proposal.** `web.py` hard-refuses non-loopback binds and the codebase
  contains no other network code: the first listener — even in `tools/` — is
  a change to the project's stated posture, and that posture belongs to the
  primary team. Phase 1 therefore ships only after explicit architect
  sign-off, with §6's hardening requirements and codex-sec's Q1 answer
  attached to the proposal. `folder` remains the automatic fallback.
- **Phase 2 — native `agenttalk link`.** Upstream as a proper subsystem:
  `link init/add-peer/status/courier`, a **constrained peer-injection API**
  (making "courier can only speak as the peer" structural instead of policy),
  peer-principal exclusion from audience/supervisor/signoff resolution,
  `doctor` checks, dashboard link panel, and the `link_origin.message_id` ↔
  issue #48 opener-identity convergence. **Ownership note:** all of this
  touches primary-team surface — phase 2 is a proposal (this doc + a GitHub
  issue), not something this team lands unilaterally. Phases 0–1 stay in
  operator-tooling space so the two leads get relief without an ownership
  collision.

## 10a. What phase 0 must prove — executed, not inspected

Every item runs against scratch stores with the exact pinned build and
config; every negative claim gets a positive control first (a probe that
errors prints exactly like a probe that passes). Condensed from the five
review lenses:

1. **Impersonation refusal, by attempting it** — envelopes claiming a real
   local principal and a reserved principal both dead-letter.
2. **The post-`send()`/pre-index crash** — kill the courier in the window,
   restart, assert exactly one injected message (recovery-by-`link_origin`
   scan). The single most important item on this list.
3. **Ledger contiguity on a busy, non-fresh store** — inject into a store
   with existing ledger entries while a live agent sends concurrently;
   contiguity holds and everyone's `send()` keeps working.
4. **Capture consumes nothing** — snapshot every agent's unread count around
   a capture cycle; only the peer's moves, and only via explicit `ack`.
5. **Meta quarantine** — a message carrying `needs_operator`, `attention`, a
   store-scoped authority key, and an *unknown* key crosses with all four
   quarantined; none reaches the local attention queue or authority paths.
6. **Id translation** — a reply and (v2) a rescind round-trip: `in_reply_to`
   / `target_msg_id` resolve to real local ids on both sides; an
   unresolvable reference dead-letters.
7. **Dead-letter and gap paths execute** — malformed body, out-of-allowlist
   kind, unknown protocol version, skipped seq, wrong link/direction/key,
   replay, duplicate, truncation: all fail closed, each raising attention on
   **both** sides exactly once (bounded — no alert storm, no recursion).
8. **Cursor watermark truth under rejection** — a dead-letter at seq N pins
   the peer cursor below N even after N+1 injects.
9. **Build assert fires** — point the courier at site-packages; it refuses.
10. **The real replicator misbehaves on schedule** — CRLF-hostile content,
    conflict-copy, final-name-before-content: hashes verify on the far side,
    conflict-copies are never consumed, pending-until-quiescent works.
11. **Live canary** — one untracked `note` each direction on the live buses
    (origin tuple, injected id, ledger entry, receipt, wake, cursor all
    verified) before any tracked kind is enabled.

## 11. Open questions

Closed during the r1 team round (dispositions on bus thread `b-2efcd7b316bc`):
peer-principal health noise (§7 — the courier never writes the peer heartbeat;
the link carries its own state identity); signing scope (§4 — per-direction
keys in the outside-the-store keys dir); broadcast relay (excluded in v1 for
two now-mechanical reasons: the audience sweep in §7 and cross-store
divergence of broadcast thread derivation, `threads.py:597-607`); composing
(crosses as link state, §4); and the operator-authority exclusion (it lives on
the meta axis, §4 — a kind rule cannot express it).

Still open — the first three belong to the architect:

1. **Transport pick.** Phase 0 is `folder` over the existing Drive share (no
   new infrastructure). The instant path is `direct` over Tailscale (~1–3 s;
   new infra, both operators' consent, posture sign-off) versus `git` over a
   dedicated private repo (~5–15 s; no new accounts). Input needed: are the
   two laptops ever on one LAN, and is Tailscale acceptable?
2. **Opener identity / issue #48 convergence.** `link_origin.message_id` is
   the stable, origin-minted message identity #48 asks for; the link should
   populate whatever #48 lands rather than keep a parallel key. Until then,
   `review-request`/`review-result` stay off the link (§4, §5).
3. **Phase-1 posture sign-off.** The first non-loopback listener changes the
   project's stated security posture; §6's hardening list is the proposal
   attachment (§10).
4. **Retention/GC.** Pruning acked spool entries and receipts requires a
   **signed retained-chain checkpoint** first — without one, a restart after
   pruning cannot distinguish legitimate history from truncation. Dead-letters
   prune manually only. Proposal: checkpoint, then a 30-day rolling window.
5. **agentchat retirement criteria.** Confirm the cutover rule (§10 proposes
   7 days of zero unexplained dead-letters, then both leads retire agentchat).
6. **More than two sites.** Pairwise links, no multi-hop (`link_origin`
   present ⇒ never re-relayed). Fine at 2–3 sites; a mesh/hub design is a
   different document.
7. **Attachment/path conventions.** Bodies referencing local paths don't
   travel. Convention (doc-level, not enforced): cross-site references use
   git SHAs, PR/issue numbers, or repo-relative paths — artifacts both sites
   resolve through the existing git integration point.

## Appendix A — mechanics verified against live stores (2026-07-20)

The full relay loop was exercised with the branch build against two scratch
stores (`init --path <site>` each, roster `lead-a,lead-b` / `lead-b,lead-a`),
nowhere near the live team bus:

1. **Send is unchanged for the lead:** `send --from lead-a --to lead-b
   --kind question --meta request_id=q-link-1` on site A worked as a plain
   local send (peer principal in roster).
2. **Capture is a read-only scan:** the courier-side read of
   `.agenttalk/messages/*.json` picked up the message without touching any
   cursor.
3. **Injection through `send()` on site B** minted a fresh local id
   (`…-oFEI` on A became `…-clgr` on B), preserved `request_id`, carried
   provenance as `link_origin_site`/`link_origin_id` meta, and wrote the
   publication-order ledger entry (`append_sequence: 1`, verified on disk).
4. **The armed wait woke instantly:** `wait --for lead-b --timeout 5` on B
   returned the message immediately with exit 0 — the "automatically told"
   requirement, on stock machinery.
5. **Reply correlates across stores:** `reply --to-request q-link-1` on B,
   relayed back and injected into A, flipped A's thread view for lead-a from
   `[OPEN-OUTBOUND] q-link-1 … peer=lead-b` to the reply arriving on the same
   thread, and lead-a's `wait` woke with the answer. Threads/deadman track
   the cross-machine obligation with zero new code.
6. **Delivery state on existing rails:** `ack --for lead-b --id <origin id>`
   advanced the peer cursor (`status` → `lead-b … unread=0`), and
   `Store.write_heartbeat("lead-b")` (public API) made `last_seen` fresh —
   the §7 mappings work as specified.

One setup footgun found while testing: `init`'s own `--here`/`--path` outrank
the global `--root` (`cli.py:393-400`, documented precedence), so
`agenttalk --root <site> init --here` initializes the *CWD*, not `<site>`.
Harmless on an already-initialized store (no-op without `--force`), but
phase-0 setup instructions must use `init --path <site>`.

**r2 caveat on this appendix's scope.** The exercise above used the CLI with
FLAT provenance keys (`link_origin_site`/`link_origin_id`) because `--meta`
accepts only flat strings (`_parse_meta`, `cli.py:159-169`). It therefore
validated the wake/threads/ledger/ack rails — not the normative nested
`meta.link_origin` schema, which the CLI structurally cannot construct.
Nested-schema injection through `Store.send()` is proof obligation §10a-6 and
is deliberately **not** claimed here.

## Revision history

- **r1** — `bd6bf03` / blob `01562533`: initial design + verified-mechanics
  appendix. Reviewed by the full team and lead, 2026-07-20.
- **r2** — this revision; folds every finding from bus thread
  `b-2efcd7b316bc` and the lead review (`b-4a9143ca62c0`): meta moves to a
  per-kind allowlist with quarantine-by-default and becomes the enforcement
  point for operator authority; kinds shrink to five with default-deny;
  `request_id` namespacing + message-id meta translation; authenticated
  per-direction receipts and the contiguous delivery watermark; the
  delivered-index/amendment-#15 row-27 coupling and crash-window recovery;
  honest heartbeat semantics (four facts kept separate); audience-sweep
  exclusion; strict v1 wire parser; no-cleartext transport floor + listener
  hardening; replicator quiescence rule; `git` transport profile; phase 0
  reframed as the proof vehicle with the §10a executed-proof matrix; phase 1
  gated on architect posture sign-off.
