---
affected_files: []
cycle_number: 1
mission_slug: operator-safety-0140-01KTBZA1
reproduction_command:
reviewed_at: '2026-06-05T15:43:54Z'
reviewer_agent: unknown
verdict: rejected
wp_id: WP05
---

# WP05 Review Feedback — rejected

## Blocking finding

1. Lead skills omit the required non-liaison escalation rule.

   WP05 T022 requires `escalate-not-your-window` for listen, sk-loop, and lead: when the roster has an operator-facing agent and this agent is not it, operator questions must go through `agenttalk escalate`, then `wait --to-request` on the printed rid; only exit 2 falls back to the local window. T023 requires the Codex-side skills to mirror T022 1:1.

   Current state:
   - `src/agenttalk/skills/claude/agenttalk.lead.md:87` covers only the liaison case: “If you ARE the operator-facing agent...” and then jumps to rescind/check.
   - `src/agenttalk/skills/codex/agenttalk-lead/SKILL.md:88` has the same gap.

   Impact: a lead agent that is not the operator-facing agent can still ask the human in its own window, which violates the single-voice operator-routing contract this release is meant to teach.

   Expected fix: add a mirrored lead-skill block for “Escalate, don’t ask your own window” to both Claude and Codex lead skills, preserving each file’s shell dialect and the exit-2-only fallback wording already used in listen/sk-loop.

## Verification performed

- Resolved Spec Kitty review context for `operator-safety-0140-01KTBZA1` / `WP05`; lane is `for_review`, head is `f98e2f648687dce93806fb46ad9080341dbc7a71`.
- Verified incremental WP05 scope from approved WP04 head `7f60ac877485da56362819016eee97a711483164` stays within owned files.
- `git diff --check 7f60ac8..f98e2f6` passed.
- Source-tree CLI checks with `PYTHONPATH=$PWD\src`: `agenttalk --version` reports `0.14.0`; `check`, scoped `wait` exit 3, and `composing --to-request` help are present and consistent.
- Reviewed README, SECURITY, CHANGELOG, and version bumps; no additional blocking findings found.
- `pytest -q`: 504 passed, 2 skipped.
- `ruff check .`: all checks passed.
