// Reference implementation of the agent-name rule (docs/06-RULES.md). Run: node name-shortener.js
// Role = everything between the project and the optional trailing number (hyphens included).
const ABBR = { developer: 'dev', reviewer: 'rev', tester: 'test', architect: 'arch', frontend: 'fe', backend: 'be', lead: 'lead' };
const RL = { claude: 'c', codex: 'x', qwen: 'q' };
function parse(id, knownProjects = []) {
  const m = /^(claude|codex|qwen)-(.+)$/.exec(id);
  if (!m) return null;
  const rest = m[2];
  let proj = knownProjects.find(p => rest.startsWith(p + '-')), tail;
  if (proj) tail = rest.slice(proj.length + 1);
  else { const i = rest.indexOf('-'); if (i < 0) return null; proj = rest.slice(0, i); tail = rest.slice(i + 1); }
  const n = /^(.*?)(?:-(\d+))?$/.exec(tail);
  if (!n[1]) return null;
  return { rt: m[1], proj, base: n[1].split('-').map(w => ABBR[w] || w).join('-') + (n[2] ? '-' + n[2] : '') };
}
function shortName(id, currentProject, teamIds = [], knownProjects = []) {
  const q = parse(id, knownProjects);
  if (!q) return id.length > 18 ? id.slice(0, 17) + '…' : id;
  let s = q.base;
  const clash = teamIds.some(o => { const z = parse(o, knownProjects); return o !== id && z && z.base === q.base && z.proj === q.proj; });
  if (clash) s = RL[q.rt] + '.' + s;
  if (currentProject && q.proj !== currentProject) s = q.proj + '/' + s;
  return s;
}
module.exports = { shortName, parse };
if (require.main === module) {
  const P = ['agenttalk', 'shopfront'];
  const TIE = ['codex-agenttalk-reviewer-1', 'qwen-agenttalk-reviewer-1'];
  const cases = [
    ['codex-agenttalk-developer-5', 'agenttalk', [], 'dev-5'],
    ['claude-agenttalk-frontend-dev', 'agenttalk', [], 'fe-dev'],
    ['claude-agenttalk-frontend-dev-2', 'agenttalk', [], 'fe-dev-2'],
    ['claude-agenttalk-lead', 'agenttalk', [], 'lead'],
    ['codex-agenttalk-reviewer-1', 'agenttalk', TIE, 'x.rev-1'],
    ['qwen-agenttalk-reviewer-1', 'agenttalk', TIE, 'q.rev-1'],
    ['claude-shopfront-developer-1', 'shopfront', [], 'dev-1'],
    ['claude-shopfront-developer-1', 'agenttalk', [], 'shopfront/dev-1'],
    ['nightly-docs-builder-agent', 'agenttalk', [], 'nightly-docs-buil…'],
  ];
  let ok = 0;
  for (const [id, p, pool, want] of cases) { const got = shortName(id, p, pool, P); console.log(got === want ? 'PASS' : 'FAIL', id, '→', got); if (got === want) ok++; }
  console.log(ok + '/' + cases.length + ' passed'); process.exit(ok === cases.length ? 0 : 1);
}
