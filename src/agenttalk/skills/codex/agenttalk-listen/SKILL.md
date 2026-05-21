---
name: agenttalk-listen
description: Enter listen mode as a Codex agent — repeatedly wait for messages from the peer and handle them. Used when this side is the passive party — waiting for review requests, questions, wake signals, or cross-review of work the peer just finished.
---

# agenttalk-listen — Listen for peer messages (codex side)

You are running as a **Codex** agent. Your peer is in another
terminal. Use this skill when you are the passive party — waiting for
review requests, cross-reviews of work the peer just did, questions,
or wake signals.

The loop is **reentrant**: after handling any message, immediately
wait for the next one. Stay in listen mode until you receive
`kind=end` or the user explicitly stops you.

## Identity

```bash
SELF="${AGENTTALK_SELF:-codex}"
PEER="${AGENTTALK_PEER:-claude}"
```

Always resolve inside your current shell — env from prior tool calls
does not persist across separate tool-call processes.

## Splitting implementation work with the peer

**If the peer proposes (or asks you to coordinate) a split of
implementation work outside a spec-kitty mission, do NOT proceed
without first asking the user.** The user invoked you to do work; you
should not silently divide it with the peer.

If the user explicitly approves a split:

1. **Confirm ownership boundaries.** Acknowledge who owns which
   files/tasks and who reviews which piece.
2. **Every implemented piece MUST receive a `kind=review-request`
   cross-review** before either of you calls the work done. This is
   the whole point of running two agents.
3. **Reviews are read-only.** Do not modify the peer's files.

In a spec-kitty mission, ignore this — spec-kitty's state machine
already assigns implement/review responsibilities per WP.

## The loop

```bash
agenttalk wait --for "$SELF" --timeout 1800
```

- exit 0: a message was received and printed. Classify and handle it,
  then loop back.
- exit 1: timeout (no new messages in 30 min). Loop back immediately
  as a liveness safety net. Do NOT return control to the user.

Use a **long** timeout (1800s). The `agenttalk wait` subprocess polls
the filesystem internally (~0.3s) so real messages still return
immediately; only the *idle* case gets cheaper. Each premature return
to the LLM costs tokens re-reading conversation context, so short
timeouts in pure listen mode are pure waste. sk-loop uses a short
timeout because it also polls `spec-kitty next` — pure listen has no
other source to interleave with.

## Message classification

| kind | handling |
| --- | --- |
| `review-request` | Mode-detect (see "Review request handling" below). |
| `review-result`  | Verdict on a request **you** sent. Match by `meta.request_id`. Act on verdict. |
| `question`       | If `meta.consult=true`, follow "Consult handling" below. Otherwise answer directly via `agenttalk send --from "$SELF" --to "$PEER" --kind message -m "<answer>"`. |
| `wake`           | State-change signal (typically from sk-loop). Re-derive your action from the authoritative source. Never act on the wake body alone. |
| `message` / `note` | Acknowledge with a one-line reply only if it asks for one. |
| `end`            | Exit the loop. Run `agenttalk transcript --format md` and surface the path. |

## Review request handling — mode detection

When you receive `kind=review-request`, check `meta`:

### Spec-kitty mode — `meta.mission` or `meta.wp_id` present
1. Run the review per spec-kitty's review workflow against the WP at
   the named feature dir.
2. Verify owned_files boundary, dead-code check, acceptance criteria.
3. Send `kind=review-result` with:
   - `--meta status=approved|rejected`
   - `--meta request_id=<echoed>` if present
   - `--meta wp_id=<echoed>`

### Ad-hoc cross-review mode — mission/wp_id absent
The peer just finished a chunk of organic split work and wants you to
review it.

1. **Parse the body.** Goal, Files changed, How to verify, Focus
   areas, Known caveats.

2. **Verify scope.**
   - If `meta.base_sha` and `meta.head_sha` are present, run
     `git diff --name-only <base_sha>..<head_sha>` and compare with
     the declared file list. Note any excess in your review.
   - If no commits exist (working-tree only), inspect `git status
     --short` and stage diffs. Mark scope as "working-tree based,
     unverified" in your reply.
   - If neither is available and you can't determine what to review,
     send a `kind=message` asking for clarification first.

3. **Review read-only.** Do NOT modify the peer's files.

4. **Send the verdict** via `kind=review-result`:
   - `--meta status=approved|rejected|needs-info`
   - `--meta request_id=<echoed>`
   - Body: Findings (ordered by severity, with file/line refs),
     Verification performed, Residual risks. If approved, state
     explicitly "no blocking findings".

## Consult handling

When you receive `kind=question` with `meta.consult=true`, the peer
is asking you to pressure-test their draft answer to a user question
(see `$agenttalk-consult` for the sender's side).

Procedure:
1. Read the body: it should contain `## User question / constraints`,
   `## My draft answer`, `## What I'm uncertain about`, `## Requested
   response shape`.
2. **Attack the draft, don't endorse it.** Look for:
   - Blocking objections (correctness, security, data-loss).
   - Missing assumptions.
   - A meaningfully different recommendation.
3. Reply with `kind=message` (NOT `kind=question` — that would loop):
   ```bash
   agenttalk send --from "$SELF" --to "$PEER" --kind message \
     --subject "consult reply" \
     --meta request_id=<echoed> --meta consult=true --meta round=<echoed> \
     -m "<your critique>"
   ```
4. End your reply with one of: `agree`, `disagree`, `qualified-agree`.
5. **Do NOT modify project files.** Consult is advisory.
6. **Do NOT answer the user directly.** The initiating agent owns
   the final answer.
7. **Do NOT start your own consult in return.** That's a loop.

## When to break the loop and ask the human

- A request would require modifying files outside any reasonable
  scope (security-sensitive, infrastructure config, secrets).
- The peer contradicts something the user said earlier this session.
- Unresolvable error (tests crash with infra problems, missing tool,
  sandbox denial).
- You've been ping-ponging on the same scope for 3+ iterations.

Print why you're pausing; wait for the human. After they respond,
send a `kind=note` to the peer with the new plan and resume.

## Exiting

The loop ends when:
- The peer sends `kind=end` (graceful shutdown).
- The user clearly says "stop listening".

On exit: `agenttalk transcript --format md` and tell the user the
saved path.
