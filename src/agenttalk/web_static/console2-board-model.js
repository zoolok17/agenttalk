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

  // F3 (PR #221): the stream's OWN local Later/Wait state decides whether an escalation currently
  // reads as open, deferred or answered - a fact the board must show too (section 6: "both views
  // show the same incident AND deferral state"), never re-derived, only looked up by escalation id.
  function presentationOf(map, escalationId) {
    return (isObj(map) && typeof map[escalationId] === 'string') ? map[escalationId] : 'open';
  }

  // One /api/work-board item -> one card. `ctx.incidents` is the CALLER's already-computed B7
  // projection (e.g. the selected team's own `view.needs.incidents`) - this function never reads
  // attention or chat itself, only overlays that shared projection onto this item's (work_item,
  // cycle), by the SAME incidentsForItem exact-match rule the stream cards use. `ctx.trustworthy`
  // (computed once per feed by boardSummary, F1/F2) marks the WHOLE card `stale` when the feed
  // itself is a failed read, degraded/expired coverage, or explicitly last-known - never invented
  // per item, since the wire carries no per-item freshness, only feed-level coverage.
  function boardCard(item, ctx) {
    var it = isObj(item) ? item : {};
    var obligations = Array.isArray(it.obligations) ? it.obligations : [];
    var openCount = obligations.filter(function (o) { return isObj(o) && o.state === 'outstanding'; }).length;
    var workItem = typeof it.work_item === 'string' ? it.work_item : null;
    var cycleText = (typeof it.cycle === 'number') ? String(it.cycle) : null;
    var incidents = Base.incidentsForItem(ctx && ctx.incidents, workItem, cycleText).map(function (inc) {
      return { escalationId: inc.escalationId, workItem: inc.workItem, workCycle: inc.workCycle,
               linked: inc.linked, itemId: inc.itemId,
               presentation: presentationOf(ctx && ctx.incidentPresentation, inc.escalationId) };
    });
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
      // the overlay changes what COLUMN is shown, never what the reducer actually placed. Deferring
      // or answering an incident never changes this placement either (section 6) - only the
      // per-incident `presentation` above says so, for the caller to display explicitly (F3).
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
      // F1/F2: the card's own truth (column/reason/checks/merge/...) is exactly as good as the
      // FEED it came from - stale whenever that feed is not currently trustworthy (see boardSummary).
      stale: !(ctx && ctx.trustworthy === true),
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

  // F1/F2 (PR #221): "trustworthy" is the ONE feed-level fact that governs whether every card's own
  // claims (column, reason, checks, merge...) may be shown as CURRENT. False whenever:
  //   - the caller's own GET failed (`ctx.readFailed`; F2 - a failed read still has a retained,
  //     last-known payload to show, just never as live);
  //   - the server itself marked the response `last_known` (a supported degraded-coverage path,
  //     envelope_snapshot.py's cached-on-staleness retention);
  //   - coverage is not `complete` (building/stale/unavailable/capacity_exceeded);
  //   - or - the case with NO signal anywhere else in this response - `coverage.valid_until` has
  //     simply elapsed since this snapshot was generated. This is the only reason a WHOLLY UNCHANGED
  //     payload object can still need to be re-evaluated as time passes with no new poll landing;
  //     the caller must recompute this (and therefore re-render) on its own clock, not only when a
  //     new payload arrives.
  // Missing/malformed valid_until is treated as already expired (fail closed), matching this
  // project's "no stale state shown as live" rule everywhere else.
  function isTrustworthy(f, coverage, nowMs, readFailed) {
    if (readFailed === true || f.last_known === true) return false;
    if ((typeof coverage.status === 'string' ? coverage.status : 'unavailable') !== 'complete') return false;
    var freshUntilMs = Base.parseMs(coverage.valid_until);
    return freshUntilMs !== null && typeof nowMs === 'number' && nowMs <= freshUntilMs;
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
    var nowMs = ctx && typeof ctx.nowMs === 'number' ? ctx.nowMs : null;
    var trustworthy = isTrustworthy(f, coverage, nowMs, ctx && ctx.readFailed);
    var cardCtx = { nowMs: nowMs, incidents: ctx && ctx.incidents, trustworthy: trustworthy,
                     incidentPresentation: ctx && ctx.incidentPresentation };
    var cards = orderCards(items.map(function (item) { return boardCard(item, cardCtx); }));
    var coverageStatus = typeof coverage.status === 'string' ? coverage.status : 'unavailable';
    var truncated = f.truncated === true;
    // F5: "no work items" is a claim about a COMPLETE, non-truncated, trustworthy count of zero -
    // never the same message for a building/unavailable/degraded read or an overflow that omitted
    // the only item that exists. Those get their own, honestly different, "cannot show" wording.
    var knownEmpty = trustworthy && coverageStatus === 'complete' && !truncated && cards.length === 0;
    return {
      cards: cards,
      legacyOpenCount: (typeof legacy.open_request_count === 'number') ? legacy.open_request_count : null,
      legacyLowerBound: (typeof legacy.known_lower_bound === 'number') ? legacy.known_lower_bound : 0,
      coverageStatus: coverageStatus,
      lastKnown: f.last_known === true,
      readFailed: ctx && ctx.readFailed === true,
      trustworthy: trustworthy,
      knownEmpty: knownEmpty,
      truncated: truncated,
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
