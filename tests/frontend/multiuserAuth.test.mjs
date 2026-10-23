import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { runInNewContext } from 'node:vm';
import { test } from 'node:test';

const require = createRequire(new URL('../../web/frontend/package.json', import.meta.url));
const ts = require('typescript');
const source = readFileSync(
  new URL('../../web/frontend/src/lib/auth.ts', import.meta.url), 'utf8');
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
    Set,
    Error,
    JSON,
    Promise,
  });
  return module.exports;
}

const A = { userId: 'user-a', workspaceId: 'workspace-a' };
const B = { userId: 'user-b', workspaceId: 'workspace-b' };

function context(user = A, csrfToken = 'csrf-a') {
  return {
    userId: user.userId,
    workspaceId: user.workspaceId,
    role: 'member',
    csrfToken,
    capabilities: ['workspace'],
  };
}

test('old identity cannot commit after switch, including same-user relogin', () => {
  const auth = load(async () => {
    throw new Error('no network');
  });
  const epochs = new auth.IdentityEpoch();
  const first = epochs.begin(A);
  const ctx = context();
  assert.equal(auth.commitIfCurrent(epochs, first, () => {}), true);

  epochs.invalidate();
  const second = epochs.begin(B);
  assert.equal(epochs.isCurrent(first), false);
  assert.equal(epochs.isCurrent(second), true);
  let committed = false;
  assert.equal(auth.commitIfCurrent(epochs, first, () => {
    committed = true;
  }), false);
  assert.equal(committed, false);

  // 同一 user/workspace 重新登录也必须换新 epoch
  const third = epochs.begin(A);
  assert.equal(epochs.isCurrent(second), false);
  assert.equal(epochs.isCurrent(third), true);
  assert.notEqual(first, third);
});

test('write requests add in-memory CSRF and same-origin credentials', async () => {
  const auth = load(async () => {
    throw new Error('no network');
  });
  const epochs = new auth.IdentityEpoch();
  const ctx = context();
  const epoch = epochs.begin(ctx);
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url, init });
    return { ok: true, status: 200, json: async () => ({ ok: true }) };
  };

  await auth.identityFetch({
    epochs, epoch, ctx, url: '/api/sessions',
    init: { method: 'POST', headers: { 'Content-Type': 'application/json' } },
    fetchImpl,
  });
  assert.equal(calls[0].init.method, 'POST');
  assert.equal(calls[0].init.credentials, 'same-origin');
  assert.equal(calls[0].init.cache, 'no-store');
  assert.equal(calls[0].init.headers['X-CSRF-Token'], 'csrf-a');
  assert.equal(calls[0].init.headers['Content-Type'], 'application/json');

  await auth.identityFetch({
    epochs, epoch, ctx, url: '/api/sessions', fetchImpl,
  });
  assert.equal(calls[1].init.headers['X-CSRF-Token'], undefined);
});

test('a stale epoch 401 cannot invalidate the newly logged-in identity', async () => {
  const auth = load(async () => {
    throw new Error('no network');
  });
  const epochs = new auth.IdentityEpoch();
  const oldEpoch = epochs.begin(A);
  const newEpoch = epochs.begin(B);
  const fetch401 = async () => ({
    ok: false, status: 401, json: async () => ({ code: 'unauthorized' }),
  });
  await assert.rejects(
    () => auth.identityFetch({
      epochs, epoch: oldEpoch, ctx: context(), url: '/x',
      fetchImpl: fetch401,
    }),
    (error) => error.status === 401 && error.code === 'unauthorized',
  );
  assert.equal(epochs.isCurrent(newEpoch), true);
  assert.equal(epochs.isCurrent(oldEpoch), false);

  // 当前身份的 401 必须标记为可卸载私人树
  const current = epochs.begin(B);
  await assert.rejects(
    () => auth.identityFetch({
      epochs, epoch: current, ctx: context(B), url: '/x',
      fetchImpl: fetch401,
    }),
    (error) => error.status === 401
      && error.identityInvalidated === true,
  );
  assert.equal(epochs.isCurrent(current), false);
});

test('403 and 503 keep explicit status, never fall back to local', async () => {
  const auth = load(async () => {
    throw new Error('no network');
  });
  for (const status of [403, 503]) {
    const epochs = new auth.IdentityEpoch();
    const epoch = epochs.begin(A);
    await assert.rejects(
      () => auth.identityFetch({
        epochs, epoch, ctx: context(), url: '/x',
        fetchImpl: async () => ({
          ok: false, status,
          json: async () => ({ code: `code_${status}` }),
        }),
      }),
      (error) => error.status === status && error.code === `code_${status}`,
    );
    assert.equal(epochs.isCurrent(epoch), true);
  }
});

test('identity cache keys are scoped by user, workspace and name', () => {
  const auth = load(async () => {
    throw new Error('no network');
  });
  const a = auth.identityCacheKey(A, 'active_session');
  const a2 = auth.identityCacheKey(A, 'run:s1');
  const b = auth.identityCacheKey(B, 'active_session');
  assert.notEqual(a, a2);
  assert.notEqual(a, b);
  assert.equal(a, auth.identityCacheKey(A, 'active_session'));
  assert.ok(a.startsWith('easel:control:v1:'));
});

test('loadIdentity distinguishes anonymous, unavailable and valid', async () => {
  const auth = load(async () => {
    throw new Error('no network');
  });
  const ctx = context();
  await assert.rejects(
    () => auth.loadIdentity(async () => ({
      status: 401, ok: false, json: async () => ({ code: 'unauthorized' }),
    })),
    (error) => error.status === 401,
  );
  await assert.rejects(
    () => auth.loadIdentity(async () => ({
      status: 503, ok: false, json: async () => ({ code: 'unavailable' }),
    })),
    (error) => error.status === 503 && error.code === 'unavailable',
  );
  const loaded = await auth.loadIdentity(async () => ({
    status: 200, ok: true, json: async () => ctx,
  }));
  assert.equal(loaded.userId, A.userId);
  assert.equal(loaded.csrfToken, 'csrf-a');
});

test('invite exchange clears neither token to storage and errors stay typed', async () => {
  const auth = load(async () => {
    throw new Error('no network');
  });
  let seen;
  const ctx = context();
  const loaded = await auth.exchangeInvite('invite-once', async (url, init) => {
    seen = { url, init };
    return { status: 200, ok: true, json: async () => ctx };
  });
  assert.equal(loaded.workspaceId, A.workspaceId);
  assert.equal(seen.url, '/api/auth/exchange');
  assert.equal(seen.init.credentials, 'same-origin');
  assert.equal(JSON.parse(seen.init.body).token, 'invite-once');

  await assert.rejects(
    () => auth.exchangeInvite('used', async () => ({
      status: 401, ok: false, json: async () => ({ code: 'unauthorized' }),
    })),
    (error) => error.status === 401,
  );
});

test('login errors keep 401/429/503/network semantics', () => {
  const auth = load(async () => {
    throw new Error('no network');
  });
  const { AuthError } = auth;
  assert.match(auth.loginErrorMessage(new AuthError(401, 'unauthorized')),
    /已使用|已过期/);
  assert.match(auth.loginErrorMessage(new AuthError(429, 'rate_limited')),
    /过多/);
  assert.match(auth.loginErrorMessage(new AuthError(0, 'network_unavailable')),
    /网络/);
  const unavailable = auth.loginErrorMessage(new AuthError(503, 'unavailable'));
  assert.match(unavailable, /服务暂不可用/);
  assert.doesNotMatch(unavailable, /邀请无效/);
});

test('logout uses the captured CSRF and reports remote failure', async () => {
  const auth = load(async () => {
    throw new Error('no network');
  });
  let request;
  await auth.logout(context(), async (url, init) => {
    request = { url, init };
    return { status: 204, ok: true, json: async () => ({}) };
  });
  assert.equal(request.url, '/api/auth/logout');
  assert.equal(request.init.credentials, 'same-origin');
  assert.equal(request.init.headers['X-CSRF-Token'], 'csrf-a');
  await assert.rejects(
    () => auth.logout(context(), async () => ({
      status: 500, ok: false, json: async () => ({ code: 'server_error' }),
    })),
    (error) => error.status === 500 && error.code === 'server_error',
  );
});
