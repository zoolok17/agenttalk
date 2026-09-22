# Increment A — task-kind draft channel

Branch `fix/wrapper-reply-channels`, base `origin/master` at `c6235a1`. Fixes field fact 1 of the
work order: `src/agenttalk/wrapper/loop.py`'s `_REPLY_DRAFT_KINDS = {question, message, wake}`
never decorated a `kind=task` inbound record with `record["reply_draft"]`, so
`_deliver_reply_draft` returned `None` at its first line (`declared = record.get("reply_draft")`
is `None`) and the turn still committed clean — a child that wrote its answer to the deterministic
draft path out of habit from a message-kind turn had its reply silently dropped, with no signal of
failure anywhere in the turn's own disposition.

## The fix

- `_REPLY_DRAFT_KINDS` now includes `"task"` (`loop.py`). `review-request`/`proposal` stay excluded
  — unchanged, their typed responses need evidence/status meta the draft channel still cannot
  carry (see `gates.validate_review_result_evidence`).
- `reply_transport.deliver_draft_reply` no longer hardcodes `kind = "message"`; it now calls the
  new `reply_transport.draft_reply_kind_for(record.get("kind"))`, which maps `"task" ->
  "task-response"` and defaults every other kind (question/message/wake) to `"message"`,
  unchanged. Correlation echo (`echo_reply_correlation`) already takes `kind` as a parameter and
  was untouched — `task-response` is not a thread-opening kind
  (`_THREAD_OPENING_REPLY_KINDS = (review-request, proposal)`), so it inherits `request_id` /
  `broadcast_id` from the anchor exactly as a CLI `agenttalk reply --kind task-response` would.
  Subject is empty on both paths already (`deliver_draft_reply`'s own hardcoded `subject=""`,
  matching `cmd_reply`'s own comment "byte-parity with a CLI-path reply").
- New supersede path in `loop._deliver_reply_draft`: when `landed_reply_exists` is true (a CLI
  reply for the same thread already published before the wrapper's end-of-turn check ran) AND the
  inbound record's kind is `"task"`, the draft is **left in place at its live path** and a new
  `<id>.superseded.md` sidecar (`reply_transport.write_superseded_note`) is written beside it,
  instead of the existing silent `draft.unlink()` that question/message/wake still get unchanged.
  Rationale: a silently deleted draft on this specific race would erase the only trace that two
  channels almost double-posted a task-response on the same thread — worth surfacing, unlike the
  ordinary (non-obligatory) freeform-reply case the existing silent unlink already correctly
  handles.

## Enumerated readers — `record["reply_draft"]`

Grepped `"reply_draft"` (record-key, not the `reply_draft_path()`/`_with_reply_draft`/
`_deliver_reply_draft` function names) across `src/agenttalk/`:

1. **`wrapper/loop.py:237`** (`_with_reply_draft`) — the sole WRITER. Unaffected by this change's
   own widening: it decorates whatever `_REPLY_DRAFT_KINDS` now includes, no reader-side branch on
   value shape changes (`{"path": str}` is unchanged).
2. **`wrapper/loop.py:~320`** (`_deliver_reply_draft`) — the sole in-process READER besides the
   prompt renderer below. Assumes `declared` is a dict with a `"path"` key when present; both
   assumptions hold unchanged for task-kind records (same decoration function, same shape).
3. **`wrapper/prompt.py:208-209`** (`assemble_turn_prompt`) — renders the "HOW TO REPLY: PREFERRED
   DRAFT CHANNEL" prose block whenever `record.get("reply_draft")` is a dict with a `"path"`. This
   is the ONLY other reader. Assumes: (a) the field is either absent or a dict shaped
   `{"path": str}` — unchanged; (b) presence of the field means the draft channel is a VALID way to
   answer THIS message — now newly true for `task`, which is exactly the fix's own intent (the
   child previously saw no draft-channel offer at all for a task inbound, per this session's own
   observation: a `kind=task` message's rendered prompt went straight from the JSON record to "==
   HOW TO REPLY TO THIS MESSAGE (exact form) ==" with no "PREFERRED DRAFT CHANNEL" section). No
   readers outside `src/agenttalk/` were found (grepped `tests/` separately — only assertions on
   the same two functions, covered by the new tests in `tests/test_reply_draft_delivery.py`).

No reader was found that assumes `record["reply_draft"]` is ABSENT for `kind=task` specifically —
the exclusion was structural (kind not in the set), not a branch anyone else keyed on. Widening the
set is therefore safe with respect to every existing reader; nothing needed updating beyond the
`_REPLY_DRAFT_KINDS` membership and the kind-selection change in `deliver_draft_reply` itself.

## Enumerated consumers — `task-response` replies

Grepped `"task-response"` across `src/agenttalk/` (excluding the CLI's own `cmd_reply`/
`cmd_task`/help text, which already accept it as an ordinary typed kind):

1. **`gates.py:44,49`** — `RESPONSE_STATUS_ENUMS["task-response"] = {accepted, declined, done}`
   and `TERMINAL_RESPONSE_STATUSES["task-response"] = {declined, done}`. Consumed by
   `validate_response_status` (called inside `deliver_draft_reply`, already shared with the CLI
   path): a **missing** `status` is explicitly a no-op ("missing status remains readable... not a
   terminal thread event") — a draft-published task-response (no typed meta support at all in the
   draft channel) passes this validator with no error. Assumption held: this validator never
   REQUIRES status, it only constrains a PRESENT one.
2. **`doctor.py:1022`** — a health check filtering `m.kind == "task-response" and
   meta.get("status") == "declined"` (surfaces declined lead tasks). A status-less
   draft-published task-response simply never matches this filter — same as a bare CLI
   `agenttalk reply --kind task-response` with no `--meta status=...` already produces today.
   No behavior change, no new gap.
3. **`threads.py:100-104`** (`_classify_event`, the lead-side thread/ball-tracking view) — for
   `opener_kind == "task"`: a `task-response` with `meta.status == "accepted"` moves the ball back
   to the responder (still owed, acknowledged); one in `TERMINAL_RESPONSE_STATUSES["task-response"]`
   (`declined`/`done`) closes the thread; **any other status value, including ABSENT, returns
   `None`** — meaning the event is not classified as advancing the thread AT ALL. **This is the one
   real, worth-flagging limitation of this increment**: a task-response published via the draft
   channel can never carry `status` (the draft channel has no typed-meta support), so the
   lead-side thread view will keep showing the thread as still-owed-with-no-progress even after
   the reply visibly lands on the bus (readable via `agenttalk recv`/`threads` message list, just
   not ball-tracked). This is NOT a new gap this increment introduces — a bare CLI
   `agenttalk reply` to a task thread with no `--kind`/`--meta` (which is what this session's own
   task replies used throughout, prior to this fix) publishes as plain `kind="message"` and is
   **equally** invisible to `_classify_event`'s `opener_kind == "task"` branch (which only matches
   `m.kind == "task-response"` in the first place) — so the draft channel's status-less
   task-response is a strict IMPROVEMENT (recognized as a task-response at all, just not yet
   ball-tracked) over what a habit-driven plain CLI reply already produced. Not fixed in this
   increment (the draft channel has no meta-passing mechanism to fix it with); flagged rather than
   silently accepted.
4. **`store.py:540-566`** (`KNOWN_KINDS`, documentation comment only) — states the CANONICAL
   contract: "`task-response`... carrying `meta.status=accepted|declined|done` like the
   review-result/proposal-response pairs already do." This increment's draft-published
   task-response does not meet that documented ideal (see point 3) — same caveat, restated at its
   source of truth. No code in `store.py` itself enforces or reads this beyond the frozenset
   membership check at write time (`Store.send`'s own kind-known check, unaffected).
5. **`wrapper/obligations.py`** (the owed-action ledger) — grepped for any `"task"`-specific
   logic: none found. The ledger treats task threads generically through `OPENER_KINDS`
   (`store.py`) and `threads.py`'s own classification (point 3 above) — no separate assumption to
   enumerate.

## Increment A2 (reviewer-3 cold-review HOLD, fixed in-round)

reviewer-3's own cold read of A2 confirmed all of A's own reported counts, then drove the FULL
pipeline (task sent, a wrapper turn that only writes the draft, then `threads.derive_threads` from
BOTH the sender's and the responder's own perspective) and found this: the thread stayed
`open-outbound` for the sender and `owed-inbound` for the responder, and `doctor.py`'s staleness
check raised a FALSE `ERROR "neither accepted, declined, nor done"` 30 minutes after a task was
already answered through the very channel A fixes. This is the exact limitation point 3 above
flagged — "the lead-side thread view will keep showing the thread as still-owed-with-no-progress" —
reviewer-3's own run gave it a name and a blast radius (a false doctor ERROR) severe enough that the
lead decided to fix it in-round rather than carry it forward as a residual.

**The fix**: `reply_transport.deliver_draft_reply` now sets `meta["status"] = "done"` whenever the
resolved outbound `kind == "task-response"` (i.e. whenever the inbound record's own kind is
`"task"`) — built right after `echo_reply_correlation`, before the digest/nonce (confirmed
`operation_payload_digest` does not cover `status` at all, so this ordering has no digest-parity
consequence). Rationale, stated plainly: a seat that answers a task by writing its draft is
declaring the task **done** — that is the channel's own semantics. The draft channel carries no
typed `--meta` at all, so it has no way to express "accepted, still working" (`status=accepted`,
which keeps the ball with the responder) — a seat that needs that shape must use the CLI path
instead (`agenttalk reply --kind task-response --meta status=accepted`), unaffected by this change
(A2 only touches the draft-channel function, never `cmd_reply`).

**Enumerated readers of `task-response`'s `meta.status`**, exactly as the lead's own HOLD asked:

1. **`threads.py:100-104`** (`_classify_event`, `opener_kind == "task"`) — the root cause reviewer-3
   found. `status` absent -> returns `None` (UNCLASSIFIED, not merely "non-terminal" — neither
   `("ball", responder)` nor `("terminal", None)`), which is why the thread state computation never
   even reached its own terminal-vs-ball branching for a status-less reply. `status="done"` (now
   the draft-channel default) -> `("terminal", None)`, exactly the same as an equivalent CLI
   `--meta status=done` reply already produced. `status="accepted"` (CLI-only, A2 does not produce
   this) -> `("ball", responder)`, the "acknowledged, still on the hook" shape — untouched.
2. **`doctor.py:995-1049`** (`_check_declined_lead_tasks`, the staleness check reviewer-3's run
   triggered) — flags a thread where, from the OPENER's own `derive_threads` view,
   `state in ("open-outbound", "reply-waiting")` AND `age_seconds >= 1800`. Before A2: a status-less
   task-response left the opener's view at `open-outbound` forever (see point 1), so ANY task
   answered only through the draft channel would eventually trip this exact false ERROR, precisely
   as reviewer-3 reproduced. After A2: `_classify_event` reaches `terminal=True`, and the opener's
   state becomes `reply-waiting` (fresh, unread reply — see the test below) or `closed` (once read)
   — NEITHER is `open-outbound`, and `reply-waiting` only re-triggers staleness if the reply itself
   sits unread for 30+ minutes (a genuinely separate, correct concern: an unread reply IS worth
   surfacing eventually, same as it always was for every other kind).
3. **`gates.py:41-49`** (`RESPONSE_STATUS_ENUMS`/`TERMINAL_RESPONSE_STATUSES`) — `"done"` was
   already a member of both sets (A's own enumeration already noted this); A2 does not add or
   change any enum membership, it only makes the draft channel actually SET the field.
   `validate_response_status` (already shared with the CLI path via `deliver_draft_reply`) accepts
   `status="done"` for `kind="task-response"` with no error — confirmed by the new passing test.
4. **`store.py:540-566`** (`KNOWN_KINDS`'s own documentation comment) — states the canonical
   contract task-response replies "should" carry `meta.status=accepted|declined|done`. A2 makes the
   draft channel's own output MEET that documented contract for the first time (previously it met
   neither `accepted` nor `declined` nor `done` — it carried no status key at all); no code change
   needed here, the comment was already correct, only the draft channel's own behavior was not
   living up to it.
5. **The lead-side thread view** (`cli.py:1287`, `cmd_threads` — "the did-the-reviewer-ever-respond
   answer") — calls the exact same `threads.derive_threads` as points 1/2 above, with no
   task-specific logic of its own; its default view hides `closed` threads and shows everything in
   `ACTIONABLE_STATES = (reply-waiting, owed-inbound, open-outbound)`. Before A2 a draft-answered
   task would show as `open-outbound` in this view FOREVER (indistinguishable from "never
   answered"); after A2 it shows as `reply-waiting` (an unread reply exists — correct, actionable:
   "here is an answer to read") until the lead's own cursor advances past it, then it drops off the
   default view entirely like any other answered thread.

**Test** (`tests/test_reply_draft_delivery.py::test_a2_task_thread_closes_on_both_sides_and_doctor_raises_nothing`,
41 lines): drives a real task through `loop.run_loop` with a draft-only responder (reviewer-3's own
repro shape), then calls `threads.derive_threads` from BOTH sides directly and
`doctor._check_declined_lead_tasks` directly (not synthesized) — confirms the opener's own view is
`reply-waiting` (not `open-outbound`), the responder's own view is `closed` (not `owed-inbound`),
the opener's view becomes `closed` too once its cursor advances past the reply, and the doctor
check returns `None` (nothing to report). Also extended the existing
`test_task_kind_gets_the_draft_channel` with a direct `meta.get("status") == "done"` assertion on
the published message. Full run: `test_reply_draft_delivery.py` 43/43 (was 42, +1 new +1 extended),
`test_doctor.py -k "declined_lead_tasks or task_response"` 3/3, `test_threads.py -k task` 5/5.
`ruff check` + `py_compile` clean.

## Confidentiality sweep

`grep -riE "amperian|jaws"` over this file and `git diff origin/master` for this increment - 0
hits. This repo (`agenttalk`) is public on GitHub; no client name, credential, or hostname belongs
in any of its tracked content regardless of hit count, and none is present.
