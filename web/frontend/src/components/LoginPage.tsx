import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { exchangeInvite, loginErrorMessage } from '../lib/auth';
import type { AuthContext } from '../lib/auth';
import AuthFrame from './AuthFrame';

interface LoginPageProps {
  onAuthenticated: (ctx: AuthContext) => void;
  notice?: string;
}

export default function LoginPage({ onAuthenticated, notice }: LoginPageProps) {
  const [token, setToken] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    inputRef.current?.focus();
    // 组件卸载/身份切换时不保留邀请明文
    return () => setToken('');
  }, []);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (busy || !token.trim()) return;
    setBusy(true);
    setError('');
    try {
      const ctx = await exchangeInvite(token.trim());
      setToken('');
      onAuthenticated(ctx);
    } catch (failure) {
      setToken('');
      setError(loginErrorMessage(failure));
    } finally {
      setBusy(false);
    }
  };

  return (
    <AuthFrame>
      <div className="auth-copy">
        <h1 className="auth-title">邀请登录</h1>
        <p className="auth-lead">
          使用管理员签发的邀请进入 Easel 工作区。登录后仍是原来的工作台与对话页。
        </p>
      </div>
      <form className="auth-form" onSubmit={submit}>
        <label className="auth-label" htmlFor="invite">邀请令牌</label>
        <input
          id="invite"
          ref={inputRef}
          className="field"
          value={token}
          onChange={(event) => setToken(event.target.value)}
          autoComplete="off"
          spellCheck={false}
          disabled={busy}
          aria-describedby="invite-help"
        />
        <p id="invite-help" className="auth-help">
          这是一次性邀请，使用后失效；退出后需重新获取。
        </p>
        <button
          type="submit"
          className="btn btn-primary auth-submit"
          disabled={busy || !token.trim()}
        >{busy ? '正在验证…' : '进入工作区'}</button>
      </form>
      {notice ? (
        <p role="status" className="auth-notice">{notice}</p>
      ) : null}
      {error ? (
        <p role="alert" className="auth-error">{error}</p>
      ) : null}
      <details className="auth-help-details">
        <summary>如何重新获取邀请？</summary>
        <p>
          联系管理员在 Easel 设置的「成员与邀请」中重新签发。
          邀请不会自动续期，也不支持公开注册。
        </p>
      </details>
    </AuthFrame>
  );
}
