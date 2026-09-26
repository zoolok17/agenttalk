/* =============================================================================
   agenttalk console v2 - console2.js   (pitch slice, milestone M1)

   Renders the /v2 shell. Rules this file keeps (and tests/test_console2_web.py
   enforces on its source):
     - every node is built with createElement + textContent; anything that came
       from the bus or a feed is text, never markup;
     - no inline style: theme and layout come from console2.css, chosen through
       one data-theme attribute;
     - no link is built from data (the two links are static in the served shell);
     - read-only: this milestone only performs GET /api/state.

   M1 scope: header (brand, team chips from the roots list, theme choice, keys
   button), footer key hints, theme engine and its persistence, the `t` key. The
   stream and rail regions stay empty until M2 (data layer) and M3/M4.
   ============================================================================= */
(function () {
  'use strict';

  var M = window.AgentTalkConsole2Model;
  if (!M) return;   // the model script failed to load: leave the shell inert

  var THEME_KEY = 'agenttalk.console2.theme';
  var THEME_LABEL = { midnight: 'Midnight', paper: 'Paper', synthwave: 'Synthwave', terminal: 'Terminal' };

  var theme = M.DEFAULT_THEME;
  var roots = null;        // null = not loaded, [] = loaded and empty
  var loadFailed = false;
  var selectedRoot = 0;

  // ---------------------------------------------------------------- helpers

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function clear(node) { node.textContent = ''; }

  function on(node, type, fn) { node.addEventListener(type, fn); }

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

  // A choice the operator made: apply it, remember it, repaint the header.
  function setTheme(name) {
    applyTheme(name);
    try { window.localStorage.setItem(THEME_KEY, theme); } catch (e) { /* not persisted */ }
    renderHeader();
  }

  // ----------------------------------------------------------------- header

  function themeSegments() {
    var seg = el('div', 'c2-seg');
    seg.setAttribute('role', 'group');
    seg.setAttribute('aria-label', 'Theme');
    M.THEMES.forEach(function (name) {
      var btn = el('button', '', THEME_LABEL[name] || name);
      btn.setAttribute('type', 'button');
      btn.setAttribute('aria-pressed', name === theme ? 'true' : 'false');
      on(btn, 'click', function () { setTheme(name); });
      seg.appendChild(btn);
    });
    return seg;
  }

  function teamSwitcher() {
    var seg = el('div', 'c2-seg');
    seg.setAttribute('role', 'group');
    seg.setAttribute('aria-label', 'Team');
    if (roots === null || roots.length === 0) {
      // Either still loading or the feed gave no team: say only what is known.
      var idle = el('button', '', loadFailed ? 'No team data' : 'Team');
      idle.setAttribute('type', 'button');
      idle.setAttribute('aria-pressed', 'false');
      idle.disabled = true;
      seg.appendChild(idle);
      return seg;
    }
    roots.forEach(function (r, i) {
      var label = r && typeof r.label === 'string' && r.label ? r.label : 'Team ' + (i + 1);
      var chip = el('button', '', label);
      chip.setAttribute('type', 'button');
      chip.setAttribute('aria-pressed', i === selectedRoot ? 'true' : 'false');
      on(chip, 'click', function () { selectedRoot = i; renderHeader(); });
      seg.appendChild(chip);
    });
    return seg;
  }

  function renderHeader() {
    var bar = document.getElementById('c2-header');
    if (!bar) return;
    clear(bar);

    var brand = el('div', 'c2-brand');
    brand.appendChild(el('span', 'c2-logo'));
    brand.appendChild(el('span', 'c2-wordmark', 'agenttalk'));
    bar.appendChild(brand);

    bar.appendChild(teamSwitcher());
    bar.appendChild(el('div', 'c2-spacer'));
    bar.appendChild(themeSegments());

    // The keyboard overlay arrives with the keyboard milestone (M4): the button
    // is present and honestly disabled until then.
    var keys = el('button', 'c2-keybtn', '?');
    keys.setAttribute('type', 'button');
    keys.setAttribute('aria-label', 'Keyboard map (not available yet)');
    keys.setAttribute('title', 'Keyboard map: coming in a later step');
    keys.disabled = true;
    bar.appendChild(keys);
  }

  function renderHints() {
    var hints = document.getElementById('c2-hints');
    if (!hints) return;
    clear(hints);
    // Only keys that work in this milestone are advertised.
    hints.appendChild(el('span', 'c2-hint-key', 't'));
    hints.appendChild(document.createTextNode('theme'));
  }

  // ------------------------------------------------------------------- data

  // One-shot in M1: the team chips need the roots list, nothing else. The polling
  // data layer (freshness, retry, per-team feeds) is milestone M2.
  function loadRoots() {
    return fetch('/api/state', { cache: 'no-store' }).then(function (r) {
      if (!r.ok) throw new Error('state ' + r.status);
      return r.json();
    }).then(function (payload) {
      roots = payload && Array.isArray(payload.roots) ? payload.roots : [];
      loadFailed = false;
      renderHeader();
    }).catch(function () {
      roots = null;
      loadFailed = true;
      renderHeader();
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
  renderHeader();
  renderHints();
  on(document, 'keydown', onKey);
  loadRoots();
}());
