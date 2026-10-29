/**
 * 原 Web 共用工作区数据层：control 走服务端目录/Run/SSE，local 保留原有
 * store/API。组件只消费 ChatSession/ChatMessage/StreamState，不自行推断
 * native_id、Cookie 或 workspace 路由。
 *
 * 所有请求复用 lib/auth 的身份 epoch、CSRF 与 401 归属判断；流式请求单独
 * 建立 AbortSignal，但 401 语义与 identityFetch 保持一致。
 */
import type { AuthContext, IdentityEpoch } from './auth';
import { AuthError, identityFetchRaw } from './auth';
import {
  consumeSseChunk, isTerminalRun, parseAdmission, parseMessagePage,
  parseQuestionData, parseRunPage, parseSessionPage, stateFromEvent,
  textFromEvent,
} from './control';
import type {
  ControlQuestion, RunMeta, ServerMessage, ServerSession, SseFrame,
} from './control';
import type { ChatMessage, ChatSession } from './store';
import type { ChatQuestion } from './api';

export interface ControlWorkspaceOptions {
  ctx: AuthContext;
  epoch: number;
  epochs: IdentityEpoch;
  fetchImpl?: typeof fetch;
  basePath?: string;
}

export interface SessionPage {
  sessions: ServerSession[];
  nextOffset: number | null;
}

export interface MessagePage {
  messages: ServerMessage[];
  total: number;
}

export interface RunStreamHandlers {
  onFrame?: (frame: SseFrame) => void;
  onState?: (state: { state: string; reason?: string },
             frame: SseFrame) => void;
  onToken?: (text: string, frame: SseFrame) => void;
  onThinking?: (text: string, frame: SseFrame) => void;
  onActivity?: (text: string, frame: SseFrame) => void;
  onErrorText?: (text: string, frame: SseFrame) => void;
  onQuestion?: (question: ControlQuestion, frame: SseFrame) => void;
}

export interface RunStreamResult {
  cursor: number;
  terminal: boolean;
  aborted: boolean;
}

export interface ControlWorkspaceClient {
  call<T>(path: string, init?: RequestInit): Promise<T>;
  listSessions(
    offset: number, limit?: number, signal?: AbortSignal,
  ): Promise<SessionPage>;
  createSession(title: string, signal?: AbortSignal): Promise<ServerSession>;
  updateSession(
    id: string, patch: { title?: string; archived?: boolean },
    signal?: AbortSignal,
  ): Promise<void>;
  deleteSession(id: string, signal?: AbortSignal): Promise<void>;
  loadMessages(
    id: string, offset: number, limit?: number, signal?: AbortSignal,
  ): Promise<MessagePage>;
  loadActiveRuns(id: string, signal?: AbortSignal): Promise<RunMeta[]>;
  submitTurn(
    sessionId: string, turnId: string, text: string,
    signal?: AbortSignal,
  ): Promise<{ runId: string; state: string; duplicate: boolean }>;
  streamRun(
    runId: string, after: number, handlers: RunStreamHandlers,
    signal?: AbortSignal,
  ): Promise<RunStreamResult>;
  stopRun(runId: string, signal?: AbortSignal): Promise<void>;
  questionStatus(
    runId: string, ids: string[], signal?: AbortSignal,
  ): Promise<Record<string, { status: string }>>;
  answerQuestion(
    runId: string, questionId: string,
    answers: Record<string, string[]>, signal?: AbortSignal,
  ): Promise<void>;
}

function jsonHeaders(): Record<string, string> {
  return { 'Content-Type': 'application/json' };
}

function requireId(payload: unknown): ServerSession {
  if (!payload || typeof payload !== 'object') {
    throw new AuthError(502, 'session_invalid');
  }
  const row = payload as Record<string, unknown>;
  if (typeof row.id !== 'string' || !row.id) {
    throw new AuthError(502, 'session_invalid');
  }
  return {
    id: row.id,
    workspaceId: typeof row.workspaceId === 'string' ? row.workspaceId : '',
    title: typeof row.title === 'string' ? row.title : '',
    archived: row.archived === true,
  };
}

function errorCode(text: string): string {
  try {
    const payload = JSON.parse(text) as { code?: unknown; detail?: unknown };
    const value = payload?.code ?? payload?.detail;
    return typeof value === 'string' && value ? value : '';
  } catch {
    return '';
  }
}

/** 服务端会话 → 原 ChatPage 视图；消息按需单独分页加载。 */
export function controlSessionView(server: ServerSession): ChatSession {
  return {
    id: server.id,
    title: server.title,
    messages: [],
    archived: server.archived,
    created: 0,
    remote: true,
  };
}

/** 服务端消息 → 原 MessageBubble 视图；只接受明确的字符串字段。 */
export function controlMessageView(message: ServerMessage): ChatMessage {
  const view: ChatMessage = {
    role: message.role === 'assistant' ? 'assistant' : 'user',
    content: message.content,
  };
  if (typeof message.thinking === 'string') view.thinking = message.thinking;
  if (typeof message.activity === 'string') view.activity = message.activity;
  return view;
}

/** 控制面问题 → 原 QuestionCards 视图；保留 opaque value。 */
export function controlQuestionView(question: ControlQuestion): ChatQuestion {
  return {
    id: question.id,
    questions: question.questions.map((item, index) => ({
      questionId: item.questionId || String(index),
      header: item.header,
      question: item.question || '',
      options: (item.options || []).map((option) => ({
        label: option.label,
        ...(option.value ? { value: option.value } : {}),
      })),
    })),
  };
}

export interface WorkspaceModeFeatures {
  personas: boolean;
  attachments: boolean;
  retry: boolean;
  localSettings: boolean;
}

/** control 暂未接通 local 宿主能力；local 保持原单机行为。 */
export function workspaceModeFeatures(
  mode: 'control' | 'local',
): WorkspaceModeFeatures {
  return mode === 'control'
    ? { personas: false, attachments: false, retry: false,
        localSettings: false }
    : { personas: true, attachments: true, retry: true,
        localSettings: true };
}

export function createControlWorkspaceClient(
  options: ControlWorkspaceOptions,
): ControlWorkspaceClient {
  const base = options.basePath || '';
  const fetchImpl = options.fetchImpl || fetch;

  async function call<T>(path: string, init?: RequestInit): Promise<T> {
    return (await identityFetchRaw<T>({
      epochs: options.epochs,
      epoch: options.epoch,
      ctx: options.ctx,
      url: `${base}${path}`,
      init,
      fetchImpl,
    })).body;
  }

  return {
    call,

    async listSessions(
      offset: number, limit = 100, signal?: AbortSignal,
    ): Promise<SessionPage> {
      const payload = await call<unknown>(
        `/api/sessions?limit=${limit}&offset=${offset}`
        + '&include_archived=true', { signal });
      return parseSessionPage(payload);
    },

    async createSession(
      title: string, signal?: AbortSignal,
    ): Promise<ServerSession> {
      const payload = await call<unknown>('/api/sessions', {
        method: 'POST',
        headers: jsonHeaders(),
        body: JSON.stringify({ title }),
        signal,
      });
      return requireId(payload);
    },

    async updateSession(
      id: string, patch: { title?: string; archived?: boolean },
      signal?: AbortSignal,
    ): Promise<void> {
      await call<unknown>(`/api/sessions/${encodeURIComponent(id)}`, {
        method: 'PATCH',
        headers: jsonHeaders(),
        body: JSON.stringify(patch),
        signal,
      });
    },

    async deleteSession(id: string, signal?: AbortSignal): Promise<void> {
      await call<unknown>(`/api/sessions/${encodeURIComponent(id)}`, {
        method: 'DELETE',
        signal,
      });
    },

    async loadMessages(
      id: string, offset: number, limit = 500, signal?: AbortSignal,
    ): Promise<MessagePage> {
      const payload = await call<unknown>(
        `/api/sessions/${encodeURIComponent(id)}/messages`
        + `?limit=${limit}&offset=${offset}`, { signal });
      return parseMessagePage(payload);
    },

    async loadActiveRuns(
      id: string, signal?: AbortSignal,
    ): Promise<RunMeta[]> {
      const runs: RunMeta[] = [];
      let offset = 0;
      for (let page = 0; page < 20; page += 1) {
        const parsed = parseRunPage(await call<unknown>(
          `/api/sessions/${encodeURIComponent(id)}/runs`
          + `?active=true&limit=200&offset=${offset}`, { signal }));
        runs.push(...parsed.runs);
        if (parsed.nextOffset === null || parsed.runs.length === 0) break;
        offset = parsed.nextOffset;
      }
      return runs;
    },

    async submitTurn(
      sessionId: string, turnId: string, text: string,
      signal?: AbortSignal,
    ) {
      const raw = await identityFetchRaw<unknown>({
        epochs: options.epochs,
        epoch: options.epoch,
        ctx: options.ctx,
        url: `${base}/api/chat`,
        init: {
          method: 'POST',
          headers: jsonHeaders(),
          body: JSON.stringify({ sessionId, turnId, message: text }),
          signal,
        },
        fetchImpl,
      });
      return parseAdmission(raw.status, raw.body);
    },

    async streamRun(
      runId: string, after: number, handlers: RunStreamHandlers,
      signal?: AbortSignal,
    ): Promise<RunStreamResult> {
      const controller = new AbortController();
      const abort = () => controller.abort();
      if (signal) {
        if (signal.aborted) controller.abort();
        else signal.addEventListener('abort', abort, { once: true });
      }
      let response: Response;
      try {
        response = await fetchImpl(
          `${base}/api/chat/jobs/${encodeURIComponent(runId)}/stream`
          + `?after=${Math.max(0, after)}`, {
            method: 'GET', credentials: 'same-origin', cache: 'no-store',
            signal: controller.signal,
          });
      } catch {
        if (controller.signal.aborted) {
          return { cursor: after, terminal: false, aborted: true };
        }
        throw new AuthError(0, 'network_unavailable');
      }
      if (response.status === 401) {
        const owned = options.epochs.isCurrent(options.epoch);
        if (owned) options.epochs.invalidate();
        const error = new AuthError(401, 'unauthorized');
        error.identityInvalidated = owned;
        throw error;
      }
      if (!response.ok) {
        let code = '';
        try { code = errorCode(await response.text()); } catch { /* ignore */ }
        throw new AuthError(response.status, code || `http_${response.status}`);
      }
      if (!response.body) {
        throw new AuthError(response.status, 'stream_unavailable');
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let cursor = Math.max(0, after);
      let terminal = false;
      const dispatch = (frame: SseFrame) => {
        if (frame.id > cursor) cursor = frame.id;
        handlers.onFrame?.(frame);
        if (frame.event === 'state') {
          const state = stateFromEvent(frame);
          if (!state) return;
          handlers.onState?.(state, frame);
          if (isTerminalRun(state.state)) terminal = true;
        } else if (frame.event === 'token') {
          handlers.onToken?.(textFromEvent(frame), frame);
        } else if (frame.event === 'thinking') {
          handlers.onThinking?.(textFromEvent(frame), frame);
        } else if (frame.event === 'activity') {
          handlers.onActivity?.(textFromEvent(frame), frame);
        } else if (frame.event === 'error') {
          handlers.onErrorText?.(textFromEvent(frame), frame);
        } else if (frame.event === 'question') {
          const question = parseQuestionData(frame.data);
          if (question) handlers.onQuestion?.(question, frame);
        }
      };
      try {
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          const parsed = consumeSseChunk(buffer, decoder.decode(value, {
            stream: true,
          }));
          buffer = parsed.rest;
          for (const frame of parsed.frames) dispatch(frame);
          if (terminal) break;
        }
        if (!terminal) {
          for (const frame of consumeSseChunk(buffer, '\n\n').frames) {
            dispatch(frame);
          }
        }
      } catch {
        if (controller.signal.aborted) {
          return { cursor, terminal, aborted: true };
        }
        throw new AuthError(0, 'stream_interrupted');
      } finally {
        signal?.removeEventListener('abort', abort);
        try { reader.releaseLock(); } catch { /* already closed */ }
      }
      return { cursor, terminal, aborted: controller.signal.aborted };
    },

    async stopRun(runId: string, signal?: AbortSignal): Promise<void> {
      await call<unknown>('/api/chat/stop', {
        method: 'POST',
        headers: jsonHeaders(),
        body: JSON.stringify({ runId }),
        signal,
      });
    },

    async questionStatus(
      runId: string, ids: string[], signal?: AbortSignal,
    ): Promise<Record<string, { status: string }>> {
      const payload = await call<unknown>('/api/chat/question/status', {
        method: 'POST',
        headers: jsonHeaders(),
        body: JSON.stringify({ runId, questionIds: ids }),
        signal,
      });
      const questions = (payload as {
        questions?: unknown } | null)?.questions;
      if (!questions || typeof questions !== 'object') return {};
      const result: Record<string, { status: string }> = {};
      for (const [key, value] of Object.entries(
        questions as Record<string, unknown>)) {
        const status = (value as { status?: unknown } | null)?.status;
        if (typeof status === 'string') result[key] = { status };
      }
      return result;
    },

    async answerQuestion(
      runId: string, questionId: string,
      answers: Record<string, string[]>, signal?: AbortSignal,
    ): Promise<void> {
      await call<unknown>('/api/chat/question/answer', {
        method: 'POST',
        headers: jsonHeaders(),
        body: JSON.stringify({ runId, questionId, answers }),
        signal,
      });
    },
  };
}
