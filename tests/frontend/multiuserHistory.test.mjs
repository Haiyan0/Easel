import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { runInNewContext } from 'node:vm';
import { test } from 'node:test';

const require = createRequire(new URL('../../web/frontend/package.json', import.meta.url));
const ts = require('typescript');
const source = readFileSync(
  new URL('../../web/frontend/src/lib/control.ts', import.meta.url), 'utf8');
const { outputText } = ts.transpileModule(source, {
  compilerOptions: {
    target: ts.ScriptTarget.ES2022,
    module: ts.ModuleKind.CommonJS,
  },
});

function load(fetchImpl) {
  const module = { exports: {} };
  runInNewContext(outputText, {
    exports: module.exports,
    fetch: fetchImpl,
    encodeURIComponent,
    Number,
    JSON,
    Promise,
    Array,
    Object,
    String,
  });
  return module.exports;
}

const idle = load(async () => {
  throw new Error('no network');
});

test('runtime mode fails closed on unknown or unavailable startup info', async () => {
  assert.equal(await idle.detectRuntimeMode(async () => ({
    ok: true, status: 200, json: async () => ({ mode: 'control' }),
  })), 'control');
  assert.equal(await idle.detectRuntimeMode(async () => ({
    ok: true, status: 200, json: async () => ({ mode: 'local' }),
  })), 'local');
  for (const response of [
    async () => ({ ok: true, status: 200, json: async () => ({ mode: 'x' }) }),
    async () => ({ ok: false, status: 404, json: async () => ({}) }),
    async () => { throw new Error('down'); },
  ]) {
    await assert.rejects(() => idle.detectRuntimeMode(response));
  }
});

test('202 admission is parsed; other statuses and malformed bodies fail', () => {
  const admission = idle.parseAdmission(202, {
    runId: 'run-1', state: 'accepted', duplicate: false,
  });
  assert.deepEqual(JSON.parse(JSON.stringify(admission)), {
    runId: 'run-1', state: 'accepted', duplicate: false,
  });
  assert.equal(idle.parseAdmission(202, {
    runId: 'run-1', state: 'running', duplicate: true,
  }).duplicate, true);
  assert.throws(() => idle.parseAdmission(500, { runId: 'run-1' }));
  assert.throws(() => idle.parseAdmission(202, { state: 'accepted' }));
  assert.throws(() => idle.parseAdmission(202, null));
});

test('SSE frames split across chunks and CRLF are parsed without loss', () => {
  let buffer = '';
  const frames = [];
  const first = idle.consumeSseChunk(buffer, 'id: 1\r\nevent: state\r\n');
  buffer = first.rest;
  assert.equal(first.frames.length, 0);
  const second = idle.consumeSseChunk(
    buffer, 'data: {"state":"running"}\r\n\r\nid: 2\nevent: token\ndata: "你');
  buffer = second.rest;
  frames.push(...second.frames);
  assert.equal(frames.length, 1);
  assert.deepEqual(
    JSON.parse(JSON.stringify(idle.stateFromEvent(frames[0]))),
    { state: 'running' });
  const third = idle.consumeSseChunk(buffer, '好"\n\n');
  frames.push(...third.frames);
  assert.equal(third.rest, '');
  assert.equal(frames[1].id, 2);
  assert.equal(idle.textFromEvent(frames[1]), '你好');
});

test('question payloads support double JSON encoding and opaque values', () => {
  const payload = {
    id: 'runq_1',
    questions: [{
      questionId: 'permission',
      header: '执行授权',
      question: '允许？',
      options: [
        { label: '1. Allow', value: 'qopt_abc' },
        { label: '2. Reject', value: 'qopt_def' },
      ],
    }],
  };
  const single = idle.parseQuestionData(JSON.stringify(payload));
  assert.equal(single.id, 'runq_1');
  const double = idle.parseQuestionData(JSON.stringify(JSON.stringify(payload)));
  assert.equal(double.questions[0].questionId, 'permission');
  assert.equal(idle.optionSubmitValue(double.questions[0].options[0]),
    'qopt_abc');
  assert.equal(idle.optionSubmitValue({ label: 'yes' }), 'yes');
  assert.equal(idle.questionAnswerKey(double.questions[0], 0), 'permission');
  assert.equal(idle.parseQuestionData('not-json'), null);
  assert.equal(idle.parseQuestionData('{"id":"x"}'), null);
});

test('recovery only streams active runs and never re-submits prompts', () => {
  const runs = [
    { runId: 'old', conversationId: 'c', state: 'done' },
    { runId: 'live', conversationId: 'c', state: 'running' },
  ];
  const plan = (value) => JSON.parse(JSON.stringify(value));
  assert.deepEqual(plan(idle.recoveryAction(runs, 'live')),
    { action: 'stream', runId: 'live' });
  assert.deepEqual(plan(idle.recoveryAction(runs, null)),
    { action: 'stream', runId: 'live' });
  assert.deepEqual(plan(idle.recoveryAction(runs, 'old')),
    { action: 'stream', runId: 'live' });
  assert.deepEqual(plan(idle.recoveryAction([
    { runId: 'old', conversationId: 'c', state: 'done' },
  ], 'old')), { action: 'snapshot', runId: null });
  assert.deepEqual(plan(idle.recoveryAction([], 'missing')),
    { action: 'snapshot', runId: null });
});

test('session, run and message pages validate shape and pagination', () => {
  const sessions = idle.parseSessionPage({
    sessions: [{ id: 's1', workspaceId: 'w', title: '标题', archived: false }],
    nextOffset: null,
  });
  assert.equal(sessions.sessions[0].id, 's1');
  assert.equal(sessions.nextOffset, null);
  assert.throws(() => idle.parseSessionPage({ sessions: [{}] }));

  const runs = idle.parseRunPage({
    runs: [{ runId: 'r1', conversationId: 'c1', state: 'done' }],
    nextOffset: 1,
  });
  assert.equal(runs.runs[0].state, 'done');
  assert.equal(runs.nextOffset, 1);
  assert.throws(() => idle.parseRunPage({ runs: [{ runId: 'r1' }] }));

  const messages = idle.parseMessages({
    messages: [{ role: 'user', content: '你好' }],
  });
  assert.equal(messages[0].content, '你好');
  assert.throws(() => idle.parseMessages({ messages: [{ role: 'user' }] }));
  assert.throws(() => idle.parseMessages({}));
});

test('local session history still reads, continues and deletes in isolation', () => {
  const storeSource = readFileSync(
    new URL('../../web/frontend/src/lib/store.ts', import.meta.url), 'utf8');
  const { outputText: storeText } = ts.transpileModule(storeSource, {
    compilerOptions: {
      target: ts.ScriptTarget.ES2022,
      module: ts.ModuleKind.CommonJS,
    },
  });
  const entries = new Map();
  const localStorage = {
    getItem: key => entries.get(key) ?? null,
    setItem: (key, value) => entries.set(key, String(value)),
    removeItem: key => entries.delete(key),
  };
  const module = { exports: {} };
  runInNewContext(storeText, {
    exports: module.exports, localStorage, Date, Math, JSON,
  });
  const store = module.exports;
  const first = store.createSession();
  first.title = '旧会话';
  first.messages.push({ role: 'user', content: '第一问' });
  store.saveSessions([first]);
  let loaded = store.loadSessions();
  assert.equal(loaded.length, 1);
  assert.equal(loaded[0].title, '旧会话');
  loaded[0].messages.push({ role: 'assistant', content: '第一答' });
  store.saveSessions(loaded);
  loaded = store.loadSessions();
  assert.equal(loaded[0].messages.length, 2);

  const second = store.createSession();
  second.title = '另一个';
  second.messages.push({ role: 'user', content: '保留' });
  store.saveSessions([loaded[0], second]);
  store.saveSessions(store.loadSessions().filter(
    (session) => session.id !== first.id));
  const afterDelete = store.loadSessions();
  assert.deepEqual(
    JSON.parse(JSON.stringify(afterDelete.map(
      (session) => session.title))), ['另一个']);
  assert.equal(afterDelete[0].messages[0].content, '保留');
});

test('terminal states are explicit and do not depend on a done event name', () => {
  for (const state of ['done', 'failed', 'interrupted', 'cancelled']) {
    assert.equal(idle.isTerminalRun(state), true);
    assert.equal(idle.isActiveRun(state), false);
  }
  for (const state of ['accepted', 'running', 'awaiting_permission',
    'reserved']) {
    assert.equal(idle.isActiveRun(state), true);
    assert.equal(idle.isTerminalRun(state), false);
  }
  assert.equal(idle.isTerminalRun('unknown'), false);
});
