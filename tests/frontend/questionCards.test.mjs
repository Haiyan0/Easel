import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { runInNewContext } from 'node:vm';
import { test } from 'node:test';

const require = createRequire(new URL('../../web/frontend/package.json', import.meta.url));
const ts = require('typescript');
const source = readFileSync(new URL('../../web/frontend/src/components/QuestionCards.tsx', import.meta.url), 'utf8');
const { outputText } = ts.transpileModule(source, {
  compilerOptions: {
    module: ts.ModuleKind.CommonJS,
    target: ts.ScriptTarget.ES2022,
    jsx: ts.JsxEmit.ReactJSX,
  },
});
const questionsSource = readFileSync(
  new URL('../../web/frontend/src/lib/questions.ts', import.meta.url), 'utf8');
const { outputText: questionsText } = ts.transpileModule(questionsSource, {
  compilerOptions: {
    module: ts.ModuleKind.CommonJS,
    target: ts.ScriptTarget.ES2022,
  },
});
const questions = { exports: {} };
runInNewContext(questionsText, { exports: questions.exports });
const component = { exports: {} };
runInNewContext(outputText, {
  exports: component.exports,
  require: name => {
    if (name === '../lib/api') return {};
    if (name === '../lib/questions') return questions.exports;
    if (name === '../lib/control') {
      return {
        optionSubmitValue: (option) => option && typeof option.value === 'string'
          && option.value ? option.value : option.label,
      };
    }
    return require(name);
  },
});
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');

test('approval cards preserve long multiline commands and both permission choices', () => {
  const command = `Codex 请求额外权限（仅本次）：python /data/project/Easel/${'long_segment_'.repeat(200)}/render.py\n第二行 <script>alert(1)</script>`;
  const rendered = renderToStaticMarkup(React.createElement(component.exports.default, {
    questions: [{
      id: 'codex-approval',
      questions: [{
        questionId: 'permission',
        header: '执行授权',
        question: command,
        options: [{ label: '允许本次' }, { label: '拒绝' }],
      }],
    }],
  }));
  assert.ok(rendered.includes('long_segment_'.repeat(200)));
  assert.ok(rendered.includes('\n第二行 &lt;script&gt;alert(1)&lt;/script&gt;'));
  assert.match(rendered, /<strong>允许本次<\/strong>/);
  assert.match(rendered, /<strong>拒绝<\/strong>/);
  assert.match(rendered, /disabled="">提交<\/button>/);
});

test('control options carry opaque values while showing human labels', () => {
  const rendered = renderToStaticMarkup(React.createElement(component.exports.default, {
    questions: [{
      id: 'q1',
      questions: [{
        questionId: 'permission',
        header: '授权',
        question: '允许？',
        options: [
          { label: '允许本次', value: 'qopt_allow' },
          { label: '拒绝', value: 'qopt_deny' },
        ],
      }],
    }],
    onAnswer: async () => ({ ok: true }),
  }));
  assert.match(rendered, /data-value="qopt_allow"/);
  assert.match(rendered, /data-value="qopt_deny"/);
  assert.match(rendered, /<strong>允许本次<\/strong>/);
  assert.match(rendered, /<strong>拒绝<\/strong>/);
  assert.equal(typeof questions.exports.questionAnswersFor, 'function');
  assert.deepEqual(
    JSON.parse(JSON.stringify(questions.exports.questionAnswersFor(
      [{ questionId: 'permission', question: '允许？' }],
      { permission: ['qopt_allow'] },
    ))),
    { permission: ['qopt_allow'] },
  );
});

test('approval card grids can shrink and wrap commands without clipping their content', () => {
  const styles = readFileSync(new URL('../../web/frontend/src/styles/index.css', import.meta.url), 'utf8');
  const rule = selector => {
    const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const matched = styles.match(new RegExp(`${escaped}\\s*\\{([^}]+)\\}`));
    assert.ok(matched, selector);
    return matched[1];
  };
  for (const selector of ['.question-card', '.question-card__item', '.question-card__option']) {
    assert.match(rule(selector), /grid-template-columns:\s*minmax\(0,\s*1fr\)/);
    assert.match(rule(selector), /min-width:\s*0/);
  }
  assert.match(rule('.question-card'), /overflow-wrap:\s*anywhere/);
  assert.match(rule('.question-card__title'), /white-space:\s*pre-wrap/);
  assert.match(rule('.question-card__options'), /minmax\(min\(150px,\s*100%\),\s*1fr\)/);
  assert.match(rule('.question-card__custom input'), /min-width:\s*0/);
});

for (const status of ['expired', 'cancelled', 'not_found', 'closed']) {
  test(`closed question ${status} remains readable but cannot be answered`, () => {
    const rendered = renderToStaticMarkup(React.createElement(component.exports.default, {
      questions: [{ id: 'old-question', status,
        questions: [{ questionId: 'choice', question: '原来的问题', options: [{ label: '原选项' }] }] }],
    }));
    assert.match(rendered, /原来的问题/);
    assert.match(rendered, /已过期或关闭/);
    assert.match(rendered, /disabled="">提交<\/button>/);
    assert.match(rendered, /disabled=""><strong>原选项/);
  });
}

test('expired timestamp prevents submission without inventing a hook deadline', () => {
  const rendered = renderToStaticMarkup(React.createElement(component.exports.default, {
    questions: [{ id: 'old-question', expiresAtMs: 1,
      questions: [{ questionId: 'choice', question: '过期问题', options: [{ label: '原选项' }] }] }],
  }));
  assert.match(rendered, /已过期或关闭/);
  assert.match(rendered, /disabled="">提交<\/button>/);
});

test('answered historical questions do not reappear', () => {
  const rendered = renderToStaticMarkup(React.createElement(component.exports.default, {
    questions: [{ id: 'old-question', status: 'answered', questions: [{ question: '已回答问题' }] }],
  }));
  assert.equal(rendered, '');
});
