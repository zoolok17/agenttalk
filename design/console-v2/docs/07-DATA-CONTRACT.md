# 07 · Data contract (view model)

One snapshot per team. Every object carries `as_of` (ISO time the source file was written). The UI derives freshness; it never invents values.

```ts
type Team = {
  key: string; label: string;            // "Main team"
  machine: string;                        // "win-ws01"
  project: string;                        // "agenttalk" — drives name shortening
  source_as_of: string;                   // newest file time seen
  reachable: boolean;                     // last poll succeeded
  agents: Agent[]; needs: NeedsItem[]; lead_latest: LeadMessage;
  usage: UsageWindow[]; gateway?: Gateway;   // this machine's qwen gateway (flag)
};
type Agent = {
  id: string;                             // full name, e.g. codex-agenttalk-developer-5
  runtime: 'claude'|'codex'|'qwen';
  state: 'working'|'busy'|'idle'|'stuck'|'capped';
  //  working = working_turn · busy = working_silent WITH evidence of activity · idle = idle_waiting
  //  stuck = stuck_suspected WITH evidence · capped = rate_limited_or_outage
  status_line: string;                    // human evidence: "Running tests 6m · output 40s ago"
  evidence: {                             // available today:
    heartbeat_age_s: number; progress_counter: number; progress_changed_s: number; last_message_s: number; replied_since_wake: boolean;
    process?: { state: 'running'|'exited'; since_s: number; last_output_s?: number }; // NOT available yet (backend work)
  };
  spend_today_eur?: number;               // qwen only
  resets_at?: string;                     // capped only
  as_of: string;
};
type NeedsItem = {
  id: string;
  kind: 'LOOKS STUCK'|'SPEND LIMIT'|'GATE HOLD'|'DECISION';
  title: string; evidence: string;        // evidence is REQUIRED
  opened_at: string; deadline?: string;   // deadline OPTIONAL
  options: { label: string; requires_actions?: boolean }[]; // first = primary
  state: 'open'|'answered'|'deferred'; answer?: string;
};
type UsageWindow = { runtime: 'claude'|'codex'; window: '5h'|'weekly'; pct: number; resets_at: string; as_of: string };
type Gateway = {                                     // one per machine
  machine: string; alert_eur: number; cutoff_eur: number;   // e.g. 39 / 44
  ledger_eur: number; ledger_as_of: string;           // live
  projected_eur?: number;                             // linear, this week's rate
  per_agent_today: Record<string, number>;
};
type Account = {                                      // across all teams
  month: string; cap_eur: number;                     // 100
  ledgers: { machine: string; eur: number; as_of: string; stale: boolean }[];
  sum_eur: number;                                    // derived: sum of ledgers
  bill_eur?: number; bill_as_of?: string;             // provider bill, late
};
type LeadMessage = { body: string; at: string };
```

## Derivations
- **Freshness**: `stale = now − source_as_of > 2 × poll interval` (or `!reachable`). Offline banner when stale > 5 min. Show "last seen <age>" and freeze at `source_as_of`.
- **Stuck vs busy (today, no process data)**: raise LOOKS STUCK if the progress counter hasn't changed for ≥ 10 min **and** no reply was sent since the wake **and** the heartbeat is fresh. Evidence line: "No progress for 14 min · no reply sent · heartbeat still fresh". Options: **Wait 10 min** (primary), Restart with context. If progress is moving, state = busy ("Progress +3 in the last 2 min · no message for 6m"), no card. If the heartbeat itself is stale, it's an offline/freshness problem, not stuck.
- **Stuck vs busy (once process data exists)**: also require process exited or no output ≥ 10 min. Evidence: "No output for 14 min · test run exited 12 min ago · no reply sent". Restart becomes primary.
- **Queue order**: kind LOOKS STUCK first, then by `opened_at` ascending.
- **Age label**: deadline ? "decide by <date>" : "no deadline · waiting <age>".
- **Team bar**: gateway ledger vs its own alert/cutoff; scale max(cutoff, projection) × 1.04. **Account bar**: stacked ledgers vs cap; headline = `max(sum, bill)` when the bill is newer than every ledger, else sum. Never show the cap on a team bar.
- **Crossing day**: day_of_month × cutoff / ledger, shown when projection > cutoff.
- **Idle count** in quiet greeting: agents with state idle.

## Where it comes from (repo)
Roster/health, messages/threads, needs queue (attention), gates, risk, ownership (`domains.json`), lessons, onboarding, lead chat, usage windows: existing snapshot API used by `web_static/console.js`. **New backend pieces (flags):** gateway spend per machine + account bill; a list of team sources; process evidence for stuck agents.

Example data for every field: `fixtures/turn3.json`.
