// The conversation-size line on an agent's Capacity card in the classic console (#475).
//
// console.js is a browser IIFE with no exports, so the one pure helper is extracted by brace-matching
// (the way console_usage_park.test.mjs does) and evaluated on its own.
// Run: node tests/console_conversation_size.test.mjs   (also run by tests/test_conversation_size.py)
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

// eslint-disable-next-line no-new-func
const text = new Function(extract('conversationSizeText') + '\nreturn conversationSizeText;')();

const results = [];
function test(name, fn) {
  try { fn(); results.push([true, name]); } catch (e) { results.push([false, name, e]); }
}

test('a token reading below the limit is a plain line that says tokens of context', () => {
  const cv = { basis: 'context_tokens', value: 120000, limit: 150000, warn: false, cli: 'claude' };
  assert.equal(text(cv), 'Conversation: 120,000 tokens of context');
});

test('a token reading above the limit warns and says every turn re-sends it', () => {
  const cv = { basis: 'context_tokens', value: 160000, limit: 150000, warn: true, cli: 'claude' };
  assert.equal(text(cv), 'Conversation: 160,000 tokens of context - over 150,000: every turn re-sends it');
});

test('the wrapper reading says in this conversation, with the share only when the window is known, and its time', () => {
  const cv = { basis: 'context_tokens', scope: 'conversation', value: 160000, limit: 150000, warn: true,
    cli: 'claude', window: 1000000, percent: 16, at: '2026-11-02T10:00:00Z' };
  assert.equal(text(cv), 'Conversation: 160,000 tokens in this conversation (16% of 1,000,000, read 2026-11-02T10:00:00Z)' +
    ' - over 150,000: every turn re-sends it');
  assert.equal(text({ ...cv, window: null, percent: null, warn: false }),
    'Conversation: 160,000 tokens in this conversation (read 2026-11-02T10:00:00Z)');
});

test('the turn count says turns in total and, above the limit, that it may include earlier conversations', () => {
  const cv = { basis: 'turns_total', value: 450, limit: 300, warn: true, cli: 'codex' };
  assert.equal(text(cv), 'Conversation: 450 turns in total - over 300: may count earlier conversations');
  assert.equal(text({ basis: 'turns_total', value: 12, limit: 300, warn: false, cli: 'codex' }),
    'Conversation: 12 turns in total');
});

test('a reading of zero is shown, not hidden', () => {
  assert.equal(text({ basis: 'context_tokens', value: 0, limit: 150000, warn: false }),
    'Conversation: 0 tokens of context');
});

test('nothing is shown when the field is absent or not a finite number', () => {
  for (const bad of [undefined, null, {}, { value: 'x' }, { value: NaN }, { value: Infinity }]) {
    assert.equal(text(bad), null);
  }
});

test('only an explicit true warns', () => {
  assert.equal(text({ basis: 'turns_total', value: 500, limit: 300, warn: 'yes' }),
    'Conversation: 500 turns in total');
});

const failed = results.filter((r) => !r[0]);
for (const r of results) console.log((r[0] ? 'PASS ' : 'FAIL ') + r[1] + (r[0] ? '' : '\n' + r[2]));
console.log('classic console conversation size: ' + (results.length - failed.length) + '/' + results.length + ' passed');
process.exit(failed.length ? 1 : 0);
