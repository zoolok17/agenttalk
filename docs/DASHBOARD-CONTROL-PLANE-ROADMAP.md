# Dashboard Control Plane — Roadmap & Design

**Status:** shipped history through v0.69.0. This document is retained as the
design history for the dashboard control plane; current contracts live in
`docs/DESIGN.md`, `docs/USER-MANUAL.md`, and the dated addendum in
`docs/DashboardDesign/BUILD-SPEC.md`.

**Goal (operator, 2026-07-03):** let a *non-developer* drive everything the CLI does from the browser — send/answer messages, manage the roster, start/stop/restart agents, run gates, onboard a team — without touching a terminal.

**Shipped baseline:** v0.59.0 added the action-gated intent-queue write spine
behind `--enable-actions`; v0.68.0 added dashboard lead-chat; v0.69.0 aligned
dashboard liveness render for fresh-heartbeat unwrapped agents.

**Planned v0.74.0 supersession (2026-07-11):** multi-root Team Console state
uses stable path-derived `project_id` as its canonical identity, not a root-list
index. Selected-root responses carry `root_info {project_id,label,path}`. GET
may omit root 0 or use a unique legacy label; multi-root POST requires one exact
full project id, never a label or omission. Invalid, blank, repeated, or
ambiguous selectors return HTTP 400 `bad_root` before mutation. The project
label and full path remain visible in every view, and root switches clear bound
state and discard stale/mismatched responses. Selected-root reads and actions
still pass through the existing loopback/session/CSRF/intent authority boundaries.
`project_id` is routing, not authentication. This paragraph supersedes root
selection details only; the phased intent-queue history below is unchanged.

**Historical baseline:** v0.58.3 was the read-only Team Console: loopback-only,
GET/HEAD-only, `/api/state` carries no body, split CSP, `/api/thread` the only
body-bearing route.

**Source:** design workflow `w5dr2t76t` (11 agents: full CLI inventory + 3 judged architectures + deep security model + non-dev UX + phased synthesis + adversarial critique). Full output archived at the task output file.

---

## 1. Architecture decision — Intent Queue (fail-closed executor boundary)

Three architectures were scored (security / non-dev UX / impl-simplicity / fit / testability):

| Arch | one-liner | total |
|---|---|---|
| **A — Thin action-API on the loopback server** | POST endpoints in web.py behind `--enable-actions` + CSRF; web tier calls `store.send()` directly | 23 |
| B — Separate authenticated control daemon | distinct privileged service owns writes/lifecycle | 17 |
| **C — Intent Queue (WINNER)** | browser only *enqueues* typed intents; a supervised executor is the sole actor and re-resolves authority | 23 |

**Winner: Architecture C.** A and C tied on score; C wins on the deciding principle (fail-closed, auditable, invariant-preserving). The load-bearing property:

> The web tier can **only append a typed intent envelope** (`store.write_intent`, a rename+generalization of the already-proven `write_launch_request`: `is_safe_id`-gated, atomic under `_config_lock`; queued/claimed intents are reset-**cleared** (they reference current-session state — firing a stale queued send into a fresh session is wrong), while only the terminal control-audit under a new top-level `.agenttalk/control-audit/` is reset-**preserved**). The **executor is the authorization boundary** — it re-resolves authority server-side and treats the browser's self-asserted `origin=web-console` / `human_authorized` as an *auditable assertion, not proof*. So the blast radius of a full web/XSS compromise is strictly **less** than what the CLI operator already has, and it generalizes the same re-verify-server-side discipline that ended the lane-shared-approval bug class.

**Grafted from A:** per-run in-memory secret minted in the `_make_handler(roots)` closure (never on disk); the drive-by-localhost defense trio (double-submit CSRF token + strict Origin/Referer + required `Content-Type: application/json`) enforced *after* `_check_peer_or_403` and *before* any write; a Host-header allowlist (web.py parses no Host today — genuinely new); re-scope (don't gut) `test_no_mutation_full_tree_hash` into GET/HEAD-identical + no-token-POST-mutates-nothing + valid-POST-appends-exactly-one; route every response through `_send` for uniform CSP/nosniff headers.
**Grafted from B:** reuse the `Assert-ActionsEnabled` brake as the master off-switch that also halts intent-drain; an append-only control-audit JSONL (origin + session + Origin/Host + UTC + verb + args-hash + decision — never bodies/secrets).

---

## 2. Security model

**Recommended model:** *loopback + per-run token + same-origin proof + tiered confirmation, additive and OFF by default.*

- `--enable-actions` **OFF by default**: `do_POST` stays 405, no token endpoint exists → shipped read-only invariants literally unchanged.
- When ON: mint a cryptographically-random **per-run secret** held in the server instance, **never written to the store**, delivered same-origin via `GET /api/session` (loopback + valid-Host peer) read into a **JS closure** (never the DOM; `script-src 'self'` unchanged). A cross-origin page cannot read it (no CORS headers emitted).
- **One** action route `POST /api/intent`, dispatched in order: `_check_peer_or_403` (FIRST, unchanged) → Host allowlist → Origin==self → `Content-Type: application/json` + custom `X-CSRF-Token` header → constant-time token match → **kill-switch check** (423 if `supervisor.kill` present; fail-closed 423 if unreadable) → bounded-body parse → **aggregate rate/cap check** → JSON parse → schema/kind allowlist → `store.write_intent`. Every check returns before any store write (403, or 423 for the kill-switch).
- **Two tiers.** Tier-1 messaging (send/reply/propose/broadcast/escalate/relay/rescind/ack/drain): token stack. Tier-2 process-control + governance (release *human-relayed only, never emergency*, request-restart *no force-protected in v1*, request-launch, gate set/waive, close publish, lane approve-shared, reset archive-default): token stack **plus** a server-minted, single-use, verb+args-bound confirmation-nonce **plus** an operator-typed verbatim reason.
- **Honest limit (ship in SECURITY.md):** the token defeats drive-by CSRF, DNS-rebinding, and cross-origin theft, and raises the bar for a co-resident process — but it does **not** defend a same-OS-user attacker, and `human_authorized` stays an auditable assertion the executor re-resolves regardless.

**Top threats (all grounded in file:line):** drive-by localhost CSRF (critical); loopback≠auth / co-resident process (high); DNS-rebinding to 127.0.0.1 (high); XSS on /dashboard steals token or fires actions (high); forged release/stand-down wedges the liaison (high); mass/force-protected restart & reset (high).

---

## 3. Phased roadmap

v0.59.1 hardening note: active intent JSON is a co-resident-writable trust
boundary, so the executor revalidates every frozen plan at drain time before
reconciliation or send. The plan must still match current payload/store
semantics exactly: `reply` re-resolves its request anchor now, and `broadcast`
re-resolves the audience now, including recipient order. If a reply anchor
disappears or a roster/group/role broadcast audience drifts, the intent is
denied with `code=plan_revalidation_failed` (visible on `GET /api/intents`) and
the operator requeues a fresh intent; v0.59.1 does not add a sealed-plan
mechanism.

> Phase order below is the original workflow synthesis. It is now historical:
> the write spine shipped in v0.59.0, lead-chat shipped in v0.68.0, and the
> dashboard liveness render shipped in v0.69.0. Current architecture lives in
> `docs/DESIGN.md` §4.8 and ADR D-18.

- **v0.59.0 — Intent-queue write spine + first bus-write verbs.** `store.write_intent` + `list_intents`/`read_intent`; `--enable-actions` flag; per-run secret; `POST /api/intent` (the full defense trio); `GET /api/session`; `GET /api/intents` (honest queued/claimed/applied/denied state); the **executor drain** (see §4 #1 — this is real new work); Tier-1 kinds **send/reply/propose/broadcast** only (fail-closed on unknown); flip the already-rendered composer live. Read-transcript already works.
- **v0.60.0 — Answer-escalation + the operator inbox goes actionable** (highest daily non-dev value). escalate / relay-operator-answer / attention dispositions; `--from` forced through the server-side liaison/sole-lead resolver; plain-language layer over the queue.
- **v0.61.0 — Reversible hygiene + roster onboarding.** ack/thread-close, mark-read, roster add/retire/set-role/set-operator-facing, dead-letter requeue/resolve. `remove --force` structurally absent from the browser.
- **v0.62.0 — Governance READ + tiered governance WRITE.** Release-readiness light (GO/HOLD + plain blockers) broadly; gate set/waive, close publish, lane approve-shared behind a separated "Operator controls" block with nonce + typed reason; approve-shared re-runs the all-matching-must-approve resolver server-side.
- **v0.63.0 — Lifecycle control.** request-restart (no force-protected), request-launch, release (`--relay-human` only), end, `supervisor.kill` (the safest big button — disables automation). Executor writes the same atomic markers the CLI writes and **never spawns/kills** — the supervisor .ps1 stays the sole Start/Stop-Process. Buttons bound to preconditions (supervisor running? liaison configured?).
- **v0.64.0 — Non-dev onboarding wizard + one-click bootstrap.** Step-0 bootstrap (`agenttalk start`): init-if-absent, start web server, open browser, start supervisor. Read-only doctor preflight checklist; name-team; install-skills; **locate agent programs** (auto-detect + file-picker → fully-substituted supervisor.json, no `REPLACE_` tokens); codex call-back toggle; start-the-team with live health transitions.

---

## 4. Critique — must-fix before/within v0.59.0

The adversarial critic (verdict: sound wall + right risk order, but three blockers) surfaced:

1. **[HIGH — blocker] The executor host does not exist as described.** The supervisor is a PowerShell do/while loop that shells out to *short-lived* Python per tick (`supervise --plan` / `--record-launch`); there is **no resident Python process** to "add a drain step" to. The drain must be a **new `agenttalk supervise --drain-intents` subcommand** wired into `PS_TEMPLATE` and re-scaffolded by `supervise --init`, invoked once per tick under `_config_lock`, with claimed→act→applied idempotency + crash-mid-drain semantics specified. Without a *decided and built* executor, every write verb is inert. This is v0.59.0 work, not a later choice.
2. **[HIGH] Rejected-intent audit write contradicts the no-mutation regression.** Writing an audit line on a rejected 403 POST mutates the tree, contradicting "invalid-CSRF POST mutates nothing." Resolve explicitly: either carve the audit log out of the tree hash (with its own tamper/rotation model) **or** don't persist rejected-intent audit on disk. Do not leave it ambiguous.
3. **[HIGH] Setup/onboarding is phased last but is the actual entry barrier.** A non-dev can't reach the console to use v0.59.0–v0.63.0 without an initialized store + installed skills + resolved supervisor.json + a *running* supervisor. At minimum a **Step-0 bootstrap + read-only doctor preflight belongs in/before v0.59.0.**

Additional real items to fold:
- The intent queue is a **co-resident-writable trust boundary**: anyone who can write `state/intents/` bypasses every web-side control. State plainly that web controls are anti-CSRF/anti-rebinding only and the **executor's server-side authority re-resolution is the sole boundary** for a store-writing co-resident; a dropped intent claiming `origin=web-console` is unverifiable.
- Confirmation-nonce must be **server-minted, single-use (enforced server-side), and bound to verb+args-hash** — else it adds nothing over the token against XSS and is replayable.
- **Token invalidation on server/supervisor restart** needs a client recovery path (403 → re-fetch `/api/session` → retry) or a non-dev sees "Send stopped working" with no diagnosis.
- **No queue/audit rotation or aggregate rate-bound** in v0.59.0 — send/broadcast is the cheap-to-flood surface; add a cap/rate-bound, not just the per-request body cap.
- **Send/reply `--from` authority resolver is needed in v0.59.0**, not v0.60.0 — otherwise the "pinned as liaison" promise has undefined behavior when no liaison is configured (dependency inversion with v0.61.0's set-operator-facing).
- Surface the **forge-release-the-liaison + can't-force-restart-from-browser deadlock** as a residual risk with a recovery path.
- The **"queued but no executor" honesty** must cover the daily send/reply verbs (a timeout/staleness escalation), not just lifecycle buttons.

---

## 5. Historical open decisions

These were open in the design pass. The shipped implementation chose the
intent-queue executor path, kept actions off by default, and added the later
lead-chat and liveness increments without turning the dashboard into a remote
API.

1. **Phasing / audience** (the strategic fork): power-user-first (as ordered) vs non-dev-first (wizard early) vs **hybrid — write spine + minimal Step-0 bootstrap + read-only doctor preflight together in v0.59.0** (critique's recommendation).
2. **Executor host:** fold the drain into the supervisor .ps1 loop as a new `--drain-intents` subcommand (recommended) vs `wrap --loop` resident host vs a dedicated control-daemon (Arch B — second lifecycle a non-dev must manage).
3. **reset from the browser:** archive-default behind the nonce (v0.63.0) vs CLI-only permanently (safer default).
4. **request-restart `--force-protected`:** disabled in v1 (recommended) vs behind a distinct louder confirm vs never.
5. **Confirmation-nonce ceremony weight** for Tier-2: re-type a shown phrase vs full typed reason, per verb class.
6. **codex-config `--enable`** (sandbox-loosening) from the wizard: guided one-toggle step (with off switch) vs terminal-only permanently. Genuine security-vs-reach fork; required for a codex team to call back.
