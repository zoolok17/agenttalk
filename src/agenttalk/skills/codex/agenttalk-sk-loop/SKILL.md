---
name: agenttalk-sk-loop
description: Enter the persistent spec-kitty implement/review loop for this mission as the codex agent. Use spec-kitty's state machine as source of truth; use agenttalk as a wake signal between two persistent CLI windows. Keeps your context across all WPs. Symmetric — codex can be implementer or reviewer for any given WP, whichever spec-kitty assigns.
---

# agenttalk-sk-loop — Persistent spec-kitty loop with agenttalk wake (codex side)

You are the **`codex`** agent in a two-window spec-kitty mission. The
peer agent (typically `claude`) is running `/agenttalk.sk-loop` in
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
If absent, ask them once, then continue. Hold it in a `$MISSION` shell
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

```bash
spec-kitty next --agent codex --mission "$MISSION" --json
```

Parse the JSON. Interesting fields: `action`, `wp_id`, `prompt_file`,
`workspace`, and either a "decision" or "waiting" marker. The returned
action will be one of: `implement`, `review`, `re-implement` (with
cycle number), `arbiter`, `wait`, or a mission-complete sentinel.

### Step 2 — if it's my action

Both `implement` and `review` are equally normal actions. Handle
whichever spec-kitty returns.

#### Action: implement WP##

1. Claim: `spec-kitty agent action implement WP## --mission "$MISSION" --agent codex:gpt-5.5:implementer:implementer`
2. `cd` into the workspace path printed by the claim.
3. Read the prompt file. Execute the work (read existing code first,
   write code, write tests, run the project's validation command).
4. Commit: `git add -A && git commit -m "feat(WP##): <description>"`
5. Mark subtasks done: `spec-kitty agent tasks mark-status T001 T002 ... --status done`
6. Transition: `spec-kitty agent tasks move-task WP## --to for_review --note "Ready for review"`
7. **Wake the peer:**
   ```bash
   agenttalk send --from codex --to claude --kind wake \
     --subject "WP## ready for review" \
     --meta mission="$MISSION" --meta wp_id=WP## --meta new_lane=for_review --meta actor=codex \
     -m "WP## is in for_review. Run spec-kitty next."
   ```
8. Loop back to step 1.

#### Action: review WP##

1. Claim: `spec-kitty agent action review WP## --mission "$MISSION" --agent codex:gpt-5.5:reviewer:reviewer`
2. `cd` into the workspace path printed by the claim.
3. Read the review prompt file. Run the diff commands inside it.
4. Verify acceptance criteria, owned_files boundary, dead-code check
   (e.g. `grep -r "from.*<new_module>" src/` — a passing test with no
   caller is dead code).
5. Issue verdict:
   - **Approve:** `spec-kitty agent tasks move-task WP## --to approved --note "Review passed: <summary>"`
   - **Reject:** write structured feedback to a temp file, then
     `spec-kitty agent tasks move-task WP## --to planned --force --review-feedback-file <path>`
6. **Wake the peer:**
   ```bash
   agenttalk send --from codex --to claude --kind wake \
     --subject "WP## review verdict" \
     --meta mission="$MISSION" --meta wp_id=WP## --meta new_lane=<approved|planned> --meta actor=codex \
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

```bash
agenttalk wait --for codex --timeout 30
```

- Exit code 0 → peer sent a wake; loop back to step 1 immediately.
- Exit code 1 → timeout. Loop back to step 1 anyway (self-healing —
  poll catches state changes the peer made without sending a wake).

Use a **short** timeout (30s, not 120).

### Step 4 — mission complete

If `spec-kitty next` returns "mission complete" / "all approved" /
"merged":

1. If a merge step is still pending and is your action: run
   `spec-kitty merge --mission "$MISSION"` from the project root.
2. Otherwise: `agenttalk transcript --format md` and tell the user the
   path. Send `agenttalk end --from codex --reason "mission $MISSION complete"`.
3. Exit the loop.

### Step 5 — stop and ask the user when:

- spec-kitty returns an action you don't understand or mentions
  `arbiter` mode.
- A wake contradicts what `spec-kitty next` returns (trust spec-kitty;
  flag the inconsistency).
- Tests crash with infrastructure errors (missing tool, sandbox denial,
  network failure).
- Owned_files violation in the diff.
- The user types a question or new direction.

After the user responds, send a `kind=note` to the peer explaining the
new plan, then resume.

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

## Sandbox / PATH note (codex-specific)

If `agenttalk` or `spec-kitty` is not on PATH inside the Codex sandbox:
- Both ship as Python entry points. Fallback: `python -m agenttalk ...`
  and `python -m spec_kitty ...` (if installed as packages).
- If `python` itself isn't on PATH, find your Python install:
  - Windows: `(Get-Command python).Source` or check
    `$env:LOCALAPPDATA\Programs\Python\`
  - POSIX: `which python3`
- If nothing works, ask the user to run
  `agenttalk codex-config --enable` from the project root, which sets
  `approval_policy = "never"` and `sandbox_mode = "workspace-write"`
  for this project in `~/.codex/config.toml`. Then restart Codex.
