// Console v2 stream render tests (M3): every kind of needs-you card and every state of it
// (open, deferred, snoozed, locked option, empty evidence), the lead's message, the chat thread
// and the composer, all against the recording DOM. Run: node tests/console2_stream.test.mjs
import assert from 'node:assert/strict';
import { createRunner, texts, walk } from './console2_harness.mjs';
import {
  ATT_ITEM, NOW, agent, busyAgents, busyRecent, env, iso, root,
} from './console2_fixtures.mjs';
import {
  HOSTILE, LEAD, all, app, boot, chips, classOf, rail, server, stream,
} from './console2_app.mjs';

const { test, run } = createRunner('console2 stream');
const LATER = 'agenttalk.console2.later';

const calm = () => [root({ project_id: 'proj-a', agents: [agent(LEAD, { since: 3000 })], recent: [env(LEAD, 'x', 'message', 5)] })];
const att = (items) => ({ attention: () => ({ target_root_project_id: 'proj-a', items }) });
const cards = (dom) => classOf(stream(dom), 'c2-card');
const btns = (node) => walk(node).filter((n) => n.tagName === 'BUTTON');
const byText = (node, re) => btns(node).filter((b) => re.test(b.textContent));
const cardText = (card) => texts(card).join(' | ');

// ------------------------------------------------------------ card kinds

test('DECISION: kind, tone, title, evidence, age, locked answer with its CLI hint, and Later', async () => {
  const { dom } = await boot(server({ roots: calm, ...att([ATT_ITEM({ id: 'e1', title: 'Keep the old CSV export?',
    detail: 'rev-3 cold review: FIX \u00b7 two callers still use it', age: 5 * 3600 })]) }));
  const [card] = cards(dom);
  assert.ok(card.className.split(' ').includes('tone-info'));
  assert.equal(classOf(card, 'c2-kind')[0].textContent, 'DECISION');
  assert.equal(classOf(card, 'c2-age')[0].textContent, 'no deadline \u00b7 waiting 5h');
  assert.equal(classOf(card, 'c2-card-title')[0].textContent, 'Keep the old CSV export?');
  assert.equal(classOf(card, 'c2-evidence-text')[0].textContent, 'rev-3 cold review: FIX \u00b7 two callers still use it');
  const [answer] = classOf(card, 'c2-opt');
  assert.equal(answer.textContent, 'Answer \u00b7 CLI only');
  assert.equal(answer.disabled, true);
  assert.equal(answer.getAttribute('aria-disabled'), 'true');
  assert.equal(answer.getAttribute('title'), 'Not available here: CLI only');
  assert.ok(answer.className.split(' ').includes('is-locked'));
  const later = classOf(card, 'c2-later')[0];
  assert.deepEqual([later.textContent, later.disabled], ['Later', false]);
});

test('GATE HOLD, an unknown supervisor kind and OTHER are cards in the warn tone, none dropped', async () => {
  const { dom } = await boot(server({ roots: calm, ...att([
    ATT_ITEM({ id: 'g', source: 'gate', source_label: 'GATE HOLD', title: 'Gate holds', age: 900 }),
    ATT_ITEM({ id: 's', source: 'supervisor', source_label: 'SUPERVISOR HOLD', title: 'Held by supervisor', age: 800 }),
    ATT_ITEM({ id: 'o', source: 'brand_new_source', source_label: '', title: 'Unknown thing', age: 700 }),
  ]) }));
  assert.deepEqual(cards(dom).map((c) => classOf(c, 'c2-kind')[0].textContent), ['GATE HOLD', 'SUPERVISOR HOLD', 'BRAND_NEW_SOURCE']);
  assert.ok(cards(dom).every((c) => c.className.split(' ').includes('tone-warn')));
  assert.ok(all(stream(dom)).includes('Three things need you.'));
});

test('LOOKS STUCK: bad tone, fallback evidence, Wait first and live, Restart locked, weaker-evidence note', async () => {
  const { dom } = await boot(server());
  const stuck = cards(dom).find((c) => classOf(c, 'c2-kind')[0].textContent === 'LOOKS STUCK');
  assert.ok(stuck.className.split(' ').includes('tone-bad'));
  assert.equal(classOf(stuck, 'c2-card-title')[0].textContent, 'dev-4 has gone quiet');
  assert.equal(classOf(stuck, 'c2-age')[0].textContent, '14m');
  assert.equal(classOf(stuck, 'c2-evidence-text')[0].textContent, 'Last progress 14m ago \u00b7 no reply sent \u00b7 heartbeat still fresh');
  assert.equal(classOf(stuck, 'c2-note')[0].textContent, 'Weaker evidence: process status isn\u2019t visible yet, so Wait comes first.');
  const [wait, restart] = classOf(stuck, 'c2-opt');
  assert.deepEqual([wait.textContent, wait.disabled, wait.className.includes('is-primary')], ['Wait 10 min', false, true]);
  assert.deepEqual([restart.textContent, restart.disabled], ['Restart with context \u00b7 CLI only', true]);
  assert.equal(restart.getAttribute('title'), 'Not available here: CLI only');
});

test('a served answer option is shown disabled with the read-only reason; the label is visible', async () => {
  const { dom } = await boot(server({ roots: calm, ...att([ATT_ITEM({ id: 'a', answerable: true, options: ['Raise to 54 \u20ac', 'Keep 44 \u20ac'] })]) }));
  const opts = classOf(cards(dom)[0], 'c2-opt');
  assert.deepEqual(opts.map((o) => o.textContent), ['Raise to 54 \u20ac \u00b7 read-only', 'Keep 44 \u20ac \u00b7 read-only']);
  assert.ok(opts.every((o) => o.disabled === true));
  opts[0].click();   // a disabled control fires nothing
  assert.equal(cards(dom).length, 1);
});

test('empty evidence: the card stays, and says "No evidence recorded"', async () => {
  const { dom } = await boot(server({ roots: calm, ...att([ATT_ITEM({ id: 'x', detail: '' })]) }));
  const [card] = cards(dom);
  const ev = classOf(card, 'c2-evidence-text')[0];
  assert.equal(ev.textContent, 'No evidence recorded');
  assert.ok(ev.className.split(' ').includes('is-missing'));
});

test('order: stuck first, then the oldest waiting', async () => {
  const { dom } = await boot(server({ ...att([ATT_ITEM({ id: 'new', title: 'Newer', age: 100 }), ATT_ITEM({ id: 'old', title: 'Older', age: 90000 })]) }));
  assert.deepEqual(cards(dom).map((c) => classOf(c, 'c2-card-title')[0].textContent), ['dev-4 has gone quiet', 'Older', 'Newer']);
});

// ------------------------------------------------------------ Later / deferred

const one = { roots: calm, ...att([ATT_ITEM({ id: 'e1', title: 'Only question', age: 3600 })]) };
const deferredLine = (dom) => classOf(stream(dom), 'c2-deferred')[0];

test('Later defers the card: it leaves the list but stays open, counted and recoverable', async () => {
  const { dom, store } = await boot(server(one));
  assert.equal(cards(dom).length, 1);
  classOf(cards(dom)[0], 'c2-later')[0].click();
  assert.equal(cards(dom).length, 0);
  assert.equal(deferredLine(dom).textContent, '1 deferred \u00b7 still open, not dismissed \u00b7 show');
  assert.ok(all(stream(dom)).includes('Nothing new needs you.'));
  assert.ok(all(stream(dom)).includes('1 item is deferred: still open, not dismissed.'));
  assert.ok(!all(stream(dom)).includes('All quiet.'), 'a deferred item is never "all quiet"');
  const saved = JSON.parse(store.get(LATER));
  assert.deepEqual(Object.keys(saved.deferred['proj-a']), ['e1']);
  assert.equal(typeof saved.deferred['proj-a'].e1, 'number');
});

test('"show" brings every deferred card back, and the store is emptied', async () => {
  const { dom, store } = await boot(server({ roots: calm, ...att([ATT_ITEM({ id: 'a', title: 'A', age: 900 }), ATT_ITEM({ id: 'b', title: 'B', age: 800 })]) }));
  classOf(cards(dom)[0], 'c2-later')[0].click();
  classOf(cards(dom)[0], 'c2-later')[0].click();
  assert.equal(cards(dom).length, 0);
  assert.equal(deferredLine(dom).textContent, '2 deferred \u00b7 still open, not dismissed \u00b7 show');
  deferredLine(dom).click();
  assert.deepEqual(cards(dom).map((c) => classOf(c, 'c2-card-title')[0].textContent), ['A', 'B']);
  assert.equal(deferredLine(dom), undefined);
  assert.deepEqual(Object.keys(JSON.parse(store.get(LATER)).deferred['proj-a'] || {}), []);
});

test('a deferral survives a reload of the page', async () => {
  const first = await boot(server(one));
  classOf(cards(first.dom)[0], 'c2-later')[0].click();
  const { dom } = await boot(server(one), { storage: { [LATER]: first.store.get(LATER) } });
  assert.equal(cards(dom).length, 0);
  assert.equal(deferredLine(dom).textContent, '1 deferred \u00b7 still open, not dismissed \u00b7 show');
});

test('deferring in one team does not defer the same id in another', async () => {
  const two = () => [root({ project_id: 'a', agents: [agent(LEAD)], recent: [env(LEAD, 'x', 'message', 5)] }),
    root({ label: 'second', project_id: 'b', agents: [agent('claude-second-lead')], recent: [env('claude-second-lead', 'x', 'message', 5)] })];
  const srv = server({ roots: two, attention: (id) => ({ target_root_project_id: id, items: [ATT_ITEM({ id: 'same', title: 'Shared id' })] }),
    chat: (id) => ({ target_root_project_id: id, available: true, lead: LEAD, messages: [] }) });
  const { dom } = await boot(srv);
  classOf(cards(dom)[0], 'c2-later')[0].click();
  assert.equal(cards(dom).length, 0);
  chips(dom)[1].click();
  for (let i = 0; i < 12; i++) await new Promise((r) => setTimeout(r, 0));
  assert.equal(cards(dom).length, 1, 'the second team still shows its card');
});

test('the needs badge counts open cards only; deferred ones are on the deferred line', async () => {
  const { dom } = await boot(server({ roots: calm, ...att([ATT_ITEM({ id: 'a', age: 900 }), ATT_ITEM({ id: 'b', age: 800 })]) }));
  assert.equal(chips(dom)[0].children[2].textContent, '2');
  classOf(cards(dom)[0], 'c2-later')[0].click();
  assert.equal(chips(dom)[0].children[2].textContent, '1');
  assert.equal(deferredLine(dom).textContent, '1 deferred \u00b7 still open, not dismissed \u00b7 show');
});

test('Later still works when storage is unavailable, for this page', async () => {
  const { dom } = await boot(server(one), { storageThrows: true });
  classOf(cards(dom)[0], 'c2-later')[0].click();
  assert.equal(cards(dom).length, 0);
  deferredLine(dom).click();
  assert.equal(cards(dom).length, 1);
});

test('hostile or junk stored state is ignored, never trusted', async () => {
  const junk = [
    'not json', '[]', '"x"', 'null', '{"deferred":[1,2],"snoozed":"x"}',
    '{"deferred":{"proj-a":{"e1":"soon","__proto__":1}},"snoozed":{"proj-a":{"e1":null}}}',
    '{"deferred":{"proj-a":{"e1":' + (Date.now() - 400 * 86400e3) + '}}}',      // older than 30 days
    '{"deferred":{"__proto__":{"e1":' + Date.now() + '}}}',
  ];
  for (const raw of junk) {
    const { dom } = await boot(server(one), { storage: { [LATER]: raw } });
    assert.equal(cards(dom).length, 1, raw);
    assert.equal(deferredLine(dom), undefined, raw);
    assert.equal({}.e1, undefined, 'no prototype pollution');
  }
});

test('a card id that is "__proto__" is just an id', async () => {
  const { dom } = await boot(server({ roots: calm, ...att([ATT_ITEM({ id: '__proto__', title: 'Odd id' })]) }));
  classOf(cards(dom)[0], 'c2-later')[0].click();
  assert.equal(cards(dom).length, 0);
  assert.equal(({}).polluted, undefined);
  deferredLine(dom).click();
  assert.equal(cards(dom).length, 1);
});

// ------------------------------------------------------------------ Wait 10 min

test('Wait 10 min hides the stuck card, lists it as waiting, and the card returns when the wait ends', async () => {
  // keep every heartbeat fresh as the clock moves, so only the snooze decides whether the card shows
  const srv = server({ roots: () => [root({ project_id: 'proj-a', operator_facing: LEAD, recent: busyRecent(),
    agents: busyAgents().map((a) => ({ ...a, last_seen: new Date(NOW + srv.clock.perf - 20e3).toISOString() })) })] });
  const { dom, clock, fire, store } = await boot(srv);
  const stuck = () => cards(dom).find((c) => classOf(c, 'c2-kind')[0].textContent === 'LOOKS STUCK');
  byText(stuck(), /^Wait 10 min$/)[0].click();
  assert.equal(stuck(), undefined);
  assert.ok(all(stream(dom)).includes('dev-4 \u00b7 waiting'));
  assert.ok(/Snoozed until \d\d:\d\d/.test(all(stream(dom))));
  assert.deepEqual(Object.keys(JSON.parse(store.get(LATER)).snoozed['proj-a']), ['stuck:codex-agenttalk-developer-4']);
  clock.perf += 9 * 60e3;
  await fire((ms) => ms < 5000);
  assert.equal(stuck(), undefined, 'still waiting after 9 minutes');
  clock.perf += 2 * 60e3;
  await fire((ms) => ms < 5000);
  assert.ok(stuck(), 'back after 11 minutes');
});

// ------------------------------------------------------------ lead message and chat

const THREAD = [
  { id: 'c1', from: 'operator', to: LEAD, body: 'Any news?', ts: iso(900) },
  { id: 'c2', from: LEAD, to: 'operator', body: 'Three things need you.', ts: iso(600) },
];
const chatOf = (messages) => ({ chat: () => ({ target_root_project_id: 'proj-a', available: true, operator: 'operator', lead: LEAD, messages }) });

test('the lead\u2019s latest message: avatar ring initials, body, who and when', async () => {
  const { dom } = await boot(server({ roots: calm, ...chatOf(THREAD) }));
  const lead = classOf(stream(dom), 'c2-lead')[0];
  assert.equal(classOf(lead, 'c2-lead-avatar')[0].textContent, 'LE');
  assert.equal(classOf(lead, 'c2-lead-body')[0].textContent, 'Three things need you.');
  assert.equal(classOf(lead, 'c2-meta')[0].textContent, 'lead \u00b7 10m ago');
});

test('the chat thread: both sides, yours on the right, oldest first', async () => {
  const { dom } = await boot(server({ roots: calm, ...chatOf(THREAD) }));
  const msgs = classOf(stream(dom), 'c2-msg');
  assert.deepEqual(msgs.map((m) => m.className.split(' ')[1]), ['is-you', 'is-lead']);
  assert.deepEqual(msgs.map((m) => classOf(m, 'c2-bubble')[0].textContent), ['Any news?', 'Three things need you.']);
  assert.deepEqual(msgs.map((m) => classOf(m, 'c2-meta')[0].textContent), ['you \u00b7 15m ago', 'lead \u00b7 10m ago']);
  assert.equal(classOf(stream(dom), 'c2-thread')[0].getAttribute('role'), 'log');
});

test('the thread scrolls to the end on the first draw and on a NEW message only', async () => {
  const msgs = THREAD.slice();
  const srv = server({ roots: calm, chat: () => ({ target_root_project_id: 'proj-a', available: true, operator: 'operator', lead: LEAD, messages: msgs.slice() }) });
  const { dom, clock, fire } = await boot(srv);
  const log = () => classOf(stream(dom), 'c2-thread')[0];
  assert.equal(log().scrollTop, log().scrollHeight, 'first draw: at the end');
  log().scrollTop = 40;                              // the operator scrolled up to read
  clock.perf += 120e3;                               // a redraw for an unrelated reason (ages moved)
  await fire((ms) => ms < 5000);
  assert.equal(log().scrollTop, 40, 'kept where it was');
  msgs.push({ id: 'c3', from: LEAD, to: 'operator', body: 'A new one', ts: iso(-100) });
  await fire((ms) => ms < 5000);
  assert.equal(log().scrollTop, log().scrollHeight, 'a new message scrolls to the end');
});

test('chat text is drawn as text: hostile bodies, and no chat at all draws no thread', async () => {
  const { dom } = await boot(server({ roots: calm, ...chatOf([{ id: 'h', from: LEAD, to: 'operator', body: HOSTILE, ts: iso(3) }]) }));
  assert.ok(all(stream(dom)).includes(HOSTILE));
  assert.deepEqual(dom.violations, []);
  const empty = await boot(server({ roots: calm, ...chatOf([]) }));
  assert.equal(classOf(stream(empty.dom), 'c2-chat').length, 0);
});

// -------------------------------------------------------------------- composer

test('the composer: pinned last, disabled, with the reason; nothing can be sent', async () => {
  const { dom } = await boot(server({ roots: calm, ...chatOf(THREAD) }));
  const composer = classOf(stream(dom), 'c2-composer')[0];
  assert.strictEqual(stream(dom).children[stream(dom).children.length - 1], composer, 'last child: it sticks to the bottom');
  const input = classOf(composer, 'c2-composer-input')[0];
  assert.deepEqual([input.tagName, input.disabled, input.getAttribute('placeholder'), input.getAttribute('aria-label')],
    ['INPUT', true, 'Message the lead   ( / )', 'Message the lead']);
  const send = classOf(composer, 'c2-send')[0];
  assert.deepEqual([send.textContent, send.disabled], ['Send', true]);
  send.click();
  assert.equal(classOf(composer, 'c2-composer-reason')[0].textContent, 'Read-only: start the console with --enable-actions to message the lead.');
});

test('the composer says the team is offline when it is', async () => {
  const srv = server({ roots: calm, ...chatOf(THREAD) });
  const { dom, fire } = await boot(srv);
  srv.down = true;
  await fire((ms) => ms < 5000);
  assert.equal(classOf(stream(dom), 'c2-composer-reason')[0].textContent, 'Paused \u2014 the lead can\u2019t receive while the team is offline.');
});

// ------------------------------------------------------- what can act, and what cannot

test('the only enabled controls in the stream are Later, Wait 10 min and the deferred line; no GO anywhere', async () => {
  const { dom } = await boot(server({ ...att([ATT_ITEM({ id: 'a', answerable: true, options: ['Keep it', 'Remove it'] }), ATT_ITEM({ id: 'b', detail: '' })]),
    ...{ chat: chatOf(THREAD).chat } }));
  const enabled = btns(stream(dom)).filter((b) => !b.disabled).map((b) => b.textContent);
  assert.ok(enabled.length > 0);
  assert.ok(enabled.every((t) => t === 'Later' || t === 'Wait 10 min' || /deferred/.test(t)), enabled.join(' / '));
  const page = [stream(dom), rail(dom)].flatMap((n) => texts(n));
  assert.ok(!page.some((t) => /\bGO\b/.test(t)));
  const inputs = walk(stream(dom)).filter((n) => ['INPUT', 'TEXTAREA', 'FORM', 'A'].includes(n.tagName));
  assert.ok(inputs.every((n) => n.tagName === 'INPUT' && n.disabled), 'the composer input is the only field, and it is disabled');
});

test('every option and control label from a feed is text, and no element gets a forbidden attribute', async () => {
  const { dom } = await boot(server({ roots: calm, ...att([ATT_ITEM({ id: HOSTILE, title: HOSTILE, detail: HOSTILE, agent: HOSTILE,
    source_label: HOSTILE, source: 'brand_new_source', answerable: true, options: [HOSTILE, HOSTILE + '2'] })]), ...chatOf([{ id: 'h', from: LEAD, to: 'operator', body: HOSTILE, ts: iso(3) }]) }));
  assert.deepEqual(dom.violations, []);
  const html = walk(stream(dom));
  assert.ok(html.some((n) => n.textContent.includes(HOSTILE)));
  for (const n of html) {
    assert.ok(!Object.keys(n.attributes).some((k) => k.startsWith('on') || k === 'style' || k === 'href' || k === 'src'));
    assert.ok(/^[a-z0-9 _:-]*$/i.test(n.className), 'fixed class vocabulary: ' + n.className);
  }
});

test('the stream keeps its scroll position across a redraw', async () => {
  const { dom, clock, fire } = await boot(server({ ...att([ATT_ITEM({ id: 'a' })]) }));
  stream(dom).scrollTop = 123;
  clock.perf += 120e3;
  await fire((ms) => ms < 5000);
  assert.equal(stream(dom).scrollTop, 123);
});

test('the busy scenario still reads end to end with the M3 controls', async () => {
  const { dom } = await boot(server({ ...att([ATT_ITEM({ id: 'e1', age: 5 * 3600 })]), ...chatOf(THREAD) }));
  const s = all(stream(dom));
  for (const want of ['Two things need you.', 'LOOKS STUCK', 'DECISION', 'ALSO HAPPENING', 'LEAD CHAT', 'Later']) assert.ok(s.includes(want), want);
  assert.ok(all(rail(dom)).includes('USAGE WINDOWS'));
  assert.equal(app(dom).className, '');
});

run();
