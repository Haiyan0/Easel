import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { runInNewContext } from 'node:vm';
import { test } from 'node:test';

const require = createRequire(new URL('../../web/frontend/package.json', import.meta.url));
const ts = require('typescript');

function transpile(relative) {
  const source = readFileSync(
    new URL(`../../web/frontend/src/${relative}`, import.meta.url), 'utf8');
  return ts.transpileModule(source, {
    compilerOptions: {
      target: ts.ScriptTarget.ES2022,
      module: ts.ModuleKind.CommonJS,
    },
  }).outputText;
}

const sources = {
  './auth': transpile('lib/auth.ts'),
  './control': transpile('lib/control.ts'),
  './workspaceClient': transpile('lib/workspaceClient.ts'),
};

function loadModules(fetchImpl) {
  const cache = new Map();
  function load(name) {
    if (cache.has(name)) return cache.get(name).exports;
    const module = { exports: {} };
    cache.set(name, module);
    runInNewContext(sources[name], {
      exports: module.exports,
      require: (id) => {
        if (name === './workspaceClient'
            && (id === './auth' || id === './control')) {
          return load(id);
        }
        if (name === './workspaceClient' && id === './store') return {};
        throw new Error(`unexpected require ${id} from ${name}`);
      },
      fetch: fetchImpl,
      AbortController,
      TextDecoder,
      encodeURIComponent,
      Number,
      JSON,
      Promise,
      Array,
      Object,
      String,
      Set,
      Map,
      Error,
      URLSearchParams,
    });
    return module.exports;
  }
  const workspace = load('./workspaceClient');
  return { workspace, auth: load('./auth'), control: load('./control') };
}

const A = { userId: 'user-a', workspaceId: 'workspace-a' };
const B = { userId: 'user-b', workspaceId: 'workspace-b' };

function context(user = A, csrfToken = 'csrf-a', role = 'member') {
  return {
    userId: user.userId,
    workspaceId: user.workspaceId,
    role,
    csrfToken,
    capabilities: ['workspace'],
  };
}

function response(status, body) {
  return {
    status,
    ok: status >= 200 && status < 300,
    json: async () => body,
  };
}

function sseResponse(chunks, status = 200) {
  let index = 0;
  return {
    status,
    ok: status >= 200 && status < 300,
    body: {
      getReader: () => ({
        read: async () => index < chunks.length
          ? { done: false, value: Buffer.from(chunks[index++]) }
          : { done: true, value: undefined },
      }),
    },
    json: async () => ({}),
  };
}

function makeClient(fetchImpl, user = A, role = 'member') {
  const { workspace, auth } = loadModules(fetchImpl);
  const identity = new auth.IdentityEpoch();
  const ctx = context(user, user === A ? 'csrf-a' : 'csrf-b', role);
  const epoch = identity.begin(ctx);
  return {
    workspace,
    auth,
    epochs: identity,
    epoch,
    ctx,
    client: workspace.createControlWorkspaceClient({
      ctx, epoch, epochs: identity, fetchImpl,
    }),
  };
}

test('session and message pages use identity-scoped paginated control APIs', async () => {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url, init });
    if (url.includes('/messages')) {
      return response(200, {
        messages: [{ role: 'user', content: '第一问' },
          { role: 'assistant', content: '第一答' }],
        total: 2,
      });
    }
    return response(200, {
      sessions: [{
        id: 's1', workspaceId: 'workspace-a', title: '会话', archived: false,
      }],
      nextOffset: null,
    });
  };
  const { client, ctx, epoch, epochs } = makeClient(fetchImpl);

  const page = await client.listSessions(100, 50);
  assert.equal(page.sessions[0].id, 's1');
  assert.equal(page.nextOffset, null);
  assert.equal(calls[0].url,
    '/api/sessions?limit=50&offset=100&include_archived=true');
  assert.equal(calls[0].init.credentials, 'same-origin');
  assert.equal(calls[0].init.headers['X-CSRF-Token'], undefined);

  const messages = await client.loadMessages('s-1', 100, 500);
  assert.equal(messages.total, 2);
  assert.equal(messages.messages[1].content, '第一答');
  assert.equal(calls[1].url,
    '/api/sessions/s-1/messages?limit=500&offset=100');
  assert.equal(epochs.isCurrent(epoch), true);
  assert.equal(ctx.csrfToken, 'csrf-a');
});

test('create, rename, archive and delete send CSRF and exact JSON bodies', async () => {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url, init });
    if (url === '/api/sessions' && init.method === 'POST') {
      return response(201, {
        id: 's-new', title: '新对话', archived: false,
        workspaceId: 'workspace-a',
      });
    }
    return response(204, undefined);
  };
  const { client } = makeClient(fetchImpl);

  const created = await client.createSession('新对话');
  assert.equal(created.id, 's-new');
  assert.equal(calls[0].init.headers['X-CSRF-Token'], 'csrf-a');
  assert.deepEqual(JSON.parse(calls[0].init.body), { title: '新对话' });

  await client.updateSession('s-1', { title: '改标题' });
  assert.equal(calls[1].url, '/api/sessions/s-1');
  assert.equal(calls[1].init.method, 'PATCH');
  assert.deepEqual(JSON.parse(calls[1].init.body), { title: '改标题' });

  await client.updateSession('s-1', { archived: true });
  assert.deepEqual(JSON.parse(calls[2].init.body), { archived: true });

  await client.deleteSession('s-1');
  assert.equal(calls[3].init.method, 'DELETE');
  assert.equal(calls[3].init.headers['X-CSRF-Token'], 'csrf-a');
});

test('202 admission keeps turnId idempotency and 409/503 stay typed', async () => {
  const calls = [];
  let mode = 'accepted';
  const fetchImpl = async (url, init) => {
    calls.push({ url, init });
    if (mode === 'conflict') {
      return response(409, { code: 'run_active' });
    }
    if (mode === 'unavailable') {
      return response(503, { code: 'service_unavailable' });
    }
    return response(202, {
      runId: 'run-1', state: 'accepted', duplicate: calls.length > 1,
    });
  };
  const { client, auth } = makeClient(fetchImpl);

  const first = await client.submitTurn('s-1', 'turn-1', '你好');
  assert.equal(first.runId, 'run-1');
  assert.equal(first.duplicate, false);
  assert.deepEqual(JSON.parse(calls[0].init.body), {
    sessionId: 's-1', turnId: 'turn-1', message: '你好',
  });

  const duplicate = await client.submitTurn('s-1', 'turn-1', '你好');
  assert.equal(duplicate.runId, 'run-1');
  assert.equal(duplicate.duplicate, true);
  assert.deepEqual(JSON.parse(calls[1].init.body), {
    sessionId: 's-1', turnId: 'turn-1', message: '你好',
  });

  mode = 'conflict';
  await assert.rejects(
    () => client.submitTurn('s-1', 'turn-2', '再次'),
    (error) => error instanceof auth.AuthError && error.status === 409,
  );
  mode = 'unavailable';
  await assert.rejects(
    () => client.submitTurn('s-1', 'turn-3', '稍后'),
    (error) => error instanceof auth.AuthError && error.status === 503,
  );
});

test('attachment refs and persona ride the same admitted turn', async () => {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url, init });
    return response(202, {
      runId: 'run-attach', state: 'accepted', duplicate: false,
    });
  };
  const { client, workspace } = makeClient(fetchImpl);
  const attachments = [{
    id: 'i1', name: '素材.txt', path: '_inbox/a/b/素材.txt',
  }];
  await client.submitTurn(
    's-1', 'turn-1', '看附件', undefined, attachments, '画像A');
  assert.deepEqual(JSON.parse(calls[0].init.body), {
    sessionId: 's-1', turnId: 'turn-1', message: '看附件',
    attachments, persona: '画像A',
  });
  const view = workspace.controlMessageView({
    role: 'user', content: '看附件', attachments,
  });
  assert.equal(view.attachments.length, 1);
  assert.equal(view.attachments[0].path, attachments[0].path);
  assert.equal(workspace.controlMessageView({
    role: 'user', content: 'x', attachments: [{ path: 1 }],
  }).attachments, undefined);
});

test('fetchText keeps identity semantics for HTML previews', async () => {
  const fetchImpl = async (url) => ({
    status: 200, ok: true,
    text: async () => `<html data-url="${url}"></html>`,
  });
  const { client, workspace } = makeClient(fetchImpl);
  const html = await client.fetchText('/api/media/card.html');
  assert.match(html, /card\.html/);
  assert.equal(typeof workspace.controlMessageView, 'function');
});

test('401 invalidates only the captured identity epoch', async () => {
  const fetch401 = async () => response(401, { code: 'unauthorized' });
  const first = makeClient(fetch401);
  await assert.rejects(
    () => first.client.listSessions(0),
    (error) => error.status === 401 && error.identityInvalidated === true,
  );
  assert.equal(first.epochs.isCurrent(first.epoch), false);

  const second = makeClient(fetch401);
  const stale = second.epoch;
  second.epochs.begin(context(B, 'csrf-b'));
  await assert.rejects(
    () => second.client.listSessions(0),
    (error) => error.status === 401 && error.identityInvalidated === false,
  );
  assert.equal(second.epochs.isCurrent(stale), false);
  assert.equal(second.epochs.isCurrent(second.epochs.snapshot().epoch), true);
});

test('SSE stream reports cursor, token, thinking, activity and terminal state', async () => {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url, init });
    return sseResponse([
      'id: 1\nevent: state\ndata: {"state":"running"}\n\n',
      'id: 2\nevent: token\ndata: "你"\n\n',
      'id: 3\nevent: thinking\ndata: "想"\n\n',
      'id: 4\nevent: activity\ndata: "正在执行"\n\n',
      'id: 5\nevent: state\ndata: {"state":"done"}\n\n',
    ]);
  };
  const { client } = makeClient(fetchImpl);
  const seen = { tokens: [], thinking: [], activity: [], states: [] };
  const result = await client.streamRun('run-1', 0, {
    onToken: (text, frame) => seen.tokens.push([text, frame.id]),
    onThinking: (text) => seen.thinking.push(text),
    onActivity: (text) => seen.activity.push(text),
    onState: (state) => seen.states.push(state.state),
  });
  assert.equal(result.cursor, 5);
  assert.equal(result.terminal, true);
  assert.deepEqual(JSON.parse(JSON.stringify(seen.tokens)), [['你', 2]]);
  assert.deepEqual(JSON.parse(JSON.stringify(seen.thinking)), ['想']);
  assert.deepEqual(JSON.parse(JSON.stringify(seen.activity)), ['正在执行']);
  assert.deepEqual(JSON.parse(JSON.stringify(seen.states)),
    ['running', 'done']);
  assert.equal(calls[0].url, '/api/chat/jobs/run-1/stream?after=0');
  assert.equal(calls[0].init.credentials, 'same-origin');
  assert.equal(calls[0].init.cache, 'no-store');
});

test('reconnect asks only for the cursor tail and 404 is explicit', async () => {
  const calls = [];
  const fetchImpl = async (url) => {
    calls.push(url);
    if (url.includes('missing')) {
      return response(404, { code: 'run_not_found' });
    }
    return sseResponse([
      'id: 6\nevent: token\ndata: "补"\n\n',
      'id: 7\nevent: state\ndata: {"state":"done"}\n\n',
    ]);
  };
  const { client, auth } = makeClient(fetchImpl);
  const result = await client.streamRun('run-1', 5, {
    onToken: () => {},
  });
  assert.equal(result.cursor, 7);
  assert.equal(calls[0], '/api/chat/jobs/run-1/stream?after=5');
  await assert.rejects(
    () => client.streamRun('missing', 0, {}),
    (error) => error instanceof auth.AuthError && error.status === 404,
  );
});

test('question status and answer use runId plus opaque option values', async () => {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url, init });
    if (url.includes('/status')) {
      return response(200, {
        questions: { q1: { status: 'pending' } },
      });
    }
    return response(200, { ok: true });
  };
  const { client, auth } = makeClient(fetchImpl);
  const status = await client.questionStatus('run-1', ['q1']);
  assert.equal(status.q1.status, 'pending');
  assert.deepEqual(JSON.parse(calls[0].init.body), {
    runId: 'run-1', questionIds: ['q1'],
  });
  await client.answerQuestion('run-1', 'q1', {
    permission: ['qopt_allow'],
  });
  assert.equal(calls[1].url, '/api/chat/question/answer');
  assert.deepEqual(JSON.parse(calls[1].init.body), {
    runId: 'run-1', questionId: 'q1',
    answers: { permission: ['qopt_allow'] },
  });
  assert.equal(calls[1].init.headers['X-CSRF-Token'], 'csrf-a');

  const conflict = makeClient(async () => response(409, { code: 'expired' }));
  await assert.rejects(
    () => conflict.client.answerQuestion('run-1', 'q1', {}),
    (error) => error instanceof conflict.auth.AuthError
      && error.status === 409,
  );
});

test('server views keep original ChatPage types and control capabilities', () => {
  const { workspace } = loadModules(async () => {
    throw new Error('no network');
  });
  const session = workspace.controlSessionView({
    id: 's1', workspaceId: 'w', title: '标题', archived: true,
  });
  assert.equal(session.id, 's1');
  assert.equal(session.title, '标题');
  assert.equal(session.archived, true);
  assert.equal(session.remote, true);
  assert.deepEqual(JSON.parse(JSON.stringify(session.messages)), []);

  const message = workspace.controlMessageView({
    role: 'assistant', content: '正文', thinking: '思考', activity: '步骤',
  });
  assert.equal(message.role, 'assistant');
  assert.equal(message.content, '正文');
  assert.equal(message.thinking, '思考');
  assert.equal(message.activity, '步骤');

  const question = workspace.controlQuestionView({
    id: 'q1',
    questions: [{
      questionId: 'permission',
      header: '授权',
      question: '允许？',
      options: [{ label: '允许', value: 'qopt_allow' }],
    }],
  });
  assert.equal(question.id, 'q1');
  assert.equal(question.questions[0].questionId, 'permission');
  assert.equal(question.questions[0].options[0].value, 'qopt_allow');

  const control = workspace.workspaceModeFeatures('control');
  assert.equal(control.personas, true);
  assert.equal(control.attachments, true);
  assert.equal(control.retry, false);
  const local = workspace.workspaceModeFeatures('local');
  assert.equal(local.personas, true);
  assert.equal(local.attachments, true);
  assert.equal(local.retry, true);
});
