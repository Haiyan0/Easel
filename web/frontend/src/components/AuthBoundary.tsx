import { useCallback, useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import LoginPage from './LoginPage';
import AuthFrame from './AuthFrame';
import {
  AuthError, IdentityEpoch, clearIdentityCache, loadIdentity, logout,
} from '../lib/auth';
import type { AuthContext } from '../lib/auth';

const CONTROL_CHANNEL = 'easel-identity';

export interface AuthSession {
  ctx: AuthContext;
  epoch: number;
  epochs: IdentityEpoch;
  onExpired: () => void;
  onLogout: () => void;
}

interface AuthBoundaryProps {
  children: (session: AuthSession) => ReactNode;
}

function Centered({ text, children }: {
  text: string; children?: ReactNode;
}) {
  return (
    <AuthFrame>
      <div className="auth-status" role="status">
        <p className="auth-status-text">{text}</p>
        {children}
      </div>
    </AuthFrame>
  );
}

function clearCurrentIdentity(ctx: AuthContext): void {
  clearIdentityCache(ctx, [localStorage, sessionStorage]);
}

/**
 * 模式确认后的身份边界：control 必须先 loadIdentity 成功才挂载私人树。
 * pending/503 时只显示状态或登录页，不读取旧 session/publish/model/画像缓存，
 * 也不渲染 LocalApp。401/登出/跨标签切换统一清理当前身份缓存。
 */
export default function AuthBoundary({ children }: AuthBoundaryProps) {
  const epochs = useRef(new IdentityEpoch());
  const [status, setStatus] = useState<'checking' | 'anonymous'
    | 'ready' | 'error'>('checking');
  const [identity, setIdentity] = useState<AuthContext | null>(null);
  const [epoch, setEpoch] = useState(0);
  const [fatal, setFatal] = useState('');
  const [notice, setNotice] = useState('');
  const channelRef = useRef<BroadcastChannel | null>(null);
  const identityRef = useRef<AuthContext | null>(null);
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
    if (prior) clearCurrentIdentity(prior);
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
        const old = identityRef.current;
        checkSeq.current += 1;
        epochs.current.invalidate();
        setIdentity(null);
        if (old) clearCurrentIdentity(old);
        const kind = (event.data as { type?: string } | null)?.type;
        if (kind === 'logout') {
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
    clearCurrentIdentity(current);
    try { channelRef.current?.postMessage({ type: 'logout' }); }
    catch { /* channel closed */ }
    // 远端撤销使用失效前捕获的 CSRF，不随私人树卸载而中止。
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
        <button type="button" className="btn btn-primary"
          onClick={() => void checkIdentity()}>
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
  return <>{children({
    ctx: identity,
    epoch,
    epochs: epochs.current,
    onExpired: handleExpired,
    onLogout: handleLogout,
  })}</>;
}
