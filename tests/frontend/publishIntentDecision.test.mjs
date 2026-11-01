import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { runInNewContext } from 'node:vm';
import { test } from 'node:test';

const require = createRequire(new URL('../../web/frontend/package.json', import.meta.url));
const ts = require('typescript');
const source = readFileSync(
  new URL('../../web/frontend/src/lib/publishIntent.ts', import.meta.url), 'utf8');
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
});

function load() {
  const module = { exports: {} };
  runInNewContext(outputText, {
    exports: module.exports, module,
    require: () => { throw new Error('no runtime imports expected'); },
    JSON, Array, Object, String, Boolean,
  });
  return module.exports;
}

const intent = load();
const PLATFORM = 'xiaohongshu';

function payload(overrides = {}) {
  return {
    title: '标题', body: '正文', media: ['project/cover.png'],
    tags: '工具', persona: '画像A', ...overrides,
  };
}

function stored(overrides = {}) {
  const value = payload(overrides);
  return {
    requestId: 'req-old',
    fingerprint: intent.publishFingerprint(PLATFORM, value),
    payload: value,
  };
}

function provenance(overrides = {}) {
  return {
    mediaPersisted: true, personaPersisted: true,
    mediaEdited: false, personaEdited: false,
    appPersona: '', ...overrides,
  };
}

test('no stored intent starts a new request with the current payload', () => {
  const current = payload();
  const decision = intent.decidePublishIntent(
    undefined, PLATFORM, current, provenance());
  assert.equal(decision.action, 'start');
  assert.deepEqual(decision.payload, current);
});

test('same fingerprint reuses the original request id', () => {
  const current = payload();
  const decision = intent.decidePublishIntent(
    stored(), PLATFORM, current, provenance());
  assert.equal(decision.action, 'reuse');
  assert.equal(decision.requestId, 'req-old');
  assert.deepEqual(decision.payload, current);
});

test('legacy draft that never stored media/persona restores the old payload', () => {
  // 旧版草稿没有 media/persona 字段：刷新后页面显示为空，但用户没有
  // 主动改动，应恢复原意图，避免同一次发布生成第二个请求。
  const decision = intent.decidePublishIntent(
    stored(), PLATFORM,
    payload({ media: [], persona: '' }),
    provenance({ mediaPersisted: false, personaPersisted: false }));
  assert.equal(decision.action, 'reuse');
  assert.equal(decision.requestId, 'req-old');
  assert.deepEqual(decision.payload, payload());
});

test('explicitly reselecting media never reuses the old payload', () => {
  const current = payload({ media: ['project/other.png'] });
  const decision = intent.decidePublishIntent(
    stored(), PLATFORM, current,
    provenance({ mediaPersisted: true, mediaEdited: true }));
  assert.equal(decision.action, 'replace');
  assert.deepEqual(decision.payload, current);
});

test('a media edit persisted before refresh still starts a new request', () => {
  // 改图后刷新：edited 标记已随页面重建丢失，但草稿里保存的是新图，
  // 与旧 payload 不同，不能把旧图悄悄提交。
  const current = payload({ media: ['project/other.png'] });
  const decision = intent.decidePublishIntent(
    stored(), PLATFORM, current,
    provenance({ mediaPersisted: true, mediaEdited: false }));
  assert.equal(decision.action, 'replace');
  assert.deepEqual(decision.payload, current);
});

test('removing all media is an explicit edit, not a lost selection', () => {
  const current = payload({ media: [] });
  const decision = intent.decidePublishIntent(
    stored(), PLATFORM, current,
    provenance({ mediaPersisted: true, mediaEdited: true }));
  assert.equal(decision.action, 'replace');
  assert.deepEqual(decision.payload, current);
});

test('switching persona starts a new request', () => {
  const current = payload({ persona: '画像B' });
  const decision = intent.decidePublishIntent(
    stored(), PLATFORM, current,
    provenance({ personaPersisted: true, personaEdited: true }));
  assert.equal(decision.action, 'replace');
  assert.deepEqual(decision.payload, current);
});

test('persona switch cannot drag the old media back in', () => {
  // 媒体这次因旧草稿丢失、画像被显式切换：只能按当前可见稿新建，
  // 不能为了补画像而复用含旧媒体的完整 payload。
  const current = payload({ media: [], persona: '画像B' });
  const decision = intent.decidePublishIntent(
    stored(), PLATFORM, current,
    provenance({
      mediaPersisted: false, personaPersisted: true,
      mediaEdited: false, personaEdited: true,
    }));
  assert.equal(decision.action, 'replace');
  assert.deepEqual(decision.payload, current);
});

test('a non-empty app persona different from the old payload starts a new request', () => {
  // 旧版草稿没有画像字段，但应用级选择已明确切到 B；重新挂载后
  // edited 标记不在，也不能把旧稿的 A 画像恢复回来。
  const current = payload({ persona: '画像B', media: [] });
  const decision = intent.decidePublishIntent(
    stored(), PLATFORM, current,
    provenance({
      mediaPersisted: false, personaPersisted: false,
      mediaEdited: false, personaEdited: false, appPersona: '画像B',
    }));
  assert.equal(decision.action, 'replace');
  assert.deepEqual(decision.payload, current);
});

test('a restored same persona still allows lost media recovery', () => {
  const current = payload({ persona: '画像A', media: [] });
  const decision = intent.decidePublishIntent(
    stored(), PLATFORM, current,
    provenance({
      mediaPersisted: false, personaPersisted: false,
      mediaEdited: false, personaEdited: false, appPersona: '画像A',
    }));
  assert.equal(decision.action, 'reuse');
  assert.deepEqual(decision.payload, payload());
});

test('legacy lost persona is filled from the old payload when media is unchanged', () => {
  const decision = intent.decidePublishIntent(
    stored(), PLATFORM, payload({ persona: '' }),
    provenance({ personaPersisted: false, personaEdited: false }));
  assert.equal(decision.action, 'reuse');
  assert.equal(decision.payload.persona, '画像A');
});

test('editing a legacy draft blocks the lost-selection fallback', () => {
  const current = payload({ media: ['project/other.png'] });
  const decision = intent.decidePublishIntent(
    stored(), PLATFORM, current,
    provenance({
      mediaPersisted: false, personaPersisted: false,
      mediaEdited: true, personaEdited: false,
    }));
  assert.equal(decision.action, 'replace');
  assert.deepEqual(decision.payload, current);
});

test('changed title or body is a new draft even when media matches', () => {
  for (const current of [
    payload({ title: '新标题' }),
    payload({ body: '新正文' }),
    payload({ tags: '新标签' }),
  ]) {
    const decision = intent.decidePublishIntent(
      stored(), PLATFORM, current, provenance());
    assert.equal(decision.action, 'replace');
    assert.deepEqual(decision.payload, current);
  }
});
