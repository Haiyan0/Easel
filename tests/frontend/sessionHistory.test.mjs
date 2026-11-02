import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { runInNewContext } from 'node:vm';
import { test } from 'node:test';

const require = createRequire(new URL('../../web/frontend/package.json', import.meta.url));
const ts = require('typescript');
const source = readFileSync(new URL('../../web/frontend/src/lib/store.ts', import.meta.url), 'utf8');
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
});

function storage(initial = {}) {
  const entries = new Map(Object.entries(initial));
  return {
    get length() { return entries.size; },
    key: index => Array.from(entries.keys())[index] ?? null,
    getItem: key => entries.get(key) ?? null,
    setItem: (key, value) => entries.set(key, String(value)),
    removeItem: key => entries.delete(key),
  };
}

function load(initial) {
  const localStorage = storage(initial);
  const sessionStorage = storage({ easel_tab_session: 'original', 'easel_pending_turn:original': 'pending' });
  const exports = {};
  runInNewContext(outputText, { exports, localStorage, sessionStorage, Date, Math });
  return { exports, localStorage, sessionStorage };
}

test('loading without the obsolete reset marker preserves existing conversation history and recovery', () => {
  const original = [{ id: 'original', messages: [{ role: 'user', content: '历史对话' }], created: 1 }];
  const state = load({ easel_sessions: JSON.stringify(original), easel_active_session: 'original' });
  assert.equal(JSON.stringify(state.exports.loadSessions()), JSON.stringify(original));
  assert.equal(state.exports.loadActiveId(), 'original');
  assert.equal(state.sessionStorage.getItem('easel_tab_session'), 'original');
  assert.equal(state.sessionStorage.getItem('easel_pending_turn:original'), 'pending');
});

test('local history tolerates shared workspace view fields without identity storage', () => {
  const original = [{
    id: 'view-session',
    title: '共享视图',
    created: 1,
    remote: true,
    messages: [
      { role: 'user', content: '发送中', pending: true },
      { role: 'assistant', content: '失败', failed: true },
    ],
  }];
  const state = load({ easel_sessions: JSON.stringify(original) });
  const loaded = state.exports.loadSessions();
  assert.equal(loaded[0].remote, true);
  assert.equal(loaded[0].messages[0].pending, true);
  assert.equal(loaded[0].messages[1].failed, true);
  state.exports.saveSessions(loaded);
  const saved = JSON.parse(state.localStorage.getItem('easel_sessions'));
  assert.equal(saved[0].remote, true);
  assert.equal(saved[0].messages[0].pending, true);
  assert.equal(
    Array.from({ length: state.localStorage.length }, (_, index) =>
      state.localStorage.key(index)).join(','),
    'easel_sessions',
  );
});

test('legacy conversation history migrates rather than being deleted on first load', () => {
  const original = [{ id: 'original', messages: [{ role: 'user', content: '迁移的历史' }], created: 1 }];
  const previous = ['post', 'craft'].join('');
  const state = load({
    [`${previous}_sessions`]: JSON.stringify(original),
    [`${previous}_active_session`]: 'original',
  });
  assert.equal(JSON.stringify(state.exports.loadSessions()), JSON.stringify(original));
  assert.equal(state.exports.loadActiveId(), 'original');
  assert.equal(state.localStorage.getItem('easel_sessions'), JSON.stringify(original));
  assert.equal(state.localStorage.getItem(`${previous}_sessions`), null);
});
