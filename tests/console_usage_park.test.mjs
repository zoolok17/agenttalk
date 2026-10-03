// A seat parked on a provider usage limit, in the classic console (console.js).
//
// console.js is a browser IIFE with no exports, so the few pure helpers involved are extracted by
// brace-matching (the way console_runtime_smoke.mjs does) and evaluated on their own.
// Run: node tests/console_usage_park.test.mjs   (also run by tests/test_usage_park_consoles.py)
import fs from 'node:fs';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const staticDir = path.join(here, '..', 'src', 'agenttalk', 'web_static');
const src = fs.readFileSync(path.join(staticDir, 'console.js'), 'utf8');
const css = fs.readFileSync(path.join(staticDir, 'console.css'), 'utf8');

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
  extract('agentStateInfo'),
].join('\n');
// eslint-disable-next-line no-new-func
const api = new Function(parts + '\nreturn { agentStateInfo: agentStateInfo, parkTime: parkTime };')();

const RESET = 1788948000;                       // 2026-09-09 10:00 UTC
const WHEN = '2026-09-09 10:00 UTC';
const park = (o = {}) => ({ present: true, state: 'parked', fresh: true, window: 'five_hour',
  reset_epoch: RESET, wake_epoch: RESET + 30, ...o });
const agent = (o = {}) => ({
  name: 'beta', last_seen_age_seconds: 5, health: { state: 'rate_limited_or_outage', stale: false },
  usage_limit_park: park(), ...o,
});

const results = [];
function test(name, fn) {
  try { fn(); results.push([true, name]); } catch (e) { results.push([false, name, e]); }
}

test('a parked seat is "Parked", needs attention, and is not the danger red of "Rate-limited"', () => {
  const info = api.agentStateInfo(agent());
  assert.equal(info.label, 'Parked · usage limit');
  assert.equal(info.key, 'usage_limit_parked');
  assert.equal(info.grp, 'attn');
  assert.equal(info.color, 'attn');
  assert.match(info.desc, new RegExp('tries again shortly after ' + WHEN));
});

test('no reset time known: it tries again each time it is started', () => {
  const info = api.agentStateInfo(agent({ usage_limit_park: park({ reset_epoch: null, wake_epoch: null }) }));
  assert.equal(info.label, 'Parked · usage limit');
  assert.match(info.desc, /each time it is started/);
  assert.ok(!/ UTC/.test(info.desc));
});

test('a consumed wake says the same as no time', () => {
  const info = api.agentStateInfo(agent({ usage_limit_park: park({ wake_epoch: null }) }));
  assert.match(info.desc, /each time it is started/);
});

test('a stale marker stays visible and says the wrapper is not responding', () => {
  const info = api.agentStateInfo(agent({ usage_limit_park: park({ state: 'stale', fresh: false }) }));
  assert.equal(info.label, 'Parked · not responding');
  assert.equal(info.grp, 'attn');
  assert.match(info.desc, /not refreshed/);
});

test('it is never shown as down, exited or errored', () => {
  const info = api.agentStateInfo(agent());
  for (const bad of ['crashed_or_exited', 'errored_fatal', 'errored_recoverable', 'cli_child_gone', 'unknown']) {
    assert.notEqual(info.key, bad);
  }
  assert.ok(info.color !== 'danger' && info.color !== 'gray');
});

test('the supervisor verdict that the child is gone is not hidden by the park', () => {
  const info = api.agentStateInfo(agent({ cli_child_verdict: { state: 'STUCK_OR_DEAD' } }));
  assert.equal(info.key, 'cli_child_gone');
  const healthy = api.agentStateInfo(agent({ cli_child_verdict: { state: 'HEALTHY_IDLE' } }));
  assert.equal(healthy.key, 'usage_limit_parked');
});

test('a park beside a stale health read is shown; one beside current working health is refused', () => {
  const info = api.agentStateInfo(agent({ health: { state: 'unknown', stale: true } }));
  assert.equal(info.key, 'usage_limit_parked');
  for (const state of ['working_turn', 'working_silent', 'stuck_suspected']) {
    const current = api.agentStateInfo(agent({ health: { state, stale: false }, usage_limit_park: park({ state: 'stale', fresh: false }) }));
    assert.notEqual(current.key, 'usage_limit_parked', state);
  }
  assert.equal(api.agentStateInfo(agent({ usage_limit_park: park({ state: 'context' }) })).label, 'Rate-limited');
});

test('current working health with no park view is shown as before', () => {
  const info = api.agentStateInfo(agent({ health: { state: 'working_turn', stale: false }, usage_limit_park: undefined }));
  assert.equal(info.key, 'working_turn');
});

test('without a park view a rate-limited seat reads as before', () => {
  const plain = agent({ usage_limit_park: undefined });
  const info = api.agentStateInfo(plain);
  assert.equal(info.label, 'Rate-limited');
  assert.equal(info.color, 'danger');
  assert.equal(api.agentStateInfo(agent({ usage_limit_park: { present: false } })).label, 'Rate-limited');
  assert.equal(api.agentStateInfo(agent({ usage_limit_park: 'x' })).label, 'Rate-limited');
});

test('parkTime: UTC wording, and nothing for a bad value', () => {
  assert.equal(api.parkTime(RESET), WHEN);
  for (const bad of [null, undefined, 'x', 0, -5, NaN, Infinity]) assert.equal(api.parkTime(bad), '');
});

// #311 blocker 2 / connector 4174800514: a finite, positive number is not necessarily a date
// Date can show - one far enough in the future overflows Date's own range and toISOString used
// to throw RangeError straight out of parkTime, breaking the whole chip instead of showing
// nothing for that one field.
test('parkTime: a reset far beyond Date\'s own range never throws', () => {
  assert.equal(api.parkTime(100000000000000), '');
  assert.doesNotThrow(() => api.agentStateInfo(agent({
    usage_limit_park: park({ reset_epoch: 100000000000000, wake_epoch: 100000000000030 }),
  })));
});

test('the stylesheet has the parked state in all four places a state colour is set', () => {
  for (const selector of ['.status-usage_limit_parked.tc-chip', '.status-usage_limit_parked.tc-dot',
    '.tc-timeline-seg.status-usage_limit_parked', '.status-usage_limit_parked.tc-stat-dot']) {
    assert.ok(css.includes(selector), selector);
  }
});

const failed = results.filter((r) => !r[0]);
for (const r of results) console.log((r[0] ? 'PASS ' : 'FAIL ') + r[1] + (r[0] ? '' : '\n' + r[2]));
console.log('classic console usage-limit park: ' + (results.length - failed.length) + '/' + results.length + ' passed');
process.exit(failed.length ? 1 : 0);
