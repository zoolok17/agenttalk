// Console v2 render tests (M1): header, theme engine, keys, textContent-only.
// Run: node tests/console2_render.test.mjs   (also run by tests/test_console2_web.py)
//
// console2.js is executed in a vm context against the recording DOM stub from
// console2_harness.mjs. The stub throws (and records) on any innerHTML / outerHTML /
// insertAdjacentHTML use and on setAttribute of style, href, src or on*.
import assert from 'node:assert/strict';
import {
  createRunner, jsonResponse, loadConsole, makeDom, tick, texts, walk,
} from './console2_harness.mjs';

const { test, run } = createRunner('console2 render');
const HOSTILE = '<img src=x onerror=alert(1)>';

async function boot(opts = {}) {
  const dom = makeDom();
  const requests = [];
  const fetch = opts.fetch || (() => jsonResponse({ roots: [{ label: 'agenttalk' }] }));
  const wrapped = (url, init) => { requests.push([url, init]); return fetch(url, init); };
  const loaded = loadConsole({ dom, fetch: wrapped, storage: opts.storage, storageThrows: opts.storageThrows });
  await tick(); await tick(); await tick();
  return { dom, requests, ...loaded };
}

const header = (dom) => dom.document.getElementById('c2-header');
const buttons = (node) => walk(node).filter((n) => n.tagName === 'BUTTON');
const label = (b) => b.textContent;

test('header: brand, team chip from /api/state, four themes, disabled keys button', async () => {
  const { dom } = await boot();
  const bar = header(dom);
  assert.ok(texts(bar).includes('agenttalk'));
  const btns = buttons(bar);
  assert.deepEqual(btns.map(label), ['agenttalk', 'Midnight', 'Paper', 'Synthwave', 'Terminal', '?']);
  assert.equal(btns[0].getAttribute('aria-pressed'), 'true');
  assert.equal(btns[1].getAttribute('aria-pressed'), 'true');
  assert.equal(btns[5].disabled, true);
  assert.deepEqual(dom.violations, []);
});

test('header carries no mission progress and no spec-kitty', async () => {
  const { dom } = await boot({ fetch: () => jsonResponse({ roots: [{ label: 'agenttalk',
    spec_kitty: { missions: ['some-mission-01ABC'] } }] }) });
  const all = texts(header(dom)).join(' ').toLowerCase();
  assert.ok(!all.includes('spec-kitty') && !all.includes('mission') && !all.includes('some-mission'));
  assert.ok(!/\d+\s*\/\s*\d+/.test(all), 'no x/y progress');
});

test('one GET /api/state, no cache, no writes, no other endpoint', async () => {
  const { requests } = await boot();
  assert.equal(requests.length, 1);
  assert.equal(requests[0][0], '/api/state');
  assert.deepEqual(Object.keys(requests[0][1]), ['cache']);
  assert.equal(requests[0][1].cache, 'no-store');
});

test('hostile labels from the feed land as text and nowhere else', async () => {
  const { dom } = await boot({ fetch: () => jsonResponse({ roots: [
    { label: HOSTILE }, { label: null }, {}, { label: 42 }, null,
  ] }) });
  const chips = buttons(header(dom)).slice(0, 5).map(label);
  assert.deepEqual(chips, [HOSTILE, 'Team 2', 'Team 3', 'Team 4', 'Team 5']);
  for (const node of walk(header(dom))) {
    assert.ok(!Object.keys(node.attributes).some((k) => k.startsWith('on')), 'no on* attribute');
    assert.ok(!('style' in node.attributes) && !('href' in node.attributes));
  }
  assert.deepEqual(dom.violations, []);
});

test('selecting a team chip moves the pressed state', async () => {
  const { dom } = await boot({ fetch: () => jsonResponse({ roots: [{ label: 'main' }, { label: 'second' }] }) });
  let chips = buttons(header(dom));
  assert.deepEqual([chips[0], chips[1]].map((c) => c.getAttribute('aria-pressed')), ['true', 'false']);
  chips[1].click();
  chips = buttons(header(dom));
  assert.deepEqual([chips[0], chips[1]].map((c) => c.getAttribute('aria-pressed')), ['false', 'true']);
});

test('feed failure or bad shape shows only what is known', async () => {
  for (const fetch of [
    () => Promise.reject(new Error('down')),
    () => jsonResponse({}, 500),
    () => Promise.resolve({ ok: true, status: 200, json: () => Promise.reject(new Error('bad json')) }),
  ]) {
    const { dom } = await boot({ fetch });
    const first = buttons(header(dom))[0];
    assert.equal(label(first), 'No team data');
    assert.equal(first.disabled, true);
  }
  const { dom } = await boot({ fetch: () => jsonResponse({ roots: 'nope' }) });
  assert.equal(label(buttons(header(dom))[0]), 'Team');
});

test('theme: default applied, click persists and repaints, unknown stored value ignored', async () => {
  let { dom, store } = await boot();
  assert.equal(dom.document.documentElement.getAttribute('data-theme'), 'midnight');
  assert.equal(store.size, 0, 'loading alone writes nothing');
  const paper = buttons(header(dom)).find((b) => label(b) === 'Paper');
  paper.click();
  assert.equal(dom.document.documentElement.getAttribute('data-theme'), 'paper');
  assert.equal(store.get('agenttalk.console2.theme'), 'paper');
  const pressed = buttons(header(dom)).filter((b) => b.getAttribute('aria-pressed') === 'true').map(label);
  assert.ok(pressed.includes('Paper') && !pressed.includes('Midnight'));

  ({ dom } = await boot({ storage: { 'agenttalk.console2.theme': 'synthwave' } }));
  assert.equal(dom.document.documentElement.getAttribute('data-theme'), 'synthwave');
  ({ dom } = await boot({ storage: { 'agenttalk.console2.theme': '"><script>' } }));
  assert.equal(dom.document.documentElement.getAttribute('data-theme'), 'midnight');
});

test('theme still works when storage is unavailable', async () => {
  const { dom } = await boot({ storageThrows: true });
  assert.equal(dom.document.documentElement.getAttribute('data-theme'), 'midnight');
  buttons(header(dom)).find((b) => label(b) === 'Terminal').click();
  assert.equal(dom.document.documentElement.getAttribute('data-theme'), 'terminal');
});

test('key t cycles themes; ignored while typing and with modifiers', async () => {
  const { dom } = await boot();
  const theme = () => dom.document.documentElement.getAttribute('data-theme');
  dom.document.dispatch('keydown', { key: 't', target: { tagName: 'BODY' } });
  assert.equal(theme(), 'paper');
  dom.document.dispatch('keydown', { key: 't', target: { tagName: 'INPUT' } });
  dom.document.dispatch('keydown', { key: 't', target: { tagName: 'TEXTAREA' } });
  dom.document.dispatch('keydown', { key: 't', target: { tagName: 'DIV', isContentEditable: true } });
  dom.document.dispatch('keydown', { key: 't', ctrlKey: true, target: { tagName: 'BODY' } });
  dom.document.dispatch('keydown', { key: 'j', target: { tagName: 'BODY' } });
  assert.equal(theme(), 'paper');
  for (let i = 0; i < 3; i++) dom.document.dispatch('keydown', { key: 't', target: { tagName: 'BODY' } });
  assert.equal(theme(), 'midnight');
});

test('footer hints advertise only keys that work in this milestone', async () => {
  const { dom } = await boot();
  const hints = dom.document.getElementById('c2-hints');
  assert.deepEqual(texts(hints), ['t', 'theme']);
});

test('script without the model stays inert', async () => {
  const dom = makeDom();
  const before = header(dom).children.length;
  // No model in the context: console2.js must return before touching the page.
  const vmMod = await import('node:vm');
  const { readStatic } = await import('./console2_harness.mjs');
  const sandbox = { document: dom.document, fetch: () => { throw new Error('no fetch'); } };
  sandbox.window = sandbox;
  vmMod.createContext(sandbox);
  vmMod.runInContext(readStatic('console2.js'), sandbox);
  assert.equal(header(dom).children.length, before);
});

run();
