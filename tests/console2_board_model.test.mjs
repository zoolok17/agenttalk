// B8: the work-board console model (#207) - pure router parsing and per-card projection.
// Fixtures match the REAL wire shapes: /api/work-board items use work_board.py's own field
// names (work_item, workflow_column, obligations[{recipient,state}], verdicts, integration,
// evidence, checks, first_dispatch_at/last_work_event_at) - not the design doc's aspirational
// §5 field list, which this shipped reducer does not produce. incidentsForItem/projectIncidents
// are the SAME B7 functions the stream uses; this file never recomputes needs-you itself.
// Run: node tests/console2_board_model.test.mjs   (also run by tests/test_console2_web.py)
import assert from 'node:assert/strict';
import { createRunner } from './console2_harness.mjs';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const M2 = require('../src/agenttalk/web_static/console2-board-model.js');
const { test, run } = createRunner('console2 board model');

const NOW = Date.UTC(2026, 8, 28, 12, 0, 0);
const iso = (secondsAgo) => new Date(NOW - secondsAgo * 1000).toISOString();

// A minimal, real-shaped work-board item (work_board.py's _evaluate_item/_place output plus the
// feed's first_dispatch_at/last_work_event_at enrichment).
function ITEM(o = {}) {
  return {
    work_item: o.workItem === undefined ? 'gate-b' : o.workItem,
    title: o.title === undefined ? null : o.title,
    cycle: o.cycle === undefined ? 1 : o.cycle,
    round: o.round === undefined ? null : o.round,
    legacy_cycle: o.legacyCycle === true,
    candidate: o.candidate === undefined ? null : o.candidate,
    obligations: o.obligations === undefined ? [
      { request_id: 'rq-1', recipient: 'claude-agenttalk-developer-2', stage: 'build', state: 'outstanding', verdict: null },
    ] : o.obligations,
    verdicts: o.verdicts === undefined ? {} : o.verdicts,
    integration: o.integration === undefined ? {} : o.integration,
    workflow_column: o.workflowColumn === undefined ? 'building' : o.workflowColumn,
    reason: o.reason === undefined ? 'build accepted' : o.reason,
    evidence: o.evidence === undefined ? ['rq-1'] : o.evidence,
    checks: o.checks === undefined ? null : o.checks,
    issues: o.issues === undefined ? [] : o.issues,
    first_dispatch_at: o.firstDispatchAgo === undefined ? iso(3600) : (o.firstDispatchAgo === null ? null : iso(o.firstDispatchAgo)),
    last_work_event_at: o.lastWorkEventAgo === undefined ? iso(120) : (o.lastWorkEventAgo === null ? null : iso(o.lastWorkEventAgo)),
  };
}

// A B7 incident (console2-model.js's own projectIncidents/mergeIncidentsWithChatOnly shape).
function INCIDENT(o = {}) {
  return {
    escalationId: o.escalationId || 'esc-1', workItem: o.workItem === undefined ? 'gate-b' : o.workItem,
    workCycle: o.workCycle === undefined ? '1' : o.workCycle, linked: o.linked !== false, itemId: o.itemId || 'esc-1',
  };
}

// ------------------------------------------------------------------------- parseRoute / routeHash

test('parseRoute: board, conversation (default and explicit), and reserved review=<id>', () => {
  assert.deepEqual(M2.parseRoute('#board'), { mode: 'board', reviewId: null });
  assert.deepEqual(M2.parseRoute(''), { mode: 'conversation', reviewId: null });
  assert.deepEqual(M2.parseRoute('#conversation'), { mode: 'conversation', reviewId: null });
  assert.deepEqual(M2.parseRoute('#review=20260928-072439-916844-XcKT'),
    { mode: 'review', reviewId: '20260928-072439-916844-XcKT' });
});

test('parseRoute: malformed fragments always fall back to conversation, never throw', () => {
  for (const bad of ['#bogus', '#review=', '#review', '#Board', '#board ', '#review=%00', null, undefined, 42, '#/etc/passwd']) {
    assert.deepEqual(M2.parseRoute(bad), { mode: 'conversation', reviewId: null }, String(bad));
  }
});

test('routeHash: hash-only strings, so a nav link preserves the current ?root= and path by construction', () => {
  assert.equal(M2.routeHash('board'), '#board');
  assert.equal(M2.routeHash('conversation'), '#conversation');
  assert.ok(M2.routeHash('board').indexOf('?') === -1 && M2.routeHash('board').indexOf('/') === -1);
});

// ------------------------------------------------------------------------------------ boardCard

test('boardCard: seats are distinct recipients, sorted; obligations open/total counted', () => {
  const item = ITEM({
    obligations: [
      { request_id: 'r1', recipient: 'zeta', stage: 'build', state: 'outstanding', verdict: null },
      { request_id: 'r2', recipient: 'alpha', stage: 'build', state: 'done', verdict: 'done' },
      { request_id: 'r3', recipient: 'alpha', stage: 'fix', state: 'outstanding', verdict: null },
    ],
  });
  const card = M2.boardCard(item, { nowMs: NOW, incidents: [] });
  assert.deepEqual(card.seats, ['alpha', 'zeta']);
  assert.equal(card.obligationsTotal, 3);
  assert.equal(card.obligationsOpen, 2);
});

test('boardCard: title falls back to the slug; round/candidate/checks null render as Unknown-shaped', () => {
  const card = M2.boardCard(ITEM({ title: null, round: null, candidate: null, checks: null }), { nowMs: NOW, incidents: [] });
  assert.equal(card.title, 'gate-b');
  assert.equal(card.round, null);
  assert.equal(card.checks, null);
});

test('boardCard: vendor is read from verdicts[candidate], never guessed; no candidate -> empty, "unverified" shown by caller', () => {
  const withVendor = M2.boardCard(ITEM({
    candidate: 'a'.repeat(40),
    verdicts: { [('a'.repeat(40))]: [{ reviewer: 'codex-agenttalk-reviewer-1', verdict: 'GO', reply: 'rp-1', independent: true, vendor: 'openai' }] },
  }), { nowMs: NOW, incidents: [] });
  assert.deepEqual(withVendor.vendors, [{ recipient: 'codex-agenttalk-reviewer-1', vendor: 'openai' }]);

  const missingVendor = M2.boardCard(ITEM({
    candidate: 'b'.repeat(40),
    verdicts: { [('b'.repeat(40))]: [{ reviewer: 'x', verdict: 'GO', reply: 'rp-2', independent: true, vendor: null }] },
  }), { nowMs: NOW, incidents: [] });
  assert.deepEqual(missingVendor.vendors, [{ recipient: 'x', vendor: 'unverified' }]);

  const noCandidate = M2.boardCard(ITEM({ candidate: null }), { nowMs: NOW, incidents: [] });
  assert.deepEqual(noCandidate.vendors, []);
});

test('boardCard: merge is integrated/not_integrated only when the reducer names THIS candidate, else unknown', () => {
  const head = 'c'.repeat(40);
  const integrated = M2.boardCard(ITEM({ candidate: head, integration: { [head]: true } }), { nowMs: NOW, incidents: [] });
  assert.equal(integrated.merge, 'integrated');
  const notIntegrated = M2.boardCard(ITEM({ candidate: head, integration: { [head]: false } }), { nowMs: NOW, incidents: [] });
  assert.equal(notIntegrated.merge, 'not_integrated');
  const unknownNoCandidate = M2.boardCard(ITEM({ candidate: null, integration: {} }), { nowMs: NOW, incidents: [] });
  assert.equal(unknownNoCandidate.merge, 'unknown');
  const unknownUnrelatedIntegration = M2.boardCard(ITEM({ candidate: head, integration: { ['d'.repeat(40)]: true } }), { nowMs: NOW, incidents: [] });
  assert.equal(unknownUnrelatedIntegration.merge, 'unknown');
});

test('boardCard: findings and cost are ALWAYS explicitly unknown in v1 - never guessed from Markdown', () => {
  const card = M2.boardCard(ITEM({}), { nowMs: NOW, incidents: [] });
  assert.deepEqual(card.findings, { count: null, status: 'unavailable' });
  assert.equal(card.cost, null);
});

test('boardCard: ages are approximate seconds from the feed timestamps, null (Unknown) when absent', () => {
  const card = M2.boardCard(ITEM({ firstDispatchAgo: 600, lastWorkEventAgo: 30 }), { nowMs: NOW, incidents: [] });
  assert.equal(card.firstDispatchAgeSeconds, 600);
  assert.equal(card.lastWorkEventAgeSeconds, 30);
  const unknownAges = M2.boardCard(ITEM({ firstDispatchAgo: null, lastWorkEventAgo: null }), { nowMs: NOW, incidents: [] });
  assert.equal(unknownAges.firstDispatchAgeSeconds, null);
  assert.equal(unknownAges.lastWorkEventAgeSeconds, null);
});

test('boardCard: the needs-you overlay changes the shown column but keeps the underlying placement', () => {
  const incidents = [INCIDENT({ workItem: 'gate-b', workCycle: '1' })];
  const card = M2.boardCard(ITEM({ workItem: 'gate-b', cycle: 1, workflowColumn: 'building' }), { nowMs: NOW, incidents: incidents });
  assert.equal(card.needsYou, true);
  assert.equal(card.column, 'needs_you');
  assert.equal(card.underlyingColumn, 'building', 'row 1: overlay on top, placement kept underneath');
  assert.equal(card.incidents.length, 1);
});

test('boardCard: an incident for a DIFFERENT cycle of the same item never overlays this card - no stuck mapping', () => {
  const incidents = [INCIDENT({ workItem: 'gate-b', workCycle: '2' })];
  const card = M2.boardCard(ITEM({ workItem: 'gate-b', cycle: 1 }), { nowMs: NOW, incidents: incidents });
  assert.equal(card.needsYou, false);
  assert.equal(card.column, card.underlyingColumn);
});

test('boardCard: an unlinked incident (no validated work_item) never sprays a warning onto any card', () => {
  const incidents = [INCIDENT({ workItem: null, workCycle: null, linked: false })];
  const card = M2.boardCard(ITEM({ workItem: 'gate-b', cycle: 1 }), { nowMs: NOW, incidents: incidents });
  assert.equal(card.needsYou, false);
});

test('boardCard: malformed/garbage item input never throws', () => {
  assert.doesNotThrow(() => M2.boardCard(null, { nowMs: NOW, incidents: [] }));
  assert.doesNotThrow(() => M2.boardCard({}, { nowMs: NOW, incidents: [] }));
  assert.doesNotThrow(() => M2.boardCard({ obligations: 'nope', evidence: 42, verdicts: 'x' }, { nowMs: NOW, incidents: [] }));
});

// --------------------------------------------------------------------------------- boardSummary

test('boardSummary: needs_you cards sort first, otherwise the feed order is preserved', () => {
  const feed = {
    items: [
      ITEM({ workItem: 'a', cycle: 1 }), ITEM({ workItem: 'b', cycle: 1 }), ITEM({ workItem: 'c', cycle: 1 }),
    ],
  };
  const incidents = [INCIDENT({ workItem: 'b', workCycle: '1' })];
  const summary = M2.boardSummary(feed, { nowMs: NOW, incidents: incidents });
  assert.deepEqual(summary.cards.map((c) => c.key), ['b', 'a', 'c']);
});

test('boardSummary: coverage/legacy/truncation pass through with defensive defaults on a partial feed', () => {
  const full = M2.boardSummary({
    items: [], legacy: { open_request_count: 4, known_lower_bound: 4 }, coverage: { status: 'stale' },
    last_known: true, truncated: true, omitted_count: 3, errors: ['board coverage stale'], window_days: 7,
    generated_at: iso(5),
  }, { nowMs: NOW, incidents: [] });
  assert.equal(full.legacyOpenCount, 4);
  assert.equal(full.coverageStatus, 'stale');
  assert.equal(full.lastKnown, true);
  assert.equal(full.truncated, true);
  assert.equal(full.omittedCount, 3);
  assert.deepEqual(full.errors, ['board coverage stale']);
  assert.equal(full.windowDays, 7);

  const bare = M2.boardSummary({}, { nowMs: NOW, incidents: [] });
  assert.deepEqual(bare.cards, []);
  assert.equal(bare.legacyOpenCount, null, 'unknown, never zero success');
  assert.equal(bare.legacyLowerBound, 0);
  assert.equal(bare.coverageStatus, 'unavailable');
  assert.equal(bare.lastKnown, false);
  assert.equal(bare.truncated, false);
  assert.equal(bare.omittedCount, null);
  assert.deepEqual(bare.errors, []);
  assert.equal(bare.generatedAtMs, null);
});

test('boardSummary: garbage/missing feed input never throws', () => {
  assert.doesNotThrow(() => M2.boardSummary(null, { nowMs: NOW, incidents: [] }));
  assert.doesNotThrow(() => M2.boardSummary(undefined, {}));
  assert.doesNotThrow(() => M2.boardSummary({ items: 'nope', legacy: 1, coverage: 'x' }, { nowMs: NOW }));
});

run();
