// Console v2 data-layer and render tests (M2): polling, time anchoring, offline truths,
// stale feeds, redraw discipline, and textContent-only drawing of every data-bearing field.
// Run: node tests/console2_data.test.mjs   (also run by tests/test_console2_web.py)
// Clock times are drawn in the browser's local zone; pin it so the expected strings hold everywhere.
process.env.TZ = 'UTC';

import assert from 'node:assert/strict';
import {
  createRunner, jsonResponse, loadConsole, makeDom, texts, walk,
} from './console2_harness.mjs';
import {
  ATT_ITEM, NOW, agent, busyAgents, busyRecent, env, iso, root,
} from './console2_fixtures.mjs';

const { test, run } = createRunner('console2 data layer');
const HOSTILE = '<img src=x onerror=alert(1)>';
const LEAD = 'claude-agenttalk-lead';

// A programmable server: state/attention/chat handlers may return a payload, a
// {status} problem, or throw; every request is recorded.
function server(o = {}) {
  const calls = [];
  const clock = { perf: 0 };   // shared with the page: the server's clock moves with it
  const s = {
    calls,
    clock,
    generated: o.generated || (() => new Date(NOW + clock.perf).toISOString()),
    roots: o.roots || (() => [root({ project_id: 'proj-a', agents: busyAgents(), recent: busyRecent(), operator_facing: LEAD })]),
    attention: o.attention || (() => ({ target_root_project_id: 'proj-a', items: [] })),
    chat: o.chat || (() => ({ target_root_project_id: 'proj-a', available: true, lead: LEAD, messages: [] })),
    down: false,
    fetch(url, init) {
      calls.push(url);
      if (s.down) return Promise.reject(new Error('down'));
      const u = new URL(url, 'http://x');
      const id = u.searchParams.get('root') || '';
      let payload;
      if (u.pathname === '/api/state') payload = { schema_version: 1, generated_at: s.generated(), roots: s.roots() };
      else if (u.pathname === '/api/attention') payload = s.attention(id);
      else if (u.pathname === '/api/lead-chat') payload = s.chat(id);
      else return jsonResponse({}, 404);
      if (payload && payload.__status) return jsonResponse({}, payload.__status);
      return jsonResponse(payload);
    },
  };
  return s;
}

async function boot(srv, opts = {}) {
  const dom = makeDom();
  const loaded = loadConsole({ dom, fetch: (u, i) => srv.fetch(u, i), clock: srv.clock, ...opts });
  for (let i = 0; i < 12; i++) await new Promise((r) => setTimeout(r, 0));
  return { dom, ...loaded };
}

const stream = (dom) => dom.document.getElementById('c2-stream');
const rail = (dom) => dom.document.getElementById('c2-rail');
const all = (node) => texts(node).join(' | ');
const app = (dom) => dom.document.getElementById('app');
const header = (dom) => dom.document.getElementById('c2-header');
const classOf = (node, cls) => walk(node).filter((n) => (n.className || '').split(' ').includes(cls));
const chips = (dom) => walk(header(dom)).filter((n) => n.getAttribute && n.getAttribute('data-c2-key') !== null);

test('busy day end to end: greeting, cards with evidence, aside, usage windows, roster', async () => {
  const srv = server({
    attention: () => ({ target_root_project_id: 'proj-a', items: [
      ATT_ITEM({ id: 'e1', title: 'Keep the old CSV export for one more release?', detail: 'rev-3 cold review: FIX · two callers still use it', age: 5 * 3600 }),
      ATT_ITEM({ id: 'e2', title: 'Second question', detail: '', age: 2 * 86400 }),
    ] }),
    chat: () => ({ target_root_project_id: 'proj-a', available: true, lead: LEAD, messages: [
      { from: LEAD, to: 'operator', body: 'Three things need you. dev-4 first.', ts: iso(240) } ] }),
  });
  const { dom } = await boot(srv);
  const s = all(stream(dom));
  assert.ok(s.includes('Three things need you.'));
  assert.ok(s.includes('Stuck agents first, then oldest. A deadline only shows if someone set one.'));
  assert.ok(s.includes('Three things need you. dev-4 first.'), 'the lead’s latest message');
  assert.ok(s.includes('lead · 4m ago'));
  // order: the stuck card, then the oldest, then the newer one
  const kinds = classOf(stream(dom), 'c2-kind').map((n) => n.textContent);
  assert.deepEqual(kinds, ['LOOKS STUCK', 'DECISION', 'DECISION']);
  assert.ok(s.includes('dev-4 has gone quiet'));
  assert.ok(s.includes('Last progress 14m ago · no reply sent · heartbeat still fresh'));
  assert.ok(s.includes('Weaker evidence: process status isn’t visible yet, so Wait comes first.'));
  assert.ok(s.includes('Wait 10 min') && s.includes('Restart with context · CLI only'));
  assert.ok(s.includes('no deadline · waiting 2d') && s.includes('No evidence recorded'));
  assert.ok(s.includes('ALSO HAPPENING · NOT FOR YOU'));
  assert.ok(s.includes('dev-5 is quiet, not stuck') && s.includes('x.rev-1 is capped'));
  const r = all(rail(dom));
  assert.ok(r.includes('USAGE WINDOWS') && r.includes('Claude · 5-hour') && r.includes('41%') && r.includes('Codex · 5-hour') && r.includes('100%'));
  assert.ok(r.includes('TEAM · 9') && r.includes('4 idle · that’s normal'));
  assert.ok(r.includes('5-hour window full') && r.includes('resets 23:40'));
  assert.deepEqual(dom.violations, []);
});

test('meter widths come from numbers only, through the style object', async () => {
  const { dom } = await boot(server());
  const fills = classOf(rail(dom), 'c2-meter-fill');
  assert.ok(fills.length >= 3);
  fills.forEach((f) => assert.match(f.style.width, /^\d{1,3}%$/));
  assert.deepEqual(fills.map((f) => f.style.width).slice(0, 2), ['41%', '23%']);
  walk(rail(dom)).forEach((n) => assert.ok(!('style' in n.attributes), 'no style attribute'));
});

test('the header chip shows freshness and the needs count', async () => {
  const srv = server({ attention: () => ({ target_root_project_id: 'proj-a', items: [ATT_ITEM({ id: 'a' }), ATT_ITEM({ id: 'b' })] }) });
  const { dom } = await boot(srv);
  const [chip] = chips(dom);
  const parts = chip.children.map((c) => [c.className, c.textContent]);
  assert.deepEqual(parts, [['c2-dot is-live', ''], ['c2-chip-label', 'agenttalk'], ['c2-badge', '3']]);
});

test('time is anchored on generated_at: a wrong local clock changes nothing, elapsed time does', async () => {
  const srv = server();
  const { dom, clock, fire } = await boot(srv);
  assert.ok(all(rail(dom)).includes('Idle · 40m'), 'idle since 40 min at the snapshot time');
  // The state feed dies; only monotonic time passes.
  srv.down = true;
  clock.perf += 120e3;
  await fire();
  assert.ok(all(rail(dom)).includes('Idle · 42m'), 'ages advance with elapsed time');
  assert.ok(all(stream(dom)).includes('CAN’T REACH THE CONSOLE SERVER'));
});

test('polling: 2 s cadence, one state read then the selected team’s feeds', async () => {
  const srv = server();
  const { timers, fire } = await boot(srv);
  assert.deepEqual(srv.calls, ['/api/state', '/api/attention?root=proj-a', '/api/lead-chat?root=proj-a']);
  assert.deepEqual(timers.map((t) => t.ms), [2000]);
  const ms = await fire();
  assert.deepEqual(ms, [2000]);
  assert.equal(srv.calls.length, 6);
  assert.deepEqual(srv.calls.slice(3), ['/api/state', '/api/attention?root=proj-a', '/api/lead-chat?root=proj-a']);
});

test('other teams’ attention is read on the first round and every fifth after', async () => {
  const two = () => [root({ project_id: 'a', agents: busyAgents(), recent: busyRecent() }),
    root({ label: 'second', project_id: 'b', agents: [agent('claude-second-lead')] })];
  const srv = server({ roots: two, attention: (id) => ({ target_root_project_id: id, items: id === 'b' ? [ATT_ITEM({ id: 'x' })] : [] }),
    chat: (id) => ({ target_root_project_id: id, available: true, lead: LEAD, messages: [] }) });
  const { fire } = await boot(srv);
  const othersAt = () => srv.calls.filter((u) => u === '/api/attention?root=b').length;
  assert.equal(othersAt(), 1, 'round 1');
  for (let i = 0; i < 4; i++) await fire();
  assert.equal(othersAt(), 1, 'rounds 2 to 5');
  await fire();
  assert.equal(othersAt(), 2, 'round 6');
  assert.equal(srv.calls.filter((u) => u.startsWith('/api/lead-chat')).every((u) => u.endsWith('root=a')), true, 'chat only for the selected team');
});

test('a failed state read keeps the last good data, greyed, with the unreachable banner; recovery clears it', async () => {
  const srv = server();
  const { dom, fire } = await boot(srv);
  assert.equal(app(dom).className, '');
  srv.down = true;
  await fire();
  assert.equal(app(dom).className, 'is-stale');
  assert.ok(all(stream(dom)).includes('CAN’T REACH THE CONSOLE SERVER'));
  assert.ok(all(stream(dom)).includes('Can’t see the team.'));
  assert.ok(all(rail(dom)).includes('TEAM · 9'), 'the roster is still there');
  assert.ok(all(rail(dom)).includes('frozen · as of'));
  assert.equal(chips(dom)[0].children[0].className, 'c2-dot is-unreachable');
  srv.down = false;
  await fire();
  assert.equal(app(dom).className, '');
  assert.ok(!all(stream(dom)).includes('CAN’T REACH'));
  assert.equal(chips(dom)[0].children[0].className, 'c2-dot is-live');
});

test('the very first read failing says so instead of showing an empty page', async () => {
  const srv = server();
  srv.down = true;
  const { dom } = await boot(srv);
  assert.ok(all(stream(dom)).includes('CAN’T REACH THE CONSOLE SERVER'));
  assert.ok(all(stream(dom)).includes('No snapshot has arrived yet.'));
  assert.equal(chips(dom).length, 0);
  assert.equal(classOf(header(dom), 'c2-chip')[0].textContent, 'No team data');
});

test('the server answers but nobody has written for over 5 minutes: the second banner', async () => {
  const stale = () => root({ project_id: 'proj-a', agents: busyAgents().map((a) => ({ ...a, last_seen: iso(3 * 3600), health: { ...a.health, updated_at: iso(3 * 3600) },
    ...(a.capacity ? { capacity: { ...a.capacity, observed_at: iso(3 * 3600) } } : {}) })), recent: [env('x', 'y', 'message', 3 * 3600)] });
  const { dom } = await boot(server({ roots: () => [stale()] }));
  const s = all(stream(dom));
  assert.ok(s.includes('NO AGENT HAS REPORTED FOR 3H 0M'));
  assert.ok(!s.includes('CAN’T REACH'));
  assert.equal(app(dom).className, 'is-stale');
  assert.equal(chips(dom)[0].children[0].className, 'c2-dot is-silent');
});

test('generated_at that stops advancing (a stuck cache) is treated as unreachable after more than 3 polls', async () => {
  const srv = server({ generated: () => new Date(NOW).toISOString() });
  const { dom, fire } = await boot(srv);
  for (let i = 0; i < 3; i++) await fire();
  assert.ok(!all(stream(dom)).includes('CAN’T REACH'), 'three repeats are tolerated');
  await fire();
  assert.ok(all(stream(dom)).includes('CAN’T REACH THE CONSOLE SERVER'));
});

test('a stale attention read greys the page; a failed one keeps the last items', async () => {
  const srv = server({ attention: () => ({ target_root_project_id: 'proj-a', items: [ATT_ITEM({ id: 'a', title: 'Only question' })] }) });
  const { dom, clock, fire } = await boot(srv);
  assert.equal(app(dom).className, '');
  srv.attention = () => ({ __status: 500 });
  await fire();
  assert.ok(all(stream(dom)).includes('Only question'), 'last good items remain');
  assert.equal(app(dom).className, 'is-stale', 'and are marked stale once the read failed');
  clock.perf += 9000;
  await fire();
  assert.equal(app(dom).className, 'is-stale');
});

test('attention with errors-as-data says it cannot read what needs you', async () => {
  const { dom } = await boot(server({ attention: () => ({ target_root_project_id: 'proj-a', items: [], errors: ['boom'] }) }));
  assert.ok(all(stream(dom)).includes('Can’t read what needs you.'));
  assert.ok(!all(stream(dom)).includes('boom'));
});

test('an answer for a different team is discarded, not shown', async () => {
  const { dom } = await boot(server({
    attention: () => ({ target_root_project_id: 'someone-else', items: [ATT_ITEM({ id: 'x', title: 'Not ours' })] }),
    chat: () => ({ target_root_project_id: 'someone-else', available: true, lead: LEAD, messages: [{ from: LEAD, body: 'Not ours', ts: iso(1) }] }),
  }));
  const s = all(stream(dom));
  assert.ok(!s.includes('Not ours'));
  assert.ok(s.includes('Waiting for the first snapshot.'), 'no attention data yet means no claim about what needs you');
});

test('quiet day: greeting, idle sentence, since-you-last-looked from the stored visit', async () => {
  const lastVisit = NOW - 6 * 3600e3;
  const srv = server({ roots: () => [root({ project_id: 'proj-a', operator_facing: LEAD,
    agents: [agent(LEAD, { since: 3000 }), agent('claude-agenttalk-developer-2', { since: 3000 })],
    recent: [env(LEAD, 'operator', 'message', 600), env('claude-agenttalk-reviewer-3', LEAD, 'review-result', 900)] })] });
  const { dom, store, windowEvents } = await boot(srv, { storage: { 'agenttalk.console2.lastvisit': String(lastVisit) } });
  const s = all(stream(dom));
  assert.ok(s.includes('All quiet.'));
  assert.ok(s.includes('Nothing needs you. 2 of 2 agents are idle'));
  assert.ok(s.includes('SINCE YOU LAST LOOKED'));
  assert.ok(s.includes('2 messages') && s.includes('1 review result'));
  assert.ok(Number(store.get('agenttalk.console2.lastvisit')) > lastVisit, 'this visit is recorded');
  assert.equal(windowEvents.pagehide.length, 1);
});

test('unknown ?root= reads no team feeds for the selected team and says so', async () => {
  const srv = server();
  const { dom } = await boot(srv, { search: '?root=nope' });
  assert.ok(all(stream(dom)).includes('Unknown team.'));
  assert.ok(srv.calls.every((u) => !u.startsWith('/api/lead-chat')));
  assert.ok(srv.calls.every((u) => u !== '/api/attention?root=nope'));
  assert.equal(all(rail(dom)), '');
});

test('picking a team fetches its feeds at once and redraws', async () => {
  const two = () => [root({ project_id: 'a', agents: [agent(LEAD)], recent: [env(LEAD, 'x', 'message', 5)] }),
    root({ label: 'second', project_id: 'b', agents: [agent('claude-second-lead')], recent: [env('claude-second-lead', 'x', 'message', 5)] })];
  const srv = server({ roots: two,
    attention: (id) => ({ target_root_project_id: id, items: id === 'b' ? [ATT_ITEM({ id: 'b1', title: 'Second team question' })] : [] }),
    chat: (id) => ({ target_root_project_id: id, available: true, lead: id === 'b' ? 'claude-second-lead' : LEAD, messages: [] }) });
  const { dom, fire } = await boot(srv);
  assert.ok(!all(stream(dom)).includes('Second team question'));
  const before = srv.calls.length;
  chips(dom)[1].click();
  for (let i = 0; i < 12; i++) await new Promise((r) => setTimeout(r, 0));
  assert.ok(srv.calls.slice(before).includes('/api/lead-chat?root=b'));
  assert.ok(all(stream(dom)).includes('Second team question'));
  assert.equal(chips(dom)[1].getAttribute('aria-pressed'), 'true');
  await fire();
});

test('the stream is not redrawn when nothing it shows has changed', async () => {
  const srv = server();
  const { dom, clock, fire } = await boot(srv);
  const first = stream(dom).children[0];
  const railFirst = rail(dom).children[0];
  clock.perf += 2000;
  await fire();
  assert.strictEqual(stream(dom).children[0], first, 'same node: no redraw, scroll position kept');
  assert.strictEqual(rail(dom).children[0], railFirst);
  clock.perf += 120e3;
  await fire();
  assert.notStrictEqual(rail(dom).children[0], railFirst, 'a minute-level age change redraws');
});

test('chips rebuild when the set of teams changes and focus returns to the same team', async () => {
  let list = [root({ label: 'a', project_id: 'p1', agents: [agent(LEAD)] }), root({ label: 'b', project_id: 'p2', agents: [agent(LEAD)] })];
  const srv = server({ roots: () => list, attention: (id) => ({ target_root_project_id: id, items: [] }),
    chat: (id) => ({ target_root_project_id: id, available: true, lead: LEAD, messages: [] }) });
  const { dom, fire } = await boot(srv);
  chips(dom)[1].focus();
  list = [root({ label: 'z', project_id: 'p0', agents: [agent(LEAD)] }), ...list];
  await fire();
  assert.deepEqual(chips(dom).map((c) => c.getAttribute('data-c2-key')), ['p0', 'p1', 'p2']);
  assert.equal(dom.document.activeElement.getAttribute('data-c2-key'), 'p2');
});

test('every data-bearing field is drawn as text: hostile input everywhere', async () => {
  const bad = HOSTILE;
  const srv = server({
    roots: () => [root({ label: bad, project_id: 'proj-a', operator_facing: 'claude-agenttalk-lead',
      agents: [
        agent('claude-agenttalk-lead', { state: 'working_turn', since: 20, task: bad }),
        agent(bad, { state: 'rate_limited_or_outage', since: 20 }),
        agent('codex-agenttalk-developer-4', { state: 'working_silent', since: 1800, progress: 900, role: bad }),
      ], recent: [] })],
    attention: () => ({ target_root_project_id: 'proj-a', items: [ATT_ITEM({ id: bad, title: bad, detail: bad, agent: bad, source_label: bad, source: 'brand_new_source' })] }),
    chat: () => ({ target_root_project_id: 'proj-a', available: true, lead: 'claude-agenttalk-lead', messages: [{ from: 'claude-agenttalk-lead', body: bad, ts: iso(3) }] }),
  });
  const { dom } = await boot(srv);
  assert.deepEqual(dom.violations, []);
  assert.ok(all(stream(dom)).includes(bad));
  assert.ok(all(rail(dom)).includes(bad));
  for (const node of [...walk(stream(dom)), ...walk(rail(dom)), ...walk(header(dom))]) {
    assert.ok(!Object.keys(node.attributes).some((k) => k.startsWith('on') || k === 'style' || k === 'href' || k === 'src'), node.tagName);
    assert.ok(/^[a-z0-9 _:-]*$/i.test(node.className), 'class names are fixed vocabulary: ' + node.className);
  }
});

test('there is no GO button and no control that could act in the page (M2 draws text only)', async () => {
  const { dom } = await boot(server({ attention: () => ({ target_root_project_id: 'proj-a', items: [ATT_ITEM({ id: 'a', answerable: true, options: ['Keep it', 'Remove it'] })] }) }));
  const page = [stream(dom), rail(dom), header(dom)].flatMap((n) => texts(n));
  assert.ok(page.length > 10);
  assert.ok(!page.some((t) => /GO/.test(t)), 'no GO');
  const controls = [stream(dom), rail(dom)].flatMap((n) => walk(n)).filter((n) => ['BUTTON', 'INPUT', 'TEXTAREA', 'FORM', 'A'].includes(n.tagName));
  assert.deepEqual(controls, []);
});

run();
