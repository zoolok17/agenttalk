# Feature Specification: 0.14.0 Operator Safety

**Mission**: `operator-safety-0140-01KTBZA1`
**Created**: 2026-06-05
**Status**: Draft
**Source**: GitHub issues #12, #13, #18, #14; ROADMAP.md Phase 2; the 2026-06-05 four-agent production retro; joint Claude/Codex design consults (agenttalk threads 535a091f, 2293cabd) and cross-review (52476b64).

## Overview

agenttalk is a file-backed message bus that lets multiple AI coding agents
coordinate on one machine. A four-agent production deployment surfaced three
operator-safety gaps: (1) a cancelled high-stakes request and its execution
message crossed mid-flight, producing a voided run, because nothing can mark
a request as no-longer-current; (2) two terminal windows silently talked to
two different message stores after a second `init`, stalling work for 30
minutes; (3) the human operator had no designated, tool-supported single
point of contact — workers asked their own windows' humans, conventions
decayed across restarts, and the operator was prompted in duplicate.

This release closes those three gaps and, capacity permitting, reduces
crossing messages by making "a reply is being drafted" visible.

## User Scenarios & Testing

### Primary actors

- **Operator**: the human running a team of agents (the project maintainer
  in the canonical deployment).
- **Liaison**: the single agent the operator talks to directly (typically
  also the team lead).
- **Worker agent**: any rostered agent that is not the liaison.
- **Requester / Executor**: agents on the two ends of a tracked request
  thread.

### Scenario 1 — rescinding a request before it is acted on (#12)

1. The liaison sends worker E a tracked request: "fire the launch".
2. New information arrives; the liaison rescinds that request with a reason.
3. E, blocked waiting on that thread, wakes immediately with a distinct
   "request rescinded" outcome instead of a reply — it does not act.
4. The thread shows as closed-superseded for both parties; rejoin digests
   flag it; the rescind and its reason are permanently visible in the
   transcript.

**Acceptance:** a waiter on a rescinded request wakes with the rescinded
outcome (not a timeout, not a normal reply); the thread state is terminal;
the rescind message is auditable in the message log.

### Scenario 2 — pre-action currentness check (#12)

1. Worker E drained its inbox earlier and is about to execute the "fire"
   request it read minutes ago.
2. Per the documented contract, E runs the currentness check on that
   request id immediately before the irreversible action.
3. The check reports `superseded` (a rescind arrived after the opener); E
   aborts and reports back instead of firing.
4. On a live request the check reports `current`, and E proceeds.

**Acceptance:** the check command returns machine-readable
current/superseded status with distinct exit codes, suitable for use as a
gate in agent skills; a stale/unknown request id is distinguishable from a
current one.

### Scenario 3 — second init cannot silently fork the store (#13)

1. The team's store lives at the project root. A new agent window starts in
   a subdirectory and runs `init` by mistake.
2. `init` refuses, naming the existing store it found up-tree; the window
   joins the existing store instead. (With an explicit force flag, a
   nested store can still be deliberately created.)
3. In an already-forked layout (two stores from the past), `doctor` names
   every store along the path from the working directory to the drive
   root, so the split-brain is visible in one command.
4. An operator can pin the root for a whole shell via an environment
   variable instead of repeating `--root` on every call.

**Acceptance:** nested `init` without force exits non-zero with a message
naming the up-tree store; `doctor` lists multiple stores when present;
the environment variable selects the root with documented precedence
(flag > environment > upward walk); `whoami`/`doctor` print the resolved
root as their first line.

### Scenario 4 — operator liaison and escalation (#18)

1. The operator designates one agent as operator-facing (the liaison).
2. A worker hits a decision only the operator can make. Instead of asking
   the human at its own window, it runs the escalate command with its
   question.
3. The escalation resolves to the liaison automatically, is correlated as
   a tracked thread, and appears in the liaison's digest under an
   "operator input needed" bucket.
4. The liaison surfaces the question to the operator with context, gets
   the answer, and replies on the same thread; the escalation shows as
   answered for both sides.
5. If zero or multiple agents are marked operator-facing, the escalate
   command refuses with a clear error (unless the sender explicitly
   overrides the target), and diagnostics warn about the misconfiguration.

**Acceptance:** escalation reaches the liaison with a correlation id the
worker can wait on; the liaison's digest shows pending escalations
distinctly; an ambiguous liaison set makes escalation fail loudly, not
silently; an answered escalation stops showing as pending.

### Scenario 5 — reply-in-flight visibility (#14, conditional)

1. Agent A asks B a tracked question and waits on the thread.
2. B starts drafting and marks composing on that thread; A's wait extends
   instead of timing out (existing behavior) **and** A's thread/digest
   views show "reply in flight" (new), so a rejoining or impatient A does
   not fire a duplicate or crossing message.
3. Stale-thread warnings are suppressed for threads with a live
   reply-in-flight marker.

**Acceptance:** thread and digest views distinguish "awaiting reply" from
"reply being drafted"; stale warnings do not fire while drafting is live.

### Edge cases

- Rescind arriving after the executor already consumed and acted: the
  check-before-irreversible-action contract is the only mitigation; the
  thread still records the rescind for the post-hoc audit trail.
- Rescind from an agent other than the requester: only a rescind from the
  thread's requester (the agent that opened the request) supersedes it.
- Rescind of an unknown/foreign request id: rejected at send time.
- Multiple rescinds: idempotent; first valid one decides the state.
- Rescind of an already-closed thread: allowed but the thread stays in its
  terminal state; the message remains for audit.
- Escalation while no liaison is configured: refused with remediation
  hint; worker may fall back to asking its own operator (documented).
- Liaison escalating to itself: refused (it already owns the operator
  channel).
- Composing marker lingering after a crash: markers are observational and
  time-bounded; staleness rules mirror the existing heartbeat/waiting
  conventions.
- Pre-0.14.0 agents on the same bus: all new metadata is ignored by older
  readers; no existing message, cursor, or state file changes meaning.

## Requirements

### Functional Requirements

| ID | Requirement | Issue | Status |
|----|-------------|-------|--------|
| FR-001 | A requester can rescind a previously sent tracked request by request id (optionally pinning a specific message id), with an optional reason; the rescind is a first-class, transcript-visible message. | #12 | Proposed |
| FR-002 | A thread with a valid rescind newer than its opener (or the pinned message) reports a terminal "closed-superseded" state in thread listings for all participants. | #12 | Proposed |
| FR-003 | A scoped wait on a rescinded request wakes promptly with a distinct rescinded outcome, distinguishable from reply, timeout, and interrupt. | #12 | Proposed |
| FR-004 | Rejoin digests and thread listings flag rescinded threads so a restarted agent cannot act on them unknowingly. | #12 | Proposed |
| FR-005 | A check command reports current/superseded for a given request id with distinct, documented exit codes usable as an execution gate. | #12 | Proposed |
| FR-006 | `init` refuses to create a store when another store exists at or above the target directory, names that store, and proceeds only with an explicit force flag. | #13 | Proposed |
| FR-007 | `doctor` detects and names every store on the path from the working directory to the filesystem root, flagging multi-store layouts. | #13 | Proposed |
| FR-008 | The bus root can be pinned via an environment variable with documented precedence: explicit flag > environment variable > upward directory walk. | #13 | Proposed |
| FR-009 | `whoami` and `doctor` print the resolved root as their first output line. | #13 | Proposed |
| FR-010 | The roster supports designating an agent as operator-facing; the designation is visible in roster, identity, and digest output. | #18 | Proposed |
| FR-011 | Diagnostics warn when zero or more than one agent is operator-facing. | #18 | Proposed |
| FR-012 | An escalate command routes an operator-input request to the operator-facing agent, auto-resolving the target, correlating the thread, and printing the correlation id. | #18 | Proposed |
| FR-013 | The escalate command refuses with a non-zero exit and remediation hint when the operator-facing designation is absent or ambiguous, unless the sender explicitly overrides the target. | #18 | Proposed |
| FR-014 | The liaison's digest and thread views present pending operator escalations as a distinct bucket; an escalation is pending until the liaison sends a correlated non-control reply to the requester, after which it is answered. | #18 | Proposed |
| FR-015 | The escalation requester sees its own escalation as open until answered. | #18 | Proposed |
| FR-016 | A drafting agent can mark composing against a specific request id with a single command argument (no hand-built metadata). | #14 | Proposed (slip candidate) |
| FR-017 | Thread and digest views show "reply in flight" for threads with a live composing marker, and stale-thread warnings are suppressed for them. | #14 | Proposed (slip candidate) |

### Non-Functional Requirements

| ID | Requirement | Threshold | Status |
|----|-------------|-----------|--------|
| NFR-001 | Backward compatibility: existing stores, messages, cursors, and state files remain valid and unchanged in meaning; pre-0.14.0 readers ignore all new metadata without error. | 100% of existing tests keep passing unmodified (except where they assert absence of new output, adjusted explicitly) | Proposed |
| NFR-002 | A waiter on a rescinded thread wakes within one poll interval of the rescind landing. | ≤ 2 seconds on a local store | Proposed |
| NFR-003 | The check command is fast enough to gate every irreversible action. | ≤ 1 second on a store of 10,000 messages | Proposed |
| NFR-004 | All new failure modes fail loudly: non-zero exit + actionable stderr message; no new silent-failure path. | Every new error path has a test asserting exit code and message | Proposed |
| NFR-005 | Documentation: every new command/flag appears in README CLI table, --help, and the bundled skills for both agent CLIs. | Skill-lint test suite passes; README table row per new verb | Proposed |

### Constraints

| ID | Constraint | Status |
|----|-----------|--------|
| C-001 | Stdlib-only runtime; no new third-party dependencies. | Standing |
| C-002 | Message history is immutable: no rewriting, editing, or deleting of existing messages; rescission is expressed by new messages only. | Standing |
| C-003 | No new hidden control kinds: the rescind is auditable content (it affects thread state and must be transcript-visible); composing remains the only control kind. | Agreed (consult 535a091f) |
| C-004 | No new load-bearing state: reply-in-flight and similar markers are observational (heartbeat/waiting pattern); thread state remains derivable from valid messages (+ existing threadstate); FR-016/017 must not introduce a new thread-state model — if they do, they slip to 0.14.x. | Agreed (consult 2293cabd) |
| C-005 | Exit-code contract preserved: 0 ok, 1 wait-timeout, 2 usage/refusal, 130 SIGINT; new outcomes get documented codes without repurposing existing ones. | Standing |
| C-006 | Transport stays governance-free: no HOLD/VOID/approval semantics in the bus; skills map team conventions onto generic primitives. | Agreed (band consensus + consult) |
| C-007 | Liaison/operator-facing is advisory routing metadata: it must not change message validity, ordinary thread closure, or authorization; enforcement questions belong to the identity/authz RFC (#19). | Agreed |
| C-008 | Windows-first ergonomics: all new commands accept `--file -` bodies where bodies exist; docs use here-string examples; no new flag requires shell-hostile quoting. | Standing |
| C-009 | Every work package is cross-reviewed by Codex over agenttalk before it is merged. | Process |
| C-010 | Scope priority order: #12 → #13 → #18 → #14; #14 is the only slip candidate and slips before any other scope is cut. | Agreed |

## Success Criteria

1. A scripted two-agent run in which a request is rescinded between send
   and execution ends with the executor aborting (via wake-on-rescind or
   the check gate) in 100% of attempts — the HOLD/fire crossing class is
   closed.
2. A scripted nested-`init` attempt under an existing store fails loudly;
   zero ways remain to create a second store on the same path without an
   explicit force flag.
3. A scripted three-agent team (liaison + two workers) routes 100% of
   worker operator-questions through the liaison; the operator answers in
   exactly one window; misconfigured liaison sets produce loud refusals,
   never silent drops.
4. The full pre-existing pytest suite passes unmodified (NFR-001), and new
   features carry tests for every FR and every error path (NFR-004).
5. The 4-agent production band can upgrade mid-project with zero changes
   to their existing store, conventions, or transcripts.

## Key Entities

- **Rescind message**: a transcript-visible message correlated to an
  existing request id; carries optional pinned message id and reason.
- **Closed-superseded thread state**: a terminal thread state derived when
  a valid rescind postdates the opener/pinned message.
- **Operator-facing designation**: roster metadata marking the liaison;
  expected cardinality exactly one.
- **Escalation thread**: a tracked request from worker to liaison carrying
  operator-input-needed metadata; pending until liaison's correlated reply.
- **Reply-in-flight marker**: an observational, time-bounded record that a
  counterparty is composing on a specific request id.
- **Resolved root**: the store directory selected by flag > environment >
  upward walk; printed first by identity/diagnostic commands.

## Assumptions

- The canonical deployment remains a fully-trusted local team; per-agent
  cryptographic identity is out of scope (RFC #19).
- The liaison is normally also the team lead; separate lead/liaison
  topologies are deferred (documented limitation, revisit with #19).
- "Operator" cardinality is one human per team for v1.
- Rescind authority: requester-only is sufficient for v1 (lead-overrides
  and third-party rescission deferred to the RFC).
- The band's remaining wishlist items (#15–#17) are explicitly out of
  scope for this release.

## Out of Scope

- Global epochs / send-time ordering barriers (#19 RFC).
- Role-scoped audiences, `reply --na`, broadcast manifest, quarantine
  (#15, #16, #17 — Phase 2b).
- Safe rename / retired identities (#9, folds into #19).
- Reply-all (#11, deferred).
- Any enforcement of what a human sees or types in any window.
