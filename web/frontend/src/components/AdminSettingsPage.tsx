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
  display: 'grid',
  gap: '16px',
  alignContent: 'start',
  maxWidth: '1080px',
} as const;

const cardStyle = {
  border: '1px solid var(--border)',
  background: 'var(--surface)',
  borderRadius: 'var(--radius-lg)',
  padding: '16px',
  display: 'grid',
  gap: '10px',
} as const;

const buttonStyle = {
  border: '1px solid var(--border)',
  background: 'var(--bg-elev)',
  color: 'var(--text)',
  borderRadius: 'var(--radius)',
  padding: '8px 10px',
  cursor: 'pointer',
  fontSize: '13px',
} as const;

const inputStyle = {
  background: 'var(--bg-elev)',
  color: 'var(--text)',
  border: '1px solid var(--border)',
  borderRadius: '8px',
  padding: '8px 10px',
  fontSize: '13px',
  width: '100%',
  boxSizing: 'border-box',
} as const;

interface Props {
  ctx: AuthContext;
  call: ControlCall;
  section?: 'all' | 'defaults' | 'members';
}

type GuardedResult<T> = { ok: true; value: T } | { ok: false };

export default function AdminSettingsPage({
  ctx, call, section = 'all',
}: Props) {
  const showDefaults = section !== 'members';
  const showMembers = section !== 'defaults';
  const [managed, setManaged] = useState<ManagedSettings | null>(null);
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [invites, setInvites] = useState<AdminInvite[]>([]);
  const [userNextOffset, setUserNextOffset] = useState<number | null>(null);
  const [inviteNextOffset, setInviteNextOffset] = useState<number | null>(null);
  const [usersLoadingMore, setUsersLoadingMore] = useState(false);
  const [invitesLoadingMore, setInvitesLoadingMore] = useState(false);
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
  const inviteSeq = useRef(0);
  const usersLoadingRef = useRef(false);
  const invitesLoadingRef = useRef(false);
  const invitesRefreshingRef = useRef(false);
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
      inviteSeq.current += 1;
    };
  }, []);

  useEffect(() => {
    if (ctx.role !== 'admin') {
      setLoading(false);
      return;
    }
    let alive = true;
    const seq = ++loadSeq.current;
    const inviteSeqAtStart = ++inviteSeq.current;
    invitesRefreshingRef.current = true;
    // 整页重读也是第一页刷新：使旧分页失效并回收其加载状态
    invitesLoadingRef.current = false;
    setInvitesLoadingMore(false);
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
          setUserNextOffset(parsedUsers.nextOffset);
          const firstMember = parsedUsers.users.find((item) =>
            item.role === 'member' && !item.disabled);
          setInviteUserId((current) => current || firstMember?.userId || '');
        } catch (error: unknown) {
          errors.push(`成员：${errorText(error)}`);
        }
      } else {
        errors.push(`成员：${errorText(usersResult.reason)}`);
      }
      if (inviteSeqAtStart === inviteSeq.current) {
        if (invitesResult.status === 'fulfilled') {
          try {
            const parsedInvites = parseAdminInvitePage(invitesResult.value);
            setInvites(parsedInvites.invites);
            setInviteNextOffset(parsedInvites.nextOffset);
          } catch (error: unknown) {
            errors.push(`邀请：${errorText(error)}`);
          }
        } else {
          errors.push(`邀请：${errorText(invitesResult.reason)}`);
        }
      }
      if (errors.length) setLoadError(`读取失败：${errors.join('；')}`);
    }).finally(() => {
      if (inviteSeqAtStart === inviteSeq.current) {
        invitesRefreshingRef.current = false;
      }
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

  const loadMoreUsers = async () => {
    if (userNextOffset === null || usersLoadingRef.current) return;
    usersLoadingRef.current = true;
    setUsersLoadingMore(true);
    const owner = ownerRef.current;
    const seq = loadSeq.current;
    try {
      const raw = await call<unknown>(
        `/api/admin/users?limit=50&offset=${userNextOffset}`);
      if (!mounted.current || ownerRef.current !== owner
          || seq !== loadSeq.current) return;
      const page = parseAdminUserPage(raw);
      setUsers((current) => {
        const seen = new Set(current.map((item) => item.userId));
        return [...current,
          ...page.users.filter((item) => !seen.has(item.userId))];
      });
      setUserNextOffset(page.nextOffset);
    } catch (error: unknown) {
      if (mounted.current) {
        setNotice(`成员列表读取失败：${errorText(error)}`);
      }
    } finally {
      usersLoadingRef.current = false;
      if (mounted.current) setUsersLoadingMore(false);
    }
  };

  const loadMoreInvites = async () => {
    if (inviteNextOffset === null || invitesLoadingRef.current
        || invitesRefreshingRef.current) return;
    invitesLoadingRef.current = true;
    setInvitesLoadingMore(true);
    const owner = ownerRef.current;
    const seq = inviteSeq.current;
    try {
      const raw = await call<unknown>(
        `/api/admin/invites?limit=50&offset=${inviteNextOffset}`);
      if (!mounted.current || ownerRef.current !== owner
          || seq !== inviteSeq.current) return;
      const page = parseAdminInvitePage(raw);
      setInvites((current) => {
        const seen = new Set(current.map((item) => item.id));
        return [...current,
          ...page.invites.filter((item) => !seen.has(item.id))];
      });
      setInviteNextOffset(page.nextOffset);
    } catch (error: unknown) {
      if (mounted.current && ownerRef.current === owner
          && seq === inviteSeq.current) {
        setNotice(`邀请列表读取失败：${errorText(error)}`);
      }
    } finally {
      if (seq === inviteSeq.current) {
        invitesLoadingRef.current = false;
        if (mounted.current) setInvitesLoadingMore(false);
      }
    }
  };

  const refreshInvites = async () => {
    const owner = ownerRef.current;
    const seq = ++inviteSeq.current;
    invitesRefreshingRef.current = true;
    // 刷新使旧分页立即失效，并回收旧请求的加载状态
    invitesLoadingRef.current = false;
    setInvitesLoadingMore(false);
    try {
      const raw = await call<unknown>(
        '/api/admin/invites?limit=50&offset=0');
      if (!mounted.current || ownerRef.current !== owner
          || seq !== inviteSeq.current) return;
      const page = parseAdminInvitePage(raw);
      setInvites(page.invites);
      setInviteNextOffset(page.nextOffset);
    } catch (error: unknown) {
      if (mounted.current && ownerRef.current === owner
          && seq === inviteSeq.current) {
        setNotice(`邀请列表读取失败：${errorText(error)}`);
      }
    } finally {
      if (seq === inviteSeq.current) {
        invitesRefreshingRef.current = false;
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
        ? `已禁用 ${updated.displayName}；旧登录与全部登录码已失效。`
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
      setNotice('永久登录码已签发，可重复使用；请保存明文，本次关闭后不再显示。');
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
        <div role="alert" style={{ color: 'var(--red)', fontSize: '14px' }}>
          无权访问管理员设置。
        </div>
      </section>
    );
  }

  const entry = managed?.defaults[channel];
  const chatEntry = managed?.defaults.chat;
  const chatReady = !!chatEntry && !!chatEntry.provider
    && !!chatEntry.model && chatEntry.credential_configured;
  const heading = showMembers && !showDefaults ? '成员与邀请' : '全体默认';
  const memberOptions = users.filter((item) =>
    item.role === 'member' && !item.disabled);
  const selectedUser = users.find((item) => item.userId === inviteUserId);
  const activeInviteUserId = selectedUser?.disabled ? '' : inviteUserId;
  const loadedTarget = memberOptions.find((item) =>
    item.userId === activeInviteUserId);
  const inviteTarget = activeInviteUserId
    || memberOptions[0]?.userId || '';
  const missingInviteTarget = activeInviteUserId !== '' && !loadedTarget;

  return (
    <section style={panelStyle} aria-label="管理员设置">
      <header style={{ display: 'grid', gap: '6px' }}>
        <h1 style={{ margin: 0, fontSize: '20px' }}>{heading}</h1>
        <p style={{ margin: 0, color: 'var(--text-secondary)', fontSize: '13px' }}>
          {showDefaults && showMembers
            ? '这里修改的是所有成员继承的默认配置；个人覆盖与成员管理分开操作。'
            : showDefaults
              ? '主聊天与能力模型的全体默认；未配置主聊天时成员无法开始对话。'
              : '创建成员、禁用账号、撤销登录与签发永久登录码；同账号新登录会使旧登录失效。'}
        </p>
      </header>

      {loadError ? (
        <div role="alert" style={{ color: 'var(--red)', fontSize: '14px' }}>
          {loadError}
        </div>
      ) : null}
      {loading ? (
        <p style={{ color: 'var(--text-secondary)', fontSize: '13px' }}>读取管理员设置…</p>
      ) : null}
      {notice ? (
        <div role="status" style={{
          color: notice.includes('失败') || notice.includes('冲突')
            ? 'var(--red)' : 'var(--green)',
          fontSize: '13px',
        }}>{notice}</div>
      ) : null}

      {showDefaults && managed ? (
        <div className={`settings-guide${
          chatReady ? ' settings-guide--ok' : ''}`}
        role="status" aria-label="主聊天状态">
          <strong>{chatReady ? '主聊天已配置' : '主聊天尚未配置'}</strong>
          <span>
            {chatReady
              ? `当前受管模型：${chatEntry?.model}。配置已保存；`
                + '实际可用性以运行时结果为准。'
              : '成员登录后将无法开始对话。请选择「主聊天」通道，'
                + '填写受管模型与凭证，再保存全体默认。'}
          </span>
          {!chatReady ? (
            <button className="btn btn-sm btn-primary"
              disabled={busy || loading}
              onClick={() => chooseChannel('chat')}>
              配置主聊天
            </button>
          ) : null}
        </div>
      ) : null}

      {showDefaults && managed ? (
        <section className="settings-section" style={cardStyle}
          aria-label="全体默认配置">
          <h2 style={{ margin: 0, fontSize: '16px' }}>
            受管默认模型 · 版本 {managed.version}
          </h2>
          <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
            {ADMIN_CHANNELS.map((item) => (
              <button key={item} className="settings-choice" style={{
                ...buttonStyle,
                borderColor: item === channel
                  ? 'var(--accent-end)' : 'var(--border)',
                background: item === channel
                  ? 'var(--accent-soft)' : 'var(--bg-elev)',
                color: item === channel
                  ? 'var(--accent-end)' : 'var(--text)',
              }} aria-pressed={item === channel}
              disabled={busy || loading}
              onClick={() => chooseChannel(item)}>
                {channelLabel(item)}
              </button>
            ))}
          </div>
          <div style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>
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
          <p style={{ margin: 0, color: 'var(--text-tertiary)', fontSize: '12px' }}>
            留空 Key 仅在已有该通道、同供应商/地址/协议时复用原受管凭证；
            新增通道或更换作用域必须显式输入。
          </p>
          <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
            <button style={{ ...buttonStyle, borderColor: 'var(--accent-end)' }}
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

      {showMembers ? (
      <section className="settings-section" style={cardStyle}
        aria-label="成员与邀请">
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
              flexWrap: 'wrap', fontSize: '13px', color: 'var(--text-secondary)',
            }}>
              <span style={{ minWidth: '120px' }}>{item.displayName}</span>
              <span style={{ color: 'var(--text-secondary)' }}>
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
          {userNextOffset !== null ? (
            <button style={buttonStyle} disabled={busy || usersLoadingMore}
              onClick={() => { void loadMoreUsers(); }}>
              {usersLoadingMore ? '加载中…' : '加载更多成员'}
            </button>
          ) : null}
        </div>

        <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
          <select aria-label="邀请成员" style={{ ...inputStyle, flex: 1 }}
            value={inviteTarget}
            onChange={(event) => setInviteUserId(event.target.value)}>
            <option value="">选择成员</option>
            {missingInviteTarget ? (
              <option value={inviteTarget}>已选成员（后页）</option>
            ) : null}
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
            border: '1px solid var(--border-strong)', background: 'var(--surface-2)',
            borderRadius: '10px', padding: '12px', display: 'grid',
            gap: '8px',
          }} aria-label="邀请签发结果">
            <div style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>
              永久登录码（可重复登录，明文只显示这一次，请保存）：
            </div>
            <code style={{
              wordBreak: 'break-all', fontSize: '13px', color: 'var(--amber)',
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
              fontSize: '13px', color: 'var(--text-secondary)', flexWrap: 'wrap',
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
          {inviteNextOffset !== null ? (
            <button style={buttonStyle}
              disabled={busy || loading || invitesLoadingMore}
              onClick={() => { void loadMoreInvites(); }}>
              {invitesLoadingMore ? '加载中…' : '加载更多邀请'}
            </button>
          ) : null}
        </div>
      </section>
      ) : null}
    </section>
  );
}
