// Console v2 view-model tests (M2): the pure derivations in console2-model.js.
// Run: node tests/console2_view.test.mjs   (also run by tests/test_console2_web.py)
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { createRunner } from './console2_harness.mjs';
import {
  ATT_ITEM, CONN_OK, NOW, agent, attention, busyAgents, busyRecent, capacity, chat, env, epochIn, iso, root,
} from './console2_fixtures.mjs';

// Loaded as a CommonJS module in this realm (the model is pure), so returned arrays and
// objects compare with deepEqual without crossing a vm boundary.
const M = createRequire(import.meta.url)('../src/agenttalk/web_static/console2-model.js');
const { test, run } = createRunner('console2 view model');
const TZ = 'utc';
const P = ['agenttalk'];

function ctx(o = {}) {
  return { nowMs: NOW, generatedMs: NOW, recent: [], teamIds: [], project: 'agenttalk', known: P, tz: TZ, ...o };
}
const view = (a, o) => M.agentView(a, ctx(o));
const NAME = 'codex-agenttalk-developer-6';
const sil = (o) => agent(NAME, { state: 'working_silent', ...o });
// The bus window covers everything since the agent woke: fewer than 25 envelopes.
const WINDOW = [env('claude-agenttalk-lead', 'operator', 'message', 5)];

// ------------------------------------------------------------------ formatting

test('fmtAge: seconds, minutes, hours, days', () => {
  const got = [0, 40, 59, 60, 840, 3599, 3600, 18000, 86399, 86400, 172800].map(M.fmtAge);
  assert.deepEqual(got, ['0s', '40s', '59s', '1m', '14m', '59m', '1h', '5h', '23h', '1d', '2d']);
  assert.equal(M.fmtAge(-5), '0s');
});

test('fmtAgeLong: banners read 3h 12m, not 3h', () => {
  assert.deepEqual([40, 840, 3 * 3600 + 12 * 60, 3600, 2 * 86400 + 4 * 3600].map(M.fmtAgeLong),
    ['40s', '14m', '3h 12m', '1h 0m', '2d 4h']);
});

test('clockHM, whenLabel and resetLabel in a fixed zone', () => {
  const t = Date.UTC(2026, 8, 26, 18, 22);
  assert.equal(M.clockHM(t, TZ), '18:22');
  assert.equal(M.whenLabel(t, NOW, TZ), '18:22', 'later today is a bare clock time');
  assert.equal(M.whenLabel(Date.UTC(2026, 8, 25, 22, 10), NOW, TZ), 'yesterday 22:10');
  assert.equal(M.whenLabel(Date.UTC(2026, 8, 22, 9, 5), NOW, TZ), 'Tue 09:05');
  assert.equal(M.resetLabel(Date.UTC(2026, 8, 26, 23, 40), NOW, TZ), '23:40');
  assert.equal(M.resetLabel(Date.UTC(2026, 8, 28, 3, 0), NOW, TZ), 'Mon 03:00');
});

test('ageSeconds: ISO and epoch input, never negative, null when unparseable', () => {
  assert.equal(M.ageSeconds(iso(90), NOW), 90);
  assert.equal(M.ageSeconds(NOW - 5000, NOW), 5);
  assert.equal(M.ageSeconds(iso(-30), NOW), 0, 'a future time reads as now');
  for (const bad of [null, undefined, '', 'yesterday', {}, NaN]) assert.equal(M.ageSeconds(bad, NOW), null, String(bad));
});

// ---------------------------------------------------------------- roster rows

test('idle agent: grey, resting, with how long', () => {
  const v = view(agent('claude-agenttalk-frontend-dev', { state: 'idle_waiting', since: 2400 }));
  assert.deepEqual([v.state, v.tone, v.line], ['idle', 'dim', 'Idle \u00b7 40m']);
  assert.equal(v.short, 'fe-dev');
  assert.equal(v.title, 'claude-agenttalk-frontend-dev');
});

test('working turn shows the task and how long; a long turn is never stuck', () => {
  const v = view(agent('claude-agenttalk-developer-2', { state: 'working_turn', since: 180, task: 'Implementing WP-15' }));
  assert.deepEqual([v.state, v.tone, v.line], ['working', 'ok', 'Working \u00b7 Implementing WP-15 \u00b7 3m']);
  const long = view(agent('claude-agenttalk-developer-2', { state: 'working_turn', since: 7200 }), { recent: WINDOW });
  assert.equal(long.state, 'working');
  assert.equal(long.stuck, null);
});

test('busy: silent but progress is recent -> no card, listed as also happening', () => {
  const v = view(sil({ since: 1200, progress: 120 }), { recent: [env(NAME, 'claude-agenttalk-lead', 'message', 360)] });
  assert.equal(v.state, 'busy');
  assert.equal(v.stuck, null);
  assert.equal(v.candidate, false);
  assert.equal(v.line, 'Quiet \u00b7 last progress 2m ago \u00b7 no message for 6m');
  assert.equal(v.aside.title, 'dev-6 is quiet, not stuck');
  assert.equal(v.aside.detail, 'Last progress 2m ago \u00b7 no message for 6m \u00b7 no card while progress moves');
});

test('stuck: 14 min without progress, no reply, fresh heartbeat -> a card with the fallback evidence', () => {
  const v = view(sil({ since: 1800, progress: 840 }), { recent: WINDOW });
  assert.equal(v.state, 'stuck');
  assert.equal(v.tone, 'warn');
  assert.equal(v.stuck.evidence, 'Last progress 14m ago \u00b7 no reply sent \u00b7 heartbeat still fresh');
  assert.equal(v.stuck.progressAge, 840);
  assert.equal(v.aside, null);
});

test('stuck without any progress note counts from when the turn began', () => {
  const v = view(sil({ since: 900 }), { recent: WINDOW });
  assert.equal(v.state, 'stuck');
  assert.equal(v.stuck.evidence, 'No progress noted since the turn began 15m ago \u00b7 no reply sent \u00b7 heartbeat still fresh');
});

test('the 10-minute boundary: 599 s is busy, 600 s is stuck', () => {
  assert.equal(view(sil({ since: 1800, progress: 599 }), { recent: WINDOW }).state, 'busy');
  assert.equal(view(sil({ since: 1800, progress: 600 }), { recent: WINDOW }).state, 'stuck');
});

test('a reply since the agent woke means it is not stuck', () => {
  const recent = [env(NAME, 'claude-agenttalk-lead', 'message', 700)];
  const v = view(sil({ since: 1800, progress: 840 }), { recent });
  assert.equal(v.state, 'busy');
  assert.equal(v.candidate, true);
  assert.equal(v.stuck, null);
  assert.equal(v.line, 'Last progress 14m ago \u00b7 replied since it woke');
  // a message from BEFORE the wake does not count
  const before = view(sil({ since: 1800, progress: 840 }), { recent: [env(NAME, 'x', 'message', 2400)] });
  assert.equal(before.state, 'stuck');
});

test('reply status unknown (window too short to cover the wake) raises no card', () => {
  const full = [];
  for (let i = 0; i < 25; i++) full.push(env('claude-agenttalk-lead', 'operator', 'message', 60 + i * 10)); // newest 25, all after the wake
  const v = view(sil({ since: 1800, progress: 840 }), { recent: full });
  assert.equal(v.state, 'busy');
  assert.equal(v.stuck, null);
  assert.equal(v.line, 'Last progress 14m ago \u00b7 reply status unknown');
  assert.equal(v.aside.title, 'dev-6 is quiet');
  // ...but the same window DOES cover the wake if it reaches back past it
  const reaching = full.slice(0, 24).concat([env('claude-agenttalk-lead', 'operator', 'message', 2000)]);
  assert.equal(view(sil({ since: 1800, progress: 840 }), { recent: reaching }).state, 'stuck');
});

test('replyInfo: coverage rules, last message age', () => {
  assert.deepEqual({ ...M.replyInfo(NAME, NOW - 1800e3, [], NOW) }, { replied: false, lastMessageAge: null });
  assert.deepEqual({ ...M.replyInfo(NAME, null, [], NOW) }, { replied: null, lastMessageAge: null });
  const after = [env(NAME, 'x', 'message', 100), env(NAME, 'x', 'message', 900)];
  assert.deepEqual({ ...M.replyInfo(NAME, NOW - 1800e3, after, NOW) }, { replied: true, lastMessageAge: 100 });
  assert.deepEqual({ ...M.replyInfo(NAME, NOW - 50e3, after, NOW) }, { replied: false, lastMessageAge: 100 });
  assert.equal(M.replyInfo(NAME, NOW, [null, 5, 'x', { ts: 'bad', from: NAME }], NOW).replied, false);
});

test('a stale heartbeat is a freshness problem, not a stuck agent', () => {
  const v = view(sil({ since: 1800, progress: 840, hb: 400 }), { recent: WINDOW });
  assert.equal(v.state, 'unknown');
  assert.equal(v.stuck, null);
  assert.equal(v.line, 'Heartbeat stale 6m \u00b7 not judged stuck');
  assert.equal(view(sil({ since: 1800, progress: 840, hb: 300 }), { recent: WINDOW }).state, 'stuck', '300 s is still fresh');
  assert.equal(view(sil({ since: 1800, progress: 840, hb: 301 }), { recent: WINDOW }).state, 'unknown');
  assert.match(view(sil({ since: 1800, hb: null }), { recent: WINDOW }).line, /^Heartbeat missing/);
});

test('the wrapper\u2019s own stuck_suspected is a candidate, and a card only with the full evidence', () => {
  const s = (o) => agent(NAME, { state: 'stuck_suspected', ...o });
  const early = view(s({ since: 1800, progress: 200 }), { recent: WINDOW });
  assert.equal(early.state, 'busy');
  assert.equal(early.candidate, true);
  assert.equal(early.stuck, null);
  assert.equal(early.line, 'Wrapper suspects a stall · last progress 3m ago · reply status unknown');
  const evidenced = view(s({ since: 1800, progress: 840 }), { recent: WINDOW });
  assert.equal(evidenced.state, 'stuck');
});

test('capped: window full and when it resets; weekly; and a bare outage', () => {
  const capped = (cap) => view(agent('codex-agenttalk-reviewer-1', { state: 'rate_limited_or_outage', since: 900, capacity: cap }));
  const five = capped(capacity({ primary: 100, primaryReset: 11 * 3600 + 40 * 60, secondary: 40 }));
  assert.deepEqual([five.state, five.tone, five.line, five.cap], ['capped', 'bad', '5-hour window full', 'resets 23:40']);
  assert.equal(five.aside.title, 'rev-1 is capped');
  assert.equal(five.aside.detail, '5-hour window full \u00b7 resets 23:40');
  const weekly = capped(capacity({ primary: 50, secondary: 100, secondaryReset: 2 * 86400 }));
  assert.equal(weekly.line, 'Weekly window full');
  assert.equal(weekly.cap, 'resets Mon 12:00');
  const bare = view(agent('codex-agenttalk-reviewer-1', { state: 'rate_limited_or_outage', since: 900 }));
  assert.equal(bare.line, 'Rate limited or provider outage');
  assert.equal(bare.cap, '');
});

test('down states get their own bad-tone row and no card', () => {
  const cases = {
    crashed_or_exited: 'Crashed or exited', errored_poison: 'Errored (poisoned turn)',
    errored_ambiguous: 'Errored', degraded_output: 'Degraded output',
  };
  for (const [state, text] of Object.entries(cases)) {
    const v = view(agent('claude-agenttalk-developer-2', { state, since: 720 }));
    assert.deepEqual([v.state, v.tone, v.line, v.stuck], ['down', 'bad', text + ' \u00b7 12m', null], state);
    assert.equal(v.aside.title, 'dev-2 is down');
  }
});

test('unknown health (missing, stale, or unrecognised) reads as unknown, never as idle', () => {
  assert.equal(view(agent('claude-agenttalk-developer-2', { state: 'unknown', hb: 700 })).line, 'No fresh health \u00b7 last seen 11m');
  assert.equal(view(agent('claude-agenttalk-developer-2', { stale: true, hb: 30 })).state, 'unknown');
  assert.equal(view(agent('claude-agenttalk-developer-2', { state: 'brand_new_state' })).state, 'unknown');
  assert.equal(view(agent('claude-agenttalk-developer-2', { state: 'unknown', hb: null })).line, 'No fresh health \u00b7 never seen');
  assert.equal(view({ name: 'x' }).state, 'unknown');
});

test('avatar file comes from a fixed hexagon list, never from the feed', () => {
  const files = ['lead', 'developer', 'reviewer', 'frontend-dev', 'weirdrole'].map((r) => M.avatarFile('claude-agenttalk-' + r, r === 'frontend-dev' ? '' : r));
  assert.deepEqual(files.slice(0, 3), ['hexagon-architect.png', 'hexagon-builder.png', 'hexagon-detective.png']);
  assert.equal(files[3], 'hexagon-builder.png');
  assert.match(files[4], /^hexagon-(analyst|architect|builder|detective|devops|docs|monitor|sandbox|social|translator)\.png$/);
  assert.equal(M.avatarFile('claude-agenttalk-x', '../../etc/passwd').startsWith('hexagon-'), true);
  assert.equal(M.avatarFile('same', 'zzz'), M.avatarFile('same', 'zzz'), 'stable');
});

// --------------------------------------------------------------- usage windows

const withCap = (name, cap) => agent(name, { capacity: cap });

test('usage: per runtime and window, both present, thresholds on the colour', () => {
  const rows = M.usageRows([
    withCap('claude-agenttalk-lead', capacity({ primary: 41, primaryReset: 3 * 3600, secondary: 23, secondaryReset: 4 * 86400 })),
    withCap('codex-agenttalk-developer-5', capacity({ primary: 100, primaryReset: 11 * 3600 + 40 * 60, secondary: 62 })),
  ], NOW, TZ);
  assert.deepEqual(rows.map((r) => [r.runtime, r.window, r.pct, r.tone]), [
    ['claude', '5h', 41, 'ok'], ['claude', 'weekly', 23, 'ok'], ['codex', '5h', 100, 'bad'], ['codex', 'weekly', 62, 'warn'],
  ]);
  assert.equal(rows[0].resetsLabel, 'resets 15:00');
  assert.equal(rows[2].resetsLabel, 'resets 23:40');
  assert.equal(rows[2].barPct, 100);
});

test('usage thresholds: 59 ok, 60 warn, 84 warn, 85 bad', () => {
  const tone = (pct) => M.usageRows([withCap('claude-agenttalk-lead', capacity({ primary: pct }))], NOW, TZ)[0].tone;
  assert.deepEqual([59, 59.4, 60, 84, 84.9, 85, 100].map(tone), ['ok', 'ok', 'warn', 'warn', 'warn', 'bad', 'bad']);
});

test('usage: the newest reading of a runtime wins, and disagreement is flagged', () => {
  const rows = M.usageRows([
    withCap('claude-agenttalk-lead', capacity({ primary: 30, observed: 600 })),
    withCap('claude-agenttalk-developer-2', capacity({ primary: 42, observed: 20 })),
    withCap('claude-agenttalk-reviewer-3', capacity({ primary: 10, observed: 4000 })),
  ], NOW, TZ);
  assert.equal(rows[0].pct, 42);
  assert.equal(rows[0].differs, true);
  const close = M.usageRows([
    withCap('claude-agenttalk-lead', capacity({ primary: 40, observed: 600 })),
    withCap('claude-agenttalk-developer-2', capacity({ primary: 43, observed: 20 })),
  ], NOW, TZ);
  assert.equal(close[0].differs, false, 'within 5 points');
});

test('usage: a window nobody reported says "no reading", never 0 %', () => {
  const rows = M.usageRows([withCap('claude-agenttalk-lead', capacity({ primary: 10 }))], NOW, TZ);
  assert.deepEqual(rows.map((r) => [r.window, r.noReading]), [['5h', false], ['weekly', true]]);
  assert.equal(rows[1].pct, undefined);
  const none = M.usageRows([agent('claude-agenttalk-lead')], NOW, TZ);
  assert.deepEqual(none.map((r) => r.noReading), [true, true]);
});

test('usage: a stale or already-reset reading is grey with its "as of" time', () => {
  const stale = M.usageRows([withCap('codex-agenttalk-developer-5', capacity({ primary: 90, confidence: 'stale', observed: 5400 }))], NOW, TZ)[0];
  assert.deepEqual([stale.stale, stale.tone, stale.asOfLabel], [true, 'dim', 'as of 10:30']);
  const passed = M.usageRows([withCap('codex-agenttalk-developer-5', capacity({ primary: 90, primaryReset: -60 }))], NOW, TZ)[0];
  assert.deepEqual([passed.stale, passed.resetsLabel], [true, 'reset passed']);
  const unknown = M.usageRows([withCap('codex-agenttalk-developer-5', capacity({ primary: 90, confidence: 'unknown' }))], NOW, TZ)[0];
  assert.equal(unknown.stale, true);
});

test('usage: only runtimes on the team get rows; qwen never does; junk is ignored', () => {
  assert.deepEqual(M.usageRows([agent('qwen-agenttalk-dev-1', { capacity: capacity({ primary: 5 }) })], NOW, TZ), []);
  assert.deepEqual(M.usageRows([], NOW, TZ), []);
  assert.deepEqual(M.usageRows(null, NOW, TZ), []);
  const rows = M.usageRows([withCap('claude-agenttalk-lead', { primary: { used_pct: 'lots' }, secondary: null })], NOW, TZ);
  assert.deepEqual(rows.map((r) => r.noReading), [true, true]);
  assert.equal(M.usageRows([{ name: 'plain', cli: 'codex', capacity: capacity({ primary: 12 }) }], NOW, TZ)[0].runtime, 'codex');
});

// ------------------------------------------------------- freshness, two banners

const FRESH_ROOT = () => root({ agents: [agent('claude-agenttalk-lead', { hb: 30 })] });

test('sourceAsOf is the newest heartbeat, health, capacity or envelope time', () => {
  const r = root({
    agents: [agent('a-b-c', { hb: 900, capacity: capacity({ primary: 1, observed: 500 }) })],
    recent: [env('x', 'y', 'message', 200)],
  });
  r.agents[0].health.updated_at = iso(700);
  assert.equal(M.sourceAsOf(r), NOW - 200e3);
  assert.equal(M.sourceAsOf(root({ agents: [agent('a-b-c', { hb: 900 })] })), NOW - 10e3);
  assert.equal(M.sourceAsOf({ agents: [{ name: 'x' }] }), null);
  assert.equal(M.sourceAsOf(null), null);
});

test('live: server reachable and something wrote within 5 minutes', () => {
  const f = M.freshness(FRESH_ROOT(), CONN_OK, NOW, TZ);
  assert.equal(f.state, 'live');
  assert.equal(f.banner, null);
});

test('silent: the server answers but nothing has been written for over 5 minutes (5:00 fine, 5:01 silent)', () => {
  const at = (age) => root({ agents: [agent('claude-agenttalk-lead', { hb: age })] });
  const quiet = (age) => { const r = at(age); r.agents[0].health.updated_at = iso(age); return M.freshness(r, CONN_OK, NOW, TZ); };
  assert.equal(quiet(300).state, 'live');
  const s = quiet(301);
  assert.equal(s.state, 'silent');
  assert.equal(s.banner.kind, 'silent');
  assert.match(s.banner.kicker, /^NO AGENT HAS REPORTED FOR 5M$/);
  const long = quiet(3 * 3600 + 12 * 60);
  assert.equal(long.banner.kicker, 'NO AGENT HAS REPORTED FOR 3H 12M');
  assert.match(long.banner.message, /server answers/);
  assert.match(long.banner.message, /08:48/);
});

test('no timestamps at all is silent, not live', () => {
  const f = M.freshness({ agents: [{ name: 'x' }] }, CONN_OK, NOW, TZ);
  assert.equal(f.state, 'silent');
  assert.match(f.banner.message, /No agent has written/);
});

test('unreachable: a failed read, or generated_at stalled for more than 3 polls, beats silent', () => {
  const down = M.freshness(FRESH_ROOT(), { reachable: false, stalledPolls: 0, lastOkMs: NOW - 192 * 60e3 }, NOW, TZ);
  assert.equal(down.state, 'unreachable');
  assert.equal(down.banner.kicker, 'CAN\u2019T REACH THE CONSOLE SERVER');
  assert.match(down.banner.message, /Last snapshot 08:48 \(3h 12m ago\)/);
  assert.equal(M.freshness(FRESH_ROOT(), { reachable: true, stalledPolls: 3, lastOkMs: NOW }, NOW, TZ).state, 'live');
  assert.equal(M.freshness(FRESH_ROOT(), { reachable: true, stalledPolls: 4, lastOkMs: NOW }, NOW, TZ).state, 'unreachable');
  const never = M.freshness(FRESH_ROOT(), { reachable: false, stalledPolls: 0, lastOkMs: null }, NOW, TZ);
  assert.match(never.banner.message, /No snapshot has arrived yet/);
  const both = M.freshness(root({ agents: [] }), { reachable: false, stalledPolls: 0, lastOkMs: NOW }, NOW, TZ);
  assert.equal(both.state, 'unreachable');
});

test('recovery: a fresh snapshot clears the banner', () => {
  const r = FRESH_ROOT();
  assert.equal(M.freshness(r, { reachable: false, lastOkMs: NOW - 60e3 }, NOW, TZ).state, 'unreachable');
  assert.equal(M.freshness(r, { reachable: true, stalledPolls: 0, lastOkMs: NOW }, NOW, TZ).state, 'live');
});

// ---------------------------------------------------------------- team view

const team = (o = {}) => M.buildTeamView({
  nowMs: NOW, generatedMs: NOW, root: root({ agents: busyAgents(), recent: busyRecent(), operator_facing: 'claude-agenttalk-lead', ...o.root }),
  attention: o.attention === undefined ? attention([]) : o.attention, chat: o.chat === undefined ? null : o.chat,
  conn: o.conn || CONN_OK, ui: o.ui || {}, tz: TZ,
});
const escalation = (o) => ATT_ITEM({ source: 'escalation', ...o });

test('busy day: stuck first, then oldest; greeting counts the cards', () => {
  const v = team({
    attention: attention([
      escalation({ id: 'e-new', title: 'Newer question', age: 300 }),
      escalation({ id: 'e-old', title: 'Old question', age: 172800 }),
      ATT_ITEM({ id: 'g-1', source: 'gate', source_label: 'GATE HOLD', title: 'Gate holds', age: 5000 }),
    ]),
  });
  assert.equal(v.mode, 'busy');
  assert.deepEqual(v.needs.open.map((c) => c.id), ['stuck:codex-agenttalk-developer-4', 'e-old', 'g-1', 'e-new']);
  assert.equal(v.greeting.text, 'Four things need you.');
  assert.equal(v.greeting.sub, 'Stuck agents first, then oldest. A deadline only shows if someone set one.');
  assert.equal(v.chip.needsCount, 4);
  const [stuck, old] = v.needs.open;
  assert.deepEqual([stuck.kind, stuck.tone, stuck.title, stuck.ageLabel], ['LOOKS STUCK', 'bad', 'dev-4 has gone quiet', '14m']);
  assert.equal(stuck.evidence, 'Last progress 14m ago \u00b7 no reply sent \u00b7 heartbeat still fresh');
  assert.equal(stuck.evidenceNote, 'Weaker evidence: process status isn\u2019t visible yet, so Wait comes first.');
  assert.deepEqual(stuck.options.map((o) => [o.label, o.primary, o.locked]), [['Wait 10 min', true, null], ['Restart with context', false, 'CLI only']]);
  assert.equal(old.ageLabel, 'no deadline \u00b7 waiting 2d');
});

test('greeting words: One thing, Three things, digits after ten', () => {
  const n = (k) => team({ attention: attention(Array.from({ length: k }, (_, i) => escalation({ id: 'e' + i, age: 100 + i })).concat([])), root: { agents: [agent('claude-agenttalk-lead')] } }).greeting.text;
  assert.equal(n(1), 'One thing needs you.');
  assert.equal(n(3), 'Three things need you.');
  assert.equal(n(10), 'Ten things need you.');
  assert.equal(n(11), '11 things need you.');
});

test('kinds: escalation -> DECISION, gate -> GATE HOLD, others keep their label, none dropped', () => {
  const v = team({
    root: { agents: [agent('claude-agenttalk-lead')] },
    attention: attention([
      escalation({ id: 'a' }),
      ATT_ITEM({ id: 'b', source: 'gate', source_label: 'GATE HOLD' }),
      ATT_ITEM({ id: 'c', source: 'supervisor', source_label: 'SUPERVISOR HOLD', severity: 'high' }),
      ATT_ITEM({ id: 'd', source: 'deadletter', source_label: 'DEAD LETTER', severity: 'med' }),
      ATT_ITEM({ id: 'e', source: 'coordination_stall', source_label: 'TEAM STALL' }),
      ATT_ITEM({ id: 'f', source: 'other', source_label: 'OTHER', severity: 'low' }),
      ATT_ITEM({ id: 'g', source: 'brand_new_source', source_label: '', severity: 'med' }),
    ]),
  });
  const byId = Object.fromEntries(v.needs.open.map((c) => [c.id, c]));
  assert.deepEqual(['a', 'b', 'c', 'd', 'e', 'f', 'g'].map((id) => byId[id].kind),
    ['DECISION', 'GATE HOLD', 'SUPERVISOR HOLD', 'DEAD LETTER', 'TEAM STALL', 'OTHER', 'BRAND_NEW_SOURCE']);
  assert.deepEqual(['a', 'b', 'c'].map((id) => byId[id].tone), ['info', 'warn', 'warn']);
});

test('low-severity known sources are "also happening", not cards', () => {
  const v = team({ root: { agents: [agent('claude-agenttalk-lead')] },
    attention: attention([ATT_ITEM({ id: 'l', source: 'supervisor', source_label: 'SUPERVISOR', severity: 'low', title: 'Lead not armed', detail: 'no lead loop' })]) });
  assert.equal(v.needs.open.length, 0);
  assert.deepEqual(v.aside.rows.map((r) => [r.title, r.detail]), [['Lead not armed', 'no lead loop']]);
});

test('the server\u2019s own stuck items are not shown as cards (the evidence rule decides)', () => {
  const v = team({ root: { agents: [agent('claude-agenttalk-lead')] },
    attention: attention([ATT_ITEM({ id: 'stuck:x', source: 'stuck', source_label: 'STUCK', severity: 'med' })]) });
  assert.equal(v.needs.open.length, 0);
});

test('evidence is required on every card: missing evidence says so, the card stays', () => {
  const v = team({ root: { agents: [agent('claude-agenttalk-lead')] },
    attention: attention([escalation({ id: 'x', detail: '' }), escalation({ id: 'y', detail: 'because' })]) });
  const [x, y] = ['x', 'y'].map((id) => v.needs.open.find((c) => c.id === id));
  assert.deepEqual([x.evidenceMissing, x.evidenceText], [true, 'No evidence recorded']);
  assert.deepEqual([y.evidenceMissing, y.evidenceText], [false, 'because']);
});

test('options: locked "CLI only" without actions; the served options when answerable', () => {
  const v = team({ root: { agents: [agent('claude-agenttalk-lead')] },
    attention: attention([escalation({ id: 'ro' }), escalation({ id: 'rw', answerable: true, options: ['Raise to 54', 'Keep 44'] })]) });
  const ro = v.needs.open.find((c) => c.id === 'ro');
  const rw = v.needs.open.find((c) => c.id === 'rw');
  assert.deepEqual(ro.options.map((o) => [o.label, o.locked]), [['Answer', 'CLI only']]);
  assert.deepEqual(rw.options.map((o) => [o.label, o.primary, o.locked]), [['Raise to 54', true, null], ['Keep 44', false, null]]);
  assert.equal(rw.answerable, true);
});

test('card ages keep growing after the attention read; unknown age says so', () => {
  const later = M.buildTeamView({ nowMs: NOW + 120e3, generatedMs: NOW, root: root({ agents: [agent('claude-agenttalk-lead')] }),
    attention: attention([escalation({ id: 'a', age: 60 }), escalation({ id: 'b', age_unknown: true, age: 0 })], { asOfMs: NOW }),
    chat: null, conn: CONN_OK, ui: {}, tz: TZ });
  const byId = Object.fromEntries(later.needs.open.map((c) => [c.id, c]));
  assert.equal(byId.a.ageLabel, 'no deadline \u00b7 waiting 3m');
  assert.equal(byId.b.ageLabel, 'age unknown');
});

test('later, snooze and answered: deferred stays counted, snoozed reappears, none is dismissed', () => {
  const items = attention([escalation({ id: 'a', age: 900 }), escalation({ id: 'b', age: 800 }), escalation({ id: 'c', age: 700 })]);
  const ui = { deferred: { a: true }, answered: { c: true }, snoozedUntil: { 'stuck:codex-agenttalk-developer-4': NOW + 600e3 } };
  const v = team({ attention: items, ui });
  assert.deepEqual(v.needs.open.map((c) => c.id), ['b']);
  assert.equal(v.needs.deferredCount, 1);
  assert.deepEqual(v.needs.deferredCards.map((c) => c.id), ['a']);
  assert.deepEqual(v.needs.answered.map((c) => [c.id, c.state]), [['c', 'answered']]);
  assert.equal(v.needs.snoozed.length, 1);
  assert.ok(v.aside.rows.some((r) => r.title === 'dev-4 \u00b7 waiting' && r.detail === 'Snoozed until 12:10'));
  const expired = team({ ui: { snoozedUntil: { 'stuck:codex-agenttalk-developer-4': NOW - 1 } } });
  assert.ok(expired.needs.open.some((c) => c.id === 'stuck:codex-agenttalk-developer-4'), 'the card comes back when the snooze ends');
});

test('quiet day: greeting, idle count, since-you-last-looked; idle is grey', () => {
  const agents = ['a', 'b', 'c', 'd'].map((x) => agent('claude-agenttalk-developer-' + x.charCodeAt(0), { since: 3000 }));
  agents.push(agent('claude-agenttalk-lead', { state: 'working_turn', since: 60, task: 'lint' }));
  const recent = [
    env('claude-agenttalk-lead', 'operator', 'message', 600),
    env('claude-agenttalk-reviewer-3', 'claude-agenttalk-lead', 'review-result', 1200),
    env('claude-agenttalk-developer-1', 'claude-agenttalk-lead', 'task-response', 1300),
    env('claude-agenttalk-developer-1', 'claude-agenttalk-lead', 'task-response', 1400),
    env('claude-agenttalk-developer-1', 'claude-agenttalk-lead', 'message', 90000),
  ];
  const lastVisit = NOW - 6 * 3600e3;
  const v = team({ root: { agents, recent }, ui: { lastVisitMs: lastVisit } });
  assert.equal(v.mode, 'quiet');
  assert.equal(v.greeting.text, 'All quiet.');
  assert.equal(v.greeting.sub, 'Nothing needs you. 4 of 5 agents are idle \u2014 that\u2019s their resting state; they wake when the lead messages them.');
  assert.equal(v.roster.summary, '4 idle \u00b7 that\u2019s normal');
  assert.equal(v.since.title, 'SINCE YOU LAST LOOKED \u00b7 06:00');
  assert.deepEqual(v.since.rows, [
    { title: '4 messages', detail: 'exchanged since 06:00' },
    { title: '1 review result', detail: 'posted since 06:00' },
    { title: '2 task responses', detail: 'posted since 06:00' },
  ]);
  assert.ok(v.roster.rows.filter((r) => r.state === 'idle').every((r) => r.tone === 'dim'));
});

test('quiet day without a last visit looks back 24 hours; a full window is "at least"', () => {
  const full = Array.from({ length: 25 }, (_, i) => env('claude-agenttalk-lead', 'operator', 'message', 60 + i * 60));
  const v = team({ root: { agents: [agent('claude-agenttalk-lead')], recent: full }, ui: {} });
  assert.equal(v.since.title, 'IN THE LAST 24 HOURS');
  assert.equal(v.since.rows[0].title, '25+ messages');
  const none = team({ root: { agents: [agent('claude-agenttalk-lead')], recent: [] }, ui: {} });
  assert.deepEqual(none.since.rows, []);
});

test('deferred-only and answered-only days do not say "All quiet"', () => {
  const one = attention([escalation({ id: 'a' })]);
  const deferred = team({ root: { agents: [agent('claude-agenttalk-lead')] }, attention: one, ui: { deferred: { a: true } } });
  assert.equal(deferred.mode, 'deferred');
  assert.equal(deferred.greeting.text, 'Nothing new needs you.');
  assert.equal(deferred.greeting.sub, '1 item is deferred: still open, not dismissed.');
  const answered = team({ root: { agents: [agent('claude-agenttalk-lead')] }, attention: one, ui: { answered: { a: true } } });
  assert.deepEqual([answered.mode, answered.greeting.text], ['answered', 'That\u2019s everything.']);
});

test('a quiet-but-not-stuck agent keeps the day "calm", not "All quiet"', () => {
  const v = team({ root: { agents: [sil({ since: 1800, progress: 840 })], recent: [env(NAME, 'x', 'message', 700)] } });
  assert.equal(v.mode, 'calm');
  assert.equal(v.greeting.text, 'Nothing needs you right now.');
});

test('offline: both truths, greeting and stamps', () => {
  const unreachable = team({ conn: { reachable: false, stalledPolls: 0, lastOkMs: NOW - 192 * 60e3 } });
  assert.equal(unreachable.mode, 'offline');
  assert.equal(unreachable.stale, true);
  assert.equal(unreachable.banner.kind, 'unreachable');
  assert.equal(unreachable.greeting.text, 'Can\u2019t see the team.');
  assert.match(unreachable.greeting.sub, /greyed and stamped as of \d\d:\d\d\. Nothing is live, and nothing you press can reach the lead until the console server is back\./);
  assert.match(unreachable.roster.summary, /^frozen \u00b7 as of \d\d:\d\d$/);
  assert.equal(unreachable.chip.freshness, 'unreachable');

  const old = busyAgents().map((a) => ({ ...a, last_seen: iso(1200), health: { ...a.health, updated_at: iso(1200) } }));
  old.forEach((a) => { a.capacity && (a.capacity.observed_at = iso(1200)); });
  const silent = team({ root: { agents: old, recent: [env('x', 'y', 'message', 1200)] } });
  assert.equal(silent.banner.kind, 'silent');
  assert.match(silent.greeting.sub, /Nothing is live until an agent writes again\./);
  assert.equal(silent.chip.freshness, 'silent');
});

test('the last known data stays visible while offline', () => {
  const v = team({ conn: { reachable: false, stalledPolls: 0, lastOkMs: NOW - 5000 }, attention: attention([escalation({ id: 'a' })]) });
  assert.equal(v.needs.open.length > 0, true);
  assert.equal(v.roster.rows.length, 9);
});

test('attention problems are stated, not hidden: stale read, failed read, never loaded', () => {
  const stale = team({ attention: attention([escalation({ id: 'a' })], { asOfMs: NOW - 9000 }) });
  assert.equal(stale.needs.stale, true);
  assert.equal(stale.stale, true);
  const fresh = team({ attention: attention([escalation({ id: 'a' })], { asOfMs: NOW - 8000 }) });
  assert.equal(fresh.needs.stale, false);
  const failed = team({ attention: attention([], { ok: false }) });
  assert.deepEqual([failed.mode, failed.greeting.text, failed.chip.needsCount], ['needs-unavailable', 'Can\u2019t read what needs you.', null]);
  const loading = team({ attention: null });
  assert.deepEqual([loading.mode, loading.greeting.sub, loading.chip.needsCount], ['loading', 'Waiting for the first snapshot.', null]);
});

test('an unreadable root says only that; a root without agents yet is loading', () => {
  const bad = M.buildTeamView({ nowMs: NOW, root: { label: 'x', project_id: 'p', errors: ['C:\\secret\\path failed'] }, conn: CONN_OK, ui: {}, tz: TZ });
  assert.equal(bad.mode, 'error');
  assert.equal(bad.greeting.text, 'Can\u2019t read this team.');
  assert.ok(!JSON.stringify(bad).includes('secret'), 'the error text (which can carry paths) is never carried into the view');
  assert.equal(bad.chip.freshness, 'error');
  const loading = M.buildTeamView({ nowMs: NOW, root: { label: 'x', project_id: 'p' }, conn: CONN_OK, ui: {}, tz: TZ });
  assert.equal(loading.mode, 'loading');
});

test('lead\u2019s latest message: the newest from the lead, bounded, with its age', () => {
  const msgs = [
    { from: 'claude-agenttalk-lead', to: 'operator', body: 'older', ts: iso(900) },
    { from: 'operator', to: 'claude-agenttalk-lead', body: 'my question', ts: iso(500) },
    { from: 'claude-agenttalk-lead', to: 'operator', body: 'Three things need you.', ts: iso(240) },
  ];
  const v = team({ chat: chat(msgs) });
  assert.deepEqual([v.lead.short, v.lead.body, v.lead.ageLabel, v.lead.truncated], ['lead', 'Three things need you.', '4m ago', false]);
  const long = team({ chat: chat([{ from: 'claude-agenttalk-lead', body: 'x'.repeat(5000), ts: iso(1) }]) });
  assert.equal(long.lead.body.length, 1200);
  assert.equal(long.lead.truncated, true);
  assert.equal(team({ chat: chat([{ from: 'operator', body: 'hi', ts: iso(1) }]) }).lead, null, 'no lead message -> nothing invented');
  assert.equal(team({ chat: null }).lead, null);
  const down = team({ chat: chat([], { available: false, detail: 'lead heartbeat stale' }) });
  assert.deepEqual([down.lead.unavailable, down.lead.detail], [true, 'lead heartbeat stale']);
});

test('roster: lead first, short names, ties broken, summary, count', () => {
  const v = team();
  assert.equal(v.roster.total, 9);
  assert.equal(v.roster.rows[0].name, 'claude-agenttalk-lead');
  assert.deepEqual(v.roster.rows.map((r) => r.short), ['lead', 'dev-2', 'fe-dev', 'rev-3', 'dev-5', 'dev-4', 'x.rev-1', 'dev-1', 'q.rev-1']);
  assert.equal(v.roster.summary, '4 idle \u00b7 that\u2019s normal');
  assert.equal(v.roster.rows.find((r) => r.short === 'x.rev-1').cap, 'resets 23:40');
});

test('also happening lists down, capped, then quiet agents, cut at 8 with a count', () => {
  const v = team();
  assert.deepEqual(v.aside.rows.map((r) => r.title), ['x.rev-1 is capped', 'dev-5 is quiet, not stuck']);
  const many = Array.from({ length: 10 }, (_, i) => agent('claude-agenttalk-developer-' + (i + 1), { state: 'crashed_or_exited', since: 60 }));
  const crowded = team({ root: { agents: many, recent: [] } });
  assert.equal(crowded.aside.rows.length, 8);
  assert.equal(crowded.aside.more, 2);
});

test('the chip carries freshness and the open count for the selected and other teams', () => {
  const shell = M.buildShellView({
    roots: [root({ project_id: 'a', agents: busyAgents(), recent: busyRecent() }), root({ label: 'second', project_id: 'b', agents: [agent('claude-second-lead')] })],
    attentionByRoot: { a: attention([escalation({ id: 'e1' })]) }, chatByRoot: {},
    param: 'b', nowMs: NOW, generatedMs: NOW, conn: CONN_OK, ui: {}, tz: TZ,
  });
  assert.deepEqual(shell.teams.map((t) => [t.label, t.pressed, t.freshness, t.needsCount]),
    [['agenttalk', false, 'live', 2], ['second', true, 'live', null]]);
  assert.equal(shell.view.label, 'second');
  const unknown = M.buildShellView({ roots: [root({ agents: [agent('claude-agenttalk-lead')] })], param: 'nope', nowMs: NOW, conn: CONN_OK, ui: {}, tz: TZ });
  assert.deepEqual([unknown.selection.status, unknown.view], ['unknown', null]);
  const none = M.buildShellView({ roots: null, param: '', nowMs: NOW, conn: CONN_OK, ui: {}, tz: TZ });
  assert.deepEqual([none.teams, none.view], [[], null]);
});

test('hostile strings pass through as plain data; nothing is escaped or executed by the model', () => {
  const evil = '<img src=x onerror=alert(1)>';
  const v = team({ root: { agents: [agent('claude-agenttalk-lead', { task: evil, state: 'working_turn', since: 5 })] },
    attention: attention([escalation({ id: evil, title: evil, detail: evil, agent: evil })]),
    chat: chat([{ from: 'claude-agenttalk-lead', body: evil, ts: iso(1) }]) });
  assert.equal(v.needs.open[0].title, evil);
  assert.equal(v.lead.body, evil);
  assert.ok(v.roster.rows[0].line.includes(evil));
});

test('the view is plain JSON: no functions, no DOM, no undefined holes that break the redraw signature', () => {
  const v = team({ chat: chat([{ from: 'claude-agenttalk-lead', body: 'hi', ts: iso(5) }]), attention: attention([escalation({ id: 'a' })]) });
  assert.equal(JSON.stringify(v), JSON.stringify(JSON.parse(JSON.stringify(v))));
});

run();
