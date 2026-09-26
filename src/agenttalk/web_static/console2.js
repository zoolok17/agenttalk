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

  var THEME_KEY = 'agenttalk.console2.theme';
  var VISIT_KEY = 'agenttalk.console2.lastvisit';
  var THEME_LABEL = { midnight: 'Midnight', paper: 'Paper', synthwave: 'Synthwave', terminal: 'Terminal' };
  var RUNTIME_LABEL = { claude: 'Claude', codex: 'Codex' };
  var RUNTIME_LETTER = { claude: 'C', codex: 'X', qwen: 'Q' };
  var POLL_MS = 2000;
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
    attention: {},           // project_id -> { ok, asOfMs, items }
    chat: {},                // project_id -> { ok, asOfMs, payload }
    cycle: 0
  };
  var ui = { deferred: {}, snoozedUntil: {}, answered: {}, lastVisitMs: null };
  var chrome = { teamSeg: null, themeButtons: [], chipKeys: [], chips: [] };
  var lastSig = null;
  var running = false;
  var inflight = {};       // feed key -> true while a request for it is outstanding

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

    // The keyboard overlay arrives with the keyboard milestone (M4): the button
    // is present and honestly disabled until then.
    var keys = el('button', 'c2-keybtn', '?');
    keys.setAttribute('type', 'button');
    keys.setAttribute('aria-label', 'Keyboard map (not available yet)');
    keys.setAttribute('title', 'Keyboard map: coming in a later step');
    keys.disabled = true;
    bar.appendChild(keys);

    syncThemeButtons();
    syncTeamChips([]);
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
      on(btn, 'click', function () { pickTeam(t.index); });
      list.appendChild(btn);
    });
    box.appendChild(list);
    return box;
  }

  function banner(b) {
    var box = el('section', 'c2-banner is-' + b.kind);
    box.appendChild(el('div', 'c2-banner-kicker', b.kicker));
    box.appendChild(el('div', 'c2-banner-text', b.message));
    return box;
  }

  function leadBlock(lead) {
    var box = el('section', 'c2-lead');
    box.setAttribute('aria-label', 'Lead’s latest message');
    if (lead.body) {
      box.appendChild(el('p', 'c2-lead-body', lead.body));
      box.appendChild(el('div', 'c2-meta', lead.short + (lead.ageLabel ? ' · ' + lead.ageLabel : '')));
    }
    // What the chat feed is doing to that message: failed read, not refreshed, lead unavailable.
    (lead.notes || []).forEach(function (n) {
      box.appendChild(el('div', 'c2-note tone-warn c2-lead-note is-' + n.kind, n.text));
    });
    return box;
  }

  function optionsRow(options) {
    var row = el('ul', 'c2-options');
    options.forEach(function (o) {
      var li = el('li', 'c2-option' + (o.primary ? ' is-primary' : '') + (o.locked ? ' is-locked' : ''),
        o.label + (o.locked ? ' · ' + o.locked : ''));
      row.appendChild(li);
    });
    return row;
  }

  // M2 draws a card as plain text so the data can be checked; M3 makes it a control.
  function needsCard(card) {
    var box = el('article', 'c2-card tone-' + card.tone);
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
    box.appendChild(optionsRow(card.options));
    return box;
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

  function renderStream(shell) {
    var main = document.getElementById('c2-stream');
    if (!main) return;
    clear(main);
    if (shell.selection.status === 'unknown' && shell.teams.length) {
      main.appendChild(unknownTeamBox(shell.teams));
      return;
    }
    var v = shell.view;
    if (!v) {
      // No snapshot yet: either still loading or the very first read failed.
      if (!data.conn.reachable) {
        main.appendChild(banner(M.freshness({}, data.conn, nowMs()).banner));
      } else {
        main.appendChild(el('p', 'c2-sub', 'Waiting for the first snapshot.'));
      }
      return;
    }
    if (v.banner) main.appendChild(banner(v.banner));
    if (v.greeting.text) main.appendChild(el('h1', 'c2-greeting', v.greeting.text));
    if (v.greeting.sub) main.appendChild(el('p', 'c2-sub', v.greeting.sub));
    if (v.lead) main.appendChild(leadBlock(v.lead));
    v.needs.open.forEach(function (card) { main.appendChild(needsCard(card)); });
    if (v.needs.deferredCount > 0) {
      main.appendChild(el('div', 'c2-deferred', v.needs.deferredCount + ' deferred · still open, not dismissed'));
    }
    if (v.aside.rows.length) main.appendChild(asideBlock(v.aside.title, v.aside.rows, v.aside.more));
    if (v.since && v.since.rows.length) main.appendChild(asideBlock(v.since.title, v.since.rows, 0));
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
    row.appendChild(el('span', 'c2-rt rt-' + (r.runtime || 'none'), RUNTIME_LETTER[r.runtime] || '?'));
    var text = el('div', 'c2-agent-text');
    text.appendChild(el('div', 'c2-agent-name', r.short));
    text.appendChild(el('div', 'c2-agent-line tone-' + r.tone, r.line));
    row.appendChild(text);
    if (r.cap) row.appendChild(el('span', 'c2-agent-cap tone-bad', r.cap));
    return row;
  }

  function renderRail(shell) {
    var rail = document.getElementById('c2-rail');
    if (!rail) return;
    clear(rail);
    var v = shell.view;
    if (!v || v.mode === 'error' || v.mode === 'loading') return;
    if (v.usage.length) {
      var usage = el('section', 'c2-usage');
      usage.appendChild(el('div', 'c2-label', 'USAGE WINDOWS'));
      v.usage.forEach(function (u) { usage.appendChild(usageRow(u)); });
      rail.appendChild(usage);
    }
    var team = el('section', 'c2-roster');
    var head = el('div', 'c2-roster-head');
    head.appendChild(el('span', 'c2-label', 'TEAM · ' + v.roster.total));
    head.appendChild(el('span', 'c2-meta', v.roster.summary));
    team.appendChild(head);
    v.roster.rows.forEach(function (r) { team.appendChild(rosterRow(r)); });
    rail.appendChild(team);
  }

  // ------------------------------------------------------------------ draw

  // Raw second-resolution ages are in the view for the tests; what is drawn is their
  // formatted label, so only the label may trigger a redraw.
  function sigReplacer(key, value) {
    return key === 'ageSeconds' || key === 'progressAge' ? undefined : value;
  }

  function renderAll() {
    var shell = M.buildShellView({
      roots: currentRoots(), attentionByRoot: data.attention, chatByRoot: data.chat, param: rootParam,
      nowMs: nowMs(), generatedMs: data.generatedMs, conn: data.conn, ui: ui
    });
    syncTeamChips(shell.teams);
    var app = document.getElementById('app');
    if (app) app.className = shell.view && shell.view.stale ? 'is-stale' : '';
    // Redraw the stream and rail only when what they show has changed (a redraw every
    // 2 s would reset scrolling and text selection for nothing).
    var sig = JSON.stringify([shell.selection, shell.view, data.conn.reachable, data.snapshot === null,
      shell.teams.map(function (t) { return t.key + '|' + t.label; })], sigReplacer);
    if (sig === lastSig) return;
    lastSig = sig;
    renderStream(shell);
    renderRail(shell);
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
    var gen = M.parseMs(payload.generated_at);
    if (gen === null) gen = Date.now();   // an old server without the field: fall back to the local clock
    var stalled = data.generatedMs !== null && gen <= data.generatedMs;
    data.conn.stalledPolls = stalled ? data.conn.stalledPolls + 1 : 0;
    data.conn.reachable = true;
    data.conn.lastOkMs = gen;
    data.snapshot = payload;
    data.generatedMs = gen;
    data.anchor = { epochMs: gen, perf: perfNow() };
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
  function pollOnce() {
    var withOthers = data.cycle % OTHER_ATTENTION_EVERY === 0;
    data.cycle += 1;
    return getJson('/api/state').then(function (payload) {
      ingestState(payload);
      return true;
    }).catch(function () {
      data.conn.reachable = false;   // keep the last good data, greyed and stamped
      return false;
    }).then(function (ok) {
      renderAll();
      if (ok) pollFeeds(withOthers);
    });
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

  function onKey(ev) {
    if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
    if (isTypingTarget(ev.target)) return;
    if (ev.key === 't') setTheme(M.nextTheme(theme));
  }

  // ------------------------------------------------------------------- init

  applyTheme(loadTheme());
  loadVisit();
  buildHeader();
  renderHints();
  on(document, 'keydown', onKey);
  on(window, 'pagehide', saveVisit);
  loop();
  setTimeout(paint, PAINT_MS);
}());
