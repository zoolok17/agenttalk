# Proposal: Making the Team Console Client-Sellable

Audience: product/ops decision-makers, not engineers. No code, no file references.

## 1. Today's console, honestly

The Team Console is a read-only, loopback-only dashboard: it only answers to a
browser on the same machine the tool runs on, only responds to GET requests,
and refuses to render anything an attacker could smuggle in as script. What it
already shows, per project: who the agents are and whether they're alive
(health, last heartbeat, what they're composing), who owns which part of the
codebase, a traffic graph of who's talking to whom, open and closed
conversation threads (including review-request/review-result exchanges,
tagged but not narrated as a story), a ranked "needs a human" queue that
already blends stalled agents, dead letters, and gate blockers with a
severity/confidence score, an onboarding ledger for a codebase analysis pass
(segments inspected, open drift, blocking unknowns), and a lessons-learned
audit trail (what got taught to which agent and when). An optional
action-gated mode lets the one loopback operator answer escalations and chat
with the lead from the browser instead of the CLI.

What it structurally cannot show today: it has no view of lanes or
deliveries (the isolated-worktree assignments and their reject/fix/re-verdict
history) even though that data is recorded on disk; it has no view of gates
or operator waivers beyond a one-line blocker mention buried in the attention
queue — the actual evidence, status, and severity behind "why is this red"
is invisible; it has nothing on close/release records (the GO/HOLD verdict
that aggregates gates, reviews, and sign-offs); and it cannot be reached by
anyone who isn't sitting at the operator's own machine — no client, however
trusted, can just open a link.

## 2. The pitch reframe: two personas, two views

**The operator** (today's only user) needs control: can I unstick this agent,
answer this escalation, see who's composing what right now. That's a live,
interactive, loopback cockpit — keep it exactly that.

**The client's engineering director** (the buyer) needs something completely
different: they are not debugging anything, they don't read code, and they
are not going to SSH-tunnel into anyone's laptop. They need three things a
sales conversation can point to: (1) *proof of progress* — a plateau-by-plateau
view of the migration, not a wall of agent chatter; (2) *proof of rigor* — a
visible wall of gates that stay red until real evidence exists, with waivers
named and dated rather than silently skipped; and (3) *something they can
keep* — a snapshot or export they can put in a status deck or an audit
folder, independent of whether the tool is even running when someone asks
"how did we know this was safe to ship."

The operator view is a cockpit. The client view is a report. They should be
visibly different surfaces built from the same store, not the same screen
with more permissions.

## 3. Ranked feature proposals

Ordered by sellability impact per unit of effort. "Data" = what's already
recorded on disk vs what needs new plumbing to compute or persist.

**1. Gate & Evidence Wall (the "nothing is green without proof" screen).**
Shows every gate by scope (a release, a plateau, a lane) as a card: red,
green, or waived, with the evidence behind a green and the reason/expiry
behind a waiver, in plain language ("tests: green, verified by CI run on
[date]" / "security scan: waived, reason: scanner unavailable, expires
[date]"). This is the single most on-brand, most sellable screen — it's the
tool's actual differentiator made visible. Data: gate status, severity,
evidence, and waivers are already fully recorded on disk today; today's
console only ever surfaces a one-line blocker mention, never the full
picture. New plumbing needed: a real API view and a UI screen; no new data
capture. Size: **M**.

**2. Migration Progress Plateaus.**
A top-level "where are we" bar: plateaus/milestones as steps, each stamped
delivered/in-progress/blocked, backed by what actually shipped (lane
deliveries) and what's still open (gate/onboarding findings) for that
plateau. This is the screen a director opens first. Data: onboarding
segments and lane deliveries both exist; there is no existing concept that
ties a delivery to a "plateau" label, so grouping logic and a small new
aggregation are needed. Size: **L**.

**3. Review-Chain Story ("reject → fix → approved").**
Instead of a raw thread list, render the review lifecycle of a piece of work
as a short narrative: reviewer raised issue X, dev fixed it, re-reviewed,
approved — with dates and named actors. This is proof that scrutiny actually
happened, not just that a checkbox got ticked. Data: the review-request and
review-result messages and their thread linkage already exist; today they
render as tagged chat entries, not a story. New plumbing: a grouping/
narration layer over existing thread data. Size: **M**.

**4. Lane & Delivery Ledger.**
A view of every scoped assignment (lane): its worktree isolation, its
current stage (assigned → checked → delivered → cleaned up), and its
verdict history including any HOLDs it had to clear. Proves the "isolated,
reviewed unit of work" promise concretely. Data: lanes and their delivery
artifacts are fully recorded on disk; there is currently **zero** exposure
of this data anywhere in the web layer — this is the single biggest gap
between what's recorded and what's shown. Size: **M**.

**5. Client Audit Export (static snapshot).**
A one-click export of gates, plateau progress, and the review-chain story as
a static HTML or PDF file, timestamped, that a client can file away
independent of whether the tool or the project is still running. This is
what actually gets attached to an invoice or a compliance folder. Data:
entirely a reuse of #1–#3's data once built; the only new work is a
render-to-static-file path. Size: **M/L** (depends on #1–#3 landing first).

**6. Risk Register from Open Findings.**
Reframe the existing "needs a human" queue as a client-legible risk
register: each open item (stalled agent, dead letter, gate blocker, open
onboarding drift/unknown) shown with severity, age, and owner, sorted the
way a risk log is sorted, not the way an operator triage queue is sorted.
Data: the ranking, severity, and confidence fields already exist and are
already computed; this is close to a relabeling exercise with a different
sort/filter and a client-safe rendering. Size: **S**.

**7. Ownership & Accountability Map.**
Surface the full domain-ownership registry — which agent/team owns which
part of the codebase, and which paths require shared sign-off — as its own
small view, not just a one-line tag buried on each agent card. Answers "who
is accountable for this part of my codebase" directly. Data: fully recorded;
today only a thin per-agent slice is shown. Size: **S**.

**8. Decision & Gotcha Log.**
Surface the general knowledge notes (decisions, gotchas — not just process
lessons) tied to the part of the codebase they apply to, e.g. "why we chose
X here." Gives a client a legible "institutional memory" artifact instead of
having to trust it exists. Data: the note types exist and are already
recorded; today's Learning view filters down to lessons only, so most notes
of this kind are invisible. Size: **S/M**.

**9. Deliverable Cycle Time.**
For each delivered lane: how long from assignment to delivery, how many
review rounds, how many dead-letter/retry events along the way. A rough
proxy for "how much did this cost to get right" without exposing raw
token/dollar spend. Data: timestamps for assignment and delivery exist; dead
letters are already counted; nothing currently rolls this into a per-
deliverable figure. Size: **M**.

**10. Before/After Code Health Snapshot.**
Diff-level metrics (files touched, test coverage delta, lines changed) shown
per plateau, alongside the gate evidence for that plateau. The most visually
persuasive "this migration actually improved things" artifact — and the
weakest-grounded proposal here. Data: **does not exist today** in any
recorded form beyond whatever a coverage percentage a gate's evidence
happens to carry; this needs new evidence collection, not just new
plumbing. Treat as speculative until a gate/evidence convention for code
metrics is agreed. Size: **L**.

**11. Client Notification Digest.**
A periodic (e.g. weekly) plain-language summary — "3 plateaus done, 1 gate
waived and expiring in 5 days, 2 open risks" — sent or exported without the
client needing to visit anything. Turns the console from "pull" to "push."
Data: entirely derivable from #1, #2, and #6 once they exist; the new work
is a summarization/formatting pass and a delivery mechanism (file drop,
email, etc. — mechanism is an open question, see §6). Size: **M**.

**12. Waiver Watch.**
A dedicated small view listing every active operator waiver with its
expiry, so nothing red-turned-green-by-waiver silently outlives its
justification unnoticed by a client. Data: fully recorded already; today
waivers are invisible outside the raw gate file. Size: **S**.

## 4. Quick wins vs strategic bets

**Two-week list** (small, mostly reads existing data, highest visible payoff
per hour): #6 Risk Register relabel, #7 Ownership Map, #12 Waiver Watch, and
the read side of #1 Gate & Evidence Wall (status + evidence text, without
export yet). These alone would take the console from "internal ops tool" to
"here's proof we're rigorous" in a demo.

**Quarter list** (the actual strategic bets): #2 Migration Progress Plateaus
(needs a plateau/milestone concept that doesn't exist yet — a real design
decision, not just plumbing), #4 Lane & Delivery Ledger (biggest data-to-UI
gap in the whole codebase), #3 Review-Chain Story, and #5 Client Audit
Export once #1–#3 exist under it. #10 Before/After Code Health should be
explicitly parked until there's a real evidence convention for code
metrics — building the UI before the data model invites showing numbers
that don't mean what they look like they mean.

## 5. What NOT to build (and why)

- **Do not give the client's engineering director live remote access to a
  running console.** The console's entire security model is "one human at
  their own workstation, loopback only, no opt-in to bind elsewhere." Any
  path that lets a client browser reach a live server — even authenticated,
  even read-only — is a new network-facing attack surface this tool has
  deliberately never had, and it changes the threat model from "protect one
  operator's laptop" to "protect an internet-reachable service." If remote
  client access is wanted, the trade-off must be named and decided by the
  operator explicitly, not solved by quietly loosening the loopback rule.
  The static-export approach (#5) gets most of the sellability value —
  "here's proof, dated and yours to keep" — without that exposure.
- **Do not add client-side write actions.** Actions (answering escalations,
  restarting agents) are already gated behind an explicit opt-in for the one
  trusted operator. A client persona has no legitimate reason to write to
  the store, and adding a write path "for convenience" multiplies the
  authorization surface for no sellability gain — a client wants to see
  proof, not operate the tool.
- **Do not relax the strict Content-Security-Policy or inline-script
  restrictions to make richer client-facing charts easier to build.** Every
  proposal above is achievable with the same self-hosted-script, no-inline,
  textContent-only discipline the console already uses; reaching for a
  chart library that needs eval or inline styles is a security regression
  for a cosmetic gain.
- **Do not fabricate a code-health or cost metric ahead of a real evidence
  source** (see #10, #9's dollar-cost variant). A client-facing number that
  looks authoritative but isn't backed by recorded evidence is worse than no
  number — it's exactly the kind of unverified claim this tool exists to
  refuse to make about itself.
- **Do not build a generic multi-tenant/SaaS version of the console.** The
  buyer is the engineering director of one client at a time using one
  operator's local tool; multi-tenant hosting is a different product with a
  different security and ops burden, not a UI feature.

## 6. Open questions for the operator

1. Is a **plateau/milestone** a concept we're willing to formally define and
   persist (needed for #2 and to make #5's export meaningful), or should
   progress stay expressed only in terms of lanes and gates as-is?
2. For client delivery of the audit export (#5) and digest (#11): is a
   manually-triggered file drop acceptable, or does the sales motion need a
   push mechanism (email, shared folder), which would need its own trust
   review?
3. Should client-facing waivers (#1, #12) show the operator's name/identity,
   or should they be anonymized to "the operator" — some clients may read a
   named individual accepting risk differently than an anonymous role?
4. How far do we want to go on #10 (code health metrics) — is it worth
   defining a real evidence convention for coverage/diff metrics this
   quarter, or is it explicitly out of scope until a client asks for it?
5. Is there appetite to eventually offer a deliberately narrower, harder
   remote-access mode for the client view only (e.g. a signed, time-boxed,
   read-only export server) — or is static export the permanent answer to
   "the client isn't at the operator's desk"?

---

## 7. Reconciliation with the third-party review (2026-08-25)

An independent reviewer assessed the live console (all seven views, desktop
and narrow-laptop) against the same question. The two reviews converge on
every structural call and the external one sharpens ours:

**Where we agree** — keep the Team Console as the engineering/operations
view and build a program layer above it (their words: "a cosmetic redesign
alone will not make the present UI compelling"); separate personas over the
same evidence (we said two, they say three — Executive / Delivery leader /
Engineer — theirs is better); the Gate & Evidence Wall generalizes into
their readiness-and-assurance matrix (every green = evidence + independent
verifier + expiry; missing = HOLD); static signed briefings first, remote
portal only as a later, explicitly-decided trade-off — both reviews reached
this independently, which settles open question 5.

**What they add that ours missed:**
- An evidence-backed *stage vocabulary* (assessment-complete → plan-approved
  → reimplementation-complete → parity-verified → acceptance-ready →
  cutover-ready → legacy-decommissioned). This answers our open question 1:
  the plateau concept should be formalized as these stages — they exist
  precisely to prevent "complete but not cutover-ready" overstatement.
- An executive scorecard with named KPIs, and a Decision Center upgrade of
  the Human Queue (plain-language decision, options, impact, owner,
  deadline, expiring risk acceptance).
- Concrete, verified mechanical defects in today's console (header
  mission-pill overflow, untrustworthy relative timestamps, default scope
  showing retired agents, endpoint latency vs the 2-second poll, narrow-
  layout priority) — filed as an immediate fix bundle.
- The best near-term showcase: a customer-facing static dashboard built
  from the Papendal pilot — a real migration that is reimplemented and
  parity-verified but honestly NOT cutover-ready, with clickable evidence.
  That demo is the differentiator stated exactly: the system proves what
  was migrated, surfaces what remains unknown, and stops a confident team
  declaring victory early.

**Where we hold our ground** — nothing in the external review contradicts
the "what NOT to build" list; it independently endorses the loopback
posture and static-first stakeholder delivery.

**Disposition:** mechanical fixes = task #207 (immediate, small); the merged
program layer = task #208 (quarter-scale, built together with the #55
assessment/comprehension plane, which is its data source).
