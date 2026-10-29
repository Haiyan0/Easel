import type { ReactNode } from 'react';
import ThemeToggle from './ThemeToggle';

/**
 * 登录、模式确认与身份检查共用原主题容器。
 * 只复用共享主题 token 与品牌图，不读取任何身份私有缓存。
 */
export default function AuthFrame({ children }: { children: ReactNode }) {
  return (
    <main className="auth-shell">
      <div className="auth-toolbar">
        <ThemeToggle />
      </div>
      <section className="auth-card">
        <div className="auth-brand">
          <img
            className="auth-brand-icon"
            src="./static/easel-icon-transparent.png"
            alt=""
            width={52}
            height={52}
          />
          <span className="auth-brand-name">Easel</span>
        </div>
        {children}
      </section>
    </main>
  );
}
