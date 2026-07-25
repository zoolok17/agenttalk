# RFC: identity, authorization, epochs, and retired agents

Status: draft
Date: 2026-06-05
Related issues: #19, #9, #12, #18

## Summary

agenttalk should keep its current local, file-backed, governance-free
transport model. The next trust work should add machine-checkable
identity and policy surfaces only where they improve safety without
pretending the bus has become a remote secure workflow engine.

Recommended 0.16.0 scope:

1. Add an identity registry in `config.json` that separates active agents
   from retired identities. Retired names are tombstones: they remain
   valid for historical reads and thread derivation, can never be
   re-bound to a new active agent, and new sends to them fail loudly with
   a rename hint. Optional forwarding is explicit, temporary, single-hop,
   and transcript visible.
2. Add a generic global epoch/barrier primitive and extend `check` so
   high-risk actions can check whether the request they are about to act
   on is current with respect to both thread-local rescinds and the latest
   surviving validated global barrier message. This is trusted-team
   correctness, not protection against a writer who suppresses a barrier.
3. Formalize read-only `next_owner` / `next_action` fields in
   `threads --json` and `sync --json`. These should describe the next
   bus action implied by the current thread state; they must not become
   arbitrary workflow state.
4. Make the current authorization limits explicit: roles, groups, and
   `operator_facing` remain routing metadata until a real per-agent
   signing model exists.

Recommended later scope:

1. Add an authorization policy bound to identity keys and policy epochs.
2. Add key rotation and revocation semantics that preserve historical
   validation.
3. Add replay/deletion hardening with a hash-chain or checkpoint anchor
   outside attacker-writable `.agenttalk/`, if the threat model requires
   it. Without that anchor, no signature scheme proves that every
   relevant message is still present.

Important constraint: stdlib-only Python does not provide an asymmetric
signature primitive. HMAC can authenticate bytes to holders of a shared
secret, but it cannot prove "agent A, not agent B" if both agents can
read the verification secret. Under the current no-runtime-dependencies
constraint, agenttalk must not claim strong per-agent cryptographic
identity among mutually untrusted local participants. It can still make
identity history, rename safety, epoch barriers, and policy decisions
machine-checkable for a trusted local team.

## Current Model

agenttalk is a local JSON-file message bus:

- Messages live under `.agenttalk/messages/<id>.json`.
- `config.json` contains the current roster plus routing metadata such
  as roles, groups, and `operator_facing`.
- `Message.validate(roster)` rejects unknown kinds and messages whose
  sender or recipient is not in the current roster.
- `Store.valid_messages()` feeds all thread derivation, so invalid or
  unsigned messages cannot close threads or create obligations.
- Optional HMAC signing uses one project key derived from the project
  root path and stored outside `.agenttalk/`.
- `rescind` plus `check --to-request` is the 0.14.0 thread-local
  currentness gate.
- 0.15.0 role audiences, `--na`, broadcast batch accounting, and
  quarantine are additive display and recovery surfaces. They do not
  change the trust boundary.

The existing security statement is correct: if a participant can write
to `.agenttalk/` and can act as the same OS user that owns the key store,
the bus cannot distinguish malicious peers from honest peers. It is a
trusted local team tool unless stronger identity is deliberately added.

## Goals

- Preserve historical message validation when an agent name leaves the
  active roster.
- Let operators rename or retire identities without stranding old
  mailboxes or invalidating old threads.
- Give high-risk actions a generic, executable barrier stronger than
  "latest warning is somewhere in the inbox".
- Define what real authorization could mean for group routing,
  operator-facing decisions, and lead-only actions.
- Keep the transport generic: agenttalk should expose primitives and
  validated facts, while skills and project governance decide what those
  facts mean operationally.
- Keep 0.14.0 and 0.15.0 stores readable. New fields must be additive.

## Non-Goals

- Do not rewrite historical messages, cursors, threadstate, transcripts,
  HMAC signatures, or request ids during rename or retirement.
- Do not implement a workflow engine. No arbitrary task graph, approval
  lattice, sprint state, or project-specific governance in the bus.
- Do not enforce what a human can type into a worker window. The bus can
  validate messages and warn; it cannot control external UI behavior.
- Do not silently forward messages from old identities to new ones.
- Do not claim malicious-peer security from shared-secret HMAC.
- Do not add a runtime dependency only to get signatures unless the
  project explicitly relaxes the stdlib-only constraint.
- Do not use message body prose as authorization input.

## Threat Model

### In Scope Today

The current bus can defend against:

- malformed JSON and unsafe agent names;
- messages from names outside the accepted identity set;
- unknown message kinds;
- forged messages from a local writer who cannot read the per-user HMAC
  key file, when project HMAC is enabled;
- stale thread-local requests, when operators use `check --to-request`
  before irreversible actions.

### Not In Scope Today

The current bus does not defend against:

- a same-OS-user peer that can read the HMAC key;
- a rostered participant intentionally writing misleading valid
  messages;
- deletion, replay, or reordering of signed old files by a writer with
  project-directory access;
- suppression of a valid barrier, revocation, or policy-change message by
  a writer who can delete, quarantine, or withhold files before another
  agent reads them;
- config tampering beyond what load-time validation catches;
- a human bypassing the designated operator-facing channel.

### New Boundary Proposed By This RFC

The first new trust boundary should be explicit:

- 0.16.0 improves correctness for trusted teams. It makes rename,
  retirement, epochs, and next actions machine-checkable, but still
  treats active roster members as trusted writers.
- The 0.16.0 barrier gate catches honest stale-action crossings and
  forgotten re-checks. It fails open if a project writer suppresses the
  barrier message itself; that is intentionally deferred to the
  replay/deletion hardening phase.
- A later authz release may restrict which identities can perform
  actions such as barrier bumps, operator answers, roster changes, and
  group sends.
- Strong per-agent cryptographic attribution is blocked by the
  stdlib-only constraint unless agenttalk accepts either an external
  signer interface or an optional asymmetric-crypto dependency.

## Identity Registry

The active roster should stop being the only source of valid historical
names. Put the registry in `.agenttalk/config.json` with the rest of the
roster surface, but treat it as no more trustworthy than that roster. It
is a trusted-team compatibility and safety feature until a later release
anchors identity claims to per-agent keys and an outside-the-project
policy boundary.

Add a registry alongside the existing config fields:

```json
{
  "agents": ["claude-lead", "codex-rev"],
  "identity_registry": {
    "version": 1,
    "identities": {
      "claude": {
        "status": "retired",
        "retired_at": "2026-06-05T00:00:00Z",
        "replaced_by": "claude-lead",
        "send_hint": "Use claude-lead",
        "forward_to": null
      },
      "claude-lead": {
        "status": "active",
        "created_at": "2026-06-05T00:00:00Z"
      }
    }
  }
}
```

Rules:

- `agents` remains the active roster for new sends, waits, roles, groups,
  and operator-facing configuration.
- `identity_registry.identities` is the historical identity set.
- A message is historically valid when `from` and `to` are either active
  or retired identities.
- A new send is valid only when `from` and `to` are active identities.
- Retired identities are permanent tombstones. A retired name must never
  be re-created as a different active identity, even if the replacement
  identity later disappears.
- Retired names may appear in old messages, old thread derivation, old
  cursors, and transcripts.
- `roster remove <name>` should refuse when messages exist for that name
  unless the operator passes through the retirement flow. A `--force`
  override may preserve today's pruning behavior, but it must print that
  historical reads can become invalid unless a tombstone is written.

A retired entry does not prove that old messages really came from that
physical agent. Because `config.json` is writable by local project
writers, a hostile writer can add a retired name and forged historical
messages from it. The registry therefore preserves history for trusted
teams; it is not a malicious-peer identity boundary.

This fixes the current safe-rename failure mode: removing an old name no
longer makes historical messages invalid, but sending to the old name
still hard-fails.

### Rename And Retirement Commands

Recommended CLI:

```powershell
agenttalk roster retire <old> --replaced-by <new> --drain-check
agenttalk roster rename <old> <new> --drain-check
agenttalk roster forward-retired <old> --to <new> --until <timestamp>
agenttalk roster retired
```

`--drain-check` should inspect derived thread state before retirement:

- owed inbound for old identity;
- reply-waiting for old identity;
- open outbound from old identity;
- stale operator escalations involving old identity;
- incomplete fan-out batches involving old identity.

The command should not mutate messages or state files. It should either
refuse with a concrete drain plan, or retire the identity and write only
config/registry state.

Forwarding is opt-in and should be narrow:

- default is hard-fail: `send --to old` exits 2 and prints the replacement;
- forwarding must have an expiration or explicit clear command;
- forwarding must be single-hop and resolve to an active terminal target;
- forwarding must refuse when the target is itself retired or forwarded;
- forwarded delivery should write a new ordinary message to the new
  active identity with meta such as `forwarded_from=old`;
- forwarding must never rewrite the original target in historical files;
- forwarding should be discouraged for review requests, proposals, and
  escalations, where a human-visible identity change is safer than
  silent delivery.

## Signing And Canonicalization

Current HMAC v1 signs the message dict with `meta.signature` removed and
all other fields included, serialized with sorted keys and compact JSON.
That exact exclusion is for project-key HMAC v1 only and should remain
stable for that format.

Future identity/authz metadata should not overload the v1 signature keys.
Use an additive auth envelope:

```json
{
  "meta": {
    "auth": {
      "version": "v2",
      "identity": "codex-rev",
      "key_id": "codex-rev:2026-06-05:1",
      "policy_epoch": "p-000012",
      "signature_alg": "external-ed25519",
      "signature": "..."
    }
  }
}
```

Canonicalization requirements:

- Sign the immutable message fields: `id`, `ts`, `from`, `to`, `kind`,
  `subject`, `body`, and all meta except the v2 signature bytes at the
  nested path `meta.auth.signature`.
- Sign identity id, key id, policy epoch, and the declared algorithm as
  data, but do not choose verification code from the message's own
  `signature_alg`.
- The verifier must choose the required algorithm and key policy from the
  anchored auth policy at the claimed `policy_epoch`. If the signed
  `signature_alg` disagrees with that policy, reject the message. This
  avoids v1/v2 coexistence becoming an algorithm-downgrade path.
- Treat `signed_at` as diagnostic only unless an epoch policy explicitly
  defines a freshness window. Wall-clock freshness alone is fragile.
- Keep body prose signed as bytes but never interpret it as policy.
- Do not sign derived data such as thread state, pending lists, or
  rendered next-action hints.

If the stdlib-only constraint remains absolute, the auth envelope can
exist as a schema placeholder, but core agenttalk should not ship an
Ed25519 dependency. A later optional signer adapter could provide
asymmetric signatures without changing message layout.

## Key Lifecycle

Key state should be modeled explicitly even before strong per-agent
signatures land:

- `active`: accepted for new messages.
- `retiring`: accepted for historical verification and possibly for a
  bounded overlap window.
- `revoked`: not accepted for new messages after the revocation barrier;
  historical messages before the revocation barrier remain valid.
- `lost`: cannot sign new messages; historical verification depends on
  archived public verification material or retained HMAC policy.

Rotation rules:

- Rotation creates a new key version for the same identity.
- The old key remains valid for messages before the rotation barrier.
- New messages after the rotation barrier must use the new key.
- Revocation is not retroactive unless the operator explicitly marks a
  key as compromised and accepts that historical messages from that key
  become suspect.

Compromised-agent rules:

- A compromised active identity can send valid messages until revoked.
- Revocation should be a barrier event so `check` can report requests
  sent under now-revoked authority as stale or suspect.
- Revocation inherits the barrier limitations below. In the trusted-team
  phase, a writer who can suppress the revocation barrier can make local
  checks keep treating surviving earlier messages as current.
- Do not delete or rewrite the compromised agent's messages. Quarantine
  is for invalid files, not for valid but no-longer-trusted history.
- Recovery is: bump barrier, revoke key, optionally retire identity,
  create a new identity/key, and re-ask any high-risk open requests.

## Unsigned Legacy Handling

Compatibility is mandatory. A 0.14.0 or 0.15.0 store must keep working.

Recommended policy:

- Stores without auth policy continue to behave exactly as they do today.
- When auth policy is enabled, it declares an `enforce_after_id` or an
  epoch boundary that resolves to a barrier message id, not a separate
  mutable counter.
- Messages before that boundary are `legacy_unsigned`.
- Legacy messages remain available for transcript export and historical
  thread derivation.
- New messages after the boundary must satisfy the current auth policy.
- Open high-risk threads that span the boundary should be re-asked with a
  fresh request id and the current barrier message id.

The enforcement boundary cannot live only in attacker-writable
`.agenttalk/config.json`. Config may mirror the policy for inspection, but
the verifier must anchor the accepted boundary outside the project tree,
for example in the same per-user directory as the HMAC key material. If a
writer can move `enforce_after_id` forward in project config, signed
messages can be downgraded back to `legacy_unsigned`.

This avoids mass-invalidating history, avoids making migration a data
rewrite, and avoids repeating the old "security boundary stored only in
project config" failure mode.

## Global Epochs And Send-Time Barriers

0.14.0 solved thread-local stale action with `rescind` and `check`. It
does not solve a global crossing such as "a HOLD was broadcast after the
fire request, but the executor already drained the fire request".

Add a generic epoch primitive:

```powershell
agenttalk barrier bump --from <agent> --scope global -m "<reason>"
agenttalk check --for <agent> --to-request <rid> --epoch
```

Data model:

- A barrier event is a meta-marked ordinary message. Use `kind=message`
  with structured meta such as
  `meta.barrier={"version":1,"scope":"global","type":"epoch-bump"}` so
  old clients see a normal note rather than an invalid new kind.
- The global epoch id is the message id of the latest validated global
  barrier event. There is no separate integer counter.
- "Latest" means latest in the bus's deterministic message-id order. That
  order is stable for readers, but it is not real-time consensus:
  cross-process ids depend on each process clock plus the random suffix.
- Tracked opener messages must automatically carry
  `meta.epoch_at_send=<latest-barrier-message-id>`, or `null` when no
  barrier exists yet.
- `check --to-request RID --epoch` verifies both:
  - no valid requester rescind supersedes the request;
  - the request's `epoch_at_send` is still the latest surviving validated
    global barrier message id by message-id order.

Trusted-team 0.16.0 semantics:

- Any active roster member may bump the global epoch.
- This is a deliberate global-stall lever. A looping or careless active
  agent can stale every high-risk request; that is acceptable only under
  the current rostered-equals-trusted model.
- Barrier bodies are audit prose only.
- `check` is the last executable safety point before an irreversible
  action. Passive warnings are useful but not sufficient.
- `check` is not atomic with the action that follows. A barrier can land
  after `check` and before the action, so skill contracts should run it
  immediately before acting and treat the residual race like the 0.14.0
  thread-local rescind race.
- Old requests without `epoch_at_send` are checkable only for
  thread-local rescind; for irreversible actions they should be treated
  as requiring a re-ask under the current barrier message id.
- A writer who deletes, quarantines, or withholds a barrier makes
  `check --epoch` read the latest surviving barrier and potentially pass.
  HMAC proves message bytes, not presence. Therefore Phase A barriers are
  trusted-team correctness checks, not malicious-peer controls.

Authz-era semantics:

- Barrier bump authority becomes policy-driven.
- Common policy: operator-facing identity and designated lead identities
  may bump global epoch; ordinary workers may request a bump by
  escalating.
- Barrier events must be signed by an identity authorized at the policy
  epoch they claim.
- Rotation and revocation barriers are subject to the same suppression
  limits until Phase D anchors message presence outside `.agenttalk/`.

Costs:

- Every high-risk workflow must include `check --epoch` immediately
  before action. A barrier primitive without skill contracts is only a
  log marker.
- A global barrier is intentionally coarse. It will stale unrelated
  requests. That is acceptable for safety-critical transitions, but it
  should not be used for normal chatter.
- Multiple barrier scopes may be tempting, but they risk recreating a
  workflow engine. Start with `global` only.

## Authorization Policy

Roles and groups are currently routing metadata. They may become inputs
to authorization only when policy is bound to identity and policy epoch.

Policy should be generic and small:

```json
{
  "policy_epoch": "p-000004",
  "operator_facing": "claude-lead",
  "permissions": {
    "barrier.bump": ["claude-lead"],
    "operator.answer": ["claude-lead"],
    "roster.admin": ["claude-lead"],
    "group.admin": ["claude-lead"],
    "broadcast.to_role": ["claude-lead", "codex-lead"]
  }
}
```

Authorization checks should apply to generic bus actions:

- sending as an identity;
- sending to a role or group;
- creating or changing roster/group/operator-facing policy;
- bumping barriers;
- marking an operator answer;
- retiring or forwarding identities;
- rescinding a request.

Do not encode project-specific semantics such as "approve WP", "release",
"trade", or "deploy". Those remain skill/governance conventions that call
generic checks before acting.

Policy storage can remain mirrored in `config.json` for one visible
surface, but the enforcing boundary must be anchored outside
`.agenttalk/`. The verifier should load the accepted policy epoch from
that anchor, then use that policy to choose the required signature
algorithm and authorized identities. It must not let a message or edited
project config choose a weaker verification mode.

### Operator-Facing With Real Authz

Current `operator_facing` can only mean "route escalations here and warn
if missing". With authz, it can additionally mean:

- only the operator-facing identity may send messages marked
  `operator_answer=true`;
- `escalate` must target the current operator-facing identity unless an
  authorized override is provided;
- worker-originated messages that claim to be operator answers are
  invalid after auth enforcement;
- `sync` can distinguish "operator answer received from liaison" from
  "ordinary reply received".

It still cannot mean:

- the human never saw another window;
- the human did not type into a worker's prompt;
- the liaison is semantically correct;
- a project action is approved unless the relevant skill/governance
  contract says an operator answer is sufficient.

## Retired Identities And Safe Rename

Safe rename should be implemented as retirement plus optional creation,
not as a rewrite.

Example:

```powershell
agenttalk roster rename claude claude-lead --drain-check
```

Effects:

- `claude-lead` becomes active if not already active.
- `claude` becomes retired with `replaced_by=claude-lead`.
- `claude` is a tombstone and cannot later be re-created as a new active
  identity.
- Old messages from or to `claude` remain valid.
- `wait --for claude` should exit 2 with a retired-identity hint unless a
  specific historical inspect command is used.
- `send --to claude` exits 2 with "claude is retired; use claude-lead".
- `sync --for claude`, `threads --for claude`, and
  `drain --for claude` keep working so the operator can inspect and clear
  old obligations. `drain` may write only the retired identity's own
  cursor/threadstate files.
- Existing cursor and threadstate files for `claude` are left in place.
- Thread derivation for historical messages still works because retired
  identities are accepted for read validation.

If there are outstanding obligations, `--drain-check` should refuse and
print commands such as:

```powershell
agenttalk sync --for claude
agenttalk threads --for claude
agenttalk drain --for claude
```

The operator can then accept-as-old and re-announce-as-new, which is the
safe manual pattern from issue #9.

## Tool-Visible Next Owner And Next Action

The current `threads` model already has a "ball owner" internally. Expose
that as a stable read-only schema:

```json
{
  "request_id": "abc",
  "state": "owed-inbound",
  "next_owner": "codex-rev",
  "next_action": "reply",
  "next_action_reason": "question awaiting a response from codex-rev"
}
```

Rules:

- `next_owner` is an agent name or a list of pending agents for
  broadcasts.
- For broadcasts, `next_owner` is a projection of the existing
  copy-based derivation. Obligations derive from the delivered copies,
  never from fan-out metadata on the original broadcast.
- `next_action` is from a small vocabulary: `reply`, `review`, `answer`,
  `drain-reply`, `wait`, `check-before-action`, `none`.
- The field is derived from validated messages and threadstate only.
- It is advisory for tools, not a new obligation source.
- It must not include project-specific action plans.
- It should be omitted or set to `none` for closed and superseded
  threads.

This helps leads and external automation see who owns the next bus move
without making agenttalk own the work package, review, or release logic.

## Replay, Deletion, And Reordering

Existing HMAC proves that a message's bytes match a key. It does not
prove that all messages are present or in their original order.

This matters for barriers: if a writer suppresses a HOLD, revocation, or
policy-change barrier, `check --epoch` can only evaluate the surviving
validated log and can fail open. A later presence anchor is the first
phase that can make suppression detectable.

Potential later hardening:

- Add a hash chain over validated message ids and canonical payload
  hashes.
- Periodically checkpoint the chain head outside `.agenttalk/`, for
  example in the same per-user config directory as the key material.
- Surface `status` warnings when the chain is broken, missing, or forks.

This should not be 0.16.0. It has real operational cost and only matters
after the project decides it needs protection from deletion/replay by a
writer who can mutate `.agenttalk/`.

## Migration And Compatibility

Migration must be read-first and additive:

1. Existing stores without `identity_registry` load as today.
2. On first 0.16.0 write, the registry can be materialized with all
   current roster agents marked active.
3. Historical validation uses active roster plus retired identities only
   when the registry exists; otherwise it uses the current roster as
   today.
4. Auth policy has an explicit enforcement boundary. No old message is
   rewritten to add signatures or epochs.
5. New JSON fields are ignored by old clients as ordinary config/meta
   data where possible.
6. If a new message kind is unavoidable, document that old clients will
   report it INVALID and must be upgraded before using that feature.

For Phase A barriers, use meta-marked ordinary messages. A new barrier
kind should be a later deliberate upgrade gate, not the default.

## Recommended Phases

### Phase A: 0.16.0 Trusted-Team Safety

Implement:

- identity registry in `config.json` with active identities and retired
  tombstones that cannot be re-bound;
- `roster retire`, `roster rename --drain-check`, and retired send
  refusal;
- `roster remove` refusal with a clear retirement hint and a force
  override for operators who knowingly accept historical-read breakage;
- optional, explicit, single-hop retired forwarding with
  transcript-visible meta;
- meta-marked ordinary global barrier events whose epoch id is the
  barrier message id;
- tracked opener messages automatically carrying `epoch_at_send`;
- `check --epoch` against the surviving validated barrier log;
- `threads --json` and `sync --json` next-owner / next-action fields;
- docs and security updates stating that this is still trusted-team
  safety, not malicious-peer authz or deletion suppression defense.

Do not implement:

- per-agent cryptographic identity;
- policy permissions beyond trusted-team barrier/retirement rules;
- hash-chain replay defense;
- a new message kind for barriers.

### Phase B: Authz Policy Design Lock

Before implementation, decide one of:

- keep stdlib-only and accept that per-agent crypto is not strong
  against mutually untrusted local peers;
- introduce an optional external signer interface;
- relax stdlib-only and add a maintained asymmetric signature
  dependency.

Then lock:

- auth envelope schema;
- policy epoch schema;
- exact canonicalization, including exclusion of only
  `meta.auth.signature` for v2;
- policy-selected verification algorithm, never message-selected
  algorithm;
- key lifecycle semantics;
- unsigned legacy boundary anchored outside `.agenttalk/`;
- revocation behavior.

### Phase C: Real Authz

Implement only after Phase B:

- signed identity-bound messages;
- policy-bound action checks;
- operator-answer authorization;
- barrier-bump authorization;
- roster/group admin authorization;
- key rotation/revocation commands.

### Phase D: Replay/Deletion Hardening

Implement only if production evidence or deployment model requires it:

- message hash chain;
- external checkpoint storage;
- fork/deletion diagnostics.

## Acceptance Criteria For The RFC

This RFC is ready to drive implementation when reviewers agree that it
now explicitly specifies:

- Phase A uses `config.json` for the trusted-team identity registry;
- retired identities are non-rebindable tombstones;
- Phase A barriers are meta-marked ordinary messages;
- global epoch ids are barrier message ids, ordered by message id rather
  than real time;
- tracked request openers automatically record `epoch_at_send`;
- `check --epoch` is trusted-team correctness and fails open against
  barrier suppression until Phase D;
- pre-authz barrier bumps are allowed by any active roster member, with
  the resulting global-stall risk documented;
- `operator_facing` remains advisory until real authz is policy-bound;
- stdlib-only core cannot claim strong per-agent crypto against mutually
  untrusted local peers.

After that, the safest immediate implementation is Phase A as scoped
above, with tests and skill-contract updates for every command that asks
agents to call `check --epoch` before acting.
