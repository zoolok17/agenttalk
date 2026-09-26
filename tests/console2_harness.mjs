// Shared harness for the console v2 node tests: a tiny runner and a recording DOM
// stub. The stub is strict on purpose. Every way of turning a string into markup or
// style (innerHTML, outerHTML, insertAdjacentHTML, setAttribute of style / href /
// on* / src) throws AND is recorded in `violations`, because a page script can catch
// a throw; the tests assert `violations` stays empty.
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
export const STATIC_DIR = path.join(here, '..', 'src', 'agenttalk', 'web_static');

export function readStatic(name) {
  return fs.readFileSync(path.join(STATIC_DIR, name), 'utf8');
}

// ------------------------------------------------------------------ runner

export function createRunner(title) {
  const cases = [];
  return {
    test(name, fn) { cases.push([name, fn]); },
    async run() {
      let passed = 0;
      for (const [name, fn] of cases) {
        try {
          await fn();
          passed += 1;
          console.log('PASS', name);
        } catch (err) {
          console.log('FAIL', name);
          console.log(String(err && err.stack ? err.stack : err));
        }
      }
      console.log(`${title}: ${passed}/${cases.length} passed`);
      process.exit(passed === cases.length ? 0 : 1);
    },
  };
}

// ---------------------------------------------------------------- DOM stub

export function makeDom() {
  const violations = [];
  const registry = new Map();   // id -> node

  function forbid(what) {
    violations.push(what);
    throw new Error('forbidden DOM use: ' + what);
  }

  class Node {
    constructor(tag) {
      this.tagName = tag.toUpperCase();
      this.className = '';
      this.children = [];
      this.attributes = {};
      this.listeners = {};
      this.disabled = false;
      this._text = '';
      this.nodeType = 1;
    }
    get textContent() {
      return this.children.length ? this.children.map((c) => c.textContent).join('') : this._text;
    }
    set textContent(value) {
      this.children = [];
      this._text = String(value);
    }
    set innerHTML(_v) { forbid('innerHTML'); }
    get innerHTML() { return forbid('innerHTML read'); }
    set outerHTML(_v) { forbid('outerHTML'); }
    insertAdjacentHTML() { forbid('insertAdjacentHTML'); }
    appendChild(child) { this.children.push(child); return child; }
    setAttribute(name, value) {
      const n = String(name).toLowerCase();
      if (n === 'style' || n === 'href' || n === 'src' || n === 'srcdoc' || n.startsWith('on')) {
        forbid('setAttribute ' + n);
      }
      this.attributes[n] = String(value);
      if (n === 'id') registry.set(String(value), this);
    }
    getAttribute(name) {
      const n = String(name).toLowerCase();
      return Object.prototype.hasOwnProperty.call(this.attributes, n) ? this.attributes[n] : null;
    }
    addEventListener(type, fn) {
      (this.listeners[type] = this.listeners[type] || []).push(fn);
    }
    click() { (this.listeners.click || []).forEach((fn) => fn({ target: this })); }
  }

  class TextNode {
    constructor(text) { this.nodeType = 3; this.textContent = String(text); }
  }

  const documentElement = new Node('html');
  const document = {
    documentElement,
    createElement: (tag) => new Node(tag),
    createTextNode: (text) => new TextNode(text),
    getElementById: (id) => registry.get(id) || null,
    addEventListener(type, fn) { (this._l[type] = this._l[type] || []).push(fn); },
    _l: {},
    dispatch(type, ev) { (this._l[type] || []).forEach((fn) => fn(ev)); },
  };

  // Server-authored shell regions the script looks up by id.
  for (const [tag, id] of [['header', 'c2-header'], ['main', 'c2-stream'], ['aside', 'c2-rail'],
                           ['footer', 'c2-footer'], ['span', 'c2-hints']]) {
    const node = new Node(tag);
    node.setAttribute('id', id);
  }

  return { document, violations, Node, walk, texts };
}

// Depth-first list of every element under (and including) `node`.
export function walk(node, out = []) {
  if (node.nodeType !== 1) return out;
  out.push(node);
  node.children.forEach((c) => walk(c, out));
  return out;
}

// Every text a node tree carries, in order.
export function texts(node, out = []) {
  if (node.nodeType === 3) { out.push(node.textContent); return out; }
  if (node._text) out.push(node._text);
  node.children.forEach((c) => texts(c, out));
  return out;
}

// ---------------------------------------------------------- script loading

// Evaluate the model then console2.js inside one vm context with the given globals.
export function loadConsole(opts) {
  const dom = opts.dom;
  const store = new Map(Object.entries(opts.storage || {}));
  const localStorage = {
    getItem(k) {
      if (opts.storageThrows) throw new Error('storage disabled');
      return store.has(k) ? store.get(k) : null;
    },
    setItem(k, v) {
      if (opts.storageThrows) throw new Error('storage disabled');
      store.set(k, String(v));
    },
  };
  const sandbox = {
    document: dom.document,
    fetch: opts.fetch,
    console,
  };
  sandbox.window = sandbox;
  sandbox.localStorage = localStorage;
  vm.createContext(sandbox);
  vm.runInContext(readStatic('console2-model.js'), sandbox, { filename: 'console2-model.js' });
  if (!opts.modelOnly) {
    vm.runInContext(readStatic('console2.js'), sandbox, { filename: 'console2.js' });
  }
  return { sandbox, store };
}

export const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

export function jsonResponse(payload, status = 200) {
  return Promise.resolve({ ok: status >= 200 && status < 300, status, json: () => Promise.resolve(payload) });
}
