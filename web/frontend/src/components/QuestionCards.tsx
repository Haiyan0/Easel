import { useMemo, useState, useRef, useEffect } from 'react';
import type {
  ChatQuestion, ChatQuestionItem, ChatQuestionOption,
} from '../lib/api';
import { answerQuestion, questionStatus } from '../lib/api';
import { optionSubmitValue } from '../lib/control';
import { questionAnswersFor } from '../lib/questions';

/**
 * ask_user 问答题卡片。
 *
 * OpenClaw 的 ask_user 一次可带 1-3 个问题（question.questions[]）。gateway 的
 * question.resolve 要求 answers 里**每个问题都有答案**（单个缺失即报
 * QUESTION_INVALID_ANSWER: "question 'xxx' requires an answer"）。
 * 因此多问题时渲染全部题目，用户每问答完一项，全部选齐后自动提交一次。
 *
 * 多选：问题对象带 multiSelect: true 时，answers[qid] 可携带多个 label
 * （gateway 对 multiSelect=false 的多值直接报 "does not allow multiple answers"，
 * 前端只按数据渲染，不绕过校验）。
 */
interface AnswerResult {
  ok: boolean;
  error?: string;
}

interface QuestionCardProps {
  question: ChatQuestion;
  onAnswered: () => void;
  onClosed?: (status: string) => void;
  onAnswer?: (
    question: ChatQuestion, answers: Record<string, string[]>,
  ) => Promise<AnswerResult>;
  pending?: boolean;
}

function QuestionCard({
  question, onAnswered, onAnswer, onClosed, pending = false,
}: QuestionCardProps) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [selected, setSelected] = useState<Record<string, string[]>>({});
  const [custom, setCustom] = useState<Record<string, string>>({});
  const [customVals, setCustomVals] = useState<Record<string, string[]>>({});
  const [showCustom, setShowCustom] = useState<Record<string, boolean>>({});
  const [now, setNow] = useState(Date.now());
  const [closed, setClosed] = useState('');
  const deadlineReached = typeof question.expiresAtMs === 'number' && now >= question.expiresAtMs;
  const status = closed || question.status || 'pending';
  const expired = deadlineReached || (status !== 'pending' && status !== 'unknown');
  const locked = busy || expired;
  const closedCallback = useRef(onClosed);
  closedCallback.current = onClosed;

  useEffect(() => {
    if (onAnswer || status !== 'pending' || deadlineReached) return;
    let active = true;
    const tick = () => {
      setNow(Date.now());
      void questionStatus([question.id]).then(states => {
        const next = states[question.id]?.status;
        if (active && next && next !== 'pending' && next !== 'unknown') {
          setClosed(next);
          closedCallback.current?.(next);
        }
      });
    };
    tick();
    const timer = setInterval(tick, 5000);
    return () => { active = false; clearInterval(timer); };
  }, [question.id, status, deadlineReached, onAnswer]);

  const items: ChatQuestionItem[] = question.questions || [];
  const valsOf = (it: ChatQuestionItem): string[] => selected[it.questionId] || [];
  const allAnswered = items.length > 0 && items.every((it) => valsOf(it).length > 0);

  const pick = (it: ChatQuestionItem, option: ChatQuestionOption) => {
    const value = optionSubmitValue(option);
    setSelected((s) => {
      const cur = s[it.questionId] || [];
      const next = it.multiSelect
        ? (cur.includes(value)
          ? cur.filter((v) => v !== value) : [...cur, value])
        : [value];
      return { ...s, [it.questionId]: next };
    });
  };

  const fillCustom = (it: ChatQuestionItem) => {
    const v = (custom[it.questionId] || '').trim();
    if (!v) return;
    setSelected((s) => {
      const cur = s[it.questionId] || [];
      return { ...s, [it.questionId]: it.multiSelect && !cur.includes(v) ? [...cur, v] : [v] };
    });
    setCustomVals((c) => {
      const cur = c[it.questionId] || [];
      return { ...c, [it.questionId]: cur.includes(v) ? cur : [...cur, v] };
    });
  };

  const removeCustom = (it: ChatQuestionItem, v: string) => {
    setSelected((s) => ({ ...s, [it.questionId]: (s[it.questionId] || []).filter((x) => x !== v) }));
    setCustomVals((c) => ({ ...c, [it.questionId]: (c[it.questionId] || []).filter((x) => x !== v) }));
  };

  const submit = async () => {
    if (locked || pending || !allAnswered) return;
    setBusy(true); setError('');
    const answers = questionAnswersFor(items, selected);
    try {
      const res = onAnswer
        ? await onAnswer(question, answers)
        : await answerQuestion({ questionId: question.id, answers });
      if (!res.ok) setError(res.error || '提交失败');
      else onAnswered();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="message-row assistant">
      <div className="msg-col assistant" style={{ maxWidth: '80%' }}>
        <div className="question-card"
          data-pending={pending ? 'true' : 'false'}>
          {items.map((it, idx) => {
            const options: { label: string; description?: string }[] = it.options || [];
            const vals = valsOf(it);
            const ctl = showCustom[it.questionId];
            const cvals = customVals[it.questionId] || [];
            return (
              <div key={it.questionId} className="question-card__item">
                {idx > 0 && <div className="question-card__divider" />}
                {it.header && <div className="question-card__chip">{it.header}</div>}
                <div className="question-card__title">
                  {it.question || '请选择'}
                  {it.multiSelect && <span className="question-card__multi">可多选</span>}
                </div>
                <div className="question-card__options">
                  {options.map((opt) => {
                    const value = optionSubmitValue(opt);
                    return (
                      <button key={opt.label}
                        data-value={value}
                        className={`question-card__option${vals.includes(value) ? ' question-card__option--selected' : ''}`}
                        disabled={locked}
                        onClick={() => pick(it, opt)}>
                        <strong>{opt.label}</strong>
                        {opt.description && <span>{opt.description}</span>}
                      </button>
                    );
                  })}
                  {cvals.map((v) => (
                    <button key={`custom-${v}`}
                      className="question-card__option question-card__option--selected"
                      disabled={locked}
                      title="点击移除"
                      onClick={() => removeCustom(it, v)}>
                      <strong>{v}</strong>
                      <span>自定义 · 点击移除</span>
                    </button>
                  ))}
                  <button className="question-card__other" disabled={locked}
                    onClick={() => setShowCustom((s) => ({ ...s, [it.questionId]: !s[it.questionId] }))}>
                    {ctl ? '收起自定义输入' : '自行输入…'}
                  </button>
                </div>
                {ctl && (
                  <div className="question-card__custom">
                    <input
                      type="text"
                      disabled={locked}
                      value={custom[it.questionId] || ''}
                      placeholder="输入你的答案"
                      onChange={(e) => setCustom((c) => ({ ...c, [it.questionId]: e.target.value }))}
                    />
                    <button disabled={locked || !(custom[it.questionId] || '').trim()}
                      onClick={() => fillCustom(it)}>
                      填入
                    </button>
                  </div>
                )}
              </div>
            );
          })}

          {/* 单选题：选完点提交；多题：全部选齐后提交（单选/多选同一规则） */}
          {items.length === 1 ? (
            <div className="question-card__actions">
              <button className="question-card__submit"
                disabled={locked || pending || !allAnswered}
                onClick={() => void submit()}>
                提交
              </button>
            </div>
          ) : (
            allAnswered && !locked && (
              <div className="question-card__actions">
                <button className="question-card__submit"
                  disabled={locked || pending} onClick={() => void submit()}>
                  全部已选，提交
                </button>
              </div>
            )
          )}
          {pending && !busy && (
            <div className="question-card__hint">正在确认问题状态…</div>
          )}
          {busy && <div className="question-card__hint">已提交，Agent 继续处理中…</div>}
          {expired && <div className="question-card__hint">问题已过期或关闭，请勿重复提交。</div>}
          {error && <div className="question-card__hint question-card__error">{error}</div>}
        </div>
      </div>
    </div>
  );
}

/** 流式中的 ask_user 问答题列表（一次性卡片，答完即从当前流移除）。 */
export default function QuestionCards({
  questions, onDone, onAnswer, onClosed, pending = false,
}: {
  questions: ChatQuestion[];
  onDone?: (questionId: string) => void;
  onClosed?: (id: string, status: string) => void;
  onAnswer?: (
    question: ChatQuestion, answers: Record<string, string[]>,
  ) => Promise<AnswerResult>;
  pending?: boolean;
}) {
  const [answered, setAnswered] = useState<Record<string, boolean>>({});
  const visible = useMemo(
    () => questions.filter((q) => !answered[q.id] && q.status !== 'answered' && q.status !== 'resolved'),
    [questions, answered],
  );
  if (!visible.length) return null;
  return (
    <>
      {visible.map((q) => (
        <QuestionCard key={q.id} question={q} onAnswer={onAnswer}
          onClosed={(status) => onClosed?.(q.id, status)}
          pending={pending}
          onAnswered={() => {
            setAnswered((a) => ({ ...a, [q.id]: true }));
            onDone?.(q.id);   // 通知 App：此题已答，重放/恢复不再出现
          }} />
      ))}
    </>
  );
}
