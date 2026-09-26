// Reference implementation of the agent-name rule (docs/06-RULES.md). Run: node name-shortener.js
const ROLE = { developer: 'dev', reviewer: 'rev', tester: 'test', architect: 'arch', lead: 'lead' };
const RL = { claude: 'c', codex: 'x', qwen: 'q' };
function parse(id) {
  const m = /^(claude|codex|qwen)-([a-z0-9]+)-([a-z]+)(?:-(\d+))?$/.exec(id);
  return m ? { rt: m[1], proj: m[2], base: (ROLE[m[3]] || m[3]) + (m[4] ? '-' + m[4] : '') } : null;
}
function shortName(id, currentProject, teamIds = []) {
  const q = parse(id);
  if (!q) return id.length > 18 ? id.slice(0, 17) + '…' : id;
  let s = q.base;
  const clash = teamIds.some(o => { const z = parse(o); return o !== id && z && z.base === q.base && z.proj === q.proj; });
  if (clash) s = RL[q.rt] + '.' + s;
  if (currentProject && q.proj !== currentProject) s = q.proj + '/' + s;
  return s;
}
module.exports = { shortName, parse };
if (require.main === module) {
  const cases = [
    ['codex-agenttalk-developer-5', 'agenttalk', [], 'dev-5'],
    ['claude-agenttalk-lead', 'agenttalk', [], 'lead'],
    ['qwen-agenttalk-reviewer-4', 'agenttalk', [], 'rev-4'],
    ['claude-shopfront-developer-1', 'shopfront', [], 'dev-1'],
    ['claude-shopfront-developer-1', 'agenttalk', [], 'shopfront/dev-1'],
    ['codex-agenttalk-developer-1', 'agenttalk', ['claude-agenttalk-developer-1', 'codex-agenttalk-developer-1'], 'x.dev-1'],
    ['nightly-docs-builder-agent', 'agenttalk', [], 'nightly-docs-buil…'],
  ];
  let ok = 0;
  for (const [id, p, pool, want] of cases) { const got = shortName(id, p, pool); console.log((got === want ? 'PASS' : 'FAIL'), id, '→', got); if (got === want) ok++; }
  console.log(ok + '/' + cases.length + ' passed'); process.exit(ok === cases.length ? 0 : 1);
}
