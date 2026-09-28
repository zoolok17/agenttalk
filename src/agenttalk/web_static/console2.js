/* =============================================================================
   agenttalk console v2 - console2.js   (pitch slice, milestones M1 / M1b / M2)

   Renders the /v2 shell. Rules this file keeps (and tests/test_console2_web.py
   enforces on its source):
     - every node is built with createElement + textContent; anything that came
       from the bus or a feed is text, never markup;
     - no inline style attribute: theme and layout come from console2.css, chosen
       through one data-theme attribute (a meter fill sets its width through the
       style object, which the console CSP allows);
     - no link is built from data (the two links are static in the served shell);
     - read-only: the only requests are GET /api/state, /api/attention and
       /api/lead-chat, all through getJson().

   Data layer (M2): poll /api/state every 2 s, then /api/attention and
   /api/lead-chat for the selected team (the others' attention every 5th round).
   "Now" is generated_at of the newest snapshot plus monotonic elapsed time, never
   the browser clock. Everything derived comes from console2-model.js; this file
   only fetches, keeps the last good data, and draws the model.

   Header controls are built ONCE and updated in place, so keyboard focus stays on
   the control the operator just used. A change in the SET of team chips rebuilds
   them and puts focus back on the chip with the same key.

   Team selection follows ?root= like the classic console: project_id first, then
   a unique label. A root that matches nothing is an explicit "unknown team" state
   with the teams to pick from; it is never replaced by the first team.
   ============================================================================= */
(function () {
  'use strict';

  var M = window.AgentTalkConsole2Model;
  if (!M) return;   // the model script failed to load: leave the shell inert
  var M2 = window.AgentTalkConsole2BoardModel;   // B8: the board's own pure model, same load guard

  var THEME_KEY = 'agenttalk.console2.theme';
  var VISIT_KEY = 'agenttalk.console2.lastvisit';
  var THEME_LABEL = { midnight: 'Midnight', paper: 'Paper', synthwave: 'Synthwave', terminal: 'Terminal' };
  var RUNTIME_LABEL = { claude: 'Claude', codex: 'Codex' };
  var RUNTIME_LETTER = { claude: 'C', codex: 'X', qwen: 'Q' };
  var POLL_MS = 2000;
  var BOARD_POLL_MS = 5000;   // section 5: the board polls on its own, slower, hidden-tab-aware cadence
  var OTHER_ATTENTION_EVERY = 5;
  var PARAM_SHOW_MAX = 64;
  var REQUEST_TIMEOUT_MS = 5000;   // no request may hold anything up for longer than this
  var PAINT_MS = 1000;             // the page repaints on its own clock, whatever the feeds are doing

  var theme = M.DEFAULT_THEME;
  var rootParam = readRootParam();   // what ?root= asked for, then what the operator picked

  // Everything the last good reads gave us, plus what the polls learned.
  var data = {
    snapshot: null,          // last good /api/state payload
    generatedMs: null,       // its generated_at
    anchor: null,            // { epochMs, perf }: server time at receipt + the monotonic clock then
    conn: { reachable: true, stalledPolls: 0, lastOkMs: null },
    attention: Object.create(null),   // project_id -> { ok, asOfMs, items }
    chat: Object.create(null),        // project_id -> { ok, asOfMs, payload }
    board: Object.create(null),       // project_id -> { ok, payload }  (B8: /api/work-board)
    cycle: 0,
    statePending: false,      // F1: at most one /api/state request in flight, ever
    stateSeq: 0                // incremented only when a request is actually issued (belt and braces)
  };
  var ui = { deferred: {}, snoozedUntil: {}, answered: {}, lastVisitMs: null };
  var chrome = { teamSeg: null, themeButtons: [], chipKeys: [], chips: [], keysBtn: null, overlay: null };
  var lastSig = null;
  var lastRailSig = null;
  var lastBoardSig = null;
  var running = false;
  var inflight = {};       // feed key -> true while a request for it is outstanding

  // M4b: j/k select an open card by id, never by DOM position (a card that leaves the list must not
  // silently hand the selection to whatever now sits at the same index). Reset whenever the visible
  // team changes, so a stale id from a different team's stream is never carried over.
  var nav = { selectedTeam: null, selectedId: null, overlayOpen: false, overlayInvoker: null };
  var streamCardTeam = null;
  var streamCardIds = [];      // ids of the currently open cards, in display order, for streamCardTeam

  // B8: the board's own selection, by card key (work_item), never by DOM position - same
  // discipline as nav.selectedId above. Reset whenever the visible team changes.
  var boardEnabled = !!(M2 && typeof M2.parseRoute === 'function');
  var boardNav = { selectedTeam: null, selectedKey: null };
  var boardCardKeys = [];      // keys of the currently rendered cards, in display order

  // ---------------------------------------------------------------- helpers

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function clear(node) { node.textContent = ''; }

  function on(node, type, fn) { node.addEventListener(type, fn); }

  function setPressed(button, pressed) {
    button.setAttribute('aria-pressed', pressed ? 'true' : 'false');
  }

  function perfNow() {
    return typeof performance !== 'undefined' && performance && typeof performance.now === 'function'
      ? performance.now() : Date.now();
  }

  // Server time now: generated_at of the newest snapshot plus elapsed monotonic time.
  function nowMs() {
    if (!data.anchor) return Date.now();
    return data.anchor.epochMs + (perfNow() - data.anchor.perf);
  }

  // ------------------------------------------------------------------ theme

  function loadTheme() {
    try {
      return M.normalizeTheme(window.localStorage.getItem(THEME_KEY));
    } catch (e) {
      return M.DEFAULT_THEME;   // storage unavailable: keep the default
    }
  }

  function applyTheme(name) {
    theme = M.normalizeTheme(name);
    document.documentElement.setAttribute('data-theme', theme);
  }

  // A choice the operator made: apply it, remember it, update the buttons in place.
  function setTheme(name) {
    applyTheme(name);
    try { window.localStorage.setItem(THEME_KEY, theme); } catch (e) { /* not persisted */ }
    syncThemeButtons();
  }

  function syncThemeButtons() {
    chrome.themeButtons.forEach(function (entry) { setPressed(entry.button, entry.name === theme); });
  }

  // "Since you last looked": the time this page was last opened, per browser. Read
  // the previous visit, then record this one (and again when the page is hidden).
  function loadVisit() {
    try {
      var raw = window.localStorage.getItem(VISIT_KEY);
      var n = raw === null ? NaN : Number(raw);
      ui.lastVisitMs = isFinite(n) && n > 0 ? n : null;
    } catch (e) {
      ui.lastVisitMs = null;
    }
    saveVisit();
  }

  function saveVisit() {
    try { window.localStorage.setItem(VISIT_KEY, String(Date.now())); } catch (e) { /* not persisted */ }
  }

  // ------------------------------------------------------------ team selection

  // The ?root= the page was opened with. A repeated parameter is treated like the
  // server treats it (not a usable selector): it becomes an unknown selection.
  function readRootParam() {
    try {
      var params = new window.URLSearchParams(window.location.search || '');
      var all = params.getAll('root');
      if (all.length > 1) return all.join(',');
      return all.length ? all[0] : '';
    } catch (e) {
      return '';
    }
  }

  function currentRoots() {
    return data.snapshot && Array.isArray(data.snapshot.roots) ? data.snapshot.roots : null;
  }

  // B8: ONE router owns #board, #conversation and the reserved #review=<escalation-id> (no UI for
  // review yet - it renders exactly like #conversation, but the id still parses without crashing,
  // so a link to it is never a broken/blank page). ?root= is untouched by any hash change: every
  // nav link this file builds is a bare hash ("#board"), which a browser navigates without
  // touching the current pathname or query string at all.
  function currentRoute() {
    if (!boardEnabled) return { mode: 'conversation', reviewId: null };
    return M2.parseRoute(window.location.hash || '');
  }

  function rootKey(root) {
    return root && typeof root.project_id === 'string' ? root.project_id : '';
  }

  // The operator picked a team: remember it, keep the address bar in step (so a
  // reload or a shared link lands on the same team), draw, and fetch its feeds now.
  function pickTeam(index) {
    var roots = currentRoots();
    var r = roots && roots[index];
    if (!r) return;
    rootParam = rootKey(r) || r.label || '';
    try {
      var params = new window.URLSearchParams(window.location.search || '');
      params.set('root', rootParam);
      window.history.replaceState({}, '', window.location.pathname + '?' + params.toString() +
        (window.location.hash || ''));
    } catch (e) { /* history unavailable: the choice still holds for this page */ }
    renderAll();
    pollFeeds(false).then(renderAll, renderAll);
  }

  // ------------------------------------------------------------ header (in place)

  function focusedChipKey() {
    var active = document.activeElement;
    return active && typeof active.getAttribute === 'function' ? active.getAttribute('data-c2-key') : null;
  }

  function buildChip(team) {
    var chip = el('button', 'c2-chip');
    chip.setAttribute('type', 'button');
    chip.setAttribute('data-c2-key', team.key);
    var parts = { dot: el('span', 'c2-dot'), label: el('span', 'c2-chip-label'), badge: el('span', 'c2-badge') };
    chip.appendChild(parts.dot);
    chip.appendChild(parts.label);
    chip.appendChild(parts.badge);
    on(chip, 'click', function () { pickTeam(team.index); });
    return { key: team.key, button: chip, parts: parts };
  }

  function paintChip(entry, team) {
    var p = entry.parts;
    p.dot.className = 'c2-dot is-' + team.freshness;
    if (p.label.textContent !== team.label) p.label.textContent = team.label;
    var badge = team.needsCount ? String(team.needsCount) : '';
    p.badge.className = badge ? 'c2-badge' : 'c2-badge is-empty';
    if (p.badge.textContent !== badge) p.badge.textContent = badge;
    entry.button.setAttribute('title', team.freshness === 'live' ? team.label : team.label + ' (' + team.freshness + ')');
    setPressed(entry.button, team.pressed);
  }

  // Keep the chips in place while the set of teams is unchanged; otherwise rebuild
  // them and give focus back to the chip that had it.
  function syncTeamChips(teams) {
    var seg = chrome.teamSeg;
    if (!seg) return;
    if (!teams.length) {
      // Still loading, or the feed gave no team: say only what is known.
      var idle = el('button', 'c2-chip', data.conn.reachable ? 'Team' : 'No team data');
      idle.setAttribute('type', 'button');
      setPressed(idle, false);
      idle.disabled = true;
      clear(seg);
      seg.appendChild(idle);
      chrome.chipKeys = [];
      chrome.chips = [];
      return;
    }
    var keys = teams.map(function (t) { return t.key; });
    var same = keys.length === chrome.chipKeys.length && keys.every(function (k, i) { return k === chrome.chipKeys[i]; });
    if (!same || chrome.chips.length !== teams.length) {
      var hadFocus = focusedChipKey();
      clear(seg);
      chrome.chips = teams.map(buildChip);
      var restore = null;
      chrome.chips.forEach(function (entry) {
        seg.appendChild(entry.button);
        if (hadFocus !== null && entry.key === hadFocus) restore = entry.button;
      });
      chrome.chipKeys = keys;
      teams.forEach(function (t, i) { paintChip(chrome.chips[i], t); });
      if (restore && typeof restore.focus === 'function') restore.focus();
      return;
    }
    teams.forEach(function (t, i) { paintChip(chrome.chips[i], t); });
  }

  function buildHeader() {
    var bar = document.getElementById('c2-header');
    if (!bar) return;
    clear(bar);

    var brand = el('div', 'c2-brand');
    brand.appendChild(el('span', 'c2-logo'));
    brand.appendChild(el('span', 'c2-wordmark', 'agenttalk'));
    bar.appendChild(brand);

    var teamSeg = el('div', 'c2-seg');
    teamSeg.setAttribute('role', 'group');
    teamSeg.setAttribute('aria-label', 'Team');
    bar.appendChild(teamSeg);
    chrome.teamSeg = teamSeg;

    bar.appendChild(el('div', 'c2-spacer'));

    var themeSeg = el('div', 'c2-seg');
    themeSeg.setAttribute('role', 'group');
    themeSeg.setAttribute('aria-label', 'Theme');
    chrome.themeButtons = M.THEMES.map(function (name) {
      var btn = el('button', '', THEME_LABEL[name] || name);
      btn.setAttribute('type', 'button');
      on(btn, 'click', function () { setTheme(name); });
      themeSeg.appendChild(btn);
      return { name: name, button: btn };
    });
    bar.appendChild(themeSeg);

    var keys = el('button', 'c2-keybtn', '?');
    keys.setAttribute('type', 'button');
    keys.setAttribute('aria-label', 'Keyboard shortcuts');
    keys.setAttribute('title', 'Keyboard shortcuts');
    keys.setAttribute('aria-haspopup', 'dialog');
    keys.setAttribute('aria-pressed', 'false');
    on(keys, 'click', function () { setOverlayOpen(!nav.overlayOpen); });
    bar.appendChild(keys);
    chrome.keysBtn = keys;

    syncThemeButtons();
    syncTeamChips([]);
  }

  // ---------------------------------------------------------- keyboard overlay

  var KEY_HELP = [
    ['j', 'Select the next card'], ['k', 'Select the previous card'],
    ['Enter', 'Open the selected card (focus its first action)'],
    ['l', 'Later: defer the selected card'],
    ['/', 'Focus the message box'], ['1', 'Switch to the first team'], ['2', 'Switch to the second team'],
    ['t', 'Cycle theme'], ['Esc', 'Close this, or clear the card selection'], ['?', 'Toggle this keyboard map'],
  ];

  function buildOverlay() {
    var box = el('div', 'c2-overlay');
    box.setAttribute('id', 'c2-keymap');
    box.setAttribute('role', 'dialog');
    box.setAttribute('aria-modal', 'true');
    box.setAttribute('aria-label', 'Keyboard shortcuts');
    box.appendChild(el('div', 'c2-overlay-backdrop'));
    var panel = el('div', 'c2-overlay-panel');
    panel.appendChild(el('h2', 'c2-overlay-title', 'Keyboard shortcuts'));
    var list = el('dl', 'c2-keymap-list');
    KEY_HELP.forEach(function (row) {
      list.appendChild(el('dt', 'c2-keymap-key', row[0]));
      list.appendChild(el('dd', 'c2-keymap-desc', row[1]));
    });
    panel.appendChild(list);
    var close = el('button', 'c2-overlay-close', 'Close');
    close.setAttribute('type', 'button');
    on(close, 'click', function () { setOverlayOpen(false); });
    panel.appendChild(close);
    box.appendChild(panel);
    return box;
  }

  // R1: the background regions, made `inert` (never a style attribute) while the overlay is open, so
  // neither Tab nor a click can reach them at all - the browser itself refuses, the same way it
  // refuses focus on a disabled control. `trapOverlayTab` below is a second, explicit line of
  // defense for the same invariant (and the only one the node test harness can exercise).
  var BACKGROUND_REGION_IDS = ['c2-header', 'c2-stream', 'c2-rail', 'c2-board', 'c2-board-detail', 'c2-footer'];

  function setBackgroundInert(makeInert) {
    BACKGROUND_REGION_IDS.forEach(function (id) {
      var node = document.getElementById(id);
      if (!node) return;
      if (makeInert) node.setAttribute('inert', ''); else node.removeAttribute('inert');
    });
  }

  // The overlay is chrome, not data: built once and only ever toggled by a CSS class, never rebuilt
  // by a redraw. Opening it moves focus to its Close button and makes the background inert; closing
  // it restores the background and returns focus to wherever it was invoked FROM (R4) - the header
  // `?` button only when that is genuinely where focus was (e.g. a click on it), never as a blanket
  // default that loses the operator's actual place (a focused Later button, say).
  function setOverlayOpen(open) {
    nav.overlayOpen = open;
    if (chrome.overlay) chrome.overlay.className = 'c2-overlay' + (open ? ' is-open' : '');
    if (chrome.keysBtn) setPressed(chrome.keysBtn, open);
    if (open) {
      // Captured BEFORE the background goes inert: an inert ancestor forces its focused
      // descendant to blur, so this must run first or the invoker would already be lost.
      nav.overlayInvoker = document.activeElement;
      setBackgroundInert(true);
      var closeBtn = chrome.overlay && firstByClass(chrome.overlay, 'c2-overlay-close');
      if (closeBtn && typeof closeBtn.focus === 'function') closeBtn.focus();
    } else {
      setBackgroundInert(false);
      var invoker = nav.overlayInvoker;
      nav.overlayInvoker = null;
      var back = (invoker && invoker.isConnected && typeof invoker.focus === 'function') ? invoker : chrome.keysBtn;
      if (back && typeof back.focus === 'function') back.focus();
    }
  }

  // Every natively focusable control inside the overlay (not the stream's data-c2-focus convention -
  // this is a plain, self-contained dialog). Today that is only Close; written generally in case the
  // dialog ever grows a second control.
  function overlayFocusables() {
    if (!chrome.overlay) return [];
    return descendants(chrome.overlay, []).filter(function (n) {
      if (n.disabled) return false;
      var tag = n.tagName;
      return tag === 'BUTTON' || tag === 'A' || tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA';
    });
  }

  // R1: Tab/Shift+Tab never leave the dialog. Stepping past either end wraps to the other; if focus
  // is somehow already outside the dialog (defence in depth beyond `inert`), it is pulled back in.
  function trapOverlayTab(ev) {
    var items = overlayFocusables();
    if (!items.length) { stop(ev); return; }
    var first = items[0];
    var last = items[items.length - 1];
    var active = document.activeElement;
    var inside = chrome.overlay && chrome.overlay.contains(active);
    if (ev.shiftKey) {
      if (!inside || active === first) { stop(ev); last.focus(); }
    } else if (!inside || active === last) { stop(ev); first.focus(); }
  }

  var HINTS = [['t', 'theme'], ['j/k', 'select'], ['l', 'later'], ['?', 'keys']];

  function renderHints() {
    var hints = document.getElementById('c2-hints');
    if (!hints) return;
    clear(hints);
    // Only keys that work in this milestone are advertised.
    HINTS.forEach(function (h, i) {
      if (i > 0) hints.appendChild(document.createTextNode(' · '));
      hints.appendChild(el('span', 'c2-hint-key', h[0]));
      hints.appendChild(document.createTextNode(h[1]));
    });
  }

  // ----------------------------------------------------------------- stream

  // "Later" and "Wait 10 min" are local to this browser: the server has no defer path. A card
  // put off with Later stays open and counted (the deferred line, with a way back); nothing here
  // ever dismisses anything. Maps are keyed by team, then card id, and have no prototype so an id
  // such as "__proto__" from a feed is just a key.
  var LATER_KEY = 'agenttalk.console2.later';
  var LATER_MAX = 500;
  var LATER_KEEP_MS = 30 * 24 * 3600e3;
  var SNOOZE_MS = M.LIMITS.SNOOZE_MS;
  var later = { deferred: Object.create(null), snoozed: Object.create(null) };

  function teamMap(table, key) {
    if (!table[key]) table[key] = Object.create(null);
    return table[key];
  }

  function cleanTable(raw, keepFrom) {
    var out = Object.create(null);
    var count = 0;
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return out;
    Object.keys(raw).forEach(function (team) {
      var inner = raw[team];
      if (!inner || typeof inner !== 'object' || Array.isArray(inner)) return;
      Object.keys(inner).forEach(function (id) {
        var v = inner[id];
        if (count >= LATER_MAX || typeof v !== 'number' || !isFinite(v) || v < keepFrom) return;
        teamMap(out, team)[id] = v;
        count += 1;
      });
    });
    return out;
  }

  function loadLater() {
    var parsed = null;
    try { parsed = JSON.parse(window.localStorage.getItem(LATER_KEY) || 'null'); } catch (e) { parsed = null; }
    var now = Date.now();
    later.deferred = cleanTable(parsed && parsed.deferred, now - LATER_KEEP_MS);
    later.snoozed = cleanTable(parsed && parsed.snoozed, now - LATER_KEEP_MS);
  }

  function saveLater() {
    try { window.localStorage.setItem(LATER_KEY, JSON.stringify(later)); } catch (e) { /* kept for this page only */ }
  }

  function uiFor(team) {
    return { deferred: later.deferred[team] || Object.create(null), snoozedUntil: later.snoozed[team] || Object.create(null),
             answered: {}, lastVisitMs: ui.lastVisitMs };
  }

  function deferCard(team, id) {
    teamMap(later.deferred, team)[id] = nowMs();
    saveLater();
    renderAll();
  }

  function waitOnCard(team, id) {
    teamMap(later.snoozed, team)[id] = nowMs() + SNOOZE_MS;
    saveLater();
    renderAll();
  }

  function restoreDeferred(team) {
    later.deferred[team] = Object.create(null);
    saveLater();
    renderAll();
  }

  function unknownTeamBox(teams) {
    var box = el('section', 'c2-unknown');
    box.setAttribute('aria-label', 'Unknown team');
    var asked = String(rootParam);
    if (asked.length > PARAM_SHOW_MAX) asked = asked.slice(0, PARAM_SHOW_MAX - 1) + '…';
    box.appendChild(el('h1', 'c2-greeting', 'Unknown team.'));
    box.appendChild(el('p', 'c2-sub', 'No team matches “' + asked + '”. Nothing has been switched for you. Pick one:'));
    var list = el('div', 'c2-pick');
    teams.forEach(function (t) {
      var btn = el('button', 'c2-pick-btn', t.label);
      btn.setAttribute('type', 'button');
      btn.setAttribute('data-c2-focus', '|pick|' + t.index);
      on(btn, 'click', function () { pickTeam(t.index); });
      list.appendChild(btn);
    });
    box.appendChild(list);
    return box;
  }

  function banner(b) {
    var box = el('section', 'c2-banner is-' + b.kind);
    box.setAttribute('role', 'status');
    box.appendChild(el('div', 'c2-banner-kicker', b.kicker));
    box.appendChild(el('div', 'c2-banner-text', b.message));
    if (b.retryLabel) box.appendChild(el('div', 'c2-meta c2-banner-retried', b.retryLabel));
    if (b.canRetry) {
      var retry = el('button', 'c2-retry', 'Retry now');
      retry.setAttribute('type', 'button');
      retry.setAttribute('data-c2-focus', '|banner|retry');
      on(retry, 'click', retryNow);
      box.appendChild(retry);
    }
    return box;
  }

  // Two letters for the avatar ring ("LE" for the lead); images arrive with the rail work.
  function initials(text) {
    var letters = String(text || '').replace(/[^A-Za-z]/g, '');
    return (letters.slice(0, 2) || '?').toUpperCase();
  }

  // avatarNode is the ONLY place an <img src> is ever set. `file` only ever reaches here as the
  // return value of M.avatarFile (a pure function of a fixed motif list and a stable hash: never
  // raw agent text), and is checked again here against the model's own closed allowlist before
  // use, so an unexpected value renders no image rather than an unvetted URL.
  var AVATAR_FILE_SET = {};
  M.AVATAR_FILES.forEach(function (f) { AVATAR_FILE_SET[f] = true; });

  function avatarNode(file, runtime, cls) {
    var box = el('span', cls || 'c2-avatar');
    if (hasOwn(AVATAR_FILE_SET, file)) {
      var img = el('img', 'c2-avatar-img');
      img.setAttribute('alt', '');
      img.setAttribute('src', '/static/avatars/' + file);
      box.appendChild(img);
    }
    box.appendChild(el('span', 'c2-avatar-badge rt-' + (runtime || 'none'), RUNTIME_LETTER[runtime] || '?'));
    return box;
  }

  function hasOwn(obj, key) { return Object.prototype.hasOwnProperty.call(obj, key); }

  function leadBlock(lead) {
    var box = el('section', 'c2-lead');
    box.setAttribute('aria-label', 'Lead’s latest message');
    if (lead.body) {
      var row = el('div', 'c2-lead-row');
      var avatar = avatarNode(lead.avatarFile, lead.runtime, 'c2-avatar c2-lead-avatar');
      avatar.setAttribute('title', lead.name);   // 06-RULES: the full name is always in the tooltip
      row.appendChild(avatar);
      var col = el('div', 'c2-lead-col');
      col.appendChild(el('p', 'c2-lead-body', lead.body));
      col.appendChild(el('div', 'c2-meta', lead.short + (lead.ageLabel ? ' · ' + lead.ageLabel : '')));
      row.appendChild(col);
      box.appendChild(row);
    }
    // What the chat feed is doing to that message: failed read, not refreshed, lead unavailable.
    (lead.notes || []).forEach(function (n) {
      box.appendChild(el('div', 'c2-note tone-warn c2-lead-note is-' + n.kind, n.text));
    });
    return box;
  }

  // One option of a card. Locked ones are real disabled buttons that say why; the only live one
  // is "Wait 10 min" (a local snooze). Nothing here can answer the lead or change any state.
  function optionButton(option, team, card) {
    var text = option.label + (option.locked ? ' · ' + option.locked : '');
    var btn = el('button', 'c2-opt' + (option.primary ? ' is-primary' : '') + (option.locked ? ' is-locked' : ''), text);
    btn.setAttribute('type', 'button');
    btn.setAttribute('data-c2-focus', team + '|' + card.id + '|' + (option.action === 'wait' ? 'wait' : 'opt'));
    if (option.locked) {
      btn.disabled = true;
      btn.setAttribute('aria-disabled', 'true');
      btn.setAttribute('title', 'Not available here: ' + option.locked);
    } else if (option.action === 'wait') {
      on(btn, 'click', function () { waitOnCard(team, card.id); });
    } else {
      btn.disabled = true;   // an unlocked option without a local action is not something this slice does
      btn.setAttribute('aria-disabled', 'true');
      btn.setAttribute('title', 'Not available here: read-only');
    }
    return btn;
  }

  function needsCard(card, team, selected) {
    var box = el('article', 'c2-card tone-' + card.tone + (selected ? ' is-selected' : ''));
    box.setAttribute('data-c2-card', team + '|' + card.id);
    // R3: the card itself is focusable (not in the natural Tab order - j/k and restoreFocus put
    // focus here, never a stray Tab) and carries the same data-c2-focus convention as every other
    // stream control, so an unrelated redraw's captureFocus/restoreFocus keeps it exactly as it
    // would a button: what is highlighted (`is-selected`) is always what real focus is on, or inside.
    box.setAttribute('tabindex', '-1');
    box.setAttribute('data-c2-focus', team + '|' + card.id + '|card');
    var head = el('div', 'c2-card-head');
    head.appendChild(el('span', 'c2-kind', card.kind));
    head.appendChild(el('span', 'c2-age', card.ageLabel));
    box.appendChild(head);
    box.appendChild(el('h2', 'c2-card-title', card.title));
    var ev = el('div', 'c2-evidence');
    ev.appendChild(el('span', 'c2-label', 'EVIDENCE'));
    ev.appendChild(el('span', card.evidenceMissing ? 'c2-evidence-text is-missing' : 'c2-evidence-text', card.evidenceText));
    box.appendChild(ev);
    if (card.evidenceNote) box.appendChild(el('div', 'c2-note', card.evidenceNote));
    var actions = el('div', 'c2-actions');
    card.options.forEach(function (o) { actions.appendChild(optionButton(o, team, card)); });
    var laterBtn = el('button', 'c2-later', 'Later');
    laterBtn.setAttribute('type', 'button');
    laterBtn.setAttribute('data-c2-focus', team + '|' + card.id + '|later');
    laterBtn.setAttribute('title', 'Put this off in this browser. It stays open and counted.');
    on(laterBtn, 'click', function () { deferCard(team, card.id); });
    actions.appendChild(laterBtn);
    box.appendChild(actions);
    return box;
  }

  function deferredLine(count, team) {
    var btn = el('button', 'c2-deferred', count + ' deferred · still open, not dismissed · show');
    btn.setAttribute('type', 'button');
    btn.setAttribute('data-c2-focus', team + '|deferred|show');
    btn.setAttribute('title', 'Bring the deferred items back');
    on(btn, 'click', function () { restoreDeferred(team); });
    return btn;
  }

  function asideBlock(title, rows, more) {
    var box = el('section', 'c2-aside');
    box.appendChild(el('div', 'c2-label', title));
    rows.forEach(function (r) {
      var line = el('div', 'c2-aside-row');
      line.appendChild(el('span', 'c2-aside-title', r.title));
      line.appendChild(el('span', 'c2-aside-detail', r.detail));
      box.appendChild(line);
    });
    if (more > 0) box.appendChild(el('div', 'c2-meta', '+' + more + ' more'));
    return box;
  }

  var thread = { node: null, lastId: null };   // the chat thread element of the last draw, and its newest message

  function chatThread(chat) {
    var box = el('section', 'c2-chat');
    box.setAttribute('aria-label', 'Lead chat');
    box.appendChild(el('div', 'c2-label', 'LEAD CHAT'));
    var log = el('div', 'c2-thread');
    log.setAttribute('role', 'log');
    chat.messages.forEach(function (m) {
      var msg = el('div', 'c2-msg is-' + m.side);
      msg.appendChild(el('p', 'c2-bubble', m.body));
      msg.appendChild(el('div', 'c2-meta', m.short + (m.ageLabel ? ' \u00b7 ' + m.ageLabel : '')));
      log.appendChild(msg);
    });
    box.appendChild(log);
    return box;
  }

  // The composer is present but never live in this slice: disabled, with the reason beside it.
  function composerBox(composer) {
    var box = el('div', 'c2-composer');
    var row = el('div', 'c2-composer-row');
    var input = el('input', 'c2-composer-input');
    input.setAttribute('type', 'text');
    input.setAttribute('placeholder', composer.placeholder);
    input.setAttribute('aria-label', 'Message the lead');
    input.disabled = !composer.enabled;
    row.appendChild(input);
    var send = el('button', 'c2-send', 'Send');
    send.setAttribute('type', 'button');
    send.disabled = !composer.enabled;
    row.appendChild(send);
    box.appendChild(row);
    if (composer.reason) box.appendChild(el('div', 'c2-meta c2-composer-reason', composer.reason));
    return box;
  }

  // ---- in-place update of the stream
  //
  // A redraw builds the new stream off-document and then brings the live one up to date IN PLACE:
  // an element whose tag, classes, key and state are unchanged keeps being the same element (only
  // its text is updated). So a redraw that only moves an age label keeps keyboard focus, the
  // operator's scroll position and the chat thread exactly where they are; an element that really
  // changed is replaced, one that is gone is removed.
  var TRACKED_ATTRS = ['title', 'aria-disabled', 'aria-label', 'role', 'type', 'placeholder', 'data-c2-focus',
    'data-c2-card', 'src'];

  function kids(node) { return Array.prototype.slice.call(node.children); }

  function sameShape(a, b) {
    if (a.tagName !== b.tagName || a.className !== b.className) return false;
    if (!!a.disabled !== !!b.disabled) return false;
    for (var i = 0; i < TRACKED_ATTRS.length; i++) {
      if (a.getAttribute(TRACKED_ATTRS[i]) !== b.getAttribute(TRACKED_ATTRS[i])) return false;
    }
    return (a.children.length === 0) === (b.children.length === 0);
  }

  // N1: a retained node's own numeric CSSOM properties are not text and are not in TRACKED_ATTRS
  // (a style ATTRIBUTE stays completely banned everywhere), so a kept meter-fill never had its width
  // brought up to date - the bar could read stale through a same-tone increase, decrease or a quota
  // reset. `usageRow` is the only place `style.width` is ever set (mirroring `avatarNode` being the
  // only place `src` is set), so this is the one narrow, sanctioned place width is ever re-applied.
  var METER_FILL_CLASS = 'c2-meter-fill';

  function hasClass(node, cls) { return (' ' + node.className + ' ').indexOf(' ' + cls + ' ') >= 0; }

  function syncNode(live, fresh) {
    if (hasClass(fresh, METER_FILL_CLASS) && live.style.width !== fresh.style.width) {
      live.style.width = fresh.style.width;
    }
    if (fresh.children.length === 0) {
      if (live.textContent !== fresh.textContent) live.textContent = fresh.textContent;
      return;
    }
    syncChildren(live, fresh);
  }

  // Match the fresh children to the live ones in order. A block that appeared or vanished (a note
  // above the cards, say) must not push every later block out of alignment, so a live child is
  // looked for ahead of the cursor; in-between live children are removed, unmatched fresh ones inserted.
  // Controls only ever pair with a control of the same key, so a kept button still acts for its own card.
  function syncChildren(parent, fresh) {
    var live = kids(parent);
    var next = kids(fresh);
    var cursor = 0;
    var i;
    var j;
    for (i = 0; i < next.length; i++) {
      var found = -1;
      for (j = cursor; j < live.length; j++) {
        if (sameShape(live[j], next[i])) { found = j; break; }
      }
      if (found < 0) {
        if (cursor < live.length) parent.insertBefore(next[i], live[cursor]);
        else parent.appendChild(next[i]);
      } else {
        for (j = cursor; j < found; j++) parent.removeChild(live[j]);
        syncNode(live[found], next[i]);
        cursor = found + 1;
      }
    }
    for (j = live.length - 1; j >= cursor; j--) parent.removeChild(live[j]);
  }

  function descendants(node, out) {
    kids(node).forEach(function (c) { out.push(c); descendants(c, out); });
    return out;
  }

  function focusKeyOf(node) { return node.getAttribute('data-c2-focus'); }

  function roleOf(key) { return key.slice(key.lastIndexOf('|') + 1); }

  function focusables(main) {
    return descendants(main, []).filter(function (n) { return focusKeyOf(n) !== null && !n.disabled; });
  }

  // Which stream control has focus (if any), and where it sits among the controls of its kind.
  function captureFocus(main) {
    var a = document.activeElement;
    if (!a || a === main || !main.contains(a) || typeof a.getAttribute !== 'function') return null;
    var key = focusKeyOf(a);
    if (key === null) return null;
    var role = roleOf(key);
    var same = focusables(main).filter(function (n) { return roleOf(focusKeyOf(n)) === role; });
    return { key: key, role: role, index: Math.max(0, same.indexOf(a)) };
  }

  // Keep focus in the stream. The same control is normally still there (kept in place, above).
  // If it was replaced or its card is gone: the same control by key, else the one now at its
  // position among controls of its kind (the next card's Later), else the deferred line (where
  // a deferred card went), else the first Later, else the stream itself. Never the page.
  function restoreFocus(main, captured) {
    if (!captured) return;
    var a = document.activeElement;
    if (a && a !== main && main.contains(a)) return;
    var all = focusables(main);
    var byKey = all.filter(function (n) { return focusKeyOf(n) === captured.key; })[0];
    var same = all.filter(function (n) { return roleOf(focusKeyOf(n)) === captured.role; });
    var pick = byKey
      || (same.length ? same[Math.min(captured.index, same.length - 1)] : null)
      || all.filter(function (n) { return roleOf(focusKeyOf(n)) === 'show'; })[0]
      || all.filter(function (n) { return roleOf(focusKeyOf(n)) === 'later'; })[0]
      || main;
    if (typeof pick.focus === 'function') pick.focus();
  }

  function firstByClass(node, cls) {
    return descendants(node, []).filter(function (n) { return (' ' + n.className + ' ').indexOf(' ' + cls + ' ') >= 0; })[0] || null;
  }

  function buildStream(shell) {
    var out = el('div', 'c2-stream-body');
    if (shell.selection.status === 'unknown' && shell.teams.length) {
      streamCardTeam = null;
      streamCardIds = [];
      out.appendChild(unknownTeamBox(shell.teams));
      return { node: out, chat: null };
    }
    var v = shell.view;
    if (!v) {
      streamCardTeam = null;
      streamCardIds = [];
      // No snapshot yet: either still loading or the very first read failed.
      if (!data.conn.reachable) out.appendChild(banner(M.freshness({}, data.conn, nowMs()).banner));
      else out.appendChild(el('p', 'c2-sub', 'Waiting for the first snapshot.'));
      return { node: out, chat: null };
    }
    var team = v.key;
    // M4b: the selection is by id, never by DOM position; it is dropped when the visible team
    // changes, or when its card is simply no longer among the open ones (answered, expired, or
    // deferred by a means other than the keyboard, e.g. a click).
    streamCardTeam = team;
    streamCardIds = v.needs.open.map(function (c) { return c.id; });
    if (nav.selectedTeam !== team) {
      nav.selectedTeam = team;
      nav.selectedId = null;
    } else if (nav.selectedId !== null && streamCardIds.indexOf(nav.selectedId) === -1) {
      nav.selectedId = null;
    }
    if (v.banner) out.appendChild(banner(v.banner));
    if (v.greeting.text) out.appendChild(el('h1', 'c2-greeting', v.greeting.text));
    if (v.greeting.sub) out.appendChild(el('p', 'c2-sub', v.greeting.sub));
    if (v.lead) out.appendChild(leadBlock(v.lead));
    v.needs.open.forEach(function (card) {
      out.appendChild(needsCard(card, team, nav.selectedId === card.id));
    });
    if (v.needs.deferredCount > 0) out.appendChild(deferredLine(v.needs.deferredCount, team));
    if (v.aside.rows.length) out.appendChild(asideBlock(v.aside.title, v.aside.rows, v.aside.more));
    if (v.since && v.since.rows.length) out.appendChild(asideBlock(v.since.title, v.since.rows, 0));
    if (v.chat) out.appendChild(chatThread(v.chat));
    if (v.composer) out.appendChild(composerBox(v.composer));
    return { node: out, chat: v.chat };
  }

  function renderStream(shell) {
    var main = document.getElementById('c2-stream');
    if (!main) return;
    var captured = captureFocus(main);
    var built = buildStream(shell);
    syncChildren(main, built.node);
    // The thread is scrolled AFTER it is in the document (a detached element has no scroll
    // layout), and only when it is a new element or has a new newest message: otherwise it
    // stays wherever the operator left it.
    var log = built.chat ? firstByClass(main, 'c2-thread') : null;
    if (log) {
      if (log !== thread.node || thread.lastId !== built.chat.lastId) log.scrollTop = log.scrollHeight;
      thread.node = log;
      thread.lastId = built.chat.lastId;
    } else {
      thread.node = null;
      thread.lastId = null;
    }
    restoreFocus(main, captured);
  }

  // ------------------------------------------------------------------- rail

  function usageRow(u) {
    var row = el('div', 'c2-usage-row' + (u.noReading || u.stale ? ' is-stale' : ''));
    var head = el('div', 'c2-usage-head');
    head.appendChild(el('span', 'c2-rt rt-' + u.runtime, RUNTIME_LETTER[u.runtime] || '?'));
    head.appendChild(el('span', 'c2-usage-name', (RUNTIME_LABEL[u.runtime] || u.runtime) + ' · ' + u.windowLabel));
    head.appendChild(el('span', 'c2-usage-pct tone-' + (u.noReading ? 'dim' : u.tone), u.noReading ? 'no reading' : u.pct + '%'));
    row.appendChild(head);
    if (!u.noReading) {
      var meter = el('div', 'c2-meter');
      var fill = el('div', 'c2-meter-fill tone-' + u.tone);
      fill.style.width = u.barPct + '%';
      meter.appendChild(fill);
      row.appendChild(meter);
      var foot = [u.resetsLabel, u.stale ? u.asOfLabel : '', u.differs ? 'differs across agents' : ''].filter(Boolean).join(' · ');
      if (foot) row.appendChild(el('div', 'c2-meta', foot));
    }
    return row;
  }

  function rosterRow(r) {
    var row = el('div', 'c2-agent st-' + r.state);
    row.setAttribute('title', r.title);
    row.appendChild(avatarNode(r.avatarFile, r.runtime));
    var text = el('div', 'c2-agent-text');
    text.appendChild(el('div', 'c2-agent-name', r.short));
    text.appendChild(el('div', 'c2-agent-line tone-' + r.tone, r.line));
    row.appendChild(text);
    if (r.cap) row.appendChild(el('span', 'c2-agent-cap tone-bad', r.cap));
    return row;
  }

  // F2: built off-document, then reconciled into the live rail with the same syncChildren the
  // stream uses (M3 F1/F2), so an avatar <img> is not torn down and re-fetched merely because an
  // age label elsewhere in the rail changed. `src` is in TRACKED_ATTRS, so two different avatar
  // files at the same tree position are correctly seen as different nodes, not silently kept.
  function buildRail(shell) {
    var out = el('div', 'c2-rail-body');
    var v = shell.view;
    if (!v || v.mode === 'error' || v.mode === 'loading') return out;
    if (v.usage.length) {
      var usage = el('section', 'c2-usage');
      usage.appendChild(el('div', 'c2-label', 'USAGE WINDOWS'));
      v.usage.forEach(function (u) { usage.appendChild(usageRow(u)); });
      out.appendChild(usage);
    }
    var team = el('section', 'c2-roster');
    var head = el('div', 'c2-roster-head');
    head.appendChild(el('span', 'c2-label', 'TEAM · ' + v.roster.total));
    head.appendChild(el('span', 'c2-meta', v.roster.summary));
    team.appendChild(head);
    v.roster.rows.forEach(function (r) { team.appendChild(rosterRow(r)); });
    out.appendChild(team);
    return out;
  }

  function renderRail(shell) {
    var rail = document.getElementById('c2-rail');
    if (!rail) return;
    syncChildren(rail, buildRail(shell));
  }

  // -------------------------------------------------------------- board (B8)

  var COLUMN_LABEL = {
    needs_you: 'NEEDS YOU', queued: 'QUEUED', building: 'BUILDING', fix_round: 'FIX ROUND',
    independent_review: 'INDEPENDENT REVIEW', ready: 'READY', done: 'DONE', unknown: 'UNKNOWN'
  };

  function columnLabel(col) { return COLUMN_LABEL[col] || 'UNKNOWN'; }

  function columnTone(col) {
    if (col === 'needs_you') return 'bad';
    if (col === 'ready' || col === 'done') return 'ok';
    if (col === 'fix_round' || col === 'unknown') return 'warn';
    return 'dim';
  }

  function vendorText(vendors) {
    return vendors.length ? vendors.map(function (v) { return v.recipient + ' (' + v.vendor + ')'; }).join(', ') : 'unverified';
  }

  function selectBoardCard(key) {
    boardNav.selectedKey = key;
    renderAll();
  }

  // F3 (PR #221): the stream's own Later/Wait state, by escalation id - the SAME fact
  // view.needs.open/deferredCards/answered already carry (each stream card keeps its own
  // `.escalation` identity, wired in B7), never re-derived here. Missing entirely means 'open'.
  function incidentPresentationMap(v) {
    var map = Object.create(null);
    function mark(list, state) {
      (Array.isArray(list) ? list : []).forEach(function (c) {
        if (c && c.escalation && typeof c.escalation.escalationId === 'string') map[c.escalation.escalationId] = state;
      });
    }
    mark(v.needs.open, 'open');
    mark(v.needs.deferredCards, 'deferred');
    mark(v.needs.answered, 'answered');
    return map;
  }

  // A NEEDS YOU badge is qualified the moment none of its incidents are still plainly open - the
  // placement itself never moves (section 6), only the label says a deferral/answer is why nothing
  // is currently plain "NEEDS YOU" about it.
  function needsYouQualifier(incidents) {
    if (!incidents.length || incidents.some(function (i) { return i.presentation === 'open'; })) return '';
    return incidents.some(function (i) { return i.presentation === 'deferred'; }) ? ' · DEFERRED' : ' · ANSWERED';
  }

  function incidentText(i) {
    return i.escalationId + ' (' + i.presentation + (i.linked ? '' : ', unlinked') + ')';
  }

  // F1/F2 (PR #221): the ONE reason, in priority order, the whole board is not currently trustworthy
  // - a failed read showing a retained payload outranks a merely-degraded one, which outranks a
  // stamped last-known response, which outranks a plain non-complete coverage status; an otherwise
  // complete, non-last-known, non-failed response can only still be untrustworthy because its own
  // valid_until has elapsed since it was generated.
  function staleReasonNote(summary) {
    if (summary.readFailed) return 'Board could not be refreshed; showing the last-known cards.';
    if (summary.lastKnown) return 'Board coverage last known (' + summary.coverageStatus + ').';
    if (summary.coverageStatus !== 'complete') return 'Board coverage: ' + summary.coverageStatus + '.';
    return 'Board data may be out of date; waiting for a fresh read.';
  }

  // One card: title/reason, seats, vendor-or-unverified, round-or-unknown, open/total obligations,
  // approximate ages and the reason that explains its placement - exactly the design's field list
  // (section 6), plus (F1) an explicit "LAST KNOWN" qualifier whenever the card is not currently
  // trustworthy, and (F3) the underlying column and each incident's own deferral/answer state.
  // Keyed by `data-c2-card`/`data-c2-focus` with the SAME "team|id"-shaped convention
  // syncChildren/captureFocus/restoreFocus already use for the stream (a fixed "board" pseudo-team),
  // so reconciliation, focus retention and scroll retention are the SAME mechanism, not a second one.
  function boardCardNode(c, selected) {
    var cls = 'c2-board-card' + (c.needsYou ? ' is-needs-you' : '') + (selected ? ' is-selected' : '')
      + (c.stale ? ' is-stale' : '');
    var card = el('article', cls);
    card.setAttribute('data-c2-card', 'board|' + c.key);
    card.setAttribute('data-c2-focus', 'board|' + c.key + '|card');
    card.setAttribute('tabindex', '0');
    card.setAttribute('role', 'button');
    card.setAttribute('aria-pressed', selected ? 'true' : 'false');
    var head = el('div', 'c2-board-card-head');
    head.appendChild(el('span', 'c2-board-card-title', c.title));
    var columnText = columnLabel(c.column) + (c.column === 'needs_you' ? needsYouQualifier(c.incidents) : '');
    // F1: a stale card's column tone is ALWAYS dim, never the ok/bad tone of its (possibly no
    // longer current) claim - a last-known READY must never read as live green.
    head.appendChild(el('span', 'c2-board-card-column tone-' + (c.stale ? 'dim' : columnTone(c.column)),
      (c.stale ? 'LAST KNOWN · ' : '') + columnText));
    card.appendChild(head);
    var meta = [
      c.seats.length ? c.seats.join(', ') : 'no seats recorded',
      vendorText(c.vendors),
      c.round === null ? 'round unknown' : 'round ' + c.round,
      c.obligationsOpen + '/' + c.obligationsTotal + ' open',
      c.firstDispatchAgeSeconds === null ? 'first dispatch unknown' : 'dispatched ' + M.fmtAge(c.firstDispatchAgeSeconds) + ' ago',
      c.lastWorkEventAgeSeconds === null ? 'last activity unknown' : 'active ' + M.fmtAge(c.lastWorkEventAgeSeconds) + ' ago'
    ];
    if (c.column !== c.underlyingColumn) meta.push('underlying: ' + columnLabel(c.underlyingColumn));
    card.appendChild(el('div', 'c2-board-card-meta', meta.join(' · ')));
    card.appendChild(el('div', 'c2-board-card-reason', c.reason));
    if (c.incidents.length) {
      card.appendChild(el('div', 'c2-board-card-incidents', 'Incidents: ' + c.incidents.map(incidentText).join(', ')));
    }
    on(card, 'click', function () { selectBoardCard(c.key); });
    return card;
  }

  // Resolves the selected team's board entry exactly once, shared by buildBoard and
  // buildBoardDetail (F1/F2's retained-last-known handling and the trustworthy computation must
  // live in exactly one place, not be re-derived slightly differently by each renderer).
  function boardViewFor(shell) {
    if (!boardEnabled) return { status: 'disabled' };
    if (shell.selection.status === 'unknown' && shell.teams.length) return { status: 'unknown-team', teams: shell.teams };
    var v = shell.view;
    if (!v) return { status: 'no-snapshot' };
    var entry = data.board[v.key];
    if (!entry) return { status: 'never-fetched', v: v };
    if (!entry.payload) return { status: 'never-succeeded', v: v };
    var summary = M2.boardSummary(entry.payload, {
      nowMs: nowMs(), incidents: v.needs.incidents, incidentPresentation: incidentPresentationMap(v),
      readFailed: entry.ok === false,
    });
    return { status: 'ok', v: v, summary: summary };
  }

  // The selected team's board feed, projected through the SHARED B7 needs-you incidents (never
  // recomputed here) into cards. A missing/unread feed, an unknown team or no snapshot yet all say
  // so in place of cards - never a quiet empty board that looks the same as "nothing to do".
  function buildBoard(shell) {
    var out = el('div', 'c2-board-body');
    var res = boardViewFor(shell);
    if (res.status === 'disabled' || res.status === 'no-snapshot') {
      boardNav.selectedTeam = null;
      boardCardKeys = [];
      out.appendChild(el('p', 'c2-sub', res.status === 'disabled' ? 'Board unavailable.' : 'Waiting for the first snapshot.'));
      return out;
    }
    if (res.status === 'unknown-team') {
      boardNav.selectedTeam = null;
      boardCardKeys = [];
      out.appendChild(unknownTeamBox(res.teams));
      return out;
    }
    if (res.status === 'never-fetched' || res.status === 'never-succeeded') {
      if (boardNav.selectedTeam !== res.v.key) { boardNav.selectedTeam = res.v.key; boardNav.selectedKey = null; }
      boardCardKeys = [];
      out.appendChild(el('p', 'c2-sub', res.status === 'never-fetched' ? 'Loading the board…' : 'Board could not be read.'));
      return out;
    }
    var v = res.v;
    var summary = res.summary;
    if (boardNav.selectedTeam !== v.key) { boardNav.selectedTeam = v.key; boardNav.selectedKey = null; }
    // F2: cards from a RETAINED (possibly failed-read) payload are still projected and keyed here,
    // so a prior selection surviving in boardCardKeys is never cleared merely because the last GET
    // failed - only when the card itself is truly no longer in the retained set.
    boardCardKeys = summary.cards.map(function (c) { return c.key; });
    if (boardNav.selectedKey !== null && boardCardKeys.indexOf(boardNav.selectedKey) === -1) boardNav.selectedKey = null;
    if (!summary.trustworthy) out.appendChild(el('p', 'c2-board-note', staleReasonNote(summary)));
    if (summary.errors.length) out.appendChild(el('p', 'c2-board-note', summary.errors[0]));
    // F5: "no work items" is said only for a complete, non-truncated, trustworthy, genuinely empty
    // board - overflow, degraded coverage or a read failure with nothing retained get their own,
    // honestly different wording instead of the same contradictory empty claim.
    if (!summary.cards.length) {
      out.appendChild(el('p', 'c2-sub', summary.knownEmpty
        ? 'No active or recently done work items.' : 'No cards can currently be shown.'));
    }
    summary.cards.forEach(function (c) { out.appendChild(boardCardNode(c, boardNav.selectedKey === c.key)); });
    if (summary.legacyOpenCount !== null || summary.legacyLowerBound > 0) {
      var legacyText = summary.legacyOpenCount !== null
        ? summary.legacyOpenCount + ' untagged open request(s)'
        : 'at least ' + summary.legacyLowerBound + ' untagged open request(s) (exact count unknown)';
      out.appendChild(el('p', 'c2-board-legacy', 'Legacy / untagged work: ' + legacyText));
    }
    if (summary.truncated) {
      out.appendChild(el('p', 'c2-board-note',
        'Board truncated' + (summary.omittedCount !== null ? ' (' + summary.omittedCount + ' omitted)' : '') + '.'));
    }
    return out;
  }

  function renderBoard(shell) {
    var main = document.getElementById('c2-board');
    if (!main) return;
    var captured = captureFocus(main);
    syncChildren(main, buildBoard(shell));
    restoreFocus(main, captured);
  }

  function detailRow(dl, term, value) {
    dl.appendChild(el('dt', '', term));
    dl.appendChild(el('dd', '', value));
  }

  // The selected card's own read-only detail: initially just evidence (and the rest of the design's
  // field list) - no document viewer, that is a later D-series slice. Re-resolving boardViewFor here
  // is cheap (<=100 small objects) and keeps this function independent of buildBoard's own state;
  // (F2) a card from a retained, failed-read payload is still found and shown, marked stale.
  function buildBoardDetail(shell) {
    var out = el('div', 'c2-board-detail-body');
    var res = boardViewFor(shell);
    var card = null;
    var summary = null;
    if (res.status === 'ok' && boardNav.selectedKey !== null) {
      summary = res.summary;
      card = summary.cards.filter(function (c) { return c.key === boardNav.selectedKey; })[0] || null;
    }
    if (!card) {
      out.appendChild(el('p', 'c2-sub', 'Select a card to see its evidence.'));
      return out;
    }
    out.appendChild(el('h2', 'c2-board-detail-title', card.title));
    out.appendChild(el('p', 'c2-board-detail-reason', card.reason));
    var fields = el('dl', 'c2-board-detail-fields');
    // F1: the same explicit last-known qualification and timestamp the card carries, spelled out
    // here too - never just a bare, ordinary-looking field list on a response that is not current.
    var asOfMs = summary.generatedAtMs;
    var asOf = asOfMs === null ? 'unknown' : M.fmtAge(Math.max(0, (nowMs() - asOfMs) / 1000)) + ' ago';
    detailRow(fields, 'Coverage', card.stale ? 'stale / last known (as of ' + asOf + ')' : 'current (as of ' + asOf + ')');
    detailRow(fields, 'Seats', card.seats.length ? card.seats.join(', ') : 'none recorded');
    detailRow(fields, 'Model vendor', vendorText(card.vendors));
    detailRow(fields, 'Round', card.round === null ? 'unknown' : String(card.round));
    detailRow(fields, 'Obligations', card.obligationsOpen + ' open of ' + card.obligationsTotal + ' total');
    detailRow(fields, 'First dispatch',
      card.firstDispatchAgeSeconds === null ? 'unknown' : M.fmtAge(card.firstDispatchAgeSeconds) + ' ago');
    detailRow(fields, 'Last activity',
      card.lastWorkEventAgeSeconds === null ? 'unknown' : M.fmtAge(card.lastWorkEventAgeSeconds) + ' ago');
    detailRow(fields, 'Checks', card.checks === null ? 'unknown' : card.checks);
    detailRow(fields, 'Findings', card.findings.status === 'unavailable' ? 'unavailable' : String(card.findings.count));
    detailRow(fields, 'Cost', card.cost === null ? 'unknown' : String(card.cost));
    detailRow(fields, 'Merge', card.merge);
    // F3: the underlying, un-overlaid placement stays inspectable even while needs-you is shown.
    detailRow(fields, 'Underlying column', columnLabel(card.underlyingColumn));
    out.appendChild(fields);
    if (card.incidents.length) {
      // F3: each incident on this item is its own row - two incidents are never merged into one
      // unqualified badge, and a deferred/answered one still names its own identity and state.
      out.appendChild(el('div', 'c2-label', 'INCIDENTS'));
      var incidents = el('ul', 'c2-board-detail-list');
      card.incidents.forEach(function (i) { incidents.appendChild(el('li', '', incidentText(i))); });
      out.appendChild(incidents);
    }
    if (card.issues.length) {
      out.appendChild(el('div', 'c2-label', 'ISSUES'));
      var issues = el('ul', 'c2-board-detail-list');
      card.issues.forEach(function (i) { issues.appendChild(el('li', '', i)); });
      out.appendChild(issues);
    }
    out.appendChild(el('div', 'c2-label', 'EVIDENCE'));
    if (card.evidence.length) {
      var list = el('ul', 'c2-board-detail-list');
      card.evidence.forEach(function (id) { list.appendChild(el('li', '', id)); });
      out.appendChild(list);
    } else {
      out.appendChild(el('p', 'c2-sub', 'No evidence recorded.'));
    }
    return out;
  }

  function renderBoardDetail(shell) {
    var aside = document.getElementById('c2-board-detail');
    if (!aside) return;
    var captured = captureFocus(aside);
    syncChildren(aside, buildBoardDetail(shell));
    restoreFocus(aside, captured);
  }

  // ------------------------------------------------------------------ draw

  // Raw second-resolution ages are in the view for the tests; what is drawn is their
  // formatted label, so only the label may trigger a redraw.
  function sigReplacer(key, value) {
    return key === 'ageSeconds' || key === 'progressAge' ? undefined : value;
  }

  // The stream's signature leaves out what only the rail draws.
  function railFreeReplacer(key, value) {
    return key === 'roster' || key === 'usage' ? undefined : sigReplacer(key, value);
  }

  // A deferral or snooze of a stuck card is about one incident. When the agent has verifiably
  // recovered (it is now in any state other than stuck or unknown, which keep the incident open),
  // the incident is over and the local Later/Wait for it is dropped, so the next stall of that
  // agent raises its own card. Not judged while the team is offline or unreadable.
  function pruneRecovered(v) {
    if (!v || v.banner || v.mode === 'error' || v.mode === 'loading') return false;
    var changed = false;
    [later.deferred, later.snoozed].forEach(function (table) {
      var map = table[v.key];
      if (!map) return;
      Object.keys(map).forEach(function (id) {
        if (id.indexOf('stuck:') !== 0) return;
        var name = id.slice('stuck:'.length);
        var row = v.roster.rows.filter(function (r) { return r.name === name; })[0];
        if (!row || (row.state !== 'stuck' && row.state !== 'unknown')) {
          delete map[id];
          changed = true;
        }
      });
    });
    if (changed) saveLater();
    return changed;
  }

  function buildShell() {
    return M.buildShellView({
      roots: currentRoots(), attentionByRoot: data.attention, chatByRoot: data.chat, param: rootParam,
      nowMs: nowMs(), generatedMs: data.generatedMs, conn: data.conn, ui: ui, uiFor: uiFor, canAct: false
    });
  }

  // B8: which of (stream, rail) vs (board, board-detail) is shown - the `hidden` attribute only
  // (never a style attribute), so the two pairs occupy the same grid cells (console2.css) and
  // switching is instant, with no layout it has to compute from scratch.
  function applyRouteVisibility(mode) {
    var showBoard = mode === 'board';
    ['c2-stream', 'c2-rail'].forEach(function (id) {
      var node = document.getElementById(id);
      if (node) node.hidden = showBoard;
    });
    ['c2-board', 'c2-board-detail'].forEach(function (id) {
      var node = document.getElementById(id);
      if (node) node.hidden = !showBoard;
    });
  }

  // The two static, server-authored route anchors (#board/#conversation) get a plain aria-current
  // toggle - never href, which every v2 script is banned from ever assigning (see the model/data
  // section below and its own tests).
  function syncRouteLinks(mode) {
    var nav = document.getElementById('c2-routes');
    if (!nav) return;
    kids(nav).forEach(function (a) {
      if (a.getAttribute('data-c2-route') === mode) a.setAttribute('aria-current', 'page');
      else a.removeAttribute('aria-current');
    });
  }

  function renderAll() {
    var shell = buildShell();
    if (pruneRecovered(shell.view)) shell = buildShell();
    syncTeamChips(shell.teams);
    var app = document.getElementById('app');
    if (app) app.className = shell.view && shell.view.stale ? 'is-stale' : '';
    // The reserved #review=<id> route has no UI yet (B8 scope): it renders exactly like
    // #conversation, but currentRoute() still parses the id without throwing or falling through
    // to something else, so a stray link to it is never a broken or blank page.
    var route = currentRoute();
    var displayMode = route.mode === 'review' ? 'conversation' : route.mode;
    applyRouteVisibility(displayMode);
    syncRouteLinks(displayMode);
    // Redraw a region only when what it shows has changed. The stream is then updated in place
    // (see syncChildren), so a redraw does not disturb focus, scrolling or selection.
    var v = shell.view;
    var streamSig = JSON.stringify([shell.selection, v, data.conn.reachable, data.snapshot === null,
      shell.teams.map(function (t) { return t.key + '|' + t.label; }),
      nav.selectedTeam, nav.selectedId], railFreeReplacer);
    var railSig = JSON.stringify(v ? [v.mode, v.roster, v.usage] : null, sigReplacer);
    if (streamSig !== lastSig) {
      lastSig = streamSig;
      renderStream(shell);
    }
    if (railSig !== lastRailSig) {
      lastRailSig = railSig;
      renderRail(shell);
    }
    if (boardEnabled) {
      // F1: `trustworthy` must be part of the signature, not just the payload object - a WHOLLY
      // UNCHANGED snapshot can still cross its own coverage.valid_until as elapsed real time makes
      // it so, and that alone must trigger a redraw (paint() already calls renderAll() every
      // second regardless of feed activity; this is what makes that redraw actually demote cards).
      var boardRes = boardViewFor(shell);
      var boardTrustworthy = boardRes.status === 'ok' ? boardRes.summary.trustworthy : null;
      var boardSig = JSON.stringify([
        displayMode, shell.selection, v && v.key, v && v.needs.incidents,
        v && data.board[v.key], boardNav.selectedTeam, boardNav.selectedKey, boardTrustworthy,
      ]);
      if (boardSig !== lastBoardSig) {
        lastBoardSig = boardSig;
        renderBoard(shell);
        renderBoardDetail(shell);
      }
    }
  }

  // ------------------------------------------------------------------- data

  // A bounded GET. The timeout also covers a request that never settles at all, and
  // one that ignores its abort signal, so a hung feed can never hold the page up.
  function getJson(url) {
    return new Promise(function (resolve, reject) {
      var ctl = typeof AbortController === 'function' ? new AbortController() : null;
      var settled = false;
      var timer = setTimeout(function () {
        if (settled) return;
        settled = true;
        if (ctl) { try { ctl.abort(); } catch (e) { /* nothing to abort */ } }
        reject(new Error('timeout'));
      }, REQUEST_TIMEOUT_MS);
      function finish(fn, value) {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        fn(value);
      }
      var init = { cache: 'no-store' };
      if (ctl) init.signal = ctl.signal;
      fetch(url, init).then(function (r) {
        if (!r.ok) throw new Error('http ' + r.status);
        return r.json();
      }).then(function (value) { finish(resolve, value); }, function (err) { finish(reject, err); });
    });
  }

  function rootUrl(path, id) {
    return id ? path + '?root=' + encodeURIComponent(id) : path;
  }

  function ingestState(payload) {
    if (!payload || !Array.isArray(payload.roots)) throw new Error('bad state');
    data.conn.retriedAt = null;   // a good read: whatever retry was pending is resolved
    var gen = M.parseMs(payload.generated_at);
    if (gen === null) gen = Date.now();   // an old server without the field: fall back to the local clock
    // F1 (final sweep): an identical or regressed generated_at is a stalled feed, not a fresh
    // moment - re-anchoring the clock to it on every such response would freeze every age and
    // classification in place (ages never advance, a stuck agent's heartbeat never goes stale)
    // even while stalledPolls correctly counts the feed as stalled. The anchor and generatedMs only
    // ever move forward, on an ACTUAL new snapshot; detecting a stalled feed must not also rejuvenate
    // its timestamps.
    var advanced = data.generatedMs === null || gen > data.generatedMs;
    data.conn.stalledPolls = advanced ? 0 : data.conn.stalledPolls + 1;
    data.conn.reachable = true;
    data.snapshot = payload;
    if (advanced) {
      data.conn.lastOkMs = gen;
      data.generatedMs = gen;
      data.anchor = { epochMs: gen, perf: perfNow() };
    }
  }

  // A feed answer is used only when it names the team it was asked for.
  function answersFor(payload, id) {
    if (!payload || typeof payload !== 'object') return false;
    if (!id) return true;
    return payload.target_root_project_id === id;
  }

  // One request per feed at a time: a slow answer is waited for (up to the timeout), not
  // stacked up behind new requests.
  function guarded(key, run) {
    if (inflight[key]) return Promise.resolve();
    inflight[key] = true;
    function release() { delete inflight[key]; }
    return run().then(release, release);
  }

  // A failed or unusable read never erases what was last known: the items stay, marked
  // as failed (and how old they are), until a good read replaces them.
  function attentionFailed(id) {
    var prior = data.attention[id];
    data.attention[id] = { ok: false, asOfMs: prior ? prior.asOfMs : null, items: prior ? prior.items : [] };
  }

  function chatFailed(id) {
    var prior = data.chat[id];
    data.chat[id] = { ok: false, asOfMs: prior ? prior.asOfMs : null, payload: prior ? prior.payload : null };
  }

  function boardFailed(id) {
    var prior = data.board[id];
    data.board[id] = { ok: false, payload: prior ? prior.payload : null };
  }

  // B8: only fetched while the board is actually the visible route - the stream/rail feeds already
  // poll unconditionally, but a board the operator is not looking at need not be kept warm.
  function fetchBoard(id) {
    return guarded('board:' + id, function () {
      return getJson(rootUrl('/api/work-board', id)).then(function (payload) {
        if (!answersFor(payload, id)) boardFailed(id);
        else data.board[id] = { ok: true, payload: payload };
      }, function () {
        boardFailed(id);
      }).then(renderAll);
    });
  }

  function fetchAttention(id) {
    return guarded('att:' + id, function () {
      return getJson(rootUrl('/api/attention', id)).then(function (payload) {
        var bad = !answersFor(payload, id) || (Array.isArray(payload.errors) && payload.errors.length > 0);
        if (bad) attentionFailed(id);
        else data.attention[id] = { ok: true, asOfMs: nowMs(), items: Array.isArray(payload.items) ? payload.items : [] };
      }, function () {
        attentionFailed(id);
      }).then(renderAll);
    });
  }

  function fetchChat(id) {
    return guarded('chat:' + id, function () {
      return getJson(rootUrl('/api/lead-chat', id)).then(function (payload) {
        if (!answersFor(payload, id)) chatFailed(id);
        else data.chat[id] = { ok: true, asOfMs: nowMs(), payload: payload };
      }, function () {
        chatFailed(id);
      }).then(renderAll);
    });
  }

  // Feeds for the selected team every round; the other teams' attention (for their
  // needs badges) on the rounds that ask for it (the first, then every 5th). Each feed
  // runs on its own: nothing here is waited for by the state poll or by the redraw.
  //
  // F6 (PR #221): the board is NOT one of these - it has its own 5 s, hidden-tab-aware cadence
  // (boardLoop below), never the stream's unconditional 2 s one.
  function pollFeeds(withOthers) {
    var roots = currentRoots();
    if (!roots) return Promise.resolve();
    var sel = M.resolveRoot(roots, rootParam);
    var jobs = [];
    roots.forEach(function (r, i) {
      var id = rootKey(r);
      if (i === sel.index) {
        jobs.push(fetchAttention(id), fetchChat(id));
      } else if (withOthers && (!r || !Array.isArray(r.errors) || !r.errors.length)) {
        jobs.push(fetchAttention(id));
      }
    });
    return Promise.all(jobs);
  }

  // One round: read the state, draw it at once, then start the feeds without waiting
  // for them. A feed that hangs delays neither the next state read nor any redraw.
  //
  // F1: the 2 s cadence (loop) and a manual Retry both call this SAME function, which is why a
  // single `statePending` gate here is enough to make "at most one /api/state request ever
  // outstanding" hold for both call sites at once - 20 Retry clicks while one is pending issue
  // zero extra requests, they do not race, and a stale completion can never land after a newer
  // one (there is never a newer one to race against). `stateSeq` is kept as a second, independent
  // check: it can only ever disagree if that invariant is ever broken by a future change, at
  // which point it discards the out-of-order result instead of applying it.
  function pollOnce() {
    if (data.statePending) return Promise.resolve();
    data.statePending = true;
    var seq = ++data.stateSeq;
    var withOthers = data.cycle % OTHER_ATTENTION_EVERY === 0;
    data.cycle += 1;
    return getJson('/api/state').then(function (payload) {
      ingestState(payload);
      return true;
    }).catch(function () {
      data.conn.reachable = false;   // keep the last good data, greyed and stamped
      return false;
    }).then(function (ok) {
      data.statePending = false;
      if (seq !== data.stateSeq) return;   // superseded: never apply an out-of-order completion
      renderAll();
      if (ok) pollFeeds(withOthers);
    });
  }

  // A manual retry: stamp the attempt time (in server-anchored time, so it prints correctly
  // even while unreachable) and poll immediately, without waiting for the next 2 s tick. A click
  // while a request (loop's or an earlier click's) is already outstanding is coalesced: it neither
  // restamps the attempt nor issues a second request.
  function retryNow() {
    if (data.statePending) return;
    data.conn.retriedAt = nowMs();
    pollOnce();
  }

  function loop() {
    if (running) return;
    running = true;
    function done() {
      running = false;
      setTimeout(loop, POLL_MS);
    }
    pollOnce().then(done, done);
  }

  function pageHidden() {
    return typeof document.hidden === 'boolean' ? document.hidden : false;
  }

  // F6 (PR #221): the board's OWN cadence - 5 s while it is the visible route AND the document is
  // not hidden, never the stream's unconditional 2 s one, and paused (not merely skipped once)
  // while backgrounded: a hidden tab reschedules without ever issuing the GET at all.
  var boardPolling = false;

  // Only reschedules itself while the board is actually the visible route - conversation-only
  // usage (the common case) must never carry a perpetual background timer for a view no one is
  // looking at. Leaving #board (or never having entered it) lets the chain go idle; onHashChange
  // and onVisibilityChange are what start it again, exactly when it becomes relevant.
  function boardLoop() {
    if (boardPolling) return;
    boardPolling = true;
    var onBoard = boardEnabled && currentRoute().mode === 'board';
    function done(delayMs) {
      boardPolling = false;
      if (onBoard) setTimeout(boardLoop, typeof delayMs === 'number' ? delayMs : BOARD_POLL_MS);
    }
    if (!onBoard || pageHidden()) { done(); return; }
    var roots = currentRoots();
    // A fresh page load landing straight on #board can race the FIRST /api/state read (loop() and
    // boardLoop() both start synchronously in init, but only /api/state is awaited): retry soon,
    // not after the full 5 s cadence, so the board is not left showing "Loading..." for up to five
    // seconds after the snapshot it actually needed already arrived.
    if (!roots) { done(200); return; }
    var sel = M.resolveRoot(roots, rootParam);
    var r = roots[sel.index];
    if (!r) { done(); return; }
    fetchBoard(rootKey(r)).then(function () { done(); }, function () { done(); });
  }

  // Time keeps moving when nothing arrives: ages, the silent threshold and stale feeds
  // are recomputed on this clock even while every request is outstanding.
  function paint() {
    try { renderAll(); } finally { setTimeout(paint, PAINT_MS); }
  }

  // --------------------------------------------------------------- keyboard

  function isTypingTarget(target) {
    if (!target || !target.tagName) return false;
    var tag = String(target.tagName).toLowerCase();
    return tag === 'input' || tag === 'textarea' || tag === 'select' || target.isContentEditable === true;
  }

  function stop(ev) { if (typeof ev.preventDefault === 'function') ev.preventDefault(); }

  function findCard(main, id) {
    var key = streamCardTeam + '|' + id;
    return descendants(main, []).filter(function (n) {
      return n.tagName === 'ARTICLE' && n.getAttribute('data-c2-card') === key;
    })[0] || null;
  }

  // j/k move the selection by id among the currently open cards, clamped at either end (no wrap);
  // starting from nothing, j selects the first and k the last.
  function moveSelection(delta) {
    var ids = streamCardIds;
    if (!ids.length) { nav.selectedId = null; return; }
    nav.selectedTeam = streamCardTeam;
    var idx = nav.selectedId === null ? -1 : ids.indexOf(nav.selectedId);
    var next = idx === -1 ? (delta > 0 ? 0 : ids.length - 1) : Math.max(0, Math.min(ids.length - 1, idx + delta));
    nav.selectedId = ids[next];
    renderAll();
    // R3: focus moves WITH the selection, onto the card itself - never leaving it on a stale
    // control from an earlier Enter, which is exactly what let a later Enter act on the wrong card.
    var main = document.getElementById('c2-stream');
    var card = main && findCard(main, nav.selectedId);
    if (card && typeof card.focus === 'function') card.focus();
  }

  // Enter "opens" the selected card: focus moves to its first focusable action (an unlocked option,
  // else Later - `focusables` already skips disabled controls, so a card with only locked options
  // still lands on Later, never on a locked one; there is no key that can reach a locked option).
  function openSelected() {
    var main = document.getElementById('c2-stream');
    if (!main || nav.selectedId === null) return;
    var card = findCard(main, nav.selectedId);
    if (!card) return;
    var targets = focusables(card);
    if (targets.length && typeof targets[0].focus === 'function') targets[0].focus();
  }

  // l defers the selected card, exactly like clicking its Later button, and the selection moves to
  // whichever card now sits where it did (the next one, else the previous, else none left).
  function deferSelected() {
    if (nav.selectedId === null || nav.selectedTeam === null) return;
    var ids = streamCardIds;
    var idx = ids.indexOf(nav.selectedId);
    var team = nav.selectedTeam;
    var id = nav.selectedId;
    nav.selectedId = idx >= 0
      ? (ids[idx + 1] !== undefined ? ids[idx + 1] : (ids[idx - 1] !== undefined ? ids[idx - 1] : null))
      : null;
    deferCard(team, id);
    renderAll();
  }

  // "/" tries to focus the message box. It stays disabled in this read-only slice, and a disabled
  // control cannot take focus (browsers refuse it) - so this key is honestly inert, never an
  // enabled control the page cannot back.
  function focusComposer() {
    var main = document.getElementById('c2-stream');
    var input = main && firstByClass(main, 'c2-composer-input');
    if (input && typeof input.focus === 'function') input.focus();
  }

  // R2: the j/k/Enter/l selection shortcuts belong to the navigation context (nothing interactive
  // focused - typically the stream root or the page body after j/k, which never call .focus() on
  // anything themselves). A focused native control keeps its own Enter: a focused Later, theme,
  // team-chip or Retry button must activate itself, never be diverted back to "open the selection".
  function isInteractiveTarget(target) {
    if (!target || !target.tagName) return false;
    var tag = String(target.tagName).toLowerCase();
    return tag === 'button' || tag === 'a' || tag === 'input' || tag === 'select' || tag === 'textarea';
  }

  // B8: j/k on the board move the selection by KEY (work_item), never by DOM position, exactly
  // like the stream's moveSelection - and focus moves WITH it, onto the card itself, so a later
  // keypress can never land on a card that is no longer the highlighted one. Selecting a card also
  // shows its (read-only) detail immediately: there is no separate "open" step to announce.
  function findBoardCard(main, key) {
    var full = 'board|' + key;
    return descendants(main, []).filter(function (n) {
      return n.tagName === 'ARTICLE' && n.getAttribute('data-c2-card') === full;
    })[0] || null;
  }

  function moveBoardSelection(delta) {
    var keys = boardCardKeys;
    if (!keys.length) { boardNav.selectedKey = null; return; }
    var idx = boardNav.selectedKey === null ? -1 : keys.indexOf(boardNav.selectedKey);
    var next = idx === -1 ? (delta > 0 ? 0 : keys.length - 1) : Math.max(0, Math.min(keys.length - 1, idx + delta));
    boardNav.selectedKey = keys[next];
    renderAll();
    var main = document.getElementById('c2-board');
    var card = main && findBoardCard(main, boardNav.selectedKey);
    if (card && typeof card.focus === 'function') card.focus();
  }

  // F4: activation is tied to whichever card is ACTUALLY focused (native Tab order, not only
  // j/k's own selection), by reading its own data-c2-card key straight off the event target -
  // exactly what a real <button> would give for free, reproduced here for this ARIA one.
  function activateFocusedBoardCard(target) {
    if (!target || typeof target.getAttribute !== 'function') return;
    var full = target.getAttribute('data-c2-card');
    if (typeof full !== 'string' || full.indexOf('board|') !== 0) return;
    selectBoardCard(full.slice('board|'.length));
  }

  function onKey(ev) {
    if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
    if (isTypingTarget(ev.target)) return;
    if (nav.overlayOpen) {
      if (ev.key === 'Escape' || ev.key === '?') { stop(ev); setOverlayOpen(false); }
      else if (ev.key === 'Tab') { trapOverlayTab(ev); }
      return;   // the overlay is modal: every other key is native, e.g. Enter activating Close
    }
    if (ev.key === 't') { setTheme(M.nextTheme(theme)); return; }
    if (ev.key === '?') { stop(ev); setOverlayOpen(true); return; }
    if (ev.key === '1') { stop(ev); pickTeam(0); return; }
    if (ev.key === '2') { stop(ev); pickTeam(1); return; }
    if (boardEnabled && currentRoute().mode === 'board') {
      if (ev.key === 'j') { stop(ev); moveBoardSelection(1); return; }
      if (ev.key === 'k') { stop(ev); moveBoardSelection(-1); return; }
      // F4: a card is `role="button" tabindex="0"` on a plain <article> - a browser gives ONLY a
      // real <button> automatic Enter/Space activation, never an ARIA role by itself, so a card
      // reached by Tab (not just j/k) must be wired up here or it can never be activated at all.
      if (ev.key === 'Enter' || ev.key === ' ' || ev.key === 'Spacebar') {
        stop(ev);
        activateFocusedBoardCard(ev.target);
        return;
      }
      if (ev.key === 'Escape') { if (boardNav.selectedKey !== null) { boardNav.selectedKey = null; renderAll(); } return; }
      return;   // stream-only keys (l//) are inert on the read-only board
    }
    if (ev.key === 'j') { stop(ev); moveSelection(1); return; }
    if (ev.key === 'k') { stop(ev); moveSelection(-1); return; }
    if (ev.key === 'Enter') {
      if (nav.selectedId !== null && !isInteractiveTarget(ev.target)) { stop(ev); openSelected(); }
      return;
    }
    if (ev.key === 'l') { if (nav.selectedId !== null) { stop(ev); deferSelected(); } return; }
    if (ev.key === '/') { stop(ev); focusComposer(); return; }
    if (ev.key === 'Escape') { if (nav.selectedId !== null) { nav.selectedId = null; renderAll(); } return; }
  }

  // B8: navigating the hash (a click on #board/#conversation, Back/Forward, or a pasted link)
  // redraws immediately and, entering the board, (re)starts boardLoop right away rather than
  // waiting for a stray timer - boardLoop itself went idle the moment the board stopped being the
  // visible route (see boardLoop), so leaving and returning to #board needs an explicit restart.
  function onHashChange() {
    renderAll();
    if (boardEnabled && currentRoute().mode === 'board') boardLoop();
  }

  // F6: regaining visibility on the board route restarts polling right away - boardLoop's own 5 s
  // timer would otherwise leave a just-unhidden tab showing whatever was last known for up to
  // another five seconds (boardLoop itself kept rescheduling, just skipping the GET, while hidden).
  function onVisibilityChange() {
    if (pageHidden()) return;
    if (boardEnabled && currentRoute().mode === 'board') boardLoop();
  }

  // ------------------------------------------------------------------- init

  applyTheme(loadTheme());
  loadVisit();
  loadLater();
  buildHeader();
  renderHints();
  chrome.overlay = buildOverlay();
  var appRoot = document.getElementById('app');
  if (appRoot) appRoot.appendChild(chrome.overlay);
  var streamRoot = document.getElementById('c2-stream');
  if (streamRoot) streamRoot.setAttribute('tabindex', '-1');
  var boardRoot = document.getElementById('c2-board');
  if (boardRoot) boardRoot.setAttribute('tabindex', '-1');
  on(document, 'keydown', onKey);
  on(window, 'pagehide', saveVisit);
  on(window, 'hashchange', onHashChange);
  on(document, 'visibilitychange', onVisibilityChange);
  loop();
  if (boardEnabled && currentRoute().mode === 'board') boardLoop();
  setTimeout(paint, PAINT_MS);
}());
