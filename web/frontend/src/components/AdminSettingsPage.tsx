import { useEffect, useRef, useState } from 'react';
import type { AuthContext } from '../lib/auth';
import {
  ADMIN_CHANNELS, buildManagedDefault, channelLabel, emptyRouteForm,
  errorStatus, errorText, formFromManaged, inviteStatusLabel,
  isPendingInvite, parseAdminInvitePage, parseAdminUser,
  parseAdminUserPage, parseIssuedInvite, parseManagedSettings,
} from '../lib/controlSettings';
import type {
  AdminChannel, AdminInvite, AdminUser, ControlCall, IssuedInvite,
  ManagedSettings, RouteForm,
} from '../lib/controlSettings';

const panelStyle = {
  padding: '24px',
  display: 'grid',
  gap: '16px',
  alignContent: 'start',
  maxWidth: '1080px',
  minHeight: '100vh',
} as const;

const cardStyle = {
  border: '1px solid #242a35',
  background: '#12151b',
  borderRadius: '12px',
  padding: '16px',
  display: 'grid',
  gap: '10px',
} as const;

const buttonStyle = {
  border: '1px solid #333a47',
  background: '#1b1f28',
  color: '#e7e9ee',
  borderRadius: '9px',
  padding: '8px 10px',
  cursor: 'pointer',
  fontSize: '13px',
} as const;

const inputStyle = {
  background: '#0f1115',
  color: '#e7e9ee',
  border: '1px solid #333a47',
  borderRadius: '8px',
  padding: '8px 10px',
  fontSize: '13px',
  width: '100%',
  boxSizing: 'border-box',
} as const;

interface Props {
  ctx: AuthContext;
  call: ControlCall;
}

type GuardedResult<T> = { ok: true; value: T } | { ok: false };

export default function AdminSettingsPage({ ctx, call }: Props) {
  const [managed, setManaged] = useState<ManagedSettings | null>(null);
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [invites, setInvites] = useState<AdminInvite[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState('');
  const [channel, setChannel] = useState<AdminChannel>('speech');
  const [form, setForm] = useState<RouteForm>(emptyRouteForm);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');
  const [reload, setReload] = useState(0);
  const [newMember, setNewMember] = useState('');
  const [inviteUserId, setInviteUserId] = useState('');
  const [issued, setIssued] = useState<IssuedInvite | null>(null);
  const loadSeq = useRef(0);
  const actionSeq = useRef(0);
  const formSeq = useRef(0);
  const mounted = useRef(true);
  const ownerRef = useRef(`${ctx.userId}:${ctx.workspaceId}`);
  const channelRef = useRef<AdminChannel>('speech');
  ownerRef.current = `${ctx.userId}:${ctx.workspaceId}`;

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      loadSeq.current += 1;
      actionSeq.current += 1;
    };
  }, []);

  useEffect(() => {
    if (ctx.role !== 'admin') {
      setLoading(false);
      return;
    }
    let alive = true;
    const seq = ++loadSeq.current;
    const owner = `${ctx.userId}:${ctx.workspaceId}`;
    setLoading(true);
    setLoadError('');
    Promise.allSettled([
      call<unknown>('/api/admin/managed-config'),
      call<unknown>('/api/admin/users?limit=50&offset=0'),
      call<unknown>('/api/admin/invites?limit=50&offset=0'),
    ]).then(([managedResult, usersResult, invitesResult]) => {
      if (!alive || !mounted.current || seq !== loadSeq.current
          || ownerRef.current !== owner) return;
      const errors: string[] = [];
      if (managedResult.status === 'fulfilled') {
        try {
          const parsedManaged = parseManagedSettings(managedResult.value);
          setManaged(parsedManaged);
          setForm(formFromManaged(parsedManaged, channelRef.current));
        } catch (error: unknown) {
          errors.push(`全体默认：${errorText(error)}`);
        }
      } else {
        errors.push(`全体默认：${errorText(managedResult.reason)}`);
      }
      if (usersResult.status === 'fulfilled') {
        try {
          const parsedUsers = parseAdminUserPage(usersResult.value);
          setUsers(parsedUsers.users);
          const firstMember = parsedUsers.users.find((item) =>
            item.role === 'member' && !item.disabled);
          setInviteUserId((current) => current || firstMember?.userId || '');
        } catch (error: unknown) {
          errors.push(`成员：${errorText(error)}`);
        }
      } else {
        errors.push(`成员：${errorText(usersResult.reason)}`);
      }
      if (invitesResult.status === 'fulfilled') {
        try {
          setInvites(parseAdminInvitePage(invitesResult.value).invites);
        } catch (error: unknown) {
          errors.push(`邀请：${errorText(error)}`);
        }
      } else {
        errors.push(`邀请：${errorText(invitesResult.reason)}`);
      }
      if (errors.length) setLoadError(`读取失败：${errors.join('；')}`);
    }).finally(() => {
      if (alive && mounted.current && seq === loadSeq.current) {
        setLoading(false);
      }
    });
    return () => { alive = false; };
  }, [call, ctx.role, ctx.userId, ctx.workspaceId, reload]);

  const chooseChannel = (next: AdminChannel) => {
    formSeq.current += 1;
    channelRef.current = next;
    setChannel(next);
    setNotice('');
    if (managed) setForm(formFromManaged(managed, next));
  };

  const updateForm = (patch: Partial<RouteForm>) => {
    formSeq.current += 1;
    setForm((current) => ({ ...current, ...patch }));
  };

  const guarded = async <T,>(
    action: () => Promise<T>,
    conflictMessage = '操作冲突，请重新读取后再试。',
  ): Promise<GuardedResult<T>> => {
    const seq = ++actionSeq.current;
    const owner = ownerRef.current;
    try {
      const value = await action();
      if (!mounted.current || seq !== actionSeq.current
          || ownerRef.current !== owner) return { ok: false };
      return { ok: true, value };
    } catch (error: unknown) {
      if (!mounted.current || seq !== actionSeq.current
          || ownerRef.current !== owner) return { ok: false };
      if (errorStatus(error) === 409) {
        setNotice(conflictMessage);
      } else {
        setNotice(`操作失败：${errorText(error)}`);
      }
      return { ok: false };
    }
  };

  const saveDefaults = async () => {
    if (!managed) return;
    setBusy(true);
    setNotice('');
    const version = managed.version;
    const generation = formSeq.current;
    const savedChannel = channel;
    const result = await guarded(() => call<{ version: number }>(
      '/api/admin/managed-config', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'If-Match': `"${version}"`,
        },
        body: JSON.stringify({
          defaults: { [channel]: buildManagedDefault(form) },
        }),
      }), '版本冲突：其他管理员已更新全体默认，请重新读取后再保存。');
    if (result.ok) {
      if (generation !== formSeq.current
          || channelRef.current !== savedChannel) {
        setNotice('已保存上一份全体默认；当前表单未被覆盖。');
      } else {
        setForm((current) => ({ ...current, secret: '' }));
        setNotice('全体默认已保存为新版本。');
        setReload((value) => value + 1);
      }
    }
    setBusy(false);
  };

  const refreshInvites = async () => {
    try {
      const raw = await call<unknown>(
        '/api/admin/invites?limit=50&offset=0');
      if (!mounted.current) return;
      setInvites(parseAdminInvitePage(raw).invites);
    } catch (error: unknown) {
      if (mounted.current) {
        setNotice(`邀请列表读取失败：${errorText(error)}`);
      }
    }
  };

  const createMember = async () => {
    if (!newMember.trim()) {
      setNotice('请输入新成员的显示名。');
      return;
    }
    setBusy(true);
    setNotice('');
    const result = await guarded(() => call<unknown>('/api/admin/users', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ displayName: newMember.trim() }),
    }));
    if (result.ok) {
      const created = parseAdminUser(result.value);
      setUsers((current) => [...current, created]);
      setInviteUserId((current) => current || created.userId);
      setNewMember('');
      setNotice(`已创建成员：${created.displayName}`);
    }
    setBusy(false);
  };

  const toggleDisabled = async (target: AdminUser) => {
    setBusy(true);
    setNotice('');
    const result = await guarded(() => call<unknown>(
      `/api/admin/users/${encodeURIComponent(target.userId)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ disabled: !target.disabled }),
      }));
    if (result.ok) {
      const updated = parseAdminUser(result.value);
      setUsers((current) => current.map((item) =>
        item.userId === updated.userId ? updated : item));
      setNotice(updated.disabled
        ? `已禁用 ${updated.displayName}；旧登录与未兑换邀请已失效。`
        : `已启用 ${updated.displayName}；仍需新邀请才能登录。`);
    }
    setBusy(false);
  };

  const revokeSessions = async (target: AdminUser) => {
    setBusy(true);
    setNotice('');
    const result = await guarded(() => call<{ revoked: number }>(
      `/api/admin/users/${encodeURIComponent(target.userId)}/revoke-sessions`,
      { method: 'POST' }));
    if (result.ok) setNotice(`已撤销 ${result.value.revoked} 个登录。`);
    setBusy(false);
  };

  const createInvite = async () => {
    const target = inviteTarget;
    if (!target) {
      setNotice('请先创建或选择一个成员。');
      return;
    }
    setBusy(true);
    setNotice('');
    const result = await guarded(() => call<unknown>('/api/admin/invites', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ userId: target }),
    }));
    if (result.ok) {
      setIssued(parseIssuedInvite(result.value));
      setNotice('邀请已签发；明文只显示本次结果。');
      await refreshInvites();
    }
    setBusy(false);
  };

  const revokeInvite = async (target: AdminInvite) => {
    setBusy(true);
    setNotice('');
    const result = await guarded(() => call<void>(
      `/api/admin/invites/${encodeURIComponent(target.id)}`,
      { method: 'DELETE' }),
    '邀请状态已变化，请重新读取后再试。');
    if (result.ok) {
      setNotice('邀请已撤销。');
      await refreshInvites();
    }
    setBusy(false);
  };

  const copyInvite = async () => {
    if (!issued) return;
    try {
      await navigator.clipboard.writeText(issued.token);
      setNotice('已复制邀请令牌。');
    } catch {
      setNotice('复制失败，请手动选择令牌文本。');
    }
  };

  if (ctx.role !== 'admin') {
    return (
      <section style={panelStyle} aria-label="管理员设置">
        <h1 style={{ margin: 0, fontSize: '20px' }}>全体默认</h1>
        <div role="alert" style={{ color: '#ff9b9b', fontSize: '14px' }}>
          无权访问管理员设置。
        </div>
      </section>
    );
  }

  const entry = managed?.defaults[channel];
  const memberOptions = users.filter((item) =>
    item.role === 'member' && !item.disabled);
  const inviteTarget = memberOptions.find((item) =>
    item.userId === inviteUserId)?.userId
    || memberOptions[0]?.userId || '';

  return (
    <section style={panelStyle} aria-label="管理员设置">
      <header style={{ display: 'grid', gap: '6px' }}>
        <h1 style={{ margin: 0, fontSize: '20px' }}>全体默认</h1>
        <p style={{ margin: 0, color: '#9aa3b2', fontSize: '13px' }}>
          这里修改的是所有成员继承的默认配置；个人覆盖与成员管理分开操作。
        </p>
      </header>

      {loadError ? (
        <div role="alert" style={{ color: '#ff9b9b', fontSize: '14px' }}>
          {loadError}
        </div>
      ) : null}
      {loading ? (
        <p style={{ color: '#9aa3b2', fontSize: '13px' }}>读取管理员设置…</p>
      ) : null}
      {notice ? (
        <div role="status" style={{
          color: notice.includes('失败') || notice.includes('冲突')
            ? '#ff9b9b' : '#8fd7a7',
          fontSize: '13px',
        }}>{notice}</div>
      ) : null}

      {managed ? (
        <section style={cardStyle} aria-label="全体默认配置">
          <h2 style={{ margin: 0, fontSize: '16px' }}>
            受管默认模型 · 版本 {managed.version}
          </h2>
          <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
            {ADMIN_CHANNELS.map((item) => (
              <button key={item} style={{
                ...buttonStyle,
                borderColor: item === channel ? '#8b5cf6' : '#333a47',
              }} aria-pressed={item === channel}
              disabled={busy || loading}
              onClick={() => chooseChannel(item)}>
                {channelLabel(item)}
              </button>
            ))}
          </div>
          <div style={{ fontSize: '12px', color: '#9aa3b2' }}>
            {entry
              ? `${entry.provider}/${entry.model} · 凭证${
                  entry.credential_configured ? '已配置' : '缺失'}`
              : '该通道尚未配置默认。'}
          </div>
          <label style={{ display: 'grid', gap: '4px', fontSize: '13px' }}>
            供应商
            <input aria-label="供应商" style={inputStyle}
              disabled={busy || loading}
              value={form.provider}
              onChange={(event) => updateForm({
                provider: event.target.value })} />
          </label>
          <label style={{ display: 'grid', gap: '4px', fontSize: '13px' }}>
            模型
            <input aria-label="模型" style={inputStyle}
              disabled={busy || loading}
              value={form.model}
              onChange={(event) => updateForm({
                model: event.target.value })} />
          </label>
          <label style={{ display: 'grid', gap: '4px', fontSize: '13px' }}>
            API 地址
            <input aria-label="API 地址" style={inputStyle}
              disabled={busy || loading}
              value={form.baseUrl}
              onChange={(event) => updateForm({
                baseUrl: event.target.value })} />
          </label>
          <label style={{ display: 'grid', gap: '4px', fontSize: '13px' }}>
            协议
            <input aria-label="协议" style={inputStyle}
              disabled={busy || loading}
              value={form.protocol}
              onChange={(event) => updateForm({
                protocol: event.target.value })} />
          </label>
          <label style={{ display: 'grid', gap: '4px', fontSize: '13px' }}>
            API Key
            <input aria-label="API Key" type="password"
              autoComplete="new-password" style={inputStyle}
              disabled={busy || loading}
              value={form.secret}
              onChange={(event) => updateForm({
                secret: event.target.value })} />
          </label>
          <label style={{ display: 'grid', gap: '4px', fontSize: '13px' }}>
            非敏感参数（JSON）
            <textarea aria-label="非敏感参数（JSON）" rows={3}
              disabled={busy || loading}
              style={inputStyle} value={form.options}
              onChange={(event) => updateForm({
                options: event.target.value })} />
          </label>
          <label style={{ display: 'grid', gap: '4px', fontSize: '13px' }}>
            允许的模型（逗号分隔）
            <input aria-label="允许的模型（逗号分隔）" style={inputStyle}
              disabled={busy || loading}
              value={form.allowedModels}
              onChange={(event) => updateForm({
                allowedModels: event.target.value })} />
          </label>
          <p style={{ margin: 0, color: '#6f7785', fontSize: '12px' }}>
            留空 Key 仅在已有该通道、同供应商/地址/协议时复用原受管凭证；
            新增通道或更换作用域必须显式输入。
          </p>
          <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
            <button style={{ ...buttonStyle, borderColor: '#8b5cf6' }}
              disabled={busy || loading}
              onClick={() => { void saveDefaults(); }}>
              保存全体默认
            </button>
            <button style={buttonStyle} disabled={busy}
              onClick={() => setReload((value) => value + 1)}>
              重新读取
            </button>
          </div>
        </section>
      ) : null}

      <section style={cardStyle} aria-label="成员与邀请">
        <h2 style={{ margin: 0, fontSize: '16px' }}>成员与邀请</h2>
        <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
          <input aria-label="新成员显示名" style={{ ...inputStyle, flex: 1 }}
            value={newMember} placeholder="新成员显示名"
            onChange={(event) => setNewMember(event.target.value)} />
          <button style={buttonStyle} disabled={busy}
            onClick={() => { void createMember(); }}>
            创建成员
          </button>
        </div>

        <div style={{ display: 'grid', gap: '8px' }}>
          {users.map((item) => (
            <div key={item.userId} style={{
              display: 'flex', gap: '8px', alignItems: 'center',
              flexWrap: 'wrap', fontSize: '13px', color: '#cfd5df',
            }}>
              <span style={{ minWidth: '120px' }}>{item.displayName}</span>
              <span style={{ color: '#9aa3b2' }}>
                {item.role === 'admin' ? '管理员' : '成员'}
                {item.disabled ? ' · 已禁用' : ''}
              </span>
              {item.role === 'member' ? (
                <>
                  <button style={buttonStyle} disabled={busy}
                    onClick={() => { void toggleDisabled(item); }}>
                    {item.disabled ? `启用 ${item.displayName}`
                      : `禁用 ${item.displayName}`}
                  </button>
                  <button style={buttonStyle} disabled={busy}
                    onClick={() => { void revokeSessions(item); }}>
                    撤销登录 {item.displayName}
                  </button>
                </>
              ) : null}
            </div>
          ))}
        </div>

        <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
          <select aria-label="邀请成员" style={{ ...inputStyle, flex: 1 }}
            value={inviteTarget}
            onChange={(event) => setInviteUserId(event.target.value)}>
            <option value="">选择成员</option>
            {memberOptions.map((item) => (
              <option key={item.userId} value={item.userId}>
                {item.displayName}
              </option>
            ))}
          </select>
          <button style={buttonStyle} disabled={busy}
            onClick={() => { void createInvite(); }}>
            创建邀请
          </button>
        </div>

        {issued ? (
          <section style={{
            border: '1px solid #4b3b7a', background: '#191625',
            borderRadius: '10px', padding: '12px', display: 'grid',
            gap: '8px',
          }} aria-label="邀请签发结果">
            <div style={{ fontSize: '13px', color: '#d8d2ea' }}>
              邀请明文只显示这一次：
            </div>
            <code style={{
              wordBreak: 'break-all', fontSize: '13px', color: '#f0b45a',
            }}>{issued.token}</code>
            <div style={{ display: 'flex', gap: '8px' }}>
              <button style={buttonStyle}
                onClick={() => { void copyInvite(); }}>复制邀请令牌</button>
              <button style={buttonStyle}
                onClick={() => setIssued(null)}>关闭邀请结果</button>
            </div>
          </section>
        ) : null}

        <div style={{ display: 'grid', gap: '6px' }}>
          {invites.map((item) => (
            <div key={item.id} style={{
              display: 'flex', gap: '8px', alignItems: 'center',
              fontSize: '13px', color: '#9aa3b2', flexWrap: 'wrap',
            }}>
              <span>{item.id.slice(0, 8)}</span>
              <span>{item.userId.slice(0, 8)}</span>
              <span>{inviteStatusLabel(item)}</span>
              {isPendingInvite(item) ? (
                <button style={buttonStyle} disabled={busy}
                  onClick={() => { void revokeInvite(item); }}>
                  撤销邀请 {item.id}
                </button>
              ) : null}
            </div>
          ))}
        </div>
      </section>
    </section>
  );
}
