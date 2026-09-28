// Console v2 stream render tests (M3): every kind of needs-you card and every state of it
// (open, deferred, snoozed, locked option, empty evidence), the lead's message, the chat thread
// and the composer, all against the recording DOM. Run: node tests/console2_stream.test.mjs
import assert from 'node:assert/strict';
import { createRunner, texts, walk } from './console2_harness.mjs';
import {
  ATT_ITEM, NOW, agent, busyAgents, busyRecent, env, iso, root,
} from './console2_fixtures.mjs';
import {
  HOSTILE, LEAD, all, app, boot, chips, classOf, header, rail, server, stream,
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

test('the lead\u2019s latest message: a hexagon avatar with the runtime badge, body, who and when', async () => {
  const { dom } = await boot(server({ roots: calm, ...chatOf(THREAD) }));
  const lead = classOf(stream(dom), 'c2-lead')[0];
  const avatar = classOf(lead, 'c2-lead-avatar')[0];
  assert.ok(avatar.className.split(' ').includes('c2-avatar'));
  assert.equal(classOf(avatar, 'c2-avatar-img')[0].getAttribute('src'), '/static/avatars/hexagon-architect.png');
  assert.equal(classOf(avatar, 'c2-avatar-badge')[0].textContent, 'C');
  assert.equal(avatar.getAttribute('title'), LEAD, '06-RULES: the full name is always in the tooltip');
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
  assert.equal(classOf(composer, 'c2-composer-reason')[0].textContent, 'Messaging the lead is not available in this read-only view. Use the classic console or the CLI.');
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
    if (n.tagName === 'IMG' && n.getAttribute('src')) {
      assert.match(n.getAttribute('src'), /^\/static\/avatars\/[a-z0-9-]+\.png$/, 'the only allowed src shape');
      assert.equal(n.getAttribute('alt'), '');
    }
    assert.ok(!Object.keys(n.attributes).some((k) => k.startsWith('on') || k === 'style' || k === 'href'
      || (k === 'src' && n.tagName !== 'IMG')));
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

// ============================================================ M3 fix round

// ---- F1: the thread is scrolled AFTER it is in the document (a detached element has no scroll layout)

const manyMsgs = (n) => Array.from({ length: n }, (_, i) => ({ id: 'm' + String(i).padStart(3, '0'), from: i % 3 === 0 ? 'operator' : LEAD,
  to: LEAD, body: 'Message number ' + i, ts: iso(3600 - i * 60) }));

test('F1: with 30 messages the first draw is scrolled to the end (the element is attached when it scrolls)', async () => {
  const { dom } = await boot(server({ roots: calm, ...chatOf(manyMsgs(30)) }));
  const log = classOf(stream(dom), 'c2-thread')[0];
  assert.equal(log.isConnected, true);
  assert.equal(log.scrollTop, log.scrollHeight);
  assert.ok(log.scrollTop > 0);
});

test('F1: a scrolled-up thread keeps its position across a normal age redraw, and the node is kept', async () => {
  const { dom, clock, fire } = await boot(server({ roots: calm, ...chatOf(manyMsgs(30)) }));
  const log = () => classOf(stream(dom), 'c2-thread')[0];
  const before = log();
  before.scrollTop = 250;
  clock.perf += 120e3;                       // ages move; no new message
  await fire((ms) => ms < 5000);
  assert.strictEqual(log(), before, 'the thread element itself is kept, not rebuilt');
  assert.equal(log().scrollTop, 250);
});

test('F1: a new message scrolls to the end, an unchanged thread does not', async () => {
  const msgs = manyMsgs(30);
  const srv = server({ roots: calm, chat: () => ({ target_root_project_id: 'proj-a', available: true, operator: 'operator', lead: LEAD, messages: msgs.slice() }) });
  const { dom, clock, fire } = await boot(srv);
  const log = () => classOf(stream(dom), 'c2-thread')[0];
  log().scrollTop = 250;
  await fire((ms) => ms < 5000);
  assert.equal(log().scrollTop, 250);
  msgs.push({ id: 'm999', from: LEAD, to: 'operator', body: 'brand new', ts: iso(-clock.perf / 1000) });
  await fire((ms) => ms < 5000);
  assert.equal(log().children.length, 31);
  assert.equal(log().scrollTop, log().scrollHeight);
});

// ---- F2: a routine redraw must not take keyboard focus away from a control in the stream

const focusKey = (dom) => dom.document.activeElement.getAttribute('data-c2-focus');
// items whose age grows with the server's clock, like a real queue
const twoCardsFor = () => {
  const srv = server({ roots: calm, attention: () => ({ target_root_project_id: 'proj-a', items: [
    ATT_ITEM({ id: 'a', title: 'A', age: 900 + srv.clock.perf / 1000 }), ATT_ITEM({ id: 'b', title: 'B', age: 800 + srv.clock.perf / 1000 })] }) });
  return srv;
};

test('F2: focus on a Later button survives an age redraw (same card, same action)', async () => {
  const { dom, clock, fire } = await boot(twoCardsFor());
  const laterA = classOf(cards(dom)[0], 'c2-later')[0];
  laterA.focus();
  assert.strictEqual(dom.document.activeElement, laterA);
  clock.perf += 120e3;                                   // "waiting 15m" -> "waiting 17m": the stream redraws
  await fire((ms) => ms < 5000);
  assert.ok(all(stream(dom)).includes('waiting 17m'), 'the redraw really happened');
  const now = dom.document.activeElement;
  assert.equal(now.tagName, 'BUTTON');
  assert.equal(now.textContent, 'Later');
  assert.equal(now.isConnected, true);
  assert.equal(focusKey(dom), 'proj-a|a|later');
});

test('F2: focus on Wait 10 min survives a feed redraw (a new chat message arrives)', async () => {
  const msgs = THREAD.slice();
  const srv = server({ chat: () => ({ target_root_project_id: 'proj-a', available: true, operator: 'operator', lead: LEAD, messages: msgs.slice() }) });
  const { dom, clock, fire } = await boot(srv);
  const wait = byText(stream(dom), /^Wait 10 min$/)[0];
  wait.focus();
  msgs.push({ id: 'c9', from: LEAD, to: 'operator', body: 'fresh word', ts: iso(-clock.perf / 1000) });
  await fire((ms) => ms < 5000);
  assert.ok(all(stream(dom)).includes('fresh word'));
  assert.equal(focusKey(dom), 'proj-a|stuck:codex-agenttalk-developer-4|wait');
});

test('F2: focus on the deferred "show" button survives an age redraw', async () => {
  const { dom, clock, fire } = await boot(server(one));
  classOf(cards(dom)[0], 'c2-later')[0].click();
  const show = deferredLine(dom);
  show.focus();
  clock.perf += 120e3;
  await fire((ms) => ms < 5000);
  assert.equal(focusKey(dom), 'proj-a|deferred|show');
});

test('F2: the focused button is not replaced at all when only text changed', async () => {
  const { dom, clock, fire } = await boot(twoCardsFor());
  const laterA = classOf(cards(dom)[0], 'c2-later')[0];
  laterA.focus();
  clock.perf += 120e3;
  await fire((ms) => ms < 5000);
  assert.strictEqual(dom.document.activeElement, laterA, 'same element: no flicker, no re-announcement');
});

test('F2: deliberate fallback when the focused card goes away: the next card, then the previous, then the deferred line', async () => {
  const { dom } = await boot(twoCardsFor());
  classOf(cards(dom)[0], 'c2-later')[0].focus();
  classOf(cards(dom)[0], 'c2-later')[0].click();         // Later on card A: it leaves, B slides up
  assert.equal(focusKey(dom), 'proj-a|b|later', 'the next card’s Later');
  classOf(cards(dom)[0], 'c2-later')[0].click();         // Later on the last card
  assert.equal(focusKey(dom), 'proj-a|deferred|show', 'the deferred line, which is where the card went');
  deferredLine(dom).click();                             // show: both come back, the button is gone
  assert.equal(dom.document.activeElement.tagName, 'BUTTON');
  assert.equal(dom.document.activeElement.textContent, 'Later', 'a Later button again, never the page');
});

test('F2: focus outside the stream is never taken by a redraw', async () => {
  const { dom, clock, fire } = await boot(twoCardsFor());
  const paper = walk(dom.document.getElementById('c2-header')).find((n) => n.textContent === 'Paper');
  paper.focus();
  clock.perf += 120e3;
  await fire((ms) => ms < 5000);
  assert.strictEqual(dom.document.activeElement, paper);
});

test('F2: with nothing left to focus the stream itself takes it, not the page', async () => {
  const srv = twoCardsFor();
  const { dom, fire } = await boot(srv);
  classOf(cards(dom)[0], 'c2-later')[0].focus();
  srv.attention = () => ({ target_root_project_id: 'proj-a', items: [] });          // both cards resolved elsewhere
  await fire((ms) => ms < 5000);
  assert.equal(cards(dom).length, 0);
  assert.strictEqual(dom.document.activeElement, stream(dom));
  assert.equal(stream(dom).getAttribute('tabindex'), '-1');
});

// ---- F3: a deferral belongs to the incident it was made for

const HB = (srv) => new Date(NOW + srv.clock.perf - 20e3).toISOString();
const stall = (srv, o) => root({ project_id: 'proj-a', operator_facing: LEAD, recent: [env(LEAD, 'x', 'message', 5)], agents: [
  agent(LEAD, { since: 3000 }),
  { ...agent('codex-agenttalk-developer-4', o), last_seen: HB(srv) }] });
const at = (srv, ageSeconds) => Math.round(ageSeconds - srv.clock.perf / 1000);     // health ages are relative to NOW
const stuckCard = (dom) => cards(dom).find((c) => classOf(c, 'c2-kind')[0].textContent === 'LOOKS STUCK');

test('F3: Later on one stalled turn does not hide a different stalled turn 15 minutes after recovery', async () => {
  let mode = 'stalled';
  const srv = server({ roots: () => [mode === 'stalled' ? stall(srv, { state: 'working_silent', since: at(srv, 1800), progress: at(srv, 840) })
    : (mode === 'idle' ? stall(srv, { state: 'idle_waiting', since: at(srv, 30) })
      : stall(srv, { state: 'working_silent', since: at(srv, 660) }))] });
  const { dom, clock, fire, store } = await boot(srv);
  assert.ok(stuckCard(dom), 'the first stall raises a card');
  classOf(stuckCard(dom), 'c2-later')[0].click();
  assert.equal(stuckCard(dom), undefined);
  assert.equal(deferredLine(dom).textContent, '1 deferred · still open, not dismissed · show');
  assert.deepEqual(Object.keys(JSON.parse(store.get(LATER)).deferred['proj-a']), ['stuck:codex-agenttalk-developer-4']);

  mode = 'idle';                                        // the agent recovers
  clock.perf += 5000;
  await fire((ms) => ms < 5000);
  assert.equal(deferredLine(dom), undefined, 'the deferred line goes with the recovery');
  assert.deepEqual(Object.keys(JSON.parse(store.get(LATER)).deferred['proj-a'] || {}), [], 'and so does the stored deferral');

  clock.perf += 15 * 60e3;                              // 15 minutes later, a different turn goes silent
  mode = 'silent-again';
  await fire((ms) => ms < 5000);
  clock.perf += 2000;
  await fire((ms) => ms < 5000);
  const card = stuckCard(dom);
  assert.ok(card, 'the new stall raises its own card');
  assert.equal(deferredLine(dom), undefined);
  assert.equal(chips(dom)[0].children[2].textContent, '1', 'and it counts in the needs badge');
});

test('F3: a deferral made in an earlier session cannot hide a turn that began after it', async () => {
  const srv = server({ roots: () => [stall(srv, { state: 'working_silent', since: at(srv, 660) })] });
  const oldDeferral = { deferred: { 'proj-a': { 'stuck:codex-agenttalk-developer-4': NOW - 3600e3 } }, snoozed: {} };
  const { dom } = await boot(srv, { storage: { [LATER]: JSON.stringify(oldDeferral) } });
  assert.ok(stuckCard(dom), 'the new turn started after that deferral: it is a new incident');
  assert.equal(deferredLine(dom), undefined);
});

test('F3: the same incident stays deferred, also when the turn is still running past the deferral', async () => {
  const srv = server({ roots: () => [stall(srv, { state: 'working_silent', since: at(srv, 1800), progress: at(srv, 840) })] });
  const fresh = { deferred: { 'proj-a': { 'stuck:codex-agenttalk-developer-4': NOW - 60e3 } }, snoozed: {} };
  const { dom } = await boot(srv, { storage: { [LATER]: JSON.stringify(fresh) } });
  assert.equal(stuckCard(dom), undefined);
  assert.equal(deferredLine(dom).textContent, '1 deferred · still open, not dismissed · show');
});

test('F3: Wait 10 min is per incident too, and recovery clears it', async () => {
  let mode = 'stalled';
  const srv = server({ roots: () => [mode === 'stalled' ? stall(srv, { state: 'working_silent', since: at(srv, 1800), progress: at(srv, 840) })
    : stall(srv, { state: 'idle_waiting', since: at(srv, 30) })] });
  const { dom, clock, fire, store } = await boot(srv);
  byText(stuckCard(dom), /^Wait 10 min$/)[0].click();
  assert.equal(stuckCard(dom), undefined);
  mode = 'idle';
  clock.perf += 5000;
  await fire((ms) => ms < 5000);
  assert.deepEqual(Object.keys(JSON.parse(store.get(LATER)).snoozed['proj-a'] || {}), []);
});

test('F3: an agent that turns "unknown" (a blip in its heartbeat) keeps the deferral: only a verified recovery ends it', async () => {
  let mode = 'stalled';
  const srv = server({ roots: () => [mode === 'blip'
    ? { ...stall(srv, { state: 'working_silent', since: at(srv, 1800), progress: at(srv, 840) }),
        agents: [agent(LEAD, { since: 3000 }), { ...agent('codex-agenttalk-developer-4', { state: 'working_silent', since: at(srv, 1800), progress: at(srv, 840) }), last_seen: new Date(NOW + srv.clock.perf - 900e3).toISOString() }] }
    : stall(srv, { state: 'working_silent', since: at(srv, 1800), progress: at(srv, 840) })] });
  const { dom, clock, fire, store } = await boot(srv);
  classOf(stuckCard(dom), 'c2-later')[0].click();
  mode = 'blip';                                             // heartbeat 15 min old: judged "unknown", not recovered
  clock.perf += 5000;
  await fire((ms) => ms < 5000);
  assert.equal(stuckCard(dom), undefined);
  assert.deepEqual(Object.keys(JSON.parse(store.get(LATER)).deferred['proj-a']), ['stuck:codex-agenttalk-developer-4']);
  mode = 'stalled';                                          // and it is the same incident when the blip ends
  clock.perf += 5000;
  await fire((ms) => ms < 5000);
  assert.equal(stuckCard(dom), undefined);
  assert.equal(deferredLine(dom).textContent, '1 deferred · still open, not dismissed · show');
});

test('F3: a deferral of an ordinary (feed) card is untouched by recovery of an agent', async () => {
  const srv = server({ roots: () => [stall(srv, { state: 'idle_waiting', since: at(srv, 30) })], ...att([ATT_ITEM({ id: 'e1', title: 'Only question', age: 3600 })]) });
  const { dom, fire, clock } = await boot(srv);
  classOf(cards(dom)[0], 'c2-later')[0].click();
  clock.perf += 5000;
  await fire((ms) => ms < 5000);
  assert.equal(deferredLine(dom).textContent, '1 deferred · still open, not dismissed · show');
});

// ---- F4: the composer never points at something this page cannot do

test('F4: the composer says messaging is not available in this read-only view, and points at the working interface', async () => {
  const { dom } = await boot(server({ roots: calm, ...chatOf(THREAD) }));
  const reason = classOf(stream(dom), 'c2-composer-reason')[0].textContent;
  assert.equal(reason, 'Messaging the lead is not available in this read-only view. Use the classic console or the CLI.');
  assert.ok(!/enable-actions/.test(all(stream(dom))), 'no instruction that this page cannot honour');
  const input = classOf(stream(dom), 'c2-composer-input')[0];
  assert.equal(input.disabled, true);
  assert.equal(classOf(stream(dom), 'c2-send')[0].disabled, true);
});

// =============================================================================== M4b: keyboard

const isSelected = (c) => (' ' + c.className + ' ').includes(' is-selected ');
const key = (dom, k, extra) => dom.document.dispatch('keydown', { key: k, target: { tagName: 'BODY' }, ...extra });

test('M4b: j/k move the selection among the open cards, clamped at either end; exactly one is-selected', async () => {
  const { dom } = await boot(twoCardsFor());
  assert.equal(cards(dom).some(isSelected), false, 'nothing selected at first');
  key(dom, 'j');
  assert.deepEqual(cards(dom).map(isSelected), [true, false]);
  key(dom, 'j');
  assert.deepEqual(cards(dom).map(isSelected), [false, true]);
  key(dom, 'j');                                          // clamps: no wraparound
  assert.deepEqual(cards(dom).map(isSelected), [false, true]);
  key(dom, 'k');
  assert.deepEqual(cards(dom).map(isSelected), [true, false]);
  key(dom, 'k');                                          // clamps at the start too
  assert.deepEqual(cards(dom).map(isSelected), [true, false]);
});

test('M4b: k with nothing selected starts from the last card', async () => {
  const { dom } = await boot(twoCardsFor());
  key(dom, 'k');
  assert.deepEqual(cards(dom).map(isSelected), [false, true]);
});

test('M4b: j/k/l/Enter/? do nothing while typing in a field, exactly like every other key', async () => {
  const { dom } = await boot(twoCardsFor());
  const before = all(stream(dom));
  dom.document.dispatch('keydown', { key: 'j', target: { tagName: 'INPUT' } });
  dom.document.dispatch('keydown', { key: 'l', target: { tagName: 'TEXTAREA' } });
  dom.document.dispatch('keydown', { key: '?', target: { tagName: 'INPUT' } });
  assert.equal(cards(dom).some(isSelected), false);
  assert.equal(all(stream(dom)), before);
});

test('M4b: the selected card keeps its node identity and its highlight across an ordinary age redraw', async () => {
  const { dom, clock, fire } = await boot(twoCardsFor());
  key(dom, 'j');
  const selected = cards(dom)[0];
  assert.ok(isSelected(selected));
  clock.perf += 120e3;
  await fire((ms) => ms < 5000);
  assert.ok(all(stream(dom)).includes('waiting 17m'), 'the redraw really happened');
  assert.strictEqual(cards(dom)[0], selected, 'same node kept');
  assert.ok(isSelected(cards(dom)[0]), 'still highlighted');
});

test('M4b: losing the selected card (by a click, not "l") drops the stale selection cleanly', async () => {
  const { dom } = await boot(twoCardsFor());
  key(dom, 'j');
  assert.deepEqual(cards(dom).map(isSelected), [true, false]);
  classOf(cards(dom)[0], 'c2-later')[0].click();          // card A leaves by a click, not by "l"
  assert.equal(cards(dom).length, 1);
  assert.equal(cards(dom).some(isSelected), false, 'the selection did not silently jump to card B');
  key(dom, 'j');                                          // must start fresh, not from a stale index
  assert.equal(isSelected(cards(dom)[0]), true, 'j after the drop selects the one remaining card');
});

test('M4b: l defers the selected card, like clicking Later, and selection moves to what is now there', async () => {
  const { dom } = await boot(twoCardsFor());
  key(dom, 'j');                                          // select card A
  key(dom, 'l');
  assert.equal(cards(dom).length, 1);
  assert.equal(cards(dom)[0].getAttribute('data-c2-card'), 'proj-a|b');
  assert.ok(isSelected(cards(dom)[0]), 'selection followed to the card that is now in its place');
  assert.equal(deferredLine(dom).textContent, '1 deferred · still open, not dismissed · show');
});

test('M4b: l does nothing when no card is selected', async () => {
  const { dom } = await boot(twoCardsFor());
  key(dom, 'l');
  assert.equal(cards(dom).length, 2);
});

test('M4b: Enter opens the selected card - focus goes to Later when it has no options at all', async () => {
  const { dom } = await boot(twoCardsFor());
  key(dom, 'j');
  key(dom, 'Enter');
  const focused = dom.document.activeElement;
  assert.equal(focused.tagName, 'BUTTON');
  assert.equal(focused.textContent, 'Later');
  assert.equal(focused.getAttribute('data-c2-focus'), 'proj-a|a|later');
});

test('R2: a native Enter on a focused interactive control is never diverted to "open the selection"', async () => {
  const { dom } = await boot(twoCardsFor());
  key(dom, 'j');
  key(dom, 'Enter');
  const laterA = dom.document.activeElement;
  assert.equal(laterA.textContent, 'Later', 'focus is now on the card’s own Later button');
  let prevented = false;
  dom.document.dispatch('keydown', { key: 'Enter', target: laterA, preventDefault: () => { prevented = true; } });
  assert.equal(prevented, false, 'the handler must get out of the way: this Enter belongs to the focused button');
  assert.strictEqual(dom.document.activeElement, laterA, 'the handler touches neither the DOM nor focus here - activation is the browser’s job');
});

test('M4b: Enter skips a card’s locked options - there is no key that can reach a locked action', async () => {
  const { dom } = await boot(server({ roots: calm, ...att([
    ATT_ITEM({ id: 'a', answerable: true, options: ['Raise to 54 €', 'Keep 44 €'] }),
  ]) }));
  key(dom, 'j');
  key(dom, 'Enter');
  const focused = dom.document.activeElement;
  assert.equal(focused.textContent, 'Later', 'both options are locked (disabled); focus lands on the one live control');
});

test('M4b: Enter does nothing when no card is selected', async () => {
  const { dom } = await boot(twoCardsFor());
  const before = dom.document.activeElement;
  key(dom, 'Enter');
  assert.equal(dom.document.activeElement, before);
});

test('M4b: Escape clears the selection (and does not touch anything else)', async () => {
  const { dom } = await boot(twoCardsFor());
  key(dom, 'j');
  assert.equal(cards(dom).some(isSelected), true);
  key(dom, 'Escape');
  assert.equal(cards(dom).some(isSelected), false);
});

test('M4b: "/" tries to focus the message box, which stays disabled - a browser refuses focus on it', async () => {
  const { dom } = await boot(server({ roots: calm, ...chatOf(THREAD) }));
  const before = dom.document.activeElement;
  key(dom, '/');
  const input = classOf(stream(dom), 'c2-composer-input')[0];
  assert.equal(input.disabled, true, 'never enabled by the keypress');
  assert.equal(dom.document.activeElement, before, 'a disabled control cannot take focus');
});

// =============================================================================== fix round 4

test('R3: j/k move real focus with the selection, onto the card itself - never left on a stale control', async () => {
  const { dom } = await boot(twoCardsFor());
  key(dom, 'j');
  const cardA = cards(dom)[0];
  assert.strictEqual(dom.document.activeElement, cardA, 'focus moved onto the newly selected card A');
  key(dom, 'Enter');
  const laterA = dom.document.activeElement;
  assert.equal(laterA.textContent, 'Later');

  key(dom, 'j');   // select card B
  const cardB = cards(dom)[1];
  assert.strictEqual(dom.document.activeElement, cardB, 'focus followed the selection to B, not left on A’s Later');

  key(dom, 'Enter');
  const laterB = dom.document.activeElement;
  assert.equal(laterB.getAttribute('data-c2-focus'), 'proj-a|b|later', 'Enter opened B - the card that is actually highlighted');

  key(dom, 'l');
  assert.equal(cards(dom).length, 1, 'B - the highlighted, focused card - was deferred');
  assert.equal(cards(dom)[0].getAttribute('data-c2-card'), 'proj-a|a', 'A is still on screen, untouched');
});

test('R3: k also moves focus with the selection', async () => {
  const { dom } = await boot(twoCardsFor());
  key(dom, 'k');   // starts from the last card with nothing selected
  const cardB = cards(dom)[1];
  assert.strictEqual(dom.document.activeElement, cardB);
  key(dom, 'k');
  const cardA = cards(dom)[0];
  assert.strictEqual(dom.document.activeElement, cardA);
});

test('R4: closing help restores focus to wherever it was invoked from - here, a focused Later button', async () => {
  const { dom } = await boot(twoCardsFor());
  const laterA = classOf(cards(dom)[0], 'c2-later')[0];
  laterA.focus();
  assert.equal(dom.document.activeElement, laterA);
  key(dom, '?');
  const closeBtn = classOf(dom.document.getElementById('c2-keymap'), 'c2-overlay-close')[0];
  assert.equal(dom.document.activeElement, closeBtn, 'focus moved into the dialog');
  key(dom, 'Escape');
  assert.equal(dom.document.activeElement, laterA, 'back on the Later button, not a blanket default to the ? button');
  assert.equal(laterA.isConnected, true);
});

test('M4b: "1" and "2" switch team by key, exactly like clicking the chip', async () => {
  const twoTeams = () => [
    root({ project_id: 'proj-a', label: 'Alpha', agents: [agent(LEAD, { since: 3000 })], recent: [env(LEAD, 'x', 'message', 5)] }),
    root({ project_id: 'proj-b', label: 'Beta', agents: [agent(LEAD, { since: 3000 })], recent: [env(LEAD, 'x', 'message', 5)] }),
  ];
  const { dom } = await boot(server({ roots: twoTeams }));
  const pressed = () => chips(dom).filter((c) => c.getAttribute('aria-pressed') === 'true').map((c) => c.textContent);
  assert.deepEqual(pressed(), ['Alpha']);
  key(dom, '2');
  assert.deepEqual(pressed(), ['Beta']);
  key(dom, '1');
  assert.deepEqual(pressed(), ['Alpha']);
});

run();
