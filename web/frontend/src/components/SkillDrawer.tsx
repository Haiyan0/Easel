import { useState, useEffect, useMemo, useRef } from 'react';
import { saveEnv } from '../lib/api';
import type { ChatQuestion, SkillDetail } from '../lib/api';
import QuestionCards from './QuestionCards';
import { useWorkspaceData } from '../lib/workspaceDataContext';
import { workspaceErrorMessage } from '../lib/workspaceData';
import { renderMarkdown } from '../lib/sanitize';
import { displayName } from '../lib/skillDisplayNames';

interface SkillDrawerProps {
  skillName: string;
  persona: string;
  onClose: () => void;
  onConfigured: () => void;   // 保存 API 后通知父组件刷新卡片状态
}

export default function SkillDrawer({ skillName, persona, onClose, onConfigured }: SkillDrawerProps) {
  const data = useWorkspaceData();
  const [detail, setDetail] = useState<SkillDetail | null>(null);
  const [loadErr, setLoadErr] = useState('');

  // API 配置输入（env -> 明文，只提交非空项）
  const [envInputs, setEnvInputs] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [savedMsg, setSavedMsg] = useState('');

  // 执行区
  const [input, setInput] = useState('');
  const [result, setResult] = useState('');
  const [running, setRunning] = useState(false);
  const [runErr, setRunErr] = useState('');
  const [activeRunId, setActiveRunId] = useState('');
  const [question, setQuestion] = useState<ChatQuestion | null>(null);
  const [questionPending, setQuestionPending] = useState(false);
  const reqSeq = useRef(0);
  const runRequest = useRef<AbortController | null>(null);
  const pendingTurn = useRef<{ input: string; id: string } | null>(null);

  const loadDetail = () => {
    const controller = new AbortController();
    data.fetchSkillDetail(skillName, controller.signal)
      .then((d) => {
        if (!controller.signal.aborted) { setDetail(d); setLoadErr(''); }
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted) {
          setLoadErr(workspaceErrorMessage(error, '加载 SKILL 详情失败'));
        }
      });
    return () => controller.abort();
  };

  useEffect(loadDetail, [data, skillName]);

  useEffect(() => {
    if (!detail?.name) return;
    let alive = true;
    let timer: number | undefined;
    const controller = new AbortController();
    const check = async () => {
      try {
        const status = await data.skillStatus(
          detail.name, controller.signal);
        if (!alive) return;
        if (status.response) setResult(status.response);
        setQuestion(status.question || null);
        let questionPending = status.questionPending === true;
        if (status.question && status.runId) {
          try {
            const states = await data.taskQuestionStatus(
              status.runId, [status.question.id], controller.signal);
            const state = states[status.question.id]?.status;
            if (state === 'pending') questionPending = false;
            else if (state === 'answered' || state === 'resolved'
                || state === 'expired' || state === 'not_found') {
              setQuestion(null);
              questionPending = false;
            } else {
              questionPending = true;
            }
          } catch {
            // 未知状态保留题卡但禁用提交；重连/下一次轮询继续确认。
            questionPending = true;
          }
        }
        if (!alive) return;
        setQuestionPending(questionPending);
        if (status.state === 'running'
            || status.state === 'awaiting_permission') {
          setRunning(true);
          if (status.runId) setActiveRunId(status.runId);
          timer = window.setTimeout(check, status.state === 'awaiting_permission'
            ? 1500 : 3000);
        } else {
          setRunning(false);
          if (status.state === 'failed') {
            setRunErr((current) => current || '上次技能任务未完成，可重新执行。');
          }
        }
      } catch (error) {
        if (!alive || controller.signal.aborted) return;
        // 状态查询失败不覆盖当前编辑/结果；题卡仍保留但禁止提交。
        setQuestionPending(true);
        setRunErr((current) => current || workspaceErrorMessage(
          error, '技能任务状态读取失败'));
      }
    };
    void check();
    return () => {
      alive = false;
      controller.abort();
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [data, detail?.name, activeRunId]);

  useEffect(() => () => runRequest.current?.abort(), []);

  const bodyHtml = useMemo(() => renderMarkdown(detail?.body || ''), [detail?.body]);
  const resultHtml = useMemo(() => renderMarkdown(result), [result]);

  const handleSaveEnv = async () => {
    const updates = Object.fromEntries(
      Object.entries(envInputs).filter(([, v]) => v.trim() !== '')
    );
    if (!data.skillEnvEnabled) {
      setSavedMsg('受管工作区不支持在此写入全局 .env');
      return;
    }
    if (Object.keys(updates).length === 0) { setSavedMsg('没有填写新值'); return; }
    setSaving(true);
    setSavedMsg('');
    try {
      await saveEnv(updates);
      const fresh = await data.fetchSkillDetail(skillName);
      setEnvInputs({});
      setDetail(fresh);
      setSavedMsg('已保存 ✓');
      onConfigured();
      setTimeout(() => setSavedMsg(''), 2500);
    } catch (e) {
      setSavedMsg(workspaceErrorMessage(e, '保存失败'));
    } finally {
      setSaving(false);
    }
  };

  const handleRun = async () => {
    if (!input.trim() || running) return;
    const seq = ++reqSeq.current;
    runRequest.current?.abort();
    const controller = new AbortController();
    runRequest.current = controller;
    setRunning(true);
    setRunErr('');
    setResult('');
    setActiveRunId('');
    const text = input.trim();
    const turnId = pendingTurn.current?.input === text
      ? pendingTurn.current.id
      : `skill:${Date.now().toString(36)}:${Math.random().toString(36).slice(2, 10)}`;
    pendingTurn.current = { input: text, id: turnId };
    try {
      const res = await data.executeSkill(
        skillName, text, persona || undefined,
        controller.signal, turnId,
        (runId) => { if (seq === reqSeq.current) setActiveRunId(runId); });
      if (seq === reqSeq.current && !controller.signal.aborted) {
        pendingTurn.current = null;
        setResult(res.response);
      }
    } catch (e) {
      if (seq === reqSeq.current && !controller.signal.aborted) {
        setRunErr(workspaceErrorMessage(e, '执行失败'));
      }
    } finally {
      if (seq === reqSeq.current) setRunning(false);
    }
  };

  const handleStop = async () => {
    if (!activeRunId) return;
    try {
      await data.stopTask(activeRunId);
    } catch (error) {
      setRunErr(workspaceErrorMessage(error, '停止任务失败'));
    }
  };

  const handleAnswer = async (
    current: ChatQuestion, answers: Record<string, string[]>,
  ) => {
    if (!activeRunId) {
      return { ok: false, error: '任务尚未接纳，无法回答问题。' };
    }
    const result = await data.answerTaskQuestion(
      activeRunId, current.id, answers);
    if (result.ok) {
      setQuestion(null);
      setQuestionPending(false);
    }
    return result;
  };

  const blocked = detail?.needsApi && !detail.apiConfigured;

  return (
    <div className="drawer-overlay" onClick={onClose}>
      <div className="drawer" onClick={(e) => e.stopPropagation()}>
        <div className="drawer-header">
          <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 12 }}>
            <div>
              <div className="skill-detail-title">{displayName(skillName)}</div>
              <div className="skill-detail-rawname">{skillName}</div>
              <div className="skill-detail-meta">
                {detail?.layer && <span className="badge badge-accent">{{discover:'发现',plan:'策划',produce:'制作',publish:'发布',attribute:'归因',general:'通用'}[detail.layer] || detail.layer}</span>}
                {detail?.needsApi && (
                  detail.apiConfigured
                    ? <span className="badge badge-ok">✓ 已配置</span>
                    : <span className="badge badge-warn">❗ 需配置 API</span>
                )}
              </div>
            </div>
            <button className="icon-btn" onClick={onClose} title="关闭">×</button>
          </div>
        </div>

        <div className="drawer-body">
          {loadErr && <div style={{ color: 'var(--red)', fontSize: 14 }}>{loadErr}</div>}

          {/* API 配置 */}
          {detail?.needsApi && detail.apiSpec && data.skillEnvEnabled && (
            <div className="panel">
              <div className="panel-title">🔑 {detail.apiSpec.label} · API 配置
                <span style={{ fontWeight: 400, color: 'var(--text-secondary)', fontSize: 12 }}>
                  （任选一个服务商填齐即可用）
                </span>
              </div>
              {detail.apiSpec.settings.length > 0 && (
                <div className="provider-block">
                  <div className="provider-head"><strong style={{ fontSize: 13 }}>默认选择与能力</strong></div>
                  {detail.apiSpec.settings.map((k) => (
                    <div key={k.env}>
                      <label className="field-label">
                        {k.label} · 可选
                        {k.configured && <span style={{ color: 'var(--green)', marginLeft: 6 }}>
                          已配置{k.masked ? `：${k.masked}` : ''}
                        </span>}
                      </label>
                      {k.choices.length > 0 ? (
                        <select
                          className="field"
                          value={envInputs[k.env] ?? ''}
                          onChange={(e) => setEnvInputs((p) => ({ ...p, [k.env]: e.target.value }))}
                        >
                          <option value="">{k.configured ? `当前：${k.masked}` : `请选择 ${k.env}`}</option>
                          {k.choices.map((choice) => <option key={choice} value={choice}>{choice}</option>)}
                        </select>
                      ) : (
                        <input
                          className="field"
                          type={k.secret ? 'password' : 'text'}
                          placeholder={k.configured ? '留空则保持不变，输入以覆盖' : `请输入 ${k.env}`}
                          value={envInputs[k.env] || ''}
                          onChange={(e) => setEnvInputs((p) => ({ ...p, [k.env]: e.target.value }))}
                        />
                      )}
                    </div>
                  ))}
                </div>
              )}
              {detail.apiSpec.providers.map((prov) => {
                const provOk = prov.keys.filter(k => k.required).every(k => k.configured);
                return (
                  <div key={prov.id} className={`provider-block ${provOk ? 'configured' : ''}`}>
                    <div className="provider-head">
                      <strong style={{ fontSize: 13 }}>{prov.name}</strong>
                      {provOk
                        ? <span className="badge badge-ok">✓ 就绪</span>
                        : <span className="badge">未配置</span>}
                    </div>
                    {prov.keys.map((k) => (
                      <div key={k.env}>
                        <label className="field-label">
                          {k.label}{k.required ? '' : ' · 可选'}
                          {k.configured && <span style={{ color: 'var(--green)', marginLeft: 6 }}>
                            已配置{k.secret && k.masked ? `（${k.masked}）` : k.masked ? `：${k.masked}` : ''}
                          </span>}
                        </label>
                        {k.choices.length > 0 ? (
                          <select className="field" value={envInputs[k.env] ?? ''}
                            onChange={(e) => setEnvInputs((p) => ({ ...p, [k.env]: e.target.value }))}>
                            <option value="">{k.configured ? `当前：${k.masked}` : `请选择 ${k.env}`}</option>
                            {k.choices.map((choice) => <option key={choice} value={choice}>{choice}</option>)}
                          </select>
                        ) : (
                          <input
                            className="field"
                            type={k.secret ? 'password' : 'text'}
                            placeholder={k.configured ? '留空则保持不变，输入以覆盖' : `请输入 ${k.env}`}
                            value={envInputs[k.env] || ''}
                            onChange={(e) => setEnvInputs((p) => ({ ...p, [k.env]: e.target.value }))}
                          />
                        )}
                      </div>
                    ))}
                  </div>
                );
              })}
              <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginTop: 8 }}>
                <button className="btn btn-primary btn-sm" onClick={handleSaveEnv} disabled={saving}>
                  {saving ? '保存中…' : '保存到 .env'}
                </button>
                {savedMsg && <span style={{ fontSize: 13, color: savedMsg.includes('✓') ? 'var(--green)' : 'var(--text-secondary)' }}>{savedMsg}</span>}
              </div>
            </div>
          )}

          {detail?.needsApi && !detail.apiConfigured && !data.skillEnvEnabled && (
            <div className="panel" style={{ color: 'var(--amber)', fontSize: 13 }}>
              该技能需要受管 API/依赖；当前工作区未配置，且成员不能在此写入全局 .env。
            </div>
          )}

          {/* 执行 */}
          <div className="panel">
            <div className="panel-title">▶ 运行</div>
            {blocked && (
              <div style={{ fontSize: 13, color: 'var(--amber)', marginBottom: 10 }}>
                该 SKILL 需要先配置上面的 API 才能运行。
              </div>
            )}
            <textarea
              className="field"
              placeholder="输入内容，例如主题 / 素材 / 要求…"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              style={{ minHeight: 100 }}
            />
            <div style={{ marginTop: 10 }}>
              <button className="btn btn-primary" onClick={handleRun} disabled={running || !input.trim() || blocked}>
                {running
                  ? <><span className="spinner" style={{ width: 14, height: 14, margin: 0 }} />执行中…</>
                  : '执行'}
              </button>
              {running && activeRunId && (
                <button className="btn btn-sm" style={{ marginLeft: 8 }}
                  onClick={handleStop}>停止</button>
              )}
            </div>
            {runErr && <div style={{ color: 'var(--red)', fontSize: 13, marginTop: 10 }}>{runErr}</div>}
            {question && (
              <div style={{ marginTop: 12 }}>
                <QuestionCards
                  questions={[question]}
                  pending={questionPending}
                  onDone={() => setQuestion(null)}
                  onAnswer={handleAnswer}
                />
              </div>
            )}
            {resultHtml && (
              <div className="skill-result" dangerouslySetInnerHTML={{ __html: resultHtml }} />
            )}
          </div>

          {/* 描述 */}
          <div className="panel">
            <div className="panel-title">📖 说明</div>
            {detail
              ? <div className="skill-body-md" dangerouslySetInnerHTML={{ __html: bodyHtml }} />
              : !loadErr && <div className="loading"><div className="spinner" />加载中…</div>}
          </div>
        </div>
      </div>
    </div>
  );
}
