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
  if (o.reasonCode) a.health.reason_code = o.reasonCode;
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
  // B7: build_attention's own wire shape for an escalation - source_refs[0] carries the exact
  // validated {work_item, work_cycle} ONLY when the escalation's opener declared both (never
  // guessed); an unlinked/malformed escalation's ref has request_id alone, matching
  // web_tags.item_ref's {} return. o.requestId defaults to the item's own id, like a real escalation.
  ...(o.source_refs !== undefined ? { source_refs: o.source_refs } : (o.linked !== undefined ? {
    source_refs: [{ kind: 'message', request_id: o.requestId || o.id || 'it-1',
                    ...(o.linked ? { work_item: o.workItem || 'demo-item', work_cycle: o.workCycle || '1' } : {}) }],
  } : {})),
});

// active_count (build round #273, fix round 1): the server's pre-grouping
// active total - defaults to items.length (the realistic value when nothing
// is deferred/grouped/excluded); a test exercising a scenario where that
// would differ from reality (grouping, a server stuck count distinct from
// the client's own) passes an explicit override via o.active_count.
export const attention = (items, o = {}) => ({
  ok: o.ok !== false, asOfMs: o.asOfMs === undefined ? NOW : o.asOfMs, items,
  active_count: o.active_count === undefined ? items.length : o.active_count,
});

export const chat = (messages, o = {}) => ({
  ok: true, asOfMs: NOW,
  payload: { available: o.available !== false, operator: 'operator', lead: o.lead || 'claude-agenttalk-lead', messages,
             detail: o.detail || '', ...(o.pendingDecisions ? { pending_decisions: o.pendingDecisions } : {}) },
});

// One /api/lead-chat pending_decisions entry (build_lead_chat's _lead_chat_pending_decisions),
// keyed the same way an attention escalation item is: by request_id.
export const PENDING_DECISION = (o = {}) => ({
  request_id: o.requestId || 'esc-1', sender: o.sender || 'claude-agenttalk-lead',
  subject: o.subject === undefined ? 'operator input needed' : o.subject,
  decision: o.decision === undefined ? 'Keep the old CSV export?' : o.decision,
  recommendation: o.recommendation || '', priority: o.priority || '', risk_severity: o.riskSeverity || '',
  options: o.options || [], age_seconds: o.age === undefined ? 18000 : o.age, answerable: true,
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
    agent('codex-agenttalk-reviewer-1', {
      state: 'rate_limited_or_outage', since: 900, reasonCode: 'usage_limit_rejected',
    }),
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

// An agent whose health read is stale (older than the TTL, or than the heartbeat): the server
// serves state 'unknown' plus last_known_* (health.normalize). o.lk = the last reported state
// (null = a plain stale read with nothing remembered); o.since / o.updated / o.progress are ages.
export function staleAgent(name, o = {}) {
  const hb = o.hb === undefined ? 20 : o.hb;
  const warning = o.warning || 'health_stale_ttl';
  const a = {
    name,
    last_seen: iso(hb),
    last_seen_age_seconds: hb,
    health: { state: 'unknown', stale: true, advisory: true, updated_at: null, since: null, age_seconds: null,
              warnings: [warning], reason_code: warning },
  };
  if (o.lk) {
    const since = o.since === undefined ? 900 : o.since;
    a.health.last_known_state = o.lk;
    a.health.last_known_since = iso(since);
    a.health.last_known_updated_at = iso(o.updated === undefined ? since : o.updated);
    if (o.progress !== undefined && o.progress !== null) a.health.last_known_progress_at = iso(o.progress);
  }
  return a;
}
