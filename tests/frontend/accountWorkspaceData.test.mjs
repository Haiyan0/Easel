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

function makeWhoamiStore({ fetcher, storage }) {
  return {
    get: () => storage.get(),
    set: (platform, value) => {
      const cache = storage.get();
      if (value) cache[platform] = { ...value, ts: 1 };
      else delete cache[platform];
      storage.set(cache);
    },
    clear: () => storage.set({}),
    async verifyStale(platforms, opts = {}) {
      for (const platform of platforms) {
        try {
          const result = await fetcher(platform, opts.force);
          const cache = storage.get();
          cache[platform] = { ...result, ts: 1 };
          storage.set(cache);
          opts.onUpdate?.(platform, result);
        } catch { /* skip */ }
      }
    },
  };
}

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
};

function load() {
  const module = { exports: {} };
  const storage = new Map();
  runInNewContext(outputText, {
    exports: module.exports, module,
    require: id => {
      if (id === './api') return api;
      if (id === './whoami') return {
        getWhoamiCache: () => ({}), setWhoamiCache: () => {},
        clearWhoamiCache: () => {}, verifyStale: () => {},
        createWhoamiStore: makeWhoamiStore,
      };
      throw new Error(`unexpected require ${id}`);
    },
    localStorage: {
      getItem: key => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, String(value)),
      removeItem: key => storage.delete(key),
    },
    FormData, fetch: async () => { throw new Error('not used'); },
    encodeURIComponent, Promise, Array, Object, String, Number, Error,
    JSON, Math, Set, Map, URL, Date, Number,
  });
  return { data: module.exports, storage };
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
  };
}

test('control login start sends a stable request id and parses qrUrl', async () => {
  const { data } = load();
  const calls = recorder({
    '/api/login/xiaohongshu': {
      mode: 'qr', state: 'qr_ready', message: '扫码', qr: '_login/x.png',
      qrTs: 7, runId: 'run-1',
    },
  });
  const control = data.createControlWorkspaceData(calls);
  const result = await control.startLogin('xiaohongshu', {
    requestId: 'request-1', restart: false,
  });
  assert.equal(result.runId, 'run-1');
  assert.equal(result.qrUrl, '/api/accounts/xiaohongshu/qr');
  const body = JSON.parse(calls.calls[0].init.body);
  assert.deepEqual(body, { requestId: 'request-1', restart: false });
  assert.equal(control.accountQrUrl('wechat-oa', true),
    '/api/accounts/wechat-oa/mp-login/qr');
  assert.equal(control.loginStopEnabled, true);
});

test('control credentials mode is distinct from qr login', async () => {
  const { data } = load();
  const calls = recorder({
    '/api/login/wechat-oa': {
      mode: 'credentials', configured: false, message: '填凭证',
    },
  });
  const control = data.createControlWorkspaceData(calls);
  const result = await control.startLogin('wechat-oa', { requestId: 'r' });
  assert.deepEqual(JSON.parse(JSON.stringify(result)), {
    mode: 'credentials', configured: false, state: 'unknown',
    message: '填凭证', qr: '',
  });
});

test('control whoami and analytics caches are per adapter identity', async () => {
  const { data } = load();
  const a = data.createControlWorkspaceData(recorder({
    '/api/accounts/xiaohongshu/whoami': {
      loggedIn: true, name: '账号 A', avatar: 'a.png',
    },
  }));
  const b = data.createControlWorkspaceData(recorder({
    '/api/accounts/xiaohongshu/whoami': {
      loggedIn: true, name: '账号 B', avatar: 'b.png',
    },
  }));
  await a.accountWhoami('xiaohongshu', true);
  a.whoami.set('xiaohongshu', { loggedIn: true, name: '账号 A', avatar: 'a.png' });
  assert.equal(a.whoami.get().xiaohongshu.name, '账号 A');
  assert.equal(b.whoami.get().xiaohongshu, undefined);

  a.setAnalyticsCache({ xiaohongshu: { platform: 'xiaohongshu', followers: 1 } });
  assert.equal(a.getAnalyticsCache().xiaohongshu.followers, 1);
  assert.deepEqual(JSON.parse(JSON.stringify(b.getAnalyticsCache())), {});
});

test('control account parser keeps the two wechat login states separate',
  async () => {
    const { data } = load();
    const calls = recorder({
      '/api/accounts': [{
        platform: 'wechat-oa', name: '公众号', backend: 'wechat-oa',
        supported: true, loggedIn: true, note: '',
        mpLoggedIn: false, credentialConfigured: true,
      }],
    });
    const control = data.createControlWorkspaceData(calls);
    const [account] = await control.fetchAccounts();
    assert.equal(account.mpLoggedIn, false);
    assert.equal(account.credentialConfigured, true);
  });

test('control login status keeps 503 semantics and does not fake success',
  async () => {
    const { data } = load();
    const error = Object.assign(new Error('worker unavailable'), { status: 503 });
    const control = data.createControlWorkspaceData(
      recorder({ '/api/login/xiaohongshu/status': error }));
    await assert.rejects(
      () => control.loginStatus('xiaohongshu'),
      candidate => candidate.status === 503);
  });

test('control stop uses the exact login or mp-stop path', async () => {
  const { data } = load();
  const calls = recorder({
    '/api/login/zhihu/stop': {
      ok: true, stopped: true, runId: 'run-z', state: 'cancelled',
    },
    '/api/accounts/wechat-oa/mp-login/stop': {
      ok: true, stopped: false, runId: '', state: 'idle',
    },
  });
  const control = data.createControlWorkspaceData(calls);
  const normal = await control.stopLogin('zhihu', { runId: 'run-z' });
  assert.equal(calls.calls[0].path, '/api/login/zhihu/stop');
  assert.equal(normal.state, 'cancelled');
  const mp = await control.stopLogin('wechat-oa', { mp: true });
  assert.equal(calls.calls[1].path,
    '/api/accounts/wechat-oa/mp-login/stop');
  assert.equal(mp.state, 'idle');
});

test('local adapter keeps localStorage analytics and has no stop action',
  async () => {
    const { data, storage } = load();
    const local = data.localWorkspaceData;
    local.setAnalyticsCache({
      xiaohongshu: {
        platform: 'xiaohongshu', loggedIn: true, followers: 3,
      },
    });
    assert.equal(
      JSON.parse(storage.get('easel_analytics')).xiaohongshu.followers, 3);
    assert.equal(local.getAnalyticsCache().xiaohongshu.followers, 3);
    assert.equal(local.loginStopEnabled, false);
    assert.equal(local.accountQrUrl('xiaohongshu'),
      '/api/media/_login/xiaohongshu.png');
  });
