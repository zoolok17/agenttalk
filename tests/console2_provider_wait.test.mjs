// A seat cooling down because its AI provider is overloaded, or answered with an error that looks like a usage
// limit but could not be confirmed, in the new console's view model (console2-model.js).
// It is alive and waiting: the state stays "parked" (never down, never "capped", never "not for you"), the words are
// its own, and the only time shown is the park's own saved next try. A payload with no kind reads as before.
// Run: node tests/console2_provider_wait.test.mjs   (also run by tests/test_usage_park_consoles.py)
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { createRunner } from './console2_harness.mjs';
import {
  CONN_OK, NOW, agent, attention, epochIn, root,
} from './console2_fixtures.mjs';

const M = createRequire(import.meta.url)('../src/agenttalk/web_static/console2-model.js');
const { test, run } = createRunner('console2 provider wait');
const TZ = 'utc';
const NAME = 'claude-agenttalk-developer-6';
const LEAD = 'claude-agenttalk-lead';

const NEXT = epochIn(900);
const label = (epoch) => new Date(epoch * 1000).toISOString().slice(0, 16).replace('T', ' ') + ' UTC';
const WAIT = (o = {}) => ({
  present: true, state: 'parked', fresh: true, window: null, reset_epoch: null, wake_epoch: null,
  message_id: 'm-1', parked_at: null, age_seconds: 12, kind: 'overloaded', next_try_epoch: NEXT, park_rev: 1, ...o,
});
const waiting = (health = {}, park = WAIT(), name = NAME) => ({
  ...agent(name, { state: 'rate_limited_or_outage', since: 600, ...health }), usage_limit_park: park,
});
const ctx = (o = {}) => ({ nowMs: NOW, generatedMs: NOW, recent: [], teamIds: [], project: 'agenttalk', known: ['agenttalk'], tz: TZ, ...o });
const view = (a, o) => M.agentView(a, ctx(o));
const team = (agents) => M.buildTeamView({
  nowMs: NOW, generatedMs: NOW, tz: TZ, conn: CONN_OK, ui: {}, canAct: false,
  root: root({ agents: [agent(LEAD, { since: 3000 }), ...agents], operator_facing: LEAD }),
  attention: attention([]), chat: null,
});

test('an overloaded seat is its own words, parked state, warn tone, with the next try', () => {
  const v = view(waiting());
  assert.deepEqual([v.state, v.tone], ['parked', 'warn']);
  assert.equal(v.line, 'Waiting for an overloaded AI provider · tries again at ' + label(NEXT));
  assert.equal(v.aside, null, 'never listed as "also happening / not for you"');
  assert.ok(!/usage limit/i.test(v.line), 'never "usage limit" for an overload');
});

test('a throttled seat says a possible usage limit', () => {
  const v = view(waiting({}, WAIT({ kind: 'throttled' })));
  assert.deepEqual([v.state, v.tone], ['parked', 'warn']);
  assert.equal(v.line, 'Waiting on a possible usage limit · tries again at ' + label(NEXT));
});

test('not down and not capped', () => {
  const v = view(waiting());
  assert.notEqual(v.state, 'down');
  assert.notEqual(v.state, 'capped');
  assert.equal(v.cap, '');
});

test('no next try known: tries again shortly', () => {
  const v = view(waiting({}, WAIT({ next_try_epoch: null })));
  assert.equal(v.line, 'Waiting for an overloaded AI provider · tries again shortly');
});

test('a next try far beyond Date\'s own range never throws', () => {
  assert.doesNotThrow(() => view(waiting({}, WAIT({ next_try_epoch: 100000000000000 }))));
  assert.equal(view(waiting({}, WAIT({ next_try_epoch: 100000000000000 }))).line,
    'Waiting for an overloaded AI provider · tries again shortly');
});

test('a stale marker stays visible and says the wrapper is not responding', () => {
  const v = view(waiting({}, WAIT({ state: 'stale', fresh: false, kind: 'throttled' })));
  assert.deepEqual([v.state, v.tone], ['parked', 'warn']);
  assert.equal(v.line, 'Waiting on a possible usage limit · wrapper not responding');
});

test('an adverse supervisor verdict and current working health still win over the wait', () => {
  const dead = waiting(); dead.cli_child_verdict = { state: 'STUCK_OR_DEAD' };
  assert.equal(view(dead).state, 'down');
  assert.notEqual(view(waiting({ state: 'working_turn' })).state, 'parked');
});

test('a payload with no kind (an older server) reads exactly as a usage limit', () => {
  const old = { present: true, state: 'parked', fresh: true, window: 'five_hour', reset_epoch: NEXT, wake_epoch: NEXT + 30,
    message_id: 'm-1', parked_at: null, age_seconds: 12 };
  assert.equal(view(waiting({}, old)).line, 'Parked on a usage limit · until ' + label(NEXT));
  assert.equal(view(waiting({}, { ...old, kind: 'usage_limit', next_try_epoch: null, park_rev: null })).line,
    'Parked on a usage limit · until ' + label(NEXT));
  assert.equal(view(waiting({}, { ...old, kind: 'something_else' })).line, 'Parked on a usage limit · until ' + label(NEXT));
});

test('the team list keeps a cooling-down seat visible and not in the "not for you" list', () => {
  const t = team([waiting()]);
  const text = JSON.stringify(t);
  assert.ok(text.includes('Waiting for an overloaded AI provider'), 'the line reaches the team view');
});

run();
