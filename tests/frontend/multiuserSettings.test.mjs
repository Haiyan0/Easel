import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { runInNewContext } from 'node:vm';
import { test } from 'node:test';

const require = createRequire(new URL('../../web/frontend/package.json', import.meta.url));
const ts = require('typescript');

const settingsSource = readFileSync(
  new URL('../../web/frontend/src/lib/controlSettings.ts', import.meta.url), 'utf8');
const controlSettings = loadTs(settingsSource, {});

function loadTs(source, requires) {
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: {
      target: ts.ScriptTarget.ES2022,
      module: ts.ModuleKind.CommonJS,
      jsx: ts.JsxEmit.React,
    },
  });
  const module = { exports: {} };
  runInNewContext(outputText, {
    exports: module.exports,
    module,
    require: (id) => {
      if (id in requires) return requires[id];
      throw new Error(`unexpected require ${id}`);
    },
    console,
    Date,
    JSON,
    Number,
    Object,
    Array,
    String,
    Math,
    Error,
    Promise,
    encodeURIComponent,
    setTimeout,
    clearTimeout,
    navigator: { clipboard: { writeText: async () => {} } },
  });
  return module.exports;
}

function createView(path, requires = {}) {
  const source = readFileSync(new URL(path, import.meta.url), 'utf8');
  const state = [];
  const effects = [];
  let cursor = 0;
  let effectCursor = 0;
  const react = {
    Fragment: 'fragment',
    createElement: (type, props, ...children) => ({
      type, props: props || {}, children: children.flat(),
    }),
    useState: (initial) => {
      const index = cursor++;
      if (!(index in state)) state[index] = initial;
      return [state[index], (value) => {
        state[index] = typeof value === 'function' ? value(state[index]) : value;
      }];
    },
    useRef: (initial) => {
      const index = cursor++;
      if (!(index in state)) state[index] = { current: initial };
      return state[index];
    },
    useEffect: (callback, dependencies) => {
      const index = effectCursor++;
      const previous = effects[index];
      const changed = !previous || !dependencies
        || dependencies.some((value, position) =>
          !Object.is(value, previous.dependencies[position]));
      if (changed) {
        previous?.cleanup?.();
        effects[index] = { dependencies, cleanup: callback() };
      }
    },
  };
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: {
      target: ts.ScriptTarget.ES2022,
      module: ts.ModuleKind.CommonJS,
      jsx: ts.JsxEmit.React,
    },
  });
  const module = { exports: {} };
  runInNewContext(outputText, {
    exports: module.exports,
    module,
    React: react,
    require: (id) => {
      if (id === 'react') return react;
      if (requires[id]) return requires[id];
      if (id.endsWith('controlSettings')) return controlSettings;
      throw new Error(`unexpected require ${id}`);
    },
    console,
    Date,
    JSON,
    Number,
    Object,
    Array,
    String,
    Math,
    Error,
    Promise,
    encodeURIComponent,
    setTimeout,
    clearTimeout,
    navigator: { clipboard: { writeText: async () => {} } },
  });
  return {
    render(props) {
      cursor = 0;
      effectCursor = 0;
      return module.exports.default(props);
    },
    unmount() {
      effects.forEach((effect) => effect?.cleanup?.());
      state.length = 0;
      effects.length = 0;
    },
  };
}

const settle = async () => {
  for (let index = 0; index < 12; index += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
};

function nodes(tree, type) {
  if (!tree || typeof tree !== 'object') return [];
  return [
    ...(tree.type === type ? [tree] : []),
    ...(tree.children || []).flatMap((child) => nodes(child, type)),
  ];
}

function text(tree) {
  if (typeof tree === 'string') return tree;
  if (!tree || typeof tree !== 'object') return '';
  return (tree.children || []).map(text).join('');
}

function field(tree, label) {
  return nodes(tree, 'input').find((node) => node.props['aria-label'] === label);
}

function area(tree, label) {
  return nodes(tree, 'textarea').find((node) => node.props['aria-label'] === label);
}

function select(tree, label) {
  return nodes(tree, 'select').find((node) => node.props['aria-label'] === label);
}

function button(tree, label) {
  return nodes(tree, 'button').find((node) => text(node).includes(label));
}

function change(node, value) {
  node.props.onChange({ target: { value } });
}

function channel(overrides = {}) {
  return {
    configured: false,
    source: 'none',
    version: 0,
    mode: 'none',
    provider: null,
    model: null,
    base_url: null,
    protocol: null,
    options: {},
    broker_adapter: null,
    broker_supported: false,
    override_allowed: true,
    ...overrides,
  };
}

function key(env, label, overrides = {}) {
  return {
    env,
    label,
    required: true,
    secret: true,
    choices: [],
    ...overrides,
  };
}

function provider(name, keys, overrides = {}) {
  return {
    name,
    keys,
    override_allowed: true,
    broker_adapter: null,
    ...overrides,
  };
}

function providers() {
  return {
    speech: {
      'openai-compatible': provider('OpenAI 兼容', [
        key('VOICE_API_KEY', 'API Key'),
        key('VOICE_BASE_URL', 'API 根地址', { secret: false }),
      ], { broker_adapter: 'openai-compatible' }),
      minimax: provider('MiniMax', [
        key('MINIMAX_API_KEY', 'API Key'),
        key('MINIMAX_GROUP_ID', 'Group ID', { secret: false }),
        key('MINIMAX_BASE_URL', '根地址', {
          required: false, secret: false,
        }),
      ]),
    },
    image: {},
    video: {},
    music: {},
    chat: {},
  };
}

function modelSettings(channels = {}) {
  return {
    version: 3,
    channels: {
      speech: channel(),
      image: channel(),
      video: channel(),
      music: channel(),
      chat: channel({ override_allowed: false }),
      ...channels,
    },
    providers: providers(),
  };
}

const harnessSummary = {
  locked: true,
  configured: true,
  runtime: 'acp',
  revision: 7,
  model: 'managed-chat',
  sandbox: 'workspace-write',
  timeout: 60,
  codexAutoApprove: false,
  unavailableReason: null,
};

function managedSettings(overrides = {}) {
  return {
    version: 3,
    defaults: {
      speech: {
        provider: 'openai-compatible',
        model: 'tts-default',
        base_url: 'https://provider.example/v1',
        protocol: 'openai',
        options: { speed: 1 },
        allowed_models: ['tts-default', 'tts-alt'],
        credential_configured: true,
      },
      ...overrides,
    },
  };
}

function user(overrides = {}) {
  return {
    userId: 'user-1',
    workspaceId: 'workspace-1',
    displayName: '成员一',
    role: 'member',
    disabled: false,
    createdAt: 1000,
    ...overrides,
  };
}

function invite(overrides = {}) {
  return {
    id: 'invite-1',
    userId: 'user-1',
    createdAt: 1000,
    expiresAt: 9999999999,
    redeemedAt: null,
    revokedAt: null,
    ...overrides,
  };
}

function ctx(overrides = {}) {
  return {
    userId: 'user-a',
    workspaceId: 'workspace-a',
    role: 'member',
    csrfToken: 'csrf-a',
    capabilities: ['workspace'],
    ...overrides,
  };
}

function recorder(handler) {
  const requests = [];
  const call = async (path, init) => {
    requests.push({ path, init });
    return handler(path, init, requests.length);
  };
  return { call, requests };
}

// --------------------------------------------------------------------------
// 协议转换
// --------------------------------------------------------------------------

test('parsers keep provenance fields and reject malformed payloads', () => {
  const parsed = controlSettings.parseModelSettings(modelSettings({
    speech: channel({
      configured: true, source: 'admin', version: 4, mode: 'inherited',
      provider: 'openai-compatible', model: 'tts-default',
      base_url: 'https://provider.example/v1', protocol: 'openai',
      options: { speed: 1.2 }, allowed_models: ['tts-default'],
      broker_adapter: 'openai-compatible', broker_supported: true,
    }),
  }));
  assert.equal(parsed.version, 3);
  assert.equal(parsed.channels.speech.source, 'admin');
  assert.equal(parsed.channels.speech.mode, 'inherited');
  assert.equal(parsed.channels.speech.version, 4);
  assert.equal(parsed.channels.speech.broker_supported, true);

  assert.throws(() => controlSettings.parseModelSettings({
    version: 1, channels: { speech: channel({ source: 'evil' }) },
  }), /settings_invalid/);
  assert.throws(() => controlSettings.parseHarnessSummary({
    locked: true, configured: 'yes',
  }), /settings_invalid/);
  assert.throws(() => controlSettings.parseAdminUserPage({
    users: [{ ...user(), disabled: 'no' }], nextOffset: null,
  }), /settings_invalid/);
  assert.throws(() => controlSettings.parseManagedSettings({
    version: 1,
    defaults: { speech: { provider: 'p', model: 'm', base_url: 'u',
      protocol: 'p', options: {}, credential_configured: 1 } },
  }), /settings_invalid/);
});

test('route builders send raw snake_case protocol and omit an empty secret', () => {
  const complete = controlSettings.buildPersonalRoute({
    routeKind: 'complete',
    provider: ' openai-compatible ',
    model: ' tts-personal ',
    baseUrl: ' https://personal.example/v1 ',
    protocol: ' openai ',
    secret: '',
    options: '{"speed": 1.2}',
  });
  assert.deepEqual(JSON.parse(JSON.stringify(complete)), {
    route_kind: 'complete',
    provider: 'openai-compatible',
    model: 'tts-personal',
    base_url: 'https://personal.example/v1',
    protocol: 'openai',
    options: { speed: 1.2 },
  });
  assert.equal('secret' in complete, false);

  const withSecret = controlSettings.buildPersonalRoute({
    routeKind: 'complete',
    provider: 'openai-compatible',
    model: 'tts-personal',
    baseUrl: 'https://personal.example/v1',
    protocol: 'openai',
    secret: 'secret-value',
    options: '',
  });
  assert.equal(withSecret.secret, 'secret-value');

  const managed = controlSettings.buildPersonalRoute({
    routeKind: 'managed-model',
    provider: 'ignored',
    model: 'tts-alt',
    baseUrl: 'https://ignored.example/v1',
    protocol: 'ignored',
    secret: 'must-not-leak',
    options: '{"speed": 1.1}',
  });
  assert.deepEqual(JSON.parse(JSON.stringify(managed)), {
    route_kind: 'managed-model',
    model: 'tts-alt',
    options: { speed: 1.1 },
  });

  const adminDefault = controlSettings.buildManagedDefault({
    routeKind: 'complete',
    provider: 'openai-compatible',
    model: 'tts-default',
    baseUrl: 'https://provider.example/v1',
    protocol: 'openai',
    secret: '',
    options: '{}',
    allowedModels: 'tts-default, tts-alt, tts-default',
  });
  assert.equal('secret' in adminDefault, false);
  assert.deepEqual(JSON.parse(JSON.stringify(
    adminDefault.allowed_models)), ['tts-default', 'tts-alt']);

  assert.throws(() => controlSettings.parseOptionsText(
    '{"headers": {"api-key": "secret"}}'), /options_invalid/);
  assert.throws(() => controlSettings.parseOptionsText(
    '{"speed": 1e999}'), /options_invalid/);
});

test('form conversion never reads a secret back from public settings', () => {
  const settings = controlSettings.parseModelSettings(modelSettings({
    speech: channel({
      configured: true, source: 'user', version: 2, mode: 'complete',
      provider: 'openai-compatible', model: 'tts-personal',
      base_url: 'https://personal.example/v1', protocol: 'openai',
      options: { speed: 1.1, voice: 'alloy' },
    }),
  }));
  const form = controlSettings.formFromChannel(settings, 'speech');
  assert.equal(form.routeKind, 'complete');
  assert.equal(form.provider, 'openai-compatible');
  assert.equal(form.model, 'tts-personal');
  assert.equal(form.secret, '');
  assert.deepEqual(JSON.parse(form.options), { speed: 1.1, voice: 'alloy' });
  assert.equal(controlSettings.sourceLabel(
    settings.channels.speech), '个人配置（complete · v2）');
});

// --------------------------------------------------------------------------
// 个人设置组件
// --------------------------------------------------------------------------

test('personal settings load provenance and save an empty key without secret', async () => {
  const settings = modelSettings({
    speech: channel({
      configured: true, source: 'user', version: 2, mode: 'complete',
      provider: 'openai-compatible', model: 'tts-personal',
      base_url: 'https://personal.example/v1', protocol: 'openai',
      options: { speed: 1.1 }, broker_adapter: 'openai-compatible',
      broker_supported: true,
    }),
  });
  const { call, requests } = recorder((path) => {
    if (path === '/api/settings/models') return settings;
    if (path === '/api/settings/harness') return harnessSummary;
    if (path === '/api/settings/models/save') return { ok: true };
    throw new Error(`unexpected ${path}`);
  });
  const view = createView('../../web/frontend/src/components/ControlSettingsPage.tsx');
  view.render({ ctx: ctx(), call });
  await settle();
  let tree = view.render({ ctx: ctx(), call });
  assert.match(text(tree), /个人设置/);
  assert.match(text(tree), /受管底座（只读）/);
  assert.match(text(tree), /个人配置（complete · v2）/);
  assert.match(text(tree), /锁定：是/);
  assert.match(text(tree), /运行时：acp/);
  assert.match(text(tree), /转发适配已声明/);

  change(field(tree, '模型'), 'tts-personal-v2');
  tree = view.render({ ctx: ctx(), call });
  button(tree, '保存个人配置').props.onClick();
  await settle();
  const save = requests.find((item) => item.path === '/api/settings/models/save');
  assert.ok(save);
  assert.equal(save.init.method, 'POST');
  const body = JSON.parse(save.init.body);
  assert.equal(body.channel, 'speech');
  assert.equal(body.route.route_kind, 'complete');
  assert.equal(body.route.model, 'tts-personal-v2');
  assert.equal('secret' in body.route, false);
  assert.equal('secret' in body.route, false);
  tree = view.render({ ctx: ctx(), call });
  assert.match(text(tree), /已保存个人配置/);
  assert.equal(field(tree, 'API Key').props.value, '');
});

test('personal complete route sends a newly typed key and never stores it', async () => {
  const { call, requests } = recorder((path) => {
    if (path === '/api/settings/models') return modelSettings();
    if (path === '/api/settings/harness') return harnessSummary;
    if (path === '/api/settings/models/save') return { ok: true };
    throw new Error(`unexpected ${path}`);
  });
  const view = createView('../../web/frontend/src/components/ControlSettingsPage.tsx');
  view.render({ ctx: ctx(), call });
  await settle();
  let tree = view.render({ ctx: ctx(), call });
  for (const [label, value] of [
    ['供应商', 'openai-compatible'],
    ['模型', 'tts-new'],
    ['API 地址', 'https://personal.example/v2'],
    ['协议', 'openai'],
    ['API Key', 'only-in-memory-secret'],
  ]) {
    change(field(tree, label), value);
    tree = view.render({ ctx: ctx(), call });
  }
  button(tree, '保存个人配置').props.onClick();
  await settle();
  const save = requests.find((item) => item.path === '/api/settings/models/save');
  const body = JSON.parse(save.init.body);
  assert.equal(body.route.secret, 'only-in-memory-secret');
  tree = view.render({ ctx: ctx(), call });
  assert.equal(field(tree, 'API Key').props.value, '');
});

test('a stale A settings response cannot fill the B form', async () => {
  let releaseA;
  const aPayload = modelSettings({
    speech: channel({
      configured: true, source: 'user', version: 9, mode: 'complete',
      provider: 'a-provider', model: 'a-secret-model',
      base_url: 'https://a.example/v1', protocol: 'openai', options: {},
    }),
  });
  const bPayload = modelSettings({
    speech: channel({
      configured: true, source: 'admin', version: 2, mode: 'inherited',
      provider: 'b-provider', model: 'b-model',
      base_url: 'https://b.example/v1', protocol: 'openai', options: {},
    }),
  });
  let modelCalls = 0;
  const call = async (path) => {
    if (path === '/api/settings/harness') return harnessSummary;
    if (path === '/api/settings/models') {
      modelCalls += 1;
      if (modelCalls === 1) {
        return new Promise((resolve) => { releaseA = resolve; });
      }
      return bPayload;
    }
    throw new Error(`unexpected ${path}`);
  };
  const view = createView('../../web/frontend/src/components/ControlSettingsPage.tsx');
  view.render({ ctx: ctx({ userId: 'a' }), call });
  await settle();
  view.render({ ctx: ctx({ userId: 'b', workspaceId: 'workspace-b' }), call });
  await settle();
  let tree = view.render({
    ctx: ctx({ userId: 'b', workspaceId: 'workspace-b' }), call,
  });
  assert.match(text(tree), /b-model/);
  releaseA(aPayload);
  await settle();
  tree = view.render({
    ctx: ctx({ userId: 'b', workspaceId: 'workspace-b' }), call,
  });
  const body = text(tree);
  assert.doesNotMatch(body, /a-secret-model/);
  assert.doesNotMatch(body, /a-provider/);
  assert.match(body, /b-model/);
});

// --------------------------------------------------------------------------
// 管理员设置组件
// --------------------------------------------------------------------------

test('managed-model form edits only personal raw values and keeps following', async () => {
  const settings = modelSettings({
    speech: channel({
      configured: true, source: 'user', version: 2, mode: 'managed-model',
      provider: 'openai-compatible', model: 'admin-v2',
      base_url: 'https://provider.example/v1', protocol: 'openai',
      options: { speed: 1.7, voice: 'alloy' },
      allowed_models: ['admin-v2', 'admin-v3'],
      personal_model: null,
      personal_options: { speed: 1.7 },
    }),
  });
  const { call, requests } = recorder((path) => {
    if (path === '/api/settings/models') return settings;
    if (path === '/api/settings/harness') return harnessSummary;
    if (path === '/api/settings/models/save') return { ok: true };
    throw new Error(`unexpected ${path}`);
  });
  const view = createView('../../web/frontend/src/components/ControlSettingsPage.tsx');
  view.render({ ctx: ctx(), call });
  await settle();
  let tree = view.render({ ctx: ctx(), call });
  assert.equal(select(tree, '受管模型').props.value, '');
  assert.deepEqual(JSON.parse(area(tree, '非敏感参数（JSON）').props.value),
    { speed: 1.7 });

  button(tree, '保存个人配置').props.onClick();
  await settle();
  const save = requests.find((item) => item.path === '/api/settings/models/save');
  const route = JSON.parse(save.init.body).route;
  assert.deepEqual(route, {
    route_kind: 'managed-model',
    options: { speed: 1.7 },
  });
  assert.equal('model' in route, false);
});

test('explicit managed model is preserved even when equal to the default', async () => {
  const settings = modelSettings({
    speech: channel({
      configured: true, source: 'user', version: 3, mode: 'managed-model',
      provider: 'openai-compatible', model: 'admin-v2',
      base_url: 'https://provider.example/v1', protocol: 'openai',
      options: { voice: 'nova' },
      allowed_models: ['admin-v2', 'admin-v3'],
      personal_model: 'admin-v2',
      personal_options: { voice: 'nova' },
    }),
  });
  const { call, requests } = recorder((path) => {
    if (path === '/api/settings/models') return settings;
    if (path === '/api/settings/harness') return harnessSummary;
    if (path === '/api/settings/models/save') return { ok: true };
    throw new Error(`unexpected ${path}`);
  });
  const view = createView('../../web/frontend/src/components/ControlSettingsPage.tsx');
  view.render({ ctx: ctx(), call });
  await settle();
  const tree = view.render({ ctx: ctx(), call });
  assert.equal(select(tree, '受管模型').props.value, 'admin-v2');
  button(tree, '保存个人配置').props.onClick();
  await settle();
  const route = JSON.parse(requests.find(
    (item) => item.path === '/api/settings/models/save').init.body).route;
  assert.equal(route.model, 'admin-v2');
  assert.deepEqual(route.options, { voice: 'nova' });
});

test('multi-field provider sends a typed bundle and never reads secrets back', async () => {
  const { call, requests } = recorder((path) => {
    if (path === '/api/settings/models') return modelSettings();
    if (path === '/api/settings/harness') return harnessSummary;
    if (path === '/api/settings/models/save') return { ok: true };
    throw new Error(`unexpected ${path}`);
  });
  const view = createView('../../web/frontend/src/components/ControlSettingsPage.tsx');
  view.render({ ctx: ctx(), call });
  await settle();
  let tree = view.render({ ctx: ctx(), call });
  change(field(tree, '供应商'), ' minimax ');
  change(field(tree, '模型'), 'speech-01');
  change(field(tree, 'API 地址'), 'https://minimax.example/v1');
  change(field(tree, '协议'), 'minimax');
  tree = view.render({ ctx: ctx(), call });
  assert.ok(field(tree, '凭证 API Key'), '缺少 API Key 凭证输入');
  assert.ok(field(tree, '凭证 Group ID'), '缺少 Group ID 凭证输入');
  assert.equal(field(tree, 'API Key'), undefined,
    '多字段凭证不得退回单个字符串输入');

  change(field(tree, '凭证 API Key'), 'minimax-key-value');
  change(field(tree, '凭证 Group ID'), 'minimax-group-value');
  tree = view.render({ ctx: ctx(), call });
  button(tree, '保存个人配置').props.onClick();
  await settle();
  const route = JSON.parse(requests.find(
    (item) => item.path === '/api/settings/models/save').init.body).route;
  assert.deepEqual(route.secret, {
    MINIMAX_API_KEY: 'minimax-key-value',
    MINIMAX_GROUP_ID: 'minimax-group-value',
  });
});

test('multi-field credentials reject partial input and allow all blank reuse', async () => {
  const settings = modelSettings({
    speech: channel({
      configured: true, source: 'user', version: 1, mode: 'complete',
      provider: 'minimax', model: 'speech-01',
      base_url: 'https://minimax.example/v1', protocol: 'minimax',
      options: {},
    }),
  });
  const { call, requests } = recorder((path) => {
    if (path === '/api/settings/models') return settings;
    if (path === '/api/settings/harness') return harnessSummary;
    if (path === '/api/settings/models/save') return { ok: true };
    throw new Error(`unexpected ${path}`);
  });
  const view = createView('../../web/frontend/src/components/ControlSettingsPage.tsx');
  view.render({ ctx: ctx(), call });
  await settle();
  let tree = view.render({ ctx: ctx(), call });
  assert.ok(field(tree, '凭证 API Key'));
  assert.ok(field(tree, '凭证 Group ID'));

  change(field(tree, '凭证 API Key'), 'only-one-field');
  tree = view.render({ ctx: ctx(), call });
  button(tree, '保存个人配置').props.onClick();
  await settle();
  assert.equal(requests.filter(
    (item) => item.path === '/api/settings/models/save').length, 0);
  tree = view.render({ ctx: ctx(), call });
  assert.match(text(tree), /请填写全部必需凭证/);

  change(field(tree, '凭证 API Key'), '');
  tree = view.render({ ctx: ctx(), call });
  button(tree, '保存个人配置').props.onClick();
  await settle();
  const route = JSON.parse(requests.find(
    (item) => item.path === '/api/settings/models/save').init.body).route;
  assert.equal('secret' in route, false);
});

test('admin member and invite pages can load later rows', async () => {
  const firstUsers = Array.from({ length: 50 }, (_, index) => user({
    userId: `user-${index}`, displayName: `成员 ${index}`,
  }));
  const lastUser = user({ userId: 'user-last', displayName: '末尾成员' });
  const firstInvites = Array.from({ length: 50 }, (_, index) => invite({
    id: `invite-${index}`, userId: firstUsers[index].userId,
  }));
  const oldInvite = invite({ id: 'old-invite', userId: 'user-last' });
  const { call, requests } = recorder((path) => {
    if (path === '/api/admin/managed-config') return managedSettings();
    if (path === '/api/admin/users?limit=50&offset=0') {
      return { users: firstUsers, nextOffset: 50 };
    }
    if (path === '/api/admin/users?limit=50&offset=50') {
      return { users: [lastUser], nextOffset: null };
    }
    if (path === '/api/admin/invites?limit=50&offset=0') {
      return { invites: firstInvites, nextOffset: 50 };
    }
    if (path === '/api/admin/invites?limit=50&offset=50') {
      return { invites: [oldInvite], nextOffset: null };
    }
    if (path === '/api/admin/invites' && init?.method === 'POST') {
      return { id: 'new-invite', token: 'new-token', expiresAt: 9999999999 };
    }
    if (path.startsWith('/api/admin/users/') && path.endsWith('/revoke-sessions')) {
      return { revoked: 0 };
    }
    if (path.startsWith('/api/admin/invites/') && path.endsWith('old-invite')) {
      return undefined;
    }
    throw new Error(`unexpected ${path}`);
  });
  const view = createView('../../web/frontend/src/components/AdminSettingsPage.tsx');
  view.render({ ctx: ctx({ role: 'admin' }), call });
  await settle();
  let tree = view.render({ ctx: ctx({ role: 'admin' }), call });
  assert.ok(button(tree, '加载更多成员'));
  assert.ok(button(tree, '加载更多邀请'));
  assert.equal(text(tree).includes('末尾成员'), false);
  assert.equal(text(tree).includes('old-invite'), false);

  button(tree, '加载更多成员').props.onClick();
  await settle();
  tree = view.render({ ctx: ctx({ role: 'admin' }), call });
  assert.match(text(tree), /末尾成员/);
  assert.ok(button(tree, '禁用 末尾成员'));
  assert.ok(nodes(tree, 'option').some((node) =>
    node.props.value === 'user-last'), '邀请目标选择未包含后页成员');

  // 选择后页成员后重新读取：目标不能被第一页悄悄替换
  const inviteSelect = select(tree, '邀请成员');
  inviteSelect.props.onChange({ target: { value: 'user-last' } });
  tree = view.render({ ctx: ctx({ role: 'admin' }), call });
  button(tree, '重新读取').props.onClick();
  await settle();
  tree = view.render({ ctx: ctx({ role: 'admin' }), call });
  assert.equal(select(tree, '邀请成员').props.value, 'user-last');
  assert.ok(nodes(tree, 'option').some((node) =>
    node.props.value === 'user-last'));
  button(tree, '创建邀请').props.onClick();
  await settle();
  const issued = requests.find((item) =>
    item.path === '/api/admin/invites' && item.init?.method === 'POST');
  assert.deepEqual(JSON.parse(issued.init.body), { userId: 'user-last' });

  tree = view.render({ ctx: ctx({ role: 'admin' }), call });
  button(tree, '加载更多邀请').props.onClick();
  await settle();
  tree = view.render({ ctx: ctx({ role: 'admin' }), call });
  assert.ok(button(tree, '撤销邀请 old-invite'));
  assert.ok(requests.some((item) =>
    item.path === '/api/admin/users?limit=50&offset=50'));
  assert.ok(requests.some((item) =>
    item.path === '/api/admin/invites?limit=50&offset=50'));
  assert.equal(requests.some((item) =>
    item.path.includes('limit=200') || item.path.includes('limit=1000')),
  false);
});

test('member never renders admin write controls or calls admin APIs', async () => {
  const { call, requests } = recorder(() => {
    throw new Error('must not call');
  });
  const view = createView('../../web/frontend/src/components/AdminSettingsPage.tsx');
  view.render({ ctx: ctx({ role: 'member' }), call });
  await settle();
  const tree = view.render({ ctx: ctx({ role: 'member' }), call });
  assert.match(text(tree), /无权访问管理员设置/);
  assert.equal(button(tree, '保存全体默认'), undefined);
  assert.equal(button(tree, '创建成员'), undefined);
  assert.deepEqual(requests, []);
});

test('admin default conflict keeps the form and warns without overwriting', async () => {
  const { call, requests } = recorder((path) => {
    if (path === '/api/admin/managed-config') return managedSettings();
    if (path.startsWith('/api/admin/users?')) {
      return { users: [user()], nextOffset: null };
    }
    if (path.startsWith('/api/admin/invites?')) {
      return { invites: [], nextOffset: null };
    }
    if (path === '/api/admin/managed-config') return managedSettings();
    throw new Error(`unexpected ${path}`);
  });
  const conflictCall = async (path, init) => {
    if (path === '/api/admin/managed-config'
        && init?.method === 'POST') {
      requests.push({ path, init });
      const error = new Error('version_conflict');
      error.status = 409;
      throw error;
    }
    return call(path, init);
  };
  const view = createView('../../web/frontend/src/components/AdminSettingsPage.tsx');
  view.render({ ctx: ctx({ role: 'admin' }), call: conflictCall });
  await settle();
  let tree = view.render({ ctx: ctx({ role: 'admin' }), call: conflictCall });
  assert.match(text(tree), /全体默认/);
  assert.match(text(tree), /tts-default/);
  change(field(tree, '模型'), 'conflict-model');
  change(field(tree, 'API Key'), 'conflict-secret');
  tree = view.render({ ctx: ctx({ role: 'admin' }), call: conflictCall });
  button(tree, '保存全体默认').props.onClick();
  await settle();
  const save = requests.find((item) => item.path === '/api/admin/managed-config'
    && item.init?.method === 'POST');
  assert.equal(save.init.headers['If-Match'], '"3"');
  tree = view.render({ ctx: ctx({ role: 'admin' }), call: conflictCall });
  assert.match(text(tree), /版本冲突/);
  assert.equal(field(tree, '模型').props.value, 'conflict-model');
  assert.equal(field(tree, 'API Key').props.value, 'conflict-secret');
});

test('admin member and invite actions use exact protocol without leaking tokens', async () => {
  let issued = false;
  const users = [user(), user({ userId: 'admin-1', displayName: '管理员',
    role: 'admin' })];
  const { call, requests } = recorder((path, init) => {
    if (path === '/api/admin/managed-config') return managedSettings();
    if (path.startsWith('/api/admin/users?') && !init) {
      return { users, nextOffset: null };
    }
    if (path === '/api/admin/users' && init?.method === 'POST') {
      return user({ userId: 'new-user', displayName: '新成员' });
    }
    if (path.startsWith('/api/admin/users/') && init?.method === 'PATCH') {
      return user({ disabled: true });
    }
    if (path.endsWith('/revoke-sessions')) return { revoked: 1 };
    if (path.startsWith('/api/admin/invites?') && !init) {
      return { invites: issued ? [invite()] : [], nextOffset: null };
    }
    if (path === '/api/admin/invites' && init?.method === 'POST') {
      issued = true;
      return { id: 'invite-1', token: 'TOKEN-ONLY-ONCE', expiresAt: 9999999999 };
    }
    throw new Error(`unexpected ${path} ${init?.method || 'GET'}`);
  });
  const view = createView('../../web/frontend/src/components/AdminSettingsPage.tsx');
  view.render({ ctx: ctx({ role: 'admin' }), call });
  await settle();
  let tree = view.render({ ctx: ctx({ role: 'admin' }), call });
  assert.match(text(tree), /成员与邀请/);

  change(field(tree, '新成员显示名'), '新成员');
  tree = view.render({ ctx: ctx({ role: 'admin' }), call });
  button(tree, '创建成员').props.onClick();
  await settle();
  const created = requests.find((item) => item.path === '/api/admin/users'
    && item.init?.method === 'POST');
  assert.deepEqual(JSON.parse(created.init.body), { displayName: '新成员' });

  tree = view.render({ ctx: ctx({ role: 'admin' }), call });
  button(tree, '禁用 成员一').props.onClick();
  await settle();
  const disabled = requests.find((item) => item.path === '/api/admin/users/user-1'
    && item.init?.method === 'PATCH');
  assert.deepEqual(JSON.parse(disabled.init.body), { disabled: true });

  tree = view.render({ ctx: ctx({ role: 'admin' }), call });
  button(tree, '撤销登录 成员一').props.onClick();
  await settle();
  assert.ok(requests.some((item) =>
    item.path === '/api/admin/users/user-1/revoke-sessions'));

  tree = view.render({ ctx: ctx({ role: 'admin' }), call });
  button(tree, '创建邀请').props.onClick();
  await settle();
  tree = view.render({ ctx: ctx({ role: 'admin' }), call });
  assert.match(text(tree), /TOKEN-ONLY-ONCE/);
  const listed = requests.filter((item) => item.path.startsWith('/api/admin/invites?')
    && !item.init?.method);
  assert.ok(listed.length >= 2);
  assert.doesNotMatch(JSON.stringify(listed.at(-1)), /TOKEN-ONLY-ONCE/);

  button(tree, '关闭邀请结果').props.onClick();
  tree = view.render({ ctx: ctx({ role: 'admin' }), call });
  assert.doesNotMatch(text(tree), /TOKEN-ONLY-ONCE/);
});

test('admin member management still loads when managed config is unavailable', async () => {
  const { call } = recorder((path) => {
    if (path === '/api/admin/managed-config') {
      const error = new Error('capability_unavailable');
      error.status = 503;
      throw error;
    }
    if (path.startsWith('/api/admin/users?')) {
      return { users: [user()], nextOffset: null };
    }
    if (path.startsWith('/api/admin/invites?')) {
      return { invites: [], nextOffset: null };
    }
    throw new Error(`unexpected ${path}`);
  });
  const view = createView('../../web/frontend/src/components/AdminSettingsPage.tsx');
  view.render({ ctx: ctx({ role: 'admin' }), call });
  await settle();
  const tree = view.render({ ctx: ctx({ role: 'admin' }), call });
  assert.match(text(tree), /成员与邀请/);
  assert.match(text(tree), /成员一/);
  assert.ok(button(tree, '创建邀请'));
  assert.match(text(tree), /读取失败：全体默认/);
  assert.doesNotMatch(text(tree), /受管默认模型/);
});

test('admin page refuses a non-admin even if mounted by mistake', async () => {
  const { call, requests } = recorder(() => managedSettings());
  const view = createView('../../web/frontend/src/components/AdminSettingsPage.tsx');
  view.render({ ctx: ctx({ role: 'member' }), call });
  await settle();
  assert.deepEqual(requests, []);
});
