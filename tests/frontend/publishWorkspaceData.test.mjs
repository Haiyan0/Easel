import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { runInNewContext } from 'node:vm';
import { test } from 'node:test';

const require = createRequire(new URL('../../web/frontend/package.json', import.meta.url));
const ts = require('typescript');
const source = readFileSync(
  new URL('../../web/frontend/src/lib/workspaceData.ts', import.meta.url), 'utf8');
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
});

const api = {
  fetchAccounts: async () => [],
  accountWhoami: async () => ({ loggedIn: false, name: '', avatar: '' }),
  startLogin: async () => ({ mode: 'qr', state: 'starting', message: '', qr: '' }),
  loginStatus: async () => ({ mode: 'qr', state: 'unknown', message: '', qr: '' }),
  submitLoginSms: async () => ({ ok: true }),
  startMpLogin: async () => ({ mode: 'qr', state: 'starting', message: '', qr: '' }),
  mpLoginStatus: async () => ({ mode: 'qr', state: 'unknown', message: '', qr: '' }),
  logoutAccount: async () => ({ ok: true, deleted: [] }),
  getCredentials: async () => ({ configured: false, appIdMasked: '', name: '', author: '' }),
  saveCredentials: async () => ({ ok: true, message: '' }),
  fetchAnalyticsPlatforms: async () => [],
  fetchAccountAnalytics: async () => ({ platform: 'xiaohongshu' }),
  mediaUrl: path => `/api/media/${path}`,
  fetchPersonas: async () => [], fetchPersonaFiles: async () => ({ name: '', files: [] }),
  savePersonaFile: async () => ({ ok: true }), deletePersona: async () => ({ ok: true }),
  fetchOutputs: async () => [], fetchOutputContent: async () => ({}),
  fetchArtifactArchive: async () => ({ paths: [] }), deleteOutput: async () => ({ ok: true }),
  uploadFiles: async () => [], adoptOversize: async () => [],
  fetchTrends: async () => ({ trends: [], updated: 0 }),
  fetchSchedule: async () => [], fetchScheduleContext: async () => ({}),
  createSchedule: async item => item, updateSchedule: async (id, item) => item,
  deleteSchedule: async () => ({ ok: true }),
  fetchIdeas: async () => [], createIdea: async item => item,
  updateIdea: async (id, item) => item, deleteIdea: async () => ({ ok: true }),
  fetchSkills: async () => [], fetchSkillDetail: async () => ({}),
  executeSkill: async () => ({}), buildProfile: async () => ({}),
  profileBuildStatus: async () => ({}), fetchStatus: async () => ({}),
  fetchPublishingPolicy: async () => ({ mode: 'automated', automaticSubmission: true }),
  exportManualPackage: async () => ({ path: '', filename: '', published: false }),
  publishNow: async () => ({ ok: true, message: '发布成功' }),
  publishStatus: async () => ({ mode: 'publish', state: 'unknown', message: '' }),
  submitPublishSms: async () => ({ ok: true }),
  runAgent: async () => ({ response: '' }),
  streamChat: () => ({ abort() {} }),
};

function load() {
  const module = { exports: {} };
  runInNewContext(outputText, {
    exports: module.exports, module,
    require: id => {
      if (id === './api') return api;
      if (id === './whoami') return {
        getWhoamiCache: () => ({}), setWhoamiCache: () => {},
        clearWhoamiCache: () => {}, verifyStale: () => {},
        createWhoamiStore: () => ({
          get: () => ({}), set: () => {}, clear: () => {}, verifyStale: async () => {},
        }),
      };
      throw new Error(`unexpected require ${id}`);
    },
    localStorage: {
      getItem: () => null, setItem: () => {}, removeItem: () => {},
    },
    FormData, fetch: async () => { throw new Error('not used'); },
    encodeURIComponent, Promise, Array, Object, String, Number, Error,
    JSON, Math, Set, Map, URL, Date, AbortController,
  });
  return module.exports;
}

function recorder(responder = {}) {
  const calls = [];
  return {
    calls,
    async call(path, init = {}) {
      calls.push({ path, init });
      const key = Object.keys(responder).find(candidate =>
        (candidate instanceof RegExp ? candidate.test(path) : candidate === path));
      if (key === undefined) return {};
      const value = responder[key];
      if (value instanceof Error) throw value;
      return typeof value === 'function' ? value(path, init) : value;
    },
    fetchText: async () => '',
    runTask: async () => ({ state: 'done', response: 'skill response' }),
  };
}

function body(init) {
  return init?.body ? JSON.parse(init.body) : undefined;
}

test('control publishing policy is a signed read with control semantics', async () => {
  const data = load();
  const calls = recorder({
    '/api/publishing-policy': { mode: 'manual', automaticSubmission: false },
  });
  const control = data.createControlWorkspaceData(calls);
  const policy = await control.fetchPublishingPolicy();
  assert.equal(policy.mode, 'manual');
  assert.equal(policy.automaticSubmission, false);
  assert.equal(calls.calls[0].path, '/api/publishing-policy');
  assert.equal(calls.calls[0].init.method, undefined);
});

test('control publish sends exact fields and parses async admission', async () => {
  const data = load();
  const calls = recorder({
    '/api/publish/douyin': {
      async: true, pending: true, state: 'starting',
      message: '发布已启动', runId: 'run-1',
    },
  });
  const control = data.createControlWorkspaceData({
    ...calls, identityKey: 'user-1:workspace-1',
  });
  const result = await control.publishNow('douyin', {
    title: '标题', body: '正文', tags: '工具',
    media: ['project/clip.mp4'], persona: '',
  }, { requestId: 'req-1' });
  assert.equal(result.async, true);
  assert.equal(result.runId, 'run-1');
  assert.equal(result.state, 'starting');
  const sent = body(calls.calls[0].init);
  assert.deepEqual(sent, {
    title: '标题', body: '正文', tags: '工具',
    media: ['project/clip.mp4'], persona: '', requestId: 'req-1',
  });
  assert.equal(control.publishDraftScope, 'user-1:workspace-1');
});

test('control publish status and sms are bound to the current run', async () => {
  const data = load();
  const calls = recorder({
    '/api/publish/douyin/status': {
      mode: 'publish', state: 'sms_required',
      message: '请输入验证码', runId: 'run-1',
    },
    '/api/publish/douyin/sms': { ok: true, runId: 'run-1' },
  });
  const control = data.createControlWorkspaceData(calls);
  const status = await control.publishStatus('douyin');
  assert.equal(status.mode, 'publish');
  assert.equal(status.state, 'sms_required');
  assert.equal(status.message, '请输入验证码');
  assert.equal(status.runId, 'run-1');
  await control.submitPublishSms('douyin', '123456', { runId: 'run-1' });
  assert.deepEqual(body(calls.calls[1].init), {
    code: '123456', runId: 'run-1',
  });
});

test('control publish status can query the original intent request id', async () => {
  const data = load();
  const calls = recorder({
    '/api/publish/douyin/status?requestId=intent%2F1': {
      mode: 'publish', state: 'success', message: '', runId: 'run-9',
    },
  });
  const control = data.createControlWorkspaceData(calls);
  const status = await control.publishStatus('douyin', { requestId: 'intent/1' });
  assert.equal(status.runId, 'run-9');
  assert.equal(calls.calls[0].path,
    '/api/publish/douyin/status?requestId=intent%2F1');
  assert.equal(calls.calls[0].init.method, undefined);
});

test('uncertain publish errors are not treated as definite rejection', () => {
  const data = load();
  assert.equal(data.isUncertainPublishError(new Error('network down')), true);
  assert.equal(data.isUncertainPublishError({ status: 0 }), true);
  assert.equal(data.isUncertainPublishError({ status: 502 }), true);
  assert.equal(data.isUncertainPublishError({ status: 504 }), true);
  assert.equal(data.isUncertainPublishError({ status: 409 }), false);
  assert.equal(data.isUncertainPublishError({ status: 403 }), false);
  assert.equal(data.isUncertainPublishError({ status: 400 }), false);
});

test('control export only accepts an unpublished package result', async () => {
  const data = load();
  const path = '_publish/manual/easel-xiaohongshu-0123456789ab.zip';
  const calls = recorder({
    '/api/manual-publish/xiaohongshu/export': {
      path, filename: 'easel-xiaohongshu-0123456789ab.zip',
      published: false, runId: 'run-export',
    },
  });
  const control = data.createControlWorkspaceData(calls);
  const result = await control.exportManualPackage('xiaohongshu', {
    title: '标题', body: '正文', tags: '', media: [], reviewed: true,
  }, { requestId: 'export-1' });
  assert.equal(result.published, false);
  assert.equal(result.path, path);
  assert.equal(body(calls.calls[0].init).requestId, 'export-1');

  const bad = data.createControlWorkspaceData(recorder({
    '/api/manual-publish/xiaohongshu/export': {
      path, filename: 'x.zip', published: true,
    },
  }));
  await assert.rejects(() => bad.exportManualPackage('xiaohongshu', {
    title: '标题', body: '', tags: '', media: [], reviewed: true,
  }), /导出/);
});

test('control agent checks reuse managed skill runs', async () => {
  const data = load();
  const calls = recorder({
    '/api/skill': {
      runId: 'run-skill', sessionId: 'session-skill', state: 'running',
    },
  });
  const control = data.createControlWorkspaceData(calls);
  const response = await control.runAgent('检查这段内容', 'persona-a');
  assert.equal(response.response, 'skill response');
  const sent = body(calls.calls[0].init);
  assert.equal(sent.skill, 'quality-gate');
  assert.equal(sent.input, '检查这段内容');
  assert.equal(sent.persona, 'persona-a');
  assert.match(sent.turnId, /.+/);
});

test('control adaptation uses content-repurposing and reports full text', async () => {
  const data = load();
  const calls = recorder({
    '/api/skill': {
      runId: 'run-adapt', sessionId: 'session-adapt', state: 'running',
    },
  });
  const control = data.createControlWorkspaceData({
    ...calls,
    runTask: async () => ({ state: 'done', response: '===小红书===\n正文' }),
  });
  const chunks = [];
  const done = new Promise(resolve => {
    control.adaptContent('改编', 'persona-a', {
      onToken: chunk => chunks.push(chunk),
      onDone: () => resolve(true),
      onError: error => { throw error; },
      onAdmission: (runId) => { assert.equal(runId, 'run-adapt'); },
    });
  });
  assert.equal(await done, true);
  assert.deepEqual(chunks, ['===小红书===\n正文']);
  assert.equal(body(calls.calls[0].init).skill, 'content-repurposing');
});
