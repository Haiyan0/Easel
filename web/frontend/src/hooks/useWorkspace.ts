/**
 * control 工作区状态与命令：服务端会话目录、历史窗口、Run 接纳、SSE 游标、
 * 停止、问题卡与身份生命周期。
 *
 * 返回值使用原 ChatPage 的 ChatSession/ChatMessage/StreamState 视图类型；
 * 所有异步提交都先复核 userId/workspaceId/epoch、当前会话与消息代次。
 * local 模式继续走原有 store/API，不经过本 hook。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { AuthContext, IdentityEpoch } from '../lib/auth';
import { AuthError, identityCacheKey } from '../lib/auth';
import type { ControlQuestion, ServerSession } from '../lib/control';
import {
  isActiveRun, isTerminalRun, olderMessageWindow, recoveryAction,
} from '../lib/control';
import type { ChatQuestion } from '../lib/api';
import type { ChatMessage, ChatSession, StreamState } from '../lib/store';
import {
  controlMessageView, controlQuestionView, controlSessionView,
  createControlWorkspaceClient,
} from '../lib/workspaceClient';
import type { ControlWorkspaceClient } from '../lib/workspaceClient';

interface Recovery {
  runId: string;
  turnId: string;
  eventId: number;
}

interface ClientRun {
  runId: string;
  turnId: string;
  sessionId: string;
  state: string;
  content: string;
  thinking: string;
  activity: string;
  question: ChatQuestion | null;
  answered: string[];
  cursor: number;
}

interface LocalSend {
  sessionId: string;
  turnId: string;
  text: string;
  generation: number;
  status: 'sending' | 'failed';
  runId?: string;
}

export interface UseWorkspaceOptions {
  ctx: AuthContext;
  epoch: number;
  epochs: IdentityEpoch;
  onExpired: () => void;
}

export interface UseWorkspaceResult {
  sessions: ChatSession[];
  activeId: string | null;
  activeSession: ChatSession | null;
  sessionNextOffset: number | null;
  loading: boolean;
  historyError: string;
  notice: string;
  messageStart: number;
  messageTotal: number;
  hasOlder: boolean;
  loadingOlder: boolean;
  stream: StreamState | undefined;
  streamActive: boolean;
  canReconnect: boolean;
  failedTurnId: string | null;
  call<T>(path: string, init?: RequestInit): Promise<T>;
  selectSession(id: string): void;
  newChat(): Promise<void>;
  renameSession(id: string, title: string): Promise<void>;
  archiveSession(id: string, archived: boolean): Promise<void>;
  deleteSession(id: string): Promise<void>;
  loadMoreSessions(): Promise<void>;
  loadOlder(): Promise<void>;
  send(text: string): Promise<void>;
  retryFailed(): Promise<void>;
  stop(): Promise<void>;
  reconnect(): void;
  answer(
    question: ChatQuestion, answers: Record<string, string[]>,
  ): Promise<void>;
}

function makeTurnId(): string {
  try {
    if (typeof crypto !== 'undefined' && crypto.randomUUID) {
      return crypto.randomUUID();
    }
  } catch { /* fall through */ }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

function readRecovery(ctx: AuthContext, sessionId: string): Recovery | null {
  try {
    const raw = sessionStorage.getItem(
      identityCacheKey(ctx, `run:${sessionId}`));
    if (!raw) return null;
    const value = JSON.parse(raw) as Partial<Recovery>;
    if (typeof value.runId !== 'string' || !value.runId
        || typeof value.turnId !== 'string') return null;
    return {
      runId: value.runId,
      turnId: value.turnId,
      eventId: typeof value.eventId === 'number' ? value.eventId : 0,
    };
  } catch {
    return null;
  }
}

function writeRecovery(ctx: AuthContext, sessionId: string,
                       recovery: Recovery): void {
  try {
    sessionStorage.setItem(
      identityCacheKey(ctx, `run:${sessionId}`),
      JSON.stringify(recovery));
  } catch { /* quota */ }
}

function clearRecovery(ctx: AuthContext, sessionId: string): void {
  try {
    sessionStorage.removeItem(identityCacheKey(ctx, `run:${sessionId}`));
  } catch { /* ignore */ }
}

function clearRecoveryForRun(ctx: AuthContext, sessionId: string,
                             runId: string): void {
  try {
    const key = identityCacheKey(ctx, `run:${sessionId}`);
    const raw = sessionStorage.getItem(key);
    if (!raw) return;
    const value = JSON.parse(raw) as { runId?: unknown };
    if (value?.runId === runId) sessionStorage.removeItem(key);
  } catch { /* ignore */ }
}

function failureNotice(error: unknown, fallback: string): string {
  if (error instanceof AuthError) {
    if (error.status === 403) return `${fallback}（403 权限不足）`;
    if (error.status === 503) return `${fallback}（503 服务暂不可用）`;
    if (error.status === 409) return `${fallback}（409 请求冲突）`;
    if (error.status === 404) return `${fallback}（404 不存在或已失效）`;
    if (error.status === 0) return `${fallback}（网络不可用）`;
    return `${fallback}（${error.status}）`;
  }
  return fallback;
}

export function useWorkspace(options: UseWorkspaceOptions): UseWorkspaceResult {
  const { ctx, epoch, epochs, onExpired } = options;
  const client: ControlWorkspaceClient = useMemo(
    () => createControlWorkspaceClient({ ctx, epoch, epochs }),
    [ctx, epoch, epochs]);

  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [sessionNextOffset, setSessionNextOffset] =
    useState<number | null>(null);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [historyError, setHistoryError] = useState('');
  const [notice, setNotice] = useState('');
  const [messageStart, setMessageStart] = useState(0);
  const [messageTotal, setMessageTotal] = useState(0);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [run, setRun] = useState<ClientRun | null>(null);
  const [streamActive, setStreamActive] = useState(false);
  const [localSends, setLocalSends] = useState<LocalSend[]>([]);
  const [failedTurnId, setFailedTurnId] = useState<string | null>(null);

  const sessionsRef = useRef<ChatSession[]>([]);
  const activeIdRef = useRef<string | null>(null);
  const runRef = useRef<ClientRun | null>(null);
  const localSendsRef = useRef<LocalSend[]>([]);
  const streamRef = useRef<AbortController | null>(null);
  const streamOwner = useRef<string | null>(null);
  const contentChannel = useRef<BroadcastChannel | null>(null);
  const connectionSeq = useRef(0);
  const requestAbort = useRef(new AbortController());
  const sessionsSeq = useRef(0);
  const selectionSeq = useRef(0);
  const userSelectSeq = useRef(0);
  const sendSeq = useRef(0);
  const messageSeq = useRef(0);

  useEffect(() => { sessionsRef.current = sessions; }, [sessions]);
  useEffect(() => { activeIdRef.current = activeId; }, [activeId]);
  useEffect(() => { runRef.current = run; }, [run]);
  useEffect(() => { localSendsRef.current = localSends; }, [localSends]);

  const apply = useCallback((update: () => void) => {
    if (epochs.isCurrent(epoch)) update();
  }, [epochs, epoch]);

  const isSelected = useCallback((sessionId: string, seq?: number) =>
    epochs.isCurrent(epoch) && activeIdRef.current === sessionId
    && (seq === undefined || selectionSeq.current === seq), [epoch, epochs]);

  const handleAuthError = useCallback((error: unknown): boolean => {
    if (error instanceof AuthError && error.status === 401) {
      if (error.identityInvalidated) onExpired();
      return true;
    }
    return false;
  }, [onExpired]);

  const updateLocalSends = useCallback((
    update: (current: LocalSend[]) => LocalSend[],
  ) => {
    const next = update(localSendsRef.current);
    localSendsRef.current = next;
    setLocalSends(next);
  }, []);

  const call = useCallback(async <T,>(
    path: string, init?: RequestInit,
  ): Promise<T> => {
    try {
      return await client.call<T>(path, init ? {
        ...init,
        signal: init.signal || requestAbort.current.signal,
      } : { signal: requestAbort.current.signal });
    } catch (error) {
      if (error instanceof AuthError && error.status === 401
          && error.identityInvalidated) {
        onExpired();
      }
      throw error;
    }
  }, [client, onExpired]);

  const abortStream = useCallback(() => {
    connectionSeq.current += 1;
    streamRef.current?.abort();
    streamRef.current = null;
    streamOwner.current = null;
    setStreamActive(false);
  }, []);

  useEffect(() => () => {
    requestAbort.current.abort();
    abortStream();
  }, [abortStream]);

  const chooseSession = useCallback((sessionId: string) => {
    userSelectSeq.current += 1;
    activeIdRef.current = sessionId;
    setActiveId(sessionId);
    setNotice('');
  }, []);

  const sessionStatus = useCallback(async (
    sessionId: string,
  ): Promise<'alive' | 'gone' | 'unknown'> => {
    try {
      await call(`/api/sessions/${encodeURIComponent(sessionId)}/runs`
                 + '?limit=1&offset=0');
      return 'alive';
    } catch (error) {
      if (error instanceof AuthError && error.status === 404) return 'gone';
      return 'unknown';
    }
  }, [call]);

  const mergeSessionViews = useCallback((
    rows: ServerSession[],
  ): ChatSession[] => {
    const current = new Map(
      sessionsRef.current.map((item) => [item.id, item]));
    return rows.map((server) => {
      const prior = current.get(server.id);
      return {
        ...controlSessionView(server),
        messages: prior?.messages || [],
      };
    });
  }, []);

  const loadSessionsPage = useCallback(async (
    offset: number, append: boolean, select?: string | null,
  ) => {
    const seq = ++sessionsSeq.current;
    const wanted = select === undefined ? activeIdRef.current : select;
    const selectionAtStart = userSelectSeq.current;
    try {
      let page = await client.listSessions(
        offset, 100, requestAbort.current.signal);
      if (seq !== sessionsSeq.current) return;
      let visible = page.sessions;
      let nextOffset = page.nextOffset;
      let keep = null as null | typeof page.sessions[number];
      if (!append && wanted && !visible.some((item) => item.id === wanted)) {
        let searchOffset = nextOffset;
        let pages = 0;
        while (searchOffset !== null && pages < 10) {
          const nextPage = await client.listSessions(
            searchOffset, 100, requestAbort.current.signal);
          if (seq !== sessionsSeq.current) return;
          const seen = new Set(visible.map((item) => item.id));
          visible = [...visible,
            ...nextPage.sessions.filter((item) => !seen.has(item.id))];
          keep = nextPage.sessions.find((item) => item.id === wanted) || null;
          nextOffset = nextPage.nextOffset;
          searchOffset = nextOffset;
          pages += 1;
          if (keep) break;
        }
        if (!keep) {
          const previous = sessionsRef.current.find(
            (item) => item.id === wanted) || null;
          const status = await sessionStatus(wanted);
          if (seq !== sessionsSeq.current) return;
          if (status === 'alive' && previous) {
            keep = {
              id: previous.id, workspaceId: '', title: previous.title,
              archived: previous.archived === true,
            };
            visible = [...visible, keep];
          } else if (status === 'unknown' && previous) {
            keep = {
              id: previous.id, workspaceId: '', title: previous.title,
              archived: previous.archived === true,
            };
            visible = [...visible, keep];
          }
        }
      }
      const selectionChanged = userSelectSeq.current !== selectionAtStart;
      if (!append && selectionChanged) {
        const currentId = activeIdRef.current;
        if (currentId && !visible.some((item) => item.id === currentId)) {
          const current = sessionsRef.current.find(
            (item) => item.id === currentId);
          if (current) {
            visible = [...visible, {
              id: current.id, workspaceId: '', title: current.title,
              archived: current.archived === true,
            }];
          }
        }
      }
      const views = mergeSessionViews(visible);
      apply(() => {
        setSessions((current) => {
          if (!append) return views.map((view) => {
            const prior = current.find((item) => item.id === view.id);
            return prior ? { ...view, messages: prior.messages } : view;
          });
          const seen = new Set(current.map((item) => item.id));
          return [...current, ...views.filter((item) => !seen.has(item.id))];
        });
        setSessionNextOffset(nextOffset);
        setLoading(false);
        if (!append && !selectionChanged) {
          const next = (keep && keep.id)
            || (visible.find((item) => item.id === wanted) ? wanted : null)
            || (visible.find((item) => !item.archived) || visible[0])?.id
            || null;
          setActiveId(next);
        }
      });
    } catch (error) {
      if (seq !== sessionsSeq.current) return;
      if (handleAuthError(error)) return;
      apply(() => {
        setLoading(false);
        setHistoryError(failureNotice(error, '目录读取失败'));
      });
    }
  }, [apply, client, handleAuthError, mergeSessionViews, sessionStatus]);

  const reloadSessions = useCallback(async (select?: string | null) => {
    await loadSessionsPage(0, false, select);
  }, [loadSessionsPage]);

  useEffect(() => {
    const stored = localStorage.getItem(
      identityCacheKey(ctx, 'active_session'));
    void loadSessionsPage(0, false, stored);
  }, [ctx, loadSessionsPage]);

  useEffect(() => {
    if (!activeId) return;
    const key = identityCacheKey(ctx, 'active_session');
    if (epochs.isCurrent(epoch)) {
      try { localStorage.setItem(key, activeId); } catch { /* quota */ }
    }
  }, [activeId, ctx, epoch, epochs]);

  useEffect(() => {
    let channel: BroadcastChannel | null = null;
    try {
      channel = new BroadcastChannel(
        `easel-control:${ctx.userId}:${ctx.workspaceId}`);
      contentChannel.current = channel;
      channel.addEventListener('message', () => {
        void reloadSessions(activeIdRef.current);
      });
    } catch { channel = null; }
    return () => {
      channel?.close();
      contentChannel.current = null;
    };
  }, [ctx.userId, ctx.workspaceId, reloadSessions]);

  const toMessages = (rows: Parameters<typeof controlMessageView>[0][])
    : ChatMessage[] => rows.map(controlMessageView);

  const fetchMessagesPage = useCallback(async (
    sessionId: string, offset: number, limit = 500,
  ) => client.loadMessages(sessionId, offset, limit,
      requestAbort.current.signal), [client]);

  const fetchLatestMessages = useCallback(async (sessionId: string) => {
    const first = await fetchMessagesPage(sessionId, 0);
    if (first.total <= 500) {
      return {
        messages: toMessages(first.messages), start: 0, total: first.total,
      };
    }
    const start = first.total - 500;
    const latest = await fetchMessagesPage(sessionId, start);
    return {
      messages: toMessages(latest.messages), start, total: latest.total,
    };
  }, [fetchMessagesPage]);

  const refreshLatestMessages = useCallback(async (
    sessionId: string, seq?: number,
  ) => {
    messageSeq.current += 1;
    const generation = messageSeq.current;
    const sendGeneration = sendSeq.current;
    const latest = await fetchLatestMessages(sessionId);
    if (!isSelected(sessionId, seq)
        || generation !== messageSeq.current
        || sendSeq.current !== sendGeneration) return null;
    apply(() => {
      setSessions((current) => current.map((item) =>
        item.id === sessionId
          ? { ...item, messages: latest.messages } : item));
      setMessageStart(latest.start);
      setMessageTotal(latest.total);
    });
    return latest;
  }, [apply, fetchLatestMessages, isSelected]);

  const pruneLocalSends = useCallback((
    sessionId: string, knownRunIds: Set<string>,
    activeRunIds: Set<string>,
  ) => {
    if (knownRunIds.size === 0) return;
    updateLocalSends((current) => {
      const next = current.filter((item) => !(
        item.sessionId === sessionId && item.runId
        && knownRunIds.has(item.runId)
        && !activeRunIds.has(item.runId)));
      return next.length === current.length ? current : next;
    });
  }, [updateLocalSends]);

  const finishRun = useCallback(async (
    sessionId: string, runId: string,
    owns: () => boolean,
  ) => {
    const allowed = () => epochs.isCurrent(epoch)
      && activeIdRef.current === sessionId
      && (owns() || localSendsRef.current.some(
        (item) => item.sessionId === sessionId));
    if (!allowed()) return;
    clearRecoveryForRun(ctx, sessionId, runId);
    const generation = ++messageSeq.current;
    let latest;
    try {
      latest = await fetchLatestMessages(sessionId);
    } catch (error) {
      if (!allowed()) return;
      if (handleAuthError(error)) return;
      const current = runRef.current;
      if (current && current.runId === runId) {
        apply(() => setSessions((items) => items.map((item) => {
          if (item.id !== sessionId) return item;
          return {
            ...item,
            messages: [...item.messages, {
              role: 'assistant',
              content: current.content
                || '（最终历史读取失败，已完成内容已保留）',
              thinking: current.thinking || undefined,
              activity: current.activity || undefined,
            }],
          };
        })));
        setRun(null);
        apply(() => setNotice('最终历史读取失败；已完成回答已保留。'));
      }
      return;
    }
    if (!allowed() || generation !== messageSeq.current) return;
    apply(() => {
      setSessions((current) => current.map((item) =>
        item.id === sessionId
          ? { ...item, messages: latest.messages } : item));
      setMessageStart(latest.start);
      setMessageTotal(latest.total);
      setRun((current) => current && current.runId === runId
        ? null : current);
    });
    updateLocalSends((current) => current.filter(
      (item) => item.runId !== runId));
  }, [apply, ctx, epoch, epochs, fetchLatestMessages, handleAuthError,
      updateLocalSends]);

  const connectRun = useCallback(async (
    runId: string, sessionId: string, after: number, turnId: string,
    previous: ClientRun | null = null, seq: number | null = null,
  ) => {
    if (!isSelected(sessionId, seq === null ? undefined : seq)) return;
    abortStream();
    const connection = ++connectionSeq.current;
    const controller = new AbortController();
    streamRef.current = controller;
    streamOwner.current = runId;
    const initial: ClientRun = previous && previous.runId === runId
      ? { ...previous, cursor: Math.max(0, after) }
      : {
        runId, turnId, sessionId, state: 'accepted', content: '',
        thinking: '', activity: '', question: null, answered: [],
        cursor: Math.max(0, after),
      };
    runRef.current = initial;
    setStreamActive(true);
    apply(() => setRun(initial));
    const sendGeneration = sendSeq.current;
    const owns = () => connectionSeq.current === connection
      && !controller.signal.aborted
      && epochs.isCurrent(epoch)
      && activeIdRef.current === sessionId
      && runRef.current?.runId === runId
      && sendSeq.current === sendGeneration;
    const persist = (cursor: number) => {
      if (cursor > 0 && epochs.isCurrent(epoch)) {
        writeRecovery(ctx, sessionId, { runId, turnId, eventId: cursor });
      }
    };
    try {
      const result = await client.streamRun(runId, after, {
        onState: (state, frame) => {
          if (!owns()) return;
          persist(frame.id);
          apply(() => setRun((current) => current
            && current.runId === runId
            ? { ...current, state: state.state, cursor: frame.id }
            : current));
          if (isTerminalRun(state.state)) {
            void finishRun(sessionId, runId, owns);
          }
        },
        onToken: (text, frame) => {
          if (!owns()) return;
          apply(() => setRun((current) => current
            && current.runId === runId
            ? { ...current, content: current.content + text,
              cursor: frame.id } : current));
        },
        onThinking: (text) => {
          if (!owns()) return;
          apply(() => setRun((current) => current
            && current.runId === runId
            ? { ...current, thinking: current.thinking + text } : current));
        },
        onActivity: (text) => {
          if (!owns()) return;
          apply(() => setRun((current) => current
            && current.runId === runId
            ? { ...current, activity: text } : current));
        },
        onErrorText: (text) => {
          if (owns()) apply(() => setNotice(text));
        },
        onQuestion: (question: ControlQuestion) => {
          if (!owns()) return;
          void client.questionStatus(
            runId, [question.id], controller.signal).then((status) => {
            if (!owns()) return;
            const state = status[question.id]?.status;
            if (state && state !== 'pending') {
              apply(() => setRun((current) => current
                && current.runId === runId
                ? { ...current,
                  answered: [...current.answered, question.id] } : current));
              return;
            }
            apply(() => setRun((current) => {
              if (!current || current.runId !== runId
                  || current.answered.includes(question.id)) return current;
              return { ...current,
                question: controlQuestionView(question) };
            }));
          }).catch(() => {
            if (!owns()) return;
            apply(() => setRun((current) => {
              if (!current || current.runId !== runId) return current;
              return { ...current,
                question: controlQuestionView(question) };
            }));
          });
        },
      }, controller.signal);
      if (result.aborted || controller.signal.aborted) return;
      if (!result.terminal && owns()) {
        apply(() => setNotice(
          '连接已断开。后台任务可能仍在执行；重连只回放，不会再次发送。'));
      }
    } catch (error) {
      if (controller.signal.aborted) return;
      if (error instanceof AuthError && error.status === 401) {
        if (error.identityInvalidated) onExpired();
        return;
      }
      if (!owns()) return;
      if (error instanceof AuthError && error.status === 404) {
        apply(() => {
          setNotice('任务记录不存在或已失效；请从服务端历史确认结果。');
          setRun(null);
        });
        clearRecovery(ctx, sessionId);
        await refreshLatestMessages(sessionId);
        return;
      }
      apply(() => setNotice(failureNotice(error, '事件流不可用')));
    } finally {
      if (streamRef.current === controller) {
        streamRef.current = null;
        if (streamOwner.current === runId) streamOwner.current = null;
      }
      if (connectionSeq.current === connection) setStreamActive(false);
    }
  }, [abortStream, apply, client, ctx, epoch, epochs, finishRun,
      isSelected, onExpired, refreshLatestMessages]);

  useEffect(() => {
    if (!activeId) {
      setRun(null);
      return;
    }
    const sessionId = activeId;
    const seq = ++selectionSeq.current;
    abortStream();
    setRun(null);
    setNotice('');
    setHistoryError('');
    setMessageStart(0);
    setMessageTotal(0);
    setLoadingOlder(false);
    const generation = ++messageSeq.current;
    let cancelled = false;
    const sendAtStart = sendSeq.current;
    const knownRunIds = new Set(localSendsRef.current
      .filter((item) => item.sessionId === sessionId && item.runId)
      .map((item) => item.runId as string));
    (async () => {
      try {
        const runs = await client.loadActiveRuns(
          sessionId, requestAbort.current.signal);
        const latest = await fetchLatestMessages(sessionId);
        if (cancelled || !isSelected(sessionId, seq)
            || generation !== messageSeq.current) return;
        apply(() => {
          setSessions((current) => current.map((item) =>
            item.id === sessionId
              ? { ...item, messages: latest.messages } : item));
          setMessageStart(latest.start);
          setMessageTotal(latest.total);
        });
        pruneLocalSends(sessionId, knownRunIds,
          new Set(runs.map((item) => item.runId)));
        if (sendSeq.current !== sendAtStart) return;
        const recovery = readRecovery(ctx, sessionId);
        const plan = recoveryAction(runs, recovery?.runId || null);
        if (plan.action === 'stream' && plan.runId) {
          const turnId = recovery?.runId === plan.runId
            ? recovery.turnId : '';
          await connectRun(plan.runId, sessionId, 0, turnId, null, seq);
        } else if (recovery) {
          clearRecovery(ctx, sessionId);
        }
      } catch (error) {
        if (cancelled || !isSelected(sessionId, seq)
            || generation !== messageSeq.current) return;
        if (handleAuthError(error)) return;
        apply(() => setHistoryError(
          failureNotice(error, '历史读取失败')));
      }
    })();
    return () => { cancelled = true; };
  }, [abortStream, activeId, apply, client, connectRun, ctx,
      fetchLatestMessages, handleAuthError, isSelected, pruneLocalSends]);

  const loadMoreSessions = useCallback(async () => {
    if (sessionNextOffset === null) return;
    await loadSessionsPage(sessionNextOffset, true);
  }, [loadSessionsPage, sessionNextOffset]);

  const loadOlder = useCallback(async () => {
    const sessionId = activeIdRef.current;
    if (!sessionId || messageStart <= 0 || loadingOlder) return;
    setLoadingOlder(true);
    const generation = messageSeq.current;
    try {
      const window = olderMessageWindow(messageStart);
      const payload = await fetchMessagesPage(
        sessionId, window.offset, window.limit);
      if (!isSelected(sessionId) || generation !== messageSeq.current) return;
      apply(() => {
        setSessions((current) => current.map((item) =>
          item.id === sessionId ? {
            ...item,
            messages: [...toMessages(payload.messages), ...item.messages],
          } : item));
        setMessageStart(window.offset);
      });
    } catch (error) {
      if (handleAuthError(error)) return;
      if (isSelected(sessionId) && generation === messageSeq.current) {
        apply(() => setNotice('更早历史加载失败，请重试。'));
      }
    } finally {
      setLoadingOlder(false);
    }
  }, [apply, fetchMessagesPage, handleAuthError, isSelected, loadingOlder,
      messageStart]);

  const beginLocalSend = useCallback((send: LocalSend, text: string) => {
    updateLocalSends((current) => {
      const index = current.findIndex((item) =>
        item.sessionId === send.sessionId && item.turnId === send.turnId);
      if (index < 0) return [...current, send];
      return current.map((item, itemIndex) =>
        itemIndex === index ? send : item);
    });
    apply(() => setSessions((current) => current.map((item) => {
      if (item.id !== send.sessionId) return item;
      const withoutFailed = item.messages.filter((message) =>
        !(message.turnId === send.turnId && message.failed));
      return {
        ...item,
        messages: [...withoutFailed, {
          role: 'user', content: text, pending: true,
          turnId: send.turnId,
        }],
      };
    })));
  }, [apply, updateLocalSends]);

  const markLocalFailed = useCallback((
    send: LocalSend, message: string,
  ) => {
    updateLocalSends((current) => current.map((item) =>
      item.sessionId === send.sessionId && item.turnId === send.turnId
        ? { ...item, status: 'failed' } : item));
    apply(() => setSessions((current) => current.map((item) => {
      if (item.id !== send.sessionId) return item;
      return {
        ...item,
        messages: item.messages.map((entry) =>
          entry.turnId === send.turnId
            ? { ...entry, pending: false, failed: true } : entry),
      };
    })));
    apply(() => setNotice(message));
    setFailedTurnId(send.turnId);
  }, [apply, updateLocalSends]);

  const submit = useCallback(async (send: LocalSend, text: string) => {
    if (!epochs.isCurrent(epoch)) return;
    sendSeq.current += 1;
    const generation = sendSeq.current;
    const current: LocalSend = { ...send, generation, status: 'sending' };
    setNotice('');
    setFailedTurnId(null);
    beginLocalSend(current, text);
    try {
      const admission = await client.submitTurn(
        send.sessionId, send.turnId, text, requestAbort.current.signal);
      if (!epochs.isCurrent(epoch)) return;
      const recovery = {
        runId: admission.runId, turnId: send.turnId, eventId: 0,
      };
      updateLocalSends((items) => items.map((item) =>
        item.sessionId === send.sessionId && item.turnId === send.turnId
          ? { ...item, runId: admission.runId, status: 'sending' } : item));
      if (activeIdRef.current === send.sessionId) {
        writeRecovery(ctx, send.sessionId, recovery);
        await connectRun(admission.runId, send.sessionId, 0, send.turnId,
          null, selectionSeq.current);
      } else {
        writeRecovery(ctx, send.sessionId, recovery);
      }
    } catch (error) {
      if (!epochs.isCurrent(epoch)) return;
      if (sendSeq.current !== generation) return;
      if (error instanceof AuthError && error.status === 401
          && error.identityInvalidated) {
        onExpired();
        return;
      }
      markLocalFailed(current, failureNotice(error, '发送失败'));
    }
  }, [beginLocalSend, client, connectRun, ctx, epoch, epochs,
      markLocalFailed, onExpired, updateLocalSends]);

  const activeSession = sessions.find((item) => item.id === activeId)
    || null;

  const send = useCallback(async (text: string) => {
    const value = text.trim();
    if (!value || !activeId) return;
    if (activeSession?.archived) {
      setNotice('该会话已归档，历史只读；新任务需先取消归档。');
      return;
    }
    if (runRef.current && isActiveRun(runRef.current.state)) {
      setNotice('当前任务仍在执行，请先等待或停止。');
      return;
    }
    await submit({
      sessionId: activeId, turnId: makeTurnId(), text: value,
      generation: 0, status: 'sending',
    }, value);
  }, [activeId, activeSession, submit]);

  const retryFailed = useCallback(async () => {
    const send = localSendsRef.current.find((item) =>
      item.sessionId === activeIdRef.current && item.status === 'failed');
    if (!send) return;
    if (runRef.current && isActiveRun(runRef.current.state)) {
      setNotice('当前任务仍在执行，请先等待或停止。');
      return;
    }
    await submit(send, send.text);
  }, [submit]);

  const stop = useCallback(async () => {
    const current = runRef.current;
    if (!current || !isActiveRun(current.state)) return;
    if (!epochs.isCurrent(epoch)
        || activeIdRef.current !== current.sessionId) return;
    try {
      await client.stopRun(current.runId, requestAbort.current.signal);
    } catch (error) {
      if (handleAuthError(error)) return;
      if (!isSelected(current.sessionId)) return;
      apply(() => setNotice(failureNotice(error, '停止失败')));
    }
  }, [apply, client, epoch, epochs, handleAuthError, isSelected]);

  const reconnect = useCallback(() => {
    const current = runRef.current;
    if (!current || isTerminalRun(current.state)) return;
    void connectRun(current.runId, current.sessionId, current.cursor,
      current.turnId, current, selectionSeq.current);
  }, [connectRun]);

  const answer = useCallback(async (
    question: ChatQuestion, answers: Record<string, string[]>,
  ) => {
    const current = runRef.current;
    if (!current || !epochs.isCurrent(epoch)
        || activeIdRef.current !== current.sessionId) return;
    try {
      await client.answerQuestion(current.runId, question.id, answers,
        requestAbort.current.signal);
      if (!isSelected(current.sessionId)
          || runRef.current?.runId !== current.runId) return;
      apply(() => setRun((value) => value && value.runId === current.runId
        ? { ...value, question: null,
          answered: [...value.answered, question.id] } : value));
    } catch (error) {
      if (handleAuthError(error)) return;
      if (!isSelected(current.sessionId)
          || runRef.current?.runId !== current.runId) return;
      apply(() => {
        setNotice(error instanceof AuthError && error.status === 409
          ? '该问题已过期或已回答；请等待服务端最终结果。'
          : failureNotice(error, '回答提交失败'));
        setRun((value) => value && value.runId === current.runId
          ? { ...value, question: null } : value);
      });
    }
  }, [apply, client, epoch, epochs, handleAuthError, isSelected]);

  const newChat = useCallback(async () => {
    try {
      const created = await client.createSession('新对话',
        requestAbort.current.signal);
      setNotice('');
      try {
        contentChannel.current?.postMessage({ type: 'sessions-changed' });
      } catch { /* channel unavailable */ }
      await reloadSessions(created.id);
    } catch (error) {
      if (handleAuthError(error)) return;
      apply(() => setNotice(failureNotice(error, '新建会话失败')));
    }
  }, [apply, client, handleAuthError, reloadSessions]);

  const renameSession = useCallback(async (id: string, title: string) => {
    const value = title.trim();
    if (!value) return;
    try {
      await client.updateSession(id, { title: value },
        requestAbort.current.signal);
      await reloadSessions(activeIdRef.current);
    } catch (error) {
      if (handleAuthError(error)) return;
      apply(() => setNotice(failureNotice(error, '重命名失败')));
    }
  }, [apply, client, handleAuthError, reloadSessions]);

  const archiveSession = useCallback(async (
    id: string, archived: boolean,
  ) => {
    try {
      await client.updateSession(id, { archived },
        requestAbort.current.signal);
      await reloadSessions(archived && activeIdRef.current === id
        ? null : activeIdRef.current);
    } catch (error) {
      if (handleAuthError(error)) return;
      apply(() => setNotice(failureNotice(error, '归档失败')));
    }
  }, [apply, client, handleAuthError, reloadSessions]);

  const deleteSession = useCallback(async (id: string) => {
    try {
      await client.deleteSession(id, requestAbort.current.signal);
      clearRecovery(ctx, id);
      await reloadSessions(activeIdRef.current === id ? null
        : activeIdRef.current);
    } catch (error) {
      if (handleAuthError(error)) return;
      apply(() => setNotice(failureNotice(error, '删除失败')));
    }
  }, [apply, client, ctx, handleAuthError, reloadSessions]);

  const activeRun = run && run.sessionId === activeId ? run : null;
  const stream: StreamState | undefined = activeRun ? {
    content: activeRun.content,
    thinking: activeRun.thinking,
    activity: activeRun.activity,
    questions: activeRun.question ? [activeRun.question] : [],
  } : undefined;
  const canReconnect = !!activeRun && !isTerminalRun(activeRun.state)
    && !streamActive;

  return {
    sessions,
    activeId,
    activeSession,
    sessionNextOffset,
    loading,
    historyError,
    notice,
    messageStart,
    messageTotal,
    hasOlder: messageStart > 0,
    loadingOlder,
    stream,
    streamActive,
    canReconnect,
    failedTurnId,
    call,
    selectSession: chooseSession,
    newChat,
    renameSession,
    archiveSession,
    deleteSession,
    loadMoreSessions,
    loadOlder,
    send,
    retryFailed,
    stop,
    reconnect,
    answer,
  };
}
