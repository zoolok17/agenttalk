# DESIGN: a default, required lead loop

**In plain words:** Today a lead has two helpers that do not know about each other. One is a written routine (check the bus, check the seats, check the work, write a handover note) that the lead is told to run every 30 minutes, with a timer the lead sets up and has to remember to renew. The other is the managed lead loop, a program that owns the lead's mailbox and wakes the lead on a short cycle, but only for three kinds of trouble. This design joins them without adding a new timer or a second reader of the mailbox. When a result arrives, such as a review verdict, the lead has to pass the decision to each seat that is waiting for it. Whether that happened is worked out from the messages themselves, so it cannot be lost with a file, and the hand-off is sent once even if two commands race or the machine stops half way. If the lead forgets, the existing cycle reminds again and escalates, and it never claims more than it can keep: the design lists every case in which no delivery time can be promised. A status screen shows what really ran and when, and never says "healthy" because a timer exists. Starting a managed lead turns the routine on unless the team switched it off on purpose; that choice survives a forced re-initialisation. Installing the skills alone starts nothing.

Status: design, revision 3 (fix round 2 after the final read `tk-004385a54646` of revision 2 at `8567dec4b93b7276fc2c203ec4c25c9a6d526e22`: no P1, five P2, three P3, all eight answered in section 12). **There is no further design read: every finding below is a binding requirement for the build, written as a contract the builder can test and the build's own code read checks.** No code in this round. Base: `origin/master` at `bdc775cb5727970b960d6ee76c785b5652c1f0dd`. Branch `design/lead-loop-default`. Author: claude-agenttalk-developer-6. Requests `tk-92c2d309ee5d`, `tk-06901411d7a4`, `tk-b8bb02fa74af`, `tk-b762df1c4e88` (the build re-challenge, `ch-2b7fcd68-2fa8-4d29-9a79-8470b497e782`: RESHAPE, accepted with one change, section 12c). The challenge (`ch-765ea8cb-231d-40fb-8473-15f20b2209b8`, RESHAPE) stays binding; its seven points are in section 13.

How to read the tags. **FACT** = I read it in this tree at the commit above (a path is given). **ESTIMATE** = a number or effort I reasoned out and did not measure. **ASSUMPTION** = something the design relies on that I did not verify; each is listed again in section 14 with how to check it.

## 1. What changes for whom

**A new team.** Setting up a managed lead (`agenttalk managed-lead-loop set <lead>` and starting it with `agenttalk wrap --loop --lead-loop`) will switch the lead routine on with no extra step, once default-on ships (slice 2). The routine's progress is recorded and shown in `status`, `overview` and `doctor`. A team that does not want it runs one command to switch it off, with a reason. The choice survives restarts, `reset`, `managed-lead-loop set` and `init --force` (sections 4 and 8). (ASSUMPTION: "starting a managed lead" means the controller acquiring its lease.)

**An existing team that already has a managed lead.** After the slice 2 upgrade the first controller start turns the routine on, writes one visible notice (recorded in `lead_routine.<agent>.default_on_notice`, section 4) and sends it to the operator-facing seat. A team with no managed lead gets nothing switched on.

**An existing team whose lead is an interactive session** (the usual Claude Code or Codex window). Nothing starts by itself, because an interactive session cannot be driven from outside. The routine stays a written procedure; what is new is the record of what completed.

**The seats waiting on a result.** A builder that waits on a review verdict or a gate result gets the lead's decision, or an explicit "nothing needed", sent once. If the lead is slow, a reminder follows and then an escalation until the obligation closes. The design promises a best-effort target, not a deadline (section 6).

**What slice 1 promises, and what it does not.** Slice 1 makes the owed hand-off of a **finished** result **visible**, **forwardable once** and **reminded**, **best effort**, **for declared waiters only**. It does not promise delivery, a time, or that every waiting seat is found. **A seat that never declared that it waits (`waiting_on`, or `relay_to` from the lead) is invisible to this mechanism**: no obligation exists for it, nothing is reminded, and `status` says nothing about it. That is the normal case for a builder that does not know to declare, and it is the design's largest limit; the lead skill text must say it in the same plain words. Nothing in this document promises delivery, and no test is allowed to be read as a promise of it.

**What ships when.** Slice 1 is the relay obligations, the atomic hand-off, coverage of every consuming path, visible owed and overdue records, best-effort reminders and a last-completion field that says exactly what completed. Default-on, the routine-health label, snapshots and rule watching come later; their contracts are settled in sections 7 to 9 now, because default-on is the operator's requirement.

## 2. What exists today (all FACT, read at the commit above)

| Part | What it does | Where |
|---|---|---|
| Lead skill, "Lead routine" | A 7-step check-in "for example every 30 minutes", a handover note every two hours, one line per timer, renew timers before they expire. It says it cannot promise a timer never lapses. | `src/agenttalk/skills/claude/agenttalk.lead.md`; the Codex copy differs only in command spelling (diff of the section) in `skills/codex/agenttalk-lead/SKILL.md` |
| Timers in the skill | Claude Code `/loop 30m`; Codex "has no built-in timer", use an operating-system task. | same files |
| Managed lead loop | An agent in config `managed_lead_loop` is consumed by a wrapped controller (`wrap --loop --lead-loop`) holding a renewable lease; a second consumer is refused on `recv`, `drain`, `wait`, `ack`. | `cli.py` `_guard_lead_loop_consumer`; `store.py` lease functions |
| Per-record ownership check | `_lead_loop_drive` wraps the **normal message turn** (not a synthetic one): lease check, then the model turn. | `cli.py` near line 12748 |
| Cadence sweep | When no message is pending and `cadence_seconds` (default 300) passed, a bounded read-only snapshot is built and a model turn runs only for `outbound_reminder` (thread older than 1800 s), `dead_letter`, `unrouted_escalation`. The sweep never advances a cursor or dead-letters. A sweep with nothing actionable is recorded as a success. | `lead_loop_cadence.py`; `cli.py` near line 12828; `wrapper/loop.py` idle branch near line 1607 |
| Sweep reads degrade to empty | Each snapshot section that fails to read becomes an empty collection and the sweep still counts as successful. | `lead_loop_cadence.py` (`except Exception` blocks of `build_cadence_snapshot`) |
| Cadence only when idle | `run_loop` consults the cadence hook only when there is no pending record, so sustained traffic starves it. | `wrapper/loop.py` idle branch |
| Turn watchdog | Not a hard deadline: it needs the turn to be 1800 s old **and** a live tool-like descendant alive 600 s, and it never kills when the process snapshot is unavailable (fail-open). Default-on only for a continuous wrapped Codex loop, not Claude. | `wrapper/turn_watchdog.py` (header and decision function); `cli.py` near line 13474 |
| Consuming paths, global and scoped | **Global** consumers end in `Store.advance_cursor`: `recv --ack` and `drain` (`_do_recv`), `wait`, `ack`, the wrapper's GLOBAL `commit`, the owed-action path and the store's recovery. **Scoped** wrapper consumption (`--to-request`) calls `mark_thread_seen` only and deliberately leaves the global cursor alone, and never closes the thread. This design changes neither rule. | `cli.py` `_do_recv`, `cmd_wait`, `cmd_ack`; `wrapper/recv_api.py` (module header and `commit`); `wrapper/obligations.py`; `store.py` |
| Locked, deduplicated publication | `send_operation(operation_nonce, operation_digest)` takes `state/operation-publication.lock` (rank 100), persists an intent, scans `valid_messages()` for the nonce, returns the existing message if the digest matches, refuses a different payload under the same nonce, otherwise sends (taking the rank-160 message-publication mutex) and marks the intent published. | `store.py` `send_operation`; ranks in `docs/LOCK-ORDER.md` |
| Terminal result semantics | By kind: a `task-response` closes with `done`, `declined` or a verdict without status, and `accepted` is **not** terminal for it (the ball stays with the assignee); a `review-result` closes with `approved` or `rejected`, and `needs-info` passes the ball back; a `proposal-response` closes with **every** status of its enum, `accepted`, `rejected` and `countered` alike. | `threads.py` `_classify_event`; `gates.py` `TERMINAL_RESPONSE_STATUSES` |
| Compaction | `compact` archives messages below a keep floor; defaults keep the newest 1000 and 30 days, automatic mode is off. The floor never archives unread messages and keeps the whole request group of any protected thread state. The floor is computed first (`_run_compaction`), then `archive_messages_below` moves the files with **no lock and no recheck**: a message published between the two steps is not considered. A closed request whose messages were archived answers `unknown` to `check --to-request` (a documented retention boundary). | `cli.py` `_compute_keep_floor`, `_run_compaction`, `COMPACT_DEFAULTS`; `store.py` `archive_messages_below`; `tests/test_compact.py` `test_old_closed_thread_check_returns_unknown_after_archive` |
| Reserved relay metadata | The relay verbs scrub `operator_command`, `operator_answer`, `operator_origin`, `operator_command_override`, `override_reason`, `needs_operator`, `broadcast_id`, `in_reply_to`, `target_msg_id`. The keys this design uses are not among them. | `cli.py` `_RELAY_RESERVED_META` |
| Relay verbs | Only `operator-answer` and `operator-command`. Nothing forwards a result to a waiting seat. | `cli.py` `cmd_relay` |
| Waiting seat | No field says "seat S waits for request R" (no `waiting_on`, `blocked_by` or `depends_on` in `work_board.py` or `threads.py`). | `threads.py`, `work_board.py` |
| Config traps | `set_managed_lead_loop(enabled=True)` rebuilds the agent's whole entry from three fields. `init --force` builds a fresh config and carries only the `retired` tombstones and the external trust classes. | `store.py` `set_managed_lead_loop`, `init` |
| Reset | `reset` deletes `messages/`, `state/`, `checkpoints/`; `config.json` stays. | `store.py` `reset` |
| Health today | `lead_loop_state`; `doctor` check `lead_loop` (unarmed managed lead = ERROR, legacy lead with open work and no live heartbeat = WARN); `status` rows carry `lead_loop`; the dashboard lists "lead-loop unarmed". | `store.py`, `doctor.py`, `cli.py`, `web.py` |
| Checkpoints and hooks | Claude hooks: heartbeat on PostToolUse, `checkpoint save` on PreCompact, `checkpoint resume` on SessionStart (compact). Codex gets only the heartbeat hook. | `docs/USER-MANUAL.md`; `checkpoint.py` |
| Hosts and systems | `wrap --lead-loop` is refused for backend profile `ovh-qwen`. The bundled supervisor needs Windows and PowerShell 7; no restarter exists on Linux and macOS (#356). | `cli.py`; `README.md`; `docs/supervisor-hosting.md` |
| Signing | Message signing is **optional**: `signing_enforced()` is true only when the per-user key file exists (and stays on if it cannot be probed). It proves which key wrote a message and explicitly does **not** defend against another process of the same OS user, which shares the key. The identity RFC lists a same-user peer and a rostered participant writing misleading valid messages as out of scope. | `signing.py` (module docstring); `store.py` `signing_enforced`, `valid_messages`; `docs/rfc-identity-authz.md` (Threat Model) |
| Attempt and commit | The wrapper records an attempt **before** `drive()` (`record_attempt_start`) and commits only after a successful outcome (`outcome.ok`, then `_commit`). A crash between the two leaves the record pending, and normal recovery redelivers it. | `wrapper/loop.py` (write-ahead block and the `outcome.ok` branch) |
| Owed-action ledger | A signed-policy ledger that enforces that a wrapped turn commits; considered and not reused, because relay obligations are per-recipient hand-offs that must be reconstructable from the bus (ASSUMPTION about fit; I read the module header and its lock rank, not the whole module). | `wrapper/obligations.py`, rank 170 |

## 3. The procedure: generic steps, and where a project adds its own

One versioned text, shipped inside the two lead skills (ASSUMPTION: a constant `LEAD_ROUTINE_VERSION` beside it so code can compare). It names **parts**. Each part has a machine half (cheap, no model turn) and a judgement half (a turn, and only if the machine half found something). The 13-step template from the lead's own checklist maps onto these generic parts; nothing project-specific is copied.

| Part | Machine half | Judgement half |
|---|---|---|
| 1 Bus | read-only snapshot of threads (exists) | answer what is owed |
| 2 Relay | owed and overdue obligations (section 5) | forward the decision, or record "nothing needed" |
| 3 Seat health | `status` health states and heartbeats (exist) | look into a stuck or parked seat |
| 4 Spend | budget feed if the team has one (exists, optional) | tell the person in charge before the limit |
| 5 Merge gates | review-results with GO not yet merged; `board verify-merges` (exist) | merge or hold, read inline review comments |
| 6 Learning | count of uncurated lessons (`knowledge pull`, exists) | verify or retract |
| 7 Memory delta | none: **manual trigger** | write the handover note (`lead-routine complete --part handover`) |
| 8 Machine resources | none: **manual trigger** (FACT, skill: agenttalk does not check memory) | the lead's own OS check (`lead-routine complete --part resources`) |
| 9 Scheduled items | the project's own list when the project defines one (below); otherwise none: **manual trigger** | act on what is due (`lead-routine complete --part scheduled`) |
| 10 Self-check | procedure version and project rule comparison (section 9) | read the shown change, acknowledge it |

**Which parts are automatic.** The rule "the judgement half runs only when the machine half finds something" applies to parts 1 to 6 and 10, which have a machine half. Parts 7, 8 and 9 (without a project list) have **no machine half and no automatic trigger**: the lead runs them by hand and records them with `lead-routine complete --part <name>`. They are **not covered by automatic health**: `status` lists their last recorded time under `manual_parts` and they never cause `overdue` or `running`. Consequently the quiet-team estimate of section 14 is unchanged: no automatic turn is ever scheduled for them. The skill text says the same, so the procedure does not imply coverage it does not give.

**Where a project adds its own.** A project keeps its rules in its own files. The procedure reads them, never edits them, and fingerprints them (section 9). Optional: `.agenttalk/lead-routine.project.md` (ASSUMPTION: this name; config may point elsewhere), whose text is added to the prompt after the generic text, labelled as the project's additions.

**What is enforced and what is an instruction** (the rule for this design):
- *Enforced in code and tested:* the loop never writes a rule file; a rule file is read once per turn and the same bytes are fingerprinted and put in the prompt; every unacknowledged change is shown, with its difference, in the prompt and in `status`; the relay obligations and health states below.
- *Instruction only, not enforced:* that a project's additions must not weaken a generic part. The prompt says so; nothing mechanical checks the prose.

## 4. Records and where they live

| Record | Where | Authority | Survives `reset`? |
|---|---|---|---|
| Relay obligations | **derived from the bus** every time (section 5); an optional index `state/<lead>.relay-index.json` only speeds the derivation | the bus messages | No, because the messages go too (FACT: reset deletes `messages/` and `state/`) |
| Routine record: procedure version seen, mode and controller start it belongs to, sweep coverage, completion per part, `generation` | `state/<lead>.lead-routine.json`; **every update is a serialised read-modify-write** under `state/<lead>.lead-routine.lock` (a store lock path, which the lock inventory treats as a leaf, rank 210, FACT `docs/LOCK-ORDER.md`) | this file | No |
| Acknowledged rule copies | `state/<lead>.rules-acked/` (size-capped copies, to show a difference) | this folder | No |
| Opt-out and default-on notice | config key `lead_routine`, a map by agent name: `{enabled, reason, by, at, default_on_notice: {at, version, message_id}}` | `config.json` | **Yes** (FACT: config stays); carried by `init --force` (section 8) |

**A missing or torn file.** The routine record and the index are caches or counters, so a missing or torn one reads as "nothing recorded" (as the existing cadence state does). The obligation is not stored anywhere that can be lost: it is derived, so there is nothing to read back (finding 2). A **corrupt or future-dated** routine record is never read as healthy (section 7).

**Writers and the synchronisation rule for the routine record.** The file has two kinds of writer, the controller (one write per sweep and per judgement turn) and the public `lead-routine complete`, so it is **not single-writer** and atomic replacement alone (which prevents torn JSON) would lose updates. Contract: (1) every write takes the lock, re-reads the file, changes only its own part (or the sweep block), increments `generation` and replaces the file atomically; (2) a controller write carries its `controller_start`, and is **refused** (and logged, never raised into the idle loop) when the stored record belongs to a later `controller_start`, so a stale controller cannot overwrite a newer controller's record; (3) a controller write also names the **lease it acquired itself at start-up** (`lease_id`, `acquired_at`), carried through the sweep and never read back from the store at write time, and the lock-held write re-checks that this lease is still the live one, so a replacement that took over while the old controller was computing its snapshot is never labelled with the old controller's work, and the old write is refused and logged; (4) an interactive write (`lead-routine complete`) writes only the part's `judgement` record and never touches a `machine` record (next paragraph). Tests: two concurrent `complete` commands for different parts through a barrier keep both parts; an old-generation controller write is refused and leaves the newer record byte-identical; a write that loses the lock race re-reads and applies cleanly.

## 5. The relay obligation: derived from the bus, closed by an atomic hand-off

**Definitions.**
- *Result:* a message to the lead that `_classify_event` classes as terminal on a thread the lead opened (FACT: that function; the public helper `threads.terminal_responses` wraps it through `_replay_step`, the one transition step it shares with `derive_threads`, including the **re-ask rule**: a new opener on the same request id after a terminal response re-opens the thread and the old result no longer stands, so it is never forwarded as current).
- *Waiter declaration:* a message from seat S with metadata `waiting_on=<request id>`, or `relay_to=<seats>` on the lead's own opening message of that request. `waiting_off=<request id>` from S cancels S's declaration. None of these keys is reserved (FACT, section 2). Any seat can declare; a declaration only creates an obligation for the lead to answer it, never an action by the lead.
- *Obligation* (lead L, result R, seat S): S has a declaration for R's request, no cancel after it, S is not retired (FACT: `derive_threads` drops retired peers), and there is no closing message.
- *Closing message:* a message whose **declared sender is L** and whose recipient is S, carrying `relay_of=R` and `relay_nonce` (below), either the decision text, an explicit note `relay_outcome=not-needed` with a reason, or the note `relay_outcome=unresolved` (below). **A closing message counts only as the full tuple** (fix round 1): `relay_of`, the `relay_nonce` that the hand-off operation computes for this lead, target and seat, a supported outcome (none means forwarded; anything else is not a closing) and, for not-needed, a reason. A message with only `relay_of` is ordinary chatter and closes nothing. A forwarded or not-needed closing also names the request (`relay_request`), so the closed relation survives the archiving of the request and its result in an earlier compaction batch (a closed hand-off stays closed; a genuinely late declaration, with no closing, is still `unresolved`). **Integrity promise, deliberately narrow:** the derivation counts a closing message only if its declared sender is L and it passes the store's configured integrity policy (`valid_messages()`: schema and roster always; the HMAC signature when `signing_enforced()`). It does **not** prove that L, rather than another process of the same OS user that shares the signing key, wrote it; protection against a malicious same-user seat is **outside this slice** (FACT, section 2: `signing.py` and the identity RFC say so), and no new identity system is added. Tests, separately: a closing message with a wrong declared sender is not counted; with signing on, an unsigned or badly signed closing message is rejected by the gate; with signing off, a closing message with the right declared sender is counted (and the test names this as the configured policy, not as proof).

**Per-recipient states** (all derived): `owed`, `closed-forwarded`, `closed-not-needed`, `closed-recipient-retired`, `cancelled`, `unresolved`, `closed-unresolved`. `--none-needed` closes only the named recipient. A declaration that arrives **after** the result was handled creates a new owed obligation while the result is still in live history (membership is **not** frozen at delivery; the limit is the other way round: a seat that never declares is unknown to the system). A retirement closes the obligation as `closed-recipient-retired`; a cancel before a hand-off removes it.

**Late declarations: the `unresolved` outcome (decision).** A declaration `waiting_on=R` whose request `R` has no opener or result in **live history** (compacted away, or never existed, which the bus cannot tell apart) is **never silently dropped**. Its derived state is `unresolved` with the text "unresolved: result no longer in live history". It is shown (1) to the lead: in the owed-relay block, in `status` and in `lead-routine status`, as an item that needs an answer; (2) to the waiting seat: by the read-only, non-consuming command `agenttalk relay waiting --for <seat>`, which lists that seat's declarations with their derived state, and by the typed note the lead publishes. The lead closes it with `agenttalk relay result --request-id R --to S --unresolved [-m TEXT]`, the same atomic operation (nonce = first 32 hex of sha256 of `relay`, L, `unresolved`, R, S, so it is sent once), which becomes `closed-unresolved`. Until then it is an owed item like any other: it is reminded, escalated and counted as overdue. Contract tests: a declaration published after `compact` archived the request shows `unresolved` to the lead and to the seat; closing it sends exactly one note; it is never absent from both views.

**The atomic operation.** `agenttalk relay result --result-id R --to S (-m TEXT | --none-needed --reason TEXT)` calls one store function modelled on `send_operation` (FACT precedent), with `operation="relay"` in the intent identity:
1. identity: nonce = first 32 hex of sha256 of `relay`, L, R, S; digest = sha256 over the canonical intent plus the body;
2. take `state/operation-publication.lock` (rank 100, FACT);
3. **inside the lock**, re-derive: R exists and is a result for L, S is a valid recipient; refuse with a typed error otherwise;
4. record the intent, scan `valid_messages()` for sender L and this nonce: same digest returns the existing message and repairs the intent without sending; a different digest is refused as a payload conflict (FACT behaviour of `send_operation`);
5. otherwise `send` (rank 160 inside rank 100, the same order `send_operation` already uses), then mark the intent published.

So two processes that race, a crash between send and intent update, and a rerun all end in exactly one message. No lock is added in slice 1 (the existing rank 100 lock is reused); any new lock needs a rank in `docs/LOCK-ORDER.md` and an inventory test (FACT: that file says so).

**Why there is no journal to lose, and what recovery looks like at each boundary.**

| Interruption | What is left | What happens next |
|---|---|---|
| after the result arrives, before the cursor moves | result unread, declaration on the bus | normal redelivery; the obligation is derived when the result is shown |
| after the cursor moved, before the lead acts | obligation derived `owed` from the bus | shown again by `status`, the next prompt and the sweep |
| index file missing, torn or stale | derived from the bus | index rebuilt; no error |
| crash after the intent, before `send` | intent `prepared`, no message | the rerun sends once |
| crash after `send`, before the intent update | message on the bus | the rerun finds it, repairs the intent, sends nothing |
| two commands at once | one wins the lock | the other returns the first message |
| messages archived by `compact` | result or declaration gone | two rules. (1) **Protection:** a request group with an owed obligation is never archived (a protected component in `_compute_keep_floor`). (2) **Synchronisation (decision):** the compaction run recomputes the floor, rechecks the owed obligations **and moves the files inside the same locks that publication uses**: it takes `state/operation-publication.lock` (rank 100) and then the message-publication mutex (rank 160), in the order `send_operation` already uses, and holds the rank 160 mutex for the recheck-and-move of one batch of files (batches of at most 200 files; the locks are released between batches and the recheck is repeated in the next one). A declaration that is published after a batch finds its request archived and becomes `unresolved` (above). Both orders are tested, plus a barrier test with a declaration racing a compaction; the invariant is that **every declaration ends up owed, closed or unresolved, never absent** (ASSUMPTION: the batch size and that holding the mutex for a batch is acceptable to concurrent senders; ESTIMATE: milliseconds per batch). |
| `reset` | messages gone | obligations gone with them, consistently |

**Every consuming path.** Because no write is needed at consumption, every path is covered by the same rule: the obligation exists from the moment the declaration and the result are on the bus. What the paths add is the *notice*. The **global** paths (`drain`, `recv --ack`, `wait`, `ack`, the wrapper's GLOBAL commit) end in `Store.advance_cursor`; the **scoped** wrapper path (`--to-request`) ends in `mark_thread_seen` and leaves the global cursor alone (FACT, section 2). This design **does not change either rule**: the notice is printed from the render step of each path and never moves a cursor or a thread-seen pointer. The first slice makes `drain`, `recv`, `wait` print an "owed relay" block after the messages they show when the agent is a lead with an obligation (ASSUMPTION: each prints from its existing render loop), and `status` and `lead-routine status` always list them. The wrapped controller adds the same block to **every** message prompt (global and scoped turns alike) and to the sweep prompt while an obligation is owed. A path that consumes silently (`ack`, a scoped `mark_thread_seen`) loses nothing, because the next block, status or sweep derives the obligation again. Notice tests run in **both** modes.

**The cursor, stated exactly.** A delivered result is consumed once through the normal path, as today. Its obligation is separate, derived, state. A reminder is a synthetic sweep turn and never advances a cursor (FACT). A result is never replayed until someone forwards it.

## 6. Reminders, the delivery target, and where no time can be promised

**The promise.** A *best-effort target*: once the lead is free (not inside a turn), an owed obligation is shown in the next prompt, and if it is older than `relay_deadline_seconds` (default 600; ESTIMATE, twice the default sweep interval) the sweep raises `relay_overdue`. This is a target, not a deadline.

**Retry and escalation until the obligation closes.** Reminders are bucketed by age: raised at 1, 2, 4, 8 times the deadline, then every 24 hours, so a reminder turn that ignores the hand-off is followed by another one. A successful reminder turn does **not** close anything or latch a dedup; only a derived `closed-*` state does. After 3 reminders (default; ESTIMATE) the sweep escalates once to the operator through `escalate`, and again each 24 hours while open. Dedup keys are per (lead, result, seat, bucket).

**Every case where the target is unbounded or unavailable** (each is a stated limit and a test):
1. *Long turn.* A wrapped lead handles one record at a time. During a long turn nothing is delivered and nothing is reminded. The watchdog does not bound this: it needs an aged tool descendant and fails open, and it is off by default for Claude (FACT, section 2). A long reasoning turn without tools is unbounded.
2. *Continuous traffic.* The sweep runs only when no message is pending (FACT), so it can be starved. The per-turn prompt block (section 5) is a mitigation only: a turn can ignore it.
3. *Watchdog off or unsupported* (process snapshot unavailable): same as 1.
4. *A reminder turn that ignores the hand-off:* handled by the repeat and escalation above; the delay is the bucket length.
5. *The lead is down, unarmed or in failure backoff* (up to 1800 s, FACT): nothing runs. The existing `lead_loop` doctor ERROR covers an unarmed managed lead; the new state shows `overdue` with the reason.
6. *Interactive lead:* the block appears only when the lead consumes or runs `status`.
7. *Linux and macOS:* no restarter (FACT), so a crashed controller stays down.
8. *A waiter that never declared:* unknown to the system.

No second mailbox consumer is added: the derivation reads messages without a cursor (FACT: the cadence snapshot already does), and sending is not consuming.

## 7. Health: exhaustive states, precedence, and what each shows

Two things are tracked **separately** and shown separately: the *machine sweep* (what the controller attempted and covered) and the *lead's routine* (which parts the lead completed). A sweep never renews the freshness of a judgement part, and only parts with a machine half can be covered automatically (section 3).

**Records.** Every part has **two separate records** (resolved in fix round 1: the first wording of this paragraph assigned one shared `completion[part]`, which contradicted the separation promised above and let a sweep erase the lead's own completion and the other way round). `parts[part].machine` is written only by a sweep: `{at, kind: sweep, mode: managed, controller_start, lease_id, covered, failed, unattempted}`. `parts[part].judgement` is written only by `lead-routine complete --part <name>` and a judgement turn: `{at, kind: self-reported|turn, by, mode: managed|interactive, note, ...}`. The sweep block carries `{at, attempted: [sections], failed: [sections], controller_start, lease_id}` (this needs `build_cadence_snapshot` to report which sections failed instead of hiding them, section 2). A sweep renews a part's `machine` record **only when every section that part needs is in `attempted` and none is in `failed`**; otherwise the earlier record stays and names `failed` or `unattempted` (a part's absence from `failed` is not proof it was read). Parts without a machine half have only `judgement`. All writes follow the synchronisation rule of section 4. Tests: both write orders on the same part keep both records.

**Step 0: normalise the records (not a row).** Before any row is applied: (a) derive the **mode** from the configuration (`managed`, `interactive` or `backend-refused`); (b) a record that belongs to another mode or to another `controller_start` than the current one is **discarded for classification** (treated as absent), and the discard is noted as `record-other-mode` in the evidence; (c) a record that cannot be read, or has a timestamp more than 120 s (ESTIMATE) in the future, is **kept and flagged** `record-unreadable` or `record-future-dated`; (d) sweep rows and completion rows are then considered separately.

**Precedence** (after step 0; **first matching row wins**; one state and one reason per agent). Rows 4 to 8 and 12 apply to a **managed** lead only; rows 13 to 15 apply to an **interactive** lead only; the other rows apply to both.

| # | Condition | State | Reason code |
|---|---|---|---|
| 1 | a valid opt-out exists | **deliberately disabled** | `opt-out` |
| 2 | mode is `backend-refused` (no automatic driver is possible for this backend) | **unsupported** | `backend-refused` |
| 3 | the record is flagged unreadable or future-dated | **overdue** | `record-unreadable` / `record-future-dated` (never read as healthy) |
| 4 | managed and the lease is not armed (`lead_loop_state`) | **overdue** | `controller-not-armed` (a recent sweep does not rescue it) |
| 5 | managed, no sweep record after step 0, inside the grace (2 x cadence from controller start; ESTIMATE) | **starting** | `first-sweep-pending` |
| 6 | managed, no sweep record after step 0, past the grace | **overdue** | `never-swept` |
| 7 | managed and the latest sweep has a section in `failed` | **overdue** | `sweep-incomplete` |
| 8 | managed and the latest sweep is older than 3 x cadence | **overdue** | `sweep-stale` |
| 9 | any relay obligation, including an `unresolved` one, is past its deadline | **overdue** | `relay-overdue` |
| 10 | a judgement part is owed (its machine half found something) and no completion of it is newer | **overdue** | `part-owed:<name>` |
| 11 | a rule change was shown and not acknowledged for longer than one interval | **overdue** | `rules-unacknowledged` |
| 12 | managed, and none of rows 3 to 11 matched | **running** | evidence: the latest sweep, the machine-half completions it wrote, the open obligations |
| 13 | interactive and no completion record after step 0 | **unsupported** | `not-recorded` (no automatic driver here; fallback in section 10) |
| 14 | interactive and the newest completion is within 1.5 x interval | **running** | `self-reported`, with the time and the part |
| 15 | interactive and the newest completion is older than 1.5 x interval | **overdue** | `stale-completion` |
| 16 | anything left over | **overdue** | `unclassified` (must be unreachable; the table test asserts it is never produced) |

Because an interactive lead has no sweep, rows 4 to 8 and 12 never apply to it, so a completion recorded just now gives row 14 (`running`, self-reported) unless an overdue relay (row 9), an owed part (row 10) or an unacknowledged rule (row 11) says otherwise. A managed lead is never classified by a completion alone: it needs rows 4 to 8 to be false. A registered timer never appears in this table. Parts without a machine half (section 3) appear under `manual_parts` and in no row. The independent checks of rows 4, 7 and 8 stay as they were.

**Table-driven test (required).** One test builds the cross product of: mode (managed, interactive, backend-refused) x opt-out (none, valid) x record (missing, readable, unreadable, future-dated, from another mode, from another controller start) x newest completion (missing, fresh, stale) x latest sweep (missing, fresh with no failed section, fresh with a failed section, stale) x armed (yes, no) x relay (none, owed inside the deadline, overdue, unresolved overdue) x part owed (no, yes) x rule change (none, unacknowledged longer than an interval), and asserts for **every** combination that exactly one row matches, that the reason code is the expected one from an independent oracle written in the test, that `unclassified` never occurs, and that `running` never appears without row 12 or row 14's evidence. Named cases from the read must appear as explicit rows of the test: an interactive lead with a completion recorded now; an old completion with a sweep younger than 3 cadences and no owed work (managed: running; interactive: `stale-completion`); a changed controller start with an old record.

**What each screen shows.** `status` row `lead_routine`: `state`, `reasons`, `sweep: {at, attempted, failed}`, `parts: {name: {at, kind}}`, `open_relays: n`, `overdue_relays: n`, and for `disabled` the `by`, `at`, `reason`. `overview`: one plain line ("Lead routine: running, last sweep 4 min ago, 0 owed hand-offs" / "OVERDUE: controller not armed" / "off on purpose since <date>" / "not available here: <why>. Fallback: <fallback>"). `doctor`: check `lead_routine`, WARN for overdue (the existing `lead_loop` ERROR is unchanged), INFO for disabled and unsupported.

## 8. Defaults and the opt-out (commands, persistence, lifecycle)

**Commands** (slice 2 except `complete`): `agenttalk lead-routine status|complete|disable|enable|ack-rules --for <lead>`; `disable` needs `--reason`.

**Persistence.** The opt-out lives in config key `lead_routine.<agent>`, separate from `managed_lead_loop`, because `managed-lead-loop set` rebuilds its own entry (FACT). A validator next to `validate_managed_lead_loop` checks the shape (ASSUMPTION: unknown top-level keys are otherwise tolerated).

**Force initialisation and roster changes (decision).** A valid opt-out **survives** `init --force`: `init` carries the whole `lead_routine` map the way it already carries `retired` and `trust_classes` (FACT: `retired_carry`, `active_external`), **including entries for identities that are not in the new roster**: the validator checks the shape of an entry and does not require roster membership (unlike `managed_lead_loop`), so omitting a seat from a force-init and adding it again later finds its opt-out and its `default_on_notice` unchanged. An opt-out is cleared **only** by an explicit, operator-confirmed destructive choice: `lead-routine enable --for <name>` (works for a name that is not in the roster), `init --force --forget-lead-routine-optout <name>` (one name per flag; ASSUMPTION: this name; it may name an absent identity), or removing `.agenttalk/` entirely (FACT: the existing tombstone message says the same). Nothing else drops an entry, and `init` prints the names it carried that are not in the roster, for information only. Tests: a disabled agent reconfigured with `init --force` with the same roster and restarted stays disabled; **a disabled agent omitted from one force-init and re-added in the next is still disabled, with the same notice record**; `--forget-lead-routine-optout` clears it once and only the named identity.

**The one-time default-on notice** is recorded in `lead_routine.<agent>.default_on_notice = {at, version, message_id}` in config, so it survives `reset` and `init --force` and is sent once. Absence of the field means "not yet shown".

## 9. The self-check: the procedure and the project's rules

1. **Procedure version.** The routine record stores the version last run. A newer shipped version raises `procedure_stale` once per version; `lead-routine complete --part self-check` clears it.
2. **Project rules: notify, do not gate (decision).** The project's rule file is the project's own authority, so a change is **active from the next turn**; there is no activation gate and no suspension of project additions. What is promised, and enforced:
   - the loop never writes a rule file (test: hash and mtime unchanged after a sweep and after a turn);
   - each turn reads each rule file once; those exact bytes are fingerprinted and placed in the prompt (test: a file changed between the fingerprint and the prompt build shows in the next turn, not a mixture);
   - every unacknowledged change is shown with its difference (against the stored acknowledged copy) in the prompt and in `status` until `lead-routine ack-rules` (test: shown on the next turn, still shown until acknowledged, gone after);
   - a rule change is also escalated to the operator once (existing `escalate`).
   Prose limits on project additions are instructions, not enforcement (section 3); the earlier idea of refusing a project file by matching part names is dropped.
3. **Derived notes.** The routine record (which parts apply, the timer lines of an interactive lead) is regenerated automatically; this is the only thing the self-check rewrites.
4. **Timers.** A managed lead has none to renew (the cycle is in-process and the lease renews with it, FACT). An interactive lead keeps the skill's rule; health never depends on it, because agenttalk cannot read a session timer (ASSUMPTION; the skill itself says nothing tells you when one is gone).

## 10. Support by host, mode and operating system

| Host and mode | Driver | Compaction | Fallback and health |
|---|---|---|---|
| Claude Code, wrapped managed lead | the cadence sweep (FACT) plus the new items | the model child resumes its session across turns (FACT: `make_cadence_drive` shares session state); the controller saves a **recovery snapshot** per sweep (ASSUMPTION: `checkpoint save --trigger auto` callable there) | none needed |
| Claude Code, interactive | none by agenttalk; `/loop` is session-scoped (FACT, skill) | hooks: save on PreCompact, resume on SessionStart (FACT); resume text gains owed relays (ASSUMPTION) | the routine's last step runs `lead-routine complete`; health from rows 13 to 15 (self-reported, never swept) |
| Codex, wrapped managed lead | the same sweep (FACT: `--lead-loop` is generic by agent name, refused only for `ovh-qwen`) | no Codex checkpoint hook (FACT): recovery snapshots per sweep | none needed |
| Codex, interactive | none (FACT, skill) | none | rows 13 to 15: `unsupported` until a completion is recorded, then self-reported; fallback "an operating-system task that nudges the lead, or run `lead-routine complete`" |
| `ovh-qwen` | none: `wrap --lead-loop` refused (FACT) | n/a | `unsupported`, `backend-refused` |
| Windows | all above; the bundled supervisor restarts a crashed controller (FACT) | as above | as above |
| Linux, macOS | `wrap --lead-loop` as a command (ASSUMPTION: nothing in it is Windows-only); **no restarter** (FACT, #356) | as above; Codex needs `reply_shell` (FACT, README) | `overdue` with `controller-not-armed`, plus the existing `lead_loop` ERROR; fallback text "restart `wrap --lead-loop`"; default-on means "on while the controller runs" |

## 11. The acceptance demonstration

Fixture store, fake clock, fake model child (ASSUMPTION: the existing wrapper tests' fake-spawn seams serve). Seats `lead`, `reviewer`, `builder`. Agreed target: the best-effort target of section 6, with the unbounded cases printed.

1. `builder` sends a message with `waiting_on=<request id>`; `lead` issued that request to `reviewer`.
2. `lead` is busy: the fake child holds a turn open.
3. `reviewer` sends a `review-result` GO. Expected: nothing delivered yet; the obligation is already derivable (declaration and result are on the bus); health `running`, no reminder (limit 1, stated).
4. The turn ends; the result is delivered once through the normal path; the prompt carries the owed block.
5. **Restart, split at the commit boundary.** (a) *Before commit:* kill the controller after the attempt was recorded and the turn started but before a successful outcome is committed, then start a new process on the same store. Expected: the result is **redelivered by the normal recovery** (that is correct; nothing is acknowledged early), the obligation is unchanged (`owed`, derived), and the rerun turn's `relay result` publishes **once** (idempotent), even if the first attempt had already published. (b) *After commit:* let the turn finish and commit, then kill the controller before the lead has forwarded. Expected: the consumed result is **not replayed**, the obligation is still `owed`, and the sweep's reminder is non-consuming (no cursor moves).
6. The child runs `relay result --to builder`. Expected: exactly one message with `relay_of`, state `closed-forwarded`.
7. **No duplicate, three ways:** the command run twice; two processes at once through a barrier; a crash injected between the send and the intent update. Expected: one message each time.
8. **Index and journal loss:** delete the index and routine record. Expected: the obligation is still derived; no error.
9. **A reminder turn that ignores the hand-off:** expected: the next bucket raises it again; after 3 the operator escalation; closing the obligation stops it.
10. **Continuous traffic:** results keep arriving so the sweep never runs. Expected: the per-turn block still shows the obligation; the sweep-starved case is reported as the stated limit.
11. **Late declaration, cancel, retire:** a declaration after the result (result still in live history) creates an owed obligation; `waiting_off` removes it; retiring the seat closes it as `closed-recipient-retired`; `--none-needed` closes only the named seat.
12. **Compaction, both orders and a race.** (a) A declaration, then `compact` with a very low keep count: the request group is kept. (b) `compact` first, then a declaration for the archived request: the lead and the waiting seat both see `unresolved`, and `relay result --unresolved` sends exactly one note. (c) A declaration racing a compaction through a barrier, repeated: every declaration ends owed, closed or unresolved, never absent from the derivation.
12a. **Integrity, three ways:** a closing message with a wrong declared sender is not counted; with signing on, an unsigned closing message is rejected by the gate; with signing off, a closing message with the right declared sender is counted (stated as the configured policy).
12b. **Completion record:** two concurrent `lead-routine complete` for different parts keep both; an old-generation controller write is refused and leaves the newer record unchanged; parts without a machine half show under `manual_parts` and never change the state.
12c. **Both consuming modes, shown honestly.** (a) *Scoped wrapper path:* the wrapper consumes a result with `--to-request`, which calls `mark_thread_seen` and does **not** advance the global cursor. Expected: the turn prompt carries the owed-relay notice (the controller adds the block to every prompt, global and scoped alike), `status` and `lead-routine status` list the obligation, the obligation is identical to the one derived in global mode, and neither the global cursor nor the thread-seen pointer is moved by the notice. (b) *Scoped CLI path (`wait --to-request`, run by a person or a tool):* the notice is printed **after the command's own work and output** and is read-only; the build spike (section 15, step 0, check 2) showed that this changes neither the global cursor nor the thread-seen record, leaves the stock output and exit code byte-identical, and must be suppressed under `--json`. The test asserts exactly that. If a path is ever found that cannot print it, the design says so and that path shows the notice only through `status`, `lead-routine status` and the prompt; no path is claimed to show it when it does not.
12d. **Late declaration after the result was consumed and archived.** A result is consumed and the request is archived by `compact`; then a seat publishes `waiting_on` for it. Expected: the explicit outcome **`unresolved: no longer in live history`** is shown to the lead and to the waiting seat (`relay waiting`), and `relay result --unresolved` sends exactly one note; the declaration is never silently lost, and the test fails if the declaration is absent from both views.
12e. **The invisible waiter.** A seat that never declared: the test asserts that no obligation, no reminder and no `status` line exist for it, and that both lead skills carry the sentence that says so. This case documents a limit; passing it is not a delivery claim.
13. **Stale procedure, changed rule (slice 2/3):** a newer shipped version raises `procedure_stale` once; a changed rule file is active next turn, shown with its difference until `ack-rules`.
14. **Opt-out lifecycle (slice 2):** `disable`, then `managed-lead-loop set`, `reset`, `init --force` with the same roster, restart: still disabled; `init --force` that omits the seat and a later `init --force` that adds it again: still disabled with the same notice record; `--forget-lead-routine-optout` clears exactly the named one.
15. **Honest health (slice 2):** the table-driven test of section 7 over every mode and record state, including a sweep section that fails to read, a controller killed right after a sweep, a corrupt or future-dated record, a record from another mode or controller start, and an interactive lead with no records.

## 12. How each finding was answered

### 12a. Final read of revision 2 (`tk-004385a54646`): binding requirements for the build

| # | Finding | Contract now in the design |
|---|---|---|
| 1 P2 | a late waiter can lose its result to compaction; compaction races a declaration | explicit `unresolved` outcome shown to lead and waiting seat, closed by one atomic note, never silent; compaction rechecks and moves inside the rank 100 then rank 160 locks in batches; both orders and a barrier race tested (section 5, step 12 of section 11) |
| 2 P2 | health table contradictory or missing answers | step 0 normalises records, then first match; sweep rows (managed) and self-report rows (interactive) separate; final stale-completion and unclassified rows; every successful sweep writes completions; table-driven test over the full cross product (section 7) |
| 3 P2 | signing does not prove the lead sent a closing message | promise narrowed to the declared sender plus the store's configured integrity policy; same-OS-user seat outside this slice, no new identity system; three separate tests (section 5) |
| 4 P2 | a roster change can clear a valid opt-out | the whole `lead_routine` map is carried, entries for absent identities included; only `enable`, the explicit destructive flag or removing the store clears one; omit and re-add tested (section 8) |
| 5 P2 | restart demonstration promises no replay where recovery can replay | split at the commit boundary: before commit, normal redelivery with idempotent publication; after commit, no replay while the relay stays owed and the reminder is non-consuming (step 5 of section 11) |
| 6 P3 | writer and synchronisation of the completion record | serialised read-modify-write under a leaf lock with a `generation`, controller writes refused when stale; tests for concurrent parts and an old-generation write (section 4) |
| 7 P3 | parts without a machine check have no trigger | parts 7, 8 and 9 (without a project list) are manual: explicit `lead-routine complete --part`, listed under `manual_parts`, not covered by automatic health; estimate unchanged by them (section 3) |
| 8 P3 | two FACT rows need qualifying | global versus scoped consumption and `accepted` by response kind written into section 2; notice tests in both modes (sections 5, 11) |

### 12c. Build re-challenge (`ch-2b7fcd68-2fa8-4d29-9a79-8470b497e782`, RESHAPE, accepted with one change)

| Reshape point | Answer in the design |
|---|---|
| scope the promise as best effort, say an undeclared waiter is invisible, no wording that over-promises | section 1 ("What slice 1 promises, and what it does not"); two wordings removed; case 12e |
| acceptance case for the scoped wrapper path | case 12c (a) and (b), with the honest fallback if the CLI scoped path cannot print the notice |
| acceptance case for a late declaration after the result was consumed and archived | case 12d (the `unresolved` outcome of section 5) |
| a third design read | **declined by the lead's rule of one final read; replaced** by a build step 0 spike of the three load-bearing assumptions and a first code read of the five P2 contracts (section 15, "Build order") |
| kill signals | adopted as the build's kill signals (section 15) |

### 12b. First read of revision 1 (`tk-682a6b30d61e`)

| # | Finding | Answer |
|---|---|---|
| 1 P2 | two callers could send the same hand-off twice | one atomic operation under the existing rank-100 publication lock, with nonce, digest, in-lock re-derivation and payload-conflict refusal; two-process barrier and crash tests (section 5) |
| 2 P2 | obligations can disappear; only `drain` covered | no journal is the authority: obligations are derived from the bus, so no stored record can be lost (a message that is archived or deleted still ends the derivation, which is the `unresolved` case); every consuming path ends in `advance_cursor` and shows the block; recovery table per boundary; compaction protection (section 5) |
| 3 P2 | worst-case delivery time not supported | best-effort target; eight named unbounded or unavailable cases; bucketed repeat and escalation until closed; success of a reminder turn closes nothing (section 6). Not a second consumer |
| 4 P2 | health undefined or overclaiming | exhaustive 13-row precedence table; sweep coverage with failed sections; machine sweep and judgement parts tracked separately; every mode tested (section 7) |
| 5 P2 | changed rules active before acknowledgement | decision: notify, do not gate; promise narrowed to three enforced items; the pattern-matching claim dropped; single-read test (section 9) |
| 6 P2 | opt-out lost on `init --force`; notice field unnamed | carried by `init --force`; destructive clearing only by explicit flag; field `lead_routine.<agent>.default_on_notice` (section 8) |
| P3 | waiting-seat model incomplete | per-recipient states, late declaration creates a new obligation, `waiting_off`, retirement, `--none-needed` per recipient; membership is not frozen (section 5) |
| P3 | cursor test wrong | stated: the result is consumed once through the normal path, the obligation is separate derived state, a reminder never advances a cursor; the old "moves no cursor" test line is replaced (sections 5, 15) |

## 13. How the challenge's seven points are met

1. **Extend, no new timer, one consumer:** the existing sweep, lease and controller; the derivation and the sending are not consumption.
2. **Decisions on the message path:** the block is in every prompt while owed and the closing command is typed; the sweep catches omissions.
3. **Honest states:** section 7; never from a timer.
4. **On by default for a managed lead, persistent opt-out:** section 8 (slice 2); skills alone start nothing.
5. **Versioned, project-agnostic, exposes rule changes:** section 9; rules are shown, never written by the loop.
6. **Compaction and host support:** section 10.
7. **Kill signals:** no second consumer; no health from a timer; no rule written by the loop. If slice 1 needs a second reader to meet any target, the target is withdrawn, not the rule. The build adopts these and one more (section 15).

## 14. Assumptions to verify, risks, open questions

1. A small public helper around `_classify_event` can name a result.
2. `drain`, `recv`, `wait` can print the block from their existing render loops without changing their cursor behaviour.
3. `_compute_keep_floor` can take one more protected component without changing its fail-safe returns, and the compaction run can take the rank 100 and rank 160 locks per batch of at most 200 files without starving concurrent senders (the lock-order inventory test must list the new acquisition).
4. `build_cadence_snapshot` can report failed sections without breaking the "partial snapshot still drives" behaviour.
5. The turn prompt assembly can carry a block that appears on every message turn.
6. `relay_to`, `waiting_on`, `waiting_off`, `relay_of`, `relay_nonce`, `relay_outcome` pass the send path as ordinary metadata (they are not in `_RELAY_RESERVED_META`; the send path may have other limits).
7. `LEAD_ROUTINE_VERSION` can be read by code without reading the installed skill file.
8. `checkpoint save --trigger auto` is callable from the sweep.
9. Nothing in `wrap --lead-loop` is Windows-only.
10. The owed-action ledger really does not fit (section 2).
11. Seats will set `waiting_on`. If they do not, only `relay_to` from the lead counts; this is the design's largest honest gap and the skill text must say so.
12. Cost of default-on: no model turn when nothing is actionable (FACT); new items add turns only while an obligation is overdue or a change is shown. ESTIMATE: a quiet team adds none; not measured.
13. The busy-lead delay is a real limit (section 6, case 1). If the operator needs a hard bound during long turns, that needs a decision on turn bounds for leads, not a design workaround.
14. A seat that declares for a request id that never existed gets `unresolved` too (the bus cannot tell an archived request from a typo); this is acceptable noise and is closed by one note.
15. A controller's `controller_start` is a total order across restarts (it is taken from the lease acquisition time); two starts within the clock resolution would be ambiguous and the generation check then decides.

## 15. The smallest first slice, tests, estimate, files

**Slice 1** (the read's slice, adopted):
1. relay obligations derived from the bus, per recipient, with waiter declarations and cancel;
2. the atomic `relay result` operation (forward or `--none-needed`) reusing the rank-100 publication pattern;
3. the notice on every consuming path (`drain`, `recv`, `wait`; the wrapped prompt on every turn) plus `status` and `lead-routine status`, with the compaction protection that the derivation needs;
4. owed and overdue visibility and best-effort bucketed reminders and escalation through one new sweep item, `relay_overdue`;
5. `lead-routine complete`, and a last-completion field that names exactly what completed (`part`, `kind`, `covered`, `failed`) and never renews another part.

Later: slice 2 default-on, the opt-out, the routine-health label and its table, the `init --force` carry; slice 3 snapshots and rule watching.

**Tests, slice 1** (each is a contract; the build's code read checks that each exists and fails when its rule is removed).
- Derivation: declarations, cancel, late declaration inside live history, retirement, **wrong declared sender**, signing-on rejection, signing-off behaviour (three separate tests), empty set; torn and missing index; a declaration for an unknown request.
- `unresolved`: after `compact`; closing it sends exactly one note; it is visible to the lead and to the seat (`relay waiting`) and is never absent from both.
- Atomic operation: sequential rerun; **two real processes released by a barrier**; crash injected before the send, between the send and the intent update, and after; payload conflict; invalid recipient; the lock-order inventory updated for the compaction acquisition.
- Compaction: protection of an owed group; declaration-then-compact; compact-then-declaration; a **barrier race** between a declaration and a compaction batch; the invariant that no declaration is ever absent; `reset` consistency.
- Consuming paths in **both** modes: global (`drain`, `recv --ack`, `wait`, `ack`, the wrapper GLOBAL commit) and scoped (`--to-request`): the block present or the obligation re-derived, and neither the global cursor nor the thread-seen pointer moved by the notice.
- Restart split at the commit boundary: before commit (redelivery allowed, publication idempotent) and after commit (no replay, relay still owed, reminder non-consuming).
- Reminders: bucket schedule, a reminder turn that ignores the hand-off, escalation count, continuous traffic with the per-turn block, long turn (fake clock), watchdog disabled, lead unarmed.
- Completion record: two concurrent `complete` calls for different parts through a barrier keep both; an old-generation controller write is refused; a sweep does not renew a judgement part; `failed` sections are recorded; parts without a machine half appear only under `manual_parts`.
- Result classification by response kind: `accepted` is not terminal for a `task-response` and is terminal for a `proposal-response`.
- Skill lint and currency tests (`test_skill_lint.py`, `test_skill_currency.py`): the new commands resolve; both lead skills carry the same step and the same manual-parts text.
- Mutation per rule: remove the in-lock re-derivation, the nonce scan, the declared-sender check, the compaction recheck, the compaction lock, the completion lock, the generation check, the bucket growth, the per-turn block, the `unresolved` state: each turns a test red.

**Tests, slice 2 (contracts settled now).** The table-driven health test of section 7; the opt-out lifecycle including omit and re-add; the default-on notice recorded once; procedure and rule changes shown, never gated; the capability table per host, mode and system.

**Estimate (ESTIMATE, reasoned, not measured; revised up from rounds 1).** Slice 1 code: about 900 to 1,150 lines (derivation and states including `unresolved` 260, atomic operation and intent 170, notice blocks across the global and scoped paths 170, compaction protection plus the batch lock and recheck 110, sweep item and bucketed reminders 120, `lead-routine complete`, locked read-modify-write with generation and `relay waiting` 190, skill and doc text 100). Slice 1 tests: about 1,900 to 2,500 lines; the two-process barriers, the compaction race, the crash-injection cases, the both-modes consuming matrix and the concurrent completion writes are the expensive part. About 7 to 9 builder-days (plus about half a day for the step 0 spike below) for one builder including the locking and recovery tests and one read-and-fix round. Round 1 said 5 to 7; the extra two days are the compaction lock and race tests, the `unresolved` state, the completion-record lock and the scoped-mode matrix. The health table (section 7) and its table-driven test belong to slice 2 and add roughly 250 lines of code and 400 of tests there.

**Files touched, slice 1.** New `src/agenttalk/lead_relay.py` (derivation, states, `unresolved`), new `src/agenttalk/lead_routine.py` (completion record with its locked read-modify-write, status field); `src/agenttalk/store.py` (relay publication function, `archive_messages_below` taking the publication locks per batch, path helpers); `src/agenttalk/cli.py` (`relay result` including `--unresolved`, `relay waiting`, `lead-routine complete`, notice blocks in `_do_recv`, `cmd_wait` and the scoped path, `_lead_loop_drive` prompt, `_compute_keep_floor` component, `_run_compaction` recheck, `status` row); `src/agenttalk/wrapper/recv_api.py` only if the scoped notice needs a hook (no change to `commit`); `src/agenttalk/lead_loop_cadence.py` (`relay_overdue`, failed-section reporting); `src/agenttalk/wrapper/prompt.py` (`_CADENCE_RULES`, the per-turn block); `docs/LOCK-ORDER.md` and its inventory test (the compaction acquisition and the routine record lock); `src/agenttalk/overview.py`, `src/agenttalk/web.py` (one field, one line); both lead skills, `docs/LEAD-GUIDE.md`, `docs/AGENT-MANUAL.md`, `CHANGELOG.md`; tests as above. Slice 2 adds `store.py` (the `lead_routine` key, validator, `init` carry including absent identities), `doctor.py`, `checkpoint.py`, the `lead-routine` command group and the manuals.

**Build order (binding for the build).**

1. **Step 0: a short spike, before any other code.** It checks the three load-bearing assumptions of this design against the tree, with throwaway code that is not part of the slice, and writes a one-page result. ESTIMATE: half a day. Each check has a pass criterion:
   - **Check 1, naming a result.** A helper around `_classify_event` (or `derive_threads`) names the terminal result of a thread the lead opened, for every opener kind and response status of the table in section 2, including `accepted` by response kind. Pass: the helper agrees with the table in every case, without changing `threads.py` behaviour.
   - **Check 2, printing the notice.** `drain`, `recv`, `wait` (global) and `wait --to-request` (scoped) can print an owed-relay block from their render step. Pass: the global cursor file and the thread-seen record are byte-identical before and after the print, in both modes. If the scoped CLI path cannot, the design says so (case 12c (b)) and the spike reports it; that alone is not a stop.
   - **Check 3, the metadata keys.** `relay_to`, `waiting_on`, `waiting_off`, `relay_of`, `relay_nonce` and `relay_outcome` go through the send path (`send --meta`, and `send_operation` for the relay identity) and come back unchanged, neither scrubbed nor rejected, with signing on and off.
   **If check 1 or check 3 fails, or check 2 fails for the global paths: stop and report to the lead before writing more code.**
2. **Then the slice,** in the order: derivation and states; the atomic operation and its crash and barrier tests; compaction protection and its locks; the notice on the consuming paths; reminders; the completion record.
3. **The build's first code read checks the five P2 contracts of the final read** (and the three P3 contracts after them): the `unresolved` outcome with compaction under the publication locks and both orders tested (section 5); the health contract (section 7; its table-driven test is slice 2, so the first read checks that slice 1 writes the completions it needs); the narrowed integrity promise with its three tests; the opt-out carry (slice 2 contract, checked as a written test plan); the restart demonstration split at the commit boundary (section 11, step 5). There is no further design read.
4. **Build kill signals** (from the challenge, adopted): (a) **a second mailbox reader turns out to be needed** to meet any target of section 6: stop, the target is withdrawn, not the rule; (b) **the barrier and crash tests of the relay operation cannot pass without a lock beyond rank 100** (the relay operation uses `state/operation-publication.lock` at rank 100 and, inside it, the existing message-publication mutex at rank 160, as `send_operation` already does; the compaction batch takes the same two; the routine record's leaf lock is outside the relay operation): stop and re-scope, and the estimate is void.

This document itself: `docs/DESIGN-lead-loop-default.md` only. No test opens it and it is not in the source package list (FACT: `tests/test_sdist_docs.py` requires only docs a test opens to be listed; the other `DESIGN-*.md` files are not listed either).

## 16. Build log: step 0 spike result

Run on the design head `25955780eba2c4fbbf413c4faf243ba76b0dd316` against the tree (throwaway scripts, not part of the slice).

- **Check 1, naming a result: PASS.** A helper that replays each thread the lead opened with `threads._classify_event` (the same ball-tracking loop as `derive_threads`) names the terminal result, and agrees with `derive_threads` in all 12 opener-kind and status cases of section 2 (`accepted` is not terminal for a `task-response` and is terminal for a `proposal-response`; `needs-info` is not terminal). Two findings for the build: `derive_threads` does not expose the terminal message, and its `closed` state depends on the reader's cursor (a result not yet read leaves the thread `reply-waiting`), so the build adds its own small public function next to `_classify_event` instead of reading `Thread.state`.
- **Check 2, printing the notice: PASS for the global paths and for `wait --to-request`.** A read-only notice appended after the command's own work in `drain`, `recv --ack`, `wait` and scoped `wait --to-request` left the stock output (ids and timestamps normalised), the exit codes, `<lead>.cursor` and `<lead>.threadstate.json` identical to a control run; it must be skipped under `--json` (the wrapper reads that output). The wrapper prompt path is built and tested in the slice.
- **Check 3, the metadata keys: PASS.** `relay_to`, `waiting_on`, `waiting_off`, `relay_of`, `relay_nonce` and `relay_outcome` come back unchanged through `Store.send`, `Store.send_operation` (second call returns the first message, sends nothing) and `agenttalk send --meta`, with signing off and with signing on. One finding: `send_operation` fixes the intent identity's `operation` label to `terminal` or `composing`, so the relay operation needs an `operation` parameter (default unchanged).
- **Kill signals so far:** no second mailbox reader is needed for any of the three checks; the lock question (barrier and crash tests) is open until those tests are written.

### Fix round 1 (read of 08ea4d35)

Six P2 and three P3 findings were fixed; the design text above was corrected where it was wrong (section 4 record, section 5 closing tuple and re-ask, section 7 records). Two statements belong to the contract and are repeated here.

- **The relay check is a snapshot, not a promise of currency (decision of the lead, finding P3-9).** `relay result` re-derives the result **inside** the publication lock (rank 100), so two relay operations exclude each other and a rerun returns the first message. An **ordinary reply does not take that lock**, so a reply that re-opens the request (for example an `accepted` task response) can overtake the check between the precheck and the send: the lead may then forward a decision for a result that was withdrawn a moment earlier. The lock order is not changed for this slice. The lead skill says so in plain words: forward the decision, and if the seat answers that the work was re-opened, the next derivation shows it.
- **Restart.** `tests/test_lead_relay_restart.py` runs a second wrapper instance on the same store after an interruption before the commit (the result is delivered again, one publication, the thread is seen afterwards) and after the commit (no replay, the hand-off is still owed because it is derived from the bus, one publication when closed).
