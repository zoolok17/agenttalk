// v0.75.0 render smoke for the Supervisor-card Model/Effort/Runtime rows.
//
// console.js is a browser IIFE with no exports, so we extract the three tiny,
// self-contained pure helpers (el, supRow, supRuntimeRows) by brace-matching
// and evaluate them against a minimal document stub. This asserts:
//   - UNTRUSTED values (model with an <img onerror=...> payload) land in
//     textContent (inert) and NEVER in innerHTML (XSS-safe);
//   - the rows render values when present and an em-dash when unset;
//   - the Runtime cell shows "state · reset_reason".
import fs from 'node:fs';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const src = fs.readFileSync(
  path.join(here, '..', 'src', 'agenttalk', 'web_static', 'console.js'), 'utf8');

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

const combined = [
  extract('el'), extract('titled'), extract('stateInfo'),
  extract('teamHealthVerdictFrom'),
  extract('supRow'), extract('supRuntimeRows'),
].join('\n');

const document = {
  createElement(tag) {
    const node = {
      tag: tag, className: '', _text: undefined, children: [], attributes: {},
      appendChild(c) { this.children.push(c); return c; },
      setAttribute(k, v) { this.attributes[k] = String(v); },
      getAttribute(k) {
        return Object.prototype.hasOwnProperty.call(this.attributes, k) ? this.attributes[k] : null;
      },
      set textContent(v) { this._text = v; },
      get textContent() { return this._text; },
    };
    return node;
  },
};

// eslint-disable-next-line no-new-func
const factory = new Function('document',
  combined + '\nreturn { el: el, titled: titled, stateInfo: stateInfo,'
  + ' teamHealthVerdictFrom: teamHealthVerdictFrom,'
  + ' supRow: supRow, supRuntimeRows: supRuntimeRows };');
const api = factory(document);

// --- 1) XSS-safe: an attacker-controlled model renders inert via textContent ---
const XSS = '<img src=x onerror=alert(1)>';
const rows = api.supRuntimeRows({
  model: XSS, reasoning_effort: 'high',
  runtime: { state: 'resumed', reset_reason: 'runtime_config_changed' },
});
assert.equal(rows.length, 3, 'three rows: Model, Effort, Runtime');
assert.equal(rows[0].children[0].textContent, 'Model');
assert.equal(rows[1].children[0].textContent, 'Effort');
assert.equal(rows[2].children[0].textContent, 'Runtime');
// value cell carries the RAW string as textContent (inert) — never parsed as HTML
assert.equal(rows[0].children[1].textContent, XSS);
assert.equal(rows[0].children[1].innerHTML, undefined, 'innerHTML must never be set');
assert.equal(rows[1].children[1].textContent, 'high');
assert.equal(rows[2].children[1].textContent, 'resumed · runtime_config_changed');

// --- 2) unset fields show an em-dash ---
const empty = api.supRuntimeRows({});
assert.equal(empty[0].children[1].textContent, '—', 'Model unset -> em-dash');
assert.equal(empty[1].children[1].textContent, '—', 'Effort unset -> em-dash');
assert.equal(empty[2].children[1].textContent, '—', 'Runtime unset -> em-dash');

// --- 3) runtime state with no reset_reason shows just the state ---
const noReason = api.supRuntimeRows({ runtime: { state: 'fresh' } });
assert.equal(noReason[2].children[1].textContent, 'fresh');

// --- 4) titled() adds a plain-language hover tooltip; empty title is a no-op (v0.76.0) ---
const withT = api.titled(api.el('span', 'x', 'HOLD'), 'Blocked — waiting on a decision');
assert.equal(withT.getAttribute('title'), 'Blocked — waiting on a decision');
const noT = api.titled(api.el('span', 'x', 'GO'), '');
assert.equal(noT.getAttribute('title'), null, 'empty title must not set the attribute');

// --- 5) stateInfo carries a plain-language desc for every state, alongside the
//        word label (C0 legibility is never color-alone), and the amber
//        "Idle · waiting" is explicitly framed as NORMAL, not broken (v0.76.0) ---
['working_turn', 'idle_waiting', 'stuck_suspected', 'crashed_or_exited', 'unknown'].forEach((s) => {
  const info = api.stateInfo(s);
  assert.ok(info.label && info.label.length > 0, s + ' has a word label');
  assert.ok(info.desc && info.desc.length > 0, s + ' has a plain-language desc');
});
const idle = api.stateInfo('idle_waiting');
assert.ok(/normal|healthy/i.test(idle.desc),
  'idle_waiting desc must say it is normal/healthy (kills the "idle = broken" false alarm)');

// --- 6) team-health verdict: UNKNOWN attention must NOT read as a green all-clear
//        (v0.76.0 trust contract — an API outage / not-yet-loaded queue was rendering
//        a confirmed "nothing needs you"). Green only on a KNOWN, empty queue. ---
const hv = api.teamHealthVerdictFrom;  // (n, stateKnown, attnKnown, q, attnCount, unknownCount)
// attnKnown=false (queue loading/failed/stale) + fresh state + clear agents -> NOT green:
const unknown = hv(3, true, false, null, 0, 0);
assert.notEqual(unknown.tone, 'ok', 'unknown attention must not be tone=ok');
assert.notEqual(unknown.pill, 'Healthy', 'unknown attention must not read "Healthy"');
assert.ok(!/nothing needs you/.test(unknown.text), 'unknown must not claim "nothing needs you"');
assert.ok(/unknown/i.test(unknown.text), 'unknown attention should say the queue status is unknown');
// STALE STATE (agent health obsolete) even with a fresh empty queue -> NOT green (codex P1b):
const staleState = hv(3, false, true, 0, 0, 0);
assert.notEqual(staleState.tone, 'ok', 'stale agent-health state must not be tone=ok');
assert.ok(!/nothing needs you/.test(staleState.text), 'stale state must not claim all-clear');
assert.ok(/stale|reconnect/i.test(staleState.text), 'stale state should say reconnecting/stale');
// BOTH fresh + empty + healthy -> green:
const clear = hv(3, true, true, 0, 0, 0);
assert.equal(clear.tone, 'ok');
assert.equal(clear.pill, 'Healthy');
assert.ok(/nothing needs you/.test(clear.text));
// both fresh + queue has items -> danger + "need a human":
const needHuman = hv(3, true, true, 2, 1, 0);
assert.equal(needHuman.tone, 'danger');
assert.ok(/need a human/.test(needHuman.text));
// no agents -> neutral:
assert.equal(hv(0, true, false, null, 0, 0).text, 'No agents running yet');

console.log('console runtime render smoke: PASS');
