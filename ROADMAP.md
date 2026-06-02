# agenttalk — roadmap & working notes

Carry-forward notes from the Claude Code + Codex collaboration that built
0.10.0–0.11.1. This is the "what next / what to remember" doc — design
boundaries, deferred work, and operational gotchas — so the next session
(or the next machine) can pick up where we left off.

> Current release: **v0.11.1** (`master`). See `CHANGELOG.md` for the
> full history and `SECURITY.md` for the trust model.

---

## Where things stand

- **0.11.1 is a clean stopping point.** The fresh-eyes review pass (each
  CLI spawning a context-free sub-agent) found real edge cases *after* the
  normal build + cross-reviews — both were fixed and documented. The
  multi-agent surface (roster roles/groups, broadcast fan-out, multi-party
  threads, lead skills) is shipped and dogfooded.

---

## Future work (in rough priority order)

### 1. Per-agent identity / authorization — the real boundary
Roles and groups are **routing metadata, not a trust boundary** (see
`SECURITY.md`). Optional HMAC is **project-key based**, so `from=<agent>`
is a by-convention identity among trusted local participants, not a
cryptographic one. **This is the gate to cross before extending agenttalk
to anything less than a fully-trusted local team** (e.g. remote or
less-trusted workers, or a lead delegating to agents it doesn't fully
trust). Per-agent signing keys + an authorization policy would be a
distinct feature with a different threat model — design it deliberately,
don't let it accrete.

### 2. Broadcast follow-ups (neither blocking; do if you lean on broadcast)
- **reply-all is still just a follow-up broadcast.** There is no in-place
  threaded "reply to everyone" primitive — a responder replies to the
  sender, and "reply to all" means sending a new `broadcast`. Fine for now;
  revisit if group back-and-forth becomes common.
- **Fan-out is not transactional.** `agenttalk broadcast` writes one
  message per recipient in a loop. If a write fails mid-loop, some
  recipients get the message and others don't — there is no rollback and
  no "all-or-nothing" guarantee. Consider a staged-write + commit, or at
  least surfacing partial-fan-out failures, if broadcast becomes critical.

### 3. Minor edges surfaced by the fresh-review pass (low priority, non-blocking)
These are internally consistent today (symmetric with existing pairwise
behavior) — tighten only if they actually bite:
- A member who answers a broadcast `question` with `--kind question`
  (an off-path counter-ask) is **not** counted as "responded" — only
  `message`/`note` close it. Matches the pairwise question contract.
- A closed broadcast slice can show `(unread)` if the member replied via
  `reply --to-request` without draining the original opener. Same as the
  pairwise path; arguably a useful nudge to ack.
- `roster set-group <g> ""` creates an **empty** group (exit 0).
  Harmless (broadcasting to it later errors "no recipients"), but it
  silently makes a do-nothing group — could warn or treat as a no-op.

---

## Operational notes (lessons from this build)

### Fresh-review ritual asymmetry
Spawning a fresh, context-free sub-agent to review works on **both**
sides (Claude via the `Agent` tool; Codex via `spawn_agent` with no
inherited context), and it earned its keep — it caught a crash and a
routing bug that the build, an adversarial review workflow, *and* the
cross-review all missed. **But the environments differ:** the Claude-side
fresh agent could run the test suite; the Codex-side fresh agent had **no
Python in its sandbox**, so it reviewed diffs statically (no `pytest`).
If you make "spawn fresh reviewers" a repeatable step, expect that
asymmetry — lean on the Claude-side reviewer for anything that needs to
*execute*, the Codex-side one for static/diff reasoning.

### Release ritual (don't repeat the tags-vs-Releases miss)
`git push --tags` only creates **git tags**. The GitHub **Releases page**
shows **Release objects**, which are separate and must be created on top
of a tag. The full ritual:

```bash
# 1. bump version (pyproject.toml + src/agenttalk/__init__.py),
#    date the CHANGELOG section, commit "release: prepare vX.Y.Z"
# 2. tag + push
git tag -a vX.Y.Z -m "agenttalk vX.Y.Z — <summary>"
git push origin master
git push origin vX.Y.Z
# 3. publish the GitHub Release object (this is the step that's easy to miss)
gh release create vX.Y.Z --verify-tag --latest \
  --title "vX.Y.Z — <short description>" \
  --notes-file <the CHANGELOG section for this version>
```

Also remember to bump the **install pin** in `README.md` (the
`pip install ...@vX.Y.Z` examples) to the new tag.

---

## Resuming work later (incl. on another machine)

1. Clone + editable install:
   ```bash
   git clone https://github.com/zoolok17/agenttalk.git
   cd agenttalk
   python -m pip install -e .
   agenttalk install-skills      # refresh ~/.claude/commands + ~/.codex/skills
   ```
2. In the project you're working in, restart the two collaboration loops:
   - **Claude Code:** `/agenttalk.listen` (or `/agenttalk.sk-loop <mission>`,
     or `/agenttalk.lead` to coordinate a team)
   - **Codex:** `$agenttalk-listen` (or `$agenttalk-sk-loop`, `$agenttalk-lead`)
3. For a team / fresh-review setup, name agents distinctly and group them,
   e.g.:
   ```bash
   agenttalk roster add claude-rev --role reviewer --group reviewers
   agenttalk roster add codex-rev  --role reviewer --group reviewers
   agenttalk broadcast --from claude-dev --to-group reviewers --kind question -m "fresh eyes on <scope>?"
   agenttalk threads --for claude-dev    # watch responded/pending
   ```

---

*Notes jointly developed by Claude Code and Codex while building 0.10.0–0.11.1.*
