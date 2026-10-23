/**
 * control 前端协议纯工具：模式判定、admission、SSE 帧与恢复计划。
 *
 * 本模块不读写 DOM/存储，Node 行为测试可直接执行；所有服务器返回都先
 * 校验形状，未知/损坏数据明确失败，不伪装成空历史。
 */

export type RuntimeMode = 'control' | 'local';

export interface ServerSession {
  id: string;
  workspaceId: string;
  title: string;
  archived: boolean;
}

export interface RunMeta {
  runId: string;
  conversationId: string;
  state: string;
}

export interface Admission {
  runId: string;
  state: string;
  duplicate: boolean;
}

export interface SseFrame {
  id: number;
  event: string;
  data: string;
}

export interface ControlQuestionOption {
  label: string;
  value?: string;
}

export interface ControlQuestionItem {
  questionId?: string;
  header?: string;
  question?: string;
  options?: ControlQuestionOption[];
}

export interface ControlQuestion {
  id: string;
  questions: ControlQuestionItem[];
}

export interface ServerMessage {
  role: string;
  content: string;
  [key: string]: unknown;
}

export const ACTIVE_RUN_STATES = [
  'accepted', 'reserved', 'running', 'awaiting_permission',
] as const;
export const TERMINAL_RUN_STATES = [
  'done', 'failed', 'interrupted', 'cancelled',
] as const;

export function isActiveRun(state: unknown): boolean {
  return typeof state === 'string'
    && (ACTIVE_RUN_STATES as readonly string[]).includes(state);
}

export function isTerminalRun(state: unknown): boolean {
  return typeof state === 'string'
    && (TERMINAL_RUN_STATES as readonly string[]).includes(state);
}

/**
 * 服务端可信启动信息；缺失/未知/网络失败一律失败关闭，绝不解释为 local。
 */
export async function detectRuntimeMode(
  fetchImpl: typeof fetch = fetch,
): Promise<RuntimeMode> {
  let response: Response;
  try {
    response = await fetchImpl('/api/runtime', {
      method: 'GET',
      credentials: 'same-origin',
      cache: 'no-store',
    });
  } catch {
    throw new Error('runtime_unavailable');
  }
  if (!response.ok) throw new Error('runtime_unavailable');
  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    throw new Error('runtime_unknown');
  }
  const mode = (payload as { mode?: unknown } | null)?.mode;
  if (mode === 'control' || mode === 'local') return mode;
  throw new Error('runtime_unknown');
}

export function parseAdmission(status: number, payload: unknown): Admission {
  if (status !== 202) throw new Error(`admission_http_${status}`);
  if (!payload || typeof payload !== 'object') {
    throw new Error('admission_invalid');
  }
  const data = payload as Record<string, unknown>;
  const runId = data.runId;
  const state = data.state;
  if (typeof runId !== 'string' || !runId
      || typeof state !== 'string' || !state) {
    throw new Error('admission_invalid');
  }
  return { runId, state, duplicate: data.duplicate === true };
}

export function parseSessionPage(payload: unknown): {
  sessions: ServerSession[];
  nextOffset: number | null;
} {
  if (!payload || typeof payload !== 'object') {
    throw new Error('sessions_invalid');
  }
  const data = payload as Record<string, unknown>;
  if (!Array.isArray(data.sessions)) throw new Error('sessions_invalid');
  const sessions = data.sessions.map((item) => {
    const row = item as Record<string, unknown>;
    if (!row || typeof row.id !== 'string' || !row.id
        || typeof row.title !== 'string'
        || typeof row.archived !== 'boolean') {
      throw new Error('sessions_invalid');
    }
    return {
      id: row.id,
      workspaceId: typeof row.workspaceId === 'string' ? row.workspaceId : '',
      title: row.title,
      archived: row.archived,
    };
  });
  const next = data.nextOffset;
  return {
    sessions,
    nextOffset: typeof next === 'number' && Number.isInteger(next) ? next : null,
  };
}

export function parseRunPage(payload: unknown): {
  runs: RunMeta[];
  nextOffset: number | null;
} {
  if (!payload || typeof payload !== 'object') {
    throw new Error('runs_invalid');
  }
  const data = payload as Record<string, unknown>;
  if (!Array.isArray(data.runs)) throw new Error('runs_invalid');
  const runs = data.runs.map((item) => {
    const row = item as Record<string, unknown>;
    if (!row || typeof row.runId !== 'string' || !row.runId
        || typeof row.conversationId !== 'string'
        || typeof row.state !== 'string') {
      throw new Error('runs_invalid');
    }
    return {
      runId: row.runId,
      conversationId: row.conversationId,
      state: row.state,
    };
  });
  const next = data.nextOffset;
  return {
    runs,
    nextOffset: typeof next === 'number' && Number.isInteger(next) ? next : null,
  };
}

export function parseMessages(payload: unknown): ServerMessage[] {
  if (!payload || typeof payload !== 'object') {
    throw new Error('messages_invalid');
  }
  const data = payload as Record<string, unknown>;
  if (!Array.isArray(data.messages)) throw new Error('messages_invalid');
  return data.messages.map((item) => {
    const row = item as Record<string, unknown>;
    if (!row || typeof row.role !== 'string'
        || typeof row.content !== 'string') {
      throw new Error('messages_invalid');
    }
    return { ...row, role: row.role, content: row.content } as ServerMessage;
  });
}

/**
 * 恢复计划：只发现本人活跃 Run 并连接回放；任何情况下都不重新 POST prompt。
 */
export function recoveryAction(
  runs: RunMeta[],
  storedRunId: string | null,
): { action: 'stream' | 'snapshot'; runId: string | null } {
  if (storedRunId) {
    const stored = runs.find((run) => run.runId === storedRunId);
    if (stored && isActiveRun(stored.state)) {
      return { action: 'stream', runId: stored.runId };
    }
  }
  const active = runs.slice().reverse().find((run) => isActiveRun(run.state));
  if (active) return { action: 'stream', runId: active.runId };
  return { action: 'snapshot', runId: null };
}

/** 逐块解析 SSE；返回已完整帧与未完成尾缓冲。 */
export function consumeSseChunk(
  buffer: string,
  chunk: string,
): { frames: SseFrame[]; rest: string } {
  const text = buffer + chunk;
  const blocks = text.split(/\r?\n\r?\n/);
  const rest = blocks.pop() ?? '';
  const frames: SseFrame[] = [];
  for (const block of blocks) {
    let id = 0;
    let event = '';
    const data: string[] = [];
    for (const line of block.split(/\r?\n/)) {
      if (line.startsWith('id:')) {
        id = Number(line.slice(3).trim()) || 0;
      } else if (line.startsWith('event:')) {
        event = line.slice(6).trim();
      } else if (line.startsWith('data:')) {
        data.push(line.slice(5).replace(/^ /, ''));
      }
    }
    if (event && data.length) {
      frames.push({ id, event, data: data.join('\n') });
    }
  }
  return { frames, rest };
}

function parseJsonText(data: string): unknown {
  try {
    return JSON.parse(data);
  } catch {
    return undefined;
  }
}

/** token/thinking/activity/error 的 data 是 JSON 字符串（兼容裸文本）。 */
export function textFromEvent(frame: SseFrame): string {
  const parsed = parseJsonText(frame.data);
  return typeof parsed === 'string' ? parsed : frame.data;
}

/** state 事件 data 是对象；未知形状不更新 UI。 */
export function stateFromEvent(
  frame: SseFrame,
): { state: string; reason?: string } | null {
  const parsed = parseJsonText(frame.data);
  if (!parsed || typeof parsed !== 'object') return null;
  const data = parsed as Record<string, unknown>;
  if (typeof data.state !== 'string' || !data.state) return null;
  const result: { state: string; reason?: string } = { state: data.state };
  if (typeof data.reason === 'string') result.reason = data.reason;
  return result;
}

/**
 * question 事件负载可能是双层 JSON 字符串；只接受带 questions 数组的对象。
 */
export function parseQuestionData(data: string): ControlQuestion | null {
  let value = parseJsonText(data);
  if (typeof value === 'string') value = parseJsonText(value);
  if (!value || typeof value !== 'object') return null;
  const payload = value as Record<string, unknown>;
  if (typeof payload.id !== 'string' || !Array.isArray(payload.questions)) {
    return null;
  }
  const questions: ControlQuestionItem[] = [];
  for (const item of payload.questions) {
    if (!item || typeof item !== 'object') return null;
    const row = item as Record<string, unknown>;
    const question: ControlQuestionItem = {
      questionId: typeof row.questionId === 'string' ? row.questionId : '',
      header: typeof row.header === 'string' ? row.header : '',
      question: typeof row.question === 'string' ? row.question : '',
      options: [],
    };
    const options = row.options;
    if (Array.isArray(options)) {
      for (const option of options) {
        if (!option || typeof option !== 'object') continue;
        const entry = option as Record<string, unknown>;
        const label = entry.label;
        if (typeof label !== 'string' || !label) continue;
        question.options!.push({
          label,
          value: typeof entry.value === 'string' && entry.value
            ? entry.value : undefined,
        });
      }
    }
    questions.push(question);
  }
  return { id: payload.id, questions };
}

/** 题卡提交值：opaque value 优先；local 旧题卡回退 label。 */
export function optionSubmitValue(option: ControlQuestionOption): string {
  return option.value && option.value.length > 0
    ? option.value : option.label;
}

export function questionAnswerKey(
  question: ControlQuestionItem,
  index: number,
): string {
  return question.questionId || String(index);
}
