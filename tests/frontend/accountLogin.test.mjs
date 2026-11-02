import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { runInNewContext } from 'node:vm';
import { test } from 'node:test';

const require = createRequire(new URL('../../web/frontend/package.json', import.meta.url));
const ts = require('typescript');
const source = readFileSync(new URL('../../web/frontend/src/components/AccountsPage.tsx', import.meta.url), 'utf8');
const parsed = ts.createSourceFile('AccountsPage.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const component = parsed.statements.find(node =>
  ts.isFunctionDeclaration(node) && node.name?.text === 'AccountsPage');
const names = ['closeQr', 'handleLogin', 'handleMpLogin', 'handleStopLogin'];
const statements = component.body.statements.filter(node =>
  ts.isVariableStatement(node) && node.declarationList.declarations.some(declaration =>
    names.includes(declaration.name.getText(parsed))));
assert.equal(statements.length, names.length);
const { outputText } = ts.transpileModule(
  statements.map(node => node.getText(parsed)).join('\n') + '\n({ closeQr, handleLogin, handleMpLogin, handleStopLogin });',
  { compilerOptions: { target: ts.ScriptTarget.ES2022 } });

test('a failed old avatar does not prevent a newly verified avatar from rendering', () => {
  const avatar = parsed.statements.find(node =>
    ts.isFunctionDeclaration(node) && node.name?.text === 'Avatar');
  const { outputText: compiled } = ts.transpileModule(
    avatar.getText(parsed) + '\nAvatar;',
    { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS,
      jsx: ts.JsxEmit.ReactJSX } },
  );
  let broken = '';
  const render = runInNewContext(compiled, {
    exports: {},
    useState: () => [broken, value => { broken = value; }],
    require: () => ({ jsx: (type, props) => ({ type, props }) }),
  });
  const old = render({ url: 'https://self.test/old', name: 'Me' });
  assert.equal(old.type, 'img');
  old.props.onError();
  assert.equal(render({ url: 'https://self.test/old', name: 'Me' }).type, 'div');
  const current = render({ url: 'https://self.test/current', name: 'Me' });
  assert.equal(current.type, 'img');
  assert.equal(current.props.src, 'https://self.test/current');
});

test('manual account verification updates the card and identity from the same result', async () => {
  const declaration = component.body.statements.find(node =>
    ts.isVariableStatement(node) && node.declarationList.declarations.some(variable =>
      variable.name.getText(parsed) === 'runWhoami'));
  const { outputText: compiled } = ts.transpileModule(
    declaration.getText(parsed) + '\nrunWhoami;',
    { compilerOptions: { target: ts.ScriptTarget.ES2022 } },
  );
  let accounts = [{ platform: 'wechat-oa', loggedIn: true }];
  let identity = {};
  const verify = runInNewContext(compiled, {
    useCallback: callback => callback,
    whoamiRequests: { current: {} },
    aliveRef: { current: true },
    setWhoami: callback => { identity = callback(identity); },
    setAccounts: callback => { accounts = callback(accounts); },
    data: {
      whoami: { set: () => {} },
      accountWhoami: async (platform, force) => {
        assert.equal(platform, 'wechat-oa');
        assert.equal(force, true);
        return { loggedIn: false, name: '', avatar: '' };
      },
    },
    expireIfNeeded: () => {},
  });
  verify('wechat-oa');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(accounts[0].loggedIn, false);
  assert.equal(identity['wechat-oa'].loggedIn, false);
});

test('failed account verification preserves the last verified nickname and avatar', async () => {
  const declaration = component.body.statements.find(node =>
    ts.isVariableStatement(node) && node.declarationList.declarations.some(variable =>
      variable.name.getText(parsed) === 'runWhoami'));
  const { outputText: compiled } = ts.transpileModule(
    declaration.getText(parsed) + '\nrunWhoami;',
    { compilerOptions: { target: ts.ScriptTarget.ES2022 } },
  );
  const cached = { loggedIn: true, name: 'Own channel', avatar: 'https://self.test/channel.png' };
  let identity = { 'weixin-channels': cached };
  const verify = runInNewContext(compiled, {
    useCallback: callback => callback,
    whoamiRequests: { current: {} },
    aliveRef: { current: true },
    setWhoami: callback => { identity = callback(identity); },
    data: {
      whoami: { get: () => ({ 'weixin-channels': cached }) },
      accountWhoami: async () => { throw new Error('identity endpoint not ready'); },
    },
    expireIfNeeded: () => {},
    setAccounts: () => assert.fail('unknown identity cannot update login state'),
  });
  verify('weixin-channels');
  assert.equal(identity['weixin-channels'], 'loading');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(identity['weixin-channels'], cached);
});

function setup() {
  const state = {
    qr: null, busy: '', err: '', stopBusy: false,
    intervals: [], verifies: [], accounts: [],
  };
  const pending = [];
  const stops = [];
  const qrRequest = { current: 0 };
  const pollRef = { current: null };
  const aliveRef = { current: true };
  let status = { state: 'qr_ready', message: '', qr: '_login/test.png', qrTs: 123 };
  let statusRequest = async () => status;
  const start = () => new Promise((resolve, reject) => pending.push({ resolve, reject }));
  const stop = (platform, options) => new Promise(
    (resolve, reject) => stops.push({ platform, options, resolve, reject }));
  const change = key => value => {
    state[key] = typeof value === 'function' ? value(state[key]) : value;
  };
  const data = {
    startLogin: start, startMpLogin: start, stopLogin: stop,
    loginStatus: () => statusRequest(), mpLoginStatus: () => statusRequest(),
    whoami: { set: () => {} },
  };
  const context = {
    Error,
    useCallback: callback => callback,
    qrRequest, aliveRef, pollRef,
    whoamiRequests: { current: {} }, qrPlatformRef: { current: '' },
    qrRunRef: { current: '' }, qrKindRef: { current: false },
    setQr: change('qr'), setBusy: change('busy'), setErr: change('err'),
    setAccounts: change('accounts'), setWhoami: () => {},
    setSmsCode: () => {}, setSmsErr: () => {}, setSmsBusy: () => {},
    setStopBusy: change('stopBusy'),
    setTerminalMsg: change('terminalMsg'), setQrNonce: () => {},
    openCred: () => { state.credentials = true; },
    load: () => {}, runWhoami: platform => state.verifies.push(platform),
    stopPoll: () => { pollRef.current = null; },
    data,
    newRequestId: () => 'request-id',
    expireIfNeeded: () => {},
    loginPollError: () => '登录状态连接中断，请检查网络后重新登录',
    workspaceErrorMessage: (error, fallback) =>
      (error instanceof Error && error.message) || fallback,
    setInterval: (callback, delay) => {
      const interval = { callback, delay };
      state.intervals.push(interval);
      return interval;
    },
  };
  // handleStopLogin 读取当前 qr 状态；用 getter 模拟 React 每次渲染的新闭包。
  Object.defineProperty(context, 'qr', { get: () => state.qr });
  const handlers = runInNewContext(outputText, context);
  return { handlers, state, pending, stops, aliveRef, qrRequest,
    setStatus: value => { status = value; },
    setStatusRequest: callback => { statusRequest = callback; } };
}

function modal(platform, name, runId) {
  return {
    platform, name, mp: false, state: 'qr_ready', message: '',
    qr: '_login/' + platform + '.png', qrTs: 1, runId,
  };
}

async function openB(view) {
  view.handlers.closeQr();
  const opening = view.handlers.handleLogin(
    { platform: 'douyin', name: '抖音', supported: true });
  view.pending[0].resolve({ mode: 'qr', state: 'starting' });
  await opening;
}

test('late stop success cannot overwrite a newer login modal', async () => {
  const view = setup();
  view.state.qr = modal('xiaohongshu', '小红书', 'run-a');
  const stopping = view.handlers.handleStopLogin();
  assert.equal(view.stops.length, 1);
  await openB(view);
  view.stops[0].resolve({
    ok: true, stopped: true, runId: 'run-a', state: 'cancelled',
  });
  await stopping;
  assert.equal(view.state.qr.platform, 'douyin');
  assert.equal(view.state.qr.state, 'starting');
  assert.notEqual(view.state.qr.message, '已停止登录');
});

test('late stop failure cannot overwrite a newer login modal', async () => {
  const view = setup();
  view.state.qr = modal('xiaohongshu', '小红书', 'run-a');
  const stopping = view.handlers.handleStopLogin();
  await openB(view);
  view.stops[0].reject(new Error('旧停止请求失败'));
  await stopping;
  assert.equal(view.state.qr.platform, 'douyin');
  assert.notEqual(view.state.qr.message, '旧停止请求失败');
});

test('late stop finally does not clear a newer stop busy state', async () => {
  const view = setup();
  view.state.qr = modal('xiaohongshu', '小红书', 'run-a');
  const first = view.handlers.handleStopLogin();
  await openB(view);
  const second = view.handlers.handleStopLogin();
  assert.equal(view.stops.length, 2);
  assert.equal(view.state.stopBusy, true);
  view.stops[0].resolve({
    ok: true, stopped: true, runId: 'run-a', state: 'cancelled',
  });
  await first;
  assert.equal(view.state.stopBusy, true,
    '旧请求的 finally 不能清掉新请求的停止状态');
  view.stops[1].resolve({
    ok: true, stopped: true, runId: 'run-b', state: 'cancelled',
  });
  await second;
  assert.equal(view.state.stopBusy, false);
  assert.equal(view.state.qr.platform, 'douyin');
});

for (const handler of ['handleLogin', 'handleMpLogin']) {
  test(`${handler} serializes polling and never overwrites terminal success`, async () => {
    const view = setup();
    const operation = view.handlers[handler]({ platform: 'weixin-channels', name: '视频号', supported: true });
    view.pending[0].resolve({ mode: 'qr', state: 'starting' });
    await operation;
    let resolve;
    let calls = 0;
    view.setStatusRequest(() => {
      calls += 1;
      return new Promise(done => { resolve = done; });
    });
    const poll = view.state.intervals[0].callback;
    const first = poll();
    await poll();
    assert.equal(calls, 1);
    resolve({ state: 'success', message: '登录成功', qr: '' });
    await first;
    await poll();
    assert.equal(calls, 1);
    assert.equal(view.state.qr.state, 'success');
    assert.deepEqual(view.state.verifies, ['weixin-channels']);
  });

  test(`${handler} surfaces persistent polling failures instead of spinning forever`, async () => {
    const view = setup();
    const operation = view.handlers[handler]({ platform: 'weixin-channels', name: '视频号', supported: true });
    view.pending[0].resolve({ mode: 'qr', state: 'starting' });
    await operation;
    view.setStatusRequest(async () => { throw new Error('network'); });
    for (let attempt = 0; attempt < 5; attempt += 1) await view.state.intervals[0].callback();
    assert.equal(view.state.qr.state, 'error');
    assert.match(view.state.qr.message, /连接中断/);
    assert.equal(view.state.qr.qr, '');
  });

  test(`${handler} opens the login modal before its start request resolves`, async () => {
    const view = setup();
    const operation = view.handlers[handler]({ platform: 'xiaohongshu', name: '小红书', supported: true });
    assert.equal(view.state.qr.state, 'starting');
    assert.equal(view.state.qr.qr, '');
    assert.equal(view.pending.length, 1);
    view.pending[0].resolve({ mode: 'qr', state: 'starting', qr: '' });
    await operation;
    assert.equal(view.state.intervals[0].delay, 1000);
    await view.state.intervals[0].callback();
    assert.equal(view.state.qr.state, 'qr_ready');
    assert.equal(view.state.qr.qrTs, 123);
  });

  test(`${handler} keeps a closed modal closed when its start response arrives`, async () => {
    const view = setup();
    const operation = view.handlers[handler]({ platform: 'xiaohongshu', name: '小红书', supported: true });
    view.handlers.closeQr();
    view.pending[0].resolve({ mode: 'qr', state: 'qr_ready', qr: 'old.png' });
    await operation;
    assert.equal(view.state.qr, null);
    assert.equal(view.state.busy, '');
    assert.equal(view.state.intervals.length, 0);
  });

  test(`${handler} shows startup failures inside the open modal`, async () => {
    const view = setup();
    const operation = view.handlers[handler]({ platform: 'xiaohongshu', name: '小红书', supported: true });
    view.pending[0].reject(new Error('启动失败'));
    await operation;
    assert.equal(view.state.qr.state, 'error');
    assert.equal(view.state.qr.message, '启动失败');
    assert.equal(view.state.busy, '');
  });

  test(`${handler} discards polling responses from a previous login`, async () => {
    const view = setup();
    const account = { platform: 'xiaohongshu', name: '小红书', supported: true };
    const first = view.handlers[handler](account);
    view.pending[0].resolve({ mode: 'qr', state: 'starting' });
    await first;
    const oldPoll = view.state.intervals[0].callback;
    const second = view.handlers[handler]({ ...account, platform: 'zhihu', name: '知乎' });
    view.setStatus({ state: 'success', message: '', qr: '' });
    await oldPoll();
    assert.equal(view.state.qr.platform, 'zhihu');
    assert.equal(view.state.qr.state, 'starting');
    assert.equal(view.state.verifies.length, 0);
    view.pending[1].resolve({ mode: 'qr', state: 'starting' });
    await second;
  });
}
