// 只读复审探针：执行当前 TrendsPage TSX，检查旧热点是否被显示成刚更新。
// 用法：node probe_trends_view.cjs <实施 worktree 绝对路径> [受控 api_trends 响应JSON文件]
// 不启动浏览器/服务，不请求网络，不读取真实用户数据，不修改实施树。
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { createRequire } = require('module');
const root = path.resolve(process.argv[2] || 'C:\\Code\\Easel\\.claude\\worktrees\\original-web-task1-diagnostics');
const frontendRequire = createRequire(path.join(root, 'web/frontend/package.json'));
const ts = frontendRequire('typescript');
const filename = path.join(root, 'web/frontend/src/components/TrendsPage.tsx');
const code = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
  compilerOptions: {
    target: ts.ScriptTarget.ES2022,
    module: ts.ModuleKind.CommonJS,
    jsx: ts.JsxEmit.ReactJSX,
  },
}).outputText;
let slots = [], index = 0, dirty = true, effects = [], tree;
const equal = (a, b) => a && b && a.length === b.length
  && a.every((value, i) => Object.is(value, b[i]));
const react = {
  useState(value) {
    const slot = index++;
    if (!(slot in slots)) slots[slot] = typeof value === 'function' ? value() : value;
    return [slots[slot], update => {
      slots[slot] = typeof update === 'function' ? update(slots[slot]) : update;
      dirty = true;
    }];
  },
  useRef(value) {
    const slot = index++;
    return slots[slot] ?? (slots[slot] = { current: value });
  },
  useMemo(factory, deps) {
    const slot = index++;
    if (!slots[slot] || !equal(slots[slot].deps, deps)) {
      slots[slot] = { value: factory(), deps };
    }
    return slots[slot].value;
  },
  useEffect(effect, deps) {
    const slot = index++;
    if (!slots[slot] || !equal(slots[slot].deps, deps)) {
      slots[slot] = { deps };
      effects.push(effect);
    }
  },
};
react.useCallback = (callback, deps) => react.useMemo(() => callback, deps);
const now = Math.floor(Date.now() / 1000);
// 与主审真实后端反例同形：缓存条目已有一小时，上游失败但返回 ok=true/updated=now。
const fixtureResponse = {
  trends: [{ platform: 'weibo', label: '微博', ok: true,
    items: [{ title: 'ONE-HOUR-OLD-SENTINEL', hot: '1', url: '' }] }],
  updated: now,
};
const response = process.argv[3]
  ? JSON.parse(fs.readFileSync(path.resolve(process.argv[3]), 'utf8').replace(/^\uFEFF/, ''))
  : fixtureResponse;
const data = { fetchTrends: async () => response };
const moduleObject = { exports: {} };
vm.runInNewContext(code, {
  exports: moduleObject.exports,
  require: id => {
    if (id === 'react') return react;
    if (id === 'react/jsx-runtime') return {
      jsx: (type, props) => ({ type, props }),
      jsxs: (type, props) => ({ type, props }),
    };
    if (id === '../lib/workspaceDataContext') return { useWorkspaceData: () => data };
    if (id === '../lib/workspaceData') return { workspaceErrorMessage: () => '请求失败' };
    if (id === './icons') return {};
    throw new Error('未预期的模块依赖：' + id);
  },
  AbortController,
  console,
});
function text(node) {
  if (node == null || typeof node === 'boolean') return '';
  if (Array.isArray(node)) return node.map(text).join('');
  if (typeof node === 'object') return text(node.props?.children);
  return String(node);
}
(async () => {
  for (let tick = 0; tick < 10; tick++) {
    if (dirty) {
      dirty = false;
      index = 0;
      tree = moduleObject.exports.default({ onUseTopic() {} });
      const pending = effects;
      effects = [];
      pending.forEach(effect => effect());
    }
    await new Promise(resolve => setImmediate(resolve));
  }
  const rendered = text(tree);
  const expectedTime = new Date(now * 1000).toLocaleTimeString('zh-CN', {
    hour: '2-digit', minute: '2-digit',
  });
  const updateLabel = rendered.match(/·\s*[^·]*?更新/)?.[0] || '';
  const result = {
    source: 'web/frontend/src/components/TrendsPage.tsx',
    fixture: '过期一小时缓存的返回形状：旧条目 + ok=true + updated=本次请求时间',
    hasOldItem: rendered.includes('ONE-HOUR-OLD-SENTINEL'),
    updateLabel,
    expectedTime,
    hasFailureOrStale: /失败|旧数据|缓存|过期/.test(rendered),
    rendered,
  };
  result.misleadingFreshness = result.hasOldItem
    && updateLabel.includes(expectedTime) && !result.hasFailureOrStale;
  console.log(JSON.stringify(result, null, 2));
})().catch(error => {
  console.error(error.message);
  process.exitCode = 1;
});

