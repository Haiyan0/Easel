import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { runInNewContext } from 'node:vm';
import { test } from 'node:test';

const require = createRequire(new URL('../../web/frontend/package.json', import.meta.url));
const ts = require('typescript');
const source = readFileSync(
  new URL('../../web/frontend/src/lib/store.ts', import.meta.url), 'utf8');
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
});

function load() {
  const module = { exports: {} };
  const storage = new Map();
  runInNewContext(outputText, {
    exports: module.exports, module,
    require: () => { throw new Error('no runtime imports expected'); },
    localStorage: {
      getItem: key => storage.has(key) ? storage.get(key) : null,
      setItem: (key, value) => storage.set(key, String(value)),
      removeItem: key => storage.delete(key),
    },
    Promise, Array, Object, String, Number, Error, JSON, Math, Set, Map,
    Date, encodeURIComponent, console,
  });
  return { store: module.exports, storage };
}

const draft = (title) => ({
  title, body: '正文', platforms: ['xiaohongshu'],
  overrides: {}, tags: '',
});

test('local draft keeps the original global key', () => {
  const { store, storage } = load();
  store.savePublishDraft(draft('本机'));
  assert.equal(storage.has('easel_publish_draft'), true);
  assert.equal(store.loadPublishDraft().title, '本机');
});

test('control draft is scoped and never falls back to the global key', () => {
  const { store, storage } = load();
  storage.set('easel_publish_draft', JSON.stringify(draft('旧全局')));
  store.savePublishDraft(draft('A'), 'user-a:ws-a');
  store.savePublishDraft(draft('B'), 'user-b:ws-b');
  assert.equal(store.loadPublishDraft('user-a:ws-a').title, 'A');
  assert.equal(store.loadPublishDraft('user-b:ws-b').title, 'B');
  assert.equal(store.loadPublishDraft('user-c:ws-c').title, '');
  assert.equal(store.loadPublishDraft().title, '旧全局');
});

test('scoped draft stays independent when the global draft changes', () => {
  const { store } = load();
  store.savePublishDraft(draft('A'), 'user-a:ws-a');
  store.savePublishDraft(draft('本机新稿'));
  assert.equal(store.loadPublishDraft('user-a:ws-a').title, 'A');
  assert.equal(store.loadPublishDraft().title, '本机新稿');
});
