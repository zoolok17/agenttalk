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

## Confidentiality sweep

`grep -riE "amperian|jaws"` over this file and `git diff origin/master` for this increment - 0
hits. This repo (`agenttalk`) is public on GitHub; no client name, credential, or hostname belongs
in any of its tracked content regardless of hit count, and none is present.
