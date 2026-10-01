// B8 work board (#207) - PR #221 fix round: board rendering driven through the REAL console2.js
// (not just the pure model), a real fetch mock and real timers, exactly like console2_data.test.mjs
// does for the stream. Each test here targets one of reviewer-1's cross-vendor findings (F1/F2/F3/
// F5/F6); F4 (native key activation) is a real-browser check instead (console2_browser_check.mjs),
// and F7 is the pytest node-runner registration itself (tests/test_console2_web.py).
// Run: node tests/console2_board_app.test.mjs   (also run by tests/test_console2_web.py)
process.env.TZ = 'UTC';

import assert from 'node:assert/strict';
import { createRunner, walk } from './console2_harness.mjs';
import { NOW, agent, root } from './console2_fixtures.mjs';
import {
  LEAD, all, board, boardCadence, boardDetail, header, server, under, boot,
} from './console2_app.mjs';

const { test, run } = createRunner('console2 board app (F1/F2/F3/F5/F6/N1/N2)');

// Dispatches a native-shaped keydown and reports whether the handler called preventDefault - N2's
// own bug was calling it unconditionally for Enter/Space on #board, suppressing whatever native
// activation an UNRELATED focused control (a theme button, a link) would otherwise have received.
function dispatchKey(dom, k, target) {
  const ev = { key: k, target: target || { tagName: 'BODY' }, prevented: false };
  ev.preventDefault = () => { ev.prevented = true; };
  dom.document.dispatch('keydown', ev);
  return ev;
}
const iso = (msFromNow) => new Date(NOW + msFromNow).toISOString();

// A real-shaped /api/work-board item.
function BOARD_ITEM(o = {}) {
  return {
    work_item: o.workItem === undefined ? 'gate-b' : o.workItem, title: null,
    cycle: o.cycle === undefined ? 1 : o.cycle, round: null, legacy_cycle: false,
    candidate: null, obligations: [
      { request_id: 'rq-1', recipient: 'claude-agenttalk-developer-2', stage: 'build', state: 'outstanding', verdict: null },
    ],
    verdicts: {}, integration: {},
    workflow_column: o.workflowColumn === undefined ? 'ready' : o.workflowColumn,
    reason: o.reason === undefined ? 'reviewed; independent GO; local checks not tracked' : o.reason,
    evidence: ['rq-1'], checks: 'local checks not tracked', issues: [],
    first_dispatch_at: iso(-3600000), last_work_event_at: iso(-60000),
  };
}

function BOARD_FEED(o = {}) {
  return {
    schema_version: 1, target_root_project_id: 'proj-a', generated_at: o.generatedAt || iso(0),
    coverage: o.coverage === undefined ? { status: 'complete', valid_until: iso(15000) } : o.coverage,
    items: o.items === undefined ? [BOARD_ITEM()] : o.items,
    legacy: { open_request_count: 0, known_lower_bound: 0 }, unassigned: { count: 0 },
    total_count: o.totalCount === undefined ? (o.items ? o.items.length : 1) : o.totalCount,
    truncated: o.truncated === true, omitted_count: o.omittedCount === undefined ? 0 : o.omittedCount,
    errors: o.errors === undefined ? [] : o.errors, window_days: 7,
    ...(o.lastKnown ? { last_known: true } : {}),
  };
}

const oneRoot = () => [root({ project_id: 'proj-a', operator_facing: LEAD, agents: [agent(LEAD, { since: 3000 })] })];

// ------------------------------------------------------------------------------------------- F1

test('F1: a fresh, complete board renders its live column tone, never qualified as last known', async () => {
  const srv = server({ roots: oneRoot, board: () => BOARD_FEED() });
  const { dom, fire } = await boot(srv, { hash: '#board' });
  await fire(under);   // the first board GET lands via a short startup-race retry (< 5s); see boardLoop
  const b = all(board(dom));
  assert.ok(b.includes('READY'));
  assert.ok(!b.includes('LAST KNOWN'), b);
});

// wb-planned-lane connector P1 (4157051588): console2.js's own COLUMN_LABEL map had no entry
// for "planned", so a Planned card silently fell back to the generic UNKNOWN label - not a model
// bug (console2-board-model.js already passed workflow_column through correctly), only a missing
// rendering label. This checks the RENDERED text, not just the model's column field.
test('wb-planned-lane: a Planned card renders the PLANNED label, not UNKNOWN', async () => {
  const srv = server({ roots: oneRoot, board: () => BOARD_FEED({
    items: [BOARD_ITEM({ workItem: 'wb-lanes-ui', workflowColumn: 'planned',
                         reason: 'planned in board lanes and the v2 team views (r2)' })],
  }) });
  const { dom, fire } = await boot(srv, { hash: '#board' });
  await fire(under);
  const b = all(board(dom));
  assert.ok(b.includes('PLANNED'), b);
  assert.ok(!b.includes('UNKNOWN'), b);
});

test('F1: a server-marked last_known board (envelope_snapshot.py\'s cached-on-staleness path) demotes the card', async () => {
  const srv = server({ roots: oneRoot, board: () => BOARD_FEED({ lastKnown: true }) });
  const { dom, fire } = await boot(srv, { hash: '#board' });
  await fire(under);
  const b = all(board(dom));
  assert.ok(b.includes('LAST KNOWN'), b);
  assert.ok(b.includes('READY'), 'the underlying claim is still shown, only qualified, never hidden');
});

test('F1: an unchanged snapshot whose coverage.valid_until elapses (no new poll) demotes on its own clock', async () => {
  const srv = server({ roots: oneRoot, board: () => BOARD_FEED({ coverage: { status: 'complete', valid_until: iso(15000) } }) });
  const { dom, clock, fire } = await boot(srv, { hash: '#board' });
  await fire(under);
  assert.ok(!all(board(dom)).includes('LAST KNOWN'), 'fresh at boot');
  clock.perf += 20000;                 // past the ORIGINAL valid_until - no new /api/work-board call
  await fire((ms) => ms === 1000);     // only the 1s paint tick fires; time alone must trigger the demotion
  assert.ok(all(board(dom)).includes('LAST KNOWN'), all(board(dom)));
});

// ------------------------------------------------------------------------------------------- F2

test('F2: a failed board GET retains and demotes the last-known cards and selection, never blanks them', async () => {
  let fail = false;
  const srv = server({ roots: oneRoot, board: () => { if (fail) throw new Error('board read failed'); return BOARD_FEED(); } });
  const { dom, fire } = await boot(srv, { hash: '#board' });
  await fire(under);
  board(dom).children[0].click();      // select the one card
  assert.ok(all(boardDetail(dom)).includes('gate-b') || all(boardDetail(dom)).includes('reviewed'),
    'the detail panel shows the selection before the failure');
  fail = true;
  await fire(boardCadence);            // the next 5s board poll fails
  const b = all(board(dom));
  assert.ok(b.includes('READY') || b.includes('gate-b'), 'the retained card is still shown, not blanked');
  assert.ok(!b.includes('Board could not be read.'), 'a retained payload is never presented as no data at all');
  assert.ok(!all(boardDetail(dom)).includes('Select a card'),
    'the selection/detail survive a failed refresh, not reset to the placeholder');
});

// ------------------------------------------------------------------------------------------- F5

test('F5: a genuinely complete, empty, non-truncated board says so plainly', async () => {
  const srv = server({ roots: oneRoot, board: () => BOARD_FEED({ items: [], totalCount: 0 }) });
  const { dom, fire } = await boot(srv, { hash: '#board' });
  await fire(under);
  assert.ok(all(board(dom)).includes('No active or recently done work items.'));
});

test('F5: an overflow that omits the only item is never claimed as "no work items"', async () => {
  const srv = server({
    roots: oneRoot,
    board: () => BOARD_FEED({ items: [], totalCount: 1, truncated: true, omittedCount: 1 }),
  });
  const { dom, fire } = await boot(srv, { hash: '#board' });
  await fire(under);
  const b = all(board(dom));
  assert.ok(!b.includes('No active or recently done work items.'), b);
  assert.ok(b.includes('No cards can currently be shown.'), b);
  assert.ok(b.includes('Board truncated') && b.includes('1 omitted'), b);
});

// ------------------------------------------------------------------------------------------- F6

test('F6: the board polls on its OWN 5s cadence, not the stream\'s 2s one, and only while it is the visible route', async () => {
  const srv = server({ roots: oneRoot, board: () => BOARD_FEED() });
  const { fire } = await boot(srv, { hash: '#board' });
  // One fire(under) settles BOTH the board's own short startup-race retry (< 5s) and a stream 2s
  // tick together - if the stream's cadence also drove the board GET, this would already show 2.
  await fire(under);
  assert.equal(srv.calls.filter((u) => u.startsWith('/api/work-board')).length, 1,
    'exactly one board GET - the stream\'s own 2s cadence must never also drive it');
  await fire(boardCadence);              // the board's own 5s tick
  assert.equal(srv.calls.filter((u) => u.startsWith('/api/work-board')).length, 2);
});

test('F6: polling pauses while the document is hidden, and resumes immediately on visibilitychange', async () => {
  const srv = server({ roots: oneRoot, board: () => BOARD_FEED() });
  const { dom, fire } = await boot(srv, { hash: '#board' });
  await fire(under);
  const before = srv.calls.filter((u) => u.startsWith('/api/work-board')).length;
  assert.equal(before, 1);
  dom.document.hidden = true;
  await fire(boardCadence);              // the 5s tick fires, but must skip the GET while hidden
  assert.equal(srv.calls.filter((u) => u.startsWith('/api/work-board')).length, before,
    'a hidden document must never poll the board');
  dom.document.hidden = false;
  dom.document.dispatch('visibilitychange', {});
  await fire(under);
  assert.equal(srv.calls.filter((u) => u.startsWith('/api/work-board')).length, before + 1,
    'regaining visibility fetches right away, not after waiting out the rest of the 5s cadence');
});

// ------------------------------------------------------------------------------------------- N1

test('N1: repeated hide/show and #conversation<->#board round-trips never multiply the board\'s polling chain', async () => {
  const srv = server({ roots: oneRoot, board: () => BOARD_FEED() });
  const { dom, fire, timers, sandbox, windowEvents } = await boot(srv, { hash: '#board' });
  await fire(under);   // the initial board load settles (startup-race retry + a stream tick)

  function navigateHash(hash) {
    sandbox.location.hash = hash;
    (windowEvents.hashchange || []).forEach((fn) => fn());
  }
  function pendingBoardTimers() { return timers.filter((t) => boardCadence(t.ms)).length; }

  assert.equal(pendingBoardTimers(), 1, 'one owned chain right after the initial load');

  // Three hide/show cycles - reviewer-1's own repro ("one normal board timer became four after
  // three hide/show cycles"). `fire(under)` never touches a 5s-tagged timer, so if the bug were
  // present, each cycle's untracked reschedule would sit here accumulating, un-fired, unnoticed
  // until something eventually fired them - exactly the failure mode reviewer-1 found.
  for (let i = 0; i < 3; i++) {
    dom.document.hidden = true;
    dom.document.dispatch('visibilitychange', {});
    await fire(under);
    dom.document.hidden = false;
    dom.document.dispatch('visibilitychange', {});
    await fire(under);
  }
  // Two rapid #conversation -> #board round-trips too (the review names both triggers).
  for (let i = 0; i < 2; i++) {
    navigateHash('#conversation');
    await fire(under);
    navigateHash('#board');
    await fire(under);
  }

  assert.equal(pendingBoardTimers(), 1,
    'still exactly one owned chain after repeated hide/show and route round-trips, never several');

  // And firing that one tick produces exactly one GET, with staggered completions between
  // repeated ticks never compounding into more than one owned chain at a time.
  const before = srv.calls.filter((u) => u.startsWith('/api/work-board')).length;
  await fire(boardCadence);
  assert.equal(srv.calls.filter((u) => u.startsWith('/api/work-board')).length, before + 1);
  assert.equal(pendingBoardTimers(), 1, 'exactly one chain survives afterward too');
});

// ------------------------------------------------------------------------------------------- N2

test('N2: native Enter/Space on #board only activates a board card - an unrelated focused control (theme, help) keeps its own default', async () => {
  const srv = server({ roots: oneRoot, board: () => BOARD_FEED() });
  const { dom, fire } = await boot(srv, { hash: '#board' });
  await fire(under);

  const paperButton = walk(header(dom)).find((n) => n.tagName === 'BUTTON' && n.textContent === 'Paper');
  assert.ok(paperButton, 'the Paper theme button exists in the header');
  const evEnter = dispatchKey(dom, 'Enter', paperButton);
  assert.equal(evEnter.prevented, false, 'Enter on the theme button must keep its own native activation');
  // console2.js's own click handler is what actually changes the theme on a real click; a native
  // Enter on an ALREADY-FOCUSED real <button> fires that same click natively in a real browser -
  // what matters here, in this synthetic-event harness, is only that the key handler never
  // intercepted/suppressed it via preventDefault, which is exactly what N2's bug did.
  const evSpace = dispatchKey(dom, ' ', paperButton);
  assert.equal(evSpace.prevented, false, 'Space on the theme button must keep its own native activation');

  const helpButton = walk(header(dom)).find((n) => n.className === 'c2-keybtn');
  assert.ok(helpButton, 'the help (?) button exists in the header');
  const evHelp = dispatchKey(dom, 'Enter', helpButton);
  assert.equal(evHelp.prevented, false, 'Enter on the help button must keep its own native activation');

  // The positive case still works: a real board card IS consumed and activated. A selected card
  // is a DIFFERENT shape (className changes), so syncChildren replaces the node - re-query after
  // dispatch rather than reusing the pre-render reference, which would now be a detached, stale node.
  const evCard = dispatchKey(dom, 'Enter', board(dom).children[0]);
  assert.equal(evCard.prevented, true, 'Enter on an actual board card is still consumed');
  assert.ok(board(dom).children[0].className.indexOf('is-selected') >= 0, 'and still selects the card');
});

run();
