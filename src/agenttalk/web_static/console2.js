/* =============================================================================
   agenttalk console v2 - console2.js   (pitch slice, milestones M1 / M1b)

   Renders the /v2 shell. Rules this file keeps (and tests/test_console2_web.py
   enforces on its source):
     - every node is built with createElement + textContent; anything that came
       from the bus or a feed is text, never markup;
     - no inline style: theme and layout come from console2.css, chosen through
       one data-theme attribute;
     - no link is built from data (the two links are static in the served shell);
     - read-only: this milestone only performs GET /api/state.

   Header controls are built ONCE and updated in place, so keyboard focus stays
   on the control the operator just used (a redraw would drop it to the page).
   The one exception is a change in the SET of team chips, which rebuilds the
   chips and puts focus back on the chip with the same key.

   Team selection follows ?root= like the classic console: project_id first, then
   a unique label. A root that matches nothing is an explicit "unknown team" state
   with the teams to pick from; it is never replaced by the first team.
   ============================================================================= */
(function () {
  'use strict';

  var M = window.AgentTalkConsole2Model;
  if (!M) return;   // the model script failed to load: leave the shell inert

  var THEME_KEY = 'agenttalk.console2.theme';
  var THEME_LABEL = { midnight: 'Midnight', paper: 'Paper', synthwave: 'Synthwave', terminal: 'Terminal' };
  var PARAM_SHOW_MAX = 64;

  var theme = M.DEFAULT_THEME;
  var roots = null;        // null = not loaded, [] = loaded and empty
  var loadFailed = false;
  var rootParam = readRootParam();   // what ?root= asked for, then what the operator picked

  var ui = { teamSeg: null, themeButtons: [], chipKeys: [] };

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
    ui.themeButtons.forEach(function (entry) { setPressed(entry.button, entry.name === theme); });
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

  function selection() {
    return M.resolveRoot(roots, rootParam);
  }

  // The operator picked a team: remember it and keep the address bar in step, so a
  // reload or a shared link lands on the same team.
  function pickTeam(index) {
    var r = roots && roots[index];
    if (!r) return;
    rootParam = (typeof r.project_id === 'string' && r.project_id) ? r.project_id : (r.label || '');
    try {
      var params = new window.URLSearchParams(window.location.search || '');
      params.set('root', rootParam);
      window.history.replaceState({}, '', window.location.pathname + '?' + params.toString() +
        (window.location.hash || ''));
    } catch (e) { /* history unavailable: the choice still holds for this page */ }
    syncTeamChips();
    renderStream();
  }

  function chipItems() {
    var sel = selection();
    return (roots || []).map(function (r, i) {
      var id = r && typeof r.project_id === 'string' ? r.project_id : '';
      return {
        key: id || 'idx:' + i,
        label: r && typeof r.label === 'string' && r.label ? r.label : 'Team ' + (i + 1),
        pressed: sel.index === i,
        index: i
      };
    });
  }

  function focusedChipKey() {
    var active = document.activeElement;
    return active && typeof active.getAttribute === 'function' ? active.getAttribute('data-c2-key') : null;
  }

  function buildChip(item) {
    var chip = el('button', '', item.label);
    chip.setAttribute('type', 'button');
    chip.setAttribute('data-c2-key', item.key);
    setPressed(chip, item.pressed);
    on(chip, 'click', function () { pickTeam(item.index); });
    return chip;
  }

  // Keep the chips in place while the set of teams is unchanged; otherwise rebuild
  // them and give focus back to the chip that had it.
  function syncTeamChips() {
    var seg = ui.teamSeg;
    if (!seg) return;
    if (roots === null || roots.length === 0) {
      // Still loading, or the feed gave no team: say only what is known.
      var idle = el('button', '', loadFailed ? 'No team data' : 'Team');
      idle.setAttribute('type', 'button');
      setPressed(idle, false);
      idle.disabled = true;
      clear(seg);
      seg.appendChild(idle);
      ui.chipKeys = [];
      return;
    }
    var items = chipItems();
    var keys = items.map(function (it) { return it.key; });
    var same = keys.length === ui.chipKeys.length && keys.every(function (k, i) { return k === ui.chipKeys[i]; });
    if (same && seg.children.length === items.length) {
      items.forEach(function (it, i) {
        var chip = seg.children[i];
        if (chip.textContent !== it.label) chip.textContent = it.label;
        setPressed(chip, it.pressed);
      });
      return;
    }
    var hadFocus = focusedChipKey();
    clear(seg);
    var restore = null;
    items.forEach(function (it) {
      var chip = buildChip(it);
      seg.appendChild(chip);
      if (hadFocus !== null && it.key === hadFocus) restore = chip;
    });
    ui.chipKeys = keys;
    if (restore && typeof restore.focus === 'function') restore.focus();
  }

  // ----------------------------------------------------------------- header

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
    ui.teamSeg = teamSeg;

    bar.appendChild(el('div', 'c2-spacer'));

    var themeSeg = el('div', 'c2-seg');
    themeSeg.setAttribute('role', 'group');
    themeSeg.setAttribute('aria-label', 'Theme');
    ui.themeButtons = M.THEMES.map(function (name) {
      var btn = el('button', '', THEME_LABEL[name] || name);
      btn.setAttribute('type', 'button');
      on(btn, 'click', function () { setTheme(name); });
      themeSeg.appendChild(btn);
      return { name: name, button: btn };
    });
    bar.appendChild(themeSeg);

    // The keyboard overlay arrives with the keyboard milestone (M4): the button
    // is present and honestly disabled until then.
    var keys = el('button', 'c2-keybtn', '?');
    keys.setAttribute('type', 'button');
    keys.setAttribute('aria-label', 'Keyboard map (not available yet)');
    keys.setAttribute('title', 'Keyboard map: coming in a later step');
    keys.disabled = true;
    bar.appendChild(keys);

    syncThemeButtons();
    syncTeamChips();
  }

  function renderHints() {
    var hints = document.getElementById('c2-hints');
    if (!hints) return;
    clear(hints);
    // Only keys that work in this milestone are advertised.
    hints.appendChild(el('span', 'c2-hint-key', 't'));
    hints.appendChild(document.createTextNode('theme'));
  }

  // ----------------------------------------------------------------- stream

  // M1b: the stream only ever says one thing, and only when the selector matched
  // no team. Everything else in the stream arrives with the next milestones.
  function renderStream() {
    var main = document.getElementById('c2-stream');
    if (!main) return;
    clear(main);
    if (roots === null || roots.length === 0 || selection().status !== 'unknown') return;

    var box = el('section', 'c2-unknown');
    box.setAttribute('aria-label', 'Unknown team');
    var asked = String(rootParam);
    if (asked.length > PARAM_SHOW_MAX) asked = asked.slice(0, PARAM_SHOW_MAX - 1) + '…';
    box.appendChild(el('h1', 'c2-greeting', 'Unknown team.'));
    box.appendChild(el('p', 'c2-sub', 'No team matches “' + asked + '”. Nothing has been switched for you. Pick one:'));
    var list = el('div', 'c2-pick');
    chipItems().forEach(function (it) {
      var btn = el('button', 'c2-pick-btn', it.label);
      btn.setAttribute('type', 'button');
      on(btn, 'click', function () { pickTeam(it.index); });
      list.appendChild(btn);
    });
    box.appendChild(list);
    main.appendChild(box);
  }

  // ------------------------------------------------------------------- data

  // One-shot in M1/M1b: the team chips need the roots list, nothing else. The
  // polling data layer (freshness, retry, per-team feeds) is milestone M2.
  function loadRoots() {
    return fetch('/api/state', { cache: 'no-store' }).then(function (r) {
      if (!r.ok) throw new Error('state ' + r.status);
      return r.json();
    }).then(function (payload) {
      roots = payload && Array.isArray(payload.roots) ? payload.roots : [];
      loadFailed = false;
    }).catch(function () {
      roots = null;
      loadFailed = true;
    }).then(function () {
      syncTeamChips();
      renderStream();
    });
  }

  // --------------------------------------------------------------- keyboard

  function isTypingTarget(target) {
    if (!target || !target.tagName) return false;
    var tag = String(target.tagName).toLowerCase();
    return tag === 'input' || tag === 'textarea' || tag === 'select' || target.isContentEditable === true;
  }

  function onKey(ev) {
    if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
    if (isTypingTarget(ev.target)) return;
    if (ev.key === 't') setTheme(M.nextTheme(theme));
  }

  // ------------------------------------------------------------------- init

  applyTheme(loadTheme());
  buildHeader();
  renderHints();
  on(document, 'keydown', onKey);
  loadRoots();
}());
