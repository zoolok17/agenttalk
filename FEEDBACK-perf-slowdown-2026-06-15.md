# agenttalk perf feedback — multi-day laptop slowdown from `wait` polling

**From:** Orbit Launcher team (claude, lead) · **Date:** 2026-06-15
**Severity:** High (degrades the whole machine after a few days of heavy use; not data-loss)
**Ask:** candidate for a near-term agenttalk release

---

## Symptom (operator-reported)

> "There seems to be a bug or some kind of a leak in the agenttalk skill; after a while, the python processes massively slow down the laptop… these slowdowns usually come after a few days of heavy uninterrupted work."

This has now reproduced twice across long sessions. It is not a crash — it's a gradual, machine-wide slowdown that scales with **session age** and **number of concurrent agents**, and clears on `reset --archive` + killing stale waiters.

## Root cause: `wait` cost is O(waiters × store), re-paid every poll

`agenttalk wait` polls on a ~0.3s interval, and **each poll re-reads and re-parses the entire message store** (no incremental cursor read off disk; the full `messages/` dir is walked and every file parsed). Two factors multiply:

1. **Store never shrinks on the hot path.** Messages accumulate in `messages/` for the life of the bus. Nothing auto-archives. Over days of heavy traffic the store grows unbounded, so each poll gets steadily more expensive.
2. **Waiters accumulate.** Every agent terminal arms a `wait` loop (`--timeout 1800`, re-armed on expiry). Leftover loops from earlier sessions are never reaped — they keep polling a bus that's been idle for days.

Combined cost ≈ `N_waiters × store_size × poll_rate`. With ~5 waiters on a ~300-message store at ~3 polls/sec that's **~4–5k file opens + parses per second, sustained**, climbing as the store grows.

## Live evidence captured 2026-06-15 (this machine, mid-slowdown)

**Python processes (7 total, 6 are agenttalk `wait` loops):**

| PID | command | age | CPU (kernel+user) |
|-----|---------|-----|-------------------|
| 37060 | `wait --for codex-dev --timeout 1800` | 1 min | ~2s |
| 29156 | `wait --for codex-orbit-dev --timeout 1800` | 20 min | ~24s |
| 30056 | `--root …/orbitlauncher wait --for claude-developer-2` | 23 min | ~28s |
| 41420 | `wait --for claude-vega --timeout 1800` | 23 min | ~28s |
| 33560 | `wait --for codex --timeout 1800` | 27 min | ~32s |
| 16148 | `wait --for codex-orbit-dev-2 --timeout 1800` | 29 min | ~36s |

(Each waiter's CPU climbs roughly linearly with its age — consistent with a fixed per-poll cost paid continuously.)

**Store state — the amplifier:**

| root | live messages | size | archived/ |
|------|--------------:|-----:|-----------|
| `…/orbitlauncher/.agenttalk` | 37 | 58 KB | **5936 files** (was `reset --archive`'d last session) |
| `…/agenttalk/.agenttalk` | **299** | **678 KB** | **none — never pruned** |

The agenttalk repo's own bus is **5–6 days stale** (`status`: cursors 5–6d ago, 25 unconsumed responses, codex `waiting(stale)`), yet still holds 299 live files that any waiter rooted there re-parses every 0.3s. The orbitlauncher bus, which *was* archived, is 8× smaller and not implicated.

## Recommended fixes (in rough priority order)

1. **Don't re-parse the whole store per poll.** Read incrementally from the persisted cursor — only parse files newer than `seen_msg_id` — or maintain an append-only index / single log the poller can `seek` to. This removes the `× store_size` term entirely and is the highest-leverage fix.
2. **Auto-archive / cap the live store.** Move messages older than a threshold (count or age) out of `messages/` automatically, instead of relying on a manual `reset --archive`. The orbitlauncher bus proves archiving fixes it; it just isn't automatic.
3. **Back off / adapt the poll interval, or use a filesystem watcher.** 0.3s fixed polling is the per-waiter constant being multiplied. An OS file-watch (or exponential backoff up to a few seconds when the bus is quiet) cuts idle cost dramatically. A bus idle for days should poll near-zero.
4. **Reap stale waiters.** A `wait` whose agent/session is long dead should self-terminate (heartbeat/lockfile staleness check), and arming should enforce kill-before-arm so a terminal can't stack duplicate loops. Optionally a soft cap + warning when N waiters exceed a threshold.

Fixes 1+3 attack the per-poll cost; 2+4 attack the accumulation. Either pair alone helps; together they should eliminate the multi-day creep.

## Operator-side mitigations (current workarounds, until a fix ships)

- `reset --archive` between sessions (drains the live store to `archived/`).
- Kill leftover `wait` processes from prior sessions; arm listeners at `--interval 2` not the 0.3s default; kill-before-arm so loops don't stack.
- Watch for unrelated long-lived daemons too — on this machine a separate 5-day-old `tools.polymarket_weather_daemon` (PID 7860) had independently burned ~5.8 CPU-hours and was a *second*, non-agenttalk contributor to the overall slowness. Worth noting so the agenttalk creep isn't blamed for 100% of it.
