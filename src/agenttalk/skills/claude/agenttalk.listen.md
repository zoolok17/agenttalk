---
description: Enter listen mode as the claude agent — repeatedly wait for messages from the peer (typically codex) and handle them. Used when claude is the passive party — waiting for review requests, questions, wake signals, or cross-review of work the peer just finished.
---

# /agenttalk.listen — Listen for peer messages

You are the **`claude`** agent. Your peer (typically `codex`) is in
another terminal. Use this skill when you are the passive party —
waiting for review requests, cross-reviews of work the peer just did,
questions, or wake signals.

The loop is **reentrant**: after handling any message, immediately wait
for the next one. Stay in listen mode until you receive `kind=end` or
the user explicitly stops you.

## The loop

```powershell
agenttalk wait --for claude --timeout 30
```

- exit 0: a message was received and printed. Classify and handle it
  (see below), then loop back.
- exit 1: timeout (no new messages in 30s). Loop back immediately. Do
  NOT return control to the user just because the poll window expired.

Use a **short** timeout (30s, not 120). The short window lets you
interleave handling of incoming messages with waiting for replies to
requests you've sent.

## Message classification

| kind | handling |
| --- | --- |
| `review-request` | Mode-detect (see "Review request handling" below). |
| `review-result`  | Verdict on a request **you** sent. Match by `meta.request_id`. Act on verdict (ship, iterate, surface to user). |
| `question`       | Answer directly via `agenttalk send --from claude --to codex --kind message -m "<answer>"`. |
| `wake`           | State-change signal (typically from sk-loop). Re-derive your action from the authoritative source (e.g. `spec-kitty next`). Never act on the wake body alone. |
| `message` / `note` | Acknowledge with a one-line reply only if it asks for one. |
| `end`            | Exit the loop. Run `agenttalk transcript --format md` and surface the path. |

## Review request handling — mode detection

When you receive `kind=review-request`, check `meta`:

### Spec-kitty mode — `meta.mission` or `meta.wp_id` present
1. Run the review per `/spec-kitty.review` against the WP at the named
   feature dir.
2. Verify owned_files boundary, dead-code check, acceptance criteria.
3. Send `kind=review-result` with:
   - `--meta status=approved|rejected`
   - `--meta request_id=<echoed>` if it was present
   - `--meta wp_id=<echoed>`

### Ad-hoc cross-review mode — mission/wp_id absent
The peer just finished a chunk of organic split work and wants you to
review it. Procedure:

1. **Parse the body.** It should contain sections: **Goal**, **Files
   changed**, **How to verify**, **Focus areas**, **Known caveats**.

2. **Verify scope.**
   - If `meta.base_sha` and `meta.head_sha` are present, run
     `git diff --name-only <base_sha>..<head_sha>` and compare with the
     declared file list. If the real diff exceeds it, note this in
     your review.
   - If no commits exist (working-tree changes only), inspect
     `git status --short` and stage diffs. Mark scope as
     "working-tree based, unverified" in your reply.
   - If neither is available and you can't determine what to review,
     send a `kind=message` asking for clarification first.

3. **Review read-only.** Do NOT modify the peer's files. If you spot
   bugs, name them in the review-result body — do not patch them
   silently. Mixing review and implementation across ownership
   boundaries loses accountability.

4. **Send the verdict** via `kind=review-result`:
   - `--meta status=approved|rejected|needs-info`
   - `--meta request_id=<echoed>`
   - Body shape:
     - **Findings** — ordered by severity, with file/line refs
     - **Verification performed** — commands you ran, output checks
     - **Residual risks / scope limits**
     - If `approved`, state explicitly "no blocking findings".

## When two requests collide

If a `review-request` arrives while you are waiting for a
`review-result` to one of YOUR own requests:
- Handle the OLDER `request_id` (or older message timestamp) first.
- After replying, resume waiting for your own outstanding result.
- This deterministic ordering avoids both sides blocking forever.

## When to break the loop and ask the human

- A request would require modifying files outside any reasonable scope
  (security-sensitive, infrastructure config, secrets).
- The peer contradicts something the user said earlier this session.
- Unresolvable error (tests crash with infra problems, missing tool,
  sandbox denial).
- You've been ping-ponging on the same scope for 3+ iterations.

Print why you're pausing; wait for the human. After they respond, send
a `kind=note` to the peer with the new plan and resume.

## Exiting

The loop ends when:
- The peer sends `kind=end` (graceful shutdown).
- The user clearly says "stop listening".

On exit: `agenttalk transcript --format md` and tell the user the saved
path.
