# Lead Link: direct lead↔lead messaging across machines

Status: DRAFT for discussion — not normative, not scheduled. Written 2026-07-20.
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
exchange prose messages through a shared Google Drive folder. That channel is:

- **Slow.** Drive sync is polling-based and takes seconds-to-minutes per hop;
  each exchange costs a human-noticeable wait.
- **Unreliable, silently.** There is no delivery receipt, no ordering guarantee,
  no conflict handling, and no alarm when a file never syncs. A lost message
  looks identical to a peer who hasn't answered yet — the same
  "idle vs. structurally unable to speak" ambiguity we already hit locally
  (see lesson in memory `cli-build-must-match-agent-build`).
- **Outside the bus.** Drive messages don't appear in `threads`, don't create
  owed-inbound obligations, don't trip `deadman`, and don't wake a waiting
  lead. Every notification is a human noticing a file.

Requirement: the two leads talk **directly, without waiting**, and the
receiving lead is **automatically told** when a message arrives. A file-based
mechanism is acceptable. Teams stay local; only leads cross the machine
boundary.

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
3. **Transport profile** — how spool entries cross the wire. Two profiles,
   one envelope format (§6):
   - `direct` (recommended): HTTP push between the two couriers over a private
     network (Tailscale or LAN). Sub-second, synchronous ack.
   - `folder` (fallback): a replicated directory (Syncthing preferred; the
     existing Google Drive share works as a degraded mode). Single writer per
     direction; receipts sync back the other way.
4. **Link state made visible on existing rails.** Delivery/ack state maps onto
   the peer principal's cursor; link liveness maps onto its heartbeat; failures
   raise operator `attention`/`escalate` items — so `status`, `threads`,
   `deadman`, and the dashboard report the link with no new views.

## 4. Identity, trust, and what a peer may say

- The envelope's sender must equal the configured peer principal. The courier
  **only ever injects `sender == <peer principal>`** — a compromised or buggy
  peer cannot speak as any local agent, the operator, or a reserved principal
  (`_avatars.RESERVED_PRINCIPALS` are rejected as peer names outright, and the
  courier never passes `_allow_reserved_sender`).
- v1 recipients: the local lead only (configurable allowlist later). Teams stay
  local by construction, matching the stated intent.
- **Meta hygiene / no cross-store authority.** Store-scoped meta keys —
  `epoch_at_send`, `roster_revision`, `authorized_liaisons`,
  `origin_request_id`/`origin_inbound_id` (the authority-sensitive path in
  `Store.send`, store.py:2657-2741) — are *moved* under `meta.link_origin.*`
  before injection. A relayed message can carry them as provenance but can
  never assert local authority with them. Portable keys (`request_id`,
  `broadcast_id`, review/proposal meta) pass through unchanged so threads
  correlate on both sides.
- **Kinds.** Injection goes through `send()`, so `KNOWN_KINDS`
  (`store.py:431`) is enforced. The link additionally applies its own
  allowlist: the coordination set (`message`, `note`, `question`, `proposal`,
  `proposal-response`, `review-request`, `review-result`, `wake`, and
  `rescind` — a lead may withdraw their own cross-site request; it correlates
  by `request_id`). Session/loop-control kinds `end`, `release`, and
  `composing` never cross the link — a remote peer must not be able to
  terminate, stand down, or throttle a local session.
- **Content trust is unchanged.** Locally, agenttalk's stance (SECURITY.md,
  `Message.validate` docstring, `docs/rfc-identity-authz.md`) is a
  trusted-team local bus: schema validation is not a defense against a writer
  of well-formed messages, and the roster is explicitly not a security
  boundary. The link keeps that stance honest: transport auth (§6) proves
  *which machine* wrote the envelope; it does not make the words true.
  Peer-lead messages remain prompt-injection surface and get the same
  skepticism as any bus message.
- **Envelope signing rides the existing signing model.** `signing.py` already
  does opt-in HMAC-SHA256 over a whole message dict (`canonical_payload`:
  sorted-key compact JSON minus `meta.signature`), with a symmetric per-project
  key stored *outside* the attacker-writable store
  (`%LOCALAPPDATA%\agenttalk\keys\`, XDG on POSIX) — exactly the
  "anchor enforcement outside `.agenttalk/`" rule the identity RFC sets for
  any future boundary. The link reuses the pattern with a **per-link key**
  (minted at `link init`, exchanged once out-of-band, stored in the same keys
  dir): HMAC over the canonical envelope. Being symmetric it proves "one of
  the two link ends" rather than which one — acceptable for a two-party link
  where the `direction` field plus transport auth disambiguate; per-site
  asymmetric identity stays deferred with the RFC's future phases.

## 5. Delivery semantics

- **At-least-once transport + idempotent injection = effectively once.** Every
  envelope carries the origin store's message id; the courier keeps a delivered
  index and injecting is a no-op for an already-seen origin id.
- **Per-direction sequence + hash chain.** Envelopes are numbered `seq = 1,2,…`
  per direction and carry `prev_sha256` of the previous envelope. The receiver
  detects gaps and requests replay from the spool; tampering or truncation is
  evident. This is the monotonic-causal-primitive pattern the codebase already
  prescribes over timestamps.
- **Fresh local identity at injection.** `send()` mints the local id/ts —
  cross-machine clock skew cannot corrupt local ordering. The origin id/ts
  live in `meta.link_origin` for display and correlation.
- **Receipts.** The receiving courier acks `{ack_seq, origin_id,
  injected_id, injected_ts, result}` after the injected message is durably in
  the store *with its ledger entry*. On the sender side, a receipt advances
  the visible delivery state (§7).
- **Dead-letter, never silent drop.** An envelope that fails validation
  (version-skewed kind, malformed body, allowlist violation) is written to
  `dead-letter/`, receipted as `result=dead-letter, reason=…`, and raised as an
  operator attention item **on both sides**. The sending lead is told their
  message did not deliver — the exact failure Drive hides today.
- **Ordering scope.** Order is guaranteed per direction (seq). No global order
  across the two directions is claimed; replies correlate by `request_id`, not
  by arrival order — same as the local bus.

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
Unknown envelope fields are ignored (forward compatibility); an unknown
`link_protocol` dead-letters.

### Profile `direct` (recommended)

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
  receipt. Idempotent by origin id. A bearer token from the link config
  (generated at `link init`, exchanged once out-of-band, stored in the keys
  dir outside `.agenttalk/`) authenticates both directions, compared with
  `hmac.compare_digest` (the dashboard's CSRF-token precedent,
  `web.py:3215`); the tailnet/LAN boundary is defense-in-depth around it. TLS
  is delegated to the overlay (Tailscale encrypts; on bare LAN the token is
  the only secret — acceptable for a home LAN, called out in config as a
  choice).
- Sender behavior: spool first (durable), then push immediately; on failure,
  retry with exponential backoff (1 s → 60 s cap) until acked. Push-on-send
  means **sub-second transport when both machines are up**; the spool is the
  queue when they aren't.
- Latency budget end-to-end: local capture ≤1 s (courier tail poll) + push
  ~10–100 ms + injection + receiver's `wait` poll ≤2 s ⇒ **lead-to-lead
  typically 1–3 s.**

### Profile `folder` (fallback; also the phase-0 bootstrap)

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
- Write-once + rename-into-place also sidesteps replicator partial-file reads:
  a temp name is not in replay order, so a half-synced file is never consumed.

## 7. Link state on existing rails (no new dashboards)

- **Delivered = cursor.** The peer principal's local cursor is advanced by
  the courier — `agenttalk ack --for <peer> --id <message-id>` — only after a
  receipt confirms durable injection on the far side. So on machine A,
  `agenttalk status` shows `lead-b` caught up (`unread=0`) ⇔ everything sent
  has been delivered *into B's store*. Undelivered backlog is visible as
  lead-b's unread count. (The courier is the single consumer of that mailbox;
  the one-live-consumer-per-mailbox rule is respected. It never uses
  `drain`/`recv` — read-only listing plus explicit `ack`, so nothing is
  consumed-but-undisplayed; memory `never-truncate-a-consuming-read`.)
- **Alive = heartbeat.** `state/<agent>.heartbeat` is normally written only by
  `agenttalk wait` (`store.py:3940`), so a principal with no local process
  reads as `last_seen` stale / `health=unknown` — which is exactly what a
  *down link* should look like. While the link is up (recent successful push
  or poll round-trip), the courier refreshes the peer's heartbeat via the
  public `Store.write_heartbeat`; link down ⇒ heartbeat goes stale ⇒ anything
  that checks peer freshness (consult skill's ~5-min check, `status`,
  supervisor reports) truthfully shows the peer unreachable.
- **Owed = threads/deadman, unchanged.** A `question` to the peer is a
  tracked `OPEN-OUTBOUND` thread exactly as locally; if the link is down long
  enough, `deadman` flags the stale obligation with no link-specific code.
  Note the SLO is already per-store config (`deadman.mail_age_slo_seconds` —
  this laptop's store runs 2700 s against a 900 s code default); a link
  install should set it to honest cross-site expectations.
- **Supervisor scope caveat.** The peer principal must be *excluded* from the
  local supervisor's managed set (`supervisor.json` is operator-authored) —
  otherwise the supervisor would try to "restart" an agent that has no local
  process. The courier itself, not the peer identity, is the supervisable
  thing.
- **Courier health.** The courier writes its own heartbeat under
  `.agenttalk/state/`; `link status` (and later `doctor`) reports: peer
  reachable, last push/ack age, outbox depth, dead-letter count. A dead
  courier with queued outbound raises an attention item.
- **Optional human ping.** On injection the courier can fire a Windows toast
  (best-effort, off by default) — useful when the receiving lead's window is
  closed and only the human is present. Not load-bearing; the bus signal is.

## 8. What was rejected, and why

| Option | Why not |
| --- | --- |
| **Share one `.agenttalk/` store via Drive/Syncthing** | The store depends on OS file locks, a config lock, single-process-monotonic id minting, and the publication-order ledger. None of these hold across a replicator: locks don't replicate, two minting clocks break id chronology, and the ledger sees foreign files appear unordered — the exact "partial write that reports success relocates the failure onto other processes" trap, at scale. Also deliberately violates team isolation. |
| **Keep raw prose files on Drive (status quo, tidied)** | No receipts, no ordering, no obligations, no wake. Every improvement converges on re-inventing §5 — at which point the envelope/receipt spool *is* this design's `folder` profile. |
| **Git/GitHub as the message transport** | Authenticated and offline-tolerant, but: polling latency (or webhook infra = a listener anyway), chat traffic pollutes the repo/PR integration point the teams deliberately keep clean, and `.agenttalk/` is gitignored by policy. Kept for what it's good at: code, PRs, issues. |
| **Third-party broker (MQTT, ntfy.sh, Slack, …)** | Message bodies leave the two machines for a third party; new runtime dependency against the stdlib-only, local-first posture; another failure domain. The `direct` profile achieves push latency with stdlib + operator-owned networking. |
| **A second full agent window "impersonating" the remote lead** | An LLM re-typing messages is a lossy, expensive, unaudited courier. The courier must be mechanical — carry bytes, mint nothing, decide nothing (same philosophy as `agenttalk relay`'s mechanical human↔bus boundary). |

## 9. Failure modes, explicitly

| Failure | Behavior |
| --- | --- |
| Peer machine asleep/offline | Outbox spools; retries back off; peer heartbeat goes stale (visibly); deadman flags owed threads if it lasts. Delivery resumes on reconnect, in order, deduped. |
| Courier crashes | Spool + cursors are durable; restart resumes exactly (capture cursor, seq, delivered index all on disk, written atomically). One-live-courier guard follows the supervisor's singleton pattern: a `link/courier.instance.lock` holding `{root, pid, token, started_at, pid_start}` (`store.py:5819` precedent), plus a script-owned state file with a `.backup` sibling (`supervisor-state.json` precedent). |
| Envelope tampered / truncated in transit | sha256 + hash chain fail ⇒ dead-letter + attention on both sides. |
| Replicator syncs a half-written file | Impossible to consume: temp names are outside replay order; rename is atomic per file; write-once thereafter. |
| Version skew between machines (different KNOWN_KINDS) | Injection rejects ⇒ dead-letter with reason ⇒ sender sees it. The envelope carries the sender's agenttalk version for the error message. |
| Wrong build runs the courier | The courier startup asserts which `agenttalk.store` file it loaded (the `s.__file__` check from memory) and refuses a site-packages/branch mismatch with the agents' build — the ledger incident must not be reproducible here. |
| Both leads message simultaneously | Two independent directions; no shared writer, no conflict. Correlation by `request_id`. |
| Clock skew between sites | Irrelevant to ordering (local ids re-minted; per-direction seq). Skew shows only inside `link_origin.ts` display. |

## 10. Rollout

- **Phase 0 — this week, zero repo changes.** A standalone courier
  (`tools/agenttalk_link.py`, run per machine) + `folder` profile pointed at
  the *existing* Google Drive share. Injection shells out to the
  memory-blessed invocation (`PYTHONPATH=<branch src>; python -m agenttalk
  --root <root> send …`) so the ledger is always written by the same build the
  agents run. Immediate wins: automatic wake, receipts, dedupe, ordering,
  loss alarms — Drive keeps only the job it can do (moving bytes eventually).
- **Phase 1 — direct profile.** Install Tailscale on both laptops (or use the
  home LAN), enable `direct` push, keep `folder` as automatic fallback when
  the peer is unreachable. Lead-to-lead latency drops to ~1–3 s.
- **Phase 2 — native `agenttalk link`.** Upstream the courier as a proper
  subsystem: `link init/add-peer/status/courier`, store-API injection instead
  of subprocess, `doctor` checks, dashboard link panel, envelope signing via
  the existing signing infrastructure. **Ownership note:** this touches
  `cli.py`/`store`-adjacent surface owned by the primary team — phase 2 is a
  proposal routed to the primary architect (GitHub issue + this doc), not
  something this team lands unilaterally. Phases 0–1 stay entirely within
  operator-tooling space (`tools/`, no owned-module edits) precisely so the
  two leads get relief now without an ownership collision.

## 11. Open questions

Two earlier open questions were closed by inspection: peer-principal health
noise (§7 — the never-reading-agent effects are cursor-keyed deadman alarms,
absent heartbeat, `health=unknown`, and all three become *truthful link
telemetry* under courier ack + heartbeat, with the supervisor-scope caveat) and
signing scope (§4 — per-link HMAC key in the existing outside-the-store keys
dir, reusing `signing.py`'s canonical-payload pattern).

Still open:

1. **Broadcast across the link.** v1 relays point-to-point only. Should a
   lead's broadcast to `all` include the peer principal copy (it would relay
   naturally as a per-recipient copy), or be excluded until cross-site
   broadcast semantics are thought through? Default: excluded via allowlist.
2. **More than two sites.** The envelope is pairwise (`link_id`); N leads =
   N·(N−1)/2 links with no multi-hop (`link_origin` present ⇒ never re-relay,
   so no loops by construction). Fine at 2–3 sites; a mesh/hub design is a
   different document.
3. **Attachment/path conventions.** Bodies referencing local paths don't
   travel. Convention (doc-level, not enforced): cross-site references use git
   SHAs, PR/issue numbers, or repo-relative paths — the artifacts both sites
   can resolve through the existing git integration point.
4. **Retention/GC.** When acked spool entries, receipts, and dead-letters are
   pruned (proposal: keep everything for a rolling 30 days, then prune acked;
   dead-letters only ever pruned manually).

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
