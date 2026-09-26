/* =============================================================================
   agenttalk console v2 - console2-model.js

   PURE logic for the v2 console. No DOM, no fetch, no timers, no storage: every
   function takes plain values and returns plain values, so the same file runs in
   the browser (as window.AgentTalkConsole2Model) and under node for the tests
   (module.exports). console2.js renders what this file computes and derives
   nothing itself.

   Content: the theme list, the agent-name shortener (rule set from the design
   handoff 06-RULES), team selection, and the M2 view model: freshness with its two
   offline truths, stuck vs busy, queue order, greeting, quiet, roster lines and
   usage windows (docs/STEP-CONSOLE-V2-PITCH.md).
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
    var counts = Object.create(null);
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

  // ------------------------------------------------------------ team selection
  //
  // Resolve the ?root= parameter against the roots of /api/state, the way the
  // server resolves it (web.py _root_selection): an exact project_id first, then
  // a display label that is unique (the read-only legacy selector). Anything else
  // is UNKNOWN: the caller shows an explicit state and never switches silently.
  //   param '' / null / undefined  -> the first root (default, nothing was asked)
  //   returns { status: 'default' | 'match' | 'unknown', index }   (index -1 = none)
  function resolveRoot(roots, param) {
    var list = Array.isArray(roots) ? roots : [];
    if (param === '' || param === null || param === undefined) {
      return { status: 'default', index: list.length ? 0 : -1 };
    }
    if (typeof param !== 'string') return { status: 'unknown', index: -1 };
    var i;
    for (i = 0; i < list.length; i++) {
      if (list[i] && list[i].project_id === param) return { status: 'match', index: i };
    }
    var found = -1;
    var count = 0;
    for (i = 0; i < list.length; i++) {
      if (list[i] && list[i].label === param) { found = i; count += 1; }
    }
    if (count === 1) return { status: 'match', index: found };
    return { status: 'unknown', index: -1 };
  }

  // ================================================================ M2 view model
  //
  // Everything below is a pure function of its arguments. The feeds are the ones
  // the console already serves: /api/state (roots[].agents[], recent[]),
  // /api/attention (items[]) and /api/lead-chat. Nothing is invented: a value the
  // feeds do not carry is absent from the view, and the view says so where it
  // matters ("reply status unknown", "No evidence recorded", "no reading").

  var HEARTBEAT_FRESH_S = 300;   // a heartbeat this old or newer counts as fresh (= the health TTL)
  var SOURCE_STALE_S = 300;      // a team whose newest write is older than this is silent (> 5:00)
  var STUCK_AFTER_S = 600;       // no progress for this long makes a working agent a stuck candidate
  var RECENT_LIMIT = 25;         // /api/state keeps the 25 newest envelopes per root
  var STALLED_POLLS = 3;         // generated_at not advancing for more than this many polls
  var ATTENTION_FRESH_S = 8;     // an attention read older than four 2 s polls is stale
  var CHAT_FRESH_S = 8;          // same for the lead-chat read
  var LEAD_BODY_LIMIT = 1200;
  var ASIDE_MAX = 8;
  var USAGE_DIFFER_POINTS = 5;
  var DAY_S = 86400;

  var WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  var NUMBER_WORDS = ['Zero', 'One', 'Two', 'Three', 'Four', 'Five', 'Six', 'Seven', 'Eight', 'Nine', 'Ten'];

  // ---------------------------------------------------------------- time helpers

  function parseMs(value) {
    if (typeof value === 'number') return isFinite(value) ? value : null;
    if (typeof value !== 'string' || !value) return null;
    var t = Date.parse(value);
    return isNaN(t) ? null : t;
  }

  // Age in seconds of an ISO time (or epoch ms) at nowMs; null when unparseable.
  // Never negative: a timestamp slightly in the future reads as "just now".
  function ageSeconds(value, nowMs) {
    var t = parseMs(value);
    return t === null ? null : Math.max(0, (nowMs - t) / 1000);
  }

  function pad2(n) { return (n < 10 ? '0' : '') + n; }

  // Local wall parts of ms. tz 'utc' is for tests; anything else is the local zone.
  function parts(ms, tz) {
    var d = new Date(ms);
    if (tz === 'utc') {
      return { y: d.getUTCFullYear(), mo: d.getUTCMonth(), d: d.getUTCDate(), h: d.getUTCHours(),
               mi: d.getUTCMinutes(), wd: d.getUTCDay() };
    }
    return { y: d.getFullYear(), mo: d.getMonth(), d: d.getDate(), h: d.getHours(),
             mi: d.getMinutes(), wd: d.getDay() };
  }

  function clockHM(ms, tz) {
    var p = parts(ms, tz);
    return pad2(p.h) + ':' + pad2(p.mi);
  }

  function dayKey(p) { return p.y * 10000 + p.mo * 100 + p.d; }

  // "22:10" today, "yesterday 22:10", else "Mon 22:10".
  function whenLabel(ms, nowMs, tz) {
    var p = parts(ms, tz);
    var today = parts(nowMs, tz);
    var yest = parts(nowMs - DAY_S * 1000, tz);
    if (dayKey(p) === dayKey(today)) return clockHM(ms, tz);
    if (dayKey(p) === dayKey(yest)) return 'yesterday ' + clockHM(ms, tz);
    return WEEKDAYS[p.wd] + ' ' + clockHM(ms, tz);
  }

  // "23:40" when the reset is later today, else "Mon 23:40".
  function resetLabel(ms, nowMs, tz) {
    if (dayKey(parts(ms, tz)) === dayKey(parts(nowMs, tz))) return clockHM(ms, tz);
    return WEEKDAYS[parts(ms, tz).wd] + ' ' + clockHM(ms, tz);
  }

  // Short age: 40s, 14m, 5h, 2d.
  function fmtAge(seconds) {
    var s = Math.max(0, Math.floor(seconds));
    if (s < 60) return s + 's';
    var m = Math.floor(s / 60);
    if (m < 60) return m + 'm';
    var h = Math.floor(m / 60);
    if (h < 24) return h + 'h';
    return Math.floor(h / 24) + 'd';
  }

  // Long age for banners: 40s, 14m, 3h 12m, 2d 4h.
  function fmtAgeLong(seconds) {
    var s = Math.max(0, Math.floor(seconds));
    if (s < 60) return s + 's';
    var m = Math.floor(s / 60);
    if (m < 60) return m + 'm';
    var h = Math.floor(m / 60);
    if (h < 24) return h + 'h ' + (m % 60) + 'm';
    var d = Math.floor(h / 24);
    return d + 'd ' + (h % 24) + 'h';
  }

  function plural(n, one, many) { return n + ' ' + (n === 1 ? one : many); }

  function str(value, limit) {
    if (typeof value !== 'string') return '';
    return limit && value.length > limit ? value.slice(0, limit - 1) + ELLIPSIS : value;
  }

  function isObj(value) { return value !== null && typeof value === 'object' && !Array.isArray(value); }

  // ---------------------------------------------------------------- avatars
  //
  // The Midnight silhouette is the hexagon set already served from
  // /static/avatars. Only names from this fixed list are ever produced: the file
  // is chosen from the role or a stable hash of the agent name, never taken from
  // the feed.
  var HEX_MOTIFS = ['analyst', 'architect', 'builder', 'detective', 'devops', 'docs', 'monitor',
                    'sandbox', 'social', 'translator'];
  var ROLE_MOTIF = {
    lead: 'architect', architect: 'architect', arch: 'architect',
    developer: 'builder', dev: 'builder', frontend: 'builder', backend: 'builder', fe: 'builder', be: 'builder',
    reviewer: 'detective', rev: 'detective',
    tester: 'sandbox', test: 'sandbox', qa: 'sandbox',
    docs: 'docs', documentation: 'docs',
    devops: 'devops', infra: 'devops',
    monitor: 'monitor', analyst: 'analyst'
  };

  function nameHash(text) {
    var h = 5381;
    for (var i = 0; i < text.length; i++) h = ((h << 5) + h + text.charCodeAt(i)) >>> 0;
    return h;
  }

  function avatarFile(agentName, role) {
    var motif = null;
    var r = typeof role === 'string' ? role.toLowerCase() : '';
    if (r && hasOwn(ROLE_MOTIF, r)) motif = ROLE_MOTIF[r];
    if (motif === null) {
      var q = parse(agentName, []);
      var first = q ? q.base.split('-')[0] : '';
      if (first && hasOwn(ROLE_MOTIF, first)) motif = ROLE_MOTIF[first];
    }
    if (motif === null) motif = HEX_MOTIFS[nameHash(String(agentName)) % HEX_MOTIFS.length];
    return 'hexagon-' + motif + '.png';
  }

  function runtimeOf(agent) {
    var cli = agent && typeof agent.cli === 'string' ? agent.cli : '';
    if (hasOwn(RUNTIME_LETTER, cli)) return cli;
    var m = ID_RE.exec(agent && typeof agent.name === 'string' ? agent.name : '');
    return m ? m[1] : '';
  }

  // ---------------------------------------------------------- agents (roster rows)

  var DOWN_LABEL = {
    degraded_output: 'Degraded output',
    errored_poison: 'Errored (poisoned turn)',
    errored_ambiguous: 'Errored',
    crashed_or_exited: 'Crashed or exited'
  };
  // What a stale health read says its agent last reported (health.normalize's last_known_*
  // fields). Only these states are ever accepted; anything else is ignored as absent.
  var LAST_KNOWN_LABEL = {
    idle_waiting: 'idle', working_turn: 'working', working_silent: 'silent turn',
    stuck_suspected: 'wrapper flagged a stall', rate_limited_or_outage: 'rate limited or outage',
    degraded_output: 'degraded output', errored_poison: 'errored', errored_ambiguous: 'errored',
    crashed_or_exited: 'crashed or exited'
  };
  var LAST_KNOWN_WORKING = { working_turn: true, working_silent: true, stuck_suspected: true };
  var TONE = { working: 'ok', busy: 'info', idle: 'dim', stuck: 'warn', capped: 'bad', down: 'bad', unknown: 'dim' };

  // Did this agent send anything since it woke? Read from the recent-envelope window:
  //   replied: true (a message after `since` exists) | false (window covers it, none) | null (cannot tell)
  //   lastMessageAge: seconds since its newest message in the window, or null
  function replyInfo(name, sinceMs, recent, nowMs) {
    var list = Array.isArray(recent) ? recent : [];
    var lastAge = null;
    var after = false;
    var oldest = null;
    for (var i = 0; i < list.length; i++) {
      var e = list[i];
      if (!isObj(e)) continue;
      var t = parseMs(e.ts);
      if (t !== null && (oldest === null || t < oldest)) oldest = t;
      if (e.from === name && t !== null) {
        if (lastAge === null) lastAge = Math.max(0, (nowMs - t) / 1000);
        if (sinceMs !== null && t > sinceMs) after = true;
      }
    }
    var covers = list.length < RECENT_LIMIT || (oldest !== null && sinceMs !== null && oldest <= sinceMs);
    var replied = after ? true : (sinceMs !== null && covers ? false : null);
    return { replied: replied, lastMessageAge: lastAge };
  }

  function cappedLine(agent) {
    var cap = isObj(agent.capacity) ? agent.capacity : {};
    var p = isObj(cap.primary) ? cap.primary : null;
    var w = isObj(cap.secondary) ? cap.secondary : null;
    if (p && typeof p.used_pct === 'number' && p.used_pct >= 100) return { text: '5-hour window full', reset: p.resets_at };
    if (w && typeof w.used_pct === 'number' && w.used_pct >= 100) return { text: 'Weekly window full', reset: w.resets_at };
    var reset = p && typeof p.resets_at === 'number' ? p.resets_at : null;
    return { text: 'Rate limited or provider outage', reset: reset };
  }

  // One roster row plus the facts the needs and "also happening" blocks reuse.
  // ctx: { nowMs, generatedMs, recent, teamIds, project, known, tz }
  function agentView(agent, ctx) {
    var name = typeof agent.name === 'string' ? agent.name : '';
    var h = isObj(agent.health) ? agent.health : {};
    var hs = typeof h.state === 'string' ? h.state : 'unknown';
    if (h.stale === true) hs = 'unknown';
    var nowMs = ctx.nowMs;
    // A stale read (older than the TTL, or than the heartbeat) is `unknown` but may carry
    // what the snapshot last said. That is a memory, not a current state: it is only used
    // to judge stuck-vs-busy, with the staleness stated in the evidence.
    var lk = null;
    if (hs === 'unknown' && typeof h.last_known_state === 'string' && hasOwn(LAST_KNOWN_LABEL, h.last_known_state)) {
      var lkUpdated = parseMs(h.last_known_updated_at);
      var lkSince = parseMs(h.last_known_since);
      if (lkUpdated !== null && lkSince !== null) {
        lk = { state: h.last_known_state, sinceMs: lkSince, progressMs: parseMs(h.last_known_progress_at),
               updatedMs: lkUpdated, age: Math.max(0, (nowMs - lkUpdated) / 1000) };
      }
    }
    var staleWorking = lk !== null && LAST_KNOWN_WORKING[lk.state] === true;
    if (staleWorking) hs = lk.state;
    var short = shortName(name, ctx.project, ctx.teamIds, ctx.known);

    var hbAge = ageSeconds(agent.last_seen, nowMs);
    if (hbAge === null && typeof agent.last_seen_age_seconds === 'number' && ctx.generatedMs !== null) {
      hbAge = agent.last_seen_age_seconds + Math.max(0, (nowMs - ctx.generatedMs) / 1000);
    }
    var sinceMs = staleWorking ? lk.sinceMs : parseMs(h.since);
    var sinceAge = sinceMs === null ? null : Math.max(0, (nowMs - sinceMs) / 1000);
    // Inactivity counts from the CURRENT turn. The wrapper keeps last_progress_at across
    // idle and turn_start, so a value older than `since` (which is when this state began:
    // the turn start for a freshly spawned, still-silent turn) belongs to an earlier turn and
    // must not count. Progress noted inside the current state is used as it is.
    var lpMs = staleWorking ? lk.progressMs : parseMs(h.last_progress_at);
    var haveProgress = lpMs !== null && (sinceMs === null || lpMs >= sinceMs);
    var baselineMs = haveProgress ? lpMs : sinceMs;
    // A stale working_turn wrote a snapshot on every adapter event: its last write is the last
    // sign of activity, so that is where its silence began.
    if (staleWorking && lk.state === 'working_turn') baselineMs = lk.updatedMs;
    var progAge = baselineMs === null ? null : Math.max(0, (nowMs - baselineMs) / 1000);
    var hbFresh = hbAge !== null && hbAge <= HEARTBEAT_FRESH_S;

    var view = {
      name: name, short: short, runtime: runtimeOf(agent), avatarFile: avatarFile(name, agent.role),
      state: 'unknown', tone: 'dim', line: '', cap: '', title: name,
      candidate: false, stuck: null, aside: null, healthStale: lk !== null
    };

    function setState(state, line) { view.state = state; view.tone = TONE[state]; view.line = line; }

    if (hs === 'idle_waiting') {
      setState('idle', sinceAge === null ? 'Idle' : 'Idle · ' + fmtAge(sinceAge));
    } else if (hs === 'working_turn' || hs === 'working_silent' || hs === 'stuck_suspected') {
      var reply = replyInfo(name, sinceMs, ctx.recent, nowMs);
      var progWord;
      if (staleWorking) progWord = 'Health stale ' + fmtAge(lk.age) + ' (last known: ' + LAST_KNOWN_LABEL[lk.state] + ')';
      else if (haveProgress) progWord = 'Last progress ' + fmtAge(progAge) + ' ago';
      else if (hs === 'stuck_suspected') progWord = 'Wrapper flagged a stall' + (progAge === null ? '' : ' ' + fmtAge(progAge) + ' ago');
      else progWord = 'No progress since the turn began' + (progAge === null ? '' : ' ' + fmtAge(progAge) + ' ago');
      var noMsg = reply.lastMessageAge === null ? '' : ' · no message for ' + fmtAge(reply.lastMessageAge);
      if (!hbFresh) {
        setState('unknown', 'Heartbeat ' + (hbAge === null ? 'missing' : 'stale ' + fmtAge(hbAge)) +
          ' · not judged stuck');
      } else if (hs === 'working_turn' && !staleWorking) {
        var task = str(agent.task, 80);
        setState('working', 'Working' + (task ? ' · ' + task : '') + (sinceAge === null ? '' : ' · ' + fmtAge(sinceAge)));
      } else {
        // working_silent, or the wrapper's own stuck_suspected: a candidate when nothing
        // has moved for 10 minutes. A card needs the full evidence (see below).
        var quietLong = progAge !== null && progAge >= STUCK_AFTER_S;
        view.candidate = (hs === 'stuck_suspected' && !staleWorking) || quietLong;
        if (quietLong && reply.replied === false) {
          var evidence = progWord + ' · no reply sent · heartbeat still fresh';
          setState('stuck', evidence);
          view.stuck = { evidence: evidence, progressAge: progAge };
        } else if (view.candidate || staleWorking) {
          var tail = reply.replied === true ? 'replied since it woke'
            : (reply.replied === false ? 'no reply sent' : 'reply status unknown');
          setState('busy', progWord + ' · ' + tail);
          view.aside = { title: short + ' is quiet',
            detail: progWord + ' · ' + tail + (quietLong ? ' · no card without more evidence'
              : ' · no card before 10 min without activity') };
        } else {
          setState('busy', 'Quiet · ' + progWord.charAt(0).toLowerCase() + progWord.slice(1) + noMsg);
          view.aside = { title: short + ' is quiet, not stuck',
            detail: progWord + noMsg + ' · no card while progress moves' };
        }
      }
    } else if (lk !== null && lk.state === 'idle_waiting' && hbFresh) {
      // The health file is older than the heartbeat (the wrapper only writes health when a turn
      // starts, progresses or ends), but its last word was idle and nothing newer exists: a turn
      // starting would have written at once. The heartbeat says the wrapper is alive.
      setState('idle', 'Idle \u00b7 ' + fmtAge(Math.max(0, (nowMs - lk.sinceMs) / 1000)));
    } else if (hs === 'rate_limited_or_outage') {
      var c = cappedLine(agent);
      var resetMs = typeof c.reset === 'number' && isFinite(c.reset) ? c.reset * 1000 : null;
      var resetText = resetMs !== null && resetMs > nowMs ? 'resets ' + resetLabel(resetMs, nowMs, ctx.tz) : '';
      setState('capped', c.text);
      view.cap = resetText;
      view.aside = { title: short + ' is capped', detail: c.text + (resetText ? ' · ' + resetText : '') };
    } else if (hasOwn(DOWN_LABEL, hs)) {
      setState('down', DOWN_LABEL[hs] + (sinceAge === null ? '' : ' · ' + fmtAge(sinceAge)));
      view.aside = { title: short + ' is down', detail: view.line };
    } else {
      var remembered = lk ? 'last known: ' + LAST_KNOWN_LABEL[lk.state] + ' (' + fmtAge(lk.age) + ' ago) · ' : '';
      setState('unknown', 'No fresh health · ' + remembered + (hbAge === null ? 'never seen' : 'last seen ' + fmtAge(hbAge)));
    }
    return view;
  }

  // ------------------------------------------------------------------ usage windows

  var USAGE_WINDOWS = [
    { key: '5h', prop: 'primary', label: '5-hour' },
    { key: 'weekly', prop: 'secondary', label: 'Weekly' }
  ];

  function usageTone(pct) { return pct >= 85 ? 'bad' : (pct >= 60 ? 'warn' : 'ok'); }

  // Claude and Codex, 5-hour and weekly. Capacity is reported per agent; one account
  // per runtime is assumed, so per runtime and window the NEWEST reading wins. A
  // reading that is not fresh, or whose reset time has passed, is shown grey with
  // its "as of" time; a window nobody has reported reads "no reading", never 0 %.
  function usageRows(agents, nowMs, tz) {
    var rows = [];
    ['claude', 'codex'].forEach(function (rt) {
      var mine = (agents || []).filter(function (a) { return isObj(a) && runtimeOf(a) === rt; });
      if (!mine.length) return;
      USAGE_WINDOWS.forEach(function (win) {
        var cands = [];
        mine.forEach(function (a) {
          var cap = isObj(a.capacity) ? a.capacity : null;
          var w = cap && isObj(cap[win.prop]) ? cap[win.prop] : null;
          if (!w || typeof w.used_pct !== 'number' || !isFinite(w.used_pct)) return;
          var obs = parseMs(cap.observed_at);
          cands.push({ pct: w.used_pct, resetsAt: typeof w.resets_at === 'number' ? w.resets_at * 1000 : null,
                       obs: obs, fresh: cap.confidence === 'fresh' });
        });
        var row = { runtime: rt, window: win.key, windowLabel: win.label };
        if (!cands.length) {
          row.noReading = true;
          rows.push(row);
          return;
        }
        var best = cands[0];
        var lo = cands[0].pct;
        var hi = cands[0].pct;
        cands.forEach(function (c) {
          if ((c.obs === null ? -Infinity : c.obs) > (best.obs === null ? -Infinity : best.obs)) best = c;
          lo = Math.min(lo, c.pct);
          hi = Math.max(hi, c.pct);
        });
        var passed = best.resetsAt !== null && best.resetsAt <= nowMs;
        row.noReading = false;
        row.pct = Math.round(best.pct);
        row.barPct = Math.max(0, Math.min(100, row.pct));
        row.stale = !best.fresh || passed;
        row.tone = row.stale ? 'dim' : usageTone(best.pct);
        row.resetsLabel = best.resetsAt === null ? '' : (passed ? 'reset passed' : 'resets ' + resetLabel(best.resetsAt, nowMs, tz));
        row.asOfLabel = best.obs === null ? '' : 'as of ' + clockHM(best.obs, tz);
        row.differs = cands.length > 1 && (hi - lo) > USAGE_DIFFER_POINTS;
        rows.push(row);
      });
    });
    return rows;
  }

  // ------------------------------------------------------------- freshness, offline

  // The newest time any agent wrote anything for this root (heartbeat, health,
  // capacity reading) or the newest bus envelope; null when there is none.
  function sourceAsOf(root) {
    var newest = null;
    function see(value) {
      var t = parseMs(value);
      if (t !== null && (newest === null || t > newest)) newest = t;
    }
    (Array.isArray(root && root.agents) ? root.agents : []).forEach(function (a) {
      if (!isObj(a)) return;
      see(a.last_seen);
      if (isObj(a.health)) see(a.health.updated_at);
      if (isObj(a.capacity)) see(a.capacity.observed_at);
    });
    var recent = Array.isArray(root && root.recent) ? root.recent : [];
    if (recent.length && isObj(recent[0])) see(recent[0].ts);
    return newest;
  }

  // Two different truths, two banners.
  //   unreachable: this page cannot get a fresh snapshot from the console server
  //                (the last read failed, or generated_at stopped advancing)
  //   silent:      the server answers, but nothing has been written for over 5 minutes
  // conn: { reachable, stalledPolls, lastOkMs }
  function freshness(root, conn, nowMs, tz) {
    var asOfMs = sourceAsOf(root);
    var ageS = asOfMs === null ? null : Math.max(0, (nowMs - asOfMs) / 1000);
    var c = conn || {};
    var out = { state: 'live', sourceAsOfMs: asOfMs, ageSeconds: ageS,
                asOfLabel: asOfMs === null ? '' : clockHM(asOfMs, tz), banner: null };
    if (c.reachable === false || (typeof c.stalledPolls === 'number' && c.stalledPolls > STALLED_POLLS)) {
      out.state = 'unreachable';
      var last = typeof c.lastOkMs === 'number' ? c.lastOkMs : null;
      out.banner = {
        kind: 'unreachable',
        kicker: 'CAN’T REACH THE CONSOLE SERVER',
        message: last === null
          ? 'No snapshot has arrived yet. Nothing below is live.'
          : 'Last snapshot ' + clockHM(last, tz) + ' (' + fmtAgeLong((nowMs - last) / 1000) +
            ' ago). Everything below is greyed and stamped; nothing is live.'
      };
    } else if (ageS === null || ageS > SOURCE_STALE_S) {
      out.state = 'silent';
      out.banner = {
        kind: 'silent',
        kicker: 'NO AGENT HAS REPORTED FOR ' + (ageS === null ? 'A WHILE' : fmtAgeLong(ageS).toUpperCase()),
        message: (ageS === null ? 'No agent has written a heartbeat or health snapshot.'
          : 'The server answers, but the newest write was at ' + clockHM(asOfMs, tz) + '.') +
          ' Everything below is greyed and stamped; nothing is live.'
      };
    }
    return out;
  }

  // ----------------------------------------------------------------- needs you

  var STUCK_NOTE = 'Weaker evidence: process status isn’t visible yet, so Wait comes first.';

  // The options a card offers. Answering sends a message to the lead, which this slice does not
  // do (read-only): a served option is shown, disabled, with the reason. `canAct` is the seam for
  // the slice that will send (actions on, CSRF session, kill-switch clear).
  function cardOptions(item, canAct) {
    var labels = Array.isArray(item.options) ? item.options.filter(function (o) { return typeof o === 'string' && o; }) : [];
    if (item.answerable === true && labels.length) {
      return labels.map(function (label, i) {
        return { label: label, primary: i === 0, locked: canAct ? null : 'read-only' };
      });
    }
    // Options are only served with --enable-actions; otherwise the answer is a CLI step.
    return [{ label: 'Answer', primary: true, locked: 'CLI only' }];
  }

  // Map one /api/attention item to a card, or to an "also happening" row.
  function attentionCard(item, ctx) {
    var src = typeof item.source === 'string' ? item.source : 'other';
    var label = str(item.source_label, 40);
    var kind;
    var tone;
    if (src === 'escalation') { kind = 'DECISION'; tone = 'info'; }
    else if (src === 'gate') { kind = 'GATE HOLD'; tone = 'warn'; }
    else { kind = label || (src === 'other' ? 'OTHER' : src.toUpperCase()); tone = 'warn'; }
    var evidence = str(item.detail, 600);
    var age = typeof item.age_seconds === 'number' && !item.age_unknown
      ? item.age_seconds + Math.max(0, (ctx.nowMs - ctx.attentionAsOfMs) / 1000) : null;
    var agent = typeof item.agent === 'string' && item.agent ? shortName(item.agent, ctx.project, ctx.teamIds, ctx.known) : '';
    return {
      id: str(item.id, 200) || (src + ':' + str(item.title, 60)),
      source: src, kind: kind, tone: tone, severity: typeof item.severity === 'string' ? item.severity : '',
      title: str(item.title, 300) || 'Attention needed',
      evidence: evidence, evidenceText: evidence || 'No evidence recorded', evidenceMissing: !evidence,
      evidenceNote: '',
      agent: agent, ageSeconds: age,
      ageLabel: age === null ? 'age unknown' : 'no deadline · waiting ' + fmtAge(age),
      options: cardOptions(item, ctx.canAct === true), answerable: item.answerable === true, state: 'open'
    };
  }

  function stuckCard(v) {
    return {
      id: 'stuck:' + v.name, source: 'stuck', kind: 'LOOKS STUCK', tone: 'bad', severity: 'med',
      title: v.short + ' has gone quiet',
      evidence: v.stuck.evidence, evidenceText: v.stuck.evidence, evidenceMissing: false,
      evidenceNote: STUCK_NOTE, agent: v.short, ageSeconds: v.stuck.progressAge,
      ageLabel: fmtAge(v.stuck.progressAge),
      options: [{ label: 'Wait 10 min', primary: true, locked: null, action: 'wait' },
                { label: 'Restart with context', primary: false, locked: 'CLI only' }],
      answerable: false, state: 'open'
    };
  }

  // Queue order: LOOKS STUCK first, then the oldest waiting, then id.
  function compareCards(a, b) {
    var sa = a.kind === 'LOOKS STUCK' ? 0 : 1;
    var sb = b.kind === 'LOOKS STUCK' ? 0 : 1;
    if (sa !== sb) return sa - sb;
    var aa = a.ageSeconds === null ? Infinity : a.ageSeconds;
    var ab = b.ageSeconds === null ? Infinity : b.ageSeconds;
    if (aa !== ab) return ab > aa ? 1 : -1;
    return a.id < b.id ? -1 : (a.id > b.id ? 1 : 0);
  }

  // ---------------------------------------------------------------- greeting

  function needsWord(n) { return n <= 10 ? NUMBER_WORDS[n] : String(n); }

  function greetingFor(mode, x) {
    switch (mode) {
      case 'loading': return { text: '', sub: 'Waiting for the first snapshot.' };
      case 'error': return { text: 'Can’t read this team.', sub: 'Its store could not be read, so nothing is shown for it.' };
      case 'offline':
        return { text: 'Can’t see the team.',
          sub: x.kind === 'unreachable'
            ? 'Everything below is greyed and stamped as of ' + x.asOf + '. Nothing is live, and nothing you press can reach the lead until the console server is back.'
            : 'Everything below is greyed and stamped as of ' + x.asOf + '. Nothing is live until an agent writes again.' };
      case 'needs-unavailable':
        return { text: 'Can’t read what needs you.', sub: 'The attention feed failed. The roster below is still live.' };
      case 'busy':
        return { text: needsWord(x.n) + (x.n === 1 ? ' thing needs you.' : ' things need you.'),
          sub: 'Stuck agents first, then oldest. A deadline only shows if someone set one.' };
      case 'answered':
        return { text: 'That’s everything.', sub: 'The lead will come back only when something needs a human.' };
      case 'deferred':
        return { text: 'Nothing new needs you.', sub: plural(x.n, 'item is', 'items are') + ' deferred: still open, not dismissed.' };
      case 'calm':
        return { text: 'Nothing needs you right now.', sub: x.what };
      default:
        return { text: 'All quiet.', sub: 'Nothing needs you. ' + x.idle + ' of ' + x.total +
          ' agents are idle — that’s their resting state; they wake when the lead messages them.' };
    }
  }

  // ------------------------------------------------------------ since you last looked

  function sinceRows(recent, sinceMs, nowMs, tz) {
    var list = Array.isArray(recent) ? recent : [];
    var n = 0;
    var reviews = 0;
    var responses = 0;
    var oldest = null;
    list.forEach(function (e) {
      if (!isObj(e)) return;
      var t = parseMs(e.ts);
      if (t === null) return;
      if (oldest === null || t < oldest) oldest = t;
      if (t <= sinceMs) return;
      n += 1;
      if (e.kind === 'review-result') reviews += 1;
      if (e.kind === 'task-response') responses += 1;
    });
    var atLeast = list.length >= RECENT_LIMIT && oldest !== null && oldest > sinceMs;
    var more = atLeast ? '+' : '';
    var since = clockHM(sinceMs, tz);
    var rows = [];
    if (n) rows.push({ title: n + more + (n === 1 && !atLeast ? ' message' : ' messages'), detail: 'exchanged since ' + since });
    if (reviews) rows.push({ title: reviews + more + (reviews === 1 && !atLeast ? ' review result' : ' review results'), detail: 'posted since ' + since });
    if (responses) rows.push({ title: responses + more + (responses === 1 && !atLeast ? ' task response' : ' task responses'), detail: 'posted since ' + since });
    return rows;
  }

  // -------------------------------------------------------------- the team view

  function knownProjectsOf(root) {
    var out = [];
    function add(v) {
      if (typeof v === 'string' && v && out.indexOf(v.toLowerCase()) < 0) out.push(v.toLowerCase());
    }
    add(root && root.label);
    var p = root && typeof root.path === 'string' ? root.path : '';
    add(p.split(/[\\/]/).filter(Boolean).pop());
    return out;
  }

  // buildTeamView(input) -> everything the UI shows for one team.
  //   input.nowMs         anchored "now" (server generated_at + elapsed), never the local clock
  //   input.generatedMs   generated_at of the snapshot that produced input.root (or null)
  //   input.root          one /api/state root
  //   input.attention     null | { ok, asOfMs, items }   from /api/attention
  //   input.chat          null | { ok, asOfMs, payload } from /api/lead-chat
  //   input.conn          { reachable, stalledPolls, lastOkMs }
  //   input.ui            { deferred:{id:true}, snoozedUntil:{id:ms}, answered:{id:true}, lastVisitMs }
  //   input.tz            'utc' in tests, else the local zone
  function buildTeamView(input) {
    var nowMs = input.nowMs;
    var root = isObj(input.root) ? input.root : {};
    var ui = isObj(input.ui) ? input.ui : {};
    var tz = input.tz;
    var label = typeof root.label === 'string' && root.label ? root.label : (input.fallbackLabel || 'Team');
    var view = {
      label: label, key: typeof root.project_id === 'string' ? root.project_id : '',
      mode: 'ok', stale: false, banner: null, freshness: null,
      greeting: { text: '', sub: '' }, lead: null,
      needs: { open: [], answered: [], deferredCount: 0, snoozed: [], stale: false, loaded: false, available: true },
      aside: { title: 'ALSO HAPPENING · NOT FOR YOU', rows: [], more: 0 }, since: null,
      roster: { total: 0, summary: '', rows: [] }, usage: [], chat: null, composer: null,
      chip: { label: label, freshness: 'loading', needsCount: null }
    };

    if (Array.isArray(root.errors) && root.errors.length) {
      // errors-as-data root: say only that it cannot be read (the text may carry paths).
      view.mode = 'error';
      view.greeting = greetingFor('error', {});
      view.chip.freshness = 'error';
      return view;
    }
    var agents = (Array.isArray(root.agents) ? root.agents : []).filter(isObj);
    if (!agents.length && !Array.isArray(root.agents)) {
      view.mode = 'loading';
      view.greeting = greetingFor('loading', {});
      return view;
    }

    // --- roster rows -----------------------------------------------------------
    var teamIds = agents.map(function (a) { return typeof a.name === 'string' ? a.name : ''; });
    var known = knownProjectsOf(root);
    var project = teamProject(teamIds, known);
    var recent = Array.isArray(root.recent) ? root.recent : [];
    var ctx = { nowMs: nowMs, generatedMs: typeof input.generatedMs === 'number' ? input.generatedMs : null,
                recent: recent, teamIds: teamIds, project: project, known: known, tz: tz };
    var rows = agents.map(function (a) { return agentView(a, ctx); });
    var lead = typeof root.operator_facing === 'string' ? root.operator_facing : '';
    if (lead) {
      rows.sort(function (a, b) { return (a.name === lead ? 0 : 1) - (b.name === lead ? 0 : 1); });   // stable for the rest
    }
    var idle = rows.filter(function (r) { return r.state === 'idle'; }).length;

    // --- freshness and the two banners ----------------------------------------
    var fresh = freshness(root, input.conn, nowMs, tz);
    view.freshness = fresh;
    view.banner = fresh.banner;
    view.chip.freshness = fresh.state;
    var offline = fresh.banner !== null;

    // --- needs you -------------------------------------------------------------
    var att = isObj(input.attention) ? input.attention : null;
    var attentionAsOf = att && typeof att.asOfMs === 'number' ? att.asOfMs : nowMs;
    var cards = [];
    var lowRows = [];
    if (att) {
      view.needs.loaded = true;
      view.needs.available = att.ok !== false;
      view.needs.stale = att.ok === false || (nowMs - attentionAsOf) / 1000 > ATTENTION_FRESH_S;
      (Array.isArray(att.items) ? att.items : []).forEach(function (item) {
        if (!isObj(item) || item.source === 'stuck') return;   // stuck cards are derived from health + evidence
        var c = attentionCard(item, { nowMs: nowMs, attentionAsOfMs: attentionAsOf, project: project, teamIds: teamIds,
          known: known, canAct: input.canAct === true });
        if (item.severity === 'low' && item.source !== 'other') {
          lowRows.push({ title: c.title, detail: c.evidence || c.kind });
        } else {
          cards.push(c);
        }
      });
    }
    var snoozeRows = [];
    rows.forEach(function (r) {
      if (!r.stuck) return;
      var card = stuckCard(r);
      var snoozes = ui.snoozedUntil || {};
      var until = hasOwn(snoozes, card.id) && typeof snoozes[card.id] === 'number' ? snoozes[card.id] : null;
      if (until !== null && until > nowMs) {
        view.needs.snoozed.push(card);
        snoozeRows.push({ title: r.short + ' · waiting', detail: 'Snoozed until ' + clockHM(until, tz) });
      } else {
        cards.push(card);
      }
    });
    cards.sort(compareCards);
    // Card ids come from a feed: only OWN keys count ("__proto__" is an id, not an inherited flag).
    var deferred = ui.deferred || {};
    var answered = ui.answered || {};
    function isDeferred(id) { return hasOwn(deferred, id) && !!deferred[id]; }
    function isAnswered(id) { return hasOwn(answered, id) && !!answered[id]; }
    cards.forEach(function (c) {
      if (isAnswered(c.id)) { c.state = 'answered'; view.needs.answered.push(c); }
      else if (isDeferred(c.id)) { view.needs.deferredCount += 1; }
      else view.needs.open.push(c);
    });
    view.needs.deferredCards = cards.filter(function (c) { return isDeferred(c.id) && !isAnswered(c.id); });
    var openCount = view.needs.open.length;
    view.chip.needsCount = att && view.needs.available ? openCount : null;

    // --- lead's latest message ---------------------------------------------------
    // The newest message stays visible, but what the chat feed is doing to it is said next
    // to it: the last read failed (with how long ago the last good read was), the read has
    // not been refreshed for a while (a hung request), or the lead is unavailable.
    var chat = isObj(input.chat) ? input.chat : null;
    if (chat) {
      var pl = isObj(chat.payload) ? chat.payload : {};
      var msgs = Array.isArray(pl.messages) ? pl.messages : [];
      var leadName = typeof pl.lead === 'string' ? pl.lead : lead;
      var found = null;
      for (var i = msgs.length - 1; i >= 0; i--) {
        if (isObj(msgs[i]) && msgs[i].from === leadName && typeof msgs[i].body === 'string') { found = msgs[i]; break; }
      }
      var chatNotes = [];
      var readAge = typeof chat.asOfMs === 'number' ? Math.max(0, (nowMs - chat.asOfMs) / 1000) : null;
      if (chat.ok === false) {
        chatNotes.push({ kind: 'failed', text: 'Lead chat could not be read' +
          (readAge === null ? '' : ' · last read ' + fmtAge(readAge) + ' ago') });
      } else if (readAge !== null && readAge > CHAT_FRESH_S) {
        chatNotes.push({ kind: 'stale', text: 'Lead chat not refreshed for ' + fmtAge(readAge) });
      }
      var leadDown = pl.available === false;
      if (leadDown) {
        chatNotes.push({ kind: 'unavailable', text: 'The lead is unavailable' + (pl.detail ? ': ' + str(pl.detail, 160) : '') });
      }
      // The thread: both sides of the lead chat, oldest first (the feed keeps the newest 100).
      var operatorName = typeof pl.operator === 'string' ? pl.operator : '';
      var thread = [];
      msgs.forEach(function (m) {
        if (!isObj(m) || typeof m.body !== 'string' || typeof m.from !== 'string') return;
        var t = parseMs(m.ts);
        thread.push({
          id: typeof m.id === 'string' ? m.id : String(thread.length), side: m.from === operatorName ? 'you' : 'lead',
          short: m.from === operatorName ? 'you' : shortName(m.from, project, teamIds, known),
          body: str(m.body, LEAD_BODY_LIMIT), truncated: m.body.length > LEAD_BODY_LIMIT, atMs: t,
          ageLabel: t === null ? '' : fmtAge(Math.max(0, (nowMs - t) / 1000)) + ' ago'
        });
      });
      if (thread.length) view.chat = { messages: thread, lastId: thread[thread.length - 1].id };
      if (found || chatNotes.length) {
        var leadShort = leadName ? shortName(leadName, project, teamIds, known) : 'lead';
        var atMs = found ? parseMs(found.ts) : null;
        view.lead = {
          name: leadName, short: leadShort, body: found ? str(found.body, LEAD_BODY_LIMIT) : '',
          truncated: found ? found.body.length > LEAD_BODY_LIMIT : false, atMs: atMs,
          ageLabel: atMs === null ? '' : fmtAge((nowMs - atMs) / 1000) + ' ago',
          unavailable: leadDown, detail: leadDown ? str(pl.detail, 160) : '', notes: chatNotes
        };
      }
    }

    // --- mode and greeting ------------------------------------------------------
    var candidates = rows.filter(function (r) { return r.candidate && !r.stuck; });
    var asOfLabel = fresh.asOfLabel || '?';
    if (offline) {
      view.mode = 'offline';
      view.greeting = greetingFor('offline', { kind: fresh.banner.kind, asOf: asOfLabel });
    } else if (att && !view.needs.available) {
      view.mode = 'needs-unavailable';
      view.greeting = greetingFor('needs-unavailable', {});
    } else if (!att) {
      view.mode = 'loading';
      view.greeting = greetingFor('loading', {});
    } else if (openCount > 0) {
      view.mode = 'busy';
      view.greeting = greetingFor('busy', { n: openCount });
    } else if (view.needs.answered.length > 0) {
      view.mode = 'answered';
      view.greeting = greetingFor('answered', {});
    } else if (view.needs.deferredCount > 0) {
      view.mode = 'deferred';
      view.greeting = greetingFor('deferred', { n: view.needs.deferredCount });
    } else if (candidates.length) {
      view.mode = 'calm';
      view.greeting = greetingFor('calm', { what: candidates[0].short + ' has been quiet for a while; the evidence does not show a stall, so it is only being watched.' });
    } else {
      view.mode = 'quiet';
      view.greeting = greetingFor('quiet', { idle: idle, total: rows.length });
    }
    view.stale = offline || view.needs.stale;
    // The composer. This slice is read-only, so it is always disabled, and says why (the first
    // reason that applies): the team is offline, the lead is unavailable, or the console cannot act.
    var leadIsDown = !!(view.lead && view.lead.unavailable);
    var composerReason = offline ? 'Paused \u2014 the lead can\u2019t receive while the team is offline.'
      : (leadIsDown ? 'The lead is unavailable, so a message cannot be delivered.'
        : 'Read-only: start the console with --enable-actions to message the lead.');
    view.composer = { enabled: input.canAct === true && !offline && !leadIsDown,
                      placeholder: 'Message the lead   ( / )', reason: input.canAct === true && !offline && !leadIsDown ? '' : composerReason };

    // --- "also happening" / "since you last looked" ----------------------------
    var aside = [];
    ['down', 'capped', 'busy'].forEach(function (state) {
      rows.forEach(function (r) { if (r.state === state && r.aside) aside.push(r.aside); });
    });
    aside = aside.concat(snoozeRows, lowRows);
    view.aside = { title: 'ALSO HAPPENING · NOT FOR YOU', rows: aside.slice(0, ASIDE_MAX),
                   more: Math.max(0, aside.length - ASIDE_MAX) };
    if (view.mode === 'quiet') {
      var lastVisit = typeof ui.lastVisitMs === 'number' ? ui.lastVisitMs : null;
      var sinceMs = lastVisit !== null ? lastVisit : nowMs - DAY_S * 1000;
      view.since = { title: lastVisit !== null ? 'SINCE YOU LAST LOOKED · ' + whenLabel(lastVisit, nowMs, tz).toUpperCase()
        : 'IN THE LAST 24 HOURS', rows: sinceRows(recent, sinceMs, nowMs, tz) };
    }

    // --- rail ------------------------------------------------------------------
    view.roster = {
      total: rows.length,
      summary: offline ? 'frozen · as of ' + asOfLabel
        : (idle + ' idle' + (idle > 0 ? ' · that’s normal' : '')),
      rows: rows
    };
    view.usage = usageRows(agents, nowMs, tz);
    return view;
  }

  // buildShellView(input) -> the team chips and the selected team's view.
  //   input.roots            /api/state roots (null before the first snapshot)
  //   input.attentionByRoot  { project_id: { ok, asOfMs, items } }
  //   input.chatByRoot       { project_id: { ok, asOfMs, payload } }
  //   input.param            the ?root= selector (or the operator's pick)
  //   plus nowMs, generatedMs, conn, ui, tz as in buildTeamView
  function buildShellView(input) {
    var roots = Array.isArray(input.roots) ? input.roots : null;
    var sel = resolveRoot(roots, input.param);
    var byAtt = isObj(input.attentionByRoot) ? input.attentionByRoot : {};
    var byChat = isObj(input.chatByRoot) ? input.chatByRoot : {};
    var teams = (roots || []).map(function (r, i) {
      var id = isObj(r) && typeof r.project_id === 'string' ? r.project_id : '';
      // Deferrals and snoozes belong to one team: uiFor(project_id) gives that team's local state.
      var teamUi = typeof input.uiFor === 'function' ? input.uiFor(id) : input.ui;
      var v = buildTeamView({ nowMs: input.nowMs, generatedMs: input.generatedMs, root: r, fallbackLabel: 'Team ' + (i + 1),
        attention: byAtt[id] || null, chat: byChat[id] || null, conn: input.conn, ui: teamUi, tz: input.tz,
        canAct: input.canAct === true });
      return { key: id || 'idx:' + i, label: v.label, freshness: v.chip.freshness, needsCount: v.chip.needsCount,
               pressed: sel.index === i, index: i, view: v };
    });
    return { selection: sel, teams: teams, view: sel.index >= 0 ? teams[sel.index].view : null };
  }

  return {
    THEMES: THEMES,
    DEFAULT_THEME: DEFAULT_THEME,
    normalizeTheme: normalizeTheme,
    nextTheme: nextTheme,
    parseAgentName: parse,
    shortName: shortName,
    teamProject: teamProject,
    resolveRoot: resolveRoot,
    // M2: pure view model
    LIMITS: {
      HEARTBEAT_FRESH_S: HEARTBEAT_FRESH_S, SOURCE_STALE_S: SOURCE_STALE_S, STUCK_AFTER_S: STUCK_AFTER_S,
      RECENT_LIMIT: RECENT_LIMIT, STALLED_POLLS: STALLED_POLLS, ATTENTION_FRESH_S: ATTENTION_FRESH_S,
      CHAT_FRESH_S: CHAT_FRESH_S
    },
    parseMs: parseMs,
    ageSeconds: ageSeconds,
    fmtAge: fmtAge,
    fmtAgeLong: fmtAgeLong,
    clockHM: clockHM,
    whenLabel: whenLabel,
    resetLabel: resetLabel,
    avatarFile: avatarFile,
    replyInfo: replyInfo,
    agentView: agentView,
    usageRows: usageRows,
    sourceAsOf: sourceAsOf,
    freshness: freshness,
    sinceRows: sinceRows,
    buildTeamView: buildTeamView,
    buildShellView: buildShellView
  };
}));
