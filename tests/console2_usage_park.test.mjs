// A seat parked on a provider usage limit, in the new console's view model (console2-model.js).
// It is alive and waiting: it needs attention, and it is never "down", never "capped" (a window
// reading) and never in the "not for you" list. A marker the wrapper stopped refreshing stays
// visible as "wrapper not responding". Current working or stuck evidence is never overridden.
// Run: node tests/console2_usage_park.test.mjs   (also run by tests/test_usage_park_consoles.py)
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { createRunner } from './console2_harness.mjs';
import {
  ATT_ITEM, CONN_OK, NOW, agent, attention, epochIn, root,
} from './console2_fixtures.mjs';

const M = createRequire(import.meta.url)('../src/agenttalk/web_static/console2-model.js');
const { test, run } = createRunner('console2 usage-limit park');
const TZ = 'utc';
const NAME = 'claude-agenttalk-developer-6';
const LEAD = 'claude-agenttalk-lead';

const RESET = epochIn(7200);
const label = (epoch) => new Date(epoch * 1000).toISOString().slice(0, 16).replace('T', ' ') + ' UTC';
const PARK = (o = {}) => ({
  present: true, state: 'parked', fresh: true, window: 'five_hour', reset_epoch: RESET, wake_epoch: RESET + 30,
  message_id: 'm-1', parked_at: null, age_seconds: 12, ...o,
});
const parked = (health = {}, park = PARK(), name = NAME) => ({
  ...agent(name, { state: 'rate_limited_or_outage', since: 600, ...health }), usage_limit_park: park,
});
const ctx = (o = {}) => ({ nowMs: NOW, generatedMs: NOW, recent: [], teamIds: [], project: 'agenttalk', known: ['agenttalk'], tz: TZ, ...o });
const view = (a, o) => M.agentView(a, ctx(o));
const team = (agents, att) => M.buildTeamView({
  nowMs: NOW, generatedMs: NOW, tz: TZ, conn: CONN_OK, ui: {}, canAct: false,
  root: root({ agents: [agent(LEAD, { since: 3000 }), ...agents], operator_facing: LEAD }),
  attention: att === undefined ? attention([]) : att, chat: null,
});
const CARD = (o = {}) => ATT_ITEM({
  id: 'usage_limit_park:' + NAME, source: 'usage_limit_park', source_label: 'PARKED', severity: 'med',
  title: NAME + ': parked on a usage limit until ' + label(RESET), agent: NAME,
  detail: 'It tries again by itself shortly after the reset time.', ...o,
});

test('a parked seat with a known time is its own state, warn tone, with the time', () => {
  const v = view(parked());
  assert.deepEqual([v.state, v.tone], ['parked', 'warn']);
  assert.equal(v.line, 'Parked on a usage limit · until ' + label(RESET));
  assert.equal(v.aside, null, 'never listed as "also happening / not for you"');
});

test('a parked seat is not down and not capped', () => {
  const v = view(parked());
  assert.notEqual(v.state, 'down');
  assert.notEqual(v.state, 'capped');
  assert.equal(v.cap, '');
});

test('no time known: until it is started again', () => {
  const v = view(parked({}, PARK({ reset_epoch: null, wake_epoch: null })));
  assert.equal(v.state, 'parked');
  assert.equal(v.line, 'Parked on a usage limit · until it is started again');
});

test('a consumed wake (known reset, no wake) also says until it is started again', () => {
  const v = view(parked({}, PARK({ wake_epoch: null })));
  assert.equal(v.line, 'Parked on a usage limit · until it is started again');
});

test('a stale marker stays visible and says the wrapper is not responding', () => {
  const v = view(parked({}, PARK({ state: 'stale', fresh: false })));
  assert.deepEqual([v.state, v.tone], ['parked', 'warn']);
  assert.equal(v.line, 'Parked on a usage limit · wrapper not responding');
});

test('the server decides: a parked view is shown whatever the heartbeat age says', () => {
  assert.equal(view(parked({ hb: 900 })).state, 'parked');
  assert.equal(view(parked({ state: 'rate_limited_or_outage', stale: true })).state, 'parked');
});

test('a payload that contradicts the precedence is refused, not shown (a park beside an adverse verdict or current work)', () => {
  const adverse = { ...parked(), cli_child_verdict: { state: 'STUCK_OR_DEAD' } };
  assert.notEqual(view(adverse).state, 'parked');
  const healthy = { ...parked(), cli_child_verdict: { state: 'HEALTHY_IDLE' } };
  assert.equal(view(healthy).state, 'parked');
  for (const state of ['working_turn', 'working_silent', 'stuck_suspected']) {
    assert.notEqual(view(parked({ state, progress: 30 }, PARK({ state: 'stale', fresh: false }))).state, 'parked', state);
  }
});

test('a stale health read does not hide the park', () => {
  const v = view(parked({ state: 'rate_limited_or_outage', stale: true }, PARK({ state: 'stale', fresh: false })));
  assert.equal(v.state, 'parked');
});

test('a fresh park beside 15-minute-old working health is the park, not "stuck"', () => {
  const a = {
    ...agent(NAME, { state: 'unknown', since: 1200, stale: true }),
    usage_limit_park: PARK(),
  };
  a.health = { ...a.health, last_known_state: 'working_turn', last_known_since: new Date(NOW - 1200e3).toISOString(),
    last_known_updated_at: new Date(NOW - 900e3).toISOString() };
  const v = view(a, { recent: [] });
  assert.deepEqual([v.state, v.tone], ['parked', 'warn']);
  assert.equal(v.stuck, null);
});

test('when the server sends no park view, the other evidence is shown as before', () => {
  // (the server sends none under an adverse verdict or under current working or stuck health)
  const a = { ...agent(NAME, { state: 'working_turn', since: 600 }), cli_child_verdict: { state: 'STUCK_OR_DEAD' } };
  assert.notEqual(view(a).state, 'parked');
  for (const state of ['working_turn', 'stuck_suspected']) {
    assert.notEqual(view(agent(NAME, { state, since: 600, progress: 30 })).state, 'parked', state);
  }
});

test('a view that is not parked or stale (for example context) is ignored', () => {
  const v = view(parked({}, PARK({ state: 'context' })));
  assert.notEqual(v.state, 'parked');
});

test('without a park view, a rate-limited seat is still capped as before', () => {
  const plain = agent(NAME, { state: 'rate_limited_or_outage', since: 600 });
  assert.equal(view(plain).state, 'capped');
  assert.equal(view({ ...plain, usage_limit_park: { present: false } }).state, 'capped');
  assert.equal(view({ ...plain, usage_limit_park: 'nope' }).state, 'capped');
});

test('the needs-you card says PARKED, carries the way to act, and counts as needing attention', () => {
  const item = { ...CARD(), recommendation: 'Start it again now: agenttalk request-restart --for X. To skip: agenttalk ack --for X --id m-1' };
  const v = team([parked()], attention([item]));
  assert.equal(v.needs.open.length, 1);
  const card = v.needs.open[0];
  assert.equal(card.kind, 'PARKED');
  assert.match(card.evidence, /agenttalk request-restart --for X/);
  assert.match(card.evidence, /agenttalk ack --for X --id m-1/);
  assert.ok(!/config/i.test(card.title + card.evidence));
  assert.equal(v.chip.needsCount, 1);
});

test('the parked seat is in the roster but never in the not-for-you list', () => {
  const v = team([parked()], attention([CARD()]));
  const row = v.roster.rows.find((r) => r.name === NAME);
  assert.equal(row.state, 'parked');
  const titles = v.aside.rows.map((r) => r.title);
  assert.ok(!titles.some((t) => /parked|capped|down/i.test(t)), titles.join(' | '));
});

test('with no attention list loaded the roster row still shows the park', () => {
  const v = team([parked()], null);
  assert.equal(v.roster.rows.find((r) => r.name === NAME).state, 'parked');
});

// #298: a parked seat follows the same rule as a server-only stalled warning. It always counts, is never "All quiet"
// and never "not for you", and no saved Later or answered choice, in any storage format, can hide it.
test('#298: the parked card is read-only, has no Later button data and is marked server-only', () => {
  const v = team([parked()], attention([{ ...CARD(), recommendation: 'Start it again now.' }]));
  const card = v.needs.open[0];
  assert.equal(card.serverOnly, true);
  assert.deepEqual(card.options, []);
  assert.equal(card.state, 'open');
});

test('#298: no saved Later or answered choice hides a parked card (boolean, timestamp and answered formats)', () => {
  const id = 'usage_limit_park:' + NAME;
  const saved = [
    { deferred: { [id]: true } },
    { deferred: { [id]: NOW + 3600e3 } },
    { deferred: { [id]: 1 } },
    { answered: { [id]: true } },
    { deferred: { [id]: true }, answered: { [id]: true } },
  ];
  for (const ui of saved) {
    const v = M.buildTeamView({
      nowMs: NOW, generatedMs: NOW, tz: TZ, conn: CONN_OK, ui, canAct: false,
      root: root({ agents: [agent(LEAD, { since: 3000 }), parked()], operator_facing: LEAD }),
      attention: attention([CARD()]), chat: null,
    });
    assert.equal(v.needs.open.length, 1, JSON.stringify(ui));
    assert.equal(v.needs.open[0].id, id, JSON.stringify(ui));
    assert.equal(v.needs.deferredCount, 0, JSON.stringify(ui));
    assert.equal(v.needs.answered.length, 0, JSON.stringify(ui));
    assert.equal(v.chip.needsCount, 1, JSON.stringify(ui));
  }
});

test('#298: a parked seat is never "All quiet" and never "Nothing needs you", even with a feed severity of low', () => {
  for (const severity of ['med', 'high', 'low']) {
    const v = team([parked()], attention([CARD({ severity })]));
    assert.equal(v.needs.open.length, 1, severity);
    assert.ok(!/All quiet|Nothing needs you|Nothing new needs you/.test(v.greeting.text), severity + ': ' + v.greeting.text);
    assert.deepEqual(v.aside.rows.map((r) => r.title).filter((t) => /parked/i.test(t)), [], severity);
  }
});

test('#298: the stalled warning and the parked card are both kept and both counted', () => {
  const v = team([parked()], {
    ...attention([
      ATT_ITEM({ id: 'stuck:alpha', source: 'stuck', source_label: 'STALLED', severity: 'med', title: 'alpha looks stalled', detail: 'x', agent: 'alpha' }),
      CARD(),
    ]),
    active_count: 2,
  });
  assert.deepEqual(v.needs.open.map((c) => c.id).sort(), ['stuck:alpha', 'usage_limit_park:' + NAME].sort());
  assert.equal(v.chip.needsCount, 2);
});

run();
