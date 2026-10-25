import { useCallback, useEffect, useRef, useState } from 'react';
import type { Dispatch, ReactNode, SetStateAction } from 'react';
import LoginPage from './LoginPage';
import AdminSettingsPage from './AdminSettingsPage';
import ControlSettingsPage from './ControlSettingsPage';
import {
  AuthError, IdentityEpoch, commitIfCurrent, identityCacheKey,
  identityFetch, loadIdentity, logout,
} from '../lib/auth';
import type { AuthContext } from '../lib/auth';
import {
  consumeSseChunk, isActiveRun, isTerminalRun, optionSubmitValue,
  olderMessageWindow,
  parseAdmission, parseMessagePage, parseQuestionData, parseRunPage,
  parseSessionPage, questionAnswerKey, recoveryAction, stateFromEvent,
  textFromEvent,
} from '../lib/control';
import type {
  ControlQuestion, RunMeta, ServerMessage, ServerSession, SseFrame,
} from '../lib/control';

interface UiMessage {
  role: 'user' | 'assistant';
  content: string;
}

interface ClientRun {
  runId: string;
  turnId: string;
  sessionId: string;
  state: string;
  content: string;
  thinking: string;
  activity: string;
  question: ControlQuestion | null;
  answered: string[];
  cursor: number;
}

interface Recovery {
  runId: string;
  turnId: string;
  eventId: number;
}

/**
 * 本地待确认发送：不写入服务端消息列表，单独渲染；终态快照确认后才移除。
 * 失败或结果未知时保留文本与 turnId，重试仍使用同一请求键。
 */
interface LocalSend {
  sessionId: string;
  turnId: string;
  text: string;
  generation: number;
  status: 'sending' | 'failed';
  runId?: string;
}

const CONTROL_CHANNEL = 'easel-identity';

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

/** 只清理属于该 Run 的恢复记录，不删除同会话新 Run 的恢复。 */
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

function clearIdentityStorage(ctx: AuthContext): void {
  const prefix = identityCacheKey(ctx, '');
  for (const storage of [localStorage, sessionStorage]) {
    const keys: string[] = [];
    try {
      for (let index = 0; index < storage.length; index += 1) {
        const key = storage.key(index);
        if (key && key.startsWith(prefix)) keys.push(key);
      }
    } catch { /* storage disabled */ }
    for (const key of keys) {
      try { storage.removeItem(key); } catch { /* ignore */ }
    }
  }
}

function makeTurnId(): string {
  try {
    if (typeof crypto !== 'undefined' && crypto.randomUUID) {
      return crypto.randomUUID();
    }
  } catch { /* fall through */ }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

const styles = {
  page: {
    minHeight: '100vh', display: 'grid',
    gridTemplateColumns: '260px minmax(0, 1fr)', background: '#0f1115',
    color: '#e7e9ee',
  } as const,
  aside: {
    borderRight: '1px solid #242a35', padding: '20px 16px',
    display: 'flex', flexDirection: 'column', gap: '12px',
    background: '#12151b',
  } as const,
  main: {
    display: 'flex', flexDirection: 'column', minHeight: '100vh',
    minWidth: 0,
  } as const,
  button: {
    border: '1px solid #333a47', background: '#1b1f28', color: '#e7e9ee',
    borderRadius: '9px', padding: '8px 10px', cursor: 'pointer',
    fontSize: '13px',
  } as const,
  accent: {
    border: 'none', background: '#8b5cf6', color: 'white',
    borderRadius: '9px', padding: '9px 12px', cursor: 'pointer',
    fontSize: '13px', fontWeight: 600,
  } as const,
};

export default function ControlApp() {
  const epochs = useRef(new IdentityEpoch());
  const [status, setStatus] = useState<'checking' | 'anonymous'
    | 'ready' | 'error'>('checking');
  const [identity, setIdentity] = useState<AuthContext | null>(null);
  const [epoch, setEpoch] = useState(0);
  const [fatal, setFatal] = useState('');
  const [notice, setNotice] = useState('');
  const channelRef = useRef<BroadcastChannel | null>(null);
  const identityRef = useRef<AuthContext | null>(null);
  // 身份确认自身的代次：新的登录/退出/广播/卸载使旧查询无权提交
  const checkSeq = useRef(0);

  useEffect(() => { identityRef.current = identity; }, [identity]);

  const adopt = useCallback((ctx: AuthContext) => {
    checkSeq.current += 1;
    const next = epochs.current.begin(ctx);
    setIdentity(ctx);
    setEpoch(next);
    setStatus('ready');
    setFatal('');
  }, []);

  const expire = useCallback((message = '') => {
    const prior = identityRef.current;
    checkSeq.current += 1;
    epochs.current.invalidate();
    if (prior) clearIdentityStorage(prior);
    setIdentity(null);
    setEpoch(0);
    setStatus('anonymous');
    setNotice(message);
  }, []);

  const checkIdentity = useCallback(async () => {
    checkSeq.current += 1;
    const seq = checkSeq.current;
    try {
      const ctx = await loadIdentity();
      if (seq !== checkSeq.current) return;
      adopt(ctx);
    } catch (error) {
      if (seq !== checkSeq.current) return;
      if (error instanceof AuthError && error.status === 401) {
        expire();
        return;
      }
      setStatus('error');
      setFatal(error instanceof AuthError && error.status === 0
        ? '网络不可用，无法确认登录状态。'
        : '服务暂不可用，无法确认登录状态。');
    }
  }, [adopt, expire]);

  useEffect(() => { void checkIdentity(); }, [checkIdentity]);

  useEffect(() => {
    let channel: BroadcastChannel | null = null;
    try {
      channel = new BroadcastChannel(CONTROL_CHANNEL);
      channelRef.current = channel;
      channel.addEventListener('message', (event) => {
        // 全局登录事件只携带类型（login/logout），不携带 token/csrf/内容。
        // 先卸载旧身份工作区，避免其他标签退出后旧内容继续可见。
        const old = identityRef.current;
        checkSeq.current += 1;
        epochs.current.invalidate();
        setIdentity(null);
        if (old) clearIdentityStorage(old);
        const kind = (event.data as { type?: string } | null)?.type;
        if (kind === 'logout') {
          // 不等待远端 Cookie 撤销完成；其他标签也必须立即回到登录页
          setStatus('anonymous');
          setNotice('');
          return;
        }
        setStatus('checking');
        void checkIdentity();
      });
    } catch { channelRef.current = null; }
    return () => {
      channel?.close();
      channelRef.current = null;
    };
  }, [checkIdentity]);

  const handleExpired = useCallback(() => {
    expire('登录已失效，请重新登录。');
  }, [expire]);

  const handleLogout = useCallback(() => {
    const current = identity;
    if (!current) return;
    checkSeq.current += 1;
    epochs.current.invalidate();
    setIdentity(null);
    setEpoch(0);
    setStatus('anonymous');
    clearIdentityStorage(current);
    try { channelRef.current?.postMessage({ type: 'logout' }); }
    catch { /* channel closed */ }
    // 远端撤销使用失效前捕获的 CSRF，独立于已取消的业务请求
    void logout(current).catch(() => {
      setNotice('已退出本机显示并清理缓存，但服务端撤销失败；Cookie 可能仍然有效。');
    });
  }, [identity]);

  if (status === 'checking') {
    return <Centered text="正在确认登录状态…" />;
  }
  if (status === 'error') {
    return (
      <Centered text={fatal || '服务暂不可用'}>
        <button style={styles.accent} onClick={() => void checkIdentity()}>
          重试
        </button>
      </Centered>
    );
  }
  if (status === 'anonymous' || !identity) {
    return (
      <LoginPage notice={notice} onAuthenticated={(ctx) => {
        adopt(ctx);
        try { channelRef.current?.postMessage({ type: 'login' }); }
        catch { /* ignore */ }
      }} />
    );
  }
  return (
    <ControlWorkspace
      key={epoch}
      ctx={identity}
      epoch={epoch}
      epochs={epochs.current}
      onExpired={handleExpired}
      onLogout={handleLogout}
    />
  );
}

function Centered({ text, children }: {
  text: string; children?: ReactNode;
}) {
  return (
    <main style={{
      minHeight: '100vh', display: 'grid', placeItems: 'center',
      background: '#0f1115', color: '#c8ced9', padding: '24px',
      fontFamily: 'system-ui, sans-serif',
    }}>
      <div style={{ textAlign: 'center', display: 'grid', gap: '14px' }}>
        <div>{text}</div>
        {children}
      </div>
    </main>
  );
}

interface WorkspaceProps {
  ctx: AuthContext;
  epoch: number;
  epochs: IdentityEpoch;
  onExpired: () => void;
  onLogout: () => void;
}

function ControlWorkspace({
  ctx, epoch, epochs, onExpired, onLogout,
}: WorkspaceProps) {
  const [sessions, setSessions] = useState<ServerSession[]>([]);
  const [sessionNextOffset, setSessionNextOffset] = useState<number | null>(null);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [messages, setMessages] = useState<UiMessage[]>([]);
  const [messageStart, setMessageStart] = useState(0);
  const [messageTotal, setMessageTotal] = useState(0);
  const [messageLoadingMore, setMessageLoadingMore] = useState(false);
  const [run, setRun] = useState<ClientRun | null>(null);
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(true);
  const [historyError, setHistoryError] = useState('');
  const [notice, setNotice] = useState('');
  const [localSends, setLocalSends] = useState<LocalSend[]>([]);
  const [answers, setAnswers] = useState<Record<string, string>>({});
  // 页面视图只切换展示；聊天组件保持挂载，SSE/待确认输入不因导航重置。
  const [view, setView] = useState<'chat' | 'settings' | 'admin'>('chat');
  const streamRef = useRef<AbortController | null>(null);
  const streamOwner = useRef<string | null>(null);
  const connectionSeq = useRef(0);
  const [streamActive, setStreamActive] = useState(false);
  // 身份卸载时同时中止普通 JSON 请求；epoch 校验仍是最终提交闸门。
  const requestAbort = useRef(new AbortController());
  const runRef = useRef<ClientRun | null>(null);
  const contentChannel = useRef<BroadcastChannel | null>(null);
  const activeIdRef = useRef<string | null>(null);
  const sessionsRef = useRef<ServerSession[]>([]);
  const sessionsSeq = useRef(0);
  // 本地待确认发送：发送代次/protocol 与 state 同步，供异步收尾读取
  const localSendsRef = useRef<LocalSend[]>([]);
  // 会话选择代次：异步历史/admission 只能提交到发起时的选择
  const selectionSeq = useRef(0);
  // 用户主动选择代次：目录刷新等待期间的新选择不被旧请求撤销
  const userSelectSeq = useRef(0);
  // 本地发送代次：从提交新一轮输入起保护输入，旧收尾不得覆盖
  const sendSeq = useRef(0);
  // 消息窗口代次：终态刷新后，旧的“加载更早”结果不得造成空洞/混排
  const messageSeq = useRef(0);

  useEffect(() => { runRef.current = run; }, [run]);
  useEffect(() => { activeIdRef.current = activeId; }, [activeId]);
  useEffect(() => { sessionsRef.current = sessions; }, [sessions]);

  const apply = useCallback((update: () => void) => {
    if (epochs.isCurrent(epoch)) update();
  }, [epochs, epoch]);

  const isSelected = useCallback((sessionId: string, seq?: number) =>
    epochs.isCurrent(epoch) && activeIdRef.current === sessionId
    && (seq === undefined || selectionSeq.current === seq), [epoch, epochs]);

  // 用户主动选择：同步更新代次与 ref，等待中的目录刷新不得回切
  const chooseSession = useCallback((sessionId: string) => {
    userSelectSeq.current += 1;
    activeIdRef.current = sessionId;
    setActiveId(sessionId);
  }, []);

  const updateLocalSends = useCallback((
    update: (current: LocalSend[]) => LocalSend[],
  ) => {
    const next = update(localSendsRef.current);
    localSendsRef.current = next;
    setLocalSends(next);
  }, []);

  const beginLocalSend = useCallback((send: LocalSend) => {
    updateLocalSends((current) => {
      const index = current.findIndex((item) =>
        item.sessionId === send.sessionId && item.turnId === send.turnId);
      if (index < 0) return [...current, send];
      return current.map((item, itemIndex) =>
        itemIndex === index ? send : item);
    });
  }, [updateLocalSends]);

  const markLocalAdmitted = useCallback((
    generation: number, turnId: string, runId: string,
  ) => {
    updateLocalSends((current) => current.map((item) =>
      item.generation === generation && item.turnId === turnId
        ? { ...item, status: 'sending', runId } : item));
  }, [updateLocalSends]);

  const markLocalFailed = useCallback((
    generation: number, turnId: string,
  ) => {
    updateLocalSends((current) => current.map((item) =>
      item.generation === generation && item.turnId === turnId
        ? { ...item, status: 'failed' } : item));
  }, [updateLocalSends]);

  const removeLocalSendRun = useCallback((runId: string) => {
    updateLocalSends((current) => {
      const next = current.filter((item) => item.runId !== runId);
      return next.length === current.length ? current : next;
    });
  }, [updateLocalSends]);

  /** 初始快照已确认：加载开始前已接纳且不再活跃的 Run 已写入历史。 */
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

  const call = useCallback(async <T,>(path: string, init?: RequestInit) => {
    try {
      return await identityFetch<T>({
        epochs, epoch, ctx, url: path,
        init: init ? {
          ...init, signal: init.signal || requestAbort.current.signal,
        } : { signal: requestAbort.current.signal },
      });
    } catch (error) {
      if (error instanceof AuthError && error.status === 401
          && error.identityInvalidated) {
        // identityFetch 已判定该 401 属于当前身份并失效；此处必须卸载私人树
        onExpired();
      }
      throw error;
    }
  }, [ctx, epoch, epochs, onExpired]);

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

  const loadSessionsPage = useCallback(async (
    offset: number, append: boolean, select?: string | null,
  ) => {
    const seq = ++sessionsSeq.current;
    // 目录请求与用户选择分别取代次：等待期间用户另选时只更新目录
    const wanted = select === undefined ? activeIdRef.current : select;
    const selectionAtStart = userSelectSeq.current;
    try {
      let page = parseSessionPage(await call(
        `/api/sessions?limit=100&offset=${offset}&include_archived=true`));
      if (seq !== sessionsSeq.current) return;
      let visible = page.sessions;
      let nextOffset = page.nextOffset;
      let keep: ServerSession | null = null;
      if (!append && wanted && !visible.some((item) => item.id === wanted)) {
        // 不能因为第一页没有就推断删除；在有界分页内继续查找本人目录
        let searchOffset = nextOffset;
        let pages = 0;
        while (searchOffset !== null && pages < 10) {
          const nextPage = parseSessionPage(await call(
            `/api/sessions?limit=100&offset=${searchOffset}`
            + '&include_archived=true'));
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
            keep = previous;
            visible = [...visible, previous];
          } else if (status === 'unknown' && previous) {
            // 服务故障不能当作删除；保留当前选择，避免无端切走
            keep = previous;
            visible = [...visible, previous];
          }
        }
      }
      const selectionChanged = userSelectSeq.current !== selectionAtStart;
      if (!append && selectionChanged) {
        // 用户在等待期间主动选择了其它会话：保留其条目，但不改活动键/连接
        const currentId = activeIdRef.current;
        if (currentId && !visible.some((item) => item.id === currentId)) {
          const current = sessionsRef.current.find(
            (item) => item.id === currentId);
          if (current) visible = [...visible, current];
        }
      }
      apply(() => {
        setSessions((current) => {
          if (!append) return visible;
          const seen = new Set(current.map((item) => item.id));
          return [...current,
            ...page.sessions.filter((item) => !seen.has(item.id))];
        });
        setSessionNextOffset(nextOffset);
        setLoading(false);
        if (!append && !selectionChanged) {
          const next = keep
            || visible.find((item) => item.id === wanted)
            || visible.find((item) => !item.archived)
            || visible[0] || null;
          setActiveId(next ? next.id : null);
        }
      });
    } catch (error) {
      if (seq !== sessionsSeq.current) return;
      apply(() => {
        setLoading(false);
        setHistoryError(error instanceof AuthError
          ? `目录读取失败（${error.status}）`
          : '目录读取失败，请稍后重试。');
      });
    }
  }, [apply, call, sessionStatus]);

  const reloadSessions = useCallback(async (select?: string | null) => {
    await loadSessionsPage(0, false, select);
  }, [loadSessionsPage]);

  useEffect(() => {
    const stored = localStorage.getItem(
      identityCacheKey(ctx, 'active_session'));
    void loadSessionsPage(0, false, stored);
  }, [ctx, loadSessionsPage]);

  // 身份作用域的活动会话；仅写当前 epoch
  useEffect(() => {
    if (!activeId) return;
    const key = identityCacheKey(ctx, 'active_session');
    commitIfCurrent(epochs, epoch,
      () => localStorage.setItem(key, activeId));
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
    } catch { contentChannel.current = null; }
    return () => {
      channel?.close();
      contentChannel.current = null;
    };
  }, [ctx.userId, ctx.workspaceId, reloadSessions]);

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

  const toUiMessages = (rows: ServerMessage[]): UiMessage[] =>
    rows.map((item) => ({
      role: item.role === 'assistant' ? 'assistant' as const : 'user' as const,
      content: item.content,
    }));

  const fetchMessagesPage = useCallback(async (
    sessionId: string, offset: number, limit: number = 500,
  ) => {
    return parseMessagePage(await call(
      `/api/sessions/${encodeURIComponent(sessionId)}/messages`
      + `?limit=${limit}&offset=${offset}`));
  }, [call]);

  const fetchLatestMessages = useCallback(async (sessionId: string) => {
    const first = await fetchMessagesPage(sessionId, 0);
    if (first.total <= 500) {
      return { messages: toUiMessages(first.messages), start: 0,
        total: first.total };
    }
    const start = first.total - 500;
    const latest = await fetchMessagesPage(sessionId, start);
    return { messages: toUiMessages(latest.messages), start,
      total: latest.total };
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
      setMessages(latest.messages);
      setMessageStart(latest.start);
      setMessageTotal(latest.total);
    });
    return latest;
  }, [apply, fetchLatestMessages, isSelected]);

  const loadActiveRuns = useCallback(async (sessionId: string) => {
    const runs: RunMeta[] = [];
    let offset = 0;
    while (true) {
      const parsed = parseRunPage(await call(
        `/api/sessions/${encodeURIComponent(sessionId)}/runs`
        + `?active=true&limit=200&offset=${offset}`));
      runs.push(...parsed.runs);
      if (parsed.nextOffset === null || parsed.runs.length === 0) break;
      offset = parsed.nextOffset;
    }
    return runs;
  }, [call]);

  const finishRun = useCallback(async (
    sessionId: string, runId: string, state: string,
    owns: () => boolean,
  ) => {
    // 终态历史收敛只受身份/会话/消息窗口约束：旧连接不再是当前 Run 时，
    // 只要本人当前选择仍有待确认发送，服务端快照仍可用于恢复已完成回答；
    // 新 Run 的状态、题卡和选择由 runId 闸门保护，旧收尾不得改动。
    const allowed = () => epochs.isCurrent(epoch)
      && activeIdRef.current === sessionId
      && (owns() || localSendsRef.current.some(
        (item) => item.sessionId === sessionId));
    if (!allowed()) return;
    clearRecoveryForRun(ctx, sessionId, runId);
    messageSeq.current += 1;
    const generation = messageSeq.current;
    let latest;
    try {
      latest = await fetchLatestMessages(sessionId);
    } catch {
      if (allowed()) {
        apply(() => {
          setNotice('最终历史读取失败；待确认输入仍保留，可重试或稍后恢复。');
          setRun((current) => current && current.runId === runId
            ? { ...current, state, question: null } : current);
          if (runRef.current?.runId === runId) setAnswers({});
        });
      }
      return;
    }
    if (!allowed() || generation !== messageSeq.current) return;
    apply(() => {
      setMessages(latest.messages);
      setMessageStart(latest.start);
      setMessageTotal(latest.total);
      setRun((current) => current && current.runId === runId
        ? { ...current, state, question: null } : current);
      if (runRef.current?.runId === runId) setAnswers({});
    });
    // 该 Run 的用户消息已随服务端快照提交，移除对应的本地待确认条目
    removeLocalSendRun(runId);
  }, [apply, ctx, epoch, epochs, fetchLatestMessages, removeLocalSendRun]);

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
    if (!previous || previous.runId !== runId) {
      // 新 Run 开始：旧题卡选择不能带入
      setAnswers({});
    }
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
      let response: Response;
      try {
        response = await fetch(
          `/api/chat/jobs/${encodeURIComponent(runId)}/stream`
          + `?after=${Math.max(0, after)}`, {
            method: 'GET', credentials: 'same-origin', cache: 'no-store',
            signal: controller.signal,
          });
      } catch (error) {
        if ((error as Error)?.name !== 'AbortError' && owns()) {
          apply(() => setNotice(
            '连接中断。不会自动重发；可点击“重连”只回放。'));
        }
        return;
      }
      if (response.status === 401) {
        // 旧 epoch 迟到的 401 不能把刚登录的 B 踢下线
        if (epochs.isCurrent(epoch)) onExpired();
        return;
      }
      if (response.status === 404) {
        if (owns()) {
          apply(() => {
            setNotice('任务记录不存在或已失效；请从服务端历史确认结果。');
            setRun(null);
          });
          clearRecovery(ctx, sessionId);
          await refreshLatestMessages(sessionId);
        }
        return;
      }
      if (!response.ok || !response.body) {
        if (owns()) {
          apply(() => setNotice(
            `事件流不可用（${response.status}），可稍后重连。`));
        }
        return;
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let terminal = false;
      try {
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const parsed = consumeSseChunk('', buffer);
          buffer = parsed.rest;
          for (const frame of parsed.frames) {
            const ended = await handleFrame(
              frame, sessionId, runId, apply, setAnswers, setRun,
              setNotice, call, finishRun, persist, owns);
            if (ended) terminal = true;
            if (controller.signal.aborted) return;
          }
        }
        const tail = consumeSseChunk(buffer, '\n\n');
        for (const frame of tail.frames) {
          const ended = await handleFrame(
            frame, sessionId, runId, apply, setAnswers, setRun,
            setNotice, call, finishRun, persist, owns);
          if (ended) terminal = true;
        }
      } catch (error) {
        if ((error as Error)?.name !== 'AbortError' && owns()) {
          apply(() => setNotice(
            '连接中断。不会自动重发；可点击“重连”只回放。'));
        }
      }
      if (!terminal && !controller.signal.aborted && owns()) {
        const current = runRef.current;
        if (!current || !isTerminalRun(current.state)) {
          apply(() => setNotice(
            '连接已断开。后台任务可能仍在执行；重连只回放，不会再次发送。'));
        }
      }
    } finally {
      // 覆盖 fetch 失败、非 2xx、缺 body、读取结束/异常/主动取消所有退出路径；
      // 只清理属于自己的 controller/连接状态，旧失败不得清掉新连接。
      if (streamRef.current === controller) {
        streamRef.current = null;
        if (streamOwner.current === runId) streamOwner.current = null;
      }
      if (connectionSeq.current === connection) {
        // 可渲染状态：相同 503 文案的连续失败也必须重新显示重连按钮
        setStreamActive(false);
      }
    }
  }, [abortStream, apply, call, ctx, epoch, epochs, finishRun,
      isSelected, onExpired, refreshLatestMessages]);

  useEffect(() => {
    if (!activeId) { setMessages([]); setRun(null); return; }
    const sessionId = activeId;
    const seq = ++selectionSeq.current;
    abortStream();
    setMessages([]);
    setRun(null);
    setAnswers({});
    setNotice('');
    setHistoryError('');
    setMessageStart(0);
    setMessageTotal(0);
    setMessageLoadingMore(false);
    // 初次读取也占用消息窗口代次；晚到的旧初次快照不得覆盖较新的提交。
    const generation = ++messageSeq.current;
    let cancelled = false;
    const sendAtStart = sendSeq.current;
    // 只按加载开始前已接纳的 Run 清理；加载期间新接纳的 Run 不误删。
    const knownRunIds = new Set(localSendsRef.current
      .filter((item) => item.sessionId === sessionId && item.runId)
      .map((item) => item.runId as string));
    (async () => {
      try {
        const runs = await loadActiveRuns(sessionId);
        const latest = await fetchLatestMessages(sessionId);
        if (cancelled || !isSelected(sessionId, seq)
            || generation !== messageSeq.current) return;
        apply(() => {
          setMessages(latest.messages);
          setMessageStart(latest.start);
          setMessageTotal(latest.total);
        });
        // 已接纳且不再活跃的本地发送已写入服务端快照，移除待确认条目。
        pruneLocalSends(sessionId, knownRunIds,
          new Set(runs.map((item) => item.runId)));
        if (sendSeq.current !== sendAtStart) return;
        const recovery = readRecovery(ctx, sessionId);
        const plan = recoveryAction(runs, recovery?.runId || null);
        if (plan.action === 'stream' && plan.runId) {
          const turnId = recovery?.runId === plan.runId
            ? recovery.turnId : '';
          // 新组件没有可信的完整前置状态：从 0 重放重建
          await connectRun(plan.runId, sessionId, 0, turnId, null, seq);
        } else if (recovery) {
          clearRecovery(ctx, sessionId);
        }
      } catch (error) {
        if (cancelled || !isSelected(sessionId, seq)
            || generation !== messageSeq.current) return;
        apply(() => setHistoryError(error instanceof AuthError
          ? `历史读取失败（${error.status}）`
          : '历史读取失败，请稍后重试。'));
      }
    })();
    return () => { cancelled = true; };
  }, [abortStream, activeId, apply, connectRun, ctx, fetchLatestMessages,
      isSelected, loadActiveRuns, pruneLocalSends]);

  const loadMoreSessions = useCallback(async () => {
    if (sessionNextOffset === null) return;
    await loadSessionsPage(sessionNextOffset, true);
  }, [loadSessionsPage, sessionNextOffset]);

  const loadOlderMessages = useCallback(async () => {
    const sessionId = activeIdRef.current;
    if (!sessionId || messageStart <= 0 || messageLoadingMore) return;
    setMessageLoadingMore(true);
    const generation = messageSeq.current;
    try {
      const window = olderMessageWindow(messageStart);
      const payload = await fetchMessagesPage(
        sessionId, window.offset, window.limit);
      if (!isSelected(sessionId) || generation !== messageSeq.current) return;
      apply(() => {
        setMessages((current) => [
          ...toUiMessages(payload.messages), ...current,
        ]);
        setMessageStart(window.offset);
      });
    } catch {
      if (isSelected(sessionId) && generation === messageSeq.current) {
        apply(() => setNotice('更早历史加载失败，请重试。'));
      }
    } finally {
      setMessageLoadingMore(false);
    }
  }, [apply, fetchMessagesPage, isSelected, messageLoadingMore,
      messageStart]);

  const createSession = useCallback(async () => {
    try {
      const raw = await call<Record<string, unknown>>('/api/sessions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: '新对话' }),
      });
      if (!raw || typeof raw.id !== 'string') {
        throw new Error('session_invalid');
      }
      apply(() => setLoading(false));
      contentChannel.current?.postMessage({ type: 'sessions-changed' });
      await reloadSessions(raw.id);
    } catch (error) {
      apply(() => setNotice(error instanceof AuthError
        ? `新建会话失败（${error.status}）` : '新建会话失败。'));
    }
  }, [apply, call, reloadSessions]);

  const renameSession = useCallback(async () => {
    if (!activeId) return;
    const current = sessions.find((item) => item.id === activeId);
    const title = window.prompt('新标题', current?.title || '');
    if (!title || !title.trim()) return;
    try {
      await call(`/api/sessions/${encodeURIComponent(activeId)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: title.trim() }),
      });
      contentChannel.current?.postMessage({ type: 'sessions-changed' });
      await reloadSessions(activeId);
    } catch (error) {
      apply(() => setNotice(existsStatus(error)
        ? `重命名失败（${(error as AuthError).status}）` : '重命名失败。'));
    }
  }, [activeId, apply, call, reloadSessions, sessions]);

  const archiveSession = useCallback(async () => {
    if (!activeId) return;
    try {
      await call(`/api/sessions/${encodeURIComponent(activeId)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ archived: true }),
      });
      contentChannel.current?.postMessage({ type: 'sessions-changed' });
      await reloadSessions(null);
    } catch (error) {
      apply(() => setNotice(existsStatus(error)
        ? `归档失败（${(error as AuthError).status}）` : '归档失败。'));
    }
  }, [activeId, apply, call, reloadSessions]);

  const deleteSession = useCallback(async () => {
    if (!activeId) return;
    if (!window.confirm('删除该会话？服务端软删除，历史不再展示。')) return;
    try {
      await call(`/api/sessions/${encodeURIComponent(activeId)}`, {
        method: 'DELETE',
      });
      clearRecovery(ctx, activeId);
      contentChannel.current?.postMessage({ type: 'sessions-changed' });
      await reloadSessions(null);
    } catch (error) {
      apply(() => setNotice(existsStatus(error)
        ? `删除失败（${(error as AuthError).status}）` : '删除失败。'));
    }
  }, [activeId, apply, call, ctx, reloadSessions]);

  const submitTurn = useCallback(async (
    sessionId: string, turnId: string, text: string,
  ) => {
    const epochAtStart = epoch;
    if (!epochs.isCurrent(epochAtStart)) return;
    // 从开始提交新输入起就保护本地发送代次：待确认输入独立于服务端消息
    // 列表展示，旧终态/404 刷新可用于恢复已完成回答，但不得改动新 Run。
    sendSeq.current += 1;
    const sendGeneration = sendSeq.current;
    setNotice('');
    beginLocalSend({
      sessionId, turnId, text, generation: sendGeneration, status: 'sending',
    });
    try {
      const body = await call<Record<string, unknown>>('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sessionId, turnId, message: text }),
      });
      if (!epochs.isCurrent(epochAtStart)) return;
      const admission = parseAdmission(202, body);
      markLocalAdmitted(sendGeneration, turnId, admission.runId);
      const recovery = {
        runId: admission.runId, turnId, eventId: 0,
      };
      if (activeIdRef.current === sessionId) {
        writeRecovery(ctx, sessionId, recovery);
        await connectRun(admission.runId, sessionId, 0, turnId, null,
          selectionSeq.current);
      } else {
        // 同一身份切到其它会话：保留本人作用域恢复信息，但不抢占当前界面/网络
        writeRecovery(ctx, sessionId, recovery);
      }
    } catch (error) {
      if (!epochs.isCurrent(epochAtStart)) return;
      // 已有更新的本地发送代次接管界面；旧请求的失败不再改写较新的待确认状态
      if (sendSeq.current !== sendGeneration) return;
      markLocalFailed(sendGeneration, turnId);
      if (activeIdRef.current !== sessionId) return;
      const status = error instanceof AuthError ? error.status : 0;
      const detail = error instanceof Error ? error.message : '';
      apply(() => setNotice(
        status === 409 ? `当前会话已有任务在运行或请求冲突：${detail}`
          : status === 502 || status === 0
            ? '发送结果未知。不会自动重发；可点击“重试同一请求”确认。'
            : `发送失败（${status || '网络'}）：${detail}`));
    }
  }, [apply, beginLocalSend, call, connectRun, ctx, epoch, epochs,
      markLocalAdmitted, markLocalFailed]);

  const activeSession = sessions.find((item) => item.id === activeId) || null;
  const visibleRun = run && run.sessionId === activeId ? run : null;

  const send = useCallback(async () => {
    const text = input.trim();
    if (!text || !activeId) return;
    if (activeSession?.archived) {
      setNotice('该会话已归档，历史只读；新任务需先取消归档。');
      return;
    }
    if (run && isActiveRun(run.state)) {
      setNotice('当前任务仍在执行，请先等待或停止。');
      return;
    }
    const turnId = makeTurnId();
    setInput('');
    await submitTurn(activeId, turnId, text);
  }, [activeId, activeSession, input, run, submitTurn]);

  const stop = useCallback(async () => {
    const current = runRef.current;
    if (!current || !isActiveRun(current.state)) return;
    if (!epochs.isCurrent(epoch)
        || activeIdRef.current !== current.sessionId) return;
    try {
      await call('/api/chat/stop', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ runId: current.runId }),
      });
    } catch (error) {
      if (!isSelected(current.sessionId)) return;
      apply(() => setNotice(error instanceof AuthError
        ? `停止失败（${error.status}）` : '停止失败。'));
    }
  }, [apply, call, epoch, epochs, isSelected]);

  const answer = useCallback(async (question: ControlQuestion) => {
    const current = runRef.current;
    if (!current || !epochs.isCurrent(epoch)
        || activeIdRef.current !== current.sessionId) return;
    const payload: Record<string, string[]> = {};
    for (let index = 0; index < question.questions.length; index += 1) {
      const item = question.questions[index];
      const key = questionAnswerKey(item, index);
      const chosen = answers[key];
      if (!chosen) {
        setNotice('请为每个问题选择一个选项。');
        return;
      }
      payload[key] = [chosen];
    }
    try {
      await call('/api/chat/question/answer', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          runId: current.runId, questionId: question.id, answers: payload,
        }),
      });
      if (!isSelected(current.sessionId)
          || runRef.current?.runId !== current.runId) return;
      apply(() => {
        setRun((value) => value && value.runId === current.runId ? {
          ...value, question: null,
          answered: [...value.answered, question.id],
        } : value);
        setAnswers({});
      });
    } catch (error) {
      if (!isSelected(current.sessionId)
          || runRef.current?.runId !== current.runId) return;
      apply(() => {
        setNotice(error instanceof AuthError && error.status === 409
          ? '该问题已过期或已回答；请等待服务端最终结果。'
          : '回答提交失败。');
        setRun((value) => value && value.runId === current.runId
          ? { ...value, question: null } : value);
      });
    }
  }, [answers, apply, call, epoch, epochs, isSelected]);

  const canSend = !!activeId && !loading && !activeSession?.archived
    && !(visibleRun && isActiveRun(visibleRun.state));

  return (
    <div style={styles.page}>
      <aside style={styles.aside}>
        <div style={{ fontSize: '12px', letterSpacing: '.12em',
          color: '#8b5cf6' }}>EASEL CONTROL</div>
        <div style={{ fontSize: '12px', color: '#8a92a1',
          overflow: 'hidden', textOverflow: 'ellipsis' }}>
          {ctx.role === 'admin' ? '管理员' : '成员'} · {ctx.workspaceId.slice(0, 8)}
        </div>
        <div style={{ display: 'grid', gap: '6px' }}>
          <button style={{
            ...styles.button,
            borderColor: view === 'chat' ? '#8b5cf6' : '#333a47',
          }} onClick={() => setView('chat')}>对话</button>
          <button style={{
            ...styles.button,
            borderColor: view === 'settings' ? '#8b5cf6' : '#333a47',
          }} onClick={() => setView('settings')}>个人设置</button>
          {ctx.role === 'admin' ? (
            <button style={{
              ...styles.button,
              borderColor: view === 'admin' ? '#8b5cf6' : '#333a47',
            }} onClick={() => setView('admin')}>全体默认</button>
          ) : null}
        </div>
        <button style={styles.accent} onClick={() => void createSession()}>
          ＋ 新建会话
        </button>
        <div style={{ overflowY: 'auto', display: 'grid', gap: '6px',
          maxHeight: '55vh' }}>
          {sessions.map((session) => (
            <button
              key={session.id}
              onClick={() => chooseSession(session.id)}
              style={{
                ...styles.button,
                textAlign: 'left', overflow: 'hidden',
                textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                borderColor: session.id === activeId ? '#8b5cf6' : '#333a47',
                opacity: session.archived ? 0.6 : 1,
              }}
            >{session.title || '未命名'}{session.archived ? '（已归档）' : ''}
            </button>
          ))}
          {sessionNextOffset !== null ? (
            <button style={styles.button}
              onClick={() => void loadMoreSessions()}>加载更多会话</button>
          ) : null}
          {!loading && sessions.length === 0 ? (
            <div style={{ color: '#818a99', fontSize: '13px' }}>
              还没有会话。新建一个开始对话。
            </div>
          ) : null}
        </div>
        <div style={{ marginTop: 'auto', display: 'grid', gap: '8px' }}>
          <button style={styles.button} onClick={() => void checkIdentityReload()}>
            重新验证身份
          </button>
          <button style={styles.button} onClick={onLogout}>退出登录</button>
        </div>
      </aside>
      <main style={styles.main}>
        <div style={{
          display: view === 'chat' ? 'flex' : 'none',
          flexDirection: 'column', minHeight: '100vh', minWidth: 0,
        }}>
        <header style={{
          display: 'flex', alignItems: 'center', gap: '10px',
          padding: '14px 20px', borderBottom: '1px solid #242a35',
        }}>
          <strong style={{ flex: 1, overflow: 'hidden',
            textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {activeSession?.title || '选择或新建会话'}
            {activeSession?.archived ? '（已归档 · 只读）' : ''}
          </strong>
          {activeSession ? (
            <>
              <button style={styles.button}
                onClick={() => void renameSession()}>重命名</button>
              <button style={styles.button}
                onClick={() => void archiveSession()}>归档</button>
              <button style={styles.button}
                onClick={() => void deleteSession()}>删除</button>
            </>
          ) : null}
          {visibleRun && isActiveRun(visibleRun.state) ? (
            <button style={styles.button}
              onClick={() => void stop()}>停止</button>
          ) : null}
        </header>
        <section style={{
          flex: 1, overflowY: 'auto', padding: '20px',
          display: 'grid', gap: '12px', alignContent: 'start',
        }}>
          {messageStart > 0 ? (
            <button style={styles.button} disabled={messageLoadingMore}
              onClick={() => void loadOlderMessages()}>
              {messageLoadingMore ? '加载中…' : '加载更早历史'}
            </button>
          ) : null}
          {historyError ? (
            <div role="alert" style={{ color: '#ff9b9b', fontSize: '14px' }}>
              {historyError}
            </div>
          ) : null}
          {messages.map((message, index) => (
            <article key={`${message.role}-${index}`} style={{
              maxWidth: '760px', whiteSpace: 'pre-wrap',
              lineHeight: 1.6, fontSize: '14px',
              padding: '10px 14px', borderRadius: '12px',
              background: message.role === 'user' ? '#242a36' : '#171a21',
              border: '1px solid #242a35',
              justifySelf: message.role === 'user' ? 'end' : 'start',
            }}>
              <div style={{ fontSize: '11px', color: '#8a92a1',
                marginBottom: '4px' }}>
                {message.role === 'user' ? '你' : '助手'}
              </div>
              {message.content}
            </article>
          ))}
          {localSends.filter((item) => item.sessionId === activeId).map(
            (item) => (
              <article key={`local-${item.turnId}`} style={{
                maxWidth: '760px', whiteSpace: 'pre-wrap',
                lineHeight: 1.6, fontSize: '14px',
                padding: '10px 14px', borderRadius: '12px',
                background: '#242a36', border: '1px solid #242a35',
                justifySelf: 'end',
              }}>
                <div style={{ fontSize: '11px', color: '#8a92a1',
                  marginBottom: '4px' }}>
                  你 · {item.status === 'failed'
                    ? '待确认（未完成）' : '发送中'}
                </div>
                {item.text}
              </article>
            ))}
          {visibleRun && visibleRun.content && !isTerminalRun(visibleRun.state)
            ? (
              <article style={{
                maxWidth: '760px', whiteSpace: 'pre-wrap', lineHeight: 1.6,
                fontSize: '14px', padding: '10px 14px',
                borderRadius: '12px', background: '#171a21',
                border: '1px solid #242a35',
              }}>
                <div style={{ fontSize: '11px', color: '#8a92a1',
                  marginBottom: '4px' }}>助手 · {visibleRun.state}</div>
                {visibleRun.content}
              </article>
            ) : null}
          {visibleRun?.question ? (
            <QuestionCard
              question={visibleRun.question}
              answers={answers}
              onChange={(key, value) => setAnswers((current) => ({
                ...current, [key]: value,
              }))}
              onSubmit={() => void answer(visibleRun.question!)}
            />
          ) : null}
          {notice ? (
            <div role="status" style={{ color: '#f0b45a', fontSize: '13px' }}>
              {notice}
            </div>
          ) : null}
          {localSends.filter((item) => item.sessionId === activeId
            && item.status === 'failed').map((item) => (
            <button key={`retry-${item.turnId}`} style={styles.accent}
              onClick={() => void submitTurn(
                item.sessionId, item.turnId, item.text)}>
              重试同一请求（不会新造请求键）
            </button>
          ))}
          {visibleRun && !isTerminalRun(visibleRun.state)
            && !streamActive ? (
            <button style={styles.button} onClick={() => {
              const current = visibleRun;
              void connectRun(current.runId, current.sessionId,
                current.cursor, current.turnId, current,
                selectionSeq.current);
            }}>重连（只回放）</button>
          ) : null}
          {messageTotal > messageStart + messages.length ? (
            <div style={{ fontSize: '12px', color: '#6f7785' }}>
              已加载 {messages.length} / {messageTotal} 条消息
            </div>
          ) : null}
        </section>
        <footer style={{
          padding: '14px 20px', borderTop: '1px solid #242a35',
          display: 'grid', gap: '8px',
        }}>
          <div style={{ display: 'flex', gap: '8px' }}>
            <textarea
              value={input}
              onChange={(event) => setInput(event.target.value)}
              placeholder={activeSession?.archived ? '已归档，只读历史'
                : activeId ? '输入消息…' : '先新建或选择会话'}
              disabled={!canSend}
              rows={2}
              onKeyDown={(event) => {
                if (event.key === 'Enter' && !event.shiftKey) {
                  event.preventDefault();
                  void send();
                }
              }}
              style={{
                flex: 1, resize: 'vertical', minHeight: '44px',
                background: '#0f1115', color: '#e7e9ee',
                border: '1px solid #333a47', borderRadius: '10px',
                padding: '10px 12px', fontSize: '14px',
              }}
            />
            <button style={{ ...styles.accent, alignSelf: 'end' }}
              disabled={!canSend || !input.trim()}
              onClick={() => void send()}>发送</button>
          </div>
          <div style={{ fontSize: '12px', color: '#6f7785' }}>
            技能执行、发布、数据分析、画像与平台登录管理暂未开放；
            本页只接入对话、个人能力模型与管理员默认配置。
          </div>
        </footer>
        </div>
        {view === 'settings' ? (
          <ControlSettingsPage ctx={ctx} call={call} />
        ) : null}
        {view === 'admin' && ctx.role === 'admin' ? (
          <AdminSettingsPage ctx={ctx} call={call} />
        ) : null}
      </main>
    </div>
  );

  function checkIdentityReload(): void {
    // 重新验证由根身份壳负责；这里只触发一次安全的重载
    window.location.reload();
  }
}

function existsStatus(error: unknown): boolean {
  return error instanceof AuthError;
}

function QuestionCard({ question, answers, onChange, onSubmit }: {
  question: ControlQuestion;
  answers: Record<string, string>;
  onChange: (key: string, value: string) => void;
  onSubmit: () => void;
}) {
  return (
    <section style={{
      border: '1px solid #4b3b7a', background: '#191625',
      borderRadius: '12px', padding: '14px', display: 'grid', gap: '10px',
    }}>
      {question.questions.map((item, index) => {
        const key = questionAnswerKey(item, index);
        return (
          <div key={key} style={{ display: 'grid', gap: '6px' }}>
            <div style={{ fontSize: '13px', color: '#d8d2ea' }}>
              {item.header ? `${item.header} · ` : ''}
              {item.question || key}
            </div>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: '8px' }}>
              {(item.options || []).map((option) => (
                <label key={`${key}-${option.label}`} style={{
                  display: 'flex', alignItems: 'center', gap: '6px',
                  fontSize: '13px', color: '#cfd5df',
                  border: '1px solid #3a3350', borderRadius: '8px',
                  padding: '6px 10px', cursor: 'pointer',
                }}>
                  <input
                    type="radio"
                    name={`q-${question.id}-${key}`}
                    checked={answers[key] === optionSubmitValue(option)}
                    onChange={() => onChange(key, optionSubmitValue(option))}
                  />
                  {option.label}
                </label>
              ))}
            </div>
          </div>
        );
      })}
      <button style={styles.accent} onClick={onSubmit}>提交回答</button>
    </section>
  );
}

async function handleFrame(
  frame: SseFrame,
  sessionId: string,
  runId: string,
  apply: (fn: () => void) => void,
  setAnswers: (value: Record<string, string>) => void,
  setRun: Dispatch<SetStateAction<ClientRun | null>>,
  setNotice: (value: string) => void,
  call: <T,>(path: string, init?: RequestInit) => Promise<T>,
  finishRun: (sessionId: string, runId: string, state: string,
              owns: () => boolean) => Promise<void>,
  persist: (cursor: number) => void,
  owns: () => boolean,
): Promise<boolean> {
  // 整个帧都属于启动该连接/选择的 Run；旧连接迟到帧一律不得提交。
  // 仅终态允许触发一次受身份/会话/待确认发送约束的历史收敛，
  // 否则新请求失败时上一轮已完成回答会永久缺失。
  if (!owns()) {
    if (frame.event === 'state') {
      const state = stateFromEvent(frame);
      if (state && isTerminalRun(state.state)) {
        await finishRun(sessionId, runId, state.state, owns);
      }
    }
    return false;
  }
  // 只在状态事件写恢复游标；token 是 provisional，刷新后可从状态点重放。
  if (frame.event === 'state' && frame.id > 0) persist(frame.id);
  if (frame.event === 'state') {
    const state = stateFromEvent(frame);
    if (!state) return false;
    const terminal = isTerminalRun(state.state);
    apply(() => {
      setRun((current) => current && current.runId === runId
        ? { ...current, state: state.state, cursor: frame.id } : current);
    });
    if (terminal) {
      await finishRun(sessionId, runId, state.state, owns);
      return true;
    }
    return false;
  }
  if (frame.event === 'token') {
    const text = textFromEvent(frame);
    apply(() => setRun((current) => current && current.runId === runId
      ? { ...current, content: current.content + text,
        cursor: frame.id } : current));
    return false;
  }
  if (frame.event === 'thinking') {
    const text = textFromEvent(frame);
    apply(() => setRun((current) => current && current.runId === runId
      ? { ...current, thinking: current.thinking + text,
        cursor: frame.id } : current));
    return false;
  }
  if (frame.event === 'activity') {
    const text = textFromEvent(frame);
    apply(() => setRun((current) => current && current.runId === runId
      ? { ...current, activity: text, cursor: frame.id } : current));
    return false;
  }
  if (frame.event === 'error') {
    const message = textFromEvent(frame);
    if (owns()) apply(() => setNotice(message));
    return false;
  }
  if (frame.event === 'question') {
    if (!owns()) return false;
    const question = parseQuestionData(frame.data);
    if (!question) return false;
    try {
      const status = await call<Record<string, unknown>>(
        '/api/chat/question/status', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            runId, questionIds: [question.id],
          }),
        });
      const entry = (status.questions as Record<string, {
        status?: string }> | undefined)?.[question.id];
      if (entry?.status && entry.status !== 'pending') {
        if (!owns()) return false;
        apply(() => setRun((current) => current
          && current.runId === runId ? {
            ...current, answered: [...current.answered, question.id],
            cursor: frame.id,
          } : current));
        return false;
      }
    } catch { /* 状态查询失败仍展示题卡；回答会被服务端校验 */ }
    if (!owns()) return false;
    apply(() => {
      setAnswers({});
      setRun((value) => {
        if (!value || value.runId !== runId
            || value.answered.includes(question.id)) return value;
        return { ...value, question, cursor: frame.id };
      });
    });
  }
  return false;
}

