---
description: Enter the persistent spec-kitty implement/review loop for this mission as the claude agent. Use spec-kitty's state machine as source of truth; use agenttalk as a wake signal between two persistent CLI windows. Keeps your context across all WPs. Symmetric — claude can be implementer or reviewer for any given WP, whichever spec-kitty assigns.
argument-hint: "<mission-slug>"
---

# /agenttalk.sk-loop — Persistent spec-kitty loop with agenttalk wake

You are the **`claude`** agent in a two-window spec-kitty mission. The
peer agent (typically `codex`) is running `$agenttalk-sk-loop` in
another terminal at the same project root. spec-kitty's state machine
is the source of truth for what either of you should do next; agenttalk
is a wake signal that lowers latency between transitions.

**Roles are symmetric.** spec-kitty assigns implement vs review per WP
based on `.kittify/config.yaml` (`preferred_implementer`,
`preferred_reviewer`), the dependency graph, and rejection cycles. You
may be the implementer on one WP and the reviewer on the next. Do
whatever `spec-kitty next` tells you.

**You stay inside this loop for the entire mission.** Do not exit just
because one WP is done — only exit on mission completion, on
`kind=end`, or when the user explicitly stops you.

## Required argument

The user passes the mission slug as the argument (e.g. `034-auth-rewrite`).
If absent, ask them once, then continue. Hold it in a `$MISSION`
variable for the rest of the loop.

## The loop

```text
forever:
  1. Ask spec-kitty what to do next.
  2. If it's a real action for me  → do it, then wake the peer.
  3. If it's "wait"                 → block briefly on agenttalk.
  4. If mission is complete         → wrap up.
  5. On ambiguity / safety wall     → stop and ask the user.
```

### Step 1 — query spec-kitty

```powershell
spec-kitty next --agent claude --mission $MISSION --json
```

Parse the JSON. The interesting fields are usually `action`, `wp_id`,
`prompt_file`, `workspace`, and either a "decision" or "waiting"
marker. The returned action will be one of: `implement`, `review`,
`re-implement` (with cycle number), `arbiter`, `wait`, or a mission-
complete sentinel.

### Step 2 — if it's my action

Both `implement` and `review` are equally normal actions. Handle
whichever spec-kitty returns.

#### Action: implement WP##

1. Claim: `spec-kitty agent action implement WP## --mission $MISSION --agent claude:opus-4-7:implementer:implementer`
2. `cd` into the workspace path printed by the claim.
3. Read the prompt file. Execute the work (read existing code first,
   write code, write tests, run the project's validation command).
4. Commit: `git add -A; git commit -m "feat(WP##): <description>"`
5. Mark subtasks done: `spec-kitty agent tasks mark-status T001 T002 ... --status done`
6. Transition: `spec-kitty agent tasks move-task WP## --to for_review --note "Ready for review"`
7. **Wake the peer:**
   ```powershell
   agenttalk send --from claude --to codex --kind wake `
     --subject "WP## ready for review" `
     --meta mission=$MISSION --meta wp_id=WP## --meta new_lane=for_review --meta actor=claude `
     -m "WP## is in for_review. Run spec-kitty next."
   ```
8. Loop back to step 1.

#### Action: review WP##

1. Claim: `spec-kitty agent action review WP## --mission $MISSION --agent claude:opus-4-7:reviewer:reviewer`
2. `cd` into the workspace path printed by the claim.
3. Read the review prompt file. Run the diff commands inside it.
4. Verify acceptance criteria, owned_files boundary, dead-code check.
5. Issue verdict:
   - **Approve:** `spec-kitty agent tasks move-task WP## --to approved --note "Review passed: <summary>"`
   - **Reject:** write feedback to a temp file, then
     `spec-kitty agent tasks move-task WP## --to planned --force --review-feedback-file <path>`
6. **Wake the peer:**
   ```powershell
   agenttalk send --from claude --to codex --kind wake `
     --subject "WP## review verdict" `
     --meta mission=$MISSION --meta wp_id=WP## --meta new_lane=<approved|planned> --meta actor=claude `
     -m "WP## moved to <lane>. Run spec-kitty next."
   ```
7. Loop back to step 1.

#### Action: re-implement WP## (cycle N)

Same as implement, but the prompt file now includes the previous
reviewer's feedback. Address every blocker. On the 3rd cycle, surface
to the user before another round.

#### Action: arbiter WP##

Only emitted after 3 reject cycles. **STOP and ask the user.** Do not
auto-arbitrate.

### Step 3 — if spec-kitty says wait

```powershell
agenttalk wait --for claude --timeout 30
```

- Exit code 0 → peer sent a wake; loop back to step 1 immediately.
- Exit code 1 → timeout. Loop back to step 1 anyway (self-healing —
  poll catches state changes the peer made without sending a wake).

Use a **short** timeout (30s, not 120). The loop must interleave wake
listening with spec-kitty polling.

### Step 4 — mission complete

If `spec-kitty next` returns "mission complete" / "all approved" /
"merged":

1. If a merge step is still pending and is your action: run
   `spec-kitty merge --mission $MISSION` from the project root.
2. Otherwise: `agenttalk transcript --format md` and tell the user the
   path. Send `agenttalk end --from claude --reason "mission $MISSION complete"`.
3. Exit the loop.

### Step 5 — stop and ask the user when:

- spec-kitty returns an action you don't understand or mentions
  `arbiter` mode.
- A wake says one thing but `spec-kitty next` returns something
  contradictory (trust spec-kitty, but flag the inconsistency).
- Tests crash with infrastructure errors (missing tool, sandbox denial,
  network failure).
- Owned_files violation: the diff touches files outside the WP's
  owned_files list.
- The user types a question or new direction.

Print a brief summary of why you're pausing. After the user responds,
send a `kind=note` to the peer explaining the new plan, then resume.

## Hard rules

- **spec-kitty is the source of truth.** Never act on a wake message's
  body alone — always re-derive your action from `spec-kitty next`.
- **You own only your current transition.** Implementer moves `planned
  → in_progress → for_review`. Reviewer moves `for_review → approved`
  or `for_review → planned`. Don't cross-write into the other role's
  lane transitions.
- **Wakes are latency optimization, not state.** If a wake is lost, the
  next poll catches the change.
- **Persistent context is the whole point.** Don't suggest spawning
  subprocesses or fresh sessions — that defeats the design.
- **3 reject cycles = stop.** Always escalate to the user; never
  arbitrate yourself.
