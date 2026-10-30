import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { runInNewContext } from 'node:vm';
import { test } from 'node:test';

const require = createRequire(new URL('../../web/frontend/package.json', import.meta.url));
const ts = require('typescript');

const source = readFileSync(
  new URL('../../web/frontend/src/lib/workspaceData.ts', import.meta.url),
  'utf8');
const { outputText } = ts.transpileModule(source, {
  compilerOptions: {
    target: ts.ScriptTarget.ES2022,
    module: ts.ModuleKind.CommonJS,
  },
});

const apiCalls = [];
const api = {
  fetchPersonas: async () => [{ name: 'A', description: 'a' }],
  fetchPersonaFiles: async () => ({ name: 'A', files: [] }),
  savePersonaFile: async () => ({ ok: true }),
  deletePersona: async () => ({ ok: true, deleted: 'A' }),
  fetchOutputs: async () => [],
  fetchOutputContent: async () => ({
    path: 'a.txt', content: 'x', kind: 'text', isBinary: false,
  }),
  fetchArtifactArchive: async () => ({ paths: [] }),
  mediaUrl: (path) => `/api/media/${path.split('/').map(encodeURIComponent).join('/')}`,
  deleteOutput: async () => ({ ok: true, deleted: 'a.txt' }),
  uploadFiles: async () => [],
  adoptOversize: async () => [],
  fetchTrends: async () => ({ trends: [], updated: 1 }),
  fetchSchedule: async () => [],
  fetchScheduleContext: async () => ({ window_days: 14, suggestions: [] }),
  createSchedule: async (item) => ({ id: 'a1b2c3d4e5f6', ...item }),
  updateSchedule: async (id, item) => ({ id, ...item }),
  deleteSchedule: async () => ({ ok: true }),
  fetchIdeas: async () => [],
  createIdea: async (item) => ({ id: 'a1b2c3d4e5f6', ...item,
    note: item.note || '', source: item.source || '',
    status: item.status || 'pending', created: 1 }),
  updateIdea: async (id, item) => ({ id, ...item }),
  deleteIdea: async () => ({ ok: true }),
};

const module = { exports: {} };
runInNewContext(outputText, {
  exports: module.exports,
  module,
  require: (id) => {
    if (id === './api') return api;
    throw new Error(`unexpected require ${id}`);
  },
  FormData,
  fetch: async () => { throw new Error('not used'); },
  encodeURIComponent,
  Promise,
  Array,
  Object,
  String,
  Number,
  Error,
  JSON,
  Math,
  Set,
  Map,
  URL,
});
const data = module.exports;

test('control workspace data exposes only implemented basic capabilities', () => {
  const control = data.createControlWorkspaceData({
    call: async () => [],
    fetchText: async () => '',
  });
  assert.equal(control.personaBuildEnabled, false);
  assert.equal(control.wechatFetchEnabled, false);
  assert.equal(control.attachmentsEnabled, true);
  assert.equal(control.oversizeMode, 'reject');
  assert.equal(control.personasEnabled, true);
  assert.equal(control.outputsEnabled, true);
  assert.equal(control.workbenchEnabled, true);
  assert.equal(control.accountsEnabled, false);
  assert.equal(control.localSettingsEnabled, false);

  const local = data.localWorkspaceData;
  assert.equal(local.personaBuildEnabled, true);
  assert.equal(local.wechatFetchEnabled, true);
  assert.equal(local.workbenchEnabled, true);
  assert.equal(local.accountsEnabled, true);
  assert.equal(local.oversizeMode, 'adopt');
  assert.equal(local.localSettingsEnabled, true);
});

test('control workspace data uses identity call for reads, writes and uploads', async () => {
  const calls = [];
  const control = data.createControlWorkspaceData({
    call: async (path, init) => {
      calls.push({ path, init });
      if (path === '/api/personas') {
        return [{ name: '画像', description: '说明' }];
      }
      if (path === '/api/outputs') return [];
      if (path === '/api/upload/limits') return { ok: true, max_mb: 50 };
      if (path === '/api/upload') {
        return { ok: true, files: [{ id: 'i', name: 'a.txt', path: '_inbox/a' }] };
      }
      if (path.startsWith('/api/media/')) return null;
      return { ok: true, name: '画像', files: [] };
    },
    fetchText: async () => '<html></html>',
  });

  const personas = await control.fetchPersonas();
  assert.equal(personas.length, 1);
  assert.equal(personas[0].name, '画像');
  assert.equal(personas[0].description, '说明');
  await control.fetchOutputs();
  assert.equal((await control.uploadLimits()).max_mb, 50);
  const form = new FormData();
  form.append('sessionId', 's1');
  const upload = await control.uploadFiles([new File(['x'], 'a.txt')], 's1');
  assert.equal(upload[0].path, '_inbox/a');
  const uploadCall = calls.find((item) => item.path === '/api/upload');
  assert.equal(uploadCall.init.method, 'POST');
  assert.ok(uploadCall.init.body instanceof FormData);
  assert.equal(uploadCall.init.headers, undefined,
    'FormData 不能手工设置 Content-Type');
  assert.equal(control.mediaUrl('a/b.png'), '/api/media/a/b.png');
});

test('oversize files never use local adoption in control', () => {
  const big = { size: 51 * 1024 * 1024 };
  const small = { size: 1024 };
  const parts = data.partitionUploads([big, small], 50);
  assert.equal(parts.accepted.length, 1);
  assert.equal(parts.oversize.length, 1);
  assert.match(data.oversizeUploadMessage(50, 'reject'), /超过 50MB/);
  assert.match(data.oversizeUploadMessage(50, 'reject'), /不支持.*本地|本地.*不可用/);
  assert.match(data.oversizeUploadMessage(50, 'adopt'), /本地素材/);
});

test('errors keep 401/403/404/413/503 semantics', () => {
  const cases = [
    [{ status: 401 }, /登录/],
    [{ status: 403 }, /无权|权限/],
    [{ status: 404 }, /不存在/],
    [{ status: 409 }, /冲突|版本/],
    [{ status: 413 }, /超过|上限/],
    [{ status: 429 }, /频繁|稍后/],
    [{ status: 503 }, /暂不可用/],
    [{ status: 502 }, /暂不可用/],
    [{ status: 0 }, /网络/],
    [new Error('boom'), /boom/],
  ];
  for (const [error, pattern] of cases) {
    assert.match(data.workspaceErrorMessage(error, '失败'), pattern);
  }
});

test('control parsers reject malformed payloads instead of faking empty data', async () => {
  const bad = data.createControlWorkspaceData({
    call: async () => ({ not: 'an array' }),
    fetchText: async () => '',
  });
  await assert.rejects(bad.fetchPersonas(), /画像列表/);
  await assert.rejects(bad.fetchOutputs(), /内容库/);
  await assert.rejects(bad.fetchIdeas(), /选题/);
  await assert.rejects(bad.fetchSchedule(), /日历/);
  const badFiles = data.createControlWorkspaceData({
    call: async () => ({ name: 'A', files: 'bad' }),
    fetchText: async () => '',
  });
  await assert.rejects(badFiles.fetchPersonaFiles('A'), /画像文件/);
  const badTrends = data.createControlWorkspaceData({
    call: async () => ({ trends: 'bad', updated: 1 }),
    fetchText: async () => '',
  });
  await assert.rejects(badTrends.fetchTrends('weibo', 12), /热点/);
  const badContext = data.createControlWorkspaceData({
    call: async () => ({ suggestions: 'bad' }),
    fetchText: async () => '',
  });
  await assert.rejects(badContext.fetchScheduleContext(14), /日历摘要/);
});

test('control trend parser preserves stale cache metadata', async () => {
  const control = data.createControlWorkspaceData({
    call: async () => ({
      trends: [{
        platform: 'weibo', label: '微博', ok: false, stale: true,
        cached_at: 1700000000,
        items: [{ title: '旧热点', hot: '', url: '' }],
      }],
      updated: 1700000000,
      stale: true,
      failed_platforms: ['weibo'],
    }),
    fetchText: async () => '',
  });
  const result = await control.fetchTrends('weibo', 15);
  assert.equal(result.stale, true);
  assert.equal(result.updated, 1700000000);
  assert.equal(result.trends[0].stale, true);
  assert.equal(result.trends[0].cached_at, 1700000000);
  assert.deepEqual(Array.from(result.failed_platforms), ['weibo']);
});

test('control workbench methods use identity call with exact paths and bodies', async () => {
  const calls = [];
  const idea = {
    id: 'a1b2c3d4e5f6', title: '选题', note: '', source: '',
    status: 'pending', created: 1,
  };
  const schedule = {
    id: 'b1b2c3d4e5f6', title: '排期', date: '2026-10-30',
    platform: '', time: '', status: 'idea', note: '',
  };
  const control = data.createControlWorkspaceData({
    call: async (path, init) => {
      calls.push({ path, init });
      if (path.startsWith('/api/trends')) {
        return {
          trends: [{
            platform: 'weibo', label: '微博', ok: true,
            items: [{ title: '热搜', hot: '1', url: '' }],
          }], updated: 2,
        };
      }
      if (path.startsWith('/api/schedule/context')) {
        return { window_days: 14, suggestions: ['保持节奏'] };
      }
      if (path === '/api/schedule') {
        return init?.method === 'POST' ? schedule : [schedule];
      }
      if (path.startsWith('/api/schedule/')) return schedule;
      if (path === '/api/ideas') {
        return init?.method === 'POST' ? idea : [idea];
      }
      if (path.startsWith('/api/ideas/')) return idea;
      throw new Error(`unexpected ${path}`);
    },
    fetchText: async () => '',
  });

  const trends = await control.fetchTrends('weibo,douyin', 15);
  assert.equal(trends.updated, 2);
  assert.equal(trends.trends[0].items[0].title, '热搜');
  assert.equal(calls.at(-1).path,
    '/api/trends?platforms=weibo%2Cdouyin&limit=15');

  assert.equal((await control.fetchSchedule())[0].id, schedule.id);
  assert.equal((await control.fetchIdeas())[0].id, idea.id);
  const context = await control.fetchScheduleContext(14);
  assert.equal(context.window_days, 14);
  assert.deepEqual(Array.from(context.suggestions || []), ['保持节奏']);
  assert.equal(calls.at(-1).path, '/api/schedule/context?days=14');

  await control.createIdea({ title: '新选题', status: 'pending' });
  let call = calls.at(-1);
  assert.equal(call.path, '/api/ideas');
  assert.equal(call.init.method, 'POST');
  assert.deepEqual(JSON.parse(call.init.body),
    { title: '新选题', status: 'pending' });

  await control.updateIdea('a1b2c3d4e5f6', { title: '改选题' });
  call = calls.at(-1);
  assert.equal(call.path, '/api/ideas/a1b2c3d4e5f6');
  assert.equal(call.init.method, 'PUT');

  await control.deleteIdea('a1b2c3d4e5f6');
  call = calls.at(-1);
  assert.equal(call.init.method, 'DELETE');

  await control.createSchedule({
    title: '新排期', date: '2026-10-30', platform: '', time: '',
    status: 'idea', note: '',
  });
  call = calls.at(-1);
  assert.equal(call.path, '/api/schedule');
  assert.equal(call.init.method, 'POST');
  assert.deepEqual(JSON.parse(call.init.body), {
    title: '新排期', date: '2026-10-30', platform: '', time: '',
    status: 'idea', note: '',
  });

  await control.updateSchedule('b1b2c3d4e5f6', {
    title: '改排期', date: '2026-10-31',
  });
  call = calls.at(-1);
  assert.equal(call.path, '/api/schedule/b1b2c3d4e5f6');
  assert.equal(call.init.method, 'PUT');

  await control.deleteSchedule('b1b2c3d4e5f6');
  call = calls.at(-1);
  assert.equal(call.init.method, 'DELETE');
});
