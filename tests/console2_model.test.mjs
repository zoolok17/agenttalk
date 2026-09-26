// Console v2 pure-model tests (M1): name shortener and theme helpers.
// Run: node tests/console2_model.test.mjs   (also run by tests/test_console2_web.py)
import assert from 'node:assert/strict';
import { createRunner, loadConsole, makeDom } from './console2_harness.mjs';

const { sandbox } = loadConsole({ dom: makeDom(), modelOnly: true });
const M = sandbox.window.AgentTalkConsole2Model;
const { test, run } = createRunner('console2 model');

const PROJECTS = ['agenttalk', 'shopfront'];
const TIE = ['codex-agenttalk-reviewer-1', 'qwen-agenttalk-reviewer-1'];

// The nine cases of the handoff's reference/name-shortener.js, verbatim.
const REFERENCE_CASES = [
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

test('reference cases (handoff 06-RULES table and name-shortener.js)', () => {
  for (const [id, project, pool, want] of REFERENCE_CASES) {
    assert.equal(M.shortName(id, project, pool, PROJECTS), want, id);
  }
});

// The live team's roster (10 agents), expected short names pinned one by one.
const ROSTER = [
  ['claude-agenttalk-developer-2', 'dev-2'],
  ['codex-agenttalk-reviewer-1', 'x.rev-1'],
  ['codex-agenttalk-developer-4', 'dev-4'],
  ['claude-agenttalk-reviewer-3', 'rev-3'],
  ['claude-agenttalk-lead', 'lead'],
  ['codex-agenttalk-developer-5', 'dev-5'],
  ['claude-agenttalk-developer-6', 'dev-6'],
  ['claude-agenttalk-frontend-dev', 'fe-dev'],
  ['qwen-agenttalk-dev-1', 'dev-1'],
  ['qwen-agenttalk-reviewer-1', 'q.rev-1'],
];

test('live roster: every short name pinned, ties only where needed', () => {
  const ids = ROSTER.map((r) => r[0]);
  for (const [id, want] of ROSTER) {
    assert.equal(M.shortName(id, 'agenttalk', ids, PROJECTS), want, id);
  }
  // Without the project list the first token is the project: same answers here.
  for (const [id, want] of ROSTER) {
    assert.equal(M.shortName(id, 'agenttalk', ids, []), want, id + ' (no known list)');
  }
});

test('a tie between runtimes gets the runtime letter on both, and only then', () => {
  const ids = ['claude-agenttalk-developer-1', 'qwen-agenttalk-dev-1', 'codex-agenttalk-developer-2'];
  assert.equal(M.shortName(ids[0], 'agenttalk', ids, PROJECTS), 'c.dev-1');
  assert.equal(M.shortName(ids[1], 'agenttalk', ids, PROJECTS), 'q.dev-1');
  assert.equal(M.shortName(ids[2], 'agenttalk', ids, PROJECTS), 'dev-2');
});

test('cross-team list keeps the project prefix, with tie letter after it', () => {
  const ids = ['codex-shopfront-reviewer-1', 'qwen-shopfront-reviewer-1', 'claude-agenttalk-lead'];
  assert.equal(M.shortName(ids[0], 'agenttalk', ids, PROJECTS), 'shopfront/x.rev-1');
  assert.equal(M.shortName(ids[0], 'shopfront', ids, PROJECTS), 'x.rev-1');
  assert.equal(M.shortName(ids[2], '', ids, PROJECTS), 'agenttalk/lead');
});

test('same base in different projects is not a tie', () => {
  const ids = ['codex-agenttalk-reviewer-1', 'codex-shopfront-reviewer-1'];
  assert.equal(M.shortName(ids[0], 'agenttalk', ids, PROJECTS), 'rev-1');
});

test('role is everything between project and number, each word abbreviated', () => {
  assert.equal(M.shortName('claude-agenttalk-backend-tester-3', 'agenttalk', [], PROJECTS), 'be-test-3');
  assert.equal(M.shortName('claude-agenttalk-frontend-architect', 'agenttalk', [], PROJECTS), 'fe-arch');
  assert.equal(M.shortName('claude-agenttalk-release-manager-2', 'agenttalk', [], PROJECTS), 'release-manager-2');
});

test('a project with hyphens is matched against the known list', () => {
  assert.equal(M.shortName('claude-my-app-developer-1', 'my-app', [], ['my-app']), 'dev-1');
  // Unknown: the first token is taken as the project (documented limit).
  assert.equal(M.shortName('claude-my-app-developer-1', 'my', [], []), 'app-dev-1');
});

test('names that do not fit are shown in full, cut at the end, never in the middle', () => {
  assert.equal(M.shortName('planner', 'agenttalk', [], PROJECTS), 'planner');
  assert.equal(M.shortName('claude-agenttalk', 'agenttalk', [], PROJECTS), 'claude-agenttalk');
  const eighteen = 'a'.repeat(18);
  assert.equal(M.shortName(eighteen, 'agenttalk', [], PROJECTS), eighteen);
  const nineteen = 'a'.repeat(19);
  assert.equal(M.shortName(nineteen, 'agenttalk', [], PROJECTS), 'a'.repeat(17) + '…');
  assert.equal(M.shortName('nightly-docs-builder-agent', 'agenttalk', [], PROJECTS).length, 18);
});

test('role words that collide with Object.prototype names are left alone', () => {
  assert.equal(M.shortName('claude-agenttalk-constructor-1', 'agenttalk', [], PROJECTS), 'constructor-1');
  assert.equal(M.shortName('claude-agenttalk-toString', 'agenttalk', [], PROJECTS), 'toString');
});

test('non-string and hostile input never throws and stays a plain string', () => {
  for (const bad of [null, undefined, 42, {}, [], '', '<img src=x onerror=alert(1)>']) {
    const out = M.shortName(bad, 'agenttalk', [bad, 'claude-agenttalk-lead'], PROJECTS);
    assert.equal(typeof out, 'string');
  }
  assert.equal(
    M.shortName('<img src=x onerror=alert(1)>', 'agenttalk', [], PROJECTS),
    '<img src=x onerro…',
  );
});

test('teamProject picks the project most agents belong to', () => {
  const ids = ROSTER.map((r) => r[0]);
  assert.equal(M.teamProject(ids, PROJECTS), 'agenttalk');
  assert.equal(M.teamProject(ids, []), 'agenttalk');
  assert.equal(M.teamProject([], PROJECTS), '');
  assert.equal(M.teamProject(['planner', 'x'], PROJECTS), '');
  assert.equal(
    M.teamProject(['claude-shopfront-lead', 'codex-shopfront-developer-1', 'claude-agenttalk-lead'], PROJECTS),
    'shopfront',
  );
});

test('themes: four names in switch order, midnight default', () => {
  assert.deepEqual(Array.from(M.THEMES), ['midnight', 'paper', 'synthwave', 'terminal']);
  assert.equal(M.DEFAULT_THEME, 'midnight');
});

test('normalizeTheme accepts only known names', () => {
  for (const name of M.THEMES) assert.equal(M.normalizeTheme(name), name);
  for (const bad of [null, undefined, 5, {}, '', 'PAPER', ' paper', '__proto__', 'constructor', 'dark']) {
    assert.equal(M.normalizeTheme(bad), 'midnight', String(bad));
  }
});

test('nextTheme cycles through all four and recovers from an unknown value', () => {
  let t = 'midnight';
  const seen = [];
  for (let i = 0; i < 5; i++) { t = M.nextTheme(t); seen.push(t); }
  assert.deepEqual(seen, ['paper', 'synthwave', 'terminal', 'midnight', 'paper']);
  assert.equal(M.nextTheme('bogus'), 'paper');
});

run();
