'use strict';
/*
 * agenttalk Team Console — vanilla-JS single-page app (v0.58.0, READ-ONLY).
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

  var ACCENTS = ['blue', 'green', 'rust', 'violet', 'azure'];
  var THEMES = ['light', 'dark'];
  var DENSITIES = ['comfortable', 'compact'];
  var PREF_KEYS = { theme: 'tc.theme', accent: 'tc.accent', density: 'tc.density' };

  var VIEWS = ['overview', 'flow', 'attention', 'sessions', 'agent'];

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

  // ------------------------------------------------------------ client state
  var state = {
    view: 'overview',
    selectedAgent: null,   // agent name
    sessionRid: null,      // thread request_id
    filter: 'all',         // all | working | idle | attention
    selectedRoot: 0,
    now: Date.now(),
  };
  // Prefs (persisted). Loaded/validated below.
  var prefs = { theme: 'light', accent: 'blue', density: 'comfortable' };

  // Latest fetched payloads.
  var lastState = null;               // /api/state
  var attentionData = null;           // /api/attention (per open)
  var attentionPending = false;
  var statePending = false;           // /api/state in-flight guard (P2-4)
  var stateSeq = 0;                   // request sequence id (issued)
  var stateCommitted = 0;             // newest committed sequence id (stale-drop)
  var threadCache = {};               // rootKey(label,rid) -> /api/thread payload (200 only)
  var threadNotFound = {};            // rootKey -> true (transient; cleared/re-validated each poll)
  var threadPending = {};             // rootKey -> bool (fetch in flight)
  var freshFeedIds = {};              // msg id -> true (animate-in one cycle)
  var seenFeedIds = {};               // msg id -> true (to detect fresh)

  // ------------------------------------------------------------ tiny helpers
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
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
    return n;
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
  function stateInfo(st) {
    switch (st) {
      case 'working_turn': return { label: 'Working', key: 'working_turn', color: 'ok', grp: 'work', pulse: true };
      case 'working_silent': return { label: 'Working · quiet', key: 'working_silent', color: 'info', grp: 'work' };
      case 'idle_waiting': return { label: 'Idle · waiting', key: 'idle_waiting', color: 'warn', grp: 'idle' };
      case 'stuck_suspected': return { label: 'Stuck?', key: 'stuck_suspected', color: 'attn', grp: 'attn' };
      case 'rate_limited_or_outage': return { label: 'Rate-limited', key: 'rate_limited_or_outage', color: 'danger', grp: 'attn' };
      case 'degraded_output': return { label: 'Degraded', key: 'degraded_output', color: 'danger', grp: 'attn' };
      case 'crashed_or_exited': return { label: 'Exited', key: 'crashed_or_exited', color: 'gray', grp: 'attn' };
      case 'errored_recoverable':
      case 'errored_poison': return { label: 'Errored', key: 'errored_recoverable', color: 'attn', grp: 'attn' };
      case 'errored_fatal':
      case 'errored_ambiguous': return { label: 'Errored', key: 'errored_fatal', color: 'danger', grp: 'attn' };
      default: return { label: 'Unknown', key: 'unknown', color: 'gray', grp: 'unknown', noHb: true };
    }
  }
  // status-<state> class carrying the raw health state (CSS owns the color).
  function statusClass(st) { return 'status-' + stateInfo(st).key; }
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
  function kindInfo(kind) {
    var k = kind || 'message';
    // Null-proto lookup (P3): an untrusted kind like "constructor" misses and
    // falls back to the neutral .kind-note rather than a garbage className.
    return { label: k, cls: 'kind-' + (KNOWN_KINDS[k] ? k : 'note') };
  }

  // Thread verdict/status -> chip class. `verdict` (§3b) maps to the CSS
  // .tstatus-<verdict> family: approved/GO -> go, HOLD/rejected -> hold,
  // countered -> countered, broadcast "x/y replied" -> replied, else neutral.
  function verdictInfo(verdict) {
    if (!verdict) return null;
    var v = String(verdict);
    var lc = v.toLowerCase();
    if (lc === 'approved' || lc === 'go' || lc === 'accepted') return { label: v, cls: 'tstatus-go' };
    if (lc === 'hold' || lc === 'rejected' || lc === 'blocked') return { label: v, cls: 'tstatus-hold' };
    if (lc === 'countered') return { label: v, cls: 'tstatus-countered' };
    if (lc.indexOf('replied') >= 0) return { label: v, cls: 'tstatus-replied' };
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
      return { label: responded + '/' + audience.length + ' replied', cls: 'tstatus-replied' };
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
  function currentRoot() {
    var rs = roots();
    if (!rs.length) return null;
    var i = state.selectedRoot;
    if (i < 0 || i >= rs.length) i = 0;
    return rs[i];
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
  // Thread cache key: root-label + rid (P2-5). Keying by rid ALONE would let a
  // same-request_id thread in another root leak / cross-bleed once cached.
  function threadKey(rid) { return currentRootLabel() + ' ' + rid; }

  // Agent bucket counts by health group (work/idle/attn/unknown).
  function agentCounts(root) {
    var c = { work: 0, idle: 0, attn: 0, unknown: 0 };
    var as = agentsOf(root);
    for (var i = 0; i < as.length; i++) {
      c[stateInfo((as[i].health || {}).state).grp]++;
    }
    return c;
  }
  function filterAgents(root) {
    var as = agentsOf(root);
    if (state.filter === 'all') return as.slice();
    var out = [];
    for (var i = 0; i < as.length; i++) {
      var grp = stateInfo((as[i].health || {}).state).grp;
      if (state.filter === 'working' && grp === 'work') out.push(as[i]);
      else if (state.filter === 'idle' && grp === 'idle') out.push(as[i]);
      else if (state.filter === 'attention' && (grp === 'attn' || grp === 'unknown')) out.push(as[i]);
    }
    return out;
  }

  // ------------------------------------------------------------ shared bits
  function statusDot(st, extraCls) {
    var info = stateInfo(st);
    var cls = 'tc-dot ' + statusClass(st) + (info.pulse ? ' is-pulsing' : '');
    if (extraCls) cls += ' ' + extraCls;
    return el('span', cls);
  }
  function statusChip(st) {
    return el('span', 'tc-chip ' + statusClass(st), stateInfo(st).label);
  }
  function kindChip(kind) {
    var info = kindInfo(kind);
    return el('span', 'tc-chip ' + info.cls, info.label);
  }
  function cliBadge(cli, name) {
    var info = cliInfo(cli, name);
    if (!info) return null;
    return el('span', 'tc-chip ' + info.cls, info.label);
  }
  // A rate/ctx mini-meter (label + value + track/fill). .tc-meter-head lays out
  // its two spans as a space-between row; the fill state comes from meterClass.
  function miniMeter(label, pct) {
    var wrap = el('div', 'tc-meter');
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

  // Read a { rate_used_pct, context_used_pct } pair from an agent's capacity.
  function capPct(agent, key) {
    var cap = agent.capacity;
    if (!cap) return null;
    var v = cap[key];
    return (typeof v === 'number') ? v : null;
  }

  // ------------------------------------------------------------ navigation
  function go(view, opts) {
    state.view = view;
    if (opts && 'selectedAgent' in opts) state.selectedAgent = opts.selectedAgent;
    if (opts && 'sessionRid' in opts) state.sessionRid = opts.sessionRid;
    // Attention/thread data is view-scoped; refetch on entry.
    if (view === 'attention') fetchAttention();
    if (view === 'sessions' && state.sessionRid) fetchThread(state.sessionRid);
    renderActiveView();
    renderChrome();
  }
  function openAgent(name) { go('agent', { selectedAgent: name }); }
  function openThread(rid) { go('sessions', { sessionRid: rid }); }

  // ------------------------------------------------------------ chrome (top bar + sidebar)
  function renderChrome() {
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

    // Multi-root project switcher (only when >1 root). Each root is a
    // .tc-project-switcher pill; the wrapper only lays them out (CSSOM geometry).
    if (roots().length > 1) {
      var sw = el('div');
      sw.style.display = 'flex';
      sw.style.gap = '6px';
      var rs = roots();
      for (var i = 0; i < rs.length; i++) {
        (function (idx) {
          var b = el('button', 'tc-project-switcher' + (idx === state.selectedRoot ? ' is-active' : ''), rs[idx].label || ('root ' + idx));
          on(b, 'click', function () {
            state.selectedRoot = idx;
            // Reset drill-in state that references the old root.
            state.selectedAgent = null;
            state.sessionRid = null;
            renderChrome();
            renderActiveView();
          });
          sw.appendChild(b);
        })(i);
      }
      bar.appendChild(sw);
    }

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
    live.appendChild(el('span', 'tc-live-label', 'Live'));
    live.appendChild(el('span', 'tc-clock', new Date(state.now).toLocaleTimeString('en-US', { hour12: false })));
    bar.appendChild(live);

    bar.appendChild(el('div', 'tc-divider'));

    // Prefs controls (theme / accent / density) — read-only nav, not a write.
    bar.appendChild(prefsControls());

    // Operator chip.
    var op = el('div', 'tc-operator');
    op.appendChild(el('span', 'tc-operator-avatar', 'yo'));
    var opText = el('div', 'tc-operator-text');
    opText.appendChild(el('span', 'tc-operator-name', 'you'));
    opText.appendChild(el('span', 'tc-operator-role', 'operator · lead-liaison'));
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
    var attnCount = attentionData && typeof attentionData.count === 'number'
      ? attentionData.count
      : (attentionData && attentionData.items ? attentionData.items.length : 0);

    var nav = el('nav', 'tc-nav');
    // "overview" nav stays active while an agent-detail is open.
    var items = [
      { key: 'overview', label: 'Team overview', icon: navIconGrid, activeWith: 'agent' },
      { key: 'flow', label: 'Conversations', icon: navIconChat },
      { key: 'attention', label: 'Attention', icon: navIconAlert, badge: attnCount },
      { key: 'sessions', label: 'Sessions', icon: navIconFile },
    ];
    for (var i = 0; i < items.length; i++) {
      (function (it) {
        var active = state.view === it.key || (it.activeWith && state.view === it.activeWith);
        var row = el('button', 'tc-nav-item' + (active ? ' is-active' : ''));
        row.appendChild(it.icon());
        row.appendChild(el('span', 'tc-nav-item-label', it.label));
        if (it.badge) row.appendChild(el('span', 'tc-nav-badge', it.badge));
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
    var rows = [
      { label: 'Working', grp: 'work', count: c.work },
      { label: 'Idle · waiting', grp: 'idle', count: c.idle },
      { label: 'Needs attention', grp: 'attn', count: c.attn },
      { label: 'Unknown / offline', grp: 'unknown', count: c.unknown },
    ];
    var legendRows = el('div', 'tc-legend-rows');
    for (var j = 0; j < rows.length; j++) {
      var lr = el('div', 'tc-legend-row');
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
    var missionN = (root.spec_kitty && isArray(root.spec_kitty.missions)) ? root.spec_kitty.missions.length : 0;
    var sub = all.length + ' agents · ' + missionN + ' mission' + (missionN === 1 ? '' : 's')
      + ' active · ' + counts.attn + ' need attention';
    titleBox.appendChild(el('p', 'tc-subtitle', sub));
    header.appendChild(titleBox);
    header.appendChild(el('div', 'tc-spacer'));
    header.appendChild(filterChips(root, counts));
    main.appendChild(header);

    // Stat tiles (4).
    var tiles = el('div', 'tc-stats');
    var tileDefs = [
      { label: 'Working', grp: 'work', value: counts.work },
      { label: 'Idle', grp: 'idle', value: counts.idle },
      { label: 'Needs attention', grp: 'attn', value: counts.attn },
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
    var shown = filterAgents(root);
    if (!shown.length) {
      grid.appendChild(emptyState('No agents match', 'Try a different filter.'));
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
      { key: 'attention', label: 'Attention', count: counts.attn + counts.unknown },
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
    var st = (a.health || {}).state;
    var info = stateInfo(st);
    var card = el('div', 'tc-agent-card');
    // Left status bar: CSS reads --status-color for the inset shadow.
    card.style.setProperty('--status-color', 'var(--' + info.color + ')');

    // Row 1: dot + name + CLI badge + spacer + wrapped icon.
    var r1 = el('div', 'tc-agent-row');
    r1.appendChild(statusDot(st));
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

    // Row 3: current task (untrusted -> textContent).
    card.appendChild(el('div', 'tc-agent-task', a.task || ''));

    // Row 4: status chip + spacer + heartbeat age ("no hb" if unknown).
    var r4 = el('div', 'tc-agent-status-row');
    r4.appendChild(statusChip(st));
    r4.appendChild(el('span', 'tc-spacer'));
    r4.appendChild(ageEl('tc-agent-hb',
      { ts: a.last_seen, age_seconds: a.last_seen_age_seconds },
      { nullText: 'no hb', noHb: info.noHb }));
    card.appendChild(r4);

    // Row 5: RATE + CTX mini-meters.
    var meters = el('div', 'tc-agent-meters');
    meters.appendChild(miniMeter('RATE', capPct(a, 'rate_used_pct')));
    meters.appendChild(miniMeter('CTX', capPct(a, 'context_used_pct')));
    card.appendChild(meters);

    on(card, 'click', function () { openAgent(a.name); });
    return card;
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
    head.appendChild(el('span', 'tc-rail-sub', 'bus messages'));
    card.appendChild(head);

    var recent = root.recent || [];
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
    for (var e = 0; e < edges.length; e++) {
      var edge = edges[e];
      var p1 = pos[edge.from], p2 = pos[edge.to];
      if (!p1 || !p2) continue;   // an edge endpoint not in the roster — skip
      var w = (typeof edge.count === 'number' ? edge.count : (edge.weight || 1));
      var hot = pairs[edge.from + '|' + edge.to] === true;
      var line = svgEl('line', {
        x1: p1.x, y1: p1.y, x2: p2.x, y2: p2.y,
        'stroke-width': (w * 1.4 + 0.6),
      });
      line.setAttribute('class', 'tc-edge' + (hot ? ' is-active' : ''));
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
        node.appendChild(statusDot(st, 'tc-node-dot'));
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
    if (vi) top.appendChild(el('span', 'tc-chip ' + vi.cls, vi.label));
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

    var header = viewHead('Needs a human',
      'Ranked queue — escalations, gate holds, stuck agents, dead letters');
    header.appendChild(el('div', 'tc-spacer'));

    var items = (attentionData && attentionData.items) || [];
    var count = attentionData && typeof attentionData.count === 'number' ? attentionData.count : items.length;
    header.appendChild(el('span', 'tc-attn-count', count + ' open'));
    wrap.appendChild(header);

    if (!attentionData) {
      wrap.appendChild(el('p', 'tc-recent-empty', 'Loading attention queue…'));
    } else if (!items.length) {
      wrap.appendChild(attentionEmpty());
    } else {
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
    tagRow.appendChild(el('span', 'tc-src src-' + item.source, item.source_label || (item.source || '').toUpperCase()));
    tagRow.appendChild(el('span', 'tc-chip sev-' + item.severity, SEV_LABEL[item.severity] || (item.severity || '').toUpperCase()));
    tagRow.appendChild(el('span', 'tc-spacer'));
    tagRow.appendChild(ageEl('tc-attn-age', item, { suffix: ' ago' }));
    body.appendChild(tagRow);
    body.appendChild(el('div', 'tc-attn-title', item.title || ''));
    var detailRow = el('div', 'tc-attn-detailrow');
    if (item.agent) detailRow.appendChild(el('span', 'tc-attn-agent', item.agent));
    detailRow.appendChild(el('span', 'tc-attn-detail', item.detail || ''));
    body.appendChild(detailRow);
    card.appendChild(body);

    // Right block: action buttons. In v0.58.0 all disposition actions render
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

  // ------------------------------------------------------------ VIEW 4: sessions
  function renderSessions(main, root) {
    main.appendChild(viewHead('Sessions', 'Full transcripts of message threads on the bus'));

    var body = el('div', 'tc-sessions-body');

    // Left: thread list (from root.threads).
    var listCard = el('div', 'tc-card tc-card-clip');
    listCard.appendChild(el('div', 'tc-session-list-title', 'Threads'));
    var threads = root.threads || [];
    if (!threads.length) {
      listCard.appendChild(el('p', 'tc-recent-empty', 'No threads.'));
    } else {
      for (var i = 0; i < threads.length; i++) {
        listCard.appendChild(sessionListRow(threads[i]));
      }
    }
    body.appendChild(listCard);

    // Right: transcript.
    body.appendChild(transcriptCard());
    main.appendChild(body);
  }

  function sessionListRow(t) {
    var sel = state.sessionRid === t.request_id;
    var row = el('div', 'tc-session-row' + (sel ? ' is-selected' : ''));
    var top = el('div', 'tc-session-row-head');
    top.appendChild(kindChip(t.opener_kind || t.kind));
    top.appendChild(el('span', 'tc-spacer'));
    var vi = threadChip(t);
    if (vi) top.appendChild(el('span', 'tc-chip ' + vi.cls, vi.label));
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
    if (vi) head.appendChild(el('span', 'tc-chip ' + vi.cls, vi.label));
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

    var st = (a.health || {}).state;
    var info = stateInfo(st);

    // Header card.
    var headerCard = el('div', 'tc-detail-header');
    var bigDot = statusDot(st, 'tc-detail-bigdot');
    // Soft ring color: CSS reads --status-soft on the bigdot.
    bigDot.style.setProperty('--status-soft', 'var(--' + info.color + '-soft)');
    headerCard.appendChild(bigDot);
    var hInfo = el('div', 'tc-detail-head-text');
    var nameRow = el('div', 'tc-detail-name-row');
    nameRow.appendChild(el('span', 'tc-detail-name', a.name));
    var badge = cliBadge(a.cli, a.name);
    if (badge) nameRow.appendChild(badge);
    hInfo.appendChild(nameRow);
    var metaRow = el('div', 'tc-detail-sub');
    metaRow.appendChild(statusChip(st));
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
    if (st === 'stuck_suspected') {
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

    // Capacity card: two labeled meters + notes.
    var cap = detailCard('Capacity');
    var rate = capPct(a, 'rate_used_pct');
    var ctx = capPct(a, 'context_used_pct');
    cap.appendChild(bigMeter('5-hour rate limit', rate,
      rate !== null && rate >= 85 ? 'Near cap — steer long work elsewhere' : 'Headroom for new work'));
    cap.appendChild(bigMeter('Context window', ctx,
      ctx !== null && ctx >= 85 ? 'Compaction risk — avoid heavy context' : 'Comfortable context budget'));
    col.appendChild(cap);

    // Supervisor card.
    var sup = detailCard('Supervisor');
    var supRows = el('div', 'tc-sup-rows');
    var cliI = cliInfo(a.cli, a.name);
    supRows.appendChild(supRow('CLI', cliI ? cliI.label : '—', cliI ? ('tc-chip ' + cliI.cls) : 'tc-chip'));
    var mode = a.wrapped ? 'wrapped · loop' : 'manual listen';
    supRows.appendChild(supRow('Mode', mode, 'tc-chip'));
    var st = (a.health || {}).state;
    var info = stateInfo(st);
    // Heartbeat age is a live-ticked chip (B2a): build the row with an ageEl
    // chip so the 1 Hz ticker advances it without a DOM rebuild.
    var hbRow = el('div', 'tc-sup-row');
    hbRow.appendChild(el('span', 'tc-sup-key', 'Heartbeat'));
    hbRow.appendChild(ageEl('tc-chip ' + statusClass(st),
      { ts: a.last_seen, age_seconds: a.last_seen_age_seconds },
      { suffix: ' ago', nullText: 'missing', noHb: info.noHb }));
    supRows.appendChild(hbRow);
    if (a.wrapped !== undefined) {
      var restartable = a.restartable !== undefined ? a.restartable : a.wrapped;
      supRows.appendChild(supRow('Restartable', restartable ? 'yes' : 'no',
        'tc-chip ' + (restartable ? 'tc-sup-restartable-yes' : 'tc-sup-restartable-no')));
    }
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
      updateFreshFeed(data);
      lastState = data;
      // Clamp selectedRoot.
      if (state.selectedRoot >= (data.roots || []).length) state.selectedRoot = 0;
      // Refetch the open transcript so new replies land (P2-2): force past the
      // cache; the fetch caches only 200s and re-validates a prior 404.
      if (state.view === 'sessions' && state.sessionRid) {
        fetchThread(state.sessionRid, true);
      }
      renderChrome();
      renderActiveView();
    }).catch(function () { statePending = false; /* transient — retry next tick */ });
  }

  // Track which recent-feed ids are newly-arrived, so the rail can animate them
  // in exactly once (mirrors the prototype's fresh-item behavior on real data).
  function updateFreshFeed(data) {
    freshFeedIds = {};
    var rs = data.roots || [];
    var ri = state.selectedRoot < rs.length ? state.selectedRoot : 0;
    var root = rs[ri];
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
    if (attentionPending) return;
    attentionPending = true;
    fetch('/api/attention').then(function (r) { return r.json(); }).then(function (data) {
      attentionData = data;
      attentionPending = false;
      renderSidebar();  // count badge
      if (state.view === 'attention') renderActiveView();
    }).catch(function () { attentionPending = false; });
  }

  // Fetch the SELECTED root's transcript (P2-5): pass ?root=<label> and key the
  // cache by root+rid so a same-request_id thread in another root can't bleed.
  // Only successful 200 payloads are cached (P2-2); a 404 sets a transient
  // not-found marker that the data poll re-validates, so new replies appear.
  // `force` (used by the poll refresh) bypasses the cache/pending short-circuit.
  function fetchThread(rid, force) {
    if (!rid) return;
    var label = currentRootLabel();
    var key = label + ' ' + rid;
    if (!force && (threadCache[key] || threadPending[key])) return;
    threadPending[key] = true;
    var url = '/api/thread/' + encodeURIComponent(rid) + '?root=' + encodeURIComponent(label);
    fetch(url).then(function (r) {
      if (r.status === 404) return { __notfound: true };
      if (!r.ok) return { __error: true };
      return r.json();
    }).then(function (data) {
      threadPending[key] = false;
      if (!data || data.__notfound) {
        // 404 → transient not-found; never cached as a permanent transcript.
        delete threadCache[key];
        threadNotFound[key] = true;
      } else if (data.__error) {
        // Non-404 error: keep any last-good payload; do NOT cache the error.
        return;
      } else {
        threadCache[key] = data;
        delete threadNotFound[key];
      }
      if (state.view === 'sessions' && state.sessionRid === rid) renderActiveView();
    }).catch(function () {
      threadPending[key] = false;
    });
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
    fetchAttention();
    setInterval(fetchState, POLL_MS);
    setInterval(fetchAttention, POLL_MS);
    setInterval(clockTick, CLOCK_MS);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
