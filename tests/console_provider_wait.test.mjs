// A seat cooling down because its AI provider is overloaded, or answered with an error that looks like a usage
// limit but could not be confirmed, in the classic console (console.js).
// It keeps the parked state's attention tone and style key, says what is true in its own words, and shows only the
// park's own saved next try. Older payloads (no kind) read exactly as before.
// Run: node tests/console_provider_wait.test.mjs   (also run by tests/test_usage_park_consoles.py)
import fs from 'node:fs';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const staticDir = path.join(here, '..', 'src', 'agenttalk', 'web_static');
const src = fs.readFileSync(path.join(staticDir, 'console.js'), 'utf8');

function extract(name) {
  const start = src.indexOf('function ' + name + '(');
  assert.ok(start >= 0, 'function not found in console.js: ' + name);
  const open = src.indexOf('{', start);
  let depth = 0;
  for (let j = open; j < src.length; j++) {
    if (src[j] === '{') depth++;
    else if (src[j] === '}') { depth--; if (depth === 0) return src.slice(start, j + 1); }
  }
  throw new Error('unbalanced braces for ' + name);
}
function constant(name) {
  const m = new RegExp('var ' + name + ' = [^;]+;').exec(src);
  assert.ok(m, 'constant not found in console.js: ' + name);
  return m[0];
}

const parts = [
  'var monotonicNow = function () { return 0; };',
  constant('UNWRAPPED_LIVE_STALE_AFTER_SECONDS'), constant('CLI_CHILD_HEALTHY_STATES'),
  constant('CLI_CHILD_GONE_NAMED_STATES'),
  extract('stateInfo'), extract('freshHeartbeat'), extract('cliChildVerdictIsGone'),
  extract('cliChildVerdictIsLaunching'), extract('parkTime'), extract('parkedStateInfo'),
  extract('rateLimitStateInfo'), extract('agentStateInfo'),
].join('\n');
// eslint-disable-next-line no-new-func
const api = new Function(parts + '\nreturn { agentStateInfo: agentStateInfo };')();

const NEXT = 1788948000;                         // 2026-09-09 10:00 UTC
const WHEN = '2026-09-09 10:00 UTC';
const wait = (o = {}) => ({ present: true, state: 'parked', fresh: true, window: null, reset_epoch: null,
  wake_epoch: null, kind: 'overloaded', next_try_epoch: NEXT, park_rev: 1, ...o });
const agent = (o = {}) => ({
  name: 'beta', last_seen_age_seconds: 5, health: { state: 'rate_limited_or_outage', stale: false },
  usage_limit_park: wait(), ...o,
});

const results = [];
function test(name, fn) {
  try { fn(); results.push([true, name]); } catch (e) { results.push([false, name, e]); }
}

test('an overloaded seat is "Waiting · provider busy", attention tone, same style key, with its next try', () => {
  const info = api.agentStateInfo(agent());
  assert.equal(info.label, 'Waiting · provider busy');
  assert.equal(info.key, 'usage_limit_parked');
  assert.equal(info.grp, 'attn');
  assert.equal(info.color, 'attn');
  assert.match(info.desc, new RegExp('overloaded AI provider; it tries again at ' + WHEN));
  assert.ok(!/usage limit/i.test(info.label + ' ' + info.desc), 'never "usage limit" for an overload');
});

test('a throttled seat is "Waiting · possible limit" and says it could not be confirmed', () => {
  const info = api.agentStateInfo(agent({ usage_limit_park: wait({ kind: 'throttled' }) }));
  assert.equal(info.label, 'Waiting · possible limit');
  assert.equal(info.key, 'usage_limit_parked');
  assert.match(info.desc, /possible usage limit that could not be confirmed; it tries again at/);
});

test('no next try known (a probe is running): it says shortly, never a made-up time', () => {
  const info = api.agentStateInfo(agent({ usage_limit_park: wait({ next_try_epoch: null }) }));
  assert.match(info.desc, /tries again shortly/);
  assert.ok(!/ UTC/.test(info.desc));
});

test('a stale marker stays visible and says the wrapper is not responding', () => {
  for (const kind of ['overloaded', 'throttled']) {
    const info = api.agentStateInfo(agent({ usage_limit_park: wait({ kind, state: 'stale', fresh: false }) }));
    assert.equal(info.label, 'Waiting · not responding');
    assert.equal(info.grp, 'attn');
    assert.match(info.desc, /not refreshed/);
  }
});

test('the next try far beyond Date\'s own range never throws', () => {
  assert.doesNotThrow(() => api.agentStateInfo(agent({ usage_limit_park: wait({ next_try_epoch: 100000000000000 }) })));
  assert.match(api.agentStateInfo(agent({ usage_limit_park: wait({ next_try_epoch: 100000000000000 }) })).desc, /shortly/);
});

test('it is never shown as down, exited or errored, and the supervisor verdict still wins', () => {
  const info = api.agentStateInfo(agent());
  assert.ok(info.color !== 'danger' && info.color !== 'gray');
  assert.equal(api.agentStateInfo(agent({ cli_child_verdict: { state: 'STUCK_OR_DEAD' } })).key, 'cli_child_gone');
  assert.equal(api.agentStateInfo(agent({ cli_child_verdict: { state: 'HEALTHY_IDLE' } })).key, 'usage_limit_parked');
});

test('current working health refuses the park, as for a usage limit', () => {
  for (const state of ['working_turn', 'working_silent', 'stuck_suspected']) {
    const info = api.agentStateInfo(agent({ health: { state, stale: false } }));
    assert.notEqual(info.key, 'usage_limit_parked', state);
  }
});

test('a payload with no kind (an older server) reads exactly as a usage limit', () => {
  const old = { present: true, state: 'parked', fresh: true, window: 'five_hour', reset_epoch: NEXT, wake_epoch: NEXT + 30 };
  const info = api.agentStateInfo(agent({ usage_limit_park: old }));
  assert.equal(info.label, 'Parked · usage limit');
  assert.match(info.desc, new RegExp('tries again shortly after ' + WHEN));
  const weird = api.agentStateInfo(agent({ usage_limit_park: { ...old, kind: 'something_else' } }));
  assert.equal(weird.label, 'Parked · usage limit');
  const present = api.agentStateInfo(agent({ usage_limit_park: { ...old, kind: 'usage_limit', next_try_epoch: null, park_rev: null } }));
  assert.equal(present.label, 'Parked · usage limit');
});

const failed = results.filter((r) => !r[0]);
for (const r of results) console.log((r[0] ? 'PASS ' : 'FAIL ') + r[1] + (r[0] ? '' : '\n' + r[2]));
console.log('classic console provider wait: ' + (results.length - failed.length) + '/' + results.length + ' passed');
process.exit(failed.length ? 1 : 0);
