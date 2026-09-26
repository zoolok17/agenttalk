// Fixtures for the console v2 model and render tests: small builders for the shapes
// /api/state, /api/attention and /api/lead-chat return, plus a fixed "now".
export const NOW = Date.UTC(2026, 8, 26, 12, 0, 0);

// ISO time `seconds` before NOW.
export const iso = (seconds) => new Date(NOW - seconds * 1000).toISOString();

// Epoch seconds `seconds` after NOW (capacity resets_at is epoch seconds).
export const epochIn = (seconds) => Math.floor((NOW + seconds * 1000) / 1000);

// One /api/state agent row. o.state is the health state; o.since / o.progress /
// o.hb are ages in seconds (progress null = no last_progress_at; hb null = never seen).
export function agent(name, o = {}) {
  const state = o.state || 'idle_waiting';
  const a = {
    name,
    last_seen_age_seconds: o.hb === undefined ? 20 : o.hb,
    health: {
      state,
      since: iso(o.since === undefined ? 1800 : o.since),
      updated_at: iso(10),
      age_seconds: 10,
      stale: !!o.stale,
    },
  };
  if (o.role) a.role = o.role;
  if (o.cli) a.cli = o.cli;
  if (o.hb !== null) a.last_seen = iso(o.hb === undefined ? 20 : o.hb);
  if (o.progress !== undefined && o.progress !== null) a.health.last_progress_at = iso(o.progress);
  if (o.task) a.task = o.task;
  if (o.capacity) a.capacity = o.capacity;
  return a;
}

// A capacity object like web.py's _capacity_entry.
export function capacity(o = {}) {
  const c = { confidence: o.confidence || 'fresh', observed_at: iso(o.observed === undefined ? 30 : o.observed) };
  if (o.primary !== undefined) c.primary = { label: '5h', used_pct: o.primary, resets_at: epochIn(o.primaryReset === undefined ? 3600 : o.primaryReset) };
  if (o.secondary !== undefined) c.secondary = { label: 'weekly', used_pct: o.secondary, resets_at: epochIn(o.secondaryReset === undefined ? 86400 * 3 : o.secondaryReset) };
  return c;
}

// A recent-envelope row (newest first in a root's recent[]).
export const env = (from, to, kind, ago) => ({ id: `m-${from}-${ago}`, ts: iso(ago), from, to, kind, subject: 's' });

export function root(o = {}) {
  const r = {
    label: o.label || 'agenttalk',
    path: o.path || 'D:\work\agenttalk',
    project_id: o.project_id === undefined ? 'proj-a' : o.project_id,
    errors: o.errors || [],
    agents: o.agents || [],
    recent: o.recent || [],
  };
  if (o.operator_facing) r.operator_facing = o.operator_facing;
  return r;
}

export const ATT_ITEM = (o = {}) => ({
  id: o.id || 'it-1', source: o.source || 'escalation', source_label: o.source_label === undefined ? 'ESCALATION' : o.source_label,
  severity: o.severity || 'high', title: o.title === undefined ? 'Keep the old CSV export?' : o.title,
  agent: o.agent === undefined ? null : o.agent, detail: o.detail === undefined ? 'rev-3 cold review: FIX' : o.detail,
  age_seconds: o.age === undefined ? 18000 : o.age, human_can_unblock_now: true,
  ...(o.answerable ? { answerable: true, options: o.options || ['Keep it', 'Remove it'] } : {}),
  ...(o.age_unknown ? { age_unknown: true } : {}),
});

export const attention = (items, o = {}) => ({ ok: o.ok !== false, asOfMs: o.asOfMs === undefined ? NOW : o.asOfMs, items });

export const chat = (messages, o = {}) => ({
  ok: true, asOfMs: NOW,
  payload: { available: o.available !== false, operator: 'operator', lead: o.lead || 'claude-agenttalk-lead', messages, detail: o.detail || '' },
});

export const CONN_OK = { reachable: true, stalledPolls: 0, lastOkMs: NOW };

// The busy-day team: 10 agents like the live roster. All heartbeats fresh.
export function busyAgents() {
  const claudeCap = capacity({ primary: 41, primaryReset: 3 * 3600, secondary: 23, secondaryReset: 4 * 86400 });
  const codexCap = capacity({ primary: 100, primaryReset: 11 * 3600 + 40 * 60, secondary: 62, secondaryReset: 2 * 86400 });
  return [
    agent('claude-agenttalk-lead', { state: 'idle_waiting', since: 600, capacity: claudeCap }),
    agent('claude-agenttalk-developer-2', { state: 'working_turn', since: 180, task: 'Implementing WP-15', capacity: claudeCap }),
    agent('claude-agenttalk-frontend-dev', { state: 'idle_waiting', since: 2400 }),
    agent('claude-agenttalk-reviewer-3', { state: 'idle_waiting', since: 7200 }),
    agent('codex-agenttalk-developer-5', { state: 'working_silent', since: 1200, progress: 120, capacity: codexCap }),
    agent('codex-agenttalk-developer-4', { state: 'working_silent', since: 1800, progress: 840, capacity: codexCap }),
    agent('codex-agenttalk-reviewer-1', { state: 'rate_limited_or_outage', since: 900, capacity: codexCap }),
    agent('qwen-agenttalk-dev-1', { state: 'working_turn', since: 60, task: 'Drafting fixtures' }),
    agent('qwen-agenttalk-reviewer-1', { state: 'idle_waiting', since: 3000 }),
  ];
}

// dev-5 messaged 6 minutes ago (busy, not stuck); dev-4 has said nothing since it woke.
export function busyRecent() {
  return [
    env('claude-agenttalk-lead', 'operator', 'message', 240),
    env('codex-agenttalk-developer-5', 'claude-agenttalk-lead', 'task-response', 360),
    env('claude-agenttalk-reviewer-3', 'claude-agenttalk-lead', 'review-result', 3000),
  ];
}
