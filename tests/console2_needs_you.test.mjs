// B7: the shared needs-you projection and incident identity (work board slice #207).
// Pure functions extracted from console2-model.js's own needs-you calculation (merged console v2,
// M2/M4) so a future board calls the SAME code the stream already uses - never a second, drifting
// copy. Fixtures match the real wire shape build_attention (attention.py) and build_lead_chat
// (web.py) publish: an escalation's source_refs[0] carries {kind:"message", request_id, work_item,
// work_cycle} - work_item/work_cycle present ONLY when work_tags.item_ref validated both on the
// escalation's own opener, absent (never guessed) otherwise.
// Run: node tests/console2_needs_you.test.mjs   (also run by tests/test_console2_web.py)
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { createRunner } from './console2_harness.mjs';
import {
  ATT_ITEM, CONN_OK, NOW, PENDING_DECISION, agent, attention, chat, root,
} from './console2_fixtures.mjs';

const M = createRequire(import.meta.url)('../src/agenttalk/web_static/console2-model.js');
const { test, run } = createRunner('console2 needs-you projection');
const TZ = 'utc';
const LEAD = 'claude-agenttalk-lead';

const view = (o = {}) => M.buildTeamView({
  nowMs: NOW, generatedMs: NOW, tz: TZ, conn: CONN_OK, ui: o.ui || {}, canAct: false,
  root: root({ agents: [agent(LEAD, { since: 3000 })], operator_facing: LEAD, ...o.root }),
  attention: o.attention === undefined ? attention([]) : o.attention,
  chat: o.chat === undefined ? null : o.chat,
});

// ------------------------------------------------------------ incidentRef

test('incidentRef: linked, unlinked and malformed escalations', () => {
  const linked = ATT_ITEM({ id: 'a', linked: true, requestId: 'esc-a', workItem: 'gate-b', workCycle: '2' });
  assert.deepEqual(M.incidentRef(linked), { escalationId: 'esc-a', workItem: 'gate-b', workCycle: '2', linked: true });

  const unlinked = ATT_ITEM({ id: 'b', linked: false, requestId: 'esc-b' });
  assert.deepEqual(M.incidentRef(unlinked), { escalationId: 'esc-b', workItem: null, workCycle: null, linked: false });

  const noRefs = ATT_ITEM({ id: 'c' });
  assert.deepEqual(M.incidentRef(noRefs), { escalationId: null, workItem: null, workCycle: null, linked: false });

  // A ref present, but not the {kind:"message"} shape build_attention actually publishes (e.g. a
  // gate/dead-letter ref that happens to sit alongside): never read as an incident.
  const wrongKind = ATT_ITEM({ id: 'd', source_refs: [{ kind: 'gate', scope: 'x', name: 'y' }] });
  assert.deepEqual(M.incidentRef(wrongKind), { escalationId: null, workItem: null, workCycle: null, linked: false });

  // A malformed ref that carries SOME identifying text but never both required keys is still
  // unlinked, not partially linked.
  const partial = ATT_ITEM({ id: 'e', source_refs: [{ kind: 'message', request_id: 'esc-e', work_item: 'gate-b' }] });
  assert.deepEqual(M.incidentRef(partial), { escalationId: 'esc-e', workItem: 'gate-b', workCycle: null, linked: false });
});

// ------------------------------------------------------------ projectIncidents

test('projectIncidents: escalations only, deduplicated by escalation id, unlinked kept as unlinked', () => {
  const items = [
    ATT_ITEM({ id: 'a', linked: true, requestId: 'esc-a', workItem: 'gate-b', workCycle: '1' }),
    ATT_ITEM({ id: 'a-again', linked: true, requestId: 'esc-a', workItem: 'gate-b', workCycle: '1' }), // same escalation
    ATT_ITEM({ id: 'b', linked: false, requestId: 'esc-b' }),
    ATT_ITEM({ id: 'gate', source: 'gate', source_label: 'GATE HOLD' }), // not an escalation at all
  ];
  const incidents = M.projectIncidents(items);
  assert.equal(incidents.length, 2, 'the repeated escalation id counted once, the gate item not at all');
  assert.deepEqual(incidents.map((i) => i.escalationId).sort(), ['esc-a', 'esc-b']);
  const a = incidents.find((i) => i.escalationId === 'esc-a');
  assert.deepEqual(a, { escalationId: 'esc-a', workItem: 'gate-b', workCycle: '1', linked: true, itemId: 'a' });
  const b = incidents.find((i) => i.escalationId === 'esc-b');
  assert.equal(b.linked, false, 'unlinked stays team attention, not dropped');
});

test('projectIncidents: non-array/garbage input never throws', () => {
  assert.deepEqual(M.projectIncidents(null), []);
  assert.deepEqual(M.projectIncidents(undefined), []);
  assert.deepEqual(M.projectIncidents('nope'), []);
});

// ------------------------------------------------------------ incidentsForItem

test('incidentsForItem: exact (workItem, workCycle) match only - no spraying, no id/slug confusion', () => {
  const incidents = M.projectIncidents([
    ATT_ITEM({ id: 'a', linked: true, requestId: 'gate-b', workItem: 'gate-b', workCycle: '1' }),
    ATT_ITEM({ id: 'b', linked: true, requestId: 'esc-b', workItem: 'gate-b', workCycle: '2' }),
    ATT_ITEM({ id: 'c', linked: true, requestId: 'esc-c', workItem: 'other-item', workCycle: '1' }),
    ATT_ITEM({ id: 'd', linked: false, requestId: 'esc-d' }),
  ]);
  const cycle1 = M.incidentsForItem(incidents, 'gate-b', '1');
  assert.deepEqual(cycle1.map((i) => i.escalationId), ['gate-b'], 'exact item+cycle only');
  // Escalation "gate-b" is a request_id that happens to spell a plausible work-item slug - it must
  // never be matched AS a work item unless the item's OWN workItem field says so (it does here,
  // coincidentally, but the match is on workItem, never on escalationId - proven by cycle mismatch).
  assert.equal(M.incidentsForItem(incidents, 'gate-b', '99').length, 0);
  // A stuck mapping needs fresh, exact correlation: cycle 2 of the SAME item is a different
  // incident set entirely - cycle 1's incident does not leak into it.
  const cycle2 = M.incidentsForItem(incidents, 'gate-b', '2');
  assert.deepEqual(cycle2.map((i) => i.escalationId), ['esc-b']);
  assert.equal(M.incidentsForItem(incidents, 'other-item', '1').length, 1);
  assert.equal(M.incidentsForItem(incidents, 'never-seen', '1').length, 0);
  assert.equal(M.incidentsForItem(incidents, null, null).length, 0, 'an unlinked incident matches nothing, ever');
});

// ------------------------------------------------------------ dedupeAttentionAndChat

test('dedupeAttentionAndChat: matched by escalation id; an unmatched pending decision surfaces as chatOnly', () => {
  const items = [ATT_ITEM({ id: 'a', linked: true, requestId: 'esc-a', workItem: 'gate-b' })];
  const matched = PENDING_DECISION({ requestId: 'esc-a' });
  const unmatched = PENDING_DECISION({ requestId: 'esc-z', decision: 'A different question' });
  const result = M.dedupeAttentionAndChat(items, [matched, unmatched]);
  assert.deepEqual(result.chatOnly.map((d) => d.request_id), ['esc-z'], 'the already-carded escalation is not doubled');
});

test('dedupeAttentionAndChat: garbage/missing input never throws, and never invents a match', () => {
  assert.deepEqual(M.dedupeAttentionAndChat(null, null), { chatOnly: [] });
  const onlyChat = M.dedupeAttentionAndChat([], [PENDING_DECISION({ requestId: 'esc-x' })]);
  assert.deepEqual(onlyChat.chatOnly.map((d) => d.request_id), ['esc-x']);
});

// F2: freshness/availability travel WITH the decision, not only as prose elsewhere.
test('dedupeAttentionAndChat: a chat-only decision carries the chat read\'s own freshness and availability', () => {
  const fresh = M.dedupeAttentionAndChat([], [PENDING_DECISION({ requestId: 'esc-x' })], { stale: false, available: true });
  assert.equal(fresh.chatOnly[0].sourceStale, false);
  assert.equal(fresh.chatOnly[0].sourceAvailable, true);

  const failed = M.dedupeAttentionAndChat([], [PENDING_DECISION({ requestId: 'esc-x' })], { stale: true, available: false });
  assert.equal(failed.chatOnly[0].sourceStale, true);
  assert.equal(failed.chatOnly[0].sourceAvailable, false, 'a failed chat read never looks available');
});

// ------------------------------------------------------------ mergeIncidentsWithChatOnly

test('mergeIncidentsWithChatOnly: a chat-only decision becomes an unlinked incident, never guessed a work item', () => {
  const incidents = M.projectIncidents([ATT_ITEM({ id: 'a', linked: true, requestId: 'esc-a', workItem: 'gate-b' })]);
  const chatOnly = M.dedupeAttentionAndChat([], [PENDING_DECISION({ requestId: 'esc-x' })], { stale: true, available: false }).chatOnly;
  const merged = M.mergeIncidentsWithChatOnly(incidents, chatOnly);
  assert.deepEqual(merged.map((i) => i.escalationId).sort(), ['esc-a', 'esc-x']);
  const fromChat = merged.find((i) => i.escalationId === 'esc-x');
  assert.deepEqual(fromChat, {
    escalationId: 'esc-x', workItem: null, workCycle: null, linked: false, itemId: null,
    source: 'chat', sourceStale: true, sourceAvailable: false,
  });
});

test('mergeIncidentsWithChatOnly: an id already an attention incident is never duplicated from chat', () => {
  const incidents = M.projectIncidents([ATT_ITEM({ id: 'a', linked: true, requestId: 'esc-a', workItem: 'gate-b' })]);
  const chatOnly = [{ request_id: 'esc-a', sourceStale: false, sourceAvailable: true }];
  assert.equal(M.mergeIncidentsWithChatOnly(incidents, chatOnly).length, 1);
});

// ------------------------------------------------------------ missingIncidentIds (F1)

test('missingIncidentIds: requires currentTrustworthy===true; an untrustworthy read reports nothing missing', () => {
  const before = M.projectIncidents([ATT_ITEM({ id: 'a', linked: true, requestId: 'esc-a', workItem: 'x' })]);
  // F1 read-error case: a root/attention read that cannot be trusted must never make a
  // previously-known incident look resolved merely because the new read came back empty.
  assert.deepEqual(M.missingIncidentIds(before, [], false), [], 'unavailable/stale: report nothing');
  assert.deepEqual(M.missingIncidentIds(before, [], undefined), [], 'no explicit true: closed by default');
});

test('missingIncidentIds: a fresh, trustworthy absence is reported as missing, never asserted resolved', () => {
  const before = M.projectIncidents([
    ATT_ITEM({ id: 'a', linked: true, requestId: 'esc-a', workItem: 'x' }),
    ATT_ITEM({ id: 'b', linked: true, requestId: 'esc-b', workItem: 'x' }),
  ]);
  // F1 defer case: a snapshot-bound server-side DEFER removes an escalation from build_queue's
  // output while it is still pending - the identical wire shape as a genuine resolution. This
  // function is named, and behaves, as "missing": it never claims that absence is resolution.
  const after = M.projectIncidents([ATT_ITEM({ id: 'b', linked: true, requestId: 'esc-b', workItem: 'x' })]);
  assert.deepEqual(M.missingIncidentIds(before, after, true), ['esc-a']);
  assert.deepEqual(M.missingIncidentIds(after, before, true), [], 'nothing missing when the set only grows');
  assert.equal(M.resolvedIncidentIds, undefined, 'no export may claim resolution from mere absence');
});

// ------------------------------------------------------------ wired into buildTeamView (stream)

test('buildTeamView: view.needs.incidents mirrors projectIncidents exactly - stream/board parity by construction', () => {
  const items = [
    ATT_ITEM({ id: 'a', linked: true, requestId: 'esc-a', workItem: 'gate-b', workCycle: '1' }),
    ATT_ITEM({ id: 'b', linked: false, requestId: 'esc-b' }),
  ];
  const v = view({ attention: attention(items) });
  assert.deepEqual(v.needs.incidents, M.projectIncidents(items), 'the exact same function, not a re-derived copy');
});

test('buildTeamView: an escalation card carries its own incident identity', () => {
  const items = [ATT_ITEM({ id: 'a', linked: true, requestId: 'esc-a', workItem: 'gate-b', workCycle: '3' })];
  const v = view({ attention: attention(items) });
  const card = v.needs.open.find((c) => c.id === 'a');
  assert.ok(card, 'the card is open (nothing deferred/answered it)');
  assert.deepEqual(card.escalation, { escalationId: 'esc-a', workItem: 'gate-b', workCycle: '3', linked: true });
});

test('Later defers local presentation only: a deferred incident still counts as an incident', () => {
  const items = [ATT_ITEM({ id: 'a', linked: true, requestId: 'esc-a', workItem: 'gate-b' })];
  const fresh = view({ attention: attention(items) });
  assert.equal(fresh.needs.open.length, 1);
  const deferred = view({ attention: attention(items), ui: { deferred: { a: NOW - 1000 } } });
  assert.equal(deferred.needs.open.length, 0, 'locally deferred: not shown as open');
  assert.equal(deferred.needs.deferredCount, 1);
  // The incident itself - the workflow fact - is exactly as present as before. Deferring never
  // resolves it.
  assert.deepEqual(deferred.needs.incidents, fresh.needs.incidents);
  assert.equal(deferred.needs.incidents[0].escalationId, 'esc-a');
});

test('buildTeamView: chatOnlyDecisions surfaces a pending decision attention has not caught up on', () => {
  const items = [ATT_ITEM({ id: 'a', linked: true, requestId: 'esc-a', workItem: 'gate-b' })];
  const msgs = [{ from: LEAD, to: 'operator', body: 'hi', ts: new Date(NOW - 1000).toISOString() }];
  const bothKnown = view({
    attention: attention(items),
    chat: chat(msgs, { pendingDecisions: [PENDING_DECISION({ requestId: 'esc-a' })] }),
  });
  assert.deepEqual(bothKnown.needs.chatOnlyDecisions, [], 'already carded - not surfaced a second time');

  const chatAhead = view({
    attention: attention(items),
    chat: chat(msgs, { pendingDecisions: [PENDING_DECISION({ requestId: 'esc-a' }), PENDING_DECISION({ requestId: 'esc-new', decision: 'Fresh one' })] }),
  });
  assert.deepEqual(chatAhead.needs.chatOnlyDecisions.map((d) => d.request_id), ['esc-new']);
});

test('buildTeamView: a stale attention read keeps its incidents - never hidden, only marked stale', () => {
  const items = [ATT_ITEM({ id: 'a', linked: true, requestId: 'esc-a', workItem: 'gate-b' })];
  const v = view({ attention: attention(items, { asOfMs: NOW - 9000 }) });
  assert.equal(v.needs.stale, true);
  assert.equal(v.needs.incidents.length, 1, 'the incident is not thrown away merely because the read is stale');
});

// F1 read-error case: a root read error must mark needs unavailable, never silently default to
// "available" - a caller diffing against this empty incident set must not read it as resolution.
test('buildTeamView: a root read error marks needs.available false, never a silent all-clear', () => {
  const v = view({ root: root({ errors: ['store unreadable'] }) });
  assert.equal(v.mode, 'error');
  assert.equal(v.needs.available, false);
  assert.deepEqual(v.needs.incidents, []);
});

// F2 exact reproduction: fresh empty attention, a 60-second-old chat read with ok=false and one
// pending decision. The decision must carry its own source freshness/availability, and the shared
// incident set must include it (rather than reporting needs.incidents=[] while an incident exists).
test('buildTeamView: a failed, stale chat read never presents its pending decision as fresh or resolved', () => {
  const msgs = [{ from: LEAD, to: 'operator', body: 'hi', ts: new Date(NOW - 1000).toISOString() }];
  const v = view({
    attention: attention([]),
    chat: {
      ok: false, asOfMs: NOW - 60000,
      payload: { available: true, operator: 'operator', lead: LEAD, messages: msgs, detail: '',
                 pending_decisions: [PENDING_DECISION({ requestId: 'esc-probe' })] },
    },
  });
  assert.equal(v.needs.stale, false, 'needs.stale reflects the ATTENTION feed only, unaffected by a failed chat read');
  assert.equal(v.needs.chatOnlyDecisions[0].sourceStale, true, 'a 60s-old failed chat read is stale');
  assert.equal(v.needs.chatOnlyDecisions[0].sourceAvailable, false, 'a failed read is never presented as available');
  assert.equal(v.needs.incidents.length, 1, 'a chat-only pending decision is part of the shared incident set');
  assert.equal(v.needs.incidents[0].linked, false, 'pending_decisions carries no work_item/work_cycle - never guessed');
});

run();
