# DESIGN #164 — Interim progress notes that don't end a wrapped turn

Status: PR-1 (this design, shipped) — the CLI-direct progress verb. PR-2
(file-draft poller for shell-less seats) is an explicit, separate fast-follow,
not started here. Author: claude-agenttalk-developer-2 · 2026-09-16, reviewed
by reviewer-3 (GO WITH CHANGES).

## Problem

A wrapped turn ends once the model sends its correlated bus reply and then
stops generating — the model's own "then stop" protocol instruction, not a
hard wrapper mechanism. If that turn is still doing real work (a long test
bar, polling CI, a multi-step build) when the seat posts a status update via
`agenttalk reply`, the reply satisfies the protocol's "you're done" signal,
the model stops, and any of that turn's OWN background subprocesses die with
the CLI session. An hour-long test bar died this way on 2026-09-15 22:46Z;
two seats repeated it on 2026-09-16 while polling CI. The lead had no
progress signal for a long turn beyond the heartbeat either way.

## Two additive channels, bare "reclassify the reply" rejected

Two designs were on the table:

- **(A) a distinct advisory channel** the seat can use mid-turn without it
  being mistaken for the answer.
- **(B) a wrapper rule** that a turn continues after an "interim" reply
  until an explicit "final" one.

(B) was rejected: it requires every reply sender (every existing skill, every
seat's muscle memory) to correctly flag a reply as interim-vs-final via a new
meta key. A single missed flag either silently reclassifies a real final
answer as interim (the turn server-side-never-ends — worse than today's
failure, where at least the model simply stops early) or loses the
distinction the other way. It also collides with the single-consumer
landed-check invariant (`reply_transport.landed_reply_exists`,
`reply_transport.py`) that assumes exactly one reply satisfies one inbound id.

(A) was adopted: a NEW message kind can never be mistaken for the answer
because nothing that decides "did the answer land" ever inspects it.

## Mechanism (this PR): a live CLI verb, modeled on `composing`

`agenttalk progress --from <seat> --to-id <id> -m "..."` (`cli.py`
`cmd_progress`, mirroring `cmd_composing` at `cli.py:1732`):

- Publishes via a plain `store.send()` (not `send_operation`'s idempotent
  digest path) — a progress note is advisory and repeatable by design, unlike
  a reply or a composing ping bound to one thread.
- `kind="progress"` is a new `KNOWN_KINDS` member (`store.py`) and joins
  `composing` in `CONTROL_KINDS` — required for the exact reason `composing`
  is there: `threads.py`'s question-opener rule closes a thread on ANY
  non-control response flowing responder → requester, so without this a
  progress note addressed back to a question's asker would be misread as the
  terminal answer. Verified directly (`test_progress_note_never_closes_a_
  question_thread`, `tests/test_threads.py`): a progress note carrying the
  SAME `request_id` as an open question leaves the thread `open-outbound`/
  `owed-inbound` unchanged; a genuine reply on the same thread flips it.
- Deliberately never echoes `in_reply_to`, `request_id`, or `broadcast_id` —
  the meta a real reply/composing ping uses for correlation. A `progress_for`
  meta key (a plain id pointer, never consulted by any correlation/dedupe/
  thread logic) is set instead, used only by `status`'s render. This means a
  progress note structurally cannot satisfy
  `reply_transport.landed_reply_exists` (verified directly) even before
  `CONTROL_KINDS` membership is considered.
- Unlike `composing --to-request` (refused unless the sender's thread state
  is `owed-inbound`), `--to-id` is UNGATED: the only requirement is that the
  id names a validated message addressed to the sender. A progress note is
  not a claim of obligation the way a composing ping is.
- `recv` hides it from the default inbox view and `wait` never returns it as
  an answer — free, by `CONTROL_KINDS` membership, same as `composing`.

Why a live CLI call needs no wrapper change to avoid ending the turn: a
wrapped seat already runs `agenttalk composing`/`send`/`escalate` mid-turn
today without ending it (see the standing "You MAY SEND" list in
`wrapper/prompt.py`'s `_DEFAULT_RULES`) — a live bus subprocess call never
itself ends a turn; only the model choosing to stop generating does. This PR
adds a new rule paragraph to that same block telling the model explicitly
that `agenttalk progress` does NOT mean "stop" the way `agenttalk reply` does
— see `_DEFAULT_RULES`'s new "LONG-RUNNING WORK" paragraph, mirrored into
both listen skills (`agenttalk.listen.md` / codex `agenttalk-listen/
SKILL.md`) and enforced in lockstep by `tests/test_skill_lint.py`'s
`SKILL_INVARIANTS`.

## Health: reason code + timestamp only, never note text

`WrapperHealthWriter`'s schema (`health.py`) is explicitly, deliberately
content-free: "no message body, model output, prompt, tool command, or tool
output can be represented here" (`build_snapshot`'s own docstring). A
progress note's TEXT therefore never goes into `health.json` — it lives only
in the bus message itself, exactly like every other send/composing ping
already visible in the transcript/dashboard.

What DOES go into health.json: `health.stamp_progress(raw, *, now=None)`, a
pure function called directly by the one-shot `cmd_progress` CLI process
(there is no live `WrapperHealthWriter` instance for a plain subprocess
invocation to reuse — that class holds in-memory state for one wrapper
process's whole lifetime). It reads the agent's CURRENT on-disk snapshot and,
only when its `state` is already a working one (`STATE_WORKING_TURN` /
`STATE_WORKING_SILENT` — it never fabricates a working state), returns an
updated snapshot with `reason_code="progress_note"` (`REASON_PROGRESS_NOTE`)
and a fresh `last_progress_at`, preserving `state`/`since`/`request_id`/
`msg_id` unchanged. This is distinct from the PRE-EXISTING
`reason_code="progress_event"` (`wrapper/health.py`'s `event()`, driven by
raw adapter stream activity — tool calls, model deltas, `wrapper/events.py`
`PROGRESS_EVENTS`) — that is a liveness signal ("the CLI adapter is doing
something"); `progress_note` means "the seat told the lead something."
`cmd_progress` calls this best-effort (`try`/`except Exception: pass`) after
the actual send succeeds, so a health-stamp failure never fails the note
itself.

## Status: read the message, not health.json

`_gather_status` (`cli.py`) already does ONE shared validated-message scan;
this PR adds a `by_sender` grouping alongside the existing `by_recipient`
one and a `_last_progress_note_for(agent_msgs, health)` helper: while an
agent's health state is a working one, it finds the most recent `kind=
progress` message that agent SENT no earlier than the turn's own `since`,
and surfaces `{text, at, age_seconds}` — resolved-aware, same convention as
#162's `reply-refused: N` summary (the note stops showing once the agent
goes idle again, verified by `test_status_progress_note_absent_once_idle`).
Human `status` gets one appended fragment per agent line:
`progress="<first line, 60 chars>" (<age>)`; `status --json` gets a
`last_progress_note` object on that agent's row. `doctor` is UNCHANGED,
deliberately: a stale `in_progress_since` (health's own `since`) with a
FRESH heartbeat just means "still working, all fine"; stale+stale is already
covered by the existing heartbeat-staleness → supervisor-relaunch path. No
new doctor check earns its complexity here.

## Refusal behavior (#162 compatibility, decided)

A progress-note publish failure (a `store.send()` exception, e.g. a lock
timeout) is swallowed best-effort inside `cmd_progress` and never surfaces
anywhere else — NOT wired into `reply_refusals.py`, NOT a dead letter, NOT a
doctor WARN. Reasoning: a refused REPLY (#162's actual scope) is a LOST
ANSWER the requester/lead must recover by hand; a refused progress note is
non-authoritative and ephemeral — the turn's real completion runs through
the completely unchanged reply-draft/commit path regardless of whether any
progress note ever landed, and the next note or the final reply supersedes a
lost one at no cost.

## Deferred: channel (b), the file-draft poller

A file-based progress-note channel for shell-less seats (mirroring the
reply-draft file channel `DESIGN-201` built for the same population) would
decorate the record with a `progress_note` path the way `_with_reply_draft`
does for `reply_draft`, and would need a NEW wrapper-side poller thread
(started before `drive()`, joined after, polling at the existing
`heartbeat_interval` cadence) to publish it LIVE, since `drive()` is a single
blocking call. This is the one genuinely new concurrency pattern the design
review flagged — the wrapper has never published anything WHILE `drive()`
is in flight, only before/after. Deferred to its own PR and its own
adversarial read, since the motivating incidents (the test bar, CI polling)
all involved CLI-capable seats that channel (a) already covers.

## Tests

- `tests/test_threads.py`: a progress note carrying an open question's real
  `request_id` never closes it; a progress note opens no thread of its own.
- `tests/test_cli.py`: happy path (kind, correlation meta, no `in_reply_to`/
  `request_id`); ungated `--to-id` (no owed-inbound gate, unlike
  `composing`); refused `--to-id` when not addressed to the sender; never
  satisfies `landed_reply_exists`; hidden from `recv`'s default view; health
  stamping happens and carries no note text; health stamping is a no-op when
  idle; `status` text/json render presence while working and absence once
  idle.
- `tests/test_wrapper_health.py`: `health.stamp_progress` unit tests —
  annotates a working snapshot, works for both working states, is a no-op
  outside a working state, is a no-op for missing/malformed input.
- `tests/test_skill_lint.py`: the new "Long-running work" rule text and the
  exact `agenttalk progress --from $SELF --to-id` invocation are required on
  BOTH the Claude and Codex listen skills.
- `tests/test_store.py`: `CONTROL_KINDS`'s literal-assertion regression
  guard consciously updated to include `progress`.

## Rollout

Additive: no existing subcommand, kind, meta convention, or health field
changes shape. A fleet running the previous prompt text is unaffected (it
simply never mentions `agenttalk progress`); a fleet on the new prompt text
gains the option without any change to how replies/composing/send already
work.
