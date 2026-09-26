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
  usage: UsageWindow[]; budget?: QwenBudget; // budget only if the gateway ledger is readable (flag)
};
type Agent = {
  id: string;                             // full name, e.g. codex-agenttalk-developer-5
  runtime: 'claude'|'codex'|'qwen';
  state: 'working'|'busy'|'idle'|'stuck'|'capped';
  //  working = working_turn · busy = working_silent WITH evidence of activity · idle = idle_waiting
  //  stuck = stuck_suspected WITH evidence · capped = rate_limited_or_outage
  status_line: string;                    // human evidence: "Running tests 6m · output 40s ago"
  evidence?: { last_output_s?: number; process?: 'running'|'exited'; exited_s?: number; replied?: boolean };
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
type QwenBudget = {
  month: string; cap_eur: number; alert_eur: number; cutoff_eur: number;
  ledger_eur: number; ledger_as_of: string;          // live
  bill_eur?: number; bill_as_of?: string;             // late
  projected_eur?: number;                             // linear, this week's rate
  per_agent_today: Record<string, number>;
};
type LeadMessage = { body: string; at: string };
```

## Derivations
- **Freshness**: `stale = now − source_as_of > 2 × poll interval` (or `!reachable`). Offline banner when stale > 5 min. Show "last seen <age>" and freeze at `source_as_of`.
- **Stuck vs busy**: raise LOOKS STUCK only if no output ≥ 10 min **and** (process exited **or** no process) **and** no reply sent. Otherwise state = busy with a status line and no card.
- **Queue order**: kind LOOKS STUCK first, then by `opened_at` ascending.
- **Age label**: deadline ? "decide by <date>" : "no deadline · waiting <age>".
- **Budget headline**: `max(ledger, bill)` when `bill_as_of` ≥ `ledger_as_of`, else ledger. Colour by headline vs alert/cutoff.
- **Idle count** in quiet greeting: agents with state idle.

## Where it comes from (repo)
Roster/health, messages/threads, needs queue (attention), gates, risk, ownership (`domains.json`), lessons, onboarding, lead chat, usage windows: existing snapshot API used by `web_static/console.js`. Qwen budget and multi-team: **new** — gateway ledger reader and a per-team source list (flags).

Example data for every field: `fixtures/turn3.json`.
