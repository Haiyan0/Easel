import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { exchangeInvite, loginErrorMessage } from '../lib/auth';
import type { AuthContext } from '../lib/auth';

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
    <main style={{
      minHeight: '100vh', display: 'flex', alignItems: 'center',
      justifyContent: 'center', background: '#0f1115', color: '#e7e9ee',
      padding: '24px', boxSizing: 'border-box',
    }}>
      <section style={{
        width: '100%', maxWidth: '420px', background: '#171a21',
        border: '1px solid #2a2f3a', borderRadius: '16px',
        padding: '28px', boxShadow: '0 24px 60px rgba(0,0,0,.35)',
      }}>
        <div style={{
          fontSize: '13px', letterSpacing: '.14em', color: '#8b5cf6',
          marginBottom: '10px',
        }}>EASEL CONTROL</div>
        <h1 style={{ fontSize: '24px', margin: '0 0 8px' }}>邀请登录</h1>
        <p style={{ color: '#9aa3b2', fontSize: '14px', margin: '0 0 22px' }}>
          使用管理员签发的一次性邀请进入工作区。没有公开注册入口。
        </p>
        <form onSubmit={submit}>
          <label htmlFor="invite" style={{
            display: 'block', fontSize: '13px', color: '#b8c0cc',
            marginBottom: '8px',
          }}>邀请令牌</label>
          <input
            id="invite"
            ref={inputRef}
            value={token}
            onChange={(event) => setToken(event.target.value)}
            autoComplete="off"
            spellCheck={false}
            disabled={busy}
            style={{
              width: '100%', boxSizing: 'border-box', padding: '12px 14px',
              borderRadius: '10px', border: '1px solid #333a47',
              background: '#0f1115', color: '#e7e9ee', fontSize: '15px',
              outline: 'none',
            }}
          />
          <button
            type="submit"
            disabled={busy || !token.trim()}
            style={{
              width: '100%', marginTop: '16px', padding: '12px 14px',
              borderRadius: '10px', border: 'none', cursor: busy
                ? 'wait' : 'pointer', background: busy ? '#5b4b9a' : '#8b5cf6',
              color: 'white', fontSize: '15px', fontWeight: 600,
            }}
          >{busy ? '正在验证…' : '进入工作区'}</button>
        </form>
        {notice ? (
          <p role="status" style={{
            margin: '14px 0 0', color: '#f0b45a', fontSize: '13px',
          }}>{notice}</p>
        ) : null}
        {error ? (
          <p role="alert" style={{
            margin: '14px 0 0', color: '#ff8c8c', fontSize: '13px',
          }}>{error}</p>
        ) : null}
        <p style={{
          margin: '22px 0 0', color: '#6f7785', fontSize: '12px',
          lineHeight: 1.6,
        }}>
          登录状态由服务端 HttpOnly Cookie 维护；浏览器不会把登录令牌写入本地存储。
        </p>
      </section>
    </main>
  );
}
