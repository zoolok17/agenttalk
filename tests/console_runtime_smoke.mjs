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

const combined = [extract('el'), extract('supRow'), extract('supRuntimeRows')].join('\n');

const document = {
  createElement(tag) {
    const node = {
      tag: tag, className: '', _text: undefined, children: [],
      appendChild(c) { this.children.push(c); return c; },
      set textContent(v) { this._text = v; },
      get textContent() { return this._text; },
    };
    return node;
  },
};

// eslint-disable-next-line no-new-func
const factory = new Function('document',
  combined + '\nreturn { el: el, supRow: supRow, supRuntimeRows: supRuntimeRows };');
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

console.log('console runtime render smoke: PASS');
