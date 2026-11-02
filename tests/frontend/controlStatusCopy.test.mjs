import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { runInNewContext } from 'node:vm';
import { test } from 'node:test';

const require = createRequire(new URL('../../web/frontend/package.json', import.meta.url));
const ts = require('typescript');
const source = readFileSync(
  new URL('../../web/frontend/src/lib/statusCopy.ts', import.meta.url), 'utf8');
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
});
const exports = {};
runInNewContext(outputText, { exports });

test('managed workspace never asks the user to install a CLI', () => {
  for (const status of ['connected', 'connecting', 'disconnected']) {
    const hero = exports.dashboardHeroText(status, false, 'control');
    const sidebar = exports.sidebarStatusText(status, false, 'control');
    assert.doesNotMatch(hero, /安装|CLI/);
    assert.doesNotMatch(sidebar, /未配置|CLI/);
  }
  assert.match(exports.dashboardHeroText('connected', false, 'control'), /受管工作区已就绪/);
  assert.match(exports.dashboardHeroText('connecting', false, 'control'), /受管工作区尚未就绪/);
  assert.match(exports.dashboardHeroText('disconnected', false, 'control'), /受管工作区尚未就绪/);
  assert.equal(exports.sidebarStatusText('connected', false, 'control'), '受管工作区已就绪');
  assert.equal(exports.sidebarStatusText('disconnected', false, 'control'), '受管工作区未就绪');
  assert.equal(exports.sidebarStatusText('connecting', false, 'control'), '受管工作区连接中…');
});

test('non-managed modes keep their existing install guidance', () => {
  assert.match(exports.dashboardHeroText('connecting', false, 'claude'), /安装并配置 claude CLI/);
  assert.match(exports.sidebarStatusText('disconnected', false, 'claude'), /claude CLI 未配置/);
  assert.equal(exports.dashboardHeroText('connected', true, 'control'), '仅聊天模式已配置，可开始对话。');
  assert.equal(exports.sidebarStatusText('connected', true, 'control'), '仅聊天模式已配置');
});

test('workspace gateway status distinguishes stopping from connecting', () => {
  assert.equal(exports.workspaceGatewayStatus(undefined), 'connecting');
  assert.equal(exports.workspaceGatewayStatus({ gateway: true, state: 'ready' }), 'connected');
  assert.equal(exports.workspaceGatewayStatus({ gateway: false, state: 'starting' }), 'connecting');
  assert.equal(exports.workspaceGatewayStatus({ gateway: false, state: 'stopping' }), 'disconnected');
  assert.equal(exports.workspaceGatewayStatus({ gateway: false, state: 'unavailable' }), 'disconnected');
});
