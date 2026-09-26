/* =============================================================================
   agenttalk console v2 - console2-model.js

   PURE logic for the v2 console. No DOM, no fetch, no timers, no storage: every
   function takes plain values and returns plain values, so the same file runs in
   the browser (as window.AgentTalkConsole2Model) and under node for the tests
   (module.exports). console2.js renders what this file computes and derives
   nothing itself.

   M1 content: the theme list and the agent-name shortener (docs/STEP-CONSOLE-V2-
   PITCH.md, rule set from the design handoff 06-RULES). The view-model
   derivations (freshness, stuck vs busy, quiet, usage windows) land in M2.
   ============================================================================= */
(function (root, factory) {
  'use strict';
  var api = factory();
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api;
  } else {
    root.AgentTalkConsole2Model = api;
  }
}(typeof window !== 'undefined' ? window : this, function () {
  'use strict';

  // ------------------------------------------------------------------ themes
  //
  // The colour and font values live in console2.css as [data-theme] blocks; this
  // list only names them, in switch order. Midnight is the default and the only
  // theme reviewed in the pitch slice.
  var THEMES = ['midnight', 'paper', 'synthwave', 'terminal'];
  var DEFAULT_THEME = 'midnight';

  // Return a known theme name, or the default for anything else (a stale or
  // hand-edited stored value must never reach the DOM attribute).
  function normalizeTheme(value) {
    return typeof value === 'string' && THEMES.indexOf(value) >= 0 ? value : DEFAULT_THEME;
  }

  function nextTheme(current) {
    var i = THEMES.indexOf(normalizeTheme(current));
    return THEMES[(i + 1) % THEMES.length];
  }

  // ------------------------------------------------------------ name shortener
  //
  // Pattern: <runtime>-<project>-<role>[-<n>], runtime in claude | codex | qwen.
  // The role is everything between the project and the optional trailing number,
  // hyphens included. Projects are matched against the known team list first, so a
  // project may itself contain hyphens; an unknown project is the first token.
  //   1. drop the runtime (the avatar badge shows it)
  //   2. drop the project when it is the team on screen; otherwise prefix "proj/"
  //   3. shorten each role word (developer -> dev ...), keep the number
  //   4. break a tie with the lowercase runtime letter, only when needed
  //   5. a name that does not fit the pattern is shown in full, cut at the END at
  //      17 characters plus an ellipsis (never in the middle)
  var ABBR = {
    developer: 'dev', reviewer: 'rev', tester: 'test', architect: 'arch',
    frontend: 'fe', backend: 'be', lead: 'lead'
  };
  var RUNTIME_LETTER = { claude: 'c', codex: 'x', qwen: 'q' };
  var FULL_NAME_LIMIT = 18;   // longer names are cut to 17 characters + "…"
  var ELLIPSIS = '…';
  var ID_RE = /^(claude|codex|qwen)-(.+)$/;
  var TAIL_RE = /^(.*?)(?:-(\d+))?$/;

  function hasOwn(obj, key) {
    return Object.prototype.hasOwnProperty.call(obj, key);
  }

  // Split a full agent id into { rt, proj, base } or null when it does not fit.
  function parse(id, knownProjects) {
    if (typeof id !== 'string') return null;
    var m = ID_RE.exec(id);
    if (!m) return null;
    var rest = m[2];
    var known = knownProjects || [];
    var proj = null;
    var tail;
    for (var i = 0; i < known.length; i++) {
      if (typeof known[i] === 'string' && known[i] && rest.indexOf(known[i] + '-') === 0) {
        proj = known[i];
        break;
      }
    }
    if (proj !== null) {
      tail = rest.slice(proj.length + 1);
    } else {
      var cut = rest.indexOf('-');
      if (cut < 0) return null;
      proj = rest.slice(0, cut);
      tail = rest.slice(cut + 1);
    }
    var n = TAIL_RE.exec(tail);
    if (!n || !n[1]) return null;
    var words = n[1].split('-');
    for (var w = 0; w < words.length; w++) {
      if (hasOwn(ABBR, words[w])) words[w] = ABBR[words[w]];
    }
    return { rt: m[1], proj: proj, base: words.join('-') + (n[2] ? '-' + n[2] : '') };
  }

  function cutFullName(id) {
    var s = String(id);
    return s.length > FULL_NAME_LIMIT ? s.slice(0, FULL_NAME_LIMIT - 1) + ELLIPSIS : s;
  }

  // shortName(id, currentProject, teamIds, knownProjects)
  //   currentProject: the team on screen; '' or null means no single team (a
  //                   cross-team list), so every name keeps its project prefix
  //   teamIds:        every full agent id in the list being rendered (tie detection)
  //   knownProjects:  the known team/project names (may contain hyphens)
  function shortName(id, currentProject, teamIds, knownProjects) {
    var q = parse(id, knownProjects);
    if (!q) return cutFullName(id);
    var ids = teamIds || [];
    var s = q.base;
    var clash = false;
    for (var i = 0; i < ids.length; i++) {
      if (ids[i] === id) continue;
      var z = parse(ids[i], knownProjects);
      if (z && z.base === q.base && z.proj === q.proj) { clash = true; break; }
    }
    if (clash) s = RUNTIME_LETTER[q.rt] + '.' + s;
    if (!currentProject || q.proj !== currentProject) s = q.proj + '/' + s;
    return s;
  }

  // The project the roster belongs to: the known project that most ids start with,
  // else the most common first token, else ''. Ties resolve to the first seen.
  function teamProject(ids, knownProjects) {
    var counts = {};
    var order = [];
    var list = ids || [];
    for (var i = 0; i < list.length; i++) {
      var q = parse(list[i], knownProjects);
      if (!q) continue;
      if (!hasOwn(counts, q.proj)) { counts[q.proj] = 0; order.push(q.proj); }
      counts[q.proj] += 1;
    }
    var best = '';
    for (var k = 0; k < order.length; k++) {
      if (best === '' || counts[order[k]] > counts[best]) best = order[k];
    }
    return best;
  }

  return {
    THEMES: THEMES,
    DEFAULT_THEME: DEFAULT_THEME,
    normalizeTheme: normalizeTheme,
    nextTheme: nextTheme,
    parseAgentName: parse,
    shortName: shortName,
    teamProject: teamProject
  };
}));
