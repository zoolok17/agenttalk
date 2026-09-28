/* =============================================================================
   agenttalk console v2 - console2-board-model.js   (work board slice B8, #207)

   PURE logic for the read-only board view: no DOM, no fetch, no timers, no
   storage - same discipline as console2-model.js, which this file requires
   (Node) or reads off window.AgentTalkConsole2Model (browser), never
   recomputing needs-you itself. The board calls the SAME projectIncidents /
   incidentsForItem / dedupeAttentionAndChat / mergeIncidentsWithChatOnly the
   stream already uses (B7); this file only shapes ONE work-board feed item
   (docs/WORK-BOARD-FEED.md, src/agenttalk/work_board.py's real field names -
   the design doc's aspirational §5 field list is NOT the shipped wire shape)
   into a card, and parses the one router's hash.

   Router: #board, #conversation (default - "no new default for /") and the
   reserved #review=<escalation-id> (no UI yet; recognised so a stray link
   never crashes or falls through to something else). A malformed fragment
   always falls back to #conversation, never throws.
   ============================================================================= */
(function (root, factory) {
  'use strict';
  var base = typeof module !== 'undefined' && module.exports
    ? require('./console2-model.js') : root.AgentTalkConsole2Model;
  var api = factory(base);
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api;
  } else {
    root.AgentTalkConsole2BoardModel = api;
  }
}(typeof window !== 'undefined' ? window : this, function (Base) {
  'use strict';
  if (!Base) return {};   // the shared model failed to load: stay inert, like console2.js does

  function isObj(v) { return v !== null && typeof v === 'object' && !Array.isArray(v); }
  function hasOwn(o, k) { return Object.prototype.hasOwnProperty.call(o, k); }

  // ------------------------------------------------------------------ router

  var REVIEW_ID_RE = /^review=([A-Za-z0-9][A-Za-z0-9_.:-]{0,199})$/;

  // Never throws; any fragment it does not recognise falls back to #conversation - the "no new
  // default for /" and "falls back safely on malformed fragments" rules from the same function.
  function parseRoute(hash) {
    var raw = typeof hash === 'string' ? hash.replace(/^#/, '') : '';
    if (raw === 'board') return { mode: 'board', reviewId: null };
    var m = REVIEW_ID_RE.exec(raw);
    if (m) return { mode: 'review', reviewId: m[1] };
    return { mode: 'conversation', reviewId: null };   // '', 'conversation', or anything else unrecognised
  }

  // For building the two chrome nav links: hash-only ("#board"), never a full path, so a browser
  // navigating it keeps the CURRENT ?root= and pathname untouched - the one mechanism this file
  // relies on to satisfy "preserves ?root=" without reading or reconstructing the query itself.
  function routeHash(mode) { return mode === 'board' ? '#board' : '#conversation'; }

  // -------------------------------------------------------------- board card

  function ageSeconds(iso, nowMs) {
    var t = Base.parseMs(iso);
    return t === null ? null : Math.max(0, (nowMs - t) / 1000);
  }

  // Distinct obligation recipients, in a stable (sorted) order - the card's "seats".
  function distinctSeats(obligations) {
    var seen = Object.create(null);
    var out = [];
    obligations.forEach(function (o) {
      if (isObj(o) && typeof o.recipient === 'string' && o.recipient && !hasOwn(seen, o.recipient)) {
        seen[o.recipient] = true;
        out.push(o.recipient);
      }
    });
    return out.sort();
  }

  // The operator-configured model_vendor of whoever reviewed the CURRENT candidate, from the
  // reducer's own verdicts table (item.verdicts[candidate]) - never inferred from CLI/name. No
  // candidate or no recorded verdicts on it: an empty list, which the card shows as "unverified".
  function candidateVendors(item) {
    var out = [];
    var seen = Object.create(null);
    var head = typeof item.candidate === 'string' ? item.candidate : null;
    var list = (head !== null && isObj(item.verdicts)) ? item.verdicts[head] : null;
    (Array.isArray(list) ? list : []).forEach(function (v) {
      if (isObj(v) && typeof v.reviewer === 'string' && v.reviewer && !hasOwn(seen, v.reviewer)) {
        seen[v.reviewer] = true;
        out.push({ recipient: v.reviewer, vendor: (typeof v.vendor === 'string' && v.vendor) ? v.vendor : 'unverified' });
      }
    });
    return out;
  }

  // integrated/not_integrated only when the reducer's own integration facts name the CURRENT
  // candidate explicitly; otherwise unknown - never guessed from a task being merely "done".
  function mergeStatus(item) {
    if (typeof item.candidate === 'string' && isObj(item.integration) && hasOwn(item.integration, item.candidate)) {
      return item.integration[item.candidate] ? 'integrated' : 'not_integrated';
    }
    return 'unknown';
  }

  // One /api/work-board item -> one card. `ctx.incidents` is the CALLER's already-computed B7
  // projection (e.g. the selected team's own `view.needs.incidents`) - this function never reads
  // attention or chat itself, only overlays that shared projection onto this item's (work_item,
  // cycle), by the SAME incidentsForItem exact-match rule the stream cards use.
  function boardCard(item, ctx) {
    var it = isObj(item) ? item : {};
    var obligations = Array.isArray(it.obligations) ? it.obligations : [];
    var openCount = obligations.filter(function (o) { return isObj(o) && o.state === 'outstanding'; }).length;
    var workItem = typeof it.work_item === 'string' ? it.work_item : null;
    var cycleText = (typeof it.cycle === 'number') ? String(it.cycle) : null;
    var incidents = Base.incidentsForItem(ctx && ctx.incidents, workItem, cycleText);
    var underlyingColumn = typeof it.workflow_column === 'string' ? it.workflow_column : 'unknown';
    return {
      key: workItem || '',
      title: (typeof it.title === 'string' && it.title) ? it.title : (workItem || 'unknown item'),
      reason: typeof it.reason === 'string' ? it.reason : 'Unknown',
      seats: distinctSeats(obligations),
      vendors: candidateVendors(it),
      round: (typeof it.round === 'number') ? it.round : null,
      obligationsOpen: openCount,
      obligationsTotal: obligations.length,
      firstDispatchAgeSeconds: ageSeconds(it.first_dispatch_at, ctx && ctx.nowMs),
      lastWorkEventAgeSeconds: ageSeconds(it.last_work_event_at, ctx && ctx.nowMs),
      evidence: Array.isArray(it.evidence) ? it.evidence.slice(0, 200) : [],
      // Row 1 of the design's precedence table: "needs_you overlay; underlying placement kept" -
      // the overlay changes what COLUMN is shown, never what the reducer actually placed.
      column: incidents.length ? 'needs_you' : underlyingColumn,
      underlyingColumn: underlyingColumn,
      needsYou: incidents.length > 0,
      incidents: incidents,
      checks: typeof it.checks === 'string' ? it.checks : null,
      // Section 6's own contract: never guessed from Markdown - always explicit Unknown in v1.
      findings: { count: null, status: 'unavailable' },
      cost: null,
      merge: mergeStatus(it),
      issues: Array.isArray(it.issues) ? it.issues : [],
      legacyCycle: it.legacy_cycle === true,
    };
  }

  // needs_you cards first (stable within each partition), matching row 1's overlay priority -
  // the underlying order (already the feed's own approximate event-time/key sort) is otherwise
  // left exactly as the server returned it.
  function orderCards(cards) {
    var needy = [];
    var rest = [];
    cards.forEach(function (c) { (c.needsYou ? needy : rest).push(c); });
    return needy.concat(rest);
  }

  // The whole /api/work-board feed -> the board's own view: ordered cards plus the passthrough
  // groups/coverage a caller needs to say "stale", "capacity_exceeded", or show the legacy count -
  // never invented fields, and every one individually defaulted so a partial/malformed feed still
  // renders something honest rather than throwing.
  function boardSummary(feed, ctx) {
    var f = isObj(feed) ? feed : {};
    var legacy = isObj(f.legacy) ? f.legacy : {};
    var coverage = isObj(f.coverage) ? f.coverage : {};
    var items = Array.isArray(f.items) ? f.items : [];
    return {
      cards: orderCards(items.map(function (item) { return boardCard(item, ctx); })),
      legacyOpenCount: (typeof legacy.open_request_count === 'number') ? legacy.open_request_count : null,
      legacyLowerBound: (typeof legacy.known_lower_bound === 'number') ? legacy.known_lower_bound : 0,
      coverageStatus: typeof coverage.status === 'string' ? coverage.status : 'unavailable',
      lastKnown: f.last_known === true,
      truncated: f.truncated === true,
      omittedCount: (typeof f.omitted_count === 'number') ? f.omitted_count : null,
      errors: Array.isArray(f.errors) ? f.errors : [],
      generatedAtMs: Base.parseMs(f.generated_at),
      windowDays: (typeof f.window_days === 'number') ? f.window_days : null,
    };
  }

  return {
    parseRoute: parseRoute,
    routeHash: routeHash,
    boardCard: boardCard,
    boardSummary: boardSummary,
  };
}));
