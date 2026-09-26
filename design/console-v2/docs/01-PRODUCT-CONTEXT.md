# 01 · Product context (from the dev team's notes)

## Who and how
- **One operator** runs a team of AI coding agents. Often away for hours or days; checks in from a **phone**, sometimes via remote desktop. Rarely sits at the console for long. → The console must answer "does anything need me?" in one glance, and let them answer it in one tap.
- **One lead agent** hands out all work, reads every reply, and only asks the operator for things that need a human: **a decision**, or **a spending limit**. A normal day has a handful of items.

## Teams and machines
- **Main team** — lead + 9 agents on one Windows machine (`win-ws01` in the mocks):
  - Claude ×5 (lead, developers, reviewers)
  - Codex ×3 (developers, reviewers)
  - Qwen ×2 — open model, paid per use through our own **gateway** with a hard monthly budget.
- **Second team** — lead + 4 agents, different product, separate **Linux** machine (`linux-02`, project `shopfront` in the mocks).

## How the work behaves (easy to get wrong)
- **Turns.** A message wakes an agent; it works, replies, goes idle. **Idle is the resting state, not a problem.**
- **Busy can look stuck.** A long test suite can mean 5–10 minutes of silence. A "stuck" card must show evidence before offering Restart.
- **Cold reviews.** The reviewer reads the change without being told what to expect. Verdict: **GO, FIX or HOLD**. It counts as evidence, not a sign-off.
- **Gates** decide GO/HOLD from evidence. The operator never clicks GO; they can only waive a gate or accept a risk, with actions on.
- **Usage limits.** Claude and Codex have rolling windows (5-hour and weekly), each shown as % used + reset time.
- **Money (qwen gateway).** Three configured levels plus a fourth, late number:
  - **Alert** ("soft stop") — notifies the operator, blocks nothing.
  - **Hard cutoff** — the gateway refuses further calls.
  - **Monthly cap** — what the operator watches most. Today 100 EUR; spend ≈ 67 EUR.
  - **Provider bill** — arrives hours late. Our internal **ledger is the live figure**; the bill confirms it later.
- **Freshness.** Every number came from a file an agent wrote. When the machine sleeps or the network drops, numbers go stale *while looking normal*. This happened for real.

## Real agent names
Like `codex-agenttalk-developer-5` — see the shortening rule in `06-RULES.md`.
