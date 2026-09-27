// Console v2 data-layer and render tests (M2): polling, time anchoring, offline truths,
// stale feeds, redraw discipline, and textContent-only drawing of every data-bearing field.
// Run: node tests/console2_data.test.mjs   (also run by tests/test_console2_web.py)
// Clock times are drawn in the browser's local zone; pin it so the expected strings hold everywhere.
process.env.TZ = 'UTC';

import assert from 'node:assert/strict';
import { createRunner } from './console2_harness.mjs';
import {
  ATT_ITEM, NOW, agent, busyAgents, busyRecent, env, iso, root,
} from './console2_fixtures.mjs';
import {
  HOSTILE, JSON_HANG, LEAD, PENDING, all, app, boot, chips, classOf, header, rail, server, stream, timeouts, under,
} from './console2_app.mjs';
import { texts, walk } from './console2_harness.mjs';

const { test, run } = createRunner('console2 data layer');
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

test('F1 (final sweep): a repeated generated_at does not re-anchor the clock - ages and classification keep advancing on real elapsed time', async () => {
  const FIXED = new Date(NOW).toISOString();
  const srv = server({
    generated: () => FIXED,
    roots: () => [root({ project_id: 'proj-a', operator_facing: LEAD, agents: [
      agent(LEAD, { since: 600 }),
      agent('codex-agenttalk-developer-4', { state: 'working_silent', since: 900, progress: 900, hb: 20 }),
    ], recent: [] })],
  });
  const { dom, clock, fire } = await boot(srv);
  assert.ok(all(stream(dom)).includes('LOOKS STUCK'), 'connected: correctly judged stuck at the snapshot time');
  clock.perf += 360e3;   // six real minutes pass; the server keeps returning the SAME generated_at
  await fire();
  // Third time-anchoring defect, pinned: a stalled (non-advancing) feed must not freeze the clock.
  // The heartbeat (fixed at 20s old in the payload, never refreshed) is now genuinely 380s old by
  // real elapsed time - past the 300s freshness boundary - so the incident can no longer be judged
  // fresh: "unknown, not judged stuck", never a stuck card asserting a heartbeat that is not really fresh.
  assert.ok(!all(stream(dom)).includes('LOOKS STUCK'),
    'a repeated snapshot must not keep the clock (and the judgment) frozen');
  assert.ok(all(rail(dom)).includes('Heartbeat') && all(rail(dom)).includes('not judged stuck'),
    'the heartbeat is correctly judged stale by real elapsed time, not "still fresh"');
});

test('F2 (final sweep, ordinary polling): switching to a team whose attention refresh is pending past 8s must not show an unqualified all-clear', async () => {
  let pendingForB = false;
  const twoTeams = () => [
    root({ project_id: 'proj-a', label: 'Alpha', agents: [agent(LEAD, { since: 3000 })], recent: [env(LEAD, 'x', 'message', 5)] }),
    root({ project_id: 'proj-b', label: 'Beta', agents: [agent(LEAD, { since: 3000 })], recent: [env(LEAD, 'x', 'message', 5)] }),
  ];
  const srv = server({
    roots: twoTeams,
    attention: (id) => (id === 'proj-b' && pendingForB ? PENDING : { target_root_project_id: id, items: [] }),
  });
  const { dom, clock, fire } = await boot(srv);   // both teams' attention already fetched fresh, empty
  pendingForB = true;
  chips(dom)[1].click();                          // switch to Beta - its NEW attention request hangs
  clock.perf += 9000;                              // 9s later: stale (> ATTENTION_FRESH_S=8s)...
  await fire(under);                               // ...but stay under the 5s request timeout: still pending, not failed
  assert.ok(!all(stream(dom)).includes('All quiet.'),
    'a stale, still-pending attention read must not claim an all-clear');
  assert.ok(all(stream(dom)).includes('confirm') || all(stream(dom)).includes('not refresh'));
});

test('N2: an outage does not un-stick an unresolved incident - the stuck card persists, shown as last known', async () => {
  const srv = server();
  const { dom, fire } = await boot(srv);
  const kindsBefore = classOf(stream(dom), 'c2-kind').map((n) => n.textContent);
  assert.ok(kindsBefore.includes('LOOKS STUCK'), 'connected: a stuck card exists');
  srv.down = true;
  await fire();
  assert.ok(all(stream(dom)).includes('CAN’T REACH THE CONSOLE SERVER'));
  const kindsAfter = classOf(stream(dom), 'c2-kind').map((n) => n.textContent);
  assert.ok(kindsAfter.includes('LOOKS STUCK'), 'offline: the same incident is still shown as a stuck card, not silently dropped');
});

test('N1: the rail meter width is kept in sync through the reconcile, both up and down within one tone', async () => {
  const srv = server();
  let pct = 41;
  srv.roots = () => [root({ project_id: 'proj-a', agents: busyAgents().map((a, i) => (i === 0
    ? { ...a, capacity: { ...a.capacity, primary: { ...a.capacity.primary, used_pct: pct } } } : a)),
    recent: busyRecent(), operator_facing: LEAD })];
  const { dom, fire } = await boot(srv);
  const meter = classOf(rail(dom), 'c2-meter-fill')[0];
  const avatar = classOf(rail(dom), 'c2-avatar-img')[0];
  assert.equal(meter.style.width, '41%');
  assert.equal(classOf(rail(dom), 'c2-usage-pct')[0].textContent, '41%');

  pct = 42;   // a fresh reading, same tone ("ok", both under 60%): a same-node redraw
  await fire();
  assert.strictEqual(classOf(rail(dom), 'c2-meter-fill')[0], meter, 'the meter node is kept, not rebuilt');
  assert.equal(classOf(rail(dom), 'c2-usage-pct')[0].textContent, '42%', 'the label updated');
  assert.equal(meter.style.width, '42%', 'the bar width updated too - an increase');
  assert.strictEqual(classOf(rail(dom), 'c2-avatar-img')[0], avatar, 'unrelated avatar identity is untouched');

  pct = 30;   // a decrease (e.g. a quota reset), still the same tone
  await fire();
  assert.strictEqual(classOf(rail(dom), 'c2-meter-fill')[0], meter);
  assert.equal(meter.style.width, '30%', 'the bar width updated - a decrease');
});

test('time is anchored on generated_at: a wrong local clock changes nothing, and while live elapsed time does advance it', async () => {
  const srv = server();
  const { dom, clock, fire } = await boot(srv);
  assert.ok(all(rail(dom)).includes('Idle · 40m'), 'idle since 40 min at the snapshot time');
  // Still live: a minute-level redraw really does advance with elapsed monotonic time.
  clock.perf += 120e3;
  await fire();
  assert.ok(all(rail(dom)).includes('Idle · 42m'), 'ages advance with elapsed time while live');
  // The state feed dies; only monotonic time passes from here.
  srv.down = true;
  clock.perf += 120e3;
  await fire();
  // Offline: the lead's later call (M4a fix round) freezes every row's age at the last
  // known-good reading instead of letting it keep ticking against a clock the data can't back.
  const frozen = all(rail(dom));
  assert.match(frozen, /as of \d\d:\d\d/);
  assert.ok(!frozen.includes('Idle · 42m'), 'no longer the live-ticked reading either: this is a fresh, source-anchored freeze');
  clock.perf += 5e3;   // still well under the 300s heartbeat-freshness boundary (N2): classification is unaffected
  await fire();
  assert.equal(all(rail(dom)), frozen, 'a second offline redraw shows the exact same frozen text: it does not keep advancing');
  assert.ok(all(stream(dom)).includes('CAN’T REACH THE CONSOLE SERVER'));
});

test('polling: 2 s cadence, one state read then the selected team’s feeds', async () => {
  const srv = server();
  const { timers, fire } = await boot(srv);
  assert.deepEqual(srv.calls, ['/api/state', '/api/attention?root=proj-a', '/api/lead-chat?root=proj-a']);
  assert.deepEqual(timers.map((t) => t.ms).sort(), [1000, 2000], 'the poll (2 s) and the repaint (1 s); requests leave no timers behind');
  const ms = await fire();
  assert.deepEqual(ms.sort(), [1000, 2000]);
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

// ============================================================================= M4: Retry now

test('Retry now polls at once (not on the 2 s cadence), and disappears once live again', async () => {
  const srv = server();
  const { dom, fire } = await boot(srv);
  srv.down = true;
  await fire();
  const before = srv.calls.filter((u) => u === '/api/state').length;
  classOf(stream(dom), 'c2-retry')[0].click();
  for (let i = 0; i < 12; i++) await new Promise((r) => setTimeout(r, 0));
  assert.equal(srv.calls.filter((u) => u === '/api/state').length, before + 1, 'no waiting for the next tick');
  assert.equal(app(dom).className, 'is-stale', 'still down: the retry itself failed too');
  srv.down = false;
  classOf(stream(dom), 'c2-retry')[0].click();
  for (let i = 0; i < 12; i++) await new Promise((r) => setTimeout(r, 0));
  assert.equal(app(dom).className, '');
  assert.equal(classOf(stream(dom), 'c2-retry').length, 0, 'the banner, and its button, are gone');
});

test('a retry that still fails is followed by "Still unreachable"; the label only appears after a retry', async () => {
  const srv = server();
  const { dom, clock, fire } = await boot(srv);
  srv.down = true;
  await fire();
  assert.equal(all(stream(dom)).includes('Still unreachable'), false, 'no retry attempted yet');
  clock.perf += 3000;
  classOf(stream(dom), 'c2-retry')[0].click();
  for (let i = 0; i < 12; i++) await new Promise((r) => setTimeout(r, 0));
  assert.match(all(stream(dom)), /Still unreachable \u00b7 tried \d\d:\d\d/);
});

test('a retry that succeeds never leaves a stale "tried" label behind the next time it goes down', async () => {
  const srv = server();
  const { dom, fire } = await boot(srv);
  srv.down = true;
  await fire();
  classOf(stream(dom), 'c2-retry')[0].click();
  for (let i = 0; i < 12; i++) await new Promise((r) => setTimeout(r, 0));
  assert.match(all(stream(dom)), /Still unreachable/);
  srv.down = false;
  await fire();
  assert.equal(app(dom).className, '');
  srv.down = true;
  await fire();
  assert.equal(all(stream(dom)).includes('Still unreachable'), false, 'a fresh outage, not attempted yet');
});

test('Retry now also appears on the "team is silent" banner', async () => {
  const stale = () => root({ project_id: 'proj-a', agents: busyAgents().map((a) => ({ ...a, last_seen: iso(3 * 3600), health: { ...a.health, updated_at: iso(3 * 3600) },
    ...(a.capacity ? { capacity: { ...a.capacity, observed_at: iso(3 * 3600) } } : {}) })), recent: [env('x', 'y', 'message', 3 * 3600)] });
  const { dom } = await boot(server({ roots: () => [stale()] }));
  assert.equal(classOf(stream(dom), 'c2-retry').length, 1);
});

test('focus on Retry now survives an ordinary redraw (the "ago" wording keeps changing)', async () => {
  const srv = server();
  const { dom, clock, fire } = await boot(srv);
  srv.down = true;
  await fire();
  const retry = classOf(stream(dom), 'c2-retry')[0];
  retry.focus();
  const before = all(stream(dom));
  clock.perf += 120e3;
  await fire();
  assert.notEqual(all(stream(dom)), before, 'the redraw really happened (the elapsed time changed)');
  assert.strictEqual(dom.document.activeElement, retry);
});

// ============================================================================= M4a fix round: F1

test('F1: 20 Retry clicks while /api/state is hanging issue exactly one extra request, not 20', async () => {
  const srv = server();
  const { dom, fire } = await boot(srv);
  srv.down = true;
  await fire();                          // now offline, with a "Retry now" button
  assert.ok(classOf(stream(dom), 'c2-retry')[0]);
  srv.down = false;
  srv.pendingState = true;                // from here, every /api/state hangs instead of answering
  const before = srv.calls.filter((u) => u === '/api/state').length;
  for (let i = 0; i < 20; i++) {
    classOf(stream(dom), 'c2-retry')[0].click();
    await new Promise((r) => setTimeout(r, 0));
  }
  assert.equal(srv.calls.filter((u) => u === '/api/state').length, before + 1, 'coalesced: at most one is ever outstanding');
  // the hung request times out after 5 s and the loop's own cadence resumes normally afterwards
  await fire(timeouts);
  const afterTimeout = srv.calls.filter((u) => u === '/api/state').length;
  srv.pendingState = false;
  await fire(under);
  assert.equal(srv.calls.filter((u) => u === '/api/state').length, afterTimeout + 1, 'the NEXT poll is a single ordinary request');
});

test('F1: clicking Retry while the loop’s own poll is outstanding does not start a second request', async () => {
  const srv = server();
  const { dom, clock, fire } = await boot(srv);
  srv.down = true;
  await fire();
  srv.down = false;
  srv.pendingState = true;
  clock.perf += 2000;
  await fire(under);                      // the loop's own 2 s tick issues one hanging request
  const before = srv.calls.filter((u) => u === '/api/state').length;
  classOf(stream(dom), 'c2-retry')[0].click();
  classOf(stream(dom), 'c2-retry')[0].click();
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(srv.calls.filter((u) => u === '/api/state').length, before, 'the clicks found a request already in flight and did nothing');
});

test('F1: whichever single request is in flight always applies (no stale completion can overwrite a newer one)', async () => {
  const srv = server();
  const { dom, fire } = await boot(srv);
  srv.down = true;
  await fire();
  assert.equal(app(dom).className, 'is-stale');
  srv.down = false;                       // recovered: the NEXT single-flight request will see this
  classOf(stream(dom), 'c2-retry')[0].click();
  await fire(under);
  assert.equal(app(dom).className, '', 'the one request that actually ran reflects the true, current state');
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

test('an answer for a different team is discarded, not shown, and counts as a failed read', async () => {
  const { dom } = await boot(server({
    attention: () => ({ target_root_project_id: 'someone-else', items: [ATT_ITEM({ id: 'x', title: 'Not ours' })] }),
    chat: () => ({ target_root_project_id: 'someone-else', available: true, lead: LEAD, messages: [{ from: LEAD, body: 'Not ours', ts: iso(1) }] }),
  }));
  const s = all(stream(dom));
  assert.ok(!s.includes('Not ours'));
  assert.ok(s.includes('Can\u2019t read what needs you.'), 'no usable attention data: no claim about what needs you');
  assert.ok(s.includes('Lead chat could not be read'));
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
  const rosterBefore = classOf(rail(dom), 'c2-roster')[0];
  const rosterTextBefore = rosterBefore.textContent;
  clock.perf += 2000;
  await fire();
  assert.strictEqual(stream(dom).children[0], first, 'same node: no redraw, scroll position kept');
  assert.strictEqual(rail(dom).children[0], railFirst);
  clock.perf += 120e3;
  await fire();
  const rosterAfter = classOf(rail(dom), 'c2-roster')[0];
  assert.strictEqual(rosterAfter, rosterBefore, 'F2: reconciled in place - the section is kept, not rebuilt');
  assert.notEqual(rosterAfter.textContent, rosterTextBefore, 'but a minute-level age change really did redraw its content');
});

// ============================================================================= M4a fix round: F2

test('F2: the rail is reconciled in place - an avatar image is the same element across an age redraw', async () => {
  const { dom, clock, fire } = await boot(server());
  const before = classOf(rail(dom), 'c2-avatar-img');
  assert.ok(before.length > 0);
  clock.perf += 120e3;
  await fire();
  const after = classOf(rail(dom), 'c2-avatar-img');
  assert.equal(after.length, before.length);
  before.forEach((img, i) => assert.strictEqual(after[i], img, 'the same <img>, not torn down and recreated'));
  // and the content genuinely did change (the roster line's age advanced), proving this is a real
  // reconcile, not merely "nothing redrew"
  const line = classOf(rail(dom), 'c2-agent-line').find((n) => n.textContent.includes('Idle'));
  assert.ok(line && /\d+m/.test(line.textContent));
});

test('F2: usage rows and roster rows are also kept in place across an ordinary redraw', async () => {
  const { dom, clock, fire } = await boot(server());
  const usageBefore = classOf(rail(dom), 'c2-usage-row');
  const agentBefore = classOf(rail(dom), 'c2-agent');
  clock.perf += 120e3;
  await fire();
  const usageAfter = classOf(rail(dom), 'c2-usage-row');
  const agentAfter = classOf(rail(dom), 'c2-agent');
  usageBefore.forEach((n, i) => assert.strictEqual(usageAfter[i], n));
  agentBefore.forEach((n, i) => assert.strictEqual(agentAfter[i], n));
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
    if (node.tagName === 'IMG' && node.getAttribute('src')) {
      assert.match(node.getAttribute('src'), /^\/static\/avatars\/[a-z0-9-]+\.png$/, 'the only allowed src shape');
    }
    assert.ok(!Object.keys(node.attributes).some((k) => k.startsWith('on') || k === 'style' || k === 'href'
      || (k === 'src' && node.tagName !== 'IMG')), node.tagName);
    assert.ok(/^[a-z0-9 _:-]*$/i.test(node.className), 'class names are fixed vocabulary: ' + node.className);
  }
});

// ------------------------------------------------------- F1: hung requests

test('a lead-chat request that never settles does not stop the state polls or the redraw', async () => {
  const srv = server();
  const { dom, clock, fire } = await boot(srv);
  srv.chat = () => PENDING;
  await fire(under);                       // the chat request is launched and hangs
  const stateCalls = () => srv.calls.filter((u) => u === '/api/state').length;
  const chatCalls = () => srv.calls.filter((u) => u.startsWith('/api/lead-chat')).length;
  const before = [stateCalls(), chatCalls()];
  for (let i = 0; i < 3; i++) { clock.perf += 2000; await fire(under); }
  assert.equal(stateCalls(), before[0] + 3, 'the state keeps being polled');
  assert.equal(chatCalls(), before[1], 'the hung feed is not stacked with new requests');
  assert.ok(all(rail(dom)).includes('TEAM · 9'), 'the roster is still drawn');
});

test('old chat data does not look live while its refresh hangs: a stale note appears on the repaint clock alone', async () => {
  const msgs = [{ from: LEAD, to: 'operator', body: 'Older word from the lead.', ts: iso(60) }];
  const srv = server({ chat: () => ({ target_root_project_id: 'proj-a', available: true, lead: LEAD, messages: msgs }) });
  const { dom, clock, fire } = await boot(srv);
  assert.ok(all(stream(dom)).includes('Older word from the lead.'));
  assert.ok(!all(stream(dom)).includes('not refreshed'));
  srv.chat = () => PENDING;
  await fire(under);                       // launches the hanging request
  clock.perf += 9000;
  srv.down = true;                         // the state is not answering either: only the repaint clock runs
  await fire((ms) => ms === 1000);
  assert.ok(all(stream(dom)).includes('Older word from the lead.'), 'the message stays');
  assert.ok(all(stream(dom)).includes('Lead chat not refreshed for 9s'));
});

test('a request that hits the timeout is aborted, recorded as failed, and retried on the next round', async () => {
  const srv = server();
  const { dom, fire } = await boot(srv);
  srv.chat = () => PENDING;
  await fire(under);
  const hung = srv.inits[srv.inits.length - 1];
  assert.ok(hung.signal && hung.signal.aborted === false);
  const before = srv.calls.filter((u) => u.startsWith('/api/lead-chat')).length;
  await fire(timeouts);                    // the 5 s timeout fires
  assert.equal(hung.signal.aborted, true, 'the request is aborted');
  assert.ok(all(stream(dom)).includes('Lead chat could not be read'));
  await fire(under);                       // next round: the feed is free again
  assert.equal(srv.calls.filter((u) => u.startsWith('/api/lead-chat')).length, before + 1);
});

test('without AbortController the timeout still bounds the wait', async () => {
  const srv = server({ chat: () => PENDING });
  const { dom, fire } = await boot(srv, { noAbort: true });
  assert.deepEqual(Object.keys(srv.inits[0]), ['cache']);
  await fire(timeouts);
  assert.ok(all(stream(dom)).includes('Lead chat could not be read'));
});

test('a response whose body never arrives is bounded too', async () => {
  const srv = server({ chat: () => JSON_HANG });
  const { dom, fire } = await boot(srv);
  await fire(timeouts);
  assert.ok(all(stream(dom)).includes('Lead chat could not be read'));
});

test('an attention request that hangs ends as "cannot read", not as an empty "loading" forever', async () => {
  const { dom, fire } = await boot(server({ attention: () => PENDING }));
  assert.ok(all(stream(dom)).includes('Waiting for the first snapshot.'), 'while it is pending, no claim either way');
  await fire(timeouts);
  assert.ok(all(stream(dom)).includes('Can’t read what needs you.'));
  assert.ok(all(rail(dom)).includes('TEAM · 9'));
});

test('a hung state read on the first load ends in the unreachable banner', async () => {
  const srv = server();
  srv.pendingState = true;
  const { dom, timers, fire } = await boot(srv);
  assert.equal(all(stream(dom)), '', 'nothing is claimed while the first read is pending');
  assert.ok(timers.some((t) => t.ms === 5000));
  await fire(timeouts);
  assert.ok(all(stream(dom)).includes('CAN’T REACH THE CONSOLE SERVER'));
  assert.ok(all(stream(dom)).includes('No snapshot has arrived yet.'));
  assert.equal(classOf(header(dom), 'c2-chip')[0].textContent, 'No team data');
});

test('a hung state read later on greys the page with the last data kept', async () => {
  const srv = server();
  const { dom, clock, fire } = await boot(srv);
  srv.pendingState = true;
  clock.perf += 2000;
  await fire(under);                       // the next poll hangs
  assert.equal(app(dom).className, '', 'not yet: the request is still inside its timeout');
  await fire(timeouts);
  assert.equal(app(dom).className, 'is-stale');
  assert.ok(all(stream(dom)).includes('CAN’T REACH THE CONSOLE SERVER'));
  assert.ok(all(rail(dom)).includes('TEAM · 9'));
});

test('a response that arrives in time clears its timeout (no leftover timers)', async () => {
  const { timers } = await boot(server());
  assert.deepEqual(timers.map((t) => t.ms).sort(), [1000, 2000]);
});

// -------------------------------------------- F3: chat failures are shown

test('a chat read that fails after a good one keeps the message and says so; recovery clears the note', async () => {
  const msgs = [{ from: LEAD, to: 'operator', body: 'Standing by.', ts: iso(100) }];
  const srv = server({ chat: () => ({ target_root_project_id: 'proj-a', available: true, lead: LEAD, messages: msgs }) });
  const { dom, clock, fire } = await boot(srv);
  srv.chat = () => ({ __status: 500 });
  clock.perf += 6000;
  await fire();
  const s = all(stream(dom));
  assert.ok(s.includes('Standing by.'));
  assert.ok(/Lead chat could not be read · last read [5-7]s ago/.test(s), s);
  srv.chat = () => ({ target_root_project_id: 'proj-a', available: true, lead: LEAD, messages: msgs });
  await fire();
  assert.ok(!all(stream(dom)).includes('could not be read'));
});

test('an unavailable lead is shown next to an older message', async () => {
  const msgs = [{ from: LEAD, to: 'operator', body: 'Last thing I said.', ts: iso(600) }];
  const { dom } = await boot(server({ chat: () => ({ target_root_project_id: 'proj-a', available: false, error: 'lead_unavailable',
    detail: 'lead heartbeat stale', lead: LEAD, messages: msgs }) }));
  const s = all(stream(dom));
  assert.ok(s.includes('Last thing I said.'));
  assert.ok(s.includes('The lead is unavailable: lead heartbeat stale'));
});

// --------------------------------- F4: attention errors-as-data keep the last decisions

test('attention errors-as-data after a good read keep the last decisions, flagged, and recovery replaces them', async () => {
  const good = (title) => () => ({ target_root_project_id: 'proj-a', items: [ATT_ITEM({ id: 'd1', title, age: 3600 })] });
  const srv = server({ attention: good('Pending decision A') });
  const { dom, fire } = await boot(srv);
  assert.ok(all(stream(dom)).includes('Pending decision A'));
  srv.attention = () => ({ target_root_project_id: 'proj-a', items: [], errors: ['scan failed'] });
  await fire();
  const s = all(stream(dom));
  assert.ok(s.includes('Pending decision A'), 'the decision is not erased');
  assert.ok(s.includes('Can’t read what needs you.'), 'and the failure is stated');
  assert.ok(!s.includes('scan failed'), 'the error text is never shown');
  assert.equal(app(dom).className, 'is-stale');
  srv.attention = good('Pending decision B');
  await fire();
  const after = all(stream(dom));
  assert.ok(after.includes('Pending decision B') && !after.includes('Pending decision A'));
  assert.ok(!after.includes('Can’t read what needs you.'));
  assert.equal(app(dom).className, '');
});

test('errors-as-data on the very first attention read says cannot read, with no items to keep', async () => {
  const calm = () => [root({ project_id: 'proj-a', agents: [agent(LEAD)], recent: [env(LEAD, 'x', 'message', 5)] })];
  const { dom } = await boot(server({ roots: calm, attention: () => ({ target_root_project_id: 'proj-a', items: [], errors: ['x'] }) }));
  assert.ok(all(stream(dom)).includes('Can’t read what needs you.'));
  assert.equal(classOf(stream(dom), 'c2-card').length, 0);
});

run();
