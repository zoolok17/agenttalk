'use strict';
/*
 * agenttalk Team Console — vanilla-JS single-page app (v0.68.1, READ-ONLY).
 *
 * Zero dependencies. CSP-safe under `script-src 'self'`: every node is built
 * via document.createElement / createElementNS + textContent. No raw-HTML
 * assignment, no dynamic code, no full-page reload, no inline handlers — all
 * events wired with addEventListener. Message bodies and all bus-derived
 * strings (subjects, names, tasks, meta) are UNTRUSTED and always land in
 * textContent.
 *
 * Codes to the frozen wire contract in docs/DashboardDesign/BUILD-SPEC.md
 * (§3 /api/state additive fields, §4 /api/attention + /api/thread/<rid>).
 * Class names follow the .tc-* / .status-<key> / .kind-<key> convention the
 * spec suggests; the lead reconciles any drift with Owner B's console.css.
 */
(function () {
  // A prototype-less lookup table: copies own keys onto a null-prototype object
  // so a lookup by UNTRUSTED wire data (severity/kind/source) can never resolve
  // to an inherited Object member (constructor, __proto__, …) and yield a
  // garbage value. Function-declaration hoisted, so callable from var inits.
  function nullMap(src) {
    var m = Object.create(null);
    for (var k in src) {
      if (Object.prototype.hasOwnProperty.call(src, k)) m[k] = src[k];
    }
    return m;
  }

  // ------------------------------------------------------------ constants
  var POLL_MS = 2000;   // /api/state data poll
  var CLOCK_MS = 1000;  // relative-age recompute + wall clock tick
  // How long a successful /api/attention payload stays "fresh" for the team-health
  // verdict (v0.76.0). fetchAttention polls every POLL_MS; a few missed polls
  // (outage) ages the data out -> the verdict shows "queue status unknown", never a
  // false green all-clear.
  var ATTENTION_STALE_MS = 4 * POLL_MS;
  var STATE_STALE_MS = 4 * POLL_MS;   // same freshness window for /api/state (agent health)

  var ACCENTS = ['blue', 'green', 'rust', 'violet', 'azure'];
  var THEMES = ['light', 'dark'];
  var DENSITIES = ['comfortable', 'compact'];
  var PREF_KEYS = { theme: 'tc.theme', accent: 'tc.accent', density: 'tc.density' };

  var VIEWS = ['overview', 'flow', 'attention', 'lead-chat', 'learning', 'onboarding', 'sessions', 'agent'];
  var VIEW_LABELS = nullMap({
    overview: 'Overview', flow: 'Flow', attention: 'Attention',
    'lead-chat': 'Lead chat', learning: 'Learning', onboarding: 'Onboarding',
    sessions: 'Sessions', agent: 'Agent',
  });

  // Graph geometry (frozen: §7 / prototype). 640x480 canvas, nodes on a
  // circle radius 178 around center (320,240), node i at angle -90 + i*36 deg.
  var GRAPH_W = 640, GRAPH_H = 480, GRAPH_CX = 320, GRAPH_CY = 240, GRAPH_R = 178;
  var SVG_NS = 'http://www.w3.org/2000/svg';

  // Attention severity → left-bar CSS color token (feeds --sev-color). The chip
  // and source-tag colors are owned by the CSS .sev-<level> / .src-<source> rules.
  // Null-prototype maps (P3): the key is UNTRUSTED wire data, so a severity like
  // "constructor" must miss cleanly (fall back to the default) rather than hit an
  // inherited Object property and produce a garbage className.
  var SEV_COLOR = nullMap({ high: 'danger', med: 'warn', low: 'gray' });
  var SEV_LABEL = nullMap({ high: 'HIGH', med: 'MED', low: 'LOW' });
  // Plain-language hover text for a zero-context viewer (v0.76.0). Keyed by the
  // UNTRUSTED wire severity/source, so null-proto (a bad key misses -> no tooltip).
  var SEV_DESC = nullMap({
    high: 'Needs a human soon', med: 'Should be looked at', low: 'Informational',
  });
  var SRC_DESC = nullMap({
    escalation: 'An agent is asking you a question',
    gate: 'A release/quality step is blocked pending a decision',
    deadletter: 'A message failed repeatedly and was set aside — a human should look',
    stuck: 'An agent appears stuck and may need a human',
    supervisor: 'The automation itself needs attention',
  });
  var ROLE_ALIAS = nullMap({
    developer: 'dev', dev: 'dev',
    reviewer: 'rev', rev: 'rev',
    tester: 'test', test: 'test',
    documentation: 'docs', docs: 'docs',
    architect: 'arch', arch: 'arch',
    infrastructure: 'infra', infra: 'infra',
    lead: 'lead',
    scout: 'scout',
  });
  // Legacy fallback for old /api/state payloads. Current servers resolve
  // avatars backend-side and send agent.avatar.file / root.operator.avatar.file.
  var LEGACY_AVATAR_FALLBACK = nullMap({
    'claude:arch': 'claude-arch.png',
    'claude:dev': 'claude-dev.png',
    'claude:docs': 'claude-docs.png',
    'claude:lead': 'claude-lead.png',
    'claude:rev': 'claude-rev.png',
    'codex:dev': 'codex-dev.png',
    'codex:infra': 'codex-infra.png',
    'codex:rev': 'codex-rev.png',
    'codex:scout': 'codex-scout.png',
    'codex:test': 'codex-test.png',
  });
  var SHAPED_AVATAR_FAMILY = nullMap({
    'hexagon': true,
    'oval-muted': true,
    'oval-vivid': true,
    'rounded-square': true,
    'star': true,
    'triangle': true,
  });

  // ------------------------------------------------------------ client state
  function initialRootId() {
    if (typeof window === 'undefined' || !window.location ||
      typeof URLSearchParams === 'undefined') return '';
    try {
      return new URLSearchParams(window.location.search).get('root') || '';
    } catch (e) {
      return '';
    }
  }

  var state = {
    view: 'overview',
    selectedAgent: null,   // agent name
    sessionRid: null,      // thread request_id
    filter: 'all',         // all | working | idle | attention
    selectedRootId: initialRootId(),
    now: Date.now(),
  };
  var rootGeneration = 0;
  // Prefs (persisted). Loaded/validated below.
  var prefs = { theme: 'light', accent: 'blue', density: 'comfortable' };

  // Latest fetched payloads.
  var lastState = null;               // /api/state
  var attentionData = null;           // /api/attention (per open)
  var leadChatData = null;            // /api/lead-chat (operator<->lead bodies)
  var learningData = null;            // /api/learning (lessons + exposure telemetry)
  var onboardingData = null;          // /api/onboarding (project analysis runs)
  var leadChatPayloadHash = null;      // unchanged-payload guard for lead-chat
  var intentsData = null;             // /api/intents (body-free queue state)
  var attentionPending = null;
  var leadChatPending = null;
  var learningPending = null;
  var onboardingPending = null;
  var intentsPending = null;
  var sessionPending = null;
  var statePending = false;           // /api/state in-flight guard (P2-4)
  var stateSeq = 0;                   // request sequence id (issued)
  var stateCommitted = 0;             // newest committed sequence id (stale-drop)
  var threadCache = {};               // rootKey(label,rid) -> /api/thread payload (200 only)
  var threadNotFound = {};            // rootKey -> true (transient; cleared/re-validated each poll)
  var threadPending = {};             // rootKey -> bool (fetch in flight)
  var freshFeedIds = {};              // msg id -> true (animate-in one cycle)
  var seenFeedIds = {};               // msg id -> true (to detect fresh)
  var actionSession = { enabled: false, token: null, pending: false, error: '' };
  var queuedAnswers = {};             // to_request -> true (optimistic queued marker)
  var composerState = {
    mode: 'send',
    target: '',
    audienceKind: 'all',
    audienceValue: '',
    kind: 'message',
    subject: '',
    body: '',
  };
  var answerComposerState = {};       // to_request -> body text
  var leadChatComposerState = { body: '' };
  var archivedState = {
    root: '',
    open: false,
    loading: false,
    stale: false,
    error: '',
    count: null,
    nextCursor: null,
    items: [],
  };

  // ------------------------------------------------------------ tiny helpers
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }
  // Attach a plain-language hover tooltip (C0 legibility, v0.76.0). No-op for an
  // empty title so callers can pass a maybe-undefined desc unconditionally. The
  // precise agenttalk term always stays visible; the tooltip only ADDS meaning.
  function titled(n, title) {
    if (n && title) n.setAttribute('title', String(title));
    return n;
  }
  function svgEl(tag, attrs) {
    var n = document.createElementNS(SVG_NS, tag);
    if (attrs) {
      for (var k in attrs) {
        if (Object.prototype.hasOwnProperty.call(attrs, k)) {
          n.setAttribute(k, String(attrs[k]));
        }
      }
    }
    return n;
  }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function isArray(x) { return Object.prototype.toString.call(x) === '[object Array]'; }
  function on(node, ev, fn) { node.addEventListener(ev, fn); return node; }
  var LEAD_CHAT_VOLATILE_KEYS = nullMap({
    age_seconds: true,
    heartbeat_age_seconds: true,
  });
  function stableHash(value) {
    var s = String(value || '');
    var h = 2166136261;
    for (var i = 0; i < s.length; i++) {
      h ^= s.charCodeAt(i);
      h = Math.imul(h, 16777619);
    }
    return h >>> 0;
  }
  function leadChatPayloadIdentity(value) {
    if (isArray(value)) {
      var arr = [];
      for (var i = 0; i < value.length; i++) arr.push(leadChatPayloadIdentity(value[i]));
      return arr;
    }
    if (value && typeof value === 'object') {
      var out = {};
      var keys = Object.keys(value).sort();
      for (var j = 0; j < keys.length; j++) {
        var key = keys[j];
        if (LEAD_CHAT_VOLATILE_KEYS[key]) continue;
        out[key] = leadChatPayloadIdentity(value[key]);
      }
      return out;
    }
    return value;
  }
  function leadChatPayloadFingerprint(data) {
    return stableHash(JSON.stringify(leadChatPayloadIdentity(data)));
  }
  function option(value, text) {
    var o = el('option', null, text);
    o.value = value;
    return o;
  }
  function formField(label, control) {
    var wrap = el('label', 'tc-action-field');
    wrap.appendChild(el('span', 'tc-action-label', label));
    wrap.appendChild(control);
    return wrap;
  }
  function selectPersistedValue(control, value) {
    var found = false;
    for (var i = 0; i < control.options.length; i++) {
      if (control.options[i].value === value) {
        found = true;
        break;
      }
    }
    if (found) {
      control.value = value;
    } else if (control.options.length) {
      control.selectedIndex = 0;
    } else {
      control.value = '';
    }
    return control.value;
  }
  function isEditableControl(node) {
    if (!node || node === document.body) return false;
    var tag = String(node.tagName || '').toUpperCase();
    return tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA' || node.isContentEditable;
  }
  function closestActionComposer(node) {
    if (!node) return null;
    if (node.closest) return node.closest(
      '.tc-action-card, .tc-action-form, .tc-attn-answer, .tc-attn-answer-form, '
      + '.tc-lead-composer, .tc-lead-decision-form'
    );
    while (node) {
      if (node.classList && (
        node.classList.contains('tc-action-card')
        || node.classList.contains('tc-action-form')
        || node.classList.contains('tc-attn-answer')
        || node.classList.contains('tc-attn-answer-form')
        || node.classList.contains('tc-lead-composer')
        || node.classList.contains('tc-lead-decision-form')
      )) return node;
      node = node.parentNode;
    }
    return null;
  }
  function isEditingAction() {
    if (!actionSession.enabled) return false;
    var active = document.activeElement;
    return isEditableControl(active) && !!closestActionComposer(active);
  }
  function renderActiveViewFromPoll() {
    if (!isEditingAction()) renderActiveView();
  }

  // Relative-age formatter (matches the prototype _fmt: s / m / h with a
  // second component under an hour). Recomputed each clock tick from ts so
  // counters feel live between polls.
  function fmtAge(sec) {
    if (sec === undefined || sec === null || isNaN(sec)) return '';
    sec = Math.max(0, Math.floor(sec));
    if (sec < 60) return sec + 's';
    if (sec < 3600) {
      var m = Math.floor(sec / 60), s = sec % 60;
      return (m < 10 && s) ? (m + 'm ' + s + 's') : (m + 'm');
    }
    var h = Math.floor(sec / 3600), mm = Math.floor((sec % 3600) / 60);
    return mm ? (h + 'h ' + mm + 'm') : (h + 'h');
  }
  // Age of a wire item: prefer a live recompute from `ts`, else fall back to
  // the server-computed `age_seconds` (adjusted by drift since the poll).
  function liveAge(item) {
    if (item && item.ts) {
      var t = Date.parse(item.ts);
      if (!isNaN(t)) return (state.now - t) / 1000;
    }
    if (item && typeof item.age_seconds === 'number') {
      var base = lastState && lastState._fetchedAt ? lastState._fetchedAt : state.now;
      return item.age_seconds + (state.now - base) / 1000;
    }
    return null;
  }

  // A relative-age span that the 1 Hz clock ticker updates IN PLACE (B2a) —
  // NOT via a DOM rebuild, which would destroy inner-scroll and text selection
  // in the transcript/feed every second. The node is tagged with the raw inputs
  // (`data-age-ts` / `data-age-sec`) plus formatting opts so `updateAges` can
  // recompute textContent from the current `state.now` without re-rendering.
  //   opts.suffix   — appended after the formatted age (e.g. ' ago')
  //   opts.prefix   — prepended before it (e.g. 'since ')
  //   opts.nullText — shown when age is null / noHb (e.g. 'no hb', 'missing')
  //   opts.noHb     — force the nullText branch (unknown health, no heartbeat)
  function ageEl(cls, item, opts) {
    var n = el('span', cls);
    opts = opts || {};
    n.setAttribute('data-tc-age', '1');
    if (item && item.ts) n.setAttribute('data-age-ts', String(item.ts));
    if (item && typeof item.age_seconds === 'number') {
      n.setAttribute('data-age-sec', String(item.age_seconds));
    }
    if (opts.suffix) n.setAttribute('data-age-suffix', opts.suffix);
    if (opts.prefix) n.setAttribute('data-age-prefix', opts.prefix);
    if (opts.nullText) n.setAttribute('data-age-null', opts.nullText);
    if (opts.noHb) n.setAttribute('data-age-nohb', '1');
    n.textContent = ageText(n);
    // Absolute wall-clock time on hover (v0.76.0): relative ages ("5m", "2h ago")
    // need interpretation; the tooltip lets a viewer judge "has this been stuck
    // long enough to worry?" without mental math. Static (the event time doesn't
    // change); skipped for a no-heartbeat/unknown node.
    titled(n, absTimeTitle(item, opts));
    return n;
  }
  // The absolute local time an age refers to, for the hover tooltip. Prefers the
  // wire `ts`; else derives from `age_seconds` relative to the last poll (like
  // liveAge). Returns '' for a no-heartbeat node or an unparseable time.
  function absTimeTitle(item, opts) {
    opts = opts || {};
    var meaning = opts.title || '';   // optional plain-language prefix (e.g. heartbeat)
    if (opts.noHb) return meaning;    // no time to show, but still explain the field
    var ms = null;
    if (item && item.ts) {
      var t = Date.parse(item.ts);
      if (!isNaN(t)) ms = t;
    }
    if (ms === null && item && typeof item.age_seconds === 'number') {
      var base = lastState && lastState._fetchedAt ? lastState._fetchedAt : state.now;
      ms = base - item.age_seconds * 1000;
    }
    if (ms === null) return meaning;
    var abs;
    try { abs = new Date(ms).toLocaleString(); } catch (e) { abs = ''; }
    if (!abs) return meaning;
    return meaning ? (meaning + ' · ' + abs) : abs;
  }
  // Format the current text for a tagged age node from its data-* inputs + the
  // live `state.now`. Shared by `ageEl` (initial render) and `updateAges` (tick).
  function ageText(n) {
    var pfx = n.getAttribute('data-age-prefix') || '';
    var sfx = n.getAttribute('data-age-suffix') || '';
    var nullText = n.getAttribute('data-age-null');
    if (n.getAttribute('data-age-nohb') === '1') return nullText || '';
    var item = {};
    var ts = n.getAttribute('data-age-ts');
    if (ts) item.ts = ts;
    var sec = n.getAttribute('data-age-sec');
    if (sec !== null && sec !== '') item.age_seconds = Number(sec);
    var age = liveAge(item);
    if (age === null) return nullText || '';
    return pfx + fmtAge(age) + sfx;
  }
  // The 1 Hz in-place age refresh (B2a): recompute every tagged node from the
  // updated `state.now`. No renderActiveView, so inner scroll + selection live.
  function updateAges() {
    var nodes = document.querySelectorAll('[data-tc-age]');
    for (var i = 0; i < nodes.length; i++) nodes[i].textContent = ageText(nodes[i]);
  }

  // Snapshot/restore inner-scroller offsets across a 2s data re-render (B2b):
  // #main.scrollTop alone is not enough — the transcript scrolls inside a nested
  // .tc-transcript-body (max-height; overflow:auto) and the rail inside .tc-feed,
  // so those would jump to top every poll. Capture before clear(), restore after.
  var INNER_SCROLLERS = ['.tc-transcript-body', '.tc-feed'];
  function snapshotScroll(root) {
    var out = {};
    for (var i = 0; i < INNER_SCROLLERS.length; i++) {
      var sel = INNER_SCROLLERS[i];
      var n = root.querySelector(sel);
      if (n) out[sel] = n.scrollTop;
    }
    return out;
  }
  function restoreScroll(root, snap) {
    for (var sel in snap) {
      if (!Object.prototype.hasOwnProperty.call(snap, sel)) continue;
      var n = root.querySelector(sel);
      if (n) n.scrollTop = snap[sel];
    }
  }

  // ------------------------------------------------------------ vocab tables
  // health.state -> { label, key (raw state), cls (status-<state>), color
  // (semantic color token: ok/info/warn/attn/danger/violet/teal/gray), grp }.
  // The CSS owns semantic→color; the CLASS carries the RAW state (status-<state>),
  // and `color` feeds the card left-bar CSS custom properties (--status-color etc).
  // States without a dedicated CSS rule (errored_*) map to the nearest defined key.
  var UNWRAPPED_LIVE_STALE_AFTER_SECONDS = 120;
  function stateInfo(st) {
    switch (st) {
      case 'working_turn': return { label: 'Working', key: 'working_turn', color: 'ok', grp: 'work', pulse: true, desc: 'Actively taking a turn right now' };
      case 'working_silent': return { label: 'Working · quiet', key: 'working_silent', color: 'info', grp: 'work', desc: 'Running, but not sending messages right now (normal while it thinks)' };
      case 'idle_waiting': return { label: 'Idle · waiting', key: 'idle_waiting', color: 'warn', grp: 'idle', desc: 'Healthy — waiting for its next message (this is normal, not a problem)' };
      case 'stuck_suspected': return { label: 'Stuck?', key: 'stuck_suspected', color: 'attn', grp: 'attn', desc: 'No progress for a while — may need a human' };
      case 'rate_limited_or_outage': return { label: 'Rate-limited', key: 'rate_limited_or_outage', color: 'danger', grp: 'attn', desc: 'Paused because it hit an AI usage limit or an outage' };
      case 'degraded_output': return { label: 'Degraded', key: 'degraded_output', color: 'danger', grp: 'attn', desc: 'Producing low-quality output — may need a human' };
      case 'crashed_or_exited': return { label: 'Exited', key: 'crashed_or_exited', color: 'gray', grp: 'attn', desc: 'The process has stopped' };
      case 'errored_recoverable': return { label: 'Errored', key: 'errored_recoverable', color: 'attn', grp: 'attn', desc: 'Hit an error it may recover from' };
      case 'errored_poison': return { label: 'Errored', key: 'errored_recoverable', color: 'attn', grp: 'attn', desc: 'This message is failing in a way that retrying won’t fix — it will be set aside for a human' };
      case 'errored_fatal':
      case 'errored_ambiguous': return { label: 'Errored', key: 'errored_fatal', color: 'danger', grp: 'attn', desc: 'Hit an error and needs a human' };
      default: return { label: 'Unknown', key: 'unknown', color: 'gray', grp: 'unknown', noHb: true, desc: 'Not reporting in — may be offline' };
    }
  }
  function freshHeartbeat(agent) {
    var age = Number(agent && agent.last_seen_age_seconds);
    return Number.isFinite(age) && age >= 0 && age <= UNWRAPPED_LIVE_STALE_AFTER_SECONDS;
  }
  var OPERATOR_URGENCY_RANK = nullMap({ fine: 0, unknown: 1, attention: 2, danger: 3 });
  function observationUrgency(info) {
    if (info.color === 'danger') return 'danger';
    if (info.key === 'unknown') return 'unknown';
    if (info.grp === 'attn') return 'attention';
    return 'fine';
  }
  function legacyHealthComposition(agent, raw, info) {
    // API/asset skew fallback: old API payloads do not carry the shared Python
    // classification. Keep each independent strict fact visibly non-green,
    // but never invent child-loss semantics from an unclassified token.
    var observation = {
      source: 'wrapper_health', state: raw || 'unknown', urgency: observationUrgency(info),
    };
    var supervisor = null;
    var decision = agent && agent.supervisor_decision;
    if (decision && typeof decision === 'object') {
      supervisor = {
        source: 'supervisor_decision', kind: 'unclassified', state: decision.state,
        action: decision.action, reason: decision.reason, urgency: 'attention',
        classification_reason: 'composition_missing',
      };
    } else if (agent && agent.supervisor_decision_unavailable === true) {
      supervisor = {
        source: 'supervisor_decision', kind: 'unavailable',
        reason: agent.supervisor_decision_unavailable_reason || 'decision_missing',
        urgency: 'attention',
      };
    }
    if (!supervisor) return null;
    var supervisorWins = OPERATOR_URGENCY_RANK[supervisor.urgency] >
      OPERATOR_URGENCY_RANK[observation.urgency];
    return {
      observation: observation,
      supervisor: supervisor,
      urgency: supervisorWins ? supervisor.urgency : observation.urgency,
      primary_source: supervisorWins ? 'supervisor' : 'observation',
      disagreement: supervisor.urgency !== observation.urgency,
    };
  }
  function supervisorFactSummary(fact) {
    if (fact.kind === 'unclassified') {
      return 'Supervisor decision (urgency classification unavailable): ' +
        (fact.state || 'unknown') +
        (fact.action ? '/' + fact.action : '') +
        (fact.reason ? ' (' + fact.reason + ')' : '');
    }
    if (fact.kind === 'unavailable') {
      if (fact.reason === 'auto_restart_disabled') {
        return 'Supervisor verdict unavailable: auto-restart is disabled';
      }
      if (fact.reason === 'assessment_failed') {
        return 'Supervisor verdict unavailable: assessment failed';
      }
      if (fact.reason === 'decision_missing') {
        return 'Supervisor verdict unavailable: assessment returned no decision';
      }
      return 'Supervisor verdict unavailable: ' + (fact.reason || 'reason unknown');
    }
    return 'Supervisor verdict: ' + (fact.state || 'unknown') +
      (fact.action ? '/' + fact.action : '') +
      (fact.reason ? ' (' + fact.reason + ')' : '');
  }
  function supervisorPresentation(fact, observation, observationInfo) {
    var label = 'Supervisor: ' + (fact.state || 'unknown');
    var key = fact.urgency === 'danger' ? 'supervisor_alert' : 'supervisor_advisory';
    if (fact.kind === 'unavailable') {
      label = 'No verdict';
      key = 'supervisor_unavailable';
    } else if (fact.kind === 'unclassified') {
      label = 'Verdict unclassified';
    } else if (fact.kind === 'lost_binding') {
      label = 'Child binding lost';
      key = 'supervisor_lost_binding';
    } else if (fact.kind === 'live_stalled') {
      label = 'Child stalled';
    } else if (fact.kind === 'failed') {
      label = 'Turn failed';
    } else if (fact.kind === 'readiness_exhausted') {
      label = 'Readiness retries exhausted';
    }
    return {
      label: label,
      key: key,
      color: fact.urgency === 'danger' ? 'danger' : 'attn',
      grp: 'attn',
      desc: supervisorFactSummary(fact) + ' · Wrapper observation: ' +
        (observation.state || 'unknown') + ' (' + observationInfo.desc + ')',
      healthComposition: { observation: observation, supervisor: fact },
    };
  }
  function agentStateInfo(agent) {
    var health = (agent && agent.health) || {};
    var raw = health.state;
    var info = stateInfo(raw);
    var composition = agent && agent.health_composition;
    if (!composition || typeof composition !== 'object' ||
        !composition.observation || !composition.supervisor) {
      composition = legacyHealthComposition(agent, raw, info);
    }
    if (info.key === 'unknown' && freshHeartbeat(agent) && agent) {
      if (!composition && agent.wrapped !== true &&
          health.reason_code !== 'health_timing_policy_unavailable') {
        info = { label: 'Active', key: 'unwrapped_live', color: 'teal', grp: 'work', heartbeatOnly: true, desc: 'Alive and checking in, but not running under the supervisor' };
      } else {
        var healthReason = health.reason_code ? ' (' + health.reason_code + ')' : '';
        info = { label: 'Unknown', key: 'unknown', color: 'gray', grp: 'unknown', desc: 'Wrapper health is unknown' + healthReason + ' despite a recent heartbeat' };
      }
    }
    if (!composition) return info;
    var supervisor = composition.supervisor;
    if (composition.primary_source === 'supervisor') {
      return supervisorPresentation(supervisor, composition.observation, info);
    }
    info.desc += ' · ' + supervisorFactSummary(supervisor);
    info.healthComposition = composition;
    return info;
  }
  function stateInfoFrom(value) {
    return value && typeof value === 'object' && value.key ? value : stateInfo(value);
  }
  // status-<state> class carrying the raw health state (CSS owns the color).
  function statusClass(st) { return 'status-' + stateInfoFrom(st).key; }
  // Representative raw health-state for a health group (work/idle/attn/unknown),
  // used to color legend/stat/filter dots via the .status-<state> CSS rules.
  function groupState(grp) {
    return grp === 'work' ? 'working_turn'
      : grp === 'idle' ? 'idle_waiting'
      : grp === 'attn' ? 'stuck_suspected'
      : 'unknown';
  }

  // kind -> { label, cls (kind-<kind>) }. The CSS owns kind→color via the raw
  // kind key; kinds without a dedicated rule fall back to the neutral .kind-note.
  var KNOWN_KINDS = nullMap({
    'review-request': 1, 'review-result': 1, 'proposal': 1, 'proposal-response': 1,
    'question': 1, 'note': 1, 'message': 1, 'reply': 1, 'wake': 1, 'end': 1,
    'escalate': 1, 'broadcast': 1, 'gate': 1,
  });
  // Plain-English gloss for each message kind (C0 legibility, v0.76.0).
  var KIND_DESC = nullMap({
    'review-request': 'Asking a teammate to review some work',
    'review-result': 'A review verdict came back',
    'proposal': 'Proposing a plan for the team to agree on',
    'proposal-response': 'A reply to a proposal (accept / reject / counter)',
    'question': 'Asking a teammate a question',
    'note': 'An FYI note (no reply expected)',
    'message': 'A general message',
    'reply': 'A reply to an earlier message',
    'wake': 'A nudge to pick up queued work',
    'end': 'A request to stand down',
    'escalate': 'Raising something to a human',
    'broadcast': 'The same message sent to several teammates',
    'gate': 'A release/quality gate check',
  });
  function kindInfo(kind) {
    var k = kind || 'message';
    // Null-proto lookup (P3): an untrusted kind like "constructor" misses and
    // falls back to the neutral .kind-note rather than a garbage className.
    return { label: k, cls: 'kind-' + (KNOWN_KINDS[k] ? k : 'note'), desc: KIND_DESC[k] || '' };
  }

  // Thread verdict/status -> chip class. `verdict` (§3b) maps to the CSS
  // .tstatus-<verdict> family: approved/GO -> go, HOLD/rejected -> hold,
  // countered -> countered, broadcast "x/y replied" -> replied, else neutral.
  function verdictInfo(verdict) {
    if (!verdict) return null;
    var v = String(verdict);
    var lc = v.toLowerCase();
    if (lc === 'approved' || lc === 'go' || lc === 'accepted') return { label: v, cls: 'tstatus-go', desc: 'Cleared / approved' };
    if (lc === 'hold' || lc === 'rejected' || lc === 'blocked') return { label: v, cls: 'tstatus-hold', desc: 'Blocked — waiting on a decision or a fix' };
    if (lc === 'countered') return { label: v, cls: 'tstatus-countered', desc: 'Countered with a different proposal' };
    if (lc.indexOf('replied') >= 0) return { label: v, cls: 'tstatus-replied', desc: 'How many recipients have replied' };
    return { label: v, cls: 'tstatus-neutral' };
  }

  // The status chip for a thread row. Show a chip ONLY when the thread carries a
  // real verdict (§3b), or — for a broadcast with an audience — a synthesized
  // "x/y replied" label. A raw FSM state (e.g. "open-outbound") is NEVER a
  // verdict, so there is no fallback to t.state.
  function threadChip(t) {
    if (!t) return null;
    if (t.verdict) return verdictInfo(t.verdict);
    var audience = t.audience || [];
    if (t.is_broadcast && audience.length) {
      var responded = (t.responded || []).length;
      return { label: responded + '/' + audience.length + ' replied', cls: 'tstatus-replied', desc: 'How many recipients have replied so far' };
    }
    return null;
  }

  // Meter threshold color: <60 ok / 60-84 warn / >=85 danger (§7). Returns the
  // .tc-meter-fill state class; caller applies the 2% minimum width. The CSS has
  // no gray meter, so an unknown/None value renders as .is-ok (empty, min-width).
  function meterClass(pct) {
    if (pct === null || pct === undefined || isNaN(pct)) return 'is-ok';
    if (pct >= 85) return 'is-danger';
    if (pct >= 60) return 'is-warn';
    return 'is-ok';
  }
  function meterWidth(pct) {
    if (pct === null || pct === undefined || isNaN(pct)) return '0%';
    return Math.max(2, pct) + '%';
  }

  // CLI badge: claude | codex (else infer from an agent-name prefix).
  function cliInfo(cli, name) {
    var c = cli;
    if (!c && name) c = String(name).split('-')[0];
    if (c === 'claude') return { label: 'CLAUDE', cls: 'cli-claude' };
    if (c === 'codex') return { label: 'CODEX', cls: 'cli-codex' };
    return null;
  }

  // ------------------------------------------------------------ prefs
  function loadPrefs() {
    try {
      var t = localStorage.getItem(PREF_KEYS.theme);
      var a = localStorage.getItem(PREF_KEYS.accent);
      var d = localStorage.getItem(PREF_KEYS.density);
      if (THEMES.indexOf(t) >= 0) prefs.theme = t;
      if (ACCENTS.indexOf(a) >= 0) prefs.accent = a;
      if (DENSITIES.indexOf(d) >= 0) prefs.density = d;
    } catch (e) { /* localStorage unavailable — keep defaults */ }
  }
  function savePref(key, value) {
    try { localStorage.setItem(PREF_KEYS[key], value); } catch (e) { /* ignore */ }
  }
  // Apply prefs by setting data-* attributes on #app (CSP-safe: no inline
  // <style>, no style= attr; the CSS defines the var sets per attribute).
  function applyPrefs() {
    var app = document.getElementById('app');
    if (!app) return;
    app.setAttribute('data-theme', prefs.theme);
    app.setAttribute('data-accent', prefs.accent);
    app.setAttribute('data-density', prefs.density);
  }

  // ------------------------------------------------------------ root helpers
  function roots() { return (lastState && lastState.roots) || []; }
  function rootById(projectId) {
    var rs = roots();
    for (var i = 0; i < rs.length; i++) {
      if (rs[i].project_id === projectId) return rs[i];
    }
    return null;
  }
  function currentRoot() {
    var rs = roots();
    if (!rs.length) return null;
    return rootById(state.selectedRootId) || rs[0];
  }
  function agentsOf(root) { return (root && root.agents) || []; }
  function findAgent(root, name) {
    var as = agentsOf(root);
    for (var i = 0; i < as.length; i++) if (as[i].name === name) return as[i];
    return null;
  }
  // Selected root's label (drives the root-scoped /api/thread fetch, P2-5).
  function currentRootLabel() {
    var r = currentRoot();
    return (r && r.label) || '';
  }
  function currentRootId() {
    var r = currentRoot();
    return (r && r.project_id) || state.selectedRootId || '';
  }
  function rootUrl(path, projectId) {
    var rootId = projectId === undefined ? currentRootId() : projectId;
    if (!rootId) return path;
    return path + (path.indexOf('?') === -1 ? '?' : '&') +
      'root=' + encodeURIComponent(rootId);
  }
  function rootRequestKey(projectId, generation) {
    return projectId + '@' + generation;
  }
  function rootPayloadMatches(data, projectId, generation) {
    if (projectId !== currentRootId() || generation !== rootGeneration) return false;
    var info = data && data.root_info;
    var responseId = (info && info.project_id) ||
      (data && data.target_root_project_id) || '';
    return !!responseId && responseId === projectId;
  }
  function updateProjectUrl(mode) {
    if (typeof window === 'undefined' || !window.location || !window.history ||
      typeof URLSearchParams === 'undefined') return;
    try {
      var params = new URLSearchParams(window.location.search || '');
      var projectId = currentRootId();
      if (projectId) params.set('root', projectId);
      else params.delete('root');
      var query = params.toString();
      var next = (window.location.pathname || '/') + (query ? '?' + query : '') +
        (window.location.hash || '');
      var method = mode === 'push' ? 'pushState' : 'replaceState';
      if (typeof window.history[method] !== 'function') return;
      window.history[method]({ projectId: projectId }, '', next);
    } catch (e) { /* history may be unavailable in embedded browsers */ }
  }
  function updateDocumentTitle() {
    var root = currentRoot();
    var project = (root && root.label) || 'agenttalk';
    var view = VIEW_LABELS[state.view] || 'Console';
    document.title = project + ' - ' + view + ' - agenttalk';
  }
  function clearRootContext() {
    if (state.view === 'agent') state.view = 'overview';
    state.selectedAgent = null;
    state.sessionRid = null;
    attentionData = null;
    leadChatData = null;
    leadChatPayloadHash = null;
    learningData = null;
    onboardingData = null;
    intentsData = null;
    threadCache = {};
    threadNotFound = {};
    threadPending = {};
    queuedAnswers = {};
    seenFeedIds = {};
    freshFeedIds = {};
    archivedState.root = '';
    archivedState.open = false;
    archivedState.loading = false;
    archivedState.stale = false;
    archivedState.error = '';
    archivedState.count = null;
    archivedState.nextCursor = null;
    archivedState.items = [];
    actionSession.enabled = false;
    actionSession.token = null;
    actionSession.pending = false;
    actionSession.error = '';
    composerState.mode = 'send';
    composerState.target = '';
    composerState.audienceKind = 'all';
    composerState.audienceValue = '';
    composerState.kind = 'message';
    composerState.subject = '';
    composerState.body = '';
    answerComposerState = {};
    leadChatComposerState.body = '';
  }
  function applyProjectSelection(projectId, historyMode) {
    if (!projectId || !rootById(projectId) || projectId === state.selectedRootId) {
      return false;
    }
    state.selectedRootId = projectId;
    rootGeneration += 1;
    clearRootContext();
    if (historyMode !== null) updateProjectUrl(historyMode || 'replace');
    updateDocumentTitle();
    return true;
  }
  function reconcileProjectSelection() {
    var rs = roots();
    if (!rs.length) return false;
    if (rootById(state.selectedRootId)) {
      updateProjectUrl('replace');
      updateDocumentTitle();
      return false;
    }
    return applyProjectSelection(rs[0].project_id, 'replace');
  }
  function selectProject(projectId) {
    if (!applyProjectSelection(projectId, 'push')) return;
    renderChrome();
    renderActiveView();
    fetchRootPayloads();
  }
  function restoreProjectFromHistory() {
    var projectId = initialRootId();
    var rs = roots();
    if (!rs.length) {
      state.selectedRootId = projectId;
      return;
    }
    var historyMode = null;
    if (!rootById(projectId)) {
      projectId = rs[0].project_id;
      historyMode = 'replace';
    }
    if (!applyProjectSelection(projectId, historyMode)) {
      if (historyMode) updateProjectUrl(historyMode);
      updateDocumentTitle();
      return;
    }
    renderChrome();
    renderActiveView();
    fetchRootPayloads();
  }
  // Thread cache key: project id + rid (P2-5). Keying by rid ALONE would let a
  // same-request_id thread in another root leak / cross-bleed once cached.
  function threadKey(rid) { return currentRootId() + '|' + rid; }
  function rootClosedCount(root) {
    var c = root && root.counts;
    return c && typeof c.closed_threads === 'number' ? c.closed_threads : 0;
  }
  function syncArchivedRoot(root) {
    var label = (root && root.project_id) || '';
    var count = rootClosedCount(root);
    if (archivedState.root !== label) {
      archivedState.root = label;
      archivedState.open = false;
      archivedState.loading = false;
      archivedState.stale = false;
      archivedState.error = '';
      archivedState.count = count;
      archivedState.nextCursor = null;
      archivedState.items = [];
      return;
    }
    if (archivedState.open && archivedState.count !== null && archivedState.count !== count) {
      archivedState.stale = true;
    }
    archivedState.count = count;
  }

  // Agent bucket counts by health group (work/idle/attn/unknown).
  function agentCounts(root) {
    var c = { work: 0, idle: 0, attn: 0, unknown: 0 };
    var as = agentsOf(root);
    for (var i = 0; i < as.length; i++) {
      c[agentStateInfo(as[i]).grp]++;
    }
    return c;
  }
  function filterAgents(root) {
    var as = agentsOf(root);
    if (state.filter === 'all') return as.slice();
    var out = [];
    for (var i = 0; i < as.length; i++) {
      var grp = agentStateInfo(as[i]).grp;
      if (state.filter === 'working' && grp === 'work') out.push(as[i]);
      else if (state.filter === 'idle' && grp === 'idle') out.push(as[i]);
      else if (state.filter === 'attention' && grp === 'attn') out.push(as[i]);
      else if (state.filter === 'unknown' && grp === 'unknown') out.push(as[i]);
    }
    return out;
  }
  // Sort rank for the overview grid (v0.76.0): attention-needing first, then
  // offline/unknown, then working, then idle — so the top-left card is the one
  // that matters to a viewer.
  function agentAttnRank(a) {
    var grp = agentStateInfo(a).grp;
    return grp === 'attn' ? 0 : grp === 'unknown' ? 1 : grp === 'work' ? 2 : 3;
  }
  function humanQueueCount() {
    return attentionData && typeof attentionData.count === 'number'
      ? attentionData.count
      : (attentionData && attentionData.items ? attentionData.items.length : 0);
  }
  // ONE plain-language "is the team OK?" verdict for a zero-context viewer
  // (v0.76.0), derived from counts already on hand. Returns { text (full
  // sentence for the overview subtitle), pill (short label for the top-bar
  // pill), tone ('ok' | 'warn' | 'danger' | 'idle') }. Never color-only — every
  // caller renders the WORD; tone only adds a color. "needs a human" = the
  // human-action queue (escalations/gate holds/dead letters/stuck); "needs
  // attention" = agent health group; "offline" = not reporting in.
  // Is the human-attention queue KNOWN (loaded + fresh)? attentionData is null
  // until the first successful fetch and is retained (not blanked) on a failed
  // poll, so a bare count can't tell "confirmed empty" from "never loaded / stale
  // during an outage". A stamped _fetchedAt (set on each successful fetchAttention,
  // which polls every POLL_MS) ages out during an outage -> unknown. An unstamped
  // non-null payload (e.g. a test harness) is treated as known.
  // PURE (testable without module state): an attention payload counts as KNOWN — safe
  // to drive a confirmed verdict or a cleared "0" badge — only if it exists, carries NO
  // collection errors, and is within the freshness window. An error-as-data 200
  // (count=0, items=[], errors=[...]) is a COLLECTION FAILURE, not a confirmed-empty
  // queue, so it must read as unknown and never paint a green all-clear (codex P1).
  function attentionKnownFrom(data, now) {
    if (data == null) return false;
    if (Array.isArray(data.errors) && data.errors.length) return false;
    var at = data._fetchedAt;
    if (typeof at !== 'number' || !isFinite(at)) return true;
    return (now - at) <= ATTENTION_STALE_MS;
  }
  function attentionFresh() { return attentionKnownFrom(attentionData, state.now); }
  // Is the agent-health data (from /api/state) KNOWN-fresh? Same rationale as
  // attentionFresh: fetchState stamps _fetchedAt on each successful poll and retains
  // last-good on failure, so a stale lastState means agent health is obsolete and the
  // verdict must NOT assert green from it (v0.76.0 trust contract).
  function stateFresh() {
    if (lastState == null) return false;
    var at = lastState._fetchedAt;
    if (typeof at !== 'number' || !isFinite(at)) return true;
    return (state.now - at) <= STATE_STALE_MS;
  }
  function teamHealthVerdict(root) {
    var c = agentCounts(root);
    var attnKnown = attentionFresh();
    // A degraded root (server couldn't scan state) must not let a green pill sit
    // beside the "Degraded" main view (codex P1b). Same predicate renderActiveView uses.
    var degraded = !!(root && root.errors && root.errors.length);
    return teamHealthVerdictFrom(agentsOf(root).length, stateFresh(), attnKnown,
      attnKnown ? humanQueueCount() : null, c.attn, c.unknown, degraded);
  }
  // PURE verdict (testable). Green "nothing needs you" requires BOTH freshness buckets:
  // stateKnown (agent health) AND attnKnown (the human queue). Either being unknown
  // (loading / failed / stale) must never render as a confirmed all-clear — an outage
  // would otherwise leave a false green on screen (C0 trust contract).
  function teamHealthVerdictFrom(n, stateKnown, attnKnown, q, attnCount, unknownCount, rootDegraded) {
    // A KNOWN, non-empty human queue is CONFIRMED urgent work from a SEPARATE feed
    // (/api/attention). It must NEVER be masked by a stale agent-state feed or a
    // degraded root — surface it as danger FIRST and append a status caveat so BOTH
    // facts show (codex P2). attnKnown is false for an errored/stale/loading attention
    // payload, so this fires only on a trustworthy non-empty queue.
    if (attnKnown && q > 0) {
      var humanPill = (q === 1 ? '1 needs' : q + ' need') + ' a human';
      var hparts = [n + ' agent' + (n === 1 ? '' : 's'), humanPill];
      if (stateKnown && !rootDegraded) {
        if (attnCount > 0) hparts.push(attnCount + (attnCount === 1 ? ' needs' : ' need') + ' attention');
        if (unknownCount > 0) hparts.push(unknownCount + ' offline');
      } else {
        hparts.push(!stateKnown ? 'agent status stale' : 'status unavailable');
      }
      return { text: hparts.join(' · '), pill: humanPill, tone: 'danger' };
    }
    // Freshness gates the rest: with no fresh agent-health data we can assert nothing —
    // not "healthy", and not even "no agents" (a cold start or a stale zero-agent payload
    // must never read as a CONFIRMED empty team, codex P1a). Show reconnecting (had data,
    // now stale) / connecting (never loaded yet).
    if (!stateKnown) {
      return n
        ? { text: n + ' agents · status stale (reconnecting…)', pill: 'Reconnecting…', tone: 'idle' }
        : { text: 'Connecting… (status not loaded yet)', pill: 'Connecting…', tone: 'idle' };
    }
    // A degraded root (the server's state scan errored) must never paint a green
    // all-clear next to the "Degraded" main view (codex P1b) — neutral/warn instead.
    if (rootDegraded) {
      return { text: 'The dashboard can’t read the team’s status right now', pill: 'Status unavailable', tone: 'warn' };
    }
    if (!n) return { text: 'No agents running yet', pill: 'No agents', tone: 'idle' };
    if (attnKnown && q === 0 && attnCount === 0 && unknownCount === 0) {
      return n === 1
        ? { text: 'Your agent is healthy — nothing needs you', pill: 'Healthy', tone: 'ok' }
        : { text: 'All ' + n + ' agents healthy — nothing needs you', pill: 'Healthy', tone: 'ok' };
    }
    // Known but not all-clear, and not an urgent human queue (handled above): itemize
    // what we DO know (agent-health attention / offline / unknown queue).
    var parts = [n + ' agent' + (n === 1 ? '' : 's')];
    if (attnCount > 0) parts.push(attnCount + (attnCount === 1 ? ' needs' : ' need') + ' attention');
    if (unknownCount > 0) parts.push(unknownCount + ' offline');
    if (!attnKnown) parts.push('queue status unknown');
    var pill = (attnCount > 0) ? (attnCount + (attnCount === 1 ? ' needs' : ' need') + ' attention')
      : (!attnKnown ? 'Checking…' : 'Some offline');
    var tone = (attnCount > 0) ? 'warn' : (!attnKnown ? 'idle' : 'warn');
    return { text: parts.join(' · '), pill: pill, tone: tone };
  }
  // The verdict currently painted (set by renderTopbar). The 1 Hz clockTick compares
  // the freshly-computed verdict to this and refreshes the health summary in place when
  // it changes — so a green pill/subtitle FLIPS to "reconnecting…" once a poll outage
  // ages the data past the freshness window, even though no data poll rendered (v0.76.0).
  var lastHealthSig = null;
  function healthSig(v) { return (v.tone || '') + '|' + (v.pill || '') + '|' + (v.text || ''); }
  function subtitleTextFor(verdict, root) {
    var missionN = (root.spec_kitty && isArray(root.spec_kitty.missions)) ? root.spec_kitty.missions.length : 0;
    return verdict.text + (missionN ? ' · ' + missionN + ' mission' + (missionN === 1 ? '' : 's') + ' active' : '');
  }
  function refreshHealthIfChanged() {
    var root = currentRoot();
    var v = root ? teamHealthVerdict(root) : { tone: '', pill: '', text: '' };
    if (healthSig(v) === lastHealthSig) return;
    renderChrome();   // re-renders topbar pill + sidebar badge (no scrollable content); resets lastHealthSig
    if (state.view === 'overview' && root) {
      var sub = document.querySelector('#main .tc-subtitle');
      if (sub) { sub.className = 'tc-subtitle is-' + v.tone; sub.textContent = subtitleTextFor(v, root); }
    }
  }

  // ------------------------------------------------------------ shared bits
  function statusDot(st, extraCls) {
    var info = stateInfoFrom(st);
    var cls = 'tc-dot ' + statusClass(info) + (info.pulse ? ' is-pulsing' : '');
    if (extraCls) cls += ' ' + extraCls;
    return el('span', cls);
  }
  function statusChip(st) {
    var info = stateInfoFrom(st);
    return titled(el('span', 'tc-chip ' + statusClass(info), info.label), info.desc);
  }
  function kindChip(kind) {
    var info = kindInfo(kind);
    return titled(el('span', 'tc-chip ' + info.cls, info.label), info.desc);
  }
  function cliBadge(cli, name) {
    var info = cliInfo(cli, name);
    if (!info) return null;
    return titled(el('span', 'tc-chip ' + info.cls, info.label),
      'Which AI powers this agent (Claude or Codex)');
  }
  // Prettify a lowercase model/CLI alias for display: 'sonnet' -> 'Sonnet'.
  // Robust to ANY string (null/empty -> ''; already-cased or multi-word values
  // are left readable — we only touch the first character).
  function prettyAlias(s) {
    var str = String(s === undefined || s === null ? '' : s).trim();
    if (!str) return '';
    return str.charAt(0).toUpperCase() + str.slice(1);
  }
  function normalizedRole(role) {
    if (!role) return '';
    var key = String(role).toLowerCase().trim();
    return ROLE_ALIAS[key] || '';
  }
  function cliFamily(agent) {
    if (agent && (agent.cli === 'claude' || agent.cli === 'codex')) return agent.cli;
    var name = agent && agent.name ? String(agent.name) : '';
    var prefix = name.split('-', 1)[0];
    return (prefix === 'claude' || prefix === 'codex') ? prefix : '';
  }
  function avatarFile(agent) {
    var backendFile = safeAvatarFile(agent && agent.avatar && agent.avatar.file);
    if (backendFile) return backendFile;
    var role = normalizedRole(agent && agent.role);
    var family = cliFamily(agent);
    if (!role || !family) return '';
    return LEGACY_AVATAR_FALLBACK[family + ':' + role] || '';
  }
  function safeAvatarFile(file) {
    if (typeof file !== 'string' || !file) return '';
    if (file.indexOf('/') !== -1 || file.indexOf('\\') !== -1) return '';
    if (file.indexOf('..') !== -1 || file.indexOf(':') !== -1) return '';
    return file;
  }
  function avatarShape(agent) {
    var shape = agent && agent.avatar && agent.avatar.shape;
    if (typeof shape !== 'string' || !shape) return '';
    return SHAPED_AVATAR_FAMILY[shape] ? shape : '';
  }
  function operatorFallbackAvatar(avatarCls) {
    var cls = avatarCls || 'tc-operator-avatar';
    if (cls.indexOf('tc-operator-avatar') === -1) cls = 'tc-operator-avatar ' + cls;
    return el('span', cls, 'yo');
  }
  function agentAvatar(agent, avatarCls, fallbackDotCls, opts) {
    opts = opts || {};
    var file = avatarFile(agent);
    if (!file) return null;
    var info = agentStateInfo(agent);
    var cls = 'tc-avatar ' + (avatarCls || '');
    if (avatarShape(agent)) cls += ' tc-avatar-shaped';
    var wrap = el('span', cls);
    var img = document.createElement('img');
    img.src = '/static/avatars/' + file;
    img.alt = '';
    img.loading = 'lazy';
    img.decoding = 'async';
    on(img, 'error', function () {
      var repl = opts.operator ? operatorFallbackAvatar(avatarCls) : statusDot(info, fallbackDotCls);
      if (wrap.parentNode) wrap.parentNode.replaceChild(repl, wrap);
    });
    wrap.appendChild(img);
    if (!opts.hideStatus) wrap.appendChild(statusDot(info, 'tc-avatar-badge'));
    return wrap;
  }
  function avatarOrDot(agent, avatarCls, dotCls) {
    return agentAvatar(agent, avatarCls, dotCls) || statusDot(agentStateInfo(agent), dotCls);
  }
  // A rate/ctx mini-meter (label + value + track/fill). .tc-meter-head lays out
  // its two spans as a space-between row; the fill state comes from meterClass.
  function miniMeter(label, pct, title) {
    var wrap = titled(el('div', 'tc-meter'), title);
    var head = el('div', 'tc-meter-head');
    head.appendChild(el('span', null, label));
    head.appendChild(el('span', null, pct === null || pct === undefined ? '—' : (Math.round(pct) + '%')));
    wrap.appendChild(head);
    var track = el('div', 'tc-meter-track');
    var fill = el('div', 'tc-meter-fill ' + meterClass(pct));
    fill.style.width = meterWidth(pct);
    track.appendChild(fill);
    wrap.appendChild(track);
    return wrap;
  }

  function capNum(obj, key) {
    if (!obj || typeof obj !== 'object') return null;
    var v = obj[key];
    return (typeof v === 'number') ? v : null;
  }

  // Read legacy flat percents, preferring the richer additive objects when present.
  function capPct(agent, key) {
    var cap = agent.capacity;
    if (!cap) return null;
    if (key === 'rate_used_pct') {
      var rate = capNum(cap.primary, 'used_pct');
      if (rate !== null) return rate;
    }
    if (key === 'context_used_pct') {
      var ctx = capNum(cap.context, 'used_pct');
      if (ctx !== null) return ctx;
    }
    var v = cap[key];
    return (typeof v === 'number') ? v : null;
  }

  function capProvider(cap, agent) {
    var src = cap && typeof cap.source === 'string' ? cap.source : '';
    if (src.indexOf('claude') === 0) return 'claude';
    if (src.indexOf('codex') === 0) return 'codex';
    return (agent && (agent.cli === 'claude' || agent.cli === 'codex')) ? agent.cli : 'unknown';
  }

  function capProviderBadge(cap, agent) {
    var provider = capProvider(cap, agent);
    var cls = provider === 'claude' ? 'cli-claude' : (provider === 'codex' ? 'cli-codex' : '');
    return el('span', 'tc-chip ' + cls, provider.toUpperCase());
  }

  function capConfidenceChip(conf) {
    var c = (conf === 'fresh' || conf === 'stale' || conf === 'unknown') ? conf : 'unknown';
    return el('span', 'tc-chip tc-cap-confidence is-' + c, c);
  }

  function capWindow(cap, key, legacyPct) {
    if (cap && cap[key] && typeof cap[key] === 'object') return cap[key];
    if (legacyPct !== null && legacyPct !== undefined) {
      return { label: key === 'primary' ? '5h' : 'weekly', used_pct: legacyPct };
    }
    return null;
  }

  function resetText(win) {
    if (!win || typeof win !== 'object') return '—';
    var secs = capNum(win, 'reset_in_seconds');
    if (secs !== null) {
      if (secs <= 0) return 'resets now';
      var mins = Math.ceil(secs / 60);
      if (mins < 60) return 'resets in ' + mins + 'm';
      var h = Math.floor(mins / 60);
      var m = mins % 60;
      return 'resets in ' + h + 'h' + (m ? ' ' + m + 'm' : '');
    }
    var at = capNum(win, 'resets_at');
    if (at !== null) {
      return 'resets at ' + new Date(at * 1000).toLocaleTimeString('en-US', {
        hour: '2-digit', minute: '2-digit'
      });
    }
    return '—';
  }

  function contextNote(ctx, fallbackPct) {
    var pct = capNum(ctx, 'used_pct');
    if (pct === null) pct = fallbackPct;
    var parts = [];
    var tokens = capNum(ctx, 'tokens');
    var size = capNum(ctx, 'window_size');
    if (tokens !== null && size !== null) parts.push(Math.round(tokens) + ' / ' + Math.round(size) + ' tokens');
    else if (tokens !== null) parts.push(Math.round(tokens) + ' tokens');
    if (pct !== null && pct >= 85) parts.push('compaction risk');
    if (!parts.length) return pct !== null && pct >= 85 ? 'Compaction risk - avoid heavy context' : 'Context budget';
    return parts.join(' · ');
  }

  // ------------------------------------------------------------ navigation
  function go(view, opts) {
    state.view = view;
    if (opts && 'selectedAgent' in opts) state.selectedAgent = opts.selectedAgent;
    if (opts && 'sessionRid' in opts) state.sessionRid = opts.sessionRid;
    // Attention/thread data is view-scoped; refetch on entry.
    if (view === 'attention') fetchAttention();
    if (view === 'lead-chat') fetchLeadChat();
    if (view === 'learning') fetchLearning();
    if (view === 'onboarding') fetchOnboarding();
    if (view === 'sessions' && state.sessionRid) fetchThread(state.sessionRid);
    renderActiveView();
    renderChrome();
  }
  function openAgent(name) { go('agent', { selectedAgent: name }); }
  function openThread(rid) { go('sessions', { sessionRid: rid }); }

  // ------------------------------------------------------------ chrome (top bar + sidebar)
  function renderChrome() {
    updateDocumentTitle();
    renderTopbar();
    renderSidebar();
  }

  function renderTopbar() {
    var bar = document.getElementById('topbar');
    if (!bar) return;
    clear(bar);

    // Wordmark: gradient square + "agenttalk" / "TEAM CONSOLE".
    var brand = el('div', 'tc-brand');
    brand.appendChild(el('span', 'tc-brand-mark'));
    var brandText = el('div', 'tc-brand-text');
    brandText.appendChild(el('span', 'tc-wordmark', 'agenttalk'));
    brandText.appendChild(el('span', 'tc-wordmark-sub', 'Team Console'));
    brand.appendChild(brandText);
    bar.appendChild(brand);

    var root = currentRoot();

    var project = el('div', 'tc-project-context');
    project.appendChild(el('span', 'tc-project-caption', 'Project'));
    var rs = roots();
    if (rs.length > 1) {
      var select = el('select', 'tc-project-select');
      select.setAttribute('aria-label', 'Current Agenttalk project');
      for (var i = 0; i < rs.length; i++) {
        var option = el('option', '', rs[i].label || ('root ' + (i + 1)));
        option.value = rs[i].project_id || '';
        select.appendChild(option);
      }
      select.value = currentRootId();
      on(select, 'change', function () { selectProject(select.value); });
      project.appendChild(select);
    } else {
      project.appendChild(el(
        'span', 'tc-project-name', (root && root.label) || 'Loading project'
      ));
    }
    var projectPath = el(
      'span', 'tc-project-path tc-mono', (root && root.path) || 'Resolving root'
    );
    if (root && root.path) {
      projectPath.setAttribute('title', root.path);
      projectPath.setAttribute('aria-label', 'Project root ' + root.path);
    }
    project.appendChild(projectPath);
    bar.appendChild(project);

    // Mission pill (from root.spec_kitty.missions — name only, no x/y).
    if (root && root.spec_kitty && isArray(root.spec_kitty.missions) && root.spec_kitty.missions.length) {
      var pill = el('div', 'tc-mission-pill');
      pill.appendChild(missionIcon());
      pill.appendChild(el('span', 'tc-mission-name', root.spec_kitty.missions.join(' · ')));
      bar.appendChild(pill);
    }

    bar.appendChild(el('div', 'tc-spacer'));

    // Live indicator + clock (recomputed each tick).
    var live = el('div', 'tc-live');
    live.appendChild(el('span', 'tc-live-dot'));
    titled(live, 'The page is polling the bus live');
    live.appendChild(el('span', 'tc-live-label', 'Live'));
    live.appendChild(el('span', 'tc-clock', new Date(state.now).toLocaleTimeString('en-US', { hour12: false })));
    bar.appendChild(live);

    // Overall team-health pill (v0.76.0): the green "Live" dot only means the page
    // is polling — this shows the TRUE team status (word + color, not color alone),
    // glanceable from every view. Full sentence on hover.
    var verdict = teamHealthVerdict(root);
    lastHealthSig = healthSig(verdict);   // record what's painted, for the clockTick freshness refresh
    var pill = el('div', 'tc-health-pill is-' + verdict.tone);
    pill.appendChild(el('span', 'tc-health-dot'));
    pill.appendChild(el('span', 'tc-health-label', verdict.pill));
    titled(pill, verdict.text);
    bar.appendChild(pill);

    bar.appendChild(el('div', 'tc-divider'));

    // Prefs controls (theme / accent / density) — read-only nav, not a write.
    bar.appendChild(prefsControls());

    // Operator chip.
    var op = el('div', 'tc-operator');
    var operator = (root && root.operator) || {};
    var operatorAvatar = agentAvatar({
      principal: operator.principal || 'operator',
      name: operator.principal || 'operator',
      avatar: operator.avatar,
    }, 'tc-operator-avatar', null, { operator: true, hideStatus: true });
    op.appendChild(operatorAvatar || operatorFallbackAvatar('tc-operator-avatar'));
    var opText = el('div', 'tc-operator-text');
    opText.appendChild(el('span', 'tc-operator-name', operator.label || 'you'));
    opText.appendChild(el('span', 'tc-operator-role', operator.role_label || 'operator'));
    op.appendChild(opText);
    bar.appendChild(op);
  }

  function prefsControls() {
    var wrap = el('div', 'tc-prefs');
    // Theme toggle.
    var themeBtn = el('button', 'tc-pref-btn', prefs.theme === 'dark' ? 'Dark' : 'Light');
    themeBtn.setAttribute('title', 'Toggle theme');
    on(themeBtn, 'click', function () {
      prefs.theme = prefs.theme === 'dark' ? 'light' : 'dark';
      savePref('theme', prefs.theme);
      applyPrefs();
      themeBtn.textContent = prefs.theme === 'dark' ? 'Dark' : 'Light';
    });
    wrap.appendChild(themeBtn);
    // Density toggle.
    var densBtn = el('button', 'tc-pref-btn', prefs.density === 'compact' ? 'Compact' : 'Comfy');
    densBtn.setAttribute('title', 'Toggle density');
    on(densBtn, 'click', function () {
      prefs.density = prefs.density === 'compact' ? 'comfortable' : 'compact';
      savePref('density', prefs.density);
      applyPrefs();
      densBtn.textContent = prefs.density === 'compact' ? 'Compact' : 'Comfy';
    });
    wrap.appendChild(densBtn);
    // Accent swatches.
    var acc = el('div', 'tc-accents');
    for (var i = 0; i < ACCENTS.length; i++) {
      (function (name) {
        var sw = el('button', 'tc-accent-sw accent-' + name + (prefs.accent === name ? ' is-active' : ''));
        sw.setAttribute('title', 'Accent: ' + name);
        on(sw, 'click', function () {
          prefs.accent = name;
          savePref('accent', name);
          applyPrefs();
          renderTopbar();
        });
        acc.appendChild(sw);
      })(ACCENTS[i]);
    }
    wrap.appendChild(acc);
    return wrap;
  }

  function renderSidebar() {
    var side = document.getElementById('sidebar');
    if (!side) return;
    clear(side);

    side.appendChild(el('div', 'tc-nav-label', 'Views'));

    var root = currentRoot();
    var attnCount = humanQueueCount();
    var leadPendingCount = leadChatData && isArray(leadChatData.pending_decisions)
      ? leadChatData.pending_decisions.length
      : 0;
    var learningCount = learningData && learningData.counts
      ? (learningData.counts.active || 0)
      : 0;
    var onboardingCount = onboardingData && onboardingData.counts
      ? (onboardingData.counts.active || 0)
      : 0;

    var nav = el('nav', 'tc-nav');
    // "overview" nav stays active while an agent-detail is open.
    var items = [
      { key: 'overview', label: 'Team overview', icon: navIconGrid, activeWith: 'agent' },
      { key: 'flow', label: 'Conversations', icon: navIconChat },
      // clearWhenZero (v0.76.0): show a subtle green "0" so "nothing waiting on you"
      // is visible in the always-on sidebar, not only after opening the queue view.
      { key: 'attention', label: 'Human queue', icon: navIconAlert, badge: attnCount,
        clearWhenZero: true, title: 'Things that need a human decision (escalations, blocked gates, stuck agents, failed messages)' },
      { key: 'lead-chat', label: 'Lead chat', icon: navIconChat, badge: leadPendingCount },
      { key: 'learning', label: 'Learning', icon: navIconFile, badge: learningCount },
      { key: 'onboarding', label: 'Onboarding', icon: navIconFile, badge: onboardingCount },
      { key: 'sessions', label: 'Sessions', icon: navIconFile },
    ];
    for (var i = 0; i < items.length; i++) {
      (function (it) {
        var active = state.view === it.key || (it.activeWith && state.view === it.activeWith);
        var row = titled(el('button', 'tc-nav-item' + (active ? ' is-active' : '')), it.title);
        row.appendChild(it.icon());
        row.appendChild(el('span', 'tc-nav-item-label', it.label));
        if (it.badge) row.appendChild(el('span', 'tc-nav-badge', it.badge));
        // green "0" ONLY when the queue is confirmed fresh-empty — never on unknown/
        // stale attention, which must not read as a false all-clear (v0.76.0).
        else if (it.clearWhenZero && attentionFresh()) row.appendChild(el('span', 'tc-nav-badge is-clear', '0'));
        on(row, 'click', function () { go(it.key, { selectedAgent: null }); });
        nav.appendChild(row);
      })(items[i]);
    }
    side.appendChild(nav);

    side.appendChild(el('div', 'tc-spacer'));

    // Status legend (live counts from the current root). The dot color comes
    // from the .status-<state>.tc-legend-dot CSS rules (raw representative state).
    var legend = el('div', 'tc-legend');
    legend.appendChild(el('div', 'tc-legend-label', 'Status legend'));
    var c = agentCounts(root);
    // Plain-language meaning per row (v0.76.0) — the legend doubles as a glossary,
    // and kills the biggest false alarm: amber "Idle · waiting" is NORMAL, not broken.
    var rows = [
      { label: 'Working', grp: 'work', count: c.work, desc: 'Actively taking a turn' },
      { label: 'Idle · waiting', grp: 'idle', count: c.idle, desc: 'Healthy — waiting for a message (normal, not a problem)' },
      { label: 'Health attention', grp: 'attn', count: c.attn, desc: 'May need a human — stuck, rate-limited, errored, or exited' },
      { label: 'Unknown / offline', grp: 'unknown', count: c.unknown, desc: 'Not reporting in — may be offline' },
    ];
    var legendRows = el('div', 'tc-legend-rows');
    for (var j = 0; j < rows.length; j++) {
      var lr = titled(el('div', 'tc-legend-row'), rows[j].desc);
      lr.appendChild(el('span', 'tc-legend-dot status-' + groupState(rows[j].grp)));
      lr.appendChild(el('span', 'tc-legend-text', rows[j].label));
      lr.appendChild(el('span', 'tc-legend-count', rows[j].count));
      legendRows.appendChild(lr);
    }
    legend.appendChild(legendRows);
    side.appendChild(legend);
  }

  // ------------------------------------------------------------ view router
  function renderActiveView() {
    var main = document.getElementById('main');
    if (!main) return;
    // Preserve scroll across the 2s data re-render (B2b): #main AND every inner
    // scroller (transcript body / activity feed), else they jump to top on poll.
    var scrollTop = main.scrollTop;
    var innerScroll = snapshotScroll(main);
    clear(main);

    var root = currentRoot();
    if (!root) {
      main.appendChild(emptyState('Loading…', 'Fetching team state from the bus.'));
      return;
    }
    // Degraded root: show its error line, do not crash (§7 / invariant 7).
    if (root.errors && root.errors.length) {
      main.appendChild(viewHead(root.label || 'root', null));
      main.appendChild(el('div', 'tc-root-error', 'Degraded: ' + root.errors.join('; ')));
      return;
    }

    switch (state.view) {
      case 'overview': renderOverview(main, root); break;
      case 'flow': renderFlow(main, root); break;
      case 'attention': renderAttention(main, root); break;
      case 'lead-chat': renderLeadChat(main, root); break;
      case 'learning': renderLearning(main, root); break;
      case 'onboarding': renderOnboarding(main, root); break;
      case 'sessions': renderSessions(main, root); break;
      case 'agent': renderAgentDetail(main, root); break;
      default: renderOverview(main, root);
    }
    main.scrollTop = scrollTop;
    restoreScroll(main, innerScroll);
  }

  // ------------------------------------------------------------ VIEW 1: overview
  function renderOverview(main, root) {
    var counts = agentCounts(root);
    var all = agentsOf(root);

    // Header row: title + subtitle + filter chips.
    var header = el('div', 'tc-view-head');
    var titleBox = el('div');
    titleBox.appendChild(el('h1', 'tc-h1', "Who's doing what"));
    // One plain-language, color-coded health verdict — the 5-second "is the team
    // OK?" answer (v0.76.0), replacing the four jargon count-clauses (the raw
    // numbers still live in the stat tiles + sidebar legend below).
    var verdict = teamHealthVerdict(root);
    titleBox.appendChild(el('p', 'tc-subtitle is-' + verdict.tone, subtitleTextFor(verdict, root)));
    header.appendChild(titleBox);
    header.appendChild(el('div', 'tc-spacer'));
    header.appendChild(filterChips(root, counts));
    main.appendChild(header);

    // Stat tiles (4).
    var tiles = el('div', 'tc-stats');
    var tileDefs = [
      { label: 'Working', grp: 'work', value: counts.work },
      { label: 'Idle', grp: 'idle', value: counts.idle },
      { label: 'Health attention', grp: 'attn', value: counts.attn },
      { label: 'Unknown', grp: 'unknown', value: counts.unknown },
    ];
    for (var i = 0; i < tileDefs.length; i++) {
      var tile = el('div', 'tc-card tc-stat');
      var th = el('div', 'tc-stat-head');
      th.appendChild(el('span', 'tc-stat-dot status-' + groupState(tileDefs[i].grp)));
      th.appendChild(el('span', 'tc-stat-label', tileDefs[i].label));
      tile.appendChild(th);
      tile.appendChild(el('div', 'tc-stat-value', tileDefs[i].value));
      tiles.appendChild(tile);
    }
    main.appendChild(tiles);

    // Two-column body: agent grid + live-activity rail.
    var body = el('div', 'tc-overview-body');
    var grid = el('div', 'tc-agent-grid');
    // Attention-first (v0.76.0): float agents that need a human/attention (and
    // then offline) to the top-left where the eye lands, so the first card a
    // viewer sees is the one that matters. Stable within a rank (ES2019 sort).
    var shown = filterAgents(root).slice().sort(function (x, y) {
      return agentAttnRank(x) - agentAttnRank(y);
    });
    if (!shown.length) {
      // Distinguish "team hasn't started" from "filter hid everything" (v0.76.0):
      // the old copy always implied a broken filter even with zero agents.
      if (!all.length) {
        if (!stateFresh()) {
          // Stale / never-loaded state: we can't CONFIRM the team is empty, so the
          // agent grid must mirror the topbar's "Connecting…" verdict rather than
          // assert a false "No agents running yet" (codex P1 r5 — the grid empty state
          // is a SECOND render path the top-bar verdict fix didn't cover).
          grid.appendChild(emptyState('Connecting…',
            'Waiting for current team status — the last update is stale.'));
        } else {
          grid.appendChild(emptyState('No agents running yet',
            'Agents will appear here as soon as the team starts.'));
        }
      } else {
        grid.appendChild(emptyState('No agents match this filter',
          'Clear the filter to see all ' + all.length + ' agents.'));
      }
    } else {
      for (var g = 0; g < shown.length; g++) grid.appendChild(agentCard(root, shown[g]));
    }
    body.appendChild(grid);
    body.appendChild(activityRail(root));
    main.appendChild(body);
  }

  function filterChips(root, counts) {
    var wrap = el('div', 'tc-filters');
    var defs = [
      { key: 'all', label: 'All', count: agentsOf(root).length },
      { key: 'working', label: 'Working', count: counts.work },
      { key: 'idle', label: 'Idle', count: counts.idle },
      { key: 'attention', label: 'Health attention', count: counts.attn },
      { key: 'unknown', label: 'Unknown', count: counts.unknown },
    ];
    for (var i = 0; i < defs.length; i++) {
      (function (d) {
        var chip = el('button', 'tc-filter' + (state.filter === d.key ? ' is-active' : ''));
        chip.appendChild(el('span', null, d.label));
        chip.appendChild(el('span', 'tc-filter-count', d.count));
        on(chip, 'click', function () { state.filter = d.key; renderActiveView(); });
        wrap.appendChild(chip);
      })(defs[i]);
    }
    return wrap;
  }

  // Agent Card (clickable -> Agent Detail).
  function agentCard(root, a) {
    var info = agentStateInfo(a);
    var card = el('div', 'tc-agent-card');
    // Left status bar: CSS reads --status-color for the inset shadow.
    card.style.setProperty('--status-color', 'var(--' + info.color + ')');

    // Row 1: dot + name + CLI badge + spacer + wrapped icon.
    var r1 = el('div', 'tc-agent-row');
    r1.appendChild(avatarOrDot(a, 'tc-agent-avatar', null));
    r1.appendChild(el('span', 'tc-agent-name', a.name));
    var badge = cliBadge(a.cli, a.name);
    if (badge) r1.appendChild(badge);
    r1.appendChild(el('span', 'tc-spacer'));
    if (a.wrapped) r1.appendChild(wrappedIcon());
    card.appendChild(r1);

    // Row 2: role · group.
    var roleParts = [];
    if (a.role) roleParts.push(a.role);
    if (a.groups && a.groups.length) roleParts.push(a.groups.join(', '));
    else if (a.group) roleParts.push(a.group);
    card.appendChild(el('div', 'tc-agent-role', roleParts.join(' · ')));

    // Runtime identity (v0.75.1): "<CLI> · <Model>" + a small effort chip.
    // Omitted entirely when the model is unknown (absent-not-null), matching
    // the card's existing conventions.
    var runtime = agentCardRuntime(a);
    if (runtime) card.appendChild(runtime);

    // Row 3: current task (untrusted -> textContent).
    card.appendChild(el('div', 'tc-agent-task', a.task || ''));

    // Row 4: status chip + spacer + heartbeat age ("no hb" if unknown).
    var r4 = el('div', 'tc-agent-status-row');
    r4.appendChild(statusChip(info));
    r4.appendChild(el('span', 'tc-spacer'));
    r4.appendChild(ageEl('tc-agent-hb',
      { ts: a.last_seen, age_seconds: a.last_seen_age_seconds },
      { nullText: 'no hb', noHb: info.noHb,
        title: "Last check-in (heartbeat) — how long since this agent reported it's alive; \"no hb\" = it hasn't" }));
    card.appendChild(r4);

    // Row 5: RATE + CTX mini-meters (v0.76.0: hover explains what each gauge means).
    var meters = el('div', 'tc-agent-meters');
    meters.appendChild(miniMeter('RATE', capPct(a, 'rate_used_pct'),
      'AI usage budget used — red means the agent is near its rate limit and may pause'));
    meters.appendChild(miniMeter('CTX', capPct(a, 'context_used_pct'),
      'Context window filled — red means the agent is running low on room and may compact'));
    card.appendChild(meters);

    on(card, 'click', function () { openAgent(a.name); });
    return card;
  }

  // Overview-card runtime identity: "<CLI> · <Model>" plus a small
  // reasoning-effort chip. model / reasoning_effort are UNTRUSTED wire data and
  // land in textContent via el() (XSS-safe). Returns null when the model is
  // unknown (absent-not-null) so the caller omits the whole row.
  function agentCardRuntime(a) {
    if (!a || !a.model) return null;
    var row = el('div', 'tc-agent-runtime');
    var family = cliFamily(a);
    var label = (family ? prettyAlias(family) + ' · ' : '') + prettyAlias(a.model);
    row.appendChild(el('span', 'tc-agent-runtime-model', label));
    if (a.reasoning_effort) {
      row.appendChild(el('span', 'tc-chip tc-agent-effort', a.reasoning_effort));
    }
    return row;
  }

  // Live-activity rail (recent messages from root.recent). Newest on top;
  // fresh arrivals animate in one cycle (.tc-feed-item.is-fresh -> fadeInUp).
  function activityRail(root) {
    var rail = el('div', 'tc-rail');
    var card = el('div', 'tc-card tc-card-clip');
    var head = el('div', 'tc-card-head');
    head.appendChild(el('span', 'tc-rail-live-dot'));
    head.appendChild(el('span', 'tc-card-title', 'Live activity'));
    head.appendChild(el('span', 'tc-spacer'));
    var recent = root.recent || [];
    // De-jargon the caption + show freshness (v0.76.0): "last message 2m ago"
    // answers "is anything happening right now?" at a glance. "bus messages" was
    // internal vocabulary a stakeholder can't map to real work.
    var sub = el('span', 'tc-rail-sub', "who's messaging whom");
    if (recent.length) {
      sub.appendChild(el('span', null, ' · last '));
      sub.appendChild(ageEl('tc-rail-lastage', recent[0], { suffix: ' ago' }));
    }
    head.appendChild(sub);
    card.appendChild(head);

    var body = el('div', 'tc-feed');
    if (!recent.length) {
      body.appendChild(el('p', 'tc-recent-empty', 'No messages yet.'));
    } else {
      for (var i = 0; i < recent.length; i++) {
        var m = recent[i];
        var fresh = m.id && freshFeedIds[m.id];
        var item = el('div', 'tc-feed-item' + (fresh ? ' is-fresh' : ''));
        var top = el('div', 'tc-feed-row1');
        top.appendChild(el('span', 'tc-feed-from', m.from || '?'));
        top.appendChild(arrowIcon());
        top.appendChild(el('span', 'tc-feed-to', m.to || '?'));
        top.appendChild(el('span', 'tc-spacer'));
        top.appendChild(ageEl('tc-feed-age', m));
        item.appendChild(top);
        var bot = el('div', 'tc-feed-row2');
        bot.appendChild(kindChip(m.kind));
        bot.appendChild(el('span', 'tc-feed-subject', m.subject || '—'));
        item.appendChild(bot);
        body.appendChild(item);
      }
    }
    card.appendChild(body);
    rail.appendChild(card);
    return rail;
  }

  // ------------------------------------------------------------ VIEW 2: flow
  function renderFlow(main, root) {
    main.appendChild(viewHead("Who's talking to whom",
      'Message flow across the team · line weight = volume · dashed = active review'));

    var body = el('div', 'tc-flow-body');

    // Left: relationship graph card (fixed 640x480 canvas, scrolls in card).
    var graphCard = el('div', 'tc-card tc-graph-card');
    graphCard.appendChild(buildGraph(root));
    graphCard.appendChild(flowLegend(root));
    body.appendChild(graphCard);

    // Right: Active-threads list.
    var listCard = el('div', 'tc-card tc-card-clip');
    var listHead = el('div', 'tc-card-head');
    listHead.appendChild(el('span', 'tc-card-title', 'Active threads'));
    listCard.appendChild(listHead);
    var threads = root.threads || [];
    var listBody = el('div', 'tc-thread-list');
    if (!threads.length) {
      listBody.appendChild(el('p', 'tc-recent-empty', 'No active threads.'));
    } else {
      for (var i = 0; i < threads.length; i++) {
        listBody.appendChild(threadRow(threads[i]));
      }
    }
    listCard.appendChild(listBody);
    body.appendChild(listCard);

    main.appendChild(body);
  }

  // Compute node positions on the circle. Step is count-derived so any team
  // size lays out evenly (a 10-agent team is still exactly 36 deg).
  function nodePositions(agents) {
    var pos = {};
    var step = agents.length ? 360 / agents.length : 36;
    for (var i = 0; i < agents.length; i++) {
      var ang = (-90 + i * step) * Math.PI / 180;
      pos[agents[i].name] = {
        x: Math.round(GRAPH_CX + GRAPH_R * Math.cos(ang)),
        y: Math.round(GRAPH_CY + GRAPH_R * Math.sin(ang)),
      };
    }
    return pos;
  }

  // Which agent pairs have an active_review thread connecting them (dashed edge).
  // A thread's two fixed endpoints are `opener` and `opener_peer` (perspective-
  // independent); mark the pair hot only when BOTH are present.
  function activeReviewPairs(root) {
    var pairs = {};
    var threads = root.threads || [];
    for (var i = 0; i < threads.length; i++) {
      var t = threads[i];
      if (!t.active_review) continue;
      var a = t.opener, b = t.opener_peer;
      if (a && b) {
        pairs[a + '|' + b] = true;
        pairs[b + '|' + a] = true;
      }
    }
    return pairs;
  }

  function graphEdgeWidth(count) {
    var w = (typeof count === 'number' && count > 0) ? count : 1;
    // Log-scale busy pairs so volume still reads, but never turns into a blob.
    return Math.max(1, Math.min(6, 1 + Math.log(w + 1) * 1.2));
  }
  function graphEdgePoints(p1, p2) {
    var dx = p2.x - p1.x;
    var dy = p2.y - p1.y;
    var len = Math.sqrt(dx * dx + dy * dy);
    if (!len) return { x1: p1.x, y1: p1.y, x2: p2.x, y2: p2.y };
    var ux = dx / len;
    var uy = dy / len;
    var start = Math.min(14, len / 3);
    var end = Math.min(26, len / 3);
    return {
      x1: Math.round(p1.x + ux * start),
      y1: Math.round(p1.y + uy * start),
      x2: Math.round(p2.x - ux * end),
      y2: Math.round(p2.y - uy * end),
    };
  }
  function agentColor(name) {
    var h = stableHash(name);
    var hue = h % 360;
    var sat = 66 + ((h >>> 9) % 3) * 6;
    var baseLight = prefs.theme === 'dark' ? 63 : 39;
    var light = baseLight + ((h >>> 17) % 3) * 4;
    return 'hsl(' + hue + ', ' + sat + '%, ' + light + '%)';
  }
  function agentMarkerId(name) {
    return 'tc-arrow-' + stableHash(name).toString(36);
  }
  function graphAgentNames(root) {
    var names = {};
    var edges = root.edges || [];
    for (var e = 0; e < edges.length; e++) {
      if (edges[e].from) names[edges[e].from] = true;
      if (edges[e].to) names[edges[e].to] = true;
    }
    return Object.keys(names).sort();
  }
  function flowLegend(root) {
    var legend = el('div', 'tc-legend tc-flow-legend');
    legend.appendChild(el('div', 'tc-legend-label', 'Participant colors'));
    var rows = el('div', 'tc-flow-legend-rows');
    var names = graphAgentNames(root);
    if (!names.length) {
      rows.appendChild(el('div', 'tc-recent-empty', 'No participants yet.'));
    } else {
      for (var i = 0; i < names.length; i++) {
        var row = el('div', 'tc-legend-row tc-flow-legend-row');
        var sw = el('span', 'tc-flow-swatch');
        sw.style.background = agentColor(names[i]);
        row.appendChild(sw);
        row.appendChild(el('span', 'tc-legend-text', names[i]));
        rows.appendChild(row);
      }
    }
    legend.appendChild(rows);
    return legend;
  }

  function buildGraph(root) {
    var agents = agentsOf(root);
    var pos = nodePositions(agents);
    var pairs = activeReviewPairs(root);

    var canvas = el('div', 'tc-graph');
    canvas.style.width = GRAPH_W + 'px';
    canvas.style.height = GRAPH_H + 'px';

    // SVG edges layer.
    var svg = svgEl('svg', { width: GRAPH_W, height: GRAPH_H, viewBox: '0 0 ' + GRAPH_W + ' ' + GRAPH_H });
    var edges = root.edges || [];
    var senders = {};
    for (var se = 0; se < edges.length; se++) if (edges[se].from) senders[edges[se].from] = true;
    var defs = svgEl('defs');
    var senderNames = Object.keys(senders).sort();
    for (var si = 0; si < senderNames.length; si++) {
      var sender = senderNames[si];
      var marker = svgEl('marker', {
        id: agentMarkerId(sender),
        viewBox: '0 0 10 10',
        refX: 8.5,
        refY: 5,
        markerWidth: 5.5,
        markerHeight: 5.5,
        orient: 'auto',
      });
      marker.appendChild(svgEl('path', {
        d: 'M 0 0 L 10 5 L 0 10 z',
        fill: agentColor(sender),
      }));
      defs.appendChild(marker);
    }
    svg.appendChild(defs);
    for (var e = 0; e < edges.length; e++) {
      var edge = edges[e];
      var p1 = pos[edge.from], p2 = pos[edge.to];
      if (!p1 || !p2) continue;   // an edge endpoint not in the roster — skip
      var w = (typeof edge.count === 'number' ? edge.count : (edge.weight || 1));
      var hot = pairs[edge.from + '|' + edge.to] === true;
      var ep = graphEdgePoints(p1, p2);
      var line = svgEl('line', {
        x1: ep.x1, y1: ep.y1, x2: ep.x2, y2: ep.y2,
        'stroke-width': graphEdgeWidth(w),
      });
      line.setAttribute('class', 'tc-edge' + (hot ? ' is-active' : ''));
      line.style.stroke = agentColor(edge.from);
      line.setAttribute('marker-end', 'url(#' + agentMarkerId(edge.from) + ')');
      var title = svgEl('title');
      title.textContent = (edge.from || '?') + ' -> ' + (edge.to || '?') + ', count ' + w;
      line.appendChild(title);
      svg.appendChild(line);
    }
    canvas.appendChild(svg);

    // HTML nodes layer.
    var nodes = el('div', 'tc-graph-nodes');
    for (var i = 0; i < agents.length; i++) {
      (function (a) {
        var p = pos[a.name];
        var st = (a.health || {}).state;
        var node = el('div', 'tc-node');
        node.style.left = p.x + 'px';
        node.style.top = p.y + 'px';
        node.appendChild(avatarOrDot(a, 'tc-node-avatar', 'tc-node-dot'));
        node.appendChild(el('span', 'tc-node-pill', a.name));
        on(node, 'click', function () { openAgent(a.name); });
        nodes.appendChild(node);
      })(agents[i]);
    }
    canvas.appendChild(nodes);
    return canvas;
  }

  // A thread row (clickable -> Sessions). kind chip + status chip + subject +
  // participants + age.
  function threadRow(t) {
    var row = el('div', 'tc-thread-row');
    var top = el('div', 'tc-thread-row-head');
    top.appendChild(kindChip(t.opener_kind || t.kind));
    top.appendChild(el('span', 'tc-spacer'));
    var vi = threadChip(t);
    if (vi) top.appendChild(titled(el('span', 'tc-chip ' + vi.cls, vi.label), vi.desc));
    row.appendChild(top);
    row.appendChild(el('div', 'tc-thread-subject', t.subject || t.request_id || ''));
    var bot = el('div', 'tc-thread-meta');
    bot.appendChild(el('span', 'tc-thread-parts', threadParts(t)));
    bot.appendChild(el('span', 'tc-spacer'));
    bot.appendChild(ageEl('tc-thread-age', t));
    row.appendChild(bot);
    on(row, 'click', function () { openThread(t.request_id); });
    return row;
  }
  // "a ⇄ b" from the thread's two fixed endpoints. opener_peer may be absent
  // (single-party / broadcast) — then show just the opener, never a blank side.
  function threadParts(t) {
    var a = t.opener || '';
    return a + (t.opener_peer ? ' ⇄ ' + t.opener_peer : '');
  }

  // ------------------------------------------------------------ VIEW 3: attention
  function renderAttention(main, root) {
    var wrap = el('div', 'tc-attention');

    var header = viewHead('Human attention queue',
      'Ranked human-needed queue — escalations, gate holds, stuck agents, dead letters');
    header.appendChild(el('div', 'tc-spacer'));

    var items = (attentionData && attentionData.items) || [];
    var errs = (attentionData && isArray(attentionData.errors)) ? attentionData.errors : [];
    var fresh = attentionFresh();   // false when null, error-as-data, or aged-out stale
    var stale = !!attentionData && !errs.length && !fresh;   // aged-out last-known, no errors
    var count = attentionData && typeof attentionData.count === 'number' ? attentionData.count : items.length;
    // Never assert a current "N open" from an untrustworthy payload: error-as-data is
    // "status unknown"; a STALE payload is qualified — "status stale" when empty, or
    // "N open · stale" when we're showing last-known cards. Only a FRESH payload claims
    // an unqualified "N open".
    var countLabel = errs.length ? 'status unknown'
      : stale ? (items.length ? (count + ' open · stale') : 'status stale')
      : (count + ' open');
    header.appendChild(el('span', 'tc-attn-count', countLabel));
    wrap.appendChild(header);

    if (!attentionData) {
      wrap.appendChild(el('p', 'tc-recent-empty', 'Loading attention queue…'));
    } else if (errs.length) {
      // Collection failed (codex P1): the queue could NOT be built, so don't claim
      // "All clear" — say plainly that the list is unavailable right now.
      wrap.appendChild(attentionError(errs));
    } else if (!items.length) {
      // Fresh + empty is a genuine all-clear; STALE + empty must NOT claim "All clear"
      // — the topbar verdict already gates on staleness and this view must match it.
      wrap.appendChild(fresh ? attentionEmpty() : attentionStale());
    } else {
      // Non-empty: preserve the last-known cards, but if the feed is STALE, qualify them
      // as last-known/out-of-date (codex r5b P1 — don't present obsolete items as current
      // authority, and don't show an unqualified "N open").
      if (stale) wrap.appendChild(attentionStaleBanner());
      var list = el('div', 'tc-attn-list');
      for (var i = 0; i < items.length; i++) list.appendChild(attentionCard(items[i]));
      wrap.appendChild(list);
    }
    main.appendChild(wrap);
  }

  function attentionCard(item) {
    var card = el('div', 'tc-attn-card');
    // Severity left bar: CSS reads --sev-color for the inset shadow.
    card.style.setProperty('--sev-color', 'var(--' + (SEV_COLOR[item.severity] || 'gray') + ')');

    // Body block. Source tag + severity chip carry the RAW source/severity key;
    // the CSS .src-<source> / .sev-<level> rules own the color.
    var body = el('div', 'tc-attn-body');
    var tagRow = el('div', 'tc-attn-tagrow');
    tagRow.appendChild(titled(el('span', 'tc-src src-' + item.source, item.source_label || (item.source || '').toUpperCase()), SRC_DESC[item.source]));
    tagRow.appendChild(titled(el('span', 'tc-chip sev-' + item.severity, SEV_LABEL[item.severity] || (item.severity || '').toUpperCase()), SEV_DESC[item.severity]));
    tagRow.appendChild(el('span', 'tc-spacer'));
    tagRow.appendChild(ageEl('tc-attn-age', item, { suffix: ' ago' }));
    body.appendChild(tagRow);
    body.appendChild(el('div', 'tc-attn-title', item.title || ''));
    var detailRow = el('div', 'tc-attn-detailrow');
    if (item.agent) detailRow.appendChild(el('span', 'tc-attn-agent', item.agent));
    detailRow.appendChild(el('span', 'tc-attn-detail', item.detail || ''));
    body.appendChild(detailRow);
    if (item.recommendation && !(actionSession.enabled && item.answerable)) {
      body.appendChild(el('div', 'tc-attn-detail', item.recommendation));
    }
    if (item.operator_command) {
      var command = el('code', 'tc-attn-detail', item.operator_command);
      command.setAttribute('aria-label', 'Operator command');
      body.appendChild(command);
    }
    if (actionSession.enabled && item.answerable) {
      body.appendChild(attentionAnswerComposer(item));
    }
    card.appendChild(body);

    // Right block: action buttons. Disposition actions render
    // DISABLED with a CLI hint; only Inspect works (client navigation).
    var right = el('div', 'tc-attn-actions');
    var actions = attentionActions(item);
    for (var i = 0; i < actions.length; i++) {
      var act = actions[i];
      if (act.kind === 'inspect') {
        var ib = el('button', 'tc-btn', act.label);
        (function (agent) {
          on(ib, 'click', function () { if (agent) openAgent(agent); });
        })(item.agent);
        if (!item.agent) ib.disabled = true;
        right.appendChild(ib);
      } else {
        var b = el('button', 'tc-btn ' + (act.primary ? 'tc-btn-primary' : ''), act.label);
        b.disabled = true;
        b.setAttribute('title', 'run via the agenttalk CLI');
        right.appendChild(b);
      }
    }
    card.appendChild(right);
    return card;
  }

  function attentionAnswerComposer(item) {
    var action = item.answer_escalation || {};
    var toRequest = action.to_request || '';
    var box = el('div', 'tc-attn-answer');
    var meta = el('div', 'tc-attn-answer-meta');
    var requester = action.requester || '';
    if (requester) meta.appendChild(el('span', 'tc-attn-agent', 'requester ' + requester));
    var priority = item.priority && item.priority !== 'unknown' ? item.priority : '';
    if (priority) meta.appendChild(el('span', 'tc-chip', 'priority ' + priority));
    var recommendation = item.recommendation || '';
    if (recommendation) meta.appendChild(el('span', 'tc-attn-detail', recommendation));
    box.appendChild(meta);
    if (item.prompt_excerpt) {
      var prompt = el('div', 'tc-attn-question');
      prompt.appendChild(el('div', 'tc-attn-question-label', 'Question'));
      prompt.appendChild(el('div', 'tc-attn-question-text', item.prompt_excerpt));
      box.appendChild(prompt);
    }
    var opts = item.options || [];
    if (opts.length) {
      var optWrap = el('div', 'tc-attn-options');
      for (var i = 0; i < opts.length; i++) optWrap.appendChild(el('span', 'tc-chip', opts[i]));
      box.appendChild(optWrap);
    }
    var form = el('div', 'tc-attn-answer-form');
    var textarea = document.createElement('textarea');
    textarea.rows = 3;
    textarea.placeholder = 'Write the operator answer';
    var send = el('button', 'tc-btn tc-btn-primary', queuedAnswers[toRequest] ? 'Queued' : 'Queue answer');
    if (toRequest && Object.prototype.hasOwnProperty.call(answerComposerState, toRequest)) {
      textarea.value = answerComposerState[toRequest];
    }
    function updateAnswerButton() {
      send.disabled = !toRequest || queuedAnswers[toRequest] || actionSession.pending || !textarea.value.trim();
    }
    on(textarea, 'input', function () {
      if (toRequest) answerComposerState[toRequest] = textarea.value;
      updateAnswerButton();
    });
    on(send, 'click', function () {
      var body = textarea.value.trim();
      if (!body || !toRequest) return;
      postIntent({ kind: 'answer_escalation', payload: { to_request: toRequest, body: body } },
        false, function () {
          queuedAnswers[toRequest] = true;
          delete answerComposerState[toRequest];
          fetchAttention();
        });
      send.disabled = true;
    });
    updateAnswerButton();
    form.appendChild(textarea);
    form.appendChild(send);
    box.appendChild(form);
    return box;
  }

  // Action set per source (read-only release: disposition actions disabled).
  function attentionActions(item) {
    switch (item.source) {
      case 'escalation':
        return [{ label: 'Answer', primary: true }, { label: 'Reassign' }, { label: 'Defer' }];
      case 'gate':
        return [{ label: 'View evidence', primary: true }, { label: 'Defer' }];
      case 'stuck':
        return [{ label: 'Restart with context', primary: true }, { label: 'Inspect', kind: 'inspect' }];
      case 'deadletter':
        return [{ label: 'Inspect', kind: 'inspect' }, { label: 'Requeue' }, { label: 'Resolve', primary: true }];
      case 'supervisor':
        return [{ label: 'Arm', primary: true }, { label: 'Dismiss' }];
      default:
        return [{ label: 'Inspect', kind: 'inspect' }];
    }
  }

  function attentionEmpty() {
    var card = el('div', 'tc-empty');
    var badge = el('div', 'tc-empty-badge');
    badge.appendChild(checkIcon());
    card.appendChild(badge);
    card.appendChild(el('div', 'tc-empty-title', 'All clear'));
    card.appendChild(el('div', 'tc-empty-text', 'Nothing is waiting on you right now.'));
    return card;
  }

  // Slim banner above a NON-empty but STALE queue: the cards are last-known and may already
  // be resolved, so they must not read as current authority (codex r5b P1).
  function attentionStaleBanner() {
    var b = el('div', 'tc-attn-stale-banner');
    b.appendChild(el('span', null,
      'Showing the last-known queue — the feed is out of date, so these may already be resolved. Reconnecting…'));
    return b;
  }

  // Shown when the attention payload is STALE (aged-out, no errors): last-good data may
  // be out of date, so this is NOT a confirmed "All clear" either (same trust contract).
  function attentionStale() {
    var card = el('div', 'tc-empty is-error');
    card.appendChild(el('div', 'tc-empty-title', 'Queue status is out of date'));
    card.appendChild(el('div', 'tc-empty-text',
      'The last update from the bus is stale, so this may not be current — reconnecting…'));
    return card;
  }

  // Shown when /api/attention returns an error-as-data payload (200 with errors): the
  // queue couldn't be built, so this is NOT an all-clear — no green check (codex P1).
  function attentionError(errs) {
    var card = el('div', 'tc-empty is-error');
    card.appendChild(el('div', 'tc-empty-title', 'Can’t read the human queue right now'));
    card.appendChild(el('div', 'tc-empty-text',
      'The dashboard hit an error building this list, so it can’t tell you what’s waiting. '
      + 'This is a dashboard problem, not an all-clear. (' + (errs || []).join('; ') + ')'));
    return card;
  }

  // ------------------------------------------------------------ VIEW 4: lead chat
  function renderLeadChat(main, root) {
    var data = leadChatData;
    var wrap = el('div', 'tc-lead-chat');
    var head = el('div', 'tc-view-head');
    var title = el('div');
    title.appendChild(el('h1', 'tc-h1', 'Lead chat'));
    title.appendChild(el('p', 'tc-subtitle', leadChatSubtitle(data, root)));
    head.appendChild(title);
    head.appendChild(el('div', 'tc-spacer'));
    head.appendChild(leadChatStatusChip(data));
    wrap.appendChild(head);

    var layout = el('div', 'tc-lead-layout');
    layout.appendChild(leadChatTranscript(data, root));
    layout.appendChild(leadChatSide(data));
    wrap.appendChild(layout);
    main.appendChild(wrap);
  }

  function leadChatSubtitle(data, root) {
    if (!data) return 'Loading operator to lead channel';
    if (data.operator && data.lead && data.request_id) {
      return data.operator + ' -> ' + data.lead + ' · ' + data.request_id;
    }
    var label = (root && root.label) || data.root || currentRootLabel() || 'root';
    return label + ' · ' + (data.detail || data.error || 'lead chat unavailable');
  }

  function leadChatStatusChip(data) {
    if (!data) return el('span', 'tc-chip status-unknown', 'loading');
    var status = data.status || 'unavailable';
    var cls = 'status-unknown';
    var label = 'unavailable';
    if (status === 'idle') {
      cls = 'status-idle_waiting';
      label = 'lead idle';
    } else if (status === 'live') {
      cls = 'status-working_turn';
      label = 'lead live';
    } else if (data.available) {
      cls = 'status-working_silent';
      label = 'lead available';
    }
    return el('span', 'tc-chip ' + cls, label);
  }

  function leadChatTranscript(data, root) {
    var card = el('div', 'tc-card tc-lead-panel tc-lead-transcript-card');
    var head = el('div', 'tc-lead-panel-head');
    head.appendChild(el('div', 'tc-card-title', 'Direct channel'));
    head.appendChild(el('span', 'tc-spacer'));
    var count = data && isArray(data.messages) ? data.messages.length : 0;
    head.appendChild(el('span', 'tc-chip', count + ' messages'));
    card.appendChild(head);

    var body = el('div', 'tc-lead-transcript-body');
    if (!data) {
      body.appendChild(transcriptEmpty('Loading chat', ''));
    } else if (!isArray(data.messages) || !data.messages.length) {
      body.appendChild(transcriptEmpty('No messages yet', 'Send the first note to the lead.'));
    } else {
      for (var i = 0; i < data.messages.length; i++) {
        body.appendChild(leadChatMessage(data.messages[i], data.operator, root));
      }
    }
    card.appendChild(body);
    card.appendChild(leadChatComposer(data));
    return card;
  }

  function leadChatMessage(m, operator, root) {
    var mine = m.from === operator;
    var row = el('div', 'tc-msg-row tc-lead-msg-row ' + (mine ? 'is-right' : 'is-left'));
    var av;
    if (mine) {
      av = agentAvatar({
        principal: operator || 'operator',
        name: operator || 'operator',
        avatar: root && root.operator && root.operator.avatar,
      }, 'tc-lead-msg-avatar tc-operator-avatar', null, { operator: true, hideStatus: true }) ||
        operatorFallbackAvatar('tc-lead-msg-avatar tc-operator-avatar');
    } else {
      var agents = root && isArray(root.agents) ? root.agents : [];
      var agentObj = null;
      for (var i = 0; i < agents.length; i++) {
        if (agents[i] && agents[i].name === m.from) {
          agentObj = agents[i];
          break;
        }
      }
      av = avatarOrDot(agentObj || { name: m.from }, 'tc-lead-msg-avatar', 'tc-lead-msg-dot');
    }
    var bubble = el('div', 'tc-bubble ' + (mine ? 'is-right' : 'is-left'));
    var bh = el('div', 'tc-bubble-head');
    bh.appendChild(el('span', 'tc-bubble-from', mine ? 'operator' : (m.from || 'lead')));
    bh.appendChild(kindChip(m.kind || 'message'));
    bh.appendChild(el('span', 'tc-spacer'));
    bh.appendChild(ageEl('tc-bubble-age', m));
    bubble.appendChild(bh);
    bubble.appendChild(el('div', 'tc-bubble-body', m.body || ''));
    if (mine) {
      row.appendChild(bubble);
      row.appendChild(av);
    } else {
      row.appendChild(av);
      row.appendChild(bubble);
    }
    return row;
  }

  function leadChatComposer(data) {
    var form = el('div', 'tc-lead-composer');
    var textarea = document.createElement('textarea');
    textarea.rows = 4;
    textarea.placeholder = data && data.available ? 'Message the lead' : 'Lead is unavailable';
    textarea.value = leadChatComposerState.body;
    var send = el('button', 'tc-btn tc-btn-primary', actionSession.pending ? 'Queueing' : 'Send');
    var hint = el('span', 'tc-action-status',
      actionSession.error || leadChatUnavailableText(data));
    function updateButton() {
      send.disabled = !actionSession.enabled || actionSession.pending
        || !(data && data.available) || !textarea.value.trim();
    }
    on(textarea, 'input', function () {
      leadChatComposerState.body = textarea.value;
      updateButton();
    });
    on(send, 'click', function () {
      var body = textarea.value.trim();
      if (!body) return;
      postLeadChat({ body: body }, false, function () {
        leadChatComposerState.body = '';
        fetchLeadChat();
      });
      send.disabled = true;
    });
    updateButton();
    form.appendChild(textarea);
    var footer = el('div', 'tc-action-footer');
    footer.appendChild(hint);
    footer.appendChild(el('span', 'tc-spacer'));
    footer.appendChild(send);
    form.appendChild(footer);
    return form;
  }

  function leadChatUnavailableText(data) {
    if (!actionSession.enabled) return 'actions off';
    if (!data) return 'loading';
    if (data.available) return '';
    return data.detail || data.error || 'lead unavailable';
  }

  function leadChatSide(data) {
    var side = el('div', 'tc-lead-side');
    side.appendChild(leadChatLivenessCard(data));
    side.appendChild(leadChatDecisionsCard(data));
    side.appendChild(intentSummaryStrip());
    return side;
  }

  function leadChatLivenessCard(data) {
    var card = el('div', 'tc-card tc-lead-status-card');
    var head = el('div', 'tc-lead-panel-head');
    head.appendChild(el('div', 'tc-card-title', 'Lead status'));
    head.appendChild(el('span', 'tc-spacer'));
    head.appendChild(leadChatStatusChip(data));
    card.appendChild(head);
    var rows = el('div', 'tc-lead-status-rows');
    var lead = data && data.lead ? data.lead : 'unresolved';
    rows.appendChild(leadChatStatusRow('Lead', lead));
    var stateValue = data && data.liveness && data.liveness.state
      ? data.liveness.state
      : (data && data.status) || 'unknown';
    rows.appendChild(leadChatStatusRow('State', stateValue));
    if (data && data.liveness && data.liveness.reason) {
      rows.appendChild(leadChatStatusRow('Reason', data.liveness.reason));
    } else if (data && data.detail) {
      rows.appendChild(leadChatStatusRow('Reason', data.detail));
    }
    card.appendChild(rows);
    return card;
  }

  function leadChatStatusRow(k, v) {
    var row = el('div', 'tc-lead-status-row');
    row.appendChild(el('span', 'tc-lead-status-key', k));
    row.appendChild(el('span', 'tc-lead-status-value', v || ''));
    return row;
  }

  function leadChatDecisionsCard(data) {
    var card = el('div', 'tc-card tc-lead-decisions-card');
    var head = el('div', 'tc-lead-panel-head');
    head.appendChild(el('div', 'tc-card-title', 'Pending decisions'));
    head.appendChild(el('span', 'tc-spacer'));
    var items = data && isArray(data.pending_decisions) ? data.pending_decisions : [];
    head.appendChild(el('span', 'tc-chip', items.length + ' open'));
    card.appendChild(head);
    if (!data) {
      card.appendChild(el('p', 'tc-recent-empty', 'Loading decisions...'));
    } else if (!items.length) {
      card.appendChild(el('p', 'tc-recent-empty', 'No lead escalations are waiting.'));
    } else {
      var list = el('div', 'tc-lead-decision-list');
      for (var i = 0; i < items.length; i++) list.appendChild(leadChatDecision(items[i]));
      card.appendChild(list);
    }
    return card;
  }

  function leadChatDecision(item) {
    var box = el('div', 'tc-lead-decision');
    var top = el('div', 'tc-lead-decision-top');
    top.appendChild(el('span', 'tc-attn-agent', item.sender || 'lead'));
    if (item.priority) top.appendChild(el('span', 'tc-chip', 'priority ' + item.priority));
    top.appendChild(el('span', 'tc-spacer'));
    top.appendChild(ageEl('tc-attn-age', item, { suffix: ' ago' }));
    box.appendChild(top);
    box.appendChild(el('div', 'tc-attn-title', item.decision || item.subject || 'Decision needed'));
    if (item.recommendation) {
      box.appendChild(el('div', 'tc-attn-detail', item.recommendation));
    }
    box.appendChild(leadChatDecisionForm(item));
    return box;
  }

  function leadChatDecisionForm(item) {
    var toRequest = item.request_id || '';
    var form = el('div', 'tc-lead-decision-form');
    var opts = isArray(item.options) ? item.options : [];
    if (opts.length) {
      var optWrap = el('div', 'tc-attn-options');
      for (var i = 0; i < opts.length; i++) {
        (function (label) {
          var b = el('button', 'tc-btn', label);
          on(b, 'click', function () {
            answerComposerState[toRequest] = label;
            renderActiveView();
          });
          optWrap.appendChild(b);
        })(opts[i]);
      }
      form.appendChild(optWrap);
    }
    var textarea = document.createElement('textarea');
    textarea.rows = 3;
    textarea.placeholder = 'Answer the lead';
    if (toRequest && Object.prototype.hasOwnProperty.call(answerComposerState, toRequest)) {
      textarea.value = answerComposerState[toRequest];
    }
    var send = el('button', 'tc-btn tc-btn-primary',
      queuedAnswers[toRequest] ? 'Queued' : 'Queue answer');
    function updateAnswerButton() {
      send.disabled = !actionSession.enabled || actionSession.pending
        || queuedAnswers[toRequest] || !toRequest || !textarea.value.trim();
    }
    on(textarea, 'input', function () {
      if (toRequest) answerComposerState[toRequest] = textarea.value;
      updateAnswerButton();
    });
    on(send, 'click', function () {
      var body = textarea.value.trim();
      if (!body || !toRequest) return;
      postLeadChat({ to_request: toRequest, body: body }, false, function () {
        queuedAnswers[toRequest] = true;
        delete answerComposerState[toRequest];
        fetchLeadChat();
        fetchAttention();
      });
      send.disabled = true;
    });
    updateAnswerButton();
    form.appendChild(textarea);
    form.appendChild(send);
    return form;
  }

  function intentSummaryStrip() {
    var card = el('div', 'tc-card tc-intents-card');
    var head = el('div', 'tc-intents-head');
    head.appendChild(el('div', 'tc-card-title', 'Queued writes'));
    head.appendChild(el('span', 'tc-chip', (intentsData && intentsData.target_root_label) || currentRootLabel() || 'root 0'));
    card.appendChild(head);
    var items = (intentsData && intentsData.items) || [];
    if (!items.length) {
      card.appendChild(el('div', 'tc-recent-empty', 'No queued intents.'));
      return card;
    }
    var list = el('div', 'tc-intents-list');
    for (var i = 0; i < Math.min(items.length, 8); i++) {
      var it = items[i];
      var row = el('div', 'tc-intent-row');
      row.appendChild(kindChip(it.kind || 'message'));
      row.appendChild(el('span', 'tc-intent-state state-' + (it.state || 'unknown'), it.state || 'unknown'));
      row.appendChild(el('span', 'tc-intent-id', it.intent_id || ''));
      if (it.queued_stale) row.appendChild(el('span', 'tc-chip status-stuck_suspected', 'stale'));
      if (it.code) row.appendChild(el('span', 'tc-intent-code', it.code));
      list.appendChild(row);
    }
    card.appendChild(list);
    return card;
  }

  // ------------------------------------------------------------ VIEW 5: learning
  function renderLearning(main, root) {
    var data = learningData;
    var wrap = el('div', 'tc-learning');
    var head = viewHead('Learning',
      'Accepted lessons, curation provenance, and wrapper exposure telemetry');
    head.appendChild(el('div', 'tc-spacer'));
    if (data && data.counts) {
      head.appendChild(el('span', 'tc-chip', (data.counts.active || 0) + ' active'));
      head.appendChild(el('span', 'tc-chip', (data.counts.exposures || 0) + ' exposures'));
    }
    wrap.appendChild(head);

    if (!data) {
      wrap.appendChild(el('p', 'tc-recent-empty', 'Loading learning ledger...'));
      main.appendChild(wrap);
      return;
    }
    if (data.error) {
      wrap.appendChild(emptyState('Learning unavailable', data.detail || data.error));
      main.appendChild(wrap);
      return;
    }

    wrap.appendChild(learningSummary(data));
    var body = el('div', 'tc-learning-layout');
    body.appendChild(learningLessons(data));
    var side = el('div', 'tc-learning-side');
    side.appendChild(learningExposurePanel(data));
    side.appendChild(learningProblemsPanel(data));
    body.appendChild(side);
    wrap.appendChild(body);
    main.appendChild(wrap);
  }

  function learningSummary(data) {
    var counts = data.counts || {};
    var grid = el('div', 'tc-learning-stats');
    var defs = [
      ['Active', counts.active || 0],
      ['Proposed', counts.proposed || 0],
      ['Accepted', counts.accepted || 0],
      ['Review due', counts.review_due || 0],
      ['Stale', counts.stale || 0],
    ];
    for (var i = 0; i < defs.length; i++) {
      var tile = el('div', 'tc-card tc-learning-stat');
      tile.appendChild(el('div', 'tc-stat-label', defs[i][0]));
      tile.appendChild(el('div', 'tc-stat-value', defs[i][1]));
      grid.appendChild(tile);
    }
    return grid;
  }

  function learningLessons(data) {
    var card = el('div', 'tc-card tc-learning-lessons');
    var head = el('div', 'tc-lead-panel-head');
    head.appendChild(el('div', 'tc-card-title', 'Accepted lessons'));
    head.appendChild(el('span', 'tc-spacer'));
    var lessons = data.items || data.lessons || [];
    head.appendChild(el('span', 'tc-chip', lessons.length + ' lessons'));
    card.appendChild(head);
    var list = el('div', 'tc-learning-list');
    if (!lessons.length) {
      list.appendChild(transcriptEmpty('No accepted lessons yet',
        'Accepted active lessons will appear here. Proposed or stale lessons are not mixed into this default view.'));
    } else {
      for (var i = 0; i < lessons.length; i++) list.appendChild(learningLessonCard(lessons[i]));
    }
    card.appendChild(list);
    return card;
  }

  function learningLessonCard(item) {
    var card = el('div', 'tc-learning-card');
    var top = el('div', 'tc-learning-card-head');
    top.appendChild(el('span', 'tc-chip status-' + learningStatusClass(item), item.status || 'unknown'));
    if (item.active) top.appendChild(el('span', 'tc-chip status-working_turn', 'active'));
    if (item.review_due) top.appendChild(el('span', 'tc-chip sev-med', 'review due'));
    if (item.hard_stale) top.appendChild(el('span', 'tc-chip sev-high', 'stale'));
    top.appendChild(el('span', 'tc-spacer'));
    top.appendChild(el('span', 'tc-learning-key', (item.domain_id || '-') + '/' + (item.key || '-')));
    card.appendChild(top);
    card.appendChild(el('div', 'tc-learning-trigger', item.trigger || 'Lesson'));
    card.appendChild(el('div', 'tc-learning-body', item.body || ''));
    var meta = el('div', 'tc-learning-meta');
    meta.appendChild(el('span', null, 'captured by ' + (item.author || 'unknown')));
    if (item.curator) meta.appendChild(el('span', null, 'curated by ' + item.curator));
    if (item.owner) meta.appendChild(el('span', null, 'owner ' + item.owner));
    if (item.scope) meta.appendChild(el('span', null, 'scope ' + item.scope));
    card.appendChild(meta);
    if (item.evidence_ref) {
      card.appendChild(el('div', 'tc-learning-evidence', 'evidence: ' + item.evidence_ref));
    }
    if (item.applies_to && item.applies_to.length) {
      var tags = el('div', 'tc-learning-tags');
      for (var i = 0; i < item.applies_to.length; i++) tags.appendChild(el('span', 'tc-chip', item.applies_to[i]));
      card.appendChild(tags);
    }
    card.appendChild(learningExposureSummary(item.exposure || {}));
    return card;
  }

  function learningStatusClass(item) {
    if (item.status === 'accepted') return 'idle_waiting';
    if (item.status === 'proposed') return 'working_silent';
    if (item.status === 'retired') return 'unknown';
    return 'unknown';
  }

  function learningExposureSummary(exposure) {
    var box = el('div', 'tc-learning-exposure');
    var count = exposure.count || 0;
    box.appendChild(el('span', 'tc-learning-exposure-count',
      count ? ('surfaced ' + count + ' time' + (count === 1 ? '' : 's')) : 'not surfaced yet'));
    if (count) {
      var agents = exposure.agents || [];
      var names = [];
      for (var i = 0; i < agents.length; i++) names.push(agents[i].agent + ' x' + agents[i].count);
      if (names.length) box.appendChild(el('span', 'tc-learning-exposure-agents', names.join(', ')));
      if (exposure.last_request_id) {
        var btn = el('button', 'tc-link-btn', 'Open last thread');
        on(btn, 'click', function () { openThread(exposure.last_request_id); });
        box.appendChild(btn);
      }
    }
    return box;
  }

  function learningExposurePanel(data) {
    var card = el('div', 'tc-card tc-learning-panel');
    var head = el('div', 'tc-lead-panel-head');
    head.appendChild(el('div', 'tc-card-title', 'Recent surfaced lessons'));
    head.appendChild(el('span', 'tc-spacer'));
    head.appendChild(el('span', 'tc-chip', 'surfaced, not proven applied'));
    card.appendChild(head);
    var rows = el('div', 'tc-learning-exposure-list');
    var items = data.recent_exposures || [];
    if (!items.length) {
      rows.appendChild(el('div', 'tc-recent-empty', 'No wrapper lesson exposures recorded yet.'));
    } else {
      for (var i = 0; i < items.length; i++) rows.appendChild(learningExposureRow(items[i]));
    }
    card.appendChild(rows);
    return card;
  }

  function learningExposureRow(item) {
    var row = el('div', 'tc-learning-exposure-row');
    var head = el('div', 'tc-learning-exposure-head');
    head.appendChild(el('span', 'tc-learning-key', (item.domain_id || '-') + '/' + (item.key || '-')));
    head.appendChild(el('span', 'tc-spacer'));
    head.appendChild(ageEl('tc-bubble-age', { ts: item.exposed_at }, { suffix: ' ago' }));
    row.appendChild(head);
    var meta = el('div', 'tc-learning-meta');
    meta.appendChild(el('span', null, 'to ' + (item.agent || 'unknown')));
    if (item.context_scope) meta.appendChild(el('span', null, 'context ' + item.context_scope));
    if (item.request_id) meta.appendChild(el('span', null, 'request ' + item.request_id));
    row.appendChild(meta);
    if (item.evidence_ref) row.appendChild(el('div', 'tc-learning-evidence', 'lesson evidence: ' + item.evidence_ref));
    return row;
  }

  function learningProblemsPanel(data) {
    var card = el('div', 'tc-card tc-learning-panel');
    var head = el('div', 'tc-lead-panel-head');
    head.appendChild(el('div', 'tc-card-title', 'Ledger health'));
    card.appendChild(head);
    var problems = [];
    var p = data.problems || {};
    var kp = p.knowledge || [];
    var ep = p.exposures || [];
    for (var i = 0; i < kp.length; i++) problems.push('knowledge line ' + kp[i].line + ': ' + kp[i].error);
    for (var j = 0; j < ep.length; j++) problems.push('exposure line ' + ep[j].line + ': ' + ep[j].error);
    if (!problems.length) {
      card.appendChild(el('div', 'tc-recent-empty', data.note || 'Learning ledger is readable.'));
    } else {
      var list = el('div', 'tc-learning-problems');
      for (var k = 0; k < problems.length; k++) list.appendChild(el('div', 'tc-learning-problem', problems[k]));
      card.appendChild(list);
    }
    return card;
  }

  // ------------------------------------------------------------ VIEW 5b: onboarding
  function renderOnboarding(main, root) {
    var data = onboardingData;
    var wrap = el('div', 'tc-onboarding');
    var head = viewHead('Onboarding',
      'Project analysis runs, codebase claims, doc drift, and open unknowns');
    head.appendChild(el('div', 'tc-spacer'));
    if (data && data.counts) {
      head.appendChild(el('span', 'tc-chip', (data.counts.active || 0) + ' active'));
      if (data.counts.human_needed) {
        head.appendChild(el('span', 'tc-chip sev-high', data.counts.human_needed + ' human-needed'));
      }
    }
    var refresh = el('button', 'tc-pref-btn', 'Refresh');
    refresh.setAttribute('title', 'Refresh onboarding runs');
    on(refresh, 'click', fetchOnboarding);
    head.appendChild(refresh);
    wrap.appendChild(head);

    if (!data) {
      wrap.appendChild(el('p', 'tc-recent-empty', 'Loading onboarding runs...'));
      main.appendChild(wrap);
      return;
    }
    if (data.error) {
      wrap.appendChild(emptyState('Onboarding unavailable', data.detail || data.error));
      main.appendChild(wrap);
      return;
    }

    wrap.appendChild(onboardingSummary(data));
    var body = el('div', 'tc-onboarding-layout');
    body.appendChild(onboardingRuns(data));
    var side = el('div', 'tc-onboarding-side');
    side.appendChild(onboardingBlockersPanel(data));
    side.appendChild(onboardingProblemsPanel(data));
    body.appendChild(side);
    wrap.appendChild(body);
    main.appendChild(wrap);
  }

  function onboardingSummary(data) {
    var counts = data.counts || {};
    var grid = el('div', 'tc-onboarding-stats');
    var defs = [
      ['Runs', counts.total || 0],
      ['Segments accepted', (counts.accepted_segments || 0) + ' / ' + (counts.segments || 0)],
      ['Claims confirmed', (counts.confirmed_claims || 0) + ' / ' + (counts.claims || 0)],
      ['Human needed', counts.human_needed || 0],
      ['Open drift', counts.open_drift || 0],
    ];
    for (var i = 0; i < defs.length; i++) {
      var tile = el('div', 'tc-card tc-onboarding-stat');
      tile.appendChild(el('div', 'tc-stat-label', defs[i][0]));
      tile.appendChild(el('div', 'tc-stat-value', defs[i][1]));
      grid.appendChild(tile);
    }
    return grid;
  }

  function onboardingRuns(data) {
    var card = el('div', 'tc-card tc-onboarding-runs');
    var head = el('div', 'tc-lead-panel-head');
    head.appendChild(el('div', 'tc-card-title', 'Analysis runs'));
    head.appendChild(el('span', 'tc-spacer'));
    head.appendChild(el('span', 'tc-chip', (data.runs || []).length + ' shown'));
    card.appendChild(head);
    var list = el('div', 'tc-onboarding-list');
    var runs = data.runs || [];
    if (!runs.length) {
      list.appendChild(transcriptEmpty('No onboarding runs yet',
        'Create one with agenttalk onboarding create before the team starts reading a codebase.'));
    } else {
      for (var i = 0; i < runs.length; i++) list.appendChild(onboardingRunCard(runs[i]));
    }
    card.appendChild(list);
    return card;
  }

  function onboardingRunCard(run) {
    var card = el('div', 'tc-onboarding-card');
    var top = el('div', 'tc-onboarding-card-head');
    top.appendChild(el('span', 'tc-chip ' + onboardingStateClass(run.state), run.state || 'unknown'));
    if (run.blocked) top.appendChild(el('span', 'tc-chip sev-high', 'blocked'));
    top.appendChild(el('span', 'tc-spacer'));
    top.appendChild(el('span', 'tc-onboarding-key', run.id || ''));
    card.appendChild(top);
    card.appendChild(el('div', 'tc-onboarding-title', run.title || run.id || 'Onboarding run'));
    if (run.objective) card.appendChild(el('div', 'tc-onboarding-body', run.objective));
    var meta = el('div', 'tc-onboarding-meta');
    if (run.lead) meta.appendChild(el('span', null, 'lead ' + run.lead));
    if (run.base_ref) meta.appendChild(el('span', null, 'base ' + run.base_ref));
    if (run.updated_at) meta.appendChild(ageEl('tc-bubble-age', { ts: run.updated_at }, { suffix: ' ago' }));
    card.appendChild(meta);
    card.appendChild(onboardingCountStrip(run.counts || {}));
    var records = run.records || {};
    card.appendChild(onboardingRecordSection('Segments', records.segment || []));
    card.appendChild(onboardingRecordSection('Claims', records.claim || []));
    card.appendChild(onboardingRecordSection('Drift', records.drift || []));
    card.appendChild(onboardingRecordSection('Unknowns', records.unknown || []));
    return card;
  }

  function onboardingCountStrip(counts) {
    var strip = el('div', 'tc-onboarding-counts');
    var defs = [
      ['segments', (counts.accepted_segments || 0) + '/' + (counts.segments || 0)],
      ['claims', (counts.confirmed_claims || 0) + '/' + (counts.claims || 0)],
      ['drift', counts.open_drift || 0],
      ['unknowns', counts.open_unknowns || 0],
    ];
    for (var i = 0; i < defs.length; i++) {
      strip.appendChild(el('span', 'tc-chip', defs[i][0] + ' ' + defs[i][1]));
    }
    if (counts.human_needed) strip.appendChild(el('span', 'tc-chip sev-high', 'human ' + counts.human_needed));
    return strip;
  }

  function onboardingRecordSection(title, rows) {
    var box = el('div', 'tc-onboarding-section');
    var head = el('div', 'tc-onboarding-section-head');
    head.appendChild(el('span', null, title));
    head.appendChild(el('span', 'tc-chip', rows.length));
    box.appendChild(head);
    if (!rows.length) return box;
    var max = Math.min(rows.length, 6);
    for (var i = 0; i < max; i++) box.appendChild(onboardingRecordRow(rows[i]));
    if (rows.length > max) box.appendChild(el('div', 'tc-onboarding-more', '+' + (rows.length - max) + ' more'));
    return box;
  }

  function onboardingRecordRow(row) {
    var item = el('div', 'tc-onboarding-row');
    var top = el('div', 'tc-onboarding-row-head');
    top.appendChild(el('span', 'tc-chip ' + onboardingStatusClass(row.status, row.blocking), row.status || 'unknown'));
    top.appendChild(el('span', 'tc-onboarding-key', row.key || ''));
    top.appendChild(el('span', 'tc-spacer'));
    if (row.confidence) top.appendChild(el('span', 'tc-chip', row.confidence));
    item.appendChild(top);
    item.appendChild(el('div', 'tc-onboarding-body', row.summary || ''));
    var meta = el('div', 'tc-onboarding-meta');
    if (row.segment) meta.appendChild(el('span', null, 'segment ' + row.segment));
    if (row.owner) meta.appendChild(el('span', null, 'owner ' + row.owner));
    if (row.actor) meta.appendChild(el('span', null, 'by ' + row.actor));
    if (row.source) meta.appendChild(el('span', null, 'source ' + row.source));
    item.appendChild(meta);
    if (row.paths && row.paths.length) {
      item.appendChild(el('div', 'tc-onboarding-evidence', 'paths: ' + row.paths.join(', ')));
    }
    if (row.refs && row.refs.length) {
      item.appendChild(el('div', 'tc-onboarding-evidence', 'refs: ' + row.refs.join(', ')));
    }
    return item;
  }

  function onboardingBlockersPanel(data) {
    var card = el('div', 'tc-card tc-onboarding-panel');
    var head = el('div', 'tc-lead-panel-head');
    head.appendChild(el('div', 'tc-card-title', 'Human-needed blockers'));
    card.appendChild(head);
    var rows = [];
    var runs = data.runs || [];
    var kinds = ['claim', 'drift', 'segment', 'unknown'];
    for (var i = 0; i < runs.length; i++) {
      var records = runs[i].records || {};
      for (var k = 0; k < kinds.length; k++) {
        var items = records[kinds[k]] || [];
        for (var j = 0; j < items.length; j++) {
          if (items[j].blocking || (kinds[k] === 'claim' && items[j].status === 'needs-human')) {
            rows.push({ run: runs[i], row: items[j], kind: kinds[k] });
          }
        }
      }
    }
    var list = el('div', 'tc-onboarding-problems');
    if (!rows.length) {
      list.appendChild(el('div', 'tc-recent-empty', 'No human-needed blockers recorded.'));
    } else {
      for (var k = 0; k < rows.length; k++) {
        var line = el('div', 'tc-onboarding-problem');
        line.appendChild(el('div', 'tc-onboarding-key', rows[k].run.id + ' / ' + rows[k].kind + ' / ' + rows[k].row.key));
        line.appendChild(el('div', null, rows[k].row.summary || ''));
        list.appendChild(line);
      }
    }
    card.appendChild(list);
    return card;
  }

  function onboardingProblemsPanel(data) {
    var card = el('div', 'tc-card tc-onboarding-panel');
    var head = el('div', 'tc-lead-panel-head');
    head.appendChild(el('div', 'tc-card-title', 'Ledger health'));
    card.appendChild(head);
    var problems = [];
    var runs = data.runs || [];
    for (var i = 0; i < runs.length; i++) {
      var ps = runs[i].problems || [];
      for (var j = 0; j < ps.length; j++) {
        problems.push((runs[i].id || 'run') + ' line ' + ps[j].line + ': ' + ps[j].error);
      }
    }
    var top = data.problems || [];
    for (var k = 0; k < top.length; k++) {
      var rows = top[k].problems || [];
      for (var m = 0; m < rows.length; m++) {
        problems.push((top[k].run_id || 'run') + ' line ' + rows[m].line + ': ' + rows[m].error);
      }
    }
    if (!problems.length) {
      card.appendChild(el('div', 'tc-recent-empty', data.note || 'Onboarding ledger is readable.'));
    } else {
      var list = el('div', 'tc-onboarding-problems');
      for (var n = 0; n < problems.length; n++) list.appendChild(el('div', 'tc-onboarding-problem', problems[n]));
      card.appendChild(list);
    }
    return card;
  }

  function onboardingStateClass(stateName) {
    if (stateName === 'ready-for-work' || stateName === 'closed') return 'status-working_turn';
    if (stateName === 'blocked') return 'sev-high';
    if (stateName === 'abandoned' || stateName === 'superseded') return 'status-unknown';
    return 'status-idle_waiting';
  }

  function onboardingStatusClass(status, blocking) {
    if (blocking || status === 'blocked' || status === 'conflicted' || status === 'needs-human') return 'sev-high';
    if (status === 'accepted' || status === 'confirmed' || status === 'resolved' || status === 'answered') return 'status-working_turn';
    if (status === 'open' || status === 'triaged' || status === 'rework') return 'status-idle_waiting';
    return 'status-unknown';
  }

  function actionComposer(root) {
    var card = el('div', 'tc-card tc-action-card');
    var head = el('div', 'tc-action-head');
    head.appendChild(el('div', 'tc-card-title', 'Compose'));
    head.appendChild(el('span', 'tc-chip ' + (actionSession.enabled ? 'status-working_turn' : 'status-unknown'),
      actionSession.enabled ? 'actions on' : 'actions off'));
    card.appendChild(head);

    var form = el('div', 'tc-action-form');
    var mode = document.createElement('select');
    mode.appendChild(option('send', 'Send'));
    mode.appendChild(option('reply', 'Reply'));
    mode.appendChild(option('propose', 'Propose'));
    mode.appendChild(option('broadcast', 'Broadcast'));
    form.appendChild(formField('Mode', mode));

    var target = document.createElement('select');
    var as = agentsOf(root);
    for (var i = 0; i < as.length; i++) target.appendChild(option(as[i].name, as[i].name));
    form.appendChild(formField('Target', target));

    var audienceKind = document.createElement('select');
    audienceKind.appendChild(option('all', 'All'));
    audienceKind.appendChild(option('group', 'Group'));
    audienceKind.appendChild(option('role', 'Role'));
    form.appendChild(formField('Audience', audienceKind));

    var audienceValue = document.createElement('input');
    audienceValue.type = 'text';
    form.appendChild(formField('Audience value', audienceValue));

    var kind = document.createElement('select');
    kind.appendChild(option('message', 'Message'));
    kind.appendChild(option('note', 'Note'));
    kind.appendChild(option('question', 'Question'));
    form.appendChild(formField('Kind', kind));

    var subject = document.createElement('input');
    subject.type = 'text';
    form.appendChild(formField('Subject', subject));

    var body = document.createElement('textarea');
    body.rows = 4;
    form.appendChild(formField('Body', body));

    if (actionSession.enabled) {
      composerState.mode = selectPersistedValue(mode, composerState.mode);
      composerState.target = selectPersistedValue(target, composerState.target);
      composerState.audienceKind = selectPersistedValue(audienceKind, composerState.audienceKind);
      audienceValue.value = composerState.audienceValue;
      composerState.kind = selectPersistedValue(kind, composerState.kind);
      subject.value = composerState.subject;
      body.value = composerState.body;
      on(mode, 'change', function () { composerState.mode = mode.value; });
      on(target, 'change', function () { composerState.target = target.value; });
      on(audienceKind, 'change', function () { composerState.audienceKind = audienceKind.value; });
      on(audienceValue, 'input', function () { composerState.audienceValue = audienceValue.value; });
      on(kind, 'change', function () { composerState.kind = kind.value; });
      on(subject, 'input', function () { composerState.subject = subject.value; });
      on(body, 'input', function () { composerState.body = body.value; });
    }

    var footer = el('div', 'tc-action-footer');
    var status = el('span', 'tc-action-status', actionSession.error || '');
    var send = el('button', 'tc-btn tc-btn-primary', 'Queue');
    send.disabled = !actionSession.enabled || actionSession.pending;
    on(send, 'click', function () {
      var payload;
      var m = mode.value;
      if (m === 'reply') {
        if (!state.sessionRid) {
          actionSession.error = 'Select a thread first.';
          renderActiveView();
          return;
        }
        payload = { to_request: state.sessionRid, body: body.value, reply_kind: 'message' };
      } else if (m === 'broadcast') {
        payload = { audience: { kind: audienceKind.value }, subject: subject.value,
          body: body.value, message_kind: kind.value };
        if (audienceKind.value !== 'all') payload.audience.value = audienceValue.value;
      } else if (m === 'propose') {
        payload = { target: target.value, subject: subject.value, body: body.value };
      } else {
        payload = { target: target.value, subject: subject.value, body: body.value,
          message_kind: kind.value };
      }
      postIntent({ kind: m, payload: payload }, false);
    });
    footer.appendChild(status);
    footer.appendChild(el('span', 'tc-spacer'));
    footer.appendChild(send);
    form.appendChild(footer);
    card.appendChild(form);
    return card;
  }

  // ------------------------------------------------------------ VIEW 6: sessions
  function renderSessions(main, root) {
    main.appendChild(viewHead('Sessions', 'Full transcripts of message threads on the bus'));
    syncArchivedRoot(root);

    var body = el('div', 'tc-sessions-body');

    var left = el('div', 'tc-session-left');
    left.appendChild(actionComposer(root));
    left.appendChild(intentSummaryStrip());
    left.appendChild(activeThreadsCard(root));
    left.appendChild(archivedThreadsCard(root));
    body.appendChild(left);

    // Right: transcript.
    body.appendChild(transcriptCard());
    main.appendChild(body);
  }

  function activeThreadsCard(root) {
    var listCard = el('div', 'tc-card tc-card-clip');
    var head = el('div', 'tc-session-list-title tc-session-section-head');
    head.appendChild(el('span', null, 'Active'));
    head.appendChild(el('span', 'tc-spacer'));
    var threads = root.threads || [];
    head.appendChild(el('span', 'tc-chip', threads.length + ' open'));
    listCard.appendChild(head);
    if (!threads.length) {
      listCard.appendChild(el('p', 'tc-recent-empty', 'No threads.'));
    } else {
      for (var i = 0; i < threads.length; i++) {
        listCard.appendChild(sessionListRow(threads[i]));
      }
    }
    return listCard;
  }

  function archivedThreadsCard(root) {
    var card = el('div', 'tc-card tc-card-clip tc-archive-card');
    var count = rootClosedCount(root);
    var head = el('button', 'tc-session-list-title tc-session-section-head tc-archive-toggle');
    head.appendChild(el('span', null, archivedState.open ? 'Archived' : 'Archived'));
    head.appendChild(el('span', 'tc-session-subtle', 'Current session'));
    head.appendChild(el('span', 'tc-spacer'));
    head.appendChild(el('span', 'tc-chip', count + ' closed'));
    on(head, 'click', function () {
      archivedState.open = !archivedState.open;
      if (archivedState.open && !archivedState.items.length && !archivedState.loading) {
        fetchArchivedThreads(true);
      } else {
        renderActiveView();
      }
    });
    card.appendChild(head);
    if (!archivedState.open) return card;
    if (archivedState.stale) {
      var stale = el('div', 'tc-archive-stale');
      stale.appendChild(el('span', null, 'Archived list changed.'));
      var refresh = el('button', 'tc-btn', 'Refresh');
      on(refresh, 'click', function (ev) {
        ev.stopPropagation();
        fetchArchivedThreads(true);
      });
      stale.appendChild(refresh);
      card.appendChild(stale);
    }
    if (archivedState.error) {
      card.appendChild(el('p', 'tc-recent-empty', archivedState.error));
    }
    if (archivedState.loading && !archivedState.items.length) {
      card.appendChild(el('p', 'tc-recent-empty', 'Loading archived threads...'));
    } else if (!archivedState.items.length && !archivedState.error) {
      card.appendChild(el('p', 'tc-recent-empty', 'No archived threads in this session.'));
    } else {
      for (var i = 0; i < archivedState.items.length; i++) {
        card.appendChild(sessionListRow(archivedState.items[i]));
      }
    }
    if (archivedState.nextCursor) {
      var more = el('button', 'tc-archive-more', archivedState.loading ? 'Loading...' : 'Load more');
      more.disabled = archivedState.loading;
      on(more, 'click', function (ev) {
        ev.stopPropagation();
        fetchArchivedThreads(false);
      });
      card.appendChild(more);
    }
    return card;
  }

  function terminalChip(t) {
    if (!t || (t.state !== 'closed' && t.state !== 'closed-superseded')) return null;
    if (t.state === 'closed-superseded') return { label: 'superseded', cls: 'tstatus-hold' };
    return { label: 'closed', cls: 'tstatus-neutral' };
  }

  function sessionListRow(t) {
    var sel = state.sessionRid === t.request_id;
    var row = el('div', 'tc-session-row' + (sel ? ' is-selected' : ''));
    var top = el('div', 'tc-session-row-head');
    top.appendChild(kindChip(t.opener_kind || t.kind));
    top.appendChild(el('span', 'tc-spacer'));
    var vi = threadChip(t) || terminalChip(t);
    if (vi) top.appendChild(titled(el('span', 'tc-chip ' + vi.cls, vi.label), vi.desc));
    row.appendChild(top);
    row.appendChild(el('div', 'tc-session-subject', t.subject || t.request_id || ''));
    row.appendChild(el('div', 'tc-session-parts', threadParts(t)));
    on(row, 'click', function () { openThread(t.request_id); });
    return row;
  }

  function transcriptCard() {
    var card = el('div', 'tc-card tc-transcript-card');
    var rid = state.sessionRid;

    if (!rid) {
      card.appendChild(transcriptEmpty('Select a thread', 'Pick a thread on the left to read its transcript.'));
      return card;
    }
    var key = threadKey(rid);
    var data = threadCache[key];
    if (!data && threadNotFound[key]) {
      // Real "no transcript" empty state — do NOT fall back to another thread.
      // Re-fetchable: the poll clears this marker so new replies appear (P2-2).
      card.appendChild(transcriptEmpty('No transcript', 'This thread has no messages on the bus.'));
      return card;
    }
    if (!data) {
      // Fetch in flight (or not yet requested) — for the SELECTED root (P2-5).
      card.appendChild(transcriptEmpty('Loading transcript…', ''));
      fetchThread(rid);
      return card;
    }

    // Header: subject + "id · a ⇄ b" + status chip.
    var head = el('div', 'tc-transcript-head');
    var hbox = el('div', 'tc-transcript-head-text');
    hbox.appendChild(el('div', 'tc-transcript-title', data.subject || rid));
    var parts = (data.participants || []).join(' ⇄ ');
    hbox.appendChild(el('div', 'tc-transcript-meta', data.request_id + (parts ? (' · ' + parts) : '')));
    head.appendChild(hbox);
    // Verdict chip from the matching thread, if we have it.
    var t = findThreadMeta(rid);
    var vi = threadChip(t);
    if (vi) head.appendChild(titled(el('span', 'tc-chip ' + vi.cls, vi.label), vi.desc));
    card.appendChild(head);

    // Body: bubbles + system-event separators.
    var bodyWrap = el('div', 'tc-transcript-body');
    var msgs = data.messages || [];
    var firstParticipant = (data.participants && data.participants[0]) || (msgs[0] && msgs[0].from);
    if (!msgs.length) {
      bodyWrap.appendChild(transcriptEmpty('No transcript', 'This thread has no messages on the bus.'));
    } else {
      for (var i = 0; i < msgs.length; i++) {
        bodyWrap.appendChild(transcriptEntry(msgs[i], firstParticipant));
      }
    }
    card.appendChild(bodyWrap);
    return card;
  }

  function findThreadMeta(rid) {
    var root = currentRoot();
    var threads = (root && root.threads) || [];
    for (var i = 0; i < threads.length; i++) if (threads[i].request_id === rid) return threads[i];
    if (archivedState.root === currentRootId()) {
      var archived = archivedState.items || [];
      for (var j = 0; j < archived.length; j++) if (archived[j].request_id === rid) return archived[j];
    }
    return null;
  }

  function transcriptEntry(m, firstParticipant) {
    var kind = m.kind;
    // System events (wake / end) render as centered separators, not bubbles.
    if (kind === 'wake' || kind === 'end') {
      var sep = el('div', 'tc-sysevent');
      sep.appendChild(el('span', 'tc-sysevent-rule'));
      sep.appendChild(kindChip(kind));
      sep.appendChild(el('span', 'tc-sysevent-from', m.from || ''));
      sep.appendChild(el('span', 'tc-sysevent-rule'));
      return sep;
    }
    // Bubble aligned by sender: first participant = left, others = right.
    var left = m.from === firstParticipant;
    var row = el('div', 'tc-msg-row');
    var bubble = el('div', 'tc-bubble ' + (left ? 'is-left' : 'is-right'));

    var bh = el('div', 'tc-bubble-head');
    var badge = cliBadge(m.cli, m.from);
    if (badge) bh.appendChild(badge);
    bh.appendChild(el('span', 'tc-bubble-from', m.from || ''));
    bh.appendChild(kindChip(kind));
    bh.appendChild(el('span', 'tc-spacer'));
    bh.appendChild(ageEl('tc-bubble-age', m));
    bubble.appendChild(bh);

    // Body: RAW untrusted text rendered pre-wrap via textContent (never parsed).
    bubble.appendChild(el('div', 'tc-bubble-body', m.body || ''));

    // Optional safe meta_line footer (server-whitelisted keys).
    if (m.meta_line) bubble.appendChild(el('div', 'tc-bubble-meta', m.meta_line));

    row.appendChild(bubble);
    return row;
  }

  function transcriptEmpty(title, sub) {
    var box = el('div', 'tc-transcript-empty');
    box.appendChild(el('div', 'tc-empty-title', title));
    if (sub) box.appendChild(el('div', 'tc-empty-text', sub));
    return box;
  }

  // ------------------------------------------------------------ VIEW 5: agent detail
  function renderAgentDetail(main, root) {
    var a = findAgent(root, state.selectedAgent);

    // Back button.
    var back = el('button', 'tc-back');
    back.appendChild(backIcon());
    back.appendChild(el('span', null, 'Back to overview'));
    on(back, 'click', function () { go('overview', { selectedAgent: null }); });
    main.appendChild(back);

    if (!a) {
      main.appendChild(emptyState('Agent not found', 'This agent is no longer in the current root.'));
      return;
    }

    var info = agentStateInfo(a);

    // Header card.
    var headerCard = el('div', 'tc-detail-header');
    var bigDot = agentAvatar(a, 'tc-detail-avatar', 'tc-detail-bigdot');
    if (!bigDot) {
      bigDot = statusDot(info, 'tc-detail-bigdot');
      // Soft ring color: CSS reads --status-soft on the bigdot.
      bigDot.style.setProperty('--status-soft', 'var(--' + info.color + '-soft)');
    }
    headerCard.appendChild(bigDot);
    var hInfo = el('div', 'tc-detail-head-text');
    var nameRow = el('div', 'tc-detail-name-row');
    nameRow.appendChild(el('span', 'tc-detail-name', a.name));
    var badge = cliBadge(a.cli, a.name);
    if (badge) nameRow.appendChild(badge);
    hInfo.appendChild(nameRow);
    var metaRow = el('div', 'tc-detail-sub');
    metaRow.appendChild(statusChip(info));
    var roleParts = [];
    if (a.role) roleParts.push(a.role);
    if (a.groups && a.groups.length) roleParts.push(a.groups.join(', '));
    else if (a.group) roleParts.push(a.group);
    if (roleParts.length) {
      metaRow.appendChild(el('span', null, '·'));
      metaRow.appendChild(el('span', null, roleParts.join(' · ')));
    }
    var since = liveAge({ ts: a.last_seen, age_seconds: a.last_seen_age_seconds });
    if (since !== null && !info.noHb) {
      metaRow.appendChild(el('span', null, '·'));
      metaRow.appendChild(ageEl(null,
        { ts: a.last_seen, age_seconds: a.last_seen_age_seconds },
        { prefix: 'since ' }));
    }
    hInfo.appendChild(metaRow);
    headerCard.appendChild(hInfo);
    headerCard.appendChild(el('div', 'tc-spacer'));

    // Header actions: Restart (only when stuck; disabled read-only) + Open transcript.
    var actions = el('div', 'tc-detail-actions');
    if (info.key === 'stuck_suspected') {
      var rb = el('button', 'tc-btn tc-btn-primary tc-btn-lg', 'Restart with context');
      rb.disabled = true;
      rb.setAttribute('title', 'run via the agenttalk CLI');
      actions.appendChild(rb);
    }
    var ot = el('button', 'tc-btn tc-btn-lg', 'Open transcript');
    (function () {
      var rid = firstThreadForAgent(root, a.name);
      on(ot, 'click', function () { if (rid) openThread(rid); else go('sessions', { sessionRid: null }); });
    })();
    actions.appendChild(ot);
    headerCard.appendChild(actions);
    main.appendChild(headerCard);

    // Two columns.
    var cols = el('div', 'tc-detail-body');
    cols.appendChild(detailLeftCol(root, a));
    cols.appendChild(detailRightCol(root, a));
    main.appendChild(cols);
  }

  function detailLeftCol(root, a) {
    var col = el('div', 'tc-detail-col');

    // Current work card.
    var work = detailCard('Current work');
    work.appendChild(el('div', 'tc-work-task', a.task || 'No current work line.'));
    var tags = el('div', 'tc-work-tags');
    var meta = threadMetaForAgent(root, a.name);
    if (meta && meta.mission) tags.appendChild(el('span', 'tc-tag', 'mission · ' + meta.mission));
    if (meta && meta.wp_id) tags.appendChild(el('span', 'tc-tag', meta.wp_id));
    if (meta && meta.peer) tags.appendChild(el('span', 'tc-tag', 'peer · ' + meta.peer));
    if (tags.firstChild) work.appendChild(tags);
    col.appendChild(work);

    // Health timeline card (or "building history…" placeholder).
    var tl = detailCard('Health timeline · last 30m');
    var timeline = a.health_timeline;
    if (isArray(timeline) && timeline.length) {
      var bar = el('div', 'tc-timeline');
      var total = 0, i;
      for (i = 0; i < timeline.length; i++) total += (timeline[i].seconds || 0);
      var seenStates = {};
      for (i = 0; i < timeline.length; i++) {
        var seg = timeline[i];
        var segInfo = stateInfo(seg.state);
        var flex = total > 0 ? (seg.seconds || 0) / total : 1 / timeline.length;
        var segEl = el('div', 'tc-timeline-seg ' + statusClass(seg.state));
        segEl.style.flex = String(flex);
        segEl.setAttribute('title', segInfo.label + ' · ~' + Math.round((seg.seconds || 0) / 60) + 'm');
        bar.appendChild(segEl);
        seenStates[seg.state] = true;
      }
      tl.appendChild(bar);
      var legend = el('div', 'tc-timeline-legend');
      for (var s in seenStates) {
        if (Object.prototype.hasOwnProperty.call(seenStates, s)) {
          var lrow = el('div', 'tc-timeline-legend-item');
          lrow.appendChild(el('span', 'tc-timeline-legend-swatch ' + statusClass(s)));
          lrow.appendChild(el('span', 'tc-timeline-legend-label', stateInfo(s).label));
          legend.appendChild(lrow);
        }
      }
      tl.appendChild(legend);
    } else {
      tl.appendChild(el('div', 'tc-timeline-placeholder', 'building history…'));
    }
    col.appendChild(tl);

    // Recent messages card.
    var recentCard = detailCardBordered('Recent messages');
    var msgs = recentForAgent(root, a.name);
    if (!msgs.length) {
      recentCard.appendChild(el('div', 'tc-recent-empty', 'No recent traffic on the bus.'));
    } else {
      var mlist = el('div', 'tc-recent-list');
      for (var m = 0; m < msgs.length; m++) {
        var msg = msgs[m];
        var mrow = el('div', 'tc-recent-row');
        mrow.appendChild(kindChip(msg.kind));
        var dir = (msg.from === a.name) ? ('→ ' + (msg.to || '?')) : ('← ' + (msg.from || '?'));
        mrow.appendChild(el('span', 'tc-recent-dir', dir));
        mrow.appendChild(el('span', 'tc-recent-subject', msg.subject || '—'));
        mrow.appendChild(ageEl('tc-recent-age', msg));
        mlist.appendChild(mrow);
      }
      recentCard.appendChild(mlist);
    }
    col.appendChild(recentCard);
    return col;
  }

  function detailRightCol(root, a) {
    var col = el('div', 'tc-detail-col');

    // Capacity card: provider budgets + context headroom.
    var cap = detailCard('Capacity');
    cap.appendChild(capacitySummary(a));
    var capData = a.capacity || {};
    var rate = capPct(a, 'rate_used_pct');
    var primary = capWindow(capData, 'primary', rate);
    var secondary = capWindow(capData, 'secondary', null);
    if (primary) {
      cap.appendChild(capacityWindowRow('5-hour rate limit', primary,
        rate !== null && rate >= 85 ? 'Near cap - steer long work elsewhere' : resetText(primary)));
    }
    if (secondary) {
      cap.appendChild(capacityWindowRow('Weekly rate limit', secondary, resetText(secondary)));
    }
    if (!primary && !secondary) {
      cap.appendChild(el('div', 'tc-cap-empty', capData.reason || 'budget unknown'));
    }
    var ctx = capPct(a, 'context_used_pct');
    cap.appendChild(bigMeter('Context window', ctx, contextNote(capData.context, ctx)));
    col.appendChild(cap);

    // Supervisor card.
    var sup = detailCard('Supervisor');
    var supRows = el('div', 'tc-sup-rows');
    var cliI = cliInfo(a.cli, a.name);
    supRows.appendChild(supRow('CLI', cliI ? cliI.label : '—', cliI ? ('tc-chip ' + cliI.cls) : 'tc-chip'));
    var mode = a.wrapped ? 'wrapped · loop' : 'manual listen';
    supRows.appendChild(supRow('Mode', mode, 'tc-chip'));
    // Skill (v0.75.1): the agent's role/function, READ-ONLY. Role is UNTRUSTED
    // wire data -> supRow -> el() (textContent). Em-dash when absent.
    supRows.appendChild(supRow('Skill', a.role ? a.role : '—', 'tc-chip'));
    var info = agentStateInfo(a);
    // Heartbeat age is a live-ticked chip (B2a): build the row with an ageEl
    // chip so the 1 Hz ticker advances it without a DOM rebuild.
    var hbRow = el('div', 'tc-sup-row');
    hbRow.appendChild(el('span', 'tc-sup-key', 'Heartbeat'));
    hbRow.appendChild(ageEl('tc-chip ' + statusClass(info),
      { ts: a.last_seen, age_seconds: a.last_seen_age_seconds },
      { suffix: ' ago', nullText: 'missing', noHb: info.noHb }));
    supRows.appendChild(hbRow);
    if (a.wrapped !== undefined) {
      var restartable = a.restartable !== undefined ? a.restartable : a.wrapped;
      supRows.appendChild(supRow('Restartable', restartable ? 'yes' : 'no',
        'tc-chip ' + (restartable ? 'tc-sup-restartable-yes' : 'tc-sup-restartable-no')));
    }
    // v0.75.0 runtime ergonomics: Model / Effort / Runtime rows. All values are
    // UNTRUSTED wire data rendered via supRow -> el() (textContent) -> XSS-safe.
    var runtimeRows = supRuntimeRows(a);
    for (var rri = 0; rri < runtimeRows.length; rri++) supRows.appendChild(runtimeRows[rri]);
    sup.appendChild(supRows);
    col.appendChild(sup);

    // Owned domains card (only when the agent owns ≥1).
    if (isArray(a.owned_domains) && a.owned_domains.length) {
      var dom = detailCard('Owned domains');
      var domList = el('div', 'tc-domains');
      for (var i = 0; i < a.owned_domains.length; i++) {
        var d = a.owned_domains[i];
        var dEl = el('div');
        dEl.appendChild(el('div', 'tc-domain-title', d.name || ''));
        var globs = isArray(d.globs) ? d.globs.join(', ') : (d.globs || '');
        dEl.appendChild(el('div', 'tc-domain-glob', globs));
        domList.appendChild(dEl);
      }
      dom.appendChild(domList);
      col.appendChild(dom);
    }
    return col;
  }

  function supRow(k, v, chipCls) {
    var row = el('div', 'tc-sup-row');
    row.appendChild(el('span', 'tc-sup-key', k));
    row.appendChild(el('span', chipCls, v));
    return row;
  }

  // v0.75.0: the Model / Effort / Runtime Supervisor-card rows. Returns an array
  // of rows so it is unit-testable in isolation. Values (model, effort, runtime
  // state + reset_reason) are UNTRUSTED wire data and land in textContent via
  // supRow -> el(). Shows an em-dash when a field is absent.
  function supRuntimeRows(a) {
    var rows = [];
    rows.push(supRow('Model', (a && a.model) ? a.model : '—', 'tc-chip'));
    rows.push(supRow('Effort', (a && a.reasoning_effort) ? a.reasoning_effort : '—', 'tc-chip'));
    var rt = a && a.runtime;
    var rtText = '—';
    if (rt && rt.state) {
      rtText = rt.state;
      if (rt.reset_reason) rtText += ' · ' + rt.reset_reason;
    }
    rows.push(supRow('Runtime', rtText, 'tc-chip'));
    return rows;
  }

  function capacitySummary(agent) {
    var cap = agent.capacity || {};
    var wrap = el('div', 'tc-cap-summary');
    var badges = el('div', 'tc-cap-badges');
    badges.appendChild(capProviderBadge(cap, agent));
    badges.appendChild(capConfidenceChip(cap.confidence));
    if (cap.plan_type) badges.appendChild(el('span', 'tc-chip', cap.plan_type));
    wrap.appendChild(badges);
    var meta = [];
    if (cap.limit_id) meta.push(cap.limit_id);
    if (cap.rate_limit_reached_type) meta.push(cap.rate_limit_reached_type);
    if (cap.confidence === 'stale') meta.push('stale budget');
    else if (cap.confidence === 'unknown') meta.push(cap.reason || 'budget unknown');
    wrap.appendChild(el('div', 'tc-cap-meta', meta.join(' · ') || 'provider budget'));
    return wrap;
  }

  function capacityWindowRow(label, win, note) {
    var shownLabel = label;
    if (win && win.label && win.label !== '5h' && label !== 'Weekly rate limit') shownLabel += ' · ' + win.label;
    var pct = capNum(win, 'used_pct');
    return bigMeter(shownLabel, pct, note);
  }

  // Capacity meter block (7px track). Reuses .tc-meter-fill for the fill+state.
  function bigMeter(label, pct, note) {
    var wrap = el('div', 'tc-cap-block');
    var head = el('div', 'tc-cap-head');
    head.appendChild(el('span', 'tc-cap-label', label));
    head.appendChild(el('span', 'tc-cap-value', pct === null || pct === undefined ? '—' : (Math.round(pct) + '%')));
    wrap.appendChild(head);
    var track = el('div', 'tc-cap-track');
    var fill = el('div', 'tc-meter-fill ' + meterClass(pct));
    fill.style.width = meterWidth(pct);
    track.appendChild(fill);
    wrap.appendChild(track);
    wrap.appendChild(el('div', 'tc-cap-note', note));
    return wrap;
  }
  // Padded detail card with a section-label title (Current work / Capacity / …).
  function detailCard(title) {
    var card = el('div', 'tc-card tc-detail-card');
    card.appendChild(el('div', 'tc-section-label', title));
    return card;
  }
  // Clipped detail card whose title sits in a bordered head (list-style cards).
  function detailCardBordered(title) {
    var card = el('div', 'tc-card tc-card-clip');
    var head = el('div', 'tc-detail-card-head');
    head.appendChild(el('div', 'tc-section-label', title));
    card.appendChild(head);
    return card;
  }

  // --- agent-detail data derivations (all envelope-derived, textContent) ---

  // The newest open thread where this agent is the opener or next_owner, used
  // for the current-work tags (mission / wp / peer).
  function threadMetaForAgent(root, name) {
    var threads = (root && root.threads) || [];
    var best = null;
    for (var i = 0; i < threads.length; i++) {
      var t = threads[i];
      var owner = t.next_owner;
      var involved = t.opener === name ||
        owner === name ||
        (isArray(owner) && owner.indexOf(name) >= 0);
      if (!involved) continue;
      if (!best) best = t;   // threads are newest-first on the wire
    }
    if (!best) return null;
    // The peer is the OTHER of the thread's two fixed endpoints. Falls back to
    // omitting the tag (falsy) when there is no distinct peer.
    var peer = (name === best.opener) ? best.opener_peer : best.opener;
    if (!peer) peer = null;
    return { mission: best.mission, wp_id: best.wp_id, peer: peer };
  }

  function firstThreadForAgent(root, name) {
    var threads = (root && root.threads) || [];
    for (var i = 0; i < threads.length; i++) {
      var t = threads[i];
      var owner = t.next_owner;
      if (t.opener === name || owner === name ||
        (isArray(owner) && owner.indexOf(name) >= 0)) return t.request_id;
    }
    return threads.length ? threads[0].request_id : null;
  }

  // Recent messages involving this agent (from root.recent — envelope only).
  function recentForAgent(root, name) {
    var recent = (root && root.recent) || [];
    var out = [];
    for (var i = 0; i < recent.length && out.length < 5; i++) {
      var m = recent[i];
      if (m.from === name || m.to === name) out.push(m);
    }
    return out;
  }

  // ------------------------------------------------------------ shared empty state
  function emptyState(title, sub) {
    var box = el('div', 'tc-empty');
    var badge = el('div', 'tc-empty-badge');
    badge.appendChild(checkIcon());
    box.appendChild(badge);
    box.appendChild(el('div', 'tc-empty-title', title));
    if (sub) box.appendChild(el('div', 'tc-empty-text', sub));
    return box;
  }

  // Stacked view header (h1 over subtitle) inside a .tc-view-head.
  function viewHead(title, sub) {
    var head = el('div', 'tc-view-head');
    var box = el('div');
    box.appendChild(el('h1', 'tc-h1', title));
    if (sub) box.appendChild(el('p', 'tc-subtitle', sub));
    head.appendChild(box);
    return head;
  }

  // ------------------------------------------------------------ icons (SVG)
  function iconPath(d, sw) {
    var svg = svgEl('svg', { width: 17, height: 17, viewBox: '0 0 24 24', fill: 'none',
      stroke: 'currentColor', 'stroke-width': sw || 1.9, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' });
    svg.setAttribute('class', 'tc-icon');
    var parts = isArray(d) ? d : [d];
    for (var i = 0; i < parts.length; i++) svg.appendChild(svgEl('path', { d: parts[i] }));
    return svg;
  }
  function navIconGrid() { return iconPath(['M3 3h7v7H3z', 'M14 3h7v7h-7z', 'M14 14h7v7h-7z', 'M3 14h7v7H3z']); }
  function navIconChat() { return iconPath('M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z'); }
  function navIconAlert() { return iconPath(['M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z', 'M12 9v4', 'M12 17h.01']); }
  function navIconFile() { return iconPath(['M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8l-6-6z', 'M14 2v6h6', 'M9 13h6', 'M9 17h6']); }
  function missionIcon() {
    var svg = iconPath(['M9 11l3 3L22 4', 'M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11'], 2);
    svg.setAttribute('width', '13'); svg.setAttribute('height', '13');
    return svg;
  }
  function wrappedIcon() {
    var svg = iconPath('M4 8V6a2 2 0 0 1 2-2h2M16 4h2a2 2 0 0 1 2 2v2M20 16v2a2 2 0 0 1-2 2h-2M8 20H6a2 2 0 0 1-2-2v-2', 2);
    svg.setAttribute('width', '13'); svg.setAttribute('height', '13');
    svg.setAttribute('class', 'tc-icon tc-agent-wrapped');
    // SVG-native tooltip (v0.76.0): a <title> child, since an inline SVG ignores
    // the HTML title attribute. Explains the otherwise-mysterious bracket icon.
    var t = document.createElementNS(SVG_NS, 'title');
    t.textContent = 'Supervised — auto-managed (launched & restarted) by the supervisor';
    svg.appendChild(t);
    return svg;
  }
  function arrowIcon() {
    var svg = iconPath('M5 12h14M13 6l6 6-6 6', 2.2);
    svg.setAttribute('width', '12'); svg.setAttribute('height', '12');
    svg.setAttribute('class', 'tc-icon tc-feed-arrow');
    return svg;
  }
  function backIcon() {
    var svg = iconPath('M19 12H5M11 18l-6-6 6-6', 2);
    svg.setAttribute('width', '15'); svg.setAttribute('height', '15');
    svg.setAttribute('class', 'tc-icon');
    return svg;
  }
  function checkIcon() {
    var svg = iconPath('M20 6L9 17l-5-5', 2.2);
    svg.setAttribute('width', '22'); svg.setAttribute('height', '22');
    svg.setAttribute('class', 'tc-icon');
    return svg;
  }

  // ------------------------------------------------------------ data fetching
  function fetchSession(cb) {
    var projectId = currentRootId();
    var generation = rootGeneration;
    var requestKey = rootRequestKey(projectId, generation);
    if (sessionPending === requestKey) return;
    sessionPending = requestKey;
    fetch(rootUrl('/api/session', projectId), { cache: 'no-store' }).then(function (r) {
      if (!r.ok) return null;
      return r.json();
    }).then(function (data) {
      if (sessionPending === requestKey) sessionPending = null;
      if (!rootPayloadMatches(data, projectId, generation)) return;
      if (data && data.csrf_token) {
        actionSession.enabled = true;
        actionSession.token = data.csrf_token;
      } else {
        actionSession.enabled = false;
        actionSession.token = null;
      }
      if (cb) cb();
      if (state.view === 'sessions' || state.view === 'lead-chat') renderActiveViewFromPoll();
    }).catch(function () {
      if (sessionPending === requestKey) sessionPending = null;
      if (projectId !== currentRootId() || generation !== rootGeneration) return;
      actionSession.enabled = false;
      actionSession.token = null;
      if (cb) cb();
    });
  }

  function fetchIntents() {
    var projectId = currentRootId();
    var generation = rootGeneration;
    var requestKey = rootRequestKey(projectId, generation);
    if (intentsPending === requestKey) return;
    intentsPending = requestKey;
    fetch(rootUrl('/api/intents', projectId)).then(function (r) {
      if (!r.ok) return null;
      return r.json();
    }).then(function (data) {
      if (intentsPending === requestKey) intentsPending = null;
      if (!data || !rootPayloadMatches(data, projectId, generation)) return;
      intentsData = data;
      if (state.view === 'sessions') renderActiveViewFromPoll();
    }).catch(function () {
      if (intentsPending === requestKey) intentsPending = null;
    });
  }

  function postIntent(envelope, retried, onQueued) {
    if (!actionSession.enabled || !actionSession.token || actionSession.pending) return;
    var projectId = currentRootId();
    var generation = rootGeneration;
    actionSession.pending = true;
    actionSession.error = '';
    fetch(rootUrl('/api/intent', projectId), {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRF-Token': actionSession.token,
      },
      body: JSON.stringify(envelope),
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        return { status: r.status, ok: r.ok, data: data };
      });
    }).then(function (res) {
      if (projectId !== currentRootId() || generation !== rootGeneration) return;
      actionSession.pending = false;
      if (res.status === 403 && res.data && res.data.error === 'bad_csrf' && !retried) {
        fetchSession(function () { postIntent(envelope, true, onQueued); });
        return;
      }
      if (!res.ok) {
        actionSession.error = (res.data && (res.data.detail || res.data.error)) || 'intent rejected';
      } else {
        if (!rootPayloadMatches(res.data, projectId, generation)) {
          actionSession.error = 'project response mismatch';
          renderActiveView();
          return;
        }
        actionSession.error = 'Queued ' + (res.data.intent_id || 'intent');
        if (onQueued) onQueued(res.data);
        fetchIntents();
        fetchAttention();
      }
      if (state.view === 'sessions' || state.view === 'attention'
        || state.view === 'lead-chat') renderActiveView();
    }).catch(function () {
      if (projectId !== currentRootId() || generation !== rootGeneration) return;
      actionSession.pending = false;
      actionSession.error = 'network error';
      if (state.view === 'sessions' || state.view === 'attention'
        || state.view === 'lead-chat') renderActiveView();
    });
  }

  function postLeadChat(payload, retried, onQueued) {
    if (!actionSession.enabled || !actionSession.token || actionSession.pending) return;
    var projectId = currentRootId();
    var generation = rootGeneration;
    actionSession.pending = true;
    actionSession.error = '';
    fetch(rootUrl('/api/lead-chat', projectId), {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRF-Token': actionSession.token,
      },
      body: JSON.stringify(payload),
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        return { status: r.status, ok: r.ok, data: data };
      });
    }).then(function (res) {
      if (projectId !== currentRootId() || generation !== rootGeneration) return;
      actionSession.pending = false;
      if (res.status === 403 && res.data && res.data.error === 'bad_csrf' && !retried) {
        fetchSession(function () { postLeadChat(payload, true, onQueued); });
        return;
      }
      if (!res.ok) {
        actionSession.error = (res.data && (res.data.detail || res.data.error))
          || 'lead chat rejected';
      } else {
        if (!rootPayloadMatches(res.data, projectId, generation)) {
          actionSession.error = 'project response mismatch';
          renderActiveView();
          return;
        }
        actionSession.error = res.data && res.data.message_id
          ? 'Sent ' + res.data.message_id
          : 'Queued ' + (res.data.intent_id || 'lead chat');
        if (onQueued) onQueued(res.data);
        if (res.data && res.data.intent_id) fetchIntents();
        fetchLeadChat();
        fetchAttention();
      }
      if (state.view === 'lead-chat' || state.view === 'attention') renderActiveView();
    }).catch(function () {
      if (projectId !== currentRootId() || generation !== rootGeneration) return;
      actionSession.pending = false;
      actionSession.error = 'network error';
      if (state.view === 'lead-chat') renderActiveView();
    });
  }

  function fetchState() {
    // In-flight guard (P2-4): only one /api/state at a time. If a scan takes
    // >2s, stacked requests could commit out of arrival order and move the
    // console backwards; the guard + the per-response sequence check below
    // (drop anything older than the newest committed) prevent that.
    if (statePending) return;
    statePending = true;
    var seq = ++stateSeq;
    fetch('/api/state').then(function (r) {
      // r.ok guard (P3): on a non-2xx, keep the last-good lastState rather than
      // blanking the view to an error object / "Loading…".
      if (!r.ok) return null;
      return r.json();
    }).then(function (data) {
      statePending = false;
      if (!data) return;                       // non-ok — keep last-good
      if (seq < stateCommitted) return;        // stale response — drop (P2-4)
      stateCommitted = seq;
      data._fetchedAt = Date.now();
      var hadState = !!lastState;
      lastState = data;
      var projectChanged = reconcileProjectSelection();
      updateFreshFeed(data);
      if (!hadState || projectChanged) fetchRootPayloads();
      // Refetch the open transcript so new replies land (P2-2): force past the
      // cache; the fetch caches only 200s and re-validates a prior 404.
      if (state.view === 'sessions' && state.sessionRid) {
        fetchThread(state.sessionRid, true);
      }
      renderChrome();
      if (state.view !== 'lead-chat') renderActiveViewFromPoll();
    }).catch(function () { statePending = false; /* transient — retry next tick */ });
  }

  // Track which recent-feed ids are newly-arrived, so the rail can animate them
  // in exactly once (mirrors the prototype's fresh-item behavior on real data).
  function updateFreshFeed(data) {
    freshFeedIds = {};
    var rs = data.roots || [];
    var root = null;
    for (var ri = 0; ri < rs.length; ri++) {
      if (rs[ri].project_id === state.selectedRootId) {
        root = rs[ri];
        break;
      }
    }
    if (!root) root = rs[0];
    var recent = (root && root.recent) || [];
    var nextSeen = {};
    for (var i = 0; i < recent.length; i++) {
      var id = recent[i].id;
      if (!id) continue;
      nextSeen[id] = true;
      if (!seenFeedIds[id] && Object.keys(seenFeedIds).length) freshFeedIds[id] = true;
    }
    seenFeedIds = nextSeen;
  }

  function fetchAttention() {
    var projectId = currentRootId();
    var generation = rootGeneration;
    var requestKey = rootRequestKey(projectId, generation);
    if (attentionPending === requestKey) return;
    attentionPending = requestKey;
    fetch(rootUrl('/api/attention', projectId)).then(function (r) {
      if (!r.ok) return null;
      return r.json();
    }).then(function (data) {
      if (attentionPending === requestKey) attentionPending = null;
      if (!data || !rootPayloadMatches(data, projectId, generation)) return;
      data._fetchedAt = Date.now();   // freshness stamp for the team-health verdict (v0.76.0)
      attentionData = data;
      renderSidebar();  // count badge
      if (state.view === 'attention') renderActiveViewFromPoll();
    }).catch(function () {
      if (attentionPending === requestKey) attentionPending = null;
    });
  }

  function fetchLeadChat() {
    var projectId = currentRootId();
    var generation = rootGeneration;
    var requestKey = rootRequestKey(projectId, generation);
    if (leadChatPending === requestKey) return;
    leadChatPending = requestKey;
    fetch(rootUrl('/api/lead-chat', projectId)).then(function (r) {
      if (!r.ok) return null;
      return r.json();
    }).then(function (data) {
      if (leadChatPending === requestKey) leadChatPending = null;
      if (!data || !rootPayloadMatches(data, projectId, generation)) return;
      var nextHash = leadChatPayloadFingerprint(data);
      var changed = leadChatPayloadHash !== nextHash;
      leadChatPayloadHash = nextHash;
      leadChatData = data;
      renderSidebar();
      if (changed && state.view === 'lead-chat') renderActiveViewFromPoll();
    }).catch(function () {
      if (leadChatPending === requestKey) leadChatPending = null;
    });
  }

  function fetchLearning() {
    var projectId = currentRootId();
    var generation = rootGeneration;
    var requestKey = rootRequestKey(projectId, generation);
    if (learningPending === requestKey) return;
    var url = rootUrl('/api/learning?status=active&limit=100', projectId);
    learningPending = requestKey;
    fetch(url).then(function (r) {
      if (!r.ok) return null;
      return r.json();
    }).then(function (data) {
      if (learningPending === requestKey) learningPending = null;
      if (!data || !rootPayloadMatches(data, projectId, generation)) return;
      learningData = data;
      renderSidebar();
      if (state.view === 'learning') renderActiveViewFromPoll();
    }).catch(function () {
      if (learningPending === requestKey) learningPending = null;
    });
  }

  function fetchOnboarding() {
    var projectId = currentRootId();
    var generation = rootGeneration;
    var requestKey = rootRequestKey(projectId, generation);
    if (onboardingPending === requestKey) return;
    var url = rootUrl('/api/onboarding?limit=50', projectId);
    onboardingPending = requestKey;
    fetch(url).then(function (r) {
      if (!r.ok) return null;
      return r.json();
    }).then(function (data) {
      if (onboardingPending === requestKey) onboardingPending = null;
      if (!data || !rootPayloadMatches(data, projectId, generation)) return;
      onboardingData = data;
      renderSidebar();
      if (state.view === 'onboarding') renderActiveViewFromPoll();
    }).catch(function () {
      if (onboardingPending === requestKey) onboardingPending = null;
    });
  }

  function fetchArchivedThreads(reset) {
    if (archivedState.loading) return;
    var projectId = currentRootId();
    var generation = rootGeneration;
    if (!projectId) return;
    archivedState.loading = true;
    archivedState.error = '';
    if (reset) {
      archivedState.items = [];
      archivedState.nextCursor = null;
      archivedState.stale = false;
    }
    if (state.view === 'sessions') renderActiveView();
    var url = rootUrl('/api/threads?state=closed&limit=50', projectId);
    if (!reset && archivedState.nextCursor) {
      url += '&cursor=' + encodeURIComponent(archivedState.nextCursor);
    }
    fetch(url, { cache: 'no-store' }).then(function (r) {
      return r.json().then(function (data) { return { ok: r.ok, data: data }; });
    }).then(function (res) {
      if (archivedState.root !== projectId || generation !== rootGeneration) return;
      archivedState.loading = false;
      var data = res.data || {};
      if (!res.ok || data.error || !rootPayloadMatches(data, projectId, generation)) {
        archivedState.error = data.detail || data.error || 'archived threads unavailable';
        archivedState.items = reset ? [] : archivedState.items;
        archivedState.nextCursor = null;
      } else {
        var items = data.items || [];
        archivedState.items = reset ? items : archivedState.items.concat(items);
        archivedState.nextCursor = data.next_cursor || null;
        archivedState.count = typeof data.total_count === 'number' ? data.total_count : archivedState.count;
        archivedState.stale = false;
      }
      if (state.view === 'sessions') renderActiveView();
    }).catch(function () {
      if (archivedState.root !== projectId || generation !== rootGeneration) return;
      archivedState.loading = false;
      archivedState.error = 'archived threads unavailable';
      if (state.view === 'sessions') renderActiveView();
    });
  }

  // Fetch the SELECTED root's transcript (P2-5): pass ?root=<project_id> and key the
  // cache by root+rid so a same-request_id thread in another root can't bleed.
  // Only successful 200 payloads are cached (P2-2); a 404 sets a transient
  // not-found marker that the data poll re-validates, so new replies appear.
  // `force` (used by the poll refresh) bypasses the cache/pending short-circuit.
  function fetchThread(rid, force) {
    if (!rid) return;
    var projectId = currentRootId();
    var generation = rootGeneration;
    var key = threadKey(rid);  // single source of truth (matches transcriptCard read)
    var pendingKey = key + '@' + generation;
    if (!force && (threadCache[key] || threadPending[pendingKey])) return;
    threadPending[pendingKey] = true;
    var url = rootUrl('/api/thread/' + encodeURIComponent(rid), projectId);
    fetch(url).then(function (r) {
      if (r.status === 404) return { __notfound: true };
      if (!r.ok) return { __error: true };
      return r.json();
    }).then(function (data) {
      delete threadPending[pendingKey];
      if (projectId !== currentRootId() || generation !== rootGeneration) return;
      if (!data || data.__notfound) {
        // 404 → transient not-found; never cached as a permanent transcript.
        delete threadCache[key];
        threadNotFound[key] = true;
      } else if (data.__error) {
        // Non-404 error: keep any last-good payload; do NOT cache the error.
        return;
      } else if (!rootPayloadMatches(data, projectId, generation)) {
        return;
      } else {
        threadCache[key] = data;
        delete threadNotFound[key];
      }
      if (state.view === 'sessions' && state.sessionRid === rid) renderActiveViewFromPoll();
    }).catch(function () {
      delete threadPending[pendingKey];
    });
  }

  function fetchRootPayloads() {
    fetchSession();
    fetchIntents();
    fetchAttention();
    fetchLeadChat();
    fetchLearning();
    fetchOnboarding();
  }

  // ------------------------------------------------------------ loops
  function clockTick() {
    state.now = Date.now();
    // Recompute the wall clock + relative ages without a network round-trip.
    var clock = document.querySelector('#topbar .tc-clock');
    if (clock) clock.textContent = new Date(state.now).toLocaleTimeString('en-US', { hour12: false });
    // Advance age counters IN PLACE (B2a) — NEVER rebuild the DOM here, or the
    // transcript inner-scroll and any in-progress text selection are destroyed
    // every second. Only the 2s DATA poll re-renders the view.
    updateAges();
    // Flip the team-health summary when a poll outage ages state/attention past the
    // freshness window (v0.76.0): re-renders only the chrome (topbar/sidebar) + patches
    // the overview subtitle in place — never the scrollable grid/feed — and only when
    // the verdict actually changed.
    refreshHealthIfChanged();
  }

  // ------------------------------------------------------------ boot
  function boot() {
    loadPrefs();
    applyPrefs();
    renderChrome();
    fetchState();
    // Fetch attention at boot AND poll it (P2-3), regardless of the initial
    // view: the sidebar count badge is the open-attention count and must be
    // current from the start, not blank until the Attention view is opened.
    fetchRootPayloads();
    setInterval(fetchState, POLL_MS);
    setInterval(fetchAttention, POLL_MS);
    setInterval(fetchLeadChat, POLL_MS);
    setInterval(fetchIntents, POLL_MS);
    setInterval(clockTick, CLOCK_MS);
  }

  if (typeof window !== 'undefined' && window.addEventListener) {
    window.addEventListener('popstate', restoreProjectFromHistory);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
